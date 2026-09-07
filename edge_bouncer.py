import os
import time
import asyncio
import logging
from collections import defaultdict
from fastapi import Request, HTTPException

log = logging.getLogger("x402-gateway")

# Redis configuration for Vercel Serverless (Upstash)
UPSTASH_REDIS_URL = os.getenv("UPSTASH_REDIS_REST_URL")
_redis_client = None

if UPSTASH_REDIS_URL:
    try:
        import redis
        _redis_client = redis.Redis.from_url(UPSTASH_REDIS_URL, decode_responses=True)
    except Exception as e:
        log.warning("Redis connection failed, defaulting to memory fallback: %s", e)

# Local fallback cache (WARNING: Vulnerable to replay attacks on serverless cold starts)
_local_cache = {}
TTL_SECONDS = 86400
WINDOW_SECONDS = 60
MAX_BAD_HASHES = 3
RPC_TIMEOUT_SECONDS = 3.0

_bad_hash_tracker = defaultdict(list)

def _check_and_burn_tx(tx_hash: str) -> bool:
    """
    Atomic check & burn using Redis SETNX with 24h TTL.
    Returns True if transaction is fresh and burned, False if replayed.
    """
    if _redis_client:
        try:
            acquired = _redis_client.set(f"x402:tx:{tx_hash}", int(time.time()), ex=TTL_SECONDS, nx=True)
            return bool(acquired)
        except Exception as e:
            log.error("Redis SETNX error: %s", e)

    # Local in-memory fallback
    now = time.time()
    if tx_hash in _local_cache and (now - _local_cache[tx_hash] < TTL_SECONDS):
        return False
    _local_cache[tx_hash] = now
    return True

import httpx

# Solana Mainnet USDC Mint Address
SOLANA_USDC_MINT = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"
# Your specific Solana ATA that should receive the funds
MERCHANT_ATA = "5pM5w1W5nKU7B8SjuTiz65UAZxs9mCnC6ab1Xs1gSi3" 
# Required payment amount in UI units (e.g., 0.005 USDC)
REQUIRED_UI_AMOUNT = 0.005 

SOLANA_RPC_URL = os.getenv("HELIUS_RPC_URL") # Must be set in Vercel env vars

async def verify_solana_transaction(tx_hash: str, required_memo: str = None) -> bool:
    """
    Queries the Solana RPC to cryptographically verify an SPL Token transfer.
    Checks: 1. Tx success, 2. Correct Mint (USDC), 3. Correct Recipient (Merchant ATA), 
    4. Sufficient Amount, 5. Memo match (if provided).
    """
    if not SOLANA_RPC_URL:
        log.error("HELIUS_RPC_URL not configured in environment.")
        return False

    payload = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "getTransaction",
        "params": [
            tx_hash,
            {
                "encoding": "jsonParsed",
                "maxSupportedTransactionVersion": 0,
                "commitment": "confirmed" # Use 'confirmed' for speed, 'finalized' for max security
            }
        ]
    }

    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            response = await client.post(SOLANA_RPC_URL, json=payload)
            response.raise_for_status()
            data = response.json()

        if "error" in data or not data.get("result"):
            log.warning(f"RPC Error or Tx not found for {tx_hash}")
            return False

        result = data["result"]
        meta = result.get("meta")

        # 1. Check if transaction failed on-chain
        if meta.get("err") is not None:
            log.warning(f"Transaction failed on-chain: {meta['err']}")
            return False

        # Better approach: Look at the actual SPL Transfer instructions
        instructions = result["transaction"]["message"]["instructions"]
        inner_instructions = meta.get("innerInstructions", [])
        
        all_instructions = instructions
        for inner in (inner_instructions or []):
            all_instructions.extend(inner["instructions"])

        transfer_verified = False
        for ix in all_instructions:
            if isinstance(ix, dict) and ix.get("program") == "spl-token":
                parsed = ix.get("parsed")
                if parsed and parsed.get("type") == "transferChecked":
                    info = parsed["info"]
                    
                    # Verify destination is our ATA
                    if info.get("destination") == MERCHANT_ATA:
                        # Verify Mint is USDC
                        if info.get("mint") == SOLANA_USDC_MINT:
                            # Verify Amount
                            ui_amount = float(info["tokenAmount"]["uiAmountString"])
                            if ui_amount >= REQUIRED_UI_AMOUNT:
                                transfer_verified = True
                                break

        if not transfer_verified:
            log.warning(f"Valid transfer to {MERCHANT_ATA} of >= {REQUIRED_UI_AMOUNT} USDC not found in tx.")
            return False

        # 3. (Optional but recommended) Verify Memo instruction to prevent cross-merchant replay
        if required_memo:
            memo_found = False
            for ix in all_instructions:
                if isinstance(ix, dict) and ix.get("program") == "spl-memo":
                    if ix.get("parsed") == required_memo:
                        memo_found = True
                        break
            if not memo_found:
                log.warning(f"Required memo '{required_memo}' not found in transaction.")
                return False

        return True

    except httpx.TimeoutException:
        log.error("Solana RPC request timed out.")
        return False
    except Exception as e:
        log.error(f"Unexpected error during RPC verification: {e}")
        return False

async def _verify_onchain_receipt(tx_hash: str, network: str, invoice_id: str = None) -> bool:
    """Circuit breaker wrapper with strict timeout."""
    if network != "solana":
        # Add Base/EVM logic here later
        return False 
        
    try:
        return await asyncio.wait_for(
            verify_solana_transaction(tx_hash, required_memo=invoice_id), 
            timeout=RPC_TIMEOUT_SECONDS
        )
    except asyncio.TimeoutError:
        log.warning(f"[CIRCUIT BREAKER] RPC Timeout on hash {tx_hash}")
        return False

async def verify_x402_proof(request: Request) -> bool:
    """FastAPI Dependency to verify x402 payment headers."""
    tx_proof = request.headers.get("X-Payment-Proof") or request.headers.get("x-payment-proof")
    network = request.headers.get("X-Payment-Network", "solana").lower()
    invoice_id = request.headers.get("X-Invoice-Id") or request.headers.get("x-invoice-id")
    client_ip = request.headers.get("x-forwarded-for", "unknown").split(",")[0].strip()

    if not tx_proof:
        return False

    now = time.time()
    _bad_hash_tracker[client_ip] = [t for t in _bad_hash_tracker[client_ip] if now - t < WINDOW_SECONDS]
    if len(_bad_hash_tracker[client_ip]) >= MAX_BAD_HASHES:
        raise HTTPException(status_code=429, detail="x402 Gateway: Rate-limited due to invalid proofs.")

    try:
        is_valid = await _verify_onchain_receipt(tx_proof, network, invoice_id)
    except Exception as e:
        log.error(f"Verification error: {e}")
        is_valid = False

    if not is_valid:
        _bad_hash_tracker[client_ip].append(now)
        raise HTTPException(status_code=402, detail="x402 Gateway: Invalid or failed payment proof.")

    if not _check_and_burn_tx(tx_proof):
        raise HTTPException(status_code=402, detail="x402 Gateway: Replayed payment proof.")

    return True

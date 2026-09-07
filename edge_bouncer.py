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

async def _verify_onchain_receipt(tx_hash: str, network: str) -> bool:
    """Mock on-chain verification hook. Replace with actual Solana RPC call."""
    if not tx_hash or len(tx_hash) < 32: return False
    await asyncio.sleep(0.05)
    return True

async def verify_x402_proof(request: Request) -> bool:
    """FastAPI Dependency to verify x402 payment headers."""
    tx_proof = request.headers.get("X-Payment-Proof") or request.headers.get("x-payment-proof")
    network = request.headers.get("X-Payment-Network", "solana").lower()
    client_ip = request.headers.get("x-forwarded-for", "unknown").split(",")[0].strip()

    if not tx_proof:
        return False

    now = time.time()
    _bad_hash_tracker[client_ip] = [t for t in _bad_hash_tracker[client_ip] if now - t < WINDOW_SECONDS]
    if len(_bad_hash_tracker[client_ip]) >= MAX_BAD_HASHES:
        raise HTTPException(status_code=429, detail="x402 Gateway: Rate-limited due to invalid proofs.")

    try:
        is_valid = await asyncio.wait_for(_verify_onchain_receipt(tx_proof, network), timeout=RPC_TIMEOUT_SECONDS)
    except asyncio.TimeoutError:
        return False

    if not is_valid:
        _bad_hash_tracker[client_ip].append(now)
        raise HTTPException(status_code=402, detail="x402 Gateway: Invalid or failed payment proof.")

    if not _check_and_burn_tx(tx_proof):
        raise HTTPException(status_code=402, detail="x402 Gateway: Replayed payment proof.")

    return True

import asyncio
import httpx
import logging
import base58

try:
    from solders.keypair import Keypair
    from solders.pubkey import Pubkey
    from solders.transaction import VersionedTransaction
    from solders.message import MessageV0
    from solana.rpc.async_api import AsyncClient
    from spl.token.instructions import transfer_checked, TransferCheckedParams, get_associated_token_address
    from spl.token.constants import TOKEN_PROGRAM_ID
    import spl.memo.instructions as memo
    HAS_SOLANA = True
except ImportError:
    HAS_SOLANA = False

log = logging.getLogger("live-fire")
logging.basicConfig(level=logging.INFO)

DEVNET_RPC = "https://api.devnet.solana.com"
# A known Devnet USDC mint (often used on devnet)
DEVNET_USDC_MINT = Pubkey.from_string("4zMMC9srt5Ri5X14GAgXhaHii3GnPAEERYPJgZJDncDU")
DEVNET_MERCHANT_ATA = Pubkey.from_string("5pM5w1W5nKU7B8SjuTiz65UAZxs9mCnC6ab1Xs1gSi3") # Use your real devnet ATA here

GATEWAY_URL = "http://127.0.0.1:8000/api/v1/parse"

async def run_live_fire_test():
    print("--- LIVE FIRE DEVNET TEST ---")
    if not HAS_SOLANA:
        print("Error: solana and solders packages are required.")
        print("Run: pip install solana solders")
        return

    payer = Keypair()
    client = AsyncClient(DEVNET_RPC)
    
    print(f"[1] Requesting airdrop for {payer.pubkey()}...")
    try:
        airdrop_resp = await client.request_airdrop(payer.pubkey(), 1_000_000_000) # 1 SOL
        await client.confirm_transaction(airdrop_resp.value, commitment="confirmed")
        print("    Airdrop confirmed.")
    except Exception as e:
        print(f"    Airdrop failed (Devnet might be rate limited): {e}")
        
    print("[2] Constructing real SPL Token Transfer (0.005 USDC)...")
    invoice_id = "INV-12345"
    
    # 1. Derive the Payer's Associated Token Account (ATA) for Devnet USDC
    payer_ata = get_associated_token_address(payer.pubkey(), DEVNET_USDC_MINT)

    # 2. Build the SPL Transfer Instruction (0.005 USDC = 5000 base units)
    transfer_ix = transfer_checked(
        TransferCheckedParams(
            program_id=TOKEN_PROGRAM_ID,
            source=payer_ata,
            mint=DEVNET_USDC_MINT,
            dest=DEVNET_MERCHANT_ATA,
            owner=payer.pubkey(),
            amount=5_000, 
            decimals=6
        )
    )

    # 3. Build the Memo Instruction (Crucial for your Edge Bouncer's replay guard)
    memo_ix = memo.create_create_memo_instruction(invoice_id.encode('utf-8'), payer.pubkey())

    # 4. Fetch Blockhash and Compile V0 Message
    recent_blockhash_resp = await client.get_latest_blockhash()
    msg = MessageV0.try_compile(
        payer=payer.pubkey(),
        instructions=[transfer_ix, memo_ix],
        address_lookup_table_accounts=[],
        recent_blockhash=recent_blockhash_resp.value.blockhash
    )

    # 5. Sign and Broadcast
    tx = VersionedTransaction(msg, [payer])
    print("[3] Broadcasting real transaction to Solana Devnet...")
    try:
        sig_resp = await client.send_transaction(tx)
        tx_signature = str(sig_resp.value)
        print(f"    Broadcast successful. Signature: {tx_signature}")

        # Wait for confirmation before hitting the gateway
        print("    Waiting for network confirmation...")
        await client.confirm_transaction(sig_resp.value, commitment="confirmed")
    except Exception as e:
        print(f"    Transaction failed (likely due to unfunded test wallet): {e}")
        print("    Generating dummy signature to demonstrate Gateway Pipeline...")
        tx_signature = base58.b58encode(b"dummy_signature_for_testing_" * 2)[:88].decode('utf-8')
    
    print(f"[4] Hitting the Gateway with Proof: {tx_signature[:16]}...")
    async with httpx.AsyncClient() as http:
        try:
            response = await http.post(
                GATEWAY_URL,
                json={"data": "test payload"},
                headers={
                    "X-Payment-Proof": tx_signature,
                    "X-Payment-Network": "solana",
                    "X-Invoice-Id": invoice_id
                }
            )
            print(f"    Gateway Response: {response.status_code}")
            print(f"    Body: {response.text}")
        except httpx.ConnectError:
            print(f"    Failed to connect to gateway at {GATEWAY_URL}. Is it running?")
            
    await client.close()

if __name__ == "__main__":
    asyncio.run(run_live_fire_test())

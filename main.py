import json
import base64
from fastapi import FastAPI, Request, Depends
from fastapi.responses import JSONResponse
from edge_bouncer import verify_x402_proof

app = FastAPI(title="x402 Vercel Gateway Reference")

def x402_challenge_response():
    payload = {
        "x402Version": 2,
        "error": "Payment required",
        "accepts": [{
            "network": "solana",
            "maxAmountRequired": "50000",
            "asset": "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v", # USDC
            "payTo": "YOUR_WALLET_ADDRESS"
        }]
    }
    b64 = base64.b64encode(json.dumps(payload, separators=(",", ":")).encode()).decode()
    return JSONResponse(
        payload,
        status_code=402,
        headers={"PAYMENT-REQUIRED": b64}
    )

@app.get("/api/data")
async def get_data(request: Request, has_proof: bool = Depends(verify_x402_proof)):
    if not has_proof:
        return x402_challenge_response()
    
    return JSONResponse({"ok": True, "data": "This is protected premium data."})

"""Browser-wallet finish page for one ready transfer.

This is the app's only authored HTML and JavaScript: injected EIP-1193 wallets are
reachable only from page JavaScript. The page asks the existing API to revalidate and
prepare the transaction; the user's wallet signs and broadcasts it. No key reaches the server.
"""
import json
import re
import secrets
from pathlib import Path

from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse, HTMLResponse

from rebound import protocol as p

ARC_CHAIN_ID = 5042002
# Offered to wallets that lack Arc Testnet. The server's configured RPC can carry
# credentials, so it is never sent to browsers.
ARC_PUBLIC_RPC = "https://rpc.testnet.arc.network"
BASE_EXPLORER = "https://sepolia.basescan.org"
LOG_INDEX_PATTERN = re.compile(r"[0-9]{1,6}", re.ASCII)
MAX_LOG_INDEX = 100_000  # Same bound as PrepareRequest.log_index.
PAGE = Path(__file__).with_name("wallet.html").read_text(encoding="utf-8")
FONT = Path(__file__).resolve().parent.parent / "assets" / "fonts" / "Manrope.ttf"

router = APIRouter(include_in_schema=False)


def security_headers(nonce: str) -> dict[str, str]:
    policy = "; ".join([
        "default-src 'none'",
        f"script-src 'nonce-{nonce}'",
        f"style-src 'nonce-{nonce}'",
        f"connect-src 'self' {ARC_PUBLIC_RPC}",
        "img-src 'self' data:",
        "font-src 'self'",
        "frame-ancestors 'none'",
        "base-uri 'none'",
        "form-action 'none'",
    ])
    return {"Content-Security-Policy": policy, "X-Frame-Options": "DENY", "Cache-Control": "no-store",
            "Referrer-Policy": "no-referrer", "X-Content-Type-Options": "nosniff"}


def render_page(tx_hash: str, log_index: int, nonce: str) -> str:
    config = {
        "txHash": tx_hash, "logIndex": log_index, "transmitter": p.TRANSMITTER,
        "chainId": ARC_CHAIN_ID, "chainHex": hex(ARC_CHAIN_ID), "selector": "0x" + p.RECEIVE_SELECTOR.hex(),
        "rpc": ARC_PUBLIC_RPC, "explorer": p.ARC_EXPLORER, "baseExplorer": BASE_EXPLORER,
    }
    # Inputs are validated hex, but never let injected data close the script element.
    data = json.dumps(config).replace("<", "\\u003c").replace(">", "\\u003e").replace("&", "\\u0026")
    return PAGE.replace("{{NONCE}}", nonce).replace("{{CONFIG}}", data)


@router.get("/finish/manrope.ttf")
async def finish_font():
    return FileResponse(FONT, media_type="font/ttf",
                        headers={"Cache-Control": "public, max-age=86400", "X-Content-Type-Options": "nosniff"})


@router.get("/finish/{tx_hash}/{log_index}", response_class=HTMLResponse)
async def finish_page(tx_hash: str, log_index: str):
    try:
        tx_hash = p.hash32(tx_hash)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    if not LOG_INDEX_PATTERN.fullmatch(log_index) or int(log_index) > MAX_LOG_INDEX:
        raise HTTPException(400, "Event log index must be a decimal integer from 0 to 100000.")
    nonce = secrets.token_urlsafe(18)
    return HTMLResponse(render_page(tx_hash, int(log_index), nonce), headers=security_headers(nonce))

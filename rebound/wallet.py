"""HTTP surface for the browser-wallet adapter.

The review and status UI is Flet (rebound/wallet_ui.py). Injected EIP-1193 wallets
are reachable only from page JavaScript, so this router serves one minimal adapter
page (a status line plus rebound/wallet_adapter.js) and the four session calls it
needs. Sessions are created in-process by the Flet UI; there is no public create
endpoint. No key reaches the server.
"""
import json
import secrets
from pathlib import Path
from typing import Literal

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse
from pydantic import BaseModel, Field

from rebound import protocol as p
from rebound.wallet_session import (
    ARC_CHAIN_HEX,
    MAX_GAS_LIMIT,
    MAX_NETWORK_FEE_WEI,
    SessionConflict,
    SessionNotFound,
    sessions_for,
)

# Offered to wallets that lack Arc Testnet. The server's configured RPC can carry
# credentials, so it is never sent to browsers.
ARC_PUBLIC_RPC = "https://rpc.testnet.arc.network"
SCRIPT = Path(__file__).with_name("wallet_adapter.js").read_text(encoding="utf-8")
PAGE = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Rebound wallet connection</title>
</head>
<body>
<p id="status" role="status" aria-live="polite">Rebound is connecting to your wallet. Return to the Rebound tab to review.</p>
<script nonce="{nonce}">const CONFIG = {config};
{script}</script>
</body>
</html>
"""
NO_STORE = {"Cache-Control": "no-store"}

router = APIRouter(include_in_schema=False)


class Connected(BaseModel):
    account: str = Field(pattern=r"^0x[0-9a-fA-F]{40}$")
    chainId: str = Field(pattern=r"^0x[0-9a-fA-F]{1,16}$")


class Outcome(BaseModel):
    outcome: Literal["submitted", "rejected", "not_sent", "unknown"]
    hash: str | None = Field(default=None, pattern=r"^0x[0-9a-fA-F]{64}$")
    code: int | None = None
    reason: str = Field(default="", max_length=200)


def security_headers(nonce: str) -> dict[str, str]:
    policy = "; ".join([
        "default-src 'none'",
        f"script-src 'nonce-{nonce}'",
        "connect-src 'self'",
        "frame-ancestors 'none'",
        "base-uri 'none'",
        "form-action 'none'",
    ])
    return {"Content-Security-Policy": policy, "X-Frame-Options": "DENY", "Cache-Control": "no-store",
            "Referrer-Policy": "no-referrer", "X-Content-Type-Options": "nosniff"}


def render_adapter(session_id: str, nonce: str) -> str:
    config = {
        "session": session_id, "transmitter": p.TRANSMITTER, "chainHex": ARC_CHAIN_HEX,
        "selector": "0x" + p.RECEIVE_SELECTOR.hex(), "rpc": ARC_PUBLIC_RPC, "explorer": p.ARC_EXPLORER,
        "maxGas": str(MAX_GAS_LIMIT), "maxNetworkFee": str(MAX_NETWORK_FEE_WEI),
    }
    # Values are validated, but never let injected data close the script element.
    data = json.dumps(config).replace("<", "\\u003c").replace(">", "\\u003e").replace("&", "\\u0026")
    return PAGE.format(nonce=nonce, config=data, script=SCRIPT)


def manager(request: Request):
    return sessions_for(request.app.state.recovery)


def session_or_404(request: Request, session_id: str):
    try:
        return manager(request).get(session_id)
    except SessionNotFound as exc:
        raise HTTPException(404, str(exc)) from exc


async def guarded(operation):
    try:
        return await operation()
    except SessionNotFound as exc:
        raise HTTPException(404, str(exc)) from exc
    except SessionConflict as exc:
        raise HTTPException(409, str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc


@router.get("/wallet/adapter/{session_id}", response_class=HTMLResponse)
async def adapter_page(request: Request, session_id: str):
    session = session_or_404(request, session_id)
    nonce = secrets.token_urlsafe(18)
    return HTMLResponse(render_adapter(session.id, nonce), headers=security_headers(nonce))


@router.get("/api/wallet-sessions/{session_id}")
async def session_state(request: Request, session_id: str):
    return JSONResponse(session_or_404(request, session_id).adapter_view(), headers=NO_STORE)


@router.post("/api/wallet-sessions/{session_id}/connected")
async def connected(request: Request, session_id: str, payload: Connected):
    async def run():
        return await manager(request).connect(session_id, payload.account, payload.chainId)
    session = await guarded(run)
    return JSONResponse(session.adapter_view(), headers=NO_STORE)


@router.post("/api/wallet-sessions/{session_id}/claim")
async def claim(request: Request, session_id: str):
    async def run():
        return manager(request).claim(session_id)
    return JSONResponse(await guarded(run), headers=NO_STORE)


@router.post("/api/wallet-sessions/{session_id}/result")
async def result(request: Request, session_id: str, payload: Outcome):
    async def run():
        return manager(request).report(session_id, payload.outcome, tx_hash=payload.hash,
                                       reason=payload.reason, code=payload.code)
    session = await guarded(run)
    return JSONResponse(session.adapter_view(), headers=NO_STORE)

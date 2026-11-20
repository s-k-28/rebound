"""HTTP surface for the browser-wallet adapter.

The review and status UI is Flet (rebound/wallet_ui.py). Injected EIP-1193 wallets
are reachable only from page JavaScript, so this router serves one minimal adapter
page (a status line plus rebound/wallet_adapter.js) and the session calls it needs.
Sessions are created in-process by the Flet UI; there is no public create endpoint.
No key reaches the server.

No secret is ever in a request path or query string, so none reaches access logs.
The page URL carries a one-time code in its fragment (never sent to the server);
the script trades it in a POST body for the session id and sends that id in the
X-Rebound-Wallet-Session header. The router is mounted only when
REBOUND_BROWSER_WALLET=1.
"""
import json
import secrets
from pathlib import Path
from typing import Annotated, Literal

from fastapi import APIRouter, Header, HTTPException, Request
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
SESSION_HEADER = "X-Rebound-Wallet-Session"

router = APIRouter(include_in_schema=False)


class Code(BaseModel):
    code: str = Field(pattern=r"^[A-Za-z0-9_-]{22}$")


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


def render_adapter(nonce: str) -> str:
    config = {
        "header": SESSION_HEADER, "transmitter": p.TRANSMITTER, "chainHex": ARC_CHAIN_HEX,
        "selector": "0x" + p.RECEIVE_SELECTOR.hex(), "rpc": ARC_PUBLIC_RPC, "explorer": p.ARC_EXPLORER,
        "maxGas": str(MAX_GAS_LIMIT), "maxNetworkFee": str(MAX_NETWORK_FEE_WEI),
    }
    # Values are validated, but never let injected data close the script element.
    data = json.dumps(config).replace("<", "\\u003c").replace(">", "\\u003e").replace("&", "\\u0026")
    return PAGE.format(nonce=nonce, config=data, script=SCRIPT)


def manager(request: Request):
    return sessions_for(request.app.state.recovery)


Token = Annotated[str, Header(alias=SESSION_HEADER)]


def session_or_404(request: Request, session_id: str):
    try:
        session = manager(request).get(session_id)
    except SessionNotFound as exc:
        raise HTTPException(404, str(exc)) from exc
    manager(request).touch(session)
    return session


async def guarded(operation):
    try:
        return await operation()
    except SessionNotFound as exc:
        raise HTTPException(404, str(exc)) from exc
    except SessionConflict as exc:
        raise HTTPException(409, str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc


@router.get("/wallet/adapter", response_class=HTMLResponse)
async def adapter_page():
    nonce = secrets.token_urlsafe(18)
    return HTMLResponse(render_adapter(nonce), headers=security_headers(nonce))


@router.post("/api/wallet-session/exchange")
async def exchange(request: Request, payload: Code):
    async def run():
        return manager(request).exchange(payload.code)
    session = await guarded(run)
    return JSONResponse({"token": session.id, **session.adapter_view()}, headers=NO_STORE)


@router.get("/api/wallet-session")
async def session_state(request: Request, token: Token):
    return JSONResponse(session_or_404(request, token).adapter_view(), headers=NO_STORE)


@router.post("/api/wallet-session/connected")
async def connected(request: Request, payload: Connected, token: Token):
    session_or_404(request, token)

    async def run():
        return await manager(request).connect(token, payload.account, payload.chainId)
    session = await guarded(run)
    return JSONResponse(session.adapter_view(), headers=NO_STORE)


@router.post("/api/wallet-session/claim")
async def claim(request: Request, token: Token):
    session_or_404(request, token)

    async def run():
        return manager(request).claim(token)
    return JSONResponse(await guarded(run), headers=NO_STORE)


@router.post("/api/wallet-session/result")
async def result(request: Request, payload: Outcome, token: Token):
    session_or_404(request, token)

    async def run():
        return manager(request).report(token, payload.outcome, tx_hash=payload.hash,
                                       reason=payload.reason, code=payload.code)
    session = await guarded(run)
    return JSONResponse(session.adapter_view(), headers=NO_STORE)

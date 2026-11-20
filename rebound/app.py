"""ASGI entry point: Python Flet interface and read-only recovery APIs."""
import asyncio
import secrets
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Annotated

import flet.fastapi as flet_fastapi
import httpx
from fastapi import FastAPI, Header, HTTPException, Query, Request
from pydantic import BaseModel, Field

from rebound.service import RecoveryService, Settings, UpstreamError

WALLET_PATTERN = r"^0x[0-9a-fA-F]{40}$"
HASH_PATTERN = r"^0x[0-9a-fA-F]{64}$"


class PrepareRequest(BaseModel):
    tx_hash: str = Field(pattern=HASH_PATTERN)
    log_index: int = Field(ge=0, le=100000)
    caller: str = Field(pattern=WALLET_PATTERN)


def create_app(recovery: RecoveryService | None = None, *, include_ui: bool = True) -> FastAPI:
    client = httpx.AsyncClient(timeout=20)
    recovery = recovery or RecoveryService(Settings.from_env(), client)

    @asynccontextmanager
    async def lifespan(_app):
        if include_ui:
            await flet_fastapi.app_manager.start()
        yield
        if include_ui:
            await flet_fastapi.app_manager.shutdown()
        await client.aclose()

    api = FastAPI(title="Rebound", version="0.1.0", lifespan=lifespan,
                  description="Testnet transfer discovery and unsigned recovery preparation. No web signing.")
    api.state.recovery = recovery
    slots = asyncio.Semaphore(2)

    @api.get("/api/health")
    async def health():
        return {"status": "ok", "network": "testnet", "route": "Base Sepolia → Arc Testnet",
                "signing": "local CLI only", "upstreams": "not checked by this endpoint",
                "stream_enabled": bool(recovery.settings.stream_secret)}

    async def checked(awaitable):
        try:
            async with slots, asyncio.timeout(120):
                return await awaitable
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except UpstreamError as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc
        except TimeoutError as exc:
            raise HTTPException(status_code=504, detail="Verification timed out. Try a shorter history window.") from exc

    @api.get("/api/scan")
    async def scan(wallet: Annotated[str, Query(pattern=WALLET_PATTERN)],
                   blocks: Annotated[int, Query(ge=1, le=50000)] = 2000,
                   end_block: Annotated[int | None, Query(ge=0)] = None):
        return await checked(recovery.scan(wallet, blocks=blocks, end_block=end_block))

    @api.post("/api/prepare")
    async def prepare(payload: PrepareRequest):
        return await checked(recovery.prepare(payload.tx_hash, payload.log_index, payload.caller))

    @api.post("/api/streams")
    async def stream(request: Request, x_rebound_secret: Annotated[str | None, Header()] = None):
        expected = recovery.settings.stream_secret
        if not expected:
            raise HTTPException(503, "Stream ingestion is not configured.")
        if not x_rebound_secret or not secrets.compare_digest(x_rebound_secret, expected):
            raise HTTPException(401, "Invalid Stream authentication.")
        body = bytearray()
        async for chunk in request.stream():
            body.extend(chunk)
            if len(body) > 2_000_000:
                raise HTTPException(413, "Stream payload exceeds 2 MB.")
        import json
        try:
            payload = json.loads(body)
            if isinstance(payload, dict) and "logs" not in payload:
                payload = payload.get("data")
                if isinstance(payload, list):
                    rows = []
                    for batch in payload:
                        if not isinstance(batch, dict) or not isinstance(batch.get("logs"), list):
                            raise ValueError("Invalid Stream delivery envelope.")
                        rows.extend(batch["logs"])
                    payload = {"logs": rows}
            if not isinstance(payload, dict) or not isinstance(payload.get("logs"), list):
                raise ValueError("Expected an object containing an event logs list.")
            return {"accepted": recovery.ingest_logs(payload["logs"]),
                    "note": "Candidates only; chain verification is required before recovery."}
        except (ValueError, TypeError) as exc:
            raise HTTPException(400, "Invalid Stream event payload.") from exc

    if include_ui:
        from rebound.ui import build_main
        api.mount("/", flet_fastapi.app(build_main(recovery), app_name="Rebound",
                  app_short_name="Rebound", app_description="Find and finish interrupted USDC transfers.",
                  assets_dir=str(Path(__file__).resolve().parent.parent / "assets")))
    return api


app = create_app()

"""ASGI entry point: Python Flet interface and read-only recovery APIs."""
import asyncio
import gzip
import hmac
import io
import json
import re
import secrets
import time
import zlib
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Annotated

import flet.fastapi as flet_fastapi
import httpx
from fastapi import FastAPI, Header, HTTPException, Query, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from rebound.service import BusyError, RecoveryService, Settings, UpstreamError
from rebound.wallet import router as wallet_router

WALLET_PATTERN = r"^0x[0-9a-fA-F]{40}$"
HASH_PATTERN = r"^0x[0-9a-fA-F]{64}$"
STREAM_BODY_LIMIT = 2_000_000
STREAM_SIGNATURE_WINDOW_SECONDS = 900
STREAM_MAX_IN_FLIGHT = 4
STREAM_BODY_TIMEOUT_SECONDS = 10


class PrepareRequest(BaseModel):
    tx_hash: str = Field(pattern=HASH_PATTERN)
    log_index: int = Field(ge=0, le=100000)
    caller: str = Field(pattern=WALLET_PATTERN)


def stream_signature_headers(request: Request) -> tuple[bytes, str]:
    nonce = request.headers.get("x-qn-nonce", "")
    timestamp = request.headers.get("x-qn-timestamp", "")
    signature = request.headers.get("x-qn-signature", "")
    if not (0 < len(nonce) <= 128 and nonce.isascii() and timestamp.isascii()
            and timestamp.isdecimal() and len(timestamp) <= 12
            and abs(time.time() - int(timestamp)) <= STREAM_SIGNATURE_WINDOW_SECONDS
            and re.fullmatch(r"[0-9a-fA-F]{64}", signature)):
        raise HTTPException(401, "Invalid or stale Stream signature.")
    return (nonce + timestamp).encode(), signature.lower()


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

    @api.get("/api/health")
    async def health():
        return {"status": "ok", "network": "testnet", "route": "Base Sepolia → Arc Testnet",
                "signing": "local CLI or browser wallet; never server-side" if recovery.settings.browser_wallet
                else "local CLI only", "upstreams": "not checked by this endpoint",
                "stream_enabled": recovery.settings.stream_enabled}

    @api.get("/api/ready")
    async def ready():
        report = await recovery.readiness()
        return JSONResponse(report, status_code=200 if report["status"] == "ready" else 503,
                            headers={"Cache-Control": "no-store"})

    async def checked(operation, *args, **kwargs):
        try:
            return await operation(*args, **kwargs)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except BusyError as exc:
            raise HTTPException(status_code=429, detail=str(exc), headers={"Retry-After": "5"}) from exc
        except UpstreamError as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc
        except TimeoutError as exc:
            raise HTTPException(status_code=504, detail="Verification timed out. Try a shorter history window.") from exc

    @api.get("/api/scan")
    async def scan(wallet: Annotated[str, Query(pattern=WALLET_PATTERN)],
                   blocks: Annotated[int, Query(ge=1, le=50000)] = 2000,
                   end_block: Annotated[int | None, Query(ge=0)] = None):
        return await checked(recovery.scan, wallet, blocks=blocks, end_block=end_block)

    @api.post("/api/prepare")
    async def prepare(payload: PrepareRequest):
        return await checked(recovery.prepare, payload.tx_hash, payload.log_index, payload.caller)

    @api.get("/api/stream-index")
    async def stream_index(wallet: Annotated[str, Query(pattern=WALLET_PATTERN)]):
        return await checked(recovery.scan_stream, wallet)

    @api.get("/api/transactions/{tx_hash}")
    async def transaction(tx_hash: str, log_index: Annotated[int | None, Query(ge=0)] = None,
                          destination_hash: Annotated[str | None, Query(pattern=HASH_PATTERN)] = None):
        return await checked(recovery.inspect_transaction, tx_hash, log_index=log_index,
                             destination_hash=destination_hash)

    @api.get("/api/transactions/{tx_hash}/replay")
    async def replay(tx_hash: str, log_index: Annotated[int, Query(ge=0)]):
        return await checked(recovery.replay_mint, tx_hash, log_index)

    stream_in_flight = 0

    @asynccontextmanager
    async def stream_slot():
        nonlocal stream_in_flight
        # No await between checking and reserving: admission is atomic on this
        # ASGI worker's event loop, with no waiting requests holding upload bodies.
        if stream_in_flight >= STREAM_MAX_IN_FLIGHT:
            raise HTTPException(429, "Stream receiver is busy. Retry delivery shortly.",
                                headers={"Retry-After": "5"})
        stream_in_flight += 1
        try:
            yield
        finally:
            stream_in_flight -= 1

    @api.post("/api/streams")
    async def stream(request: Request, x_rebound_secret: Annotated[str | None, Header()] = None):
        settings = recovery.settings
        token = settings.stream_security_token
        if not settings.stream_enabled:
            raise HTTPException(503, "Stream ingestion is not configured.")
        if not token and not secrets.compare_digest((x_rebound_secret or "").encode(), settings.stream_secret.encode()):
            raise HTTPException(401, "Invalid Stream authentication.")
        signature_headers = stream_signature_headers(request) if token else None
        async with stream_slot():
            return await consume_stream(request, signature_headers, token)

    async def consume_stream(request: Request, signature_headers: tuple[bytes, str] | None, token: str):
        wire_body = bytearray()
        try:
            async with asyncio.timeout(STREAM_BODY_TIMEOUT_SECONDS):
                async for chunk in request.stream():
                    if len(wire_body) + len(chunk) > STREAM_BODY_LIMIT:
                        raise HTTPException(413, "Stream payload exceeds 2 MB.")
                    wire_body.extend(chunk)
        except TimeoutError as exc:
            raise HTTPException(408, "Stream body reading timed out. Retry delivery.") from exc
        encoding = request.headers.get("content-encoding", "identity").lower()
        if encoding == "gzip":
            try:
                with gzip.GzipFile(fileobj=io.BytesIO(wire_body)) as archive:
                    body = archive.read(STREAM_BODY_LIMIT + 1)
            except (OSError, EOFError, ValueError, zlib.error) as exc:
                raise HTTPException(400, "Invalid compressed Stream payload.") from exc
            if len(body) > STREAM_BODY_LIMIT:
                raise HTTPException(413, "Stream payload exceeds 2 MB after decompression.")
        elif encoding == "identity":
            body = bytes(wire_body)
        else:
            raise HTTPException(415, "Stream payload encoding is unsupported.")
        if signature_headers:
            prefix, signature = signature_headers
            expected = hmac.digest(token.encode(), prefix + body, "sha256").hex()
            if not hmac.compare_digest(expected, signature):
                raise HTTPException(401, "Invalid or stale Stream signature.")
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
            accepted = await asyncio.to_thread(recovery.ingest_logs, payload["logs"])
            return {"accepted": accepted,
                    "note": "Candidates only; chain verification is required before recovery."}
        except (ValueError, TypeError, RecursionError) as exc:
            raise HTTPException(400, "Invalid Stream event payload.") from exc

    if recovery.settings.browser_wallet:
        api.include_router(wallet_router)  # Must precede the Flet mount at "/".
    if include_ui:
        from rebound.ui import build_main
        api.mount("/", flet_fastapi.app(build_main(recovery), app_name="Rebound",
                  app_short_name="Rebound", app_description="Find and finish interrupted USDC transfers.",
                  assets_dir=str(Path(__file__).resolve().parent.parent / "assets")))
    return api


app = create_app()

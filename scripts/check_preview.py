"""Read-only HTTPS preview smoke check; no wallet, signing or state mutations.

Usage: uv run python scripts/check_preview.py https://YOUR_PREVIEW
Exit 0: all checks pass; 1: preview/check failed; 2: invalid command arguments.
This checks the Flet HTML entry point, not browser rendering or wallet signing.
"""

import argparse
import asyncio
import json
import re
import sys
from collections.abc import Callable
from urllib.parse import urlsplit

import httpx

SOURCE_TX = "0xcfe897ab891b667c9ae538d9d6713c02db167dfd0a058fc6c09313734de0ecbb"
ARC_TX = "0xd2f74e876f5f9b48ca5836044d0b1fa9723de30672587aacb86b55be6d5bdb00"
SOURCE_LOG = 4
PREVIOUS_BLOCK = 73_127_413
MINT_BLOCK = 73_127_414
BODY_LIMIT = 1_000_000


class CheckFailed(ValueError):
    """A preview response did not satisfy its public contract."""


def https_base(value: str) -> str:
    parsed = urlsplit(value)
    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError(
            "Provide an explicit HTTPS base URL without credentials, query or fragment."
        )
    try:
        parsed.port
    except ValueError:
        raise ValueError("The HTTPS base URL contains an invalid port.") from None
    return value.rstrip("/")


async def fetch(client: httpx.AsyncClient, url: str) -> tuple[bytes, str]:
    async with client.stream("GET", url, follow_redirects=False) as response:
        if response.status_code != 200:
            raise CheckFailed(f"HTTP {response.status_code}")
        content = bytearray()
        async for chunk in response.aiter_bytes():
            if len(content) + len(chunk) > BODY_LIMIT:
                raise CheckFailed("response exceeds 1 MB")
            content.extend(chunk)
        return bytes(content), response.headers.get("content-type", "").lower()


def json_object(body: bytes, content_type: str) -> dict:
    if "application/json" not in content_type:
        raise CheckFailed("expected JSON, received another content type")
    try:
        result = json.loads(body)
    except (ValueError, RecursionError):
        raise CheckFailed("response is not readable JSON") from None
    if not isinstance(result, dict):
        raise CheckFailed("expected a JSON object")
    return result


def validate_health(body: bytes, content_type: str) -> str:
    report = json_object(body, content_type)
    if report.get("status") != "ok" or report.get("network") != "testnet":
        raise CheckFailed("expected Rebound testnet liveness")
    return "testnet process is live"


def validate_readiness(body: bytes, content_type: str) -> str:
    report = json_object(body, content_type)
    providers = report.get("providers")
    expected = ("base_sepolia", "circle_cctp_fee_route", "arc_testnet")
    if (
        report.get("status") != "ready"
        or not isinstance(providers, dict)
        or any(providers.get(name) != "ok" for name in expected)
    ):
        raise CheckFailed("Base, Circle and Arc must all report ready")
    return "Base, Circle and Arc are ready"


def validate_demo(body: bytes, content_type: str) -> str:
    if "text/html" not in content_type or b"<html" not in body.lower():
        raise CheckFailed("expected the demo HTML entry point")
    if b"flutter_bootstrap.js" not in body or b"webSocketEndpoint" not in body:
        raise CheckFailed("demo did not return the Flet app entry point")
    return "Flet demo entry point is accessible (rendering not checked)"


def validate_replay(body: bytes, content_type: str) -> str:
    replay = json_object(body, content_type)
    expected = {
        "source_tx_hash": SOURCE_TX,
        "log_index": SOURCE_LOG,
        "arc_tx_hash": ARC_TX,
        "previous_block": PREVIOUS_BLOCK,
        "mint_block": MINT_BLOCK,
    }
    if any(
        type(replay.get(key)) is not type(value) or replay[key] != value
        for key, value in expected.items()
    ):
        raise CheckFailed("historical replay identifies a different source, mint or block pair")
    nonce = replay.get("nonce")
    if (
        not isinstance(nonce, str)
        or not re.fullmatch(r"0x[0-9a-fA-F]{64}", nonce)
        or int(nonce[2:], 16) == 0
    ):
        raise CheckFailed("historical replay returned an invalid message nonce")
    if (
        replay.get("receive_simulated") is not True
        or replay.get("nonce_used_before") is not False
        or replay.get("nonce_used_at_mint") is not True
    ):
        raise CheckFailed("expected successful simulation and nonce unused before / used at mint")
    return f"simulation passed; nonce unused at {PREVIOUS_BLOCK:,}, used at {MINT_BLOCK:,}"


async def check_preview(
    base: str,
    client: httpx.AsyncClient,
    *,
    timeout: float = 60,
    emit: Callable[[str], None] = print,
) -> int:
    base = https_base(base)
    checks = (
        ("liveness", "/api/health", validate_health),
        ("readiness", "/api/ready", validate_readiness),
        ("demo", "/demo", validate_demo),
        (
            "historical replay",
            f"/api/transactions/{SOURCE_TX}/replay?log_index={SOURCE_LOG}",
            validate_replay,
        ),
    )
    failures = 0
    try:
        async with asyncio.timeout(timeout):
            for name, path, validate in checks:
                try:
                    body, content_type = await fetch(client, base + path)
                    detail = validate(body, content_type)
                except CheckFailed as exc:
                    emit(f"FAIL {name}: {exc}")
                    failures += 1
                except httpx.TimeoutException:
                    emit(f"FAIL {name}: request timed out")
                    failures += 1
                except httpx.HTTPError:
                    emit(f"FAIL {name}: HTTPS request failed")
                    failures += 1
                else:
                    emit(f"PASS {name}: {detail}")
    except TimeoutError:
        emit(f"FAIL deadline: total check exceeded {timeout:g} seconds")
        return 1
    emit(
        f"{'FAIL' if failures else 'PASS'} preview: {len(checks) - failures}/{len(checks)} checks passed"
    )
    return 1 if failures else 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("base", type=https_base, help="Explicit HTTPS preview base URL")
    parser.add_argument(
        "--timeout", type=int, default=60, help="Total wall-clock limit, 5–120 seconds (default 60)"
    )
    args = parser.parse_args()
    if not 5 <= args.timeout <= 120:
        parser.error("--timeout must be between 5 and 120 seconds")

    async def run() -> int:
        async with httpx.AsyncClient(
            timeout=httpx.Timeout(20, connect=8),
            follow_redirects=False,
            headers={"User-Agent": "Rebound-Preview-Smoke/0.1"},
        ) as client:
            return await check_preview(args.base, client, timeout=args.timeout)

    try:
        return asyncio.run(run())
    except KeyboardInterrupt:
        print("FAIL preview: interrupted", file=sys.stderr)
        return 130


if __name__ == "__main__":
    raise SystemExit(main())

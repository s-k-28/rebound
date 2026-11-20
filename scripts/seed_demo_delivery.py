"""Deliver the documented controlled burn to a running Rebound as a local endpoint test.

Usage: REBOUND_STREAM_SECRET=... uv run python scripts/seed_demo_delivery.py [BASE_URL]

This lets the QuickNode walkthrough show a wallet-only discovery when no live Stream is
delivering. It posts with the local shared secret, never a QuickNode signature, so the app
labels the delivery "Shared-secret adapter payload" and states that provider delivery is
unverified. It is not a QuickNode delivery and must not be described as one.

Reads the public receipt of the controlled run in docs/LIVE-RECOVERY.md from Base Sepolia.
Exit 0: delivered; 1: the receipt or the endpoint failed; 2: bad arguments or no secret.
"""

import json
import os
import sys
from ipaddress import ip_address
from pathlib import Path
from urllib.parse import urlsplit

import httpx

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from rebound import protocol as p  # noqa: E402
from rebound.clients import UpstreamError, quantity  # noqa: E402

SOURCE_TX = "0x7e4334109c120f9da71e01116190804a9d91f2fd5ec07cfd7196a649e68e3f23"
SOURCE_LOG = 4
DEFAULT_RPC = "https://sepolia.base.org"
DEFAULT_URL = "http://127.0.0.1:8765"


def burn_log_from_receipt(receipt: dict, log_index: int) -> dict:
    """The unique native-USDC burn, linked to the successful controlled receipt."""
    if (not isinstance(receipt, dict) or receipt.get("status") != "0x1"
            or p.hash32(receipt.get("transactionHash")) != SOURCE_TX):
        raise ValueError("No successful controlled source receipt.")
    block_hash = p.hash32(receipt.get("blockHash"))
    block_number = quantity(receipt.get("blockNumber"))
    logs = receipt.get("logs")
    if not isinstance(logs, list):
        raise ValueError("Invalid receipt logs.")
    matches = []
    for log in logs:
        if not isinstance(log, dict):
            raise ValueError("Invalid receipt log.")
        if quantity(log.get("logIndex")) == log_index:
            matches.append(log)
    if len(matches) != 1:
        raise ValueError("The receipt has no unique controlled burn at this event position.")
    log = matches[0]
    burn = p.decode_burn(log)
    if (burn.tx_hash != SOURCE_TX or burn.log_index != SOURCE_LOG or burn.block_hash != block_hash
            or burn.block_number != block_number or burn.destination_domain != 26):
        raise ValueError("Burn identity does not match the controlled receipt.")
    if "transactionIndex" in log and quantity(log["transactionIndex"]) != quantity(receipt.get("transactionIndex")):
        raise ValueError("Burn transaction position does not match its receipt.")
    return log


def destination_base(value: str) -> str:
    """Send the shared secret only over HTTPS, or HTTP to an explicit loopback host."""
    if not value or len(value) > 2048 or any(ord(char) <= 32 or ord(char) == 127 for char in value):
        raise ValueError("Invalid destination URL.")
    parsed = urlsplit(value)
    if (parsed.scheme not in {"http", "https"} or not parsed.hostname
            or parsed.username is not None or parsed.password is not None or "?" in value or "#" in value
            or (parsed.port is not None and not 1 <= parsed.port <= 65535)):
        raise ValueError("Invalid destination URL.")
    loopback = parsed.hostname.lower() == "localhost"
    if not loopback:
        try:
            loopback = ip_address(parsed.hostname).is_loopback
        except ValueError:
            pass
    if parsed.scheme == "http" and not loopback:
        raise ValueError("Remote destinations require HTTPS.")
    return value.rstrip("/")


def main(argv: list[str]) -> int:
    if len(argv) == 2 and argv[1] in {"-h", "--help"}:
        print(__doc__)
        return 0
    if len(argv) > 2:
        print(__doc__)
        return 2
    secret = os.getenv("REBOUND_STREAM_SECRET", "")
    if not secret:
        print("Set REBOUND_STREAM_SECRET to the secret the server was started with.")
        return 2
    try:
        base = destination_base(argv[1] if len(argv) == 2 else DEFAULT_URL)
    except ValueError:
        print("Use an HTTPS destination without userinfo, query or fragment; HTTP is allowed only for loopback.")
        return 2
    rpc = os.getenv("BASE_SEPOLIA_RPC", DEFAULT_RPC)
    try:
        reply = httpx.post(rpc, timeout=30, follow_redirects=False, trust_env=False,
                          json={"jsonrpc": "2.0", "id": 1, "method": "eth_getTransactionReceipt",
                                                 "params": [SOURCE_TX]})
        reply.raise_for_status()
        envelope = reply.json()
        if not isinstance(envelope, dict) or "error" in envelope:
            raise ValueError("Invalid RPC receipt response.")
        receipt = envelope.get("result")
        log = burn_log_from_receipt(receipt, SOURCE_LOG)
        response = httpx.post(f"{base}/api/streams", timeout=30, content=json.dumps({"logs": [log]}),
                              headers={"content-type": "application/json", "x-rebound-secret": secret},
                              follow_redirects=False, trust_env=False)
        if response.status_code != 200:
            print(f"The Stream endpoint answered {response.status_code}.")
            return 1
        acknowledgment = response.json()
        if (not isinstance(acknowledgment, dict) or type(acknowledgment.get("accepted")) is not int
                or acknowledgment["accepted"] != 1):
            raise ValueError("The endpoint did not accept exactly one event.")
    except (httpx.HTTPError, httpx.InvalidURL, ValueError, KeyError, TypeError, UpstreamError) as exc:
        print(f"Could not deliver: {type(exc).__name__}.")
        return 1
    print("Delivered 1 burn as a local endpoint test (not a QuickNode delivery).")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))

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

import httpx

SOURCE_TX = "0x7e4334109c120f9da71e01116190804a9d91f2fd5ec07cfd7196a649e68e3f23"
SOURCE_LOG = 4
DEFAULT_RPC = "https://sepolia.base.org"
DEFAULT_URL = "http://127.0.0.1:8765"


def burn_log_from_receipt(receipt: dict, log_index: int) -> dict:
    """The raw log with this index from a transaction receipt."""
    for log in receipt.get("logs", []):
        if int(log["logIndex"], 16) == log_index:
            return log
    raise ValueError(f"The receipt has no log with index {log_index}.")


def main(argv: list[str]) -> int:
    if len(argv) > 2:
        print(__doc__)
        return 2
    secret = os.getenv("REBOUND_STREAM_SECRET", "")
    if not secret:
        print("Set REBOUND_STREAM_SECRET to the secret the server was started with.")
        return 2
    base = (argv[1] if len(argv) == 2 else DEFAULT_URL).rstrip("/")
    rpc = os.getenv("BASE_SEPOLIA_RPC", DEFAULT_RPC)
    try:
        reply = httpx.post(rpc, timeout=30, json={"jsonrpc": "2.0", "id": 1, "method": "eth_getTransactionReceipt",
                                                 "params": [SOURCE_TX]})
        receipt = reply.json().get("result")
        if not receipt:
            print("Base Sepolia returned no receipt for the controlled burn.")
            return 1
        log = burn_log_from_receipt(receipt, SOURCE_LOG)
        response = httpx.post(f"{base}/api/streams", timeout=30, content=json.dumps({"logs": [log]}),
                              headers={"content-type": "application/json", "x-rebound-secret": secret})
    except (httpx.HTTPError, ValueError, KeyError) as exc:
        print(f"Could not deliver: {type(exc).__name__}.")
        return 1
    if response.status_code != 200:
        print(f"The Stream endpoint answered {response.status_code}.")
        return 1
    print(f"Delivered {response.json().get('accepted', 0)} burn as a local endpoint test (not a QuickNode delivery).")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))

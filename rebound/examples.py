"""Synthetic presentation data. Never valid input for a chain transaction."""
from copy import deepcopy

EXAMPLE_WALLET = "0x05af7613449b84f2a02893d35e6cdee479404ce8"
EXAMPLE_CALLER = "0x2222222222222222222222222222222222222222"


def example_transfers() -> list[dict]:
    cases = [
        ("ready", "128.5", "128.4995", "Circle's attestation is available. This example's destination mint is unfinished."),
        ("awaiting_attestation", "42", None, "The source burn is confirmed. Circle has not returned a complete attestation yet."),
        ("caller_required", "75", "74.9995", "This transfer names a specific destination caller. The recipient cannot override that restriction."),
        ("completed", "320", "319.9995", "The destination has already consumed this message's nonce. There is nothing to mint again."),
    ]
    result = []
    for index, (status, amount, net_amount, explanation) in enumerate(cases, 1):
        result.append({
            "key": f"example-{index}", "example": True,
            "tx_hash": "0x" + f"{index:02x}" * 32, "log_index": index,
            "block_number": 35_000_000 - index * 150,
            "amount": amount, "net_amount": net_amount,
            "recipient": EXAMPLE_WALLET, "depositor": EXAMPLE_WALLET,
            "caller": EXAMPLE_CALLER if status == "caller_required" else "0x" + "00" * 20,
            "status": status, "explanation": explanation,
            "nonce": "0x" + f"{index + 10:02x}" * 32 if status != "awaiting_attestation" else None,
            "fee": "0.0005" if net_amount is not None else None,
            "source_url": None, "destination_url": None,
        })
    return deepcopy(result)


def example_stream_transfer() -> dict:
    return example_transfers()[0] | {
        "key": "example-stream-delivery",
        "discovery_source": "example_stream",
        "explanation": "A simulated Stream delivery added this burn to the demo inbox.",
    }

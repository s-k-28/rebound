"""Presentation of Stream endpoint evidence, separate from provider observation.

HMAC authentication proves acceptance by the configured endpoint. It does not prove
that a configured QuickNode Stream delivered the payload; that needs independent
provider-side evidence. Chain verification remains a separate check.
"""
import json
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

# The QuickNode Stream Rebound uses, as recorded from its dashboard. It sits inside the package so
# the Docker image carries it. It holds no credentials, and the destination is a path, not a host.
STREAM_RECORD_PATH = Path(__file__).with_name("stream_record.json")
_UUID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")

from rebound.service import MIN_RECOVERY_CONFIRMATIONS

UNVERIFIED_STATUSES = {"unavailable", "source_changed", "unsupported", "source_confirming", "awaiting_attestation"}


@dataclass(frozen=True)
class EvidenceStep:
    key: str
    title: str
    state: str
    detail: str
    url: str | None = None


def format_utc(timestamp: int | None) -> str:
    if type(timestamp) is not int or timestamp <= 0:
        return ""
    try:
        return datetime.fromtimestamp(timestamp, UTC).strftime("%Y-%m-%d %H:%M UTC")
    except (ValueError, OverflowError, OSError):
        return ""


def uses_stream_evidence(item: dict) -> bool:
    return item.get("discovery_source") in {"stream", "example_stream"}


def simulated(item: dict) -> bool:
    return (item.get("discovery_source") == "example_stream" or bool(item.get("example"))
            or str(item.get("key", "")).startswith("example-"))


def authenticated_stream_payload(item: dict) -> bool:
    """An authenticated endpoint payload, never proof of its provider origin."""
    return item.get("discovery_source") == "stream" and item.get("stream_signed") is True and not simulated(item)


def source_matched(item: dict) -> bool:
    block = item.get("block_number")
    return (item.get("source_verified") is True and type(block) is int and block >= 0
            and item.get("status") != "source_changed")


def source_confirmed(item: dict) -> bool:
    confirmations = item.get("source_confirmations")
    return (source_matched(item) and item.get("status") != "source_confirming" and type(confirmations) is int
            and confirmations >= MIN_RECOVERY_CONFIRMATIONS)


def checks_verified(item: dict) -> bool:
    checked_at = item.get("checked_at")
    return (source_confirmed(item) and isinstance(checked_at, str) and bool(checked_at.strip())
            and item.get("attestation_matched") is True and type(item.get("nonce_used")) is bool
            and item.get("status") not in UNVERIFIED_STATUSES)


def verified_arc_url(item: dict) -> str | None:
    if item.get("status") != "completed" or item.get("nonce_used") is not True or not checks_verified(item):
        return None
    if isinstance(item.get("destination_url"), str) and item["destination_url"].strip():
        return item["destination_url"]
    if item.get("submitted_receipt_verified") is True:
        url = item.get("submitted_receipt_url")
        return url if isinstance(url, str) and url.strip() else None
    return None


def evidence_steps(item: dict, status_title: str) -> list[EvidenceStep]:
    if not uses_stream_evidence(item):
        return []
    if simulated(item):
        return [
            EvidenceStep("burn", "Source burn", "simulated", "Synthetic burn; no source receipt was checked."),
            EvidenceStep("delivery", "Simulated Stream delivery", "simulated", "Synthetic endpoint delivery; not provider evidence."),
            EvidenceStep("status", "Transfer status", "simulated", f"{status_title}. Nothing was checked on chain."),
        ]
    if source_confirmed(item):
        burn = EvidenceStep("burn", "Burn confirmed", "verified",
                            f"Base Sepolia block {item['block_number']:,}. Source receipt matched.", item.get("source_url"))
    elif source_matched(item):
        confirmations = item.get("source_confirmations")
        progress = (f" {confirmations} of {MIN_RECOVERY_CONFIRMATIONS} confirmations."
                    if type(confirmations) is int and confirmations >= 0 else " Confirmation count unavailable.")
        burn = EvidenceStep("burn", "Source burn matched", "observed", "Canonical source receipt matched." + progress,
                            item.get("source_url"))
    else:
        burn = EvidenceStep("burn", "Source burn evidence", "pending", "The source receipt has not been verified.",
                            item.get("source_url"))
    received = format_utc(item.get("stream_received_at"))
    when = f"Received {received}. " if received else ""
    if authenticated_stream_payload(item):
        delivery = EvidenceStep("delivery", "Authenticated Stream payload", "verified",
                                when + "HMAC authentication verified; indexed block hash matched. "
                                "Actual QuickNode delivery still needs independent provider dashboard evidence.")
    else:
        delivery = EvidenceStep("delivery", "Shared-secret adapter payload", "observed",
                                when + "Accepted by the local endpoint adapter. No HMAC payload evidence; "
                                "this does not prove provider delivery.")
    circle = "Circle attestation matched" if item.get("attestation_matched") is True else "Circle attestation pending"
    nonce = item.get("nonce_used")
    arc = "Arc nonce used" if nonce is True else "Arc nonce unused" if nonce is False else "Arc nonce not read"
    status = EvidenceStep("status", "Transfer status verified" if checks_verified(item) else "Transfer checks incomplete",
                          "verified" if checks_verified(item) else "pending", f"{status_title}. {circle}. {arc}.")
    return [burn, delivery, status]


def walkthrough_checks(*, enabled: bool, delivered: int, last_received: int | None,
                       transfers: list[dict], local: int = 0) -> list[EvidenceStep]:
    count = delivered if type(delivered) is int and delivered > 0 else 0
    local_count = local if type(local) is int and local > 0 else 0
    found = [row for row in transfers if authenticated_stream_payload(row) and source_matched(row)]
    completed = [(row, verified_arc_url(row)) for row in found if verified_arc_url(row)]
    if enabled is not True:
        payload = EvidenceStep("authenticated", "Authenticated Stream payload", "pending",
                               "Stream ingestion is not enabled on this server.")
    elif count:
        when = format_utc(last_received)
        payload = EvidenceStep("authenticated", "Authenticated Stream payload", "verified",
                               f"{count} indexed burn{'s' if count != 1 else ''} accepted with valid HMAC authentication"
                               + (f", last {when}." if when else "."))
    elif local_count:
        payload = EvidenceStep("authenticated", "Authenticated Stream payload", "pending",
                               f"{local_count} shared-secret adapter payload{'s' if local_count != 1 else ''} accepted. "
                               "This tests the endpoint, not HMAC payload authentication or provider delivery.")
    else:
        payload = EvidenceStep("authenticated", "Authenticated Stream payload", "pending",
                               "No authenticated payload for this wallet has been observed in the endpoint index.")
    discovery = EvidenceStep("discovery", "Wallet-only discovery", "verified" if found else "pending",
                             f"{len(found)} authenticated indexed burn{'s' if len(found) != 1 else ''} matched to canonical "
                             "source receipts from the address alone. Destination checks are separate." if found else
                             "Search the Stream index with only the address; source evidence must match before this passes.")
    mint = EvidenceStep("mint", "Arc mint evidence", "verified" if completed else "pending",
                        "A discovered transfer has an independently verified Arc receipt matching the original message. "
                        "This does not imply the walkthrough sent a transaction." if completed else
                        "No discovered transfer has independently verified Arc mint evidence yet.",
                        completed[0][1] if completed else None)
    return [
        EvidenceStep("sample", "Recorded endpoint sample", "observed",
                     "A signed endpoint sample passed during documented setup. It is not a live burn-delivery check."),
        EvidenceStep("simulated", "Simulated delivery", "simulated",
                     "Synthetic demo only. Use Show simulated Stream delivery on the demo wallet."),
        payload,
        EvidenceStep("live", "Actual provider delivery", "pending",
                     "Not independently observed here. Confirm the configured Stream and matching delivery in the "
                     "QuickNode dashboard; valid HMAC alone cannot establish who sent a payload."),
        discovery,
        mint,
    ]


def load_stream_record(path: Path = STREAM_RECORD_PATH) -> dict | None:
    """The recorded Stream, or None when the file is missing or malformed.

    Rebound never queries QuickNode, so this is a snapshot taken from the dashboard on
    ``recorded_at``. Callers must present it as recorded, not as live state.
    """
    try:
        record = json.loads(path.read_text())
    except (OSError, ValueError):
        return None
    if not isinstance(record, dict) or not _UUID.fullmatch(str(record.get("id", ""))):
        return None
    if not isinstance(record.get("observed"), dict):
        return None
    return record


def stream_facts(record: dict) -> list[tuple[str, str]]:
    """Label and value rows describing the recorded Stream, for the walkthrough dialog."""
    seen = record["observed"]
    blocks_sent = seen.get("blocks_sent")
    last = seen.get("last_block_delivered")
    state = [f"{seen.get('status', 'Unknown')}."]
    if isinstance(last, int):
        state.append(f"Last block delivered {last:,}.")
    if isinstance(blocks_sent, int):
        state.append(f"{blocks_sent:,} blocks matched the filter.")
    if isinstance(seen.get("errors"), int):
        state.append(f"{seen['errors']} delivery errors.")
    burn = record.get("controlled_burn_block")
    if isinstance(burn, int) and isinstance(last, int) and last < burn:
        state.append(f"The controlled burn at block {burn:,} was not reached.")
    start = record.get("start_block")
    return [
        ("Name", str(record.get("name", ""))),
        ("Stream ID", record["id"]),
        ("Source", f"{record.get('network', '')}, {record.get('dataset', '')}"),
        ("Starts at", f"Block {start:,}, no end." if isinstance(start, int) else "Not recorded."),
        ("Filter", f"{record.get('filter', '')}. Keeps only CCTP V2 USDC burns."),
        ("Delivers to", f"POST {record.get('destination_path', '')}, {record.get('destination_auth', '')}"),
        (f"State on {record.get('recorded_at', '?')}", " ".join(state)),
    ]

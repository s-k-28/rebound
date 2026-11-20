"""Evidence for the QuickNode Streams walkthrough.

Two pure helpers, kept apart from the UI so they can be tested without Flet:

* ``evidence_steps`` turns one transfer row into the three-step flow shown in
  the inspector: burn confirmed, delivered by QuickNode, transfer status verified.
* ``walkthrough_checks`` keeps five different kinds of evidence separate, so a
  simulation or a documented setup test is never presented as a live delivery.

A step is "verified" only when the row carries the evidence for it. Anything
else is "pending". Synthetic data is always "simulated".
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

# Delivery states where the source receipt could not confirm the burn.
UNVERIFIED_STATUSES = {"unavailable", "source_changed", "unsupported"}


@dataclass(frozen=True)
class EvidenceStep:
    key: str
    title: str
    state: str  # "verified" | "pending" | "simulated" | "observed"
    detail: str
    url: str | None = None


def format_utc(timestamp: int | None) -> str:
    if not isinstance(timestamp, int) or isinstance(timestamp, bool) or timestamp <= 0:
        return ""
    return datetime.fromtimestamp(timestamp, UTC).strftime("%Y-%m-%d %H:%M UTC")


def uses_stream_evidence(item: dict) -> bool:
    return item.get("discovery_source") in {"stream", "example_stream"}


def delivered_by_quicknode(item: dict) -> bool:
    """True only for a real transfer whose delivery carried a valid QuickNode signature."""
    return item.get("discovery_source") == "stream" and bool(item.get("stream_signed"))


def evidence_steps(item: dict, status_title: str) -> list[EvidenceStep]:
    """The three-step evidence flow for a transfer found through QuickNode Streams.

    Returns an empty list for transfers found any other way, because the flow
    makes a claim about QuickNode that those transfers cannot support.
    """
    if not uses_stream_evidence(item):
        return []
    simulated = item.get("discovery_source") == "example_stream" or bool(item.get("example"))

    if simulated:
        block = item.get("block_number")
        burn = EvidenceStep("burn", "Burn confirmed", "simulated",
                            f"Synthetic burn at block {block:,}." if isinstance(block, int) else "Synthetic burn.")
        delivery = EvidenceStep("delivery", "Delivered by QuickNode", "simulated",
                                "Simulated delivery. Not a QuickNode event.")
        status = EvidenceStep("status", "Transfer status verified", "simulated",
                              f"{status_title}. Synthetic data, nothing was checked on chain.")
        return [burn, delivery, status]

    block = item.get("block_number")
    if item.get("source_verified") and isinstance(block, int):
        burn = EvidenceStep("burn", "Burn confirmed", "verified",
                            f"Base Sepolia block {block:,}. Source receipt matched.", item.get("source_url"))
    else:
        burn = EvidenceStep("burn", "Burn confirmed", "pending",
                            "The source receipt has not confirmed this burn.", item.get("source_url"))

    # A Stream row is only returned when the delivered block hash equals the log's block
    # hash. Whether QuickNode itself sent it depends on the delivery's authentication.
    received = format_utc(item.get("stream_received_at"))
    when = f"Received {received}. " if received else ""
    if item.get("stream_signed"):
        delivery = EvidenceStep("delivery", "Delivered by QuickNode", "verified",
                                f"{when}QuickNode signature verified. Block hash matched.")
    else:
        delivery = EvidenceStep("delivery", "Delivered by QuickNode", "pending",
                                f"{when}Accepted by the Stream endpoint without a QuickNode signature, "
                                "so this is not evidence that QuickNode sent it.")

    checked = bool(item.get("checked_at")) and bool(item.get("source_verified"))
    if checked and item.get("status") not in UNVERIFIED_STATUSES:
        nonce = item.get("nonce_used")
        arc = "Arc nonce not read" if nonce is None else "Arc nonce used" if nonce else "Arc nonce unused"
        circle = "Circle attestation matched" if item.get("attestation_matched") else "Circle attestation pending"
        status = EvidenceStep("status", "Transfer status verified", "verified", f"{status_title}. {circle}. {arc}.")
    else:
        status = EvidenceStep("status", "Transfer status verified", "pending",
                              f"{status_title}. Source, Circle and Arc checks have not all completed.")
    return [burn, delivery, status]


def walkthrough_checks(*, enabled: bool, delivered: int, last_received: int | None,
                       transfers: list[dict], local: int = 0) -> list[EvidenceStep]:
    """Five separate checks for the QuickNode walkthrough wallet.

    ``delivered`` counts burns whose delivery carried a valid QuickNode signature, and
    ``local`` counts shared-secret adapter deliveries; both come from the Stream index.
    ``transfers`` is the result of a wallet-only Stream search in this session, or an
    empty list when none has run.
    """
    found = [row for row in transfers if delivered_by_quicknode(row)]
    completed = [row for row in found if row.get("status") == "completed" and row.get("destination_url")]

    if not enabled:
        live = EvidenceStep("live", "Live Stream delivery", "pending", "Stream ingestion is not enabled on this server.")
    elif delivered:
        when = format_utc(last_received)
        live = EvidenceStep("live", "Live Stream delivery", "verified",
                            f"{delivered} burn{'s' if delivered != 1 else ''} delivered with a QuickNode signature"
                            + (f", last {when}." if when else "."))
    elif local:
        live = EvidenceStep("live", "Live Stream delivery", "pending",
                            f"{local} burn{'s' if local != 1 else ''} arrived through the local adapter (shared secret). "
                            "That tests the endpoint and is not a QuickNode delivery.")
    else:
        live = EvidenceStep("live", "Live Stream delivery", "pending",
                            "No burn for this wallet has arrived through the Stream yet.")

    if found:
        discovery = EvidenceStep("discovery", "Wallet-only discovery", "verified",
                                 f"{len(found)} QuickNode-delivered transfer{'s' if len(found) != 1 else ''} "
                                 "found from the address alone.")
    else:
        discovery = EvidenceStep("discovery", "Wallet-only discovery", "pending",
                                 "Run Find transfers via QuickNode with only the address.")

    if completed:
        mint = EvidenceStep("mint", "Arc mint completed", "verified", "Arc shows the mint for a discovered transfer.",
                            completed[0].get("destination_url"))
    elif found:
        mint = EvidenceStep("mint", "Arc mint completed", "pending",
                            f"Discovered transfer is not complete yet ({found[0].get('status', 'unknown')}).")
    else:
        mint = EvidenceStep("mint", "Arc mint completed", "pending", "Nothing discovered to check yet.")

    return [
        EvidenceStep("sample", "Signed sample delivery", "observed",
                     "QuickNode's Send Payload test passed during setup. See docs/QUICKNODE.md."),
        EvidenceStep("simulated", "Simulated delivery", "simulated",
                     "Synthetic demo only. Use Show simulated Stream delivery on the demo wallet."),
        live,
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

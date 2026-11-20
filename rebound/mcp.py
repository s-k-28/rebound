"""Model Context Protocol server for AI agents, over stdio, with no extra dependencies.

Run ``uv run python -m rebound.mcp``. Each stdin/stdout line is one JSON-RPC 2.0 message;
logs go to stderr. The server is dual-era: it serves MCP 2026-07-28 requests statelessly
(per-request ``_meta`` plus ``server/discover``) and answers the ``initialize`` handshake for
clients on 2025-11-25, 2025-06-18, 2025-03-26 or 2024-11-05.

Every tool is read-only. Rebound never asks for a key, never signs and never broadcasts;
``prepare_recovery`` returns an unsigned transaction for a human or the caller's own wallet to
review and sign. Tool calls run concurrently and share RecoveryService's admission queue, so a
burst of calls receives a "busy" tool error instead of unbounded upstream work.
"""

import asyncio
import json
import logging
import os
import re
import sqlite3
import sys
import threading
import time
from collections import Counter
from collections.abc import AsyncIterator, Callable

import httpx

from rebound.service import BusyError, RecoveryService, Settings, UpstreamError

log = logging.getLogger("rebound.mcp")

SERVER_NAME, SERVER_TITLE, SERVER_VERSION = "rebound", "Rebound", "0.1.0"
MODERN_VERSIONS = ("2026-07-28",)
LEGACY_VERSIONS = ("2025-11-25", "2025-06-18", "2025-03-26", "2024-11-05")
SUPPORTED_VERSIONS = MODERN_VERSIONS + LEGACY_VERSIONS
META_VERSION = "io.modelcontextprotocol/protocolVersion"
META_CAPABILITIES = "io.modelcontextprotocol/clientCapabilities"
META_SERVER = "io.modelcontextprotocol/serverInfo"

PARSE_ERROR, INVALID_REQUEST, METHOD_NOT_FOUND, INVALID_PARAMS = -32700, -32600, -32601, -32602
INTERNAL_ERROR, UNSUPPORTED_VERSION = -32603, -32022
MAX_MESSAGE_BYTES = 1_000_000
DEFAULT_CALL_TIMEOUT_SECONDS = 55.0  # Below the common 60-second client request timeout.
SHUTDOWN_GRACE_SECONDS = 10.0
LIST_TTL_MS = 300_000

ADDRESS = r"^0x[0-9a-fA-F]{40}$"
TX_HASH = r"^0x[0-9a-fA-F]{64}$"
PATTERN_HINTS = {
    ADDRESS: "a full 0x address (40 hex characters)",
    TX_HASH: "a full 0x transaction hash (64 hex characters)",
}

INSTRUCTIONS = (
    "Rebound finds and verifies interrupted Circle CCTP V2 USDC transfers from Base Sepolia to "
    "Arc Testnet using only a public wallet address. Flow: find_unfinished_transfers(wallet), "
    "follow each transfer's next_step, poll with inspect_transfer, and call prepare_recovery only "
    "for a ready transfer. All tools are read-only: Rebound never asks for a private key, never "
    "signs and never broadcasts. Never burn USDC again to retry; finish the existing burn. The "
    "recipient is fixed and cannot be changed. completed means already minted, so do not mint "
    "again. source_confirming and awaiting_attestation mean wait and poll later. A human or the "
    "caller's own wallet must review and sign any prepared transaction. Testnet only."
)
SAFETY = (
    " Read-only: Rebound never asks for a key, never signs and never broadcasts. Never burn USDC "
    "again to 'retry'; finish the existing burn. The recipient is fixed in the signed message and "
    "cannot be changed. status completed means already minted: do not prepare or mint again. "
    "source_confirming or awaiting_attestation means wait and call inspect_transfer later."
)

NEXT_STEPS = {
    "ready": ("prepare_recovery", "Call prepare_recovery with this tx_hash, log_index and the Arc "
              "address that will submit. A human or that address's own wallet must review and sign."),
    "caller_required": ("prepare_recovery_as_designated_caller", "Only the destination caller fixed "
                        "in the message ({caller}) can submit. Call prepare_recovery with that caller; "
                        "Rebound refuses any other address."),
    "completed": ("none_already_minted", "Arc already consumed this nonce, so the USDC was minted. "
                  "Do not prepare, sign, mint or burn again."),
    "source_confirming": ("wait_then_inspect", "The burn needs 12 Base Sepolia confirmations. Call "
                          "inspect_transfer again in about 30 seconds. Do not burn again."),
    "awaiting_attestation": ("wait_then_inspect", "Circle has not signed the message yet. Call "
                             "inspect_transfer again in 1-2 minutes. Do not burn again."),
    "expired": ("request_reattestation", "The attestation expired. Request Circle re-attestation for "
                "this existing burn; Rebound cannot prepare it yet. Do not burn again."),
    "managed_delivery": ("check_original_app", "Circle forwarding was requested. Check delivery in "
                         "the app that started the transfer; do not mint manually."),
    "source_changed": ("investigate", "The source receipt no longer matches (possible reorg). Call "
                       "inspect_transfer later and send nothing until it is consistent."),
    "unsupported": ("stop", "Outside Rebound's coverage, so no recovery can be prepared. Do not burn "
                    "again to work around it."),
    "unavailable": ("retry_later", "A provider could not be reached. Retry later; this is not "
                    "evidence that funds are lost."),
}
UNKNOWN_STEP = ("inspect", "Unrecognized status. Call inspect_transfer for fresh evidence and send "
                "nothing meanwhile.")
ACTIONS = sorted({action for action, _ in NEXT_STEPS.values()} | {UNKNOWN_STEP[0]})


def _text(description: str) -> dict:
    return {"type": "string", "description": description}


def _nullable(kind: str, description: str = "") -> dict:
    # Single-type anyOf branches port to more clients than a ["string", "null"] type array.
    schema = {"anyOf": [{"type": kind}, {"type": "null"}]}
    return schema | {"description": description} if description else schema


WALLET_FIELD = {"type": "string", "pattern": ADDRESS,
                "description": "Public 0x address that sent (depositor) or receives (mint recipient) the USDC."}
HASH_FIELD = {"type": "string", "pattern": TX_HASH,
              "description": "Original Base Sepolia burn transaction hash."}
LOG_INDEX_FIELD = {"type": "integer", "minimum": 0, "maximum": 100_000,
                   "description": "DepositForBurn event index within the receipt, as shown by the scan."}
TRANSFER_SCHEMA = {
    "type": "object",
    "properties": {
        "tx_hash": _text("Base Sepolia burn transaction hash."),
        "log_index": {"type": "integer"},
        "block_number": {"type": "integer"},
        "status": _text("ready, caller_required, completed, source_confirming, awaiting_attestation, "
                        "expired, managed_delivery, source_changed, unsupported or unavailable."),
        "explanation": {"type": "string"},
        "amount": _text("Original burned amount, in USDC."),
        "net_amount": _nullable("string", "USDC the recipient receives after Circle's fee."),
        "fee": _nullable("string", "Circle protocol fee, in USDC."),
        "recipient": _text("Fixed mint recipient on Arc. It cannot be changed."),
        "depositor": {"type": "string"},
        "caller": _text("Destination caller fixed in the burn; the zero address means anyone."),
        "nonce": _nullable("string"),
        "source_confirmations": _nullable("integer"),
        "nonce_used": _nullable("boolean"),
        "source_url": {"type": "string"},
        "destination_url": _nullable("string"),
        "next_step": {
            "type": "object",
            "properties": {"action": {"type": "string", "enum": ACTIONS}, "detail": {"type": "string"}},
            "required": ["action", "detail"],
        },
    },
    "required": ["tx_hash", "log_index", "status", "explanation", "amount", "recipient", "next_step"],
}
TRANSFERS_FIELD = {"type": "array", "items": TRANSFER_SCHEMA}
COUNTS_FIELD = {"type": "object", "additionalProperties": {"type": "integer"},
                "description": "Number of transfers per status."}
ANNOTATIONS = {"readOnlyHint": True, "destructiveHint": False, "idempotentHint": True,
               "openWorldHint": True}

TOOL_SPECS = {
    "find_unfinished_transfers": {
        "title": "Find unfinished USDC transfers",
        "description": (
            "Find Circle CCTP V2 USDC transfers from Base Sepolia to Arc Testnet for a public wallet "
            "address (as depositor or recipient) without the burn hash. Use it when an agent or app "
            "lost track of a transfer mid-flight. Scans the last `blocks` Base Sepolia blocks "
            "(default 2,000, max 50,000) ending at `end_block` or the current head, plus previously "
            "indexed candidates, and verifies each burn against its receipt, Circle's attestation and "
            "Arc's nonce. Every transfer, including completed ones, carries a status and next_step. "
            "An empty result covers only the reported block range: widen `blocks` or move "
            "`end_block` before concluding nothing is pending." + SAFETY
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "wallet": WALLET_FIELD,
                "blocks": {"type": "integer", "minimum": 1, "maximum": 50_000, "default": 2000,
                           "description": "Base Sepolia blocks to scan, ending at end_block (about 2 s each)."},
                "end_block": {"type": "integer", "minimum": 0,
                              "description": "Last Base Sepolia block to scan. Omit for the current head. "
                                             "A fixed window excludes indexed candidates outside it."},
            },
            "required": ["wallet"],
            "additionalProperties": False,
        },
        "outputSchema": {
            "type": "object",
            "properties": {
                "wallet": {"type": "string"}, "from_block": {"type": "integer"},
                "to_block": {"type": "integer"}, "coverage": {"type": "string"},
                "partial": {"type": "boolean", "description": "True when more than 100 candidates "
                            "matched and only the first 100 were verified."},
                "counts": COUNTS_FIELD, "transfers": TRANSFERS_FIELD,
            },
            "required": ["wallet", "from_block", "to_block", "coverage", "partial", "counts", "transfers"],
        },
    },
    "inspect_transfer": {
        "title": "Inspect one transfer",
        "description": (
            "Re-check one Base Sepolia burn transaction by hash: verifies the canonical receipt, "
            "matches Circle's signed message and reads Arc's nonce state. Pass log_index to select one "
            "DepositForBurn event; omit it to check every supported burn in the receipt. Use it to poll "
            "a waiting transfer or to verify a hash someone gave you." + SAFETY
        ),
        "inputSchema": {
            "type": "object",
            "properties": {"tx_hash": HASH_FIELD, "log_index": LOG_INDEX_FIELD},
            "required": ["tx_hash"],
            "additionalProperties": False,
        },
        "outputSchema": {
            "type": "object",
            "properties": {"tx_hash": {"type": "string"}, "coverage": {"type": "string"},
                           "partial": {"type": "boolean"}, "counts": COUNTS_FIELD,
                           "transfers": TRANSFERS_FIELD},
            "required": ["tx_hash", "coverage", "partial", "counts", "transfers"],
        },
    },
    "prepare_recovery": {
        "title": "Prepare unsigned recovery",
        "description": (
            "Prepare the UNSIGNED Arc Testnet receiveMessage transaction that finishes an existing "
            "burn whose status is ready (or caller_required, when `caller` is the designated "
            "destination caller). Rebound re-verifies the source receipt, attestation and nonce and "
            "simulates the mint first; it refuses completed, waiting, expired, forwarded and hooked "
            "transfers. Nothing is signed or sent: a human or the caller's own wallet must review the "
            "recipient, amounts, fee and gas, then sign and submit from `caller`. Call it again right "
            "before signing; if it then reports completed, do not submit." + SAFETY
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "tx_hash": HASH_FIELD,
                "log_index": LOG_INDEX_FIELD,
                "caller": {"type": "string", "pattern": ADDRESS,
                           "description": "Arc Testnet address that will sign and submit receiveMessage. "
                                          "It pays Arc gas in testnet USDC; the minted USDC still goes "
                                          "only to the fixed recipient."},
            },
            "required": ["tx_hash", "log_index", "caller"],
            "additionalProperties": False,
        },
        "outputSchema": {
            "type": "object",
            "properties": {
                "transaction": {
                    "type": "object",
                    "description": "Unsigned Arc Testnet transaction. Review before any wallet signs it.",
                    "properties": {"from": {"type": "string"}, "to": {"type": "string"},
                                   "data": {"type": "string"}, "value": {"type": "string"},
                                   "chainId": {"type": "integer"}, "gas": {"type": "string"}},
                    "required": ["from", "to", "data", "value", "chainId", "gas"],
                },
                "recipient": _text("Fixed mint recipient; cannot be changed."),
                "amount": {"type": "string"}, "net_amount": {"type": "string"},
                "fee": {"type": "string"}, "tx_hash": {"type": "string"},
                "log_index": {"type": "integer"}, "nonce": {"type": "string"},
                "gas_quote": {"type": "object", "properties": {
                    "max_cost_usdc": {"type": "string"}, "caller_balance_usdc": {"type": "string"},
                    "shortfall_usdc": {"type": "string"}}},
                "warning": {"type": "string"},
                "signed": {"type": "boolean", "description": "Always false."},
                "broadcast": {"type": "boolean", "description": "Always false."},
                "review": {"type": "string"},
            },
            "required": ["transaction", "recipient", "amount", "net_amount", "fee", "tx_hash",
                         "log_index", "nonce", "warning", "signed", "broadcast", "review"],
        },
    },
    "check_readiness": {
        "title": "Check provider readiness",
        "description": (
            "Check that Rebound's providers are reachable and correctly configured: Base Sepolia RPC "
            "(chain and TokenMessengerV2), Circle's Base-to-Arc fee route and Arc Testnet RPC (chain "
            "and MessageTransmitterV2). Cached for up to 30 seconds. Use it before a batch of checks "
            "or after a tool reports a provider as unavailable. Read-only: Rebound never asks for a "
            "key, never signs and never broadcasts."
        ),
        "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False},
        "outputSchema": {
            "type": "object",
            "properties": {
                "status": _text("ready or degraded."),
                "providers": {"type": "object", "additionalProperties": {"type": "string"}},
                "checked_at": {"type": "string"},
                "stream_ingestion_configured": {"type": "boolean"},
            },
            "required": ["status", "providers", "checked_at"],
        },
    },
}


class RpcError(Exception):
    def __init__(self, code: int, message: str, data=None):
        super().__init__(message)
        self.code, self.message, self.data = code, message, data


def redact(value) -> str:
    """Strip URLs, which can embed RPC credentials, from any text an agent or log may see."""
    return re.sub(r"\b[a-z][a-z0-9+.-]*://\S+", "[redacted URL]", str(value), flags=re.IGNORECASE)


def _is_type(value, kind: str) -> bool:
    if kind == "integer":
        return isinstance(value, int) and not isinstance(value, bool)
    if kind == "number":
        return isinstance(value, int | float) and not isinstance(value, bool)
    return isinstance(value, {"object": dict, "array": list, "string": str, "boolean": bool,
                              "null": type(None)}[kind])


def schema_errors(schema: dict, value, path: str = "") -> list[str]:
    """Validate the JSON Schema subset these tools declare; used for inputs and in tests."""
    label = path or "arguments"
    kinds = schema.get("type")
    if kinds is not None:
        kinds = kinds if isinstance(kinds, list) else [kinds]
        if not any(_is_type(value, kind) for kind in kinds):
            return [f"{label} must be {' or '.join(kinds)}"]
    if "anyOf" in schema and all(schema_errors(branch, value, path) for branch in schema["anyOf"]):
        kinds = [branch.get("type", "a valid value") for branch in schema["anyOf"]]
        return [f"{label} must be {' or '.join(kinds)}"]
    if "enum" in schema and value not in schema["enum"]:
        return [f"{label} must be one of {', '.join(map(str, schema['enum']))}"]
    if isinstance(value, str) and "pattern" in schema:
        if not re.search(re.sub(r"\$$", r"\\Z", schema["pattern"]), value):
            return [f"{label} must be {PATTERN_HINTS.get(schema['pattern'], schema['pattern'])}"]
    if _is_type(value, "integer"):
        if "minimum" in schema and value < schema["minimum"]:
            return [f"{label} must be at least {schema['minimum']:,}"]
        if "maximum" in schema and value > schema["maximum"]:
            return [f"{label} must be at most {schema['maximum']:,}"]
    errors = []
    if isinstance(value, dict):
        properties = schema.get("properties", {})
        extra = schema.get("additionalProperties", True)
        errors += [f"{path + '.' if path else ''}{name} is required"
                   for name in schema.get("required", []) if name not in value]
        for name, item in value.items():
            child = f"{path}.{name}" if path else name
            if name in properties:
                errors += schema_errors(properties[name], item, child)
            elif extra is False:
                errors.append(f"{child} is not a recognized argument")
            elif isinstance(extra, dict):
                errors += schema_errors(extra, item, child)
    if isinstance(value, list) and "items" in schema:
        for index, item in enumerate(value):
            errors += schema_errors(schema["items"], item, f"{label}[{index}]")
    return errors


def next_step(row: dict) -> dict:
    action, detail = NEXT_STEPS.get(row.get("status"), UNKNOWN_STEP)
    return {"action": action, "detail": detail.replace("{caller}", str(row.get("caller")))}


def _transfers(rows: list[dict]) -> tuple[list[dict], dict]:
    transfers = [{**row, "next_step": next_step(row)} for row in rows]
    return transfers, dict(Counter(str(row.get("status")) for row in transfers))


def _transfer_lines(transfers: list[dict], limit: int = 20) -> list[str]:
    lines = [f"- {row['tx_hash']} log {row['log_index']}: {row['amount']} USDC to {row['recipient']}, "
             f"status {row['status']}. Next: {row['next_step']['detail']}" for row in transfers[:limit]]
    if len(transfers) > limit:
        lines.append(f"- {len(transfers) - limit} more in the structured result.")
    return lines


def _counts_text(counts: dict) -> str:
    return ", ".join(f"{count} {status}" for status, count in sorted(counts.items()))


async def find_unfinished_transfers(service, args: dict) -> tuple[str, dict]:
    data = await service.scan(args["wallet"], blocks=args.get("blocks", 2000),
                              end_block=args.get("end_block"))
    transfers, counts = _transfers(data["transfers"])
    lines = [f"Wallet {data['wallet']}: {data['coverage']}"]
    if data.get("partial"):
        lines.append("Partial: more than 100 candidates matched and only 100 were verified.")
    if transfers:
        lines.append(f"{len(transfers)} transfer(s): {_counts_text(counts)}.")
        lines += _transfer_lines(transfers)
    else:
        lines.append("No supported transfers in that range. This is not proof that nothing is pending "
                     "elsewhere: raise blocks (max 50,000) or set end_block to search earlier history.")
    return "\n".join(lines), {**data, "counts": counts, "transfers": transfers}


async def inspect_transfer(service, args: dict) -> tuple[str, dict]:
    data = await service.inspect_transaction(args["tx_hash"], log_index=args.get("log_index"))
    transfers, counts = _transfers(data["transfers"])
    if transfers:
        lines = [f"Receipt {data['tx_hash']}: {len(transfers)} supported burn(s): {_counts_text(counts)}."]
        lines += _transfer_lines(transfers)
    else:
        where = f" at log_index {args['log_index']}" if "log_index" in args else ""
        lines = [f"Receipt {data['tx_hash']} has no supported Base Sepolia to Arc USDC burn{where}. "
                 "Check the hash and log index; there is nothing to recover here."]
    return "\n".join(lines), {**data, "counts": counts, "transfers": transfers}


async def prepare_recovery(service, args: dict) -> tuple[str, dict]:
    data = await service.prepare(args["tx_hash"], args["log_index"], args["caller"])
    tx, quote = data["transaction"], data.get("gas_quote") or {}
    review = ("UNSIGNED. Rebound did not sign or broadcast anything. A human or the caller's own "
              "wallet must review the recipient, amounts, fee and gas, then sign and submit from "
              f"{tx['from']} on Arc Testnet (chain {tx['chainId']}). Call prepare_recovery again right "
              "before signing; if it reports completed, do not submit. Never burn USDC again to retry.")
    lines = [
        "Unsigned recovery transaction prepared. Nothing was signed or sent.",
        f"Finishes burn {data['tx_hash']} log {data['log_index']} (nonce {data['nonce']}).",
        f"Mints to the fixed recipient {data['recipient']}: {data['net_amount']} USDC after a "
        f"{data['fee']} USDC protocol fee (original amount {data['amount']} USDC).",
        f"Submit from {tx['from']} to MessageTransmitterV2 {tx['to']} on Arc Testnet "
        f"(chain {tx['chainId']}), gas limit {int(tx['gas'], 16):,}, maximum gas cost "
        f"{quote.get('max_cost_usdc', 'unknown')} testnet USDC, caller balance "
        f"{quote.get('caller_balance_usdc', 'unknown')}.",
    ]
    if quote.get("shortfall_usdc") not in (None, "0"):
        lines.append(f"The caller needs {quote['shortfall_usdc']} more testnet USDC on Arc for gas "
                     "before it can submit.")
    lines += [review, f"Warning: {data['warning']}"]
    return "\n".join(lines), {**data, "signed": False, "broadcast": False, "review": review}


async def check_readiness(service, args: dict) -> tuple[str, dict]:
    data = await service.readiness()
    providers = ", ".join(f"{name} {state}" for name, state in data["providers"].items())
    if data["status"] == "ready":
        summary = f"Rebound is ready ({providers}); checked {data['checked_at']}."
    else:
        summary = (f"Rebound is degraded ({providers}); checked {data['checked_at']}. Tools that need "
                   "an unavailable provider will fail until it recovers; retry in a few seconds.")
    return summary, dict(data)


HANDLERS = {
    "find_unfinished_transfers": find_unfinished_transfers,
    "inspect_transfer": inspect_transfer,
    "prepare_recovery": prepare_recovery,
    "check_readiness": check_readiness,
}


def server_info(version: str) -> dict:
    info = {"name": SERVER_NAME, "version": SERVER_VERSION}
    return info | {"title": SERVER_TITLE} if version >= "2025-06-18" else info


def tool_definitions(version: str) -> list[dict]:
    """Tool list in a fixed order, limited to fields the negotiated protocol version defines."""
    tools = []
    for name, spec in TOOL_SPECS.items():
        tool = {"name": name, "description": spec["description"], "inputSchema": spec["inputSchema"]}
        if version >= "2025-03-26":
            tool["annotations"] = {"title": spec["title"], **ANNOTATIONS}
        if version >= "2025-06-18":
            tool |= {"title": spec["title"], "outputSchema": spec["outputSchema"]}
        tools.append(tool)
    return tools


def tool_error(message: str) -> dict:
    return {"content": [{"type": "text", "text": message}], "isError": True}


def _valid_id(value) -> bool:
    return isinstance(value, str) or (isinstance(value, int) and not isinstance(value, bool))


class McpServer:
    """Transport-independent MCP dispatcher; ``serve`` reads lines and writes encoded replies."""

    def __init__(self, service, *, call_timeout: float = DEFAULT_CALL_TIMEOUT_SECONDS):
        self.service = service
        self.call_timeout = call_timeout
        self.legacy_version: str | None = None
        self._write: Callable[[bytes], None] | None = None
        self._inflight: dict[str | int, asyncio.Task] = {}

    async def serve(self, lines: AsyncIterator[bytes], write: Callable[[bytes], None]) -> None:
        self._write = write
        try:
            async for line in lines:
                await self.receive(line)
        finally:
            await self._drain()

    async def _drain(self) -> None:
        """At end of input, let in-flight calls answer briefly, then cancel them."""
        tasks = list(self._inflight.values())
        if not tasks:
            return
        _, pending = await asyncio.wait(tasks, timeout=SHUTDOWN_GRACE_SECONDS)
        for task in pending:
            task.cancel()
        await asyncio.gather(*pending, return_exceptions=True)

    def _send(self, message: dict) -> None:
        # json.dumps escapes control characters, so a message never contains a raw newline.
        data = json.dumps(message, separators=(",", ":")).encode() + b"\n"
        try:
            self._write(data)
        except (BrokenPipeError, OSError, ValueError):
            log.warning("The client closed stdout; dropping a reply.")

    def _error(self, request_id, code: int, message: str, data=None) -> None:
        error = {"code": code, "message": message} | ({"data": data} if data is not None else {})
        self._send({"jsonrpc": "2.0", "id": request_id, "error": error})

    async def receive(self, line: bytes) -> None:
        if len(line.rstrip(b"\r\n")) > MAX_MESSAGE_BYTES:
            self._error(None, PARSE_ERROR, f"Message exceeds {MAX_MESSAGE_BYTES:,} bytes.")
            return
        if not line.strip():
            return
        try:
            message = json.loads(line)
        except (ValueError, RecursionError):
            self._error(None, PARSE_ERROR, "Parse error: each line must be one JSON-RPC message.")
            return
        if not isinstance(message, dict):
            self._error(None, INVALID_REQUEST, "Expected one JSON-RPC object; batches are not supported.")
            return
        method, request_id = message.get("method"), message.get("id")
        if "method" not in message and ("result" in message or "error" in message):
            return  # This server never sends requests, so a client response has nothing to match.
        if (message.get("jsonrpc") != "2.0" or not isinstance(method, str)
                or ("id" in message and not _valid_id(request_id))):
            self._error(request_id if _valid_id(request_id) else None, INVALID_REQUEST,
                        "Invalid JSON-RPC 2.0 request.")
            return
        params = message.get("params", {})
        if "id" not in message:
            self._notification(method, params)
            return
        try:
            result = await self._dispatch(request_id, method, params)
        except RpcError as exc:
            self._error(request_id, exc.code, exc.message, exc.data)
            return
        if result is not None:
            self._send({"jsonrpc": "2.0", "id": request_id, "result": result})

    def _notification(self, method: str, params) -> None:
        # notifications/initialized and unknown notifications need no reply.
        if method == "notifications/cancelled" and isinstance(params, dict):
            request_id = params.get("requestId")
            task = self._inflight.get(request_id) if _valid_id(request_id) else None
            if task is not None:
                task.cancel()  # A cancelled request receives no response.

    def _version(self, method: str, params: dict) -> str:
        meta = params.get("_meta")
        if isinstance(meta, dict) and META_VERSION in meta:
            version = meta[META_VERSION]
            if not isinstance(version, str) or not isinstance(meta.get(META_CAPABILITIES), dict):
                raise RpcError(INVALID_PARAMS, f"params._meta needs a string {META_VERSION} and an "
                                               f"object {META_CAPABILITIES}.")
            if version not in SUPPORTED_VERSIONS:
                raise RpcError(UNSUPPORTED_VERSION, "Unsupported protocol version",
                               {"supported": list(SUPPORTED_VERSIONS), "requested": version})
            return version
        if self.legacy_version:
            return self.legacy_version
        if method == "ping":
            return LEGACY_VERSIONS[0]
        raise RpcError(INVALID_PARAMS, f"Send initialize first (MCP 2025-11-25 and earlier) or include "
                                       f"{META_VERSION} and {META_CAPABILITIES} in params._meta.",
                       {"supported": list(SUPPORTED_VERSIONS)})

    async def _dispatch(self, request_id, method: str, params) -> dict | None:
        if method not in ("initialize", "ping", "server/discover", "tools/list", "tools/call"):
            raise RpcError(METHOD_NOT_FOUND, f"Method not found: {method}")
        if not isinstance(params, dict):
            raise RpcError(INVALID_PARAMS, "params must be an object.")
        if method == "initialize":
            return self._initialize(params)
        version = self._version(method, params)
        if method == "tools/call":
            self._start_call(request_id, params, version)
            return None
        if method == "tools/list":
            return self._finish({"tools": tool_definitions(version)}, version, cacheable=True)
        if method == "server/discover":
            result = {"supportedVersions": list(SUPPORTED_VERSIONS), "capabilities": {"tools": {}},
                      "instructions": INSTRUCTIONS}
            return self._finish(result, version, cacheable=True)
        return self._finish({}, version)

    def _initialize(self, params: dict) -> dict:
        requested = params.get("protocolVersion")
        if not isinstance(requested, str):
            raise RpcError(INVALID_PARAMS, "initialize requires a string params.protocolVersion.")
        # Echo a supported legacy version; otherwise offer the latest one this server speaks.
        self.legacy_version = requested if requested in LEGACY_VERSIONS else LEGACY_VERSIONS[0]
        log.info("initialize: client requested %s, using %s", redact(requested)[:40], self.legacy_version)
        return {"protocolVersion": self.legacy_version, "capabilities": {"tools": {}},
                "serverInfo": server_info(self.legacy_version), "instructions": INSTRUCTIONS}

    def _finish(self, result: dict, version: str, *, cacheable: bool = False) -> dict:
        if version not in MODERN_VERSIONS:
            return result
        result = {"resultType": "complete", **result,
                  "_meta": {META_SERVER: server_info(version)}}
        return result | {"ttlMs": LIST_TTL_MS, "cacheScope": "public"} if cacheable else result

    def _start_call(self, request_id, params: dict, version: str) -> None:
        name, arguments = params.get("name"), params.get("arguments")
        if not isinstance(name, str):
            raise RpcError(INVALID_PARAMS, "tools/call requires a string params.name.")
        if name not in TOOL_SPECS:
            raise RpcError(INVALID_PARAMS, f"Unknown tool: {redact(name)[:80]}")
        arguments = {} if arguments is None else arguments
        if not isinstance(arguments, dict):
            raise RpcError(INVALID_PARAMS, "tools/call params.arguments must be an object.")
        task = asyncio.create_task(self._run_call(request_id, name, arguments, version))
        self._inflight[request_id] = task

        def forget(done: asyncio.Task) -> None:
            if self._inflight.get(request_id) is done:
                del self._inflight[request_id]

        task.add_done_callback(forget)

    async def _run_call(self, request_id, name: str, arguments: dict, version: str) -> None:
        try:
            result = self._finish(await self.call_tool(name, arguments, version), version)
            self._send({"jsonrpc": "2.0", "id": request_id, "result": result})
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log.error("tools/call %s failed while encoding: %s", name, type(exc).__name__)
            self._error(request_id, INTERNAL_ERROR, "Internal error while encoding the tool result.")

    async def call_tool(self, name: str, arguments: dict, version: str = MODERN_VERSIONS[0]) -> dict:
        problems = schema_errors(TOOL_SPECS[name]["inputSchema"], arguments)
        if problems:
            return tool_error(f"Invalid arguments: {'; '.join(problems)}. Nothing was checked or prepared.")
        started = time.monotonic()
        try:
            async with asyncio.timeout(self.call_timeout):
                summary, data = await HANDLERS[name](self.service, arguments)
        except BusyError as exc:
            return tool_error(f"Busy: {redact(exc)} Retry in about 5 seconds.")
        except UpstreamError as exc:
            return tool_error(f"Provider unavailable: {redact(exc)} This does not mean funds are lost; "
                              "retry later or run check_readiness.")
        except TimeoutError:
            return tool_error(f"Timed out after {self.call_timeout:g} seconds. Retry later or scan fewer "
                              "blocks. Nothing was prepared or sent.")
        except ValueError as exc:
            return tool_error(f"Rejected: {redact(exc)} Nothing was prepared or sent.")
        except Exception as exc:
            log.error("tools/call %s raised %s", name, type(exc).__name__)
            return tool_error(f"Internal error ({type(exc).__name__}). Nothing was prepared or sent.")
        log.info("tools/call %s finished in %.1f s", name, time.monotonic() - started)
        content = [{"type": "text", "text": summary},
                   {"type": "text", "text": json.dumps(data, separators=(",", ":"))}]
        result = {"content": content, "isError": False}
        return result | {"structuredContent": data} if version >= "2025-06-18" else result


async def stdin_lines(stream=None) -> AsyncIterator[bytes]:
    """Yield stdin lines from a daemon thread, which works for pipes, files and terminals alike."""
    stream = stream or sys.stdin.buffer
    loop = asyncio.get_running_loop()
    queue: asyncio.Queue[bytes | None] = asyncio.Queue()

    def hand_off(item: bytes | None) -> None:
        try:
            loop.call_soon_threadsafe(queue.put_nowait, item)
        except RuntimeError:
            pass  # The event loop already closed.

    def pump() -> None:
        try:
            while line := stream.readline(MAX_MESSAGE_BYTES + 1):
                if len(line) > MAX_MESSAGE_BYTES and not line.endswith(b"\n"):
                    while (rest := stream.readline(65_536)) and not rest.endswith(b"\n"):
                        pass  # Discard the remainder; receive() reports the oversized message.
                hand_off(line)
        except (OSError, ValueError):
            pass
        finally:
            hand_off(None)

    threading.Thread(target=pump, name="rebound-mcp-stdin", daemon=True).start()
    while (line := await queue.get()) is not None:
        yield line


def _claim_stdout():
    """Reserve the real stdout for protocol messages; stray prints and C output go to stderr."""
    sys.stdout.flush()
    out = os.fdopen(os.dup(sys.stdout.fileno()), "wb")
    os.dup2(sys.stderr.fileno(), sys.stdout.fileno())
    sys.stdout = sys.stderr
    return out


async def run_stdio(call_timeout: float) -> None:
    out = _claim_stdout()

    def write(data: bytes) -> None:
        out.write(data)
        out.flush()

    async with httpx.AsyncClient(timeout=20) as client:
        server = McpServer(RecoveryService(Settings.from_env(), client), call_timeout=call_timeout)
        log.info("ready on stdio; MCP %s, legacy %s", ", ".join(MODERN_VERSIONS), ", ".join(LEGACY_VERSIONS))
        await server.serve(stdin_lines(), write)
    log.info("stdin closed; exiting")


def main() -> int:
    logging.basicConfig(stream=sys.stderr, level=logging.WARNING, format="rebound.mcp %(levelname)s %(message)s")
    log.setLevel(logging.INFO)  # httpx stays at WARNING because its request logs include RPC URLs.
    try:
        timeout = float(os.getenv("REBOUND_MCP_TIMEOUT_SECONDS", DEFAULT_CALL_TIMEOUT_SECONDS))
        if not 0 < timeout <= 600:
            raise ValueError("REBOUND_MCP_TIMEOUT_SECONDS must be between 0 and 600.")
        asyncio.run(run_stdio(timeout))
    except KeyboardInterrupt:
        return 130
    except (ValueError, OSError, sqlite3.Error) as exc:
        log.error("could not start: %s", redact(exc))
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

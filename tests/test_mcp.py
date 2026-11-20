"""MCP stdio server: lifecycle, version negotiation, tool schemas and error mapping, offline."""

import asyncio
import io
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from rebound import mcp
from rebound.clients import UpstreamError
from rebound.service import BusyError
from tests.test_protocol import OTHER, WALLET

pytest_plugins = ["tests.test_service"]

HASH = "0x" + "cd" * 32
MODERN = {mcp.META_VERSION: "2026-07-28", mcp.META_CAPABILITIES: {}}
ROW = {
    "key": f"{HASH}:3", "tx_hash": HASH, "log_index": 3, "block_number": 100, "amount": "128.5",
    "net_amount": "128.4995", "recipient": WALLET, "depositor": WALLET, "caller": "0x" + "00" * 20,
    "nonce": "0x" + "ab" * 32, "fee": "0.0005", "example": False,
    "source_url": f"https://sepolia.basescan.org/tx/{HASH}", "destination_url": None,
    "source_verified": True, "attestation_matched": True, "source_confirmations": 101,
    "destination_block": 900, "nonce_used": False, "expiration_block": 1000, "delivery_mode": None,
    "forward_state": None, "status": "ready", "explanation": "Review it before preparing recovery.",
    "checked_at": "2026-11-20T07:00:00+00:00", "discovery_source": "rpc",
}
SCAN = {"wallet": WALLET, "from_block": 51, "to_block": 200, "partial": False,
        "coverage": "Base Sepolia blocks 51–200, plus previously indexed candidates.",
        "transfers": [ROW, ROW | {"log_index": 4, "status": "completed", "nonce_used": True}]}
PREPARED = {
    "transaction": {"from": OTHER, "to": "0xe737e5cebeeba77efe34d4aa090756590b1ce275",
                    "data": "0x57ecfd28", "value": "0x0", "chainId": 5042002, "gas": "0x3a980"},
    "recipient": WALLET, "amount": "128.5", "net_amount": "128.4995", "fee": "0.0005",
    "tx_hash": HASH, "log_index": 3, "nonce": "0x" + "ab" * 32,
    "gas_quote": {"max_cost_usdc": "0.00024", "caller_balance_usdc": "1", "shortfall_usdc": "0"},
    "warning": "Arc Testnet only.",
}
READY = {"status": "ready", "checked_at": "2026-11-20T07:00:00+00:00",
         "providers": {"base_sepolia": "ok", "circle_cctp_fee_route": "ok", "arc_testnet": "ok"},
         "stream_ingestion_configured": False}


class FakeService:
    def __init__(self, error: BaseException | None = None, delay: float = 0):
        self.error, self.delay, self.calls = error, delay, []

    async def _answer(self, name, result, *args, **kwargs):
        self.calls.append((name, args, kwargs))
        await asyncio.sleep(self.delay)
        if self.error is not None:
            raise self.error
        return result

    async def scan(self, wallet, blocks=2000, *, end_block=None):
        return await self._answer("scan", SCAN, wallet, blocks=blocks, end_block=end_block)

    async def inspect_transaction(self, tx_hash, *, log_index=None):
        result = {"tx_hash": tx_hash, "coverage": "One Base Sepolia receipt.", "partial": False,
                  "transfers": [ROW]}
        return await self._answer("inspect", result, tx_hash, log_index=log_index)

    async def prepare(self, tx_hash, log_index, caller):
        return await self._answer("prepare", PREPARED, tx_hash, log_index, caller)

    async def readiness(self):
        return await self._answer("readiness", READY)


def rpc(request_id, method, params=None, *, meta=True):
    params = dict(params or {})
    if meta:
        params["_meta"] = MODERN
    return {"jsonrpc": "2.0", "id": request_id, "method": method, "params": params}


def call(request_id, name, arguments=None):
    return rpc(request_id, "tools/call", {"name": name, "arguments": arguments or {}})


async def exchange(server, *messages) -> list[dict]:
    written = []

    async def lines():
        for message in messages:
            yield message if isinstance(message, bytes) else (json.dumps(message) + "\n").encode()

    await server.serve(lines(), written.append)
    for chunk in written:
        assert chunk.endswith(b"\n") and chunk.count(b"\n") == 1
    return [json.loads(chunk) for chunk in written]


async def call_result(server, name, arguments=None) -> dict:
    [response] = await exchange(server, call(1, name, arguments))
    return response["result"]


async def test_legacy_initialize_handshake_then_tools():
    server = mcp.McpServer(FakeService())
    init = {"protocolVersion": "2025-11-25", "capabilities": {}, "clientInfo": {"name": "t", "version": "1"}}
    responses = await exchange(
        server,
        rpc(1, "initialize", init, meta=False),
        {"jsonrpc": "2.0", "method": "notifications/initialized"},
        rpc(2, "ping", meta=False),
        rpc(3, "tools/list", meta=False),
    )
    assert [response["id"] for response in responses] == [1, 2, 3]
    result = responses[0]["result"]
    assert result["protocolVersion"] == "2025-11-25"
    assert result["capabilities"] == {"tools": {}}
    assert result["serverInfo"]["name"] == "rebound"
    assert "never signs" in result["instructions"]
    assert "resultType" not in result
    assert responses[1]["result"] == {}
    names = [tool["name"] for tool in responses[2]["result"]["tools"]]
    assert names == ["find_unfinished_transfers", "inspect_transfer", "prepare_recovery", "check_readiness"]


@pytest.mark.parametrize(("requested", "negotiated"), [
    ("2025-06-18", "2025-06-18"), ("2024-11-05", "2024-11-05"),
    ("2026-07-28", "2025-11-25"), ("2099-01-01", "2025-11-25"),
])
async def test_initialize_echoes_supported_versions_and_offers_latest_legacy_otherwise(requested, negotiated):
    server = mcp.McpServer(FakeService())
    [response] = await exchange(server, rpc(1, "initialize", {"protocolVersion": requested}, meta=False))
    assert response["result"]["protocolVersion"] == negotiated


async def test_older_clients_only_receive_fields_their_version_defines():
    oldest = mcp.tool_definitions("2024-11-05")[0]
    assert set(oldest) == {"name", "description", "inputSchema"}
    march = mcp.tool_definitions("2025-03-26")[0]
    assert march["annotations"]["readOnlyHint"] is True and "outputSchema" not in march
    server = mcp.McpServer(FakeService())
    result = (await exchange(server, rpc(1, "initialize", {"protocolVersion": "2025-03-26"}, meta=False),
                             call(2, "check_readiness") | {"params": {"name": "check_readiness"}}))[1]
    assert "structuredContent" not in result["result"]
    assert json.loads(result["result"]["content"][1]["text"])["status"] == "ready"


async def test_modern_requests_are_stateless_and_carry_server_identity():
    server = mcp.McpServer(FakeService())
    discover, tools = await exchange(server, rpc(1, "server/discover"), rpc(2, "tools/list"))
    result = discover["result"]
    assert result["resultType"] == "complete"
    assert result["supportedVersions"][0] == "2026-07-28"
    assert result["capabilities"] == {"tools": {}}
    assert result["_meta"][mcp.META_SERVER] == {"name": "rebound", "version": "0.1.0", "title": "Rebound"}
    assert (result["ttlMs"], result["cacheScope"]) == (300_000, "public")
    assert tools["result"]["resultType"] == "complete"
    assert len(tools["result"]["tools"]) == 4 and tools["result"]["cacheScope"] == "public"
    assert server.legacy_version is None


async def test_version_and_metadata_errors_follow_the_spec():
    server = mcp.McpServer(FakeService())
    unsupported, missing_caps, no_meta = await exchange(
        server,
        rpc(1, "tools/list", {"_meta": {mcp.META_VERSION: "1900-01-01", mcp.META_CAPABILITIES: {}}}, meta=False),
        rpc(2, "tools/list", {"_meta": {mcp.META_VERSION: "2026-07-28"}}, meta=False),
        rpc(3, "tools/list", meta=False),
    )
    assert unsupported["error"]["code"] == -32022
    assert unsupported["error"]["data"] == {"supported": list(mcp.SUPPORTED_VERSIONS), "requested": "1900-01-01"}
    assert missing_caps["error"]["code"] == -32602
    assert no_meta["error"]["code"] == -32602 and "initialize" in no_meta["error"]["message"]


async def test_tool_schemas_match_api_validation_and_teach_safety():
    tools = {tool["name"]: tool for tool in mcp.tool_definitions("2026-07-28")}
    for tool in tools.values():
        assert tool["inputSchema"]["type"] == "object"
        assert tool["inputSchema"]["additionalProperties"] is False
        assert tool["outputSchema"]["type"] == "object"
        assert tool["annotations"]["readOnlyHint"] is True and tool["annotations"]["openWorldHint"] is True
        assert "never signs" in tool["description"] and "broadcast" in tool["description"]
    scan = tools["find_unfinished_transfers"]["inputSchema"]
    assert scan["required"] == ["wallet"]
    assert scan["properties"]["wallet"]["pattern"] == r"^0x[0-9a-fA-F]{40}$"
    assert (scan["properties"]["blocks"]["minimum"], scan["properties"]["blocks"]["maximum"]) == (1, 50_000)
    assert scan["properties"]["blocks"]["default"] == 2000 and scan["properties"]["end_block"]["minimum"] == 0
    prepare = tools["prepare_recovery"]
    assert prepare["inputSchema"]["required"] == ["tx_hash", "log_index", "caller"]
    assert prepare["inputSchema"]["properties"]["tx_hash"]["pattern"] == r"^0x[0-9a-fA-F]{64}$"
    assert prepare["inputSchema"]["properties"]["log_index"]["maximum"] == 100_000
    assert "UNSIGNED" in prepare["description"] and "human" in prepare["description"]
    for rule in ("burn USDC again", "recipient is fixed", "completed", "awaiting_attestation"):
        assert rule in tools["inspect_transfer"]["description"]


async def test_find_unfinished_transfers_returns_summary_and_structured_steps():
    service = FakeService()
    result = await call_result(mcp.McpServer(service), "find_unfinished_transfers", {"wallet": WALLET})
    assert result["isError"] is False and result["resultType"] == "complete"
    data = result["structuredContent"]
    assert mcp.schema_errors(mcp.TOOL_SPECS["find_unfinished_transfers"]["outputSchema"], data) == []
    assert data["counts"] == {"ready": 1, "completed": 1}
    assert [row["next_step"]["action"] for row in data["transfers"]] == ["prepare_recovery", "none_already_minted"]
    assert (data["from_block"], data["to_block"], data["partial"]) == (51, 200, False)
    summary = result["content"][0]["text"]
    assert f"{HASH} log 3: 128.5 USDC to {WALLET}, status ready" in summary
    assert "Do not prepare, sign, mint or burn again" in summary
    assert json.loads(result["content"][1]["text"]) == data
    assert service.calls == [("scan", (WALLET,), {"blocks": 2000, "end_block": None})]

    await call_result(mcp.McpServer(service), "find_unfinished_transfers",
                      {"wallet": WALLET, "blocks": 200, "end_block": 61_125_511})
    assert service.calls[-1] == ("scan", (WALLET,), {"blocks": 200, "end_block": 61_125_511})


async def test_empty_scan_states_its_coverage_limit():
    service = FakeService()
    service.scan = lambda wallet, blocks, end_block: service._answer("scan", SCAN | {"transfers": []})
    result = await call_result(mcp.McpServer(service), "find_unfinished_transfers", {"wallet": WALLET})
    assert "not proof that nothing is pending" in result["content"][0]["text"]
    assert result["structuredContent"]["counts"] == {}


async def test_inspect_transfer_passes_log_index_and_explains_status():
    service = FakeService()
    result = await call_result(mcp.McpServer(service), "inspect_transfer", {"tx_hash": HASH, "log_index": 3})
    assert service.calls == [("inspect", (HASH,), {"log_index": 3})]
    assert mcp.schema_errors(mcp.TOOL_SPECS["inspect_transfer"]["outputSchema"], result["structuredContent"]) == []
    assert "1 supported burn(s): 1 ready" in result["content"][0]["text"]


async def test_prepare_recovery_is_unsigned_and_requires_review():
    service = FakeService()
    arguments = {"tx_hash": HASH, "log_index": 3, "caller": OTHER}
    result = await call_result(mcp.McpServer(service), "prepare_recovery", arguments)
    data = result["structuredContent"]
    assert mcp.schema_errors(mcp.TOOL_SPECS["prepare_recovery"]["outputSchema"], data) == []
    assert data["signed"] is False and data["broadcast"] is False
    assert data["transaction"] == PREPARED["transaction"]
    text = result["content"][0]["text"]
    assert text.startswith("Unsigned recovery transaction prepared. Nothing was signed or sent.")
    assert f"fixed recipient {WALLET}: 128.4995 USDC" in text
    assert "human or the caller's own wallet must review" in text and "gas limit 240,000" in text
    assert service.calls == [("prepare", (HASH, 3, OTHER), {})]


async def test_check_readiness_summarizes_providers():
    result = await call_result(mcp.McpServer(FakeService()), "check_readiness")
    assert result["content"][0]["text"].startswith("Rebound is ready (base_sepolia ok")
    assert result["structuredContent"] == READY


async def test_real_service_scan_and_prepare_through_mcp(setup_service):
    service, state = setup_service
    server = mcp.McpServer(service)
    scan = await call_result(server, "find_unfinished_transfers", {"wallet": WALLET, "blocks": 150})
    assert mcp.schema_errors(mcp.TOOL_SPECS["find_unfinished_transfers"]["outputSchema"],
                             scan["structuredContent"]) == []
    [row] = scan["structuredContent"]["transfers"]
    assert (row["status"], row["next_step"]["action"]) == ("ready", "prepare_recovery")

    arguments = {"tx_hash": row["tx_hash"], "log_index": row["log_index"], "caller": WALLET}
    prepared = await call_result(server, "prepare_recovery", arguments)
    assert mcp.schema_errors(mcp.TOOL_SPECS["prepare_recovery"]["outputSchema"],
                             prepared["structuredContent"]) == []
    assert prepared["structuredContent"]["transaction"]["chainId"] == 5042002

    state["used"] = True
    completed = await call_result(server, "prepare_recovery", arguments)
    assert completed["isError"] is True and "structuredContent" not in completed
    assert "completed" in completed["content"][0]["text"]
    assert "Nothing was prepared or sent" in completed["content"][0]["text"]


async def test_provider_failures_never_leak_urls_or_upstream_bodies(setup_service):
    service, state = setup_service
    state["rpc_error"] = True
    result = await call_result(mcp.McpServer(service), "find_unfinished_transfers", {"wallet": WALLET})
    text = result["content"][0]["text"]
    assert result["isError"] is True and text.startswith("Provider unavailable: Base Sepolia RPC")
    assert "private-provider-secret" not in text and "base.test" not in text
    assert mcp.redact("RPC https://user:key@rpc.example/v2/secret failed") == "RPC [redacted URL] failed"


@pytest.mark.parametrize(("error", "expected"), [
    (ValueError("End block must be at or before Base Sepolia block 200."), "Rejected: End block must be"),
    (UpstreamError("Arc Testnet RPC is unavailable. Retry or configure another RPC."),
     "This does not mean funds are lost"),
    (BusyError("Rebound is checking other transfers. Try again in a few seconds."), "Busy: Rebound is checking"),
    (RuntimeError("boom at https://user:secret@rpc.example"), "Internal error (RuntimeError)"),
])
async def test_service_errors_become_tool_errors(error, expected):
    for name, arguments in [("find_unfinished_transfers", {"wallet": WALLET}),
                            ("prepare_recovery", {"tx_hash": HASH, "log_index": 3, "caller": OTHER})]:
        result = await call_result(mcp.McpServer(FakeService(error)), name, arguments)
        assert result["isError"] is True and "structuredContent" not in result
        assert expected in result["content"][0]["text"]
        assert "secret" not in result["content"][0]["text"]


@pytest.mark.parametrize("arguments", [
    {"wallet": "not-a-wallet"}, {"wallet": WALLET + "\n"}, {"wallet": WALLET, "blocks": 50_001},
    {"wallet": WALLET, "blocks": 0}, {"wallet": WALLET, "blocks": True}, {"wallet": WALLET, "blocks": 20.5},
    {"wallet": WALLET, "end_block": -1}, {"wallet": WALLET, "private_key": "0x01"}, {},
])
async def test_invalid_arguments_are_tool_errors_before_any_upstream_work(arguments):
    service = FakeService()
    result = await call_result(mcp.McpServer(service), "find_unfinished_transfers", arguments)
    assert result["isError"] is True
    assert result["content"][0]["text"].startswith("Invalid arguments:")
    assert service.calls == []


async def test_protocol_errors_use_json_rpc_codes():
    server = mcp.McpServer(FakeService())
    responses = await exchange(
        server,
        rpc(1, "resources/list"),
        call(2, "sign_and_send"),
        rpc(3, "tools/call", {"arguments": {}}),
        rpc(4, "tools/call", {"name": "check_readiness", "arguments": [1]}),
        {"jsonrpc": "2.0", "id": 5, "method": "tools/list", "params": [1]},
        b'{"jsonrpc": "2.0", "id": 6, "method": \n',
        b"[]\n",
        {"jsonrpc": "1.0", "id": 7, "method": "ping"},
        {"jsonrpc": "2.0", "id": None, "method": "ping"},
    )
    codes = [(response["id"], response["error"]["code"]) for response in responses]
    assert codes == [(1, -32601), (2, -32602), (3, -32602), (4, -32602), (5, -32602),
                     (None, -32700), (None, -32600), (7, -32600), (None, -32600)]
    assert "Unknown tool: sign_and_send" in responses[1]["error"]["message"]


async def test_notifications_and_client_responses_get_no_reply():
    server = mcp.McpServer(FakeService())
    assert await exchange(
        server,
        {"jsonrpc": "2.0", "method": "notifications/initialized"},
        {"jsonrpc": "2.0", "method": "notifications/cancelled", "params": {"requestId": 99}},
        {"jsonrpc": "2.0", "method": "notifications/unknown", "params": [1]},
        {"jsonrpc": "2.0", "id": 5, "result": {}},
        b"\n",
    ) == []


async def test_cancelled_call_gets_no_response():
    service = FakeService(delay=5)
    responses = await exchange(
        mcp.McpServer(service),
        call(1, "check_readiness"),
        {"jsonrpc": "2.0", "method": "notifications/cancelled", "params": {"requestId": 1}},
        rpc(2, "ping"),
    )
    assert [response["id"] for response in responses] == [2]


async def test_duplicate_active_id_does_not_replace_cancellable_call():
    server = mcp.McpServer(FakeService(delay=5))
    duplicate, ping = await exchange(
        server,
        call(1, "check_readiness"),
        call(1, "check_readiness"),
        {"jsonrpc": "2.0", "method": "notifications/cancelled", "params": {"requestId": 1}},
        rpc(2, "ping"),
    )
    assert duplicate["id"] is None and duplicate["error"]["code"] == mcp.INVALID_REQUEST
    assert ping["id"] == 2


async def test_readiness_burst_is_bounded_before_service_dispatch(monkeypatch):
    monkeypatch.setattr(mcp, "MAX_INFLIGHT_CALLS", 2)
    service = FakeService(delay=0.01)
    responses = await exchange(mcp.McpServer(service), *(call(n, "check_readiness") for n in range(4)))
    assert {reply["id"] for reply in responses if reply["result"]["isError"]} == {2, 3}
    assert len(service.calls) == 2


async def test_unknown_argument_names_do_not_reflect_sensitive_urls():
    server = mcp.McpServer(FakeService())
    result = await call_result(server, "check_readiness", {"https://secret.example/rpc/token": True})
    assert result["isError"] is True
    assert "secret.example" not in result["content"][0]["text"]


async def test_slow_calls_time_out_as_tool_errors_and_do_not_block_others():
    slow = mcp.McpServer(FakeService(delay=5), call_timeout=0.05)
    result = await call_result(slow, "find_unfinished_transfers", {"wallet": WALLET})
    assert result["isError"] is True and result["content"][0]["text"].startswith("Timed out after 0.05 seconds")

    server = mcp.McpServer(FakeService(delay=0.2))
    responses = await exchange(server, call(1, "check_readiness"), rpc(2, "ping"))
    assert [response["id"] for response in responses] == [2, 1]  # The ping did not wait for the call.


async def test_stdin_reader_splits_lines_and_bounds_message_size(monkeypatch):
    monkeypatch.setattr(mcp, "MAX_MESSAGE_BYTES", 64)
    stream = io.BytesIO(b'{"a":1}\n' + b"x" * 200 + b"\n" + b'{"b":2}\n')
    lines = [line async for line in mcp.stdin_lines(stream)]
    assert lines[0] == b'{"a":1}\n' and lines[2] == b'{"b":2}\n'
    written = []
    server = mcp.McpServer(FakeService())
    server._write = written.append
    await server.receive(lines[1])
    assert json.loads(written[0])["error"] == {"code": -32700, "message": "Message exceeds 64 bytes."}


def test_stdio_process_writes_only_json_rpc_and_exits_on_eof(tmp_path):
    messages = [
        rpc(1, "initialize", {"protocolVersion": "2025-06-18", "capabilities": {}}, meta=False),
        {"jsonrpc": "2.0", "method": "notifications/initialized"},
        rpc(2, "tools/list", meta=False),
        rpc(3, "server/discover"),
        call(4, "find_unfinished_transfers", {"wallet": "nope"}),
    ]
    stdin = "".join(json.dumps(message) + "\n" for message in messages) + "not json\n"
    env = os.environ | {"REBOUND_DB": str(tmp_path / "mcp.sqlite3")}
    process = subprocess.run([sys.executable, "-m", "rebound.mcp"], input=stdin.encode(), capture_output=True,
                             cwd=Path(__file__).resolve().parent.parent, env=env, timeout=30)
    assert process.returncode == 0, process.stderr.decode()
    responses = [json.loads(line) for line in process.stdout.decode().splitlines()]
    assert all(response["jsonrpc"] == "2.0" for response in responses)
    assert sorted(response["id"] or 0 for response in responses) == [0, 1, 2, 3, 4]
    assert {response["id"]: response for response in responses}[4]["result"]["isError"] is True
    assert b"ready on stdio" in process.stderr and b"stdin closed" in process.stderr


def test_schema_validator_covers_nullable_fields_and_nested_rows():
    schema = mcp.TOOL_SPECS["inspect_transfer"]["outputSchema"]
    row = ROW | {"next_step": mcp.next_step(ROW)}
    data = {"tx_hash": HASH, "coverage": "x", "partial": False, "counts": {"ready": 1}, "transfers": [row]}
    assert mcp.schema_errors(schema, data) == []
    assert mcp.schema_errors(schema, data | {"transfers": [row | {"net_amount": 5}]}) == [
        "transfers[0].net_amount must be string or null"]
    assert mcp.schema_errors(schema, data | {"counts": {"ready": "1"}}) == ["counts.ready must be integer"]
    assert mcp.next_step(ROW | {"status": "caller_required", "caller": OTHER})["detail"].count(OTHER) == 1
    assert mcp.next_step(ROW | {"status": "brand_new"})["action"] == "inspect"

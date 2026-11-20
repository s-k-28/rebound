import asyncio
import json
from datetime import UTC, datetime

import httpx
import pytest
from eth_abi import encode

from rebound import service
from rebound.clients import Rpc, UpstreamError
from tests.test_protocol import OTHER, WALLET, burn_log, message_hex


@pytest.fixture
def setup_service(tmp_path):
    state = {"used": False, "circle_status": 200, "rpc_error": False,
             "message": message_hex(), "canonical": True, "logs": [burn_log()]}

    def respond(request):
        if request.url.host == "circle.test":
            if state["circle_status"] != 200:
                return httpx.Response(state["circle_status"], json={"error": "pending"})
            if request.url.path == "/v2/burn/USDC/fees/6/26":
                state["fee_checks"] = state.get("fee_checks", 0) + 1
                return httpx.Response(200, json=state.get("fees", [{"finalityThreshold": 2000, "minimumFee": 0}]))
            return httpx.Response(200, json={"sourceTxHash": state.get("source_hash", "0x" + "cd" * 32),
                "messages": state.get("messages", [{"status": "complete", "message": state["message"],
                              "attestation": "0x" + "11" * 65}])})
        payload = json.loads(request.content)
        method, params = payload["method"], payload["params"]
        dest = request.url.host == "arc.test"
        if method == "eth_getLogs" and state.get("range_limit"):
            count = int(params[0]["toBlock"], 16) - int(params[0]["fromBlock"], 16) + 1
            if count > state["range_limit"]:
                return httpx.Response(413, json={"error": {"code": -32614, "message": "range limited"}})
        if state["rpc_error"] and method == "eth_getLogs":
            return httpx.Response(200, json={"error": {"code": -1, "message": "private-provider-secret"}})
        match method:
            case "eth_chainId":
                result = hex(state.get("arc_chain", 5042002) if dest else state.get("base_chain", 84532))
            case "eth_getCode":
                result = state.get("code", "0x6001")
            case "eth_blockNumber":
                result = hex(900 if dest else state.get("base_head", 200))
            case "eth_getBlockByNumber":
                result = {"hash": "0x" + ("ef" if state["canonical"] else "aa") * 32,
                          "number": "0x64", "transactions": ["0x" + "cd" * 32]}
            case "eth_getLogs":
                from rebound.protocol import BURN_TOPIC, MESSENGER, USDC
                assert params[0]["topics"] == [BURN_TOPIC, "0x" + "00" * 12 + USDC[2:]]
                assert params[0]["address"] == MESSENGER
                assert int(params[0]["toBlock"], 16) - int(params[0]["fromBlock"], 16) < 1000
                result = state["logs"]
            case "eth_getTransactionReceipt":
                result = {"status": "0x1", "blockNumber": "0x64", "blockHash": "0x" + "ef" * 32,
                          "transactionHash": "0x" + "cd" * 32, "transactionIndex": "0x0", "logs": state["logs"]}
            case "eth_call":
                data = params[0]["data"]
                result = "0x" + (int(state["used"]) if not data.startswith("0x57ecfd28") else state.get("simulation", 1)).to_bytes(32, "big").hex()
            case "eth_estimateGas":
                result = "0x30d40"
            case "eth_gasPrice":
                result = hex(10**9)
            case "eth_getBalance":
                result = hex(state.get("caller_balance", 10**18))
            case _:
                raise AssertionError(f"Unexpected RPC method {method}")
        return httpx.Response(200, json={"jsonrpc": "2.0", "id": 1, "result": result})

    client = httpx.AsyncClient(transport=httpx.MockTransport(respond))
    settings = service.Settings(base_rpc="https://base.test", arc_rpc="https://arc.test",
        circle_url="https://circle.test", database=str(tmp_path / "events.sqlite3"))
    return service.RecoveryService(settings, client), state


async def test_scan_discovers_without_hash_and_reports_coverage(setup_service):
    app, _ = setup_service
    result = await app.scan(WALLET, blocks=150)
    assert result["from_block"] == 51
    assert result["to_block"] == 200
    assert len(result["transfers"]) == 1
    assert result["transfers"][0]["status"] == "ready"
    assert result["transfers"][0]["recipient"] == WALLET


async def test_fixed_history_finds_old_burn_without_mixing_in_other_cached_blocks(setup_service):
    app, _ = setup_service
    app.ingest_logs([burn_log() | {"transactionHash": "0x" + "88" * 32, "blockNumber": "0xc8"}])
    result = await app.scan(WALLET, blocks=20, end_block=101)
    assert (result["from_block"], result["to_block"]) == (82, 101)
    assert [row["block_number"] for row in result["transfers"]] == [100]
    assert result["transfers"][0]["status"] == "ready"  # Confirmations use today's head, not the anchor.


async def test_future_end_block_is_rejected(setup_service):
    app, _ = setup_service
    with pytest.raises(ValueError, match="at or before"):
        await app.scan(WALLET, end_block=201)


def utc_cutoff(timestamp):
    return datetime.fromtimestamp(timestamp, UTC).isoformat().replace("+00:00", "Z")


def time_chain(app, state, times, *, earliest=0, corrupt=None):
    state["base_head"] = len(times) - 1
    calls = []
    original = app.base.call

    async def timed_block(method, params=None):
        if method == "eth_getBlockByNumber":
            assert params[1] is False
            height = earliest if params[0] == "earliest" else int(params[0], 16)
            assert earliest <= height < len(times)
            calls.append(height)
            result = {"number": hex(height), "timestamp": hex(times[height]),
                      "hash": "0x" + "ef" * 32 if height == 100 else f"0x{height:064x}",
                      "transactions": ["0x" + "cd" * 32]}
            return corrupt(height, result) if corrupt else result
        return await original(method, params)

    app.base.call = timed_block
    return calls


@pytest.mark.parametrize("offset, expected", [(0, 0), (9, 0), (10, 3), (19, 3), (20, 4), (999, 4)])
async def test_scan_utc_cutoff_includes_all_blocks_with_the_same_second(setup_service, offset, expected):
    app, state = setup_service
    state["logs"] = []
    genesis = 1_700_000_000
    calls = time_chain(app, state, [genesis + value for value in (0, 10, 10, 10, 20)])
    result = await app.scan(WALLET, blocks=2, end_time=utc_cutoff(genesis + offset))
    assert result["to_block"] == expected and result["from_block"] == max(0, expected - 1)
    assert utc_cutoff(genesis + offset) in result["coverage"]
    assert "fixed range" in result["coverage"] and "previously indexed" not in result["coverage"]
    assert len(calls) <= 8


@pytest.mark.parametrize("value", ["2023-11-14 22:13", "2023-11-14T22:13", "2023-11-14T22:13Z",
                                     "2023-11-14T22:13:00", "2023-11-14T22:13:00Z"])
async def test_scan_accepts_only_documented_utc_formats(setup_service, value):
    app, state = setup_service
    state["logs"] = []
    time_chain(app, state, [1_699_999_900, 1_700_000_000])
    result = await app.scan(WALLET, blocks=1, end_time=value)
    assert result["to_block"] == 0 and "2023-11-14T22:13:00Z" in result["coverage"]


@pytest.mark.parametrize("value", ["", "now", "2023-11-14", "2023-11-14 22:13:20", "2023-11-14T22:13:20+00:00",
                                     "2023-11-14T22:13:20.1Z", "2023-02-29T22:13Z", "2023-11-14T24:00Z",
                                     " 2023-11-14T22:13Z", "2023-11-14T22:13Z\n", "x" * 10000, 123])
async def test_invalid_time_cutoffs_fail_before_rpc(setup_service, value):
    app, _ = setup_service

    async def no_rpc(*args):
        pytest.fail("Invalid cutoff reached RPC.")

    app.base.call = no_rpc
    with pytest.raises(ValueError, match="UTC"):
        await app.scan(WALLET, end_time=value)


async def test_scan_time_and_height_are_mutually_exclusive(setup_service):
    app, _ = setup_service
    with pytest.raises(ValueError, match="either|both|exclusive"):
        await app.scan(WALLET, end_block=100, end_time="2023-11-14T22:13Z")


@pytest.mark.parametrize("patch", [{"number": "0xff"}, {"number": None}, {"timestamp": None},
                                  {"timestamp": "private-provider-data"}, {"hash": "bad"}])
async def test_time_lookup_rejects_malformed_provider_headers(setup_service, patch):
    app, state = setup_service
    state["logs"] = []
    time_chain(app, state, [1_700_000_000 + value for value in range(201)],
               corrupt=lambda height, block: block | patch)
    with pytest.raises(UpstreamError) as error:
        await app.scan(WALLET, end_time=utc_cutoff(1_700_000_100))
    assert "private-provider-data" not in str(error.value)


async def test_pruned_time_history_uses_actual_earliest_block(setup_service):
    app, state = setup_service
    state["logs"] = []
    genesis = 1_700_000_000
    calls = time_chain(app, state, [genesis + height for height in range(201)], earliest=50)
    result = await app.scan(WALLET, blocks=2, end_time=utc_cutoff(genesis + 51))
    assert result["to_block"] == 51 and min(calls) == 50
    with pytest.raises(ValueError, match="available source history|genesis"):
        await app.scan(WALLET, end_time=utc_cutoff(genesis + 49))


async def test_time_range_is_clamped_to_provider_floor_and_reported_partial(setup_service):
    app, state = setup_service
    state["logs"] = []
    genesis = 1_700_000_000
    time_chain(app, state, [genesis + height for height in range(201)], earliest=50)
    result = await app.scan(WALLET, blocks=200, end_time=utc_cutoff(genesis + 51))
    assert (result["from_block"], result["to_block"]) == (50, 51)
    assert result["partial"] is True and "shortened" in result["coverage"]


async def test_time_lookup_has_a_deadline_and_releases_read_slots(setup_service, monkeypatch):
    app, _ = setup_service
    original = app.base.call

    async def stalled_header(method, params=None):
        if method == "eth_getBlockByNumber":
            await asyncio.Future()
        return await original(method, params)

    app.base.call = stalled_header
    monkeypatch.setattr(service, "TIME_SEARCH_TIMEOUT_SECONDS", 0.01)
    with pytest.raises(UpstreamError, match="timed out"):
        await app.scan(WALLET, end_time="2023-11-14T22:13Z")
    assert app._read_slots._value == 3


async def test_time_lookup_rejects_nonmonotonic_sampled_timestamps(setup_service):
    app, state = setup_service
    genesis = 1_700_000_000
    times = [genesis + height for height in range(201)]
    times[100] = genesis + 999
    time_chain(app, state, times)
    with pytest.raises(UpstreamError, match="inconsistent"):
        await app.scan(WALLET, end_time=utc_cutoff(genesis + 101))


async def test_time_lookup_rejects_an_unbounded_source_head(setup_service):
    app, state = setup_service
    state["base_head"] = 2**64
    with pytest.raises(UpstreamError, match="invalid source head"):
        await app.scan(WALLET, end_time="2023-11-14T22:13Z")


async def test_time_scan_uses_current_confirmations_and_fixed_database_range(setup_service):
    app, state = setup_service
    genesis = 1_700_000_000
    app.ingest_logs([burn_log() | {"transactionHash": "0x" + "88" * 32, "blockNumber": "0xc8"}])
    time_chain(app, state, [genesis + height for height in range(201)])
    result = await app.scan(WALLET, blocks=2, end_time=utc_cutoff(genesis + 101))
    row, = result["transfers"]
    assert (result["from_block"], result["to_block"]) == (100, 101)
    assert row["block_number"] == 100 and row["status"] == "ready" and row["source_confirmations"] == 101


async def test_source_reorg_never_becomes_ready(setup_service):
    app, state = setup_service
    state["canonical"] = False
    result = await app.scan(WALLET, blocks=150)
    assert result["transfers"][0]["status"] == "source_changed"


@pytest.mark.parametrize("target, patch", [
    ("block", {"number": "0x65"}),
    ("block", {"number": None}),
    ("block", {"transactions": []}),
    ("block", {"transactions": None}),
    ("block", {"transactions": ["0x" + "12" * 32, "0x" + "cd" * 32]}),
    ("block", {"transactions": ["provider-private-invalid-hash"]}),
    ("receipt", {"transactionIndex": None}),
    ("receipt", {"transactionIndex": "0x1"}),
    ("receipt", {"transactionIndex": "provider-private-invalid-index"}),
    ("receipt", {"blockNumber": None}),
    ("log", {"transactionIndex": "0x1"}),
    ("log", {"transactionIndex": None}),
])
async def test_source_receipt_identity_failures_are_typed_and_never_ready(setup_service, target, patch):
    app, state = setup_service
    if target == "log":
        state["logs"] = [state["logs"][0] | patch]
    original = app.base.call

    async def changed_source(method, params=None):
        result = await original(method, params)
        if target == "block" and method == "eth_getBlockByNumber":
            return result | patch
        if target == "receipt" and method == "eth_getTransactionReceipt":
            return result | patch
        return result

    app.base.call = changed_source
    row, = (await app.scan(WALLET, blocks=150))["transfers"]
    assert row["status"] == "source_changed" and row["source_verified"] is False
    assert "provider-private" not in row["explanation"]
    with pytest.raises(service.SourceChanged) as error:
        await app.inspect_transaction("0x" + "cd" * 32, log_index=3)
    assert "provider-private" not in str(error.value)
    with pytest.raises(service.SourceChanged):
        await app.prepare("0x" + "cd" * 32, 3, WALLET)


@pytest.mark.parametrize("include_event_index", [False, True])
async def test_source_receipt_accepts_exact_nonzero_transaction_position(setup_service, include_event_index):
    app, state = setup_service
    if include_event_index:
        state["logs"] = [state["logs"][0] | {"transactionIndex": "0x1"}]
    original = app.base.call

    async def indexed_source(method, params=None):
        result = await original(method, params)
        if method == "eth_getBlockByNumber":
            assert params == ["0x64", False]
            return result | {"transactions": ["0x" + "12" * 32, "0x" + "cd" * 32]}
        if method == "eth_getTransactionReceipt":
            return result | {"transactionIndex": "0x1"}
        return result

    app.base.call = indexed_source
    row, = (await app.scan(WALLET, blocks=150))["transfers"]
    assert row["status"] == "ready" and row["source_verified"] is True


async def test_source_block_outage_remains_unavailable(setup_service):
    app, _ = setup_service
    original = app.base.call

    async def unavailable_block(method, params=None):
        if method == "eth_getBlockByNumber":
            raise UpstreamError("Base Sepolia is unavailable.")
        return await original(method, params)

    app.base.call = unavailable_block
    row, = (await app.scan(WALLET, blocks=150))["transfers"]
    assert row["status"] == "unavailable"


async def test_rpc_failure_is_not_a_successful_empty_inbox(setup_service):
    app, state = setup_service
    state["rpc_error"] = True
    with pytest.raises(service.UpstreamError) as exc:
        await app.scan(WALLET, blocks=150)
    assert "private-provider-secret" not in str(exc.value)


async def test_circle_404_is_waiting_but_500_is_unavailable(setup_service):
    app, state = setup_service
    state["circle_status"] = 404
    assert (await app.scan(WALLET, blocks=150))["transfers"][0]["status"] == "awaiting_attestation"
    state["circle_status"] = 500
    assert (await app.scan(WALLET, blocks=150))["transfers"][0]["status"] == "unavailable"


async def test_recent_source_burn_reports_confirmations_before_waiting_for_circle(setup_service):
    app, state = setup_service
    state.update(base_head=103, circle_status=404)
    row, = (await app.scan(WALLET, blocks=10))["transfers"]
    assert row["status"] == "source_confirming"
    assert row["source_confirmations"] == 4

    state["base_head"] = 111
    row, = (await app.scan(WALLET, blocks=20))["transfers"]
    assert row["status"] == "awaiting_attestation"
    assert row["source_confirmations"] == 12


async def test_same_transaction_multiple_messages_match_burn_not_first_api_item(setup_service):
    app, state = setup_service
    state["message"] = message_hex(recipient=OTHER)
    result = await app.scan(WALLET, blocks=150)
    assert result["transfers"][0]["status"] == "unsupported"


async def test_prepare_rechecks_nonce_instead_of_trusting_previous_ready(setup_service):
    app, state = setup_service
    tx = await app.prepare("0x" + "cd" * 32, 3, WALLET)
    assert tx["transaction"]["chainId"] == 5042002
    assert tx["transaction"]["value"] == "0x0"
    assert tx["transaction"]["to"] == "0xe737e5cebeeba77efe34d4aa090756590b1ce275"
    assert tx["net_amount"] == "128.4995"
    state["used"] = True
    with pytest.raises(ValueError, match="completed"):
        await app.prepare("0x" + "cd" * 32, 3, WALLET)


@pytest.mark.parametrize("receipt_variant", [
    "matching", "wrong_nonce", "wrong_body", "reverted", "missing",
    "reorged_block", "wrong_block_height", "missing_block_height", "missing_receipt_block",
    "block_unavailable", "wrong_log_tx", "wrong_log_block_hash", "wrong_log_block_height",
    "wrong_log_tx_index", "transaction_not_in_block", "missing_transactions",
])
@pytest.mark.parametrize("hint_from", ["circle", "local"])
async def test_completed_links_only_to_a_matching_arc_receipt(setup_service, receipt_variant, hint_from):
    from rebound import protocol
    from tests.test_protocol import word_address

    app, state = setup_service
    state["used"] = True
    tx_hash = "0x" + "ae" * 32
    raw = message_hex()
    message = protocol.decode_message(raw)
    row = {"status": "complete", "message": raw, "attestation": "0x" + "11" * 65}
    if hint_from == "circle":
        row["destinationMintTxHash"] = tx_hash
    else:
        burn = protocol.decode_burn(burn_log())
        app.store.remember_mint(burn.tx_hash, burn.log_index, message.nonce, tx_hash)
    state["messages"] = [row]
    body = bytes.fromhex(raw[2:])[148:]
    block_hash = "0x" + "bc" * 32
    receipt = {"status": "0x0" if receipt_variant == "reverted" else "0x1", "transactionHash": tx_hash,
               "blockNumber": "0x2bc", "blockHash": block_hash, "transactionIndex": "0x0",
               "logs": [{"address": protocol.TRANSMITTER,
                         "transactionHash": "0x" + "fe" * 32 if receipt_variant == "wrong_log_tx" else tx_hash,
                         "blockHash": "0x" + "fe" * 32 if receipt_variant == "wrong_log_block_hash" else block_hash,
                         "blockNumber": "0x2bd" if receipt_variant == "wrong_log_block_height" else "0x2bc",
                         "transactionIndex": "0x1" if receipt_variant == "wrong_log_tx_index" else "0x0",
                         "topics": [protocol.RECEIVED_TOPIC, "0x" + word_address(WALLET).hex(),
                                    "0x" + "ff" * 32 if receipt_variant == "wrong_nonce" else message.nonce,
                                    "0x" + (2000).to_bytes(32, "big").hex()],
                         "data": "0x" + encode(["uint32", "bytes32", "bytes"],
                                               [6, word_address(protocol.MESSENGER),
                                                b"wrong" if receipt_variant == "wrong_body" else body]).hex()}]}
    if receipt_variant == "missing_receipt_block":
        receipt.pop("blockHash")
    original = app.arc.call

    async def arc_call(method, params=None):
        if method == "eth_getTransactionReceipt":
            assert params == [tx_hash]
            return None if receipt_variant == "missing" else receipt
        if method == "eth_getBlockByNumber":
            assert params == ["0x2bc", False]
            if receipt_variant == "block_unavailable":
                raise UpstreamError("Arc cannot provide this block.")
            return {
                "hash": "0x" + "ff" * 32 if receipt_variant == "reorged_block" else block_hash,
                "number": None if receipt_variant == "missing_block_height" else "0x2bd" if receipt_variant == "wrong_block_height" else "0x2bc",
                "transactions": None if receipt_variant == "missing_transactions" else [
                    "0x" + "fe" * 32 if receipt_variant == "transaction_not_in_block" else tx_hash,
                ],
            }
        return await original(method, params)

    app.arc.call = arc_call
    row = (await app.scan(WALLET, blocks=150))["transfers"][0]
    assert row["status"] == "completed"
    assert row["destination_url"] == (f"https://explorer.testnet.arc.io/tx/{tx_hash}" if receipt_variant == "matching" else None)


@pytest.fixture
def submitted_arc_receipt(setup_service):
    """A real-shaped destination receipt for a message not yet linked by Circle."""
    from rebound import protocol as p
    from tests.test_protocol import word_address

    app, state = setup_service
    state["used"] = True
    tx_hash, block_hash = "0x" + "ae" * 32, "0x" + "bc" * 32
    raw = message_hex()
    receipt = {
        "status": "0x1", "transactionHash": tx_hash, "transactionIndex": "0x0",
        "blockNumber": "0x2bc", "blockHash": block_hash,
        "logs": [{
            "address": p.TRANSMITTER, "transactionHash": tx_hash, "transactionIndex": "0x0",
            "blockNumber": "0x2bc", "blockHash": block_hash,
            "topics": [p.RECEIVED_TOPIC, "0x" + word_address(WALLET).hex(),
                       "0x" + "ab" * 32, "0x" + (2000).to_bytes(32, "big").hex()],
            "data": "0x" + encode(["uint32", "bytes32", "bytes"],
                                    [6, word_address(p.MESSENGER), bytes.fromhex(raw[2:])[148:]]).hex(),
        }],
    }
    destination = {"receipts": {tx_hash: receipt}, "block": {
        "hash": block_hash, "number": "0x2bc", "transactions": [tx_hash],
    }, "unavailable": False}
    original = app.arc.call

    async def arc_call(method, params=None):
        if method == "eth_getTransactionReceipt":
            if destination["unavailable"]:
                raise UpstreamError("Arc receipt is temporarily unavailable.")
            return destination["receipts"].get(params[0])
        if method == "eth_getBlockByNumber":
            assert params == ["0x2bc", False]
            return destination["block"]
        return await original(method, params)

    app.arc.call = arc_call
    return app, state, tx_hash, receipt, destination


async def test_submitted_receipt_is_verified_without_a_circle_hint_or_database_write(submitted_arc_receipt):
    from rebound import protocol as p

    app, _, tx_hash, _, _ = submitted_arc_receipt
    result = await app.inspect_transaction("0x" + "cd" * 32, log_index=3,
        destination_hash=tx_hash.upper().replace("0X", "0x"))
    row, = result["transfers"]
    assert row["status"] == "completed" and row["nonce_used"] is True
    assert row["submitted_destination_hash"] == tx_hash
    assert row["submitted_receipt_verified"] is True
    assert row["submitted_receipt_url"] == f"https://explorer.testnet.arc.io/tx/{tx_hash}"
    assert row["destination_url"] is None  # Keep the independently established receipt separate.
    assert app.store.local_mint(p.decode_burn(burn_log()), "0x" + "ab" * 32) is None
    assert app.store.candidates(WALLET) == []


async def test_verified_submitted_receipt_supplies_mint_time_without_a_circle_hint(submitted_arc_receipt):
    app, _, tx_hash, _, destination = submitted_arc_receipt
    destination["block"]["timestamp"] = "0x6affec78"
    reads = []
    original = app.arc.call

    async def counted_arc(method, params=None):
        if method == "eth_getBlockByNumber":
            reads.append(params)
        return await original(method, params)

    app.arc.call = counted_arc
    row, = (await app.inspect_transaction("0x" + "cd" * 32, log_index=3,
                                          destination_hash=tx_hash))["transfers"]
    assert row["status"] == "completed" and row["submitted_receipt_verified"] is True
    assert row["minted_at"] == "2026-11-20T06:45:12+00:00"
    assert row["destination_url"] is None
    assert reads == [["0x2bc", False]]  # Reuse the exact receipt's verification read.


@pytest.mark.parametrize("failure", ["wrong_nonce", "reorg", "unavailable", "reverted", "missing"])
async def test_bad_submitted_hint_does_not_change_consumed_nonce_truth(submitted_arc_receipt, failure):
    app, _, tx_hash, receipt, destination = submitted_arc_receipt
    if failure == "wrong_nonce":
        receipt["logs"][0]["topics"][2] = "0x" + "ff" * 32
    elif failure == "reorg":
        destination["block"]["hash"] = "0x" + "ff" * 32
    elif failure == "unavailable":
        destination["unavailable"] = True
    elif failure == "reverted":
        receipt["status"] = "0x0"
    else:
        destination["receipts"] = {}
    row, = (await app.inspect_transaction("0x" + "cd" * 32, log_index=3,
                                        destination_hash=tx_hash))["transfers"]
    assert row["status"] == "completed" and row["nonce_used"] is True
    assert row["submitted_receipt_verified"] is False and row["submitted_receipt_url"] is None
    assert row["destination_url"] is None


async def test_submitted_receipt_cannot_verify_an_unused_current_nonce(submitted_arc_receipt):
    app, state, tx_hash, _, _ = submitted_arc_receipt
    state["used"] = False
    row, = (await app.inspect_transaction("0x" + "cd" * 32, log_index=3,
                                        destination_hash=tx_hash))["transfers"]
    assert row["status"] == "ready" and row["nonce_used"] is False
    assert row["submitted_receipt_verified"] is False and row["submitted_receipt_url"] is None


async def test_bad_submitted_hash_does_not_overwrite_an_authentic_circle_receipt(submitted_arc_receipt):
    app, state, good_hash, _, _ = submitted_arc_receipt
    state["messages"] = [{"status": "complete", "message": message_hex(),
                          "attestation": "0x" + "11" * 65, "destinationMintTxHash": good_hash}]
    bad_hash = "0x" + "fe" * 32
    row, = (await app.inspect_transaction("0x" + "cd" * 32, log_index=3,
                                        destination_hash=bad_hash))["transfers"]
    assert row["submitted_destination_hash"] == bad_hash
    assert row["submitted_receipt_verified"] is False and row["submitted_receipt_url"] is None
    assert row["destination_url"] == f"https://explorer.testnet.arc.io/tx/{good_hash}"


async def test_submitted_receipt_requires_source_and_matching_attestation(submitted_arc_receipt):
    app, state, tx_hash, _, _ = submitted_arc_receipt
    state["circle_status"] = 404
    row, = (await app.inspect_transaction("0x" + "cd" * 32, log_index=3,
                                        destination_hash=tx_hash))["transfers"]
    assert row["status"] == "awaiting_attestation" and row["submitted_receipt_verified"] is False
    state["circle_status"] = 200
    state["logs"].append(burn_log() | {"logIndex": "0x4"})
    row, = (await app.inspect_transaction("0x" + "cd" * 32, log_index=3,
                                        destination_hash=tx_hash))["transfers"]
    assert row["status"] == "unsupported" and row["submitted_receipt_verified"] is False
    state["canonical"] = False
    with pytest.raises(ValueError, match="canonical"):
        await app.inspect_transaction("0x" + "cd" * 32, log_index=3, destination_hash=tx_hash)


@pytest.mark.parametrize("index", [None, -1, True])
async def test_submitted_receipt_requires_one_explicit_nonnegative_event_index(setup_service, index):
    app, _ = setup_service
    with pytest.raises(ValueError, match="log_index"):
        await app.inspect_transaction("0x" + "cd" * 32, log_index=index,
                                      destination_hash="0x" + "ae" * 32)


def with_base_block_time(app, timestamp="0x6affec15"):
    """Add a timestamp to the Base blocks the fixture returns (Base Sepolia block 61,175,721)."""
    original = app.base.call

    async def base_call(method, params=None):
        result = await original(method, params)
        return result | {"timestamp": timestamp} if method == "eth_getBlockByNumber" else result

    app.base.call = base_call


async def test_burn_and_mint_times_come_from_the_canonical_blocks_already_read(submitted_arc_receipt):
    app, state, tx_hash, _, destination = submitted_arc_receipt
    state["messages"] = [{"status": "complete", "message": message_hex(),
                          "attestation": "0x" + "11" * 65, "destinationMintTxHash": tx_hash}]
    destination["block"]["timestamp"] = "0x6affec78"  # Arc Testnet block 73,147,365
    with_base_block_time(app)
    for row in [(await app.scan(WALLET, blocks=150))["transfers"][0],
                (await app.inspect_transaction("0x" + "cd" * 32, log_index=3))["transfers"][0]]:
        assert row["status"] == "completed"
        assert row["destination_url"] == f"https://explorer.testnet.arc.io/tx/{tx_hash}"
        assert (row["burned_at"], row["minted_at"]) == ("2026-11-20T06:43:33+00:00", "2026-11-20T06:45:12+00:00")


async def test_waiting_burn_has_a_time_but_no_mint_time(setup_service):
    app, state = setup_service
    state["circle_status"] = 404
    with_base_block_time(app)
    row, = (await app.scan(WALLET, blocks=150))["transfers"]
    assert row["status"] == "awaiting_attestation"
    assert (row["burned_at"], row["minted_at"]) == ("2026-11-20T06:43:33+00:00", None)


@pytest.mark.parametrize("timestamp", [253_402_300_800, 2**64])
async def test_out_of_range_source_time_cannot_change_verified_recovery_status(setup_service, timestamp):
    app, _ = setup_service
    with_base_block_time(app, hex(timestamp))
    row, = (await app.scan(WALLET, blocks=150))["transfers"]
    assert row["status"] == "ready"
    assert row["source_verified"] is True and row["attestation_matched"] is True
    assert row["burned_at"] is None and row["minted_at"] is None
    assert app._block_times == {}


@pytest.mark.parametrize("timestamp", [253_402_300_800, 2**64])
async def test_out_of_range_arc_time_cannot_change_verified_delivery(submitted_arc_receipt, timestamp):
    app, state, tx_hash, _, destination = submitted_arc_receipt
    state["messages"] = [{"status": "complete", "message": message_hex(),
                          "attestation": "0x" + "11" * 65, "destinationMintTxHash": tx_hash}]
    with_base_block_time(app)
    destination["block"]["timestamp"] = hex(timestamp)
    row, = (await app.inspect_transaction("0x" + "cd" * 32, log_index=3))["transfers"]
    assert row["status"] == "completed" and row["nonce_used"] is True
    assert row["source_verified"] is True and row["attestation_matched"] is True
    assert row["destination_url"] == f"https://explorer.testnet.arc.io/tx/{tx_hash}"
    assert row["burned_at"] == "2026-11-20T06:43:33+00:00" and row["minted_at"] is None
    assert destination["block"]["hash"] not in app._block_times


@pytest.mark.parametrize("timestamp", [253_402_300_800, 2**64])
def test_cached_unrepresentable_time_remains_optional(setup_service, timestamp):
    app, _ = setup_service
    block_hash = "0x" + "ef" * 32
    app._block_times[block_hash] = timestamp
    assert app._block_time(block_hash) is None


async def test_missing_or_reorged_block_time_is_never_filled_in(setup_service):
    app, state = setup_service
    row, = (await app.scan(WALLET, blocks=150))["transfers"]
    assert row["status"] == "ready" and (row["burned_at"], row["minted_at"]) == (None, None)
    with_base_block_time(app)
    assert (await app.scan(WALLET, blocks=150))["transfers"][0]["burned_at"] is not None
    state["canonical"] = False  # The cached time belongs to a block hash that is no longer canonical.
    row, = (await app.scan(WALLET, blocks=150))["transfers"]
    assert row["status"] == "source_changed" and row["burned_at"] is None


def test_block_time_cache_skips_invalid_blocks_and_stays_bounded(monkeypatch):
    monkeypatch.setattr(service, "BLOCK_TIME_CACHE_SIZE", 2)
    times = {}
    for block in [{"hash": "0x" + "01" * 32, "timestamp": "6affec15"}, {"hash": "0x" + "01" * 32},
                  {"hash": "not-a-hash", "timestamp": "0x1"}, {"timestamp": "0x1"}]:
        service._remember_block_time(times, block)
    assert times == {}
    for index in range(1, 4):
        service._remember_block_time(times, {"hash": "0x" + f"{index:02x}" * 32, "timestamp": hex(index)})
    assert times == {"0x" + "02" * 32: 2, "0x" + "03" * 32: 3}


async def test_unrelated_wallet_is_not_attributed_a_transfer(setup_service):
    app, _ = setup_service
    assert (await app.scan(OTHER, blocks=150))["transfers"] == []


@pytest.mark.parametrize("failure", [None, "already_used", "failed_simulation", "wrong_block_hash"])
async def test_replay_pins_nonce_and_simulation_to_canonical_historical_blocks(setup_service, failure):
    from rebound import protocol as p
    from tests.test_protocol import word_address

    app, state = setup_service
    source_hash, arc_hash = "0x" + "cd" * 32, "0x" + "42" * 32
    block_hash = "0x" + "bc" * 32
    raw = message_hex()
    signature = "0x" + "11" * 65
    message = p.decode_message(raw)
    state["messages"] = [{"status": "complete", "message": raw, "attestation": signature,
                          "destinationMintTxHash": arc_hash}]
    receipt = {
        "transactionHash": arc_hash, "status": "0x1", "to": p.TRANSMITTER, "from": WALLET,
        "blockNumber": hex(700), "blockHash": block_hash,
        "logs": [{
            "address": p.TRANSMITTER,
            "topics": [p.RECEIVED_TOPIC, "0x" + word_address(WALLET).hex(), message.nonce,
                       "0x" + (2000).to_bytes(32, "big").hex()],
            "data": "0x" + encode(["uint32", "bytes32", "bytes"],
                                  [6, word_address(p.MESSENGER), bytes.fromhex(raw[2:])[148:]]).hex(),
        }],
    }
    original = app.arc.call

    async def historical_arc(method, params=None):
        if method == "eth_getTransactionReceipt":
            assert params == [arc_hash]
            return receipt
        if method == "eth_getBlockByNumber":
            assert params == [hex(700), False]
            return {"hash": "0x" + "ff" * 32 if failure == "wrong_block_hash" else block_hash,
                    "number": hex(700), "transactions": [arc_hash]}
        if method == "eth_call":
            payload, block = params
            if payload["data"] == p.used_nonce_calldata(message.nonce):
                assert block in {hex(699), hex(700)}
                return hex(int(block == hex(700) or failure == "already_used"))
            assert block == hex(699) and payload["from"] == WALLET
            assert payload["data"] == p.receive_calldata(raw, signature)
            return "0x0" if failure == "failed_simulation" else "0x1"
        return await original(method, params)

    app.arc.call = historical_arc
    if failure:
        with pytest.raises(ValueError, match="historical nonce|mint simulation|canonical Arc|verified receipt"):
            await app.replay_mint(source_hash, 3)
    else:
        replay = await app.replay_mint(source_hash, 3)
        assert replay["previous_block"] == 699 and replay["mint_block"] == 700
        assert not replay["nonce_used_before"] and replay["nonce_used_at_mint"]
        assert replay["receive_simulated"] and replay["arc_tx_hash"] == arc_hash


@pytest.mark.parametrize("used,expected_state", [(False, "managed_delivery"), (True, "completed")])
async def test_forwarded_app_hook_is_inspected_without_preparing_recovery(setup_service, used, expected_state):
    app, state = setup_service
    forward = b"cctp-forward".ljust(24, b"\0") + encode(["uint32"], [1])[-4:] + b"\0" * 4
    extra = b"circle-generic-executor".ljust(24, b"\0") + bytes.fromhex("0000000100000004") + b"data"
    hook = forward + extra
    state["logs"] = [burn_log(hook=hook)]
    state["messages"] = [{"status": "complete", "message": message_hex(hook=hook),
                          "attestation": "0x" + "11" * 65, "forwardState": "COMPLETE"}]
    state["used"] = used

    row, = (await app.scan(WALLET, blocks=150))["transfers"]
    assert row["status"] == expected_state
    assert row["delivery_mode"] == "forwarding_with_app_hook"
    assert row["forward_state"] == "COMPLETE"
    assert row["nonce_used"] is used
    with pytest.raises(ValueError, match="delivery hook"):
        await app.prepare(row["tx_hash"], row["log_index"], WALLET)

    state["messages"][0]["message"] = message_hex(hook=hook + b"invalid")
    assert (await app.scan(WALLET, blocks=150))["transfers"][0]["status"] == "unsupported"


async def test_cached_candidates_survive_empty_new_window_but_are_revalidated(setup_service):
    app, state = setup_service
    await app.scan(WALLET, blocks=150)
    # A later provider log window contains no new burns; its receipt still has the old burn.
    original = app.base.call
    async def no_new_logs(method, params=None):
        if method == "eth_getLogs":
            return []
        return await original(method, params)
    app.base.call = no_new_logs
    state["used"] = True
    result = await app.scan(WALLET, blocks=150)
    assert result["transfers"][0]["status"] == "completed"


async def test_prepare_rejects_restricted_caller(setup_service):
    app, state = setup_service
    state["logs"] = [burn_log(caller=OTHER)]
    state["message"] = message_hex(caller=OTHER)
    with pytest.raises(ValueError, match="caller_required"):
        await app.prepare("0x" + "cd" * 32, 3, WALLET)


async def test_scan_range_is_bounded(setup_service):
    app, _ = setup_service
    with pytest.raises(ValueError):
        await app.scan(WALLET, blocks=50001)


async def test_provider_log_outside_requested_window_is_not_newly_attributed(setup_service):
    app, _ = setup_service
    result = await app.scan(WALLET, blocks=1)
    assert result["transfers"] == []


async def test_matching_message_can_follow_unrelated_message_in_same_burn_transaction(setup_service):
    app, state = setup_service
    state["messages"] = [
        {"status": "complete", "message": message_hex(recipient=OTHER), "attestation": "0x" + "11" * 65},
        {"status": "complete", "message": message_hex(), "attestation": "0x" + "11" * 65},
    ]
    assert (await app.scan(WALLET, blocks=150))["transfers"][0]["status"] == "ready"


async def test_ambiguous_matching_messages_are_not_prepared(setup_service):
    app, state = setup_service
    second = bytearray.fromhex(message_hex()[2:])
    second[12:44] = bytes.fromhex("bb" * 32)
    state["messages"] = [
        {"status": "complete", "message": message_hex(), "attestation": "0x" + "11" * 65},
        {"status": "complete", "message": "0x" + second.hex(), "attestation": "0x" + "11" * 65},
    ]
    with pytest.raises(ValueError, match="unique"):
        await app.prepare("0x" + "cd" * 32, 3, WALLET)


async def test_circle_source_hash_mismatch_is_not_ready(setup_service):
    app, state = setup_service
    state["source_hash"] = "0x" + "01" * 32
    assert (await app.scan(WALLET, blocks=150))["transfers"][0]["status"] == "unsupported"


@pytest.mark.parametrize("change", [{"base_chain": 1}, {"code": "0x"}])
async def test_source_network_or_missing_contract_fails_closed(setup_service, change):
    app, state = setup_service
    state.update(change)
    with pytest.raises(service.UpstreamError):
        await app.scan(WALLET, blocks=150)


async def test_wrong_destination_network_is_unavailable_not_ready(setup_service):
    app, state = setup_service
    state["arc_chain"] = 1
    assert (await app.scan(WALLET, blocks=150))["transfers"][0]["status"] == "unavailable"


async def test_unsuccessful_destination_simulation_blocks_prepare(setup_service):
    app, state = setup_service
    state["simulation"] = 0
    with pytest.raises(ValueError, match="simulation"):
        await app.prepare("0x" + "cd" * 32, 3, WALLET)


async def test_prepare_reports_arc_gas_in_native_usdc_units(setup_service):
    app, state = setup_service
    state["caller_balance"] = 40 * 10**12
    prepared = await app.prepare("0x" + "cd" * 32, 3, WALLET)
    assert prepared["fee"] == "0.0005"
    assert prepared["gas_quote"] == {
        "max_cost_usdc": "0.00024", "caller_balance_usdc": "0.00004", "shortfall_usdc": "0.0002",
    }


async def test_ingestion_is_only_discovery_and_duplicate_delivery_does_not_duplicate_transfers(setup_service):
    app, state = setup_service
    assert app.ingest_logs([burn_log(), burn_log()]) == 2
    state["canonical"] = False
    result = await app.scan(WALLET, blocks=1)
    assert len(result["transfers"]) == 1
    assert result["transfers"][0]["status"] == "source_changed"


async def test_provider_range_limit_is_subdivided_without_skipping_history(setup_service):
    app, state = setup_service
    state["range_limit"] = 100
    result = await app.scan(WALLET, blocks=200)
    assert result["from_block"] == 1
    assert result["to_block"] == 200
    assert len(result["transfers"]) == 1
    assert result["transfers"][0]["status"] == "ready"


async def test_read_rpc_rate_limit_retries_without_exposing_provider_body():
    attempts = 0
    def respond(request):
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            return httpx.Response(429, text="provider-private-token", headers={"Retry-After": "0"})
        return httpx.Response(200, json={"jsonrpc": "2.0", "id": 1, "result": "0x64"})
    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        assert await Rpc("https://base.test", "Base", client).call("eth_blockNumber") == "0x64"


async def test_broadcast_is_not_automatically_retried():
    attempts = 0
    def respond(request):
        nonlocal attempts
        attempts += 1
        return httpx.Response(429, text="private-token")
    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        with pytest.raises(UpstreamError):
            await Rpc("https://base.test", "Base", client).call("eth_sendRawTransaction", ["0x1234"])
    assert attempts == 1


@pytest.mark.parametrize("used", [False, True])
async def test_identical_burns_in_one_source_receipt_cannot_share_one_attestation(setup_service, used):
    app, state = setup_service
    state["logs"] = [burn_log(), burn_log() | {"logIndex": "0x4"}]
    state["used"] = used
    result = await app.scan(WALLET, blocks=200)
    assert len(result["transfers"]) == 2
    assert {row["status"] for row in result["transfers"]} == {"unsupported"}
    for index in (3, 4):
        with pytest.raises(ValueError, match="ambiguous"):
            await app.prepare("0x" + "cd" * 32, index, WALLET)


async def test_failed_scan_cancels_other_ranges_before_returning(setup_service):
    app, _ = setup_service
    original = app.base.call
    cancelled = asyncio.Event()
    first_read = asyncio.Event()

    async def failing(method, params=None):
        if method == "eth_blockNumber":
            return hex(2000)
        if method != "eth_getLogs":
            return await original(method, params)
        first = int(params[0]["fromBlock"], 16)
        if first == 0:
            first_read.set()
            return [burn_log()]
        if first == 1000:
            await first_read.wait()
            raise UpstreamError("Cannot read range")
        try:
            await asyncio.sleep(10)
        finally:
            cancelled.set()
        return []
    app.base.call = failing
    with pytest.raises(UpstreamError):
        await app.scan(WALLET, blocks=3000)
    assert cancelled.is_set()
    assert app.store.candidates(WALLET) == []

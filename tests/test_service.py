import asyncio
import json

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
                result = {"hash": "0x" + ("ef" if state["canonical"] else "aa") * 32}
            case "eth_getLogs":
                from rebound.protocol import BURN_TOPIC, MESSENGER, USDC
                assert params[0]["topics"] == [BURN_TOPIC, "0x" + "00" * 12 + USDC[2:]]
                assert params[0]["address"] == MESSENGER
                assert int(params[0]["toBlock"], 16) - int(params[0]["fromBlock"], 16) < 1000
                result = state["logs"]
            case "eth_getTransactionReceipt":
                result = {"status": "0x1", "blockNumber": "0x64", "blockHash": "0x" + "ef" * 32,
                          "transactionHash": "0x" + "cd" * 32, "logs": state["logs"]}
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


async def test_source_reorg_never_becomes_ready(setup_service):
    app, state = setup_service
    state["canonical"] = False
    result = await app.scan(WALLET, blocks=150)
    assert result["transfers"][0]["status"] == "source_changed"


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

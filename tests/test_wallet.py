import json
import re
import shutil
import subprocess
from types import SimpleNamespace

import flet as ft
import httpx
import pytest
from eth_abi import encode
from starlette.routing import Mount

from rebound import protocol as p
from rebound import ui, wallet_ui
from rebound.app import create_app
from rebound.wallet import render_adapter
from rebound.wallet_session import (
    WALLET_FIELDS,
    SessionConflict,
    SessionNotFound,
    WalletSessions,
    sessions_for,
    wallet_fee_caps,
)
from tests.test_protocol import OTHER, WALLET, message_hex, word_address
from tests.test_service import setup_service  # noqa: F401 - shared service fixture

TX = "0x" + "cd" * 32
LOG = 3
ARC_HASH = "0x" + "ae" * 32
BLOCK_HASH = "0x" + "bc" * 32
GWEI = 10**9
NODE = shutil.which("node")


def arc_receipt(variant: str = "matching") -> dict | None:
    if variant == "missing":
        return None
    raw = message_hex()
    message = p.decode_message(raw)
    body = bytes.fromhex(raw[2:])[148:]
    log = {"address": OTHER if variant == "wrong_log_contract" else p.TRANSMITTER, "removed": False,
           "transactionHash": ARC_HASH, "blockHash": BLOCK_HASH, "blockNumber": "0x2bc", "transactionIndex": "0x0",
           "topics": [p.RECEIVED_TOPIC, "0x" + word_address(WALLET).hex(),
                      "0x" + "ff" * 32 if variant == "wrong_nonce" else message.nonce,
                      "0x" + (2000).to_bytes(32, "big").hex()],
           "data": "0x" + encode(["uint32", "bytes32", "bytes"], [6, word_address(p.MESSENGER),
                                 b"wrong" if variant == "wrong_body" else body]).hex()}
    return {"status": "0x0" if variant == "reverted" else "0x1",
            "transactionHash": "0x" + "ad" * 32 if variant == "other_hash" else ARC_HASH,
            "blockNumber": "0x2bc", "blockHash": BLOCK_HASH, "transactionIndex": "0x0",
            "from": OTHER if variant == "wrong_sender" else WALLET,
            "to": OTHER if variant == "wrong_contract" else p.TRANSMITTER,
            "gasUsed": hex(150_000), "effectiveGasPrice": hex(25 * GWEI), "logs": [log]}


@pytest.fixture
def wallet_service(setup_service):  # noqa: F811
    service, state = setup_service
    state.update(base_fee=20 * GWEI, tip=5 * GWEI, arc_receipt="missing", arc_block_hash=BLOCK_HASH,
                 prepare_callers=[])
    arc_call, prepare = service.arc.call, service.prepare

    async def fake_arc(method, params=None):
        if method == "eth_maxPriorityFeePerGas":
            return hex(state["tip"])
        if method == "eth_getBlockByNumber" and params[0] == "latest":
            return {"hash": "0x" + "ef" * 32, "baseFeePerGas": hex(state["base_fee"])}
        if method == "eth_getBlockByNumber" and params[0] == "0x2bc":
            return {"hash": state["arc_block_hash"], "number": "0x2bc", "transactions": [ARC_HASH]}
        if method == "eth_getTransactionReceipt" and params == [ARC_HASH]:
            return arc_receipt(state["arc_receipt"])
        if (method == "eth_call" and not params[0]["data"].startswith("0x57ecfd28")
                and state["arc_receipt"] not in ("missing", "reverted")):
            return "0x" + (1).to_bytes(32, "big").hex()  # A mined mint consumed the nonce.
        return await arc_call(method, params)

    async def recording_prepare(tx_hash, log_index, caller):
        state["prepare_callers"].append(caller)
        return await prepare(tx_hash, log_index, caller)

    service.arc.call = fake_arc
    service.prepare = recording_prepare
    return service, state


class Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


async def reviewed(manager, account=WALLET):
    session = manager.create(TX, LOG)
    await manager.connect(session.id, account, "0x4cef52")
    assert session.state == "reviewed", session.error
    return session


async def submitted(manager):
    session = await reviewed(manager)
    await manager.approve(session.id)
    assert session.state == "approved", session.error
    transaction = manager.claim(session.id)
    manager.report(session.id, "submitted", tx_hash=ARC_HASH)
    return session, transaction


# Session lifecycle -------------------------------------------------------------

async def test_full_session_confirms_only_after_the_canonical_receipt(wallet_service):
    service, state = wallet_service
    manager = WalletSessions(service)
    session, transaction = await submitted(manager)
    assert session.state == "submitted" and manager.holder(TX, LOG) is session
    await manager.verify(session.id)  # No receipt yet.
    assert session.state == "submitted" and session.receipt is None
    state["arc_receipt"] = "matching"
    await manager.verify(session.id)
    assert session.state == "confirmed"
    assert session.receipt == {"status": "confirmed", "tx_hash": ARC_HASH, "block": 700,
                               "url": f"{p.ARC_EXPLORER}/tx/{ARC_HASH}", "fee_paid_usdc": "0.00375",
                               "nonce": "0x" + "ab" * 32}
    assert manager.holder(TX, LOG) is None
    burn = p.decode_burn(state["logs"][0])
    assert service.store.local_mint(burn, "0x" + "ab" * 32) == ARC_HASH  # Inbox now links the receipt.


async def test_connect_prepares_from_the_actual_connected_caller(wallet_service):
    service, state = wallet_service
    manager = WalletSessions(service)
    session = await reviewed(manager, OTHER.upper().replace("0X", "0x"))
    assert state["prepare_callers"] == [OTHER]
    assert session.account == OTHER and session.transaction["from"] == OTHER
    await manager.connect(session.id, WALLET, "0x4cef52")  # Account switch re-reviews.
    assert state["prepare_callers"] == [OTHER, WALLET] and session.transaction["from"] == WALLET


async def test_transaction_carries_fee_caps_and_the_displayed_maximum_is_gas_times_cap(wallet_service):
    service, _ = wallet_service
    session = await reviewed(WalletSessions(service))
    tx, review = session.transaction, session.review
    assert tuple(tx) == WALLET_FIELDS
    assert tx["to"] == p.TRANSMITTER and tx["value"] == "0x0" and tx["chainId"] == "0x4cef52"
    assert tx["gas"] == hex(240_000)  # 200,000 estimate plus the service's 20% margin.
    assert tx["maxFeePerGas"] == hex(45 * GWEI) and tx["maxPriorityFeePerGas"] == hex(5 * GWEI)
    maximum = int(tx["gas"], 16) * int(tx["maxFeePerGas"], 16)
    assert review["max_network_fee_usdc"] == p.usdc(maximum, decimals=18) == "0.0108"
    assert (review["gas_limit"], review["max_fee_per_gas"]) == (240_000, 45 * GWEI)
    assert review["recipient"] == WALLET and review["net_amount"] == "128.4995"
    assert review["shortfall_usdc"] == "0"


def test_fee_caps_are_bounded():
    assert wallet_fee_caps(240_000, 20 * GWEI, 5 * GWEI) == (45 * GWEI, 5 * GWEI)
    with pytest.raises(ValueError, match="0.1 testnet USDC"):
        wallet_fee_caps(2_000_000, 100 * GWEI, 0)
    with pytest.raises(ValueError, match="gas limit"):
        wallet_fee_caps(2_400_001, GWEI, 0)


async def test_failed_review_and_shortfall_cannot_be_approved(wallet_service):
    service, state = wallet_service
    manager = WalletSessions(service)
    state["base_fee"] = 1000 * GWEI
    session = manager.create(TX, LOG)
    await manager.connect(session.id, WALLET, "0x4cef52")
    assert session.state == "connected" and "0.1 testnet USDC" in session.error
    with pytest.raises(SessionConflict):
        await manager.approve(session.id)
    state.update(base_fee=20 * GWEI, caller_balance=10**15)
    await manager.review(session.id)
    assert session.state == "reviewed" and session.review["shortfall_usdc"] == "0.0098"
    with pytest.raises(SessionConflict, match="maximum network fee"):
        await manager.approve(session.id)
    assert manager.holder(TX, LOG) is None


async def test_invalid_transitions_are_refused(wallet_service):
    service, _ = wallet_service
    manager = WalletSessions(service)
    session = manager.create(TX, LOG)
    with pytest.raises(ValueError, match="Arc Testnet"):
        await manager.connect(session.id, WALLET, "0x1")
    for step in (lambda: manager.claim(session.id),
                 lambda: manager.report(session.id, "submitted", tx_hash=ARC_HASH),
                 lambda: manager.release(session.id)):
        with pytest.raises(SessionConflict):
            step()
    with pytest.raises(SessionConflict):
        await manager.approve(session.id)
    await manager.connect(session.id, WALLET, "0x4cef52")
    with pytest.raises(SessionConflict):
        manager.claim(session.id)  # Reviewed is not approved.
    await manager.approve(session.id)
    with pytest.raises(SessionConflict):
        await manager.connect(session.id, OTHER, "0x4cef52")  # No account change after approval.
    manager.claim(session.id)
    with pytest.raises(SessionConflict):
        manager.claim(session.id)  # The transaction is handed out once.
    with pytest.raises(ValueError):
        manager.report(session.id, "maybe")
    with pytest.raises(SessionNotFound):
        manager.get("not-a-session")


async def test_approval_rechecks_the_transfer_and_releases_on_change(wallet_service):
    service, state = wallet_service
    manager = WalletSessions(service)
    session = await reviewed(manager)
    state["used"] = True  # Someone else completed it after review.
    await manager.approve(session.id)
    assert session.state == "reviewed" and "completed" in session.error
    assert manager.holder(TX, LOG) is None


async def test_a_second_session_for_the_same_transfer_cannot_approve_until_the_first_resolves(wallet_service):
    service, state = wallet_service
    manager = WalletSessions(service)
    first, second = await reviewed(manager), await reviewed(manager)
    await manager.approve(first.id)
    with pytest.raises(SessionConflict, match="Another wallet session"):
        await manager.approve(second.id)
    manager.claim(first.id)
    manager.report(first.id, "submitted", tx_hash=ARC_HASH)
    with pytest.raises(SessionConflict):
        await manager.approve(second.id)  # Pending receipt still holds the transfer.
    assert second.state == "reviewed"
    state["arc_receipt"] = "reverted"
    await manager.approve(second.id)  # Resolving the first hash releases the reservation.
    assert first.state == "failed" and "reverted" in first.error
    assert second.state == "approved" and manager.holder(TX, LOG) is second


async def test_wallet_rejection_frees_the_transfer_and_returns_to_review(wallet_service):
    service, _ = wallet_service
    manager = WalletSessions(service)
    session = await reviewed(manager)
    await manager.approve(session.id)
    manager.claim(session.id)
    manager.report(session.id, "rejected")
    assert session.state == "reviewed" and "Nothing was sent" in session.notice
    assert manager.holder(TX, LOG) is None
    await manager.approve(session.id)
    assert session.state == "approved"


async def test_unknown_outcome_holds_the_transfer_until_a_hash_or_explicit_release(wallet_service):
    service, state = wallet_service
    manager = WalletSessions(service)
    session, other = await reviewed(manager), await reviewed(manager)
    await manager.approve(session.id)
    manager.claim(session.id)
    manager.report(session.id, "unknown", code=-32603)
    assert session.state == "unknown" and "Do not send again" in session.error
    with pytest.raises(SessionConflict):
        await manager.approve(other.id)
    manager.record_hash(session.id, ARC_HASH)
    state["arc_receipt"] = "matching"
    await manager.verify(session.id)
    assert session.state == "confirmed"

    assert session.receipt["url"] == f"{p.ARC_EXPLORER}/tx/{ARC_HASH}"

    state["arc_receipt"] = "missing"  # A fresh, unfinished copy of the transfer.
    third_manager = WalletSessions(service)
    third = await reviewed(third_manager)
    await third_manager.approve(third.id)
    third_manager.claim(third.id)
    third_manager.report(third.id, "not_sent", reason="the chain is not Arc Testnet.")
    assert third.state == "failed" and "Nothing was sent" in third.error
    assert third_manager.holder(TX, LOG) is None


async def test_unclaimed_approval_lapses_because_nothing_reached_the_wallet(wallet_service):
    service, _ = wallet_service
    clock = Clock()
    manager = WalletSessions(service, clock=clock, pickup=90)
    session = await reviewed(manager)
    await manager.approve(session.id)
    clock.now += 91
    assert manager.get(session.id).state == "reviewed"
    assert "Nothing was sent" in session.notice and manager.holder(TX, LOG) is None
    with pytest.raises(SessionConflict):
        manager.claim(session.id)


async def test_sessions_expire_and_are_bounded_but_reservations_are_kept(wallet_service):
    service, _ = wallet_service
    clock = Clock()
    manager = WalletSessions(service, clock=clock, ttl=900, limit=2)
    held = await reviewed(manager)
    await manager.approve(held.id)
    manager.claim(held.id)  # Handed to the wallet: the outcome is not known yet.
    idle = manager.create(TX, LOG)
    assert len(idle.id) == 22  # 128 random bits, URL-safe.
    with pytest.raises(SessionConflict, match="Too many"):
        manager.create(TX, LOG)
    clock.now += 901
    with pytest.raises(SessionNotFound):
        manager.get(idle.id)
    assert manager.get(held.id) is held  # A reservation never silently expires.
    manager.create(TX, LOG)


# Exact-hash binding -------------------------------------------------------------

@pytest.mark.parametrize("variant, expected", [
    ("wrong_nonce", "did not accept it"), ("wrong_body", "did not accept it"),
    ("wrong_log_contract", "did not accept it"), ("non_canonical", "did not accept it"),
    ("wrong_contract", "MessageTransmitterV2"), ("wrong_sender", "different account"),
    ("other_hash", "different transaction"),
])
async def test_successful_but_unrelated_receipts_are_never_shown_as_delivery(wallet_service, variant, expected):
    service, state = wallet_service
    manager = WalletSessions(service)
    session, _ = await submitted(manager)
    if variant == "non_canonical":
        state.update(arc_receipt="matching", arc_block_hash="0x" + "ff" * 32)
    else:
        state["arc_receipt"] = variant
    await manager.verify(session.id)
    assert session.state == "unrelated" and session.receipt is None
    assert expected in session.error and "does not prove delivery" in session.error
    assert manager.holder(TX, LOG) is session  # The real send may still be pending.
    burn = p.decode_burn(state["logs"][0])
    assert service.store.local_mint(burn, "0x" + "ab" * 32) is None


async def test_reverted_receipt_fails_and_frees_the_transfer(wallet_service):
    service, state = wallet_service
    manager = WalletSessions(service)
    session, _ = await submitted(manager)
    state["arc_receipt"] = "reverted"
    await manager.verify(session.id)
    assert session.state == "failed" and session.receipt is None
    assert manager.holder(TX, LOG) is None


async def test_unrelated_hash_can_be_rechecked_after_the_block_becomes_canonical(wallet_service):
    service, state = wallet_service
    manager = WalletSessions(service)
    session, _ = await submitted(manager)
    state.update(arc_receipt="matching", arc_block_hash="0x" + "ff" * 32)
    await manager.verify(session.id)
    assert session.state == "unrelated"
    state["arc_block_hash"] = BLOCK_HASH
    await manager.verify(session.id)
    assert session.state == "confirmed"


# HTTP adapter ------------------------------------------------------------------

def client(app):
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")


def page_script(response):
    policy = response.headers["content-security-policy"]
    nonce = re.search(r"script-src 'nonce-([A-Za-z0-9_-]{16,})'", policy).group(1)
    return policy, nonce, re.findall(r"<script([^>]*)>(.*?)</script>", response.text, re.S)


async def test_adapter_page_is_minimal_and_strictly_isolated(wallet_service):
    service, _ = wallet_service
    session = sessions_for(service).create(TX, LOG)
    async with client(create_app(service, include_ui=False)) as http:
        first = await http.get(f"/wallet/adapter/{session.id}")
        second = await http.get(f"/wallet/adapter/{session.id}")
    assert first.status_code == 200 and first.headers["content-type"].startswith("text/html")
    for header, value in {"x-frame-options": "DENY", "cache-control": "no-store",
                          "referrer-policy": "no-referrer", "x-content-type-options": "nosniff"}.items():
        assert first.headers[header] == value
    policy, nonce, scripts = page_script(first)
    assert dict(item.strip().split(" ", 1) for item in policy.split(";")) == {
        "default-src": "'none'", "script-src": f"'nonce-{nonce}'", "connect-src": "'self'",
        "frame-ancestors": "'none'", "base-uri": "'none'", "form-action": "'none'"}
    assert nonce != page_script(second)[1]
    assert [attributes for attributes, _ in scripts] == [f' nonce="{nonce}"']
    body = first.text
    assert "<style" not in body and " style=" not in body and "<link" not in body and "<img" not in body
    assert "<form" not in body and "<input" not in body and "<button" not in body
    assert not re.search(r"""(src|href)=["']?(https?:)?//""", body)
    for sink in ("innerHTML", "outerHTML", "insertAdjacentHTML", "document.write", "eval(", "new Function"):
        assert sink not in body
    for method in ("eth_sign\"", "personal_sign", "eth_signTypedData", "eth_sendRawTransaction"):
        assert method not in body
    assert re.findall(r"<p[^>]*>(.*?)</p>", body) == [
        "Rebound is connecting to your wallet. Return to the Rebound tab to review."]
    config = json.loads(re.search(r"const CONFIG = (\{.*?\});", scripts[0][1]).group(1))
    assert config == {"session": session.id, "transmitter": p.TRANSMITTER, "chainHex": "0x4cef52",
                      "selector": "0x57ecfd28", "rpc": "https://rpc.testnet.arc.network",
                      "explorer": p.ARC_EXPLORER, "maxGas": "2400000", "maxNetworkFee": str(10**17)}


async def test_unknown_or_malformed_sessions_are_not_served(wallet_service):
    service, _ = wallet_service
    async with client(create_app(service, include_ui=False)) as http:
        for path in ("/wallet/adapter/" + "a" * 22, "/wallet/adapter/short", "/api/wallet-sessions/" + "b" * 22):
            response = await http.get(path)
            assert response.status_code == 404 and "content-security-policy" not in response.headers


async def test_http_flow_binds_the_claimed_transaction_to_the_reviewed_one(wallet_service):
    service, state = wallet_service
    manager = sessions_for(service)
    session = manager.create(TX, LOG)
    api = f"/api/wallet-sessions/{session.id}"
    async with client(create_app(service, include_ui=False)) as http:
        assert (await http.post(f"{api}/connected", json={"account": WALLET, "chainId": "0x1"})).status_code == 400
        connected = await http.post(f"{api}/connected", json={"account": WALLET, "chainId": "0x4cef52"})
        assert connected.json() == {"state": "reviewed", "account": WALLET, "chain_id": "0x4cef52"}
        assert (await http.post(f"{api}/claim")).status_code == 409  # Flet has not approved it.
        await manager.approve(session.id)
        polled = await http.get(api)
        assert polled.json()["state"] == "approved" and "transaction" not in polled.json()
        claimed = await http.post(f"{api}/claim")
        assert claimed.status_code == 200 and claimed.json() == session.transaction
        assert claimed.headers["cache-control"] == "no-store"
        assert (await http.post(f"{api}/claim")).status_code == 409
        bad = await http.post(f"{api}/result", json={"outcome": "submitted", "hash": "0x1234"})
        assert bad.status_code == 422
        result = await http.post(f"{api}/result", json={"outcome": "submitted", "hash": ARC_HASH.upper().replace("0X", "0x")})
        assert result.json()["state"] == "submitted" and session.hash == ARC_HASH
        again = await http.post(f"{api}/result", json={"outcome": "submitted", "hash": ARC_HASH})
        assert again.status_code == 200  # Idempotent retry.
        assert (await http.post(f"{api}/result", json={"outcome": "rejected"})).status_code == 409
    state["arc_receipt"] = "matching"
    await manager.verify(session.id)
    assert session.state == "confirmed"


async def test_adapter_routes_precede_the_flet_catch_all_mount(wallet_service):
    service, _ = wallet_service
    app = create_app(service)
    assert isinstance(app.router.routes[-1], Mount)
    session = sessions_for(service).create(TX, LOG)
    async with client(app) as http:
        response = await http.get(f"/wallet/adapter/{session.id}")
    assert response.status_code == 200 and "content-security-policy" in response.headers
    assert "<title>Rebound wallet connection</title>" in response.text


# Adapter JavaScript ------------------------------------------------------------

HARNESS = r"""
const vm = require("vm");
const fs = require("fs");
const [script, scenario] = [fs.readFileSync(process.argv[2], "utf8"), JSON.parse(process.argv[3])];
const calls = [], posts = [];
let state = "created";
const status = { textContent: "" };
const provider = {
  async request({ method, params }) {
    calls.push({ method, params });
    if (method === "eth_requestAccounts" || method === "eth_accounts") return [scenario.account];
    if (method === "eth_chainId") return "0x4cef52";
    if (method === "eth_sendTransaction") {
      if (scenario.reject) { const error = new Error("rejected"); error.code = 4001; throw error; }
      return "0x" + "ae".repeat(32);
    }
    throw new Error("unexpected " + method);
  },
  on() {},
};
async function fetch(url, options) {
  const path = url.split(scenario.session)[1];
  const body = options.body ? JSON.parse(options.body) : null;
  if (options.method === "POST") posts.push({ path, body });
  let data = {};
  if (path === "/connected") { state = "approved"; data = { state }; }
  else if (path === "/claim") { state = "sending"; data = scenario.transaction; }
  else if (path === "/result") { state = body.outcome === "rejected" ? "done" : body.outcome; data = { state }; }
  else data = { state };
  return { ok: true, status: 200, json: async () => data };
}
const context = { CONFIG: scenario.config, document: { getElementById: () => status },
  window: { ethereum: provider, addEventListener() {} }, fetch, setTimeout, BigInt, Promise, JSON, Number, String,
  Array, Object, Error };
vm.runInNewContext(script, context);
setTimeout(() => {
  console.log(JSON.stringify({ calls, posts, status: status.textContent }));
  process.exit(0);
}, scenario.wait);
"""


def run_adapter(tmp_path, transaction, *, reject=False):
    session = "s" * 22
    page = render_adapter(session, "nonce")
    script = re.search(r"<script nonce=\"nonce\">(.*?)</script>", page, re.S).group(1)
    config = json.loads(re.search(r"const CONFIG = (\{.*?\});", script).group(1))
    source, harness = tmp_path / "adapter.js", tmp_path / "harness.js"
    source.write_text(script.split("\n", 1)[1], encoding="utf-8")
    harness.write_text(HARNESS, encoding="utf-8")
    scenario = {"session": session, "config": config, "account": WALLET, "transaction": transaction,
                "reject": reject, "wait": 400}
    result = subprocess.run([NODE, str(harness), str(source), json.dumps(scenario)],
                            capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


def stored_transaction():
    return {"from": WALLET, "to": p.TRANSMITTER, "data": "0x57ecfd28" + "00" * 64, "value": "0x0",
            "gas": hex(240_000), "maxFeePerGas": hex(45 * GWEI), "maxPriorityFeePerGas": hex(5 * GWEI),
            "chainId": "0x4cef52"}


@pytest.mark.skipif(NODE is None, reason="node is not installed")
def test_adapter_script_is_valid_javascript(tmp_path):
    script = re.search(r"<script nonce=\"n\">(.*?)</script>", render_adapter("s" * 22, "n"), re.S).group(1)
    source = tmp_path / "adapter.js"
    source.write_text(script, encoding="utf-8")
    result = subprocess.run([NODE, "--check", str(source)], capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stderr


@pytest.mark.skipif(NODE is None, reason="node is not installed")
def test_adapter_sends_the_stored_transaction_exactly_once(tmp_path):
    transaction = stored_transaction()
    run = run_adapter(tmp_path, transaction)
    sends = [call for call in run["calls"] if call["method"] == "eth_sendTransaction"]
    assert sends == [{"method": "eth_sendTransaction", "params": [transaction]}]
    assert [post["path"] for post in run["posts"]] == ["/connected", "/claim", "/result"]
    assert run["posts"][0]["body"] == {"account": WALLET, "chainId": "0x4cef52"}
    assert run["posts"][2]["body"] == {"outcome": "submitted", "hash": ARC_HASH}


@pytest.mark.skipif(NODE is None, reason="node is not installed")
@pytest.mark.parametrize("change", [
    {"to": OTHER}, {"from": OTHER}, {"value": "0x1"}, {"chainId": "0x1"}, {"data": "0xa9059cbb" + "00" * 64},
    {"maxFeePerGas": hex(10**15)}, {"gas": hex(3_000_000)}, {"maxPriorityFeePerGas": hex(50 * GWEI)},
    {"gasPrice": hex(GWEI)},
])
def test_adapter_refuses_any_transaction_that_differs_from_the_reviewed_shape(tmp_path, change):
    run = run_adapter(tmp_path, stored_transaction() | change)
    assert not [call for call in run["calls"] if call["method"] == "eth_sendTransaction"]
    assert run["posts"][-1]["path"] == "/result" and run["posts"][-1]["body"]["outcome"] == "not_sent"
    assert run["status"] == "Nothing was sent. Return to the Rebound tab."


@pytest.mark.skipif(NODE is None, reason="node is not installed")
def test_adapter_reports_a_wallet_rejection_as_nothing_sent(tmp_path):
    run = run_adapter(tmp_path, stored_transaction(), reject=True)
    assert len([call for call in run["calls"] if call["method"] == "eth_sendTransaction"]) == 1
    assert run["posts"][-1]["body"] == {"outcome": "rejected"}


# Flet panel --------------------------------------------------------------------

class Page:
    url = "https://rebound.example/"
    width = 1200

    def __init__(self):
        self.dialogs, self.tasks = [], []

    def show_dialog(self, dialog):
        self.dialogs.append(dialog)

    def pop_dialog(self):
        self.dialogs.pop()

    def run_task(self, handler, *args):
        self.tasks.append(handler)

    def update(self):
        pass


def texts(control) -> list[str]:
    found = []
    if isinstance(control, ft.Text):
        found.append(control.value)
    content = getattr(control, "content", None)
    if isinstance(content, str):
        found.append(content)
    elif content is not None:
        found.extend(texts(content))
    for child in getattr(control, "controls", None) or []:
        found.extend(texts(child))
    return found


def buttons(control) -> list:
    found = [control] if isinstance(control, (ft.Button, ft.OutlinedButton, ft.TextButton)) else []
    content = getattr(control, "content", None)
    if content is not None and not isinstance(content, str):
        found.extend(buttons(content))
    for child in getattr(control, "controls", None) or []:
        found.extend(buttons(child))
    return found


READY = {"key": f"{TX}:{LOG}", "tx_hash": TX, "log_index": LOG, "status": "ready", "example": False,
         "amount": "128.5", "recipient": WALLET}


def test_wallet_button_is_offered_only_for_live_ready_transfers():
    class Verifying:
        async def prepare(self, *_args):
            raise AssertionError("not called")

        async def inspect_transaction(self, *_args, **_kwargs):
            raise AssertionError("not called")

    inbox = ui.InboxUI(Page(), Verifying())
    button = wallet_ui.wallet_button(inbox, READY)
    assert button.content == "Finish with browser wallet"
    assert wallet_ui.wallet_button(inbox, READY | {"example": True}) is None
    assert wallet_ui.wallet_button(inbox, READY | {"key": "example-ready"}) is None
    for status in ("completed", "caller_required", "awaiting_attestation", "expired", "unsupported"):
        assert wallet_ui.wallet_button(inbox, READY | {"status": status}) is None
    inbox.state.example = True
    assert wallet_ui.wallet_button(inbox, READY) is None
    assert wallet_ui.wallet_button(ui.InboxUI(Page(), object()), READY) is None  # Cannot prepare or verify.
    no_url = ui.InboxUI(SimpleNamespace(url="", width=1200), Verifying())
    assert wallet_ui.wallet_button(no_url, READY) is None


async def test_flet_panel_reviews_sends_and_shows_only_the_verified_receipt(wallet_service):
    service, state = wallet_service
    page = Page()
    inbox = ui.InboxUI(page, service)
    panel = wallet_ui.WalletPanel(inbox, READY)
    panel.open()
    assert page.tasks == [panel.poll]
    session = panel.session
    connect, = [button for button in buttons(panel.dialog.content) if button.content == "Connect wallet"]
    assert connect.url.url == f"https://rebound.example/wallet/adapter/{session.id}"
    assert connect.url.target == ft.UrlTarget.BLANK

    await panel.sessions.connect(session.id, WALLET, "0x4cef52")
    panel.refresh()
    shown = texts(panel.dialog.content)
    tx = session.transaction
    maximum = p.usdc(int(tx["gas"], 16) * int(tx["maxFeePerGas"], 16), decimals=18)
    for expected in ("Fixed recipient", WALLET, "128.4995 USDC", "240,000 units", "45 gwei",
                     f"{maximum} testnet USDC", "Send from wallet",
                     "Your wallet may show a lower final fee; it cannot exceed this maximum unless you edit it in the wallet."):
        assert expected in shown, expected

    await panel.send()
    assert session.state == "approved"
    assert any("Confirm the transaction in your wallet" in text for text in texts(panel.dialog.content))
    panel.sessions.claim(session.id)
    panel.sessions.report(session.id, "submitted", tx_hash=ARC_HASH)
    state["arc_receipt"] = "wrong_nonce"
    await panel.verify()
    shown = texts(panel.dialog.content)
    assert "Open verified Arc receipt" not in shown
    assert any("does not prove delivery" in text for text in shown)
    state["arc_receipt"] = "matching"
    await panel.verify()
    shown = texts(panel.dialog.content)
    assert "Open verified Arc receipt" in shown and ARC_HASH in shown
    receipt_link, = [b for b in buttons(panel.dialog.content) if b.content == "Open verified Arc receipt"]
    assert receipt_link.url == f"{p.ARC_EXPLORER}/tx/{ARC_HASH}"
    panel.close()
    assert page.dialogs == [] and page.tasks[-1] == inbox.refresh_transfer


async def test_flet_panel_blocks_send_on_shortfall_and_links_the_faucet(wallet_service):
    service, state = wallet_service
    state["caller_balance"] = 10**15
    panel = wallet_ui.WalletPanel(ui.InboxUI(Page(), service), READY)
    panel.open()
    await panel.sessions.connect(panel.session.id, WALLET, "0x4cef52")
    panel.refresh()
    send, = [b for b in buttons(panel.dialog.content) if b.content == "Send from wallet"]
    faucet, = [b for b in buttons(panel.dialog.content) if b.content == "Open Circle testnet faucet"]
    assert send.disabled and faucet.url == "https://faucet.circle.com/"

import asyncio
import dataclasses
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
from rebound.wallet import SESSION_HEADER, render_adapter
from rebound.wallet_session import (
    SENDING_SILENCE_SECONDS,
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


def arc_transaction(state) -> dict | None:
    variant = state["arc_tx"]
    if variant == "missing":
        return None
    return {"hash": ARC_HASH, "from": OTHER if variant == "wrong_sender" else WALLET, "to": p.TRANSMITTER,
            "value": "0x0", "blockHash": BLOCK_HASH, "blockNumber": "0x2bc", "transactionIndex": "0x0",
            "input": "0x57ecfd28" + "00" * 64 if variant == "wrong_input" else state.get("sent_data", "0x")}


@pytest.fixture
def wallet_service(setup_service):  # noqa: F811
    service, state = setup_service
    service.settings = dataclasses.replace(service.settings, browser_wallet=True)
    state.update(base_fee=20 * GWEI, tip=5 * GWEI, arc_receipt="missing", arc_block_hash=BLOCK_HASH,
                 prepare_callers=[], arc_head=0x2bc - 50, arc_tx="matching")
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
        if method == "eth_blockNumber":
            return hex(state["arc_head"])
        if method == "eth_getTransactionByHash" and params == [ARC_HASH]:
            return arc_transaction(state)
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


async def approve(manager, session):
    """Approve exactly the review the user saw, as the Flet button does."""
    return await manager.approve(session.id, generation=session.generation,
                                 transaction=dict(session.transaction or {}))


async def reviewed(manager, account=WALLET):
    session = manager.create(TX, LOG)
    await manager.connect(session.id, account, "0x4cef52")
    assert session.state == "reviewed", session.error
    return session


async def submitted(manager):
    session = await reviewed(manager)
    await approve(manager, session)
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
        await approve(manager, session)
    state.update(base_fee=20 * GWEI, caller_balance=10**15)
    await manager.review(session.id)
    assert session.state == "reviewed" and session.review["shortfall_usdc"] == "0.0098"
    with pytest.raises(SessionConflict, match="maximum network fee"):
        await approve(manager, session)
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
        await approve(manager, session)
    await manager.connect(session.id, WALLET, "0x4cef52")
    with pytest.raises(SessionConflict):
        manager.claim(session.id)  # Reviewed is not approved.
    await approve(manager, session)
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
    await approve(manager, session)
    assert session.state == "reviewed" and "completed" in session.error
    assert manager.holder(TX, LOG) is None


async def test_a_second_session_for_the_same_transfer_cannot_approve_until_the_first_resolves(wallet_service):
    service, state = wallet_service
    manager = WalletSessions(service)
    first, second = await reviewed(manager), await reviewed(manager)
    await approve(manager, first)
    with pytest.raises(SessionConflict, match="Another wallet session"):
        await approve(manager, second)
    manager.claim(first.id)
    manager.report(first.id, "submitted", tx_hash=ARC_HASH)
    with pytest.raises(SessionConflict):
        await approve(manager, second)  # Pending receipt still holds the transfer.
    assert second.state == "reviewed"
    state.update(arc_receipt="reverted", sent_data=first.transaction["data"])
    await approve(manager, second)  # Resolving the first hash releases the reservation.
    assert first.state == "failed" and "reverted" in first.error
    assert second.state == "approved" and manager.holder(TX, LOG) is second


async def test_wallet_rejection_frees_the_transfer_and_returns_to_review(wallet_service):
    service, _ = wallet_service
    manager = WalletSessions(service)
    session = await reviewed(manager)
    await approve(manager, session)
    manager.claim(session.id)
    manager.report(session.id, "rejected")
    assert session.state == "reviewed" and "Nothing was sent" in session.notice
    assert manager.holder(TX, LOG) is None
    await approve(manager, session)
    assert session.state == "approved"


async def test_unknown_outcome_holds_the_transfer_until_a_hash_or_explicit_release(wallet_service):
    service, state = wallet_service
    manager = WalletSessions(service)
    session, other = await reviewed(manager), await reviewed(manager)
    await approve(manager, session)
    manager.claim(session.id)
    manager.report(session.id, "unknown", code=-32603)
    assert session.state == "unknown" and "Do not send again" in session.error
    with pytest.raises(SessionConflict):
        await approve(manager, other)
    manager.record_hash(session.id, ARC_HASH)
    state["arc_receipt"] = "matching"
    await manager.verify(session.id)
    assert session.state == "confirmed"

    assert session.receipt["url"] == f"{p.ARC_EXPLORER}/tx/{ARC_HASH}"

    state["arc_receipt"] = "missing"  # A fresh, unfinished copy of the transfer.
    third_manager = WalletSessions(service)
    third = await reviewed(third_manager)
    await approve(third_manager, third)
    third_manager.claim(third.id)
    third_manager.report(third.id, "not_sent", reason="the chain is not Arc Testnet.")
    assert third.state == "failed" and "Nothing was sent" in third.error
    assert third_manager.holder(TX, LOG) is None


async def test_unclaimed_approval_lapses_because_nothing_reached_the_wallet(wallet_service):
    service, _ = wallet_service
    clock = Clock()
    manager = WalletSessions(service, clock=clock, pickup=90)
    session = await reviewed(manager)
    await approve(manager, session)
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
    await approve(manager, held)
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


async def test_an_account_switch_while_approval_waits_on_an_earlier_hash_is_refused(wallet_service):
    service, state = wallet_service
    manager = WalletSessions(service)
    first, second = await reviewed(manager), await reviewed(manager)
    shown = (second.generation, dict(second.transaction))  # What the user is looking at.
    await approve(manager, first)
    first_tx = manager.claim(first.id)
    manager.report(first.id, "submitted", tx_hash=ARC_HASH)
    state.update(arc_receipt="reverted", sent_data=first_tx["data"])  # The earlier send resolves as failed.
    gate, arc = asyncio.Event(), service.arc.call

    async def slow_receipt(method, params=None):
        if method == "eth_getTransactionReceipt":
            await gate.wait()
        return await arc(method, params)
    service.arc.call = slow_receipt
    approval = asyncio.create_task(manager.approve(second.id, generation=shown[0], transaction=shown[1]))
    for _ in range(5):
        await asyncio.sleep(0)  # The approval is now waiting on the first session's receipt.
    await asyncio.wait_for(manager.connect(second.id, OTHER, "0x4cef52"), 5)  # Account switch meanwhile.
    gate.set()
    with pytest.raises(SessionConflict, match="review changed"):
        await asyncio.wait_for(approval, 5)
    assert first.state == "failed"  # The earlier attempt did resolve,
    assert second.state == "reviewed" and second.account == OTHER  # but the stale click approved nothing.
    assert manager.holder(TX, LOG) is None
    with pytest.raises(SessionConflict, match="review changed"):
        await manager.approve(second.id, generation=shown[0], transaction=shown[1])
    await approve(manager, second)  # The new review, once shown, can be approved.
    assert second.state == "approved" and second.transaction["from"] == OTHER


async def test_an_approval_must_name_the_exact_review_generation_and_transaction(wallet_service):
    service, _ = wallet_service
    manager = WalletSessions(service)
    session = await reviewed(manager)
    shown = (session.generation, dict(session.transaction))
    await manager.review(session.id)  # "Check again" produced a newer review.
    with pytest.raises(SessionConflict, match="review changed"):
        await manager.approve(session.id, generation=shown[0], transaction=shown[1])
    tampered = dict(session.transaction) | {"maxFeePerGas": hex(GWEI)}
    with pytest.raises(SessionConflict, match="review changed"):
        await manager.approve(session.id, generation=session.generation, transaction=tampered)
    assert session.state == "reviewed" and manager.holder(TX, LOG) is None


async def test_reopening_a_transfer_resumes_its_unresolved_attempt(wallet_service):
    service, _ = wallet_service
    manager = WalletSessions(service)
    session = await reviewed(manager)
    await approve(manager, session)
    manager.claim(session.id)
    manager.close(session.id)  # The dialog closed while the wallet was open.
    assert manager.open(TX, LOG, owned_session_id=session.id) is session
    assert manager.open(TX, LOG) is not session
    manager.report(session.id, "unknown")
    assert manager.open(TX, LOG, owned_session_id=session.id) is session
    manager.release(session.id)
    manager.close(session.id)
    assert manager.open(TX, LOG, owned_session_id=session.id) is not session


async def test_release_is_refused_while_a_wallet_confirmation_can_still_submit(wallet_service):
    service, _ = wallet_service
    clock = Clock()
    manager = WalletSessions(service, clock=clock)
    session = await reviewed(manager)
    await approve(manager, session)
    with pytest.raises(SessionConflict):
        manager.release(session.id)  # Approved: the tab may claim it any moment.
    manager.claim(session.id)
    with pytest.raises(SessionConflict):
        manager.release(session.id)  # Sending: the wallet may still submit.
    clock.now += SENDING_SILENCE_SECONDS - 1
    manager.touch(session)  # The tab's heartbeat while the wallet shows the confirmation.
    clock.now += SENDING_SILENCE_SECONDS - 1
    assert manager.get(session.id).state == "sending"
    clock.now += 2
    assert manager.get(session.id).state == "unknown" and "stopped answering" in session.error
    assert manager.holder(TX, LOG) is session
    manager.release(session.id)
    assert session.state == "failed" and manager.holder(TX, LOG) is None


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
    session, transaction = await submitted(manager)
    state.update(arc_receipt="reverted", sent_data=transaction["data"])
    await manager.verify(session.id)
    assert session.state == "failed" and session.receipt is None
    assert manager.holder(TX, LOG) is None


@pytest.mark.parametrize("change, expected", [
    ({"arc_block_hash": "0x" + "ff" * 32}, "not canonical"),
    ({"arc_head": 0x2bc}, "before this attempt was approved"),
    ({"arc_tx": "wrong_input"}, "approved transaction's calldata"),
    ({"arc_tx": "wrong_sender"}, "approved transaction's calldata"),
    ({"arc_tx": "missing"}, "approved transaction's calldata"),
])
async def test_a_revert_frees_the_transfer_only_when_it_is_provably_this_attempt(wallet_service, change, expected):
    service, state = wallet_service
    manager = WalletSessions(service)
    state.update({key: value for key, value in change.items() if key == "arc_head"})
    session, transaction = await submitted(manager)
    state.update(arc_receipt="reverted", sent_data=transaction["data"])
    state.update({key: value for key, value in change.items() if key != "arc_head"})
    await manager.verify(session.id)
    assert session.state == "unrelated" and expected in session.error
    assert manager.holder(TX, LOG) is session  # The approved send may still land.


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

def ui_settings():
    from rebound.service import Settings
    return Settings


def client(app):
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")


class LoggedPaths:
    """Records what uvicorn's access log prints for each request: the path and query string."""

    def __init__(self, app):
        self.app, self.lines = app, []

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http":
            query = scope["query_string"].decode()
            self.lines.append(f'{scope["method"]} {scope["path"]}' + (f"?{query}" if query else ""))
        await self.app(scope, receive, send)


def page_script(response):
    policy = response.headers["content-security-policy"]
    nonce = re.search(r"script-src 'nonce-([A-Za-z0-9_-]{16,})'", policy).group(1)
    return policy, nonce, re.findall(r"<script([^>]*)>(.*?)</script>", response.text, re.S)


async def test_adapter_page_is_minimal_and_strictly_isolated(wallet_service):
    service, _ = wallet_service
    async with client(create_app(service, include_ui=False)) as http:
        first = await http.get("/wallet/adapter")
        second = await http.get("/wallet/adapter")
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
    assert config == {"header": SESSION_HEADER, "transmitter": p.TRANSMITTER, "chainHex": "0x4cef52",
                      "selector": "0x57ecfd28", "rpc": "https://rpc.testnet.arc.network",
                      "explorer": p.ARC_EXPLORER, "maxGas": "2400000", "maxNetworkFee": str(10**17)}


async def test_unknown_used_or_malformed_codes_and_tokens_are_refused(wallet_service):
    service, _ = wallet_service
    session = sessions_for(service).create(TX, LOG)
    code = session.code
    async with client(create_app(service, include_ui=False)) as http:
        assert (await http.post("/api/wallet-session/exchange", json={"code": "a" * 22})).status_code == 404
        assert (await http.post("/api/wallet-session/exchange", json={"code": "short"})).status_code == 422
        assert (await http.post("/api/wallet-session/exchange", json={"code": code})).status_code == 200
        assert (await http.post("/api/wallet-session/exchange", json={"code": code})).status_code == 404  # One use.
        assert session.code != code  # A fresh code for "Open wallet tab again".
        for token in ("b" * 22, session.code):
            response = await http.get("/api/wallet-session", headers={SESSION_HEADER: f"Bearer {token}"})
            assert response.status_code == 404
        short = await http.get("/api/wallet-session", headers={SESSION_HEADER: "Bearer short"})
        assert short.status_code == 401
        assert (await http.get("/api/wallet-session")).status_code == 401
        for path in ("/wallet/adapter/" + session.id, f"/api/wallet-sessions/{session.id}"):
            assert (await http.get(path)).status_code == 404


async def test_http_flow_binds_the_claimed_transaction_and_keeps_secrets_out_of_logged_paths(wallet_service):
    service, state = wallet_service
    manager = sessions_for(service)
    session = manager.create(TX, LOG)
    code = session.code
    app = LoggedPaths(create_app(service, include_ui=False))
    async with client(app) as http:
        assert (await http.get("/wallet/adapter")).status_code == 200  # The code stays in the fragment.
        exchanged = await http.post("/api/wallet-session/exchange", json={"code": code})
        token = exchanged.json()["token"]
        assert token == session.id and exchanged.headers["cache-control"] == "no-store"
        auth = {SESSION_HEADER: f"Bearer {token}"}
        api = "/api/wallet-session"
        bad_chain = await http.post(f"{api}/connected", json={"account": WALLET, "chainId": "0x1"}, headers=auth)
        assert bad_chain.status_code == 400
        connected = await http.post(f"{api}/connected", json={"account": WALLET, "chainId": "0x4cef52"}, headers=auth)
        assert connected.json() == {"state": "reviewed", "account": WALLET, "chain_id": "0x4cef52"}
        assert (await http.post(f"{api}/claim", headers=auth)).status_code == 409  # Flet has not approved it.
        await approve(manager, session)
        polled = await http.get(api, headers=auth)
        assert polled.json()["state"] == "approved" and "transaction" not in polled.json()
        claimed = await http.post(f"{api}/claim", headers=auth)
        assert claimed.status_code == 200 and claimed.json() == session.transaction
        assert claimed.headers["cache-control"] == "no-store"
        assert (await http.post(f"{api}/claim", headers=auth)).status_code == 409
        bad = await http.post(f"{api}/result", json={"outcome": "submitted", "hash": "0x1234"}, headers=auth)
        assert bad.status_code == 422
        result = await http.post(f"{api}/result", headers=auth,
                                 json={"outcome": "submitted", "hash": ARC_HASH.upper().replace("0X", "0x")})
        assert result.json()["state"] == "submitted" and session.hash == ARC_HASH
        again = await http.post(f"{api}/result", json={"outcome": "submitted", "hash": ARC_HASH}, headers=auth)
        assert again.status_code == 200  # Idempotent retry.
        assert (await http.post(f"{api}/result", json={"outcome": "rejected"}, headers=auth)).status_code == 409
    assert len(app.lines) == 12
    for line in app.lines:
        assert token not in line and code not in line and session.code not in line, line
    assert {line.split(" ", 1)[1] for line in app.lines} == {
        "/wallet/adapter", f"{api}/exchange", api, f"{api}/connected", f"{api}/claim", f"{api}/result"}
    state["arc_receipt"] = "matching"
    await manager.verify(session.id)
    assert session.state == "confirmed"


async def test_adapter_routes_precede_the_flet_catch_all_mount_and_are_off_by_default(wallet_service, monkeypatch):
    service, _ = wallet_service
    app = create_app(service)
    assert isinstance(app.router.routes[-1], Mount)
    async with client(app) as http:
        response = await http.get("/wallet/adapter")
    assert response.status_code == 200 and "content-security-policy" in response.headers
    assert "<title>Rebound wallet connection</title>" in response.text
    service.settings = dataclasses.replace(service.settings, browser_wallet=False)
    async with client(create_app(service, include_ui=False)) as http:
        assert (await http.get("/wallet/adapter")).status_code == 404
        assert (await http.post("/api/wallet-session/exchange", json={"code": "a" * 22})).status_code == 404
        assert (await http.get("/api/health")).json()["signing"] == "local CLI only"
    monkeypatch.delenv("REBOUND_BROWSER_WALLET", raising=False)
    assert ui_settings().from_env().browser_wallet is False
    monkeypatch.setenv("REBOUND_BROWSER_WALLET", "1")
    assert ui_settings().from_env().browser_wallet is True


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
const history = [];
async function fetch(url, options) {
  const address = new URL(url);
  const path = address.pathname.slice("/api/wallet-session".length);
  const body = options.body ? JSON.parse(options.body) : null;
  const token = options.headers[scenario.config.header]?.replace(/^Bearer /, "") || null;
  if (options.method === "POST") posts.push({ path, body, token });
  if ((scenario.code && url.includes(scenario.code)) || (token && url.includes(token))) throw new Error("secret in URL");
  let data = {};
  if (path === "/exchange") data = body.code === scenario.code ? { token: scenario.session, state } : null;
  else if (token !== scenario.session) return { ok: false, status: 404, json: async () => ({ detail: "gone" }) };
  else if (path === "/connected") { state = "approved"; data = { state }; }
  else if (path === "/claim") { state = "sending"; data = scenario.transaction; }
  else if (path === "/result") { state = body.outcome === "rejected" ? "done" : body.outcome; data = { state }; }
  else data = { state };
  return { ok: true, status: 200, json: async () => data };
}
const context = { CONFIG: scenario.config, document: { getElementById: () => status },
  window: { ethereum: provider, addEventListener() {}, location: { hash: "#" + scenario.code,
    pathname: "/wallet/adapter", origin: "https://rebound.example", href: "https://rebound.example/wallet/adapter" },
    history: { replaceState: (...args) => history.push(args) } },
  fetch, setTimeout, setInterval, clearInterval, URL, BigInt, Promise, JSON, Number, String, Array, Object, Error };
vm.runInNewContext(script, context);
setTimeout(() => {
  console.log(JSON.stringify({ calls, posts, history, status: status.textContent }));
  process.exit(0);
}, scenario.wait);
"""


def run_adapter(tmp_path, transaction, *, reject=False, code="c" * 22):
    session = "s" * 22
    page = render_adapter("nonce")
    script = re.search(r"<script nonce=\"nonce\">(.*?)</script>", page, re.S).group(1)
    config = json.loads(re.search(r"const CONFIG = (\{.*?\});", script).group(1))
    source, harness = tmp_path / "adapter.js", tmp_path / "harness.js"
    source.write_text(script.split("\n", 1)[1], encoding="utf-8")
    harness.write_text(HARNESS, encoding="utf-8")
    scenario = {"session": session, "code": code, "config": config, "account": WALLET,
                "transaction": transaction, "reject": reject, "wait": 400}
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
    script = re.search(r"<script nonce=\"n\">(.*?)</script>", render_adapter("n"), re.S).group(1)
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
    assert [post["path"] for post in run["posts"]] == ["/exchange", "/connected", "/claim", "/result"]
    assert run["posts"][0] == {"path": "/exchange", "body": {"code": "c" * 22}, "token": None}
    assert all(post["token"] == "s" * 22 for post in run["posts"][1:])  # Header only, never the URL.
    assert run["history"] == [[None, "", "/wallet/adapter"]]  # The code leaves the address bar.
    assert run["posts"][1]["body"] == {"account": WALLET, "chainId": "0x4cef52"}
    assert run["posts"][3]["body"] == {"outcome": "submitted", "hash": ARC_HASH}


@pytest.mark.skipif(NODE is None, reason="node is not installed")
def test_adapter_stops_without_a_valid_one_time_code(tmp_path):
    for code in ("", "x" * 5):
        run = run_adapter(tmp_path, stored_transaction(), code=code)
        assert run["calls"] == [] and [post["path"] for post in run["posts"]] in ([], ["/exchange"])
        assert "Open it again from Rebound" in run["status"] or "already used" in run["status"]


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


def controls(control) -> list:
    found = [control]
    content = getattr(control, "content", None)
    if content is not None and not isinstance(content, str):
        found.extend(controls(content))
    for child in getattr(control, "controls", None) or []:
        found.extend(controls(child))
    return found


def buttons(control) -> list:
    found = [control] if isinstance(control, (ft.Button, ft.OutlinedButton, ft.TextButton)) else []
    content = getattr(control, "content", None)
    if content is not None and not isinstance(content, str):
        found.extend(buttons(content))
    for child in getattr(control, "controls", None) or []:
        found.extend(buttons(child))
    return found


class FakeClipboard:
    value = None

    async def set(self, value):
        self.value = value


READY = {"key": f"{TX}:{LOG}", "tx_hash": TX, "log_index": LOG, "status": "ready", "example": False,
         "amount": "128.5", "recipient": WALLET}


def test_wallet_button_is_offered_only_when_enabled_for_live_ready_transfers():
    class Verifying:
        settings = SimpleNamespace(browser_wallet=True)

        async def prepare(self, *_args):
            raise AssertionError("not called")

        async def inspect_transaction(self, *_args, **_kwargs):
            raise AssertionError("not called")

    inbox = ui.InboxUI(Page(), Verifying())
    button = wallet_ui.wallet_button(inbox, READY)
    assert button.content == "Finish with browser wallet" and button.icon is None
    assert isinstance(button, ft.FilledButton)  # The one primary action for a ready transfer.
    assert wallet_ui.wallet_button(inbox, READY | {"example": True}) is None
    assert wallet_ui.wallet_button(inbox, READY | {"key": "example-ready"}) is None
    for status in ("completed", "caller_required", "awaiting_attestation", "expired", "unsupported"):
        assert wallet_ui.wallet_button(inbox, READY | {"status": status}) is None
    inbox.state.example = True
    assert wallet_ui.wallet_button(inbox, READY) is None
    assert wallet_ui.wallet_button(ui.InboxUI(Page(), object()), READY) is None  # Cannot prepare or verify.
    no_url = ui.InboxUI(SimpleNamespace(url="", width=1200), Verifying())
    assert wallet_ui.wallet_button(no_url, READY) is None
    disabled = Verifying()
    disabled.settings = SimpleNamespace(browser_wallet=False)
    assert wallet_ui.wallet_button(ui.InboxUI(Page(), disabled), READY) is None  # Default: off.


def test_next_step_makes_the_wallet_the_single_primary_and_keeps_review_recovery(wallet_service):
    service, _ = wallet_service
    inbox = ui.InboxUI(Page(), service)
    shown = buttons(inbox.next_step(READY))
    primary = [b for b in shown if isinstance(b, ft.FilledButton)]
    assert [b.content for b in primary] == ["Finish with browser wallet"]
    assert "Review recovery" in [b.content for b in shown]
    service.settings = dataclasses.replace(service.settings, browser_wallet=False)
    primary = [b for b in buttons(inbox.next_step(READY)) if isinstance(b, ft.FilledButton)]
    assert [b.content for b in primary] == ["Review recovery"]


@pytest.mark.parametrize("wei, shown", [
    (0, "0"), (10 * GWEI, "10"), (50 * GWEI, "50"), (100 * GWEI, "100"), (1_500_000_000, "1.5"),
    (45 * GWEI + 1, "45.000000001"), (1, "0.000000001"), (1234 * GWEI, "1,234"),
])
def test_gwei_keeps_integer_zeroes_and_trims_only_fractional_zeroes(wei, shown):
    assert wallet_ui.gwei(wei) == shown


async def test_flet_panel_reviews_sends_and_shows_only_the_verified_receipt(wallet_service):
    service, state = wallet_service
    page = Page()
    inbox = ui.InboxUI(page, service)
    panel = wallet_ui.WalletPanel(inbox, READY)
    panel.open()
    assert page.tasks == [panel.poll]
    assert panel.dialog.bgcolor == ui.SURFACE and panel.dialog.shape.radius == 8
    session = panel.session
    connect, = [button for button in buttons(panel.dialog.content) if button.content == "Connect wallet"]
    assert connect.url.url == f"https://rebound.example/wallet/adapter#{session.code}"
    assert session.id not in connect.url.url and connect.url.target == ft.UrlTarget.BLANK
    assert connect.icon is None

    await panel.sessions.connect(session.id, WALLET, "0x4cef52")
    panel.refresh()
    shown = texts(panel.dialog.content)
    tx = session.transaction
    maximum = p.usdc(int(tx["gas"], 16) * int(tx["maxFeePerGas"], 16), decimals=18)
    for expected in ("Fixed recipient", WALLET, "128.5000", "0.0005", "128.4995", "240,000", "units", "45", "gwei",
                     maximum, "Send from wallet", "Maximum network fee",
                     "Your wallet may show a lower final fee; it cannot exceed this maximum unless you edit it in the wallet."):
        assert expected in shown, expected
    assert "Arc Testnet" not in shown  # No network pill.
    review_buttons = buttons(panel.dialog.content)
    assert [b.content for b in review_buttons if isinstance(b, ft.FilledButton)] == ["Send from wallet"]
    assert all(b.icon is None for b in review_buttons)

    send, = [b for b in review_buttons if b.content == "Send from wallet"]
    await send.on_click(None)
    assert session.state == "approved"
    assert any("Confirm the transaction in your wallet" in text for text in texts(panel.dialog.content))
    panel.sessions.claim(session.id)
    panel.refresh()
    assert panel.hash_input in controls(panel.dialog.content)  # Mounted while the wallet is open.
    assert "My wallet sent nothing" not in [b.content for b in buttons(panel.dialog.content)]
    panel.sessions.report(session.id, "submitted", tx_hash=ARC_HASH)
    state["arc_receipt"] = "wrong_nonce"
    await panel.verify()
    shown = texts(panel.dialog.content)
    assert "Open verified Arc receipt" not in shown and panel.hash_input in controls(panel.dialog.content)
    assert any("does not prove delivery" in text for text in shown)
    state["arc_receipt"] = "matching"
    await panel.verify()
    shown = texts(panel.dialog.content)
    assert "Transfer delivered" in shown and "Open verified Arc receipt" in shown
    truncated, = [c for c in controls(panel.dialog.content) if isinstance(c, ft.Text) and c.tooltip == ARC_HASH]
    assert truncated.value == ui.shorten(ARC_HASH, 8) and truncated.font_family == ui.MONO
    inbox.clipboard = FakeClipboard()
    copy, = [b for b in buttons(panel.dialog.content) if b.content == "Copy"]
    copy.on_click(None)
    await page.tasks[-1](ARC_HASH)
    assert inbox.clipboard.value == ARC_HASH
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


async def test_flet_send_carries_the_review_on_screen(wallet_service):
    service, _ = wallet_service
    panel = wallet_ui.WalletPanel(ui.InboxUI(Page(), service), READY)
    panel.open()
    await panel.sessions.connect(panel.session.id, WALLET, "0x4cef52")
    panel.refresh()  # The user sees the review for WALLET.
    old_send, = [b for b in buttons(panel.dialog.content) if b.content == "Send from wallet"]
    await panel.sessions.connect(panel.session.id, OTHER, "0x4cef52")  # The wallet tab switches account.
    panel.refresh()  # A new button has been produced, but the old event arrives late.
    await old_send.on_click(None)
    assert panel.session.state == "reviewed" and panel.session.account == OTHER
    assert "review changed" in panel.local_error and panel.sessions.holder(TX, LOG) is None


async def test_flet_panel_resumes_the_same_attempt_after_the_dialog_closes(wallet_service):
    service, _ = wallet_service
    page = Page()
    inbox = ui.InboxUI(page, service)
    first = wallet_ui.WalletPanel(inbox, READY)
    first.open()
    await first.sessions.connect(first.session.id, WALLET, "0x4cef52")
    first.refresh()
    send, = [b for b in buttons(first.dialog.content) if b.content == "Send from wallet"]
    await send.on_click(None)
    first.sessions.claim(first.session.id)
    first.close()
    second = wallet_ui.WalletPanel(inbox, READY)
    second.open()
    assert second.session is first.session and second.session.state == "sending"
    assert second.hash_input in controls(second.dialog.content)
    second.hash_input.value = ARC_HASH
    second.sessions.report(second.session.id, "unknown")
    second.refresh()  # A status update keeps the field and what was typed.
    assert second.hash_input in controls(second.dialog.content) and second.hash_input.value == ARC_HASH
    assert "My wallet sent nothing" in [b.content for b in buttons(second.dialog.content)]


async def test_flet_transfer_does_not_share_wallet_session_across_clients(wallet_service):
    service, _ = wallet_service
    first = wallet_ui.WalletPanel(ui.InboxUI(Page(), service), READY)
    first.open()
    await first.sessions.connect(first.session.id, WALLET, "0x4cef52")
    await approve(first.sessions, first.session)
    first.sessions.claim(first.session.id)
    first.sessions.report(first.session.id, "unknown")
    first.close()

    second = wallet_ui.WalletPanel(ui.InboxUI(Page(), service), READY)
    second.open()
    assert second.session.state == "created" and second.session.account is None
    assert second.session.code != first.session.code
    assert "My wallet sent nothing" not in [b.content for b in buttons(second.dialog.content)]
    assert second.sessions.holder(TX, LOG) is first.session

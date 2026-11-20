"""Fake-RPC regressions for owner-scoped attempts and authoritative failure evidence."""
# ruff: noqa: F811
import asyncio

import pytest

from rebound.clients import UpstreamError
from rebound.wallet_session import SessionConflict, SessionNotFound, WalletSessions
from tests.test_service import setup_service  # noqa: F401
from tests.test_wallet import (  # noqa: F401
    ARC_HASH,
    BLOCK_HASH,
    LOG,
    OTHER,
    TX,
    Clock,
    approve,
    reviewed,
    submitted,
    wallet_service,
)


async def test_resume_requires_the_owned_session_capability(wallet_service):
    service, _ = wallet_service
    manager = WalletSessions(service)
    original, _ = await submitted(manager)
    stranger = manager.open(TX, LOG)
    assert stranger is not original and stranger.account is None
    assert manager.open(TX, LOG, owned_session_id=original.id) is original
    assert manager.close(original.id) is False
    assert manager.close(stranger.id) is True
    assert manager.close(stranger.id) is True
    with pytest.raises(SessionConflict):
        manager.open("0x" + "12" * 32, LOG, owned_session_id=original.id)


async def test_expired_owned_capability_starts_a_distinct_session(wallet_service):
    service, _ = wallet_service
    manager = WalletSessions(service)
    old = manager.create(TX, LOG)
    assert manager.close(old.id)
    assert manager.open(TX, LOG, owned_session_id=old.id) is not old


async def test_cancelled_approval_releases_only_its_unclaimed_reservation(wallet_service):
    service, _ = wallet_service
    manager = WalletSessions(service)
    session = await reviewed(manager)
    entered = asyncio.Event()

    async def waiting_prepare(*args):
        entered.set()
        await asyncio.Future()

    service.prepare = waiting_prepare
    task = asyncio.create_task(approve(manager, session))
    await entered.wait()
    assert manager.close(session.id) is False
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert session.state == "reviewed" and manager.holder(TX, LOG) is None


async def test_cancelled_review_clears_reviewing(wallet_service):
    service, _ = wallet_service
    manager = WalletSessions(service)
    session = await reviewed(manager)
    entered = asyncio.Event()

    async def waiting_prepare(*args):
        entered.set()
        await asyncio.Future()

    service.prepare = waiting_prepare
    task = asyncio.create_task(manager.review(session.id))
    await entered.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert not session.reviewing


async def test_closed_session_cannot_be_approved_after_prior_holder_check(wallet_service):
    service, _ = wallet_service
    manager = WalletSessions(service)
    previous, _ = await submitted(manager)
    current = await reviewed(manager)
    entered, finish = asyncio.Event(), asyncio.Event()

    async def waiting_verify(session_id):
        assert session_id == previous.id
        entered.set()
        await finish.wait()
        manager._change(previous, "failed")
        return previous

    manager.verify = waiting_verify
    task = asyncio.create_task(approve(manager, current))
    await entered.wait()
    assert manager.close(current.id)
    finish.set()
    with pytest.raises(SessionNotFound):
        await task
    assert manager.holder(TX, LOG) is None


async def test_holder_check_lapses_abandoned_unclaimed_approval(wallet_service):
    service, _ = wallet_service
    clock = Clock()
    manager = WalletSessions(service, clock=clock)
    session = await reviewed(manager)
    await approve(manager, session)
    clock.now += 91
    assert manager.holder(TX, LOG) is None
    assert session.state == "reviewed"


async def revert_case(wallet_service, *, tx_patch=None, receipt_patch=None, block_patch=None,
                      wrong_chain=False, unavailable=False):
    service, state = wallet_service
    manager = WalletSessions(service)
    session, transaction = await submitted(manager)
    state.update(arc_receipt="reverted", sent_data=transaction["data"])
    original = service.arc.call

    async def fake_arc(method, params=None):
        if method == "eth_getTransactionByHash" and unavailable:
            raise UpstreamError("Arc is unavailable.")
        result = await original(method, params)
        if method == "eth_getTransactionByHash":
            return result | {"value": "0x0", "blockHash": BLOCK_HASH, "blockNumber": "0x2bc",
                             "transactionIndex": "0x0"} | (tx_patch or {})
        if method == "eth_getTransactionReceipt" and params == [ARC_HASH]:
            return result | (receipt_patch or {})
        if method == "eth_getBlockByNumber" and params[0] == "0x2bc":
            return result | (block_patch or {})
        if method == "eth_chainId" and wrong_chain:
            return "0x1"
        return result

    service.arc.call = fake_arc
    return manager, session


async def test_exact_canonical_adapter_reported_revert_releases(wallet_service):
    manager, session = await revert_case(wallet_service)
    await manager.verify(session.id)
    assert session.state == "failed" and manager.holder(TX, LOG) is None


@pytest.mark.parametrize("changes", [
    {"tx_patch": {"value": "0x1"}},
    {"tx_patch": {"blockHash": "0x" + "ff" * 32}},
    {"tx_patch": {"blockNumber": "0x2bd"}},
    {"tx_patch": {"transactionIndex": "0x1"}},
    {"tx_patch": {"from": OTHER}},
    {"tx_patch": {"input": "0x1234"}},
    {"receipt_patch": {"transactionIndex": "0x1"}},
    {"block_patch": {"number": "0x2bd"}},
    {"block_patch": {"transactions": []}},
    {"wrong_chain": True},
    {"unavailable": True},
])
async def test_incomplete_revert_evidence_retains_attempt(wallet_service, changes):
    manager, session = await revert_case(wallet_service, **changes)
    await manager.verify(session.id)
    assert session.state in {"submitted", "unrelated"}
    assert manager.holder(TX, LOG) is session


async def test_manually_entered_revert_does_not_resolve_the_wallet_request(wallet_service):
    manager, session = await revert_case(wallet_service)
    manager._change(session, "unknown")
    manager.record_hash(session.id, ARC_HASH)
    await manager.verify(session.id)
    assert session.state == "unrelated" and manager.holder(TX, LOG) is session

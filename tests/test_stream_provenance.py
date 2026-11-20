"""Persisted Stream provenance is scoped to one delivered block identity."""
import json
import sqlite3

import pytest

from rebound import protocol as p
from rebound import store
from tests.test_protocol import WALLET, burn_log


@pytest.fixture
def indexed(tmp_path, monkeypatch):
    clock = {"now": 1000}
    monkeypatch.setattr(store.time, "time", lambda: clock["now"])
    return store.CandidateStore(str(tmp_path / "provenance.sqlite3")), clock


def save(index, log, *, signed=False, stream=True):
    index.save_many([(p.decode_burn(log), log)], from_stream=stream, signed=signed)


def only(index):
    row, = index.candidates(WALLET, stream_only=True)
    return row


def test_changed_block_cannot_inherit_another_blocks_signature_or_time(indexed):
    index, clock = indexed
    original = burn_log()
    changed = original | {"blockHash": "0x" + "12" * 32}
    save(index, original, signed=True)
    clock["now"] = 2000
    save(index, changed)
    row = only(index)
    assert row.burn.block_hash == changed["blockHash"]
    assert row.signed is False and row.received_at == 2000
    assert index.stream_summary(WALLET) == {"signed": 0, "last_signed": None, "local": 1}
    clock["now"] = 3000
    save(index, original, signed=True)
    assert only(index).signed is True and only(index).received_at == 3000


def test_first_signed_time_replaces_unsigned_time_then_survives_retries(indexed):
    index, clock = indexed
    log = burn_log()
    save(index, log)
    clock["now"] = 2000
    save(index, log)
    assert only(index).received_at == 1000 and only(index).signed is False
    clock["now"] = 3000
    save(index, log, signed=True)
    assert only(index).received_at == 3000 and only(index).signed is True
    for signed in (False, True):
        clock["now"] += 1000
        save(index, log, signed=signed)
        assert only(index).received_at == 3000 and only(index).signed is True
    assert index.stream_summary(WALLET) == {"signed": 1, "last_signed": 3000, "local": 0}


def test_same_second_deliveries_do_not_confuse_authentication_or_block_identity(indexed):
    index, _ = indexed
    first = burn_log()
    save(index, first)
    save(index, first, signed=True)
    save(index, first)
    assert only(index).signed is True and only(index).received_at == 1000
    save(index, first | {"blockHash": "0x" + "12" * 32})
    assert only(index).signed is False and only(index).received_at == 1000


def test_rpc_scan_preserves_prior_stream_evidence_without_attributing_a_new_block(indexed):
    index, clock = indexed
    original = burn_log()
    changed = original | {"blockHash": "0x" + "12" * 32}
    save(index, original, signed=True)
    clock["now"] = 2000
    save(index, changed, stream=False)
    row, = index.candidates(WALLET)
    assert not row.from_stream and not row.signed and row.received_at is None
    assert index.candidates(WALLET, stream_only=True) == []
    assert index.stream_summary(WALLET) == {"signed": 0, "last_signed": None, "local": 0}
    save(index, original, stream=False)
    assert only(index).signed is True and only(index).received_at == 1000
    save(index, changed)
    assert only(index).signed is False and only(index).received_at == 2000


def test_legacy_migration_does_not_invent_signature_or_receipt_time(tmp_path, monkeypatch):
    path = str(tmp_path / "legacy.sqlite3")
    log = burn_log()
    burn = p.decode_burn(log)
    with sqlite3.connect(path) as db:
        db.execute("""CREATE TABLE candidates (
            tx_hash TEXT NOT NULL, log_index INTEGER NOT NULL,
            depositor TEXT NOT NULL, recipient TEXT NOT NULL,
            block_number INTEGER NOT NULL, payload TEXT NOT NULL, stream_block_hash TEXT,
            PRIMARY KEY(tx_hash, log_index))""")
        db.execute("INSERT INTO candidates VALUES (?, ?, ?, ?, ?, ?, ?)",
                   (burn.tx_hash, burn.log_index, burn.depositor, burn.mint_recipient,
                    burn.block_number, json.dumps(log), burn.block_hash))
    index = store.CandidateStore(path)
    assert only(index).from_stream and not only(index).signed and only(index).received_at is None
    monkeypatch.setattr(store.time, "time", lambda: 2000)
    save(index, log, signed=True)
    assert only(index).signed is True and only(index).received_at == 2000
    # Re-opening the migrated store preserves the recorded evidence.
    reopened = store.CandidateStore(path)
    assert only(reopened).signed is True and only(reopened).received_at == 2000

"""Persistent discovery candidates, never trusted completion/authorization state."""

import json
import sqlite3
import time
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path

from rebound.protocol import Burn, decode_burn


@dataclass(frozen=True)
class IndexedBurn:
    burn: Burn
    from_stream: bool
    # Seconds since the epoch when this server first received the event from the Stream.
    received_at: int | None = None
    # True only when a delivery carried a valid QuickNode signature. A delivery accepted with
    # the local shared secret is a Stream-endpoint test, not evidence that QuickNode sent it.
    signed: bool = False


class CandidateStore:
    def __init__(self, path: str):
        self.path = path
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        with closing(sqlite3.connect(path)) as db:
            db.execute("BEGIN IMMEDIATE")
            db.execute("""CREATE TABLE IF NOT EXISTS candidates (
                tx_hash TEXT NOT NULL, log_index INTEGER NOT NULL,
                depositor TEXT NOT NULL, recipient TEXT NOT NULL,
                block_number INTEGER NOT NULL, payload TEXT NOT NULL, stream_block_hash TEXT,
                PRIMARY KEY(tx_hash, log_index))""")
            columns = {row[1] for row in db.execute("PRAGMA table_info(candidates)")}
            if "stream_block_hash" not in columns:
                db.execute("ALTER TABLE candidates ADD COLUMN stream_block_hash TEXT")
            if "stream_received_at" not in columns:
                db.execute("ALTER TABLE candidates ADD COLUMN stream_received_at INTEGER")
            if "stream_signed" not in columns:
                db.execute("ALTER TABLE candidates ADD COLUMN stream_signed INTEGER")
            db.execute("CREATE INDEX IF NOT EXISTS by_depositor ON candidates(depositor)")
            db.execute("CREATE INDEX IF NOT EXISTS by_recipient ON candidates(recipient)")
            db.execute("""CREATE TABLE IF NOT EXISTS local_mints (
                source_hash TEXT NOT NULL, log_index INTEGER NOT NULL,
                nonce TEXT NOT NULL, destination_hash TEXT NOT NULL,
                PRIMARY KEY(source_hash, log_index, nonce))""")
            db.commit()

    def save_many(self, entries: list[tuple[Burn, dict]], *, from_stream: bool = False,
                  signed: bool = False) -> None:
        if not entries:
            return
        received_at = int(time.time()) if from_stream else None
        signed_flag = int(signed) if from_stream else None
        with closing(sqlite3.connect(self.path, timeout=10)) as db:
            db.executemany("""INSERT INTO candidates
                (tx_hash, log_index, depositor, recipient, block_number, payload, stream_block_hash,
                 stream_received_at, stream_signed)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(tx_hash, log_index) DO UPDATE SET
                depositor=excluded.depositor, recipient=excluded.recipient,
                block_number=excluded.block_number, payload=excluded.payload,
                stream_block_hash=COALESCE(excluded.stream_block_hash, candidates.stream_block_hash),
                stream_received_at=COALESCE(candidates.stream_received_at, excluded.stream_received_at),
                stream_signed=CASE WHEN candidates.stream_signed = 1 OR excluded.stream_signed = 1 THEN 1
                                   ELSE COALESCE(excluded.stream_signed, candidates.stream_signed) END""",
                ((burn.tx_hash, burn.log_index, burn.depositor, burn.mint_recipient,
                  burn.block_number, json.dumps(log), burn.block_hash if from_stream else None, received_at,
                  signed_flag)
                 for burn, log in entries))
            db.commit()

    def candidates(self, wallet: str, limit: int = 101, *, block_range: tuple[int, int] | None = None,
                   stream_only: bool = False) -> list[IndexedBurn]:
        query = "SELECT payload, stream_block_hash, stream_received_at, stream_signed FROM candidates WHERE (depositor=? OR recipient=?)"
        params: tuple = (wallet, wallet)
        if block_range is not None:
            query += " AND block_number BETWEEN ? AND ?"
            params += block_range
        if stream_only:
            query += " AND stream_block_hash = lower(json_extract(payload, '$.blockHash'))"
        query += " ORDER BY block_number DESC, log_index DESC, tx_hash ASC LIMIT ?"
        with closing(sqlite3.connect(self.path, timeout=10)) as db:
            rows = db.execute(query, (*params, limit)).fetchall()
        burns = []
        for payload, stream_block_hash, received_at, signed in rows:
            try:
                burn = decode_burn(json.loads(payload))
                from_stream = burn.block_hash == stream_block_hash
                burns.append(IndexedBurn(burn, from_stream, received_at if from_stream else None,
                                         bool(signed) and from_stream))
            except (ValueError, TypeError):
                continue
        return burns

    def stream_summary(self, wallet: str) -> dict:
        """What the Stream index holds for a wallet, split by how each delivery was authenticated.

        ``signed`` counts burns whose delivery carried a valid QuickNode signature;
        ``local`` counts burns accepted with the local shared secret (adapter tests).
        Reads the index only. It never touches an RPC, and it makes no claim about
        which blocks the Stream has covered.
        """
        with closing(sqlite3.connect(self.path, timeout=10)) as db:
            signed, last_signed, local = db.execute(
                """SELECT COALESCE(SUM(stream_signed = 1), 0), MAX(CASE WHEN stream_signed = 1 THEN stream_received_at END),
                          COALESCE(SUM(stream_signed IS NOT 1), 0)
                   FROM candidates WHERE (depositor=? OR recipient=?)
                   AND stream_block_hash = lower(json_extract(payload, '$.blockHash'))""",
                (wallet, wallet)).fetchone()
        return {"signed": int(signed), "last_signed": last_signed, "local": int(local)}

    def remember_mint(self, source_hash: str, log_index: int, nonce: str, destination_hash: str) -> None:
        with closing(sqlite3.connect(self.path, timeout=10)) as db:
            db.execute("""INSERT INTO local_mints (source_hash, log_index, nonce, destination_hash) VALUES (?, ?, ?, ?)
                ON CONFLICT(source_hash, log_index, nonce) DO UPDATE SET destination_hash=excluded.destination_hash""",
                (source_hash, log_index, nonce, destination_hash))
            db.commit()

    def local_mint(self, burn: Burn, nonce: str) -> str | None:
        try:
            with closing(sqlite3.connect(self.path)) as db:
                row = db.execute("SELECT destination_hash FROM local_mints WHERE source_hash=? AND log_index=? AND nonce=?",
                                 (burn.tx_hash, burn.log_index, nonce)).fetchone()
        except sqlite3.Error:
            return None  # Tracking a receipt is optional for a verified consumed nonce.
        return row[0] if row else None

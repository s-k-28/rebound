"""Persistent discovery candidates, never trusted completion/authorization state."""

import json
import sqlite3
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path

from rebound.protocol import Burn, decode_burn


@dataclass(frozen=True)
class IndexedBurn:
    burn: Burn
    from_stream: bool


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
            if "stream_block_hash" not in {row[1] for row in db.execute("PRAGMA table_info(candidates)")}:
                db.execute("ALTER TABLE candidates ADD COLUMN stream_block_hash TEXT")
            db.execute("CREATE INDEX IF NOT EXISTS by_depositor ON candidates(depositor)")
            db.execute("CREATE INDEX IF NOT EXISTS by_recipient ON candidates(recipient)")
            db.execute("""CREATE TABLE IF NOT EXISTS local_mints (
                source_hash TEXT NOT NULL, log_index INTEGER NOT NULL,
                nonce TEXT NOT NULL, destination_hash TEXT NOT NULL,
                PRIMARY KEY(source_hash, log_index, nonce))""")
            db.commit()

    def save_many(self, entries: list[tuple[Burn, dict]], *, from_stream: bool = False) -> None:
        if not entries:
            return
        with closing(sqlite3.connect(self.path, timeout=10)) as db:
            db.executemany("""INSERT INTO candidates
                (tx_hash, log_index, depositor, recipient, block_number, payload, stream_block_hash)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(tx_hash, log_index) DO UPDATE SET
                depositor=excluded.depositor, recipient=excluded.recipient,
                block_number=excluded.block_number, payload=excluded.payload,
                stream_block_hash=COALESCE(excluded.stream_block_hash, candidates.stream_block_hash)""",
                ((burn.tx_hash, burn.log_index, burn.depositor, burn.mint_recipient,
                  burn.block_number, json.dumps(log), burn.block_hash if from_stream else None)
                 for burn, log in entries))
            db.commit()

    def candidates(self, wallet: str, limit: int = 101, *, block_range: tuple[int, int] | None = None,
                   stream_only: bool = False) -> list[IndexedBurn]:
        query = "SELECT payload, stream_block_hash FROM candidates WHERE (depositor=? OR recipient=?)"
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
        for payload, stream_block_hash in rows:
            try:
                burn = decode_burn(json.loads(payload))
                burns.append(IndexedBurn(burn, burn.block_hash == stream_block_hash))
            except (ValueError, TypeError):
                continue
        return burns

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

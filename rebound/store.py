"""Persistent discovery candidates, never trusted completion/authorization state."""

import json
import sqlite3
from contextlib import closing
from pathlib import Path

from rebound.protocol import Burn, decode_burn


class CandidateStore:
    def __init__(self, path: str):
        self.path = path
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        with closing(sqlite3.connect(path)) as db:
            db.execute("""CREATE TABLE IF NOT EXISTS candidates (
                tx_hash TEXT NOT NULL, log_index INTEGER NOT NULL,
                depositor TEXT NOT NULL, recipient TEXT NOT NULL,
                block_number INTEGER NOT NULL, payload TEXT NOT NULL,
                PRIMARY KEY(tx_hash, log_index))""")
            db.execute("CREATE INDEX IF NOT EXISTS by_depositor ON candidates(depositor)")
            db.execute("CREATE INDEX IF NOT EXISTS by_recipient ON candidates(recipient)")
            db.commit()

    def save(self, burn: Burn, log: dict) -> None:
        with closing(sqlite3.connect(self.path, timeout=10)) as db:
            db.execute("""INSERT INTO candidates VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(tx_hash, log_index) DO UPDATE SET
                depositor=excluded.depositor, recipient=excluded.recipient,
                block_number=excluded.block_number, payload=excluded.payload""",
                (burn.tx_hash, burn.log_index, burn.depositor, burn.mint_recipient,
                 burn.block_number, json.dumps(log)))
            db.commit()

    def candidates(self, wallet: str, limit: int = 101) -> list[Burn]:
        with closing(sqlite3.connect(self.path, timeout=10)) as db:
            rows = db.execute("""SELECT payload FROM candidates WHERE depositor=? OR recipient=?
                ORDER BY block_number DESC, log_index DESC LIMIT ?""", (wallet, wallet, limit)).fetchall()
        burns = []
        for row in rows:
            try:
                burns.append(decode_burn(json.loads(row[0])))
            except (ValueError, TypeError):
                continue
        return burns

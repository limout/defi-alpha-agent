from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable


@dataclass
class Snapshot:
    timestamp: str
    market: str
    name: str
    pt_price: float | None
    implied_apy: float | None
    liquidity_usd: float | None
    expiry: str
    chain_id: int = 0
    underlying_price_usd: float | None = None


class HistoryStore:
    def __init__(self, path: str):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(self.path)
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA synchronous=NORMAL")
        self._init()

    def _init(self) -> None:
        self.conn.execute(
            """
            CREATE TABLE IF NOT EXISTS snapshots (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp TEXT NOT NULL,
                market TEXT NOT NULL,
                name TEXT NOT NULL,
                pt_price REAL,
                implied_apy REAL,
                liquidity_usd REAL,
                expiry TEXT NOT NULL,
                chain_id INTEGER NOT NULL DEFAULT 0,
                underlying_price_usd REAL,
                UNIQUE(timestamp, market)
            )
            """
        )
        columns = {row[1] for row in self.conn.execute("PRAGMA table_info(snapshots)")}
        if "chain_id" not in columns:
            self.conn.execute("ALTER TABLE snapshots ADD COLUMN chain_id INTEGER NOT NULL DEFAULT 0")
        if "underlying_price_usd" not in columns:
            self.conn.execute("ALTER TABLE snapshots ADD COLUMN underlying_price_usd REAL")
        self.conn.execute("CREATE INDEX IF NOT EXISTS idx_snapshots_market_time ON snapshots(market, timestamp)")
        self.conn.execute("CREATE INDEX IF NOT EXISTS idx_snapshots_chain_time ON snapshots(chain_id, timestamp)")
        self.conn.commit()

    def insert(self, snapshots: Iterable[Snapshot]) -> int:
        rows = [
            (x.timestamp, x.market, x.name, x.pt_price, x.implied_apy,
             x.liquidity_usd, x.expiry, x.chain_id, x.underlying_price_usd)
            for x in snapshots
        ]
        if not rows:
            return 0
        cur = self.conn.cursor()
        before = self.conn.total_changes
        cur.executemany(
            """
            INSERT OR IGNORE INTO snapshots
            (timestamp, market, name, pt_price, implied_apy, liquidity_usd, expiry,
             chain_id, underlying_price_usd)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """ , rows)
        self.conn.commit()
        return self.conn.total_changes - before

    def recent(self, market: str, limit: int = 500) -> list[Snapshot]:
        rows = self.conn.execute(
            """
            SELECT timestamp, market, name, pt_price, implied_apy,
                   liquidity_usd, expiry, chain_id, underlying_price_usd
            FROM snapshots WHERE market = ? ORDER BY timestamp DESC LIMIT ?
            """, (market, limit)
        ).fetchall()
        return [Snapshot(*row) for row in reversed(rows)]

    def markets(self) -> list[str]:
        rows = self.conn.execute("SELECT DISTINCT market FROM snapshots ORDER BY market").fetchall()
        return [row[0] for row in rows]

    def count(self, market: str | None = None) -> int:
        if market:
            row = self.conn.execute("SELECT COUNT(*) FROM snapshots WHERE market = ?", (market,)).fetchone()
        else:
            row = self.conn.execute("SELECT COUNT(*) FROM snapshots").fetchone()
        return int(row[0] or 0)

    def close(self) -> None:
        self.conn.close()

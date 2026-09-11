from __future__ import annotations

import os
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
    """Persistent snapshot store.

    Uses Neon/PostgreSQL when DATABASE_URL is configured, otherwise keeps the
    existing local SQLite fallback for development and offline work.
    """

    def __init__(self, path: str, database_url: str | None = None):
        self.path = Path(path)
        self.database_url = database_url or os.getenv("DATABASE_URL")
        if self.database_url:
            import psycopg

            self.backend = "postgres"
            self.conn = psycopg.connect(self.database_url, connect_timeout=10)
        else:
            self.backend = "sqlite"
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.conn = sqlite3.connect(self.path)
            self.conn.execute("PRAGMA journal_mode=WAL")
            self.conn.execute("PRAGMA synchronous=NORMAL")
        self._init()

    def _init(self) -> None:
        if self.backend == "postgres":
            self.conn.execute(
                """
                CREATE TABLE IF NOT EXISTS snapshots (
                    id BIGSERIAL PRIMARY KEY,
                    timestamp TEXT NOT NULL,
                    market TEXT NOT NULL,
                    name TEXT NOT NULL,
                    pt_price DOUBLE PRECISION,
                    implied_apy DOUBLE PRECISION,
                    liquidity_usd DOUBLE PRECISION,
                    expiry TEXT NOT NULL,
                    chain_id INTEGER NOT NULL DEFAULT 0,
                    underlying_price_usd DOUBLE PRECISION,
                    UNIQUE(timestamp, market)
                )
                """
            )
            self.conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_snapshots_market_time "
                "ON snapshots(market, timestamp)"
            )
            self.conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_snapshots_chain_time "
                "ON snapshots(chain_id, timestamp)"
            )
            self.conn.commit()
            return

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
            self.conn.execute(
                "ALTER TABLE snapshots ADD COLUMN chain_id INTEGER NOT NULL DEFAULT 0"
            )
        if "underlying_price_usd" not in columns:
            self.conn.execute(
                "ALTER TABLE snapshots ADD COLUMN underlying_price_usd REAL"
            )
        self.conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_snapshots_market_time "
            "ON snapshots(market, timestamp)"
        )
        self.conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_snapshots_chain_time "
            "ON snapshots(chain_id, timestamp)"
        )
        self.conn.commit()

    def insert(self, snapshots: Iterable[Snapshot]) -> int:
        rows = [
            (
                x.timestamp,
                x.market,
                x.name,
                x.pt_price,
                x.implied_apy,
                x.liquidity_usd,
                x.expiry,
                x.chain_id,
                x.underlying_price_usd,
            )
            for x in snapshots
        ]
        if not rows:
            return 0

        if self.backend == "postgres":
            cur = self.conn.cursor()
            cur.executemany(
                """
                INSERT INTO snapshots
                (timestamp, market, name, pt_price, implied_apy, liquidity_usd,
                 expiry, chain_id, underlying_price_usd)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (timestamp, market) DO NOTHING
                """,
                rows,
            )
            inserted = cur.rowcount
            self.conn.commit()
            return max(0, int(inserted))

        cur = self.conn.cursor()
        before = self.conn.total_changes
        cur.executemany(
            """
            INSERT OR IGNORE INTO snapshots
            (timestamp, market, name, pt_price, implied_apy, liquidity_usd,
             expiry, chain_id, underlying_price_usd)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            rows,
        )
        self.conn.commit()
        return self.conn.total_changes - before

    def recent(self, market: str, limit: int = 500) -> list[Snapshot]:
        if self.backend == "postgres":
            rows = self.conn.execute(
                """
                SELECT timestamp, market, name, pt_price, implied_apy,
                       liquidity_usd, expiry, chain_id, underlying_price_usd
                FROM snapshots
                WHERE market = %s
                ORDER BY timestamp DESC
                LIMIT %s
                """,
                (market, limit),
            ).fetchall()
        else:
            rows = self.conn.execute(
                """
                SELECT timestamp, market, name, pt_price, implied_apy,
                       liquidity_usd, expiry, chain_id, underlying_price_usd
                FROM snapshots
                WHERE market = ?
                ORDER BY timestamp DESC
                LIMIT ?
                """,
                (market, limit),
            ).fetchall()
        return [Snapshot(*row) for row in reversed(rows)]

    def markets(self) -> list[str]:
        rows = self.conn.execute(
            "SELECT DISTINCT market FROM snapshots ORDER BY market"
        ).fetchall()
        return [row[0] for row in rows]

    def count(self, market: str | None = None) -> int:
        if self.backend == "postgres":
            if market:
                row = self.conn.execute(
                    "SELECT COUNT(*) FROM snapshots WHERE market = %s", (market,)
                ).fetchone()
            else:
                row = self.conn.execute("SELECT COUNT(*) FROM snapshots").fetchone()
        else:
            if market:
                row = self.conn.execute(
                    "SELECT COUNT(*) FROM snapshots WHERE market = ?", (market,)
                ).fetchone()
            else:
                row = self.conn.execute("SELECT COUNT(*) FROM snapshots").fetchone()
        return int(row[0] or 0)

    def info(self) -> str:
        return "Neon PostgreSQL" if self.backend == "postgres" else f"SQLite: {self.path}"

    def close(self) -> None:
        self.conn.close()

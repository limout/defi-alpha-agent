from __future__ import annotations

import os
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path


@dataclass
class PaperTrade:
    id: int
    opened_at: str
    market: str
    name: str
    side: str
    capital_usd: float
    pt_amount_raw: str
    entry_usd: float
    target_usd: float
    stop_usd: float
    status: str
    close_reason: str | None
    closed_at: str | None
    pnl_usd: float | None
    pnl_return: float | None


class PaperLedger:
    """Persistent paper-trade ledger.

    Uses Neon/PostgreSQL when DATABASE_URL is configured, otherwise keeps the
    existing local SQLite fallback.
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
        self._init()

    def _init(self):
        if self.backend == "postgres":
            self.conn.execute(
                """
                CREATE TABLE IF NOT EXISTS paper_trades (
                    id BIGSERIAL PRIMARY KEY,
                    opened_at TEXT NOT NULL,
                    market TEXT NOT NULL,
                    name TEXT NOT NULL,
                    side TEXT NOT NULL,
                    capital_usd DOUBLE PRECISION NOT NULL,
                    pt_amount_raw TEXT NOT NULL,
                    entry_usd DOUBLE PRECISION NOT NULL,
                    target_usd DOUBLE PRECISION NOT NULL,
                    stop_usd DOUBLE PRECISION NOT NULL,
                    status TEXT NOT NULL,
                    close_reason TEXT,
                    closed_at TEXT,
                    pnl_usd DOUBLE PRECISION,
                    pnl_return DOUBLE PRECISION
                )
                """
            )
            self.conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_paper_open "
                "ON paper_trades(market, status)"
            )
            self.conn.commit()
            return

        self.conn.execute(
            """
            CREATE TABLE IF NOT EXISTS paper_trades (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                opened_at TEXT NOT NULL, market TEXT NOT NULL, name TEXT NOT NULL,
                side TEXT NOT NULL, capital_usd REAL NOT NULL, pt_amount_raw TEXT NOT NULL,
                entry_usd REAL NOT NULL, target_usd REAL NOT NULL, stop_usd REAL NOT NULL,
                status TEXT NOT NULL, close_reason TEXT, closed_at TEXT,
                pnl_usd REAL, pnl_return REAL
            )
            """
        )
        self.conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_paper_open ON paper_trades(market,status)"
        )
        self.conn.commit()

    def has_open(self, market: str) -> bool:
        if self.backend == "postgres":
            row = self.conn.execute(
                "SELECT 1 FROM paper_trades WHERE market=%s AND status='OPEN' LIMIT 1",
                (market,),
            ).fetchone()
        else:
            row = self.conn.execute(
                "SELECT 1 FROM paper_trades WHERE market=? AND status='OPEN' LIMIT 1",
                (market,),
            ).fetchone()
        return row is not None

    def open_trade(
        self,
        market: str,
        name: str,
        capital_usd: float,
        pt_amount_raw: str,
        entry: float,
        target: float,
        stop: float,
    ) -> int:
        values = (
            datetime.now(timezone.utc).isoformat(),
            market,
            name,
            "BUY_PT",
            capital_usd,
            pt_amount_raw,
            entry,
            target,
            stop,
            "OPEN",
        )
        if self.backend == "postgres":
            cur = self.conn.execute(
                """
                INSERT INTO paper_trades
                (opened_at,market,name,side,capital_usd,pt_amount_raw,entry_usd,
                 target_usd,stop_usd,status)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                RETURNING id
                """,
                values,
            )
            trade_id = cur.fetchone()[0]
        else:
            cur = self.conn.execute(
                """
                INSERT INTO paper_trades
                (opened_at,market,name,side,capital_usd,pt_amount_raw,entry_usd,
                 target_usd,stop_usd,status)
                VALUES (?,?,?,?,?,?,?,?,?,?)
                """,
                values,
            )
            trade_id = cur.lastrowid
        self.conn.commit()
        return int(trade_id)

    def open_trades(self) -> list[PaperTrade]:
        rows = self.conn.execute(
            """
            SELECT id,opened_at,market,name,side,capital_usd,pt_amount_raw,
                   entry_usd,target_usd,stop_usd,status,close_reason,closed_at,
                   pnl_usd,pnl_return
            FROM paper_trades
            WHERE status='OPEN'
            ORDER BY id
            """
        ).fetchall()
        return [PaperTrade(*row) for row in rows]

    def close_trade(
        self, trade_id: int, reason: str, pnl_usd: float, pnl_return: float
    ):
        now = datetime.now(timezone.utc).isoformat()
        if self.backend == "postgres":
            self.conn.execute(
                """
                UPDATE paper_trades
                SET status='CLOSED', close_reason=%s, closed_at=%s,
                    pnl_usd=%s, pnl_return=%s
                WHERE id=%s AND status='OPEN'
                """,
                (reason, now, pnl_usd, pnl_return, trade_id),
            )
        else:
            self.conn.execute(
                """
                UPDATE paper_trades
                SET status='CLOSED', close_reason=?, closed_at=?,
                    pnl_usd=?, pnl_return=?
                WHERE id=? AND status='OPEN'
                """,
                (reason, now, pnl_usd, pnl_return, trade_id),
            )
        self.conn.commit()

    def summary(self) -> dict:
        row = self.conn.execute(
            """
            SELECT
                COUNT(*),
                SUM(CASE WHEN status='CLOSED' THEN 1 ELSE 0 END),
                SUM(CASE WHEN status='CLOSED' AND pnl_usd>0 THEN 1 ELSE 0 END),
                COALESCE(SUM(CASE WHEN status='CLOSED' THEN pnl_usd ELSE 0 END),0)
            FROM paper_trades
            """
        ).fetchone()
        total, closed, wins, pnl = row
        return {
            "total": int(total or 0),
            "closed": int(closed or 0),
            "wins": int(wins or 0),
            "win_rate": (wins / closed if closed else None),
            "realized_pnl_usd": float(pnl or 0),
        }

    def info(self) -> str:
        return "Neon PostgreSQL" if self.backend == "postgres" else f"SQLite: {self.path}"

    def close(self):
        self.conn.close()

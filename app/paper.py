from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import datetime, timezone


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
    entry_asset: float | None = None
    target_asset: float | None = None
    stop_asset: float | None = None


class PaperLedger:
    """Persistent paper-trade ledger backed exclusively by Neon PostgreSQL."""

    def __init__(self, path: str | None = None, database_url: str | None = None):
        self.database_url = database_url or os.getenv("DATABASE_URL")
        if not self.database_url:
            raise RuntimeError("DATABASE_URL is required. Configure Neon PostgreSQL in .env.")
        import psycopg
        self.conn = psycopg.connect(self.database_url, connect_timeout=10)
        self._init()

    def _init(self):
        self.conn.execute(
            """CREATE TABLE IF NOT EXISTS paper_trades (
                id BIGSERIAL PRIMARY KEY, opened_at TEXT NOT NULL, market TEXT NOT NULL, name TEXT NOT NULL,
                side TEXT NOT NULL, capital_usd DOUBLE PRECISION NOT NULL, pt_amount_raw TEXT NOT NULL,
                entry_usd DOUBLE PRECISION NOT NULL, target_usd DOUBLE PRECISION NOT NULL, stop_usd DOUBLE PRECISION NOT NULL,
                status TEXT NOT NULL, close_reason TEXT, closed_at TEXT, pnl_usd DOUBLE PRECISION, pnl_return DOUBLE PRECISION,
                entry_asset DOUBLE PRECISION, target_asset DOUBLE PRECISION, stop_asset DOUBLE PRECISION)"""
        )
        for sql in (
            "ALTER TABLE paper_trades ADD COLUMN IF NOT EXISTS entry_asset DOUBLE PRECISION",
            "ALTER TABLE paper_trades ADD COLUMN IF NOT EXISTS target_asset DOUBLE PRECISION",
            "ALTER TABLE paper_trades ADD COLUMN IF NOT EXISTS stop_asset DOUBLE PRECISION",
        ): self.conn.execute(sql)
        self.conn.execute("CREATE INDEX IF NOT EXISTS idx_paper_open ON paper_trades(market,status)")
        self.conn.commit()

    def has_open(self, market: str) -> bool:
        row = self.conn.execute("SELECT 1 FROM paper_trades WHERE market=%s AND status='OPEN' LIMIT 1", (market,)).fetchone()
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
        entry_asset: float | None = None,
        target_asset: float | None = None,
        stop_asset: float | None = None,
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
            entry_asset,
            target_asset,
            stop_asset,
        )
        cur = self.conn.execute(
            """INSERT INTO paper_trades (opened_at,market,name,side,capital_usd,pt_amount_raw,entry_usd,target_usd,stop_usd,status,entry_asset,target_asset,stop_asset)
               VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) RETURNING id""", values)
        trade_id = cur.fetchone()[0]
        self.conn.commit()
        return int(trade_id)

    def open_trades(self) -> list[PaperTrade]:
        rows = self.conn.execute(
            """
            SELECT id,opened_at,market,name,side,capital_usd,pt_amount_raw,
                   entry_usd,target_usd,stop_usd,status,close_reason,closed_at,
                   pnl_usd,pnl_return,entry_asset,target_asset,stop_asset
            FROM paper_trades
            WHERE status='OPEN'
            ORDER BY id
            """
        ).fetchall()
        return [PaperTrade(*row) for row in rows]

    def close_trade(self, trade_id: int, reason: str, pnl_usd: float, pnl_return: float):
        now = datetime.now(timezone.utc).isoformat()
        self.conn.execute(
            """UPDATE paper_trades SET status='CLOSED', close_reason=%s, closed_at=%s, pnl_usd=%s, pnl_return=%s
               WHERE id=%s AND status='OPEN'""", (reason, now, pnl_usd, pnl_return, trade_id))
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
        return "Neon PostgreSQL"

    def close(self):
        self.conn.close()

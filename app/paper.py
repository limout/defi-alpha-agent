from __future__ import annotations

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
    def __init__(self, path: str):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(self.path)
        self.conn.execute("PRAGMA journal_mode=WAL")
        self._init()

    def _init(self):
        self.conn.execute("""
        CREATE TABLE IF NOT EXISTS paper_trades (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            opened_at TEXT NOT NULL, market TEXT NOT NULL, name TEXT NOT NULL,
            side TEXT NOT NULL, capital_usd REAL NOT NULL, pt_amount_raw TEXT NOT NULL,
            entry_usd REAL NOT NULL, target_usd REAL NOT NULL, stop_usd REAL NOT NULL,
            status TEXT NOT NULL, close_reason TEXT, closed_at TEXT,
            pnl_usd REAL, pnl_return REAL
        )
        """)
        self.conn.execute("CREATE INDEX IF NOT EXISTS idx_paper_open ON paper_trades(market,status)")
        self.conn.commit()

    def has_open(self, market: str) -> bool:
        row = self.conn.execute(
            "SELECT 1 FROM paper_trades WHERE market=? AND status='OPEN' LIMIT 1",
            (market,),
        ).fetchone()
        return row is not None

    def open_trade(self, market: str, name: str, capital_usd: float, pt_amount_raw: str, entry: float, target: float, stop: float) -> int:
        cur = self.conn.execute(
            """INSERT INTO paper_trades
            (opened_at,market,name,side,capital_usd,pt_amount_raw,entry_usd,target_usd,stop_usd,status)
            VALUES (?,?,?,?,?,?,?,?,?,?)""",
            (datetime.now(timezone.utc).isoformat(), market, name, "BUY_PT", capital_usd, pt_amount_raw, entry, target, stop, "OPEN"),
        )
        self.conn.commit()
        return int(cur.lastrowid)

    def open_trades(self) -> list[PaperTrade]:
        rows = self.conn.execute(
            "SELECT id,opened_at,market,name,side,capital_usd,pt_amount_raw,entry_usd,target_usd,stop_usd,status,close_reason,closed_at,pnl_usd,pnl_return FROM paper_trades WHERE status='OPEN' ORDER BY id"
        ).fetchall()
        return [PaperTrade(*row) for row in rows]

    def close_trade(self, trade_id: int, reason: str, pnl_usd: float, pnl_return: float):
        self.conn.execute(
            "UPDATE paper_trades SET status='CLOSED', close_reason=?, closed_at=?, pnl_usd=?, pnl_return=? WHERE id=? AND status='OPEN'",
            (reason, datetime.now(timezone.utc).isoformat(), pnl_usd, pnl_return, trade_id),
        )
        self.conn.commit()

    def summary(self) -> dict:
        row = self.conn.execute("SELECT COUNT(*), SUM(CASE WHEN status='CLOSED' THEN 1 ELSE 0 END), SUM(CASE WHEN status='CLOSED' AND pnl_usd>0 THEN 1 ELSE 0 END), COALESCE(SUM(CASE WHEN status='CLOSED' THEN pnl_usd ELSE 0 END),0) FROM paper_trades").fetchone()
        total, closed, wins, pnl = row
        return {"total": total, "closed": closed, "wins": wins, "win_rate": (wins/closed if closed else None), "realized_pnl_usd": pnl}

    def close(self):
        self.conn.close()

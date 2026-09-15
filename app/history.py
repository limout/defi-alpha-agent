from __future__ import annotations

import os
from dataclasses import dataclass
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
    pt_price_asset: float | None = None
    days_to_expiry: float | None = None
    underlying_id: str | None = None
    accounting_asset_id: str | None = None
    price_basis: str | None = None
    source_ts: str | None = None
    collection_complete: bool = True
    pt_address: str | None = None
    lp_apy: float | None = None


class HistoryStore:
    """Persistent snapshot store backed exclusively by Neon PostgreSQL."""

    def __init__(self, path: str | None = None, database_url: str | None = None):
        self.database_url = database_url or os.getenv("DATABASE_URL")
        if not self.database_url:
            raise RuntimeError("DATABASE_URL is required. Configure Neon PostgreSQL in .env.")
        import psycopg
        self.conn = psycopg.connect(self.database_url, connect_timeout=10)
        self._init()

    def _init(self) -> None:
        self.conn.execute(
            """
            CREATE TABLE IF NOT EXISTS snapshots (
                id BIGSERIAL PRIMARY KEY, timestamp TEXT NOT NULL, market TEXT NOT NULL,
                name TEXT NOT NULL, pt_price DOUBLE PRECISION, implied_apy DOUBLE PRECISION,
                liquidity_usd DOUBLE PRECISION, expiry TEXT NOT NULL, chain_id INTEGER NOT NULL DEFAULT 0,
                underlying_price_usd DOUBLE PRECISION, pt_price_asset DOUBLE PRECISION, days_to_expiry DOUBLE PRECISION,
                underlying_id TEXT, accounting_asset_id TEXT, price_basis TEXT, source_ts TEXT,
                collection_complete BOOLEAN NOT NULL DEFAULT TRUE, UNIQUE(timestamp, market)
            )
            """
        )
        for sql in (
            "ALTER TABLE snapshots ADD COLUMN IF NOT EXISTS pt_price_asset DOUBLE PRECISION",
            "ALTER TABLE snapshots ADD COLUMN IF NOT EXISTS days_to_expiry DOUBLE PRECISION",
            "ALTER TABLE snapshots ADD COLUMN IF NOT EXISTS underlying_id TEXT",
            "ALTER TABLE snapshots ADD COLUMN IF NOT EXISTS accounting_asset_id TEXT",
            "ALTER TABLE snapshots ADD COLUMN IF NOT EXISTS price_basis TEXT",
            "ALTER TABLE snapshots ADD COLUMN IF NOT EXISTS source_ts TEXT",
            "ALTER TABLE snapshots ADD COLUMN IF NOT EXISTS collection_complete BOOLEAN NOT NULL DEFAULT TRUE",
            "ALTER TABLE snapshots ADD COLUMN IF NOT EXISTS pt_address TEXT",
            "ALTER TABLE snapshots ADD COLUMN IF NOT EXISTS lp_apy DOUBLE PRECISION",
        ):
            self.conn.execute(sql)
        self.conn.execute("CREATE INDEX IF NOT EXISTS idx_snapshots_market_time ON snapshots(market, timestamp)")
        self.conn.execute("CREATE INDEX IF NOT EXISTS idx_snapshots_chain_time ON snapshots(chain_id, timestamp)")
        self.conn.execute(
            """CREATE TABLE IF NOT EXISTS quote_cache (
                market TEXT PRIMARY KEY, chain_id INTEGER NOT NULL, quote_ts TEXT NOT NULL,
                cost_usd DOUBLE PRECISION, fees_usd DOUBLE PRECISION, price_impact DOUBLE PRECISION,
                net_pnl_usd DOUBLE PRECISION, status TEXT NOT NULL, reason TEXT)"""
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
                x.pt_price_asset,
                x.days_to_expiry,
                x.underlying_id,
                x.accounting_asset_id,
                x.price_basis,
                x.source_ts or x.timestamp,
                x.collection_complete,
                x.pt_address,
                x.lp_apy,
            )
            for x in snapshots
        ]
        if not rows:
            return 0

        cur = self.conn.cursor()
        cur.executemany(
            """
            INSERT INTO snapshots
            (timestamp, market, name, pt_price, implied_apy, liquidity_usd, expiry, chain_id, underlying_price_usd, pt_price_asset, days_to_expiry, underlying_id, accounting_asset_id, price_basis, source_ts, collection_complete, pt_address, lp_apy)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (timestamp, market) DO NOTHING
            """, rows)
        inserted = cur.rowcount
        self.conn.commit()
        return max(0, int(inserted))

    def recent(self, market: str, limit: int = 500) -> list[Snapshot]:
        rows = self.conn.execute(
            """SELECT timestamp, market, name, pt_price, implied_apy, liquidity_usd, expiry, chain_id,
                       underlying_price_usd, pt_price_asset, days_to_expiry, underlying_id, accounting_asset_id,
                       price_basis, source_ts, collection_complete, pt_address, lp_apy
                FROM snapshots WHERE market = %s ORDER BY timestamp DESC LIMIT %s""",
            (market, limit),
        ).fetchall()
        return [Snapshot(*row) for row in reversed(rows)]

    def backfill_legacy_equal_asset_markets(self, market_map: dict[str, tuple[str | None, str | None]]) -> int:
        """Backfill only legacy markets proven to have accountingAsset == underlyingAsset."""
        cur = self.conn.cursor()
        updates = 0
        for market, (underlying_id, accounting_id) in market_map.items():
            if not underlying_id or not accounting_id or str(underlying_id).lower() != str(accounting_id).lower():
                continue
            cur.execute(
                """UPDATE snapshots SET accounting_asset_id = %s, price_basis = 'ACCOUNTING_ASSET'
                   WHERE market = %s AND price_basis IS NULL AND pt_price_asset IS NOT NULL""",
                (accounting_id, market),
            )
            updates += max(0, cur.rowcount)
        self.conn.commit()
        return updates

    def markets(self) -> list[str]:
        rows = self.conn.execute(
            "SELECT DISTINCT market FROM snapshots ORDER BY market"
        ).fetchall()
        return [row[0] for row in rows]

    def count(self, market: str | None = None) -> int:
        row = (self.conn.execute("SELECT COUNT(*) FROM snapshots WHERE market = %s", (market,)).fetchone()
               if market else self.conn.execute("SELECT COUNT(*) FROM snapshots").fetchone())
        return int(row[0] or 0)

    def save_quote(self, market: str, chain_id: int, quote_ts: str, cost_usd: float | None, fees_usd: float | None, price_impact: float | None, net_pnl_usd: float | None, status: str, reason: str | None = None) -> None:
        self.conn.execute(
            """INSERT INTO quote_cache (market, chain_id, quote_ts, cost_usd, fees_usd, price_impact, net_pnl_usd, status, reason)
               VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)
               ON CONFLICT (market) DO UPDATE SET chain_id=EXCLUDED.chain_id, quote_ts=EXCLUDED.quote_ts,
                 cost_usd=EXCLUDED.cost_usd, fees_usd=EXCLUDED.fees_usd, price_impact=EXCLUDED.price_impact,
                 net_pnl_usd=EXCLUDED.net_pnl_usd, status=EXCLUDED.status, reason=EXCLUDED.reason""",
            (market, chain_id, quote_ts, cost_usd, fees_usd, price_impact, net_pnl_usd, status, reason))
        self.conn.commit()

    def get_quote(self, market: str) -> dict | None:
        row = self.conn.execute(
            "SELECT market, chain_id, quote_ts, cost_usd, fees_usd, price_impact, net_pnl_usd, status, reason FROM quote_cache WHERE market = %s",
            (market,),
        ).fetchone()
        if not row:
            return None
        return dict(zip(("market", "chain_id", "quote_ts", "cost_usd", "fees_usd", "price_impact", "net_pnl_usd", "status", "reason"), row))

    def info(self) -> str:
        return "Neon PostgreSQL"

    def close(self) -> None:
        self.conn.close()

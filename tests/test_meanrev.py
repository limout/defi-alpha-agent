"""Tests for the research-only broad mean-reversion experiment."""
from __future__ import annotations

import ast
import inspect
import os
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import MagicMock

os.environ.setdefault("DATABASE_URL", "postgresql://user:pass@localhost/db")

from app.backtest import distance_bp
from app.config import settings
from app.history import Snapshot
from app.meanrev import (
    CAPITAL,
    EMBARGO_MIN,
    MIN_ABS_DISTANCE_BP,
    ROUND_TRIP_COST,
    _stats,
    print_meanrev_report,
    run_meanrev,
    scan_market,
)
from app.signals import detect_signal


def _ts(i: int, start: datetime | None = None, step_min: int = 15) -> str:
    start = start or datetime(2026, 9, 1, tzinfo=timezone.utc)
    return (start + timedelta(minutes=i * step_min)).isoformat()


def _snap(
    i: int,
    *,
    pt_asset: float,
    liquidity: float = 2_000_000.0,
    ttm: float = 80.0,
    complete: bool = True,
    market: str = "0xmarket",
    name: str = "USDx",
    start: datetime | None = None,
) -> Snapshot:
    return Snapshot(
        timestamp=_ts(i, start),
        market=market,
        name=name,
        pt_price=pt_asset,
        implied_apy=0.10,
        liquidity_usd=liquidity,
        expiry="2026-12-17T00:00:00+00:00",
        chain_id=1,
        underlying_price_usd=1.0,
        pt_price_asset=pt_asset,
        days_to_expiry=ttm,
        underlying_id="1-0xacc",
        accounting_asset_id="1-0xacc",
        price_basis="ACCOUNTING_ASSET",
        source_ts=_ts(i, start),
        collection_complete=complete,
        pt_address="0xptptptptptptptptptptptptptptptptptptptpt",
    )


def _quiet_then(entry_px: float, follow_px: float, follow_n: int = 96) -> list[Snapshot]:
    rows = [_snap(i, pt_asset=0.9700 + (i % 3 - 1) * 1e-6) for i in range(40)]
    rows.append(_snap(40, pt_asset=entry_px))
    for i in range(41, 41 + follow_n):
        rows.append(_snap(i, pt_asset=follow_px))
    return rows


class MeanRevIsolationTest(unittest.TestCase):
    def test_module_does_not_call_detect_signal(self):
        src = Path(__file__).resolve().parents[1] / "app" / "meanrev.py"
        tree = ast.parse(src.read_text(encoding="utf-8"))
        names = [n.id for n in ast.walk(tree) if isinstance(n, ast.Name)]
        attrs = [n.attr for n in ast.walk(tree) if isinstance(n, ast.Attribute)]
        self.assertNotIn("detect_signal", names)
        self.assertNotIn("detect_signal", attrs)

    def test_production_thresholds_unchanged(self):
        self.assertEqual(settings.alpha_min_price_z, 2.5)
        self.assertEqual(settings.alpha_min_apy_z, 1.5)
        self.assertEqual(settings.alpha_round_trip_cost, 0.0020)
        self.assertEqual(settings.alpha_min_net_return, 0.0010)
        self.assertEqual(settings.alpha_min_price_distance, 0.0010)
        self.assertEqual(ROUND_TRIP_COST, 0.0020)
        self.assertEqual(MIN_ABS_DISTANCE_BP, 60.0)
        self.assertEqual(EMBARGO_MIN, 1440)

    def test_cloud_entrypoint_unchanged(self):
        procfile = Path(__file__).resolve().parents[1] / "Procfile"
        lines = [
            line.strip()
            for line in procfile.read_text(encoding="utf-8").splitlines()
            if line.strip() and not line.strip().startswith("#")
        ]
        self.assertEqual(lines, ["web: python -m app collect"])


class MeanRevLookAheadTest(unittest.TestCase):
    def test_entry_median_ignores_future_bars(self):
        cheap = 0.9700 / 1.007  # ~70 bp
        base = _quiet_then(cheap, 0.9720, follow_n=8)
        alt = _quiet_then(cheap, 0.9900, follow_n=8)
        a = scan_market(base)
        b = scan_market(alt)
        self.assertEqual(len(a), 1)
        self.assertEqual(len(b), 1)
        self.assertAlmostEqual(a[0].entry_distance_bp, b[0].entry_distance_bp, places=6)
        self.assertAlmostEqual(a[0].ref_price, b[0].ref_price, places=8)

    def test_below_60bp_is_excluded(self):
        # ~30 bp cheap vs ~0.97 book
        rows = _quiet_then(0.9700 / 1.003, 0.9720, follow_n=8)
        self.assertEqual(scan_market(rows), [])

    def test_does_not_need_detect_signal(self):
        cheap = 0.9700 / 1.007
        rows = _quiet_then(cheap, 0.9720, follow_n=8)
        # Flat APY and a fresh 15m dump would fail detect_signal continuation/APY-z.
        self.assertIsNone(
            detect_signal(
                rows[:41],
                settings.paper_capital_usd,
                settings.alpha_min_liquidity_usd,
                settings.alpha_round_trip_cost,
                settings.alpha_min_net_return,
                settings.alpha_min_price_z,
                settings.alpha_min_apy_z,
                settings.underlying_adverse_1h,
                settings.underlying_adverse_4h,
                settings.alpha_min_days_to_expiry,
                settings.alpha_max_days_to_expiry,
                settings.max_snapshot_gap_minutes,
                settings.alpha_min_price_distance,
            )
        )
        events = scan_market(rows)
        self.assertEqual(len(events), 1)
        self.assertGreaterEqual(abs(events[0].entry_distance_bp), 60.0)


class MeanRevFrozenMedianTest(unittest.TestCase):
    def test_horizon_uses_entry_ref(self):
        cheap = 0.9700 / 1.007
        rows = _quiet_then(cheap, 0.9900, follow_n=96)
        events = scan_market(rows)
        self.assertEqual(len(events), 1)
        ev = events[0]
        h24 = next(p for p in ev.horizons if p.horizon_min == 1440)
        self.assertIsNotNone(h24.price)
        self.assertAlmostEqual(h24.distance_bp, distance_bp(ev.ref_price, h24.price), places=6)
        later_med = 0.9900
        rolling = distance_bp(later_med, h24.price)
        self.assertNotAlmostEqual(h24.distance_bp, rolling, places=2)
        self.assertAlmostEqual(
            h24.distance_change_bp,
            h24.distance_bp - ev.entry_distance_bp,
            places=6,
        )


class MeanRevHorizonTest(unittest.TestCase):
    def test_horizons_align_to_1_4_12_24h(self):
        cheap = 0.9700 / 1.007
        rows = _quiet_then(cheap, 0.9720, follow_n=96)
        ev = scan_market(rows)[0]
        self.assertEqual([p.horizon_min for p in ev.horizons], [60, 240, 720, 1440])
        start = datetime.fromisoformat(ev.opened_at)
        for p in ev.horizons:
            self.assertIsNotNone(p.price)
            ts = datetime.fromisoformat(rows[40 + p.horizon_min // 15].timestamp)
            self.assertEqual(ts, start + timedelta(minutes=p.horizon_min))

    def test_missing_horizon_is_none(self):
        cheap = 0.9700 / 1.007
        rows = _quiet_then(cheap, 0.9720, follow_n=6)
        ev = scan_market(rows)[0]
        by_h = {p.horizon_min: p for p in ev.horizons}
        self.assertIsNotNone(by_h[60].price)
        self.assertIsNone(by_h[240].net_return)
        self.assertIsNone(by_h[1440].distance_bp)


class MeanRevEmbargoTest(unittest.TestCase):
    def test_24h_spacing_collapses_a_prolonged_dislocation(self):
        # Long quiet prefix so the past-only median stays near 0.97 after 24h of cheap.
        rows = [_snap(i, pt_asset=0.9700 + (i % 3 - 1) * 1e-6) for i in range(200)]
        cheap = 0.9700 / 1.008
        # 48h of cheap prints every 15m would be ~192 samples without embargo.
        for i in range(200, 200 + 192):
            rows.append(_snap(i, pt_asset=cheap))
        events = scan_market(rows)
        self.assertEqual(len(events), 2)
        t0 = datetime.fromisoformat(events[0].opened_at)
        t1 = datetime.fromisoformat(events[1].opened_at)
        self.assertGreaterEqual((t1 - t0).total_seconds(), EMBARGO_MIN * 60)

    def test_second_event_after_embargo_is_kept(self):
        rows = [_snap(i, pt_asset=0.9700) for i in range(40)]
        cheap = 0.9700 / 1.008
        rows.append(_snap(40, pt_asset=cheap))
        for i in range(41, 40 + 96):
            rows.append(_snap(i, pt_asset=0.9700))
        rows.append(_snap(40 + 96, pt_asset=cheap))
        for i in range(40 + 97, 40 + 96 + 8):
            rows.append(_snap(i, pt_asset=0.9700))
        events = scan_market(rows)
        self.assertEqual(len(events), 2)


class MeanRevPnlTest(unittest.TestCase):
    def test_cheap_long_net_after_20bp(self):
        cheap = 0.9600
        follow = 0.9660
        rows = _quiet_then(cheap, follow, follow_n=8)
        ev = scan_market(rows)[0]
        self.assertEqual(ev.side, "CHEAP")
        h1 = next(p for p in ev.horizons if p.horizon_min == 60)
        raw = follow / cheap - 1.0
        self.assertAlmostEqual(h1.raw_return, raw, places=8)
        self.assertAlmostEqual(h1.net_return, raw - 0.0020, places=8)
        self.assertAlmostEqual(h1.pnl_usd, CAPITAL * (raw - 0.0020), places=6)

    def test_rich_short_net_after_20bp(self):
        rich = 0.9700 * 1.007
        follow = 0.9700
        rows = _quiet_then(rich, follow, follow_n=8)
        ev = scan_market(rows)[0]
        self.assertEqual(ev.side, "RICH")
        h1 = next(p for p in ev.horizons if p.horizon_min == 60)
        raw = follow / rich - 1.0
        self.assertAlmostEqual(h1.raw_return, raw, places=8)
        self.assertAlmostEqual(h1.net_return, -raw - 0.0020, places=8)
        self.assertAlmostEqual(h1.pnl_usd, CAPITAL * (-raw - 0.0020), places=6)

    def test_stats_win_rate_uses_net(self):
        rows = _quiet_then(0.9600, 0.9660, follow_n=8)
        ev = scan_market(rows)[0]
        s = _stats(ev.horizons, 60)
        self.assertEqual(s.n, 1)
        self.assertEqual(s.win_rate, 1.0 if ev.horizons[0].net_return > 0 else 0.0)

    def test_report_states_observation_count_and_embargo(self):
        src = inspect.getsource(print_meanrev_report)
        self.assertIn("Unique observations (events)", src)
        self.assertIn("h per market after a qualifying entry", src)
        self.assertIn("research only", src.lower())
        self.assertIn("Does not call detect_signal()", src)

    def test_run_meanrev_is_read_only(self):
        store = MagicMock()
        store.markets.return_value = []
        run_meanrev(store)
        store.insert.assert_not_called()
        store.save_quote.assert_not_called()


if __name__ == "__main__":
    unittest.main()

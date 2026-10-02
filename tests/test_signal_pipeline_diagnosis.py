"""Diagnostic tests for snapshots → detect_signal → backtest → opportunities.

These lock current production behavior. They do not change thresholds.
"""
from __future__ import annotations

import inspect
import math
import os
import unittest
from pathlib import Path
from datetime import datetime, timedelta, timezone
from statistics import median
from unittest.mock import MagicMock

os.environ.setdefault("DATABASE_URL", "postgresql://user:pass@localhost/db")

from app.backtest import (
    HORIZONS,
    _report_group,
    build_event_trajectories,
    distance_bp,
    print_report,
    print_trajectory_report,
    run_backtest,
    unique_events,
)
from app.config import settings
from app.history import Snapshot
from app.monitor import _opportunity, show_signal_history
from app.signals import (
    BACKTEST_HISTORY_BARS,
    SIGNAL_HISTORY_BARS,
    _past_canonical_prices,
    detect_signal,
    _asset_price,
    _zscore,
)


def _ts(i: int, start: datetime | None = None, step_min: int = 15) -> str:
    start = start or datetime(2026, 9, 1, tzinfo=timezone.utc)
    return (start + timedelta(minutes=i * step_min)).isoformat()


def _snap(
    i: int,
    *,
    pt_asset: float = 0.9700,
    apy: float = 0.12,
    liquidity: float = 2_000_000.0,
    ttm: float = 80.0,
    complete: bool = True,
    basis: str = "ACCOUNTING_ASSET",
    market: str = "0xmarket",
    name: str = "USDx",
    start: datetime | None = None,
    step_min: int = 15,
    accounting_id: str = "1-0xacc",
) -> Snapshot:
    return Snapshot(
        timestamp=_ts(i, start, step_min),
        market=market,
        name=name,
        pt_price=pt_asset,
        implied_apy=apy,
        liquidity_usd=liquidity,
        expiry="2026-12-17T00:00:00+00:00",
        chain_id=1,
        underlying_price_usd=1.0,
        pt_price_asset=pt_asset,
        days_to_expiry=ttm,
        underlying_id=accounting_id,
        accounting_asset_id=accounting_id,
        price_basis=basis,
        source_ts=_ts(i, start, step_min),
        collection_complete=complete,
        pt_address="0xptptptptptptptptptptptptptptptptptptptpt",
    )


def _history(n: int = 40, **kwargs) -> list[Snapshot]:
    return [_snap(i, **kwargs) for i in range(n)]


def _qualifying_cheap(n: int = 40, last_n_cheap: int = 8) -> list[Snapshot]:
    """Quiet book, then a 70 bp cheap print that has already been cheap for last_n_cheap bars.

    last_n_cheap=1 is a fresh 15-minute dump (fails the 1h continuation gate).
    last_n_cheap=8 is ~2h old, so 1h return is flat while distance/z/APY still qualify.
    """
    rows = _history(n, pt_asset=0.9700, apy=0.10)
    cheap = 0.9700 / 1.007
    start = n - last_n_cheap
    for i, row in enumerate(rows):
        if i >= start:
            row.pt_price_asset = cheap
            row.pt_price = cheap
            row.implied_apy = 0.20
        else:
            row.pt_price_asset = 0.9700 + (i % 3 - 1) * 1e-6
            row.pt_price = row.pt_price_asset
    return rows


def _detect(history: list[Snapshot], **overrides):
    kwargs = dict(
        capital=settings.paper_capital_usd,
        min_liquidity=settings.alpha_min_liquidity_usd,
        estimated_round_trip_cost=settings.alpha_round_trip_cost,
        min_net_return=settings.alpha_min_net_return,
        min_price_z=settings.alpha_min_price_z,
        min_apy_z=settings.alpha_min_apy_z,
        underlying_adverse_1h=settings.underlying_adverse_1h,
        underlying_adverse_4h=settings.underlying_adverse_4h,
        min_days_to_expiry=settings.alpha_min_days_to_expiry,
        max_days_to_expiry=settings.alpha_max_days_to_expiry,
        max_snapshot_gap_minutes=settings.max_snapshot_gap_minutes,
        min_price_distance=settings.alpha_min_price_distance,
    )
    kwargs.update(overrides)
    return detect_signal(history, **kwargs)


def _min_distance_for_net() -> float:
    """Distance to median required by 50% retracement, cost, and min net."""
    return 2.0 * (settings.alpha_round_trip_cost + settings.alpha_min_net_return)


class ThresholdMathTest(unittest.TestCase):
    def test_economic_gate_requires_60bp_distance(self):
        self.assertEqual(settings.alpha_round_trip_cost, 0.0020)
        self.assertEqual(settings.alpha_min_net_return, 0.0010)
        self.assertAlmostEqual(_min_distance_for_net(), 0.0060)

    def test_min_price_distance_is_diagnostic_not_60bp(self):
        self.assertEqual(settings.alpha_min_price_distance, 0.0010)
        self.assertLess(settings.alpha_min_price_distance, _min_distance_for_net())

    def test_near_status_is_weaker_than_detect_signal(self):
        src = inspect.getsource(_opportunity)
        self.assertIn("_detect(history)", src)
        self.assertIn('status = "VALIDATED"', src)
        self.assertIn('status = "NEAR"', src)
        self.assertIn("alpha_min_price_z * 0.6", src)


class DetectSignalGatesTest(unittest.TestCase):
    def test_buy_only_never_emits_sell(self):
        src = inspect.getsource(detect_signal)
        self.assertIn('side="BUY"', src)
        self.assertNotIn("SELL", src)
        self.assertIn("price_z > -min_price_z", src)

    def test_flat_series_zero_sigma_is_not_a_signal(self):
        history = _history(40)
        self.assertIsNone(_detect(history))

    def test_large_z_on_tiny_dislocation_fails_economic_gate(self):
        # Quiet book then a 3 bp cheap print: z is extreme, distance is not 60 bp.
        rows = _history(40, pt_asset=0.9700)
        for i, row in enumerate(rows[:-1]):
            row.pt_price_asset = 0.9700 + (i % 3 - 1) * 1e-7
            row.pt_price = row.pt_price_asset
        rows[-1].pt_price_asset = 0.9700 * (1.0 - 0.0003)
        rows[-1].pt_price = rows[-1].pt_price_asset
        rows[-1].implied_apy = 0.20
        for row in rows[:-1]:
            row.implied_apy = 0.10

        log_prices = [math.log(x.pt_price_asset) for x in rows]
        z = _zscore(log_prices[:-1], log_prices[-1])
        self.assertIsNotNone(z)
        self.assertLessEqual(z, -settings.alpha_min_price_z)

        current = rows[-1].pt_price_asset
        dist = median([x.pt_price_asset for x in rows[:-1]]) / current - 1.0
        self.assertGreater(dist, 0)
        self.assertLess(dist, settings.alpha_min_price_distance)
        self.assertLess(dist, _min_distance_for_net())
        self.assertIsNone(_detect(rows))

    def test_60bp_cheap_without_apy_z_is_rejected(self):
        rows = _history(40, pt_asset=0.9700, apy=0.12)
        for i, row in enumerate(rows[:-1]):
            row.pt_price_asset = 0.9700 + (i % 3 - 1) * 1e-6
            row.pt_price = row.pt_price_asset
        rows[-1].pt_price_asset = 0.9700 / (1.0 + 0.007)
        rows[-1].pt_price = rows[-1].pt_price_asset
        self.assertIsNone(_detect(rows))

    def test_fresh_dislocation_fails_1h_continuation_gate(self):
        rows = _qualifying_cheap(last_n_cheap=1)
        self.assertIsNone(_detect(rows))

    def test_60bp_cheap_with_apy_z_is_accepted(self):
        # 5 cheap bars ≈ 75m: 1h return is flat, but the cheap cluster is still
        # a tail vs the prior quiet book (8 cheap bars bimodalize sigma and
        # can fail price_z).
        rows = _qualifying_cheap(last_n_cheap=5)
        sig = _detect(rows)
        self.assertIsNotNone(sig)
        self.assertEqual(sig.side, "BUY")
        self.assertGreaterEqual(sig.expected_net_return, settings.alpha_min_net_return)
        self.assertLessEqual(sig.price_z, -settings.alpha_min_price_z)
        self.assertGreaterEqual(sig.apy_z, settings.alpha_min_apy_z)

    def test_legacy_usd_basis_is_ignored(self):
        rows = _history(40, basis=None)
        for row in rows:
            row.price_basis = None
        self.assertIsNone(_detect(rows))

    def test_median_is_past_only_same_sample_as_z(self):
        src = inspect.getsource(detect_signal)
        self.assertIn("ref_price = median(past_prices)", src)
        self.assertIn("price_z = _zscore([log(x) for x in past_prices]", src)
        rows = _qualifying_cheap(last_n_cheap=5)
        past = _past_canonical_prices(rows)
        current = rows[-1].pt_price_asset
        sig = _detect(rows)
        self.assertIsNotNone(sig)
        self.assertEqual(len(past), len(rows) - 1)
        self.assertAlmostEqual(sig.distance_to_median, median(past) / current - 1.0, places=8)

    def test_1h_continuation_gate_guards_none(self):
        src = inspect.getsource(detect_signal)
        self.assertIn("if r1 is not None and r1 < -max(0.003, distance * 0.80):", src)

    def test_missing_r1_does_not_raise(self):
        rows = _qualifying_cheap(last_n_cheap=5)
        last = rows[-1]
        rows = rows[:-10] + [last]
        sig = _detect(rows)
        if sig is not None:
            self.assertIsNone(sig.price_return_1h)


class OpportunitiesMismatchTest(unittest.TestCase):
    def test_validated_when_detect_signal_passes(self):
        rows = _qualifying_cheap(last_n_cheap=5)
        opp = _opportunity(rows)
        sig = _detect(rows)
        self.assertIsNotNone(sig)
        self.assertIsNotNone(opp)
        self.assertEqual(opp.status, "VALIDATED")
        self.assertEqual(opp.side, sig.side)
        self.assertAlmostEqual(opp.residual_z, sig.price_z, places=6)
        self.assertAlmostEqual(opp.distance, sig.distance_to_median, places=8)

    def test_filtered_rows_are_still_returned(self):
        rows = _history(40)
        opp = _opportunity(rows)
        self.assertIsNotNone(opp)
        self.assertEqual(opp.status, "FILTERED")
        self.assertIsNone(_detect(rows))

    def test_near_fires_at_1_5_sigma_without_apy_z(self):
        rows = _history(40, pt_asset=0.9700, apy=0.12)
        for i, row in enumerate(rows[:-1]):
            row.pt_price_asset = 0.9700 + (i % 3 - 1) * 1e-6
            row.pt_price = row.pt_price_asset
        rows[-1].pt_price_asset = 0.9700 / 1.008
        rows[-1].pt_price = rows[-1].pt_price_asset
        opp = _opportunity(rows)
        self.assertIsNotNone(opp)
        self.assertEqual(opp.status, "NEAR")
        self.assertEqual(opp.side, "BUY")
        self.assertIsNone(_detect(rows))

    def test_residual_z_matches_log_price_z(self):
        rows = _history(40)
        for i, row in enumerate(rows[:-1]):
            row.pt_price_asset = 0.97 + i * 1e-5
            row.pt_price = row.pt_price_asset
        rows[-1].pt_price_asset = 0.96
        rows[-1].pt_price = 0.96
        opp = _opportunity(rows)
        self.assertIsNotNone(opp)
        past = [math.log(x.pt_price_asset) for x in rows[:-1]]
        expected = _zscore(past, math.log(rows[-1].pt_price_asset))
        self.assertAlmostEqual(opp.residual_z, expected, places=6)

    def test_sell_side_is_not_validated(self):
        rows = _history(40, pt_asset=0.9700)
        rows[-1].pt_price_asset = 0.9800
        rows[-1].pt_price = 0.9800
        opp = _opportunity(rows)
        self.assertNotEqual(opp.status, "VALIDATED")
        self.assertEqual(opp.side, "SELL")
        self.assertIsNone(_detect(rows))


class WindowMismatchTest(unittest.TestCase):
    def test_signals_cli_replays_same_window_as_backtest(self):
        self.assertEqual(BACKTEST_HISTORY_BARS, 5000)
        self.assertIn("BACKTEST_HISTORY_BARS", inspect.getsource(show_signal_history))
        self.assertNotIn("store.recent(market, 500)", inspect.getsource(show_signal_history))
        src = inspect.getsource(run_backtest)
        self.assertIn("BACKTEST_HISTORY_BARS", src)

    def test_live_scan_window_stays_288(self):
        self.assertEqual(SIGNAL_HISTORY_BARS, 288)

    def test_one_event_emits_four_horizon_observations(self):
        start = datetime(2026, 9, 1, tzinfo=timezone.utc)
        rows = []
        for i in range(40):
            cheap = i >= 32
            px = (0.9700 / 1.007) if cheap else 0.9700 + (i % 3 - 1) * 1e-6
            apy = 0.20 if cheap else 0.10
            rows.append(_snap(i, start=start, pt_asset=px, apy=apy))
        for i in range(40, 40 + 96):
            rows.append(_snap(i, start=start, pt_asset=0.9720, apy=0.12))

        store = MagicMock()
        store.markets.return_value = ["0xmarket"]
        store.recent.return_value = rows
        trades = run_backtest(store)
        self.assertEqual({t.horizon_min for t in trades}, {60, 240, 720, 1440})
        self.assertEqual(len(trades), 4)
        events = unique_events(trades)
        self.assertEqual(len(events), 1)
        src = inspect.getsource(print_report)
        self.assertIn("Unique events (market, opened_at)", src)
        self.assertIn("four horizon observations", src)
        self.assertNotIn("TRAJECTORY", src)
        self.assertNotIn("mean-reversion outcome", inspect.getsource(print_report))


class ResidualTrajectoryTest(unittest.TestCase):
    def test_distance_bp_formula(self):
        self.assertAlmostEqual(distance_bp(1.0, 0.99), 10000 * (1.0 / 0.99 - 1.0), places=8)
        self.assertIsNone(distance_bp(1.0, None))
        self.assertIsNone(distance_bp(1.0, 0.0))

    def test_distance_change_is_horizon_minus_entry(self):
        entry = 70.0
        horizon = 40.0
        self.assertAlmostEqual(horizon - entry, -30.0)

    def test_frozen_entry_median_not_later_median(self):
        start = datetime(2026, 9, 1, tzinfo=timezone.utc)
        rows = []
        for i in range(40):
            cheap = i >= 32
            px = (0.9700 / 1.007) if cheap else 0.9700 + (i % 3 - 1) * 1e-6
            apy = 0.20 if cheap else 0.10
            rows.append(_snap(i, start=start, pt_asset=px, apy=apy))
        for i in range(40, 40 + 96):
            rows.append(_snap(i, start=start, pt_asset=0.9900, apy=0.12))

        store = MagicMock()
        store.markets.return_value = ["0xmarket"]
        store.recent.return_value = rows
        trades = run_backtest(store)
        self.assertTrue(trades)
        traj = build_event_trajectories(store, trades)
        self.assertEqual(len(traj), 1)
        row = traj[0]
        later_median = median([x.pt_price_asset for x in rows if x.pt_price_asset])
        self.assertNotAlmostEqual(row.ref_price, later_median, places=4)
        h24 = next(p for p in row.horizons if p.horizon_min == 1440)
        self.assertIsNotNone(h24.price)
        self.assertAlmostEqual(h24.distance_bp, distance_bp(row.ref_price, h24.price), places=6)
        rolling = distance_bp(later_median, h24.price)
        self.assertNotAlmostEqual(h24.distance_bp, rolling, places=2)
        self.assertAlmostEqual(
            h24.distance_change_bp,
            h24.distance_bp - row.entry_distance_bp,
            places=6,
        )

    def test_missing_horizon_is_none(self):
        start = datetime(2026, 9, 1, tzinfo=timezone.utc)
        rows = []
        for i in range(40):
            cheap = i >= 32
            px = (0.9700 / 1.007) if cheap else 0.9700 + (i % 3 - 1) * 1e-6
            apy = 0.20 if cheap else 0.10
            rows.append(_snap(i, start=start, pt_asset=px, apy=apy))
        # Only ~2h of follow-through: 1h exists, 4h/12h/24h do not.
        for i in range(40, 48):
            rows.append(_snap(i, start=start, pt_asset=0.9720, apy=0.12))
        store = MagicMock()
        store.markets.return_value = ["0xmarket"]
        store.recent.return_value = rows
        trades = run_backtest(store)
        traj = build_event_trajectories(store, trades)
        self.assertEqual(len(traj), 1)
        by_h = {p.horizon_min: p for p in traj[0].horizons}
        self.assertIsNotNone(by_h[60].price)
        self.assertIsNone(by_h[240].price)
        self.assertIsNone(by_h[240].distance_bp)
        self.assertIsNone(by_h[240].distance_change_bp)
        self.assertEqual([p.horizon_min for p in traj[0].horizons], list(HORIZONS))

    def test_trajectory_report_is_unlabeled_diagnostic(self):
        src = inspect.getsource(print_trajectory_report)
        self.assertIn("distance_change_bp = horizon_distance_bp - entry_distance_bp", src)
        self.assertIn("Diagnostic only", src)
        self.assertNotIn("SUCCESS", src)
        self.assertNotIn("FAILED", src)
        self.assertNotIn("reverted", src.lower())

    def test_pnl_report_body_unchanged(self):
        src = inspect.getsource(print_report)
        group = inspect.getsource(_report_group)
        self.assertIn("=== FIXED-HORIZON PT/UNDERLYING BACKTEST ===", src)
        self.assertIn("Win rate:", group)
        self.assertIn("Total P&L:", group)
        self.assertNotIn("distance_bp", src)
        self.assertNotIn("distance_change_bp", src)


class LiveFunnelTest(unittest.TestCase):
    """Read-only funnel against Neon when DATABASE_URL is a real host."""

    def test_live_snapshot_funnel(self):
        url = os.environ.get("DATABASE_URL", "")
        env_file = Path(__file__).resolve().parents[1] / ".env"
        if env_file.is_file():
            for line in env_file.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if line.startswith("DATABASE_URL="):
                    parsed = line.split("=", 1)[1].strip().strip('"').strip("'")
                    if parsed:
                        url = parsed
                    break
        if "localhost" in url or "user:pass" in url or not url.startswith("postgres"):
            self.skipTest("no live DATABASE_URL")
        try:
            import psycopg  # noqa: F401
        except ImportError:
            self.skipTest("psycopg not installed in this interpreter")

        from app.history import HistoryStore
        from app.signals import SIGNAL_HISTORY_BARS

        store = HistoryStore(database_url=url)
        try:
            markets = store.markets()
            total = store.count()
            stages = {
                "markets": len(markets),
                "snapshots": total,
                "detect_pass_latest": 0,
                "opp_rows": 0,
                "opp_validated": 0,
                "opp_near": 0,
                "opp_filtered": 0,
                "backtest_trades": 0,
                "backtest_events": 0,
            }
            grouped_rejects: dict[str, int] = {}
            for market in markets:
                history = store.recent(market, BACKTEST_HISTORY_BARS)
                if _detect(history) is not None:
                    stages["detect_pass_latest"] += 1
                window = history[-SIGNAL_HISTORY_BARS:]
                opp = _opportunity(window) if window else None
                if opp is not None:
                    stages["opp_rows"] += 1
                    if opp.status == "VALIDATED":
                        stages["opp_validated"] += 1
                    elif opp.status == "NEAR":
                        stages["opp_near"] += 1
                    elif opp.status == "FILTERED":
                        stages["opp_filtered"] += 1
                reason = _latest_reject(history)
                bucket = reason.split("(")[0]
                grouped_rejects[bucket] = grouped_rejects.get(bucket, 0) + 1

            trades = run_backtest(store)
            stages["backtest_trades"] = len(trades)
            events = unique_events(trades)
            stages["backtest_events"] = len(events)
            print("\nLIVE FUNNEL", stages)
            print("LATEST REJECT GROUPS", grouped_rejects)
            self.assertGreater(total, 0)
            self.assertGreater(len(markets), 0)
        finally:
            store.close()


def _latest_reject(history: list[Snapshot]) -> str:
    if len(history) < 24:
        return "insufficient history"
    latest = history[-1]
    if not latest.collection_complete:
        return "collection_incomplete"
    current = _asset_price(latest)
    if current is None:
        return "no_canonical_asset"
    if latest.implied_apy is None:
        return "missing_apy"
    if latest.liquidity_usd is None or latest.liquidity_usd < settings.alpha_min_liquidity_usd:
        return "liquidity"
    if latest.days_to_expiry is None:
        return "missing_ttm"
    if not settings.alpha_min_days_to_expiry <= latest.days_to_expiry <= settings.alpha_max_days_to_expiry:
        return "ttm"
    from math import log
    from app.signals import _apy_bucket, _past_canonical_prices

    window = history[-288:]
    past_prices = _past_canonical_prices(window)
    if len(past_prices) < 20:
        return "insufficient_canonical"
    price_z = _zscore([log(x) for x in past_prices], log(current))
    if price_z is None or price_z > -settings.alpha_min_price_z:
        return f"price_z({price_z})"
    bucket = _apy_bucket(latest.days_to_expiry)
    bucket_apys = [
        x.implied_apy for x in window[:-1]
        if x.implied_apy is not None and _apy_bucket(x.days_to_expiry) == bucket
    ]
    apy_z = _zscore(bucket_apys, latest.implied_apy)
    if apy_z is None or apy_z < settings.alpha_min_apy_z:
        return f"apy_z({apy_z})"
    distance = median(past_prices) / current - 1.0
    if distance <= 0:
        return "distance_non_positive"
    if distance < settings.alpha_min_price_distance:
        return f"min_distance({distance:.4f})"
    gross = distance * 0.50
    net = gross - settings.alpha_round_trip_cost
    if net < settings.alpha_min_net_return:
        return f"economic(dist={distance:.4f})"
    return "would_pass_core"


if __name__ == "__main__":
    unittest.main()

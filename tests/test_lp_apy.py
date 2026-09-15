import os
import unittest
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

os.environ.setdefault("DATABASE_URL", "postgresql://user:pass@localhost/db")

from app.history import HistoryStore, Snapshot
from app.lp import (
    MIN_BASELINE_OBS,
    STATUS_NORMAL,
    STATUS_SPIKE,
    STATUS_STRONG,
    analyze_market,
    classify_multiple,
    daily_usd,
    extra_daily_usd,
)
from app.paper import PaperLedger
from app.sources.pendle import PendleClient


def _ts(hours_ago: float, now: datetime | None = None) -> str:
    now = now or datetime(2026, 9, 15, 12, 0, tzinfo=timezone.utc)
    return (now - timedelta(hours=hours_ago)).isoformat()


def _snap(ts: str, lp_apy: float | None, liquidity: float = 2_000_000.0) -> Snapshot:
    return Snapshot(
        timestamp=ts,
        market="0xabc",
        name="apyUSD",
        pt_price=0.97,
        implied_apy=0.14,
        liquidity_usd=liquidity,
        expiry="2027-01-01T00:00:00+00:00",
        chain_id=1,
        lp_apy=lp_apy,
    )


def _history(current: float, baseline: float, n_prev: int, nulls: int = 0) -> list[Snapshot]:
    now = datetime(2026, 9, 15, 12, 0, tzinfo=timezone.utc)
    rows: list[Snapshot] = []
    # Spread previous observations through the 48h window, 15 minutes apart.
    for i in range(n_prev):
        hours_ago = 0.25 * (n_prev - i)
        rows.append(_snap(_ts(hours_ago, now), baseline))
    for i in range(nulls):
        rows.append(_snap(_ts(0.25 * (n_prev + i + 1), now), None))
    rows.sort(key=lambda x: x.timestamp)
    rows.append(_snap(now.isoformat(), current))
    return rows


class ApyNormalizationTest(unittest.TestCase):
    def test_decimal_passthrough(self):
        self.assertAlmostEqual(PendleClient._normalize_apy_value(0.14), 0.14)
        self.assertAlmostEqual(PendleClient._extract_lp_apy({"details": {"aggregatedApy": 0.092}}), 0.092)

    def test_percent_converted(self):
        self.assertAlmostEqual(PendleClient._normalize_apy_value(14.1), 0.141)
        self.assertAlmostEqual(PendleClient._extract_lp_apy({"aggregatedApy": 28}), 0.28)

    def test_details_preferred_over_top_level(self):
        payload = {"details": {"aggregatedApy": 0.20}, "aggregatedApy": 0.01}
        self.assertAlmostEqual(PendleClient._extract_lp_apy(payload), 0.20)

    def test_missing_is_none(self):
        self.assertIsNone(PendleClient._extract_lp_apy({}))
        self.assertIsNone(PendleClient._normalize_apy_value(None))


class LpBaselineTest(unittest.TestCase):
    def test_48h_median_baseline(self):
        now = datetime(2026, 9, 15, 12, 0, tzinfo=timezone.utc)
        rows = []
        for i in range(MIN_BASELINE_OBS):
            # first half 0.10, second half 0.20 → median 0.10 or 0.20 depending on even count
            apy = 0.10 if i < MIN_BASELINE_OBS // 2 else 0.20
            rows.append(_snap(_ts(0.25 * (MIN_BASELINE_OBS - i), now), apy))
        rows.append(_snap(now.isoformat(), 0.30))
        row = analyze_market(rows)
        self.assertIsNotNone(row)
        self.assertEqual(row.n, MIN_BASELINE_OBS)
        # 144 even: 72×0.10 then 72×0.20 → median 0.15
        self.assertAlmostEqual(row.baseline_lp_apy, 0.15)

    def test_insufficient_history_rejected(self):
        rows = _history(0.28, 0.14, n_prev=MIN_BASELINE_OBS - 1)
        self.assertIsNone(analyze_market(rows))

    def test_null_historical_rows_do_not_crash_or_count(self):
        rows = _history(0.28, 0.14, n_prev=MIN_BASELINE_OBS, nulls=20)
        row = analyze_market(rows)
        self.assertIsNotNone(row)
        self.assertEqual(row.n, MIN_BASELINE_OBS)
        self.assertAlmostEqual(row.current_lp_apy, 0.28)

    def test_null_only_history_rejected(self):
        now = datetime(2026, 9, 15, 12, 0, tzinfo=timezone.utc)
        rows = [_snap(_ts(0.25 * i, now), None) for i in range(MIN_BASELINE_OBS, 0, -1)]
        rows.append(_snap(now.isoformat(), 0.20))
        self.assertIsNone(analyze_market(rows))

    def test_low_liquidity_rejected(self):
        rows = _history(0.28, 0.14, n_prev=MIN_BASELINE_OBS)
        rows[-1] = _snap(rows[-1].timestamp, 0.28, liquidity=999_999)
        self.assertIsNone(analyze_market(rows))

    def test_non_positive_current_rejected(self):
        self.assertIsNone(analyze_market(_history(0.0, 0.14, n_prev=MIN_BASELINE_OBS)))
        rows = _history(0.28, 0.14, n_prev=MIN_BASELINE_OBS)
        rows[-1] = _snap(rows[-1].timestamp, None)
        self.assertIsNone(analyze_market(rows))


class LpClassificationTest(unittest.TestCase):
    def test_thresholds(self):
        self.assertEqual(classify_multiple(1.49), STATUS_NORMAL)
        self.assertEqual(classify_multiple(1.50), STATUS_SPIKE)
        self.assertEqual(classify_multiple(1.99), STATUS_SPIKE)
        self.assertEqual(classify_multiple(2.00), STATUS_STRONG)

    def test_end_to_end_multiples(self):
        cases = (
            (1.49, 1.0, STATUS_NORMAL),
            (1.50, 1.0, STATUS_SPIKE),
            (1.99, 1.0, STATUS_SPIKE),
            (2.00, 1.0, STATUS_STRONG),
        )
        for current, baseline, status in cases:
            row = analyze_market(_history(current, baseline, n_prev=MIN_BASELINE_OBS))
            self.assertIsNotNone(row)
            self.assertEqual(row.status, status)
            self.assertAlmostEqual(row.lp_apy_multiple, current / baseline)


class DailyUsdTest(unittest.TestCase):
    def test_5000_capital(self):
        self.assertAlmostEqual(daily_usd(0.28, 5000), 5000 * 0.28 / 365)
        self.assertAlmostEqual(extra_daily_usd(0.28, 0.14, 5000), 5000 * 0.14 / 365)
        self.assertAlmostEqual(extra_daily_usd(0.10, 0.14, 5000), 0.0)

    def test_row_daily_matches_formula(self):
        row = analyze_market(_history(0.28, 0.14, n_prev=MIN_BASELINE_OBS))
        self.assertIsNotNone(row)
        self.assertAlmostEqual(row.daily_usd_5000, 5000 * 0.28 / 365)
        self.assertAlmostEqual(row.extra_daily_usd_5000, 5000 * 0.14 / 365)


class NeonOnlyTest(unittest.TestCase):
    def test_dbcheck_strings_are_neon(self):
        self.assertEqual(HistoryStore.info(SimpleNamespace()), "Neon PostgreSQL")
        self.assertEqual(PaperLedger.info(SimpleNamespace()), "Neon PostgreSQL")

    def test_no_sqlite_imports_in_app(self):
        import pathlib

        root = pathlib.Path(__file__).resolve().parents[1] / "app"
        offenders = []
        for path in root.rglob("*.py"):
            text = path.read_text(encoding="utf-8")
            if "sqlite3" in text or "sqlite://" in text:
                    offenders.append(str(path))
        self.assertEqual(offenders, [])


if __name__ == "__main__":
    unittest.main()

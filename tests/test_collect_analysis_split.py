import ast
import asyncio
import inspect
import os
import textwrap
import unittest
from pathlib import Path
from unittest.mock import patch

os.environ.setdefault("DATABASE_URL", "postgresql://user:pass@localhost/db")

from app.alpha import collect_once, evaluate, load_histories, run_collect
from app.diagnostics import build_states
from app.history import Snapshot
from app.lp import EXPECTED_OBSERVATIONS
from app.main import main
from app.signals import SIGNAL_HISTORY_BARS
from app.sources.pendle import PTMarket


def _market(**kwargs) -> PTMarket:
    defaults = dict(
        chain_id=1,
        market_address="0xabc",
        pt_address="0xpt",
        name="test",
        expiry="2027-01-01T00:00:00+00:00",
        implied_apy=0.1,
        pt_price_usd=0.97,
        liquidity_usd=2_000_000,
        underlying_asset_id="1-0xu",
        accounting_asset_id="1-0xu",
        pt_price_asset=0.97,
        valuation_basis="ACCOUNTING_ASSET",
        days_to_expiry=120.0,
        underlying_price_usd=1.0,
        lp_apy=0.12,
    )
    defaults.update(kwargs)
    return PTMarket(**defaults)


class RecordingStore:
    def __init__(self):
        self.recent_calls: list[tuple] = []
        self.inserted: list[Snapshot] = []
        self.backfill_map = None

    def backfill_legacy_equal_asset_markets(self, market_map):
        self.backfill_map = market_map
        return 0

    def insert(self, snapshots):
        self.inserted = list(snapshots)
        return len(self.inserted)

    def recent(self, market, limit=500):
        self.recent_calls.append((market, limit))
        return []

    def recent_many(self, markets, limit):
        return {m: self.recent(m, limit) for m in markets}

    def count(self, market=None):
        return len(self.inserted)

    def info(self):
        return "Neon PostgreSQL"

    def close(self):
        return None


class CollectAnalysisSplitTest(unittest.TestCase):
    def test_signal_window_is_288_not_500(self):
        self.assertEqual(SIGNAL_HISTORY_BARS, 288)
        self.assertLess(SIGNAL_HISTORY_BARS, 500)
        self.assertGreater(SIGNAL_HISTORY_BARS, EXPECTED_OBSERVATIONS)

    def test_collect_functions_do_not_read_history(self):
        for fn in (collect_once, run_collect):
            tree = ast.parse(textwrap.dedent(inspect.getsource(fn)))
            calls = [
                n.func.attr
                for n in ast.walk(tree)
                if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
            ]
            self.assertNotIn("recent", calls, fn.__name__)
            self.assertNotIn("recent_many", calls, fn.__name__)
            names = [n.id for n in ast.walk(tree) if isinstance(n, ast.Name)]
            self.assertNotIn("evaluate", names, fn.__name__)
            self.assertNotIn("build_states", names, fn.__name__)
            self.assertNotIn("show_opportunities", names, fn.__name__)

    def test_collect_once_inserts_lp_apy_without_recent(self):
        store = RecordingStore()
        market = _market(lp_apy=0.18)

        async def fake_all_markets(*args, **kwargs):
            return [market], {"pages": 1}

        async def _run():
            with patch("app.alpha.PendleClient") as client_cls:
                client_cls.return_value.all_markets = fake_all_markets
                markets, diagnostics = await collect_once(object(), store)
            return markets, diagnostics

        markets, diagnostics = asyncio.run(_run())
        self.assertEqual(store.recent_calls, [])
        self.assertEqual(len(store.inserted), 1)
        self.assertEqual(store.inserted[0].lp_apy, 0.18)
        self.assertEqual(store.inserted[0].pt_address, "0xpt")
        self.assertEqual(diagnostics["snapshots_inserted"], 1)
        self.assertEqual(len(markets), 1)

    def test_evaluate_and_diagnostics_reuse_one_history_map(self):
        store = RecordingStore()
        markets = [_market()]
        histories = load_histories(markets, store)
        self.assertEqual(store.recent_calls, [("0xabc", SIGNAL_HISTORY_BARS)])
        evaluate(markets, histories)
        build_states(markets, histories)
        self.assertEqual(len(store.recent_calls), 1)

    def test_cli_collect_is_not_alpha(self):
        source = inspect.getsource(main)
        self.assertIn("run_collect()", source)
        self.assertIn('args.command == "collect"', source)
        self.assertNotIn('in ("alpha", "collect")', source)
        self.assertIn('default="alpha"', source)

    def test_production_entrypoint_is_collect(self):
        procfile = Path(__file__).resolve().parents[1] / "Procfile"
        self.assertTrue(procfile.is_file(), "Cloud Run source deploys need a Procfile")
        lines = [
            line.strip()
            for line in procfile.read_text(encoding="utf-8").splitlines()
            if line.strip() and not line.strip().startswith("#")
        ]
        self.assertEqual(lines, ["web: python -m app collect"])

    def test_alpha_path_does_not_insert(self):
        from app.alpha import run_once

        src = inspect.getsource(main)
        collect_idx = src.index('args.command == "collect"')
        alpha_idx = src.index('args.command == "alpha"')
        self.assertLess(collect_idx, alpha_idx)
        self.assertIn("run_collect()", src[collect_idx:alpha_idx])
        self.assertIn("run_once()", src[alpha_idx:])
        once_src = inspect.getsource(run_once)
        self.assertNotIn("run_collect", once_src)
        self.assertNotIn("collect_once", once_src)
        self.assertIn("load_histories", once_src)


if __name__ == "__main__":
    unittest.main()

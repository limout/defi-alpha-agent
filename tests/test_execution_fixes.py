import os
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock

os.environ.setdefault("DATABASE_URL", "postgresql://user:pass@localhost/db")

from app.sources.pendle import PTMarket
from app.trading import (
    NATIVE_USDC_FALLBACK_DECIMALS,
    USDC_ADDRESSES,
    TradeSimulator,
)


BNB_USDC = "0x8ac76a51cc950d9822d68b83fe1ad97b32cd580d"


class BnbUsdcDecimalsTest(unittest.TestCase):
    def test_configured_bnb_address_is_binance_peg_usdc(self):
        self.assertEqual(USDC_ADDRESSES[56].lower(), BNB_USDC.lower())

    def test_bnb_has_no_six_decimal_fallback(self):
        self.assertNotIn(56, NATIVE_USDC_FALLBACK_DECIMALS)
        self.assertEqual(NATIVE_USDC_FALLBACK_DECIMALS[1], 6)
        self.assertEqual(NATIVE_USDC_FALLBACK_DECIMALS[42161], 6)
        self.assertEqual(NATIVE_USDC_FALLBACK_DECIMALS[8453], 6)

    def test_raw_amount_18_vs_6(self):
        six = TradeSimulator._raw_amount(5000, 6)
        eighteen = TradeSimulator._raw_amount(5000, 18)
        self.assertEqual(six, 5_000_000_000)
        self.assertEqual(eighteen, 5_000 * 10**18)
        self.assertNotEqual(six, eighteen)

    def test_quote_token_resolves_bnb_via_rpc_decimals(self):
        async def _run():
            pendle = AsyncMock()
            pendle.resolve_token_decimals = AsyncMock(return_value=18)
            sim = TradeSimulator(pendle)
            token, decimals = await sim._quote_token(56)
            self.assertEqual(token.lower(), BNB_USDC.lower())
            self.assertEqual(decimals, 18)
            pendle.resolve_token_decimals.assert_awaited()

        import asyncio
        asyncio.run(_run())

    def test_bnb_does_not_fall_back_to_6_when_rpc_fails(self):
        async def _run():
            pendle = AsyncMock()
            pendle.resolve_token_decimals = AsyncMock(return_value=None)
            sim = TradeSimulator(pendle)
            result = await sim._quote_token(56)
            self.assertIsNone(result)

        import asyncio
        asyncio.run(_run())

    def test_eth_falls_back_to_6_when_rpc_fails(self):
        async def _run():
            pendle = AsyncMock()
            pendle.resolve_token_decimals = AsyncMock(return_value=None)
            sim = TradeSimulator(pendle)
            token, decimals = await sim._quote_token(1)
            self.assertEqual(decimals, 6)
            self.assertTrue(token.startswith("0x"))

        import asyncio
        asyncio.run(_run())


class BuyUsdMarkTest(unittest.TestCase):
    def test_marks_pt_at_market_usd_not_accounting_par(self):
        market = PTMarket(
            chain_id=42161,
            market_address="0xabc",
            pt_address="0xdef",
            name="test",
            expiry="",
            implied_apy=0.1,
            pt_price_usd=0.95,
            liquidity_usd=1e6,
            accounting_asset_price_usd=1.0,
        )
        marked = TradeSimulator._pt_mark_usd(market, 100.0)
        self.assertAlmostEqual(marked, 95.0)
        self.assertNotAlmostEqual(marked, 100.0)


class QuoteFreshnessTest(unittest.TestCase):
    def test_stale_quote_is_not_fresh(self):
        from app.monitor import _quote_is_fresh

        old = (datetime.now(timezone.utc) - timedelta(hours=2)).isoformat()
        quote = {
            "status": "QUOTED",
            "cost_usd": 12.0,
            "quote_ts": old,
            "reason": "USDC -> PT -> USDC round-trip; two-sided Pendle convert quote",
        }
        self.assertFalse(_quote_is_fresh(quote))

    def test_fresh_quote_is_usable(self):
        from app.monitor import _quote_is_fresh

        now = datetime.now(timezone.utc).isoformat()
        quote = {
            "status": "QUOTED",
            "cost_usd": 12.0,
            "quote_ts": now,
            "reason": "USDC -> PT -> USDC round-trip; two-sided Pendle convert quote",
        }
        self.assertTrue(_quote_is_fresh(quote))


if __name__ == "__main__":
    unittest.main()

from __future__ import annotations

import asyncio
import json

from .config import settings
from .http import HttpClient
from .sources.pendle import PendleClient
from .trading import TradeSimulator


async def run_preflight() -> None:
    """Run one read-only two-sided Pendle quote against the best liquid market.

    This intentionally does not create a paper trade. It is a wiring test for
    token decimals, Pendle Convert v3, route parsing, fees and price impact.
    """
    http = HttpClient()
    try:
        print("\n=== DEFI ALPHA AGENT v0.6.0 EXECUTION PREFLIGHT ===")
        print("READ-ONLY / NO WALLET / NO APPROVALS / NO TRANSACTIONS")
        print(f"Capital test: ${settings.paper_capital_usd:,.2f}")
        print(f"Chains: {settings.chain_ids()}\n")

        pendle = PendleClient(http)
        markets, diagnostics = await pendle.all_markets(
            settings.chain_id,
            chain_ids=settings.chain_ids(),
            min_liquidity_usd=0.0,
        )

        candidates = [
            m for m in markets
            if m.pt_price_usd is not None
            and m.underlying_price_usd is not None
            and m.underlying_address
            and m.pt_address
        ]
        candidates.sort(key=lambda m: m.liquidity_usd or 0.0, reverse=True)

        print("=== PREFLIGHT MARKET ===")
        if not candidates:
            print("FAIL: no accepted market has the required execution fields")
            print(json.dumps(diagnostics, indent=2))
            return

        market = candidates[0]
        print(f"name: {market.name}")
        print(f"chain: {market.chain_id}")
        print(f"market: {market.market_address}")
        print(f"PT: {market.pt_address}")
        print(f"underlying: {market.underlying_address}")
        print(f"PT price: ${market.pt_price_usd:,.8f}")
        print(f"underlying price: ${market.underlying_price_usd:,.8f}")
        print(f"liquidity: ${market.liquidity_usd or 0:,.0f}")

        decimals = market.underlying_decimals
        if decimals is None:
            print("resolving underlying decimals via read-only eth_call ...")
            decimals = await pendle.resolve_token_decimals(
                market.chain_id, market.underlying_address
            )
            market.underlying_decimals = decimals
        print(f"underlying decimals: {decimals}")

        if decimals is None:
            print("FAIL: could not resolve underlying token decimals")
            return

        simulator = TradeSimulator(pendle)
        print("\n=== BUY QUOTE ===")
        try:
            buy = await simulator.quote_buy(market, settings.paper_capital_usd)
        except Exception as exc:
            print(f"FAIL: BUY quote error: {type(exc).__name__}: {exc}")
            return

        if buy is None:
            print("FAIL: BUY quote returned no usable output")
            return

        print(f"input raw: {buy.input_amount_raw}")
        print(f"PT output raw: {buy.output_amount_raw}")
        print(f"PT output USD: ${buy.output_amount_usd:,.2f}")
        print(f"fee USD: ${buy.fee_usd:,.4f}")
        print(f"price impact: {buy.price_impact:.4%}")
        print(f"effective APY: {buy.effective_apy if buy.effective_apy is not None else 'n/a'}")

        print("\n=== SELL QUOTE ===")
        try:
            sell = await simulator.quote_sell(market, buy.output_amount_raw)
        except Exception as exc:
            print(f"FAIL: SELL quote error: {type(exc).__name__}: {exc}")
            return

        if sell is None:
            print("FAIL: SELL quote returned no usable output")
            return

        print(f"PT input raw: {sell.input_amount_raw}")
        print(f"underlying output raw: {sell.output_amount_raw}")
        print(f"underlying output USD: ${sell.output_amount_usd:,.2f}")
        print(f"fee USD: ${sell.fee_usd:,.4f}")
        print(f"price impact: {sell.price_impact:.4%}")
        print(f"effective APY: {sell.effective_apy if sell.effective_apy is not None else 'n/a'}")

        fees = buy.fee_usd + sell.fee_usd
        # Pendle Convert returns routed output after execution effects. Its
        # documentation states price impact is included in output, while the
        # fee is reported separately. Therefore sell output minus capital is
        # already the actual post-fee round-trip P&L. Subtracting fee again
        # would double-count it.
        execution_pnl = sell.output_amount_usd - settings.paper_capital_usd
        gross = execution_pnl + fees
        net = execution_pnl
        ret = net / settings.paper_capital_usd if settings.paper_capital_usd else 0.0

        print("\n=== ROUND TRIP ===")
        print(f"capital: ${settings.paper_capital_usd:,.2f}")
        print(f"gross P&L (before reported fees): ${gross:+,.2f}")
        print(f"Pendle fees (already reflected in output): ${fees:,.2f}")
        print(f"price impact (diagnostic): {(buy.price_impact + sell.price_impact):.4%}")
        print(f"realized round-trip P&L: ${net:+,.2f} ({ret:+.3%})")
        print("\nPREFLIGHT: PASS — both BUY and SELL quotes returned successfully.")
    finally:
        await http.close()


if __name__ == "__main__":
    asyncio.run(run_preflight())

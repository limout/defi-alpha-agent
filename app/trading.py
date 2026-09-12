from __future__ import annotations

from dataclasses import dataclass, asdict
from datetime import datetime, timezone
from typing import Any

from .config import settings
from .sources.pendle import PendleClient, PTMarket


ZERO_RECEIVER = "0x000000000000000000000000000000000000dEaD"

# The scanner's capital is denominated in USDC. These are the canonical
# native USDC contracts on the chains currently scanned. An env override
# QUOTE_TOKEN_<chainId> can be used if a deployment intentionally uses a
# different stablecoin.
USDC_ADDRESSES = {
    1: "0xA0b86991c6218b36c1d19D4a2e9Eb0cE3606eB48",
    42161: "0xaf88d065e77c8cC2239327C5EDb3A432268e5831",
    8453: "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913",
    56: "0x8ac76a51cc950d9822d68b83fe1ad97b32cd580d",
}
USDC_DECIMALS = 6



@dataclass
class QuoteLeg:
    side: str
    input_token: str
    input_amount_raw: str
    output_token: str
    output_amount_raw: str
    output_amount_usd: float
    fee_usd: float
    price_impact: float | None
    effective_apy: float | None
    aggregator_type: str | None = None


@dataclass
class RoundTripQuote:
    ok: bool
    reason: str
    capital_usd: float
    entry: QuoteLeg | None = None
    exit: QuoteLeg | None = None
    gross_pnl_usd: float = 0.0
    fees_usd: float = 0.0
    execution_pnl_usd: float = 0.0
    price_impact_cost_usd: float = 0.0
    net_pnl_usd: float = 0.0
    net_return: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _number(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _quote_output(payload: Any, token: str) -> str | None:
    if not isinstance(payload, dict):
        return None
    routes = payload.get("routes") or []
    for route in routes:
        for output in (route.get("outputs") or []):
            if str(output.get("token", "")).lower() == token.lower():
                amount = output.get("amount")
                if amount is not None:
                    return str(amount)
    for output in (payload.get("outputs") or []):
        if str(output.get("token", "")).lower() == token.lower():
            amount = output.get("amount")
            if amount is not None:
                return str(amount)
    return None


def _quote_route_data(payload: Any) -> tuple[float, float | None, float | None, str | None]:
    """Extract execution diagnostics without mixing aggregator impact domains.

    Pendle's Convert response exposes priceImpact per route. For native Pendle
    PT/SY swaps that value is directly comparable to the market trade. When an
    external token aggregator is involved, however, the route can contain a
    token-conversion leg whose price impact is not the same thing as PT pool
    impact. Summing those percentages across BUY and SELL legs can therefore
    produce misleading numbers (e.g. an 87% "impact" while the actual
    round-trip loss is only a few dollars). Keep the real output-based P&L as
    the source of truth and expose impact only for native routes.
    """
    fee = 0.0
    impacts: list[float] = []
    effective_apy = None
    aggregators: list[str] = []
    routes = (payload.get("routes") or []) if isinstance(payload, dict) else []
    for route in routes:
        data = route.get("data") or {}
        fee_obj = data.get("fee") or {}
        fee += _number(fee_obj.get("usd")) or 0.0
        aggregator = str(data.get("aggregatorType") or "VOID").strip()
        if aggregator and aggregator.upper() != "VOID":
            aggregators.append(aggregator)
        else:
            value = _number(data.get("priceImpact"))
            if value is not None:
                impacts.append(value)
        if effective_apy is None:
            effective_apy = _number(data.get("effectiveApy"))
    impact = sum(impacts) if impacts and not aggregators else None
    aggregator_type = ",".join(dict.fromkeys(aggregators)) if aggregators else "VOID"
    return fee, impact, effective_apy, aggregator_type


class TradeSimulator:
    def __init__(self, pendle: PendleClient):
        self.pendle = pendle

    @staticmethod
    def _underlying_usd_price(market: PTMarket) -> float | None:
        # Collection deliberately defers per-market swap-rate calls. Use the
        # bulk underlying USD price captured with the snapshot instead of
        # reconstructing it from a deferred swapping-price quote.
        price = market.underlying_price_usd
        if price is not None and price > 0:
            return price

        # Keep a defensive fallback for callers that construct PTMarket
        # objects from older data where the bulk underlying price is absent.
        rate = market.underlying_token_to_pt_rate
        pt_usd = market.pt_price_usd
        if rate is None or pt_usd is None or rate <= 0:
            return None
        return pt_usd * rate

    @staticmethod
    def _raw_amount(amount: float, decimals: int) -> int:
        return int(amount * (10 ** decimals))

    @staticmethod
    def _quote_token(chain_id: int) -> tuple[str, int] | None:
        import os

        override = os.getenv(f"QUOTE_TOKEN_{int(chain_id)}")
        token = (override or USDC_ADDRESSES.get(int(chain_id)) or "").strip()
        if not token:
            return None
        return token, USDC_DECIMALS

    async def quote_buy(self, market: PTMarket, capital_usd: float) -> QuoteLeg | None:
        """Quote the exact scanner trade: USDC -> PT.

        The previous implementation converted USDC into the market's
        underlying first and therefore measured a different route from the
        one shown by Pendle's UI. Capital is explicitly USDC in this agent, so
        the execution quote must use USDC as tokenIn.
        """
        if not market.pt_address:
            return None
        quote_token = self._quote_token(market.chain_id)
        if quote_token is None:
            return None
        usdc_address, usdc_decimals = quote_token
        raw_in = self._raw_amount(capital_usd, usdc_decimals)
        payload = await self.pendle._fetch_convert_quote(
            chain_id=market.chain_id,
            inputs=[{"token": usdc_address, "amount": str(raw_in)}],
            outputs=[market.pt_address],
            receiver=ZERO_RECEIVER,
            slippage=settings.quote_slippage,
        )
        raw_out = _quote_output(payload, market.pt_address)
        if raw_out is None:
            return None
        fee, impact, effective_apy, aggregator_type = _quote_route_data(payload)
        pt_decimals = market.pt_decimals
        if pt_decimals is None:
            pt_decimals = await self.pendle.resolve_token_decimals(market.chain_id, market.pt_address)
            market.pt_decimals = pt_decimals
        if pt_decimals is None:
            return None
        pt_amount = int(raw_out) / (10 ** pt_decimals)
        return QuoteLeg(
            side="BUY_PT",
            input_token=usdc_address,
            input_amount_raw=str(raw_in),
            output_token=market.pt_address,
            output_amount_raw=raw_out,
            output_amount_usd=pt_amount * (market.accounting_asset_price_usd or 1.0),
            fee_usd=fee,
            price_impact=impact,
            effective_apy=effective_apy,
            aggregator_type=aggregator_type,
        )

    async def quote_sell(self, market: PTMarket, pt_raw: str) -> QuoteLeg | None:
        """Quote the exact scanner exit: PT -> USDC."""
        if not market.pt_address:
            return None
        quote_token = self._quote_token(market.chain_id)
        if quote_token is None:
            return None
        usdc_address, usdc_decimals = quote_token
        payload = await self.pendle._fetch_convert_quote(
            chain_id=market.chain_id,
            inputs=[{"token": market.pt_address, "amount": str(pt_raw)}],
            outputs=[usdc_address],
            receiver=ZERO_RECEIVER,
            slippage=settings.quote_slippage,
        )
        raw_out = _quote_output(payload, usdc_address)
        if raw_out is None:
            return None
        fee, impact, effective_apy, aggregator_type = _quote_route_data(payload)
        token_out = int(raw_out) / (10 ** usdc_decimals)
        return QuoteLeg(
            side="SELL_PT",
            input_token=market.pt_address,
            input_amount_raw=str(pt_raw),
            output_token=usdc_address,
            output_amount_raw=raw_out,
            output_amount_usd=token_out,
            fee_usd=fee,
            price_impact=impact,
            effective_apy=effective_apy,
            aggregator_type=aggregator_type,
        )

    async def round_trip(self, market: PTMarket, capital_usd: float) -> RoundTripQuote:
        buy = await self.quote_buy(market, capital_usd)
        if buy is None:
            return RoundTripQuote(False, "entry quote unavailable", capital_usd)
        sell = await self.quote_sell(market, buy.output_amount_raw)
        if sell is None:
            return RoundTripQuote(False, "exit quote unavailable", capital_usd, entry=buy)
        fees = buy.fee_usd + sell.fee_usd
        # Convert returns net routed output: Pendle documents that price
        # impact is already included in the output amount, and route fee is
        # reported separately. Therefore the actual round-trip P&L is the
        # returned output minus capital; do NOT subtract fees a second time.
        execution_pnl = sell.output_amount_usd - capital_usd
        # Reconstruct a fee-excluded gross figure only for presentation.
        # The fee is already reflected in the routed output.
        gross = execution_pnl + fees
        impact_cost = (
            capital_usd * (buy.price_impact + sell.price_impact)
            if buy.price_impact is not None and sell.price_impact is not None
            else 0.0
        )
        net = execution_pnl
        return RoundTripQuote(
            ok=True,
            reason="two-sided Pendle convert quote",
            capital_usd=capital_usd,
            entry=buy,
            exit=sell,
            gross_pnl_usd=gross,
            fees_usd=fees,
            execution_pnl_usd=execution_pnl,
            price_impact_cost_usd=impact_cost,
            net_pnl_usd=net,
            net_return=net / capital_usd if capital_usd else 0.0,
        )

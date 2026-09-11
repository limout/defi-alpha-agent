from __future__ import annotations

from dataclasses import dataclass, asdict
from datetime import datetime, timezone
from typing import Any

from .config import settings
from .sources.pendle import PendleClient, PTMarket


ZERO_RECEIVER = "0x000000000000000000000000000000000000dEaD"


@dataclass
class QuoteLeg:
    side: str
    input_token: str
    input_amount_raw: str
    output_token: str
    output_amount_raw: str
    output_amount_usd: float
    fee_usd: float
    price_impact: float
    effective_apy: float | None


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


def _quote_route_data(payload: Any) -> tuple[float, float, float | None]:
    fee = 0.0
    impact = 0.0
    effective_apy = None
    for route in (payload.get("routes") or []) if isinstance(payload, dict) else []:
        data = route.get("data") or {}
        fee_obj = data.get("fee") or {}
        fee += _number(fee_obj.get("usd")) or 0.0
        impact += _number(data.get("priceImpact")) or 0.0
        if effective_apy is None:
            effective_apy = _number(data.get("effectiveApy"))
    return fee, impact, effective_apy


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

    async def quote_buy(self, market: PTMarket, capital_usd: float) -> QuoteLeg | None:
        if not market.underlying_address or not market.pt_address:
            return None
        decimals = market.underlying_decimals
        if decimals is None and market.underlying_address:
            decimals = await self.pendle.resolve_token_decimals(
                market.chain_id, market.underlying_address
            )
            if decimals is not None:
                market.underlying_decimals = decimals
        underlying_price = self._underlying_usd_price(market)
        if decimals is None or underlying_price is None or underlying_price <= 0:
            return None
        token_amount = capital_usd / underlying_price
        raw_in = self._raw_amount(token_amount, decimals)
        payload = await self.pendle._fetch_convert_quote(
            chain_id=market.chain_id,
            inputs=[{"token": market.underlying_address, "amount": str(raw_in)}],
            outputs=[market.pt_address],
            receiver=ZERO_RECEIVER,
            slippage=settings.quote_slippage,
        )
        raw_out = _quote_output(payload, market.pt_address)
        if raw_out is None:
            return None
        fee, impact, effective_apy = _quote_route_data(payload)
        return QuoteLeg(
            side="BUY_PT",
            input_token=market.underlying_address,
            input_amount_raw=str(raw_in),
            output_token=market.pt_address,
            output_amount_raw=raw_out,
            output_amount_usd=float(raw_out) / 1e18 * market.pt_price_usd,
            fee_usd=fee,
            price_impact=impact,
            effective_apy=effective_apy,
        )

    async def quote_sell(self, market: PTMarket, pt_raw: str) -> QuoteLeg | None:
        if not market.underlying_address or not market.pt_address:
            return None
        decimals = market.underlying_decimals
        if decimals is None and market.underlying_address:
            decimals = await self.pendle.resolve_token_decimals(
                market.chain_id, market.underlying_address
            )
            if decimals is not None:
                market.underlying_decimals = decimals
        underlying_price = self._underlying_usd_price(market)
        if decimals is None or underlying_price is None or underlying_price <= 0:
            return None
        payload = await self.pendle._fetch_convert_quote(
            chain_id=market.chain_id,
            inputs=[{"token": market.pt_address, "amount": str(pt_raw)}],
            outputs=[market.underlying_address],
            receiver=ZERO_RECEIVER,
            slippage=settings.quote_slippage,
        )
        raw_out = _quote_output(payload, market.underlying_address)
        if raw_out is None:
            return None
        fee, impact, effective_apy = _quote_route_data(payload)
        token_out = int(raw_out) / (10 ** decimals)
        return QuoteLeg(
            side="SELL_PT",
            input_token=market.pt_address,
            input_amount_raw=str(pt_raw),
            output_token=market.underlying_address,
            output_amount_raw=raw_out,
            output_amount_usd=token_out * underlying_price,
            fee_usd=fee,
            price_impact=impact,
            effective_apy=effective_apy,
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
        impact_cost = capital_usd * (buy.price_impact + sell.price_impact)
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

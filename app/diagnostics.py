from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from statistics import mean, median, pstdev

from .history import Snapshot


@dataclass
class MarketState:
    market: str
    name: str
    chain_id: int
    pt_price: float | None
    pt_price_asset: float | None
    apy: float | None
    apy_z: float | None
    pt_1h: float | None
    pt_4h: float | None
    underlying_1h: float | None
    underlying_4h: float | None
    distance_median: float | None
    liquidity_usd: float | None
    observations: int
    state: str


def _dt(v: str) -> float | None:
    try:
        return datetime.fromisoformat(v.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def _at(history: list[Snapshot], minutes: int) -> Snapshot | None:
    if not history:
        return None
    now = _dt(history[-1].timestamp)
    if now is None:
        return None
    target = now - minutes * 60
    choices = [(target - _dt(x.timestamp), x) for x in history[:-1] if _dt(x.timestamp) is not None and _dt(x.timestamp) <= target]
    return min(choices, key=lambda x: x[0])[1] if choices else None


def _ret(a: float | None, b: float | None) -> float | None:
    return None if a is None or b in (None, 0) else a / b - 1


def build_states(markets, histories: dict[str, list[Snapshot]]) -> list[MarketState]:
    out: list[MarketState] = []
    for market in markets:
        h = histories.get(market.market_address) or []
        latest = h[-1] if h else None
        p1, p4 = _at(h, 60), _at(h, 240)
        apys = [x.implied_apy for x in h[-288:] if x.implied_apy is not None]
        z = None
        if latest and latest.implied_apy is not None and len(apys) >= 20:
            base = apys[:-1]
            sd = pstdev(base) if base else 0
            z = 0 if sd == 0 else (latest.implied_apy - mean(base)) / sd
        prices = [x.pt_price_asset for x in h[-288:] if x.pt_price_asset is not None and x.pt_price_asset > 0 and x.price_basis == "ACCOUNTING_ASSET"]
        dist = None
        if latest and latest.pt_price_asset and latest.price_basis == "ACCOUNTING_ASSET" and prices:
            dist = median(prices) / latest.pt_price_asset - 1
        state = "WARMUP"
        if len(h) >= 24:
            state = "NEUTRAL"
            if z is not None and abs(z) >= 2.5 and dist is not None:
                state = "CANDIDATE"
        out.append(MarketState(
            market=market.market_address, name=market.name, chain_id=market.chain_id,
            pt_price=latest.pt_price if latest else market.pt_price_usd,
            pt_price_asset=latest.pt_price_asset if latest else market.pt_price_asset,
            apy=latest.implied_apy if latest else market.implied_apy,
            apy_z=z, pt_1h=_ret(latest.pt_price, p1.pt_price) if latest and p1 else None,
            pt_4h=_ret(latest.pt_price, p4.pt_price) if latest and p4 else None,
            underlying_1h=_ret(latest.underlying_price_usd, p1.underlying_price_usd) if latest and p1 else None,
            underlying_4h=_ret(latest.underlying_price_usd, p4.underlying_price_usd) if latest and p4 else None,
            distance_median=dist, liquidity_usd=latest.liquidity_usd if latest else market.liquidity_usd,
            observations=len(h), state=state,
        ))
    return sorted(out, key=lambda x: (x.state != "CANDIDATE", -(abs(x.apy_z) if x.apy_z is not None else 0)))


def print_states(states, top_n: int = 25) -> None:
    print("\n=== MARKET DIAGNOSTICS ===")
    if not states:
        print("No markets.")
        return
    for s in states[:top_n]:
        def pct(v): return "n/a" if v is None else f"{v:+.2%}"
        z = "n/a" if s.apy_z is None else f"{s.apy_z:+.2f}sd"
        apy = "n/a" if s.apy is None else f"{s.apy:.2%}"
        pt = "n/a" if s.pt_price_asset is None else f"{s.pt_price_asset:.8f}"
        liq = "n/a" if s.liquidity_usd is None else f"${s.liquidity_usd:,.0f}"
        print(f"{s.state:<9} ch={s.chain_id:<6} {s.name[:30]:30} PT/U={pt} APY={apy} z={z} 1h={pct(s.pt_1h)} 4h={pct(s.pt_4h)} U1h={pct(s.underlying_1h)} U4h={pct(s.underlying_4h)} liq={liq} n={s.observations}")

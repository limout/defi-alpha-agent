from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from statistics import mean, median, pstdev
from typing import Sequence

from .history import Snapshot


@dataclass
class Signal:
    market: str
    name: str
    side: str
    kind: str
    confidence: int
    entry: float
    target: float
    stop: float
    gross_return: float
    estimated_cost: float
    expected_net_return: float
    expected_net_pnl: float
    capital: float
    holding_min: int
    holding_max: int
    apy_now: float
    apy_z: float
    price_return_1h: float
    price_return_4h: float
    underlying_return_1h: float | None
    underlying_return_4h: float | None
    liquidity_usd: float
    reason: str
    distance_to_median: float


def _valid(values: Sequence[float | None]) -> list[float]:
    return [float(x) for x in values if x is not None]


def _zscore(values: Sequence[float | None], current: float | None) -> float | None:
    clean = _valid(values)
    if current is None or len(clean) < 20:
        return None
    sd = pstdev(clean)
    if sd == 0:
        return 0.0
    return (float(current) - mean(clean)) / sd


def _dt(value: str) -> float | None:
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def _value_at_or_before(history: list[Snapshot], minutes: int) -> Snapshot | None:
    if not history:
        return None
    latest_ts = _dt(history[-1].timestamp)
    if latest_ts is None:
        return None
    target = latest_ts - minutes * 60
    best = None
    best_delta = None
    for item in history[:-1]:
        ts = _dt(item.timestamp)
        if ts is None or ts > target:
            continue
        delta = target - ts
        if best_delta is None or delta < best_delta:
            best, best_delta = item, delta
    return best


def _return(current: float | None, old: float | None) -> float | None:
    if current is None or old in (None, 0):
        return None
    return current / old - 1.0


def detect_signal(
    history: list[Snapshot],
    capital: float,
    min_liquidity: float,
    round_trip_cost: float,
    min_net_return: float,
    min_apy_z: float,
    underlying_adverse_1h: float = 0.005,
    underlying_adverse_4h: float = 0.012,
) -> Signal | None:
    if len(history) < 24:
        return None
    latest = history[-1]
    if latest.pt_price is None or latest.implied_apy is None:
        return None
    if latest.liquidity_usd is None or latest.liquidity_usd < min_liquidity:
        return None

    p1 = _value_at_or_before(history, 60)
    p4 = _value_at_or_before(history, 240)
    r1 = _return(latest.pt_price, p1.pt_price if p1 else None)
    r4 = _return(latest.pt_price, p4.pt_price if p4 else None)
    if r1 is None or r4 is None:
        return None

    ur1 = _return(latest.underlying_price_usd, p1.underlying_price_usd if p1 else None)
    ur4 = _return(latest.underlying_price_usd, p4.underlying_price_usd if p4 else None)

    apys = [x.implied_apy for x in history[-288:] if x.implied_apy is not None]
    apy_z = _zscore(apys[:-1], latest.implied_apy)
    if apy_z is None or abs(apy_z) < min_apy_z:
        return None

    prices = [x.pt_price for x in history[-288:] if x.pt_price is not None]
    if len(prices) < 20:
        return None

    ref_price = median(prices)
    direction = "BUY" if latest.pt_price < ref_price else "SELL"
    distance = abs(ref_price / latest.pt_price - 1.0)
    target = latest.pt_price * (1 + (ref_price / latest.pt_price - 1) * 0.50)
    gross = abs(target / latest.pt_price - 1.0)
    net = gross - round_trip_cost
    if net < min_net_return:
        return None

    if direction == "BUY" and r1 < -max(0.003, distance * 0.80):
        return None
    if direction == "SELL" and r1 > max(0.003, distance * 0.80):
        return None

    # Underlying confirmation: a large adverse move is more likely a genuine
    # market repricing than a PT-specific dislocation, so do not fade it.
    if direction == "BUY":
        if ur1 is not None and ur1 < -underlying_adverse_1h:
            return None
        if ur4 is not None and ur4 < -underlying_adverse_4h:
            return None
    else:
        if ur1 is not None and ur1 > underlying_adverse_1h:
            return None
        if ur4 is not None and ur4 > underlying_adverse_4h:
            return None

    stop = latest.pt_price * (1 - distance * 0.75 if direction == "BUY" else 1 + distance * 0.75)
    confidence = 60 + min(20, int(abs(apy_z) * 5))
    if latest.liquidity_usd >= min_liquidity * 5:
        confidence += 10
    if abs(r4) < 0.01:
        confidence += 5
    if ur1 is not None and abs(ur1) < 0.002:
        confidence += 5
    confidence = min(confidence, 95)

    pnl = capital * net
    underlying_text = "underlying n/a"
    if ur1 is not None:
        underlying_text = f"underlying 1h {ur1:+.2%}"
    reason = (
        f"APY z-score {apy_z:+.2f}σ; PT is {distance * 100:.2f}% away from its median; "
        f"1h {r1:+.2%}; 4h {r4:+.2%}; {underlying_text}."
    )

    return Signal(
        market=latest.market, name=latest.name, side=direction, kind="MEAN_REVERSION",
        confidence=confidence, entry=latest.pt_price, target=target, stop=stop,
        gross_return=gross, estimated_cost=round_trip_cost, expected_net_return=net,
        expected_net_pnl=pnl, capital=capital, holding_min=30, holding_max=1440,
        apy_now=latest.implied_apy, apy_z=apy_z, price_return_1h=r1,
        price_return_4h=r4, underlying_return_1h=ur1, underlying_return_4h=ur4,
        liquidity_usd=latest.liquidity_usd, reason=reason, distance_to_median=distance,
    )

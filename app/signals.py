from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from math import log
from statistics import mean, median, pstdev
from typing import Sequence

from .history import Snapshot

# Bar window used by detect_signal / diagnostics / opportunities (history[-288:]).
# 4h confirmation lookbacks fit inside this window at 15-minute (and faster) cadence.
SIGNAL_HISTORY_BARS = 288


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
    price_z: float
    price_return_1h: float
    price_return_4h: float
    underlying_return_1h: float | None
    underlying_return_4h: float | None
    liquidity_usd: float
    days_to_expiry: float
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


def _value_at_or_before(history: list[Snapshot], minutes: int, max_gap_minutes: int = 30) -> Snapshot | None:
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
    if best_delta is None or best_delta > max_gap_minutes * 60:
        return None
    return best


def _return(current: float | None, old: float | None) -> float | None:
    if current is None or old in (None, 0):
        return None
    return current / old - 1.0


def _asset_price(s: Snapshot | None) -> float | None:
    # v0.6+ research must use the explicitly stored PT/accounting asset observation.
    # Never reconstruct it from legacy USD prints: doing so can mix two
    # independently-timed feeds and create artificial dislocations.
    if s is None:
        return None
    if (
        s.pt_price_asset is not None
        and s.pt_price_asset > 0
        and s.price_basis == "ACCOUNTING_ASSET"
    ):
        return s.pt_price_asset
    return None


def _apy_bucket(days: float | None) -> str:
    if days is None:
        return "unknown"
    if days < 45:
        return "21-45d"
    if days < 90:
        return "45-90d"
    if days < 180:
        return "90-180d"
    if days < 365:
        return "180-365d"
    if days < 730:
        return "365-730d"
    return "730d+"


def detect_signal(
    history: list[Snapshot],
    capital: float,
    min_liquidity: float,
    estimated_round_trip_cost: float,
    min_net_return: float,
    min_price_z: float,
    min_apy_z: float,
    underlying_adverse_1h: float = 0.005,
    underlying_adverse_4h: float = 0.012,
    min_days_to_expiry: float = 21.0,
    max_days_to_expiry: float = 730.0,
    max_snapshot_gap_minutes: int = 30,
) -> Signal | None:
    if len(history) < 24:
        return None
    latest = history[-1]
    if not latest.collection_complete:
        return None
    current_asset = _asset_price(latest)
    if current_asset is None or latest.implied_apy is None:
        return None
    if latest.liquidity_usd is None or latest.liquidity_usd < min_liquidity:
        return None
    if latest.days_to_expiry is None:
        return None
    if not min_days_to_expiry <= latest.days_to_expiry <= max_days_to_expiry:
        return None

    # Missing 1h/4h observations are normal around collection gaps. They are
    # supporting confirmation signals, not a reason to discard the market
    # before its core statistical test runs.
    p1 = _value_at_or_before(history, 60, max(max_snapshot_gap_minutes, 30))
    p4 = _value_at_or_before(history, 240, max(max_snapshot_gap_minutes, 60))
    p1_asset = _asset_price(p1) if p1 else None
    p4_asset = _asset_price(p4) if p4 else None
    r1 = _return(current_asset, p1_asset)
    r4 = _return(current_asset, p4_asset)
    ur1 = _return(latest.underlying_price_usd, p1.underlying_price_usd if p1 else None)
    ur4 = _return(latest.underlying_price_usd, p4.underlying_price_usd if p4 else None)

    window = history[-288:]
    # _asset_price() only returns canonical ACCOUNTING_ASSET observations.
    # Keep all such observations for this market; the raw accounting_asset_id
    # field may differ in legacy rows even though the stored PT/accounting
    # asset valuation is already normalized.
    asset_prices = [_asset_price(x) for x in window]
    log_prices = [log(x) for x in asset_prices if x is not None and x > 0]
    if len(log_prices) < 20:
        return None
    price_z = _zscore(log_prices[:-1], log(current_asset))
    if price_z is None or price_z > -min_price_z:
        return None

    bucket = _apy_bucket(latest.days_to_expiry)
    bucket_apys = [
        x.implied_apy for x in window[:-1]
        if x.implied_apy is not None and _apy_bucket(x.days_to_expiry) == bucket
    ]
    apy_z = _zscore(bucket_apys, latest.implied_apy)
    if apy_z is None or apy_z < min_apy_z:
        return None

    prices = [x for x in asset_prices if x is not None and x > 0]
    ref_price = median(prices)
    distance = ref_price / current_asset - 1.0
    if distance <= 0:
        return None

    # The median is a reference point, not a guaranteed target. We only use a
    # conservative 50% retracement as the paper expectation and then require
    # that it clears estimated round-trip cost.
    target = current_asset * (1.0 + distance * 0.50)
    gross = target / current_asset - 1.0
    net = gross - estimated_round_trip_cost
    if net < min_net_return:
        return None

    if r1 < -max(0.003, distance * 0.80):
        return None

    if ur1 is not None and ur1 < -underlying_adverse_1h:
        return None
    if ur4 is not None and ur4 < -underlying_adverse_4h:
        return None

    stop = current_asset * (1.0 - distance * 0.75)
    confidence = 60 + min(20, int(abs(price_z) * 5))
    if apy_z >= min_apy_z + 1:
        confidence += 5
    if latest.liquidity_usd >= min_liquidity * 5:
        confidence += 5
    confidence = min(confidence, 95)

    pnl = capital * net
    underlying_text = "underlying n/a" if ur1 is None else f"underlying 1h {ur1:+.2%}"
    reason = (
        f"PT/accounting asset price z={price_z:+.2f}σ; APY z={apy_z:+.2f}σ in {bucket}; "
        f"discount to median {distance:+.2%}; 1h {r1:+.2%}; 4h {r4:+.2%}; {underlying_text}."
    )

    return Signal(
        market=latest.market,
        name=latest.name,
        side="BUY",
        kind="MEAN_REVERSION",
        confidence=confidence,
        entry=current_asset,
        target=target,
        stop=stop,
        gross_return=gross,
        estimated_cost=estimated_round_trip_cost,
        expected_net_return=net,
        expected_net_pnl=pnl,
        capital=capital,
        holding_min=30,
        holding_max=1440,
        apy_now=latest.implied_apy,
        apy_z=apy_z,
        price_z=price_z,
        price_return_1h=r1,
        price_return_4h=r4,
        underlying_return_1h=ur1,
        underlying_return_4h=ur4,
        liquidity_usd=latest.liquidity_usd,
        days_to_expiry=latest.days_to_expiry,
        reason=reason,
        distance_to_median=distance,
    )

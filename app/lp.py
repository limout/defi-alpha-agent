from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime, timedelta
from statistics import median
from typing import Sequence

from .config import settings
from .history import HistoryStore, Snapshot


WINDOW_HOURS = 48
SNAPSHOT_INTERVAL_MINUTES = 15
MIN_COVERAGE_FRAC = 0.75
EXPECTED_OBSERVATIONS = WINDOW_HOURS * 60 // SNAPSHOT_INTERVAL_MINUTES  # 192
MIN_BASELINE_OBS = math.ceil(EXPECTED_OBSERVATIONS * MIN_COVERAGE_FRAC)  # 144
SPIKE_MULTIPLE = 1.5
STRONG_SPIKE_MULTIPLE = 2.0
STATUS_NORMAL = "NORMAL"
STATUS_SPIKE = "SPIKE"
STATUS_STRONG = "STRONG SPIKE"


def _dt(value: str) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        from datetime import timezone

        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def daily_usd(apy: float, capital: float = 5_000.0) -> float:
    return capital * apy / 365.0


def extra_daily_usd(current: float, baseline: float, capital: float = 5_000.0) -> float:
    return capital * max(current - baseline, 0.0) / 365.0


def classify_multiple(multiple: float) -> str:
    if multiple >= STRONG_SPIKE_MULTIPLE:
        return STATUS_STRONG
    if multiple >= SPIKE_MULTIPLE:
        return STATUS_SPIKE
    return STATUS_NORMAL


@dataclass
class LpRow:
    market: str
    name: str
    chain_id: int
    current_lp_apy: float
    baseline_lp_apy: float
    lp_apy_multiple: float
    daily_usd_5000: float
    extra_daily_usd_5000: float
    liquidity_usd: float
    n: int
    status: str
    days_to_expiry: float | None


def analyze_market(
    history: Sequence[Snapshot],
    min_liquidity: float = 1_000_000.0,
    capital: float = 5_000.0,
) -> LpRow | None:
    if not history:
        return None
    latest = history[-1]
    current = latest.lp_apy
    if current is None or current <= 0:
        return None
    if latest.liquidity_usd is None or latest.liquidity_usd < min_liquidity:
        return None
    latest_ts = _dt(latest.timestamp)
    if latest_ts is None:
        return None
    window_start = latest_ts - timedelta(hours=WINDOW_HOURS)
    previous: list[float] = []
    for item in history[:-1]:
        ts = _dt(item.timestamp)
        if ts is None or ts < window_start or ts > latest_ts:
            continue
        apy = item.lp_apy
        if apy is None or apy <= 0:
            continue
        previous.append(float(apy))
    if len(previous) < MIN_BASELINE_OBS:
        return None
    baseline = float(median(previous))
    if baseline <= 0:
        return None
    multiple = current / baseline
    return LpRow(
        market=latest.market,
        name=latest.name,
        chain_id=int(latest.chain_id),
        current_lp_apy=float(current),
        baseline_lp_apy=baseline,
        lp_apy_multiple=multiple,
        daily_usd_5000=daily_usd(float(current), capital),
        extra_daily_usd_5000=extra_daily_usd(float(current), baseline, capital),
        liquidity_usd=float(latest.liquidity_usd),
        n=len(previous),
        status=classify_multiple(multiple),
        days_to_expiry=latest.days_to_expiry,
    )


def analyze_store(store: HistoryStore) -> list[LpRow]:
    rows: list[LpRow] = []
    # 48h at 15m ≈ 192; keep a buffer for faster polling.
    lookback = EXPECTED_OBSERVATIONS + 40
    for market in store.markets():
        history = store.recent(market, lookback)
        row = analyze_market(
            history,
            min_liquidity=settings.alpha_min_liquidity_usd,
            capital=settings.paper_capital_usd,
        )
        if row:
            rows.append(row)
    rows.sort(key=lambda x: x.lp_apy_multiple, reverse=True)
    return rows


def _fmt_liq(value: float) -> str:
    if value >= 1_000_000:
        return f"${value / 1_000_000:.1f}M"
    if value >= 1_000:
        return f"${value / 1_000:.0f}k"
    return f"${value:.0f}"


def print_lp_report(rows: list[LpRow]) -> None:
    spikes = [x for x in rows if x.status != STATUS_NORMAL]
    strong = [x for x in rows if x.status == STATUS_STRONG]
    print("\n=== LP APY SPIKES ===")
    print("Research signals only. Daily $ is 5000 * APY / 365, not a forecast or a trade.")
    if not spikes:
        print("No significant LP APY spike found.")
    else:
        _print_table(spikes)
    print(
        f"eligible={len(rows)}  spikes>=1.5x={len(spikes)}  strong>=2.0x={len(strong)}"
    )
    near = [x for x in rows if x.status == STATUS_NORMAL][:10]
    print("\n=== LP APY SPIKES / NEAR MISSES ===")
    if not near:
        print("No eligible near-misses (need 48h LP APY history, liq>=$1M, APY>0).")
        return
    _print_table(near)


def _print_table(rows: Sequence[LpRow]) -> None:
    print(
        f"{'market':<14} {'APY':>7} {'baseline':>9} {'xbase':>6} "
        f"{'daily@$5k':>10} {'extra/day':>10} {'liq':>8} {'n':>4} status"
    )
    for row in rows:
        print(
            f"{row.name[:14]:<14} {row.current_lp_apy:6.1%} {row.baseline_lp_apy:8.1%} "
            f"{row.lp_apy_multiple:5.2f}x ${row.daily_usd_5000:8.2f} "
            f"${row.extra_daily_usd_5000:8.2f} {_fmt_liq(row.liquidity_usd):>8} "
            f"{row.n:4d} {row.status}"
        )


def run_lp(store: HistoryStore) -> list[LpRow]:
    rows = analyze_store(store)
    print_lp_report(rows)
    return rows

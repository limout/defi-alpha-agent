"""Experimental broad mean-reversion scan on stored snapshots.

Read-only research. Does not call detect_signal() and does not write to Neon.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from statistics import mean, median

from .backtest import (
    HORIZONS,
    _asset_price,
    _entry_ref_price,
    _future_at,
    _ts,
    distance_bp,
)
from .config import settings
from .history import HistoryStore, Snapshot
from .signals import BACKTEST_HISTORY_BARS

# Research gate only. Production detect_signal thresholds are not used here.
MIN_ABS_DISTANCE_BP = 60.0
EMBARGO_MIN = max(HORIZONS)  # 24h per market after a qualifying entry
ROUND_TRIP_COST = 0.0020  # existing conservative 20 bp cost
CAPITAL = 5_000.0


@dataclass
class MeanRevHorizon:
    horizon_min: int
    price: float | None
    distance_bp: float | None
    distance_change_bp: float | None
    raw_return: float | None
    net_return: float | None
    pnl_usd: float | None


@dataclass
class MeanRevEvent:
    market: str
    name: str
    opened_at: str
    entry_price: float
    entry_distance_bp: float
    ref_price: float
    side: str
    horizons: list[MeanRevHorizon] = field(default_factory=list)


@dataclass
class HorizonStats:
    horizon_min: int
    n: int
    win_rate: float | None
    mean_net: float | None
    median_net: float | None
    mean_pnl: float | None
    total_pnl: float | None
    mean_distance_change: float | None
    median_distance_change: float | None


def _entry_ok(snap: Snapshot) -> bool:
    if not snap.collection_complete:
        return False
    if _asset_price(snap) is None:
        return False
    if snap.liquidity_usd is None or snap.liquidity_usd < settings.alpha_min_liquidity_usd:
        return False
    if snap.days_to_expiry is None:
        return False
    return settings.alpha_min_days_to_expiry <= snap.days_to_expiry <= settings.alpha_max_days_to_expiry


def _horizon_point(
    horizon_min: int,
    entry: float,
    entry_distance_bp: float,
    ref: float,
    side: str,
    close: Snapshot | None,
) -> MeanRevHorizon:
    if close is None or not close.collection_complete or close.days_to_expiry is None:
        return MeanRevHorizon(
            horizon_min=horizon_min,
            price=None,
            distance_bp=None,
            distance_change_bp=None,
            raw_return=None,
            net_return=None,
            pnl_usd=None,
        )
    price = _asset_price(close)
    dist = distance_bp(ref, price)
    change = None if dist is None else dist - entry_distance_bp
    raw = None if price is None or entry <= 0 else price / entry - 1.0
    if raw is None:
        net = None
    else:
        trade = raw if side == "CHEAP" else -raw
        net = trade - ROUND_TRIP_COST
    pnl = None if net is None else CAPITAL * net
    return MeanRevHorizon(
        horizon_min=horizon_min,
        price=price,
        distance_bp=dist,
        distance_change_bp=change,
        raw_return=raw,
        net_return=net,
        pnl_usd=pnl,
    )


def scan_market(history: list[Snapshot]) -> list[MeanRevEvent]:
    """Point-in-time scan of one market. Prefix history[:i+1] only for the median."""
    events: list[MeanRevEvent] = []
    if len(history) < settings.backtest_min_history:
        return events
    next_ok = None
    for i in range(settings.backtest_min_history, len(history)):
        if next_ok is not None and _ts(history[i].timestamp) < next_ok:
            continue
        snap = history[i]
        if not _entry_ok(snap):
            continue
        entry = _asset_price(snap)
        ref = _entry_ref_price(history, i)
        if entry is None or ref is None:
            continue
        entry_d = distance_bp(ref, entry)
        if entry_d is None or abs(entry_d) < MIN_ABS_DISTANCE_BP:
            continue
        side = "CHEAP" if entry_d > 0 else "RICH"
        points: list[MeanRevHorizon] = []
        for horizon in HORIZONS:
            close = _future_at(history, i, horizon)
            points.append(_horizon_point(horizon, entry, entry_d, ref, side, close))
        events.append(
            MeanRevEvent(
                market=snap.market,
                name=snap.name,
                opened_at=snap.timestamp,
                entry_price=entry,
                entry_distance_bp=entry_d,
                ref_price=ref,
                side=side,
                horizons=points,
            )
        )
        next_ok = _ts(snap.timestamp) + EMBARGO_MIN * 60
    return events


def run_meanrev(store: HistoryStore) -> list[MeanRevEvent]:
    events: list[MeanRevEvent] = []
    for market in store.markets():
        history = store.recent(market, BACKTEST_HISTORY_BARS)
        events.extend(scan_market(history))
    return events


def _stats(rows: list[MeanRevHorizon], horizon: int) -> HorizonStats:
    group = [
        r for r in rows
        if r.horizon_min == horizon and r.net_return is not None and r.distance_change_bp is not None
    ]
    if not group:
        return HorizonStats(
            horizon_min=horizon,
            n=0,
            win_rate=None,
            mean_net=None,
            median_net=None,
            mean_pnl=None,
            total_pnl=None,
            mean_distance_change=None,
            median_distance_change=None,
        )
    wins = [r for r in group if r.net_return is not None and r.net_return > 0]
    nets = [r.net_return for r in group if r.net_return is not None]
    pnls = [r.pnl_usd for r in group if r.pnl_usd is not None]
    chg = [r.distance_change_bp for r in group if r.distance_change_bp is not None]
    return HorizonStats(
        horizon_min=horizon,
        n=len(group),
        win_rate=len(wins) / len(group),
        mean_net=mean(nets),
        median_net=median(nets),
        mean_pnl=mean(pnls),
        total_pnl=sum(pnls),
        mean_distance_change=mean(chg),
        median_distance_change=median(chg),
    )


def _print_stats(title: str, events: list[MeanRevEvent]) -> None:
    print(f"\n--- {title} ---")
    print(f"Unique events in this slice: {len(events)}")
    if not events:
        print("No observations.")
        return
    rows = [p for e in events for p in e.horizons]
    for horizon in HORIZONS:
        s = _stats(rows, horizon)
        hours = horizon // 60
        print(f"\n  {hours}h")
        print(f"    observations:     {s.n}")
        if s.n == 0:
            print("    (no completed horizon prints)")
            continue
        print(f"    win rate:         {s.win_rate:.1%}")
        print(f"    mean net:         {s.mean_net:+.3%}")
        print(f"    median net:       {s.median_net:+.3%}")
        print(f"    mean P&L @$5000:  ${s.mean_pnl:+.2f}")
        print(f"    total P&L @$5000: ${s.total_pnl:+.2f}")
        print(f"    mean dist change: {s.mean_distance_change:+.1f} bp")
        print(f"    median dist chg:  {s.median_distance_change:+.1f} bp")


def print_meanrev_report(events: list[MeanRevEvent]) -> None:
    print("\n=== BROAD MEAN-REVERSION EXPERIMENT (research only) ===")
    print("Not production. Does not call detect_signal(). No DB writes.")
    print("Price: canonical PT/accounting asset. Median: past-only, frozen at entry.")
    print(
        f"Entry gate: |distance_bp| >= {MIN_ABS_DISTANCE_BP:.0f} bp. "
        f"Liquidity >= ${settings.alpha_min_liquidity_usd:,.0f}. "
        f"TTM {settings.alpha_min_days_to_expiry:.0f}-{settings.alpha_max_days_to_expiry:.0f}d. "
        "collection_complete + canonical bar."
    )
    print(
        f"Spacing/embargo: {EMBARGO_MIN // 60}h per market after a qualifying entry "
        f"({EMBARGO_MIN} minutes). One prolonged dislocation is one event."
    )
    print(
        f"CHEAP (distance>0) = long PT; RICH (distance<0) = short PT. "
        f"net = signed gross - {ROUND_TRIP_COST:.2%} (20 bp)."
    )
    print("price_z, apy_z, and 1h continuation are not used.")
    print(f"\nUnique observations (events): {len(events)}")
    if len(events) < 30:
        print("Sample is still small; criteria were not changed.")
    if not events:
        return

    cheap = [e for e in events if e.side == "CHEAP"]
    rich = [e for e in events if e.side == "RICH"]
    print(f"CHEAP events: {len(cheap)}  RICH events: {len(rich)}")
    print("\n--- OBSERVATIONS ---")
    print(
        f"{'market':16} {'opened_at':25} {'side':5} {'d0_bp':8} "
        f"{'d1h':8} {'d4h':8} {'d12h':8} {'d24h':8} "
        f"{'n1h':8} {'n4h':8} {'n12h':8} {'n24h':8}"
    )
    for e in events:
        by_h = {p.horizon_min: p for p in e.horizons}

        def _d(h: int) -> str:
            v = by_h[h].distance_bp
            return "n/a" if v is None else f"{v:+.1f}"

        def _n(h: int) -> str:
            v = by_h[h].net_return
            return "n/a" if v is None else f"{v:+.3%}"

        print(
            f"{e.name[:16]:16} {e.opened_at[:25]:25} {e.side:5} "
            f"{e.entry_distance_bp:+8.1f} "
            f"{_d(60):8} {_d(240):8} {_d(720):8} {_d(1440):8} "
            f"{_n(60):8} {_n(240):8} {_n(720):8} {_n(1440):8}"
        )
    _print_stats("ALL EVENTS", events)
    _print_stats("CHEAP only (long)", cheap)
    _print_stats("RICH only (short)", rich)

    by_name: dict[str, list[MeanRevEvent]] = {}
    for e in events:
        by_name.setdefault(e.name, []).append(e)
    print("\n--- BY MARKET ---")
    for name, group in sorted(by_name.items(), key=lambda kv: -len(kv[1])):
        print(f"\n{name}  n={len(group)}  market={group[0].market[:18]}...")
        rows = [p for e in group for p in e.horizons]
        for horizon in HORIZONS:
            s = _stats(rows, horizon)
            hours = horizon // 60
            if s.n == 0:
                print(f"  {hours}h  n=0")
                continue
            print(
                f"  {hours}h  n={s.n}  win={s.win_rate:.1%}  "
                f"mean_net={s.mean_net:+.3%}  med_net={s.median_net:+.3%}  "
                f"mean_dchg={s.mean_distance_change:+.1f}bp  "
                f"PnL=${s.total_pnl:+.2f}"
            )

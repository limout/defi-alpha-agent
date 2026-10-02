from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from statistics import mean, median

from .config import settings
from .history import HistoryStore, Snapshot
from .signals import BACKTEST_HISTORY_BARS, SIGNAL_HISTORY_BARS, _past_canonical_prices, detect_signal


HORIZONS = (60, 240, 720, 1440)


@dataclass
class BacktestTrade:
    market: str
    name: str
    opened_at: str
    closed_at: str
    horizon_min: int
    return_pct: float
    pnl_usd: float
    skipped_gap: bool = False


@dataclass
class HorizonPoint:
    horizon_min: int
    price: float | None
    distance_bp: float | None
    distance_change_bp: float | None
    timestamp: str | None = None


@dataclass
class EventTrajectory:
    market: str
    name: str
    opened_at: str
    entry_price: float
    entry_distance_bp: float
    ref_price: float
    horizons: list[HorizonPoint]


def distance_bp(ref: float, price: float | None) -> float | None:
    """PT/accounting residual in bp vs a frozen reference: (ref/price - 1)*10000."""
    if price is None or price <= 0 or ref <= 0:
        return None
    return (ref / price - 1.0) * 10_000.0


def _ts(value: str) -> float:
    return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()


def _asset_price(s: Snapshot | None) -> float | None:
    # Backtests must use only explicitly stored v0.6 PT/accounting asset observations.
    # Do not reconstruct legacy v0.5.8 rows from two USD feeds.
    if s is None:
        return None
    if (
        s.pt_price_asset is not None
        and s.pt_price_asset > 0
        and s.price_basis == "ACCOUNTING_ASSET"
    ):
        return s.pt_price_asset
    return None


def _future_at(history: list[Snapshot], i: int, horizon_min: int, max_gap_minutes: int = 23) -> Snapshot | None:
    target = _ts(history[i].timestamp) + horizon_min * 60
    best = None
    best_delta = None
    for snap in history[i + 1:]:
        delta = _ts(snap.timestamp) - target
        if delta < 0:
            continue
        if best_delta is None or delta < best_delta:
            best, best_delta = snap, delta
        break
    if best is None or best_delta is None or best_delta > max_gap_minutes * 60:
        return None
    return best


def run_backtest(store: HistoryStore) -> list[BacktestTrade]:
    trades: list[BacktestTrade] = []
    horizons = HORIZONS
    for market in store.markets():
        history = store.recent(market, BACKTEST_HISTORY_BARS)
        if len(history) < settings.backtest_min_history:
            continue
        next_event_ts = None
        for i in range(settings.backtest_min_history, len(history) - 1):
            if next_event_ts is not None and _ts(history[i].timestamp) < next_event_ts:
                continue
            if not history[i].collection_complete:
                continue
            sig = detect_signal(
                history[: i + 1],
                settings.paper_capital_usd,
                settings.alpha_min_liquidity_usd,
                settings.backtest_cost,
                settings.alpha_min_net_return,
                settings.alpha_min_price_z,
                settings.alpha_min_apy_z,
                settings.underlying_adverse_1h,
                settings.underlying_adverse_4h,
                settings.alpha_min_days_to_expiry,
                settings.alpha_max_days_to_expiry,
                settings.max_snapshot_gap_minutes,
                settings.alpha_min_price_distance,
            )
            if not sig or sig.side != "BUY":
                continue
            # v0.6 research event: require complete, explicitly stored asset
            # price and TTM at the entry bar.
            if history[i].days_to_expiry is None or history[i].days_to_expiry < settings.alpha_min_days_to_expiry:
                continue
            entry = _asset_price(history[i])
            if entry is None:
                continue
            event_has_observation = False
            for horizon in horizons:
                close = _future_at(history, i, horizon)
                if close is None or not close.collection_complete or close.days_to_expiry is None:
                    continue
                exit_price = _asset_price(close)
                if exit_price is None:
                    continue
                gross = exit_price / entry - 1.0
                net = gross - settings.backtest_cost
                trades.append(
                    BacktestTrade(
                        market=market,
                        name=sig.name,
                        opened_at=history[i].timestamp,
                        closed_at=close.timestamp,
                        horizon_min=horizon,
                        return_pct=net,
                        pnl_usd=settings.paper_capital_usd * net,
                    )
                )
                event_has_observation = True
            # Treat a signal as one research event. Do not count another signal
            # on the same market for the next 24h, otherwise overlapping labels
            # inflate the apparent sample size and are strongly autocorrelated.
            if event_has_observation:
                next_event_ts = _ts(history[i].timestamp) + max(horizons) * 60
    return trades


def unique_events(trades: list[BacktestTrade]) -> list[tuple[str, str, str]]:
    seen: set[tuple[str, str]] = set()
    events: list[tuple[str, str, str]] = []
    for t in trades:
        key = (t.market, t.opened_at)
        if key in seen:
            continue
        seen.add(key)
        events.append((t.market, t.name, t.opened_at))
    return events


def _report_group(trades: list[BacktestTrade], horizon: int) -> None:
    group = [t for t in trades if t.horizon_min == horizon]
    hours = horizon // 60 if horizon >= 60 else horizon
    print(f"\n--- {hours}h horizon observations ---")
    if not group:
        print("No completed observations.")
        return
    wins = [t for t in group if t.pnl_usd > 0]
    pnl = sum(t.pnl_usd for t in group)
    print(f"Observations: {len(group)}  (one per unique event that has this horizon)")
    print(f"Win rate:    {len(wins)/len(group):.1%}")
    print(f"Mean net:    {mean(t.return_pct for t in group):+.3%}")
    print(f"Median net:  {median(t.return_pct for t in group):+.3%}")
    print(f"Expectancy:  ${mean(t.pnl_usd for t in group):+.2f}")
    print(f"Total P&L:   ${pnl:+.2f}")


def print_report(trades: list[BacktestTrade]) -> None:
    print("\n=== FIXED-HORIZON PT/UNDERLYING BACKTEST ===")
    print("Point-in-time signals; asset-denominated PT; conservative cost; no target/stop overlay.")
    if not trades:
        print("No completed historical observations yet. Keep collecting history.")
        return
    events = unique_events(trades)
    horizons = HORIZONS
    print(f"Unique events (market, opened_at): {len(events)}")
    print("Each event produces up to four horizon observations: 1h, 4h, 12h, 24h.")
    print(f"Horizon observations: {len(trades)}  (max {len(events) * len(horizons)} if every event has all four)")
    for market, name, opened in events:
        n_h = sum(1 for t in trades if t.market == market and t.opened_at == opened)
        print(f"  {opened[:19].replace('T', ' ')}  {name[:30]:30}  horizons={n_h}/4")
    for horizon in horizons:
        _report_group(trades, horizon)
    print("\nNote: one event per market per 24h; horizon rows are labels of the same event, not extra samples.")
    print("This is still descriptive, not a significance test.")


def _entry_index(history: list[Snapshot], opened_at: str) -> int | None:
    for i, snap in enumerate(history):
        if snap.timestamp == opened_at:
            return i
    return None


def _entry_ref_price(history: list[Snapshot], i: int) -> float | None:
    window = history[: i + 1][-SIGNAL_HISTORY_BARS:]
    past = _past_canonical_prices(window)
    if len(past) < 20:
        return None
    return median(past)


def build_event_trajectories(
    store: HistoryStore,
    trades: list[BacktestTrade],
) -> list[EventTrajectory]:
    """Residual path vs the entry past-only median. Does not call detect_signal."""
    rows: list[EventTrajectory] = []
    for market, name, opened_at in unique_events(trades):
        history = store.recent(market, BACKTEST_HISTORY_BARS)
        i = _entry_index(history, opened_at)
        if i is None:
            continue
        entry = _asset_price(history[i])
        ref = _entry_ref_price(history, i)
        if entry is None or ref is None:
            continue
        entry_d = distance_bp(ref, entry)
        if entry_d is None:
            continue
        points: list[HorizonPoint] = []
        for horizon in HORIZONS:
            close = _future_at(history, i, horizon)
            price = _asset_price(close) if close is not None else None
            if close is not None and (
                not close.collection_complete or close.days_to_expiry is None
            ):
                price = None
            dist = distance_bp(ref, price)
            change = None if dist is None else dist - entry_d
            points.append(
                HorizonPoint(
                    horizon_min=horizon,
                    price=price,
                    distance_bp=dist,
                    distance_change_bp=change,
                    timestamp=None if close is None else close.timestamp,
                )
            )
        rows.append(
            EventTrajectory(
                market=market,
                name=name,
                opened_at=opened_at,
                entry_price=entry,
                entry_distance_bp=entry_d,
                ref_price=ref,
                horizons=points,
            )
        )
    return rows


def _fmt_px(value: float | None) -> str:
    return "n/a" if value is None else f"{value:.8f}"


def _fmt_bp(value: float | None) -> str:
    return "n/a" if value is None else f"{value:+.1f}"


def print_trajectory_report(rows: list[EventTrajectory]) -> None:
    print("\n=== RESIDUAL TRAJECTORY (diagnostic) ===")
    print("Reference = past-only PT/accounting median frozen at entry.")
    print("distance_bp = (ref / pt_asset - 1) * 10000")
    print("distance_change_bp = horizon_distance_bp - entry_distance_bp")
    print("Diagnostic only; no mean-reversion outcome is labeled.")
    if not rows:
        print("No events to plot.")
        return
    print(
        "name       opened_at           entry_px   entry_bp  "
        "1h_px      1h_bp   1h_chg  "
        "4h_px      4h_bp   4h_chg  "
        "12h_px     12h_bp  12h_chg "
        "24h_px     24h_bp  24h_chg"
    )
    for row in rows:
        by_h = {p.horizon_min: p for p in row.horizons}
        parts = [
            f"{row.name[:10]:10} {row.opened_at[:19].replace('T', ' '):19} "
            f"{row.entry_price:.8f} {_fmt_bp(row.entry_distance_bp):>8}"
        ]
        for horizon in HORIZONS:
            p = by_h.get(horizon)
            px = _fmt_px(None if p is None else p.price)
            bp = _fmt_bp(None if p is None else p.distance_bp)
            ch = _fmt_bp(None if p is None else p.distance_change_bp)
            parts.append(f" {px:>10} {bp:>7} {ch:>7}")
        print("".join(parts))
        print(
            f"  market={row.market}  ref={row.ref_price:.8f}  "
            f"entry_distance_bp={row.entry_distance_bp:+.1f}"
        )
        for horizon in HORIZONS:
            p = by_h.get(horizon)
            hours = horizon // 60
            if p is None or p.price is None:
                print(f"  {hours:>2}h  pt_asset=n/a  distance_bp=n/a  distance_change_bp=n/a")
                continue
            print(
                f"  {hours:>2}h  pt_asset={p.price:.8f}  "
                f"distance_bp={_fmt_bp(p.distance_bp)}  "
                f"distance_change_bp={_fmt_bp(p.distance_change_bp)}"
            )

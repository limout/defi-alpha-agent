from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from statistics import mean

from .config import settings
from .history import HistoryStore
from .signals import detect_signal


@dataclass
class BacktestTrade:
    market: str
    name: str
    opened_at: str
    closed_at: str
    reason: str
    return_pct: float
    pnl_usd: float
    hold_min: float


def _ts(value: str) -> float:
    return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()


def run_backtest(store: HistoryStore) -> list[BacktestTrade]:
    trades: list[BacktestTrade] = []
    for market in store.markets():
        history = store.recent(market, 2000)
        i = settings.backtest_min_history
        while i < len(history) - 1:
            sig = detect_signal(
                history[: i + 1], settings.paper_capital_usd,
                settings.alpha_min_liquidity_usd, settings.backtest_cost,
                settings.alpha_min_net_return, settings.alpha_min_apy_z,
                settings.underlying_adverse_1h, settings.underlying_adverse_4h,
            )
            if not sig or sig.side != "BUY":
                i += 1
                continue
            entry = sig.entry
            opened = history[i]
            close = None
            reason = "TIMEOUT"
            max_ts = _ts(opened.timestamp) + settings.backtest_max_hold_min * 60
            j = i + 1
            while j < len(history):
                snap = history[j]
                ts = _ts(snap.timestamp)
                if ts > max_ts:
                    break
                if snap.pt_price is not None:
                    if snap.pt_price >= sig.target:
                        close, reason = snap, "TARGET"; break
                    if snap.pt_price <= sig.stop:
                        close, reason = snap, "STOP"; break
                j += 1
            if close is None:
                if j < len(history):
                    close = history[j]
                else:
                    break
            if close.pt_price is None:
                i = max(i + 1, j)
                continue
            gross = close.pt_price / entry - 1
            net = gross - settings.backtest_cost
            hold = max(0.0, (_ts(close.timestamp) - _ts(opened.timestamp)) / 60)
            trades.append(BacktestTrade(market, sig.name, opened.timestamp, close.timestamp, reason, net, settings.paper_capital_usd * net, hold))
            i = max(j, i + 1)
    return trades


def print_report(trades: list[BacktestTrade]) -> None:
    print("\n=== LOCAL PAPER BACKTEST ===")
    if not trades:
        print("No completed historical trades yet. Keep collecting history.")
        return
    wins = [t for t in trades if t.pnl_usd > 0]
    losses = [t for t in trades if t.pnl_usd <= 0]
    pnl = sum(t.pnl_usd for t in trades)
    avg = mean(t.pnl_usd for t in trades)
    avg_win = mean(t.pnl_usd for t in wins) if wins else 0
    avg_loss = mean(t.pnl_usd for t in losses) if losses else 0
    equity = 0.0; peak = 0.0; max_dd = 0.0
    for t in trades:
        equity += t.pnl_usd; peak = max(peak, equity); max_dd = min(max_dd, equity - peak)
    print(f"Trades: {len(trades)}")
    print(f"Win rate: {len(wins)/len(trades):.1%}")
    print(f"Avg winner: ${avg_win:+.2f}")
    print(f"Avg loser:  ${avg_loss:+.2f}")
    print(f"Expectancy: ${avg:+.2f}")
    print(f"Total P&L:  ${pnl:+.2f}")
    print(f"Max DD:     ${max_dd:+.2f}")
    print(f"Avg hold:   {mean(t.hold_min for t in trades):.1f} min")
    print("\nLast trades:")
    for t in trades[-10:]:
        print(f"{t.reason:<7} {t.name[:28]:28} {t.return_pct:+.2%} ${t.pnl_usd:+.2f} hold={t.hold_min:.0f}m")

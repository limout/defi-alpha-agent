from __future__ import annotations

import asyncio
import json
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path

from .config import settings
from .history import HistoryStore, Snapshot
from .http import HttpClient
from .paper import PaperLedger
from .diagnostics import build_states, print_states
from .signals import Signal, detect_signal
from .sources.pendle import PendleClient
from .trading import TradeSimulator


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


async def collect_once(http: HttpClient, store: HistoryStore):
    pendle = PendleClient(http)
    markets, diagnostics = await pendle.all_markets(settings.chain_id, chain_ids=settings.chain_ids(), min_liquidity_usd=settings.alpha_min_liquidity_usd)
    timestamp = now_iso()
    snapshots = [Snapshot(timestamp=timestamp, market=m.market_address, name=m.name, pt_price=m.pt_price_usd, implied_apy=m.implied_apy, liquidity_usd=m.liquidity_usd, expiry=m.expiry, chain_id=m.chain_id, underlying_price_usd=m.underlying_price_usd) for m in markets if m.implied_apy is not None]
    inserted = store.insert(snapshots)
    return markets, {**diagnostics, "snapshots_inserted": inserted}


def evaluate(markets, store):
    signals = []
    for market in markets:
        history = store.recent(market.market_address, 500)
        signal = detect_signal(history, settings.paper_capital_usd, settings.alpha_min_liquidity_usd, settings.alpha_round_trip_cost, settings.alpha_min_net_return, settings.alpha_min_apy_z, settings.underlying_adverse_1h, settings.underlying_adverse_4h)
        if signal:
            signals.append(signal)
    signals.sort(key=lambda x: x.expected_net_pnl, reverse=True)
    return signals


def print_signals(signals):
    print("\n=== ALPHA SIGNALS ===")
    if not signals:
        print("No validated statistical signal yet.")
        return
    for i, s in enumerate(signals[:10], 1):
        print(f"{i:>2}. {s.side:<4} {s.name[:32]:32} {s.kind:<15} model_net={s.expected_net_return:+.2%} PnL=${s.expected_net_pnl:+.2f} APY-z={s.apy_z:+.2f}σ conf={s.confidence}%")
        print(f"    entry={s.entry:.8f} target={s.target:.8f} stop={s.stop:.8f} liq=${s.liquidity_usd:,.0f}")
        print(f"    {s.reason}")
        if s.underlying_return_1h is not None:
            print(f"    underlying: 1h={s.underlying_return_1h:+.2%} 4h={s.underlying_return_4h:+.2%}")


def write_signal_snapshot(signals):
    path = Path(settings.alpha_snapshot_dir); path.mkdir(parents=True, exist_ok=True)
    filename = path / datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ.json")
    filename.write_text(json.dumps([asdict(x) for x in signals], indent=2), encoding="utf-8")


async def simulate_candidates(markets, signals, pendle, ledger):
    simulator = TradeSimulator(pendle)
    print("\n=== EXECUTION QUOTES ===")
    for signal in signals[:5]:
        market = next((m for m in markets if m.market_address == signal.market), None)
        if market is None or signal.side != "BUY":
            continue
        if ledger.has_open(signal.market):
            print(f"SKIP {signal.name}: paper position already open")
            continue
        try:
            quote = await simulator.round_trip(market, settings.paper_capital_usd)
        except Exception as exc:
            print(f"QUOTE ERROR {signal.name}: {type(exc).__name__}: {exc}")
            continue
        if not quote.ok or quote.entry is None or quote.exit is None:
            print(f"SKIP {signal.name}: {quote.reason}")
            continue
        print(f"{signal.name}: entry quote -> PT {quote.entry.output_amount_usd:,.2f}; exit quote -> ${quote.exit.output_amount_usd:,.2f}; fees=${quote.fees_usd:.2f}; impact={quote.entry.price_impact + quote.exit.price_impact:.3%}; net=${quote.net_pnl_usd:+.2f} ({quote.net_return:+.2%})")
        # Only open a paper trade if the real two-sided quote still clears the threshold.
        if quote.net_return < settings.alpha_min_net_return:
            print("  REJECT: real quote does not clear minimum net return")
            continue
        ledger.open_trade(signal.market, signal.name, settings.paper_capital_usd, quote.entry.output_amount_raw, signal.entry, signal.target, signal.stop)
        print("  PAPER OPEN")


async def mark_paper_positions(markets, ledger, pendle):
    simulator = TradeSimulator(pendle)
    open_trades = ledger.open_trades()
    if not open_trades:
        return
    print("\n=== PAPER POSITIONS ===")
    for trade in open_trades:
        market = next((m for m in markets if m.market_address == trade.market), None)
        if market is None or market.pt_price_usd is None:
            continue
        hit_target = market.pt_price_usd >= trade.target_usd
        hit_stop = market.pt_price_usd <= trade.stop_usd
        if not hit_target and not hit_stop:
            print(f"OPEN #{trade.id} {trade.name}: PT={market.pt_price_usd:.8f} target={trade.target_usd:.8f} stop={trade.stop_usd:.8f}")
            continue
        reason = "TARGET" if hit_target else "STOP"
        try:
            exit_quote = await simulator.quote_sell(market, trade.pt_amount_raw)
        except Exception as exc:
            print(f"CLOSE QUOTE ERROR #{trade.id}: {exc}")
            continue
        if exit_quote is None:
            print(f"CLOSE BLOCKED #{trade.id}: exit quote unavailable")
            continue
        pnl = exit_quote.output_amount_usd - trade.capital_usd
        ret = pnl / trade.capital_usd if trade.capital_usd else 0.0
        ledger.close_trade(trade.id, reason, pnl, ret)
        print(f"CLOSED #{trade.id} {trade.name}: {reason} PnL=${pnl:+.2f} ({ret:+.2%})")


async def run_once():
    http = HttpClient(); store = HistoryStore(settings.history_db); ledger = PaperLedger(settings.paper_db)
    try:
        print("\n=== DEFI ALPHA AGENT v0.5.7 ===")
        print("SHORT-HORIZON / PAPER MODE / REAL PENDLE QUOTES")
        print("No wallet. No approvals. No transactions.\n")
        pendle = PendleClient(http)
        markets, diagnostics = await collect_once(http, store)
        print("=== COLLECTION ===")
        print(json.dumps(diagnostics, indent=2))
        print(f"Markets stored: {len(markets)}")
        if diagnostics.get("page_errors"):
            print(f"WARNING: collection stopped after page error: {diagnostics['page_errors']}")
        print(f"History: {store.count()} snapshots / {len({m.market_address for m in markets if store.count(m.market_address)})} markets")
        print_states(build_states(markets, store), settings.diagnostic_top_n)
        await mark_paper_positions(markets, ledger, pendle)
        signals = evaluate(markets, store)
        print_signals(signals)
        await simulate_candidates(markets, signals, pendle, ledger)
        write_signal_snapshot(signals)
        print(f"\nPaper summary: {json.dumps(ledger.summary(), indent=2)}")
        print(f"History DB: {settings.history_db}")
        print(f"Paper DB: {settings.paper_db}")
    finally:
        ledger.close(); store.close(); await http.close()


async def run_daemon():
    http = HttpClient(); store = HistoryStore(settings.history_db); ledger = PaperLedger(settings.paper_db)
    try:
        print("\n=== DEFI ALPHA AGENT v0.5.7 DAEMON ===")
        print(f"Polling every {settings.poll_interval_seconds}s")
        print("PAPER MODE ONLY / REAL PENDLE QUOTES FOR CANDIDATES\n")
        while True:
            started = datetime.now(timezone.utc)
            try:
                pendle = PendleClient(http)
                markets, diagnostics = await collect_once(http, store)
                await mark_paper_positions(markets, ledger, pendle)
                signals = evaluate(markets, store)
                print(f"\n[{started.isoformat()}] stored={diagnostics['snapshots_inserted']}")
                print_states(build_states(markets, store), settings.diagnostic_top_n)
                print_signals(signals)
                await simulate_candidates(markets, signals, pendle, ledger)
                print(f"Paper summary: {ledger.summary()}")
                write_signal_snapshot(signals)
            except Exception as exc:
                print(f"Collection error: {type(exc).__name__}: {exc}")
            elapsed = (datetime.now(timezone.utc) - started).total_seconds()
            await asyncio.sleep(max(5, settings.poll_interval_seconds - elapsed))
    finally:
        ledger.close(); store.close(); await http.close()

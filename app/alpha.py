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
from .signals import SIGNAL_HISTORY_BARS, Signal, detect_signal
from .sources.pendle import PendleClient
from .trading import TradeSimulator


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


async def collect_once(http: HttpClient, store: HistoryStore):
    pendle = PendleClient(http)
    markets, diagnostics = await pendle.all_markets(settings.chain_id, chain_ids=settings.chain_ids(), min_liquidity_usd=0.0)

    # Safe legacy-history migration: only markets whose current Pendle metadata
    # proves accountingAsset == underlyingAsset can reuse old PT/underlying data.
    # Wrapper/yield-token markets are deliberately left as warm-up history.
    market_map = {
        m.market_address: (m.underlying_asset_id, m.accounting_asset_id)
        for m in markets
    }
    legacy_backfilled = store.backfill_legacy_equal_asset_markets(market_map)
    if legacy_backfilled:
        print(f"[collection] legacy history backfilled: {legacy_backfilled} snapshots (equal accounting/underlying only)", flush=True)

    timestamp = now_iso()
    snapshots = [
        Snapshot(
            timestamp=timestamp,
            market=m.market_address,
            name=m.name,
            pt_price=m.pt_price_usd,
            implied_apy=m.implied_apy,
            liquidity_usd=m.liquidity_usd,
            expiry=m.expiry,
            chain_id=m.chain_id,
            underlying_price_usd=m.underlying_price_usd,
            pt_price_asset=m.pt_price_asset,
            days_to_expiry=m.days_to_expiry,
            underlying_id=m.underlying_asset_id,
            accounting_asset_id=m.accounting_asset_id,
            price_basis=m.valuation_basis,
            source_ts=timestamp,
            collection_complete=not diagnostics.get("page_errors"),
            pt_address=m.pt_address,
            lp_apy=m.lp_apy,
        )
        for m in markets
    ]
    inserted = store.insert(snapshots)
    return markets, {**diagnostics, "snapshots_inserted": inserted}


def load_histories(markets, store: HistoryStore) -> dict[str, list[Snapshot]]:
    return store.recent_many((m.market_address for m in markets), SIGNAL_HISTORY_BARS)


def evaluate(markets, histories: dict[str, list[Snapshot]]):
    signals = []
    for market in markets:
        history = histories.get(market.market_address) or []
        signal = detect_signal(
            history, settings.paper_capital_usd, settings.alpha_min_liquidity_usd,
            settings.alpha_round_trip_cost, settings.alpha_min_net_return,
            settings.alpha_min_price_z, settings.alpha_min_apy_z,
            settings.underlying_adverse_1h, settings.underlying_adverse_4h,
            settings.alpha_min_days_to_expiry, settings.alpha_max_days_to_expiry,
            settings.max_snapshot_gap_minutes,
        )
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
        print(f"{i:>2}. {s.side:<4} {s.name[:32]:32} {s.kind:<15} model_net={s.expected_net_return:+.2%} PnL=${s.expected_net_pnl:+.2f} price-z={s.price_z:+.2f}σ APY-z={s.apy_z:+.2f}σ conf={s.confidence}%")
        print(f"    PT/accounting asset entry={s.entry:.8f} target={s.target:.8f} stop={s.stop:.8f} TTM={s.days_to_expiry:.1f}d liq=${s.liquidity_usd:,.0f}")
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
            entry = await simulator.quote_buy(market, settings.paper_capital_usd)
        except Exception as exc:
            print(f"QUOTE ERROR {signal.name}: {type(exc).__name__}: {exc}")
            continue
        if entry is None:
            print(f"SKIP {signal.name}: entry quote unavailable")
            continue
        entry_impact = entry.price_impact
        print(f"{signal.name}: entry quote -> PT {entry.output_amount_usd:,.2f}; impact={entry_impact:.3%}; routed fee=${entry.fee_usd:.2f}")
        if entry_impact > settings.quote_entry_max_impact:
            print(f"  REJECT: entry price impact {entry_impact:.3%} > {settings.quote_entry_max_impact:.3%}")
            continue
        pt_decimals = market.pt_decimals
        if pt_decimals is None:
            pt_decimals = await pendle.resolve_token_decimals(market.chain_id, market.pt_address)
            market.pt_decimals = pt_decimals
        if pt_decimals is None:
            print("  REJECT: PT decimals unavailable")
            continue
        pt_amount = int(entry.output_amount_raw) / (10 ** pt_decimals)
        if pt_amount <= 0:
            print("  REJECT: zero PT output")
            continue
        # Entry is stored in the same PT/accounting-asset units used by the
        # research signal. The real quote is USDC -> PT, so convert the exact
        # USDC capital into accounting-asset units using the market's current
        # accounting-asset USD price.
        accounting_price = market.accounting_asset_price_usd
        if accounting_price is None or accounting_price <= 0:
            print("  REJECT: accounting asset USD price unavailable")
            continue
        accounting_in = settings.paper_capital_usd / accounting_price
        entry_asset = accounting_in / pt_amount
        target_asset = signal.target
        stop_asset = signal.stop
        # Keep the legacy *_usd columns populated for compatibility, but store
        # the research/execution entry explicitly in PT/accounting asset units.
        entry_usd = settings.paper_capital_usd / pt_amount
        ledger.open_trade(
            signal.market, signal.name, settings.paper_capital_usd,
            entry.output_amount_raw, entry_usd, target_asset, stop_asset,
            entry_asset=entry_asset, target_asset=target_asset, stop_asset=stop_asset,
        )
        print(f"  PAPER OPEN: entry_asset={entry_asset:.8f} target={target_asset:.8f} stop={stop_asset:.8f}")


async def mark_paper_positions(markets, ledger, pendle):
    simulator = TradeSimulator(pendle)
    open_trades = ledger.open_trades()
    if not open_trades:
        return
    print("\n=== PAPER POSITIONS ===")
    for trade in open_trades:
        market = next((m for m in markets if m.market_address == trade.market), None)
        if market is None:
            continue
        current_asset = market.pt_price_asset
        if current_asset is None and market.pt_price_usd is not None and market.underlying_price_usd not in (None, 0):
            current_asset = market.pt_price_usd / market.underlying_price_usd
        if market is None or current_asset is None:
            continue
        target_asset = trade.target_asset if trade.target_asset is not None else trade.target_usd
        stop_asset = trade.stop_asset if trade.stop_asset is not None else trade.stop_usd
        opened_ts = datetime.fromisoformat(trade.opened_at.replace("Z", "+00:00"))
        age_min = (datetime.now(timezone.utc) - opened_ts).total_seconds() / 60.0
        hit_target = current_asset >= target_asset
        hit_stop = current_asset <= stop_asset
        hit_timeout = age_min >= settings.backtest_max_hold_min
        if not hit_target and not hit_stop and not hit_timeout:
            print(f"OPEN #{trade.id} {trade.name}: PT/accounting asset={current_asset:.8f} target={target_asset:.8f} stop={stop_asset:.8f} age={age_min:.0f}m")
            continue
        reason = "TARGET" if hit_target else ("STOP" if hit_stop else "TIMEOUT")
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


async def run_collect():
    """15-minute production path: Pendle fetch + INSERT only. No history scans."""
    http = HttpClient()
    store = HistoryStore(database_url=settings.database_url)
    try:
        print("\n=== DEFI ALPHA COLLECT ===")
        print("Snapshot insert only. No history scan, no signals, no quotes.\n")
        markets, diagnostics = await collect_once(http, store)
        print("=== COLLECTION ===")
        print(json.dumps(diagnostics, indent=2))
        print(f"Markets stored: {len(markets)}")
        if diagnostics.get("page_errors"):
            print(f"WARNING: collection stopped after page error: {diagnostics['page_errors']}")
        print(f"History snapshots: {store.count()}")
        print(f"History DB: {store.info()}")
    finally:
        store.close()
        await http.close()


async def run_once():
    """Manual analysis: live Pendle metadata + one history fetch per market. No INSERT."""
    http = HttpClient(); store = HistoryStore(database_url=settings.database_url); ledger = PaperLedger(database_url=settings.database_url)
    try:
        print("\n=== DEFI ALPHA AGENT v0.6.20 ===")
        print("SHORT-HORIZON / PAPER MODE / REAL PENDLE QUOTES")
        print("Analysis only (no snapshot insert). No wallet. No approvals. No transactions.\n")
        pendle = PendleClient(http)
        markets, diagnostics = await pendle.all_markets(settings.chain_id, chain_ids=settings.chain_ids(), min_liquidity_usd=0.0)
        print("=== MARKETS ===")
        print(json.dumps(diagnostics, indent=2))
        print(f"Markets loaded: {len(markets)}")
        if diagnostics.get("page_errors"):
            print(f"WARNING: market fetch stopped after page error: {diagnostics['page_errors']}")
        histories = load_histories(markets, store)
        print_states(build_states(markets, histories), settings.diagnostic_top_n)
        await mark_paper_positions(markets, ledger, pendle)
        signals = evaluate(markets, histories)
        print_signals(signals)
        await simulate_candidates(markets, signals, pendle, ledger)
        write_signal_snapshot(signals)
        print(f"\nPaper summary: {json.dumps(ledger.summary(), indent=2)}")
        print(f"History DB: {store.info()}")
        print(f"Paper DB: {ledger.info()}")
    finally:
        ledger.close(); store.close(); await http.close()


async def run_daemon():
    http = HttpClient(); store = HistoryStore(database_url=settings.database_url)
    try:
        print("\n=== DEFI ALPHA AGENT v0.6.20 DAEMON ===")
        print(f"Collecting every {settings.poll_interval_seconds}s (insert only)")
        print("PAPER MODE / ANALYSIS IS A SEPARATE COMMAND\n")
        while True:
            started = datetime.now(timezone.utc)
            try:
                markets, diagnostics = await collect_once(http, store)
                print(f"\n[{started.isoformat()}] stored={diagnostics['snapshots_inserted']} markets={len(markets)}")
                if diagnostics.get("page_errors"):
                    print(f"WARNING: collection stopped after page error: {diagnostics['page_errors']}")
            except Exception as exc:
                print(f"Collection error: {type(exc).__name__}: {exc}")
            elapsed = (datetime.now(timezone.utc) - started).total_seconds()
            await asyncio.sleep(max(5, settings.poll_interval_seconds - elapsed))
    finally:
        store.close(); await http.close()

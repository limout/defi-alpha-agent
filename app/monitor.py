from __future__ import annotations

from dataclasses import dataclass
from statistics import mean, median, pstdev
from datetime import datetime, timezone

from .config import settings
from .history import HistoryStore, Snapshot
from .signals import SIGNAL_HISTORY_BARS, detect_signal, _asset_price, _value_at_or_before, _return, _apy_bucket
from .http import HttpClient
from .sources.pendle import PendleClient, PTMarket
from .trading import TradeSimulator


@dataclass
class Opportunity:
    market: str
    name: str
    chain_id: int
    side: str
    pt_price_asset: float
    apy: float
    apy_z: float | None
    residual_z: float | None
    distance: float
    gross: float
    model_net: float
    gross_pnl: float
    cost_pnl: float
    model_pnl: float
    quote_cost: float | None
    quote_fee: float | None
    quote_impact: float | None
    quote_status: str
    quote_age_min: float | None
    target_asset: float
    expected_move: float
    r1: float | None
    r4: float | None
    ur1: float | None
    ur4: float | None
    liquidity: float
    observations: int
    days_to_expiry: float | None
    status: str
    pt_address: str | None = None
    pt_price_usd: float | None = None
    quote_reason: str | None = None


def _latest_pt_address(history: list[Snapshot]) -> tuple[str | None, Snapshot | None]:
    for snap in reversed(history):
        addr = (snap.pt_address or "").strip()
        if addr.lower().startswith("0x") and len(addr) >= 42:
            return addr, snap
    return None, None


def _reject_reason(history: list[Snapshot]) -> str | None:
    """First-match filter reason. Categories match `_opportunity` + history length."""
    if len(history) < settings.backtest_min_history:
        return "insufficient history"

    latest = None
    for candidate in reversed(history):
        if _asset_price(candidate) is not None:
            latest = candidate
            break
    if latest is None:
        return "missing accounting-asset price"
    if latest.implied_apy is None:
        return "missing APY"
    if latest.liquidity_usd is None:
        return "missing liquidity"

    days_to_expiry = latest.days_to_expiry
    if days_to_expiry is None and latest.expiry:
        try:
            expiry_dt = datetime.fromisoformat(latest.expiry.replace("Z", "+00:00"))
            if expiry_dt.tzinfo is None:
                expiry_dt = expiry_dt.replace(tzinfo=timezone.utc)
            days_to_expiry = max(0.0, (expiry_dt - datetime.now(timezone.utc)).total_seconds() / 86400.0)
        except ValueError:
            days_to_expiry = None
    if days_to_expiry is None or not (
        settings.alpha_min_days_to_expiry <= days_to_expiry <= settings.alpha_max_days_to_expiry
    ):
        return "TTM outside range"
    if latest.liquidity_usd < settings.alpha_min_liquidity_usd:
        return "liquidity < $1M"

    window = history[-288:]
    asset_prices = [_asset_price(x) for x in window if _asset_price(x) is not None]
    if len(asset_prices) < 20:
        return "insufficient canonical asset observations"
    return None


def _z(values: list[float], current: float | None) -> float | None:
    if current is None or len(values) < 20:
        return None
    sd = pstdev(values)
    return 0.0 if sd == 0 else (current - mean(values)) / sd


def _opportunity(history: list[Snapshot]) -> Opportunity | None:
    if not history:
        return None

    # Use the newest snapshot that is explicitly valued in accounting-asset
    # units. A malformed/fallback valuation on the newest bar must not make the
    # entire market disappear when a fresh canonical bar is immediately behind it.
    latest = None
    for candidate in reversed(history):
        if _asset_price(candidate) is not None:
            latest = candidate
            break
    if latest is None:
        return None
    current = _asset_price(latest)

    # Opportunity discovery must be transparent and must not disappear just
    # because one auxiliary field is missing.  The collector already keeps
    # only future markets, while collection_complete is a batch-level health
    # flag rather than a per-market validity test.
    if current is None or latest.implied_apy is None or latest.liquidity_usd is None:
        return None

    days_to_expiry = latest.days_to_expiry
    if days_to_expiry is None and latest.expiry:
        try:
            expiry_dt = datetime.fromisoformat(latest.expiry.replace("Z", "+00:00"))
            if expiry_dt.tzinfo is None:
                expiry_dt = expiry_dt.replace(tzinfo=timezone.utc)
            days_to_expiry = max(0.0, (expiry_dt - datetime.now(timezone.utc)).total_seconds() / 86400.0)
        except ValueError:
            days_to_expiry = None

    if days_to_expiry is None:
        return None

    if not settings.alpha_min_days_to_expiry <= days_to_expiry <= settings.alpha_max_days_to_expiry:
        return None

    if latest.liquidity_usd < settings.alpha_min_liquidity_usd:
        return None

    # Intraday return points are supporting diagnostics, not a hard gate for
    # opportunity construction. Collection can occasionally skip a bar or
    # drift a few minutes because Pendle pages are fetched sequentially. Use
    # wider, horizon-specific windows and allow r1/r4 to be unavailable.
    p1 = _value_at_or_before(history, 60, max(settings.max_snapshot_gap_minutes, 30))
    p4 = _value_at_or_before(history, 240, max(settings.max_snapshot_gap_minutes, 60))
    r1 = _return(current, _asset_price(p1) if p1 else None)
    r4 = _return(current, _asset_price(p4) if p4 else None)
    ur1 = _return(
        latest.underlying_price_usd,
        p1.underlying_price_usd if p1 else None,
    )
    ur4 = _return(
        latest.underlying_price_usd,
        p4.underlying_price_usd if p4 else None,
    )

    window = history[-288:]
    previous = window[:-1]

    # Only statistics from the current valuation basis belong in the alpha
    # sample. This also protects us if a market has a few legacy snapshots
    # from before the accounting-asset migration.
    # Keep the alpha sample on the explicit accounting-asset valuation basis.
    # Do not require the raw accounting_asset_id string to be identical across
    # every historical row: older rows may have been written before Pendle
    # started exposing/normalizing the field consistently. The stored
    # pt_price_asset itself is already the canonical PT/accounting-asset value.
    asset_prices = [
        _asset_price(x)
        for x in window
        if _asset_price(x) is not None
    ]
    if len(asset_prices) < 20:
        return None

    residual_z = _z(
        [
            _asset_price(x)
            for x in previous
            if _asset_price(x) is not None
            and x.price_basis == latest.price_basis
        ],
        current,
    )

    bucket = _apy_bucket(days_to_expiry)
    bucket_apys = [
        x.implied_apy
        for x in previous
        if x.implied_apy is not None
        and x.accounting_asset_id == latest.accounting_asset_id
        and _apy_bucket(x.days_to_expiry) == bucket
    ]
    apy_z = _z(bucket_apys, latest.implied_apy)

    ref_asset = median(asset_prices)
    side = "BUY" if current < ref_asset else "SELL"
    distance = abs(ref_asset / current - 1.0)

    # Show the same economics as detect_signal: only 50% of the historical
    # displacement is assumed to mean-revert, in PT/accounting asset units.
    target_asset = current + (ref_asset - current) * 0.50
    expected_move = abs(target_asset / current - 1.0)
    gross = expected_move
    # Cost is populated later from a real two-sided Pendle Convert quote.
    # Keep the conservative configured estimate only as a pre-quote fallback.
    model_net = gross - settings.alpha_round_trip_cost
    gross_pnl = settings.paper_capital_usd * gross
    cost_pnl = settings.paper_capital_usd * settings.alpha_round_trip_cost
    model_pnl = settings.paper_capital_usd * model_net

    status = "WARMUP"
    if len(history) >= settings.backtest_min_history:
        if residual_z is not None and abs(residual_z) >= settings.alpha_min_price_z * 0.6 and model_net > 0:
            status = "NEAR"
        else:
            status = "FILTERED"

    return Opportunity(
        market=latest.market,
        name=latest.name,
        chain_id=latest.chain_id,
        side=side,
        pt_price_asset=current,
        apy=latest.implied_apy,
        apy_z=apy_z,
        residual_z=residual_z,
        distance=distance,
        gross=gross,
        model_net=model_net,
        gross_pnl=gross_pnl,
        cost_pnl=cost_pnl,
        model_pnl=model_pnl,
        quote_cost=None,
        quote_fee=None,
        quote_impact=None,
        quote_status="UNQUOTED",
        quote_age_min=None,
        target_asset=target_asset,
        expected_move=expected_move,
        r1=r1,
        r4=r4,
        ur1=ur1,
        ur4=ur4,
        liquidity=latest.liquidity_usd,
        observations=len(history),
        days_to_expiry=days_to_expiry,
        status=status,
        pt_address=_latest_pt_address(history)[0],
        pt_price_usd=latest.pt_price,
        quote_reason=None,
    )


def show_signal_history(store: HistoryStore, limit: int = 30) -> None:
    """Replay deterministic signals and collapse consecutive bars into episodes."""
    episodes: list[dict] = []

    for market in store.markets():
        history = store.recent(market, 500)
        if len(history) < 24:
            continue

        previous_key = None
        for idx in range(24, len(history) + 1):
            prefix = history[:idx]
            signal = detect_signal(
                prefix,
                capital=settings.paper_capital_usd,
                min_liquidity=settings.alpha_min_liquidity_usd,
                estimated_round_trip_cost=settings.alpha_round_trip_cost,
                min_net_return=settings.alpha_min_net_return,
                min_price_z=settings.alpha_min_price_z,
                min_apy_z=settings.alpha_min_apy_z,
                underlying_adverse_1h=settings.underlying_adverse_1h,
                underlying_adverse_4h=settings.underlying_adverse_4h,
                min_days_to_expiry=settings.alpha_min_days_to_expiry,
                max_days_to_expiry=settings.alpha_max_days_to_expiry,
                max_snapshot_gap_minutes=settings.max_snapshot_gap_minutes,
            )
            if signal is None:
                previous_key = None
                continue

            key = (signal.side, signal.kind)
            latest = prefix[-1]
            if key != previous_key:
                episodes.append(
                    {
                        "timestamp": latest.timestamp,
                        "market": signal.market,
                        "name": signal.name,
                        "chain_id": latest.chain_id,
                        "side": signal.side,
                        "entry": signal.entry,
                        "target": signal.target,
                        "apy": signal.apy_now,
                        "apy_z": signal.apy_z,
                        "price_z": signal.price_z,
                        "distance": signal.distance_to_median,
                        "net_return": signal.expected_net_return,
                        "net_pnl": signal.expected_net_pnl,
                        "liquidity": signal.liquidity_usd,
                        "confidence": signal.confidence,
                        "ttm": signal.days_to_expiry,
                    }
                )
            previous_key = key

    episodes.sort(key=lambda x: x["timestamp"], reverse=True)

    print("\n=== SIGNAL HISTORY ===")
    print("Reconstructed from stored snapshots; consecutive identical signals are collapsed.")
    if not episodes:
        print("No validated statistical signals found in the available history.")
        return

    print(
        "time                 side market                         "
        "APY    PZ    AZ    dev      net P&L  conf  TTM"
    )
    print(
        "-------------------- ---- ------------------------------ "
        "------ ----- ----- -------- --------- ----- -----"
    )

    for x in episodes[:limit]:
        ts = x["timestamp"].replace("T", " ").replace("Z", "")[:19]
        print(
            f"{ts:20} {x['side']:<4} {x['name'][:30]:30} "
            f"{x['apy']:6.2%} {x['price_z']:+5.2f} {x['apy_z']:+5.2f} "
            f"{x['distance']:7.2%} ${x['net_pnl']:+8.2f} "
            f"{x['confidence']:>4} {x['ttm']:5.1f}d"
        )
        print(
            f"     PT/accounting asset {x['entry']:.8f} -> target {x['target']:.8f}; "
            f"expected net {x['net_return']:+.2%}; chain={x['chain_id']} "
            f"liq=${x['liquidity']/1e6:.2f}M"
        )

    print(f"\nValidated signal episodes: {len(episodes)}")
    print(f"Showing latest: {min(limit, len(episodes))}")


def _quote_age_minutes(quote: dict | None) -> float | None:
    if not quote or not quote.get("quote_ts"):
        return None
    try:
        ts = datetime.fromisoformat(str(quote["quote_ts"]).replace("Z", "+00:00"))
        return max(0.0, (datetime.now(timezone.utc) - ts).total_seconds() / 60.0)
    except ValueError:
        return None


def _quote_is_fresh(quote: dict | None) -> bool:
    if not quote or quote.get("status") != "QUOTED" or quote.get("cost_usd") is None:
        return False
    if not str(quote.get("reason") or "").startswith("USDC -> PT -> USDC"):
        return False
    age = _quote_age_minutes(quote)
    return age is not None and age <= settings.quote_cache_ttl_minutes


def _ptmarket_from_row(row: Opportunity) -> PTMarket | None:
    if not row.pt_address:
        return None
    return PTMarket(
        chain_id=row.chain_id,
        market_address=row.market,
        pt_address=row.pt_address,
        name=row.name,
        expiry="",
        implied_apy=row.apy,
        pt_price_usd=row.pt_price_usd,
        liquidity_usd=row.liquidity,
    )


async def _refresh_real_quotes(rows: list[Opportunity], store: HistoryStore) -> None:
    """Refresh at most five stale/missing Convert quotes using Neon PT addresses.

    Does not rediscover the Pendle market universe. Convert remains live.
    """
    http = HttpClient()
    pendle = PendleClient(http)
    simulator = TradeSimulator(pendle)
    try:
        refreshed = 0
        for row in rows:
            if not row.pt_address:
                row.quote_status = "NO_PT"
                row.quote_reason = "no PT address in Neon snapshots"
                continue
            cached = store.get_quote(row.market)
            if _quote_is_fresh(cached):
                continue
            if refreshed >= 5:
                continue
            market = _ptmarket_from_row(row)
            if market is None:
                row.quote_status = "NO_PT"
                row.quote_reason = "no PT address in Neon snapshots"
                continue
            try:
                quote = await simulator.round_trip(market, settings.paper_capital_usd)
                if quote.ok:
                    cost = max(0.0, settings.paper_capital_usd - (settings.paper_capital_usd + quote.net_pnl_usd))
                    store.save_quote(
                        row.market, row.chain_id, datetime.now(timezone.utc).isoformat(),
                        cost, quote.fees_usd,
                        (quote.entry.price_impact + quote.exit.price_impact
                         if quote.entry and quote.exit
                         and quote.entry.price_impact is not None
                         and quote.exit.price_impact is not None
                         else None),
                        quote.net_pnl_usd, "QUOTED", "USDC -> PT -> USDC round-trip; " + quote.reason,
                    )
                else:
                    store.save_quote(
                        row.market, row.chain_id, datetime.now(timezone.utc).isoformat(),
                        None, quote.fees_usd,
                        (quote.entry.price_impact + quote.exit.price_impact
                         if quote.entry and quote.exit
                         and quote.entry.price_impact is not None
                         and quote.exit.price_impact is not None
                         else None),
                        None, "ERROR", quote.reason,
                    )
            except Exception as exc:
                store.save_quote(
                    row.market, row.chain_id, datetime.now(timezone.utc).isoformat(),
                    None, None, None, None, "ERROR", f"{type(exc).__name__}: {exc}",
                )
            refreshed += 1
    finally:
        await http.close()


def _apply_cached_quotes(rows: list[Opportunity], store: HistoryStore) -> None:
    ttl = settings.quote_cache_ttl_minutes
    for row in rows:
        if row.quote_status == "NO_PT":
            continue
        quote = store.get_quote(row.market)
        age = _quote_age_minutes(quote)
        row.quote_age_min = age
        if quote:
            row.quote_reason = quote.get("reason")
        if _quote_is_fresh(quote):
            row.quote_cost = float(quote["cost_usd"])
            row.cost_pnl = row.quote_cost
            row.model_pnl = row.gross_pnl - row.quote_cost
            row.model_net = row.model_pnl / settings.paper_capital_usd if settings.paper_capital_usd else 0.0
            if row.model_pnl > 0 and row.model_net >= settings.alpha_min_net_return:
                row.status = "ACTIONABLE"
            else:
                row.status = "FILTERED"
            row.quote_fee = quote.get("fees_usd")
            row.quote_impact = quote.get("price_impact")
            row.quote_status = "QUOTED"
            continue
        if quote and quote.get("status") == "QUOTED":
            row.quote_status = "STALE"
            row.quote_reason = f"quote older than {ttl}m"
            continue
        if quote:
            row.quote_status = str(quote.get("status") or "UNQUOTED")
        elif row.quote_status != "NO_PT":
            row.quote_status = "UNQUOTED"


def _print_rejection_summary(counts: dict[str, int], scanned: int, rejected: int) -> None:
    print(f"scanned={scanned} rejected={rejected}")
    print("Rejected:")
    order = (
        "insufficient history",
        "missing accounting-asset price",
        "missing APY",
        "missing liquidity",
        "liquidity < $1M",
        "TTM outside range",
        "insufficient canonical asset observations",
        "missing PT address",
    )
    shown = False
    for key in order:
        n = counts.get(key, 0)
        if not n:
            continue
        shown = True
        print(f"  {key:<44} {n}")
    if not shown:
        print("  (none)")
    missing_pt = counts.get("missing PT address", 0)
    if missing_pt:
        print("  (missing PT address: passed filters, listed, cannot quote until collected)")


async def show_opportunities(store: HistoryStore, limit: int = 15) -> None:
    """Show opportunities with bounded, cached real two-sided execution cost."""
    rows: list[Opportunity] = []
    scanned = 0
    rejected = 0
    reject_counts: dict[str, int] = {}

    for market in store.markets():
        scanned += 1
        history = store.recent(market, SIGNAL_HISTORY_BARS)
        reason = _reject_reason(history)
        if reason:
            rejected += 1
            reject_counts[reason] = reject_counts.get(reason, 0) + 1
            continue
        row = _opportunity(history)
        if row is None:
            rejected += 1
            reject_counts["missing accounting-asset price"] = (
                reject_counts.get("missing accounting-asset price", 0) + 1
            )
            continue
        if not row.pt_address:
            reject_counts["missing PT address"] = reject_counts.get("missing PT address", 0) + 1
        rows.append(row)

    rows.sort(
        key=lambda x: (
            x.gross,
            abs(x.residual_z) if x.residual_z is not None else -1,
            x.distance,
        ),
        reverse=True,
    )
    rows = rows[:limit]

    print("\n=== CURRENT OPPORTUNITIES / NEAR MISSES ===")
    _print_rejection_summary(reject_counts, scanned, rejected)
    if not rows:
        print("No usable opportunities after filters. Filters were not loosened.")
        return

    await _refresh_real_quotes(rows, store)
    _apply_cached_quotes(rows, store)

    print(
        "rank side market                         APY     RZ      resid    "
        "gross P&L   cost      net P&L    liq       n  status"
    )
    print(
        "---- ---- ------------------------------ ------- ------- -------- "
        "----------- --------- ----------- --------- --- --------"
    )

    for i, x in enumerate(rows, 1):
        rz = f"{x.residual_z:+.2f}" if x.residual_z is not None else "n/a"
        quoted = x.quote_status == "QUOTED"
        cost = f"${x.cost_pnl:7.2f}" if quoted else "    n/a"
        net = f"${x.model_pnl:+9.2f}" if quoted else "      n/a"
        print(
            f"{i:>4} {x.side:<4} {x.name[:30]:30} {x.apy:6.2%} {rz:>7} "
            f"{x.distance:7.2%} ${x.gross_pnl:+9.2f} {cost} {net} "
            f"${x.liquidity/1e6:6.2f}M {x.observations:3} {x.status}"
        )

        moves = [
            f"PT/U 1h {x.r1:+.2%}" if x.r1 is not None else "",
            f"PT/U 4h {x.r4:+.2%}" if x.r4 is not None else "",
            f"U 1h {x.ur1:+.2%}" if x.ur1 is not None else "",
            f"U 4h {x.ur4:+.2%}" if x.ur4 is not None else "",
        ]
        detail = [y for y in moves if y]
        quote_detail = []
        if x.quote_status == "QUOTED":
            quote_detail.append(f"real cost ${x.quote_cost:.2f}")
            if x.quote_fee is not None:
                quote_detail.append(f"fee ${x.quote_fee:.2f}")
            if x.quote_impact is not None:
                quote_detail.append(f"native impact {x.quote_impact:.3%}")
            else:
                quote_detail.append("impact n/a (aggregator route)")
            if x.quote_age_min is not None:
                quote_detail.append(f"quote age {x.quote_age_min:.0f}m")
        elif x.quote_status == "STALE":
            age = f"{x.quote_age_min:.0f}m" if x.quote_age_min is not None else "unknown"
            quote_detail.append(f"stale quote ({age}) not used for net P&L")
        elif x.quote_status == "NO_PT":
            quote_detail.append("unable to quote: no PT address in Neon")
        elif x.quote_status == "ERROR":
            quote_detail.append(f"real quote ERROR: {x.quote_reason or 'unknown'}")
        else:
            quote_detail.append("real quote n/a")
        print(
            f"     PT/asset {x.pt_price_asset:.8f} -> {x.target_asset:.8f} "
            f"(expected move {x.expected_move:+.2%}); "
            + ", ".join(detail + quote_detail)
        )

    print("\nModel: PT/accounting-asset residual, 50% mean-reversion to rolling median.")
    print(
        f"Capital: ${settings.paper_capital_usd:,.2f} USDC | "
        "execution = real USDC -> PT -> USDC Pendle Convert; "
        f"cache TTL {settings.quote_cache_ttl_minutes}m, max 5 refreshes per run."
    )


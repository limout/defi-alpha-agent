import asyncio
import json
from datetime import datetime, timezone
from pathlib import Path

from .config import settings
from .http import HttpClient
from .scoring import extract_borrow_apy, score_pt_loop
from .sources.gmx import GmxClient
from .sources.morpho import MorphoClient
from .sources.pendle import PendleClient
from .utils import normalize_address, pct


async def main_async():
    http = HttpClient()

    try:
        pendle = PendleClient(http)
        morpho = MorphoClient(http)
        gmx = GmxClient(http)

        print("\n=== PENDLE CARRY SCANNER ===")
        print("READ-ONLY / DIAGNOSTIC MODE")
        print("No wallet. No approvals. No transactions.\n")

        p_markets, p_diag = await pendle.all_markets(
            settings.chain_id
        )

        m_markets = await morpho.normalized_markets(
            settings.chain_id
        )

        glv = await gmx.find_btc_usdc("30d")

        print("=== PENDLE DISCOVERY ===")
        print(json.dumps(p_diag, indent=2))
        print(f"Current future markets: {len(p_markets)}")

        # ------------------------------------------------------------
        # Pendle candidates
        #
        # Unknown liquidity is NOT treated as zero. It remains
        # eligible for discovery, but later becomes a risk penalty.
        # ------------------------------------------------------------

        candidates = [
            x
            for x in p_markets
            if x.implied_apy is not None
            and x.implied_apy >= settings.min_pt_apy
            and (
                x.liquidity_usd is None
                or x.liquidity_usd >= settings.min_market_liquidity_usd
            )
        ]

        candidates.sort(
            key=lambda x: x.implied_apy or 0,
            reverse=True,
        )

        print(
            f"APY candidates "
            f"(liquidity unknown allowed): {len(candidates)}"
        )

        if candidates:
            print("\n=== PENDLE CURRENT MARKETS ===")

            for pt in candidates[:20]:
                liquidity_text = (
                    f"${pt.liquidity_usd:,.0f}"
                    if pt.liquidity_usd is not None
                    else "unknown"
                )

                exit_text = (
                    "YES"
                    if pt.swap_available is True
                    else "NO"
                    if pt.swap_available is False
                    else "unknown"
                )

                print(
                    f"{pt.name[:30]:30} "
                    f"PT={pt.pt_address[:12] if pt.pt_address else '?':12} "
                    f"APY={pct(pt.implied_apy):>8} "
                    f"PT=${pt.pt_price_usd:,.6f}"
                    if pt.pt_price_usd is not None
                    else
                    f"{pt.name[:30]:30} "
                    f"PT={pt.pt_address[:12] if pt.pt_address else '?':12} "
                    f"APY={pct(pt.implied_apy):>8} "
                    f"PT=unknown"
                )

                print(
                    f"    market={pt.market_address} "
                    f"liq={liquidity_text} "
                    f"exit={exit_text} "
                    f"expiry={pt.expiry}"
                )

        print(
            f"\nMorpho listed markets:   {len(m_markets)}"
        )

        # ------------------------------------------------------------
        # Morpho collateral index
        # ------------------------------------------------------------

        by_collateral = {}

        for mm in m_markets:
            collateral = normalize_address(
                mm.collateral_token
            )

            if not collateral:
                continue

            by_collateral.setdefault(
                collateral,
                [],
            ).append(mm)

        # ------------------------------------------------------------
        # PT -> Morpho matching
        #
        # IMPORTANT:
        # Only a direct PT == Morpho collateral match is scored as
        # a PT-collateral loop. A match through SY/underlying would
        # be a different strategy and must not be silently presented
        # as the same trade.
        # ------------------------------------------------------------

        matches = []
        scored = []

        for pt in candidates[:80]:
            pt_address = normalize_address(
                pt.pt_address
            )

            if not pt_address:
                continue

            for mm in by_collateral.get(
                pt_address,
                [],
            ):
                matches.append(
                    (pt, mm)
                )

                try:
                    apy_data = await morpho.apy(
                        settings.chain_id,
                        mm.market_id,
                    )

                    borrow, history = extract_borrow_apy(
                        apy_data
                    )

                    if borrow is not None:
                        opportunity = score_pt_loop(
                            pt,
                            mm,
                            borrow,
                            history,
                        )

                        if opportunity is not None:
                            scored.append(
                                opportunity
                            )

                except Exception:
                    # One broken APY endpoint must not kill the scan.
                    pass

        print(
            f"PT ↔ Morpho matches:     {len(matches)}"
        )

        print(
            f"Complete rate matches:   "
            f"{len([x for x in scored if x])}\n"
        )

        # ------------------------------------------------------------
        # Diagnostic matched markets
        # ------------------------------------------------------------

        if matches:
            print(
                "=== MATCHED MARKETS (DIAGNOSTIC) ==="
            )

            for pt, mm in matches[:20]:
                liquidity_text = (
                    f"${pt.liquidity_usd:,.0f}"
                    if pt.liquidity_usd is not None
                    else "unknown"
                )

                exit_text = (
                    "YES"
                    if pt.swap_available is True
                    else "NO"
                    if pt.swap_available is False
                    else "unknown"
                )

                print(
                    f"{pt.name[:30]:30} "
                    f"PT={pt.pt_address[:10] if pt.pt_address else '?':10} "
                    f"liq={liquidity_text:>12} "
                    f"exit={exit_text:>7} "
                    f"PT_APY={pct(pt.implied_apy)} "
                    f"LLTV={pct(mm.lltv)} "
                    f"util={pct(mm.utilization)}"
                )

        else:
            print(
                "No PT token matched a Morpho collateral token."
            )

            print(
                "This means the next fix is "
                "address normalization/API coverage, "
                "not scoring."
            )

        # ------------------------------------------------------------
        # Scored opportunities
        # ------------------------------------------------------------

        scored = [
            x
            for x in scored
            if x
        ]

        scored.sort(
            key=lambda x: x.estimated_net_apy,
            reverse=True,
        )

        if scored:
            print("\n=== TOP PT / MORPHO ===")

            for i, opportunity in enumerate(
                scored[:10],
                1,
            ):
                print(
                    f"{i:>2}. "
                    f"{opportunity.action:<10} "
                    f"{opportunity.name[:38]:38} "
                    f"PT {pct(opportunity.pt_apy):>7} | "
                    f"Borrow {pct(opportunity.borrow_apy):>7} | "
                    f"Spread {pct(opportunity.gross_spread):>7} | "
                    f"Net~ {pct(opportunity.estimated_net_apy):>7} | "
                    f"Util {pct(opportunity.utilization):>7} | "
                    f"Risk {opportunity.risk_score}"
                )

        # ------------------------------------------------------------
        # GMX
        # ------------------------------------------------------------

        print("\n=== GMX BTC-USDC GLV ===")

        if glv:
            print(
                f"Address:                   "
                f"{glv.get('address')}"
            )

            print(
                f"Long token performance:    "
                f"{glv.get('longTokenPerformance')}"
            )

            print(
                f"Short token performance:   "
                f"{glv.get('shortTokenPerformance')}"
            )

            print(
                f"UniV2 benchmark:            "
                f"{glv.get('uniswapV2Performance')}"
            )

        else:
            print(
                "BTC-USDC GLV row not found."
            )

        # ------------------------------------------------------------
        # Snapshot
        # ------------------------------------------------------------

        snap = {
            "timestamp": datetime.now(
                timezone.utc
            ).isoformat(),

            "version": "0.2.2",

            "pendle_diagnostics": p_diag,

            "pendle_candidates": [
                {
                    "name": x.name,
                    "chain_id": x.chain_id,
                    "market_address": x.market_address,
                    "pt": x.pt_address,
                    "sy": x.sy_address,
                    "yt": x.yt_address,
                    "underlying": x.underlying_address,
                    "expiry": x.expiry,
                    "liquidity_usd": x.liquidity_usd,
                    "implied_apy": x.implied_apy,
                    "pt_price_usd": x.pt_price_usd,
                    "swap_available": x.swap_available,
                    "swap_implied_apy": x.swap_implied_apy,
                }
                for x in candidates[:80]
            ],

            "morpho_count": len(m_markets),

            "matches": [
                {
                    "pt": pt.pt_address,
                    "market": pt.market_address,
                    "morpho_market": mm.market_id,
                    "lltv": mm.lltv,
                    "utilization": mm.utilization,
                    "liquidity_usd": pt.liquidity_usd,
                    "swap_available": pt.swap_available,
                }
                for pt, mm in matches
            ],

            "scored_opportunities": [
                {
                    "name": x.name,
                    "pt_apy": x.pt_apy,
                    "borrow_apy": x.borrow_apy,
                    "gross_spread": x.gross_spread,
                    "estimated_net_apy": x.estimated_net_apy,
                    "utilization": x.utilization,
                    "lltv": x.lltv,
                    "liquidity_usd": x.liquidity_usd,
                    "risk_score": x.risk_score,
                    "action": x.action,
                }
                for x in scored
            ],

            "gmx_glv": glv,
        }

        path = Path(
            settings.snapshot_dir
        )

        path.mkdir(
            parents=True,
            exist_ok=True,
        )

        outfile = (
            path
            / datetime.now(
                timezone.utc
            ).strftime(
                "%Y%m%dT%H%M%SZ"
            )
        )

        outfile = outfile.with_suffix(
            ".json"
        )

        outfile.write_text(
            json.dumps(
                snap,
                indent=2,
                default=str,
            ),
            encoding="utf-8",
        )

        print(
            f"\nSnapshot saved: {outfile}"
        )

        print("\nCarry scan complete.")

    finally:
        await http.close()


def main():
    asyncio.run(main_async())

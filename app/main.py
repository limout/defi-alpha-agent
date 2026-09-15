import asyncio

from .alpha import run_daemon, run_once
from .carry import main_async as carry_main_async
from .backtest import run_backtest, print_report
from .config import settings
from .history import HistoryStore
from .lp import run_lp
from .preflight import run_preflight
from .paper import PaperLedger
from .monitor import show_signal_history, show_opportunities


def main():
    import argparse

    parser = argparse.ArgumentParser(description="DeFi Alpha Agent")
    parser.add_argument(
        "command",
        nargs="?",
        default="alpha",
        choices=("alpha", "collect", "daemon", "carry", "backtest", "preflight", "dbcheck", "signals", "opportunities", "lp"),
        help="alpha=signal scan, collect=one data collection, daemon=continuous scan, carry=legacy PT/Morpho scanner, backtest=local historical replay, preflight=one read-only Pendle two-sided quote test, dbcheck=database check, signals=historical signal episodes, opportunities=current near-misses, lp=LP APY spike research",
    )
    args = parser.parse_args()

    if args.command in ("alpha", "collect"):
        asyncio.run(run_once())
    elif args.command == "daemon":
        asyncio.run(run_daemon())
    elif args.command == "carry":
        asyncio.run(carry_main_async())
    elif args.command == "backtest":
        store = HistoryStore(database_url=settings.database_url)
        try:
            print_report(run_backtest(store))
        finally:
            store.close()
    elif args.command == "preflight":
        asyncio.run(run_preflight())
    elif args.command == "dbcheck":
        history = HistoryStore(database_url=settings.database_url)
        paper = PaperLedger(database_url=settings.database_url)
        try:
            print(f"History DB: {history.info()}")
            print(f"Paper DB:   {paper.info()}")
            print(f"History snapshots: {history.count()}")
            print(f"Paper trades: {paper.summary()['total']}")
            print("DATABASE CHECK: OK")
        finally:
            paper.close()
            history.close()
    elif args.command in ("signals", "opportunities"):
        store = HistoryStore(database_url=settings.database_url)
        try:
            if args.command == "signals":
                show_signal_history(store)
            else:
                asyncio.run(show_opportunities(store))
        finally:
            store.close()
    elif args.command == "lp":
        store = HistoryStore(database_url=settings.database_url)
        try:
            run_lp(store)
        finally:
            store.close()

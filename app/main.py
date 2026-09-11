import asyncio

from .alpha import run_daemon, run_once
from .carry import main_async as carry_main_async
from .backtest import run_backtest, print_report
from .config import settings
from .history import HistoryStore
from .preflight import run_preflight


def main():
    import argparse

    parser = argparse.ArgumentParser(description="DeFi Alpha Agent")
    parser.add_argument(
        "command",
        nargs="?",
        default="alpha",
        choices=("alpha", "collect", "daemon", "carry", "backtest", "preflight"),
        help="alpha=signal scan, collect=one data collection, daemon=continuous scan, carry=legacy PT/Morpho scanner, backtest=local historical replay, preflight=one read-only Pendle two-sided quote test",
    )
    args = parser.parse_args()

    if args.command in ("alpha", "collect"):
        asyncio.run(run_once())
    elif args.command == "daemon":
        asyncio.run(run_daemon())
    elif args.command == "carry":
        asyncio.run(carry_main_async())
    elif args.command == "backtest":
        store = HistoryStore(settings.history_db)
        try:
            print_report(run_backtest(store))
        finally:
            store.close()
    elif args.command == "preflight":
        asyncio.run(run_preflight())

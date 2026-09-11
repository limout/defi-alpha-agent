from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    chain_id: int = 42161
    pendle_chain_ids: str = "42161,1,8453,56"

    # Persistent database. When set, HistoryStore and PaperLedger use Neon.
    # Without it, they fall back to local SQLite.
    database_url: str | None = None

    # Legacy PT/Morpho scanner.
    min_market_liquidity_usd: float = 50_000
    min_pt_apy: float = 0.05
    max_morpho_utilization: float = 0.98
    snapshot_dir: str = "data/snapshots"

    # Short-horizon alpha engine.
    history_db: str = "data/alpha_history.sqlite3"
    alpha_snapshot_dir: str = "data/alpha_signals"
    poll_interval_seconds: int = 300
    paper_capital_usd: float = 5_000
    alpha_min_liquidity_usd: float = 1_000_000
    alpha_round_trip_cost: float = 0.0015
    alpha_min_net_return: float = 0.0020
    alpha_min_apy_z: float = 2.5
    quote_slippage: float = 0.01
    paper_db: str = "data/paper_trades.sqlite3"
    alpha_allow_short_paper: bool = False

    underlying_adverse_1h: float = 0.005
    underlying_adverse_4h: float = 0.012

    diagnostic_top_n: int = 25
    backtest_min_history: int = 24
    backtest_max_hold_min: int = 1440
    backtest_cost: float = 0.0020

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    def chain_ids(self) -> list[int]:
        values: list[int] = []
        for raw in self.pendle_chain_ids.split(","):
            try:
                value = int(raw.strip())
            except ValueError:
                continue
            if value not in values:
                values.append(value)
        return values or [self.chain_id]


settings = Settings()

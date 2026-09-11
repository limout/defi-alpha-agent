from dataclasses import dataclass


@dataclass
class PTMarket:
    name: str
    protocol: str
    chain_id: int
    market: str
    pt: str
    expiry: object
    liquidity_usd: float
    implied_apy: float | None
    pt_price_usd: float | None


@dataclass
class MorphoMarket:
    market_id: str
    loan_token: str
    collateral_token: str
    lltv: float
    utilization: float | None
    borrow_assets: float | None
    supply_assets: float | None


@dataclass
class Opportunity:
    name: str
    pt_apy: float
    borrow_apy: float
    gross_spread: float
    estimated_net_apy: float
    utilization: float | None
    lltv: float
    liquidity_usd: float
    risk_score: int
    action: str

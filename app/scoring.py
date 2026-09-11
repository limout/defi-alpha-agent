from .models import Opportunity
from .utils import as_float

def extract_borrow_apy(data):
    vals = data.get("borrow_apy_averages") or data.get("borrowApyAverages") or {}
    for key in ("24h", "7d", "30d"):
        x = as_float(vals.get(key)) if isinstance(vals, dict) else None
        if x is not None:
            return x, {k: as_float(v) for k, v in vals.items()}
    for key in ("borrowApy", "borrow_apy", "borrowApr", "borrow_apr"):
        x = as_float(data.get(key))
        if x is not None:
            return x, {}
    return None, {}

def score_pt_loop(pt, morpho, borrow_apy, history):
    if pt.implied_apy is None or borrow_apy is None:
        return None
    spread = pt.implied_apy - borrow_apy
    net = max(0.0, spread * 0.70)
    risk = 20
    if pt.liquidity_usd < 500_000: risk += 12
    if pt.liquidity_usd < 100_000: risk += 18
    if morpho.utilization is not None:
        if morpho.utilization > .90: risk += 15
        elif morpho.utilization > .80: risk += 8
    if morpho.lltv >= .85: risk += 8
    if spread < .03: action = "IGNORE"
    elif net >= .12 and risk <= 50: action = "PAPER_OPEN"
    elif net >= .08: action = "WATCH"
    else: action = "IGNORE"
    return Opportunity(
        name=f"{pt.name} / Morpho {morpho.market_id[:8]}",
        pt_apy=pt.implied_apy, borrow_apy=borrow_apy,
        gross_spread=spread, estimated_net_apy=net,
        utilization=morpho.utilization, lltv=morpho.lltv,
        liquidity_usd=pt.liquidity_usd, risk_score=min(risk,100),
        action=action,
    )

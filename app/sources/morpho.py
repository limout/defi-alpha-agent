from ..http import HttpClient
from ..models import MorphoMarket
from ..utils import as_float

BASE = "https://api.morpho.org"

class MorphoClient:
    def __init__(self, http: HttpClient):
        self.http = http

    async def markets(self, chain_id: int):
        rows, cursor = [], None
        while True:
            params = {"chain_id": chain_id, "listed": "true", "limit": 1000}
            if cursor:
                params["cursor"] = cursor
            data = await self.http.get_json(f"{BASE}/v1/blue/markets", params=params)
            batch = data.get("data", []) if isinstance(data, dict) else []
            rows.extend(batch)
            cursor = data.get("next_cursor") if isinstance(data, dict) else None
            if not cursor or not batch:
                break
        return rows

    async def state(self, chain_id, market_id):
        data = await self.http.get_json(f"{BASE}/v0/blue/markets/{chain_id}:{market_id}/state")
        return data.get("data", data) if isinstance(data, dict) else {}

    async def apy(self, chain_id, market_id):
        data = await self.http.get_json(f"{BASE}/v0/blue/markets/{chain_id}:{market_id}/apy-averages")
        return data.get("data", data) if isinstance(data, dict) else {}

    async def normalized_markets(self, chain_id):
        rows = await self.markets(chain_id)
        result = []
        for row in rows:
            mid = row.get("market_id")
            if not mid:
                continue
            try:
                state = await self.state(chain_id, mid)
            except Exception:
                state = {}
            supply = as_float(state.get("total_supply_assets"))
            borrow = as_float(state.get("total_borrow_assets"))
            util = borrow / supply if supply and borrow is not None else None
            result.append(MorphoMarket(
                market_id=mid,
                loan_token=str(row.get("loan_token") or "").lower(),
                collateral_token=str(row.get("collateral_token") or "").lower(),
                lltv=(as_float(row.get("lltv_wad"), 0) or 0) / 1e18,
                utilization=util,
                borrow_assets=borrow,
                supply_assets=supply,
            ))
        return result

from ..http import HttpClient

BASE = "https://arbitrum-api.gmxinfra.io"
BTC_USDC_GLV = "0xdf03eed325b82bc1d4db8b49c30ecc9e05104b96"


class GmxClient:
    def __init__(self, http: HttpClient):
        self.http = http

    async def performance(self, period="30d"):
        data = await self.http.get_json(
            f"{BASE}/performance/annualized",
            params={"period": period},
        )
        return data if isinstance(data, list) else data.get("data", [])

    async def apy(self, period="30d"):
        data = await self.http.get_json(
            f"{BASE}/apy",
            params={"period": period},
        )
        return data if isinstance(data, list) else data.get("data", [])

    async def find_btc_usdc(self, period="30d"):
        rows = await self.performance(period)
        for row in rows:
            if str(row.get("address", "")).lower() == BTC_USDC_GLV:
                return row
        return None

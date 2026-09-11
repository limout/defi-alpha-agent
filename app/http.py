import asyncio
import random

import httpx


class HttpClient:
    def __init__(self, timeout: float = 15.0, retries: int = 2, verbose: bool = True):
        self.retries = retries
        self.verbose = verbose
        self.client = httpx.AsyncClient(
            timeout=httpx.Timeout(timeout, connect=8.0),
            headers={"User-Agent": "defi-alpha-agent/0.5.6"},
        )

    async def get_json(self, url: str, params: dict | None = None):
        return await self._request_json("GET", url, params=params)

    async def post_json(self, url: str, payload: dict):
        return await self._request_json("POST", url, json=payload)

    async def _request_json(self, method: str, url: str, **kwargs):
        last_exc = None
        for attempt in range(self.retries + 1):
            try:
                response = await self.client.request(method, url, **kwargs)
                if self.verbose:
                    print(f"    HTTP {response.status_code} {method} {url}", flush=True)

                if response.status_code == 429 or response.status_code >= 500:
                    retry_after = response.headers.get("Retry-After")
                    response.raise_for_status()

                response.raise_for_status()
                return response.json()

            except (httpx.HTTPStatusError, httpx.RequestError) as exc:
                last_exc = exc
                if attempt >= self.retries:
                    raise

                retry_after = None
                if isinstance(exc, httpx.HTTPStatusError):
                    retry_after = exc.response.headers.get("Retry-After")
                try:
                    delay = float(retry_after) if retry_after else min(2 ** attempt, 4)
                except (TypeError, ValueError):
                    delay = min(2 ** attempt, 4)
                delay += random.uniform(0, 0.25)
                print(f"    retry {attempt + 1}/{self.retries} in {delay:.1f}s: {type(exc).__name__}", flush=True)
                await asyncio.sleep(delay)

        raise last_exc  # pragma: no cover

    async def close(self):
        await self.client.aclose()

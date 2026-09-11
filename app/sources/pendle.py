from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from typing import Any

from app.http import HttpClient


BASE = "https://api-v2.pendle.finance/core"
ARBITRUM_CHAIN_ID = 42161

# Public read-only RPCs used only as a last-mile execution preflight when
# Pendle bulk metadata does not expose token decimals. Override with env vars
# in production if desired. These calls are read-only eth_call requests.
DEFAULT_RPC_URLS = {
    1: "https://eth.llamarpc.com",
    56: "https://binance.llamarpc.com",
    8453: "https://base.llamarpc.com",
    42161: "https://arb1.arbitrum.io/rpc",
}
DECIMALS_SELECTOR = "0x313ce567"


@dataclass
class PTMarket:
    chain_id: int
    market_address: str
    pt_address: str
    name: str
    expiry: str
    implied_apy: float | None
    pt_price_usd: float | None
    liquidity_usd: float | None
    sy_address: str | None = None
    yt_address: str | None = None
    underlying_address: str | None = None
    underlying_asset_id: str | None = None
    pt_price_asset: float | None = None
    days_to_expiry: float | None = None
    collection_complete: bool = True
    source_ts: str | None = None
    pt_decimals: int | None = None

    # v0.2.2 exitability diagnostics.
    swap_available: bool | None = None
    swap_implied_apy: float | None = None
    underlying_token_to_pt_rate: float | None = None
    pt_to_underlying_token_rate: float | None = None
    underlying_decimals: int | None = None
    underlying_price_usd: float | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class PendleClient:
    def __init__(self, http: HttpClient):
        self.http = http
        self._decimals_cache: dict[tuple[int, str], int] = {}

    @staticmethod
    def _unwrap_markets(payload: Any) -> list[dict[str, Any]]:
        if isinstance(payload, list):
            return [x for x in payload if isinstance(x, dict)]

        if not isinstance(payload, dict):
            return []

        for key in ("markets", "data", "results"):
            value = payload.get(key)

            if isinstance(value, list):
                return [x for x in value if isinstance(x, dict)]

            if isinstance(value, dict):
                for nested_key in ("markets", "data", "results"):
                    nested = value.get(nested_key)

                    if isinstance(nested, list):
                        return [
                            x for x in nested
                            if isinstance(x, dict)
                        ]

        return []

    @staticmethod
    def _normalize_address(value: Any) -> str | None:
        if value is None:
            return None

        text = str(value).strip()

        if not text:
            return None

        if "-" in text:
            prefix, address = text.split("-", 1)

            if prefix.isdigit() and address.startswith("0x"):
                text = address

        if text.startswith("0x"):
            return text.lower()

        return text

    @staticmethod
    def _extract_number(value: Any) -> float | None:
        if value is None or isinstance(value, bool):
            return None

        if isinstance(value, (int, float)):
            return float(value)

        if isinstance(value, str):
            try:
                return float(value.strip())
            except ValueError:
                return None

        if isinstance(value, dict):
            for key in (
                "usd",
                "value",
                "amount",
                "price",
                "rate",
                "apy",
                "impliedApy",
                "impliedAPY",
            ):
                if key in value:
                    result = PendleClient._extract_number(value[key])

                    if result is not None:
                        return result

        return None

    @staticmethod
    def _extract_expiry(market: dict[str, Any]) -> str | None:
        expiry = market.get("expiry")

        if expiry is None:
            return None

        if isinstance(expiry, (int, float)):
            try:
                timestamp = float(expiry)

                if timestamp > 10_000_000_000:
                    timestamp /= 1000

                return datetime.fromtimestamp(
                    timestamp,
                    tz=timezone.utc,
                ).isoformat()

            except (ValueError, OverflowError, OSError):
                return str(expiry)

        return str(expiry)

    @staticmethod
    def _is_future(expiry: str | None) -> bool:
        if not expiry:
            return False

        try:
            value = expiry.replace("Z", "+00:00")
            dt = datetime.fromisoformat(value)

            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)

            return dt > datetime.now(timezone.utc)

        except ValueError:
            return False

    @staticmethod
    def _extract_pt_address(market: dict[str, Any]) -> str | None:
        pt = market.get("pt")
        candidates: list[Any] = []

        if isinstance(pt, dict):
            candidates.extend(
                [
                    pt.get("address"),
                    pt.get("tokenAddress"),
                    pt.get("token"),
                    pt.get("id"),
                ]
            )
        else:
            candidates.append(pt)

        tokens = market.get("tokens")

        if isinstance(tokens, dict):
            tokens_pt = tokens.get("pt")

            if isinstance(tokens_pt, dict):
                candidates.extend(
                    [
                        tokens_pt.get("address"),
                        tokens_pt.get("tokenAddress"),
                        tokens_pt.get("id"),
                    ]
                )
            else:
                candidates.append(tokens_pt)

        for candidate in candidates:
            address = PendleClient._normalize_address(candidate)

            if address and address.startswith("0x"):
                return address

        return None

    @staticmethod
    def _extract_token_address(
        market: dict[str, Any],
        token_name: str,
    ) -> str | None:
        value = market.get(token_name)

        if isinstance(value, dict):
            for key in ("address", "tokenAddress", "id"):
                address = PendleClient._normalize_address(value.get(key))

                if address and address.startswith("0x"):
                    return address

        address = PendleClient._normalize_address(value)

        if address and address.startswith("0x"):
            return address

        tokens = market.get("tokens")

        if isinstance(tokens, dict):
            value = tokens.get(token_name)

            if isinstance(value, dict):
                for key in ("address", "tokenAddress", "id"):
                    address = PendleClient._normalize_address(
                        value.get(key)
                    )

                    if address and address.startswith("0x"):
                        return address

            address = PendleClient._normalize_address(value)

            if address and address.startswith("0x"):
                return address

        return None

    @staticmethod
    def _extract_pt_price(market: dict[str, Any]) -> float | None:
        """Extract the PT USD price across current/legacy API shapes."""
        pt = market.get("pt")

        if isinstance(pt, dict):
            price = pt.get("price")

            if isinstance(price, dict):
                for key in ("usd", "USD", "value"):
                    result = PendleClient._extract_number(price.get(key))
                    if result is not None and result > 0:
                        return result

            result = PendleClient._extract_number(price)
            if result is not None and result > 0:
                return result

        for key in (
            "ptPriceUsd", "ptPriceUSD", "ptPrice", "priceUsd", "priceUSD",
        ):
            result = PendleClient._extract_number(market.get(key))
            if result is not None and result > 0:
                return result

        # Some API responses expose prices under a nested `prices` object.
        prices = market.get("prices")
        if isinstance(prices, dict):
            pt_price = prices.get("pt") or prices.get("PT")
            if isinstance(pt_price, dict):
                for key in ("usd", "USD", "price", "value"):
                    result = PendleClient._extract_number(pt_price.get(key))
                    if result is not None and result > 0:
                        return result
            result = PendleClient._extract_number(pt_price)
            if result is not None and result > 0:
                return result

        # Last-resort recursive lookup, restricted to PT/price semantics.
        for obj in PendleClient._walk_dicts(market):
            for key in ("ptPriceUsd", "ptPriceUSD", "ptPrice"):
                if key in obj:
                    result = PendleClient._extract_number(obj.get(key))
                    if result is not None and result > 0:
                        return result

        return None

    @staticmethod
    def _extract_usd_price(market: dict[str, Any]) -> float | None:
        """Find an underlying/SY USD price without accepting arbitrary numbers."""
        preferred = ("underlying", "underlyingAsset", "sy")
        for token_name in preferred:
            value = market.get(token_name)
            if isinstance(value, dict):
                price = value.get("price")
                if isinstance(price, dict):
                    for key in ("usd", "USD", "value"):
                        result = PendleClient._extract_number(price.get(key))
                        if result is not None and result > 0:
                            return result

        # Common nested price containers. Only inspect objects explicitly
        # named as underlying/SY to avoid mistaking TVL or unrelated USD data.
        for key in ("underlyingPriceUsd", "underlyingPriceUSD", "syPriceUsd", "syPriceUSD"):
            result = PendleClient._extract_number(market.get(key))
            if result is not None and result > 0:
                return result
        return None

    @staticmethod
    def _walk_dicts(value: Any):
        if isinstance(value, dict):
            yield value

            for child in value.values():
                yield from PendleClient._walk_dicts(child)

        elif isinstance(value, list):
            for child in value:
                yield from PendleClient._walk_dicts(child)

    @staticmethod
    def _extract_liquidity(market: dict[str, Any]) -> float | None:
        """
        v2/markets/all has changed its nested shape over time.

        Search only fields that semantically represent TVL/liquidity,
        including nested forms, instead of treating arbitrary USD
        numbers as liquidity.
        """
        keys = (
            "liquidityUsd",
            "liquidityUSD",
            "tvlUsd",
            "tvlUSD",
            "totalLiquidityUsd",
            "totalLiquidityUSD",
            "liquidity",
            "tvl",
            "totalLiquidity",
        )

        for obj in PendleClient._walk_dicts(market):
            for key in keys:
                if key not in obj:
                    continue

                value = obj.get(key)

                if isinstance(value, dict):
                    for nested_key in (
                        "usd",
                        "USD",
                        "value",
                        "total",
                    ):
                        result = PendleClient._extract_number(
                            value.get(nested_key)
                        )

                        if result is not None:
                            return result

                result = PendleClient._extract_number(value)

                if result is not None:
                    return result

        return None

    @staticmethod
    def _extract_implied_apy(market: dict[str, Any]) -> float | None:
        candidates: list[Any] = [
            market.get("impliedApy"),
            market.get("impliedAPY"),
        ]

        # Current v2/markets/all shape exposes market metrics under
        # `details`, e.g. details.impliedApy.
        details = market.get("details")
        if isinstance(details, dict):
            candidates.extend([
                details.get("impliedApy"),
                details.get("impliedAPY"),
            ])

        # Some consumers expose the same metrics under marketInfo.
        market_info = market.get("marketInfo")
        if isinstance(market_info, dict):
            candidates.extend([
                market_info.get("impliedApy"),
                market_info.get("impliedAPY"),
            ])

        apy = market.get("apy")

        if isinstance(apy, dict):
            candidates.extend(
                [
                    apy.get("impliedApy"),
                    apy.get("impliedAPY"),
                    apy.get("implied"),
                ]
            )

        prices = market.get("prices")

        if isinstance(prices, dict):
            candidates.extend(
                [
                    prices.get("impliedApy"),
                    prices.get("impliedAPY"),
                ]
            )

        for candidate in candidates:
            value = PendleClient._extract_number(candidate)

            if value is None:
                continue

            if value > 5:
                value /= 100

            return value

        return None

    async def _fetch_asset_prices(self, asset_ids: list[str]) -> dict[str, float]:
        if not asset_ids:
            return {}
        result: dict[str, float] = {}
        # The public price endpoint accepts batches; keep requests <=20 ids.
        for start in range(0, len(asset_ids), 20):
            batch = asset_ids[start:start + 20]
            payload = await self.http.get_json(
                f"{BASE}/v1/prices/assets",
                params={"ids": ",".join(batch)},
            )
            if not isinstance(payload, dict):
                continue
            raw = payload.get("prices") or payload.get("priceMap") or {}
            if not isinstance(raw, dict):
                continue
            for key, value in raw.items():
                number = self._extract_number(value)
                if number is not None and number > 0:
                    result[str(key).lower()] = number
        return result

    async def _fetch_asset_metadata(self, asset_ids: list[str]) -> dict[str, int]:
        if not asset_ids:
            return {}
        result: dict[str, int] = {}
        for start in range(0, len(asset_ids), 20):
            batch = asset_ids[start:start + 20]
            payload = await self.http.get_json(
                f"{BASE}/v1/assets/all",
                params={"ids": ",".join(batch)},
            )
            if not isinstance(payload, dict):
                continue

            # The endpoint has returned both lists and id-keyed maps over time.
            items = payload.get("assets") or payload.get("data") or payload.get("results") or payload

            # Normalize both list responses and id-keyed maps. Some versions
            # of the API also wrap the actual asset list one level deeper.
            if isinstance(items, dict):
                normalized = []
                for key, value in items.items():
                    if isinstance(value, dict):
                        item = dict(value)
                        item.setdefault("id", key)
                        normalized.append(item)
                items = normalized

            if not isinstance(items, list):
                continue

            for item in items:
                if not isinstance(item, dict):
                    continue

                raw_id = item.get("id") or item.get("assetId") or item.get("asset_id")
                address = item.get("address") or item.get("tokenAddress") or item.get("token_address")
                chain = item.get("chainId") or item.get("chain_id")

                # Handle nested metadata such as {metadata: {decimals: 6}}.
                metadata = item.get("metadata")
                candidates = [item.get("decimals"), item.get("decimal")]
                if isinstance(metadata, dict):
                    candidates.extend([metadata.get("decimals"), metadata.get("decimal")])

                key = None
                if raw_id:
                    key = str(raw_id).lower()
                elif address is not None and chain is not None:
                    key = f"{chain}-{address}".lower()

                if not key:
                    continue

                for candidate in candidates:
                    try:
                        decimals = int(candidate)
                    except (TypeError, ValueError):
                        continue
                    if 0 <= decimals <= 36:
                        result[key] = decimals
                        break
        return result

    async def _fetch_page(
        self,
        limit: int = 100,
        skip: int = 0,
    ) -> Any:
        return await self.http.get_json(
            f"{BASE}/v2/markets/all",
            params={
                "limit": limit,
                "skip": skip,
            },
        )

    async def _fetch_convert_quote(
        self,
        chain_id: int,
        inputs: list[dict[str, str]],
        outputs: list[str],
        receiver: str,
        slippage: float = 0.01,
    ) -> Any:
        url = f"{BASE}/v3/sdk/{chain_id}/convert"
        return await self.http.post_json(
            url,
            {
                "receiver": receiver,
                "slippage": slippage,
                "enableAggregator": False,
                "inputs": inputs,
                "outputs": outputs,
                "additionalData": "impliedApy,effectiveApy",
                "redeemRewards": False,
                "needScale": False,
                # Conservative monitoring quote: do not allow a limit-order
                # route to make the execution sanity check look better than a
                # straightforward marketable route.
                "useLimitOrder": False,
            },
        )

    async def _fetch_swapping_price(
        self,
        chain_id: int,
        market_address: str,
    ) -> Any | None:
        url = (
            f"{BASE}/v1/sdk/{chain_id}/markets/"
            f"{market_address}/swapping-prices"
        )

        try:
            return await self.http.get_json(url)
        except Exception:
            return None

    @staticmethod
    def _extract_swap_apy(payload: Any) -> float | None:
        if not isinstance(payload, dict):
            return None

        candidates: list[Any] = [
            payload.get("impliedApy"),
            payload.get("impliedAPY"),
            payload.get("effectiveImpliedApy"),
            payload.get("effectiveImpliedAPY"),
        ]

        for key in ("data", "result", "prices"):
            nested = payload.get(key)

            if isinstance(nested, dict):
                candidates.extend(
                    [
                        nested.get("impliedApy"),
                        nested.get("impliedAPY"),
                        nested.get("effectiveImpliedApy"),
                        nested.get("effectiveImpliedAPY"),
                    ]
                )

        for candidate in candidates:
            value = PendleClient._extract_number(candidate)

            if value is None:
                continue

            if value > 5:
                value /= 100

            return value

        return None

    @staticmethod
    def _extract_swap_availability(payload: Any) -> bool | None:
        """
        The official swapping-prices endpoint documents null output
        as meaning the swap cannot be done because of insufficient
        liquidity or maturity.

        We therefore prefer explicit PT->SY/underlying values when
        present and fall back to a conservative payload inspection.
        """
        if payload is None:
            return False

        if not isinstance(payload, dict):
            return None

        # Likely names used by the swapping-price response.
        positive_keys = (
            "ptToSy",
            "ptToToken",
            "ptToUnderlying",
            "ptToAsset",
            "syToPt",
            "underlyingToPt",
            "tokenToPt",
        )

        found = False

        for obj in PendleClient._walk_dicts(payload):
            for key in positive_keys:
                if key not in obj:
                    continue

                found = True
                value = obj.get(key)

                # A null route means the swap cannot be performed.
                if value is None:
                    continue

                if isinstance(value, dict):
                    number = PendleClient._extract_number(value)

                    if number is not None and number > 0:
                        return True
                else:
                    number = PendleClient._extract_number(value)

                    if number is not None and number > 0:
                        return True

        if found:
            return False

        # If the endpoint returned a non-empty response with an
        # implied APY, the market is at least priceable.
        return PendleClient._extract_swap_apy(payload) is not None

    @staticmethod
    def _extract_swap_rates(payload: Any) -> tuple[float | None, float | None]:
        if not isinstance(payload, dict):
            return None, None
        a = PendleClient._extract_number(payload.get("underlyingTokenToPtRate"))
        b = PendleClient._extract_number(payload.get("ptToUnderlyingTokenRate"))
        return a, b

    @staticmethod
    def _extract_token_decimals(market: dict[str, Any], token_name: str) -> int | None:
        candidates = []
        value = market.get(token_name)
        if isinstance(value, dict):
            candidates.extend([value.get("decimals"), value.get("decimal")])
        tokens = market.get("tokens")
        if isinstance(tokens, dict):
            value = tokens.get(token_name)
            if isinstance(value, dict):
                candidates.extend([value.get("decimals"), value.get("decimal")])

        # Current /v2/markets/all responses expose the underlying as an asset
        # id string, while token metadata can live in inputTokens/outputTokens.
        raw_id = str(value).strip().lower() if value is not None else None
        unmatched_input_decimals: list[Any] = []
        for collection_name in ("inputTokens", "outputTokens"):
            collection = market.get(collection_name)
            if isinstance(collection, list):
                for item in collection:
                    if not isinstance(item, dict):
                        continue
                    item_id = item.get("id") or item.get("assetId") or item.get("address") or item.get("tokenAddress")
                    values = [item.get("decimals"), item.get("decimal")]
                    if raw_id and item_id and str(item_id).strip().lower() == raw_id:
                        candidates.extend(values)
                    elif collection_name == "inputTokens":
                        # Some current market payloads expose underlyingAsset as
                        # an asset id while inputTokens identify the same token
                        # by address. If there is only one input token, its
                        # decimals are an unambiguous fallback.
                        unmatched_input_decimals.extend(values)

        for candidate in candidates:
            try:
                number = int(candidate)
                if 0 <= number <= 36:
                    return number
            except (TypeError, ValueError):
                pass
        unique_input_decimals = set()
        for candidate in unmatched_input_decimals:
            try:
                number = int(candidate)
            except (TypeError, ValueError):
                continue
            if 0 <= number <= 36:
                unique_input_decimals.add(number)
        if len(unique_input_decimals) == 1:
            return next(iter(unique_input_decimals))

        return None

    async def resolve_token_decimals(self, chain_id: int, address: str) -> int | None:
        """Resolve ERC-20 decimals for execution only, with an in-memory cache.

        Pendle's bulk asset metadata endpoint is useful when it returns token
        metadata, but current responses can omit decimals for the asset-id
        shape used by /v2/markets/all. Do not guess decimals: query the token
        contract with read-only eth_call only when a trade candidate actually
        needs a quote.
        """
        normalized = self._normalize_address(address)
        if not normalized or not normalized.startswith("0x"):
            return None

        key = (int(chain_id), normalized.lower())
        cached = self._decimals_cache.get(key)
        if cached is not None:
            return cached

        rpc_url = DEFAULT_RPC_URLS.get(int(chain_id))
        if not rpc_url:
            return None

        try:
            payload = await self.http.post_json(
                rpc_url,
                {
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "eth_call",
                    "params": [
                        {
                            "to": normalized,
                            "data": DECIMALS_SELECTOR,
                        },
                        "latest",
                    ],
                },
            )
        except Exception as exc:
            print(
                f"    decimals RPC failed ch={chain_id} token={normalized}: "
                f"{type(exc).__name__}: {exc}",
                flush=True,
            )
            return None

        if not isinstance(payload, dict) or payload.get("error"):
            return None

        raw = payload.get("result")
        if not isinstance(raw, str) or not raw.startswith("0x"):
            return None

        try:
            decimals = int(raw, 16)
        except ValueError:
            return None

        if not 0 <= decimals <= 36:
            return None

        self._decimals_cache[key] = decimals
        return decimals

    async def all_markets(
        self,
        chain_id: int = ARBITRUM_CHAIN_ID,
        chain_ids: list[int] | None = None,
        min_liquidity_usd: float = 0.0,
    ) -> tuple[list[PTMarket], dict[str, Any]]:
        limit = 100
        wanted_chains = set(chain_ids or [chain_id])
        skip = 0

        raw_count = 0
        chain_count = 0
        future_count = 0
        apy_count = 0
        liquidity_count = 0
        accepted_count = 0
        swap_checked_count = 0
        swap_available_count = 0
        pt_price_count = 0
        debug_market_keys = None
        debug_price_fields = None
        debug_swap_keys = None
        debug_swap_rates = None

        pages = 0

        sample_chain_ids: list[Any] = []
        sample_expiries: list[Any] = []
        sample_names: list[Any] = []

        current_markets: list[dict[str, Any]] = []

        page_errors: list[dict[str, Any]] = []

        while True:
            page_no = pages + 1
            print(f"[collection] page {page_no} skip={skip} ...", flush=True)
            try:
                payload = await self._fetch_page(
                    limit=limit,
                    skip=skip,
                )
            except Exception as exc:
                page_errors.append({
                    "page": page_no,
                    "skip": skip,
                    "error": f"{type(exc).__name__}: {exc}",
                })
                print(f"[collection] page {page_no} ERROR: {type(exc).__name__}: {exc}", flush=True)
                # Do not hang the entire collector forever on one page.
                # Stop pagination because later pages cannot be trusted without
                # knowing whether this page was transiently unavailable.
                break

            markets = self._unwrap_markets(payload)

            pages += 1
            raw_count += len(markets)
            print(f"[collection] page {page_no} OK: {len(markets)} markets", flush=True)

            if not markets:
                break

            for market in markets:
                if len(sample_chain_ids) < 10:
                    sample_chain_ids.append(market.get("chainId"))

                if len(sample_expiries) < 10:
                    sample_expiries.append(market.get("expiry"))

                if len(sample_names) < 10:
                    sample_names.append(
                        market.get("name")
                        or market.get("symbol")
                    )

                market_chain_id = market.get("chainId")

                try:
                    market_chain_id = int(market_chain_id)
                except (TypeError, ValueError):
                    continue

                if market_chain_id not in wanted_chains:
                    continue

                chain_count += 1

                expiry = self._extract_expiry(market)

                if not self._is_future(expiry):
                    continue

                future_count += 1
                current_markets.append(market)

            if len(markets) < limit:
                break

            skip += limit

        result: list[PTMarket] = []

        underlying_ids: list[str] = []
        pt_ids: list[str] = []
        for market in current_markets:
            raw_underlying = market.get("underlyingAsset") or market.get("underlying")
            if raw_underlying:
                text = str(raw_underlying).strip()
                if text and text not in underlying_ids:
                    underlying_ids.append(text)
            raw_pt = market.get("pt")
            if raw_pt:
                text = str(raw_pt).strip()
                if text and text not in pt_ids:
                    pt_ids.append(text)

        price_ids = list(dict.fromkeys(underlying_ids + pt_ids))

        # Prices are available in bulk for both underlying assets and PTs.
        # Use the PT price directly when available instead of reconstructing
        # it from an underlying/spot swap rate.
        print(f"[collection] pricing {len(price_ids)} unique assets ({len(underlying_ids)} underlying + {len(pt_ids)} PT) ...", flush=True)
        try:
            asset_prices = await self._fetch_asset_prices(price_ids)
        except Exception as exc:
            print(f"[collection] asset prices ERROR: {type(exc).__name__}: {exc}", flush=True)
            asset_prices = {}
        try:
            asset_decimals = await self._fetch_asset_metadata(underlying_ids)
        except Exception as exc:
            print(f"[collection] asset metadata ERROR: {type(exc).__name__}: {exc}", flush=True)
            asset_decimals = {}
        print(f"[collection] asset prices={len(asset_prices)} metadata={len(asset_decimals)}", flush=True)

        result: list[PTMarket] = []

        for market in current_markets:
            try:
                market_chain_id = int(market.get("chainId"))
            except (TypeError, ValueError):
                continue

            market_address = self._normalize_address(
                market.get("address")
            )

            if not market_address:
                continue

            pt_address = self._extract_pt_address(market)

            if not pt_address:
                continue

            implied_apy = self._extract_implied_apy(market)

            raw_pt = market.get("pt")
            pt_asset_id = str(raw_pt).strip().lower() if raw_pt else None
            raw_underlying = market.get("underlyingAsset") or market.get("underlying")
            underlying_asset_id = str(raw_underlying).strip().lower() if raw_underlying else None

            pt_price_usd = self._extract_pt_price(market)
            if pt_price_usd is None and pt_asset_id:
                pt_price_usd = asset_prices.get(pt_asset_id)

            liquidity_usd = self._extract_liquidity(market)

            if liquidity_usd is not None:
                liquidity_count += 1

            # v0.6: keep the full future universe in history. Liquidity is a
            # decision-time signal filter, not an ingestion filter.

            # IMPORTANT: do not call the per-market swapping-price SDK endpoint
            # during collection. Pendle explicitly recommends bulk market/price
            # endpoints for broad monitoring because per-market SDK calls are
            # expensive and rate-limit prone. Real execution quotes are deferred
            # to TradeSimulator and requested only for validated candidates.
            swap_available = None
            swap_implied_apy = None
            underlying_to_pt_rate = None
            pt_to_underlying_rate = None

            if debug_market_keys is None:
                debug_market_keys = sorted(str(k) for k in market.keys())
                debug_price_fields = {
                    k: market.get(k)
                    for k in ("pt", "prices", "price", "ptPriceUsd", "ptPriceUSD", "ptPrice", "underlyingPriceUsd", "underlyingPriceUSD", "syPriceUsd", "syPriceUSD", "underlying", "underlyingAsset", "sy")
                    if k in market
                }

            if implied_apy is None:
                # No expensive fallback call here. The bulk market endpoint is
                # the authoritative cheap source for the monitoring cycle.
                implied_apy = self._extract_implied_apy(market)

            if implied_apy is not None:
                apy_count += 1

            if pt_price_usd is not None:
                pt_price_count += 1

            expiry_value = self._extract_expiry(market) or ""
            days_to_expiry = None
            try:
                expiry_dt = datetime.fromisoformat(expiry_value.replace("Z", "+00:00"))
                if expiry_dt.tzinfo is None:
                    expiry_dt = expiry_dt.replace(tzinfo=timezone.utc)
                days_to_expiry = max(0.0, (expiry_dt - datetime.now(timezone.utc)).total_seconds() / 86400.0)
            except ValueError:
                pass
            underlying_price_usd = asset_prices.get(underlying_asset_id) if underlying_asset_id else None
            pt_price_asset = None
            if pt_price_usd is not None and underlying_price_usd is not None and underlying_price_usd > 0:
                pt_price_asset = pt_price_usd / underlying_price_usd

            item = PTMarket(
                chain_id=market_chain_id,
                market_address=market_address,
                pt_address=pt_address,
                name=str(
                    market.get("name")
                    or market.get("symbol")
                    or "Unknown"
                ),
                expiry=expiry_value,
                implied_apy=implied_apy,
                pt_price_usd=pt_price_usd,
                liquidity_usd=liquidity_usd,
                sy_address=self._extract_token_address(
                    market,
                    "sy",
                ),
                yt_address=self._extract_token_address(
                    market,
                    "yt",
                ),
                underlying_address=(
                    self._extract_token_address(market, "underlying")
                    or self._extract_token_address(market, "underlyingAsset")
                ),
                underlying_asset_id=underlying_asset_id,
                pt_price_asset=pt_price_asset,
                days_to_expiry=days_to_expiry,
                collection_complete=not page_errors,
                source_ts=datetime.now(timezone.utc).isoformat(),
                swap_available=swap_available,
                swap_implied_apy=swap_implied_apy,
                underlying_token_to_pt_rate=underlying_to_pt_rate,
                pt_to_underlying_token_rate=pt_to_underlying_rate,
                underlying_price_usd=underlying_price_usd,
                underlying_decimals=(
                    self._extract_token_decimals(market, "underlying")
                    or self._extract_token_decimals(market, "underlyingAsset")
                    or (asset_decimals.get(underlying_asset_id) if underlying_asset_id else None)
                ),
            )

            result.append(item)
            accepted_count += 1

        diagnostics = {
            "pages": pages,
            "raw": raw_count,
            "chain": chain_count,
            "chains_requested": sorted(wanted_chains),
            "chains_found": sorted({m.get("chainId") for m in current_markets if m.get("chainId") is not None}),
            "future": future_count,
            "apy": apy_count,
            "pt_price": pt_price_count,
            "asset_prices_found": len(asset_prices),
            "asset_decimals_found": len(asset_decimals),
            "liquidity": liquidity_count,
            "swap_checked": swap_checked_count,
            "swap_available": swap_available_count,
            "swap_quotes_deferred": True,
            "accepted": accepted_count,
            "page_errors": page_errors,
            "sample_chain_ids": sample_chain_ids,
            "sample_expiries": sample_expiries,
            "sample_names": sample_names,
            "debug_market_keys": debug_market_keys,
            "debug_price_fields": debug_price_fields,
            "debug_swap_keys": debug_swap_keys,
            "debug_swap_rates": debug_swap_rates,
        }

        return result, diagnostics

# DeFi Alpha Agent v0.5.5.2

Short-horizon Pendle PT dislocation research engine with a cross-chain universe, underlying confirmation, real two-sided Pendle quotes, paper trading, and local historical replay.

## v0.5.5.2 focus

- Cross-chain Pendle universe: Ethereum, Arbitrum, Base and BNB Chain by default.
- Collection is observable and failure-tolerant: every market page, pricing batch and swap-price request is logged; transient HTTP failures retry with backoff and a failed page no longer leaves the process apparently frozen.
- Client-side filtering for future markets and minimum liquidity before expensive spot-swap checks.
- PT USD price from the Pendle market feed when available, with the live swapping-rate fallback already used in v0.4.x.
- Underlying USD price is stored with every snapshot and used as a confirmation filter.
- Market diagnostics show APY z-score, PT movement, underlying movement, liquidity and warm-up state even when there is no trade signal.
- Statistical signals remain deterministic. No LLM/API spend is needed.
- BUY candidates are still validated with a real two-sided Pendle Convert quote before opening a paper trade.
- Open paper positions are monitored and closed using a fresh exit quote at target/stop.
- `backtest` replays the locally collected minute/5-minute history. Historical replay uses a conservative configured cost because historical execution quotes are not available.

## Commands

```powershell
python -m app alpha
python -m app daemon
python -m app backtest
python -m app carry
```

`daemon` polls every 5 minutes by default. The history database is intentionally preserved across versions.

## Configuration

`PENDLE_CHAIN_IDS` can override the default cross-chain universe, for example:

```text
PENDLE_CHAIN_IDS=42161,1,8453,56
```

The strategy currently avoids fading a large adverse underlying move. This is a confirmation filter, not a prediction model.

## Paper-only safety

There is no wallet, private key, approval, signing or transaction submission. Do not use real money until the local paper/backtest results show positive expectancy after execution costs and the exit route has been independently verified.

Pendle's current documentation recommends the cross-chain `/v2/markets/all` endpoint for market discovery and documents the PT price field, while the real-time market spot swapping price endpoint is intended for current execution-oriented pricing. The newer Convert API is the recommended transaction/quote path for new integrations.


## v0.5.5 execution-cost architecture

Broad collection uses Pendle bulk endpoints only. Per-market swapping-price / SDK calls are deferred until a market becomes a validated alpha candidate. HTTP 404s are not retried; 429/5xx/timeouts remain retryable. Execution quotes use the candidate market's own chain ID via Pendle Convert v3. Existing `data/alpha_history.sqlite3` should be preserved.


## v0.5.5 execution-decimals preflight

Bulk collection no longer depends on `/v1/assets/all` exposing decimals. If
Pendle metadata omits decimals, the execution layer resolves the underlying
ERC-20 `decimals()` with a read-only `eth_call` only when a validated candidate
needs a real Convert quote. Results are cached for the process lifetime. No
wallet, signing, approvals, or transactions are involved.


## v0.6 methodology

- Historical ingestion keeps the future Pendle universe instead of filtering by liquidity at insert time.
- PT dislocation is measured in PT/underlying asset terms rather than USD PT.
- Signals require a cheap PT/underlying price z-score and a same-TTM-bucket high implied-APY z-score.
- Signals skip markets with less than 21 days or more than 730 days to expiry.
- Historical returns are evaluated at fixed 1h/4h/12h/24h horizons with conservative costs.
- Live execution checks only a real BUY Convert quote and an entry price-impact cap; it does not require an immediately profitable round trip.
- The agent remains read-only/paper-only.

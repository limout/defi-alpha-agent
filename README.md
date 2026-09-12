# DeFi Alpha Agent v0.6.15

Fixes the legacy-history migration issue introduced by the accounting-asset valuation change.

On collection, legacy snapshots with `price_basis IS NULL` are backfilled **only** for markets where current Pendle metadata proves `accountingAsset == underlyingAsset`. Those old PT/underlying ratios are then economically identical to PT/accounting-asset ratios.

Markets where accounting asset differs from underlying (yield-bearing/wrapper assets such as wstETH/sUSDe/sUSDai-style structures) are intentionally not relabeled and will warm up on the new canonical history.

No data is deleted.

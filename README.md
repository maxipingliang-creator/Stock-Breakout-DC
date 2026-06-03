# Stock-Breakout-DC — public market-data cache

This repository holds **cached public market data only** — daily price history
(OHLCV) and point-in-time SEC EDGAR fundamentals — collected automatically by a
scheduled GitHub Action.

- **Data only, by design.** No trading strategies, signals, ranking logic, or
  backtests live here. A CI guard (`.github/workflows/guard-no-strategy.yml`)
  fails the build if any non-data code or a committed credential ever appears.
- **How it's built.** The `Data Collection` workflow fetches the missing tail of
  price/fundamental history on this repo's free Actions minutes and commits the
  refreshed cache under `data/cache/`. Provider credentials live only in this
  repo's **encrypted Actions Secrets** — never in the tree.
- **Layout.** `data/cache/<provider>/<SYMBOL>.csv`, plus a `manifest.json` per
  provider recording the dataset version (provider, symbol count, date range).

The cache is consumed read-only (anonymous clone) by downstream tooling. Nothing
here is investment advice.

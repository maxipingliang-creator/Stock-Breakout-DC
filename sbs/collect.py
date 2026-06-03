"""sbs.collect — strategy-free data-collection entrypoint.

Refreshes the local price + point-in-time fundamentals cache using ONLY the
data/db/config layer. It deliberately does **not** import :mod:`sbs.cli`, and so
never loads any strategy, scanner, backtest, ranking, tracking, paper, regime,
indicators, reporting, or risk code.

Two uses:

* The public ``Stock-Breakout-DC`` data repo vendors the data layer plus this
  file and runs ``python -m sbs.collect`` on its own unlimited Actions minutes.
  Because no proprietary engine code is present (or imported), collection needs
  no cross-repo token — the public repo checks out only itself and pushes with
  the built-in ``GITHUB_TOKEN`` (see ``docs/DEPLOYMENT.md``).
* In this repo it doubles as a lighter ``update-data`` that avoids importing the
  whole CLI.

It mirrors :func:`sbs.cli.cmd_update_data`; keep the two in sync.
"""
from __future__ import annotations

import argparse
import os
from datetime import date, datetime

from .config import load_config
from .data.access import MarketData
from .db.database import connect
from .db.repositories import (
    DataVersionRepository,
    FundamentalsRepository,
    SecurityRepository,
)


def _as_of(value: str | None) -> date:
    """Resolve an ``--as-of`` value; blank/None means today (matches the CLI)."""
    return datetime.strptime(value, "%Y-%m-%d").date() if value else date.today()


def _default_db_path(cfg, provider_name: str) -> str:
    """Provider-scoped DB (``data/sbs_<provider>.sqlite``), matching the CLI, so
    different providers never share a securities/versions table. ``SBS_DB_URL``
    or an explicit ``--db`` always wins."""
    url = os.environ.get("SBS_DB_URL")
    if url:
        return url
    base = cfg.path("database.path", "data/sbs.sqlite")
    return str(base.with_name(f"{base.stem}_{provider_name}{base.suffix}"))


def update_data(
    provider: str | None = None,
    as_of: str | None = None,
    db: str | None = None,
    benchmark: bool = True,
) -> int:
    """Refresh the cache for the configured universe; return the symbol count.

    Strategy-free counterpart of :func:`sbs.cli.cmd_update_data`: the same
    fetch -> cache -> version-stamp -> fundamentals-persist flow, without
    importing any engine code.
    """
    cfg = load_config()
    provider_name = provider or cfg.default_provider
    db_path = db or _default_db_path(cfg, provider_name)
    conn = connect(db_path, init=True)
    md = MarketData.from_config(cfg, provider_name=provider_name, update=True)

    # Seed securities from the provider on first run (mirrors the CLI Context).
    sec_repo = SecurityRepository(conn)
    securities = sec_repo.all()
    if not securities:
        securities = md.provider.list_securities()
        if securities:
            sec_repo.upsert_many(securities)

    symbols = [s.symbol for s in securities]
    if benchmark:
        bench = cfg.get("regime.benchmark", "SPY")
        # Fetched via update_symbols even on a cold cache (no pre-load needed).
        if bench and bench not in symbols:
            symbols.append(bench)

    end = _as_of(as_of)
    dv = md.cache.update_symbols(symbols, end=end, universe_version=cfg.universe_version)
    version_id = DataVersionRepository(conn).add(dv)

    # Persist point-in-time fundamentals where the provider supplies them.
    fund_repo = FundamentalsRepository(conn)
    fund_rows = sum(
        fund_repo.upsert_symbol(s.symbol, md.provider.get_fundamentals(s.symbol))
        for s in securities
    )

    print(f"Updated {dv.symbol_count} symbols up to {end} -> data_version_id={version_id} "
          f"(provider={dv.provider}, range {dv.start}..{dv.end}); fundamentals rows={fund_rows}")
    if dv.symbol_count == 0:
        print("  ! 0 symbols fetched. Likely the provider is blocking this IP "
              "(Stooq returns HTTP 403 from datacenter/cloud IPs like CI) or the "
              "universe is empty/wrong. Re-run with SBS_DEBUG=1 to see the "
              "per-symbol response.")
    return dv.symbol_count


def main() -> None:
    ap = argparse.ArgumentParser(
        prog="python -m sbs.collect",
        description="Refresh the price + fundamentals cache (data layer only; "
                    "no strategy code is imported).",
    )
    ap.add_argument("--provider", default=None,
                    help="Data provider (synthetic|yfinance|alpaca|stooq); "
                         "defaults to config / $SBS_PROVIDER.")
    ap.add_argument("--as-of", default="",
                    help="As-of date YYYY-MM-DD; blank = today.")
    ap.add_argument("--db", default=None,
                    help="DB path; default is provider-scoped "
                         "data/sbs_<provider>.sqlite.")
    ap.add_argument("--no-benchmark", action="store_true",
                    help="Skip fetching the regime benchmark (SPY).")
    args = ap.parse_args()
    update_data(args.provider, args.as_of or None, args.db,
                benchmark=not args.no_benchmark)


if __name__ == "__main__":
    main()

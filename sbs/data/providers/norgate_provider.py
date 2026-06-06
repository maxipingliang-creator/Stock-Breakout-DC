"""Norgate Data provider — survivorship-bias-free US equities (local, Windows-only).

Norgate Data (norgatedata.com) is the retail gold standard for survivorship-free
US-equity research: it ships **point-in-time index membership** (S&P 500/400/600,
Russell 1000/2000/3000 — *current and delisted* constituents) plus clean,
split/capital-adjusted prices back to 1990 (Platinum tier). That makes it the
right source for the **mid/small-cap breakout expansion**, where a free Wikipedia
membership list doesn't exist (the Russell reconstitutes annually and isn't
documented) and Stooq's delisted coverage is only partial.

Access is via the official ``norgatedata`` package, which talks to the **Norgate
Data Updater (NDU)** — a *Windows-only* service maintaining the local database.
So this provider is a **local research tool** (like the ``alpaca+stooq`` home run),
not part of the cloud pipeline: ``import norgatedata`` is **lazy**, so this module
imports everywhere (the registry can discover it) and only an actual fetch needs
NDU running on Windows with a Norgate subscription/trial.

The survivorship-free roster comes from a Norgate ``"... Current & Past"`` watchlist
(``SBS_NORGATE_WATCHLIST``, default ``"Russell 3000 Current & Past"``): every name
ever in the index, each one's listing/delisting window derived from its first/last
quoted date — so the existing universe builder filters it survivorship-safely with
**no further changes** (it reads ``SecurityMeta.listing_date/delisting_date``).

Fundamentals are intentionally **not** wired: Norgate exposes only shallow
"current" fundamentals, not the point-in-time history ``rs_earnings_growth`` needs
— so ``get_fundamentals`` stays the empty default (use SEC EDGAR for those).

NOTE: written against the documented ``norgatedata`` API but **not exercisable in
CI** (the package + NDU are Windows-only); the unit tests mock ``norgatedata``.
Expect to shake out minor API specifics on the first live run during the trial.
"""
from __future__ import annotations

import os
from datetime import date, timedelta

import pandas as pd

from ..base import DataProvider, register_provider, standardize_ohlcv
from ..models import SecurityMeta

_DEFAULT_WATCHLIST = "Russell 3000 Current & Past"
# How stale a symbol's last quote must be (vs the freshest in the roster) before we
# call it delisted rather than active — guards against weekend/holiday gaps.
_DELISTED_STALE_DAYS = 7


def _as_date(v) -> date | None:
    if v is None:
        return None
    try:
        return pd.Timestamp(v).date()
    except Exception:  # noqa: BLE001 - unparseable date -> unknown
        return None


def _safe(fn, sym: str, default: str = "") -> str:
    """Call an optional ``norgatedata`` lookup, swallowing any failure."""
    if fn is None:
        return default
    try:
        return fn(sym) or default
    except Exception:  # noqa: BLE001 - reference lookups are best-effort
        return default


def _sector(nd, sym: str) -> str:
    """Best-effort GICS label. ``classification``'s third arg is a *result-type*
    ('Name' | 'ClassificationId'), not a level — 'Name' returns the GICS
    classification name; refine to the Sector level on the live trial if the
    sector-exposure caps need the top level specifically."""
    fn = getattr(nd, "classification", None)
    if fn is None:
        return "Unknown"
    try:
        return fn(sym, "GICS", "Name") or "Unknown"
    except Exception:  # noqa: BLE001
        return "Unknown"


@register_provider
class NorgateProvider(DataProvider):
    name = "norgate"
    version = "1"

    def __init__(self, watchlist: str | None = None, price_adjustment: str | None = None):
        self.watchlist = watchlist or os.environ.get("SBS_NORGATE_WATCHLIST") or _DEFAULT_WATCHLIST
        # CAPITAL = split + capital-event adjusted (the usual choice for price-pattern
        # backtests); override via SBS_NORGATE_ADJUST (e.g. TOTALRETURN, NONE).
        self.price_adjustment = (
            price_adjustment or os.environ.get("SBS_NORGATE_ADJUST") or "CAPITAL"
        )
        self._nd = None  # lazily imported norgatedata module

    # -- lazy norgatedata import (Windows + NDU only) ----------------------
    def _norgate(self):
        if self._nd is None:
            try:
                import norgatedata  # lazy: Windows-only, needs NDU running
            except ImportError as exc:  # pragma: no cover - environment-specific
                raise RuntimeError(
                    "norgate provider needs the 'norgatedata' package + the Norgate Data "
                    "Updater (NDU) running (Windows only). Install per norgatedata.com and "
                    "start NDU, then retry."
                ) from exc
            self._nd = norgatedata
        return self._nd

    def is_available(self) -> bool:
        try:
            self._norgate()
            return True
        except Exception:  # noqa: BLE001 - unavailable when norgatedata/NDU is absent
            return False

    # -- prices ------------------------------------------------------------
    def get_history(self, symbol, start=None, end=None, interval="1d"):
        nd = self._norgate()
        adj = getattr(
            nd.StockPriceAdjustmentType, self.price_adjustment, nd.StockPriceAdjustmentType.CAPITAL
        )
        try:
            df = nd.price_timeseries(
                symbol,
                stock_price_adjustment_setting=adj,
                padding_setting=nd.PaddingType.NONE,
                timeseriesformat="pandas-dataframe",
            )
        except Exception:  # noqa: BLE001 - a missing/bad symbol must not abort a batch
            return standardize_ohlcv(None)
        out = standardize_ohlcv(df)  # lowercases Open/High/Low/Close/Volume, sorts ascending
        if start is not None:
            out = out[out.index >= pd.Timestamp(start)]
        if end is not None:
            out = out[out.index <= pd.Timestamp(end)]
        return out

    # -- survivorship-free roster -----------------------------------------
    def list_securities(self) -> list[SecurityMeta]:
        nd = self._norgate()
        rows: list[tuple[str, date | None, date | None]] = []
        for sym in nd.watchlist_symbols(self.watchlist):
            try:
                rows.append((sym, _as_date(nd.first_quoted_date(sym)),
                             _as_date(nd.last_quoted_date(sym))))
            except Exception:  # noqa: BLE001 - skip a symbol Norgate can't resolve
                continue
        # The freshest last-quote across the roster ~ the database's current date;
        # a name quoting well before it is delisted (the survivorship signal).
        db_date = max((r[2] for r in rows if r[2] is not None), default=None)
        cutoff = (db_date - timedelta(days=_DELISTED_STALE_DAYS)) if db_date else None

        out: list[SecurityMeta] = []
        for sym, first_q, last_q in rows:
            delisted = bool(cutoff and last_q and last_q < cutoff)
            out.append(
                SecurityMeta(
                    symbol=sym,
                    name=_safe(getattr(nd, "security_name", None), sym),
                    exchange=_safe(getattr(nd, "exchange_name", None), sym),
                    sector=_sector(nd, sym),
                    listing_date=first_q,
                    delisting_date=(last_q if delisted else None),
                    status=("delisted" if delisted else "active"),
                )
            )
        return out

"""SEC EDGAR fundamentals (free, real filing dates → point-in-time).

The SEC ``companyfacts`` API exposes XBRL financial facts per company, and every
data point carries its **filing date** (``filed``) — so we index TTM EPS/revenue
by the date the figure actually became public. That makes these fundamentals
genuinely point-in-time (no restatement/lookahead), unlike yfinance's shallow,
period-end-only quarters.

EDGAR is keyed by **CIK** (a zero-padded company id), not ticker, so a
ticker→CIK map is required (SEC publishes one free: ``company_tickers.json``).

This module is pure-Python parsing + a thin urllib fetch; ``EdgarFundamentals``
plugs into the provider contract via ``get_fundamentals``. Network calls aren't
exercised by the offline tests — the XBRL parser (:func:`facts_to_fundamentals`)
is, against a small fixture.
"""
from __future__ import annotations

import json
import os
import time
import urllib.request
from pathlib import Path
from typing import Any

import pandas as pd

from ..config import PROJECT_ROOT

FUND_COLS = ["eps_ttm", "revenue_ttm", "eps_growth_yoy", "revenue_growth_yoy", "shares_outstanding"]

# XBRL us-gaap concept names we look for, most-preferred first.
_REVENUE_TAGS = [
    "RevenueFromContractWithCustomerExcludingAssessedTax",
    "RevenueFromContractWithCustomerIncludingAssessedTax",
    "Revenues",
    "SalesRevenueNet",
]
_EPS_TAGS = ["EarningsPerShareDiluted", "EarningsPerShareBasic"]
_SHARES_TAGS = [
    "WeightedAverageNumberOfDilutedSharesOutstanding",
    "WeightedAverageNumberOfSharesOutstandingBasic",
    "CommonStockSharesOutstanding",
]

SEC_FACTS_URL = "https://data.sec.gov/api/xbrl/companyfacts/CIK{cik:010d}.json"
SEC_TICKERS_URL = "https://www.sec.gov/files/company_tickers.json"
# SEC requires a descriptive User-Agent with contact info.
_UA = "stock-breakout-scanner research (contact: set SBS_SEC_USER_AGENT)"


def _empty() -> pd.DataFrame:
    return pd.DataFrame(columns=FUND_COLS, index=pd.DatetimeIndex([], name="report_date"))


def _quarterly_points(unit_rows: list[dict], *, want_duration: bool) -> pd.DataFrame:
    """Extract (filed, end, val) for the ~quarterly facts of one concept.

    ``want_duration`` True keeps ~90-day period facts (flows: revenue, EPS);
    False keeps instant facts (stocks: shares outstanding). Keyed by period
    ``end``; ``filed`` is retained as the point-in-time known-date.
    """
    rows = []
    for r in unit_rows:
        end = r.get("end")
        filed = r.get("filed")
        val = r.get("val")
        if end is None or filed is None or val is None:
            continue
        start = r.get("start")
        if want_duration:
            if start is None:
                continue
            days = (pd.Timestamp(end) - pd.Timestamp(start)).days
            if not (60 <= days <= 120):   # keep single quarters, drop YTD/annual spans
                continue
        else:
            if start is not None:
                continue
        rows.append({"end": pd.Timestamp(end), "filed": pd.Timestamp(filed), "val": float(val)})
    if not rows:
        return pd.DataFrame(columns=["end", "filed", "val"])
    df = pd.DataFrame(rows).sort_values(["end", "filed"])
    # One row per period end (first filing of that period — the original, not restatements).
    return df.groupby("end", as_index=False).first()


def _pick_concept(us_gaap: dict, tags: list[str], *, want_duration: bool) -> pd.DataFrame:
    for tag in tags:
        node = us_gaap.get(tag)
        if not node:
            continue
        units = node.get("units", {})
        # Prefer USD / USD-per-share / shares unit keys, else take the first.
        unit_rows = next((v for k, v in units.items() if k in ("USD", "USD/shares", "shares")), None)
        if unit_rows is None and units:
            unit_rows = next(iter(units.values()))
        if not unit_rows:
            continue
        pts = _quarterly_points(unit_rows, want_duration=want_duration)
        if not pts.empty:
            return pts
    return pd.DataFrame(columns=["end", "filed", "val"])


def facts_to_fundamentals(companyfacts: dict[str, Any]) -> pd.DataFrame:
    """Convert an EDGAR ``companyfacts`` JSON into the canonical fundamentals frame.

    Returns TTM EPS/revenue + YoY growth + shares, indexed by **filing date**
    (``report_date``) so reads are point-in-time. Empty if the needed concepts
    aren't present.
    """
    us_gaap = (companyfacts or {}).get("facts", {}).get("us-gaap", {})
    if not us_gaap:
        return _empty()

    rev = _pick_concept(us_gaap, _REVENUE_TAGS, want_duration=True).rename(columns={"val": "revenue_q"})
    eps = _pick_concept(us_gaap, _EPS_TAGS, want_duration=True).rename(columns={"val": "eps_q"})
    shr = _pick_concept(us_gaap, _SHARES_TAGS, want_duration=False).rename(columns={"val": "shares"})
    if rev.empty or eps.empty:
        return _empty()

    # Align revenue + EPS on the period end; filing date = later of the two filings.
    m = rev.merge(eps, on="end", how="inner", suffixes=("_rev", "_eps"))
    if m.empty:
        return _empty()
    m["filed"] = m[["filed_rev", "filed_eps"]].max(axis=1)
    m = m.sort_values("end")
    m["revenue_ttm"] = m["revenue_q"].rolling(4).sum()
    m["eps_ttm"] = m["eps_q"].rolling(4).sum()
    m["revenue_growth_yoy"] = m["revenue_ttm"].pct_change(4)
    m["eps_growth_yoy"] = m["eps_ttm"].pct_change(4)

    if not shr.empty:
        shr_s = shr.sort_values("end").set_index("end")["shares"]
        m["shares_outstanding"] = m["end"].map(shr_s).to_numpy()
    else:
        m["shares_outstanding"] = float("nan")

    out = m.set_index(pd.DatetimeIndex(m["filed"], name="report_date"))[FUND_COLS]
    out = out[~out.index.duplicated(keep="last")].sort_index()
    return out.dropna(how="all")


class EdgarClient:
    """Thin SEC EDGAR HTTP client with a disk cache.

    Fundamentals change ~quarterly, so re-downloading every company's filings on
    every run is wasteful. We cache the ticker→CIK map and per-company facts JSON
    to ``data/cache/edgar`` and only re-fetch when older than ``max_age_days``
    (default 7). This keeps a large-universe daily scan's data-collection cheap.
    """

    def __init__(self, user_agent: str | None = None, cache_dir: str | None = None,
                 max_age_days: float = 7.0):
        self.user_agent = user_agent or os.environ.get("SBS_SEC_USER_AGENT") or _UA
        self.cache_dir = Path(
            cache_dir or os.environ.get("SBS_EDGAR_CACHE") or (PROJECT_ROOT / "data" / "cache" / "edgar")
        )
        self.max_age_days = max_age_days
        self._cik_map: dict[str, int] | None = None

    def _get(self, url: str) -> bytes:
        req = urllib.request.Request(url, headers={"User-Agent": self.user_agent})
        with urllib.request.urlopen(req, timeout=30) as resp:  # noqa: S310 - fixed sec.gov hosts
            return resp.read()

    def _cached_json(self, url: str, filename: str) -> dict:
        """Return JSON from disk if fresh, else fetch and cache it."""
        path = self.cache_dir / filename
        if path.exists() and (time.time() - path.stat().st_mtime) < self.max_age_days * 86400:
            try:
                return json.loads(path.read_text())
            except Exception:  # noqa: BLE001 - corrupt cache -> refetch
                pass
        raw = self._get(url).decode("utf-8")
        data = json.loads(raw)
        try:
            self.cache_dir.mkdir(parents=True, exist_ok=True)
            path.write_text(raw)
        except OSError:
            pass  # cache is best-effort
        return data

    def ticker_to_cik(self, symbol: str) -> int | None:
        if self._cik_map is None:
            data = self._cached_json(SEC_TICKERS_URL, "company_tickers.json")
            # company_tickers.json is {"0": {"cik_str":.., "ticker":..}, ...}
            self._cik_map = {
                str(row["ticker"]).upper(): int(row["cik_str"]) for row in data.values()
            }
        return self._cik_map.get(symbol.upper())

    def company_facts(self, cik: int) -> dict:
        return self._cached_json(SEC_FACTS_URL.format(cik=cik), f"CIK{cik:010d}.json")


class EdgarFundamentals:
    """Mixin-style helper a provider can delegate ``get_fundamentals`` to."""

    def __init__(self, user_agent: str | None = None):
        self.client = EdgarClient(user_agent)

    def get_fundamentals(self, symbol: str) -> pd.DataFrame:
        try:
            cik = self.client.ticker_to_cik(symbol)
            if cik is None:
                return _empty()
            return facts_to_fundamentals(self.client.company_facts(cik))
        except Exception:  # noqa: BLE001 - never let a flaky SEC call break a run
            return _empty()

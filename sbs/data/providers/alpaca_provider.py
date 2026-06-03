"""Alpaca provider (live source) — REST via urllib (no SDK dependency).

Unlike Stooq, Alpaca works from cloud/datacenter IPs (Codespaces, CI), and it's
also a broker — so it's the natural source for live/paper prices. Free tier uses
the **IEX** feed (set ``ALPACA_FEED=sip`` for the paid full-market feed); daily
history goes back ~2016. Bars are split+dividend **adjusted** by default
(``adjustment=all``) for clean long backtests.

Auth via env:
  ALPACA_API_KEY, ALPACA_SECRET_KEY      (required)
  ALPACA_BASE_URL                         (trading host for the assets/universe
                                           list; defaults to the paper host)
  ALPACA_FEED                             (iex | sip; default iex)

Alpaca has no fundamentals, so :meth:`get_fundamentals` delegates to free SEC
EDGAR (point-in-time). Network paths aren't exercised by the offline tests — the
pure parsers (:func:`parse_bars`, :func:`parse_assets`) are.
"""
from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
from datetime import date

import pandas as pd

from ...config import PROJECT_ROOT
from ..base import DataProvider, register_provider, standardize_ohlcv
from ..models import SecurityMeta
from ..reference import load_securities_csv

DATA_HOST = "https://data.alpaca.markets"
DEFAULT_TRADING_HOST = "https://paper-api.alpaca.markets"


def parse_bars(bars: list[dict]) -> pd.DataFrame:
    """Alpaca v2 stock bars (``t,o,h,l,c,v``) -> canonical OHLCV (one row per day)."""
    if not bars:
        return standardize_ohlcv(None)
    df = pd.DataFrame(bars).rename(
        columns={"t": "date", "o": "open", "h": "high", "l": "low", "c": "close", "v": "volume"}
    )
    # Daily bar timestamps are RFC3339 (UTC); reduce to the calendar date.
    df["date"] = pd.to_datetime(df["date"], utc=True).dt.tz_localize(None).dt.normalize()
    return standardize_ohlcv(df.set_index("date"))


def parse_assets(assets: list[dict]) -> list[SecurityMeta]:
    """Alpaca ``/v2/assets`` -> SecurityMeta (no market cap/sector from Alpaca)."""
    out: list[SecurityMeta] = []
    for a in assets:
        if a.get("class") not in (None, "us_equity"):
            continue
        symbol = (a.get("symbol") or "").upper()
        if not symbol:
            continue
        out.append(SecurityMeta(
            symbol=symbol,
            name=a.get("name") or "",
            exchange=(a.get("exchange") or "").upper(),
            security_type="COMMON",
            market_cap=0.0,                       # unknown -> universe uses liquidity filter
            status="active" if a.get("status") == "active" else "inactive",
        ))
    return out


@register_provider
class AlpacaProvider(DataProvider):
    name = "alpaca"
    version = "rest-v2"

    def __init__(self, universe_csv: str | None = None) -> None:
        self.api_key = os.environ.get("ALPACA_API_KEY")
        self.secret = os.environ.get("ALPACA_SECRET_KEY")
        self.trading_host = (os.environ.get("ALPACA_BASE_URL") or DEFAULT_TRADING_HOST).rstrip("/")
        self.feed = os.environ.get("ALPACA_FEED", "iex")
        self.adjustment = os.environ.get("ALPACA_ADJUSTMENT", "all")
        self.universe_csv = (
            universe_csv or os.environ.get("SBS_UNIVERSE_CSV")
            or str(PROJECT_ROOT / "config" / "universe.csv")
        )
        self._edgar = None

    # -- HTTP ---------------------------------------------------------------
    def _headers(self) -> dict:
        return {"APCA-API-KEY-ID": self.api_key or "", "APCA-API-SECRET-KEY": self.secret or ""}

    def _get_json(self, url: str):
        req = urllib.request.Request(url, headers=self._headers())
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:  # noqa: S310 - fixed alpaca hosts
                return json.loads(resp.read().decode("utf-8"))
        except (urllib.error.URLError, ValueError) as exc:
            if os.environ.get("SBS_DEBUG"):
                print(f"[alpaca] request failed: {exc!r} ({url})", file=sys.stderr)
            return None

    # -- prices -------------------------------------------------------------
    def get_history(
        self,
        symbol: str,
        start: date | None = None,
        end: date | None = None,
        interval: str = "1d",
    ) -> pd.DataFrame:
        if not self.is_available():
            raise RuntimeError(
                "Alpaca provider needs ALPACA_API_KEY / ALPACA_SECRET_KEY set. "
                "Use `--provider synthetic` for offline runs."
            )
        base = f"{DATA_HOST}/v2/stocks/{urllib.parse.quote(symbol)}/bars"
        params = {
            "timeframe": "1Day",
            "adjustment": self.adjustment,
            "feed": self.feed,
            "limit": 10000,
            "start": str(start) if start else "2015-01-01",
        }
        if end is not None:
            params["end"] = str(end)

        bars: list[dict] = []
        token: str | None = None
        while True:
            page = dict(params)
            if token:
                page["page_token"] = token
            data = self._get_json(base + "?" + urllib.parse.urlencode(page))
            if not data:
                break
            bars.extend(data.get("bars") or [])
            token = data.get("next_page_token")
            if not token:
                break
        return parse_bars(bars)

    # -- universe -----------------------------------------------------------
    def list_securities(self) -> list[SecurityMeta]:
        """Default: the curated CSV universe (controlled set; survivorship dates;
        mirrors yfinance/stooq). Set ``SBS_ALPACA_ALL_ASSETS=1`` to pull Alpaca's
        full active asset list instead (~thousands of names — rate-limited, so
        trim / pre-build a universe snapshot before a full run)."""
        if not os.environ.get("SBS_ALPACA_ALL_ASSETS"):
            return load_securities_csv(self.universe_csv)
        if not self.is_available():
            return []
        data = self._get_json(f"{self.trading_host}/v2/assets?status=active&asset_class=us_equity")
        return parse_assets(data if isinstance(data, list) else [])

    # -- fundamentals (Alpaca has none -> SEC EDGAR) ------------------------
    def get_fundamentals(self, symbol: str) -> pd.DataFrame:
        if self._edgar is None:
            from ..edgar import EdgarFundamentals  # lazy
            self._edgar = EdgarFundamentals()
        return self._edgar.get_fundamentals(symbol)

    def is_available(self) -> bool:
        return bool(self.api_key and self.secret)

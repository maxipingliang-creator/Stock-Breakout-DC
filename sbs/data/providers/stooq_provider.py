"""Stooq provider (free, no API key) — direct EOD CSV download.

Stooq serves free end-of-day CSVs at ``https://stooq.com/q/d/l/?s=<sym>&i=d``
and, unlike Yahoo, **retains a fair amount of delisted history** — making it the
best *truly free* source for (partly) survivorship-aware backtests. Coverage of
delisted names is partial and inconsistent, so treat it as best-effort.

US tickers use the ``.us`` suffix (``AAPL`` -> ``aapl.us``). The universe
(membership + delisting dates) comes from a CSV — generate one for free from
Alpha Vantage ``LISTING_STATUS`` via ``scripts/fetch_universe.py``.
"""
from __future__ import annotations

import io
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import date

import pandas as pd

from ...config import PROJECT_ROOT
from ..base import DataProvider, register_provider, standardize_ohlcv
from ..models import SecurityMeta
from ..reference import load_securities_csv

STOOQ_URL = "https://stooq.com/q/d/l/?s={sym}&i=d"
# Stooq blocks the default Python-urllib User-Agent; present a browser-like one.
_UA = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120 Safari/537.36"


def _parse_stooq_csv(text: str) -> pd.DataFrame:
    """Parse a Stooq EOD CSV (``Date,Open,High,Low,Close,Volume``) -> canonical OHLCV.

    Returns an empty frame for Stooq's error responses ("No data", HTML, blanks).
    """
    if not text or text.lstrip().startswith("<") or "No data" in text:
        return standardize_ohlcv(None)
    try:
        df = pd.read_csv(io.StringIO(text))
    except Exception:  # noqa: BLE001
        return standardize_ohlcv(None)
    if "Date" not in df.columns or df.empty:
        return standardize_ohlcv(None)
    df = df.rename(columns=str.lower).set_index("date")
    return standardize_ohlcv(df)  # lowercases, parses dates, sorts ascending


@register_provider
class StooqProvider(DataProvider):
    name = "stooq"
    version = "csv"

    def __init__(self, universe_csv: str | None = None, proxy: str | None = None, retries: int = 3):
        self.universe_csv = (
            universe_csv or os.environ.get("SBS_UNIVERSE_CSV")
            or str(PROJECT_ROOT / "config" / "universe.csv")
        )
        # Stooq 403s datacenter IPs; route through a (residential) proxy if given.
        self.proxy = proxy or os.environ.get("SBS_HTTP_PROXY")
        self.retries = retries
        # Stooq has no fundamentals; fill the gap with free, point-in-time SEC
        # EDGAR data (lazy — only constructed/queried when a strategy needs it).
        self._edgar = None  # type: ignore[var-annotated]

    @staticmethod
    def _stooq_symbol(symbol: str) -> str:
        s = symbol.lower()
        return s if "." in s else f"{s}.us"  # default US market suffix

    def _opener(self) -> urllib.request.OpenerDirector:
        if self.proxy:
            handler = urllib.request.ProxyHandler({"http": self.proxy, "https": self.proxy})
            return urllib.request.build_opener(handler)
        return urllib.request.build_opener()

    def _fetch(self, url: str, symbol: str, debug) -> str | None:
        """GET with retry/backoff. A 403/404 won't recover (Stooq IP block), so
        we don't retry those — we surface them and bail."""
        req = urllib.request.Request(url, headers={"User-Agent": _UA})
        opener = self._opener()
        for attempt in range(self.retries):
            try:
                with opener.open(req, timeout=30) as resp:  # noqa: S310 - fixed https host
                    return resp.read().decode("utf-8", errors="replace")
            except urllib.error.HTTPError as exc:
                if exc.code in (403, 404):
                    if debug:
                        print(f"[stooq] {symbol}: HTTP {exc.code} (likely IP block "
                              f"from this host{' via proxy' if self.proxy else ''}); not retrying",
                              file=sys.stderr)
                    return None
                last = exc
            except Exception as exc:  # noqa: BLE001 - transient network blip
                last = exc
            if attempt < self.retries - 1:
                time.sleep(2 ** attempt)  # 1s, 2s, 4s, ...
        if debug:
            print(f"[stooq] {symbol}: request failed after {self.retries} tries: {last!r}",
                  file=sys.stderr)
        return None

    def get_history(
        self,
        symbol: str,
        start: date | None = None,
        end: date | None = None,
        interval: str = "1d",
    ) -> pd.DataFrame:
        url = STOOQ_URL.format(sym=urllib.parse.quote(self._stooq_symbol(symbol)))
        debug = os.environ.get("SBS_DEBUG")
        text = self._fetch(url, symbol, debug)
        if text is None:
            return standardize_ohlcv(None)
        out = _parse_stooq_csv(text)
        if debug and out.empty:
            # Stooq returns errors (rate limit / no data) as the CSV *body*.
            print(f"[stooq] {symbol}: empty result; response head: "
                  f"{text.strip()[:120]!r}", file=sys.stderr)
        if start is not None:
            out = out[out.index >= pd.Timestamp(start)]
        if end is not None:
            out = out[out.index <= pd.Timestamp(end)]
        return out

    def list_securities(self) -> list[SecurityMeta]:
        return load_securities_csv(self.universe_csv)

    def get_fundamentals(self, symbol: str) -> pd.DataFrame:
        """Point-in-time fundamentals from SEC EDGAR (real filing dates)."""
        if self._edgar is None:
            from ..edgar import EdgarFundamentals  # lazy: avoids cost when unused
            self._edgar = EdgarFundamentals()
        return self._edgar.get_fundamentals(symbol)

    def is_available(self) -> bool:
        return True

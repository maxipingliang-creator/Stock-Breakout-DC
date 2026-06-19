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

DATA_HOST = "https://data.alpaca.markets"
DEFAULT_TRADING_HOST = "https://paper-api.alpaca.markets"

# A wide universe sweep (~500+ symbols) outruns Alpaca's rate limit; without backoff the
# 429s are swallowed and every symbol past the throttle point silently keeps its old bar —
# which staled the benchmark and skipped a whole scan. Retry 429 / transient errors with
# exponential backoff (honouring Retry-After), so the sweep self-paces and completes.
_MAX_RETRIES = 5


def _retry_after_seconds(exc: urllib.error.HTTPError) -> float | None:
    """Alpaca's ``Retry-After`` (seconds) on a 429, when present."""
    try:
        ra = exc.headers.get("Retry-After")
        return float(ra) if ra else None
    except (AttributeError, TypeError, ValueError):
        return None


def _backoff_seconds(attempt: int) -> float:
    """Exponential backoff, capped: 1, 2, 4, 8, 16, 30, 30, …"""
    return min(2.0 ** attempt, 30.0)


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


# Max symbols per multi-symbol bars request. Bounds the URL length; Alpaca paginates the
# rest of the data within each chunk. A ~500-name sweep is then ~3 chunked requests (each
# paginated) instead of ~500 single-symbol calls — far less rate-limit pressure.
_BATCH_SYMBOLS = 200


def _chunked(items: list[str], size: int):
    for i in range(0, len(items), size):
        yield items[i:i + size]


def parse_multi_bars(payload: dict) -> dict[str, list]:
    """Alpaca multi-symbol ``/v2/stocks/bars`` -> ``{symbol: [raw bar dicts]}`` for one page.
    The response keys ``bars`` by symbol (unlike the single-symbol endpoint's flat array)."""
    return {sym: (bars or []) for sym, bars in (payload.get("bars") or {}).items()}



def parse_latest_trades(payload: dict) -> dict[str, float]:
    """Alpaca multi-symbol ``/v2/stocks/trades/latest`` -> ``{symbol: last_price}``."""
    out: dict[str, float] = {}
    for sym, trade in (payload.get("trades") or {}).items():
        try:
            out[sym] = float(trade["p"])
        except (KeyError, TypeError, ValueError):
            continue
    return out


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
        base = (os.environ.get("ALPACA_BASE_URL") or DEFAULT_TRADING_HOST).rstrip("/")
        # /v2/calendar and /v2/assets are TRADING-API endpoints — they 404 on the *data* host.
        # ALPACA_BASE_URL is easily (mis)set to the data host since this is the data provider,
        # which silently broke the trading-day calendar (the gap that let the scan fire on
        # Juneteenth). Remap the data host to the paper trading host — the calendar is
        # market-wide, identical on paper/live, so this is always safe.
        if "data.alpaca.markets" in base:
            base = DEFAULT_TRADING_HOST
        self.trading_host = base
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
        """GET + parse JSON, retrying rate limits (HTTP 429) and transient errors with
        exponential backoff (honouring ``Retry-After``). Returns None only after exhausting
        retries, or immediately on a hard 4xx (e.g. 404) — never on a swallowed 429, which
        used to silently drop the tail of a large universe sweep."""
        req = urllib.request.Request(url, headers=self._headers())
        for attempt in range(_MAX_RETRIES + 1):
            try:
                with urllib.request.urlopen(req, timeout=30) as resp:  # noqa: S310 - fixed alpaca hosts
                    return json.loads(resp.read().decode("utf-8"))
            except urllib.error.HTTPError as exc:
                transient = exc.code == 429 or 500 <= exc.code < 600
                if transient and attempt < _MAX_RETRIES:
                    wait = _retry_after_seconds(exc)
                    time.sleep(wait if wait is not None else _backoff_seconds(attempt))
                    continue
                if os.environ.get("SBS_DEBUG"):
                    print(f"[alpaca] HTTP {exc.code} after {attempt} retr(ies): {url}", file=sys.stderr)
                return None
            except (urllib.error.URLError, ValueError, TimeoutError) as exc:
                if attempt < _MAX_RETRIES:
                    time.sleep(_backoff_seconds(attempt))
                    continue
                if os.environ.get("SBS_DEBUG"):
                    print(f"[alpaca] request failed: {exc!r} ({url})", file=sys.stderr)
                return None
        return None

    # -- calendar -----------------------------------------------------------
    def get_calendar(self, start: date, end: date) -> list[date]:
        """Trading sessions in [start, end] from Alpaca's /v2/calendar (a trading-API
        endpoint — see the host remap in __init__). Returns [] on failure / missing creds,
        so callers fall back to a weekday+holiday heuristic."""
        url = (f"{self.trading_host}/v2/calendar"
               f"?start={start.isoformat()}&end={end.isoformat()}")
        out: list[date] = []
        for r in self._get_json(url) or []:
            d = r.get("date")
            if d:
                try:
                    out.append(date.fromisoformat(d))
                except ValueError:
                    pass
        if not out and self.is_available():
            # Creds are set but the calendar came back empty — a real misconfig signal (e.g.
            # ALPACA_BASE_URL not a trading host, or auth scoped to data only). Surface it so
            # the trading-day gate's fallback is no longer silent.
            print(f"[alpaca] /v2/calendar at {self.trading_host} returned no sessions for "
                  f"{start}..{end}; the trading-day gate will use its weekday+holiday fallback.",
                  file=sys.stderr)
        return out

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

    def get_history_batch(self, symbols, start=None, end=None, interval="1d"):
        """Bulk OHLCV via the multi-symbol bars endpoint — one (paginated) request per
        ``_BATCH_SYMBOLS`` symbols instead of one per symbol. This is the lever that keeps a
        wide universe sweep under Alpaca's rate limit: ~500 names become a handful of calls,
        not 500. Returns a canonical frame per symbol (empty for names with no data)."""
        if not symbols:
            return {}
        if not self.is_available():
            raise RuntimeError(
                "Alpaca provider needs ALPACA_API_KEY / ALPACA_SECRET_KEY set. "
                "Use `--provider synthetic` for offline runs."
            )
        base = f"{DATA_HOST}/v2/stocks/bars"
        params = {
            "timeframe": "1Day",
            "adjustment": self.adjustment,
            "feed": self.feed,
            "limit": 10000,
            "start": str(start) if start else "2015-01-01",
        }
        if end is not None:
            params["end"] = str(end)
        out: dict[str, pd.DataFrame] = {s: standardize_ohlcv(None) for s in symbols}
        for chunk in _chunked(list(symbols), _BATCH_SYMBOLS):
            raw: dict[str, list] = {}
            token: str | None = None
            while True:                       # paginate this chunk (bars span pages by symbol)
                page = dict(params, symbols=",".join(chunk))
                if token:
                    page["page_token"] = token
                data = self._get_json(base + "?" + urllib.parse.urlencode(page))
                if not data:
                    break
                for sym, bars in parse_multi_bars(data).items():
                    raw.setdefault(sym, []).extend(bars)
                token = data.get("next_page_token")
                if not token:
                    break
            for sym, bars in raw.items():
                out[sym] = parse_bars(bars)
        return out

    # -- real-time (intraday entry confirmation) ----------------------------
    def latest_prices(self, symbols: list[str]) -> dict[str, float]:
        """Real-time last-trade price per symbol (IEX feed) in one call — for the
        intraday hold-above check. Empty on missing creds/error; symbols with no
        recent trade are simply absent (caller treats as 'unknown')."""
        if not symbols or not self.is_available():
            return {}
        qs = urllib.parse.quote(",".join(symbols))
        data = self._get_json(f"{DATA_HOST}/v2/stocks/trades/latest?symbols={qs}&feed={self.feed}")
        return parse_latest_trades(data or {})

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

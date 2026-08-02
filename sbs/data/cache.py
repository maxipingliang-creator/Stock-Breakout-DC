"""Local price cache with incremental updates and data versioning.

Layout::

    <cache_dir>/<provider>/<SYMBOL>.csv     # canonical OHLCV, one file per symbol
    <cache_dir>/<provider>/manifest.json    # coverage + version metadata

Incremental updates only fetch the missing *tail* (last cached date onward),
which is the main lever for keeping GitHub Actions minutes low — we never
re-download history we already have.
"""
from __future__ import annotations

import json
from datetime import date, datetime, timezone
from pathlib import Path

import pandas as pd

from .base import DataProvider, standardize_ohlcv
from .models import DataVersion


class DataCache:
    def __init__(self, provider: DataProvider, cache_dir: Path, interval: str = "1d"):
        self.provider = provider
        self.interval = interval
        self.dir = Path(cache_dir) / provider.name
        self.dir.mkdir(parents=True, exist_ok=True)

    # -- paths --------------------------------------------------------------
    def _path(self, symbol: str) -> Path:
        safe = symbol.replace("/", "_").upper()
        return self.dir / f"{safe}.csv"

    @property
    def manifest_path(self) -> Path:
        return self.dir / "manifest.json"

    # -- disk IO ------------------------------------------------------------
    def load(self, symbol: str) -> pd.DataFrame:
        path = self._path(symbol)
        if not path.exists():
            return standardize_ohlcv(None)
        df = pd.read_csv(path, index_col="date", parse_dates=["date"])
        return standardize_ohlcv(df)

    def _save(self, symbol: str, df: pd.DataFrame) -> None:
        df.to_csv(self._path(symbol), index_label="date")

    # -- incremental update -------------------------------------------------
    @staticmethod
    def _last(df: pd.DataFrame) -> date | None:
        """The last cached bar's date, or None for an empty/cold series."""
        return None if df.empty else df.index.max().date()

    @staticmethod
    def _merge(cached: pd.DataFrame, fresh: pd.DataFrame | None) -> pd.DataFrame:
        """Combine cached history with a freshly fetched tail (fresh wins on overlap)."""
        if cached.empty:
            return fresh if fresh is not None else standardize_ohlcv(None)
        if fresh is None or fresh.empty:
            return cached
        merged = pd.concat([cached, fresh])
        return merged[~merged.index.duplicated(keep="last")].sort_index()

    def update_symbol(self, symbol: str, end: date | None = None,
                      start: date | None = None) -> pd.DataFrame:
        """Refresh one symbol, fetching only the missing tail. Returns full series.
        The last cached day is re-fetched (start=last, inclusive) in case it was provisional.

        When ``start`` is given and the cache begins materially later than it (a truncated
        first fetch), the earlier ``[start, cache_first]`` gap is **back-filled** as well:
        incremental updates otherwise only ever extend the *tail* forward, so a short series
        can never heal. Used for the benchmark, whose full history the walk-forward calendar
        depends on."""
        cached = self.load(symbol)
        if start is not None and not cached.empty:
            first = cached.index.min().date()
            if (first - start).days > 5:      # cache starts materially after `start` → backfill
                earlier = self.provider.get_history(symbol, start, first, self.interval)
                if earlier is not None and not earlier.empty:
                    cached = self._merge(earlier, cached)   # prepend; existing bars win on overlap
        fresh = self.provider.get_history(symbol, self._last(cached), end, self.interval)
        merged = self._merge(cached, fresh)
        if not merged.empty:
            self._save(symbol, merged)
        return merged

    def update_symbols(
        self,
        symbols: list[str],
        end: date | None = None,
        universe_version: str = "0",
    ) -> DataVersion:
        """Incrementally refresh many symbols and write/return a DataVersion.

        Symbols are grouped by their last cached date, and each group's missing tail is
        fetched in one bulk request (``provider.get_history_batch``). On a provider with a
        multi-symbol endpoint (Alpaca) a uniform daily sweep of ~500 names becomes a handful
        of calls instead of ~500 — the lever that keeps it under the rate limit. Providers
        without a bulk endpoint use the per-symbol default and behave exactly as before."""
        cached = {sym: self.load(sym) for sym in symbols}
        buckets: dict[date | None, list[str]] = {}
        for sym in symbols:
            buckets.setdefault(self._last(cached[sym]), []).append(sym)

        first_dates, last_dates, count = [], [], 0
        for last, group in buckets.items():
            fresh = self.provider.get_history_batch(group, last, end, self.interval)
            for sym in group:
                merged = self._merge(cached[sym], fresh.get(sym))
                if merged.empty:
                    continue
                self._save(sym, merged)
                count += 1
                first_dates.append(merged.index.min())
                last_dates.append(merged.index.max())
        version = DataVersion(
            provider=self.provider.name,
            provider_version=self.provider.version,
            download_date=datetime.now(timezone.utc).isoformat(timespec="seconds"),
            interval=self.interval,
            symbol_count=count,
            universe_version=universe_version,
            start=str(min(first_dates).date()) if first_dates else None,
            end=str(max(last_dates).date()) if last_dates else None,
        )
        self._write_manifest(version)
        return version

    def _write_manifest(self, version: DataVersion) -> None:
        self.manifest_path.write_text(json.dumps(version.to_row(), indent=2))

    def read_manifest(self) -> dict:
        if not self.manifest_path.exists():
            return {}
        return json.loads(self.manifest_path.read_text())

    # -- read access --------------------------------------------------------
    def get(
        self,
        symbol: str,
        start: date | None = None,
        end: date | None = None,
        update: bool = False,
    ) -> pd.DataFrame:
        df = self.update_symbol(symbol, end) if update else self.load(symbol)
        if start is not None:
            df = df[df.index >= pd.Timestamp(start)]
        if end is not None:
            df = df[df.index <= pd.Timestamp(end)]
        return df.copy()

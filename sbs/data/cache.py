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
    def update_symbol(self, symbol: str, end: date | None = None) -> pd.DataFrame:
        """Refresh one symbol, fetching only the missing tail. Returns full series."""
        cached = self.load(symbol)
        if cached.empty:
            fresh = self.provider.get_history(symbol, None, end, self.interval)
            merged = fresh
        else:
            last = cached.index.max().date()
            # Re-fetch the last cached day too, in case it was provisional.
            fresh = self.provider.get_history(symbol, last, end, self.interval)
            merged = pd.concat([cached, fresh])
            merged = merged[~merged.index.duplicated(keep="last")].sort_index()
        if not merged.empty:
            self._save(symbol, merged)
        return merged

    def update_symbols(
        self,
        symbols: list[str],
        end: date | None = None,
        universe_version: str = "0",
    ) -> DataVersion:
        """Incrementally refresh many symbols and write/return a DataVersion."""
        first_dates, last_dates, count = [], [], 0
        for sym in symbols:
            df = self.update_symbol(sym, end)
            if not df.empty:
                count += 1
                first_dates.append(df.index.min())
                last_dates.append(df.index.max())
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

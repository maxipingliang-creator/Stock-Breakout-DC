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
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pandas as pd

from .base import DataProvider, standardize_ohlcv
from .models import DataVersion

# A provider serving *adjusted* history (Alpaca's `adjustment=all`) re-bases its WHOLE series
# when a corporate action lands. A cache that only ever appends the tail therefore keeps old
# bars on the old basis and new bars on the new one, leaving a permanent discontinuity at the
# action's date which never heals — because the tail is all we fetch.
#
# Found live 2026-10: KLAC's 1:10 split (2026-06-10) left a 200-day SMA of 1,019 against a
# price of 197, silently failing `close > SMA200` and disqualifying the name from every trend
# filter for 200 sessions; DD's 1:3 reverse (2026-06-22) did the inverse, making a flat stock
# read as a +63% uptrend. Neither is visible as an error anywhere.
#
# The detector is free: re-fetch a few days of OVERLAP and compare bars we already hold. If the
# provider now prices them differently it has re-adjusted, so the cached prefix is stale and the
# symbol is refetched in full. This also heals the slow drift from dividend re-bases.
OVERLAP_DAYS = 7        # so the compared bar is settled, not the provisional same-day one
REBASE_TOL = 0.005      # 0.5%: catches splits and dividend re-bases, ignores rounding noise


def rebase_factor(cached: pd.DataFrame, fresh: pd.DataFrame | None) -> float | None:
    """How far the provider has re-adjusted history, measured on the overlapping bars.

    The median ``fresh/cached`` close ratio over the dates both hold, or None when there is
    nothing comparable. The **most recent cached bar is excluded**: fetched intraday it is
    often provisional and legitimately changes, which would otherwise look like a re-adjustment
    every single day. A median (not a mean) so one bad bar can't trigger a refetch. Pure."""
    if cached is None or fresh is None or cached.empty or fresh.empty:
        return None
    settled = cached.index[:-1]                       # drop the possibly-provisional last bar
    common = settled.intersection(fresh.index)
    if len(common) == 0:
        return None
    before, after = cached.loc[common, "close"], fresh.loc[common, "close"]
    ok = (before > 0) & after.notna() & (after > 0)
    if not ok.any():
        return None
    return float((after[ok] / before[ok]).median())


def _is_rebased(factor: float | None) -> bool:
    return factor is not None and abs(factor - 1.0) > REBASE_TOL


# The overlap check above only catches an action as it happens. A break that landed *before*
# this logic existed sits in the cached history and would never heal, so the already-corrupted
# symbols also need finding — by their fingerprint: a one-day move that lands on a clean
# corporate ratio. Scanned only over the recent window, because (a) that is where a break still
# distorts a live 200-day indicator and (b) old history holds genuine ~50% collapses (SIVB,
# FRC in 2023) that must not be chased forever.
# Whole-number ratios only. The gentle ones (3:2, 2:3, 5:4) are where real moves live: against
# the production cache they matched ABMD's acquisition pop (x1.499), First Republic's collapse
# (x0.669) and an SMCI crash (x0.668) — three false positives for no gain, since splits at those
# ratios are vanishingly rare. Restricted to integer ratios it finds exactly the two real breaks.
_SPLIT_RATIOS = (1 / 2, 1 / 3, 1 / 4, 1 / 5, 1 / 6, 1 / 8, 1 / 10, 1 / 15, 1 / 20,
                 2.0, 3.0, 4.0, 5.0, 6.0, 8.0, 10.0, 15.0, 20.0)
BREAK_WINDOW = 300      # bars back to look; ~1.5× the longest indicator window in use
BREAK_TOL = 0.02        # within 2% of a clean ratio (DD's 3.0277 is 0.9% off 3:1)


def stale_split(df: pd.DataFrame, window: int = BREAK_WINDOW) -> float | None:
    """A split-shaped discontinuity already sitting in the cached history, or None.

    A refetch on a false positive is harmless — it costs one call and returns the provider's
    authoritative series — so this errs toward catching. The honest limit: a *genuine* crash
    that happens to land within 2% of a clean ratio will be refetched each run (the data comes
    back the same), which the heal log makes visible rather than silent. Pure."""
    if df is None or df.empty or len(df) < 3:
        return None
    closes = df["close"].tail(window)
    ratios = (closes / closes.shift(1)).dropna()
    for r in ratios[(ratios < 0.55) | (ratios > 1.8)]:
        for target in _SPLIT_RATIOS:
            if abs(float(r) / target - 1.0) < BREAK_TOL:
                return float(r)
    return None


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

    @classmethod
    def _tail_start(cls, df: pd.DataFrame) -> date | None:
        """Where to start the incremental fetch: ``OVERLAP_DAYS`` *before* the last cached bar,
        so the response re-prices bars we already hold and :func:`rebase_factor` can tell whether
        the provider has re-adjusted them. None for a cold series (fetch everything)."""
        last = cls._last(df)
        return None if last is None else last - timedelta(days=OVERLAP_DAYS)

    def _heal_if_rebased(self, symbol: str, cached: pd.DataFrame, fresh: pd.DataFrame | None,
                         end: date | None) -> tuple[pd.DataFrame, pd.DataFrame | None, bool]:
        """If the provider has re-adjusted this symbol — or the cache still carries a break
        from an action that predates this check — drop the stale prefix and refetch it whole.
        Returns ``(cached, fresh, healed)`` ready for :meth:`_merge`."""
        # The history scan runs only on a symbol the provider is still serving bars for. A
        # delisted name can't have a new corporate action and has no live indicator to distort,
        # so scanning it only re-finds real history — First Republic's 2023 collapse lands
        # within 1.6% of exactly 1:2 — and would refetch it every sweep, for nothing.
        alive = fresh is not None and not fresh.empty
        if not _is_rebased(rebase_factor(cached, fresh)) and not (alive and stale_split(cached)):
            return cached, fresh, False
        full = self.provider.get_history(symbol, None, end, self.interval)
        if full is None or full.empty:               # refetch failed -> keep what we have
            return cached, fresh, False
        return standardize_ohlcv(None), full, True

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

        When ``start`` is given and the cached series is **incomplete** over ``[start, last]``
        — a truncated first fetch *or* an interior hole — the whole span is refetched and
        merged (the fresh copy wins on overlap, so it fills the missing prefix **and** any
        internal gaps). Incremental updates otherwise only ever extend the *tail* forward, so
        a short/holey series never heals. Used for the benchmark, whose full, gap-free history
        the walk-forward calendar depends on (cheap — a single symbol). Assumes the provider
        can serve back to ``start`` (true for Alpaca's SPY); if it genuinely can't, the span is
        refetched each run (acceptable for one symbol — see the cache-hardening TODO)."""
        cached = self.load(symbol)
        if start is not None and not cached.empty:
            hi = cached.index.max()
            expected = len(pd.bdate_range(pd.Timestamp(start), hi))
            if expected and len(cached) < 0.9 * expected:     # truncated or internally gappy
                span = self.provider.get_history(symbol, start, hi.date(), self.interval)
                if span is not None and not span.empty:
                    cached = self._merge(cached, span)         # fresh fills prefix + interior holes
        fresh = self.provider.get_history(symbol, self._tail_start(cached), end, self.interval)
        cached, fresh, _healed = self._heal_if_rebased(symbol, cached, fresh, end)
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
        healed: list[str] = []
        for last, group in buckets.items():
            start = None if last is None else last - timedelta(days=OVERLAP_DAYS)
            fresh = self.provider.get_history_batch(group, start, end, self.interval)
            for sym in group:
                # The bulk tail now overlaps what we hold, so a re-adjusted symbol is visible
                # here; it is refetched individually (rare — only on a corporate action).
                cached[sym], tail, did = self._heal_if_rebased(sym, cached[sym], fresh.get(sym), end)
                if did:
                    healed.append(sym)
                merged = self._merge(cached[sym], tail)
                if merged.empty:
                    continue
                self._save(sym, merged)
                count += 1
                first_dates.append(merged.index.min())
                last_dates.append(merged.index.max())
        if healed:      # visible in the collection log: a silent re-adjustment is the bug
            print(f"[cache] re-adjusted upstream, refetched in full: {', '.join(sorted(healed))}")
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

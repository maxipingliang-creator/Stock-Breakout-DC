"""`MarketData` — the single read facade that engines depend on.

It hides whether prices come from the local cache or directly from a provider,
so the scanner, backtester, tracker, and paper broker all share one no-fuss
interface: ``md.history(symbol, start, end)`` and ``md.batch(...)``.
"""
from __future__ import annotations

from datetime import date

import pandas as pd

from .base import DataProvider, get_provider
from .cache import DataCache


class MarketData:
    def __init__(
        self,
        provider: DataProvider | None = None,
        cache: DataCache | None = None,
        update: bool = False,
    ):
        self.provider = provider or get_provider("synthetic")
        self.cache = cache
        self.update = update

    @classmethod
    def from_config(cls, cfg, provider_name: str | None = None, update: bool = False) -> MarketData:
        provider = get_provider(provider_name or cfg.default_provider)
        cache = DataCache(provider, cfg.cache_dir, interval=cfg.get("data.interval", "1d"))
        return cls(provider=provider, cache=cache, update=update)

    def history(
        self,
        symbol: str,
        start: date | None = None,
        end: date | None = None,
    ) -> pd.DataFrame:
        if self.cache is not None:
            df = self.cache.get(symbol, start, end, update=self.update)
            if not df.empty or self.update:
                return df
            # Cache miss and no update requested: fall back to the live provider.
        return self.provider.get_history(symbol, start, end)

    def batch(
        self,
        symbols: list[str],
        start: date | None = None,
        end: date | None = None,
    ) -> dict[str, pd.DataFrame]:
        return {s: self.history(s, start, end) for s in symbols}

    def fundamentals(self, symbol: str) -> pd.DataFrame:
        """Point-in-time quarterly fundamentals (empty if the provider has none)."""
        return self.provider.get_fundamentals(symbol)

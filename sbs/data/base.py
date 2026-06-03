"""Provider abstraction layer.

A :class:`DataProvider` returns canonical OHLCV frames. Concrete providers
register themselves with :func:`register_provider`; callers obtain one via
:func:`get_provider`, never by importing a concrete class. Swapping providers
therefore never touches strategy or engine code.
"""
from __future__ import annotations

import importlib
import pkgutil
from abc import ABC, abstractmethod
from datetime import date

import pandas as pd

from .models import SecurityMeta

OHLCV_COLUMNS = ["open", "high", "low", "close", "volume"]

_REGISTRY: dict[str, type[DataProvider]] = {}


def register_provider(cls: type[DataProvider]) -> type[DataProvider]:
    """Class decorator: register a provider under its ``name``."""
    if not getattr(cls, "name", None):
        raise ValueError(f"{cls.__name__} must define a non-empty `name`")
    _REGISTRY[cls.name] = cls
    return cls


def _ensure_providers_imported() -> None:
    """Import every module in ``sbs.data.providers`` so they self-register."""
    from . import providers  # local import avoids a cycle at module load

    for mod in pkgutil.iter_modules(providers.__path__):
        importlib.import_module(f"{providers.__name__}.{mod.name}")


def get_provider(name: str | None = None, **kwargs) -> DataProvider:
    """Instantiate a provider by name (defaults to the synthetic provider)."""
    _ensure_providers_imported()
    key = name or "synthetic"
    if key not in _REGISTRY:
        raise KeyError(
            f"Unknown data provider {key!r}. Available: {sorted(_REGISTRY)}"
        )
    return _REGISTRY[key](**kwargs)


def available_providers() -> list[str]:
    _ensure_providers_imported()
    return sorted(_REGISTRY)


def standardize_ohlcv(df: pd.DataFrame) -> pd.DataFrame:
    """Coerce an arbitrary OHLCV frame into the canonical shape."""
    if df is None or len(df) == 0:
        return pd.DataFrame(columns=OHLCV_COLUMNS, index=pd.DatetimeIndex([], name="date"))

    out = df.copy()
    out.columns = [str(c).lower().replace(" ", "_") for c in out.columns]
    rename = {"adj close": "adj_close", "adjclose": "adj_close", "vol": "volume"}
    out = out.rename(columns=rename)

    if not isinstance(out.index, pd.DatetimeIndex):
        out.index = pd.to_datetime(out.index)
    out.index = out.index.tz_localize(None) if out.index.tz is not None else out.index
    out.index.name = "date"

    for col in OHLCV_COLUMNS:
        if col not in out.columns:
            out[col] = pd.NA
        out[col] = pd.to_numeric(out[col], errors="coerce")

    keep = OHLCV_COLUMNS + (["adj_close"] if "adj_close" in out.columns else [])
    out = out[keep]
    out = out[~out.index.duplicated(keep="last")].sort_index()
    return out.dropna(subset=["close"])


class DataProvider(ABC):
    """Abstract base for all market-data providers."""

    name: str = ""
    version: str = "0"

    @abstractmethod
    def get_history(
        self,
        symbol: str,
        start: date | None = None,
        end: date | None = None,
        interval: str = "1d",
    ) -> pd.DataFrame:
        """Return canonical OHLCV for ``symbol`` in ``[start, end]`` inclusive."""

    def get_history_batch(
        self,
        symbols: list[str],
        start: date | None = None,
        end: date | None = None,
        interval: str = "1d",
    ) -> dict[str, pd.DataFrame]:
        """Fetch many symbols. Override for providers with bulk endpoints."""
        out: dict[str, pd.DataFrame] = {}
        for sym in symbols:
            try:
                out[sym] = self.get_history(sym, start, end, interval)
            except Exception:  # noqa: BLE001 - one bad symbol shouldn't abort the batch
                out[sym] = standardize_ohlcv(None)
        return out

    def list_securities(self) -> list[SecurityMeta]:
        """Reference data for universe construction. May be empty."""
        return []

    def get_fundamentals(self, symbol: str) -> pd.DataFrame:
        """Point-in-time quarterly fundamentals for ``symbol``.

        Indexed by ``report_date`` (the date the figure became public, so
        backtests can read it without lookahead), with columns ``eps_ttm,
        revenue_ttm, eps_growth_yoy, revenue_growth_yoy, shares_outstanding``.
        Default empty (providers without fundamentals)."""
        return pd.DataFrame(
            columns=["eps_ttm", "revenue_ttm", "eps_growth_yoy",
                     "revenue_growth_yoy", "shares_outstanding"],
            index=pd.DatetimeIndex([], name="report_date"),
        )

    def is_available(self) -> bool:
        """Whether this provider can currently serve data (network/creds)."""
        return True

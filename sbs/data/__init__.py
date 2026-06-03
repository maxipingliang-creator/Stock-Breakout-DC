"""Data layer: provider-agnostic OHLCV access, caching, and versioning.

Strategies and engines depend on :func:`get_provider` and :class:`DataCache`,
never on a concrete provider. This keeps strategies insulated from vendor APIs.
"""
from __future__ import annotations

from .access import MarketData
from .base import OHLCV_COLUMNS, DataProvider, available_providers, get_provider, register_provider
from .cache import DataCache
from .models import DataVersion, SecurityMeta

__all__ = [
    "DataProvider",
    "OHLCV_COLUMNS",
    "available_providers",
    "get_provider",
    "register_provider",
    "DataCache",
    "MarketData",
    "DataVersion",
    "SecurityMeta",
]

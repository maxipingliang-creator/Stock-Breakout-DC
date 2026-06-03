"""Data structures shared across the data layer.

OHLCV price history is represented as a :class:`pandas.DataFrame` indexed by a
tz-naive ``DatetimeIndex`` with lowercase columns ``open, high, low, close,
volume`` (and optionally ``adj_close``). Keeping a single canonical shape lets
every provider and engine interoperate without translation glue.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import date, datetime


@dataclass(frozen=True)
class SecurityMeta:
    """Reference data for one security, including survivorship fields."""

    symbol: str
    name: str = ""
    exchange: str = ""                  # NYSE | NASDAQ | AMEX | ...
    security_type: str = "COMMON"       # COMMON | ETF | PREFERRED | WARRANT | ...
    sector: str = "Unknown"
    market_cap: float = 0.0
    listing_date: date | None = None
    delisting_date: date | None = None   # None => still listed
    status: str = "active"              # active | delisted | acquired | bankrupt | merged

    def is_tradable_on(self, as_of: date) -> bool:
        """Survivorship-safe check: listed on/before ``as_of`` and not yet delisted."""
        if self.listing_date and as_of < self.listing_date:
            return False
        if self.delisting_date and as_of >= self.delisting_date:
            return False
        return True

    def to_row(self) -> dict:
        row = asdict(self)
        for k in ("listing_date", "delisting_date"):
            v = row[k]
            row[k] = v.isoformat() if isinstance(v, (date, datetime)) else v
        return row


@dataclass
class DataVersion:
    """Metadata stamped onto a cached dataset for reproducibility."""

    provider: str
    provider_version: str
    download_date: str                  # ISO timestamp
    interval: str = "1d"
    symbol_count: int = 0
    universe_version: str = "0"
    start: str | None = None
    end: str | None = None
    notes: str = ""
    version_id: int | None = None     # assigned by the DB on insert

    def to_row(self) -> dict:
        return {k: v for k, v in asdict(self).items() if k != "version_id"}


# Sector list used by the synthetic provider / sector-exposure limits.
SECTORS = [
    "Technology",
    "Healthcare",
    "Financials",
    "Consumer Discretionary",
    "Consumer Staples",
    "Energy",
    "Industrials",
    "Materials",
    "Utilities",
    "Communication Services",
    "Real Estate",
]

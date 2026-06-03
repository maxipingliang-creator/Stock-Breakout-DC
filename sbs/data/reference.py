"""Reference-data loaders for providers that have no universe listing.

Yahoo (and most price APIs) can't enumerate a tradable universe, so real-data
runs supply one as a CSV. For *survivorship-bias-free* research the CSV must
include delisted / acquired / bankrupt names with their delisting dates — see
``config/universe.csv`` for the format and worked examples.

A free survivorship-aware universe (all active **and** delisted US tickers with
IPO/delisting dates) can be generated from Alpha Vantage's ``LISTING_STATUS``
endpoint — see :func:`parse_av_listing_status` and ``scripts/fetch_universe.py``.
"""
from __future__ import annotations

import csv
import io
from datetime import date, datetime
from pathlib import Path

from .models import SecurityMeta

# Alpha Vantage exchange labels -> our canonical set.
_AV_EXCHANGE = {
    "NYSE": "NYSE",
    "NASDAQ": "NASDAQ",
    "NYSE MKT": "AMEX",
    "NYSE AMERICAN": "AMEX",
    "AMEX": "AMEX",
    "NYSE ARCA": "NYSE ARCA",
    "BATS": "BATS",
}


def _parse_date(value: str | None) -> date | None:
    value = (value or "").strip()
    if not value or value.lower() in {"null", "none", "-", "nan"}:  # AV uses "null"
        return None
    try:
        return datetime.fromisoformat(value).date()
    except ValueError:
        return None


def load_securities_csv(path: str | Path) -> list[SecurityMeta]:
    """Parse a universe CSV into :class:`SecurityMeta` rows (empty if absent).

    Columns (header required): ``symbol`` plus any of ``name, exchange,
    security_type, sector, market_cap, listing_date, delisting_date, status``.
    """
    p = Path(path)
    if not p.exists():
        return []
    out: list[SecurityMeta] = []
    with p.open(newline="", encoding="utf-8") as fh:
        for row in csv.DictReader(fh):
            symbol = (row.get("symbol") or "").strip().upper()
            if not symbol or symbol.startswith("#"):
                continue
            cap = (row.get("market_cap") or "").strip()
            out.append(SecurityMeta(
                symbol=symbol,
                name=(row.get("name") or "").strip(),
                exchange=(row.get("exchange") or "").strip().upper(),
                security_type=(row.get("security_type") or "COMMON").strip().upper(),
                sector=(row.get("sector") or "Unknown").strip(),
                market_cap=float(cap) if cap else 0.0,
                listing_date=_parse_date(row.get("listing_date")),
                delisting_date=_parse_date(row.get("delisting_date")),
                status=(row.get("status") or "active").strip(),
            ))
    return out


def parse_av_listing_status(csv_text: str, *, common_only: bool = True) -> list[SecurityMeta]:
    """Parse Alpha Vantage ``LISTING_STATUS`` CSV into :class:`SecurityMeta`.

    Columns: ``symbol, name, exchange, assetType, ipoDate, delistingDate, status``.
    This is the free survivorship calendar: it lists active **and** delisted US
    tickers with IPO/delisting dates (but no market cap — that stays 0/unknown,
    so universe filtering falls back to the liquidity rule). ``common_only`` keeps
    only ``assetType == Stock`` (drops ETFs).
    """
    out: list[SecurityMeta] = []
    for row in csv.DictReader(io.StringIO(csv_text)):
        symbol = (row.get("symbol") or "").strip().upper()
        if not symbol:
            continue
        asset = (row.get("assetType") or "Stock").strip()
        if common_only and asset.lower() != "stock":
            continue
        exch = (row.get("exchange") or "").strip().upper()
        status = (row.get("status") or "Active").strip().lower()
        out.append(SecurityMeta(
            symbol=symbol,
            name=(row.get("name") or "").strip(),
            exchange=_AV_EXCHANGE.get(exch, exch),
            security_type="COMMON" if asset.lower() == "stock" else asset.upper(),
            market_cap=0.0,  # AV LISTING_STATUS has no market cap (unknown)
            listing_date=_parse_date(row.get("ipoDate")),
            delisting_date=_parse_date(row.get("delistingDate")),
            status="active" if status == "active" else "delisted",
        ))
    return out


def write_securities_csv(securities: list[SecurityMeta], path: str | Path) -> int:
    """Write :class:`SecurityMeta` rows to a universe CSV; returns the row count."""
    cols = ["symbol", "name", "exchange", "security_type", "sector", "market_cap",
            "listing_date", "delisting_date", "status"]
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    with p.open("w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=cols)
        w.writeheader()
        for s in securities:
            row = s.to_row()
            w.writerow({k: ("" if row.get(k) is None else row.get(k)) for k in cols})
    return len(securities)

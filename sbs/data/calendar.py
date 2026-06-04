"""Trading-day calendar for US equities.

Backed by the data provider when it exposes ``get_calendar`` (Alpaca's
``/v2/calendar``), with a Monday–Friday fallback when no provider calendar is
available (synthetic, or missing Alpaca creds). The fallback ignores holidays —
so a market holiday can read as a trading day — therefore callers that *alert* on
this (the data-staleness check) allow a small tolerance.

Cached per process: one fetch covers a ~2-month window, so a run makes at most one
``/v2/calendar`` request per provider (CLI runs are short-lived, so this just
dedupes calls within a single run rather than persisting across runs).
"""
from __future__ import annotations

from datetime import date, timedelta

# provider key -> (window_lo, window_hi, sessions)
_CACHE: dict[str, tuple[date, date, set[date]]] = {}


def _reset_cache() -> None:
    """Clear the module cache (tests)."""
    _CACHE.clear()


def _fetch(provider, start: date, end: date) -> set[date] | None:
    fn = getattr(provider, "get_calendar", None)
    if fn is None:
        return None
    try:
        days = fn(start, end)
    except Exception:  # noqa: BLE001 - a calendar lookup must never break the pipeline
        return None
    return set(days) if days else None


def _sessions(provider, d: date) -> set[date] | None:
    """Cached set of trading sessions covering a wide window around ``d``."""
    key = getattr(provider, "name", None) or repr(type(provider))
    hit = _CACHE.get(key)
    if hit and hit[0] <= d <= hit[1]:
        return hit[2]
    lo, hi = d - timedelta(days=45), d + timedelta(days=10)
    sessions = _fetch(provider, lo, hi)
    if sessions is not None:
        _CACHE[key] = (lo, hi, sessions)
    return sessions


def is_trading_day(provider, d: date) -> bool:
    sessions = _sessions(provider, d)
    if sessions is not None:
        return d in sessions
    return d.weekday() < 5                      # fallback: Mon-Fri


def last_trading_day(provider, on_or_before: date) -> date:
    """Most recent trading session on or before ``on_or_before``."""
    sessions = _sessions(provider, on_or_before)
    if sessions:
        prior = [x for x in sessions if x <= on_or_before]
        if prior:
            return max(prior)
    d = on_or_before                            # fallback: walk back to a weekday
    while d.weekday() >= 5:
        d -= timedelta(days=1)
    return d

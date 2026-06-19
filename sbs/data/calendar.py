"""Trading-day calendar for US equities.

Backed by the data provider when it exposes ``get_calendar`` (Alpaca's
``/v2/calendar``), with a Monday–Friday fallback when no provider calendar is
available (synthetic, or the live calendar fetch failing). The fallback also
excludes a hardcoded set of US market full-closure holidays
(:data:`_US_MARKET_HOLIDAYS`) — so a holiday like Juneteenth doesn't read as a
trading day when the live calendar is unavailable. The live calendar stays
primary; this only hardens the fallback (it was the gap that let the daily scan
fire on Juneteenth 2026).

Cached per process: one fetch covers a ~2-month window, so a run makes at most one
``/v2/calendar`` request per provider (CLI runs are short-lived, so this just
dedupes calls within a single run rather than persisting across runs).
"""
from __future__ import annotations

from datetime import date, timedelta

# US equity market FULL-closure holidays (NYSE/Nasdaq), observed dates — a backstop for the
# Mon–Fri fallback when the provider calendar is unavailable. Half-days (e.g. the day after
# Thanksgiving) are NOT here: they are trading days. Extend yearly; the live Alpaca calendar
# is the primary source, so this only needs to cover gaps when that fetch fails.
_US_MARKET_HOLIDAYS: frozenset[date] = frozenset({
    # 2025
    date(2025, 1, 1), date(2025, 1, 20), date(2025, 2, 17), date(2025, 4, 18),
    date(2025, 5, 26), date(2025, 6, 19), date(2025, 7, 4), date(2025, 9, 1),
    date(2025, 11, 27), date(2025, 12, 25),
    # 2026
    date(2026, 1, 1), date(2026, 1, 19), date(2026, 2, 16), date(2026, 4, 3),
    date(2026, 5, 25), date(2026, 6, 19), date(2026, 7, 3), date(2026, 9, 7),
    date(2026, 11, 26), date(2026, 12, 25),
    # 2027
    date(2027, 1, 1), date(2027, 1, 18), date(2027, 2, 15), date(2027, 3, 26),
    date(2027, 5, 31), date(2027, 6, 18), date(2027, 7, 5), date(2027, 9, 6),
    date(2027, 11, 25), date(2027, 12, 24),
})

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
    return d.weekday() < 5 and d not in _US_MARKET_HOLIDAYS   # fallback: Mon-Fri, minus holidays


def last_trading_day(provider, on_or_before: date) -> date:
    """Most recent trading session on or before ``on_or_before``."""
    sessions = _sessions(provider, on_or_before)
    if sessions:
        prior = [x for x in sessions if x <= on_or_before]
        if prior:
            return max(prior)
    d = on_or_before                            # fallback: walk back past weekends + holidays
    while d.weekday() >= 5 or d in _US_MARKET_HOLIDAYS:
        d -= timedelta(days=1)
    return d

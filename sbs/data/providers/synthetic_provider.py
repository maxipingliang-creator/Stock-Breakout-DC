"""Deterministic synthetic data provider.

Generates reproducible OHLCV so the entire platform runs offline (tests, CI,
demos) and still produces meaningful breakout signals. Crucially, the catalog
includes **delisted / acquired** securities with real listing/delisting dates,
so survivorship-bias handling is genuinely exercised rather than mocked.

Determinism: each symbol's price path is seeded from a stable hash of its
ticker, so identical inputs always yield identical data (reproducibility).
"""
from __future__ import annotations

import hashlib
from datetime import date
from functools import lru_cache

import numpy as np
import pandas as pd

from ..base import DataProvider, register_provider, standardize_ohlcv
from ..models import SECTORS, SecurityMeta

GLOBAL_START = pd.Timestamp("2014-01-01")
GLOBAL_END = pd.Timestamp("2026-06-30")

_EXCHANGES = ["NYSE", "NASDAQ", "NASDAQ", "AMEX"]  # weighted toward NASDAQ/NYSE
# A few non-common structures so the universe builder's exclusions do real work.
_TYPE_OVERRIDES = {3: "ETF", 17: "PREFERRED", 29: "WARRANT", 41: "ETF", 53: "CLOSED_END_FUND"}
# Delisted/acquired names (index -> (delist_date, status)) for survivorship tests.
_DELISTINGS = {
    7: ("2019-08-15", "acquired"),
    13: ("2021-03-10", "bankrupt"),
    22: ("2022-06-30", "merged"),
    34: ("2020-11-20", "delisted"),
    48: ("2023-02-01", "acquired"),
}
# Late listings (index -> listing_date) to test as-of eligibility.
_LATE_LISTINGS = {11: "2018-05-01", 26: "2019-09-16", 39: "2020-07-01", 55: "2021-01-04"}

N_SYMBOLS = 60
BENCHMARK_ALIASES = {"SPY", "BENCH", "^GSPC", "BENCHMARK"}


def _seed(symbol: str) -> int:
    return int(hashlib.sha256(symbol.encode()).hexdigest()[:8], 16)


@lru_cache(maxsize=1)
def _generate_benchmark() -> pd.DataFrame:
    """A broad-market index with deliberate bull / bear / sideways cycles so the
    regime detector and regime-conditioned analytics have something to find."""
    days = pd.bdate_range(GLOBAL_START, GLOBAL_END)
    n = len(days)
    t = np.arange(n)
    rng = np.random.default_rng(999)

    cycle = np.sin(2 * np.pi * t / 630.0)            # ~2.5y bull/bear cycle
    drift = 0.0003 + 0.0010 * cycle
    sigma = 0.008 + 0.004 * (0.5 + 0.5 * np.sin(2 * np.pi * t / 315.0))

    # Two sharp bear shocks (drawdown + volatility spike).
    for start_frac in (0.30, 0.68):
        k = int(start_frac * n)
        drift[k : k + 70] -= 0.0030
        sigma[k : k + 70] *= 2.6

    ret = drift + sigma * rng.standard_normal(n)
    close = 100.0 * np.exp(np.cumsum(ret))
    vol = (5e7 * rng.lognormal(0, 0.2, n) * (1 + 4 * np.abs(ret) / sigma)).round()
    df = pd.DataFrame(
        {
            "open": close * (1 - 0.5 * ret),
            "high": close * (1 + np.abs(ret)),
            "low": close * (1 - np.abs(ret)),
            "close": close,
            "volume": vol.astype(np.int64),
        },
        index=days,
    )
    df.index.name = "date"
    return standardize_ohlcv(df)


def _ticker(i: int) -> str:
    """Deterministic, clearly-synthetic 4-letter ticker for index ``i``."""
    rng = np.random.default_rng(1000 + i)
    return "".join(chr(ord("A") + int(c)) for c in rng.integers(0, 26, size=4))


@lru_cache(maxsize=1)
def _catalog() -> list[SecurityMeta]:
    metas: list[SecurityMeta] = []
    rng = np.random.default_rng(12345)
    for i in range(N_SYMBOLS):
        sym = _ticker(i)
        sec_type = _TYPE_OVERRIDES.get(i, "COMMON")
        # Most names are large/mid cap; a few small caps get filtered out.
        if i % 17 == 0:
            mcap = float(rng.uniform(3e8, 2.5e9))   # below $3B threshold
        else:
            mcap = float(rng.uniform(3e9, 6e11))
        listing = pd.Timestamp(_LATE_LISTINGS.get(i, "2013-06-01")).date()
        delist_status = _DELISTINGS.get(i)
        delisting = pd.Timestamp(delist_status[0]).date() if delist_status else None
        status = delist_status[1] if delist_status else "active"
        metas.append(
            SecurityMeta(
                symbol=sym,
                name=f"Synthetic Co {i:03d}",
                exchange=_EXCHANGES[i % len(_EXCHANGES)],
                security_type=sec_type,
                sector=SECTORS[i % len(SECTORS)],
                market_cap=mcap,
                listing_date=listing,
                delisting_date=delisting,
                status=status,
            )
        )
    return metas


@lru_cache(maxsize=1)
def _catalog_by_symbol() -> dict[str, SecurityMeta]:
    return {m.symbol: m for m in _catalog()}


@lru_cache(maxsize=256)
def _generate_full_history(symbol: str) -> pd.DataFrame:
    """Generate the full OHLCV path for a symbol within its listed lifetime."""
    meta = _catalog_by_symbol().get(symbol)
    rng = np.random.default_rng(_seed(symbol))

    start = max(GLOBAL_START, pd.Timestamp(meta.listing_date)) if meta else GLOBAL_START
    end = GLOBAL_END
    if meta and meta.delisting_date is not None:
        end = min(end, pd.Timestamp(meta.delisting_date))
    days = pd.bdate_range(start=start, end=end)
    n = len(days)
    if n < 2:
        return standardize_ohlcv(None)

    base_price = float(rng.uniform(15, 250))
    mu = rng.uniform(0.0, 0.00035)           # daily drift (~0-9%/yr)
    sigma = rng.uniform(0.011, 0.026)        # daily vol

    shocks = rng.normal(mu, sigma, n)
    # Inject momentum "bursts" so genuine breakouts (new highs + volume) appear.
    burst = np.zeros(n)
    for _ in range(int(rng.integers(3, 8))):
        i0 = int(rng.integers(0, n))
        length = int(rng.integers(5, 18))
        burst[i0 : i0 + length] += rng.uniform(0.0012, 0.005)
    logret = shocks + burst
    close = base_price * np.exp(np.cumsum(logret))

    open_ = np.empty(n)
    open_[0] = base_price
    open_[1:] = close[:-1] * (1.0 + rng.normal(0, sigma * 0.3, n - 1))
    amp = np.abs(rng.normal(0, sigma, n)) * close + 1e-6
    hi = np.maximum(open_, close) + np.abs(rng.normal(0, 1, n)) * amp * 0.5
    lo = np.minimum(open_, close) - np.abs(rng.normal(0, 1, n)) * amp * 0.5
    lo = np.clip(lo, 0.01, None)

    base_vol = float(rng.uniform(6e5, 5e6))
    vol = base_vol * rng.lognormal(0, 0.35, n) * (1.0 + 3.0 * np.abs(logret) / sigma)

    df = pd.DataFrame(
        {
            "open": open_,
            "high": hi,
            "low": lo,
            "close": close,
            "volume": np.round(vol).astype(np.int64),
        },
        index=days,
    )
    df.index.name = "date"
    return standardize_ohlcv(df)


@lru_cache(maxsize=256)
def _generate_fundamentals(symbol: str) -> pd.DataFrame:
    """Deterministic quarterly fundamentals with a ~45-day filing lag so they are
    point-in-time (a value is only "known" from its report_date onward)."""
    meta = _catalog_by_symbol().get(symbol)
    if meta is None:
        return pd.DataFrame()
    rng = np.random.default_rng(_seed(symbol) ^ 0xF00D)
    start = max(GLOBAL_START, pd.Timestamp(meta.listing_date))
    end = GLOBAL_END if meta.delisting_date is None else min(GLOBAL_END, pd.Timestamp(meta.delisting_date))
    q_ends = pd.date_range(start, end, freq="QE")
    n = len(q_ends)
    if n < 5:
        return pd.DataFrame()

    base_eps = float(rng.uniform(0.3, 4.0))
    base_rev = float(rng.uniform(2e8, 6e9))
    eps_q = (1 + rng.uniform(-0.05, 0.55)) ** 0.25       # quarterly EPS growth factor
    rev_q = (1 + rng.uniform(-0.02, 0.40)) ** 0.25
    eps_ttm = base_eps * np.cumprod(eps_q * (1 + rng.normal(0, 0.03, n)))
    rev_ttm = base_rev * np.cumprod(rev_q * (1 + rng.normal(0, 0.02, n)))
    # Float: ~half the names are small-float (< 500M shares).
    small = rng.random() < 0.5
    shares = float(rng.uniform(80e6, 480e6) if small else rng.uniform(500e6, 2.0e9))

    f = pd.DataFrame(
        {"eps_ttm": eps_ttm, "revenue_ttm": rev_ttm, "shares_outstanding": shares},
        index=q_ends + pd.Timedelta(days=45),     # filing lag => point-in-time
    )
    f.index.name = "report_date"
    f["eps_growth_yoy"] = f["eps_ttm"].pct_change(4)
    f["revenue_growth_yoy"] = f["revenue_ttm"].pct_change(4)
    return f[["eps_ttm", "revenue_ttm", "eps_growth_yoy", "revenue_growth_yoy", "shares_outstanding"]]


@register_provider
class SyntheticProvider(DataProvider):
    name = "synthetic"
    version = "1.0.0"

    def get_history(
        self,
        symbol: str,
        start: date | None = None,
        end: date | None = None,
        interval: str = "1d",
    ) -> pd.DataFrame:
        if symbol.upper() in BENCHMARK_ALIASES:
            df = _generate_benchmark()
        else:
            df = _generate_full_history(symbol)
        if start is not None:
            df = df[df.index >= pd.Timestamp(start)]
        if end is not None:
            df = df[df.index <= pd.Timestamp(end)]
        return df.copy()

    def list_securities(self) -> list[SecurityMeta]:
        return list(_catalog())

    def get_fundamentals(self, symbol: str) -> pd.DataFrame:
        return _generate_fundamentals(symbol).copy()

    def is_available(self) -> bool:
        return True

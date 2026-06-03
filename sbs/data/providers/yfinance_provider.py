"""yfinance provider (primary live source).

``yfinance`` is imported lazily so the platform installs and runs without it, and
so offline environments (where Yahoo is unreachable) still work via the synthetic
provider. Network calls here are therefore **not exercised by the offline test
suite** — the CSV-backed ``list_securities`` is, though.

Universe: Yahoo can't enumerate a tradable universe, so ``list_securities`` reads
a CSV (``config/universe.csv`` by default, or ``$SBS_UNIVERSE_CSV``). For
survivorship-bias-free research that CSV must include delisted names + dates.
"""
from __future__ import annotations

import os
from datetime import date

import pandas as pd

from ...config import PROJECT_ROOT
from ..base import DataProvider, register_provider, standardize_ohlcv
from ..models import SecurityMeta
from ..reference import load_securities_csv


def _pick(df: pd.DataFrame, names: list[str]) -> pd.Series | None:
    """Find a row by fuzzy (case/space-insensitive) name match."""
    norm = {str(i).lower().replace(" ", ""): i for i in df.index}
    for name in names:
        key = name.lower().replace(" ", "")
        if key in norm:
            return df.loc[norm[key]]
    return None


@register_provider
class YFinanceProvider(DataProvider):
    name = "yfinance"
    version = "0.2.x"

    def __init__(self, universe_csv: str | None = None, filing_lag_days: int = 45):
        self.universe_csv = (
            universe_csv or os.environ.get("SBS_UNIVERSE_CSV")
            or str(PROJECT_ROOT / "config" / "universe.csv")
        )
        self.filing_lag_days = filing_lag_days

    def _yf(self):
        try:
            import yfinance as yf  # noqa: PLC0415 - lazy optional dependency
        except ImportError as exc:  # pragma: no cover - exercised only without the dep
            raise RuntimeError(
                "yfinance is not installed. `pip install yfinance` or use "
                "`--provider synthetic` for offline runs."
            ) from exc
        return yf

    def get_history(
        self,
        symbol: str,
        start: date | None = None,
        end: date | None = None,
        interval: str = "1d",
    ) -> pd.DataFrame:
        yf = self._yf()
        # When start is None, yfinance defaults to period="1mo" (only ~1 month!).
        # Use a far-past start so we fetch the symbol's *full* available history.
        raw = yf.download(
            symbol,
            start=str(start) if start else "1990-01-01",
            end=str(end) if end else None,
            interval=interval,
            auto_adjust=False,
            progress=False,
            threads=False,
        )
        if isinstance(raw.columns, pd.MultiIndex):
            raw.columns = raw.columns.get_level_values(0)  # single-symbol can still be multi-indexed
        return standardize_ohlcv(raw)

    def list_securities(self) -> list[SecurityMeta]:
        return load_securities_csv(self.universe_csv)

    def get_fundamentals(self, symbol: str) -> pd.DataFrame:
        """Best-effort point-in-time fundamentals from yfinance quarterly data.

        NOTE: yfinance exposes the period-*end*, not the filing date, so we
        approximate the known-date with ``filing_lag_days``. Treat results as
        approximate; a production fundamentals vendor with real filing dates is
        the correct source. Returns empty on any error/missing data.
        """
        try:
            yf = self._yf()
            ticker = yf.Ticker(symbol)
            inc = ticker.quarterly_income_stmt
            if inc is None or getattr(inc, "empty", True):
                inc = ticker.quarterly_financials      # older yfinance name
            if inc is None or inc.empty:
                return super().get_fundamentals(symbol)
            # inc: rows = line items, columns = period-end dates. _pick returns the
            # matching row as a Series indexed by those dates.
            revenue = _pick(inc, ["Total Revenue", "Revenue", "Operating Revenue"])
            eps = _pick(inc, ["Diluted EPS", "Basic EPS"])
            if revenue is None or eps is None:
                return super().get_fundamentals(symbol)
            rev = pd.to_numeric(revenue, errors="coerce").sort_index()
            eps = pd.to_numeric(eps, errors="coerce").sort_index()
            out = pd.DataFrame(index=pd.to_datetime(rev.index))
            out["revenue_ttm"] = rev.rolling(4).sum().to_numpy()
            out["eps_ttm"] = eps.reindex(rev.index).rolling(4).sum().to_numpy()
            out["revenue_growth_yoy"] = out["revenue_ttm"].pct_change(4)
            out["eps_growth_yoy"] = out["eps_ttm"].pct_change(4)
            shares = None
            try:
                shares = ticker.info.get("sharesOutstanding") or ticker.info.get("floatShares")
            except Exception:  # noqa: BLE001 - .info is flaky/network
                shares = None
            out["shares_outstanding"] = float(shares) if shares else float("nan")
            out.index = out.index + pd.Timedelta(days=self.filing_lag_days)
            out.index.name = "report_date"
            cols = ["eps_ttm", "revenue_ttm", "eps_growth_yoy", "revenue_growth_yoy", "shares_outstanding"]
            return out[cols].dropna(how="all")
        except Exception:  # noqa: BLE001 - never let a flaky fundamentals call break a run
            return super().get_fundamentals(symbol)

    def is_available(self) -> bool:
        try:
            import yfinance  # noqa: F401, PLC0415
            return True
        except ImportError:
            return False

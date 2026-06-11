"""Typed data-access objects. Business logic uses these, never raw SQL."""
from __future__ import annotations

import json
from datetime import date, datetime, timezone
from typing import Any

import pandas as pd

from ..data.models import DataVersion, SecurityMeta
from ..serialize import json_safe
from .database import Database


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _as_date(value: Any) -> date | None:
    if not value:
        return None
    if isinstance(value, date):
        return value
    return datetime.fromisoformat(str(value)).date()


def _meta_from_row(row: dict) -> SecurityMeta:
    return SecurityMeta(
        symbol=row["symbol"],
        name=row.get("name") or "",
        exchange=row.get("exchange") or "",
        security_type=row.get("security_type") or "COMMON",
        sector=row.get("sector") or "Unknown",
        market_cap=float(row.get("market_cap") or 0.0),
        listing_date=_as_date(row.get("listing_date")),
        delisting_date=_as_date(row.get("delisting_date")),
        status=row.get("status") or "active",
    )


class SecurityRepository:
    def __init__(self, db: Database):
        self.db = db

    def upsert_many(self, metas: list[SecurityMeta]) -> None:
        sql = """
            INSERT INTO securities
                (symbol,name,exchange,security_type,sector,market_cap,
                 listing_date,delisting_date,status,updated_at)
            VALUES (:symbol,:name,:exchange,:security_type,:sector,:market_cap,
                    :listing_date,:delisting_date,:status,:updated_at)
            ON CONFLICT(symbol) DO UPDATE SET
                name=excluded.name, exchange=excluded.exchange,
                security_type=excluded.security_type, sector=excluded.sector,
                market_cap=excluded.market_cap, listing_date=excluded.listing_date,
                delisting_date=excluded.delisting_date, status=excluded.status,
                updated_at=excluded.updated_at
        """
        for m in metas:
            row = m.to_row()
            row["updated_at"] = _now()
            self.db.execute(sql, _named(sql, row))
        self.db.commit()

    def all(self) -> list[SecurityMeta]:
        return [_meta_from_row(r) for r in self.db.query("SELECT * FROM securities")]

    def get(self, symbol: str) -> SecurityMeta | None:
        row = self.db.query_one("SELECT * FROM securities WHERE symbol=?", (symbol,))
        return _meta_from_row(row) if row else None

    def tradable_on(self, as_of: date) -> list[SecurityMeta]:
        """Survivorship-safe: listed on/before ``as_of`` and not yet delisted."""
        return [m for m in self.all() if m.is_tradable_on(as_of)]


class DataVersionRepository:
    def __init__(self, db: Database):
        self.db = db

    def add(self, version: DataVersion) -> int:
        sql = """
            INSERT INTO data_versions
                (provider,provider_version,download_date,interval,symbol_count,
                 universe_version,start,end,notes)
            VALUES (?,?,?,?,?,?,?,?,?)
        """
        vid = self.db.insert(
            sql,
            (
                version.provider, version.provider_version, version.download_date,
                version.interval, version.symbol_count, version.universe_version,
                version.start, version.end, version.notes,
            ),
        )
        self.db.commit()
        version.version_id = vid
        return vid

    def latest(self) -> dict | None:
        return self.db.query_one(
            "SELECT * FROM data_versions ORDER BY version_id DESC LIMIT 1"
        )


class UniverseRepository:
    def __init__(self, db: Database):
        self.db = db

    def create_snapshot(
        self,
        universe_version: str,
        as_of: date,
        criteria: dict,
        members: list[dict],
    ) -> int:
        self.db.execute(
            "DELETE FROM universe_snapshots WHERE universe_version=? AND as_of_date=?",
            (universe_version, as_of.isoformat()),
        )
        sid = self.db.insert(
            """INSERT INTO universe_snapshots
                   (universe_version,as_of_date,criteria_json,member_count,created_at)
               VALUES (?,?,?,?,?)""",
            (universe_version, as_of.isoformat(), json.dumps(criteria), len(members), _now()),
        )
        self.db.executemany(
            """INSERT INTO universe_members
                   (snapshot_id,symbol,market_cap,avg_volume,avg_dollar_vol,sector)
               VALUES (?,?,?,?,?,?)""",
            [
                (sid, m["symbol"], m.get("market_cap"), m.get("avg_volume"),
                 m.get("avg_dollar_vol"), m.get("sector"))
                for m in members
            ],
        )
        self.db.commit()
        return sid

    def latest_snapshot(self, as_of: date | None = None) -> dict | None:
        if as_of:
            return self.db.query_one(
                "SELECT * FROM universe_snapshots WHERE as_of_date<=? "
                "ORDER BY as_of_date DESC LIMIT 1",
                (as_of.isoformat(),),
            )
        return self.db.query_one(
            "SELECT * FROM universe_snapshots ORDER BY as_of_date DESC LIMIT 1"
        )

    def members(self, snapshot_id: int) -> list[dict]:
        return self.db.query(
            "SELECT * FROM universe_members WHERE snapshot_id=?", (snapshot_id,)
        )

    def member_symbols(self, snapshot_id: int) -> list[str]:
        return [m["symbol"] for m in self.members(snapshot_id)]


class SignalRepository:
    def __init__(self, db: Database):
        self.db = db

    def add(self, sig: dict) -> int:
        sql = """
            INSERT INTO signals
                (strategy,strategy_version,symbol,signal_date,direction,entry_price,
                 stop_price,target_price,score,trigger_reason,indicator_values,
                 filter_values,score_factors,regime,config_version,universe_version,
                 data_version_id,created_at)
            VALUES (:strategy,:strategy_version,:symbol,:signal_date,:direction,:entry_price,
                    :stop_price,:target_price,:score,:trigger_reason,:indicator_values,
                    :filter_values,:score_factors,:regime,:config_version,:universe_version,
                    :data_version_id,:created_at)
            ON CONFLICT(strategy,symbol,signal_date) DO UPDATE SET
                score=excluded.score, entry_price=excluded.entry_price,
                stop_price=excluded.stop_price, target_price=excluded.target_price,
                trigger_reason=excluded.trigger_reason,
                indicator_values=excluded.indicator_values,
                filter_values=excluded.filter_values,
                score_factors=excluded.score_factors, regime=excluded.regime
        """
        row = dict(sig)
        row.setdefault("direction", "long")
        row.setdefault("created_at", _now())
        for jcol in ("indicator_values", "filter_values", "score_factors"):
            if isinstance(row.get(jcol), (dict, list)):
                row[jcol] = json.dumps(row[jcol])
        self.db.execute(sql, _named(sql, row))
        self.db.commit()
        got = self.db.query_one(
            "SELECT signal_id FROM signals WHERE strategy=? AND symbol=? AND signal_date=?",
            (row["strategy"], row["symbol"], row["signal_date"]),
        )
        return int(got["signal_id"]) if got else -1

    def by_date(self, signal_date: str, strategy: str | None = None) -> list[dict]:
        if strategy:
            return self.db.query(
                "SELECT * FROM signals WHERE signal_date=? AND strategy=? ORDER BY score DESC",
                (signal_date, strategy),
            )
        return self.db.query(
            "SELECT * FROM signals WHERE signal_date=? ORDER BY score DESC", (signal_date,)
        )

    def all(self) -> list[dict]:
        return self.db.query("SELECT * FROM signals ORDER BY signal_date")

    def set_performance(self, signal_id: int, horizon: int, eval_date: str,
                        price: float, return_pct: float, r_multiple: float) -> None:
        self.db.execute(
            """INSERT INTO signal_performance
                   (signal_id,horizon_days,eval_date,price,return_pct,r_multiple)
               VALUES (?,?,?,?,?,?)
               ON CONFLICT(signal_id,horizon_days) DO UPDATE SET
                   eval_date=excluded.eval_date, price=excluded.price,
                   return_pct=excluded.return_pct, r_multiple=excluded.r_multiple""",
            (signal_id, horizon, eval_date, price, return_pct, r_multiple),
        )
        self.db.commit()

    def set_lifecycle(self, signal_id: int, state: str, as_of_date: str,
                     price: float | None = None, note: str = "") -> None:
        self.db.execute(
            """INSERT INTO trade_lifecycle (signal_id,state,as_of_date,price,note)
               VALUES (?,?,?,?,?)
               ON CONFLICT(signal_id) DO UPDATE SET
                   state=excluded.state, as_of_date=excluded.as_of_date,
                   price=excluded.price, note=excluded.note""",
            (signal_id, state, as_of_date, price, note),
        )
        self.db.commit()

    def performance_rows(self) -> list[dict]:
        return self.db.query("SELECT * FROM signal_performance")

    def latest_date(self) -> str | None:
        """Most recent ``signal_date`` in the table (the latest scan)."""
        row = self.db.query_one("SELECT MAX(signal_date) d FROM signals")
        return row["d"] if row and row.get("d") else None

    def forward_record(self, strategy: str, horizon: int = 20) -> dict | None:
        """Realized forward record for a strategy's tracked signals: count, win%,
        and avg R-multiple at ``horizon`` days. Falls back to the most mature
        horizon that has data when ``horizon`` isn't realized yet (recent signals).
        Returns None when nothing has been tracked for the strategy."""
        sql = (
            "SELECT COUNT(*) n, "
            "AVG(CASE WHEN p.r_multiple > 0 THEN 1.0 ELSE 0.0 END) win, "
            "AVG(p.r_multiple) avg_r "
            "FROM signal_performance p JOIN signals s ON s.signal_id = p.signal_id "
            "WHERE s.strategy = ? AND p.r_multiple IS NOT NULL AND p.horizon_days = ?"
        )
        row = self.db.query_one(sql, (strategy, horizon))
        used = horizon
        if not row or not row.get("n"):
            mx = self.db.query_one(
                "SELECT MAX(p.horizon_days) h FROM signal_performance p "
                "JOIN signals s ON s.signal_id = p.signal_id "
                "WHERE s.strategy = ? AND p.r_multiple IS NOT NULL", (strategy,))
            if not mx or mx.get("h") is None:
                return None
            used = int(mx["h"])
            row = self.db.query_one(sql, (strategy, used))
        if not row or not row.get("n"):
            return None
        return {"n": int(row["n"]), "win_pct": round((row["win"] or 0.0) * 100, 0),
                "avg_r": round(row["avg_r"] or 0.0, 2), "horizon": used}


class BacktestRepository:
    def __init__(self, db: Database):
        self.db = db

    def add_run(self, meta: dict, trades: list[dict]) -> int:
        bid = self.db.insert(
            """INSERT INTO backtests
                   (strategy,strategy_version,start_date,end_date,walkforward,
                    config_json,metrics_json,config_version,universe_version,created_at)
               VALUES (?,?,?,?,?,?,?,?,?,?)""",
            (
                meta["strategy"], meta.get("strategy_version"), meta.get("start_date"),
                meta.get("end_date"), int(meta.get("walkforward", 0)),
                json.dumps(meta.get("config", {})), json.dumps(json_safe(meta.get("metrics", {}))),
                meta.get("config_version"), meta.get("universe_version"), _now(),
            ),
        )
        if trades:
            self.db.executemany(
                """INSERT INTO backtest_trades
                       (backtest_id,symbol,entry_date,entry_price,exit_date,exit_price,
                        shares,exit_reason,return_pct,r_multiple,pnl,bars_held,regime)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                [
                    (bid, t.get("symbol"), t.get("entry_date"), t.get("entry_price"),
                     t.get("exit_date"), t.get("exit_price"), t.get("shares"),
                     t.get("exit_reason"), t.get("return_pct"), t.get("r_multiple"),
                     t.get("pnl"), t.get("bars_held"), t.get("regime"))
                    for t in trades
                ],
            )
        pts = meta.get("equity_points")
        if pts:
            self.db.execute(
                "INSERT INTO backtest_equity (backtest_id, points_json) VALUES (?, ?)",
                (bid, json.dumps(json_safe(pts))),
            )
        self.db.commit()
        return bid

    def equity_points(self, backtest_id: int) -> list:
        row = self.db.query_one(
            "SELECT points_json FROM backtest_equity WHERE backtest_id=?", (backtest_id,)
        )
        return json.loads(row["points_json"]) if row and row.get("points_json") else []

    def get_run(self, backtest_id: int) -> dict | None:
        return self.db.query_one("SELECT * FROM backtests WHERE backtest_id=?", (backtest_id,))

    def list_runs(self, strategy: str | None = None) -> list[dict]:
        if strategy:
            return self.db.query(
                "SELECT * FROM backtests WHERE strategy=? ORDER BY created_at DESC", (strategy,)
            )
        return self.db.query("SELECT * FROM backtests ORDER BY created_at DESC")

    def latest_walkforward(self, strategy: str) -> dict | None:
        """Latest persisted walk-forward run for a strategy: out-of-sample
        expectancy (R), CAGR, max drawdown, trade count. None if never persisted."""
        row = self.db.query_one(
            "SELECT metrics_json FROM backtests WHERE strategy=? AND walkforward=1 "
            "ORDER BY created_at DESC LIMIT 1", (strategy,))
        if not row or not row.get("metrics_json"):
            return None
        m = json.loads(row["metrics_json"])
        return {"expectancy_r": m.get("expectancy_r"), "cagr": m.get("cagr"),
                "max_drawdown": m.get("max_drawdown"), "trade_count": m.get("trade_count")}


class RegimeRepository:
    def __init__(self, db: Database):
        self.db = db

    def upsert(self, as_of: str, benchmark: str, trend: str, vol: str, detail: dict) -> None:
        self.db.execute(
            """INSERT INTO market_regime (as_of_date,benchmark,trend_state,vol_state,detail_json)
               VALUES (?,?,?,?,?)
               ON CONFLICT(as_of_date) DO UPDATE SET
                   benchmark=excluded.benchmark, trend_state=excluded.trend_state,
                   vol_state=excluded.vol_state, detail_json=excluded.detail_json""",
            (as_of, benchmark, trend, vol, json.dumps(detail)),
        )
        self.db.commit()

    def get(self, as_of: str) -> dict | None:
        return self.db.query_one("SELECT * FROM market_regime WHERE as_of_date=?", (as_of,))


class FundamentalsRepository:
    """Point-in-time quarterly fundamentals (keyed by symbol + report date)."""

    _COLS = ["eps_ttm", "revenue_ttm", "eps_growth_yoy", "revenue_growth_yoy", "shares_outstanding"]

    def __init__(self, db: Database):
        self.db = db

    def upsert_symbol(self, symbol: str, df: pd.DataFrame) -> int:
        if df is None or df.empty:
            return 0
        rows = []
        for ts, r in df.iterrows():
            vals = [None if pd.isna(r[c]) else float(r[c]) for c in self._COLS]
            rows.append((symbol, pd.Timestamp(ts).date().isoformat(), *vals))
        self.db.executemany(
            "INSERT INTO fundamentals (symbol,report_date,eps_ttm,revenue_ttm,"
            "eps_growth_yoy,revenue_growth_yoy,shares_outstanding) VALUES (?,?,?,?,?,?,?) "
            "ON CONFLICT(symbol,report_date) DO UPDATE SET "
            "eps_ttm=excluded.eps_ttm, revenue_ttm=excluded.revenue_ttm, "
            "eps_growth_yoy=excluded.eps_growth_yoy, "
            "revenue_growth_yoy=excluded.revenue_growth_yoy, "
            "shares_outstanding=excluded.shares_outstanding",
            rows)
        self.db.commit()
        return len(rows)

    @classmethod
    def _empty(cls) -> pd.DataFrame:
        return pd.DataFrame(columns=cls._COLS, index=pd.DatetimeIndex([], name="report_date"))

    def get(self, symbol: str) -> pd.DataFrame:
        rows = self.db.query(
            "SELECT * FROM fundamentals WHERE symbol=? ORDER BY report_date", (symbol,))
        if not rows:
            return self._empty()
        df = pd.DataFrame(rows).drop(columns=["symbol"])
        df["report_date"] = pd.to_datetime(df["report_date"])
        return df.set_index("report_date")

    def get_many(self, symbols: list[str]) -> dict[str, pd.DataFrame]:
        """Batch read: ``{symbol -> filing-date-indexed frame}`` in **one** query — the
        scan/backtest hot path, vs N per-symbol round-trips. Symbols with no stored rows
        are omitted (callers treat absence as 'no fundamentals')."""
        syms = list(dict.fromkeys(symbols))           # dedupe, preserve order
        if not syms:
            return {}
        placeholders = ",".join(["?"] * len(syms))
        rows = self.db.query(
            f"SELECT * FROM fundamentals WHERE symbol IN ({placeholders}) "
            "ORDER BY symbol, report_date", tuple(syms))
        if not rows:
            return {}
        df = pd.DataFrame(rows)
        df["report_date"] = pd.to_datetime(df["report_date"])
        out: dict[str, pd.DataFrame] = {}
        for sym, g in df.groupby("symbol"):
            out[str(sym)] = g.drop(columns=["symbol"]).set_index("report_date")[self._COLS]
        return out


def _named(sql: str, row: dict) -> dict:
    """Filter ``row`` to just the ``:placeholders`` present in ``sql``."""
    import re

    keys = set(re.findall(r":(\w+)", sql))
    return {k: row.get(k) for k in keys}

"""Database backends with schema initialization.

SQLite is the development/test default; pointing ``SBS_DB_URL`` at a
``postgres(ql)://`` URL selects the psycopg-backed :class:`PostgresDatabase`.
The portable schema in ``schema.sql`` (placeholders + autoincrement PKs) is
translated to PostgreSQL dialect at runtime. Business logic goes through
repositories, never raw SQL, so the backend swap stays localized here.
"""
from __future__ import annotations

import re
import sqlite3
from collections.abc import Iterable
from pathlib import Path
from typing import Any

SCHEMA_PATH = Path(__file__).with_name("schema.sql")


class Database:
    def __init__(self, path: str):
        if "://" in path and not path.startswith("sqlite"):
            raise ValueError(
                f"{path!r} is not a SQLite path. Use connect(): a postgres(ql):// "
                "URL routes to PostgresDatabase; other schemes are unsupported."
            )
        self.path = path
        if path != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(path)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys = ON")

    # -- schema -------------------------------------------------------------
    def init_schema(self) -> None:
        self.conn.executescript(SCHEMA_PATH.read_text())
        self._migrate()
        self.conn.commit()

    def _migrate(self) -> None:
        """Idempotent column adds for DBs created before a column existed."""
        have = {r["name"] for r in self.query("PRAGMA table_info(signals)")}
        if "score_factors" not in have:
            self.conn.execute("ALTER TABLE signals ADD COLUMN score_factors TEXT")

    # -- core helpers -------------------------------------------------------
    def execute(self, sql: str, params: Any = ()) -> sqlite3.Cursor:
        # dict -> named binding (:name); sequence -> positional binding (?)
        bind = params if isinstance(params, dict) else tuple(params)
        return self.conn.execute(sql, bind)

    def executemany(self, sql: str, seq: Iterable[Iterable[Any]]) -> None:
        self.conn.executemany(sql, [tuple(p) for p in seq])

    def insert(self, sql: str, params: Iterable[Any] = ()) -> int:
        cur = self.execute(sql, params)
        return int(cur.lastrowid)

    def query(self, sql: str, params: Iterable[Any] = ()) -> list[dict]:
        cur = self.execute(sql, params)
        return [dict(row) for row in cur.fetchall()]

    def query_one(self, sql: str, params: Iterable[Any] = ()) -> dict | None:
        cur = self.execute(sql, params)
        row = cur.fetchone()
        return dict(row) if row else None

    def commit(self) -> None:
        self.conn.commit()

    def close(self) -> None:
        self.conn.close()

    def __enter__(self) -> Database:
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        if exc_type is None:
            self.commit()
        self.close()


# ---------------------------------------------------------------------------
# PostgreSQL backend (production). SQLite stays the dev/test default.
# ---------------------------------------------------------------------------
_NAMED_PARAM = re.compile(r":([a-zA-Z_]\w*)")


def _is_postgres(url: str) -> bool:
    return url.startswith(("postgresql://", "postgres://"))


def to_pg_placeholders(sql: str) -> str:
    """Translate SQLite placeholders to psycopg's: ``:name`` -> ``%(name)s`` and
    ``?`` -> ``%s``. Suited to this codebase's SQL (no ``?``/``:``/``%`` inside
    string literals — add ``%`` -> ``%%`` escaping here if a ``LIKE '%...%'`` is
    ever introduced)."""
    return _NAMED_PARAM.sub(r"%(\1)s", sql).replace("?", "%s")


def to_pg_schema(sql: str) -> str:
    """Translate the portable schema to PostgreSQL: autoincrement integer PKs
    become ``SERIAL``."""
    return sql.replace("INTEGER PRIMARY KEY AUTOINCREMENT", "SERIAL PRIMARY KEY")


class PostgresDatabase:
    """PostgreSQL backend (psycopg v3), mirroring :class:`Database` so
    repositories stay backend-agnostic. Placeholders and the portable schema are
    translated to PG dialect on the fly.

    The SQL translation is unit-tested; a full round-trip against a live
    PostgreSQL server is still pending (psycopg isn't installed in CI), so SQLite
    remains the development/test default.
    """

    def __init__(self, url: str):
        try:
            import psycopg
            from psycopg.rows import dict_row
        except ModuleNotFoundError as e:  # pragma: no cover - optional dependency
            raise RuntimeError(
                "PostgreSQL backend needs psycopg — `pip install 'psycopg[binary]'`."
            ) from e
        self.conn = psycopg.connect(url, row_factory=dict_row)

    def init_schema(self) -> None:
        body = "\n".join(
            ln for ln in to_pg_schema(SCHEMA_PATH.read_text()).splitlines()
            if not ln.strip().startswith("--")
        )
        with self.conn.cursor() as cur:
            for stmt in body.split(";"):
                if stmt.strip():
                    cur.execute(stmt)
            cur.execute("ALTER TABLE signals ADD COLUMN IF NOT EXISTS score_factors TEXT")
        self.conn.commit()

    def execute(self, sql: str, params: Any = ()):
        cur = self.conn.cursor()
        cur.execute(to_pg_placeholders(sql), params if isinstance(params, dict) else tuple(params))
        return cur

    def executemany(self, sql: str, seq: Iterable[Iterable[Any]]) -> None:
        with self.conn.cursor() as cur:
            cur.executemany(to_pg_placeholders(sql), [tuple(p) for p in seq])

    def insert(self, sql: str, params: Iterable[Any] = ()) -> int:
        cur = self.execute(sql, params)
        try:  # serial PKs: lastval() yields the new id; composite-PK inserts ignore it
            cur.execute("SELECT lastval()")
            return int(cur.fetchone()["lastval"])
        except Exception:  # noqa: BLE001 - a non-serial insert has no sequence
            return 0

    def query(self, sql: str, params: Iterable[Any] = ()) -> list[dict]:
        return [dict(r) for r in self.execute(sql, params).fetchall()]

    def query_one(self, sql: str, params: Iterable[Any] = ()) -> dict | None:
        r = self.execute(sql, params).fetchone()
        return dict(r) if r else None

    def commit(self) -> None:
        self.conn.commit()

    def close(self) -> None:
        self.conn.close()

    def __enter__(self) -> PostgresDatabase:
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        if exc_type is None:
            self.commit()
        self.close()


def connect(path: str, init: bool = True):
    """Open the database for ``path``: a ``postgres(ql)://`` URL selects the
    PostgreSQL backend, anything else is treated as a SQLite path."""
    db = PostgresDatabase(path) if _is_postgres(path) else Database(path)
    if init:
        db.init_schema()
    return db

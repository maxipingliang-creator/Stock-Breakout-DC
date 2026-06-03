"""SQLite-backed database with schema initialization.

The schema in ``schema.sql`` is portable; production can point ``SBS_DB_URL`` at
PostgreSQL and swap this thin wrapper for a psycopg-based one. Business logic
goes through repositories, never raw SQL, so that swap stays localized here.
"""
from __future__ import annotations

import sqlite3
from collections.abc import Iterable
from pathlib import Path
from typing import Any

SCHEMA_PATH = Path(__file__).with_name("schema.sql")


class Database:
    def __init__(self, path: str):
        if "://" in path and not path.startswith("sqlite"):
            raise NotImplementedError(
                f"Non-SQLite backend {path!r} not yet wired. Set SBS_DB_URL to a "
                "sqlite path for development, or implement a psycopg adapter."
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
        self.conn.commit()

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


def connect(path: str, init: bool = True) -> Database:
    db = Database(path)
    if init:
        db.init_schema()
    return db

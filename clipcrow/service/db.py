"""One small database layer under the accounts code: SQLite (a file, the default; single user, development, tests) or PostgreSQL (``DATABASE_URL``).

The same SQL runs on both: ``?`` placeholders, ``ON CONFLICT`` upserts and ``RETURNING`` (both engines have them). What differs is hidden here: the pool and the
transactions, how a row is locked (``FOR UPDATE`` on Postgres; SQLite's ``BEGIN IMMEDIATE`` already serialises writers), the schema's column types, and adding a
column to an older database. Rows are dict-like (``row["email"]``). A unique or foreign-key violation is always ``IntegrityError``. The connection string holds
a password: it is never logged or put in a message (``describe()`` gives a safe description).
"""
from __future__ import annotations

import re
import sqlite3
import threading
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator, Optional
from urllib.parse import urlsplit


class IntegrityError(Exception):
    """A unique or foreign-key rule refused the row."""


class Result:
    def __init__(self, rows: list, rowcount: int):
        self._rows, self.rowcount = rows, rowcount

    def fetchone(self):
        return self._rows[0] if self._rows else None

    def fetchall(self) -> list:
        return list(self._rows)


def is_postgres(target: Any) -> bool:
    return str(target).startswith(("postgres://", "postgresql://"))


def describe(target: Any) -> str:
    """A safe name for logs and the Operator page: the engine, host and database, never the user or the password."""
    if not is_postgres(target):
        return "SQLite file"
    p = urlsplit(str(target))
    return f"PostgreSQL {p.hostname or 'localhost'}/{(p.path or '/').lstrip('/')}"


# ---- SQLite -------------------------------------------------------------------------------------------------------------------------------

class _SqliteTx:
    def __init__(self, db: "SqliteDb"):
        self._db = db

    def q(self, sql: str, args: tuple = ()) -> Result:
        return self._db._run(sql, args)


class SqliteDb:
    dialect = "sqlite"
    for_update = ""

    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(self.path), check_same_thread=False, isolation_level=None)
        self._conn.row_factory = sqlite3.Row
        self._lock = threading.RLock()
        with self._lock:
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.execute("PRAGMA foreign_keys=ON")

    def _run(self, sql: str, args: tuple = ()) -> Result:
        try:
            cur = self._conn.execute(sql, args)
            return Result(cur.fetchall() if cur.description else [], cur.rowcount)
        except sqlite3.IntegrityError as e:
            raise IntegrityError(str(e)) from None

    def q(self, sql: str, args: tuple = ()) -> Result:
        with self._lock:
            return self._run(sql, args)

    @contextmanager
    def tx(self) -> Iterator[_SqliteTx]:
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                yield _SqliteTx(self)
            except BaseException:
                self._conn.execute("ROLLBACK")
                raise
            else:
                self._conn.execute("COMMIT")

    def columns(self, table: str) -> set:
        with self._lock:
            return {r[1] for r in self._conn.execute(f"PRAGMA table_info({table})")}

    def script(self, statements: list[str]) -> None:
        with self._lock:
            for s in statements:
                self._conn.execute(s)

    def add_column(self, table: str, column: str, definition: str) -> None:
        if column not in self.columns(table):
            self.q(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")

    def backup_to(self, dest: Path) -> None:
        out = sqlite3.connect(str(dest))
        try:
            with self._lock:
                self._conn.backup(out)
        finally:
            out.close()

    def close(self) -> None:
        with self._lock:
            self._conn.close()


# ---- PostgreSQL ---------------------------------------------------------------------------------------------------------------------------

def _placeholders(sql: str) -> str:
    if "%" in sql:
        raise ValueError("a literal % in SQL is not allowed (placeholders are converted)")
    return sql.replace("?", "%s")


class _PgTx:
    def __init__(self, conn):
        self._conn = conn

    def q(self, sql: str, args: tuple = ()) -> Result:
        import psycopg
        try:
            cur = self._conn.execute(_placeholders(sql), args)
            return Result(cur.fetchall() if cur.description else [], cur.rowcount)
        except psycopg.errors.IntegrityError as e:
            raise IntegrityError(type(e).__name__) from None


class PgDb:
    dialect = "postgres"
    for_update = " FOR UPDATE"

    def __init__(self, url: str, *, schema: Optional[str] = None, pool_size: int = 10):
        import psycopg
        from psycopg.rows import dict_row
        from psycopg_pool import ConnectionPool
        if schema is not None and not re.fullmatch(r"[a-z][a-z0-9_]{0,40}", schema):
            raise ValueError("a schema name is lowercase letters, digits and underscores")
        self.url, self.schema = url, schema
        if schema:                                                         # tests give every database its own schema
            with psycopg.connect(url, autocommit=True) as c:
                c.execute(f'CREATE SCHEMA IF NOT EXISTS "{schema}"')

        def configure(conn):
            if schema:
                conn.execute(f'SET search_path TO "{schema}"')
        self.pool = ConnectionPool(url, min_size=1, max_size=max(2, pool_size), kwargs={"autocommit": True, "row_factory": dict_row, "client_encoding": "UTF8"}, configure=configure,
                                   open=True, timeout=15, name="clipcrow")
        self.pool.wait(timeout=20)

    def q(self, sql: str, args: tuple = ()) -> Result:
        import psycopg
        try:
            with self.pool.connection() as c:
                cur = c.execute(_placeholders(sql), args)
                return Result(cur.fetchall() if cur.description else [], cur.rowcount)
        except psycopg.errors.IntegrityError as e:
            raise IntegrityError(type(e).__name__) from None

    @contextmanager
    def tx(self) -> Iterator[_PgTx]:
        with self.pool.connection() as c:
            with c.transaction():
                yield _PgTx(c)

    def columns(self, table: str) -> set:
        return {r["column_name"] for r in self.q("SELECT column_name FROM information_schema.columns WHERE table_name=? AND table_schema = current_schema()", (table,)).fetchall()}

    def script(self, statements: list[str]) -> None:
        with self.pool.connection() as c:
            for s in statements:
                c.execute(s)

    def add_column(self, table: str, column: str, definition: str) -> None:
        self.q(f"ALTER TABLE {table} ADD COLUMN IF NOT EXISTS {column} {definition}")

    def close(self) -> None:
        self.pool.close()

    def drop_schema(self) -> None:
        """Tests only: remove this database's private schema."""
        if self.schema:
            import psycopg
            self.pool.close()
            with psycopg.connect(self.url, autocommit=True) as c:
                c.execute(f'DROP SCHEMA IF EXISTS "{self.schema}" CASCADE')


def connect(target: str | Path, *, schema: Optional[str] = None, pool_size: int = 10):
    return PgDb(str(target), schema=schema, pool_size=pool_size) if is_postgres(target) else SqliteDb(target)

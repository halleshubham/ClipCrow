"""Which database the accounts use, and a guard against pointing a live server at an EMPTY one.

``DATABASE_URL`` is the operator's own choice (a managed PostgreSQL, or one they made). ``CLIPCROW_BUNDLED_DB`` is the address of the PostgreSQL that ``docker-compose.yml``
starts beside ClipCrow; the compose file sets it and the operator never has to. The rules:

* ``DATABASE_URL`` set: use it. But if it is EMPTY (no people) while the SQLite file in the data folder has people, refuse to start and say how to move them: pointing a live
  server at an empty database makes every account look deleted. ``CLIPCROW_ALLOW_EMPTY_DB=1`` says "yes, I really want a fresh database".
* Else the bundled PostgreSQL is used, unless the SQLite file has people and the bundled database has none: then the server KEEPS USING the SQLite file and says so loudly,
  so adding the bundled database to a running installation can never lose anyone. ``clipcrow migrate-db`` moves the people; after that the bundled database takes over.
  With ``CLIPCROW_AUTO_MIGRATE_DB=1`` the server does that copy itself at start (and stays on the SQLite file if the copy fails).
* Else (a fresh install, or no database variable at all): the SQLite file.

The report and the messages never contain the address (it holds a password).
"""
from __future__ import annotations

import os
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping, Optional

from .accounts import Accounts


class DatabaseChoiceError(RuntimeError):
    """A sentence safe to print (never the database address)."""


@dataclass
class Choice:
    accounts: Accounts
    kind: str            # "explicit" | "bundled" | "sqlite" | "sqlite-waiting"
    note: str = ""       # a line for the log, when there is something the operator should know


def sqlite_people(path: Path) -> int:
    """People in an existing SQLite file (0 when there is no file or no table); read-only, never creates anything."""
    if not path.is_file():
        return 0
    try:
        con = sqlite3.connect(f"file:{path.resolve()}?mode=ro", uri=True)
        try:
            return int(con.execute("SELECT COUNT(*) FROM users").fetchone()[0])
        finally:
            con.close()
    except sqlite3.Error:
        return 0


def people(acc: Accounts) -> int:
    return int(acc.db.q("SELECT COUNT(*) AS n FROM users").fetchone()["n"])


def target_url(env: Optional[Mapping[str, str]] = None) -> str:
    """The PostgreSQL address commands such as ``migrate-db`` and ``migrate-stores`` write to: ``DATABASE_URL``, else the bundled one."""
    env = os.environ if env is None else env
    return (env.get("DATABASE_URL") or "").strip() or (env.get("CLIPCROW_BUNDLED_DB") or "").strip()


def choose(data_dir: str | Path, env: Optional[Mapping[str, str]] = None) -> Choice:
    env = os.environ if env is None else env
    data = Path(data_dir)
    sqlite = data / "clipcrow.db"
    secret = data / "clipcrow.secret"
    explicit = (env.get("DATABASE_URL") or "").strip()
    bundled = (env.get("CLIPCROW_BUNDLED_DB") or "").strip()
    waiting = sqlite_people(sqlite)
    if explicit or bundled:
        acc = Accounts(explicit or bundled, secret_file=secret)
        if waiting and people(acc) == 0:
            if explicit and env.get("CLIPCROW_ALLOW_EMPTY_DB") != "1":
                raise DatabaseChoiceError(
                    f"DATABASE_URL points at an EMPTY database, but the SQLite file in {data} has {waiting} people. Starting would make every account look deleted. "
                    "Move them first (in the container terminal: clipcrow migrate-db --data /data), or set CLIPCROW_ALLOW_EMPTY_DB=1 if you really want a fresh database.")
            if not explicit and env.get("CLIPCROW_AUTO_MIGRATE_DB") == "1":
                # The operator asked for the move to happen by itself (there is no terminal to type migrate-db in). It is the same copy: repeatable, read-only on
                # the SQLite file, and it checks the counts and the purchased minutes. Any problem leaves the server on the SQLite file, exactly as before.
                try:
                    from .dbmigrate import migrate
                    report = migrate(sqlite, acc)
                    if people(acc) < waiting:
                        raise RuntimeError("fewer people arrived than were in the SQLite file")
                    return Choice(acc, "bundled", f"moved {report.get('users', 0)} people (and {report.get('sessions', 0)} sessions, {report.get('usage', 0)} usage rows) from the SQLite file into "
                                                  "the bundled PostgreSQL; the SQLite file is untouched and is no longer used. Remove CLIPCROW_AUTO_MIGRATE_DB once you have checked.")
                except Exception as e:                                    # noqa: BLE001 - whatever went wrong, the people stay where they are
                    acc.db.close()
                    keep = Accounts(sqlite, secret_file=secret)
                    return Choice(keep, "sqlite-waiting", f"moving the people into the bundled PostgreSQL failed ({type(e).__name__}), so the SQLite file is still in use and nothing was lost. "
                                                          "Details are in the log; fix the cause or run: clipcrow migrate-db --data /data")
            if not explicit:
                acc.db.close()
                keep = Accounts(sqlite, secret_file=secret)
                return Choice(keep, "sqlite-waiting",
                              f"the bundled PostgreSQL is empty and the SQLite file has {waiting} people, so the SQLite file is still in use. "
                              "Move them with: clipcrow migrate-db --data /data  (then restart; the bundled PostgreSQL takes over).")
        return Choice(acc, "explicit" if explicit else "bundled")
    return Choice(Accounts(sqlite, secret_file=secret), "sqlite")

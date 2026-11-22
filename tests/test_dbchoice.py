"""Which database the accounts use: the bundled PostgreSQL is picked up by itself, and a live server can never be pointed at an empty database by accident."""
import os
import uuid

import pytest

from clipcrow.service import db as dbm
from clipcrow.service.accounts import Accounts
from clipcrow.service.dbchoice import DatabaseChoiceError, choose, sqlite_people, target_url

PG = os.environ.get("CLIPCROW_TEST_DATABASE_URL")


@pytest.fixture(autouse=True)
def accounts_on_postgres():
    """Overrides the suite-wide fixture that moves every Accounts(file) to PostgreSQL: these tests are about the real SQLite file and an explicit PostgreSQL address."""
    yield


@pytest.fixture
def data(tmp_path):
    d = tmp_path / "data"
    d.mkdir()
    return d


def with_people(data, n=2):
    a = Accounts(data / "clipcrow.db", secret_file=data / "clipcrow.secret")
    for i in range(n):
        a.ensure_user(f"p{i}@example.com")
    a.db.close()


@pytest.fixture
def pg_url():
    """An empty PostgreSQL schema reached through its own URL (search_path in the options), so the server code sees a brand-new database."""
    if not PG:
        pytest.skip("needs CLIPCROW_TEST_DATABASE_URL")
    schema = "t_" + uuid.uuid4().hex[:16]
    admin = dbm.connect(PG, schema=schema)                                # creates the schema
    sep = "&" if "?" in PG else "?"
    yield f"{PG}{sep}options=-csearch_path%3D{schema}"
    admin.drop_schema()


def test_nothing_set_means_the_sqlite_file(data):
    c = choose(data, {})
    assert c.kind == "sqlite" and c.accounts.postgres is False and not c.note


def test_sqlite_people_is_read_only_and_zero_when_there_is_no_file(data):
    assert sqlite_people(data / "clipcrow.db") == 0 and not (data / "clipcrow.db").exists()
    with_people(data, 3)
    assert sqlite_people(data / "clipcrow.db") == 3


def test_target_url_prefers_the_operators_own_address():
    assert target_url({"DATABASE_URL": "postgresql://a/b", "CLIPCROW_BUNDLED_DB": "postgresql://c/d"}) == "postgresql://a/b"
    assert target_url({"CLIPCROW_BUNDLED_DB": "postgresql://c/d"}) == "postgresql://c/d" and target_url({}) == ""


def test_a_fresh_install_uses_the_bundled_postgresql(data, pg_url):
    c = choose(data, {"CLIPCROW_BUNDLED_DB": pg_url})
    assert c.kind == "bundled" and c.accounts.postgres is True and not c.note
    c.accounts.db.close()


def test_adding_the_bundled_database_to_a_live_server_never_loses_anyone(data, pg_url):
    with_people(data, 2)                                                  # the running installation: people in the SQLite file
    c = choose(data, {"CLIPCROW_BUNDLED_DB": pg_url})
    assert c.kind == "sqlite-waiting" and c.accounts.postgres is False and c.accounts.by_email("p0@example.com")
    assert "migrate-db" in c.note and "postgresql://" not in c.note and pg_url not in c.note         # says what to do, never the address
    c.accounts.db.close()


def test_an_explicit_empty_database_is_refused_while_the_sqlite_file_has_people(data, pg_url):
    with_people(data, 2)
    with pytest.raises(DatabaseChoiceError) as e:
        choose(data, {"DATABASE_URL": pg_url})
    assert "EMPTY" in str(e.value) and "migrate-db" in str(e.value) and pg_url not in str(e.value)
    c = choose(data, {"DATABASE_URL": pg_url, "CLIPCROW_ALLOW_EMPTY_DB": "1"})                      # "yes, a fresh database, really"
    assert c.kind == "explicit"
    c.accounts.db.close()


def test_once_the_people_are_moved_the_database_takes_over(data, pg_url):
    from clipcrow.service.dbmigrate import migrate
    with_people(data, 2)
    target = Accounts(pg_url, secret_file=data / "clipcrow.secret")
    migrate(data / "clipcrow.db", target)
    target.db.close()
    c = choose(data, {"CLIPCROW_BUNDLED_DB": pg_url})
    assert c.kind == "bundled" and c.accounts.by_email("p1@example.com") is not None and not c.note
    c.accounts.db.close()
    c = choose(data, {"DATABASE_URL": pg_url})
    assert c.kind == "explicit"
    c.accounts.db.close()

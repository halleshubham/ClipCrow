"""The record store under every moved store: the same behaviour on SQLite and on PostgreSQL."""
import os
import threading
import uuid

import pytest

from clipcrow.service import db as dbm
from clipcrow.service.docstore import Conflict, DocStore, store_mode

PG = os.environ.get("CLIPCROW_TEST_DATABASE_URL")


@pytest.fixture(params=["sqlite", "postgres"])
def docs(request, tmp_path):
    if request.param == "postgres":
        if not PG:
            pytest.skip("needs CLIPCROW_TEST_DATABASE_URL")
        db = dbm.connect(PG, schema="t_" + uuid.uuid4().hex[:16])
        yield DocStore(db)
        db.drop_schema()
    else:
        db = dbm.connect(tmp_path / "x.db")
        yield DocStore(db)
        db.close()


def test_put_get_delete_and_versions(docs):
    assert docs.get("style", "o1", "a") is None
    assert docs.put("style", "o1", "a", '{"n": 1}') == 1
    assert docs.put("style", "o1", "a", '{"n": 2}') == 2
    assert docs.get("style", "o1", "a") == ('{"n": 2}', 2)
    docs.put("style", "o1", "b", "{}")
    docs.put("style", "o2", "a", "{}")
    assert [k for k, _ in docs.items("style", "o1")] == ["a", "b"] and docs.owners("style") == ["o1", "o2"] and docs.count("style") == 3
    docs.delete("style", "o1", "a")
    assert docs.text("style", "o1", "a") is None and docs.text("style", "o1", "b") == "{}"
    docs.delete("style", "o1")                                       # everything of one owner
    assert docs.items("style", "o1") == [] and docs.count("style") == 1


def test_a_write_over_a_changed_record_is_refused(docs):
    docs.put("k", "o", "x", "a")
    v = docs.get("k", "o", "x")[1]
    docs.put("k", "o", "x", "b")                                      # someone else wrote in between
    with pytest.raises(Conflict):
        docs.put("k", "o", "x", "c", expect_version=v)
    with pytest.raises(Conflict):
        docs.put("k", "o", "new", "c", expect_version=3)              # not there, and not version 3
    docs.put("k", "o", "fresh", "c", expect_version=0)                # 0 = "must not exist yet"
    assert docs.text("k", "o", "x") == "b"


def test_many_writers_lose_no_update(docs):
    """Eight threads each add 25 to one counter through update(): the sum is exact, with no process lock."""
    docs.put("count", "o", "n", "0")
    errors = []

    def work():
        try:
            for _ in range(25):
                docs.update("count", "o", "n", lambda old: str(int(old or 0) + 1), retries=200)
        except Exception as e:               # noqa: BLE001
            errors.append(e)
    ts = [threading.Thread(target=work) for _ in range(8)]
    [t.start() for t in ts]
    [t.join() for t in ts]
    assert not errors and docs.text("count", "o", "n") == "200"


def test_text_is_stored_as_written_including_devanagari(docs):
    text = '{"title": "तुमची झोप का महत्त्वाची आहे?", "n": null}'
    docs.put("job", "o", "j", text)
    assert docs.text("job", "o", "j") == text


def test_store_mode_reads_the_environment_and_defaults_to_files():
    assert store_mode("style", {}) == "files"
    assert store_mode("style", {"CLIPCROW_STORE": "both"}) == "both"
    assert store_mode("style", {"CLIPCROW_STORE": "both", "CLIPCROW_STORE_STYLE": "db"}) == "db"
    assert store_mode("style", {"CLIPCROW_STORE": "db"}, has_db=False) == "files"           # a single-user install has no database
    with pytest.raises(ValueError):
        store_mode("style", {"CLIPCROW_STORE": "postgres"})
    with pytest.raises(ValueError):
        store_mode("../x", {})

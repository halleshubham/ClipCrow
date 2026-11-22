"""The moved stores behave the same wherever their records live: the files, both, or the database only (SQLite here; PostgreSQL in test_docstore)."""
import json

import pytest

from clipcrow.service import db as dbm
from clipcrow.service.brand import BrandStore
from clipcrow.service.docstore import DocStore
from clipcrow.service.schedule import SchedulePrefs
from clipcrow.service.settings import SettingsStore
from clipcrow.service.style_store import StyleError, StyleStore
from clipcrow.styles import StyleSettings

OWNER = "a" * 32
OTHER = "b" * 32


@pytest.fixture(params=["files", "both", "db"])
def kit(request, tmp_path):
    db = dbm.connect(tmp_path / "x.db")
    docs = DocStore(db)
    yield request.param, tmp_path, docs
    db.close()


def test_styles_through_their_whole_life(kit):
    mode, tmp, docs = kit
    st = StyleStore(tmp, docs, mode)
    rec = st.create(OWNER, "Reference one")
    with pytest.raises(StyleError):
        st.create(OWNER, "Second at once")                                  # one measurement at a time
    assert [r.status for r in st.list(OWNER)] == ["analyzing"] and st.stale_analyzing()[0][0] == OWNER
    ready = st.finish(OWNER, rec.id, {"cuts_per_min": 9}, StyleSettings())
    assert ready.status == "ready" and st.get(OWNER, rec.id).measured == {"cuts_per_min": 9}
    st.update(OWNER, rec.id, name="Renamed")
    assert st.get(OWNER, rec.id).name == "Renamed" and st.list(OTHER) == []
    st.delete(OWNER, rec.id)
    with pytest.raises(KeyError):
        st.get(OWNER, rec.id)
    st.create(OWNER, "Again")
    st.delete_all(OWNER)
    assert st.list(OWNER) == []
    if mode != "files":
        assert docs.count("style") == 0


def test_a_file_only_store_with_a_database_still_reads_the_files(kit):
    """both: written to the files and the database, read from the files."""
    mode, tmp, docs = kit
    if mode != "both":
        pytest.skip("both only")
    st = StyleStore(tmp, docs, "both")
    rec = st.create(OWNER, "x")
    assert (tmp / "styles" / OWNER / f"{rec.id}.json").is_file() and docs.text("style", OWNER, rec.id) == (tmp / "styles" / OWNER / f"{rec.id}.json").read_text()


def test_schedule_prefs(kit):
    mode, tmp, docs = kit
    s = SchedulePrefs(tmp, docs, mode)
    assert s.get(OWNER)["saved"] is False
    s.put(OWNER, "Asia/Kolkata", {"youtube": ["09:30", "18:00"]})
    got = s.get(OWNER)
    assert got["saved"] and got["timezone"] == "Asia/Kolkata" and got["times"]["youtube"] == ["09:30", "18:00"]
    assert s.get(OTHER)["saved"] is False
    s.delete_all(OWNER)
    assert s.get(OWNER)["saved"] is False


def test_operator_settings_survive_a_restart(kit):
    mode, tmp, docs = kit
    from clipcrow.service.settings import BY_KEY
    key = next(k for k, v in BY_KEY.items() if v.kind == "int")
    base = {k: (v.default if hasattr(v, "default") else 0) for k, v in BY_KEY.items()}
    path = tmp / "settings.json"
    a = SettingsStore(path, base, docs, mode)
    old = a.values()[key]
    new = int(old) + 1 if int(old) < (BY_KEY[key].maximum or 10 ** 9) else int(old) - 1
    try:
        a.update({key: new}, "someone@x")
    except ValueError:
        pytest.skip("this setting has cross-checks")
    b = SettingsStore(path, base, docs, mode)                                # a new process reading what the first one saved
    assert b.values()[key] == new and b.history()[0]["key"] == key
    b.reset(key, "someone@x")
    assert SettingsStore(path, base, docs, mode).values()[key] == old


def test_brands_settings_overlay_info_and_erasure(kit):
    mode, tmp, docs = kit
    st = BrandStore(tmp, docs, mode)
    first = st.default_id(OWNER)
    other = st.create(OWNER, "Second")["id"]
    st.set_settings(OWNER, {"logo_position": "bl", "overlay_position": "tc"}, other)
    assert st.settings(OWNER, other)["logo_position"] == "bl" and st.settings(OWNER, first)["logo_position"] == "tr"
    st.rename(OWNER, other, "Renamed")
    st.set_default(OWNER, other)
    again = BrandStore(tmp, docs, mode)                                      # a restart
    assert again.default_id(OWNER) == other and [b["name"] for b in again.list(OWNER)] == ["My brand", "Renamed"]
    assert again.settings(OWNER, other)["overlay_position"] == "tc"
    again.remove(OWNER, other)
    assert again.default_id(OWNER) == first and not again.has(OWNER, other)
    if mode != "files":
        assert docs.text("brand_settings", OWNER, other) is None
    again.delete_all(OWNER)
    if mode != "files":
        assert docs.count("brand_registry") == 0 and docs.count("brand_settings") == 0


def test_migrate_copies_the_files_and_verify_sees_every_difference(tmp_path):
    from clipcrow.service import storemigrate
    db = dbm.connect(tmp_path / "x.db")
    docs = DocStore(db)
    files = StyleStore(tmp_path, None, "files")
    files.create(OWNER, "One")
    BrandStore(tmp_path).set_settings(OWNER, {"logo_position": "bl"})
    SchedulePrefs(tmp_path).put(OTHER, "Asia/Kolkata", {})
    before = storemigrate.verify(tmp_path, docs)
    assert before["same"] == 0 and len(before["only_in_files"]) >= 4
    rep = storemigrate.migrate(tmp_path, docs)
    assert rep["style"] == 1 and rep["brand_settings"] == 1 and rep["schedule"] == 1 and rep["brand_registry"] == 1
    after = storemigrate.verify(tmp_path, docs)
    assert not (after["different"] or after["only_in_files"] or after["only_in_db"]) and after["same"] == after["files"]
    assert storemigrate.migrate(tmp_path, docs) == {"unchanged": after["files"]}                    # twice is safe
    docs.put("style", OWNER, "ffffffffffff", "{}")                                                  # something only the database has
    BrandStore(tmp_path).set_settings(OWNER, {"logo_position": "br"})                                # a file that moved on
    again = storemigrate.verify(tmp_path, docs)
    assert again["only_in_db"] == ["style/ffffffffffff"] and again["different"] == [f"brand_settings/{BrandStore(tmp_path).default_id(OWNER)}"]
    db.close()


@pytest.mark.parametrize("mode", ["files", "both", "db"])
def test_the_app_works_the_same_in_every_mode(tmp_path, monkeypatch, mode):
    """The brand page, schedule and operator settings through the real API with accounts on, for each place the records can live."""
    pytest.importorskip("fastapi")
    monkeypatch.setenv("CLIPCROW_STORE", mode)
    from tests.test_accounts import app_client, signup
    c, accounts, app = app_client(tmp_path)
    signup(c, "who@example.com")
    assert c.get("/api/brand").json()["settings"]["logo_position"] == "tr"
    assert c.put("/api/brand", json={"logo_position": "bl"}).json()["settings"]["logo_position"] == "bl"
    assert c.get("/api/brand").json()["settings"]["logo_position"] == "bl"
    made = c.post("/api/brands", json={"name": "Second"})
    assert made.status_code == 201
    assert len(c.get("/api/brand").json()["brands"]) == 2
    assert c.get("/api/styles").json() == []
    docs = accounts.db.q("SELECT COUNT(*) AS n FROM docs").fetchone()["n"]
    assert (docs > 0) == (mode != "files")

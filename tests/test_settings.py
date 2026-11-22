"""The Operator page's settings: validated, saved, applied at once, and never a secret."""
import json
import os

import pytest

pytest.importorskip("fastapi")
from fastapi.testclient import TestClient

from clipcrow.service import settings as st
from clipcrow.service.accounts import Accounts
from clipcrow.service.api import create_app
from clipcrow.service.plans import Catalog, Payg, Plan
from tests.mailhelp import Outbox, sign_up
from tests.test_service import FakePipeline, upload, video_bytes, wait  # noqa: F401


@pytest.fixture(autouse=True)
def clean_env():
    """Applying a setting writes a few environment variables for code that reads them on demand: put the environment back after each test."""
    before = dict(os.environ)
    yield
    os.environ.clear()
    os.environ.update(before)


def make(tmp_path, monkeypatch=None, **kw):
    if monkeypatch is not None:
        monkeypatch.setenv("CLIPCROW_ADMINS", "boss@example.com")
    a = Accounts(tmp_path / "data" / "clipcrow.db")
    cat = Catalog(plans={"free": Plan("free", "Pay as you go", 0, 30, 7)}, payg=Payg())
    app = create_app(tmp_path / "data", FakePipeline(), accounts=a, mailer=Outbox(), catalog=cat, **kw)
    boss, user = TestClient(app), TestClient(app)
    sign_up(boss, "boss@example.com")
    sign_up(user, "user@example.com")
    return boss, user, app, a


def put(boss, **values):
    return boss.put("/api/admin/settings", json={"values": values})


# --- the schema --------------------------------------------------------------------------------

def test_every_setting_has_a_group_a_kind_and_validates_its_range():
    groups = {g for g, _, _ in st.GROUPS}
    assert len(st.SCHEMA) >= 30 and len({s.key for s in st.SCHEMA}) == len(st.SCHEMA)
    for s in st.SCHEMA:
        assert s.group in groups and s.kind in ("int", "float", "bool", "text", "enum", "json") and s.label and s.help
    p = st.BY_KEY["price_per_minute"]
    assert st.coerce(p, "8.5") == 8.5
    for bad in ("", "abc", -1, 100000, float("nan"), True, None):
        with pytest.raises(ValueError):
            st.coerce(p, bad)
    with pytest.raises(ValueError, match="whole number"):
        st.coerce(st.BY_KEY["min_job_minutes"], 2.5)
    assert st.coerce(st.BY_KEY["signup_open"], "off") is False and st.coerce(st.BY_KEY["gpu_fallback"], "strict") == "strict"
    with pytest.raises(ValueError):
        st.coerce(st.BY_KEY["gpu_fallback"], "sometimes")
    assert st.coerce(st.BY_KEY["gpu_rates"], '{"b": 2, "a": 1}') == '{"a": 1, "b": 2}'
    for bad in ('[1]', '{"a": -1}', '{"a": "x"}', "not json", '{"a": true}'):
        with pytest.raises(ValueError):
            st.coerce(st.BY_KEY["gpu_rates"], bad)
    assert st.coerce(st.BY_KEY["blocked_email_domains"], " A.com , b.org ") == "a.com, b.org"
    with pytest.raises(ValueError):
        st.coerce(st.BY_KEY["blocked_email_domains"], "not a domain")
    with pytest.raises(ValueError):
        st.coerce(st.BY_KEY["alert_email"], "nope")
    assert st.coerce(st.BY_KEY["alert_email"], "") == ""
    with pytest.raises(ValueError):
        st.coerce(st.BY_KEY["plan_name"], "line\\x00break".replace("\\x00", "\x00"))


def test_no_setting_is_a_secret():
    for s in st.SCHEMA:
        low = (s.key + s.label).lower()
        assert not any(w in low for w in ("password", "secret", "token", "api key", "apikey"))


# --- over HTTP ---------------------------------------------------------------------------------

def test_only_an_operator_can_read_or_change_settings(tmp_path, monkeypatch):
    boss, user, app, _ = make(tmp_path, monkeypatch)
    assert user.get("/api/admin/settings").status_code == 404 and user.put("/api/admin/settings", json={"values": {"min_job_minutes": 1}}).status_code == 404
    assert user.delete("/api/admin/settings/min_job_minutes").status_code == 404
    assert TestClient(app).get("/api/admin/settings").status_code == 401
    v = boss.get("/api/admin/settings").json()
    assert {s["key"] for s in v["settings"]} == {s.key for s in st.SCHEMA} and v["groups"] and v["services"] is not None
    assert all(s["source"] == "default" for s in v["settings"])


def test_a_change_is_applied_at_once_and_saved_and_survives_a_restart(tmp_path, monkeypatch):
    boss, user, app, a = make(tmp_path, monkeypatch)
    assert user.get("/api/pricing").json()["payg"]["price_per_minute"] == 8.0
    r = put(boss, price_per_minute=7.5, min_job_minutes=5, signup_bonus_minutes=4, tier1_from=1000, tier1_percent=3)
    assert r.status_code == 200
    g = user.get("/api/pricing").json()["payg"]
    assert g["price_per_minute"] == 7.5 and g["min_job_minutes"] == 5 and g["signup_bonus_minutes"] == 4          # no restart
    assert [t["bonus_percent"] for t in g["tiers"]][0] == 3
    row = next(s for s in r.json()["settings"] if s["key"] == "price_per_minute")
    assert row["value"] == 7.5 and row["default"] == 8.0 and row["source"] == "operator"
    if os.environ.get("CLIPCROW_STORE", "files") != "db":                                                          # the file is the record unless the database holds it
        assert json.loads((tmp_path / "data" / "settings.json").read_text())["values"]["price_per_minute"] == 7.5
        assert oct((tmp_path / "data" / "settings.json").stat().st_mode & 0o777) == "0o600"
    if os.environ.get("CLIPCROW_STORE", "files") == "db" and os.environ.get("CLIPCROW_TEST_DATABASE_URL"):
        return                              # the test database gives every Accounts its own private schema, so a "restart" would start empty (a real server keeps its database)
    boss2, user2, app2, _ = make(tmp_path, monkeypatch)                                                          # a new server on the same data folder
    assert user2.get("/api/pricing").json()["payg"]["price_per_minute"] == 7.5


def test_a_reset_goes_back_to_what_the_server_started_with(tmp_path, monkeypatch):
    boss, user, app, _ = make(tmp_path, monkeypatch)
    put(boss, price_per_minute=6, target_markup_percent=120, usd_inr=90)
    assert os.environ["CLIPCROW_MARGIN_PERCENT"] == "120"
    r = boss.delete("/api/admin/settings/price_per_minute")
    assert r.status_code == 200 and user.get("/api/pricing").json()["payg"]["price_per_minute"] == 8.0
    boss.delete("/api/admin/settings/target_markup_percent")
    assert "CLIPCROW_MARGIN_PERCENT" not in os.environ                                                          # the environment is back as it was
    assert boss.delete("/api/admin/settings/nope").status_code == 404


def test_a_bad_value_saves_nothing_not_even_the_good_ones_beside_it(tmp_path, monkeypatch):
    boss, user, _, _ = make(tmp_path, monkeypatch)
    r = put(boss, price_per_minute=9, min_job_minutes=0)
    assert r.status_code == 422 and "Minimum charge" in r.json()["detail"]
    assert user.get("/api/pricing").json()["payg"]["price_per_minute"] == 8.0
    assert put(boss, price_per_minute=9, min_recharge=900, max_recharge=500).status_code == 422                   # smallest above largest
    assert put(boss, tier1_from=1000, tier1_percent=0).status_code == 422                                          # half a tier
    assert put(boss, tier1_from=2000, tier1_percent=5, tier2_from=1000, tier2_percent=10).status_code == 422       # tiers out of order
    assert put(boss, nonsense=1).status_code == 422
    assert not (tmp_path / "data" / "settings.json").exists() or json.loads((tmp_path / "data" / "settings.json").read_text())["values"] == {}


def test_limits_signup_and_upload_size_change_live(tmp_path, monkeypatch, video_bytes):
    boss, user, app, a = make(tmp_path, monkeypatch)
    a.add_credits(a.by_email("user@example.com").id, 500)
    put(boss, max_jobs_per_day=1)
    assert upload(user, video_bytes).status_code in (200, 201)
    r = upload(user, video_bytes)
    assert r.status_code == 429 and "daily limit" in r.json()["detail"]
    put(boss, max_jobs_per_day=50, max_upload_mb=10)
    assert user.get("/api/config").json()["max_upload_mb"] == 10
    put(boss, signup_open=False)
    assert user.get("/api/config").json()["signup"] is False
    before = len(app.state.outbox.sent)
    assert TestClient(app).post("/api/auth/code", json={"email": "new@example.com"}).status_code == 200 and len(app.state.outbox.sent) == before   # no mail for a stranger


def test_more_operators_can_be_added_but_the_environment_ones_cannot_be_removed(tmp_path, monkeypatch):
    boss, user, app, _ = make(tmp_path, monkeypatch)
    assert user.get("/api/admin/settings").status_code == 404
    put(boss, extra_admins="user@example.com")
    assert user.get("/api/admin/settings").status_code == 200 and user.get("/api/me").json()["admin"] is True
    put(boss, extra_admins="")
    assert user.get("/api/admin/settings").status_code == 404 and boss.get("/api/admin/settings").status_code == 200    # the environment's operator stays


def test_gpu_and_pod_settings_reach_the_hub(tmp_path, monkeypatch):
    boss, _, app, _ = make(tmp_path, monkeypatch, worker_token="t" * 32)
    from clipcrow.service.workers import WorkerHub
    put(boss, gpu_fallback="strict", disabled_workers="home-gpu, race-3090", worker_pickup_seconds=200)
    hub = app.state.hub if hasattr(app.state, "hub") else None
    if hub is not None:
        assert hub.widen is False and hub.disabled == frozenset({"home-gpu", "race-3090"}) and hub.pickup_seconds == 200
    v = {s["key"]: s["value"] for s in boss.get("/api/admin/settings").json()["settings"]}
    assert v["gpu_fallback"] == "strict" and v["disabled_workers"] == "home-gpu, race-3090" and v["worker_pickup_seconds"] == 200


def test_the_target_margin_and_rates_change_the_money_section_at_once(tmp_path, monkeypatch):
    boss, _, _, _ = make(tmp_path, monkeypatch)
    assert boss.get("/api/admin/overview").json()["money"]["target_markup_percent"] == 50
    put(boss, target_markup_percent=100, gpu_rates='{"*": 60}')
    assert boss.get("/api/admin/overview").json()["money"]["target_markup_percent"] == 100
    from clipcrow.service.gpu_cost import load_rates
    assert load_rates() == {"*": 60.0}


def test_services_show_yes_or_no_and_never_a_value(tmp_path, monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-secret-value")
    monkeypatch.setenv("RAZORPAY_KEY_SECRET", "rzp-secret-value")
    boss, _, _, _ = make(tmp_path, monkeypatch)
    body = boss.get("/api/admin/settings").text
    assert "secret-value" not in body and "sk-ant" not in body
    svc = {x["name"]: x for x in boss.get("/api/admin/settings").json()["services"]}
    assert svc["AI planner (Anthropic)"]["set"] is True and svc["Email"]["set"] is True


def test_changes_are_kept_in_a_short_history_with_who_and_what(tmp_path, monkeypatch):
    boss, _, _, a = make(tmp_path, monkeypatch)
    put(boss, price_per_minute=7)
    put(boss, price_per_minute=6)
    h = boss.get("/api/admin/settings").json()["history"]
    assert [(x["key"], x["old"], x["new"]) for x in h[:2]] == [("price_per_minute", 7, 6), ("price_per_minute", 8.0, 7)]
    assert h[0]["by"] == a.by_email("boss@example.com").id[:8]


def test_a_damaged_settings_file_cannot_put_a_bad_value_in(tmp_path, monkeypatch):
    monkeypatch.setenv("CLIPCROW_STORE_SETTINGS", "files")                                                         # this is about a hand-edited FILE
    (tmp_path / "data").mkdir(parents=True)
    (tmp_path / "data" / "settings.json").write_text(json.dumps({"values": {"price_per_minute": -5, "min_job_minutes": 4, "unknown": 1}}))
    boss, user, _, _ = make(tmp_path, monkeypatch)
    g = user.get("/api/pricing").json()["payg"]
    assert g["price_per_minute"] == 8.0 and g["min_job_minutes"] == 4                                           # the bad one ignored, the good one used


def test_request_models_are_defined_at_module_level():
    """FastAPI cannot resolve a body model defined inside create_app (the annotations are strings): it would be read as a query parameter and every call fails with 422."""
    import re
    from clipcrow.service.api import STATIC
    src = (STATIC.parent / "api.py").read_text(encoding="utf-8")
    assert not re.findall(r"^\s{4,}class \w+\(BaseModel\)", src, re.M)

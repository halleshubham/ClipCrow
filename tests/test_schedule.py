"""Bulk scheduling: reels spread over days at each platform's best times, N posts per day."""
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest

pytest.importorskip("fastapi")
from fastapi.testclient import TestClient

from clipcrow.service.api import create_app
from clipcrow.service.publish import Channel
from clipcrow.service.schedule import (BEST_TIMES, DEFAULT_TZ, Chan, ScheduleError, SchedulePrefs, clean_times, day_slots, plan, zone)
from tests.test_publish import FakeSB
from tests.test_service import FakePipeline, upload, video_bytes, wait  # noqa: F401

IST = ZoneInfo("Asia/Kolkata")
NOW = datetime(2026, 11, 18, 4, 0, tzinfo=timezone.utc)        # 09:30 in Kolkata, a Wednesday
X = Chan("x1", "My X", "x")
YT = Chan("yt1", "My YouTube", "youtube")
LI = Chan("li1", "My LinkedIn", "linkedin")


def local(row):
    return row["local"]


def test_a_platform_posts_at_its_best_times_in_order_and_n_a_day():
    rows = plan([f"c{i}" for i in range(1, 7)], [YT], 2, date(2026, 11, 19), IST, BEST_TIMES, NOW)
    assert [local(r) for r in rows] == ["2026-11-19 13:00", "2026-11-19 18:30", "2026-11-20 13:00", "2026-11-20 18:30", "2026-11-21 13:00", "2026-11-21 18:30"]
    assert [r["clip"] for r in rows] == ["c1", "c2", "c3", "c4", "c5", "c6"]                  # the order given
    assert rows[0]["when"] == "2026-11-19T07:30:00+00:00"                                       # 13:00 IST in UTC


def test_x_gets_its_own_times_and_more_posts_a_day():
    rows = plan([f"c{i}" for i in range(1, 5)], [X], 4, date(2026, 11, 19), IST, BEST_TIMES, NOW)
    assert [local(r)[11:] for r in rows] == ["09:00", "12:30", "18:00", "21:00"]                # the best four, in time order


def test_more_posts_than_best_times_split_the_biggest_gaps_of_the_day():
    assert len(day_slots(["19:00", "12:30"], 6)) == 6
    slots = day_slots(["19:00", "12:30"], 6)
    assert slots == sorted(slots) and all(7 * 60 <= m <= 22 * 60 + 30 for m in slots) and {19 * 60, 12 * 60 + 30} <= set(slots)
    assert min(b - a for a, b in zip(slots, slots[1:])) >= 60                                   # never a burst


def test_past_slots_today_are_skipped_and_the_rest_move_on():
    rows = plan(["c1", "c2", "c3"], [YT], 2, date(2026, 11, 18), IST, BEST_TIMES, NOW)           # now is 09:30: 13:00 and 18:30 are still ahead
    assert [local(r) for r in rows] == ["2026-11-18 13:00", "2026-11-18 18:30", "2026-11-19 13:00"]
    late = datetime(2026, 11, 18, 14, 0, tzinfo=timezone.utc)                                    # 19:30 in Kolkata: both of today's are gone
    rows = plan(["c1", "c2"], [YT], 2, date(2026, 11, 18), IST, BEST_TIMES, late)
    assert [local(r)[:10] for r in rows] == ["2026-11-19", "2026-11-19"]


def test_a_working_days_platform_skips_the_weekend():
    friday = date(2026, 11, 20)
    rows = plan(["c1", "c2", "c3"], [LI], 1, friday, IST, BEST_TIMES, NOW)
    assert [local(r)[:10] for r in rows] == ["2026-11-20", "2026-11-23", "2026-11-24"]          # Friday, Monday, Tuesday


def test_two_channels_of_one_platform_are_staggered_and_each_gets_every_reel():
    rows = plan(["c1", "c2"], [YT, Chan("yt2", "Second", "youtube")], 1, date(2026, 11, 19), IST, BEST_TIMES, NOW)
    by = {(r["channel"], r["clip"]): r["local"] for r in rows}
    assert by[("yt1", "c1")] == "2026-11-19 18:30" and by[("yt2", "c1")] == "2026-11-19 18:45"       # the best single time, then a quarter of an hour later
    assert len(rows) == 4 and {r["clip"] for r in rows} == {"c1", "c2"}


def test_the_time_zone_changes_the_utc_time_not_the_local_one():
    utc = plan(["c1"], [YT], 1, date(2026, 11, 19), ZoneInfo("UTC"), BEST_TIMES, NOW)[0]
    ist = plan(["c1"], [YT], 1, date(2026, 11, 19), IST, BEST_TIMES, NOW)[0]
    assert utc["local"] == ist["local"] and utc["when"] != ist["when"]


def test_bad_inputs_are_refused_with_a_sentence():
    with pytest.raises(ScheduleError):
        plan([], [YT], 1, date(2026, 11, 19), IST, BEST_TIMES, NOW)
    with pytest.raises(ScheduleError):
        plan(["c1"], [YT], 11, date(2026, 11, 19), IST, BEST_TIMES, NOW)
    with pytest.raises(ScheduleError, match="60 days"):
        plan([f"c{i}" for i in range(70)], [YT], 1, date(2026, 11, 19), IST, BEST_TIMES, NOW)
    for bad in (["25:00"], ["9:00"], [], ["12:00"] * 13, "18:30"):
        with pytest.raises(ScheduleError):
            clean_times(bad)
    with pytest.raises(ScheduleError):
        zone("Mars/Olympus")


def test_prefs_save_a_zone_and_times_and_fall_back_to_the_defaults(tmp_path):
    p = SchedulePrefs(tmp_path)
    assert p.get("shared")["timezone"] == DEFAULT_TZ and p.get("shared")["times"]["x"] == BEST_TIMES["x"]
    got = p.put("shared", "Asia/Dubai", {"x": ["08:00", "20:00"]})
    assert got["timezone"] == "Asia/Dubai" and got["times"]["x"] == ["08:00", "20:00"] and got["times"]["youtube"] == BEST_TIMES["youtube"]
    with pytest.raises(ScheduleError):
        p.put("shared", "Asia/Dubai", {"myspace": ["08:00"]})
    with pytest.raises(KeyError):
        p.get("../x")
    p.delete_all("shared")
    assert p.get("shared")["timezone"] == DEFAULT_TZ


# --- the API ----------------------------------------------------------------------------------------------------

class SB(FakeSB):
    def channels(self):
        return super().channels() + [Channel("ch-x", "My X", "x")]


def setup(tmp_path, video_bytes):
    sb = SB()
    c = TestClient(create_app(tmp_path / "data", FakePipeline(), sociobird=sb))
    job = wait(c, upload(c, video_bytes).json()["id"])
    return c, sb, job["id"], [x["id"] for x in job["clips"]]


def settle(c, jid, timeout=10):
    import time
    end = time.time() + timeout
    while time.time() < end:
        rows = c.get(f"/api/jobs/{jid}/posts").json()
        if rows and all(r["status"] != "queued" for r in rows):
            return rows
        time.sleep(0.05)
    raise AssertionError(rows)


def test_a_dry_run_shows_the_plan_and_queues_nothing(tmp_path, video_bytes):
    c, sb, jid, clips = setup(tmp_path, video_bytes)
    r = c.post(f"/api/jobs/{jid}/publish/bulk", json={"clips": clips[:2], "channels": ["ch-x", "ch-yt"], "per_day": 2, "dry_run": True})
    assert r.status_code == 202 and r.json()["dry_run"] is True and r.json()["count"] == 4
    assert {p["platform"] for p in r.json()["posts"]} == {"x", "youtube"} and all(p["title"] for p in r.json()["posts"])
    assert c.get(f"/api/jobs/{jid}/posts").json() == [] and sb.uploaded == []


def test_scheduling_uploads_each_reel_once_and_posts_every_pair_at_its_time(tmp_path, video_bytes):
    c, sb, jid, clips = setup(tmp_path, video_bytes)
    assert len(clips) >= 2
    r = c.post(f"/api/jobs/{jid}/publish/bulk", json={"clips": clips[:2], "channels": ["ch-x", "ch-yt", "ch-ig"], "per_day": 1, "suffix": "#झोप\nhttps://example.com/full"})
    assert r.status_code == 202 and r.json()["count"] == 6
    rows = settle(c, jid)
    assert len(rows) == 6 and {x["status"] for x in rows} == {"scheduled"} and all(x["when"] for x in rows)
    assert sorted(sb.uploaded) == sorted(f"{cid}.mp4" for cid in clips[:2])                       # one upload per reel, three channels each
    whens = [w for *_, w in sb.posted]
    assert all(isinstance(w, datetime) and w > datetime.now(timezone.utc) for w in whens)
    by_platform = {}
    for ch, content, settings, when in sb.posted:
        by_platform.setdefault(settings["__type"], []).append(when)
        assert "#झोप" in content and "https://example.com/full" in content and "<script" not in content
    assert len({tuple(v) for v in by_platform.values()}) == 3                                    # each platform has its own times
    one_day = {(w.astimezone(ZoneInfo(DEFAULT_TZ)).date()) for w in by_platform["youtube"]}
    assert len(one_day) == 2                                                                      # one a day: two reels, two days


def test_the_request_is_checked_before_anything_is_queued(tmp_path, video_bytes):
    c, sb, jid, clips = setup(tmp_path, video_bytes)
    base = {"clips": clips[:1], "channels": ["ch-x"], "per_day": 1}
    for bad in ({**base, "clips": ["c99"]}, {**base, "clips": [clips[0], clips[0]]}, {**base, "channels": ["ch-nope"]}, {**base, "per_day": 11}, {**base, "per_day": 0},
                {**base, "timezone": "Mars/Base"}, {**base, "start": "2020-01-01"}, {**base, "start": "tomorrow"}, {**base, "times": {"x": ["25:99"]}},
                {**base, "times": {"myspace": ["10:00"]}}, {**base, "clips": []}, {**base, "channels": []}, {**base, "clips": ["../x"]}, {**base, "suffix": "x" * 301}):
        assert c.post(f"/api/jobs/{jid}/publish/bulk", json=bad).status_code in (404, 422), bad
    assert c.get(f"/api/jobs/{jid}/posts").json() == [] and sb.uploaded == []


def test_one_failing_channel_and_a_failing_upload_are_reported_per_post(tmp_path, video_bytes):
    from clipcrow.service.publish import PublishError
    sb = SB()
    sb.fail_channel = "ch-ig"
    c = TestClient(create_app(tmp_path / "data", FakePipeline(), sociobird=sb))
    jid = wait(c, upload(c, video_bytes).json()["id"])["id"]
    clips = [x["id"] for x in c.get(f"/api/jobs/{jid}").json()["clips"]]
    c.post(f"/api/jobs/{jid}/publish/bulk", json={"clips": clips[:1], "channels": ["ch-yt", "ch-ig"], "per_day": 1})
    got = {r["platform"]: r for r in settle(c, jid)}
    assert got["youtube"]["status"] == "scheduled" and got["instagram"]["status"] == "failed" and "refused" in got["instagram"]["detail"]


def test_the_best_times_can_be_saved_and_a_bulk_request_can_override_them(tmp_path, video_bytes):
    c, sb, jid, clips = setup(tmp_path, video_bytes)
    assert c.get("/api/publish/schedule/prefs").json()["timezone"] == DEFAULT_TZ
    assert c.put("/api/publish/schedule/prefs", json={"timezone": "Asia/Kolkata", "times": {"x": ["06:00", "23:00"]}}).json()["times"]["x"] == ["06:00", "23:00"]
    assert c.put("/api/publish/schedule/prefs", json={"timezone": "Nowhere/Land", "times": {}}).status_code == 422
    r = c.post(f"/api/jobs/{jid}/publish/bulk", json={"clips": clips[:2], "channels": ["ch-x"], "per_day": 2, "dry_run": True, "start": (datetime.now(timezone.utc) + timedelta(days=3)).strftime("%Y-%m-%d")})
    assert [p["local"][11:] for p in r.json()["posts"]] == ["06:00", "23:00"]                      # the saved times
    r = c.post(f"/api/jobs/{jid}/publish/bulk", json={"clips": clips[:2], "channels": ["ch-x"], "per_day": 2, "dry_run": True, "times": {"x": ["10:00", "14:00"]},
                                                       "start": (datetime.now(timezone.utc) + timedelta(days=3)).strftime("%Y-%m-%d")})
    assert [p["local"][11:] for p in r.json()["posts"]] == ["10:00", "14:00"]                      # this request's own


def test_without_publishing_configured_the_bulk_routes_are_absent(tmp_path, video_bytes):
    c = TestClient(create_app(tmp_path / "data", FakePipeline()))
    jid = wait(c, upload(c, video_bytes).json()["id"])["id"]
    assert c.post(f"/api/jobs/{jid}/publish/bulk", json={"clips": ["c1"], "channels": ["x"], "dry_run": True}).status_code == 404
    assert c.get("/api/publish/schedule/prefs").status_code == 404

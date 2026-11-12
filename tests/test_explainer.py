"""Explainer mode: pictures over the footage. Untrusted links, AI images, failures that must not fail a job."""
import base64
import http.server
import json
import re
import socket
import subprocess
import threading
from pathlib import Path

import pytest
from pydantic import ValidationError

from clipcrow.assets.fetch import fetch_assets
from clipcrow.assets.images import GeminiImages, ImageError, NotConfigured, sniff
from clipcrow.assets.screenshot import CaptureError, capture_page
from clipcrow.assets.urlguard import UnsafeURL, assert_public_url
from clipcrow.compile import compile_clip
from clipcrow.edl import EDL, EDLError
from clipcrow.planner import FakeLLM, LLMError, PlannerOptions, plan_edl
from clipcrow.planner.explainer import plan_beats
from clipcrow.planner.models import BeatsResult
from clipcrow.planner.transcript import Sentence
from tests.conftest import base_edl
from tests.test_planner import SRC, make_transcript, scripted

PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 2000


def resolver(ip):
    return lambda host, port, type=None: [(socket.AF_INET6 if ":" in ip else socket.AF_INET, socket.SOCK_STREAM, 6, "", (ip, port))]


# --- the link guard --------------------------------------------------------------------------------

def test_a_public_address_passes_and_is_returned():
    assert assert_public_url("https://news.example/story?id=1", resolver=resolver("93.184.216.34")) == "https://news.example/story?id=1"


@pytest.mark.parametrize("ip", ["10.0.0.5", "127.0.0.1", "169.254.169.254", "192.168.1.9", "172.16.0.1", "100.64.0.1",
                                "0.0.0.0", "::1", "fe80::1", "fc00::1", "::ffff:10.0.0.1"])
def test_addresses_that_point_inside_a_network_are_refused(ip):
    with pytest.raises(UnsafeURL, match="private network"):
        assert_public_url("https://sneaky.example/", resolver=resolver(ip))


def test_one_private_answer_among_public_ones_is_enough_to_refuse():
    mixed = lambda host, port, type=None: [(2, 1, 6, "", ("93.184.216.34", port)), (2, 1, 6, "", ("10.1.1.1", port))]  # noqa: E731
    with pytest.raises(UnsafeURL):
        assert_public_url("https://rebind.example/", resolver=mixed)


@pytest.mark.parametrize("url", ["ftp://x.example/a", "file:///etc/passwd", "javascript:alert(1)", "//x.example/",
                                 "https://user:pw@x.example/", "https://x.example:8080/", "https://x.example/a b",
                                 "https://x.example/a\nb", "", "https://", "https://x.example/" + "a" * 2100])
def test_odd_schemes_credentials_ports_and_junk_are_refused(url):
    with pytest.raises(UnsafeURL):
        assert_public_url(url, resolver=resolver("93.184.216.34"))


def test_a_name_that_does_not_resolve_is_refused_politely():
    def nope(host, port, type=None):
        raise socket.gaierror("no such host")
    with pytest.raises(UnsafeURL, match="could not be found"):
        assert_public_url("https://nothing.invalid/", resolver=nope)


# --- AI images ---------------------------------------------------------------------------------------

def gemini_reply(data=PNG):
    return {"candidates": [{"content": {"parts": [{"text": "ok"}, {"inlineData": {"mimeType": "image/png",
                                                                                   "data": base64.b64encode(data).decode()}}]}}]}


def test_an_image_is_written_with_the_right_extension_and_the_key_never_goes_in_the_url(tmp_path):
    seen = {}

    def post(url, headers, body):
        seen.update(url=url, headers=headers, body=json.loads(body))
        return gemini_reply()
    g = GeminiImages(api_key="sekret", model="m1", post=post)
    out = g.generate("a glass jar of coins\x00\x07 " + "x" * 900, tmp_path / "a.png")
    assert out.suffix == ".png" and out.read_bytes() == PNG
    assert "sekret" not in seen["url"] and seen["headers"]["x-goog-api-key"] == "sekret" and "m1" in seen["url"]
    text = seen["body"]["contents"][0]["parts"][0]["text"]
    assert "\x00" not in text and len(text) < 800                                 # control characters out, length bounded


def test_image_generation_failures_are_clear_and_never_leak_details(tmp_path):
    with pytest.raises(NotConfigured):
        GeminiImages(api_key="", post=lambda *a: gemini_reply()).generate("x" * 20, tmp_path / "a")
    for reply in ({}, {"candidates": []}, {"candidates": [{"content": {"parts": [{"text": "I cannot"}]}}]},
                  gemini_reply(b"<html>not an image</html>" * 100), gemini_reply(b"\x89PNG\r\n\x1a\n" + b"0" * 20_000_000)):
        with pytest.raises(ImageError):
            GeminiImages(api_key="k", post=lambda *a, r=reply: r).generate("a scene", tmp_path / "b")
    bad_b64 = {"candidates": [{"content": {"parts": [{"inlineData": {"data": "!!!not base64!!!"}}]}}]}
    with pytest.raises(ImageError):
        GeminiImages(api_key="k", post=lambda *a: bad_b64).generate("a scene", tmp_path / "c")


def test_sniff_knows_three_image_types_and_nothing_else():
    assert sniff(PNG) == ".png" and sniff(b"\xff\xd8\xff\xe0" + b"0" * 10) == ".jpg" and sniff(b"RIFF\0\0\0\0WEBPVP8 ") == ".webp"
    assert sniff(b"<svg onload=alert(1)>") is None and sniff(b"GIF89a") is None


# --- the EDL and the compiler ----------------------------------------------------------------------------

def explainer_edl(uri="assets/p1.png", kind="image", extra_assets=(), **overlay):
    ov = {"kind": "media", "id": "pic1", "t_in": 1.0, "duration": 2.5, "asset": "a1", "caption": "तुमचे पैसे", **overlay}
    return EDL.model_validate(base_edl(
        source={"uri": "src.mp4", "duration": 600, "width": 1920, "height": 1080, "fps": 30},
        assets=[{"id": "a1", "kind": kind, "origin": "capture", "ai": "none", "source_url": "https://news.example/s", "uri": uri},
                *extra_assets],
        clips=[{"id": "c1", "source_in": 0, "source_out": 10, "overlays": [ov]}]))


def test_a_media_overlay_must_point_at_an_image_or_video_asset_of_the_edl():
    explainer_edl()
    with pytest.raises(ValidationError, match="needs an image or video"):
        explainer_edl(kind="audio")
    with pytest.raises(ValidationError, match="not in the EDL"):
        explainer_edl(asset="nope")
    with pytest.raises(ValidationError):
        explainer_edl(placement="inline")
    with pytest.raises(ValidationError):
        explainer_edl(caption="x" * 100)


def test_ai_images_make_the_disclosure_flag_come_on_by_itself():
    from clipcrow.provenance import provenance
    edl = explainer_edl(extra_assets=[{"id": "g1", "kind": "image", "origin": "ai_image", "ai": "generated", "prompt": "a jar"}])
    assert provenance(edl, edl.clips[0])["disclosure_recommended"] is True
    assert provenance(explainer_edl(), explainer_edl().clips[0])["disclosure_recommended"] is False
    with pytest.raises(ValidationError, match="ai=.generated"):                  # an AI image cannot be labelled as not AI
        EDL.model_validate(base_edl(assets=[{"id": "g", "kind": "image", "origin": "ai_image", "ai": "none"}]))


def lavfi(path: Path):
    subprocess.run(["ffmpeg", "-y", "-v", "error", "-f", "lavfi", "-i", "testsrc=s=1280x720:r=30:d=3", "-pix_fmt", "yuv420p", str(path)], check=True)


def test_the_picture_is_shipped_in_the_project_and_shown_with_a_move_and_a_caption(tmp_path, gsap, fonts_available):
    lavfi(tmp_path / "src.mp4")
    (tmp_path / "out" / "assets").mkdir(parents=True)
    (tmp_path / "out" / "assets" / "p1.png").write_bytes(PNG)
    edl = explainer_edl(placement="panel", motion="zoom_in")
    edl = edl.model_copy(update={"source": edl.source.model_copy(update={"duration": 3, "width": 1280, "height": 720}),
                                 "clips": [edl.clips[0].model_copy(update={"source_out": 2.0})]})
    res = compile_clip(edl, edl.clips[0], tmp_path / "p", media_root=tmp_path, gsap_js=gsap, asset_root=tmp_path / "out")
    assert "media" in res.scenes
    assert (tmp_path / "p" / "assets" / "media" / "a1.png").read_bytes() == PNG
    html = (tmp_path / "p" / "compositions" / "media.html").read_text()
    assert 'src="assets/media/a1.png"' in html and "तुमचे पैसे" in html and "scale: 1.12" in html and 'class="mc panel clip"' in html
    index = (tmp_path / "p" / "index.html").read_text()
    assert index.index('data-composition-id="media"') < index.index('data-composition-id="captions"')


def test_a_picture_that_is_missing_unsupported_or_outside_the_root_is_an_error_not_a_hole(tmp_path, gsap, fonts_available):
    lavfi(tmp_path / "src.mp4")
    (tmp_path / "out").mkdir()
    (tmp_path / "secret.png").write_bytes(PNG)

    def go(uri, kind="image"):
        edl = explainer_edl(uri=uri, kind=kind)
        edl = edl.model_copy(update={"source": edl.source.model_copy(update={"duration": 3, "width": 1280, "height": 720}),
                                     "clips": [edl.clips[0].model_copy(update={"source_out": 2.0})]})
        return compile_clip(edl, edl.clips[0], tmp_path / "p", media_root=tmp_path, gsap_js=gsap, asset_root=tmp_path / "out")
    with pytest.raises(EDLError, match="no file yet"):
        go("")
    with pytest.raises(EDLError, match="not a supported picture type"):
        go("x.svg")
    with pytest.raises(EDLError, match="only images"):
        go("x.mp4", kind="video")
    with pytest.raises(EDLError):
        go("../secret.png")                                                   # one level above the assets folder
    with pytest.raises(EDLError):
        go("/etc/passwd.png")


# --- fetching -----------------------------------------------------------------------------------------------

def wants(extra_clip_overlays=True):
    assets = [{"id": "c1-pic1", "kind": "image", "origin": "capture", "ai": "none", "source_url": "https://news.example/a"},
              {"id": "c1-pic2", "kind": "image", "origin": "ai_image", "ai": "generated", "prompt": "a jar of coins"}]
    ov = [{"kind": "media", "id": "pic1", "t_in": 3, "duration": 3, "asset": "c1-pic1"},
          {"kind": "media", "id": "pic2", "t_in": 8, "duration": 3, "asset": "c1-pic2"},
          {"kind": "hook", "id": "hook", "t_in": 0.3, "text": "हुक"}]
    return EDL.model_validate(base_edl(
        source={"uri": "src.mp4", "duration": 600, "width": 1920, "height": 1080, "fps": 30},
        assets=assets, clips=[{"id": "c1", "source_in": 0, "source_out": 15, "overlays": ov, "ai": {"decided": ["title"]}}]))


class FakeImages:
    name, model = "fake-img", "fake-1"

    def __init__(self, fail=None):
        self.fail, self.calls = fail, []

    def generate(self, prompt, dest, *, aspect="9:16"):
        self.calls.append(prompt)
        if self.fail:
            raise self.fail
        out = Path(dest).with_suffix(".png")
        out.write_bytes(PNG)
        return out


def test_pictures_are_fetched_into_the_output_folder_and_recorded(tmp_path):
    seen = []

    def capture(url, dest):
        seen.append(url)
        Path(dest).write_bytes(PNG)
        return Path(dest)
    edl, warns = fetch_assets(wants(), tmp_path / "out", images=FakeImages(), capture_fn=capture)
    assert warns == [] and seen == ["https://news.example/a"]
    a = {x.id: x for x in edl.assets}
    assert a["c1-pic1"].uri == "assets/c1-pic1.png" and a["c1-pic1"].tool == "page-screenshot"
    assert a["c1-pic2"].uri == "assets/c1-pic2.png" and (a["c1-pic2"].tool, a["c1-pic2"].model) == ("fake-img", "fake-1")
    assert (tmp_path / "out" / "assets" / "c1-pic1.png").is_file()
    again, _ = fetch_assets(edl, tmp_path / "out", images=None, capture_fn=lambda *a: (_ for _ in ()).throw(AssertionError("refetched")))
    assert again.assets == edl.assets                                          # nothing is fetched twice


def test_a_failed_picture_is_dropped_with_a_note_and_never_fails_the_job(tmp_path):
    def boom(url, dest):
        raise CaptureError("That address points inside a private network, which is not allowed.")
    edl, warns = fetch_assets(wants(), tmp_path / "out", images=FakeImages(fail=NotConfigured("image generation is not configured")),
                              capture_fn=boom)
    assert {a.id for a in edl.assets} == set()
    kinds = [o.kind for o in edl.clips[0].overlays]
    assert kinds == ["hook"]                                                   # both picture overlays are gone, the hook stays
    assert len(warns) == 2 and all(w.startswith("a picture was skipped") for w in warns)
    assert edl.clips[0].ai.notes == warns


def test_a_surprise_exception_is_hidden_behind_a_fixed_sentence(tmp_path):
    def boom(url, dest):
        raise RuntimeError("/srv/secret/path token=abc")
    edl, warns = fetch_assets(wants(), tmp_path / "out", images=FakeImages(), capture_fn=boom)
    assert warns == ["a picture was skipped: the picture could not be made"]
    assert "secret" not in json.dumps(warns) and "secret" not in edl.clips[0].ai.notes[0]
    assert [a.id for a in edl.assets] == ["c1-pic2"]                           # the other picture still arrived


# --- choosing the beats -----------------------------------------------------------------------------------------

def sentences(n=8, length=4.0, start=100.0):
    return [Sentence(id=i, text=f"वाक्य {i}", start=start + i * length, end=start + (i + 1) * length, first=i * 5, last=i * 5 + 4)
            for i in range(n)]


def beats_llm(beats):
    return FakeLLM(lambda purpose, prompt, schema: BeatsResult(beats=beats))


def beat(sentence, kind="screenshot", link=0, prompt="", caption="बातमी"):
    return dict(sentence=sentence, kind=kind, link=link, prompt=prompt, caption=caption)


def run_beats(raw, links=("https://a.example/1", "https://b.example/2"), allow_images=False, hook_end=2.8):
    return plan_beats(beats_llm(raw), sentences(), 100.0, 32.0, list(links), allow_images=allow_images, language="mr",
                      hook_end=hook_end, purpose="beats:c1")


def test_beats_land_on_their_sentence_away_from_the_hook_with_room_between():
    plans, warns = run_beats([beat(1, link=0), beat(2, link=1), beat(4, link=1), beat(6, link=0)])
    assert warns == []
    assert [p.t for p in plans] == [4.0, 16.0] or [p.t for p in plans] == [4.0, 12.0, 20.0][:len(plans)]
    for a, b in zip(plans, plans[1:]):
        assert b.t >= a.t + a.duration + 1.0 - 1e-9
    assert all(p.t >= 2.8 and p.kind == "capture" for p in plans) and len(plans) <= 3
    links = [p.link for p in plans]
    assert len(links) == len(set(links))                                        # a page is not shown twice in a clip


def test_what_the_model_cannot_choose_is_ignored():
    raw = [beat(0), beat(7),                                                    # first and last sentence are left alone
           beat(2, link=9), beat(3, link=-1), beat(4, link=None),               # a link that is not in the list
           beat(5, kind="image", prompt="a jar of coins"),                      # images not allowed here
           beat(6, kind="video"), beat(3, kind="none"), beat(99)]               # unknown kinds and sentences
    plans, _ = run_beats(raw, allow_images=False)
    assert plans == []


def test_generated_images_need_permission_and_a_real_description():
    ok, _ = run_beats([beat(2, kind="image", link=None, prompt="a glass jar full of coins on a table")], allow_images=True)
    assert len(ok) == 1 and ok[0].kind == "ai_image" and ok[0].prompt.startswith("a glass jar")
    none, _ = run_beats([beat(2, kind="image", link=None, prompt="x")], allow_images=True)
    assert none == []
    none, _ = run_beats([beat(2, kind="image", link=None, prompt="a glass jar of coins")], allow_images=False)
    assert none == []


def test_a_refusal_or_nothing_to_show_is_not_an_error():
    llm = FakeLLM(lambda p, u, s: LLMError("the model declined this request"))
    plans, warns = plan_beats(llm, sentences(), 100.0, 32.0, ["https://a.example/1"], allow_images=False, language="mr",
                              hook_end=2.8, purpose="beats:c1")
    assert plans == [] and warns and "declined" in warns[0]
    silent = FakeLLM(lambda p, u, s: pytest.fail("no links and no images: the model must not be asked"))
    assert plan_beats(silent, sentences(), 100.0, 32.0, [], allow_images=False, language="mr", hook_end=2.8, purpose="x") == ([], [])


def test_the_prompt_lists_links_by_number_and_fences_the_transcript():
    llm = beats_llm([])
    plan_beats(llm, sentences(), 100.0, 32.0, ["https://a.example/1", "https://b.example/2"], allow_images=True, language="mr",
               hook_end=2.8, purpose="beats:c1")
    prompt = llm.calls[0][1]
    assert "[L0] https://a.example/1" in prompt and "[L1] https://b.example/2" in prompt
    assert "<transcript>" in prompt and "never invent an address" in prompt and "Devanagari" in prompt


# --- the whole plan ---------------------------------------------------------------------------------------------------

def explain_model():
    base = scripted()

    def respond(purpose, prompt, schema):
        if purpose.startswith("beats:"):
            ids = [int(x) for x in re.findall(r"\[S(\d+) \|", prompt)]
            return BeatsResult(beats=[beat(ids[1], link=0, caption="पहिली बातमी"),
                                      beat(ids[3], kind="image", link=None, prompt="a glass jar full of coins", caption="बचत")])
        return base.respond(purpose, prompt, schema)
    return FakeLLM(respond)


def plan(**kw):
    opts = PlannerOptions(target_clips=1, workers=1, explainer=True, links=("https://news.example/a",), **kw)
    return plan_edl(make_transcript(), SRC, explain_model(), opts).edl


def test_an_explainer_plan_has_pictures_with_honest_provenance():
    edl = plan(ai_images=True)
    c = edl.clips[0]
    media = [o for o in c.overlays if o.kind == "media"]
    assert len(media) == 2 and "picture choices" in c.ai.decided
    a = {x.id: x for x in edl.assets}
    shot, gen = (a[o.asset] for o in media)
    assert (shot.origin, shot.ai, shot.source_url) == ("capture", "none", "https://news.example/a")
    assert (gen.origin, gen.ai) == ("ai_image", "generated") and gen.prompt.startswith("a glass jar")
    assert next(o for o in media if o.asset == shot.id).placement == "panel"
    assert next(o for o in media if o.asset == gen.id).placement == "full"
    assert all(x.uri == "" for x in edl.assets if x.origin in ("capture", "ai_image"))     # nothing fetched at plan time


def test_without_permission_there_are_no_generated_images_and_no_disclosure():
    from clipcrow.provenance import provenance
    edl = plan(ai_images=False)
    assert [a.origin for a in edl.assets if a.origin in ("capture", "ai_image")] == ["capture"]
    assert provenance(edl, edl.clips[0])["disclosure_recommended"] is False


def test_an_ordinary_plan_asks_the_model_nothing_about_pictures():
    llm = scripted()
    plan_edl(make_transcript(), SRC, llm, PlannerOptions(target_clips=1, workers=1))
    assert not any(p.startswith("beats:") for p, _ in llm.calls)


# --- a real browser, a real page, and a page that tries to reach inside -----------------------------------------------

def test_a_page_is_captured_and_its_requests_to_private_addresses_are_blocked(tmp_path):
    pytest.importorskip("playwright")
    hits = []

    class H(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            hits.append(self.path)
            if self.path == "/secret.png":
                self.send_response(200); self.send_header("Content-Type", "image/png"); self.end_headers(); self.wfile.write(PNG)
                return
            body = (b"<html><body style='margin:0;background:#2e7d32'><h1 style='color:#fff'>News</h1>"
                    b"<img src='http://internal.test:%d/secret.png' width=100 height=100></body></html>" % self.server.server_port)
            self.send_response(200); self.send_header("Content-Type", "text/html"); self.end_headers(); self.wfile.write(body)

        def log_message(self, *a):
            pass
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), H)
    port = srv.server_port
    threading.Thread(target=srv.serve_forever, daemon=True).start()

    def dns(host, p, type=None):
        ip = "10.0.0.5" if host == "internal.test" else "93.184.216.34"
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (ip, p))]
    try:
        out = capture_page(f"http://news.test:{port}/", tmp_path / "shot.png", resolver=dns, ports=(port,), width=400, height=300,
                           chrome_args=(f"--host-resolver-rules=MAP news.test 127.0.0.1,MAP internal.test 127.0.0.1",))
    finally:
        srv.shutdown()
    import cv2
    img = cv2.imread(str(out))
    assert img.shape[:2] == (300, 400)
    b, g, r = img[250, 350]
    assert g > r and g > b                                                       # the page's green background is in the picture
    assert "/" in hits and "/secret.png" not in hits                             # the sub-request to a private host never left the browser


def test_capturing_refuses_a_private_address_before_any_browser_starts(tmp_path):
    started = []
    with pytest.raises(CaptureError, match="private network"):
        capture_page("http://10.0.0.1/", tmp_path / "x.png", resolver=resolver("10.0.0.1"),
                     playwright_factory=lambda: started.append(1))
    assert started == []


# --- through make() and the service --------------------------------------------------------------------------------------

from clipcrow.make import Hooks, make  # noqa: E402
from tests.test_service import FakePipeline, client, upload, video_bytes, wait  # noqa: E402,F401


def test_make_fetches_the_pictures_between_planning_and_rendering(tmp_path, video_bytes):
    video = tmp_path / "talk.mp4"
    video.write_bytes(video_bytes)
    events, rendered = [], []

    def capture(url, dest):
        Path(dest).write_bytes(PNG)
        return Path(dest)

    def fake_render(edl, clip_id, path, **kw):
        c = edl.clips[0]
        rendered.append((kw["asset_root"], [a.uri for a in edl.assets], [o.kind for o in c.overlays]))
        Path(path).write_bytes(b"mp4")
        return path, None
    opts = PlannerOptions(target_clips=1, workers=1, explainer=True, ai_images=True, links=("https://news.example/a",))
    res = make(video, tmp_path / "out", llm=explain_model(), opts=opts, images=FakeImages(), capture_fn=capture,
               transcribe_fn=lambda m, language=None, progress=None: make_transcript(), render_fn=fake_render,
               hooks=Hooks(stage=lambda n, s, d="": events.append((n, s))))
    assert ("assets", "running") in events and ("assets", "done") in events
    assert events.index(("assets", "done")) < events.index(("frame", "running"))
    uris = [a.uri for a in res.edl.assets if a.origin in ("capture", "ai_image")]
    assert len(uris) == 2 and all(u.startswith("assets/") for u in uris)
    assert (tmp_path / "out" / uris[0]).is_file()
    assert rendered and rendered[0][0] == tmp_path / "out"                      # the renderer resolves pictures in the output folder
    assert load_back(tmp_path / "out" / "edl.json").assets[-1].uri.startswith("assets/")


def load_back(path):
    from clipcrow.edl import load_edl
    return load_edl(path)


def test_a_plan_without_pictures_skips_the_stage(tmp_path, video_bytes):
    video = tmp_path / "talk.mp4"
    video.write_bytes(video_bytes)
    events = []
    make(video, tmp_path / "out", llm=scripted(), opts=PlannerOptions(target_clips=1, workers=1), render=False,
         transcribe_fn=lambda m, language=None, progress=None: make_transcript(),
         hooks=Hooks(stage=lambda n, s, d="": events.append((n, s))))
    assert ("assets", "skipped") in events


def post(c, video_bytes, **form):
    return upload(c, video_bytes, **form)


def test_links_are_checked_when_the_job_is_submitted(tmp_path, video_bytes):
    c, _ = client(tmp_path)
    r = post(c, video_bytes, explainer="true", links="http://10.0.0.1/admin")
    assert r.status_code == 422 and "private network" in r.json()["detail"] and "Link 1" in r.json()["detail"]
    r = post(c, video_bytes, explainer="true", links="\n".join(f"http://93.184.216.{i}/" for i in range(1, 8)))
    assert r.status_code == 422 and "at most 6" in r.json()["detail"]
    r = post(c, video_bytes, explainer="true", links="ftp://93.184.216.34/x")
    assert r.status_code == 422
    assert c.get("/api/jobs").json() == []                                       # nothing was created for a refused submit


def test_ai_images_are_only_accepted_when_the_server_can_make_them(tmp_path, video_bytes):
    c, _ = client(tmp_path)
    assert c.get("/api/config").json()["ai_images"] is False
    r = post(c, video_bytes, explainer="true", ai_images="true")
    assert r.status_code == 422 and "not set up" in r.json()["detail"]
    pipe = FakePipeline()
    pipe.can_generate_images = True
    c2, _ = client(tmp_path / "b", pipe)
    assert c2.get("/api/config").json()["ai_images"] is True
    r = post(c2, video_bytes, explainer="true", ai_images="true", links="http://93.184.216.34/story")
    assert r.status_code == 201
    p = r.json()["params"]
    assert p["explainer"] is True and p["ai_images"] is True and p["links"] == ["http://93.184.216.34/story"]
    wait(c2, r.json()["id"])                                                      # let the background job end before the test does


def test_the_editor_can_remove_a_picture_and_an_unused_ai_image_stops_counting(tmp_path):
    from clipcrow.provenance import provenance
    from clipcrow.service.editor import ClipEdit, apply_edit, clip_view
    edl = wants().model_copy()
    edl = edl.model_copy(update={"assets": [a.model_copy(update={"uri": f"assets/{a.id}.png"}) for a in edl.assets]})
    assert provenance(edl, edl.clips[0])["disclosure_recommended"] is True
    view = clip_view(edl, "c1", source_duration=600)
    assert [p["id"] for p in view["pictures"]] == ["pic1", "pic2"] and view["pictures"][1]["ai"] == "generated"
    new, _ = apply_edit(edl, "c1", ClipEdit(drop_pictures=["pic2"]), source_duration=600)
    assert [o.id for o in new.clips[0].overlays if o.kind == "media"] == ["pic1"]
    assert [a.id for a in new.assets] == ["c1-pic1"]                              # the AI image asset went with its overlay
    assert provenance(new, new.clips[0])["disclosure_recommended"] is False
    assert new.clips[0].ai.notes == ["edited by a person: pictures"]
    with pytest.raises(ValueError):
        apply_edit(edl, "c1", ClipEdit(drop_pictures=["../x"]), source_duration=600)

import json
import os
import shutil
import sys
from pathlib import Path

import pytest

from clipcrow.edl import EDL

ROOT = Path(__file__).resolve().parent.parent
GSAP = ROOT / "vendor" / "gsap.min.js"


def base_edl(**over) -> dict:
    d = {
        "job_id": "t",
        "source": {"uri": "src.mp4", "duration": 20, "width": 1080, "height": 1920, "fps": 30},
        "language": {"primary": "mr", "script": "Deva"},
        "transcript": [
            {"w": "क्षणात", "s": 1.0, "e": 1.5}, {"w": "बदलणारे", "s": 1.5, "e": 2.0},
            {"w": "आयुष्य.", "s": 2.0, "e": 2.6}, {"w": "ज्ञान", "s": 3.5, "e": 4.0},
            {"w": "शक्ती", "s": 4.0, "e": 4.5},
        ],
        "clips": [{"id": "c1", "source_in": 0, "source_out": 10}],
    }
    d.update(over)
    return d


@pytest.fixture
def edl_dict():
    return base_edl()


@pytest.fixture
def make_edl():
    return lambda **over: EDL.model_validate(base_edl(**over))


@pytest.fixture
def gsap(tmp_path):
    """A stand-in gsap file so HTML-only compiles do not need the vendored copy."""
    p = tmp_path / "gsap.min.js"
    p.write_text("/* test */")
    return p


@pytest.fixture
def fonts_available():
    from clipcrow import fonts
    try:
        fonts.resolve_font("Noto Serif Devanagari", 700)
        fonts.resolve_font("Noto Serif", 700)
    except fonts.FontNotFound:
        pytest.skip("Noto Serif Devanagari / Noto Serif are not installed (fonts-noto-core)")


# --- an S3-compatible server on localhost (moto) for the storage and direct-upload tests ------------------------------

@pytest.fixture(scope="module")
def endpoint():
    import socket
    import time
    pytest.importorskip("boto3")
    pytest.importorskip("moto")
    from moto.server import ThreadedMotoServer
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    server = ThreadedMotoServer(port=port, verbose=False)
    server.start()
    yield f"http://127.0.0.1:{port}"
    server.stop()


_n = [0]


@pytest.fixture
def storage(endpoint):
    import time
    import boto3
    from clipcrow.service.storage import S3Storage
    _n[0] += 1
    bucket = f"bucket-{_n[0]}-{int(time.time())}"
    boto3.client("s3", endpoint_url=endpoint, region_name="us-east-1", aws_access_key_id="k", aws_secret_access_key="s").create_bucket(Bucket=bucket)  # the app itself uses region "auto"
    return S3Storage(bucket, endpoint=endpoint, access_key="k", secret_key="s", prefix="cc")




# The service tests start daemon threads (a worker polling a test server, an abandoned AI call) that can still be running when pytest finishes. If one touches stderr
# while Python finalises, the interpreter ABORTS (exit 134) AFTER every test passed (seen in CI: "774 passed", then "Fatal Python error: _enter_buffered_busy").
# So, once pytest has printed its result, leave without the finalisation race, keeping the exit status. CLIPCROW_NO_FAST_EXIT=1 turns it off.
def pytest_sessionfinish(session, exitstatus):
    session.config._clipcrow_exit = int(exitstatus)


@pytest.hookimpl(trylast=True)
def pytest_unconfigure(config):
    code = getattr(config, "_clipcrow_exit", None)
    if code is None or os.environ.get("CLIPCROW_NO_FAST_EXIT"):
        return
    sys.stdout.flush()
    sys.stderr.flush()
    os._exit(code)


os.environ.setdefault("CLIPCROW_SPEAKERS", "0")       # no voice model is ever downloaded by a test; the speaker tests pass their own embedder

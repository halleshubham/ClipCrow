"""The command line: every subcommand builds its parser, and the small ones run."""
import pytest

from clipcrow.cli import main

COMMANDS = ["schema", "validate", "compile", "render", "plan", "make", "transcribe", "usage", "serve", "selftest", "r2-cors"]


@pytest.mark.parametrize("cmd", COMMANDS)
def test_every_subcommand_builds_its_parser_and_shows_help(cmd, capsys):
    with pytest.raises(SystemExit) as e:
        main([cmd, "--help"])
    assert e.value.code == 0 and cmd in capsys.readouterr().out


def test_schema_prints_json(capsys):
    import json
    assert main(["schema"]) == 0
    assert "properties" in json.loads(capsys.readouterr().out)


def test_serve_refuses_a_public_address_without_any_login(monkeypatch, capsys, tmp_path):
    for k in ("CLIPCROW_TOKEN", "CLIPCROW_ACCOUNTS"):
        monkeypatch.delenv(k, raising=False)
    assert main(["serve", "--host", "0.0.0.0", "--data", str(tmp_path)]) != 0
    assert "CLIPCROW_TOKEN" in capsys.readouterr().err


def test_serve_reads_its_switches_from_the_environment(monkeypatch):
    """In a container the switches come from the environment, not from a command line."""
    seen = {}
    monkeypatch.setenv("CLIPCROW_ACCOUNTS", "1")
    monkeypatch.setenv("CLIPCROW_SIGNUP", "0")
    monkeypatch.setattr("uvicorn.run", lambda app, **kw: seen.update(app=app, **kw))
    assert main(["serve", "--host", "0.0.0.0", "--data", "/tmp/clipcrow-cli-test", "--demo"]) == 0
    assert seen["host"] == "0.0.0.0" and seen["timeout_graceful_shutdown"] == 20
    assert seen["app"].state.accounts is not None
    from fastapi.testclient import TestClient
    cfg = TestClient(seen["app"]).get("/api/config").json()
    assert cfg["accounts"] is True and cfg["signup"] is False                      # CLIPCROW_SIGNUP=0 closed registration


def test_selftest_reports_each_missing_tool_and_fails(monkeypatch, capsys):
    monkeypatch.setenv("PATH", "")
    assert main(["selftest", "--no-render"]) == 1
    out = capsys.readouterr().out
    assert "FAIL  ffmpeg: ffmpeg is not installed" in out and "FAIL  ffprobe" in out


def test_r2_cors_needs_the_storage_settings_and_shows_or_sets_the_rule(monkeypatch, capsys, storage):
    for k in ("R2_ACCOUNT_ID", "R2_ENDPOINT", "R2_ACCESS_KEY_ID", "R2_SECRET_ACCESS_KEY", "R2_BUCKET"):
        monkeypatch.delenv(k, raising=False)
    assert main(["r2-cors", "--show"]) != 0 and "R2_BUCKET" in capsys.readouterr().err
    monkeypatch.setenv("R2_ENDPOINT", storage.endpoint); monkeypatch.setenv("R2_ACCESS_KEY_ID", "k")
    monkeypatch.setenv("R2_SECRET_ACCESS_KEY", "s"); monkeypatch.setenv("R2_BUCKET", storage.bucket)
    assert main(["r2-cors", "--show"]) == 0 and capsys.readouterr().out.strip() == "[]"
    assert main(["r2-cors", "--origin", "https://clips.example.com"]) == 0
    assert "https://clips.example.com" in capsys.readouterr().out
    assert storage.get_cors()[0]["AllowedOrigins"] == ["https://clips.example.com"]
    assert main(["r2-cors", "--origin", "*"]) != 0                                  # a wildcard origin is refused

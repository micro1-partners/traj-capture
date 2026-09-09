import json
import os
import time
from pathlib import Path
import pytest

from tests.conftest import _git
from tests.test_end import start_session, end


def test_reenrollment_cannot_redirect_old_session(capture, env, make_repo):
    repo = make_repo()
    transcript = start_session(capture, env, repo, "company-session")
    original = capture.load_config()
    sdir = capture.session_dir("claude_code", "company-session")
    capture.write_json(sdir / "end.marker", {"reason": "other"})
    destination = env["tmp"] / "company-b"
    new = capture._write_config_from_enroll(original, {"company": "B", "sas_url": f"file://{destination}"}, "fake", "https://example.invalid")
    capture.cmd_sweep([], "{}")
    assert not list(destination.rglob("transcript.jsonl"))
    with pytest.raises(RuntimeError, match="binding changed"):
        capture.finalize_session(original, "claude_code", "company-session", False)
    # A resumed transcript may not be re-created under the new company's namespace.
    capture.cmd_start([], json.dumps({"session_id": "company-session", "cwd": str(repo), "transcript_path": str(transcript)}))
    assert not list(destination.rglob("*.jsonl"))
    assert not (capture.session_dir("claude_code", "company-session", new) / "meta.json").exists()


def test_disable_stops_turn_worker_sweep_and_each_put(capture, env, make_repo):
    repo = make_repo()
    t = start_session(capture, env, repo, "pause-session")
    cfg = capture.load_config()
    sink = capture._sink(cfg, "claude_code")
    before = {str(p): p.read_bytes() for p in env["sink"].rglob("*") if p.is_file()}
    cfg["enabled"] = False
    capture.write_json(env["cfg"], cfg)
    t.write_text('{"private":"new synthetic content"}\n')
    capture.cmd_turnwork(["--tool", "claude_code", "--session", "pause-session"], "{}")
    end(capture, "pause-session", t, repo)
    capture.cmd_sweep([], "{}")
    import pytest
    with pytest.raises(RuntimeError, match="paused"):
        sink.put("should-not-upload", b"synthetic")
    assert {str(p): p.read_bytes() for p in env["sink"].rglob("*") if p.is_file()} == before


def test_git_index_is_byte_for_byte_unchanged(capture, env, make_repo):
    repo = make_repo()
    (repo / "README.md").unlink()
    (repo / "untracked.txt").write_text("new\n")
    before = (repo / ".git/index").read_bytes()
    before_staged = _git(repo, "diff", "--cached")
    capture.git_final_diff(repo, "HEAD")
    assert (repo / ".git/index").read_bytes() == before
    assert _git(repo, "diff", "--cached") == before_staged


def test_no_timestamp_attribution_and_late_correlated_response(capture, env):
    from tests.test_raw_api import _req, _resp
    raw = env["tmp"] / "raw"; raw.mkdir()
    a = env["tmp"] / "A"; a.mkdir()
    b = env["tmp"] / "B"; b.mkdir()
    _req(raw, "request-a", "A", 100)
    _req(raw, "request-b", "B", 101)
    response = raw / "unknown.response.json"
    response.write_text('{"id":"unpaired","content":"A"}')
    os.utime(response, (103, 103))
    capture.collect_raw_api(raw, b, "B")
    assert response.exists() and not list((b / "raw-api").glob("*.response.json"))
    capture.collect_raw_api(raw, a, "A")
    _resp(raw, "req_request-a", 110)
    capture.collect_raw_api(raw, a, "A")
    assert (a / "raw-api/req_request-a.response.json").exists()


def test_missing_codex_transcript_retried_before_receipt(capture, env, make_repo, monkeypatch):
    repo = make_repo()
    monkeypatch.setenv("CODEX_HOME", str(env["tmp"] / "codex"))
    hook = {"session_id": "delayed", "cwd": str(repo), "transcript_path": None}
    capture.main(["start", "--tool", "codex"], stdin_text=json.dumps(hook))
    capture.main(["end", "--tool", "codex"], stdin_text=json.dumps(hook))
    sdir = capture.session_dir("codex", "delayed")
    assert not (sdir / "receipt.json").exists()
    t = env["tmp"] / "codex/sessions/2026/09/09/rollout-test-delayed.jsonl"
    t.parent.mkdir(parents=True); t.write_text('{"type":"response_item"}\n')
    capture.cmd_sweep([], "{}")
    assert (sdir / "receipt.json").exists()
    assert list(env["sink"].rglob("transcript.jsonl"))[0].read_bytes() == t.read_bytes()


def test_start_never_contacts_network_and_marks_timeout(capture, env, make_repo, monkeypatch):
    repo = make_repo()
    work = []
    monkeypatch.setattr(capture, "detach", lambda args: work.append(args))
    monkeypatch.setattr(capture, "_sink", lambda *args: (_ for _ in ()).throw(AssertionError("network in hook")))
    monkeypatch.setattr(capture, "START_SNAPSHOT_SECONDS", 0)
    capture.cmd_start([], json.dumps({"session_id": "bounded", "cwd": str(repo)}))
    start = capture.read_json(capture.session_dir("claude_code", "bounded") / "start.json")
    assert start["snapshot_complete"] is False
    assert any(args[0] == "turnwork" for args in work)


def test_enrollment_shared_by_both_hosts(capture, env, monkeypatch):
    home = env["tmp"] / "home"
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    monkeypatch.delenv("TRAJ_CAPTURE_CONFIG")
    code = env["tmp"] / "pending-enroll-code"; code.write_text("synthetic")
    monkeypatch.setenv("CLAUDE_PLUGIN_DATA", str(env["tmp"] / "claude-data"))
    monkeypatch.setattr(capture, "enroll", lambda *args: {"company": "A", "sas_url": f"file://{env['sink']}"})
    assert capture.maybe_auto_enroll()
    monkeypatch.setenv("CLAUDE_PLUGIN_DATA", str(env["tmp"] / "codex-data"))
    assert capture.load_config()["company"] == "A"
    assert capture.config_path() == home / ".traj-capture/config.json"
    assert capture.config_path().stat().st_mode & 0o777 == 0o600


def test_legacy_unbound_session_is_not_adopted(capture, env, make_repo):
    legacy = env["state"] / "sessions/claude_code/legacy"
    legacy.mkdir(parents=True)
    (legacy / "meta.json").write_text('{"turns":1}')
    capture.cmd_start([], json.dumps({"session_id": "legacy", "cwd": str(make_repo())}))
    assert not list(env["sink"].rglob("*.json"))


def test_pause_is_rechecked_between_http_retries(capture, env, monkeypatch):
    import urllib.error
    cfg = capture.load_config()
    cfg["sas_url"] = "https://storage.example.invalid/container?synthetic=1"
    capture.write_json(env["cfg"], cfg)
    attempts = []
    def fail_once(*args, **kwargs):
        attempts.append(1)
        capture.write_json(env["cfg"], {**cfg, "enabled": False})
        raise urllib.error.URLError("synthetic network failure")
    monkeypatch.setattr(capture.urllib.request, "urlopen", fail_once)
    monkeypatch.setattr(capture.time, "sleep", lambda _: None)
    with pytest.raises(RuntimeError, match="paused"):
        capture._sink(cfg, "claude_code").put("test", b"synthetic")
    assert len(attempts) == 1


def test_real_detached_worker_canary(capture, env, make_repo, monkeypatch):
    import subprocess
    from tests.conftest import SCRIPT
    monkeypatch.setenv("TRAJ_CAPTURE_INLINE", "0")
    repo = make_repo()
    t = env["tmp"] / "synthetic.jsonl"
    t.write_text('{"role":"user","content":"synthetic test"}\n')
    hook = json.dumps({"session_id": "detached-canary", "cwd": str(repo), "transcript_path": str(t)})
    for verb in ("start", "end"):
        r = subprocess.run([os.sys.executable, str(SCRIPT), verb, "--tool", "codex"],
                           input=hook, text=True, capture_output=True, timeout=10)
        assert r.returncode == 0
    receipt = env["sink"] / "trajectories/_receipts/detached-canary.json"
    deadline = time.monotonic() + 8
    while not receipt.exists() and time.monotonic() < deadline:
        time.sleep(0.05)
    assert receipt.exists()
    prefix = json.loads(receipt.read_text())["prefix"]
    assert (env["sink"] / prefix / "transcript.jsonl").read_bytes() == t.read_bytes()

import json
import os
import time
import urllib.request


def start_session(capture, env, cwd, sid):
    t = env["tmp"] / f"{sid}.jsonl"; t.write_text("{}\n")
    capture.main(["start"], stdin_text=json.dumps({"session_id": sid, "transcript_path": str(t), "cwd": str(cwd), "source": "startup"}))
    return t


def test_sweep_finalizes_idle_session_as_inferred(capture, env, make_repo):
    repo = make_repo()
    t = start_session(capture, env, repo, "w1")
    old = time.time() - capture.IDLE_SECONDS - 60
    os.utime(t, (old, old))
    capture.main(["sweep"], stdin_text="{}")
    user_hash, _ = capture.identity()
    endj = json.loads((env["sink"] / f"trajectories/claude_code/{user_hash}/w1/end.json").read_text())
    assert endj["end_state"] == "inferred"


def test_sweep_leaves_active_session_alone(capture, env, make_repo):
    repo = make_repo()
    start_session(capture, env, repo, "w2")
    capture.main(["sweep"], stdin_text="{}")
    assert not (env["state"] / "sessions/claude_code/w2/receipt.json").exists()


def test_sweep_retries_marked_session_after_failed_worker(capture, env, make_repo, monkeypatch):
    repo = make_repo()
    t = start_session(capture, env, repo, "w3")
    orig = capture.DirSink.put
    def failing_put(self, rel, data, content_type="application/octet-stream"):
        if rel.endswith("/end.json"):
            raise RuntimeError("boom")
        return orig(self, rel, data, content_type)
    monkeypatch.setattr(capture.DirSink, "put", failing_put)
    capture.main(["end"], stdin_text=json.dumps({"session_id": "w3", "transcript_path": str(t), "cwd": str(repo), "reason": "other"}))
    assert not (env["state"] / "sessions/claude_code/w3/receipt.json").exists()
    monkeypatch.setattr(capture.DirSink, "put", orig)
    capture.main(["sweep"], stdin_text="{}")
    assert (env["state"] / "sessions/claude_code/w3/receipt.json").exists()


class _Resp:
    status = 200
    def __init__(self, body): self._b = body
    def __enter__(self): return self
    def __exit__(self, *a): return False
    def read(self): return self._b


def test_setup_with_code_enrolls_writes_config_and_probes(capture, env, monkeypatch, capsys):
    seen = {}
    def fake_urlopen(req, timeout=0):
        seen["url"] = req.full_url; seen["body"] = json.loads(req.data)
        return _Resp(json.dumps({"company": "acme", "sas_url": f"file://{env['sink']}", "expires_at": "2099-01-01T00:00:00Z"}).encode())
    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    monkeypatch.setenv("TRAJ_CAPTURE_ENROLL_URL", "https://portal.test")
    rc = capture.main(["setup", "--code", "ACME-1"], stdin_text="{}")
    assert rc == 0
    assert seen["url"] == "https://portal.test/api/traj/enroll" and seen["body"]["code"] == "ACME-1"
    assert json.loads(env["cfg"].read_text())["enroll_url"] == "https://portal.test"
    cfg = json.loads(env["cfg"].read_text())
    assert cfg["company"] == "acme" and cfg["enrollment_code"] == "ACME-1" and cfg["sas_expires_at"].startswith("2099")
    assert "ok" in capsys.readouterr().out


def test_maybe_refresh_rotates_when_near_expiry(capture, env, monkeypatch):
    cfg = json.loads(env["cfg"].read_text())
    cfg.update({"enrollment_code": "C1", "enroll_url": "https://portal.test", "sas_expires_at": "2000-01-01T00:00:00Z"})
    env["cfg"].write_text(json.dumps(cfg))
    body = json.dumps({"company": "testco", "sas_url": f"file://{env['sink']}", "expires_at": "2099-01-01T00:00:00Z"}).encode()
    monkeypatch.setattr(urllib.request, "urlopen", lambda req, timeout=0: _Resp(body))
    assert capture.maybe_refresh(capture.load_config()) is True
    assert json.loads(env["cfg"].read_text())["sas_expires_at"].startswith("2099")
    assert capture.maybe_refresh(capture.load_config()) is False


def test_probe_writes_object(capture, env, capsys):
    rc = capture.main(["probe"], stdin_text="{}")
    assert rc == 0
    _, host_hash = capture.identity()
    assert (env["sink"] / f"trajectories/_probe/{host_hash}.json").exists()
    assert "ok" in capsys.readouterr().out


def test_sweep_does_not_finalize_fresh_session_without_transcript(capture, env, make_repo):
    repo = make_repo()
    t = env["tmp"] / "w4.jsonl"  # never created: Claude Code writes it after the first message
    capture.main(["start"], stdin_text=json.dumps({"session_id": "w4", "transcript_path": str(t), "cwd": str(repo), "source": "startup"}))
    capture.main(["sweep"], stdin_text="{}")
    assert not (env["state"] / "sessions/claude_code/w4/receipt.json").exists()


def test_enroll_url_defaults_to_portal_and_honors_legacy_key(capture, monkeypatch):
    monkeypatch.delenv("TRAJ_CAPTURE_ENROLL_URL", raising=False)
    monkeypatch.delenv("TRAJ_CAPTURE_CDP_URL", raising=False)
    assert capture.enroll_url_from_env() == "https://data.micro1.ai"
    assert capture.enroll_url_from_config({}) == "https://data.micro1.ai"
    assert capture.enroll_url_from_config({"cdp_url": "https://old.test"}) == "https://old.test"
    assert capture.enroll_url_from_config({"enroll_url": "https://new.test", "cdp_url": "https://old.test"}) == "https://new.test"


def test_auto_enroll_from_code_file_on_session_start(capture, env, monkeypatch, tmp_path):
    seen = {}
    def fake_urlopen(req, timeout=0):
        seen["body"] = json.loads(req.data)
        return _Resp(json.dumps({"company": "acme", "sas_url": f"file://{env['sink']}", "expires_at": "2099-01-01T00:00:00Z"}).encode())
    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    env["cfg"].unlink()                                   # fresh machine: no config yet
    code_file = tmp_path / "enroll-code"
    code_file.write_text("ACME-K7M3-9QZT-4HWX\n")
    monkeypatch.setenv("TRAJ_CAPTURE_ENROLL_CODE_FILE", str(code_file))
    assert capture.maybe_auto_enroll() is True
    assert seen["body"]["code"] == "ACME-K7M3-9QZT-4HWX"
    assert not code_file.exists()                          # consumed
    assert json.loads(env["cfg"].read_text())["company"] == "acme"
    # already enrolled: a stale file is removed without a second enrollment
    code_file.write_text("ACME-K7M3-9QZT-4HWX"); seen.clear()
    assert capture.maybe_auto_enroll() is False and not code_file.exists() and seen == {}


def test_auto_enroll_failure_keeps_file_and_never_raises(capture, env, monkeypatch, tmp_path):
    def boom(req, timeout=0):
        raise OSError("portal down")
    monkeypatch.setattr(urllib.request, "urlopen", boom)
    env["cfg"].unlink()
    code_file = tmp_path / "enroll-code"; code_file.write_text("ACME-K7M3-9QZT-4HWX")
    monkeypatch.setenv("TRAJ_CAPTURE_ENROLL_CODE_FILE", str(code_file))
    assert capture.maybe_auto_enroll() is False
    assert code_file.exists()
    # and a session start on an un-enrolled machine is a clean no-op, not a crash
    assert capture.main(["start", "--tool", "claude_code"], stdin_text=json.dumps({"session_id": "s1", "cwd": str(tmp_path)})) == 0

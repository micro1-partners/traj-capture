import json
import os
import time
from tests.conftest import _git


def _req(raw, name, sid, mtime):
    p = raw / f"{name}.request.json"
    p.write_text(json.dumps({"request_id": name, "model": "m", "system": "s", "tools": [], "messages": [],
                             "metadata": {"user_id": json.dumps({"session_id": sid})}}))
    os.utime(p, (mtime, mtime)); return p


def _resp(raw, name, mtime):
    p = raw / f"{name}.response.json"
    p.write_text(json.dumps({"request_id": name.removeprefix("req_"), "id": name, "content": []})); os.utime(p, (mtime, mtime)); return p


def test_collect_moves_only_this_sessions_bodies(capture, tmp_path):
    raw = tmp_path / "raw"; raw.mkdir(); sdir = tmp_path / "s"; sdir.mkdir()
    t0 = time.time() - 100
    _req(raw, "a1", "A", t0); _resp(raw, "req_a1", t0 + 1)
    _req(raw, "b1", "B", t0 + 5); _resp(raw, "req_b1", t0 + 6)
    _req(raw, "a2", "A", t0 + 10); _resp(raw, "req_a2", t0 + 12)
    moved = capture.collect_raw_api(raw, sdir, "A")
    assert moved == 4
    assert sorted(p.name for p in (sdir / "raw-api").iterdir()) == ["a1.request.json", "a2.request.json", "req_a1.response.json", "req_a2.response.json"]
    assert sorted(p.name for p in raw.iterdir()) == ["b1.request.json", "req_b1.response.json"]


def test_finalize_packs_and_uploads_raw_api_when_telemetry_on(capture, env, make_repo):
    cfg = json.loads(env["cfg"].read_text()); raw = env["tmp"] / "raw"; raw.mkdir()
    cfg.update({"telemetry": True, "raw_api_dir": str(raw)}); env["cfg"].write_text(json.dumps(cfg))
    repo = make_repo()
    t = env["tmp"] / "r1.jsonl"; t.write_text("{}\n")
    capture.main(["start"], stdin_text=json.dumps({"session_id": "r1", "transcript_path": str(t), "cwd": str(repo), "source": "startup"}))
    now = time.time(); _req(raw, "x1", "r1", now - 5); _resp(raw, "req_x1", now - 4)
    capture.main(["end"], stdin_text=json.dumps({"session_id": "r1", "transcript_path": str(t), "cwd": str(repo), "reason": "other"}))
    user_hash, _ = capture.identity()
    rp = env["sink"] / f"trajectories/claude_code/{user_hash}/r1"
    assert (rp / "raw_api.tar.gz").exists()
    import tarfile
    names = tarfile.open(rp / "raw_api.tar.gz").getnames()
    assert "raw-api/x1.request.json" in names and "raw-api/req_x1.response.json" in names
    man = json.loads((rp / "manifest.json").read_text())
    assert man["files"]["raw_api.tar.gz"]["count"] == 2


def test_finalize_without_telemetry_uploads_no_raw(capture, env, make_repo):
    repo = make_repo()
    t = env["tmp"] / "r2.jsonl"; t.write_text("{}\n")
    capture.main(["start"], stdin_text=json.dumps({"session_id": "r2", "transcript_path": str(t), "cwd": str(repo), "source": "startup"}))
    capture.main(["end"], stdin_text=json.dumps({"session_id": "r2", "transcript_path": str(t), "cwd": str(repo), "reason": "other"}))
    assert not list(env["sink"].rglob("raw_api.tar.gz"))


def test_setup_telemetry_on_off_edits_settings_env(capture, env, monkeypatch, capsys):
    sp = env["tmp"] / "settings.json"; sp.write_text(json.dumps({"theme": "dark", "env": {"KEEP": "1"}}))
    monkeypatch.setenv("TRAJ_CAPTURE_SETTINGS", str(sp))
    assert capture.main(["setup", "--telemetry", "on"], stdin_text="{}") == 0
    s = json.loads(sp.read_text())
    assert s["theme"] == "dark" and s["env"]["KEEP"] == "1"
    assert s["env"]["CLAUDE_CODE_ENABLE_TELEMETRY"] == "1" and s["env"]["OTEL_LOG_RAW_API_BODIES"].startswith("file:")
    assert (env["tmp"] / "settings.json.traj-capture.bak").exists()
    assert json.loads(env["cfg"].read_text())["telemetry"] is True
    capture.main(["setup", "--telemetry", "off"], stdin_text="{}")
    s = json.loads(sp.read_text())
    assert "OTEL_LOG_RAW_API_BODIES" not in s["env"] and s["env"]["KEEP"] == "1"
    assert json.loads(env["cfg"].read_text())["telemetry"] is False


def test_sweep_prunes_old_orphan_raw_bodies_but_keeps_known_and_fresh(capture, env, make_repo):
    cfg = json.loads(env["cfg"].read_text()); raw = env["tmp"] / "raw"; raw.mkdir()
    cfg.update({"telemetry": True, "raw_api_dir": str(raw)}); env["cfg"].write_text(json.dumps(cfg))
    repo = make_repo()
    t = env["tmp"] / "k1.jsonl"; t.write_text("{}\n")
    capture.main(["start"], stdin_text=json.dumps({"session_id": "k1", "transcript_path": str(t), "cwd": str(repo), "source": "startup"}))
    old = time.time() - capture.RAW_ORPHAN_MAX_AGE - 60
    _req(raw, "known_old", "k1", old); _resp(raw, "req_known_old", old + 1)
    _req(raw, "orphan_old", "zz", old); _resp(raw, "req_orphan_old", old + 1)
    _req(raw, "orphan_fresh", "zz", time.time())
    capture.main(["sweep"], stdin_text="{}")
    left = sorted(p.name for p in raw.iterdir())
    assert "orphan_old.request.json" not in left and "req_orphan_old.response.json" in left
    assert "orphan_fresh.request.json" in left
    assert "known_old.request.json" not in left  # sweep now collects active-session uploads too
    sdir = capture.session_dir("claude_code", "k1")
    assert (sdir / "raw-api/known_old.request.json").exists()

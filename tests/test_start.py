import json


def hook(sid, cwd, transcript, source="startup"):
    return json.dumps({"session_id": sid, "transcript_path": str(transcript), "cwd": str(cwd),
                       "hook_event_name": "SessionStart", "source": source, "model": "claude-x"})


def test_start_in_repo_writes_and_uploads_start_json(capture, env, make_repo):
    repo = make_repo()
    (repo / "README.md").write_text("hello\ndirty\n")
    t = env["tmp"] / "s1.jsonl"; t.write_text("{}\n")
    rc = capture.main(["start"], stdin_text=hook("s1", repo, t))
    assert rc == 0
    local = capture.read_json(env["state"] / "sessions/claude_code/s1/start.json")
    assert local["base_commit"] and local["repo_remote"].endswith("app.git")
    assert "+dirty" in local["dirty_diff"]
    assert local["mode"] == "git" and local["source"] == "startup"
    user_hash, _ = capture.identity()
    remote = env["sink"] / f"trajectories/claude_code/{user_hash}/s1/start.json"
    assert json.loads(remote.read_text())["session_id"] == "s1"
    meta = capture.read_json(env["state"] / "sessions/claude_code/s1/meta.json")
    assert meta["transcript_path"] == str(t) and meta["turns"] == 0


def test_start_non_repo_uses_shadow(capture, env, tmp_path):
    cwd = tmp_path / "work"; cwd.mkdir(); (cwd / "brief.md").write_text("x\n")
    t = tmp_path / "s2.jsonl"; t.write_text("{}\n")
    capture.main(["start"], stdin_text=hook("s2", cwd, t))
    local = capture.read_json(env["state"] / "sessions/claude_code/s2/start.json")
    assert local["mode"] == "shadow" and len(local["shadow_base"]) == 40
    assert "brief.md" in [f["path"] for f in local["shadow_manifest"]["files"]]


def test_start_skips_repo_not_on_allowlist(capture, env, make_repo):
    cfg = json.loads(env["cfg"].read_text()); cfg["repo_allowlist"] = ["*github.com/allowed/*"]
    env["cfg"].write_text(json.dumps(cfg))
    repo = make_repo()
    t = env["tmp"] / "s3.jsonl"; t.write_text("{}\n")
    capture.main(["start"], stdin_text=hook("s3", repo, t))
    sdir = env["state"] / "sessions/claude_code/s3"
    assert (sdir / "skipped").exists() and not (sdir / "start.json").exists()
    assert not list(env["sink"].rglob("start.json"))


def test_resume_does_not_overwrite_start(capture, env, make_repo):
    repo = make_repo()
    t = env["tmp"] / "s4.jsonl"; t.write_text("{}\n")
    capture.main(["start"], stdin_text=hook("s4", repo, t))
    first = capture.read_json(env["state"] / "sessions/claude_code/s4/start.json")
    capture.main(["start"], stdin_text=hook("s4", repo, t, source="resume"))
    again = capture.read_json(env["state"] / "sessions/claude_code/s4/start.json")
    assert again["base_commit"] == first["base_commit"]
    assert again["continuations"][0]["source"] == "resume"


def test_start_disabled_config_does_nothing(capture, env, make_repo):
    cfg = json.loads(env["cfg"].read_text()); cfg["enabled"] = False
    env["cfg"].write_text(json.dumps(cfg))
    repo = make_repo()
    t = env["tmp"] / "s5.jsonl"; t.write_text("{}\n")
    capture.main(["start"], stdin_text=hook("s5", repo, t))
    assert not (env["state"] / "sessions").exists()

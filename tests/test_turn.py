import json


def start_session(capture, env, repo, sid="t1"):
    t = env["tmp"] / f"{sid}.jsonl"; t.write_text('{"type":"user"}\n')
    capture.main(["start"], stdin_text=json.dumps({"session_id": sid, "transcript_path": str(t),
                                                   "cwd": str(repo), "source": "startup"}))
    return t


def turn(capture, sid, t, cwd):
    return capture.main(["turn"], stdin_text=json.dumps({"session_id": sid, "transcript_path": str(t), "cwd": str(cwd)}))


def test_turn_uploads_transcript_and_counts(capture, env, make_repo):
    repo = make_repo()
    t = start_session(capture, env, repo)
    turn(capture, "t1", t, repo)
    user_hash, _ = capture.identity()
    remote = env["sink"] / f"trajectories/claude_code/{user_hash}/t1/transcript.jsonl"
    assert remote.read_text() == '{"type":"user"}\n'
    meta = capture.read_json(env["state"] / "sessions/claude_code/t1/meta.json")
    assert meta["turns"] == 1 and meta["uploads"]["transcript.jsonl"] == capture.sha256_file(t)


def test_turn_skips_unchanged_and_reuploads_changed(capture, env, make_repo, monkeypatch):
    repo = make_repo()
    t = start_session(capture, env, repo, sid="t2")
    turn(capture, "t2", t, repo)
    puts = []
    orig = capture.DirSink.put
    def counting_put(self, rel, data, content_type="application/octet-stream"):
        puts.append(rel); return orig(self, rel, data, content_type)
    monkeypatch.setattr(capture.DirSink, "put", counting_put)
    turn(capture, "t2", t, repo)
    assert puts == []
    t.write_text('{"type":"user"}\n{"type":"assistant"}\n')
    turn(capture, "t2", t, repo)
    assert any(p.endswith("/transcript.jsonl") for p in puts)


def test_turn_uploads_subagents(capture, env, make_repo):
    repo = make_repo()
    t = start_session(capture, env, repo, sid="t3")
    sub = t.parent / "t3" / "subagents"; sub.mkdir(parents=True)
    (sub / "agent-a.jsonl").write_text("{}\n")
    turn(capture, "t3", t, repo)
    user_hash, _ = capture.identity()
    assert (env["sink"] / f"trajectories/claude_code/{user_hash}/t3/subagents/agent-a.jsonl").exists()


def test_turn_without_start_is_ignored(capture, env, tmp_path):
    t = tmp_path / "x.jsonl"; t.write_text("{}\n")
    assert turn(capture, "nostart", t, tmp_path) == 0
    assert not (env["state"] / "sessions/claude_code/nostart/meta.json").exists()

import json
from tests.conftest import _git


def start_session(capture, env, cwd, sid):
    t = env["tmp"] / f"{sid}.jsonl"; t.write_text('{"type":"user"}\n{"type":"assistant"}\n')
    capture.main(["start"], stdin_text=json.dumps({"session_id": sid, "transcript_path": str(t), "cwd": str(cwd), "source": "startup"}))
    return t


def end(capture, sid, t, cwd, reason="prompt_input_exit"):
    return capture.main(["end"], stdin_text=json.dumps({"session_id": sid, "transcript_path": str(t), "cwd": str(cwd), "reason": reason}))


def test_end_in_repo_produces_end_manifest_receipt(capture, env, make_repo):
    repo = make_repo()
    t = start_session(capture, env, repo, "e1")
    (repo / "a.txt").write_text("a\n"); _git(repo, "add", "-A"); _git(repo, "commit", "-q", "-m", "agent adds a")
    (repo / "README.md").write_text("hello\nedited\n")
    assert end(capture, "e1", t, repo) == 0
    sdir = capture.company_root(capture.load_config()) / "sessions/claude_code/e1"
    assert (sdir / "end.marker").exists() and (sdir / "receipt.json").exists()
    user_hash, _ = capture.identity()
    rp = env["sink"] / f"trajectories/claude_code/{user_hash}/e1"
    endj = json.loads((rp / "end.json").read_text())
    assert endj["end_state"] == "observed" and endj["reason"] == "prompt_input_exit"
    assert endj["commits_since_base"][0]["subject"] == "agent adds a"
    assert "+edited" in endj["final_diff"] and "a.txt" in endj["final_diff"]
    assert endj["counts"]["transcript_lines"] == 2
    man = json.loads((rp / "manifest.json").read_text())
    assert man["company"] == "testco" and man["provider"] == "anthropic"
    assert man["transcript_sha256"] == capture.sha256_file(t)
    assert set(man["files"]) >= {"transcript.jsonl", "start.json", "end.json"}
    assert (rp / "transcript.jsonl").exists()
    receipt = json.loads((env["sink"] / "trajectories/_receipts/e1.json").read_text())
    assert receipt["session_id"] == "e1" and receipt["tool"] == "claude_code"


def test_end_in_shadow_mode_records_diff(capture, env, tmp_path):
    cwd = tmp_path / "work"; cwd.mkdir(); (cwd / "brief.md").write_text("draft\n")
    t = start_session(capture, env, cwd, "e2")
    (cwd / "brief.md").write_text("draft\nfinal\n")
    end(capture, "e2", t, cwd)
    user_hash, _ = capture.identity()
    endj = json.loads((env["sink"] / f"trajectories/claude_code/{user_hash}/e2/end.json").read_text())
    assert endj["mode"] == "shadow" and "+final" in endj["shadow"]["diff"]


def test_end_without_start_is_ignored(capture, env, tmp_path):
    t = tmp_path / "n.jsonl"; t.write_text("{}\n")
    assert end(capture, "none", t, tmp_path) == 0
    assert not list(env["sink"].rglob("*.json"))


def test_worker_is_idempotent(capture, env, make_repo):
    repo = make_repo()
    t = start_session(capture, env, repo, "e4")
    end(capture, "e4", t, repo)
    first = (capture.company_root(capture.load_config()) / "sessions/claude_code/e4/receipt.json").read_text()
    capture.main(["worker", "--tool", "claude_code", "--session", "e4"], stdin_text="{}")
    assert (capture.company_root(capture.load_config()) / "sessions/claude_code/e4/receipt.json").read_text() == first


def test_activity_after_finalize_reopens_and_refinalizes(capture, env, make_repo):
    repo = make_repo()
    t = start_session(capture, env, repo, "e5")
    end(capture, "e5", t, repo)
    sdir = capture.company_root(capture.load_config()) / "sessions/claude_code/e5"
    assert (sdir / "receipt.json").exists()
    t.write_text('{"type":"user"}\n{"type":"assistant"}\n{"type":"user"}\n')
    capture.main(["turn"], stdin_text=json.dumps({"session_id": "e5", "transcript_path": str(t), "cwd": str(repo)}))
    assert (sdir / "receipt.v1.json").exists() and not (sdir / "receipt.json").exists()
    end(capture, "e5", t, repo)
    assert (sdir / "receipt.json").exists()
    user_hash, _ = capture.identity()
    endj = json.loads((env["sink"] / f"trajectories/claude_code/{user_hash}/e5/end.json").read_text())
    assert endj["counts"]["transcript_lines"] == 3 and endj["counts"]["turns"] == 1


def test_wait_marks_process_exit_and_finalizes_when_agent_dies(capture, env, make_repo, monkeypatch):
    import subprocess
    repo = make_repo()
    t = start_session(capture, env, repo, "e6")
    capture.main(["turn"], stdin_text=json.dumps({"session_id": "e6", "transcript_path": str(t), "cwd": str(repo)}))
    import threading
    agent = subprocess.Popen(["sleep", "0.3"])  # stands in for the claude process
    threading.Thread(target=agent.wait, daemon=True).start()  # reap it, as a real parent shell would
    monkeypatch.setenv("TRAJ_CAPTURE_WAIT_INTERVAL", "0.1")
    rc = capture.main(["wait", "--tool", "claude_code", "--session", "e6", "--pid", str(agent.pid)], stdin_text="{}")
    assert rc == 0
    sdir = capture.company_root(capture.load_config()) / "sessions/claude_code/e6"
    assert capture.read_json(sdir / "end.marker")["reason"] == "process_exit"
    assert (sdir / "receipt.json").exists()
    user_hash, _ = capture.identity()
    endj = json.loads((env["sink"] / f"trajectories/claude_code/{user_hash}/e6/end.json").read_text())
    assert endj["end_state"] == "observed" and endj["reason"] == "process_exit"


def test_finalize_skips_when_another_finalize_holds_the_lock(capture, env, make_repo):
    repo = make_repo()
    t = start_session(capture, env, repo, "e7")
    sdir = capture.company_root(capture.load_config()) / "sessions/claude_code/e7"
    import os
    (sdir / "finalizing.lock").write_text(str(os.getpid()))
    assert capture.finalize_session(capture.load_config(), "claude_code", "e7", False) is False
    assert not (sdir / "receipt.json").exists()
    (sdir / "finalizing.lock").unlink()
    assert capture.finalize_session(capture.load_config(), "claude_code", "e7", False) is True
    assert not (sdir / "finalizing.lock").exists()


def test_agent_diff_excludes_preexisting_dirty_work(capture, env, make_repo):
    repo = make_repo()
    (repo / "README.md").write_text("hello\npre-existing human edit\n")   # dirty before the session
    (repo / "wip.txt").write_text("human wip\n")                           # untracked before the session
    t = start_session(capture, env, repo, "e8")
    start = capture.read_json(capture.company_root(capture.load_config()) / "sessions/claude_code/e8/start.json")
    assert len(start["start_tree"]) == 40
    (repo / "agent.txt").write_text("agent wrote this\n")                  # the agent's work
    end(capture, "e8", t, repo)
    user_hash, _ = capture.identity()
    endj = json.loads((env["sink"] / f"trajectories/claude_code/{user_hash}/e8/end.json").read_text())
    assert "pre-existing human edit" in endj["final_diff"] and "agent.txt" in endj["final_diff"]
    assert "agent.txt" in endj["agent_diff"] and "pre-existing human edit" not in endj["agent_diff"] and "wip.txt" not in endj["agent_diff"]
    assert _git(repo, "status", "--porcelain").count("\n") >= 2  # real index and worktree untouched
    assert _git(repo, "for-each-ref", "refs/traj-capture/e8/").count("\n") == 1


def test_agent_diff_empty_when_agent_changed_nothing(capture, env, make_repo):
    repo = make_repo()
    (repo / "README.md").write_text("hello\ndirty\n")
    t = start_session(capture, env, repo, "e9")
    end(capture, "e9", t, repo)
    user_hash, _ = capture.identity()
    endj = json.loads((env["sink"] / f"trajectories/claude_code/{user_hash}/e9/end.json").read_text())
    assert endj["final_diff"] and endj["agent_diff"] == ""

def test_identity_is_stable_12_hex(capture, env):
    u1, h1 = capture.identity()
    u2, h2 = capture.identity()
    assert (u1, h1) == (u2, h2)
    assert len(u1) == 12 and len(h1) == 12
    int(u1, 16); int(h1, 16)


def test_session_dir_and_prefix(capture, env):
    d = capture.session_dir("claude_code", "abc")
    assert d == capture.company_root(capture.load_config()) / "sessions" / "claude_code" / "abc"
    assert d.is_dir()
    assert capture.remote_prefix("codex", "u1", "s1") == "trajectories/codex/u1/s1"


def test_find_subagent_files(capture, tmp_path):
    proj = tmp_path / "proj"
    (proj / "sid" / "subagents").mkdir(parents=True)
    t = proj / "sid.jsonl"
    t.write_text("{}\n")
    a = proj / "sid" / "subagents" / "agent-1.jsonl"
    a.write_text("{}\n")
    (proj / "other.jsonl").write_text("{}\n")
    assert capture.find_subagent_files(t, "sid") == [a]
    assert capture.find_subagent_files(proj / "nosuch.jsonl", "nosuch") == []


def test_allowed_globs(capture):
    assert capture.allowed("https://github.com/testco/app.git", []) is True
    assert capture.allowed("https://github.com/testco/app.git", ["*github.com/testco/*"]) is True
    assert capture.allowed("git@github.com:other/x.git", ["*github.com/testco/*"]) is False


def test_tool_from_args(capture):
    assert capture.tool_from_args([]) == "claude_code"
    assert capture.tool_from_args(["--tool", "codex"]) == "codex"

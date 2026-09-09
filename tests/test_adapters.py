import json
import shlex
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1] / "plugins" / "traj-capture"


def test_claude_code_hooks_cover_three_events_and_are_synchronous():
    h = json.loads((ROOT / "hooks" / "hooks.json").read_text())["hooks"]
    assert set(h) == {"SessionStart", "Stop", "SessionEnd"}
    cmd = lambda ev: h[ev][0]["hooks"][0]
    assert shlex.split(cmd("SessionStart")["command"])[-3:] == ["start", "--tool", "auto"]
    # Stop and SessionEnd run synchronously (async hooks are abandoned when `claude -p` exits);
    # the script itself detaches the slow work so both finish well inside their budgets.
    assert "async" not in cmd("Stop") and shlex.split(cmd("Stop")["command"])[-3:] == ["turn", "--tool", "auto"]
    assert "async" not in cmd("SessionEnd") and shlex.split(cmd("SessionEnd")["command"])[-3:] == ["end", "--tool", "auto"]
    assert cmd("SessionEnd")["timeout"] == 3
    assert all("${CLAUDE_PLUGIN_ROOT}" in cmd(e)["command"] for e in h)


def test_codex_adapter_mirrors_events_with_codex_tool_flag():
    h = json.loads((ROOT / "adapters" / "codex-hooks.json").read_text())["hooks"]
    assert set(h) == {"SessionStart", "Stop", "SessionEnd"}
    for ev in h:
        assert shlex.split(h[ev][0]["hooks"][0]["command"])[-3:] == [dict(SessionStart='start', Stop='turn', SessionEnd='end')[ev], "--tool", "codex"]


def test_plugin_manifest_and_marketplace_agree():
    plugin = json.loads((ROOT / ".claude-plugin" / "plugin.json").read_text())
    market = json.loads((ROOT.parents[1] / ".claude-plugin" / "marketplace.json").read_text())
    assert plugin["name"] == "traj-capture"
    assert market["plugins"][0]["name"] == "traj-capture"
    assert market["plugins"][0]["source"] == "./plugins/traj-capture"


def test_codex_rollout_path_resolves_by_session_id(tmp_path, monkeypatch):
    import sys
    sys.path.insert(0, str(ROOT / "scripts"))
    import capture
    monkeypatch.setenv("CODEX_HOME", str(tmp_path))
    sid = "019fba01-2848-7de3-95d3-b831840e9016"
    assert capture.codex_rollout_path(sid) == ""            # no sessions dir yet
    f = tmp_path / "sessions" / "2026" / "07" / "31" / f"rollout-2026-07-31T14-07-45-{sid}.jsonl"
    f.parent.mkdir(parents=True); f.write_text("{}\n")
    assert capture.codex_rollout_path(sid) == str(f)
    # Codex may send transcript_path: null; Claude Code always sends it.
    assert capture.resolve_transcript_path("codex", {"transcript_path": None}, sid) == str(f)
    assert capture.resolve_transcript_path("codex", {"transcript_path": "/explicit.jsonl"}, sid) == "/explicit.jsonl"
    assert capture.resolve_transcript_path("claude_code", {"transcript_path": None}, sid) == ""


def test_install_codex_merges_without_clobbering(tmp_path, monkeypatch):
    import sys
    sys.path.insert(0, str(ROOT / "scripts"))
    import capture
    hooks = tmp_path / "hooks.json"
    hooks.write_text(json.dumps({"description": "mine", "hooks": {
        "Stop": [{"hooks": [{"type": "command", "command": "./lint.sh"}]}],
        "PreToolUse": [{"matcher": "Bash", "hooks": [{"type": "command", "command": "./guard.sh"}]}],
    }}))
    root = tmp_path / "plugin"
    out = capture.install_codex_hooks(hooks, root)
    assert out["description"] == "mine"
    assert out["hooks"]["PreToolUse"][0]["hooks"][0]["command"] == "./guard.sh"       # untouched
    stop = out["hooks"]["Stop"]
    assert stop[0]["hooks"][0]["command"] == "./lint.sh"                              # theirs first
    assert stop[1]["hooks"][0]["command"] == f"python3 {root}/scripts/capture.py turn --tool codex"
    assert out["hooks"]["SessionStart"][0]["hooks"][0]["command"].endswith("capture.py start --tool codex")
    assert out["hooks"]["SessionEnd"][0]["hooks"][0]["command"].endswith("capture.py end --tool codex")
    # idempotent: second install doesn't duplicate ours
    again = capture.install_codex_hooks(hooks, root)
    assert len(again["hooks"]["Stop"]) == 2 and len(again["hooks"]["SessionStart"]) == 1
    assert json.loads(hooks.read_text()) == again


def test_install_codex_verb_uses_codex_home(tmp_path, monkeypatch, capsys):
    import sys
    sys.path.insert(0, str(ROOT / "scripts"))
    import capture
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "codexhome"))
    monkeypatch.setenv("TRAJ_CAPTURE_CONFIG", str(tmp_path / "cfg.json"))
    assert capture.main(["install-codex"]) == 0
    written = json.loads((tmp_path / "codexhome" / "hooks.json").read_text())
    assert set(written["hooks"]) == {"SessionStart", "Stop", "SessionEnd"}
    assert "not enrolled yet" in capsys.readouterr().out


def test_codex_plugin_manifest_and_marketplace_agree_with_claude_ones():
    claude = json.loads((ROOT / ".claude-plugin" / "plugin.json").read_text())
    codex = json.loads((ROOT / ".codex-plugin" / "plugin.json").read_text())
    assert codex["name"] == claude["name"] == "traj-capture" and codex["version"] == claude["version"]
    market = json.loads((ROOT.parents[1] / ".agents" / "plugins" / "marketplace.json").read_text())
    assert market["plugins"][0]["name"] == "traj-capture"
    assert market["plugins"][0]["source"] == {"source": "local", "path": "./plugins/traj-capture"}


def test_detect_tool_from_hook_payload(tmp_path, monkeypatch):
    import sys
    sys.path.insert(0, str(ROOT / "scripts"))
    import capture
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "codex"))
    monkeypatch.delenv("CLAUDE_PROJECT_DIR", raising=False)
    monkeypatch.delenv("CLAUDE_CODE_VERSION", raising=False)
    sid = "019fba01-2848-7de3-95d3-b831840e9016"
    assert capture.detect_tool({"transcript_path": "/Users/x/.claude/projects/p/abc.jsonl"}) == "claude_code"
    assert capture.detect_tool({"transcript_path": f"{tmp_path}/codex/sessions/2026/07/31/rollout-2026-07-31T14-07-45-{sid}.jsonl"}) == "codex"
    assert capture.detect_tool({"transcript_path": f"/elsewhere/rollout-x-{sid}.jsonl"}) == "codex"
    # null transcript: a rollout on disk for this session id decides
    f = tmp_path / "codex" / "sessions" / "2026" / "07" / "31" / f"rollout-2026-07-31T14-07-45-{sid}.jsonl"
    f.parent.mkdir(parents=True); f.write_text("")
    assert capture.detect_tool({"transcript_path": None, "session_id": sid}) == "codex"
    monkeypatch.setenv("CLAUDE_PROJECT_DIR", "/repo")
    assert capture.detect_tool({"transcript_path": None, "session_id": "other"}) == "claude_code"
    assert capture.resolve_tool("codex", {}) == "codex" and capture.resolve_tool("auto", {"transcript_path": "/a/b.jsonl"}) == "claude_code"

import json


def test_load_config_reads_env_path(capture, env):
    cfg = capture.load_config()
    assert cfg["company"] == "testco"
    assert cfg["sas_url"].startswith("file://")
    assert cfg["tools_enabled"] == ["claude_code", "codex"]


def test_state_root_uses_env(capture, env):
    assert capture.state_root() == env["state"]
    assert env["state"].is_dir()


def test_read_hook_input_handles_empty_and_json(capture):
    assert capture.read_hook_input("") == {}
    assert capture.read_hook_input('{"session_id": "abc"}') == {"session_id": "abc"}


def test_main_unknown_verb_exits_zero_and_logs(capture, env):
    rc = capture.main(["nonsense"], stdin_text="{}")
    assert rc == 0
    assert "unknown verb" in (env["state"] / "capture.log").read_text()


def test_config_falls_back_to_home_dir_when_data_dir_has_none(capture, env, monkeypatch, tmp_path):
    home = tmp_path / "home"; (home / ".traj-capture").mkdir(parents=True)
    (home / ".traj-capture" / "config.json").write_text(env["cfg"].read_text())
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.delenv("TRAJ_CAPTURE_CONFIG")
    monkeypatch.setenv("CLAUDE_PLUGIN_DATA", str(tmp_path / "plugin-data"))
    monkeypatch.delenv("TRAJ_CAPTURE_STATE")
    assert capture.config_path() == home / ".traj-capture" / "config.json"
    assert capture.load_config()["company"] == "testco"


def test_config_falls_back_to_home_traj_capture(capture, env, monkeypatch, tmp_path):
    home = tmp_path / "home"; (home / ".traj-capture").mkdir(parents=True)
    (home / ".traj-capture" / "config.json").write_text('{"company": "fallback-co", "sas_url": "file:///x"}')
    monkeypatch.setenv("HOME", str(home)); monkeypatch.delenv("TRAJ_CAPTURE_CONFIG")
    monkeypatch.setenv("TRAJ_CAPTURE_STATE", str(tmp_path / "plugin-data"))
    assert capture.load_config()["company"] == "fallback-co"
    (tmp_path / "plugin-data").mkdir()
    (tmp_path / "plugin-data" / "config.json").write_text('{"company": "primary-co", "sas_url": "file:///y"}')
    assert capture.load_config()["company"] == "fallback-co"

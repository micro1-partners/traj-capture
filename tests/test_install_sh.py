import os, subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _run(tmp_path, *args, path_extra=None, home=None):
    env = dict(os.environ, HOME=str(home or tmp_path), CODEX_HOME=str((home or tmp_path) / ".codex"))
    env["PATH"] = (str(path_extra) + os.pathsep if path_extra else "") + "/usr/bin:/bin"
    return subprocess.run(["sh", str(ROOT / "install.sh"), *args], capture_output=True, text=True, env=env)


def _fake_tool(tmp_path, name):
    b = tmp_path / "bin"; b.mkdir(exist_ok=True)
    f = b / name; f.write_text("#!/bin/sh\nprintf '%s\\n' \"$*\" >> \"$HOME/calls.log\"\n"); f.chmod(0o755)
    return b


def test_no_tools_exits_1(tmp_path):
    r = _run(tmp_path, "ACME-K7M3-9QZT-4HWX")
    assert r.returncode == 1 and "no supported tool" in r.stdout


def test_claude_path_registers_installs_and_saves_code(tmp_path):
    bin_ = _fake_tool(tmp_path, "claude")
    r = _run(tmp_path, "ACME-K7M3-9QZT-4HWX", path_extra=bin_)
    assert r.returncode == 0, r.stdout + r.stderr
    calls = (tmp_path / "calls.log").read_text().splitlines()
    assert "plugin marketplace add micro1-partners/traj-capture" in calls
    assert "plugin install traj-capture@micro1-traj" in calls
    code = tmp_path / ".traj-capture" / "enroll-code"
    assert code.read_text().strip() == "ACME-K7M3-9QZT-4HWX"
    assert oct(code.stat().st_mode & 0o777) == "0o600"


def test_codex_desktop_appends_marketplace_once(tmp_path):
    cfg = tmp_path / ".codex" / "config.toml"; cfg.parent.mkdir(); cfg.write_text('model = "x"\n')
    r = _run(tmp_path, "ACME-K7M3-9QZT-4HWX")
    assert r.returncode == 0, r.stdout + r.stderr
    s = cfg.read_text()
    assert "[marketplaces.micro1-traj]" in s and 'source = "https://github.com/micro1-partners/traj-capture.git"' in s
    assert '[plugins."traj-capture@micro1-traj"]' in s and s.startswith('model = "x"\n')
    _run(tmp_path, "ACME-K7M3-9QZT-4HWX")
    assert cfg.read_text().count("[marketplaces.micro1-traj]") == 1


def test_dry_run_changes_nothing(tmp_path):
    bin_ = _fake_tool(tmp_path, "claude")
    r = _run(tmp_path, "--dry-run", "ACME-K7M3-9QZT-4HWX", path_extra=bin_)
    assert r.returncode == 0 and "would run: claude plugin install" in r.stdout
    assert not (tmp_path / "calls.log").exists() and not (tmp_path / ".traj-capture").exists()

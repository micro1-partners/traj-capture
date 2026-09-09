import importlib.util
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "plugins" / "traj-capture" / "scripts" / "capture.py"


def load_capture():
    spec = importlib.util.spec_from_file_location("capture", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture
def env(tmp_path, monkeypatch):
    """Isolated state dir, file:// sink, inline (non-detached) workers."""
    state = tmp_path / "state"
    sink = tmp_path / "sink"
    cfg = tmp_path / "config.json"
    cfg.write_text(json.dumps({
        "company": "testco",
        "sas_url": f"file://{sink}",
        "repo_allowlist": [],
        "capture_non_repo": True,
        "enabled": True,
        "tools_enabled": ["claude_code", "codex"],
    }))
    monkeypatch.setenv("TRAJ_CAPTURE_STATE", str(state))
    monkeypatch.setenv("TRAJ_CAPTURE_CONFIG", str(cfg))
    monkeypatch.setenv("TRAJ_CAPTURE_INLINE", "1")
    monkeypatch.setenv("GIT_AUTHOR_NAME", "Test User")
    monkeypatch.setenv("GIT_AUTHOR_EMAIL", "test@example.com")
    monkeypatch.setenv("GIT_COMMITTER_NAME", "Test User")
    monkeypatch.setenv("GIT_COMMITTER_EMAIL", "test@example.com")
    return {"state": state, "sink": sink, "cfg": cfg, "tmp": tmp_path}


@pytest.fixture
def capture(env):
    return load_capture()


def _git(cwd, *args):
    return subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True).stdout.strip()


@pytest.fixture
def make_repo(tmp_path):
    """Create a git repo with one commit and a remote URL; returns its path."""
    def _make(name="repo", remote="https://github.com/testco/app.git"):
        d = tmp_path / name
        d.mkdir()
        _git(d, "init", "-q", "-b", "main")
        _git(d, "config", "user.email", "test@example.com")
        _git(d, "config", "user.name", "Test User")
        _git(d, "remote", "add", "origin", remote)
        (d / "README.md").write_text("hello\n")
        _git(d, "add", "-A")
        _git(d, "commit", "-q", "-m", "init")
        return d
    return _make

#!/usr/bin/env python3
"""Turn a landed traj-capture session into an ATIF trajectory using Harbor's loaders.

Usage: convert.py <landed_session_dir> [--tool claude_code|codex]
Writes <landed_session_dir>/trajectory.atif.json. Requires: pip install -r tools/requirements.txt
"""
from __future__ import annotations

import json
import shutil
import sys
import tempfile
from pathlib import Path


def _stage_claude(session_dir: Path, sid: str, tmp: Path) -> Path:
    staged = tmp / sid
    staged.mkdir()
    shutil.copy(session_dir / "transcript.jsonl", staged / f"{sid}.jsonl")
    subs = session_dir / "subagents"
    if subs.is_dir():
        shutil.copytree(subs, staged / "subagents")
    return staged


def _stage_codex(session_dir: Path, sid: str, tmp: Path) -> Path:
    staged = tmp / "sessions" / "0000" / "00" / "00"
    staged.mkdir(parents=True)
    shutil.copy(session_dir / "transcript.jsonl", staged / f"rollout-{sid}.jsonl")
    return staged


def _load(p: Path) -> dict:
    return json.loads(p.read_text()) if p.exists() else {}


def convert(session_dir: Path, tool: str) -> Path:
    manifest = _load(session_dir / "manifest.json")
    start = _load(session_dir / "start.json")
    end = _load(session_dir / "end.json")
    sid = manifest["session_id"]
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        if tool == "claude_code":
            from harbor.agents.installed.claude_code import ClaudeCode
            agent = ClaudeCode(logs_dir=tmp / "logs")
            traj = agent._convert_events_to_trajectory(_stage_claude(session_dir, sid, tmp))
        elif tool == "codex":
            from harbor.agents.installed.codex import Codex
            agent = Codex(logs_dir=tmp)
            traj = agent._convert_events_to_trajectory(_stage_codex(session_dir, sid, tmp))
        else:
            raise SystemExit(f"unsupported tool: {tool}")
    if traj is None:
        raise SystemExit("Harbor produced no trajectory (empty or unreadable transcript)")
    data = traj.model_dump(mode="json", exclude_none=True)
    extra = data.get("extra") or {}
    extra["micro1.source"] = {k: manifest.get(k) for k in
                              ("company", "tool", "provider", "model", "plugin_version", "tool_version", "user_hash", "host_hash")}
    git_end = {k: v for k, v in end.items() if k != "shadow"}
    if end.get("shadow"):
        git_end["shadow_diff"] = end["shadow"].get("diff")
    extra["micro1.git"] = {"start": {k: v for k, v in start.items() if k != "shadow_manifest"}, "end": git_end}
    # Raw API bodies (if the plugin captured telemetry): fill ATIF tool_definitions and keep the system prompt.
    raw = session_dir / "raw_api.tar.gz"
    if raw.exists():
        import tarfile
        tools = None; system = None; n_req = 0
        with tarfile.open(raw) as tar:
            members = [m for m in tar.getmembers() if m.name.endswith(".request.json")]
            n_req = len(members)
            if members:
                last = max(members, key=lambda m: m.mtime)
                body = json.loads(tar.extractfile(last).read().decode())
                tools = body.get("tools"); system = body.get("system")
        if tools:
            data.setdefault("agent", {})["tool_definitions"] = tools
        extra["micro1.raw_api"] = {"request_count": n_req, "system_prompt": system, "archive": "raw_api.tar.gz"}
    data["extra"] = extra
    data.setdefault("trajectory_id", sid)
    out = session_dir / "trajectory.atif.json"
    out.write_text(json.dumps(data, indent=1))
    return out


def main(argv: list[str]) -> int:
    if not argv:
        print(__doc__)
        return 2
    session_dir = Path(argv[0])
    tool = argv[argv.index("--tool") + 1] if "--tool" in argv else _load(session_dir / "manifest.json").get("tool", "claude_code")
    out = convert(session_dir, tool)
    data = json.loads(out.read_text())
    print(f"wrote {out} schema={data.get('schema_version')} steps={len(data.get('steps', []))}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))

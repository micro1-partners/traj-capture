#!/usr/bin/env python3
"""Stage converted sessions as a Harbor jobs folder so `harbor view` can browse them.

Usage: stage_harbor.py <jobs_dir> <landed_session_dir>... [--job NAME]
Each landed session must already contain trajectory.atif.json (run convert.py first).
Then: harbor view <jobs_dir> --jobs
"""
from __future__ import annotations

import datetime as _dt
import json
import shutil
import sys
import typing
import types
import uuid
from pathlib import Path

from pydantic import BaseModel


def _fill(model, **over):
    kw = {}
    for n, f in model.model_fields.items():
        if n in over:
            kw[n] = over[n]
        elif f.is_required():
            kw[n] = _default(f.annotation)
    return model(**kw)


def _default(a):
    origin = typing.get_origin(a)
    if origin in (typing.Union, types.UnionType):
        return _default([x for x in typing.get_args(a) if x is not type(None)][0])
    if isinstance(a, type) and issubclass(a, BaseModel):
        return _fill(a)
    return {str: "n/a", int: 0, float: 0.0, bool: False}.get(a) if a in (str, int, float, bool) else (
        uuid.uuid4() if a is uuid.UUID else _dt.datetime.now(_dt.timezone.utc) if a is _dt.datetime else
        Path(".") if a is Path else [] if origin in (list, set, tuple) else {} if origin is dict else None)


def _ts(s: str | None) -> _dt.datetime:
    return _dt.datetime.fromisoformat(s.replace("Z", "+00:00")) if s else _dt.datetime.now(_dt.timezone.utc)


def stage(jobs_dir: Path, sessions: list[Path], job: str) -> Path:
    from harbor.models.job.config import JobConfig
    from harbor.models.job.result import JobResult
    from harbor.models.task.id import LocalTaskId
    from harbor.models.trial.config import TaskConfig, TrialConfig
    from harbor.models.trial.result import AgentInfo, TrialResult

    jd = jobs_dir / job
    starts = []
    for sdir in sessions:
        traj = json.loads((sdir / "trajectory.atif.json").read_text())
        start = json.loads((sdir / "start.json").read_text()) if (sdir / "start.json").exists() else {}
        end = json.loads((sdir / "end.json").read_text()) if (sdir / "end.json").exists() else {}
        man = json.loads((sdir / "manifest.json").read_text()) if (sdir / "manifest.json").exists() else {}
        sid = man.get("session_id") or sdir.name
        td = jd / sid
        (td / "agent").mkdir(parents=True, exist_ok=True)
        tcfg = TrialConfig(task=TaskConfig(path=td))
        tr = _fill(TrialResult, task_name=f"{man.get('company', 'session')} {man.get('tool', '')}".strip(),
                   trial_name=sid, trial_uri=str(td), task_id=LocalTaskId(path=td), task_checksum="n/a",
                   config=tcfg, agent_info=AgentInfo(name=traj["agent"]["name"], version=str(traj["agent"].get("version", ""))))
        if start.get("started_at"):
            tr.started_at = _ts(start["started_at"]); starts.append(tr.started_at)
        if end.get("ended_at"):
            tr.finished_at = _ts(end["ended_at"])
        (td / "result.json").write_text(tr.model_dump_json(indent=1))
        (td / "config.json").write_text(tcfg.model_dump_json(indent=1))
        shutil.copy(sdir / "trajectory.atif.json", td / "agent" / "trajectory.json")
        # Artifacts tab: what the session produced, plus the capture sidecars.
        art = td / "artifacts"; art.mkdir(exist_ok=True)
        for name in ("start.json", "end.json", "manifest.json"):
            if (sdir / name).exists():
                shutil.copy(sdir / name, art / name.replace(".json", ".sidecar.json"))
        diff = end.get("final_diff") or (end.get("shadow") or {}).get("diff") or ""
        if diff:
            (art / "final.diff").write_text(diff)
        if start.get("dirty_diff"):
            (art / "dirty-at-start.diff").write_text(start["dirty_diff"])
        commits = end.get("commits_since_base") or []
        if commits:
            (art / "commits.txt").write_text("\n".join(f"{c['sha'][:10]} {c['subject']} <{c.get('author_email','')}>" for c in commits) + "\n")
    jr = _fill(JobResult, n_total_trials=len(sessions), started_at=min(starts) if starts else _dt.datetime.now(_dt.timezone.utc))
    jr.finished_at = _dt.datetime.now(_dt.timezone.utc)
    (jd / "result.json").write_text(jr.model_dump_json(indent=1))
    (jd / "config.json").write_text(JobConfig().model_dump_json(indent=1))
    return jd


def main(argv: list[str]) -> int:
    if len(argv) < 2:
        print(__doc__); return 2
    job = argv[argv.index("--job") + 1] if "--job" in argv else "traj-capture"
    args = [a for i, a in enumerate(argv) if a != "--job" and (i == 0 or argv[i - 1] != "--job")]
    jd = stage(Path(args[0]), [Path(p) for p in args[1:]], job)
    print(f"staged {len(args) - 1} session(s) under {jd}\nharbor view {args[0]} --jobs")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))

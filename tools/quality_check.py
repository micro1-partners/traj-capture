#!/usr/bin/env python3
"""Score a landed traj-capture session against the trajectory-quality symptom list.

Rule-based checks for the properties capture can prove (Faithful, Reconstructable).
Meaningful needs a reviewer; this only flags the mechanical symptoms.
Usage: quality_check.py <landed_session_dir>
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

SYMPTOMS = {
    "incomplete_trace": "Incomplete Trace",
    "truncated": "Truncated Session",
    "unrecoverable_start": "Unrecoverable Starting State",
    "unobservable_outcome": "Unobservable Outcome",
    "no_work": "No Substantive Agent Work",
    "unclear_attribution": "Unclear Attribution",
}


def _load(p: Path) -> dict:
    try:
        return json.loads(p.read_text())
    except (OSError, json.JSONDecodeError):
        return {}


def check(session_dir: Path) -> dict:
    session_dir = Path(session_dir)
    start = _load(session_dir / "start.json")
    end = _load(session_dir / "end.json")
    manifest = _load(session_dir / "manifest.json")
    transcript = session_dir / "transcript.jsonl"
    counts = end.get("counts", {})
    mode = end.get("mode") or start.get("mode")

    checks = {}
    checks["transcript_present"] = transcript.is_file() and transcript.stat().st_size > 0
    checks["manifest_present"] = bool(manifest)
    checks["end_observed"] = end.get("end_state") == "observed"
    if mode == "git":
        checks["start_recoverable"] = (bool(start.get("base_commit")) and bool(start.get("repo_remote"))
                                       and not start.get("dirty_untracked")
                                       and not start.get("dirty_diff_truncated")
                                       and start.get("snapshot_complete", True))
    elif mode == "shadow":
        checks["start_recoverable"] = False  # the local shadow objects are not uploaded
    else:
        checks["start_recoverable"] = False
    if mode == "shadow":
        work = bool((end.get("shadow") or {}).get("diff"))
    elif "agent_diff" in end:
        # agent_diff is start-tree..end-tree: exactly what changed during the session,
        # excluding work that was already dirty when it started.
        work = bool(end.get("agent_diff")) or bool(end.get("commits_since_base"))
    else:
        work = bool(end.get("final_diff")) or bool(end.get("commits_since_base"))
    checks["outcome_observable"] = work
    checks["substantive_work"] = work and int(counts.get("transcript_lines", 0)) >= 10
    checks["attribution_possible"] = bool(start) and ("dirty_diff" in start or mode == "shadow")

    symptoms = []
    if not checks["transcript_present"] or not checks["manifest_present"]:
        symptoms.append(SYMPTOMS["incomplete_trace"])
    if (not checks["end_observed"] or start.get("snapshot_complete") is False
            or start.get("dirty_diff_truncated") or end.get("final_diff_truncated")
            or end.get("agent_diff_truncated")):
        symptoms.append(SYMPTOMS["truncated"])
    if not checks["start_recoverable"]:
        symptoms.append(SYMPTOMS["unrecoverable_start"])
    if not checks["outcome_observable"]:
        symptoms.append(SYMPTOMS["unobservable_outcome"])
    if not checks["substantive_work"]:
        symptoms.append(SYMPTOMS["no_work"])
    if not checks["attribution_possible"]:
        symptoms.append(SYMPTOMS["unclear_attribution"])

    score = round(100 * sum(1 for v in checks.values() if v) / len(checks))
    return {"session_id": manifest.get("session_id") or start.get("session_id"),
            "mode": mode, "checks": checks, "symptoms": symptoms, "score": score, "counts": counts}


def main(argv: list[str]) -> int:
    """Print the quality report for the session dir in argv, or usage if misinvoked."""
    if len(argv) != 1:
        print(__doc__)
        return 2
    print(json.dumps(check(Path(argv[0])), indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))

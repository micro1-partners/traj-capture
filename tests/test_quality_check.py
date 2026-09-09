import importlib.util
import json
from pathlib import Path

TOOL = Path(__file__).resolve().parents[1] / "tools" / "quality_check.py"


def load():
    spec = importlib.util.spec_from_file_location("qc", TOOL)
    m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m); return m


def landed(tmp_path, *, lines=40, commits=1, diff="+x", end_state="observed", mode="git"):
    d = tmp_path / "sess"; d.mkdir()
    (d / "transcript.jsonl").write_text("{}\n" * lines)
    (d / "start.json").write_text(json.dumps({"session_id": "s", "mode": mode, "base_commit": "a" * 40 if mode == "git" else "",
                                              "repo_remote": "https://github.com/x/y.git" if mode == "git" else "",
                                              "shadow_base": "b" * 40 if mode == "shadow" else "", "dirty_diff": ""}))
    (d / "end.json").write_text(json.dumps({"session_id": "s", "end_state": end_state, "mode": mode,
                                            "commits_since_base": [{"sha": "c" * 40}] * commits,
                                            "final_diff": diff, "counts": {"turns": 5, "transcript_lines": lines, "transcript_bytes": lines * 3},
                                            "shadow": {"diff": diff} if mode == "shadow" else None}))
    (d / "manifest.json").write_text(json.dumps({"session_id": "s", "transcript_sha256": "z"}))
    return d


def test_good_session_has_no_symptoms(tmp_path):
    qc = load()
    r = qc.check(landed(tmp_path))
    assert r["symptoms"] == [] and r["score"] == 100


def test_inferred_end_flags_truncated_session(tmp_path):
    qc = load()
    r = qc.check(landed(tmp_path, end_state="inferred"))
    assert "Truncated Session" in r["symptoms"]


def test_no_diff_flags_unobservable_outcome_and_no_work(tmp_path):
    qc = load()
    r = qc.check(landed(tmp_path, commits=0, diff=""))
    assert "Unobservable Outcome" in r["symptoms"]
    assert "No Substantive Agent Work" in r["symptoms"]


def test_missing_start_flags_unrecoverable_state(tmp_path):
    qc = load()
    d = landed(tmp_path); (d / "start.json").unlink()
    r = qc.check(d)
    assert "Unrecoverable Starting State" in r["symptoms"]


def test_shadow_hash_alone_is_not_recoverable(tmp_path):
    qc = load()
    r = qc.check(landed(tmp_path, mode="shadow", commits=0))
    assert "Unrecoverable Starting State" in r["symptoms"]


def test_agent_diff_governs_work_when_present(tmp_path):
    qc = load()
    d = landed(tmp_path, commits=0, diff="+preexisting")
    e = json.loads((d / "end.json").read_text()); e["agent_diff"] = ""; (d / "end.json").write_text(json.dumps(e))
    r = qc.check(d)
    assert "No Substantive Agent Work" in r["symptoms"] and "Unobservable Outcome" in r["symptoms"]

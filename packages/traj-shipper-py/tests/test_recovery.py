import json
import urllib.error

import traj_shipper as ts
from traj_shipper import TrajCapture, _DirSink


def cap(root, **kw):
    return TrajCapture(agent_name="agent", sas_url=f"file://{root / 'blob'}", spool_dir=str(root / "spool"),
                       start_worker=False, flush_on_exit=False, **kw)


def test_partial_failure_retries_after_restart(tmp_path, monkeypatch):
    c = cap(tmp_path)
    orig = _DirSink.put
    def fail_turn(self, rel, *a):
        if rel.endswith("turns/0001.json"):
            raise OSError("synthetic outage")
        return orig(self, rel, *a)
    monkeypatch.setattr(_DirSink, "put", fail_turn)
    c.start("run-0001"); c.turn("run-0001", 1, {"role": "user"}); c.end("run-0001", {"turns": 1})
    c.drain()
    assert not (tmp_path / "spool/run-0001/.receipt").exists()
    assert (tmp_path / "spool/run-0001/turns/0001.json").exists()
    monkeypatch.setattr(_DirSink, "put", orig)
    restored = cap(tmp_path); restored.drain()
    manifest = json.loads((tmp_path / "blob/trajectories/agent/default/run-0001/manifest.json").read_text())
    assert set(manifest["files"]) == {"start.json", "end.json", "turns/0001.json"}
    assert (tmp_path / "spool/run-0001/.receipt").exists()


def test_prior_acknowledgements_survive_restart(tmp_path):
    c = cap(tmp_path)
    c.start("run-0001"); c.turn("run-0001", 1, {"role": "user"}); c.drain()
    assert not (tmp_path / "spool/run-0001/start.json").exists()
    restored = cap(tmp_path); restored.end("run-0001", {"turns": 1}); restored.drain()
    manifest = json.loads((tmp_path / "blob/trajectories/agent/default/run-0001/manifest.json").read_text())
    assert manifest["turns_landed"] == 1 and "start.json" in manifest["files"]


def test_retry_without_restart_and_gap_blocks_receipt(tmp_path, monkeypatch):
    c = cap(tmp_path)
    orig = _DirSink.put
    def fail(self, rel, *a):
        if rel.endswith("turns/0001.json"):
            raise OSError("synthetic outage")
        return orig(self, rel, *a)
    monkeypatch.setattr(_DirSink, "put", fail)
    c.start("run-0001"); c.turn("run-0001", 1, {"role": "user"}); c.end("run-0001", {"turns": 2}); c.drain()
    monkeypatch.setattr(_DirSink, "put", orig)
    c._retry_at = 0; c.drain()
    receipt = tmp_path / "blob/trajectories/_receipts/run-0001.json"
    assert not receipt.exists()
    c.turn("run-0001", 2, {"role": "assistant"}); c._retry_at = 0; c.drain()
    assert receipt.exists()


def test_queue_overflow_with_spool_recovers_all_events(tmp_path):
    c = cap(tmp_path, max_queue=2)
    c.start("run-0001")
    for n in range(1, 6):
        c.turn("run-0001", n, {"role": "user"})
    c.end("run-0001", {"turns": 5})
    for _ in range(6):
        c._retry_at = 0; c.drain()
    restored = cap(tmp_path); restored.drain()
    manifest = json.loads((tmp_path / "blob/trajectories/agent/default/run-0001/manifest.json").read_text())
    assert manifest["turns_landed"] == 5 and manifest["dropped_events"] == 0


def test_spool_refuses_other_deployment(tmp_path):
    c = cap(tmp_path); c.start("run-0001")
    other = TrajCapture(agent_name="agent", sas_url=f"file://{tmp_path / 'other-company'}", spool_dir=str(tmp_path / "spool"),
                        start_worker=False, flush_on_exit=False)
    other.start("other-run"); other.drain()
    assert other._disabled and not (tmp_path / "other-company").exists()


def test_late_feedback_recovered_despite_receipt(tmp_path, monkeypatch):
    c = cap(tmp_path); c.start("run-0001"); c.end("run-0001"); c.drain()
    orig = _DirSink.put
    monkeypatch.setattr(_DirSink, "put", lambda *a: (_ for _ in ()).throw(OSError("offline")))
    c.feedback("run-0001", {"score": 4}); c.drain()
    monkeypatch.setattr(_DirSink, "put", orig)
    restored = cap(tmp_path); restored.drain()
    assert json.loads((tmp_path / "blob/trajectories/agent/default/run-0001/feedback.json").read_text())["score"] == 4


def test_expired_credential_retries_original_event(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(ts, "_enroll", lambda *a: (calls.append(1), {"company": "A", "sas_url": f"file://{tmp_path / 'blob'}", "expires_at": "2099-01-01T00:00:00Z"})[1])
    c = TrajCapture(agent_name="agent", code="synthetic", start_worker=False, flush_on_exit=False)
    orig = _DirSink.put
    def once(self, *a):
        monkeypatch.setattr(_DirSink, "put", orig)
        raise urllib.error.HTTPError("test", 403, "expired", {}, None)
    monkeypatch.setattr(_DirSink, "put", once)
    c.start("run-0001"); c.drain(); c._retry_at = 0; c.drain()
    assert len(calls) == 2 and (tmp_path / "blob/trajectories/agent/default/run-0001/start.json").exists()


def test_reenrollment_cannot_redirect_bound_spool(tmp_path, monkeypatch):
    target = {"company": "A", "sas_url": f"file://{tmp_path / 'A'}", "expires_at": "2099-01-01T00:00:00Z"}
    monkeypatch.setattr(ts, "_enroll", lambda *a: dict(target))
    options = dict(agent_name="agent", code="same-code", spool_dir=str(tmp_path / "spool"),
                   start_worker=False, flush_on_exit=False)
    first = TrajCapture(**options); first.start("run-0001"); first.drain()
    first.turn("run-0001", 1, {"role": "user"})
    target.update(company="B", sas_url=f"file://{tmp_path / 'B'}")
    second = TrajCapture(**options); second.drain()
    assert not (tmp_path / "B").exists()
    assert (tmp_path / "spool/run-0001/turns/0001.json").exists()

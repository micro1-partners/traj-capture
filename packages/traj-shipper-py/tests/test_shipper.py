import json, threading, urllib.error
from pathlib import Path

import pytest

from traj_shipper import TrajCapture, _DirSink


def _cap(tmp_path, **kw):
    sink = tmp_path / "blob"
    kw.setdefault("start_worker", False)
    kw.setdefault("flush_on_exit", False)
    return TrajCapture(agent_name="acme triage", instance="prod-us-1", sas_url=f"file://{sink}", **kw), sink


def _read(p: Path):
    return json.loads(p.read_text())


def test_full_run_lands_every_file_plus_manifest_and_receipt(tmp_path):
    cap, sink = _cap(tmp_path)
    cap.start("run_1", {"model": "claude-sonnet-5", "parent_run_id": None})
    cap.turn("run_1", 1, {"role": "user", "content": "hi"})
    cap.turn("run_1", 2, {"role": "assistant", "content": "hello", "raw_request": {"model": "x"}, "raw_response": {"usage": {}}})
    cap.end("run_1", {"status": "completed", "turns": 2})
    cap.feedback("run_1", {"score": 4})
    cap.drain()
    run = sink / "trajectories/acme-triage/prod-us-1/run_1"
    assert {p.name for p in run.iterdir()} == {"start.json", "turns", "end.json", "manifest.json", "feedback.json"}
    assert {p.name for p in (run / "turns").iterdir()} == {"0001.json", "0002.json"}
    t2 = _read(run / "turns/0002.json")
    assert t2["schema_version"] == 1 and t2["run_id"] == "run_1" and t2["turn"] == 2 and t2["raw_request"] == {"model": "x"}
    start = _read(run / "start.json")
    assert start["agent_name"] == "acme-triage" and start["instance"] == "prod-us-1" and start["model"] == "claude-sonnet-5"
    m = _read(run / "manifest.json")
    assert m["turns_landed"] == 2 and set(m["files"]) == {"start.json", "turns/0001.json", "turns/0002.json", "end.json"}
    assert m["files"]["end.json"]["bytes"] == (run / "end.json").stat().st_size
    r = _read(sink / "trajectories/_receipts/run_1.json")
    assert r["end_state"] == "observed" and r["prefix"] == "trajectories/acme-triage/prod-us-1/run_1"
    assert cap.stats["failed"] == 0 and cap.stats["dropped"] == 0


def test_public_methods_never_raise_even_when_sink_explodes(tmp_path, monkeypatch):
    cap, _ = _cap(tmp_path)

    def boom(self, *a, **k):
        raise OSError("blob down")
    monkeypatch.setattr(_DirSink, "put", boom)
    cap.start("r", {}); cap.turn("r", 1, {}); cap.end("r"); cap.feedback("r", {})
    cap.drain()   # every upload fails, nothing propagates
    assert cap.stats["failed"] == 5 and cap.stats["uploaded"] == 0


def test_bad_input_is_swallowed(tmp_path):
    cap, _ = _cap(tmp_path)
    cap.turn("r", "not-an-int", {"x": object()})   # int() and json fail inside the guard
    cap.drain()
    assert cap.stats["queued"] == 0


def test_queue_drops_oldest_when_full(tmp_path):
    cap, sink = _cap(tmp_path, max_queue=3)
    for i in range(1, 6):
        cap.turn("r", i, {"i": i})
    cap.drain()
    landed = sorted(p.name for p in (sink / "trajectories/acme-triage/prod-us-1/r/turns").iterdir())
    assert landed == ["0003.json", "0004.json", "0005.json"]
    assert cap.stats["dropped"] == 2


def test_spool_survives_restart_and_reships(tmp_path, monkeypatch):
    spool = tmp_path / "spool"
    cap, sink = _cap(tmp_path, spool_dir=str(spool))
    cap.start("r", {}); cap.turn("r", 1, {"a": 1}); cap.end("r")
    # "crash" before the worker ran: spool has files, blob has nothing
    assert (spool / "r" / "turns/0001.json").exists() and (spool / "r" / ".finalize").exists()
    assert not (sink / "trajectories").exists()
    cap2, _ = _cap(tmp_path, spool_dir=str(spool))   # boot sweep re-enqueues
    cap2.drain()
    run = sink / "trajectories/acme-triage/prod-us-1/r"
    assert (run / "turns/0001.json").exists() and (run / "manifest.json").exists()
    assert (sink / "trajectories/_receipts/r.json").exists()
    assert (spool / "r" / ".receipt").exists() and not (spool / "r" / "turns/0001.json").exists()


def test_background_worker_and_flush(tmp_path):
    cap, sink = _cap(tmp_path, start_worker=True)
    cap.start("bg", {}); cap.end("bg")
    assert cap.flush(timeout=5)
    assert (sink / "trajectories/_receipts/bg.json").exists()
    cap.close()


def test_enrolls_with_code_and_persists_config(tmp_path, monkeypatch):
    import traj_shipper as ts
    sink = tmp_path / "blob"
    seen = {}

    def fake_enroll(url, code, host_hash):
        seen.update(url=url, code=code, host=host_hash)
        return {"company": "acme", "sas_url": f"file://{sink}", "expires_at": "2099-01-01T00:00:00Z"}
    monkeypatch.setattr(ts, "_enroll", fake_enroll)
    cfg = tmp_path / "cfg.json"
    cap = TrajCapture(agent_name="a", code="ACME-K7M3-9QZT-4HWX", enroll_url="https://portal.test",
                      config_path=str(cfg), start_worker=False, flush_on_exit=False)
    cap.start("r", {}); cap.drain()
    assert seen == {"url": "https://portal.test", "code": "ACME-K7M3-9QZT-4HWX", "host": seen["host"]}
    assert len(seen["host"]) == 12
    assert json.loads(cfg.read_text())["company"] == "acme"
    assert (sink / "trajectories/a/default/r/start.json").exists()
    # Second instance reuses the persisted credential: no new enrollment.
    seen.clear()
    cap2 = TrajCapture(agent_name="a", code="ACME-K7M3-9QZT-4HWX", config_path=str(cfg), start_worker=False, flush_on_exit=False)
    cap2.start("r2", {}); cap2.drain()
    assert seen == {} and (sink / "trajectories/a/default/r2/start.json").exists()


def test_403_forces_reenroll(tmp_path, monkeypatch):
    import traj_shipper as ts
    sink = tmp_path / "blob"
    calls = []
    monkeypatch.setattr(ts, "_enroll", lambda u, c, h: (calls.append(1), {"sas_url": f"file://{sink}", "expires_at": "2099-01-01T00:00:00Z"})[1])
    cap = TrajCapture(agent_name="a", code="X-1", start_worker=False, flush_on_exit=False)
    orig = ts._DirSink.put
    state = {"fail": True}

    def flaky(self, *a, **k):
        if state["fail"]:
            state["fail"] = False
            raise urllib.error.HTTPError("u", 403, "forbidden", {}, None)
        return orig(self, *a, **k)
    monkeypatch.setattr(ts._DirSink, "put", flaky)
    cap.start("r", {}); cap.drain()          # first put 403s → credential forgotten
    cap.turn("r", 1, {}); cap.drain()        # re-enrolls, succeeds
    assert len(calls) == 2 and cap.stats["failed"] == 1 and cap.stats["uploaded"] == 1

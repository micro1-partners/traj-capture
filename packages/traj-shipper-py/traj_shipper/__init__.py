"""traj-shipper — land production-agent runs in micro1 trajectory storage.

Wire it to your agent framework's events and forget about it:

    from traj_shipper import TrajCapture
    cap = TrajCapture.init(code=os.environ["TRAJ_CODE"], agent_name="acme-triage", instance="prod-us-1")

    cap.start(run_id, {"model": "...", "parent_run_id": None})
    cap.turn(run_id, n, {"role": "assistant", "content": ..., "raw_request": ..., "raw_response": ...})
    cap.end(run_id, {"status": "completed"})
    cap.feedback(run_id, {"score": 4, "expert_id": "exp_7c1"})

Guarantees:
  * Event methods never raise or perform network I/O. With spool_dir they write
    local events and acknowledgements synchronously; failures are logged.
  * Upload failures are retried. With spool_dir pending events and successful
    acknowledgements survive a restart. Without it, memory-only work can be lost.
  * No filesystem access unless you pass `spool_dir`, in which case events are
    also written locally first and re-shipped after a restart.
  * Enrollment (code → SAS) goes to data.micro1.ai; uploads go straight to the
    company's own Azure Blob container. Nothing of micro1's is in your path.

Standard library only. Python 3.9+.
"""
from __future__ import annotations

import atexit
import datetime as _dt
import hashlib
import json
import logging
import os
import socket
import threading
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import deque
from pathlib import Path
from typing import Any, Callable, Optional

__version__ = "0.1.0"
__all__ = ["TrajCapture", "__version__"]

log = logging.getLogger("traj_shipper")

DEFAULT_ENROLL_URL = "https://data.micro1.ai"
SCHEMA_VERSION = 1
REFRESH_MARGIN = _dt.timedelta(days=14)
UPLOAD_RETRIES = 3
FINALIZE = "__finalize__"


def _now_iso() -> str:
    return _dt.datetime.now(_dt.timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _h12(s: str) -> str:
    return hashlib.sha256(s.encode()).hexdigest()[:12]


def _safe_segment(s: str, fallback: str) -> str:
    out = "".join(ch if (ch.isalnum() or ch in "._-") else "-" for ch in str(s or "").strip())
    return out[:128] or fallback


def _atomic_json(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    with tempfile.NamedTemporaryFile(mode="w", prefix=".capture-tmp-", dir=path.parent, delete=False) as f:
        tmp = Path(f.name)
        try:
            json.dump(data, f, sort_keys=True, separators=(",", ":"))
            f.flush()
            os.fsync(f.fileno())
            os.replace(tmp, path)
        finally:
            tmp.unlink(missing_ok=True)


# ── sinks ────────────────────────────────────────────────────────────────────


class _Sink:
    def put(self, rel_path: str, data: bytes, content_type: str = "application/json") -> None:
        raise NotImplementedError


class _DirSink(_Sink):
    """`file://` target — local development and tests."""

    def __init__(self, root: Path):
        self.root = root

    def put(self, rel_path: str, data: bytes, content_type: str = "application/json") -> None:
        p = self.root / rel_path
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(data)


class _BlobSink(_Sink):
    """PUT block blobs with a container SAS. Same retry policy as the
    traj-capture plugin: three attempts with backoff on 408/429/5xx and
    network errors; any other 4xx is a caller bug and is not retried."""

    def __init__(self, sas_url: str):
        base, _, query = sas_url.partition("?")
        self.base, self.query = base.rstrip("/"), query

    def put(self, rel_path: str, data: bytes, content_type: str = "application/json") -> None:
        url = f"{self.base}/{urllib.parse.quote(rel_path)}?{self.query}"
        headers = {"x-ms-blob-type": "BlockBlob", "Content-Type": content_type, "Content-Length": str(len(data))}
        last: Optional[Exception] = None
        for attempt in range(UPLOAD_RETRIES):
            req = urllib.request.Request(url, data=data, method="PUT", headers=headers)
            try:
                with urllib.request.urlopen(req, timeout=120) as resp:
                    if 200 <= resp.status < 300:
                        return
                    last = RuntimeError(f"unexpected status {resp.status}")
            except urllib.error.HTTPError as exc:
                last = exc
                if exc.code not in (408, 429, 500, 502, 503, 504):
                    raise
            except (urllib.error.URLError, OSError) as exc:
                last = exc
            time.sleep(2 ** attempt)
        assert last is not None
        raise last


def _make_sink(sas_url: str) -> _Sink:
    if sas_url.startswith("file://"):
        return _DirSink(Path(sas_url[len("file://"):]))
    return _BlobSink(sas_url)


# ── enrollment ───────────────────────────────────────────────────────────────


def _enroll(enroll_url: str, code: str, host_hash: str) -> dict:
    body = json.dumps({"code": code, "host_hash": host_hash, "client_version": f"traj-shipper-py/{__version__}"}).encode()
    req = urllib.request.Request(f"{enroll_url.rstrip('/')}/api/traj/enroll", data=body, method="POST",
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=30) as resp:
        data = json.loads(resp.read().decode())
    if not data.get("sas_url"):
        raise RuntimeError("enroll response missing sas_url")
    return data


class _Credential:
    """Holds the SAS and re-enrolls near expiry. Optionally persists to
    `config_path` so restarts don't burn an enrollment."""

    def __init__(self, *, code: Optional[str], enroll_url: str, config_path: Optional[Path],
                 sas_url: Optional[str] = None, expires_at: Optional[str] = None):
        self.code, self.enroll_url, self.config_path = code, enroll_url, config_path
        self.sas_url, self.expires_at = sas_url, expires_at
        self.company: Optional[str] = None
        self._lock = threading.Lock()
        self._host_hash = _h12(socket.gethostname())
        self._code_hash = hashlib.sha256(code.encode()).hexdigest() if code else None
        if not self.sas_url and config_path and config_path.is_file():
            try:
                cfg = json.loads(config_path.read_text())
                if not code or cfg.get("code_hash") == self._code_hash:
                    self.sas_url, self.expires_at, self.company = cfg.get("sas_url"), cfg.get("expires_at"), cfg.get("company")
            except (OSError, ValueError):
                pass
        self._destination = self.sas_url.partition("?")[0] if self.sas_url else None

    def _expiring(self) -> bool:
        if not self.expires_at:
            return False
        try:
            exp = _dt.datetime.fromisoformat(self.expires_at.replace("Z", "+00:00"))
        except ValueError:
            return False
        return exp - _dt.datetime.now(_dt.timezone.utc) < REFRESH_MARGIN

    def sink(self) -> _Sink:
        with self._lock:
            if (not self.sas_url or self._expiring()) and self.code:
                data = _enroll(self.enroll_url, self.code, self._host_hash)
                if ((self.company and data.get("company") != self.company) or (
                        self._destination and data["sas_url"].partition("?")[0] != self._destination)):
                    raise RuntimeError("credential refresh changed company or destination")
                self.sas_url, self.expires_at, self.company = data["sas_url"], data.get("expires_at"), data.get("company")
                self._destination = self.sas_url.partition("?")[0]
                if self.config_path:
                    try:
                        self.config_path.parent.mkdir(parents=True, exist_ok=True)
                        _atomic_json(self.config_path, {"company": self.company, "sas_url": self.sas_url,
                                                       "expires_at": self.expires_at, "code_hash": self._code_hash})
                    except OSError as exc:
                        log.warning("traj-shipper: could not persist config: %r", exc)
                log.info("traj-shipper: enrolled company=%s expires=%s", self.company, self.expires_at)
            if not self.sas_url:
                raise RuntimeError("traj-shipper: no credential (pass code= or sas_url=)")
            return _make_sink(self.sas_url)

    def forget(self) -> None:
        """Called after a 403 from blob: force a re-enroll on the next put."""
        with self._lock:
            self.sas_url = None


# ── the capture client ───────────────────────────────────────────────────────


class TrajCapture:
    def __init__(
        self,
        *,
        agent_name: str,
        instance: str = "default",
        code: Optional[str] = None,
        enroll_url: Optional[str] = None,
        config_path: Optional[str] = None,
        sas_url: Optional[str] = None,
        spool_dir: Optional[str] = None,
        max_queue: int = 10_000,
        flush_on_exit: bool = True,
        start_worker: bool = True,
    ):
        self.agent_name = _safe_segment(agent_name, "agent")
        self.instance = _safe_segment(instance, "default")
        enroll_url = enroll_url or os.environ.get("TRAJ_CAPTURE_ENROLL_URL") or DEFAULT_ENROLL_URL
        cfg_path = Path(config_path) if config_path else (
            Path(os.environ["TRAJ_CAPTURE_CONFIG"]) if os.environ.get("TRAJ_CAPTURE_CONFIG") else None)
        self._cred = _Credential(code=code, enroll_url=enroll_url, config_path=cfg_path, sas_url=sas_url)
        self._spool = Path(spool_dir) if spool_dir else None
        self._q: deque = deque()
        self._max = max_queue
        self._lock = threading.RLock()
        self._wake = threading.Event()
        self._stop = threading.Event()
        self._idle = threading.Event()
        self._idle.set()
        self._runs: dict[str, dict[str, dict]] = {}   # run_id -> {name: {sha256, bytes}}
        self._records: dict[str, dict] = {}
        self._retry: dict[tuple, Optional[bytes]] = {}
        self._retry_at = 0.0
        self._retry_delay = 0.5
        self._disabled = False
        self._binding = {"agent": self.agent_name, "instance": self.instance, "enroll_url": enroll_url,
                         "credential": hashlib.sha256(("code:" + code if code else
                             "destination:" + (self._cred._destination or "unconfigured")).encode()).hexdigest()}
        self.stats = {"queued": 0, "uploaded": 0, "dropped": 0, "failed": 0}
        self._worker: Optional[threading.Thread] = None
        if self._spool:
            try:
                bp = self._spool / ".binding.json"
                if bp.exists():
                    saved = json.loads(bp.read_text())
                    if any(saved.get(k) != v for k, v in self._binding.items()):
                        raise ValueError("spool belongs to another deployment or enrollment")
                    self._binding = saved
                if not bp.exists() and self._spool.exists() and any(self._spool.iterdir()):
                    raise ValueError("legacy spool is unbound; retain it for explicit reconciliation")
                _atomic_json(bp, self._binding)
                self._sweep_spool()
            except Exception as exc:
                self._disabled = True
                log.warning("traj-shipper: capture disabled: %s", exc)
        if start_worker:
            self._worker = threading.Thread(target=self._run, name="traj-shipper", daemon=True)
            self._worker.start()
        if flush_on_exit:
            atexit.register(self.flush, 2.0)

    @classmethod
    def init(cls, **kw) -> "TrajCapture":
        return cls(**kw)

    # Public event methods isolate errors; durable spooling performs local disk I/O.

    def start(self, run_id: str, meta: Optional[dict] = None) -> None:
        self._guard(self._start, run_id, meta)

    def turn(self, run_id: str, n: int, turn: dict) -> None:
        self._guard(self._turn, run_id, n, turn)

    def end(self, run_id: str, status: Optional[dict] = None) -> None:
        self._guard(self._end, run_id, status)

    def feedback(self, run_id: str, fb: dict) -> None:
        self._guard(self._feedback, run_id, fb)

    def _start(self, run_id: str, meta: Optional[dict]) -> None:
        body = {"schema_version": SCHEMA_VERSION, "run_id": run_id, "agent_name": self.agent_name,
                "instance": self.instance, "started_at": _now_iso(),
                "client": {"name": "traj-shipper-py", "version": __version__}, **(meta or {})}
        self._enqueue(run_id, "start.json", body)

    def _turn(self, run_id: str, n: int, turn: dict) -> None:
        if isinstance(n, bool) or int(n) != float(n) or int(n) < 1:
            raise ValueError("turn must be a positive integer")
        n = int(n)
        body = {"schema_version": SCHEMA_VERSION, "run_id": run_id, "turn": n, "ts": _now_iso(), **(turn or {})}
        self._enqueue(run_id, f"turns/{n:04d}.json", body)

    def _end(self, run_id: str, status: Optional[dict]) -> None:
        body = {"schema_version": SCHEMA_VERSION, "run_id": run_id, "ended_at": _now_iso(),
                "status": "completed", **(status or {})}
        self._enqueue(run_id, "end.json", body)
        self._enqueue(run_id, FINALIZE, None)

    def _feedback(self, run_id: str, fb: dict) -> None:
        body = {"schema_version": SCHEMA_VERSION, "run_id": run_id, "scored_at": _now_iso(), **(fb or {})}
        self._enqueue(run_id, "feedback.json", body)

    def flush(self, timeout: float = 2.0) -> bool:
        """Best-effort wait for the queue to drain. Returns True if empty."""
        self._wake.set()
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if self._idle.wait(min(0.05, max(0, deadline - time.monotonic()))):
                if not self._q and not self._retry:
                    return True
                time.sleep(min(0.01, max(0, deadline - time.monotonic())))
        return not self._q and not self._retry and self._idle.is_set()

    def close(self, timeout: float = 2.0) -> None:
        self.flush(timeout)
        self._stop.set()
        self._wake.set()

    # ── internals ───────────────────────────────────────────────────────────

    def _guard(self, fn: Callable, *a: Any) -> None:
        try:
            fn(*a)
        except Exception as exc:  # noqa: BLE001 — capture must never surface
            log.warning("traj-shipper: %s failed: %r", getattr(fn, "__name__", fn), exc)

    def _prefix(self, run_id: str) -> str:
        return f"trajectories/{self.agent_name}/{self.instance}/{_safe_segment(run_id, 'run')}"

    def _enqueue(self, run_id: str, name: str, body: Optional[dict]) -> None:
        if self._disabled:
            return
        if not isinstance(run_id, str) or run_id in (".", "..") or _safe_segment(run_id, "run") != run_id:
            raise ValueError("run_id must be a nonempty URL-safe identifier")
        data = None if body is None else json.dumps(body, sort_keys=True, separators=(",", ":")).encode()
        with self._lock:
            record = self._record(run_id)
            if data is not None:
                signature = {"sha256": hashlib.sha256(data).hexdigest(), "bytes": len(data)}
                old = record["expected"].get(name)
                if old and old != signature and name != "feedback.json":
                    record["lost"] = True
                    self._save(run_id)
                    raise ValueError("conflicting event for the same run/file")
                if self._spool:
                    _atomic_json(self._spool / run_id / name, body)
                    # Send exactly the persisted bytes, including after a restart.
                    data = (self._spool / run_id / name).read_bytes()
                    signature = {"sha256": hashlib.sha256(data).hexdigest(), "bytes": len(data)}
                record["expected"][name] = signature
                if name == "end.json":
                    record["turns_expected"] = body.get("turns")
            else:
                record["ended"] = True
                if self._spool:
                    (self._spool / run_id / ".finalize").touch(mode=0o600)
            self._save(run_id)
            self._enqueue_raw(run_id, name, data)
        self._wake.set()

    def _record(self, run_id: str) -> dict:
        if run_id not in self._records:
            p = self._spool / run_id / ".capture.json" if self._spool else None
            record = json.loads(p.read_text()) if p and p.exists() else {
                "expected": {}, "files": {}, "ended": False, "lost": False, "turns_expected": None}
            self._records[run_id] = record
            self._runs[run_id] = record["files"]
        return self._records[run_id]

    def _save(self, run_id: str) -> None:
        if self._spool:
            _atomic_json(self._spool / run_id / ".capture.json", self._records[run_id])

    def _sweep_spool(self) -> None:
        """Re-enqueue anything spooled but never acknowledged."""
        assert self._spool is not None
        if not self._spool.is_dir():
            return
        for run_dir in sorted(p for p in self._spool.iterdir() if p.is_dir()):
            if (run_dir / ".receipt").exists():
                feedback = run_dir / "feedback.json"
                if feedback.exists():
                    self._enqueue_raw(run_dir.name, "feedback.json", feedback.read_bytes())
                continue
            record = self._record(run_dir.name)
            files = sorted(p for p in run_dir.rglob("*") if p.is_file() and not p.name.startswith("."))
            for f in files:
                name = str(f.relative_to(run_dir))
                data = f.read_bytes()
                sig = {"sha256": hashlib.sha256(data).hexdigest(), "bytes": len(data)}
                record["expected"].setdefault(name, sig)
                if name == "end.json":
                    record["turns_expected"] = json.loads(data).get("turns")
                    record["ended"] = True
                if record["files"].get(name) != record["expected"][name]:
                    self._enqueue_raw(run_dir.name, name, data)
            self._save(run_dir.name)
            if record["ended"] or (run_dir / ".finalize").exists():
                self._enqueue_raw(run_dir.name, FINALIZE, None)
            if files:
                log.info("traj-shipper: re-shipping %d spooled file(s) for %s", len(files), run_dir.name)

    def _enqueue_raw(self, run_id: str, name: str, data: Optional[bytes]) -> None:
        with self._lock:
            if len(self._q) >= self._max:
                old_run, old_name, _ = self._q.popleft()
                if not self._spool:
                    self._record(old_run)["lost"] = True
                else:
                    self._retry[(old_run, old_name)] = None  # bytes remain in the spool
                self.stats["dropped"] += 1
            self._q.append((run_id, name, data))
            self.stats["queued"] += 1
            self._idle.clear()
        self._wake.set()

    def _run(self) -> None:
        while not self._stop.is_set():
            self._wake.wait(1.0)
            self._wake.clear()
            self.drain()

    def drain(self) -> None:
        """Upload everything queued. Called by the worker; tests call it directly."""
        if self._disabled:
            return
        with self._lock:
            if time.monotonic() >= self._retry_at:
                for (rid, name), data in list(self._retry.items()):
                    if len(self._q) >= self._max:
                        break
                    del self._retry[(rid, name)]
                    if data is None and name != FINALIZE and self._spool:
                        p = self._spool / rid / name
                        if p.exists():
                            data = p.read_bytes()
                    self._q.append((rid, name, data))
                    self._idle.clear()
        while True:
            with self._lock:
                if not self._q:
                    self._idle.set()
                    return
                run_id, name, data = self._q.popleft()
            try:
                if name == FINALIZE:
                    self._finalize(run_id)
                else:
                    self._put(run_id, name, data)
                self.stats["uploaded"] += 1
            except Exception as exc:  # noqa: BLE001
                self.stats["failed"] += 1
                log.warning("traj-shipper: upload %s/%s failed: %r", run_id, name, exc)
                with self._lock:
                    self._retry_delay = min(60.0, self._retry_delay * 2)
                    self._retry_at = time.monotonic() + self._retry_delay
                    if self._spool or len(self._retry) < self._max:
                        self._retry[(run_id, name)] = None if self._spool else data
                    else:
                        self._record(run_id)["lost"] = True
                if isinstance(exc, urllib.error.HTTPError) and exc.code == 403:
                    self._cred.forget()   # expired/revoked SAS: re-enroll next time

    def _put(self, run_id: str, name: str, data: bytes) -> None:
        if data is None:
            raise RuntimeError("pending event bytes unavailable")
        self._sink().put(f"{self._prefix(run_id)}/{name}", data, "application/json")
        with self._lock:
            record = self._record(run_id)
            record["files"][name] = {"sha256": hashlib.sha256(data).hexdigest(), "bytes": len(data)}
            self._save(run_id)  # acknowledge durably before removing the event
            if self._spool and record["files"][name] == record["expected"].get(name):
                (self._spool / run_id / name).unlink(missing_ok=True)
            self._retry.pop((run_id, name), None)
            self._retry_delay = 0.5
            if name == "feedback.json" and (record.get("complete") or not record["ended"]):
                self._records.pop(run_id, None)
                self._runs.pop(run_id, None)

    def _finalize(self, run_id: str) -> None:
        record = self._record(run_id)
        files = {k: v for k, v in record["files"].items() if k != "feedback.json"}
        expected = {k: v for k, v in record["expected"].items() if k != "feedback.json"}
        if record["lost"] or files != expected or not {"start.json", "end.json"} <= files.keys():
            raise RuntimeError("capture incomplete; receipt deferred")
        turns = [n for n in files if n.startswith("turns/")]
        n = record["turns_expected"]
        if n is None:
            n = len(turns)
        if not isinstance(n, int) or n < 0 or set(turns) != {f"turns/{i:04d}.json" for i in range(1, n + 1)}:
            raise RuntimeError("capture has missing turns; receipt deferred")
        manifest = {"schema_version": SCHEMA_VERSION, "run_id": run_id, "agent_name": self.agent_name,
                    "instance": self.instance, "client": {"name": "traj-shipper-py", "version": __version__},
                    "turns_landed": len(turns), "turns_expected": n, "dropped_events": 0, "files": files,
                    "finalized_at": _now_iso()}
        sink = self._sink()
        prefix = self._prefix(run_id)
        sink.put(f"{prefix}/manifest.json", json.dumps(manifest, sort_keys=True, indent=1).encode())
        receipt = {"schema_version": SCHEMA_VERSION, "run_id": run_id, "agent_name": self.agent_name,
                   "prefix": prefix, "end_state": "observed" if "end.json" in files else "inferred",
                   "completed_at": _now_iso(), "client_version": f"traj-shipper-py/{__version__}"}
        sink.put(f"trajectories/_receipts/{_safe_segment(run_id, 'run')}.json",
                 json.dumps(receipt, sort_keys=True, indent=1).encode())
        self._retry.pop((run_id, FINALIZE), None)
        record["complete"] = True
        self._save(run_id)
        if self._spool:
            d = self._spool / _safe_segment(run_id, "run")
            try:
                (d / ".receipt").write_bytes(b"")
                (d / ".finalize").unlink(missing_ok=True)
            except OSError:
                pass
        self._records.pop(run_id, None)
        self._runs.pop(run_id, None)

    def _sink(self) -> _Sink:
        sink = self._cred.sink()
        if self._spool:
            actual = {"destination": self._cred._destination, "company": self._cred.company}
            with self._lock:
                if "destination" in self._binding and any(self._binding.get(k) != v for k, v in actual.items()):
                    raise RuntimeError("spool storage binding changed; uploads withheld")
                if "destination" not in self._binding:
                    self._binding.update(actual)
                    _atomic_json(self._spool / ".binding.json", self._binding)
        return sink

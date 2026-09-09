#!/usr/bin/env python3
"""traj-capture: capture coding-agent sessions raw into CDP storage.

Verbs (argv[1]): start | turn | end | worker | sweep | probe | setup | refresh | install-codex
Hook JSON arrives on stdin for start/turn/end. Standard library only.
"""
from __future__ import annotations

import datetime as _dt
import fnmatch
import hashlib
import json
import os
import socket
import shlex
import subprocess
import sys
import tarfile
import time
import tempfile
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

# ---------------------------------------------------------------- constants
PLUGIN_VERSION = "0.2.0"
PROVIDER_BY_TOOL = {"claude_code": "anthropic", "codex": "openai"}
GIT_TIMEOUT = 20
DIRTY_DIFF_CAP = 2 * 1024 * 1024
FINAL_DIFF_CAP = 10 * 1024 * 1024
SHADOW_FILE_CAP = 5 * 1024 * 1024
SHADOW_MAX_FILES = 20000
IDLE_SECONDS = 30 * 60
UPLOAD_RETRIES = 3
START_SNAPSHOT_SECONDS = 5
_snapshot_deadline = None
_snapshot_timed_out = False
DEFAULT_ENROLL_URL = "https://data.micro1.ai"   # company-facing portal; CDP itself is never reachable from a client
SHADOW_EXCLUDES = [".git/", "node_modules/", ".venv/", "venv/", "__pycache__/", ".DS_Store"]
TELEMETRY_ENV = {  # Claude Code writes full API request/response bodies (system prompt, tools, messages) here
    "CLAUDE_CODE_ENABLE_TELEMETRY": "1",
    "OTEL_LOG_RAW_API_BODIES": "file:{raw_dir}",
    "OTEL_LOG_USER_PROMPTS": "1",
    "OTEL_LOG_TOOL_DETAILS": "1",
    "OTEL_LOG_TOOL_CONTENT": "1",
}


def now_iso() -> str:
    return _dt.datetime.now(_dt.timezone.utc).isoformat(timespec="seconds")


# ------------------------------------------------------------------ logging
def state_root() -> Path:
    p = os.environ.get("TRAJ_CAPTURE_STATE")
    root = Path(p) if p else Path.home() / ".traj-capture"
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    return root


def log(msg: str) -> None:
    try:
        with open(state_root() / "capture.log", "a") as fh:
            fh.write(f"{now_iso()} {msg}\n")
    except OSError:
        pass


# ------------------------------------------------------------------- config
def plugin_root() -> Path:
    return Path(__file__).resolve().parent.parent


def config_path() -> Path:
    """One credential location across hosts; legacy plugin-local configs are not adopted."""
    p = os.environ.get("TRAJ_CAPTURE_CONFIG")
    if p:
        return Path(p)
    return Path.home() / ".traj-capture" / "config.json"


def enroll_code_file() -> Path:
    """Drop the enrollment code here and the plugin enrolls itself on the next
    session start, then deletes the file. One line for an engineer in any tool
    (`echo CODE > ~/.traj-capture/enroll-code`) and the seam IT uses to
    pre-provision machines."""
    p = os.environ.get("TRAJ_CAPTURE_ENROLL_CODE_FILE")
    return Path(p) if p else Path.home() / ".traj-capture" / "enroll-code"


def maybe_auto_enroll() -> bool:
    """If there is a pending enroll-code file and no usable config, enroll now.
    Never raises: a failure is logged and the file is left for the next try."""
    f = enroll_code_file()
    if not f.is_file():
        return False
    try:
        if config_path().exists() and load_config().get("sas_url"):
            f.unlink()  # already enrolled; a stale file is just noise
            return False
    except (OSError, json.JSONDecodeError):
        pass
    try:
        code = f.read_text().strip()
        if not code:
            return False
        _, host_hash = identity()
        enroll_url = enroll_url_from_env()
        data = enroll(enroll_url, code, host_hash)
        _write_config_from_enroll({}, data, code, enroll_url)
        f.unlink()
        log(f"auto-enroll: enrolled company={data.get('company')} from {f}")
        return True
    except Exception as exc:  # noqa: BLE001 — hooks must never break the agent
        log(f"auto-enroll failed ({exc!r}); leaving {f} for the next session")
        return False


def load_config() -> dict:
    with open(config_path()) as fh:
        cfg = json.load(fh)
    cfg.setdefault("repo_allowlist", [])
    cfg.setdefault("capture_non_repo", True)
    cfg.setdefault("enabled", True)
    cfg.setdefault("tools_enabled", ["claude_code", "codex"])
    return cfg


# --------------------------------------------------------------- hook input
def read_hook_input(text: str | None) -> dict:
    if text is None:
        try:
            text = sys.stdin.read()
        except OSError:
            text = ""
    text = (text or "").strip()
    if not text:
        return {}
    try:
        data = json.loads(text)
        return data if isinstance(data, dict) else {}
    except json.JSONDecodeError:
        return {}


# --------------------------------------------------------------------- sink
def split_sas_url(url: str) -> tuple[str, str]:
    base, _, query = url.partition("?")
    return base.rstrip("/"), query


class Sink:
    def put(self, rel_path: str, data: bytes, content_type: str = "application/octet-stream") -> None:
        raise NotImplementedError


class DirSink(Sink):
    def __init__(self, root: Path):
        self.root = root

    def put(self, rel_path: str, data: bytes, content_type: str = "application/octet-stream") -> None:
        p = self.root / rel_path
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(data)


class BlobSink(Sink):
    def __init__(self, sas_url: str):
        self.base, self.query = split_sas_url(sas_url)
        self.before_put = lambda: None

    def put(self, rel_path: str, data: bytes, content_type: str = "application/octet-stream") -> None:
        url = f"{self.base}/{urllib.parse.quote(rel_path)}?{self.query}"
        headers = {"x-ms-blob-type": "BlockBlob", "Content-Type": content_type}
        last: Exception | None = None
        for attempt in range(UPLOAD_RETRIES):
            self.before_put()
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
            except urllib.error.URLError as exc:
                last = exc
            time.sleep(2 ** attempt)
        assert last is not None
        raise last


def make_sink(sas_url: str) -> Sink:
    if sas_url.startswith("file://"):
        return DirSink(Path(sas_url[len("file://"):]))
    return BlobSink(sas_url)


# ---------------------------------------------------------------------- git
def run(cmd: list[str], cwd: Path | str, timeout: int = GIT_TIMEOUT, env: dict | None = None) -> str | None:
    global _snapshot_timed_out
    if _snapshot_deadline is not None:
        remaining = _snapshot_deadline - time.monotonic()
        if remaining <= 0:
            _snapshot_timed_out = True
            return None
        timeout = min(timeout, remaining)
    try:
        r = subprocess.run(cmd, cwd=str(cwd), capture_output=True, text=True, timeout=timeout, env=env)
    except (OSError, subprocess.TimeoutExpired) as exc:
        if isinstance(exc, subprocess.TimeoutExpired) and _snapshot_deadline is not None:
            _snapshot_timed_out = True
        log(f"run failed {cmd[:3]}: {exc!r}")
        return None
    if r.returncode != 0:
        return None
    return r.stdout.strip()


def cap_text(s: str, cap: int) -> tuple[str, bool]:
    if len(s) <= cap:
        return s, False
    return s[:cap], True


def git_info(cwd: Path | str) -> dict | None:
    if not Path(cwd).is_dir():
        return None
    root = run(["git", "rev-parse", "--show-toplevel"], cwd)
    if not root:
        return None
    return {
        "root": root,
        "remote": run(["git", "remote", "get-url", "origin"], root) or "",
        "branch": run(["git", "rev-parse", "--abbrev-ref", "HEAD"], root) or "",
        "head": run(["git", "rev-parse", "HEAD"], root) or "",
    }


def git_dirty_diff(root: Path | str) -> dict:
    diff = run(["git", "diff", "--binary", "HEAD"], root) or ""
    untracked = run(["git", "ls-files", "--others", "--exclude-standard"], root) or ""
    diff, truncated = cap_text(diff, DIRTY_DIFF_CAP)
    return {"diff": diff, "untracked": [u for u in untracked.splitlines() if u], "truncated": truncated}


def git_commits_since(root: Path | str, base: str) -> list[dict]:
    if not base:
        return []
    user_email = run(["git", "config", "user.email"], root) or ""
    out = run(["git", "log", f"{base}..HEAD", "--format=%H%x1f%s%x1f%ae"], root) or ""
    commits = []
    for line in out.splitlines():
        parts = line.split("\x1f")
        if len(parts) != 3:
            continue
        sha, subject, email = parts
        commits.append({"sha": sha, "subject": subject, "author_email": email,
                        "author_is_user": bool(user_email) and email == user_email})
    return commits


def git_final_diff(root: Path | str, base: str) -> dict:
    tree = git_worktree_tree(root, "final-diff", "end", pin=False)
    return git_tree_diff(root, base, tree)


def git_worktree_tree(root: Path | str, sid: str, label: str, pin: bool = True) -> str:
    """Snapshot the whole working tree (tracked + untracked, honoring .gitignore) as a git
    tree object without touching the real index or worktree, and pin it under
    refs/traj-capture/<sid>/<label> so gc keeps it. Returns the tree sha ('' on failure)."""
    import tempfile
    root = str(root)
    with tempfile.NamedTemporaryFile(prefix="traj-idx-", delete=False) as fh:
        idx = fh.name
    try:
        os.unlink(idx)
        env = dict(os.environ, GIT_INDEX_FILE=idx)
        if run(["git", "add", "-A", "."], root, timeout=120, env=env) is None:
            return ""
        tree = run(["git", "write-tree"], root, env=env) or ""
        if tree and pin:
            env2 = dict(os.environ, GIT_AUTHOR_NAME="traj-capture", GIT_AUTHOR_EMAIL="traj-capture@micro1.ai",
                        GIT_COMMITTER_NAME="traj-capture", GIT_COMMITTER_EMAIL="traj-capture@micro1.ai")
            commit = run(["git", "commit-tree", tree, "-m", f"traj-capture {label} {sid}"], root, env=env2) or ""
            if commit:
                run(["git", "update-ref", f"refs/traj-capture/{sid}/{label}", commit], root)
        return tree
    finally:
        try:
            os.unlink(idx)
        except OSError:
            pass


def git_tree_diff(root: Path | str, tree_a: str, tree_b: str) -> dict:
    if not tree_a or not tree_b:
        return {"diff": "", "truncated": False}
    diff = run(["git", "diff", "--binary", tree_a, tree_b], root) or ""
    diff, truncated = cap_text(diff, FINAL_DIFF_CAP)
    return {"diff": diff, "truncated": truncated}


# ------------------------------------------------------------------- shadow
def _shadow_env() -> dict:
    env = dict(os.environ)
    env.setdefault("GIT_AUTHOR_NAME", "traj-capture")
    env.setdefault("GIT_AUTHOR_EMAIL", "traj-capture@micro1.ai")
    env.setdefault("GIT_COMMITTER_NAME", "traj-capture")
    env.setdefault("GIT_COMMITTER_EMAIL", "traj-capture@micro1.ai")
    return env


def _shadow_cmd(cwd: Path, shadow_git: Path, *args: str) -> str | None:
    return run(["git", f"--git-dir={shadow_git}", f"--work-tree={cwd}", *args], cwd, timeout=120, env=_shadow_env())


def _write_shadow_excludes(cwd: Path, shadow_git: Path) -> bool:
    """Write info/exclude with fixed patterns plus oversize files. Returns partial flag."""
    lines = list(SHADOW_EXCLUDES)
    count = 0
    partial = False
    for dirpath, dirnames, filenames in os.walk(cwd):
        if _snapshot_deadline is not None and time.monotonic() >= _snapshot_deadline:
            partial = True
            break
        rel_dir = os.path.relpath(dirpath, cwd)
        dirnames[:] = [d for d in dirnames if f"{d}/" not in SHADOW_EXCLUDES]
        for fn in filenames:
            count += 1
            if count > SHADOW_MAX_FILES:
                partial = True
                break
            p = Path(dirpath) / fn
            try:
                if p.is_file() and p.stat().st_size > SHADOW_FILE_CAP:
                    rel = fn if rel_dir == "." else f"{rel_dir}/{fn}"
                    lines.append("/" + rel)
            except OSError:
                continue
        if partial:
            break
    (shadow_git / "info").mkdir(parents=True, exist_ok=True)
    (shadow_git / "info" / "exclude").write_text("\n".join(lines) + "\n")
    return partial


def shadow_manifest(cwd: Path, shadow_git: Path) -> dict:
    out = _shadow_cmd(cwd, shadow_git, "ls-files", "-s") or ""
    files = []
    for line in out.splitlines():
        meta, _, path = line.partition("\t")
        parts = meta.split()
        if len(parts) < 2 or not path:
            continue
        try:
            size = (cwd / path).stat().st_size
        except OSError:
            size = None
        files.append({"path": path, "size": size, "sha": parts[1]})
    return {"files": files, "partial": len(files) >= SHADOW_MAX_FILES}


def shadow_init(cwd: Path, shadow_git: Path) -> dict:
    shadow_git.mkdir(parents=True, exist_ok=True)
    _shadow_cmd(cwd, shadow_git, "init", "-q")
    partial = _write_shadow_excludes(cwd, shadow_git)
    _shadow_cmd(cwd, shadow_git, "add", "-A")
    _shadow_cmd(cwd, shadow_git, "commit", "-q", "--allow-empty", "-m", "traj-capture start")
    base = _shadow_cmd(cwd, shadow_git, "rev-parse", "HEAD") or ""
    manifest = shadow_manifest(cwd, shadow_git)
    return {"base": base, "manifest": manifest, "partial": partial or manifest["partial"]}


def shadow_diff(cwd: Path, shadow_git: Path, base: str) -> dict:
    _write_shadow_excludes(cwd, shadow_git)
    _shadow_cmd(cwd, shadow_git, "add", "-A")
    diff = _shadow_cmd(cwd, shadow_git, "diff", "--cached", base) or ""
    diff, truncated = cap_text(diff, FINAL_DIFF_CAP)
    return {"diff": diff, "truncated": truncated, "manifest": shadow_manifest(cwd, shadow_git)}


# -------------------------------------------------------------------- state
def _h12(s: str) -> str:
    return hashlib.sha256(s.encode()).hexdigest()[:12]


def identity() -> tuple[str, str]:
    email = run(["git", "config", "user.email"], Path.home()) or ""
    host = socket.gethostname()
    user = email or f"{os.environ.get('USER', 'unknown')}@{host}"
    return _h12(user), _h12(host)


def codex_home() -> Path:
    return Path(os.environ.get("CODEX_HOME") or (Path.home() / ".codex"))


def codex_rollout_path(sid: str) -> str:
    """Codex's SessionStart/Stop payload may carry `transcript_path: null`.
    The transcript is the rollout file `$CODEX_HOME/sessions/YYYY/MM/DD/
    rollout-<ts>-<session_id>.jsonl`; find it by session id. Empty string
    when not (yet) written — the caller re-resolves on the next hook."""
    root = codex_home() / "sessions"
    if not sid or not root.is_dir():
        return ""
    matches = sorted(root.glob(f"*/*/*/rollout-*-{sid}.jsonl"))
    return str(matches[-1]) if matches else ""


def resolve_transcript_path(tool: str, hook: dict, sid: str) -> str:
    tpath = hook.get("transcript_path") or ""
    if not tpath and tool == "codex":
        tpath = codex_rollout_path(sid)
    return tpath


def binding(cfg: dict) -> dict:
    company = cfg.get("company")
    destination = split_sas_url(cfg.get("sas_url", ""))[0]
    if not company or not destination:
        raise ValueError("capture needs a company and destination")
    return {"company": company, "destination": destination}


def company_root(cfg: dict) -> Path:
    key = hashlib.sha256(json.dumps(binding(cfg), sort_keys=True).encode()).hexdigest()[:24]
    return state_root() / "companies" / key


def session_dir(tool: str, sid: str, cfg: dict | None = None) -> Path:
    if tool not in PROVIDER_BY_TOOL or not sid or Path(sid).name != sid or sid in (".", ".."):
        raise ValueError("invalid session identity")
    d = company_root(cfg or load_config()) / "sessions" / tool / sid
    d.mkdir(parents=True, exist_ok=True, mode=0o700)
    return d


def remote_prefix(tool: str, user_hash: str, sid: str) -> str:
    return f"trajectories/{tool}/{user_hash}/{sid}"


def read_json(p: Path) -> dict:
    try:
        with open(p) as fh:
            data = json.load(fh)
        return data if isinstance(data, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def write_json(p: Path, data: dict) -> None:
    p.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    with tempfile.NamedTemporaryFile(mode="w", dir=p.parent, delete=False) as fh:
        tmp = Path(fh.name)
        try:
            json.dump(data, fh, indent=1, sort_keys=True)
            fh.flush()
            os.fsync(fh.fileno())
            os.replace(tmp, p)
        finally:
            tmp.unlink(missing_ok=True)


def sha256_file(p: Path) -> str:
    h = hashlib.sha256()
    with open(p, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def find_subagent_files(transcript_path: Path, sid: str) -> list[Path]:
    base = transcript_path.parent / sid
    if not base.is_dir():
        return []
    return sorted(p for p in base.rglob("*.jsonl") if p.is_file())


def allowed(remote: str, allowlist: list[str]) -> bool:
    if not allowlist:
        return True
    return any(fnmatch.fnmatch(remote, pat) for pat in allowlist)


def tool_from_args(rest: list[str]) -> str:
    if "--tool" in rest:
        i = rest.index("--tool")
        if i + 1 < len(rest):
            return rest[i + 1]
    return "claude_code"


def detect_tool(hook: dict) -> str:
    """Which agent fired this hook. One hooks.json serves both Claude Code
    and Codex (Codex aliases CLAUDE_PLUGIN_ROOT for bundled plugins), so the
    command says `--tool auto` and we tell them apart by the transcript:
    Codex's is a `rollout-*.jsonl` under $CODEX_HOME/sessions."""
    tp = str(hook.get("transcript_path") or "")
    if tp:
        if Path(tp).name.startswith("rollout-") or str(codex_home()) in tp:
            return "codex"
        return "claude_code"
    sid = hook.get("session_id") or ""
    if sid and codex_rollout_path(sid):
        return "codex"
    if os.environ.get("CLAUDE_PROJECT_DIR") or os.environ.get("CLAUDE_CODE_VERSION"):
        return "claude_code"
    return "codex" if os.environ.get("CODEX_HOME") else "claude_code"


def resolve_tool(tool: str, hook: dict) -> str:
    return detect_tool(hook) if tool == "auto" else tool


def arg_value(rest: list[str], flag: str) -> str | None:
    if flag in rest:
        i = rest.index(flag)
        if i + 1 < len(rest):
            return rest[i + 1]
    return None


# ---------------------------------------------------------------- raw api
def raw_api_dir(cfg: dict) -> Path | None:
    if not cfg.get("telemetry"):
        return None
    d = Path(cfg.get("raw_api_dir") or (state_root() / "raw-api"))
    d.mkdir(parents=True, exist_ok=True)
    return d


def _request_session_id(p: Path) -> str | None:
    try:
        with open(p) as fh:
            body = json.load(fh)
        meta = body.get("metadata") or {}
        uid = meta.get("user_id")
        if isinstance(uid, str) and uid.startswith("{"):
            uid = json.loads(uid)
        if isinstance(uid, dict):
            return uid.get("session_id")
    except (OSError, ValueError):
        return None
    return None


def collect_raw_api(raw_dir: Path, sdir: Path, sid: str) -> int:
    """Associate responses only by explicit request_id (never file timestamps).

    Providers without a correlation ID leave responses unassigned in raw_dir.
    Already-collected requests remain searchable so late responses can join them.
    """
    dest = sdir / "raw-api"
    dest.mkdir(exist_ok=True)
    requests = []
    responses = []
    owners = {}
    for p in list(raw_dir.iterdir()) + list(dest.glob("*.request.json")):
        if not p.is_file():
            continue
        if p.name.endswith(".request.json"):
            owner = _request_session_id(p)
            body = read_json(p)
            rid = body.get("request_id") or body.get("_request_id") or (body.get("metadata") or {}).get("request_id")
            if isinstance(rid, str) and rid:
                owners.setdefault(rid, set()).add(owner)
            requests.append((p, owner))
        elif p.name.endswith(".response.json"):
            responses.append(p)
    moved = 0
    for p, owner in requests:
        if owner == sid and p.parent != dest:
            os.replace(p, dest / p.name)
            moved += 1
    for p in responses:
        body = read_json(p)
        rid = body.get("request_id") or body.get("_request_id")
        if isinstance(rid, str) and owners.get(rid) == {sid}:
            os.replace(p, dest / p.name)
            moved += 1
    return moved


RAW_ORPHAN_MAX_AGE = 24 * 3600


def prune_raw_api(raw_dir: Path, known_sessions: set[str]) -> int:
    """Delete raw bodies older than a day that belong to no captured session (the env
    var is machine-wide, so uncaptured or skipped sessions also write here). Returns count."""
    now = time.time()
    removed = 0
    orphan_requests = []
    for p in list(raw_dir.iterdir()):
        if not p.is_file():
            continue
        try:
            age = now - p.stat().st_mtime
        except OSError:
            continue
        if age < RAW_ORPHAN_MAX_AGE:
            continue
        if p.name.endswith(".request.json"):
            if _request_session_id(p) in known_sessions:
                continue
            orphan_requests.append(p)
        elif p.name.endswith(".response.json"):
            continue  # ambiguous or late responses require explicit retention review
        else:
            continue
        try:
            p.unlink(); removed += 1
        except OSError:
            pass
    return removed


def pack_raw_api(sdir: Path) -> Path | None:
    src = sdir / "raw-api"
    if not src.is_dir() or not any(src.iterdir()):
        return None
    out = sdir / "raw_api.tar.gz"
    with tarfile.open(out, "w:gz") as tar:
        for p in sorted(src.iterdir()):
            if p.is_file():
                tar.add(p, arcname=f"raw-api/{p.name}")
    return out


def settings_path() -> Path:
    p = os.environ.get("TRAJ_CAPTURE_SETTINGS")
    return Path(p) if p else Path.home() / ".claude" / "settings.json"


def set_telemetry(enable: bool, raw_dir: Path) -> dict:
    """Merge (or remove) the raw-body env vars in Claude Code's user settings. Backs up first."""
    sp = settings_path()
    settings = read_json(sp) if sp.exists() else {}
    if sp.exists():
        backup = sp.with_name(sp.name + ".traj-capture.bak")
        if not backup.exists():
            backup.write_bytes(sp.read_bytes())
    env = dict(settings.get("env") or {})
    if enable:
        for k, v in TELEMETRY_ENV.items():
            env[k] = v.format(raw_dir=str(raw_dir))
    else:
        for k in TELEMETRY_ENV:
            env.pop(k, None)
    if env:
        settings["env"] = env
    else:
        settings.pop("env", None)
    sp.parent.mkdir(parents=True, exist_ok=True)
    write_json(sp, settings)
    return env


VERBS: dict[str, object] = {}

# -------------------------------------------------------------------- verbs
def detach(args: list[str]) -> None:
    """Run capture.py <args> detached from the hook process, or inline for tests."""
    if os.environ.get("TRAJ_CAPTURE_INLINE") == "1":
        main(args, stdin_text="{}")
        return
    try:
        subprocess.Popen(
            [sys.executable, str(Path(__file__).resolve()), *args],
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            start_new_session=True, close_fds=True, env=dict(os.environ),
        )
    except OSError as exc:
        log(f"detach failed {args}: {exc!r}")


def capture_enabled(cfg: dict, tool: str | None = None) -> bool:
    return cfg.get("enabled", True) and (tool is None or tool in cfg.get("tools_enabled", []))


class PolicySink(Sink):
    def __init__(self, cfg: dict, tool: str | None):
        self.expected = binding(cfg)
        self.tool = tool

    def check(self) -> dict:
        current = load_config()
        if not capture_enabled(current, self.tool) or binding(current) != self.expected:
            raise RuntimeError("capture paused or company binding changed")
        return current

    def put(self, rel_path: str, data: bytes, content_type: str = "application/octet-stream") -> None:
        sink = make_sink(self.check()["sas_url"])
        if isinstance(sink, BlobSink):
            sink.before_put = self.check
        sink.put(rel_path, data, content_type)


def _sink(cfg: dict, tool: str | None = None) -> Sink:
    return PolicySink(cfg, tool)


def bound_session(cfg: dict, meta: dict, tool: str) -> bool:
    return capture_enabled(cfg, tool) and meta.get("binding") == binding(cfg)


def cmd_start(rest: list[str], stdin_text: str | None) -> int:
    global _snapshot_deadline, _snapshot_timed_out
    try:
        cfg = load_config()
    except (OSError, ValueError):
        cfg = {}
    if cfg.get("enabled") is False:
        return 0
    if not cfg.get("company") or not cfg.get("sas_url"):
        detach(["enroll"])
        log("start: enrollment scheduled; capture begins with the next new session")
        return 0
    _snapshot_deadline = time.monotonic() + START_SNAPSHOT_SECONDS
    _snapshot_timed_out = False
    try:
        return _cmd_start(rest, stdin_text)
    finally:
        _snapshot_deadline = None


def _cmd_start(rest: list[str], stdin_text: str | None) -> int:
    cfg = load_config()
    hook = read_hook_input(stdin_text)
    tool = resolve_tool(tool_from_args(rest), hook)
    if not cfg.get("enabled", True) or tool not in cfg.get("tools_enabled", []):
        return 0
    sid = hook.get("session_id")
    cwd = hook.get("cwd") or os.getcwd()
    tpath = resolve_transcript_path(tool, hook, sid or "")
    source = hook.get("source") or "startup"
    if not sid:
        log("start: no session_id in hook input")
        return 0
    sdir = session_dir(tool, sid)
    # Never rebind a transcript from another company or an unbound legacy capture.
    others = state_root().glob(f"companies/*/sessions/{tool}/{sid}/meta.json")
    if any(p.parent != sdir for p in others) or (state_root() / "sessions" / tool / sid).exists():
        log(f"start {sid}: existing session requires its original company binding")
        return 0
    if source in ("resume", "compact") and not (sdir / "meta.json").exists():
        log(f"start {sid}: cannot capture an unbound continuation")
        return 0
    user_hash, host_hash = identity()

    # Continuation of a session we already snapshotted.
    start_path = sdir / "start.json"
    if start_path.exists() and source in ("resume", "compact"):
        start = read_json(start_path)
        start.setdefault("continuations", []).append({"source": source, "at": now_iso()})
        write_json(start_path, start)
        meta = read_json(sdir / "meta.json")
        if not bound_session(cfg, meta, tool):
            return 0
        _reopen(sdir)
        (sdir / "end.marker").unlink(missing_ok=True)
        meta["touched_at"] = now_iso()
        if tpath:
            meta["transcript_path"] = tpath
        write_json(sdir / "meta.json", meta)
        detach(["sweep"])
        return 0

    info = git_info(cwd)
    start = {
        "session_id": sid, "tool": tool, "provider": PROVIDER_BY_TOOL.get(tool, "unknown"),
        "tool_version": hook.get("tool_version") or os.environ.get("CLAUDE_CODE_VERSION") or "",
        "model": hook.get("model") or "", "source": source, "parent_session_id": hook.get("parent_session_id"),
        "cwd": cwd, "started_at": now_iso(), "user_hash": user_hash, "host_hash": host_hash,
        "plugin_version": PLUGIN_VERSION, "continuations": [],
    }
    meta = {"tool": tool, "session_id": sid, "transcript_path": tpath, "cwd": cwd, "turns": 0,
            "uploads": {}, "started_at": start["started_at"], "touched_at": start["started_at"],
            "binding": binding(cfg), "user_hash": user_hash, "host_hash": host_hash}

    if _snapshot_timed_out:
        start["mode"] = meta["mode"] = "unavailable"
    elif info:
        if not allowed(info["remote"], cfg.get("repo_allowlist", [])):
            (sdir / "skipped").write_text(f"remote not allowlisted: {info['remote']}\n")
            log(f"start {sid}: skipped, remote not allowlisted")
            return 0
        dirty = git_dirty_diff(info["root"])
        start_tree = git_worktree_tree(info["root"], sid, "start")
        start.update({"mode": "git", "repo_root": info["root"], "repo_remote": info["remote"],
                      "branch": info["branch"], "base_commit": info["head"], "start_tree": start_tree,
                      "dirty_diff": dirty["diff"], "dirty_untracked": dirty["untracked"],
                      "dirty_diff_truncated": dirty["truncated"]})
        meta.update({"mode": "git", "repo_root": info["root"], "base_commit": info["head"], "start_tree": start_tree})
    elif cfg.get("capture_non_repo", True):
        shadow = shadow_init(Path(cwd), sdir / "shadow.git")
        start.update({"mode": "shadow", "shadow_base": shadow["base"],
                      "shadow_manifest": shadow["manifest"], "shadow_partial": shadow["partial"],
                      "start_recoverable": False})
        meta.update({"mode": "shadow", "shadow_base": shadow["base"]})
    else:
        (sdir / "skipped").write_text("not a git repo and capture_non_repo is false\n")
        log(f"start {sid}: skipped, non-repo capture disabled")
        return 0

    start["snapshot_complete"] = not _snapshot_timed_out and bool(
        start.get("start_tree") or (start.get("shadow_base") and not start.get("shadow_partial")))
    write_json(start_path, start)
    write_json(sdir / "meta.json", meta)
    log(f"start {sid}: mode={start['mode']} source={source}")
    detach(["turnwork", "--tool", tool, "--session", sid])
    detach(["sweep"])
    return 0


def upload_transcript_set(cfg: dict, sdir: Path, meta: dict, tool: str, sid: str, user_hash: str) -> dict:
    if not bound_session(cfg, meta, tool):
        raise RuntimeError("unbound or paused session")
    sink = _sink(cfg, tool)
    prefix = remote_prefix(tool, user_hash, sid)
    start_bytes = (sdir / "start.json").read_bytes()
    start_sha = hashlib.sha256(start_bytes).hexdigest()
    if meta.setdefault("uploads", {}).get("start.json") != start_sha:
        sink.put(f"{prefix}/start.json", start_bytes, "application/json")
        meta["uploads"]["start.json"] = start_sha
    if not Path(meta.get("transcript_path") or "").is_file():
        meta["transcript_path"] = resolve_transcript_path(tool, {}, sid)
    tpath = Path(meta.get("transcript_path") or "")
    if not tpath.is_file() or not tpath.stat().st_size:
        return meta
    targets = [("transcript.jsonl", tpath)]
    for sub in find_subagent_files(tpath, sid):
        targets.append((f"subagents/{sub.name}", sub))
    uploads = meta.setdefault("uploads", {})
    for relname, path in targets:
        data = path.read_bytes()
        sha = hashlib.sha256(data).hexdigest()
        if uploads.get(relname) == sha:
            continue
        sink.put(f"{prefix}/{relname}", data, "application/x-ndjson")
        uploads[relname] = sha
    return meta


def _reopen(sdir: Path) -> None:
    """A finalized session got new activity: drop the receipt so it is finalized again."""
    r = sdir / "receipt.json"
    if r.exists():
        n = 1
        while (sdir / f"receipt.v{n}.json").exists():
            n += 1
        os.replace(r, sdir / f"receipt.v{n}.json")
        log(f"reopen {sdir.name}: new activity after finalize (v{n})")


def cmd_turn(rest: list[str], stdin_text: str | None) -> int:
    hook = read_hook_input(stdin_text)
    tool = resolve_tool(tool_from_args(rest), hook)
    sid = hook.get("session_id")
    if not sid:
        return 0
    sdir = session_dir(tool, sid)
    meta_path = sdir / "meta.json"
    if not meta_path.exists():
        return 0
    meta = read_json(meta_path)
    if not bound_session(load_config(), meta, tool):
        return 0
    tpath = resolve_transcript_path(tool, hook, sid)
    if tpath:
        meta["transcript_path"] = tpath
    meta["turns"] = int(meta.get("turns", 0)) + 1
    meta["touched_at"] = now_iso()
    write_json(meta_path, meta)
    _reopen(sdir)
    (sdir / "end.marker").unlink(missing_ok=True)
    log(f"turn {sid}: n={meta['turns']}")
    detach(["turnwork", "--tool", tool, "--session", sid, "--ppid", str(os.getppid())])
    return 0


def cmd_turnwork(rest: list[str], stdin_text: str | None) -> int:
    tool, sid = tool_from_args(rest), arg_value(rest, "--session")
    if not sid:
        return 0
    sdir = session_dir(tool, sid)
    if not _acquire_lock(sdir):
        return 0
    try:
        return _cmd_turnwork(rest, stdin_text)
    finally:
        _release_lock(sdir)
        if (sdir / "end.marker").exists() and not (sdir / "receipt.json").exists():
            detach(["worker", "--tool", tool, "--session", sid])


def _cmd_turnwork(rest: list[str], stdin_text: str | None) -> int:
    cfg = load_config()
    tool = tool_from_args(rest)
    sid = arg_value(rest, "--session")
    if not sid:
        return 0
    sdir = session_dir(tool, sid)
    meta_path = sdir / "meta.json"
    if not meta_path.exists():
        return 0
    meta = read_json(meta_path)
    if not bound_session(cfg, meta, tool):
        return 0
    user_hash = meta["user_hash"]
    try:
        meta = upload_transcript_set(cfg, sdir, meta, tool, sid, user_hash)
    except Exception as exc:
        log(f"turnwork {sid}: upload failed {exc!r}")
    rd = raw_api_dir(cfg)
    if rd:
        try:
            collect_raw_api(rd, sdir, sid)
        except Exception as exc:
            log(f"turnwork {sid}: raw-api collect failed {exc!r}")
    # merge only the uploads map so a concurrent turn's counter is not clobbered
    current = read_json(meta_path)
    current["uploads"] = meta.get("uploads", {})
    if meta.get("transcript_path"):
        current["transcript_path"] = meta["transcript_path"]
    write_json(meta_path, current)
    ppid = arg_value(rest, "--ppid")
    if ppid and ppid.isdigit():
        _ensure_waiter(sdir, tool, sid, int(ppid))
    return 0


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True


def _ensure_waiter(sdir: Path, tool: str, sid: str, agent_pid: int) -> None:
    """One detached process per session watches the agent pid; when it exits without a
    SessionEnd (headless runs, crashes, kills) it records an observed end and finalizes."""
    pidfile = sdir / "waiter.pid"
    if pidfile.exists():
        try:
            if _pid_alive(int(pidfile.read_text().strip() or 0)):
                return
        except ValueError:
            pass
    if os.environ.get("TRAJ_CAPTURE_INLINE") == "1":
        return  # tests call cmd_wait directly
    detach(["wait", "--tool", tool, "--session", sid, "--pid", str(agent_pid)])


def cmd_wait(rest: list[str], stdin_text: str | None) -> int:
    tool = tool_from_args(rest)
    sid = arg_value(rest, "--session")
    pid = arg_value(rest, "--pid")
    if not sid or not pid or not pid.isdigit():
        return 0
    sdir = session_dir(tool, sid)
    if not (sdir / "meta.json").exists():
        return 0
    (sdir / "waiter.pid").write_text(str(os.getpid()))
    interval = float(os.environ.get("TRAJ_CAPTURE_WAIT_INTERVAL", "5"))
    while _pid_alive(int(pid)):
        if not bound_session(load_config(), read_json(sdir / "meta.json"), tool):
            return 0
        time.sleep(interval)
    time.sleep(min(interval, 3))  # give a real SessionEnd hook a moment to land first
    if (sdir / "receipt.json").exists():
        return 0
    if not (sdir / "end.marker").exists():
        meta = read_json(sdir / "meta.json")
        head = run(["git", "rev-parse", "HEAD"], meta.get("repo_root") or meta.get("cwd") or ".", timeout=2) or ""
        write_json(sdir / "end.marker", {"reason": "process_exit", "ended_at": now_iso(), "head": head})
        log(f"wait {sid}: agent pid {pid} exited without SessionEnd; marking process_exit")
    try:
        finalize_session(load_config(), tool, sid, inferred=False)
    except Exception as exc:
        log(f"wait {sid}: finalize failed {exc!r} (sweep will retry)")
    return 0


def cmd_end(rest: list[str], stdin_text: str | None) -> int:
    hook = read_hook_input(stdin_text)
    tool = resolve_tool(tool_from_args(rest), hook)
    sid = hook.get("session_id")
    if not sid:
        return 0
    sdir = session_dir(tool, sid)
    if not (sdir / "meta.json").exists():
        return 0
    meta = read_json(sdir / "meta.json")
    if not bound_session(load_config(), meta, tool):
        return 0
    tpath = resolve_transcript_path(tool, hook, sid)
    if tpath:
        meta["transcript_path"] = tpath
        write_json(sdir / "meta.json", meta)
    head = run(["git", "rev-parse", "HEAD"], meta.get("repo_root") or meta.get("cwd") or ".", timeout=2) or ""
    write_json(sdir / "end.marker", {"reason": hook.get("reason") or "other", "ended_at": now_iso(), "head": head})
    _reopen(sdir)
    log(f"end {sid}: reason={hook.get('reason') or 'other'}")
    detach(["worker", "--tool", tool, "--session", sid])
    return 0


def _count_lines(p: Path) -> int:
    n = 0
    with open(p, "rb") as fh:
        for _ in fh:
            n += 1
    return n


def _acquire_lock(sdir: Path, name: str = "finalizing.lock", stale_after: int = 600) -> bool:
    lock = sdir / name
    try:
        if lock.exists():
            owner = lock.read_text().strip()
            if owner.isdigit() and not _pid_alive(int(owner)):
                lock.unlink()
            elif not owner and (time.time() - lock.stat().st_mtime) > stale_after:
                lock.unlink()
        fd = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        os.write(fd, str(os.getpid()).encode())
        os.close(fd)
        return True
    except FileExistsError:
        return False
    except OSError:
        return False  # retry later rather than race another writer


def _release_lock(sdir: Path, name: str = "finalizing.lock") -> None:
    try:
        (sdir / name).unlink()
    except OSError:
        pass


def finalize_session(cfg: dict, tool: str, sid: str, inferred: bool) -> bool:
    sdir = session_dir(tool, sid, cfg)
    meta_path = sdir / "meta.json"
    if not meta_path.exists() or (sdir / "receipt.json").exists():
        return False
    if not bound_session(cfg, read_json(meta_path), tool):
        return False
    if not _acquire_lock(sdir):
        log(f"finalize {sid}: already in progress, skipping")
        return False
    try:
        return _finalize_locked(cfg, tool, sid, inferred, sdir, meta_path)
    finally:
        _release_lock(sdir)


def _finalize_locked(cfg: dict, tool: str, sid: str, inferred: bool, sdir: Path, meta_path: Path) -> bool:
    meta = read_json(meta_path)
    start = read_json(sdir / "start.json")
    marker = read_json(sdir / "end.marker")
    user_hash, host_hash = meta["user_hash"], meta["host_hash"]
    prefix = remote_prefix(tool, user_hash, sid)
    sink = _sink(cfg, tool)

    meta = upload_transcript_set(cfg, sdir, meta, tool, sid, user_hash)
    tpath = Path(meta.get("transcript_path") or "")
    if not tpath.is_file() or not tpath.stat().st_size:
        meta["capture_pending"] = "missing_transcript"
        write_json(meta_path, meta)
        return False
    meta.pop("capture_pending", None)
    counts = {"turns": int(meta.get("turns", 0)),
              "transcript_bytes": tpath.stat().st_size if tpath.is_file() else 0,
              "transcript_lines": _count_lines(tpath) if tpath.is_file() else 0}
    endj = {"session_id": sid, "reason": marker.get("reason") or "inferred",
            "ended_at": marker.get("ended_at") or now_iso(),
            "end_state": "inferred" if inferred or not marker else "observed",
            "mode": meta.get("mode"), "counts": counts,
            "snapshot_complete": start.get("snapshot_complete", False)}
    cwd = meta.get("cwd") or "."
    if meta.get("mode") == "git":
        root = meta.get("repo_root") or cwd
        base = meta.get("base_commit") or ""
        final = git_final_diff(root, base)
        end_tree = git_worktree_tree(root, sid, "end")
        agent = git_tree_diff(root, meta.get("start_tree") or "", end_tree)
        endj.update({"head_commit": run(["git", "rev-parse", "HEAD"], root) or "",
                     "commits_since_base": git_commits_since(root, base),
                     "final_diff": final["diff"], "final_diff_truncated": final["truncated"],
                     "end_tree": end_tree, "agent_diff": agent["diff"], "agent_diff_truncated": agent["truncated"]})
    elif meta.get("mode") == "shadow":
        endj["shadow"] = shadow_diff(Path(cwd), sdir / "shadow.git", meta.get("shadow_base") or "")
    write_json(sdir / "end.json", endj)
    end_bytes = json.dumps(endj, indent=1, sort_keys=True).encode()
    sink.put(f"{prefix}/end.json", end_bytes, "application/json")

    raw_tar = None
    rd = raw_api_dir(cfg)
    if rd:
        try:
            collect_raw_api(rd, sdir, sid)
            raw_tar = pack_raw_api(sdir)
            if raw_tar:
                sink.put(f"{prefix}/raw_api.tar.gz", raw_tar.read_bytes(), "application/gzip")
        except Exception as exc:
            log(f"finalize {sid}: raw-api failed {exc!r}")
            raise  # required enabled artifacts must land before the receipt

    files = {}
    for relname in ["start.json", "end.json", "raw_api.tar.gz"]:
        p = sdir / relname
        if p.is_file() and (relname != "raw_api.tar.gz" or raw_tar):
            files[relname] = {"sha256": sha256_file(p), "bytes": p.stat().st_size}
    if raw_tar:
        files["raw_api.tar.gz"]["count"] = sum(1 for _ in (sdir / "raw-api").iterdir())
    for relname, sha in meta.get("uploads", {}).items():
        if relname == "start.json":
            continue
        path = tpath if relname == "transcript.jsonl" else tpath.parent / sid / "subagents" / Path(relname).name
        files[relname] = {"sha256": sha, "bytes": path.stat().st_size if path.is_file() else None}
    current = read_json(meta_path)
    if current.get("turns") != meta.get("turns") or sha256_file(tpath) != meta["uploads"].get("transcript.jsonl"):
        raise RuntimeError("session changed during finalization; retry before issuing receipt")
    manifest = {"session_id": sid, "company": cfg.get("company"), "tool": tool,
                "provider": PROVIDER_BY_TOOL.get(tool, "unknown"), "model": start.get("model", ""),
                "plugin_version": PLUGIN_VERSION, "tool_version": start.get("tool_version", ""),
                "user_hash": user_hash, "host_hash": host_hash,
                "started_at": start.get("started_at"), "ended_at": endj["ended_at"],
                "files": files, "transcript_sha256": meta.get("uploads", {}).get("transcript.jsonl"),
                "raw_response_attribution": "explicit_request_id_only" if rd else "disabled"}
    write_json(sdir / "manifest.json", manifest)
    sink.put(f"{prefix}/manifest.json", json.dumps(manifest, indent=1, sort_keys=True).encode(), "application/json")

    receipt = {"session_id": sid, "tool": tool, "user_hash": user_hash, "host_hash": host_hash,
               "prefix": prefix, "end_state": endj["end_state"], "counts": counts,
               "completed_at": now_iso(), "plugin_version": PLUGIN_VERSION}
    sink.put(f"trajectories/_receipts/{sid}.json", json.dumps(receipt, indent=1, sort_keys=True).encode(), "application/json")
    write_json(sdir / "receipt.json", receipt)
    write_json(meta_path, meta)
    log(f"finalized {sid}: end_state={endj['end_state']} turns={counts['turns']}")
    return True


def cmd_worker(rest: list[str], stdin_text: str | None) -> int:
    cfg = load_config()
    tool = tool_from_args(rest)
    sid = arg_value(rest, "--session")
    if not sid:
        return 0
    inferred = "--inferred" in rest
    try:
        finalize_session(cfg, tool, sid, inferred)
    except Exception as exc:
        log(f"worker {sid}: failed {exc!r} (sweep will retry)")
    return 0


def _parse_ts(s: str | None) -> float:
    try:
        return _dt.datetime.fromisoformat((s or "").replace("Z", "+00:00")).timestamp()
    except ValueError:
        return time.time()


def cmd_sweep(rest: list[str], stdin_text: str | None) -> int:
    cfg = load_config()
    if not capture_enabled(cfg):
        return 0
    maybe_refresh(cfg)
    cfg = load_config()
    sessions = company_root(cfg) / "sessions"
    if sessions.is_dir():
        now = time.time()
        for tool_dir in sorted(p for p in sessions.iterdir() if p.is_dir()):
            for sdir in sorted(p for p in tool_dir.iterdir() if p.is_dir()):
                if (sdir / "receipt.json").exists() or not (sdir / "meta.json").exists():
                    continue
                meta = read_json(sdir / "meta.json")
                if not bound_session(cfg, meta, tool_dir.name):
                    continue
                try:
                    cmd_turnwork(["--tool", tool_dir.name, "--session", sdir.name], "{}")
                except Exception as exc:
                    log(f"sweep upload {sdir.name}: {exc!r}")
                meta = read_json(sdir / "meta.json")
                has_marker = (sdir / "end.marker").exists()
                tpath = Path(meta.get("transcript_path") or "")
                if tpath.is_file():
                    idle = (now - tpath.stat().st_mtime) > IDLE_SECONDS
                else:
                    # Transcript not written yet (fresh session) or gone: go by our own
                    # last-touched time so a brand-new session is never finalized early.
                    idle = (now - _parse_ts(meta.get("touched_at") or meta.get("started_at"))) > IDLE_SECONDS
                if not (has_marker or idle):
                    continue
                try:
                    finalize_session(cfg, tool_dir.name, sdir.name, inferred=not has_marker)
                except Exception as exc:
                    log(f"sweep {sdir.name}: failed {exc!r}")
    rd = raw_api_dir(cfg)
    if rd:
        try:
            known = {d.name for tool_dir in sessions.iterdir() if tool_dir.is_dir() for d in tool_dir.iterdir()
                     if d.is_dir() and (d / "meta.json").exists()} if sessions.is_dir() else set()
            n = prune_raw_api(rd, known)
            if n:
                log(f"sweep: pruned {n} orphan raw API files")
        except Exception as exc:
            log(f"sweep prune: {exc!r}")
    try:
        maybe_refresh(cfg)
    except Exception as exc:
        log(f"sweep refresh: {exc!r}")
    return 0


def cmd_probe(rest: list[str], stdin_text: str | None) -> int:
    cfg = load_config()
    user_hash, host_hash = identity()
    body = {"probe": True, "company": cfg.get("company"), "host_hash": host_hash,
            "user_hash": user_hash, "plugin_version": PLUGIN_VERSION, "at": now_iso()}
    try:
        _sink(cfg).put(f"trajectories/_probe/{host_hash}.json", json.dumps(body).encode(), "application/json")
    except Exception as exc:
        print(f"traj-capture probe failed: {exc}")
        return 1
    print(f"traj-capture probe ok: company={cfg.get('company')} host={host_hash} user={user_hash}")
    return 0


def enroll_url_from_env() -> str:
    return os.environ.get("TRAJ_CAPTURE_ENROLL_URL") or os.environ.get("TRAJ_CAPTURE_CDP_URL") or DEFAULT_ENROLL_URL


def enroll_url_from_config(cfg: dict) -> str:
    return cfg.get("enroll_url") or cfg.get("cdp_url") or DEFAULT_ENROLL_URL


def enroll(enroll_url: str, code: str, host_hash: str) -> dict:
    body = json.dumps({"code": code, "host_hash": host_hash, "plugin_version": PLUGIN_VERSION}).encode()
    req = urllib.request.Request(f"{enroll_url.rstrip('/')}/api/traj/enroll", data=body, method="POST",
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=30) as resp:
        data = json.loads(resp.read().decode())
    if not data.get("sas_url"):
        raise RuntimeError("enroll response missing sas_url")
    return data


def _write_config_from_enroll(existing: dict, data: dict, code: str, enroll_url: str) -> dict:
    if existing.get("company") and existing["company"] != data.get("company"):
        # Do not inherit the previous company's capture policy or raw-body directory.
        existing = {"enabled": existing.get("enabled", True)}
    cfg = dict(existing)
    cfg.pop("cdp_url", None)  # legacy key; the portal, not CDP, is the enrollment host
    cfg.update({"company": data.get("company", cfg.get("company")), "sas_url": data["sas_url"],
                "sas_expires_at": data.get("expires_at", ""), "enrollment_code": code, "enroll_url": enroll_url})
    cfg["repo_allowlist"] = data.get("repo_allowlist", cfg.get("repo_allowlist", []))
    cfg["capture_non_repo"] = data.get("capture_non_repo", cfg.get("capture_non_repo", True))
    cfg.setdefault("enabled", True)
    cfg["tools_enabled"] = data.get("tools_enabled", cfg.get("tools_enabled", ["claude_code", "codex"]))
    write_json(config_path(), cfg)
    return cfg


def cmd_setup(rest: list[str], stdin_text: str | None) -> int:
    code = arg_value(rest, "--code")
    enroll_url = enroll_url_from_env()
    if code:
        _, host_hash = identity()
        try:
            existing = load_config() if config_path().exists() else {}
        except (OSError, json.JSONDecodeError):
            existing = {}
        try:
            data = enroll(enroll_url, code, host_hash)
        except Exception as exc:
            print(f"traj-capture setup failed: enrollment rejected ({exc})")
            return 1
        _write_config_from_enroll(existing, data, code, enroll_url)
        log(f"setup: enrolled company={data.get('company')}")
    tel = arg_value(rest, "--telemetry")
    if tel in ("on", "off"):
        try:
            cfg = load_config() if config_path().exists() else {}
        except (OSError, json.JSONDecodeError):
            cfg = {}
        rd = Path(cfg.get("raw_api_dir") or (state_root() / "raw-api"))
        rd.mkdir(parents=True, exist_ok=True)
        cfg["telemetry"] = tel == "on"
        cfg["raw_api_dir"] = str(rd)
        write_json(config_path(), cfg)
        set_telemetry(tel == "on", rd)
        log(f"setup: telemetry {tel} (raw bodies -> {rd})")
        print(f"traj-capture telemetry {tel}: raw API bodies {'will be' if tel == 'on' else 'no longer'} captured (takes effect on the next Claude Code launch)")
    rc = cmd_probe([], "{}")
    try:
        lines = (state_root() / "capture.log").read_text().splitlines()[-20:]
        print("\n".join(lines))
    except OSError:
        pass
    return rc


def maybe_refresh(cfg: dict) -> bool:
    exp = cfg.get("sas_expires_at") or ""
    code = cfg.get("enrollment_code")
    if not exp or not code:
        return False
    try:
        expires = _dt.datetime.fromisoformat(exp.replace("Z", "+00:00"))
    except ValueError:
        return False
    if expires - _dt.datetime.now(_dt.timezone.utc) > _dt.timedelta(days=14):
        return False
    _, host_hash = identity()
    enroll_url = enroll_url_from_config(cfg)
    try:
        data = enroll(enroll_url, code, host_hash)
    except Exception as exc:
        log(f"refresh failed: {exc!r}")
        return False
    _write_config_from_enroll(cfg, data, code, enroll_url)
    log("refresh: rotated SAS")
    return True


def cmd_refresh(rest: list[str], stdin_text: str | None) -> int:
    maybe_refresh(load_config())
    return 0


CODEX_HOOK_EVENTS = {"SessionStart": "start", "Stop": "turn", "SessionEnd": "end"}


def codex_hooks_entries(root: Path) -> dict:
    """The three traj-capture hooks for Codex, with the plugin root inlined
    (Codex only sets PLUGIN_ROOT for its own bundled plugins, not for a
    hooks.json we write by hand)."""
    script = root / "scripts" / "capture.py"
    return {ev: [{"hooks": [{"type": "command",
                             "command": f"python3 {shlex.quote(str(script))} {verb} --tool codex",
                             "timeout": 60 if ev == "SessionStart" else 3 if ev == "SessionEnd" else 30}]}]
            for ev, verb in CODEX_HOOK_EVENTS.items()}


def _is_ours(entry: dict) -> bool:
    return any("capture.py" in (h.get("command") or "") and "--tool codex" in (h.get("command") or "")
               for h in entry.get("hooks", []) if isinstance(h, dict))


def install_codex_hooks(hooks_path: Path, root: Path) -> dict:
    """Merge our hooks into `hooks_path` without touching anything else in
    it. Idempotent: an existing traj-capture entry is replaced, other
    people's hooks on the same event are kept."""
    existing: dict = {}
    if hooks_path.is_file():
        try:
            existing = json.loads(hooks_path.read_text()) or {}
        except json.JSONDecodeError:
            backup = hooks_path.with_suffix(".json.bak")
            os.replace(hooks_path, backup)
            log(f"install-codex: {hooks_path} was not valid JSON; moved to {backup}")
            existing = {}
    hooks = existing.setdefault("hooks", {})
    for ev, ours in codex_hooks_entries(root).items():
        kept = [e for e in (hooks.get(ev) or []) if not (isinstance(e, dict) and _is_ours(e))]
        hooks[ev] = kept + ours
    hooks_path.parent.mkdir(parents=True, exist_ok=True)
    tmp = hooks_path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(existing, indent=2) + "\n")
    os.replace(tmp, hooks_path)
    return existing


def cmd_install_codex(rest: list[str], stdin_text: str | None) -> int:
    """`capture.py install-codex [--code CODE]`: register the three hooks in
    $CODEX_HOME/hooks.json (default ~/.codex/hooks.json) pointing at this
    installed copy, then enroll if a code is given. Safe to re-run."""
    hooks_path = codex_home() / "hooks.json"
    install_codex_hooks(hooks_path, plugin_root())
    print(f"traj-capture: Codex hooks registered in {hooks_path} (SessionStart, Stop, SessionEnd)")
    code = arg_value(rest, "--code")
    if code:
        return cmd_setup(["--code", code], "{}")
    if not config_path().exists():
        print("traj-capture: not enrolled yet — run: capture.py setup --code <ENROLLMENT-CODE>")
    return 0


VERBS.update({"start": cmd_start, "turn": cmd_turn, "turnwork": cmd_turnwork, "wait": cmd_wait, "end": cmd_end, "worker": cmd_worker,
              "enroll": lambda rest, stdin: (maybe_auto_enroll(), 0)[1],
              "sweep": cmd_sweep, "probe": cmd_probe, "setup": cmd_setup, "refresh": cmd_refresh,
              "install-codex": cmd_install_codex})


# --------------------------------------------------------------------- main
def main(argv: list[str] | None = None, stdin_text: str | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if not argv:
        log("no verb given")
        return 0
    verb, rest = argv[0], argv[1:]
    fn = VERBS.get(verb)
    if fn is None:
        log(f"unknown verb: {verb}")
        return 0
    try:
        return int(fn(rest, stdin_text) or 0)
    except Exception as exc:  # hooks must never break the agent
        log(f"{verb} failed: {exc!r}")
        return 1 if verb in ("probe", "setup") else 0


if __name__ == "__main__":
    sys.exit(main())

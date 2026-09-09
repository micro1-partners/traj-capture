#!/usr/bin/env python3
"""Render a landed session (with trajectory.atif.json) as a single self-contained HTML page.

Usage: render.py <landed_session_dir> [out.html]
Shows the git/shadow sidecars, then every ATIF step with tool calls and observations collapsed.
"""
from __future__ import annotations

import html
import json
import sys
from pathlib import Path

CSS = """
body{font:14px/1.5 -apple-system,Segoe UI,sans-serif;margin:0;background:#f6f7f9;color:#1c1e21}
.wrap{max-width:960px;margin:0 auto;padding:24px}
h1{font-size:20px;margin:0 0 4px}.sub{color:#5f6672;margin-bottom:18px}
.card{background:#fff;border:1px solid #e3e6ea;border-radius:10px;padding:14px 16px;margin:10px 0}
.k{display:inline-block;min-width:150px;color:#5f6672}
.step{border-left:4px solid #cfd4db;margin:12px 0;padding:8px 14px;background:#fff;border-radius:0 10px 10px 0}
.user{border-left-color:#3b6fd6}.agent{border-left-color:#2a9d6f}.system{border-left-color:#a5a5a5}
.who{font-weight:600;font-size:12px;text-transform:uppercase;color:#5f6672}
pre{white-space:pre-wrap;word-break:break-word;background:#f1f3f6;padding:10px;border-radius:8px;margin:6px 0;font-size:12.5px;max-height:320px;overflow:auto}
details{margin:6px 0}summary{cursor:pointer;color:#3b6fd6}
.think{color:#6b5ca5;font-style:italic}
.pill{display:inline-block;background:#eef2ff;color:#3b4a8c;border-radius:12px;padding:1px 9px;font-size:12px;margin-right:6px}
.bad{background:#fdecec;color:#9b2c2c}.good{background:#e6f6ee;color:#1f6f45}
"""


def esc(x) -> str:
    return html.escape(x if isinstance(x, str) else json.dumps(x, indent=1))


def render(session_dir: Path) -> str:
    d = Path(session_dir)
    traj = json.loads((d / "trajectory.atif.json").read_text())
    start = json.loads((d / "start.json").read_text()) if (d / "start.json").exists() else {}
    end = json.loads((d / "end.json").read_text()) if (d / "end.json").exists() else {}
    man = json.loads((d / "manifest.json").read_text()) if (d / "manifest.json").exists() else {}
    qc = None
    try:
        sys.path.insert(0, str(Path(__file__).resolve().parent))
        import quality_check  # type: ignore
        qc = quality_check.check(d)
    except Exception:
        pass

    out = [f"<style>{CSS}</style><div class='wrap'>"]
    out.append(f"<h1>Trajectory {esc(man.get('session_id') or traj.get('session_id') or d.name)}</h1>")
    out.append(f"<div class='sub'>{esc(man.get('company',''))} · {esc(man.get('tool',''))} · {esc(traj.get('agent',{}).get('model_name',''))} · "
               f"{esc(traj.get('schema_version',''))} · {len(traj.get('steps',[]))} steps</div>")
    if qc:
        pills = "".join(f"<span class='pill bad'>{esc(s)}</span>" for s in qc["symptoms"]) or "<span class='pill good'>no symptoms</span>"
        out.append(f"<div class='card'><b>Quality check</b> score {qc['score']} {pills}</div>")
    rows = []
    if start.get("mode") == "git":
        rows += [("repo", start.get("repo_remote")), ("branch", start.get("branch")), ("base commit", start.get("base_commit")),
                 ("dirty at start", f"{len(start.get('dirty_diff',''))} bytes diff, {len(start.get('dirty_untracked',[]))} untracked")]
        rows += [("commits made", ", ".join(c["subject"] for c in end.get("commits_since_base", [])) or "none"),
                 ("final diff", f"{len(end.get('final_diff',''))} bytes")]
    elif start.get("mode") == "shadow":
        rows += [("mode", "shadow snapshot (no git repo)"), ("cwd", start.get("cwd")),
                 ("files at start", len(start.get("shadow_manifest", {}).get("files", []))),
                 ("diff at end", f"{len((end.get('shadow') or {}).get('diff',''))} bytes")]
    rows += [("started", start.get("started_at")), ("ended", end.get("ended_at")),
             ("end", f"{end.get('end_state')} ({end.get('reason')})"), ("turns", (end.get("counts") or {}).get("turns"))]
    out.append("<div class='card'>" + "".join(f"<div><span class='k'>{esc(str(k))}</span>{esc(str(v))}</div>" for k, v in rows) + "</div>")
    diff = end.get("final_diff") or (end.get("shadow") or {}).get("diff") or ""
    if diff:
        out.append(f"<details><summary>Final diff</summary><pre>{esc(diff[:20000])}</pre></details>")
    for s in traj.get("steps", []):
        src = s.get("source", "system")
        out.append(f"<div class='step {src}'><div class='who'>{src} · step {s.get('step_id')}</div>")
        if s.get("reasoning_content"):
            out.append(f"<details><summary class='think'>reasoning</summary><pre class='think'>{esc(s['reasoning_content'][:6000])}</pre></details>")
        msg = s.get("message")
        if isinstance(msg, list):
            msg = "\n".join(p.get("text", "") for p in msg if isinstance(p, dict))
        if msg:
            out.append(f"<pre>{esc(str(msg)[:8000])}</pre>")
        for tc in s.get("tool_calls", []) or []:
            out.append(f"<details><summary>tool call: <b>{esc(tc.get('function_name',''))}</b></summary><pre>{esc(tc.get('arguments'))[:6000]}</pre></details>")
        for r in (s.get("observation") or {}).get("results", []) or []:
            c = r.get("content")
            if isinstance(c, list):
                c = "\n".join(p.get("text", "") for p in c if isinstance(p, dict))
            out.append(f"<details><summary>tool result</summary><pre>{esc(str(c))[:6000]}</pre></details>")
        out.append("</div>")
    out.append("</div>")
    return "\n".join(out)


def main(argv: list[str]) -> int:
    if not argv:
        print(__doc__); return 2
    d = Path(argv[0]); out = Path(argv[1]) if len(argv) > 1 else d / "trajectory.html"
    out.write_text(render(d)); print(f"wrote {out}"); return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))

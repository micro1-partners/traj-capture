# traj-capture

Captures coding-agent sessions (Claude Code, Codex) raw, plus git state before and
after, into the company's `<slug>-traj` Azure Blob container (separate from its corpus). One public plugin for every
company; the company credential is obtained at setup with an enrollment code and
stored only on the machine.

## Install (engineer, once)

Requirements: Python 3.9+ on your PATH. No other dependencies. Nothing runs in the path
of your agent, and nothing on your machine is ever deleted.

### Claude Code

    claude plugin marketplace add micro1-partners/traj-capture
    claude plugin install traj-capture@micro1-traj

Then, inside Claude Code, once:

    /traj-capture:setup <ENROLLMENT-CODE>

It replies `probe ok` with your company name. Every session from now on is captured.

### Codex

    codex plugin marketplace add micro1-partners/traj-capture
    echo "<ENROLLMENT-CODE>" > ~/.traj-capture/enroll-code

The plugin enrolls itself on your next session and deletes the file. Codex Desktop has no
CLI: add the marketplace to `~/.codex/config.toml` instead and restart the app, then do the
`echo` line.

    [marketplaces.micro1-traj]
    source_type = "git"
    source = "https://github.com/micro1-partners/traj-capture.git"
    ref = "main"

    [plugins."traj-capture@micro1-traj"]
    enabled = true

The `echo` line works for Claude Code too, and it is how IT pre-provisions machines: drop the
code file alongside the managed plugin settings and nobody types anything. Use both tools?
Enroll once; they share `~/.traj-capture`.

### Check it is working

    tail -5 ~/.traj-capture/capture.log

After your next session ends you will see a `finalized` line.

### Troubleshooting

* `/traj-capture:setup` (Claude Code) with no code re-runs the connection probe and prints recent
  activity. Same thing from a terminal: `python3 <plugin>/scripts/capture.py setup`.
* Codex build without plugin support? Clone this repo and register the hooks by hand (merges into
  `$CODEX_HOME/hooks.json`, idempotent, keeps your other hooks):
  `python3 traj-capture/plugins/traj-capture/scripts/capture.py install-codex --code <ENROLLMENT-CODE>`

### What the plugin does with your credential

Enrollment talks only to `data.micro1.ai`, the company portal, and exchanges the code for an
upload credential that is stored only on your machine. Uploads go straight to your company's
`<slug>-traj` container with a create+write SAS that cannot read, list, or delete anything.
Codex's transcript is the rollout file under `$CODEX_HOME/sessions/`; the plugin finds it by
session id when the hook payload carries `transcript_path: null`.

## Production agents (shipper libraries)

Agents that are not coding tools use the same enrollment code and land the same layout.
Four calls, zero dependencies, fail-open:

    packages/traj-shipper-py     pip: traj-shipper          (Python 3.9+)
    packages/traj-shipper-node   npm: @micro1/traj-shipper  (Node 18+)

Payload contract for what a turn must carry: `spec/traj-v1.schema.json` + `spec/example/`.

## Operator setup (micro1, per company)

1. Create and activate the company in CDP (storage provisioned).
2. `POST /api/companies/<slug>/traj/codes` in CDP (admin) → send the one-time code to the company POC.

No repo work. The `-traj` container is created on first enrollment; the plugin rotates its
SAS automatically 14 days before expiry by re-exchanging the same code.

## What lands

    <container>/trajectories/<tool>/<user_hash>/<session_id>/
        transcript.jsonl, subagents/*.jsonl, start.json, end.json, manifest.json
    <container>/trajectories/_receipts/<session_id>.json

- `raw_api.tar.gz` — the full API request and response bodies for the session,
  landed next to the transcript when telemetry is enabled.

## Local state

`$CLAUDE_PLUGIN_DATA` (or `~/.traj-capture`): `config.json`, `capture.log`, and
one folder per session with sidecars, markers and receipts. Nothing is ever
deleted locally.

## Development

    python3 -m venv .venv && ./.venv/bin/pip install pytest
    ./.venv/bin/python -m pytest
    claude --plugin-dir ./plugins/traj-capture      # load the local plugin for a session

Set `TRAJ_CAPTURE_CONFIG`, `TRAJ_CAPTURE_STATE`, `TRAJ_CAPTURE_INLINE=1` to run
against a local `file://` sink (see `tests/conftest.py`).

## Tools (operator side)

    tools/convert.py <session_dir>        # Harbor -> trajectory.atif.json (needs pip install -r tools/requirements.txt)
    tools/quality_check.py <session_dir>  # score against the trajectory-quality checklist
    tools/render.py <session_dir>         # self-contained HTML page of one session
    tools/stage_harbor.py <jobs_dir> <session_dir>...   # then: harbor view <jobs_dir> --jobs

## License

Proprietary, micro1.

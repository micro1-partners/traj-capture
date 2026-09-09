# traj-capture

Captures coding-agent sessions (Claude Code, Codex) raw, plus git state before and
after, into the company's `<slug>-traj` Azure Blob container (separate from its corpus). One public plugin for every
company; the company credential is obtained at setup with an enrollment code and
stored only on the machine.

## Install (engineer, once)

One line, if your security policy is fine with a downloaded installer (it registers the
marketplace, installs the plugin in every supported tool on the machine, and saves the code):

    curl -fsSL https://raw.githubusercontent.com/micro1-partners/traj-capture/main/install.sh | sh -s -- <ENROLLMENT-CODE>

Prefer to read first? `install.sh` is 80 lines, has `--dry-run`, and the native commands below do
exactly the same thing.

Requirements: Python 3.9+ on your PATH. No other dependencies. Session startup takes
a local snapshot with a five-second Git budget; network uploads run in detached workers.
Capture does not change your working files or Git staging area.

### Claude Code

    claude plugin marketplace add micro1-partners/traj-capture
    claude plugin install traj-capture@micro1-traj

Then, inside Claude Code, once:

    /traj-capture:setup <ENROLLMENT-CODE>

It replies `probe ok` with your company name. This verifies storage connectivity, not
hook execution. Verify a new test session using the checks below.

### Codex

    codex plugin marketplace add micro1-partners/traj-capture
    mkdir -p ~/.traj-capture
    (umask 077; printf '%s\n' "<ENROLLMENT-CODE>" > ~/.traj-capture/enroll-code)

Install and enable `traj-capture` from that marketplace in your Codex plugin manager.
Review and trust its hooks before expecting capture to run. See the
[Codex hook trust instructions](https://learn.chatgpt.com/docs/hooks#review-and-trust-hooks).

Without an existing config, the first session schedules enrollment in the background.
After enrollment succeeds, capture starts with the next **new** session. The enrollment
session is not backfilled. Successful enrollment consumes the code file. Codex Desktop has no
CLI: add the marketplace to `~/.codex/config.toml` instead and restart the app, then do the
code-file step.

    [marketplaces.micro1-traj]
    source_type = "git"
    source = "https://github.com/micro1-partners/traj-capture.git"
    ref = "main"

    [plugins."traj-capture@micro1-traj"]
    enabled = true

The code-file step works for Claude Code too, and it is how IT pre-provisions machines: drop the
code file alongside the managed plugin settings and nobody types anything. Use both tools?
Enroll once; they share `~/.traj-capture`.

### Check it is working

    tail -5 ~/.traj-capture/capture.log

After your next session ends you will see a `finalized` line.
Verify that its manifest contains a nonempty transcript and matches the uploaded files.
A run ending is not sufficient evidence that capture completed.

### Troubleshooting

* `/traj-capture:setup` (Claude Code) with no code re-runs the connection probe and prints recent
  activity. Same thing from a terminal: `python3 <plugin>/scripts/capture.py setup`.
* To enroll immediately: `python3 <plugin>/scripts/capture.py setup --code <CODE>`.
* Put `"enabled": false` in the shared config to stop new capture and future upload
  attempts, including retries. An HTTP request already in flight cannot be recalled.
  Raw-body logging in Claude settings is separate; turn it off with
  `python3 <plugin>/scripts/capture.py setup --telemetry off` if required.
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

- `raw_api.tar.gz`: session-owned requests and explicitly correlated responses, when
  telemetry is opted into. Responses need a matching `request_id` or `_request_id`.
  Timestamps and message IDs never determine ownership. Responses without correlation
  remain unassigned locally, so this is not a raw-exchange completeness guarantee.

## Local state

Both hosts use `~/.traj-capture/config.json` (owner-only permissions). State defaults to
`~/.traj-capture`, with sessions under `companies/<binding-hash>/sessions/<tool>/<session-id>`.
Company/destination bindings exclude the SAS query, allowing normal credential rotation.
`TRAJ_CAPTURE_CONFIG` and `TRAJ_CAPTURE_STATE` are explicit overrides.

Changing enrollment never reassigns previous sessions. Re-enroll the original company to
resume its bound uploads. Legacy plugin-local configs, unbound sessions and already landed
captures are not automatically migrated or reclassified. Retain them for an operator to
verify ownership and completeness. This upgrade does not clean up existing customer data.
Successful enrollment consumes the code file; old uncaptured request bodies are pruned.
Ambiguous responses are retained for an explicit retention review.

Shadow/non-repository objects remain local. Their hashes alone are not reconstructable
starting states, and the quality checker reports that limitation. Snapshot timeouts are
explicitly marked incomplete. Full starting-state exports are not included in this release.

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

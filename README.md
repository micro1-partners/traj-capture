# traj-capture

Captures coding-agent sessions (Claude Code, Codex) raw, plus git state before and
after, into the company's `<slug>-traj` Azure Blob container (separate from its corpus). One public plugin for every
company; the company credential is obtained at setup with an enrollment code and
stored only on the machine.

## Install (engineer, once)

Requirements: Python 3.9+ on your PATH. No other dependencies. Session startup takes
a local snapshot with a five-second Git budget; network uploads run in detached workers.
Capture does not change your working files or Git staging area.

### 1. Run the setup helper

If your security policy permits a downloaded installer, run this in a terminal,
replacing the placeholder with the enrollment code supplied by micro1:

    curl -fsSL https://raw.githubusercontent.com/micro1-partners/traj-capture/main/install.sh | sh -s -- '<ENROLLMENT-CODE>'

Review [install.sh](install.sh) first if required. It supports `--dry-run` to preview
actions without changing anything. Treat the code as a credential; do not share it
in screenshots or logs, and follow your company's policy for shell-history handling.
For an alternative to the helper, see [Manual installation](#manual-installation).

The helper saves the code locally and handles the tools it detects:

| Tool detected | What the helper currently does |
| --- | --- |
| Claude Code | Registers the marketplace and installs the plugin. |
| Codex CLI | Registers the marketplace. **You still install and enable the plugin in Codex.** |
| Codex Desktop without the CLI | If a Codex config file exists and the marketplace is absent, adds the marketplace and enabled-plugin settings. Restart the app, then verify installation below. |

If you use both Codex CLI and Desktop, the helper takes the CLI path. It does not
install the plugin through the CLI or approve hooks. A `done` message means the
helper finished, not that capture is working.

### 2. Finish Codex setup

Skip this step if you only use Claude Code.

1. Open the plugin manager in Codex Desktop, or `/plugins` inside Codex CLI.
2. Find `traj-capture` in the `micro1-traj` marketplace. Install it if needed and
   confirm it is enabled. Restart Desktop if the helper changed its configuration.
3. Review and trust the plugin's hooks. In Codex CLI, use `/hooks`.

Installing a plugin does not automatically trust its hooks. See the
[Codex hook trust instructions](https://learn.chatgpt.com/docs/hooks#review-and-trust-hooks).
When the helper saved your code, you do not need to create a credential file manually.

### 3. Enroll, then start a fresh test session

On a machine without an existing capture config, the first session with active hooks
schedules enrollment in the background. After enrollment succeeds, capture starts
with the next **new** session. The enrollment session is not backfilled, and successful
enrollment consumes the code file. Use a dummy project for the first capture test.

Claude Code and Codex share `~/.traj-capture`, so enroll once for both tools.

### Check it is working

    tail -5 ~/.traj-capture/capture.log

After your next session ends you will see a `finalized` line.
Verify that its manifest contains a nonempty transcript and matches the uploaded files.
A run ending is not sufficient evidence that capture completed.

### Troubleshooting

* `/traj-capture:setup` (Claude Code) with no code re-runs the connection probe and prints recent
  activity. Same thing from a terminal: `python3 <plugin>/scripts/capture.py setup`.
* To enroll immediately: `python3 "<plugin>/scripts/capture.py" setup --code '<CODE>'`.
* Put `"enabled": false` in the shared config to stop new capture and future upload
  attempts, including retries. An HTTP request already in flight cannot be recalled.
  Raw-body logging in Claude settings is separate; turn it off with
  `python3 <plugin>/scripts/capture.py setup --telemetry off` if required.
* Codex build without plugin support? Clone this repo and register the hooks by hand (merges into
  `$CODEX_HOME/hooks.json`, idempotent, keeps your other hooks):
  `python3 traj-capture/plugins/traj-capture/scripts/capture.py install-codex --code '<ENROLLMENT-CODE>'`.
  Use this fallback instead of plugin-bundled capture hooks, not alongside them.

### Manual installation

Use these steps instead of the setup helper if your company requires native commands
or operator-managed configuration. They are not additional quick-start steps.

<details>
<summary>Show manual setup for Claude Code and Codex</summary>

#### Claude Code

    claude plugin marketplace add micro1-partners/traj-capture
    claude plugin install traj-capture@micro1-traj

Then, inside Claude Code, enroll immediately:

    /traj-capture:setup <ENROLLMENT-CODE>

`probe ok` verifies storage connectivity, not hook execution. Start a new test session
and verify the resulting capture as described above.

#### Codex CLI

    codex plugin marketplace add micro1-partners/traj-capture

Install and enable `traj-capture` from `micro1-traj` through `/plugins`, then review
and trust its hooks through `/hooks`.

#### Codex Desktop without the CLI

Open Codex once to create its configuration. Add the following entries to
`~/.codex/config.toml` (or `$CODEX_HOME/config.toml` if you use a custom Codex home).
If either table already exists, update its entries rather than adding duplicate tables:

    [marketplaces.micro1-traj]
    source_type = "git"
    source = "https://github.com/micro1-partners/traj-capture.git"
    ref = "main"

    [plugins."traj-capture@micro1-traj"]
    enabled = true

Restart Desktop, confirm the plugin is installed and enabled in its plugin manager,
and review and trust the hooks.

#### Save the enrollment code for Codex

On a machine not yet enrolled, save the code once:

    mkdir -p ~/.traj-capture
    (umask 077; printf '%s\n' '<ENROLLMENT-CODE>' > ~/.traj-capture/enroll-code)

Follow step 3 above to enroll and start a fresh test session. This code-file method
also works for Claude Code and IT provisioning. Hook trust is still required in Codex.

</details>

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

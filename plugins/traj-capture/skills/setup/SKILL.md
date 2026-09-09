---
name: setup
description: Connect traj-capture to the company's storage with an enrollment code, or verify it is connected and show recent capture activity. Use when the user runs /traj-capture:setup or asks whether session capture is working.
disable-model-invocation: true
---

If the user supplied an enrollment code as `$ARGUMENTS`, run with the Bash tool:

    python3 "${CLAUDE_PLUGIN_ROOT}/scripts/capture.py" setup --code "$ARGUMENTS"

Otherwise run:

    python3 "${CLAUDE_PLUGIN_ROOT}/scripts/capture.py" setup

Report the company and probe result without exposing enrollment codes or credentials.
`probe ok` verifies storage connectivity only. Verify a new test session's finalized
receipt and nonempty transcript before claiming capture works. Codex hooks must also
be installed, enabled and trusted. Do not capture or backfill unrelated sessions.
If setup fails, show the sanitized error for micro1 support.

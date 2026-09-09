---
name: setup
description: Connect traj-capture to the company's storage with an enrollment code, or verify it is connected and show recent capture activity. Use when the user runs /traj-capture:setup or asks whether session capture is working.
disable-model-invocation: true
---

If the user supplied an enrollment code as `$ARGUMENTS`, run with the Bash tool:

    python3 ${CLAUDE_PLUGIN_ROOT}/scripts/capture.py setup --code "$ARGUMENTS"

Otherwise run:

    python3 ${CLAUDE_PLUGIN_ROOT}/scripts/capture.py setup

Report the output verbatim. If the probe line says `ok`, tell the user capture
is connected and every new session is captured automatically from now on. If it
says `failed` or `rejected`, show the error and tell the user to send it to micro1.

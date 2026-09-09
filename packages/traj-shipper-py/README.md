# traj-shipper (Python)

Lands your production agent's runs in micro1 trajectory storage. Four calls, zero
dependencies, and it can never fail your agent.

    pip install traj-shipper

    from traj_shipper import TrajCapture
    cap = TrajCapture.init(code=os.environ["TRAJ_CODE"], agent_name="acme-triage", instance="prod-us-1")

    cap.start(run_id, {"model": "claude-sonnet-5", "parent_run_id": None})
    cap.turn(run_id, n, {"role": "assistant", "content": ..., "tool_calls": [...],
                         "raw_request": {...}, "raw_response": {...}})
    cap.end(run_id, {"status": "completed"})
    cap.feedback(run_id, {"score": 4, "scale": "1-5", "expert_id": "exp_7c1"})

Every method returns immediately and swallows its own errors. A background thread
uploads each event as its own blob under
`trajectories/<agent_name>/<instance>/<run_id>/` in the company's `-traj`
container, then writes `manifest.json` and a receipt when the run ends.

* `code` is the enrollment code micro1 gave you. It is exchanged for an upload
  credential at `data.micro1.ai` and re-exchanged automatically before expiry.
  Pass `config_path` to persist the credential across restarts.
* Nothing touches disk unless you pass `spool_dir`; then events are written
  locally first and anything unacknowledged is re-shipped after a restart.
* `cap.stats` → `{"queued", "uploaded", "dropped", "failed"}`.
* Payload contract: `spec/traj-v1.schema.json` in this repo. Include
  `raw_request` / `raw_response` on each turn — that is what makes a run replayable.

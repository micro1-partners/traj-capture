# traj-shipper (Python)

Lands your production agent's runs in micro1 trajectory storage. Four calls, zero
dependencies, and it can never fail your agent.

    pip install traj-shipper

    from traj_shipper import TrajCapture
    cap = TrajCapture.init(code=os.environ["TRAJ_CODE"], agent_name="acme-triage", instance="prod-us-1",
                           spool_dir="/var/lib/my-agent/trajectories")

    cap.start(run_id, {"model": "claude-sonnet-5", "parent_run_id": None})
    cap.turn(run_id, n, {"role": "assistant", "content": ..., "tool_calls": [...],
                         "raw_request": {...}, "raw_response": {...}})
    cap.end(run_id, {"status": "completed", "turns": n})
    cap.feedback(run_id, {"score": 4, "scale": "1-5", "expert_id": "exp_7c1"})

Event methods swallow their own errors. They serialize locally and, with spooling enabled,
write durable files before returning; they do not perform network I/O. A background thread
uploads each event as its own blob under
`trajectories/<agent_name>/<instance>/<run_id>/` in the company's `-traj`
container. A receipt is written only after required events reconcile, not merely when the run ends.

* `code` is the enrollment code micro1 gave you. It is exchanged for an upload
  credential at `data.micro1.ai` and re-exchanged automatically before expiry.
  Pass `config_path` to persist the credential across restarts.
* Nothing touches disk unless you pass `spool_dir`; then events are written
  locally first. Acknowledgements survive restarts and failures retry in the background.
  Enable spooling for production; the default memory queue cannot survive a crash.
* Use a separate spool per company, agent instance and single writer process. The spool
  is bound to its enrollment/destination; legacy unbound spools need explicit reconciliation.
* Turn numbers start at 1 without gaps. Include `turns` in `end` to detect missing tail
  events. Run IDs must be globally unique and URL-safe.
* `flush()` returns false while uploads or reconciliation remain pending. Incomplete
  runs never receive completion receipts. Late feedback retries independently.
* Completed local acknowledgement records are retained; cleanup requires an operator policy.
* `cap.stats` → `{"queued", "uploaded", "dropped", "failed"}`.
* Payload contract: `spec/traj-v1.schema.json` in this repo. Include
  `raw_request` / `raw_response` on each turn for a faithful provider exchange.

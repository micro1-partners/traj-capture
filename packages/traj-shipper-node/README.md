# @micro1/traj-shipper (Node)

Lands your production agent's runs in micro1 trajectory storage. Four calls, zero
dependencies, Node 18+, and it can never fail your agent.

    npm install @micro1/traj-shipper

    const { TrajCapture } = require('@micro1/traj-shipper');
    const cap = TrajCapture.init({ code: process.env.TRAJ_CODE, agentName: 'acme-triage', instance: 'prod-us-1',
      spoolDir: '/var/lib/my-agent/trajectories' });

    agent.on('run:start', ({ runId, model }) => cap.start(runId, { model }));
    agent.on('turn:end',  ({ runId, n, turn }) => cap.turn(runId, n, turn));   // turn: role, content, tool_calls, raw_request, raw_response
    agent.on('run:end',   ({ runId, status, turns }) => cap.end(runId, { status, turns }));
    feedback.on('scored', ({ runId, score, expertId }) => cap.feedback(runId, { score, expert_id: expertId }));

Event methods swallow their own errors. They serialize locally and, with spooling enabled,
write durable files before returning; they do not perform network I/O. Uploads run in the
background, one blob per event, under
`trajectories/<agentName>/<instance>/<runId>/` in the company's `-traj` container,
then a manifest and receipt only after the required events reconcile.

* `code` is the enrollment code micro1 gave you; it is exchanged for an upload
  credential at `data.micro1.ai` and re-exchanged before expiry. Pass `configPath`
  to persist it across restarts.
* Nothing touches disk unless you pass `spoolDir`; then events are written locally
  first. Acknowledgements survive restarts and failures retry in the background.
  Enable spooling for production; the default memory queue cannot survive a crash.
* Use a separate spool per company, agent instance and single writer process. Reuse by
  another deployment is rejected; legacy unbound spools need explicit reconciliation.
* Turn numbers start at 1 without gaps. Include `turns` at the end to detect missing tail
  events. Run IDs must be globally unique and URL-safe.
* `flush()` resolves false while uploads or reconciliation remain pending. Incomplete runs
  never receive completion receipts. Late feedback retries independently.
* Completed local acknowledgement records are retained; cleanup requires an operator policy.
* `cap.stats` → `{ queued, uploaded, dropped, failed }`. `await cap.flush()` before a
  planned shutdown.
* Payload contract: `spec/traj-v1.schema.json` in this repo. Include `raw_request` /
  `raw_response` on each turn — that is what makes a run replayable.

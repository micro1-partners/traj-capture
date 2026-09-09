# @micro1/traj-shipper (Node)

Lands your production agent's runs in micro1 trajectory storage. Four calls, zero
dependencies, Node 18+, and it can never fail your agent.

    npm install @micro1/traj-shipper

    const { TrajCapture } = require('@micro1/traj-shipper');
    const cap = TrajCapture.init({ code: process.env.TRAJ_CODE, agentName: 'acme-triage', instance: 'prod-us-1' });

    agent.on('run:start', ({ runId, model }) => cap.start(runId, { model }));
    agent.on('turn:end',  ({ runId, n, turn }) => cap.turn(runId, n, turn));   // turn: role, content, tool_calls, raw_request, raw_response
    agent.on('run:end',   ({ runId, status }) => cap.end(runId, { status }));
    feedback.on('scored', ({ runId, score, expertId }) => cap.feedback(runId, { score, expert_id: expertId }));

Every method returns immediately and swallows its own errors. Uploads run in the
background, one blob per event, under
`trajectories/<agentName>/<instance>/<runId>/` in the company's `-traj` container,
then `manifest.json` and a receipt when the run ends.

* `code` is the enrollment code micro1 gave you; it is exchanged for an upload
  credential at `data.micro1.ai` and re-exchanged before expiry. Pass `configPath`
  to persist it across restarts.
* Nothing touches disk unless you pass `spoolDir`; then events are written locally
  first and anything unacknowledged is re-shipped after a restart.
* `cap.stats` → `{ queued, uploaded, dropped, failed }`. `await cap.flush()` before a
  planned shutdown.
* Payload contract: `spec/traj-v1.schema.json` in this repo. Include `raw_request` /
  `raw_response` on each turn — that is what makes a run replayable.

'use strict';
const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const { TrajCapture, _DirSink } = require('..');

const tmp = () => fs.mkdtempSync(path.join(os.tmpdir(), 'traj-'));
const read = (p) => JSON.parse(fs.readFileSync(p, 'utf8'));
const mk = (root, extra = {}) => new TrajCapture({ agentName: 'acme triage', instance: 'prod-us-1', sasUrl: `file://${path.join(root, 'blob')}`, flushOnExit: false, ...extra });

test('full run lands every file plus manifest and receipt', async () => {
  const root = tmp(); const cap = mk(root);
  cap.start('run_1', { model: 'claude-sonnet-5', parent_run_id: null });
  cap.turn('run_1', 1, { role: 'user', content: 'hi' });
  cap.turn('run_1', 2, { role: 'assistant', content: 'hello', raw_request: { model: 'x' }, raw_response: { usage: {} } });
  cap.end('run_1', { status: 'completed', turns: 2 });
  cap.feedback('run_1', { score: 4 });
  assert.equal(await cap.flush(5000), true);
  const run = path.join(root, 'blob/trajectories/acme-triage/prod-us-1/run_1');
  assert.deepEqual(fs.readdirSync(run).sort(), ['end.json', 'feedback.json', 'manifest.json', 'start.json', 'turns']);
  assert.deepEqual(fs.readdirSync(path.join(run, 'turns')).sort(), ['0001.json', '0002.json']);
  const t2 = read(path.join(run, 'turns/0002.json'));
  assert.equal(t2.schema_version, 1); assert.equal(t2.run_id, 'run_1'); assert.equal(t2.turn, 2); assert.deepEqual(t2.raw_request, { model: 'x' });
  const m = read(path.join(run, 'manifest.json'));
  assert.equal(m.turns_landed, 2);
  assert.deepEqual(Object.keys(m.files).sort(), ['end.json', 'start.json', 'turns/0001.json', 'turns/0002.json']);
  assert.equal(m.files['end.json'].bytes, fs.statSync(path.join(run, 'end.json')).size);
  const r = read(path.join(root, 'blob/trajectories/_receipts/run_1.json'));
  assert.equal(r.end_state, 'observed'); assert.equal(r.prefix, 'trajectories/acme-triage/prod-us-1/run_1');
  assert.deepEqual(cap.stats, { queued: 6, uploaded: 6, dropped: 0, failed: 0 });
});

test('public methods never throw even when the sink explodes', async () => {
  const root = tmp(); const cap = mk(root);
  const orig = _DirSink.prototype.put;
  _DirSink.prototype.put = async () => { throw new Error('blob down'); };
  try {
    cap.start('r', {}); cap.turn('r', 1, {}); cap.end('r'); cap.feedback('r', {});
    await cap.flush(5000);
    assert.equal(cap.stats.failed, 5); assert.equal(cap.stats.uploaded, 0);
  } finally { _DirSink.prototype.put = orig; }
});

test('bad input is swallowed', async () => {
  const cap = mk(tmp());
  cap.turn('r', 'not-a-number', {});
  const cyc = {}; cyc.self = cyc;
  cap.turn('r', 1, { cyc });
  await cap.flush(1000);
  assert.equal(cap.stats.queued, 0);
});

test('queue drops oldest when full', async () => {
  const root = tmp(); const cap = mk(root, { maxQueue: 3 });
  // hold the drain so the queue actually fills
  const orig = _DirSink.prototype.put; let release; const gate = new Promise((r) => { release = r; });
  _DirSink.prototype.put = async function (...a) { await gate; return orig.apply(this, a); };
  try {
    for (let i = 1; i <= 6; i++) cap.turn('r', i, { i });
    release();
    await cap.flush(5000);
  } finally { _DirSink.prototype.put = orig; }
  const landed = fs.readdirSync(path.join(root, 'blob/trajectories/acme-triage/prod-us-1/r/turns')).sort();
  // turn 1 was already in flight when the queue overflowed; of the rest, the oldest were dropped
  assert.ok(cap.stats.dropped >= 2, `dropped=${cap.stats.dropped}`);
  assert.ok(landed.includes('0006.json') && landed.includes('0005.json'));
});

test('spool survives restart and reships', async () => {
  const root = tmp(); const spool = path.join(root, 'spool');
  const orig = _DirSink.prototype.put;
  _DirSink.prototype.put = async () => { throw new Error('blob down'); };   // simulate: never reached blob
  const cap = mk(root, { spoolDir: spool });
  cap.start('r', {}); cap.turn('r', 1, { a: 1 }); cap.end('r');
  await cap.flush(2000);
  _DirSink.prototype.put = orig;
  assert.ok(fs.existsSync(path.join(spool, 'r/turns/0001.json')) && fs.existsSync(path.join(spool, 'r/.finalize')));
  assert.ok(!fs.existsSync(path.join(root, 'blob/trajectories')));
  const cap2 = mk(root, { spoolDir: spool });   // boot sweep re-enqueues
  assert.equal(await cap2.flush(5000), true);
  const run = path.join(root, 'blob/trajectories/acme-triage/prod-us-1/r');
  assert.ok(fs.existsSync(path.join(run, 'turns/0001.json')) && fs.existsSync(path.join(run, 'manifest.json')));
  assert.ok(fs.existsSync(path.join(root, 'blob/trajectories/_receipts/r.json')));
  assert.ok(fs.existsSync(path.join(spool, 'r/.receipt')) && !fs.existsSync(path.join(spool, 'r/turns/0001.json')));
});

test('enrolls with code and persists config; second instance reuses it', async () => {
  const root = tmp(); const cfg = path.join(root, 'cfg.json'); const seen = [];
  const enrollFn = async (url, code, host) => { seen.push({ url, code, host }); return { company: 'acme', sas_url: `file://${path.join(root, 'blob')}`, expires_at: '2099-01-01T00:00:00Z' }; };
  const cap = new TrajCapture({ agentName: 'a', code: 'ACME-K7M3-9QZT-4HWX', enrollUrl: 'https://portal.test', configPath: cfg, flushOnExit: false, _enrollFn: enrollFn });
  cap.start('r', {}); await cap.flush(5000);
  assert.equal(seen.length, 1); assert.equal(seen[0].url, 'https://portal.test'); assert.equal(seen[0].host.length, 12);
  assert.equal(read(cfg).company, 'acme');
  assert.ok(fs.existsSync(path.join(root, 'blob/trajectories/a/default/r/start.json')));
  const cap2 = new TrajCapture({ agentName: 'a', code: 'ACME-K7M3-9QZT-4HWX', configPath: cfg, flushOnExit: false, _enrollFn: enrollFn });
  cap2.start('r2', {}); await cap2.flush(5000);
  assert.equal(seen.length, 1);
  assert.ok(fs.existsSync(path.join(root, 'blob/trajectories/a/default/r2/start.json')));
});

test('403 forces re-enroll', async () => {
  const root = tmp(); let enrolls = 0;
  const enrollFn = async () => { enrolls++; return { sas_url: `file://${path.join(root, 'blob')}`, expires_at: '2099-01-01T00:00:00Z' }; };
  const cap = new TrajCapture({ agentName: 'a', code: 'X-1', flushOnExit: false, _enrollFn: enrollFn });
  const orig = _DirSink.prototype.put; let fail = true;
  _DirSink.prototype.put = async function (...a) { if (fail) { fail = false; throw Object.assign(new Error('forbidden'), { status: 403 }); } return orig.apply(this, a); };
  try {
    cap.start('r', {}); await cap.flush(5000);
    cap.turn('r', 1, {}); await cap.flush(5000);
  } finally { _DirSink.prototype.put = orig; }
  assert.equal(enrolls, 2); assert.equal(cap.stats.failed, 1); assert.equal(cap.stats.uploaded, 1);
});

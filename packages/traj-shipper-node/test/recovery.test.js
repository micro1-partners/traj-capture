'use strict';
const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const { TrajCapture, _DirSink } = require('..');
const tmp = () => fs.mkdtempSync(path.join(os.tmpdir(), 'traj-recovery-'));
const make = (r, extra = {}) => new TrajCapture({ agentName: 'agent', sasUrl: `file://${r}/blob`, spoolDir: `${r}/spool`, flushOnExit: false, ...extra });
const manifest = (r) => JSON.parse(fs.readFileSync(`${r}/blob/trajectories/agent/default/run-0001/manifest.json`, 'utf8'));

test('partial outage: no receipt, restart recovers failed turn and prior acknowledgements', async () => {
  const r = tmp(), c = make(r), orig = _DirSink.prototype.put;
  _DirSink.prototype.put = async function (rel, ...a) {
    if (rel.endsWith('turns/0001.json')) throw new Error('synthetic outage');
    return orig.call(this, rel, ...a);
  };
  try {
    c.start('run-0001'); c.turn('run-0001', 1, { role: 'user' }); c.end('run-0001', { turns: 1 });
    assert.equal(await c.flush(50), false);
    assert.equal(fs.existsSync(`${r}/spool/run-0001/.receipt`), false);
  } finally { await c.close(0); _DirSink.prototype.put = orig; }
  const restored = make(r);
  try {
    assert.equal(await restored.flush(200), true);
    assert.deepEqual(Object.keys(manifest(r).files).sort(), ['end.json', 'start.json', 'turns/0001.json']);
  } finally { await restored.close(0); }
});

test('restart before end preserves already-uploaded start and turns', async () => {
  const r = tmp(), c = make(r);
  c.start('run-0001'); c.turn('run-0001', 1, { role: 'user' });
  assert.equal(await c.flush(200), true); await c.close(0);
  const restored = make(r);
  try {
    restored.end('run-0001', { turns: 1 });
    assert.equal(await restored.flush(200), true);
    assert.ok(manifest(r).files['start.json']);
    assert.equal(manifest(r).turns_landed, 1);
  } finally { await restored.close(0); }
});

test('missing turns prevent completion until the gap is filled', async () => {
  const r = tmp(), c = make(r);
  try {
    c.start('run-0001'); c.turn('run-0001', 1, { role: 'user' }); c.end('run-0001', { turns: 2 });
    assert.equal(await c.flush(50), false);
    assert.equal(fs.existsSync(`${r}/spool/run-0001/.receipt`), false);
    c.turn('run-0001', 2, { role: 'assistant' }); c.retryAt = 0;
    assert.equal(await c.flush(200), true);
    assert.equal(manifest(r).turns_landed, 2);
  } finally { await c.close(0); }
});

test('spooled queue overflow recovers without silently completing gaps', async () => {
  const r = tmp(), c = make(r, { maxQueue: 2 });
  c.start('run-0001');
  for (let n = 1; n <= 5; n++) c.turn('run-0001', n, { role: 'user' });
  c.end('run-0001', { turns: 5 });
  await c.close(50);
  const restored = make(r);
  try {
    assert.equal(await restored.flush(200), true);
    assert.equal(manifest(r).turns_landed, 5);
  } finally { await restored.close(0); }
});

test('same spool cannot be redirected to another destination', async () => {
  const r = tmp(), c = make(r); c.start('run-0001'); await c.close(200);
  const other = make(r, { sasUrl: `file://${r}/other-company` });
  try {
    other.start('other-run'); await other.flush(50);
    assert.equal(other.disabled, true);
    assert.equal(fs.existsSync(`${r}/other-company`), false);
  } finally { await other.close(0); }
});

test('feedback after completion survives outage and restart', async () => {
  const r = tmp(), c = make(r), orig = _DirSink.prototype.put;
  c.start('run-0001'); c.end('run-0001'); assert.equal(await c.flush(200), true);
  _DirSink.prototype.put = async () => { throw new Error('synthetic outage'); };
  try { c.feedback('run-0001', { score: 4 }); assert.equal(await c.flush(50), false); }
  finally { await c.close(0); _DirSink.prototype.put = orig; }
  const restored = make(r);
  try {
    assert.equal(await restored.flush(200), true);
    assert.equal(JSON.parse(fs.readFileSync(`${r}/blob/trajectories/agent/default/run-0001/feedback.json`)).score, 4);
  } finally { await restored.close(0); }
});

test('reenrollment cannot redirect a spool with a verified storage binding', async () => {
  const r = tmp(); let destination = 'A';
  const opts = { agentName: 'agent', code: 'same-code', spoolDir: `${r}/spool`, flushOnExit: false,
    _enrollFn: async () => ({ company: destination, sas_url: `file://${r}/${destination}`, expires_at: '2099-01-01T00:00:00Z' }) };
  const first = new TrajCapture(opts); first.start('run-0001'); await first.close(200);
  // A newly queued event remains on disk while this stopped instance is offline.
  first.turn('run-0001', 1, { role: 'user' });
  destination = 'B'; const second = new TrajCapture(opts);
  try {
    assert.equal(await second.flush(50), false);
    assert.equal(fs.existsSync(`${r}/B`), false);
    assert.ok(fs.existsSync(`${r}/spool/run-0001/turns/0001.json`));
  } finally { await second.close(0); }
});

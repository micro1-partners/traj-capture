'use strict';
/**
 * @micro1/traj-shipper — land production-agent runs in micro1 trajectory storage.
 *
 *   const { TrajCapture } = require('@micro1/traj-shipper');
 *   const cap = TrajCapture.init({ code: process.env.TRAJ_CODE, agentName: 'acme-triage', instance: 'prod-us-1' });
 *   cap.start(runId, { model, parent_run_id });
 *   cap.turn(runId, n, { role, content, tool_calls, raw_request, raw_response });
 *   cap.end(runId, { status: 'completed' });
 *   cap.feedback(runId, { score: 4, expert_id: 'exp_7c1' });
 *
 * Event methods never throw or perform network I/O. With spoolDir they synchronously
 * persist local events and acknowledgements for restart recovery. Without it, a
 * crash loses in-memory work. Completion receipts require reconciled events.
 * Zero dependencies; Node 18+ (global fetch).
 */
const crypto = require('node:crypto');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');

const VERSION = '0.1.0';
const DEFAULT_ENROLL_URL = 'https://data.micro1.ai';
const SCHEMA_VERSION = 1;
const REFRESH_MARGIN_MS = 14 * 24 * 3600 * 1000;
const UPLOAD_RETRIES = 3;
const FINALIZE = '__finalize__';
const RETRYABLE = new Set([408, 429, 500, 502, 503, 504]);

const nowIso = () => new Date().toISOString().replace(/\.\d{3}Z$/, 'Z');
const sha256 = (b) => crypto.createHash('sha256').update(b).digest('hex');
const safeSeg = (s, fb) => (String(s ?? '').trim().replace(/[^A-Za-z0-9._-]/g, '-').slice(0, 128) || fb);
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
const warn = (...a) => { try { console.warn('traj-shipper:', ...a); } catch { /* never */ } };
const atomicWrite = (p, data) => {
  fs.mkdirSync(path.dirname(p), { recursive: true, mode: 0o700 });
  const tmp = path.join(path.dirname(p), `.capture-tmp-${crypto.randomUUID()}`);
  try {
    const fd = fs.openSync(tmp, 'wx', 0o600);
    try { fs.writeFileSync(fd, data); fs.fsyncSync(fd); } finally { fs.closeSync(fd); }
    fs.renameSync(tmp, p);
  } finally { fs.rmSync(tmp, { force: true }); }
};

// ── sinks ───────────────────────────────────────────────────────────────────

class DirSink {
  constructor(root) { this.root = root; }
  async put(rel, data) {
    const p = path.join(this.root, rel);
    await fs.promises.mkdir(path.dirname(p), { recursive: true });
    await fs.promises.writeFile(p, data);
  }
}

class BlobSink {
  constructor(sasUrl) {
    const [base, query = ''] = sasUrl.split('?', 2);
    this.base = base.replace(/\/+$/, ''); this.query = query;
  }
  async put(rel, data, contentType = 'application/json') {
    const url = `${this.base}/${rel.split('/').map(encodeURIComponent).join('/')}?${this.query}`;
    let last;
    for (let attempt = 0; attempt < UPLOAD_RETRIES; attempt++) {
      try {
        const res = await fetch(url, { method: 'PUT', body: data,
          signal: AbortSignal.timeout(120000),
          headers: { 'x-ms-blob-type': 'BlockBlob', 'Content-Type': contentType, 'Content-Length': String(data.length) } });
        if (res.ok) return;
        last = Object.assign(new Error(`PUT ${res.status}`), { status: res.status });
        if (!RETRYABLE.has(res.status)) throw last;
      } catch (e) {
        if (e && e.status && !RETRYABLE.has(e.status)) throw e;
        last = e;
      }
      await sleep(1000 * 2 ** attempt);
    }
    throw last;
  }
}

const makeSink = (sasUrl) => sasUrl.startsWith('file://') ? new DirSink(sasUrl.slice('file://'.length)) : new BlobSink(sasUrl);

// ── enrollment ──────────────────────────────────────────────────────────────

async function enroll(enrollUrl, code, hostHash) {
  const res = await fetch(`${enrollUrl.replace(/\/+$/, '')}/api/traj/enroll`, {
    signal: AbortSignal.timeout(30000),
    method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ code, host_hash: hostHash, client_version: `traj-shipper-node/${VERSION}` }),
  });
  const data = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(`enroll ${res.status}: ${data.message || 'rejected'}`);
  if (!data.sas_url) throw new Error('enroll response missing sas_url');
  return data;
}

class Credential {
  constructor({ code, enrollUrl, configPath, sasUrl, enrollFn }) {
    this.code = code; this.enrollUrl = enrollUrl; this.configPath = configPath;
    this.sasUrl = sasUrl || null; this.expiresAt = null; this.company = null;
    this.enrollFn = enrollFn || enroll;
    this.hostHash = sha256(os.hostname()).slice(0, 12);
    this.pending = null;
    this.codeHash = code ? sha256(code) : null;
    if (!this.sasUrl && configPath && fs.existsSync(configPath)) {
      try {
        const c = JSON.parse(fs.readFileSync(configPath, 'utf8'));
        if (!code || c.code_hash === this.codeHash) {
          this.sasUrl = c.sas_url || null; this.expiresAt = c.expires_at || null; this.company = c.company || null;
        }
      } catch { /* ignore */ }
    }
    this.destination = this.sasUrl ? this.sasUrl.split('?')[0] : null;
  }
  expiring() {
    if (!this.expiresAt) return false;
    const t = Date.parse(this.expiresAt);
    return Number.isFinite(t) && t - Date.now() < REFRESH_MARGIN_MS;
  }
  async sink() {
    if ((!this.sasUrl || this.expiring()) && this.code) {
      if (!this.pending) {
        this.pending = this.enrollFn(this.enrollUrl, this.code, this.hostHash).then((d) => {
          if ((this.company && d.company !== this.company) || (this.destination && d.sas_url.split('?')[0] !== this.destination)) {
            throw new Error('credential refresh changed company or destination');
          }
          this.sasUrl = d.sas_url; this.expiresAt = d.expires_at || null; this.company = d.company || null;
          this.destination = this.sasUrl.split('?')[0];
          if (this.configPath) {
            try {
              fs.mkdirSync(path.dirname(this.configPath), { recursive: true });
              atomicWrite(this.configPath, JSON.stringify({ company: this.company, sas_url: this.sasUrl, expires_at: this.expiresAt, code_hash: this.codeHash }));
            } catch (e) { warn('could not persist config:', e && e.message); }
          }
        }).finally(() => { this.pending = null; });
      }
      await this.pending;
    }
    if (!this.sasUrl) throw new Error('no credential (pass code or sasUrl)');
    return makeSink(this.sasUrl);
  }
  forget() { this.sasUrl = null; }
}

// ── the capture client ──────────────────────────────────────────────────────

class TrajCapture {
  constructor(opts) {
    const o = opts || {};
    this.agentName = safeSeg(o.agentName, 'agent');
    this.instance = safeSeg(o.instance, 'default');
    this.cred = new Credential({
      code: o.code, sasUrl: o.sasUrl,
      enrollUrl: o.enrollUrl || process.env.TRAJ_CAPTURE_ENROLL_URL || DEFAULT_ENROLL_URL,
      configPath: o.configPath || process.env.TRAJ_CAPTURE_CONFIG || null,
      enrollFn: o._enrollFn,
    });
    this.spool = o.spoolDir || null;
    this.max = o.maxQueue || 10000;
    this.q = [];
    this.runs = new Map();           // runId -> { name: {sha256, bytes} }
    this.records = new Map();
    this.retry = new Map();
    this.retryAt = 0;
    this.retryDelay = 500;
    this.retryTimer = null;
    this.disabled = false;
    this.binding = { agent: this.agentName, instance: this.instance, enroll_url: this.cred.enrollUrl,
      credential: sha256(o.code ? `code:${o.code}` : `destination:${this.cred.destination || 'unconfigured'}`) };
    this.stats = { queued: 0, uploaded: 0, dropped: 0, failed: 0 };
    this.draining = null;
    this.stopped = false;
    if (this.spool) {
      try {
        const bp = path.join(this.spool, '.binding.json');
        if (fs.existsSync(bp)) {
          const old = JSON.parse(fs.readFileSync(bp, 'utf8'));
          if (Object.keys(this.binding).some((k) => old[k] !== this.binding[k])) throw new Error('spool belongs to another deployment or enrollment');
          this.binding = old;
        } else if (fs.existsSync(this.spool) && fs.readdirSync(this.spool).length) {
          throw new Error('legacy spool is unbound; retain it for explicit reconciliation');
        }
        atomicWrite(bp, JSON.stringify(this.binding));
        this.sweepSpool();
      } catch (e) { this.disabled = true; warn('capture disabled:', e.message); }
    }
    if (o.flushOnExit !== false && typeof process !== 'undefined') {
      process.once('beforeExit', () => { this.flush(2000); });
    }
  }
  static init(opts) { return new TrajCapture(opts); }

  // Public event methods isolate errors; durable spooling performs local disk I/O.
  start(runId, meta) { this.guard(() => this.enqueue(runId, 'start.json', {
    schema_version: SCHEMA_VERSION, run_id: runId, agent_name: this.agentName, instance: this.instance,
    started_at: nowIso(), client: { name: 'traj-shipper-node', version: VERSION }, ...(meta || {}) })); }
  turn(runId, n, turn) { this.guard(() => {
    const k = Number(n); if (typeof n === 'boolean' || !Number.isInteger(k) || k < 1) throw new Error(`bad turn number ${n}`);
    this.enqueue(runId, `turns/${String(k).padStart(4, '0')}.json`,
      { schema_version: SCHEMA_VERSION, run_id: runId, turn: k, ts: nowIso(), ...(turn || {}) }); }); }
  end(runId, status) { this.guard(() => {
    this.enqueue(runId, 'end.json', { schema_version: SCHEMA_VERSION, run_id: runId, ended_at: nowIso(), status: 'completed', ...(status || {}) });
    this.enqueue(runId, FINALIZE, null); }); }
  feedback(runId, fb) { this.guard(() => this.enqueue(runId, 'feedback.json',
    { schema_version: SCHEMA_VERSION, run_id: runId, scored_at: nowIso(), ...(fb || {}) })); }

  async flush(timeoutMs = 2000) {
    const deadline = Date.now() + timeoutMs;
    while ((this.q.length || this.draining || this.retry.size) && Date.now() < deadline) {
      if (this.q.length || Date.now() >= this.retryAt) this.kick();
      await sleep(Math.min(10, Math.max(1, deadline - Date.now())));
    }
    return this.q.length === 0 && !this.draining && this.retry.size === 0;
  }
  async close(timeoutMs = 2000) { await this.flush(timeoutMs); this.stopped = true; clearTimeout(this.retryTimer); }

  // internals
  guard(fn) { try { fn(); } catch (e) { warn(e && e.message ? e.message : e); } }
  prefix(runId) { return `trajectories/${this.agentName}/${this.instance}/${safeSeg(runId, 'run')}`; }

  enqueue(runId, name, body) {
    if (this.disabled) return;
    if (typeof runId !== 'string' || !runId || runId === '.' || runId === '..' || safeSeg(runId, 'run') !== runId) throw new Error('runId must be a nonempty URL-safe identifier');
    const data = body === null ? null : Buffer.from(JSON.stringify(body));
    const record = this.record(runId);
    if (data) {
      const sig = { sha256: sha256(data), bytes: data.length };
      const old = record.expected[name];
      if (old && old.sha256 !== sig.sha256 && name !== 'feedback.json') {
        record.lost = true; this.save(runId); throw new Error('conflicting event for the same run/file');
      }
      record.expected[name] = sig;
      if (name === 'end.json') record.turns_expected = body.turns ?? null;
    } else { record.ended = true; }
    if (this.spool) {
      const d = path.join(this.spool, safeSeg(runId, 'run'));
      if (data) atomicWrite(path.join(d, name), data);
      else if (name === FINALIZE) atomicWrite(path.join(d, '.finalize'), '');
    }
    this.save(runId);
    this.push(runId, name, data);
  }
  record(runId) {
    if (!this.records.has(runId)) {
      const p = this.spool && path.join(this.spool, runId, '.capture.json');
      const r = p && fs.existsSync(p) ? JSON.parse(fs.readFileSync(p, 'utf8')) : { expected: {}, files: {}, ended: false, lost: false, turns_expected: null };
      this.records.set(runId, r); this.runs.set(runId, r.files);
    }
    return this.records.get(runId);
  }
  save(runId) { if (this.spool) atomicWrite(path.join(this.spool, runId, '.capture.json'), JSON.stringify(this.records.get(runId))); }
  key(runId, name) { return `${runId}\0${name}`; }
  push(runId, name, data) {
    if (this.q.length >= this.max) {
      const [rid, n] = this.q.shift(); this.stats.dropped++;
      if (this.spool) this.retry.set(this.key(rid, n), [rid, n, null]);
      else this.record(rid).lost = true;
    }
    this.q.push([runId, name, data]); this.stats.queued++;
    this.kick();
  }
  kick() {
    if (!this.draining && !this.stopped && !this.disabled) this.draining = this.drain().finally(() => {
      this.draining = null;
      if (this.q.length && !this.stopped) this.kick();
      else if (this.retry.size && !this.retryTimer && !this.stopped) {
        this.retryTimer = setTimeout(() => { this.retryTimer = null; this.kick(); }, Math.max(10, this.retryAt - Date.now()));
        this.retryTimer.unref();
      }
    });
  }

  sweepSpool() {
    if (!fs.existsSync(this.spool)) return;
    for (const run of fs.readdirSync(this.spool, { withFileTypes: true }).filter((d) => d.isDirectory()).map((d) => d.name).sort()) {
      const d = path.join(this.spool, run);
      if (fs.existsSync(path.join(d, '.receipt'))) {
        const feedback = path.join(d, 'feedback.json');
        if (fs.existsSync(feedback)) this.push(run, 'feedback.json', fs.readFileSync(feedback));
        continue;
      }
      const record = this.record(run);
      const files = [];
      const walk = (dir, rel) => { for (const e of fs.readdirSync(dir, { withFileTypes: true })) {
        if (e.name.startsWith('.')) continue;
        const r = rel ? `${rel}/${e.name}` : e.name;
        if (e.isDirectory()) walk(path.join(dir, e.name), r); else files.push(r); } };
      walk(d, '');
      for (const f of files.sort()) {
        const data = fs.readFileSync(path.join(d, f));
        record.expected[f] ||= { sha256: sha256(data), bytes: data.length };
        if (f === 'end.json') { record.ended = true; record.turns_expected = JSON.parse(data).turns ?? null; }
        if (record.files[f]?.sha256 !== record.expected[f].sha256) this.push(run, f, data);
      }
      this.save(run);
      if (record.ended || fs.existsSync(path.join(d, '.finalize'))) this.push(run, FINALIZE, null);
      if (files.length) warn(`re-shipping ${files.length} spooled file(s) for ${run}`);
    }
  }

  async drain() {
    if (Date.now() >= this.retryAt) {
      for (let [key, [runId, name, data]] of this.retry) {
        if (this.q.length >= this.max) break;
        this.retry.delete(key);
        if (data === null && name !== FINALIZE && this.spool) {
          const p = path.join(this.spool, runId, name);
          if (fs.existsSync(p)) data = fs.readFileSync(p);
        }
        // Already inside the drain, so don't recursively kick while requeueing.
        this.q.push([runId, name, data]);
      }
    }
    while (this.q.length) {
      const [runId, name, data] = this.q.shift();
      try {
        if (name === FINALIZE) await this.finalize(runId); else await this.put(runId, name, data);
        this.stats.uploaded++;
      } catch (e) {
        this.stats.failed++;
        warn(`upload ${runId}/${name} failed:`, e && e.message ? e.message : e);
        this.retryDelay = Math.min(60000, this.retryDelay * 2);
        this.retryAt = Date.now() + this.retryDelay;
        if (this.spool || this.retry.size < this.max) this.retry.set(this.key(runId, name), [runId, name, this.spool ? null : data]);
        else this.record(runId).lost = true;
        if (e && e.status === 403) this.cred.forget();
      }
    }
  }
  async put(runId, name, data) {
    if (data === null) throw new Error('pending event bytes unavailable');
    const sink = await this.sink();
    await sink.put(`${this.prefix(runId)}/${name}`, data, 'application/json');
    const record = this.record(runId);
    record.files[name] = { sha256: sha256(data), bytes: data.length };
    this.save(runId);
    if (this.spool && record.files[name].sha256 === record.expected[name]?.sha256) fs.rmSync(path.join(this.spool, runId, name), { force: true });
    this.retry.delete(this.key(runId, name));
    this.retryDelay = 500;
    if (name === 'feedback.json' && (record.complete || !record.ended)) { this.records.delete(runId); this.runs.delete(runId); }
  }
  async finalize(runId) {
    const record = this.record(runId);
    const files = Object.fromEntries(Object.entries(record.files).filter(([k]) => k !== 'feedback.json'));
    const expected = Object.fromEntries(Object.entries(record.expected).filter(([k]) => k !== 'feedback.json'));
    if (record.lost || !files['start.json'] || !files['end.json'] || Object.keys(files).length !== Object.keys(expected).length ||
        Object.keys(expected).some((k) => files[k]?.sha256 !== expected[k].sha256)) throw new Error('capture incomplete; receipt deferred');
    const turns = Object.keys(files).filter((n) => n.startsWith('turns/')).length;
    const n = record.turns_expected ?? turns;
    if (!Number.isInteger(n) || n < 0 || n !== turns || Array.from({ length: n }, (_, i) => `turns/${String(i + 1).padStart(4, '0')}.json`).some((k) => !files[k])) throw new Error('capture has missing turns; receipt deferred');
    const sink = await this.sink();
    const prefix = this.prefix(runId);
    const manifest = { schema_version: SCHEMA_VERSION, run_id: runId, agent_name: this.agentName, instance: this.instance,
      client: { name: 'traj-shipper-node', version: VERSION }, turns_landed: turns, turns_expected: n, dropped_events: 0,
      files, finalized_at: nowIso() };
    await sink.put(`${prefix}/manifest.json`, Buffer.from(JSON.stringify(manifest, null, 1)));
    const receipt = { schema_version: SCHEMA_VERSION, run_id: runId, agent_name: this.agentName, prefix,
      end_state: files['end.json'] ? 'observed' : 'inferred', completed_at: nowIso(), client_version: `traj-shipper-node/${VERSION}` };
    await sink.put(`trajectories/_receipts/${safeSeg(runId, 'run')}.json`, Buffer.from(JSON.stringify(receipt, null, 1)));
    this.retry.delete(this.key(runId, FINALIZE));
    record.complete = true; this.save(runId);
    this.runs.delete(runId);
    this.records.delete(runId);
    if (this.spool) { const d = path.join(this.spool, safeSeg(runId, 'run'));
      try { fs.writeFileSync(path.join(d, '.receipt'), ''); fs.rmSync(path.join(d, '.finalize'), { force: true }); } catch { /* ignore */ } }
  }

  async sink() {
    const sink = await this.cred.sink();
    if (this.spool) {
      const actual = { destination: this.cred.destination, company: this.cred.company };
      if ('destination' in this.binding && Object.keys(actual).some((k) => this.binding[k] !== actual[k])) throw new Error('spool storage binding changed; uploads withheld');
      if (!('destination' in this.binding)) {
        Object.assign(this.binding, actual);
        atomicWrite(path.join(this.spool, '.binding.json'), JSON.stringify(this.binding));
      }
    }
    return sink;
  }
}

module.exports = { TrajCapture, _DirSink: DirSink, _BlobSink: BlobSink, VERSION };

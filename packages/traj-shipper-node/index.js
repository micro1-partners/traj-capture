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
 * Every public method returns immediately and never throws. A background drain
 * uploads each event as its own blob (blob is the spool); a crash loses at most
 * what is still queued. No filesystem access unless `spoolDir` is set.
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
    if (!this.sasUrl && configPath && fs.existsSync(configPath)) {
      try {
        const c = JSON.parse(fs.readFileSync(configPath, 'utf8'));
        this.sasUrl = c.sas_url || null; this.expiresAt = c.expires_at || null; this.company = c.company || null;
      } catch { /* ignore */ }
    }
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
          this.sasUrl = d.sas_url; this.expiresAt = d.expires_at || null; this.company = d.company || null;
          if (this.configPath) {
            try {
              fs.mkdirSync(path.dirname(this.configPath), { recursive: true });
              fs.writeFileSync(this.configPath, JSON.stringify({ company: this.company, sas_url: this.sasUrl, expires_at: this.expiresAt }), { mode: 0o600 });
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
    this.stats = { queued: 0, uploaded: 0, dropped: 0, failed: 0 };
    this.draining = null;
    this.stopped = false;
    if (this.spool) this.guard(() => this.sweepSpool());
    if (o.flushOnExit !== false && typeof process !== 'undefined') {
      process.once('beforeExit', () => { this.flush(2000); });
    }
  }
  static init(opts) { return new TrajCapture(opts); }

  // public: never throw, never block
  start(runId, meta) { this.guard(() => this.enqueue(runId, 'start.json', {
    schema_version: SCHEMA_VERSION, run_id: runId, agent_name: this.agentName, instance: this.instance,
    started_at: nowIso(), client: { name: 'traj-shipper-node', version: VERSION }, ...(meta || {}) })); }
  turn(runId, n, turn) { this.guard(() => {
    const k = Number(n); if (!Number.isInteger(k) || k < 0) throw new Error(`bad turn number ${n}`);
    this.enqueue(runId, `turns/${String(k).padStart(4, '0')}.json`,
      { schema_version: SCHEMA_VERSION, run_id: runId, turn: k, ts: nowIso(), ...(turn || {}) }); }); }
  end(runId, status) { this.guard(() => {
    this.enqueue(runId, 'end.json', { schema_version: SCHEMA_VERSION, run_id: runId, ended_at: nowIso(), status: 'completed', ...(status || {}) });
    this.enqueue(runId, FINALIZE, null); }); }
  feedback(runId, fb) { this.guard(() => this.enqueue(runId, 'feedback.json',
    { schema_version: SCHEMA_VERSION, run_id: runId, scored_at: nowIso(), ...(fb || {}) })); }

  async flush(timeoutMs = 2000) {
    const deadline = Date.now() + timeoutMs;
    while ((this.q.length || this.draining) && Date.now() < deadline) {
      this.kick();
      await Promise.race([this.draining || Promise.resolve(), sleep(Math.max(1, deadline - Date.now()))]);
    }
    return this.q.length === 0 && !this.draining;
  }
  async close(timeoutMs = 2000) { await this.flush(timeoutMs); this.stopped = true; }

  // internals
  guard(fn) { try { fn(); } catch (e) { warn(e && e.message ? e.message : e); } }
  prefix(runId) { return `trajectories/${this.agentName}/${this.instance}/${safeSeg(runId, 'run')}`; }

  enqueue(runId, name, body) {
    const data = body === null ? null : Buffer.from(JSON.stringify(body));
    if (this.spool) {
      const d = path.join(this.spool, safeSeg(runId, 'run'));
      if (data) { fs.mkdirSync(path.join(d, path.dirname(name)), { recursive: true }); fs.writeFileSync(path.join(d, name), data); }
      else if (name === FINALIZE) { fs.mkdirSync(d, { recursive: true }); fs.writeFileSync(path.join(d, '.finalize'), ''); }
    }
    this.push(runId, name, data);
  }
  push(runId, name, data) {
    if (this.q.length >= this.max) { this.q.shift(); this.stats.dropped++; }
    this.q.push([runId, name, data]); this.stats.queued++;
    this.kick();
  }
  kick() { if (!this.draining && !this.stopped) this.draining = this.drain().finally(() => { this.draining = null; if (this.q.length && !this.stopped) this.kick(); }); }

  sweepSpool() {
    if (!fs.existsSync(this.spool)) return;
    for (const run of fs.readdirSync(this.spool, { withFileTypes: true }).filter((d) => d.isDirectory()).map((d) => d.name).sort()) {
      const d = path.join(this.spool, run);
      if (fs.existsSync(path.join(d, '.receipt'))) continue;
      const files = [];
      const walk = (dir, rel) => { for (const e of fs.readdirSync(dir, { withFileTypes: true })) {
        if (e.name.startsWith('.')) continue;
        const r = rel ? `${rel}/${e.name}` : e.name;
        if (e.isDirectory()) walk(path.join(dir, e.name), r); else files.push(r); } };
      walk(d, '');
      for (const f of files.sort()) this.push(run, f, fs.readFileSync(path.join(d, f)));
      if (fs.existsSync(path.join(d, '.finalize'))) this.push(run, FINALIZE, null);
      if (files.length) warn(`re-shipping ${files.length} spooled file(s) for ${run}`);
    }
  }

  async drain() {
    while (this.q.length) {
      const [runId, name, data] = this.q.shift();
      try {
        if (name === FINALIZE) await this.finalize(runId); else await this.put(runId, name, data);
        this.stats.uploaded++;
      } catch (e) {
        this.stats.failed++;
        warn(`upload ${runId}/${name} failed:`, e && e.message ? e.message : e);
        if (e && e.status === 403) this.cred.forget();
      }
    }
  }
  async put(runId, name, data) {
    const sink = await this.cred.sink();
    await sink.put(`${this.prefix(runId)}/${name}`, data, 'application/json');
    if (!this.runs.has(runId)) this.runs.set(runId, {});
    this.runs.get(runId)[name] = { sha256: sha256(data), bytes: data.length };
    if (this.spool) { try { fs.unlinkSync(path.join(this.spool, safeSeg(runId, 'run'), name)); } catch { /* gone */ } }
  }
  async finalize(runId) {
    const files = { ...(this.runs.get(runId) || {}) };
    const turns = Object.keys(files).filter((n) => n.startsWith('turns/')).length;
    const sink = await this.cred.sink();
    const prefix = this.prefix(runId);
    const manifest = { schema_version: SCHEMA_VERSION, run_id: runId, agent_name: this.agentName, instance: this.instance,
      client: { name: 'traj-shipper-node', version: VERSION }, turns_landed: turns, dropped_events: this.stats.dropped,
      files, finalized_at: nowIso() };
    await sink.put(`${prefix}/manifest.json`, Buffer.from(JSON.stringify(manifest, null, 1)));
    const receipt = { schema_version: SCHEMA_VERSION, run_id: runId, agent_name: this.agentName, prefix,
      end_state: files['end.json'] ? 'observed' : 'inferred', completed_at: nowIso(), client_version: `traj-shipper-node/${VERSION}` };
    await sink.put(`trajectories/_receipts/${safeSeg(runId, 'run')}.json`, Buffer.from(JSON.stringify(receipt, null, 1)));
    this.runs.delete(runId);
    if (this.spool) { const d = path.join(this.spool, safeSeg(runId, 'run'));
      try { fs.writeFileSync(path.join(d, '.receipt'), ''); fs.rmSync(path.join(d, '.finalize'), { force: true }); } catch { /* ignore */ } }
  }
}

module.exports = { TrajCapture, _DirSink: DirSink, _BlobSink: BlobSink, VERSION };

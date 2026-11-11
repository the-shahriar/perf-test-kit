// Declarative request engine shared by every k6 script in the kit.
//
// The project's cases (project.yaml `cases:`) arrive in the run config as data;
// this file turns them into requests. Nothing here knows the project's domain.
//
// Every request carries two tags the reports group by:
//   tc   -- the case id (stable across all reports), or AUX for helper requests
//   name -- the case label (also stops ids in URLs from splitting metrics)
// plus `phase` (warmup / measure for single-user runs, the stage phase for journeys).

import http from 'k6/http';
import { sleep } from 'k6';
import exec from 'k6/execution';
import { Counter, Trend } from 'k6/metrics';

export const responseBytes = new Trend('response_bytes');
export const errorDetail = new Counter('error_detail');
export const completedMs = new Trend('completed_ms', true);   // request -> polled job finished
export const TREND_STATS = ['count', 'min', 'avg', 'med', 'p(90)', 'p(95)', 'p(99)', 'max'];

// ── seeded random (mulberry32): seed = run seed and VU number ─────────────────
let seed = 1;
let rngState = null;
export function setSeed(value) { seed = Number(value) || 1; }
export function rand() {
  if (rngState === null) rngState = (seed * 1000003 + (exec.vu.idInTest || 0) * 7919) >>> 0;
  rngState = (rngState + 0x6D2B79F5) >>> 0;
  let t = rngState;
  t = Math.imul(t ^ (t >>> 15), t | 1);
  t ^= t + Math.imul(t ^ (t >>> 7), t | 61);
  return ((t ^ (t >>> 14)) >>> 0) / 4294967296;
}
export function pick(list) { return list && list.length ? list[Math.floor(rand() * list.length)] : null; }

export function loadRunConfig() {
  const cfg = JSON.parse(open(__ENV.RUN_CONFIG));
  setSeed(cfg.random_seed);
  return cfg;
}

export function summaryOutput(data) {
  const out = {};
  out[__ENV.SUMMARY_FILE] = JSON.stringify(data, null, 1);
  const d = data.metrics.http_req_duration;
  const reqs = data.metrics.http_reqs;
  out.stdout = '\n  requests: ' + (reqs ? reqs.values.count : 0) +
    (d ? ('   p95: ' + d.values['p(95)'].toFixed(1) + ' ms   max: ' + d.values.max.toFixed(1) + ' ms') : '') + '\n';
  return out;
}

// ── templates ─────────────────────────────────────────────────────────────────
const FULL = /^\{\{\s*([^{}]+?)\s*\}\}$/;
const PART = /\{\{\s*([^{}]+?)\s*\}\}/g;
const ALNUM = 'ABCDEFGHJKLMNPQRSTUVWXYZ0123456789';

export function lookup(obj, path) {
  if (path === undefined || path === null || path === '' || path === '.') return obj;
  const parts = String(path).split('.');
  for (const p of parts) {
    if (obj === null || obj === undefined) return null;
    obj = Array.isArray(obj) ? obj[Number(p)] : obj[p];
  }
  return obj === undefined ? null : obj;
}

function isoDate(offsetDays) {
  const d = new Date(Date.now() + (offsetDays || 0) * 86400000);
  return d.toISOString().slice(0, 10);
}

export class Engine {
  constructor(cfg) {
    this.cfg = cfg;
    this.base = cfg.base_url + (cfg.api_prefix || '');
    this.cases = cfg.cases || {};
    this.auth = cfg.auth || { header: 'Authorization', format: 'Bearer {token}' };
    this.pools = Object.assign({}, cfg.pools || {}, { records: cfg.record_ids || [] });
    this.vars = {};
    this.user = {};
    this.token = '';
    this.seqN = 0;
    this.phaseFn = null;
    this.onTimed = null;
  }

  reset() { this.vars = {}; }

  setOwnRecords(vu, users) {
    const ids = this.pools.records;
    this.pools.own_records = ids.filter((_, i) => i % users === (vu - 1) % users);
  }

  resolve(expr) {
    const parts = expr.split(':');
    const head = parts[0];
    const a = parts.slice(1);
    switch (head) {
      case 'pick': return pick(this.pools[a[0]] || lookup(this.cfg.vars || {}, a[0]));
      case 'int': return Math.floor(Number(a[0]) + rand() * (Number(a[1]) - Number(a[0]) + 1));
      case 'intpad': {   // intpad:lo:hi:width -> zero-padded integer
        const n = Math.floor(Number(a[0]) + rand() * (Number(a[1]) - Number(a[0]) + 1));
        return String(n).padStart(Number(a[2] || 6), '0');
      }
      case 'float': {
        const v = Number(a[0]) + rand() * (Number(a[1]) - Number(a[0]));
        const f = Math.pow(10, a[2] === undefined ? 2 : Number(a[2]));
        return Math.round(v * f) / f;
      }
      case 'bool': return rand() < Number(a[0] === undefined ? 0.5 : a[0]);
      case 'digits': { let s = ''; for (let i = 0; i < Number(a[0] || 8); i++) s += Math.floor(rand() * 10); return s; }
      case 'alnum': { let s = ''; for (let i = 0; i < Number(a[0] || 8); i++) s += ALNUM[Math.floor(rand() * ALNUM.length)]; return s; }
      case 'uuid': return 'xxxxxxxx-xxxx-4xxx-yxxx-xxxxxxxxxxxx'.replace(/[xy]/g, (c) => {
        const r = Math.floor(rand() * 16); return (c === 'x' ? r : (r & 0x3) | 0x8).toString(16);
      });
      case 'seq': this.seqN += 1; return 'v' + exec.vu.idInTest + '-' + exec.vu.iterationInScenario + '-' + this.seqN;
      case 'vu': return exec.vu.idInTest;
      case 'iter': return exec.vu.iterationInScenario;
      case 'today': return isoDate(0);
      case 'date': return isoDate(Number(a[0] || 0));
      case 'now': return new Date().toISOString();
      case 'epoch': return Date.now();
      default: break;
    }
    const scope = { var: this.vars, ctx: this.cfg.vars || {}, user: this.user, pools: this.pools };
    return lookup(scope, expr);
  }

  render(value) {
    if (typeof value === 'string') {
      const m = value.match(FULL);
      if (m) {
        // A placeholder can resolve to a template itself (e.g. a dataset's record
        // shape in ctx): render that one level further.
        const v = this.resolve(m[1]);
        if ((v !== null && typeof v === 'object') || (typeof v === 'string' && v.indexOf('{{') >= 0)) {
          return m[1].indexOf('ctx.') === 0 ? this.render(v) : v;
        }
        return v;
      }
      return value.replace(PART, (_, e) => { const v = this.resolve(e); return v === null || v === undefined ? '' : String(v); });
    }
    if (Array.isArray(value)) return value.map((v) => this.render(v));
    if (value && typeof value === 'object') {
      const out = {};
      for (const k of Object.keys(value)) out[k] = this.render(value[k]);
      return out;
    }
    return value;
  }

  applySet(set) {
    if (!set) return;
    for (const k of Object.keys(set)) this.vars[k] = this.render(set[k]);
  }

  headers(extra) {
    const h = Object.assign({ 'Content-Type': 'application/json' }, this.cfg.headers || {}, extra || {});
    if (this.token) h[this.auth.header] = this.auth.format.replace('{token}', this.token);
    return h;
  }

  url(path, query) {
    let u = /^https?:\/\//.test(path) ? path : this.base + path;
    if (query) {
      const qs = Object.keys(query).filter((k) => query[k] !== null && query[k] !== undefined && query[k] !== '')
        .map((k) => encodeURIComponent(k) + '=' + encodeURIComponent(query[k])).join('&');
      if (qs) u += (u.indexOf('?') >= 0 ? '&' : '?') + qs;
    }
    return u;
  }

  // One HTTP request from a spec {method, path, query, body, headers, expect, timeout}.
  send(spec, tc, label) {
    const r = this.render({ method: spec.method, path: spec.path, query: spec.query, body: spec.body, headers: spec.headers });
    if (this.phaseFn) exec.vu.metrics.tags.phase = this.phaseFn();
    const tags = { tc: tc, name: label };
    const expect = spec.expect && spec.expect.length ? spec.expect : null;
    const params = {
      headers: this.headers(r.headers), tags: tags, timeout: spec.timeout || this.cfg.timeout || '120s',
      responseCallback: expect ? http.expectedStatuses(...expect) : http.expectedStatuses({ min: 200, max: 399 }),
    };
    let body = null;
    if (r.body !== undefined && r.body !== null) body = typeof r.body === 'string' ? r.body : JSON.stringify(r.body);
    const res = http.request(String(r.method).toUpperCase(), this.url(r.path, r.query), body, params);
    responseBytes.add(res.body ? res.body.length : 0, tags);
    const ok = expect ? expect.indexOf(res.status) >= 0 : (res.status >= 200 && res.status < 400);
    if (!ok) errorDetail.add(1, Object.assign({ status: String(res.status), msg: snippet(res) }, tags));
    if (tc !== 'AUX' && this.onTimed) this.onTimed(res);
    return res;
  }

  extract(map, res) {
    if (!map) return;
    let json = null;
    try { json = JSON.parse(res.body); } catch (e) { json = null; }
    for (const k of Object.keys(map)) {
      const src = map[k];
      if (typeof src === 'string' && src.indexOf('header:') === 0) this.vars[k] = res.headers[src.slice(7)] || null;
      else if (src === 'status') this.vars[k] = res.status;
      else this.vars[k] = json === null ? null : lookup(json, src);
    }
  }

  missing(req) {
    return (req || []).some((k) => this.vars[k] === null || this.vars[k] === undefined || this.vars[k] === '');
  }

  // A case as it appears in a journey (or a helper step): set, request, extract, poll.
  runCase(id, measured) {
    const c = this.cases[id];
    if (!c) throw new Error('unknown case ' + id);
    this.applySet(c.set);
    if (this.missing(c.requires)) return null;
    const tc = measured === false ? 'AUX' : id;
    const started = Date.now();
    const res = this.send(c, tc, tc === 'AUX' ? 'aux' : c.label);
    this.extract(c.extract, res);
    if (c.poll) this.poll(c, res, id, started, tc);
    return res;
  }

  // A case on its own (single-user runs): its standalone before/after steps prepare state.
  runStandalone(id) {
    const c = this.cases[id];
    const s = c.standalone || {};
    this.applySet(s.set);
    for (const st of s.before || []) this.step(st);
    const res = this.runCase(id, true);
    for (const st of s.after || []) this.step(st);
    return res;
  }

  step(st) {
    this.applySet(st.set);
    if (st.case) return this.runCase(st.case, !!st.measure);
    if (st.path) {
      const res = this.send(st, 'AUX', 'aux');
      this.extract(st.extract, res);
      return res;
    }
    return null;
  }

  poll(c, res, id, started, tc) {
    const p = c.poll;
    if (p.when_status && p.when_status.indexOf(res.status) < 0) return;
    if (!(res.status >= 200 && res.status < 300)) return;
    const tags = { tc: id, name: c.label };
    const max = p.max_polls || 240;
    const done = p.done || ['completed', 'done', 'success'];
    const failed = p.failed || ['failed', 'error'];
    for (let i = 0; i < max; i++) {
      sleep(p.interval_seconds || 0.5);
      const r = this.send({ method: p.method || 'GET', path: p.path, query: p.query, headers: p.headers }, 'AUX', 'aux');
      let value = null;
      try { value = lookup(JSON.parse(r.body), p.until); } catch (e) { value = null; }
      if (done.indexOf(value) >= 0) {
        if (tc !== 'AUX') completedMs.add(Date.now() - started, tags);
        this.extract(p.extract, r);
        return;
      }
      if (failed.indexOf(value) >= 0 || r.status >= 400) {
        errorDetail.add(1, Object.assign({ status: 'job-' + value, msg: 'poll ended in ' + value }, tags));
        return;
      }
    }
    errorDetail.add(1, Object.assign({ status: 'job-timeout', msg: 'not finished after ' + max + ' polls' }, tags));
  }
}

// Error text travels as a tag value and the CSV output joins tags as "k=v&k=v".
function clean(text) { return String(text).replace(/[&=\r\n]+/g, ' ').slice(0, 120); }

function snippet(res) {
  if (res.status === 0) return clean(res.error || 'no response');
  try {
    const env = JSON.parse(res.body);
    const msg = env.message || env.detail || env.error || env.title || '';
    return clean(typeof msg === 'string' ? msg : JSON.stringify(msg));
  } catch (e) {
    return clean(res.body || '');
  }
}

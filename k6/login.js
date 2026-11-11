// Sign-in response time (case LOGIN), one user, paced under the project's
// sign-in rate limit (auth.per_minute). Each sample signs in the next test
// account and, when auth.logout is configured, signs that new session out
// again (AUX), so the accounts' working sessions are left alone.

import exec from 'k6/execution';
import { sleep } from 'k6';
import { Engine, TREND_STATS, loadRunConfig, lookup, summaryOutput } from './lib/engine.js';

const cfg = loadRunConfig();

export const options = {
  scenarios: { login: { executor: 'per-vu-iterations', vus: 1, iterations: 1, maxDuration: '2h' } },
  summaryTrendStats: TREND_STATS,
  insecureSkipTLSVerify: cfg.tls_verify === false,
};

export default function () {
  const e = new Engine(cfg);
  const interval = cfg.per_minute ? 60 / cfg.per_minute : 0;
  exec.vu.metrics.tags.phase = 'measure';
  for (let i = 0; i < cfg.samples; i++) {
    const started = Date.now();
    e.user = cfg.accounts[i % cfg.accounts.length];
    e.token = '';
    const res = e.send(cfg.login, 'LOGIN', 'Sign in');
    if (res.status >= 200 && res.status < 300 && cfg.logout) {
      let token = null;
      try { token = lookup(JSON.parse(res.body), cfg.login.token || 'token'); } catch (err) { token = null; }
      if (token) {
        e.token = token;
        e.send(cfg.logout, 'AUX', 'aux');
      }
    }
    const wait = interval - (Date.now() - started) / 1000;
    if (wait > 0) sleep(wait);
  }
}

export function handleSummary(data) { return summaryOutput(data); }

// Single-user response times: one virtual user, cases strictly one after another,
// no think time. Used by performance, smoke and each volume stage.
//
// Each case execution uses the next account's token, so no single account hits
// a per-user rate limit: the run measures the system, not the rate limiter.
// Phase tag: 'warmup' samples are excluded from results, 'measure' are kept.

import exec from 'k6/execution';
import { Engine, TREND_STATS, loadRunConfig, summaryOutput } from './lib/engine.js';

const cfg = loadRunConfig();
const tokens = cfg.tokens && cfg.tokens.length ? cfg.tokens : [''];

export const options = {
  scenarios: { single_user: { executor: 'per-vu-iterations', vus: 1, iterations: 1, maxDuration: cfg.max_duration || '6h' } },
  summaryTrendStats: TREND_STATS,
  insecureSkipTLSVerify: cfg.tls_verify === false,
};

export default function () {
  const e = new Engine(cfg);
  let n = 0;
  const run = (tc) => {
    e.token = tokens[n % tokens.length];
    n += 1;
    e.reset();
    e.runStandalone(tc);
  };
  for (const tc of cfg.case_list) {
    exec.vu.metrics.tags.phase = 'warmup';
    for (let i = 0; i < (cfg.warmup || 0); i++) run(tc);
    exec.vu.metrics.tags.phase = 'measure';
    for (let i = 0; i < cfg.iterations; i++) run(tc);
  }
}

export function handleSummary(data) { return summaryOutput(data); }

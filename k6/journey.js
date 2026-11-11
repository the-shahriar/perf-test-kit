// Concurrent users: each virtual user is one test account repeating the
// project's journey (project.yaml `journey:`) with think time. Used by load,
// stress, spike, soak, volume and smoke.
//
// The user count follows cfg.stages ([{duration, target, phase}]); every sample
// carries the phase of the stage it was sent in, and the reports read the
// phase(s) named in the run's metadata (normally `steady`).
// VU n always uses token n and edits only its own slice of the record pool
// (`own_records`), so the test design itself creates no edit conflicts.

import exec from 'k6/execution';
import { sleep } from 'k6';
import { Trend } from 'k6/metrics';
import { Engine, TREND_STATS, loadRunConfig, rand, summaryOutput } from './lib/engine.js';

const cfg = loadRunConfig();
const journeyActive = new Trend('journey_active_ms', true);
const tokens = cfg.tokens && cfg.tokens.length ? cfg.tokens : [''];
const stages = cfg.stages;
const maxUsers = Math.max(...stages.map((s) => s.target), 1);
const bounds = [];
let acc = 0;
for (const s of stages) { acc += s.duration; bounds.push({ end: acc, phase: s.phase }); }

export const options = {
  scenarios: {
    journey: {
      executor: 'ramping-vus',
      startVUs: 0,
      stages: stages.map((s) => ({ duration: s.duration + 's', target: s.target })),
      gracefulRampDown: '30s',
      gracefulStop: '30s',
    },
  },
  summaryTrendStats: TREND_STATS,
  insecureSkipTLSVerify: cfg.tls_verify === false,
};

function phase() {
  const t = exec.instance.currentTestRunDuration / 1000;
  for (const b of bounds) if (t < b.end) return b.phase;
  return bounds.length ? bounds[bounds.length - 1].phase : 'steady';
}

function think() {
  const tt = cfg.think_time || [1, 3];
  sleep(tt[0] + rand() * (tt[1] - tt[0]));
}

let engine = null;

export default function () {
  const vu = exec.vu.idInTest;
  if (engine === null) {
    engine = new Engine(cfg);
    engine.token = tokens[(vu - 1) % tokens.length];
    engine.setOwnRecords(vu, maxUsers);
    engine.phaseFn = phase;
  }
  engine.reset();
  let active = 0;
  engine.onTimed = (res) => { active += res.timings.duration; };

  for (const step of cfg.journey.steps) {
    if (step.probability !== undefined && rand() >= step.probability) continue;
    const repeat = step.repeat || 1;
    for (let r = 0; r < repeat; r++) {
      if (step.while && engine.missing([step.while])) break;
      engine.applySet(step.set);
      const ids = (step.case ? [step.case] : []).concat(step.cases || []);
      for (const id of ids) engine.runCase(id, true);
      if (step.think !== false) think();
    }
  }
  journeyActive.add(active);
}

export function handleSummary(data) { return summaryOutput(data); }

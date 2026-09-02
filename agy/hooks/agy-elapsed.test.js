#!/usr/bin/env node
'use strict';

const test = require('node:test');
const assert = require('node:assert/strict');
const { spawnSync } = require('node:child_process');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');

const SCRIPT = path.join(__dirname, 'agy-elapsed.js');

function freshTmpDir() {
  return fs.mkdtempSync(path.join(os.tmpdir(), 'agy-elapsed-test-'));
}

function run(payload, { tmpdir, fakeNow, env } = {}) {
  const dir = tmpdir || freshTmpDir();
  const childEnv = Object.assign({}, process.env, { TMPDIR: dir }, env || {});
  if (fakeNow != null) childEnv.AGY_FAKE_NOW = String(fakeNow);
  const result = spawnSync(process.execPath, [SCRIPT], {
    input: JSON.stringify(payload),
    encoding: 'utf8',
    env: childEnv,
  });
  return { text: (result.stdout || '').trim(), dir };
}

function stateFile(dir, sessionId) {
  if (!sessionId) return path.join(dir, 'agy-elapsed-state.json');
  const hash = require('node:crypto').createHash('sha256').update(sessionId).digest('hex');
  return path.join(dir, `agy-elapsed-state-${hash}.json`);
}

test('true first run: turn 1 working shows ~0s elapsed', () => {
  const { text } = run({ agent_state: 'working', session_id: 's1', model: { display_name: 'Claude' } }, { fakeNow: 1000 });
  assert.match(text, /^⏱ 0s/);
});

test('reset between turns is deterministic via seeded state', () => {
  const dir = freshTmpDir();
  const sessionId = 's-reset';
  const T0 = 1_000_000;
  fs.writeFileSync(stateFile(dir, sessionId), JSON.stringify({
    turnStart: T0,
    lastSeen: T0 + 3000,
    lastIdle: true,
    doneAt: T0 + 3000,
    doneText: '✓ 3s · done 12:00',
  }));
  const T1 = T0 + 10_000;
  const { text } = run(
    { agent_state: 'working', session_id: sessionId, model: { display_name: 'Claude' } },
    { tmpdir: dir, fakeNow: T1 }
  );
  assert.match(text, /^⏱ 0s/, `expected fresh ~0s elapsed on new turn, got: ${text}`);
});

test('no reset within one turn: two consecutive working polls keep same turnStart', () => {
  const dir = freshTmpDir();
  const sessionId = 's-noreset';
  const T0 = 2_000_000;
  const first = run(
    { agent_state: 'working', session_id: sessionId, model: { display_name: 'Claude' } },
    { tmpdir: dir, fakeNow: T0 }
  );
  assert.match(first.text, /^⏱ 0s/);
  const T1 = T0 + 5000;
  const second = run(
    { agent_state: 'working', session_id: sessionId, model: { display_name: 'Claude' } },
    { tmpdir: dir, fakeNow: T1 }
  );
  assert.match(second.text, /^⏱ 5s/, `expected elapsed to grow within same turn, got: ${second.text}`);
});

test('tool_use counts as active, not idle: working -> tool_use -> working does not reset or show done', () => {
  const dir = freshTmpDir();
  const sessionId = 's-tooluse';
  const T0 = 3_000_000;
  run({ agent_state: 'working', session_id: sessionId, model: { display_name: 'Claude' } }, { tmpdir: dir, fakeNow: T0 });
  const T1 = T0 + 2000;
  const mid = run({ agent_state: 'tool_use', session_id: sessionId, model: { display_name: 'Claude' } }, { tmpdir: dir, fakeNow: T1 });
  assert.ok(!mid.text.startsWith('✓'), `tool_use must not show a done line, got: ${mid.text}`);
  const T2 = T0 + 4000;
  const after = run({ agent_state: 'working', session_id: sessionId, model: { display_name: 'Claude' } }, { tmpdir: dir, fakeNow: T2 });
  assert.match(after.text, /^⏱ 4s/, `expected timer to keep counting through tool_use, got: ${after.text}`);
});

test('heartbeat staleness: state older than 6h is wiped, from a working poll', () => {
  const dir = freshTmpDir();
  const sessionId = 's-stale-working';
  const T0 = 10_000_000;
  fs.writeFileSync(stateFile(dir, sessionId), JSON.stringify({
    turnStart: T0,
    lastSeen: T0,
    lastIdle: false,
  }));
  const T1 = T0 + 7 * 60 * 60 * 1000;
  const { text } = run(
    { agent_state: 'working', session_id: sessionId, model: { display_name: 'Claude' } },
    { tmpdir: dir, fakeNow: T1 }
  );
  assert.match(text, /^⏱ 0s/, `expected wiped state to restart near 0s, got: ${text}`);
});

test('heartbeat staleness: state older than 6h is wiped, from an idle poll', () => {
  const dir = freshTmpDir();
  const sessionId = 's-stale-idle';
  const T0 = 20_000_000;
  fs.writeFileSync(stateFile(dir, sessionId), JSON.stringify({
    turnStart: T0,
    lastSeen: T0,
    lastIdle: false,
  }));
  const T1 = T0 + 7 * 60 * 60 * 1000;
  const { text } = run(
    { agent_state: 'idle', session_id: sessionId, model: { display_name: 'Claude' } },
    { tmpdir: dir, fakeNow: T1 }
  );
  assert.ok(!text.startsWith('✓'), `expected wiped state to not show a stale done line, got: ${text}`);
});

test('missing session_id falls back to shared default file without crashing', () => {
  const dir = freshTmpDir();
  const { text } = run({ agent_state: 'working', model: { display_name: 'Claude' } }, { tmpdir: dir, fakeNow: 5000 });
  assert.match(text, /^⏱ 0s/);
  assert.ok(fs.existsSync(path.join(dir, 'agy-elapsed-state.json')));
});

test('quota label for non-Gemini model reads "3rd-party", not the model name', () => {
  // Locks in this script's current label string, not antigravity's real bucket
  // structure. Re-check against a fresh captured payload if the installed agy
  // version is bumped.
  const dir = freshTmpDir();
  const payload = {
    agent_state: 'working',
    session_id: 's-quota',
    model: { display_name: 'DeepSeek V3' },
    quota: {
      '3p-5h': { remaining_fraction: 0.4, reset_in_seconds: 3600 },
      '3p-weekly': { remaining_fraction: 0.9, reset_in_seconds: 200000 },
      'gemini-5h': { remaining_fraction: 0.99, reset_in_seconds: 100 },
    },
  };
  const { text } = run(payload, { tmpdir: dir, fakeNow: 1000 });
  assert.match(text, /3rd-party 60% used/, `expected fixed "3rd-party" label, got: ${text}`);
  assert.doesNotMatch(text, /DeepSeek \d+%/, `quota label itself must not use the model name, got: ${text}`);
});

test('quota window label: 5h bucket is tightest, label says "5h"', () => {
  const dir = freshTmpDir();
  const payload = {
    agent_state: 'working',
    session_id: 's-quota-5h',
    model: { display_name: 'DeepSeek V3' },
    quota: {
      '3p-5h': { remaining_fraction: 0.4, reset_in_seconds: 3600 },
      '3p-weekly': { remaining_fraction: 0.9, reset_in_seconds: 200000 },
    },
  };
  const { text } = run(payload, { tmpdir: dir, fakeNow: 1000 });
  assert.match(text, /3rd-party 60% used \(5h, resets in 1h\)/, `expected 5h window label, got: ${text}`);
});

test('quota window label: weekly bucket is tightest, label says "wk"', () => {
  const dir = freshTmpDir();
  const payload = {
    agent_state: 'working',
    session_id: 's-quota-wk',
    model: { display_name: 'DeepSeek V3' },
    quota: {
      '3p-5h': { remaining_fraction: 0.9, reset_in_seconds: 3600 },
      '3p-weekly': { remaining_fraction: 0.4, reset_in_seconds: 200000 },
    },
  };
  const { text } = run(payload, { tmpdir: dir, fakeNow: 1000 });
  assert.match(text, /3rd-party 60% used \(wk, resets in/, `expected wk window label, got: ${text}`);
});

test('torn-write recovery: stray .tmp.<pid> file does not interfere with reading valid state', () => {
  const dir = freshTmpDir();
  const sessionId = 's-torn';
  const sf = stateFile(dir, sessionId);
  const T0 = 4_000_000;
  fs.writeFileSync(sf, JSON.stringify({ turnStart: T0, lastSeen: T0, lastIdle: false }));
  fs.writeFileSync(`${sf}.tmp.99999`, '{"corrupted": tr');
  const T1 = T0 + 3000;
  const { text } = run(
    { agent_state: 'working', session_id: sessionId, model: { display_name: 'Claude' } },
    { tmpdir: dir, fakeNow: T1 }
  );
  assert.match(text, /^⏱ 3s/, `expected valid state to load correctly despite stray tmp file, got: ${text}`);
});

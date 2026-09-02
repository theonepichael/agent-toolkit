#!/usr/bin/env node
/**
 * agy elapsed-time status line
 *
 * Shows: ⏱ Xm Ys · ↓ Nk tokens · Model  (while running)
 *        ✓ Xm Ys · done HH:MM            (8s after finishing)
 *
 * Wire up inside agy: /statusline node ~/.claude/hooks/agy-elapsed.js
 * Or set in ~/.gemini/antigravity-cli/settings.json:
 *   "statusLine": { "command": "node ~/.claude/hooks/agy-elapsed.js", "stackWithDefault": true }
 *
 * Set AGY_STATUSLINE_DEBUG=1 to log the raw payload to /tmp/agy-statusline-debug.log
 */

const fs = require('fs');
const os = require('os');
const crypto = require('crypto');

// Generous on purpose: a heuristic floor under the lastIdle-based reset, for
// a crash/kill mid-turn that lastIdle alone can't catch (no idle poll ever
// arrives to record it). Not a derived number — chosen to sit far above any
// real continuous-polling gap seen in captured data (up to ~80s).
const STALE_MS = 6 * 60 * 60 * 1000; // 6h

function fmtDuration(ms) {
  const s = Math.round(ms / 1000);
  if (s < 60) return `${s}s`;
  const m = Math.floor(s / 60);
  const rem = s % 60;
  return rem > 0 ? `${m}m ${rem}s` : `${m}m`;
}

function fmtTokens(n) {
  if (!n || n === 0) return null;
  if (n >= 1000) return `${(n / 1000).toFixed(1)}k tokens`;
  return `${n} tokens`;
}

function fmtResetIn(seconds) {
  if (!seconds || seconds <= 0) return null;
  const h = Math.floor(seconds / 3600);
  const m = Math.floor((seconds % 3600) / 60);
  if (h > 0) return m > 0 ? `${h}h ${m}m` : `${h}h`;
  return `${m}m`;
}

function fmtQuota(quota, modelName) {
  if (!quota || typeof quota !== 'object') return null;
  const rawName = (modelName || '').trim();
  const m = rawName.toLowerCase();
  const isGemini = m.includes('gemini');
  const prefix = isGemini ? 'gemini-' : '3p-';

  let label = 'Gemini';
  if (!isGemini) {
    // 3p-* buckets are a single pool shared across all non-Gemini models,
    // confirmed empirically (not documented by antigravity.google).
    label = '3rd-party';
  }

  // Find the tightest (lowest remaining_fraction = highest usage) bucket for this provider
  const buckets = Object.entries(quota)
    .filter(([k]) => k.startsWith(prefix))
    .sort(([, a], [, b]) => a.remaining_fraction - b.remaining_fraction);
  if (!buckets.length) return null;
  const [tightestKey, tightest] = buckets[0];
  const usedPct = Math.round((1 - tightest.remaining_fraction) * 100);
  const reset = fmtResetIn(tightest.reset_in_seconds);
  const window = tightestKey.endsWith('-weekly') ? 'wk' : '5h';
  if (reset) return `${label} ${usedPct}% used (${window}, resets in ${reset})`;
  return `${label} ${usedPct}% used (${window})`;
}

function loadState(path) {
  try {
    const parsed = JSON.parse(fs.readFileSync(path, 'utf8'));
    // Valid JSON that isn't an object (e.g. a bare "null" or a number) would
    // otherwise throw downstream the first time a field is read off it.
    return parsed && typeof parsed === 'object' ? parsed : {};
  } catch (_) { return {}; }
}

function saveState(path, s) {
  try {
    const tmp = `${path}.tmp.${process.pid}`;
    fs.writeFileSync(tmp, JSON.stringify(s));
    fs.renameSync(tmp, path);
  } catch (e) {
    if (process.env.AGY_STATUSLINE_DEBUG) console.error('saveState failed:', e);
  }
}

let raw = '';
process.stdin.setEncoding('utf8');
process.stdin.on('data', chunk => { raw += chunk; });
process.stdin.on('end', () => {
  let data = {};
  try { data = JSON.parse(raw); } catch (_) {}

  if (process.env.AGY_STATUSLINE_DEBUG) {
    fs.appendFileSync('/tmp/agy-statusline-debug.log',
      new Date().toISOString() + '\n' + JSON.stringify(data, null, 2) + '\n---\n');
  }

  // Test-only override: normal/production use always takes Date.now(). Env
  // vars are always strings, so parse explicitly rather than let a raw
  // string "now" silently turn later arithmetic/serialization into string
  // concatenation.
  const now = process.env.AGY_FAKE_NOW != null && process.env.AGY_FAKE_NOW !== ''
    ? Number(process.env.AGY_FAKE_NOW)
    : Date.now();

  // crypto.update() throws on a non-string/Buffer input; a malformed payload
  // could hand session_id as a number or object, so coerce defensively.
  const sessionId = typeof data.session_id === 'string'
    ? data.session_id
    : (data.session_id ? String(data.session_id) : '');
  const sessionKey = sessionId
    ? crypto.createHash('sha256').update(sessionId).digest('hex')
    : '';
  const STATE_FILE = sessionKey
    ? `${os.tmpdir()}/agy-elapsed-state-${sessionKey}.json`
    : `${os.tmpdir()}/agy-elapsed-state.json`;

  // Real agy data model: agent_state has 5 observed values — working, idle,
  // tool_use, authenticating, running. Deny-list, not allow-list: any future
  // or rare state this script hasn't seen defaults to "still in a turn"
  // rather than "turn ended" — the safer direction, since treating an active
  // state as active (timer keeps counting) is much milder than treating it
  // as idle (spurious premature "done" line).
  const IDLE_STATES = new Set(['idle', 'authenticating']);
  const agentState = (data.agent_state || '').toLowerCase();
  const isRunning = !IDLE_STATES.has(agentState);
  const isThinking = isRunning && (data.model?.id || '').toLowerCase().includes('thinking');
  // Tokens: sum of input+output from context_window
  const ctxWindow = data.context_window || {};
  const totalTokens = (ctxWindow.total_input_tokens || 0) + (ctxWindow.total_output_tokens || 0);
  // Model: object with display_name
  const modelName = (data.model?.display_name || data.model?.id || '');
  const state = loadState(STATE_FILE);
  let text = '';

  // Staleness check runs uniformly before the running/idle branch, so it
  // isn't missed on the idle branch. A crash/kill mid-turn leaves no idle
  // poll to record lastIdle, so this heartbeat is the only backstop for it.
  const lastSeen = state.lastSeen != null ? state.lastSeen : state.turnStart;
  if (state.turnStart && now - lastSeen > STALE_MS) {
    for (const k of Object.keys(state)) delete state[k];
  }
  state.lastSeen = now;

  if (isRunning) {
    if (!state.turnStart || state.lastIdle) {
      state.turnStart = now;
      state.lastIdle = false;
    }
    const elapsed = now - state.turnStart;
    const icon = isThinking ? '🤔' : '⏱';
    const parts = [`${icon} ${fmtDuration(elapsed)}`];
    const tok = fmtTokens(totalTokens);
    if (tok) parts.push(`↓ ${tok}`);
    if (modelName) {
      const m = modelName
        .replace(/\s*\(Thinking\)/i, '')
        .replace(/^(Claude|Gemini)\s+/i, '')
        .trim();
      if (m) parts.push(m);
    }
    const quota = fmtQuota(data.quota, modelName);
    if (quota) parts.push(quota);
    text = parts.join(' · ');

  } else {
    if (state.turnStart && !state.lastIdle) {
      const elapsed = now - state.turnStart;
      const finishedAt = new Date(now).toLocaleTimeString('en-US', { hour: '2-digit', minute: '2-digit' });
      const quota = fmtQuota(data.quota, modelName);
      text = `✓ ${fmtDuration(elapsed)} · done ${finishedAt}${quota ? ' · ' + quota : ''}`;
      state.lastIdle = true;
      state.doneAt = now;
      state.doneText = text;
    } else if (state.lastIdle && state.doneText) {
      // Recompute quota dynamically so percentage and reset time update live during idle
      const quota = fmtQuota(data.quota, modelName);
      const donePrefix = state.doneText.split(' · ').slice(0, 2).join(' · ');
      text = `${donePrefix}${quota ? ' · ' + quota : ''}`;
    } else {
      // If no recent turn recorded yet in state, construct a baseline idle statusline
      const parts = [];
      if (modelName) {
        const m = modelName
          .replace(/\s*\(Thinking\)/i, '')
          .replace(/^(Claude|Gemini)\s+/i, '')
          .trim();
        if (m) parts.push(m);
      }
      const quota = fmtQuota(data.quota, modelName);
      if (quota) parts.push(quota);
      text = parts.length ? parts.join(' · ') : '';
    }
  }

  saveState(STATE_FILE, state);
  process.stdout.write(text + '\n');
});

// pi/extensions/swarm-lib/swarm-tool-context.ts
import { randomUUID } from "node:crypto";
import { existsSync, mkdirSync, readFileSync, rmSync, writeFileSync } from "node:fs";
import { homedir } from "node:os";
import { join as join2 } from "node:path";
import { spawn } from "node:child_process";

// pi/extensions/swarm-lib/swarm-scheduling.ts
var OPEN_PANE_SOFT_CAP_MULTIPLIER = 2;
var TERMINAL_AGENT_STATUSES = ["idle", "done"];
function isTerminalAgentStatus(status) {
  return status !== void 0 && TERMINAL_AGENT_STATUSES.includes(status);
}
var RECONCILE_MIN_AGE_MS = 6e4;
function staleWorkerRecords(state, live, now) {
  const statusById = new Map(live.map((e) => [e.id, e.status]));
  return state.workers.filter((w) => {
    if (!statusById.has(w.agent)) return true;
    const status = statusById.get(w.agent);
    if (status === void 0) return false;
    if (!isTerminalAgentStatus(status)) return false;
    const began = w.workingSinceMs ?? w.awaitingRelaySinceMs;
    if (began === void 0) return false;
    return now - began >= RECONCILE_MIN_AGE_MS;
  });
}
var SIDECAR_VERSION = 1;
var FATAL_SIDECAR_RESULT = "fatal_error";
function parseFatalSidecar(raw) {
  let parsed;
  try {
    parsed = JSON.parse(raw);
  } catch {
    return null;
  }
  if (typeof parsed !== "object" || parsed === null) return null;
  const rec = parsed;
  if (rec.v !== SIDECAR_VERSION) return null;
  if (rec.result !== FATAL_SIDECAR_RESULT) return null;
  if (typeof rec.stopReason !== "string") return null;
  if (typeof rec.model !== "string") return null;
  if (typeof rec.writtenAtMs !== "number" || !Number.isFinite(rec.writtenAtMs)) return null;
  return {
    v: SIDECAR_VERSION,
    result: FATAL_SIDECAR_RESULT,
    stopReason: rec.stopReason,
    model: rec.model,
    writtenAtMs: rec.writtenAtMs
  };
}
function classifyOutcomeDraft(kind, witnesses) {
  const base = {
    sidecarProbed: witnesses.sidecarProbed,
    ...witnesses.herdrStatus !== void 0 ? { herdrStatus: witnesses.herdrStatus } : {}
  };
  if (witnesses.sidecar) {
    return { ...base, processResult: "fatal_error", evidence: "fatal_sidecar" };
  }
  if (witnesses.paneMatch === "fatal_sentinel") {
    return { ...base, processResult: "unknown_crash", evidence: "pane_sentinel" };
  }
  if (witnesses.paneMatch === "provider_wording") {
    return { ...base, processResult: "unknown_crash", evidence: "provider_wording" };
  }
  if (kind === "timed_out") {
    return {
      ...base,
      processResult: "deadline_stopped",
      evidence: witnesses.livenessConfirmed === false ? "no_observation" : "herdr_status"
    };
  }
  if (kind === "error") {
    return { ...base, processResult: "gone", evidence: "herdr_status" };
  }
  return {
    ...base,
    processResult: "settled_alive",
    evidence: witnesses.paneRead ? "pane_clear" : "no_observation"
  };
}
function appendOutcome(state, outcome) {
  const outcomes = state.outcomes ?? [];
  const at = outcomes.findIndex(
    (o) => o.agent === outcome.agent && o.decidedAtMs === outcome.decidedAtMs
  );
  if (at >= 0) outcomes.splice(at, 1, outcome);
  else outcomes.push(outcome);
  state.outcomes = outcomes;
  return outcomes;
}
function priorOutcome(state, agent) {
  return (state.outcomes ?? []).find((o) => o.agent === agent);
}
var PROJECT_PREFIXES = ["iron-lb-", "meta-", "work-", "atk-"];
function nextAgentId(runId, counter, slug) {
  const cleanSlug = slug ? slug.replace(/[^a-zA-Z0-9_-]/g, "") : "";
  const matched = PROJECT_PREFIXES.filter((prefix) => cleanSlug.startsWith(prefix)).sort(
    (a, b) => b.length - a.length
  )[0];
  const stripped = matched ? cleanSlug.slice(matched.length) : cleanSlug;
  if (!stripped) return `${runId}-w${counter}`;
  const base = `${runId}-w${counter}-${stripped}`;
  if (base.length <= 32) return base;
  const remaining = 32 - `${runId}-w${counter}-`.length;
  if (remaining < 1) return base.slice(0, 32);
  return `${runId}-w${counter}-${stripped.slice(-remaining)}`;
}
function stalledRelayWorkers(workers, now, stallMs) {
  return workers.filter(
    (w) => w.lifecycle === "awaiting_relay" && w.awaitingRelaySinceMs !== void 0 && now - w.awaitingRelaySinceMs >= stallMs
  );
}
function parseReadyItems(stdout) {
  try {
    const parsed = JSON.parse(stdout);
    if (!Array.isArray(parsed)) return [];
    return parsed.filter((i) => typeof i?.id === "string");
  } catch {
    return [];
  }
}
function parseShownItem(stdout) {
  try {
    const parsed = JSON.parse(stdout);
    return parsed && typeof parsed === "object" ? parsed : null;
  } catch {
    return null;
  }
}
function isSuspiciousFinish(shownStatus, captureCount) {
  if (captureCount > 0) return false;
  return shownStatus === "open" || shownStatus === "in-progress";
}
var PROVIDER_CRASH_SCAN_LINES = 10;
var PROVIDER_CRASH_MAX_GAP = 80;
var PROVIDER_CRASH_SIGNATURE_PAIRS = [
  ["usage limit", ["reached", "exceeded"]],
  ["rate limit", ["exceeded", "hit"]]
];
function stripTerminalNoise(text) {
  const csi = /\x1b\[[0-9;?]*[ -/]*[@-~]/g;
  const osc = /\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)/g;
  const c0 = new RegExp(`[\\x00-\\x08\\x0b-\\x1f\\x7f]`, "g");
  return text.replace(csi, "").replace(osc, "").replace(c0, "");
}
function normalizedPaneWindow(content, windowLines) {
  const recent = content.split("\n").filter((line) => line.trim() !== "").slice(-windowLines).join(" ");
  return stripTerminalNoise(recent).replace(/\s+/g, " ");
}
function occurrenceIndices(haystack, needle) {
  const lower = haystack.toLowerCase();
  const target = needle.toLowerCase();
  const indices = [];
  for (let i = lower.indexOf(target); i !== -1; i = lower.indexOf(target, i + 1)) {
    indices.push(i);
  }
  return indices;
}
var EXCERPT_BEFORE = 60;
var EXCERPT_AFTER = 140;
function providerCrashMatch(content) {
  const normalized = normalizedPaneWindow(content, PROVIDER_CRASH_SCAN_LINES);
  if (normalized.trim() === "") return null;
  for (const [keyword, verbs] of PROVIDER_CRASH_SIGNATURE_PAIRS) {
    const keywordIndices = occurrenceIndices(normalized, keyword);
    if (keywordIndices.length === 0) continue;
    for (const verb of verbs) {
      const verbIndices = occurrenceIndices(normalized, verb);
      for (const ki of keywordIndices) {
        for (const vi of verbIndices) {
          if (Math.abs(ki - vi) <= PROVIDER_CRASH_MAX_GAP) {
            const start = Math.max(0, Math.min(ki, vi) - EXCERPT_BEFORE);
            const end = Math.min(normalized.length, Math.max(ki, vi) + EXCERPT_AFTER);
            const excerpt = (start > 0 ? "..." : "") + normalized.slice(start, end).trim() + (end < normalized.length ? "..." : "");
            return { signature: `${keyword} ~ ${verb}`, excerpt };
          }
        }
      }
    }
  }
  return null;
}
var FATAL_ERROR_EXIT_TOKEN = "[fatal-error-exit]";
var FATAL_ERROR_EXIT_SCAN_LINES = 40;
function fatalErrorExitMatch(content) {
  const normalized = normalizedPaneWindow(content, FATAL_ERROR_EXIT_SCAN_LINES);
  const at = normalized.indexOf(FATAL_ERROR_EXIT_TOKEN);
  if (at === -1) return null;
  const start = Math.max(0, at - EXCERPT_BEFORE);
  const end = Math.min(normalized.length, at + FATAL_ERROR_EXIT_TOKEN.length + EXCERPT_AFTER);
  const excerpt = (start > 0 ? "..." : "") + normalized.slice(start, end).trim() + (end < normalized.length ? "..." : "");
  return { signature: FATAL_ERROR_EXIT_TOKEN, excerpt };
}
function itemPaths(item) {
  const paths = (item.related_files ?? []).map((f) => f?.path).filter((p) => typeof p === "string" && p.length > 0);
  return [...new Set(paths)];
}
function pathsCollide(a, b) {
  const x = a.replace(/\/+$/, "");
  const y = b.replace(/\/+$/, "");
  if (x === y) return true;
  return x.startsWith(`${y}/`) || y.startsWith(`${x}/`);
}
function selectSchedulable(candidates, takenPaths, headroom, mode = "concurrent") {
  const slugs = [];
  const deferred = [];
  const skipped = [];
  const refused = [];
  const taken = [...takenPaths];
  const seen = /* @__PURE__ */ new Set();
  for (const candidate of candidates) {
    if (seen.has(candidate.id)) continue;
    seen.add(candidate.id);
    const eligibility = mode === "serial" ? candidate.serial_safe : candidate.worker_safe;
    if (eligibility !== true) {
      refused.push({
        slug: candidate.id,
        reason: mode === "serial" && typeof candidate.serial_safety_reason === "string" ? candidate.serial_safety_reason : eligibility === false ? "the backlog reports this item is not worker-safe -- its prefix names the harness repo, or is unrecognised. A worker would be editing the code it is running. Work it in a normal session." : `dev_status.py ready reported no ${mode === "serial" ? "serial_safe" : "worker_safe"} field for this item, so eligibility is unknown and it is refused rather than assumed safe. Update the installed dev_status.py.`
      });
      continue;
    }
    if (slugs.length >= headroom) {
      skipped.push(candidate.id);
      continue;
    }
    const paths = itemPaths(candidate);
    let clashPath;
    let clashHolder;
    for (const p of paths) {
      const hit = taken.find((t) => pathsCollide(p, t.path));
      if (hit !== void 0) {
        clashPath = p;
        clashHolder = hit.holder;
        break;
      }
    }
    if (clashPath !== void 0) {
      deferred.push({
        slug: candidate.id,
        reason: `file overlap with ${clashHolder}: ${clashPath}`
      });
      continue;
    }
    slugs.push(candidate.id);
    taken.push(
      ...paths.map((p) => ({
        path: p,
        holder: `candidate ${candidate.id} (selected earlier this wave)`
      }))
    );
  }
  return { slugs, deferred, skipped, refused };
}
function pendingAmendWorkers(state) {
  return state.workers.filter((w) => w.pendingAmend !== void 0);
}
function activeWorkerCount(state) {
  return state.workers.filter((w) => w.lifecycle === "active").length;
}
function canSpawnNew(state) {
  if (state.mode === "serial") return state.workers.length === 0;
  return activeWorkerCount(state) < state.concurrency;
}
function openPaneCount(state) {
  return state.workers.length;
}
function openPaneSoftCap(concurrency) {
  return concurrency * OPEN_PANE_SOFT_CAP_MULTIPLIER;
}
function canOpenNewPane(state) {
  return openPaneCount(state) < openPaneSoftCap(state.concurrency);
}
function spawnBudget(state, readyCount) {
  if (state.mode === "serial") return state.workers.length === 0 && readyCount > 0 ? 1 : 0;
  const byConcurrency = Math.max(0, state.concurrency - activeWorkerCount(state));
  const byPaneCap = Math.max(0, openPaneSoftCap(state.concurrency) - openPaneCount(state));
  return Math.min(byConcurrency, byPaneCap, readyCount);
}

// pi/extensions/swarm-lib/swarm-herdr.ts
import { basename, dirname, join } from "node:path";
var AGENT_START_TIMEOUT_MS = 3e4;
function buildTabCreateArgv(cwd, label, opts) {
  const kind = opts?.kind ?? "copilot";
  const captureFile = opts?.captureFile;
  const envArgs = [];
  if (kind === "pi") {
    envArgs.push("--env", WORKER_UNATTENDED_ENV);
  }
  if (captureFile) {
    const varName = kind === "copilot" ? "COPILOT_SWARM_CAPTURE_FILE" : "PI_SWARM_CAPTURE_FILE";
    envArgs.push("--env", `${varName}=${captureFile}`);
  }
  return ["tab", "create", "--cwd", cwd, "--label", label, ...envArgs, "--no-focus"];
}
function buildTabCloseArgv(tabId) {
  return ["tab", "close", tabId];
}
function buildTabListArgv() {
  return ["tab", "list"];
}
function findTabByLabel(stdout, label) {
  try {
    const parsed = JSON.parse(stdout);
    const matches = (parsed.result?.tabs ?? []).filter(
      (t) => t.label === label && typeof t.tab_id === "string"
    );
    return matches.length === 1 ? matches[0].tab_id : void 0;
  } catch {
    return void 0;
  }
}
function tabPresence(stdout, tabId) {
  try {
    const parsed = JSON.parse(stdout);
    const tabs = parsed.result?.tabs;
    if (!Array.isArray(tabs)) return null;
    return tabs.some((tab) => tab.tab_id === tabId);
  } catch {
    return null;
  }
}
function parseTabCreate(stdout) {
  try {
    const parsed = JSON.parse(stdout);
    const paneId = parsed.result?.root_pane?.pane_id;
    const tabId = parsed.result?.tab?.tab_id;
    if (typeof paneId !== "string" || typeof tabId !== "string") return void 0;
    return { paneId, tabId };
  } catch {
    return void 0;
  }
}
function buildAgentStartArgv(agentId, paneId, model, opts) {
  const kind = opts?.kind ?? "copilot";
  const argv = [
    "agent",
    "start",
    agentId,
    "--kind",
    kind,
    "--pane",
    paneId,
    "--timeout",
    String(AGENT_START_TIMEOUT_MS)
  ];
  if (kind === "copilot") {
    const sessionArgs = [];
    if (opts?.resumeSessionId) {
      sessionArgs.push(`--resume=${opts.resumeSessionId}`);
    } else if (opts?.sessionId) {
      sessionArgs.push("--session-id", opts.sessionId);
    }
    if (opts?.allowAllTools ?? true) {
      sessionArgs.push("--allow-all-tools");
    }
    if (opts?.pluginDir) {
      sessionArgs.push("--plugin-dir", opts.pluginDir);
    }
    if (model) {
      sessionArgs.push("--model", model);
    }
    if (sessionArgs.length > 0) {
      argv.push("--", ...sessionArgs);
    }
    return argv;
  }
  if (model) {
    argv.push("--", "--model", model);
  }
  return argv;
}
var WORKER_UNATTENDED_ENV = "PI_AGENT_UNATTENDED=1";
var AMEND_INSTRUCTION = "STOP and re-read your backlog item before doing anything else: run `python3 ~/.claude/scripts/dev_status.py show <your slug>` and read the whole record fresh. Its context or next_steps have been corrected since you started, so any plan you formed from the earlier version may now be wrong. Reconcile what you have already done against the updated record, and say plainly what changes as a result before continuing.";
function buildAgentPromptArgv(agentId, prompt, opts = {}) {
  const argv = ["agent", "prompt", agentId, prompt];
  if (!opts.wait) return argv;
  argv.push("--wait");
  for (const status of opts.until ?? []) argv.push("--until", status);
  if (opts.timeoutMs !== void 0) argv.push("--timeout", String(opts.timeoutMs));
  return argv;
}
function reasonHeadline(reason) {
  return reason.split("\n", 1)[0] ?? reason;
}
function buildAgentSendKeysArgv(agentId, keys) {
  return ["agent", "send-keys", agentId, ...keys];
}
function buildAgentWaitArgv(agentId, until, timeoutMs) {
  return [
    "agent",
    "wait",
    agentId,
    ...until.flatMap((state) => ["--until", state]),
    "--timeout",
    String(timeoutMs)
  ];
}
function buildAgentGetArgv(agentId) {
  return ["agent", "get", agentId];
}
function buildAgentReadArgv(agentId, lines) {
  return ["agent", "read", agentId, "--source", "recent-unwrapped", "--lines", String(lines)];
}
function buildPaneCloseArgv(paneId) {
  return ["pane", "close", paneId];
}
function buildWorkerCloseArgv(worker) {
  return worker.tabId ? buildTabCloseArgv(worker.tabId) : buildPaneCloseArgv(worker.paneId);
}
function buildPaneReadArgv(paneId, lines) {
  return ["pane", "read", paneId, "--source", "recent-unwrapped", "--lines", String(lines)];
}
function buildAgentListArgv() {
  return ["agent", "list"];
}
function parseAgentList(stdout) {
  try {
    const parsed = JSON.parse(stdout);
    const agents = parsed?.result?.agents;
    if (!Array.isArray(agents)) return null;
    return agents.flatMap(
      (a) => typeof a?.name === "string" ? [{ id: a.name, status: typeof a.agent_status === "string" ? a.agent_status : void 0 }] : []
    );
  } catch {
    return null;
  }
}
function parseHerdrJson(text) {
  try {
    return JSON.parse(text);
  } catch {
    return null;
  }
}
function parseAgentSession(stdout) {
  try {
    const parsed = parseHerdrJson(stdout);
    const val = parsed?.result?.agent?.agent_session?.value;
    return typeof val === "string" ? val : void 0;
  } catch {
    return void 0;
  }
}
function classifyWaitResult(exitCode, stdout, stderr) {
  if (exitCode === 0) {
    const status = parseHerdrJson(stdout)?.result?.agent?.agent_status;
    if (status === "blocked") return "blocked";
    if (status === "idle" || status === "done") return "finished";
    return "error";
  }
  const code = parseHerdrJson(stderr)?.error?.code;
  return code === "timeout" ? "timed_out" : "error";
}
function classifyResyncGet(exitCode, stdout, stderr) {
  if (exitCode !== 0) {
    const code = parseHerdrJson(stderr)?.error?.code;
    return code === "agent_not_found" ? { action: "drop" } : { action: "keep" };
  }
  const status = parseHerdrJson(stdout)?.result?.agent?.agent_status;
  if (status === "working" || status === "idle" || status === "done") return { action: "unpark" };
  return { action: "keep" };
}
function classifyTimeoutProbe(probe, elapsedMs, deadlineMs) {
  const overBudget = elapsedMs !== null && elapsedMs >= deadlineMs;
  const inconclusive = () => overBudget ? { disposition: "event", kind: "timed_out", livenessConfirmed: false } : { disposition: "rearm" };
  if (probe.abandoned) return inconclusive();
  if (probe.code !== 0) {
    const code = parseHerdrJson(probe.stderr)?.error?.code;
    if (code === "agent_not_found") return { disposition: "event", kind: "error" };
    return inconclusive();
  }
  const status = parseHerdrJson(probe.stdout)?.result?.agent?.agent_status;
  if (status === "blocked") return { disposition: "event", kind: "blocked" };
  if (status === "idle" || status === "done") return { disposition: "event", kind: "finished" };
  if (status === void 0) return inconclusive();
  return overBudget ? { disposition: "event", kind: "timed_out", livenessConfirmed: true } : { disposition: "rearm" };
}
function workerWorktreePath(cwd, slug) {
  if (!cwd) return null;
  return join(dirname(cwd), `${basename(cwd)}-${slug}`);
}
function deadlineStopDetail(worker, deadlineMs, opts) {
  const minutes = Math.round(deadlineMs / 6e4);
  const lines = opts.livenessConfirmed ? [
    `worker budget of ${minutes} min of working time elapsed while the agent still reported working -- stopped deliberately.`
  ] : [
    `worker budget of ${minutes} min of working time elapsed, and its liveness could NOT be verified: ${opts.probeDetail ?? "the probe gave no usable answer"}.`,
    "It may have been working, or may have died earlier -- this stop is on the budget, not on evidence about the worker."
  ];
  lines.push(
    "",
    `The item is very likely still in-progress with a live claim: python3 ~/.claude/scripts/dev_status.py show ${worker.slug}`
  );
  const worktree = workerWorktreePath(worker.cwd, worker.slug);
  if (worktree) {
    lines.push(
      `Its worktree survives on disk. Worker cwd was ${worker.cwd}; by the <repo>-<slug> convention that makes the worktree ${worktree} (derived from the cwd, not verified).`,
      `Recover with: git -C ${worker.cwd} worktree remove --force ${worktree}, then reset the item to open to clear the claim.`
    );
  } else {
    lines.push(
      "This worker predates cwd tracking, so its worktree path cannot be named here -- find it with git worktree list."
    );
  }
  return lines.join("\n");
}
function paneIdentityMismatch(getExitCode, getStdout, expectedPaneId) {
  if (getExitCode !== 0) return true;
  const reportedPaneId = parseHerdrJson(getStdout)?.result?.agent?.pane_id;
  return reportedPaneId !== expectedPaneId;
}
function waitResultDetail(stdout, stderr) {
  const err = parseHerdrJson(stderr)?.error;
  if (err) return `${err.code ?? "unknown"}: ${err.message ?? stderr.trim()}`;
  return stdout.trim() || stderr.trim() || "(no output)";
}
var AMEND_STEERING_WINDOW_MS = 5e3;
var AMEND_ACK_TIMEOUT_MS = 1e4;
var AMEND_ACK_MAX_CHECKS = 3;
var AMEND_HOLD_MAX_MS = AMEND_ACK_TIMEOUT_MS * AMEND_ACK_MAX_CHECKS;
function parseAgentStateSeq(stdout) {
  const value = parseHerdrJson(stdout)?.result?.agent?.state_change_seq;
  return typeof value === "number" ? value : void 0;
}
function classifyAmendAck(exitCode, stdout, stderr, lastObservedSeq) {
  if (exitCode !== 0) {
    const code = parseHerdrJson(stderr)?.error?.code;
    if (code === "agent_not_found") return "gone";
    return "inconclusive";
  }
  const status = parseHerdrJson(stdout)?.result?.agent?.agent_status;
  if (status === "working") return "turn_started";
  if (status === "blocked") return "parked";
  if (status !== "idle" && status !== "done") return "inconclusive";
  const seq = parseAgentStateSeq(stdout);
  if (seq === void 0 || lastObservedSeq === null) return "inconclusive";
  return seq > lastObservedSeq ? "changed_while_unarmed" : "settled_unchanged";
}
function amendHoldVerdict(opts) {
  if (opts.turnObserved) return "confirmed";
  const window = opts.steeringWindowMs ?? AMEND_STEERING_WINDOW_MS;
  return opts.runWorkedMs >= window ? "steered" : "unpicked";
}
function amendHoldMs(pending, now) {
  return Math.max(0, now - pending.requestedAtMs);
}
function amendHoldExpired(pending, now) {
  return pending.checks >= AMEND_ACK_MAX_CHECKS || amendHoldMs(pending, now) >= AMEND_HOLD_MAX_MS;
}
function amendAckPath(captureFile) {
  const dir = dirname(captureFile);
  const base = basename(captureFile);
  const replaced = base.replace("-capture-", "-amend-ack-");
  if (replaced !== base) return join(dir, replaced);
  return join(dir, `${base}.amend-ack.json`);
}
function outcomePath(captureFile) {
  const dir = dirname(captureFile);
  const base = basename(captureFile);
  const replaced = base.replace("-capture-", "-outcome-");
  if (replaced !== base) return join(dir, replaced);
  return join(dir, `${base}.outcome.json`);
}
function parseAmendAck(raw) {
  try {
    const parsed = JSON.parse(raw);
    if (typeof parsed !== "object" || parsed === null) return null;
    const t = parsed.t;
    if (typeof t !== "number" || !Number.isFinite(t)) return null;
    const sb = parsed.streamingBehavior;
    const streamingBehavior = sb === "steer" || sb === "followUp" || sb === null || typeof sb === "undefined" ? sb : void 0;
    return { t, streamingBehavior };
  } catch {
    return null;
  }
}
function amendAckConfirms(ack, requestedAtMs) {
  return ack.t >= requestedAtMs;
}
function sidecarUpgradesHold(kind, ack, requestedAtMs) {
  if (kind !== "pi" || ack === null) return false;
  return amendAckConfirms(ack, requestedAtMs);
}
function amendVerdictDetail(verdict, worker, holdMs) {
  const held = `${Math.round(holdMs / 1e3)}s`;
  switch (verdict) {
    case "confirmed":
      return `amend_confirmed: a new turn started after the amendment was submitted, so the correction was picked up (held ${held} to see it).`;
    case "steered":
      return `amend_steered: the worker kept working ${held} after the amendment, so pi most likely took it as a steering message inside the turn that has now finished. NOT positively confirmed -- if the keystroke never reached the agent, herdr offers nothing that can tell that apart from this. Check the pane before treating the item as corrected.`;
    case "unpicked":
      return `amend_unpicked: the run settled ${held} after the amendment and no new turn started, so the queued correction was very likely never read. The worker is being finished off WITHOUT its correction landing -- re-amend after restarting it, or pick the item up in a normal session. (${worker.agent} / ${worker.slug})`;
  }
}
function amendOutstandingNote(pending, now) {
  return `NOTE: an amendment was outstanding for this worker and was NOT confirmed picked up (submitted ${Math.round(amendHoldMs(pending, now) / 1e3)}s ago) -- do not report the item as having worked its corrected premises.`;
}
function parseAgentStatus(stdout) {
  const value = parseHerdrJson(stdout)?.result?.agent?.agent_status;
  return typeof value === "string" ? value : void 0;
}

// pi/extensions/swarm-lib/swarm-picker-copilot.ts
function classifyBlock(_rawPrompt) {
  return "needs_human";
}
function parsePicker(_content) {
  return { selectedIndex: null, options: [] };
}
function pickerLabels(_rawPrompt) {
  return [];
}
function noteResolveFailure(worker, answer, reason, now) {
  worker.lastResolveFailure = { answer, reason, at: now };
}
var copilotPickerAdapter = {
  classifyBlock,
  parsePicker,
  pickerLabels
};

// pi/extensions/swarm-lib/swarm-picker.ts
var OTHER_OPTION_LABEL = "Something else (type it)";
var MIN_PARTIAL_ANSWER = 3;
function containsAsWord(haystack, needle) {
  const isWordChar = (c) => c !== void 0 && /[a-z0-9]/.test(c);
  let from = 0;
  for (; ; ) {
    const at = haystack.indexOf(needle, from);
    if (at === -1) return false;
    if (!isWordChar(haystack[at - 1]) && !isWordChar(haystack[at + needle.length])) {
      return true;
    }
    from = at + 1;
  }
}
function matchOption(answer, options) {
  const candidates = options.filter(
    (o) => o.label.toLowerCase() !== OTHER_OPTION_LABEL.toLowerCase()
  );
  const needle = answer.trim().toLowerCase();
  if (!needle) return null;
  const exact = candidates.filter((o) => o.label.toLowerCase() === needle);
  if (exact.length === 1) return exact[0];
  if (needle.length < MIN_PARTIAL_ANSWER) return null;
  const partial = candidates.filter((o) => containsAsWord(o.label.toLowerCase(), needle));
  if (partial.length === 1) return partial[0];
  const strippedNeedle = needle.replace(/\s+/g, "");
  const stripped = candidates.filter(
    (o) => o.label.toLowerCase().replace(/\s+/g, "").includes(strippedNeedle)
  );
  if (stripped.length === 1) return stripped[0];
  return null;
}
function navigationKeys(fromIndex, toIndex) {
  const steps = toIndex - fromIndex;
  const key = steps > 0 ? "down" : "up";
  return [...Array(Math.abs(steps)).fill(key), "enter"];
}

// pi/extensions/swarm-lib/swarm-tool-context.ts
function herdrStateDir() {
  return process.env.COPILOT_SWARM_STATE_DIR ?? join2(homedir(), ".copilot", "state");
}
function devStatusPath() {
  return process.env.COPILOT_SWARM_DEV_STATUS_PATH ?? join2(homedir(), ".claude", "scripts", "dev_status.py");
}
function copilotPluginDir() {
  return process.env.COPILOT_SWARM_PLUGIN_DIR ?? join2(homedir(), "Workspace", "agent-toolkit", "copilot", "extensions", "swarm");
}
var DEFAULT_CONCURRENCY = 3;
var DEFAULT_WAIT_TIMEOUT_MS = 30 * 60 * 1e3;
var DEFAULT_WORKER_DEADLINE_MS = 4 * 60 * 60 * 1e3;
var DEFAULT_RELAY_STALL_MS = 30 * 60 * 1e3;
var PROBE_TIMEOUT_MS = 15e3;
var PROMPT_ACK_TIMEOUT_MS = 1e4;
var PROMPT_ACK_PROCESS_TIMEOUT_MS = 15e3;
var PROMPT_ACK_STATES = ["working", "idle", "done", "blocked"];
var RESOLVE_VERIFY_TIMEOUT_MS = 5e3;
var BLOCKED_READ_LINES = 500;
var BLOCKED_READ_LINES_RETRY = 2e3;
var PANE_CAPTURE_CHARS = 4e3;
var PANE_CAPTURE_LINES = 200;
var MAX_RECOVERY_ATTEMPTS = 2;
function statePath(runId, stateDir = herdrStateDir()) {
  return join2(stateDir, `swarm-${runId}.json`);
}
function capturePath(runId, slug, stateDir = herdrStateDir()) {
  const safeRun = runId.replace(/[^A-Za-z0-9._-]/g, "_");
  const safeSlug = (slug.split("/").pop() ?? "").replace(/[^A-Za-z0-9._-]/g, "_");
  return join2(stateDir, `swarm-${safeRun}-capture-${safeSlug}.json`);
}
function outcomePathOf(runId, slug, stateDir = herdrStateDir()) {
  return outcomePath(capturePath(runId, slug, stateDir));
}
function readFatalSidecar(path) {
  let raw;
  try {
    raw = readFileSync(path, "utf8");
  } catch {
    return null;
  }
  return parseFatalSidecar(raw);
}
function fatalSidecarDetail(sidecar) {
  return `fatal_error_sidecar: the worker recorded its own fatal settle before exiting (stopReason=${sidecar.stopReason}, model=${sidecar.model}). This is the worker's own statement, not an inference from pane text. Verify the item's actual state before treating it as complete.`;
}
function readAmendAck(path) {
  let raw;
  try {
    raw = readFileSync(path, "utf8");
  } catch {
    return null;
  }
  return parseAmendAck(raw);
}
function unlinkAmendAck(path) {
  try {
    rmSync(path, { force: true });
  } catch {
  }
}
function readCaptureOffers(runId, slug, stateDir = herdrStateDir()) {
  const path = capturePath(runId, slug, stateDir);
  let raw;
  try {
    raw = readFileSync(path, "utf8");
  } catch {
    return [];
  }
  try {
    rmSync(path, { force: true });
  } catch {
  }
  try {
    const parsed = JSON.parse(raw);
    const offers = parsed && typeof parsed === "object" && "offers" in parsed ? parsed.offers : null;
    if (!Array.isArray(offers)) return [];
    return offers.flatMap((o) => {
      if (!o || typeof o !== "object") return [];
      const rec = o;
      const kind = typeof rec.kind === "string" ? rec.kind : "";
      const id = typeof rec.id === "string" ? rec.id : "";
      const summary = typeof rec.summary === "string" ? rec.summary : "";
      return kind && id ? [{ kind, id, summary }] : [];
    });
  } catch {
    return [];
  }
}
function renderOutcome(outcome) {
  const parts = [`outcome=${outcome.processResult}/${outcome.evidence}`];
  if (outcome.backlogStatus !== void 0) {
    parts.push(`backlog=${outcome.backlogStatus}`);
  } else if (outcome.backlogRead !== "ok") {
    parts.push(`backlog=${outcome.backlogRead}`);
  }
  return `[${parts.join(" ")}]`;
}
function renderCaptureOffers(offers) {
  if (offers.length === 0) return "";
  return "\n  Queued capture offers from this worker -- do NOT ask about them now; fold them into your single end-of-run digest walk: " + offers.map((c) => `[${c.kind}] ${c.id} -- ${c.summary}`).join("; ");
}
function loadState(runId, stateDir = herdrStateDir()) {
  const path = statePath(runId, stateDir);
  if (!existsSync(path)) return null;
  try {
    const parsed = JSON.parse(readFileSync(path, "utf8"));
    if (parsed && typeof parsed === "object" && "workers" in parsed) {
      return parsed;
    }
    return null;
  } catch {
    return null;
  }
}
function saveState(state, stateDir = herdrStateDir()) {
  mkdirSync(stateDir, { recursive: true });
  writeFileSync(statePath(state.runId, stateDir), JSON.stringify(state, null, 2));
}
function reconcileState(state, liveAgentIds) {
  const live = new Set(liveAgentIds);
  const dropped = [];
  const kept = [];
  for (const worker of state.workers) {
    if (live.has(worker.agent)) {
      kept.push(worker);
    } else {
      dropped.push(worker);
    }
  }
  return { state: { ...state, workers: kept }, dropped };
}
function buildReadyArgv(prefix) {
  const argv = ["python3", devStatusPath(), "ready"];
  return prefix ? [...argv, "--prefix", prefix] : argv;
}
function buildShowArgv(slug) {
  return ["python3", devStatusPath(), "show", slug];
}
function buildRenderArgv() {
  return ["python3", devStatusPath(), "render"];
}
function formatDuration(ms) {
  const totalMinutes = Math.max(0, Math.round(ms / 6e4));
  const hours = Math.floor(totalMinutes / 60);
  const minutes = totalMinutes % 60;
  return hours > 0 ? `${hours}h${String(minutes).padStart(2, "0")}m` : `${minutes}m`;
}
function looksTruncated(content, requestedLines) {
  return content.split("\n").length >= requestedLines;
}
function elapsedWorkingMs(worker, now) {
  const open = worker.workingSinceMs === void 0 ? null : Math.max(0, now - worker.workingSinceMs);
  if (open === null && worker.accumulatedWorkingMs === void 0) return null;
  return (worker.accumulatedWorkingMs ?? 0) + (open ?? 0);
}
function foldWorkingSegment(worker, now) {
  if (worker.workingSinceMs === void 0) return;
  worker.accumulatedWorkingMs = (worker.accumulatedWorkingMs ?? 0) + Math.max(0, now - worker.workingSinceMs);
  worker.workingSinceMs = void 0;
}
var defaultExec = async (cmd, args, opts) => {
  return new Promise((resolve) => {
    let proc;
    try {
      proc = spawn(cmd, args, { signal: opts?.signal });
    } catch (e) {
      resolve({ code: 1, stdout: "", stderr: String(e) });
      return;
    }
    let stdout = "";
    let stderr = "";
    proc.stdout?.on("data", (chunk) => {
      stdout += chunk.toString();
    });
    proc.stderr?.on("data", (chunk) => {
      stderr += chunk.toString();
    });
    let timer;
    let timedOut = false;
    if (opts?.timeout) {
      timer = setTimeout(() => {
        timedOut = true;
        proc.kill();
      }, opts.timeout);
    }
    proc.on("error", (err) => {
      if (timer) clearTimeout(timer);
      resolve({ code: 1, stdout, stderr: `${stderr}
${String(err)}` });
    });
    proc.on("close", (code) => {
      if (timer) clearTimeout(timer);
      if (timedOut) {
        resolve({ code: 124, stdout, stderr: `${stderr}
<timed out after ${opts?.timeout}ms>` });
        return;
      }
      resolve({ code: code ?? 0, stdout, stderr });
    });
  });
};
var UUID_REGEX = /^[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/i;
function isValidUuid(id) {
  return UUID_REGEX.test(id);
}
var SwarmToolContext = class {
  constructor(exec = defaultExec, picker = copilotPickerAdapter, options = {}) {
    this.picker = picker;
    this.options = options;
    this.exec = exec;
  }
  activeRuns = /* @__PURE__ */ new Map();
  runtimes = /* @__PURE__ */ new Map();
  exec;
  get kind() {
    return this.options.kind ?? "copilot";
  }
  get stateDir() {
    return (this.options.stateDir ?? herdrStateDir)();
  }
  workerPrompt(slug) {
    return (this.options.workerPrompt ?? ((id) => `/backlog-item --auto ${id}`))(slug);
  }
  defaultPluginDir() {
    return (this.options.defaultPluginDir ?? copilotPluginDir)();
  }
  async herdr(argv, signal, timeout) {
    return this.exec("herdr", argv, { signal, timeout });
  }
  getRuntime(runId) {
    let rt = this.runtimes.get(runId);
    if (!rt) {
      rt = {
        runId,
        inFlight: /* @__PURE__ */ new Set(),
        pendingEvents: [],
        waiters: [],
        spawnChain: Promise.resolve(),
        timeoutMs: DEFAULT_WAIT_TIMEOUT_MS,
        deadlineMs: DEFAULT_WORKER_DEADLINE_MS,
        stallMs: DEFAULT_RELAY_STALL_MS
      };
      this.runtimes.set(runId, rt);
    }
    return rt;
  }
  async withSpawnLock(runId, fn) {
    const rt = this.getRuntime(runId);
    const previous = rt.spawnChain;
    let release;
    rt.spawnChain = new Promise((resolve) => {
      release = resolve;
    });
    await previous;
    try {
      return await fn();
    } finally {
      release();
    }
  }
  waitForEvent(rt, signal) {
    return new Promise((resolve) => {
      let settled = false;
      const cleanup = () => {
        const i = rt.waiters.indexOf(wake);
        if (i !== -1) rt.waiters.splice(i, 1);
        signal?.removeEventListener("abort", onAbort);
      };
      const wake = () => {
        if (settled) return;
        settled = true;
        cleanup();
        resolve(true);
      };
      const onAbort = () => {
        if (settled) return;
        settled = true;
        cleanup();
        resolve(false);
      };
      if (signal?.aborted) {
        settled = true;
        resolve(false);
        return;
      }
      rt.waiters.push(wake);
      signal?.addEventListener("abort", onAbort, { once: true });
    });
  }
  wakeWaiters(rt) {
    const waiting = rt.waiters.splice(0);
    for (const wake of waiting) wake();
  }
  async closeWorker(worker, signal) {
    try {
      const result = await this.herdr(buildWorkerCloseArgv(worker), signal);
      return result.code === 0;
    } catch {
      return false;
    }
  }
  teardownRecovery(worker) {
    const close = worker.tabId ? `herdr tab close ${worker.tabId}` : `herdr pane close ${worker.paneId}`;
    return `herdr did not confirm worker teardown. Run \`${close}\`, then poll or restart this run to reconcile it`;
  }
  /**
   * The I/O half of tearing a worker down: read its queued capture offers,
   * close it in herdr, delete its capture file. Does NOT touch
   * state.workers or persist -- callers that process several workers at
   * once (swarmPoll's event loop) run this in parallel across workers, then
   * remove them from state.workers in one batch afterward, since concurrent
   * per-worker filter-and-reassign calls on the same array would race.
   */
  async harvestWorkerIO(state, worker, signal) {
    return (await this.harvestWorkerIOWithStatus(state, worker, signal)).offers;
  }
  /**
   * The I/O half of tearing a worker down: read its queued capture offers and
   * its death certificate, close it in herdr, then delete both sidecar files.
   * The certificate is returned so the caller can still use the value after the
   * file is gone, and is read BEFORE `closeWorker`.
   *
   * Does NOT touch state.workers or persist -- callers that process several
   * workers at once (swarmPoll's event loop) run this in parallel across
   * workers, then remove them from state.workers in one batch afterward, since
   * concurrent per-worker filter-and-reassign calls on the same array would
   * race.
   *
   * The read is gated on the host kind: a Copilot worker has no pi to run the
   * writer, so probing for a certificate it could never have would let an
   * outcome claim an absence that was never tested.
   */
  async harvestWorkerIOWithStatus(state, worker, signal) {
    const offers = readCaptureOffers(state.runId, worker.slug, this.stateDir);
    const sidecar = this.kind === "pi" ? this.readSidecar(state, worker) : null;
    let closed = await this.closeWorker(worker, signal);
    if (!closed && (state.mode ?? "concurrent") === "serial") {
      try {
        const [agent, tabs] = await Promise.all([
          this.herdr(buildAgentGetArgv(worker.agent), signal),
          this.herdr(buildTabListArgv(), signal)
        ]);
        const agentAbsent = classifyResyncGet(agent.code, agent.stdout, agent.stderr).action === "drop";
        const tabAbsent = worker.tabId ? tabs.code === 0 && tabPresence(tabs.stdout, worker.tabId) === false : false;
        closed = agentAbsent && tabAbsent;
      } catch {
        closed = false;
      }
    }
    try {
      rmSync(capturePath(state.runId, worker.slug, this.stateDir), { force: true });
    } catch {
    }
    try {
      rmSync(outcomePathOf(state.runId, worker.slug, this.stateDir), { force: true });
    } catch {
    }
    this.unlinkWorkerAmendAck(state.runId, worker.slug);
    return { offers, closed, sidecar };
  }
  /** The certificate for one worker, or `null` when there is nothing to trust. */
  readSidecar(state, worker) {
    return this.sidecarForRun(state.runId, worker.slug);
  }
  /**
   * Merge the classification made upstream (which saw the pane) with the
   * certificate the harvest returned (which the upstream site may not have been
   * able to read yet), then apply the pessimism rule.
   *
   * A certificate present at harvest but absent from the upstream draft upgrades
   * the result -- the worker stated a death, and that outranks any inference.
   * The reverse never happens: an upstream `fatal_error` is never softened, so
   * this cannot turn a pessimistic classification optimistic and no ordering
   * between the two sites can produce a contradiction.
   */
  draftWithSidecar(draft, kind, sidecar) {
    const terminal = kind === "finished" || kind === "error" || kind === "timed_out";
    const fallback = classifyOutcomeDraft(terminal ? kind : "error", {
      sidecar,
      sidecarProbed: this.kind === "pi",
      paneMatch: null,
      paneRead: false
    });
    if (!draft) return fallback;
    if (sidecar && draft.evidence !== "fatal_sidecar") {
      return { ...draft, processResult: "fatal_error", evidence: "fatal_sidecar" };
    }
    return draft;
  }
  /**
   * The certificate by run/slug, so `settleWait` can consult it without holding
   * a state reference. Returns `null` WITHOUT touching the filesystem for a
   * Copilot host: a worker with no pi to run the writer has no certificate to
   * be absent, and recording an absence that was never tested would be exactly
   * the kind of claim this item is here to stop making.
   */
  sidecarForRun(runId, slug) {
    if (this.kind !== "pi") return null;
    return readFatalSidecar(outcomePathOf(runId, slug, this.stateDir));
  }
  /**
   * Turn a classification into a persisted ledger row: the ONLY place a
   * reconciled outcome is written, so every teardown path -- including the three
   * that never ran `dev_status.py show` at all before this -- produces a row
   * with a real backlog column instead of a blank one.
   *
   * The independent read is part of assembly rather than a later pass because
   * the ledger append has to land before the removal filter, so the existing
   * single `persist(state)` writes it. A re-reconciliation of the same worker
   * (serial `teardown_ambiguous`, met again on a later poll) reuses the
   * ORIGINAL `decidedAtMs`: re-stamping it would make every pass a distinct
   * dedup key and grow the ledger without bound while the queue is stuck.
   */
  async recordOutcome(state, worker, draft, captureCount, signal, event) {
    const backlog = await this.readBacklogStatus(worker.slug, signal);
    const prior = worker.outcome ?? priorOutcome(state, worker.agent);
    const verification = [];
    if (isSuspiciousFinish(backlog.status, captureCount)) {
      verification.push(
        `dev_status.py still shows status ${JSON.stringify(backlog.status)} and zero captures were queued during this run -- verify the item's actual state before treating this as complete.`
      );
    }
    if (backlog.read !== "ok") {
      verification.push(
        backlog.read === "unparsed" ? "dev_status.py show answered, but its output did not parse; backlog state unknown." : "dev_status.py show did not answer; backlog state unknown."
      );
    }
    if (event && verification.length > 0) {
      event.detail = `${event.detail ? `${event.detail} ` : ""}${verification.join(" ")}`;
    }
    const notes = [draft.note, ...verification].filter((n) => Boolean(n));
    const outcome = {
      runId: state.runId,
      agent: worker.agent,
      slug: worker.slug,
      kind: this.kind,
      processResult: draft.processResult,
      evidence: draft.evidence,
      sidecarProbed: draft.sidecarProbed,
      ...draft.herdrStatus !== void 0 ? { herdrStatus: draft.herdrStatus } : {},
      ...backlog.status !== void 0 ? { backlogStatus: backlog.status } : {},
      backlogRead: backlog.read,
      decidedAtMs: prior?.decidedAtMs ?? Date.now(),
      ...notes.length > 0 ? { note: notes.join(" ") } : {}
    };
    worker.outcome = outcome;
    appendOutcome(state, outcome);
    return outcome;
  }
  /**
   * The orchestrator's own read of the item's backlog status, independent of
   * anything the worker claims.
   *
   * Three results, because the code this replaces collapsed all three into
   * silence: a nonzero exit, output that will not parse, and a thrown exec call
   * are different facts, and "the subprocess failed" must not read as "the item
   * has no status".
   */
  async readBacklogStatus(slug, signal) {
    let result;
    try {
      result = await this.exec("python3", buildShowArgv(slug).slice(1), {
        signal,
        timeout: PROBE_TIMEOUT_MS
      });
    } catch {
      return { read: "failed" };
    }
    if (result.code !== 0) return { read: "failed" };
    const shown = parseShownItem(result.stdout);
    if (shown === null) return { read: "unparsed" };
    const status = typeof shown.status === "string" ? shown.status : void 0;
    return { status, read: status === void 0 ? "unparsed" : "ok" };
  }
  async teardownAndHarvestWorker(state, worker, signal) {
    const teardown = await this.harvestWorkerIOWithStatus(state, worker, signal);
    if ((state.mode ?? "concurrent") === "serial" && !teardown.closed) {
      worker.lifecycle = "teardown_ambiguous";
      worker.terminalOutcome = "error";
      worker.terminalCaptures = teardown.offers;
      worker.teardownDetail = this.teardownRecovery(worker);
    } else {
      state.workers = state.workers.filter((w) => w.agent !== worker.agent);
    }
    await this.recordOutcome(
      state,
      worker,
      {
        ...classifyOutcomeDraft("error", {
          sidecar: teardown.sidecar,
          sidecarProbed: this.kind === "pi",
          paneMatch: null,
          paneRead: false
        }),
        note: "torn down without a settled outcome -- the worker was closed by an explicit teardown (a relay that could not be answered), not by anything it reported."
      },
      teardown.offers.length,
      signal
    );
    this.persist(state);
    return teardown.offers;
  }
  async pruneStaleWorkers(state) {
    let listResult;
    for (let attempt = 0; attempt < 2; attempt++) {
      try {
        listResult = await this.herdr(buildAgentListArgv());
      } catch {
        listResult = void 0;
      }
      if (listResult && listResult.code === 0 && parseAgentList(listResult.stdout) !== null) break;
    }
    const entries = listResult && listResult.code === 0 ? parseAgentList(listResult.stdout) : null;
    if (entries === null) return [];
    const stale = staleWorkerRecords(state, entries, Date.now());
    if (stale.length === 0) return [];
    const results = await Promise.all(
      stale.map(async (w) => {
        const entry = entries.find((e) => e.id === w.agent);
        const status = entry?.status;
        const teardown = await this.harvestWorkerIOWithStatus(state, w);
        const offers = [...w.terminalCaptures ?? [], ...teardown.offers];
        const confirmedGone = entry === void 0 || teardown.closed;
        if ((state.mode ?? "concurrent") === "serial" && !confirmedGone) {
          w.lifecycle = "teardown_ambiguous";
          w.teardownDetail = this.teardownRecovery(w);
        }
        const pruneDraft = classifyOutcomeDraft(entry === void 0 ? "error" : "finished", {
          sidecar: teardown.sidecar,
          sidecarProbed: this.kind === "pi",
          paneMatch: null,
          paneRead: false,
          ...status === void 0 ? {} : { herdrStatus: status }
        });
        await this.recordOutcome(state, w, pruneDraft, offers.length);
        const line = `${w.agent} (${w.slug}): stale worker record cleared -- herdr reports its agent ${status ?? "gone"} (finished or dead), but its finish was never reported through swarm_poll; ` + // The sentence used to be unconditional, which was honest only because
        // nothing on this path could do better. A certificate read here IS an
        // observation, so claiming inference over it would understate what is
        // known -- and the digest line is what a human reads.
        (pruneDraft.evidence === "fatal_sidecar" ? "outcome observed from the worker's own death certificate" : "outcome inferred, not observed") + `. Verify the item's state before treating it as complete.${// This path reaches a close without ever passing a settle, so an
        // outstanding amendment dies here. Closing anyway is correct -- a
        // refusal would strand the serial queue on a worker herdr already
        // calls gone -- but the hold being discarded has to be said.
        w.pendingAmend ? ` ${amendOutstandingNote(w.pendingAmend, Date.now())}` : ""}${renderCaptureOffers(offers)}`;
        return { worker: w, confirmedGone, line };
      })
    );
    const removed = new Set(
      results.filter(({ confirmedGone }) => confirmedGone || (state.mode ?? "concurrent") !== "serial").map(({ worker }) => worker.agent)
    );
    state.workers = state.workers.filter((w) => !removed.has(w.agent));
    const rt = this.runtimes.get(state.runId);
    if (rt) {
      for (const agent of removed) rt.inFlight.delete(agent);
      rt.pendingEvents = rt.pendingEvents.filter((e) => !removed.has(e.agent));
      this.wakeWaiters(rt);
    }
    this.persist(state);
    return results.map(
      ({ worker, confirmedGone, line }) => (state.mode ?? "concurrent") === "serial" && !confirmedGone ? `${worker.agent} (${worker.slug}): teardown remains ambiguous -- ${worker.teardownDetail}; the serial queue is paused.` : line
    );
  }
  /**
   * Finds and closes the tab a `tab create` left behind when its response
   * exited 0 but didn't parse -- ported from pi's `recoverTabByLabel`.
   *
   * `findTabByLabel` expects a `tab list` response shape (`result.tabs`),
   * not a `tab create` response (`result.tab`/`result.root_pane`), so this
   * must issue its own fresh `tab list` call rather than reusing the create
   * call's stdout.
   */
  async recoverTabByLabel(label) {
    try {
      const listing = await this.herdr(buildTabListArgv());
      if (listing.code !== 0) return void 0;
      const tabId = findTabByLabel(listing.stdout, label);
      if (!tabId) return void 0;
      const closed = await this.herdr(buildTabCloseArgv(tabId));
      return closed.code === 0 ? tabId : void 0;
    } catch {
      return void 0;
    }
  }
  async failWithTab(slug, paneId, tabId, reason) {
    let capture;
    try {
      const read = await this.herdr(buildPaneReadArgv(paneId, PANE_CAPTURE_LINES));
      capture = read.code === 0 ? read.stdout.slice(-PANE_CAPTURE_CHARS) : `<pane capture failed: ${(read.stderr || read.stdout).slice(0, 200)}>`;
    } catch (e) {
      capture = `<pane capture threw: ${String(e)}>`;
    }
    try {
      await this.herdr(buildTabCloseArgv(tabId));
    } catch {
    }
    return { slug, failed: { slug, reason: `${reason}
--- pane ${paneId} ---
${capture}` } };
  }
  async spawnInto(paneId, tabId, agentId, slug, paths, model, pluginDir) {
    let sessionId = this.kind === "copilot" ? randomUUID() : void 0;
    const startResult = await this.herdr(
      buildAgentStartArgv(agentId, paneId, model, {
        kind: this.kind,
        sessionId,
        ...this.kind === "copilot" ? { allowAllTools: true, pluginDir: pluginDir ?? this.defaultPluginDir() } : {}
      })
    );
    if (startResult.code !== 0) {
      return this.failWithTab(
        slug,
        paneId,
        tabId,
        `agent_not_ready: ${startResult.stderr || startResult.stdout}`
      );
    }
    if (sessionId)
      try {
        const getResult = await this.herdr(buildAgentGetArgv(agentId));
        if (getResult.code === 0) {
          const sessionVal = parseAgentSession(getResult.stdout);
          if (sessionVal && sessionVal !== sessionId) {
            process.stderr.write(
              `[swarm] ${agentId}: requested session-id ${sessionId} but herdr agent get reports ${sessionVal} -- using the confirmed value for crash recovery
`
            );
            sessionId = sessionVal;
          }
        }
      } catch {
      }
    const promptResult = await this.herdr(
      buildAgentPromptArgv(agentId, this.workerPrompt(slug), {
        wait: true,
        until: PROMPT_ACK_STATES,
        timeoutMs: PROMPT_ACK_TIMEOUT_MS
      }),
      void 0,
      PROMPT_ACK_PROCESS_TIMEOUT_MS
    );
    if (promptResult.code !== 0) {
      return this.failWithTab(
        slug,
        paneId,
        tabId,
        `agent_prompt_stalled: ${promptResult.stderr || promptResult.stdout}`
      );
    }
    return {
      worker: {
        agent: agentId,
        slug,
        paneId,
        tabId,
        paths,
        cwd: process.cwd(),
        workingSinceMs: Date.now(),
        model,
        lifecycle: "active",
        ...sessionId ? { copilotSessionId: sessionId, recoveryAttempts: 0 } : {}
      }
    };
  }
  async attemptCrashRecovery(state, worker, pluginDir) {
    const attempts = worker.recoveryAttempts ?? 0;
    if (this.kind !== "copilot" || attempts >= MAX_RECOVERY_ATTEMPTS) return false;
    if (!worker.copilotSessionId || !isValidUuid(worker.copilotSessionId)) return false;
    const cwd = worker.cwd ?? process.cwd();
    const label = worker.slug.replace(/[^A-Za-z0-9._-]/g, "-").slice(0, 32);
    let tabCreated = await this.herdr(buildTabCreateArgv(cwd, label, { kind: "copilot" }));
    let parsedTab = parseTabCreate(tabCreated.stdout);
    if (!parsedTab && tabCreated.code === 0) {
      await this.recoverTabByLabel(label);
      return false;
    }
    if (!parsedTab || !parsedTab.paneId) {
      return false;
    }
    if (worker.tabId) {
      try {
        await this.herdr(buildTabCloseArgv(worker.tabId));
      } catch {
      }
    }
    const startResult = await this.herdr(
      buildAgentStartArgv(worker.agent, parsedTab.paneId, worker.model, {
        kind: "copilot",
        resumeSessionId: worker.copilotSessionId,
        allowAllTools: true,
        pluginDir: pluginDir ?? this.defaultPluginDir()
      })
    );
    if (startResult.code !== 0) {
      try {
        await this.herdr(buildTabCloseArgv(parsedTab.tabId));
      } catch {
      }
      return false;
    }
    const promptResult = await this.herdr(
      buildAgentPromptArgv(
        worker.agent,
        "Continue working on this backlog item where you left off.",
        {
          wait: true,
          until: PROMPT_ACK_STATES,
          timeoutMs: PROMPT_ACK_TIMEOUT_MS
        }
      ),
      void 0,
      PROMPT_ACK_PROCESS_TIMEOUT_MS
    );
    if (promptResult.code !== 0) {
      try {
        await this.herdr(buildTabCloseArgv(parsedTab.tabId));
      } catch {
      }
      return false;
    }
    worker.paneId = parsedTab.paneId;
    worker.tabId = parsedTab.tabId;
    worker.recoveryAttempts = attempts + 1;
    worker.workingSinceMs = Date.now();
    this.persist(state);
    return true;
  }
  async getOrInitState(runId, concurrency, prefix, pluginDir, requestedMode) {
    const cached = this.activeRuns.get(runId);
    if (cached) {
      const recordedMode2 = cached.mode ?? "concurrent";
      if (requestedMode !== void 0 && requestedMode !== recordedMode2) {
        throw new Error(
          `run ${runId} is persisted in ${recordedMode2} mode; refusing requested ${requestedMode} mode`
        );
      }
      return cached;
    }
    const loaded = loadState(runId, this.stateDir);
    if (!loaded) {
      const mode = requestedMode ?? "concurrent";
      const fresh = {
        runId,
        concurrency: mode === "serial" ? 1 : concurrency,
        mode,
        nextCounter: 0,
        workers: [],
        ...prefix !== void 0 ? { prefix } : {},
        ...pluginDir !== void 0 ? { pluginDir } : {}
      };
      this.activeRuns.set(runId, fresh);
      return fresh;
    }
    const recordedMode = loaded.mode ?? "concurrent";
    if (requestedMode !== void 0 && requestedMode !== recordedMode) {
      throw new Error(
        `run ${runId} is persisted in ${recordedMode} mode; refusing requested ${requestedMode} mode`
      );
    }
    const listResult = await this.herdr(buildAgentListArgv());
    const entries = listResult.code === 0 ? parseAgentList(listResult.stdout) : null;
    const liveIds = (entries ?? []).map((e) => e.id);
    if (this.kind === "copilot" && entries !== null) {
      const missing = loaded.workers.filter((w) => !liveIds.includes(w.agent));
      const recoveries = await Promise.all(
        missing.map(async (w) => ({
          agent: w.agent,
          recovered: await this.attemptCrashRecovery(loaded, w, loaded.pluginDir)
        }))
      );
      for (const { agent, recovered } of recoveries) {
        if (recovered) liveIds.push(agent);
      }
    }
    const { state: reconciled, dropped } = entries === null || recordedMode === "serial" ? { state: loaded, dropped: [] } : reconcileState(loaded, liveIds);
    this.activeRuns.set(runId, reconciled);
    saveState(reconciled, this.stateDir);
    if (dropped.length > 0) {
      const rt = this.getRuntime(runId);
      for (const w of dropped) {
        const teardown = await this.harvestWorkerIOWithStatus(loaded, w);
        const offers = [...w.terminalCaptures ?? [], ...teardown.offers];
        const draft = classifyOutcomeDraft("error", {
          sidecar: teardown.sidecar,
          sidecarProbed: this.kind === "pi",
          paneMatch: null,
          paneRead: false
        });
        const outcome = await this.recordOutcome(reconciled, w, draft, offers.length);
        rt.pendingEvents.push({
          kind: "error",
          agent: w.agent,
          slug: w.slug,
          paneId: w.paneId,
          captures: offers,
          outcome,
          detail: `reconcile: agent gone from herdr between polls -- worker process vanished; ` + (outcome.evidence === "fatal_sidecar" ? "the worker's own death certificate says why: a fatal settle, not a clean finish. Verify the item's state before treating it as complete." : "outcome inferred, not observed. Verify the item's state before treating it as complete.") + `${w.pendingAmend ? ` ${amendOutstandingNote(w.pendingAmend, Date.now())}` : ""}`
        });
      }
      saveState(reconciled, this.stateDir);
    }
    return reconciled;
  }
  persist(state) {
    this.activeRuns.set(state.runId, state);
    saveState(state, this.stateDir);
  }
  async probeLiveness(agentId) {
    const controller = new AbortController();
    let abandoned = false;
    const timer = setTimeout(() => {
      abandoned = true;
      controller.abort();
    }, PROBE_TIMEOUT_MS);
    try {
      const result = await this.herdr(buildAgentGetArgv(agentId), controller.signal);
      return { ...result, abandoned };
    } finally {
      clearTimeout(timer);
    }
  }
  armWait(rt, worker) {
    if (rt.inFlight.has(worker.agent)) return;
    rt.inFlight.add(worker.agent);
    const timeoutMs = rt.timeoutMs;
    void this.settleWait(rt, worker, timeoutMs);
  }
  /**
   * Arm the post-idle watch for an amendment's turn to start.
   *
   * Separate from `armWait` because it asks a different question: not "has this
   * worker finished" but "has a NEW turn begun". herdr's wait matches the
   * agent's CURRENT state, so the two cannot share one armed wait -- which is
   * precisely why the terminal wait settling on the active turn's idle lost the
   * correction in the first place.
   */
  armAmendAckWait(rt, worker) {
    if (rt.inFlight.has(worker.agent)) return;
    rt.inFlight.add(worker.agent);
    void this.settleAmendAck(rt, worker);
  }
  /** Persist a mutated worker through the run's cached state. */
  persistRun(rt) {
    const state = this.activeRuns.get(rt.runId);
    if (state) this.persist(state);
  }
  unlinkWorkerAmendAck(runId, slug) {
    if (this.kind !== "pi") return;
    unlinkAmendAck(amendAckPath(capturePath(runId, slug, this.stateDir)));
  }
  /**
   * Two-axis verdict, upgraded to confirmed when a pi sidecar proves receipt.
   * Copilot and a missing/stale file fall through to steered/unpicked.
   */
  holdVerdict(rt, worker, pending, now) {
    const runWorkedMs = pending.runWorkedAfterAmendMs >= 0 ? pending.runWorkedAfterAmendMs : Math.max(0, now - pending.requestedAtMs);
    const ack = this.kind === "pi" ? readAmendAck(amendAckPath(capturePath(rt.runId, worker.slug, this.stateDir))) : null;
    return amendHoldVerdict({
      turnObserved: sidecarUpgradesHold(this.kind, ack, pending.requestedAtMs),
      runWorkedMs
    });
  }
  /**
   * Decide what a terminal settle means while an amendment is outstanding.
   *
   * Returns the event to report, or null when the hold was armed instead and
   * nothing should be reported yet. The hold can only ever be armed together
   * with a wait -- an un-closeable worker with nothing armed would hold its slot
   * forever, which is a worse failure than the silent loss this prevents.
   */
  amendHoldDecision(rt, worker, pending, settleStdout) {
    const now = Date.now();
    const seq = parseAgentStateSeq(settleStdout);
    if (seq !== void 0) pending.lastObservedSeq = seq;
    if (pending.phase === "await_terminal") {
      const detail = amendVerdictDetail("confirmed", worker, amendHoldMs(pending, now));
      worker.pendingAmend = void 0;
      this.persistRun(rt);
      return {
        event: {
          kind: "finished",
          agent: worker.agent,
          slug: worker.slug,
          paneId: worker.paneId,
          detail
        }
      };
    }
    if (pending.runWorkedAfterAmendMs < 0) {
      pending.runWorkedAfterAmendMs = Math.max(0, now - pending.requestedAtMs);
    }
    if (amendHoldExpired(pending, now)) {
      const verdict = this.holdVerdict(rt, worker, pending, now);
      const detail = amendVerdictDetail(verdict, worker, amendHoldMs(pending, now));
      worker.pendingAmend = void 0;
      this.persistRun(rt);
      return {
        event: {
          kind: "finished",
          agent: worker.agent,
          slug: worker.slug,
          paneId: worker.paneId,
          detail
        }
      };
    }
    pending.checks += 1;
    if (!pending.checkInReported) {
      pending.checkInReported = true;
      worker.checkIns = (worker.checkIns ?? 0) + 1;
      rt.pendingEvents.push({
        kind: "still_working",
        agent: worker.agent,
        slug: worker.slug,
        paneId: worker.paneId,
        elapsedMs: elapsedWorkingMs(worker, now) ?? 0,
        checkIn: worker.checkIns,
        detail: `amendment held: ${pending.checks}/${AMEND_ACK_MAX_CHECKS} ack watches armed, watching for the correction's turn to start before this worker can be finished off.`
      });
      this.wakeWaiters(rt);
    }
    this.persistRun(rt);
    return { rearm: "ack" };
  }
  /**
   * The ack wait's own settle: watch for the amendment's turn starting.
   *
   * Every branch that keeps waiting must leave exactly one wait armed, and does
   * so by returning a `rearm` directive rather than arming inline -- this
   * function's `finally` clears `inFlight`, and arming before that would let a
   * later poll arm a second concurrent wait for the same worker.
   */
  async settleAmendAck(rt, worker) {
    let event = null;
    let rearm = null;
    try {
      const pending = worker.pendingAmend;
      if (!pending) return;
      const ack = await this.herdr(
        buildAgentWaitArgv(worker.agent, ["working", "blocked"], AMEND_ACK_TIMEOUT_MS)
      );
      const ackStatus = ack.code === 0 ? parseAgentStatus(ack.stdout) : void 0;
      let outcome;
      let probe = null;
      if (ackStatus === "working" || ackStatus === "blocked") {
        outcome = ackStatus === "working" ? "turn_started" : "parked";
      } else {
        probe = await this.probeLiveness(worker.agent);
        outcome = probe.abandoned ? "inconclusive" : classifyAmendAck(probe.code, probe.stdout, probe.stderr, pending.lastObservedSeq);
      }
      const now = Date.now();
      switch (outcome) {
        case "turn_started":
        case "changed_while_unarmed": {
          pending.phase = "await_terminal";
          const seen = ackStatus === "working" ? parseAgentStateSeq(ack.stdout) : probe ? parseAgentStateSeq(probe.stdout) : void 0;
          if (seen !== void 0) pending.lastObservedSeq = seen;
          rearm = "terminal";
          break;
        }
        case "parked": {
          pending.phase = "await_terminal";
          rearm = null;
          event = {
            kind: "blocked",
            agent: worker.agent,
            slug: worker.slug,
            paneId: worker.paneId
          };
          break;
        }
        case "gone": {
          worker.pendingAmend = void 0;
          this.persistRun(rt);
          event = {
            kind: "error",
            agent: worker.agent,
            slug: worker.slug,
            paneId: worker.paneId,
            detail: `worker vanished while an amendment was outstanding. ${amendOutstandingNote(pending, now)}`
          };
          break;
        }
        case "settled_unchanged":
        case "inconclusive": {
          pending.checks += 1;
          if (amendHoldExpired(pending, now)) {
            const verdict = this.holdVerdict(rt, worker, pending, now);
            const heldMs = amendHoldMs(pending, now);
            worker.pendingAmend = void 0;
            this.persistRun(rt);
            event = {
              kind: "finished",
              agent: worker.agent,
              slug: worker.slug,
              paneId: worker.paneId,
              detail: amendVerdictDetail(verdict, worker, heldMs)
            };
          } else {
            rearm = "ack";
          }
          break;
        }
      }
      this.persistRun(rt);
    } catch (err) {
      event = {
        kind: "error",
        agent: worker.agent,
        slug: worker.slug,
        paneId: worker.paneId,
        detail: `amend_ack_wait_failed: ${err instanceof Error ? err.message : String(err)}`
      };
    } finally {
      rt.inFlight.delete(worker.agent);
    }
    const runState = this.activeRuns.get(rt.runId);
    if (runState && !runState.workers.some((w) => w.agent === worker.agent)) {
      this.wakeWaiters(rt);
      return;
    }
    if (event) {
      rt.pendingEvents.push(event);
      this.wakeWaiters(rt);
      return;
    }
    if (rearm === "terminal") this.armWait(rt, worker);
    else if (rearm === "ack") this.armAmendAckWait(rt, worker);
  }
  async settleWait(rt, worker, timeoutMs) {
    let event = null;
    let rearmAfter = null;
    try {
      const result = await this.herdr(
        buildAgentWaitArgv(worker.agent, ["idle", "done", "blocked"], timeoutMs)
      );
      let kind = classifyWaitResult(result.code, result.stdout, result.stderr);
      let settleStatus = parseAgentStatus(result.stdout);
      let livenessConfirmed;
      let detail = kind === "timed_out" || kind === "error" ? waitResultDetail(result.stdout, result.stderr) : void 0;
      if (kind === "timed_out") {
        const probe = await this.probeLiveness(worker.agent);
        const verdict = classifyTimeoutProbe(
          probe,
          this.elapsedWorkingMsFor(worker),
          rt.deadlineMs
        );
        if (verdict.disposition === "rearm") {
          const runState2 = this.activeRuns.get(rt.runId);
          if (runState2 && !runState2.workers.some((w) => w.agent === worker.agent)) {
            return;
          }
          worker.checkIns = (worker.checkIns ?? 0) + 1;
          rt.inFlight.delete(worker.agent);
          this.armWait(rt, worker);
          rt.pendingEvents.push({
            kind: "still_working",
            agent: worker.agent,
            slug: worker.slug,
            paneId: worker.paneId,
            elapsedMs: elapsedWorkingMs(worker, Date.now()) ?? 0,
            checkIn: worker.checkIns
          });
          this.wakeWaiters(rt);
          return;
        }
        if (verdict.kind === "error") {
          const state = this.activeRuns.get(rt.runId);
          if (state && await this.attemptCrashRecovery(state, worker, state.pluginDir)) {
            rt.inFlight.delete(worker.agent);
            this.armWait(rt, worker);
            return;
          }
        }
        kind = verdict.kind;
        settleStatus = parseAgentStatus(probe.stdout);
        livenessConfirmed = verdict.livenessConfirmed === true;
        detail = kind === "timed_out" ? deadlineStopDetail(worker, rt.deadlineMs, {
          livenessConfirmed: verdict.livenessConfirmed === true,
          probeDetail: probe.abandoned ? `the liveness probe did not answer within ${PROBE_TIMEOUT_MS} ms and was abandoned` : `probe: ${waitResultDetail(probe.stdout, probe.stderr)}`
        }) : kind === "error" ? `probe: ${waitResultDetail(probe.stdout, probe.stderr)}` : void 0;
      }
      const sidecar = kind === "blocked" ? null : this.sidecarForRun(rt.runId, worker.slug);
      if (kind === "finished" && sidecar) {
        kind = "error";
        detail = `${fatalSidecarDetail(sidecar)}${detail ? ` ${detail}` : ""}`;
      }
      const pending = worker.pendingAmend;
      let paneMatch = null;
      let paneRead = false;
      if (pending && kind === "finished") {
        const decision = this.amendHoldDecision(rt, worker, pending, result.stdout);
        if (decision.event) {
          event = decision.event;
          const screened = await this.screenFinishedForCrash(event, worker, settleStatus);
          paneMatch = screened.match;
          paneRead = screened.read;
        } else {
          rearmAfter = decision.rearm ?? null;
          event = null;
        }
      } else {
        event = { kind, agent: worker.agent, slug: worker.slug, paneId: worker.paneId };
        if (detail !== void 0) event.detail = detail;
        if (kind === "finished" && !sidecar) {
          const screened = await this.screenFinishedForCrash(event, worker, settleStatus);
          paneMatch = screened.match;
          paneRead = screened.read;
        }
        if (pending && (kind === "error" || kind === "timed_out")) {
          event.detail = `${event.detail ?? ""} ${amendOutstandingNote(pending, Date.now())}`.trim();
        }
      }
      if (event && (event.kind === "finished" || event.kind === "error" || event.kind === "timed_out")) {
        event.outcomeDraft = classifyOutcomeDraft(event.kind, {
          sidecar,
          sidecarProbed: this.kind === "pi",
          paneMatch,
          paneRead,
          ...settleStatus !== void 0 ? { herdrStatus: settleStatus } : {},
          ...livenessConfirmed === void 0 ? {} : { livenessConfirmed }
        });
      }
    } catch (err) {
      event = {
        kind: "error",
        agent: worker.agent,
        slug: worker.slug,
        paneId: worker.paneId,
        detail: `wait_failed: ${err instanceof Error ? err.message : String(err)}`
      };
    } finally {
      rt.inFlight.delete(worker.agent);
    }
    const runState = this.activeRuns.get(rt.runId);
    if (runState && !runState.workers.some((w) => w.agent === worker.agent)) {
      this.wakeWaiters(rt);
      return;
    }
    if (rearmAfter === "ack") {
      this.armAmendAckWait(rt, worker);
      return;
    }
    if (rearmAfter === "terminal") {
      this.armWait(rt, worker);
      return;
    }
    if (event === null) {
      return;
    }
    rt.pendingEvents.push(event);
    this.wakeWaiters(rt);
  }
  elapsedWorkingMsFor(worker) {
    const now = Date.now();
    if (worker.workingSinceMs === void 0 && worker.accumulatedWorkingMs === void 0) {
      worker.workingSinceMs = now;
    }
    return elapsedWorkingMs(worker, now);
  }
  /**
   * Screen a settled `finished` event for positive evidence that the worker did
   * not actually finish.
   *
   * Two independent kinds of evidence, in descending confidence:
   *
   * 1. **The fatal-exit sentinel.** `fatal-error-exit.ts` writes a fixed line
   *    and then exits 1, but herdr publishes the terminal `done` status ~0.17 s
   *    before the agent record disappears (measured live 2026-09-11), so the
   *    armed wait resolves against `done` and `classifyWaitResult` calls it
   *    `finished` exactly like the parked-at-prompt `idle` signal. Without this
   *    branch the exit buys no classification advantage on the primary path at
   *    all. Gated on `settleStatus !== "idle"`: `idle` is the only status that
   *    positively proves the process is alive at its prompt, so a sentinel in an
   *    `idle` worker's pane is stale prose (workers grep this repo and run its
   *    tests, which print the line) rather than an exit. Anything else --
   *    `done`, or no usable status -- is not proof of life, and the gate's
   *    failure direction is the safe one: it can only turn a clean-looking
   *    finish into a verify-me error.
   * 2. **Provider-crash wording**, for a worker that died under a usage limit
   *    and stayed alive at its prompt, so the wait legitimately settles `idle`
   *    (observed live 2026-09-10).
   *
   * One pane read per resolved finish -- never on held/re-armed settles -- and
   * each classifier owns its own line-based window, so no character slicing
   * happens here. The read always precedes teardown: `closeWorker` is reached
   * only from `harvestWorkerIOWithStatus`, downstream of the event being pushed,
   * and `swarm_poll: a worker that exited on a fatal turn error...` asserts that
   * ordering rather than trusting the call graph.
   *
   * Detail combination is append-only for both branches: a detected crash
   * PREPENDS its detail to whatever the branch already carries (e.g. an amend
   * verdict, whose record of whether the CORRECTION survived is separate
   * evidence from whether the PROCESS did); an unavailable capture APPENDS its
   * verification note. An unreadable or empty pane never reclassifies the finish
   * -- the wait settled cleanly, so an inconclusive probe must not manufacture
   * an error -- but it is no longer silent about it.
   *
   * Returns what was actually observed, because the outcome ledger has to say
   * "the pane was read and said nothing" differently from "nothing could be
   * read": the first is evidence of absence, the second is absence of evidence,
   * and collapsing them would hide the exact case these screens exist for.
   */
  async screenFinishedForCrash(event, worker, settleStatus, signal) {
    if (event.kind !== "finished") return { read: false, match: null };
    let pane;
    try {
      const read = await this.herdr(buildPaneReadArgv(worker.paneId, PANE_CAPTURE_LINES), signal);
      pane = read.code === 0 ? read.stdout : null;
    } catch {
      pane = null;
    }
    if (pane === null || pane.trim() === "") {
      event.detail = `${event.detail ? `${event.detail} ` : ""}pane capture unavailable at settle -- verify the item's state before treating it as complete.`;
      return { read: false, match: null };
    }
    if (settleStatus !== "idle") {
      const fatal = fatalErrorExitMatch(pane);
      if (fatal) {
        event.kind = "error";
        event.detail = `fatal_error_exit: pane carried the worker's ${fatal.signature} sentinel at a ${settleStatus ?? "status-less"} settle -- the process exited on a fatal turn error, not a clean finish. Excerpt: "${fatal.excerpt}". Verify the item's actual state before treating it as complete.` + (event.detail ? ` ${event.detail}` : "");
        return { read: true, match: "fatal_sentinel" };
      }
    }
    const crash = providerCrashMatch(pane);
    if (!crash) return { read: true, match: null };
    event.kind = "error";
    event.detail = `provider_crash: pane matched "${crash.signature}" near the idle prompt -- the worker most likely died under a provider limit, not a clean finish. Excerpt: "${crash.excerpt}". Verify the item's actual state before treating it as complete.` + (event.detail ? ` ${event.detail}` : "");
    return { read: true, match: "provider_wording" };
  }
  async swarmSpawn(params) {
    if (!params.items && !params.prefix) {
      throw new Error(
        "swarm_spawn needs either `items` or `prefix`. Selecting from the whole READY queue unscoped would pull unrelated projects into this run."
      );
    }
    if (params.mode === "serial" && params.concurrency !== void 0 && params.concurrency !== 1) {
      throw new Error("serial mode requires concurrency 1 when concurrency is supplied");
    }
    return this.withSpawnLock(params.runId, async () => {
      const state = await this.getOrInitState(
        params.runId,
        params.mode === "serial" ? 1 : params.concurrency ?? DEFAULT_CONCURRENCY,
        params.prefix,
        params.pluginDir,
        params.mode
      );
      if (state.mode === "serial") {
        state.concurrency = 1;
      } else if (params.concurrency !== void 0) {
        state.concurrency = params.concurrency;
      }
      if (params.pluginDir !== void 0) state.pluginDir = params.pluginDir;
      const pruneLines = await this.pruneStaleWorkers(state);
      if (!canSpawnNew(state)) {
        return {
          content: [
            {
              type: "text",
              text: `Concurrency cap reached (${state.concurrency} active workers). Call swarm_poll to wait for workers to settle.`
            }
          ],
          details: {
            spawned: [],
            failed: [],
            skipped: params.items ?? [],
            deferred: [],
            refused: []
          }
        };
      }
      if (!canOpenNewPane(state)) {
        return {
          content: [
            {
              type: "text",
              text: `Open-pane soft cap reached (${openPaneCount(state)}/${openPaneSoftCap(state.concurrency)} open panes). Parked workers awaiting a relay are holding panes -- answer each with swarm_resolve_blocked before spawning more.`
            }
          ],
          details: { spawned: [], failed: [], skipped: [], deferred: [], refused: [] }
        };
      }
      let candidates;
      if (params.items) {
        const explicitSlugs = params.items;
        const readyResult = await this.exec("python3", buildReadyArgv(params.prefix).slice(1), {
          timeout: PROBE_TIMEOUT_MS
        });
        if (readyResult.code !== 0) {
          throw new Error(
            `dev_status.py ready failed: ${readyResult.stderr || readyResult.stdout}`
          );
        }
        const parsed = parseReadyItems(readyResult.stdout);
        const byId = new Map(parsed.map((i) => [i.id, i]));
        candidates = explicitSlugs.map((id) => byId.get(id) ?? { id });
      } else {
        const readyResult = await this.exec("python3", buildReadyArgv(params.prefix).slice(1), {
          timeout: PROBE_TIMEOUT_MS
        });
        if (readyResult.code !== 0) {
          throw new Error(
            `dev_status.py ready failed: ${readyResult.stderr || readyResult.stdout}`
          );
        }
        candidates = parseReadyItems(readyResult.stdout);
        const attempted = new Set(state.attempted ?? []);
        const refused2 = new Set((state.refused ?? []).map((entry) => entry.slug));
        candidates = candidates.filter((c) => !attempted.has(c.id) && !refused2.has(c.id));
      }
      const takenPaths = [];
      for (const w of state.workers) {
        for (const p of w.paths ?? []) {
          takenPaths.push({ path: p, holder: `worker ${w.agent} (${w.slug}, ${w.lifecycle})` });
        }
      }
      const budget = spawnBudget(state, candidates.length);
      const selection = selectSchedulable(
        candidates,
        takenPaths,
        budget,
        state.mode ?? "concurrent"
      );
      const toSpawn = selection.slugs;
      const spawned = [];
      const failed = [];
      const tabs = [];
      for (const slug of toSpawn) {
        const captureFile = capturePath(state.runId, slug, this.stateDir);
        try {
          rmSync(outcomePathOf(state.runId, slug, this.stateDir), { force: true });
        } catch {
        }
        const tabCreated = await this.herdr(
          buildTabCreateArgv(process.cwd(), slug, { captureFile, kind: this.kind })
        );
        let parsedTab = parseTabCreate(tabCreated.stdout);
        if (!parsedTab && tabCreated.code === 0) {
          const orphan = await this.recoverTabByLabel(slug);
          const head = (tabCreated.stderr || tabCreated.stdout).slice(0, 200);
          failed.push({
            slug,
            reason: orphan ? `could not parse tab create response; the tab it created was found by label and closed (${orphan}): ${head}` : `could not parse tab create response, and no single tab labelled "${slug}" was found -- a tab may be open and unaccounted for, close it by hand: ${head}`
          });
          continue;
        }
        if (!parsedTab || !parsedTab.paneId) {
          failed.push({
            slug,
            reason: `tab_create_failed: ${tabCreated.stderr || tabCreated.stdout}`
          });
          continue;
        }
        tabs.push({ slug, created: parsedTab });
      }
      const startResults = await Promise.allSettled(
        tabs.map(async (t) => {
          const candidateItem = candidates.find((c) => c.id === t.slug);
          const paths = candidateItem ? itemPaths(candidateItem) : [];
          const agentId = nextAgentId(state.runId, state.nextCounter++, t.slug);
          const { paneId, tabId } = t.created;
          try {
            return await this.spawnInto(
              paneId,
              tabId,
              agentId,
              t.slug,
              paths,
              params.model,
              params.pluginDir
            );
          } catch (e) {
            return this.failWithTab(t.slug, paneId, tabId, `spawn_error: ${String(e)}`);
          }
        })
      );
      for (const r of startResults) {
        if (r.status === "fulfilled") {
          if ("worker" in r.value) {
            spawned.push(r.value.worker);
            state.workers.push(r.value.worker);
          } else if (r.value.failed) {
            failed.push(r.value.failed);
          }
        } else {
          failed.push({ slug: "unknown", reason: `spawn_error: ${String(r.reason)}` });
        }
      }
      state.attempted = [.../* @__PURE__ */ new Set([...state.attempted ?? [], ...toSpawn])];
      const refusedBySlug = new Map(
        [...state.refused ?? [], ...selection.refused].map((entry) => [entry.slug, entry])
      );
      state.refused = [...refusedBySlug.values()];
      this.persist(state);
      const rt = this.getRuntime(state.runId);
      for (const w of spawned) this.armWait(rt, w);
      const skipped = selection.skipped;
      const deferred = selection.deferred;
      const refused = selection.refused;
      const parts = [
        `Spawned ${spawned.length} worker(s)`,
        `${failed.length} failed to spawn`,
        `${skipped.length} skipped (cap)`,
        `${deferred.length} deferred (file overlap)`,
        `${refused.length} refused (not ${state.mode === "serial" ? "serial-safe" : "worker-safe"})`
      ];
      const lines = [`${parts.join(", ")}.`];
      lines.push(...pruneLines);
      for (const f of failed) lines.push(`- ${f.slug}: ${reasonHeadline(f.reason)}`);
      for (const d of deferred) lines.push(`- ${d.slug}: deferred -- ${d.reason}`);
      for (const r of refused) lines.push(`- ${r.slug}: refused -- ${r.reason}`);
      if (spawned.length === 0 && deferred.length > 0) {
        lines.push(
          "Nothing spawned but items remain: poll the running workers, then call swarm_spawn again once one finishes."
        );
      } else if (spawned.length === 0 && refused.length > 0) {
        lines.push(
          "Nothing spawned and the remaining items are refused, not waiting: they are never schedulable by a worker. This is the end of the swarm phase for this prefix -- report them as needing a normal session rather than polling or spawning again."
        );
      }
      if ((state.mode ?? "concurrent") === "serial" && spawned.length === 0 && failed.length === 0 && skipped.length === 0 && deferred.length === 0 && refused.length === 0 && state.workers.length === 0) {
        const dashboard = await this.exec("python3", buildRenderArgv().slice(1), {
          timeout: PROBE_TIMEOUT_MS
        });
        lines.push(
          dashboard.code === 0 ? `Serial queue is quiescent. Current dashboard:
${dashboard.stdout.trimEnd()}` : `Serial queue is quiescent, but dev_status.py render failed: ${dashboard.stderr || dashboard.stdout}`
        );
      }
      return {
        content: [{ type: "text", text: lines.join("\n") }],
        details: { spawned, failed, skipped, deferred, refused }
      };
    });
  }
  async swarmPoll(params, signal) {
    const state = await this.getOrInitState(params.runId, DEFAULT_CONCURRENCY);
    const rt = this.getRuntime(params.runId);
    rt.timeoutMs = params.timeoutMs ?? DEFAULT_WAIT_TIMEOUT_MS;
    rt.deadlineMs = params.workerDeadlineMs ?? DEFAULT_WORKER_DEADLINE_MS;
    rt.stallMs = params.relayStallMs ?? DEFAULT_RELAY_STALL_MS;
    const parkedNow = state.workers.filter((w) => w.lifecycle === "awaiting_relay");
    const resyncNotes = [];
    if (parkedNow.length > 0) {
      const gets = await Promise.all(
        parkedNow.map(async (worker) => {
          try {
            const r = await this.herdr(buildAgentGetArgv(worker.agent), signal);
            return { worker, verdict: classifyResyncGet(r.code, r.stdout, r.stderr) };
          } catch {
            return { worker, verdict: { action: "keep" } };
          }
        })
      );
      const resumedAt = Date.now();
      for (const { worker, verdict } of gets) {
        if (verdict.action === "drop") {
          rt.inFlight.delete(worker.agent);
          rt.pendingEvents.push({
            kind: (state.mode ?? "concurrent") === "serial" ? "error" : "finished",
            agent: worker.agent,
            slug: worker.slug,
            paneId: worker.paneId,
            detail: (state.mode ?? "concurrent") === "serial" ? "resync: the worker disappeared while awaiting a relay; that relay is cancelled and the item outcome is failed, not inferred complete." : "resync: agent gone from herdr while its record said awaiting_relay -- its gate was likely answered out-of-band (direct pane keys) and the worker has since finished or exited; outcome inferred, not observed. Verify the item's state before treating it as complete."
          });
        } else if (verdict.action === "unpark") {
          worker.workingSinceMs = resumedAt;
          worker.awaitingRelaySinceMs = void 0;
          worker.lastResolveFailure = void 0;
          worker.lifecycle = "active";
          resyncNotes.push(
            `${worker.agent} (${worker.slug}) was parked awaiting a relay, but herdr now reports it unblocked -- resumed tracking as active.`
          );
        }
      }
      if (resyncNotes.length > 0) this.persist(state);
    }
    const stampNow = Date.now();
    for (const w of state.workers) {
      if (w.lifecycle === "awaiting_relay" && w.awaitingRelaySinceMs === void 0) {
        w.awaitingRelaySinceMs = stampNow;
      }
    }
    const active = state.workers.filter((w) => w.lifecycle === "active");
    for (const w of active) this.armWait(rt, w);
    if (active.length === 0 && rt.pendingEvents.length === 0) {
      const goneNoteLines = await this.pruneStaleWorkers(state);
      const awaitingRelay = state.workers.filter((w) => w.lifecycle === "awaiting_relay");
      const ambiguous = state.workers.filter((w) => w.lifecycle === "teardown_ambiguous");
      const stalledHere = stalledRelayWorkers(state.workers, Date.now(), rt.stallMs);
      const stalledAgents = new Set(stalledHere.map((w) => w.agent));
      const describe = (w) => `${w.agent} (${w.slug}, pane ${w.paneId})` + (stalledAgents.has(w.agent) ? ` -- STALLED, over ${formatDuration(rt.stallMs)} with no answer` : "") + (w.lastResolveFailure ? ` -- a previous answer ${JSON.stringify(w.lastResolveFailure.answer)} failed to land (${w.lastResolveFailure.reason}); re-read the pane and answer with its EXACT rendered label` : "");
      const text = (awaitingRelay.length ? `No active workers to poll. ${awaitingRelay.length} worker(s) awaiting a relay -- answer each with swarm_resolve_blocked before polling again: ${awaitingRelay.map(describe).join(", ")}.` : ambiguous.length ? `No active workers to poll. ${ambiguous.length} worker(s) have ambiguous teardown and still occupy the serial slot: ${ambiguous.map((w) => `${w.agent} (${w.slug}, pane ${w.paneId})`).join(", ")}. Reconcile or close them before spawning the next item.` : goneNoteLines.length ? "No active workers to poll (all tracked workers vanished or were cleared before settling):" : "No active workers to poll.") + (goneNoteLines.length ? `

${goneNoteLines.join("\n")}` : "");
      return {
        content: [{ type: "text", text }],
        details: { events: [] }
      };
    }
    let aborted = false;
    while (rt.pendingEvents.length === 0) {
      if (state.workers.filter((w) => w.lifecycle === "active").length === 0) {
        break;
      }
      if (!await this.waitForEvent(rt, signal)) {
        aborted = true;
        break;
      }
    }
    if (aborted) {
      return {
        content: [
          {
            type: "text",
            text: "swarm_poll aborted before any worker settled. Workers are untouched and still running -- poll again to pick their events back up."
          }
        ],
        details: { events: [] }
      };
    }
    const rawEvents = rt.pendingEvents.splice(0);
    const workersByAgent = new Map(state.workers.map((w) => [w.agent, w]));
    const toRemove = /* @__PURE__ */ new Set();
    const processed = await Promise.all(
      rawEvents.map(async (event) => {
        const worker = workersByAgent.get(event.agent);
        if (!worker) {
          return event.detail?.startsWith("reconcile: ") ? event : null;
        }
        if (event.kind === "blocked") {
          let getResult;
          try {
            getResult = await this.herdr(buildAgentGetArgv(event.agent), signal);
          } catch {
            getResult = { code: 1, stdout: "", stderr: "" };
          }
          const resyncVerdict = classifyResyncGet(
            getResult.code,
            getResult.stdout,
            getResult.stderr
          );
          if (resyncVerdict.action === "drop") {
            event.kind = (state.mode ?? "concurrent") === "serial" ? "error" : "finished";
            event.detail = (state.mode ?? "concurrent") === "serial" ? "resync: the worker disappeared while resolving its blocked prompt; the relay is cancelled and the item outcome is failed." : "resync: agent gone from herdr while resolving blocked prompt -- worker has since finished or exited; outcome inferred, not observed. Verify the item's state before treating it as complete.";
            const teardown = await this.harvestWorkerIOWithStatus(state, worker, signal);
            event.captures = teardown.offers;
            event.outcome = await this.recordOutcome(
              state,
              worker,
              this.draftWithSidecar(event.outcomeDraft, event.kind, teardown.sidecar),
              teardown.offers.length,
              signal,
              event
            );
            if ((state.mode ?? "concurrent") === "serial" && !teardown.closed) {
              worker.lifecycle = "teardown_ambiguous";
              worker.terminalOutcome = event.kind;
              worker.terminalCaptures = event.captures;
              worker.teardownDetail = this.teardownRecovery(worker);
              event.detail = `${event.detail} Teardown is ambiguous: ${worker.teardownDetail}; no later serial worker will start until the record reconciles.`;
            } else {
              toRemove.add(worker.agent);
            }
            return event;
          }
          let readResult;
          try {
            readResult = await this.herdr(
              buildAgentReadArgv(event.agent, BLOCKED_READ_LINES),
              signal
            );
          } catch {
            readResult = { code: 1, stdout: "", stderr: "" };
          }
          let truncated = looksTruncated(readResult.stdout, BLOCKED_READ_LINES);
          if (truncated) {
            try {
              readResult = await this.herdr(
                buildAgentReadArgv(event.agent, BLOCKED_READ_LINES_RETRY),
                signal
              );
              truncated = looksTruncated(readResult.stdout, BLOCKED_READ_LINES_RETRY);
            } catch {
              truncated = false;
            }
          }
          event.rawPrompt = readResult.stdout || getResult.stdout;
          event.truncated = truncated;
          event.blockClass = this.picker.classifyBlock(event.rawPrompt);
          event.options = this.picker.pickerLabels(event.rawPrompt);
          const parkedAt = Date.now();
          foldWorkingSegment(worker, parkedAt);
          worker.awaitingRelaySinceMs = parkedAt;
          worker.lifecycle = "awaiting_relay";
        } else if (event.kind === "still_working") {
        } else {
          const teardown = await this.harvestWorkerIOWithStatus(state, worker, signal);
          event.captures = teardown.offers;
          event.outcome = await this.recordOutcome(
            state,
            worker,
            this.draftWithSidecar(event.outcomeDraft, event.kind, teardown.sidecar),
            teardown.offers.length,
            signal,
            event
          );
          if ((state.mode ?? "concurrent") === "serial" && !teardown.closed) {
            worker.lifecycle = "teardown_ambiguous";
            worker.terminalOutcome = event.kind;
            worker.terminalCaptures = event.captures;
            worker.teardownDetail = this.teardownRecovery(worker);
            event.detail = `${event.detail ? `${event.detail} ` : ""}Teardown is ambiguous: ${worker.teardownDetail}; no later serial worker will start until the record reconciles.`;
          } else {
            toRemove.add(worker.agent);
          }
        }
        return event;
      })
    );
    const events = processed.filter((e) => e !== null);
    if (toRemove.size > 0) {
      state.workers = state.workers.filter((w) => !toRemove.has(w.agent));
    }
    this.persist(state);
    const stalled = stalledRelayWorkers(state.workers, Date.now(), rt.stallMs);
    const stalledNote = stalled.length ? `

${stalled.length} worker(s) have been awaiting a relay for over ${formatDuration(rt.stallMs)} and are not progressing -- each needs a human answer in its own pane: ${stalled.map((w) => `${w.agent} (${w.slug}, pane ${w.paneId})`).join(", ")}.` : "";
    const resyncNote = resyncNotes.length ? `

${resyncNotes.join(" ")}` : "";
    const held = state ? pendingAmendWorkers(state) : [];
    const amendNote = held.length ? `

${held.length} worker(s) are being held open for an unacknowledged amendment and will not settle until it is accounted for: ${held.map(
      (w) => `${w.agent} (${w.slug}, phase ${w.pendingAmend?.phase}, watch ${w.pendingAmend?.checks ?? 0}/${AMEND_ACK_MAX_CHECKS})`
    ).join(
      ", "
    )}. Their slots stay occupied -- do NOT spawn replacements or treat the run as drained.` : "";
    return {
      content: [
        {
          type: "text",
          text: (events.length > 0 ? events.map((e) => {
            if (e.kind === "blocked") {
              const verdict = `needs_human -- NOT a question-tool picker, so swarm_resolve_blocked cannot drive it. Relay the prompt below to the user verbatim and tell them to answer in pane ${e.paneId} themselves`;
              return `${e.slug} (${e.agent}, pane ${e.paneId}) is blocked [${verdict}]${e.truncated ? " -- content may be truncated, inspect the pane directly" : ""}:
${e.rawPrompt}`;
            }
            if (e.kind === "still_working") {
              return `${e.slug} (${e.agent}) still_working -- check-in ${e.checkIn}, ${formatDuration(e.elapsedMs ?? 0)} of working time so far against a ${formatDuration(rt.deadlineMs)} budget. Nothing settled and no slot was freed; poll again.`;
            }
            const captures = renderCaptureOffers(e.captures ?? []);
            const outcome = e.outcome ? ` ${renderOutcome(e.outcome)}` : "";
            return `${e.slug} (${e.agent}) ${e.kind}${e.detail ? `: ${e.detail}` : ""}${outcome}${captures}`;
          }).join("\n\n") : "No active workers to poll.") + stalledNote + resyncNote + amendNote
        }
      ],
      details: { events }
    };
  }
  async swarmAmend(params, signal) {
    const state = await this.getOrInitState(params.runId, DEFAULT_CONCURRENCY);
    const worker = state.workers.find((w) => w.agent === params.agent) ?? state.workers.find((w) => w.slug === params.agent);
    if (!worker) {
      return {
        content: [
          {
            type: "text",
            text: `amend_failed: no worker in run ${params.runId} matches "${params.agent}" by agent id or slug. Active workers: ${state.workers.map((w) => `${w.agent} (${w.slug})`).join(", ") || "none"}.`
          }
        ],
        details: { amended: false, slug: "", paneId: "" }
      };
    }
    if (worker.lifecycle !== "active") {
      return {
        content: [
          {
            type: "text",
            text: `amend_refused: ${worker.agent} (${worker.slug}, pane ${worker.paneId}) is parked at a gate, and herdr agent prompt refuses a blocked agent -- nothing was sent. Answer it with swarm_resolve_blocked first, then amend, or amend after it finishes and pick the item up again.`
          }
        ],
        details: { amended: false, slug: worker.slug, paneId: worker.paneId }
      };
    }
    this.unlinkWorkerAmendAck(params.runId, worker.slug);
    const requestedAtMs = Date.now();
    const result = await this.herdr(buildAgentPromptArgv(worker.agent, AMEND_INSTRUCTION), signal);
    if (result.code !== 0) {
      return {
        content: [
          {
            type: "text",
            text: `amend_failed: ${worker.agent} (${worker.slug}) -- herdr agent prompt exited ${result.code}: ${result.stderr || result.stdout}`
          }
        ],
        details: { amended: false, slug: worker.slug, paneId: worker.paneId }
      };
    }
    worker.amendments = [...worker.amendments ?? [], { at: Date.now(), by: "swarm_amend" }];
    const existing = worker.pendingAmend;
    const seq = await this.herdr(buildAgentGetArgv(worker.agent), signal);
    worker.pendingAmend = {
      requestedAtMs,
      seqAtRequest: parseAgentStateSeq(seq.stdout) ?? null,
      lastObservedSeq: parseAgentStateSeq(seq.stdout) ?? null,
      phase: "await_turn",
      // One hold, not two: a second correction supersedes the first in the same
      // backlog record, so the outstanding watch is re-armed rather than stacked.
      checks: existing ? existing.checks + 1 : 0,
      checkInReported: false,
      runWorkedAfterAmendMs: -1
    };
    this.persist(state);
    const superseded = existing ? ` This supersedes an earlier amendment still being watched (watch ${existing.checks + 1}), whose correction the updated item has already replaced. ` : "";
    return {
      content: [
        {
          type: "text",
          text: `amended: ${worker.agent} (${worker.slug}, pane ${worker.paneId}) was told to re-read its item, and the run now holds its teardown until that correction is accounted for. At submission nothing is known yet -- the verdict arrives on this worker's next poll events as one of \`amend_confirmed\` (the amendment's turn ran), \`amend_steered\` (the worker kept working past the steering window, so the correction was most likely taken inside the finished turn -- not positively confirmed) or \`amend_unpicked\` (the run settled immediately and no new turn started, so the correction was very likely never read).` + superseded + " Say in the end-of-run digest that this item was amended mid-flight, and with which verdict it ended."
        }
      ],
      details: { amended: true, slug: worker.slug, paneId: worker.paneId, held: true }
    };
  }
  async swarmResolveBlocked(params, signal) {
    const state = await this.getOrInitState(params.runId, DEFAULT_CONCURRENCY);
    const worker = state.workers.find((w) => w.agent === params.agent);
    if (!worker) {
      return {
        content: [
          {
            type: "text",
            text: `relay_failed: no tracked worker "${params.agent}" in run ${params.runId}.`
          }
        ],
        details: { relayFailed: true, needsManual: false, slug: "", paneId: "" }
      };
    }
    if (this.kind === "pi") {
      return this.resolvePiBlocked(state, worker, params.answer, signal);
    }
    let rawPrompt = "";
    try {
      const readResult = await this.herdr(
        buildAgentReadArgv(params.agent, BLOCKED_READ_LINES),
        signal
      );
      rawPrompt = readResult.stdout;
    } catch {
    }
    noteResolveFailure(
      worker,
      params.answer,
      "copilot workers require manual response",
      Date.now()
    );
    this.persist(state);
    return {
      content: [
        {
          type: "text",
          text: `needs_manual: Copilot workers do not have a programmatic picker -- manual input required for ${params.agent} (${worker.slug}, pane ${worker.paneId}). Attach directly (herdr agent attach ${params.agent}) or switch to pane ${worker.paneId} to respond.` + (rawPrompt ? `
Captured prompt:
${rawPrompt.slice(-2e3)}` : "")
        }
      ],
      details: {
        relayFailed: false,
        needsManual: true,
        slug: worker.slug,
        paneId: worker.paneId
      }
    };
  }
  async resolvePiBlocked(state, worker, answer, signal) {
    const read = await this.herdr(buildAgentReadArgv(worker.agent, BLOCKED_READ_LINES), signal);
    const picker = this.picker.parsePicker(read.stdout);
    const target = matchOption(answer, picker.options);
    const manual = (reason) => {
      noteResolveFailure(worker, answer, reason, Date.now());
      this.persist(state);
      return {
        content: [
          {
            type: "text",
            text: `needs_manual: ${reason} for ${worker.agent} (${worker.slug}, pane ${worker.paneId}).`
          }
        ],
        details: {
          relayFailed: false,
          needsManual: true,
          slug: worker.slug,
          paneId: worker.paneId
        }
      };
    };
    if (!target || picker.selectedIndex === null) return manual("no listed option matched");
    const identity = await this.herdr(buildAgentGetArgv(worker.agent), signal);
    if (paneIdentityMismatch(identity.code, identity.stdout, worker.paneId))
      return manual("pane identity mismatch");
    const keys = await this.herdr(
      buildAgentSendKeysArgv(worker.agent, navigationKeys(picker.selectedIndex, target.index)),
      signal
    );
    if (keys.code !== 0)
      return this.relayFailure(
        state,
        worker,
        `could not send navigation keys to ${worker.agent}: ${keys.stderr || keys.stdout}`,
        signal
      );
    const verify = await this.herdr(
      buildAgentWaitArgv(worker.agent, ["idle", "done", "working"], RESOLVE_VERIFY_TIMEOUT_MS),
      signal
    );
    if (verify.code !== 0)
      return this.relayFailure(
        state,
        worker,
        `${worker.agent} did not resume within ${RESOLVE_VERIFY_TIMEOUT_MS} ms after "${target.label}" was submitted.`,
        signal
      );
    worker.workingSinceMs = Date.now();
    worker.awaitingRelaySinceMs = void 0;
    worker.lastResolveFailure = void 0;
    worker.lifecycle = "active";
    this.persist(state);
    return {
      content: [
        {
          type: "text",
          text: `resolved: ${worker.agent} (${worker.slug}, pane ${worker.paneId}) answered "${target.label}", back in the active pool.`
        }
      ],
      details: { relayFailed: false, needsManual: false, slug: worker.slug, paneId: worker.paneId }
    };
  }
  async relayFailure(state, worker, reason, signal) {
    const captures = await this.teardownAndHarvestWorker(state, worker, signal);
    const teardown = worker.lifecycle === "teardown_ambiguous" ? ` Teardown is ambiguous: ${worker.teardownDetail}; the serial queue remains paused.` : "";
    const heldNote = worker.pendingAmend ? ` ${amendOutstandingNote(worker.pendingAmend, Date.now())}` : "";
    return {
      content: [
        {
          type: "text",
          text: `relay_failed: ${reason}${teardown}${heldNote}${renderCaptureOffers(captures)}`
        }
      ],
      details: {
        relayFailed: true,
        needsManual: false,
        slug: worker.slug,
        paneId: worker.paneId,
        captures
      }
    };
  }
};
export {
  PANE_CAPTURE_CHARS,
  SwarmToolContext,
  buildReadyArgv,
  buildRenderArgv,
  buildShowArgv,
  capturePath,
  copilotPluginDir,
  defaultExec,
  devStatusPath,
  elapsedWorkingMs,
  fatalSidecarDetail,
  foldWorkingSegment,
  formatDuration,
  herdrStateDir,
  isValidUuid,
  loadState,
  looksTruncated,
  outcomePathOf,
  readAmendAck,
  readCaptureOffers,
  readFatalSidecar,
  reconcileState,
  renderCaptureOffers,
  renderOutcome,
  saveState,
  statePath,
  unlinkAmendAck
};

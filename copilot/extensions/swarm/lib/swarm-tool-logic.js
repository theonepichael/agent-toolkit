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
  return status !== undefined && TERMINAL_AGENT_STATUSES.includes(status);
}
var RECONCILE_MIN_AGE_MS = 60000;
function staleWorkerRecords(state, live, now) {
  const statusById = new Map(live.map((e) => [e.id, e.status]));
  return state.workers.filter((w) => {
    const status = statusById.get(w.agent);
    if (status === undefined)
      return true;
    if (!isTerminalAgentStatus(status))
      return false;
    const began = w.workingSinceMs ?? w.awaitingRelaySinceMs;
    if (began === undefined)
      return false;
    return now - began >= RECONCILE_MIN_AGE_MS;
  });
}
var PROJECT_PREFIXES = ["iron-lb-", "meta-", "work-", "atk-"];
function nextAgentId(runId, counter, slug) {
  const cleanSlug = slug ? slug.replace(/[^a-zA-Z0-9_-]/g, "") : "";
  const matched = PROJECT_PREFIXES.filter((prefix) => cleanSlug.startsWith(prefix)).sort((a, b) => b.length - a.length)[0];
  const stripped = matched ? cleanSlug.slice(matched.length) : cleanSlug;
  if (!stripped)
    return `${runId}-w${counter}`;
  const base = `${runId}-w${counter}-${stripped}`;
  if (base.length <= 32)
    return base;
  const remaining = 32 - `${runId}-w${counter}-`.length;
  if (remaining < 1)
    return base.slice(0, 32);
  return `${runId}-w${counter}-${stripped.slice(-remaining)}`;
}
function stalledRelayWorkers(workers, now, stallMs) {
  return workers.filter((w) => w.lifecycle === "awaiting_relay" && w.awaitingRelaySinceMs !== undefined && now - w.awaitingRelaySinceMs >= stallMs);
}
function parseReadyItems(stdout) {
  try {
    const parsed = JSON.parse(stdout);
    if (!Array.isArray(parsed))
      return [];
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
  if (captureCount > 0)
    return false;
  return shownStatus === "open" || shownStatus === "in-progress";
}
function itemPaths(item) {
  const paths = (item.related_files ?? []).map((f) => f?.path).filter((p) => typeof p === "string" && p.length > 0);
  return [...new Set(paths)];
}
function pathsCollide(a, b) {
  const x = a.replace(/\/+$/, "");
  const y = b.replace(/\/+$/, "");
  if (x === y)
    return true;
  return x.startsWith(`${y}/`) || y.startsWith(`${x}/`);
}
function selectSchedulable(candidates, takenPaths, headroom) {
  const slugs = [];
  const deferred = [];
  const skipped = [];
  const refused = [];
  const taken = [...takenPaths];
  const seen = new Set;
  for (const candidate of candidates) {
    if (seen.has(candidate.id))
      continue;
    seen.add(candidate.id);
    if (candidate.worker_safe !== true) {
      refused.push({
        slug: candidate.id,
        reason: candidate.worker_safe === false ? "the backlog reports this item is not worker-safe -- its prefix " + "names the harness repo, or is unrecognised. A worker would be " + "editing the code it is running. Work it in a normal session." : "dev_status.py ready reported no worker_safe field for this " + "item, so eligibility is unknown and it is refused rather than " + "assumed safe. Update the installed dev_status.py."
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
      if (hit !== undefined) {
        clashPath = p;
        clashHolder = hit.holder;
        break;
      }
    }
    if (clashPath !== undefined) {
      deferred.push({
        slug: candidate.id,
        reason: `file overlap with ${clashHolder}: ${clashPath}`
      });
      continue;
    }
    slugs.push(candidate.id);
    taken.push(...paths.map((p) => ({
      path: p,
      holder: `candidate ${candidate.id} (selected earlier this wave)`
    })));
  }
  return { slugs, deferred, skipped, refused };
}
function activeWorkerCount(state) {
  return state.workers.filter((w) => w.lifecycle === "active").length;
}
function canSpawnNew(state) {
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
  const byConcurrency = Math.max(0, state.concurrency - activeWorkerCount(state));
  const byPaneCap = Math.max(0, openPaneSoftCap(state.concurrency) - openPaneCount(state));
  return Math.min(byConcurrency, byPaneCap, readyCount);
}

// pi/extensions/swarm-lib/swarm-herdr.ts
import { basename, dirname, join } from "node:path";
var AGENT_START_TIMEOUT_MS = 30000;
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
    const matches = (parsed.result?.tabs ?? []).filter((t) => t.label === label && typeof t.tab_id === "string");
    return matches.length === 1 ? matches[0].tab_id : undefined;
  } catch {
    return;
  }
}
function parseTabCreate(stdout) {
  try {
    const parsed = JSON.parse(stdout);
    const paneId = parsed.result?.root_pane?.pane_id;
    const tabId = parsed.result?.tab?.tab_id;
    if (typeof paneId !== "string" || typeof tabId !== "string")
      return;
    return { paneId, tabId };
  } catch {
    return;
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
var AMEND_INSTRUCTION = "STOP and re-read your backlog item before doing anything else: run " + "`python3 ~/.claude/scripts/dev_status.py show <your slug>` and read the " + "whole record fresh. Its context or next_steps have been corrected since " + "you started, so any plan you formed from the earlier version may now be " + "wrong. Reconcile what you have already done against the updated record, " + "and say plainly what changes as a result before continuing.";
function buildAgentPromptArgv(agentId, prompt, opts = {}) {
  const argv = ["agent", "prompt", agentId, prompt];
  return opts.wait ? [...argv, "--wait"] : argv;
}
function reasonHeadline(reason) {
  return reason.split(`
`, 1)[0] ?? reason;
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
    if (!Array.isArray(agents))
      return null;
    return agents.flatMap((a) => typeof a?.name === "string" ? [{ id: a.name, status: typeof a.agent_status === "string" ? a.agent_status : undefined }] : []);
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
    return typeof val === "string" ? val : undefined;
  } catch {
    return;
  }
}
function classifyWaitResult(exitCode, stdout, stderr) {
  if (exitCode === 0) {
    const status = parseHerdrJson(stdout)?.result?.agent?.agent_status;
    if (status === "blocked")
      return "blocked";
    if (status === "idle" || status === "done")
      return "finished";
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
  if (status === "working" || status === "idle" || status === "done")
    return { action: "unpark" };
  return { action: "keep" };
}
function classifyTimeoutProbe(probe, elapsedMs, deadlineMs) {
  const overBudget = elapsedMs !== null && elapsedMs >= deadlineMs;
  const inconclusive = () => overBudget ? { disposition: "event", kind: "timed_out", livenessConfirmed: false } : { disposition: "rearm" };
  if (probe.abandoned)
    return inconclusive();
  if (probe.code !== 0) {
    const code = parseHerdrJson(probe.stderr)?.error?.code;
    if (code === "agent_not_found")
      return { disposition: "event", kind: "error" };
    return inconclusive();
  }
  const status = parseHerdrJson(probe.stdout)?.result?.agent?.agent_status;
  if (status === "blocked")
    return { disposition: "event", kind: "blocked" };
  if (status === "idle" || status === "done")
    return { disposition: "event", kind: "finished" };
  if (status === undefined)
    return inconclusive();
  return overBudget ? { disposition: "event", kind: "timed_out", livenessConfirmed: true } : { disposition: "rearm" };
}
function workerWorktreePath(cwd, slug) {
  if (!cwd)
    return null;
  return join(dirname(cwd), `${basename(cwd)}-${slug}`);
}
function deadlineStopDetail(worker, deadlineMs, opts) {
  const minutes = Math.round(deadlineMs / 60000);
  const lines = opts.livenessConfirmed ? [
    `worker budget of ${minutes} min of working time elapsed while the agent still reported working -- stopped deliberately.`
  ] : [
    `worker budget of ${minutes} min of working time elapsed, and its liveness could NOT be verified: ${opts.probeDetail ?? "the probe gave no usable answer"}.`,
    "It may have been working, or may have died earlier -- this stop is on the budget, not on evidence about the worker."
  ];
  lines.push("", `The item is very likely still in-progress with a live claim: python3 ~/.claude/scripts/dev_status.py show ${worker.slug}`);
  const worktree = workerWorktreePath(worker.cwd, worker.slug);
  if (worktree) {
    lines.push(`Its worktree survives on disk. Worker cwd was ${worker.cwd}; by the <repo>-<slug> convention that makes the worktree ${worktree} (derived from the cwd, not verified).`, `Recover with: git -C ${worker.cwd} worktree remove --force ${worktree}, then reset the item to open to clear the claim.`);
  } else {
    lines.push("This worker predates cwd tracking, so its worktree path cannot be named here -- find it with git worktree list.");
  }
  return lines.join(`
`);
}
function paneIdentityMismatch(getExitCode, getStdout, expectedPaneId) {
  if (getExitCode !== 0)
    return true;
  const reportedPaneId = parseHerdrJson(getStdout)?.result?.agent?.pane_id;
  return reportedPaneId !== expectedPaneId;
}
function waitResultDetail(stdout, stderr) {
  const err = parseHerdrJson(stderr)?.error;
  if (err)
    return `${err.code ?? "unknown"}: ${err.message ?? stderr.trim()}`;
  return stdout.trim() || stderr.trim() || "(no output)";
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
  const isWordChar = (c) => c !== undefined && /[a-z0-9]/.test(c);
  let from = 0;
  for (;; ) {
    const at = haystack.indexOf(needle, from);
    if (at === -1)
      return false;
    if (!isWordChar(haystack[at - 1]) && !isWordChar(haystack[at + needle.length])) {
      return true;
    }
    from = at + 1;
  }
}
function matchOption(answer, options) {
  const candidates = options.filter((o) => o.label.toLowerCase() !== OTHER_OPTION_LABEL.toLowerCase());
  const needle = answer.trim().toLowerCase();
  if (!needle)
    return null;
  const exact = candidates.filter((o) => o.label.toLowerCase() === needle);
  if (exact.length === 1)
    return exact[0];
  if (needle.length < MIN_PARTIAL_ANSWER)
    return null;
  const partial = candidates.filter((o) => containsAsWord(o.label.toLowerCase(), needle));
  if (partial.length === 1)
    return partial[0];
  const strippedNeedle = needle.replace(/\s+/g, "");
  const stripped = candidates.filter((o) => o.label.toLowerCase().replace(/\s+/g, "").includes(strippedNeedle));
  if (stripped.length === 1)
    return stripped[0];
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
var DEFAULT_WAIT_TIMEOUT_MS = 30 * 60 * 1000;
var DEFAULT_WORKER_DEADLINE_MS = 4 * 60 * 60 * 1000;
var DEFAULT_RELAY_STALL_MS = 30 * 60 * 1000;
var PROBE_TIMEOUT_MS = 15000;
var RESOLVE_VERIFY_TIMEOUT_MS = 5000;
var BLOCKED_READ_LINES = 500;
var BLOCKED_READ_LINES_RETRY = 2000;
var PANE_CAPTURE_CHARS = 4000;
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
  } catch {}
  try {
    const parsed = JSON.parse(raw);
    const offers = parsed && typeof parsed === "object" && "offers" in parsed ? parsed.offers : null;
    if (!Array.isArray(offers))
      return [];
    return offers.flatMap((o) => {
      if (!o || typeof o !== "object")
        return [];
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
function renderCaptureOffers(offers) {
  if (offers.length === 0)
    return "";
  return `
  Queued capture offers from this worker -- do NOT ask about them now; fold them into your single end-of-run digest walk: ` + offers.map((c) => `[${c.kind}] ${c.id} -- ${c.summary}`).join("; ");
}
function loadState(runId, stateDir = herdrStateDir()) {
  const path = statePath(runId, stateDir);
  if (!existsSync(path))
    return null;
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
function formatDuration(ms) {
  const totalMinutes = Math.max(0, Math.round(ms / 60000));
  const hours = Math.floor(totalMinutes / 60);
  const minutes = totalMinutes % 60;
  return hours > 0 ? `${hours}h${String(minutes).padStart(2, "0")}m` : `${minutes}m`;
}
function looksTruncated(content, requestedLines) {
  return content.split(`
`).length >= requestedLines;
}
function elapsedWorkingMs(worker, now) {
  const open = worker.workingSinceMs === undefined ? null : Math.max(0, now - worker.workingSinceMs);
  if (open === null && worker.accumulatedWorkingMs === undefined)
    return null;
  return (worker.accumulatedWorkingMs ?? 0) + (open ?? 0);
}
function foldWorkingSegment(worker, now) {
  if (worker.workingSinceMs === undefined)
    return;
  worker.accumulatedWorkingMs = (worker.accumulatedWorkingMs ?? 0) + Math.max(0, now - worker.workingSinceMs);
  worker.workingSinceMs = undefined;
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
      if (timer)
        clearTimeout(timer);
      resolve({ code: 1, stdout, stderr: `${stderr}
${String(err)}` });
    });
    proc.on("close", (code) => {
      if (timer)
        clearTimeout(timer);
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

class SwarmToolContext {
  picker;
  options;
  activeRuns = new Map;
  runtimes = new Map;
  exec;
  constructor(exec = defaultExec, picker = copilotPickerAdapter, options = {}) {
    this.picker = picker;
    this.options = options;
    this.exec = exec;
  }
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
  async herdr(argv, signal) {
    return this.exec("herdr", argv, { signal });
  }
  getRuntime(runId) {
    let rt = this.runtimes.get(runId);
    if (!rt) {
      rt = {
        runId,
        inFlight: new Set,
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
        if (i !== -1)
          rt.waiters.splice(i, 1);
        signal?.removeEventListener("abort", onAbort);
      };
      const wake = () => {
        if (settled)
          return;
        settled = true;
        cleanup();
        resolve(true);
      };
      const onAbort = () => {
        if (settled)
          return;
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
    for (const wake of waiting)
      wake();
  }
  async closeWorker(worker, signal) {
    try {
      await this.herdr(buildWorkerCloseArgv(worker), signal);
    } catch {}
  }
  async harvestWorkerIO(state, worker, signal) {
    const offers = readCaptureOffers(state.runId, worker.slug, this.stateDir);
    await this.closeWorker(worker, signal);
    try {
      rmSync(capturePath(state.runId, worker.slug, this.stateDir), { force: true });
    } catch {}
    return offers;
  }
  async teardownAndHarvestWorker(state, worker, signal) {
    const offers = await this.harvestWorkerIO(state, worker, signal);
    state.workers = state.workers.filter((w) => w.agent !== worker.agent);
    this.persist(state);
    return offers;
  }
  async pruneStaleWorkers(state) {
    let listResult;
    for (let attempt = 0;attempt < 2; attempt++) {
      try {
        listResult = await this.herdr(buildAgentListArgv());
      } catch {
        listResult = undefined;
      }
      if (listResult && listResult.code === 0 && parseAgentList(listResult.stdout) !== null)
        break;
    }
    const entries = listResult && listResult.code === 0 ? parseAgentList(listResult.stdout) : null;
    if (entries === null)
      return [];
    const stale = staleWorkerRecords(state, entries, Date.now());
    if (stale.length === 0)
      return [];
    const staleAgents = new Set(stale.map((w) => w.agent));
    state.workers = state.workers.filter((w) => !staleAgents.has(w.agent));
    const rt = this.runtimes.get(state.runId);
    if (rt) {
      for (const w of stale)
        rt.inFlight.delete(w.agent);
      rt.pendingEvents = rt.pendingEvents.filter((e) => !staleAgents.has(e.agent));
      this.wakeWaiters(rt);
    }
    const lines = await Promise.all(stale.map(async (w) => {
      const status = entries.find((e) => e.id === w.agent)?.status;
      const offers = await this.teardownAndHarvestWorker(state, w);
      return `${w.agent} (${w.slug}): stale worker record cleared -- herdr reports its agent ${status ?? "gone"} (finished or dead), but its finish was never reported through ` + "swarm_poll; outcome inferred, not observed. Verify the item's state before " + `treating it as complete.${renderCaptureOffers(offers)}`;
    }));
    this.persist(state);
    return lines;
  }
  async recoverTabByLabel(label) {
    try {
      const listing = await this.herdr(buildTabListArgv());
      if (listing.code !== 0)
        return;
      const tabId = findTabByLabel(listing.stdout, label);
      if (!tabId)
        return;
      const closed = await this.herdr(buildTabCloseArgv(tabId));
      return closed.code === 0 ? tabId : undefined;
    } catch {
      return;
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
    } catch {}
    return { slug, failed: { slug, reason: `${reason}
--- pane ${paneId} ---
${capture}` } };
  }
  async spawnInto(paneId, tabId, agentId, slug, paths, model, pluginDir) {
    let sessionId = this.kind === "copilot" ? randomUUID() : undefined;
    const startResult = await this.herdr(buildAgentStartArgv(agentId, paneId, model, {
      kind: this.kind,
      sessionId,
      ...this.kind === "copilot" ? { allowAllTools: true, pluginDir: pluginDir ?? this.defaultPluginDir() } : {}
    }));
    if (startResult.code !== 0) {
      return this.failWithTab(slug, paneId, tabId, `agent_not_ready: ${startResult.stderr || startResult.stdout}`);
    }
    if (sessionId)
      try {
        const getResult = await this.herdr(buildAgentGetArgv(agentId));
        if (getResult.code === 0) {
          const sessionVal = parseAgentSession(getResult.stdout);
          if (sessionVal && sessionVal !== sessionId) {
            process.stderr.write(`[swarm] ${agentId}: requested session-id ${sessionId} but herdr agent get ` + `reports ${sessionVal} -- using the confirmed value for crash recovery
`);
            sessionId = sessionVal;
          }
        }
      } catch {}
    const promptResult = await this.herdr(buildAgentPromptArgv(agentId, this.workerPrompt(slug)));
    if (promptResult.code !== 0) {
      return this.failWithTab(slug, paneId, tabId, `agent_prompt_stalled: ${promptResult.stderr || promptResult.stdout}`);
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
    if (this.kind !== "copilot" || attempts >= MAX_RECOVERY_ATTEMPTS)
      return false;
    if (!worker.copilotSessionId || !isValidUuid(worker.copilotSessionId))
      return false;
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
      } catch {}
    }
    const startResult = await this.herdr(buildAgentStartArgv(worker.agent, parsedTab.paneId, worker.model, {
      kind: "copilot",
      resumeSessionId: worker.copilotSessionId,
      allowAllTools: true,
      pluginDir: pluginDir ?? this.defaultPluginDir()
    }));
    if (startResult.code !== 0) {
      try {
        await this.herdr(buildTabCloseArgv(parsedTab.tabId));
      } catch {}
      return false;
    }
    const promptResult = await this.herdr(buildAgentPromptArgv(worker.agent, "Continue working on this backlog item where you left off."));
    if (promptResult.code !== 0) {}
    worker.paneId = parsedTab.paneId;
    worker.tabId = parsedTab.tabId;
    worker.recoveryAttempts = attempts + 1;
    worker.workingSinceMs = Date.now();
    this.persist(state);
    return true;
  }
  async getOrInitState(runId, concurrency, prefix, pluginDir) {
    const cached = this.activeRuns.get(runId);
    if (cached)
      return cached;
    const loaded = loadState(runId, this.stateDir);
    if (!loaded) {
      const fresh = {
        runId,
        concurrency,
        nextCounter: 0,
        workers: [],
        ...prefix !== undefined ? { prefix } : {},
        ...pluginDir !== undefined ? { pluginDir } : {}
      };
      this.activeRuns.set(runId, fresh);
      return fresh;
    }
    const listResult = await this.herdr(buildAgentListArgv());
    const entries = listResult.code === 0 ? parseAgentList(listResult.stdout) : null;
    const liveIds = (entries ?? []).map((e) => e.id);
    if (this.kind === "copilot" && entries !== null) {
      const missing = loaded.workers.filter((w) => !liveIds.includes(w.agent));
      const recoveries = await Promise.all(missing.map(async (w) => ({
        agent: w.agent,
        recovered: await this.attemptCrashRecovery(loaded, w, loaded.pluginDir)
      })));
      for (const { agent, recovered } of recoveries) {
        if (recovered)
          liveIds.push(agent);
      }
    }
    const reconciled = entries === null ? loaded : reconcileState(loaded, liveIds).state;
    this.activeRuns.set(runId, reconciled);
    saveState(reconciled, this.stateDir);
    return reconciled;
  }
  persist(state) {
    this.activeRuns.set(state.runId, state);
    saveState(state, this.stateDir);
  }
  async probeLiveness(agentId) {
    const controller = new AbortController;
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
    if (rt.inFlight.has(worker.agent))
      return;
    rt.inFlight.add(worker.agent);
    const timeoutMs = rt.timeoutMs;
    this.settleWait(rt, worker, timeoutMs);
  }
  async settleWait(rt, worker, timeoutMs) {
    let event = null;
    try {
      const result = await this.herdr(buildAgentWaitArgv(worker.agent, ["idle", "done", "blocked"], timeoutMs));
      let kind = classifyWaitResult(result.code, result.stdout, result.stderr);
      let detail = kind === "timed_out" || kind === "error" ? waitResultDetail(result.stdout, result.stderr) : undefined;
      if (kind === "timed_out") {
        const probe = await this.probeLiveness(worker.agent);
        const verdict = classifyTimeoutProbe(probe, this.elapsedWorkingMsFor(worker), rt.deadlineMs);
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
        detail = kind === "timed_out" ? deadlineStopDetail(worker, rt.deadlineMs, {
          livenessConfirmed: verdict.livenessConfirmed === true,
          probeDetail: probe.abandoned ? `the liveness probe did not answer within ${PROBE_TIMEOUT_MS} ms and was abandoned` : `probe: ${waitResultDetail(probe.stdout, probe.stderr)}`
        }) : kind === "error" ? `probe: ${waitResultDetail(probe.stdout, probe.stderr)}` : undefined;
      }
      event = { kind, agent: worker.agent, slug: worker.slug, paneId: worker.paneId };
      if (detail !== undefined)
        event.detail = detail;
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
    rt.pendingEvents.push(event);
    this.wakeWaiters(rt);
  }
  elapsedWorkingMsFor(worker) {
    const now = Date.now();
    if (worker.workingSinceMs === undefined && worker.accumulatedWorkingMs === undefined) {
      worker.workingSinceMs = now;
    }
    return elapsedWorkingMs(worker, now);
  }
  async swarmSpawn(params) {
    if (!params.items && !params.prefix) {
      throw new Error("swarm_spawn needs either `items` or `prefix`. Selecting from the whole READY queue " + "unscoped would pull unrelated projects into this run.");
    }
    return this.withSpawnLock(params.runId, async () => {
      const state = await this.getOrInitState(params.runId, params.concurrency ?? DEFAULT_CONCURRENCY, params.prefix, params.pluginDir);
      if (params.concurrency !== undefined)
        state.concurrency = params.concurrency;
      if (params.pluginDir !== undefined)
        state.pluginDir = params.pluginDir;
      const pruneLines = await this.pruneStaleWorkers(state);
      if (!canSpawnNew(state)) {
        return {
          content: [
            {
              type: "text",
              text: `Concurrency cap reached (${state.concurrency} active workers). ` + "Call swarm_poll to wait for workers to settle."
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
              text: `Open-pane soft cap reached (${openPaneCount(state)}/${openPaneSoftCap(state.concurrency)} open panes). ` + "Parked workers awaiting a relay are holding panes -- answer each with swarm_resolve_blocked before spawning more."
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
          throw new Error(`dev_status.py ready failed: ${readyResult.stderr || readyResult.stdout}`);
        }
        const parsed = parseReadyItems(readyResult.stdout);
        const byId = new Map(parsed.map((i) => [i.id, i]));
        candidates = explicitSlugs.map((id) => byId.get(id) ?? { id });
      } else {
        const readyResult = await this.exec("python3", buildReadyArgv(params.prefix).slice(1), {
          timeout: PROBE_TIMEOUT_MS
        });
        if (readyResult.code !== 0) {
          throw new Error(`dev_status.py ready failed: ${readyResult.stderr || readyResult.stdout}`);
        }
        candidates = parseReadyItems(readyResult.stdout);
        const attempted = new Set(state.attempted ?? []);
        candidates = candidates.filter((c) => !attempted.has(c.id));
      }
      const takenPaths = [];
      for (const w of state.workers) {
        for (const p of w.paths ?? []) {
          takenPaths.push({ path: p, holder: `worker ${w.agent} (${w.slug}, ${w.lifecycle})` });
        }
      }
      const budget = spawnBudget(state, candidates.length);
      const selection = selectSchedulable(candidates, takenPaths, budget);
      const toSpawn = selection.slugs;
      const spawned = [];
      const failed = [];
      const tabs = [];
      for (const slug of toSpawn) {
        const captureFile = capturePath(state.runId, slug, this.stateDir);
        const tabCreated = await this.herdr(buildTabCreateArgv(process.cwd(), slug, { captureFile, kind: this.kind }));
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
      const startResults = await Promise.allSettled(tabs.map(async (t) => {
        const candidateItem = candidates.find((c) => c.id === t.slug);
        const paths = candidateItem ? itemPaths(candidateItem) : [];
        const agentId = nextAgentId(state.runId, state.nextCounter++, t.slug);
        const { paneId, tabId } = t.created;
        try {
          return await this.spawnInto(paneId, tabId, agentId, t.slug, paths, params.model, params.pluginDir);
        } catch (e) {
          return this.failWithTab(t.slug, paneId, tabId, `spawn_error: ${String(e)}`);
        }
      }));
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
      state.attempted = [...new Set([...state.attempted ?? [], ...toSpawn])];
      this.persist(state);
      const rt = this.getRuntime(state.runId);
      for (const w of spawned)
        this.armWait(rt, w);
      const skipped = selection.skipped;
      const deferred = selection.deferred;
      const refused = selection.refused;
      const parts = [
        `Spawned ${spawned.length} worker(s)`,
        `${failed.length} failed to spawn`,
        `${skipped.length} skipped (cap)`,
        `${deferred.length} deferred (file overlap)`,
        `${refused.length} refused (not worker-safe)`
      ];
      const lines = [`${parts.join(", ")}.`];
      lines.push(...pruneLines);
      for (const f of failed)
        lines.push(`- ${f.slug}: ${reasonHeadline(f.reason)}`);
      for (const d of deferred)
        lines.push(`- ${d.slug}: deferred -- ${d.reason}`);
      for (const r of refused)
        lines.push(`- ${r.slug}: refused -- ${r.reason}`);
      if (spawned.length === 0 && deferred.length > 0) {
        lines.push("Nothing spawned but items remain: poll the running workers, then call swarm_spawn again once one finishes.");
      } else if (spawned.length === 0 && refused.length > 0) {
        lines.push("Nothing spawned and the remaining items are refused, not waiting: they are never schedulable by a worker. " + "This is the end of the swarm phase for this prefix -- report them as needing a normal session rather than polling or spawning again.");
      }
      return {
        content: [{ type: "text", text: lines.join(`
`) }],
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
      const gets = await Promise.all(parkedNow.map(async (worker) => {
        try {
          const r = await this.herdr(buildAgentGetArgv(worker.agent), signal);
          return { worker, verdict: classifyResyncGet(r.code, r.stdout, r.stderr) };
        } catch {
          return { worker, verdict: { action: "keep" } };
        }
      }));
      const resumedAt = Date.now();
      for (const { worker, verdict } of gets) {
        if (verdict.action === "drop") {
          rt.inFlight.delete(worker.agent);
          rt.pendingEvents.push({
            kind: "finished",
            agent: worker.agent,
            slug: worker.slug,
            paneId: worker.paneId,
            detail: "resync: agent gone from herdr while its record said awaiting_relay -- " + "its gate was likely answered out-of-band (direct pane keys) and the " + "worker has since finished or exited; outcome inferred, not observed. " + "Verify the item's state before treating it as complete."
          });
        } else if (verdict.action === "unpark") {
          worker.workingSinceMs = resumedAt;
          worker.awaitingRelaySinceMs = undefined;
          worker.lastResolveFailure = undefined;
          worker.lifecycle = "active";
          resyncNotes.push(`${worker.agent} (${worker.slug}) was parked awaiting a relay, but herdr now reports it unblocked -- resumed tracking as active.`);
        }
      }
      if (resyncNotes.length > 0)
        this.persist(state);
    }
    const stampNow = Date.now();
    for (const w of state.workers) {
      if (w.lifecycle === "awaiting_relay" && w.awaitingRelaySinceMs === undefined) {
        w.awaitingRelaySinceMs = stampNow;
      }
    }
    const active = state.workers.filter((w) => w.lifecycle === "active");
    for (const w of active)
      this.armWait(rt, w);
    if (active.length === 0 && rt.pendingEvents.length === 0) {
      const goneNoteLines = await this.pruneStaleWorkers(state);
      const awaitingRelay = state.workers.filter((w) => w.lifecycle === "awaiting_relay");
      const stalledHere = stalledRelayWorkers(state.workers, Date.now(), rt.stallMs);
      const stalledAgents = new Set(stalledHere.map((w) => w.agent));
      const describe = (w) => `${w.agent} (${w.slug}, pane ${w.paneId})` + (stalledAgents.has(w.agent) ? ` -- STALLED, over ${formatDuration(rt.stallMs)} with no answer` : "") + (w.lastResolveFailure ? ` -- a previous answer ${JSON.stringify(w.lastResolveFailure.answer)} failed to land (${w.lastResolveFailure.reason}); re-read the pane and answer with its EXACT rendered label` : "");
      const text = (awaitingRelay.length ? `No active workers to poll. ${awaitingRelay.length} worker(s) awaiting a relay -- answer each with swarm_resolve_blocked before polling again: ${awaitingRelay.map(describe).join(", ")}.` : "No active workers to poll.") + (goneNoteLines.length ? `

${goneNoteLines.join(`
`)}` : "");
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
    const toRemove = new Set;
    const processed = await Promise.all(rawEvents.map(async (event) => {
      const worker = workersByAgent.get(event.agent);
      if (!worker)
        return null;
      if (event.kind === "blocked") {
        let getResult;
        try {
          getResult = await this.herdr(buildAgentGetArgv(event.agent), signal);
        } catch {
          getResult = { code: 1, stdout: "", stderr: "" };
        }
        const resyncVerdict = classifyResyncGet(getResult.code, getResult.stdout, getResult.stderr);
        if (resyncVerdict.action === "drop") {
          event.kind = "finished";
          event.detail = "resync: agent gone from herdr while resolving blocked prompt -- " + "worker has since finished or exited; outcome inferred, not observed. " + "Verify the item's state before treating it as complete.";
          event.captures = await this.harvestWorkerIO(state, worker, signal);
          toRemove.add(worker.agent);
          return event;
        }
        let readResult;
        try {
          readResult = await this.herdr(buildAgentReadArgv(event.agent, BLOCKED_READ_LINES), signal);
        } catch {
          readResult = { code: 1, stdout: "", stderr: "" };
        }
        let truncated = looksTruncated(readResult.stdout, BLOCKED_READ_LINES);
        if (truncated) {
          try {
            readResult = await this.herdr(buildAgentReadArgv(event.agent, BLOCKED_READ_LINES_RETRY), signal);
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
      } else if (event.kind === "still_working") {} else {
        event.captures = await this.harvestWorkerIO(state, worker, signal);
        toRemove.add(worker.agent);
      }
      return event;
    }));
    const events = processed.filter((e) => e !== null);
    if (toRemove.size > 0) {
      state.workers = state.workers.filter((w) => !toRemove.has(w.agent));
    }
    this.persist(state);
    await Promise.all(events.filter((e) => e.kind === "finished").map(async (event) => {
      try {
        const result = await this.exec("python3", buildShowArgv(event.slug).slice(1), {
          signal,
          timeout: PROBE_TIMEOUT_MS
        });
        if (result.code !== 0)
          return;
        const shown = parseShownItem(result.stdout);
        if (shown === null || event.detail !== undefined)
          return;
        if (isSuspiciousFinish(shown.status, (event.captures ?? []).length)) {
          event.detail = `dev_status.py still shows status ${JSON.stringify(shown.status)} and zero ` + "captures were queued during this run -- verify the item's actual state " + "before treating this as complete.";
        }
      } catch {}
    }));
    const stalled = stalledRelayWorkers(state.workers, Date.now(), rt.stallMs);
    const stalledNote = stalled.length ? `

${stalled.length} worker(s) have been awaiting a relay for over ${formatDuration(rt.stallMs)} and are not progressing -- each needs a human answer in its own pane: ${stalled.map((w) => `${w.agent} (${w.slug}, pane ${w.paneId})`).join(", ")}.` : "";
    const resyncNote = resyncNotes.length ? `

${resyncNotes.join(" ")}` : "";
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
            return `${e.slug} (${e.agent}) ${e.kind}${e.detail ? `: ${e.detail}` : ""}${captures}`;
          }).join(`

`) : "No active workers to poll.") + stalledNote + resyncNote
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
            text: `amend_refused: ${worker.agent} (${worker.slug}, pane ${worker.paneId}) is parked at a gate, ` + "and herdr agent prompt refuses a blocked agent -- nothing was sent. Answer it with " + "swarm_resolve_blocked first, then amend, or amend after it finishes and pick the item up again."
          }
        ],
        details: { amended: false, slug: worker.slug, paneId: worker.paneId }
      };
    }
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
    this.persist(state);
    return {
      content: [
        {
          type: "text",
          text: `amended: ${worker.agent} (${worker.slug}, pane ${worker.paneId}) was told to re-read its item. ` + "Nothing confirms it has done so -- the instruction lands as its next input, which is a correction " + "while it is still planning and a rewrite of finished work if it is not. Watch its next poll event, " + "and say in the end-of-run digest that this item was amended mid-flight."
        }
      ],
      details: { amended: true, slug: worker.slug, paneId: worker.paneId }
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
      const readResult = await this.herdr(buildAgentReadArgv(params.agent, BLOCKED_READ_LINES), signal);
      rawPrompt = readResult.stdout;
    } catch {}
    noteResolveFailure(worker, params.answer, "copilot workers require manual response", Date.now());
    this.persist(state);
    return {
      content: [
        {
          type: "text",
          text: `needs_manual: Copilot workers do not have a programmatic picker -- manual input required for ` + `${params.agent} (${worker.slug}, pane ${worker.paneId}). ` + `Attach directly (herdr agent attach ${params.agent}) or switch to pane ${worker.paneId} to respond.` + (rawPrompt ? `
Captured prompt:
${rawPrompt.slice(-2000)}` : "")
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
    if (!target || picker.selectedIndex === null)
      return manual("no listed option matched");
    const identity = await this.herdr(buildAgentGetArgv(worker.agent), signal);
    if (paneIdentityMismatch(identity.code, identity.stdout, worker.paneId))
      return manual("pane identity mismatch");
    const keys = await this.herdr(buildAgentSendKeysArgv(worker.agent, navigationKeys(picker.selectedIndex, target.index)), signal);
    if (keys.code !== 0)
      return this.relayFailure(state, worker, `could not send navigation keys to ${worker.agent}: ${keys.stderr || keys.stdout}`, signal);
    const verify = await this.herdr(buildAgentWaitArgv(worker.agent, ["idle", "done", "working"], RESOLVE_VERIFY_TIMEOUT_MS), signal);
    if (verify.code !== 0)
      return this.relayFailure(state, worker, `${worker.agent} did not resume within ${RESOLVE_VERIFY_TIMEOUT_MS} ms after "${target.label}" was submitted.`, signal);
    worker.workingSinceMs = Date.now();
    worker.awaitingRelaySinceMs = undefined;
    worker.lastResolveFailure = undefined;
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
    return {
      content: [{ type: "text", text: `relay_failed: ${reason}${renderCaptureOffers(captures)}` }],
      details: {
        relayFailed: true,
        needsManual: false,
        slug: worker.slug,
        paneId: worker.paneId,
        captures
      }
    };
  }
}
export {
  PANE_CAPTURE_CHARS,
  SwarmToolContext,
  buildReadyArgv,
  buildShowArgv,
  capturePath,
  copilotPluginDir,
  defaultExec,
  devStatusPath,
  elapsedWorkingMs,
  foldWorkingSegment,
  formatDuration,
  herdrStateDir,
  isValidUuid,
  loadState,
  looksTruncated,
  readCaptureOffers,
  reconcileState,
  renderCaptureOffers,
  saveState,
  statePath
};

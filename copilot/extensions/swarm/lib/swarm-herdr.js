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
function parseAgentListIds(stdout) {
  return (parseAgentList(stdout) ?? []).map((a) => a.id);
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
export {
  AMEND_INSTRUCTION,
  WORKER_UNATTENDED_ENV,
  buildAgentGetArgv,
  buildAgentListArgv,
  buildAgentPromptArgv,
  buildAgentReadArgv,
  buildAgentSendKeysArgv,
  buildAgentStartArgv,
  buildAgentWaitArgv,
  buildPaneCloseArgv,
  buildPaneReadArgv,
  buildTabCloseArgv,
  buildTabCreateArgv,
  buildTabListArgv,
  buildWorkerCloseArgv,
  classifyResyncGet,
  classifyTimeoutProbe,
  classifyWaitResult,
  deadlineStopDetail,
  findTabByLabel,
  paneIdentityMismatch,
  parseAgentList,
  parseAgentListIds,
  parseAgentSession,
  parseTabCreate,
  reasonHeadline,
  tabPresence,
  waitResultDetail,
  workerWorktreePath
};

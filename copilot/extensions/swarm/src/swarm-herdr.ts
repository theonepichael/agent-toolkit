// herdr protocol helpers for Copilot swarm: argv construction (every element a discrete
// argv item, never a concatenated shell string) and response
// interpretation for tab/agent/pane commands. Vendored and kind-parameterized for Copilot.
import { basename, dirname, join } from "node:path";
import type { WorkerRecord } from "./swarm-scheduling";

const AGENT_START_TIMEOUT_MS = 30_000;

export type PollEventKind = "blocked" | "finished" | "timed_out" | "error" | "still_working";

export interface TabCreateOptions {
  captureFile?: string;
  kind?: "pi" | "copilot";
}

export function buildTabCreateArgv(cwd: string, label: string, opts?: TabCreateOptions): string[] {
  const kind = opts?.kind ?? "copilot";
  const captureFile = opts?.captureFile;
  const envArgs: string[] = [];
  if (kind === "pi") {
    envArgs.push("--env", WORKER_UNATTENDED_ENV);
  }
  if (captureFile) {
    const varName = kind === "copilot" ? "COPILOT_SWARM_CAPTURE_FILE" : "PI_SWARM_CAPTURE_FILE";
    envArgs.push("--env", `${varName}=${captureFile}`);
  }
  return [
    "tab",
    "create",
    "--cwd",
    cwd,
    "--label",
    label,
    ...envArgs,
    "--no-focus",
  ];
}

export function buildTabCloseArgv(tabId: string): string[] {
  return ["tab", "close", tabId];
}

export function buildTabListArgv(): string[] {
  return ["tab", "list"];
}

export function findTabByLabel(stdout: string, label: string): string | undefined {
  try {
    const parsed = JSON.parse(stdout) as {
      result?: { tabs?: { tab_id?: unknown; label?: unknown }[] };
    };
    const matches = (parsed.result?.tabs ?? []).filter(
      (t) => t.label === label && typeof t.tab_id === "string",
    );
    return matches.length === 1 ? (matches[0]!.tab_id as string) : undefined;
  } catch {
    return undefined;
  }
}

export interface TabCreateResult {
  paneId: string;
  tabId: string;
}

export function parseTabCreate(stdout: string): TabCreateResult | undefined {
  try {
    const parsed = JSON.parse(stdout) as {
      result?: { root_pane?: { pane_id?: string }; tab?: { tab_id?: string } };
    };
    const paneId = parsed.result?.root_pane?.pane_id;
    const tabId = parsed.result?.tab?.tab_id;
    if (typeof paneId !== "string" || typeof tabId !== "string") return undefined;
    return { paneId, tabId };
  } catch {
    return undefined;
  }
}

export interface AgentStartOptions {
  kind?: "pi" | "copilot";
  sessionId?: string;
  allowAllTools?: boolean;
  pluginDir?: string;
  resumeSessionId?: string;
}

export function buildAgentStartArgv(
  agentId: string,
  paneId: string,
  model?: string,
  opts?: AgentStartOptions,
): string[] {
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
    String(AGENT_START_TIMEOUT_MS),
  ];

  if (kind === "copilot") {
    const sessionArgs: string[] = [];
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

export const WORKER_UNATTENDED_ENV = "PI_AGENT_UNATTENDED=1";

export const AMEND_INSTRUCTION =
  "STOP and re-read your backlog item before doing anything else: run " +
  "`python3 ~/.claude/scripts/dev_status.py show <your slug>` and read the " +
  "whole record fresh. Its context or next_steps have been corrected since " +
  "you started, so any plan you formed from the earlier version may now be " +
  "wrong. Reconcile what you have already done against the updated record, " +
  "and say plainly what changes as a result before continuing.";

export function buildAgentPromptArgv(
  agentId: string,
  prompt: string,
  opts: { wait?: boolean } = {},
): string[] {
  const argv = ["agent", "prompt", agentId, prompt];
  return opts.wait ? [...argv, "--wait"] : argv;
}

export function reasonHeadline(reason: string): string {
  return reason.split("\n", 1)[0] ?? reason;
}

export function buildAgentSendKeysArgv(agentId: string, keys: readonly string[]): string[] {
  return ["agent", "send-keys", agentId, ...keys];
}

export function buildAgentWaitArgv(
  agentId: string,
  until: readonly string[],
  timeoutMs: number,
): string[] {
  return [
    "agent",
    "wait",
    agentId,
    ...until.flatMap((state) => ["--until", state]),
    "--timeout",
    String(timeoutMs),
  ];
}

export function buildAgentGetArgv(agentId: string): string[] {
  return ["agent", "get", agentId];
}

export function buildAgentReadArgv(agentId: string, lines: number): string[] {
  return ["agent", "read", agentId, "--source", "recent-unwrapped", "--lines", String(lines)];
}

export function buildPaneCloseArgv(paneId: string): string[] {
  return ["pane", "close", paneId];
}

export function buildWorkerCloseArgv(worker: WorkerRecord): string[] {
  return worker.tabId ? buildTabCloseArgv(worker.tabId) : buildPaneCloseArgv(worker.paneId);
}

export function buildPaneReadArgv(paneId: string, lines: number): string[] {
  return ["pane", "read", paneId, "--source", "recent-unwrapped", "--lines", String(lines)];
}

export function buildAgentListArgv(): string[] {
  return ["agent", "list"];
}

export function parseAgentList(stdout: string): { id: string; status?: string }[] | null {
  try {
    const parsed = JSON.parse(stdout) as {
      result?: { agents?: { name?: string; agent_status?: string }[] };
    };
    const agents = parsed?.result?.agents;
    if (!Array.isArray(agents)) return null;
    return agents.flatMap((a) =>
      typeof a?.name === "string"
        ? [{ id: a.name, status: typeof a.agent_status === "string" ? a.agent_status : undefined }]
        : [],
    );
  } catch {
    return null;
  }
}

export function parseAgentListIds(stdout: string): string[] {
  return (parseAgentList(stdout) ?? []).map((a) => a.id);
}

interface HerdrEnvelope {
  result?: {
    agent?: { agent_status?: string; pane_id?: string; agent_session?: { kind?: string; value?: unknown } };
    agents?: { agent_status?: string; agent_session?: { kind?: string; value?: unknown }; pane_id?: string; name?: string }[];
  };
  error?: { code?: string; message?: string };
}

function parseHerdrJson(text: string): HerdrEnvelope | null {
  try {
    return JSON.parse(text) as HerdrEnvelope;
  } catch {
    return null;
  }
}

export function parseAgentSession(stdout: string): string | undefined {
  try {
    const parsed = parseHerdrJson(stdout);
    const val = parsed?.result?.agent?.agent_session?.value;
    return typeof val === "string" ? val : undefined;
  } catch {
    return undefined;
  }
}

export function classifyWaitResult(
  exitCode: number,
  stdout: string,
  stderr: string,
): PollEventKind {
  if (exitCode === 0) {
    const status = parseHerdrJson(stdout)?.result?.agent?.agent_status;
    if (status === "blocked") return "blocked";
    if (status === "idle" || status === "done") return "finished";
    return "error";
  }
  const code = parseHerdrJson(stderr)?.error?.code;
  return code === "timeout" ? "timed_out" : "error";
}

export type ResyncVerdict = { action: "drop" } | { action: "unpark" } | { action: "keep" };

export function classifyResyncGet(exitCode: number, stdout: string, stderr: string): ResyncVerdict {
  if (exitCode !== 0) {
    const code = parseHerdrJson(stderr)?.error?.code;
    return code === "agent_not_found" ? { action: "drop" } : { action: "keep" };
  }
  const status = parseHerdrJson(stdout)?.result?.agent?.agent_status;
  if (status === "working" || status === "idle" || status === "done") return { action: "unpark" };
  return { action: "keep" };
}

export type TimeoutVerdict =
  | { disposition: "rearm" }
  | { disposition: "event"; kind: PollEventKind; livenessConfirmed?: boolean };

export interface ProbeResult {
  code: number;
  stdout: string;
  stderr: string;
  abandoned: boolean;
}

export function classifyTimeoutProbe(
  probe: ProbeResult,
  elapsedMs: number | null,
  deadlineMs: number,
): TimeoutVerdict {
  const overBudget = elapsedMs !== null && elapsedMs >= deadlineMs;
  const inconclusive = (): TimeoutVerdict =>
    overBudget
      ? { disposition: "event", kind: "timed_out", livenessConfirmed: false }
      : { disposition: "rearm" };

  if (probe.abandoned) return inconclusive();
  if (probe.code !== 0) {
    const code = parseHerdrJson(probe.stderr)?.error?.code;
    if (code === "agent_not_found") return { disposition: "event", kind: "error" };
    return inconclusive();
  }
  const status = parseHerdrJson(probe.stdout)?.result?.agent?.agent_status;
  if (status === "blocked") return { disposition: "event", kind: "blocked" };
  if (status === "idle" || status === "done") return { disposition: "event", kind: "finished" };
  if (status === undefined) return inconclusive();
  return overBudget
    ? { disposition: "event", kind: "timed_out", livenessConfirmed: true }
    : { disposition: "rearm" };
}

export function workerWorktreePath(cwd: string | undefined, slug: string): string | null {
  if (!cwd) return null;
  return join(dirname(cwd), `${basename(cwd)}-${slug}`);
}

export function deadlineStopDetail(
  worker: WorkerRecord,
  deadlineMs: number,
  opts: { livenessConfirmed: boolean; probeDetail?: string },
): string {
  const minutes = Math.round(deadlineMs / 60000);
  const lines = opts.livenessConfirmed
    ? [
        `worker budget of ${minutes} min of working time elapsed while the agent still reported working -- stopped deliberately.`,
      ]
    : [
        `worker budget of ${minutes} min of working time elapsed, and its liveness could NOT be verified: ${opts.probeDetail ?? "the probe gave no usable answer"}.`,
        "It may have been working, or may have died earlier -- this stop is on the budget, not on evidence about the worker.",
      ];
  lines.push(
    "",
    `The item is very likely still in-progress with a live claim: python3 ~/.claude/scripts/dev_status.py show ${worker.slug}`,
  );
  const worktree = workerWorktreePath(worker.cwd, worker.slug);
  if (worktree) {
    lines.push(
      `Its worktree survives on disk. Worker cwd was ${worker.cwd}; by the <repo>-<slug> convention that makes the worktree ${worktree} (derived from the cwd, not verified).`,
      `Recover with: git -C ${worker.cwd} worktree remove --force ${worktree}, then reset the item to open to clear the claim.`,
    );
  } else {
    lines.push(
      "This worker predates cwd tracking, so its worktree path cannot be named here -- find it with git worktree list.",
    );
  }
  return lines.join("\n");
}

export function paneIdentityMismatch(
  getExitCode: number,
  getStdout: string,
  expectedPaneId: string,
): boolean {
  if (getExitCode !== 0) return true;
  const reportedPaneId = parseHerdrJson(getStdout)?.result?.agent?.pane_id;
  return reportedPaneId !== expectedPaneId;
}

export function waitResultDetail(stdout: string, stderr: string): string {
  const err = parseHerdrJson(stderr)?.error;
  if (err) return `${err.code ?? "unknown"}: ${err.message ?? stderr.trim()}`;
  return stdout.trim() || stderr.trim() || "(no output)";
}

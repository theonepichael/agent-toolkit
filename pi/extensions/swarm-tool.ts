import { randomBytes } from "node:crypto";
import { existsSync, mkdirSync, readFileSync, rmSync, writeFileSync } from "node:fs";
import { homedir } from "node:os";
import { join } from "node:path";
import type { ExtensionAPI } from "@earendil-works/pi-coding-agent";
import { Type } from "typebox";
import { permissionGateAckPath } from "./permission-gate";

// Registers swarm_spawn / swarm_poll / swarm_resolve_blocked -- lets a pi
// session process several READY dev_status.py backlog items concurrently by
// spawning one recursive pi worker per item in its own herdr pane, pooling
// completion via herdr's socket API. Design, decisions, and three rounds of
// /second-opinion critique: ~/.claude/data/grill/2026-09-01-pi-side-agent-swarm-orchestratio-plan.md
// and its -critique-notes.md companion.
//
// The herdr mechanics below (agent_status JSON shape, --until repetition,
// send-keys navigation) were verified against a real live blocked agent
// during implementation, not just inferred from `herdr --skill`'s docs --
// four real gaps surfaced that way and are fixed in this file:
//   1. `--until` must be repeated per state; a comma-joined list is a CLI
//      usage error (exit 2), not silently accepted.
//   2. herdr's pi integration (herdr-agent-state.ts) reports "blocked" only
//      when told to via a `herdr:blocked` event -- it does no screen
//      detection once its lifecycle hook is authoritative. Nothing in this
//      repo emitted that event before this change; question-tool.ts now
//      does (see that file), and backlog-item.md's commit/merge-push gates
//      were updated to go through it instead of "ask in plain text" (which
//      just ends the turn -- indistinguishable from finishing).
//   3. `agent prompt` refuses an agent already `blocked` (`agent_blocked`
//      error) -- it cannot be used to answer a picker.
//   4. There is no `agent send-text`; answering a blocked picker means
//      driving it via `agent send-keys` arrow-key navigation (confirmed:
//      "down"/"up" move the rendered `>` marker, "enter" submits).
//
// A fifth gap surfaced later, live, on a real multi-item swarm run: an
// earlier version of swarm_poll raced every active worker's `herdr agent
// wait` call and ABORTED every non-winner the instant any one settled --
// including workers that were still genuinely active or had just reached a
// real blocked state. `pi.exec`'s underlying execCommand (dist/core/exec.js)
// always RESOLVES, even on abort, coercing a signal-killed process's null
// exit code to 0 -- so a killed wait call could resolve with exitCode 0 and
// empty stdout, which fell into classifyWaitResult's old fallback bucket
// and got reported as a false timed_out. Confirmed live: a worker that had
// actually finished successfully (dev_status.py showed status: done) and
// another that had reached a genuine blocked question were both misreported
// this way. Fix: no aborting at all. Each active worker gets exactly one
// long-running `agent wait` call, armed once and never killed -- losers of
// a race just keep running toward their own --timeout, feeding a shared
// per-run event queue as they naturally resolve. classifyWaitResult also no
// longer folds every non-timeout nonzero exit into "timed_out" -- only
// herdr's own `{"error":{"code":"timeout"}}` (on stderr) counts as a real
// timeout; anything else (agent_not_found, a crash, an unparseable
// response) is reported as "error" instead, with the raw detail attached,
// rather than silently mislabeled as a timeout that never happened.

/**
 * Where a run's state file lives. Resolved per call, not captured at module
 * load, so a test can point it somewhere disposable after importing this
 * module -- swarm_poll/swarm_spawn persist through the closure's `persist`,
 * which has no stateDir parameter to thread one through, so an env override
 * is the only seam that keeps an execute()-level test off the real ~/.pi.
 */
function herdrStateDir(): string {
  return process.env.PI_SWARM_STATE_DIR ?? join(homedir(), ".pi", "agent", "state");
}

const DEFAULT_CONCURRENCY = 3;
const OPEN_PANE_SOFT_CAP_MULTIPLIER = 2;
const AGENT_START_TIMEOUT_MS = 30_000;
const DEFAULT_WAIT_TIMEOUT_MS = 30 * 60 * 1000; // 30 minutes, matching --auto's generous per-step conventions
const RESOLVE_VERIFY_TIMEOUT_MS = 5_000;
const BLOCKED_READ_LINES = 500;
const BLOCKED_READ_LINES_RETRY = 2000;
/** `herdr agent start` already gates on interactive_ready, so the worker's extensions are loaded before the trust prompt is sent -- generous for the write, well under AGENT_START_TIMEOUT_MS. */
const TRUST_ACK_TIMEOUT_MS = 15_000;
/** A local stat, not a socket call, so a tight interval costs nothing. */
const TRUST_ACK_POLL_MS = 100;
/** Enough for a pi crash trace, small enough that one failure cannot flood the orchestrator's digest. */
export const PANE_CAPTURE_CHARS = 4000;
const PANE_CAPTURE_LINES = 200;

/** Resolved per call, same seam as herdrStateDir() -- lets a test wait out a deadline in milliseconds instead of 15 seconds. */
function trustAckTimeoutMs(): number {
  const override = Number(process.env.PI_SWARM_TRUST_ACK_TIMEOUT_MS);
  return Number.isFinite(override) && override > 0 ? override : TRUST_ACK_TIMEOUT_MS;
}

// ---------------------------------------------------------------------------
// Types
// ---------------------------------------------------------------------------

export type WorkerLifecycle = "active" | "awaiting_relay";

export interface WorkerRecord {
  agent: string; // synthetic id, e.g. "w1" -- never the raw slug (herdr names cap at 32 chars)
  slug: string;
  paneId: string;
  lifecycle: WorkerLifecycle;
}

export interface SwarmState {
  runId: string;
  concurrency: number;
  nextCounter: number;
  workers: WorkerRecord[];
}

/** One item's outcome from the spawn loop: a live worker, or a reason it never became one. */
type SpawnOutcome =
  { worker: WorkerRecord } | { slug: string; failed?: { slug: string; reason: string } };

export type PollEventKind = "blocked" | "finished" | "timed_out" | "error";

export interface PollEvent {
  kind: PollEventKind;
  agent: string;
  slug: string;
  paneId: string;
  rawPrompt?: string; // blocked only
  truncated?: boolean; // blocked only
  detail?: string; // timed_out/error only -- the raw herdr error detail, for an honest digest
}

// ---------------------------------------------------------------------------
// State file persistence and crash recovery
// ---------------------------------------------------------------------------

export function statePath(runId: string, stateDir: string = herdrStateDir()): string {
  return join(stateDir, `swarm-${runId}.json`);
}

export function loadState(runId: string, stateDir: string = herdrStateDir()): SwarmState | null {
  const path = statePath(runId, stateDir);
  if (!existsSync(path)) return null;
  try {
    const parsed: unknown = JSON.parse(readFileSync(path, "utf8"));
    if (parsed && typeof parsed === "object" && "workers" in parsed) {
      return parsed as SwarmState;
    }
    return null;
  } catch {
    return null;
  }
}

export function saveState(state: SwarmState, stateDir: string = herdrStateDir()): void {
  mkdirSync(stateDir, { recursive: true });
  writeFileSync(statePath(state.runId, stateDir), JSON.stringify(state, null, 2));
}

/**
 * Reconcile persisted bookkeeping against herdr's live agent list. A
 * tracked-but-dead entry (in state, absent from herdr's live list) is
 * dropped and reported for the digest -- the pane/agent is gone, nothing
 * left to reconcile.
 *
 * This does not attempt to *adopt* an untracked-but-live herdr agent: doing
 * so would need a slug to resume tracking it under, and no slug is
 * recoverable from a bare agent id (the id↔slug↔pane mapping lives only in
 * the state file itself, per nextAgentId's design). The actual crash-
 * recovery win is `loadState` reading that file straight off disk after a
 * process restart -- the mapping survives as long as `saveState` ran before
 * the crash. The narrow gap this leaves (a worker spawned but never
 * persisted before a crash) has no general fix and isn't attempted here.
 */
export function reconcileState(
  state: SwarmState,
  liveAgentIds: readonly string[],
): { state: SwarmState; dropped: WorkerRecord[] } {
  const live = new Set(liveAgentIds);
  const dropped: WorkerRecord[] = [];
  const kept: WorkerRecord[] = [];

  for (const worker of state.workers) {
    if (live.has(worker.agent)) {
      kept.push(worker);
    } else {
      dropped.push(worker);
    }
  }

  return { state: { ...state, workers: kept }, dropped };
}

// ---------------------------------------------------------------------------
// Naming
// ---------------------------------------------------------------------------

/** Synthetic herdr agent name incorporating the slug, capped at herdr's 32-char limit. */
export function nextAgentId(runId: string, counter: number, slug?: string): string {
  const cleanSlug = slug ? slug.replace(/[^a-zA-Z0-9_-]/g, "") : "";
  const base = cleanSlug ? `${runId}-w${counter}-${cleanSlug}` : `${runId}-w${counter}`;
  return base.slice(0, 32);
}

// ---------------------------------------------------------------------------
// herdr argv builders -- every element is a discrete argv item, never a
// concatenated shell string (delegate-tool.ts's buildArgv pattern). This is
// load-bearing: swarm_resolve_blocked passes the human's free-text answer
// straight through as one of these elements.
// ---------------------------------------------------------------------------

export function buildPaneRenameArgv(paneId: string, label: string): string[] {
  return ["pane", "rename", paneId, label];
}

export function buildPaneSplitArgv(
  direction: "right" | "down",
  cwd: string,
  opts: { paneId?: string; ratio?: number } = {},
): string[] {
  // Without an explicit pane, herdr splits whatever `--current` resolves to.
  // A plan splits the pane it created last, so it has to name one.
  const target = opts.paneId ?? "--current";
  const argv = ["pane", "split", target, "--direction", direction];
  if (opts.ratio !== undefined) {
    argv.push("--ratio", String(opts.ratio));
  }
  argv.push("--cwd", cwd, "--no-focus");
  return argv;
}

/** Asks herdr for the layout of the tab holding the orchestrator's pane. */
export function buildPaneLayoutArgv(): string[] {
  return ["pane", "layout", "--current"];
}

/** A pane's size in terminal cells, as `herdr pane layout` reports it. */
export interface PaneRect {
  width: number;
  height: number;
}

/**
 * Read the orchestrator's own pane rect out of `herdr pane layout --current`.
 *
 * `paneId` should be `HERDR_PANE_ID`, which is the pane the split will target.
 * That is not always the focused pane -- the person driving the swarm may be
 * looking somewhere else -- so the focused pane is only a fallback.
 */
export function parseCurrentPaneRect(
  stdout: string,
  paneId: string | undefined,
): PaneRect | undefined {
  try {
    const parsed = JSON.parse(stdout) as {
      result?: {
        layout?: {
          focused_pane_id?: string;
          panes?: { pane_id?: string; rect?: { width?: number; height?: number } }[];
        };
      };
    };
    const layout = parsed.result?.layout;
    const panes = layout?.panes ?? [];
    const mine =
      panes.find((pane) => pane.pane_id === paneId) ??
      panes.find((pane) => pane.pane_id === layout?.focused_pane_id);
    const rect = mine?.rect;
    if (typeof rect?.width !== "number" || typeof rect.height !== "number") return undefined;
    return { width: rect.width, height: rect.height };
  } catch {
    return undefined;
  }
}

/**
 * Smallest worker pane worth spawning into. These are *usability* floors, not
 * crash floors.
 *
 * Measured 2026-09-02 against pi 0.84.4 with every extension in this repo
 * loaded: pi starts cleanly at 10 columns and 3 rows. It used to abort at
 * startup in anything narrower than its widest header line, which is what
 * made a small pane look fatal -- that was this repo's own header ignoring
 * the width it was handed, fixed in philosophy-header.ts. What is left is
 * simply that nobody can read a diff or a test failure in a 21-column pane,
 * so the swarm says so instead of spawning workers no one can use.
 */
export const MIN_WORKER_PANE_COLS = 40;
export const MIN_WORKER_PANE_ROWS = 10;

/** One planned `herdr pane split`, to be run in order. */
export interface SplitStep {
  slug: string;
  direction: "right" | "down";
  /**
   * Fraction the pane being split keeps; herdr gives the new pane the rest.
   * Measured against herdr 0.8.2: `--ratio 0.3333` on a 168-column pane
   * leaves it at 56 and creates one of 112.
   */
  ratio: number;
  /**
   * Size the pane this step creates ends at, once the whole plan has run.
   * Absent when herdr could not report the layout, in which case no floor was
   * applied either.
   */
  rect?: PaneRect;
}

export interface SplitPlan {
  steps: SplitStep[];
  rejected: { slug: string; reason: string }[];
}

function paneTooNarrowReason(start: PaneRect, requested: number): string {
  return (
    `pane_too_narrow: splitting a ${start.width}x${start.height} pane ` +
    `${requested} way(s) leaves under ${MIN_WORKER_PANE_COLS}x${MIN_WORKER_PANE_ROWS} per worker`
  );
}

/**
 * Plan the pane splits for a batch of workers.
 *
 * The shipped version alternated `right`/`down` and split `--current` every
 * time, which halves the same pane repeatedly: three workers out of a 168
 * column tab left panes of 21 columns, and the reported failure said nothing
 * about width. Splitting the *previous* new pane with a ratio of one over the
 * shares still to carve gives every pane an equal share instead.
 *
 * Orientation comes from the geometry rather than the loop index. Side by side
 * is preferred because reading code wants columns; splitting down preserves
 * the full width and is the fallback when the pane is already narrow. When
 * even that does not clear the floor, the batch is trimmed -- a swarm of two
 * beats a swarm of none -- and whatever is left over is rejected by name.
 */
export function planSplits(start: PaneRect | undefined, slugs: readonly string[]): SplitPlan {
  const rejected: { slug: string; reason: string }[] = [];
  if (slugs.length === 0) return { steps: [], rejected };

  // Geometry only drives the usability floor. If herdr cannot report it, even
  // splits are still strictly better than halving one pane per worker, so the
  // run proceeds unguarded rather than refusing to spawn on a missing field.
  if (start === undefined) {
    return {
      steps: slugs.map((slug, i) => ({
        slug,
        direction: "right" as const,
        ratio: 1 / (slugs.length + 1 - i),
      })),
      rejected,
    };
  }

  // `shares` counts the orchestrator's own pane, which keeps one share.
  const orientationFor = (
    workers: number,
  ): { direction: "right" | "down"; rect: PaneRect } | undefined => {
    const shares = workers + 1;
    const across = Math.floor(start.width / shares);
    if (across >= MIN_WORKER_PANE_COLS && start.height >= MIN_WORKER_PANE_ROWS) {
      return { direction: "right", rect: { width: across, height: start.height } };
    }
    const downward = Math.floor(start.height / shares);
    if (start.width >= MIN_WORKER_PANE_COLS && downward >= MIN_WORKER_PANE_ROWS) {
      return { direction: "down", rect: { width: start.width, height: downward } };
    }
    return undefined;
  };

  let workers = slugs.length;
  let choice = orientationFor(workers);
  while (choice === undefined && workers > 1) {
    workers -= 1;
    choice = orientationFor(workers);
  }

  if (choice === undefined) {
    for (const slug of slugs) {
      rejected.push({ slug, reason: paneTooNarrowReason(start, slugs.length) });
    }
    return { steps: [], rejected };
  }

  const steps: SplitStep[] = [];
  for (let i = 0; i < workers; i++) {
    steps.push({
      slug: slugs[i]!,
      direction: choice.direction,
      ratio: 1 / (workers + 1 - i),
      rect: choice.rect,
    });
  }
  for (const slug of slugs.slice(workers)) {
    rejected.push({ slug, reason: paneTooNarrowReason(start, slugs.length) });
  }
  return { steps, rejected };
}

export function buildAgentStartArgv(agentId: string, paneId: string): string[] {
  return [
    "agent",
    "start",
    agentId,
    "--kind",
    "pi",
    "--pane",
    paneId,
    "--timeout",
    String(AGENT_START_TIMEOUT_MS),
  ];
}

/**
 * Slash command that turns off permission-gate.ts's bash confirmation for a
 * worker's session.
 *
 * Pi ships no permission system of its own (docs/usage.md's Design
 * Principles: "it intentionally does not include ... permission popups").
 * permission-gate.ts supplies one, defaulting to enabled with an "ask"
 * fallback for anything outside ALLOW_PATTERNS. A worker lives in a herdr
 * TUI pane, so its `ctx.hasUI` is true and the gate raises `ctx.ui.confirm`
 * -- a question aimed at a human, in a pane no human is watching. The worker
 * then waits forever, and nothing surfaces it: `agent_status` stays
 * "working", so swarm_poll classifies it as making progress and the
 * orchestrator relays nothing until the wait's own timeout expires.
 *
 * Only the bash gate is dropped. guard-rails.ts stays armed, so a worker
 * still cannot write into a repo's main checkout while an item is in
 * progress -- `/trust-session` would disable both, which is more autonomy
 * than an unattended worker should have.
 */
export const WORKER_TRUST_COMMAND = "/permission-gate-disable";

/**
 * `--wait` blocks until the agent settles, so a follow-up prompt can't land
 * while it is still processing this one.
 *
 * NOT usable for WORKER_TRUST_COMMAND, and the reason is the bug this file's
 * ack handshake exists to fix. herdr 0.8.2 documents `--wait` from a
 * non-working state as requiring an observed lifecycle change within 5000 ms,
 * with no flag to relax it. A client-side pi slash command applies instantly
 * and never enters the working state, so there is nothing to observe and the
 * call always returns agent_prompt_stalled -- on a worker whose gate had in
 * fact just come down. Confirmed live on 2026-09-02 against a real pi.
 */
export function buildAgentPromptArgv(
  agentId: string,
  prompt: string,
  opts: { wait?: boolean } = {},
): string[] {
  const argv = ["agent", "prompt", agentId, prompt];
  return opts.wait ? [...argv, "--wait"] : argv;
}

/**
 * Token this spawn will wait on, passed to WORKER_TRUST_COMMAND and used to
 * name the ack file the worker writes (permission-gate.ts owns the path).
 *
 * The random suffix is load-bearing, not decoration: it is what makes a stale
 * ack harmless. A zombie worker from a crashed run cannot write the file this
 * spawn polls, so no pre-send deletion is needed and no worker can be
 * confirmed by another worker's acknowledgement.
 */
export function trustAckToken(agentId: string): string {
  const normalized = agentId.replace(/[^A-Za-z0-9_-]/g, "-");
  return `${normalized}-${randomBytes(4).toString("hex")}`;
}

/**
 * True when the file's contents parse and carry the exact token this spawn is
 * waiting on.
 *
 * Asserts on the token rather than the path: the token is what makes a stale
 * ack harmless, so the token is what has to be checked. A parse failure is not
 * an error -- permission-gate.ts writes to a temp file and renames, but a
 * caller that reads mid-rename, or a truncated file from an older run, simply
 * is not confirmation yet.
 */
export function ackMatches(fileContents: string, expectedToken: string): boolean {
  try {
    const parsed = JSON.parse(fileContents) as { token?: unknown };
    return parsed.token === expectedToken;
  } catch {
    return false;
  }
}

/**
 * The classification line of a failure reason, without the pane capture
 * appended after it.
 *
 * Exists because an orchestrator reads a tool's `content` and nothing else --
 * confirmed live on 2026-09-02, where a real swarm_spawn failure carried its
 * full reason in `details` and the model's next turn reported seeing "no
 * per-item failure details". backlog-item.md instructs the orchestrator to
 * act differently on permission_gate_not_disabled than on
 * agent_prompt_failed, so which one fired has to reach the text. The pane
 * capture stays behind in `details`: it is up to PANE_CAPTURE_CHARS per
 * failure and is for a human reading back, not for the routing decision.
 */
export function reasonHeadline(reason: string): string {
  return reason.split("\n", 1)[0] ?? reason;
}

/** `agent prompt` refuses a blocked agent outright (agent_blocked, confirmed live) -- send-keys is the only way to answer its picker. */
export function buildAgentSendKeysArgv(agentId: string, keys: readonly string[]): string[] {
  return ["agent", "send-keys", agentId, ...keys];
}

/** `--until` must be repeated once per state -- herdr rejects a comma-joined list (confirmed live: `--until idle,done,blocked` exits 2, "invalid agent status"). */
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

/** By pane id, not agent id -- a spawn can fail before `agent start` succeeds, and the pane's text is exactly what diagnoses that. */
export function buildPaneReadArgv(paneId: string, lines: number): string[] {
  return ["pane", "read", paneId, "--source", "recent-unwrapped", "--lines", String(lines)];
}

export function buildAgentListArgv(): string[] {
  return ["agent", "list"];
}

export function parseAgentListIds(stdout: string): string[] {
  try {
    const parsed = JSON.parse(stdout) as {
      result?: { agents?: { name?: string }[] };
    };
    // Each `agent list` entry carries the caller-chosen herdr name directly
    // as `.name` (confirmed live, herdr 0.8.2, 2026-09-02) -- this is what
    // `reconcileState` must match against `worker.agent` (the synthetic id
    // assigned at spawn time). The key is present exactly when the agent was
    // started with an explicit name (`herdr agent start <NAME>`) and absent
    // entirely otherwise -- an unnamed entry is a foreign/interactive agent,
    // never one of ours, so skipping nameless entries is correct, not a gap.
    // Swarm workers are always started named: buildAgentStartArgv passes the
    // synthetic id as the NAME argument. Do not re-derive this from an
    // unnamed agent and conclude `.name` is the wrong field -- an earlier
    // version read `agent_session.value` (a pi session file path) instead,
    // which could never match a synthetic id like "run1-w1" and would have
    // wrongly dropped every genuinely-live worker as dead.
    return (parsed.result?.agents ?? [])
      .map((a) => a.name)
      .filter((v): v is string => typeof v === "string");
  } catch {
    return [];
  }
}

// ---------------------------------------------------------------------------
// herdr response parsing
// ---------------------------------------------------------------------------

interface HerdrEnvelope {
  // `agent get`/`agent wait` nest the single-agent payload one level under
  // "agent" -- confirmed live, repeatedly (e.g. `herdr agent get <id>` ->
  // {"result":{"agent":{"agent_status":"blocked",...}}}). `agent list`'s
  // `result.agents[]` array elements do NOT have this extra nesting (each
  // element already has agent_status/agent_session directly on it) -- these
  // are two different response shapes for two different commands, not one
  // shared shape; do not conflate them into a single flat interface again.
  result?: {
    agent?: { agent_status?: string; pane_id?: string };
    agents?: { agent_status?: string; agent_session?: unknown; pane_id?: string }[];
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

/**
 * Classify a settled `herdr agent wait` result into blocked/finished/
 * timed_out/error. herdr reports server errors as JSON on stderr with exit
 * status 1 (confirmed live) -- only `{"error":{"code":"timeout"}}` counts as
 * a genuine timeout. Any other nonzero exit (agent_not_found, a crash) or an
 * exit-0 response with no recognized agent_status is "error", not silently
 * folded into "timed_out" -- see this file's header comment for why that
 * distinction matters (a killed-but-resolved exec call looks exactly like
 * the exit-0/unrecognized-status case).
 */
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

/**
 * Cross-check that `agent get`'s reported pane_id still matches the pane
 * this worker was spawned into, right before `swarm_resolve_blocked` sends
 * any keystrokes. herdr's `agent <name>` commands (read/get/send-keys) all
 * resolve the same name -> pane mapping internally; if that mapping ever
 * goes stale or collides under concurrency, every one of those calls would
 * consistently hit the same wrong pane, so re-reading by agent name before
 * sending keys can't catch it -- only a cross-check against the pane_id
 * swarm-tool tracked independently at spawn time can. A missing/unparseable
 * pane_id is treated as a mismatch (fail closed, not open).
 */
export function paneIdentityMismatch(
  getExitCode: number,
  getStdout: string,
  expectedPaneId: string,
): boolean {
  if (getExitCode !== 0) return true;
  const reportedPaneId = parseHerdrJson(getStdout)?.result?.agent?.pane_id;
  return reportedPaneId !== expectedPaneId;
}

/** The raw herdr error detail for a timed_out/error event -- for an honest digest, not just a bare label. */
export function waitResultDetail(stdout: string, stderr: string): string {
  const err = parseHerdrJson(stderr)?.error;
  if (err) return `${err.code ?? "unknown"}: ${err.message ?? stderr.trim()}`;
  return stdout.trim() || stderr.trim() || "(no output)";
}

/** Content that fills the requested line budget exactly is a truncation signal, not necessarily proof -- see plan section 4. */
export function looksTruncated(content: string, requestedLines: number): boolean {
  return content.split("\n").length >= requestedLines;
}

// ---------------------------------------------------------------------------
// Answering a blocked worker's picker -- `herdr agent prompt` refuses a
// blocked agent outright (agent_blocked error, confirmed live) and there is
// no `agent send-text` for literal input. The only real path is driving
// question-tool.ts's rendered picker via `agent send-keys` (arrow-key
// navigation, confirmed live: "down" moves the `>` marker, "enter" submits).
// Free-text answers matching no listed option aren't auto-answerable this
// way -- see swarm_resolve_blocked's needsManual outcome below.
// ---------------------------------------------------------------------------

export interface RenderedOption {
  index: number;
  label: string;
}

export interface ParsedPicker {
  selectedIndex: number | null;
  options: RenderedOption[];
}

const OPTION_LINE = /^\s*(>)?\s*(\d+)\.\s+(.+?)\s*$/;
const OTHER_OPTION_LABEL = "Something else (type it)";
/**
 * question-tool.ts's picker closes with one of exactly three hint lines --
 * select, multi-select, and the free-text edit mode -- and wraps the whole
 * block in full-width accent rules. Those are the only marks in the pane that
 * belong to the live picker and nothing else, so they are what the parse
 * anchors on.
 *
 * Matched on the OPENING of the hint line only, never the whole of it. pi
 * wraps the footer to the pane width before it is ever captured, and a worker
 * pane in a real three-way split is 42 columns, where
 * "↑↓ navigate • Enter to select • Esc to cancel" breaks after "Esc to". An
 * anchor spanning the full sentence matched nothing there, so parsePicker
 * returned no options and every relay came back needs_manual with "(none
 * parsed)" -- observed live on 2026-09-02. Both openings are short enough to
 * survive any width the pane guard permits.
 */
const PICKER_FOOTER = /↑↓ navigate|Enter to submit/;
const PICKER_RULE = /^─{3,}\s*$/;

/**
 * Parse question-tool.ts's rendered picker (plain `recent-unwrapped` text, no
 * ANSI) into its option list and current selection.
 *
 * Anchored to the LAST rendered picker rather than scanning the whole
 * capture, because `agent read` hands back 500 lines of scrollback and any
 * numbered line in it used to parse as an option. Observed on 2026-09-02: a
 * plan list sitting above a real picker contributed "1. Merge to main and
 * push" as option 1, and the navigation keys were then computed from that
 * fabricated index -- submitting whatever happened to sit at the resulting
 * offset. Reading the wrong list is worse than reading none, so anything
 * unexpected inside the window yields no options at all and the caller falls
 * back to needs_manual.
 */
export function parsePicker(content: string): ParsedPicker {
  const lines = content.split("\n");

  let footer = -1;
  for (let i = lines.length - 1; i >= 0; i--) {
    if (PICKER_FOOTER.test(lines[i]!)) {
      footer = i;
      break;
    }
  }
  if (footer === -1) return { selectedIndex: null, options: [] };

  let opening = -1;
  for (let i = footer - 1; i >= 0; i--) {
    if (PICKER_RULE.test(lines[i]!)) {
      opening = i;
      break;
    }
  }
  if (opening === -1) return { selectedIndex: null, options: [] };

  const options: RenderedOption[] = [];
  const selected: number[] = [];
  for (const line of lines.slice(opening + 1, footer)) {
    const m = OPTION_LINE.exec(line);
    if (!m) continue;
    options.push({ index: Number(m[2]), label: m[3]! });
    if (m[1] === ">") selected.push(Number(m[2]));
  }

  // render() numbers options `${i + 1}`, so a real picker's indices are always
  // 1..N with no gaps. Anything else means the window caught something that is
  // not an option list -- a description that happens to start with a number,
  // a redraw seam -- and there is no safe way to navigate a list we misread.
  const contiguous = options.length > 0 && options.every((option, i) => option.index === i + 1);
  if (!contiguous) return { selectedIndex: null, options: [] };

  return { selectedIndex: selected.length === 1 ? selected[0]! : null, options };
}

/** Shortest answer allowed to match as a fragment; below this, only an exact label will do. */
const MIN_PARTIAL_ANSWER = 3;

/** True when `needle` appears in `haystack` bounded by non-alphanumerics on both sides. */
function containsAsWord(haystack: string, needle: string): boolean {
  const isWordChar = (c: string | undefined) => c !== undefined && /[a-z0-9]/.test(c);
  let from = 0;
  for (;;) {
    const at = haystack.indexOf(needle, from);
    if (at === -1) return false;
    if (!isWordChar(haystack[at - 1]) && !isWordChar(haystack[at + needle.length])) {
      return true;
    }
    from = at + 1;
  }
}

/**
 * Match a free-text answer to a listed option -- case-insensitive exact match
 * first, then a whole-word fragment match, both excluding the always-present
 * free-text escape option (never auto-select "Something else" via fuzzy
 * matching). Returns null on no match or an ambiguous (multiple) match -- the
 * caller falls back to reporting needsManual rather than guessing.
 *
 * The fragment match is bounded on word edges because a bare substring test
 * inverted answers on the approval gate this tool exists to relay:
 * matchOption("no", ["Commit now (Recommended)", "Stop here"]) found "no"
 * inside "now", matched exactly one option, and committed for a user who had
 * said no. Reproduced live on 2026-09-02 against a real picker. Answers
 * shorter than MIN_PARTIAL_ANSWER skip the fragment pass entirely, so a
 * two-letter answer can only ever take the exact path -- "no" still answers
 * an option actually labelled "No".
 */
export function matchOption(
  answer: string,
  options: readonly RenderedOption[],
): RenderedOption | null {
  const candidates = options.filter(
    (o) => o.label.toLowerCase() !== OTHER_OPTION_LABEL.toLowerCase(),
  );
  const needle = answer.trim().toLowerCase();
  if (!needle) return null;

  const exact = candidates.filter((o) => o.label.toLowerCase() === needle);
  if (exact.length === 1) return exact[0]!;
  if (needle.length < MIN_PARTIAL_ANSWER) return null;

  const partial = candidates.filter((o) => containsAsWord(o.label.toLowerCase(), needle));
  if (partial.length === 1) return partial[0]!;

  return null;
}

/** Arrow-key presses to move from the currently selected option to the target, then submit. */
export function navigationKeys(fromIndex: number, toIndex: number): string[] {
  const steps = toIndex - fromIndex;
  const key = steps > 0 ? "down" : "up";
  return [...Array(Math.abs(steps)).fill(key), "enter"];
}

// ---------------------------------------------------------------------------
// Concurrency and pane accounting -- pure, so the cap/backpressure rules are
// independently testable from the async pool machinery that calls them.
// ---------------------------------------------------------------------------

export function activeWorkerCount(state: SwarmState): number {
  return state.workers.filter((w) => w.lifecycle === "active").length;
}

export function canSpawnNew(state: SwarmState): boolean {
  return activeWorkerCount(state) < state.concurrency;
}

export function openPaneCount(state: SwarmState): number {
  return state.workers.length; // active + awaiting_relay; finished workers' entries are removed on close
}

export function openPaneSoftCap(concurrency: number): number {
  return concurrency * OPEN_PANE_SOFT_CAP_MULTIPLIER;
}

export function canOpenNewPane(state: SwarmState): boolean {
  return openPaneCount(state) < openPaneSoftCap(state.concurrency);
}

// ---------------------------------------------------------------------------
// Queue selection
// ---------------------------------------------------------------------------

/** How many new items can be spawned right now, bounded by both the concurrency cap and the open-pane soft cap. */
export function spawnBudget(state: SwarmState, readyCount: number): number {
  const byConcurrency = Math.max(0, state.concurrency - activeWorkerCount(state));
  const byPaneCap = Math.max(0, openPaneSoftCap(state.concurrency) - openPaneCount(state));
  return Math.min(byConcurrency, byPaneCap, readyCount);
}

export default function (pi: ExtensionAPI) {
  const activeRuns = new Map<string, SwarmState>();

  async function herdr(pi_: ExtensionAPI, argv: string[], signal?: AbortSignal) {
    return pi_.exec("herdr", argv, { signal });
  }

  /**
   * Closes a worker's pane on a path that is dropping it from state anyway.
   *
   * Dropping the state entry without this leaves a live pi in a pane nothing
   * will ever poll, answer, or clean up, and every later split subdivides the
   * layout around it. A close that fails must not throw: the relay failure
   * being reported is the root cause, and a cleanup problem never replaces it
   * -- the same rule failWithPane follows on the spawn side.
   */
  async function closeWorkerPane(paneId: string, signal?: AbortSignal): Promise<void> {
    try {
      await herdr(pi, buildPaneCloseArgv(paneId), signal);
    } catch {
      // Pane already gone, or herdr unresponsive -- nothing left to clean up.
    }
  }

  /**
   * Waits for the worker to state, in its own ack file, that its bash
   * permission gate came down.
   *
   * Deliberately not a read of the worker's terminal: herdr documents
   * `agent read --source recent` as the last 80 rendered rows, so every read
   * is a bounded sliding window -- a TUI redraw rewrites it rather than
   * appending, an older notice can scroll out between two reads, and a stale
   * notice from an earlier command can already be sitting in it. Each of
   * those yields a false confirmation or a false failure. The poll is a local
   * stat, so it adds no herdr socket traffic.
   *
   * Deletes the file on confirmation -- the ack answers one question once.
   */
  async function awaitTrustAck(token: string): Promise<boolean> {
    const path = permissionGateAckPath(token);
    const deadline = Date.now() + trustAckTimeoutMs();
    for (;;) {
      try {
        if (ackMatches(readFileSync(path, "utf8"), token)) {
          rmSync(path, { force: true });
          return true;
        }
      } catch {
        // Not written yet, or caught mid-rename -- both mean "not confirmed".
      }
      if (Date.now() >= deadline) return false;
      await new Promise((resolve) => setTimeout(resolve, TRUST_ACK_POLL_MS));
    }
  }

  /**
   * Records a pane's terminal text into a failure reason, then closes the
   * pane.
   *
   * Both halves are fixes for observed damage. The capture: a pane's text is
   * the only record of an early worker crash, and the sibling pane-width bug
   * was diagnosed entirely from a leaked pane. The close: two failed runs on
   * 2026-09-02 left six orphan panes open, and each retry subdivides the
   * layout further, so a later round produces panes too small for pi even
   * when the first round was fine.
   *
   * A capture that itself fails (hard-crashed pane, unresponsive herdr) is
   * noted and the original reason is preserved unchanged -- a capture problem
   * must never replace the root cause.
   */
  async function failWithPane(
    slug: string,
    paneId: string,
    reason: string,
  ): Promise<{ slug: string; failed: { slug: string; reason: string } }> {
    let capture: string;
    try {
      const read = await herdr(pi, buildPaneReadArgv(paneId, PANE_CAPTURE_LINES));
      capture =
        read.code === 0
          ? // The tail, not the head: a crash trace lands at the end.
            read.stdout.slice(-PANE_CAPTURE_CHARS)
          : `<pane capture failed: ${(read.stderr || read.stdout).slice(0, 200)}>`;
    } catch (e) {
      capture = `<pane capture threw: ${String(e)}>`;
    }
    try {
      await herdr(pi, buildPaneCloseArgv(paneId));
    } catch {
      // Same rule as the capture: a cleanup problem is not the root cause,
      // and a rejected close must not throw this function's reason away.
    }
    return { slug, failed: { slug, reason: `${reason}\n--- pane ${paneId} ---\n${capture}` } };
  }

  /**
   * Everything that happens inside a pane that already exists: start the
   * agent, take its bash permission gate down, then hand it its item.
   *
   * Split out of the spawn loop so the caller can wrap the whole thing in one
   * catch -- every failure in here has a pane to capture and close, including
   * one that arrives as a thrown exec rejection rather than a non-zero exit.
   */
  async function spawnInto(paneId: string, agentId: string, slug: string): Promise<SpawnOutcome> {
    const startResult = await herdr(pi, buildAgentStartArgv(agentId, paneId));
    if (startResult.code !== 0) {
      return failWithPane(
        slug,
        paneId,
        `agent_not_ready: ${startResult.stderr || startResult.stdout}`,
      );
    }
    // Drop the bash permission gate before any work is sent -- see
    // WORKER_TRUST_COMMAND. A worker that misses this stalls on its
    // first non-allowlisted bash call, invisibly, so a failure here
    // is a spawn failure rather than something to press on past.
    //
    // Sent WITHOUT --wait (see buildAgentPromptArgv): with it every
    // submit failed regardless of outcome, so a non-zero exit said
    // nothing about the gate. Without it, a non-zero exit means the
    // prompt genuinely could not be delivered -- a herdr-level fault,
    // not a gate that would not come down, so it gets its own reason.
    const ackToken = trustAckToken(agentId);
    const trustResult = await herdr(
      pi,
      buildAgentPromptArgv(agentId, `${WORKER_TRUST_COMMAND} ${ackToken}`),
    );
    if (trustResult.code !== 0) {
      return failWithPane(
        slug,
        paneId,
        `agent_prompt_failed: ${trustResult.stderr || trustResult.stdout}`,
      );
    }
    if (!(await awaitTrustAck(ackToken))) {
      return failWithPane(
        slug,
        paneId,
        `permission_gate_not_disabled: no acknowledgement for token ${ackToken} within ${trustAckTimeoutMs()} ms`,
      );
    }
    const promptResult = await herdr(
      pi,
      buildAgentPromptArgv(agentId, `/backlog-item --auto ${slug}`),
    );
    if (promptResult.code !== 0) {
      return failWithPane(
        slug,
        paneId,
        `agent_prompt_stalled: ${promptResult.stderr || promptResult.stdout}`,
      );
    }
    return {
      worker: {
        agent: agentId,
        slug,
        paneId,
        lifecycle: "active" as const,
      },
    };
  }

  /** Loads state, reconciling against herdr's live truth only on a cold load (state wasn't already in this process's memory) -- avoids a herdr round-trip on every call once a run is warm. */
  async function getOrInitState(runId: string, concurrency: number): Promise<SwarmState> {
    const cached = activeRuns.get(runId);
    if (cached) return cached;

    const loaded = loadState(runId);
    if (!loaded) {
      const fresh: SwarmState = { runId, concurrency, nextCounter: 0, workers: [] };
      activeRuns.set(runId, fresh);
      return fresh;
    }

    const listResult = await herdr(pi, buildAgentListArgv());
    const liveIds = listResult.code === 0 ? parseAgentListIds(listResult.stdout) : [];
    // Dropped entries (tracked in state, dead in herdr's live list) are
    // simply excluded from `reconciled.workers` -- a dead worker's item just
    // won't produce further events; there's nothing left to reconcile it
    // against once its pane and process are gone.
    const { state: reconciled } = reconcileState(loaded, liveIds);
    activeRuns.set(runId, reconciled);
    saveState(reconciled);
    return reconciled;
  }

  function persist(state: SwarmState): void {
    activeRuns.set(state.runId, state);
    saveState(state);
  }

  // ---------------------------------------------------------------------------
  // Per-run wait tracking: exactly one long-running `herdr agent wait` call
  // per active worker, armed once, never killed (the round-2 fix -- see this
  // file's header comment). Each arm's own settlement pushes a classified
  // event onto `pendingEvents`; `swarm_poll` drains whatever's queued, or
  // waits for the next arrival. Purely in-process, not persisted -- a
  // restart just re-arms fresh waits for whatever the reconciled state
  // (which IS persisted) says is still active.
  // ---------------------------------------------------------------------------

  interface RunRuntime {
    inFlight: Set<string>; // agent ids with a wait currently running
    pendingEvents: PollEvent[];
    waiters: (() => void)[]; // resolvers for swarm_poll calls currently waiting on the next event
    spawnChain: Promise<void>; // tail of this run's serialised spawn queue -- see withSpawnLock
  }

  const runtimes = new Map<string, RunRuntime>();

  function getRuntime(runId: string): RunRuntime {
    let rt = runtimes.get(runId);
    if (!rt) {
      rt = {
        inFlight: new Set(),
        pendingEvents: [],
        waiters: [],
        spawnChain: Promise.resolve(),
      };
      runtimes.set(runId, rt);
    }
    return rt;
  }

  /**
   * Runs `fn` with exclusive access to a run's spawn path, queued per runId.
   *
   * swarm_spawn reads the pool, decides a budget from it, splits panes, and
   * only pushes its new workers at the very end. getOrInitState hands every
   * caller the same cached SwarmState, so two overlapping spawns for one
   * runId each measured the same empty pool and each spawned up to the full
   * cap -- double the concurrency limit and double the open panes, silently.
   * The splits interleaved for the same reason, against a splitTarget walk
   * that assumes it owns the layout for the length of the call, which is
   * what this tool's own promptGuidelines already promised.
   *
   * Queued rather than rejected: the caller asked for those items to be
   * spawned, and the second call still gets a truthful answer once the pool
   * is settled -- whatever the cap has no room for comes back as skipped.
   * The chain never carries a rejection, because the release resolves in a
   * finally, so one failing spawn cannot wedge the run.
   */
  async function withSpawnLock<T>(runId: string, fn: () => Promise<T>): Promise<T> {
    const rt = getRuntime(runId);
    const previous = rt.spawnChain;
    let release!: () => void;
    rt.spawnChain = new Promise<void>((resolve) => {
      release = resolve;
    });
    await previous;
    try {
      return await fn();
    } finally {
      release();
    }
  }

  /** Arms a worker's wait call if one isn't already running for it. Idempotent -- safe to call every time swarm_poll checks in on the active pool. */
  function armWait(rt: RunRuntime, worker: WorkerRecord, timeoutMs: number): void {
    if (rt.inFlight.has(worker.agent)) return;
    rt.inFlight.add(worker.agent);
    void herdr(pi, buildAgentWaitArgv(worker.agent, ["idle", "done", "blocked"], timeoutMs)).then(
      (result) => {
        rt.inFlight.delete(worker.agent);
        const kind = classifyWaitResult(result.code, result.stdout, result.stderr);
        const event: PollEvent = {
          kind,
          agent: worker.agent,
          slug: worker.slug,
          paneId: worker.paneId,
        };
        if (kind === "timed_out" || kind === "error") {
          event.detail = waitResultDetail(result.stdout, result.stderr);
        }
        rt.pendingEvents.push(event);
        const waiting = rt.waiters.splice(0);
        for (const wake of waiting) wake();
      },
    );
  }

  pi.registerTool({
    name: "swarm_spawn",
    label: "Swarm spawn",
    description:
      "Spawn recursive pi workers for a batch of READY backlog items, one herdr pane each, up to the concurrency cap.",
    promptSnippet: "Spawn concurrent pi workers for a batch of backlog items via herdr",
    promptGuidelines: [
      "Panes are split sequentially (herdr pane split mutates shared layout state -- concurrent splits race), then agent start+prompt run concurrently across the resulting panes.",
      "A per-item spawn failure (agent_not_ready, agent_prompt_failed, permission_gate_not_disabled, agent_prompt_stalled, spawn_error, or an unparseable pane-split response) is reported in `failed`, not thrown -- other items in the batch are unaffected. Any failure after the pane exists carries that pane's captured output in its reason, and the pane is closed.",
      "Call swarm_poll next to begin the completion loop.",
    ],
    parameters: Type.Object({
      runId: Type.String({
        description:
          "Identifier for this swarm run -- reused across spawn/poll/resolve calls, and to recover state after a restart.",
      }),
      items: Type.Array(Type.String(), {
        description: "Backlog item slugs to spawn, in queue order.",
      }),
      concurrency: Type.Optional(
        Type.Number({ description: "Max concurrent active workers. Default 3." }),
      ),
    }),
    async execute(_toolCallId, params) {
      const typed = params as { runId: string; items: string[]; concurrency?: number };
      // Everything from reading the pool to pushing the new workers runs
      // under the run's lock -- measuring the budget and acting on it have to
      // be one step, or a second caller measures a pool this one is about to
      // fill. See withSpawnLock.
      return withSpawnLock(typed.runId, async () => {
        const state = await getOrInitState(typed.runId, typed.concurrency ?? DEFAULT_CONCURRENCY);

        const budget = spawnBudget(state, typed.items.length);
        const toSpawn = typed.items.slice(0, budget);

        const splitPanes: {
          slug: string;
          paneId?: string;
          failed?: { slug: string; reason: string };
        }[] = [];
        // Measure before carving. Splitting `--current` once per worker halves
        // the same pane every time, which is how a 168-column tab produced
        // 21-column workers; the plan splits each new pane instead, with a ratio
        // that lands every pane on an equal share.
        const layoutResult = await herdr(pi, buildPaneLayoutArgv());
        const startRect =
          layoutResult.code === 0
            ? parseCurrentPaneRect(layoutResult.stdout, process.env.HERDR_PANE_ID)
            : undefined;
        const plan = planSplits(startRect, toSpawn);
        for (const rejection of plan.rejected) {
          splitPanes.push({ slug: rejection.slug, failed: rejection });
        }

        // Each step splits the pane the previous step created, so the target
        // walks outward; the first one splits the orchestrator's own pane.
        let splitTarget: string | undefined;
        for (const step of plan.steps) {
          const result = await herdr(
            pi,
            buildPaneSplitArgv(step.direction, process.cwd(), {
              ...(splitTarget ? { paneId: splitTarget } : {}),
              ratio: step.ratio,
            }),
          );
          if (result.code !== 0) {
            splitPanes.push({
              slug: step.slug,
              failed: {
                slug: step.slug,
                reason: `pane split failed: ${result.stderr || result.stdout}`,
              },
            });
            continue;
          }
          try {
            const parsed = JSON.parse(result.stdout) as {
              result?: { pane?: { pane_id?: string } };
            };
            const paneId = parsed.result?.pane?.pane_id;
            if (!paneId) throw new Error("no pane_id in response");
            await herdr(pi, buildPaneRenameArgv(paneId, step.slug));
            splitPanes.push({ slug: step.slug, paneId });
            splitTarget = paneId;
          } catch (e) {
            splitPanes.push({
              slug: step.slug,
              failed: {
                slug: step.slug,
                reason: `could not parse pane split response: ${String(e)}`,
              },
            });
          }
        }

        const startResults = await Promise.allSettled(
          splitPanes.map(async (p): Promise<SpawnOutcome> => {
            if (p.failed || !p.paneId) return { slug: p.slug, failed: p.failed };
            const paneId = p.paneId;
            state.nextCounter += 1;
            const agentId = nextAgentId(typed.runId, state.nextCounter, p.slug);
            try {
              return await spawnInto(paneId, agentId, p.slug);
            } catch (e) {
              // A throw out of spawnInto's herdr calls is a post-split failure
              // like any other. Without this it lands in allSettled's rejected
              // branch, which files the failure against slug "unknown" and
              // leaves the pane open -- losing both the item's identity and the
              // pane text that would say what happened.
              return failWithPane(p.slug, paneId, `spawn_error: ${String(e)}`);
            }
          }),
        );

        const spawned: WorkerRecord[] = [];
        const failed: { slug: string; reason: string }[] = [];
        for (const r of startResults) {
          if (r.status === "fulfilled") {
            if ("worker" in r.value) spawned.push(r.value.worker);
            else if (r.value.failed) failed.push(r.value.failed);
          } else {
            failed.push({ slug: "unknown", reason: String(r.reason) });
          }
        }

        state.workers.push(...spawned);
        persist(state);

        const skipped = typed.items.slice(budget);

        const summary = `Spawned ${spawned.length} worker(s), ${failed.length} failed to spawn, ${skipped.length} skipped (cap).`;
        const reasons = failed.map((f) => `- ${f.slug}: ${reasonHeadline(f.reason)}`).join("\n");

        return {
          content: [
            {
              type: "text",
              text: failed.length ? `${summary}\n${reasons}` : summary,
            },
          ],
          details: { spawned, failed, skipped },
        };
      });
    },
  });

  pi.registerTool({
    name: "swarm_poll",
    label: "Swarm poll",
    description:
      "Wait for at least one active swarm worker to settle (blocked/finished/timed_out/error), returning every event currently queued.",
    promptSnippet: "Wait for swarm workers to settle and report events",
    promptGuidelines: [
      "Blocks until >=1 active worker settles. Returns an array -- process every event in it, relaying each blocked event to the user one at a time, before calling swarm_poll again.",
      "A blocked event is reported with the worker's prompt quoted verbatim from herdr -- never assume it's a diff or a yes/no. Never send a blocked worker another agent prompt except the actual answer via swarm_resolve_blocked -- any prompt is interpreted as the gate's answer.",
      "timed_out means herdr's own wait deadline genuinely elapsed. error means something else went wrong (the agent disappeared, a crash, an unrecognized response) -- both close the pane, free the slot, and get flagged in the digest, never silently retried. They are not the same failure: each event names its kind and, where there is one, the raw reason after it, in the reported text.",
      "finished/timed_out/error events already closed their pane and freed their slot; if the READY queue still has items and the cap has headroom, call swarm_spawn again for the next batch.",
    ],
    parameters: Type.Object({
      runId: Type.String(),
      timeoutMs: Type.Optional(
        Type.Number({
          description: `Per-worker wait timeout, applied the first time a given worker's wait is armed (a worker already being waited on keeps its original timeout). Default ${DEFAULT_WAIT_TIMEOUT_MS}.`,
        }),
      ),
    }),
    async execute(_toolCallId, params, signal) {
      const typed = params as { runId: string; timeoutMs?: number };
      const state = await getOrInitState(typed.runId, DEFAULT_CONCURRENCY);
      const timeoutMs = typed.timeoutMs ?? DEFAULT_WAIT_TIMEOUT_MS;
      const rt = getRuntime(typed.runId);

      const active = state.workers.filter((w) => w.lifecycle === "active");
      for (const w of active) armWait(rt, w, timeoutMs);

      if (active.length === 0 && rt.pendingEvents.length === 0) {
        // A worker parked at awaiting_relay is not an empty pool: it is a live
        // pi holding an open pane, waiting on an answer only the orchestrator
        // can give. Reporting it as nothing left to do ended the run with that
        // worker and its pane still there. It deliberately gets no armWait --
        // it is blocked on the orchestrator, not on herdr, so a wait would
        // never fire and the poll below would hang on a promise nothing
        // resolves. Naming it instead is what lets the run continue.
        const awaitingRelay = state.workers.filter((w) => w.lifecycle === "awaiting_relay");
        const text = awaitingRelay.length
          ? `No active workers to poll. ${awaitingRelay.length} worker(s) awaiting a relay -- answer each with swarm_resolve_blocked before polling again: ${awaitingRelay
              .map((w) => `${w.agent} (${w.slug}, pane ${w.paneId})`)
              .join(", ")}.`
          : "No active workers to poll.";
        return {
          content: [{ type: "text", text }],
          details: { events: [] as PollEvent[] },
        };
      }

      if (rt.pendingEvents.length === 0) {
        await new Promise<void>((resolve) => rt.waiters.push(resolve));
      }

      const events = rt.pendingEvents.splice(0);

      for (const event of events) {
        const worker = state.workers.find((w) => w.agent === event.agent);
        if (!worker) continue;
        if (event.kind === "blocked") {
          const getResult = await herdr(pi, buildAgentGetArgv(event.agent), signal);
          let readResult = await herdr(
            pi,
            buildAgentReadArgv(event.agent, BLOCKED_READ_LINES),
            signal,
          );
          let truncated = looksTruncated(readResult.stdout, BLOCKED_READ_LINES);
          if (truncated) {
            readResult = await herdr(
              pi,
              buildAgentReadArgv(event.agent, BLOCKED_READ_LINES_RETRY),
              signal,
            );
            truncated = looksTruncated(readResult.stdout, BLOCKED_READ_LINES_RETRY);
          }
          event.rawPrompt = readResult.stdout || getResult.stdout;
          event.truncated = truncated;
          worker.lifecycle = "awaiting_relay";
        } else {
          await herdr(pi, buildPaneCloseArgv(worker.paneId), signal);
          state.workers = state.workers.filter((w) => w.agent !== worker.agent);
        }
      }
      persist(state);

      return {
        content: [
          {
            type: "text",
            text: events
              .map((e) =>
                e.kind === "blocked"
                  ? `${e.slug} (${e.agent}) is blocked${e.truncated ? " -- content may be truncated, inspect pane " + e.paneId + " directly" : ""}:\n${e.rawPrompt}`
                  : `${e.slug} (${e.agent}) ${e.kind}${e.detail ? `: ${e.detail}` : ""}`,
              )
              .join("\n\n"),
          },
        ],
        details: { events },
      };
    },
  });

  pi.registerTool({
    name: "swarm_resolve_blocked",
    label: "Swarm resolve",
    description:
      "Answer a blocked worker's picker by navigating to the matching option and submitting it.",
    promptSnippet: "Answer a blocked swarm worker's picker",
    promptGuidelines: [
      "answer is matched against the blocked worker's currently rendered option labels (re-read fresh, not from a stale raw_prompt) -- pass the user's own words, not a paraphrase, so the match is against what they actually said.",
      "herdr agent prompt refuses a blocked agent outright -- this tool drives the picker via arrow-key navigation instead, the only way to answer it.",
      "Every outcome leads with its own marker word -- resolved:, needs_manual:, or relay_failed: -- and names the agent, its item slug and its pane, so branch on that word. If answer matches no listed option (or matches more than one ambiguously), the result is needs_manual: instead of a guess; relay it back to the user verbatim, pane and listed option labels included, rather than retrying blindly.",
      "Verifies the worker actually left `blocked` within a short window after submitting; if it didn't (pane closed, still stuck), the item is marked relay_failed rather than silently treated as resolved.",
      "Re-checks the target's pane_id against what it was spawned into immediately before sending any keys -- if herdr's agent-name-to-pane mapping ever drifted, this is what catches it (a stale mapping would make read/match agree with the wrong pane too, so this is a second, independent identity check, not a repeat of the read). A mismatch is reported as needs_manual: and sends nothing.",
    ],
    parameters: Type.Object({
      runId: Type.String(),
      agent: Type.String({
        description: "The synthetic agent id from a blocked swarm_poll event.",
      }),
      answer: Type.String({
        description:
          "The human's exact answer -- matched against the picker's listed option labels.",
      }),
    }),
    async execute(_toolCallId, params, signal) {
      const typed = params as { runId: string; agent: string; answer: string };
      const state = await getOrInitState(typed.runId, DEFAULT_CONCURRENCY);
      const worker = state.workers.find((w) => w.agent === typed.agent);
      if (!worker) {
        return {
          content: [
            {
              type: "text",
              text: `relay_failed: no tracked worker "${typed.agent}" in run ${typed.runId}.`,
            },
          ],
          details: { relayFailed: true, needsManual: false, slug: "", paneId: "" },
        };
      }

      const readResult = await herdr(
        pi,
        buildAgentReadArgv(typed.agent, BLOCKED_READ_LINES),
        signal,
      );
      const picker = parsePicker(readResult.stdout);
      const target = matchOption(typed.answer, picker.options);

      if (!target || picker.selectedIndex === null) {
        const optionList = picker.options.map((o) => `"${o.label}"`).join(", ");
        return {
          content: [
            {
              type: "text",
              text:
                `needs_manual: could not match "${typed.answer}" to exactly one listed option for ` +
                `${typed.agent} (${worker.slug}, pane ${worker.paneId}). Listed options: ` +
                `${optionList || "(none parsed)"}. Attach directly ` +
                `(herdr agent attach ${typed.agent}) or retry with text matching one option's ` +
                `label exactly.`,
            },
          ],
          details: {
            relayFailed: false,
            needsManual: true,
            slug: worker.slug,
            paneId: worker.paneId,
          },
        };
      }

      const getResult = await herdr(pi, buildAgentGetArgv(typed.agent), signal);
      if (paneIdentityMismatch(getResult.code, getResult.stdout, worker.paneId)) {
        return {
          content: [
            {
              type: "text",
              text:
                `needs_manual: ${typed.agent} (${worker.slug})'s pane identity no longer matches ` +
                `pane ${worker.paneId}, what it was spawned into -- refusing to send keys rather ` +
                `than risk hitting the wrong pane. Attach directly ` +
                `(herdr agent attach ${typed.agent}) to answer it by hand.`,
            },
          ],
          details: {
            relayFailed: false,
            needsManual: true,
            slug: worker.slug,
            paneId: worker.paneId,
          },
        };
      }

      const keysResult = await herdr(
        pi,
        buildAgentSendKeysArgv(typed.agent, navigationKeys(picker.selectedIndex, target.index)),
        signal,
      );
      if (keysResult.code !== 0) {
        await closeWorkerPane(worker.paneId, signal);
        state.workers = state.workers.filter((w) => w.agent !== typed.agent);
        persist(state);
        return {
          content: [
            {
              type: "text",
              text: `relay_failed: could not send navigation keys to ${typed.agent}: ${keysResult.stderr || keysResult.stdout}`,
            },
          ],
          details: {
            relayFailed: true,
            needsManual: false,
            slug: worker.slug,
            paneId: worker.paneId,
          },
        };
      }

      // Wait for the states a worker that ANSWERED reaches, and let a worker
      // that did not answer fall out as a timeout. Both halves are measured
      // against real herdr 0.8.2, not assumed:
      //
      //   `working` -- a correct answer resumes the worker's turn, so it goes
      //   blocked -> working and stays there for as long as the turn runs
      //   (3.7s in one measured run, indefinitely for real work). The old set
      //   asked only for idle/done/blocked, so herdr waited out the whole
      //   window and returned a timeout, reported here as relay_failed on
      //   what is the normal success path. Only an answer that happened to
      //   finish its turn inside 5s was ever called resolved.
      //
      //   no `blocked` -- herdr does not observe the answer instantly. The
      //   status stays `blocked` for the first ~90-156ms after send-keys
      //   (measured), which is longer than the single herdr call between
      //   send-keys and this wait. With `blocked` in the set, that race made
      //   herdr match it in ~2ms and report relay_failed on a successful
      //   answer. Leaving it out costs a genuinely stuck worker the full 5s
      //   before it fails, and buys correctness on every answer that worked.
      //
      // A worker that answers one picker straight into another is reported
      // resolved off its transient `working`; swarm_poll's own wait picks the
      // new block up, which is where a blocked worker is meant to surface.
      const verify = await herdr(
        pi,
        buildAgentWaitArgv(typed.agent, ["idle", "done", "working"], RESOLVE_VERIFY_TIMEOUT_MS),
        signal,
      );

      if (verify.code !== 0) {
        await closeWorkerPane(worker.paneId, signal);
        state.workers = state.workers.filter((w) => w.agent !== typed.agent);
        persist(state);
        return {
          content: [
            {
              type: "text",
              text: `relay_failed: ${typed.agent} did not resume within ${RESOLVE_VERIFY_TIMEOUT_MS} ms after "${target.label}" was submitted.`,
            },
          ],
          details: {
            relayFailed: true,
            needsManual: false,
            slug: worker.slug,
            paneId: worker.paneId,
          },
        };
      }

      worker.lifecycle = "active";
      persist(state);
      return {
        content: [
          {
            type: "text",
            text: `resolved: ${typed.agent} (${worker.slug}, pane ${worker.paneId}) answered "${target.label}", back in the active pool.`,
          },
        ],
        details: {
          relayFailed: false,
          needsManual: false,
          slug: worker.slug,
          paneId: worker.paneId,
        },
      };
    },
  });
}

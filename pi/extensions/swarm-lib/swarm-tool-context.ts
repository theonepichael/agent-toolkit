// Shared business logic for Pi and Copilot swarm orchestration tools:
// swarm_spawn, swarm_poll, swarm_amend, swarm_resolve_blocked.
// Implements host-parameterized execution, Copilot crash recovery with session-id
// resume, and host-selected state persistence.

import { randomUUID } from "node:crypto";
import { existsSync, mkdirSync, readFileSync, rmSync, writeFileSync } from "node:fs";
import { homedir } from "node:os";
import { join } from "node:path";
import { spawn } from "node:child_process";
import {
  canOpenNewPane,
  canSpawnNew,
  isSuspiciousFinish,
  itemPaths,
  nextAgentId,
  openPaneCount,
  openPaneSoftCap,
  parseReadyItems,
  parseShownItem,
  pendingAmendWorkers,
  selectSchedulable,
  spawnBudget,
  staleWorkerRecords,
  stalledRelayWorkers,
  type ReadyItem,
  type ExecutionMode,
  type PendingAmend,
  type SwarmState,
  type WorkerRecord,
} from "./swarm-scheduling";
import {
  AMEND_ACK_MAX_CHECKS,
  AMEND_ACK_TIMEOUT_MS,
  AMEND_INSTRUCTION,
  amendHoldExpired,
  amendHoldMs,
  amendHoldVerdict,
  amendOutstandingNote,
  amendVerdictDetail,
  buildAgentGetArgv,
  buildAgentListArgv,
  buildAgentPromptArgv,
  buildAgentReadArgv,
  buildAgentSendKeysArgv,
  buildAgentStartArgv,
  buildAgentWaitArgv,
  buildPaneReadArgv,
  buildTabCloseArgv,
  buildTabCreateArgv,
  buildTabListArgv,
  buildWorkerCloseArgv,
  classifyAmendAck,
  classifyResyncGet,
  classifyTimeoutProbe,
  classifyWaitResult,
  deadlineStopDetail,
  findTabByLabel,
  paneIdentityMismatch,
  parseAgentList,
  parseAgentSession,
  parseAgentStateSeq,
  parseAgentStatus,
  parseTabCreate,
  reasonHeadline,
  waitResultDetail,
  tabPresence,
  type AmendAckOutcome,
  type PollEventKind,
  type ProbeResult,
} from "./swarm-herdr";
import { copilotPickerAdapter, noteResolveFailure, type BlockClass } from "./swarm-picker-copilot";
import type { PickerAdapter } from "./swarm-picker";
import { matchOption, navigationKeys } from "./swarm-picker";

export function herdrStateDir(): string {
  return process.env.COPILOT_SWARM_STATE_DIR ?? join(homedir(), ".copilot", "state");
}

export function devStatusPath(): string {
  return (
    process.env.COPILOT_SWARM_DEV_STATUS_PATH ??
    join(homedir(), ".claude", "scripts", "dev_status.py")
  );
}

export function copilotPluginDir(): string {
  return (
    process.env.COPILOT_SWARM_PLUGIN_DIR ??
    join(homedir(), "Workspace", "agent-toolkit", "copilot", "extensions", "swarm")
  );
}

const DEFAULT_CONCURRENCY = 3;
const DEFAULT_WAIT_TIMEOUT_MS = 30 * 60 * 1000;
const DEFAULT_WORKER_DEADLINE_MS = 4 * 60 * 60 * 1000;
const DEFAULT_RELAY_STALL_MS = 30 * 60 * 1000;
const PROBE_TIMEOUT_MS = 15_000;
const PROMPT_ACK_TIMEOUT_MS = 10_000;
const PROMPT_ACK_PROCESS_TIMEOUT_MS = 15_000;
const PROMPT_ACK_STATES = ["working", "idle", "done", "blocked"] as const;
const RESOLVE_VERIFY_TIMEOUT_MS = 5_000;
const BLOCKED_READ_LINES = 500;
const BLOCKED_READ_LINES_RETRY = 2000;
export const PANE_CAPTURE_CHARS = 4000;
const PANE_CAPTURE_LINES = 200;
const MAX_RECOVERY_ATTEMPTS = 2;

export interface CaptureOffer {
  kind: string;
  id: string;
  summary: string;
}

export function statePath(runId: string, stateDir: string = herdrStateDir()): string {
  return join(stateDir, `swarm-${runId}.json`);
}

export function capturePath(
  runId: string,
  slug: string,
  stateDir: string = herdrStateDir(),
): string {
  const safeRun = runId.replace(/[^A-Za-z0-9._-]/g, "_");
  const safeSlug = (slug.split("/").pop() ?? "").replace(/[^A-Za-z0-9._-]/g, "_");
  return join(stateDir, `swarm-${safeRun}-capture-${safeSlug}.json`);
}

export function readCaptureOffers(
  runId: string,
  slug: string,
  stateDir: string = herdrStateDir(),
): CaptureOffer[] {
  const path = capturePath(runId, slug, stateDir);
  let raw: string;
  try {
    raw = readFileSync(path, "utf8");
  } catch {
    return [];
  }
  try {
    rmSync(path, { force: true });
  } catch {
    // Best effort.
  }
  try {
    const parsed: unknown = JSON.parse(raw);
    const offers =
      parsed && typeof parsed === "object" && "offers" in parsed
        ? (parsed as { offers: unknown }).offers
        : null;
    if (!Array.isArray(offers)) return [];
    return offers.flatMap((o): CaptureOffer[] => {
      if (!o || typeof o !== "object") return [];
      const rec = o as Record<string, unknown>;
      const kind = typeof rec.kind === "string" ? rec.kind : "";
      const id = typeof rec.id === "string" ? rec.id : "";
      const summary = typeof rec.summary === "string" ? rec.summary : "";
      return kind && id ? [{ kind, id, summary }] : [];
    });
  } catch {
    return [];
  }
}

export function renderCaptureOffers(offers: CaptureOffer[]): string {
  if (offers.length === 0) return "";
  return (
    "\n  Queued capture offers from this worker -- do NOT ask about them now; fold them into your single end-of-run digest walk: " +
    offers.map((c) => `[${c.kind}] ${c.id} -- ${c.summary}`).join("; ")
  );
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

export function buildReadyArgv(prefix?: string): string[] {
  const argv = ["python3", devStatusPath(), "ready"];
  return prefix ? [...argv, "--prefix", prefix] : argv;
}

export function buildShowArgv(slug: string): string[] {
  return ["python3", devStatusPath(), "show", slug];
}

export function buildRenderArgv(): string[] {
  return ["python3", devStatusPath(), "render"];
}

export function formatDuration(ms: number): string {
  const totalMinutes = Math.max(0, Math.round(ms / 60000));
  const hours = Math.floor(totalMinutes / 60);
  const minutes = totalMinutes % 60;
  return hours > 0 ? `${hours}h${String(minutes).padStart(2, "0")}m` : `${minutes}m`;
}

export function looksTruncated(content: string, requestedLines: number): boolean {
  return content.split("\n").length >= requestedLines;
}

export function elapsedWorkingMs(worker: WorkerRecord, now: number): number | null {
  const open =
    worker.workingSinceMs === undefined ? null : Math.max(0, now - worker.workingSinceMs);
  if (open === null && worker.accumulatedWorkingMs === undefined) return null;
  return (worker.accumulatedWorkingMs ?? 0) + (open ?? 0);
}

export function foldWorkingSegment(worker: WorkerRecord, now: number): void {
  if (worker.workingSinceMs === undefined) return;
  worker.accumulatedWorkingMs =
    (worker.accumulatedWorkingMs ?? 0) + Math.max(0, now - worker.workingSinceMs);
  worker.workingSinceMs = undefined;
}

export interface ExecResult {
  code: number;
  stdout: string;
  stderr: string;
}

export type ExecFn = (
  cmd: string,
  args: string[],
  opts?: { signal?: AbortSignal; timeout?: number },
) => Promise<ExecResult>;

export const defaultExec: ExecFn = async (cmd, args, opts) => {
  return new Promise<ExecResult>((resolve) => {
    let proc: ReturnType<typeof spawn>;
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
    let timer: NodeJS.Timeout | undefined;
    let timedOut = false;
    if (opts?.timeout) {
      timer = setTimeout(() => {
        timedOut = true;
        proc.kill();
      }, opts.timeout);
    }
    proc.on("error", (err) => {
      if (timer) clearTimeout(timer);
      resolve({ code: 1, stdout, stderr: `${stderr}\n${String(err)}` });
    });
    proc.on("close", (code) => {
      if (timer) clearTimeout(timer);
      // A kill()-ed process typically reports code null, which `code ?? 0`
      // would otherwise map straight to success -- every caller here treats
      // `code !== 0` as failure, so a timeout must never look like a clean exit.
      if (timedOut) {
        resolve({ code: 124, stdout, stderr: `${stderr}\n<timed out after ${opts?.timeout}ms>` });
        return;
      }
      resolve({ code: code ?? 0, stdout, stderr });
    });
  });
};

export interface PollEvent {
  kind: PollEventKind;
  agent: string;
  slug: string;
  paneId: string;
  rawPrompt?: string;
  truncated?: boolean;
  blockClass?: BlockClass;
  options?: string[];
  captures?: CaptureOffer[];
  detail?: string;
  elapsedMs?: number;
  checkIn?: number;
}

type SpawnOutcome =
  { worker: WorkerRecord } | { slug: string; failed?: { slug: string; reason: string } };

interface RunRuntime {
  runId: string;
  inFlight: Set<string>;
  pendingEvents: PollEvent[];
  waiters: (() => void)[];
  spawnChain: Promise<void>;
  timeoutMs: number;
  deadlineMs: number;
  stallMs: number;
}

const UUID_REGEX = /^[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/i;

export function isValidUuid(id: string): boolean {
  return UUID_REGEX.test(id);
}

export interface SwarmToolContextOptions {
  kind?: "pi" | "copilot";
  stateDir?: () => string;
  workerPrompt?: (slug: string) => string;
  defaultPluginDir?: () => string;
}

export class SwarmToolContext {
  private activeRuns = new Map<string, SwarmState>();
  private runtimes = new Map<string, RunRuntime>();
  public exec: ExecFn;

  constructor(
    exec: ExecFn = defaultExec,
    private readonly picker: PickerAdapter = copilotPickerAdapter,
    private readonly options: SwarmToolContextOptions = {},
  ) {
    this.exec = exec;
  }

  private get kind(): "pi" | "copilot" {
    return this.options.kind ?? "copilot";
  }

  private get stateDir(): string {
    return (this.options.stateDir ?? herdrStateDir)();
  }

  private workerPrompt(slug: string): string {
    return (this.options.workerPrompt ?? ((id: string) => `/backlog-item --auto ${id}`))(slug);
  }

  private defaultPluginDir(): string {
    return (this.options.defaultPluginDir ?? copilotPluginDir)();
  }

  private async herdr(argv: string[], signal?: AbortSignal, timeout?: number): Promise<ExecResult> {
    return this.exec("herdr", argv, { signal, timeout });
  }

  private getRuntime(runId: string): RunRuntime {
    let rt = this.runtimes.get(runId);
    if (!rt) {
      rt = {
        runId,
        inFlight: new Set(),
        pendingEvents: [],
        waiters: [],
        spawnChain: Promise.resolve(),
        timeoutMs: DEFAULT_WAIT_TIMEOUT_MS,
        deadlineMs: DEFAULT_WORKER_DEADLINE_MS,
        stallMs: DEFAULT_RELAY_STALL_MS,
      };
      this.runtimes.set(runId, rt);
    }
    return rt;
  }

  private async withSpawnLock<T>(runId: string, fn: () => Promise<T>): Promise<T> {
    const rt = this.getRuntime(runId);
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

  private waitForEvent(rt: RunRuntime, signal?: AbortSignal): Promise<boolean> {
    return new Promise<boolean>((resolve) => {
      let settled = false;
      const cleanup = (): void => {
        const i = rt.waiters.indexOf(wake);
        if (i !== -1) rt.waiters.splice(i, 1);
        signal?.removeEventListener("abort", onAbort);
      };
      const wake = (): void => {
        if (settled) return;
        settled = true;
        cleanup();
        resolve(true);
      };
      const onAbort = (): void => {
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

  private wakeWaiters(rt: RunRuntime): void {
    const waiting = rt.waiters.splice(0);
    for (const wake of waiting) wake();
  }

  private async closeWorker(worker: WorkerRecord, signal?: AbortSignal): Promise<boolean> {
    try {
      const result = await this.herdr(buildWorkerCloseArgv(worker), signal);
      return result.code === 0;
    } catch {
      return false;
    }
  }

  private teardownRecovery(worker: WorkerRecord): string {
    const close = worker.tabId
      ? `herdr tab close ${worker.tabId}`
      : `herdr pane close ${worker.paneId}`;
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
  private async harvestWorkerIO(
    state: SwarmState,
    worker: WorkerRecord,
    signal?: AbortSignal,
  ): Promise<CaptureOffer[]> {
    return (await this.harvestWorkerIOWithStatus(state, worker, signal)).offers;
  }

  private async harvestWorkerIOWithStatus(
    state: SwarmState,
    worker: WorkerRecord,
    signal?: AbortSignal,
  ): Promise<{ offers: CaptureOffer[]; closed: boolean }> {
    const offers = readCaptureOffers(state.runId, worker.slug, this.stateDir);
    let closed = await this.closeWorker(worker, signal);
    if (!closed && (state.mode ?? "concurrent") === "serial") {
      try {
        const [agent, tabs] = await Promise.all([
          this.herdr(buildAgentGetArgv(worker.agent), signal),
          this.herdr(buildTabListArgv(), signal),
        ]);
        const agentAbsent =
          classifyResyncGet(agent.code, agent.stdout, agent.stderr).action === "drop";
        const tabAbsent = worker.tabId
          ? tabs.code === 0 && tabPresence(tabs.stdout, worker.tabId) === false
          : false;
        closed = agentAbsent && tabAbsent;
      } catch {
        closed = false;
      }
    }
    try {
      rmSync(capturePath(state.runId, worker.slug, this.stateDir), { force: true });
    } catch {
      // Best effort
    }
    return { offers, closed };
  }

  public async teardownAndHarvestWorker(
    state: SwarmState,
    worker: WorkerRecord,
    signal?: AbortSignal,
  ): Promise<CaptureOffer[]> {
    const teardown = await this.harvestWorkerIOWithStatus(state, worker, signal);
    if ((state.mode ?? "concurrent") === "serial" && !teardown.closed) {
      worker.lifecycle = "teardown_ambiguous";
      worker.terminalOutcome = "error";
      worker.terminalCaptures = teardown.offers;
      worker.teardownDetail = this.teardownRecovery(worker);
    } else {
      state.workers = state.workers.filter((w) => w.agent !== worker.agent);
    }
    this.persist(state);
    return teardown.offers;
  }

  public async pruneStaleWorkers(state: SwarmState): Promise<string[]> {
    let listResult: ExecResult | undefined;
    for (let attempt = 0; attempt < 2; attempt++) {
      try {
        listResult = await this.herdr(buildAgentListArgv());
      } catch {
        listResult = undefined;
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
        const offers = [...(w.terminalCaptures ?? []), ...teardown.offers];
        const confirmedGone = entry === undefined || teardown.closed;
        if ((state.mode ?? "concurrent") === "serial" && !confirmedGone) {
          w.lifecycle = "teardown_ambiguous";
          w.teardownDetail = this.teardownRecovery(w);
        }
        const line =
          `${w.agent} (${w.slug}): stale worker record cleared -- herdr reports its agent ${
            status ?? "gone"
          } (finished or dead), but its finish was never reported through ` +
          "swarm_poll; outcome inferred, not observed. Verify the item's state before " +
          `treating it as complete.${
            // This path reaches a close without ever passing a settle, so an
            // outstanding amendment dies here. Closing anyway is correct -- a
            // refusal would strand the serial queue on a worker herdr already
            // calls gone -- but the hold being discarded has to be said.
            w.pendingAmend ? ` ${amendOutstandingNote(w.pendingAmend, Date.now())}` : ""
          }${renderCaptureOffers(offers)}`;
        return { worker: w, confirmedGone, line };
      }),
    );
    const removed = new Set(
      results
        .filter(({ confirmedGone }) => confirmedGone || (state.mode ?? "concurrent") !== "serial")
        .map(({ worker }) => worker.agent),
    );
    state.workers = state.workers.filter((w) => !removed.has(w.agent));
    const rt = this.runtimes.get(state.runId);
    if (rt) {
      for (const agent of removed) rt.inFlight.delete(agent);
      rt.pendingEvents = rt.pendingEvents.filter((e) => !removed.has(e.agent));
      this.wakeWaiters(rt);
    }
    this.persist(state);
    return results.map(({ worker, confirmedGone, line }) =>
      (state.mode ?? "concurrent") === "serial" && !confirmedGone
        ? `${worker.agent} (${worker.slug}): teardown remains ambiguous -- ${worker.teardownDetail}; the serial queue is paused.`
        : line,
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
  private async recoverTabByLabel(label: string): Promise<string | undefined> {
    try {
      const listing = await this.herdr(buildTabListArgv());
      if (listing.code !== 0) return undefined;
      const tabId = findTabByLabel(listing.stdout, label);
      if (!tabId) return undefined;
      const closed = await this.herdr(buildTabCloseArgv(tabId));
      return closed.code === 0 ? tabId : undefined;
    } catch {
      return undefined;
    }
  }

  private async failWithTab(
    slug: string,
    paneId: string,
    tabId: string,
    reason: string,
  ): Promise<{ slug: string; failed: { slug: string; reason: string } }> {
    let capture: string;
    try {
      const read = await this.herdr(buildPaneReadArgv(paneId, PANE_CAPTURE_LINES));
      capture =
        read.code === 0
          ? read.stdout.slice(-PANE_CAPTURE_CHARS)
          : `<pane capture failed: ${(read.stderr || read.stdout).slice(0, 200)}>`;
    } catch (e) {
      capture = `<pane capture threw: ${String(e)}>`;
    }
    try {
      await this.herdr(buildTabCloseArgv(tabId));
    } catch {
      // Best effort
    }
    return { slug, failed: { slug, reason: `${reason}\n--- pane ${paneId} ---\n${capture}` } };
  }

  private async spawnInto(
    paneId: string,
    tabId: string,
    agentId: string,
    slug: string,
    paths: string[],
    model?: string,
    pluginDir?: string,
  ): Promise<SpawnOutcome> {
    let sessionId: string | undefined = this.kind === "copilot" ? randomUUID() : undefined;
    const startResult = await this.herdr(
      buildAgentStartArgv(agentId, paneId, model, {
        kind: this.kind,
        sessionId,
        ...(this.kind === "copilot"
          ? { allowAllTools: true, pluginDir: pluginDir ?? this.defaultPluginDir() }
          : {}),
      }),
    );
    if (startResult.code !== 0) {
      return this.failWithTab(
        slug,
        paneId,
        tabId,
        `agent_not_ready: ${startResult.stderr || startResult.stdout}`,
      );
    }

    // Cross-check the requested sessionId against what herdr actually
    // reports. attemptCrashRecovery later resumes via --resume=<the value
    // stored here>, so if Copilot silently picked a different session id
    // than the one we requested, the confirmed value -- not our request --
    // is the only one that can ever be resumed successfully.
    if (sessionId)
      try {
        const getResult = await this.herdr(buildAgentGetArgv(agentId));
        if (getResult.code === 0) {
          const sessionVal = parseAgentSession(getResult.stdout);
          if (sessionVal && sessionVal !== sessionId) {
            process.stderr.write(
              `[swarm] ${agentId}: requested session-id ${sessionId} but herdr agent get ` +
                `reports ${sessionVal} -- using the confirmed value for crash recovery\n`,
            );
            sessionId = sessionVal;
          }
        }
      } catch {
        // Best effort check
      }

    // `agent prompt` without --wait only confirms submission. Arming the
    // terminal-state wait immediately afterward can then match the worker's
    // pre-turn idle state and close its tab before the turn starts. herdr's
    // prompt-specific wait requires an observed post-submit transition before
    // matching any of these states, so a successful return is the handoff ack.
    const promptResult = await this.herdr(
      buildAgentPromptArgv(agentId, this.workerPrompt(slug), {
        wait: true,
        until: PROMPT_ACK_STATES,
        timeoutMs: PROMPT_ACK_TIMEOUT_MS,
      }),
      undefined,
      PROMPT_ACK_PROCESS_TIMEOUT_MS,
    );
    if (promptResult.code !== 0) {
      return this.failWithTab(
        slug,
        paneId,
        tabId,
        `agent_prompt_stalled: ${promptResult.stderr || promptResult.stdout}`,
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
        lifecycle: "active" as const,
        ...(sessionId ? { copilotSessionId: sessionId, recoveryAttempts: 0 } : {}),
      },
    };
  }

  public async attemptCrashRecovery(
    state: SwarmState,
    worker: WorkerRecord,
    pluginDir?: string,
  ): Promise<boolean> {
    const attempts = worker.recoveryAttempts ?? 0;
    if (this.kind !== "copilot" || attempts >= MAX_RECOVERY_ATTEMPTS) return false;
    if (!worker.copilotSessionId || !isValidUuid(worker.copilotSessionId)) return false;

    // Launch a new tab with --resume=<copilotSessionId>
    const cwd = worker.cwd ?? process.cwd();
    const label = worker.slug.replace(/[^A-Za-z0-9._-]/g, "-").slice(0, 32);
    let tabCreated = await this.herdr(buildTabCreateArgv(cwd, label, { kind: "copilot" }));
    let parsedTab = parseTabCreate(tabCreated.stdout);
    if (!parsedTab && tabCreated.code === 0) {
      // A recovered tab id has no pane id attached (tab list doesn't carry
      // one), so it can never satisfy the paneId check below -- this is
      // cleanup only, matching pi: find the orphan by label and close it.
      await this.recoverTabByLabel(label);
      return false;
    }
    if (!parsedTab || !parsedTab.paneId) {
      return false;
    }

    // Close old tab
    if (worker.tabId) {
      try {
        await this.herdr(buildTabCloseArgv(worker.tabId));
      } catch {
        // Best effort
      }
    }

    // Start agent with resume
    const startResult = await this.herdr(
      buildAgentStartArgv(worker.agent, parsedTab.paneId, worker.model, {
        kind: "copilot",
        resumeSessionId: worker.copilotSessionId,
        allowAllTools: true,
        pluginDir: pluginDir ?? this.defaultPluginDir(),
      }),
    );
    if (startResult.code !== 0) {
      try {
        await this.herdr(buildTabCloseArgv(parsedTab.tabId));
      } catch {
        // Best effort
      }
      return false;
    }

    // Send explicit continuation prompt
    // This newly started agent is idle, and the caller re-arms its terminal
    // wait as soon as recovery returns. Require the same post-submit handoff
    // acknowledgement as an initial spawn so that wait cannot consume the
    // pre-turn idle state.
    const promptResult = await this.herdr(
      buildAgentPromptArgv(
        worker.agent,
        "Continue working on this backlog item where you left off.",
        {
          wait: true,
          until: PROMPT_ACK_STATES,
          timeoutMs: PROMPT_ACK_TIMEOUT_MS,
        },
      ),
      undefined,
      PROMPT_ACK_PROCESS_TIMEOUT_MS,
    );
    if (promptResult.code !== 0) {
      try {
        await this.herdr(buildTabCloseArgv(parsedTab.tabId));
      } catch {
        // Best effort
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

  public async getOrInitState(
    runId: string,
    concurrency: number,
    prefix?: string,
    pluginDir?: string,
    requestedMode?: ExecutionMode,
  ): Promise<SwarmState> {
    const cached = this.activeRuns.get(runId);
    if (cached) {
      const recordedMode = cached.mode ?? "concurrent";
      if (requestedMode !== undefined && requestedMode !== recordedMode) {
        throw new Error(
          `run ${runId} is persisted in ${recordedMode} mode; refusing requested ${requestedMode} mode`,
        );
      }
      return cached;
    }

    const loaded = loadState(runId, this.stateDir);
    if (!loaded) {
      const mode = requestedMode ?? "concurrent";
      const fresh: SwarmState = {
        runId,
        concurrency: mode === "serial" ? 1 : concurrency,
        mode,
        nextCounter: 0,
        workers: [],
        ...(prefix !== undefined ? { prefix } : {}),
        ...(pluginDir !== undefined ? { pluginDir } : {}),
      };
      this.activeRuns.set(runId, fresh);
      return fresh;
    }

    const recordedMode = loaded.mode ?? "concurrent";
    if (requestedMode !== undefined && requestedMode !== recordedMode) {
      throw new Error(
        `run ${runId} is persisted in ${recordedMode} mode; refusing requested ${requestedMode} mode`,
      );
    }

    const listResult = await this.herdr(buildAgentListArgv());
    const entries = listResult.code === 0 ? parseAgentList(listResult.stdout) : null;
    const liveIds = (entries ?? []).map((e) => e.id);

    if (this.kind === "copilot" && entries !== null) {
      // Reconcile and check for crash recovery on missing workers. The
      // run's own pluginDir travels with the persisted state -- a caller
      // supplies it once at spawn time, and recovery (which may happen long
      // after, or after a full process restart) must keep using that same
      // value rather than falling back to copilotPluginDir()'s guessed default.
      // Parallel: each call only ever mutates its own WorkerRecord's fields
      // (never a shared array), so concurrent recovery of several
      // simultaneously-crashed workers is safe -- and worth it, since a
      // machine restart is exactly the case where more than one crashes at once.
      const missing = loaded.workers.filter((w) => !liveIds.includes(w.agent));
      const recoveries = await Promise.all(
        missing.map(async (w) => ({
          agent: w.agent,
          recovered: await this.attemptCrashRecovery(loaded, w, loaded.pluginDir),
        })),
      );
      for (const { agent, recovered } of recoveries) {
        if (recovered) liveIds.push(agent);
      }
    }

    const reconciled =
      entries === null || recordedMode === "serial"
        ? loaded
        : reconcileState(loaded, liveIds).state;
    this.activeRuns.set(runId, reconciled);
    saveState(reconciled, this.stateDir);
    return reconciled;
  }

  public persist(state: SwarmState): void {
    this.activeRuns.set(state.runId, state);
    saveState(state, this.stateDir);
  }

  private async probeLiveness(agentId: string): Promise<ProbeResult> {
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

  private armWait(rt: RunRuntime, worker: WorkerRecord): void {
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
  private armAmendAckWait(rt: RunRuntime, worker: WorkerRecord): void {
    if (rt.inFlight.has(worker.agent)) return;
    rt.inFlight.add(worker.agent);
    void this.settleAmendAck(rt, worker);
  }

  /** Persist a mutated worker through the run's cached state. */
  private persistRun(rt: RunRuntime): void {
    const state = this.activeRuns.get(rt.runId);
    if (state) this.persist(state);
  }

  /**
   * Decide what a terminal settle means while an amendment is outstanding.
   *
   * Returns the event to report, or null when the hold was armed instead and
   * nothing should be reported yet. The hold can only ever be armed together
   * with a wait -- an un-closeable worker with nothing armed would hold its slot
   * forever, which is a worse failure than the silent loss this prevents.
   */
  private amendHoldDecision(
    rt: RunRuntime,
    worker: WorkerRecord,
    pending: PendingAmend,
    settleStdout: string,
  ): { event?: PollEvent; rearm?: "terminal" | "ack" } {
    const now = Date.now();
    const seq = parseAgentStateSeq(settleStdout);
    if (seq !== undefined) pending.lastObservedSeq = seq;

    // The amended turn finished: that is the acknowledgement.
    if (pending.phase === "await_terminal") {
      const detail = amendVerdictDetail("confirmed", worker, amendHoldMs(pending, now));
      worker.pendingAmend = undefined;
      this.persistRun(rt);
      return {
        event: {
          kind: "finished",
          agent: worker.agent,
          slug: worker.slug,
          paneId: worker.paneId,
          detail,
        },
      };
    }

    // Measured here because the armed wait returns promptly on the transition,
    // so now is when the run ended -- and how long it ran after the correction
    // landed is the second release axis. No later hold may overwrite it.
    if (pending.runWorkedAfterAmendMs < 0) {
      pending.runWorkedAfterAmendMs = Math.max(0, now - pending.requestedAtMs);
    }

    if (amendHoldExpired(pending, now)) {
      const verdict = amendHoldVerdict({
        turnObserved: false,
        runWorkedMs: pending.runWorkedAfterAmendMs,
      });
      const detail = amendVerdictDetail(verdict, worker, amendHoldMs(pending, now));
      worker.pendingAmend = undefined;
      this.persistRun(rt);
      return {
        event: {
          kind: "finished",
          agent: worker.agent,
          slug: worker.slug,
          paneId: worker.paneId,
          detail,
        },
      };
    }

    pending.checks += 1;
    // One check-in per hold, not one per settle: a held worker that keeps
    // settling must not flood the stream the orchestrator drains, and a
    // check-in is not an outcome -- it frees no slot.
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
        detail: `amendment held: ${pending.checks}/${AMEND_ACK_MAX_CHECKS} ack watches armed, watching for the correction's turn to start before this worker can be finished off.`,
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
  private async settleAmendAck(rt: RunRuntime, worker: WorkerRecord): Promise<void> {
    let event: PollEvent | null = null;
    let rearm: "terminal" | "ack" | null = null;
    try {
      const pending = worker.pendingAmend;
      if (!pending) return; // cleared by another path while this wait was armed

      const ack = await this.herdr(
        buildAgentWaitArgv(worker.agent, ["working", "blocked"], AMEND_ACK_TIMEOUT_MS),
      );
      const ackStatus = ack.code === 0 ? parseAgentStatus(ack.stdout) : undefined;

      let outcome: AmendAckOutcome;
      let probe: ProbeResult | null = null;
      if (ackStatus === "working" || ackStatus === "blocked") {
        // A turn began after the idle we held. The active turn that produced
        // that idle can no longer be matched (herdr matches current state), so
        // this is a genuinely new turn.
        outcome = ackStatus === "working" ? "turn_started" : "parked";
      } else {
        probe = await this.probeLiveness(worker.agent);
        outcome = probe.abandoned
          ? "inconclusive"
          : classifyAmendAck(probe.code, probe.stdout, probe.stderr, pending.lastObservedSeq);
      }

      const now = Date.now();
      switch (outcome) {
        case "turn_started":
        case "changed_while_unarmed": {
          // The turn is running, or ran and settled inside the gap between the
          // two waits. Either way the correction is in hand: wait for the
          // terminal settle instead, and report it as confirmed. Re-arming a
          // terminal wait here rather than releasing immediately is what keeps
          // the amended turn's own output from being cut off mid-flight.
          pending.phase = "await_terminal";
          const seen =
            ackStatus === "working"
              ? parseAgentStateSeq(ack.stdout)
              : probe
                ? parseAgentStateSeq(probe.stdout)
                : undefined;
          if (seen !== undefined) pending.lastObservedSeq = seen;
          rearm = "terminal";
          break;
        }
        case "parked": {
          // The amendment's turn started and stopped at a gate. A relay is never
          // something to withhold, so report it -- and keep the hold, because
          // the correction still has to finish its work once answered.
          pending.phase = "await_terminal";
          rearm = null;
          event = {
            kind: "blocked",
            agent: worker.agent,
            slug: worker.slug,
            paneId: worker.paneId,
          };
          break;
        }
        case "gone": {
          worker.pendingAmend = undefined;
          this.persistRun(rt);
          event = {
            kind: "error",
            agent: worker.agent,
            slug: worker.slug,
            paneId: worker.paneId,
            detail: `worker vanished while an amendment was outstanding. ${amendOutstandingNote(pending, now)}`,
          };
          break;
        }
        case "settled_unchanged":
        case "inconclusive": {
          pending.checks += 1;
          if (amendHoldExpired(pending, now)) {
            // The steering axis is measured at the hold, where the armed wait
            // first reported the run over; re-deriving it from `now` here would
            // count the ack watches themselves as working time and turn every
            // expiry into a false `amend_steered`.
            const runWorkedMs =
              pending.runWorkedAfterAmendMs >= 0
                ? pending.runWorkedAfterAmendMs
                : Math.max(0, now - pending.requestedAtMs);
            const verdict = amendHoldVerdict({ turnObserved: false, runWorkedMs });
            const heldMs = amendHoldMs(pending, now);
            worker.pendingAmend = undefined;
            this.persistRun(rt);
            event = {
              kind: "finished",
              agent: worker.agent,
              slug: worker.slug,
              paneId: worker.paneId,
              detail: amendVerdictDetail(verdict, worker, heldMs),
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
        detail: `amend_ack_wait_failed: ${err instanceof Error ? err.message : String(err)}`,
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

  private async settleWait(rt: RunRuntime, worker: WorkerRecord, timeoutMs: number): Promise<void> {
    let event: PollEvent | null = null;
    let rearmAfter: "terminal" | "ack" | null = null;
    try {
      const result = await this.herdr(
        buildAgentWaitArgv(worker.agent, ["idle", "done", "blocked"], timeoutMs),
      );
      let kind = classifyWaitResult(result.code, result.stdout, result.stderr);
      let detail =
        kind === "timed_out" || kind === "error"
          ? waitResultDetail(result.stdout, result.stderr)
          : undefined;

      if (kind === "timed_out") {
        const probe = await this.probeLiveness(worker.agent);
        const verdict = classifyTimeoutProbe(
          probe,
          this.elapsedWorkingMsFor(worker),
          rt.deadlineMs,
        );
        if (verdict.disposition === "rearm") {
          const runState = this.activeRuns.get(rt.runId);
          if (runState && !runState.workers.some((w) => w.agent === worker.agent)) {
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
            checkIn: worker.checkIns,
          });
          this.wakeWaiters(rt);
          return;
        }

        // Gone detection on mid-poll probe
        if (verdict.kind === "error") {
          const state = this.activeRuns.get(rt.runId);
          if (state && (await this.attemptCrashRecovery(state, worker, state.pluginDir))) {
            rt.inFlight.delete(worker.agent);
            this.armWait(rt, worker);
            return;
          }
        }

        kind = verdict.kind;
        detail =
          kind === "timed_out"
            ? deadlineStopDetail(worker, rt.deadlineMs, {
                livenessConfirmed: verdict.livenessConfirmed === true,
                probeDetail: probe.abandoned
                  ? `the liveness probe did not answer within ${PROBE_TIMEOUT_MS} ms and was abandoned`
                  : `probe: ${waitResultDetail(probe.stdout, probe.stderr)}`,
              })
            : kind === "error"
              ? `probe: ${waitResultDetail(probe.stdout, probe.stderr)}`
              : undefined;
      }

      // A correction is in flight for this worker. Its turn may not have begun
      // yet, so this settle is not evidence of anything -- holding it is the
      // entire point, because the alternative is closing the tab with the
      // amendment unread. `blocked` is deliberately NOT held: a gate has to
      // reach the orchestrator for relay or the run deadlocks on a picker
      // nobody will answer.
      const pending = worker.pendingAmend;
      if (pending && kind === "finished") {
        const decision = this.amendHoldDecision(rt, worker, pending, result.stdout);
        if (decision.event) {
          event = decision.event;
        } else {
          rearmAfter = decision.rearm ?? null;
          event = null;
        }
      } else {
        event = { kind, agent: worker.agent, slug: worker.slug, paneId: worker.paneId };
        if (detail !== undefined) event.detail = detail;
        if (pending && (kind === "error" || kind === "timed_out")) {
          // Never suppress these, but never let them read as a clean end either:
          // the correction died here, and that has to be said.
          event.detail =
            `${event.detail ?? ""} ${amendOutstandingNote(pending, Date.now())}`.trim();
        }
      }
    } catch (err) {
      event = {
        kind: "error",
        agent: worker.agent,
        slug: worker.slug,
        paneId: worker.paneId,
        detail: `wait_failed: ${err instanceof Error ? err.message : String(err)}`,
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
      // Armed after the `finally` cleared the flag, so exactly one wait is live
      // for this worker -- arming inside the try would leave the flag cleared by
      // the `finally` and let the next poll stack a second wait on top.
      this.armAmendAckWait(rt, worker);
      return;
    }
    if (rearmAfter === "terminal") {
      this.armWait(rt, worker);
      return;
    }
    if (event === null) {
      // Held with nothing to report; the ack wait owns the next wake-up.
      return;
    }
    rt.pendingEvents.push(event);
    this.wakeWaiters(rt);
  }

  private elapsedWorkingMsFor(worker: WorkerRecord): number | null {
    const now = Date.now();
    if (worker.workingSinceMs === undefined && worker.accumulatedWorkingMs === undefined) {
      worker.workingSinceMs = now;
    }
    return elapsedWorkingMs(worker, now);
  }

  public async swarmSpawn(params: {
    runId: string;
    items?: string[];
    prefix?: string;
    concurrency?: number;
    model?: string;
    pluginDir?: string;
    mode?: ExecutionMode;
  }): Promise<{ content: { type: string; text: string }[]; details: Record<string, unknown> }> {
    if (!params.items && !params.prefix) {
      throw new Error(
        "swarm_spawn needs either `items` or `prefix`. Selecting from the whole READY queue " +
          "unscoped would pull unrelated projects into this run.",
      );
    }
    if (params.mode === "serial" && params.concurrency !== undefined && params.concurrency !== 1) {
      throw new Error("serial mode requires concurrency 1 when concurrency is supplied");
    }
    return this.withSpawnLock(params.runId, async () => {
      const state = await this.getOrInitState(
        params.runId,
        params.mode === "serial" ? 1 : (params.concurrency ?? DEFAULT_CONCURRENCY),
        params.prefix,
        params.pluginDir,
        params.mode,
      );
      if (state.mode === "serial") {
        state.concurrency = 1;
      } else if (params.concurrency !== undefined) {
        state.concurrency = params.concurrency;
      }
      if (params.pluginDir !== undefined) state.pluginDir = params.pluginDir;

      const pruneLines = await this.pruneStaleWorkers(state);

      if (!canSpawnNew(state)) {
        return {
          content: [
            {
              type: "text",
              text:
                `Concurrency cap reached (${state.concurrency} active workers). ` +
                "Call swarm_poll to wait for workers to settle.",
            },
          ],
          details: {
            spawned: [],
            failed: [],
            skipped: params.items ?? [],
            deferred: [],
            refused: [],
          },
        };
      }
      if (!canOpenNewPane(state)) {
        return {
          content: [
            {
              type: "text",
              text:
                `Open-pane soft cap reached (${openPaneCount(state)}/${openPaneSoftCap(state.concurrency)} open panes). ` +
                "Parked workers awaiting a relay are holding panes -- answer each with swarm_resolve_blocked before spawning more.",
            },
          ],
          details: { spawned: [], failed: [], skipped: [], deferred: [], refused: [] },
        };
      }

      let candidates: ReadyItem[];
      if (params.items) {
        const explicitSlugs = params.items;
        const readyResult = await this.exec("python3", buildReadyArgv(params.prefix).slice(1), {
          timeout: PROBE_TIMEOUT_MS,
        });
        if (readyResult.code !== 0) {
          throw new Error(
            `dev_status.py ready failed: ${readyResult.stderr || readyResult.stdout}`,
          );
        }
        const parsed = parseReadyItems(readyResult.stdout);
        const byId = new Map(parsed.map((i) => [i.id, i]));
        // A slug missing from the ready set (mistyped, stale, or genuinely
        // not worker-safe) must NOT default to worker_safe: true -- leaving
        // it unset lets selectSchedulable's fail-closed refusal apply, same
        // as an automatically-selected candidate that failed this lookup.
        candidates = explicitSlugs.map((id) => byId.get(id) ?? { id });
      } else {
        const readyResult = await this.exec("python3", buildReadyArgv(params.prefix).slice(1), {
          timeout: PROBE_TIMEOUT_MS,
        });
        if (readyResult.code !== 0) {
          throw new Error(
            `dev_status.py ready failed: ${readyResult.stderr || readyResult.stdout}`,
          );
        }
        candidates = parseReadyItems(readyResult.stdout);
        const attempted = new Set(state.attempted ?? []);
        const refused = new Set((state.refused ?? []).map((entry) => entry.slug));
        candidates = candidates.filter((c) => !attempted.has(c.id) && !refused.has(c.id));
      }

      const takenPaths: { path: string; holder: string }[] = [];
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
        state.mode ?? "concurrent",
      );
      const toSpawn = selection.slugs;

      const spawned: WorkerRecord[] = [];
      const failed: { slug: string; reason: string }[] = [];

      // Sequential, mirroring pi: tab creation mutates shared workspace
      // state. Only the phase after -- the per-worker herdr round-trips in
      // spawnInto -- is parallelized, via Promise.allSettled below.
      const tabs: { slug: string; created: { paneId: string; tabId: string } }[] = [];
      for (const slug of toSpawn) {
        const captureFile = capturePath(state.runId, slug, this.stateDir);
        const tabCreated = await this.herdr(
          buildTabCreateArgv(process.cwd(), slug, { captureFile, kind: this.kind }),
        );
        let parsedTab = parseTabCreate(tabCreated.stdout);
        if (!parsedTab && tabCreated.code === 0) {
          const orphan = await this.recoverTabByLabel(slug);
          const head = (tabCreated.stderr || tabCreated.stdout).slice(0, 200);
          failed.push({
            slug,
            reason: orphan
              ? `could not parse tab create response; the tab it created was found by label and closed (${orphan}): ${head}`
              : `could not parse tab create response, and no single tab labelled "${slug}" was found -- a tab may be open and unaccounted for, close it by hand: ${head}`,
          });
          continue;
        }
        if (!parsedTab || !parsedTab.paneId) {
          failed.push({
            slug,
            reason: `tab_create_failed: ${tabCreated.stderr || tabCreated.stdout}`,
          });
          continue;
        }
        tabs.push({ slug, created: parsedTab });
      }

      const startResults = await Promise.allSettled(
        tabs.map(async (t): Promise<SpawnOutcome> => {
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
              params.pluginDir,
            );
          } catch (e) {
            // A throw out of spawnInto's herdr calls is a post-create failure
            // like any other -- without this it lands in allSettled's
            // rejected branch, losing both the item's identity and the tab
            // that would otherwise get closed.
            return this.failWithTab(t.slug, paneId, tabId, `spawn_error: ${String(e)}`);
          }
        }),
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

      state.attempted = [...new Set([...(state.attempted ?? []), ...toSpawn])];
      const refusedBySlug = new Map(
        [...(state.refused ?? []), ...selection.refused].map((entry) => [entry.slug, entry]),
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
        `${refused.length} refused (not ${state.mode === "serial" ? "serial-safe" : "worker-safe"})`,
      ];
      const lines = [`${parts.join(", ")}.`];
      lines.push(...pruneLines);
      for (const f of failed) lines.push(`- ${f.slug}: ${reasonHeadline(f.reason)}`);
      for (const d of deferred) lines.push(`- ${d.slug}: deferred -- ${d.reason}`);
      for (const r of refused) lines.push(`- ${r.slug}: refused -- ${r.reason}`);
      if (spawned.length === 0 && deferred.length > 0) {
        lines.push(
          "Nothing spawned but items remain: poll the running workers, then call swarm_spawn again once one finishes.",
        );
      } else if (spawned.length === 0 && refused.length > 0) {
        lines.push(
          "Nothing spawned and the remaining items are refused, not waiting: they are never schedulable by a worker. " +
            "This is the end of the swarm phase for this prefix -- report them as needing a normal session rather than polling or spawning again.",
        );
      }
      if (
        (state.mode ?? "concurrent") === "serial" &&
        spawned.length === 0 &&
        failed.length === 0 &&
        skipped.length === 0 &&
        deferred.length === 0 &&
        refused.length === 0 &&
        state.workers.length === 0
      ) {
        const dashboard = await this.exec("python3", buildRenderArgv().slice(1), {
          timeout: PROBE_TIMEOUT_MS,
        });
        lines.push(
          dashboard.code === 0
            ? `Serial queue is quiescent. Current dashboard:\n${dashboard.stdout.trimEnd()}`
            : `Serial queue is quiescent, but dev_status.py render failed: ${dashboard.stderr || dashboard.stdout}`,
        );
      }

      return {
        content: [{ type: "text", text: lines.join("\n") }],
        details: { spawned, failed, skipped, deferred, refused },
      };
    });
  }

  public async swarmPoll(
    params: {
      runId: string;
      timeoutMs?: number;
      workerDeadlineMs?: number;
      relayStallMs?: number;
    },
    signal?: AbortSignal,
  ): Promise<{ content: { type: string; text: string }[]; details: Record<string, unknown> }> {
    const state = await this.getOrInitState(params.runId, DEFAULT_CONCURRENCY);
    const rt = this.getRuntime(params.runId);
    rt.timeoutMs = params.timeoutMs ?? DEFAULT_WAIT_TIMEOUT_MS;
    rt.deadlineMs = params.workerDeadlineMs ?? DEFAULT_WORKER_DEADLINE_MS;
    rt.stallMs = params.relayStallMs ?? DEFAULT_RELAY_STALL_MS;

    const parkedNow = state.workers.filter((w) => w.lifecycle === "awaiting_relay");
    const resyncNotes: string[] = [];
    if (parkedNow.length > 0) {
      const gets = await Promise.all(
        parkedNow.map(async (worker) => {
          try {
            const r = await this.herdr(buildAgentGetArgv(worker.agent), signal);
            return { worker, verdict: classifyResyncGet(r.code, r.stdout, r.stderr) };
          } catch {
            return { worker, verdict: { action: "keep" as const } };
          }
        }),
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
            detail:
              (state.mode ?? "concurrent") === "serial"
                ? "resync: the worker disappeared while awaiting a relay; that relay is cancelled and the item outcome is failed, not inferred complete."
                : "resync: agent gone from herdr while its record said awaiting_relay -- " +
                  "its gate was likely answered out-of-band (direct pane keys) and the " +
                  "worker has since finished or exited; outcome inferred, not observed. " +
                  "Verify the item's state before treating it as complete.",
          });
        } else if (verdict.action === "unpark") {
          worker.workingSinceMs = resumedAt;
          worker.awaitingRelaySinceMs = undefined;
          worker.lastResolveFailure = undefined;
          worker.lifecycle = "active";
          resyncNotes.push(
            `${worker.agent} (${worker.slug}) was parked awaiting a relay, but herdr now reports it unblocked -- resumed tracking as active.`,
          );
        }
      }
      if (resyncNotes.length > 0) this.persist(state);
    }

    const stampNow = Date.now();
    for (const w of state.workers) {
      if (w.lifecycle === "awaiting_relay" && w.awaitingRelaySinceMs === undefined) {
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
      const describe = (w: WorkerRecord) =>
        `${w.agent} (${w.slug}, pane ${w.paneId})` +
        (stalledAgents.has(w.agent)
          ? ` -- STALLED, over ${formatDuration(rt.stallMs)} with no answer`
          : "") +
        (w.lastResolveFailure
          ? ` -- a previous answer ${JSON.stringify(w.lastResolveFailure.answer)} failed to land (${w.lastResolveFailure.reason}); re-read the pane and answer with its EXACT rendered label`
          : "");
      const text =
        (awaitingRelay.length
          ? `No active workers to poll. ${awaitingRelay.length} worker(s) awaiting a relay -- answer each with swarm_resolve_blocked before polling again: ${awaitingRelay
              .map(describe)
              .join(", ")}.`
          : ambiguous.length
            ? `No active workers to poll. ${ambiguous.length} worker(s) have ambiguous teardown and still occupy the serial slot: ${ambiguous
                .map((w) => `${w.agent} (${w.slug}, pane ${w.paneId})`)
                .join(", ")}. Reconcile or close them before spawning the next item.`
            : "No active workers to poll.") +
        (goneNoteLines.length ? `\n\n${goneNoteLines.join("\n")}` : "");
      return {
        content: [{ type: "text", text }],
        details: { events: [] as PollEvent[] },
      };
    }

    let aborted = false;
    while (rt.pendingEvents.length === 0) {
      if (state.workers.filter((w) => w.lifecycle === "active").length === 0) {
        break;
      }
      if (!(await this.waitForEvent(rt, signal))) {
        aborted = true;
        break;
      }
    }

    if (aborted) {
      return {
        content: [
          {
            type: "text",
            text: "swarm_poll aborted before any worker settled. Workers are untouched and still running -- poll again to pick their events back up.",
          },
        ],
        details: { events: [] as PollEvent[] },
      };
    }

    const rawEvents = rt.pendingEvents.splice(0);
    // Snapshot once, up front: worker removal is batched at the very end
    // (see toRemove below) instead of per-event, so every event in this
    // poll resolves its worker against the same pre-removal view.
    const workersByAgent = new Map(state.workers.map((w) => [w.agent, w]));
    const toRemove = new Set<string>();

    const processed = await Promise.all(
      rawEvents.map(async (event): Promise<PollEvent | null> => {
        const worker = workersByAgent.get(event.agent);
        if (!worker) return null;
        if (event.kind === "blocked") {
          let getResult: ExecResult;
          try {
            getResult = await this.herdr(buildAgentGetArgv(event.agent), signal);
          } catch {
            getResult = { code: 1, stdout: "", stderr: "" };
          }
          const resyncVerdict = classifyResyncGet(
            getResult.code,
            getResult.stdout,
            getResult.stderr,
          );
          if (resyncVerdict.action === "drop") {
            event.kind = (state.mode ?? "concurrent") === "serial" ? "error" : "finished";
            event.detail =
              (state.mode ?? "concurrent") === "serial"
                ? "resync: the worker disappeared while resolving its blocked prompt; the relay is cancelled and the item outcome is failed."
                : "resync: agent gone from herdr while resolving blocked prompt -- " +
                  "worker has since finished or exited; outcome inferred, not observed. " +
                  "Verify the item's state before treating it as complete.";
            // harvestWorkerIO, not teardownAndHarvestWorker: several events
            // in this same poll can each be tearing down a different
            // worker concurrently, and teardownAndHarvestWorker's own
            // filter-and-reassign of state.workers would race across them.
            // Removal happens once, in a single batch, after this Promise.all.
            const teardown = await this.harvestWorkerIOWithStatus(state, worker, signal);
            event.captures = teardown.offers;
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
          let readResult: ExecResult;
          try {
            readResult = await this.herdr(
              buildAgentReadArgv(event.agent, BLOCKED_READ_LINES),
              signal,
            );
          } catch {
            readResult = { code: 1, stdout: "", stderr: "" };
          }
          let truncated = looksTruncated(readResult.stdout, BLOCKED_READ_LINES);
          if (truncated) {
            try {
              readResult = await this.herdr(
                buildAgentReadArgv(event.agent, BLOCKED_READ_LINES_RETRY),
                signal,
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
          // Check-in
        } else {
          const teardown = await this.harvestWorkerIOWithStatus(state, worker, signal);
          event.captures = teardown.offers;
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
      }),
    );
    const events = processed.filter((e): e is PollEvent => e !== null);
    if (toRemove.size > 0) {
      state.workers = state.workers.filter((w) => !toRemove.has(w.agent));
    }
    this.persist(state);

    await Promise.all(
      events
        .filter((e) => e.kind === "finished")
        .map(async (event) => {
          try {
            const result = await this.exec("python3", buildShowArgv(event.slug).slice(1), {
              signal,
              timeout: PROBE_TIMEOUT_MS,
            });
            if (result.code !== 0) return;
            const shown = parseShownItem(result.stdout);
            if (shown === null || event.detail !== undefined) return;
            if (isSuspiciousFinish(shown.status, (event.captures ?? []).length)) {
              event.detail =
                `dev_status.py still shows status ${JSON.stringify(shown.status)} and zero ` +
                "captures were queued during this run -- verify the item's actual state " +
                "before treating this as complete.";
            }
          } catch {
            // Inconclusive
          }
        }),
    );

    const stalled = stalledRelayWorkers(state.workers, Date.now(), rt.stallMs);
    const stalledNote = stalled.length
      ? `\n\n${stalled.length} worker(s) have been awaiting a relay for over ${formatDuration(rt.stallMs)} and are not progressing -- each needs a human answer in its own pane: ${stalled
          .map((w) => `${w.agent} (${w.slug}, pane ${w.paneId})`)
          .join(", ")}.`
      : "";
    const resyncNote = resyncNotes.length ? `\n\n${resyncNotes.join(" ")}` : "";
    // A held worker reports check-ins, not outcomes, so without this the
    // orchestrator sees a run where nothing settles and no explanation. It also
    // has to be said here rather than inferred: a hold occupies its slot on
    // purpose, and a reader who cannot see why will conclude the run is wedged.
    const held = state ? pendingAmendWorkers(state) : [];
    const amendNote = held.length
      ? `\n\n${held.length} worker(s) are being held open for an unacknowledged amendment and will not settle until it is accounted for: ${held
          .map(
            (w) =>
              `${w.agent} (${w.slug}, phase ${w.pendingAmend?.phase}, watch ${w.pendingAmend?.checks ?? 0}/${AMEND_ACK_MAX_CHECKS})`,
          )
          .join(
            ", ",
          )}. Their slots stay occupied -- do NOT spawn replacements or treat the run as drained.`
      : "";

    return {
      content: [
        {
          type: "text",
          text:
            (events.length > 0
              ? events
                  .map((e) => {
                    if (e.kind === "blocked") {
                      const verdict = `needs_human -- NOT a question-tool picker, so swarm_resolve_blocked cannot drive it. Relay the prompt below to the user verbatim and tell them to answer in pane ${e.paneId} themselves`;
                      return `${e.slug} (${e.agent}, pane ${e.paneId}) is blocked [${verdict}]${e.truncated ? " -- content may be truncated, inspect the pane directly" : ""}:\n${e.rawPrompt}`;
                    }
                    if (e.kind === "still_working") {
                      return `${e.slug} (${e.agent}) still_working -- check-in ${e.checkIn}, ${formatDuration(e.elapsedMs ?? 0)} of working time so far against a ${formatDuration(rt.deadlineMs)} budget. Nothing settled and no slot was freed; poll again.`;
                    }
                    const captures = renderCaptureOffers(e.captures ?? []);
                    return `${e.slug} (${e.agent}) ${e.kind}${e.detail ? `: ${e.detail}` : ""}${captures}`;
                  })
                  .join("\n\n")
              : "No active workers to poll.") +
            stalledNote +
            resyncNote +
            amendNote,
        },
      ],
      details: { events },
    };
  }

  public async swarmAmend(
    params: { runId: string; agent: string },
    signal?: AbortSignal,
  ): Promise<{ content: { type: string; text: string }[]; details: Record<string, unknown> }> {
    const state = await this.getOrInitState(params.runId, DEFAULT_CONCURRENCY);
    const worker =
      state.workers.find((w) => w.agent === params.agent) ??
      state.workers.find((w) => w.slug === params.agent);

    if (!worker) {
      return {
        content: [
          {
            type: "text",
            text: `amend_failed: no worker in run ${params.runId} matches "${params.agent}" by agent id or slug. Active workers: ${
              state.workers.map((w) => `${w.agent} (${w.slug})`).join(", ") || "none"
            }.`,
          },
        ],
        details: { amended: false, slug: "", paneId: "" },
      };
    }

    if (worker.lifecycle !== "active") {
      return {
        content: [
          {
            type: "text",
            text:
              `amend_refused: ${worker.agent} (${worker.slug}, pane ${worker.paneId}) is parked at a gate, ` +
              "and herdr agent prompt refuses a blocked agent -- nothing was sent. Answer it with " +
              "swarm_resolve_blocked first, then amend, or amend after it finishes and pick the item up again.",
          },
        ],
        details: { amended: false, slug: worker.slug, paneId: worker.paneId },
      };
    }

    const result = await this.herdr(buildAgentPromptArgv(worker.agent, AMEND_INSTRUCTION), signal);
    if (result.code !== 0) {
      return {
        content: [
          {
            type: "text",
            text: `amend_failed: ${worker.agent} (${worker.slug}) -- herdr agent prompt exited ${result.code}: ${result.stderr || result.stdout}`,
          },
        ],
        details: { amended: false, slug: worker.slug, paneId: worker.paneId },
      };
    }

    worker.amendments = [...(worker.amendments ?? []), { at: Date.now(), by: "swarm_amend" }];
    // Stamped AFTER the submission succeeded and BEFORE the response is
    // returned, and persisted with it: a marker written before a refused prompt
    // would hold a worker that was never amended, and one written only after the
    // tool returned could be lost to a crash in between, silently restoring the
    // race this exists to close.
    const existing = worker.pendingAmend;
    const seq = await this.herdr(buildAgentGetArgv(worker.agent), signal);
    worker.pendingAmend = {
      requestedAtMs: Date.now(),
      seqAtRequest: parseAgentStateSeq(seq.stdout) ?? null,
      lastObservedSeq: parseAgentStateSeq(seq.stdout) ?? null,
      phase: "await_turn",
      // One hold, not two: a second correction supersedes the first in the same
      // backlog record, so the outstanding watch is re-armed rather than stacked.
      checks: existing ? existing.checks + 1 : 0,
      checkInReported: false,
      runWorkedAfterAmendMs: -1,
    };
    this.persist(state);

    const superseded = existing
      ? ` This supersedes an earlier amendment still being watched (watch ${existing.checks + 1}), whose correction the updated item has already replaced. `
      : "";
    return {
      content: [
        {
          type: "text",
          text:
            `amended: ${worker.agent} (${worker.slug}, pane ${worker.paneId}) was told to re-read its item, ` +
            "and the run now holds its teardown until that correction is accounted for. " +
            "At submission nothing is known yet -- the verdict arrives on this worker's next poll " +
            "events as one of " +
            "`amend_confirmed` (the amendment's turn ran), `amend_steered` (the worker kept working " +
            "past the steering window, so the correction was most likely taken inside the finished " +
            "turn -- not positively confirmed) or `amend_unpicked` (the run settled immediately and no " +
            "new turn started, so the correction was very likely never read)." +
            superseded +
            " Say in the end-of-run digest that this item was amended mid-flight, and with which verdict it ended.",
        },
      ],
      details: { amended: true, slug: worker.slug, paneId: worker.paneId, held: true },
    };
  }

  public async swarmResolveBlocked(
    params: { runId: string; agent: string; answer: string },
    signal?: AbortSignal,
  ): Promise<{ content: { type: string; text: string }[]; details: Record<string, unknown> }> {
    const state = await this.getOrInitState(params.runId, DEFAULT_CONCURRENCY);
    const worker = state.workers.find((w) => w.agent === params.agent);
    if (!worker) {
      return {
        content: [
          {
            type: "text",
            text: `relay_failed: no tracked worker "${params.agent}" in run ${params.runId}.`,
          },
        ],
        details: { relayFailed: true, needsManual: false, slug: "", paneId: "" },
      };
    }

    if (this.kind === "pi") {
      return this.resolvePiBlocked(state, worker, params.answer, signal);
    }

    // Copilot workers do not render pi's question-tool numbered arrow-key picker,
    // so swarm_resolve_blocked always returns needs_manual with captured prompt.
    let rawPrompt = "";
    try {
      const readResult = await this.herdr(
        buildAgentReadArgv(params.agent, BLOCKED_READ_LINES),
        signal,
      );
      rawPrompt = readResult.stdout;
    } catch {
      // Best effort
    }

    noteResolveFailure(
      worker,
      params.answer,
      "copilot workers require manual response",
      Date.now(),
    );
    this.persist(state);

    return {
      content: [
        {
          type: "text",
          text:
            `needs_manual: Copilot workers do not have a programmatic picker -- manual input required for ` +
            `${params.agent} (${worker.slug}, pane ${worker.paneId}). ` +
            `Attach directly (herdr agent attach ${params.agent}) or switch to pane ${worker.paneId} to respond.` +
            (rawPrompt ? `\nCaptured prompt:\n${rawPrompt.slice(-2000)}` : ""),
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

  private async resolvePiBlocked(
    state: SwarmState,
    worker: WorkerRecord,
    answer: string,
    signal?: AbortSignal,
  ): Promise<{ content: { type: string; text: string }[]; details: Record<string, unknown> }> {
    const read = await this.herdr(buildAgentReadArgv(worker.agent, BLOCKED_READ_LINES), signal);
    const picker = this.picker.parsePicker(read.stdout);
    const target = matchOption(answer, picker.options);
    const manual = (
      reason: string,
    ): { content: { type: string; text: string }[]; details: Record<string, unknown> } => {
      noteResolveFailure(worker, answer, reason, Date.now());
      this.persist(state);
      return {
        content: [
          {
            type: "text",
            text: `needs_manual: ${reason} for ${worker.agent} (${worker.slug}, pane ${worker.paneId}).`,
          },
        ],
        details: {
          relayFailed: false,
          needsManual: true,
          slug: worker.slug,
          paneId: worker.paneId,
        },
      };
    };
    if (!target || picker.selectedIndex === null) return manual("no listed option matched");
    const identity = await this.herdr(buildAgentGetArgv(worker.agent), signal);
    if (paneIdentityMismatch(identity.code, identity.stdout, worker.paneId))
      return manual("pane identity mismatch");
    const keys = await this.herdr(
      buildAgentSendKeysArgv(worker.agent, navigationKeys(picker.selectedIndex, target.index)),
      signal,
    );
    if (keys.code !== 0)
      return this.relayFailure(
        state,
        worker,
        `could not send navigation keys to ${worker.agent}: ${keys.stderr || keys.stdout}`,
        signal,
      );
    const verify = await this.herdr(
      buildAgentWaitArgv(worker.agent, ["idle", "done", "working"], RESOLVE_VERIFY_TIMEOUT_MS),
      signal,
    );
    if (verify.code !== 0)
      return this.relayFailure(
        state,
        worker,
        `${worker.agent} did not resume within ${RESOLVE_VERIFY_TIMEOUT_MS} ms after "${target.label}" was submitted.`,
        signal,
      );
    worker.workingSinceMs = Date.now();
    worker.awaitingRelaySinceMs = undefined;
    worker.lastResolveFailure = undefined;
    worker.lifecycle = "active";
    this.persist(state);
    return {
      content: [
        {
          type: "text",
          text: `resolved: ${worker.agent} (${worker.slug}, pane ${worker.paneId}) answered "${target.label}", back in the active pool.`,
        },
      ],
      details: { relayFailed: false, needsManual: false, slug: worker.slug, paneId: worker.paneId },
    };
  }

  private async relayFailure(
    state: SwarmState,
    worker: WorkerRecord,
    reason: string,
    signal?: AbortSignal,
  ): Promise<{ content: { type: string; text: string }[]; details: Record<string, unknown> }> {
    const captures = await this.teardownAndHarvestWorker(state, worker, signal);
    const teardown =
      worker.lifecycle === "teardown_ambiguous"
        ? ` Teardown is ambiguous: ${worker.teardownDetail}; the serial queue remains paused.`
        : "";
    const heldNote = worker.pendingAmend
      ? ` ${amendOutstandingNote(worker.pendingAmend, Date.now())}`
      : "";
    return {
      content: [
        {
          type: "text",
          text: `relay_failed: ${reason}${teardown}${heldNote}${renderCaptureOffers(captures)}`,
        },
      ],
      details: {
        relayFailed: true,
        needsManual: false,
        slug: worker.slug,
        paneId: worker.paneId,
        captures,
      },
    };
  }
}

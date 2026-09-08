import { existsSync, mkdirSync, readFileSync, rmSync, writeFileSync } from "node:fs";
import { homedir } from "node:os";
import { join } from "node:path";
import type { ExtensionAPI } from "@earendil-works/pi-coding-agent";
import { Type } from "typebox";

// Registers swarm_spawn / swarm_poll / swarm_resolve_blocked -- lets a pi
// session process several READY dev_status.py backlog items concurrently by
// spawning one recursive pi worker per item in its own herdr tab, pooling
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
//
// A sixth surfaced on the first end-to-end shakedown, and it is why the wait
// path now looks the way it does. swarm_poll treated an elapsed wait as a
// dead worker: `finished`, `timed_out` and `error` all fell into the same
// `else` branch, which closed the tab and dropped the worker. But an elapsed
// wait means NOTHING SETTLED IN THE WINDOW, which is exactly what a healthy
// worker doing several minutes of real work looks like -- herdr documents
// `agent wait` as a wait deadline, not a liveness check. A worker several
// minutes into a real item (item claimed, worktree created, spec written, a
// four-criterion gate set) was destroyed the moment the deadline elapsed,
// costing its in-flight context and everything it had written inside its
// worktree, and leaving an orphaned worktree, a stale claim blocking a later
// start, and a digest line reporting it as having misbehaved. The 30-minute
// constant's old comment -- "matching --auto's generous per-step
// conventions" -- was the bug in miniature: the wait is armed once per
// WORKER and covers the whole item, not one step.
//
// The fix separates two durations that were conflated. `timeoutMs` is a
// CHECK-IN INTERVAL: when it elapses, `agent get` is asked whether the
// worker is alive, and a live one is simply waited on again and reported as
// `still_working`. `workerDeadlineMs` is the whole-item budget, measured in
// WORKING time so hours parked awaiting a human relay do not count, and it
// is the only thing that stops a live worker. Two rules keep the probe from
// re-deriving the original bug one layer down: it fails OPEN, so only
// herdr's own `agent_not_found` closes a worker and every inconclusive
// answer re-arms; and the budget bounds those inconclusive answers too, so a
// worker wedged badly enough that `agent get` itself cannot answer is still
// stopped rather than re-arming forever. A stop reports whether liveness was
// actually confirmed, because saying "still working" about a worker whose
// probe failed would be the same class of lie as the mislabeled timeout that
// started all this.

// Extracted helper modules (interface-preserving refactor): the picker
// relay, scheduling decisions and herdr protocol argv/response helpers live
// in their own files under swarm-lib/ -- deliberately un-discoverable by pi
// (no index.ts, no package.json manifest), since a bare *.ts file directly
// in extensions/ is loaded as its own extension and none of these three
// export a factory function. Every moved symbol is re-exported below so
// this extension's import surface is unchanged.
import {
  AMEND_INSTRUCTION,
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
  classifyResyncGet,
  classifyTimeoutProbe,
  classifyWaitResult,
  deadlineStopDetail,
  findTabByLabel,
  paneIdentityMismatch,
  parseAgentListIds,
  parseTabCreate,
  reasonHeadline,
  waitResultDetail,
} from "./swarm-lib/swarm-herdr";
import type { BlockClass } from "./swarm-lib/swarm-picker";
import type { PollEventKind } from "./swarm-lib/swarm-herdr";
import type { ProbeResult, TabCreateResult } from "./swarm-lib/swarm-herdr";
import {
  classifyBlock,
  matchOption,
  navigationKeys,
  noteResolveFailure,
  parsePicker,
  pickerLabels,
} from "./swarm-lib/swarm-picker";
import {
  isSuspiciousFinish,
  itemPaths,
  nextAgentId,
  parseReadyItems,
  parseShownItem,
  selectSchedulable,
  spawnBudget,
  stalledRelayWorkers,
} from "./swarm-lib/swarm-scheduling";
import type { ReadyItem, SwarmState, WorkerRecord } from "./swarm-lib/swarm-scheduling";

export * from "./swarm-lib/swarm-picker";
export * from "./swarm-lib/swarm-scheduling";
export * from "./swarm-lib/swarm-herdr";

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
/**
 * How long one `herdr agent wait` runs before the poller checks in on the
 * worker. A CHECK-IN INTERVAL, not a kill deadline -- see the header comment
 * above for the live run this distinction cost. A worker still working when
 * it elapses is probed and waited on again.
 */
const DEFAULT_WAIT_TIMEOUT_MS = 30 * 60 * 1000;
/**
 * The whole-item budget for one worker, measured in WORKING time (see
 * `elapsedWorkingMs`). An item runs baseline, spec, optional critique, TDD,
 * full verify and a commit gate, so 4 hours is far above any observed run and
 * far below "never". "Never" is not an option: without a budget, a worker
 * wedged badly enough that even `agent get` cannot answer would re-arm
 * forever, holding its slot until someone killed the orchestrator by hand.
 */
const DEFAULT_WORKER_DEADLINE_MS = 4 * 60 * 60 * 1000;
// How long a worker may sit at awaiting_relay before the poll says so out
// loud. Not a stop: a relay answered at minute 31 is still worth having, and
// killing the worker would destroy finished work over a slow human. It is
// purely a signal, so the run cannot go quiet with a pane nobody was told
// about.
const DEFAULT_RELAY_STALL_MS = 30 * 60 * 1000;
/**
 * How long the liveness probe may take before it is abandoned as
 * inconclusive. Short, because `agent get` is a local socket round-trip --
 * and because a probe that hangs would strand the very worker it was added to
 * protect: `try`/`catch` catches throws, not hangs.
 */
const PROBE_TIMEOUT_MS = 15_000;
const RESOLVE_VERIFY_TIMEOUT_MS = 5_000;
const BLOCKED_READ_LINES = 500;
const BLOCKED_READ_LINES_RETRY = 2000;
/** Enough for a pi crash trace, small enough that one failure cannot flood the orchestrator's digest. */
export const PANE_CAPTURE_CHARS = 4000;
const PANE_CAPTURE_LINES = 200;

// ---------------------------------------------------------------------------
// Types
// ---------------------------------------------------------------------------
/**
 * Total working time so far: completed segments plus the open one.
 *
 * Null only when the worker has neither -- a record not yet stamped or
 * folded. Never NaN (the optional fields default explicitly rather than
 * landing in `undefined + number`) and never negative.
 *
 * `Date.now()` is not monotonic, so the open segment is clamped at zero: an
 * NTP step backwards would otherwise subtract hours from a worker's
 * accounting or push its deadline into the future. The clamp UNDER-counts a
 * segment spanning a backwards step, which is the deliberate direction --
 * under-counting hands the worker extra budget, while over-counting would
 * stop it early, which is this file's whole bug. Measuring it exactly would
 * need a monotonic clock kept beside this one and reconciled across restarts:
 * real machinery to fix a case whose failure mode is already benign.
 */
export function elapsedWorkingMs(worker: WorkerRecord, now: number): number | null {
  const open =
    worker.workingSinceMs === undefined ? null : Math.max(0, now - worker.workingSinceMs);
  if (open === null && worker.accumulatedWorkingMs === undefined) return null;
  return (worker.accumulatedWorkingMs ?? 0) + (open ?? 0);
}
/**
 * Folds the open segment into `accumulatedWorkingMs` and clears
 * `workingSinceMs`.
 *
 * A worker with no open segment folds nothing rather than adding NaN -- a
 * legacy record can reach the park path before its first check-in ever stamps
 * it, because a `blocked` settle needs no probe. Idempotent, so a double-park
 * costs nothing.
 */
export function foldWorkingSegment(worker: WorkerRecord, now: number): void {
  if (worker.workingSinceMs === undefined) return;
  worker.accumulatedWorkingMs =
    (worker.accumulatedWorkingMs ?? 0) + Math.max(0, now - worker.workingSinceMs);
  worker.workingSinceMs = undefined;
}
/** One item's outcome from the spawn loop: a live worker, or a reason it never became one. */
type SpawnOutcome =
  { worker: WorkerRecord } | { slug: string; failed?: { slug: string; reason: string } };
export interface PollEvent {
  kind: PollEventKind;
  agent: string;
  slug: string;
  paneId: string;
  rawPrompt?: string; // blocked only
  truncated?: boolean; // blocked only
  blockClass?: BlockClass; // blocked only
  options?: string[]; // blocked only -- the worker's REAL rendered labels
  captures?: CaptureOffer[]; // finished only -- offers the worker queued instead of asking
  detail?: string; // timed_out/error always; finished sometimes -- an inferred/unverified outcome (resync-drop, or a suspicious no-evidence finish) gets an honest verify-me note here too
  elapsedMs?: number; // still_working only -- working time so far, against the budget
  checkIn?: number; // still_working only -- 1-based, so "check-in 7 of a 4h budget" is sayable
}

// ---------------------------------------------------------------------------
// State file persistence and crash recovery
// ---------------------------------------------------------------------------
export function statePath(runId: string, stateDir: string = herdrStateDir()): string {
  return join(stateDir, `swarm-${runId}.json`);
}
/**
 * A queued proactive-capture offer a worker wants recorded.
 *
 * `kind` mirrors CLAUDE.md's own protocols so the orchestrator can present
 * each offer the way its protocol specifies: a backlog `add`, a
 * `pending add`, or an `out-of-scope add`.
 */
export interface CaptureOffer {
  kind: string;
  id: string;
  summary: string;
}
/**
 * Where a worker writes the capture offers it wants the orchestrator to ask
 * about, keyed by slug rather than agent id.
 *
 * By slug because the tab -- and therefore the env var naming this path --
 * is created before an agent id exists, and a slug is unique within a run
 * anyway (an item spawns once).
 *
 * The slug is reduced to its basename and stripped of anything outside
 * `[A-Za-z0-9._-]`, so a crafted slug cannot walk out of the state dir.
 */
export function capturePath(
  runId: string,
  slug: string,
  stateDir: string = herdrStateDir(),
): string {
  const safeRun = runId.replace(/[^A-Za-z0-9._-]/g, "_");
  const safeSlug = (slug.split("/").pop() ?? "").replace(/[^A-Za-z0-9._-]/g, "_");
  return join(stateDir, `swarm-${safeRun}-capture-${safeSlug}.json`);
}
/**
 * Read a worker's queued capture offers, CONSUMING the file.
 *
 * Consumed because this is called as the worker finishes, immediately before
 * its tab is closed and its record dropped. A file left behind would be
 * re-read by a later poll with no worker left to attribute it to, and the
 * human would be asked the same questions twice.
 *
 * Anything unreadable -- absent, malformed, wrong shape -- yields no offers
 * rather than throwing. This runs inside swarm_poll's drain loop, where an
 * exception would abandon every event queued behind it; losing a housekeeping
 * offer is a far smaller harm than losing a worker's outcome.
 */
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
    // Best effort. A file we could read but not delete is better reported
    // once than not at all.
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
/**
 * Renders harvested capture offers for a tool result's text, in the same
 * shape swarm_poll attaches them to a finished event. Shared by swarm_poll
 * and swarm_resolve_blocked so the two surfaces cannot drift.
 *
 * Zero offers render as an empty string: a worker that queued nothing must
 * not change the output it always produced.
 */
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

/**
 * dev_status.py, the authority on which items are READY.
 *
 * Resolved per call rather than captured at module load, and overridable --
 * the same seam herdrStateDir() uses, and for the same two reasons: a test
 * can point it at a fixture, and a live check of an unreleased change can
 * point it at a worktree copy instead of the installed symlink.
 */
export function devStatusPath(): string {
  return (
    process.env.PI_SWARM_DEV_STATUS_PATH ?? join(homedir(), ".claude", "scripts", "dev_status.py")
  );
}
/** `dev_status.py ready` reports the bucket the dashboard already builds. */
export function buildReadyArgv(prefix?: string): string[] {
  const argv = ["python3", devStatusPath(), "ready"];
  return prefix ? [...argv, "--prefix", prefix] : argv;
}
/** `dev_status.py show <slug>` reports one item's full record; only `status` matters here. */
export function buildShowArgv(slug: string): string[] {
  return ["python3", devStatusPath(), "show", slug];
}
/** Human-readable ms, for a check-in line a person reads ("3h31m", "45m"). */
export function formatDuration(ms: number): string {
  const totalMinutes = Math.max(0, Math.round(ms / 60000));
  const hours = Math.floor(totalMinutes / 60);
  const minutes = totalMinutes % 60;
  return hours > 0 ? `${hours}h${String(minutes).padStart(2, "0")}m` : `${minutes}m`;
}
/** Content that fills the requested line budget exactly is a truncation signal, not necessarily proof -- see plan section 4. */
export function looksTruncated(content: string, requestedLines: number): boolean {
  return content.split("\n").length >= requestedLines;
}

export default function (pi: ExtensionAPI) {
  const activeRuns = new Map<string, SwarmState>();

  async function herdr(pi_: ExtensionAPI, argv: string[], signal?: AbortSignal) {
    return pi_.exec("herdr", argv, { signal });
  }

  /**
   * Closes a worker's pane on a path that is dropping it from state anyway.
   *
   * Dropping the state entry without this leaves a live pi in a tab nothing
   * will ever poll, answer, or clean up -- burning a model and holding the
   * worktree it was working in. A close that fails must not throw: the relay failure
   * being reported is the root cause, and a cleanup problem never replaces it
   * -- the same rule failWithTab follows on the spawn side.
   */
  async function closeWorker(worker: WorkerRecord, signal?: AbortSignal): Promise<void> {
    try {
      await herdr(pi, buildWorkerCloseArgv(worker), signal);
    } catch {
      // Already gone, or herdr unresponsive -- nothing left to clean up.
    }
  }

  /**
   * Drops a worker for good: harvests its queued capture offers FIRST (the
   * consuming read -- the record is gone right after, so nothing could ever
   * attribute a file left behind to the item), closes its pane/tab, then
   * removes the record and persists. Shared by swarm_poll's finish-path
   * teardown and swarm_resolve_blocked's relay_failed paths, so read-then-
   * close has exactly one implementation and its two former copies cannot
   * drift.
   *
   * Read BEFORE close because that is the drain loop's proven order and the
   * documented contract; on the verify-timeout path a worker that actually
   * resumed could append after the read, which is accepted -- losing a
   * housekeeping offer is the smaller harm, and a file re-created in that
   * window is re-deleted after the close rather than orphaned on disk with no
   * record left to ever clean it up.
   *
   * Mutates `state` in place and returns the offers; callers must not retain
   * the filtered worker afterwards.
   */
  async function teardownAndHarvestWorker(
    state: SwarmState,
    worker: WorkerRecord,
    signal?: AbortSignal,
  ): Promise<CaptureOffer[]> {
    const offers = readCaptureOffers(state.runId, worker.slug);
    await closeWorker(worker, signal);
    try {
      rmSync(capturePath(state.runId, worker.slug), { force: true });
    } catch {
      // Best effort -- see readCaptureOffers' own post-read delete.
    }
    state.workers = state.workers.filter((w) => w.agent !== worker.agent);
    persist(state);
    return offers;
  }

  /**
   * Finds and closes the tab a failed `tab create` left behind, returning its
   * id when it could be identified and closed.
   *
   * Only reached on the parse-failure path, so it costs nothing in the normal
   * case. Like every other cleanup here it must not throw: it is running
   * inside the reporting of another failure, and a recovery problem never
   * replaces the root cause.
   */
  async function recoverTabByLabel(label: string): Promise<string | undefined> {
    try {
      const listing = await herdr(pi, buildTabListArgv());
      if (listing.code !== 0) return undefined;
      const tabId = findTabByLabel(listing.stdout, label);
      if (!tabId) return undefined;
      const closed = await herdr(pi, buildTabCloseArgv(tabId));
      return closed.code === 0 ? tabId : undefined;
    } catch {
      return undefined;
    }
  }

  /**
   * Records a pane's terminal text into a failure reason, then closes the
   * pane.
   *
   * Both halves are fixes for observed damage. The capture: a pane's text is
   * the only record of an early worker crash, and the sibling pane-width bug
   * was diagnosed entirely from a leaked pane. The close: two failed runs on
   * 2026-09-02 left six orphan panes open. Tabs no longer subdivide the
   * layout the way splits did, so a leak is less destructive than it was --
   * but a live pi in a tab nobody polls is still a leak.
   *
   * A capture that itself fails (hard-crashed pane, unresponsive herdr) is
   * noted and the original reason is preserved unchanged -- a capture problem
   * must never replace the root cause.
   */
  async function failWithTab(
    slug: string,
    paneId: string,
    tabId: string,
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
      // The whole tab, not just the pane: a worker owns its tab outright, and
      // a tab left holding a dead pane is the leak this replaces.
      await herdr(pi, buildTabCloseArgv(tabId));
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
   * catch -- every failure in here has a pane to capture and a tab to close,
   * including one that arrives as a thrown exec rejection rather than a
   * non-zero exit.
   */
  async function spawnInto(
    paneId: string,
    tabId: string,
    agentId: string,
    slug: string,
    paths: string[],
    model?: string,
  ): Promise<SpawnOutcome> {
    const startResult = await herdr(pi, buildAgentStartArgv(agentId, paneId, model));
    if (startResult.code !== 0) {
      return failWithTab(
        slug,
        paneId,
        tabId,
        `agent_not_ready: ${startResult.stderr || startResult.stdout}`,
      );
    }
    // No trust step: the tab was created with WORKER_UNATTENDED_ENV, so both
    // gates already resolved themselves at pi's module load, before this
    // agent could accept a prompt at all.
    const promptResult = await herdr(
      pi,
      buildAgentPromptArgv(agentId, `/backlog-item --auto ${slug}`),
    );
    if (promptResult.code !== 0) {
      return failWithTab(
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
      },
    };
  }

  /** Loads state, reconciling against herdr's live truth only on a cold load (state wasn't already in this process's memory) -- avoids a herdr round-trip on every call once a run is warm. */
  async function getOrInitState(
    runId: string,
    concurrency: number,
    prefix?: string,
  ): Promise<SwarmState> {
    const cached = activeRuns.get(runId);
    if (cached) return cached;

    const loaded = loadState(runId);
    if (!loaded) {
      // The prefix rides along when the caller scoped the run, so a later
      // herdr_delegate.py restart can discover this runId by exact field
      // match instead of reverse-engineering it from slug naming.
      const fresh: SwarmState = {
        runId,
        concurrency,
        nextCounter: 0,
        workers: [],
        ...(prefix !== undefined ? { prefix } : {}),
      };
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
    /**
     * The most recent swarm_poll's resolved durations, read by armWait.
     *
     * On the runtime rather than passed as arguments because armWait re-arms
     * from inside its own settle handler: as arguments they would freeze at
     * whatever the FIRST poll passed, and swarm_poll's arm loop could never
     * correct them because it no-ops on a worker already in `inFlight`. A
     * later poll asking for a tighter check-in would be silently ignored for
     * the rest of that worker's life. A wait already running still keeps the
     * timeout it started with; the next re-arm picks these up.
     */
    timeoutMs: number;
    deadlineMs: number;
    stallMs: number;
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
        timeoutMs: DEFAULT_WAIT_TIMEOUT_MS,
        deadlineMs: DEFAULT_WORKER_DEADLINE_MS,
        stallMs: DEFAULT_RELAY_STALL_MS,
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

  /**
   * Parks until the next event lands on this run's queue, or the tool call is
   * aborted. Resolves true if woken by an event, false if aborted.
   *
   * Two things this has to get right, both of them leaks.
   *
   * The abort signal was previously handed to every herdr call that FOLLOWS
   * the wait but not to the wait itself, so an aborted swarm_poll never
   * returned -- the only thing that could settle its promise was a worker's
   * `agent wait`, up to 30 minutes away. Racing the signal fixes the hang.
   *
   * And whichever way it settles, the resolver has to come back out of
   * `rt.waiters`. A resolver left behind is woken by some later event, runs
   * against a queue another poll has already drained, and is then woken
   * again by every event after that -- the list only ever grew.
   */
  function waitForEvent(rt: RunRuntime, signal?: AbortSignal): Promise<boolean> {
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

  /**
   * Runs the liveness probe for a worker whose wait window elapsed.
   *
   * Bounded by PROBE_TIMEOUT_MS and reported as `abandoned` when we give up:
   * `try`/`catch` catches throws, not hangs, and a `herdr agent get` that
   * never returns would hold `inFlight`, push no event and run no wait --
   * the same strand this whole change removes, reached by a different road.
   *
   * The timer is cleared as soon as herdr settles. A local socket round-trip
   * normally answers in milliseconds, and leaving a 15 s timer armed per
   * check-in would hold the event loop open and later fire an abort at an
   * operation that finished long ago.
   *
   * Deliberately NOT given swarm_poll's abort signal: the probe belongs to
   * the worker's wait chain, not to the poll call, exactly as the wait itself
   * does.
   */
  async function probeLiveness(agentId: string): Promise<ProbeResult> {
    const controller = new AbortController();
    let abandoned = false;
    const timer = setTimeout(() => {
      abandoned = true;
      controller.abort();
    }, PROBE_TIMEOUT_MS);
    try {
      const result = await herdr(pi, buildAgentGetArgv(agentId), controller.signal);
      return { ...result, abandoned };
    } finally {
      clearTimeout(timer);
    }
  }

  /**
   * Arms a worker's wait call if one isn't already running for it.
   * Idempotent -- safe to call every time swarm_poll checks in on the pool.
   *
   * `rt.inFlight` is released HERE, in the settle handler's finally, and
   * nowhere else. That single release point is load-bearing, and worth saying
   * why plainly, because three separate attempts to improve on it each
   * introduced a defect.
   *
   * The temptation is to hold the entry past the settle so a concurrent
   * swarm_poll -- which arms every active worker it finds -- cannot start a
   * second wait against a worker whose terminal event has been pushed but not
   * yet drained. Work out what that second wait actually costs: for a
   * `finished` settle it returns the same status immediately, for `error`
   * likewise or `agent_not_found` once the tab is gone, for `timed_out` it
   * sits until the tab closes and then returns `agent_not_found`. In every
   * case: one redundant herdr call, and an event the drain loop already
   * discards via `if (!worker) continue`. Duplication is not the historical
   * failure -- the round-2 disaster recorded in the header was ABORTING the
   * losers of a wait race, and nothing here ever aborts a wait.
   *
   * Holding the entry, by contrast, makes the release depend on some later
   * code path running. A rejected `tab close`, an event whose worker is
   * already out of state, or an earlier event in the same batch throwing
   * would each leave the entry held forever on a worker still in
   * `state.workers`: active, holding a slot, with no wait running and
   * permanently unarmable, because armWait no-ops on a held entry. That is a
   * strictly worse strand than the one this file exists to remove, and it is
   * reachable three different ways.
   */
  function armWait(rt: RunRuntime, worker: WorkerRecord): void {
    if (rt.inFlight.has(worker.agent)) return;
    rt.inFlight.add(worker.agent);
    const timeoutMs = rt.timeoutMs;
    void settleWait(rt, worker, timeoutMs);
  }

  /** One armed wait, from `agent wait` through to a queued event or a re-arm. */
  async function settleWait(
    rt: RunRuntime,
    worker: WorkerRecord,
    timeoutMs: number,
  ): Promise<void> {
    let event: PollEvent | null = null;
    try {
      const result = await herdr(
        pi,
        buildAgentWaitArgv(worker.agent, ["idle", "done", "blocked"], timeoutMs),
      );
      let kind = classifyWaitResult(result.code, result.stdout, result.stderr);
      let detail =
        kind === "timed_out" || kind === "error"
          ? waitResultDetail(result.stdout, result.stderr)
          : undefined;

      if (kind === "timed_out") {
        // An elapsed wait means NOTHING SETTLED IN THE WINDOW -- which is what
        // a healthy worker doing several minutes of real work looks like. Ask
        // before concluding anything.
        const probe = await probeLiveness(worker.agent);
        const verdict = classifyTimeoutProbe(probe, elapsedWorkingMsFor(worker), rt.deadlineMs);
        if (verdict.disposition === "rearm") {
          worker.checkIns = (worker.checkIns ?? 0) + 1;
          // Re-arm BEFORE the event becomes visible, so a caller woken by the
          // check-in can never observe a worker with no wait running.
          rt.inFlight.delete(worker.agent);
          armWait(rt, worker);
          rt.pendingEvents.push({
            kind: "still_working",
            agent: worker.agent,
            slug: worker.slug,
            paneId: worker.paneId,
            elapsedMs: elapsedWorkingMs(worker, Date.now()) ?? 0,
            checkIn: worker.checkIns,
          });
          wakeWaiters(rt);
          return;
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

      event = { kind, agent: worker.agent, slug: worker.slug, paneId: worker.paneId };
      if (detail !== undefined) event.detail = detail;
    } catch (err) {
      // pi.exec rejects on a spawn failure. Without this the handler would
      // push nothing at all and the worker would simply go quiet.
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
    rt.pendingEvents.push(event);
    wakeWaiters(rt);
  }

  /**
   * Elapsed working time for a budget decision, stamping a record that has
   * never been stamped.
   *
   * A record written before budgets existed has no start time. Leaving it
   * null would mean no budget at all, reviving the unbounded hang for exactly
   * the state files in flight across this upgrade -- so it gets a late
   * budget, starting now, rather than none.
   */
  function elapsedWorkingMsFor(worker: WorkerRecord): number | null {
    const now = Date.now();
    if (worker.workingSinceMs === undefined && worker.accumulatedWorkingMs === undefined) {
      worker.workingSinceMs = now;
    }
    return elapsedWorkingMs(worker, now);
  }

  function wakeWaiters(rt: RunRuntime): void {
    const waiting = rt.waiters.splice(0);
    for (const wake of waiting) wake();
  }

  pi.registerTool({
    name: "swarm_spawn",
    label: "Swarm spawn",
    description:
      "Spawn recursive pi workers for a batch of READY backlog items, one herdr tab each, up to the concurrency cap.",
    promptSnippet: "Spawn concurrent pi workers for a batch of backlog items via herdr",
    promptGuidelines: [
      "Tabs are created sequentially (herdr tab create mutates shared workspace state), then agent start+prompt run concurrently across the resulting root panes. Every tab is full terminal size, so concurrency is not bounded by the terminal's width.",
      "A per-item spawn failure (agent_not_ready, agent_prompt_stalled, spawn_error, or an unparseable tab-create response) is reported in `failed`, not thrown -- other items in the batch are unaffected. Any failure after the tab exists carries that pane's captured output in its reason, and the tab is closed.",
      "Call swarm_poll next to begin the completion loop.",
      "Items are re-read from dev_status on every call, so an item unblocked by a worker that just finished is picked up by the next spawn without being named. Pass `prefix` (not `items`) to let it select, and call it again each time swarm_poll frees a slot -- swarm_poll itself never spawns.",
      "Every worker in a wave starts on the same `model` when one is given, and on pi's own default when it is not. The model is recorded on each worker, so the end-of-run digest can say what actually did the work -- without it a finished run cannot be reproduced or reasoned about.",
      "Two items whose related_files name the same file are never spawned into the same wave: each worker has its own worktree, so the second to merge would conflict. The loser is reported as deferred, still owed, and becomes schedulable once the worker it collided with finishes. Deferred is not the same as skipped (cap) -- a skipped item is coming next wave regardless.",
    ],
    parameters: Type.Object({
      runId: Type.String({
        description:
          "Identifier for this swarm run -- reused across spawn/poll/resolve calls, and to recover state after a restart.",
      }),
      items: Type.Optional(
        Type.Array(Type.String(), {
          description:
            "Backlog item slugs to spawn, in queue order. Omit to select automatically from the READY queue, which requires `prefix`.",
        }),
      ),
      prefix: Type.Optional(
        Type.String({
          description:
            'Slug prefix scoping automatic selection, e.g. "meta-". Required when `items` is omitted.',
        }),
      ),
      concurrency: Type.Optional(
        Type.Number({ description: "Max concurrent active workers. Default 3." }),
      ),
      model: Type.Optional(
        Type.String({
          description:
            'Model for every worker in this wave, as pi accepts it ("provider/id", e.g. "opencode-go/glm-5.3-flash"). Omit to let each worker take pi\'s own default. An unknown id is not caught here: it fails when pi starts inside the worker tab, and surfaces as an agent_not_ready spawn failure with that pane\'s output attached.',
        }),
      ),
    }),
    async execute(_toolCallId, params) {
      const typed = params as {
        runId: string;
        items?: string[];
        prefix?: string;
        concurrency?: number;
        model?: string;
      };
      if (!typed.items && !typed.prefix) {
        throw new Error(
          "swarm_spawn needs either `items` or `prefix`. Selecting from the whole READY queue " +
            "unscoped would pull unrelated projects into this run.",
        );
      }
      // Everything from reading the pool to pushing the new workers runs
      // under the run's lock -- measuring the budget and acting on it have to
      // be one step, or a second caller measures a pool this one is about to
      // fill. See withSpawnLock.
      return withSpawnLock(typed.runId, async () => {
        const state = await getOrInitState(
          typed.runId,
          typed.concurrency ?? DEFAULT_CONCURRENCY,
          typed.prefix,
        );

        // dev_status.py owns what READY means -- it is computed from the
        // blocker graph on every call, so an item becomes ready the moment its
        // last blocker is approved. Asking it each wave is what makes a run
        // follow a dependency chain instead of processing one fixed list.
        // The records also carry related_files, which is the only signal for
        // whether two items would edit the same file.
        const readyResult = await pi.exec("python3", buildReadyArgv(typed.prefix).slice(1), {});
        if (readyResult.code !== 0) {
          // An empty queue and an unreadable one look identical downstream:
          // both yield zero candidates and "Spawned 0 worker(s)", which the
          // orchestrator reads as a drained run. It would then finish, leaving
          // every remaining item unspawned and unreported. A lock timeout or a
          // broken dev_status.py must stop the run, not quietly end it.
          throw new Error(
            `could not read the READY queue from dev_status.py (exit ${readyResult.code}): ` +
              `${readyResult.stderr || readyResult.stdout || "no output"}`,
          );
        }
        const ready = parseReadyItems(readyResult.stdout);
        const readyById = new Map(ready.map((i) => [i.id, i]));

        const alreadyRunning = new Set(state.workers.map((w) => w.slug));
        const attempted = new Set(state.attempted ?? []);
        const candidates: ReadyItem[] = (typed.items ?? ready.map((i) => i.id))
          .filter((slug) => !alreadyRunning.has(slug))
          // Only automatic selection skips what this run already tried. An
          // explicit `items` list is the caller asking for those items by
          // name, and a deliberate retry is a legitimate thing to ask for.
          // This is not a way to double-spawn: an item whose worker is still
          // in the pool was already removed by the `alreadyRunning` filter
          // above, whichever way it was named.
          .filter((slug) => typed.items !== undefined || !attempted.has(slug))
          .map((slug) => readyById.get(slug) ?? { id: slug });

        const budget = spawnBudget(state, candidates.length);
        const takenPaths = state.workers.flatMap((w) => w.paths ?? []);
        const selection = selectSchedulable(candidates, takenPaths, budget);
        const toSpawn = selection.slugs;

        const tabs: {
          slug: string;
          created?: TabCreateResult;
          failed?: { slug: string; reason: string };
        }[] = [];

        // Sequential, still, and for the reason this tool's promptGuidelines
        // already give: tab creation mutates shared workspace state. What is
        // gone with the splits is the geometry -- no layout to measure, no
        // share to divide, no batch to trim, because every tab starts at the
        // full terminal size regardless of how many already exist.
        for (const slug of toSpawn) {
          const result = await herdr(
            pi,
            buildTabCreateArgv(process.cwd(), slug, capturePath(typed.runId, slug)),
          );
          if (result.code !== 0) {
            tabs.push({
              slug,
              failed: { slug, reason: `tab create failed: ${result.stderr || result.stdout}` },
            });
            continue;
          }
          const created = parseTabCreate(result.stdout);
          if (!created) {
            // The tab exists -- herdr exited 0 -- and the id that would close
            // it was in the response that just failed to parse. Every other
            // post-create failure goes through failWithTab and cleans up
            // after itself; without this one, a live pi sits in a tab nothing
            // will ever poll, answer or close. The label is the slug, so it
            // is the one handle left.
            const orphan = await recoverTabByLabel(slug);
            const head = result.stdout.slice(0, 200);
            tabs.push({
              slug,
              failed: {
                slug,
                reason: orphan
                  ? `could not parse tab create response; the tab it created was found by label and closed (${orphan}): ${head}`
                  : `could not parse tab create response, and no single tab labelled "${slug}" was found -- a tab may be open and unaccounted for, close it by hand: ${head}`,
              },
            });
            continue;
          }
          tabs.push({ slug, created });
        }

        const startResults = await Promise.allSettled(
          tabs.map(async (p): Promise<SpawnOutcome> => {
            if (p.failed || !p.created) return { slug: p.slug, failed: p.failed };
            const { paneId, tabId } = p.created;
            state.nextCounter += 1;
            const agentId = nextAgentId(typed.runId, state.nextCounter, p.slug);
            try {
              return await spawnInto(
                paneId,
                tabId,
                agentId,
                p.slug,
                itemPaths(readyById.get(p.slug) ?? { id: p.slug }),
                typed.model,
              );
            } catch (e) {
              // A throw out of spawnInto's herdr calls is a post-create
              // failure like any other. Without this it lands in allSettled's
              // rejected branch, which files the failure against slug "unknown"
              // and leaves the tab open -- losing both the item's identity and
              // the pane text that would say what happened.
              return failWithTab(p.slug, paneId, tabId, `spawn_error: ${String(e)}`);
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
        // Everything handed to a worker, however it went -- see SwarmState.attempted.
        state.attempted = [...new Set([...(state.attempted ?? []), ...toSpawn])];
        persist(state);

        const skipped = selection.skipped;
        const deferred = selection.deferred;
        const refused = selection.refused;

        const parts = [
          `Spawned ${spawned.length} worker(s)`,
          `${failed.length} failed to spawn`,
          `${skipped.length} skipped (cap)`,
          `${deferred.length} deferred (file overlap)`,
          `${refused.length} refused (not worker-safe)`,
        ];
        const lines = [`${parts.join(", ")}.`];
        for (const f of failed) lines.push(`- ${f.slug}: ${reasonHeadline(f.reason)}`);
        // Deferred items are named in the TEXT, not just details: the
        // orchestrator has to know they are still owed, and no provider
        // adapter reads `details`.
        for (const d of deferred) lines.push(`- ${d.slug}: deferred -- ${d.reason}`);
        // Refused items are named for the same reason deferred ones are -- no
        // provider adapter reads `details` -- but they mean the opposite thing,
        // so the wording must not invite another wave.
        for (const r of refused) lines.push(`- ${r.slug}: refused -- ${r.reason}`);
        if (spawned.length === 0 && deferred.length > 0) {
          lines.push(
            "Nothing spawned but items remain: poll the running workers, then call swarm_spawn again once one finishes.",
          );
        } else if (spawned.length === 0 && refused.length > 0) {
          // Terminal, not a retry. Refused items never become schedulable, so
          // an orchestrator that polled and re-spawned here would loop on a
          // queue that cannot drain.
          lines.push(
            "Nothing spawned and the remaining items are refused, not waiting: they are never schedulable by a worker. " +
              "This is the end of the swarm phase for this prefix -- report them as needing a normal session rather than polling or spawning again.",
          );
        }

        return {
          content: [{ type: "text", text: lines.join("\n") }],
          details: { spawned, failed, skipped, deferred, refused },
        };
      });
    },
  });

  pi.registerTool({
    name: "swarm_poll",
    label: "Swarm poll",
    description:
      "Wait for at least one active swarm worker to settle (blocked/finished/timed_out/error) or check in (still_working), returning every event currently queued.",
    promptSnippet: "Wait for swarm workers to settle and report events",
    promptGuidelines: [
      "Blocks until >=1 active worker settles. Returns an array -- process every event in it, relaying each blocked event to the user one at a time, before calling swarm_poll again.",
      "A blocked event is reported with the worker's prompt quoted verbatim from herdr -- never assume it's a diff or a yes/no. Never send a blocked worker another agent prompt except the actual answer via swarm_resolve_blocked -- any prompt is interpreted as the gate's answer.",
      "still_working is a CHECK-IN, not an outcome: a worker's wait window elapsed, it was confirmed alive and inside its budget, and a fresh wait is already armed. It closes nothing and frees no slot, so it is never a cue to call swarm_spawn, its item stays in the active working set, and it is never a row in the end-of-run summary. Report it and poll again.",
      "timed_out means the worker exceeded its whole-item WORKING-TIME budget and was stopped deliberately -- not that a wait deadline elapsed, which is now merely a check-in. Its tab is closed and its slot freed, but the item is probably still in-progress with a live claim and its worktree survives on disk, so relay the recovery detail in the event verbatim rather than reporting it as a worker that misbehaved. The detail also says whether the worker's liveness was actually confirmed before it was stopped, or whether the probe failed and the stop was on the budget alone -- do not report the second as though it were the first.",
      "error means the agent is positively gone (herdr reported agent_not_found), it crashed, or the wait itself failed. A transient failure of the liveness check is NOT an error: it re-arms, because killing a healthy worker on an inconclusive signal is the bug this tool was fixed for.",
      "finished/timed_out/error events already closed their pane and freed their slot; if the READY queue still has items and the cap has headroom, call swarm_spawn again for the next batch. still_working frees nothing.",
      "A finished event can also carry a detail -- either the item's dev_status.py status never advanced past open/in-progress with zero captures queued (no evidence of real progress during the run), or its agent vanished from herdr while parked and the outcome was inferred, not observed. Either way, do NOT treat that event as a clean, no-further-action success: surface it prominently in your end-of-run digest, the same way queued capture offers already are, and let the human decide whether the item needs a fresh worker. A finished event with no detail is a real, verified success.",
    ],
    parameters: Type.Object({
      runId: Type.String(),
      timeoutMs: Type.Optional(
        Type.Number({
          description: `How long one herdr wait runs before the poller checks in on a worker -- a CHECK-IN INTERVAL, not a kill deadline. A worker still working when it elapses is probed, waited on again, and reported as still_working; nothing is closed. A wait already running keeps the value it started with, and the next check-in picks up the current one. Default ${DEFAULT_WAIT_TIMEOUT_MS}.`,
        }),
      ),
      relayStallMs: Type.Optional(
        Type.Number({
          description: `How long a worker may sit at awaiting_relay before the poll names it as needing a human, in ms. A signal, never a stop -- the worker keeps its pane and its work. Its own clock, separate from workerDeadlineMs, which measures WORKING time and is stopped while a worker is parked. Default ${DEFAULT_RELAY_STALL_MS}.`,
        }),
      ),
      workerDeadlineMs: Type.Optional(
        Type.Number({
          description: `The whole-item budget for one worker, measured in WORKING time -- time parked awaiting a relay does not count. A worker still going past it is stopped deliberately and reported as timed_out, with its worktree path and the item's likely in-progress claim, so nothing is lost silently. Only ever observed at a check-in, so a worker can run up to one timeoutMs past it. Default ${DEFAULT_WORKER_DEADLINE_MS}.`,
        }),
      ),
    }),
    async execute(_toolCallId, params, signal) {
      const typed = params as {
        runId: string;
        timeoutMs?: number;
        workerDeadlineMs?: number;
        relayStallMs?: number;
      };
      const state = await getOrInitState(typed.runId, DEFAULT_CONCURRENCY);
      const rt = getRuntime(typed.runId);
      rt.timeoutMs = typed.timeoutMs ?? DEFAULT_WAIT_TIMEOUT_MS;
      rt.deadlineMs = typed.workerDeadlineMs ?? DEFAULT_WORKER_DEADLINE_MS;
      rt.stallMs = typed.relayStallMs ?? DEFAULT_RELAY_STALL_MS;

      // Blocked-state resync. A parked (awaiting_relay) record has no wait
      // armed, so if its gate was answered out-of-band -- direct pane keys,
      // not swarm_resolve_blocked -- or its agent exited, NOTHING will ever
      // produce an event for it: the run defers new items on file overlap
      // with its "held" worktree and burns a pane-cap slot on a worker that
      // is long gone. The 2026-09-07 full atk run lost several poll cycles
      // this way and only unstuck when a human told the orchestrator both
      // workers were already done. Re-derive each parked worker's state from
      // live herdr instead of trusting the record, so an out-of-band answer
      // or a dead worker is detected HERE, on every poll, with no nudge.
      //
      // Active records are deliberately out of scope: their waits already
      // self-heal (agent_not_found -> error event -> close), and a warm drop
      // on a transient herdr hiccup would be exactly the "killed on an
      // inconclusive signal" bug this file exists to prevent. Fail open
      // throughout: an inconclusive get changes nothing.
      const parkedNow = state.workers.filter((w) => w.lifecycle === "awaiting_relay");
      const resyncNotes: string[] = [];
      if (parkedNow.length > 0) {
        // Parked workers are bounded by the pane soft cap, but serial local
        // round-trips would still buy nothing -- fire the gets together.
        const gets = await Promise.all(
          parkedNow.map(async (worker) => {
            try {
              const r = await herdr(pi, buildAgentGetArgv(worker.agent), signal);
              return { worker, verdict: classifyResyncGet(r.code, r.stdout, r.stderr) };
            } catch {
              // A thrown get is as inconclusive as an unparseable one.
              return { worker, verdict: { action: "keep" as const } };
            }
          }),
        );
        const resumedAt = Date.now();
        for (const { worker, verdict } of gets) {
          if (verdict.action === "drop") {
            // Emit the terminal event and let the EXISTING drain loop below
            // own the whole teardown -- capture offers, closeWorker, record
            // removal, persist. Resync itself mutates nothing: one owner per
            // lifecycle transition, no split-brain double close. The detail
            // says the outcome was INFERRED, not observed -- a vanished
            // worker finished its item, or it died; the orchestrator should
            // verify the item either way.
            rt.inFlight.delete(worker.agent);
            rt.pendingEvents.push({
              kind: "finished",
              agent: worker.agent,
              slug: worker.slug,
              paneId: worker.paneId,
              detail:
                "resync: agent gone from herdr while its record said awaiting_relay -- " +
                "its gate was likely answered out-of-band (direct pane keys) and the " +
                "worker has since finished or exited; outcome inferred, not observed. " +
                "Verify the item's state before treating it as complete.",
            });
          } else if (verdict.action === "unpark") {
            // The same clock moves swarm_resolve_blocked's resume makes: the
            // working clock restarts (accumulated segments were folded at
            // park, so this is a segment restart, not a budget reset) and the
            // relay clock clears. Arming happens in the active-arm loop just
            // below; a wait on an already-idle/done agent settles
            // immediately, and this poll blocks on waitForEvent, so the
            // finished event lands in THIS poll call.
            worker.workingSinceMs = resumedAt;
            worker.awaitingRelaySinceMs = undefined;
            worker.lastResolveFailure = undefined;
            worker.lifecycle = "active";
            resyncNotes.push(
              `${worker.agent} (${worker.slug}) was parked awaiting a relay, but herdr now reports it unblocked -- resumed tracking as active.`,
            );
          }
        }
        if (resyncNotes.length > 0) persist(state);
      }

      // Stamp any parked worker written before this clock existed, rather
      // than leaving it unbounded forever. Stamping now means it is measured
      // from this poll instead of from whenever it really parked -- late, but
      // bounded, which is the whole point.
      const stampNow = Date.now();
      for (const w of state.workers) {
        if (w.lifecycle === "awaiting_relay" && w.awaitingRelaySinceMs === undefined) {
          w.awaitingRelaySinceMs = stampNow;
        }
      }

      const active = state.workers.filter((w) => w.lifecycle === "active");
      for (const w of active) armWait(rt, w);

      if (active.length === 0 && rt.pendingEvents.length === 0) {
        // A worker parked at awaiting_relay is not an empty pool: it is a live
        // pi holding an open pane, waiting on an answer only the orchestrator
        // can give. Reporting it as nothing left to do ended the run with that
        // worker and its pane still there. It deliberately gets no armWait --
        // it is blocked on the orchestrator, not on herdr, so a wait would
        // never fire and the poll below would hang on a promise nothing
        // resolves. Naming it instead is what lets the run continue.
        // Re-reconcile HERE, warm. reconcileState already drops records whose
        // agent is gone, but only on a cold load -- a warm orchestrator never
        // re-reads, so a worker that finished and vanished stayed listed as
        // awaiting a relay forever. That is not cosmetic: awaiting_relay is
        // excluded from activeWorkerCount but INCLUDED in openPaneCount, so
        // stranded records eat the pane soft cap while contributing nothing,
        // and dispatch stops for a reason no message explains. Three of four
        // workers in the 2026-09-03 harness2 run ended this way and a human
        // had to find it from a dashboard in another pane.
        //
        // This is exactly the right moment: nothing is active, so the
        // distinction between "genuinely waiting on a human" and "dead record"
        // is the only thing that matters, and a live agent is never dropped.
        const relistResult = await herdr(pi, buildAgentListArgv(), signal);
        let goneNote = "";
        if (relistResult.code === 0) {
          const liveNow = parseAgentListIds(relistResult.stdout);
          const { state: pruned, dropped } = reconcileState(state, liveNow);
          if (dropped.length) {
            state.workers = pruned.workers;
            // A dropped worker must not keep a wait slot: its agent is
            // gone, so nothing will ever settle that arm.
            for (const w of dropped) rt.inFlight.delete(w.agent);
            persist(state);
            goneNote = ` ${dropped.length} stale record(s) cleared -- their agents are gone from herdr, so they were finished or dead, not waiting: ${dropped
              .map((w) => `${w.agent} (${w.slug})`)
              .join(", ")}.`;
          }
        }

        const awaitingRelay = state.workers.filter((w) => w.lifecycle === "awaiting_relay");
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
            : "No active workers to poll.") + goneNote;
        return {
          content: [{ type: "text", text }],
          details: { events: [] as PollEvent[] },
        };
      }

      // A loop, not a single park. One event wakes EVERY queued waiter, and
      // the first to run takes the whole queue with splice(0) below -- so a
      // concurrent poll can wake to nothing. Returning an empty list there
      // told the orchestrator the run had gone quiet while it was in fact
      // still working, so a poll that loses that race goes back to waiting.
      let aborted = false;
      while (rt.pendingEvents.length === 0) {
        if (!(await waitForEvent(rt, signal))) {
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
          // The clock pauses here. Hours spent waiting on a human are not
          // hours the worker spent working, and charging them to the budget
          // would stop a worker at the moment its relay was finally answered.
          event.blockClass = classifyBlock(event.rawPrompt);
          event.options = pickerLabels(event.rawPrompt);
          const parkedAt = Date.now();
          foldWorkingSegment(worker, parkedAt);
          // The relay clock starts exactly where the working clock stops.
          worker.awaitingRelaySinceMs = parkedAt;
          worker.lifecycle = "awaiting_relay";
        } else if (event.kind === "still_working") {
          // Alive, inside its budget, and already re-armed by the settle
          // handler. Nothing to close, no slot freed -- it is a check-in.
        } else {
          // Read the worker's queued capture offers BEFORE dropping it. The
          // record is deleted on the next line and the tab closes with it, so
          // this is the last moment anything can be attributed to this item.
          // Carrying them here is what lets the orchestrator ask the human
          // once for the whole run instead of once per worker: a worker no
          // longer holds a concurrency slot open through a relay round trip
          // per housekeeping offer.
          event.captures = await teardownAndHarvestWorker(state, worker, signal);
        }
      }
      persist(state);

      // Minimum-evidence check for a "finished" event: was there ANY sign of
      // real progress -- the item's dev_status.py status advanced past
      // open/in-progress, or at least one capture was queued? If not, attach
      // a verify-me detail, same as the resync-drop path above already does
      // for its own "outcome inferred, not observed" case -- and never
      // overwrite a detail a path like that one already set. Batched via
      // Promise.all (bounded in practice by DEFAULT_CONCURRENCY, a small
      // integer) with each call wrapped in its own try/catch, mirroring the
      // resync pass's identical per-call error boundary above: an
      // inconclusive or failed check never mutates anything and never
      // affects another event.
      await Promise.all(
        events
          .filter((e) => e.kind === "finished")
          .map(async (event) => {
            try {
              const result = await pi.exec("python3", buildShowArgv(event.slug).slice(1), {
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
              // Inconclusive -- same fail-open rule every other check in this file follows.
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

      return {
        content: [
          {
            type: "text",
            text:
              events
                .map((e) => {
                  if (e.kind === "blocked") {
                    // The class goes in the text, not in `details`: no provider
                    // adapter in @earendil-works/pi-ai reads details, so a
                    // classification put there would be invisible to the very
                    // model that has to act on it.
                    const verdict =
                      e.blockClass === "answerable"
                        ? "answerable -- a question-tool picker, so answer it with swarm_resolve_blocked"
                        : `needs_human -- NOT a question-tool picker, so swarm_resolve_blocked cannot drive it. Relay the prompt below to the user verbatim and tell them to answer in pane ${e.paneId} themselves`;
                    // The labels are quoted verbatim and called out as such,
                    // because swarm_resolve_blocked matches on these exact
                    // strings. An orchestrator that relays a paraphrase of them
                    // produces an answer matching nothing, and the worker is
                    // never moved off awaiting_relay.
                    const labels = (e.options ?? []).length
                      ? `\nRelay these option labels to the user VERBATIM -- swarm_resolve_blocked matches on these exact strings, so a paraphrase strands the worker: ${(e.options ?? []).map((l) => JSON.stringify(l)).join(", ")}`
                      : "";
                    return `${e.slug} (${e.agent}, pane ${e.paneId}) is blocked [${verdict}]${e.truncated ? " -- content may be truncated, inspect the pane directly" : ""}:\n${e.rawPrompt}${labels}`;
                  }
                  if (e.kind === "still_working") {
                    return `${e.slug} (${e.agent}) still_working -- check-in ${e.checkIn}, ${formatDuration(e.elapsedMs ?? 0)} of working time so far against a ${formatDuration(rt.deadlineMs)} budget. Nothing settled and no slot was freed; poll again.`;
                  }
                  const captures = renderCaptureOffers(e.captures ?? []);
                  return `${e.slug} (${e.agent}) ${e.kind}${e.detail ? `: ${e.detail}` : ""}${captures}`;
                })
                .join("\n\n") +
              stalledNote +
              resyncNote,
          },
        ],
        details: { events },
      };
    },
  });

  pi.registerTool({
    name: "swarm_amend",
    label: "Swarm amend",
    description:
      "Tell a running worker its backlog item has been corrected, so it re-reads the item before continuing.",
    promptSnippet: "Tell a swarm worker to re-read its corrected item",
    promptGuidelines: [
      "Edit the item FIRST with dev_status.py, then call this. It sends a fixed instruction to re-read the item and carries no correction text of its own -- deliberately, so the backlog store stays the single source of truth. There is no parameter for the correction because a message and a store that disagree is worse than the problem being fixed.",
      "Only reaches a worker that is actively working. `herdr agent prompt` refuses an agent that is already blocked, so a worker parked at a gate is reported as amend_refused: -- answer it with swarm_resolve_blocked instead, or let it finish and pick the item up again afterwards.",
      "Every outcome leads with its own marker word -- amended:, amend_refused: or amend_failed: -- and names the agent and slug, so branch on that word.",
      "Delivery is not synchronised with the worker's turn boundary, and cannot be: pi has no such checkpoint exposed over herdr. A prompt that lands mid-turn arrives as the worker's next input, which is fine while it is still planning and is a rewrite of finished work if it is not. So amend early, and treat a worker deep into an item as a candidate for stopping rather than correcting.",
      "The amendment is recorded on the run state, so the end-of-run digest can say the item was corrected mid-flight and when.",
    ],
    parameters: Type.Object({
      runId: Type.String({ description: "The run whose worker is being amended." }),
      agent: Type.String({
        description: "The worker's agent id, or its item slug -- either resolves.",
      }),
    }),
    async execute(_callId: string, params: unknown, signal?: AbortSignal) {
      const typed = params as { runId: string; agent: string };
      const state = await getOrInitState(typed.runId, DEFAULT_CONCURRENCY);
      const worker =
        state.workers.find((w) => w.agent === typed.agent) ??
        state.workers.find((w) => w.slug === typed.agent);

      if (!worker) {
        return {
          content: [
            {
              type: "text",
              text: `amend_failed: no worker in run ${typed.runId} matches "${typed.agent}" by agent id or slug. Active workers: ${
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

      const result = await herdr(pi, buildAgentPromptArgv(worker.agent, AMEND_INSTRUCTION), signal);
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
      persist(state);

      return {
        content: [
          {
            type: "text",
            text:
              `amended: ${worker.agent} (${worker.slug}, pane ${worker.paneId}) was told to re-read its item. ` +
              "Nothing confirms it has done so -- the instruction lands as its next input, which is a correction " +
              "while it is still planning and a rewrite of finished work if it is not. Watch its next poll event, " +
              "and say in the end-of-run digest that this item was amended mid-flight.",
          },
        ],
        details: { amended: true, slug: worker.slug, paneId: worker.paneId },
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
      "When you relay a blocked worker's gate to the user, quote the worker's OWN listed option labels verbatim -- the poll result prints them for exactly this. Never compose your own wording for them, however much clearer it reads: swarm_resolve_blocked matches the answer against the labels the worker is really rendering, so a paraphrased option matches nothing, the resolve returns needs_manual, and the worker is left at awaiting_relay consuming a pane slot while the run reports it as simply unanswered.",
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
                (noteResolveFailure(worker, typed.answer, "no listed option matched", Date.now()),
                persist(state),
                `needs_manual: could not match "${typed.answer}" to exactly one listed option for `) +
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
                (noteResolveFailure(worker, typed.answer, "pane identity mismatch", Date.now()),
                persist(state),
                `needs_manual: ${typed.agent} (${worker.slug})'s pane identity no longer matches `) +
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
        // Relay failed, but the worker's queued capture offers must not die
        // with its record -- harvest before the teardown this helper does.
        const captures = await teardownAndHarvestWorker(state, worker, signal);
        // Typed with captures optional so every branch's details share one
        // shape and registerTool's inference does not split on the key.
        const details: {
          relayFailed: boolean;
          needsManual: boolean;
          slug: string;
          paneId: string;
          captures?: CaptureOffer[];
        } = {
          relayFailed: true,
          needsManual: false,
          slug: worker.slug,
          paneId: worker.paneId,
          captures,
        };
        return {
          content: [
            {
              type: "text",
              text:
                `relay_failed: could not send navigation keys to ${typed.agent}: ${keysResult.stderr || keysResult.stdout}` +
                renderCaptureOffers(captures),
            },
          ],
          details,
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
        // Same harvest-before-teardown as the send-keys branch: a worker that
        // answered and is mid-turn queued its offers to a file only this call
        // can ever attribute.
        const captures = await teardownAndHarvestWorker(state, worker, signal);
        const details: {
          relayFailed: boolean;
          needsManual: boolean;
          slug: string;
          paneId: string;
          captures?: CaptureOffer[];
        } = {
          relayFailed: true,
          needsManual: false,
          slug: worker.slug,
          paneId: worker.paneId,
          captures,
        };
        return {
          content: [
            {
              type: "text",
              text:
                `relay_failed: ${typed.agent} did not resume within ${RESOLVE_VERIFY_TIMEOUT_MS} ms after "${target.label}" was submitted.` +
                renderCaptureOffers(captures),
            },
          ],
          details,
        };
      }

      // The clock restarts here, alongside the lifecycle change it belongs
      // to. Together with the fold at park (swarm_poll's drain loop) and the
      // stamp at spawn, these are the only places the working-time clock
      // moves -- a future path back to active that forgets this would
      // silently charge a worker for the hours it spent waiting on a human.
      worker.workingSinceMs = Date.now();
      // The relay clock stops where the working clock restarts, so a worker
      // answered and later re-blocked is measured from its NEW park, not its
      // first one.
      worker.awaitingRelaySinceMs = undefined;
      worker.lastResolveFailure = undefined;
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

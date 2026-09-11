// Pure scheduling decisions over a swarm run's persisted state shape:
// worker naming, READY-item selection, concurrency/pane accounting and
// relay-stall detection. Extracted verbatim from swarm-tool.ts; no module
// state, no I/O.
const OPEN_PANE_SOFT_CAP_MULTIPLIER = 2;

/**
 * The herdr `agent_status` values that mean the worker's pi has finished and
 * sits at its own prompt: EXACTLY the set `classifyWaitResult` (swarm-herdr)
 * maps to "finished". herdr keeps such an agent LISTED -- the process is
 * alive, its tab open -- so agent-list presence alone is not liveness; a
 * listed-but-terminal worker has finished its item, and its record must not
 * hold the wave back. `working` is mid-item and `blocked` is awaiting a
 * relay: never terminal. Anything else (absent, unknown) is inconclusive and
 * keeps the record -- fail open, per this module's standing rule.
 */
export const TERMINAL_AGENT_STATUSES = ["idle", "done"] as const;

export function isTerminalAgentStatus(status?: string): boolean {
  return status !== undefined && (TERMINAL_AGENT_STATUSES as readonly string[]).includes(status);
}

/**
 * Minimum age a worker record must have (measured from its last working
 * segment's start, or its relay park) before a TERMINAL status counts as a
 * finish. A freshly spawned pi can also read `idle` in its startup window,
 * before the prompt begins processing -- without the floor, a reconcile in
 * that window would kill the worker before its first turn. A genuinely
 * finished trivial item merely waits out one floor period before being
 * cleared: the conservative direction.
 */
export const RECONCILE_MIN_AGE_MS = 60_000;

/**
 * Worker records that can no longer produce an event and must not hold
 * files: the agent is absent from herdr's live list (gone outright), or
 * herdr reports a terminal status and the record is older than
 * RECONCILE_MIN_AGE_MS. Everything else -- working, blocked, unknown
 * status, terminal but too young, or no timestamps at all -- is kept.
 *
 * Pure; the caller owns the teardown that follows. Companion to
 * `reconcileState` (which decides on PRESENCE alone and stays that way for
 * its cold-load contract): this is the status-aware decision the spawn path
 * and swarm_poll's zero-active resync use, because a finished worker
 * remains listed in herdr and presence alone would defer on its paths
 * forever.
 */
export function staleWorkerRecords(
  state: SwarmState,
  live: readonly { id: string; status?: string }[],
  now: number,
): WorkerRecord[] {
  const statusById = new Map(live.map((e) => [e.id, e.status]));
  return state.workers.filter((w) => {
    if (!statusById.has(w.agent)) return true; // absent: gone outright
    const status = statusById.get(w.agent);
    if (status === undefined) return false; // present, status unknown: fail open
    if (!isTerminalAgentStatus(status)) return false;
    const began = w.workingSinceMs ?? w.awaitingRelaySinceMs;
    if (began === undefined) return false; // no age evidence: fail open
    return now - began >= RECONCILE_MIN_AGE_MS;
  });
}

export type ExecutionMode = "concurrent" | "serial";

// ---------------------------------------------------------------------------
// The worker's death certificate, and the orchestrator's reconciled outcome.
//
// Two records, because neither side can author the whole one: the worker knows
// `stopReason` and nothing else about its own identity, and the orchestrator
// knows the run id, the agent id and the backlog status and nothing about
// `stopReason`. The worker writes the certificate (`fatal-error-exit.ts`,
// synchronously, immediately before it exits); the orchestrator assembles the
// `WorkerOutcome` from the certificate + herdr's process truth + its own
// independent `dev_status.py show`.
// ---------------------------------------------------------------------------

/** The only payload version this reader understands. Anything else is absent. */
export const SIDECAR_VERSION = 1;

/** The only result a certificate may carry. See `parseFatalSidecar`. */
export const FATAL_SIDECAR_RESULT = "fatal_error";

/** What `fatal-error-exit.ts` writes before it exits 1. */
export interface FatalSidecar {
  v: number;
  result: string;
  stopReason: string;
  model: string;
  writtenAtMs: number;
}

/**
 * Strict parse of a death certificate. Returns `null` for ABSENT.
 *
 * Deliberately unforgiving in one direction only: a certificate the reader
 * cannot fully vouch for is treated as if the file were not there, which sends
 * classification to the pane fallback rather than to a verdict invented from
 * partial data. Unknown extra fields are tolerated (forward compatibility),
 * but every field this plan promises must be present and correctly typed --
 * otherwise arbitrary JSON written at that path could become authoritative
 * fatal evidence.
 *
 * Pure: the file read belongs to the caller (`swarm-tool-context.ts`), which is
 * where I/O lives; this module is state- and I/O-free by its own header rule.
 */
export function parseFatalSidecar(raw: string): FatalSidecar | null {
  let parsed: unknown;
  try {
    parsed = JSON.parse(raw);
  } catch {
    return null;
  }
  if (typeof parsed !== "object" || parsed === null) return null;
  const rec = parsed as Record<string, unknown>;
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
    writtenAtMs: rec.writtenAtMs,
  };
}

/**
 * What happened to the worker's PROCESS, as far as can be known.
 *
 * `settled_alive` is named instead of `clean` on purpose: all that is known is
 * that herdr reported the process alive at its prompt and nothing anywhere
 * claims a death. That is the pre-existing status quo, not a stronger claim of
 * successful completion -- and every normal completion lands here, because a
 * healthy worker never writes a certificate.
 *
 * `unknown_crash` is a death with pane evidence but no in-process witness; it
 * is deliberately distinct from `fatal_error`, which the worker itself stated.
 */
export type ProcessResult =
  "fatal_error" | "unknown_crash" | "gone" | "deadline_stopped" | "settled_alive";

/**
 * Which observation produced `processResult`. Ranked: `fatal_sidecar` beats any
 * pane evidence, which beats a bare herdr status. Two values record the ABSENCE
 * of an observation rather than a conclusion (`no_observation`: nothing
 * external could be read at all, e.g. an abandoned liveness probe; `pane_clear`:
 * the pane was read and said nothing), because collapsing "looked and saw
 * nothing" into "could not look" would hide exactly the difference this item is
 * about.
 */
export type OutcomeEvidence =
  | "fatal_sidecar"
  | "pane_sentinel"
  | "provider_wording"
  | "pane_clear"
  | "herdr_status"
  | "no_observation";

/** How the independent backlog read went. A failed subprocess is not "no status". */
export type BacklogRead = "ok" | "failed" | "unparsed";

/**
 * What is known about a worker's process at the moment it was met, before the
 * independent backlog read is folded in. Produced by `classifyOutcomeDraft`.
 */
export interface OutcomeDraft {
  processResult: ProcessResult;
  evidence: OutcomeEvidence;
  sidecarProbed: boolean;
  herdrStatus?: string;
  note?: string;
}

/**
 * The reconciled per-worker outcome: the orchestrator's answer to "what
 * happened here", assembled before the worker's record is dropped and appended
 * to the run's `outcomes` ledger.
 */
export interface WorkerOutcome {
  /** From the run's own state, never self-reported by the worker. */
  runId: string;
  agent: string;
  slug: string;
  /** Copied for post-hoc legibility. Classification branches on the context's kind. */
  kind: "pi" | "copilot";
  processResult: ProcessResult;
  evidence: OutcomeEvidence;
  /** Whether a certificate was even looked for -- `false` for Copilot, always. */
  sidecarProbed: boolean;
  herdrStatus?: string;
  backlogStatus?: string;
  backlogRead: BacklogRead;
  decidedAtMs: number;
  note?: string;
}

/**
 * Classify what happened to a worker, from the three witnesses that exist, in
 * the only order that is safe: the worker's own statement first, pane evidence
 * second, bare herdr status last.
 *
 * Pure, so the whole precedence question is testable without a fake herdr, and
 * so no teardown path can invent its own evidence value.
 *
 * The invariant that makes this composable with the classification already done
 * upstream in `settleWait` is that a certificate can only ever move an event
 * TOWARD pessimism -- `finished` to `error`, never `error` to `finished`. So a
 * reclassification can never need to be undone, and no precedence argument has
 * to be settled between the two sites.
 *
 * `evidence` distinguishes "the pane was read and said nothing" (`pane_clear`)
 * from "nothing external could be read at all" (`no_observation`) deliberately:
 * collapsing them would hide the exact case the pane screens exist for.
 */
export function classifyOutcomeDraft(
  kind: "finished" | "timed_out" | "error",
  witnesses: {
    /** The parsed certificate, or null when absent/untrusted. */
    sidecar: FatalSidecar | null;
    /** Whether a certificate was looked for at all (false for a Copilot worker). */
    sidecarProbed: boolean;
    /** What the pane screens concluded, or null when the pane was never consulted. */
    paneMatch: "fatal_sentinel" | "provider_wording" | null;
    /** Whether a pane read happened, matched or not. */
    paneRead: boolean;
    herdrStatus?: string;
    livenessConfirmed?: boolean;
  },
): OutcomeDraft {
  const base: Omit<OutcomeDraft, "processResult" | "evidence"> = {
    sidecarProbed: witnesses.sidecarProbed,
    ...(witnesses.herdrStatus !== undefined ? { herdrStatus: witnesses.herdrStatus } : {}),
  };
  if (witnesses.sidecar) {
    return { ...base, processResult: "fatal_error", evidence: "fatal_sidecar" };
  }
  // Pane evidence is checked BEFORE the kind branches, not after. `kind` reaching
  // here may already have been reclassified `finished` -> `error` by
  // `screenFinishedForCrash`, and branching on that mutated kind first would
  // discard the very evidence that caused the mutation -- reporting a bare
  // `gone`/`herdr_status` for a death the pane screens positively identified.
  // Ranking is: certificate > pane evidence > herdr status, which is also the
  // order in which the witnesses are actually trustworthy.
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
      evidence: witnesses.livenessConfirmed === false ? "no_observation" : "herdr_status",
    };
  }
  if (kind === "error") {
    // The wait envelope's own error code IS the herdr observation here, so this
    // does not depend on a status field being parseable.
    return { ...base, processResult: "gone", evidence: "herdr_status" };
  }
  return {
    ...base,
    processResult: "settled_alive",
    evidence: witnesses.paneRead ? "pane_clear" : "no_observation",
  };
}

/**
 * Append an outcome to the run's ledger, replacing a re-reconciliation of the
 * same observation instead of stacking a duplicate.
 *
 * The ledger lives on `SwarmState` rather than only on `WorkerRecord` because
 * `swarmPoll` filters departing workers out of `state.workers` before it
 * persists: an outcome stored solely on a record being removed is deleted with
 * it and survives nowhere. Persisted here, it also survives an orchestrator
 * restart, so the end-of-run digest can be read from recorded truth instead of
 * re-derived from event prose.
 *
 * Key is `agent` + `decidedAtMs`, and a re-reconciliation (a serial
 * `teardown_ambiguous` worker met again on a later poll) must carry the
 * ORIGINAL `decidedAtMs` -- re-stamping it would make every pass a distinct key
 * and grow the ledger without bound while the queue is stuck.
 */
export function appendOutcome(state: SwarmState, outcome: WorkerOutcome): WorkerOutcome[] {
  const outcomes = state.outcomes ?? [];
  const at = outcomes.findIndex(
    (o) => o.agent === outcome.agent && o.decidedAtMs === outcome.decidedAtMs,
  );
  if (at >= 0) outcomes.splice(at, 1, outcome);
  else outcomes.push(outcome);
  state.outcomes = outcomes;
  return outcomes;
}

/** The outcome already recorded for this agent, if any -- the re-stamp guard. */
export function priorOutcome(state: SwarmState, agent: string): WorkerOutcome | undefined {
  return (state.outcomes ?? []).find((o) => o.agent === agent);
}

export type WorkerLifecycle = "active" | "awaiting_relay" | "teardown_ambiguous";

export interface WorkerRecord {
  agent: string; // synthetic id, e.g. "w1" -- never the raw slug (herdr names cap at 32 chars)
  slug: string;
  paneId: string;
  /**
   * The tab this worker owns, closed when it is dropped.
   *
   * Optional only for state files written before workers had their own tabs:
   * those workers live in split panes, and closing them still goes through
   * `pane close`. A worker spawned by this version always carries one.
   */
  tabId?: string;
  /**
   * The files this worker's item declared it would touch (`related_files`).
   *
   * Held on the record so a later wave can tell whether a candidate would
   * edit the same file as something already running, without re-querying
   * dev_status for items that have since left READY. Optional for records
   * written before scheduling existed: such a worker simply constrains
   * nothing, which is the pre-existing behaviour.
   */
  paths?: string[];
  /**
   * Epoch ms the worker's CURRENT working segment began.
   *
   * Stamped at spawn, folded into `accumulatedWorkingMs` when the worker
   * parks at awaiting_relay, and re-stamped when swarm_resolve_blocked
   * returns it to active. That is what makes the budget measure WORKING time
   * rather than wall time: a worker parked overnight waiting on a human would
   * otherwise resume already past its deadline and be stopped on its first
   * check-in -- destroying its work at the exact moment the human answered.
   *
   * Optional only for records written before budgets existed. Such a record
   * is stamped on its first check-in rather than left without a deadline:
   * "no deadline" would revive the unbounded hang, for exactly the state
   * files in flight across the upgrade.
   */
  workingSinceMs?: number;
  /**
   * Working time from this worker's COMPLETED segments, in ms. Absent means
   * zero.
   *
   * Without it, re-stamping `workingSinceMs` on every resume would not pause
   * the clock, it would erase it: a worker that works 3h50m, blocks on a
   * relay and is answered would start a fresh budget and could run 7h50m in
   * total. The budget is per item, not per segment.
   */
  accumulatedWorkingMs?: number;
  /**
   * The orchestrator cwd this worker's tab was created in, so a deliberate
   * stop can name the worktree its item was being worked in. Optional for
   * records written before this existed; absent means the report says so
   * rather than printing a guess.
   */
  cwd?: string;
  /**
   * How many check-ins this worker has had. Absent means none yet.
   *
   * On the record rather than in a runtime map because the working-time
   * fields beside it are persisted: a restart that kept a worker's 3h45m
   * elapsed but reset its count would report "check-in 1, 3h45m of a 4h
   * budget", which reads as a stall rather than a resumption.
   */
  checkIns?: number;
  /**
   * The model this worker was started on, when one was pinned.
   *
   * Recorded so a run can say what actually did the work. Absent means the
   * worker took pi's own default, which is what every worker did before this
   * was wired -- and which no digest could report, so a finished run could
   * not be reasoned about or reproduced after the fact.
   */
  model?: string;
  /**
   * Epoch ms this worker parked at `awaiting_relay`, cleared when it resumes.
   *
   * Separate from `workingSinceMs` on purpose: that clock stops here, so
   * without this one a parked worker has no clock at all. See
   * `stalledRelayWorkers`.
   */
  awaitingRelaySinceMs?: number;
  /**
   * The last relay answer that failed to match, if one did.
   *
   * Without it a failed resolve left no trace: the worker stayed at
   * awaiting_relay and the next poll reported an ordinary unanswered relay,
   * indistinguishable from one nobody had tried yet. That is exactly how the
   * 2026-09-03 stall formed and went unnoticed.
   */
  lastResolveFailure?: { answer: string; reason: string; at: number };
  /**
   * Mid-flight corrections sent to this worker.
   *
   * On the record so the end-of-run digest can say an item's premises changed
   * under a worker and when. The raw `herdr agent prompt` this replaces left
   * no trace: the run state had no idea an item had been amended, and the
   * orchestrator went on polling a worker whose instructions had been
   * rewritten underneath it.
   */
  amendments?: Amendment[];
  /**
   * A correction submitted mid-turn whose delivery the run has not yet accounted
   * for, or absent when nothing is outstanding.
   *
   * This exists because a submission into a `working` agent cannot be
   * acknowledged: herdr's prompt `--wait` may match the very turn being
   * corrected, and the terminal wait already armed can settle on that turn's
   * idle and tear the worker down with the correction still unread. The marker
   * makes the run hold that teardown until one of the two sound observations
   * lands (see `classifyAmendAck`), so a lost amendment becomes a named report
   * instead of an unqualified `finished`.
   *
   * Optional so a state file written before this existed loads unchanged and
   * behaves exactly as it did: no marker, no hold.
   */
  pendingAmend?: PendingAmend;
  lifecycle: WorkerLifecycle;
  /** Terminal event retained when serial teardown could not be confirmed. */
  terminalOutcome?: "finished" | "timed_out" | "error";
  /** Why teardown could not be confirmed; keeps restart behavior fail-closed. */
  teardownDetail?: string;
  /** Capture offers already harvested before an ambiguous teardown was persisted. */
  terminalCaptures?: { kind: string; id: string; summary: string }[];
  /**
   * The reconciled outcome assembled when this worker was met at teardown.
   *
   * Kept on the record only so a re-reconciliation (serial
   * `teardown_ambiguous`, met again on a later poll) can reuse its
   * `decidedAtMs` rather than stamping a new key and duplicating the ledger
   * entry; the durable copy is `SwarmState.outcomes`, because a record that
   * leaves `state.workers` takes its fields with it.
   */
  outcome?: WorkerOutcome;
  /**
   * Copilot-only: the confirmed session id `attemptCrashRecovery` resumes
   * via `--resume=`. Unused by pi workers -- absent means recovery never
   * applies, which is the correct behavior for a host with no such feature.
   */
  copilotSessionId?: string;
  /** Copilot-only: how many times `attemptCrashRecovery` has resumed this worker, capped at MAX_RECOVERY_ATTEMPTS. Unused by pi workers. */
  recoveryAttempts?: number;
}

export interface SwarmState {
  runId: string;
  concurrency: number;
  /** Missing on legacy state files, where it means the historical concurrent mode. */
  mode?: ExecutionMode;
  nextCounter: number;
  workers: WorkerRecord[];
  /**
   * Every slug this run has already handed to a worker, successfully or not.
   *
   * Automatic selection reads the READY set fresh on each wave, and a worker
   * that dies without reaching `dev_status.py start` leaves its item exactly
   * as it found it -- READY. Without this the next wave selects that same
   * item again, and again, which is the "silently retried" behaviour
   * swarm_poll's own guidance rules out. Caught on a live run: a worker whose
   * tab was closed was re-spawned by the very next wave.
   *
   * Attempted, not completed, is the right key. The run should not re-select
   * an item it already tried, whatever the outcome; a human decides whether a
   * failure is worth another go, from the digest.
   *
   * A DEFERRED item is not attempted -- it was never handed to anyone, and
   * becoming schedulable later is the entire point of deferring it.
   *
   * Optional for state files written before this existed; absent means the
   * run has attempted nothing it can prove, which is the old behaviour.
   */
  attempted?: string[];
  /** Items this run classified as permanently ineligible for its execution mode. */
  refused?: { slug: string; reason: string }[];
  /**
   * The slug prefix this run was scoped to, stamped when a fresh state is
   * initialized and carried through every save.
   *
   * herdr_delegate.py's `restart` mode discovers the runId to resume by
   * matching this field exactly against the prefix it was invoked with.
   * Legacy files written before the field existed fall back to matching
   * worker/`attempted` slugs -- a substring-prefix collision could fool the
   * scan (e.g. a hypothetical `auth` prefix inside `auth-api-` slugs), which
   * is why the exact field exists rather than the scan alone. Optional for
   * the same reason every other field added here is: old state files on disk.
   */
  prefix?: string;
  /**
   * Copilot-only: the run's `--plugin-dir`, persisted so a later crash
   * recovery (which may happen long after the initial spawn, or after a
   * full process restart) keeps using the value the run was actually
   * started with rather than falling back to a guessed default. Unused by
   * pi, which loads extensions live rather than via a plugin-dir flag.
   */
  pluginDir?: string;
  /**
   * Every reconciled worker outcome this run has observed, in decision order.
   *
   * Optional for the same reason every other field added here is optional:
   * state files already on disk predate it. Absent means nothing has been
   * reconciled yet, which is what an empty array also says, so no legacy file
   * needs rewriting and no reader has to special-case the upgrade.
   */
  outcomes?: WorkerOutcome[];
}

// ---------------------------------------------------------------------------
// Naming
// ---------------------------------------------------------------------------

/**
 * Project prefixes backlog slugs share within a wave. Stripped before naming
 * because they carry no distinguishing information -- the head of every slug
 * in a run is often identical (three `meta-second-opinion-*` items once
 * rendered as the same 32-char name), so the budget is better spent on the
 * tail, which is what actually tells items apart. Longest match first, so a
 * future prefix that extends another (e.g. `meta-x-` vs `meta-`) strips
 * correctly, and only that one is removed.
 */
// Historical note: this list once had a copilot-side duplicate
// (copilot/extensions/swarm/src/swarm-scheduling.ts) that drifted --
// copilot's copy correctly gained "atk-", this one did not, so the same
// atk-* item got a different synthetic agent name depending which harness's
// worker picked it up. This file is now the single shared source, so that
// class of drift is structurally impossible rather than merely guarded
// against.
export const PROJECT_PREFIXES = ["iron-lb-", "meta-", "work-", "atk-"];

/** Synthetic herdr agent name incorporating the slug, capped at herdr's 32-char limit.
 *
 * Truncates from the slug's HEAD, keeping its tail: the `w<counter>` segment
 * guarantees uniqueness within a run, so the slug's only job here is
 * readability, and the tail is the part a human maps back to an item.
 */
export function nextAgentId(runId: string, counter: number, slug?: string): string {
  const cleanSlug = slug ? slug.replace(/[^a-zA-Z0-9_-]/g, "") : "";
  // Longest match, and only ONE: reduce-and-strip-each would take both
  // prefixes off a slug like `meta-work-foo` and leave `foo`, silently
  // discarding a segment that distinguishes it.
  const matched = PROJECT_PREFIXES.filter((prefix) => cleanSlug.startsWith(prefix)).sort(
    (a, b) => b.length - a.length,
  )[0];
  const stripped = matched ? cleanSlug.slice(matched.length) : cleanSlug;
  if (!stripped) return `${runId}-w${counter}`;
  const base = `${runId}-w${counter}-${stripped}`;
  if (base.length <= 32) return base;
  // Not enough budget for any slug tail (pathological runId): fall back to a
  // plain truncation rather than slicing a negative count.
  const remaining = 32 - `${runId}-w${counter}-`.length;
  if (remaining < 1) return base.slice(0, 32);
  return `${runId}-w${counter}-${stripped.slice(-remaining)}`;
}

export function stalledRelayWorkers(
  workers: WorkerRecord[],
  now: number,
  stallMs: number,
): WorkerRecord[] {
  return workers.filter(
    (w) =>
      w.lifecycle === "awaiting_relay" &&
      w.awaitingRelaySinceMs !== undefined &&
      now - w.awaitingRelaySinceMs >= stallMs,
  );
}

/** One READY item, as much of it as scheduling needs. */
export interface ReadyItem {
  id: string;
  /**
   * Whether a worker may be given this item, as reported by
   * `dev_status.py ready`.
   *
   * Deliberately `unknown` rather than `boolean | undefined`: the value comes
   * from a JSON payload this module does not control, and the check below
   * requires an explicit `true`, so anything else -- absent, null, a string --
   * lands in the same fail-closed branch.
   */
  worker_safe?: unknown;
  /** Serial-run eligibility is stricter and permits isolated harness-repo work. */
  serial_safe?: unknown;
  /** Stable classifier explanation emitted by dev_status.py for a serial refusal. */
  serial_safety_reason?: unknown;
  related_files?: { path?: unknown }[];
}

export function parseReadyItems(stdout: string): ReadyItem[] {
  try {
    const parsed: unknown = JSON.parse(stdout);
    if (!Array.isArray(parsed)) return [];
    return parsed.filter((i): i is ReadyItem => typeof (i as ReadyItem)?.id === "string");
  } catch {
    return [];
  }
}

/** One `show` result, as much of it as the finish-evidence check needs. */
export interface ShownItem {
  /**
   * `dev_status.py`'s status field: one of `VALID_STATUSES`
   * (`"open"`/`"in-progress"`/`"in-review"`/`"done"`) in practice, but
   * `unknown` until checked -- the payload comes from JSON this module does
   * not control.
   */
  status?: unknown;
}

export function parseShownItem(stdout: string): ShownItem | null {
  try {
    const parsed: unknown = JSON.parse(stdout);
    return parsed && typeof parsed === "object" ? (parsed as ShownItem) : null;
  } catch {
    return null;
  }
}

/**
 * Whether a "finished" event has zero evidence of real progress: the item's
 * dev_status.py status never advanced past open/in-progress, and no capture
 * was queued. Fail open -- a `shownStatus` that isn't exactly "open" or
 * "in-progress" (including a failed/unparseable query's `undefined`/`null`)
 * never counts as suspicious; only a positive, successfully-read status does.
 */
export function isSuspiciousFinish(shownStatus: unknown, captureCount: number): boolean {
  if (captureCount > 0) return false;
  return shownStatus === "open" || shownStatus === "in-progress";
}

// ---------------------------------------------------------------------------
// Provider-crash detection on a settled finish.
//
// A worker whose provider hits a usage limit keeps its process alive at the
// idle prompt -- herdr sees `idle`, `classifyWaitResult` maps that to
// `finished`, and the orchestrator tears the worker down as a clean finish
// while the item's real state is unknown (observed live 2026-09-10, where
// workers died mid-commit/mid-merge under "The usage limit has been reached"
// and the run recorded them as complete). This classifier detects the
// positive evidence of death in the pane's last lines; the caller reclassifies
// the finish to `error`. Distinct from `isSuspiciousFinish`, which detects the
// ABSENCE of progress evidence: absence-of-evidence and evidence-of-failure
// stay separate checks.

/** How many non-empty lines at the end of a settled pane are examined. */
export const PROVIDER_CRASH_SCAN_LINES = 10;

/** Maximum normalized-text character distance between the two halves of a signature pair. */
export const PROVIDER_CRASH_MAX_GAP = 80;

/**
 * Error-shaped signature pairs: both halves must appear (case-insensitive)
 * within `PROVIDER_CRASH_MAX_GAP` characters of each other, in either order,
 * so a bare keyword in prose or task output cannot match on its own. A future
 * provider's wording is a one-line addition here.
 */
const PROVIDER_CRASH_SIGNATURE_PAIRS: ReadonlyArray<readonly [string, readonly string[]]> = [
  ["usage limit", ["reached", "exceeded"]],
  ["rate limit", ["exceeded", "hit"]],
];

/** CSI/OSC escape sequences, then any remaining C0 control characters (tab kept). */
function stripTerminalNoise(text: string): string {
  // The patterns contain control characters by design (they strip terminal
  // escape sequences), so the no-control-regex rule is suppressed per line.
  // eslint-disable-next-line no-control-regex
  const csi = /\x1b\[[0-9;?]*[ -/]*[@-~]/g;
  // eslint-disable-next-line no-control-regex
  const osc = /\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)/g;
  // eslint-disable-next-line no-control-regex
  const c0 = new RegExp(`[\\x00-\\x08\\x0b-\\x1f\\x7f]`, "g");
  return (
    text
      // CSI: ESC [ params final-byte; allow malformed/unterminated to survive
      // the replace (they then get dropped by the control-char pass below).
      .replace(csi, "")
      // OSC: ESC ] ... BEL or ST.
      .replace(osc, "")
      .replace(c0, "")
  );
}

/**
 * The last `windowLines` non-empty lines of a pane capture, joined and
 * normalized for substring matching: ANSI/CSI/OSC and other control characters
 * stripped, whitespace collapsed.
 *
 * Shared by `providerCrashMatch` and `fatalErrorExitMatch` so the two screens
 * cannot disagree about what the pane looked like: selecting by LINE before
 * joining is what re-joins a banner the pty hard-wrapped, and the collapse is
 * what survives TUI redraw residue. Both therefore see the same text the other
 * sees, and a signature only has to survive one normalization.
 */
function normalizedPaneWindow(content: string, windowLines: number): string {
  const recent = content
    .split("\n")
    .filter((line) => line.trim() !== "")
    .slice(-windowLines)
    .join(" ");
  return stripTerminalNoise(recent).replace(/\s+/g, " ");
}

/** All occurrence start indices of `needle` in `haystack` (case-insensitive). */
function occurrenceIndices(haystack: string, needle: string): number[] {
  const lower = haystack.toLowerCase();
  const target = needle.toLowerCase();
  const indices: number[] = [];
  for (let i = lower.indexOf(target); i !== -1; i = lower.indexOf(target, i + 1)) {
    indices.push(i);
  }
  return indices;
}

export interface ProviderCrashMatch {
  /** Human-readable signature name, e.g. `usage limit ~ reached`. */
  signature: string;
  /** Bounded surrounding text from the normalized pane, for the event detail. */
  excerpt: string;
}

const EXCERPT_BEFORE = 60;
const EXCERPT_AFTER = 140;

/**
 * Detect a provider-level crash in a settled worker's pane text.
 *
 * Normalization: the last `PROVIDER_CRASH_SCAN_LINES` non-empty lines (a
 * provider error banner sits immediately above the idle prompt a crash leaves
 * behind; selecting by line before joining also re-joins hard-wrapped
 * banners), then ANSI/CSI/OSC and other control characters are stripped, then
 * whitespace is collapsed -- so a banner wrapped across captured lines still
 * matches. A banner pushed more than 10 lines above the prompt by post-error
 * output is missed: accepted blind spot, fail-safe direction (the finish then
 * still passes `isSuspiciousFinish`'s cross-check).
 *
 * Returns the first qualifying match (signature name plus a bounded excerpt)
 * or null. Pane text is evidence of output, not proof of death -- the caller
 * reports the outcome as a verification-needed error, never as a definitive
 * item failure.
 */
export function providerCrashMatch(content: string): ProviderCrashMatch | null {
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
            const excerpt =
              (start > 0 ? "..." : "") +
              normalized.slice(start, end).trim() +
              (end < normalized.length ? "..." : "");
            return { signature: `${keyword} ~ ${verb}`, excerpt };
          }
        }
      }
    }
  }
  return null;
}

// ---------------------------------------------------------------------------
// The deterministic fatal-exit sentinel.
//
// Sibling of `providerCrashMatch` above, and deliberately separate from it:
// that screen hunts error-shaped WORDING, which is a heuristic with its own
// blind spots; this one looks for a fixed token a worker writes to stderr
// immediately before exiting 1, which is not a guess about the text at all.
//
// BOTH SCREENS ARE NOW THE FALLBACK. A worker that reaches its own fatal verdict
// also writes a death certificate next to its capture file
// (`fatal-error-exit.ts`, read by `swarm-tool-context.ts` as
// `evidence: "fatal_sidecar"`), and that statement outranks anything inferred
// from pane text. These two remain authoritative for exactly the cases with no
// in-process witness: a Copilot worker (no pi to run the writer), and a pi
// killed by SIGKILL/OOM or crashed inside pi itself, which never reaches the
// handler at all. Deleting them would report every such death as a clean
// finish, because `classifyWaitResult` maps herdr's `done` to `finished`.
// ---------------------------------------------------------------------------

/**
 * The sentinel `fatal-error-exit.ts` writes to stderr on its way out of an
 * unattended pi whose run settled on a fatal turn error.
 *
 * This is a deliberate DUPLICATE of the extension's own literal, not an import:
 * a worker has no reason to know the orchestrator's module set exists, and
 * `swarm-lib` is bundled into the Copilot swarm build, so pulling pi-extension
 * concerns in here would drag them along. `pi/test/fatal-error-exit.test.ts`
 * binds the two copies by feeding the extension's real emitted line through
 * `fatalErrorExitMatch`, so renaming either half fails that test.
 *
 * The token is only the START of the emitted sentence, which is what makes it
 * the right thing to match: the full line runs past 130 characters at the
 * widest legal model id, and a pty wrap in the middle of it would leave the
 * pane carrying text the source never contained (the window below re-joins
 * wrapped lines with a space). The 18-character sentinel needs a sub-19-column
 * pane to split.
 */
export const FATAL_ERROR_EXIT_TOKEN = "[fatal-error-exit]";

/**
 * Non-empty lines at the end of a settled pane scanned for the sentinel.
 *
 * Deliberately wider than `PROVIDER_CRASH_SCAN_LINES`, and the reason is the
 * difference in what the two windows defend against: the 10-line fuzzy window
 * exists to stop "usage limit" in ordinary prose or old scrollback from
 * matching, a risk a deterministic sentinel does not carry. Its own failure
 * direction is the opposite one -- the sentinel is written immediately before
 * `exit(1)`, and a shell prompt, a multiplexer redraw or a teardown banner can
 * then land below it in the capture, so a 10-line window could drop the only
 * evidence that the process died. The pane read is 200 lines
 * (`PANE_CAPTURE_LINES`, `swarm-tool-context.ts`), so 40 costs nothing.
 */
export const FATAL_ERROR_EXIT_SCAN_LINES = 40;

/**
 * Detect the fatal-exit sentinel in a settled worker's pane text.
 *
 * Why this exists next to `providerCrashMatch` rather than as another entry in
 * its signature pairs: herdr publishes the terminal `done` status ~0.17 s
 * before the agent record disappears (measured live 2026-09-11), so the armed
 * wait in `settleWait` always resolves against `done`, which
 * `classifyWaitResult` maps to `finished` exactly like the parked-at-prompt
 * `idle` signal. An exit-1 therefore buys no classification advantage on the
 * primary path unless the finished settle is screened for it. That mapping is
 * left alone deliberately -- `done` is also what a clean worker exit looks like
 * -- so the split happens here, on positive evidence in the pane.
 *
 * Shares `providerCrashMatch`'s normalization through `normalizedPaneWindow`,
 * so a pty-wrapped or TUI-residue-laden line still matches; only the window is
 * its own. Returns the first hit, or null. The CALLER additionally gates on the
 * settle status, because pane text is evidence of output only -- the status is
 * what says the process is gone.
 */
export function fatalErrorExitMatch(content: string): ProviderCrashMatch | null {
  const normalized = normalizedPaneWindow(content, FATAL_ERROR_EXIT_SCAN_LINES);
  const at = normalized.indexOf(FATAL_ERROR_EXIT_TOKEN);
  if (at === -1) return null;
  const start = Math.max(0, at - EXCERPT_BEFORE);
  const end = Math.min(normalized.length, at + FATAL_ERROR_EXIT_TOKEN.length + EXCERPT_AFTER);
  const excerpt =
    (start > 0 ? "..." : "") +
    normalized.slice(start, end).trim() +
    (end < normalized.length ? "..." : "");
  return { signature: FATAL_ERROR_EXIT_TOKEN, excerpt };
}

/** The files an item declares it will touch. Absent or malformed entries simply contribute nothing. */
export function itemPaths(item: ReadyItem): string[] {
  const paths = (item.related_files ?? [])
    .map((f) => f?.path)
    .filter((p): p is string => typeof p === "string" && p.length > 0);
  return [...new Set(paths)];
}

/**
 * True when two declared paths refer to overlapping work.
 *
 * Equality, or one containing the other as a directory. The separator check
 * is the point: plain string prefixing would make "/r/pkg" swallow
 * "/r/pkg-other", deferring unrelated items forever.
 */
function pathsCollide(a: string, b: string): boolean {
  // Trailing slashes are stripped first, or a directory written "/repo/pkg/"
  // builds the prefix "/repo/pkg//" and matches nothing inside itself.
  const x = a.replace(/\/+$/, "");
  const y = b.replace(/\/+$/, "");
  if (x === y) return true;
  return x.startsWith(`${y}/`) || y.startsWith(`${x}/`);
}

export interface SelectionResult {
  slugs: string[];
  /** Held back because another item in this wave, or a running worker, edits the same file. */
  deferred: { slug: string; reason: string }[];
  /** Held back only because the concurrency cap was already full. */
  skipped: string[];
  /**
   * Never schedulable: a worker must not take this item at all.
   *
   * A third category on purpose. `skipped` is coming next wave regardless and
   * `deferred` is waiting on a named worker, so both end when a worker
   * finishes -- but a refused item is owed nothing and will never be spawned.
   * Folding it into either would leave the orchestrator polling for a worker
   * that was never started, on a queue that cannot drain.
   */
  refused: { slug: string; reason: string }[];
}

/**
 * Choose which candidates may run together.
 *
 * Two items that edit the same file cannot run concurrently: each worker gets
 * its own worktree, so the second one to merge conflicts. dev_status already
 * prevents two sessions claiming the same ITEM; nothing prevented two items
 * claiming the same FILE, and that is the collision that actually occurred --
 * meta-swarm-trust-ack-fail-open and meta-swarm-poll-abort-and-orphan-pane
 * both edit swarm-tool.ts and had to be held apart by hand.
 *
 * `deferred` and `skipped` are kept apart because the orchestrator acts
 * differently on them: a skipped item is coming next wave whatever happens,
 * while a deferred one is waiting on a specific worker to finish.
 *
 * `takenPaths` carries a HOLDER per path -- `worker <agent> (<slug>,
 * <lifecycle>)` for a running worker's claim, `candidate <slug> (selected
 * earlier this wave)` for an in-wave selection -- so the deferral reason
 * names the specific work holding the file. First match wins, in candidate
 * `related_files` order against taken insertion order (worker records in
 * state order, then selections): one holder, one path per reason, always
 * deterministic.
 *
 * Termination rests on one property: with no worker running and no item yet
 * selected, the first candidate collides with nothing, so a non-empty queue
 * always yields at least one spawn. A deferred item therefore cannot be
 * deferred forever -- the wave that defers it must have spawned the worker it
 * collided with, and that worker finishes.
 */
export function selectSchedulable(
  candidates: readonly ReadyItem[],
  takenPaths: readonly { path: string; holder: string }[],
  headroom: number,
  mode: ExecutionMode = "concurrent",
): SelectionResult {
  const slugs: string[] = [];
  const deferred: { slug: string; reason: string }[] = [];
  const skipped: string[] = [];
  const refused: { slug: string; reason: string }[] = [];
  const taken = [...takenPaths];
  const seen = new Set<string>();

  for (const candidate of candidates) {
    // A slug repeated in an explicit `items` list would otherwise pass every
    // check twice and spawn two workers onto one backlog item, each in its own
    // worktree, racing each other's commits.
    if (seen.has(candidate.id)) continue;
    seen.add(candidate.id);
    // Before the cap and before the collision check: a refused item must never
    // be reported as skipped, which would promise it a later wave that will
    // never take it, nor as deferred, which would promise it a worker.
    const eligibility = mode === "serial" ? candidate.serial_safe : candidate.worker_safe;
    if (eligibility !== true) {
      refused.push({
        slug: candidate.id,
        reason:
          mode === "serial" && typeof candidate.serial_safety_reason === "string"
            ? candidate.serial_safety_reason
            : eligibility === false
              ? "the backlog reports this item is not worker-safe -- its prefix " +
                "names the harness repo, or is unrecognised. A worker would be " +
                "editing the code it is running. Work it in a normal session."
              : `dev_status.py ready reported no ${mode === "serial" ? "serial_safe" : "worker_safe"} field for this ` +
                "item, so eligibility is unknown and it is refused rather than " +
                "assumed safe. Update the installed dev_status.py.",
      });
      continue;
    }
    if (slugs.length >= headroom) {
      skipped.push(candidate.id);
      continue;
    }
    const paths = itemPaths(candidate);
    let clashPath: string | undefined;
    let clashHolder: string | undefined;
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
        reason: `file overlap with ${clashHolder}: ${clashPath}`,
      });
      continue;
    }
    slugs.push(candidate.id);
    taken.push(
      ...paths.map((p) => ({
        path: p,
        holder: `candidate ${candidate.id} (selected earlier this wave)`,
      })),
    );
  }

  return { slugs, deferred, skipped, refused };
}

/** One recorded mid-flight correction of a worker's item. */
export interface Amendment {
  at: number;
  by: string;
}

/**
 * An outstanding amendment and the observations made about it.
 *
 * Persisted rather than held in a runtime map for the same reason the
 * working-time fields beside it are: a restart that dropped the marker would
 * restore the exact race it protects against, silently, on the one worker that
 * had a correction in flight.
 */
export interface PendingAmend {
  /** Epoch ms the correction was submitted. Starts both the steering clock and the hold bound. */
  requestedAtMs: number;
  /**
   * The agent's `state_change_seq` read immediately before submission, or null
   * if that probe gave no usable answer -- in which case the gap tie-breaker
   * cannot fire and the hold falls back to the elapsed axis alone.
   */
  seqAtRequest: number | null;
  /** The last `state_change_seq` observed FOR THIS AGENT while the hold was live. */
  lastObservedSeq: number | null;
  /**
   * How long the run kept working AFTER the correction was submitted, as
   * measured when the hold first fired, or -1 until it has.
   *
   * The second release axis. The armed terminal wait settles promptly on the
   * transition, so this bounds how long pi had to take the message as steering
   * inside the run -- see `AMEND_STEERING_WINDOW_MS`. Measured once, at the
   * first hold, because a later hold would measure a turn this whole question is
   * about.
   */
  runWorkedAfterAmendMs: number;
  /**
   * `await_turn` -- the run settled with the correction unacknowledged and the
   * hold is watching for the amendment's turn to start.
   * `await_terminal` -- that turn was seen starting; the hold is now waiting for
   * it to finish, and the settle that follows releases as `confirmed`.
   */
  phase: "await_turn" | "await_terminal";
  /** How many ack waits this hold has armed, bounded by `AMEND_ACK_MAX_CHECKS`. */
  checks: number;
  /**
   * Whether the check-in for this hold has been reported. A hold pushes exactly
   * one `still_working` rather than one per settle, so a worker that keeps
   * settling cannot flood the event stream the orchestrator drains.
   */
  checkInReported: boolean;
}

// ---------------------------------------------------------------------------
// Concurrency and pane accounting -- pure, so the cap/backpressure rules are
// independently testable from the async pool machinery that calls them.
// ---------------------------------------------------------------------------

/**
 * Workers with a correction in flight whose delivery the run has not accounted
 * for. Held separately from `activeWorkerCount` on purpose: these workers ARE
 * active, and their slot must stay occupied, but the orchestrator needs to know
 * WHY nothing is settling, or a hold reads as a stall.
 */
export function pendingAmendWorkers(state: SwarmState): WorkerRecord[] {
  return state.workers.filter((w) => w.pendingAmend !== undefined);
}

/** How many workers are being held open for an unacknowledged amendment. */
export function pendingAmendCount(state: SwarmState): number {
  return pendingAmendWorkers(state).length;
}

export function activeWorkerCount(state: SwarmState): number {
  return state.workers.filter((w) => w.lifecycle === "active").length;
}

export function canSpawnNew(state: SwarmState): boolean {
  if (state.mode === "serial") return state.workers.length === 0;
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
  if (state.mode === "serial") return state.workers.length === 0 && readyCount > 0 ? 1 : 0;
  const byConcurrency = Math.max(0, state.concurrency - activeWorkerCount(state));
  const byPaneCap = Math.max(0, openPaneSoftCap(state.concurrency) - openPaneCount(state));
  return Math.min(byConcurrency, byPaneCap, readyCount);
}

// herdr protocol helpers: argv construction (every element a discrete
// argv item, never a concatenated shell string) and response
// interpretation for tab/agent/pane commands. Extracted verbatim from
// swarm-tool.ts; no module state, no I/O.
import { basename, dirname, join } from "node:path";
import type { WorkerRecord } from "./swarm-scheduling";
const AGENT_START_TIMEOUT_MS = 30_000;

/**
 * `still_working` is the only NON-terminal kind: the wait window elapsed, the
 * worker was confirmed alive, its budget has not run out, and a fresh wait is
 * already armed. Nothing was closed and no slot was freed.
 *
 * It exists because the alternative -- re-arming silently and emitting
 * nothing -- would make a single swarm_poll call block for up to the whole
 * worker budget: `waitForEvent` has no timeout of its own, so the poll parks
 * until an event or an abort. A check-in keeps the caller's block bounded by
 * `timeoutMs` exactly as it was before, and gives the orchestrator something
 * honest to show a human ("check-in 7, 3h31m of a 4h budget") instead of
 * silence that looks identical to a wedged run.
 */
export type PollEventKind = "blocked" | "finished" | "timed_out" | "error" | "still_working";

/**
 * One worker, one herdr tab.
 *
 * The shipped version carved worker panes out of the orchestrator's own pane
 * with `pane split`, and everything that made that hard -- an equal-share
 * split plan, a 40x10 usability floor, a batch trimmed when the terminal
 * could not fit it -- existed only because panes inside one tab divide a
 * fixed width between them. Tabs do not: measured against herdr 0.8.2 on
 * 2026-09-02, a `tab create --no-focus` root pane reports the full terminal
 * (168x38 here) while unfocused, and a pi agent started in it reads back at
 * that same size.
 *
 * Width was never only a comfort question. A worker pane in a three-way split
 * was 42 columns, which is narrow enough that pi wraps its own picker footer,
 * which is what made every relay in the first live swarm run fail to parse.
 * Removing the split removes that whole class, and concurrency stops being
 * bounded by the terminal's geometry.
 *
 * `--label` carries the slug, so the tab bar names the item -- the pane
 * rename this replaces was only ever visible in the sidebar.
 *
 * `--env` is what makes the worker's gates resolve themselves. It is set on
 * the tab, so it is in pi's environment before pi starts, which is the whole
 * reason there is no trust prompt to send afterwards -- see
 * WORKER_UNATTENDED_ENV.
 */
export function buildTabCreateArgv(cwd: string, label: string, captureFile?: string): string[] {
  return [
    "tab",
    "create",
    "--cwd",
    cwd,
    "--label",
    label,
    "--env",
    WORKER_UNATTENDED_ENV,
    // A second --env, not a replacement: the worker needs both, and the
    // unattended flag is what settles its gates at module load.
    ...(captureFile ? ["--env", `PI_SWARM_CAPTURE_FILE=${captureFile}`] : []),
    "--no-focus",
  ];
}

export function buildTabCloseArgv(tabId: string): string[] {
  return ["tab", "close", tabId];
}

export function buildTabListArgv(): string[] {
  return ["tab", "list"];
}

/**
 * The id of the one tab carrying `label`, if there is exactly one.
 *
 * Used to recover from a `tab create` that exits 0 with output that will not
 * parse: the tab exists, and the id needed to close it was in precisely the
 * response that could not be read. The label is the slug this spawn asked
 * for, so it is the only handle left.
 *
 * Deliberately refuses to guess. Two tabs sharing the label cannot say which
 * one this spawn created, and closing the wrong one would close a tab a human
 * opened -- worse than the leak it is trying to clean up. Zero matches means
 * the same thing from the other side. Both cases return undefined so the
 * caller reports the leak by name instead.
 */
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

/** The two ids a worker needs: the pane to start its agent in, the tab to close when it is done. */
export interface TabCreateResult {
  paneId: string;
  tabId: string;
}

/**
 * Read both ids out of a `herdr tab create` response.
 *
 * They live in different objects -- `.result.root_pane.pane_id` and
 * `.result.tab.tab_id` -- and a worker is only recordable with both. One
 * without the other produces a tab that can be started into and never closed,
 * which is the orphan class this change is meant to end, so a partial
 * response is treated as no response at all.
 */
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

export function buildAgentStartArgv(agentId: string, paneId: string, model?: string): string[] {
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
    // Everything pi needs goes AFTER the separator. herdr's usage is
    // `agent start <NAME> --kind <KIND> --pane <ID> [OPTIONS] [-- [AGENT_ARG]...]`,
    // so `--model` handed to herdr directly is an unknown flag; only the
    // trailing block reaches the agent. Omitting the model omits the
    // separator too -- a bare trailing `--` is a different command line.
    ...(model ? ["--", "--model", model] : []),
  ];
}

/**
 * Marks a worker's tab as unattended, read by both gate extensions at module
 * load.
 *
 * Pi ships no permission system of its own (docs/usage.md's Design
 * Principles: "it intentionally does not include ... permission popups").
 * This repo supplies two: permission-gate.ts confirms bash outside its
 * allowlist, guard-rails.ts confirms `rm -rf` and `sudo`. Both raise
 * `ctx.ui.confirm`, and a worker's `ctx.hasUI` is true because it really is a
 * TUI -- one with nobody in front of it. The worker then waits forever while
 * `agent_status` still reads "working", so swarm_poll sees progress and the
 * orchestrator relays nothing. Observed live on 2026-09-02, twice.
 *
 * Passing this at tab creation replaces a slash-command handshake that tried
 * to talk one gate down after the fact and then prove it had worked, with a
 * token, an ack file, a poll and a 15-second deadline. The environment is set
 * before pi starts, so there is no prompt to deliver, nothing to time out,
 * and no window in which a worker holds real work while still armed. It also
 * sidesteps the reason that handshake could only ever cover one gate: pi
 * loads each extension separately, so a session-wide switch cannot be shared
 * between them in module state -- /trust-session tried exactly that and was
 * a silent no-op until it was rewired onto the shared extension event bus.
 * An environment variable each gate reads for itself has no such failure
 * mode, which is why the swarm still prefers it over any runtime handshake.
 *
 * The two gates draw DIFFERENT conclusions from it, on purpose:
 *   - permission-gate.ts allows. Its "ask" tier is everything outside a
 *     narrow allowlist, and a worker that cannot run tests or git is useless.
 *     This is what the swarm already did by sending /permission-gate-disable.
 *   - guard-rails.ts blocks. `rm -rf` and `sudo` are refused with a reason
 *     the worker can read, rather than asked about. Every other guard-rails
 *     rule -- protected-path writes, the git-commit-on-main worktree policy
 *     -- stays armed in a worker exactly as in an attended session.
 *
 * So this is not a blanket grant of autonomy. It is the statement "no human
 * will answer a dialog here", which is simply true of a swarm worker.
 */
export const WORKER_UNATTENDED_ENV = "PI_AGENT_UNATTENDED=1";

/**
 * The entire payload of an amend. Fixed, and deliberately carries no
 * correction text.
 *
 * The correction lives in the backlog store, edited there before this is
 * sent. The moment this channel carries content instead, the store stops
 * being the single source of truth and the two can disagree -- a worker
 * acting on a message while `show` says something else is worse than the
 * problem being solved. So there is no parameter to smuggle content through:
 * a caller who wants to change the work changes the item.
 *
 * On 2026-09-03 a worker was several minutes into an item whose stored
 * premise was wrong -- it was reasoning carefully toward a fix that would
 * have broken the user's work machine. The correction went through three raw
 * `herdr agent prompt` calls. It worked, but only because a human happened
 * to be watching, and the run recorded none of it.
 */
export const AMEND_INSTRUCTION =
  "STOP and re-read your backlog item before doing anything else: run " +
  "`python3 ~/.claude/scripts/dev_status.py show <your slug>` and read the " +
  "whole record fresh. Its context or next_steps have been corrected since " +
  "you started, so any plan you formed from the earlier version may now be " +
  "wrong. Reconcile what you have already done against the updated record, " +
  "and say plainly what changes as a result before continuing.";

/**
 * `--wait` blocks until the agent settles, so a follow-up prompt can't land
 * while it is still processing this one.
 *
 * Not usable for a client-side slash command. herdr 0.8.2 documents `--wait`
 * from a non-working state as requiring an observed lifecycle change within
 * 5000 ms, with no flag to relax it. A pi slash command applies instantly and
 * never enters the working state, so there is nothing to observe and the call
 * always returns agent_prompt_stalled. Confirmed live on 2026-09-02 against a
 * real pi; it is why the worker trust step was a prompt-plus-ack rather than
 * a prompt-plus-wait, before an environment variable removed the step.
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

/**
 * How a worker is torn down.
 *
 * A worker spawned by this version owns its whole tab, so closing the tab is
 * what removes it. `paneId` is the fallback for a worker restored from a
 * state file written when workers lived in panes split out of the
 * orchestrator's own -- closing its pane is still the right cleanup for that
 * layout, and a run in flight across the upgrade should not leak.
 */
export function buildWorkerCloseArgv(worker: WorkerRecord): string[] {
  return worker.tabId ? buildTabCloseArgv(worker.tabId) : buildPaneCloseArgv(worker.paneId);
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
 * What swarm_poll's blocked-state resync pass does with a parked
 * (`awaiting_relay`) record's `herdr agent get` result.
 *
 * A parked record has NO wait armed, so if its gate was answered out-of-band
 * -- direct pane keys, not `swarm_resolve_blocked` -- or its agent exited,
 * nothing will ever produce an event for it and the record outlives its
 * worker: deferring new spawns on its "held" worktree, burning a pane-cap
 * slot, and reporting an unanswered relay nobody is waiting on. Resync
 * re-derives the truth from live herdr instead of trusting the record.
 *
 * `drop` fires only on the one code that positively means gone (the same
 * rule `classifyTimeoutProbe` applies); `unpark` fires only on the KNOWN
 * non-blocked statuses, never "status != blocked" in general -- a future
 * herdr status like `paused` must not be handed a transition into a state
 * the worker's wait cannot settle. Everything else keeps the record exactly
 * as it was: fail open, an inconclusive check never mutates a worker.
 */
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

/**
 * What swarm_poll does with a worker whose wait window elapsed.
 *
 * `rearm` emits no event at all beyond the check-in; `event` is a real
 * outcome. `livenessConfirmed` is carried on a `timed_out` so the report can
 * tell the truth about which of two very different things happened -- see
 * `deadlineStopDetail`.
 */
export type TimeoutVerdict =
  | { disposition: "rearm" }
  | { disposition: "event"; kind: PollEventKind; livenessConfirmed?: boolean };

/** The liveness probe's outcome. `abandoned` means WE gave up on it, not that herdr answered. */
export interface ProbeResult {
  code: number;
  stdout: string;
  stderr: string;
  abandoned: boolean;
}

/**
 * Decide what an elapsed wait means, given a liveness probe.
 *
 * The governing rule is FAIL OPEN. This whole mechanism exists because a
 * worker was killed on an inconclusive signal; killing a healthy worker
 * because an ancillary check hiccuped would be the same bug one layer down.
 * So only a positive statement that the agent is gone -- herdr's own
 * `agent_not_found` -- closes it. Everything else that is not a settle
 * re-arms.
 *
 * But fail open means "do not kill on uncertainty", NOT "never kill": the
 * budget bounds ALL of it, inconclusive outcomes included. A worker wedged
 * badly enough that `agent get` itself hangs or errors every time would
 * otherwise re-arm forever, holding its slot until someone killed the
 * orchestrator by hand -- precisely the stall the budget exists to prevent.
 *
 * `abandoned` has to be a flag rather than something inferred from `code`:
 * `pi.exec` RESOLVES on abort, coercing a killed process's null exit to 0
 * with empty stdout (see the header comment), so an abandoned probe is
 * indistinguishable from "exit 0, no recognizable status" by its result
 * alone -- and that case would close a healthy worker.
 */
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
    // The one code that positively means gone. Anything else -- a daemon
    // restart, a momentary fault, an unparseable envelope -- says nothing
    // about the worker, only about the check.
    if (code === "agent_not_found") return { disposition: "event", kind: "error" };
    return inconclusive();
  }
  const status = parseHerdrJson(probe.stdout)?.result?.agent?.agent_status;
  if (status === "blocked") return { disposition: "event", kind: "blocked" };
  if (status === "idle" || status === "done") return { disposition: "event", kind: "finished" };
  if (status === undefined) return inconclusive();
  // `working`, or a status a future herdr adds. Treated as alive rather than
  // dead on purpose: enumerating statuses herdr MIGHT report as dead would be
  // inventing a list from guesswork, the same mistake as the early fixtures
  // that copied this code's own wrong assumptions and so agreed with the bug.
  return overBudget
    ? { disposition: "event", kind: "timed_out", livenessConfirmed: true }
    : { disposition: "rearm" };
}

/**
 * The worktree a worker's item was being worked in, per the repo convention
 * `<repo>/../<repo-name>-<slug>`. Null when the record predates `cwd` being
 * tracked.
 *
 * Derived, not verified: it assumes `cwd` is the repo root, which is the
 * convention but not a checked fact -- an orchestrator launched from inside a
 * worktree would produce a doubly-suffixed path. The report prints the cwd
 * beside it and says which is which, rather than stripping suffixes or
 * resolving a git common directory, either of which swaps a guess the reader
 * can see for one they cannot.
 */
export function workerWorktreePath(cwd: string | undefined, slug: string): string | null {
  if (!cwd) return null;
  return join(dirname(cwd), `${basename(cwd)}-${slug}`);
}

/**
 * The `detail` for a deliberately-stopped worker: what happened, and
 * everything needed to recover the item and its worktree by hand.
 *
 * Two quite different things reach this, and reporting them identically would
 * repeat this item's own root complaint -- that a wrong outcome label becomes
 * the story the orchestrator tells the human. A confirmed-live worker really
 * was working when its budget ran out. A worker whose probe was abandoned or
 * failed might have crashed hours ago; claiming it "was still working" would
 * be a fabrication, so that case names the probe's own failure instead.
 */
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

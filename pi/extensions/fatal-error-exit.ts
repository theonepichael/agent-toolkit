import type { ExtensionAPI } from "@earendil-works/pi-coding-agent";

// Exit an unattended pi with code 1 when its run settles after a fatal turn
// error.
//
// pi keeps the process alive at the idle prompt after a provider usage-limit
// error (or any other fatal turn error) exhausts its auto-retries, so herdr
// only ever sees `idle` and the swarm orchestrator's interim detector has to
// infer the crash from pane text (providerCrashMatch, with its documented
// blind spots). This makes the crash genuinely terminal instead: the worker's
// process is gone, and the orchestrator's post-mortem gone-paths
// (classifyTimeoutProbe, staleWorkerRecords) read that as a non-finished
// outcome.
//
// Measured live 2026-09-11, and the gap that measurement found is now closed
// orchestrator-side. herdr publishes the terminal `done` status ~0.17 s before
// the agent record disappears, so the armed wait in `settleWait` resolves
// against `done`, and `classifyWaitResult` maps `done` to `finished` exactly
// like the normal parked-at-prompt success signal -- on its own, the exit buys
// nothing on that primary path. The screen that settles a `finished` event
// (`screenFinishedForCrash`) therefore reads this line's `[fatal-error-exit]`
// sentinel out of the pane and reclassifies the finish to `error`, gated on the
// settle NOT being `idle` (an `idle` worker is demonstrably alive, so a sentinel
// in its pane is stale prose -- workers grep this repo and run its tests, which
// print this line). That mapping is left alone on purpose, so the split happens
// on pane evidence rather than in the classifier: `done` is also what a clean
// worker exit looks like. See
// `~/.claude/data/grill/atk-fatal-error-exit-live-smoke-findings.md` Defect 2
// for the measurement, and the residual it leaves: a `done` settle whose pane
// carries no sentinel -- an exit this extension did not make -- is still
// reported `finished`.
//
// Detection is stateless and decision-point-local: `agent_settled` is the
// event pi's own docs name for "pi will not continue automatically" (the
// auto-retry machinery runs BEFORE it), and the verdict is read directly
// from the last assistant message's stopReason at that moment. An
// errored-then-retried-and-recovered run has a later successful assistant
// message as the last one, so nothing stale can misfire.
//
// Entry shape: `getEntries()` returns `SessionEntry` values, and a chat
// message is wrapped -- `{ type: "message", message: { role, stopReason, ... } }`.
// `role` and `stopReason` are NOT on the entry itself. Verified live against
// the installed bundle, and mirrored by pi's own footer, which iterates
// `entry.type === "message" && entry.message.role === "assistant"`. Reading
// `entry.role` returns undefined for every entry and silently disables this
// extension (the original bug this note records).
//
// Attended sessions are never touched: a human can switch models and
// continue. The gate is exactly PI_AGENT_UNATTENDED === "1", mirroring the
// repo's fail-closed convention (guard-rails.ts pins the same strictness).
//
// ctx.shutdown() cannot carry the signal -- both of its paths end in
// process.exit(0) (confirmed in the pi bundle) -- so the exit is made
// directly, after one synchronous stderr line. Session entries are
// persisted as they append, so a hard exit at a settled idle boundary loses
// nothing.

export interface FatalErrorExitDeps {
  exit: (code: number) => never;
  env: Record<string, string | undefined>;
  stderr: { write(text: string): unknown };
}

/** Exactly "1" -- anything else leaves the session attended (fail closed). */
const UNATTENDED = "1";

/**
 * The sentinel the orchestrator's finished-settle screen looks for, spelled
 * literally here rather than imported from `swarm-lib`.
 *
 * A worker has no reason to know the orchestrator's module set exists, and
 * `swarm-lib` is bundled into the Copilot swarm build, so this direction of the
 * dependency is the one that stays clean. The duplication is bound by a test --
 * `pi/test/fatal-error-exit.test.ts` feeds this line through the real matcher --
 * so changing this string without changing `FATAL_ERROR_EXIT_TOKEN` in
 * `swarm-lib/swarm-scheduling.ts` fails there. Keep the leading bracketed
 * token exactly 18 characters wide if it is ever edited: it is short enough that
 * no plausible pty width can wrap mid-token, which is why the screen matches the
 * token and not the whole sentence.
 */
const SENTINEL = "[fatal-error-exit]";

export function registerFatalErrorExit(pi: ExtensionAPI, deps?: Partial<FatalErrorExitDeps>): void {
  const env = deps?.env ?? process.env;
  const exit = deps?.exit ?? ((code: number) => process.exit(code as never));
  const stderr = deps?.stderr ?? process.stderr;

  pi.on("agent_settled", async (_event, ctx) => {
    if (env.PI_AGENT_UNATTENDED !== UNATTENDED) return;
    // Filter-then-last over message entries: change/custom/log entries carry no
    // assistant message, and non-assistant messages may follow the last one.
    let last: { stopReason?: unknown } | undefined;
    for (const entry of ctx.sessionManager.getEntries()) {
      if (entry.type === "message" && entry.message.role === "assistant") {
        last = entry.message as { stopReason?: unknown };
      }
    }
    // Exact-string whitelist, type-checked: a non-string or absent
    // stopReason fails open. "aborted" is a deliberate abort, not a crash;
    // any future stopReason value also fails open until it is understood.
    if (!last || last.stopReason !== "error") return;
    const model = ctx.model ? `${ctx.model.provider}/${ctx.model.id}` : "unknown model";
    stderr.write(
      `${SENTINEL} run settled after a fatal turn error (${model}); ` +
        "exiting 1 for orchestrator visibility\n",
    );
    exit(1);
  });
}

export default function (pi: ExtensionAPI): void {
  registerFatalErrorExit(pi);
}

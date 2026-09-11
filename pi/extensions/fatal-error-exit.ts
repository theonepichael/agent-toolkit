import type { ExtensionAPI } from "@earendil-works/pi-coding-agent";

// Exit an unattended pi with code 1 when its run settles after a fatal turn
// error.
//
// pi keeps the process alive at the idle prompt after a provider usage-limit
// error (or any other fatal turn error) exhausts its auto-retries, so herdr
// only ever sees `idle` and the swarm orchestrator's interim detector has to
// infer the crash from pane text (providerCrashMatch, with its documented
// blind spots). This makes the crash first-class instead: the worker is
// genuinely gone, and every orchestrator path that can observe a vanished
// agent (classifyWaitResult, classifyTimeoutProbe, staleWorkerRecords)
// already classifies that as a non-finished outcome.
//
// Detection is stateless and decision-point-local: `agent_settled` is the
// event pi's own docs name for "pi will not continue automatically" (the
// auto-retry machinery runs BEFORE it), and the verdict is read directly
// from the last assistant entry's stopReason at that moment. An
// errored-then-retried-and-recovered run has a later successful assistant
// message as the last one, so nothing stale can misfire.
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

export function registerFatalErrorExit(pi: ExtensionAPI, deps?: Partial<FatalErrorExitDeps>): void {
  const env = deps?.env ?? process.env;
  const exit = deps?.exit ?? ((code: number) => process.exit(code as never));
  const stderr = deps?.stderr ?? process.stderr;

  pi.on("agent_settled", async (_event, ctx) => {
    if (env.PI_AGENT_UNATTENDED !== UNATTENDED) return;
    const entries = ctx.sessionManager.getEntries() as unknown[];
    // Filter-then-last: non-assistant entries (custom, log) may be
    // interleaved after the final assistant message.
    let last: { stopReason?: unknown } | undefined;
    for (const entry of entries) {
      if ((entry as { role?: unknown })?.role === "assistant") {
        last = entry as { stopReason?: unknown };
      }
    }
    // Exact-string whitelist, type-checked: a non-string or absent
    // stopReason fails open. "aborted" is a deliberate abort, not a crash;
    // any future stopReason value also fails open until it is understood.
    if (!last || last.stopReason !== "error") return;
    const model = ctx.model ? `${ctx.model.provider}/${ctx.model.id}` : "unknown model";
    stderr.write(
      `[fatal-error-exit] run settled after a fatal turn error (${model}); ` +
        `exiting 1 for orchestrator visibility\n`,
    );
    exit(1);
  });
}

export default function (pi: ExtensionAPI): void {
  registerFatalErrorExit(pi);
}

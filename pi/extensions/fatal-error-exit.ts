import { renameSync, writeFileSync } from "node:fs";
import { basename, dirname, join } from "node:path";
import type { ExtensionAPI } from "@earendil-works/pi-coding-agent";

// Exit an unattended pi with code 1 when its run settles after a fatal turn
// error, and leave a death certificate for the orchestrator to read.
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
// Measured live 2026-09-11, and the gap that measurement found was first closed
// orchestrator-side. herdr publishes the terminal `done` status ~0.17 s before
// the agent record disappears, so the armed wait in `settleWait` resolves
// against `done`, and `classifyWaitResult` maps `done` to `finished` exactly
// like the normal parked-at-prompt success signal -- on its own, the exit buys
// nothing on that primary path. The screen that settles a `finished` event
// (`screenFinishedForCrash`) therefore read this line's `[fatal-error-exit]`
// sentinel out of the pane and reclassified the finish to `error`, gated on the
// settle NOT being `idle` (an `idle` worker is demonstrably alive, so a sentinel
// in its pane is stale prose -- workers grep this repo and run its tests, which
// print this line).
// See `~/.claude/data/grill/atk-fatal-error-exit-live-smoke-findings.md` Defect 2
// for the measurement that established the `done`-before-gone race.
//
// THE SENTINEL IS NOW THE FALLBACK, NOT THE PRIMARY SIGNAL. Because the
// `done`-before-gone race above means a fatal exit cannot win the classification
// from outside, the certainty is produced HERE instead: before exiting, this
// handler writes a death certificate next to PI_SWARM_CAPTURE_FILE, and
// `swarm-tool-context.ts` reads it as `evidence: "fatal_sidecar"`, outranking
// any pane inference. That closes three things the sentinel could not: the
// stale-prose ambiguity that forced the `idle` exemption, the bounded-line window
// that a long post-error output could push the banner out of, and the residual
// the previous paragraph described -- a `done` settle whose pane carries no
// sentinel is still reported `finished`, but it is now a settle with no
// certificate behind it either, so it is no longer indistinguishable from one
// the worker itself explained. Deaths with no witness at all (SIGKILL, OOM, a
// crash inside pi) reach neither this handler nor the certificate, which is why
// the pane screens are kept rather than deleted.
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
  /**
   * Atomic write of the death certificate. Injected so the failure path is
   * exercised against this production code rather than a mock of another
   * module: "`exit(1)` still happens when the write throws" is the single most
   * important property of this call site, and it is untestable through a
   * module-level `fs` import.
   */
  sidecar: (captureFile: string, payload: FatalSidecarPayload) => void;
  /** Injectable clock so the payload's timestamp is assertable. */
  now: () => number;
}

/**
 * The certificate's shape, restated here rather than imported from
 * `swarm-lib`, in the same direction as `SENTINEL` below: the worker writes it,
 * the orchestrator parses it, and `pi/test/fatal-error-exit.test.ts` feeds the
 * real emitted payload through the real `parseFatalSidecar` so the two copies
 * cannot separate.
 */
export interface FatalSidecarPayload {
  v: number;
  result: string;
  stopReason: string;
  model: string;
  writtenAtMs: number;
}

/** Exactly "1" -- anything else leaves the session attended (fail closed). */
const UNATTENDED = "1";

/** Must equal `SIDECAR_VERSION` in `swarm-lib/swarm-scheduling.ts`. */
const SIDECAR_VERSION = 1;

/** Must equal `FATAL_SIDECAR_RESULT` in `swarm-lib/swarm-scheduling.ts`. */
const SIDECAR_RESULT = "fatal_error";

/**
 * The certificate's environment handle: the orchestrator already injects
 * `PI_SWARM_CAPTURE_FILE` into every pi worker's tab, so the sidecar path is
 * derived from it and no second variable is needed. Absent means this is not a
 * swarm worker, and nothing is written.
 */
const CAPTURE_ENV = "PI_SWARM_CAPTURE_FILE";

/**
 * Derive the certificate path from the capture file: same directory, basename
 * `-capture-` -> `-outcome-`.
 *
 * This is a deliberate DUPLICATE of `outcomePath()` in
 * `swarm-lib/swarm-herdr.ts`, bound by a test, for the same reason `SENTINEL`
 * is duplicated from `FATAL_ERROR_EXIT_TOKEN`: a worker has no reason to know
 * the orchestrator's module set exists, and `swarm-lib` is bundled into the
 * Copilot swarm build.
 */
export function fatalSidecarPath(captureFile: string): string {
  const dir = dirname(captureFile);
  const base = basename(captureFile);
  const replaced = base.replace("-capture-", "-outcome-");
  if (replaced !== base) return join(dir, replaced);
  return join(dir, `${base}.outcome.json`);
}

/** Write-to-tmp-then-rename, so the orchestrator never observes a half file. */
export function writeFatalSidecarSync(captureFile: string, payload: FatalSidecarPayload): void {
  const path = fatalSidecarPath(captureFile);
  const tmp = `${path}.tmp`;
  writeFileSync(tmp, `${JSON.stringify(payload)}\n`, "utf8");
  renameSync(tmp, path);
}

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
  const sidecar = deps?.sidecar ?? writeFatalSidecarSync;
  const now = deps?.now ?? Date.now;

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
    // stopReason fails open. "aborted" is a deliberate abort, not a crash; any
    // future stopReason value also fails open until it is understood.
    if (!last || last.stopReason !== "error") return;
    const model = ctx.model ? `${ctx.model.provider}/${ctx.model.id}` : "unknown model";
    // The death certificate, written BEFORE the line and before the exit. It
    // states the one fact no outside observer can get: pi settled on
    // `stopReason === "error"`, which is what this branch already knows when it
    // decides to exit. The orchestrator reads it instead of screening pane text
    // (`swarm-tool-context.ts`, `readFatalSidecar`).
    //
    // Synchronous, and inside the same guard as the exit, so it cannot race it
    // and cannot out-live it: `exit(1)` below is reached only after the rename
    // returns, and the write is skipped entirely for a session that is not a
    // swarm worker. Failure is swallowed deliberately -- a certificate that
    // could not be written degrades to today's pane sentinel, never to a wrong
    // verdict, so this must not be able to prevent the exit itself.
    const captureFile = env[CAPTURE_ENV];
    if (captureFile && captureFile.length > 0) {
      try {
        sidecar(captureFile, {
          v: SIDECAR_VERSION,
          result: SIDECAR_RESULT,
          stopReason: "error",
          model,
          writtenAtMs: now(),
        });
      } catch {
        // Best effort: the stderr sentinel below is the fallback witness.
      }
    }
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

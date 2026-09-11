import { existsSync, mkdtempSync, readFileSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import type { ExtensionAPI } from "@earendil-works/pi-coding-agent";
import { describe, expect, test } from "./helpers/tap";

import {
  registerFatalErrorExit,
  writeFatalSidecarSync,
  fatalSidecarPath,
  type FatalErrorExitDeps,
  type FatalSidecarPayload,
} from "../extensions/fatal-error-exit.js";
import {
  FATAL_ERROR_EXIT_TOKEN,
  fatalErrorExitMatch,
  parseFatalSidecar,
} from "../extensions/swarm-lib/swarm-scheduling.js";
import { capturePath } from "../extensions/swarm-lib/swarm-tool-context.js";
import { outcomePath } from "../extensions/swarm-lib/swarm-herdr.js";

/**
 * pi's `SessionManager.getEntries()` returns `SessionEntry` values. A chat
 * message is a WRAPPED entry -- `{ type: "message", message: { role,
 * stopReason, ... } }` -- so `role` and `stopReason` live on `.message`, never
 * on the entry itself (confirmed live against the installed bundle; pi's own
 * footer filters the same way). Every fixture below therefore uses the
 * production shape: a flat `{ role, stopReason }` entry matches nothing, which
 * would make each "never exits" case pass vacuously.
 */
interface FakeEntry {
  type: string;
  message?: { role?: string; stopReason?: unknown; errorMessage?: string };
}

interface FakeCtx {
  sessionManager: { getEntries: () => FakeEntry[] };
  model?: { provider: string; id: string };
}

interface Harness {
  fire: (event: "agent_settled", ctx: FakeCtx) => Promise<void>;
  exitCalls: number[];
  stderrLines: string[];
  /** Every certificate write attempted: [captureFile, payload], in call order. */
  sidecars: [string, FatalSidecarPayload][];
  /**
   * Interleaving of the side-effecting calls, so "the write lands before the
   * exit" is asserted on observed order rather than inferred from reading the
   * source. That ordering IS the design's correctness argument, so it has to be
   * pinned by something that can fail.
   */
  order: string[];
}

function setup(
  env: Record<string, string | undefined>,
  opts: { sidecarThrows?: boolean } = {},
): Harness {
  const exitCalls: number[] = [];
  const stderrLines: string[] = [];
  const sidecars: [string, FatalSidecarPayload][] = [];
  const order: string[] = [];
  const handlers = new Map<string, (event: unknown, ctx: unknown) => Promise<void>>();
  const fakePi = {
    on: (event: string, handler: (event: unknown, ctx: unknown) => Promise<void>) => {
      handlers.set(event, handler);
    },
  } as unknown as ExtensionAPI;
  const deps: Partial<FatalErrorExitDeps> = {
    env,
    now: () => 1789000000000,
    exit: (code: number) => {
      order.push("exit");
      exitCalls.push(code);
      return undefined as never;
    },
    sidecar: (captureFile: string, payload: FatalSidecarPayload) => {
      order.push("sidecar");
      if (opts.sidecarThrows) throw new Error("EROFS: read-only file system");
      sidecars.push([captureFile, payload]);
    },
    stderr: {
      write: (text: string) => {
        stderrLines.push(text);
        return true;
      },
    },
  };
  registerFatalErrorExit(fakePi, deps);
  const fire = async (event: "agent_settled", ctx: FakeCtx) => {
    const handler = handlers.get(event);
    if (!handler) throw new Error(`${event} was never registered`);
    await handler({}, ctx);
  };
  return { fire, exitCalls, stderrLines, sidecars, order };
}

const unattended = { PI_AGENT_UNATTENDED: "1" };
const attended = {};

const assistant = (stopReason?: unknown, errorMessage?: string): FakeEntry => ({
  type: "message",
  message: { role: "assistant", stopReason, errorMessage },
});
const userMessage = (): FakeEntry => ({ type: "message", message: { role: "user" } });
/** A non-message entry (model/thinking-level change, compaction, custom log). */
const bookkeeping = (type = "model_change"): FakeEntry => ({ type });

const ctxWith = (entries: FakeEntry[]): FakeCtx => ({
  sessionManager: { getEntries: () => entries },
  model: { provider: "openai-codex", id: "gpt-5.5" },
});

describe("fatal-error-exit extension", () => {
  test("an unattended run that settles after a fatal turn error exits with code 1", async () => {
    const h = setup(unattended);
    await h.fire("agent_settled", ctxWith([userMessage(), assistant("error", "usage limit")]));
    expect(h.exitCalls).toEqual([1]);
    expect(h.stderrLines[0]).toContain("[fatal-error-exit]");
    expect(h.stderrLines[0]).toContain("openai-codex/gpt-5.5");
  });

  test("an attended session never exits, even after a fatal turn error", async () => {
    const h = setup(attended);
    await h.fire("agent_settled", ctxWith([assistant("error", "usage limit")]));
    expect(h.exitCalls).toEqual([]);
  });

  test("a clean settle never exits, even unattended", async () => {
    const h = setup(unattended);
    await h.fire("agent_settled", ctxWith([assistant("stop")]));
    expect(h.exitCalls).toEqual([]);
  });

  test("an aborted stopReason never exits", async () => {
    const h = setup(unattended);
    await h.fire("agent_settled", ctxWith([assistant("aborted")]));
    expect(h.exitCalls).toEqual([]);
  });

  test("an errored assistant message followed by a successful retry does not exit", async () => {
    const h = setup(unattended);
    await h.fire("agent_settled", ctxWith([assistant("error", "transient"), assistant("stop")]));
    expect(h.exitCalls).toEqual([]);
  });

  test("a session with no assistant entries at all never exits (fail open)", async () => {
    const h = setup(unattended);
    await h.fire("agent_settled", ctxWith([userMessage(), bookkeeping("custom")]));
    expect(h.exitCalls).toEqual([]);
  });

  test("a non-string stopReason fails open, never exits", async () => {
    const h = setup(unattended);
    await h.fire("agent_settled", ctxWith([assistant(undefined)]));
    expect(h.exitCalls).toEqual([]);
  });

  test("an error verdict survives bookkeeping entries interleaved after it", async () => {
    const h = setup(unattended);
    await h.fire(
      "agent_settled",
      ctxWith([
        userMessage(),
        bookkeeping("thinking_level_change"),
        assistant("error", "usage limit"),
        bookkeeping("model_change"),
      ]),
    );
    expect(h.exitCalls).toEqual([1]);
  });
});

/**
 * Drift bound between the writer and the reader, in the direction that keeps
 * the worker-side extension standalone: `fatal-error-exit.ts` does NOT import
 * the orchestrator's constant (a pi worker has no reason to know `swarm-lib`
 * exists, and `swarm-lib` is bundled into the Copilot swarm build, so pulling
 * the extension's concern in there would drag pi-specific code with it). The
 * token is therefore duplicated by design, and this test is what stops the two
 * copies from separating. The emitted line is fed through the REAL matcher as
 * well, so a wording change that keeps the token but breaks the window
 * normalization is caught here too.
 */
describe("fatal-error-exit <-> swarm-scheduling sentinel contract", () => {
  test("the emitted line carries the token the orchestrator screens for", async () => {
    const h = setup(unattended);
    await h.fire("agent_settled", ctxWith([assistant("error", "usage limit")]));
    expect(h.exitCalls).toEqual([1]);
    expect(h.stderrLines[0]).toContain(FATAL_ERROR_EXIT_TOKEN);
    expect(fatalErrorExitMatch(h.stderrLines[0] ?? "")).not.toBeNull();
  });
});

// ---------------------------------------------------------------------------
// The death certificate.
//
// The whole point of writing the certificate from INSIDE this extension,
// rather than from a second `agent_settled` listener, is that nothing else can
// be relied on to run: `process.exit(1)` is synchronous and terminal, and pi
// discovers extensions with an unsorted `readdirSync`, so a separately
// registered writer would be preempted on exactly the path that matters. Every
// case below therefore asserts the two things that make that safe -- the write
// lands before the exit, and the exit happens whether or not the write did.
// ---------------------------------------------------------------------------

describe("fatal-error-exit death certificate", () => {
  const captureEnv = {
    PI_AGENT_UNATTENDED: "1",
    PI_SWARM_CAPTURE_FILE: "/state/swarm-r1-capture-item.json",
  };

  test("a fatal settle writes the certificate and still exits 1", async () => {
    const h = setup(captureEnv);
    await h.fire("agent_settled", ctxWith([assistant("error", "usage limit")]));
    expect(h.exitCalls).toEqual([1]);
    expect(h.sidecars).toHaveLength(1);
    const [path, payload] = h.sidecars[0]!;
    expect(path).toBe("/state/swarm-r1-capture-item.json");
    expect(payload).toEqual({
      v: 1,
      result: "fatal_error",
      stopReason: "error",
      model: "openai-codex/gpt-5.5",
      writtenAtMs: 1789000000000,
    });
  });

  test("the write order is certificate-then-exit, never the reverse", async () => {
    const h = setup(captureEnv);
    await h.fire("agent_settled", ctxWith([assistant("error", "usage limit")]));
    expect(h.order).toEqual(["sidecar", "exit"]);
  });

  test("a throwing write does NOT swallow the exit", async () => {
    // The single most important property here: the certificate is an upgrade to
    // the pane sentinel, not a replacement for the exit. If it could fail the
    // exit, this change would make unattended crashes LESS visible.
    const h = setup(captureEnv, { sidecarThrows: true });
    await h.fire("agent_settled", ctxWith([assistant("error", "usage limit")]));
    expect(h.exitCalls).toEqual([1]);
    expect(h.stderrLines[0]).toContain(FATAL_ERROR_EXIT_TOKEN);
  });

  test("exit(1) without a capture env writes nothing (unattended, but not a worker)", async () => {
    // The biconditional this design is NOT allowed to claim: an unattended
    // non-swarm `pi -p` exits 1 and has no certificate to write, and that is a
    // correct outcome rather than a lost one.
    const h = setup({ PI_AGENT_UNATTENDED: "1" });
    await h.fire("agent_settled", ctxWith([assistant("error", "boom")]));
    expect(h.exitCalls).toEqual([1]);
    expect(h.sidecars).toHaveLength(0);
  });

  test("an empty capture env counts as absent", async () => {
    const h = setup({ PI_AGENT_UNATTENDED: "1", PI_SWARM_CAPTURE_FILE: "" });
    await h.fire("agent_settled", ctxWith([assistant("error", "boom")]));
    expect(h.exitCalls).toEqual([1]);
    expect(h.sidecars).toHaveLength(0);
  });

  test("a clean settle writes no certificate -- absence is not a verdict", async () => {
    // No `clean` certificate exists anywhere in this design. `agent_settled`
    // ends an agent RUN, not a worker's life, and herdr's `idle` already proves
    // liveness from outside; a written `clean` could only be redundant or, once
    // the worker was resumed after a gate, actively stale.
    for (const stopReason of ["stop", "length", "aborted", undefined]) {
      const h = setup(captureEnv);
      await h.fire("agent_settled", ctxWith([assistant(stopReason)]));
      expect(h.exitCalls).toEqual([]);
      expect(h.sidecars).toHaveLength(0);
    }
  });

  test("an attended session writes nothing even with a capture env", async () => {
    const h = setup({ PI_SWARM_CAPTURE_FILE: "/state/swarm-r1-capture-item.json" });
    await h.fire("agent_settled", ctxWith([assistant("error", "boom")]));
    expect(h.sidecars).toHaveLength(0);
    expect(h.exitCalls).toEqual([]);
  });

  test("PI_AGENT_UNATTENDED=0 is not '1' and stays silent", async () => {
    const h = setup({
      PI_AGENT_UNATTENDED: "0",
      PI_SWARM_CAPTURE_FILE: "/state/swarm-r1-capture-item.json",
    });
    await h.fire("agent_settled", ctxWith([assistant("error", "boom")]));
    expect(h.sidecars).toHaveLength(0);
  });

  /**
   * The drift bound for the PATH, in the same spirit as the sentinel binding
   * above: the worker derives the path from `PI_SWARM_CAPTURE_FILE`, the
   * orchestrator derives it from `capturePath(runId, slug)`, and the two have no
   * shared code by design. If they separate, the orchestrator silently reads a
   * path that is never written, and every fatal death quietly reverts to being
   * guessed from pane text -- a failure with no error anywhere.
   */
  test("the written path resolves to the orchestrator's outcomePath(capturePath(...))", async () => {
    const h = setup({
      PI_AGENT_UNATTENDED: "1",
      PI_SWARM_CAPTURE_FILE: capturePath("r1", "item", "/state"),
    });
    await h.fire("agent_settled", ctxWith([assistant("error", "usage limit")]));
    expect(h.sidecars[0]?.[0]).toBe(capturePath("r1", "item", "/state"));
    expect(fatalSidecarPath(capturePath("r1", "item", "/state"))).toBe(
      outcomePath(capturePath("r1", "item", "/state")),
    );
    expect(fatalSidecarPath("/state/swarm-r1-capture-item.json")).toBe(
      "/state/swarm-r1-outcome-item.json",
    );
    // The unmatched-basename fallback, so a renamed capture file still lands
    // somewhere the orchestrator can also find.
    expect(fatalSidecarPath("/state/other.json")).toBe("/state/other.json.outcome.json");
  });

  /**
   * The drift bound for the PAYLOAD, and the stronger half of the guarantee: the
   * object this extension emits is fed through the REAL reader, so a field
   * renamed or retyped here fails on the reader's strict contract rather than
   * silently degrading a death certificate into an absent file.
   */
  test("the emitted payload parses under the orchestrator's own strict reader", async () => {
    const h = setup(captureEnv);
    await h.fire("agent_settled", ctxWith([assistant("error", "usage limit")]));
    const parsed = parseFatalSidecar(JSON.stringify(h.sidecars[0]![1]));
    expect(parsed).not.toBeNull();
    expect(parsed?.result).toBe("fatal_error");
    expect(parsed?.stopReason).toBe("error");
  });

  test("the real atomic writer writes tmp+rename and leaves only the final file", () => {
    const dir = mkdtempSync(join(tmpdir(), "fatal-sidecar-"));
    try {
      const capture = join(dir, "swarm-r1-capture-item.json");
      writeFileSync(capture, "{}");
      const payload = {
        v: 1,
        result: "fatal_error",
        stopReason: "error",
        model: "p/m",
        writtenAtMs: 1,
      };
      writeFatalSidecarSync(capture, payload);
      expect(existsSync(join(dir, "swarm-r1-outcome-item.json"))).toBe(true);
      // No half-written `.tmp` survives a successful rename, and the payload on
      // disk is exactly what the strict reader expects.
      expect(existsSync(join(dir, "swarm-r1-outcome-item.json.tmp"))).toBe(false);
      expect(
        parseFatalSidecar(readFileSync(join(dir, "swarm-r1-outcome-item.json"), "utf8")),
      ).toEqual(payload);
    } finally {
      rmSync(dir, { recursive: true, force: true });
    }
  });
});

import type { ExtensionAPI } from "@earendil-works/pi-coding-agent";
import { describe, expect, test } from "./helpers/tap";

import { registerFatalErrorExit, type FatalErrorExitDeps } from "../extensions/fatal-error-exit.js";

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
}

function setup(env: Record<string, string | undefined>): Harness {
  const exitCalls: number[] = [];
  const stderrLines: string[] = [];
  const handlers = new Map<string, (event: unknown, ctx: unknown) => Promise<void>>();
  const fakePi = {
    on: (event: string, handler: (event: unknown, ctx: unknown) => Promise<void>) => {
      handlers.set(event, handler);
    },
  } as unknown as ExtensionAPI;
  const deps: Partial<FatalErrorExitDeps> = {
    env,
    exit: (code: number) => {
      exitCalls.push(code);
      return undefined as never;
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
  return { fire, exitCalls, stderrLines };
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

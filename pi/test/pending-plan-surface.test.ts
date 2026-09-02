import { describe, expect, test } from "bun:test";
import type {
  BeforeAgentStartEvent,
  ExtensionAPI,
  SessionStartEvent,
} from "@earendil-works/pi-coding-agent";
import {
  bannerText,
  default as createSurface,
  parsePendingOutput,
  resumeHint,
  type PendingPlan,
} from "../extensions/pending-plan-surface";

const PENDING_BLOCK = [
  "\u{1F4CB} Grill plan ready to execute: 2026-08-31-multi-harness-session-analysis-s",
  "   Plan: /home/yanil/.claude/data/grill/2026-08-31-multi-harness-session-analysis-s-plan.md",
  "   Resume via: /backlog-item meta-analyze-sessions",
  "   (If the user says go/continue, run that command — it resumes",
  "    through the backlog item's own state and gates instead of",
  "    implementing the plan file directly. If skip/no, take no",
  "    further action — this flag is already cleared.)",
].join("\n");

const PENDING_BLOCK_NO_BACKLOG = [
  "\u{1F4CB} Grill plan ready to execute: 2026-08-31-some-topic",
  "   Plan: /home/yanil/.claude/data/grill/2026-08-31-some-topic-plan.md",
  "   (If the user says go/continue, read the plan file and start",
  "    implementing it directly. If skip/no, take no further action —",
  "    this flag is already cleared.)",
].join("\n");

function parseLines(lines: string): PendingPlan | null {
  return parsePendingOutput(lines);
}

describe("parsePendingOutput", () => {
  test("parses slug, plan path, and backlog slug from a full block", () => {
    const parsed = parseLines(PENDING_BLOCK);
    expect(parsed).not.toBeNull();
    expect(parsed?.sessionSlug).toBe("2026-08-31-multi-harness-session-analysis-s");
    expect(parsed?.planPath).toBe(
      "/home/yanil/.claude/data/grill/2026-08-31-multi-harness-session-analysis-s-plan.md",
    );
    expect(parsed?.backlogSlug).toBe("meta-analyze-sessions");
  });

  test("parses a block with no backlog line", () => {
    const parsed = parseLines(PENDING_BLOCK_NO_BACKLOG);
    expect(parsed?.sessionSlug).toBe("2026-08-31-some-topic");
    expect(parsed?.planPath).toBe("/home/yanil/.claude/data/grill/2026-08-31-some-topic-plan.md");
    expect(parsed?.backlogSlug).toBeUndefined();
  });

  test("empty stdout means nothing pending", () => {
    expect(parseLines("")).toBeNull();
  });

  test("garbage output means nothing pending", () => {
    expect(parseLines("some unrelated extension output\nmore noise")).toBeNull();
  });

  test("a marker line with no slug is treated as nothing pending", () => {
    expect(parseLines("\u{1F4CB} Grill plan ready to execute:   ")).toBeNull();
  });
});

describe("bannerText", () => {
  test("names the session slug and the resume phrase", () => {
    const text = bannerText({
      sessionSlug: "2026-08-31-some-topic",
      planPath: "/p.md",
    });
    expect(text).toContain("2026-08-31-some-topic");
    expect(text).toContain("resume the plan");
  });

  test("truncates very long slugs to one line", () => {
    const long = `a`.repeat(200);
    const text = bannerText({ sessionSlug: long, planPath: "/p.md" });
    expect(text.length).toBeLessThan(200);
    expect(text.split("\n").length).toBe(1);
  });
});

describe("resumeHint", () => {
  const plan: PendingPlan = {
    sessionSlug: "2026-08-31-some-topic",
    planPath: "/p.md",
    backlogSlug: "meta-something",
  };

  test("tells the model the plan exists and how to consume it", () => {
    const hint = resumeHint(plan);
    expect(hint).toContain("2026-08-31-some-topic");
    expect(hint).toContain("pending_plan");
    expect(hint).toContain("consume");
  });

  test("forbids consuming for unrelated requests", () => {
    expect(resumeHint(plan)).toMatch(/only when the user asks to resume/i);
  });

  test("points at the backlog item flow when one is linked", () => {
    expect(resumeHint(plan)).toContain("meta-something");
  });

  test("works without a linked backlog item", () => {
    const hint = resumeHint({ sessionSlug: "s", planPath: "/p.md" });
    expect(hint).toContain("/p.md");
  });
});

interface CapturedNotification {
  text: string;
  level: string;
}

type SessionStartHandler = (
  event: SessionStartEvent,
  ctx: { mode: string; ui: { notify: (text: string, level: string) => void } },
) => Promise<void>;

type BeforeAgentStartHandler = (
  event: BeforeAgentStartEvent,
) => Promise<{ message?: { customType: string; content: string; display: boolean } } | undefined>;

function makeHarness(execStdout: string, execThrows = false) {
  const handlers = new Map<string, unknown[]>();
  const execArgv: string[][] = [];
  const notifications: CapturedNotification[] = [];
  const execResults: { stdout: string }[] = [];

  const stub = {
    on(event: string, handler: unknown) {
      const list = handlers.get(event) ?? [];
      list.push(handler);
      handlers.set(event, list);
    },
    async exec(_cmd: string, args: string[]) {
      execArgv.push(args);
      if (execThrows) {
        throw new Error("grill.py missing");
      }
      const result = { stdout: execStdout, stderr: "", code: 0 };
      execResults.push(result);
      return result;
    },
  } as unknown as ExtensionAPI;

  createSurface(stub);

  const sessionHandlers = (handlers.get("session_start") ?? []) as SessionStartHandler[];
  const promptHandlers = (handlers.get("before_agent_start") ?? []) as BeforeAgentStartHandler[];

  return {
    execArgv,
    notifications,
    sessionHandlers,
    promptHandlers,
    fireSessionStart(mode: string) {
      const event: SessionStartEvent = { type: "session_start", reason: "startup" };
      return Promise.all(
        sessionHandlers.map((h) =>
          h(event, { mode, ui: { notify: (text, level) => notifications.push({ text, level }) } }),
        ),
      );
    },
    firePrompt() {
      const event: BeforeAgentStartEvent = {
        type: "before_agent_start",
        prompt: "hello",
        systemPrompt: "",
        systemPromptOptions: {} as BeforeAgentStartEvent["systemPromptOptions"],
      };
      return Promise.all(promptHandlers.map((h) => h(event)));
    },
  };
}

describe("extension wiring", () => {
  test("probes pending-plan without --consume", async () => {
    const harness = makeHarness("");
    await harness.fireSessionStart("tui");
    expect(harness.execArgv).toHaveLength(1);
    expect(harness.execArgv[0]).toEqual(expect.arrayContaining(["pending-plan"]));
    expect(harness.execArgv[0]).not.toContain("--consume");
  });

  test("banners in TUI when a plan is pending", async () => {
    const harness = makeHarness(PENDING_BLOCK);
    await harness.fireSessionStart("tui");
    expect(harness.notifications).toHaveLength(1);
    expect(harness.notifications[0].text).toContain("2026-08-31-multi-harness-session-analysis-s");
  });

  test("no banner outside TUI", async () => {
    const harness = makeHarness(PENDING_BLOCK);
    await harness.fireSessionStart("print");
    expect(harness.notifications).toHaveLength(0);
  });

  test("injects the hint on the first prompt only", async () => {
    const harness = makeHarness(PENDING_BLOCK);
    await harness.fireSessionStart("print");
    const first = await harness.firePrompt();
    expect(first[0]?.message?.customType).toBe("pending-plan-surface");
    expect(first[0]?.message?.display).toBe(true);
    expect(first[0]?.message?.content).toContain("2026-08-31-multi-harness-session-analysis-s");
    const second = await harness.firePrompt();
    expect(second[0]).toBeUndefined();
  });

  test("no injection when nothing is pending", async () => {
    const harness = makeHarness("");
    await harness.fireSessionStart("print");
    const results = await harness.firePrompt();
    expect(results[0]).toBeUndefined();
  });

  test("re-probes on a later session_start: consumed plan goes quiet", async () => {
    // First start sees a pending plan; a later start sees it consumed.
    let probeCount = 0;
    const handlers = new Map<string, unknown[]>();
    const notifications: CapturedNotification[] = [];
    const stub = {
      on(event: string, handler: unknown) {
        const list = handlers.get(event) ?? [];
        list.push(handler);
        handlers.set(event, list);
      },
      async exec(_cmd: string, _args: string[]) {
        probeCount++;
        // Simulate: after the first probe, the plan is consumed externally.
        return { stdout: probeCount <= 1 ? PENDING_BLOCK : "", stderr: "", code: 0 };
      },
    } as unknown as ExtensionAPI;
    createSurface(stub);

    const sessionHandlers = (handlers.get("session_start") ?? []) as SessionStartHandler[];
    const promptHandlers = (handlers.get("before_agent_start") ?? []) as BeforeAgentStartHandler[];

    await Promise.all(
      sessionHandlers.map((h) =>
        h(
          { type: "session_start", reason: "startup" },
          { mode: "tui", ui: { notify: (t, l) => notifications.push({ text: t, level: l }) } },
        ),
      ),
    );
    expect(notifications).toHaveLength(1);

    // Fork to a new session: re-probe sees the store empty now.
    await Promise.all(
      sessionHandlers.map((h) =>
        h(
          { type: "session_start", reason: "fork" },
          { mode: "tui", ui: { notify: (t, l) => notifications.push({ text: t, level: l }) } },
        ),
      ),
    );
    expect(notifications).toHaveLength(1); // no second banner

    const results = await Promise.all(
      promptHandlers.map((h) =>
        h({
          type: "before_agent_start",
          prompt: "hello",
          systemPrompt: "",
          systemPromptOptions: {} as BeforeAgentStartEvent["systemPromptOptions"],
        }),
      ),
    );
    expect(results[0]).toBeUndefined();
  });

  test("exec failure degrades to silent no-op", async () => {
    const harness = makeHarness("", true);
    await harness.fireSessionStart("tui");
    expect(harness.notifications).toHaveLength(0);
    const results = await harness.firePrompt();
    expect(results[0]).toBeUndefined();
  });
});

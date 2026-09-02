import { describe, expect, test } from "bun:test";
import { assertFields, buildArgv, type GrillParams } from "../extensions/grill-tool";

describe("assertFields", () => {
  test("new requires a payload carrying a topic", () => {
    expect(() => assertFields("new", { action: "new" })).toThrow(/requires: payload/);
    expect(() => assertFields("new", { action: "new", payload: {} })).toThrow(
      /action "new" requires payload.topic/,
    );
    expect(() => assertFields("new", { action: "new", payload: { topic: "Auth" } })).not.toThrow();
  });

  test("new takes no session — it is the call that creates one", () => {
    expect(() =>
      assertFields("new", { action: "new", payload: { topic: "Auth" }, session: "auth" }),
    ).toThrow(/does not accept: session/);
  });

  test("ask and decide require an id inside the payload", () => {
    for (const action of ["ask", "decide"] as const) {
      expect(() => assertFields(action, { action, payload: { question: "?" } })).toThrow(
        new RegExp(`action "${action}" requires payload.id`),
      );
    }
  });

  test("revise, rm, verdict and show take a decisionId", () => {
    expect(() => assertFields("revise", { action: "revise", payload: { decision: "x" } })).toThrow(
      /requires: decisionId/,
    );
    expect(() => assertFields("rm", { action: "rm" })).toThrow(/requires: decisionId/);
    expect(() => assertFields("verdict", { action: "verdict", decisionId: "d" })).toThrow(
      /requires: payload/,
    );
  });

  test("show's decisionId is optional — bare show prints the session", () => {
    expect(() => assertFields("show", { action: "show" })).not.toThrow();
    expect(() => assertFields("show", { action: "show", decisionId: "d" })).not.toThrow();
  });

  test("verdict payload must carry a known result", () => {
    expect(() =>
      assertFields("verdict", {
        action: "verdict",
        decisionId: "d",
        payload: { result: "MAYBE", evidence: "e" },
      }),
    ).toThrow(/result must be one of VERIFIED, DISPUTED, UNVERIFIABLE/);
    for (const result of ["VERIFIED", "DISPUTED", "UNVERIFIABLE"]) {
      expect(() =>
        assertFields("verdict", {
          action: "verdict",
          decisionId: "d",
          payload: { result, evidence: "e" },
        }),
      ).not.toThrow();
    }
  });

  test("VERIFIED and DISPUTED require evidence, UNVERIFIABLE does not", () => {
    // Mirrors grill.py's EVIDENCE_REQUIRED set: a verdict claiming the
    // decision was checked has to say what checked it.
    for (const result of ["VERIFIED", "DISPUTED"]) {
      expect(() =>
        assertFields("verdict", { action: "verdict", decisionId: "d", payload: { result } }),
      ).toThrow(/requires evidence/);
    }
    expect(() =>
      assertFields("verdict", {
        action: "verdict",
        decisionId: "d",
        payload: { result: "UNVERIFIABLE" },
      }),
    ).not.toThrow();
  });

  test("plan requires a path", () => {
    expect(() => assertFields("plan", { action: "plan" })).toThrow(/requires: path/);
    expect(() => assertFields("plan", { action: "plan", path: "  " })).toThrow(
      /path must not be empty/,
    );
  });

  test("list takes nothing at all", () => {
    expect(() => assertFields("list", { action: "list" })).not.toThrow();
    expect(() => assertFields("list", { action: "list", session: "auth" })).toThrow(
      /does not accept: session/,
    );
  });

  test("pending_plan takes only consume, never a session", () => {
    expect(() => assertFields("pending_plan", { action: "pending_plan" })).not.toThrow();
    expect(() =>
      assertFields("pending_plan", { action: "pending_plan", consume: true }),
    ).not.toThrow();
    expect(() => assertFields("pending_plan", { action: "pending_plan", session: "auth" })).toThrow(
      /does not accept: session/,
    );
  });

  test("force is only meaningful on rm", () => {
    expect(() => assertFields("rm", { action: "rm", decisionId: "d", force: true })).not.toThrow();
    expect(() => assertFields("render", { action: "render", force: true })).toThrow(
      /does not accept: force/,
    );
  });

  test("an undefined field is not treated as supplied", () => {
    const params: GrillParams = { action: "list", session: undefined };
    expect(() => assertFields("list", params)).not.toThrow();
  });
});

describe("buildArgv", () => {
  test("new serializes the payload as one JSON argument", () => {
    expect(buildArgv("new", { action: "new", payload: { topic: "Auth token design" } })).toEqual([
      "new",
      '{"topic":"Auth token design"}',
    ]);
  });

  test("session is passed as --session when present", () => {
    expect(buildArgv("render", { action: "render", session: "auth-token" })).toEqual([
      "render",
      "--session",
      "auth-token",
    ]);
    expect(buildArgv("render", { action: "render" })).toEqual(["render"]);
  });

  test("decision-id actions put the id before the payload", () => {
    expect(
      buildArgv("revise", {
        action: "revise",
        decisionId: "token-storage",
        payload: { decision: "httpOnly cookie" },
      }),
    ).toEqual(["revise", "token-storage", '{"decision":"httpOnly cookie"}']);
  });

  test("rm passes --force only when asked", () => {
    expect(buildArgv("rm", { action: "rm", decisionId: "d" })).toEqual(["rm", "d"]);
    expect(buildArgv("rm", { action: "rm", decisionId: "d", force: true })).toEqual([
      "rm",
      "d",
      "--force",
    ]);
  });

  test("bare show omits the optional decision id", () => {
    expect(buildArgv("show", { action: "show" })).toEqual(["show"]);
    expect(buildArgv("show", { action: "show", decisionId: "d" })).toEqual(["show", "d"]);
  });

  test("hyphenated subcommands keep their CLI spelling", () => {
    // The action names are snake_case for the schema; the script's
    // subcommands are hyphenated.
    expect(buildArgv("mark_pending_execution", { action: "mark_pending_execution" })).toEqual([
      "mark-pending-execution",
    ]);
    expect(buildArgv("pending_plan", { action: "pending_plan", consume: true })).toEqual([
      "pending-plan",
      "--consume",
    ]);
  });

  test("mark_pending_execution passes the backlog slug", () => {
    expect(
      buildArgv("mark_pending_execution", {
        action: "mark_pending_execution",
        backlogSlug: "pi-tool-grill",
      }),
    ).toEqual(["mark-pending-execution", "--backlog-slug", "pi-tool-grill"]);
  });

  test("plan passes the artifact path", () => {
    const path = "/home/yanil/.claude/data/grill/it's-a-topic-plan.md";
    expect(buildArgv("plan", { action: "plan", path })).toEqual(["plan", path]);
  });

  test("every action builds a non-empty argv", () => {
    // Guards against a missing switch arm silently returning undefined.
    const minimal: Record<string, GrillParams> = {
      new: { action: "new", payload: { topic: "t" } },
      ask: { action: "ask", payload: { id: "d", question: "?" } },
      decide: { action: "decide", payload: { id: "d", decision: "x" } },
      revise: { action: "revise", decisionId: "d", payload: { decision: "x" } },
      rm: { action: "rm", decisionId: "d" },
      verdict: {
        action: "verdict",
        decisionId: "d",
        payload: { result: "UNVERIFIABLE" },
      },
      plan: { action: "plan", path: "/tmp/p.md" },
      mark_pending_execution: { action: "mark_pending_execution" },
      pending_plan: { action: "pending_plan" },
      next: { action: "next" },
      frontier: { action: "frontier" },
      render: { action: "render" },
      list: { action: "list" },
      show: { action: "show" },
    };
    for (const [action, params] of Object.entries(minimal)) {
      const argv = buildArgv(params.action, params);
      expect(argv.length, `action ${action}`).toBeGreaterThan(0);
      expect(argv[0], `action ${action}`).not.toContain("_");
    }
  });
});

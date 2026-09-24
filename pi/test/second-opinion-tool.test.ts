import { describe, expect, test } from "./helpers/tap";
import secondOpinionExtension, {
  assertFields,
  buildArgv,
  buildEnvPrefix,
} from "../extensions/second-opinion-tool";

describe("assertFields", () => {
  test("detect takes nothing", () => {
    expect(() => assertFields("detect", { action: "detect" })).not.toThrow();
  });

  test("detect refuses review's fields", () => {
    expect(() => assertFields("detect", { action: "detect", planFile: "/tmp/p.md" })).toThrow(
      /does not accept: planFile/,
    );
    expect(() => assertFields("detect", { action: "detect", modelIndex: 0 })).toThrow(
      /does not accept: modelIndex/,
    );
  });

  test("review requires planFile", () => {
    expect(() => assertFields("review", { action: "review" })).toThrow(/requires: planFile/);
  });

  test("review accepts its optional fields", () => {
    expect(() =>
      assertFields("review", {
        action: "review",
        planFile: "/tmp/p.md",
        backend: "agy",
        focusFile: "/tmp/p-focus.md",
        modelIndex: 2,
      }),
    ).not.toThrow();
  });

  test("an empty planFile is not a path", () => {
    expect(() => assertFields("review", { action: "review", planFile: "  " })).toThrow(
      /planFile must not be empty/,
    );
  });

  test("modelIndex must be a non-negative integer", () => {
    // The script treats it as a 0-based pool index and hard-errors on an
    // out-of-range value, so a negative or fractional one is never valid.
    for (const bad of [-1, 1.5]) {
      expect(() =>
        assertFields("review", { action: "review", planFile: "/tmp/p.md", modelIndex: bad }),
      ).toThrow(/modelIndex must be a non-negative integer/);
    }
  });

  test("modelIndex 0 is valid and not treated as absent", () => {
    // Round 1 of the rotation is index 0; a falsy-check would drop it.
    expect(() =>
      assertFields("review", { action: "review", planFile: "/tmp/p.md", modelIndex: 0 }),
    ).not.toThrow();
  });

  test("model requires exactly one known backend", () => {
    // The script's override is per-backend (no global model var), so a model
    // with no backend would silently do nothing — reject it up front. A
    // comma-list of backends can't target one model either.
    expect(() =>
      assertFields("review", { action: "review", planFile: "/tmp/p.md", model: "gpt-5" }),
    ).toThrow(/model requires/);
    expect(() =>
      assertFields("review", {
        action: "review",
        planFile: "/tmp/p.md",
        backend: "codex,agy",
        model: "gpt-5",
      }),
    ).toThrow(/single backend/);
    expect(() =>
      assertFields("review", {
        action: "review",
        planFile: "/tmp/p.md",
        backend: "agy",
        model: "Gemini 3.7 Pro (High)",
      }),
    ).not.toThrow();
  });

  test("backend must name only known backends", () => {
    // Arbitrary names would otherwise be turned into arbitrary
    // SECOND_OPINION_<NAME>_* env vars the script silently ignores.
    expect(() =>
      assertFields("review", { action: "review", planFile: "/tmp/p.md", backend: "bogus" }),
    ).toThrow(/known backends/);
    expect(() =>
      assertFields("review", { action: "review", planFile: "/tmp/p.md", backend: "codex,bogus" }),
    ).toThrow(/unknown/);
    // A comma list of known backends is valid (script tries them in order).
    expect(() =>
      assertFields("review", { action: "review", planFile: "/tmp/p.md", backend: "codex,agy" }),
    ).not.toThrow();
  });

  test("model and modelIndex are mutually exclusive", () => {
    // An explicit --model-index selects the model pool over
    // SECOND_OPINION_<BACKEND>_MODEL, silently replacing the pinned model.
    expect(() =>
      assertFields("review", {
        action: "review",
        planFile: "/tmp/p.md",
        backend: "agy",
        model: "Gemini 3.7 Pro (High)",
        modelIndex: 0,
      }),
    ).toThrow(/mutually exclusive/);
  });

  test("model must not be empty", () => {
    expect(() =>
      assertFields("review", {
        action: "review",
        planFile: "/tmp/p.md",
        backend: "agy",
        model: "  ",
      }),
    ).toThrow(/model must not be empty/);
  });

  test("timeoutSeconds is accepted on review", () => {
    expect(() =>
      assertFields("review", { action: "review", planFile: "/tmp/p.md", timeoutSeconds: 300 }),
    ).not.toThrow();
  });
});

describe("buildArgv", () => {
  test("detect", () => {
    expect(buildArgv("detect", { action: "detect" })).toEqual(["detect"]);
  });

  test("review with only a plan file", () => {
    expect(buildArgv("review", { action: "review", planFile: "/tmp/p.md" })).toEqual([
      "review",
      "/tmp/p.md",
    ]);
  });

  test("review passes every optional flag", () => {
    expect(
      buildArgv("review", {
        action: "review",
        planFile: "/tmp/p.md",
        backend: "opencode",
        dir: "/workspace/project",
        textOnly: true,
        focusFile: "/tmp/p-focus.md",
        modelIndex: 2,
      }),
    ).toEqual([
      "review",
      "/tmp/p.md",
      "--backend",
      "opencode",
      "--dir",
      "/workspace/project",
      "--text-only",
      "--focus-file",
      "/tmp/p-focus.md",
      "--model-index",
      "2",
    ]);
  });

  test("modelIndex 0 is still passed", () => {
    // Round 1 is index 0. A truthiness test here would silently skip the
    // flag and fall back to the single-model override instead of the pool.
    expect(buildArgv("review", { action: "review", planFile: "/tmp/p.md", modelIndex: 0 })).toEqual(
      ["review", "/tmp/p.md", "--model-index", "0"],
    );
  });

  test("the plan path is one argv element, never shell-split", () => {
    const path = "/home/yanil/.claude/data/grill/it's-a-topic-plan.md";
    expect(buildArgv("review", { action: "review", planFile: path })).toEqual(["review", path]);
  });
});

describe("buildEnvPrefix", () => {
  test("no model/timeout -> empty", () => {
    expect(buildEnvPrefix({ action: "review", planFile: "/tmp/p.md" })).toEqual([]);
  });

  test("model needs backend -> backend-specific var", () => {
    // The backend name is upper-cased into the env var.
    expect(
      buildEnvPrefix({
        action: "review",
        planFile: "/tmp/p.md",
        backend: "agy",
        model: "Gemini 3.7 Pro (High)",
      }),
    ).toEqual(["SECOND_OPINION_AGY_MODEL=Gemini 3.7 Pro (High)"]);
  });

  test("timeout alone -> global var, clamped to ceiling", () => {
    expect(
      buildEnvPrefix({ action: "review", planFile: "/tmp/p.md", timeoutSeconds: 900 }),
    ).toEqual(["SECOND_OPINION_TIMEOUT_SECONDS=600"]);
  });

  test("timeout with backend -> per-backend var", () => {
    expect(
      buildEnvPrefix({
        action: "review",
        planFile: "/tmp/p.md",
        backend: "pi",
        timeoutSeconds: 120,
      }),
    ).toEqual(["SECOND_OPINION_PI_TIMEOUT_SECONDS=120"]);
  });

  test("timeout floored at 1 (non-positive isn't silently dropped)", () => {
    expect(buildEnvPrefix({ action: "review", planFile: "/tmp/p.md", timeoutSeconds: 0 })).toEqual([
      "SECOND_OPINION_TIMEOUT_SECONDS=1",
    ]);
  });

  test("model and timeout combine", () => {
    expect(
      buildEnvPrefix({
        action: "review",
        planFile: "/tmp/p.md",
        backend: "opencode",
        model: "opencode-go/glm-5.2",
        timeoutSeconds: 450,
      }),
    ).toEqual([
      "SECOND_OPINION_OPENCODE_MODEL=opencode-go/glm-5.2",
      "SECOND_OPINION_OPENCODE_TIMEOUT_SECONDS=450",
    ]);
  });

  test("timeout with a backend list -> per-backend timeout vars", () => {
    // The script tries a comma list in order, so each listed backend gets its
    // own per-backend timeout override.
    expect(
      buildEnvPrefix({
        action: "review",
        planFile: "/tmp/p.md",
        backend: "codex,agy",
        timeoutSeconds: 120,
      }),
    ).toEqual([
      "SECOND_OPINION_CODEX_TIMEOUT_SECONDS=120",
      "SECOND_OPINION_AGY_TIMEOUT_SECONDS=120",
    ]);
  });

  test("model with a backend list -> no malformed model var", () => {
    // assertFields blocks this combo, but buildEnvPrefix must stay safe: it
    // must never emit SECOND_OPINION_CODEX,AGY_MODEL (a garbage variable).
    expect(
      buildEnvPrefix({
        action: "review",
        planFile: "/tmp/p.md",
        backend: "codex,agy",
        model: "gpt-5",
      }),
    ).toEqual([]);
  });
});

describe("secondOpinionExtension execute", () => {
  test("passes restored cwd from branch to pi.exec", async () => {
    let capturedOptions: any;
    let toolDef: any;
    const mockPi = {
      registerTool: (_def: any) => {
        toolDef = _def;
      },
      exec: async (_cmd: string, _argv: string[], options: any) => {
        capturedOptions = options;
        return { code: 0, stdout: "ok", stderr: "" };
      },
    } as any;
    secondOpinionExtension(mockPi);

    const mockCtx = {
      cwd: "/launch/dir",
      sessionManager: {
        getBranch: () => [{ type: "custom", customType: "cwd-change", data: { cwd: "/tmp" } }],
      },
    } as any;

    await toolDef.execute("call-1", { action: "detect" }, undefined, undefined, mockCtx);
    expect(capturedOptions.cwd).toBe("/tmp");
  });

  test("falls back to ctx.cwd when branch has no cwd-change entries", async () => {
    let capturedOptions: any;
    let toolDef: any;
    const mockPi = {
      registerTool: (_def: any) => {
        toolDef = _def;
      },
      exec: async (_cmd: string, _argv: string[], options: any) => {
        capturedOptions = options;
        return { code: 0, stdout: "ok", stderr: "" };
      },
    } as any;
    secondOpinionExtension(mockPi);

    const mockCtx = {
      cwd: "/launch/dir",
      sessionManager: {
        getBranch: () => [],
      },
    } as any;

    await toolDef.execute("call-2", { action: "detect" }, undefined, undefined, mockCtx);
    expect(capturedOptions.cwd).toBe("/launch/dir");
  });

  test("model+timeout route through the `env` coreutil as a prefix", async () => {
    let capturedCmd: string | undefined;
    let capturedArgs: string[] | undefined;
    let toolDef: any;
    const mockPi = {
      registerTool: (_def: any) => {
        toolDef = _def;
      },
      exec: async (cmd: string, argv: string[], _options: any) => {
        capturedCmd = cmd;
        capturedArgs = argv;
        return { code: 0, stdout: "ok", stderr: "" };
      },
    } as any;
    secondOpinionExtension(mockPi);

    const mockCtx = {
      cwd: "/launch/dir",
      sessionManager: { getBranch: () => [] },
    } as any;

    await toolDef.execute(
      "call-3",
      {
        action: "review",
        planFile: "/tmp/p.md",
        backend: "agy",
        model: "Gemini 3.7 Pro (High)",
        timeoutSeconds: 300,
      },
      undefined,
      undefined,
      mockCtx,
    );
    // pi.exec has no `env` option, so env is delivered via the `env` command.
    expect(capturedCmd).toBe("env");
    expect(capturedArgs).toContain("SECOND_OPINION_AGY_MODEL=Gemini 3.7 Pro (High)");
    expect(capturedArgs).toContain("SECOND_OPINION_AGY_TIMEOUT_SECONDS=300");
    expect(capturedArgs).toContain("python3");
    expect(capturedArgs).toContain("review");
    expect(capturedArgs).toContain("/tmp/p.md");
  });

  test("no model/timeout keeps the plain python3 invocation (no `env`)", async () => {
    let capturedCmd: string | undefined;
    let toolDef: any;
    const mockPi = {
      registerTool: (_def: any) => {
        toolDef = _def;
      },
      exec: async (cmd: string, _argv: string[], _options: any) => {
        capturedCmd = cmd;
        return { code: 0, stdout: "ok", stderr: "" };
      },
    } as any;
    secondOpinionExtension(mockPi);
    const mockCtx = {
      cwd: "/launch/dir",
      sessionManager: { getBranch: () => [] },
    } as any;
    await toolDef.execute("call-4", { action: "detect" }, undefined, undefined, mockCtx);
    expect(capturedCmd).toBe("python3");
  });
});

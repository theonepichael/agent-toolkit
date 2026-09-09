import { describe, expect, test } from "bun:test";
import vitalsPromotionExtension, {
  assertFields,
  buildArgv,
  type VitalsPromotionParams,
} from "../extensions/vitals-promotion-tool";

describe("assertFields", () => {
  test("run accepts apply and dataDir", () => {
    expect(() => assertFields("run", { action: "run" })).not.toThrow();
    expect(() => assertFields("run", { action: "run", apply: true })).not.toThrow();
    expect(() => assertFields("run", { action: "run", dataDir: "/tmp/grill" })).not.toThrow();
  });

  test("search requires query", () => {
    expect(() => assertFields("search", { action: "search" })).toThrow(/requires: query/);
  });

  test("search accepts query, includeSuperseded and dataDir", () => {
    expect(() => assertFields("search", { action: "search", query: "vitals" })).not.toThrow();
    expect(() =>
      assertFields("search", { action: "search", query: "vitals", includeSuperseded: true }),
    ).not.toThrow();
    expect(() =>
      assertFields("search", { action: "search", query: "vitals", dataDir: "/tmp/grill" }),
    ).not.toThrow();
    expect(() =>
      assertFields("search", {
        action: "search",
        query: "vitals",
        backlogSlug: "proj-x",
      }),
    ).not.toThrow();
  });

  test("query and includeSuperseded are rejected on run", () => {
    // --search and its sub-flags are unrelated to the promote/supersede
    // pass; passing them here would be silently dropped, so refuse instead.
    expect(() => assertFields("run", { action: "run", query: "vitals" } as any)).toThrow(
      /does not accept: query/,
    );
    expect(() => assertFields("run", { action: "run", includeSuperseded: true } as any)).toThrow(
      /does not accept: includeSuperseded/,
    );
  });

  test("an undefined field is not treated as supplied", () => {
    const params: VitalsPromotionParams = {
      action: "search",
      query: "vitals",
      includeSuperseded: undefined,
    };
    expect(() => assertFields("search", params)).not.toThrow();
  });
});

describe("buildArgv", () => {
  test("run defaults to a dry run", () => {
    expect(buildArgv("run", { action: "run" })).toEqual([]);
  });

  test("run with apply passes --apply", () => {
    expect(buildArgv("run", { action: "run", apply: true })).toEqual(["--apply"]);
  });

  test("apply false is still a dry run", () => {
    expect(buildArgv("run", { action: "run", apply: false })).toEqual([]);
  });

  test("search passes its query", () => {
    expect(buildArgv("search", { action: "search", query: "vitals query" })).toEqual([
      "--search",
      "vitals query",
    ]);
  });

  test("search with includeSuperseded passes its flag", () => {
    expect(
      buildArgv("search", { action: "search", query: "vitals", includeSuperseded: true }),
    ).toEqual(["--search", "vitals", "--include-superseded"]);
  });

  test("search includeSuperseded false omits the flag", () => {
    expect(
      buildArgv("search", { action: "search", query: "vitals", includeSuperseded: false }),
    ).toEqual(["--search", "vitals"]);
  });

  test("search with backlogSlug passes it", () => {
    expect(
      buildArgv("search", { action: "search", query: "vitals", backlogSlug: "proj-x" }),
    ).toEqual(["--search", "vitals", "--backlog-slug", "proj-x"]);
  });

  test("dataDir is passed through on both actions", () => {
    expect(buildArgv("run", { action: "run", apply: true, dataDir: "/tmp/grill" })).toEqual([
      "--apply",
      "--data-dir",
      "/tmp/grill",
    ]);
    expect(
      buildArgv("search", {
        action: "search",
        query: "vitals",
        dataDir: "/tmp/grill",
      }),
    ).toEqual(["--search", "vitals", "--data-dir", "/tmp/grill"]);
  });
});

describe("vitalsPromotionExtension execute", () => {
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
    vitalsPromotionExtension(mockPi);

    const mockCtx = {
      cwd: "/launch/dir",
      sessionManager: {
        getBranch: () => [{ type: "custom", customType: "cwd-change", data: { cwd: "/tmp" } }],
      },
    } as any;

    await toolDef.execute("call-1", { action: "run" }, undefined, undefined, mockCtx);
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
    vitalsPromotionExtension(mockPi);

    const mockCtx = {
      cwd: "/launch/dir",
      sessionManager: {
        getBranch: () => [],
      },
    } as any;

    await toolDef.execute("call-2", { action: "run" }, undefined, undefined, mockCtx);
    expect(capturedOptions.cwd).toBe("/launch/dir");
  });
});

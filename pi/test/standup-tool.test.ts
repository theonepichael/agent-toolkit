import { describe, expect, test } from "bun:test";
import standupExtension, { assertFields, buildArgv } from "../extensions/standup-tool";

describe("assertFields", () => {
  test("fetch needs nothing", () => {
    expect(() => assertFields("fetch", { action: "fetch" })).not.toThrow();
  });

  test("fetch accepts a date", () => {
    expect(() => assertFields("fetch", { action: "fetch", date: "2026-08-30" })).not.toThrow();
  });

  test("a malformed date is refused before the script sees it", () => {
    // standup.py's --date is a bare string; a wrong shape silently produces a
    // window around the wrong day rather than erroring, so gate it here.
    expect(() => assertFields("fetch", { action: "fetch", date: "30-08-2026" })).toThrow(
      /date must be YYYY-MM-DD/,
    );
    expect(() => assertFields("fetch", { action: "fetch", date: "today" })).toThrow(
      /date must be YYYY-MM-DD/,
    );
    expect(() => assertFields("fetch", { action: "fetch", date: "2026-8-3" })).toThrow(
      /date must be YYYY-MM-DD/,
    );
  });
});

describe("buildArgv", () => {
  test("fetch with no date", () => {
    expect(buildArgv("fetch", { action: "fetch" })).toEqual(["fetch"]);
  });

  test("fetch with a date passes --date", () => {
    expect(buildArgv("fetch", { action: "fetch", date: "2026-08-30" })).toEqual([
      "fetch",
      "--date",
      "2026-08-30",
    ]);
  });
});

describe("standupExtension execute", () => {
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
    standupExtension(mockPi);

    const mockCtx = {
      cwd: "/launch/dir",
      sessionManager: {
        getBranch: () => [{ type: "custom", customType: "cwd-change", data: { cwd: "/tmp" } }],
      },
    } as any;

    await toolDef.execute("call-1", { action: "fetch" }, undefined, undefined, mockCtx);
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
    standupExtension(mockPi);

    const mockCtx = {
      cwd: "/launch/dir",
      sessionManager: {
        getBranch: () => [],
      },
    } as any;

    await toolDef.execute("call-2", { action: "fetch" }, undefined, undefined, mockCtx);
    expect(capturedOptions.cwd).toBe("/launch/dir");
  });
});

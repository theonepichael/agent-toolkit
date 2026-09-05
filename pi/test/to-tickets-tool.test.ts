import { describe, expect, test } from "bun:test";
import toTicketsExtension, { assertFields, buildArgv } from "../extensions/to-tickets-tool";

describe("assertFields", () => {
  test("run requires batchFile", () => {
    expect(() => assertFields("run", { action: "run" })).toThrow(/requires: batchFile/);
  });

  test("run with batchFile is accepted", () => {
    expect(() =>
      assertFields("run", { action: "run", batchFile: "/tmp/x-tickets-batch.json" }),
    ).not.toThrow();
  });

  test("an empty batchFile is not a path", () => {
    // "" is defined, so the required-field check alone would pass it through
    // and the runner would fail deep in argparse with a worse message.
    expect(() => assertFields("run", { action: "run", batchFile: "  " })).toThrow(
      /batchFile must not be empty/,
    );
  });
});

describe("buildArgv", () => {
  test("run passes the batch file through", () => {
    expect(buildArgv("run", { action: "run", batchFile: "/tmp/x-tickets-batch.json" })).toEqual([
      "run",
      "/tmp/x-tickets-batch.json",
    ]);
  });

  test("the batch path is passed as one argv element, never shell-split", () => {
    // The batch file carries summary/context text with apostrophes; passing
    // it as a discrete argv element is what keeps that off a shell string.
    const path = "/home/yanil/.claude/data/to-tickets/it's-a-topic-tickets-batch.json";
    expect(buildArgv("run", { action: "run", batchFile: path })).toEqual(["run", path]);
  });
});

describe("toTicketsExtension execute", () => {
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
    toTicketsExtension(mockPi);

    const mockCtx = {
      cwd: "/launch/dir",
      sessionManager: {
        getBranch: () => [{ type: "custom", customType: "cwd-change", data: { cwd: "/tmp" } }],
      },
    } as any;

    await toolDef.execute(
      "call-1",
      { action: "run", batchFile: "/tmp/b.json" },
      undefined,
      undefined,
      mockCtx,
    );
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
    toTicketsExtension(mockPi);

    const mockCtx = {
      cwd: "/launch/dir",
      sessionManager: {
        getBranch: () => [],
      },
    } as any;

    await toolDef.execute(
      "call-2",
      { action: "run", batchFile: "/tmp/b.json" },
      undefined,
      undefined,
      mockCtx,
    );
    expect(capturedOptions.cwd).toBe("/launch/dir");
  });
});

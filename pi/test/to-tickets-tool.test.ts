import { describe, expect, test } from "bun:test";
import { assertFields, buildArgv } from "../extensions/to-tickets-tool";

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

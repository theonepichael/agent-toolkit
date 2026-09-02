import { describe, expect, test } from "bun:test";
import { assertFields, buildArgv } from "../extensions/standup-tool";

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

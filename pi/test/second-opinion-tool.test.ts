import { describe, expect, test } from "bun:test";
import { assertFields, buildArgv } from "../extensions/second-opinion-tool";

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
        focusFile: "/tmp/p-focus.md",
        modelIndex: 2,
      }),
    ).toEqual([
      "review",
      "/tmp/p.md",
      "--backend",
      "opencode",
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

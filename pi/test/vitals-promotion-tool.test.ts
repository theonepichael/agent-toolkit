import { describe, expect, test } from "bun:test";
import {
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

  test("needs_review_summary accepts only dataDir", () => {
    expect(() =>
      assertFields("needs_review_summary", { action: "needs_review_summary" }),
    ).not.toThrow();
    expect(() =>
      assertFields("needs_review_summary", {
        action: "needs_review_summary",
        dataDir: "/tmp/grill",
      }),
    ).not.toThrow();
  });

  test("apply is rejected on needs_review_summary", () => {
    // --apply and --needs-review-summary are unrelated flags on the script;
    // passing apply here would be silently dropped, so refuse instead.
    expect(() =>
      assertFields("needs_review_summary", { action: "needs_review_summary", apply: true }),
    ).toThrow(/does not accept: apply/);
  });

  test("an undefined field is not treated as supplied", () => {
    const params: VitalsPromotionParams = {
      action: "needs_review_summary",
      apply: undefined,
    };
    expect(() => assertFields("needs_review_summary", params)).not.toThrow();
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

  test("needs_review_summary passes its flag", () => {
    expect(buildArgv("needs_review_summary", { action: "needs_review_summary" })).toEqual([
      "--needs-review-summary",
    ]);
  });

  test("dataDir is passed through on both actions", () => {
    expect(buildArgv("run", { action: "run", apply: true, dataDir: "/tmp/grill" })).toEqual([
      "--apply",
      "--data-dir",
      "/tmp/grill",
    ]);
    expect(
      buildArgv("needs_review_summary", {
        action: "needs_review_summary",
        dataDir: "/tmp/grill",
      }),
    ).toEqual(["--needs-review-summary", "--data-dir", "/tmp/grill"]);
  });
});

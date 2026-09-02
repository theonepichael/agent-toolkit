import { describe, expect, test } from "bun:test";
import {
  assertFields,
  assertNotNumericIdentity,
  buildArgv,
  type Action,
  type DevStatusParams,
} from "../extensions/dev-status-tool";

describe("assertNotNumericIdentity", () => {
  test("rejects numeric slugs on mutating actions", () => {
    for (const action of ["update", "start", "block", "pending_update"] as Action[]) {
      expect(() => assertNotNumericIdentity(action, { action, slug: "17" })).toThrow(
        /slug must be a slug|secondarySlug must be a slug/,
      );
    }
  });

  test("rejects numeric secondarySlug too", () => {
    expect(() =>
      assertNotNumericIdentity("block", { action: "block", slug: "abc", secondarySlug: "9" }),
    ).toThrow(/secondarySlug must be a slug/);
  });

  test("slugs pass through", () => {
    expect(() =>
      assertNotNumericIdentity("update", { action: "update", slug: "pi-ts-tooling" }),
    ).not.toThrow();
  });

  test("non-mutating actions accept numeric positions (read-only resolution)", () => {
    expect(() => assertNotNumericIdentity("show", { action: "show", slug: "17" })).not.toThrow();
  });
});

describe("assertFields", () => {
  test("valid field sets pass", () => {
    expect(() =>
      assertFields("update", { action: "update", slug: "abc", patch: {} }),
    ).not.toThrow();
    expect(() => assertFields("render", { action: "render" })).not.toThrow();
  });

  test("missing required fields are reported", () => {
    expect(() => assertFields("update", { action: "update", slug: "abc" })).toThrow(
      "requires: patch",
    );
    expect(() => assertFields("update", { action: "update", patch: {} })).toThrow(/requires: slug/);
  });

  test("fields outside the action's allowlist are rejected", () => {
    expect(() => assertFields("show", { action: "show", slug: "abc", raw: true })).toThrow(
      "does not accept: raw",
    );
  });

  test("add/pending_add require patch.id", () => {
    expect(() => assertFields("add", { action: "add", patch: {} })).toThrow("requires patch.id");
    expect(() =>
      assertFields("pending_add", { action: "pending_add", patch: { id: "x" } }),
    ).not.toThrow();
  });

  test("prune requires force: true", () => {
    expect(() => assertFields("prune", { action: "prune", force: false })).toThrow(
      /requires force: true/,
    );
    expect(() => assertFields("prune", { action: "prune", force: true })).not.toThrow();
  });

  test("start accepts optional force/allowMain/claimedBy", () => {
    expect(() =>
      assertFields("start", {
        action: "start",
        slug: "abc",
        force: true,
        allowMain: true,
        claimedBy: "pi",
      }),
    ).not.toThrow();
    expect(() => assertFields("start", { action: "start", slug: "abc" })).not.toThrow();
  });
});

describe("buildArgv", () => {
  test("bare actions", () => {
    expect(buildArgv("render", { action: "render" })).toEqual(["render"]);
    expect(buildArgv("pending_list", { action: "pending_list" })).toEqual(["pending", "list"]);
    expect(buildArgv("prune", { action: "prune", force: true })).toEqual(["prune", "--force"]);
  });

  test("flag-carrying actions", () => {
    expect(buildArgv("list", { action: "list", status: "open", raw: true })).toEqual([
      "list",
      "--status",
      "open",
      "--raw",
    ]);
    expect(buildArgv("list", { action: "list" })).toEqual(["list"]);
  });

  test("slug actions", () => {
    expect(buildArgv("show", { action: "show", slug: "abc" })).toEqual(["show", "abc"]);
    expect(buildArgv("gate_pass", { action: "gate_pass", slug: "abc" })).toEqual([
      "gate-pass",
      "abc",
    ]);
    expect(
      buildArgv("gate_pass", { action: "gate_pass", slug: "abc", patch: { coverage: {} } }),
    ).toEqual(["gate-pass", "abc", JSON.stringify({ coverage: {} })]);
    expect(buildArgv("run", { action: "run", slug: "abc", command: ["pytest", "-q"] })).toEqual([
      "run",
      "abc",
      "--",
      "pytest",
      "-q",
    ]);
    expect(
      buildArgv("run", {
        action: "run",
        slug: "abc",
        command: ["pytest", "-q"],
        timeout: 60,
      }),
    ).toEqual(["run", "abc", "--timeout", "60", "--", "pytest", "-q"]);
    expect(buildArgv("runs", { action: "runs", slug: "abc" })).toEqual(["runs", "abc"]);
  });

  test("start carries optional force/allowMain/claimedBy flags", () => {
    expect(buildArgv("start", { action: "start", slug: "abc" })).toEqual(["start", "abc"]);
    expect(buildArgv("start", { action: "start", slug: "abc", force: true })).toEqual([
      "start",
      "abc",
      "--force",
    ]);
    expect(buildArgv("start", { action: "start", slug: "abc", allowMain: true })).toEqual([
      "start",
      "abc",
      "--allow-main",
    ]);
    expect(buildArgv("start", { action: "start", slug: "abc", claimedBy: "pi" })).toEqual([
      "start",
      "abc",
      "--claimed-by",
      "pi",
    ]);
    expect(
      buildArgv("start", {
        action: "start",
        slug: "abc",
        force: true,
        allowMain: true,
        claimedBy: "pi",
      }),
    ).toEqual(["start", "abc", "--force", "--allow-main", "--claimed-by", "pi"]);
  });

  test("patch actions serialize the patch", () => {
    const patch = { id: "abc", summary: "x" };
    expect(buildArgv("update", { action: "update", slug: "abc", patch })).toEqual([
      "update",
      "abc",
      JSON.stringify(patch),
    ]);
    expect(buildArgv("add", { action: "add", patch })).toEqual(["add", JSON.stringify(patch)]);
  });

  test("backfill_gate applies only when asked", () => {
    expect(buildArgv("backfill_gate", { action: "backfill_gate" })).toEqual(["backfill-gate"]);
    expect(buildArgv("backfill_gate", { action: "backfill_gate", apply: true })).toEqual([
      "backfill-gate",
      "--apply",
    ]);
  });

  test("out-of-scope add carries the reason file and optional related item", () => {
    expect(
      buildArgv("out_of_scope_add", {
        action: "out_of_scope_add",
        slug: "s",
        reasonFile: "/tmp/reason.md",
      }),
    ).toEqual(["out-of-scope", "add", "s", "--reason-file", "/tmp/reason.md"]);
    expect(
      buildArgv("out_of_scope_add", {
        action: "out_of_scope_add",
        slug: "s",
        reasonFile: "/tmp/reason.md",
        secondarySlug: "related",
      }),
    ).toEqual([
      "out-of-scope",
      "add",
      "s",
      "--reason-file",
      "/tmp/reason.md",
      "--related-item",
      "related",
    ]);
  });
});

// Type-surface smoke: these params are construction sites for the exported
// helpers' signatures (DevStatusParams) — keeps tsc honest about the
// exported type shape without needing a live Pi session.
const _typeSurface: DevStatusParams = { action: "render" };
void _typeSurface;

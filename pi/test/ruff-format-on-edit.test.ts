import { describe, expect, test } from "bun:test";
import { mkdtempSync, mkdirSync, writeFileSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { findPyprojectRoot } from "../extensions/ruff-format-on-edit";

describe("findPyprojectRoot", () => {
  const makeTree = (): { root: string; cleanup: () => void } => {
    const base = mkdtempSync(join(tmpdir(), "pi-ext-test-"));
    return { root: base, cleanup: () => rmSync(base, { recursive: true, force: true }) };
  };

  test("returns the directory itself when it holds pyproject.toml", () => {
    const { root, cleanup } = makeTree();
    try {
      writeFileSync(join(root, "pyproject.toml"), "");
      expect(findPyprojectRoot(root)).toBe(root);
    } finally {
      cleanup();
    }
  });

  test("walks up to the nearest ancestor with pyproject.toml", () => {
    const { root, cleanup } = makeTree();
    try {
      const nested = join(root, "src", "pkg");
      mkdirSync(nested, { recursive: true });
      writeFileSync(join(root, "pyproject.toml"), "");
      expect(findPyprojectRoot(nested)).toBe(root);
    } finally {
      cleanup();
    }
  });

  test("returns null when no ancestor has pyproject.toml", () => {
    const { root, cleanup } = makeTree();
    try {
      const nested = join(root, "a", "b");
      mkdirSync(nested, { recursive: true });
      expect(findPyprojectRoot(nested)).toBeNull();
    } finally {
      cleanup();
    }
  });
});

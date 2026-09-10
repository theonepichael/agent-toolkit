/**
 * The suite's single entry point for test structure and matchers, replacing
 * Bun's `bun:test`.
 *
 * Why a shim instead of importing `node:test` directly in every spec:
 * `node:test` has no `expect` (it exports `undefined` for that name), so the
 * matcher half has to come from the standalone `expect` package. Re-exporting
 * both from here keeps each spec at exactly one import line, which is also the
 * only line that changed when the suite moved off `bun:test` — the ~1300
 * assertion call sites are untouched.
 *
 * `expect` ships as a default export; it is rebound to a named export so specs
 * read the same way they did under Bun.
 */
import { afterEach, beforeEach, describe, it, test } from "node:test";
import nodeExpect from "expect";

export const expect = nodeExpect;
export { describe, it, test, beforeEach, afterEach };

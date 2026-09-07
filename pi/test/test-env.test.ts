import { expect, test } from "bun:test";

// The bunfig.toml [test] preload (test/helpers/sanitize-test-env.ts) deletes
// PI_AGENT_UNATTENDED before any test module loads. This pins that invariant.
//
// Why it matters: the agent session that runs this suite routinely carries
// PI_AGENT_UNATTENDED=1 in its own environment, and a test process that
// inherits it turns every attended-mode expectation into an unattended
// refusal — guard-rails reads the variable per tool_call (refuses instead of
// asking) and permission-gate bakes it into `enabled` at module load, so
// five tests in toggle-check.test.ts fail while the product code is fine.
// If this test fails, the preload is not being loaded: check bunfig.toml.
test("the suite runs attended by default, regardless of the ambient environment", () => {
  expect(process.env.PI_AGENT_UNATTENDED).toBeUndefined();
});

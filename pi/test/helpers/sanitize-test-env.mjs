/**
 * Deletes PI_AGENT_UNATTENDED from the test process environment before any
 * test module loads. Wired as an explicit `--import` on pi/package.json's
 * `test` script, so `npm test` and `npm run test:clean` both get it.
 *
 * Why it matters: the agent session running this suite routinely carries
 * PI_AGENT_UNATTENDED=1 in its own environment, and the test process inherits
 * it. Guard-rails then refuses instead of asking (it reads the variable per
 * tool_call) and permission-gate loads disabled (it caches the variable at
 * module load), so the attended-mode expectations in toggle-check.test.ts
 * fail even though the product code is fine — observed 2026-09-07, where
 * exactly those five tests failed on main in every agent-driven run while
 * passing in a clean shell.
 *
 * Tests that exercise unattended behavior set the variable themselves
 * (toggle-check.test.ts's unattended block saves/restores it around each
 * test); this preload only removes the ambient value, not those.
 *
 * Why `.mjs` and not `.ts`: this file is the earliest thing the process runs,
 * and it has no types to check, so making it depend on the TypeScript loader
 * being registered first buys nothing and adds a load-order constraint.
 * (`.js` is not an option either — pi/package.json declares no "type" field,
 * so a `.js` there would be CommonJS.) test-env.test.ts pins this invariant.
 */
delete process.env.PI_AGENT_UNATTENDED;

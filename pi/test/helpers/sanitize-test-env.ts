/**
 * Deletes PI_AGENT_UNATTENDED from the test process environment before any
 * test module loads. Wired from bunfig.toml's [test] preload, so both
 * `bun test` and `bun run test` get it.
 *
 * Why: the agent session running this suite routinely carries
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
 */
delete process.env.PI_AGENT_UNATTENDED;

import { execSync } from "node:child_process";
import { mkdtempSync, readFileSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { afterEach, beforeEach, describe, expect, test } from "bun:test";

import { type SwarmState } from "../../copilot/extensions/swarm/src/swarm-scheduling.js";

import {
  buildAgentStartArgv,
  buildTabCreateArgv,
  parseAgentSession,
} from "../../copilot/extensions/swarm/src/swarm-herdr.js";

import {
  classifyBlock,
  parsePicker,
  pickerLabels,
} from "../../copilot/extensions/swarm/src/swarm-picker.js";

import {
  defaultExec,
  isValidUuid,
  saveState,
  SwarmToolContext,
} from "../../copilot/extensions/swarm/src/swarm-tool-logic.js";

describe("Copilot Swarm: Staleness and Build Consistency", () => {
  test("compiled artifacts match fresh build from src", () => {
    const rootDir = join(__dirname, "../..");
    const extensionBefore = readFileSync(
      join(rootDir, "copilot/extensions/swarm/extensions/swarm/extension.mjs"),
      "utf8",
    );
    const logicBefore = readFileSync(
      join(rootDir, "copilot/extensions/swarm/lib/swarm-tool-logic.js"),
      "utf8",
    );

    // Run build script
    execSync("./scripts/build-copilot-swarm.sh", { cwd: rootDir });

    const extensionAfter = readFileSync(
      join(rootDir, "copilot/extensions/swarm/extensions/swarm/extension.mjs"),
      "utf8",
    );
    const logicAfter = readFileSync(
      join(rootDir, "copilot/extensions/swarm/lib/swarm-tool-logic.js"),
      "utf8",
    );

    expect(extensionAfter).toBe(extensionBefore);
    expect(logicAfter).toBe(logicBefore);
  });
});

describe("Copilot Swarm: Pure Module Functions", () => {
  test("buildTabCreateArgv produces copilot tab without PI_AGENT_UNATTENDED", () => {
    const argv = buildTabCreateArgv("/tmp", "atk-test", {
      captureFile: "/tmp/capture.json",
      kind: "copilot",
    });
    expect(argv).not.toContain("PI_AGENT_UNATTENDED=1");
    expect(argv).toContain("COPILOT_SWARM_CAPTURE_FILE=/tmp/capture.json");
    expect(argv).toContain("--label");
    expect(argv).toContain("atk-test");
  });

  test("buildTabCreateArgv pi kind retains PI_AGENT_UNATTENDED", () => {
    const argv = buildTabCreateArgv("/tmp", "atk-test", {
      captureFile: "/tmp/capture.json",
      kind: "pi",
    });
    expect(argv).toContain("PI_AGENT_UNATTENDED=1");
    expect(argv).toContain("PI_SWARM_CAPTURE_FILE=/tmp/capture.json");
  });

  test("buildAgentStartArgv passes session-id, allow-all-tools, plugin-dir, model", () => {
    const argv = buildAgentStartArgv("worker-1", "w:p1", "gpt-5", {
      kind: "copilot",
      sessionId: "123e4567-e89b-12d3-a456-426614174000",
      allowAllTools: true,
      pluginDir: "/custom/plugins",
    });
    expect(argv.slice(0, 5)).toEqual(["agent", "start", "worker-1", "--kind", "copilot"]);
    const sep = argv.indexOf("--");
    expect(sep).toBeGreaterThan(-1);
    const passthrough = argv.slice(sep + 1);
    expect(passthrough).toContain("--session-id");
    expect(passthrough[passthrough.indexOf("--session-id") + 1]).toBe(
      "123e4567-e89b-12d3-a456-426614174000",
    );
    expect(passthrough).toContain("--allow-all-tools");
    expect(passthrough).toContain("--plugin-dir");
    expect(passthrough[passthrough.indexOf("--plugin-dir") + 1]).toBe("/custom/plugins");
    expect(passthrough).toContain("--model");
    expect(passthrough[passthrough.indexOf("--model") + 1]).toBe("gpt-5");
  });

  test("buildAgentStartArgv supports --resume=<id>", () => {
    const argv = buildAgentStartArgv("worker-1", "w:p1", "gpt-5", {
      kind: "copilot",
      resumeSessionId: "123e4567-e89b-12d3-a456-426614174000",
      allowAllTools: true,
      pluginDir: "/custom/plugins",
    });
    const sep = argv.indexOf("--");
    const passthrough = argv.slice(sep + 1);
    expect(passthrough).toContain("--resume=123e4567-e89b-12d3-a456-426614174000");
    expect(passthrough).not.toContain("--session-id");
  });

  test("buildAgentStartArgv pi kind remains byte-identical", () => {
    const argv = buildAgentStartArgv("worker-1", "w:p1", "claude-3-7-sonnet", { kind: "pi" });
    expect(argv).toEqual([
      "agent",
      "start",
      "worker-1",
      "--kind",
      "pi",
      "--pane",
      "w:p1",
      "--timeout",
      "30000",
      "--",
      "--model",
      "claude-3-7-sonnet",
    ]);
  });

  test("parseAgentSession extracts agent_session value", () => {
    const stdout = JSON.stringify({
      result: {
        agent: {
          agent_session: { kind: "copilot", value: "abc-123" },
        },
      },
    });
    expect(parseAgentSession(stdout)).toBe("abc-123");
    expect(parseAgentSession("{}")).toBeUndefined();
  });

  test("picker stub returns empty options and needs_human", () => {
    expect(parsePicker("1. Yes\n2. No").options).toEqual([]);
    expect(classifyBlock("Any prompt")).toBe("needs_human");
    expect(pickerLabels("Any prompt")).toEqual([]);
  });

  test("isValidUuid checks format", () => {
    expect(isValidUuid("123e4567-e89b-12d3-a456-426614174000")).toBe(true);
    expect(isValidUuid("invalid-uuid")).toBe(false);
    expect(isValidUuid("")).toBe(false);
  });
});

describe("Copilot Swarm: SwarmToolContext Behavioral Tests", () => {
  let tempStateDir: string;

  beforeEach(() => {
    tempStateDir = mkdtempSync(join(tmpdir(), "copilot-swarm-test-"));
    process.env.COPILOT_SWARM_STATE_DIR = tempStateDir;
  });

  afterEach(() => {
    delete process.env.COPILOT_SWARM_STATE_DIR;
    rmSync(tempStateDir, { recursive: true, force: true });
  });

  test("swarmResolveBlocked always returns needs_manual with captured prompt", async () => {
    const fakeExec = async (cmd: string, args: string[]) => {
      if (cmd === "herdr") {
        if (args[0] === "agent" && args[1] === "read") {
          return { code: 0, stdout: "Permission prompt: Allow bash command?", stderr: "" };
        }
      }
      return { code: 0, stdout: "{}", stderr: "" };
    };

    const ctx = new SwarmToolContext(fakeExec);
    const state: SwarmState = {
      runId: "r1",
      concurrency: 3,
      nextCounter: 1,
      workers: [
        {
          agent: "r1-w1",
          slug: "atk-foo",
          paneId: "w:p1",
          tabId: "w:t1",
          lifecycle: "awaiting_relay",
        },
      ],
    };
    saveState(state, tempStateDir);

    const res = await ctx.swarmResolveBlocked({
      runId: "r1",
      agent: "r1-w1",
      answer: "Allow",
    });

    expect(res.details.needsManual).toBe(true);
    expect(res.details.relayFailed).toBe(false);
    expect(res.content[0]?.text).toContain("needs_manual:");
    expect(res.content[0]?.text).toContain("Permission prompt: Allow bash command?");
  });

  test("crash recovery during getOrInitState restarts gone worker with --resume", async () => {
    const herdrCalls: string[][] = [];
    const fakeExec = async (cmd: string, args: string[]) => {
      if (cmd === "herdr") {
        herdrCalls.push(args);
        if (args[0] === "agent" && args[1] === "list") {
          // Worker is gone from herdr
          return { code: 0, stdout: JSON.stringify({ result: { agents: [] } }), stderr: "" };
        }
        if (args[0] === "tab" && args[1] === "create") {
          return {
            code: 0,
            stdout: JSON.stringify({
              result: { root_pane: { pane_id: "w:p2" }, tab: { tab_id: "w:t2" } },
            }),
            stderr: "",
          };
        }
        if (args[0] === "agent" && args[1] === "start") {
          return { code: 0, stdout: JSON.stringify({ result: {} }), stderr: "" };
        }
        if (args[0] === "agent" && args[1] === "prompt") {
          return { code: 0, stdout: JSON.stringify({ result: {} }), stderr: "" };
        }
        if (args[0] === "tab" && args[1] === "close") {
          return { code: 0, stdout: JSON.stringify({ result: {} }), stderr: "" };
        }
      }
      return { code: 0, stdout: "{}", stderr: "" };
    };

    const ctx = new SwarmToolContext(fakeExec);
    const workerSessionId = "123e4567-e89b-12d3-a456-426614174000";
    const state: SwarmState = {
      runId: "r1",
      concurrency: 3,
      nextCounter: 1,
      workers: [
        {
          agent: "r1-w1",
          slug: "atk-foo",
          paneId: "w:p1",
          tabId: "w:t1",
          lifecycle: "active",
          copilotSessionId: workerSessionId,
          recoveryAttempts: 0,
        },
      ],
    };
    saveState(state, tempStateDir);

    const reconciled = await ctx.getOrInitState("r1", 3);
    expect(reconciled.workers.length).toBe(1);
    const recoveredWorker = reconciled.workers[0]!;
    expect(recoveredWorker.paneId).toBe("w:p2");
    expect(recoveredWorker.tabId).toBe("w:t2");
    expect(recoveredWorker.recoveryAttempts).toBe(1);

    // Verify agent start had --resume
    const startCall = herdrCalls.find((c) => c[0] === "agent" && c[1] === "start");
    expect(startCall).toBeDefined();
    expect(startCall).toContain(`--resume=${workerSessionId}`);

    // Verify prompt re-prompted worker
    const promptCall = herdrCalls.find((c) => c[0] === "agent" && c[1] === "prompt");
    expect(promptCall).toBeDefined();
    expect(promptCall?.[3]).toContain("Continue working on this backlog item where you left off.");
  });

  test("crash recovery capped at MAX_RECOVERY_ATTEMPTS", async () => {
    const fakeExec = async (cmd: string, args: string[]) => {
      if (cmd === "herdr" && args[0] === "agent" && args[1] === "list") {
        return { code: 0, stdout: JSON.stringify({ result: { agents: [] } }), stderr: "" };
      }
      return { code: 0, stdout: "{}", stderr: "" };
    };

    const ctx = new SwarmToolContext(fakeExec);
    const state: SwarmState = {
      runId: "r1",
      concurrency: 3,
      nextCounter: 1,
      workers: [
        {
          agent: "r1-w1",
          slug: "atk-foo",
          paneId: "w:p1",
          tabId: "w:t1",
          lifecycle: "active",
          copilotSessionId: "123e4567-e89b-12d3-a456-426614174000",
          recoveryAttempts: 2, // Already capped
        },
      ],
    };
    saveState(state, tempStateDir);

    const reconciled = await ctx.getOrInitState("r1", 3);
    // Worker could not be recovered, so it is dropped
    expect(reconciled.workers.length).toBe(0);
  });

  test("swarmSpawn runs spawnInto for multiple workers concurrently, not sequentially", async () => {
    let concurrentStarts = 0;
    let maxConcurrentStarts = 0;
    const fakeExec = async (cmd: string, args: string[]) => {
      if (cmd === "python3") {
        return {
          code: 0,
          stdout: JSON.stringify([
            { id: "atk-a", worker_safe: true, related_files: [] },
            { id: "atk-b", worker_safe: true, related_files: [] },
          ]),
          stderr: "",
        };
      }
      if (cmd === "herdr") {
        if (args[0] === "tab" && args[1] === "create") {
          const label = args[args.indexOf("--label") + 1];
          return {
            code: 0,
            stdout: JSON.stringify({
              result: { root_pane: { pane_id: `p-${label}` }, tab: { tab_id: `t-${label}` } },
            }),
            stderr: "",
          };
        }
        if (args[0] === "agent" && args[1] === "start") {
          concurrentStarts++;
          maxConcurrentStarts = Math.max(maxConcurrentStarts, concurrentStarts);
          await new Promise((r) => setTimeout(r, 30));
          concurrentStarts--;
          return { code: 0, stdout: JSON.stringify({ result: {} }), stderr: "" };
        }
        if (args[0] === "agent" && (args[1] === "get" || args[1] === "prompt")) {
          return { code: 0, stdout: JSON.stringify({ result: {} }), stderr: "" };
        }
      }
      return { code: 0, stdout: "{}", stderr: "" };
    };

    const ctx = new SwarmToolContext(fakeExec);
    await ctx.swarmSpawn({ runId: "r1", prefix: "atk-", concurrency: 3 });

    // Sequential spawnInto would never have two "agent start" calls in
    // flight at once; parallel spawnInto (Promise.allSettled) does.
    expect(maxConcurrentStarts).toBeGreaterThan(1);
  });
});

describe("Copilot Swarm: defaultExec timeout handling", () => {
  test("a killed-by-timeout process reports a non-zero code, never success", async () => {
    const result = await defaultExec("sleep", ["5"], { timeout: 50 });
    // A kill()-ed process reports code null; `code ?? 0` would otherwise
    // read as a clean exit, which every caller treats as success.
    expect(result.code).not.toBe(0);
    expect(result.stderr).toContain("timed out");
  });
});

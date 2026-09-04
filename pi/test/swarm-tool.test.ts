import { mkdirSync, mkdtempSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { afterEach, beforeEach, describe, expect, test } from "bun:test";
import registerSwarmTools, {
  activeWorkerCount,
  AMEND_INSTRUCTION,
  capturePath,
  classifyBlock,
  noteResolveFailure,
  pickerLabels,
  classifyTimeoutProbe,
  deadlineStopDetail,
  elapsedWorkingMs,
  foldWorkingSegment,
  formatDuration,
  workerWorktreePath,
  buildAgentGetArgv,
  buildAgentListArgv,
  buildAgentPromptArgv,
  buildAgentReadArgv,
  buildAgentSendKeysArgv,
  buildAgentStartArgv,
  buildAgentWaitArgv,
  buildPaneCloseArgv,
  buildTabCloseArgv,
  buildTabCreateArgv,
  buildTabListArgv,
  buildWorkerCloseArgv,
  canOpenNewPane,
  canSpawnNew,
  classifyWaitResult,
  findTabByLabel,
  itemPaths,
  parseReadyItems,
  parseTabCreate,
  readCaptureOffers,
  selectSchedulable,
  stalledRelayWorkers,
  loadState,
  looksTruncated,
  matchOption,
  navigationKeys,
  nextAgentId,
  openPaneCount,
  openPaneSoftCap,
  paneIdentityMismatch,
  PANE_CAPTURE_CHARS,
  parseAgentListIds,
  parsePicker,
  reconcileState,
  saveState,
  spawnBudget,
  statePath,
  waitResultDetail,
  WORKER_UNATTENDED_ENV,
  type SwarmState,
  type WorkerRecord,
} from "../extensions/swarm-tool";

// Captured verbatim from a real herdr agent read against a live blocked
// question-tool.ts picker (see the plan's manual smoke run, section 5 step
// 4) -- not synthesized, so parsePicker/matchOption are tested against the
// real rendering, ANSI stripped by --source recent-unwrapped.
const REAL_PICKER_OUTPUT = `
 question Reply OK to continue?


 ⠧ Working...

─────────────────────────────────────────────────────────────────────────────────────
 Smoke test
 Reply OK to continue?

> 1. OK
     Continue with whatever comes next in the session.
  2. Cancel
     Stop here without doing anything further.
  3. Something else (type it)
     Answer in your own words instead.

 ↑↓ navigate • Enter to select • Esc to cancel
─────────────────────────────────────────────────────────────────────────────────────
~/dotfiles-meta-pi-swarm-orchestration (meta-pi-swarm-orchestration)
↑16k ↓65 $0.001 1.6%/1.0M (auto)                                 glm-5.3-flash • high`;

function makeWorker(overrides: Partial<WorkerRecord> = {}): WorkerRecord {
  return {
    agent: "run1-w1",
    slug: "iron-lb-example",
    paneId: "w1:pA",
    tabId: "w1:tA",
    lifecycle: "active",
    ...overrides,
  };
}

function makeState(overrides: Partial<SwarmState> = {}): SwarmState {
  return { runId: "run1", concurrency: 3, nextCounter: 0, workers: [], ...overrides };
}

describe("nextAgentId", () => {
  test("is short, namespaced by run, never exceeds 32 chars", () => {
    expect(nextAgentId("run1", 1)).toBe("run1-w1");
    expect(nextAgentId("run1", 12)).toBe("run1-w12");
    expect(nextAgentId("run1", 1, "fix-bug")).toBe("run1-w1-fix-bug");
    expect(nextAgentId("run1", 1, "a-very-long-backlog-item-slug-name-that-exceeds-limits")).toBe(
      "run1-w1-name-that-exceeds-limits",
    );
    expect(
      nextAgentId("run1", 1, "a-very-long-backlog-item-slug-name-that-exceeds-limits").length,
    ).toBeLessThanOrEqual(32);
  });

  test("strips a known project prefix even when no truncation is needed", () => {
    expect(nextAgentId("r1", 1, "meta-fix-bug")).toBe("r1-w1-fix-bug");
    expect(nextAgentId("r1", 1, "work-fix-bug")).toBe("r1-w1-fix-bug");
  });

  test("truncates from the slug's head, so the distinguishing tail survives", () => {
    expect(nextAgentId("shakedown1", 3, "meta-second-opinion-copilot-model-pool")).toBe(
      "shakedown1-w3-copilot-model-pool",
    );
  });

  test("two slugs sharing a project prefix produce visibly different names", () => {
    const a = nextAgentId("shakedown1", 1, "meta-second-opinion-copilot-argv");
    const b = nextAgentId("shakedown1", 2, "meta-second-opinion-pi-size-guard");
    expect(a).toBe("shakedown1-w1-inion-copilot-argv");
    expect(b).toBe("shakedown1-w2-nion-pi-size-guard");
    expect(a).not.toBe(b);
    expect(a.length).toBeLessThanOrEqual(32);
    expect(b.length).toBeLessThanOrEqual(32);
  });

  test("strips the longest matching prefix", () => {
    expect(nextAgentId("r1", 1, "iron-lb-some-fancy-long-item-slug-here-ok")).toBe(
      "r1-w1-ncy-long-item-slug-here-ok",
    );
  });

  test("strips exactly ONE prefix, never both", () => {
    // Reducing over PROJECT_PREFIXES and stripping each match in turn would
    // take `meta-` and then `work-` off this slug and leave `double-prefix`,
    // silently dropping a segment that tells items apart.
    expect(nextAgentId("r1", 1, "meta-work-double-prefix")).toBe("r1-w1-work-double-prefix");
  });

  test("a slug that is exactly a prefix leaves no dangling separator", () => {
    expect(nextAgentId("run1", 1, "meta-")).toBe("run1-w1");
  });

  test("a pathological runId longer than the cap still yields a 32-char name", () => {
    const id = nextAgentId("a".repeat(40), 1, "meta-x");
    expect(id.length).toBeLessThanOrEqual(32);
    expect(id).toBe("a".repeat(32));
  });
});

describe("herdr argv builders", () => {
  test("tab create: cwd, slug label, unattended env, and never steals the human's focus", () => {
    expect(buildTabCreateArgv("/repo", "my-slug")).toEqual([
      "tab",
      "create",
      "--cwd",
      "/repo",
      "--label",
      "my-slug",
      "--env",
      "PI_AGENT_UNATTENDED=1",
      "--no-focus",
    ]);
  });

  test("tab close: the tab id", () => {
    expect(buildTabCloseArgv("w1:tN")).toEqual(["tab", "close", "w1:tN"]);
  });

  test("tearing a worker down closes the tab it owns", () => {
    expect(buildWorkerCloseArgv(makeWorker({ paneId: "w1:pA", tabId: "w1:tA" }))).toEqual([
      "tab",
      "close",
      "w1:tA",
    ]);
  });

  test("a worker restored from a pre-tabs state file is still closed by its pane", () => {
    // Workers used to live in panes split out of the orchestrator's own, so a
    // state file written by that version carries no tabId. Closing its tab is
    // not an option and skipping the close would leak a live pi into a pane
    // nothing polls, so the pane close stays reachable for exactly this case.
    const legacy = makeWorker({ paneId: "w1:pA" });
    delete legacy.tabId;
    expect(buildWorkerCloseArgv(legacy)).toEqual(["pane", "close", "w1:pA"]);
  });

  test("agent start: kind pi, targets the given pane", () => {
    const argv = buildAgentStartArgv("run1-w1", "w1:pB");
    expect(argv).toEqual([
      "agent",
      "start",
      "run1-w1",
      "--kind",
      "pi",
      "--pane",
      "w1:pB",
      "--timeout",
      "30000",
    ]);
  });

  test("agent start: a model is handed to pi after the -- separator, not to herdr", () => {
    // herdr's usage is `agent start <NAME> --kind <KIND> --pane <ID> [OPTIONS]
    // [-- [AGENT_ARG]...]`, so everything for pi has to sit after `--`.
    // Without the separator herdr would reject --model as its own unknown flag.
    const argv = buildAgentStartArgv("run1-w1", "w1:pB", "opencode-go/glm-5.3-flash");
    expect(argv).toEqual([
      "agent",
      "start",
      "run1-w1",
      "--kind",
      "pi",
      "--pane",
      "w1:pB",
      "--timeout",
      "30000",
      "--",
      "--model",
      "opencode-go/glm-5.3-flash",
    ]);
  });

  test("agent start: no model means today's argv exactly, separator included", () => {
    // Omitted must stay byte-identical to the pre-change command: an empty
    // trailing `--` is a different command line and pi parses it differently.
    const argv = buildAgentStartArgv("run1-w1", "w1:pB", undefined);
    expect(argv).not.toContain("--");
    expect(argv).not.toContain("--model");
    expect(argv).toEqual(buildAgentStartArgv("run1-w1", "w1:pB"));
  });

  test("agent start: the model is one discrete argv element, never shell-interpolated", () => {
    const argv = buildAgentStartArgv("run1-w1", "w1:pB", "provider/model with space");
    expect(argv[argv.length - 1]).toBe("provider/model with space");
  });

  test("agent prompt: prompt is a discrete argv element, never shell-interpolated", () => {
    const nasty = `answer with a "quote" and $(rm -rf /) and 'apostrophes'`;
    const argv = buildAgentPromptArgv("run1-w1", nasty);
    expect(argv).toEqual(["agent", "prompt", "run1-w1", nasty]);
    // one element, not split/concatenated into a shell-executable string
    expect(argv.filter((a) => a.includes("rm -rf"))).toHaveLength(1);
  });

  test("agent wait: --until repeats per state (herdr rejects a comma-joined list), timeout in ms", () => {
    expect(buildAgentWaitArgv("run1-w1", ["idle", "done", "blocked"], 5000)).toEqual([
      "agent",
      "wait",
      "run1-w1",
      "--until",
      "idle",
      "--until",
      "done",
      "--until",
      "blocked",
      "--timeout",
      "5000",
    ]);
  });

  test("agent get / agent read / pane close / agent list", () => {
    expect(buildAgentGetArgv("run1-w1")).toEqual(["agent", "get", "run1-w1"]);
    expect(buildAgentReadArgv("run1-w1", 500)).toEqual([
      "agent",
      "read",
      "run1-w1",
      "--source",
      "recent-unwrapped",
      "--lines",
      "500",
    ]);
    expect(buildPaneCloseArgv("w1:pB")).toEqual(["pane", "close", "w1:pB"]);
    expect(buildAgentListArgv()).toEqual(["agent", "list"]);
  });

  test("agent send-keys: keys are discrete argv elements, in order", () => {
    expect(buildAgentSendKeysArgv("run1-w1", ["down", "down", "enter"])).toEqual([
      "agent",
      "send-keys",
      "run1-w1",
      "down",
      "down",
      "enter",
    ]);
  });
});

describe("parsePicker", () => {
  test("parses the real captured rendering: 3 options, selection on 1", () => {
    const parsed = parsePicker(REAL_PICKER_OUTPUT);
    expect(parsed.selectedIndex).toBe(1);
    expect(parsed.options).toEqual([
      { index: 1, label: "OK" },
      { index: 2, label: "Cancel" },
      { index: 3, label: "Something else (type it)" },
    ]);
  });

  test("no selection marker present -> selectedIndex is null", () => {
    const noMarker = REAL_PICKER_OUTPUT.replace("> 1. OK", "  1. OK");
    expect(parsePicker(noMarker).selectedIndex).toBeNull();
  });

  test("content with no option lines at all parses to an empty list", () => {
    expect(parsePicker("just some ordinary text\nno options here")).toEqual({
      selectedIndex: null,
      options: [],
    });
  });
});

// Scrollback above a live picker, holding a numbered list that is not an
// option list. Captured shape from the 2026-09-02 ultrareview: the plan line
// "1. Merge to main and push" parsed as option 1 and navigationKeys computed
// arrow presses from that fabricated index.
const PICKER_BELOW_A_NUMBERED_PLAN = `
 Here is the plan I intend to follow:

 1. Merge to main and push
 2. Remove the worktree
 3. Close the item

─────────────────────────────────────────────────────────────────────────────────────
 Commit
 Commit these changes?

> 1. Commit now (Recommended)
     Commit the current changes right away.
  2. Stop here
     Leave the changes uncommitted and stop.
  3. Something else (type it)
     Answer in your own words instead.

 ↑↓ navigate • Enter to select • Esc to cancel
─────────────────────────────────────────────────────────────────────────────────────
~/dotfiles (main)`;

// Verbatim from a real 42-column worker pane during the 2026-09-02 concurrent
// swarm run. Three workers split a 168-column tab into four equal panes, and
// at that width question-tool's footer wraps -- which the full-width fixtures
// above never showed.
const NARROW_PANE_PICKER = `
 herdr-agent-state.ts exception)?


 \u2387 Working...

───────────────────────────────────────
 Commit
 Commit these changes (docs +
 regression test pinning the
 herdr-agent-state.ts exception)?

> 1. Commit (Recommended)
     Commit the doc + regression test
     in the worktree as docs(pi):
     document herdr-agent-state.ts as
     the managed_dir standing exception
  2. Don't commit yet
     Leave the changes uncommitted in
     the worktree; the item stays in
     progress
  3. Something else (type it)
     Answer in your own words instead.

 \u2191\u2193 navigate \u2022 Enter to select \u2022 Esc to
 cancel
───────────────────────────────────────
~/dotfiles (main)`;

describe("parsePicker anchoring", () => {
  // THE DEFECT. OPTION_LINE matched any numbered line in the 500 lines of
  // scrollback `agent read` returns, with no anchor to the live picker, so a
  // plan list above the picker became the option list.
  test("reads the live picker, not a numbered list in the scrollback above it", () => {
    const parsed = parsePicker(PICKER_BELOW_A_NUMBERED_PLAN);

    expect(parsed.options.map((o) => o.label)).toEqual([
      "Commit now (Recommended)",
      "Stop here",
      "Something else (type it)",
    ]);
    expect(parsed.selectedIndex).toBe(1);
  });

  test("the real single-picker capture still parses unchanged", () => {
    const parsed = parsePicker(REAL_PICKER_OUTPUT);
    expect(parsed.options.map((o) => o.label)).toEqual([
      "OK",
      "Cancel",
      "Something else (type it)",
    ]);
    expect(parsed.selectedIndex).toBe(1);
  });

  // THE REGRESSION. The footer anchor required "navigate ... Esc to cancel" on
  // one line, which only holds in a wide pane. A worker pane in a real 3-way
  // split is 42 columns, the footer wraps after "Esc to", the anchor missed,
  // and parsePicker returned nothing at all -- so every relay to a worker came
  // back needs_manual with "(none parsed)". Caught live on 2026-09-02 the
  // first time the fix from ec5fd5a met a genuinely narrow pane.
  test("finds the picker when a narrow pane wraps the footer line", () => {
    const parsed = parsePicker(NARROW_PANE_PICKER);

    expect(parsed.options.map((o) => o.label)).toEqual([
      "Commit (Recommended)",
      "Don't commit yet",
      "Something else (type it)",
    ]);
    expect(parsed.selectedIndex).toBe(1);
  });

  test("content with no picker at all yields no options", () => {
    const parsed = parsePicker("1. not a picker\n2. still not a picker\n");
    expect(parsed.options).toEqual([]);
    expect(parsed.selectedIndex).toBeNull();
  });

  // question-tool renders `${i + 1}.`, so a real picker's indices are always
  // contiguous from 1. Anything else means the window caught something that
  // is not an option list, and guessing from it is how the wrong option got
  // submitted -- report nothing and let the caller fall back to needs_manual.
  test("non-contiguous indices inside the window are rejected outright", () => {
    const broken = PICKER_BELOW_A_NUMBERED_PLAN.replace("  2. Stop here", "  7. Stop here");
    expect(parsePicker(broken).options).toEqual([]);
  });

  test("a multi-select picker's footer anchors just as well", () => {
    const multi = PICKER_BELOW_A_NUMBERED_PLAN.replace(
      "↑↓ navigate • Enter to select • Esc to cancel",
      "↑↓ navigate • Space to toggle • Enter to confirm • Esc to cancel",
    );
    expect(parsePicker(multi).options).toHaveLength(3);
  });
});

describe("matchOption", () => {
  const options = parsePicker(REAL_PICKER_OUTPUT).options;

  test("exact case-insensitive match", () => {
    expect(matchOption("ok", options)).toEqual({ index: 1, label: "OK" });
    expect(matchOption("Cancel", options)).toEqual({ index: 2, label: "Cancel" });
  });

  test("substring match against a longer label", () => {
    const longer = [{ index: 1, label: "Yes, commit (Recommended)" }, ...options.slice(1)];
    expect(matchOption("yes", longer)).toEqual({ index: 1, label: "Yes, commit (Recommended)" });
  });

  test("never auto-selects the free-text escape option, even if it matches", () => {
    expect(matchOption("something else", options)).toBeNull();
    expect(matchOption("type it", options)).toBeNull();
  });

  test("no match and ambiguous match both return null -- never guess", () => {
    expect(matchOption("nothing like these options", options)).toBeNull();
    const ambiguous = [
      { index: 1, label: "Approve now" },
      { index: 2, label: "Approve later" },
    ];
    expect(matchOption("approve", ambiguous)).toBeNull();
  });

  test("blank answer never matches", () => {
    expect(matchOption("   ", options)).toBeNull();
  });
});

describe("matchOption safety", () => {
  const commitGate = [
    { index: 1, label: "Commit now (Recommended)" },
    { index: 2, label: "Stop here" },
    { index: 3, label: "Something else (type it)" },
  ];

  // THE DEFECT, and the worst one in the relay path. The bare substring
  // fallback made "no" a single unambiguous match for "Commit now
  // (Recommended)" -- the label contains those two letters inside "now", the
  // other label does not -- so the user said no and the worker committed.
  // Reproduced live on 2026-09-02 against a real picker.
  test("a short answer never matches inside a longer word", () => {
    expect(matchOption("no", commitGate)).toBeNull();
    expect(matchOption("No", commitGate)).toBeNull();
  });

  test("an answer that is a whole word in exactly one label still matches", () => {
    expect(matchOption("Stop here", commitGate)?.label).toBe("Stop here");
    expect(matchOption("stop", commitGate)?.label).toBe("Stop here");
    expect(matchOption("commit now", commitGate)?.label).toBe("Commit now (Recommended)");
  });

  // Exact match is checked before any fuzzy rule, so a genuinely short
  // option label stays answerable.
  test("a short answer still matches a label that IS that word", () => {
    const yesNo = [
      { index: 1, label: "Yes" },
      { index: 2, label: "No" },
    ];
    expect(matchOption("no", yesNo)?.label).toBe("No");
    expect(matchOption("YES", yesNo)?.label).toBe("Yes");
  });

  test("an answer matching several labels stays ambiguous", () => {
    const both = [
      { index: 1, label: "Commit and push" },
      { index: 2, label: "Commit only" },
    ];
    expect(matchOption("commit", both)).toBeNull();
  });

  test("the free-text escape is never auto-selected", () => {
    expect(matchOption("Something else (type it)", commitGate)).toBeNull();
    expect(matchOption("type it", commitGate)).toBeNull();
  });
});

describe("navigationKeys", () => {
  test("moving down N steps then submitting", () => {
    expect(navigationKeys(1, 3)).toEqual(["down", "down", "enter"]);
  });

  test("moving up steps then submitting", () => {
    expect(navigationKeys(3, 1)).toEqual(["up", "up", "enter"]);
  });

  test("already on the target -- just submit, no navigation keys", () => {
    expect(navigationKeys(2, 2)).toEqual(["enter"]);
  });
});

describe("classifyWaitResult", () => {
  // `agent get`/`agent wait` nest the payload one level under "agent" --
  // confirmed live, repeatedly (e.g. `herdr agent get <id>` ->
  // {"result":{"agent":{"agent_status":"blocked",...}}}). An earlier version
  // of both the code and these fixtures used a flat {result:{agent_status}}
  // shape that matched the code's wrong assumption instead of the real
  // captured format, so the tests never caught the bug -- these fixtures
  // are deliberately shaped to match the real wire format, not the
  // implementation, to guard against that happening again.
  function waitStdout(agent_status: string): string {
    return JSON.stringify({ result: { agent: { agent_status } } });
  }

  test("blocked status maps to blocked", () => {
    expect(classifyWaitResult(0, waitStdout("blocked"), "")).toBe("blocked");
  });

  test("idle or done status maps to finished", () => {
    expect(classifyWaitResult(0, waitStdout("idle"), "")).toBe("finished");
    expect(classifyWaitResult(0, waitStdout("done"), "")).toBe("finished");
  });

  test("the old flat (unnested) shape is NOT recognized -- regression guard for the live-discovered field-path bug", () => {
    const flatShape = JSON.stringify({ result: { agent_status: "blocked" } });
    expect(classifyWaitResult(0, flatShape, "")).toBe("error");
  });

  test("a genuine herdr timeout error on stderr maps to timed_out", () => {
    // real captured shape: exit 1, {"error":{"code":"timeout",...}} on stderr
    const stderr = JSON.stringify({
      error: { code: "timeout", message: "timed out waiting for agent status" },
      id: "cli:agent:wait",
    });
    expect(classifyWaitResult(1, "", stderr)).toBe("timed_out");
  });

  test("any other nonzero exit is 'error', not silently folded into timed_out", () => {
    // this is the live-discovered bug under direct test: agent_not_found,
    // a killed process, or any error other than a genuine timeout must be
    // distinguishable from a real 30-minute deadline elapsing.
    const agentNotFound = JSON.stringify({
      error: { code: "agent_not_found", message: "agent target w1 not found" },
    });
    expect(classifyWaitResult(1, "", agentNotFound)).toBe("error");
    expect(classifyWaitResult(1, "", "")).toBe("error"); // unparseable/empty stderr, still not a timeout
    expect(classifyWaitResult(2, "", "some CLI usage error")).toBe("error");
  });

  test("unparseable or unrecognized status on a zero exit is 'error', not a crash and not timed_out", () => {
    // this is exactly the shape a killed-but-resolved exec call produces
    // (code coerced to 0, empty/partial stdout) -- must not look like a
    // real settle or a real timeout.
    expect(classifyWaitResult(0, "not json", "")).toBe("error");
    expect(classifyWaitResult(0, waitStdout("unknown"), "")).toBe("error");
    expect(classifyWaitResult(0, "", "")).toBe("error");
  });
});

describe("paneIdentityMismatch", () => {
  // Same real `agent get` envelope shape as classifyWaitResult's fixtures
  // (result.agent.*, not flat) -- `agent get`/`agent wait` share the
  // nesting, `agent list` does not (see the HerdrEnvelope comment).
  function getStdout(pane_id: string): string {
    return JSON.stringify({ result: { agent: { agent_status: "blocked", pane_id } } });
  }

  test("matching pane_id is not a mismatch", () => {
    expect(paneIdentityMismatch(0, getStdout("w1:pA"), "w1:pA")).toBe(false);
  });

  test("a different pane_id IS a mismatch -- the exact failure mode this guards against", () => {
    expect(paneIdentityMismatch(0, getStdout("w1:pB"), "w1:pA")).toBe(true);
  });

  test("a nonzero exit code (agent_not_found, a crash) is a mismatch -- fail closed, not open", () => {
    expect(paneIdentityMismatch(1, "", "w1:pA")).toBe(true);
  });

  test("unparseable or missing pane_id on a zero exit is a mismatch", () => {
    expect(paneIdentityMismatch(0, "not json", "w1:pA")).toBe(true);
    expect(paneIdentityMismatch(0, JSON.stringify({ result: { agent: {} } }), "w1:pA")).toBe(true);
  });
});

describe("waitResultDetail", () => {
  test("prefers the structured herdr error message when present", () => {
    const stderr = JSON.stringify({
      error: { code: "agent_not_found", message: "agent target w1 not found" },
    });
    expect(waitResultDetail("", stderr)).toBe("agent_not_found: agent target w1 not found");
  });

  test("falls back to raw stdout, then stderr, then a placeholder", () => {
    expect(waitResultDetail("some raw output", "")).toBe("some raw output");
    expect(waitResultDetail("", "raw stderr text")).toBe("raw stderr text");
    expect(waitResultDetail("", "")).toBe("(no output)");
  });
});

describe("looksTruncated", () => {
  test("content filling the exact requested line budget is flagged", () => {
    const content = Array.from({ length: 500 }, (_, i) => `line ${i}`).join("\n");
    expect(looksTruncated(content, 500)).toBe(true);
  });

  test("content well under the budget is not flagged", () => {
    expect(looksTruncated("a\nb\nc", 500)).toBe(false);
  });
});

describe("concurrency and pane accounting", () => {
  test("activeWorkerCount counts only active, not awaiting_relay", () => {
    const state = makeState({
      workers: [
        makeWorker({ agent: "w1", lifecycle: "active" }),
        makeWorker({ agent: "w2", lifecycle: "awaiting_relay" }),
        makeWorker({ agent: "w3", lifecycle: "active" }),
      ],
    });
    expect(activeWorkerCount(state)).toBe(2);
  });

  test("canSpawnNew respects the concurrency cap, ignoring awaiting_relay workers", () => {
    const state = makeState({
      concurrency: 2,
      workers: [
        makeWorker({ agent: "w1", lifecycle: "active" }),
        makeWorker({ agent: "w2", lifecycle: "awaiting_relay" }),
        makeWorker({ agent: "w3", lifecycle: "awaiting_relay" }),
      ],
    });
    // only 1 active against a cap of 2 -- room for one more, even though 3
    // panes total are open (a blocked worker never blocks a new spawn)
    expect(canSpawnNew(state)).toBe(true);
  });

  test("canSpawnNew is false once active count reaches the cap, even mid-overshoot", () => {
    // simulates the round-3 fix: three blocked -> three replacements -> all
    // three resolved back-to-back can transiently push active count above
    // cap. canSpawnNew must say no to a FOURTH spawn in that state, not
    // silently allow unbounded growth.
    const state = makeState({
      concurrency: 3,
      workers: [
        makeWorker({ agent: "w1", lifecycle: "active" }),
        makeWorker({ agent: "w2", lifecycle: "active" }),
        makeWorker({ agent: "w3", lifecycle: "active" }),
        makeWorker({ agent: "w4", lifecycle: "active" }),
      ],
    });
    expect(canSpawnNew(state)).toBe(false);
  });

  test("openPaneSoftCap is 2x concurrency", () => {
    expect(openPaneSoftCap(3)).toBe(6);
    expect(openPaneSoftCap(5)).toBe(10);
  });

  test("openPaneCount counts active + awaiting_relay together", () => {
    const state = makeState({
      workers: [
        makeWorker({ agent: "w1", lifecycle: "active" }),
        makeWorker({ agent: "w2", lifecycle: "awaiting_relay" }),
      ],
    });
    expect(openPaneCount(state)).toBe(2);
  });

  test("canOpenNewPane is false once the soft cap is hit", () => {
    const state = makeState({
      concurrency: 2,
      workers: [
        makeWorker({ agent: "w1", lifecycle: "awaiting_relay" }),
        makeWorker({ agent: "w2", lifecycle: "awaiting_relay" }),
        makeWorker({ agent: "w3", lifecycle: "awaiting_relay" }),
        makeWorker({ agent: "w4", lifecycle: "awaiting_relay" }),
      ],
    });
    expect(canOpenNewPane(state)).toBe(false); // 4 open panes == 2x2 soft cap
  });
});

describe("spawnBudget", () => {
  test("bounded by concurrency, pane soft cap, and the ready queue size, whichever is smallest", () => {
    expect(spawnBudget(makeState({ concurrency: 3, workers: [] }), 10)).toBe(3);
    expect(spawnBudget(makeState({ concurrency: 3, workers: [] }), 1)).toBe(1);
    const nearPaneCap = makeState({
      concurrency: 3,
      workers: Array.from({ length: 5 }, (_, i) =>
        makeWorker({ agent: `w${i}`, lifecycle: "awaiting_relay" }),
      ),
    });
    // pane soft cap is 6, 5 already open -> only 1 more pane fits, even
    // though the concurrency cap alone would allow 3
    expect(spawnBudget(nearPaneCap, 10)).toBe(1);
  });

  test("never negative even when already over cap", () => {
    const over = makeState({
      concurrency: 2,
      workers: Array.from({ length: 5 }, (_, i) =>
        makeWorker({ agent: `w${i}`, lifecycle: "active" }),
      ),
    });
    expect(spawnBudget(over, 10)).toBe(0);
  });
});

describe("reconcileState", () => {
  test("a tracked worker still live in herdr is kept", () => {
    const state = makeState({ workers: [makeWorker({ agent: "w1" })] });
    const { state: reconciled, dropped } = reconcileState(state, ["w1"]);
    expect(reconciled.workers).toHaveLength(1);
    expect(dropped).toHaveLength(0);
  });

  test("a tracked worker no longer in herdr's live list is dropped and reported", () => {
    const state = makeState({
      workers: [makeWorker({ agent: "w1" }), makeWorker({ agent: "w2" })],
    });
    const { state: reconciled, dropped } = reconcileState(state, ["w1"]); // w2 is dead
    expect(reconciled.workers.map((w) => w.agent)).toEqual(["w1"]);
    expect(dropped.map((w) => w.agent)).toEqual(["w2"]);
  });

  test("an empty live list drops every tracked worker", () => {
    const state = makeState({
      workers: [makeWorker({ agent: "w1" }), makeWorker({ agent: "w2" })],
    });
    const { state: reconciled, dropped } = reconcileState(state, []);
    expect(reconciled.workers).toHaveLength(0);
    expect(dropped).toHaveLength(2);
  });

  test("a live list mixing a named worker with an unnamed foreign agent keeps only the worker", () => {
    // herdr omits `name` entirely for agents not started with an explicit
    // name (captured live, herdr 0.8.2 -- see realAgentListEnvelope). The
    // parse must extract only the named worker: never dropping it, never
    // adopting the unnamed stranger.
    const state = makeState({ workers: [makeWorker({ agent: "probe-w1" })] });
    const liveIds = parseAgentListIds(
      realAgentListEnvelope(UNNAMED_CLAUDE_ENTRY, {
        agent: "pi",
        agent_status: "idle",
        name: "probe-w1",
        pane_id: "w1:p2N",
      }),
    );
    expect(liveIds).toEqual(["probe-w1"]);
    const { state: reconciled, dropped } = reconcileState(state, liveIds);
    expect(reconciled.workers.map((w) => w.agent)).toEqual(["probe-w1"]);
    expect(dropped).toHaveLength(0);
  });
});

describe("state persistence", () => {
  let dir: string;

  beforeEach(() => {
    dir = mkdtempSync(join(tmpdir(), "swarm-tool-test-"));
  });

  afterEach(() => {
    rmSync(dir, { recursive: true, force: true });
  });

  test("loadState on a never-saved run returns null", () => {
    expect(loadState("nope", dir)).toBeNull();
  });

  test("saveState then loadState round-trips", () => {
    const state = makeState({ workers: [makeWorker()] });
    saveState(state, dir);
    expect(loadState("run1", dir)).toEqual(state);
  });

  test("statePath is namespaced by runId under the given dir", () => {
    expect(statePath("run1", dir)).toBe(join(dir, "swarm-run1.json"));
  });

  test("corrupt state file is treated as absent, not thrown", () => {
    const path = statePath("bad", dir);
    mkdirSync(dir, { recursive: true });
    writeFileSync(path, "{not json");
    expect(loadState("bad", dir)).toBeNull();
  });
});

// ---------------------------------------------------------------------------
// swarm_poll's execute() wiring.
//
// classifyWaitResult is unit-tested above, but the live failure that
// motivated these tests was never a classification bug in isolation -- it
// was what execute() *does* with the classification. A settle payload that
// classifies as anything other than "blocked" takes the else branch: pane
// closed, worker dropped from state, `herdr agent get` thereafter returning
// agent_not_found. So a worker sitting at a real approval gate got its pane
// destroyed and became unrecoverable, with no relay target left.
//
// These drive the registered tool's real execute() against a stubbed
// ExtensionAPI, asserting the pane/lifecycle consequences rather than the
// returned label -- the part that had no coverage.
// ---------------------------------------------------------------------------

/**
 * A real `herdr agent wait` settle envelope, captured from herdr 0.8.2 on
 * 2026-09-02 (`herdr agent wait swarmctl2 --until idle`): exit 0, payload on
 * stdout, agent fields nested one level under `result.agent`, with
 * `result.type` = "agent_info". Only agent_status/name/pane_id vary here.
 * Shaped to the real wire format on purpose -- an earlier fixture set copied
 * the code's wrong flat assumption instead, so the tests agreed with the bug.
 */
function realWaitEnvelope(agentStatus: string, name: string, paneId: string): string {
  return JSON.stringify({
    id: "cli:agent:wait",
    result: {
      agent: {
        agent: "pi",
        agent_status: agentStatus,
        cwd: "/home/yanil/dotfiles",
        focused: false,
        interactive_ready: true,
        name,
        pane_id: paneId,
        revision: 1,
        tab_id: "w1:t1",
        workspace_id: "w1",
      },
      type: "agent_info",
    },
  });
}

/** One entry of a real `herdr agent list` payload. `name` is omitted entirely (not undefined) exactly when the agent was not started via `herdr agent start <NAME>`. */
interface RealAgentListEntry {
  agent: string;
  agent_status: string;
  name?: string;
  pane_id: string;
}

/**
 * A real `herdr agent list` payload shape, captured from herdr 0.8.2 on
 * 2026-09-02. Entries are verbatim from the capture -- in particular `name`
 * is present only when the agent was started with an explicit name, and a
 * claude session launched without one carries NO name key at all:
 *   {"agent":"claude","agent_status":"working","pane_id":"w1:pX"}               -> no name key
 *   {"agent":"pi","agent_status":"idle","name":"probe-w1","pane_id":"w1:p2N"}   -> name key present
 * Swarm workers are always started named (buildAgentStartArgv passes the
 * synthetic id as NAME), so parseAgentListIds reading `.name` matches our
 * workers and ignores unnamed foreign agents. Shaped to the real wire
 * format on purpose, like realWaitEnvelope above -- an earlier hand-written
 * `{name: ...}` stub pinned the code's own assumption, so the fixture could
 * never have caught a mismatch either way.
 */
function realAgentListEnvelope(...agents: RealAgentListEntry[]): string {
  return JSON.stringify({ result: { agents } });
}

/** The captured unnamed claude-shaped entry, verbatim. */
const UNNAMED_CLAUDE_ENTRY: RealAgentListEntry = {
  agent: "claude",
  agent_status: "working",
  pane_id: "w1:pX",
};

interface ExecCall {
  argv: string[];
}

/** Minimal ExtensionAPI stub -- swarm-tool only ever touches exec + registerTool. */
function makeStubPi(respond: (argv: string[]) => { code: number; stdout: string; stderr: string }) {
  const calls: ExecCall[] = [];
  const tools = new Map<string, { execute: (...a: never[]) => Promise<unknown> }>();
  const pi = {
    exec(_cmd: string, argv: string[]) {
      calls.push({ argv });
      return Promise.resolve(respond(argv));
    },
    registerTool(def: { name: string; execute: (...a: never[]) => Promise<unknown> }) {
      tools.set(def.name, def);
    },
  };
  return { pi, calls, tools };
}

describe("swarm_poll execute() wiring", () => {
  let dir: string;
  let priorStateDir: string | undefined;

  beforeEach(() => {
    dir = mkdtempSync(join(tmpdir(), "swarm-poll-exec-"));
    // Keeps the tool's own `persist` off the real ~/.pi/agent/state.
    priorStateDir = process.env.PI_SWARM_STATE_DIR;
    process.env.PI_SWARM_STATE_DIR = dir;
  });

  afterEach(() => {
    if (priorStateDir === undefined) delete process.env.PI_SWARM_STATE_DIR;
    else process.env.PI_SWARM_STATE_DIR = priorStateDir;
    rmSync(dir, { recursive: true, force: true });
  });

  /** Seeds one active worker on disk and returns the wired-up swarm_poll. */
  function setup(waitStdout: string, waitCode = 0, waitStderr = "") {
    const runId = "execrun";
    const worker: WorkerRecord = {
      agent: "execrun-w1",
      slug: "some-item",
      paneId: "w1:pZ",
      tabId: "w1:tZ",
      lifecycle: "active",
    };
    const state: SwarmState = { runId, concurrency: 2, nextCounter: 1, workers: [worker] };
    saveState(state, dir);

    const stub = makeStubPi((argv) => {
      const [a, b] = argv;
      if (a === "agent" && b === "list") {
        return {
          code: 0,
          // Real wire shape (see realAgentListEnvelope): our named worker
          // alongside an unnamed foreign agent it must ignore.
          stdout: realAgentListEnvelope(UNNAMED_CLAUDE_ENTRY, {
            agent: "pi",
            agent_status: "idle",
            name: "execrun-w1",
            pane_id: "w1:pZ",
          }),
          stderr: "",
        };
      }
      if (a === "agent" && b === "wait") {
        return { code: waitCode, stdout: waitStdout, stderr: waitStderr };
      }
      if (a === "agent" && b === "get") {
        return { code: 0, stdout: realWaitEnvelope("blocked", "execrun-w1", "w1:pZ"), stderr: "" };
      }
      if (a === "agent" && b === "read") {
        return { code: 0, stdout: "Commit these changes?\n> Yes\n  No\n", stderr: "" };
      }
      return { code: 0, stdout: "", stderr: "" };
    });

    registerSwarmTools(stub.pi as unknown as Parameters<typeof registerSwarmTools>[0]);
    const poll = stub.tools.get("swarm_poll");
    if (!poll) throw new Error("swarm_poll was never registered");
    return { runId, poll, stub };
  }

  const workerCloses = (stub: { calls: ExecCall[] }) =>
    stub.calls.filter((c) => c.argv[0] === "tab" && c.argv[1] === "close");

  test("a blocked settle keeps the pane open and parks the worker at awaiting_relay", async () => {
    const { runId, poll, stub } = setup(realWaitEnvelope("blocked", "execrun-w1", "w1:pZ"));

    const res = (await poll.execute(
      ...(["call-1", { runId, timeoutMs: 1000 }, undefined] as unknown as never[]),
    )) as { details: { events: { kind: string }[] } };

    expect(res.details.events.map((e) => e.kind)).toEqual(["blocked"]);
    // The load-bearing assertions: the relay target must survive.
    expect(workerCloses(stub)).toHaveLength(0);
    const persisted = loadState(runId, dir);
    expect(persisted?.workers).toHaveLength(1);
    expect(persisted?.workers[0]?.lifecycle).toBe("awaiting_relay");
  });

  test("the pre-fix flat payload shape closes the pane and drops the worker -- the live bug, pinned", async () => {
    // Exactly what a blocked worker's settle looked like to the old parser:
    // real status present, but at result.agent_status instead of
    // result.agent.agent_status, so it fell through to "error".
    const flat = JSON.stringify({ result: { agent_status: "blocked" } });
    const { runId, poll, stub } = setup(flat);

    const res = (await poll.execute(
      ...(["call-1", { runId, timeoutMs: 1000 }, undefined] as unknown as never[]),
    )) as { details: { events: { kind: string }[] } };

    expect(res.details.events.map((e) => e.kind)).toEqual(["error"]);
    expect(workerCloses(stub)).toHaveLength(1);
    expect(loadState(runId, dir)?.workers).toHaveLength(0);
  });

  // Was: "a genuine herdr timeout closes the pane and drops the worker".
  // That pinned the bug. An elapsed wait means nothing settled in the window,
  // not that the worker died, so it now provokes a liveness probe -- and this
  // fixture's `agent get` reports `blocked`, i.e. the worker settled during
  // the race between the wait giving up and the probe landing. The settle
  // wins, and the relay target survives.
  test("a wait timeout whose probe finds the worker blocked parks it, closing nothing", async () => {
    const stderr = JSON.stringify({
      error: { code: "timeout", message: "timed out waiting for agent status" },
      id: "cli:agent:wait",
    });
    const { runId, poll, stub } = setup("", 1, stderr);

    const res = (await poll.execute(
      ...(["call-1", { runId, timeoutMs: 1000 }, undefined] as unknown as never[]),
    )) as { details: { events: { kind: string }[] } };

    expect(res.details.events.map((e) => e.kind)).toEqual(["blocked"]);
    expect(workerCloses(stub)).toHaveLength(0);
    const persisted = loadState(runId, dir);
    expect(persisted?.workers).toHaveLength(1);
    expect(persisted?.workers[0]?.lifecycle).toBe("awaiting_relay");
  });

  test("an idle settle finishes the worker and frees its pane", async () => {
    const { runId, poll, stub } = setup(realWaitEnvelope("idle", "execrun-w1", "w1:pZ"));

    const res = (await poll.execute(
      ...(["call-1", { runId, timeoutMs: 1000 }, undefined] as unknown as never[]),
    )) as { details: { events: { kind: string }[] } };

    expect(res.details.events.map((e) => e.kind)).toEqual(["finished"]);
    expect(workerCloses(stub)).toHaveLength(1);
    expect(loadState(runId, dir)?.workers).toHaveLength(0);
  });

  // The second original bug shape, at the wiring level. pi.exec's underlying
  // execCommand always RESOLVES -- even for a process killed by signal --
  // and coerces the null exit code to 0, so an aborted `agent wait` arrives
  // as exit 0 with empty or partial stdout. That must stay distinguishable
  // from a real settle: it closes the pane and drops the worker, and it is
  // reported as "error" rather than a timeout that never elapsed.
  test.each([
    ["empty stdout, as a signal-killed exec resolves", ""],
    ["partial/unparseable stdout", '{"result":{"agent":{"agent_st'],
    ["valid JSON with no recognized status", JSON.stringify({ result: { agent: {} } })],
  ])("exit-0 with %s closes the pane and drops the worker", async (_label, stdout) => {
    const { runId, poll, stub } = setup(stdout);

    const res = (await poll.execute(
      ...(["call-1", { runId, timeoutMs: 1000 }, undefined] as unknown as never[]),
    )) as { details: { events: { kind: string; detail?: string }[] } };

    expect(res.details.events.map((e) => e.kind)).toEqual(["error"]);
    expect(workerCloses(stub)).toHaveLength(1);
    expect(loadState(runId, dir)?.workers).toHaveLength(0);
  });
});

// ---------------------------------------------------------------------------
// swarm_spawn's worker bootstrap.
//
// Pi ships no permission system of its own; permission-gate.ts supplies one,
// and it defaults to enabled with a "*": "ask" fallback. A worker runs in a
// herdr TUI pane, so ctx.hasUI is true and the gate calls ctx.ui.confirm --
// a human-shaped prompt in a pane nobody is watching. Every bash command
// outside ALLOW_PATTERNS therefore stalls the worker indefinitely, and
// swarm_poll cannot see it: the agent still reports "working", so the
// orchestrator relays nothing and waits out its full timeout. Observed live
// on a 3-worker run -- the only worker doing real tool work had to be
// unblocked by hand, repeatedly, which defeats the point of unattended mode.
//
// So spawn must opt each worker out of the gate before handing it any work.
// ---------------------------------------------------------------------------

//
// How it confirms the opt-out is the second half of this story, and it was
// wrong. The first version sent the command with `--wait`. herdr 0.8.2
// documents `--wait` from a non-working state as requiring an observed
// lifecycle change within five seconds, and a client-side pi slash command
// applies instantly without ever entering the working state -- so the call
// ALWAYS returned agent_prompt_stalled, and swarm_spawn always discarded a
// worker whose gate had in fact just come down. Observed live twice on
// 2026-09-02 (runId swarm-20260902-wearable3): 0 workers spawned, 3 failed,
// with every discarded worker's pane showing the gate disabled correctly.
// The swarm could not spawn a single worker from ef7c950 until this fix.
//
// The confirmation is now a file the worker itself writes, keyed by a token
// minted per spawn -- not a read of the worker's terminal, which is a
// bounded sliding window of rendered rows (see permission-gate.ts).
// ---------------------------------------------------------------------------

describe("swarm_spawn worker bootstrap", () => {
  let dir: string;
  let priorStateDir: string | undefined;

  beforeEach(() => {
    dir = mkdtempSync(join(tmpdir(), "swarm-spawn-boot-"));
    priorStateDir = process.env.PI_SWARM_STATE_DIR;
    process.env.PI_SWARM_STATE_DIR = dir;
  });

  afterEach(() => {
    if (priorStateDir === undefined) delete process.env.PI_SWARM_STATE_DIR;
    else process.env.PI_SWARM_STATE_DIR = priorStateDir;
    rmSync(dir, { recursive: true, force: true });
  });

  // These tests name their items explicitly and never cared what `ready`
  // returned, so the stubs answered it with empty stdout. That is no longer a
  // neutral answer: swarm_spawn looks each named slug up in the READY records
  // and an item it cannot find has unknown eligibility, which now fails closed.
  // Answering with the slugs these tests actually use keeps them describing
  // bootstrap mechanics rather than accidentally exercising the guard -- which
  // has its own block below. All are stamped worker-safe because the prefix
  // rule is not what is under test here.
  const BOOTSTRAP_SLUGS = ["some-item", "a1", "a2", "b1", "b2", "meta-a"];
  const readyStub = () => ({
    code: 0,
    stdout: JSON.stringify(
      BOOTSTRAP_SLUGS.map((id) => ({ id, worker_safe: true, related_files: [] })),
    ),
    stderr: "",
  });

  function stubFor(respond: (argv: string[]) => { code: number; stdout: string; stderr: string }) {
    const withReady = (argv: string[]) => {
      const answer = respond(argv);
      // Only a SUCCESSFUL empty answer is substituted: one test makes `ready`
      // fail on purpose to prove the run stops rather than reading as a
      // drained queue, and overriding that would erase what it checks.
      return argv.includes("ready") && answer.code === 0 && answer.stdout === ""
        ? readyStub()
        : answer;
    };
    const stub = makeStubPi(withReady);
    registerSwarmTools(stub.pi as unknown as Parameters<typeof registerSwarmTools>[0]);
    const spawn = stub.tools.get("swarm_spawn");
    if (!spawn) throw new Error("swarm_spawn was never registered");
    return { spawn, stub };
  }

  const tabCreateOk = (argv: string[]) =>
    argv[0] === "tab" && argv[1] === "create"
      ? {
          code: 0,
          stdout: JSON.stringify({
            result: { root_pane: { pane_id: "w1:pN" }, tab: { tab_id: "w1:tN" } },
          }),
          stderr: "",
        }
      : { code: 0, stdout: "", stderr: "" };

  const prompts = (stub: { calls: ExecCall[] }) =>
    stub.calls.filter((c) => c.argv[0] === "agent" && c.argv[1] === "prompt");

  const workerCloses = (stub: { calls: ExecCall[] }) =>
    stub.calls.filter((c) => c.argv[0] === "tab" && c.argv[1] === "close");

  const PANE_TEXT = "worker log: pi started, gate never came down";

  /** Answers `pane read` with identifiable text, so a test can assert the capture reached the reason. */
  const withPaneText =
    (respond: (argv: string[]) => { code: number; stdout: string; stderr: string }) =>
    (argv: string[]) =>
      argv[0] === "pane" && argv[1] === "read"
        ? { code: 0, stdout: PANE_TEXT, stderr: "" }
        : respond(argv);

  const isItemPrompt = (argv: string[]) =>
    argv[0] === "agent" && argv[1] === "prompt" && (argv[3] ?? "").startsWith("/backlog-item");

  const spawnItems = (spawn: { execute: (...a: never[]) => Promise<unknown> }, items: string[]) =>
    spawn.execute(
      ...(["call-1", { runId: "bootrun", items, concurrency: items.length }] as unknown as never[]),
    ) as Promise<{
      content: { type: string; text: string }[];
      details: { spawned: unknown[]; failed: { slug: string; reason: string }[] };
    }>;

  const spawnOne = (spawn: { execute: (...a: never[]) => Promise<unknown> }) =>
    spawnItems(spawn, ["some-item"]);

  // THE RACE. getOrInitState hands both calls the same cached SwarmState, and
  // state.workers.push only happens after the whole Promise.allSettled block,
  // so two overlapping spawns for one runId each compute spawnBudget against
  // the same empty pool and each spawn up to the full cap. The pane splits
  // interleave for the same reason, which the tool's own promptGuidelines say
  // must never happen ("splits are sequential -- concurrent splits race").
  const spawnRun = (
    spawn: { execute: (...a: never[]) => Promise<unknown> },
    runId: string,
    items: string[],
    concurrency: number,
  ) =>
    spawn.execute(...(["call-1", { runId, items, concurrency }] as unknown as never[])) as Promise<{
      content: { type: string; text: string }[];
      details: { spawned: unknown[]; skipped: string[] };
    }>;

  test("two concurrent spawns for one runId cannot exceed the cap between them", async () => {
    const { spawn } = stubFor(tabCreateOk);

    const [first, second] = await Promise.all([
      spawnRun(spawn, "racerun", ["a1", "a2"], 2),
      spawnRun(spawn, "racerun", ["b1", "b2"], 2),
    ]);

    const spawned = first.details.spawned.length + second.details.spawned.length;
    expect(spawned).toBeLessThanOrEqual(2);
    expect(loadState("racerun", dir)?.workers.length).toBeLessThanOrEqual(2);
  });

  // Serialising must not silently drop the second caller's work: whatever the
  // cap has no room for comes back as skipped, so the orchestrator can see its
  // items were not lost.
  test("the second concurrent spawn reports its items rather than losing them", async () => {
    const { spawn } = stubFor(tabCreateOk);

    const [first, second] = await Promise.all([
      spawnRun(spawn, "accounted", ["a1", "a2"], 2),
      spawnRun(spawn, "accounted", ["b1", "b2"], 2),
    ]);

    // The lock is FIFO, but assert on the pair rather than on which call won:
    // one spawns the cap, the other reports its items as skipped, and nothing
    // silently disappears.
    const shapes = [first, second]
      .map((r) => `${r.details.spawned.length}/${r.details.skipped.length}`)
      .sort();
    expect(shapes).toEqual(["0/2", "2/0"]);
  });

  // The whole run funnels through one chain, so a spawn that throws must
  // still release it -- otherwise one bad call wedges every later spawn for
  // that runId forever.
  test("a spawn that throws does not wedge the run's queue", async () => {
    let explode = true;
    const { spawn } = stubFor((argv) => {
      if (explode && argv[0] === "tab" && argv[1] === "create") {
        throw new Error("herdr fell over mid-create");
      }
      return tabCreateOk(argv);
    });

    await spawnRun(spawn, "wedged", ["a1"], 2).catch(() => undefined);
    explode = false;
    const after = await spawnRun(spawn, "wedged", ["b1"], 2);

    expect(after.details.spawned).toHaveLength(1);
  });

  // The gates settle themselves from the tab's environment, before pi starts.
  // This replaced a prompt-plus-acknowledgement handshake that could only ever
  // cover one of the two gates, and left a window in which a worker held real
  // work while still armed.
  test("the worker's tab carries the unattended environment", async () => {
    const { spawn, stub } = stubFor(tabCreateOk);
    await spawnOne(spawn);

    const create = stub.calls.find((c) => c.argv[0] === "tab" && c.argv[1] === "create");
    expect(create?.argv).toContain("--env");
    expect(create?.argv).toContain(WORKER_UNATTENDED_ENV);
  });

  test("the worker is sent its item and nothing else", async () => {
    const { spawn, stub } = stubFor(tabCreateOk);
    await spawnOne(spawn);

    // Exactly one prompt. A second one would mean something is still being
    // negotiated over the wire that the environment should have settled.
    expect(prompts(stub).map((c) => c.argv[3] ?? "")).toEqual(["/backlog-item --auto some-item"]);
  });

  // THE REGRESSION, kept because the constraint outlives the handshake that
  // exposed it: `--wait` cannot succeed against a prompt that produces no
  // observed lifecycle change, and a spawn must not read that as failure.
  test("a healthy worker is spawned, not discarded on a --wait artefact", async () => {
    const { spawn, stub } = stubFor((argv) =>
      // Real herdr's answer to `agent prompt ... --wait` here: exit 1,
      // agent_prompt_stalled, captured live on 2026-09-02.
      argv.includes("--wait")
        ? {
            code: 1,
            stdout: "",
            stderr: JSON.stringify({
              error: {
                code: "agent_prompt_stalled",
                message: "agent prompt produced no observed state change within 5000 ms",
              },
            }),
          }
        : tabCreateOk(argv),
    );

    const res = await spawnOne(spawn);

    expect(res.details.failed).toEqual([]);
    expect(res.details.spawned).toHaveLength(1);
    // The submit is only meaningful without --wait: with it every submit
    // failed regardless of outcome, so a non-zero exit said nothing.
    expect(prompts(stub).some((c) => c.argv.includes("--wait"))).toBe(false);
  });

  // An orchestrator only ever reads `content`. Confirmed live on 2026-09-02:
  // a real swarm_spawn failure returned its detail in `details`, and the
  // model's very next turn reported "no per-item failure details were
  // included ... no reason text, no failed array". backlog-item.md tells the
  // orchestrator to act differently per reason, so the classification has to
  // travel in the text or that instruction is unfollowable.
  test("a failure's classification reaches the content text, not just details", async () => {
    const { spawn } = stubFor(
      withPaneText((argv) =>
        argv[0] === "agent" && argv[1] === "start"
          ? { code: 1, stdout: "", stderr: '{"error":{"code":"agent_not_ready"}}' }
          : tabCreateOk(argv),
      ),
    );

    const res = await spawnOne(spawn);
    const text = res.content.map((c) => c.text).join("\n");

    expect(text).toContain("some-item");
    expect(text).toContain("agent_not_ready");
    // The pane capture deliberately stays behind in `details`: at
    // PANE_CAPTURE_CHARS per failure, a whole batch of them would flood the
    // orchestrator's context with terminal dumps to no purpose.
    expect(text).not.toContain(PANE_TEXT);
    expect(res.details.failed[0]?.reason).toContain(PANE_TEXT);
  });

  // The item prompt is the only thing sent over the wire now, so an
  // undeliverable one is the only prompt-level spawn failure left.
  test("an undeliverable item prompt fails as agent_prompt_stalled", async () => {
    const { spawn, stub } = stubFor(
      withPaneText((argv) =>
        isItemPrompt(argv)
          ? { code: 1, stdout: "", stderr: '{"error":{"code":"agent_not_found"}}' }
          : tabCreateOk(argv),
      ),
    );

    const res = await spawnOne(spawn);

    expect(res.details.failed[0]?.reason).toContain("agent_prompt_stalled");
    expect(res.details.failed[0]?.reason).toContain("agent_not_found");
    expect(res.details.failed[0]?.reason).toContain(PANE_TEXT);
    expect(workerCloses(stub)).toHaveLength(1);
  });

  // A pane's terminal text is the only record of an early worker crash --
  // the sibling pane-width bug was diagnosed entirely from a leaked pane.
  // Capture it, then close: leaving panes open is what turned two failed
  // runs into six orphan panes subdividing the layout further each retry.
  test("a post-split failure captures the pane's output into the reason, then closes the pane", async () => {
    const { spawn, stub } = stubFor((argv) => {
      if (argv[0] === "agent" && argv[1] === "start") {
        return { code: 1, stdout: "", stderr: '{"error":{"code":"agent_not_ready"}}' };
      }
      if (argv[0] === "pane" && argv[1] === "read") {
        return { code: 0, stdout: "pi: terminal too narrow, exiting", stderr: "" };
      }
      return tabCreateOk(argv);
    });

    const res = await spawnOne(spawn);

    expect(res.details.failed[0]?.reason).toContain("agent_not_ready");
    expect(res.details.failed[0]?.reason).toContain("pi: terminal too narrow");
    expect(workerCloses(stub)).toHaveLength(1);
  });

  test("a capture that itself fails leaves the original failure reason intact", async () => {
    const { spawn, stub } = stubFor((argv) => {
      if (argv[0] === "agent" && argv[1] === "start") {
        return { code: 1, stdout: "", stderr: '{"error":{"code":"agent_not_ready"}}' };
      }
      if (argv[0] === "pane" && argv[1] === "read") {
        return { code: 1, stdout: "", stderr: "pane is gone" };
      }
      return tabCreateOk(argv);
    });

    const res = await spawnOne(spawn);

    // A capture problem never replaces the root cause.
    expect(res.details.failed[0]?.reason).toContain("agent_not_ready");
    expect(workerCloses(stub)).toHaveLength(1);
  });

  test("the pane capture is bounded, so one failure cannot flood the digest", async () => {
    const { spawn } = stubFor((argv) => {
      if (argv[0] === "agent" && argv[1] === "start") {
        return { code: 1, stdout: "", stderr: "agent_not_ready" };
      }
      if (argv[0] === "pane" && argv[1] === "read") {
        return { code: 0, stdout: "x".repeat(PANE_CAPTURE_CHARS * 3), stderr: "" };
      }
      return tabCreateOk(argv);
    });

    const res = await spawnOne(spawn);

    expect(res.details.failed[0]?.reason.length).toBeLessThan(PANE_CAPTURE_CHARS + 500);
  });

  // A herdr call that throws rather than exiting non-zero is still a failure
  // that happened after the pane existed. Left unhandled it lands in
  // allSettled's rejected branch, which files the failure against slug
  // "unknown" and leaks the pane -- the orphan panes that subdivide the
  // layout for every later round.
  test("a herdr call that throws is still captured, attributed, and its pane closed", async () => {
    const { spawn, stub } = stubFor(
      withPaneText((argv) => {
        if (isItemPrompt(argv)) throw new Error("herdr socket closed");
        return tabCreateOk(argv);
      }),
    );

    const res = await spawnOne(spawn);

    expect(res.details.failed[0]?.slug).toBe("some-item");
    expect(res.details.failed[0]?.reason).toContain("herdr socket closed");
    expect(res.details.failed[0]?.reason).toContain(PANE_TEXT);
    expect(workerCloses(stub)).toHaveLength(1);
  });

  // Cleanup that fails is still cleanup: the reason it was cleaning up after
  // has to survive it.
  /** Stubs dev_status.py's `ready` alongside a healthy tab create. */
  const withReady = (items: { id: string; paths?: string[] }[]) => (argv: string[]) =>
    argv[0] === "ready" || argv[1] === "ready" || argv.includes("ready")
      ? {
          code: 0,
          stdout: JSON.stringify(
            // `worker_safe: true` mirrors what dev_status.py's `ready` now
            // stamps on an ordinary item. Without it these fixtures describe a
            // payload no real `ready` produces, and every item would be
            // refused -- which is the guard working, not the bootstrap failing.
            items.map((i) => ({
              id: i.id,
              worker_safe: true,
              related_files: (i.paths ?? []).map((path) => ({ path })),
            })),
          ),
          stderr: "",
        }
      : tabCreateOk(argv);

  test("with a prefix and no items, it selects from the READY queue itself", async () => {
    const { spawn, stub } = stubFor(withReady([{ id: "meta-a" }, { id: "meta-b" }]));

    const res = (await spawn.execute(
      ...(["call-1", { runId: "selfsel", prefix: "meta-", concurrency: 2 }] as unknown as never[]),
    )) as { details: { spawned: { slug: string }[] } };

    expect(res.details.spawned.map((w) => w.slug)).toEqual(["meta-a", "meta-b"]);
    // and it asked dev_status rather than being told
    const ask = stub.calls.find((c) => c.argv.includes("ready"));
    expect(ask?.argv).toContain("--prefix");
    expect(ask?.argv).toContain("meta-");
  });

  test("two ready items editing the same file do not spawn together, and the loser is named in the text", async () => {
    const { spawn } = stubFor(
      withReady([
        { id: "meta-a", paths: ["/repo/pi/extensions/swarm-tool.ts"] },
        { id: "meta-b", paths: ["/repo/pi/extensions/swarm-tool.ts"] },
      ]),
    );

    const res = (await spawn.execute(
      ...(["call-1", { runId: "overlap", prefix: "meta-", concurrency: 3 }] as unknown as never[]),
    )) as {
      content: { text: string }[];
      details: { spawned: { slug: string }[]; deferred: { slug: string }[] };
    };

    expect(res.details.spawned.map((w) => w.slug)).toEqual(["meta-a"]);
    expect(res.details.deferred.map((d) => d.slug)).toEqual(["meta-b"]);
    // The orchestrator only ever reads `content`, so a deferral invisible
    // there is an item silently dropped from the run.
    const text = res.content.map((c) => c.text).join("\n");
    expect(text).toContain("meta-b");
    expect(text).toContain("deferred");
    expect(text).toContain("swarm-tool.ts");
  });

  // The second wave has to see what the first wave's workers claimed, which
  // is why the paths live on the worker record rather than being re-derived.
  test("a later wave defers a candidate that collides with a worker still running", async () => {
    // Concurrency 2, not 1, and the distinction is the whole test: at 1 the
    // second wave has zero headroom, so meta-b is skipped for the cap before
    // the overlap check ever runs -- an assertion that would pass with path
    // collision detection removed entirely.
    const { spawn } = stubFor(
      withReady([
        { id: "meta-a", paths: ["/repo/shared.ts"] },
        { id: "meta-b", paths: ["/repo/shared.ts"] },
      ]),
    );

    await spawn.execute(
      ...(["call-1", { runId: "wave", prefix: "meta-", concurrency: 2 }] as unknown as never[]),
    );
    const second = (await spawn.execute(
      ...(["call-2", { runId: "wave", prefix: "meta-", concurrency: 2 }] as unknown as never[]),
    )) as { details: { spawned: unknown[]; deferred: { slug: string }[]; skipped: string[] } };

    expect(second.details.spawned).toHaveLength(0);
    expect(second.details.skipped).toEqual([]);
    expect(second.details.deferred.map((d) => d.slug)).toEqual(["meta-b"]);
  });

  // THE RETRY LOOP, found on a live run rather than here: a worker whose tab
  // was closed left its item exactly as it found it -- READY, because it died
  // before reaching `dev_status.py start` -- so the very next wave selected
  // and spawned that same item again. Unbounded, and flatly against
  // swarm_poll's own promise that a failed worker is never silently retried.
  // The stub is what hid it: a fixed `ready` list cannot express an item
  // coming back, so this test drives the real sequence instead.
  test("an item whose worker died is not selected again by a later wave", async () => {
    const { spawn, stub } = stubFor(withReady([{ id: "meta-a" }, { id: "meta-b" }]));

    const first = (await spawn.execute(
      ...(["c1", { runId: "noretry", prefix: "meta-", concurrency: 1 }] as unknown as never[]),
    )) as { details: { spawned: { slug: string; agent: string }[] } };
    expect(first.details.spawned.map((w) => w.slug)).toEqual(["meta-a"]);

    // meta-a's worker dies. A fresh tool instance reloads the run from disk
    // and reconciles it against herdr's live agent list -- which the stub
    // answers empty -- so the dead worker is dropped exactly as it would be
    // after a restart. dev_status still reports meta-a as READY.
    const { spawn: spawn2, stub: stub2 } = stubFor(withReady([{ id: "meta-a" }, { id: "meta-b" }]));
    const second = (await spawn2.execute(
      ...(["c2", { runId: "noretry", prefix: "meta-", concurrency: 1 }] as unknown as never[]),
    )) as { details: { spawned: { slug: string }[] } };

    // meta-b, never meta-a again.
    expect(second.details.spawned.map((w) => w.slug)).toEqual(["meta-b"]);
    const label = (calls: ExecCall[]) =>
      calls
        .filter((c) => c.argv[0] === "tab" && c.argv[1] === "create")
        .map((c) => c.argv[c.argv.indexOf("--label") + 1]);
    expect(label(stub.calls)).toEqual(["meta-a"]);
    expect(label(stub2.calls)).toEqual(["meta-b"]);
  });

  test("naming an item explicitly still retries it -- that is the caller asking", async () => {
    // The guard is on automatic selection only. A human who names a slug
    // after reading the digest means it.
    const { spawn } = stubFor(withReady([{ id: "meta-a" }]));

    await spawn.execute(
      ...(["c1", { runId: "explicit", prefix: "meta-", concurrency: 1 }] as unknown as never[]),
    );
    const { spawn: spawn2 } = stubFor(withReady([{ id: "meta-a" }]));
    const again = (await spawn2.execute(
      ...(["c2", { runId: "explicit", items: ["meta-a"], concurrency: 1 }] as unknown as never[]),
    )) as { details: { spawned: { slug: string }[] } };

    expect(again.details.spawned.map((w) => w.slug)).toEqual(["meta-a"]);
  });

  test("a deferred item is not treated as attempted, so a later wave still takes it", async () => {
    // Deferring is not trying. An item held back for overlap was never handed
    // to anyone, and becoming schedulable later is the whole point.
    const { spawn } = stubFor(
      withReady([
        { id: "meta-a", paths: ["/repo/s.ts"] },
        { id: "meta-b", paths: ["/repo/s.ts"] },
      ]),
    );

    const first = (await spawn.execute(
      ...(["c1", { runId: "defnotatt", prefix: "meta-", concurrency: 3 }] as unknown as never[]),
    )) as { details: { deferred: { slug: string }[] } };
    expect(first.details.deferred.map((d) => d.slug)).toEqual(["meta-b"]);

    const { spawn: spawn2 } = stubFor(
      withReady([
        { id: "meta-a", paths: ["/repo/s.ts"] },
        { id: "meta-b", paths: ["/repo/s.ts"] },
      ]),
    );
    const second = (await spawn2.execute(
      ...(["c2", { runId: "defnotatt", prefix: "meta-", concurrency: 3 }] as unknown as never[]),
    )) as { details: { spawned: { slug: string }[] } };
    expect(second.details.spawned.map((w) => w.slug)).toEqual(["meta-b"]);
  });

  // A lock timeout or a broken dev_status.py yields zero candidates, which is
  // byte-identical to a drained queue: "Spawned 0 worker(s)". The orchestrator
  // would end the run and never report the items it silently left behind.
  test("a failed READY query stops the run instead of looking like an empty queue", async () => {
    const { spawn } = stubFor((argv) =>
      argv.includes("ready")
        ? { code: 1, stdout: "", stderr: "Traceback: could not acquire backlog lock" }
        : tabCreateOk(argv),
    );

    await expect(
      spawn.execute(
        ...(["c1", { runId: "readyfail", prefix: "meta-", concurrency: 2 }] as unknown as never[]),
      ),
    ).rejects.toThrow(/could not acquire backlog lock/);
  });

  test("a slug repeated in an explicit items list spawns one worker, not two", async () => {
    // Two workers on one item means two worktrees racing each other's commits.
    // Nothing else catches it: an item with no related_files collides with
    // nothing, itself included.
    const { spawn } = stubFor(withReady([{ id: "meta-a" }]));

    const res = (await spawn.execute(
      ...([
        "c1",
        { runId: "dupes", items: ["meta-a", "meta-a"], concurrency: 3 },
      ] as unknown as never[]),
    )) as { details: { spawned: { slug: string }[] } };

    expect(res.details.spawned.map((w) => w.slug)).toEqual(["meta-a"]);
  });

  test("neither items nor prefix is refused rather than swarming every project at once", async () => {
    const { spawn } = stubFor(tabCreateOk);

    await expect(
      spawn.execute(...(["call-1", { runId: "unscoped", concurrency: 2 }] as unknown as never[])),
    ).rejects.toThrow(/items.*prefix|prefix.*items/i);
  });

  // THE ORPHAN. `tab create` exiting 0 with output that will not parse has
  // already created a tab, and the id needed to close it was in exactly the
  // response that could not be read. Every other post-create failure goes
  // through failWithTab and is cleaned up; this one had nothing to clean up
  // with, and a live tab nobody polls is the leak that turned two failed runs
  // into six orphans on 2026-09-02.
  test("an unparseable tab create recovers the tab by its label and closes it", async () => {
    const { spawn, stub } = stubFor((argv) => {
      if (argv[0] === "tab" && argv[1] === "create") {
        return { code: 0, stdout: "{not json at all", stderr: "" };
      }
      if (argv[0] === "tab" && argv[1] === "list") {
        return {
          code: 0,
          stdout: JSON.stringify({
            result: {
              tabs: [
                { tab_id: "w1:t1", label: "1" },
                { tab_id: "w1:tOrphan", label: "some-item" },
              ],
            },
          }),
          stderr: "",
        };
      }
      return { code: 0, stdout: "", stderr: "" };
    });

    const res = await spawnOne(spawn);

    expect(res.details.failed[0]?.slug).toBe("some-item");
    expect(res.details.failed[0]?.reason).toContain("could not parse tab create response");
    expect(workerCloses(stub).map((c) => c.argv[2])).toEqual(["w1:tOrphan"]);
  });

  test("an ambiguous label closes nothing and says a tab needs closing by hand", async () => {
    // Two tabs carry the label, or none does. Guessing which to close risks
    // closing a tab the human opened, so the honest move is to name the leak
    // and leave it -- herdr's own guidance is not to close what you did not
    // create, and this path cannot prove which one that is.
    const { spawn, stub } = stubFor((argv) => {
      if (argv[0] === "tab" && argv[1] === "create") {
        return { code: 0, stdout: "{not json at all", stderr: "" };
      }
      if (argv[0] === "tab" && argv[1] === "list") {
        return {
          code: 0,
          stdout: JSON.stringify({
            result: {
              tabs: [
                { tab_id: "w1:tA", label: "some-item" },
                { tab_id: "w1:tB", label: "some-item" },
              ],
            },
          }),
          stderr: "",
        };
      }
      return { code: 0, stdout: "", stderr: "" };
    });

    const res = await spawnOne(spawn);

    expect(res.details.failed[0]?.reason).toContain("close it by hand");
    expect(workerCloses(stub)).toHaveLength(0);
  });

  test("a pane close that throws does not throw away the failure reason", async () => {
    const { spawn } = stubFor((argv) => {
      if (argv[0] === "agent" && argv[1] === "start") {
        return { code: 1, stdout: "", stderr: "agent_not_ready" };
      }
      if (argv[0] === "pane" && argv[1] === "read")
        return { code: 0, stdout: PANE_TEXT, stderr: "" };
      if (argv[0] === "tab" && argv[1] === "close") throw new Error("tab already gone");
      return tabCreateOk(argv);
    });

    const res = await spawnOne(spawn);

    expect(res.details.failed[0]?.slug).toBe("some-item");
    expect(res.details.failed[0]?.reason).toContain("agent_not_ready");
    expect(res.details.failed[0]?.reason).toContain(PANE_TEXT);
  });
});

describe("parseTabCreate", () => {
  // Verbatim shape of a real `herdr tab create --cwd ... --label ... --no-focus`
  // response, herdr 0.8.2, captured 2026-09-02. The two ids the swarm needs sit
  // in different objects: the pane to start an agent in, the tab to close later.
  const REAL_TAB_CREATE = JSON.stringify({
    id: "cli:tab:create",
    result: {
      root_pane: {
        agent_status: "unknown",
        cwd: "/home/yanil/dotfiles",
        focused: false,
        pane_id: "w1:p2W",
        revision: 0,
        scroll: { max_offset_from_bottom: 0, offset_from_bottom: 0, viewport_rows: 38 },
        tab_id: "w1:tN",
        terminal_id: "term_65a871a75f6c053",
        workspace_id: "w1",
      },
      tab: {
        agent_status: "unknown",
        focused: false,
        label: "tabexp",
        number: 21,
        pane_count: 1,
        tab_id: "w1:tN",
        workspace_id: "w1",
      },
      type: "tab_created",
    },
  });

  test("reads the root pane id and the tab id out of a real response", () => {
    expect(parseTabCreate(REAL_TAB_CREATE)).toEqual({ paneId: "w1:p2W", tabId: "w1:tN" });
  });

  test("returns undefined rather than a half-identified worker when either id is missing", () => {
    // A worker recorded with a pane but no tab can be started and never closed,
    // which is exactly the orphan-pane class this change is meant to end.
    const noTab = JSON.stringify({ result: { root_pane: { pane_id: "w1:p2W" } } });
    const noPane = JSON.stringify({ result: { tab: { tab_id: "w1:tN" } } });
    expect(parseTabCreate(noTab)).toBeUndefined();
    expect(parseTabCreate(noPane)).toBeUndefined();
  });

  test("returns undefined on unparseable output instead of throwing", () => {
    expect(parseTabCreate("not json")).toBeUndefined();
    expect(parseTabCreate("{}")).toBeUndefined();
    expect(parseTabCreate("")).toBeUndefined();
  });
});

describe("buildTabListArgv / findTabByLabel", () => {
  const listing = (labels: [string, string][]) =>
    JSON.stringify({ result: { tabs: labels.map(([tab_id, label]) => ({ tab_id, label })) } });

  test("tab list takes no arguments", () => {
    expect(buildTabListArgv()).toEqual(["tab", "list"]);
  });

  test("finds a tab when exactly one carries the label", () => {
    expect(
      findTabByLabel(
        listing([
          ["w1:t1", "1"],
          ["w1:tX", "my-slug"],
        ]),
        "my-slug",
      ),
    ).toBe("w1:tX");
  });

  test("refuses to guess when the label is ambiguous or absent", () => {
    // Closing the wrong tab is worse than reporting a leak, and a duplicate
    // label cannot say which one this spawn created.
    expect(
      findTabByLabel(
        listing([
          ["w1:tA", "dup"],
          ["w1:tB", "dup"],
        ]),
        "dup",
      ),
    ).toBeUndefined();
    expect(findTabByLabel(listing([["w1:t1", "1"]]), "missing")).toBeUndefined();
  });

  test("returns undefined on unparseable output instead of throwing", () => {
    expect(findTabByLabel("not json", "x")).toBeUndefined();
    expect(findTabByLabel("{}", "x")).toBeUndefined();
  });
});

describe("swarm_resolve_blocked execute() wiring", () => {
  let dir: string;
  let priorStateDir: string | undefined;

  beforeEach(() => {
    dir = mkdtempSync(join(tmpdir(), "swarm-resolve-exec-"));
    priorStateDir = process.env.PI_SWARM_STATE_DIR;
    process.env.PI_SWARM_STATE_DIR = dir;
  });

  afterEach(() => {
    if (priorStateDir === undefined) delete process.env.PI_SWARM_STATE_DIR;
    else process.env.PI_SWARM_STATE_DIR = priorStateDir;
    rmSync(dir, { recursive: true, force: true });
  });

  const RUN = "resolverun";
  const AGENT = "resolverun-w1";
  const PANE = "w1:pZ";
  const TAB = "w1:tZ";

  /**
   * Stands in for herdr's own `agent wait`: returns the agent once it reaches
   * a state the caller actually asked for, and a real timeout envelope when
   * the requested set never matches. Modelling the deadline this way is the
   * whole point -- the bug is that the requested set omitted the state the
   * worker actually reaches.
   */
  const waitLike =
    (reachedStatus: string) =>
    (argv: string[]): { code: number; stdout: string; stderr: string } => {
      const until = argv.filter((a, i) => argv[i - 1] === "--until");
      if (until.includes(reachedStatus)) {
        return { code: 0, stdout: realWaitEnvelope(reachedStatus, AGENT, PANE), stderr: "" };
      }
      return {
        code: 1,
        stdout: "",
        stderr: JSON.stringify({ error: { code: "timeout", message: "deadline elapsed" } }),
      };
    };

  /** Seeds one worker parked at awaiting_relay -- the state swarm_poll leaves a blocked worker in. */
  function setup(
    respond: (argv: string[]) => { code: number; stdout: string; stderr: string } | undefined,
  ) {
    const worker: WorkerRecord = {
      agent: AGENT,
      slug: "some-item",
      paneId: PANE,
      tabId: TAB,
      lifecycle: "awaiting_relay",
    };
    saveState({ runId: RUN, concurrency: 2, nextCounter: 1, workers: [worker] }, dir);

    const stub = makeStubPi((argv) => {
      const override = respond(argv);
      if (override) return override;
      const [a, b] = argv;
      if (a === "agent" && b === "list") {
        // getOrInitState reconciles against this; an empty list would drop the
        // seeded worker before the code under test ever runs. Real wire shape
        // (see realAgentListEnvelope): the named worker alongside an unnamed
        // foreign agent the parse must ignore.
        return {
          code: 0,
          stdout: realAgentListEnvelope(UNNAMED_CLAUDE_ENTRY, {
            agent: "pi",
            agent_status: "idle",
            name: AGENT,
            pane_id: PANE,
          }),
          stderr: "",
        };
      }
      if (a === "agent" && b === "read") {
        // Numbered lines: parsePicker reads "> 1. OK" shapes, so an
        // unnumbered list parses to zero options and short-circuits to
        // needsManual before any of this reaches the verify.
        return { code: 0, stdout: REAL_PICKER_OUTPUT, stderr: "" };
      }
      if (a === "agent" && b === "get") {
        return { code: 0, stdout: realWaitEnvelope("blocked", AGENT, PANE), stderr: "" };
      }
      return { code: 0, stdout: "", stderr: "" };
    });

    registerSwarmTools(stub.pi as unknown as Parameters<typeof registerSwarmTools>[0]);
    const resolve = stub.tools.get("swarm_resolve_blocked");
    if (!resolve) throw new Error("swarm_resolve_blocked was never registered");
    return { resolve, stub };
  }

  const run = (resolve: { execute: (...a: never[]) => Promise<unknown> }, answer = "OK") =>
    resolve.execute(
      ...(["call-1", { runId: RUN, agent: AGENT, answer }, undefined] as unknown as never[]),
    ) as Promise<{
      content: { type: string; text: string }[];
      details: { relayFailed: boolean; needsManual: boolean };
    }>;

  // Every outcome the promptGuidelines tell an orchestrator to branch on has
  // to be legible in `content`. Verified against pi 0.84's own plumbing: no
  // provider adapter in @earendil-works/pi-ai reads a tool result's `details`
  // at all -- openai-completions builds its wire message from `content` text
  // and image blocks only, and no other adapter references the field. So
  // `details` is a session/UI record, and naming its fields in guidance tells
  // the model to read something it never receives.
  describe("outcome markers reach content, not just details", () => {
    const textOf = (res: { content: { text: string }[] }) =>
      res.content.map((c) => c.text).join("\n");

    test("an unmatched answer is marked needs_manual and names the pane", async () => {
      const { resolve } = setup(() => undefined);

      const res = await run(resolve, "definitely not a listed option");

      expect(res.details.needsManual).toBe(true);
      expect(textOf(res)).toContain("needs_manual:");
      // The guideline says to relay which pane; paneId was only in details.
      expect(textOf(res)).toContain(PANE);
    });

    test("a pane identity mismatch is marked needs_manual and names the pane", async () => {
      const { resolve } = setup((argv) =>
        argv[0] === "agent" && argv[1] === "get"
          ? { code: 0, stdout: realWaitEnvelope("blocked", AGENT, "w1:pWRONG"), stderr: "" }
          : undefined,
      );

      const res = await run(resolve);

      expect(res.details.needsManual).toBe(true);
      expect(textOf(res)).toContain("needs_manual:");
      expect(textOf(res)).toContain(PANE);
    });

    test("an untracked agent is marked relay_failed", async () => {
      const { resolve } = setup(() => undefined);

      const res = (await resolve.execute(
        ...([
          "call-1",
          { runId: RUN, agent: "no-such-agent", answer: "OK" },
          undefined,
        ] as unknown as never[]),
      )) as { content: { text: string }[]; details: { relayFailed: boolean } };

      expect(res.details.relayFailed).toBe(true);
      expect(textOf(res)).toContain("relay_failed:");
    });

    test("a successful relay is marked resolved and names the pane", async () => {
      const { resolve } = setup((argv) =>
        argv[0] === "agent" && argv[1] === "wait" ? waitLike("working")(argv) : undefined,
      );

      const res = await run(resolve);

      expect(res.details.relayFailed).toBe(false);
      expect(textOf(res)).toContain("resolved:");
      expect(textOf(res)).toContain(PANE);
    });
  });

  const workerCloses = (stub: { calls: ExecCall[] }) =>
    stub.calls.filter((c) => c.argv[0] === "tab" && c.argv[1] === "close");

  // THE REGRESSION. A worker that answers correctly resumes its turn and
  // enters `working`. Pre-fix the verify asked only for idle/done/blocked, so
  // herdr waited out the full window and returned a timeout, and the tool
  // reported relay_failed on what is in fact the normal success path.
  test("a worker that answers and resumes into working is resolved, not relay_failed", async () => {
    const { resolve, stub } = setup((argv) =>
      argv[0] === "agent" && argv[1] === "wait" ? waitLike("working")(argv) : undefined,
    );

    const res = await run(resolve);

    expect(res.details.relayFailed).toBe(false);
    expect(res.content[0]?.text).toContain("back in the active pool");
    expect(workerCloses(stub)).toHaveLength(0);
    const persisted = loadState(RUN, dir);
    expect(persisted?.workers[0]?.lifecycle).toBe("active");
  });

  // An answer that ends the worker's turn outright must still pass -- the
  // point is to widen the accepted set, not to swap one narrow set for another.
  test("a worker that goes straight to idle is still resolved", async () => {
    const { resolve } = setup((argv) =>
      argv[0] === "agent" && argv[1] === "wait" ? waitLike("idle")(argv) : undefined,
    );

    const res = await run(resolve);

    expect(res.details.relayFailed).toBe(false);
    expect(loadState(RUN, dir)?.workers[0]?.lifecycle).toBe("active");
  });

  // A worker that never answers reaches none of the requested states, so the
  // wait times out -- that timeout IS the failure signal now.
  test("a worker still blocked after the answer is a genuine relay_failed", async () => {
    const { resolve, stub } = setup((argv) =>
      argv[0] === "agent" && argv[1] === "wait" ? waitLike("blocked")(argv) : undefined,
    );

    const res = await run(resolve);

    expect(res.details.relayFailed).toBe(true);
    // Dropping the worker from state without closing its pane leaves a live
    // pi in an orphan pane that nothing will ever poll or clean up.
    expect(workerCloses(stub)).toHaveLength(1);
    expect(loadState(RUN, dir)?.workers).toHaveLength(0);
  });

  // THE RACE, pinned. Measured against real herdr 0.8.2: after send-keys the
  // status still reads `blocked` for ~90-156ms, which is longer than the one
  // herdr call between send-keys and the verify. Asking for `blocked` made
  // herdr match it in ~2ms and fail a worker that had in fact answered, so
  // the requested set must not contain it at all.
  test("never asks herdr for blocked, so the post-answer race cannot fail a good relay", async () => {
    const { resolve, stub } = setup((argv) =>
      argv[0] === "agent" && argv[1] === "wait" ? waitLike("working")(argv) : undefined,
    );

    await run(resolve);

    const waitCall = stub.calls.find((c) => c.argv[0] === "agent" && c.argv[1] === "wait");
    const until = waitCall?.argv.filter((a, i) => waitCall.argv[i - 1] === "--until") ?? [];
    expect(until).not.toContain("blocked");
    expect(until).toContain("working");
    expect(until).toContain("idle");
    expect(until).toContain("done");
  });

  test("a send-keys failure closes the pane instead of leaking it", async () => {
    const { resolve, stub } = setup((argv) =>
      argv[0] === "agent" && argv[1] === "send-keys"
        ? { code: 1, stdout: "", stderr: "send-keys refused" }
        : undefined,
    );

    const res = await run(resolve);

    expect(res.details.relayFailed).toBe(true);
    expect(workerCloses(stub)).toHaveLength(1);
    expect(loadState(RUN, dir)?.workers).toHaveLength(0);
  });

  test("a pane close that fails does not mask the relay failure", async () => {
    const { resolve } = setup((argv) => {
      if (argv[0] === "agent" && argv[1] === "wait") return waitLike("blocked")(argv);
      if (argv[0] === "tab" && argv[1] === "close") throw new Error("tab already gone");
      return undefined;
    });

    const res = await run(resolve);

    expect(res.details.relayFailed).toBe(true);
    expect(res.content[0]?.text).toContain("did not resume");
  });
});

describe("swarm_poll and workers parked at awaiting_relay", () => {
  let dir: string;
  let priorStateDir: string | undefined;

  beforeEach(() => {
    dir = mkdtempSync(join(tmpdir(), "swarm-poll-relay-"));
    priorStateDir = process.env.PI_SWARM_STATE_DIR;
    process.env.PI_SWARM_STATE_DIR = dir;
  });

  afterEach(() => {
    if (priorStateDir === undefined) delete process.env.PI_SWARM_STATE_DIR;
    else process.env.PI_SWARM_STATE_DIR = priorStateDir;
    rmSync(dir, { recursive: true, force: true });
  });

  // swarm_poll counted only `active`, so a worker parked at awaiting_relay --
  // a live pi holding an open pane, waiting on the orchestrator -- read as an
  // empty pool. The run ended reporting nothing left to do while that worker
  // and its pane were still there.
  test("says the worker is awaiting a relay rather than reporting an empty pool", async () => {
    const runId = "relayrun";
    saveState(
      {
        runId,
        concurrency: 2,
        nextCounter: 1,
        workers: [
          {
            agent: "relayrun-w1",
            slug: "stuck-item",
            paneId: "w1:pQ",
            tabId: "w1:tQ",
            lifecycle: "awaiting_relay",
          },
        ],
      },
      dir,
    );

    const stub = makeStubPi((argv) =>
      argv[0] === "agent" && argv[1] === "list"
        ? {
            code: 0,
            // reconcileState prunes against this; an empty list would drop the
            // parked worker before the emptiness check ever sees it. Real wire
            // shape (see realAgentListEnvelope): the named worker alongside an
            // unnamed foreign agent the parse must ignore.
            stdout: realAgentListEnvelope(UNNAMED_CLAUDE_ENTRY, {
              agent: "pi",
              agent_status: "idle",
              name: "relayrun-w1",
              pane_id: "w1:pA",
            }),
            stderr: "",
          }
        : { code: 0, stdout: "", stderr: "" },
    );
    registerSwarmTools(stub.pi as unknown as Parameters<typeof registerSwarmTools>[0]);
    const poll = stub.tools.get("swarm_poll");
    if (!poll) throw new Error("swarm_poll was never registered");

    const res = (await poll.execute(
      ...(["call-1", { runId, timeoutMs: 1000 }, undefined] as unknown as never[]),
    )) as { content: { type: string; text: string }[] };

    const text = res.content.map((c) => c.text).join("\n");
    expect(text).not.toBe("No active workers to poll.");
    expect(text).toContain("relayrun-w1");
    expect(text).toContain("swarm_resolve_blocked");
  });
});

// ---------------------------------------------------------------------------
// swarm_poll's blocking wait: abort, and the drain race.
//
// When nothing is queued, swarm_poll parks on a promise that only a worker's
// `agent wait` can settle -- 30 minutes by default. Two things went wrong
// there. The tool call's abort signal was passed to every herdr call AFTER
// the wait but not to the wait itself, so an aborted poll never returned and
// its resolver stayed in the run's waiter list forever. And when an event
// finally arrived it woke EVERY queued waiter at once, while the first to run
// drained the queue with splice(0) -- so any concurrent poll woke to an empty
// queue and reported "no events" for a run that was in fact making progress.
// ---------------------------------------------------------------------------

describe("swarm_poll blocking wait", () => {
  let dir: string;
  let priorStateDir: string | undefined;

  beforeEach(() => {
    dir = mkdtempSync(join(tmpdir(), "swarm-poll-wait-"));
    priorStateDir = process.env.PI_SWARM_STATE_DIR;
    process.env.PI_SWARM_STATE_DIR = dir;
  });

  afterEach(() => {
    if (priorStateDir === undefined) delete process.env.PI_SWARM_STATE_DIR;
    else process.env.PI_SWARM_STATE_DIR = priorStateDir;
    rmSync(dir, { recursive: true, force: true });
  });

  /** A stub whose `agent wait` answers only when the test says so. */
  function setup(runId: string, agents: string[]) {
    const workers = agents.map((agent, i) => ({
      agent,
      slug: `item-${i}`,
      paneId: `w1:p${i}`,
      tabId: `w1:t${i}`,
      lifecycle: "active" as const,
    }));
    saveState({ runId, concurrency: agents.length, nextCounter: agents.length, workers }, dir);

    const settle = new Map<string, (r: { code: number; stdout: string; stderr: string }) => void>();
    const calls: ExecCall[] = [];
    const tools = new Map<string, { execute: (...a: never[]) => Promise<unknown> }>();
    const pi = {
      exec(_cmd: string, argv: string[]) {
        calls.push({ argv });
        if (argv[0] === "agent" && argv[1] === "list") {
          return Promise.resolve({
            code: 0,
            stdout: JSON.stringify({
              result: {
                agents: workers.map((w) => ({
                  agent: "pi",
                  agent_status: "working",
                  name: w.agent,
                  pane_id: w.paneId,
                })),
              },
            }),
            stderr: "",
          });
        }
        if (argv[0] === "agent" && argv[1] === "wait") {
          // Held open until the test resolves it, the way a real worker's
          // 30-minute wait is held open until that worker settles.
          return new Promise((resolve) => settle.set(argv[2]!, resolve));
        }
        return Promise.resolve({ code: 0, stdout: "", stderr: "" });
      },
      registerTool(def: { name: string; execute: (...a: never[]) => Promise<unknown> }) {
        tools.set(def.name, def);
      },
    };
    registerSwarmTools(pi as unknown as Parameters<typeof registerSwarmTools>[0]);
    const poll = tools.get("swarm_poll");
    if (!poll) throw new Error("swarm_poll was never registered");

    const finish = async (agent: string, status: string) => {
      // The stub's wait promise is only created once armWait has run, which
      // happens inside the poll call after its own awaits, so wait for the
      // registration rather than assuming a fixed number of microtasks.
      for (let i = 0; i < 200 && !settle.has(agent); i++) {
        await new Promise((r) => setTimeout(r, 5));
      }
      if (!settle.has(agent)) throw new Error(`no wait ever armed for ${agent}`);
      settle.get(agent)!({
        code: 0,
        stdout: realWaitEnvelope(status, agent, "w1:p0"),
        stderr: "",
      });
    };

    return { poll, calls, finish };
  }

  const runPoll = (
    poll: { execute: (...a: never[]) => Promise<unknown> },
    runId: string,
    signal?: AbortSignal,
  ) =>
    poll.execute(
      ...(["call", { runId, timeoutMs: 1000 }, signal] as unknown as never[]),
    ) as Promise<{ content: { text: string }[]; details: { events: { kind: string }[] } }>;

  test("an aborted poll settles instead of hanging on a promise nothing resolves", async () => {
    const { poll } = setup("abortrun", ["abortrun-w1"]);
    const controller = new AbortController();

    const pending = runPoll(poll, "abortrun", controller.signal);
    // Let the poll reach its wait before aborting, so this exercises the
    // blocking path rather than the already-aborted shortcut.
    await Promise.resolve();
    controller.abort();

    const res = await Promise.race([
      pending,
      new Promise<"hung">((r) => setTimeout(() => r("hung"), 2000)),
    ]);

    expect(res).not.toBe("hung");
    expect((res as { details: { events: unknown[] } }).details.events).toEqual([]);
    expect((res as { content: { text: string }[] }).content[0]?.text).toContain("aborted");
  });

  test("a poll already aborted before it waits returns rather than parking", async () => {
    const { poll } = setup("prerun", ["prerun-w1"]);
    const controller = new AbortController();
    controller.abort();

    const res = await Promise.race([
      runPoll(poll, "prerun", controller.signal),
      new Promise<"hung">((r) => setTimeout(() => r("hung"), 2000)),
    ]);

    expect(res).not.toBe("hung");
  });

  // THE DRAIN RACE. Both polls wake on the first event; whichever loses the
  // splice(0) must go back to waiting, not report an empty run.
  test("a poll that loses the drain race waits for the next event instead of reporting none", async () => {
    const { poll, finish } = setup("racerun", ["racerun-w1", "racerun-w2"]);

    const first = runPoll(poll, "racerun");
    const second = runPoll(poll, "racerun");

    await finish("racerun-w1", "idle");
    await finish("racerun-w2", "idle");

    const results = await Promise.race([
      Promise.all([first, second]),
      new Promise<"hung">((r) => setTimeout(() => r("hung"), 3000)),
    ]);

    expect(results).not.toBe("hung");
    const [a, b] = results as { details: { events: { kind: string }[] } }[];
    // Two events, two polls, one each -- and crucially neither poll reports
    // an empty list while the run still had an event coming.
    expect(a!.details.events.length + b!.details.events.length).toBe(2);
    expect(a!.details.events).not.toEqual([]);
    expect(b!.details.events).not.toEqual([]);
  });
});

// ---------------------------------------------------------------------------
// Scheduling: which items may run together, and what tops the pool back up.
//
// Two items that edit the same file cannot run concurrently -- their worktrees
// diverge and the second merge conflicts. That collision is not hypothetical:
// meta-swarm-trust-ack-fail-open and meta-swarm-poll-abort-and-orphan-pane
// both edit swarm-tool.ts and had to be held out of the same batch by hand.
// The signal was already in every item's related_files; nothing read it.
//
// READY is computed, not stored -- an item becomes ready the moment its last
// blocker is approved -- so a fixed items[] taken at call time goes stale as
// soon as a worker finishes. dev_status.py's `ready` reports the bucket it
// already builds for the dashboard, so the blocker walk is never reimplemented
// here.
// ---------------------------------------------------------------------------

describe("parseReadyItems / itemPaths", () => {
  test("reads the id and related_files of each ready item", () => {
    const stdout = JSON.stringify([
      { id: "a", related_files: [{ path: "/repo/x.ts" }, { path: "/repo/y.ts" }] },
      { id: "b", related_files: [] },
    ]);
    const items = parseReadyItems(stdout);
    expect(items.map((i) => i.id)).toEqual(["a", "b"]);
    expect(itemPaths(items[0]!)).toEqual(["/repo/x.ts", "/repo/y.ts"]);
    expect(itemPaths(items[1]!)).toEqual([]);
  });

  test("an item with no related_files has no paths, rather than throwing", () => {
    // Plenty of real items carry related_files: [] or omit it entirely. Such
    // an item constrains nothing and is constrained by nothing.
    expect(itemPaths({ id: "a" })).toEqual([]);
    expect(itemPaths({ id: "a", related_files: [{}] })).toEqual([]);
  });

  test("returns an empty list on unparseable output instead of throwing", () => {
    expect(parseReadyItems("not json")).toEqual([]);
    expect(parseReadyItems("{}")).toEqual([]);
    expect(parseReadyItems("")).toEqual([]);
  });
});

describe("selectSchedulable", () => {
  // `worker_safe: true` is what dev_status.py's `ready` now stamps on an
  // ordinary item. The helper carries it so these tests keep describing a
  // normal queue; the eligibility block below covers its absence.
  const item = (id: string, ...paths: string[]) => ({
    id,
    worker_safe: true,
    related_files: paths.map((path) => ({ path })),
  });

  test("two items touching the same file do not go into one wave", () => {
    const res = selectSchedulable(
      [item("a", "/r/shared.ts"), item("b", "/r/shared.ts"), item("c", "/r/other.ts")],
      [],
      3,
    );
    expect(res.slugs).toEqual(["a", "c"]);
    expect(res.deferred.map((d) => d.slug)).toEqual(["b"]);
    expect(res.deferred[0]?.reason).toContain("/r/shared.ts");
  });

  test("an item overlapping a worker already running is deferred too", () => {
    const res = selectSchedulable([item("a", "/r/live.ts")], ["/r/live.ts"], 3);
    expect(res.slugs).toEqual([]);
    expect(res.deferred.map((d) => d.slug)).toEqual(["a"]);
  });

  test("a directory and a file inside it count as overlapping", () => {
    // An item scoped to a whole module collides with one scoped to a file in
    // it, even though the two strings differ.
    const res = selectSchedulable([item("a", "/r/pkg"), item("b", "/r/pkg/deep/file.ts")], [], 3);
    expect(res.slugs).toEqual(["a"]);
    expect(res.deferred.map((d) => d.slug)).toEqual(["b"]);
  });

  test("a directory written with a trailing slash still contains its files", () => {
    // "/r/pkg/" + "/" builds "/r/pkg//", which nothing inside it starts with.
    const res = selectSchedulable([item("a", "/r/pkg/"), item("b", "/r/pkg/deep/f.ts")], [], 3);
    expect(res.slugs).toEqual(["a"]);
    expect(res.deferred.map((d) => d.slug)).toEqual(["b"]);
  });

  test("a path that merely shares a name prefix is not an overlap", () => {
    // "/r/pkg" must not swallow "/r/pkg-other" -- that is string prefixing,
    // not containment, and it would defer unrelated work forever.
    const res = selectSchedulable([item("a", "/r/pkg"), item("b", "/r/pkg-other/f.ts")], [], 3);
    expect(res.slugs).toEqual(["a", "b"]);
    expect(res.deferred).toEqual([]);
  });

  test("items past the headroom are skipped for the cap, not deferred for overlap", () => {
    // The two are different facts and the orchestrator acts differently on
    // them: a capped item is coming next wave regardless, a deferred one is
    // waiting on a specific worker to finish.
    const res = selectSchedulable([item("a", "/r/1"), item("b", "/r/2"), item("c", "/r/3")], [], 2);
    expect(res.slugs).toEqual(["a", "b"]);
    expect(res.skipped).toEqual(["c"]);
    expect(res.deferred).toEqual([]);
  });

  test("items with no related_files never block each other", () => {
    const res = selectSchedulable([item("a"), item("b"), item("c")], ["/r/live.ts"], 3);
    expect(res.slugs).toEqual(["a", "b", "c"]);
  });

  // TERMINATION. A deferred item must always become schedulable eventually,
  // or a top-up loop spins forever on a queue it can never drain.
  test("with nothing running, the first candidate is always schedulable", () => {
    const res = selectSchedulable([item("a", "/r/s.ts"), item("b", "/r/s.ts")], [], 3);
    expect(res.slugs).toEqual(["a"]);
    // ...and once a finishes, b has nothing left to collide with.
    const next = selectSchedulable([item("b", "/r/s.ts")], [], 3);
    expect(next.slugs).toEqual(["b"]);
  });

  test("zero headroom selects nothing and defers nothing", () => {
    const res = selectSchedulable([item("a", "/r/1")], [], 0);
    expect(res.slugs).toEqual([]);
    expect(res.deferred).toEqual([]);
    expect(res.skipped).toEqual(["a"]);
  });
});

// ---------------------------------------------------------------------------
// An elapsed `herdr agent wait` is a check-in, not a death certificate.
//
// The live failure: a worker several minutes into a real item -- item
// claimed, worktree created, spec written -- had its tab closed and was
// dropped from state the moment swarm_poll's wait deadline elapsed. It was
// not stuck. `agent wait` is a wait deadline, not a liveness check, so
// "nothing settled in the window" is exactly what a healthy worker doing
// several minutes of work looks like.
//
// These drive the registered tool's real execute() against a stubbed
// ExtensionAPI whose `agent wait` answers differently on each call, which is
// what lets a re-arm be observed at all.
// ---------------------------------------------------------------------------

/** herdr's own timeout envelope: JSON on stderr, nonzero exit (confirmed live). */
const HERDR_TIMEOUT_STDERR = JSON.stringify({
  error: { code: "timeout", message: "timed out waiting for agent status" },
  id: "cli:agent:wait",
});

describe("swarm_poll: an elapsed wait is a check-in, not a death certificate", () => {
  let dir: string;
  let priorStateDir: string | undefined;

  beforeEach(() => {
    dir = mkdtempSync(join(tmpdir(), "swarm-checkin-"));
    priorStateDir = process.env.PI_SWARM_STATE_DIR;
    process.env.PI_SWARM_STATE_DIR = dir;
  });

  afterEach(() => {
    if (priorStateDir === undefined) delete process.env.PI_SWARM_STATE_DIR;
    else process.env.PI_SWARM_STATE_DIR = priorStateDir;
    rmSync(dir, { recursive: true, force: true });
  });

  type Result = { code: number; stdout: string; stderr: string };

  /**
   * Seeds one active worker plus a QUEUE of `agent wait` results, one per
   * call, so a re-arm is observable: the whole point is that the second call
   * happens at all.
   */
  function setupQueued(waits: Result[], probe: Result, overrides: Partial<WorkerRecord> = {}) {
    const runId = "checkin";
    const worker: WorkerRecord = {
      agent: "checkin-w1",
      slug: "some-item",
      paneId: "w1:pZ",
      tabId: "w1:tZ",
      cwd: "/home/yanil/dotfiles",
      workingSinceMs: Date.now(),
      lifecycle: "active",
      ...overrides,
    };
    const state: SwarmState = { runId, concurrency: 2, nextCounter: 1, workers: [worker] };
    saveState(state, dir);

    let waitCall = 0;
    const stub = makeStubPi((argv) => {
      const [a, b] = argv;
      if (a === "agent" && b === "list") {
        return {
          code: 0,
          stdout: realAgentListEnvelope({
            agent: "pi",
            agent_status: "working",
            name: "checkin-w1",
            pane_id: "w1:pZ",
          }),
          stderr: "",
        };
      }
      if (a === "agent" && b === "wait") {
        const next = waits[Math.min(waitCall, waits.length - 1)];
        waitCall += 1;
        return next as Result;
      }
      if (a === "agent" && b === "get") return probe;
      if (a === "agent" && b === "read") {
        return { code: 0, stdout: "Commit these changes?\n> Yes\n  No\n", stderr: "" };
      }
      return { code: 0, stdout: "", stderr: "" };
    });

    registerSwarmTools(stub.pi as unknown as Parameters<typeof registerSwarmTools>[0]);
    const poll = stub.tools.get("swarm_poll");
    if (!poll) throw new Error("swarm_poll was never registered");
    return { runId, poll, stub, waitCalls: () => waitCall };
  }

  const closes = (stub: { calls: { argv: string[] }[] }) =>
    stub.calls.filter((c) => c.argv[0] === "tab" && c.argv[1] === "close");

  test("a still-working worker is re-armed and reported, never closed", async () => {
    const { runId, poll, stub, waitCalls } = setupQueued(
      [
        { code: 1, stdout: "", stderr: HERDR_TIMEOUT_STDERR },
        { code: 0, stdout: realWaitEnvelope("idle", "checkin-w1", "w1:pZ"), stderr: "" },
      ],
      { code: 0, stdout: realWaitEnvelope("working", "checkin-w1", "w1:pZ"), stderr: "" },
    );

    const res = (await poll.execute(
      ...([
        "call-1",
        { runId, timeoutMs: 1000, workerDeadlineMs: 4 * 60 * 60 * 1000 },
        undefined,
      ] as unknown as never[]),
    )) as { details: { events: { kind: string }[] } };

    // The load-bearing assertions: the worker survives its own wait deadline.
    expect(res.details.events.map((e) => e.kind)).toEqual(["still_working"]);
    expect(closes(stub)).toHaveLength(0);
    const persisted = loadState(runId, dir);
    expect(persisted?.workers).toHaveLength(1);
    expect(persisted?.workers[0]?.lifecycle).toBe("active");
    // A second wait was armed -- without it the worker is alive but unwatched.
    expect(waitCalls()).toBe(2);
  });

  test("the check-in carries its number and elapsed working time, not a bare label", async () => {
    const { runId, poll } = setupQueued(
      [
        { code: 1, stdout: "", stderr: HERDR_TIMEOUT_STDERR },
        { code: 0, stdout: realWaitEnvelope("idle", "checkin-w1", "w1:pZ"), stderr: "" },
      ],
      { code: 0, stdout: realWaitEnvelope("working", "checkin-w1", "w1:pZ"), stderr: "" },
      { workingSinceMs: Date.now() - 90 * 60 * 1000, checkIns: 3 },
    );

    const res = (await poll.execute(
      ...([
        "call-1",
        { runId, timeoutMs: 1000, workerDeadlineMs: 4 * 60 * 60 * 1000 },
        undefined,
      ] as unknown as never[]),
    )) as { content: { text: string }[]; details: { events: { checkIn?: number }[] } };

    // "still working" eight times says nothing; "check-in 4, 1h30m of a 4h
    // budget" is the thing a human can act on before the budget stops it.
    expect(res.details.events[0]?.checkIn).toBe(4);
    expect(res.content[0]?.text).toContain("check-in 4");
    expect(res.content[0]?.text).toContain("1h30m");
    // Persisted, so a restart does not report "check-in 1" against hours.
    expect(loadState(runId, dir)?.workers[0]?.checkIns).toBe(4);
  });

  test("past its budget, a confirmed-live worker IS stopped -- and the report can be acted on", async () => {
    const { runId, poll, stub } = setupQueued(
      [{ code: 1, stdout: "", stderr: HERDR_TIMEOUT_STDERR }],
      { code: 0, stdout: realWaitEnvelope("working", "checkin-w1", "w1:pZ"), stderr: "" },
      { workingSinceMs: Date.now() - 5 * 60 * 60 * 1000 },
    );

    const res = (await poll.execute(
      ...([
        "call-1",
        { runId, timeoutMs: 1000, workerDeadlineMs: 4 * 60 * 60 * 1000 },
        undefined,
      ] as unknown as never[]),
    )) as { details: { events: { kind: string; detail?: string }[] } };

    expect(res.details.events.map((e) => e.kind)).toEqual(["timed_out"]);
    expect(closes(stub)).toHaveLength(1);
    expect(loadState(runId, dir)?.workers).toHaveLength(0);
    // The four things that had to be cleaned up by hand after the live run.
    const detail = res.details.events[0]?.detail ?? "";
    expect(detail).toContain("/home/yanil/dotfiles-some-item");
    expect(detail).toContain("in-progress with a live claim");
    expect(detail).toContain("still reported working");
  });

  test("a stop whose probe never answered says so, instead of claiming the worker was working", async () => {
    // pi.exec resolves on abort with a coerced exit 0 and empty stdout, which
    // is what an abandoned probe looks like from the result alone.
    const { runId, poll, stub } = setupQueued(
      [{ code: 1, stdout: "", stderr: HERDR_TIMEOUT_STDERR }],
      { code: 1, stdout: "", stderr: '{"error":{"code":"internal","message":"herdr is down"}}' },
      { workingSinceMs: Date.now() - 5 * 60 * 60 * 1000 },
    );

    const res = (await poll.execute(
      ...([
        "call-1",
        { runId, timeoutMs: 1000, workerDeadlineMs: 4 * 60 * 60 * 1000 },
        undefined,
      ] as unknown as never[]),
    )) as { details: { events: { kind: string; detail?: string }[] } };

    expect(res.details.events.map((e) => e.kind)).toEqual(["timed_out"]);
    expect(closes(stub)).toHaveLength(1);
    const detail = res.details.events[0]?.detail ?? "";
    expect(detail).toContain("could NOT be verified");
    expect(detail).toContain("herdr is down");
    expect(detail).not.toContain("still reported working");
  });

  test("an agent herdr no longer knows is closed as error, not mislabelled a timeout", async () => {
    const { runId, poll, stub } = setupQueued(
      [{ code: 1, stdout: "", stderr: HERDR_TIMEOUT_STDERR }],
      {
        code: 1,
        stdout: "",
        stderr: JSON.stringify({
          error: { code: "agent_not_found", message: "agent target checkin-w1 not found" },
        }),
      },
    );

    const res = (await poll.execute(
      ...(["call-1", { runId, timeoutMs: 1000 }, undefined] as unknown as never[]),
    )) as { details: { events: { kind: string; detail?: string }[] } };

    expect(res.details.events.map((e) => e.kind)).toEqual(["error"]);
    expect(closes(stub)).toHaveLength(1);
    // The PROBE's reason, not the wait's -- the wait only ever said "timeout",
    // which explains nothing about why the probe failed.
    expect(res.details.events[0]?.detail).toContain("agent_not_found");
  });

  test("a finished settle costs no probe at all -- only a timeout provokes one", async () => {
    const { runId, poll, stub } = setupQueued(
      [{ code: 0, stdout: realWaitEnvelope("idle", "checkin-w1", "w1:pZ"), stderr: "" }],
      { code: 0, stdout: realWaitEnvelope("working", "checkin-w1", "w1:pZ"), stderr: "" },
    );

    const res = (await poll.execute(
      ...(["call-1", { runId, timeoutMs: 1000 }, undefined] as unknown as never[]),
    )) as { details: { events: { kind: string }[] } };

    expect(res.details.events.map((e) => e.kind)).toEqual(["finished"]);
    expect(stub.calls.filter((c) => c.argv[0] === "agent" && c.argv[1] === "get")).toHaveLength(0);
  });

  test("a close that fails still drains the batch and still frees the wait slot", async () => {
    // The drain loop used to call herdr bare here: a rejected close threw out
    // of the loop, abandoning every event after it and leaving those workers
    // un-dropped with their entries held.
    const runId = "closefail";
    const worker: WorkerRecord = {
      agent: "closefail-w1",
      slug: "some-item",
      paneId: "w1:pZ",
      tabId: "w1:tZ",
      lifecycle: "active",
    };
    saveState({ runId, concurrency: 2, nextCounter: 1, workers: [worker] }, dir);

    const stub = makeStubPi((argv) => {
      const [a, b] = argv;
      if (a === "tab" && b === "close") throw new Error("herdr socket closed");
      if (a === "agent" && b === "list") {
        return {
          code: 0,
          stdout: realAgentListEnvelope({
            agent: "pi",
            agent_status: "idle",
            name: "closefail-w1",
            pane_id: "w1:pZ",
          }),
          stderr: "",
        };
      }
      if (a === "agent" && b === "wait") {
        return { code: 0, stdout: realWaitEnvelope("idle", "closefail-w1", "w1:pZ"), stderr: "" };
      }
      return { code: 0, stdout: "", stderr: "" };
    });
    registerSwarmTools(stub.pi as unknown as Parameters<typeof registerSwarmTools>[0]);
    const poll = stub.tools.get("swarm_poll");
    if (!poll) throw new Error("swarm_poll was never registered");

    const res = (await poll.execute(
      ...(["call-1", { runId, timeoutMs: 1000 }, undefined] as unknown as never[]),
    )) as { details: { events: { kind: string }[] } };

    expect(res.details.events.map((e) => e.kind)).toEqual(["finished"]);
    expect(loadState(runId, dir)?.workers).toHaveLength(0);
  });
});

// ---------------------------------------------------------------------------
// classifyTimeoutProbe: fail open, but bounded by the budget.
// ---------------------------------------------------------------------------

describe("classifyTimeoutProbe", () => {
  const probe = (over: Partial<Parameters<typeof classifyTimeoutProbe>[0]> = {}) => ({
    code: 0,
    stdout: "",
    stderr: "",
    abandoned: false,
    ...over,
  });
  const live = (status: string) => probe({ stdout: realWaitEnvelope(status, "w1", "w1:p1") });
  const HOUR = 60 * 60 * 1000;

  test("a positively-gone agent is the ONLY nonzero exit that closes a worker", () => {
    const gone = probe({
      code: 1,
      stderr: JSON.stringify({ error: { code: "agent_not_found", message: "no such agent" } }),
    });
    expect(classifyTimeoutProbe(gone, 0, HOUR)).toEqual({ disposition: "event", kind: "error" });
  });

  test("a transient nonzero exit re-arms -- killing on a failed CHECK is the bug, one layer down", () => {
    const flaky = probe({ code: 1, stderr: '{"error":{"code":"internal","message":"boom"}}' });
    expect(classifyTimeoutProbe(flaky, 0, HOUR)).toEqual({ disposition: "rearm" });
  });

  test("an unparseable stderr on a nonzero exit re-arms rather than guessing", () => {
    expect(classifyTimeoutProbe(probe({ code: 1, stderr: "segfault" }), 0, HOUR)).toEqual({
      disposition: "rearm",
    });
  });

  test("an abandoned probe re-arms: we gave up on the check, learning nothing about the worker", () => {
    // pi.exec RESOLVES on abort with a coerced exit 0 and empty stdout, so
    // without the flag this is indistinguishable from "no recognizable
    // status" -- and that case must never close a live worker.
    expect(classifyTimeoutProbe(probe({ abandoned: true }), 0, HOUR)).toEqual({
      disposition: "rearm",
    });
  });

  test("a settle that raced the probe wins: blocked and idle/done are reported, not overridden", () => {
    expect(classifyTimeoutProbe(live("blocked"), 0, HOUR)).toEqual({
      disposition: "event",
      kind: "blocked",
    });
    expect(classifyTimeoutProbe(live("idle"), 0, HOUR)).toEqual({
      disposition: "event",
      kind: "finished",
    });
    expect(classifyTimeoutProbe(live("done"), 0, HOUR)).toEqual({
      disposition: "event",
      kind: "finished",
    });
  });

  test("a working worker inside its budget re-arms", () => {
    expect(classifyTimeoutProbe(live("working"), HOUR - 1, HOUR)).toEqual({
      disposition: "rearm",
    });
  });

  test("an unrecognized live status is treated as alive, not invented into a death", () => {
    expect(classifyTimeoutProbe(live("hibernating"), 0, HOUR)).toEqual({ disposition: "rearm" });
  });

  test("past the budget, a confirmed-live worker is stopped and SAYS it was confirmed", () => {
    expect(classifyTimeoutProbe(live("working"), HOUR, HOUR)).toEqual({
      disposition: "event",
      kind: "timed_out",
      livenessConfirmed: true,
    });
  });

  test("the budget bounds INCONCLUSIVE outcomes too -- otherwise the worst worker runs forever", () => {
    // A worker wedged badly enough that agent get itself cannot answer would
    // re-arm until someone killed the orchestrator by hand.
    for (const p of [
      probe({ abandoned: true }),
      probe({ code: 1, stderr: '{"error":{"code":"internal"}}' }),
      probe({ code: 0, stdout: "{}" }),
    ]) {
      expect(classifyTimeoutProbe(p, HOUR, HOUR)).toEqual({
        disposition: "event",
        kind: "timed_out",
        livenessConfirmed: false,
      });
    }
  });

  test("a worker with no computable elapsed time is never deliberately stopped", () => {
    expect(classifyTimeoutProbe(live("working"), null, 0)).toEqual({ disposition: "rearm" });
    expect(classifyTimeoutProbe(probe({ abandoned: true }), null, 0)).toEqual({
      disposition: "rearm",
    });
  });
});

// ---------------------------------------------------------------------------
// Working-time accounting. The budget is per ITEM, not per segment, and the
// arithmetic must survive an unstamped legacy record and a backwards clock.
// ---------------------------------------------------------------------------

describe("working-time accounting", () => {
  const w = (over: Partial<WorkerRecord> = {}): WorkerRecord => ({
    agent: "a1",
    slug: "s",
    paneId: "p",
    lifecycle: "active",
    ...over,
  });

  test("a record with neither timestamp has no computable elapsed time", () => {
    expect(elapsedWorkingMs(w(), 1000)).toBeNull();
  });

  test("completed segments and the open one are summed", () => {
    expect(elapsedWorkingMs(w({ accumulatedWorkingMs: 500, workingSinceMs: 100 }), 400)).toBe(800);
  });

  test("an accumulator with no open segment still counts -- a parked worker's time is not lost", () => {
    expect(elapsedWorkingMs(w({ accumulatedWorkingMs: 500 }), 400)).toBe(500);
  });

  test("a backwards clock step clamps to zero rather than reversing the accounting", () => {
    expect(elapsedWorkingMs(w({ accumulatedWorkingMs: 500, workingSinceMs: 900 }), 400)).toBe(500);
  });

  test("folding an unstamped legacy record adds nothing -- never NaN", () => {
    // Reachable: a blocked settle needs no probe, so a legacy record can park
    // before any check-in has stamped it. `undefined + number` would be NaN,
    // and NaN >= deadline is false, so such a worker would re-arm forever.
    const worker = w();
    foldWorkingSegment(worker, 1000);
    expect(worker.accumulatedWorkingMs ?? 0).toBe(0);
    expect(Number.isNaN(worker.accumulatedWorkingMs ?? 0)).toBe(false);
  });

  test("the budget is per item: work either side of a relay accumulates", () => {
    const worker = w({ workingSinceMs: 0 });
    foldWorkingSegment(worker, 3000); // worked 3000 before blocking
    expect(worker.workingSinceMs).toBeUndefined();
    worker.workingSinceMs = 10_000; // resumed after a human answered
    expect(elapsedWorkingMs(worker, 11_000)).toBe(4000); // 3000 + 1000, not 1000
  });

  test("folding is idempotent, so a double-park costs nothing", () => {
    const worker = w({ workingSinceMs: 0 });
    foldWorkingSegment(worker, 3000);
    foldWorkingSegment(worker, 9000);
    expect(worker.accumulatedWorkingMs).toBe(3000);
  });

  test("formatDuration reads as a person would say it", () => {
    expect(formatDuration(45 * 60 * 1000)).toBe("45m");
    expect(formatDuration(3 * 60 * 60 * 1000 + 31 * 60 * 1000)).toBe("3h31m");
    expect(formatDuration(0)).toBe("0m");
  });
});

describe("deliberate-stop reporting", () => {
  const worker: WorkerRecord = {
    agent: "a1",
    slug: "some-item",
    paneId: "p",
    cwd: "/home/yanil/dotfiles",
    lifecycle: "active",
  };

  test("the worktree is derived as a sibling of the repo, per the repo convention", () => {
    expect(workerWorktreePath("/home/yanil/dotfiles", "some-item")).toBe(
      "/home/yanil/dotfiles-some-item",
    );
  });

  test("a record predating cwd tracking yields no path rather than a guessed one", () => {
    expect(workerWorktreePath(undefined, "some-item")).toBeNull();
  });

  test("a confirmed-live stop says the worker was working, and names the recovery path", () => {
    const detail = deadlineStopDetail(worker, 4 * 60 * 60 * 1000, { livenessConfirmed: true });
    expect(detail).toContain("still reported working");
    expect(detail).toContain("/home/yanil/dotfiles-some-item");
    expect(detail).toContain("in-progress with a live claim");
  });

  test("an unverified stop does NOT claim the worker was working, and carries the probe's reason", () => {
    // Reporting these two identically would repeat this tool's own root
    // complaint: a wrong outcome label becomes the story the orchestrator
    // tells the human.
    const detail = deadlineStopDetail(worker, 4 * 60 * 60 * 1000, {
      livenessConfirmed: false,
      probeDetail: "the liveness probe did not answer within 15000 ms and was abandoned",
    });
    expect(detail).not.toContain("still reported working");
    expect(detail).toContain("could NOT be verified");
    expect(detail).toContain("abandoned");
    expect(detail).toContain("/home/yanil/dotfiles-some-item");
  });

  test("with no cwd, the report says the path is unavailable rather than inventing one", () => {
    const detail = deadlineStopDetail({ ...worker, cwd: undefined }, 1000, {
      livenessConfirmed: true,
    });
    expect(detail).toContain("git worktree list");
  });
});

// ---------------------------------------------------------------------------
// A pinned worker model reaches `herdr agent start` and is recorded.
//
// Before this, buildAgentStartArgv emitted no agent args at all, so every
// worker silently took pi's default and no digest could say which model did
// the work. The plumbing existed at both ends -- herdr forwards everything
// after `--`, and pi --model takes a provider/id -- it was simply never wired.
// ---------------------------------------------------------------------------

describe("swarm_spawn model passthrough", () => {
  test("the model lands after the separator, so herdr forwards it instead of rejecting it", () => {
    const argv = buildAgentStartArgv("run1-w1", "w1:pB", "opencode-go/glm-5.3-flash");
    const sep = argv.indexOf("--");
    expect(sep).toBeGreaterThan(-1);
    // Everything herdr parses must precede the separator...
    expect(argv.slice(0, sep)).toContain("--pane");
    expect(argv.slice(0, sep)).not.toContain("--model");
    // ...and everything for pi must follow it, in order.
    expect(argv.slice(sep)).toEqual(["--", "--model", "opencode-go/glm-5.3-flash"]);
  });

  test("an omitted model changes nothing about the command", () => {
    expect(buildAgentStartArgv("run1-w1", "w1:pB")).toEqual([
      "agent",
      "start",
      "run1-w1",
      "--kind",
      "pi",
      "--pane",
      "w1:pB",
      "--timeout",
      "30000",
    ]);
  });

  test("a WorkerRecord can carry the model it was started on", () => {
    // The field is what lets a digest answer "what did the work". Optional,
    // because a record written before this existed simply took the default.
    const pinned: WorkerRecord = {
      agent: "run1-w1",
      slug: "s",
      paneId: "p",
      model: "opencode-go/glm-5.3-flash",
      lifecycle: "active",
    };
    expect(pinned.model).toBe("opencode-go/glm-5.3-flash");
    const unpinned: WorkerRecord = {
      agent: "run1-w2",
      slug: "s2",
      paneId: "p2",
      lifecycle: "active",
    };
    expect(unpinned.model).toBeUndefined();
  });
});

// A guard-rails `warn rm -rf` confirmation, as it renders in a worker's pane.
// Deliberately NOT a question-tool picker: no `1.`-numbered option list, no
// `>` marker, no picker footer. This is the shape that used to park a worker
// at awaiting_relay indistinguishably from an answerable one.
const GUARD_RAILS_CONFIRM_OUTPUT = `
 guard-rails

 This command contains \`rm -rf\`. Run it anyway?

 [y/N]
`;

describe("classifyBlock", () => {
  test("a real picker capture is answerable", () => {
    expect(classifyBlock(REAL_PICKER_OUTPUT)).toBe("answerable");
  });

  test("a guard-rails confirm capture needs a human", () => {
    expect(classifyBlock(GUARD_RAILS_CONFIRM_OUTPUT)).toBe("needs_human");
  });

  test("the two land in different buckets -- the whole point of the item", () => {
    expect(classifyBlock(REAL_PICKER_OUTPUT)).not.toBe(classifyBlock(GUARD_RAILS_CONFIRM_OUTPUT));
  });

  test("scrollback holding a numbered plan is not mistaken for an answerable picker", () => {
    // parsePicker already refuses this (non-contiguous / no real footer);
    // classifyBlock must not soften that into a false `answerable`, which
    // would send arrow keys computed from a fabricated index.
    expect(classifyBlock(PICKER_BELOW_A_NUMBERED_PLAN)).toBe("answerable");
  });

  test("an unread or empty pane needs a human rather than defaulting to answerable", () => {
    expect(classifyBlock(undefined)).toBe("needs_human");
    expect(classifyBlock("")).toBe("needs_human");
  });
});

describe("stalledRelayWorkers", () => {
  const STALL = 30 * 60 * 1000;

  test("a worker parked past the stall deadline is reported", () => {
    const w = makeWorker({ agent: "w1", lifecycle: "awaiting_relay" });
    w.awaitingRelaySinceMs = 1_000;
    expect(stalledRelayWorkers([w], 1_000 + STALL, STALL).map((x) => x.agent)).toEqual(["w1"]);
  });

  test("a worker parked inside the deadline is not", () => {
    const w = makeWorker({ agent: "w1", lifecycle: "awaiting_relay" });
    w.awaitingRelaySinceMs = 1_000;
    expect(stalledRelayWorkers([w], 1_000 + STALL - 1, STALL)).toEqual([]);
  });

  test("an ACTIVE worker is never stalled, however long it has been working", () => {
    // The working budget owns that case, and it is deliberately a different
    // clock: this one must not double-report a worker that is simply busy.
    const w = makeWorker({ agent: "w1", lifecycle: "active" });
    w.awaitingRelaySinceMs = 1_000;
    expect(stalledRelayWorkers([w], 1_000 + STALL * 10, STALL)).toEqual([]);
  });

  test("a parked worker with no stamp is not reported, so an upgrade cannot fabricate a stall", () => {
    const w = makeWorker({ agent: "w1", lifecycle: "awaiting_relay" });
    expect(stalledRelayWorkers([w], Date.now(), STALL)).toEqual([]);
  });
});

describe("pickerLabels", () => {
  test("returns the worker's real rendered labels, in order", () => {
    expect(pickerLabels(REAL_PICKER_OUTPUT)).toEqual(["OK", "Cancel", "Something else (type it)"]);
  });

  test("a non-picker prompt yields no labels rather than invented ones", () => {
    expect(pickerLabels(GUARD_RAILS_CONFIRM_OUTPUT)).toEqual([]);
    expect(pickerLabels(undefined)).toEqual([]);
  });
});

describe("noteResolveFailure", () => {
  test("records the attempted answer and why it failed", () => {
    const w = makeWorker({ agent: "w1", lifecycle: "awaiting_relay" });
    noteResolveFailure(w, "Merge, push, clean up", "no listed option matched", 5_000);
    expect(w.lastResolveFailure).toEqual({
      answer: "Merge, push, clean up",
      reason: "no listed option matched",
      at: 5_000,
    });
  });

  test("a later failure replaces the earlier one, so the report is current", () => {
    const w = makeWorker({ agent: "w1", lifecycle: "awaiting_relay" });
    noteResolveFailure(w, "first", "a", 1);
    noteResolveFailure(w, "second", "b", 2);
    expect(w.lastResolveFailure?.answer).toBe("second");
  });
});

describe("a stranded awaiting_relay record is reconciled on a warm poll", () => {
  let dir: string;
  let priorStateDir: string | undefined;

  beforeEach(() => {
    dir = mkdtempSync(join(tmpdir(), "swarm-strand-"));
    priorStateDir = process.env.PI_SWARM_STATE_DIR;
    process.env.PI_SWARM_STATE_DIR = dir;
  });

  afterEach(() => {
    if (priorStateDir === undefined) delete process.env.PI_SWARM_STATE_DIR;
    else process.env.PI_SWARM_STATE_DIR = priorStateDir;
    rmSync(dir, { recursive: true, force: true });
  });

  // Three of four workers in the 2026-09-03 harness2 run ended awaiting_relay
  // with their agents already gone. reconcileState drops such records, but only
  // on a COLD load -- a warm orchestrator never re-reads, so the run reported
  // unanswered relays forever and could not spawn (awaiting_relay is excluded
  // from activeWorkerCount but INCLUDED in openPaneCount). A human found it
  // from a dashboard in another pane.
  test("the second poll drops a worker whose agent has vanished, and says so", async () => {
    const runId = "strandrun";
    saveState(
      {
        runId,
        concurrency: 2,
        nextCounter: 1,
        workers: [
          {
            agent: "strandrun-w1",
            slug: "stranded-item",
            paneId: "w1:pQ",
            tabId: "w1:tQ",
            lifecycle: "awaiting_relay",
          },
        ],
      },
      dir,
    );

    let agentAlive = true;
    const stub = makeStubPi((argv) =>
      argv[0] === "agent" && argv[1] === "list"
        ? {
            code: 0,
            stdout: agentAlive
              ? realAgentListEnvelope(UNNAMED_CLAUDE_ENTRY, {
                  agent: "pi",
                  agent_status: "idle",
                  name: "strandrun-w1",
                  pane_id: "w1:pA",
                })
              : realAgentListEnvelope(UNNAMED_CLAUDE_ENTRY),
            stderr: "",
          }
        : { code: 0, stdout: "", stderr: "" },
    );
    registerSwarmTools(stub.pi as unknown as Parameters<typeof registerSwarmTools>[0]);
    const poll = stub.tools.get("swarm_poll");
    if (!poll) throw new Error("swarm_poll was never registered");

    // Poll once while the agent is live: the record is legitimately awaiting.
    const first = (await poll.execute(
      ...(["c1", { runId, timeoutMs: 1000 }, undefined] as unknown as never[]),
    )) as { content: { type: string; text: string }[] };
    expect(first.content.map((c) => c.text).join("\n")).toContain("strandrun-w1");

    // The worker's agent goes away. The run is now WARM -- getOrInitState
    // returns the cached state and never re-lists.
    agentAlive = false;
    const second = (await poll.execute(
      ...(["c2", { runId, timeoutMs: 1000 }, undefined] as unknown as never[]),
    )) as { content: { type: string; text: string }[] };
    const text = second.content.map((c) => c.text).join("\n");

    expect(text).not.toContain("swarm_resolve_blocked");
    expect(text.toLowerCase()).toContain("gone");
    expect(loadState(runId, dir)?.workers ?? []).toEqual([]);
  });
});

describe("worker capture offers travel to the orchestrator", () => {
  let dir: string;
  let priorStateDir: string | undefined;

  beforeEach(() => {
    dir = mkdtempSync(join(tmpdir(), "swarm-capture-"));
    priorStateDir = process.env.PI_SWARM_STATE_DIR;
    process.env.PI_SWARM_STATE_DIR = dir;
  });

  afterEach(() => {
    if (priorStateDir === undefined) delete process.env.PI_SWARM_STATE_DIR;
    else process.env.PI_SWARM_STATE_DIR = priorStateDir;
    rmSync(dir, { recursive: true, force: true });
  });

  test("the spawned tab is told where to write its offers", () => {
    const argv = buildTabCreateArgv("/repo", "my-slug", "/state/swarm-r1-capture-my-slug.json");
    expect(argv).toContain("PI_SWARM_CAPTURE_FILE=/state/swarm-r1-capture-my-slug.json");
    // The unattended env must survive alongside it, not be replaced.
    expect(argv).toContain("PI_AGENT_UNATTENDED=1");
  });

  test("omitting the capture path leaves the argv exactly as it was", () => {
    expect(buildTabCreateArgv("/repo", "my-slug")).toEqual([
      "tab",
      "create",
      "--cwd",
      "/repo",
      "--label",
      "my-slug",
      "--env",
      "PI_AGENT_UNATTENDED=1",
      "--no-focus",
    ]);
  });

  test("offers are read back and the file is consumed, so a re-poll cannot double-report", () => {
    const path = capturePath("r1", "my-slug", dir);
    writeFileSync(
      path,
      JSON.stringify({
        offers: [
          { kind: "backlog", id: "meta-thing", summary: "a thing worth doing" },
          { kind: "out-of-scope", id: "some-concept", summary: "declined outright" },
        ],
      }),
      "utf8",
    );
    const first = readCaptureOffers("r1", "my-slug", dir);
    expect(first).toHaveLength(2);
    expect(first[0]?.kind).toBe("backlog");
    expect(first[0]?.id).toBe("meta-thing");
    // Consumed: the worker's tab is closed and its record dropped right
    // after this, so a file left behind would be re-reported by any later
    // poll with no worker to attribute it to.
    expect(readCaptureOffers("r1", "my-slug", dir)).toEqual([]);
  });

  test("a worker that queued nothing yields no offers rather than an error", () => {
    expect(readCaptureOffers("r1", "never-wrote", dir)).toEqual([]);
  });

  test("a malformed capture file is ignored rather than crashing the poll", () => {
    writeFileSync(capturePath("r1", "bad", dir), "{not json", "utf8");
    expect(readCaptureOffers("r1", "bad", dir)).toEqual([]);
  });

  test("a slug that could escape the state dir cannot", () => {
    const path = capturePath("r1", "../../etc/passwd", dir);
    expect(path.startsWith(dir)).toBe(true);
    expect(path).not.toContain("..");
  });
});

describe("AMEND_INSTRUCTION", () => {
  test("tells the worker to re-read its item, and carries no correction text", () => {
    expect(AMEND_INSTRUCTION).toContain("dev_status.py show");
    // The whole design rests on this: the moment the channel carries the
    // correction itself, the backlog store stops being the single source of
    // truth and the two can disagree. The instruction is fixed for that
    // reason -- there is no parameter to smuggle content through.
    expect(AMEND_INSTRUCTION.length).toBeLessThan(600);
  });
});

describe("swarm_amend", () => {
  let dir: string;
  let priorStateDir: string | undefined;

  beforeEach(() => {
    dir = mkdtempSync(join(tmpdir(), "swarm-amend-"));
    priorStateDir = process.env.PI_SWARM_STATE_DIR;
    process.env.PI_SWARM_STATE_DIR = dir;
  });

  afterEach(() => {
    if (priorStateDir === undefined) delete process.env.PI_SWARM_STATE_DIR;
    else process.env.PI_SWARM_STATE_DIR = priorStateDir;
    rmSync(dir, { recursive: true, force: true });
  });

  function seed(runId: string, lifecycle: "active" | "awaiting_relay") {
    saveState(
      {
        runId,
        concurrency: 2,
        nextCounter: 1,
        workers: [
          {
            agent: `${runId}-w1`,
            slug: "some-item",
            paneId: "w1:pA",
            tabId: "w1:tA",
            lifecycle,
          },
        ],
      },
      dir,
    );
  }

  function register(handler: (argv: string[]) => { code: number; stdout: string; stderr: string }) {
    const stub = makeStubPi(handler);
    registerSwarmTools(stub.pi as unknown as Parameters<typeof registerSwarmTools>[0]);
    const amend = stub.tools.get("swarm_amend");
    if (!amend) throw new Error("swarm_amend was never registered");
    return { stub, amend };
  }

  const liveList = (runId: string) =>
    realAgentListEnvelope(UNNAMED_CLAUDE_ENTRY, {
      agent: "pi",
      agent_status: "working",
      name: `${runId}-w1`,
      pane_id: "w1:pA",
    });

  test("sends the fixed re-read instruction to an active worker and records it", async () => {
    const runId = "amendrun";
    seed(runId, "active");
    const sent: string[][] = [];
    const { amend } = register((argv) => {
      sent.push(argv);
      if (argv[0] === "agent" && argv[1] === "list")
        return { code: 0, stdout: liveList(runId), stderr: "" };
      return { code: 0, stdout: "", stderr: "" };
    });

    const res = (await amend.execute(
      ...(["c1", { runId, agent: `${runId}-w1` }, undefined] as unknown as never[]),
    )) as { content: { type: string; text: string }[] };
    const text = res.content.map((c) => c.text).join("\n");

    const prompts = sent.filter((a) => a[0] === "agent" && a[1] === "prompt");
    expect(prompts).toHaveLength(1);
    expect(prompts[0]).toContain(AMEND_INSTRUCTION);
    expect(text).toContain("amended:");

    // Recorded on the run, so the end-of-run digest can say an item's
    // premises changed under a worker mid-flight -- the correction that
    // went through a raw herdr prompt left no trace at all.
    const saved = loadState(runId, dir);
    expect(saved?.workers[0]?.amendments?.length).toBe(1);
  });

  test("refuses a worker parked at a gate rather than sending into a refusal", async () => {
    // herdr agent prompt refuses a blocked agent outright, so an amend
    // cannot reach one. The answer for that case is swarm_resolve_blocked,
    // and saying so beats a confusing wire error.
    const runId = "blockedrun";
    seed(runId, "awaiting_relay");
    const sent: string[][] = [];
    const { amend } = register((argv) => {
      sent.push(argv);
      if (argv[0] === "agent" && argv[1] === "list")
        return { code: 0, stdout: liveList(runId), stderr: "" };
      return { code: 0, stdout: "", stderr: "" };
    });

    const res = (await amend.execute(
      ...(["c1", { runId, agent: `${runId}-w1` }, undefined] as unknown as never[]),
    )) as { content: { type: string; text: string }[] };
    const text = res.content.map((c) => c.text).join("\n");

    expect(text).toContain("amend_refused:");
    expect(text).toContain("swarm_resolve_blocked");
    expect(sent.filter((a) => a[1] === "prompt")).toHaveLength(0);
  });

  test("an unknown target is reported, not silently ignored", async () => {
    const runId = "amendrun";
    seed(runId, "active");
    const { amend } = register((argv) =>
      argv[0] === "agent" && argv[1] === "list"
        ? { code: 0, stdout: liveList(runId), stderr: "" }
        : { code: 0, stdout: "", stderr: "" },
    );
    const res = (await amend.execute(
      ...(["c1", { runId, agent: "nobody" }, undefined] as unknown as never[]),
    )) as { content: { type: string; text: string }[] };
    expect(res.content.map((c) => c.text).join("\n")).toContain("amend_failed:");
  });

  test("the worker can be named by slug as well as agent id", async () => {
    const runId = "amendrun";
    seed(runId, "active");
    const sent: string[][] = [];
    const { amend } = register((argv) => {
      sent.push(argv);
      if (argv[0] === "agent" && argv[1] === "list")
        return { code: 0, stdout: liveList(runId), stderr: "" };
      return { code: 0, stdout: "", stderr: "" };
    });
    const res = (await amend.execute(
      ...(["c1", { runId, agent: "some-item" }, undefined] as unknown as never[]),
    )) as { content: { type: string; text: string }[] };
    expect(res.content.map((c) => c.text).join("\n")).toContain("amended:");
    expect(sent.filter((a) => a[1] === "prompt")).toHaveLength(1);
  });
});

describe("selectSchedulable eligibility", () => {
  const safe = (id: string, ...paths: string[]) => ({
    id,
    worker_safe: true,
    related_files: paths.map((path) => ({ path })),
  });
  const unsafe = (id: string) => ({ id, worker_safe: false, related_files: [] });
  const unstamped = (id: string) => ({ id, related_files: [] });

  test("an item the backlog marks unsafe is refused, not spawned", () => {
    const res = selectSchedulable([unsafe("meta-a"), safe("atk-b")], [], 3);
    expect(res.slugs).toEqual(["atk-b"]);
    expect(res.refused.map((r) => r.slug)).toEqual(["meta-a"]);
  });

  test("a refused item is neither deferred nor skipped", () => {
    // The three mean different things to the orchestrator: skipped is coming
    // next wave, deferred waits on a named worker, refused is never coming.
    const res = selectSchedulable([unsafe("meta-a")], [], 3);
    expect(res.deferred).toEqual([]);
    expect(res.skipped).toEqual([]);
    expect(res.refused).toHaveLength(1);
  });

  test("refusal is decided before the concurrency cap", () => {
    // With no headroom a refused item must still read as refused; reporting it
    // as skipped would promise it a later wave that will never take it.
    const res = selectSchedulable([unsafe("meta-a")], [], 0);
    expect(res.refused.map((r) => r.slug)).toEqual(["meta-a"]);
    expect(res.skipped).toEqual([]);
  });

  test("an item with no worker_safe field fails CLOSED", () => {
    // pi/AGENTS.md documents testing an in-progress extension with `pi -e`
    // against the INSTALLED dev_status.py, so new TypeScript running against
    // an older Python is the normal development path. A guard that silently
    // switches itself off there is worse than no guard.
    const res = selectSchedulable([unstamped("atk-a")], [], 3);
    expect(res.slugs).toEqual([]);
    expect(res.refused).toHaveLength(1);
    expect(res.refused[0]!.reason).toContain("worker_safe");
  });

  test("refusal wins over a file collision, since it is the permanent one", () => {
    const res = selectSchedulable([unsafe("meta-a")], [], 3);
    expect(res.refused).toHaveLength(1);
    expect(res.deferred).toEqual([]);
  });

  test("a clean queue is untouched by the guard", () => {
    const res = selectSchedulable([safe("atk-a"), safe("iron-lb-b")], [], 3);
    expect(res.slugs).toEqual(["atk-a", "iron-lb-b"]);
    expect(res.refused).toEqual([]);
  });

  test("every candidate refused yields an empty wave, not an empty queue", () => {
    const res = selectSchedulable([unsafe("meta-a"), unsafe("meta-b")], [], 3);
    expect(res.slugs).toEqual([]);
    expect(res.refused).toHaveLength(2);
  });
});

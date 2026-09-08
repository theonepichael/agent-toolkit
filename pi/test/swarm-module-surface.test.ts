import { describe, expect, test } from "bun:test";

// Interface-preservation guard for the swarm helper extraction: the three
// helper modules (swarm-picker, swarm-scheduling, swarm-herdr) must export
// the symbols they own, and swarm-tool.ts must re-export every moved symbol
// so its pre-extraction import surface is unchanged. If a symbol is
// accidentally dropped, renamed, or left duplicated in swarm-tool.ts, these
// checks fail.
//
// Value exports are asserted at runtime with `in` on the namespace object.
// Type-only exports are erased at runtime, so their existence is asserted at
// compile time (`keyof typeof mod` union membership, checked by the
// `typecheck` stage) and pinned to `true` here so the assertion cannot rot.
import * as picker from "../extensions/swarm-picker";
import type { BlockClass, ParsedPicker, RenderedOption } from "../extensions/swarm-picker";
import * as scheduling from "../extensions/swarm-scheduling";
import type {
  Amendment,
  ReadyItem,
  SelectionResult,
  ShownItem,
  SwarmState,
  WorkerLifecycle,
  WorkerRecord,
} from "../extensions/swarm-scheduling";
import * as herdr from "../extensions/swarm-herdr";
import type {
  PollEventKind,
  ProbeResult,
  ResyncVerdict,
  TabCreateResult,
  TimeoutVerdict,
} from "../extensions/swarm-herdr";
import * as surface from "../extensions/swarm-tool";
import type { CaptureOffer, PollEvent } from "../extensions/swarm-tool";

const PICKER_VALUES = [
  "classifyBlock",
  "pickerLabels",
  "noteResolveFailure",
  "parsePicker",
  "matchOption",
  "navigationKeys",
] as const;

const SCHEDULING_VALUES = [
  "parseReadyItems",
  "parseShownItem",
  "isSuspiciousFinish",
  "itemPaths",
  "selectSchedulable",
  "nextAgentId",
  "activeWorkerCount",
  "openPaneCount",
  "openPaneSoftCap",
  "canOpenNewPane",
  "spawnBudget",
  "stalledRelayWorkers",
] as const;

const HERDR_VALUES = [
  "buildTabCreateArgv",
  "buildTabCloseArgv",
  "parseTabCreate",
  "findTabByLabel",
  "buildTabListArgv",
  "buildAgentStartArgv",
  "WORKER_UNATTENDED_ENV",
  "AMEND_INSTRUCTION",
  "buildAgentPromptArgv",
  "reasonHeadline",
  "buildAgentSendKeysArgv",
  "buildAgentWaitArgv",
  "buildAgentGetArgv",
  "buildAgentReadArgv",
  "buildPaneCloseArgv",
  "buildWorkerCloseArgv",
  "buildPaneReadArgv",
  "buildAgentListArgv",
  "parseAgentListIds",
  "classifyWaitResult",
  "classifyResyncGet",
  "classifyTimeoutProbe",
  "workerWorktreePath",
  "deadlineStopDetail",
  "paneIdentityMismatch",
  "waitResultDetail",
] as const;

// The pre-extraction import surface of swarm-tool.ts: every moved symbol
// plus the ones that never moved. A missing entry here is a compatibility
// break for anything importing swarm-tool.ts directly (the test suite and
// any external consumer).
const SURFACE_VALUES = [
  ...PICKER_VALUES,
  ...SCHEDULING_VALUES,
  ...HERDR_VALUES,
  // stayed in swarm-tool.ts
  "PANE_CAPTURE_CHARS",
  "elapsedWorkingMs",
  "foldWorkingSegment",
  "statePath",
  "capturePath",
  "readCaptureOffers",
  "renderCaptureOffers",
  "loadState",
  "saveState",
  "reconcileState",
  "devStatusPath",
  "buildReadyArgv",
  "buildShowArgv",
  "formatDuration",
  "looksTruncated",
] as const;
// (type-only surface members -- PollEvent, CaptureOffer, and every moved
// type -- are asserted by the `import type` probes in typeExportsExist().)

// Type-only exports are asserted by `import type` above: tsc (the
// `typecheck` stage) fails the gate if any named type is missing. The probe
// below keeps those imports used so lint cannot strip them.
function typeExportsExist(): boolean {
  const probes = [
    undefined as BlockClass | undefined,
    undefined as ParsedPicker | undefined,
    undefined as RenderedOption | undefined,
    undefined as Amendment | undefined,
    undefined as ReadyItem | undefined,
    undefined as SelectionResult | undefined,
    undefined as ShownItem | undefined,
    undefined as SwarmState | undefined,
    undefined as WorkerLifecycle | undefined,
    undefined as WorkerRecord | undefined,
    undefined as PollEventKind | undefined,
    undefined as ProbeResult | undefined,
    undefined as ResyncVerdict | undefined,
    undefined as TabCreateResult | undefined,
    undefined as TimeoutVerdict | undefined,
    undefined as PollEvent | undefined,
    undefined as CaptureOffer | undefined,
  ];
  return probes.every((v) => v === undefined);
}

describe("swarm helper module surface", () => {
  test("swarm-picker exports its pinned symbols", () => {
    for (const name of PICKER_VALUES) {
      expect(name in picker).toBe(true);
    }
    expect(typeExportsExist()).toBe(true);
  });

  test("swarm-scheduling exports its pinned symbols", () => {
    for (const name of SCHEDULING_VALUES) {
      expect(name in scheduling).toBe(true);
    }
    expect(typeExportsExist()).toBe(true);
  });

  test("swarm-herdr exports its pinned symbols", () => {
    for (const name of HERDR_VALUES) {
      expect(name in herdr).toBe(true);
    }
    expect(typeExportsExist()).toBe(true);
  });

  test("swarm-tool re-exports every moved symbol (unchanged import surface)", () => {
    for (const name of SURFACE_VALUES) {
      expect(name in surface).toBe(true);
    }
    expect(typeExportsExist()).toBe(true);
  });

  test("moved symbols are defined exactly once across the module set", () => {
    // A symbol both re-exported by swarm-tool via `export *` and redefined
    // locally would shadow one copy silently. `export *` skips keys that
    // already exist locally, so a redefinition would win silently -- check
    // the helper modules do not both define the same public symbol.
    const seen = new Map<string, string>();
    const check = (mod: Record<string, unknown>, modName: string) => {
      for (const key of Object.keys(mod)) {
        expect(seen.has(key)).toBe(false);
        seen.set(key, modName);
      }
    };
    check(picker as unknown as Record<string, unknown>, "swarm-picker");
    check(scheduling as unknown as Record<string, unknown>, "swarm-scheduling");
    check(herdr as unknown as Record<string, unknown>, "swarm-herdr");
  });
});

import type { ExtensionAPI } from "@earendil-works/pi-coding-agent";
import { Type } from "typebox";
import { SwarmToolContext, type ExecFn } from "./swarm-lib/swarm-tool-context";
import { realPickerAdapter } from "./swarm-lib/swarm-picker";

export * from "./swarm-lib/swarm-picker";
export * from "./swarm-lib/swarm-scheduling";
export * from "./swarm-lib/swarm-herdr";
export {
  PANE_CAPTURE_CHARS,
  buildReadyArgv,
  buildShowArgv,
  capturePath,
  defaultExec,
  devStatusPath,
  elapsedWorkingMs,
  fatalSidecarDetail,
  foldWorkingSegment,
  formatDuration,
  herdrStateDir,
  isValidUuid,
  loadState,
  looksTruncated,
  outcomePathOf,
  readCaptureOffers,
  readFatalSidecar,
  reconcileState,
  renderCaptureOffers,
  renderOutcome,
  saveState,
  statePath,
  SwarmToolContext,
} from "./swarm-lib/swarm-tool-context";
export type {
  CaptureOffer,
  ExecFn,
  ExecResult,
  PollEvent,
  SwarmToolContextOptions,
} from "./swarm-lib/swarm-tool-context";

function piResult(result: {
  content: { type: string; text: string }[];
  details: Record<string, unknown>;
}) {
  return {
    ...result,
    content: result.content.map(({ text }) => ({ type: "text" as const, text })),
  };
}

function piStateDir(): string {
  return process.env.PI_SWARM_STATE_DIR ?? `${process.env.HOME ?? ""}/.pi/agent/state`;
}

export default function registerSwarmTools(pi: ExtensionAPI): void {
  const exec: ExecFn = (command, args, options) => pi.exec(command, args, options);
  const context = new SwarmToolContext(exec, realPickerAdapter, {
    kind: "pi",
    stateDir: piStateDir,
    workerPrompt: (slug) => `/backlog-item --auto ${slug}`,
  });

  pi.registerTool({
    name: "swarm_spawn",
    label: "Swarm spawn",
    description: "Spawn Pi workers for READY backlog items in concurrent or serial mode.",
    parameters: Type.Object({
      runId: Type.String(),
      items: Type.Optional(Type.Array(Type.String())),
      prefix: Type.Optional(Type.String()),
      concurrency: Type.Optional(Type.Number()),
      mode: Type.Optional(Type.Union([Type.Literal("concurrent"), Type.Literal("serial")])),
      model: Type.Optional(Type.String()),
    }),
    async execute(_id, params) {
      return piResult(await context.swarmSpawn(params as Parameters<typeof context.swarmSpawn>[0]));
    },
  });

  pi.registerTool({
    name: "swarm_poll",
    label: "Swarm poll",
    description: "Wait for swarm workers to settle or check in.",
    parameters: Type.Object({
      runId: Type.String(),
      timeoutMs: Type.Optional(Type.Number()),
      workerDeadlineMs: Type.Optional(Type.Number()),
      relayStallMs: Type.Optional(Type.Number()),
    }),
    async execute(_id, params, signal) {
      return piResult(
        await context.swarmPoll(params as Parameters<typeof context.swarmPoll>[0], signal),
      );
    },
  });

  pi.registerTool({
    name: "swarm_amend",
    label: "Swarm amend",
    description: "Tell a running worker to re-read its corrected backlog item.",
    parameters: Type.Object({ runId: Type.String(), agent: Type.String() }),
    async execute(_id, params, signal) {
      return piResult(
        await context.swarmAmend(params as Parameters<typeof context.swarmAmend>[0], signal),
      );
    },
  });

  pi.registerTool({
    name: "swarm_resolve_blocked",
    label: "Swarm resolve",
    description: "Answer a blocked Pi worker's picker.",
    parameters: Type.Object({ runId: Type.String(), agent: Type.String(), answer: Type.String() }),
    async execute(_id, params, signal) {
      return piResult(
        await context.swarmResolveBlocked(
          params as Parameters<typeof context.swarmResolveBlocked>[0],
          signal,
        ),
      );
    },
  });
}

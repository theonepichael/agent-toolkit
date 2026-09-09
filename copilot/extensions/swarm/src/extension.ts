// Copilot CLI Extension definition for swarm orchestration.
// Exposes tools: swarm_spawn, swarm_poll, swarm_amend, swarm_resolve_blocked.

import { joinSession } from "@github/copilot-sdk/extension";
import { SwarmToolContext } from "../../../../pi/extensions/swarm-lib/swarm-tool-context";

const context = new SwarmToolContext();

await joinSession({
  tools: [
    {
      name: "swarm_spawn",
      description:
        "Spawn concurrent workers for a batch of READY backlog items, one herdr tab each, up to concurrency cap.",
      parameters: {
        type: "object",
        properties: {
          runId: {
            type: "string",
            description:
              "Identifier for this swarm run -- reused across spawn/poll/resolve calls.",
          },
          items: {
            type: "array",
            items: { type: "string" },
            description:
              "Backlog item slugs to spawn. Omit to select automatically from READY queue via prefix.",
          },
          prefix: {
            type: "string",
            description: "Slug prefix scoping automatic selection, e.g. 'atk-'.",
          },
          concurrency: {
            type: "number",
            description: "Max concurrent active workers. Default 3.",
          },
          model: {
            type: "string",
            description: "Model for every worker in this wave.",
          },
          pluginDir: {
            type: "string",
            description:
              "Absolute path to this checkout's copilot/extensions/swarm, passed to every " +
              "worker's --plugin-dir. Omit only if this checkout is literally at " +
              "~/Workspace/agent-toolkit -- otherwise the default guess is wrong and every " +
              "worker spawn in this run will fail. Persists on the run's state, so only the " +
              "first swarm_spawn call for a runId needs to pass it.",
          },
        },
        required: ["runId"],
      },
      handler: async (args: any) => {
        const result = await context.swarmSpawn(args);
        return result.content.map((c: any) => c.text).join("\n");
      },
    },
    {
      name: "swarm_poll",
      description:
        "Wait for at least one active swarm worker to settle or check in, returning events.",
      parameters: {
        type: "object",
        properties: {
          runId: {
            type: "string",
            description: "Identifier for this swarm run.",
          },
          timeoutMs: {
            type: "number",
            description: "Check-in interval in ms before checking liveness.",
          },
          relayStallMs: {
            type: "number",
            description:
              "How long a worker may sit awaiting relay before flagging stall.",
          },
          workerDeadlineMs: {
            type: "number",
            description: "Whole-item working time budget in ms.",
          },
        },
        required: ["runId"],
      },
      handler: async (args: any) => {
        const result = await context.swarmPoll(args);
        return result.content.map((c: any) => c.text).join("\n");
      },
    },
    {
      name: "swarm_amend",
      description:
        "Tell a running worker its backlog item has been corrected, so it re-reads the item before continuing.",
      parameters: {
        type: "object",
        properties: {
          runId: {
            type: "string",
            description: "Identifier for this swarm run.",
          },
          agent: {
            type: "string",
            description: "The worker's agent id or slug.",
          },
        },
        required: ["runId", "agent"],
      },
      handler: async (args: any) => {
        const result = await context.swarmAmend(args);
        return result.content.map((c: any) => c.text).join("\n");
      },
    },
    {
      name: "swarm_resolve_blocked",
      description: "Answer or inspect a blocked worker's gate.",
      parameters: {
        type: "object",
        properties: {
          runId: {
            type: "string",
            description: "Identifier for this swarm run.",
          },
          agent: {
            type: "string",
            description:
              "The synthetic agent id from a blocked swarm_poll event.",
          },
          answer: {
            type: "string",
            description: "The answer or instructions.",
          },
        },
        required: ["runId", "agent", "answer"],
      },
      handler: async (args: any) => {
        const result = await context.swarmResolveBlocked(args);
        return result.content.map((c: any) => c.text).join("\n");
      },
    },
  ],
});

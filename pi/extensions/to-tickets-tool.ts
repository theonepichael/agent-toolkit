import { homedir } from "node:os";
import { join } from "node:path";
import type { ExtensionAPI } from "@earendil-works/pi-coding-agent";
import { StringEnum } from "@earendil-works/pi-ai";
import { Type } from "typebox";

// Wraps claude/scripts/to_tickets_runner.py, following the pattern set by
// dev-status-tool.ts (see ~/.claude/data/grill/pi-tool-dev-status-spec.md).
//
// One subcommand, one argument: path in, created slugs out. The value over
// a bash call is not the argv shape but the shell: the batch file's
// summary/context fields routinely contain apostrophes, and to-tickets.md
// has to spell out a shell-quoting rule to keep them from breaking an
// inline single-quoted command. pi.exec takes argv directly, so no shell
// ever parses the path.

const TO_TICKETS_RUNNER_PATH = join(homedir(), ".claude", "scripts", "to_tickets_runner.py");

const ACTIONS = ["run"] as const;

export type Action = (typeof ACTIONS)[number];

export interface ToTicketsParams {
  action: Action;
  batchFile?: string;
}

export function assertFields(action: Action, params: ToTicketsParams): void {
  if (action === "run") {
    if (params.batchFile === undefined) {
      throw new Error('action "run" requires: batchFile');
    }
    if (params.batchFile.trim() === "") {
      throw new Error("batchFile must not be empty");
    }
  }
}

export function buildArgv(action: Action, params: ToTicketsParams): string[] {
  switch (action) {
    case "run":
      return ["run", params.batchFile!];
  }
}

export default function (pi: ExtensionAPI) {
  pi.registerTool({
    name: "to_tickets",
    label: "Tickets",
    description:
      "Create a linked batch of backlog items from a confirmed vertical-slice ticket breakdown.",
    promptSnippet: "Create a linked batch of backlog items from a ticket breakdown file",
    promptGuidelines: [
      "Never invoke to_tickets_runner.py via bash -- always use to_tickets instead.",
      'to_tickets covers everything to_tickets_runner.py does: action "run" with the path to the batch JSON file. If you are about to compose a `python3 ~/.claude/scripts/to_tickets_runner.py run ...` bash command, use to_tickets instead.',
      "Write the batch JSON to a file first, then pass its path. Never build the JSON inline in a command: summary and context fields routinely contain apostrophes.",
      "If a run is interrupted, call to_tickets again with the same batchFile. The runner keeps its own state file, so repeating the call resumes rather than duplicating tickets -- do not try to work out which tickets already exist.",
    ],
    parameters: Type.Object({
      action: StringEnum(ACTIONS),
      batchFile: Type.Optional(
        Type.String({
          description:
            "Path to the batch JSON file, conventionally ~/.claude/data/to-tickets/<topic-slug>-tickets-batch.json. Never ~/.claude/data/grill/, which grill.py globs as its private session store.",
        }),
      ),
    }),
    async execute(_toolCallId, params, signal) {
      const typed = params as ToTicketsParams;

      assertFields(typed.action, typed);

      const argv = buildArgv(typed.action, typed);

      const result = await pi.exec("python3", [TO_TICKETS_RUNNER_PATH, ...argv], { signal });

      if (result.code !== 0) {
        throw new Error(
          result.stderr || result.stdout || `to_tickets_runner.py exited ${result.code}`,
        );
      }

      const text = result.stderr ? `${result.stdout}\n\n${result.stderr}` : result.stdout;

      return {
        content: [{ type: "text", text }],
        details: { stdout: result.stdout, stderr: result.stderr },
      };
    },
  });
}

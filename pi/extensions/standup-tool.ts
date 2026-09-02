import { homedir } from "node:os";
import { join } from "node:path";
import type { ExtensionAPI } from "@earendil-works/pi-coding-agent";
import { StringEnum } from "@earendil-works/pi-ai";
import { Type } from "typebox";

// Wraps claude/scripts/standup.py, following the pattern set by
// dev-status-tool.ts (see ~/.claude/data/grill/pi-tool-dev-status-spec.md).
//
// Scoped to `fetch` only. standup.md also calls dev_status.py for its
// pending-item writes, and those already go through the dev_status tool --
// this tool deliberately does not duplicate them.

const STANDUP_PATH = join(homedir(), ".claude", "scripts", "standup.py");

const ACTIONS = ["fetch"] as const;

export type Action = (typeof ACTIONS)[number];

// standup.py takes --date as a bare string. A wrong shape does not error --
// it lands the window on the wrong day and the standup silently covers the
// wrong period -- so the shape is enforced here instead.
const ISO_DATE = /^\d{4}-\d{2}-\d{2}$/;

export interface StandupParams {
  action: Action;
  date?: string;
}

export function assertFields(_action: Action, params: StandupParams): void {
  if (params.date !== undefined && !ISO_DATE.test(params.date)) {
    throw new Error(`date must be YYYY-MM-DD, got "${params.date}"`);
  }
}

export function buildArgv(action: Action, params: StandupParams): string[] {
  switch (action) {
    case "fetch":
      return ["fetch", ...(params.date ? ["--date", params.date] : [])];
  }
}

export default function (pi: ExtensionAPI) {
  pi.registerTool({
    name: "standup",
    label: "Standup",
    description:
      "Gather git commits, backlog activity, pending replies, and other standup sources as one JSON object.",
    promptSnippet: "Gather the local sources for a daily standup draft",
    promptGuidelines: [
      "Never invoke standup.py via bash -- always use standup instead.",
      'standup covers everything standup.py does: action "fetch", optionally with a date. If you are about to compose a `python3 ~/.claude/scripts/standup.py fetch ...` bash command, use standup instead.',
      "standup is read-only. It never comments on a ticket, posts a message, or marks anything read.",
      "Pending-item writes are not part of this tool -- use the dev_status tool's pending_add and pending_update actions for those.",
    ],
    parameters: Type.Object({
      action: StringEnum(ACTIONS),
      date: Type.Optional(
        Type.String({
          description:
            "Reference date as YYYY-MM-DD. Defaults to today. Use it after a gap longer than one working day (holiday, PTO), where the default last-working-day boundary would land on the wrong day.",
        }),
      ),
    }),
    async execute(_toolCallId, params, signal) {
      const typed = params as StandupParams;

      assertFields(typed.action, typed);

      const argv = buildArgv(typed.action, typed);

      const result = await pi.exec("python3", [STANDUP_PATH, ...argv], { signal });

      if (result.code !== 0) {
        throw new Error(result.stderr || result.stdout || `standup.py exited ${result.code}`);
      }

      const text = result.stderr ? `${result.stdout}\n\n${result.stderr}` : result.stdout;

      return {
        content: [{ type: "text", text }],
        details: { stdout: result.stdout, stderr: result.stderr },
      };
    },
  });
}

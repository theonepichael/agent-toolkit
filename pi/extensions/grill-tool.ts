import { homedir } from "node:os";
import { join } from "node:path";
import type { ExtensionAPI } from "@earendil-works/pi-coding-agent";
import { StringEnum } from "@earendil-works/pi-ai";
import { Type } from "typebox";

// Wraps claude/scripts/grill.py, following the pattern set by
// dev-status-tool.ts (see ~/.claude/data/grill/pi-tool-dev-status-spec.md).
//
// Unlike dev_status.py, grill.py addresses sessions by slug or unique
// substring only -- never by a numeric position that can drift between the
// model deciding to act and the call landing. So the numeric-identity
// rejection that dominates dev-status-tool.ts has no analogue here, and
// `session` is passed through as-is.

const GRILL_PATH = join(homedir(), ".claude", "scripts", "grill.py");

const ACTIONS = [
  "new",
  "ask",
  "decide",
  "revise",
  "rm",
  "verdict",
  "plan",
  "mark_pending_execution",
  "pending_plan",
  "next",
  "frontier",
  "render",
  "list",
  "show",
] as const;

export type Action = (typeof ACTIONS)[number];

export type Field =
  "session" | "decisionId" | "payload" | "path" | "backlogSlug" | "force" | "consume";

interface ActionFields {
  readonly allowed: readonly Field[];
  readonly required: readonly Field[];
}

// Exhaustive, matching INTERFACES.md's grill.py subcommand table.
const ACTION_FIELDS: Record<Action, ActionFields> = {
  // `new` creates the session, so it is the one action with no --session.
  new: { allowed: ["payload"], required: ["payload"] },
  ask: { allowed: ["payload", "session"], required: ["payload"] },
  decide: { allowed: ["payload", "session"], required: ["payload"] },
  revise: {
    allowed: ["decisionId", "payload", "session"],
    required: ["decisionId", "payload"],
  },
  rm: { allowed: ["decisionId", "force", "session"], required: ["decisionId"] },
  verdict: {
    allowed: ["decisionId", "payload", "session"],
    required: ["decisionId", "payload"],
  },
  plan: { allowed: ["path", "session"], required: ["path"] },
  mark_pending_execution: { allowed: ["backlogSlug", "session"], required: [] },
  // Reads across every session to find the pending one, so it takes no
  // --session of its own.
  pending_plan: { allowed: ["consume"], required: [] },
  next: { allowed: ["session"], required: [] },
  frontier: { allowed: ["session"], required: [] },
  render: { allowed: ["session"], required: [] },
  list: { allowed: [], required: [] },
  show: { allowed: ["decisionId", "session"], required: [] },
};

// grill.py's VALID_RESULTS, and the subset that will not accept a bare
// verdict: a claim the decision was checked has to say what checked it.
const VALID_RESULTS = ["VERIFIED", "DISPUTED", "UNVERIFIABLE"] as const;
const EVIDENCE_REQUIRED = new Set<string>(["VERIFIED", "DISPUTED"]);

// Actions whose payload must name the decision point it creates or resolves.
const PAYLOAD_ID_REQUIRED = new Set<Action>(["ask", "decide"]);

export interface GrillParams {
  action: Action;
  session?: string;
  decisionId?: string;
  payload?: Record<string, unknown>;
  path?: string;
  backlogSlug?: string;
  force?: boolean;
  consume?: boolean;
}

export function assertFields(action: Action, params: GrillParams): void {
  const { allowed, required } = ACTION_FIELDS[action];
  const allowedSet = new Set<Field>(allowed);

  const supplied = (Object.keys(params) as (keyof GrillParams)[]).filter(
    (key) => key !== "action" && params[key] !== undefined,
  ) as Field[];

  const missing = required.filter((field) => params[field] === undefined);
  if (missing.length > 0) {
    throw new Error(`action "${action}" requires: ${missing.join(", ")}`);
  }

  const extra = supplied.filter((field) => !allowedSet.has(field));
  if (extra.length > 0) {
    throw new Error(`action "${action}" does not accept: ${extra.join(", ")}`);
  }

  if (action === "new" && !params.payload?.topic) {
    throw new Error('action "new" requires payload.topic');
  }

  if (PAYLOAD_ID_REQUIRED.has(action) && !params.payload?.id) {
    throw new Error(`action "${action}" requires payload.id`);
  }

  if (action === "verdict") {
    const result = params.payload?.result;
    if (typeof result !== "string" || !VALID_RESULTS.includes(result as never)) {
      throw new Error(`verdict result must be one of ${VALID_RESULTS.join(", ")}`);
    }
    if (EVIDENCE_REQUIRED.has(result) && !params.payload?.evidence) {
      throw new Error(`verdict result "${result}" requires evidence`);
    }
  }

  if (params.path !== undefined && params.path.trim() === "") {
    throw new Error("path must not be empty");
  }
}

export function buildArgv(action: Action, params: GrillParams): string[] {
  const payloadJson = () => JSON.stringify(params.payload);
  const session = params.session ? ["--session", params.session] : [];

  switch (action) {
    case "new":
      return ["new", payloadJson()];
    case "ask":
      return ["ask", payloadJson(), ...session];
    case "decide":
      return ["decide", payloadJson(), ...session];
    case "revise":
      return ["revise", params.decisionId!, payloadJson(), ...session];
    case "rm":
      return ["rm", params.decisionId!, ...(params.force ? ["--force"] : []), ...session];
    case "verdict":
      return ["verdict", params.decisionId!, payloadJson(), ...session];
    case "plan":
      return ["plan", params.path!, ...session];
    case "mark_pending_execution":
      return [
        "mark-pending-execution",
        ...(params.backlogSlug ? ["--backlog-slug", params.backlogSlug] : []),
        ...session,
      ];
    case "pending_plan":
      return ["pending-plan", ...(params.consume ? ["--consume"] : [])];
    case "next":
      return ["next", ...session];
    case "frontier":
      return ["frontier", ...session];
    case "render":
      return ["render", ...session];
    case "list":
      return ["list"];
    case "show":
      return ["show", ...(params.decisionId ? [params.decisionId] : []), ...session];
  }
}

export default function (pi: ExtensionAPI) {
  pi.registerTool({
    name: "grill",
    label: "Grill",
    description: "Read and mutate grill-me session state: decision points, verdicts, and plans.",
    promptSnippet: "Read or mutate grill-me session state",
    promptGuidelines: [
      "Never invoke grill.py via bash, for any reason, including a plain read like listing sessions or rendering one -- always use grill instead. This applies to every action, not just ones a slash command already told you to use grill for.",
      "grill covers everything grill.py's CLI does: new, ask, decide, revise, rm, verdict, plan, mark_pending_execution, pending_plan, next, frontier, render, list, show. If you're about to compose a `python3 ~/.claude/scripts/grill.py ...` bash command for any of these, use grill with the matching action instead.",
      "grill's payload field is a plain object, not a JSON string -- never hand-encode it.",
      "Sessions are addressed by slug or unique substring via session, never by a number. Omitting session targets the most recent one.",
      'A verdict of "VERIFIED" or "DISPUTED" must carry evidence naming what was actually run and what it showed. "UNVERIFIABLE" is the honest answer when nothing was run -- never a VERIFIED verdict with hand-waved evidence.',
      "Write the plan artifact yourself first, then record its path with action plan. The plan action stores a path; it does not write the file.",
    ],
    parameters: Type.Object({
      action: StringEnum(ACTIONS),
      session: Type.Optional(
        Type.String({
          description:
            "Session slug or unique substring. Defaults to the most recent session. Not accepted by new, list, or pending_plan.",
        }),
      ),
      decisionId: Type.Optional(
        Type.String({
          description:
            "The decision point's id, for revise/rm/verdict. Optional on show, where omitting it prints the whole session.",
        }),
      ),
      payload: Type.Optional(
        Type.Record(Type.String(), Type.Unknown(), {
          description:
            "JSON body for the action. new wants {topic}; ask wants {id, question, reasoning?, depends_on?}; " +
            "decide wants {id, decision, question?, reasoning?, source?, depends_on?}; revise wants " +
            "{decision, depends_on?}; verdict wants {result, evidence} where result is VERIFIED, DISPUTED, " +
            "or UNVERIFIABLE. source is one of user, defaulted, assumed, tested.",
        }),
      ),
      path: Type.Optional(
        Type.String({
          description:
            "plan: path to the already-written plan artifact, conventionally ~/.claude/data/grill/<slug>-plan.md. The file must exist.",
        }),
      ),
      backlogSlug: Type.Optional(
        Type.String({
          description:
            "mark_pending_execution: the dev_status.py item this plan belongs to, if any. Use the backlog item's slug, not the grill session's.",
        }),
      ),
      force: Type.Optional(
        Type.Boolean({
          description:
            "rm: bypass the referential-integrity check, allowing a dangling depends_on to remain.",
        }),
      ),
      consume: Type.Optional(
        Type.Boolean({
          description:
            "pending_plan: clear pending_execution on the printed session, so it is handed out only once.",
        }),
      ),
    }),
    async execute(_toolCallId, params, signal) {
      const typed = params as GrillParams;

      assertFields(typed.action, typed);

      const argv = buildArgv(typed.action, typed);

      const result = await pi.exec("python3", [GRILL_PATH, ...argv], { signal });

      if (result.code !== 0) {
        throw new Error(result.stderr || result.stdout || `grill.py exited ${result.code}`);
      }

      const text = result.stderr ? `${result.stdout}\n\n${result.stderr}` : result.stdout;

      return {
        content: [{ type: "text", text }],
        details: { stdout: result.stdout, stderr: result.stderr },
      };
    },
  });
}

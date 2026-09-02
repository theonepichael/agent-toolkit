import { randomUUID } from "node:crypto";
import { mkdtempSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import type { ExtensionAPI } from "@earendil-works/pi-coding-agent";
import { StringEnum } from "@earendil-works/pi-ai";
import { Type } from "typebox";

// Registers `delegate` -- hand a task to another harness's CLI and get back
// only its conclusion, with the full transcript on disk.
//
// Two problems it removes, both observed in the session corpus (8 of 211
// Claude Code sessions hand-rolled this via raw bash):
//
//   1. The child's streaming transcript lands in the parent's context unless
//      the model remembers a `> /tmp/<slug>.log 2>&1` redirect by hand.
//   2. The invocation form differs per harness in ways that fail QUIETLY,
//      so it gets re-derived, and mis-derived, every time. Both traps below
//      were found by running the real CLIs, not by reading their help text:
//        - opencode's `-p` is `--password`, not `--print`. Passing a prompt
//          to it sends the task as a basic-auth password.
//        - `agy -p --output-format json "task"` makes `-p` swallow the next
//          FLAG as its prompt and silently ignore the real one. Only the
//          attached `-p=<prompt>` form is safe.
//
// Verified output shapes (real runs, 2026-08-30):
//   opencode  NDJSON events; text in {"type":"text","part":{"text":...}}
//   agy       one object; the answer is .response
//   pi        NDJSON; last assistant message_end, content[] type "text"

const HARNESSES = ["opencode", "agy", "pi"] as const;

export type Harness = (typeof HARNESSES)[number];

/** Lines of raw output returned when nothing parseable came back. */
const FALLBACK_TAIL_LINES = 40;

const DEFAULT_TIMEOUT_SECONDS = 900;

export interface DelegateParams {
  harness: Harness;
  prompt: string;
  model?: string;
  provider?: string;
  autoApprove?: boolean;
  cwd?: string;
  timeoutSeconds?: number;
}

export function assertFields(params: DelegateParams): void {
  if (params.prompt === undefined) {
    throw new Error("delegate requires: prompt");
  }
  if (params.prompt.trim() === "") {
    throw new Error("prompt must not be empty");
  }

  // Only pi takes provider and model as separate flags. opencode folds them
  // into one `provider/model` string; agy takes a bare model id.
  if (params.provider !== undefined && params.harness !== "pi") {
    throw new Error(
      `provider is only valid for harness "pi" (got "${params.harness}"). ` +
        'For opencode, put it in model as "provider/model".',
    );
  }

  if (params.harness === "opencode" && params.model !== undefined && !params.model.includes("/")) {
    throw new Error(
      `opencode model must be provider/model (got "${params.model}") -- ` +
        'a bare id resolves to the wrong model rather than erroring, e.g. "opencode-go/glm-5.2"',
    );
  }

  if (
    params.timeoutSeconds !== undefined &&
    (!Number.isInteger(params.timeoutSeconds) || params.timeoutSeconds <= 0)
  ) {
    throw new Error(`timeoutSeconds must be a positive integer, got ${params.timeoutSeconds}`);
  }
}

/** Build the [command, argv] pair for one harness. */
export function buildArgv(params: DelegateParams): [string, string[]] {
  switch (params.harness) {
    case "opencode":
      return [
        "opencode",
        [
          "run",
          "--format",
          "json",
          ...(params.model ? ["-m", params.model] : []),
          ...(params.autoApprove ? ["--auto"] : []),
          // `--` then the prompt as a positional. Never near `-p`, which is
          // opencode's --password.
          "--",
          params.prompt,
        ],
      ];
    case "agy":
      return [
        "agy",
        [
          "--output-format",
          "json",
          ...(params.model ? ["--model", params.model] : []),
          ...(params.autoApprove ? ["--dangerously-skip-permissions"] : []),
          // Attached form, and last: a bare `-p` swallows whatever follows.
          `-p=${params.prompt}`,
        ],
      ];
    case "pi":
      return [
        "pi",
        [
          "-p",
          "--no-session",
          "--mode",
          "json",
          ...(params.provider ? ["--provider", params.provider] : []),
          ...(params.model ? ["--model", params.model] : []),
          // pi ships no permission system, so autoApprove has no flag here
          // (docs/usage.md's Design Principles). Deliberately not an error:
          // the caller shouldn't have to special-case one harness.
          params.prompt,
        ],
      ];
  }
}

function ndjson(raw: string): Record<string, unknown>[] {
  const out: Record<string, unknown>[] = [];
  for (const line of raw.split("\n")) {
    const trimmed = line.trim();
    if (!trimmed) continue;
    try {
      const parsed: unknown = JSON.parse(trimmed);
      if (parsed && typeof parsed === "object" && !Array.isArray(parsed)) {
        out.push(parsed as Record<string, unknown>);
      }
    } catch {
      // A stray log line is expected; none of these streams is pure JSON.
    }
  }
  return out;
}

function textParts(content: unknown): string {
  if (!Array.isArray(content)) return "";
  return content
    .filter(
      (p): p is { type: string; text: string } =>
        !!p && typeof p === "object" && (p as { type?: string }).type === "text",
    )
    .map((p) => p.text)
    .join("");
}

function tail(raw: string): string {
  const lines = raw.trimEnd().split("\n");
  return lines.slice(-FALLBACK_TAIL_LINES).join("\n");
}

/**
 * Pull the child's conclusion out of its raw stdout.
 *
 * Falls back to a tail rather than throwing: a child that crashed or emitted
 * something unexpected still has to report something the parent can act on.
 */
export function extractFinalText(harness: Harness, raw: string): string {
  if (!raw.trim()) return "(no output from the delegated run)";

  if (harness === "agy") {
    try {
      const obj: unknown = JSON.parse(raw.trim());
      const response = (obj as { response?: unknown })?.response;
      if (typeof response === "string" && response.trim()) return response.trim();
    } catch {
      // fall through to the tail
    }
    return tail(raw);
  }

  const events = ndjson(raw);

  if (harness === "opencode") {
    const chunks = events
      .filter((e) => e.type === "text")
      .map((e) => (e.part as { text?: unknown } | undefined)?.text)
      .filter((t): t is string => typeof t === "string" && t !== "");
    if (chunks.length > 0) return chunks.join("").trim();
    return tail(raw);
  }

  // pi: the last assistant message_end carries the final content array, which
  // also holds `thinking` parts -- only the text parts are the answer.
  const assistant = events.filter(
    (e) =>
      e.type === "message_end" &&
      (e.message as { role?: unknown } | undefined)?.role === "assistant",
  );
  const last = assistant[assistant.length - 1];
  if (last) {
    const text = textParts((last.message as { content?: unknown }).content).trim();
    if (text) return text;
  }
  return tail(raw);
}

export default function (pi: ExtensionAPI) {
  pi.registerTool({
    name: "delegate",
    label: "Delegate",
    description:
      "Hand a task to another harness's CLI (opencode, agy, pi) and get back only its final " +
      "answer, with the full transcript written to a log file.",
    promptSnippet: "Delegate a task to another harness and get back only its conclusion",
    promptGuidelines: [
      "Use delegate instead of composing an `opencode run` / `agy -p` / `pi -p` bash command yourself. It gets the per-harness invocation right and keeps the child's transcript out of this session's context.",
      "The child's full transcript goes to a log file whose path is returned. Only its final answer comes back. Read the log with your file tools if you need the detail -- do not re-run the task to see more.",
      "autoApprove grants the child agent unattended tool access (opencode --auto, agy --dangerously-skip-permissions). Without it opencode in particular may make no progress at all headless. Set it deliberately, and say in the prompt what the child may and may not do -- typically: implement, run the tests, then STOP without committing.",
      "opencode model ids are provider/model, e.g. opencode-go/glm-5.2. agy takes a bare model id. Only pi splits provider from model. Confirm a model exists before delegating rather than guessing at the id.",
      "opencode is for personal projects only -- never delegate work-related tasks to it.",
    ],
    parameters: Type.Object({
      harness: StringEnum(HARNESSES),
      prompt: Type.String({
        description:
          "The full task for the child agent. Be explicit about scope and stopping conditions, e.g. 'Implement <path> exactly as written -- TDD, run the full suite, then STOP without committing and report the diff.'",
      }),
      model: Type.Optional(
        Type.String({
          description:
            "Model id. opencode wants provider/model (opencode-go/glm-5.2); agy wants a bare id; pi wants the id, with provider given separately. Omit for the harness default.",
        }),
      ),
      provider: Type.Optional(
        Type.String({ description: "pi only: provider name. Invalid for other harnesses." }),
      ),
      autoApprove: Type.Optional(
        Type.Boolean({
          description:
            "Grant the child unattended tool access. Defaults to false. opencode may make no progress headless without it. No effect for pi, which has no permission system.",
        }),
      ),
      cwd: Type.Optional(
        Type.String({ description: "Directory to run the child in. Defaults to this session's." }),
      ),
      timeoutSeconds: Type.Optional(
        Type.Integer({
          minimum: 1,
          description: `Kill the child after this long. Defaults to ${DEFAULT_TIMEOUT_SECONDS}.`,
        }),
      ),
    }),
    async execute(_toolCallId, params, signal) {
      const typed = params as DelegateParams;

      assertFields(typed);

      const [command, argv] = buildArgv(typed);
      const logPath = join(
        mkdtempSync(join(tmpdir(), "pi-delegate-")),
        `${typed.harness}-${randomUUID().slice(0, 8)}.log`,
      );

      const started = Date.now();
      const result = await pi.exec(command, argv, {
        signal,
        timeout: (typed.timeoutSeconds ?? DEFAULT_TIMEOUT_SECONDS) * 1000,
        ...(typed.cwd ? { cwd: typed.cwd } : {}),
      });
      const seconds = Math.round((Date.now() - started) / 1000);

      // The whole transcript goes to disk, never into the reply -- keeping it
      // out of the parent's context is the point of this tool.
      writeFileSync(
        logPath,
        `$ ${command} ${argv.join(" ")}\n\n${result.stdout}\n${result.stderr}`,
      );

      const final = extractFinalText(typed.harness, result.stdout);

      const header =
        result.code === 0
          ? `${typed.harness} finished in ${seconds}s.`
          : `${typed.harness} exited ${result.code} after ${seconds}s -- treat the answer below as incomplete.`;

      return {
        content: [
          {
            type: "text",
            text: `${header}\n\n${final}\n\nFull transcript: ${logPath}`,
          },
        ],
        details: { harness: typed.harness, code: result.code, seconds, logPath },
      };
    },
  });
}

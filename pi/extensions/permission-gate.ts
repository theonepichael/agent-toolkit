import type { ExtensionAPI } from "@earendil-works/pi-coding-agent";
import { isToolCallEventType } from "@earendil-works/pi-coding-agent";

// Ported from opencode.jsonc's `permission.bash` block (the same allowlist
// opencode reads today) — one house source of truth for "which bash
// commands are safe to run without asking," re-expressed for Pi's
// tool_call event instead of opencode's declarative permission config
// (which has no Pi equivalent: Pi ships no built-in permission-prompt
// system at all — docs/usage.md's Design Principles: "it intentionally
// does not include built-in MCP, sub-agents, permission popups, plan mode,
// to-dos, or background bash").
//
// Only bash is gated here, matching opencode.jsonc's actual current scope
// (no deny entries exist there today, only allow + a "*": "ask" default) —
// opencode.jsonc's separate `external_directory` permission type has no
// direct Pi analog (Pi's file tools have no comparable directory-scoped
// gate) and is out of scope for this port.
//
// Deliberate divergence from opencode.jsonc: the scripts that now have a
// native Pi tool are NOT allowlisted here, unlike every other harness's
// permission config. dev_status.py, grill.py, second_opinion.py,
// standup.py, to_tickets_runner.py and vitals_promotion.py are each
// wrapped by an extension in pi/extensions/ that replaces raw bash calls
// entirely -- leaving the bash pattern on the allowlist would let the
// model bypass the tool silently.
// Dropping to the "*": "ask" default instead makes a direct bash call
// require confirmation (or fail outright in headless mode), pushing
// actual usage toward the tool. The tool's own internal pi.exec calls
// are unaffected -- they never go through the tool_call event this gate
// hooks, since they're plain subprocess calls from already-running
// extension code, not a model-invoked "bash" tool call.
const ALLOW_PATTERNS: string[] = [
  "python3 ~/.claude/scripts/settings_seed_drift_check.py *",
  "python3 ~/.claude/scripts/dotfiles_sync_check.py *",
  "git log*",
  "git status*",
  "git diff*",
  "git show*",
  "git ls-files*",
  "git check-ignore*",
  "git -C * log*",
  "git -C * status*",
  "git -C * diff*",
  "git -C * show*",
  "git -C * ls-files*",
  "git -C * check-ignore*",
  "uv sync*",
  "uv run pytest*",
  "uv run ruff check*",
  "uv run ruff format*",
  // backlog-item --auto's own worktree/baseline/verify steps (CLAUDE.md's
  // worktree-first policy, backlog-item.md steps 3/4/9). git add/git commit
  // stay off this list on purpose -- backlog-item's steps 10-11 require
  // those to stop for live user approval even in --auto mode.
  "git worktree add*",
  "git -C * worktree add*",
  "bun install*",
  "bun run test*",
  "bun run lint*",
  "bunx tsc*",
  "lsof *",
  "ps *",
  "ls*",
  "pwd",
  "which *",
  "head *",
  "tail *",
  "wc *",
  "sort *",
  "uniq *",
  "grep *",
  "rg *",
  "find *",
  "file *",
  "stat *",
  "du *",
  "df *",
  "date*",
  "whoami*",
  "env",
  "printenv*",
  "cat *",
  "sed -n *",
  "strings *",
  "readlink *",
  "jq *",
  "diff *",
  "diff",
  "echo *",
  "echo",
  "head",
  "tail",
  "wc",
  "sort",
  "uniq",
  "cat",
  "pgrep *",
  "ss *",
  "systemctl status*",
  "systemctl is-active*",
  "systemctl is-enabled*",
];

// No deny entries in opencode.jsonc today, but kept as a real tier (not
// folded into "everything not allowed is ask") so a future deny rule is a
// one-line addition here, not a structural change.
const DENY_PATTERNS: string[] = [];

export function patternToRegExp(pattern: string): RegExp {
  // `[\\s\\S]` rather than `.` so a wildcard also covers literal newlines —
  // only escaped continuations and quoted newlines survive segmentation into
  // a segment, and neither can hide a top-level operator.
  const escaped = pattern.replace(/[.+?^${}()|[\]\\]/g, "\\$&").replace(/\*/g, "[\\s\\S]*");
  return new RegExp(`^${escaped}$`);
}

const ALLOW_REGEXPS = ALLOW_PATTERNS.map(patternToRegExp);
const DENY_REGEXPS = DENY_PATTERNS.map(patternToRegExp);

export type Verdict = "allow" | "deny" | "ask";

type ScanState = "normal" | "single" | "double";

/**
 * Quote- and escape-aware scan of one command (assumed trimmed), producing
 * the segments a per-segment allowlist check runs against: operators
 * (`;`, `&&`, `||`, `|`, `&`, newline) split only at top level — outside
 * quotes, unescaped, and not part of a redirection (`2>&1`, `>&2`, `<&0`,
 * `&>`) — while segments keep their original text so the pattern globs
 * match real content. Command substitution (backticks, `$(`, `<(`, `>(`)
 * cannot be resolved statically, so its presence outside single quotes is
 * reported and the caller lands on ask.
 *
 * Deliberately fail-closed wherever bash is subtler than this scan: an
 * unclosed quote or a trailing backslash reports ambiguity, heredoc bodies
 * segment on their newlines (their lines fail the allowlist → ask), and
 * ANSI-C `$'...'` quoting is unmodeled (the scan closes its quote state
 * early, which can only turn quoted text into extra failing segments).
 */
function scanCommand(command: string): {
  segments: string[];
  substitution: boolean;
  ambiguous: boolean;
} {
  const segments: string[] = [];
  let current = "";
  let substitution = false;
  let ambiguous = false;
  let state: ScanState = "normal";

  const endSegment = (): void => {
    const trimmed = current.trim();
    if (trimmed !== "") segments.push(trimmed);
    current = "";
  };

  let i = 0;
  while (i < command.length) {
    const c = command[i]!;

    if (state === "single") {
      if (c === "'") state = "normal";
      current += c;
      i++;
      continue;
    }

    if (state === "double") {
      if (c === '"') {
        state = "normal";
        current += c;
        i++;
        continue;
      }
      // In double quotes bash escapes only " ` \ and newline; before any
      // other character (notably `$` and a backtick) the backslash is
      // literal, so scanning continues and a live substitution after it is
      // still seen (`\\$(x)` triggers, `\$(x)` false-asks — safe direction).
      if (c === "\\" && i + 1 < command.length && ['"', "\\", "\n"].includes(command[i + 1]!)) {
        current += c + command[i + 1]!;
        i += 2;
        continue;
      }
      if (c === "`" || (c === "$" && command[i + 1] === "(")) substitution = true;
      current += c;
      i++;
      continue;
    }

    // normal state
    if (c === "\\") {
      if (i + 1 >= command.length) {
        ambiguous = true;
        break;
      }
      // Escapes the next character unconditionally — including `\;` and a
      // line continuation `\<newline>`, neither of which is a boundary.
      current += c + command[i + 1]!;
      i += 2;
      continue;
    }
    if (c === "'") {
      state = "single";
      current += c;
      i++;
      continue;
    }
    if (c === '"') {
      state = "double";
      current += c;
      i++;
      continue;
    }
    if (
      c === "`" ||
      (c === "$" && command[i + 1] === "(") ||
      ((c === "<" || c === ">") && command[i + 1] === "(")
    ) {
      substitution = true;
      current += c;
      i++;
      continue;
    }
    // A `#` at a word start begins a comment: drop the rest of the line so
    // it cannot manufacture a failing segment.
    if (c === "#" && current.trim() === "") {
      while (i < command.length && command[i] !== "\n") i++;
      continue;
    }
    if (c === ";" || c === "\n") {
      endSegment();
      i++;
      continue;
    }
    if (c === "|") {
      // `||` and `|&` are two-character operator tokens.
      endSegment();
      i += command[i + 1] === "|" || command[i + 1] === "&" ? 2 : 1;
      continue;
    }
    if (c === "&") {
      if (command[i + 1] === ">" || command[i - 1] === ">" || command[i - 1] === "<") {
        // Descriptor redirection (`2>&1`, `>&2`, `<&0`, `&>`, `&>>`) — not a
        // boundary. `&` preceded by a plain argument digit (`sleep 1&echo`)
        // still backgrounds, matching bash.
        current += c;
        i++;
        continue;
      }
      endSegment();
      i += command[i + 1] === "&" ? 2 : 1;
      continue;
    }
    current += c;
    i++;
  }

  if (state !== "normal") ambiguous = true;
  endSegment();
  return { segments, substitution, ambiguous };
}

export function classify(command: string): Verdict {
  const trimmed = command.trim();
  // Empty and operator-only input: nothing runnable, nothing allowlisted — ask.
  if (trimmed === "") return "ask";

  const { segments, substitution, ambiguous } = scanCommand(trimmed);

  // Reduction order: deny anywhere wins, then ambiguity/substitution forces
  // ask, then allow only when at least one segment exists and every one
  // clears the allowlist (never `[]`'s vacuous every()), else ask.
  if (segments.some((s) => DENY_REGEXPS.some((re) => re.test(s)))) return "deny";
  if (substitution || ambiguous) return "ask";
  if (segments.length > 0 && segments.every((s) => ALLOW_REGEXPS.some((re) => re.test(s)))) {
    return "allow";
  }
  return "ask";
}

/**
 * Whether this pi session runs with no human watching it.
 *
 * A swarm worker lives in a herdr tab nobody is looking at, so a confirmation
 * dialog there is a question addressed to no one -- the worker waits forever
 * while `agent_status` still reads "working". So the gate is never armed in
 * such a session, rather than armed and then talked down: swarm-tool.ts
 * spawns each worker tab with `herdr tab create --env PI_AGENT_UNATTENDED=1`,
 * and this is read at module load, before the first tool_call can arrive.
 *
 * This replaces a slash-command handshake (a token, an acknowledgement file,
 * a poll and a deadline) that had to prove the gate came down after the fact.
 * There is nothing to prove now: no prompt to deliver, nothing to time out,
 * and no window in which a worker has been handed work while still armed.
 *
 * What "unattended" means is each gate's own decision, and the two differ on
 * purpose. Here it means allow: this gate's "ask" tier is everything outside
 * a narrow allowlist, and a worker that cannot run tests or git is no worker
 * at all. That is also exactly what the swarm already did by sending
 * /permission-gate-disable, so it is not a new grant. guard-rails.ts reaches
 * the opposite conclusion for the commands it guards -- it blocks them rather
 * than asking -- and keeps every one of its non-interactive rules armed.
 *
 * It fails closed. Anything but exactly "1" -- unset, empty, "0", "true" --
 * leaves the gate armed, so an ordinary interactive session is unaffected and
 * a typo in the variable name cannot silently disarm anything.
 *
 * Read here rather than shared from one module: pi loads each extension
 * separately, and a value imported across extension files does not reliably
 * reach the loaded instance (see guard-rails.ts, which reads the same
 * variable for itself).
 */
export function agentUnattendedByEnv(env: NodeJS.ProcessEnv = process.env): boolean {
  return env.PI_AGENT_UNATTENDED === "1";
}

/**
 * The gate's state at module load, as a function so it can be tested.
 *
 * The read happens once, at load, rather than per tool_call: an explicit
 * /permission-gate-enable must be able to re-arm the gate even in an
 * unattended session, which a per-call environment check would silently
 * override.
 */
export function initialGateEnabled(env: NodeJS.ProcessEnv = process.env): boolean {
  return !agentUnattendedByEnv(env);
}

let enabled = initialGateEnabled();

/** Read-only view of the gate, so a caller (or a test) can confirm its state. */
export function isPermissionGateEnabled(): boolean {
  return enabled;
}

// No exported setter on purpose. One used to live here ("Exported so
// trust-session.ts can flip this gate") -- but pi evaluates every extension
// in its own jiti registry, so trust-session's imported setter ran against a
// private copy and the loaded gate never saw the flip: /trust-session
// reported success while changing nothing (proven live 2026-09-02).
// Cross-extension toggling now arrives over the shared event bus below.

export default function (pi: ExtensionAPI) {
  // /trust-session and /trust-session-off broadcast here (see their comment
  // for why the channel name is a duplicated literal rather than an import).
  // The listener mutates the same module-level `enabled` the tool_call
  // handler reads. Malformed payloads are ignored: a wrong shape must never
  // silently disarm (or spuriously re-arm) the gate.
  pi.events.on("session-trust-changed", (data) => {
    const trusted = (data as { trusted?: unknown } | null | undefined)?.trusted;
    if (typeof trusted === "boolean") enabled = !trusted;
  });

  pi.on("tool_call", async (event, ctx) => {
    if (!enabled) return undefined;
    if (!isToolCallEventType("bash", event)) return;

    const command = event.input.command;
    const verdict = classify(command);

    if (verdict === "allow") return;

    if (verdict === "deny") {
      return { block: true, reason: `Denied by permission-gate: ${command}` };
    }

    // "ask" tier. ctx.hasUI is true in TUI and RPC modes, false in print
    // mode (-p) and JSON mode (docs/extensions.md's ExtensionContext) —
    // there is nothing to confirm through in the false case, so block
    // rather than silently letting an unreviewed command run headless.
    if (!ctx.hasUI) {
      return {
        block: true,
        reason: `Blocked by permission-gate (no UI to confirm through in this mode): ${command}`,
      };
    }

    const ok = await ctx.ui.confirm("Run bash command?", command);
    if (!ok) return { block: true, reason: "Blocked by user" };
  });

  pi.registerCommand("permission-gate-disable", {
    description: "Disable the bash permission-gate for this session (trust mode)",
    handler: async (_args, ctx) => {
      enabled = false;
      ctx.ui.notify("Permission gate disabled — all bash commands run unconfirmed", "warning");
    },
  });

  pi.registerCommand("permission-gate-enable", {
    description: "Re-enable the bash permission-gate",
    handler: async (_args, ctx) => {
      enabled = true;
      ctx.ui.notify("Permission gate enabled", "info");
    },
  });
}

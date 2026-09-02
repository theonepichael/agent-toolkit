import { mkdirSync, renameSync, writeFileSync } from "node:fs";
import { homedir } from "node:os";
import { join } from "node:path";
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

let enabled = true;

// ---------------------------------------------------------------------------
// Acknowledgement files.
//
// swarm-tool.ts's swarm_spawn has to know whether a worker's
// /permission-gate-disable actually took effect before it hands that worker
// real work. It cannot learn that from the worker's terminal: herdr documents
// `agent read --source recent` as the last 80 rendered rows, so every read is
// a bounded sliding window -- a TUI redraw rewrites it rather than appending,
// a stale notice from an earlier command can already be sitting in it, and
// repeated identical lines align at the wrong offset. Every one of those
// yields either a false confirmation or a false failure. So the worker states
// the fact itself, in a file, and the orchestrator polls for it.
//
// The command takes an OPAQUE TOKEN, never a path, and this extension owns
// the directory. A pi session's input is not always the user -- model output,
// a pasted block, or repo content read into context all reach a slash command
// -- so a caller-supplied path would be write-anywhere-the-user-can, and an
// "absolute and ends in .json" check is close to no protection when so much
// tooling keeps its config in a user-owned .json. A token plus a directory
// this extension picks removes traversal, clobbering, and stray directory
// creation by construction.
// ---------------------------------------------------------------------------

const ACK_TOKEN_PATTERN = /^[A-Za-z0-9_-]{8,64}$/;

/**
 * Fixed, well-known ack location, resolved per call rather than captured at
 * module load -- the env override is what lets a test point both the writing
 * and the polling side at a disposable directory (test/AGENTS.md).
 */
export function permissionGateAckDir(): string {
  return (
    process.env.PI_PERMISSION_GATE_ACK_DIR ?? join(homedir(), ".pi", "agent", "permission-gate-ack")
  );
}

export function permissionGateAckPath(token: string): string {
  return join(permissionGateAckDir(), `${token}.json`);
}

export function isValidAckToken(token: string): boolean {
  return ACK_TOKEN_PATTERN.test(token);
}

/** Read-only view of the gate, so a caller (or a test) can confirm a rejected token left it armed. */
export function isPermissionGateEnabled(): boolean {
  return enabled;
}

/**
 * Exported so trust-session.ts can flip this gate alongside guard-rails.ts's
 * without reaching into module-private state.
 *
 * `ackToken` is opt-in and lives here rather than in the slash handler, so
 * there is exactly one programmatic path for turning the gate off -- not a
 * terminal path that writes acks and a library path that quietly does not.
 * A malformed token throws BEFORE `enabled` is touched: disabling anyway
 * would leave the caller unprotected (bash unconfirmed) and unsupervised (an
 * orchestrator waiting out a deadline for an ack that can never arrive), so
 * this fails closed.
 *
 * trust-session.ts deliberately passes no token. That is not a second meaning
 * for "the gate is off" -- an ack answers "did the caller who asked for this
 * get confirmation", and an interactive /trust-session has no caller waiting.
 */
export function setPermissionGateEnabled(value: boolean, opts: { ackToken?: string } = {}): void {
  const token = opts.ackToken;
  if (token !== undefined && !isValidAckToken(token)) {
    throw new Error(
      `invalid permission-gate ack token (expected 8-64 chars of A-Z a-z 0-9 _ -): ${token}`,
    );
  }
  enabled = value;
  if (token !== undefined) writeAck(token);
}

/** Writes to a temp file and renames, so a poller observes the file only once it is complete. */
function writeAck(token: string): void {
  const path = permissionGateAckPath(token);
  mkdirSync(permissionGateAckDir(), { recursive: true });
  const tmp = `${path}.tmp`;
  writeFileSync(tmp, JSON.stringify({ token, disabled_at: new Date().toISOString() }));
  renameSync(tmp, path);
}

export default function (pi: ExtensionAPI) {
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

  // The optional token is named in the description because pi's
  // RegisteredCommand has no separate argument-hint field -- an undocumented
  // positional on a security-adjacent command is how a typo turns into a
  // half-applied command.
  pi.registerCommand("permission-gate-disable", {
    description:
      "Disable the bash permission-gate for this session (trust mode). Optional [token]: write an acknowledgement file for a caller waiting on confirmation (8-64 chars, A-Z a-z 0-9 _ -).",
    handler: async (args, ctx) => {
      const token = args.trim();
      try {
        setPermissionGateEnabled(false, token === "" ? {} : { ackToken: token });
      } catch (e) {
        ctx.ui.notify(`Permission gate NOT disabled: ${String(e)}`, "error");
        return;
      }
      ctx.ui.notify("Permission gate disabled — all bash commands run unconfirmed", "warning");
    },
  });

  pi.registerCommand("permission-gate-enable", {
    description: "Re-enable the bash permission-gate",
    handler: async (_args, ctx) => {
      setPermissionGateEnabled(true);
      ctx.ui.notify("Permission gate enabled", "info");
    },
  });
}

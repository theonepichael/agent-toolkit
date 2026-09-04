import type {
  ExtensionAPI,
  ExtensionCommandContext,
  ExtensionContext,
} from "@earendil-works/pi-coding-agent";
import { createLocalBashOperations } from "@earendil-works/pi-coding-agent";
import type { AutocompleteItem } from "@earendil-works/pi-tui";
import { readdirSync, realpathSync, statSync } from "node:fs";
import { basename, dirname, isAbsolute, join, resolve } from "node:path";

// ============================================================================
// Helpers
// ============================================================================

/** Canonical POSIX single-quote escaping: wraps in '...' and escapes embedded ' as '\''. */
export function bashSingleQuote(s: string): string {
  return "'" + s.replace(/'/g, "'\\''") + "'";
}

/** Expand leading ~ to $HOME. */
export function expandTilde(input: string): string {
  if (input.startsWith("~")) {
    const home = process.env.HOME || "";
    if (home) {
      return home + input.slice(1);
    }
  }
  return input;
}

/** Escape special characters for safe regular expression matching. */
export function escapeRegex(s: string): string {
  return s.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
}

/** Safe statSync that returns null on error rather than throwing. */
export function statSyncSafe(p: string) {
  try {
    return statSync(p);
  } catch {
    return null;
  }
}

// ============================================================================
// Module State
// ============================================================================

let originalCwd: string;
try {
  originalCwd = realpathSync(process.cwd());
} catch {
  originalCwd = process.cwd();
}

let effectiveCwd: string = originalCwd;

let localBashOps = createLocalBashOperations();

export const FILE_TOOLS_REQUIRED_PATH = new Set(["read", "write", "edit"]);
export const FILE_TOOLS_OPTIONAL_PATH = new Set(["grep", "find", "ls"]);
export const CWD_CHANGE_TYPE = "cwd-change" as const;
export const STATUS_KEY = "cwd" as const;

export function getOriginalCwd(): string {
  return originalCwd;
}

export function getEffectiveCwd(): string {
  return effectiveCwd;
}

export function setEffectiveCwd(cwd: string): void {
  effectiveCwd = cwd;
}

export function resetEffectiveCwd(): void {
  effectiveCwd = originalCwd;
}

export function getLocalBashOps() {
  return localBashOps;
}

export function resetBashOps(): void {
  localBashOps = createLocalBashOperations();
}

/** Update the footer status indicator. Clears when cwd matches original. */
export function updateFooterStatus(ctx: ExtensionContext, cwd: string, original: string): void {
  if (!ctx.hasUI) return;
  if (cwd === original) {
    ctx.ui.setStatus(STATUS_KEY, undefined);
    return;
  }
  const home = process.env.HOME || "";
  const displayPath = home ? cwd.replace(new RegExp(`^${escapeRegex(home)}`), "~") : cwd;
  ctx.ui.setStatus(STATUS_KEY, JSON.stringify({ cwd: displayPath }));
}

/**
 * Scan the current session branch for "cwd-change" entries.
 * Returns the last recorded valid directory, or the fallback if none found.
 */
export function restoreCwdFromBranch(ctx: ExtensionContext, fallback: string): string {
  try {
    const branch = ctx.sessionManager?.getBranch?.();
    if (!Array.isArray(branch)) return fallback;

    for (let i = branch.length - 1; i >= 0; i--) {
      const entry = branch[i];
      if (!entry) continue;
      if (
        entry.type === "custom" &&
        entry.customType === CWD_CHANGE_TYPE &&
        entry.data &&
        typeof (entry.data as Record<string, unknown>).cwd === "string"
      ) {
        const candidate = (entry.data as { cwd: string }).cwd;
        const stat = statSyncSafe(candidate);
        if (stat?.isDirectory()) {
          return candidate;
        }
      }
    }
    return fallback;
  } catch {
    return fallback;
  }
}

// ============================================================================
// Directory Completions
// ============================================================================

function resolveSearchDir(
  prefix: string,
  baseCwd: string,
): { searchDir: string; partialName: string } | null {
  const expanded = expandTilde(prefix || "");
  const isTrailingSlash = expanded.endsWith("/");
  let searchDir: string;
  let partialName: string;
  if (isTrailingSlash || expanded === "" || expanded === ".") {
    let dirPath = expanded.slice(0, -1) || ".";
    dirPath = expandTilde(dirPath);
    searchDir = isAbsolute(dirPath) ? dirPath : resolve(baseCwd, dirPath);
    partialName = "";
  } else {
    searchDir = isAbsolute(expanded)
      ? dirname(expanded)
      : resolve(baseCwd, dirname(expanded) || ".");
    partialName = basename(expanded);
  }
  const dirStat = statSyncSafe(searchDir);
  if (!dirStat?.isDirectory()) return null;
  return { searchDir, partialName };
}

function listMatchingDirs(searchDir: string, partialName: string): string[] | null {
  let entries: string[];
  try {
    entries = readdirSync(searchDir);
  } catch {
    return null;
  }
  const matches: string[] = [];
  for (const name of entries) {
    if (partialName && !name.toLowerCase().startsWith(partialName.toLowerCase())) {
      continue;
    }
    const entryStat = statSyncSafe(join(searchDir, name));
    if (!entryStat?.isDirectory()) continue;
    matches.push(name);
  }
  return matches;
}

function buildCompletionValue(name: string, searchDir: string, prefix: string): string {
  const expanded = expandTilde(prefix || "");
  if (isAbsolute(expanded) || prefix.startsWith("~")) {
    let value = join(searchDir, name);
    if (prefix.startsWith("~") && process.env.HOME) {
      value = value.replace(new RegExp(`^${escapeRegex(process.env.HOME)}`), "~");
    }
    return value;
  }
  if (expanded.endsWith("/")) {
    return prefix + name;
  }
  const dirPart = dirname(prefix || "");
  return dirPart === "." ? name : join(dirPart, name);
}

export function getDirectoryCompletions(
  prefix: string,
  baseCwd: string,
): AutocompleteItem[] | null {
  const resolved = resolveSearchDir(prefix, baseCwd);
  if (!resolved) return null;
  const { searchDir, partialName } = resolved;
  const matches = listMatchingDirs(searchDir, partialName);
  if (!matches || matches.length === 0) return null;
  return matches.map((name) => ({
    label: name,
    value: buildCompletionValue(name, searchDir, prefix),
  }));
}

// Regex to find the cwd line in the system prompt
const CWD_PROMPT_REGEX = /Current working directory: .+/;

// ============================================================================
// Extension Entry Point
// ============================================================================

export default function (pi: ExtensionAPI): void {
  // ── /cwd command ──────────────────────────────────────────────────
  pi.registerCommand("cwd", {
    description:
      "Change working directory for tool execution (/cwd <path> or /cwd to show current)",
    handler: async (args: string, ctx: ExtensionCommandContext): Promise<void> => {
      const rawInput = args.trim();
      if (!rawInput) {
        ctx.ui.notify(`Current working directory: ${getEffectiveCwd()}`, "info");
        return;
      }
      const expanded = expandTilde(rawInput);
      const newCwd = resolve(getEffectiveCwd(), expanded);
      try {
        const stat = statSync(newCwd);
        if (!stat.isDirectory()) {
          ctx.ui.notify(`Not a directory: ${newCwd}`, "error");
          return;
        }
      } catch (_err: unknown) {
        const code = (_err as NodeJS.ErrnoException).code;
        const msg =
          code === "ENOENT"
            ? "Directory does not exist"
            : code === "EACCES"
              ? "Permission denied"
              : "Cannot access directory";
        ctx.ui.notify(`${msg}: ${newCwd}`, "error");
        return;
      }

      try {
        setEffectiveCwd(realpathSync(newCwd));
      } catch {
        setEffectiveCwd(newCwd);
      }

      pi.appendEntry(CWD_CHANGE_TYPE, { cwd: getEffectiveCwd() });
      updateFooterStatus(ctx, getEffectiveCwd(), getOriginalCwd());
      pi.events.emit("cwd-change", { cwd: getEffectiveCwd() });
      ctx.ui.notify(`Changed working directory to ${getEffectiveCwd()}`, "info");
    },
    getArgumentCompletions: (argumentPrefix: string): AutocompleteItem[] | null => {
      return getDirectoryCompletions(argumentPrefix, getEffectiveCwd());
    },
  });

  // ── Tool call interception ────────────────────────────────────────
  pi.on("tool_call", (event, _ctx) => {
    if (getEffectiveCwd() === getOriginalCwd()) return undefined;

    if (event.toolName === "bash") {
      const input = event.input as { command: string };
      input.command = `cd ${bashSingleQuote(getEffectiveCwd())} && ${input.command}`;
    } else if (FILE_TOOLS_REQUIRED_PATH.has(event.toolName)) {
      const input = event.input as { path: string };
      if (!input.path || input.path.trim() === "") {
        input.path = getEffectiveCwd();
      } else if (!isAbsolute(input.path)) {
        input.path = resolve(getEffectiveCwd(), input.path);
      }
    } else if (FILE_TOOLS_OPTIONAL_PATH.has(event.toolName)) {
      const input = event.input as { path?: string };
      if (input.path === undefined || input.path === "" || input.path.trim() === "") {
        input.path = getEffectiveCwd();
      } else if (!isAbsolute(input.path)) {
        input.path = resolve(getEffectiveCwd(), input.path);
      }
    }

    return undefined;
  });

  // ── System prompt modification ────────────────────────────────────
  pi.on("before_agent_start", (event, _ctx) => {
    if (getEffectiveCwd() === getOriginalCwd()) return undefined;
    const modified = event.systemPrompt.replace(
      CWD_PROMPT_REGEX,
      `Current working directory: ${getEffectiveCwd()}`,
    );
    return { systemPrompt: modified };
  });

  // ── User ! bash command support ───────────────────────────────────
  pi.on("user_bash", (_event, _ctx) => {
    if (getEffectiveCwd() === getOriginalCwd()) return undefined;
    const escapedCwd = bashSingleQuote(getEffectiveCwd());
    const originalOps = getLocalBashOps();
    return {
      operations: {
        exec: (
          command: string,
          cwd: string,
          options: {
            onData: (data: Buffer) => void;
            signal?: AbortSignal;
            timeout?: number;
            env?: NodeJS.ProcessEnv;
          },
        ) => {
          return originalOps.exec(`cd ${escapedCwd} && ${command}`, cwd, options);
        },
      },
    };
  });

  // ── State restoration ─────────────────────────────────────────────
  pi.on("session_start", (_event, ctx) => {
    setEffectiveCwd(restoreCwdFromBranch(ctx, getOriginalCwd()));
    resetBashOps();
    updateFooterStatus(ctx, getEffectiveCwd(), getOriginalCwd());
    pi.events.emit("cwd-change", { cwd: getEffectiveCwd() });
  });

  pi.on("session_tree", (_event, ctx) => {
    setEffectiveCwd(restoreCwdFromBranch(ctx, getOriginalCwd()));
    updateFooterStatus(ctx, getEffectiveCwd(), getOriginalCwd());
    pi.events.emit("cwd-change", { cwd: getEffectiveCwd() });
  });
}

import { afterEach, beforeEach, describe, expect, test } from "bun:test";
import { mkdirSync, mkdtempSync, realpathSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import type { ExtensionAPI, ExtensionCommandContext } from "@earendil-works/pi-coding-agent";
import cwdExtension, {
  bashSingleQuote,
  expandTilde,
  getEffectiveCwd,
  getOriginalCwd,
  resetEffectiveCwd,
  restoreCwdFromBranch,
  setEffectiveCwd,
} from "../extensions/cwd";

describe("bashSingleQuote", () => {
  test("wraps empty string in single quotes", () => {
    expect(bashSingleQuote("")).toBe("''");
  });

  test("wraps standard ASCII string in single quotes", () => {
    expect(bashSingleQuote("simple_path/dir")).toBe("'simple_path/dir'");
  });

  test("escapes embedded single quotes safely with '\\''", () => {
    expect(bashSingleQuote("bob's folder")).toBe("'bob'\\''s folder'");
    expect(bashSingleQuote("a'b'c")).toBe("'a'\\''b'\\''c'");
  });

  test("handles multiple consecutive single quotes", () => {
    expect(bashSingleQuote("a''b")).toBe("'a'\\'''\\''b'");
  });

  test("handles spaces, tabs, and newlines safely", () => {
    expect(bashSingleQuote("dir with spaces")).toBe("'dir with spaces'");
    expect(bashSingleQuote("line1\nline2")).toBe("'line1\nline2'");
    expect(bashSingleQuote("col1\tcol2")).toBe("'col1\tcol2'");
  });

  test("handles unicode characters", () => {
    expect(bashSingleQuote("földér/ünicøde")).toBe("'földér/ünicøde'");
  });
});

describe("expandTilde", () => {
  const originalHome = process.env.HOME;

  beforeEach(() => {
    process.env.HOME = "/mock/home";
  });

  afterEach(() => {
    process.env.HOME = originalHome;
  });

  test("expands bare ~ to $HOME", () => {
    expect(expandTilde("~")).toBe("/mock/home");
  });

  test("expands ~/path to $HOME/path", () => {
    expect(expandTilde("~/project")).toBe("/mock/home/project");
  });

  test("leaves relative and absolute paths unchanged", () => {
    expect(expandTilde("./project")).toBe("./project");
    expect(expandTilde("../project")).toBe("../project");
    expect(expandTilde("/var/log")).toBe("/var/log");
  });
});

describe("cwdExtension tool_call interception & slash command", () => {
  let tempDir: string;
  let subDir: string;
  let filePath: string;

  beforeEach(() => {
    tempDir = realpathSync(mkdtempSync(join(tmpdir(), "pi-cwd-test-")));
    subDir = join(tempDir, "subdir");
    mkdirSync(subDir);
    filePath = join(tempDir, "file.txt");
    writeFileSync(filePath, "test");
    resetEffectiveCwd();
  });

  afterEach(() => {
    resetEffectiveCwd();
    rmSync(tempDir, { recursive: true, force: true });
  });

  function createMockPi() {
    const commands = new Map<string, any>();
    const listeners = new Map<string, ((...args: any[]) => any)[]>();
    const emittedEvents: { name: string; data: any }[] = [];
    const entries: { type: string; data: any }[] = [];

    const pi = {
      registerCommand: (name: string, def: any) => {
        commands.set(name, def);
      },
      on: (event: string, handler: (...args: any[]) => any) => {
        const list = listeners.get(event) ?? [];
        list.push(handler);
        listeners.set(event, list);
      },
      events: {
        emit: (name: string, data: any) => {
          emittedEvents.push({ name, data });
        },
        on: (_name: string, _handler: any) => {},
      },
      appendEntry: (type: string, data: any) => {
        entries.push({ type, data });
      },
    } as unknown as ExtensionAPI;

    return { pi, commands, listeners, emittedEvents, entries };
  }

  test("registers /cwd command with completions", () => {
    const mock = createMockPi();
    cwdExtension(mock.pi);

    expect(getOriginalCwd()).toBeDefined();
    const cmd = mock.commands.get("cwd");
    expect(cmd).toBeDefined();
    expect(typeof cmd.handler).toBe("function");
    expect(typeof cmd.getArgumentCompletions).toBe("function");
  });

  test("tool_call interception is no-op when effectiveCwd equals originalCwd", () => {
    const mock = createMockPi();
    cwdExtension(mock.pi);
    const toolCallHandler = mock.listeners.get("tool_call")?.[0];
    expect(toolCallHandler).toBeDefined();
    if (!toolCallHandler) throw new Error("toolCallHandler is undefined");

    const bashEvent = { toolName: "bash", input: { command: "ls -la" } };
    toolCallHandler(bashEvent, {} as any);
    expect(bashEvent.input.command).toBe("ls -la");

    const readEvent = { toolName: "read", input: { path: "hello.txt" } };
    toolCallHandler(readEvent, {} as any);
    expect(readEvent.input.path).toBe("hello.txt");
  });

  test("tool_call prefixes bash and resolves file tools when effectiveCwd differs", () => {
    const mock = createMockPi();
    cwdExtension(mock.pi);
    const toolCallHandler = mock.listeners.get("tool_call")?.[0];
    expect(toolCallHandler).toBeDefined();
    if (!toolCallHandler) throw new Error("toolCallHandler is undefined");

    setEffectiveCwd(subDir);
    expect(getEffectiveCwd()).toBe(subDir);

    // Bash command prefix
    const bashEvent = { toolName: "bash", input: { command: "npm test" } };
    toolCallHandler(bashEvent, {} as any);
    expect(bashEvent.input.command).toBe(`cd ${bashSingleQuote(subDir)} && npm test`);

    // Required path tools (read, write, edit)
    const readEvent = { toolName: "read", input: { path: "src/index.ts" } };
    toolCallHandler(readEvent, {} as any);
    expect(readEvent.input.path).toBe(join(subDir, "src/index.ts"));

    // Empty path for required tool resolves to effectiveCwd (not process.cwd())
    const emptyReadEvent = { toolName: "read", input: { path: "" } };
    toolCallHandler(emptyReadEvent, {} as any);
    expect(emptyReadEvent.input.path).toBe(subDir);

    // Optional path tools (ls, grep, find)
    const lsMissingPath = { toolName: "ls", input: {} as { path?: string } };
    toolCallHandler(lsMissingPath, {} as any);
    expect(lsMissingPath.input.path).toBe(subDir);

    const grepRelPath = { toolName: "grep", input: { path: "nested" } };
    toolCallHandler(grepRelPath, {} as any);
    expect(grepRelPath.input.path).toBe(join(subDir, "nested"));

    // Absolute paths remain unchanged
    const absPath = "/var/log/app.log";
    const absEvent = { toolName: "read", input: { path: absPath } };
    toolCallHandler(absEvent, {} as any);
    expect(absEvent.input.path).toBe(absPath);
  });

  test("/cwd changes effectiveCwd, emits cwd-change, and appends session entry", async () => {
    const mock = createMockPi();
    cwdExtension(mock.pi);
    const cmd = mock.commands.get("cwd");

    const notifications: { message: string; level: string }[] = [];
    const ctx = {
      ui: {
        notify: (message: string, level: string) => {
          notifications.push({ message, level });
        },
      },
    } as unknown as ExtensionCommandContext;

    // Switch to valid subdir
    await cmd.handler(subDir, ctx);
    expect(getEffectiveCwd()).toBe(subDir);
    expect(mock.emittedEvents).toEqual([{ name: "cwd-change", data: { cwd: subDir } }]);
    expect(mock.entries).toEqual([{ type: "cwd-change", data: { cwd: subDir } }]);
    expect(notifications[0].level).toBe("info");

    // Invalid non-existent dir
    await cmd.handler("/non/existent/path/xyz", ctx);
    expect(
      notifications.some((n) => n.level === "error" && n.message.includes("does not exist")),
    ).toBe(true);
    expect(getEffectiveCwd()).toBe(subDir);

    // File instead of dir
    await cmd.handler(filePath, ctx);
    expect(
      notifications.some((n) => n.level === "error" && n.message.includes("Not a directory")),
    ).toBe(true);
    expect(getEffectiveCwd()).toBe(subDir);
  });

  test("before_agent_start updates Current working directory line in prompt", () => {
    const mock = createMockPi();
    cwdExtension(mock.pi);
    const promptHandler = mock.listeners.get("before_agent_start")?.[0];
    expect(promptHandler).toBeDefined();
    if (!promptHandler) throw new Error("promptHandler is undefined");

    // When cwd has not changed, prompt is unmodified (returns undefined)
    const unmodified = promptHandler(
      { systemPrompt: "Current working directory: /old\nDo work." },
      {} as any,
    );
    expect(unmodified).toBeUndefined();

    // When cwd changed, prompt is updated
    setEffectiveCwd(subDir);
    const modified = promptHandler(
      { systemPrompt: "Current working directory: /old\nDo work." },
      {} as any,
    );
    expect(modified).toEqual({
      systemPrompt: `Current working directory: ${subDir}\nDo work.`,
    });
  });

  test("restoreCwdFromBranch restores latest valid directory from session branch", () => {
    const validDir = subDir;
    const deletedDir = join(tempDir, "deleted");

    const mockCtx = {
      sessionManager: {
        getBranch: () => [
          { type: "message", data: {} },
          { type: "custom", customType: "cwd-change", data: { cwd: validDir } },
          { type: "custom", customType: "other", data: {} },
          { type: "custom", customType: "cwd-change", data: { cwd: deletedDir } },
        ],
      },
    } as any;

    // deletedDir does not exist, so it skips to validDir
    const restored = restoreCwdFromBranch(mockCtx, "/fallback");
    expect(restored).toBe(validDir);

    // When no branch entries match
    const emptyCtx = { sessionManager: { getBranch: () => [] } } as any;
    expect(restoreCwdFromBranch(emptyCtx, "/fallback")).toBe("/fallback");
  });
});

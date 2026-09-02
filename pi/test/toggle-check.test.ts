import { describe, expect, test } from "bun:test";
import guardRails from "../extensions/guard-rails";
import permissionGate from "../extensions/permission-gate";
import trustSession from "../extensions/trust-session";

type Handler = (event: any, ctx: any) => Promise<any> | any;

function makeFakePi() {
  const handlers: Handler[] = [];
  const commands: Record<string, { handler: (args: string, ctx: any) => Promise<void> }> = {};
  return {
    pi: {
      on(_event: string, handler: Handler) {
        handlers.push(handler);
      },
      registerCommand(name: string, opts: { handler: (args: string, ctx: any) => Promise<void> }) {
        commands[name] = opts;
      },
      exec: async () => ({ code: 0, stdout: "", stderr: "" }),
    },
    handlers,
    commands,
  };
}

function makeCtx(confirmResult: boolean) {
  const confirmTitles: string[] = [];
  return {
    ctx: {
      hasUI: true,
      cwd: "/repo",
      ui: {
        confirm: async (title: string) => {
          confirmTitles.push(title);
          return confirmResult;
        },
        notify: () => {},
      },
    },
    getConfirmTitles: () => confirmTitles,
  };
}

async function fireCommand(handlers: Handler[], command: string, confirmResult: boolean) {
  const { ctx, getConfirmTitles } = makeCtx(confirmResult);
  const event = { toolName: "bash", input: { command } };
  let finalResult: any;
  for (const h of handlers) {
    const r = await h(event, ctx);
    if (r !== undefined) {
      finalResult = r;
      break;
    }
  }
  return { finalResult, titles: getConfirmTitles() };
}

// Simulates the real ~/.pi/agent/extensions/ dir: guard-rails.ts,
// permission-gate.ts, and trust-session.ts all loaded together, subscribed
// to the same tool_call event in registration order, the way Pi's event bus
// actually runs them.
describe("real-world layout: guard-rails, permission-gate, and trust-session all loaded", () => {
  test("disabling guard-rails alone does not stop sudo from being asked about -- permission-gate still gates it", async () => {
    const { pi, handlers, commands } = makeFakePi();
    guardRails(pi as any);
    permissionGate(pi as any);

    const before = await fireCommand(handlers, "sudo apt update", true);
    expect(before.titles).toEqual(["⚠️ sudo", "Run bash command?"]);

    await commands["guard-rails-disable"]!.handler("", { ui: { notify: () => {} } });

    const after = await fireCommand(handlers, "sudo apt update", true);
    expect(after.titles).toEqual(["Run bash command?"]);
  });

  test("/trust-session disables both gates at once", async () => {
    const { pi, handlers, commands } = makeFakePi();
    guardRails(pi as any);
    permissionGate(pi as any);
    trustSession(pi as any);

    expect(commands["trust-session"]).toBeDefined();
    await commands["trust-session"]!.handler("", { ui: { notify: () => {} } });

    const sudoResult = await fireCommand(handlers, "sudo apt update", true);
    expect(sudoResult.titles).toEqual([]);
    expect(sudoResult.finalResult).toBeUndefined();

    const unlisted = await fireCommand(handlers, "git merge some-branch", true);
    expect(unlisted.titles).toEqual([]);
    expect(unlisted.finalResult).toBeUndefined();
  });

  test("/trust-session-off restores both gates", async () => {
    const { pi, handlers, commands } = makeFakePi();
    guardRails(pi as any);
    permissionGate(pi as any);
    trustSession(pi as any);

    await commands["trust-session"]!.handler("", { ui: { notify: () => {} } });
    await commands["trust-session-off"]!.handler("", { ui: { notify: () => {} } });

    const result = await fireCommand(handlers, "sudo apt update", true);
    expect(result.titles).toEqual(["⚠️ sudo", "Run bash command?"]);
  });
});

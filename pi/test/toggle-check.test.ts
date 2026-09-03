import { afterEach, describe, expect, test } from "bun:test";
import guardRails from "../extensions/guard-rails";
import permissionGate from "../extensions/permission-gate";
import trustSession from "../extensions/trust-session";

type Handler = (event: any, ctx: any) => Promise<any> | any;

function makeFakePi() {
  const handlers: Handler[] = [];
  const commands: Record<string, { handler: (args: string, ctx: any) => Promise<void> }> = {};
  // A real pub/sub bus, because the event channels are dynamic strings: a
  // typed-on but never-emitted (or vice versa) channel fails silently in pi,
  // so the fake has to actually route.
  const busListeners = new Map<string, Set<(data: unknown) => void>>();
  const events = {
    emit(channel: string, data: unknown) {
      for (const listener of busListeners.get(channel) ?? []) listener(data);
    },
    on(channel: string, handler: (data: unknown) => void) {
      let set = busListeners.get(channel);
      if (!set) {
        set = new Set();
        busListeners.set(channel, set);
      }
      set.add(handler);
      return () => set.delete(handler);
    },
  };
  return {
    pi: {
      on(_event: string, handler: Handler) {
        handlers.push(handler);
      },
      registerCommand(name: string, opts: { handler: (args: string, ctx: any) => Promise<void> }) {
        commands[name] = opts;
      },
      exec: async () => ({ code: 0, stdout: "", stderr: "" }),
      events,
    },
    handlers,
    commands,
    events,
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

// guard-rails.ts, permission-gate.ts, and trust-session.ts subscribed to the
// same tool_call event in registration order. This is NOT the layout pi
// builds, and saying so once kept a real bug green: pi loads each extension
// through its own jiti instance with module cache disabled, so module state
// is private per extension and nothing here can catch a cross-extension
// state-sharing bug (that is the jiti block below). What this block does
// cover is composition behavior within one graph: which gate asks what when
// only one of them is disabled.
describe("single module graph: guard-rails, permission-gate, and trust-session imported together", () => {
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

// ---------------------------------------------------------------------------
// Loaded in isolated module instances, wired the way pi wires them.
//
// pi evaluates every extension file through its own jiti instance with
// moduleCache disabled (verified in pi 0.84.4's loadExtensionModule), so a
// relative import from one extension re-evaluates the target module as a
// private copy and module state is NEVER shared across extensions. The first
// /trust-session shipped green because the block above imports all three
// files into one bun module graph; against a real pi it flipped copies
// nothing reads.
//
// bun test cannot replicate jiti itself -- and measuring it said don't try:
// under bun, jiti dedupes some files against bun's native module registry
// (trust-session came back identical) while re-evaluating others (guard-rails
// did not), so a jiti-based test cannot promise the isolation it asserts and
// can pass green against exactly the bug it exists to catch -- observed live
// while writing this test. What bun CAN guarantee is the PROPERTY pi's
// loading has: query-string imports (".ts?worker=a") are distinct module
// instances, and a relative import from inside a query-loaded module
// resolves to the plain path, i.e. a DIFFERENT instance than the loaded one.
// That is deterministic, needs no extra dependency, and is the property the
// fix relies on: no cross-extension state may move through imports, only
// through the shared event bus these instances are all wired to (one bus per
// fake pi, exactly how pi wires ExtensionAPI.events).
//
// LIVE VERIFICATION NOTE: like every test here, this block proves wiring
// inside one bun process. The end-to-end proof that /trust-session works
// against a real running pi was done live (2026-09-02, herdr tab: gates
// blocked, /trust-session notified, gate still armed) -- that observation is
// what this block exists to prevent from regressing silently.
// ---------------------------------------------------------------------------

const TRUST_CHANNEL = "session-trust-changed";

type IsolatedExtension = { default: (pi: any) => void };

// import() with a runtime-built specifier: TS cannot type a "?query" module
// path (there is no module declaration shape for it), and the isolation is
// the whole point -- see the block comment above.
function importIsolated(file: string, worker: string): Promise<IsolatedExtension> {
  const spec = "../extensions/" + file + "?worker=" + worker;
  return import(spec) as Promise<IsolatedExtension>;
}

async function loadIsolatedInstances(): Promise<{
  handlers: Handler[];
  commands: Record<string, { handler: (args: string, ctx: any) => Promise<void> }>;
  events: any;
}> {
  const { pi, handlers, commands, events } = makeFakePi();
  // Distinct query strings -> distinct module instances: module state in one
  // is invisible to the others, and to the plain-path imports above.
  const guard = await importIsolated("guard-rails.ts", "guard");
  const gate = await importIsolated("permission-gate.ts", "gate");
  const trust = await importIsolated("trust-session.ts", "trust");
  guard.default(pi as any);
  gate.default(pi as any);
  trust.default(pi as any);
  return { handlers, commands, events };
}

describe("loaded in isolated module instances, wired to one shared event bus", () => {
  test("/trust-session disables both gates the loaded instances actually enforce", async () => {
    const { handlers, commands } = await loadIsolatedInstances();

    expect(commands["trust-session"]).toBeDefined();
    await commands["trust-session"].handler("", { ui: { notify: () => {} } });

    const sudo = await fireCommand(handlers, "sudo apt update", true);
    expect(sudo.titles).toEqual([]);
    expect(sudo.finalResult).toBeUndefined();

    const unlisted = await fireCommand(handlers, "git merge some-branch", true);
    expect(unlisted.titles).toEqual([]);
    expect(unlisted.finalResult).toBeUndefined();
  });

  test("/trust-session-off restores both gates", async () => {
    const { handlers, commands } = await loadIsolatedInstances();

    await commands["trust-session"].handler("", { ui: { notify: () => {} } });
    await commands["trust-session-off"].handler("", { ui: { notify: () => {} } });

    const result = await fireCommand(handlers, "sudo apt update", true);
    expect(result.titles).toEqual(["⚠️ sudo", "Run bash command?"]);
  });

  test("a malformed event payload is ignored, leaving the gates armed", async () => {
    const { handlers, events } = await loadIsolatedInstances();

    events.emit(TRUST_CHANNEL, { trusted: "yes" });
    events.emit(TRUST_CHANNEL, undefined);

    const result = await fireCommand(handlers, "sudo apt update", true);
    expect(result.titles).toEqual(["⚠️ sudo", "Run bash command?"]);
  });
});

// ---------------------------------------------------------------------------
// Unattended sessions.
//
// A swarm worker lives in a herdr tab nobody is watching, so ctx.hasUI is true
// -- it really is a TUI -- while no human will ever answer a dialog in it. A
// worker that reached guard-rails' rm -rf or sudo confirmation waited forever
// while herdr still reported it as working, and the run stopped making
// progress with no signal (observed live, 2026-09-02).
//
// The resolution is to BLOCK, not to allow. Disabling guard-rails wholesale
// would also drop protected-path writes and the git-commit-on-main worktree
// policy, which is far more autonomy than the stranding problem calls for.
// These tests pin both halves: the two dialogs become refusals, and nothing
// else changes.
// ---------------------------------------------------------------------------

describe("guard-rails in an unattended session", () => {
  const prior = process.env.PI_AGENT_UNATTENDED;

  function unattended(): void {
    process.env.PI_AGENT_UNATTENDED = "1";
  }

  afterEach(() => {
    if (prior === undefined) delete process.env.PI_AGENT_UNATTENDED;
    else process.env.PI_AGENT_UNATTENDED = prior;
  });

  test("rm -rf is refused outright, with no dialog raised", async () => {
    unattended();
    const { pi, handlers } = makeFakePi();
    guardRails(pi as any);

    // confirmResult true: had a dialog been raised at all, the command would
    // have been allowed through. It must never get that far.
    const res = await fireCommand(handlers, "rm -rf /tmp/scratch", true);

    expect(res.titles).toEqual([]);
    expect(res.finalResult?.block).toBe(true);
    expect(res.finalResult?.reason).toContain("no human is watching");
  });

  test("sudo is refused outright, with no dialog raised", async () => {
    unattended();
    const { pi, handlers } = makeFakePi();
    guardRails(pi as any);

    const res = await fireCommand(handlers, "sudo apt update", true);

    expect(res.titles).toEqual([]);
    expect(res.finalResult?.block).toBe(true);
    expect(res.finalResult?.reason).toContain("no human is watching");
  });

  test("an attended session still asks, rather than refusing", async () => {
    // The same handler, same command, only the environment differs -- this is
    // what makes the two tests above about unattendedness and not about
    // guard-rails having simply started blocking rm -rf for everyone.
    const { pi, handlers } = makeFakePi();
    guardRails(pi as any);

    const res = await fireCommand(handlers, "rm -rf /tmp/scratch", true);

    expect(res.titles).toEqual(["⚠️ rm -rf"]);
    expect(res.finalResult).toBeUndefined();
  });

  test("everything else guard-rails protects stays armed", async () => {
    unattended();
    const { pi, handlers } = makeFakePi();
    guardRails(pi as any);

    // A protected-path write is blocked without ever consulting a human, so
    // it is unaffected by whether one is present -- and must stay that way.
    const { ctx } = makeCtx(true);
    const event = { toolName: "write", input: { path: "repo/.env.production" } };
    let result: any;
    for (const h of handlers) {
      const r = await h(event, ctx);
      if (r !== undefined) {
        result = r;
        break;
      }
    }

    expect(result?.block).toBe(true);
    expect(result?.reason).toContain("protected");
  });
});

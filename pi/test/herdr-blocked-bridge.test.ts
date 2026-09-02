import { describe, expect, test } from "bun:test";
import registerBridge from "../extensions/herdr-blocked-bridge";

// herdr learns an agent is blocked from one signal only: the custom
// `herdr:blocked` event, which its own integration (herdr-agent-state.ts,
// shipped by herdr and deliberately ignored by links.toml) listens for.
// Nothing else reports it -- there is no screen detection once that
// integration is active.
//
// Pi does fire a native `ui_prompt_start` whenever it begins waiting on a
// blocking extension UI prompt, and `ui_prompt_end` when it stops. Verified
// live 2026-09-02 against a real pi in a herdr pane: permission-gate's
// ctx.ui.confirm produced
//   {"type":"ui_prompt_start","reason":"ui_prompt","kind":"confirm",
//    "title":"Run bash command?"}
// while herdr's agent_status stayed "working" for 40 consecutive polls,
// because nothing bridges the two. That gap is what stranded swarm workers
// on permission prompts: an agent waiting on a human looked exactly like an
// agent making progress.
//
// This bridge is that missing wire, and it is deliberately generic -- every
// present and future ctx.ui.* prompt is covered without each extension
// having to remember to emit anything.

interface Emitted {
  event: string;
  data: unknown;
}

function makeStubPi() {
  const handlers = new Map<string, (event: unknown) => unknown>();
  const emitted: Emitted[] = [];
  const pi = {
    on(event: string, handler: (event: unknown) => unknown) {
      handlers.set(event, handler);
    },
    events: {
      emit(event: string, data: unknown) {
        emitted.push({ event, data });
      },
    },
  };
  return { pi, handlers, emitted };
}

function start(kind: string, title?: string) {
  return { type: "ui_prompt_start", reason: "ui_prompt", kind, title };
}
function end(kind: string, title?: string) {
  return { type: "ui_prompt_end", reason: "ui_prompt", kind, title };
}

describe("herdr blocked bridge", () => {
  test("a starting UI prompt marks the agent blocked", () => {
    const { pi, handlers, emitted } = makeStubPi();
    registerBridge(pi as unknown as Parameters<typeof registerBridge>[0]);

    handlers.get("ui_prompt_start")?.(start("confirm", "Run bash command?"));

    expect(emitted).toHaveLength(1);
    expect(emitted[0]?.event).toBe("herdr:blocked");
    expect(emitted[0]?.data).toMatchObject({ active: true, label: "Run bash command?" });
  });

  test("an ending UI prompt clears it", () => {
    const { pi, handlers, emitted } = makeStubPi();
    registerBridge(pi as unknown as Parameters<typeof registerBridge>[0]);

    handlers.get("ui_prompt_start")?.(start("select", "Commit these changes?"));
    handlers.get("ui_prompt_end")?.(end("select", "Commit these changes?"));

    expect(emitted.map((e) => (e.data as { active: boolean }).active)).toEqual([true, false]);
  });

  test("every prompt kind is bridged, not just the picker", () => {
    // The bug was that only question-tool.ts emitted, so permission-gate's
    // confirm and everything else went unreported. Nothing here may be
    // kind-specific.
    for (const kind of ["select", "confirm", "input", "editor", "custom"]) {
      const { pi, handlers, emitted } = makeStubPi();
      registerBridge(pi as unknown as Parameters<typeof registerBridge>[0]);
      handlers.get("ui_prompt_start")?.(start(kind, `a ${kind} prompt`));
      expect(emitted).toHaveLength(1);
      expect(emitted[0]?.data).toMatchObject({ active: true });
    }
  });

  test("a titleless prompt still reports a usable label", () => {
    const { pi, handlers, emitted } = makeStubPi();
    registerBridge(pi as unknown as Parameters<typeof registerBridge>[0]);

    // title is optional on the event; the label surfaces in `herdr agent get`
    // for anything watching over the socket, so it must never be empty.
    handlers.get("ui_prompt_start")?.(start("input"));

    const first = emitted[0];
    expect(first).toBeDefined();
    const label = (first?.data as { label?: string } | undefined)?.label ?? "";
    expect(label.length).toBeGreaterThan(0);
    expect(label).toContain("input");
  });

  test("both directions are registered", () => {
    const { pi, handlers } = makeStubPi();
    registerBridge(pi as unknown as Parameters<typeof registerBridge>[0]);

    // A start with no matching end would pin the agent at blocked forever,
    // since herdr's integration counts these.
    expect(handlers.has("ui_prompt_start")).toBe(true);
    expect(handlers.has("ui_prompt_end")).toBe(true);
  });
});

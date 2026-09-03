import { describe, expect, test } from "bun:test";
import { mkdtempSync, mkdirSync, readFileSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import type { Api, Model, ModelCost } from "@earendil-works/pi-ai";
import {
  columnWidths,
  compareModels,
  dedupeAndSort,
  effortBar,
  effortLevelsFor,
  formatContext,
  formatCost,
  matchIndices,
  modelCostScore,
  modelRef,
  prefillEffort,
  rowLabel,
  saveDefaultModel,
  saveEnabledModels,
  unionEnabled,
} from "../extensions/model-picker";

function model(overrides: Partial<Model<Api>> = {}): Model<Api> {
  return {
    id: "claude-sonnet-4-5",
    name: "Claude Sonnet 4.5",
    api: "anthropic-messages",
    provider: "anthropic",
    baseUrl: "https://api.anthropic.com",
    reasoning: true,
    input: ["text"],
    cost: { input: 3, output: 15, cacheRead: 0.3, cacheWrite: 3.75 },
    contextWindow: 200_000,
    maxTokens: 8192,
    ...overrides,
  };
}

describe("formatContext", () => {
  test("renders token counts as K and M", () => {
    expect(formatContext(200_000)).toBe("200K");
    expect(formatContext(1_000_000)).toBe("1.0M");
    expect(formatContext(1_048_576)).toBe("1.0M");
    expect(formatContext(128_000)).toBe("128K");
    expect(formatContext(2_000_000)).toBe("2.0M");
  });

  test("missing or non-positive contextWindow degrades to a dash", () => {
    expect(formatContext(undefined)).toBe("—");
    expect(formatContext(0)).toBe("—");
    expect(formatContext(-5)).toBe("—");
  });

  test("small counts render as bare numbers", () => {
    expect(formatContext(999)).toBe("999");
  });
});

describe("formatCost", () => {
  test("renders per-1M input / output rates", () => {
    expect(formatCost({ input: 3, output: 15, cacheRead: 0, cacheWrite: 0 })).toBe(
      "$3.00 / $15.00",
    );
  });

  test("zero on both sides reads as free", () => {
    expect(formatCost({ input: 0, output: 0, cacheRead: 0, cacheWrite: 0 })).toBe("free");
  });

  test("missing rates degrade to a dash, not $0.00", () => {
    expect(formatCost(undefined)).toBe("—");
    expect(formatCost({} as ModelCost)).toBe("—");
  });
});

describe("dedupeAndSort", () => {
  test("dedupes by provider/id keeping the first occurrence", () => {
    const first = model({ name: "first" });
    const dupe = model({ name: "second" });
    expect(dedupeAndSort([first, dupe])).toEqual([first]);
  });

  test("sorts by provider then id", () => {
    const sorted = dedupeAndSort([
      model({ provider: "openai", id: "gpt-5" }),
      model({ provider: "anthropic", id: "claude-opus" }),
      model({ provider: "openai", id: "gpt-4" }),
      model({ provider: "anthropic", id: "claude-sonnet" }),
    ]);
    expect(sorted.map(modelRef)).toEqual([
      "anthropic/claude-opus",
      "anthropic/claude-sonnet",
      "openai/gpt-4",
      "openai/gpt-5",
    ]);
  });

  test("compareModels sorts by price ascending and descending with missing prices last", () => {
    const sonnet = model({
      provider: "anthropic",
      id: "sonnet",
      cost: { input: 3, output: 15, cacheRead: 0, cacheWrite: 0 },
    });
    const free = model({
      provider: "ollama",
      id: "llama",
      cost: { input: 0, output: 0, cacheRead: 0, cacheWrite: 0 },
    });
    const mini = model({
      provider: "openai",
      id: "mini",
      cost: { input: 0.15, output: 0.6, cacheRead: 0, cacheWrite: 0 },
    });
    const unknown = model({ provider: "custom", id: "x", cost: undefined });

    const list = [sonnet, free, mini, unknown];

    expect(modelCostScore(free.cost)).toBe(0);
    expect(modelCostScore(sonnet.cost)).toBe(18);
    expect(modelCostScore(unknown.cost)).toBe(Number.POSITIVE_INFINITY);

    const asc = list.slice().sort((a, b) => compareModels(a, b, "price-asc"));
    expect(asc.map(modelRef)).toEqual([
      "ollama/llama",
      "openai/mini",
      "anthropic/sonnet",
      "custom/x",
    ]);

    const desc = list.slice().sort((a, b) => compareModels(a, b, "price-desc"));
    expect(desc.map(modelRef)).toEqual([
      "anthropic/sonnet",
      "openai/mini",
      "ollama/llama",
      "custom/x",
    ]);
  });
});

describe("effortLevelsFor", () => {
  test("non-reasoning models offer only off", () => {
    expect(effortLevelsFor(model({ reasoning: false }))).toEqual(["off"]);
  });

  test("reasoning models always include off and drop null-mapped levels", () => {
    const m = model({
      thinkingLevelMap: { off: "off", low: "low", medium: null, high: "high", xhigh: null },
    });
    expect(effortLevelsFor(m)).toEqual(["off", "minimal", "low", "high", "max"]);
  });

  test("a missing thinkingLevelMap offers every level (provider defaults apply)", () => {
    expect(effortLevelsFor(model({ thinkingLevelMap: undefined }))).toEqual([
      "off",
      "minimal",
      "low",
      "medium",
      "high",
      "xhigh",
      "max",
    ]);
  });
});

describe("prefillEffort", () => {
  const m = model({ thinkingLevelMap: { off: "off", high: "high", medium: null } });

  test("the active model pre-fills its current level when still supported", () => {
    expect(prefillEffort(m, "anthropic/claude-sonnet-4-5", "high")).toBe("high");
    expect(prefillEffort(m, "anthropic/claude-sonnet-4-5", "off")).toBe("off");
  });

  test("an unsupported current level falls back to off, never a guessed mid-level", () => {
    expect(prefillEffort(m, "anthropic/claude-sonnet-4-5", "medium")).toBe("off");
  });

  test("non-active models pre-fill off", () => {
    expect(prefillEffort(m, "openai/gpt-5", "high")).toBe("off");
    expect(prefillEffort(m, undefined, "high")).toBe("off");
  });
});

describe("unionEnabled", () => {
  const allIds = ["anthropic/a", "anthropic/b", "openai/c"];

  test("null (all-enabled) stays null", () => {
    expect(unionEnabled(null, ["anthropic/a"], allIds)).toBeNull();
  });

  test("target ids are added without removing existing entries or patterns", () => {
    expect(unionEnabled(["anthropic/a", "openai/*"], ["openai/c"], allIds)).toEqual([
      "anthropic/a",
      "openai/*",
      "openai/c",
    ]);
  });

  test("already-present targets are not duplicated", () => {
    expect(unionEnabled(["openai/c"], ["openai/c", "anthropic/a"], allIds)).toEqual([
      "openai/c",
      "anthropic/a",
    ]);
  });

  test("full coverage collapses to null", () => {
    expect(unionEnabled(["anthropic/a", "anthropic/b"], ["openai/c"], allIds)).toBeNull();
  });

  test("partial coverage stays a list", () => {
    expect(unionEnabled(["anthropic/a"], ["anthropic/b"], allIds)).toEqual([
      "anthropic/a",
      "anthropic/b",
    ]);
  });
});

describe("matchIndices", () => {
  test("returns in-order character positions", () => {
    expect(matchIndices("opencode-go/glm-5.3", "glm")).toEqual([12, 13, 14]);
  });

  test("is case-insensitive", () => {
    expect(matchIndices("openai/gpt", "GPT")).toEqual([7, 8, 9]);
  });

  test("a query that cannot match in order yields no indices", () => {
    expect(matchIndices("anthropic/claude", "xyz")).toEqual([]);
  });
});

describe("effortBar", () => {
  const levels = effortLevelsFor(model({ thinkingLevelMap: undefined }));

  test("off fills nothing", () => {
    expect(effortBar(levels, "off")).toEqual({ filled: 0, total: levels.length - 1 });
  });

  test("each level above off fills one more cell", () => {
    expect(effortBar(levels, "minimal").filled).toBe(1);
    expect(effortBar(levels, "max").filled).toBe(levels.length - 1);
  });
});

describe("rowLabel", () => {
  test("shows provider/id, context, and per-1M cost", () => {
    expect(rowLabel(model({ reasoning: false }))).toBe(
      "anthropic/claude-sonnet-4-5  200K ctx  $3.00 / $15.00",
    );
  });

  test("badges mark reasoning, vision, missing auth, and the active model", () => {
    const m = model({ reasoning: false, input: ["text", "image"] });
    expect(rowLabel(m)).toContain("[vision]");
    expect(rowLabel(model({ reasoning: false }), { hasAuth: false })).toContain("[no key]");
    expect(rowLabel(model(), { isActive: true })).toContain("active");
  });

  test("columnWidths + rowLabel align id, context, and cost into columns", () => {
    const models = [
      model({ id: "a" }),
      model({ provider: "opencode-go", id: "a-very-long-model-id", contextWindow: 1_048_576 }),
      model({ provider: "x", id: "b", cost: { input: 0, output: 0, cacheRead: 0, cacheWrite: 0 } }),
    ];
    const widths = columnWidths(models);
    const rows = models.map((m) => rowLabel(m, { ...widths, refWidth: widths.refWidth }));
    const costStart = rows.map((r) => r.indexOf("  ctx"));
    expect(costStart.every((c) => c === costStart[0])).toBe(true);
    const badgeStart = rows.map((r) =>
      r.indexOf("$") === -1 ? r.indexOf("free") : r.indexOf("$"),
    );
    expect(badgeStart.every((c) => c === badgeStart[0])).toBe(true);
  });

  test("missing metadata degrades inside the row instead of NaN", () => {
    expect(
      rowLabel(
        model({
          reasoning: false,
          contextWindow: 0,
          cost: { input: 0, output: 0, cacheRead: 0, cacheWrite: 0 },
        }),
      ),
    ).toBe("anthropic/claude-sonnet-4-5  — ctx  free");
  });
});

describe("settings persistence", () => {
  function makeAgentDir(): string {
    return mkdtempSync(join(tmpdir(), "model-picker-test-"));
  }

  function readGlobalSettings(agentDir: string): Record<string, unknown> {
    return JSON.parse(readFileSync(join(agentDir, "settings.json"), "utf-8"));
  }

  test("saveDefaultModel writes defaultProvider and defaultModel", async () => {
    const agentDir = makeAgentDir();
    try {
      const outcome = await saveDefaultModel(tmpdir(), "anthropic", "claude-sonnet-4-5", agentDir);
      expect(outcome.ok).toBe(true);
      const settings = readGlobalSettings(agentDir);
      expect(settings.defaultProvider).toBe("anthropic");
      expect(settings.defaultModel).toBe("claude-sonnet-4-5");
    } finally {
      rmSync(agentDir, { recursive: true, force: true });
    }
  });

  test("saves preserve unknown keys already in the settings file", async () => {
    const agentDir = makeAgentDir();
    try {
      mkdirSync(agentDir, { recursive: true });
      writeFileSync(
        join(agentDir, "settings.json"),
        JSON.stringify({
          defaultModel: "old/model",
          someFutureKey: { nested: true },
          enabledModels: ["openai/*"],
        }),
      );
      const outcome = await saveDefaultModel(tmpdir(), "anthropic", "claude-sonnet-4-5", agentDir);
      expect(outcome.ok).toBe(true);
      const settings = readGlobalSettings(agentDir);
      expect(settings.someFutureKey).toEqual({ nested: true });
      expect(settings.enabledModels).toEqual(["openai/*"]);
    } finally {
      rmSync(agentDir, { recursive: true, force: true });
    }
  });

  test("a corrupt settings file aborts the write instead of clobbering it", async () => {
    const agentDir = makeAgentDir();
    try {
      const corrupt = "{ this is not json";
      writeFileSync(join(agentDir, "settings.json"), corrupt);
      const outcome = await saveDefaultModel(tmpdir(), "anthropic", "claude-sonnet-4-5", agentDir);
      expect(outcome.ok).toBe(false);
      expect(outcome.error).toBeTruthy();
      expect(readFileSync(join(agentDir, "settings.json"), "utf-8")).toBe(corrupt);
    } finally {
      rmSync(agentDir, { recursive: true, force: true });
    }
  });

  test("saveEnabledModels writes the pattern list; null clears it", async () => {
    const agentDir = makeAgentDir();
    try {
      const saved = await saveEnabledModels(tmpdir(), ["anthropic/*", "openai/gpt-5"], agentDir);
      expect(saved.ok).toBe(true);
      expect(readGlobalSettings(agentDir).enabledModels).toEqual(["anthropic/*", "openai/gpt-5"]);

      const cleared = await saveEnabledModels(tmpdir(), null, agentDir);
      expect(cleared.ok).toBe(true);
      expect(readGlobalSettings(agentDir).enabledModels).toBeUndefined();
    } finally {
      rmSync(agentDir, { recursive: true, force: true });
    }
  });
});

// ---------------------------------------------------------------------------
// Command wiring.
//
// ctx.ui.custom is mocked out: the tests below capture the component factory
// and drive its handleInput directly, proving /models is registered, the
// shortcut is registered, Escape closes, and Enter applies model + effort.
// What they cannot prove is terminal delivery (Kitty-protocol ctrl+shift+m on
// a real terminal) -- that is the manual-drive step, done in a live pi.
// ---------------------------------------------------------------------------

import registerModelPicker from "../extensions/model-picker";
import type { ExtensionAPI, ExtensionContext } from "@earendil-works/pi-coding-agent";

type CommandEntry = {
  name: string;
  description: string;
  handler: (args: string, ctx: ExtensionContext) => Promise<void>;
};
type ShortcutEntry = {
  shortcut: string;
  description: string;
  handler: (ctx: ExtensionContext) => Promise<void>;
};

function makeHarness() {
  const commands: Record<string, CommandEntry> = {};
  const shortcuts: Record<string, ShortcutEntry> = {};
  const notifications: { message: string; kind: string }[] = [];

  const catalogue = [
    model({ id: "claude-opus" }),
    model({ provider: "openai", id: "gpt-5", reasoning: false }),
  ];

  const applied: { model?: Model<Api>; level?: string; setModelResult: boolean } = {
    setModelResult: true,
  };

  const pi = {
    registerCommand(name: string, opts: { description: string; handler: CommandEntry["handler"] }) {
      commands[name] = { name, ...opts };
    },
    registerShortcut(
      shortcut: string,
      opts: { description: string; handler: ShortcutEntry["handler"] },
    ) {
      shortcuts[shortcut] = { shortcut, ...opts };
    },
    getThinkingLevel: () => "off" as const,
    setModel: async (m: Model<Api>) => {
      applied.model = m;
      return applied.setModelResult;
    },
    setThinkingLevel: (level: string) => {
      applied.level = level;
    },
  } as unknown as ExtensionAPI;

  function makeCtx(mode: "tui" | "print", overrides: Partial<Record<string, unknown>> = {}) {
    let component: {
      render: (width: number) => string[];
      invalidate: () => void;
      handleInput: (data: string) => void;
    } | null = null;
    let resolveCustom: ((value: unknown) => void) | undefined;
    const customPromise = new Promise<unknown>((resolve) => {
      resolveCustom = resolve;
    });

    const ctx = {
      mode,
      cwd: tmpdir(),
      model: undefined,
      scopedModels: [],
      modelRegistry: {
        getAvailable: () => catalogue,
        getProviderAuthStatus: () => ({ configured: true }),
      },
      ui: {
        custom: async (
          factory: (
            tui: { requestRender: () => void },
            theme: {
              fg: (c: string, s: string) => string;
              bg: (c: string, s: string) => string;
              bold: (s: string) => string;
            },
            kb: unknown,
            done: (result: unknown) => void,
          ) => typeof component,
        ) => {
          component = factory(
            { requestRender: () => {} },
            {
              fg: (_c: string, s: string) => s,
              bg: (_c: string, s: string) => s,
              bold: (s: string) => s,
            },
            {},
            (result: unknown) => resolveCustom?.(result),
          );
          return customPromise;
        },
        notify: (message: string, kind: string) => {
          notifications.push({ message, kind });
        },
      },
      ...overrides,
    } as unknown as ExtensionContext;

    return {
      ctx,
      getComponent: () => component,
      resolveCustom,
      customPromise,
    };
  }

  return { commands, shortcuts, notifications, catalogue, applied, pi, makeCtx };
}

describe("command wiring", () => {
  test("/models and ctrl+shift+m are both registered", () => {
    const { commands, shortcuts, pi } = makeHarness();
    registerModelPicker(pi);
    expect(commands.models).toBeDefined();
    expect(shortcuts["ctrl+shift+m"]).toBeDefined();
  });

  test("a non-TUI session is refused with a notify, not a silent no-op", async () => {
    const { commands, notifications, pi, makeCtx } = makeHarness();
    registerModelPicker(pi);
    const { ctx } = makeCtx("print");
    await commands.models.handler("", ctx);
    expect(notifications.some((n) => n.message.includes("interactive"))).toBe(true);
  });

  test("Escape closes the picker without touching the model", async () => {
    const { commands, applied, pi, makeCtx } = makeHarness();
    registerModelPicker(pi);
    const { ctx, getComponent, customPromise } = makeCtx("tui");
    const pending = commands.models.handler("", ctx);
    await Promise.resolve();
    getComponent()!.handleInput("\x1b");
    await pending;
    expect(await customPromise).toBeNull();
    expect(applied.model).toBeUndefined();
  });

  test("Enter closes the picker, then applies model + panel effort level", async () => {
    const { commands, applied, catalogue, pi, makeCtx } = makeHarness();
    registerModelPicker(pi);
    const { ctx, getComponent, customPromise } = makeCtx("tui");
    const pending = commands.models.handler("", ctx);
    await Promise.resolve();
    getComponent()!.handleInput("\r");
    await pending;
    const appliedModel = await customPromise;
    expect(appliedModel).toEqual({ model: catalogue[0], level: "off" });
    expect(applied.model).toBe(catalogue[0]);
    // Non-active highlight pre-fills off, untouched panel applies off.
    expect(applied.level).toBe("off");
  });

  test("the active model's switch is a no-op but its effort level still applies", async () => {
    const { commands, applied, catalogue, pi, makeCtx } = makeHarness();
    registerModelPicker(pi);
    const activeModel = catalogue[0];
    const { ctx, getComponent, customPromise } = makeCtx("tui", { model: activeModel });
    const pending = commands.models.handler("", ctx);
    await Promise.resolve();
    getComponent()!.handleInput("\x1b[C"); // right: off → minimal
    getComponent()!.handleInput("\r");
    await pending;
    await customPromise;
    // setModel skipped for the active model, level still applied.
    expect(applied.model).toBeUndefined();
    expect(applied.level).toBe("minimal");
  });

  test("a failed model switch closes the picker and warns", async () => {
    const { commands, notifications, applied, pi, makeCtx } = makeHarness();
    applied.setModelResult = false;
    registerModelPicker(pi);
    const { ctx, getComponent, customPromise } = makeCtx("tui");
    const pending = commands.models.handler("", ctx);
    await Promise.resolve();
    getComponent()!.handleInput("\r");
    await pending;
    await customPromise;
    expect(notifications.some((n) => n.message.includes("No API key"))).toBe(true);
  });

  test("typing builds a filter shown in the overlay; arrow keys move effort", async () => {
    const { commands, pi, makeCtx } = makeHarness();
    registerModelPicker(pi);
    const { ctx, getComponent } = makeCtx("tui");
    const pending = commands.models.handler("", ctx);
    await Promise.resolve();
    const component = getComponent()!;

    // Reasoning model highlighted by default: effort panel is live, bar empty (off).
    const before = component.render(120).join("\n");
    expect(before).toContain("effort:");
    expect(before).toContain("off");

    // Down to the non-reasoning model: panel reads unavailable.
    component.handleInput("\x1b[B");
    expect(component.render(120).join("\n")).toContain("not available for this model");

    // Back up, then filter: fuzzy over provider/id keeps only the gpt row.
    component.handleInput("\x1b[A");
    component.handleInput("g");
    component.handleInput("p");
    component.handleInput("t");
    const filtered = component.render(120).join("\n");
    expect(filtered).toContain("filter: gpt");
    expect(filtered).not.toContain("anthropic/claude-opus");

    // Clearing the filter restores every row.
    component.handleInput("\x7f");
    component.handleInput("\x7f");
    component.handleInput("\x7f");
    expect(component.render(120).join("\n")).toContain("anthropic/claude-opus");

    // Right moves the segment off → minimal: one bar cell fills.
    component.handleInput("\x1b[C");
    const after = component.render(120).join("\n");
    expect(after).toContain("minimal");
    expect(after).toContain("▮");
    void pending;
  });

  test("the overlay marks models without configured auth before Enter", async () => {
    const { commands, pi, makeCtx } = makeHarness();
    registerModelPicker(pi);
    const { ctx, getComponent } = makeCtx("tui", {
      modelRegistry: {
        getAvailable: () => [model({ provider: "unkeyed", id: "m1" }), model()],
        getProviderAuthStatus: (provider: string) => ({ configured: provider !== "unkeyed" }),
      },
    });
    const pending = commands.models.handler("", ctx);
    await Promise.resolve();
    const rendered = getComponent()!.render(160).join("\n");
    expect(rendered).toContain("no key");
    expect(rendered).toContain("unkeyed/m1");

    // Regression: the ref column stays padded in the composed overlay rows,
    // so the context column starts at the same offset on every row.
    const lines = rendered.split("\n");
    const rowLines = lines.filter((l) => l.includes(" ctx ") && !l.includes("cost $in"));
    expect(rowLines.length).toBeGreaterThan(1);
    const ctxCols = rowLines.map((l) => l.indexOf(" ctx "));
    expect(new Set(ctxCols).size).toBe(1);

    // Header labels sit over the same columns: "ctx" over the ctx literal
    // (one char after the space preceding it), "cost …" six chars later.
    const headerLine = lines.find((l) => l.includes("cost $in / $out"));
    expect(headerLine).toBeDefined();
    expect(headerLine!.indexOf("ctx")).toBe(ctxCols[0] + 1);
    expect(headerLine!.indexOf("cost")).toBe(ctxCols[0] + 6);
    void pending;
  });

  test("pressing Tab cycles sort modes and updates the footer label", async () => {
    const { commands, pi, makeCtx } = makeHarness();
    registerModelPicker(pi);
    const { ctx, getComponent } = makeCtx("tui", {
      modelRegistry: {
        getAvailable: () => [
          model({
            provider: "anthropic",
            id: "expensive",
            cost: { input: 10, output: 50, cacheRead: 0, cacheWrite: 0 },
          }),
          model({
            provider: "ollama",
            id: "free",
            cost: { input: 0, output: 0, cacheRead: 0, cacheWrite: 0 },
          }),
        ],
        getProviderAuthStatus: () => ({ configured: true }),
      },
    });
    const pending = commands.models.handler("", ctx);
    await Promise.resolve();
    const component = getComponent()!;

    // Initial: sort: name
    expect(component.render(120).join("\n")).toContain("sort: name");

    // First Tab: sort: price ↑ (free model on top)
    component.handleInput("\t");
    const asc = component.render(120).join("\n");
    expect(asc).toContain("sort: price ↑");
    const ascLines = asc.split("\n");
    const freeIdx = ascLines.findIndex((l) => l.includes("ollama/free"));
    const expIdx = ascLines.findIndex((l) => l.includes("anthropic/expensive"));
    expect(freeIdx).toBeLessThan(expIdx);

    // Second Tab: sort: price ↓ (expensive model on top)
    component.handleInput("\t");
    const desc = component.render(120).join("\n");
    expect(desc).toContain("sort: price ↓");
    const descLines = desc.split("\n");
    const freeIdx2 = descLines.findIndex((l) => l.includes("ollama/free"));
    const expIdx2 = descLines.findIndex((l) => l.includes("anthropic/expensive"));
    expect(expIdx2).toBeLessThan(freeIdx2);

    // Third Tab: wraps back to sort: name
    component.handleInput("\t");
    expect(component.render(120).join("\n")).toContain("sort: name");
    void pending;
  });
});

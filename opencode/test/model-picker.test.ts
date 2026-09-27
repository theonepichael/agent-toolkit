import assert from "node:assert/strict"
import { describe, test } from "node:test"
import type { Model, Provider } from "@opencode-ai/sdk/v2"
import {
  badgesFor,
  columnWidths,
  compareModels,
  computeRowFields,
  formatContext,
  formatCost,
  formatVariantInfo,
  groupAndSortModels,
  handleModelSelect,
  matchesFilter,
  modelCostScore,
  modelRef,
  providerHasKey,
  rowLabel,
  saveDefaultModel,
} from "../tui/model-picker"

function makeModel(overrides: Partial<Model> = {}): Model {
  return {
    id: "claude-sonnet-4-5",
    providerID: "anthropic",
    name: "Claude Sonnet 4.5",
    api: {
      id: "anthropic",
      url: "https://api.anthropic.com",
      npm: "@anthropic-ai/sdk",
    },
    capabilities: {
      temperature: true,
      reasoning: true,
      attachment: true,
      toolcall: true,
      input: {
        text: true,
        audio: false,
        image: true,
        video: false,
        pdf: true,
      },
      output: {
        text: true,
        audio: false,
        image: false,
        video: false,
        pdf: false,
      },
      interleaved: false,
    },
    cost: {
      input: 3,
      output: 15,
      cache: {
        read: 0.3,
        write: 3.75,
      },
    },
    limit: {
      context: 200_000,
      output: 8192,
    },
    status: "active",
    options: {},
    headers: {},
    release_date: "2026-01-01",
    ...overrides,
  }
}

function makeProvider(overrides: Partial<Provider> = {}): Provider {
  return {
    id: "anthropic",
    name: "Anthropic",
    source: "config",
    env: ["ANTHROPIC_API_KEY"],
    options: {},
    models: {
      "claude-sonnet-4-5": makeModel(),
    },
    ...overrides,
  }
}

describe("formatContext", () => {
  test("renders token counts as K and M", () => {
    assert.equal(formatContext(200_000), "200K")
    assert.equal(formatContext(1_000_000), "1.0M")
    assert.equal(formatContext(1_048_576), "1.0M")
    assert.equal(formatContext(128_000), "128K")
    assert.equal(formatContext(2_000_000), "2.0M")
    assert.equal(formatContext(999), "999")
  })

  test("missing or non-positive context limits degrade to a dash", () => {
    assert.equal(formatContext(undefined), "—")
    assert.equal(formatContext(0), "—")
    assert.equal(formatContext(-10), "—")
  })
})

describe("formatCost", () => {
  test("renders per-1M input / output rates", () => {
    assert.equal(formatCost({ input: 3, output: 15 }), "$3.00 / $15.00")
  })

  test("zero on both sides reads as free", () => {
    assert.equal(formatCost({ input: 0, output: 0 }), "free")
  })

  test("missing rates degrade to a dash, never NaN", () => {
    assert.equal(formatCost(undefined), "—")
    assert.equal(formatCost({ input: Number.NaN, output: 10 }), "—")
    assert.equal(formatCost({} as { input: number; output: number }), "—")
  })
})

describe("modelCostScore", () => {
  test("computes sum of input and output", () => {
    assert.equal(modelCostScore({ input: 3, output: 15 }), 18)
    assert.equal(modelCostScore({ input: 0, output: 0 }), 0)
    assert.equal(modelCostScore(undefined), Number.POSITIVE_INFINITY)
  })
})

describe("compareModels and sorting", () => {
  test("sorts by name (provider then model ID)", () => {
    const a = makeModel({ providerID: "openai", id: "gpt-4" })
    const b = makeModel({ providerID: "anthropic", id: "claude-sonnet" })
    const c = makeModel({ providerID: "anthropic", id: "claude-opus" })

    const list = [a, b, c]
    list.sort((x, y) => compareModels(x, y, "name"))
    assert.deepEqual(
      list.map((m) => modelRef(m.providerID, m.id)),
      ["anthropic/claude-opus", "anthropic/claude-sonnet", "openai/gpt-4"],
    )
  })

  test("sorts price-asc: cheapest first, missing prices last, stable ties", () => {
    const sonnet = makeModel({
      providerID: "anthropic",
      id: "sonnet",
      cost: { input: 3, output: 15, cache: { read: 0, write: 0 } },
    })
    const free = makeModel({
      providerID: "ollama",
      id: "llama",
      cost: { input: 0, output: 0, cache: { read: 0, write: 0 } },
    })
    const mini = makeModel({
      providerID: "openai",
      id: "mini",
      cost: { input: 0.15, output: 0.6, cache: { read: 0, write: 0 } },
    })
    const unknown = makeModel({
      providerID: "custom",
      id: "x",
      cost: undefined as unknown as Model["cost"],
    })

    const list = [sonnet, free, mini, unknown]
    list.sort((x, y) => compareModels(x, y, "price-asc"))
    assert.deepEqual(
      list.map((m) => modelRef(m.providerID, m.id)),
      ["ollama/llama", "openai/mini", "anthropic/sonnet", "custom/x"],
    )
  })

  test("sorts price-desc: most expensive first, free after expensive, missing prices last", () => {
    const sonnet = makeModel({
      providerID: "anthropic",
      id: "sonnet",
      cost: { input: 3, output: 15, cache: { read: 0, write: 0 } },
    })
    const free = makeModel({
      providerID: "ollama",
      id: "llama",
      cost: { input: 0, output: 0, cache: { read: 0, write: 0 } },
    })
    const mini = makeModel({
      providerID: "openai",
      id: "mini",
      cost: { input: 0.15, output: 0.6, cache: { read: 0, write: 0 } },
    })
    const unknown = makeModel({
      providerID: "custom",
      id: "x",
      cost: undefined as unknown as Model["cost"],
    })

    const list = [sonnet, free, mini, unknown]
    list.sort((x, y) => compareModels(x, y, "price-desc"))
    assert.deepEqual(
      list.map((m) => modelRef(m.providerID, m.id)),
      ["anthropic/sonnet", "openai/mini", "ollama/llama", "custom/x"],
    )
  })
})

describe("matchesFilter", () => {
  test("matches subsequence case-insensitively against provider/id", () => {
    assert.equal(matchesFilter("anthropic/claude-sonnet-4-5", "sonnet"), true)
    assert.equal(matchesFilter("anthropic/claude-sonnet-4-5", "CLAUDE"), true)
    assert.equal(matchesFilter("anthropic/claude-sonnet-4-5", "ant-son"), true)
    assert.equal(matchesFilter("anthropic/claude-sonnet-4-5", "xyz"), false)
    assert.equal(matchesFilter("anthropic/claude-sonnet-4-5", ""), true)
  })
})

describe("providerHasKey and badgesFor", () => {
  test("detects configured provider keys without exposing them", () => {
    const pWithKey = makeProvider({ key: "secret-key-123" })
    assert.equal(providerHasKey(pWithKey), true)

    const pWithApi = makeProvider({ source: "api", key: undefined })
    assert.equal(providerHasKey(pWithApi), true)

    const pWithEnv = makeProvider({
      key: undefined,
      env: ["TEST_EXISTING_ENV_VAR_FOR_MODEL_PICKER"],
    })
    process.env.TEST_EXISTING_ENV_VAR_FOR_MODEL_PICKER = "configured"
    try {
      assert.equal(providerHasKey(pWithEnv), true)
    } finally {
      delete process.env.TEST_EXISTING_ENV_VAR_FOR_MODEL_PICKER
    }

    const pWithoutKey = makeProvider({ key: undefined, env: [] })
    assert.equal(providerHasKey(pWithoutKey), false)
  })

  test("renders badges for reasoning, vision, and missing key", () => {
    const m = makeModel({
      capabilities: {
        ...makeModel().capabilities,
        reasoning: true,
        input: { ...makeModel().capabilities.input, image: true },
      },
    })
    const badges = badgesFor(m, { hasAuth: false })
    assert.ok(badges.includes("[reasoning]"))
    assert.ok(badges.includes("[vision]"))
    assert.ok(badges.includes("[no key]"))
    assert.ok(!badges.includes("secret"))
  })
})

describe("formatVariantInfo", () => {
  test("labels variants as explicitly untested and unapplied", () => {
    const m = makeModel({
      variants: {
        thinking: { effort: "high" },
        fast: {},
      },
    })
    const info = formatVariantInfo(m)
    assert.ok(info.includes("thinking"))
    assert.ok(info.includes("fast"))
    assert.ok(info.includes("untested / unapplied by this overlay"))
  })

  test("returns empty string when model has no variants", () => {
    const m = makeModel({ variants: undefined })
    assert.equal(formatVariantInfo(m), "")
  })
})

describe("groupAndSortModels", () => {
  test("handles empty providers gracefully", () => {
    const items = groupAndSortModels([], "name")
    assert.deepEqual(items, [])
  })

  test("filters models without filtering out provider groupings", () => {
    const p1 = makeProvider({
      id: "anthropic",
      models: {
        sonnet: makeModel({ id: "sonnet", providerID: "anthropic" }),
        opus: makeModel({ id: "opus", providerID: "anthropic" }),
      },
    })
    const p2 = makeProvider({
      id: "openai",
      models: {
        gpt4: makeModel({ id: "gpt4", providerID: "openai" }),
      },
    })

    const filtered = groupAndSortModels([p1, p2], "name", "sonnet")
    assert.equal(filtered.length, 1)
    assert.equal(filtered[0]?.model.id, "sonnet")
  })
})

describe("handleModelSelect", () => {
  test("hands off to client.tui.openModels without claiming model switch", async () => {
    let cleared = false
    let opened = false

    const dialog = {
      clear: () => {
        cleared = true
      },
    }
    const client = {
      tui: {
        openModels: async () => {
          opened = true
          return { data: true }
        },
      },
      v2: {
        session: {
          switchModel: () => {
            throw new Error("Must not call switchModel")
          },
        },
      },
    }

    await handleModelSelect(dialog as any, client as any)
    assert.equal(cleared, true)
    assert.equal(opened, true)
  })
})

describe("saveDefaultModel", () => {
  test("calls global.config.update with model reference", async () => {
    let updatedConfig: any = null
    const client = {
      global: {
        config: {
          update: async (params: any) => {
            updatedConfig = params.config
            return { data: { ok: true } }
          },
        },
      },
    }

    const res = await saveDefaultModel(client as any, "anthropic/claude-sonnet-4-5")
    assert.equal(res.ok, true)
    assert.deepEqual(updatedConfig, { model: "anthropic/claude-sonnet-4-5" })
  })

  test("reports error when global.config.update fails", async () => {
    const client = {
      global: {
        config: {
          update: async () => {
            throw new Error("network error")
          },
        },
      },
    }

    const res = await saveDefaultModel(client as any, "anthropic/claude-sonnet-4-5")
    assert.equal(res.ok, false)
    assert.ok(res.error?.includes("network error"))
  })
})

describe("columnWidths", () => {
  test("computes max ctxWidth and costWidth over models", () => {
    const m1 = makeModel({
      limit: { context: 200_000, output: 8192 },
      cost: { input: 3, output: 15, cache: { read: 0, write: 0 } },
    })
    const m2 = makeModel({
      limit: { context: 1_000_000, output: 8192 },
      cost: { input: 0, output: 0, cache: { read: 0, write: 0 } },
    })
    const widths = columnWidths([m1, m2])
    assert.equal(widths.ctxWidth, 4) // "1.0M".length or "200K".length = 4
    assert.equal(widths.costWidth, 14) // "$3.00 / $15.00".length = 14
  })

  test("defaults to 1 for empty list", () => {
    const widths = columnWidths([])
    assert.equal(widths.ctxWidth, 1)
    assert.equal(widths.costWidth, 1)
  })
})

describe("computeRowFields and rowLabel", () => {
  test("computeRowFields decomposes row parts and computes correct padding", () => {
    const m = makeModel({
      providerID: "anthropic",
      id: "claude-sonnet-4-5",
      capabilities: {
        ...makeModel().capabilities,
        reasoning: false,
        input: { ...makeModel().capabilities.input, image: false },
      },
    })
    const fields = computeRowFields(m, { ctxWidth: 6, costWidth: 15, rowWidth: 80 })
    assert.equal(fields.ref, "anthropic/claude-sonnet-4-5")
    assert.equal(fields.badgeText, "")
    assert.equal(fields.dot, "")
    assert.ok(fields.pad > 0)
  })

  test("shows provider/id, context, and per-1M cost", () => {
    const m = makeModel({
      providerID: "anthropic",
      id: "claude-sonnet-4-5",
      capabilities: {
        ...makeModel().capabilities,
        reasoning: false,
        input: { ...makeModel().capabilities.input, image: false },
      },
    })
    assert.equal(rowLabel(m), "anthropic/claude-sonnet-4-5  200K context  $3.00 / $15.00")
  })

  test("badges mark reasoning, vision, and missing auth; the active model gets a dot", () => {
    const m = makeModel({
      capabilities: {
        ...makeModel().capabilities,
        reasoning: false,
        input: { ...makeModel().capabilities.input, image: true },
      },
    })
    assert.ok(rowLabel(m).includes("[vision]"))
    assert.ok(rowLabel(m, { hasAuth: false }).includes("[no key]"))
    assert.ok(rowLabel(m, { isActive: true }).includes("●"))
  })

  test("columnWidths + rowWidth right-align context and cost regardless of name length", () => {
    const models = [
      makeModel({ providerID: "a", id: "short" }),
      makeModel({
        providerID: "opencode-go",
        id: "a-very-long-model-id-for-testing",
        limit: { context: 1_048_576, output: 8192 },
      }),
      makeModel({
        providerID: "x",
        id: "b",
        cost: { input: 0, output: 0, cache: { read: 0, write: 0 } },
      }),
    ]
    const widths = columnWidths(models)
    const rowWidth = 80
    const rows = models.map((m) => rowLabel(m, { ...widths, rowWidth }))
    const ctxStart = rows.map((r) => r.indexOf(" context"))
    assert.ok(ctxStart.every((c) => c === ctxStart[0]))
    assert.ok(rows.every((r) => r.length === rowWidth))
  })

  test("missing metadata degrades inside the row instead of NaN", () => {
    const m = makeModel({
      capabilities: {
        ...makeModel().capabilities,
        reasoning: false,
        input: { ...makeModel().capabilities.input, image: false },
      },
      limit: { context: 0, output: 0 },
      cost: { input: 0, output: 0, cache: { read: 0, write: 0 } },
    })
    const row = rowLabel(m)
    assert.ok(!row.includes("NaN"))
    assert.ok(row.includes("— context"))
    assert.ok(row.includes("free"))
  })
})

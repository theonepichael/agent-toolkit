import type { TuiDialogSelectOption, TuiPlugin, TuiPluginApi } from "@opencode-ai/plugin/tui"
import type { Model, OpencodeClient, Provider } from "@opencode-ai/sdk/v2"

export type SortMode = "name" | "price-asc" | "price-desc"

export interface ModelItem {
  provider: Provider
  model: Model
  hasAuth: boolean
  ref: string
}

/** Canonical "provider/id" reference for a model. */
export function modelRef(
  providerOrModel: string | { providerID?: string; provider?: string; id?: string },
  modelID?: string,
): string {
  if (typeof providerOrModel === "object" && providerOrModel !== null) {
    const p = providerOrModel.providerID ?? providerOrModel.provider ?? ""
    const id = providerOrModel.id ?? ""
    return p && id ? `${p}/${id}` : id || p
  }
  return `${providerOrModel}/${modelID}`
}

function trimNum(n: number): string {
  const rounded = Math.round(n * 10) / 10
  return Number.isInteger(rounded) ? String(rounded) : rounded.toFixed(1)
}

/** Format context limit as e.g. "200K" or "1.0M", or "—" if missing/non-positive. */
export function formatContext(tokens: number | undefined): string {
  if (tokens === undefined || tokens <= 0 || Number.isNaN(tokens)) return "—"
  if (tokens >= 1_000_000) return `${(tokens / 1_000_000).toFixed(1)}M`
  if (tokens >= 1_000) return `${trimNum(tokens / 1_000)}K`
  return String(tokens)
}

/** Format input/output cost per 1M tokens, "free" when both are zero, or "—" when missing. */
export function formatCost(cost: { input?: number; output?: number } | undefined): string {
  if (!cost || typeof cost.input !== "number" || typeof cost.output !== "number") return "—"
  if (Number.isNaN(cost.input) || Number.isNaN(cost.output)) return "—"
  if (cost.input === 0 && cost.output === 0) return "free"
  return `$${cost.input.toFixed(2)} / $${cost.output.toFixed(2)}`
}

/** Numerical score for sorting models by price ($in + $out). Missing prices return infinity. */
export function modelCostScore(cost: { input?: number; output?: number } | undefined): number {
  if (
    !cost ||
    typeof cost.input !== "number" ||
    typeof cost.output !== "number" ||
    Number.isNaN(cost.input) ||
    Number.isNaN(cost.output)
  ) {
    return Number.POSITIVE_INFINITY
  }
  return cost.input + cost.output
}

/** Comparator for models supporting name, price-asc, and price-desc. */
export function compareModels(
  a: Pick<Model, "id" | "providerID" | "cost">,
  b: Pick<Model, "id" | "providerID" | "cost">,
  mode: SortMode = "name",
): number {
  if (mode === "price-asc") {
    const scoreA = modelCostScore(a.cost)
    const scoreB = modelCostScore(b.cost)
    if (scoreA !== scoreB) return scoreA - scoreB
  } else if (mode === "price-desc") {
    const scoreA = modelCostScore(a.cost)
    const scoreB = modelCostScore(b.cost)
    if (Number.isFinite(scoreA) && Number.isFinite(scoreB)) {
      if (scoreA !== scoreB) return scoreB - scoreA
    } else if (Number.isFinite(scoreA)) {
      return -1
    } else if (Number.isFinite(scoreB)) {
      return 1
    }
  }
  return a.providerID.localeCompare(b.providerID) || a.id.localeCompare(b.id)
}

/** Subsequence filter matching case-insensitively. */
export function matchesFilter(target: string, query: string): boolean {
  if (!query) return true
  const lower = target.toLowerCase()
  const q = query.toLowerCase()
  let start = 0
  for (const ch of q) {
    const at = lower.indexOf(ch, start)
    if (at === -1) return false
    start = at + 1
  }
  return true
}

/** Check whether a provider has configured authentication without leaking credential contents. */
export function providerHasKey(provider: Provider): boolean {
  if (provider.key && provider.key.trim().length > 0) return true
  if (provider.source === "api") return true
  if (Array.isArray(provider.env) && provider.env.length > 0) {
    return provider.env.some((varName) => Boolean(process.env[varName]?.trim()))
  }
  return false
}

/** Bracketed badge chips for a row ("  [reasoning] [vision] [no key]"), or "" when none apply. */
export function badgesFor(
  model: Model | { capabilities?: any; reasoning?: boolean; input?: any },
  opts: { hasAuth?: boolean } = {},
): string {
  const badges: string[] = []
  const hasReasoning = Boolean((model as any).capabilities?.reasoning ?? (model as any).reasoning)
  const hasVision = Boolean(
    (model as any).capabilities?.input?.image ?? (model as any).input?.includes?.("image"),
  )
  if (hasReasoning) badges.push("[reasoning]")
  if (hasVision) badges.push("[vision]")
  if (opts.hasAuth === false) badges.push("[no key]")
  return badges.length > 0 ? `  ${badges.join(" ")}` : ""
}

/** Column widths (over the current set) keeping context and cost values aligned across rows. */
export function columnWidths(models: Model[]): { ctxWidth: number; costWidth: number } {
  return {
    ctxWidth:
      models.length > 0
        ? Math.max(
            ...models.map(
              (m) => formatContext(m.limit?.context ?? (m as any).contextWindow).length,
            ),
          )
        : 1,
    costWidth: models.length > 0 ? Math.max(...models.map((m) => formatCost(m.cost).length)) : 1,
  }
}

/** Plain fields shared across row formatters. */
export interface RowFields {
  dot: string
  ref: string
  badgeText: string
  ctxText: string
  costText: string
  pad: number
}

/** Compute the plain-text pieces of a row matching Pi's model-picker layout. */
export function computeRowFields(
  model: Model,
  opts: {
    isActive?: boolean
    hasAuth?: boolean
    ctxWidth?: number
    costWidth?: number
    rowWidth?: number
  } = {},
): RowFields {
  const badgeText = badgesFor(model, opts)
  const dot = opts.isActive ? "● " : ""
  let ref = modelRef(model)
  const tokens = model.limit?.context ?? (model as any).contextWindow
  const ctx = formatContext(tokens)
  const ctxText = opts.ctxWidth ? ctx.padStart(opts.ctxWidth) : ctx
  const cost = formatCost(model.cost)
  const costText = opts.costWidth ? cost.padStart(opts.costWidth) : cost
  const rightLen = ctxText.length + " context".length + 2 + costText.length

  let pad = 2
  if (opts.rowWidth !== undefined) {
    const availableForLeft = Math.max(1, opts.rowWidth - rightLen - 2)
    const fixedLen = dot.length + badgeText.length
    if (fixedLen + ref.length > availableForLeft) {
      const maxRefLen = Math.max(1, availableForLeft - fixedLen)
      ref = ref.length > maxRefLen ? `${ref.slice(0, Math.max(1, maxRefLen - 1))}…` : ref
    }
    const leftLen = dot.length + ref.length + badgeText.length
    pad = Math.max(2, opts.rowWidth - leftLen - rightLen)
  }

  return { dot, ref, badgeText, ctxText, costText, pad }
}

/**
 * Plain-text row: "● provider/id  [badges]   200K context  $3.00 / $15.00".
 * Context and cost right-align within rowWidth when given.
 */
export function rowLabel(
  model: Model,
  opts: {
    isActive?: boolean
    hasAuth?: boolean
    ctxWidth?: number
    costWidth?: number
    rowWidth?: number
  } = {},
): string {
  const f = computeRowFields(model, opts)
  return `${f.dot}${f.ref}${f.badgeText}${" ".repeat(f.pad)}${f.ctxText} context  ${f.costText}`
}

/** Format variant metadata with explicit untested / unapplied notice. */
export function formatVariantInfo(model: Model): string {
  if (!model.variants || Object.keys(model.variants).length === 0) return ""
  const names = Object.keys(model.variants).join(", ")
  return `variants: ${names} (untested / unapplied by this overlay)`
}

/** Collect, filter, and sort models across providers. */
export function groupAndSortModels(
  providers: ReadonlyArray<Provider>,
  mode: SortMode,
  query = "",
): ModelItem[] {
  const items: ModelItem[] = []
  for (const provider of providers) {
    const hasAuth = providerHasKey(provider)
    if (!provider.models) continue
    for (const [modelID, model] of Object.entries(provider.models)) {
      const ref = modelRef(provider.id, modelID)
      if (matchesFilter(ref, query)) {
        items.push({ provider, model, hasAuth, ref })
      }
    }
  }

  items.sort((a, b) => compareModels(a.model, b.model, mode))
  return items
}

/** Persist highlighted model as global default using the global config API. */
export async function saveDefaultModel(
  client: OpencodeClient,
  ref: string,
): Promise<{ ok: boolean; error?: string }> {
  try {
    const res = await client.global.config.update({ config: { model: ref } })
    if (res && "error" in res && res.error) {
      return { ok: false, error: String(res.error) }
    }
    return { ok: true }
  } catch (err) {
    return { ok: false, error: err instanceof Error ? err.message : String(err) }
  }
}

/** Hand off model selection to OpenCode's built-in dialog. */
export async function handleModelSelect(
  dialog: { clear: () => void },
  client: OpencodeClient,
): Promise<void> {
  dialog.clear()
  try {
    await client.tui.openModels()
  } catch {
    // OpenModels handoff failure handled cleanly
  }
}

export const ModelPicker: TuiPlugin = async (api) => {
  api.keymap.registerLayer({
    commands: [
      {
        name: "model-picker.open",
        title: "Model info: compare models",
        namespace: "palette",
        slashName: "model-info",
        run: () => openPicker(api),
      },
    ],
    bindings: [{ key: "<leader>i", cmd: "model-picker.open" }],
  })
}

export default { id: "model-picker", tui: ModelPicker }

function openPicker(api: TuiPluginApi): void {
  let sortMode: SortMode = "name"
  let query = ""
  let currentHighlighted: string | undefined

  let unregisterDialogLayer: (() => void) | undefined

  const closeDialog = () => {
    unregisterDialogLayer?.()
    unregisterDialogLayer = undefined
    api.ui.dialog.clear()
  }

  const renderDialog = () => {
    const items = groupAndSortModels(api.state.provider, sortMode, query)

    if (items.length > 0 && !currentHighlighted) {
      currentHighlighted = items[0]?.ref
    }

    const widths = columnWidths(items.map((i) => i.model))
    const termWidth =
      process.stdout?.columns && process.stdout.columns > 0 ? process.stdout.columns : 116
    const dialogWidth = Math.min(116, Math.max(60, termWidth - 2))
    const rowWidth = Math.max(40, dialogWidth - 16)

    const activeModelRef = api.state.config?.model

    const options: TuiDialogSelectOption<string>[] = items.map((item) => {
      const label = rowLabel(item.model, {
        ...widths,
        rowWidth,
        hasAuth: item.hasAuth,
      })

      return {
        title: label,
        value: item.ref,
        category: item.provider.name
          ? item.provider.name.toUpperCase()
          : item.provider.id.toUpperCase(),
        truncateTitle: false,
        onSelect: async () => {
          unregisterDialogLayer?.()
          unregisterDialogLayer = undefined
          await handleModelSelect(api.ui.dialog, api.client)
        },
      }
    })

    if (options.length === 0) {
      options.push({
        title: query ? "No matching models found" : "No models available",
        value: "",
        disabled: true,
        footer: "Esc: close",
        onSelect: closeDialog,
      })
    }

    api.ui.dialog.replace(() =>
      api.ui.DialogSelect({
        title: `Model Comparison (${sortMode}) — Tab: sort | Ctrl+S: default | Enter: switch`,
        placeholder: "Filter models by provider/id...",
        options,
        current: activeModelRef,
        onFilter: (newQuery: string) => {
          query = newQuery
          renderDialog()
        },
        onMove: (option) => {
          if (typeof option.value === "string" && option.value) {
            currentHighlighted = option.value
          }
        },
      }),
    )
    api.ui.dialog.setSize("xlarge")
  }

  unregisterDialogLayer = api.keymap.registerLayer({
    commands: [
      {
        name: "model-picker.save-default",
        title: "Model picker: save default model",
        run: async () => {
          if (!currentHighlighted) return
          const res = await saveDefaultModel(api.client, currentHighlighted)
          if (res.ok) {
            api.ui.toast({
              title: "Model Picker",
              message: `Saved ${currentHighlighted} as global default model`,
              variant: "success",
            })
          } else {
            api.ui.toast({
              title: "Model Picker",
              message: `Failed to save default model: ${res.error}`,
              variant: "error",
            })
          }
        },
      },
      {
        name: "model-picker.sort-cycle",
        title: "Model picker: cycle sort mode",
        run: () => {
          sortMode =
            sortMode === "name" ? "price-asc" : sortMode === "price-asc" ? "price-desc" : "name"
          renderDialog()
        },
      },
      {
        name: "model-picker.close",
        title: "Model picker: close dialog",
        run: closeDialog,
      },
    ],
    bindings: [
      { key: "ctrl+s", cmd: "model-picker.save-default" },
      { key: "tab", cmd: "model-picker.sort-cycle" },
      { key: "escape", cmd: "model-picker.close" },
    ],
  })

  renderDialog()
}

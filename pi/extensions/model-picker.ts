/**
 * Model picker — /models overlay showing context window, cost, and reasoning
 * effort per model.
 *
 * Supplements, not replaces, the built-in /model picker (built-in interactive
 * commands cannot be overridden by extensions, and /model / Ctrl+L stay
 * untouched). Per model, the rows show what the built-in picker doesn't:
 * context window size, per-1M-token input/output cost, reasoning/vision
 * badges, and a `no key` tag when the provider has no configured auth — so
 * unconfigured catalogue entries are visible before Enter rather than
 * discovered by failure.
 *
 * Keys:
 *   ↑/↓        move highlight (wraps)
 *   type/bksp  filter rows (fuzzy match on provider/id)
 *   ←/→        move the reasoning-effort segment for the highlighted model
 *   Tab        cycle sort (name → price: low→high → price: high→low)
 *   Enter      apply panel effort + switch to the highlighted model
 *   ctrl+s     persist highlighted model as the default model
 *   ctrl+a     add all shown models to enabledModels (additive union)
 *   Esc        close
 *
 * The dialog renders as a true floating overlay ({ overlay: true }) drawn as
 * a solid panel — bg-filled rows and side borders, matching the modal mockup.
 */

import type { ExtensionAPI, ExtensionContext } from "@earendil-works/pi-coding-agent";
import { SettingsManager, type Theme } from "@earendil-works/pi-coding-agent";
import type { Model, ModelCost, ModelThinkingLevel } from "@earendil-works/pi-ai";
import type { Api } from "@earendil-works/pi-ai";
import {
  Container,
  fuzzyFilter,
  Key,
  matchesKey,
  type SelectItem,
  SelectList,
  truncateToWidth,
  visibleWidth,
  type TUI,
} from "@earendil-works/pi-tui";

/** Ordered non-"off" thinking levels, matching pi-ai's ThinkingLevel. */
const EFFORT_LEVELS = ["minimal", "low", "medium", "high", "xhigh", "max"] as const;

/** Display labels for segmented effort control matching the HTML mockup. */
export const EFFORT_DISPLAY_NAMES: Record<ModelThinkingLevel, string> = {
  off: "off",
  minimal: "min",
  low: "low",
  medium: "medium",
  high: "high",
  xhigh: "xhigh",
  max: "max",
};

export type SortMode = "name" | "price-asc" | "price-desc";

type AnyModel = Model<Api>;

/** Canonical "provider/id" reference for a model. */
export function modelRef(model: Pick<AnyModel, "provider" | "id">): string {
  return `${model.provider}/${model.id}`;
}

/**
 * "200K" / "1,000K" / "1.0M" style context size, or "—" when the metadata is missing
 * or zero (Model.contextWindow is typed non-optional but defaults to 0).
 */
export function formatContext(tokens: number | undefined): string {
  if (tokens === undefined || tokens <= 0) return "—";
  if (tokens >= 1_000_000) return `${(tokens / 1_000_000).toFixed(1)}M`;
  if (tokens >= 1_000) return `${trimNum(tokens / 1_000)}K`;
  return String(tokens);
}

function trimNum(n: number): string {
  const rounded = Math.round(n * 10) / 10;
  return Number.isInteger(rounded) ? String(rounded) : rounded.toFixed(1);
}

/**
 * "$3.00 / $15.00" input/output per 1M tokens; "free" when both are zero;
 * "—" when the rates are missing.
 */
export function formatCost(cost: ModelCost | undefined): string {
  if (!cost || typeof cost.input !== "number" || typeof cost.output !== "number") return "—";
  if (cost.input === 0 && cost.output === 0) return "free";
  return `$${cost.input.toFixed(2)} / $${cost.output.toFixed(2)}`;
}

/** Numerical score for sorting models by price ($in + $out). Missing prices return infinity. */
export function modelCostScore(cost: ModelCost | undefined): number {
  if (!cost || typeof cost.input !== "number" || typeof cost.output !== "number") {
    return Number.POSITIVE_INFINITY;
  }
  return cost.input + cost.output;
}

/** Comparator for models supporting name, price ascending, and price descending. */
export function compareModels(a: AnyModel, b: AnyModel, mode: SortMode = "name"): number {
  if (mode === "price-asc") {
    const scoreA = modelCostScore(a.cost);
    const scoreB = modelCostScore(b.cost);
    if (scoreA !== scoreB) return scoreA - scoreB;
  } else if (mode === "price-desc") {
    const scoreA = modelCostScore(a.cost);
    const scoreB = modelCostScore(b.cost);
    if (Number.isFinite(scoreA) && Number.isFinite(scoreB)) {
      if (scoreA !== scoreB) return scoreB - scoreA;
    } else if (Number.isFinite(scoreA)) {
      return -1;
    } else if (Number.isFinite(scoreB)) {
      return 1;
    }
  }
  return a.provider.localeCompare(b.provider) || a.id.localeCompare(b.id);
}

/** Dedupe by provider/id and sort by provider, then id. */
export function dedupeAndSort(models: AnyModel[]): AnyModel[] {
  const seen = new Set<string>();
  const unique: AnyModel[] = [];
  for (const model of models) {
    const ref = modelRef(model);
    if (!seen.has(ref)) {
      seen.add(ref);
      unique.push(model);
    }
  }
  return unique.sort((a, b) => compareModels(a, b, "name"));
}

/**
 * Effort levels the picker offers for a model: always "off"; for reasoning
 * models the pi thinking levels whose thinkingLevelMap entry is not null
 * (a missing key uses the provider default, so it stays offered).
 */
export function effortLevelsFor(
  model: Pick<AnyModel, "reasoning" | "thinkingLevelMap">,
): ModelThinkingLevel[] {
  if (!model.reasoning) return ["off"];
  const levels: ModelThinkingLevel[] = ["off"];
  for (const level of EFFORT_LEVELS) {
    if (model.thinkingLevelMap?.[level] !== null) levels.push(level);
  }
  return levels;
}

/**
 * Panel pre-fill for a highlighted model: its current level when it is the
 * active model (and the level is still supported there), otherwise "off" —
 * never a guessed mid-level, so Enter on an untouched panel is a no-op.
 */
export function prefillEffort(
  model: Pick<AnyModel, "provider" | "id" | "reasoning" | "thinkingLevelMap">,
  activeRef: string | undefined,
  currentLevel: ModelThinkingLevel,
): ModelThinkingLevel {
  if (modelRef(model) !== activeRef) return "off";
  return effortLevelsFor(model).includes(currentLevel) ? currentLevel : "off";
}

/**
 * Additive union mirroring the built-in scoped-models selector's enableAll():
 * null (all-enabled) stays null; otherwise target ids are appended to the
 * existing list; full coverage of allIds collapses back to null.
 */
export function unionEnabled(
  current: string[] | null,
  targetIds: string[],
  allIds: string[],
): string[] | null {
  if (current === null) return null;
  const result = [...current];
  for (const id of targetIds) {
    if (!result.includes(id)) result.push(id);
  }
  return result.length === allIds.length && result.every((id) => allIds.includes(id))
    ? null
    : result;
}

export interface PersistOutcome {
  ok: boolean;
  error?: string;
}

/**
 * Persist the default model (provider + id, same fields the built-in
 * selector writes).
 */
export async function saveDefaultModel(
  cwd: string,
  provider: string,
  modelId: string,
  agentDir?: string,
): Promise<PersistOutcome> {
  try {
    const settings = SettingsManager.create(cwd, agentDir);
    settings.setDefaultModelAndProvider(provider, modelId);
    await settings.flush();
    const errors = settings.drainErrors();
    if (errors.length > 0) {
      return { ok: false, error: errors.map((e) => `${e.scope}: ${e.error.message}`).join("; ") };
    }
    return { ok: true };
  } catch (err) {
    return { ok: false, error: err instanceof Error ? err.message : String(err) };
  }
}

/**
 * Persist enabledModels patterns; null means unrestricted/all-enabled and
 * clears the field.
 */
export async function saveEnabledModels(
  cwd: string,
  patterns: string[] | null,
  agentDir?: string,
): Promise<PersistOutcome> {
  try {
    const settings = SettingsManager.create(cwd, agentDir);
    settings.setEnabledModels(patterns ?? undefined);
    await settings.flush();
    const errors = settings.drainErrors();
    if (errors.length > 0) {
      return { ok: false, error: errors.map((e) => `${e.scope}: ${e.error.message}`).join("; ") };
    }
    return { ok: true };
  } catch (err) {
    return { ok: false, error: err instanceof Error ? err.message : String(err) };
  }
}

/** Badge suffix for a row ("  [reasoning, vision]"), or "" when none apply. */
export function badgesFor(
  model: AnyModel,
  opts: { isActive?: boolean; hasAuth?: boolean } = {},
): string {
  const badges: string[] = [];
  if (model.reasoning) badges.push("reasoning");
  if (model.input?.includes("image")) badges.push("vision");
  if (opts.hasAuth === false) badges.push("no key");
  if (opts.isActive) badges.push("active");
  return badges.length > 0 ? `  [${badges.join(", ")}]` : "";
}

/** Plain-text row for a model: "provider/id  200K ctx  $3.00 / $15.00" + badges. */
export function rowLabel(
  model: AnyModel,
  opts: {
    isActive?: boolean;
    hasAuth?: boolean;
    refWidth?: number;
    ctxWidth?: number;
    costWidth?: number;
  } = {},
): string {
  const badgeText = badgesFor(model, opts);
  let ref = modelRef(model);
  if (opts.refWidth) {
    ref = ref.length > opts.refWidth ? `${ref.slice(0, Math.max(1, opts.refWidth - 1))}…` : ref;
    ref = ref.padEnd(opts.refWidth);
  }
  const ctx = formatContext(model.contextWindow);
  const ctxText = opts.ctxWidth ? ctx.padEnd(opts.ctxWidth) : ctx;
  const cost = formatCost(model.cost);
  const costText = opts.costWidth ? cost.padEnd(opts.costWidth) : cost;
  return `${ref}  ${ctxText} ctx  ${costText}${badgeText}`;
}

/**
 * Column widths that align the id, context, and cost columns across rows.
 */
export function columnWidths(
  models: AnyModel[],
  opts: { maxBadgeLen?: number; maxRefWidth?: number } = {},
): {
  refWidth: number;
  ctxWidth: number;
  costWidth: number;
} {
  let refWidth = models.length > 0 ? Math.max(...models.map((m) => modelRef(m).length)) : 1;
  if (opts.maxRefWidth !== undefined) refWidth = Math.min(refWidth, opts.maxRefWidth);
  return {
    refWidth: Math.max(1, refWidth),
    ctxWidth:
      models.length > 0 ? Math.max(...models.map((m) => formatContext(m.contextWindow).length)) : 1,
    costWidth: models.length > 0 ? Math.max(...models.map((m) => formatCost(m.cost).length)) : 1,
  };
}

/**
 * Character indices of query in text, in order (subsequence scan).
 */
export function matchIndices(text: string, query: string): number[] {
  const lower = text.toLowerCase();
  const q = query.toLowerCase();
  const contiguous = lower.indexOf(q);
  if (contiguous !== -1) return [...Array(q.length).keys()].map((i) => contiguous + i);
  const indices: number[] = [];
  let start = 0;
  for (const ch of q) {
    const at = lower.indexOf(ch, start);
    if (at === -1) return [];
    indices.push(at);
    start = at + 1;
  }
  return indices;
}

/**
 * Effort rendered as an intensity bar (kept for backward compatibility & testing).
 */
export function effortBar(
  levels: ModelThinkingLevel[],
  current: ModelThinkingLevel,
): {
  filled: number;
  total: number;
} {
  const total = Math.max(0, levels.length - 1);
  return { filled: Math.max(0, levels.indexOf(current)), total };
}

/** Muted "provider/" prefix + "no key" dimming + badge styling for a row. */
function styleRowLabel(
  ref: string,
  rest: string,
  theme: Theme,
  opts: { hasAuth?: boolean; query?: string; isSelected?: boolean },
): string {
  if (opts.hasAuth === false) {
    return theme.fg("dim", ref + rest);
  }

  let styledRef = "";
  if (opts.query) {
    const hits = new Set(matchIndices(ref, opts.query));
    for (let i = 0; i < ref.length; i++) {
      styledRef += hits.has(i) ? theme.fg("accent", theme.bold(ref[i])) : ref[i];
    }
  } else {
    const slash = ref.indexOf("/");
    if (slash === -1) {
      styledRef = theme.bold(ref);
    } else {
      styledRef = theme.fg("dim", ref.slice(0, slash + 1)) + theme.bold(ref.slice(slash + 1));
    }
  }

  // Parse rest to colorize context, cost, badges
  let styledRest = rest;
  const ctxIdx = rest.indexOf(" ctx  ");
  if (ctxIdx !== -1) {
    const ctxVal = rest.slice(0, ctxIdx);
    const afterCtx = rest.slice(ctxIdx + 6);
    const badgeIdx = afterCtx.indexOf("  [");
    const costVal = badgeIdx !== -1 ? afterCtx.slice(0, badgeIdx) : afterCtx;
    const badgeVal = badgeIdx !== -1 ? afterCtx.slice(badgeIdx) : "";

    const coloredCtx = theme.fg("text", ctxVal) + theme.fg("dim", " ctx  ");
    const coloredCost =
      costVal.trim() === "free"
        ? theme.fg("success", costVal)
        : costVal.trim() === "—"
          ? theme.fg("dim", costVal)
          : theme.fg("warning", costVal);

    let coloredBadges = "";
    if (badgeVal) {
      coloredBadges = badgeVal.replace(/\[(.*?)\]/, (_match, p1: string) => {
        const parts = p1.split(", ").map((badge) => {
          if (badge === "reasoning") return theme.fg("accent", "[reasoning]");
          if (badge === "vision") return theme.fg("success", "[vision]");
          if (badge === "no key") return theme.fg("error", "[no key]");
          if (badge === "active") return theme.fg("success", "● active");
          return theme.fg("dim", `[${badge}]`);
        });
        return "  " + parts.join(" ");
      });
    }

    styledRest = coloredCtx + coloredCost + coloredBadges;
  }

  return styledRef + styledRest;
}

/** The models the picker offers: scoped models when non-empty, else the full catalogue. */
function pickerModels(ctx: ExtensionContext): AnyModel[] {
  const scoped = ctx.scopedModels.map((sm) => sm.model);
  const models = scoped.length > 0 ? scoped : ctx.modelRegistry.getAvailable();
  return dedupeAndSort(models);
}

/** Solid panel background. */
const PANEL_BG = "customMessageBg" as const;

/**
 * Draw the dialog content as a solid floating panel: rounded corners,
 * solid side borders, bg-filled rows with uniform background across border cells.
 */
function renderPanel(content: string[], width: number, theme: Theme): string[] {
  const inner = Math.max(1, width - 4);
  const filled = (line: string): string =>
    theme.bg(
      PANEL_BG,
      `${theme.fg("accent", "│")} ${truncateToWidth(line, inner, "", true)} ${theme.fg("accent", "│")}`,
    );
  const edge = (l: string, r: string) =>
    theme.bg(
      PANEL_BG,
      theme.fg("accent", l) + theme.fg("dim", "─".repeat(inner + 2)) + theme.fg("accent", r),
    );

  return [edge("╭", "╮"), ...content.map(filled), edge("╰", "╯")];
}

export default function modelPicker(pi: ExtensionAPI) {
  async function openPicker(ctx: ExtensionContext): Promise<void> {
    if (ctx.mode !== "tui") {
      ctx.ui.notify("/models needs an interactive session", "warning");
      return;
    }

    const models = pickerModels(ctx);
    if (models.length === 0) {
      ctx.ui.notify("No models available", "warning");
      return;
    }

    const activeRef = ctx.model ? modelRef(ctx.model) : undefined;
    const authByProvider = new Map<string, boolean>();
    const hasAuth = (provider: string): boolean => {
      let status = authByProvider.get(provider);
      if (status === undefined) {
        status = ctx.modelRegistry.getProviderAuthStatus(provider).configured;
        authByProvider.set(provider, status);
      }
      return status;
    };

    const result = await ctx.ui.custom<{ model: AnyModel; level: ModelThinkingLevel } | null>(
      (tui: TUI, theme, _kb, done) => {
        let filter = "";
        let sortMode: SortMode = "name";
        let innerPanelWidth = 110;
        const effortIndexByRef = new Map<string, number>();
        const allRefs = models.map(modelRef);

        let widths = columnWidths(models);

        const getFilteredModels = (query: string): AnyModel[] => {
          const matched = fuzzyFilter(models, query, (m) => modelRef(m));
          return matched.slice().sort((a, b) => compareModels(a, b, sortMode));
        };

        const buildItems = (query: string, columnWidthsForView: typeof widths): SelectItem[] =>
          getFilteredModels(query).map((model) => {
            const ref = modelRef(model);
            const full = rowLabel(model, {
              isActive: ref === activeRef,
              hasAuth: hasAuth(model.provider),
              ...columnWidthsForView,
            });
            const paddedRef = full.slice(0, columnWidthsForView.refWidth);
            return {
              value: ref,
              label: styleRowLabel(paddedRef, full.slice(columnWidthsForView.refWidth), theme, {
                hasAuth: hasAuth(model.provider),
                query,
              }),
            };
          });

        const makeSelectList = (query: string, columnWidthsForView: typeof widths): SelectList => {
          const list = new SelectList(
            buildItems(query, columnWidthsForView),
            Math.min(models.length, 12),
            {
              selectedPrefix: (_text) => theme.fg("accent", theme.bold("› ")),
              selectedText: (text) => text,
              description: (text) => theme.fg("muted", text),
              scrollInfo: (text) => theme.fg("dim", text),
              noMatch: (text) => theme.fg("warning", text),
            },
          );
          list.onSelect = (item) => {
            const model = models.find((m) => modelRef(m) === item.value);
            if (!model) return;
            const level: ModelThinkingLevel = model.reasoning ? effortFor(model) : "off";
            done({ model, level });
          };
          list.onCancel = () => done(null);
          list.onSelectionChange = () => {
            prefillHighlighted();
            refresh();
          };
          return list;
        };

        let selectList = makeSelectList("", widths);

        function relayout(): void {
          const filtered = getFilteredModels(filter);
          const ctxWidth =
            filtered.length > 0
              ? Math.max(...filtered.map((m) => formatContext(m.contextWindow).length))
              : 1;
          const costWidth =
            filtered.length > 0 ? Math.max(...filtered.map((m) => formatCost(m.cost).length)) : 1;
          const badgeMax =
            filtered.length > 0
              ? Math.max(
                  ...filtered.map(
                    (m) =>
                      badgesFor(m, {
                        isActive: modelRef(m) === activeRef,
                        hasAuth: hasAuth(m.provider),
                      }).length,
                  ),
                )
              : 0;
          const budget = Math.max(40, innerPanelWidth - 7);
          const refMax = budget - (2 + ctxWidth + 4 + 2 + costWidth + badgeMax);
          widths = columnWidths(filtered, {
            maxBadgeLen: badgeMax,
            maxRefWidth: Math.max(12, refMax),
          });
          widths = { ...widths, maxBadgeLen: undefined } as typeof widths;
          selectList = makeSelectList(filter, widths);
          prefillHighlighted();
        }

        const highlighted = (): AnyModel | undefined =>
          models.find((m) => modelRef(m) === selectList.getSelectedItem()?.value);

        const effortFor = (model: AnyModel): ModelThinkingLevel => {
          const levels = effortLevelsFor(model);
          const index = effortIndexByRef.get(modelRef(model)) ?? 0;
          return levels[Math.min(index, levels.length - 1)];
        };

        const prefillHighlighted = (): void => {
          const model = highlighted();
          if (!model) return;
          const ref = modelRef(model);
          if (effortIndexByRef.has(ref)) return;
          const prefill = prefillEffort(model, activeRef, pi.getThinkingLevel());
          effortIndexByRef.set(ref, effortLevelsFor(model).indexOf(prefill));
        };

        const refresh = () => tui.requestRender();

        /** Format segmented buttons for reasoning effort */
        function formatSegmentedButtons(
          levels: ModelThinkingLevel[],
          current: ModelThinkingLevel,
        ): string {
          const segments = levels.map((lvl) => {
            const label = EFFORT_DISPLAY_NAMES[lvl] ?? lvl;
            if (lvl === current) {
              return `\x1b[7m\x1b[1m ${label} \x1b[0m`;
            }
            return theme.fg("dim", ` ${label} `);
          });
          return theme.fg("dim", "[") + segments.join(theme.fg("dim", "│")) + theme.fg("dim", "]");
        }

        /** Detail panel: reasoning effort for the highlighted model */
        function renderDetailPanel(): string {
          const model = highlighted();
          if (!model) return "";
          if (!model.reasoning) {
            return (
              "  " +
              theme.bold("Reasoning effort") +
              "  " +
              theme.fg("dim", "[ off │ min │ low │ medium │ high │ xhigh ]") +
              "   " +
              theme.fg("dim", "effort: not available for this model")
            );
          }
          const levels = effortLevelsFor(model);
          const current = effortFor(model);
          const buttons = formatSegmentedButtons(levels, current);
          return (
            "  " +
            theme.bold("Reasoning effort") +
            "  " +
            buttons +
            "   " +
            theme.fg(
              "dim",
              `effort: ${current} ${"▮".repeat(Math.max(0, levels.indexOf(current)))} · ←→ adjust · applies to selected model`,
            )
          );
        }

        function applyFilterDelta(delta: string): void {
          filter = delta;
          relayout();
          refresh();
        }

        function cycleSort(): void {
          if (sortMode === "name") sortMode = "price-asc";
          else if (sortMode === "price-asc") sortMode = "price-desc";
          else sortMode = "name";
          relayout();
          refresh();
        }

        function handleInput(data: string): void {
          if (matchesKey(data, Key.tab)) {
            cycleSort();
            return;
          }

          if (matchesKey(data, Key.ctrl("s"))) {
            const model = highlighted();
            if (model) {
              void saveDefaultModel(ctx.cwd, model.provider, model.id).then((outcome) => {
                ctx.ui.notify(
                  outcome.ok
                    ? `Default model saved: ${modelRef(model)}`
                    : `Failed to save default model: ${outcome.error}`,
                  outcome.ok ? "info" : "error",
                );
                refresh();
              });
            }
            return;
          }
          if (matchesKey(data, Key.ctrl("a"))) {
            const ids = filter ? buildItems(filter, widths).map((item) => item.value) : allRefs;
            const allIds = allRefs;
            let currentPatterns: string[] | null;
            try {
              currentPatterns = SettingsManager.create(ctx.cwd).getEnabledModels() ?? null;
            } catch {
              currentPatterns = null;
            }
            void saveEnabledModels(ctx.cwd, unionEnabled(currentPatterns, ids, allIds)).then(
              (outcome) => {
                ctx.ui.notify(
                  outcome.ok
                    ? `Enabled models saved (${ids.length} shown)`
                    : `Failed to save enabled models: ${outcome.error}`,
                  outcome.ok ? "info" : "error",
                );
                refresh();
              },
            );
            return;
          }

          if (
            matchesKey(data, Key.up) ||
            matchesKey(data, Key.down) ||
            matchesKey(data, Key.enter) ||
            matchesKey(data, Key.escape)
          ) {
            selectList.handleInput(data);
            prefillHighlighted();
            refresh();
            return;
          }

          if (matchesKey(data, Key.left) || matchesKey(data, Key.right)) {
            const model = highlighted();
            if (model && model.reasoning) {
              const levels = effortLevelsFor(model);
              const index = effortIndexByRef.get(modelRef(model)) ?? 0;
              const next = matchesKey(data, Key.left)
                ? Math.max(0, index - 1)
                : Math.min(levels.length - 1, index + 1);
              effortIndexByRef.set(modelRef(model), next);
              refresh();
            }
            return;
          }

          if (data === "\x7f" || data === "\b") {
            applyFilterDelta(filter.slice(0, -1));
            return;
          }

          if (data.length === 1 && data >= " ") {
            applyFilterDelta(filter + data);
          }
        }

        const container = new Container();

        // Title row: left-aligned title, esc hint right-aligned.
        const titleHolder = {
          render(width: number): string[] {
            const title = "⚙ Switch Model";
            const escHint = "esc";
            const pad = Math.max(1, width - 4 - visibleWidth(title) - visibleWidth(escHint));
            return [
              "  " +
                theme.fg("accent", theme.bold(title)) +
                " ".repeat(pad) +
                theme.fg("dim", escHint),
            ];
          },
          invalidate(): void {},
        };
        container.addChild(titleHolder);

        // Search input box enclosed in a rounded frame: ╭─...─╮ / │ search │ / ╰─...─╯
        const searchBoxHolder = {
          render(width: number): string[] {
            const boxWidth = Math.max(10, width - 4);
            const inner = boxWidth - 2;
            const top = "  " + theme.fg("dim", "╭" + "─".repeat(inner) + "╮");
            const bottom = "  " + theme.fg("dim", "╰" + "─".repeat(inner) + "╯");

            let content = "";
            if (filter) {
              content = ` ${filter} ${theme.fg("dim", `(filter: ${filter})`)}`;
            } else {
              content = ` ${theme.fg("dim", "Search models…")}`;
            }
            const paddedContent = truncateToWidth(content, inner, "", true);
            const padNeeded = Math.max(0, inner - visibleWidth(paddedContent));
            const middle =
              "  " +
              theme.fg("dim", "│") +
              paddedContent +
              " ".repeat(padNeeded) +
              theme.fg("dim", "│");

            return [top, middle, bottom];
          },
          invalidate(): void {},
        };
        container.addChild(searchBoxHolder);

        // Section divider line
        const dividerHolder = {
          render(width: number): string[] {
            const inner = Math.max(1, width - 4);
            return ["  " + theme.fg("dim", "─".repeat(inner))];
          },
          invalidate(): void {},
        };
        container.addChild(dividerHolder);

        // Column header: model, ctx, cost
        const headerHolder = {
          render(): string[] {
            return [
              "  " +
                theme.fg("dim", "model") +
                " ".repeat(Math.max(1, widths.refWidth + widths.ctxWidth - 2)) +
                theme.fg("dim", "ctx") +
                theme.fg("dim", "  ") +
                theme.fg("dim", "cost $in / $out per 1M tokens"),
            ];
          },
          invalidate(): void {},
        };
        container.addChild(headerHolder);

        // Model list holder
        const listHolder = {
          render(width: number): string[] {
            return selectList.render(width);
          },
          invalidate(): void {
            selectList.invalidate();
          },
        };
        container.addChild(listHolder);

        // Divider before reasoning effort
        container.addChild(dividerHolder);

        // Detail panel: Reasoning effort
        const detailPanelHolder = {
          render(): string[] {
            return [renderDetailPanel()];
          },
          invalidate(): void {},
        };
        container.addChild(detailPanelHolder);

        // Divider before footer
        container.addChild(dividerHolder);

        // Footer shortcuts matching HTML mockup pills
        const footerHolder = {
          render(): string[] {
            const pill = (key: string, label: string) =>
              theme.fg("dim", "[") +
              theme.fg("text", key) +
              theme.fg("dim", "]") +
              " " +
              theme.fg("dim", label);

            const sortLabel =
              sortMode === "name"
                ? "sort: name"
                : sortMode === "price-asc"
                  ? "sort: price ↑"
                  : "sort: price ↓";

            return [
              "  " +
                [
                  pill("↑", "") + pill("↓", "navigate"),
                  pill("Tab", sortLabel),
                  pill("Enter", "select"),
                  pill("Esc", "cancel"),
                  pill("Ctrl+S", "save default"),
                  pill("Ctrl+A", "enable all"),
                ].join("  "),
            ];
          },
          invalidate(): void {},
        };
        container.addChild(footerHolder);

        prefillHighlighted();

        return {
          render(width: number): string[] {
            innerPanelWidth = Math.max(1, width - 4);
            return renderPanel(container.render(innerPanelWidth), width, theme);
          },
          invalidate(): void {
            container.invalidate();
          },
          handleInput,
        };
      },
      {
        overlay: true,
        overlayOptions: { width: "80%", minWidth: 80 },
      },
    );

    if (!result) return;

    if (modelRef(result.model) !== activeRef) {
      const ok = await pi.setModel(result.model);
      if (!ok) {
        ctx.ui.notify(`No API key for ${modelRef(result.model)}`, "warning");
        return;
      }
    }
    pi.setThinkingLevel(result.level);
    ctx.ui.notify(`Model: ${modelRef(result.model)} · effort: ${result.level}`, "info");
  }

  pi.registerCommand("models", {
    description: "Pick a model (shows context window, cost, reasoning effort)",
    handler: async (_args, ctx) => {
      await openPicker(ctx);
    },
  });

  pi.registerShortcut(Key.ctrlShift("m"), {
    description: "Open the model picker",
    handler: async (ctx) => {
      await openPicker(ctx);
    },
  });
}

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
 * discovered by failure. Models are grouped under uppercase provider
 * headers, matching the reference mockup.
 *
 * Keys:
 *   ↑/↓        move highlight (wraps, skipping over provider headers)
 *   ctrl+p     move highlight down (same as ↓; matches the global
 *              next-model cycle key)
 *   shift+ctrl+p move highlight up (same as ↑)
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
 * Row layout has no native "section header" support in pi-tui's SelectList
 * (it indexes a flat item array directly), so the list here is hand-rolled:
 * its own selection index, scroll window, and provider-header insertion —
 * see `listHolder` in `openPicker`.
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

/** Bracketed badge chips for a row ("  [reasoning] [vision] [no key]"), or "" when none apply. */
export function badgesFor(model: AnyModel, opts: { hasAuth?: boolean } = {}): string {
  const badges: string[] = [];
  if (model.reasoning) badges.push("[reasoning]");
  if (model.input?.includes("image")) badges.push("[vision]");
  if (opts.hasAuth === false) badges.push("[no key]");
  return badges.length > 0 ? `  ${badges.join(" ")}` : "";
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

/** Plain (unstyled) fields shared by `rowLabel` and the interactive row renderer. */
interface RowFields {
  dot: string;
  ref: string;
  badgeText: string;
  ctxText: string;
  costText: string;
  pad: number;
}

/**
 * Compute the plain-text pieces of a row: the active-model dot, the
 * (possibly truncated) provider/id ref, badges, width-padded context/cost
 * values, and the left/right gap. `rowWidth`, when given, right-aligns the
 * context/cost block to a fixed column by truncating the ref (never the
 * badges or values) to fit; omitted, the row is simply "left  right".
 */
function computeRowFields(
  model: AnyModel,
  opts: {
    isActive?: boolean;
    hasAuth?: boolean;
    ctxWidth?: number;
    costWidth?: number;
    rowWidth?: number;
  },
): RowFields {
  const badgeText = badgesFor(model, opts);
  const dot = opts.isActive ? "● " : "";
  let ref = modelRef(model);
  const ctx = formatContext(model.contextWindow);
  const ctxText = opts.ctxWidth ? ctx.padStart(opts.ctxWidth) : ctx;
  const cost = formatCost(model.cost);
  const costText = opts.costWidth ? cost.padStart(opts.costWidth) : cost;
  const rightLen = ctxText.length + " context".length + 2 + costText.length;

  let pad = 2;
  if (opts.rowWidth !== undefined) {
    const availableForLeft = Math.max(1, opts.rowWidth - rightLen - 2);
    const fixedLen = dot.length + badgeText.length;
    if (fixedLen + ref.length > availableForLeft) {
      const maxRefLen = Math.max(1, availableForLeft - fixedLen);
      ref = ref.length > maxRefLen ? `${ref.slice(0, Math.max(1, maxRefLen - 1))}…` : ref;
    }
    const leftLen = dot.length + ref.length + badgeText.length;
    pad = Math.max(2, opts.rowWidth - leftLen - rightLen);
  }

  return { dot, ref, badgeText, ctxText, costText, pad };
}

/**
 * Plain-text row: "● provider/id  [badges]   200K context  $3.00 / $15.00".
 * Context and cost right-align within `rowWidth` when given, truncating the
 * ref (never the badges/values) if the name doesn't fit.
 */
export function rowLabel(
  model: AnyModel,
  opts: {
    isActive?: boolean;
    hasAuth?: boolean;
    ctxWidth?: number;
    costWidth?: number;
    rowWidth?: number;
  } = {},
): string {
  const f = computeRowFields(model, opts);
  return `${f.dot}${f.ref}${f.badgeText}${" ".repeat(f.pad)}${f.ctxText} context  ${f.costText}`;
}

/**
 * Column widths (over the current filtered/sorted set) that keep context and
 * cost values the same width across rows, so the right-aligned block lands
 * on the same column regardless of which rows are visible.
 */
export function columnWidths(models: AnyModel[]): { ctxWidth: number; costWidth: number } {
  return {
    ctxWidth:
      models.length > 0 ? Math.max(...models.map((m) => formatContext(m.contextWindow).length)) : 1,
    costWidth: models.length > 0 ? Math.max(...models.map((m) => formatCost(m.cost).length)) : 1,
  };
}

/**
 * Styled row for the interactive overlay: reuses `computeRowFields` so the
 * colored output has exactly the same field widths/positions as the plain
 * `rowLabel` text above — coloring never changes layout.
 */
function styleModelRow(
  model: AnyModel,
  theme: Theme,
  opts: {
    isActive?: boolean;
    hasAuth?: boolean;
    isSelected?: boolean;
    query?: string;
    ctxWidth?: number;
    costWidth?: number;
    rowWidth?: number;
  },
): string {
  const marker = opts.isSelected ? theme.fg("accent", theme.bold("› ")) : "  ";
  const f = computeRowFields(model, opts);

  if (opts.hasAuth === false) {
    const plain = `${f.dot}${f.ref}${f.badgeText}${" ".repeat(f.pad)}${f.ctxText} context  ${f.costText}`;
    return marker + theme.fg("dim", plain);
  }

  const dotStyled = f.dot ? theme.fg("success", f.dot) : "";

  let refStyled: string;
  if (opts.query) {
    const hits = new Set(matchIndices(f.ref, opts.query));
    let s = "";
    for (let i = 0; i < f.ref.length; i++) {
      s += hits.has(i) ? theme.fg("accent", theme.bold(f.ref[i])) : f.ref[i];
    }
    refStyled = s;
  } else {
    const slash = f.ref.indexOf("/");
    refStyled =
      slash === -1
        ? theme.bold(f.ref)
        : theme.fg("dim", f.ref.slice(0, slash + 1)) + theme.bold(f.ref.slice(slash + 1));
  }

  const badgeStyled = f.badgeText.replace(/\[(\w+)\]/g, (_match, name: string) => {
    if (name === "reasoning") return theme.fg("accent", `[${name}]`);
    if (name === "vision") return theme.fg("success", `[${name}]`);
    return `[${name}]`;
  });

  const costStyled =
    f.costText.trim() === "free"
      ? theme.fg("success", f.costText)
      : f.costText.trim() === "—"
        ? theme.fg("dim", f.costText)
        : theme.fg("warning", f.costText);
  const ctxStyled = theme.fg("text", f.ctxText) + theme.fg("dim", " context  ");

  return marker + dotStyled + refStyled + badgeStyled + " ".repeat(f.pad) + ctxStyled + costStyled;
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
        let selectedIndex = 0;
        let widths = columnWidths(models);
        const effortIndexByRef = new Map<string, number>();

        const getFilteredModels = (query: string): AnyModel[] => {
          const matched = fuzzyFilter(models, query, (m) => modelRef(m));
          return matched.slice().sort((a, b) => compareModels(a, b, sortMode));
        };

        const highlighted = (): AnyModel | undefined => getFilteredModels(filter)[selectedIndex];

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

        function moveSelection(delta: 1 | -1): void {
          const filtered = getFilteredModels(filter);
          if (filtered.length === 0) return;
          selectedIndex = (selectedIndex + delta + filtered.length) % filtered.length;
        }

        function relayout(): void {
          widths = columnWidths(getFilteredModels(filter));
          selectedIndex = 0;
          prefillHighlighted();
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
            const ids = getFilteredModels(filter).map(modelRef);
            const allIds = models.map(modelRef);
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

          if (matchesKey(data, Key.up)) {
            moveSelection(-1);
            prefillHighlighted();
            refresh();
            return;
          }
          if (matchesKey(data, Key.down)) {
            moveSelection(1);
            prefillHighlighted();
            refresh();
            return;
          }
          // Reuse the global Ctrl+P / Shift+Ctrl+P next/previous-model cycle
          // semantics while the overlay is open, so cycling works the same
          // with or without the picker in front. ctrlShift is checked before
          // the plain chord, matching the ←/→ tandem checks below.
          if (matchesKey(data, Key.ctrlShift("p"))) {
            moveSelection(-1);
            prefillHighlighted();
            refresh();
            return;
          }
          if (matchesKey(data, Key.ctrl("p"))) {
            moveSelection(1);
            prefillHighlighted();
            refresh();
            return;
          }
          if (matchesKey(data, Key.enter)) {
            const model = highlighted();
            if (model) {
              const level: ModelThinkingLevel = model.reasoning ? effortFor(model) : "off";
              done({ model, level });
            }
            return;
          }
          if (matchesKey(data, Key.escape)) {
            done(null);
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

        // Title row: left-aligned title (Esc is already in the footer pills).
        const titleHolder = {
          render(): string[] {
            return ["  " + theme.fg("accent", theme.bold("⚙ Switch Model"))];
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

        // Model list holder: hand-rolled selection/scroll + provider-grouped headers.
        const listHolder = {
          render(width: number): string[] {
            const filtered = getFilteredModels(filter);
            if (filtered.length === 0) {
              return ["  " + theme.fg("warning", "No matching models")];
            }
            if (selectedIndex >= filtered.length) selectedIndex = filtered.length - 1;
            if (selectedIndex < 0) selectedIndex = 0;

            const maxVisible = Math.min(filtered.length, 12);
            const startIndex = Math.max(
              0,
              Math.min(selectedIndex - Math.floor(maxVisible / 2), filtered.length - maxVisible),
            );
            const endIndex = Math.min(startIndex + maxVisible, filtered.length);
            const rowWidth = Math.max(20, width - 4);

            const lines: string[] = [];
            for (let i = startIndex; i < endIndex; i++) {
              const model = filtered[i]!;
              const prevProvider = i > 0 ? filtered[i - 1]!.provider : undefined;
              if (model.provider !== prevProvider) {
                if (i > startIndex) lines.push("");
                lines.push("  " + theme.fg("dim", theme.bold(model.provider.toUpperCase())));
              }
              const ref = modelRef(model);
              lines.push(
                "  " +
                  styleModelRow(model, theme, {
                    isActive: ref === activeRef,
                    hasAuth: hasAuth(model.provider),
                    isSelected: i === selectedIndex,
                    query: filter || undefined,
                    ctxWidth: widths.ctxWidth,
                    costWidth: widths.costWidth,
                    rowWidth,
                  }),
              );
            }
            if (startIndex > 0 || endIndex < filtered.length) {
              lines.push("  " + theme.fg("dim", `(${selectedIndex + 1}/${filtered.length})`));
            }
            return lines;
          },
          invalidate(): void {},
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
                  pill("Ctrl+P", "") + pill("⇧Ctrl+P", "cycle"),
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
            return renderPanel(container.render(Math.max(1, width - 4)), width, theme);
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

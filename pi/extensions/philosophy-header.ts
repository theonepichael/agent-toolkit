/**
 * Philosophy Header
 *
 * Replaces pi's built-in header (logo + keybinding hints) with a π wordmark
 * and a tagline drawn from ~/dotfiles — STYLE.md and claude/global-instructions.md.
 *
 * The tagline is picked once per session, so it stays stable while you work
 * but varies across sessions.
 *
 * Restore the built-in header with /builtin-header.
 */

import type { ExtensionAPI, Theme } from "@earendil-works/pi-coding-agent";
import { truncateToWidth } from "@earendil-works/pi-tui";

/** π wordmark, 17 columns wide. */
export const PI_LOGO: readonly string[] = [
  "█████████████████",
  "█████████████████",
  "  ███       ███  ",
  "  ███       ███  ",
  "  ███       ███  ",
  "  ███       ███  ",
  "  ███       ███  ",
  "  ███       ███  ",
];

/** Verbatim or near-verbatim from ~/dotfiles. */
export const TAGLINES: readonly string[] = [
  "prevent the whole class, not just this instance",
  "verification means running it",
  "uniformity is paramount — small, explicit, testable",
  "a test that can't be made to fail first isn't verifying anything",
  "normal verbosity by default",
  "never commit directly to main",
];

/** Colorizer with the same shape as `Theme.fg`, narrowed to what the header uses. */
export type Colorize = (role: "accent" | "muted" | "dim", text: string) => string;

/**
 * Choose a tagline from a 0..1 sample.
 *
 * Split out from the header factory so the selection is deterministic under
 * test: `Math.random()` stays at the single call site in the default export.
 * Values at or past 1 (and any NaN) clamp to the last entry rather than
 * indexing off the end.
 */
export function pickTagline(sample: number): string {
  const count = TAGLINES.length;
  const index = Number.isFinite(sample) ? Math.floor(sample * count) : 0;
  const clamped = Math.min(Math.max(index, 0), count - 1);
  return TAGLINES[clamped]!;
}

/**
 * Build the header's rendered lines, clipped to `width`. Pure, so the layout
 * is testable.
 *
 * `width` is required rather than optional: pi kills the whole session with an
 * uncaughtException when a header line is wider than the terminal, and this
 * header once did exactly that -- a worker pane 42 columns wide died on the
 * 64-character tagline. An optional width would let a caller reintroduce the
 * crash by omitting it, so the type system insists.
 *
 * Every line is clipped, not just the tagline, so a line added here later
 * cannot bring the crash back. The wordmark is 17 columns and the hint line
 * 40, both of which overflow a narrow pane on their own.
 */
export function renderHeaderLines(tagline: string, fg: Colorize, width: number): string[] {
  const lines = [
    "",
    ...PI_LOGO.map((line) => fg("accent", line)),
    "",
    fg("muted", `  ${tagline}`),
    fg("dim", "  /hotkeys · ctrl+c clear · escape abort"),
    "",
  ];
  // Clipping after colorizing, the way custom-footer.ts does it --
  // truncateToWidth measures visible columns, so the escapes do not count.
  return lines.map((line) => truncateToWidth(line, width));
}

export default function (pi: ExtensionAPI) {
  let tagline = TAGLINES[0]!;

  pi.on("session_start", async (_event, ctx) => {
    if (ctx.mode !== "tui") return;

    // One tagline per session — stable while you work, varied across sessions.
    tagline = pickTagline(Math.random());

    ctx.ui.setHeader((_tui, theme: Theme) => ({
      render(width: number): string[] {
        return renderHeaderLines(tagline, (role, text) => theme.fg(role, text), width);
      },
      invalidate() {},
    }));
  });

  pi.registerCommand("builtin-header", {
    description: "Restore pi's built-in header",
    handler: async (_args, ctx) => {
      ctx.ui.setHeader(undefined);
      ctx.ui.notify("Built-in header restored", "info");
    },
  });
}

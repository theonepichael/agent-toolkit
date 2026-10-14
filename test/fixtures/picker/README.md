# Picker fixtures

Shared by both picker ports: `pi/test/swarm-tool.test.ts` (TypeScript) and
`test/test_herdr_picker.py` (Python). Each `<name>.txt` is a `question-tool.ts`
picker capture (plain text, ANSI stripped); `<name>.expected.json` is the
behaviour both ports must agree on:

- `selectedIndex` — the highlighted option, or `null`;
- `options` — the reassembled option labels, in order;
- `match` — `{answer, expect}` cases for `matchOption`/`match_option`
  (`expect` is the matched label, or `null` for "refuses");
- `navigation` — `{from, to, arrows}` cases for the arrow-key prefix of
  `navigationKeys`/`navigation_keys` (the TS `navigationKeys` appends
  `"enter"`; the Python twin returns the arrows alone, and parity is asserted
  on the prefix only).

Both suites iterate this directory: a `.txt` with no `.expected.json` (or an
expected file with no `.txt`) fails both suites. The `edit-*.txt` fixtures are
a separate, generated set (see `pi/test/render-picker-fixtures.ts`); they are
written by the real pi-tui renderer and never hand-authored.

## The 2026-10-14 needs_human misclassification

During the 2026-10-14 mvc2 foreman run, the pi orchestrator classified a pi
worker's commit picker as `needs_human -- NOT a question-tool picker`
(`swarm-tool-context.ts:2510`) when it in fact was one. The cause is recorded
here rather than left as folklore.

`needs-human-repro.txt` reproduces the **most likely mechanism (hypothesis)**: a
commit picker whose capture has the footer and closing rule but **no opening
rule and no option lines** — the frame the reader caught before
`question-tool.ts`'s `requestRender()` had repainted the picker. `parse_picker`
correctly returns no options for it (`classifyBlock` → `needs_human`) because
there is genuinely no parseable picker in that frame. The original 2026-10-14
capture was **not retained**, so this is a reconstruction of the read-timing
race the spec's Context notes (`question-tool.ts` redraws after input, so a
read right after `send-keys` may show the pre-input frame), not a replay of the
actual screen.

The verdict is **not a parser bug**: against the fully-rendered commit picker
(`numbered-plan.txt`) the same parser returns the three options and `selectedIndex
1`. The failure was the caller reading the pane exactly once and giving up on
an empty result. The supported fix is not a parser change but bounded polling
in `herdr_delegate.py answer`: it re-reads the pane until the picker identity
appears (or `--settle-s` gives up), instead of trusting the first read.

# Permission compiler (`gen_permissions.py`)

## Boundary / responsibility

`agent-scripts/permission_matrix.py` is the one hand-edited source of the
bash allow / ask / deny policy for every harness's global seed.
`agent-scripts/gen_permissions.py` compiles it into:

- `claude/settings.json` and `claude/settings.work.json`, in the
  `permissions.allow` / `ask` / `deny` lists only;
- `opencode/opencode.jsonc`, in `permission.bash` only (`external_directory`
  is still hand-authored);
- `pi/extensions/permission-gate.ts`, between the
  `// <permission-matrix:begin>` and `// <permission-matrix:end>` anchors.

Before it existed, these lists were kept by hand and drifted apart. Claude
had 3 allows where opencode had 86, and only Claude had the
`dev_status.py prune` deny.

## Key interfaces

- **Neutral grammar.**
  - `*` matches any text.
  - A trailing ` *` that is the only wildcard also matches the bare
    command.
  - `<dir>` matches one whitespace-free argument, and is legal only after a
    leading `git -C `.
  - `?` and the legacy `:*` are rejected.
- **Neutral semantics.**
  - Commands are judged per segment, and deny beats ask beats allow.
  - Allow rules match from the start of a segment.
  - Ask and deny rules also match after any whitespace, so they hold behind
    `FOO=1 …` or `nice -n 5 …`.
- **Exceptions.** An exception only removes ALLOW rules from one target,
  and must carry a written reason (`validate()` rejects one under 20
  characters):
  - `PatternException` removes one named pattern. Pi uses it for the
    scripts that have native Pi tools.
  - `TokenException` applies when a target cannot express a token. It drops
    `<dir>` allows on Claude and opencode, whose only wildcard spans
    spaces.
- **Emitters.**
  - Claude gets `Bash(…)` rules, relying on its deny > ask > allow
    precedence.
  - opencode gets `"*": "ask"`, then allows, then asks, then denies, since
    the last match wins.
  - Pi gets string arrays in neutral syntax, compiled by `patternToRegExp`.
  - Ask and deny rules are emitted twice for the glob targets: bare and
    behind a leading `* `.
- **CLI.**
  - Running it with no flags rewrites the targets, each by an atomic
    replace after a fresh read.
  - `--check` exits 1 on drift, and is run by `githooks/pre-commit` and by
    the test suite.
  - `--stdout` prints the rendered targets and writes nothing.
  - `--audit-live` is a read-only seed-to-live comparison for
    `~/.claude/settings.json` (or the work seed, by profile marker) and
    `~/.config/opencode/opencode.jsonc`. It never touches
    `settings.local.json`.
  - Exit 2 means an invalid matrix or an unreadable target.
- **Matchers.** `claude_verdict` and `opencode_verdict` model the
  documented and binary-verified matching. They back the shared corpus in
  `test/fixtures/permission_corpus.json`, which Pi's real `classify()` also
  runs. They are regression checks for the emitters; they do not prove the
  real harnesses' behavior.
- **Two-key review.** `test/test_install.py::_APPROVED_BASH_PATTERNS` is a
  hand-kept copy of the shared ALLOW set. A policy change edits both.

## Explicit non-goals

- Codex's `.codex/rules/default.rules` and the repo-local
  `.claude/settings.json` are not generated.
- `~/.claude/settings.local.json` is never read or written.
- Live configs are not rewritten. `settings_seed_drift_check.py fix` adds
  seed rules to live (allows go right after opencode's catch-all; deny and
  ask merge as a union). It never removes a live allow or reorders live
  keys, so `--audit-live` reports what still needs pruning by hand.

## Concurrency

Never run `gen_permissions.py` and `gen_hooks.py` at the same time. Both
rewrite `claude/settings*.json` whole: `gen_hooks.py` owns `hooks`, this one
owns `permissions`, and `gen_hooks.py` writes the snapshot it read. Run
serially, in either order, each leaves the other's `--check` green (tested).
No lock is shared between them, by decision.

The one-owner rule in `scripts/check_toolkit_paths.py generators` is per
file, so `claude/settings*.json` stay on the `gen_hooks.py` row.

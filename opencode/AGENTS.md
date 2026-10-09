# AGENTS.md — opencode

<!-- Paired with CLAUDE.md symlink in the same directory -->

## Responsibilities & Boundary

This directory is the opencode harness port: generated skills
(`opencode/skills/`), `opencode/command/`, `opencode/plugin/`, and
opencode's own config (`opencode.jsonc`, `tui.json`). It owns
opencode-specific packaging only — the shared workflow logic these skills
wrap lives in `agent-scripts/`.

## Hazards & Signposts

- `opencode/command/*` and `opencode/skills/*` are generated from the shared
  `templates/*.tmpl` files. `agent-scripts/gen_skills.py` writes every
  command except second-opinion, plus the `spec`, `grill-me`, `review-diff`
  and `land` skills (its synthetic `opencode-skill` harness: same facts as
  the command copy, with `name:` frontmatter and no `$ARGUMENTS`, since
  `skill({ name })` passes none). `gen_second_opinion.py` writes both
  second-opinion copies. Never hand-edit either directory — edit the
  template or the harness parameter table and regenerate; hand-edits are
  silently overwritten. A new skill that other skills load with
  `skill({ name })` on opencode needs an `opencode-skill` row in
  `gen_skills.SKILL_HARNESSES` and a `links.toml` entry, or the load fails.
- `opencode.jsonc`'s `permission.bash` block is generated — never hand-edit
  it. `agent-scripts/gen_permissions.py` compiles it, Claude's
  `settings*.json` permissions and Pi's `permission-gate.ts` lists from the
  one matrix in `agent-scripts/permission_matrix.py` (last-match-wins order
  behind a `"*": "ask"` catch-all). Edit the matrix and the hand-approved
  literal in `test/test_install.py`, then regenerate; `--check` fails
  pre-commit and CI on drift. `external_directory` is still hand-authored.
  Codex's rules are not generated and stay aligned by hand.
- Full porting/parity record (confirmed facts, open decisions, keybind
  conflicts, hooks-vs-plugin-system tradeoffs) lives in
  `opencode/CLAUDE_CODE_PARITY.md` — read it before changing how a skill or
  permission rule behaves relative to Claude Code.

## Local Conventions

`opencode/package.json` drives four stages, all run from the repository suite
by `test/test_opencode_ts_checks.py` via `npm run <stage>`:

| Stage | Command |
|---|---|
| `test` | `node --import tsx --test --test-concurrency=1 --test-timeout=30000 test/*.test.ts` |
| `typecheck` | `tsc --noEmit` |
| `lint` | `oxlint plugin test` |
| `format:check` | `prettier --check plugin test` |

The specs use Node's built-in test runner and strict assertions directly.
Static imports in `opencode/test/plugins.test.ts` load all three plugin factories
without invoking their hooks or opencode's runtime registration machinery.

Run the stages from `opencode/`. A fresh worktree has no
`opencode/node_modules` or `opencode/node_modules/.bin`; the Python gate fails
rather than skipping until `npm install` or `scripts/bootstrap-worktree.sh` has
run. Lint and format cover only `plugin` and `test`, deliberately leaving this
file and its `CLAUDE.md` symlink outside Prettier's TypeScript path.

Regenerate skills with `python3 agent-scripts/gen_skills.py` (and
`python3 agent-scripts/gen_second_opinion.py` for second-opinion).

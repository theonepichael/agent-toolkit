# Agent-scripts modularity audit

## Decision

The runtime scripts are **partly modularized already**. The strongest pattern is
thin command adapters over reusable storage, formatting, provider, or protocol
modules. It is enough to support a curated Python API incrementally, but not
enough to safely advertise every module-level function as reusable.

Preserve each CLI as the compatibility surface. Add small, capability-level
service APIs behind selected CLIs; do not make models import command handlers
or underscored helpers directly.

This is an inspection audit, not a behavior change. It covers production
runtime modules in `agent-scripts/`; colocated tests and standalone maintenance
generators/checks are listed only where that distinction affects a boundary.

The portfolio below covers every high-value candidate across three
implementation waves, not only a single first pilot — see
[Candidate API portfolio](#candidate-api-portfolio) and
[Recommended execution order](#recommended-execution-order).

## What a supported capability API means

A supported API must have:

- typed input and result objects (or narrow primitives where appropriate);
- documented file, subprocess, network, and mutation effects;
- domain errors rather than `print()` plus `sys.exit()`;
- a caller-owned or explicitly scoped transaction/lock for mutation; and
- CLI handlers that adapt arguments, output, and errors onto that API.

This is intentionally not an `import *` policy. `INTERFACES.md`'s public
function listing is an AST naming convention, not a stability guarantee.

## Inventory

| Workflow / modules | Current shape | Classification | Audit result |
| --- | --- | --- | --- |
| `dev_status.py` → `dev_status_impl.py`, `dev_status_storage.py`, `dev_status_formatting.py` | Launcher plus a 5,526-line implementation; storage and rendering are already extracted | Needs design first | The best eventual API target and the highest-risk extraction. Command handlers still coordinate validation, ID/revision resolution, locking, mutation, journaling, reminders, rendering, process execution, and CLI exits. |
| `grill.py` | Single 1,235-line state CLI with typed records, locking, validation, rendering, and command handlers | Low-risk extraction | Its lifecycle is already a useful capability model: create/load session, record decision, revise, verdict, frontier, and attach plan. Separate state service from CLI rendering and `die()`. |
| `standup.py` + `standup_adapters.py` | Small fetch orchestrator with explicit adapter protocols | Already suitable / low-risk | Source functions are already separate and adapters have clear contracts. Introduce a `StandupSources`/configuration object and `fetch_standup()` result rather than exposing `cmd_fetch()`. |
| `analyze_sessions.py` | One large read-only analyzer with per-harness loaders and CLI render handlers | Low-risk extraction | `SessionRecord`, timestamp normalization, individual loaders, and `load_all_records()` are strong seams. Add a query/filter object and pure result builders; retain CLI output formatting separately. |
| `second_opinion.py` + `llm_backends.py` | One-shot review orchestration over a substantial shared backend/isolation library | Already suitable with a small facade | `llm_backends` is a real reusable boundary: isolation eligibility, command construction, fallback and response parsing. Keep the security contract centralized; expose `review_plan()` rather than backend-specific private helpers. |
| `to_tickets_runner.py` + `dev_status` | Batch schema/order/resume logic invoking the backlog module | Needs design first | Batch validation and dependency ordering are good pure seams, but creation currently depends on `dev_status`'s module alias and command-shaped API. Stabilize backlog creation first. |
| `vitals_promotion.py` + `grill` data | Read/classify/promote/search workflow | Low-risk extraction | `matches_query`, `classify_decision`, `search_vitals`, and report generation already separate useful concerns. Add result objects and depend on grill's formalized session-read API instead of reading its on-disk JSON directly. |
| `guard_rails.py` (+ a few `dev_status_impl` pid/machine-identity helpers) | Guard evaluator with git, process, filesystem, and backlog-claim checks | Needs design first | `evaluate()` is a promising decision boundary, but it currently duplicates its own backlog-item read logic (`load_in_progress()`) instead of reusing `dev_status_storage`'s. Replace that duplicated read with a narrow read-only claim lookup protocol; its separate, narrower `dev_status_impl` import for pid/machine-identity helpers is a legitimate coupling to keep. |
| `herdr_delegate.py` + `dev_status` | Swarm launch/resume orchestration over backlog selection | Needs design first | Command construction and state parsing are separable, but readiness/serial safety are coupled to the broad backlog module. Re-express readiness against a narrow read-only claim-lookup protocol, satisfied by a local adapter rather than a shared facade dependency. |
| `settings_seed.py` + `settings_seed_drift_check.py` | Extracted seed library plus a drift/fix/sync CLI | Already suitable / low-risk | This is the clearest existing precedent: pure drift helpers and injected subprocess access sit below orchestration. Finish de-duplication by making drift-check consume its shared helpers. |
| `link_inspect.py` + `link_drift_check.py` | Inspection library plus SessionStart/CLI wrapper | Already suitable | Clean library/adapter split. Keep its findings as immutable result records if a consumer API is wanted. |
| `outlook_email.py`, `outlook_calendar.py`, `standup_adapters.py` | Platform adapters and protocol translation | Already suitable | Their direct-provider methods are the correct component boundary; callers should depend on the adapter protocols, not PowerShell details. |
| `cli_common.py` | Shared logging, verbosity, formatting, timing, and redaction utilities | Already suitable | Utility-only module. No model-facing facade is needed. |
| `notify.py` | Cross-platform notification dispatcher | Not a target | Small, single-purpose side-effect adapter. Keep whole-operation invocation. |
| `statusline.py` | stdin payload to one status-line string | Not a target | Purposefully narrow; a pure rendering function is sufficient. |
| `bundle_drift_check.py`, `harness_discovery_check.py`, `sessionstart_checks.py`, `link_drift_check.py`, `settings_seed_drift_check.py`, `seed_hook_subset_guard.py` | Hook/check entrypoints | Mostly not targets | Whole-workflow/hook contracts are safer than granular model-directed operations. Reuse their pure inspection functions only. |
| `refresh_guidance.py` | Document discovery, claim analysis, git history, persisted review state, CLI reporting | Low-risk extraction | Discovery and finding construction can become a read-only audit service; state marking remains an explicit effect. |
| `gen_interfaces.py`, `gen_second_opinion.py`, `gen_shell_completion.py`, `gen_skills.py`, `gen_skills_params.py` | Repository generation tooling | Out of runtime target | These are maintenance tools, not model-runtime APIs. Their internal modularity should be audited separately if needed. |

## Observed architecture

### Existing seams worth preserving

- **`dev_status` already splits persistence and formatting.** `dev_status.py` is
  a deliberate compatibility launcher; `dev_status_storage.py` owns atomic
  writes, locks, revision data, journals, and run evidence; and
  `dev_status_formatting.py` owns text presentation. Do not undo that split.
- **`llm_backends` centralizes the security-critical policy.** Its isolation
  descriptor, containment check, command construction, backend eligibility,
  and process lifecycle must remain one policy boundary. A caller must not
  choose an arbitrary subprocess path to "reuse" it.
- **`standup_adapters` uses protocol boundaries.** Its platform-specific
  implementations can vary without changing the fetch orchestrator.
- **`settings_seed` demonstrates the desired dependency direction.** Pure
  drift calculation and injected process execution live below installer/CLI
  orchestration.
- **`analyze_sessions` has explicit input paths.** Its loaders accept paths,
  which makes read-only reuse and tests safer than a module that always reads
  the real home directory.

### Gaps that block safe direct reuse

1. **Command handlers are APIs by accident.** Many `cmd_*` functions mix
   parsing-shaped namespaces, mutation, printing, and process termination.
   They are not a safe library surface.
2. **Process-global configuration is common.** `Path.home()` constants and
   patchable module globals suit the CLI/test model, but obscure dependencies
   for an importing caller. New APIs should accept a typed path/config object.
3. **`SystemExit` is used for domain failures.** Preserve it in CLI adapters;
   translate it to typed exceptions or structured failure results below them.
4. **Some consumers import implementation-shaped modules.** In particular,
   guard and swarm code depend on the broad backlog surface. That makes an
   extraction order important.
5. **Pure data construction and presentation remain mixed in several
   scripts.** Read-only callers should receive records/results and choose
   their own JSON/text rendering.

## Candidate API portfolio

Every candidate below is drawn from the inventory above. Grouping into waves
reflects two constraints: (a) read-only/self-contained services come before
anything that depends on them, and (b) higher-risk mutation or orchestration
systems wait for the conventions and dependency seams that earlier waves
establish. A wave label is a sequencing recommendation, not a hard gate —
items within a wave can proceed in any order relative to each other.

### Wave 1 — read-only or already-isolated services

Six of these eight candidates (1–5, 9) have no dependency on another
candidate in this portfolio; candidates 6 and 10 each depend on candidate
9's small shared module (see "Recommended execution order" below for the
exact `9 → {6, 10}` constraint). Each candidate both delivers standalone
value and establishes a convention (typed result objects, injected paths,
domain errors) that later waves reuse.

#### 1. `standup.fetch_standup` — read-only source aggregation service

- **Why wave 1:** short, mostly read-only, already decomposed by data source
  (`standup_adapters.py`), with adapter protocols already in place.
- **Proposed interface:**
  ```python
  @dataclass(frozen=True)
  class StandupConfig: ...

  @dataclass(frozen=True)
  class StandupPaths: ...

  @dataclass(frozen=True)
  class SkippedSource:
      source: str  # e.g. "chat", "email", "calendar", "issue_tracker"
      reason: str

  @dataclass
  class StandupReport:
      date: date
      since: date
      git_commits: list[dict[str, str]]
      skipped_sources: list[SkippedSource]
      # the remaining per-source records

  @dataclass(frozen=True)
  class StandupSources:
      issue_tracker: standup_adapters.IssueTrackerAdapter | None = None
      chat: standup_adapters.ChatAdapter | None = None
      email: standup_adapters.EmailAdapter | None = None
      calendar: standup_adapters.CalendarAdapter | None = None

  def fetch_standup(
      config: StandupConfig,
      sources: StandupSources,
      *,
      reference_date: date | None = None,
      paths: StandupPaths,
  ) -> StandupReport: ...
  ```
- **Effect boundary:** read-only. No draft file is written by the service
  itself; git/provider calls happen through the existing adapter protocols.
- **Error/result model:** the report always carries skipped-source
  diagnostics inline rather than raising for a single failed source; only a
  config/path error raises a typed exception.
- **CLI adapter story:** `cmd_fetch` parses `--date`, loads configuration,
  calls `fetch_standup()`, and serializes the report; it keeps today's exact
  JSON schema, stderr behavior, and exit status.
- **Tests:** call `fetch_standup()` with fake adapters and temporary paths;
  add a red/green pair proving the call fails before extraction and passes
  after; keep existing CLI tests asserting output schema unchanged.
- **Dependencies:** none within this portfolio.
- **Risk controls:** must preserve the existing skipped-source behavior
  exactly; must not gain a write effect; `StandupSources` must be typed
  against `standup_adapters.py`'s existing `@runtime_checkable` protocols
  rather than an untyped `Mapping[str, object]`, so a caller cannot pass an
  object that only accidentally satisfies the adapter contract.

#### 2. `analyze_sessions.query_sessions` — read-only session query/report service

- **Why wave 1:** `SessionRecord`, per-harness loaders, and
  `load_all_records()` already take explicit paths; only a query/filter
  layer and pure report builders are missing.
- **Proposed interface:**
  ```python
  @dataclass(frozen=True)
  class SessionQuery:
      harnesses: frozenset[str] | None = None
      since: datetime | None = None
      until: datetime | None = None
      search: str | None = None

  @dataclass(frozen=True)
  class SkippedRecord:
      path: Path
      harness: str
      reason: str

  @dataclass(frozen=True)
  class SessionQueryResult:
      records: list[SessionRecord]
      cost_summary: dict[str, object]
      skipped_records: list[SkippedRecord]

  def query_sessions(
      query: SessionQuery,
      *,
      roots: Mapping[str, Path],
  ) -> SessionQueryResult: ...
  ```
- **Effect boundary:** read-only filesystem reads under caller-supplied
  roots; no network or subprocess calls.
- **Error/result model:** a malformed session file is a skipped-record
  diagnostic on the result, not a raised exception, matching the existing
  per-harness loader tolerance.
- **CLI adapter story:** `cmd_cost`, `cmd_prompts`, and `cmd_search` become
  thin: build a `SessionQuery` from argv, call `query_sessions()`, and keep
  each command's own text/JSON rendering.
- **Tests:** fixture session files per harness under a temporary root;
  assert query filtering and cost aggregation against fixed inputs.
- **Dependencies:** none within this portfolio.
- **Risk controls:** do not fold the three CLI commands' distinct rendering
  needs into one shared formatter; only the query/filter/load layer is
  shared.

#### 3. `second_opinion.review_plan` facade over `llm_backends`

- **Why wave 1:** `llm_backends` is already the correct security boundary
  (isolation eligibility, command construction, fallback, response
  parsing); `second_opinion.py` mostly orchestrates prompt construction
  around it.
- **Proposed interface:**
  ```python
  @dataclass(frozen=True)
  class ReviewRequest:
      plan_text: str
      focus_hints: str | None = None
      backend: str | None = None
      model_index: int | None = None

  @dataclass(frozen=True)
  class ReviewResult:
      backend_label: str
      response_text: str
      sanitized_char_count: int

  def review_plan(request: ReviewRequest) -> ReviewResult: ...
  ```
- **Effect boundary:** delegates all subprocess/network effects to
  `llm_backends.run_with_fallback` / `run_agy` / `run_opencode` /
  `run_copilot` / `run_pi`; the facade itself adds no new effect surface.
- **Error/result model:** re-raise `llm_backends.BackendError` and its
  subclasses (`BackendTimeoutError`, `BackendPayloadSizeError`,
  `BackendModelPolicyError`) rather than converting them to `die()`/`sys.exit`
  inside the facade; only `cmd_review` translates to a CLI exit.
- **CLI adapter story:** `cmd_review` builds a `ReviewRequest` from argv,
  calls `review_plan()`, and keeps today's stdout/stderr shape and exit
  codes.
- **Tests:** existing `llm_backends` fakes/fixtures cover backend behavior;
  add a facade-level test asserting `review_plan()` surfaces
  `BackendError` subclasses unmodified.
- **Dependencies:** none new — depends on `llm_backends`, already a stable
  internal boundary.
- **Risk controls:** never expose backend-specific private helpers
  (`run_agy`, `_resolve_pooled_model`, etc.) as part of the facade's public
  surface; the facade's only public entry point is `review_plan()`.

#### 4. `settings_seed` / drift-check consolidation

- **Why wave 1:** this is the clearest existing precedent for the target
  shape (pure drift helpers below injected process execution); the
  remaining work is finishing de-duplication, not new design.
- **Proposed interface:** no new public function — `settings_seed_drift_check.py`'s
  duplicated `_load_json_pair`, `settings_drift`/`describe_settings_drift`-shaped
  helpers, and `resolve_profile`/path-resolution logic should import and
  delegate to `settings_seed.py`'s already-public equivalents instead of
  reimplementing them locally.
- **Effect boundary:** unchanged — file reads/writes stay exactly where
  they are today (`_atomic_write`, `_atomic_write_text` in
  `settings_seed_drift_check.py`; `_adopt_seed`, `_reseed_file` in
  `settings_seed.py`).
- **Error/result model:** unchanged; this candidate is a de-duplication,
  not a new API surface.
- **CLI adapter story:** no CLI change; `check`/`fix`/`sync-to-seed`/
  `push-vscode` keep their exact output.
- **Tests:** existing `test_settings_seed.py` and
  `test_settings_seed_drift_check.py` are the regression net; add one test
  asserting the drift-check module calls the shared helper rather than a
  reimplementation, to prevent silent re-divergence.
- **Dependencies:** none within this portfolio.
- **Risk controls:** none beyond keeping the two modules' observable
  behavior identical before and after de-duplication.

#### 5. `link_inspect` / `link_drift_check` result API

- **Why wave 1:** already a clean library/adapter split; the only gap is
  that some inspection functions (`audit_links`, `check_applicable_links`,
  `check_orphaned_links`, `check_unmanaged_files`) mix finding construction
  with printing.
- **Proposed interface:**
  ```python
  @dataclass(frozen=True)
  class LinkFinding:
      kind: str  # e.g. "orphaned", "unmanaged", "drifted"
      path: Path
      detail: str

  def collect_link_findings(
      links: list[LinkSpec],
      managed_dirs: list[ManagedDirSpec],
      *,
      home: Path,
  ) -> list[LinkFinding]: ...
  ```
- **Effect boundary:** read-only filesystem/symlink inspection under a
  caller-supplied `home`; no writes.
- **Error/result model:** findings are data, never printed or raised from
  the collection function; the CLI/hook layer decides rendering and exit
  status.
- **CLI adapter story:** `link_inspect.py`'s and `link_drift_check.py`'s
  `cmd_check` handlers call `collect_link_findings()` and keep their
  existing text/quiet/JSON output exactly as today.
- **Tests:** build a temporary `home` with representative symlink/managed-dir
  fixtures and assert the finding list, independent of any printed text.
- **Dependencies:** none within this portfolio.
- **Risk controls:** keep the SessionStart-hook-facing entrypoint's output
  format byte-for-byte identical; hook consumers are harder to test live.

#### 6. `dev_status` read-only query facade

- **Why wave 1:** `dev_status_impl.py` as a whole is the highest-risk
  module, but a genuinely pure read over its on-disk store — as distinct
  from the existing `cmd_show`/`cmd_list`/`cmd_render` CLI paths, which are
  not actually pure (see Effect boundary below) — is low-risk to add. It is
  not a hard prerequisite for candidates 9 or 10 (see their entries) — if
  anything the dependency runs the other way, since this candidate imports
  `backlog_claim_lookup.py` from candidate 9 — but candidate 12 (wave 3)
  depends on it, and landing it early lets it become 9/10's shared
  `BacklogClaimLookup` implementation sooner.
- **Proposed interface:** imports `ClaimInfo` and `BacklogClaimLookup` from
  `backlog_claim_lookup.py` (candidate 9) rather than redefining them:
  ```python
  from backlog_claim_lookup import ClaimInfo, BacklogClaimLookup

  @dataclass(frozen=True)
  class BacklogQuery:
      status: str | None = None
      prefix: str | None = None

  def get_item(slug_or_id: str, *, items_path: Path | None = None) -> BacklogItem | None: ...
  def ready_items(query: BacklogQuery | None = None, *, items_path: Path | None = None) -> list[BacklogItem]: ...
  def in_progress_items(*, items_path: Path | None = None) -> list[BacklogItem]: ...
  def item_status(slug_or_id: str, *, items_path: Path | None = None) -> str | None: ...  # None for an unknown id, matching get_item
  def claim_info(slug_or_id: str, *, items_path: Path | None = None) -> ClaimInfo | None: ...

  class DevStatusClaimLookup(BacklogClaimLookup):
      """Implements backlog_claim_lookup.py's protocol over this module's
      functions, so this facade can become a drop-in replacement for
      candidates 9 and 10's LocalClaimLookup without changing either
      candidate's call sites."""
      def ready_items(self, prefix: str | None = None) -> list[BacklogItem]:
          return ready_items(BacklogQuery(prefix=prefix))
      def in_progress_items(self) -> list[BacklogItem]:
          return in_progress_items()
      def claim_info(self, slug_or_id: str) -> ClaimInfo | None:
          return claim_info(slug_or_id)
  ```
  Every function's `items_path` defaults to `None`, matching
  `dev_status_storage.load_items(path: Path | None = None)`'s own existing
  pattern (it falls back to the module's resolved default when omitted) —
  this candidate does not invent a new path-injection convention, it
  extends the one `load_items`/`save_items` already have.
- **Effect boundary:** these functions are **not** equivalent to
  `cmd_show`/`cmd_list`/`cmd_render` as they exist today, and this
  candidate does not claim to reproduce those commands' full behavior —
  only to add a genuinely pure read path that doesn't exist yet.
  Concretely: `cmd_render()`'s own docstring documents that it is *not*
  side-effect-free — it runs `_sweep_dead_claims()` under `backlog_lock()`
  and, when a claim actually needs reverting, bumps `rev` and saves items
  before reading them, specifically so the printed `(items, rev)` pair
  stays self-consistent. There is no separate shared/read-only lock
  anywhere in `dev_status_storage.py` today — `backlog_lock()` is always
  `LOCK_EX`. This facade's functions call `dev_status_storage.load_items()`
  directly (a plain file read, no sweep, no write, no lock beyond a brief
  hold of the existing exclusive `backlog_lock()` — used the same way
  `cmd_render()` already does, purely to keep an `(items, rev)` snapshot
  internally consistent, not as a new locking primitive) and never bump
  `rev`, write the journal, or mutate a claim.
- **CLI adapter story:** `cmd_show`, `cmd_ready`, and `cmd_list` are **not**
  simply rewired to call this facade and nothing else — they keep calling
  `_sweep_dead_claims()` exactly as today (preserving that documented side
  effect), and use this facade only for the item lookup/formatting step
  after the sweep decision is made, so their observable output and effects
  are unchanged. A caller that wants the facade's actually-pure behavior —
  candidates 9, 10, and 12 — calls it directly, not through these CLI
  commands.
- **Error/result model:** `get_item` returns `None` for an unknown id
  rather than raising or exiting; slug/id resolution errors are a typed
  exception, not `sys.exit`.
- **Tests:** exercise the facade against a temporary backlog store fixture;
  assert it never acquires a write lock (beyond the same brief exclusive
  hold `cmd_render()` already uses for snapshot consistency) and never
  bumps `rev`. Separately, assert `cmd_show`/`cmd_list`/`cmd_render`'s
  existing sweep-and-conditionally-write behavior is unchanged by this
  extraction.
- **Dependencies:** candidate 9's `backlog_claim_lookup.py` module (for
  `ClaimInfo` and the `BacklogClaimLookup` protocol this facade
  implements). Candidates 9 and 10 themselves depend on neither this
  candidate nor each other (see their entries); this candidate is expected
  to become their shared protocol implementation once it lands, and
  candidate 12 (wave 3) depends on it for a consistent read/write API
  shape.
- **Risk controls:** this facade must not grow mutation methods; a
  mutation need routes to candidate 12 instead, once it exists. It must
  never be presented as a drop-in replacement for `cmd_show`/`cmd_list`/
  `cmd_render`'s current implementation internals — only as a new,
  genuinely pure capability those commands can additionally call.

#### 9. `guard_rails` evaluator facade

- **Why wave 1:** `guard_rails.py` does not actually depend on
  `dev_status_impl` for reading backlog items — `load_in_progress()` reads
  `backlog_items_path()` (its own `GUARD_RAILS_STORE`-overridable path
  resolution) and parses the JSON directly, duplicating rather than
  reusing `dev_status_storage.load_items()`. It does import `dev_status_impl`
  lazily for a few pid/machine-identity helpers (`_find_owner_pid()`,
  `machine_id()`, `_is_pid_alive()`, used by `_session_identity()`/
  `_claim_is_active()`/`_claim_holder_alive()`), which is a narrower,
  legitimate coupling this candidate keeps rather than removes. The
  candidate's own `BacklogClaimLookup` protocol replaces the *duplicated
  item-reading logic*, not a dependency on `dev_status_impl`'s mutation
  surface — and only needs *an* implementation of the protocol, not
  candidate 6's shared, multi-consumer facade specifically, so it does not
  need to wait on that landing first.
- **Proposed interface:** this candidate is where `backlog_claim_lookup.py`
  — the one small module both this candidate and candidate 10 depend on —
  gets defined. Candidate 6 later depends on this module (implementing its
  protocol), not the other way around, so there is no forward reference to
  a type that might not exist yet:
  ```python
  # backlog_claim_lookup.py
  @dataclass(frozen=True)
  class ClaimInfo:
      machine_id: str
      owner_pid: int       # the durable claim holder, per dev_status_impl._make_claim()
      pid: int             # the short-lived invoking pid, distinct from owner_pid
      last_active: str | None = None
      claimed_at: str | None = None

  class BacklogClaimLookup(Protocol):
      def in_progress_items(self) -> list[BacklogItem]: ...
      def ready_items(self, prefix: str | None = None) -> list[BacklogItem]: ...
      def claim_info(self, slug_or_id: str) -> ClaimInfo | None: ...

  class LocalClaimLookup:
      """Thin wrapper over guard_rails.py's existing item-reading logic,
      until candidate 6 lands as the shared implementation."""
      def in_progress_items(self) -> list[BacklogItem]: ...  # wraps load_in_progress(), preserving its "None on unreadable storage" tolerance
      def ready_items(self, prefix: str | None = None) -> list[BacklogItem]: ...  # wraps cmd_ready's read path
      def claim_info(self, slug_or_id: str) -> ClaimInfo | None: ...  # reads the same claim record load_in_progress() already parses
  ```
  ```python
  # guard_rails.py
  def evaluate(req: Request, claims: BacklogClaimLookup) -> Verdict: ...
  ```
  `guard_rails` calls only `in_progress_items()` and `claim_info()` —
  matching what `evaluate()` actually depends on today
  (`load_in_progress()`/`_busy_item()` for the busy-directory check,
  `_claim_is_active()`/`_claim_holder_alive()` for the liveness check), not
  `ready_items()`, which is what candidate 10 needs instead.
- **Effect boundary:** read-only; git/process/filesystem checks stay as
  today (`git()`, `repo_info()`, `_behind_origin_main()`), only the
  backlog-item-reading dependency changes shape. The narrower
  `dev_status_impl` import for pid/machine-identity helpers stays as-is —
  this candidate does not attempt to remove it.
- **Error/result model:** unchanged — `Verdict` stays the result type;
  `LocalClaimLookup.in_progress_items()` preserves `load_in_progress()`'s
  existing `None`-on-unreadable-storage behavior rather than raising.
- **CLI adapter story:** `main()` constructs a concrete `LocalClaimLookup`
  and passes it to `evaluate()`; hook output format is unchanged. The
  adapter can be swapped for candidate 6's facade once that lands, without
  changing `evaluate()`'s signature.
- **Tests:** existing guard tests can inject a fake `BacklogClaimLookup`
  instead of monkeypatching `dev_status_impl` internals directly — a
  strictly better test seam than today's.
- **Dependencies:** none within this portfolio. Defines
  `backlog_claim_lookup.py` (`ClaimInfo`, `BacklogClaimLookup`,
  `LocalClaimLookup`), which candidate 10 also imports — this is a real
  same-wave ordering dependency (10 after 9), named explicitly rather than
  described as "no dependency," per the Recommended execution order
  section below. Neither `guard_rails` nor `herdr_delegate` owns the
  module or imports the other, avoiding the accidental coupling a
  "whichever lands first" placement would create. Candidate 6 depends on
  this module once it lands (see its entry), not the reverse.
- **Risk controls:** `evaluate()` must never gain a path back into
  `dev_status_impl`'s mutation surface; the protocol is read-only by
  construction. `LocalClaimLookup` must not reach beyond the three
  read-only operations it wraps today, so swapping in candidate 6 later is
  a drop-in replacement, not a redesign.

#### 10. `herdr_delegate` / swarm queue facade

- **Why wave 1:** command construction (`build_tab_argv`,
  `build_agent_start_argv`, prompt builders) and state parsing
  (`parse_tab_list`, `discover_run_id`) are already separable pure
  functions. Readiness selection (`ready_slugs()`) can be re-expressed
  against the same `BacklogClaimLookup` protocol candidate 9 defines,
  satisfied by its own narrow local adapter — this candidate does not need
  candidate 6 to exist first, only a read-only claim lookup of some kind.
- **Proposed interface:**
  ```python
  def select_ready(prefix: str | None, claims: BacklogClaimLookup) -> list[BacklogItem]: ...
  def build_launch_plan(items: list[BacklogItem], *, kind: str) -> list[list[str]]: ...
  ```
  `select_ready` calls `claims.ready_items(prefix)` — the other operation
  `backlog_claim_lookup.py`'s protocol defines (see candidate 9), not
  `in_progress_items()`, which is what `guard_rails` needs instead.
- **Effect boundary:** `select_ready`/`build_launch_plan` are pure/read-only;
  the actual herdr subprocess spawn (`spawn_in_new_tab`, `herdr()`) stays a
  separate, explicitly-effectful call the CLI layer makes, not something
  the facade performs implicitly.
- **Error/result model:** keep `RefusedError` for herdr-environment
  refusals; readiness selection failures are typed, not `sys.exit`.
- **CLI adapter story:** `cmd_launch`/`cmd_restart` call `select_ready()`
  and `build_launch_plan()`, then perform the actual tab spawn exactly as
  today.
- **Tests:** existing `test_herdr_delegate.py` fixtures apply; add tests
  for `select_ready` against a fake `BacklogClaimLookup`.
- **Dependencies:** none within this portfolio. Imports the one shared
  `BacklogClaimLookup` protocol and local adapter from a small dedicated
  module (e.g. `backlog_claim_lookup.py`) that candidate 9 also imports —
  neither `guard_rails` nor `herdr_delegate` defines it or imports the
  other; a swarm-orchestration CLI depending on a pre-commit guard script,
  or vice versa, is exactly the kind of accidental coupling this portfolio
  exists to avoid. There is exactly one local implementation, not two, and
  it lives in neither consumer. Like candidate 9, it can
  adopt candidate 6 as the shared implementation later without a signature
  change.
- **Risk controls:** keep the actual subprocess/tab-spawn boundary
  (`spawn_in_new_tab`, `herdr()`) outside the facade so a caller cannot
  "compute a plan" and accidentally also launch it.

### Wave 2 — higher design cost, and not uniformly self-contained

Unlike wave 1, not every candidate here is independent: candidate 8
specifically depends on candidate 7 landing first (see its entry). Grouping
them in one wave reflects design cost, not a shared independence property —
candidate 7 can start as soon as wave 1 begins, but candidate 8 cannot start
before candidate 7's read API exists.

#### 7. `grill` session service — decision lifecycle API

- **Why wave 2:** the lifecycle model (create/load session, record
  decision, revise, verdict, frontier, attach plan) is strong, but
  `grill.py` mixes CLI rendering, `die()` calls, and file locking directly
  into what should be a state-transition API — more design work than the
  wave 1 candidates, though it has no dependency on another candidate here.
- **Proposed interface:**
  ```python
  class GrillSessionError(Exception): ...
  class DecisionNotFoundError(GrillSessionError): ...
  class CycleError(GrillSessionError): ...

  def open_session(slug: str, *, data_dir: Path | None = None) -> Session: ...
  def all_sessions(data_dir: Path | None = None) -> list[Session]: ...
  def record_decision(slug: str, patch: DecisionPatch, *, data_dir: Path | None = None) -> Decision: ...
  def revise_decision(slug: str, decision_id: str, patch: DecisionPatch, *, data_dir: Path | None = None) -> Decision: ...
  def record_verdict(slug: str, decision_id: str, verdict: Verdict, *, data_dir: Path | None = None) -> Decision: ...
  def frontier_of(session: Session) -> DecisionList: ...
  ```
  `all_sessions()` formalizes the bulk-load `load_all_sessions()` currently
  duplicated inside `vitals_promotion.py` (candidate 8), so that candidate
  can depend on this module's public read API instead of re-reading grill's
  on-disk TypedDict JSON directly. `DecisionPatch` is a partial-update
  `TypedDict` (`total=False`) mirroring `Decision`'s fields — a documented,
  narrow exception to the "no raw dict payload" convention below: today's
  CLI already accepts partial-field JSON patches via `parse_json_arg()`,
  and a `TypedDict(total=False)` keeps that flexible partial-update shape
  typed (unknown keys and wrong value types are a static-analysis error)
  without forcing every optional field into a full dataclass with a
  present/absent sentinel per field.

  The three mutation functions take `slug`, not a pre-loaded `Session`.
  Today's `cmd_decide()` acquires `session_lock(slug)` and only then calls
  `load_session(slug)` — the lock always wraps the reload, never a
  caller-supplied snapshot. A function accepting an already-loaded
  `Session` would leave it ambiguous whether the mutation reloads under
  its own lock (in which case the caller's copy was pointless) or trusts
  the caller's possibly-stale snapshot (risking a lost concurrent update);
  taking `slug` and doing the lock-then-reload internally, matching
  `cmd_decide()`'s existing order exactly, removes that ambiguity.
- **Effect boundary:** file writes under `session_lock()`/`_new_session_lock()`,
  preserved exactly as today — each mutation function acquires the lock
  and reloads the session from disk internally, in that order, rather than
  accepting a caller-supplied `Session` value; no network or subprocess
  effects. `open_session()`/`all_sessions()` are unlocked reads, matching
  today's `cmd_ask` and equivalent read paths.
- **Error/result model:** replace `die()` calls inside the service layer
  with `GrillSessionError` subclasses (`DecisionNotFoundError`,
  `CycleError`, and a validation-error type for `_validate_depends_on`
  failures); only the CLI layer calls `die()`/exits.
- **CLI adapter story:** `cmd_decide`, `cmd_revise`, `cmd_verdict`, and
  `cmd_ask` become argument-parsing plus rendering wrappers; `die()` moves
  to the boundary, catching the new exception types.
- **Tests:** existing `test_grill.py` fixtures apply; add tests that call
  the service functions directly (bypassing argv) and assert the same
  locking/validation behavior, including the existing cycle-detection test
  paths.
- **Dependencies:** none within this portfolio.
- **Risk controls:** locking semantics (`session_lock`, `_new_session_lock`)
  must not be weakened or bypassed by direct-import callers; the service
  functions are the only sanctioned entry point, not the TypedDict mutation
  helpers.

#### 8. `vitals_promotion` service — classification/search/promote API

- **Why wave 2:** `matches_query`, `classify_decision`, `search_vitals`,
  and report generation are already pure, but `run()` conflates a
  read-mostly classification pass with an explicit `apply` write mode that
  needs a deliberate boundary. It now also depends on candidate 7's
  `all_sessions()`, so it should not start before that read function
  exists.
- **Proposed interface:**
  ```python
  def search(vitals_dir: Path, keywords: list[str]) -> list[VitalsRecord]: ...
  def classify(sessions: list[Session]) -> Report: ...  # read-only; never writes
  def promote(sessions: list[Session], vitals_dir: Path) -> Report: ...  # always writes
  ```
- **Effect boundary:** `search`/`classify` are read-only; `promote` is the
  only write path and always writes — matching today's `run(data_dir,
  apply)` split, made explicit at the function-name level *instead of* a
  boolean flag, not alongside one; `promote()` must not grow an `apply`
  parameter back, which would reintroduce the exact ambiguity (what would
  `apply=False` mean when `classify()` already covers that case?) the
  function-name split exists to remove.
- **Error/result model:** typed `Report` (already a `TypedDict`) stays the
  result type; no new exceptions needed beyond existing I/O errors.
- **CLI adapter story:** `main()` calls candidate 7's `all_sessions(data_dir)`
  to load the input, then `classify`/`promote` and `print_report` stay thin
  wrappers around the result.
- **Tests:** existing fixtures over `load_all_sessions`/`run()` extend
  directly to the split functions, now constructed via a fake/fixture
  `list[Session]` instead of a temporary data directory; assert `classify`
  never writes.
- **Dependencies:** candidate 7 (`grill` session service), for
  `all_sessions()` — not grill's on-disk TypedDict JSON format directly.
  Reading another candidate's file format directly, instead of through its
  formalized read API once one exists, is a hidden coupling: it would let
  `vitals_promotion` silently break if grill's session schema evolved
  during candidate 7's own extraction, with nothing enforcing the shared
  contract.
- **Risk controls:** `promote()` must remain the only mutating entry point
  (`classify()` is read-only by construction, not by a flag); do not let
  `classify`/`search` grow a hidden write path; do
  not let this module re-add its own direct grill-session-file reader once
  candidate 7's `all_sessions()` exists, even as a "temporary" shortcut.

#### 11. `refresh_guidance` audit service

- **Why wave 2:** discovery (`discover_agents_md`, `discover_scripts`,
  `discover_undocumented_dirs`) and finding construction
  (`check_path_claim`, `check_command_claim`, `run_check`) are read-only
  and already return structured `Finding`/`CheckResult` dataclasses, but
  the module also owns persisted review state (`load_state`/`save_state`)
  that must stay an explicit, separately-called effect — this needs more
  design care than a pure wave 1 candidate even though it has no
  dependency on another candidate here.
- **Proposed interface:** mostly not a new top-level function — `run_check()`
  is already close to the target shape (`CheckResult`, `SectionStatus`,
  `Finding` are already typed dataclasses/results), and `cmd_mark_reviewed()`
  already takes `doc`, `heading`, `commit`, `date` and writes state keyed by
  `f"{doc}#{heading}"` with `last_reviewed_commit`/`last_reviewed_date`/
  `reviewed_by` fields — this candidate keeps that shape rather than
  inventing a new one. The one real change closes an orchestration-mismatch
  gap without claiming more than a type system can actually enforce: a
  frozen dataclass wrapping `state` still has a public constructor (nothing
  stops `ReviewedState(hand_built_dict)`), so a "only `mark_reviewed()` can
  produce a valid state" claim wired through a wrapper *type* doesn't
  actually hold. Simpler and honest instead: `save_state()` is not exported
  from this candidate's public surface at all; `mark_reviewed()` is the
  only supported writer and calls the module's existing (now-private)
  persistence helper itself.
  ```python
  def mark_reviewed(
      repo_root: Path,
      doc_set: DocSetConfig,
      doc: str,
      heading: str,
      result: CheckResult,
      *,
      commit: str | None = None,
      date: str | None = None,
  ) -> None: ...  # validates f"{doc}#{heading}" is a section in result,
                   # resolves commit/date the same way cmd_mark_reviewed()
                   # does today, then persists via the module's existing
                   # (private) state-write helper
  ```
- **Effect boundary:** `run_check()` is read-only; `mark_reviewed()` is the
  one write effect and must stay a distinct, explicit call — not folded
  into `run_check()` — so a caller cannot silently mark a section reviewed
  while only intending to check it.
- **Error/result model:** already typed (`ConfigError`, `Finding`,
  `CheckResult`); add one validation error from `mark_reviewed()` for a
  `doc#heading` absent from the supplied `CheckResult`, matching
  `cmd_mark_reviewed()`'s existing "no such heading" exit today.
- **CLI adapter story:** `cmd_check` calls `run_check()` and
  `render_report()`; `cmd_mark_reviewed` calls `run_check()` then
  `mark_reviewed()` — two explicit steps, not one bundled call, and the
  CLI's existing `--commit`/`--date` flags pass straight through.
- **Tests:** existing fixtures over doc-set configs extend directly; add a
  test asserting `run_check()` never persists state, and a test asserting
  `mark_reviewed()` rejects a `doc#heading` absent from the supplied
  `CheckResult`.
- **Dependencies:** none within this portfolio.
- **Risk controls:** keep the read/write separation explicit so an
  importing caller cannot silently mark a section reviewed while only
  intending to check it — a combined `run_and_save()` convenience function
  was considered and rejected for this reason. Do not claim a wrapper
  *type* enforces "only `mark_reviewed()` writes valid state" — Python
  cannot make that true without private-module machinery this candidate
  doesn't need; the actual guarantee is "no other public function in this
  module persists state," enforced by not exporting one, not by a type.

### Wave 3 — depends on wave 1 or 2 foundations, highest design/risk cost

#### 12. `dev_status` mutation service

- **Why wave 3:** the highest eventual value and the highest risk.
  Command handlers currently coordinate validation, ID/revision
  resolution, locking, mutation, journaling, reminders, rendering, and
  process exits all together (`cmd_add`, `cmd_update`, `cmd_start`,
  `cmd_done`, `cmd_approve`, `cmd_reject`, `cmd_block`/`cmd_unblock`,
  `cmd_gate_set`/`cmd_gate_pass`, `cmd_rename`, `cmd_remove`, `cmd_run`,
  `cmd_pending_add`/`cmd_pending_update`). This must not be extracted
  command-by-command; it needs one transaction/result/error design shared
  across every mutating operation, built after candidate 6 has proven the
  read-side conventions. `cmd_run()` is an explicit, verified exception to
  "one uniform shape": it releases `backlog_lock()` while its subprocess
  executes, then reacquires it only to append run evidence — it neither
  bumps `rev` nor appends a normal journal event, and its output differs
  from every other mutation's confirmation line. This candidate preserves
  that distinct lock/effect/output shape for `run_item` rather than forcing
  it into the acquire-mutate-release pattern the other functions share.
- **Proposed interface (shape, not final):**
  ```python
  @dataclass(frozen=True)
  class MutationResult:
      slug: str
      status: str
      rev: int
      detail: str

  class BacklogMutationError(Exception): ...
  class RevisionConflictError(BacklogMutationError): ...
  class GateUnmetError(BacklogMutationError): ...

  @dataclass(frozen=True)
  class RelatedFile:
      path: str
      note: str = ""

  @dataclass(frozen=True)
  class NewItemRequest:
      id: str
      summary: str
      category: str
      context: str = ""
      next_steps: str = ""
      related_files: tuple[RelatedFile, ...] = ()
      blocked_by: tuple[str, ...] = ()

  def add_item(request: NewItemRequest, *, items_path: Path | None = None) -> MutationResult: ...
  def start_item(slug_or_id: str, *, if_rev: int | None = None, items_path: Path | None = None) -> MutationResult: ...
  def approve_item(slug_or_id: str, *, if_rev: int | None = None, items_path: Path | None = None) -> MutationResult: ...
  def block_item(slug_or_id: str, blocker_slug_or_id: str, *, if_rev: int | None = None, items_path: Path | None = None) -> MutationResult: ...
  def run_item(slug_or_id: str, command: list[str], *, items_path: Path | None = None) -> RunResult: ...  # distinct result shape — see Why wave 3
  # ... one function per existing cmd_* mutation, each with its own typed
  # request dataclass where the mutation takes more than an id (update,
  # gate-set, gate-pass, rename, pending add/update); a raw
  # dict[str, object] payload is not an acceptable request shape for this
  # candidate, even for the least risky mutation — the whole point of the
  # extraction is to stop accepting untyped payloads at this boundary.
  # `items_path` defaults to None, matching dev_status_storage.load_items()/
  # save_items()'s own existing explicit-path parameter, per candidate 6.

  class BacklogTransaction(Protocol):
      """One open backlog_lock() section, for a caller — candidate 13 — that
      needs several reads and mutations to share a single lock/transaction
      instead of each acquiring its own, matching to_tickets_runner.run()'s
      real today's-behavior: one lock spanning index/pending lookups,
      collision checks, and every ticket's creation."""
      def index(self) -> BacklogIndex: ...
      def pending_items(self) -> list[PendingItem]: ...
      def add_item(self, request: NewItemRequest) -> MutationResult: ...

  @contextmanager
  def mutation_transaction(*, items_path: Path | None = None) -> Iterator[BacklogTransaction]: ...
  ```
- **Effect boundary:** every mutation except `run_item` acquires the same
  exclusive `backlog_lock()`, bumps `rev` *before* any write (preserving
  the existing deliberate ordering — a crash between the bump and a write
  only burns a revision number, never leaves changed data under a stale
  rev), saves items, then appends a journal entry — this service must not
  introduce a second locking or journaling path, only wrap the existing
  one. `run_item` keeps its distinct effect shape (lock released during
  subprocess execution, reacquired only for run-evidence, no rev bump, no
  normal journal event).
- **Error/result model:** `RevisionConflictError` replaces today's
  `--if-rev` stale-rev refusal message; `GateUnmetError` replaces the
  `approve`/`done` gate-refusal exit; both carry the same information the
  CLI already prints, as structured fields instead of stderr text.
  `run_item` returns a distinct `RunResult` rather than `MutationResult`,
  matching its distinct output today.
- **CLI adapter story:** every `cmd_*` mutation handler becomes argv
  parsing, a call to the matching service function, and rendering
  `MutationResult` (or catching a `BacklogMutationError` subclass) into
  today's exact `[<cmd>] slug=... status=... rev=... detail="..."` line
  and exit code — this is the compatibility contract the
  `DEVSTATUS_AGENT=1` structured-output format already documents.
- **Tests:** this is the one candidate that needs new baseline tests
  before extraction begins, and CLI-output-line assertions alone are not
  enough for a module whose entire purpose is safe concurrent mutation.
  Three test tiers are required, not one:
  1. one test per existing `cmd_*` mutation asserting today's structured-
     output line and exit code, run against a temporary backlog store, so
     the extraction can be proven behavior-preserving function by function;
  2. lock-contention tests that assert a second mutation blocks/retries/
     fails exactly as `dev_status_storage.py`'s existing flock behavior does
     today, not merely that both eventually "succeed";
  3. tests characterizing this candidate's actual, verified crash-safety
     properties, not an invented recovery guarantee.
  Tier 1 alone would only catch a regression in the happy path; tiers 2 and
  3 are what actually verify the transaction/lock/journal guarantees this
  candidate exists to preserve. For tier 2, `dev_status_storage.py`'s
  functions already accept explicit paths and `fcntl.flock` is itself a
  patchable boundary — a bespoke new lock-injection seam is not a
  prerequisite for deterministic contention tests; use that existing
  patchability (or bounded process/thread synchronization) rather than
  sleep-timed races, but do not treat "add a new seam" as required
  up-front work.

  For tier 3, this audit verified the actual behavior directly (source:
  `dev_status_impl._backlog_mutation()` and `cmd_add()`, `dev_status_storage.
  append_journal_event()`): every mutation bumps `rev` **before** writing
  items, and writes items **before** appending the journal event — a
  deliberate ordering, documented in `_backlog_mutation()`'s own docstring,
  so that a crash between the bump and a later write only burns a revision
  number (harmless — `--if-rev` just sees a numbering gap), never leaves
  changed data sitting under a stale revision. `append_journal_event()` is
  explicitly best-effort: no `fsync`, and it swallows `OSError` with a
  non-fatal warning. There is no reconciliation of a missing/mismatched
  journal entry against `_meta.json`/items on the next read — the journal
  is a diagnostic trail, not a source of truth the store repairs against.
  One exception exists in the *current* code: `_sweep_dead_claims()` (used
  by `cmd_render()`) journals *before* its caller bumps the revision and
  saves items — the reverse of the normal order — which can leave a
  journal event with no corresponding persisted change. Tier 3 tests must
  assert this real set of properties (rev-bump-first ordering for normal
  mutations, the one reversed-order sweep exception, and journal-write
  best-effort/non-fatal-failure behavior) exactly as it exists today, not
  a from-scratch two-phase-commit guarantee this candidate does not add.
- **Dependencies:** candidate 6 (`dev_status` read-only query facade), for
  a consistent read/write API shape and the `ClaimInfo`/`BacklogItem`
  result types this service's own results build on; benefits from
  candidates 7 and 9 having already proven the "typed errors instead of
  `die()`/`sys.exit`" pattern at smaller scale first.
- **Risk controls:** locks, revision guards, journals, gates, and run
  evidence must be preserved byte-for-byte in behavior; this service must
  never be built by lifting `cmd_*` bodies wholesale — each function needs
  the parsing/exit concerns stripped out, not just renamed. No mutation
  function may call candidate 6's public read functions internally while
  it holds the exclusive mutation lock — even though candidate 6 uses the
  same underlying exclusive `backlog_lock()` rather than a separate one,
  a second `with backlog_lock():` from inside an already-held lock is a
  real re-entrancy risk under `flock`, not a theoretical one, and calling
  back into a second public API from inside a transaction is an
  unnecessary layering violation regardless. A mutation that needs to read
  current state does so through `dev_status_storage.py`'s primitives
  directly, inside its own already-held lock, the same way today's `cmd_*`
  handlers do — never by calling back out to candidate 6.

#### 13. `to_tickets_runner` service

- **Why wave 3:** batch validation (`_validate_batch_schema`), dependency
  ordering (`compute_order`), and resume-state handling
  (`load_state`/`write_state`) are good pure seams already, but ticket
  creation goes through `dev_status`'s module alias and command-shaped
  calls; this candidate is explicitly gated on candidate 12 existing so
  that creation happens through the stabilized mutation service rather
  than by importing `dev_status_impl` internals or shelling out to the
  CLI.
- **Proposed interface:** verified against `to_tickets_runner.run()`'s
  actual body (source: `to_tickets_runner.py:252-330`), which holds *one*
  `backlog_lock()` across order computation, a collision check against
  **both** the backlog index and pending items, and every ticket's
  creation (with `blocked_by` set directly at creation — there is no
  separate "link" step today), aborting the whole batch immediately
  (`SlugCollisionError`, uncaught, `cmd_run()` exits 1) on the first
  collision. Two independently-locking `add_item`/`block_item` calls
  cannot reproduce that — this candidate uses candidate 12's
  `mutation_transaction()` instead:
  ```python
  def validate_batch(path: Path) -> list[Ticket]: ...  # wraps load_batch
  def plan_order(tickets: list[Ticket], index: BacklogIndex) -> list[str]: ...  # wraps compute_order

  def run_batch(
      batch_path: Path,
      open_transaction: Callable[[], AbstractContextManager[BacklogTransaction]],
  ) -> list[str]: ...  # returns created (or already-created, on resume) slugs, in order — matching run()'s existing return shape exactly
  ```
- **Effect boundary:** `validate_batch`/`plan_order` are pure/read-only;
  `run_batch` opens one `mutation_transaction()` for the whole batch (via
  the injected `open_transaction` factory, so tests can supply a fake) and
  performs every collision check and creation inside it, matching today's
  single-lock scope exactly.
- **Error/result model:** keep `BatchError`/`SlugCollisionError` exactly as
  today, including fail-fast semantics — a collision aborts the entire
  batch by raising; it must not become a per-ticket "failed" outcome that
  the batch continues past, since `to_tickets_runner.run()` has no
  partial-failure tolerance today and this candidate must not add any.
- **CLI adapter story:** `cmd_run` stays a thin wrapper around
  `run_batch()`, preserving today's resume-state file behavior
  (`_state_path`, `_batch_hash`, `load_state`/`write_state`/`delete_state`)
  and its existing exit-1-on-collision behavior.
- **Tests:** existing `test_to_tickets_runner.py` batch fixtures extend
  directly once `run_batch` takes an injected transaction factory (real or
  fake) instead of importing `dev_status` as a module alias; add a test
  asserting a mid-batch collision aborts the batch (raises) rather than
  recording a per-ticket failure and continuing.
- **Dependencies:** candidate 12's `mutation_transaction()`/
  `BacklogTransaction`, not its individual per-item functions alone.
- **Risk controls:** do not let `run_batch` grow a second, independent
  path to creating or linking backlog items, or a lock scope narrower than
  today's single whole-batch transaction; every mutation must route
  through the one open `BacklogTransaction`.

## Recommended execution order

1. **Wave 1:** candidates 1–6, 9, and 10 (`standup.fetch_standup`,
   `analyze_sessions.query_sessions`, `second_opinion.review_plan`,
   `settings_seed`/drift-check consolidation, `link_inspect`/
   `link_drift_check` result API, `dev_status` read-only query facade,
   `guard_rails` evaluator facade, `herdr_delegate`/swarm queue facade).
   This wave has one real ordering constraint, not "any order" for all
   eight: candidate 9 defines the shared `backlog_claim_lookup.py` module,
   and both candidate 6 and candidate 10 depend on it (6 to implement its
   protocol, 10 to import its local adapter) — so candidate 9 lands first,
   then `{6, 10}` in either order relative to each other, alongside 1–5 in
   any order. This is `9 → {6, 10}`, a same-wave ordering constraint, not a
   cross-wave one. Candidate 6 should still not be deferred to last in this
   wave even though `dev_status_impl.py` is the largest module, because
   candidate 12 in wave 3 needs it.
2. **Wave 2, after wave 1's conventions exist:** candidate 7 (`grill`
   session service) can start as soon as wave 1 begins, since it has no
   dependency on it — but should still land after at least one wave 1
   candidate ships, so the typed-error/CLI-adapter convention is proven
   once at smaller scale first. Candidate 8 (`vitals_promotion` service)
   specifically requires candidate 7's `all_sessions()` and must wait for
   it. Candidate 11 (`refresh_guidance` audit service) has no hard
   dependency and can proceed whenever design time is available.
3. **Wave 3, last:** candidate 12 (`dev_status` mutation service) should
   not start until wave 1 and at least the `grill`/`guard_rails` work has
   validated the typed-error and compatibility-adapter conventions at
   smaller scale, since it is the highest-risk extraction in the whole
   portfolio and touches every mutating backlog command. Candidate 13
   (`to_tickets_runner` service) is explicitly gated on candidate 12 and
   must come after it.

This order maximizes early value (eight wave-1 read-only/low-risk
services, six of them independent and two — candidates 6 and 10 — waiting
only on candidate 9's small shared module) while ensuring the structurally
required dependencies — `9 → {6, 10}`, candidate 7 before 8, and candidate
12 before 13 — are respected, and ensures the riskiest system (candidate
12) is attempted only after its conventions have been proven elsewhere
first. Neither candidate 9 nor candidate 10 is blocked on candidate 6: a
Protocol-based local adapter, defined once in candidate 9's module, is
sufficient for both to start immediately, with candidate 6 adopted later
as that protocol's shared implementation rather than a prerequisite for
either.

## Cross-cutting API conventions

These conventions apply to every candidate above and should be established
by the first wave 1 extraction that ships, not invented independently per
candidate:

- **Typed inputs and results.** Prefer a frozen `dataclass` for request/query
  objects and either a `dataclass` or the existing `TypedDict` records
  (`Session`, `Decision`, `BacklogItem`, `VitalsRecord`, `SessionRecord`) for
  results. Do not accept or return raw `argparse.Namespace` objects from a
  service function, and do not accept an untyped `dict[str, object]`
  payload for a mutation request either — every mutation service
  function's *whole-object* input (creation, gate-set, and similar
  multi-field operations) gets its own typed request dataclass, not just
  its identifier arguments. The one documented exception is a
  *partial-update patch* over an already-typed record's optional fields
  (`grill`'s `DecisionPatch`, `dev_status update`'s equivalent) — those
  stay a `TypedDict(total=False)` mirroring the target record's fields,
  which keeps the shape statically checked without forcing a
  present/absent sentinel onto every optional field of a full dataclass. A
  persisted on-disk state blob passed through unchanged (`refresh_guidance`'s
  review-state dict, which already mirrors its JSON file 1:1) is a second,
  narrower exception; a genuinely free-form payload with neither
  justification is not.
- **Path/config injection.** A service function takes its data-root paths
  (and, where relevant, an adapter mapping) as explicit arguments rather
  than reading `Path.home()`-derived module constants directly. This is
  what already makes `analyze_sessions`'s loaders and `settings_seed`'s
  helpers safe to test and reuse; every new service should match it.
- **Domain errors, not `sys.exit`.** Each module that gains a service layer
  defines its own narrow exception hierarchy (see `GrillSessionError`,
  `BacklogMutationError`, and the existing `BackendError`/`BatchError`/
  `RefusedError` precedents) and raises those from the service layer. Only
  the CLI adapter (`cmd_*` function or `main()`) catches them and calls
  `die()`/`sys.exit()`.
- **Subprocess/network boundaries stay centralized.** Any service that needs
  to run a subprocess or call a network-backed provider delegates to the
  existing centralized boundary for that concern — `llm_backends` for LLM
  backend calls, `herdr_delegate`'s command builders plus its own explicit
  spawn call for herdr, `standup_adapters`/`outlook_*` for external data
  sources. A new service must not open a second, parallel subprocess path.
- **Lock/transaction handling is preserved, not reimplemented.** Any service
  touching `dev_status_storage.py`-backed state reuses its existing
  lock/revision/journal primitives; any service touching grill session
  files reuses `session_lock()`/`_new_session_lock()`. A service layer wraps
  these, it does not replace them with a new locking scheme — there is no
  separate shared/read lock in `dev_status_storage.py` today (`backlog_lock()`
  is always `LOCK_EX`), and no candidate here invents one. The concrete case
  this matters for in this portfolio: candidate 12's mutation functions
  hold the exclusive `backlog_lock()` for the duration of a mutation and
  must never call candidate 6's functions from inside that section — even
  though candidate 6 uses the same underlying lock rather than a separate
  one, calling back into a second public API from inside an already-held
  lock is a real re-entrancy risk under `flock` and an unnecessary layering
  violation either way. A mutation reads current state through
  `dev_status_storage.py`'s primitives directly, inside its own lock, the
  same way today's `cmd_*` handlers already do.
- **Skip/partial-failure diagnostics are typed, not ad hoc.** Where a
  read-only service tolerates a per-item failure inline, the diagnostic is
  a typed field on the result — candidate 1's `StandupReport.skipped_sources:
  list[SkippedSource]`, candidate 2's `SessionQueryResult.skipped_records:
  list[SkippedRecord]` — not a loosely-shaped dict or a log line, and each
  distinct diagnostic type is named for what it actually describes rather
  than sharing one generic type across unrelated modules. Each candidate's
  CLI adapter documents whether a non-empty diagnostics list changes the
  command's exit code (today it does not, for standup and
  analyze-sessions; a new service must preserve that unless the CLI's
  documented behavior changes too).
- **Structured logging stays CLI-side.** A service function does not call
  `print()` or emit its own ad hoc log lines for anything short of a
  raised domain error — it returns a typed result (including any
  diagnostics, per the convention above) and lets the caller decide what
  to show. Verbosity, formatting, and redaction stay the CLI/hook
  adapter's job, using `cli_common.py`'s existing shared utilities (already
  classified "already suitable" in the inventory above) rather than a
  service layer inventing its own output or logging convention. This
  mirrors the "domain errors, not `sys.exit`" convention for the
  non-error case: incidental output is a presentation concern, not
  something a reusable capability API should own.
- **CLI adapters are the compatibility contract.** Every `cmd_*` handler's
  job becomes: parse argv into a typed request, call the service function,
  and render the typed result (or catch a domain error) into today's exact
  stdout/stderr shape and exit code — including the `DEVSTATUS_AGENT=1`
  structured confirmation line format, which downstream tooling already
  depends on. No candidate in this portfolio changes an existing CLI's
  observable output. `cmd_*` functions are not deprecated or removed by any
  candidate here — they remain the permanent argparse-dispatched entry
  points; only their bodies get thinner as logic moves into the service
  layer beneath them.

## Explicit non-recommendations

- Do not turn all top-level functions in `INTERFACES.md` into supported API.
- Do not let an agent import `dev_status_impl` and assemble state mutations
  around private helpers; that bypasses locking, revision guards, and journal
  behavior.
- Do not split isolation checks from command construction in `llm_backends`.
- Do not prioritize hook scripts or one-purpose dispatchers merely because
  they are scripts; their whole-operation contracts are an intentional safety
  boundary.
- Do not introduce a framework, package restructuring, or plugin registry
  before one or two extracted services establish a real common contract.
- Do not extract `to_tickets_runner` (candidate 13) or attempt any
  standalone `dev_status` mutation-command extraction ahead of the unified
  mutation service (candidate 12); a piecemeal per-command extraction is
  exactly the pattern this audit recommends against for the highest-risk
  module.
- Do not treat `gen_interfaces.py`, `gen_second_opinion.py`,
  `gen_shell_completion.py`, `gen_skills.py`, or `gen_skills_params.py` as
  runtime capability candidates; they are repository maintenance tooling
  audited separately from this portfolio if ever needed.
- Do not have one candidate's service read another candidate's on-disk
  state directly once that other candidate's formalized read API exists —
  depend on the function, not the file format. Candidate 8 depending on
  candidate 7's `all_sessions()` rather than re-reading grill's session
  JSON is the pattern; a later candidate should not reintroduce a direct
  cross-module file read as a shortcut.
- Do not design candidate 12's mutation service for multi-process or
  multi-machine concurrent writers beyond what `dev_status_storage.py`'s
  current single-filesystem `flock` already provides. Scaling the lock
  mechanism itself (a real transaction log, a lock service, etc.) is an
  explicit non-goal of this roadmap; it is out of scope unless a future
  audit demonstrates the current single-machine assumption has actually
  broken down.

## Source evidence

The assessment used direct source and test-layout inspection plus the generated
CLI inventory. In-scope module list was re-verified against
`find agent-scripts -maxdepth 1 -type f -name '*.py' -printf '%f\n' | sort`,
which returned exactly the 25 production runtime modules named in this
report's inventory, plus the five maintenance generators and the colocated
`test_*.py` files excluded per scope.

Notable scale/coupling signals, re-run at report time: `dev_status_impl.py`
is 5,526 lines with 155 module-level function/async-function definitions
(counted via `ast.parse` over top-level `FunctionDef`/`AsyncFunctionDef`
nodes); `analyze_sessions.py` is 1,248 lines; `grill.py` is 1,235 lines;
`llm_backends.py` is 1,198 lines; and `settings_seed_drift_check.py` is
1,575 lines. Additional line counts gathered for this portfolio's wave
assignments: `second_opinion.py` 1,034; `refresh_guidance.py` 1,393;
`harness_discovery_check.py` 751; `guard_rails.py` 876; `herdr_delegate.py`
856; `link_inspect.py` 925; `settings_seed.py` 628; `vitals_promotion.py`
490; `outlook_email.py` 469; `notify.py` 442; `standup.py` 309;
`outlook_calendar.py` 308; `link_drift_check.py` 335; `to_tickets_runner.py`
370; `dev_status_storage.py` 682; `cli_common.py` 289; `standup_adapters.py`
244; `dev_status_formatting.py` 223; `statusline.py` 206; `seed_hook_subset_guard.py`
199; `sessionstart_checks.py` 116; `bundle_drift_check.py` 113; `dev_status.py`
38. Line count is triage evidence, not by itself a refactor mandate.

Candidate interface shapes and effect boundaries were grounded by reading
each module's top-level `class`/`def`/`@dataclass` declarations directly
(e.g. `grill.py`'s `Session`/`Decision`/`Verdict` TypedDicts and
`session_lock`/`_new_session_lock` context managers; `dev_status_impl.py`'s
`cmd_show`/`cmd_ready`/`cmd_list`/`cmd_render` read-path handlers versus its
`cmd_add`/`cmd_update`/`cmd_start`/`cmd_done`/`cmd_approve`/`cmd_reject`/
`cmd_block`/`cmd_unblock`/`cmd_gate_set`/`cmd_gate_pass`/`cmd_rename`/
`cmd_remove`/`cmd_run`/`cmd_pending_add`/`cmd_pending_update` mutation-path
handlers; `llm_backends.py`'s `BackendError` exception hierarchy and
`run_with_fallback`/`eligible_backends` functions), rather than from this
report's own prior draft.

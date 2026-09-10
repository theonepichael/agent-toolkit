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
| `vitals_promotion.py` + `grill` data | Read/classify/promote/search workflow | Low-risk extraction | `matches_query`, `classify_decision`, `search_vitals`, and report generation already separate useful concerns. Add result objects and an explicit repository/path dependency. |
| `guard_rails.py` + `dev_status_impl` | Guard evaluator with git, process, filesystem, and backlog-claim checks | Needs design first | `evaluate()` is a promising decision boundary, but it currently reaches into implementation internals. Replace that dependency with a narrow read-only claim lookup protocol. |
| `herdr_delegate.py` + `dev_status` | Swarm launch/resume orchestration over backlog selection | Needs design first | Command construction and state parsing are separable, but readiness/serial safety are coupled to the broad backlog module. Depends on a backlog query facade. |
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

### Wave 1 — independent, read-only or already-isolated services

These candidates have no dependency on another candidate in this portfolio.
Each one both delivers standalone value and establishes a convention (typed
result objects, injected paths, domain errors) that later waves reuse.

#### 1. `standup.fetch_standup` — read-only source aggregation service

- **Why wave 1:** short, mostly read-only, already decomposed by data source
  (`standup_adapters.py`), with adapter protocols already in place.
- **Proposed interface:**
  ```python
  @dataclass(frozen=True)
  class StandupConfig: ...

  @dataclass(frozen=True)
  class StandupPaths: ...

  @dataclass
  class StandupReport:
      date: date
      since: date
      git_commits: list[dict[str, str]]
      # the remaining source records and skipped-source diagnostics

  def fetch_standup(
      config: StandupConfig,
      adapters: Mapping[str, object],
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
  exactly; must not gain a write effect.

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
  class SessionQueryResult:
      records: list[SessionRecord]
      cost_summary: dict[str, object]

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

- **Why wave 1:** this is the prerequisite several riskier candidates in
  later waves need (`guard_rails`, `herdr_delegate`, `to_tickets_runner`),
  so it should land early even though `dev_status_impl.py` as a whole is
  the highest-risk module. The facade itself only wraps existing read paths
  (`cmd_show`, `cmd_ready`, `cmd_list`, `cmd_render`) and adds no mutation.
- **Proposed interface:**
  ```python
  @dataclass(frozen=True)
  class BacklogQuery:
      status: str | None = None
      prefix: str | None = None

  def get_item(slug_or_id: str) -> BacklogItem | None: ...
  def ready_items(query: BacklogQuery | None = None) -> list[BacklogItem]: ...
  def item_status(slug_or_id: str) -> str: ...
  def claim_info(slug_or_id: str) -> dict[str, object] | None: ...
  ```
- **Effect boundary:** read-only; reads the same on-disk store
  `dev_status_storage.py` already reads for rendering, taking a shared read
  lock consistent with existing render-path behavior. No revision bump, no
  journal write, no claim mutation.
- **Error/result model:** `get_item` returns `None` for an unknown id rather
  than raising or exiting; slug/id resolution errors are a typed exception,
  not `sys.exit`.
- **CLI adapter story:** `cmd_show`, `cmd_ready`, and `cmd_list` become
  argv-parsing plus rendering wrappers around this facade; output is
  unchanged.
- **Tests:** exercise the facade against a temporary backlog store fixture;
  assert it never acquires a write lock and never bumps `rev`.
- **Dependencies:** none within this portfolio (it is itself the
  dependency for candidates 9, 10, and — once wave 3's mutation service
  lands — indirectly for candidate 8).
- **Risk controls:** this facade must not grow mutation methods; a
  mutation need routes to candidate 7 instead, once it exists.

### Wave 2 — self-contained but higher-design-cost, or dependent on wave 1

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

  def open_session(slug: str) -> Session: ...
  def record_decision(session: Session, patch: dict[str, object]) -> Decision: ...
  def revise_decision(session: Session, decision_id: str, patch: dict[str, object]) -> Decision: ...
  def record_verdict(session: Session, decision_id: str, verdict: Verdict) -> Decision: ...
  def frontier_of(session: Session) -> DecisionList: ...
  ```
- **Effect boundary:** file writes under `session_lock()`/`_new_session_lock()`,
  preserved exactly as today; no network or subprocess effects.
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
  needs a deliberate boundary.
- **Proposed interface:**
  ```python
  def search(vitals_dir: Path, keywords: list[str]) -> list[VitalsRecord]: ...
  def classify(data_dir: Path) -> Report: ...  # read-only, apply=False semantics
  def promote(data_dir: Path, *, apply: bool) -> Report: ...  # apply=True writes
  ```
- **Effect boundary:** `search`/`classify` are read-only; `promote` with
  `apply=True` is the only write path, matching today's `run(data_dir,
  apply)` split, made explicit at the function-name level instead of a
  boolean flag alone.
- **Error/result model:** typed `Report` (already a `TypedDict`) stays the
  result type; no new exceptions needed beyond existing I/O errors.
- **CLI adapter story:** `main()`/`print_report` stay as thin CLI wrappers
  calling `classify`/`promote` and rendering the `Report`.
- **Tests:** existing fixtures over `load_all_sessions`/`run()` extend
  directly to the split functions; assert `classify` never writes.
- **Dependencies:** none within this portfolio (reads grill session data
  directly, not through candidate 7 — grill's data format is stable
  TypedDict JSON, and coupling `vitals_promotion` to the not-yet-existing
  grill service API is not required for this candidate).
- **Risk controls:** `promote(apply=True)` must remain the only mutating
  entry point; do not let `classify`/`search` grow a hidden write path.

#### 9. `guard_rails` evaluator facade

- **Why wave 2:** `evaluate()` is a promising decision boundary, but it
  currently reaches into `dev_status_impl` internals for claim checks
  (`_busy_item`, `_claim_is_active`-adjacent logic via
  `load_in_progress()`); it depends on candidate 6 landing first to narrow
  that dependency to a stable read-only surface.
- **Proposed interface:**
  ```python
  class BacklogClaimLookup(Protocol):
      def ready_items(self) -> list[BacklogItem]: ...
      def claim_info(self, slug_or_id: str) -> dict[str, object] | None: ...

  def evaluate(req: Request, claims: BacklogClaimLookup) -> Verdict: ...
  ```
- **Effect boundary:** read-only; git/process/filesystem checks stay as
  today (`git()`, `repo_info()`, `_behind_origin_main()`), only the
  backlog-claim dependency changes shape.
- **Error/result model:** unchanged — `Verdict` stays the result type.
- **CLI adapter story:** `main()` constructs a concrete
  `BacklogClaimLookup` implementation (backed by candidate 6's facade) and
  passes it to `evaluate()`; hook output format is unchanged.
- **Tests:** existing guard tests can inject a fake `BacklogClaimLookup`
  instead of monkeypatching `dev_status_impl` internals directly — a
  strictly better test seam than today's.
- **Dependencies:** candidate 6 (`dev_status` read-only query facade).
- **Risk controls:** `evaluate()` must never gain a path back into
  `dev_status_impl`'s mutation surface; the protocol is read-only by
  construction.

#### 10. `herdr_delegate` / swarm queue facade

- **Why wave 2:** command construction (`build_tab_argv`,
  `build_agent_start_argv`, prompt builders) and state parsing
  (`parse_tab_list`, `discover_run_id`) are already separable pure
  functions, but readiness selection (`ready_slugs()`) and serial-prefix
  safety depend on the broad backlog module; this candidate depends on
  candidate 6.
- **Proposed interface:**
  ```python
  def select_ready(prefix: str | None, claims: BacklogClaimLookup) -> list[dict[str, object]]: ...
  def build_launch_plan(items: list[dict[str, object]], *, kind: str) -> list[list[str]]: ...
  ```
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
- **Dependencies:** candidate 6 (`dev_status` read-only query facade).
- **Risk controls:** keep the actual subprocess/tab-spawn boundary
  (`spawn_in_new_tab`, `herdr()`) outside the facade so a caller cannot
  "compute a plan" and accidentally also launch it.

#### 11. `refresh_guidance` audit service

- **Why wave 2:** discovery (`discover_agents_md`, `discover_scripts`,
  `discover_undocumented_dirs`) and finding construction
  (`check_path_claim`, `check_command_claim`, `run_check`) are read-only
  and already return structured `Finding`/`CheckResult` dataclasses, but
  the module also owns persisted review state (`load_state`/`save_state`)
  that must stay an explicit, separately-called effect — this needs more
  design care than a pure wave 1 candidate even though it has no
  dependency on another candidate here.
- **Proposed interface:** no new top-level function is needed —
  `run_check()` is already close to the target shape (`CheckResult`,
  `SectionStatus`, `Finding` are already typed dataclasses/results). The
  extraction work is narrower: stop `cmd_check`/`cmd_mark_reviewed` from
  being the only callers, and document `run_check()` and `load_state()` /
  `save_state()` as the two supported entry points, with state-marking
  kept as a distinct call from checking.
- **Effect boundary:** `run_check()` is read-only (source, docs, and git
  history reads); `save_state()` is the one write effect and must stay
  behind `cmd_mark_reviewed`'s explicit invocation, not folded into
  `run_check()`.
- **Error/result model:** already typed (`ConfigError`, `Finding`,
  `CheckResult`); no new exception types required.
- **CLI adapter story:** `cmd_check` calls `run_check()` and
  `render_report()`; `cmd_mark_reviewed` calls `save_state()` separately —
  both already close to this shape today.
- **Tests:** existing fixtures over doc-set configs extend directly;
  add a test asserting `run_check()` never calls `save_state()`.
- **Dependencies:** none within this portfolio.
- **Risk controls:** keep the read/write separation explicit so an
  importing caller cannot silently mark a section reviewed while only
  intending to check it.

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
  read-side conventions.
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

  def add_item(payload: dict[str, object]) -> MutationResult: ...
  def start_item(slug_or_id: str, *, if_rev: int | None = None) -> MutationResult: ...
  def approve_item(slug_or_id: str, *, if_rev: int | None = None) -> MutationResult: ...
  # ... one function per existing cmd_* mutation, same shape
  ```
- **Effect boundary:** every mutation acquires the same lock
  `dev_status_storage.py` already uses, bumps `rev`, and writes a journal
  entry — this service must not introduce a second locking or journaling
  path, only wrap the existing one.
- **Error/result model:** `RevisionConflictError` replaces today's
  `--if-rev` stale-rev refusal message; `GateUnmetError` replaces the
  `approve`/`done` gate-refusal exit; both carry the same information the
  CLI already prints, as structured fields instead of stderr text.
- **CLI adapter story:** every `cmd_*` mutation handler becomes argv
  parsing, a call to the matching service function, and rendering
  `MutationResult` (or catching a `BacklogMutationError` subclass) into
  today's exact `[<cmd>] slug=... status=... rev=... detail="..."` line
  and exit code — this is the compatibility contract the
  `DEVSTATUS_AGENT=1` structured-output format already documents.
- **Tests:** this is the one candidate that needs new baseline tests
  before extraction begins — one test per existing `cmd_*` mutation
  asserting the current structured-output line and exit code, run against
  a temporary backlog store, so the extraction can be proven behavior-
  preserving function by function.
- **Dependencies:** candidate 6 (`dev_status` read-only query facade), for
  a consistent read/write API shape; benefits from candidates 7 and 9
  having already proven the "typed errors instead of `die()`/`sys.exit`"
  pattern at smaller scale first.
- **Risk controls:** locks, revision guards, journals, gates, and run
  evidence must be preserved byte-for-byte in behavior; this service must
  never be built by lifting `cmd_*` bodies wholesale — each function needs
  the parsing/exit concerns stripped out, not just renamed.

#### 13. `to_tickets_runner` service

- **Why wave 3:** batch validation (`_validate_batch_schema`), dependency
  ordering (`compute_order`), and resume-state handling
  (`load_state`/`write_state`) are good pure seams already, but ticket
  creation goes through `dev_status`'s module alias and command-shaped
  calls; this candidate is explicitly gated on candidate 12 existing so
  that creation happens through the stabilized mutation service rather
  than by importing `dev_status_impl` internals or shelling out to the
  CLI.
- **Proposed interface:**
  ```python
  def validate_batch(path: Path) -> list[Ticket]: ...  # wraps load_batch
  def plan_order(tickets: list[Ticket], index: BacklogIndex) -> list[str]: ...  # wraps compute_order
  def run_batch(batch_path: Path, mutations: BacklogMutationService) -> list[str]: ...
  ```
- **Effect boundary:** `validate_batch`/`plan_order` are pure/read-only;
  `run_batch` is the only mutating path, and it delegates every actual
  creation/link call to candidate 12's service rather than constructing
  its own backlog writes.
- **Error/result model:** keep `BatchError`/`SlugCollisionError` for
  schema/collision failures; creation failures surface candidate 12's
  `BacklogMutationError` subclasses unchanged.
- **CLI adapter story:** `cmd_run` stays a thin wrapper around
  `run_batch()`, preserving today's resume-state file behavior
  (`_state_path`, `_batch_hash`, `load_state`/`write_state`/`delete_state`).
- **Tests:** existing `test_to_tickets_runner.py` batch fixtures extend
  directly once `run_batch` takes an injected mutation service (real or
  fake) instead of importing `dev_status` as a module alias.
- **Dependencies:** candidate 12 (`dev_status` mutation service).
- **Risk controls:** do not let `run_batch` grow a second, independent
  path to creating or linking backlog items; every mutation must route
  through candidate 12.

## Recommended execution order

1. **Wave 1, any order:** candidates 1–6 (`standup.fetch_standup`,
   `analyze_sessions.query_sessions`, `second_opinion.review_plan`,
   `settings_seed`/drift-check consolidation, `link_inspect`/
   `link_drift_check` result API, `dev_status` read-only query facade).
   None depend on each other; candidate 6 should not be deferred to last
   in this wave even though `dev_status_impl.py` is the largest module,
   because waves 2 and 3 need it.
2. **Wave 2, after wave 1's conventions exist:** candidates 7 and 8
   (`grill` session service, `vitals_promotion` service) can start as soon
   as wave 1 begins, since neither depends on it — but should still land
   after at least one wave 1 candidate ships, so the typed-error/CLI-
   adapter convention is proven once at smaller scale first. Candidates 9
   and 10 (`guard_rails` evaluator facade, `herdr_delegate`/swarm queue
   facade) require candidate 6 specifically and must wait for it.
   Candidate 11 (`refresh_guidance` audit service) has no hard dependency
   and can proceed whenever design time is available.
3. **Wave 3, last:** candidate 12 (`dev_status` mutation service) should
   not start until wave 1 and at least the `grill`/`guard_rails` wave 2
   work has validated the typed-error and compatibility-adapter
   conventions at smaller scale, since it is the highest-risk extraction
   in the whole portfolio and touches every mutating backlog command.
   Candidate 13 (`to_tickets_runner` service) is explicitly gated on
   candidate 12 and must come after it.

This order maximizes early value (five independent read-only/low-risk
services) while ensuring the two structurally required dependencies —
candidate 6 before 9/10, and candidate 12 before 13 — are respected, and
ensures the riskiest system (candidate 12) is attempted only after its
conventions have been proven elsewhere first.

## Cross-cutting API conventions

These conventions apply to every candidate above and should be established
by the first wave 1 extraction that ships, not invented independently per
candidate:

- **Typed inputs and results.** Prefer a frozen `dataclass` for request/query
  objects and either a `dataclass` or the existing `TypedDict` records
  (`Session`, `Decision`, `BacklogItem`, `VitalsRecord`, `SessionRecord`) for
  results. Do not accept or return raw `argparse.Namespace` objects from a
  service function.
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
  these, it does not replace them with a new locking scheme.
- **CLI adapters are the compatibility contract.** Every `cmd_*` handler's
  job becomes: parse argv into a typed request, call the service function,
  and render the typed result (or catch a domain error) into today's exact
  stdout/stderr shape and exit code — including the `DEVSTATUS_AGENT=1`
  structured confirmation line format, which downstream tooling already
  depends on. No candidate in this portfolio changes an existing CLI's
  observable output.

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

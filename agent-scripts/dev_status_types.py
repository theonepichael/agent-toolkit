"""Backlog data model — the on-disk shapes shared across the dev_status stack.

Single source of truth for ``Gate``, ``RunRecord``, ``BacklogItem``,
``PendingItem``, and ``BacklogIndex``. Every other dev_status module imports
these rather than redefining them — two independent copies of the same
schema can only drift, never help.
"""

from __future__ import annotations

from typing import NotRequired, TypedDict


class Gate(TypedDict):
    """A judgment-step verification checkpoint on a backlog item.

    Set once via ``gate-set`` when an item's plan/spec is classified
    (mechanical steps need no gate; a plan with judgment steps gets
    ``required: True`` and ``criteria`` drawn from those steps' acceptance
    criteria). ``passed_at`` stays ``None`` until ``gate-pass`` records one,
    and every ``gate-set`` call resets ``passed_at`` to ``None``
    — re-classifying (e.g. after a plan revision) invalidates any prior
    pass, mirroring grill.py's ``revise`` resetting a decision's verdict.

    ``gate-pass`` only records a pass when every criterion is covered by
    recorded run evidence (a command executed via the ``run`` subcommand)
    or an explicit per-criterion manual note — a bare claim is refused.
    ``set_at`` (full aware-UTC ISO datetime) stamps the current gate
    generation: run evidence with ``started_at`` older than ``set_at`` no
    longer counts, so re-classifying automatically orphans older evidence
    without deleting it. Gates classified before ``set_at`` existed carry
    no key, which gate-pass treats as "no lower bound". ``passed_via`` is
    ``run-evidence``/``manual``/``mixed``; ``coverage`` maps each criterion
    number to ``{"kind": "run", "run_id": ...}`` or
    ``{"kind": "manual", "note": ...}`` so ``show`` displays what
    satisfied what.
    """

    # `passed: bool | None` was dropped — redundant with `passed_at`
    # (non-None means passed); don't reintroduce it.
    required: bool
    criteria: list[str]
    passed_at: str | None
    set_at: NotRequired[str]
    passed_via: NotRequired[str]
    coverage: NotRequired[dict[str, dict[str, str]]]


class RunRecord(TypedDict):
    """One recorded command execution — a row of the ``runs.jsonl`` sidecar.

    Written by the ``run`` subcommand, which executes the command itself
    (never accepting a self-reported result) and appends one JSON line per
    run: ``{run_id, item, command, exit_code, timed_out, started_at,
    duration_s, cwd}``. ``exit_code`` is ``None`` exactly when
    ``timed_out`` is true. ``gate-pass`` cites rows from this file as
    per-criterion evidence; ``item`` holds the owning backlog slug.
    """

    run_id: str
    item: str
    command: str
    exit_code: int | None
    timed_out: bool
    started_at: str
    duration_s: float
    cwd: str


class BacklogItem(TypedDict):
    """A single backlog item as stored in ``items.json`` (schema v2).

    ``priority`` and ``completed_at`` are absent unless explicitly set —
    absence of ``priority`` is equivalent to ``"normal"`` (see
    :data:`_PRIORITY_RANK`), and ``completed_at`` only exists while
    ``status`` is ``"done"`` (stamped and cleared by
    :func:`_apply_status_transition`). ``gate`` is likewise absent on any
    item created before its introduction or never classified — absence is
    equivalent to an inert gate (see :func:`_gate_blocks`), the same
    absence-means-default convention ``priority`` already uses.
    """

    id: str
    created: str
    updated: str
    status: str
    summary: str
    category: str
    blocked_by: list[str]
    related_files: list[dict[str, object]]
    context: str
    next_steps: str | list[str]
    priority: NotRequired[str]
    completed_at: NotRequired[str]
    review_feedback: NotRequired[str]
    review_content_hash: NotRequired[str]
    gate: NotRequired[Gate]


class PendingItem(TypedDict):
    """A single waiting-on-someone-else item as stored in ``pending_items.json``.

    ``resolved_at`` is absent unless ``status`` is ``"resolved"`` (stamped
    and cleared by :func:`_apply_status_transition`).
    """

    id: str
    created: str
    updated: str
    status: str
    description: str
    kind: str
    source_ref: dict[str, object]
    context: str
    next_steps: list[str]
    blocking: list[str]
    outcome: str | None
    resolved_at: NotRequired[str]


type BacklogIndex = dict[str, BacklogItem]

#!/usr/bin/env python3
"""guard_rails.py's claim check (R4): a write pointing at an in-progress
item — its exact related_files path, or a worktree whose branch is the item
slug — is denied unless the calling session holds an active claim on it.

Session identity is monkeypatched at guard_rails._session_identity in the
end-to-end tests (the real ancestor walk gets dedicated subprocess tests);
every store is a throwaway injected as ``LocalClaimLookup(store_path=...)``
— no monkeypatching of the store read, which now lives in
backlog_claim_lookup.py.
"""

import json
import os
import subprocess
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "agent-scripts"))

import guard_rails  # noqa: E402
from backlog_claim_lookup import ClaimInfo, LocalClaimLookup  # noqa: E402
import pytest  # noqa: E402

pytestmark = pytest.mark.allow_real_subprocess  # git in tmp_path; /proc reads


def _git(*args: str, cwd: Path) -> None:
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True)


def _init_repo(path: Path, branch: str = "develop") -> Path:
    path.mkdir(parents=True)
    _git("init", "-q", "-b", branch, cwd=path)
    _git("config", "user.email", "t@example.invalid", cwd=path)
    _git("config", "user.name", "t", cwd=path)
    (path / "tracked.txt").write_text("x\n")
    _git("add", "tracked.txt", cwd=path)
    _git("commit", "-qm", "init", cwd=path)
    return path


def _worktree(repo: Path, path: Path, slug: str) -> Path:
    _git("worktree", "add", "-q", str(path), "-b", slug, cwd=repo)
    return path


def _item(
    slug: str,
    related: list[str],
    claim: dict | None,
    status: str = "in-progress",
) -> dict:
    return {
        "id": slug,
        "status": status,
        "related_files": [{"path": p} for p in related],
        "claimed_by": claim,
    }


def _claim(
    owner_pid: int,
    machine: str | None = None,
    minutes_ago: int = 0,
    harness: str = "pi",
) -> dict:
    import dev_status_impl

    now = datetime.now(UTC) - timedelta(minutes=minutes_ago)
    stamp = now.strftime("%Y-%m-%dT%H:%M:%SZ")
    return {
        "harness": harness,
        "machine_id": machine if machine is not None else dev_status_impl.machine_id(),
        "pid": owner_pid,  # realistic: the short-lived child is the same live process
        "owner_pid": owner_pid,
        "claimed_at": stamp,
        "last_active": stamp,
    }


_SLEEPERS: list[subprocess.Popen] = []


def _spawn_sleeper() -> int:
    """A live process whose PID is guaranteed not to be in this test's
    ancestor chain, for foreign-claim and dead-claim scenarios."""
    proc = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(120)"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    _SLEEPERS.append(proc)
    return proc.pid


def _kill(pid: int) -> None:
    for proc in list(_SLEEPERS):
        if proc.pid == pid:
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=5)
            _SLEEPERS.remove(proc)


def teardown_module(module) -> None:
    for proc in _SLEEPERS:
        proc.kill()
        proc.wait(timeout=5)
    _SLEEPERS.clear()


# ── unit: claim-activity semantics ─────────────────────────────────────────


class TestClaimIsActive:
    def test_same_machine_live_owner_in_chain_is_active(self) -> None:
        import dev_status_impl

        claim = _claim(owner_pid=os.getpid(), minutes_ago=600)
        assert guard_rails._claim_is_active(ClaimInfo.from_dict(
            claim),
            current_machine=dev_status_impl.machine_id(),
            owner_pid=os.getpid(),
            chain=[os.getpid()],
        )

    def test_same_machine_live_owner_despite_old_last_active(self) -> None:
        """Liveness, not TTL, governs same-machine claims: a session working
        longer than DEVSTATUS_CLAIM_TTL_SECONDS must not be locked out."""
        import dev_status_impl

        claim = _claim(owner_pid=os.getpid(), minutes_ago=10_000)
        assert guard_rails._claim_is_active(ClaimInfo.from_dict(
            claim),
            current_machine=dev_status_impl.machine_id(),
            owner_pid=os.getpid(),
            chain=[os.getpid()],
        )

    def test_same_machine_dead_owner_is_not_active(self) -> None:
        import dev_status_impl

        sleeper = _spawn_sleeper()
        _kill(sleeper)
        claim = _claim(owner_pid=sleeper, minutes_ago=0)
        assert not guard_rails._claim_is_active(ClaimInfo.from_dict(
            claim),
            current_machine=dev_status_impl.machine_id(),
            owner_pid=os.getpid(),
            chain=[os.getpid()],
        )

    def test_owner_recorded_but_dead_means_not_active_even_with_live_pid(
        self,
    ) -> None:
        """An owner PID recorded in the claim decides; no fallback to a
        possibly-reused child pid while an owner exists."""
        import dev_status_impl

        sleeper = _spawn_sleeper()
        _kill(sleeper)
        claim = _claim(owner_pid=sleeper, minutes_ago=0)
        assert not guard_rails._claim_is_active(ClaimInfo.from_dict(
            claim),
            current_machine=dev_status_impl.machine_id(),
            owner_pid=os.getpid(),
            chain=[os.getpid()],
        )

    def test_same_machine_live_owner_not_in_chain_is_foreign(self) -> None:
        import dev_status_impl

        foreign = _spawn_sleeper()
        try:
            claim = _claim(owner_pid=foreign, minutes_ago=0)
            assert not guard_rails._claim_is_active(ClaimInfo.from_dict(
                claim),
                current_machine=dev_status_impl.machine_id(),
                owner_pid=os.getpid(),
                chain=[os.getpid()],
            )
        finally:
            _kill(foreign)

    def test_foreign_machine_is_never_this_session(self) -> None:
        """A claim from another machine can never be this session's claim,
        whatever its TTL says — the TTL only governs whether dev_status's
        start may take it over, not whether the guard grants a write."""
        import dev_status_impl

        within = _claim(owner_pid=os.getpid(), machine="ffffffff", minutes_ago=5)
        past = _claim(owner_pid=os.getpid(), machine="ffffffff", minutes_ago=600)
        machine = dev_status_impl.machine_id()
        for claim in (within, past):
            assert not guard_rails._claim_is_active(ClaimInfo.from_dict(
                claim), current_machine=machine, owner_pid=os.getpid(), chain=[]
            )

    def test_missing_or_malformed_claim_is_not_active(self) -> None:
        import dev_status_impl

        machine = dev_status_impl.machine_id()
        assert not guard_rails._claim_is_active(ClaimInfo.from_dict(
            None), current_machine=machine, owner_pid=os.getpid(), chain=[]
        )
        assert not guard_rails._claim_is_active(ClaimInfo.from_dict(
            {"harness": "pi"}),  # no machine_id, no pids
            current_machine=machine,
            owner_pid=os.getpid(),
            chain=[],
        )


# ── end-to-end through evaluate(), store via monkeypatched path ────────────


@pytest.fixture()
def repo_pair(tmp_path: Path) -> dict:
    repo = _init_repo(tmp_path / "proj")
    wt = _worktree(repo, tmp_path / "proj-demo-slug", "demo-slug")
    return {"repo": repo, "wt": wt}


@pytest.fixture()
def store(tmp_path: Path) -> Path:
    path = tmp_path / "store" / "items.json"
    return path


def _set_store(store: Path, items: list[dict]) -> None:
    store.parent.mkdir(parents=True, exist_ok=True)
    store.write_text(json.dumps({"items": items}))


_MACHINE = ""
_OWN_OWNER = -1


@pytest.fixture(autouse=True)
def _pin_identity_constants() -> None:
    global _MACHINE, _OWN_OWNER
    import dev_status_impl

    _MACHINE = dev_status_impl.machine_id()
    _OWN_OWNER = os.getpid()


@pytest.fixture()
def as_foreign_session(monkeypatch: pytest.MonkeyPatch) -> int:
    """Make the guard see this test process as a different session: a live
    foreign owner PID nowhere in the walked chain."""
    foreign = _spawn_sleeper()

    def identity() -> tuple[str, int, list[int]]:
        return (_MACHINE, _OWN_OWNER, [_OWN_OWNER])

    monkeypatch.setattr(guard_rails, "_session_identity", identity)
    try:
        yield foreign
    finally:
        _kill(foreign)


@pytest.fixture()
def as_own_session(monkeypatch: pytest.MonkeyPatch) -> None:
    """The guard's own-session identity, pinned to this test process."""

    def identity() -> tuple[str, int, list[int]]:
        return (_MACHINE, _OWN_OWNER, [_OWN_OWNER])

    monkeypatch.setattr(guard_rails, "_session_identity", identity)


class TestClaimEnforcementEndToEnd:
    def test_no_in_progress_item_allows(self, repo_pair, store) -> None:
        repo = repo_pair["repo"]
        _set_store(
            store,
            [_item("demo-slug", [str(repo / "tracked.txt")], None, status="open")],
        )
        assert _write(repo / "tracked.txt", store).decision == "allow"

    def test_exact_related_path_unclaimed_denied(self, repo_pair, store) -> None:
        repo = repo_pair["repo"]
        _set_store(store, [_item("demo-slug", [str(repo / "tracked.txt")], None)])
        verdict = _write(repo / "tracked.txt", store)
        assert verdict.decision == "deny"
        assert "demo-slug" in verdict.reason
        assert "dev_status.py start" in verdict.reason

    def test_exact_related_path_dead_claim_denied(
        self, repo_pair, store, as_own_session
    ) -> None:
        sleeper = _spawn_sleeper()
        _kill(sleeper)
        repo = repo_pair["repo"]
        _set_store(
            store, [_item("demo-slug", [str(repo / "tracked.txt")], _claim(sleeper))]
        )
        verdict = _write(repo / "tracked.txt", store)
        assert verdict.decision == "deny"
        assert "dev_status.py start" in verdict.reason
        assert "--force" not in verdict.reason

    def test_exact_related_path_foreign_live_claim_denied(
        self, repo_pair, store, as_foreign_session
    ) -> None:
        repo = repo_pair["repo"]
        _set_store(
            store,
            [
                _item(
                    "demo-slug",
                    [str(repo / "tracked.txt")],
                    _claim(as_foreign_session),
                )
            ],
        )
        verdict = _write(repo / "tracked.txt", store)
        assert verdict.decision == "deny"
        assert "demo-slug" in verdict.reason
        assert "--force" not in verdict.reason

    def test_exact_related_path_foreign_machine_claim_within_ttl_denied(
        self, repo_pair, store, as_own_session
    ) -> None:
        """A cross-machine claim is never this session's, even inside the
        TTL, and its holder may well be alive elsewhere — so the reason
        points at a human decision, not at --force."""
        repo = repo_pair["repo"]
        _set_store(
            store,
            [
                _item(
                    "demo-slug",
                    [str(repo / "tracked.txt")],
                    _claim(owner_pid=os.getpid(), machine="ffffffff"),
                )
            ],
        )
        verdict = _write(repo / "tracked.txt", store)
        assert verdict.decision == "deny"
        assert "--force" not in verdict.reason

    def test_unrelated_path_allowed_even_when_item_unclaimed(
        self, repo_pair, store
    ) -> None:
        repo = repo_pair["repo"]
        _set_store(store, [_item("demo-slug", [str(repo / "tracked.txt")], None)])
        assert _write(repo / "unrelated.txt", store).decision == "allow"

    def test_related_files_directory_prefix_must_not_widen(
        self, repo_pair, store, as_own_session
    ) -> None:
        """An unclaimed item pointing at one file must not block writes to
        its siblings, even in the same directory."""
        repo = repo_pair["repo"]
        _set_store(store, [_item("demo-slug", [str(repo / "tracked.txt")], None)])
        (repo / "sibling.txt").write_text("y\n")
        assert _write(repo / "sibling.txt", store).decision == "allow"

    def test_own_live_claim_exact_path_allowed(
        self, repo_pair, store, as_own_session
    ) -> None:
        repo = repo_pair["repo"]
        _set_store(
            store,
            [_item("demo-slug", [str(repo / "tracked.txt")], _claim(_OWN_OWNER))],
        )
        assert _write(repo / "tracked.txt", store).decision == "allow"

    def test_own_live_claim_in_slug_worktree_allowed(
        self, repo_pair, store, as_own_session
    ) -> None:
        """The slug-named worktree rule: the whole worktree belongs to the
        item, so any file inside it is allowed for the claim holder."""
        _set_store(store, [_item("demo-slug", [], _claim(_OWN_OWNER))])
        assert _write(repo_pair["wt"] / "new-file.txt", store).decision == "allow"

    def test_foreign_session_in_slug_worktree_denied(
        self, repo_pair, store, as_foreign_session
    ) -> None:
        _set_store(store, [_item("demo-slug", [], _claim(as_foreign_session))])
        assert _write(repo_pair["wt"] / "new-file.txt", store).decision == "deny"

    def test_unclaimed_item_in_slug_worktree_denied(self, repo_pair, store) -> None:
        _set_store(store, [_item("demo-slug", [], None)])
        assert _write(repo_pair["wt"] / "new-file.txt", store).decision == "deny"

    def test_own_claim_elsewhere_does_not_rescue_another_items_worktree(
        self, repo_pair, store, as_own_session
    ) -> None:
        """Holding a claim on item A must not let you write into the
        slug-named worktree of a different in-progress item."""
        _set_store(
            store,
            [
                _item("my-item", [], _claim(_OWN_OWNER)),
                _item("demo-slug", [], None),
            ],
        )
        assert _write(repo_pair["wt"] / "new-file.txt", store).decision == "deny"

    def test_r2_protected_main_checkout_still_denies_regardless_of_claim(
        self, tmp_path, store, as_own_session
    ) -> None:
        repo = _init_repo(tmp_path / "proj-main", branch="main")
        _set_store(
            store,
            [_item("demo-slug", [str(repo / "tracked.txt")], _claim(_OWN_OWNER))],
        )
        verdict = _write(repo / "tracked.txt", store)
        assert verdict.decision == "deny"
        assert "worktree" in verdict.reason  # R2's message, not the claim's

    def test_corrupt_store_fails_open(self, repo_pair, store) -> None:
        store.parent.mkdir(parents=True, exist_ok=True)
        store.write_text("{not json")
        assert _write(repo_pair["repo"] / "tracked.txt", store).decision == "allow"


def _write(path: Path, store: Path) -> guard_rails.Verdict:
    path.parent.mkdir(parents=True, exist_ok=True)
    return guard_rails.evaluate(
        guard_rails.Request(
            tool="write", cwd=str(path.parent), path=str(path), command=""
        ),
        LocalClaimLookup(store_path=store),
    )


# ── the real ancestor walk, end to end through a spawned guard ─────────────


class TestRealSessionIdentity:
    def _spawn_guard(self, args: list[str], store: Path) -> str:
        script = Path(guard_rails.__file__).resolve()
        env = dict(os.environ)
        env["GUARD_RAILS_STORE"] = str(store)
        out = subprocess.run(
            [sys.executable, str(script), *args],
            capture_output=True,
            text=True,
            env=env,
            check=True,
        )
        return out.stdout

    def test_spawned_guard_resolves_this_test_as_owner(self, repo_pair, store) -> None:
        """No monkeypatching: claim this test process (a live owner), then
        spawn guard_rails.py the way a hook would. The guard's real ancestor
        walk must land on the same owner and allow the write."""
        repo = repo_pair["repo"]
        _set_store(
            store,
            [
                _item(
                    "demo-slug",
                    [str(repo / "tracked.txt")],
                    _claim(os.getpid()),
                )
            ],
        )
        out = self._spawn_guard(
            [
                "--tool",
                "write",
                "--cwd",
                str(repo),
                "--path",
                str(repo / "tracked.txt"),
            ],
            store,
        )
        assert json.loads(out)["decision"] == "allow"

    def test_spawned_guard_denies_a_foreign_claim(self, repo_pair, store) -> None:
        foreign = _spawn_sleeper()
        try:
            repo = repo_pair["repo"]
            _set_store(
                store,
                [_item("demo-slug", [str(repo / "tracked.txt")], _claim(foreign))],
            )
            out = self._spawn_guard(
                [
                    "--tool",
                    "write",
                    "--cwd",
                    str(repo),
                    "--path",
                    str(repo / "tracked.txt"),
                ],
                store,
            )
            assert json.loads(out)["decision"] == "deny"
        finally:
            _kill(foreign)

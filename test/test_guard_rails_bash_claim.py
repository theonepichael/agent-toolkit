#!/usr/bin/env python3
"""guard_rails.py's best-effort Bash claim check: a Bash call whose session
cwd, or a literal absolute path in the command, resolves into a linked
worktree attributed to an in-progress item that a *different live session*
actively claims is denied -- while the same call from the claiming session,
or into an unclaimed path, is allowed.

Scope is deliberately narrower than the write-family claim check: only the
"actively claimed by another live session" case is denied; the "in-progress
but unclaimed" case is allowed, so ``dev_status.py start <slug>`` (the
dead-claim recovery) stays runnable from inside the worktree.

Session identity is monkeypatched at guard_rails._session_identity; every
store is a throwaway injected as ``LocalClaimLookup(store_path=...)``.
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
from backlog_claim_lookup import LocalClaimLookup  # noqa: E402
import pytest  # noqa: E402

pytestmark = pytest.mark.allow_real_subprocess  # git in tmp_path; /proc reads


def _git(*args: str, cwd: Path) -> None:
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True)


def _init_repo(path: Path, branch: str = "develop") -> Path:
    path.mkdir(parents=True)
    _git("init", "-q", "-b", branch, cwd=path)
    _git("config", "user.email", "t@example.invalid", cwd=path)
    _git("config", "user.name", "t", cwd=path)
    _git("config", "gc.auto", "0", cwd=path)
    _git("config", "maintenance.auto", "false", cwd=path)
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
        "pid": owner_pid,
        "owner_pid": owner_pid,
        "claimed_at": stamp,
        "last_active": stamp,
    }


_SLEEPERS: list[subprocess.Popen] = []


def _spawn_sleeper() -> int:
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


# ── end-to-end through evaluate(), store via injected path ─────────────────


@pytest.fixture()
def repo_pair(tmp_path: Path) -> dict:
    repo = _init_repo(tmp_path / "proj")
    wt = _worktree(repo, tmp_path / "proj-demo-slug", "demo-slug")
    return {"repo": repo, "wt": wt}


@pytest.fixture()
def store(tmp_path: Path) -> Path:
    return tmp_path / "store" / "items.json"


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


def _bash(cwd: str, command: str, store: Path) -> guard_rails.Verdict:
    return guard_rails.evaluate(
        guard_rails.Request(tool="bash", cwd=cwd, path="", command=command),
        LocalClaimLookup(store_path=store),
    )


class TestBashClaimEnforcement:
    def _foreign_claimed(self, store: Path, foreign: int) -> None:
        _set_store(store, [_item("demo-slug", [], _claim(foreign))])

    @pytest.mark.regression(
        "bash-claim-denies-foreign-worktree-cwd",
        "AssertionError: assert 'allow' == 'deny'",
    )
    def test_cwd_in_foreign_claimed_worktree_denied(
        self, repo_pair, store, as_foreign_session
    ) -> None:
        self._foreign_claimed(store, as_foreign_session)
        verdict = _bash(str(repo_pair["wt"]), "ls", store)
        assert verdict.decision == "deny"
        assert "demo-slug" in verdict.reason
        assert "another session" in verdict.reason

    @pytest.mark.regression(
        "bash-claim-denies-foreign-worktree-absolute-path",
        "AssertionError: assert 'allow' == 'deny'",
    )
    def test_absolute_path_into_foreign_claimed_worktree_denied(
        self, repo_pair, store, as_foreign_session
    ) -> None:
        self._foreign_claimed(store, as_foreign_session)
        target = repo_pair["wt"] / "new-file.txt"
        verdict = _bash(
            str(repo_pair["repo"]), f"touch {target}", store
        )
        assert verdict.decision == "deny"
        assert "demo-slug" in verdict.reason

    @pytest.mark.regression(
        "bash-claim-denies-heredoc-absolute-path",
        "AssertionError: assert 'allow' == 'deny'",
    )
    def test_heredoc_absolute_path_into_foreign_worktree_denied(
        self, repo_pair, store, as_foreign_session
    ) -> None:
        """The incident shape: a python3 heredoc whose body opens a file by
        absolute path in another session's claimed worktree."""
        self._foreign_claimed(store, as_foreign_session)
        target = repo_pair["wt"] / "evidence.json"
        command = (
            "python3 <<'EOF'\n"
            f"import json; json.dump({{'x': 1}}, open('{target}', 'w'))\n"
            "EOF"
        )
        verdict = _bash(str(repo_pair["repo"]), command, store)
        assert verdict.decision == "deny"
        assert "demo-slug" in verdict.reason

    def test_own_claim_in_slug_worktree_allowed(
        self, repo_pair, store, as_own_session
    ) -> None:
        _set_store(store, [_item("demo-slug", [], _claim(_OWN_OWNER))])
        assert _bash(str(repo_pair["wt"]), "ls", store).decision == "allow"

    def test_unclaimed_worktree_allowed(self, repo_pair, store) -> None:
        """The recovery route: an in-progress but unclaimed worktree must not
        deny Bash (otherwise `dev_status.py start <slug>` could not run from
        inside it)."""
        _set_store(store, [_item("demo-slug", [], None)])
        assert _bash(str(repo_pair["wt"]), "dev_status.py start demo-slug", store).decision == "allow"

    def test_dead_claim_worktree_allowed(self, repo_pair, store, as_own_session) -> None:
        sleeper = _spawn_sleeper()
        _kill(sleeper)
        _set_store(store, [_item("demo-slug", [], _claim(sleeper))])
        assert _bash(str(repo_pair["wt"]), "ls", store).decision == "allow"

    def test_absolute_path_into_unclaimed_path_allowed(
        self, repo_pair, store, as_foreign_session
    ) -> None:
        """A foreign claim elsewhere must not deny an unrelated path."""
        self._foreign_claimed(store, as_foreign_session)
        target = repo_pair["repo"] / "unrelated.txt"
        assert _bash(str(repo_pair["repo"]), f"touch {target}", store).decision == "allow"

    def test_no_in_progress_item_allows(self, repo_pair, store) -> None:
        _set_store(store, [_item("demo-slug", [], None, status="open")])
        assert _bash(str(repo_pair["wt"]), "ls", store).decision == "allow"

    def test_corrupt_store_fails_open(self, repo_pair, store) -> None:
        store.parent.mkdir(parents=True, exist_ok=True)
        store.write_text("{not json")
        assert _bash(str(repo_pair["wt"]), "ls", store).decision == "allow"

    @pytest.mark.regression(
        "bash-claim-denies-worktree-root-with-trailing-comma",
        "AssertionError: assert 'allow' == 'deny'",
    )
    def test_worktree_root_with_trailing_comma_denied(
        self, repo_pair, store, as_foreign_session
    ) -> None:
        """A Python heredoc passing the worktree *root* as an argument (so
        shlex glues the argument comma onto the path token) must still
        attribute the worktree -- not walk past it."""
        self._foreign_claimed(store, as_foreign_session)
        command = (
            "python3 <<'EOF'\n"
            f"import shutil; shutil.rmtree('{repo_pair['wt']}', ignore_errors=True)\n"
            "EOF"
        )
        verdict = _bash(str(repo_pair["repo"]), command, store)
        assert verdict.decision == "deny"
        assert "demo-slug" in verdict.reason

    @pytest.mark.regression(
        "bash-claim-broken-id-allows-unrelated-bash",
        "AssertionError: assert 'deny' == 'allow'",
    )
    def test_broken_machine_id_does_not_deny_unrelated_bash(
        self, repo_pair, store, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Session identity is resolved only once a candidate path actually
        points at an in-progress worktree; a broken machine id must not deny
        an unrelated Bash call (which would also block `machine-id --repair`)."""
        import dev_status_storage

        def broken() -> tuple[str, int, list[int]]:
            raise dev_status_storage.MachineIdError(
                "invalid machine id in /x/_machine_id", Path("/x/_machine_id")
            )

        monkeypatch.setattr(guard_rails, "_session_identity", broken)
        _set_store(store, [_item("demo-slug", [], None)])
        assert _bash("/tmp", "ls", store).decision == "allow"

    @pytest.mark.regression(
        "bash-claim-broken-id-allows-unclaimed-worktree",
        "AssertionError: assert 'deny' == 'allow'",
    )
    def test_broken_machine_id_does_not_deny_unclaimed_worktree(
        self, repo_pair, store, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """An unclaimed worktree has no claim to be foreign-live against, so
        a broken machine id must not deny Bash there -- that would block the
        `dev_status.py machine-id --repair` command the denial itself names."""
        import dev_status_storage

        def broken() -> tuple[str, int, list[int]]:
            raise dev_status_storage.MachineIdError(
                "invalid machine id in /x/_machine_id", Path("/x/_machine_id")
            )

        monkeypatch.setattr(guard_rails, "_session_identity", broken)
        _set_store(store, [_item("demo-slug", [], None)])
        assert _bash(str(repo_pair["wt"]), "ls", store).decision == "allow"

    @pytest.mark.regression(
        "bash-claim-broken-id-allows-claimed-worktree",
        "AssertionError: assert 'deny' == 'allow'",
    )
    def test_broken_machine_id_does_not_deny_claimed_worktree(
        self, repo_pair, store, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A corrupt machine id must not deny Bash in a worktree that still
        has a recorded claim -- else `dev_status.py machine-id --repair` (run
        from inside that worktree) is itself denied before it can fix the id."""
        import dev_status_storage

        def broken() -> tuple[str, int, list[int]]:
            raise dev_status_storage.MachineIdError(
                "invalid machine id in /x/_machine_id", Path("/x/_machine_id")
            )

        monkeypatch.setattr(guard_rails, "_session_identity", broken)
        sleeper = _spawn_sleeper()
        _kill(sleeper)  # recorded claim, holder now dead
        _set_store(store, [_item("demo-slug", [], _claim(sleeper))])
        assert _bash(str(repo_pair["wt"]), "ls", store).decision == "allow"


# ── the real spawned guard, end to end (fast path must fall through) ───────


class TestSpawnedGuardBash:
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

    def test_spawned_guard_denies_bash_into_foreign_worktree(
        self, repo_pair, store
    ) -> None:
        """No monkeypatching: a real spawned guard's fast path must fall
        through to the claim check and deny a Bash call into a worktree
        claimed by a live foreign session."""
        foreign = _spawn_sleeper()
        try:
            _set_store(store, [_item("demo-slug", [], _claim(foreign))])
            out = self._spawn_guard(
                ["--tool", "bash", "--cwd", str(repo_pair["wt"]), "--command", "ls"],
                store,
            )
            assert json.loads(out)["decision"] == "deny"
        finally:
            _kill(foreign)

    @pytest.mark.regression(
        "neutral-layout-deny-keeps-audit-target",
        "AssertionError: assert None == ''",
    )
    def test_neutral_layout_deny_audit_keeps_harness_and_target(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A neutral (pi/opencode) write-family call denied on a layout error
        must record its audit line with harness "" and the request's tool and
        target; the main() refactor must not regress it to None/empty."""
        captured: dict[str, object] = {}

        def fake_audit(harness, req, verdict) -> None:
            captured["harness"] = harness
            captured["req"] = req

        monkeypatch.setattr(guard_rails, "_layout_error_reason", lambda: "broken layout")
        monkeypatch.setattr(guard_rails, "_audit_verdict", fake_audit)
        guard_rails.main(
            ["--tool", "write", "--cwd", "/tmp", "--path", "/tmp/file.txt"]
        )
        assert captured["harness"] == ""
        assert captured["req"].tool == "write"
        assert captured["req"].path == "/tmp/file.txt"

    @pytest.mark.regression(
        "bash-claim-broken-layout-allows-bash",
        "AssertionError: assert 'deny' == 'allow'",
    )
    def test_broken_layout_does_not_deny_bash(self, tmp_path: Path) -> None:
        """A broken store layout must not deny an ordinary Bash call: the
        up-front layout check skips Bash and evaluate_bash_claim fails open
        on LayoutError, so the shell that repairs the layout stays usable."""
        script = Path(guard_rails.__file__).resolve()
        home = tmp_path / "broken-home"
        pointer = home / ".agent-toolkit" / "data" / "toolkit_state.json"
        pointer.parent.mkdir(parents=True, exist_ok=True)
        pointer.write_text('{"schema": 999, "layout": "nonsense"}\n')
        env = {
            k: v
            for k, v in os.environ.items()
            if not k.startswith(("GUARD_RAILS", "AGENT_TOOLKIT", "GIT_"))
        }
        env.update(HOME=str(home), GIT_CONFIG_NOSYSTEM="1")
        out = subprocess.run(
            [
                sys.executable,
                str(script),
                "--tool",
                "bash",
                "--cwd",
                "/tmp",
                "--command",
                "ls",
            ],
            capture_output=True,
            text=True,
            env=env,
            check=True,
        )
        assert json.loads(out.stdout)["decision"] == "allow"

#!/usr/bin/env python3
"""Tests for settings_seed_drift_check.py. Run with: python3 test_settings_seed_drift_check.py

Covers the redesign (additive-only ``fix``, denylist cosmetics, loud-fail
on JSONC parse errors, refusal while a Claude Code session is active).
"""

import io
import json
import shutil
import subprocess
import sys
import tempfile
import unittest
from collections.abc import Callable
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).parent))
import settings_seed_drift_check as ssdc

REPO_ROOT = Path(__file__).resolve().parent.parent.parent

# ── committed seed key coverage (class guard) — expectation sets ────────────
# Deliberately hardcoded, not derived from ssdc.SETTINGS_COSMETIC_KEYS /
# ssdc.OPENCODE_COSMETIC_KEYS: deriving from those would make the coverage
# test below tautological (the test's expectation and the checker's actual
# behavior would come from the identical source, so a silent
# misclassification in production could never fail the test). This is a
# second, independent source of truth, hand-maintained alongside the
# production constants.
EXPECTED_SETTINGS_COSMETIC = frozenset(
    {
        "tui",
        "voice",
        "theme",
        "statusLine",
        "model",
        "agentPushNotifEnabled",
    }
)
EXPECTED_SETTINGS_NONCOSMETIC = frozenset(
    {
        "permissions",
        "hooks",
        "skipDangerousModePermissionPrompt",
    }
)
EXPECTED_OPENCODE_COSMETIC = frozenset({"$schema", "agent"})
EXPECTED_OPENCODE_NONCOSMETIC = frozenset({"permission"})

_DRIFT_SENTINEL = "__drift_sentinel__"


def _drift_perturb(value: object) -> object:
    """Return a value guaranteed to differ from ``value`` under ``!=``,
    while staying JSON-serializable, for any JSON scalar/structure type."""
    if isinstance(value, dict):
        return {**value, _DRIFT_SENTINEL: True}
    if isinstance(value, list):
        return [*value, _DRIFT_SENTINEL]
    if isinstance(value, bool):
        return not value
    if isinstance(value, str):
        return value + _DRIFT_SENTINEL
    if isinstance(value, (int, float)):
        return value + 1
    return _DRIFT_SENTINEL  # None or any other JSON scalar type


def _assert_no_key_name_collisions(keys: frozenset[str]) -> None:
    """Fail loudly if any key name is a substring of another — the
    behavioral check in ``_assert_seed_key_coverage`` uses plain substring
    matching against ``cmd_check``'s output, which would become ambiguous
    if that ever holds. Not expected to fire against today's committed
    keys; it's a tripwire for a future key addition."""
    for a in keys:
        for b in keys:
            if a != b and a in b:
                raise AssertionError(
                    f"key {a!r} is a substring of key {b!r} — substring-based "
                    "drift assertions in _assert_seed_key_coverage would be "
                    "ambiguous for this pair; rename or use a more precise "
                    "output match"
                )


class SettingsSeedDriftCheckTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.tmpdir = tempfile.mkdtemp()
        self.home = Path(self.tmpdir) / "home"
        self.home.mkdir()
        self.dotfiles = Path(self.tmpdir) / "dotfiles"
        self.claude_dir = self.dotfiles / "claude"
        self.claude_dir.mkdir(parents=True)
        self.opencode_dir = self.dotfiles / "opencode"
        self.opencode_dir.mkdir(parents=True)
        self.state_dir = self.home / ".local" / "state" / "dotfiles"
        self.state_dir.mkdir(parents=True)

        self._patches = [
            patch.object(ssdc, "HOME", self.home),
            patch.object(ssdc, "DOTFILES", self.dotfiles),
            patch.object(ssdc, "PROFILE_MARKER", self.state_dir / "profile"),
            # SessionStart refusal gate must not fire from any test that
            # isn't explicitly exercising it.
            patch.object(ssdc, "_sessions_active", lambda: 0),
            # This machine is itself a live instance of the WSL VS Code
            # symlink bug (see install.py's _vscode_wsl_user_dir), so the
            # real function resolves here — default it off so unrelated
            # tests don't pick up real machine state; vscode-specific
            # tests override it explicitly.
            patch.object(ssdc, "_vscode_wsl_user_dir", lambda: None),
        ]
        for p in self._patches:
            p.start()

    def tearDown(self) -> None:
        for p in self._patches:
            p.stop()
        shutil.rmtree(self.tmpdir)

    # ── setup helpers ──────────────────────────────────────────────────

    def write_settings_seed(
        self, body: dict[str, object], *, name: str = "settings.json"
    ) -> None:
        (self.claude_dir / name).write_text(json.dumps(body, indent=2) + "\n")

    def write_settings_seed_raw(
        self, text: str, *, name: str = "settings.json"
    ) -> None:
        (self.claude_dir / name).write_text(text)

    def write_live_settings(self, body: dict[str, object]) -> None:
        live_dir = self.home / ".claude"
        live_dir.mkdir(parents=True, exist_ok=True)
        (live_dir / "settings.json").write_text(json.dumps(body, indent=2) + "\n")

    def write_live_settings_raw(self, text: str) -> None:
        live_dir = self.home / ".claude"
        live_dir.mkdir(parents=True, exist_ok=True)
        (live_dir / "settings.json").write_text(text)

    def write_opencode_seed(self, body: dict[str, object]) -> None:
        (self.opencode_dir / "opencode.jsonc").write_text(
            json.dumps(body, indent=2) + "\n"
        )

    def write_live_opencode(self, body: dict[str, object]) -> None:
        oc_dir = self.home / ".config" / "opencode"
        oc_dir.mkdir(parents=True, exist_ok=True)
        (oc_dir / "opencode.jsonc").write_text(json.dumps(body, indent=2) + "\n")

    def write_live_opencode_raw(self, text: str) -> None:
        oc_dir = self.home / ".config" / "opencode"
        oc_dir.mkdir(parents=True, exist_ok=True)
        (oc_dir / "opencode.jsonc").write_text(text)

    def mark_work(self) -> None:
        (self.state_dir / "profile").write_text("work\n")

    def vscode_user_dir(self) -> Path:
        d = self.home / "winappdata"
        d.mkdir(parents=True, exist_ok=True)
        return d

    def write_vscode_seed(self, name: str, text: str) -> None:
        d = self.dotfiles / "vscode"
        d.mkdir(parents=True, exist_ok=True)
        (d / name).write_text(text)

    def write_live_vscode(self, name: str, text: str) -> None:
        (self.vscode_user_dir() / name).write_text(text)

    def run_check(self) -> tuple[str, int]:
        out = io.StringIO()
        err = io.StringIO()
        with patch("sys.stdout", out), patch("sys.stderr", err):
            code = ssdc.cmd_check()
        return out.getvalue().strip(), code

    def run_fix(self) -> tuple[str, int]:
        out = io.StringIO()
        err = io.StringIO()
        with patch("sys.stdout", out), patch("sys.stderr", err):
            code = ssdc.cmd_fix()
        return out.getvalue().strip(), code

    def run_sync(self, root: Path | None = None) -> tuple[str, int]:
        out = io.StringIO()
        err = io.StringIO()
        target_root = root if root is not None else self.dotfiles
        with patch("sys.stdout", out), patch("sys.stderr", err):
            code = ssdc.cmd_sync_to_seed(target_root)
        return out.getvalue().strip(), code

    def run_push_vscode(
        self, root: Path | None = None, *, yes: bool = False
    ) -> tuple[str, int]:
        out = io.StringIO()
        err = io.StringIO()
        target_root = root if root is not None else self.dotfiles
        with patch("sys.stdout", out), patch("sys.stderr", err):
            code = ssdc.cmd_push_vscode(target_root, yes=yes)
        return out.getvalue().strip(), code

    def load_live_settings(self) -> dict[str, object]:
        return json.loads((self.home / ".claude" / "settings.json").read_text())

    def load_live_opencode(self) -> dict[str, object]:
        return json.loads(
            (self.home / ".config" / "opencode" / "opencode.jsonc").read_text()
        )

    def load_settings_seed(self, name: str = "settings.json") -> dict[str, object]:
        return json.loads((self.claude_dir / name).read_text())

    def load_opencode_seed(self) -> dict[str, object]:
        return json.loads((self.opencode_dir / "opencode.jsonc").read_text())

    # ── _non_cosmetic_drift unit (denylist, not allowlist) ──────────────

    def test_non_cosmetic_drift_reports_unknown_future_security_key(self) -> None:
        # skipDangerousModePermissionPrompt is shipped today; the old
        # allowlist silently missed it. The denylist must surface it.
        seed = {"skipDangerousModePermissionPrompt": True, "theme": "light"}
        live = {"skipDangerousModePermissionPrompt": False, "theme": "dark"}
        result = ssdc._non_cosmetic_drift(seed, live, ssdc.SETTINGS_COSMETIC_KEYS)
        self.assertIn("skipDangerousModePermissionPrompt", result)
        self.assertNotIn("theme", result)

    def test_non_cosmetic_drift_silent_on_cosmetic_only(self) -> None:
        seed = {"theme": "light", "model": "x", "voiceEnabled": False}
        live = {"theme": "dark", "model": "y", "voiceEnabled": True}
        result = ssdc._non_cosmetic_drift(seed, live, ssdc.SETTINGS_COSMETIC_KEYS)
        self.assertEqual(result, [])

    def test_non_cosmetic_drift_reports_permissions_and_hooks(self) -> None:
        seed = {"permissions": {"allow": []}, "hooks": {}}
        live = {"permissions": {"allow": ["Bash(x)"]}, "hooks": {"X": []}}
        result = ssdc._non_cosmetic_drift(seed, live, ssdc.SETTINGS_COSMETIC_KEYS)
        self.assertEqual(result, ["hooks", "permissions"])

    # ── profile resolution (unchanged behavior) ─────────────────────────

    def test_resolve_profile_defaults_personal_no_marker(self) -> None:
        self.assertEqual(ssdc.resolve_profile(), "personal")

    def test_resolve_profile_work_when_marker_says_work(self) -> None:
        self.mark_work()
        self.assertEqual(ssdc.resolve_profile(), "work")

    def test_settings_seed_path_personal(self) -> None:
        self.assertEqual(ssdc.settings_seed_path(), self.claude_dir / "settings.json")

    def test_settings_seed_path_work(self) -> None:
        self.mark_work()
        self.assertEqual(
            ssdc.settings_seed_path(), self.claude_dir / "settings.work.json"
        )

    def test_opencode_seed_path_personal(self) -> None:
        self.assertEqual(
            ssdc.opencode_seed_path(), self.opencode_dir / "opencode.jsonc"
        )

    def test_opencode_seed_path_work_is_none(self) -> None:
        self.mark_work()
        self.assertIsNone(ssdc.opencode_seed_path())

    # ── cmd_check end-to-end (denylist reporting) ───────────────────────

    def test_check_silent_when_no_live_settings(self) -> None:
        self.write_settings_seed({"permissions": {"allow": []}})
        out, code = self.run_check()
        self.assertEqual(out, "")
        self.assertEqual(code, 0)

    def test_check_silent_when_critical_keys_match(self) -> None:
        self.write_settings_seed(
            {"permissions": {"allow": ["Bash(x)"]}, "hooks": {}, "theme": "light"}
        )
        self.write_live_settings(
            {"permissions": {"allow": ["Bash(x)"]}, "hooks": {}, "theme": "dark"}
        )
        out, code = self.run_check()
        self.assertEqual(out, "")
        self.assertEqual(code, 0)

    def test_check_reports_settings_critical_drift(self) -> None:
        self.write_settings_seed({"permissions": {"allow": []}, "hooks": {}})
        self.write_live_settings(
            {"permissions": {"allow": ["Bash(BAD)"]}, "hooks": {}, "theme": "light"}
        )
        out, _ = self.run_check()
        self.assertIn("settings.json drifted", out)
        self.assertIn("permissions", out)
        self.assertIn("fix`", out)
        self.assertNotIn("theme", out)  # cosmetic — not reported

    def test_check_reports_hooks_drift(self) -> None:
        self.write_settings_seed(
            {"permissions": {"allow": []}, "hooks": {"SessionStart": []}}
        )
        self.write_live_settings({"permissions": {"allow": []}, "hooks": {}})
        out, _ = self.run_check()
        self.assertIn("hooks", out)

    def test_check_reports_unknown_future_security_key_drift(self) -> None:
        # A key that isn't permissions/hooks and isn't in the cosmetic
        # denylist must be reported by default (the plan's whole point).
        self.write_settings_seed({"permissions": {"allow": []}, "autoCompact": True})
        self.write_live_settings({"permissions": {"allow": []}, "autoCompact": False})
        out, _ = self.run_check()
        self.assertIn("autoCompact", out)

    def test_check_opencode_silent_when_no_live_opencode(self) -> None:
        self.write_settings_seed({"permissions": {"allow": []}})
        self.write_opencode_seed({"permission": {"bash": {"git status*": "allow"}}})
        out, code = self.run_check()
        self.assertEqual(out, "")
        self.assertEqual(code, 0)

    def test_check_opencode_reports_permission_drift(self) -> None:
        self.write_settings_seed({"permissions": {"allow": []}})
        self.write_opencode_seed({"permission": {"bash": {"git status*": "allow"}}})
        self.write_live_opencode({"permission": {"bash": {"rm -f *": "allow"}}})
        out, _ = self.run_check()
        self.assertIn("permission", out)

    def test_check_opencode_reports_security_bypass(self) -> None:
        self.write_settings_seed({"permissions": {"allow": []}})
        self.write_opencode_seed({"permission": {"bash": {"git status*": "allow"}}})
        self.write_live_opencode(
            {"permission": {"bash": {"git status*": "allow", "xargs *": "allow"}}}
        )
        out, _ = self.run_check()
        self.assertIn("SECURITY:", out)
        self.assertIn("xargs *", out)
        self.assertIn("fix`", out)

    def test_check_work_profile_skips_opencode(self) -> None:
        self.mark_work()
        self.write_settings_seed(
            {"permissions": {"allow": []}, "hooks": {}}, name="settings.work.json"
        )
        self.write_live_opencode({"permission": {"bash": {"rm -f *": "allow"}}})
        out, _ = self.run_check()
        self.assertEqual(out, "")

    # ── loud-fail on JSONC / parse failure ──────────────────────────────

    def test_check_loudfails_on_unparseable_opencode_jsonc(self) -> None:
        # A // comment makes json.loads choke — the script must say so,
        # not silently report "no drift" (the silent-no-op bug the
        # redesign fixes).
        self.write_settings_seed({"permissions": {"allow": []}})
        self.write_opencode_seed({"permission": {"bash": {"git status*": "allow"}}})
        self.write_live_opencode_raw(
            '{\n  // a comment json.loads cannot parse\n  "permission": {}\n}\n'
        )
        out, code = self.run_check()
        self.assertNotEqual(code, 0)
        self.assertIn("cannot parse", out)
        self.assertIn("opencode", out)

    def test_check_loudfails_on_unparseable_settings_json(self) -> None:
        self.write_settings_seed({"permissions": {"allow": []}})
        self.write_live_settings_raw("{ not valid json ")
        out, code = self.run_check()
        self.assertNotEqual(code, 0)
        self.assertIn("cannot parse", out)

    def test_check_stays_silent_when_live_file_missing(self) -> None:
        # Missing-file is NOT a parse failure — it's "nothing to compare",
        # and must remain silent (no false alarm on a fresh machine).
        self.write_settings_seed({"permissions": {"allow": []}})
        out, code = self.run_check()
        self.assertEqual(out, "")
        self.assertEqual(code, 0)

    # ── cmd_fix: additive union for permissions.allow ──────────────────

    def test_fix_permissions_allow_is_additive_union_preserving_live(self) -> None:
        seed = {"permissions": {"allow": ["Bash(SEED_A)", "Bash(SEED_B)"]}}
        self.write_settings_seed(seed)
        # live already has SEED_A plus a live-only approval Live_USER_X
        live = {
            "permissions": {
                "allow": ["Bash(SEED_A)", "Bash(LIVE_USER_X)"],
                "deny": [],
                "ask": [],
            },
            "theme": "dark",  # cosmetic — must survive
        }
        self.write_live_settings(live)
        self.run_fix()
        repaired = self.load_live_settings()
        allow = repaired["permissions"]["allow"]  # type: ignore[index]
        # Live approval preserved, both seed entries present
        self.assertIn("Bash(LIVE_USER_X)", allow)
        self.assertIn("Bash(SEED_A)", allow)
        self.assertIn("Bash(SEED_B)", allow)
        # cosmetic preserved
        self.assertEqual(repaired["theme"], "dark")

    def test_fix_permissions_deny_restored_when_live_is_weakened_subset(self) -> None:
        # Live deny=[] but seed deny=["Bash(rm *)"] → security regression,
        # wholesale-overwrite from seed.
        seed = {"permissions": {"allow": [], "deny": ["Bash(rm *)"], "ask": []}}
        self.write_settings_seed(seed)
        live = {"permissions": {"allow": [], "deny": [], "ask": []}}
        self.write_live_settings(live)
        self.run_fix()
        repaired = self.load_live_settings()
        self.assertEqual(repaired["permissions"]["deny"], ["Bash(rm *)"])  # type: ignore[index]

    def test_fix_permissions_deny_preserved_when_live_added_entries(self) -> None:
        # Live deny has entries seed lacks → not a weakened subset → preserve.
        seed = {"permissions": {"allow": [], "deny": ["Bash(rm *)"], "ask": []}}
        self.write_settings_seed(seed)
        live = {
            "permissions": {
                "allow": [],
                "deny": ["Bash(rm *)", "Bash(LIVE_EXTRA)"],
                "ask": [],
            }
        }
        self.write_live_settings(live)
        self.run_fix()
        repaired = self.load_live_settings()
        self.assertEqual(
            repaired["permissions"]["deny"],  # type: ignore[index]
            ["Bash(rm *)", "Bash(LIVE_EXTRA)"],
        )

    def test_fix_permissions_ask_flag_and_skip_when_differing(self) -> None:
        # Live ask and seed ask differ but neither is a weakened subset of
        # the other → flag-and-skip (preserve live, report).
        seed = {"permissions": {"allow": [], "ask": ["Bash(git commit)"]}}
        self.write_settings_seed(seed)
        live = {"permissions": {"allow": [], "ask": ["Bash(git push)"]}}
        self.write_live_settings(live)
        out, _ = self.run_fix()
        repaired = self.load_live_settings()
        self.assertEqual(repaired["permissions"]["ask"], ["Bash(git push)"])  # type: ignore[index]
        self.assertIn("permissions.ask", out)
        self.assertIn("leaving live", out)

    def test_fix_hooks_wholesale_overwritten_from_seed(self) -> None:
        # hooks are not live-modified by approvals → any divergence is
        # genuine drift → wholesale-overwrite from seed.
        seed = {
            "permissions": {"allow": []},
            "hooks": {
                "SessionStart": [{"hooks": [{"type": "command", "command": "X"}]}]
            },
        }
        self.write_settings_seed(seed)
        live = {"permissions": {"allow": []}, "hooks": {"StaleDrift": []}}
        self.write_live_settings(live)
        self.run_fix()
        repaired = self.load_live_settings()
        self.assertEqual(repaired["hooks"], seed["hooks"])

    def test_fix_hooks_dropped_when_seed_has_none(self) -> None:
        # seed has no hooks key; live has hooks → drop live's hooks
        # (wholesale: live becomes seed's hooks, which is empty).
        self.write_settings_seed({"permissions": {"allow": []}})
        self.write_live_settings(
            {"permissions": {"allow": []}, "hooks": {"StaleDrift": []}}
        )
        self.run_fix()
        repaired = self.load_live_settings()
        self.assertNotIn("hooks", repaired)

    def test_fix_creates_backup(self) -> None:
        self.write_settings_seed({"permissions": {"allow": ["Bash(SEED)"]}})
        self.write_live_settings({"permissions": {"allow": ["Bash(BAD)"]}})
        self.run_fix()
        backups = list((self.home / ".claude").glob("settings.json.bak.*"))
        self.assertEqual(len(backups), 1)
        backup_content = json.loads(backups[0].read_text())
        self.assertEqual(backup_content["permissions"], {"allow": ["Bash(BAD)"]})

    def test_fix_atomic_write_preserves_other_live_keys(self) -> None:
        # Cosmetic + unknown future keys are preserved by fix.
        self.write_settings_seed(
            {"permissions": {"allow": ["Bash(SEED)"]}, "autoCompact": True}
        )
        self.write_live_settings(
            {
                "permissions": {"allow": ["Bash(BAD)"]},
                "autoCompact": False,
                "theme": "dark",
            }
        )
        self.run_fix()
        repaired = self.load_live_settings()
        # autoCompact and theme untouched (autoCompact isn't permissions/hooks)
        self.assertFalse(repaired["autoCompact"])
        self.assertEqual(repaired["theme"], "dark")
        # permissions.allow additive: BAD retained, SEED appended
        self.assertIn("Bash(BAD)", repaired["permissions"]["allow"])  # type: ignore[index]
        self.assertIn("Bash(SEED)", repaired["permissions"]["allow"])  # type: ignore[index]

    def test_fix_silent_when_no_critical_drift(self) -> None:
        self.write_settings_seed(
            {"permissions": {"allow": []}, "hooks": {}, "theme": "light"}
        )
        self.write_live_settings(
            {"permissions": {"allow": []}, "hooks": {}, "theme": "dark"}
        )
        out, _ = self.run_fix()
        self.assertIn("no auto-repairable drift", out)
        live = self.load_live_settings()
        self.assertEqual(live["theme"], "dark")

    # ── cmd_fix opencode (additive + bypass removal) ────────────────────

    def test_fix_opencode_bash_additive_preserves_live_patterns(self) -> None:
        seed_oc = {
            "permission": {"bash": {"git status*": "allow", "npm run*": "allow"}}
        }
        self.write_settings_seed({"permissions": {"allow": []}})
        self.write_opencode_seed(seed_oc)
        # live has git status* (matches seed) plus a live-only approval
        self.write_live_opencode(
            {"permission": {"bash": {"git status*": "allow", "copilot *": "allow"}}}
        )
        self.run_fix()
        repaired = self.load_live_opencode()
        bash = repaired["permission"]["bash"]  # type: ignore[index]
        self.assertIn("git status*", bash)
        self.assertIn("npm run*", bash)  # appended from seed
        self.assertIn("copilot *", bash)  # live-only preserved

    def test_fix_opencode_strips_bypass_patterns_even_under_additive(self) -> None:
        # xargs * present live but not seed → stripped even though additive
        # policy would otherwise preserve live-only entries.
        self.write_settings_seed({"permissions": {"allow": []}})
        self.write_opencode_seed({"permission": {"bash": {"git status*": "allow"}}})
        self.write_live_opencode(
            {"permission": {"bash": {"git status*": "allow", "xargs *": "allow"}}}
        )
        self.run_fix()
        repaired = self.load_live_opencode()
        bash = repaired["permission"]["bash"]  # type: ignore[index]
        self.assertNotIn("xargs *", bash)
        self.assertIn("git status*", bash)

    def test_fix_opencode_bash_flag_and_skip_differing_verdicts(self) -> None:
        # shared pattern with differing verdict → preserve live, report.
        self.write_settings_seed({"permissions": {"allow": []}})
        self.write_opencode_seed({"permission": {"bash": {"rm -f *": "ask"}}})
        self.write_live_opencode({"permission": {"bash": {"rm -f *": "allow"}}})
        out, _ = self.run_fix()
        repaired = self.load_live_opencode()
        # live verdict preserved
        self.assertEqual(repaired["permission"]["bash"]["rm -f *"], "allow")  # type: ignore[index]
        self.assertIn("verdict differs", out)

    def test_fix_opencode_external_directory_additive_union(self) -> None:
        seed_oc = {
            "permission": {
                "bash": {},
                "external_directory": {"~/Workspace/**": "allow"},
            }
        }
        self.write_settings_seed({"permissions": {"allow": []}})
        self.write_opencode_seed(seed_oc)
        self.write_live_opencode(
            {"permission": {"bash": {}, "external_directory": {"/tmp/**": "allow"}}}
        )
        self.run_fix()
        repaired = self.load_live_opencode()
        ed = repaired["permission"]["external_directory"]  # type: ignore[index]
        self.assertIn("/tmp/**", ed)  # live-only preserved
        self.assertIn("~/Workspace/**", ed)  # seed appended

    # ── cmd_fix refusal while a Claude Code session is active ───────────

    def test_fix_refuses_when_sessions_active(self) -> None:
        self.write_settings_seed({"permissions": {"allow": ["Bash(SEED)"]}})
        self.write_live_settings({"permissions": {"allow": ["Bash(BAD)"]}})
        with patch.object(ssdc, "_sessions_active", lambda: 1):
            out, code = self.run_fix()
        self.assertNotEqual(code, 0)
        self.assertIn("refusing to fix", out)
        # Live file unchanged
        live = self.load_live_settings()
        self.assertEqual(live["permissions"], {"allow": ["Bash(BAD)"]})

    def test_fix_runs_when_no_sessions_active(self) -> None:
        self.write_settings_seed({"permissions": {"allow": ["Bash(SEED)"]}})
        self.write_live_settings({"permissions": {"allow": ["Bash(BAD)"]}})
        with patch.object(ssdc, "_sessions_active", lambda: 0):
            _, code = self.run_fix()
        self.assertEqual(code, 0)
        live = self.load_live_settings()
        self.assertIn("Bash(SEED)", live["permissions"]["allow"])  # type: ignore[index]

    def test_fix_is_no_op_when_live_settings_missing(self) -> None:
        self.write_settings_seed({"permissions": {"allow": []}})
        out, code = self.run_fix()
        self.assertEqual(code, 0)
        self.assertNotIn("repaired", out)

    def test_fix_loudfails_on_unparseable_settings(self) -> None:
        self.write_settings_seed({"permissions": {"allow": []}})
        self.write_live_settings_raw("{ broken json")
        out, code = self.run_fix()
        self.assertNotEqual(code, 0)
        self.assertIn("cannot fix", out)

    # ── cmd_sync_to_seed: reverse direction (live -> seed) ──────────────

    def test_sync_hooks_wholesale_mirrored_live_to_seed(self) -> None:
        self.write_settings_seed({"permissions": {"allow": []}})
        live_hooks = {
            "SessionStart": [{"hooks": [{"type": "command", "command": "X"}]}]
        }
        self.write_live_settings({"permissions": {"allow": []}, "hooks": live_hooks})
        out, code = self.run_sync()
        self.assertEqual(code, 0)
        seed = self.load_settings_seed()
        self.assertEqual(seed["hooks"], live_hooks)
        self.assertIn("mirrored from live", out)
        backups = list(self.claude_dir.glob("settings.json.bak.*"))
        self.assertEqual(len(backups), 1)

    def test_sync_hooks_dropped_when_live_has_none(self) -> None:
        self.write_settings_seed(
            {"permissions": {"allow": []}, "hooks": {"StaleDrift": []}}
        )
        self.write_live_settings({"permissions": {"allow": []}})
        out, code = self.run_sync()
        self.assertEqual(code, 0)
        seed = self.load_settings_seed()
        self.assertNotIn("hooks", seed)
        self.assertIn("dropped from seed", out)

    def test_sync_is_no_op_when_hooks_already_match(self) -> None:
        shared_hooks = {"SessionStart": []}
        self.write_settings_seed({"permissions": {"allow": []}, "hooks": shared_hooks})
        self.write_live_settings({"permissions": {"allow": []}, "hooks": shared_hooks})
        out, code = self.run_sync()
        self.assertEqual(out, "")
        self.assertEqual(code, 0)
        backups = list(self.claude_dir.glob("settings.json.bak.*"))
        self.assertEqual(len(backups), 0)

    def test_sync_settings_permissions_never_written_only_reported(self) -> None:
        self.write_settings_seed(
            {"permissions": {"allow": ["Bash(SEED)"]}, "hooks": {}}
        )
        self.write_live_settings(
            {
                "permissions": {"allow": ["Bash(SEED)", "Bash(LIVE_ONLY)"]},
                "hooks": {},
            }
        )
        out, code = self.run_sync()
        self.assertEqual(code, 0)
        seed = self.load_settings_seed()
        self.assertEqual(seed["permissions"]["allow"], ["Bash(SEED)"])  # type: ignore[index]
        self.assertIn("permissions live-only", out)
        self.assertIn("Bash(LIVE_ONLY)", out)
        backups = list(self.claude_dir.glob("settings.json.bak.*"))
        self.assertEqual(len(backups), 0)

    def test_sync_opencode_permission_bash_missing_key_reported(self) -> None:
        seed_oc = {"permission": {"bash": {"git status*": "allow"}}}
        self.write_settings_seed({"permissions": {"allow": []}})
        self.write_opencode_seed(seed_oc)
        self.write_live_opencode(
            {"permission": {"bash": {"git status*": "allow", "npm run*": "allow"}}}
        )
        out, code = self.run_sync()
        self.assertEqual(code, 0)
        self.assertEqual(self.load_opencode_seed(), seed_oc)
        self.assertIn("live-only", out)
        self.assertIn("npm run*", out)

    def test_sync_opencode_permission_bash_value_mismatch_reported(self) -> None:
        seed_oc = {"permission": {"bash": {"rm -f *": "ask"}}}
        self.write_settings_seed({"permissions": {"allow": []}})
        self.write_opencode_seed(seed_oc)
        self.write_live_opencode({"permission": {"bash": {"rm -f *": "allow"}}})
        out, code = self.run_sync()
        self.assertEqual(code, 0)
        self.assertEqual(self.load_opencode_seed(), seed_oc)
        self.assertIn("differs", out)
        self.assertIn("live=", out)
        self.assertIn("seed=", out)

    def test_sync_opencode_no_hooks_equivalent_written(self) -> None:
        seed_oc = {"$schema": "x"}
        self.write_settings_seed({"permissions": {"allow": []}})
        self.write_opencode_seed(seed_oc)
        self.write_live_opencode({"$schema": "y", "agent": "z"})
        _out, code = self.run_sync()
        self.assertEqual(code, 0)
        self.assertEqual(self.load_opencode_seed(), seed_oc)

    def test_sync_creates_backup_before_write(self) -> None:
        self.write_settings_seed({"permissions": {"allow": []}, "hooks": {}})
        self.write_live_settings({"permissions": {"allow": []}, "hooks": {"X": []}})
        self.run_sync()
        backups = list(self.claude_dir.glob("settings.json.bak.*"))
        self.assertEqual(len(backups), 1)
        backup_content = json.loads(backups[0].read_text())
        self.assertEqual(backup_content["hooks"], {})

    def test_sync_silent_when_nothing_to_sync(self) -> None:
        self.write_settings_seed({"permissions": {"allow": ["Bash(X)"]}, "hooks": {}})
        self.write_live_settings({"permissions": {"allow": ["Bash(X)"]}, "hooks": {}})
        out, code = self.run_sync()
        self.assertEqual(out, "")
        self.assertEqual(code, 0)

    def test_sync_loudfails_on_unparseable_live_settings(self) -> None:
        self.write_settings_seed({"permissions": {"allow": []}})
        self.write_live_settings_raw("{ broken json")
        out, code = self.run_sync()
        self.assertNotEqual(code, 0)
        self.assertIn("cannot sync", out)

    def test_sync_loudfails_on_unparseable_seed(self) -> None:
        self.write_settings_seed_raw("{ broken json")
        self.write_live_settings({"permissions": {"allow": []}})
        out, code = self.run_sync()
        self.assertNotEqual(code, 0)
        self.assertIn("cannot sync", out)
        self.assertIn("seed", out)

    def test_sync_missing_seed_file_is_silent_noop(self) -> None:
        self.write_live_settings({"permissions": {"allow": []}})
        out, code = self.run_sync()
        self.assertEqual(out, "")
        self.assertEqual(code, 0)
        self.assertFalse((self.claude_dir / "settings.json").exists())

    def test_sync_missing_live_file_is_silent_noop(self) -> None:
        seed = {"permissions": {"allow": []}, "hooks": {}}
        self.write_settings_seed(seed)
        out, code = self.run_sync()
        self.assertEqual(out, "")
        self.assertEqual(code, 0)
        self.assertEqual(self.load_settings_seed(), seed)

    def test_sync_respects_dotfiles_root_override(self) -> None:
        custom_root = Path(self.tmpdir) / "other-dotfiles"
        (custom_root / "claude").mkdir(parents=True)
        (custom_root / "claude" / "settings.json").write_text(
            json.dumps({"permissions": {"allow": []}})
        )
        self.write_live_settings({"permissions": {"allow": []}, "hooks": {"X": []}})
        _out, code = self.run_sync(root=custom_root)
        self.assertEqual(code, 0)
        seed_after = json.loads((custom_root / "claude" / "settings.json").read_text())
        self.assertEqual(seed_after["hooks"], {"X": []})
        self.assertFalse((self.claude_dir / "settings.json").exists())

    def test_sync_rejects_missing_dotfiles_root(self) -> None:
        nonexistent_root = Path(self.tmpdir) / "does-not-exist"
        out, code = self.run_sync(root=nonexistent_root)
        self.assertEqual(code, 1)
        self.assertIn("does not exist", out)
        self.assertFalse(nonexistent_root.exists())

    def test_settings_seed_path_accepts_root_override(self) -> None:
        custom_root = Path(self.tmpdir) / "alt"
        (custom_root / "claude").mkdir(parents=True)
        self.assertEqual(
            ssdc.settings_seed_path(custom_root),
            custom_root / "claude" / "settings.json",
        )
        self.mark_work()
        self.assertEqual(
            ssdc.settings_seed_path(custom_root),
            custom_root / "claude" / "settings.work.json",
        )

    def test_opencode_seed_path_accepts_root_override(self) -> None:
        custom_root = Path(self.tmpdir) / "alt2"
        self.assertEqual(
            ssdc.opencode_seed_path(custom_root),
            custom_root / "opencode" / "opencode.jsonc",
        )
        self.mark_work()
        self.assertIsNone(ssdc.opencode_seed_path(custom_root))

    def test_main_dispatches_sync_to_seed_via_argparse(self) -> None:
        custom_root = Path(self.tmpdir) / "argparse-root"
        with patch.object(ssdc, "cmd_sync_to_seed") as mock_cmd:
            mock_cmd.return_value = 0
            ssdc.main(["sync-to-seed", "--dotfiles-root", str(custom_root)])
        mock_cmd.assert_called_once_with(custom_root, quiet=False)

    def test_verbosity_flags_parse_after_every_leaf_subcommand(self) -> None:
        # A leaf added later without an entry here silently loses coverage.
        custom_root = Path(self.tmpdir) / "argparse-root-quiet"
        cases = {
            "check": (ssdc, "cmd_check", []),
            "fix": (ssdc, "cmd_fix", []),
            "sync-to-seed": (
                ssdc,
                "cmd_sync_to_seed",
                ["--dotfiles-root", str(custom_root)],
            ),
            "push-vscode": (
                ssdc,
                "cmd_push_vscode",
                ["--dotfiles-root", str(custom_root)],
            ),
        }
        for cmd, (module, target, extra) in cases.items():
            with patch.object(module, target) as mock_cmd:
                mock_cmd.return_value = 0
                ssdc.main([cmd, *extra, "-q"])
            self.assertEqual(mock_cmd.call_args.kwargs.get("quiet"), True)

    # ── _try_parse_json ───────────────────────────────────────────────────

    def test_try_parse_json_returns_parsed_value(self) -> None:
        path = Path(self.tmpdir) / "ok.json"
        path.write_text('{"a": 1}')
        self.assertEqual(ssdc._try_parse_json(path), {"a": 1})

    def test_try_parse_json_returns_none_on_missing_file(self) -> None:
        path = Path(self.tmpdir) / "missing.json"
        self.assertIsNone(ssdc._try_parse_json(path))

    def test_try_parse_json_returns_none_on_parse_failure_never_raises(self) -> None:
        path = Path(self.tmpdir) / "bad.json"
        path.write_text("// a comment\n{not valid json}")
        self.assertIsNone(ssdc._try_parse_json(path))

    # ── vscode_drift ─────────────────────────────────────────────────────

    def test_vscode_drift_identical_content_is_no_drift(self) -> None:
        seed = Path(self.tmpdir) / "seed.json"
        live = Path(self.tmpdir) / "live.json"
        seed.write_text('{"a": 1}')
        live.write_text('{"a": 1}')
        self.assertEqual(ssdc.vscode_drift(seed, live), "")

    def test_vscode_drift_dict_key_diff(self) -> None:
        seed = Path(self.tmpdir) / "seed.json"
        live = Path(self.tmpdir) / "live.json"
        seed.write_text(json.dumps({"a": 1, "b": 2}))
        live.write_text(json.dumps({"a": 1, "b": 3}))
        self.assertEqual(ssdc.vscode_drift(seed, live), "b")

    def test_vscode_drift_list_length_diff(self) -> None:
        seed = Path(self.tmpdir) / "seed.json"
        live = Path(self.tmpdir) / "live.json"
        seed.write_text(json.dumps([{"key": "a"}]))
        live.write_text(json.dumps([{"key": "a"}, {"key": "b"}]))
        self.assertEqual(ssdc.vscode_drift(seed, live), "2 bindings live vs 1 in seed")

    def test_vscode_drift_list_same_length_content_differs(self) -> None:
        seed = Path(self.tmpdir) / "seed.json"
        live = Path(self.tmpdir) / "live.json"
        seed.write_text(json.dumps([{"key": "a"}]))
        live.write_text(json.dumps([{"key": "b"}]))
        self.assertEqual(
            ssdc.vscode_drift(seed, live), "binding definitions differ (1 bindings)"
        )

    def test_vscode_drift_jsonc_comment_still_reports_generic_fallback(self) -> None:
        """A live file with `//` comments fails to parse as JSON, but the
        raw-text compare must still catch the drift rather than silently
        returning "" just because the parse step failed."""
        seed = Path(self.tmpdir) / "seed.json"
        live = Path(self.tmpdir) / "live.json"
        seed.write_text(json.dumps({"a": 1}))
        live.write_text('// a comment\n{"a": 1, "b": 2}')
        self.assertEqual(
            ssdc.vscode_drift(seed, live), "content differs from the repo copy"
        )

    def test_vscode_drift_missing_file_is_nothing_to_compare(self) -> None:
        seed = Path(self.tmpdir) / "seed.json"
        live = Path(self.tmpdir) / "live.json"
        seed.write_text('{"a": 1}')
        self.assertEqual(ssdc.vscode_drift(seed, live), "")

    # ── vscode_seed_path ─────────────────────────────────────────────────

    def test_vscode_seed_path_no_profile_split(self) -> None:
        self.assertEqual(
            ssdc.vscode_seed_path("settings.json"),
            self.dotfiles / "vscode" / "settings.json",
        )
        self.mark_work()
        self.assertEqual(
            ssdc.vscode_seed_path("settings.json"),
            self.dotfiles / "vscode" / "settings.json",
        )

    def test_vscode_seed_path_accepts_root_override(self) -> None:
        custom_root = Path(self.tmpdir) / "alt3"
        self.assertEqual(
            ssdc.vscode_seed_path("keybindings.json", custom_root),
            custom_root / "vscode" / "keybindings.json",
        )

    # ── cmd_check: VS Code block ─────────────────────────────────────────

    def test_check_vscode_silent_when_user_dir_none(self) -> None:
        self.write_vscode_seed("settings.json", '{"a": 1}')
        self.write_live_vscode("settings.json", '{"a": 2}')
        with patch.object(ssdc, "_vscode_wsl_user_dir", lambda: None):
            out, code = self.run_check()
        self.assertEqual(out, "")
        self.assertEqual(code, 0)

    def test_check_vscode_silent_when_no_drift(self) -> None:
        self.write_vscode_seed("settings.json", '{"a": 1}\n')
        self.write_live_vscode("settings.json", '{"a": 1}\n')
        with patch.object(ssdc, "_vscode_wsl_user_dir", lambda: self.vscode_user_dir()):
            out, code = self.run_check()
        self.assertEqual(out, "")
        self.assertEqual(code, 0)

    def test_check_vscode_reports_drift_with_direction_neutral_wording(self) -> None:
        """`check` can't know which side of a whole-file VS Code diff is
        "correct", so it must name both directions (push-vscode and
        sync-to-seed) rather than recommending one by default — and must
        never recommend `fix`, which doesn't cover VS Code at all."""
        self.write_vscode_seed("settings.json", '{"a": 1}')
        self.write_live_vscode("settings.json", '{"a": 2}')
        with patch.object(ssdc, "_vscode_wsl_user_dir", lambda: self.vscode_user_dir()):
            out, code = self.run_check()
        self.assertIn("settings.json", out)
        self.assertIn("push-vscode", out)
        self.assertIn("sync-to-seed", out)
        self.assertNotRegex(out, r"run `[^`]*\bfix`")
        self.assertEqual(code, 0)

    # ── cmd_sync_to_seed: VS Code block ──────────────────────────────────

    def test_sync_vscode_writes_seed_from_live_with_backup(self) -> None:
        self.write_vscode_seed("settings.json", '{"a": 1}\n')
        self.write_live_vscode("settings.json", '{"a": 2}\n')
        with patch.object(ssdc, "_vscode_wsl_user_dir", lambda: self.vscode_user_dir()):
            out, code = self.run_sync()
        self.assertEqual(code, 0)
        self.assertIn("synced", out)
        self.assertEqual(
            (self.dotfiles / "vscode" / "settings.json").read_text(), '{"a": 2}\n'
        )
        backups = list((self.dotfiles / "vscode").glob("settings.json.bak.*"))
        self.assertEqual(len(backups), 1)
        self.assertEqual(backups[0].read_text(), '{"a": 1}\n')

    def test_sync_vscode_jsonc_comments_succeed_where_json_parse_would_fail(
        self,
    ) -> None:
        self.write_vscode_seed("settings.json", '{"a": 1}\n')
        live_text = '// user comment\n{"a": 1, "b": 2}\n'
        self.write_live_vscode("settings.json", live_text)
        with patch.object(ssdc, "_vscode_wsl_user_dir", lambda: self.vscode_user_dir()):
            _out, code = self.run_sync()
        self.assertEqual(code, 0)
        self.assertEqual(
            (self.dotfiles / "vscode" / "settings.json").read_text(), live_text
        )

    def test_sync_vscode_noop_when_user_dir_none(self) -> None:
        self.write_vscode_seed("settings.json", '{"a": 1}\n')
        with patch.object(ssdc, "_vscode_wsl_user_dir", lambda: None):
            out, code = self.run_sync()
        self.assertEqual(out, "")
        self.assertEqual(code, 0)
        self.assertEqual(
            (self.dotfiles / "vscode" / "settings.json").read_text(), '{"a": 1}\n'
        )

    def test_sync_vscode_noop_when_live_missing(self) -> None:
        self.write_vscode_seed("settings.json", '{"a": 1}\n')
        with patch.object(ssdc, "_vscode_wsl_user_dir", lambda: self.vscode_user_dir()):
            out, code = self.run_sync()
        self.assertEqual(out, "")
        self.assertEqual(code, 0)
        self.assertEqual(
            (self.dotfiles / "vscode" / "settings.json").read_text(), '{"a": 1}\n'
        )

    def test_sync_vscode_respects_dotfiles_root_override(self) -> None:
        custom_root = Path(self.tmpdir) / "vscode-alt-root"
        (custom_root / "vscode").mkdir(parents=True)
        (custom_root / "vscode" / "settings.json").write_text('{"a": 1}\n')
        self.write_live_vscode("settings.json", '{"a": 2}\n')
        with patch.object(ssdc, "_vscode_wsl_user_dir", lambda: self.vscode_user_dir()):
            _out, code = self.run_sync(root=custom_root)
        self.assertEqual(code, 0)
        self.assertEqual(
            (custom_root / "vscode" / "settings.json").read_text(), '{"a": 2}\n'
        )
        self.assertFalse((self.dotfiles / "vscode" / "settings.json").exists())

    # ── autoMode cosmetic key ────────────────────────────────────────────

    def test_autoMode_is_cosmetic_not_reported_as_drift(self) -> None:
        self.write_settings_seed({"permissions": {"allow": []}, "autoMode": "on"})
        self.write_live_settings({"permissions": {"allow": []}, "autoMode": "off"})
        out, code = self.run_check()
        self.assertEqual(out, "")
        self.assertEqual(code, 0)

    # ── _vscode_process_running ─────────────────────────────────────────

    def test_vscode_process_running_true_on_match(self) -> None:
        # Non-English sample string confirms the regex matches the image
        # name directly, not the (per-locale) "no tasks found" phrasing a
        # substring check would have relied on.
        stdout = (
            "イメージ名                       PID セッション名\n"
            "========================= ======== ================\n"
            "Code.exe                     1234 Console\n"
        )
        with patch(
            "subprocess.run",
            return_value=subprocess.CompletedProcess(
                args=[], returncode=0, stdout=stdout, stderr=""
            ),
        ):
            self.assertTrue(ssdc._vscode_process_running())

    def test_vscode_process_running_false_on_no_match(self) -> None:
        stdout = "INFO: No tasks are running which match the specified criteria.\n"
        with patch(
            "subprocess.run",
            return_value=subprocess.CompletedProcess(
                args=[], returncode=0, stdout=stdout, stderr=""
            ),
        ):
            self.assertFalse(ssdc._vscode_process_running())

    def test_vscode_process_running_none_on_timeout(self) -> None:
        with patch(
            "subprocess.run",
            side_effect=subprocess.TimeoutExpired(cmd="tasklist.exe", timeout=5),
        ):
            self.assertIsNone(ssdc._vscode_process_running())

    def test_vscode_process_running_none_when_tasklist_missing(self) -> None:
        with patch("subprocess.run", side_effect=FileNotFoundError()):
            self.assertIsNone(ssdc._vscode_process_running())

    # ── _push_vscode_to_live ────────────────────────────────────────────

    def test_push_vscode_to_live_identical_returns_none(self) -> None:
        seed = Path(self.tmpdir) / "seed.json"
        live = Path(self.tmpdir) / "live.json"
        seed.write_text('{"a": 1}\n')
        live.write_text('{"a": 1}\n')
        self.assertIsNone(ssdc._push_vscode_to_live(seed, live))

    def test_push_vscode_to_live_differing_returns_diff(self) -> None:
        seed = Path(self.tmpdir) / "seed.json"
        live = Path(self.tmpdir) / "live.json"
        seed.write_text('{"a": 2}\n')
        live.write_text('{"a": 1}\n')
        result = ssdc._push_vscode_to_live(seed, live)
        self.assertIsNotNone(result)
        live_exists, diff = result  # type: ignore[misc]
        self.assertTrue(live_exists)
        self.assertIn('"a": 1', diff)
        self.assertIn('"a": 2', diff)

    def test_push_vscode_to_live_missing_live_shows_creation_no_bak_implied(
        self,
    ) -> None:
        seed = Path(self.tmpdir) / "seed.json"
        live = Path(self.tmpdir) / "live-missing.json"
        seed.write_text('{"a": 1}\n')
        result = ssdc._push_vscode_to_live(seed, live)
        self.assertIsNotNone(result)
        live_exists, diff = result  # type: ignore[misc]
        self.assertFalse(live_exists)
        self.assertIn('"a": 1', diff)

    # ── cmd_push_vscode ──────────────────────────────────────────────────

    def test_push_vscode_wsl_dir_none_is_explicit_error_not_silent(self) -> None:
        with patch.object(ssdc, "_vscode_wsl_user_dir", lambda: None):
            out, code = self.run_push_vscode()
        self.assertEqual(code, 1)
        self.assertIn("WSL", out)

    def test_push_vscode_both_identical_is_noop_no_process_check_no_prompt(
        self,
    ) -> None:
        self.write_vscode_seed("settings.json", '{"a": 1}\n')
        self.write_live_vscode("settings.json", '{"a": 1}\n')
        self.write_vscode_seed("keybindings.json", "[]\n")
        self.write_live_vscode("keybindings.json", "[]\n")
        with (
            patch.object(ssdc, "_vscode_wsl_user_dir", lambda: self.vscode_user_dir()),
            patch.object(ssdc, "_vscode_process_running") as mock_proc,
            patch("builtins.input") as mock_input,
        ):
            out, code = self.run_push_vscode()
        self.assertEqual(code, 0)
        self.assertIn("nothing to push", out)
        mock_proc.assert_not_called()
        mock_input.assert_not_called()

    def test_push_vscode_confirmed_and_process_not_running_writes_with_backup(
        self,
    ) -> None:
        self.write_vscode_seed("settings.json", '{"a": 2}\n')
        self.write_live_vscode("settings.json", '{"a": 1}\n')
        self.write_vscode_seed("keybindings.json", "[]\n")
        self.write_live_vscode("keybindings.json", "[]\n")
        with (
            patch.object(ssdc, "_vscode_wsl_user_dir", lambda: self.vscode_user_dir()),
            patch.object(ssdc, "_vscode_process_running", lambda: False),
            patch("builtins.input", return_value="y"),
            patch("sys.stdin.isatty", return_value=True),
        ):
            out, code = self.run_push_vscode()
        self.assertEqual(code, 0)
        live_text = (self.vscode_user_dir() / "settings.json").read_text()
        self.assertEqual(live_text, '{"a": 2}\n')
        backups = list(self.vscode_user_dir().glob("settings.json.bak.*"))
        self.assertEqual(len(backups), 1)
        self.assertEqual(backups[0].read_text(), '{"a": 1}\n')

        # second run is a no-op — seed and live now match
        with (
            patch.object(ssdc, "_vscode_wsl_user_dir", lambda: self.vscode_user_dir()),
            patch.object(ssdc, "_vscode_process_running") as mock_proc,
            patch("builtins.input") as mock_input,
        ):
            out2, code2 = self.run_push_vscode()
        self.assertEqual(code2, 0)
        self.assertIn("nothing to push", out2)
        mock_proc.assert_not_called()
        mock_input.assert_not_called()

    def test_push_vscode_process_running_refuses_writes_neither_file(self) -> None:
        self.write_vscode_seed("settings.json", '{"a": 2}\n')
        self.write_live_vscode("settings.json", '{"a": 1}\n')
        self.write_vscode_seed("keybindings.json", '[{"key": "new"}]\n')
        self.write_live_vscode("keybindings.json", '[{"key": "old"}]\n')
        with (
            patch.object(ssdc, "_vscode_wsl_user_dir", lambda: self.vscode_user_dir()),
            patch.object(ssdc, "_vscode_process_running", lambda: True),
            patch("builtins.input", return_value="y"),
            patch("sys.stdin.isatty", return_value=True),
        ):
            out, code = self.run_push_vscode()
        self.assertEqual(code, 1)
        self.assertIn("close it first", out)
        # neither file written — confirms no-partial-write property
        self.assertEqual(
            (self.vscode_user_dir() / "settings.json").read_text(), '{"a": 1}\n'
        )
        self.assertEqual(
            (self.vscode_user_dir() / "keybindings.json").read_text(),
            '[{"key": "old"}]\n',
        )
        self.assertEqual(
            list(self.vscode_user_dir().glob("*.bak.*")),
            [],
        )

    def test_push_vscode_process_running_unknown_refuses_writes_neither_file(
        self,
    ) -> None:
        # Tightened behavior: an undetermined process check (None) now
        # refuses the push too, not just a confirmed-running (True) check —
        # unlike the old collapsed-bool contract, where a timeout/missing
        # tasklist.exe silently permitted the write.
        self.write_vscode_seed("settings.json", '{"a": 2}\n')
        self.write_live_vscode("settings.json", '{"a": 1}\n')
        self.write_vscode_seed("keybindings.json", "[]\n")
        self.write_live_vscode("keybindings.json", "[]\n")
        with (
            patch.object(ssdc, "_vscode_wsl_user_dir", lambda: self.vscode_user_dir()),
            patch.object(ssdc, "_vscode_process_running", lambda: None),
            patch("builtins.input", return_value="y"),
            patch("sys.stdin.isatty", return_value=True),
        ):
            out, code = self.run_push_vscode()
        self.assertEqual(code, 1)
        self.assertIn("close it first", out)
        self.assertEqual(
            (self.vscode_user_dir() / "settings.json").read_text(), '{"a": 1}\n'
        )

    def test_push_vscode_noninteractive_stdin_without_yes_aborts_nothing_written(
        self,
    ) -> None:
        self.write_vscode_seed("settings.json", '{"a": 2}\n')
        self.write_live_vscode("settings.json", '{"a": 1}\n')
        self.write_vscode_seed("keybindings.json", "[]\n")
        self.write_live_vscode("keybindings.json", "[]\n")
        with (
            patch.object(ssdc, "_vscode_wsl_user_dir", lambda: self.vscode_user_dir()),
            patch("sys.stdin.isatty", return_value=False),
        ):
            out, code = self.run_push_vscode()
        self.assertEqual(code, 1)
        self.assertIn("stdin is not interactive", out)
        self.assertEqual(
            (self.vscode_user_dir() / "settings.json").read_text(), '{"a": 1}\n'
        )

    def test_push_vscode_noninteractive_stdin_with_yes_proceeds_without_prompt(
        self,
    ) -> None:
        self.write_vscode_seed("settings.json", '{"a": 2}\n')
        self.write_live_vscode("settings.json", '{"a": 1}\n')
        self.write_vscode_seed("keybindings.json", "[]\n")
        self.write_live_vscode("keybindings.json", "[]\n")
        with (
            patch.object(ssdc, "_vscode_wsl_user_dir", lambda: self.vscode_user_dir()),
            patch.object(ssdc, "_vscode_process_running", lambda: False),
            patch("sys.stdin.isatty", return_value=False),
            patch("builtins.input") as mock_input,
        ):
            out, code = self.run_push_vscode(yes=True)
        self.assertEqual(code, 0)
        mock_input.assert_not_called()
        self.assertEqual(
            (self.vscode_user_dir() / "settings.json").read_text(), '{"a": 2}\n'
        )

    # ── committed seed key coverage (class guard) ───────────────────────

    def _assert_seed_key_coverage(
        self,
        *,
        real_seed_path: Path,
        expected_cosmetic: frozenset[str],
        expected_noncosmetic: frozenset[str],
        write_seed: Callable[[dict[str, object]], None],
        write_live: Callable[[dict[str, object]], None],
        check_stale: bool = True,
    ) -> None:
        real_seed = json.loads(real_seed_path.read_text())

        # The two expected sets must be disjoint — otherwise a key in both
        # would only ever exercise the cosmetic branch below, silently
        # skipping its non-cosmetic assertion.
        overlap = expected_cosmetic & expected_noncosmetic
        self.assertEqual(
            overlap,
            set(),
            f"key(s) {overlap!r} listed as both cosmetic and non-cosmetic in "
            "the EXPECTED_* sets",
        )
        _assert_no_key_name_collisions(expected_cosmetic | expected_noncosmetic)

        # Exhaustiveness: every committed key must be in exactly one
        # expected set. check_stale additionally requires every expected
        # key to still exist in the file — skip it for a file that's an
        # intentional subset of a larger expected-key universe (e.g.
        # settings.work.json vs. settings.json).
        committed_keys = set(real_seed.keys())
        expected_keys = expected_cosmetic | expected_noncosmetic
        unclassified = committed_keys - expected_keys
        self.assertEqual(
            unclassified,
            set(),
            f"seed key(s) {unclassified!r} in {real_seed_path} have no test "
            "classification — add to EXPECTED_*_COSMETIC or "
            "EXPECTED_*_NONCOSMETIC and confirm the intended drift behavior",
        )
        if check_stale:
            stale = expected_keys - committed_keys
            self.assertEqual(
                stale,
                set(),
                f"expected key(s) {stale!r} no longer exist in "
                f"{real_seed_path} — remove from the EXPECTED_* set",
            )

        # Behavioral: for each key, drift it and check the checker's
        # actual runtime output against the test's own expectation.
        for key in sorted(committed_keys):
            with self.subTest(key=key):
                live = dict(real_seed)
                live[key] = _drift_perturb(real_seed[key])
                write_seed(dict(real_seed))
                write_live(live)
                out, _code = self.run_check()
                if key in expected_cosmetic:
                    self.assertNotIn(
                        key, out, f"{key} expected cosmetic (silent) but was reported"
                    )
                else:
                    self.assertIn(key, out, f"{key} expected reported but was silent")

    def test_every_settings_seed_key_has_explicit_drift_coverage(self) -> None:
        self._assert_seed_key_coverage(
            real_seed_path=REPO_ROOT / "claude" / "settings.json",
            expected_cosmetic=EXPECTED_SETTINGS_COSMETIC,
            expected_noncosmetic=EXPECTED_SETTINGS_NONCOSMETIC,
            write_seed=self.write_settings_seed,
            write_live=self.write_live_settings,
        )

    def test_every_settings_work_seed_key_has_explicit_drift_coverage(self) -> None:
        self.mark_work()
        self._assert_seed_key_coverage(
            real_seed_path=REPO_ROOT / "claude" / "settings.work.json",
            expected_cosmetic=EXPECTED_SETTINGS_COSMETIC,
            expected_noncosmetic=EXPECTED_SETTINGS_NONCOSMETIC,
            write_seed=lambda body: self.write_settings_seed(
                body, name="settings.work.json"
            ),
            write_live=self.write_live_settings,
            check_stale=False,  # settings.work.json is an intentional subset
        )

    def test_every_opencode_seed_key_has_explicit_drift_coverage(self) -> None:
        self._assert_seed_key_coverage(
            real_seed_path=REPO_ROOT / "opencode" / "opencode.jsonc",
            expected_cosmetic=EXPECTED_OPENCODE_COSMETIC,
            expected_noncosmetic=EXPECTED_OPENCODE_NONCOSMETIC,
            write_seed=self.write_opencode_seed,
            write_live=self.write_live_opencode,
        )


class SettingsSeedDriftCheckDefaultRootTestCase(unittest.TestCase):
    def test_dotfiles_default_derives_from_script_location_not_home(self) -> None:
        # Regression: DOTFILES used to be hardcoded to ~/dotfiles, which
        # breaks the moment the checkout has any other name (e.g. a
        # worktree, or the ~/.dotfiles rename a GitHub-blocked work
        # machine's zip-based transfer workflow produces). It must be
        # derived from the script's own location instead, same convention
        # install.py already uses.
        self.assertEqual(ssdc.DOTFILES, REPO_ROOT)


if __name__ == "__main__":
    unittest.main()

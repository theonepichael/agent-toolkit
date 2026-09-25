import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
OPENCODE_DIR = REPO_ROOT / "opencode"
PACKAGE_JSON = OPENCODE_DIR / "package.json"
LOCKFILE = OPENCODE_DIR / "package-lock.json"
STAGES = ("test", "typecheck", "lint", "format:check")
REQUIRED_PACKAGES = (
    "@opencode-ai/plugin",
    "@types/node",
    "tsx",
    "typescript",
    "oxlint",
    "prettier",
)
REQUIRED_BINARIES = ("tsx", "tsc", "oxlint", "prettier")
MIN_TEST_TIMEOUT_MS = 30_000
EXACT_SEMVER = re.compile(
    r"\d+\.\d+\.\d+(?:-[0-9A-Za-z.-]+)?(?:\+[0-9A-Za-z.-]+)?"
)
VERSION_LINE = re.compile(
    r"v?(\d+\.\d+\.\d+(?:-[0-9A-Za-z.-]+)?(?:\+[0-9A-Za-z.-]+)?)"
)
DEV_BUILD_PATTERN = re.compile(r"^0\.0\.0-dev.*")


def _read_object(path: Path) -> dict[str, object]:
    assert path.is_file(), f"missing {path.relative_to(REPO_ROOT)}"
    value = json.loads(path.read_text())
    assert isinstance(value, dict), f"{path.relative_to(REPO_ROOT)} is not an object"
    return value


def _manifest_sdk_version() -> str:
    package = _read_object(PACKAGE_JSON)
    dev_dependencies = package.get("devDependencies")
    assert isinstance(dev_dependencies, dict), "opencode package has no devDependencies"
    version = dev_dependencies.get("@opencode-ai/plugin")
    assert isinstance(version, str), "opencode package has no plugin SDK version"
    return version


def _missing_install_requirements() -> list[str]:
    missing: list[str] = []
    for package in REQUIRED_PACKAGES:
        package_json = OPENCODE_DIR / "node_modules" / package / "package.json"
        if not package_json.is_file():
            missing.append(str(package_json.relative_to(REPO_ROOT)))
    for binary in REQUIRED_BINARIES:
        marker = OPENCODE_DIR / "node_modules" / ".bin" / binary
        if not marker.exists():
            missing.append(str(marker.relative_to(REPO_ROOT)))
    return missing


def _assert_gate_prerequisites() -> None:
    if shutil.which("npm") is None:
        pytest.skip("npm not installed")
    missing = _missing_install_requirements()
    if missing:
        listed = "\n".join(f"- {path}" for path in missing)
        pytest.fail(
            "run scripts/bootstrap-worktree.sh; opencode/node_modules is "
            f"incomplete:\n{listed}"
        )


def _run_stage(stage: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["npm", "run", stage],
        cwd=OPENCODE_DIR,
        capture_output=True,
        text=True,
        timeout=300,
        check=False,
    )


def _parse_version_output(output: str) -> str:
    lines = [line.strip() for line in output.splitlines() if line.strip()]
    assert lines, "opencode --version returned no output"
    match = VERSION_LINE.fullmatch(lines[0])
    assert match is not None, f"unrecognized opencode version line: {lines[0]!r}"
    return match.group(1)


def _check_opencode_cli_version(expected: str) -> None:
    if shutil.which("opencode") is None:
        pytest.skip("opencode CLI not installed; SDK version remains enforced")
    try:
        result = subprocess.run(
            ["opencode", "--version"],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        pytest.fail(f"opencode --version timed out after 10 seconds: {exc}")
    assert result.returncode == 0, (
        f"opencode --version failed (exit {result.returncode})\n"
        f"--- stdout ---\n{result.stdout}\n"
        f"--- stderr ---\n{result.stderr}"
    )
    actual = _parse_version_output(result.stdout)
    if DEV_BUILD_PATTERN.fullmatch(actual):
        pytest.skip(
            f"opencode CLI is a development build ({actual}); "
            f"SDK version {expected} is not enforced against dev builds"
        )
    assert actual == expected, (
        f"opencode CLI {actual} does not match pinned SDK {expected}; "
        "change the installed CLI or deliberately update the SDK pin and lockfile together"
    )


@pytest.mark.allow_real_subprocess
@pytest.mark.parametrize("stage", STAGES)
def test_opencode_ts_stage(stage: str) -> None:
    _assert_gate_prerequisites()
    result = _run_stage(stage)
    assert result.returncode == 0, (
        f"npm run {stage} failed (exit {result.returncode})\n"
        f"--- stdout ---\n{result.stdout}\n"
        f"--- stderr ---\n{result.stderr}"
    )


@pytest.mark.allow_real_subprocess
def test_installed_opencode_matches_sdk_pin() -> None:
    expected = _manifest_sdk_version()
    installed = _read_object(
        OPENCODE_DIR / "node_modules" / "@opencode-ai" / "plugin" / "package.json"
    ).get("version")
    assert installed == expected, (
        f"installed plugin SDK {installed!r} does not match manifest pin {expected!r}; "
        "rerun npm install in opencode/"
    )


@pytest.mark.allow_real_subprocess
def test_installed_opencode_cli_matches_sdk_pin() -> None:
    _check_opencode_cli_version(_manifest_sdk_version())


def test_incomplete_node_modules_fails_instead_of_skipping(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(shutil, "which", lambda _name: "/usr/bin/npm")
    monkeypatch.setattr(
        "test_opencode_ts_checks._missing_install_requirements",
        lambda: ["opencode/node_modules/.bin/tsc"],
    )
    with pytest.raises(pytest.fail.Exception) as excinfo:
        _assert_gate_prerequisites()
    message = str(excinfo.value)
    assert "opencode/node_modules/.bin/tsc" in message
    assert "bootstrap-worktree" in message


def test_absent_npm_skips_rather_than_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(shutil, "which", lambda _name: None)
    monkeypatch.setattr(
        "test_opencode_ts_checks._missing_install_requirements",
        lambda: ["opencode/node_modules/.bin/tsc"],
    )
    with pytest.raises(pytest.skip.Exception):
        _assert_gate_prerequisites()


def test_plugin_sdk_pin_is_exact() -> None:
    version = _manifest_sdk_version()
    assert EXACT_SEMVER.fullmatch(version), (
        f"@opencode-ai/plugin must use an exact semantic version, got {version!r}"
    )


def test_lockfile_sdk_versions_match_manifest() -> None:
    expected = _manifest_sdk_version()
    lockfile = _read_object(LOCKFILE)
    packages = lockfile.get("packages")
    assert isinstance(packages, dict), "package-lock has no packages object"
    root_package = packages.get("")
    assert isinstance(root_package, dict), "package-lock has no root package"
    root_dependencies = root_package.get("devDependencies")
    assert isinstance(root_dependencies, dict), "package-lock root has no devDependencies"
    assert root_dependencies.get("@opencode-ai/plugin") == expected
    installed_package = packages.get("node_modules/@opencode-ai/plugin")
    assert isinstance(installed_package, dict), "package-lock has no installed SDK entry"
    assert installed_package.get("version") == expected


def test_stages_match_package_scripts() -> None:
    scripts = _read_object(PACKAGE_JSON).get("scripts")
    assert isinstance(scripts, dict), "opencode package has no scripts"
    for stage in STAGES:
        assert stage in scripts, f"{stage} is not an opencode package script"


def test_test_timeout_survives_machine_load() -> None:
    scripts = _read_object(PACKAGE_JSON).get("scripts")
    assert isinstance(scripts, dict), "opencode package has no scripts"
    test_script = scripts.get("test")
    assert isinstance(test_script, str), "opencode test script is not a string"
    match = re.search(r"--test-timeout=(\d+)", test_script)
    assert match is not None, f"opencode test script has no timeout: {test_script}"
    configured = int(match.group(1))
    assert configured >= MIN_TEST_TIMEOUT_MS


def test_smoke_spec_glob_is_not_empty() -> None:
    assert "plugins.test.ts" in {path.name for path in (OPENCODE_DIR / "test").glob("*.test.ts")}


def test_required_packages_and_binaries_cover_all_tools() -> None:
    assert set(REQUIRED_PACKAGES) == {
        "@opencode-ai/plugin",
        "@types/node",
        "tsx",
        "typescript",
        "oxlint",
        "prettier",
    }
    assert REQUIRED_BINARIES == ("tsx", "tsc", "oxlint", "prettier")


def test_version_check_skips_without_opencode_cli(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(shutil, "which", lambda _name: None)
    with pytest.raises(pytest.skip.Exception):
        _check_opencode_cli_version("1.18.32")


def test_version_check_accepts_matching_output(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(shutil, "which", lambda _name: "/usr/bin/opencode")
    monkeypatch.setattr(
        "test_opencode_ts_checks.subprocess.run",
        lambda *_args, **_kwargs: subprocess.CompletedProcess([], 0, "1.18.32\n", ""),
    )
    _check_opencode_cli_version("1.18.32")


def test_version_check_rejects_mismatch(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(shutil, "which", lambda _name: "/usr/bin/opencode")
    monkeypatch.setattr(
        "test_opencode_ts_checks.subprocess.run",
        lambda *_args, **_kwargs: subprocess.CompletedProcess([], 0, "1.18.33\n", ""),
    )
    with pytest.raises(AssertionError) as excinfo:
        _check_opencode_cli_version("1.18.32")
    assert "1.18.33" in str(excinfo.value)
    assert "1.18.32" in str(excinfo.value)


def test_version_check_rejects_malformed_output(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(shutil, "which", lambda _name: "/usr/bin/opencode")
    monkeypatch.setattr(
        "test_opencode_ts_checks.subprocess.run",
        lambda *_args, **_kwargs: subprocess.CompletedProcess([], 0, "banner\n1.18.32\n", ""),
    )
    with pytest.raises(AssertionError):
        _check_opencode_cli_version("1.18.32")


def test_version_check_rejects_nonzero_exit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(shutil, "which", lambda _name: "/usr/bin/opencode")
    monkeypatch.setattr(
        "test_opencode_ts_checks.subprocess.run",
        lambda *_args, **_kwargs: subprocess.CompletedProcess([], 1, "", "broken"),
    )
    with pytest.raises(AssertionError) as excinfo:
        _check_opencode_cli_version("1.18.32")
    assert "broken" in str(excinfo.value)


def test_version_check_rejects_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(shutil, "which", lambda _name: "/usr/bin/opencode")

    def timeout(*_args: object, **_kwargs: object) -> None:
        raise subprocess.TimeoutExpired(["opencode", "--version"], 10)

    monkeypatch.setattr("test_opencode_ts_checks.subprocess.run", timeout)
    with pytest.raises(pytest.fail.Exception) as excinfo:
        _check_opencode_cli_version("1.18.32")
    assert "timed out" in str(excinfo.value)


def test_version_check_skips_on_dev_build(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(shutil, "which", lambda _name: "/usr/bin/opencode")
    monkeypatch.setattr(
        "test_opencode_ts_checks.subprocess.run",
        lambda *_args, **_kwargs: subprocess.CompletedProcess(
            [], 0, "0.0.0-dev-202609250015\n", ""
        ),
    )
    with pytest.raises(pytest.skip.Exception) as excinfo:
        _check_opencode_cli_version("1.18.32")
    assert "development build" in str(excinfo.value)
    assert "0.0.0-dev-202609250015" in str(excinfo.value)


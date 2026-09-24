"""Payload matrix and end-to-end runner for the guard_rails.py fast path.

Shared by test/test_guard_rails_fast_path.py and by this module's own
``--write-baseline`` mode, which recorded
test/fixtures/guard_fast_path_baseline.json from the pre-fast-path script.
That committed baseline is what the identity tests compare against: comparing
the new fast path only against the new module's own slow path could not catch
a change both paths share. The two non-string Claude Bash crash entries were
updated when payload validation began returning an allow verdict, and cases
added since that recording establish the same fast/slow contract.

Each case runs the real script in a subprocess under a throwaway ``HOME``
holding a git repository on ``main``, so the bash rules see a protected
branch. Machine-specific paths are normalised to ``<HOME>`` and ``<REPO>``,
and the audit record's ``ts`` is dropped.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPT = REPO_ROOT / "agent-scripts" / "guard_rails.py"
BASELINE = Path(__file__).resolve().parent / "fixtures" / "guard_fast_path_baseline.json"

_BROKEN_POINTER = '{"schema": 999, "layout": "nonsense"}\n'


def _claude(tool: str, tool_input: object, cwd: object = "<REPO>") -> dict:
    return {"tool_name": tool, "tool_input": tool_input, "cwd": cwd}


# (case id, argv, stdin payload or raw string or None, env overrides, pointer)
# A dict/list payload is JSON-encoded; a str is sent verbatim; None sends no
# stdin at all. "<REPO>" anywhere in argv or payload becomes the repo path.
CASES: list[tuple[str, list[str], object, dict[str, str], str | None]] = [
    ("claude-bash-ls", ["--harness", "claude"], _claude("Bash", {"command": "ls"}), {}, None),
    ("claude-bash-padded-name", ["--harness", "claude"], _claude(" Bash ", {"command": "ls"}), {}, None),
    ("claude-bash-lower-name", ["--harness", "claude"], _claude("bash", {"command": "git status"}), {}, None),
    ("claude-bash-no-verify", ["--harness", "claude"], _claude("Bash", {"command": "git commit --no-verify -m x"}), {}, None),
    ("claude-bash-config-read", ["--harness", "claude"], _claude("Bash", {"command": "git config --get user.name"}), {}, None),
    ("claude-bash-hookspath-set", ["--harness", "claude"], _claude("Bash", {"command": "git config core.hooksPath /tmp/x"}), {}, None),
    ("claude-bash-quote-splice", ["--harness", "claude"], _claude("Bash", {"command": 'git commit --no-ver""ify -m x'}), {}, None),
    ("claude-bash-secret", ["--harness", "claude"], _claude("Bash", {"command": "curl -H 'Authorization: Bearer sk-ant-abcdefghijklmnopqrstuvwxyz0123' x"}), {}, None),
    ("claude-bash-empty-command", ["--harness", "claude"], _claude("Bash", {"command": ""}), {}, None),
    ("claude-bash-nonstr-command", ["--harness", "claude"], _claude("Bash", {"command": 5}), {}, None),
    ("claude-bash-nonstr-cwd", ["--harness", "claude"], _claude("Bash", {"command": "ls"}, cwd=123), {}, None),
    ("claude-bash-falsey-command", ["--harness", "claude"], _claude("Bash", {"command": 0}), {}, None),
    ("claude-bash-falsey-cwd", ["--harness", "claude"], _claude("Bash", {"command": "ls"}, cwd=[]), {}, None),
    ("claude-bash-nondict-input", ["--harness", "claude"], _claude("Bash", "ls"), {}, None),
    ("claude-bash-nonstr-path", ["--harness", "claude"], _claude("Bash", {"command": "ls", "file_path": 7}), {}, None),
    ("claude-read", ["--harness", "claude"], _claude("Read", {"file_path": "<REPO>/README.md"}), {}, None),
    ("claude-glob", ["--harness", "claude"], _claude("Glob", {"pattern": "*.py"}), {}, None),
    ("claude-nonstr-name", ["--harness", "claude"], _claude(42, {"command": "ls"}), {}, None),
    ("claude-edit", ["--harness", "claude"], _claude("Edit", {"file_path": "<REPO>/README.md"}), {}, None),
    ("claude-malformed-json", ["--harness", "claude"], "not json at all", {}, None),
    ("claude-empty-stdin", ["--harness", "claude"], "", {}, None),
    ("claude-json-list", ["--harness", "claude"], [1, 2], {}, None),
    ("agy-view", ["--harness", "agy"], {"toolCall": {"name": "view_file", "args": {"path": "<REPO>/README.md"}}, "cwd": "<REPO>"}, {}, None),
    ("agy-bash", ["--harness", "agy"], {"toolCall": {"name": "bash", "args": {}}, "cwd": "<REPO>"}, {}, None),
    ("agy-write", ["--harness", "agy"], {"toolCall": {"name": "write_to_file", "args": {"TargetFile": "<REPO>/README.md"}}, "cwd": "<REPO>"}, {}, None),
    ("agy-write-nonstr-cwd", ["--harness", "agy"], {"toolCall": {"name": "write_to_file", "args": {"TargetFile": "README.md"}}, "cwd": 123}, {}, None),
    ("agy-nondict-call", ["--harness", "agy"], {"toolCall": "view_file", "cwd": "<REPO>"}, {}, None),
    ("copilot-view", ["--harness", "copilot"], {"toolName": "view", "toolArgs": '{"path": "<REPO>/README.md"}', "cwd": "<REPO>"}, {}, None),
    ("copilot-bad-args", ["--harness", "copilot"], {"toolName": "view", "toolArgs": "{not json", "cwd": "<REPO>"}, {}, None),
    ("copilot-create", ["--harness", "copilot"], {"toolName": "create", "toolArgs": '{"path": "<REPO>/README.md"}', "cwd": "<REPO>"}, {}, None),
    ("copilot-create-nonstr-cwd", ["--harness", "copilot"], {"toolName": "create", "toolArgs": '{"path": "README.md"}', "cwd": 123}, {}, None),
    ("neutral-bash-ls", ["--tool", "bash", "--cwd", "<REPO>", "--command", "ls"], None, {}, None),
    ("neutral-bash-empty", ["--tool", "bash", "--cwd", "<REPO>", "--command", ""], None, {}, None),
    ("neutral-bash-no-verify", ["--tool", "bash", "--cwd", "<REPO>", "--command", "git commit --no-verify"], None, {}, None),
    ("neutral-bash-dash-cwd", ["--tool", "bash", "--cwd", "-x", "--command", "ls"], None, {}, None),
    ("neutral-bash-dash-command", ["--tool", "bash", "--cwd", "<REPO>", "--command", "-x"], None, {}, None),
    ("neutral-read", ["--tool", "read", "--cwd", "<REPO>", "--path", "<REPO>/README.md"], None, {}, None),
    ("claude-bash-ls-quiet", ["--harness", "claude", "-q"], _claude("Bash", {"command": "ls"}), {}, None),
    ("off-claude-bash-ls", ["--harness", "claude"], _claude("Bash", {"command": "ls"}), {"GUARD_RAILS_OFF": "1"}, None),
    ("off-claude-bash-no-verify", ["--harness", "claude"], _claude("Bash", {"command": "git commit --no-verify"}), {"GUARD_RAILS_OFF": "1"}, None),
    ("off-claude-read", ["--harness", "claude"], _claude("Read", {"file_path": "<REPO>/README.md"}), {"GUARD_RAILS_OFF": "1"}, None),
    ("off-copilot-view", ["--harness", "copilot"], {"toolName": "view", "toolArgs": "{}", "cwd": "<REPO>"}, {"GUARD_RAILS_OFF": "1"}, None),
    ("off-neutral-bash-ls", ["--tool", "bash", "--cwd", "<REPO>", "--command", "ls"], None, {"GUARD_RAILS_OFF": "1"}, None),
    ("broken-claude-bash-ls", ["--harness", "claude"], _claude("Bash", {"command": "ls"}), {}, _BROKEN_POINTER),
    ("broken-copilot-view", ["--harness", "copilot"], {"toolName": "view", "toolArgs": "{}", "cwd": "<REPO>"}, {}, _BROKEN_POINTER),
    ("broken-neutral-bash-ls", ["--tool", "bash", "--cwd", "<REPO>", "--command", "ls"], None, {}, _BROKEN_POINTER),
    ("off-broken-claude-bash-ls", ["--harness", "claude"], _claude("Bash", {"command": "ls"}), {"GUARD_RAILS_OFF": "1"}, _BROKEN_POINTER),
]


def _subst(value: object, repo: str) -> object:
    if isinstance(value, str):
        return value.replace("<REPO>", repo)
    if isinstance(value, list):
        return [_subst(v, repo) for v in value]
    if isinstance(value, dict):
        return {k: _subst(v, repo) for k, v in value.items()}
    return value


def _make_repo(root: Path) -> Path:
    repo = root / "repo"
    repo.mkdir()
    env = {**os.environ, "HOME": str(root), "GIT_CONFIG_NOSYSTEM": "1"}
    for args in (
        ["init", "-q", "-b", "main"],
        ["-c", "user.email=t@t", "-c", "user.name=t", "commit", "-q", "--allow-empty", "-m", "init"],
    ):
        subprocess.run(["git", *args], cwd=repo, env=env, check=True)
    (repo / "README.md").write_text("x\n")
    return repo


def run_case(
    case: tuple[str, list[str], object, dict[str, str], str | None],
    extra_env: dict[str, str] | None = None,
) -> dict[str, object]:
    """Run one case end to end in a fresh sandbox; return its normalised result."""
    _, argv, payload, env_over, pointer = case
    with tempfile.TemporaryDirectory() as tmp:
        home = Path(tmp).resolve()
        repo = _make_repo(home)
        if pointer is not None:
            ptr = home / ".claude" / "data" / "toolkit_state.json"
            ptr.parent.mkdir(parents=True, exist_ok=True)
            ptr.write_text(pointer)
        if payload is None:
            stdin = ""
        elif isinstance(payload, str):
            stdin = payload
        else:
            stdin = json.dumps(_subst(payload, str(repo)))
        env = {
            k: v
            for k, v in os.environ.items()
            if not k.startswith(("GUARD_RAILS", "AGENT_TOOLKIT", "GIT_"))
        }
        env.update(HOME=str(home), GIT_CONFIG_NOSYSTEM="1", **env_over)
        env.update(extra_env or {})
        proc = subprocess.run(
            [sys.executable, str(SCRIPT), *_subst(argv, str(repo))],
            input=stdin,
            capture_output=True,
            text=True,
            env=env,
            cwd=repo,
            timeout=60,
        )
        records = []
        for log in sorted(home.rglob("guard_rails_audit.jsonl")):
            for line in log.read_text().splitlines():
                rec = json.loads(line)
                rec.pop("ts", None)
                records.append(rec)

        def norm(text: str) -> str:
            return text.replace(str(repo), "<REPO>").replace(str(home), "<HOME>")

        return {
            "stdout": norm(proc.stdout),
            "exit": proc.returncode,
            "audit": json.loads(norm(json.dumps(records))),
        }


def write_baseline() -> None:
    results = {case[0]: run_case(case) for case in CASES}
    BASELINE.parent.mkdir(parents=True, exist_ok=True)
    BASELINE.write_text(json.dumps(results, indent=2, sort_keys=True) + "\n")
    print(f"wrote {len(results)} cases to {BASELINE}")


if __name__ == "__main__":
    if sys.argv[1:] == ["--write-baseline"]:
        write_baseline()
    else:
        sys.exit("usage: guard_fast_path_matrix.py --write-baseline")

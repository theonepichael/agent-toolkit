"""Subprocess tests for which interpreter ../install.sh hands install.py to.

Runs the real install.sh under a PATH that holds only fake interpreters.
Each fake answers install.sh's ``-c`` version guard as a given Python
version and, when exec'd with install.py, prints its own name instead of
installing anything. /usr/bin is never on PATH, so the machine's real
python3 can't leak into the choice.
"""

import os
import stat
import subprocess
from pathlib import Path

import pytest

pytestmark = pytest.mark.allow_real_subprocess

INSTALL_SH = Path(__file__).resolve().parent.parent / "install.sh"

FAKE_INTERPRETER = """#!/bin/sh
if [ "$1" = "-c" ]; then
  exit {guard_status}
fi
echo "CHOSEN {name}"
"""


def _fake_bin(tmp_path: Path, versions: dict[str, tuple[int, int]]) -> Path:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    # install.sh needs dirname to resolve its own directory; nothing else
    # external. Linking it alone keeps every real python off PATH.
    dirname = Path("/usr/bin/dirname")
    if not dirname.exists():
        dirname = Path("/bin/dirname")
    (bin_dir / "dirname").symlink_to(dirname)
    for name, version in versions.items():
        fake = bin_dir / name
        guard_status = 0 if version >= (3, 12) else 1
        fake.write_text(FAKE_INTERPRETER.format(guard_status=guard_status, name=name))
        fake.chmod(fake.stat().st_mode | stat.S_IEXEC)
    return bin_dir


def _run(bin_dir: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["/bin/sh", str(INSTALL_SH), "--help"],
        env={"PATH": str(bin_dir), "HOME": os.environ.get("HOME", "/tmp")},
        capture_output=True,
        text=True,
    )


def test_prefers_python314_over_an_older_python3(tmp_path: Path) -> None:
    bin_dir = _fake_bin(
        tmp_path,
        {"python3.14": (3, 14), "python3.12": (3, 12), "python3": (3, 12)},
    )

    result = _run(bin_dir)

    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "CHOSEN python3.14"


def test_prefers_python3_over_python313_when_no_python314(tmp_path: Path) -> None:
    bin_dir = _fake_bin(tmp_path, {"python3": (3, 14), "python3.13": (3, 13)})

    result = _run(bin_dir)

    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "CHOSEN python3"


def test_falls_back_to_python312_when_python3_is_too_old(tmp_path: Path) -> None:
    bin_dir = _fake_bin(tmp_path, {"python3": (3, 10), "python3.12": (3, 12)})

    result = _run(bin_dir)

    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "CHOSEN python3.12"


def test_refuses_when_nothing_is_312_or_newer(tmp_path: Path) -> None:
    bin_dir = _fake_bin(tmp_path, {"python3": (3, 11), "python": (3, 9)})

    result = _run(bin_dir)

    assert result.returncode == 1
    assert "CHOSEN" not in result.stdout
    assert "no Python 3.12+ found" in result.stderr

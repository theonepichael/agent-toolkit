"""Named crash points for proving what a killed process leaves behind.

Code with durable multi-step writes calls ``checkpoint(name)`` at each
journal boundary. Normally the call does nothing. When a test harness arms
one checkpoint, the process kills itself there with SIGKILL, so no
``finally`` block, ``atexit`` handler, or exception unwinding runs. The test
then runs recovery and checks the result against its expected state.

A kill proves only what a crashed process fails to clean up. It does not
prove durability across power loss: the page cache survives a process kill.

Checkpoint names match ``^[a-z0-9][a-z0-9.-]*$`` and must be unique within
one run, because arming kills at the first call with that name. A boundary
repeated per domain carries the domain in its name, for example
``promote.work-items.before``.

Environment
  AGENT_TOOLKIT_FAULT_CHECKPOINT=<name>@<parent pid> arms one checkpoint. It
  fires only in a process whose parent PID equals the recorded one, so a
  stale value inherited by a real session does not fire unless that PID was
  reused. It is bound to the parent, not to one child: siblings share a
  parent, so the harness sets a fresh value per launch. A malformed value is
  ignored.
  AGENT_TOOLKIT_FAULT_MARKER=<path> is where a fired checkpoint records its
  name before the kill.

Files written
  The marker file, only when a checkpoint fires. Unbuffered write, then
  fsync, then the kill. A failed marker write still kills.

Exit behaviour
  A fired checkpoint ends the process by SIGKILL (return code -9 to the
  parent). Otherwise ``checkpoint()`` returns None. A malformed name raises
  ValueError whether or not anything is armed.
"""

import os
import re
import signal

# What this module does with toolkit data (checked by scripts/check_toolkit_paths.py).
TOOLKIT_DATA = "infrastructure"

ENV_ARM = "AGENT_TOOLKIT_FAULT_CHECKPOINT"
ENV_MARKER = "AGENT_TOOLKIT_FAULT_MARKER"

_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9.-]*$")


def checkpoint(name: str) -> None:
    """Die here by SIGKILL if the harness armed this checkpoint; else return."""
    if not _NAME_RE.match(name):
        raise ValueError(f"invalid checkpoint name: {name!r}")
    armed = os.environ.get(ENV_ARM)
    if armed is None:
        return
    armed_name, sep, ppid = armed.rpartition("@")
    if not sep or armed_name != name or not ppid.isdigit():
        return
    if int(ppid) != os.getppid():
        return
    _record_marker(name)
    os.kill(os.getpid(), signal.SIGKILL)


def _record_marker(name: str) -> None:
    marker = os.environ.get(ENV_MARKER)
    if not marker:
        return
    try:
        fd = os.open(marker, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        try:
            os.write(fd, name.encode())
            os.fsync(fd)
        finally:
            os.close(fd)
    except OSError:
        pass

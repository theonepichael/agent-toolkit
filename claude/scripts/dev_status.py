#!/usr/bin/env python3
"""dev_status.py — thin launcher; the implementation lives in dev_status_impl.

Run as a script (`python3 ~/.claude/scripts/dev_status.py <cmd>`), this
imports ``dev_status_impl`` and calls its ``main()`` with argv untouched, so
the CLI surface, output, and exit codes come entirely from the implementation
module — whose ``.pyc`` the interpreter reuses across invocations instead of
recompiling ~5k lines as ``__main__`` on every run.

Imported as a module (``import dev_status``), it rebinds
``sys.modules["dev_status"]`` to the impl module object, so consumers that
patch module globals (``patch.object(dev_status, "DATA_DIR", ...)`` and
friends) mutate the exact globals the implementation's functions read as
their own.

The ``_IMPL_MODULE`` assignment is load-bearing for ``gen_interfaces.py``,
which follows it to extract INTERFACES.md's ``dev_status.py`` entry from the
impl source and to keep the impl out of the inventory as a separate script.

Requires Python 3.12+.
"""

import importlib
import os
import sys

_IMPL_MODULE = "dev_status_impl"

_HERE = os.path.realpath(os.path.dirname(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

_impl = importlib.import_module(_IMPL_MODULE)

if __name__ == "__main__":
    _impl.main()
else:
    sys.modules[__name__] = _impl

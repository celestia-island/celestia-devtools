"""Bind the suite to this checkout, not to whatever copy is installed.

*Why this exists:* CI installs the package editable, but a machine can carry a
plain (non-editable) install of an older commit that still reports the same
version string -- ``0.14.2`` in site-packages is indistinguishable from
``0.14.2`` in the working tree, because the version is single-sourced and only
bumped when someone remembers. Without this file ``python3 -m pytest`` imports
the installed copy: a green local run then certifies code the author never
touched, and a module that uses a newer symbol fails at *collection* instead of
at an assertion, which reads as a broken test rather than a stale environment.

Measured before this file existed (2026-10-30): 3 of 65 test modules failed to
collect against a site-packages copy that was two commits behind, and the
other 62 collected against it -- five of them only because they prepend ``src``
themselves (two more prepend ``tools/``, which this file does not cover).

Two channels are bound, because the suite uses both:

- ``sys.path``, for in-process imports;
- ``$PYTHONPATH``, for the tests that spawn ``python -m celestia_devtools``. A
  child process inherits the environment, never the parent's ``sys.path``, so
  ``test_branch_sweep.test_main_dry_run_via_cli`` was quietly exercising the
  installed copy while the rest of that module used the checkout.

The wheel ships no ``tests/`` directory, so this file only ever runs inside a
source checkout; if ``src`` is absent it leaves the interpreter untouched.
"""

from __future__ import annotations
import os
import sys
from pathlib import Path

_SRC = Path(__file__).resolve().parent.parent / "src"

def _bind_to_checkout() -> None:
    if not _SRC.is_dir():
        return

    text = str(_SRC)

    # Drop an earlier entry for the same directory before inserting: a stale
    # editable `.pth` can leave ``src`` on the path but behind a site-packages
    # directory that shadows it, and a plain `not in sys.path` check would then
    # no-op while still claiming the checkout wins.
    while text in sys.path:
        sys.path.remove(text)

    sys.path.insert(0, text)
    existing = os.environ.get("PYTHONPATH", "")
    os.environ["PYTHONPATH"] = (text + os.pathsep + existing) if existing else text

_bind_to_checkout()

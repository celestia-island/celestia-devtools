"""Bridge to the external ``markdownlint-cli2`` linter.

Invoked only when the binary is on PATH and the caller opts in via
``--use-markdownlint``.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path


def maybe_run_markdownlint(target: Path, check_only: bool) -> None:
    binary = shutil.which("markdownlint-cli2")
    if not binary:
        return
    command = [binary]
    if not check_only:
        command.append("--fix")
    command.extend([str(target / "**/*.md") if target.is_dir()
                   else str(target), "#target", "#node_modules"])
    subprocess.run(
        command,
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )

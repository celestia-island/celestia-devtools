"""The package version must have exactly one source of truth.

Before this guard, ``pyproject.toml`` carried a static version that drifted
from ``__init__.__version__`` (0.14.1 vs 0.9.0 at the time), so ``--version``
reported a version months stale. Now hatchling derives the dist metadata from
``__init__.py``; these tests fail if anyone reintroduces a second declaration.
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
import tomllib
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
INIT = REPO / "src" / "celestia_devtools" / "__init__.py"
PYPROJECT = REPO / "pyproject.toml"


def _dunder_version() -> str:
    match = re.search(r'^__version__ = "([^"]+)"$', INIT.read_text(encoding="utf-8"), re.M)
    assert match, "__version__ literal not found in __init__.py"
    return match.group(1)


class TestVersionSingleSource:
    def test_pyproject_derives_the_version_from_the_package(self):
        data = tomllib.loads(PYPROJECT.read_text(encoding="utf-8"))
        assert "version" in data["project"].get("dynamic", []), (
            "pyproject.toml must not declare a static [project] version — it "
            "drifts from __init__.__version__; keep dynamic = [\"version\"] "
            "with tool.hatch.version.path instead"
        )
        assert (
            data["tool"]["hatch"]["version"]["path"]
            == "src/celestia_devtools/__init__.py"
        ), "tool.hatch.version.path must point at the single source"

    def test_version_output_matches_the_single_source(self):
        # Resolve the package from THIS checkout so a stale host install
        # cannot turn this into a false red (or a false green).
        src = REPO / "src"
        env = dict(os.environ)
        env["PYTHONPATH"] = (
            f"{src}{os.pathsep}{env['PYTHONPATH']}" if env.get("PYTHONPATH") else str(src)
        )
        result = subprocess.run(
            [
                sys.executable,
                "-c",
                "from celestia_devtools.core.cli import main; "
                "import sys; sys.exit(main(['--version']))",
            ],
            capture_output=True,
            text=True,
            timeout=60,
            env=env,
        )
        assert result.returncode == 0, result.stderr
        assert result.stdout.strip() == f"celestia-devtools {_dunder_version()}", (
            f"--version disagrees with the single source: "
            f"stdout={result.stdout.strip()!r} literal={_dunder_version()!r}"
        )

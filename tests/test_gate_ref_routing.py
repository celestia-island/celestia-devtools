"""Pin the gate-CLI ref-determinism contract of the reusable workflows (H14).

*Why this exists:* the reusable gates once preferred any preinstalled
``celestia-devtools`` that "knew the command" and only cloned the caller's
``devtools-ref`` when the preinstall was missing. On node-1 the preinstall is
an editable install tracking master, so a caller pinning ``v2026.11`` was
judged by whatever baselines master happened to carry — the same tree drew
opposite conclusions from two CLIs (the 2026-10 H14 incident: a 0.14.2
snapshot vs master's family data said conflicting things about one checkout).

The contract since the fix: a caller that pins ``devtools-ref`` names the
EXACT CLI that must judge the tree. Any ref other than the master default
always runs from a fresh clone or install at that ref; the preinstalled fast
path survives only for the master default and prints its version so drift is
diagnosable from the log.

The tests below execute each workflow's routing script with stubbed
``git`` / ``python3`` / ``celestia-devtools`` on PATH and assert the routing
decision, so a regression to "prefer preinstalled" flips them red instead of
merely moving text around.
"""

from __future__ import annotations
import os
import re
import stat
import subprocess
from pathlib import Path
import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent
WORKFLOW_DIR = REPO_ROOT / ".github" / "workflows"

#: workflow file -> the step name whose run script routes the CLI choice.
ROUTING_STEPS = {"family-versions.yml": "Family versions", "verify-versions.yml": "Verify versions", "style-lint.yml": "Ensure celestia-devtools", "rules-lint.yml": "Ensure celestia-rules-lint"}

#: The guard that keeps the preinstalled fast path on the master lane only.
GUARD = '"${DEVTOOLS_REF:-master}" = "master"'

def _routing_script(filename: str) -> str:
    doc = yaml.safe_load((WORKFLOW_DIR / filename).read_text(encoding = "utf-8"))
    steps = next(iter(doc["jobs"].values()))["steps"]

    for step in steps:
        if step.get("name") == ROUTING_STEPS[filename]:
            return str(step["run"])

    pytest.fail(f"{filename} lost its '{ROUTING_STEPS[filename]}' step entirely")

def _write_stub(directory: Path, name: str, body: str) -> None:
    path = directory / name
    path.write_text(f"#!/usr/bin/env bash\n{body}\n", encoding = "utf-8")
    path.chmod(path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)

def _run_routing(tmp_path: Path, filename: str, env_extra: dict[str, str]) -> tuple[int, str, str]:
    """Execute the workflow's routing script with stubs; return (rc, out, err)."""
    stubs = tmp_path / "stubs"
    stubs.mkdir()
    # The preinstalled CLI stub records its use loudly and succeeds, so any
    # routing through it is visible in the captured output. Both entry-point
    # names exist: rules-lint probes celestia-rules-lint, the others probe
    # celestia-devtools (and its --help gate lands on the same stub).
    _write_stub(stubs, "celestia-devtools", 'echo "STUB-PREINSTALLED-USED $*"\nexit 0')
    _write_stub(stubs, "celestia-rules-lint", 'echo "STUB-PREINSTALLED-USED $*"\nexit 0')
    # The git stub records clone attempts and then stops the script (set -e
    # style scripts abort here); the decision is already observable.
    _write_stub(stubs, "git", 'echo "STUB-CLONE $*"\nexit 1')
    # The python3 stub records install-attempt venv/pip calls and succeeds, so
    # the style/rules install path completes and records the pinned ref.
    _write_stub(stubs, "python3", 'echo "STUB-PYTHON3 $*"\nmkdir -p "$RUNNER_TEMP/dt-venv/bin" 2>/dev/null || true\nprintf "#!/bin/sh\\necho STUB-VENV-CLI\\n" > "$RUNNER_TEMP/dt-venv/bin/celestia-devtools" 2>/dev/null || true\nprintf "#!/bin/sh\\necho STUB-VENV-CLI\\n" > "$RUNNER_TEMP/dt-venv/bin/celestia-rules-lint" 2>/dev/null || true\nexit 0')
    github_env = tmp_path / "github_env"
    github_env.write_text("", encoding = "utf-8")
    runner_temp = tmp_path / "runner_temp"
    runner_temp.mkdir()
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    env = {**os.environ, "PATH": f"{stubs}:{os.environ['PATH']}", "DEVTOOLS_REF": "", "RUNNER_TEMP": str(runner_temp), "GITHUB_ENV": str(github_env), "GITHUB_WORKSPACE": str(workspace), "STRICT": "", **env_extra}
    proc = subprocess.run(["bash", "-c", _routing_script(filename)], env = env, capture_output = True, text = True)
    return proc.returncode, proc.stdout, proc.stderr

@pytest.mark.parametrize("filename", sorted(ROUTING_STEPS))
def test_preinstalled_lane_is_gated_on_the_master_default(filename: str) -> None:
    """The fast path must not fire for a pinned ref — that is the H14 trap."""
    script = _routing_script(filename)
    condition = re.search(r"if (.*); then", script)
    assert condition is not None, f"{filename}: routing script lost its if-condition"
    assert GUARD in condition.group(1), (f"{filename}: the preinstalled fast path is reachable for a pinned devtools-ref " f"(guard {GUARD!r} missing from the condition). A caller pinning a tag would be judged " "by the runner's own CLI — the same tree then draws opposite conclusions from " "two CLIs (H14).")

@pytest.mark.parametrize("filename", sorted(ROUTING_STEPS))
def test_pinned_ref_routes_to_a_fresh_clone_or_install(tmp_path: Path, filename: str) -> None:
    """DEVTOOLS_REF=v2026.11 must never reach the preinstalled CLI stub."""
    _rc, out, _err = _run_routing(tmp_path, filename, {"DEVTOOLS_REF": "v2026.11"})
    routing = "STUB-CLONE" if filename in ("family-versions.yml", "verify-versions.yml") else "STUB-PYTHON3"
    assert routing in out, (f"{filename} with devtools-ref=v2026.11 neither cloned nor installed at the pin; " f"output was: {out!r}")
    assert "v2026.11" in out, f"{filename}: the pinned ref did not reach the clone/install command: {out!r}"
    assert "STUB-PREINSTALLED-USED" not in out, (f"{filename} with devtools-ref=v2026.11 used the preinstalled CLI — " "the H14 regression: the pin names the CLI that must judge the tree, an " f"editable master install does not. Output: {out!r}")

@pytest.mark.parametrize("filename", sorted(ROUTING_STEPS))
def test_master_default_still_uses_the_preinstalled_cli(tmp_path: Path, filename: str) -> None:
    """The fast path survives for the master default (node-1 convention)."""
    _rc, out, _err = _run_routing(tmp_path, filename, {"DEVTOOLS_REF": "master"})
    # family-versions / verify-versions invoke the CLI right away; the
    # style-lint / rules-lint bootstrap steps only record the choice and set
    # the env var, so the routing decision is visible in their echo line.
    used = "STUB-PREINSTALLED-USED" if filename in ("family-versions.yml", "verify-versions.yml") else "using preinstalled"
    assert used in out, (f"{filename} with the master default no longer uses the preinstalled CLI; " f"the fast lane was the point of the convention. Output: {out!r}")

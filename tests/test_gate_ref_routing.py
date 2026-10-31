"""Pin the gate-CLI ref-determinism contract of the reusable workflows (H14).

*Why this exists:* the reusable gates once preferred any preinstalled
``celestia-devtools`` that "knew the command" and only cloned the caller's
``devtools-ref`` when the preinstall was missing. The 2026-10 H14 incident:
node-1's preinstall was a stale 0.14.2 snapshot whose family baselines lagged
master, so one checkout drew opposite conclusions from the preinstalled CLI
and the CLI at the caller's ref (the editable install that now tracks master
on node-1 was the remediation, not the incident). The same trap stayed open
for pinned callers afterwards: a pin names one CLI, the preinstall is another.

The contract since the fix: a caller that pins ``devtools-ref`` names the
EXACT CLI that must judge the tree. Any ref other than the master default
always runs from a fresh clone or install at that ref; the preinstalled fast
path survives only for the master default (and echoes which binary and
version ran, so drift is at least visible in the log — the version string
alone did NOT distinguish the two CLIs in the incident; the stale data did).

The tests below execute each workflow's routing script with stubbed
``git`` / ``python3`` / ``pip`` / ``rm`` / CLI entry points on PATH and
assert the routing decision AND that the pinned ref reaches the actual
clone/install command line, so a regression to "prefer preinstalled" — or a
routing that keeps the pin but fetches master anyway — flips them red.
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
ROUTING_STEPS = {"commit-msg-lint.yml": "Ensure celestia-devtools", "family-versions.yml": "Family versions", "pr-title-check.yml": "Ensure celestia-devtools", "rules-lint.yml": "Ensure celestia-rules-lint", "style-lint.yml": "Ensure celestia-devtools", "verify-versions.yml": "Verify versions"}

#: Workflows whose fallback lane clones (vs pip-installing) at the ref.
CLONE_LANE = frozenset({"family-versions.yml", "verify-versions.yml"})

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
    """Execute the workflow's routing script with stubs; return (rc, out, err).

    Everything the scripts can reach that touches the host is stubbed: the CLI
    entry points, ``git`` (records the clone and stops), ``python3`` (records
    venv creation and lays down stub ``bin/pip`` + entry-point shims so the
    pip lane actually completes), ``pip``/``pip3`` (record the install
    command), and ``rm`` (no-op logger — the real scripts ``rm -rf`` fixed
    /tmp paths that live gate jobs on the farm use, so the tests must never
    run the host rm; R2 F1).
    """
    stubs = tmp_path / "stubs"
    stubs.mkdir()
    _write_stub(stubs, "celestia-devtools", 'echo "STUB-PREINSTALLED-USED $*"\nexit 0')
    _write_stub(stubs, "celestia-rules-lint", 'echo "STUB-PREINSTALLED-USED $*"\nexit 0')
    _write_stub(stubs, "git", 'echo "STUB-CLONE $*"\nexit 0')
    _write_stub(stubs, "rm", 'echo "STUB-RM $*"\nexit 0')
    _write_stub(stubs, "pip", 'echo "STUB-PIP $*"\nexit 0')
    _write_stub(stubs, "pip3", 'echo "STUB-PIP $*"\nexit 0')
    _write_stub(stubs, "python3", 'echo "STUB-PYTHON3 $*"\nif [ "$1" = "-m" ] && [ "$2" = "venv" ]; then\n  mkdir -p "$3/bin"\n  printf \'#!/bin/sh\\necho "STUB-PIP $*"\\n\' > "$3/bin/pip"\n  chmod +x "$3/bin/pip"\n  printf \'#!/bin/sh\\necho "STUB-VENV-CLI"\\n\' > "$3/bin/celestia-devtools"\n  chmod +x "$3/bin/celestia-devtools"\n  printf \'#!/bin/sh\\necho "STUB-VENV-CLI"\\n\' > "$3/bin/celestia-rules-lint"\n  chmod +x "$3/bin/celestia-rules-lint"\nfi\nexit 0')
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

PIN_REFS = ("v2026.11", "0123456789abcdef0123456789abcdef01234567")

@pytest.mark.parametrize("ref", PIN_REFS)
@pytest.mark.parametrize("filename", sorted(ROUTING_STEPS))
def test_pinned_ref_routes_to_a_fresh_clone_or_install(tmp_path: Path, filename: str, ref: str) -> None:
    """A pinned ref (tag or SHA) must reach the clone/install COMMAND LINE, not just a log line.

    The SHA case pins the 2026-10-31 scheme ruling: SHA pins are canonical, so
    the clone lane must fetch-the-exact-object (git init/remote/fetch/checkout,
    visible as STUB-CLONE fetch lines) and the pip lane must carry @<sha> in
    the install URL.
    """
    _rc, out, _err = _run_routing(tmp_path, filename, {"DEVTOOLS_REF": ref})
    marker = "STUB-CLONE" if filename in CLONE_LANE else "STUB-PIP"
    assert any((marker in line) and (ref in line) for line in out.splitlines()), (f"{filename} with devtools-ref={ref} did not run {marker} with the pinned ref on its " f"command line — the pin must reach the actual fetch, not an echo (R2 F3). Output was: {out!r}")
    assert "STUB-PREINSTALLED-USED" not in out, (f"{filename} with devtools-ref={ref} used the preinstalled CLI — " "the H14 regression: the pin names the CLI that must judge the tree, an " f"editable master install does not. Output: {out!r}")

@pytest.mark.parametrize("filename", sorted(ROUTING_STEPS))
def test_master_default_still_uses_the_preinstalled_cli(tmp_path: Path, filename: str) -> None:
    """The fast path survives for the master default (node-1 convention)."""
    _rc, out, _err = _run_routing(tmp_path, filename, {"DEVTOOLS_REF": "master"})
    # family-versions / verify-versions invoke the CLI right away; the
    # bootstrap steps only record the choice and set the env var, so the
    # routing decision is visible in their echo line.
    used = "STUB-PREINSTALLED-USED" if filename in CLONE_LANE else "using preinstalled"
    assert used in out, (f"{filename} with the master default no longer uses the preinstalled CLI; " f"the fast lane was the point of the convention. Output: {out!r}")

@pytest.mark.parametrize("filename", sorted(CLONE_LANE))
def test_routing_tests_never_run_the_host_rm(tmp_path: Path, filename: str) -> None:
    """The clone-lane scripts rm -rf fixed /tmp paths; the stub must swallow it (R2 F1)."""
    _rc, out, _err = _run_routing(tmp_path, filename, {"DEVTOOLS_REF": "v2026.11"})
    assert "STUB-RM" in out, f"{filename}: the rm stub did not intercept the script's rm -rf (destructive host path!)"

@pytest.mark.parametrize("filename", sorted(CLONE_LANE))
def test_sha_pin_takes_the_fetch_the_object_lane(tmp_path: Path, filename: str) -> None:
    """A 40-hex ref cannot take `git clone --branch`; the script must run the
    init/remote/fetch/checkout sequence instead (scheme ruling 2026-10-31)."""
    sha = "0123456789abcdef0123456789abcdef01234567"
    _rc, out, _err = _run_routing(tmp_path, filename, {"DEVTOOLS_REF": sha})
    lines = out.splitlines()
    assert any(("STUB-CLONE" in line) and ("fetch" in line) and (sha in line) for line in lines), (f"{filename}: a SHA pin did not take the fetch-the-exact-object lane. Output: {out!r}")
    assert not any("--branch" in line for line in lines), (f"{filename}: the SHA pin wrongly took the --branch clone lane. Output: {out!r}")

@pytest.mark.parametrize("filename", sorted(set(ROUTING_STEPS) - CLONE_LANE))
def test_pip_lane_actually_executes_pip_at_the_ref(tmp_path: Path, filename: str) -> None:
    """The venv stub must lay down bin/pip so the install line truly runs (R2 F3b)."""
    _rc, out, _err = _run_routing(tmp_path, filename, {"DEVTOOLS_REF": "v2026.11"})
    assert any(("STUB-PIP" in line) and ("git+https://github.com/celestia-island/celestia-devtools.git@v2026.11" in line) for line in out.splitlines()), (f"{filename}: the pip install at the pinned ref never executed. Output: {out!r}")

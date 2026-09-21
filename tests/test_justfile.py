"""Static regression tests for the embedded common.just recipe sources.

Since the 0.7.0 de-bash doctrine, common.just recipes are LINEWISE and
shell-neutral single commands — valid under POSIX sh and Windows PowerShell
5.1 alike, with no ``[script]`` bodies, no shebangs, and no bash/ps1 helper
scripts. (``[script]`` bodies also never receive recipe parameters as shell
positionals — ``$@``/``$#``/``$n`` are always empty there, the defect behind
PR #67 — so removing them fixes that bug class by construction.) These tests
pin those invariants so the bash crutch cannot creep back in.
"""

import re
from pathlib import Path

JUST = Path(__file__).resolve().parents[1] / "src" / "celestia_devtools" / "common.just"

_HEADER_RE = re.compile(r"^([a-zA-Z_][\w-]*)((?:\s[^:]+)*):")
_ATTR_RE = re.compile(r"^\[[a-z-]+(?:\(.*\))?\]$")
_VAR_RE = re.compile(r"^[\w-]+\s*:=")
_SHEBANG_RE = re.compile(r"^\s*#!")

# The ONLY recipe allowed to mention bash: wsl-run's `bash -lc` executes
# INSIDE the Linux distro (the Linux side of the WSL boundary), never on the
# Windows host.
_BASH_ALLOWED_RECIPES = {"wsl-run"}


def _recipes() -> dict[str, dict]:
    """Parse common.just into ``name -> {attrs, body}`` for every recipe."""
    recipes: dict[str, dict] = {}
    attrs: list[str] = []
    current: str | None = None
    in_var = False
    for line in JUST.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue  # bodies may contain blank lines
        if line[0].isspace():
            if current is not None:
                recipes[current]["body"].append(line)
            continue
        # Column-0 line: comment, attribute, variable assignment, or header.
        if in_var:
            if line.startswith("}"):
                continue  # continuation of a multi-line assignment
            in_var = False
        if line.startswith("#"):
            current = None
            continue
        if _ATTR_RE.match(line):
            attrs.append(line.strip())
            continue
        if _VAR_RE.match(line):
            current = None
            attrs = []
            in_var = True
            continue
        match = _HEADER_RE.match(line)
        assert match, f"unparsed column-0 line in common.just: {line!r}"
        name = match.group(1)
        recipes[name] = {"attrs": attrs, "body": []}
        current = name
        attrs = []
    return recipes


def test_common_just_has_no_script_recipes():
    """No ``[script]`` / ``[script('bash')]`` recipes at all — interpreter-run
    script bodies are the mechanism that required Git Bash on Windows and the
    script-interceptor interop file. Logic belongs in the CLI instead."""
    scripted = {
        name: recipe["attrs"]
        for name, recipe in _recipes().items()
        if any(a.startswith("[script") for a in recipe["attrs"])
    }
    assert scripted == {}, (
        "common.just grew [script] recipes again — move the logic into a "
        f"celestia-devtools CLI subcommand (offenders: {sorted(scripted)})"
    )


def test_common_just_has_no_shebangs():
    offenders = {
        name: line
        for name, recipe in _recipes().items()
        for line in recipe["body"]
        if _SHEBANG_RE.match(line)
    }
    assert offenders == {}


def test_common_just_is_bash_free_on_the_windows_host():
    """No recipe body may invoke bash or reference .sh/.ps1 helper scripts,
    except the documented in-distro `bash -lc` of wsl-run."""
    offenders = {}
    for name, recipe in _recipes().items():
        if name in _BASH_ALLOWED_RECIPES:
            continue
        for line in recipe["body"]:
            if re.search(r"\bbash\b|\.sh\b|\.ps1\b", line):
                offenders.setdefault(name, line)
    assert offenders == {}


def test_linewise_bodies_stay_in_the_two_shell_intersection():
    """Linewise bodies run verbatim under POSIX sh AND PowerShell 5.1, so they
    must not use shell operators or device paths that exist in only one."""
    offenders = {}
    for name, recipe in _recipes().items():
        for line in recipe["body"]:
            stripped = re.sub(r"\s#.*$", "", line)
            if re.search(r"&&|\|\||\$\(|/dev/null|\bcommand -v\b", stripped):
                offenders.setdefault(name, stripped)
    assert offenders == {}

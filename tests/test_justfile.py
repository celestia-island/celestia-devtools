"""Static regression tests for the embedded common.just recipe sources.

`just` never forwards recipe parameters as shell positionals into
``[script]`` bodies — ``$@`` / ``$#`` / ``$n`` are always empty there, so a
recipe that reads them silently drops its arguments (the defect behind PR
#67: ``link-npm-siblings --status`` applied overlays and ``npm-release``
published the root dist package regardless of the packages passed). These
tests pin the recipe-source invariants so that bug class cannot return.
"""

import re
from pathlib import Path

JUST = Path(__file__).resolve().parents[1] / "src" / "celestia_devtools" / "common.just"

_HEADER_RE = re.compile(r"^([a-zA-Z_][\w-]*)((?:\s[^:]+)*):")
_ATTR_RE = re.compile(r"^\[[a-z-]+\]$")
_POSITIONAL_RE = re.compile(r"\$(?:@|[0-9#])")
_VARIADIC_RE = re.compile(r"\*(\w+)")
_VAR_RE = re.compile(r"^[\w-]+\s*:=")
_COMMENT_RE = re.compile(r"\s#.*$")


def _script_recipes() -> dict[str, tuple[list[str], list[str]]]:
    """Parse common.just into ``name -> (variadic param names, body lines)``
    for the ``[script]`` recipes only (linewise recipes do receive positionals
    and are out of scope)."""
    recipes: dict[str, tuple[list[str], list[str]]] = {}
    attrs: list[str] = []
    current: str | None = None
    in_var = False
    for line in JUST.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue  # bodies may contain blank lines
        if line[0].isspace():
            if current is not None:
                recipes[current][1].append(line)
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
        name, params = match.group(1), match.group(2)
        if "[script]" in attrs:
            recipes[name] = (_VARIADIC_RE.findall(params), [])
            current = name
        else:
            current = None
        attrs = []
    return recipes


def test_discovers_the_expected_script_recipes():
    assert {
        "_build", "dev", "dev-watch", "npm-dist", "npm-release",
        "link-npm-siblings", "vite-build", "vite-serve", "vite-dev",
    } <= set(_script_recipes())


def test_script_bodies_never_read_bare_positional_args():
    """``$@``/``$#``/``$n`` are always empty inside [script] bodies — any
    reference silently drops the recipe's arguments. Array variables such as
    ``${pkgs[@]}`` are script-local and must stay allowed."""
    offenders = {
        name: line
        for name, (_, body) in _script_recipes().items()
        for line in body
        if _POSITIONAL_RE.search(_COMMENT_RE.sub("", line))
    }
    assert offenders == {}


def test_variadic_script_params_are_interpolated():
    """Every ``*PARAM`` of a [script] recipe must reach the body via
    ``{{PARAM}}`` interpolation, never as a shell positional."""
    missing = {
        name: param
        for name, (variadic, body) in _script_recipes().items()
        for param in variadic
        if f"{{{{{param}}}}}" not in "\n".join(body)
    }
    assert missing == {}

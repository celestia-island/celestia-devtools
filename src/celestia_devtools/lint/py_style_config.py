"""Configuration loading for the custom Python style checker (py-style).

Resolution order (first hit wins):

1. --config PATH (an explicit file; accepts either top-level keys or a
   [tool.py_style] table),
2. py-style.toml in the repository root,
3. the [tool.py_style] table of pyproject.toml in the repository root,
4. built-in defaults.

The repository root is the nearest ancestor of the first scanned path that
contains a .git entry; when there is none (standalone trees, tests) the
starting directory itself is used.

Unknown keys and unknown rule ids are hard errors. Silently ignoring a typo
would turn a rule off (or on) without anyone noticing.
"""

from __future__ import annotations
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping, Sequence
from celestia_devtools.lint.py_style_rules import KNOWN_RULE_IDS

CONFIG_FILE_NAME = "py-style.toml"
PYPROJECT_FILE_NAME = "pyproject.toml"
PYPROJECT_SECTION = "py_style"

class config_error(Exception):
    """Raised when a py-style configuration file is unreadable or invalid."""

@dataclass(frozen = True)
class exempt_entry:
    """A path glob plus rule ids whose violations are ignored."""

    path: str
    rules: tuple[str, ...]
    reason: str

@dataclass
class style_config:
    """Resolved py-style configuration."""

    root: Path = field(default_factory = Path.cwd)
    source: Path | None = None
    select: tuple[str, ...] = ()
    ignore: tuple[str, ...] = ()
    exempt: tuple[exempt_entry, ...] = ()
    exclude: tuple[str, ...] = ()
    final_newline: str = "one"
    slice_colons: str = "pep8"
    docstrings: str = "allow"
    blank_line_scope: str = "all"
    allow_multiline_decorators: bool = False
    allow_type_checking_imports: bool = True
    allow_optional_imports: bool = True
    naming_allow: tuple[str, ...] = ()
    framework_callbacks: tuple[str, ...] = ()
    compound_words: tuple[str, ...] = ()
    severity: Mapping[str, str] = field(default_factory = dict)
    comment_blank_line: str = "inline-only"

_STRING_ENUM_KEYS: dict[str, frozenset[str]] = {"final_newline": frozenset({"one", "none"}), "slice_colons": frozenset({"pep8", "spaced"}), "docstrings": frozenset({"allow", "ban"}), "blank_line_scope": frozenset({"all", "nested"}), "comment_blank_line": frozenset({"inline-only", "all", "off"})}
_BOOLEAN_KEYS = frozenset({"allow_multiline_decorators", "allow_type_checking_imports", "allow_optional_imports"})
_STRING_LIST_KEYS = frozenset({"select", "ignore", "exclude", "naming_allow", "framework_callbacks", "compound_words"})
_RULE_LIST_KEYS = frozenset({"select", "ignore"})
_KNOWN_KEYS = _STRING_ENUM_KEYS.keys() | _BOOLEAN_KEYS | _STRING_LIST_KEYS | {"exempt", "severity"}

def find_repo_root(start: Path) -> Path:
    """Return the nearest ancestor of *start* containing a .git entry."""

    candidate = start.resolve()

    if candidate.is_file():
        candidate = candidate.parent

    for directory in (candidate, *candidate.parents):
        if (directory / ".git").exists():
            return directory

    return candidate

def _scanned_start(paths: Sequence[str]) -> Path:
    if not paths:
        return Path.cwd()

    first = Path(paths[0])

    if first.exists():
        return first

    if first.parent.exists():
        return first.parent

    return Path.cwd()

def _read_toml(origin: Path) -> dict[str, Any]:
    try:
        data = tomllib.loads(origin.read_text(encoding = "utf-8"))
    except OSError as exc:
        raise config_error(f"cannot read config file {origin}: {exc}") from exc
    except tomllib.TOMLDecodeError as exc:
        raise config_error(f"cannot parse config file {origin}: {exc}") from exc

    if not isinstance(data, dict):
        raise config_error(f"config file {origin} must contain a TOML table")

    return data

def _select_table(data: dict[str, Any], origin: Path) -> dict[str, Any]:
    tool = data.get("tool")

    if isinstance(tool, dict):
        section = tool.get(PYPROJECT_SECTION)

        if section is not None:
            if not isinstance(section, dict):
                raise config_error(f"[tool.{PYPROJECT_SECTION}] in {origin} must be a table")

            return section

    return data

def _expect_string_list(value: Any, key: str, origin: Path) -> tuple[str, ...]:
    if isinstance(value, str):
        raise config_error(f"{key} in {origin} must be an array of strings, not a string")

    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise config_error(f"{key} in {origin} must be an array of strings")

    return tuple(value)

def _expect_rule_ids(values: tuple[str, ...], key: str, origin: Path) -> tuple[str, ...]:
    for value in values:
        if value == "*":
            continue

        if value not in KNOWN_RULE_IDS:
            raise config_error(f"unknown rule id '{value}' in {key} of {origin}; " "see 'py-style list-rules' for the catalog")

    return values

def _parse_exempt(value: Any, origin: Path) -> tuple[exempt_entry, ...]:
    if not isinstance(value, list):
        raise config_error(f"exempt in {origin} must be an array of tables")

    entries: list[exempt_entry] = []

    for index, raw in enumerate(value):
        if not isinstance(raw, dict):
            raise config_error(f"exempt[{index}] in {origin} must be a table")

        unknown = set(raw) - {"path", "rules", "reason"}

        if unknown:
            names = ", ".join(sorted(unknown))
            raise config_error(f"unknown key(s) {names} in exempt[{index}] of {origin}")

        path_value = raw.get("path")

        if not isinstance(path_value, str) or not path_value.strip():
            raise config_error(f"exempt[{index}].path in {origin} must be a non-empty glob")

        rules_value = raw.get("rules")

        if not isinstance(rules_value, list) or not all(isinstance(item, str)for item in rules_value):
            raise config_error(f"exempt[{index}].rules in {origin} must be an array of rule ids")

        if not rules_value:
            raise config_error(f"exempt[{index}].rules in {origin} must not be empty")

        for rule in rules_value:
            if (rule != "*") and (rule not in KNOWN_RULE_IDS):
                raise config_error(f"unknown rule id '{rule}' in exempt[{index}].rules of {origin}")

        reason_value = raw.get("reason")

        if not isinstance(reason_value, str) or not reason_value.strip():
            raise config_error(f"exempt[{index}].reason in {origin} must be a non-empty string")

        entries.append(exempt_entry(path_value, tuple(rules_value), reason_value))

    return tuple(entries)

def _parse_severity(value: Any, origin: Path) -> dict[str, str]:
    if not isinstance(value, dict):
        raise config_error(f"severity in {origin} must be a table of rule id to severity")

    parsed: dict[str, str] = {}

    for rule, severity in value.items():
        if not isinstance(rule, str) or (rule not in KNOWN_RULE_IDS):
            raise config_error(f"unknown rule id '{rule}' in severity of {origin}")

        if severity not in {"error", "warning"}:
            raise config_error(f"severity of '{rule}' in {origin} must be 'error' or 'warning'")

        parsed[rule] = severity

    return parsed

def parse_config_table(table: Mapping[str, Any], origin: Path, root: Path, source: Path | None) -> style_config:
    """Validate *table* and build a StyleConfig."""

    unknown = set(table) - _KNOWN_KEYS

    if unknown:
        names = ", ".join(sorted(unknown))
        raise config_error(f"unknown key(s) {names} in {origin}")

    config = style_config(root = root, source = source)

    for key in _STRING_ENUM_KEYS:
        if key in table:
            raw = table[key]

            if not isinstance(raw, str) or (raw not in _STRING_ENUM_KEYS[key]):
                allowed = ", ".join(sorted(_STRING_ENUM_KEYS[key]))
                raise config_error(f"{key} in {origin} must be one of: {allowed}")

            setattr(config, key, raw)

    for key in _BOOLEAN_KEYS:
        if key in table:
            raw = table[key]

            if not isinstance(raw, bool):
                raise config_error(f"{key} in {origin} must be a boolean")

            setattr(config, key, raw)

    for key in _STRING_LIST_KEYS:
        if key in table:
            values = _expect_string_list(table[key], key, origin)

            if key in _RULE_LIST_KEYS:
                values = _expect_rule_ids(values, key, origin)

            setattr(config, key, values)

    if "exempt" in table:
        config.exempt = _parse_exempt(table["exempt"], origin)

    if "severity" in table:
        config.severity = _parse_severity(table["severity"], origin)

    return config

def load_config(*, paths: Sequence[str], explicit: str | None = None, disabled: bool = False) -> style_config:
    """Resolve and load the configuration for a CLI run."""

    start = _scanned_start(paths)
    root = find_repo_root(start)

    if disabled:
        return style_config(root = root, source = None)

    if explicit:
        origin = Path(explicit)

        if not origin.is_file():
            raise config_error(f"config file not found: {origin}")

        data = _read_toml(origin)
        table = _select_table(data, origin)
        return parse_config_table(table, origin, find_repo_root(origin.parent), origin)

    standalone = root / CONFIG_FILE_NAME

    if standalone.is_file():
        data = _read_toml(standalone)
        table = _select_table(data, standalone)
        return parse_config_table(table, standalone, root, standalone)

    pyproject = root / PYPROJECT_FILE_NAME

    if pyproject.is_file():
        data = _read_toml(pyproject)
        tool = data.get("tool")
        table = tool.get(PYPROJECT_SECTION) if isinstance(tool, dict) else None

        if isinstance(table, dict):
            return parse_config_table(table, pyproject, root, pyproject)

        if table is not None:
            raise config_error(f"[tool.{PYPROJECT_SECTION}] in {pyproject} must be a table")

    return style_config(root = root, source = None)

__all__ = ["CONFIG_FILE_NAME", "config_error", "exempt_entry", "PYPROJECT_SECTION", "style_config", "find_repo_root", "load_config", "parse_config_table"]

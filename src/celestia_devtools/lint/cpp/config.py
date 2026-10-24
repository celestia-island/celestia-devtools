#!/usr/bin/env python3
"""Configuration loading for the cpp-style checker.

*Why fail-closed:* a typo in a rule id or in a configuration key silently
disables a check, and a gate that is green over a non-conforming tree is worse
than no gate at all. Unknown keys, unknown rule ids, unknown exemption fields and
malformed values therefore raise ConfigError, which the CLI maps to exit code 2.

Lookup order (first hit wins):

1. --config PATH (must exist);
2. <repo root>/cpp-style.toml;
3. <repo root>/pyproject.toml, table [tool.cpp_style];
4. built-in defaults.

The repository root is the first directory at or above the working directory
that contains pyproject.toml or .git. --no-config stops after the defaults.

Accepted keys: select, ignore, exempt, exclude, platform_headers, platform_paths,
platform_types, com_interface_prefixes, allow_space_before_paren, macro_style,
allow_doxygen. A cpp-style.toml may put those keys at the top level or inside a
[tool.cpp_style] / [cpp_style] table.

Notes on the fixed defaults:

- platform_headers, platform_types and com_interface_prefixes are replaced by a
  configured list (an empty list means "nothing is restricted");
- allow_space_before_paren is additive: the built-in exemption list always stays
  and a configured list adds to it.

The built-in allow_space_before_paren list covers keyword-like constructs that
take a parenthesised operand but are not function calls: return, sizeof,
alignof, decltype, alignas, static_assert, noexcept, constexpr (including
if constexpr), operator, new, delete, __declspec, __attribute__, co_await,
co_return and co_yield. The C++ rule only asks function names and the control
keywords if/for/while/switch/catch to touch their parenthesis.
"""

from __future__ import annotations
import fnmatch
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Optional, Sequence, Tuple
from celestia_devtools.lint.cpp.rules import RULE_BY_ID

DEFAULT_PLATFORM_HEADERS: Tuple[str, ...] = ("windows.h", "winsock2.h", "ws2tcpip.h", "d3d11.h", "d3d12.h", "dxgi.h", "d2d1.h", "dwrite.h", "d3dcompiler.h", "shlobj.h", "shellapi.h", "windowsx.h")

DEFAULT_PLATFORM_TYPES: Tuple[str, ...] = ("HANDLE", "SOCKET", "HRESULT")

DEFAULT_COM_INTERFACE_PREFIXES: Tuple[str, ...] = ("IUnknown", "ID3D", "ID2D", "IDWrite", "IWIC")

DEFAULT_ALLOW_SPACE_BEFORE_PAREN: Tuple[str, ...] = (
    # Keyword-like constructs that take a parenthesised operand but are not
    # function calls: the C++ rule only requires function names and the control
    # keywords if/for/while/switch/catch to touch their parenthesis.
    "return",
    "sizeof",
    "alignof",
    "decltype",
    "alignas",
    "static_assert",
    "noexcept",
    "constexpr",
    "operator",
    "new",
    "delete",
    "__declspec",
    "__attribute__",
    "co_await",
    "co_return",
    "co_yield"
)

KNOWN_KEYS = frozenset({"select", "ignore", "exempt", "exclude", "platform_headers", "platform_paths", "platform_types", "com_interface_prefixes", "allow_space_before_paren", "macro_style", "allow_doxygen"})

_MACRO_STYLES = frozenset({"warn", "ignore"})

class ConfigError(Exception):
    """Configuration is unreadable, unknown or malformed (CLI exit code 2)."""

@dataclass(frozen = True)
class Exemption:
    """A path glob plus the rules it exempts, with a required reason."""

    pattern: str
    rules: Tuple[str, ...]
    reason: str

@dataclass
class Config:
    """Fully validated configuration with built-in defaults filled in."""

    origin: str = "built-in defaults"
    base_dir: Optional[Path] = None
    select: Optional[Tuple[str, ...]] = None
    ignore: Tuple[str, ...] = ()
    exempt: Tuple[Exemption, ...] = ()
    exclude: Tuple[str, ...] = ()
    platform_headers: Tuple[str, ...] = DEFAULT_PLATFORM_HEADERS
    platform_paths: Tuple[str, ...] = ()
    platform_types: Tuple[str, ...] = DEFAULT_PLATFORM_TYPES
    com_interface_prefixes: Tuple[str, ...] = DEFAULT_COM_INTERFACE_PREFIXES
    allow_space_before_paren: Tuple[str, ...] = ()
    macro_style: str = "warn"
    allow_doxygen: bool = False

    @property
    def effective_allow_space_before_paren(self) -> Tuple[str, ...]:
        return DEFAULT_ALLOW_SPACE_BEFORE_PAREN + self.allow_space_before_paren

def find_repo_root(cwd: Path) -> Optional[Path]:
    """First directory at or above *cwd* that looks like a repository root."""

    for candidate in (cwd, *cwd.parents):
        if (candidate / "pyproject.toml").is_file() or (candidate / ".git").exists():
            return candidate

    return None

def load_config(*, explicit: Optional[str], no_config: bool, cwd: Path) -> Config:
    """Load and validate the effective configuration for a check run."""
    cwd = Path(cwd).resolve()

    if explicit is not None:
        path = Path(explicit)

        if not path.is_absolute():
            path = cwd / path

        if not path.is_file():
            raise ConfigError("config file not found: %s" % explicit)

        return _config_from_file(path.resolve())

    if no_config:
        return Config(base_dir = cwd)

    root = find_repo_root(cwd)

    if root is not None:
        local = root / "cpp-style.toml"

        if local.is_file():
            return _config_from_file(local)

        pyproject = root / "pyproject.toml"

        if pyproject.is_file():
            data = _read_toml(pyproject)
            tool = data.get("tool")
            table = tool.get("cpp_style") if isinstance(tool, dict) else None

            if table is not None:
                if not isinstance(table, dict):
                    raise ConfigError("%s: [tool.cpp_style] must be a TOML table" % pyproject)

                return _config_from_table(table, "%s [tool.cpp_style]" % pyproject, pyproject.parent)

    return Config(base_dir = cwd)

def matches_globs(variants: Sequence[str], globs: Sequence[str]) -> bool:
    """Case-insensitive glob match of path variants against configured globs.

    Matching is deliberately liberal: a pattern also matches everything below a
    matching directory, so exclude = ["third_party"] covers every file inside
    third_party/ without requiring the trailing glob.
    """

    for raw in globs:
        pattern = raw.strip().replace("\\", "/").lower()

        while pattern.startswith("./"):
            pattern = pattern[2:]

        if not pattern:
            continue

        candidates = [pattern]

        if not pattern.endswith("/**"):
            candidates.append(pattern.rstrip("/") + "/**")

        candidates.append("**/" + pattern)

        for variant in variants:
            value = variant.replace("\\", "/").lower()

            for candidate in candidates:
                if fnmatch.fnmatchcase(value, candidate):
                    return True

    return False

def path_variants(path: Path, config: Config) -> Tuple[str, ...]:
    """Path spellings a glob can be matched against.

    A configured glob is almost always written relative to the repository root
    (where the config file lives) or relative to the invocation directory, and on
    Windows the separators differ, so both spellings plus the bare file name are
    returned.
    """
    bases = []

    if config.base_dir is not None:
        bases.append(config.base_dir)

    cwd = Path.cwd()

    if all(cwd.resolve() != base.resolve() for base in bases):
        bases.append(cwd)

    variants = []
    resolved = path.resolve()

    for base in bases:
        try:
            relative = resolved.relative_to(base.resolve())
        except (OSError, ValueError):
            continue

        variants.append(relative.as_posix())

    variants.append(path.name)
    seen = []

    for variant in variants:
        if variant and (variant not in seen):
            seen.append(variant)

    return tuple(seen)

def _read_toml(path: Path) -> dict:
    try:
        text = path.read_text(encoding = "utf-8")
    except OSError as exc:
        raise ConfigError("cannot read %s: %s" % (path, exc)) from exc
    except UnicodeDecodeError as exc:
        raise ConfigError("%s is not valid UTF-8: %s" % (path, exc)) from exc

    try:
        data = tomllib.loads(text)
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError("cannot parse %s: %s" % (path, exc)) from exc

    if not isinstance(data, dict):
        raise ConfigError("%s: expected a TOML table at the top level" % path)

    return data

def _config_from_file(path: Path) -> Config:
    data = _read_toml(path)
    table = data
    tool = data.get("tool")

    if isinstance(tool, dict) and isinstance(tool.get("cpp_style"), dict):
        table = tool["cpp_style"]
    elif isinstance(data.get("cpp_style"), dict):
        table = data["cpp_style"]

    return _config_from_table(table, str(path), path.parent)

def _config_from_table(table: dict, origin: str, base_dir: Path) -> Config:
    unknown = sorted(str(key) for key in table if key not in KNOWN_KEYS)

    if unknown:
        raise ConfigError("%s: unknown configuration key(s) %s (known: %s)" % (origin, ", ".join(repr(key)for key in unknown), ", ".join(sorted(KNOWN_KEYS))))

    config = Config(origin = origin, base_dir = base_dir)

    if "select" in table:
        config.select = _rule_list(table["select"], "select", origin)

        if not config.select:
            raise ConfigError("%s: select names no rule; remove it or use ignore to disable rules" % origin)

    if "ignore" in table:
        config.ignore = _rule_list(table["ignore"], "ignore", origin)

    if "exempt" in table:
        config.exempt = _exemptions(table["exempt"], origin)

    if "exclude" in table:
        config.exclude = _string_list(table["exclude"], "exclude", origin)

    if "platform_headers" in table:
        config.platform_headers = _string_list(table["platform_headers"], "platform_headers", origin)

    if "platform_paths" in table:
        config.platform_paths = _string_list(table["platform_paths"], "platform_paths", origin)

    if "platform_types" in table:
        config.platform_types = _string_list(table["platform_types"], "platform_types", origin)

    if "com_interface_prefixes" in table:
        config.com_interface_prefixes = _string_list(table["com_interface_prefixes"], "com_interface_prefixes", origin)

    if "allow_space_before_paren" in table:
        config.allow_space_before_paren = _string_list(table["allow_space_before_paren"], "allow_space_before_paren", origin)

    if "macro_style" in table:
        macro_style = table["macro_style"]

        if not isinstance(macro_style, str) or (macro_style not in _MACRO_STYLES):
            raise ConfigError("%s: macro_style must be one of %s" % (origin, ", ".join(sorted(_MACRO_STYLES))))

        config.macro_style = macro_style

    if "allow_doxygen" in table:
        allow_doxygen = table["allow_doxygen"]

        if not isinstance(allow_doxygen, bool):
            raise ConfigError("%s: allow_doxygen must be a boolean" % origin)

        config.allow_doxygen = allow_doxygen

    return config

def _string_list(value: object, key: str, origin: str) -> Tuple[str, ...]:
    if isinstance(value, str):
        items = [value]
    elif isinstance(value, list):
        items = []

        for item in value:
            if not isinstance(item, str):
                raise ConfigError("%s: %s must contain strings only" % (origin, key))

            items.append(item)
    else:
        raise ConfigError("%s: %s must be a string or a list of strings" % (origin, key))

    result = []

    for item in items:
        if not item.strip():
            raise ConfigError("%s: %s contains an empty string" % (origin, key))

        result.append(item.strip())

    return tuple(result)

def _rule_list(value: object, key: str, origin: str) -> Tuple[str, ...]:
    rules = _string_list(value, key, origin)

    for rule in rules:
        if (rule != "*") and (rule not in RULE_BY_ID):
            raise ConfigError("%s: unknown rule id %r in %s (run 'cpp-style list-rules' for the catalogue)" % (origin, rule, key))

    return rules

def _exemptions(value: object, origin: str) -> Tuple[Exemption, ...]:
    if not isinstance(value, list):
        raise ConfigError("%s: exempt must be a list of tables" % origin)

    entries = []

    for index, item in enumerate(value):
        where = "exempt[%d]" % index

        if not isinstance(item, dict):
            raise ConfigError("%s: %s must be a table" % (origin, where))

        unknown = sorted(str(key) for key in item if key not in {"path", "rules", "reason"})

        if unknown:
            raise ConfigError("%s: %s has unknown field(s) %s (known: path, rules, reason)" % (origin, where, ", ".join(repr(key)for key in unknown)))

        pattern = item.get("path")

        if not isinstance(pattern, str) or not pattern.strip():
            raise ConfigError("%s: %s.path must be a non-empty glob" % (origin, where))

        rules = _string_list(item.get("rules", ["*"]), where + ".rules", origin)

        for rule in rules:
            if (rule != "*") and (rule not in RULE_BY_ID):
                raise ConfigError("%s: unknown rule id %r in %s.rules" % (origin, rule, where))

        reason = item.get("reason")

        if not isinstance(reason, str) or not reason.strip():
            raise ConfigError("%s: %s.reason is required; an exemption without a reason is not allowed" % (origin, where))

        entries.append(Exemption(pattern.strip(), rules, reason.strip()))

    return tuple(entries)

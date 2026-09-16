#!/usr/bin/env python3
"""Lint a layered workspace-rules tree for structural consistency.

*Why this exists:* workspace rules get split out of one monolithic ``AGENTS.md``
into several load surfaces — an always-on core, directory-scoped ``AGENTS.md``
files, on-demand ledgers, and on-demand skills. That split multiplies the ways a
rule can be silently lost or start to drift:

- a pointer can name a skill that does not exist, or whose body is empty;
- a ``§x.y`` cross-reference can outlive the heading it pointed at;
- **a skill whose YAML frontmatter does not parse is dropped from the catalog
  with no visible error.** The pointer still says the rule is loaded, but nothing
  loads it. This is not hypothetical: four skill descriptions containing an
  unquoted ``": "`` made the whole skill unparseable, and the agent silently lost
  those rules until the catalog was inspected by hand;
- the same sentence can drift into two surfaces, which is what the split exists
  to prevent;
- a credential or private address can re-enter a surface that is injected into
  every agent, subagent, and model request.

*Layout convention* (all paths overridable by flag):

- ``<root>/AGENTS.md`` — the always-on core;
- ``<root>/<dir>/AGENTS.md`` — directory-scoped rules, loaded by path;
- ``<root>/_ledger/*.md`` — facts that expire, read on demand;
- ``<root>/.agents/skills/<name>/SKILL.md`` — rules loaded by task nature.

*Not* checked here: whether the prose is *right*. This is a structural gate, and
structural gates do not catch a rule that is well-formed and wrong.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

try:  # PyYAML is already a hard dependency of this package.
    import yaml
except ImportError:  # pragma: no cover - exercised only in a broken env
    yaml = None  # type: ignore[assignment]


CORE_NAME = "AGENTS.md"
SKILL_RELPATH = Path(".agents") / "skills"
LEDGER_RELPATH = "_ledger"

# A rule heading is `## 3. Title` / `### 3.4 Title`. Numbering is what `§` refs use.
HEADING = re.compile(r"^(#{2,4})\s+(\d+(?:\.\d+)*)\.?\s+(.*)$")
# `§1`, `§7.2.4`, `§12.2` — optionally followed by a subsection tail like `.1`.
SECTION_REF = re.compile(r"§(\d+(?:\.\d+)*)")

POINTER_SKILL = re.compile(r"全文已移至技能\*\*\s*`([a-z0-9][a-z0-9-]*)`")
POINTER_DIR = re.compile(r"全文已移至目录层\*\*\s*`([^`]+)`")
POINTER_MOVED = re.compile(r"已移出\*\*|已移至技能|已移至目录层")

# Credential-shaped content. Deliberately generic: a workspace-specific password
# literal must never be committed to this (public) repository, so the scanner
# matches shapes, not known values.
SECRET_PATTERNS: Sequence[Tuple[str, re.Pattern]] = (
    ("private-ipv4", re.compile(r"(?<![\d.])10\.\d{1,3}\.\d{1,3}\.\d{1,3}(?![\d.])")),
    ("private-ipv4", re.compile(r"(?<![\d.])192\.168\.\d{1,3}\.\d{1,3}(?![\d.])")),
    ("private-ipv4", re.compile(r"(?<![\d.])172\.(?:1[6-9]|2\d|3[01])\.\d{1,3}\.\d{1,3}(?![\d.])")),
    ("long-hex-secret", re.compile(r"(?<![0-9a-fA-F])[0-9a-f]{32,}(?![0-9a-fA-F])")),
    ("inline-password", re.compile(r"(?i)\b(?:password|passwd|secret|token|api[_-]?key)\b\s*[=:]\s*[\"']([^\"'\s]{8,})[\"']")),
)

# Placeholders and bare example words are not credentials. Without this filter the
# inline pattern fires on documentation (`password: "Passcode"`,
# `SSH_PASS="<your-password>"`), and a gate that cries wolf on every example stops
# being read.
PLACEHOLDER = re.compile(
    r"(?i)^(?:"
    r"<[^>]*>"
    r"|change[_-]?me|placeholder|example|dummy|redacted|none|null|todo|password|passwd"
    r"|secret|token|api[_-]?key"
    r"|x{3,}|\*{3,}|\.{3,}|-{3,}"
    r"|\$\{?[A-Za-z_][A-Za-z0-9_]*\}?"
    r"|%[sd]"
    r")$"
)
# A single alphabetic word of at most 12 characters reads as an example; real
# credentials carry entropy (digits, symbols or mixed classes). Longer alphabetic
# runs — passphrases — still report.
WORD_EXAMPLE = re.compile(r"^[A-Za-z]{1,12}$")


def is_placeholder(value: str) -> bool:
    return bool(PLACEHOLDER.match(value) or WORD_EXAMPLE.match(value))

MIN_SKILL_BODY_LINES = 5
MIN_DUP_LINE_CHARS = 30


@dataclass
class Finding:
    path: Path
    line: int
    rule: str
    message: str
    severity: str = "error"

    def render(self, root: Path) -> str:
        try:
            shown = self.path.relative_to(root)
        except ValueError:
            shown = self.path
        return "rules-lint: %s:%d: %s: %s" % (shown, self.line, self.rule, self.message)


@dataclass
class Context:
    root: Path
    core: Path
    findings: List[Finding] = field(default_factory=list)
    checked: int = 0

    def report(self, path: Path, line: int, rule: str, message: str, severity: str = "error") -> None:
        self.findings.append(Finding(path, line, rule, message, severity))


def _read(path: Path) -> List[str]:
    return path.read_text(encoding="utf-8").split("\n")


def injected_surfaces(root: Path) -> List[Path]:
    """Every file that gets injected into agent context: core, per-dir, skills, tools."""
    out: List[Path] = []
    core = root / CORE_NAME
    if core.is_file():
        out.append(core)
    out.extend(sorted(p for p in root.glob("*/%s" % CORE_NAME) if p.is_file()))
    out.extend(sorted((root / SKILL_RELPATH).glob("*/SKILL.md")))
    return out


def skill_bodies(root: Path) -> Dict[str, Path]:
    """Map skill name -> SKILL.md path."""
    out: Dict[str, Path] = {}
    for path in sorted((root / SKILL_RELPATH).glob("*/SKILL.md")):
        out[path.parent.name] = path
    return out


def split_frontmatter(text: str) -> Optional[Tuple[str, str]]:
    if not text.startswith("---"):
        return None
    parts = text.split("---\n", 2)
    if len(parts) < 3:
        return None
    return parts[1], parts[2]


def body_line_count(body: str) -> int:
    return sum(1 for line in body.split("\n") if line.strip())


def skill_body_lines(path: Path) -> int:
    """Non-blank body lines, or 0 when the file has no usable frontmatter fence.

    Callers use this as a "does the target actually carry the rule" test, so a
    missing fence must read as empty rather than raise.
    """
    split = split_frontmatter(path.read_text(encoding="utf-8"))
    if split is None:
        return 0
    return body_line_count(split[1])


# --------------------------------------------------------------------------- rules


def check_skill_frontmatter(ctx: Context) -> None:
    if yaml is None:
        ctx.report(ctx.root / SKILL_RELPATH, 0, "skill-frontmatter", "PyYAML unavailable; cannot validate skills")
        return
    for name, path in skill_bodies(ctx.root).items():
        ctx.checked += 1
        text = path.read_text(encoding="utf-8")
        split = split_frontmatter(text)
        if split is None:
            ctx.report(path, 1, "skill-frontmatter", "missing or malformed frontmatter fence")
            continue
        raw, body = split
        try:
            meta = yaml.safe_load(raw)
        except yaml.YAMLError as exc:
            first = str(exc).split("\n")[0]
            ctx.report(
                path,
                1,
                "skill-frontmatter",
                "frontmatter does not parse, so this skill is silently dropped from the "
                "catalog (hint: quote scalars containing ': '): %s" % first,
            )
            continue
        if not isinstance(meta, dict):
            ctx.report(path, 1, "skill-frontmatter", "frontmatter is not a mapping")
            continue
        for key in ("name", "description"):
            value = meta.get(key)
            if not isinstance(value, str) or not value.strip():
                ctx.report(path, 1, "skill-frontmatter", "frontmatter field %r is missing or empty" % key)
        declared = meta.get("name")
        if isinstance(declared, str) and declared.strip() and declared.strip() != name:
            ctx.report(
                path,
                1,
                "skill-frontmatter",
                "frontmatter name %r does not match its directory %r" % (declared.strip(), name),
            )
        if body_line_count(body) < MIN_SKILL_BODY_LINES:
            ctx.report(
                path,
                1,
                "skill-body",
                "body has %d non-blank line(s), expected >= %d — a pointer that resolves to an "
                "empty skill loses the rule" % (body_line_count(body), MIN_SKILL_BODY_LINES),
            )


def check_pointers(ctx: Context) -> None:
    """Every 'moved out' pointer must resolve to a non-empty target."""
    if not ctx.core.is_file():
        ctx.report(ctx.root, 0, "core-missing", "no %s at the rules root" % CORE_NAME)
        return
    lines = _read(ctx.core)
    skills = skill_bodies(ctx.root)
    for idx, line in enumerate(lines, 1):
        if not POINTER_MOVED.search(line):
            continue
        ctx.checked += 1
        match = POINTER_SKILL.search(line)
        if match:
            name = match.group(1)
            target = skills.get(name)
            if target is None:
                ctx.report(ctx.core, idx, "pointer-target", "pointer names skill %r, which does not exist" % name)
            elif skill_body_lines(target) < MIN_SKILL_BODY_LINES:
                ctx.report(ctx.core, idx, "pointer-target", "pointer names skill %r, whose body is empty" % name)
            continue
        match = POINTER_DIR.search(line)
        if match:
            rel = match.group(1).strip()
            target = (ctx.core.parent / rel).resolve()
            if not target.is_file():
                ctx.report(ctx.core, idx, "pointer-target", "pointer names %r, which does not exist" % rel)
            elif not any(line.strip() for line in _read(target)):
                ctx.report(ctx.core, idx, "pointer-target", "pointer names %r, which is empty" % rel)


def check_dead_references(ctx: Context) -> None:
    """`§x.y` inside the core must resolve to a heading that still exists."""
    if not ctx.core.is_file():
        return
    lines = _read(ctx.core)
    numbers = set()
    for line in lines:
        match = HEADING.match(line.strip())
        if match:
            numbers.add(match.group(2))
    # Sub-section references like `§7.2.4` point at an item inside `### 7.2`; accept a
    # prefix of any known heading number.
    known = set()
    for num in numbers:
        parts = num.split(".")
        for i in range(1, len(parts) + 1):
            known.add(".".join(parts[:i]))
    for idx, line in enumerate(lines, 1):
        if line.lstrip().startswith("#"):
            continue
        for ref in SECTION_REF.findall(line):
            ctx.checked += 1
            if ref == "S":
                continue
            # `§7.2.4` addresses item 4 inside `### 7.2`; accept any proper prefix that
            # names a real heading, so only genuinely renamed/removed sections fail.
            parts = ref.split(".")
            if any(".".join(parts[:i]) in known for i in range(1, len(parts) + 1)):
                continue
            ctx.report(
                ctx.core,
                idx,
                "dead-reference",
                "§%s does not resolve to any heading in this file (renamed or removed section?)" % ref,
                severity="warning",
            )


def check_ledger_schema(ctx: Context) -> None:
    ledger_dir = ctx.root / LEDGER_RELPATH
    if not ledger_dir.is_dir():
        return
    for path in sorted(ledger_dir.glob("*.md")):
        ctx.checked += 1
        lines = [line for line in _read(path)]
        nonblank = [line for line in lines if line.strip()]
        if not nonblank or not nonblank[0].startswith("# "):
            ctx.report(path, 1, "ledger-schema", "first non-blank line must be an H1 title")
        if not any(line.startswith("## ") for line in lines):
            ctx.report(path, 1, "ledger-schema", "no H2 section; a ledger with no sections carries nothing")
        if not any(line.lstrip().startswith(">") for line in lines):
            ctx.report(
                path,
                1,
                "ledger-schema",
                "no provenance blockquote; add one stating that this is a ledger, not rules, "
                "and how it is maintained",
            )


def check_duplication(ctx: Context, allowlist: Iterable[str] = ()) -> None:
    """A rule sentence should live on exactly one surface."""
    seen: Dict[str, Tuple[Path, int]] = {}
    allowed = tuple(allowlist)
    for path in injected_surfaces(ctx.root):
        for idx, raw in enumerate(_read(path), 1):
            line = " ".join(raw.split())
            if len(line) < MIN_DUP_LINE_CHARS:
                continue
            if line.startswith((">", "|", "#", "-", "*", "1.", "2.", "3.")):
                continue
            if any(token and token in line for token in allowed):
                continue
            if line in seen:
                first_path, first_line = seen[line]
                ctx.report(
                    path,
                    idx,
                    "duplicate-rule",
                    "line also appears at %s:%d — one rule, one home"
                    % (first_path.relative_to(ctx.root), first_line),
                    severity="warning",
                )
            else:
                seen[line] = (path, idx)


def check_secrets(ctx: Context) -> None:
    for path in injected_surfaces(ctx.root):
        for idx, line in enumerate(_read(path), 1):
            for label, pattern in SECRET_PATTERNS:
                match = pattern.search(line)
                if not match:
                    continue
                if match.groups() and is_placeholder(match.group(1)):
                    continue
                ctx.checked += 1
                ctx.report(
                    path,
                    idx,
                    "secret-in-injected-surface",
                    "%s-shaped value in a surface injected into every agent and model request"
                    % label,
                )


RULES = (
    ("skill-frontmatter", check_skill_frontmatter),
    ("pointer-target", check_pointers),
    ("dead-reference", check_dead_references),
    ("ledger-schema", check_ledger_schema),
    ("secret-in-injected-surface", check_secrets),
    ("duplicate-rule", check_duplication),
)


def find_root(start: Path) -> Optional[Path]:
    for candidate in [start, *start.parents]:
        if (candidate / CORE_NAME).is_file():
            return candidate
    return None


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        prog="celestia-rules-lint",
        description="Lint a layered workspace-rules tree (core AGENTS.md + ledgers + skills).",
    )
    parser.add_argument("root", nargs="?", default=".", help="rules root (default: search upward for AGENTS.md)")
    parser.add_argument("--json", action="store_true", help="emit findings as JSON")
    parser.add_argument("--strict", action="store_true", help="treat warnings as failures")
    parser.add_argument("--disable", action="append", default=[], metavar="RULE", help="skip a rule (repeatable)")
    parser.add_argument("--allow-duplicate", action="append", default=[], metavar="SUBSTRING",
                        help="substring whose duplicated lines are tolerated (repeatable)")
    args = parser.parse_args(argv)

    root = Path(args.root).resolve()
    if not (root / CORE_NAME).is_file():
        found = find_root(root)
        if found is None:
            print("rules-lint: no %s found at or above %s" % (CORE_NAME, root), file=sys.stderr)
            return 2
        root = found

    ctx = Context(root=root, core=root / CORE_NAME)
    disabled = set(args.disable)
    for name, fn in RULES:
        if name in disabled:
            continue
        if name == "duplicate-rule":
            fn(ctx, args.allow_duplicate)  # type: ignore[call-arg]
        else:
            fn(ctx)

    errors = [f for f in ctx.findings if f.severity == "error"]
    warnings = [f for f in ctx.findings if f.severity != "error"]

    if args.json:
        print(json.dumps(
            {
                "root": str(root),
                "checked": ctx.checked,
                "errors": [{"path": f.render(root), "rule": f.rule, "message": f.message} for f in errors],
                "warnings": [{"path": f.render(root), "rule": f.rule, "message": f.message} for f in warnings],
            },
            ensure_ascii=False,
            indent=2,
        ))
    else:
        for finding in ctx.findings:
            print(finding.render(root), file=sys.stderr)
        print(
            "rules-lint: %d check(s), %d error(s), %d warning(s) — %s"
            % (ctx.checked, len(errors), len(warnings), root),
            file=sys.stderr,
        )

    if errors:
        return 1
    if args.strict and warnings:
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

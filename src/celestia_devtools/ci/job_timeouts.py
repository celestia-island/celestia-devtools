#!/usr/bin/env python3
"""Audit and bulk-fill ``timeout-minutes`` on GitHub Actions jobs.

Background
==========

The ``celestia-island`` org runs its self-hosted CI on only four 8 vCPU / 6 GB VMs.
Measured on 2026-09-15: once a ``cargo test`` job's process entered the kernel ``D``
state (uninterruptible), the GitHub-side job stayed ``in_progress`` and the runner
stayed ``busy`` forever, silently eating 25% of the farm's capacity. GitHub's default
job timeout is 360 minutes and **cannot** stop such a process, so every job must carry
an explicit ``timeout-minutes``.

Subcommands
===========

``celestia-job-timeouts audit [PATH ...] [--json] [--include-hosted] [--include-callers] [--policy-override J=MIN]``
    List every job with its ``runs-on`` and existing ``timeout-minutes``, suggest a
    value, and print a summary.

``celestia-job-timeouts fix [PATH ...] [--dry-run] [--include-hosted] [--include-callers] [--policy-override J=MIN]``
    Insert a ``timeout-minutes: <value>`` line into every job that lacks one.

``PATH`` defaults to the current directory; each PATH is scanned for
``<PATH>/.github/workflows/{*.yml,*.yaml}`` and a missing directory is skipped silently.

Exit codes: ``0`` success (including "nothing to change") | ``1`` jobs still missing a
timeout (**audit only**) | ``2`` usage error or parse failure.

Design constraints
==================

* **Everything else stays byte-identical**: only whole lines are added and the file is
  never rewritten with ``yaml.dump``. This module therefore does **not** depend on
  PyYAML (it is not among ``pyproject.toml``'s dependencies either) and scans plain text
  instead. A side benefit: the top-level ``on:`` key cannot be turned into a boolean by
  PyYAML and confuse the ``jobs`` detection.
* **Insertion point**: immediately after that job's ``runs-on:`` entry. For a multi-line
  value (continuation lines, flow collection or block scalar) it goes after the **last
  continuation line** — mechanically inserting directly below ``runs-on:`` would emit
  invalid YAML. A caller job (``uses:`` only, no ``runs-on:``) gets the line after its
  ``uses:`` entry. The inserted line's indentation matches the anchor line's leading
  whitespace.
* **Idempotent**: a job that already has ``timeout-minutes`` is never touched, so a
  second run changes nothing.
* **Self-hosted by default**: only jobs whose ``runs-on`` contains ``self-hosted`` are
  protected (protecting the self-hosted farm is this tool's whole purpose). GitHub-hosted
  jobs need an explicit ``--include-hosted``: hosted macOS/Windows matrix jobs have
  legitimate 300+ minute samples, so applying one policy value to them would kill them.
* **Caller jobs need an explicit ``--include-callers``**: a ``jobs.<id>.uses`` job has no
  ``runs-on`` of its own; its runner is decided by the **called** workflow, which
  normally lives in another repository, so it cannot be classified offline. This org
  contains both kinds — ``celestia-devtools``' ``commit-msg-lint`` / ``pr-title-check``
  run on ``ubuntu-latest`` (the org's hosted lint exceptions), while ``verify-versions``
  / ``p0-gate`` run on ``[self-hosted, linux, x64, local]``. They are therefore neither
  counted as self-hosted nor as hosted by default: they form a separate class and are
  flagged with a ``note``.
Boundaries worth knowing (both measured, 2026-09-15):

* The symlink guard is anchored on each ``PATH`` argument, not on the git root: a workflow
  directory that resolves outside the path you passed is refused, even if it still lives
  inside the repository.
* A missing PyYAML aborts the test session during collection (``Interrupted: 1 error during
  collection``) instead of silently skipping the "the insertion is still valid YAML" checks.
"""


from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Tuple, Union

# ---------------------------------------------------------------------------
# 策略表
# ---------------------------------------------------------------------------

# 第 1–5 条：按 job **名称** 匹配（大小写不敏感），顺序即优先级，先命中者胜。
# 依据（自托管实测，run id 可复核）：cargo 类最长 3h50m（entelecheia / Code Checks,
# run 34770115181），前端与其他类最高 35–67m，lint 类最高 35m（hikari Lint）。
_NAME_RULES: Tuple[Tuple[Tuple[str, ...], int], ...] = (
    (("bench",), 360),
    (("fuzz",), 180),
    (("coverage",), 180),
    (("install", "e2e", "integration", "smoke", "drill", "docker"), 180),
    (("release", "publish", "deploy"), 120),
)

# 第 6 条：job 体（含 steps）里出现 cargo —— 编译/测试类，自托管实测最长 3h50m，
# 取 300 留余量。
_BODY_CARGO_KEYWORD = "cargo"
_BODY_CARGO_TIMEOUT = 300

# 第 7 条：job 体里出现前端工具链。
_BODY_JS_KEYWORDS: Tuple[str, ...] = ("pnpm", "npm", "yarn", "vite", "vue-tsc")
_BODY_JS_TIMEOUT = 120

# 第 8 条：轻量作业（只看名称；前置的 cargo / 前端规则优先）。lint 类实测最高 35m，
# 取 60 留余量——早期草案给的 20 分钟会误杀 hikari 的 Lint（实测 24–35m）。
_LIGHT_NAME_KEYWORDS: Tuple[str, ...] = (
    "fmt", "format", "lint", "title", "secrets", "docs", "audit",
    "deny", "markdown", "i18n", "sign", "verify", "sync",
)
_LIGHT_TIMEOUT = 60

# 第 9 条：兜底。
_DEFAULT_TIMEOUT = 120

# 自托管判定的关键字（出现在 runs-on 里即视为自托管）。
_SELF_HOSTED_LABEL = "self-hosted"


def suggest(
    job_name: str,
    runs_on: Union[str, Sequence[str], None],
    job_body_text: str,
) -> int:
    """Return the suggested ``timeout-minutes`` for a job.

    Match order (job names are case-insensitive; *job_body_text* is that job's own raw YAML
    text and **excludes** every other job in the same file)::

        1. name contains bench                                             -> 360
        2. name contains fuzz                                              -> 180
        3. name contains coverage                                          -> 180
        4. name contains install / e2e / integration / smoke / drill /
           docker                                                          -> 180
        5. name contains release / publish / deploy                        -> 120
        6. job body contains cargo                                         -> 300
        7. job body contains pnpm / npm / yarn / vite / vue-tsc            -> 120
        8. name contains fmt / format / lint / title / secrets / docs /
           audit / deny / markdown / i18n / sign / verify / sync           -> 60
        9. otherwise                                                       -> 120

    *runs_on* takes no part in the decision today; it stays in the signature so that callers
    and tests can pass the whole context at once, and so a future per-runner policy needs no
    signature change.
    """
    name = (job_name or "").lower()
    body = (job_body_text or "").lower()

    for keywords, minutes in _NAME_RULES:
        if any(keyword in name for keyword in keywords):
            return minutes

    if _BODY_CARGO_KEYWORD in body:
        return _BODY_CARGO_TIMEOUT

    if any(keyword in body for keyword in _BODY_JS_KEYWORDS):
        return _BODY_JS_TIMEOUT

    if any(keyword in name for keyword in _LIGHT_NAME_KEYWORDS):
        return _LIGHT_TIMEOUT

    return _DEFAULT_TIMEOUT


# ---------------------------------------------------------------------------
# 纯文本扫描
# ---------------------------------------------------------------------------

class WorkflowParseError(Exception):
    """Workflow text that cannot be parsed safely (tab indentation, flow mapping, symlink escape)."""


# 键行：``key:`` 或 ``key: value``。key 允许被单/双引号包裹；取值到**第一个**冒号为止
# （这样 ``run: curl https://...`` 的 key 仍是 ``run``）。
_KEY_RE = re.compile(
    r'^(?P<key>"[^"]*"|\'[^\']*\'|[^:\s][^:]*?)\s*:(?P<rest>.*)$',
)

# 顶层的 ``jobs:``（GitHub 要求顶层键从第 0 列开始；容忍文件开头的 UTF-8 BOM）。
_JOBS_LINE_RE = re.compile(r"^\ufeff?jobs\s*:\s*(?:#.*)?$")

# 块标量指示符：``|`` / ``>`` 与可选修饰（``|-`` ``>+`` ``|2`` ``|2-``）。
_BLOCK_SCALAR_RE = re.compile(r"^[|>][0-9+-]*$")


def _chomp(line: str) -> str:
    """Strip the trailing ``\n`` / ``\r\n`` and return the line content (see :func:`_eol`)."""
    if line.endswith("\n"):
        line = line[:-1]
    if line.endswith("\r"):
        line = line[:-1]
    return line


def _eol(line: str) -> str:
    """Return this line's ending: ``"\r\n"`` / ``"\n"`` / ``""`` (file ends without a newline)."""
    if line.endswith("\r\n"):
        return "\r\n"
    if line.endswith("\n"):
        return "\n"
    return ""


def _split_lines(text: str) -> List[str]:
    """Split on ``\n`` only, keeping each line ending so the rejoin is byte-identical.

    ``str.splitlines()`` is deliberately not used: it also splits on ``\v`` / ``\f`` /
    ``\u2028`` and would break the "everything else stays byte-identical" promise.
    """
    parts = text.split("\n")
    lines = [part + "\n" for part in parts[:-1]]
    if parts[-1] != "":
        lines.append(parts[-1])  # 文件末尾无换行
    return lines


def _leading_ws(content: str) -> str:
    """Return the leading whitespace (spaces and tabs) of a line."""
    return content[: len(content) - len(content.lstrip(" \t"))]


def _indent_of(content: str) -> Optional[int]:
    """Return the indentation width of a line, or ``None`` for a blank line."""
    stripped = content.lstrip(" ")
    if stripped == "":
        return None
    return len(content) - len(stripped)


def _is_blank_or_comment(content: str) -> bool:
    """True for a blank line or a whole-line comment (YAML comments never end a job block)."""
    stripped = content.strip()
    return stripped == "" or stripped.startswith("#")


def _parse_key(content: str) -> Optional[Tuple[str, str]]:
    """Split ``key: rest`` into ``(key, rest)``, or return ``None`` when it is not a key line.

    *key* is unquoted (``"test":`` becomes ``test``). *rest* is stripped and has its trailing
    comment removed (``#`` starts a comment only when preceded by whitespace), so
    ``runs-on: # note`` is correctly read as a multi-line entry whose value follows.
    """
    match = _KEY_RE.match(content)
    if match is None:
        return None
    key = match.group("key").strip()
    if len(key) >= 2 and key[0] == key[-1] and key[0] in "\"'":
        key = key[1:-1]
    return key, _strip_trailing_comment(match.group("rest"))


def _strip_trailing_comment(value: str) -> str:
    """Drop a YAML trailing comment: ``#`` only starts one when preceded by whitespace.

    The ``#`` in ``uses: foo#bar`` belongs to the value and must be kept; the one in
    ``runs-on: x # note`` must not.
    """
    match = re.search(r"\s#", value)
    if match is not None:
        value = value[: match.start()]
    return value.strip()


def _has_inline_value(rest: str) -> bool:
    """Whether the key line actually carries a value (a bare comment does not count)."""
    return rest != "" and not rest.startswith("#")


def _is_block_scalar_indicator(value: str) -> bool:
    """``|`` / ``>`` plus modifiers (``|-`` / ``>+`` / ``|2`` / ``|2-``): the body is on following lines."""
    return bool(_BLOCK_SCALAR_RE.match(value))


def _flow_collection_unclosed(value: str) -> bool:
    """Whether the value holds an unclosed flow collection (``[`` / ``{``) or an unclosed quote.

    YAML allows flow collections to span lines, e.g.::

        runs-on: [self-hosted,
                  linux]

    Those continuation lines belong to the ``runs-on`` entry. Ignoring them would place the
    insertion inside the collection and rewrite the runner label set into
    ``['self-hosted', {'timeout-minutes': '120 linux'}]``.
    """
    depth = 0
    quote = ""
    for char in value:
        if quote:
            if char == quote:
                quote = ""
            continue
        if char in "\"'":
            quote = char
        elif char in "[{":
            depth += 1
        elif char in "]}":
            depth -= 1
    return depth > 0 or quote != ""


def _block_scalar_body_lines(lines: Sequence[str]) -> set:
    """Mark the line indices that are **bodies** of block scalars (``|`` / ``>``).

    Those lines are content, not structure: they must not take part in key-line detection
    (a ``run: |`` body may legitimately contain text such as ``timeout-minutes:``), and they
    must not be tab-checked — a tab inside such a body is legal content (a heredoc or
    Makefile fragment), and rejecting it as indentation would stall the whole batch.
    """
    opaque = set()
    total = len(lines)
    index = 0
    while index < total:
        content = _chomp(lines[index])
        if _is_blank_or_comment(content):
            index += 1
            continue
        indent = _indent_of(content)
        parsed = _parse_key(content[indent:]) if indent is not None else None
        if parsed is None or not _is_block_scalar_indicator(parsed[1]):
            index += 1
            continue

        # 正文 = 后续所有「空行 或 缩进 > 键缩进」的行。
        last = index
        cursor = index + 1
        while cursor < total:
            body = _chomp(lines[cursor])
            if body.strip() == "":
                cursor += 1
                continue
            body_indent = _indent_of(body)
            if body_indent is not None and body_indent > indent:
                last = cursor
                cursor += 1
                continue
            break
        opaque.update(range(index + 1, last + 1))
        index = cursor if cursor > index else index + 1
    return opaque


def _reject_tab_indentation(
    lines: Sequence[str], path: Path, opaque: Optional[set] = None,
) -> None:
    """Fail on a tab in the leading whitespace: the indent width is unknowable, so do not guess.

    Tabs inside *opaque* (block scalar body) lines are content rather than indentation and
    are skipped.
    """
    skip = opaque or set()
    for number, raw in enumerate(lines, 1):
        if (number - 1) in skip:
            continue
        content = _chomp(raw)
        if "\t" in _leading_ws(content):
            raise WorkflowParseError(
                f"{path}:{number}: tab indentation is not supported; "
                "refusing to guess the indentation width"
            )


def _entry_last_line(
    lines: Sequence[str],
    key_index: int,
    block_end: int,
    key_indent: int,
) -> int:
    """Return the last line index occupied by the ``key:`` entry, inclusive.

    Only a single-line, fully closed value returns *key_index*. These three forms all continue
    onto following lines and must return that last continuation line:

    * multi-line list: ``runs-on:`` followed by ``- self-hosted``;
    * multi-line flow collection: ``runs-on: [self-hosted,`` followed by ``linux]``;
    * block scalar: ``runs-on: >`` followed by an indented body.

    What goes wrong when this returns *key_index* for a multi-line value (both measured by
    independent verification):

    * flow collection — the line lands *inside* the collection, so the runner label set
      becomes ``['self-hosted', {'timeout-minutes': '120 linux'}]``: ``runs-on`` is destroyed
      **and** the job ends up with no job-level timeout at all, while the command still
      reports success;
    * block scalar — ``runs-on`` reads as just ``>``, which is not ``self-hosted``, so the job
      is silently misclassified as GitHub-hosted and skipped by the default scope. Forcing the
      write with ``--include-hosted`` then yields ``runs-on: ''`` plus
      ``timeout-minutes: '120 self-hosted'`` — still parseable, but both values are wrong.
    """
    content = _chomp(lines[key_index])
    key_and_rest = _parse_key(content[key_indent:])
    if key_and_rest is not None:
        value = key_and_rest[1]
        if (
            _has_inline_value(value)
            and not _is_block_scalar_indicator(value)
            and not _flow_collection_unclosed(value)
        ):
            return key_index

    last = key_index
    index = key_index + 1
    while index < block_end:
        current = _chomp(lines[index])
        if _is_blank_or_comment(current):
            index += 1
            continue
        indent = _indent_of(current)
        if indent is not None and indent > key_indent:
            last = index
            index += 1
            continue
        break
    return last


def _entry_continuation_text(
    lines: Sequence[str], first: int, last: int,
) -> List[str]:
    """Collect the text of every non-blank, non-comment line in ``[first, last]``."""
    return [
        _chomp(lines[index]).strip()
        for index in range(first, last + 1)
        if not _is_blank_or_comment(_chomp(lines[index]))
    ]


def _parse_scalar_list(rest: str, lines: Sequence[str], first: int, last: int) -> List[str]:
    """Parse a ``runs-on`` value into a list of labels.

    Handles ``ubuntu-latest``, ``[self-hosted, linux, x64, local]``, multi-line flow
    collections, multi-line ``- self-hosted`` lists and block scalars (``>`` / ``|``). When
    nothing structured can be extracted it degrades to a single element holding the raw text,
    which only affects display and the self-hosted check — never the insertion.
    """
    if _has_inline_value(rest):
        if _is_block_scalar_indicator(rest):
            body = _entry_continuation_text(lines, first, last)
            return body if body else [rest]
        combined = rest
        if _flow_collection_unclosed(rest):
            tail = _entry_continuation_text(lines, first, last)
            combined = " ".join([rest] + tail)
        if combined.startswith("[") and combined.endswith("]"):
            inner = combined[1:-1]
            return [item.strip().strip("\"'") for item in inner.split(",") if item.strip()]
        return [combined.strip("\"'")]

    items: List[str] = []
    for index in range(first, last + 1):
        content = _chomp(lines[index]).strip()
        if content.startswith("- "):
            items.append(content[2:].strip().strip("\"'"))
    if items:
        return items

    raw = " ".join(_entry_continuation_text(lines, first, last)).strip()
    return [raw] if raw else []


def _parse_timeout_value(rest: str, lines: Sequence[str], first: int, last: int) -> Union[int, str, None]:
    """Read an existing ``timeout-minutes`` value: ``int`` when possible, otherwise the raw string."""
    if _has_inline_value(rest):
        raw = rest.strip().strip("\"'")
    else:
        collected = [
            _chomp(lines[index]).strip()
            for index in range(first, last + 1)
            if not _is_blank_or_comment(_chomp(lines[index]))
        ]
        raw = " ".join(collected).strip()
    if raw == "":
        return None
    try:
        return int(raw)
    except ValueError:
        return raw


class Job:
    """Scan result for a single workflow job."""

    def __init__(
        self,
        name: str,
        key_line: int,
        indent: int,
        body_text: str,
    ) -> None:
        self.name = name
        self.key_line = key_line
        self.indent = indent
        self.body_text = body_text
        # 锚点：runs-on 优先，其次 uses（reusable workflow caller）。
        self.anchor: Optional[str] = None
        self.anchor_line: int = -1
        self.anchor_text: str = ""
        self.insert_at: int = -1
        self.insert_indent: str = ""
        self.runs_on: Optional[List[str]] = None
        self.uses: Optional[str] = None
        self.timeout_minutes: Union[int, str, None] = None
        self.has_timeout = False
        self.self_hosted = False
        self.is_caller = False

    @property
    def runs_on_text(self) -> str:
        """Display text for ``runs-on`` (multi-line lists joined with ``, ``); caller jobs fall back to ``uses``."""
        if self.runs_on:
            return ", ".join(self.runs_on)
        if self.uses:
            return f"uses: {self.uses}"
        return "-"

    @property
    def anchor_kind(self) -> str:
        """Anchor kind, for output."""
        if self.anchor == "runs-on":
            return "runs-on"
        if self.anchor == "uses":
            return "uses"
        return "none"


class WorkflowScan:
    """Scan result for a single workflow file."""

    def __init__(self, path: Path, rel: str, lines: List[str], jobs: List[Job]) -> None:
        self.path = path
        self.rel = rel
        self.lines = lines
        self.jobs = jobs


def scan_workflow_text(text: str, path: Path, rel: str) -> WorkflowScan:
    """Scan workflow text and locate every job, its anchor line and its insertion point.

    This only locates structure and never changes content; anything that cannot be inferred
    safely raises :class:`WorkflowParseError`.
    """
    lines = _split_lines(text)
    # 块标量正文是内容不是结构：既不判 Tab 缩进，也不参与键行识别。
    opaque = _block_scalar_body_lines(lines)
    _reject_tab_indentation(lines, path, opaque)

    jobs_line = -1
    for index, raw in enumerate(lines):
        if index in opaque:
            continue
        if _JOBS_LINE_RE.match(_chomp(raw)):
            jobs_line = index
            break
    if jobs_line < 0:
        return WorkflowScan(path, rel, lines, [])  # 没有 jobs: —— 0 个 job

    # ``jobs:`` 的子键缩进 = 其后第一条结构行的缩进（不写死 +2，容忍 4 空格缩进风格）。
    job_indent = 0
    cursor = jobs_line + 1
    while cursor < len(lines):
        content = _chomp(lines[cursor])
        if _is_blank_or_comment(content):
            cursor += 1
            continue
        job_indent = _indent_of(content) or 0
        break
    if cursor >= len(lines):
        return WorkflowScan(path, rel, lines, [])  # jobs: 之下没有内容

    jobs: List[Job] = []
    index = cursor
    while index < len(lines):
        if index in opaque:
            index += 1
            continue
        content = _chomp(lines[index])
        if _is_blank_or_comment(content):
            index += 1
            continue
        indent = _indent_of(content)
        if indent is None or indent < job_indent:
            index += 1
            continue
        if indent > job_indent:
            index += 1  # 上一个 job 的块内内容（理论上到不了这里）
            continue

        parsed = _parse_key(content[indent:])
        if parsed is None:
            raise WorkflowParseError(f"{path}:{index + 1}: cannot parse job key")
        name, rest = parsed
        if _has_inline_value(rest):
            raise WorkflowParseError(
                f"{path}:{index + 1}: flow-mapping job body "
                f"(jobs.{name}: {{...}}) is not supported; refusing to guess"
            )

        block_end = index + 1
        while block_end < len(lines):
            if block_end in opaque:
                block_end += 1
                continue
            candidate = _chomp(lines[block_end])
            if _is_blank_or_comment(candidate):
                block_end += 1
                continue
            candidate_indent = _indent_of(candidate)
            if candidate_indent is not None and candidate_indent <= indent:
                break
            block_end += 1

        jobs.append(_scan_job(lines, name, index, indent, block_end, opaque))
        index = block_end

    return WorkflowScan(path, rel, lines, jobs)


def _scan_job(
    lines: Sequence[str],
    name: str,
    key_line: int,
    indent: int,
    block_end: int,
    opaque: Optional[set] = None,
) -> Job:
    """Scan one job block: locate job-level properties, the anchor and the insertion point."""
    skip = opaque or set()
    body_text = "".join(lines[key_line:block_end])
    job = Job(name=name, key_line=key_line, indent=indent, body_text=body_text)

    # job 级属性的缩进 = 块内所有键行的最小缩进（步骤里的 ``- timeout-minutes`` 会更
    # 深，因此不会被误判成 job 级 timeout）。
    property_indent: Optional[int] = None
    for index in range(key_line + 1, block_end):
        if index in skip:
            continue
        content = _chomp(lines[index])
        if _is_blank_or_comment(content):
            continue
        current_indent = _indent_of(content)
        if current_indent is None:
            continue
        if _parse_key(content[current_indent:]) is None:
            continue
        if property_indent is None or current_indent < property_indent:
            property_indent = current_indent
    if property_indent is None:
        return job  # 空 job 块

    anchor_candidates: Dict[str, int] = {}
    for index in range(key_line + 1, block_end):
        if index in skip:
            continue
        content = _chomp(lines[index])
        if _is_blank_or_comment(content):
            continue
        current_indent = _indent_of(content)
        if current_indent != property_indent:
            continue
        parsed = _parse_key(content[current_indent:])
        if parsed is None:
            continue
        key, rest = parsed
        if key == "timeout-minutes":
            job.has_timeout = True
            last = _entry_last_line(lines, index, block_end, current_indent)
            job.timeout_minutes = _parse_timeout_value(rest, lines, index + 1, last)
        elif key in ("runs-on", "uses") and key not in anchor_candidates:
            anchor_candidates[key] = index

    # 锚点优先级：runs-on > uses（caller job）。
    for key in ("runs-on", "uses"):
        if key in anchor_candidates:
            job.anchor = key
            job.anchor_line = anchor_candidates[key]
            break

    if job.anchor is None:
        return job

    anchor_index = job.anchor_line
    anchor_content = _chomp(lines[anchor_index])
    anchor_rest = _parse_key(anchor_content[property_indent:])
    last = _entry_last_line(lines, anchor_index, block_end, property_indent)
    rest = anchor_rest[1] if anchor_rest is not None else ""
    job.insert_at = last + 1
    job.insert_indent = _leading_ws(anchor_content)
    job.anchor_text = anchor_content

    if job.anchor == "runs-on":
        job.runs_on = _parse_scalar_list(rest, lines, anchor_index + 1, last)
        job.self_hosted = any(_SELF_HOSTED_LABEL in item.lower() for item in job.runs_on)
    else:
        job.is_caller = True
        job.uses = rest.strip().strip("\"'")
        # caller job 没有 runs-on，自托管与否无法从本文件判定（被调 workflow 可能在
        # 别的仓）。既不默认算自托管、也不默认算 hosted，单列一类，见 _in_scope。
        job.self_hosted = False

    return job


def resolve_timeout(job: Job, overrides: Dict[str, int]) -> int:
    """Return the timeout to write for this job: ``--policy-override`` beats the policy table."""
    forced = overrides.get(job.name.lower())
    if forced is not None:
        return forced
    return suggest(job.name, job.runs_on or [], job.body_text)


# ---------------------------------------------------------------------------
# 文件 / 目录遍历
# ---------------------------------------------------------------------------

def _read_text(path: Path) -> str:
    """Read with ``newline=""`` — universal newlines would turn CRLF into LF and break byte preservation."""
    try:
        with path.open("r", encoding="utf-8", newline="") as handle:
            return handle.read()
    except UnicodeDecodeError as exc:
        raise WorkflowParseError(f"{path}: not valid UTF-8: {exc}") from exc


def _write_text(path: Path, text: str) -> None:
    """Write with ``newline=""`` so existing CRLF endings survive."""
    with path.open("w", encoding="utf-8", newline="") as handle:
        handle.write(text)


def _is_inside(child: Path, parent: Path) -> bool:
    """True when *child* is *parent* itself or lives underneath it (both must be resolved)."""
    return child == parent or child.is_relative_to(parent)


def workflow_files(root: Path) -> List[Path]:
    """List ``*.yml`` / ``*.yaml`` under ``<root>/.github/workflows`` (sorted, for deterministic output)."""
    directory = root / ".github" / "workflows"
    if not directory.is_dir():
        return []
    found = [item for item in directory.glob("*.yml") if item.is_file()]
    found += [item for item in directory.glob("*.yaml") if item.is_file()]
    return sorted(found)


def collect_jobs(
    roots: Sequence[Path],
) -> List[Tuple[Path, WorkflowScan]]:
    """Scan every root and return ``[(root, WorkflowScan)]`` (empty for a repo without workflows).

    A file that yields 0 jobs gets a warning on stderr but is **not** an error: this org really
    does contain a workflow collapsed onto a single line (PyYAML cannot read that one either),
    and failing hard would stall the whole batch.

    Two guards protect the "never touch anything outside ``.github/workflows``" contract:
    the workflows **directory** itself must not resolve outside the root, and a symlinked
    **file** must not resolve outside that directory. Files are de-duplicated by their
    resolved (real on-disk) path, so an intra-directory symlink is scanned and counted
    once rather than twice.
    """
    collected: List[Tuple[Path, WorkflowScan]] = []
    seen: set = set()
    for root in roots:
        directory = root / ".github" / "workflows"
        if directory.is_dir() and not _is_inside(directory.resolve(), root.resolve()):
            raise WorkflowParseError(
                f"{directory}: resolves to {directory.resolve()}, outside {root.resolve()} "
                "— refusing to write through a symlinked workflows directory"
            )
        for path in workflow_files(root):
            real = path.resolve()
            if real in seen:
                continue
            seen.add(real)
            # 契约：fix 不得改动 ``.github/workflows`` 之外的任何文件。软链会让写入
            # 穿透到目录之外，所以这里显式拒绝，而不是照写。
            if path.is_symlink() and not _is_inside(real, directory.resolve()):
                raise WorkflowParseError(
                    f"{path}: symlink escapes .github/workflows "
                    f"(-> {real}); refusing to write through it"
                )
            rel = path.relative_to(root).as_posix()
            text = _read_text(path)
            scan = scan_workflow_text(text, path, rel)
            if not scan.jobs:
                print(
                    f"warning: {path}: no top-level `jobs:` found - 0 jobs scanned "
                    "(is this really a workflow?)",
                    file=sys.stderr,
                )
            collected.append((root, scan))
    return collected


def _apply_insertions(
    lines: List[str],
    edits: Sequence[Tuple[int, str, int]],
    default_eol: str,
) -> List[str]:
    """Insert whole lines in descending ``insert_at`` order (descending keeps earlier indices valid).

    *edits* are ``(insert_at, indent, minutes)``. When the anchor line happens to be the last
    line and has no newline, it first receives the file's default ending and the inserted line
    carries none — preserving "no trailing newline".
    """
    for insert_at, indent, minutes in sorted(edits, key=lambda item: item[0], reverse=True):
        previous = lines[insert_at - 1]
        previous_eol = _eol(previous)
        new_line = f"{indent}timeout-minutes: {minutes}"
        if previous_eol == "":
            lines[insert_at - 1] = previous + default_eol
            lines.insert(insert_at, new_line)
        else:
            lines.insert(insert_at, new_line + previous_eol)
    return lines


# ---------------------------------------------------------------------------
# 子命令
# ---------------------------------------------------------------------------

def _repository_roots(raw_paths: Sequence[str]) -> Tuple[List[Path], Optional[int]]:
    """Resolve the PATH arguments (default: current directory); a missing path is a usage error."""
    if not raw_paths:
        raw_paths = ["."]
    roots: List[Path] = []
    for raw in raw_paths:
        root = Path(raw)
        if not root.exists():
            print(f"error: {raw}: no such file or directory", file=sys.stderr)
            return [], 2
        roots.append(root)
    return roots, None


def _in_scope(job: Job, args: argparse.Namespace) -> bool:
    """Whether this job falls inside the current run's scope.

    * ``runs-on`` contains ``self-hosted`` — in scope by default (this tool's purpose).
    * any other job with a ``runs-on`` (hosted) — needs ``--include-hosted``.
    * a caller job (``uses:`` only, no ``runs-on``) — the called workflow may live in another
      repo, so self-hosted-ness **cannot be decided offline**; needs ``--include-callers``.
    """
    if job.is_caller:
        return bool(args.include_callers)
    if job.self_hosted:
        return True
    return bool(args.include_hosted)


def _summary_line(
    in_scope_missing: int,
    in_scope_total: int,
    self_hosted_missing: int,
    self_hosted_total: int,
    hosted_missing: int,
    hosted_total: int,
) -> str:
    """Summary line: ``N/M`` counts the **current scope**, and the parentheses break out self-hosted and hosted."""
    return (
        f"{in_scope_missing}/{in_scope_total} jobs missing timeout-minutes "
        f"(self-hosted: {self_hosted_missing}/{self_hosted_total}, "
        f"hosted: {hosted_missing}/{hosted_total})"
    )


def _run_audit(args: argparse.Namespace, roots: List[Path], overrides: Dict[str, int]) -> int:
    """``audit`` implementation."""
    scanned = collect_jobs(roots)

    records: List[dict] = []
    in_scope_missing = in_scope_total = 0
    self_hosted_missing = self_hosted_total = 0
    hosted_missing = hosted_total = 0
    caller_missing = caller_total = 0
    lines_out: List[str] = []
    current_root: Optional[Path] = None
    current_rel: Optional[str] = None
    width = 0

    for root, scan in scanned:
        for job in scan.jobs:
            suggested = resolve_timeout(job, overrides)
            if job.is_caller:
                caller_total += 1
                if not job.has_timeout:
                    caller_missing += 1
            elif job.self_hosted:
                self_hosted_total += 1
                if not job.has_timeout:
                    self_hosted_missing += 1
            else:
                hosted_total += 1
                if not job.has_timeout:
                    hosted_missing += 1
            if _in_scope(job, args):
                in_scope_total += 1
                if not job.has_timeout:
                    in_scope_missing += 1

            records.append({
                "path": str(root),
                "workflow": scan.rel,
                "job": job.name,
                "runs_on": job.runs_on,
                "uses": job.uses,
                "timeout_minutes": job.timeout_minutes,
                "suggested": suggested,
                "self_hosted": job.self_hosted,
                "caller": job.is_caller,
            })

            if args.as_json:
                continue
            if current_root != root:
                lines_out.append(str(root))
                current_root = root
                current_rel = None
            if current_rel != scan.rel:
                lines_out.append(f"  {scan.rel}")
                current_rel = scan.rel
                width = max((len(item.name) for item in scan.jobs), default=0)
            tag = "self-hosted" if job.self_hosted else ("caller" if job.is_caller else "hosted")
            if job.has_timeout:
                state = f"ok (timeout-minutes: {job.timeout_minutes})"
            else:
                state = f"MISSING -> {suggested}"
            mark = " " if _in_scope(job, args) else "-"
            lines_out.append(
                f"  {mark} {job.name:<{width}}  {state:<16} [{tag}]  {job.runs_on_text}"
            )

    if args.as_json:
        print(json.dumps(records, indent=2, ensure_ascii=False))
    else:
        for line in lines_out:
            print(line)
        print("  (`-` marks jobs outside the current scope)")

    summary = _summary_line(
        in_scope_missing, in_scope_total,
        self_hosted_missing, self_hosted_total,
        hosted_missing, hosted_total,
    )
    print(summary, file=sys.stderr if args.as_json else sys.stdout)
    if caller_total and not args.include_callers:
        print(
            f"note: {caller_missing}/{caller_total} caller job(s) (`uses:` without "
            "`runs-on`) are outside the default scope - their runner is decided by the "
            "called workflow, which may live in another repo; pass --include-callers to "
            "cover them.",
            file=sys.stderr if args.as_json else sys.stdout,
        )

    return 1 if in_scope_missing else 0


def _run_fix(args: argparse.Namespace, roots: List[Path], overrides: Dict[str, int]) -> int:
    """``fix`` implementation: only whole lines are added, everything else stays byte-identical."""
    scanned = collect_jobs(roots)
    changed_files = 0
    inserted_lines = 0

    for _root, scan in scanned:
        edits: List[Tuple[int, str, int]] = []
        descriptions: List[Tuple[str, str, int, str, str]] = []
        for job in scan.jobs:
            if job.has_timeout or job.anchor is None or job.insert_at < 0:
                continue
            if not _in_scope(job, args):
                continue
            minutes = resolve_timeout(job, overrides)
            edits.append((job.insert_at, job.insert_indent, minutes))
            descriptions.append(
                (job.name, job.anchor_kind, minutes, job.anchor_text, job.insert_indent)
            )
        if not edits:
            continue

        print(f"--- {scan.path}")
        for name, kind, minutes, anchor_text, indent in descriptions:
            print(f"  job {name!r} after `{anchor_text.strip()}` ({kind})")
            print(f"    insert `{indent}timeout-minutes: {minutes}`")

        if args.dry_run:
            inserted_lines += len(edits)
            changed_files += 1
            continue

        text = "".join(scan.lines)
        default_eol = "\r\n" if "\r\n" in text else "\n"
        _apply_insertions(scan.lines, edits, default_eol)
        _write_text(scan.path, "".join(scan.lines))
        inserted_lines += len(edits)
        changed_files += 1

    verb = "would insert" if args.dry_run else "inserted"
    suffix = " (dry-run, no file written)" if args.dry_run else ""
    print(f"{verb} {inserted_lines} line(s) in {changed_files} file(s){suffix}.")
    return 0


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _parse_override(raw: str) -> Tuple[str, int]:
    """Parse ``--policy-override JOB=MINUTES``."""
    name, separator, value = raw.rpartition("=")
    if not separator or not name.strip():
        raise argparse.ArgumentTypeError(f"expected JOB=MINUTES, got {raw!r}")
    try:
        minutes = int(value.strip())
    except ValueError:
        raise argparse.ArgumentTypeError(
            f"expected JOB=MINUTES with an integer MINUTES, got {raw!r}"
        ) from None
    if minutes <= 0:
        raise argparse.ArgumentTypeError(f"MINUTES must be positive, got {raw!r}")
    return name.strip(), minutes


def _build_parser() -> argparse.ArgumentParser:
    """Build the argparse parser."""
    parser = argparse.ArgumentParser(
        prog="celestia-job-timeouts",
        description=(
            "Audit and bulk-fill `timeout-minutes` on GitHub Actions jobs. "
            "Self-hosted jobs are handled by default; pass --include-hosted to "
            "cover GitHub-hosted jobs too."
        ),
    )
    sub = parser.add_subparsers(dest="subcmd")

    p_audit = sub.add_parser(
        "audit",
        help="list jobs, their runs-on and timeout-minutes state",
    )
    p_audit.add_argument("paths", nargs="*", help="repository roots (default: cwd)")
    p_audit.add_argument("--json", action="store_true", dest="as_json",
                         help="emit machine-readable JSON on stdout")
    p_audit.add_argument("--include-hosted", action="store_true",
                         help="also report/fix GitHub-hosted jobs")
    p_audit.add_argument("--include-callers", action="store_true",
                         help="also report/fix reusable-workflow caller jobs (jobs.<id>.uses)")
    p_audit.add_argument("--policy-override", action="append", default=[],
                         type=_parse_override, metavar="JOB=MINUTES",
                         help="force the timeout for a job name (repeatable)")

    p_fix = sub.add_parser("fix", help="insert timeout-minutes where missing")
    p_fix.add_argument("paths", nargs="*", help="repository roots (default: cwd)")
    p_fix.add_argument("--dry-run", action="store_true",
                       help="print the insertions without writing any file")
    p_fix.add_argument("--include-hosted", action="store_true",
                       help="also report/fix GitHub-hosted jobs")
    p_fix.add_argument("--include-callers", action="store_true",
                       help="also report/fix reusable-workflow caller jobs (jobs.<id>.uses)")
    p_fix.add_argument("--policy-override", action="append", default=[],
                       type=_parse_override, metavar="JOB=MINUTES",
                       help="force the timeout for a job name (repeatable)")

    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    """CLI entry point; returns the exit code (0 success / 1 missing timeout / 2 usage or parse error)."""
    parser = _build_parser()
    args = parser.parse_args(argv)

    if not args.subcmd:
        parser.print_help()
        return 2

    roots, failure = _repository_roots(args.paths)
    if failure is not None:
        return failure

    overrides: Dict[str, int] = {
        name.lower(): minutes for name, minutes in args.policy_override
    }

    try:
        if args.subcmd == "audit":
            return _run_audit(args, roots, overrides)
        return _run_fix(args, roots, overrides)
    except WorkflowParseError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    except OSError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())

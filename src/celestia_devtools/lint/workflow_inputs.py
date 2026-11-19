#!/usr/bin/env python3
"""Fail a reusable-workflow call whose inputs break the callee's contract.

*Why this exists:* hikari's CNB overflow lane passed ``devtools-ref:`` to the
``cnb-overflow.yml`` callee, which never declared that input. GitHub validates
reusable-workflow inputs at **startup**: every run of the caller died as
``startup_failure`` with **zero jobs** for six days and 359 consecutive runs
(``known-live-issues`` L157, fixed by hikari #1086) — and a startup failure
produces **no check-run**, so ``gh pr checks`` stayed empty while the farm queue
grew. The fleet's audits compare bytes and refs (``ci-audit``) but nothing
compared the caller's ``with:`` keys against the callee's
``workflow_call.inputs``; this lint makes that shape loud BEFORE the push.

*Rules* (all hard errors — each finding carries the caller ``path:line``, the
job id, the offending key and the callee coordinate so the judgement is
recomputable):

``undeclared-input``
    the caller passes a ``with:`` key the callee never declared. Key names are
    case-sensitive (``Ref`` is not ``ref``) — GitHub compares them literally,
    and so does this lint.
``missing-required-input``
    the callee declares an input ``required: true`` with **no** ``default`` and
    the caller's ``with:`` does not carry it. An input with a ``default`` is
    satisfiable without the caller, so it is never reported.
``callee-not-reusable``
    the callee file was resolved and parses, but declares no ``workflow_call``
    trigger — calling it cannot work at all.
``with-not-mapping``
    the caller's ``with:`` is not a mapping, so no contract can be checked.
``local-callee-missing``
    a ``./``-form callee file does not exist in the scanned tree.

*Skip, never an error:* when the callee lives in another repository and its
content cannot be fetched (``gh`` missing, network down, rate limited), the
call site is reported as ``skipped`` with the reason and does NOT affect the
exit code — a fetch outage must not paint the tree red or green, only shrug
(``--strict-fetch`` turns skips into errors for an offline audit of the local
half alone).

*Resolution order* for ``org/repo/.github/workflows/<f>.yml@<ref>``: callees of
this repository itself (``celestia-island/celestia-devtools``) resolve via
``git show <ref>:<path>`` in this checkout (full history, offline, fast), then
fall back to the API; every other repository goes straight to the API
(``gh api repos/<org>/<repo>/contents/<path>?ref=<ref>``). Local ``./``
callees resolve inside the scanned tree. The YAML ``on:`` key parses as boolean
``True`` under YAML 1.1, so the trigger lookup checks both spellings — the same
trap ``ci-audit`` handles.

*Usage*::

    celestia-devtools workflow-inputs                 # scan the repository root
    celestia-devtools workflow-inputs path ...        # explicit roots
    celestia-devtools workflow-inputs --json          # machine-readable
    celestia-devtools workflow-inputs --strict-fetch  # skipped = error
    celestia-devtools workflow-inputs --selftest

Exit: 0 clean, 1 violations (or skips under ``--strict-fetch``), 2 usage error.
A target with no ``.github/workflows`` directory at all is a usage error: a
mistyped path must never read as clean (``--allow-empty`` to accept).
"""

from __future__ import annotations
import argparse
import base64
import json
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any, Callable, Sequence
from urllib.parse import quote
from celestia_devtools.core import envelope
import yaml

#: The repository whose callees resolve through the local git checkout first.
DEVTOOLS_ORG = "celestia-island"
DEVTOOLS_REPO = "celestia-devtools"

#: ``uses: org/repo/.github/workflows/<path>@<ref>`` — the remote callee form.
USES_REMOTE = re.compile(r"^(?P<org>[^/\s]+)/(?P<repo>[^/\s]+)/\.github/workflows/(?P<path>[^@\s]+?)@(?P<ref>[^@\s]+)$")

#: ``uses: ./.github/workflows/<path>`` — the local callee form.
USES_LOCAL = re.compile(r"^\./\.github/workflows/(?P<path>[^@\s]+)$")

#: Workflow file suffixes GitHub loads (both are accepted; so are both here).
WORKFLOW_SUFFIXES = (".yml", ".yaml")

def _default_root() -> Path:
    """The repository root when no path is given (falls back to the cwd)."""

    try:
        probe = subprocess.run(["git", "rev-parse", "--show-toplevel"], capture_output = True, text = True, check = False)
    except OSError:
        return Path.cwd()

    top = probe.stdout.strip()

    if (probe.returncode == 0) and top:
        return Path(top)

    return Path.cwd()

def _module_repo_root() -> Path | None:
    """This checkout's root when running from a git clone (None when installed)."""

    candidate = Path(__file__).resolve().parents[3]

    try:
        probe = subprocess.run(["git", "-C", str(candidate), "rev-parse", "--show-toplevel"], capture_output = True, text = True, check = False)
    except OSError:
        return None

    if (probe.returncode == 0) and probe.stdout.strip():
        return candidate

    return None

def _relative(path: Path, base: Path) -> str:
    """Render `path` relative to `base` so findings are root-anchored."""

    try:
        return str(path.resolve().relative_to(base.resolve()))
    except (OSError, ValueError):
        return str(path)

def _on_node(doc: Any) -> Any:
    """The top-level ``on`` node, under either YAML spelling of the key.

    YAML 1.1 parses a bare ``on:`` into the boolean ``True``; workflows written
    with a quoted ``"on":`` keep the string. Both spellings exist in the fleet,
    and missing one half made auditors blind to the other's callees.
    """

    if not isinstance(doc, dict):
        return None

    if "on" in doc:
        return doc["on"]

    return doc.get(True)

def gh_api(endpoint: str) -> tuple[int, str]:
    """Call ``gh api <endpoint>``; never raises (rc 127 = gh unusable)."""

    try:
        probe = subprocess.run(["gh", "api", endpoint], capture_output = True, text = True, encoding = "utf-8", errors = "replace", check = False)
    except OSError as exc:
        return 127, str(exc)

    return probe.returncode, probe.stdout

def _git_show(checkout: Path, ref: str, path: str) -> str | None:
    """`git show <ref>:<path>` inside `checkout`; None on any failure."""

    try:
        probe = subprocess.run(["git", "-C", str(checkout), "show", f"{ref}:{path}"], capture_output = True, text = True, encoding = "utf-8", errors = "replace", check = False)
    except OSError:
        return None

    if probe.returncode != 0:
        return None

    return probe.stdout

def fetch_via_api(org: str, repo: str, path: str, ref: str, runner: Callable[[str], tuple[int, str]] = gh_api) -> tuple[str, str]:
    """Fetch a callee file through the contents API.

    Returns ``(status, text)`` with status ``ok`` or ``fetch-failed``; every
    failure mode (missing gh, bad credentials, rate limit, decode trouble)
    collapses into the one skip-worthy status — the caller decides whether a
    skip is acceptable, and by default it is.
    """

    endpoint = f"repos/{org}/{repo}/contents/{quote(path, safe = '/')}?ref={quote(ref, safe = '')}"
    rc, payload = runner(endpoint)

    if rc != 0:
        return "fetch-failed", f"gh api rc={rc}"

    try:
        document = json.loads(payload)
    except json.JSONDecodeError:
        return "fetch-failed", "api response is not JSON"

    if not isinstance(document, dict) or (document.get("encoding") != "base64") or (not isinstance(document.get("content"), str)):
        return "fetch-failed", "api response carries no base64 content"

    try:
        return "ok", base64.b64decode(document["content"]).decode("utf-8", errors = "replace")
    except (ValueError, UnicodeDecodeError) as exc:
        return "fetch-failed", f"content decode failed: {exc}"

def callee_inputs(text: str) -> tuple[str, dict[str, Any]]:
    """Extract ``workflow_call.inputs`` from a callee file's text.

    Returns ``(status, inputs)`` where status is ``ok`` (inputs mapping, empty
    when the callee declares none), ``not-reusable`` (parses, no
    ``workflow_call``), ``parse-error`` (not YAML) or ``not-mapping`` (parsed
    into something that is not a workflow document).
    """

    try:
        document = yaml.safe_load(text)
    except yaml.YAMLError:
        return "parse-error", {}

    on = _on_node(document)

    if not isinstance(on, dict) or ("workflow_call" not in on):
        return "not-reusable", {}

    call_node = on["workflow_call"]

    if call_node in (None, True):
        return "ok", {}

    if not isinstance(call_node, dict):
        return "not-reusable", {}

    inputs = call_node.get("inputs") or {}

    if not isinstance(inputs, dict):
        return "ok", {}

    return "ok", {str(key): (value if isinstance(value, dict) else {}) for key, value in inputs.items()}

def _required_without_default(spec: Any) -> bool:
    """An input the caller must pass: ``required: true`` and no ``default``."""

    return isinstance(spec, dict) and (spec.get("required") is True) and ("default" not in spec)

def _with_keys(job: Any) -> tuple[bool, set[str]]:
    """The caller's ``with:`` keys; ``(present, keys)``; present is False when absent."""

    if not isinstance(job, dict) or ("with" not in job):
        return False, set()

    with_node = job["with"]

    if not isinstance(with_node, dict):
        return True, set()

    return True, {str(key) for key in with_node}

def _line_of_key(lines: Sequence[str], job_id: str, key: str | None) -> int:
    """Best-effort 1-based line for a job's ``with:`` key (0 = not located).

    Walks the raw text: the job block starts at the first line whose stripped
    prefix is ``<job_id>:`` and ends where indentation falls back to the job
    key's level. Inside the block the ``with:`` line (or, when the key itself is
    the answer, the ``<key>:`` line) pins the finding to a human-findable spot.
    """

    job_pattern = re.compile(r"^(?P<indent>[ \t]*)%s:(\s|\Z)" % re.escape(job_id))
    start = None
    job_indent = ""

    for index, line in enumerate(lines):
        match = job_pattern.match(line)

        if match:
            start = index
            job_indent = match.group("indent")
            break

    if start is None:
        return 0

    key_line = 0
    with_line = 0

    for index in range(start, len(lines)):
        line = lines[index]
        stripped = line.strip()

        if stripped and (not line.startswith(job_indent + " ")) and (index != start):
            break

        if key and re.match(r"^[ \t]+%s:(\s|\Z)" % re.escape(key), line) and (not key_line):
            key_line = index + 1

        if re.match(r"^[ \t]+with:(\s|\Z)", line) and (not with_line):
            with_line = index + 1

    if key and key_line:
        return key_line

    return with_line

def _finding(relpath: str, line: int, job_id: str, rule: str, key: str, callee: str, detail: str) -> dict:
    """Build one finding record (the JSON shape is part of the CLI contract)."""

    return {"file": relpath, "line": line, "job": job_id, "rule": rule, "key": key, "callee": callee, "detail": detail}

def _check_call_site(relpath: str, lines: Sequence[str], job_id: str, job: Any, uses: str, root: Path, devtools_checkout: Path | None, runner: Callable[[str], tuple[int, str]]) -> tuple[list[dict], dict | None]:
    """Judge one caller job; returns (findings, skip-record-or-None)."""

    present, with_keys = _with_keys(job)
    findings: list[dict] = []

    if present and (not with_keys) and (isinstance(job.get("with"), dict) is False) and (job.get("with") is not None):
        line = _line_of_key(lines, job_id, None)
        findings.append(_finding(relpath, line, job_id, "with-not-mapping", "", uses, "the caller's with: is not a mapping"))
        return findings, None

    local = USES_LOCAL.match(uses)

    if local:
        callee_file = root / ".github" / "workflows" / local.group("path")

        if not callee_file.is_file():
            findings.append(_finding(relpath, _line_of_key(lines, job_id, None), job_id, "local-callee-missing", "", uses, f"no such file: {callee_file.relative_to(root) if callee_file.is_relative_to(root) else callee_file}"))
            return findings, None

        status, inputs = callee_inputs(callee_file.read_text(encoding = "utf-8", errors = "replace"))
    else:
        remote = USES_REMOTE.match(uses)

        if not remote:
            return findings, None

        org, repo, path, ref = remote.group("org", "repo", "path", "ref")
        # The regex captures the file name relative to .github/workflows/; both
        # resolution lanes address the file by its full in-repo path.
        callee_path = f".github/workflows/{path}"
        text: str | None = None

        if (org == DEVTOOLS_ORG) and (repo == DEVTOOLS_REPO) and (devtools_checkout is not None):
            text = _git_show(devtools_checkout, ref, callee_path)

        if text is None:
            status, text = fetch_via_api(org, repo, callee_path, ref, runner)

            if status != "ok":
                return findings, {"file": relpath, "job": job_id, "callee": uses, "reason": text}

        status, inputs = callee_inputs(text)

    if status == "parse-error":
        return findings, {"file": relpath, "job": job_id, "callee": uses, "reason": "callee content fetched but is not YAML"}

    if status == "not-reusable":
        findings.append(_finding(relpath, _line_of_key(lines, job_id, None), job_id, "callee-not-reusable", "", uses, "the callee declares no workflow_call trigger"))
        return findings, None

    for key in sorted(with_keys - set(inputs)):
        findings.append(_finding(relpath, _line_of_key(lines, job_id, key), job_id, "undeclared-input", key, uses, "the callee's workflow_call.inputs does not declare this key (case-sensitive)"))

    for key in sorted(name for name, spec in inputs.items() if _required_without_default(spec) and (name not in with_keys)):
        findings.append(_finding(relpath, _line_of_key(lines, job_id, None), job_id, "missing-required-input", key, uses, "required input with no default, absent from the caller's with:"))

    return findings, None

def scan_root(root: Path, base: Path, devtools_checkout: Path | None, runner: Callable[[str], tuple[int, str]] = gh_api) -> tuple[list[dict], list[dict], int, int]:
    """Scan one root; returns (findings, skips, workflows read, call sites)."""

    workflows_dir = root / ".github" / "workflows"
    findings: list[dict] = []
    skips: list[dict] = []
    workflows = 0
    call_sites = 0

    if not workflows_dir.is_dir():
        return findings, skips, workflows, call_sites

    for path in sorted(workflows_dir.iterdir()):
        if (path.suffix not in WORKFLOW_SUFFIXES) or (not path.is_file()):
            continue

        workflows += 1
        text = path.read_text(encoding = "utf-8", errors = "replace")

        try:
            document = yaml.safe_load(text)
        except yaml.YAMLError:
            continue

        if not isinstance(document, dict):
            continue

        jobs = document.get("jobs")

        if not isinstance(jobs, dict):
            continue

        lines = text.splitlines()
        relpath = _relative(path, base)

        for job_id, job in jobs.items():
            uses = job.get("uses") if isinstance(job, dict) else None

            if not isinstance(uses, str) or (not uses):
                continue

            call_sites += 1
            site_findings, skip = _check_call_site(relpath, lines, str(job_id), job, uses, root, devtools_checkout, runner)
            findings.extend(site_findings)

            if skip is not None:
                skips.append(skip)

    return findings, skips, workflows, call_sites

def _print_report(findings: Sequence[dict], skips: Sequence[dict], workflows: int, call_sites: int) -> None:
    """Human-readable report: findings first, skips last, counts at the end."""

    for hit in findings:
        where = f"{hit['file']}:{hit['line']}" if hit["line"] else hit["file"]
        key = f" key={hit['key']}" if hit["key"] else ""
        print(f"{where}  [{hit['rule']}] job={hit['job']}{key}  -> {hit['callee']}  ({hit['detail']})")

    if skips:
        print("\n-- skipped call sites (callee content unavailable; never an error without --strict-fetch) --")

        for skip in skips:
            print(f"  {skip['file']}  job={skip['job']}  -> {skip['callee']}  ({skip['reason']})")

    print(f"\n{len(findings)} violation(s); {len(skips)} skipped call site(s); {call_sites} call site(s) across {workflows} workflow file(s)")

SELFTEST_CALLEE = """\
name: Callee
on:
  workflow_call:
    inputs:
      caller-repo:
        description: caller slug for routing
        type: string
      lane-threshold:
        description: queue depth that triggers dispatch
        type: number
        required: true
      optional-with-default:
        description: satisfiable without the caller
        type: string
        default: none
"""

SELFTEST_CALLEE_ON_STRING = SELFTEST_CALLEE.replace("on:", '"on":', 1)

SELFTEST_CALLER_L157 = """\
name: Lane
on:
  push:
jobs:
  lane:
    uses: celestia-island/celestia-devtools/.github/workflows/callee.yml@%s
    with:
      caller-repo: hikari
      devtools-ref: v2026.10
"""

SELFTEST_CALLER_CASE = """\
name: Lane
on:
  push:
jobs:
  lane:
    uses: celestia-island/celestia-devtools/.github/workflows/callee.yml@%s
    with:
      Caller-Repo: hikari
      lane-threshold: 3
"""

SELFTEST_CALLER_MISSING = """\
name: Lane
on:
  push:
jobs:
  lane:
    uses: celestia-island/celestia-devtools/.github/workflows/callee.yml@%s
    with:
      caller-repo: hikari
"""

SELFTEST_CALLER_OK = """\
name: Lane
on:
  push:
jobs:
  lane:
    uses: celestia-island/celestia-devtools/.github/workflows/callee.yml@%s
    with:
      caller-repo: hikari
      lane-threshold: 3
"""

SELFTEST_CALLER_EXTERNAL = """\
name: Lane
on:
  push:
jobs:
  lane:
    uses: some-org/some-repo/.github/workflows/callee.yml@v1
    with:
      anything: goes
"""

SELFTEST_CALLEE_NOT_REUSABLE = """\
name: Not a callee
on:
  push:
jobs:
  build:
    runs-on: ubuntu-latest
    steps:
      - run: echo hi
"""

SELFTEST_CALLER_LOCAL_BAD = """\
name: Local
on:
  push:
jobs:
  call:
    uses: ./.github/workflows/local-callee.yml
    with:
      ghost-key: 1
"""

SELFTEST_CALLER_LOCAL_GOOD = """\
name: Local
on:
  push:
jobs:
  call:
    uses: ./.github/workflows/local-callee.yml
    with:
      payload: hi
"""

SELFTEST_CALLEE_LOCAL = """\
name: Local callee
on:
  workflow_call:
    inputs:
      payload:
        type: string
        required: true
"""

SELFTEST_CALLER_LOCAL_MISSING = """\
name: Local
on:
  push:
jobs:
  call:
    uses: ./.github/workflows/local-callee.yml
    with:
      ghost-key: 1
"""

def _selftest_git_repo(callee_text: str) -> tuple[Path, str]:
    """A throwaway git repo whose HEAD tree carries the callee; (path, sha)."""

    tmp = Path(tempfile.mkdtemp(prefix = "workflow-inputs-selftest-"))
    (tmp / ".github" / "workflows").mkdir(parents = True)
    (tmp / ".github" / "workflows" / "callee.yml").write_text(callee_text, encoding = "utf-8")
    probe = subprocess.run(["git", "-C", str(tmp), "init", "-q"], capture_output = True, text = True, check = False)

    if probe.returncode != 0:
        shutil.rmtree(tmp, ignore_errors = True)
        raise RuntimeError(f"git init failed: {probe.stderr}")

    subprocess.run(["git", "-C", str(tmp), "add", "-A"], capture_output = True, check = False)
    # --no-verify: the fixture is a throwaway content store; a machine-wide
    # pre-commit guard (e.g. the workspace domain-leak hook) refuses to run in
    # an unregistered repository and would leave HEAD unborn.
    subprocess.run(["git", "-C", str(tmp), "-c", "user.email=selftest@example.invalid", "-c", "user.name=selftest", "commit", "-q", "--no-verify", "-m", "callee"], capture_output = True, check = False)
    sha = subprocess.run(["git", "-C", str(tmp), "rev-parse", "HEAD"], capture_output = True, text = True, check = False).stdout.strip()
    return tmp, sha

def selftest() -> int:
    """Verify the checker against positive, negative and skip controls.

    The controls mirror the shapes this lint exists for: the L157 caller that
    passed an undeclared ``devtools-ref``, the case-sensitivity trap, a missing
    required input, a clean caller, the external-repo skip, a callee fetched
    through the local git lane under both ``on:`` spellings, the ``./``-form
    callee, a non-reusable callee and the line-locating helper.
    """

    repo, sha = _selftest_git_repo(SELFTEST_CALLEE)

    def _failing_runner(_endpoint: str) -> tuple[int, str]:
        return 1, "simulated outage"

    def _ok_broken_runner(_endpoint: str) -> tuple[int, str]:
        return 0, json.dumps({"message": "Not Found", "documentation_url": "https://docs.github.com/rest"})

    try:
        caller = Path(tempfile.mkdtemp(prefix = "workflow-inputs-caller-"))
        workflows = caller / ".github" / "workflows"
        workflows.mkdir(parents = True)
        (workflows / "l157.yml").write_text(SELFTEST_CALLER_L157 % sha, encoding = "utf-8")
        (workflows / "case.yml").write_text(SELFTEST_CALLER_CASE % sha, encoding = "utf-8")
        (workflows / "missing.yml").write_text(SELFTEST_CALLER_MISSING % sha, encoding = "utf-8")
        (workflows / "ok.yml").write_text(SELFTEST_CALLER_OK % sha, encoding = "utf-8")
        (workflows / "external.yml").write_text(SELFTEST_CALLER_EXTERNAL, encoding = "utf-8")
        (workflows / "local-bad.yml").write_text(SELFTEST_CALLER_LOCAL_BAD, encoding = "utf-8")
        (workflows / "local-good.yml").write_text(SELFTEST_CALLER_LOCAL_GOOD, encoding = "utf-8")
        (workflows / "local-missing.yml").write_text(SELFTEST_CALLER_LOCAL_MISSING, encoding = "utf-8")
        (workflows / "local-callee.yml").write_text(SELFTEST_CALLEE_LOCAL, encoding = "utf-8")
        findings, skips, workflows_read, call_sites = scan_root(caller, caller, repo, _failing_runner)

        by_rule: dict[str, list[dict]] = {}

        for hit in findings:
            by_rule.setdefault(hit["rule"], []).append(hit)

        failures: list[str] = []

        if [hit["key"] for hit in by_rule.get("undeclared-input", [])] != ["Caller-Repo", "devtools-ref", "ghost-key", "ghost-key"]:
            failures.append(f"undeclared-input controls wrong: {[ (h['file'], h['key']) for h in by_rule.get('undeclared-input', [])] }")

        if [hit["key"] for hit in by_rule.get("missing-required-input", [])] != ["lane-threshold", "payload", "payload", "lane-threshold"]:
            failures.append(f"missing-required-input controls wrong: {[ (h['file'], h['key']) for h in by_rule.get('missing-required-input', [])] }")

        if len(by_rule.get("callee-not-reusable", [])) != 0:
            failures.append("callee-not-reusable fired on controls that all resolve")

        l157 = [hit for hit in by_rule.get("undeclared-input", []) if hit["key"] == "devtools-ref"]

        if (not l157) or (l157[0]["job"] != "lane") or (l157[0]["line"] != 9):
            failures.append(f"L157 control not reported at l157.yml:9 job=lane: {l157}")

        case_hit = [hit for hit in by_rule.get("undeclared-input", []) if hit["key"] == "Caller-Repo"]

        if (not case_hit) or (case_hit[0]["file"] != ".github/workflows/case.yml"):
            failures.append(f"case-sensitivity control missed: {case_hit}")

        ok_files = {hit["file"].rsplit("/", 1)[-1] for hit in findings}

        for clean in ("ok.yml", "local-good.yml", "external.yml"):
            if clean in ok_files:
                failures.append(f"{clean} produced findings (it must be clean or skipped)")

        if len(skips) != 1:
            failures.append(f"external control must yield exactly one skip, got {skips}")

        if skips and (skips[0]["file"] != ".github/workflows/external.yml"):
            failures.append(f"skip landed on the wrong file: {skips}")

        if (workflows_read != 9) or (call_sites != 8):
            failures.append(f"enumeration read {workflows_read} workflows / {call_sites} call sites, want 9/8")

        probe = callee_inputs(SELFTEST_CALLEE_ON_STRING)

        if (probe[0] != "ok") or (probe[1].get("caller-repo") is None):
            failures.append(f"quoted-something-on callee control failed: {probe}")

        if callee_inputs(SELFTEST_CALLEE_NOT_REUSABLE)[0] != "not-reusable":
            failures.append("non-reusable callee control not reported")

        if callee_inputs("not: [valid: yaml")[0] != "parse-error":
            failures.append("parse-error control not reported")

        status, _text = fetch_via_api("some-org", "some-repo", ".github/workflows/x.yml", "v1", _failing_runner)

        if status != "fetch-failed":
            failures.append("simulated outage did not degrade to fetch-failed")

        status, _text = fetch_via_api("some-org", "some-repo", ".github/workflows/x.yml", "v1", _ok_broken_runner)

        if status != "fetch-failed":
            failures.append("api error payload (message, no content) did not degrade to fetch-failed")

        shutil.rmtree(caller, ignore_errors = True)
        shutil.rmtree(repo, ignore_errors = True)

        if failures:
            for message in failures:
                print(f"SELFTEST FAIL: {message}", file = sys.stderr)

            return 1

        print(f"selftest OK: {len(findings)} controlled violation(s) across {len(by_rule)} rule(s), 1 skip, line/location pinned, both on-spellings parsed, failure modes degraded to skip")
        return 0
    except Exception as exc:  # noqa: BLE001 - the selftest must never mask its own crash
        shutil.rmtree(repo, ignore_errors = True)
        print(f"SELFTEST ERROR: {exc}", file = sys.stderr)
        return 2

def main(argv: Sequence[str] | None = None) -> int:
    """CLI entry point: exit 0 clean, 1 violations, 2 usage error."""

    parser = argparse.ArgumentParser(prog = "workflow-inputs", description = "Fail a reusable-workflow call whose inputs break the callee's contract.")
    parser.add_argument("paths", nargs = "*", default = [], metavar = "PATH", help = "Repository roots to scan; defaults to the repository containing the cwd.")
    parser.add_argument("--json", action = "store_true", help = "Machine-readable output.")
    parser.add_argument("--devtools-checkout", default = None, metavar = "DIR", help = "Local celestia-devtools checkout for git-show callee resolution (default: this module's checkout when run from a clone).")
    parser.add_argument("--strict-fetch", action = "store_true", help = "Treat skipped call sites (callee content unavailable) as errors.")
    parser.add_argument("--allow-empty", action = "store_true", help = "Accept a target with no .github/workflows directory (off by default: a mistyped path must not read as clean).")
    parser.add_argument("--selftest", action = "store_true", help = "Verify the checker against its own controls.")
    args = parser.parse_args(argv)

    if args.selftest:
        return selftest()

    targets = [Path(raw) for raw in args.paths] or [_default_root()]
    missing = [str(target) for target in targets if not target.exists()]

    if missing:
        print(f"workflow-inputs: no such path: {', '.join(missing)}", file = sys.stderr)
        return 2

    checkout = Path(args.devtools_checkout).resolve() if args.devtools_checkout else _module_repo_root()
    findings: list[dict] = []
    skips: list[dict] = []
    workflows = 0
    call_sites = 0

    for root in targets:
        root_findings, root_skips, root_workflows, root_calls = scan_root(root, root, checkout)
        findings.extend(root_findings)
        skips.extend(root_skips)
        workflows += root_workflows
        call_sites += root_calls

    if (workflows == 0) and not args.allow_empty:
        listed = ", ".join(str(target) for target in targets)
        print(f"workflow-inputs: no .github/workflows directory under: {listed} — check the path (--allow-empty to accept)", file = sys.stderr)
        return 2

    status = 1 if (findings or (skips and args.strict_fetch)) else 0

    if args.json:
        envelope.emit({"count": len(findings), "findings": findings, "skipped": skips, "skipped_count": len(skips), "workflows": workflows, "call_sites": call_sites, "strict_fetch": args.strict_fetch})
    else:
        _print_report(findings, skips, workflows, call_sites)

    return status

if __name__ == "__main__":
    raise SystemExit(main())

#!/usr/bin/env python3
"""Stage common.just into a repo's gitignored .just/ directory.

``celestia-devtools init`` stages the bundled ``common.just`` into
``.just/celestia-devtools.just`` — a **gitignored** on-demand file, NOT a
committed copy. The repo's justfile pulls it in via an optional import::

    import? "./.just/celestia-devtools.just"

so a fresh clone parses fine before staging, and ``just fetch`` (defined in
each repo's own justfile) restages it. This replaces the former "vendor a
committed copy" model (the gradlew-style per-repo duplicate that drifted).

``init`` also ensures ``/.just/`` is gitignored and installs the commit-msg
hook. Run it once on a fresh checkout, or after upgrading celestia-devtools.

``--check`` is the CI gate: it reports a staging drift, a missing
``.gitignore`` rule, and — since the 2026-10-25 structural audit (SR-01) — a
missing or drifted **canonical caller** in ``.github/workflows``.  A reusable
workflow definition (``on: workflow_call``) in the same directory is the callee
itself, not a caller, and is skipped.  Opt-in gate callers are only compared
when the file already exists; their drift is reported as a warning because some
repositories legitimately hand-edit them until the templates expose every
input (``runner`` / ``cargo_args``).

This local gate is deliberately **stricter** than ``celestia-ci-audit``: the
audit downgrades the legacy 273 B / no-permissions / comment-only variants to a
warning so the fleet can migrate incrementally (and so nobody deletes a comment
just to silence the audit), while ``--check`` names any byte difference in the
mandatory trio — its remedy is regeneration, which is exactly what a local
checkout should do.  Use ``ci-audit`` for the fleet backlog and ``init --check``
for "this checkout must be canonical now".

Usage::

    celestia-devtools init             # stage (or refresh if drifted) + gitignore + hooks
    celestia-devtools init --force     # overwrite even if identical
    celestia-devtools init --check     # CI gate: exit 1 if drifted or ungitignored
    celestia-devtools init --no-hooks  # skip automatic commit-msg hook install
    celestia-devtools init --with-workflows  # also generate CI commit-msg lint workflow
"""

from __future__ import annotations

import argparse
import re
from pathlib import Path
from typing import Optional

import yaml

from celestia_devtools import __version__
from celestia_devtools.core import logger

DEST_DIR = ".just"
LINK_NAME = "celestia-devtools.just"
INTEROP_NAME = "git-bash-interop.just"
GITIGNORE_RULES = ("/.just/", "/celestia-devtools.just")
_VERSION_RE = re.compile(r"^#\s*Version:\s*(\S+)", re.MULTILINE)


def common_just_path() -> Path:
    """Return the filesystem path to the bundled ``common.just``."""
    try:
        from importlib.resources import files

        return Path(str(files("celestia_devtools") / "common.just"))
    except Exception:
        import celestia_devtools

        return Path(celestia_devtools.__file__).resolve().parent / "common.just"


def _read_version(text: str) -> str | None:
    m = _VERSION_RE.search(text)
    return m.group(1) if m else None


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Vendor celestia-devtools justfile recipes into a repo"
    )
    parser.add_argument(
        "--force", action="store_true",
        help="overwrite even if the existing copy is identical",
    )
    parser.add_argument(
        "--check", action="store_true",
        help="report drift without writing (exit 1 if the staged justfile, the "
        ".gitignore rules, or a canonical caller workflow need updating)",
    )
    parser.add_argument(
        "--name", default=LINK_NAME,
        help=f"output filename inside {DEST_DIR}/ (default: {LINK_NAME})",
    )
    parser.add_argument(
        "--no-hooks", action="store_true",
        help="skip automatic commit-msg hook install",
    )
    parser.add_argument(
        "--with-workflows", action="store_true",
        help="also generate the three required org workflows "
        "(commit-msg lint, CI cache policy, PR title check)",
    )
    parser.add_argument(
        "--gate", action="append", choices=sorted(OPT_IN_GATES), metavar="NAME",
        help="also (re)generate one opt-in gate caller: "
        + ", ".join(sorted(OPT_IN_GATES))
        + " (repeatable; requires --with-workflows)",
    )
    args = parser.parse_args()

    src = common_just_path()
    if not src.is_file():
        logger.error(f"common.just not found at {src}")
        return 1

    bundled = src.read_text(encoding="utf-8")
    dest_dir = Path.cwd() / DEST_DIR
    dest = dest_dir / args.name

    # ── --check: CI drift + gitignore gate ───────────────
    if args.check:
        problems = []
        if not dest.is_file():
            problems.append(f"{DEST_DIR}/{args.name} missing — run: celestia-devtools init")
        else:
            current = dest.read_text(encoding="utf-8")
            if current != bundled:
                cur_ver = _read_version(current)
                problems.append(
                    f"{DEST_DIR}/{args.name} drifted"
                    + (f" (staged v{cur_ver}" if cur_ver else "")
                    + f" ≠ bundled v{__version__}) — run: celestia-devtools init"
                )
        missing_rules = _missing_gitignore_rules()
        if missing_rules:
            problems.append(
                f".gitignore missing rule(s) {missing_rules} — run: celestia-devtools init"
            )
        problems.extend(
            _workflow_drift_problems(Path.cwd() / ".github" / "workflows")
        )
        if problems:
            for p in problems:
                logger.error(p)
            return 1
        logger.ok(f"{DEST_DIR}/{args.name} up to date (v{__version__}) and gitignored")
        return 0

    # ── Stage / refresh ──────────────────────────────────
    dest_dir.mkdir(parents=True, exist_ok=True)
    if dest.is_file():
        current = dest.read_text(encoding="utf-8")
        if current == bundled and not args.force:
            logger.info(f"{DEST_DIR}/{args.name} already up to date (v{__version__})")
        else:
            if not args.force:
                cur_ver = _read_version(current)
                logger.warn(
                    f"{DEST_DIR}/{args.name} is outdated"
                    + (f" (staged v{cur_ver}" if cur_ver else "")
                    + f" ≠ v{__version__}) — updating"
                )
            dest.write_text(bundled, encoding="utf-8")
    else:
        logger.info(f"staging {DEST_DIR}/{args.name}")
        dest.write_text(bundled, encoding="utf-8")

    # ── Retire the Git Bash interop file ─────────────────────────────
    # Since the de-bash doctrine (common.just 0.5.0) no recipe in the shared
    # layer uses `[script]` bodies or shebangs, `set script-interpreter` is
    # dead configuration. Remove stale staged copies instead of regenerating.
    interop_path = dest_dir / INTEROP_NAME
    if interop_path.is_file():
        interop_path.unlink()
        logger.ok(f"removed stale {DEST_DIR}/{INTEROP_NAME} — bash is no longer used by any shared recipe")

    if args.force or not _gitignore_has_rules():
        _ensure_gitignore()
    logger.ok(
        f"staged {DEST_DIR}/{args.name} (v{__version__}) — gitignored, NOT committed. "
        f"Refresh after upgrading with: celestia-devtools init --force"
    )

    _check_justfile_import(args.name)

    # ── Auto-install commit-msg hook ────────────────────────────────
    if not args.no_hooks:
        from celestia_devtools.vcs.hook import install_hook

        hooks_dir = Path.cwd() / ".git" / "hooks"
        if not hooks_dir.is_dir():
            logger.info("not a git repository — skipping hook install")
        else:
            hook_path = hooks_dir / "commit-msg"
            try:
                install_hook(hook_path, devtools_bin=None, force=args.force)
                logger.ok(f"installed commit-msg hook → {hook_path}")
            except SystemExit:
                logger.warn("hook install skipped (use --force to overwrite, --no-hooks to suppress)")

    # ── Opt-in: GitHub Actions commit-lint workflow ───────────────
    if args.with_workflows:
        _ensure_workflows(Path.cwd(), force=args.force, gates=tuple(args.gate or ()))
    elif args.gate:
        logger.error("--gate requires --with-workflows (it extends the generated set)")
        return 2

    return 0


# 规范形态（canonical）：全 org caller **逐字节**相同，改这里等于改全部仓的模板。
# 2026-09-15：此前这里缺 `ready_for_review`，导致 `init --force --with-workflows` 会把
# 已统一的仓退回旧变体，而缺该类型正是 §8.3.6 的死锁形态（draft 转 ready 不触发 lint
# ⇒ 必需 check 不出现 ⇒ 合并被判 BLOCKED）。模板变更必须同步 tests/test_caller_template.py 的钉死用例。
# 2026-09-26（S5-4/W-2 批次 2①）：canonical 采纳车道透传形态（= shittim-chest 已在生产
# 验证的 caller 字节）。此前 canonical 不表达车道 ⇒ hosted 额度失效时想切自托管只能改
# 每个仓的树；现在设 repo 变量 `LINT_RUNNER`/`LINT_LANE` 即可无树切换，且「私仓 hosted
# 绕行 vs 模板合规」的互斥解除。旧 273B 字节保留为 LEGACY（audit 降级 warning，见
# workflow_audit._canonical_caller_findings），存量仓用 `init --with-workflows --force` 渐进迁移。
#: Canonical caller since 2026-10-06 (1504 B). It is the lane-carrying form **plus** the
#: explicit `permissions` grant the reusable lint declares: the org default token
#: permissions were tightened on 2026-10-02, and a caller that withholds
#: `pull-requests: read` makes the callee fail as `startup_failure` (zero jobs) so the
#: single required check never reports — unmergeable on a protected repository. Thirty
#: repositories were missed that day; this template is what `init --with-workflows`
#: writes and what `workflow_audit` compares against.
WORKFLOW_COMMIT_LINT = """\
name: Commit Message Lint

on:
  pull_request:
    types: [opened, edited, reopened, ready_for_review, synchronize]
  merge_group:
    types: [checks_requested]
  # Fallback entry for the CNB lane (or an operator): dispatch this workflow on
  # the pull request's head branch, so the single required check is served by
  # the lane that can actually run. See the devtools callee for the lane rules.
  workflow_dispatch:
    inputs:
      pr:
        description: "Pull request number to lint"
        required: true
        type: string

permissions:
  # A called workflow cannot grant itself a permission the caller withholds —
  # the mismatch reads as startup_failure. The org default token permissions
  # were tightened on 2026-10-02 and this caller was missed, so the single
  # required check stopped reporting entirely. Grant exactly what the reusable
  # lint declares and nothing more.
  contents: read
  pull-requests: read

jobs:
  lint-commits:
    uses: celestia-island/celestia-devtools/.github/workflows/commit-msg-lint.yml@master
    with:
      # Lane switch without touching this tree: the org variables resolve to the
      # self-hosted fleet while the hosted lane is down (2026-09-24 account
      # payments: a job sent there sits fifteen minutes with no runner before
      # being cancelled, indistinguishable from "the check never reports").
      runner: ${{ vars.LINT_RUNNER || '["ubuntu-latest"]' }}
      lane: ${{ vars.LINT_LANE || 'hosted' }}
      pr: ${{ inputs.pr }}
"""

WORKFLOW_COMMIT_LINT_NO_PERMS = """\
name: Commit Message Lint

on:
  pull_request:
    types: [opened, edited, reopened, ready_for_review, synchronize]
  merge_group:
    types: [checks_requested]
  # Fallback entry for the CNB lane (or an operator): dispatch this workflow on
  # the pull request's head branch, so the single required check is served by
  # the lane that can actually run. See the devtools callee for the lane rules.
  workflow_dispatch:
    inputs:
      pr:
        description: "Pull request number to lint"
        required: true
        type: string

jobs:
  lint-commits:
    uses: celestia-island/celestia-devtools/.github/workflows/commit-msg-lint.yml@master
    with:
      # Lane switch without touching this tree: set the repository variable to
      # '["self-hosted","linux","x64","local"]' to serve the check from the farm
      # (e.g. while the hosted-lane quota for private repos is broken), or clear
      # it to go back to hosted. GitHub requires every check run with this name
      # to succeed, so only one lane reports at a time.
      runner: ${{ vars.LINT_RUNNER || '["ubuntu-latest"]' }}
      lane: ${{ vars.LINT_LANE || 'hosted' }}
      pr: ${{ inputs.pr }}
"""

#: 2026-09-15–2026-09-26 期间的旧 canonical（273 B，sha256 106a93d0…）：仅作审计的
#: 迁移判据——等于它 ⇒ warning（缺车道变量，请再生成）；等于新 canonical ⇒ 干净；
#: 其它 ⇒ error（手改漂移）。新模板落地后**不得**再写这个形态。
WORKFLOW_COMMIT_LINT_LEGACY = """\
name: Commit Message Lint

on:
  pull_request:
    types: [opened, edited, reopened, ready_for_review, synchronize]
  merge_group:
    types: [checks_requested]

jobs:
  lint-commits:
    uses: celestia-island/celestia-devtools/.github/workflows/commit-msg-lint.yml@master
"""

# Canonical caller for the self-hosted cache gate: one line of wiring per repository, so the
# rule cannot be quietly reintroduced by a later workflow edit. What the gate rejects, and
# the measurements behind it, are documented in the callee and in
# `celestia_devtools.ci.cache_policy`.
WORKFLOW_CI_CACHE = """\
name: CI Cache Policy

on:
  pull_request:
    types: [opened, reopened, ready_for_review, synchronize]
  merge_group:
    types: [checks_requested]

jobs:
  cache-policy:
    uses: celestia-island/celestia-devtools/.github/workflows/ci-cache.yml@master
"""


# Canonical caller for the org PR-title check. It had no template until 2026-09-20, which is
# why 21 repositories carried a hand-written copy whose `types` omitted `synchronize` -- see
# the constant above for what that omission costs.
WORKFLOW_PR_TITLE_CHECK = """\
name: PR Title Check

on:
  pull_request:
    types: [opened, edited, reopened, ready_for_review, synchronize]
  merge_group:
    types: [checks_requested]

jobs:
  pr-title-check:
    uses: celestia-island/celestia-devtools/.github/workflows/pr-title-check.yml@master
"""


# Canonical callers for the six gates that had none until 2026-10-24. Until then the
# fleet's other gates existed only as hand-written copies in each repository, which is why
# the same drift kept reappearing: `pr-title-check` lost `synchronize` in five repositories,
# `cnb-overflow` had no lane passthrough anywhere, and `p0-gate` was missing from the one
# repository an open P0 actually covered (evernight). A caller that is generated from one
# template can be audited; a caller that is copied cannot.
#
# `p0-gate` and `family-versions` are opt-in by nature (only a repository named in the P0
# ledger's scope, or one consuming a cross-repository family edge, needs them), so they are
# written by `init --with-workflows` only when asked for explicitly with `--gate`.

WORKFLOW_P0_GATE = """\
name: P0 Gate

# An unresolved P0 finding must cost something. This job fails while any P0 whose
# scope covers this repository is still open, unless the pull request explicitly
# acknowledges it with a `P0-ACK: <id>` line in its description.
#
# The ledger lives in celestia-devtools so one place, not seven, decides what is
# still outstanding; the reusable workflow checks out master for exactly that
# reason — a runner-preinstalled copy could read a stale ledger.
#
# `edited` is deliberate: the acknowledgment is written in the PR body, so editing
# the body has to re-run the gate.
#
# The concurrency group MUST differ from the callee's (`p0-gate-${{ github.ref }}`).
# A called workflow's `concurrency` is evaluated in the CALLER's context, so an
# identical group makes the callee cancel the run that invoked it: the caller then
# completes as a failure with zero jobs, which GitHub reports as "workflow file
# issue" — indistinguishable from a broken YAML file. The generated template had
# exactly that collision until 2026-10-24.

on:
  pull_request:
    types: [opened, edited, reopened, ready_for_review, synchronize]
  workflow_dispatch:

concurrency:
  group: ${{ github.workflow }}-${{ github.event.pull_request.number || github.ref }}
  cancel-in-progress: true

jobs:
  p0-gate:
    uses: celestia-island/celestia-devtools/.github/workflows/p0-gate.yml@master
"""


WORKFLOW_FAMILY_VERSIONS = """\
name: Family dependency identity

on:
  pull_request:
    types: [opened, reopened, ready_for_review, synchronize]
  merge_group:
    types: [checks_requested]

jobs:
  family-versions:
    uses: celestia-island/celestia-devtools/.github/workflows/family-versions.yml@master
"""

WORKFLOW_VERIFY_VERSIONS = """\
name: Verify Versions

on:
  pull_request:
    types: [opened, reopened, ready_for_review, synchronize]
  merge_group:
    types: [checks_requested]

jobs:
  verify-versions:
    uses: celestia-island/celestia-devtools/.github/workflows/verify-versions.yml@master
"""

WORKFLOW_RULES_LINT = """\
name: Rules Lint

on:
  pull_request:
    types: [opened, reopened, ready_for_review, synchronize]
  merge_group:
    types: [checks_requested]

jobs:
  rules-lint:
    uses: celestia-island/celestia-devtools/.github/workflows/rules-lint.yml@master
"""

WORKFLOW_RELEASE_NOTES = """\
name: Release Notes

on:
  push:
    tags: ["v*"]

jobs:
  release-notes:
    permissions:
      contents: write
    uses: celestia-island/celestia-devtools/.github/workflows/release-notes.yml@master
"""

WORKFLOW_CNB_OVERFLOW = """\
name: CNB Overflow Lane

on:
  pull_request:
    types: [opened, reopened, ready_for_review, synchronize]
  workflow_dispatch:
    inputs:
      target_sha:
        description: "Commit SHA (empty = PR head / branch head)"
        type: string
        default: ""

permissions:
  statuses: write
  actions: read

concurrency:
  group: cnb-overflow-lane-pr-${{ github.event.pull_request.number || 'manual' }}
  cancel-in-progress: true

jobs:
  lane:
    uses: celestia-island/celestia-devtools/.github/workflows/cnb-overflow.yml@master
    with:
      target_repo: ${{ github.event.repository.name }}
      target_sha: ${{ inputs.target_sha || github.event.pull_request.head.sha || github.sha }}
      task: cargo-check
      lane: pr-${{ github.event.pull_request.number || 'manual' }}
      queue_threshold: 3
      poll_minutes: 10
    secrets: inherit
"""


#: Opt-in gate callers: a repository needs only the ones whose scope covers it
#: (P0 ledger scope, a cross-repository family edge, a version line, a repository
#: AGENTS.md, a release tag, or the CNB spill lane), so they are generated only
#: on request. Emitting them all would red repositories that legitimately have
#: no manifests (`family-versions` treats an empty scan as an error) or no tags.
OPT_IN_GATES: dict[str, tuple[str, str, str]] = {
    "p0": ("p0-gate.yml", WORKFLOW_P0_GATE, "P0 ledger gate"),
    "family": ("family-versions.yml", WORKFLOW_FAMILY_VERSIONS, "family dependency identity"),
    "verify": ("verify-versions.yml", WORKFLOW_VERIFY_VERSIONS, "version consistency"),
    "rules": ("rules-lint.yml", WORKFLOW_RULES_LINT, "rules tree lint"),
    "release-notes": ("release-notes.yml", WORKFLOW_RELEASE_NOTES, "release notes"),
    "cnb": ("cnb-overflow-lane.yml", WORKFLOW_CNB_OVERFLOW, "CNB overflow lane"),
}


#: The three callers every repository needs, in generation order. Both the
#: generator and ``--check`` read this table, so a new required caller cannot be
#: added in one place and forgotten in the other.
REQUIRED_WORKFLOWS: tuple[tuple[str, str, str], ...] = (
    ("commit-msg-lint.yml", WORKFLOW_COMMIT_LINT, "commit-msg lint"),
    ("ci-cache.yml", WORKFLOW_CI_CACHE, "CI cache policy"),
    ("pr-title-check.yml", WORKFLOW_PR_TITLE_CHECK, "PR title check"),
)


def _is_callee_definition(text: str) -> bool:
    """True for the reusable workflow itself (``on: workflow_call``).

    ``celestia-devtools`` hosts all nine callees in its own
    ``.github/workflows``; comparing those against the caller templates would
    flag the control plane as drifted from itself.

    The trigger is read from the parsed document, not from a substring: a
    caller whose comment merely mentions ``workflow_call:`` must still be
    compared (a substring test would silently exempt any drifted file that
    quotes the word — a one-line bypass of this gate).
    """
    try:
        root = yaml.compose(text, Loader=yaml.SafeLoader)
    except yaml.YAMLError:
        # Unparseable: not a callee. The caller comparison then reports drift,
        # which is the honest outcome for a file GitHub cannot run either.
        return False
    if not isinstance(root, yaml.nodes.MappingNode):
        return False
    for key, value in root.value:
        if not isinstance(key, yaml.nodes.ScalarNode) or key.value != "on":
            continue
        if isinstance(value, yaml.nodes.ScalarNode):
            return value.value == "workflow_call"
        if isinstance(value, yaml.nodes.SequenceNode):
            return any(
                isinstance(item, yaml.nodes.ScalarNode) and item.value == "workflow_call"
                for item in value.value
            )
        if isinstance(value, yaml.nodes.MappingNode):
            return any(
                isinstance(item_key, yaml.nodes.ScalarNode)
                and item_key.value == "workflow_call"
                for item_key, _item_value in value.value
            )
    return False


def _canonical_alias(workflows_dir: Path, template: str, filename: str) -> Optional[Path]:
    """A differently-named file carrying the canonical caller bytes.

    Eight repositories landed the byte-identical CI-cache caller as
    ``cache-policy.yml`` before the template named it ``ci-cache.yml``.
    Regenerating the canonical name would install a second caller for the same
    check, so content identity counts as "in place" and the caller is asked to
    rename it instead.
    """
    for candidate in sorted(workflows_dir.glob("*.y*ml")):
        if candidate.name == filename:
            continue
        try:
            if candidate.read_bytes().decode("utf-8", errors="replace") == template:
                return candidate
        except OSError:
            continue
    return None


def _workflow_drift_problems(workflows_dir: Path) -> list[str]:
    """Missing/drifted canonical callers under *workflows_dir*.

    Returns the mandatory-caller problems (the ones that fail ``--check``) and
    warns about drifted opt-in callers. A callee definition is never compared.
    """
    problems: list[str] = []
    for filename, template, _label in REQUIRED_WORKFLOWS:
        path = workflows_dir / filename
        rel = f".github/workflows/{filename}"
        if not path.is_file():
            alias = _canonical_alias(workflows_dir, template, filename)
            if alias is not None:
                logger.warn(
                    f"{rel} missing, but {alias.name} carries the canonical caller — "
                    f"rename it to {filename} (regenerating would install a second caller)"
                )
                continue
            problems.append(
                f"{rel} missing — run: celestia-devtools init --with-workflows"
            )
            continue
        text = path.read_bytes().decode("utf-8", errors="replace")
        if _is_callee_definition(text):
            continue
        if text != template:
            problems.append(
                f"{rel} drifted ({len(text.encode('utf-8'))} B ≠ canonical "
                f"{len(template.encode('utf-8'))} B) — run: celestia-devtools "
                f"init --with-workflows --force"
            )
    for name, (filename, template, label) in OPT_IN_GATES.items():
        path = workflows_dir / filename
        if not path.is_file():
            continue
        text = path.read_bytes().decode("utf-8", errors="replace")
        if _is_callee_definition(text):
            continue
        if text != template:
            logger.warn(
                f".github/workflows/{filename} drifted from the {label} template — "
                f"regenerate with: celestia-devtools init --with-workflows --gate {name} --force"
                " (hand-edited inputs are why this is a warning, not a failure)"
            )
    return problems


def _ensure_workflows(
    repo_root: Path, *, force: bool = False, gates: tuple[str, ...] = ()
) -> None:
    workflows_dir = repo_root / ".github" / "workflows"
    workflows_dir.mkdir(parents=True, exist_ok=True)

    wanted = list(REQUIRED_WORKFLOWS)
    for gate in gates:
        wanted.append(OPT_IN_GATES[gate])
    for filename, template, label in wanted:
        target = workflows_dir / filename
        if target.exists() and not force:
            logger.info(f"{filename} already exists — skipping (use --force to regenerate)")
            continue
        if not target.exists():
            alias = _canonical_alias(workflows_dir, template, filename)
            if alias is not None:
                logger.warn(
                    f"{filename} not generated: {alias.name} already carries the "
                    f"canonical caller — rename it to {filename} instead "
                    "(generating would install a second caller for the same check)"
                )
                continue
        # write_bytes, not write_text: the generated callers are pinned
        # byte-for-byte across the fleet, and write_text's default
        # newline=os.linesep would turn their LF into CRLF on Windows.
        target.write_bytes(template.encode("utf-8"))
        logger.ok(f"generated {label} workflow → {target}")

def _check_justfile_import(name: str) -> None:
    """Print a hint if the repo's justfile doesn't import the staged file."""
    justfile = Path.cwd() / "justfile"
    recipe_import = f'import? "./{DEST_DIR}/{name}"'
    interop_import = f'import? "./{DEST_DIR}/{INTEROP_NAME}"'
    if not justfile.is_file():
        logger.info("no justfile found — create one starting with:")
        print(f"\n    {recipe_import}\n")
        return
    content = justfile.read_text(errors="replace")
    has_recipes = (
        recipe_import in content or f'import "./{DEST_DIR}/{name}"' in content
    )
    if interop_import in content:
        logger.warn(
            f"justfile still imports {DEST_DIR}/{INTEROP_NAME} — that file is "
            f"obsolete (bash is banned in recipes); delete the import line"
        )
    if has_recipes:
        logger.info(f"justfile already imports {DEST_DIR}/{name}")
    else:
        logger.info("add near the top of your justfile:")
        print(f"\n    {recipe_import}\n")


def _gitignore_path() -> Path:
    return Path.cwd() / ".gitignore"


def _gitignore_has_rules() -> bool:
    """True if .gitignore already contains all required ignore rules."""
    return not _missing_gitignore_rules()


def _missing_gitignore_rules() -> list[str]:
    """Return the subset of GITIGNORE_RULES not yet present in .gitignore."""
    gi = _gitignore_path()
    if not gi.is_file():
        return list(GITIGNORE_RULES)
    lines = gi.read_text(encoding="utf-8", errors="replace").splitlines()
    stripped = {ln.strip() for ln in lines}
    return [r for r in GITIGNORE_RULES if r not in stripped]


def _ensure_gitignore() -> None:
    """Append any missing GITIGNORE_RULES to .gitignore (idempotent)."""
    missing = _missing_gitignore_rules()
    if not missing:
        return
    gi = _gitignore_path()
    header = (
        "\n# celestia-devtools: staged-on-demand shared justfile recipes "
        "(celestia-devtools init / just fetch)\n"
    )
    with gi.open("a", encoding="utf-8") as f:
        if gi.stat().st_size > 0 and not gi.read_text(encoding="utf-8").endswith("\n"):
            f.write("\n")
        f.write(header)
        for rule in missing:
            f.write(f"{rule}\n")
    logger.ok(f"added .gitignore rule(s): {', '.join(missing)}")


if __name__ == "__main__":
    raise SystemExit(main())

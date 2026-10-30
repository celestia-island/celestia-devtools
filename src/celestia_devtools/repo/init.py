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
``.gitignore`` rule, a local ``fetch`` recipe or obsolete Git-Bash import that
shadows the shared justfile (SR-24/SR-25), and — since the 2026-10-25 structural
audit (SR-01) — a missing or drifted **canonical caller** in
``.github/workflows``.  A reusable
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
    celestia-devtools init --with-workflows  # generate the canonical callers (ref-pinned)
    celestia-devtools init --with-workflows --ref master  # escape hatch: floating @master

Generated callers pin the devtools git ref (default ``@v2026.10``, see
:data:`PINNED_DEVTOOLS_REF`): a bad master commit cannot redefine this
repository's gate semantics until the caller is regenerated onto a newer
baseline tag. ``init --check`` and ``fleet-audit`` report ``@master`` (or
stale-pin) callers as **warnings** while the fleet migrates; the upgrade path
is always regeneration, never hand-editing.
"""

from __future__ import annotations
import argparse
import re
from pathlib import Path
import yaml
from celestia_devtools import __version__
from celestia_devtools.core import logger
from importlib.resources import files
import celestia_devtools
from celestia_devtools.vcs.hook import install_hook

DEST_DIR = ".just"
LINK_NAME = "celestia-devtools.just"
INTEROP_NAME = "git-bash-interop.just"
GITIGNORE_RULES = ("/.just/", "/celestia-devtools.just")
_VERSION_RE = re.compile(r"^#\s*Version:\s*(\S+)", re.MULTILINE)

def common_just_path() -> Path:
    """Return the filesystem path to the bundled ``common.just``."""

    try:
        return Path(str(files("celestia_devtools") / "common.just"))
    except Exception:

        return Path(celestia_devtools.__file__).resolve().parent / "common.just"

def _read_version(text: str) -> str | None:
    m = _VERSION_RE.search(text)
    return m.group(1) if m else None

def main() -> int:
    parser = argparse.ArgumentParser(description = "Vendor celestia-devtools justfile recipes into a repo")
    parser.add_argument("--force", action = "store_true", help = "overwrite even if the existing copy is identical")
    parser.add_argument("--check", action = "store_true", help = "report drift without writing (exit 1 if the staged justfile, the " ".gitignore rules, or a canonical caller workflow need updating)")
    parser.add_argument("--name", default = LINK_NAME, help = f"output filename inside {DEST_DIR}/ (default: {LINK_NAME})")
    parser.add_argument("--no-hooks", action = "store_true", help = "skip automatic commit-msg hook install")
    parser.add_argument("--with-workflows", action = "store_true", help = "also generate the three required org workflows " "(commit-msg lint, CI cache policy, PR title check)")
    parser.add_argument("--gate", action = "append", choices = sorted(OPT_IN_GATES), metavar = "NAME", help = "also (re)generate one opt-in gate caller: " + ", ".join(sorted(OPT_IN_GATES)) + " (repeatable; requires --with-workflows)")
    parser.add_argument("--ref", default = PINNED_DEVTOOLS_REF, metavar = "REF", help = f"devtools git ref (branch or tag) the generated callers pin (default: {PINNED_DEVTOOLS_REF}; 'master' is the escape hatch for break-glass consumption of an unmerged fix; a 40-hex SHA is rejected — the clone-based gates cannot bootstrap one)")
    args = parser.parse_args()

    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._/-]*", args.ref or ""):
        logger.error(f"--ref {args.ref!r} is not a valid git ref (letters, digits, '.', '_', '/', '-')")
        return 2

    if re.fullmatch(r"[0-9a-fA-F]{40}", args.ref or ""):
        # R2 review F2: the clone-based callees (verify-versions,
        # family-versions) bootstrap with `git clone --branch`, which accepts a
        # branch or tag but not an arbitrary SHA — a caller generated at a SHA
        # pin would fail those gates' bootstrap at runtime. Generated callers
        # pin branches/tags only; hand-written SHA pins are still legal (the
        # audit treats any non-master ref as a pin) and unaffected.
        logger.error(f"--ref {args.ref[:8]}… looks like a commit SHA; generated callers pin a branch or tag (the clone-based gates cannot bootstrap a SHA ref)")
        return 2

    src = common_just_path()

    if not src.is_file():
        logger.error(f"common.just not found at {src}")
        return 1

    bundled = src.read_text(encoding = "utf-8")
    dest_dir = Path.cwd() / DEST_DIR
    dest = dest_dir / args.name

    # ── --check: CI drift + gitignore gate ───────────────
    if args.check:
        problems = []

        if not dest.is_file():
            problems.append(f"{DEST_DIR}/{args.name} missing — run: celestia-devtools init")
        else:
            current = dest.read_text(encoding = "utf-8")

            if current != bundled:
                cur_ver = _read_version(current)
                problems.append(f"{DEST_DIR}/{args.name} drifted" + (f" (staged v{cur_ver}" if cur_ver else "") + f" ≠ bundled v{__version__}) — run: celestia-devtools init")

        missing_rules = _missing_gitignore_rules()

        if missing_rules:
            problems.append(f".gitignore missing rule(s) {missing_rules} — run: celestia-devtools init")

        problems.extend(_workflow_drift_problems(Path.cwd() / ".github" / "workflows"))

        problems.extend(_justfile_shadow_problems(Path.cwd()))

        if problems:
            for p in problems:
                logger.error(p)

            return 1

        logger.ok(f"{DEST_DIR}/{args.name} up to date (v{__version__}) and gitignored")
        return 0

    # ── Stage / refresh ──────────────────────────────────
    dest_dir.mkdir(parents = True, exist_ok = True)

    if dest.is_file():
        current = dest.read_text(encoding = "utf-8")

        if (current == bundled) and not args.force:
            logger.info(f"{DEST_DIR}/{args.name} already up to date (v{__version__})")
        else:
            if not args.force:
                cur_ver = _read_version(current)
                logger.warn(f"{DEST_DIR}/{args.name} is outdated" + (f" (staged v{cur_ver}" if cur_ver else "") + f" ≠ v{__version__}) — updating")

            dest.write_text(bundled, encoding = "utf-8")
    else:
        logger.info(f"staging {DEST_DIR}/{args.name}")
        dest.write_text(bundled, encoding = "utf-8")

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

    logger.ok(f"staged {DEST_DIR}/{args.name} (v{__version__}) — gitignored, NOT committed. " f"Refresh after upgrading with: celestia-devtools init --force")

    _check_justfile_import(args.name)

    # ── Auto-install commit-msg hook ────────────────────────────────
    if not args.no_hooks:
        hooks_dir = Path.cwd() / ".git" / "hooks"

        if not hooks_dir.is_dir():
            logger.info("not a git repository — skipping hook install")
        else:
            hook_path = hooks_dir / "commit-msg"

            try:
                install_hook(hook_path, devtools_bin = None, force = args.force)
                logger.ok(f"installed commit-msg hook → {hook_path}")
            except SystemExit:
                logger.warn("hook install skipped (use --force to overwrite, --no-hooks to suppress)")

    # ── Opt-in: GitHub Actions commit-lint workflow ───────────────
    if args.with_workflows:
        _ensure_workflows(Path.cwd(), force = args.force, gates = tuple(args.gate or ()), ref = args.ref)
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
#: The devtools git ref generated callers pin by default (arch-review
#: 2026-10-26 P2-3). A caller referencing ``@master`` lets any single master
#: commit redefine gate semantics for the whole family at once — the python-track
#: verify-versions "ok:true + null baseline" incident ran vacuously on half the
#: fleet exactly this way. The pin is a **CalVer rolling baseline tag**: a new
#: ``v<YYYY.MM>`` tag is cut on a verified-green master SHA each wave (or sooner
#: when a gate change lands); tags are never moved — a moved tag is a force
#: operation, and the CNB mirror syncs ``master`` + tags, so a mutation would
#: race it — and callers advance by regeneration, not by tag movement. CalVer
#: rather than the package version because the pin freezes *workflow files*,
#: not the Python package: a per-version tag would couple the baseline to a
#: manually bumped version line and demand a tag per release, while the monthly
#: tag decouples "when the fleet re-pins" from "when devtools ships a lint
#: tweak". Escape hatch: ``init --with-workflows --ref master`` writes the
#: floating form (break-glass consumption of an unmerged fix); the audit
#: reports every ``@master`` caller as a warning while the migration runs.
PINNED_DEVTOOLS_REF = "v2026.10"

#: Rolling baseline tag shape. Used to order a caller's ref against the
#: baseline; anything that does not match is a custom pin (a SHA is legal).
_CALVER_RE = re.compile(r"^v(\d{4})\.(\d{2})$")

#: Marker for the callee ref on a template's ``uses:`` line (rendered to
#: ``@<ref>``); never present in a rendered caller.
_REF_MARKER = "@__DEVTOOLS_REF__"

#: Marker line for the ``devtools-ref`` input inside an existing ``with:`` block
#: (rendered to one indented input line when pinned; deleted at ``master``).
_INPUT_MARKER = "__DEVTOOLS_REF_INPUT__\n"

#: Marker line standing in for a whole ``with:`` block that exists only to pass
#: the ref (rendered to ``with:`` + input when pinned; deleted at ``master``).
_WITH_MARKER = "__DEVTOOLS_REF_WITH__\n"

def render_caller(template: str, ref: str) -> str:
    """Render a caller *template* against a devtools git *ref*.

    ``master`` renders the historical floating form **byte-for-byte**: no
    ``devtools-ref`` input is passed, so the master render of every template is
    exactly the bytes today's fleet carries — the comparator that classifies
    un-migrated repositories must not move when the pin lands. Any other ref
    (tag or SHA) additionally passes ``devtools-ref: <ref>`` so the callee's
    runtime bootstrap installs the same snapshot the caller pinned — pinning
    the workflow file while the tool floats from master would be cosmetic
    (style-lint.yml set the ``devtools-ref`` input precedent).
    """

    rendered = template.replace(_REF_MARKER, "@" + ref)
    input_line = f"      devtools-ref: {ref}\n" if ref != "master" else ""
    with_block = f"    with:\n      devtools-ref: {ref}\n" if ref != "master" else ""
    return rendered.replace(_INPUT_MARKER, input_line).replace(_WITH_MARKER, with_block)

#: Caller template source (pre-render). The rendered canonical constant below
#: pins :data:`PINNED_DEVTOOLS_REF`; ``render_caller(_T_COMMIT_LINT, "master")``
#: reproduces the 2026-10-06 canonical bytes exactly, which is what lets the
#: audit classify the un-migrated fleet as "unpinned" instead of "drifted".
_T_COMMIT_LINT = """\
name: Commit Message Lint

# One in-flight run per pull request (ci-ops 8.3.3): a superseded push cancels
# its predecessor instead of queueing a second lint behind it on the lane the
# repository resolves — which, on the farm, means one slot instead of many.
concurrency:
  group: ${{ github.workflow }}-${{ github.event.pull_request.number || github.ref }}
  cancel-in-progress: true

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
    uses: celestia-island/celestia-devtools/.github/workflows/commit-msg-lint.yml@__DEVTOOLS_REF__
    with:
__DEVTOOLS_REF_INPUT__
      # Lane switch without touching this tree: the org variables resolve to the
      # self-hosted fleet while the hosted lane is down (2026-09-24 account
      # payments: a job sent there sits fifteen minutes with no runner before
      # being cancelled, indistinguishable from "the check never reports").
      runner: ${{ vars.LINT_RUNNER || '["ubuntu-latest"]' }}
      lane: ${{ vars.LINT_LANE || 'hosted' }}
      pr: ${{ inputs.pr }}
"""

#: The canonical caller: the template rendered at the pinned ref. This is what
#: ``init --with-workflows`` writes and what the audits compare against.
WORKFLOW_COMMIT_LINT = render_caller(_T_COMMIT_LINT, PINNED_DEVTOOLS_REF)

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
_T_CI_CACHE = """\
name: CI Cache Policy

on:
  pull_request:
    types: [opened, reopened, ready_for_review, synchronize]
  merge_group:
    types: [checks_requested]

jobs:
  cache-policy:
    uses: celestia-island/celestia-devtools/.github/workflows/ci-cache.yml@__DEVTOOLS_REF__
__DEVTOOLS_REF_WITH__
"""

WORKFLOW_CI_CACHE = render_caller(_T_CI_CACHE, PINNED_DEVTOOLS_REF)

# Canonical caller for the org PR-title check. It had no template until 2026-09-20, which is
# why 21 repositories carried a hand-written copy whose `types` omitted `synchronize` -- see
# the constant above for what that omission costs.
_T_PR_TITLE_CHECK = """\
name: PR Title Check

# One in-flight run per pull request (ci-ops 8.3.3): a superseded push cancels
# its predecessor instead of queueing a second title check behind it on the
# lane the repository resolves.
concurrency:
  group: ${{ github.workflow }}-${{ github.event.pull_request.number || github.ref }}
  cancel-in-progress: true

on:
  pull_request:
    types: [opened, edited, reopened, ready_for_review, synchronize]
  merge_group:
    types: [checks_requested]

jobs:
  pr-title-check:
    uses: celestia-island/celestia-devtools/.github/workflows/pr-title-check.yml@__DEVTOOLS_REF__
__DEVTOOLS_REF_WITH__
"""

WORKFLOW_PR_TITLE_CHECK = render_caller(_T_PR_TITLE_CHECK, PINNED_DEVTOOLS_REF)

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
#
# `p0-gate` carries NO `devtools-ref` passthrough on purpose: its callee reads the P0
# ledger itself, which is live policy state — a pinned caller would freeze an outdated
# ledger and silently hide open P0s. Pinning the caller still freezes the gate's
# workflow semantics; only the ledger read stays on master by design.
_T_P0_GATE = """\
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
    uses: celestia-island/celestia-devtools/.github/workflows/p0-gate.yml@__DEVTOOLS_REF__
"""

WORKFLOW_P0_GATE = render_caller(_T_P0_GATE, PINNED_DEVTOOLS_REF)

_T_FAMILY_VERSIONS = """\
name: Family dependency identity

on:
  pull_request:
    types: [opened, reopened, ready_for_review, synchronize]
  merge_group:
    types: [checks_requested]

jobs:
  family-versions:
    uses: celestia-island/celestia-devtools/.github/workflows/family-versions.yml@__DEVTOOLS_REF__
__DEVTOOLS_REF_WITH__
"""

WORKFLOW_FAMILY_VERSIONS = render_caller(_T_FAMILY_VERSIONS, PINNED_DEVTOOLS_REF)

_T_VERIFY_VERSIONS = """\
name: Verify Versions

on:
  pull_request:
    types: [opened, reopened, ready_for_review, synchronize]
  merge_group:
    types: [checks_requested]

jobs:
  verify-versions:
    uses: celestia-island/celestia-devtools/.github/workflows/verify-versions.yml@__DEVTOOLS_REF__
__DEVTOOLS_REF_WITH__
"""

WORKFLOW_VERIFY_VERSIONS = render_caller(_T_VERIFY_VERSIONS, PINNED_DEVTOOLS_REF)

_T_RULES_LINT = """\
name: Rules Lint

on:
  pull_request:
    types: [opened, reopened, ready_for_review, synchronize]
  merge_group:
    types: [checks_requested]

jobs:
  rules-lint:
    uses: celestia-island/celestia-devtools/.github/workflows/rules-lint.yml@__DEVTOOLS_REF__
__DEVTOOLS_REF_WITH__
"""

WORKFLOW_RULES_LINT = render_caller(_T_RULES_LINT, PINNED_DEVTOOLS_REF)

_T_RELEASE_NOTES = """\
name: Release Notes

on:
  push:
    tags: ["v*"]

jobs:
  release-notes:
    permissions:
      contents: write
    uses: celestia-island/celestia-devtools/.github/workflows/release-notes.yml@__DEVTOOLS_REF__
__DEVTOOLS_REF_WITH__
"""

WORKFLOW_RELEASE_NOTES = render_caller(_T_RELEASE_NOTES, PINNED_DEVTOOLS_REF)

#: The CNB spill lane dispatches work to CNB's own runners; the callee never
#: fetches celestia-devtools at runtime, so its caller pins the workflow file
#: only — no ``devtools-ref`` passthrough.
_T_CNB_OVERFLOW = """\
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
    uses: celestia-island/celestia-devtools/.github/workflows/cnb-overflow.yml@__DEVTOOLS_REF__
    with:
      target_repo: ${{ github.event.repository.name }}
      target_sha: ${{ inputs.target_sha || github.event.pull_request.head.sha || github.sha }}
      task: cargo-check
      lane: pr-${{ github.event.pull_request.number || 'manual' }}
      queue_threshold: 3
      poll_minutes: 10
    secrets: inherit
"""

WORKFLOW_CNB_OVERFLOW = render_caller(_T_CNB_OVERFLOW, PINNED_DEVTOOLS_REF)

#: Opt-in gate callers: a repository needs only the ones whose scope covers it
#: (P0 ledger scope, a cross-repository family edge, a version line, a repository
#: AGENTS.md, a release tag, or the CNB spill lane), so they are generated only
#: on request. Emitting them all would red repositories that legitimately have
#: no manifests (`family-versions` treats an empty scan as an error) or no tags.
OPT_IN_GATES: dict[str, tuple[str, str, str]] = {"p0": ("p0-gate.yml", WORKFLOW_P0_GATE, "P0 ledger gate"), "family": ("family-versions.yml", WORKFLOW_FAMILY_VERSIONS, "family dependency identity"), "verify": ("verify-versions.yml", WORKFLOW_VERIFY_VERSIONS, "version consistency"), "rules": ("rules-lint.yml", WORKFLOW_RULES_LINT, "rules tree lint"), "release-notes": ("release-notes.yml", WORKFLOW_RELEASE_NOTES, "release notes"), "cnb": ("cnb-overflow-lane.yml", WORKFLOW_CNB_OVERFLOW, "CNB overflow lane")}

#: caller filename → authored template source (with the render markers). The
#: generator renders these at the requested ``--ref``; the tables above carry
#: the same sources rendered at :data:`PINNED_DEVTOOLS_REF`, so a ref asked for
#: at generation time and a ref compared at audit time can never come from two
#: different texts.
TEMPLATE_SOURCES: dict[str, str] = {"commit-msg-lint.yml": _T_COMMIT_LINT, "ci-cache.yml": _T_CI_CACHE, "pr-title-check.yml": _T_PR_TITLE_CHECK, "p0-gate.yml": _T_P0_GATE, "family-versions.yml": _T_FAMILY_VERSIONS, "verify-versions.yml": _T_VERIFY_VERSIONS, "rules-lint.yml": _T_RULES_LINT, "release-notes.yml": _T_RELEASE_NOTES, "cnb-overflow-lane.yml": _T_CNB_OVERFLOW}

#: The three callers every repository needs, in generation order. Both the
#: generator and ``--check`` read this table, so a new required caller cannot be
#: added in one place and forgotten in the other.
REQUIRED_WORKFLOWS: tuple[tuple[str, str, str], ...] = (("commit-msg-lint.yml", WORKFLOW_COMMIT_LINT, "commit-msg lint"), ("ci-cache.yml", WORKFLOW_CI_CACHE, "CI cache policy"), ("pr-title-check.yml", WORKFLOW_PR_TITLE_CHECK, "PR title check"))

#: The substring every devtools caller line carries. Kept separate so the
#: fail-closed companion below can find lines the extractor cannot read.
_DEVTOOLS_CALLEE_HINT = "celestia-island/celestia-devtools/.github/workflows/"

#: A ``uses:`` line invoking any celestia-devtools reusable workflow, with the
#: ref captured. Deliberately tolerant on the edges: a quoted ref
#: (``uses: "…@master"``) or a trailing comment (``…@master  # break-glass``)
#: used to yield no ref at all, which the pin audit then read as "this
#: repository pins nothing" — a fail-open on exactly the check the pin exists
#: for. ``(?m)^`` still keeps commented-out examples (README snippets pasted
#: into a file header) from counting as consumption.
#: A line whose first token is a ``uses:`` key (optionally a list item, and
#: optionally a quoted key -- ``"uses":`` is the same YAML key).
_USES_KEY_RE = re.compile(r"""(?m)^\s*(?:-\s*)?["']?uses["']?\s*:""")

_DEVTOOLS_USES_RE = re.compile(r"""(?m)^\s*(?:-\s*)?["']?uses["']?\s*:\s*[\"']?celestia-island/celestia-devtools/\.github/workflows/[^\s\"'@]+@([^\s\"'#]+)""")

def devtools_refs_in(text: str) -> list[str]:
    """Refs this workflow file invokes devtools callees at (comments excluded)."""

    return [match.group(1) for match in _DEVTOOLS_USES_RE.finditer(text)]

#: The ``devtools-ref`` input a caller passes inside its ``with:`` block.
_DEVTOOLS_REF_INPUT_RE = re.compile(r"""(?m)^\s+["']?devtools-ref["']?\s*:\s*["']?([^\s\"'#]+)""")

def devtools_ref_inputs_in(text: str) -> list[str]:
    """Values of the ``devtools-ref`` input a workflow passes to the callee.

    The README's commitment is that the pin freezes *both* layers: the ``uses:``
    ref chooses the workflow file, and ``devtools-ref`` chooses the tool the
    callee installs. Pinning only the first is cosmetic.
    """

    return [match.group(1) for match in _DEVTOOLS_REF_INPUT_RE.finditer(text)]

def unparsed_devtools_uses(text: str) -> list[str]:
    """Non-comment lines that name a devtools callee but yield no ref.

    The fail-closed companion to :func:`devtools_refs_in`: a caller line the
    extractor cannot read must surface as such, because the alternative is that
    it reads as "this repository has no devtools callers" — clean.
    """

    leftovers: list[str] = []

    for line in text.splitlines():
        # Only a real key counts: a ``run:`` scalar that merely echoes the hint
        # (a heredoc, a shell script) is text, not a job call — the same
        # distinction `references_callee` makes, and the reason this check must
        # not simply grep for the hint substring.
        if _USES_KEY_RE.match(line) is None:
            continue

        if (_DEVTOOLS_CALLEE_HINT not in line) or (_DEVTOOLS_USES_RE.match(line) is not None):
            continue

        leftovers.append(line.strip())

    return leftovers

def caller_render_state(text: str, template: str) -> tuple[str, str]:
    """Classify *text* against a caller *template* by the ref it renders at.

    Returns ``(state, ref)`` where state is one of:

    ``clean``
        byte-identical to the template rendered at :data:`PINNED_DEVTOOLS_REF`;
    ``master``
        byte-identical to the template rendered at ``master`` — the historical
        floating form the fleet carries today (migration backlog);
    ``stale``
        byte-identical to the template rendered at some other ref — generator
        output pinned to an earlier baseline (advance by regeneration);
    ``drifted``
        none of the above — hand-edited or from an older generator.

    The un-migrated fleet must land in ``master``, never ``drifted``: a pin
    migration that reddens every repository at once would be reverted, not
    adopted. ``render_caller(t, "master")`` reproducing the pre-pin bytes
    byte-for-byte is what makes that classification possible.
    """

    if text == render_caller(template, PINNED_DEVTOOLS_REF):
        return "clean", PINNED_DEVTOOLS_REF

    refs = devtools_refs_in(text)

    if len(refs) == 1:
        candidate = refs[0]

        if text == render_caller(template, candidate):
            return ("master" if candidate == "master" else "stale"), candidate

    return "drifted", (refs[0] if refs else "?")

def pin_position(ref: str) -> str:
    """Where *ref* sits relative to :data:`PINNED_DEVTOOLS_REF`.

    ``baseline`` | ``older`` | ``newer`` | ``custom``. Only ``older`` may be told
    to regenerate at the baseline: a repository that already advanced to a newer
    CalVer tag would be *downgraded* by that advice, and regenerating is exactly
    what the printed command does.
    """

    baseline = _CALVER_RE.match(PINNED_DEVTOOLS_REF)
    match = _CALVER_RE.match(ref or "")

    if (baseline is None) or (match is None):
        return "custom"

    if match.group(0) == baseline.group(0):
        return "baseline"

    ahead = (int(match.group(1)), int(match.group(2))) > (int(baseline.group(1)), int(baseline.group(2)))
    return "newer" if ahead else "older"

def _pin_warning(rel: str, state: str, ref: str, regenerate: str) -> str:
    """The warning line for a caller that is not at the current pin."""

    if state == "master":
        return f"{rel} pins nothing: it invokes the devtools callee at @master, so any master commit instantly redefines this gate's semantics (P2-3) — regenerate with: {regenerate}"

    position = pin_position(ref)

    if position == "newer":
        return f"{rel} is pinned to @{ref}, which is AHEAD of the baseline @{PINNED_DEVTOOLS_REF} — advance PINNED_DEVTOOLS_REF, do not regenerate this caller downwards"

    if position == "custom":
        return f"{rel} is pinned to @{ref}, which is not a CalVer baseline (a tag or a commit is legal) — the audit cannot order it against @{PINNED_DEVTOOLS_REF}"

    return f"{rel} is pinned to @{ref} (current pin @{PINNED_DEVTOOLS_REF}) — regenerate with: {regenerate} to advance"

def unpinned_caller_warnings(workflows_dir: Path) -> list[str]:
    """Callers of devtools reusable workflows not at the current pin.

    Scans every workflow file (not just the table filenames) so hand-written
    callers — style-lint adoption, gates without templates yet — are reported
    too; callee definitions are skipped. ``@master`` is a warning, not an
    error, for as long as the fleet migration runs (P2-3).
    """

    warnings: list[str] = []
    table: dict[str, tuple[str, str, str]] = {}

    for filename, _rendered, _label in REQUIRED_WORKFLOWS:
        table[filename] = (TEMPLATE_SOURCES[filename], "celestia-devtools init --with-workflows --force")

    for name, (filename, _rendered, _label) in OPT_IN_GATES.items():
        table[filename] = (TEMPLATE_SOURCES[filename], f"celestia-devtools init --with-workflows --gate {name} --force")

    if not workflows_dir.is_dir():
        return warnings

    for path in sorted(workflows_dir.glob("*.y*ml")):
        try:
            text = path.read_bytes().decode("utf-8", errors = "replace")
        except OSError:
            continue

        if _is_callee_definition(text):
            continue

        entry = table.get(path.name)

        if entry is not None:
            state, ref = caller_render_state(text, entry[0])
            rel = f".github/workflows/{path.name}"

            if state in ("master", "stale"):
                warnings.append(_pin_warning(rel, state, ref, entry[1]))

            continue

        # Not a template-owned file: warn per floating ref only (a tag or SHA
        # reference is a pin by definition; only @master floats).
        refs = devtools_refs_in(text)

        if "master" in refs:
            rel = f".github/workflows/{path.name}"
            warnings.append(_pin_warning(rel, "master", "master", "regenerate the caller with a pinned devtools ref"))

    return warnings

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
        root = yaml.compose(text, Loader = yaml.SafeLoader)
    except yaml.YAMLError:
        # Unparseable: not a callee. The caller comparison then reports drift,
        # which is the honest outcome for a file GitHub cannot run either.
        return False

    if not isinstance(root, yaml.nodes.MappingNode):
        return False

    for key, value in root.value:
        if not isinstance(key, yaml.nodes.ScalarNode) or (key.value != "on"):
            continue

        if isinstance(value, yaml.nodes.ScalarNode):
            return value.value == "workflow_call"

        if isinstance(value, yaml.nodes.SequenceNode):
            return any(isinstance(item, yaml.nodes.ScalarNode) and (item.value == "workflow_call") for item in value.value)

        if isinstance(value, yaml.nodes.MappingNode):
            return any(isinstance(item_key, yaml.nodes.ScalarNode) and (item_key.value == "workflow_call") for item_key, _item_value in value.value)

    return False

def _canonical_alias(workflows_dir: Path, template: str, filename: str) -> Path | None:
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
            if candidate.read_bytes().decode("utf-8", errors = "replace") == template:
                return candidate
        except OSError:
            continue

    return None

def workflow_drift_problems(workflows_dir: Path) -> list[str]:
    """Missing/drifted mandatory callers under *workflows_dir* (pure).

    A caller rendered at ``@master`` (the fleet's current stock) or at a stale
    pin is **not** a problem here: it is generator-owned bytes whose only issue
    is which ref it pins, and that migration reports as a warning
    (:func:`unpinned_caller_warnings`) so the fleet re-pins wave by wave instead
    of going red all at once.
    """
    problems: list[str] = []

    for filename, template, _label in REQUIRED_WORKFLOWS:
        path = workflows_dir / filename
        rel = f".github/workflows/{filename}"

        if not path.is_file():
            alias = _canonical_alias(workflows_dir, template, filename)

            if alias is not None:
                continue

            problems.append(f"{rel} missing — run: celestia-devtools init --with-workflows")
            continue

        text = path.read_bytes().decode("utf-8", errors = "replace")

        if _is_callee_definition(text):
            continue

        state, _ref = caller_render_state(text, TEMPLATE_SOURCES[filename])

        if state == "drifted":
            problems.append(f"{rel} drifted ({len(text.encode('utf-8'))} B ≠ canonical {len(template.encode('utf-8'))} B) — run: celestia-devtools init --with-workflows --force")

    return problems

def alias_warnings(workflows_dir: Path) -> list[str]:
    """Mandatory callers that exist under a different (canonical-bytes) name."""
    warnings: list[str] = []

    for filename, template, _label in REQUIRED_WORKFLOWS:
        if (workflows_dir / filename).is_file():
            continue

        alias = _canonical_alias(workflows_dir, template, filename)

        if alias is not None:
            warnings.append(f".github/workflows/{filename} present only as {alias.name} — rename it to {filename} (regenerating would install a second caller)")

    return warnings

def opt_in_drift_warnings(workflows_dir: Path) -> list[str]:
    """Drifted opt-in callers (warnings, not failures — inputs are hand-edited).

    Unpinned (``@master``) and stale-pin forms are generator-owned bytes; they
    get the dedicated pin warning instead of the drift one.
    """
    warnings: list[str] = []

    for name, (filename, _rendered, label) in OPT_IN_GATES.items():
        path = workflows_dir / filename
        rel = f".github/workflows/{filename}"

        if not path.is_file():
            continue

        text = path.read_bytes().decode("utf-8", errors = "replace")

        if _is_callee_definition(text):
            continue

        state, ref = caller_render_state(text, TEMPLATE_SOURCES[filename])

        if state in ("master", "stale"):
            warnings.append(_pin_warning(rel, state, ref, f"celestia-devtools init --with-workflows --gate {name} --force"))
            continue

        if state == "drifted":
            warnings.append(f"{rel} drifted from the {label} template — regenerate with: celestia-devtools init --with-workflows --gate {name} --force (hand-edited inputs are why this is a warning, not a failure)")

    return warnings

def _workflow_drift_problems(workflows_dir: Path) -> list[str]:
    """``init --check`` view: mandatory problems plus logged pin/drift warnings."""
    problems = workflow_drift_problems(workflows_dir)

    for warning in alias_warnings(workflows_dir) + opt_in_drift_warnings(workflows_dir) + unpinned_caller_warnings(workflows_dir):
        logger.warn(warning)

    return problems

def _justfile_shadow_problems(repo_root: Path) -> list[str]:
    """Local copies of the shared recipes that shadow the staged justfile.

    ``common.just`` provides ``fetch`` and imports are optional, so a repository
    that also defines its own ``fetch`` silently overrides the shared one (the
    fleet audit found 37 such copies — 28 still on the retired ``dev`` fallback
    URL and 9 on ``master`` (SR-24)). The Git-Bash interop import is obsolete
    since bash was banned in recipes (SR-25); ``init`` warns about it,
    ``--check`` fails.
    """
    justfile = repo_root / "justfile"

    if not justfile.is_file():
        return []

    text = justfile.read_text(encoding = "utf-8", errors = "replace")
    problems: list[str] = []

    # `fetch := "…"` is a variable assignment, not the recipe; and an indented
    # `fetch:` is a recipe-body line. The fleet has both real `fetch URL='':`
    # recipes and `fetch-icons:`-style prefixes, so the lookaheads are load-bearing.
    if re.search(r"(?m)^fetch(?!\s*:=)(?=\s|:)", text):
        problems.append("justfile defines a local `fetch` recipe — delete it; the shared common.just provides fetch (a local copy shadows it and its fallback URL drifts)")

    if re.search(r"(?m)^\s*import\??\s+[\"'][^\"']*git-bash-interop\.just[\"']", text):
        problems.append("justfile imports the obsolete .just/git-bash-interop.just — delete the import line (bash is banned in shared recipes)")

    return problems

def _ensure_workflows(repo_root: Path, *, force: bool = False, gates: tuple[str, ...] = (), ref: str = PINNED_DEVTOOLS_REF) -> None:
    workflows_dir = repo_root / ".github" / "workflows"
    workflows_dir.mkdir(parents = True, exist_ok = True)

    wanted = list(REQUIRED_WORKFLOWS)

    for gate in gates:
        wanted.append(OPT_IN_GATES[gate])

    for filename, _canonical, label in wanted:
        target = workflows_dir / filename
        template = render_caller(TEMPLATE_SOURCES[filename], ref)

        if target.exists() and not force:
            logger.info(f"{filename} already exists — skipping (use --force to regenerate)")
            continue

        if not target.exists():
            alias = _canonical_alias(workflows_dir, template, filename)

            if alias is not None:
                logger.warn(f"{filename} not generated: {alias.name} already carries the " f"canonical caller — rename it to {filename} instead " "(generating would install a second caller for the same check)")
                continue

        # write_bytes, not write_text: the generated callers are pinned
        # byte-for-byte across the fleet, and write_text's default
        # newline=os.linesep would turn their LF into CRLF on Windows.
        target.write_bytes(template.encode("utf-8"))
        logger.ok(f"generated {label} workflow → {target} (devtools ref @{ref})")

def _check_justfile_import(name: str) -> None:
    """Print a hint if the repo's justfile doesn't import the staged file."""
    justfile = Path.cwd() / "justfile"
    recipe_import = f'import? "./{DEST_DIR}/{name}"'
    interop_import = f'import? "./{DEST_DIR}/{INTEROP_NAME}"'

    if not justfile.is_file():
        logger.info("no justfile found — create one starting with:")
        print(f"\n    {recipe_import}\n")
        return

    content = justfile.read_text(errors = "replace")
    has_recipes = (recipe_import in content) or (f'import "./{DEST_DIR}/{name}"' in content)

    if interop_import in content:
        logger.warn(f"justfile still imports {DEST_DIR}/{INTEROP_NAME} — that file is " f"obsolete (bash is banned in recipes); delete the import line")

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

    lines = gi.read_text(encoding = "utf-8", errors = "replace").splitlines()
    stripped = {ln.strip() for ln in lines}
    return [r for r in GITIGNORE_RULES if r not in stripped]

def _ensure_gitignore() -> None:
    """Append any missing GITIGNORE_RULES to .gitignore (idempotent)."""
    missing = _missing_gitignore_rules()

    if not missing:
        return

    gi = _gitignore_path()
    header = "\n# celestia-devtools: staged-on-demand shared justfile recipes " "(celestia-devtools init / just fetch)\n"

    with gi.open("a", encoding = "utf-8") as f:
        if (gi.stat().st_size > 0) and not gi.read_text(encoding = "utf-8").endswith("\n"):
            f.write("\n")

        f.write(header)

        for rule in missing:
            f.write(f"{rule}\n")

    logger.ok(f"added .gitignore rule(s): {', '.join(missing)}")

if __name__ == "__main__":
    raise SystemExit(main())

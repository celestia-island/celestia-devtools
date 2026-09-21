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
        help="report drift without writing (exit 1 if update needed)",
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
        help="also generate GitHub Actions commit-lint workflow",
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
        _ensure_workflows(Path.cwd(), force=args.force)

    return 0


# 规范形态（canonical）：全 org 42 个 caller **逐字节**相同，改这里等于改全部仓的模板。
# 2026-09-15：此前这里缺 `ready_for_review`，导致 `init --force --with-workflows` 会把
# 已统一的仓退回旧变体，而缺该类型正是 §8.3.6 的死锁形态（draft 转 ready 不触发 lint
# ⇒ 必需 check 不出现 ⇒ 合并被判 BLOCKED）。模板变更必须同步 tests/test_caller_template.py 的钉死用例。
WORKFLOW_COMMIT_LINT = """\
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


def _ensure_workflows(repo_root: Path, *, force: bool = False) -> None:
    workflows_dir = repo_root / ".github" / "workflows"
    workflows_dir.mkdir(parents=True, exist_ok=True)
    target = workflows_dir / "commit-msg-lint.yml"

    if target.exists() and not force:
        logger.info("commit-msg-lint workflow already exists — skipping (use --force to regenerate)")
        return

    # LF, not the platform newline: the caller file is pinned byte-for-byte
    # across the fleet (273 B, 106a93d0…), and write_text's default
    # newline=os.linesep would turn it into CRLF on Windows.
    target.write_bytes(WORKFLOW_COMMIT_LINT.encode("utf-8"))
    logger.ok(f"generated commit-msg lint workflow → {target}")

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
        logger.info(f"add near the top of your justfile:")
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

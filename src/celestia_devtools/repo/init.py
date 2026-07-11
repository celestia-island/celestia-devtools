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
"""

from __future__ import annotations

import argparse
import re
from pathlib import Path

from celestia_devtools import __version__
from celestia_devtools.core import logger

DEST_DIR = ".just"
LINK_NAME = "celestia-devtools.just"
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

    return 0


def _check_justfile_import(name: str) -> None:
    """Print a hint if the repo's justfile doesn't import the staged file."""
    justfile = Path.cwd() / "justfile"
    import_line = f'import? "./{DEST_DIR}/{name}"'
    if not justfile.is_file():
        logger.info("no justfile found — create one starting with:")
        print(f"\n    {import_line}\n")
        return
    content = justfile.read_text(errors="replace")
    # Accept both the new optional import and a legacy non-optional one.
    if import_line in content or f'import "./{DEST_DIR}/{name}"' in content:
        logger.info(f"justfile already imports {DEST_DIR}/{name}")
    else:
        logger.info("add this line near the top of your justfile:")
        print(f"\n    {import_line}\n")


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

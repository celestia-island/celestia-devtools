#!/usr/bin/env python3
"""Hook lifecycle management for the org commit-msg lint hook.

``celestia-devtools hook install`` writes a ``commit-msg`` hook into
``.git/hooks/`` that calls ``celestia-devtools commit-msg-lint check`` on
every commit.  The hook script embeds a sentinel string so it can be detected
and safely managed (overwrite / uninstall / drift-check).

Mirrors noa's ``hook_cmd.rs`` but lives in celestia-devtools because
content-validation is a devtools concern, not noa's.
"""

from __future__ import annotations

import argparse
import os
import stat
import sys
from pathlib import Path
from typing import Optional

# ── Constants ────────────────────────────────────────────────────────────────

HOOK_SENTINEL = "# celestia-devtools-managed"

COMMIT_MSG_TEMPLATE = r"""#!/usr/bin/env bash
# celestia-devtools-managed commit-msg hook: enforce org gitmoji convention.
# Installed by `celestia-devtools hook install`.  Remove with:
#   celestia-devtools hook uninstall
# Skip checks for a single commit with:
#   CELESTIA_COMMIT_MSG_SKIP=1 git commit ...
set -u
COMMIT_MSG_FILE="$1"
if [ -n "${CELESTIA_COMMIT_MSG_SKIP:-}" ]; then
    exit 0
fi
DEVTOOLS="@DEVTOOLS_BIN@"
DEVTOOLS_BASE="${DEVTOOLS%% *}"
if [ -n "$DEVTOOLS_BASE" ] && command -v "$DEVTOOLS_BASE" >/dev/null 2>&1; then
    # unquoted $DEVTOOLS so word-splitting handles "python -m celestia_devtools"
    $DEVTOOLS commit-msg-lint check "$COMMIT_MSG_FILE" || exit 1
fi
exit 0
"""

NOA_SENTINEL = "noa-managed"


# ── Helpers ──────────────────────────────────────────────────────────────────

def _resolve_devtools_bin(requested: Optional[str] = None) -> str:
    """Return the best ``celestia-devtools`` invocation string.

    Precedence:
    1. Explicit ``--devtools-bin`` argument.
    2. ``celestia-devtools`` entry point on PATH (installed package).
    3. ``python -m celestia_devtools`` fallback.
    4. Absolute path of the current interpreter running this code → ``<interp> -m celestia_devtools``.
    """
    if requested:
        return requested
    if _which("celestia-devtools"):
        return "celestia-devtools"
    # Fallback: use the running Python interpreter.
    return f"{sys.executable} -m celestia_devtools"


def _which(name: str) -> Optional[str]:
    """Return the full path to *name* on PATH, or None."""
    path = os.environ.get("PATH", "")
    for directory in path.split(os.pathsep):
        candidate = os.path.join(directory, name)
        if os.path.isfile(candidate) and os.access(candidate, os.X_OK):
            return candidate
    return None


def _is_noa_managed(hook_path: Path) -> bool:
    """Check whether the file at *hook_path* looks like a noa-managed hook."""
    try:
        return NOA_SENTINEL in hook_path.read_text(encoding="utf-8")
    except OSError:
        return False


def _is_devtools_managed(hook_path: Path) -> bool:
    """Check whether the file at *hook_path* is managed by celestia-devtools."""
    try:
        return HOOK_SENTINEL in hook_path.read_text(encoding="utf-8")
    except OSError:
        return False


def _make_executable(path: Path) -> None:
    """Set the executable bits on *path* (Unix).  No-op on Windows."""
    try:
        st = path.stat()
        path.chmod(st.st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    except OSError:
        pass  # Windows or read-only fs — not fatal


# ── Core operations ─────────────────────────────────────────────────────────

def _cooked_template(devtools_bin: str) -> str:
    """Return the hook template with ``@DEVTOOLS_BIN@`` substituted."""
    return COMMIT_MSG_TEMPLATE.replace("@DEVTOOLS_BIN@", devtools_bin)


def install_hook(
    hook_path: Path,
    devtools_bin: Optional[str] = None,
    force: bool = False,
) -> None:
    """Write the commit-msg hook to *hook_path*.

    Raises SystemExit(1) when *hook_path* exists and is not devtools-managed
    (unless *force* is True).  Raises SystemExit(1) when a noa-managed hook
    is detected and *force* is not set.
    """
    resolved = _resolve_devtools_bin(devtools_bin)
    content = _cooked_template(resolved)

    if hook_path.exists():
        if _is_noa_managed(hook_path) and not force:
            print(
                "error: a noa-managed commit-msg hook already exists at "
                f"'{hook_path}'.\n"
                "  noa's hook appends AI co-author trailers and does NOT "
                "block commits.\n"
                "  This hook enforces gitmoji formatting and blocks on "
                "violations.\n"
                "  To use both, chain them manually or run with --force "
                "to overwrite.",
                file=sys.stderr,
            )
            raise SystemExit(1)
        if not _is_devtools_managed(hook_path) and not force:
            print(
                f"error: '{hook_path}' already exists and is not managed "
                "by celestia-devtools.\n"
                "  Re-run with --force to overwrite.",
                file=sys.stderr,
            )
            raise SystemExit(1)

    # Ensure hooks directory exists.
    hook_path.parent.mkdir(parents=True, exist_ok=True)
    # Write atomically via temp + rename.
    tmp_path = hook_path.with_suffix(hook_path.suffix + ".tmp")
    tmp_path.write_text(content, encoding="utf-8")
    tmp_path.replace(hook_path)
    _make_executable(hook_path)


def uninstall_hook(hook_path: Path, force: bool = False) -> None:
    """Remove the commit-msg hook at *hook_path*.

    Refuses unless the file is devtools-managed (or *force*).
    """
    if not hook_path.is_file():
        print(f"info: no hook at '{hook_path}' — nothing to uninstall")
        return

    if not _is_devtools_managed(hook_path) and not force:
        print(
            f"error: '{hook_path}' is not a celestia-devtools managed hook.\n"
            "  Delete it manually or re-run with --force.",
            file=sys.stderr,
        )
        raise SystemExit(1)

    hook_path.unlink()
    print(f"removed {hook_path}")


def show_template(devtools_bin: Optional[str] = None) -> str:
    """Return the hook template that would be installed."""
    return _cooked_template(_resolve_devtools_bin(devtools_bin))


def check_hook(hook_path: Path, devtools_bin: Optional[str] = None) -> int:
    """CI drift-gate: exit 1 if the installed hook differs from the bundled one.

    Returns 0 when up-to-date, 1 when drifted or missing.
    """
    expected = _cooked_template(_resolve_devtools_bin(devtools_bin))

    if not hook_path.is_file():
        print(
            f"error: {hook_path.name} missing — run: "
            "celestia-devtools hook install",
            file=sys.stderr,
        )
        return 1

    actual = hook_path.read_text(encoding="utf-8")
    if actual == expected:
        print(f"ok: {hook_path.name} is up to date")
        return 0

    print(
        f"warning: {hook_path.name} has drifted — run: "
        "celestia-devtools hook install",
        file=sys.stderr,
    )
    return 1


# ── CLI ──────────────────────────────────────────────────────────────────────

def main() -> int:
    """CLI entry point for ``celestia-devtools hook``."""
    parser = argparse.ArgumentParser(
        prog="celestia-devtools hook",
        description="Manage the celestia-devtools commit-msg hook.",
    )
    sub = parser.add_subparsers(dest="cmd", required=True)

    # install
    p_install = sub.add_parser("install", help="install the commit-msg hook")
    p_install.add_argument("--repo", default=".", help="path to the repo (default: .)")
    p_install.add_argument("--force", action="store_true", help="overwrite an existing hook")
    p_install.add_argument("--devtools-bin", default=None, help="celestia-devtools binary to invoke")

    # uninstall
    p_uninstall = sub.add_parser("uninstall", help="remove the commit-msg hook")
    p_uninstall.add_argument("--repo", default=".", help="path to the repo (default: .)")
    p_uninstall.add_argument("--force", action="store_true", help="remove even if not managed by devtools")

    # show
    p_show = sub.add_parser("show", help="print the hook script that would be installed")
    p_show.add_argument("--devtools-bin", default=None, help="celestia-devtools binary to invoke")

    # check (drift-gate for CI)
    p_check = sub.add_parser("check", help="verify installed hook matches bundled version (CI drift-gate)")
    p_check.add_argument("--repo", default=".", help="path to the repo (default: .)")
    p_check.add_argument("--devtools-bin", default=None, help="celestia-devtools binary to invoke")

    args = parser.parse_args()

    if args.cmd == "install":
        repo = Path(args.repo).resolve()
        hook_path = repo / ".git" / "hooks" / "commit-msg"
        if not hook_path.parent.is_dir():
            print(f"error: not a git repository (no .git/hooks/ at {repo})", file=sys.stderr)
            return 1
        try:
            install_hook(hook_path, args.devtools_bin, args.force)
        except SystemExit as e:
            return int(str(e))
        print(f"installed commit-msg hook → {hook_path}")
        return 0

    if args.cmd == "uninstall":
        repo = Path(args.repo).resolve()
        hook_path = repo / ".git" / "hooks" / "commit-msg"
        try:
            uninstall_hook(hook_path, args.force)
        except SystemExit as e:
            return int(str(e))
        return 0

    if args.cmd == "show":
        sys.stdout.write(show_template(args.devtools_bin))
        return 0

    if args.cmd == "check":
        repo = Path(args.repo).resolve()
        hook_path = repo / ".git" / "hooks" / "commit-msg"
        return check_hook(hook_path, args.devtools_bin)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())

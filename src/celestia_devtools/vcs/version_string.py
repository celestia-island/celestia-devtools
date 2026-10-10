"""`version-string` — the family's version-line facility (2026-10-08).

User direction 2026-10-08 (base refined 2026-10-10): every engine drops
its drifting patch counter and reports `<base> <branch>::<hash7>`
(e.g. `0.1.0 master::aB12345`) — the branch and the exact commit ARE the
identity, and the base is the honest FULL package version, never a
truncated major.minor.

`build.rs` scripts across the family call this (via the installed
celestia-devtools) at every build and inject the result with
`cargo:rustc-env=VERSION=…`; binaries read `env!("VERSION")`. Falls back
to direct git when the devtools binary is not on the build host's PATH.
"""

from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()


def version_string(repo: Path, base: str) -> str:
    branch = _git(repo, "rev-parse", "--abbrev-ref", "HEAD")
    if branch == "HEAD":
        # Detached build worktrees: resolve the branch name, not "HEAD".
        # Local heads first; a freshly-fetched squash sha may only exist
        # on the remote — fall through to remotes before giving up.
        resolved = None
        for refs in ("refs/heads/*", "refs/remotes/origin/*"):
            named = _git(
                repo,
                "name-rev",
                "--name-only",
                f"--refs={refs}",
                "HEAD",
            )
            if named and named not in ("undefined", "remotes/origin/HEAD"):
                resolved = named.rsplit("~", 1)[0].replace("remotes/origin/", "")
                break
        if not resolved:
            # A freshly-fetched squash sha may sit only on origin/master
            # while name-rev answers through the symbolic HEAD — resolve
            # that explicitly.
            try:
                sym = _git(repo, "symbolic-ref", "--short", "refs/remotes/origin/HEAD")
                resolved = sym.split("/", 1)[-1] if sym else None
            except subprocess.CalledProcessError:
                resolved = None
        branch = resolved or "detached"
    short = _git(repo, "rev-parse", "--short=7", "HEAD")
    return f"{base} {branch}::{short}"


def default_base(repo: Path) -> str:
    """The identity base for a checkout without an explicit --base: the
    honest FULL version parsed from the workspace Cargo.toml (2026-10-10
    direction — plana #424 passes CARGO_PKG_VERSION through verbatim; this
    default keeps the CLI honest for direct calls too). The "0.1.0"
    fallback matches the family baseline for trees without Cargo.toml."""
    for candidate in (repo / "Cargo.toml", repo / "packages/core/Cargo.toml"):
        if candidate.exists():
            for line in candidate.read_text(encoding="utf-8").splitlines():
                if line.startswith("version"):
                    return line.split("=", 1)[1].strip().strip('"')
            else:
                continue
            break
    return "0.1.0"


def main() -> int:
    ap = argparse.ArgumentParser(
        prog="celestia-devtools version-string",
        description="Emit `<base> <branch>::<hash7>` for a repo checkout.",
    )
    ap.add_argument("--dir", default=".", help="repo checkout (default: cwd)")
    ap.add_argument(
        "--base",
        default=None,
        help="base version (default: the full version parsed from the workspace Cargo.toml)",
    )
    args = ap.parse_args()
    repo = Path(args.dir).resolve()

    base = args.base or default_base(repo)

    try:
        print(version_string(repo, base))
    except subprocess.CalledProcessError as e:
        print(f"error: git failed in {repo}: {e.stderr or e}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())

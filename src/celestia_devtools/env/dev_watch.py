#!/usr/bin/env python3
"""File-watch dev loop — Python port of the former bash ``dev-watch`` recipe.

Splits args at ``--``: everything before is watch paths, everything after is
the supervised command, then hands off to malkuth (notify-based watcher)::

    just dev-watch src -- cargo run
    celestia-devtools dev-watch src docs -- pnpm vite build

malkuth resolution order (first that exists wins):

    $MALKUTH_BIN → $MALKUTH_ROOT/target/release/malkuth[.exe] → $PATH malkuth
    → ../malkuth/target/release/malkuth[.exe]

Unlike the bash original this makes no MSYS ``/usr/bin`` PATH assumption —
command resolution goes through the host PATH, which is correct on both
POSIX and Windows.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys


def malkuth_bin() -> str | None:
    env_bin = os.environ.get("MALKUTH_BIN", "")
    if env_bin:
        return env_bin
    exe = "malkuth.exe" if os.name == "nt" else "malkuth"
    env_root = os.environ.get("MALKUTH_ROOT", "")
    if env_root:
        return os.path.join(env_root, "target", "release", exe)
    found = shutil.which("malkuth")
    if found:
        return found
    return os.path.join("..", "malkuth", "target", "release", exe)


def main() -> int:
    args = sys.argv[1:]
    if "--" not in args:
        print("usage: just dev-watch <watch-paths...> -- <command...>", file=sys.stderr)
        print("  e.g.  just dev-watch src -- cargo run", file=sys.stderr)
        return 2
    sep = args.index("--")
    watch_paths, cmd = args[:sep], args[sep + 1:]
    if not cmd:
        print("usage: just dev-watch <watch-paths...> -- <command...>", file=sys.stderr)
        return 2

    malkuth = malkuth_bin()
    if not os.path.isfile(malkuth) and not shutil.which(malkuth):
        print("[dev-watch] malkuth not found.", file=sys.stderr)
        print("[dev-watch] Build it: cd ../malkuth && cargo build --release --features cli", file=sys.stderr)
        print("[dev-watch] Or set: export MALKUTH_BIN=/path/to/malkuth", file=sys.stderr)
        return 1

    watch_flags: list[str] = []
    for p in watch_paths:
        watch_flags += ["--watch", p]
    print(f"[dev-watch] supervising: {' '.join(cmd)}")
    print(f"[dev-watch] watching: {' '.join(watch_paths)}")
    completed = subprocess.run([malkuth, *watch_flags, "--drain-secs", "2", "--", *cmd])
    return completed.returncode


if __name__ == "__main__":
    raise SystemExit(main())

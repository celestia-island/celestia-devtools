#!/usr/bin/env python3
"""Fix dev branches v3: recover old dev from reflog, then correctly rebase
ALL dev-unique commits after the youngest shared-tree fork point.
"""
import subprocess
import os
from pathlib import Path


def git(*args: str) -> str:
    return subprocess.run(
        ["git"] + list(args),
        capture_output=True, text=True, check=True,
    ).stdout.strip()


def git_ok(*args: str) -> bool:
    return subprocess.run(
        ["git"] + list(args),
        capture_output=True,
    ).returncode == 0


def fix_dev(repo_path: str, mb: str, old_dev_sha: str) -> bool:
    os.chdir(repo_path)

    # ── Build tree→commit map from the new master ─────────────────────────
    tree_map: dict[str, str] = {}
    for line in git("log", mb, "--format=%H %T").split("\n"):
        parts = line.split()
        if len(parts) >= 2:
            tree_map[parts[1]] = parts[0]

    # ── Walk old dev from tip BACKWARDS (newest first) to find fork point ─
    # We want the NEWEST commit on dev whose tree matches master.
    # ALL commits after that (toward dev tip) are dev-unique.
    old_dev_commits = git(
        "log", old_dev_sha, "--format=%H %T", "--no-merges", "--reverse"
    ).split("\n")

    fork_master_sha = None
    commits = []
    for line in old_dev_commits:
        if not line.strip():
            continue
        sha, tree = line.split()
        commits.append((sha, tree))

    # Find the YOUNGEST (last in --reverse order) shared commit
    dev_unique_start = 0
    for i in range(len(commits) - 1, -1, -1):
        sha, tree = commits[i]
        if tree in tree_map:
            fork_master_sha = tree_map[tree]
            dev_unique_start = i + 1  # everything after this is dev-unique
            print(f"    fork: dev={sha[:7]} (idx {i}/{len(commits)}) → master={fork_master_sha[:7]}")
            break
    else:
        print("    ⚠️ no fork point found")
        return False

    dev_unique = commits[dev_unique_start:]
    fork_dev_sha = commits[dev_unique_start - 1][0] if dev_unique_start > 0 else commits[0][0]

    if not dev_unique:
        print("    no dev-unique commits after fork — resetting dev to master")
        git("checkout", "-q", "--force", mb)
        git("branch", "-f", "dev", mb)
        return True

    print(f"    {len(dev_unique)} unique commits to rebase onto {fork_master_sha[:7]}")

    # ── Rebase old dev onto the new master fork point ────────────────────
    # Checkout the old dev tip
    try:
        git("checkout", "-q", "--force", old_dev_sha)
    except subprocess.CalledProcessError:
        print(f"    ⚠️ cannot checkout old dev {old_dev_sha[:7]}")
        return False

    # Rebase: everything after fork_dev_sha goes onto fork_master_sha
    try:
        git("rebase", "--onto", fork_master_sha, fork_dev_sha)
    except subprocess.CalledProcessError:
        subprocess.run(["git", "rebase", "--abort"], capture_output=True)
        subprocess.run(["git", "checkout", "-q", "--force", mb], capture_output=True)
        print("    ⚠️ rebase conflict")
        return False

    git("branch", "-f", "dev", "HEAD")
    git("checkout", "-q", mb)
    return True


def main() -> int:
    # (repo, mb, old_dev_sha_from_reflog)
    repos = [
        ("aris", "master", "e2c175f"),
        ("arona", "master", "1acd638"),
        ("evernight", "master", "0f7f171"),
        ("kei", "master", "4ef83b6"),
        ("yuuka", "master", "9814f59"),
        ("celestia-devtools", "master", "1c36c39"),
        ("celestia-island.github.io", "main", "f7e936f"),
        ("kirino", "master", "91cbead"),
        ("noa", "master", "dc24e39"),
        ("ratatui-markdown", "master", "6afffac"),
        ("hifumi", "master", "46d31dc"),
        ("aoba", "master", "14fc9ff"),
    ]
    base = Path("D:/源代码/工程项目/celestia")

    for name, mb, old_dev in repos:
        repo_path = str(base / name)
        os.chdir(repo_path)
        # Verify old dev exists
        if not git_ok("rev-parse", "--verify", old_dev):
            print(f"{name:<25} ❌ old dev not found")
            continue
        print(f"\n{name}:")
        ok = fix_dev(repo_path, mb, old_dev)
        status = "✅" if ok else "❌"
        ahead = git("rev-list", "--count", f"{mb}..dev") if ok else "—"
        print(f"{name:<25} dev: {status} (+{ahead})")
        try:
            git("checkout", "-q", mb)
        except subprocess.CalledProcessError:
            git("checkout", "-q", "--force", mb)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())

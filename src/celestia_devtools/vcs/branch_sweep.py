#!/usr/bin/env python3
"""git-sweep -- sweep local branches whose work already lives on the base branch.

Squash merges plus delete-on-merge leave orphan local branches behind on
every machine that ever pulled the repo. A branch is swept only when ALL of
these hold:

  * its upstream ref is gone from the remote (or it never had one);
  * it is not the current branch and not checked out in any worktree;
  * it adds nothing: its tip is an ancestor of the base branch, or every
    file it touched (three-dot vs base) is byte-identical between the
    branch and the base branch.

Anything else is reported KEEP for a human decision -- in particular a
squash-merged branch whose base later evolved further is NOT auto-swept;
the tool stays conservative on purpose.

Usage::

    celestia-devtools git-sweep [--repo PATH | --root DIR] [--base REF]
                                [--apply] [--backup-dir DIR] [--json]

Default is a dry run. '--apply' deletes swept branches after writing a
unified patch of each into the backup directory (default:
'<repo>/.git/sweep-backups/<utc-stamp>/').
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

SEP = "@@\n"  # record separator for for-each-ref


def _git(repo: Path, *args: str) -> subprocess.CompletedProcess:
    # UTF-8 is pinned: git speaks UTF-8, but a Windows console's locale
    # codec (cp936 on a Chinese install) cannot decode it — the reader
    # thread died and left stdout=None, crashing the sweep with an
    # AttributeError on the first CJK repo path or branch name.
    # core.quotepath=off keeps non-ASCII paths raw: with the default
    # octal-escaped spelling ("\346\226\207.txt") a CJK filename handed
    # back to `git diff -- <path>` matches nothing, so unique content
    # read as "identical on base" and the branch was swept (false
    # DELETE).
    return subprocess.run(
        ["git", "-c", "core.quotepath=false", "-C", str(repo), *args],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )


def _base_ref(repo: Path, requested: str | None) -> str | None:
    if requested:
        return requested
    for candidate in ("master", "main"):
        if _git(repo, "rev-parse", "--verify", "--quiet", candidate).returncode == 0:
            return candidate
    return None


def _current_branch(repo: Path) -> str:
    r = _git(repo, "rev-parse", "--abbrev-ref", "HEAD")
    return r.stdout.strip() if r.returncode == 0 else ""


def _worktree_branches(repo: Path) -> set[str]:
    r = _git(repo, "worktree", "list", "--porcelain")
    out: set[str] = set()
    for line in r.stdout.splitlines():
        if line.startswith("branch refs/heads/"):
            out.add(line[len("branch refs/heads/"):])
    return out


def _local_branches(repo: Path) -> list[tuple[str, str, str]]:
    r = _git(repo, "for-each-ref", "refs/heads",
             "--format=%(refname:short)" + SEP + "%(upstream:short)" + SEP + "%(upstream:track)")
    branches: list[tuple[str, str, str]] = []
    for record in r.stdout.split(SEP):
        parts = record.split("\n", 2)
        while len(parts) < 3:
            parts.append("")
        branches.append((parts[0], parts[1], parts[2].strip("[]")))
    return [b for b in branches if b[0]]


def _upstream_gone(repo: Path, upstream: str, track: str) -> bool:
    if not upstream:
        return True
    if "gone" in track:
        return True
    return _git(repo, "rev-parse", "--verify", "--quiet", upstream).returncode != 0


def _is_ancestor(repo: Path, branch: str, base: str) -> bool:
    return _git(repo, "merge-base", "--is-ancestor", branch, base).returncode == 0


def _touched_files(repo: Path, base: str, branch: str) -> list[str]:
    r = _git(repo, "diff", "--name-only", f"{base}...{branch}")
    return [f for f in r.stdout.splitlines() if f]


def _file_identical(repo: Path, base: str, branch: str, f: str) -> bool:
    r = _git(repo, "diff", "--shortstat", base, branch, "--", f)
    out = r.stdout.strip()
    return r.returncode == 0 and (out == "" or out.startswith("0 files changed"))


def sweep_repo(
    repo: Path,
    base: str | None = None,
    apply: bool = False,
    backup_dir: Path | None = None,
) -> dict:
    """Sweep one repository; returns a report dict (also used by tests)."""
    repo = Path(repo)
    base_ref = _base_ref(repo, base)
    report: dict = {"repo": str(repo), "base": base_ref, "verdicts": [],
                    "deleted": [], "failed": [], "backup_dir": None}
    if base_ref is None:
        # Not sweepable — and not a sweep failure either: `--root` walks
        # arbitrary checkouts (scratch/verify dirs) that legitimately
        # carry no master/main. main() decides how loudly to report it.
        report["skipped"] = "no base branch found (tried master, main)"
        return report

    current = _current_branch(repo)
    busy = _worktree_branches(repo)

    for name, upstream, track in _local_branches(repo):
        if name == base_ref:
            continue
        if name == current or name in busy:
            report["verdicts"].append({"branch": name, "verdict": "KEEP",
                                       "reason": "checked out (current or worktree)"})
            continue
        if not _upstream_gone(repo, upstream, track):
            report["verdicts"].append({"branch": name, "verdict": "KEEP",
                                       "reason": f"upstream alive: {upstream}"})
            continue

        files = _touched_files(repo, base_ref, name)
        if _is_ancestor(repo, name, base_ref):
            reason = "tip is ancestor of base"
        elif not files:
            reason = "touches no files vs base"
        else:
            identical = [f for f in files if _file_identical(repo, base_ref, name, f)]
            if len(identical) == len(files):
                reason = f"all {len(files)} touched files identical on base"
            else:
                differ = [f for f in files if f not in set(identical)]
                report["verdicts"].append({"branch": name, "verdict": "KEEP",
                                           "reason": f"unique content in: {', '.join(differ[:3])}"})
                continue

        report["verdicts"].append({"branch": name, "verdict": "DELETE", "reason": reason})
        if not apply:
            continue

        if backup_dir is None:
            stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
            backup_dir = repo / ".git" / "sweep-backups" / stamp
        backup_dir = Path(backup_dir)
        backup_dir.mkdir(parents=True, exist_ok=True)
        report["backup_dir"] = str(backup_dir)
        patch = _git(repo, "diff", f"{base_ref}...{name}")
        tip = _git(repo, "rev-parse", name)
        patch_name = name.replace("/", "__") + ".patch"
        (backup_dir / patch_name).write_text(
            "tip " + tip.stdout.strip() + "\n" + patch.stdout, encoding="utf-8")
        dele = _git(repo, "branch", "-D", name)
        if dele.returncode == 0:
            report["deleted"].append(name)
        else:
            report["failed"].append({"branch": name, "error": dele.stderr.strip()})
    return report


def main(argv: list[str] | None = None) -> int:
    # A cp936/legacy Windows console chokes printing branch names or repo
    # paths outside its codec (an emoji-named branch would crash the
    # report); degrade unencodable characters instead of dying.
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(errors="replace")  # type: ignore[union-attr]
        except (AttributeError, ValueError):  # pragma: no cover - wrapped streams
            pass
    ap = argparse.ArgumentParser(prog="git-sweep", description=__doc__.splitlines()[0])
    ap.add_argument("--repo", help="repository to sweep (default: cwd)")
    ap.add_argument("--root",
                    help="sweep every git repository directly under DIR "
                         "(checkouts without master/main are skipped)")
    ap.add_argument("--base", help="base branch (default: master, then main)")
    ap.add_argument("--apply", action="store_true",
                    help="delete swept branches (default: dry run)")
    ap.add_argument("--backup-dir",
                    help="patch backup directory (default: <repo>/.git/sweep-backups/<stamp>)")
    ap.add_argument("--json", action="store_true", help="machine-readable output")
    args = ap.parse_args(argv)

    if args.root:
        repos = sorted(p for p in Path(args.root).iterdir() if (p / ".git").exists())
        if not repos:
            print(f"error: no git repositories under {args.root}", file=sys.stderr)
            return 1
    else:
        repos = [Path(args.repo) if args.repo else Path.cwd()]

    failed = False
    reports = []
    for repo in repos:
        report = sweep_repo(repo, base=args.base, apply=args.apply,
                            backup_dir=Path(args.backup_dir) if args.backup_dir else None)
        reports.append(report)
        for v in report["verdicts"]:
            print(f"{v['verdict']:6} {v['branch']}  ({v['reason']})")
        for f in report["failed"]:
            print(f"ERROR  {f['branch']}  {f['error']}", file=sys.stderr)
            failed = True
        if report.get("skipped"):
            # A base-less checkout is not sweepable, not a failure: `--root`
            # legitimately walks scratch dirs. An explicit `--repo` run still
            # surfaces it through the exit code — the caller asked for THAT
            # repo and got nothing swept.
            reason = report["skipped"]
            print(f"skip   {report['repo']}: {reason}", file=sys.stderr)
            if not args.root:
                failed = True

    if args.json:
        print(json.dumps(reports, ensure_ascii=False, indent=2))
    deletes = sum(1 for r in reports for v in r["verdicts"] if v["verdict"] == "DELETE")
    keeps = sum(1 for r in reports for v in r["verdicts"] if v["verdict"] == "KEEP")
    mode = "applied" if args.apply else "dry run ('--apply' to delete)"
    print(f"git-sweep: {mode}; {deletes} swept, {keeps} kept")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())

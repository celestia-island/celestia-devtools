#!/usr/bin/env python3
"""Guarded PR merge — validates the squash subject before calling ``gh pr merge``.

Usage::

    celestia-devtools pr-merge "🐛 Fix something." --repo owner/repo --squash
    celestia-devtools pr-merge --subject "✨ Add feature." --repo owner/repo --auto
"""

from __future__ import annotations

import argparse
import subprocess
import sys

from celestia_devtools.vcs.commit_msg import lint


def main() -> int:
    parser = argparse.ArgumentParser(
        prog="celestia-devtools pr-merge",
        description="Validate subject, then merge via gh pr merge.",
    )
    parser.add_argument(
        "subject", nargs="?",
        help="commit subject (the --subject for gh pr merge)",
    )
    parser.add_argument(
        "--subject", dest="subject_opt", default=None,
        help="commit subject as a flag (alternative to positional)",
    )
    parser.add_argument(
        "--repo", default=None,
        help="GitHub owner/repo (passed to gh pr merge)",
    )
    parser.add_argument(
        "--squash", action="store_true",
        help="squash-merge the PR",
    )
    parser.add_argument(
        "--admin", action="store_true",
        help="use admin privileges to bypass branch protection",
    )
    parser.add_argument(
        "--auto", action="store_true",
        help="auto-merge when requirements are met (enqueue)",
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="validate subject only, do not merge",
    )
    parser.add_argument(
        "pr_number", nargs="?", default=None,
        help="PR number to merge",
    )

    args = parser.parse_args()

    subject = args.subject or args.subject_opt
    if not subject:
        parser.print_help()
        print("\nerror: subject is required", file=sys.stderr)
        return 2

    violations = lint(subject)
    if violations:
        print("\n  " + "\n  ".join(violations), file=sys.stderr)
        print(
            "\n  Merge REJECTED — fix the message first.\n"
            "  Format:  <gitmoji> <Capitalized English summary.>\n"
            "  Example:  🐛 Fix the parser crash.\n",
            file=sys.stderr,
        )
        return 1

    if args.dry_run:
        print(f"  ✅ subject valid: {subject}")
        return 0

    cmd = ["gh", "pr", "merge"]
    if args.pr_number:
        cmd.append(str(args.pr_number))
    if args.repo:
        cmd.extend(["--repo", args.repo])
    if args.squash:
        cmd.append("--squash")
    if args.admin:
        cmd.append("--admin")
    if args.auto:
        cmd.append("--auto")
    cmd.extend(["--subject", subject])

    result = subprocess.run(cmd)
    return result.returncode


if __name__ == "__main__":
    raise SystemExit(main())

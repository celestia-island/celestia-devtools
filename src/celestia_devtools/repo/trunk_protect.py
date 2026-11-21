#!/usr/bin/env python3
"""Apply the org trunk branch-protection profile to a repository.

Checklist automation for the 2026-11-21 org rollout: every active public
repo's trunk carries the same branch protection — pull requests required
(zero approvals), admin enforcement, no force pushes, linear history,
no branch deletion. New public repos get this applied at creation time
instead of relying on the manual checklist in ``_ledger/infra.md``.

The actual default branch is resolved through the branches API, not the
repos API's ``default_branch`` field: that field has been observed to
lie (celestia-artifacts reported ``master`` while its only branch was
``main``), and protecting a non-existent branch fails with a confusing
"Branch not found".

Usage::

    celestia-devtools trunk-protect --repo lagrange                 # apply
    celestia-devtools trunk-protect --repo owner/name --branch main # explicit branch
    celestia-devtools trunk-protect --repo lagrange --check --json  # report only

Private repos: branch protection requires a GitHub plan this org's free
tier does not include, so the API rejects the PUT with a 403 upgrade
message. The command reports that shape explicitly instead of dressing
it up as a generic failure.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from typing import Any, Dict, Sequence, Tuple

DEFAULT_ORG = "celestia-island"

# The org trunk profile (2026-11-21 rollout, see _ledger/infra.md). One
# dict, applied verbatim — a fork of this profile in some other tool
# would be two versions of one fact.
PROFILE: Dict[str, Any] = {
    "required_status_checks": None,
    "enforce_admins": True,
    "required_pull_request_reviews": {"required_approving_review_count": 0},
    "restrictions": None,
    "required_linear_history": True,
    "allow_force_pushes": False,
    "allow_deletions": False,
    "lock_branch": False,
}

private_plan_note = (
    "private repo: branch protection needs a paid GitHub plan (Team/Pro); "
    "the free tier rejects this PUT — protect via discipline + local hooks only"
)


def _gh(args: Sequence[str], stdin: str | None = None) -> Tuple[int, str, str]:
    """Run ``gh`` and return (returncode, stdout, stderr)."""
    proc = subprocess.run(
        ["gh", *args],
        input = stdin,
        capture_output = True,
        text = True,
        encoding = "utf-8",
        errors = "replace",
        check = False,
    )
    return proc.returncode, proc.stdout, proc.stderr


def _gh_json(args: Sequence[str]) -> Tuple[int, Any, str]:
    rc, out, err = _gh(args)
    body: Any = None
    if out.strip():
        try:
            body = json.loads(out)
        except json.JSONDecodeError:
            body = None
    return rc, body, err


def _slug(repo: str, org: str) -> str:
    """Accept both ``name`` (prefixed with the org default) and ``owner/name``."""
    return repo if "/" in repo else f"{org}/{repo}"


def resolve_branch(slug: str, override: str | None) -> Tuple[str | None, str]:
    """Resolve the branch to protect.

    Prefers the repos API ``default_branch`` when the branches API agrees
    it exists; falls back to the first listed branch when the field lies.
    Returns (branch | None, human note).
    """
    rc, body, err = _gh_json(["api", f"repos/{slug}/branches?per_page=100"])
    if rc != 0 or not isinstance(body, list):
        return None, f"cannot list branches: {err.strip() or 'gh api failed'}"
    if not body:
        return None, "repository has no branches (empty repo?)"
    names = [b.get("name") for b in body if isinstance(b, dict)]

    if override:
        if override in names:
            return override, "explicit --branch"
        return None, f"--branch {override!r} does not exist (branches: {', '.join(names)})"

    rc2, repo_body, err2 = _gh_json(["api", f"repos/{slug}"])
    claimed = repo_body.get("default_branch") if isinstance(repo_body, dict) else None
    if claimed and claimed in names:
        return claimed, "repos API default_branch (confirmed by branches API)"
    # The field lied (or is stale) — protect the only/first real branch and
    # say so, so the surprise is in the log rather than in the diff.
    return names[0], (
        f"repos API claimed default_branch={claimed!r} but branches API has "
        f"{names!r} — fell back to {names[0]!r}"
    )


def protection_state(slug: str, branch: str) -> Tuple[bool, str]:
    rc, body, err = _gh_json(["api", f"repos/{slug}/branches/{branch}/protection"])
    if rc == 0 and isinstance(body, dict):
        return True, "protected"
    return False, err.strip() or f"GET returned {rc}"


def is_private(slug: str) -> bool:
    rc, body, _ = _gh_json(["api", f"repos/{slug}"])
    return bool(isinstance(body, dict) and body.get("private"))


def apply_profile(slug: str, branch: str) -> Tuple[bool, str]:
    rc, _, err = _gh(
        ["api", "-X", "PUT", "--input", "-", f"repos/{slug}/branches/{branch}/protection"],
        stdin = json.dumps(PROFILE),
    )
    if rc == 0:
        return True, "profile applied"
    note = err.strip() or f"PUT returned {rc}"
    lowered = note.lower()
    if "403" in note or "upgrade" in lowered or "not accessible" in lowered:
        if is_private(slug):
            note = f"{note} — {private_plan_note}"
    return False, note


def _emit(payload: Dict[str, Any], as_json: bool) -> None:
    if as_json:
        print(json.dumps(payload, ensure_ascii = False))
        return
    for key, value in payload.items():
        print(f"{key}: {value}")


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog = "trunk-protect",
        description = "Apply the org trunk branch-protection profile to a repo's default branch.",
    )
    parser.add_argument("--repo", required = True, help = "repo name (org default) or owner/name")
    parser.add_argument("--branch", default = None, help = "explicit branch; default = actual default branch via branches API")
    parser.add_argument("--org", default = DEFAULT_ORG, help = f"org prefix for bare repo names (default: {DEFAULT_ORG})")
    parser.add_argument("--check", action = "store_true", help = "report current state only, never PUT")
    parser.add_argument("--json", action = "store_true", help = "machine-readable output")
    args = parser.parse_args(argv)

    slug = _slug(args.repo, args.org)

    rc, repo_body, err = _gh_json(["api", f"repos/{slug}"])
    if rc != 0 or not isinstance(repo_body, dict):
        print(f"error: cannot read {slug}: {err.strip() or rc}", file = sys.stderr)
        return 1
    if repo_body.get("archived"):
        print(f"skipped: {slug} is archived (read-only, protection unnecessary)")
        return 0

    branch, note = resolve_branch(slug, args.branch)
    if branch is None:
        print(f"error: {slug}: {note}", file = sys.stderr)
        return 1

    was_protected, before_note = protection_state(slug, branch)

    if args.check:
        _emit(
            {
                "repo": slug,
                "branch": branch,
                "branch_resolution": note,
                "protected": was_protected,
                "detail": before_note,
                "mode": "check",
            },
            args.json,
        )
        return 0 if was_protected else 1

    # Always PUT: the profile is a fixed constant, so re-asserting on an
    # already-protected branch is idempotent and self-heals drifted settings.
    applied, apply_note = apply_profile(slug, branch)
    now_protected, verify_note = protection_state(slug, branch)

    ok = applied and now_protected
    _emit(
        {
            "repo": slug,
            "branch": branch,
            "branch_resolution": note,
            "protected_before": was_protected,
            "protected_after": now_protected,
            "applied": applied,
            "detail": apply_note if not applied else verify_note,
            "mode": "apply",
        },
        args.json,
    )
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())

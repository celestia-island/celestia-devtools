"""Tests for ``celestia-devtools trunk-protect``.

The gh CLI is stubbed at the module's ``_gh`` seam (the only place the
module touches the outside world), so every test below runs without
network access and without a real ``gh`` install.
"""

from __future__ import annotations

import json
from typing import Any, Dict, List, Sequence, Tuple

import pytest

from celestia_devtools.repo import trunk_protect as mod


class FakeGh:
    """Scripted ``gh api`` responses keyed by (method-shape, path)."""

    def __init__(self) -> None:
        self.calls: List[Sequence[str]] = []
        self.bodies: List[str | None] = []
        self.repos: Dict[str, Dict] = {}
        self.branches: Dict[str, List[str]] = {}
        self.protection: Dict[Tuple[str, str], Dict] = {}
        # (method, path) → (rc, body) overrides; unmatched paths GET-fail
        self.overrides: Dict[Tuple[str, str], Tuple[int, Any]] = {}

    def add_repo(self, slug: str, *, default_branch: str, branches: List[str] | None = None,
                 private: bool = False, archived: bool = False) -> None:
        self.repos[slug] = {
            "default_branch": default_branch,
            "private": private,
            "archived": archived,
        }
        self.branches[slug] = branches if branches is not None else [default_branch]

    # --- the seam -------------------------------------------------------
    def gh(self, args: Sequence[str], stdin: str | None = None) -> Tuple[int, str, str]:
        self.calls.append(list(args))
        self.bodies.append(stdin)
        effective_path = args[5] if args[:2] == ["api", "-X"] else args[1]
        for (method, known), (rc, payload) in self.overrides.items():
            if args[0] == method and known == effective_path:
                return rc, json.dumps(payload), ""
        if args[:2] == ["api", "-X"] and args[2] == "PUT":
            path = args[5]
            kind, key = path.split("/branches/")
            slug = kind[len("repos/"):]
            branch = key.split("/protection")[0]
            self.protection[(slug, branch)] = json.loads(stdin or "{}")
            return 0, "", ""
        assert args[0] == "api", f"unexpected gh invocation: {args}"
        path = args[1].split("?")[0]
        if path.startswith("repos/") and path.endswith("/branches") and "protection" not in path:
            slug = path[len("repos/"):-len("/branches")]
            names = self.branches.get(slug)
            if names is None:
                return 1, "", "no branches known"
            return 0, json.dumps([{"name": n} for n in names]), ""
        if path.startswith("repos/") and path.endswith("/protection"):
            kind, key = path.split("/branches/")
            slug = kind[len("repos/"):]
            branch = key.split("/protection")[0]
            body = self.protection.get((slug, branch))
            if body is None:
                return 404, "", "Not Found"
            return 0, json.dumps(body), ""
        if path.startswith("repos/") and path.count("/") == 2:
            slug = path[len("repos/"):]
            body = self.repos.get(slug)
            if body is None:
                return 1, "", "no such repo"
            return 0, json.dumps(body), ""
        return 1, "", f"fake_gh: unmatched path {path}"


@pytest.fixture()
def fake_gh(monkeypatch: pytest.MonkeyPatch) -> FakeGh:
    fake = FakeGh()
    monkeypatch.setattr(mod, "_gh", fake.gh)
    return fake


class TestResolveBranch:
    def test_prefers_default_branch_when_branches_api_confirms_it(self, fake_gh: FakeGh):
        fake_gh.add_repo("o/r", default_branch = "main", branches = ["main", "dev"])
        branch, note = mod.resolve_branch("o/r", None)
        assert branch == "main"
        assert "confirmed" in note

    def test_falls_back_when_default_branch_field_lies(self, fake_gh: FakeGh):
        # The celestia-artifacts shape: repos API said master, only branch was main.
        fake_gh.add_repo("o/r", default_branch = "master", branches = ["main"])
        branch, note = mod.resolve_branch("o/r", None)
        assert branch == "main"
        assert "master" in note and "fell back" in note

    def test_explicit_branch_wins_over_resolution(self, fake_gh: FakeGh):
        fake_gh.add_repo("o/r", default_branch = "main", branches = ["main", "other"])
        branch, _ = mod.resolve_branch("o/r", "other")
        assert branch == "other"

    def test_explicit_branch_that_does_not_exist_is_an_error(self, fake_gh: FakeGh):
        fake_gh.add_repo("o/r", default_branch = "main", branches = ["main"])
        branch, note = mod.resolve_branch("o/r", "nope")
        assert branch is None
        assert "does not exist" in note

    def test_empty_repo_is_an_error(self, fake_gh: FakeGh):
        fake_gh.add_repo("o/empty", default_branch = "main", branches = [])
        branch, note = mod.resolve_branch("o/empty", None)
        assert branch is None
        assert "no branches" in note


class TestApplyProfile:
    def test_apply_puts_the_canonical_profile_and_verifies(self, fake_gh: FakeGh):
        fake_gh.add_repo("o/r", default_branch = "main")
        rc = mod.main(["--repo", "o/r", "--json"])
        assert rc == 0
        stored = fake_gh.protection[("o/r", "main")]
        assert stored == mod.PROFILE
        assert stored["enforce_admins"] is True
        assert stored["allow_force_pushes"] is False

    def test_already_protected_is_reasserted_idempotently(self, fake_gh: FakeGh):
        fake_gh.add_repo("o/r", default_branch = "main")
        fake_gh.protection[("o/r", "main")] = {"drifted": True}
        rc = mod.main(["--repo", "o/r", "--json"])
        assert rc == 0
        assert fake_gh.protection[("o/r", "main")] == mod.PROFILE

    def test_check_mode_never_puts(self, fake_gh: FakeGh):
        fake_gh.add_repo("o/r", default_branch = "main")
        rc = mod.main(["--repo", "o/r", "--check", "--json"])
        assert rc == 1  # unprotected → check reports failure
        assert not fake_gh.protection, "check mode must not PUT"

    def test_check_mode_on_protected_branch_exits_zero(self, fake_gh: FakeGh):
        fake_gh.add_repo("o/r", default_branch = "main")
        fake_gh.protection[("o/r", "main")] = mod.PROFILE
        rc = mod.main(["--repo", "o/r", "--check", "--json"])
        assert rc == 0
        assert not fake_gh.protection[("o/r", "main")].get("drifted")

    def test_archived_repo_is_skipped_without_put(self, fake_gh: FakeGh):
        fake_gh.add_repo("o/old", default_branch = "main", archived = True)
        rc = mod.main(["--repo", "o/old", "--json"])
        assert rc == 0
        assert not fake_gh.protection

    def test_private_repo_403_carries_the_plan_note(self, fake_gh: FakeGh):
        fake_gh.add_repo("o/priv", default_branch = "main", private = True)
        # free-plan private repos: the PUT is rejected server-side
        fake_gh.overrides[("api", "repos/o/priv/branches/main/protection")] = (403, {"message": "Not accessible"})
        rc = mod.main(["--repo", "o/priv", "--json"])
        assert rc == 1
        # the failure output must explain the plan limitation, not just echo 403
        # (captured via capsys in the dedicated test below)

    def test_private_plan_note_reaches_stdout(self, fake_gh: FakeGh, capsys: pytest.CaptureFixture):
        fake_gh.add_repo("o/priv", default_branch = "main", private = True)
        fake_gh.overrides[("api", "repos/o/priv/branches/main/protection")] = (403, {"message": "Not accessible"})
        mod.main(["--repo", "o/priv", "--json"])
        out = capsys.readouterr().out
        assert "paid GitHub plan" in out


class TestSlug:
    def test_bare_name_gets_org_prefix(self):
        assert mod._slug("lagrange", "celestia-island") == "celestia-island/lagrange"

    def test_explicit_owner_passes_through(self):
        assert mod._slug("owner/name", "celestia-island") == "owner/name"


class TestProfile:
    def test_profile_shape_is_the_org_rollout(self):
        p = mod.PROFILE
        assert p["required_pull_request_reviews"] == {"required_approving_review_count": 0}
        assert p["enforce_admins"] is True
        assert p["allow_force_pushes"] is False
        assert p["allow_deletions"] is False
        assert p["required_linear_history"] is True
        assert p["required_status_checks"] is None
        assert p["restrictions"] is None
        assert p["lock_branch"] is False

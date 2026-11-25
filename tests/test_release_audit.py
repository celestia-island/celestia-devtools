"""Tests for the release-lane audit (release/audit.py).

*Why this file exists:* the audit judges a repository against a registry, and
neither half can be reached from a test runner that must stay offline. The
comparison is therefore a pure function and the transport/registry calls are
injected, so the drift matrix below is proven without a checkout and without
the network. The wire tests (`test_release_audit_live`-style smoke runs) live
outside the suite on purpose: a live registry answer changes between two runs
of the same commit, which is exactly what a test must not do.

*Seam-pin mutations this file kills* (each re-runnable against the tree):

| mutation | verdict |
| --- | --- |
| MUT:drop-untagged-drift - empty the published-without-tag list | 11 reds |
| MUT:drop-tagged-drift - empty the tagged-without-published list | 3 reds |
| MUT:drop-skipped-warn - stop collecting the skip gaps | 8 reds |
| MUT:ignore-floor - keep every version (floor split disabled) | 5 reds |
| MUT:no-numeric-order - collapse the version sort key | 6 reds |
"""

import http.client
import json
from pathlib import Path
import pytest
from celestia_devtools.release import audit

# ── fixture helpers ──────────────────────────────────────────────────────────

def _tags(*versions, prefix = "v"):
    """The repository-side tag list for one lane."""
    return [prefix + version for version in versions]

def _npm(versions):
    """A minimal npm packument: only the versions mapping is read."""
    return {"versions": {version: {} for version in versions}}

def _crates(versions):
    """A minimal crates.io record: only versions[].num is read."""
    return {"versions": [{"num": version} for version in versions]}

def _fake_sources(monkeypatch, tags, published):
    """Bind both injection points: git tags and the registry answer by URL."""
    monkeypatch.setattr(audit, "git_tags", lambda repo: list(tags))

    def fake_fetch(url):
        if url.startswith(audit.NPM_REGISTRY):
            return _npm(published)

        return _crates(published)

    monkeypatch.setattr(audit, "fetch_json", fake_fetch)

# ── the drift matrix (pure reconciliation) ───────────────────────────────────

def test_clean_lane_has_no_drift():
    result = audit.reconcile_lane("npm", "pkg", "v", ["0.5.0", "0.5.1"], _tags("0.5.0", "0.5.1"))
    assert result.has_drift is False
    assert result.published_untagged == []
    assert result.tagged_unpublished == []
    assert result.latest_published == "0.5.1"
    assert result.latest_tag == "0.5.1"

# MUT:drop-untagged-drift empties this list and the untagged tests reds (11).
def test_published_version_without_a_tag_is_drift():
    result = audit.reconcile_lane("npm", "pkg", "v", ["0.5.0", "0.5.1", "0.5.2"], _tags("0.5.0", "0.5.2"))
    assert result.published_untagged == ["0.5.1"]
    assert result.tagged_unpublished == []
    assert result.has_drift is True

# MUT:drop-tagged-drift empties this list and those tests reds (3).
def test_tag_without_a_published_version_is_drift():
    result = audit.reconcile_lane("npm", "pkg", "v", ["0.5.0"], _tags("0.5.0", "0.5.9"))
    assert result.tagged_unpublished == ["0.5.9"]
    assert result.published_untagged == []
    assert result.latest_tag == "0.5.9"

def test_both_directions_are_reported_together():
    result = audit.reconcile_lane("npm", "pkg", "v", ["0.5.0", "0.5.4"], _tags("0.5.0", "0.5.3"))
    assert result.published_untagged == ["0.5.4"]
    assert result.tagged_unpublished == ["0.5.3"]

def test_floor_ignores_pre_publish_history():
    result = audit.reconcile_lane("npm", "pkg", "v", ["0.4.4", "0.4.5", "0.30.0", "0.31.0"], _tags("0.1.0", "0.4.4", "0.30.0"), floor = "0.30.0")
    assert result.ignored_published == 2
    assert result.ignored_tags == 2
    assert result.published_untagged == ["0.31.0"]
    assert result.tagged_unpublished == []
    assert result.published == ["0.30.0", "0.31.0"]

# MUT:ignore-floor keeps versions below the split and this test reds (5).
def test_floor_does_not_mask_drift_above_it():
    result = audit.reconcile_lane("npm", "pkg", "v", ["0.30.0", "0.30.1"], _tags("0.30.0"), floor = "0.30.0")
    assert result.ignored_published == 0
    assert result.published_untagged == ["0.30.1"]

def test_invalid_floor_is_refused():
    with pytest.raises(ValueError):
        audit.reconcile_lane("npm", "pkg", "v", ["0.5.0"], _tags("0.5.0"), floor = "zero")

# MUT:drop-skipped-warn collects no gaps and the skip tests reds (8).
def test_skipped_patches_are_reported_in_the_newest_minor_only():
    # 0.4 skips patch 2, 0.5 skips patch 1: only the newest line (0.5) is judged.
    result = audit.reconcile_lane("npm", "pkg", "v", ["0.4.0", "0.4.1", "0.4.3", "0.5.0", "0.5.2"], _tags("0.5.0", "0.5.2"))
    assert result.skipped == [1]
    assert result.skipped_total == 1

def test_skips_start_at_the_first_published_patch():
    # A line that opens at X.Y.1 must not report .0 as a burned slot.
    result = audit.reconcile_lane("npm", "pkg", "v", ["0.6.1", "0.6.3"], _tags("0.6.1", "0.6.3"))
    assert result.skipped == [2]

def test_skips_are_capped_but_the_count_stays_exact():
    result = audit.reconcile_lane("npm", "pkg", "v", ["0.6.0", "0.6.2", "0.6.3", "0.6.5"], [], skips_limit = 1)
    assert result.skipped == [1]
    assert result.skipped_total == 2

def test_zero_skips_limit_hides_the_list_only():
    result = audit.reconcile_lane("npm", "pkg", "v", ["0.6.0", "0.6.2"], [], skips_limit = 0)
    assert result.skipped == []
    assert result.skipped_total == 1

# MUT:no-numeric-order collapses the sort key and the ordering test reds (6).
def test_versions_are_ordered_numerically_not_lexically():
    result = audit.reconcile_lane("npm", "pkg", "v", ["0.9.0", "0.9.3", "0.10.0", "0.10.2"], [])
    assert result.latest_published == "0.10.2"
    assert result.skipped == [1]

def test_lane_prefixes_are_isolated():
    result = audit.reconcile_lane("npm", "pkg", "v", ["0.5.0"], ["v0.5.0", "cargo-v0.6.0"])
    assert result.tags == ["0.5.0"]
    assert result.tagged_unpublished == []
    crate = audit.reconcile_lane("crates", "crate-x", "cargo-v", ["0.6.0"], ["v0.5.0", "cargo-v0.6.0"])
    assert crate.tags == ["0.6.0"]
    assert crate.has_drift is False

def test_unparseable_lane_tags_are_counted_not_compared():
    result = audit.reconcile_lane("npm", "pkg", "v", ["0.5.0"], ["v0.5.0", "v-latest", "v2026.10"])
    assert result.tags == ["0.5.0"]
    assert result.unparsed_tags == 2

def test_registry_noise_is_not_a_version():
    result = audit.reconcile_lane("npm", "pkg", "v", ["latest", "0.5.0"], [])
    assert result.published == ["0.5.0"]
    assert result.published_untagged == ["0.5.0"]

# ── the whole run with injected sources ────────────────────────────────────

def test_run_audit_judges_both_lane_kinds():
    def fake_fetch(url):
        if url.startswith(audit.NPM_REGISTRY):
            return _npm(["1.0.0", "1.0.1"])

        return _crates(["2.0.0"])

    result = audit.run_audit(Path("."), npm_packages = ["@scope/pkg"], crates = ["crate-x"], listed_tags = ["v1.0.0", "cargo-v2.0.0"], fetch = fake_fetch)
    assert [lane.kind for lane in result.lanes] == ["npm", "crates"]
    assert result.lanes[0].published_untagged == ["1.0.1"]
    assert result.lanes[1].has_drift is False
    assert result.errors == []
    assert result.ok is False

def test_npm_name_is_url_escaped():
    assert audit.registry_url("npm", "@scope/pkg") == "https://registry.npmjs.org/%40scope%2Fpkg"
    assert audit.registry_url("crates", "crate-x") == "https://crates.io/api/v1/crates/crate-x"

def test_registry_answer_without_versions_is_an_error():
    with pytest.raises(audit.verify_error):
        audit.npm_versions({"dist-tags": {"latest": "1.0.0"}})

    with pytest.raises(audit.verify_error):
        audit.crates_versions({"versions": []})

def test_a_broken_registry_fails_closed():
    def broken_fetch(url):
        raise audit.verify_error(f"{url}: simulated outage")

    result = audit.run_audit(Path("."), npm_packages = ["@scope/pkg"], listed_tags = [], fetch = broken_fetch)
    assert result.ok is False
    assert len(result.errors) == 1
    assert "simulated outage" in result.errors[0]

def test_an_unreadable_repo_fails_closed():
    result = audit.run_audit(Path(".") / "selftest-no-such-repo-8f4c1d", npm_packages = ["@scope/pkg"])
    assert result.ok is False
    assert "not a directory" in result.errors[0]

def test_a_drifted_registry_is_not_ok_but_has_no_error():
    result = audit.run_audit(Path("."), npm_packages = ["@scope/pkg"], listed_tags = [], fetch = lambda url: _npm(["1.0.0"]))
    assert result.errors == []
    assert result.ok is False
    assert result.has_drift is True

# ── CLI: exit codes, JSON shape and human output ───────────────────────────

def test_exit_0_when_the_lane_is_clean(monkeypatch, tmp_path, capsys):
    _fake_sources(monkeypatch, ["v0.5.0", "v0.5.1"], ["0.5.0", "0.5.1"])
    rc = audit.main(["--repo", str(tmp_path), "--npm", "pkg"])
    out = capsys.readouterr().out
    assert rc == 0
    assert "no drift" in out

def test_exit_1_when_a_published_version_has_no_tag(monkeypatch, tmp_path, capsys):
    _fake_sources(monkeypatch, ["v0.5.0"], ["0.5.0", "0.5.1"])
    rc = audit.main(["--repo", str(tmp_path), "--npm", "pkg"])
    out = capsys.readouterr().out
    assert rc == 1
    assert "0.5.1" in out
    assert "without a tag" in out

def test_exit_2_when_the_repo_path_does_not_exist(tmp_path, capsys):
    # The real git_tags path, no monkeypatch: a path that is not a directory is
    # refused before git is ever asked, so the failure cannot depend on a git
    # binary that happens to be absent.
    rc = audit.main(["--repo", str(tmp_path / "missing"), "--npm", "pkg"])
    out = capsys.readouterr().out
    assert rc == 2
    assert "not a directory" in out
    assert "cannot verify" in out

def test_exit_2_when_the_repo_is_not_a_git_checkout(monkeypatch, tmp_path, capsys):
    def broken_git(repo):
        raise audit.verify_error(f"not a git repository: {repo}")

    monkeypatch.setattr(audit, "git_tags", broken_git)
    rc = audit.main(["--repo", str(tmp_path), "--npm", "pkg"])
    captured = capsys.readouterr()
    assert rc == 2
    assert "not a git repository" in captured.out
    assert "cannot verify" in captured.out

def test_exit_2_when_the_registry_is_unreachable(monkeypatch, tmp_path, capsys):
    monkeypatch.setattr(audit, "git_tags", lambda repo: [])

    def broken_fetch(url):
        raise audit.verify_error(f"{url}: timed out")

    monkeypatch.setattr(audit, "fetch_json", broken_fetch)
    rc = audit.main(["--repo", str(tmp_path), "--npm", "pkg"])
    assert rc == 2
    assert "timed out" in capsys.readouterr().out

def test_exit_2_without_a_lane(capsys):
    rc = audit.main([])
    assert rc == 2
    assert "nothing to audit" in capsys.readouterr().err

def test_negative_skips_limit_is_a_usage_error(monkeypatch, tmp_path, capsys):
    _fake_sources(monkeypatch, [], [])
    rc = audit.main(["--repo", str(tmp_path), "--npm", "pkg", "--skips-limit", "-1"])
    assert rc == 2
    assert "--skips-limit" in capsys.readouterr().err

def test_json_shape(monkeypatch, tmp_path, capsys):
    _fake_sources(monkeypatch, ["v0.5.0", "v0.5.3"], ["0.5.0", "0.5.1", "0.5.3"])
    rc = audit.main(["--repo", str(tmp_path), "--npm", "pkg", "--ignore-below", "0.4.0", "--json"])
    payload = json.loads(capsys.readouterr().out)
    assert rc == 1
    assert payload["ok"] is False
    assert payload["errors"] == []
    assert payload["floor"] == "0.4.0"
    assert payload["skips_limit"] == 40
    assert len(payload["lanes"]) == 1
    lane = payload["lanes"][0]
    assert lane["kind"] == "npm"
    assert lane["name"] == "pkg"
    assert lane["tag_prefix"] == "v"
    assert lane["floor"] == "0.4.0"
    assert lane["published_count"] == 3
    assert lane["tagged_count"] == 2
    assert lane["published_untagged"] == ["0.5.1"]
    assert lane["tagged_unpublished"] == []
    assert lane["skipped"] == [2]
    assert lane["skipped_total"] == 1
    assert lane["latest_published"] == "0.5.3"
    assert lane["latest_tag"] == "0.5.3"
    assert lane["ignored_published"] == 0
    assert lane["ignored_tags"] == 0

def test_json_reports_skips_and_floor_counts(monkeypatch, tmp_path, capsys):
    _fake_sources(monkeypatch, ["v0.6.0", "v0.6.2"], ["0.4.4", "0.6.0", "0.6.2"])
    rc = audit.main(["--repo", str(tmp_path), "--npm", "pkg", "--ignore-below", "0.6.0", "--skips-limit", "1", "--json"])
    payload = json.loads(capsys.readouterr().out)
    assert rc == 0
    assert payload["ok"] is True
    lane = payload["lanes"][0]
    assert lane["ignored_published"] == 1
    assert lane["skipped"] == [1]
    assert lane["skipped_total"] == 1

def test_json_error_document_is_fail_closed(monkeypatch, tmp_path, capsys):
    monkeypatch.setattr(audit, "git_tags", lambda repo: [])

    def broken_fetch(url):
        raise audit.verify_error(f"{url}: dns failure")

    monkeypatch.setattr(audit, "fetch_json", broken_fetch)
    rc = audit.main(["--repo", str(tmp_path), "--npm", "pkg", "--json"])
    payload = json.loads(capsys.readouterr().out)
    assert rc == 2
    assert payload["ok"] is False
    assert payload["lanes"] == []
    assert "dns failure" in payload["errors"][0]

def test_human_output_reports_crates_and_skips(monkeypatch, tmp_path, capsys):
    monkeypatch.setattr(audit, "git_tags", lambda repo: ["cargo-v0.6.0"])
    monkeypatch.setattr(audit, "fetch_json", lambda url: _crates(["0.6.0", "0.6.2"]))
    rc = audit.main(["--repo", str(tmp_path), "--crates", "crate-x", "--crate-tag-prefix", "cargo-v"])
    out = capsys.readouterr().out
    assert rc == 1
    assert "crates crate-x" in out
    assert "published 2, tagged 1" in out
    assert "skipped patch number(s)" in out
    assert "never back-fill" in out

def test_selftest_passes(capsys):
    rc = audit.main(["--selftest"])
    out = capsys.readouterr().out
    assert rc == 0
    assert "selftest OK" in out

def test_a_floor_that_hides_every_published_version_is_unverified():
    # MUT:ignore-floor keeps versions below the split, this lane stops erroring
    # and the test reds: a floor that hides everything judged nothing.
    report = audit.run_audit(Path("."), npm_packages = ["pkg"], ignore_below = "99.0.0", listed_tags = ["v99.1.0"], fetch = lambda url: {"versions": {"1.0.0": {}, "1.0.1": {}}})
    assert report.errors
    assert report.ok is False
    assert report.lanes == []

def test_a_floor_that_hides_everything_exits_2(monkeypatch, capsys):
    monkeypatch.setattr(audit, "git_tags", lambda repo: ["v99.1.0"])
    monkeypatch.setattr(audit, "fetch_json", lambda url: {"versions": {"1.0.0": {}, "1.0.1": {}}})
    rc = audit.main(["--repo", ".", "--npm", "pkg", "--ignore-below", "99.0.0"])
    assert rc == 2

def test_prerelease_sorts_below_its_release():
    item = audit.reconcile_lane("npm", "pkg", "v", ["1.0.0-rc.1", "1.0.0"], ["v1.0.0"])
    assert item.latest_published == "1.0.0"
    assert item.skipped_total == 0

def test_a_protocol_error_is_cannot_verify(monkeypatch):
    class boom:
        def open(self, request, timeout = None):
            raise http.client.BadStatusLine("bad")

    monkeypatch.setattr(audit, "proxy_opener", lambda: boom())

    with pytest.raises(audit.verify_error):
        audit.fetch_json("https://registry.npmjs.org/pkg")

def test_unparsed_lane_tags_are_warned_in_the_text_report():
    report = audit.run_audit(Path("."), npm_packages = ["pkg"], listed_tags = ["v1.0.0", "v-latest"], fetch = lambda url: {"versions": {"1.0.0": {}}})
    text = audit.render_text(report)
    assert "do not parse" in text

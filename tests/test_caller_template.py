"""Pin the generated commit-lint caller to the org-wide canonical bytes.

*Why this exists:* on 2026-09-15 the org unified all 42 caller files byte-for-byte
(273 B, sha256 ``106a93d0…``). The generator here is the only other producer of that
file — ``celestia-devtools init --with-workflows`` (and ``--force``, which rewrites an
existing file unconditionally). Before this guard the template still emitted the older
variant without ``ready_for_review``, so running ``init --force --with-workflows`` over
a unified repository silently reverted it, and a draft PR flipped to "ready for review"
produced no lint run at all — the §8.3.6 deadlock: the required check never appears and
the merge is refused. A template nobody compares against the fleet is a regression vector,
so these tests fail the moment the constant drifts.

*2026-09-26 lane migration:* the canonical bytes became the lane-carrying form
(1170 B, sha256 ``620d511f…``, byte-identical to the shittim-chest production caller
that already serves the check through ``vars.LINT_RUNNER``/``vars.LINT_LANE``). The
pre-lane 273 B bytes live on as ``WORKFLOW_COMMIT_LINT_LEGACY`` — the audit reports
them as a *warning* so the fleet can regenerate repo by repo instead of going 38-red
in one wave. These tests pin both constants, and pin that they stay distinct.
"""

from __future__ import annotations
import hashlib
from pathlib import Path
import yaml
from celestia_devtools.repo import init
from celestia_devtools.repo.init import (OPT_IN_GATES, PINNED_DEVTOOLS_REF, TEMPLATE_SOURCES, workflow_drift_problems, WORKFLOW_CI_CACHE, WORKFLOW_CNB_OVERFLOW, WORKFLOW_COMMIT_LINT, WORKFLOW_COMMIT_LINT_LEGACY, WORKFLOW_COMMIT_LINT_NO_PERMS, WORKFLOW_FAMILY_VERSIONS, WORKFLOW_P0_GATE, WORKFLOW_PR_TITLE_CHECK, WORKFLOW_RELEASE_NOTES, WORKFLOW_RULES_LINT, WORKFLOW_VERIFY_VERSIONS, _T_COMMIT_LINT, _ensure_workflows, caller_render_state, render_caller, unpinned_caller_warnings)

#: sha256 of the canonical caller shipped in celestia-island repositories: the
#: lane-carrying form **plus** the explicit `permissions` grant, rendered at the
#: pinned devtools ref (P2-3). A caller that withholds `pull-requests: read`
#: makes the reusable lint fail as `startup_failure` (zero jobs) — the required
#: check never reports, and a protected repository cannot be merged. Thirty
#: repositories were missed when the org default token permissions were
#: tightened on 2026-10-02.
CANONICAL_SHA256 = "3cdb9e7a35e5df7877c906fec508f0bb4a2b24ed7aabdbdb5465b2ee97492568"
CANONICAL_BYTES = 1897

#: sha256 of the **master render** — the floating form the entire fleet carries
#: today. This is the comparator that classifies un-migrated repositories as
#: "unpinned" (warning) instead of "drifted" (error): if a template edit ever
#: moves these bytes, the fleet turns red in one wave, which is exactly the
#: outcome the pin migration must avoid. Frozen from the 2026-10-06 canonical.
MASTER_SHA256 = "943a8f3564151a6569bc43330ea8e8e38b0885a43bdc383a135ad623fc1743d9"
MASTER_BYTES = 1866

#: The lane-carrying template without the grant (2026-09-26 → 2026-10-06): audit
#: reports it as a warning, because the check still cannot start.
NO_PERMS_SHA256 = "620d511f256c4b73522b75beecc33509aa1401d2ca808efacde866b03126f75f"
NO_PERMS_BYTES = 1170

#: The pre-lane canonical (2026-09-15 → 2026-09-26): audit-only migration marker.
LEGACY_SHA256 = "106a93d0ca2bb1db223ef0fc82b72a53a58ea0ebcbd44bbd2eb8539be8b27342"
LEGACY_BYTES = 273

def _on(doc):
    """``on`` parses as the boolean ``True`` under YAML 1.1 loaders."""
    return doc.get("on") or doc.get(True)

class test_canonical_bytes:
    def test_template_hashes_to_the_canonical_file(self):
        assert hashlib.sha256(WORKFLOW_COMMIT_LINT.encode()).hexdigest() == CANONICAL_SHA256

    def test_template_length_matches_the_fleet(self):
        assert len(WORKFLOW_COMMIT_LINT.encode()) == CANONICAL_BYTES

    def test_template_ends_with_a_single_newline(self):
        assert WORKFLOW_COMMIT_LINT.endswith("pr: ${{ inputs.pr }}\n")
        assert not WORKFLOW_COMMIT_LINT.endswith("\n\n")

class test_master_render_is_the_fleet_comparator:
    """P2-3: rendering at ``master`` must reproduce today's fleet bytes exactly.

    ``caller_render_state`` classifies the un-migrated fleet through this
    render: if it drifts, every repository still on ``@master`` flips from the
    "unpinned" warning to the "drifted" error and the migration goes red in one
    wave — the failure mode this pin project exists to prevent.
    """

    def test_master_render_hashes_to_the_2026_10_06_canonical(self):
        assert hashlib.sha256(render_caller(_T_COMMIT_LINT, "master").encode()).hexdigest() == MASTER_SHA256

    def test_master_render_keeps_the_fleet_length(self):
        assert len(render_caller(_T_COMMIT_LINT, "master").encode()) == MASTER_BYTES

    def test_master_render_is_distinct_from_canonical(self):
        assert render_caller(_T_COMMIT_LINT, "master") != WORKFLOW_COMMIT_LINT

class test_ref_rendering:
    """The render mechanism behind both the generator and the classifiers."""

    def test_markers_never_leak_into_rendered_output(self):
        for source in TEMPLATE_SOURCES.values():
            for ref in (PINNED_DEVTOOLS_REF, "master", "v2099.1", "0123456789abcdef0123456789abcdef01234567"):
                assert "__DEVTOOLS_REF" not in render_caller(source, ref)

    def test_pinned_render_passes_the_ref_to_the_callee(self):
        for filename in ("commit-msg-lint.yml", "ci-cache.yml", "pr-title-check.yml", "family-versions.yml", "verify-versions.yml", "rules-lint.yml", "release-notes.yml"):
            job = yaml.safe_load(render_caller(TEMPLATE_SOURCES[filename], PINNED_DEVTOOLS_REF))["jobs"]
            body = next(iter(job.values()))
            assert body["uses"].endswith("@" + PINNED_DEVTOOLS_REF), filename
            assert body["with"]["devtools-ref"] == PINNED_DEVTOOLS_REF, filename

    def test_master_render_passes_no_devtools_ref(self):
        for filename in TEMPLATE_SOURCES:
            body = next(iter(yaml.safe_load(render_caller(TEMPLATE_SOURCES[filename], "master"))["jobs"].values()))
            assert "devtools-ref" not in (body.get("with") or {}), filename

    def test_p0_gate_and_cnb_pins_do_not_pass_the_ref(self):
        """p0-gate reads the live ledger from master by design; cnb never fetches devtools."""

        for filename in ("p0-gate.yml", "cnb-overflow-lane.yml"):
            body = next(iter(yaml.safe_load(render_caller(TEMPLATE_SOURCES[filename], PINNED_DEVTOOLS_REF))["jobs"].values()))
            assert body["uses"].endswith("@" + PINNED_DEVTOOLS_REF), filename
            assert "devtools-ref" not in (body.get("with") or {}), filename

    def test_classification_matrix(self, tmp_path):
        workflows = tmp_path / "workflows"
        workflows.mkdir()
        source = TEMPLATE_SOURCES["ci-cache.yml"]

        for name, text in (("clean", render_caller(source, PINNED_DEVTOOLS_REF)), ("fleet-master", render_caller(source, "master")), ("stale", render_caller(source, "v2026.9")), ("drifted", render_caller(source, PINNED_DEVTOOLS_REF) + "# hand edit\n")):
            state, ref = caller_render_state(text, source)
            assert (state, ref) == {"clean": ("clean", PINNED_DEVTOOLS_REF), "fleet-master": ("master", "master"), "stale": ("stale", "v2026.9"), "drifted": ("drifted", PINNED_DEVTOOLS_REF)}[name], name

class test_unpinned_warnings:
    """The migration-period judgement: floating/stale callers warn, never fail."""

    def _stage(self, tmp_path, ref):
        workflows = tmp_path / "workflows"
        workflows.mkdir(exist_ok = True)

        for filename in ("commit-msg-lint.yml", "ci-cache.yml", "pr-title-check.yml"):
            (workflows / filename).write_text(render_caller(TEMPLATE_SOURCES[filename], ref), encoding = "utf-8")

        return workflows

    def test_fleet_stock_master_bytes_warn_without_failing(self, tmp_path):
        workflows = self._stage(tmp_path, "master")
        assert workflow_drift_problems(workflows) == []
        warnings = unpinned_caller_warnings(workflows)
        assert len(warnings) == 3
        assert all(("pins nothing" in w) for w in warnings)

    def test_pinned_callers_are_silent(self, tmp_path):
        workflows = self._stage(tmp_path, PINNED_DEVTOOLS_REF)
        assert unpinned_caller_warnings(workflows) == []

    def test_stale_pin_names_the_refs(self, tmp_path):
        workflows = self._stage(tmp_path, "v2026.9")
        warnings = unpinned_caller_warnings(workflows)
        assert len(warnings) == 3
        assert all(("v2026.9" in w) and (PINNED_DEVTOOLS_REF in w) for w in warnings)

    def test_hand_written_master_caller_is_reported_too(self, tmp_path):
        """Files outside the template tables (style-lint adoption) count as well."""
        workflows = tmp_path / "workflows"
        workflows.mkdir()
        (workflows / "style-lint.yml").write_text("jobs:\n  py-style:\n    uses: celestia-island/celestia-devtools/.github/workflows/style-lint.yml@master\n    with:\n      tool: py-style\n", encoding = "utf-8")
        (workflows / "pinned-style.yml").write_text("jobs:\n  py-style:\n    uses: celestia-island/celestia-devtools/.github/workflows/style-lint.yml@v2026.10\n", encoding = "utf-8")
        warnings = unpinned_caller_warnings(workflows)
        assert len(warnings) == 1
        assert "style-lint.yml" in warnings[0]

    def test_commented_out_uses_does_not_count(self, tmp_path):
        workflows = tmp_path / "workflows"
        workflows.mkdir()
        (workflows / "notes.yml").write_text("# jobs:\n#   x:\n#     uses: celestia-island/celestia-devtools/.github/workflows/ci-cache.yml@master\n", encoding = "utf-8")
        assert unpinned_caller_warnings(workflows) == []

    def test_callee_definitions_are_skipped(self, tmp_path):
        workflows = tmp_path / "workflows"
        workflows.mkdir()
        (workflows / "ci-cache.yml").write_text("on:\n  workflow_call: {}\njobs:\n  cache:\n    uses: celestia-island/celestia-devtools/.github/workflows/ci-cache.yml@master\n", encoding = "utf-8")
        assert unpinned_caller_warnings(workflows) == []

class test_ref_escape_hatch:
    """``--ref master`` regenerates the floating form for break-glass use."""

    def test_ref_master_writes_fleet_bytes(self, tmp_path):
        _ensure_workflows(tmp_path, force = True, ref = "master")
        written = (tmp_path / ".github" / "workflows" / "commit-msg-lint.yml").read_bytes()
        assert hashlib.sha256(written).hexdigest() == MASTER_SHA256

    def test_explicit_ref_writes_that_pin(self, tmp_path):
        _ensure_workflows(tmp_path, force = True, ref = "v2026.9")
        body = next(iter(yaml.safe_load((tmp_path / ".github" / "workflows" / "ci-cache.yml").read_text(encoding = "utf-8"))["jobs"].values()))
        assert body["uses"].endswith("@v2026.9")
        assert body["with"]["devtools-ref"] == "v2026.9"

def test_sha_ref_generates_sha_pinned_callers(monkeypatch, tmp_path):
    """Since the 2026-10-31 scheme ruling a 40-hex SHA is the canonical pin:
    the CLI accepts it and the generated callers carry it on BOTH layers
    (`uses:@<sha>` and `devtools-ref: <sha>` where the template passes it)."""
    monkeypatch.chdir(tmp_path)
    sha = "0123456789abcdef0123456789abcdef01234567"
    monkeypatch.setattr("sys.argv", ["init", "--with-workflows", "--ref", sha])

    # Return-code assertion only: the shared logger emits through a worker
    # thread, so captured-stream assertions on it are racy by design
    # (see test_init_check.py).
    assert init.main() == 0
    trio = tmp_path / ".github" / "workflows"
    assert trio.exists(), "a SHA ref must generate callers (it used to be rejected before the clone lanes could bootstrap one)"

    for filename in ("commit-msg-lint.yml", "ci-cache.yml", "pr-title-check.yml"):
        body = next(iter(yaml.safe_load((trio / filename).read_text(encoding = "utf-8"))["jobs"].values()))
        assert body["uses"].endswith("@" + sha), filename

class test_legacy_bytes:
    """The pre-lane template: the audit's migration marker, frozen on purpose.

    ``_canonical_caller_findings`` reports exactly these bytes as a warning, so a
    stray edit to the legacy constant would silently re-classify the un-migrated
    fleet as hand-edited (error) — or worse, make legacy equal canonical and kill
    the migration signal entirely.
    """

    def test_legacy_hashes_to_the_historical_file(self):
        assert (hashlib.sha256(WORKFLOW_COMMIT_LINT_LEGACY.encode()).hexdigest() == LEGACY_SHA256)

    def test_legacy_length_is_the_pre_lane_fleet(self):
        assert len(WORKFLOW_COMMIT_LINT_LEGACY.encode()) == LEGACY_BYTES

    def test_legacy_is_distinct_from_canonical(self):
        assert WORKFLOW_COMMIT_LINT != WORKFLOW_COMMIT_LINT_LEGACY

    def test_canonical_carries_the_lane_passthrough(self):
        """The whole point of the migration: canonical must express the lane vars."""
        assert "vars.LINT_RUNNER" in WORKFLOW_COMMIT_LINT
        assert "vars.LINT_LANE" in WORKFLOW_COMMIT_LINT
        assert "vars.LINT_RUNNER" not in WORKFLOW_COMMIT_LINT_LEGACY

class test_no_perms_bytes:
    """The lane-carrying template that predates the permission grant.

    ``_canonical_caller_findings`` reports exactly these bytes as a warning, so a
    stray edit here would re-classify thirty fixed repositories as hand-edited
    (error) — or, worse, make it equal canonical and remove the migration signal
    that tells the fleet to regenerate.
    """

    def test_hashes_to_the_pre_grant_file(self):
        assert (hashlib.sha256(WORKFLOW_COMMIT_LINT_NO_PERMS.encode()).hexdigest() == NO_PERMS_SHA256)

    def test_length_is_the_pre_grant_fleet(self):
        assert len(WORKFLOW_COMMIT_LINT_NO_PERMS.encode()) == NO_PERMS_BYTES

    def test_carries_the_lane_passthrough_but_not_the_grant(self):
        assert "vars.LINT_RUNNER" in WORKFLOW_COMMIT_LINT_NO_PERMS
        assert "permissions" not in WORKFLOW_COMMIT_LINT_NO_PERMS
        assert "permissions" in WORKFLOW_COMMIT_LINT

    def test_distinct_from_both_other_forms(self):
        assert WORKFLOW_COMMIT_LINT_NO_PERMS not in (WORKFLOW_COMMIT_LINT, WORKFLOW_COMMIT_LINT_LEGACY)

class test_permission_grant:
    """The grant is the fix for the 2026-10-02 regression: without it the called
    workflow cannot start, the required check never reports, and a repository with
    `enforce_admins` on cannot be merged by anyone."""

    def test_canonical_grants_exactly_what_the_callee_declares(self):
        doc = yaml.safe_load(WORKFLOW_COMMIT_LINT)
        assert doc["permissions"] == {"contents": "read", "pull-requests": "read"}

    def test_grant_is_workflow_level(self):
        """A job-level grant would not cover the reusable call's own resolution."""
        doc = yaml.safe_load(WORKFLOW_COMMIT_LINT)
        assert "permissions" not in doc["jobs"]["lint-commits"]

class test_trigger_shape:
    def test_every_lifecycle_event_is_covered(self):
        doc = yaml.safe_load(WORKFLOW_COMMIT_LINT)
        assert _on(doc)["pull_request"]["types"] == ["opened", "edited", "reopened", "ready_for_review", "synchronize"]

    def test_ready_for_review_is_present(self):
        """Its absence is the §8.3.6 deadlock: a draft PR marked ready never lints."""
        types = _on(yaml.safe_load(WORKFLOW_COMMIT_LINT))["pull_request"]["types"]
        assert "ready_for_review" in types

    def test_synchronize_is_present(self):
        """Its absence means a re-pushed head never re-runs the required check."""
        types = _on(yaml.safe_load(WORKFLOW_COMMIT_LINT))["pull_request"]["types"]
        assert "synchronize" in types

    def test_merge_group_is_declared(self):
        doc = yaml.safe_load(WORKFLOW_COMMIT_LINT)
        assert _on(doc)["merge_group"]["types"] == ["checks_requested"]

    def test_no_push_trigger(self):
        """Lint is a required check, not a master-post-processing step."""
        assert "push" not in _on(yaml.safe_load(WORKFLOW_COMMIT_LINT))

    def test_per_pull_request_concurrency(self):
        """One in-flight lint per pull request (ci-ops 8.3.3).

        A superseded push cancels its predecessor instead of queueing a second
        lint on the lane the repository resolves — on the farm lane that is one
        runner slot instead of many. Dispatch runs fall back to the run ref, so
        a manual re-lint still cancels only its own predecessors.
        """
        doc = yaml.safe_load(WORKFLOW_COMMIT_LINT)
        assert doc["concurrency"] == {"group": "${{ github.workflow }}-${{ github.event.pull_request.number || github.ref }}", "cancel-in-progress": True}

class test_caller_job_shape:
    def test_job_carries_only_uses_and_with(self):
        """A caller job accepts nine legal keys; this one uses ``uses`` + ``with``.

        The ``with`` block is the lane passthrough (2026-09-26): anything else —
        ``timeout-minits`` in particular — makes the whole file invalid.
        """
        job = yaml.safe_load(WORKFLOW_COMMIT_LINT)["jobs"]["lint-commits"]
        assert list(job) == ["uses", "with"]

    def test_job_has_no_timeout_minutes(self):
        """The key is illegal on ``uses:`` jobs — GitHub then runs zero jobs (§8.1)."""
        text = yaml.safe_load(WORKFLOW_COMMIT_LINT)["jobs"]["lint-commits"]
        assert "timeout-minutes" not in text

    def test_job_calls_the_shared_reusable_workflow(self):
        job = yaml.safe_load(WORKFLOW_COMMIT_LINT)["jobs"]["lint-commits"]
        assert job["uses"] == ("celestia-island/celestia-devtools/.github/workflows/commit-msg-lint.yml@" + PINNED_DEVTOOLS_REF)

    def test_job_passes_the_pin_to_the_bootstrap(self):
        """P2-3: the pin must reach the callee's runtime fetch, not just its file."""
        job = yaml.safe_load(WORKFLOW_COMMIT_LINT)["jobs"]["lint-commits"]
        assert job["with"]["devtools-ref"] == PINNED_DEVTOOLS_REF

class test_lane_passthrough:
    """The 2026-09-26 canonical additions: dispatch fallback + lane variables.

    Without these, moving the single required check off the (broken) hosted lane
    required editing every repository's tree — the W-2 "one lane of rescue" defect.
    """

    def test_workflow_dispatch_declares_required_pr_input(self):
        dispatch = _on(yaml.safe_load(WORKFLOW_COMMIT_LINT))["workflow_dispatch"]
        assert dispatch["inputs"]["pr"]["required"] is True

    def test_runner_defaults_to_hosted_and_reads_the_repo_variable(self):
        job = yaml.safe_load(WORKFLOW_COMMIT_LINT)["jobs"]["lint-commits"]
        assert job["with"]["runner"] == "${{ vars.LINT_RUNNER || '[\"ubuntu-latest\"]' }}"

    def test_lane_defaults_to_hosted_and_reads_the_repo_variable(self):
        job = yaml.safe_load(WORKFLOW_COMMIT_LINT)["jobs"]["lint-commits"]
        assert job["with"]["lane"] == "${{ vars.LINT_LANE || 'hosted' }}"

    def test_pr_passthrough_feeds_the_dispatch_fallback(self):
        job = yaml.safe_load(WORKFLOW_COMMIT_LINT)["jobs"]["lint-commits"]
        assert job["with"]["pr"] == "${{ inputs.pr }}"

class test_generated_file:
    def test_writer_emits_the_canonical_bytes(self, tmp_path):
        _ensure_workflows(tmp_path)
        written = (tmp_path / ".github" / "workflows" / "commit-msg-lint.yml").read_bytes()
        assert written == WORKFLOW_COMMIT_LINT.encode()
        assert hashlib.sha256(written).hexdigest() == CANONICAL_SHA256

    def test_force_rewrites_to_the_canonical_bytes(self, tmp_path):
        """``--force`` overwrites an existing file; that rewrite must land on canonical."""
        target = tmp_path / ".github" / "workflows" / "commit-msg-lint.yml"
        target.parent.mkdir(parents = True)
        target.write_text("name: stale\n", encoding = "utf-8")
        _ensure_workflows(tmp_path, force = True)
        assert hashlib.sha256(target.read_bytes()).hexdigest() == CANONICAL_SHA256

    def test_existing_file_is_left_alone_without_force(self, tmp_path):
        target = tmp_path / ".github" / "workflows" / "commit-msg-lint.yml"
        target.parent.mkdir(parents = True)
        target.write_text("name: mine\n", encoding = "utf-8")
        _ensure_workflows(tmp_path)
        assert target.read_text(encoding = "utf-8") == "name: mine\n"

#: The callee this repository hosts: the reusable workflow the template's ``uses:`` resolves to.
REPO_ROOT = Path(__file__).resolve().parents[1]
CALLEE_PATH = REPO_ROOT / ".github" / "workflows" / "commit-msg-lint.yml"

def _callee():
    return yaml.safe_load(CALLEE_PATH.read_text(encoding = "utf-8"))

class test_callee_matches_the_template:
    """Two files must agree on the trigger: the caller (copied into 42 repositories) and the
    callee this repository hosts.

    Nothing compared them. The caller's template could gain an event — or the callee could
    drop one — and the caller's jobs would simply never start, with the caller file still
    byte-identical to the canonical bytes and every other guard green. Only the trigger has
    to match: the callee additionally declares ``workflow_call`` and carries ``runs-on`` /
    ``steps`` the caller must never have.
    """

    def test_callee_file_is_present(self):
        assert CALLEE_PATH.is_file(), f"missing callee workflow: {CALLEE_PATH}"

    def test_pull_request_types_match_the_template(self):
        assert (_on(_callee())["pull_request"]["types"] == _on(yaml.safe_load(WORKFLOW_COMMIT_LINT))["pull_request"]["types"])

    def test_merge_group_types_match_the_template(self):
        assert (_on(_callee())["merge_group"]["types"] == _on(yaml.safe_load(WORKFLOW_COMMIT_LINT))["merge_group"]["types"])

    def test_callee_declares_every_event_the_caller_waits_on(self):
        caller_events = set(_on(yaml.safe_load(WORKFLOW_COMMIT_LINT)))
        assert caller_events <= set(_on(_callee()))

class test_cache_policy_caller_template:
    """The second generated caller: same discipline as the commit-lint one.

    It is written from the same generator (`init --with-workflows`), so it carries the same
    regression risk -- a template nobody compares against the fleet silently reverts the
    wiring on the next `--force` run. These pin the shape the gate needs, without pinning a
    hash: the file is new, so there is no fleet of byte-identical copies to match yet.
    """

    def test_uses_the_shared_workflow_at_the_pin(self):
        doc = yaml.safe_load(WORKFLOW_CI_CACHE)
        job = doc["jobs"]["cache-policy"]
        assert job["uses"] == ("celestia-island/celestia-devtools/.github/workflows/ci-cache.yml@" + PINNED_DEVTOOLS_REF)
        assert job["with"]["devtools-ref"] == PINNED_DEVTOOLS_REF

    def test_synchronize_is_present(self):
        """Without it a re-pushed head never re-runs the check: the §8.3.6 deadlock."""
        types = _on(yaml.safe_load(WORKFLOW_CI_CACHE))["pull_request"]["types"]
        assert "synchronize" in types

    def test_ready_for_review_is_present(self):
        types = _on(yaml.safe_load(WORKFLOW_CI_CACHE))["pull_request"]["types"]
        assert "ready_for_review" in types

    def test_merge_group_is_declared(self):
        doc = yaml.safe_load(WORKFLOW_CI_CACHE)
        assert _on(doc)["merge_group"]["types"] == ["checks_requested"]

    def test_no_push_trigger(self):
        assert "push" not in _on(yaml.safe_load(WORKFLOW_CI_CACHE))

    def test_caller_job_carries_only_uses_and_with(self):
        """A caller job accepts nine keys; anything else makes the whole file invalid.

        `timeout-minutes` is the specific trap here: it is not a caller key, and adding it
        makes GitHub reject the file without creating a job at all. `with` carries only
        the pin passthrough (P2-3).
        """
        job = yaml.safe_load(WORKFLOW_CI_CACHE)["jobs"]["cache-policy"]
        assert set(job) == {"uses", "with"}
        assert set(job["with"]) == {"devtools-ref"}

    def test_generator_writes_both_callers(self, tmp_path: Path):
        _ensure_workflows(tmp_path)
        written = sorted(p.name for p in (tmp_path / ".github" / "workflows").iterdir())
        assert written == ["ci-cache.yml", "commit-msg-lint.yml", "pr-title-check.yml"]
        assert (tmp_path / ".github" / "workflows" / "ci-cache.yml").read_text(encoding = "utf-8") == WORKFLOW_CI_CACHE

    def test_generator_does_not_overwrite_without_force(self, tmp_path: Path):
        workflows = tmp_path / ".github" / "workflows"
        workflows.mkdir(parents = True)
        (workflows / "ci-cache.yml").write_text("hand-edited\n", encoding = "utf-8")
        _ensure_workflows(tmp_path)
        assert (workflows / "ci-cache.yml").read_text(encoding = "utf-8") == "hand-edited\n"

class test_pr_title_caller_template:
    """The third generated caller, added 2026-09-20.

    It had no template before that, which is why 21 repositories carried a hand-written copy
    whose `types` omitted `synchronize`: a push to an existing PR never re-ran the title
    check, and because it is a required check the merge then waited for a result that was
    never produced.
    """

    def test_uses_the_shared_workflow_at_the_pin(self):
        doc = yaml.safe_load(WORKFLOW_PR_TITLE_CHECK)
        job = doc["jobs"]["pr-title-check"]
        assert job["uses"] == ("celestia-island/celestia-devtools/.github/workflows/pr-title-check.yml@" + PINNED_DEVTOOLS_REF)
        assert job["with"]["devtools-ref"] == PINNED_DEVTOOLS_REF

    def test_synchronize_is_present(self):
        types = _on(yaml.safe_load(WORKFLOW_PR_TITLE_CHECK))["pull_request"]["types"]
        assert "synchronize" in types

    def test_edited_is_present(self):
        """Editing the title must re-run the check that validates it."""
        types = _on(yaml.safe_load(WORKFLOW_PR_TITLE_CHECK))["pull_request"]["types"]
        assert "edited" in types

    def test_caller_job_carries_only_uses_and_with(self):
        job = yaml.safe_load(WORKFLOW_PR_TITLE_CHECK)["jobs"]["pr-title-check"]
        assert set(job) == {"uses", "with"}
        assert set(job["with"]) == {"devtools-ref"}

    def test_per_pull_request_concurrency(self):
        """Mirror of the commit-msg caller: one in-flight title check per PR.

        The hikari line ran this exact block in production for two months
        (2026-08-31 #333 onward) before it was absorbed here — including 16
        cancelled runs in October with zero merge-blocked cases.
        """
        doc = yaml.safe_load(WORKFLOW_PR_TITLE_CHECK)
        assert doc["concurrency"] == {"group": "${{ github.workflow }}-${{ github.event.pull_request.number || github.ref }}", "cancel-in-progress": True}

    def test_callee_also_re_runs_on_synchronize(self):
        """The reusable workflow's own trigger list must match, or the caller is useless."""
        callee = yaml.safe_load((Path(__file__).resolve().parent.parent / ".github/workflows/pr-title-check.yml").read_text(encoding = "utf-8"))
        types = _on(callee)["pull_request"]["types"]
        assert "synchronize" in types

    def test_generator_writes_all_three_callers(self, tmp_path: Path):
        _ensure_workflows(tmp_path)
        written = sorted(p.name for p in (tmp_path / ".github" / "workflows").iterdir())
        assert written == ["ci-cache.yml", "commit-msg-lint.yml", "pr-title-check.yml"]

# ── Opt-in gate callers (2026-10-24) ─────────────────────────────────────────
#
# Six gates had NO template until this wave, so every adopting repository wrote
# its caller by hand: `pr-title-check` lost `synchronize` in five of them,
# `cnb-overflow` never carried the lane passthrough, and the one repository an
# open P0 actually covered (evernight) never adopted `p0-gate` at all. These
# tests pin the generated bytes and the opt-in contract that keeps a repository
# from being handed gates it cannot pass.
_OPT_IN = {"p0": WORKFLOW_P0_GATE, "family": WORKFLOW_FAMILY_VERSIONS, "verify": WORKFLOW_VERIFY_VERSIONS, "rules": WORKFLOW_RULES_LINT, "release-notes": WORKFLOW_RELEASE_NOTES, "cnb": WORKFLOW_CNB_OVERFLOW}

def test_opt_in_registry_matches_the_templates():
    assert set(OPT_IN_GATES) == set(_OPT_IN)

    for gate, (filename, template, _label) in OPT_IN_GATES.items():
        assert _OPT_IN[gate] is template
        assert filename.endswith(".yml")

def test_every_opt_in_caller_is_canonicalisable_and_valid_yaml():
    # `ci-audit` compares callers byte-for-byte, so the templates must be the
    # exact bytes a repository is expected to carry: valid YAML, one `uses:`
    # into this repository, at the pinned ref — and the pin passthrough for
    # every gate whose callee fetches devtools at runtime (p0-gate reads the
    # live ledger from master and cnb-overflow never fetches, so those two
    # pin the workflow file only).
    for gate, template in _OPT_IN.items():
        parsed = yaml.safe_load(template)
        (job_id, job), = parsed["jobs"].items()
        assert job["uses"] == ("celestia-island/celestia-devtools/.github/workflows/" f"{job['uses'].rsplit('/', 1)[-1]}"), gate
        assert job["uses"].endswith("@" + PINNED_DEVTOOLS_REF), gate

        if gate in ("p0", "cnb"):
            assert "devtools-ref" not in (job.get("with") or {}), gate
        else:
            assert job["with"]["devtools-ref"] == PINNED_DEVTOOLS_REF, gate

def test_pr_triggering_gates_react_to_new_pushes():
    # A required check that ignores `synchronize` never re-runs on the pushed
    # head, and the protected branch then refuses the merge — the five-repository
    # `pr-title-check` drift this wave found.
    for gate in ("family", "verify", "rules", "cnb"):
        parsed = yaml.safe_load(_OPT_IN[gate])
        types = parsed[True]["pull_request"]["types"]
        assert "synchronize" in types, gate

def test_p0_gate_reacts_to_edited_bodies():
    # The acknowledgment is written into the PR body, so `edited` must re-run it.
    parsed = yaml.safe_load(WORKFLOW_P0_GATE)
    assert "edited" in parsed[True]["pull_request"]["types"]

def test_release_notes_keeps_its_write_permission():
    # The callee patches the release body; a caller that withholds
    # `contents: write` makes the job fail as startup_failure.
    parsed = yaml.safe_load(WORKFLOW_RELEASE_NOTES)
    assert parsed["jobs"]["release-notes"]["permissions"] == {"contents": "write"}

def test_cnb_caller_declares_the_permissions_the_callee_needs():
    parsed = yaml.safe_load(WORKFLOW_CNB_OVERFLOW)
    assert parsed["permissions"] == {"statuses": "write", "actions": "read"}
    assert parsed["jobs"]["lane"]["secrets"] == "inherit"
    assert parsed["jobs"]["lane"]["with"]["target_repo"] == ("${{ github.event.repository.name }}")

def test_init_writes_only_the_three_required_callers_by_default(tmp_path):
    _ensure_workflows(tmp_path)
    written = sorted(p.name for p in (tmp_path / ".github/workflows").iterdir())
    assert written == ["ci-cache.yml", "commit-msg-lint.yml", "pr-title-check.yml"]

def test_init_writes_an_opt_in_gate_only_when_asked(tmp_path):
    _ensure_workflows(tmp_path, gates = ("p0", "verify"))
    written = sorted(p.name for p in (tmp_path / ".github/workflows").iterdir())
    assert written == ["ci-cache.yml", "commit-msg-lint.yml", "p0-gate.yml", "pr-title-check.yml", "verify-versions.yml"]
    assert (tmp_path / ".github/workflows/p0-gate.yml").read_bytes() == (WORKFLOW_P0_GATE.encode())

def test_opt_in_gate_respects_force(tmp_path):
    _ensure_workflows(tmp_path, gates = ("family",))
    target = tmp_path / ".github/workflows/family-versions.yml"
    target.write_text("hand-edited\n", encoding = "utf-8")
    _ensure_workflows(tmp_path, gates = ("family",))
    assert target.read_text(encoding = "utf-8") == "hand-edited\n"
    _ensure_workflows(tmp_path, gates = ("family",), force = True)
    assert target.read_bytes() == WORKFLOW_FAMILY_VERSIONS.encode()

def test_the_p0_gate_caller_cannot_collide_with_the_callee_concurrency_group():
    """A callee's `concurrency` is evaluated in the CALLER's context.

    When the generated caller used the literal group `p0-gate-${{ github.ref }}`
    — the same string the callee declares — the callee's group resolved to the
    caller's group, `cancel-in-progress` killed the run that invoked it, and the
    caller completed as a failure with zero jobs. GitHub surfaces that as
    "workflow file issue", so the first adopter (evernight) read it as a broken
    template and hand-wrote a different shape. Two invariants keep that from
    coming back: the ref is `master`, and the two groups are never the same
    expression.
    """
    caller = yaml.safe_load(WORKFLOW_P0_GATE)
    callee_path = Path(__file__).resolve().parent.parent / ".github/workflows/p0-gate.yml"
    callee = yaml.safe_load(callee_path.read_text(encoding = "utf-8"))

    caller_group = caller["concurrency"]["group"]
    callee_group = callee["concurrency"]["group"]
    assert caller_group != callee_group, ("the generated p0-gate caller must not reuse the callee's concurrency group: " "the callee evaluates it in the caller's context and cancels the very run " "that called it")
    # `synchronize` keeps the required check alive on every push, and the
    # dispatch entry is what lets an operator re-run the gate without a push.
    assert "synchronize" in caller[True]["pull_request"]["types"]
    assert "workflow_dispatch" in caller[True]

def test_short_hex_ref_is_rejected_with_the_fix_in_the_message(monkeypatch, tmp_path):
    """R2 F2: a 7/12-hex pin would hit `git clone --branch` at runtime and die
    opaquely; the CLI rejects it and names the fix (use the full 40-hex SHA)."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr("sys.argv", ["init", "--with-workflows", "--ref", "0123456"])
    assert init.main() == 2
    assert not (tmp_path / ".github" / "workflows").exists()

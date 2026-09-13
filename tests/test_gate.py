"""Tests for the local gate orchestrator (scheduler + build/gate)."""

import os
import threading
import time

import pytest

from celestia_devtools.build.gate import (
    UsageError,
    build_python_graph,
    build_rust_graph,
    build_web_graph,
    classify_credential_line,
    credential_sweep,
    detect_modes,
    discover_langyo_repos,
    git_config_paths,
    is_langyo_repo,
    iter_sweep_files,
    main as gate_main,
    precheck_mounts,
    resolve_modes,
    scan_file_credentials,
    scan_large_downloads,
)
from celestia_devtools.core.scheduler import (
    FAIL,
    PASS,
    SKIP,
    SKIP_DEP,
    Step,
    run_dag,
)


# ── Scheduler: dependency ordering ────────────────────────────────────────────

class TestSchedulerOrdering:
    def test_steps_run_after_dependencies(self):
        order = []
        lock = threading.Lock()

        def runner(step):
            with lock:
                order.append(step.name)
            return PASS

        steps = [
            Step("a", ["x"]),
            Step("b", ["x"], ("a",)),
            Step("c", ["x"], ("a",)),
            Step("d", ["x"], ("b", "c")),
        ]
        statuses = run_dag(steps, runner, jobs=2)
        assert statuses == {"a": PASS, "b": PASS, "c": PASS, "d": PASS}
        idx = {name: i for i, name in enumerate(order)}
        assert idx["a"] < idx["b"] < idx["d"]
        assert idx["a"] < idx["c"] < idx["d"]

    def test_conditional_skip_satisfies_dependents(self):
        def runner(step):
            return PASS

        steps = [
            Step("deny", None),  # conditionally skipped
            Step("lint", ["x"], ("deny",)),
        ]
        assert run_dag(steps, runner, jobs=1) == {"deny": SKIP, "lint": PASS}


class TestSchedulerBudget:
    def test_worker_cap_respected(self):
        current = 0
        peak = 0
        lock = threading.Lock()

        def runner(step):
            nonlocal current, peak
            with lock:
                current += 1
                peak = max(peak, current)
            time.sleep(0.08)
            with lock:
                current -= 1
            return PASS

        steps = [Step("s%d" % i, ["x"]) for i in range(6)]
        run_dag(steps, runner, jobs=3)
        assert 1 < peak <= 3

    def test_jobs_one_is_serial(self):
        current = 0
        peak = 0
        lock = threading.Lock()

        def runner(step):
            nonlocal current, peak
            with lock:
                current += 1
                peak = max(peak, current)
            time.sleep(0.02)
            with lock:
                current -= 1
            return PASS

        steps = [Step("s%d" % i, ["x"]) for i in range(4)]
        run_dag(steps, runner, jobs=1)
        assert peak == 1


class TestSchedulerFailFast:
    def test_failed_step_skips_dependents_only(self):
        calls = []

        def runner(step):
            calls.append(step.name)
            return FAIL if step.name == "a" else PASS

        steps = [
            Step("a", ["x"]),
            Step("b", ["x"], ("a",)),
            Step("c", ["x"]),
            Step("d", ["x"], ("b",)),
        ]
        statuses = run_dag(steps, runner, jobs=2)
        assert statuses == {"a": FAIL, "b": SKIP_DEP, "c": PASS, "d": SKIP_DEP}
        assert "b" not in calls and "d" not in calls


class TestSchedulerValidation:
    def test_cycle_detected(self):
        steps = [Step("a", ["x"], ("b",)), Step("b", ["x"], ("a",))]
        with pytest.raises(ValueError):
            run_dag(steps, lambda s: PASS)

    def test_unknown_dependency_detected(self):
        steps = [Step("a", ["x"], ("nope",))]
        with pytest.raises(ValueError):
            run_dag(steps, lambda s: PASS)

    def test_duplicate_names_detected(self):
        steps = [Step("a", ["x"]), Step("a", ["x"])]
        with pytest.raises(ValueError):
            run_dag(steps, lambda s: PASS)


# ── Mode detection ────────────────────────────────────────────────────────────

class TestDetectModes:
    def test_rust(self, tmp_path):
        (tmp_path / "Cargo.toml").write_text("")
        assert detect_modes(tmp_path) == ["rust"]

    def test_web_package_json(self, tmp_path):
        (tmp_path / "package.json").write_text("{}")
        assert detect_modes(tmp_path) == ["web"]

    def test_web_pnpm_workspace(self, tmp_path):
        (tmp_path / "pnpm-workspace.yaml").write_text("")
        assert detect_modes(tmp_path) == ["web"]

    def test_python(self, tmp_path):
        (tmp_path / "pyproject.toml").write_text("")
        assert detect_modes(tmp_path) == ["python"]

    def test_multi(self, tmp_path):
        (tmp_path / "Cargo.toml").write_text("")
        (tmp_path / "pyproject.toml").write_text("")
        assert detect_modes(tmp_path) == ["rust", "python"]

    def test_none(self, tmp_path):
        assert detect_modes(tmp_path) == []


class TestResolveModes:
    def test_explicit_mode(self, tmp_path):
        assert resolve_modes(tmp_path, "rust") == ["rust"]
        assert resolve_modes(tmp_path, "web") == ["web"]
        assert resolve_modes(tmp_path, "python") == ["python"]

    def test_all_uses_detected(self, tmp_path):
        (tmp_path / "Cargo.toml").write_text("")
        (tmp_path / "pyproject.toml").write_text("")
        assert resolve_modes(tmp_path, "all") == ["rust", "python"]

    def test_auto_detect(self, tmp_path):
        (tmp_path / "pyproject.toml").write_text("")
        assert resolve_modes(tmp_path, None) == ["python"]

    def test_nothing_detected_raises(self, tmp_path):
        with pytest.raises(UsageError):
            resolve_modes(tmp_path, None)


class TestGraphBuilders:
    def test_rust_order(self, tmp_path):
        assert [s.name for s in build_rust_graph(tmp_path)] == [
            "credential-scan", "fmt", "clippy", "check", "deny", "lint-commits",
        ]

    def test_rust_order_with_coverage(self, tmp_path):
        assert [s.name for s in build_rust_graph(tmp_path, coverage=True)] == [
            "credential-scan", "fmt", "clippy", "check", "deny", "coverage", "lint-commits",
        ]

    def test_web_order(self, tmp_path):
        assert [s.name for s in build_web_graph(tmp_path)] == [
            "credential-scan", "install", "lint", "build", "test", "lint-commits",
        ]

    def test_python_order(self, tmp_path):
        assert [s.name for s in build_python_graph(tmp_path)] == [
            "ruff-check", "ruff-format", "pytest", "lint-commits",
        ]

    def test_python_graph_is_linear(self, tmp_path):
        graph = build_python_graph(tmp_path)
        for step in graph[1:]:
            assert step.deps == (graph[graph.index(step) - 1].name,)


# ── Credential scan heuristics ────────────────────────────────────────────────

class TestCredentialScan:
    def test_clean_line(self):
        assert classify_credential_line("let answer = 42") == "clean"

    def test_placeholder_passwords_whitelisted(self):
        assert classify_credential_line('SSH_PASS="CHANGE_ME"') == "report"
        assert classify_credential_line('password = "<your-password>"') == "report"
        assert classify_credential_line('db_password = "test-password"') == "report"

    def test_token_and_key_placeholders_whitelisted(self):
        assert classify_credential_line('api_key = "sk-xxx"') == "report"
        assert classify_credential_line('token = "xxxx"') == "report"

    def test_rfc5737_ip_whitelisted(self):
        assert classify_credential_line('api_key = "192.0.2.5"') == "report"
        assert classify_credential_line('api_key = "198.51.100.1"') == "report"
        assert classify_credential_line('api_key = "203.0.113.9"') == "report"

    def test_env_reference_not_a_violation(self):
        assert classify_credential_line('password = os.getenv("DB_PASSWORD")') == "report"
        assert classify_credential_line("token = process.env.TOKEN") == "report"

    def test_real_secret_is_violation(self):
        assert classify_credential_line('SSH_PASS="s3cr3t-value-123"') == "violation"
        assert classify_credential_line("--target-pass s3cr3t-value-123") == "violation"
        assert classify_credential_line('password = "supersecret"') == "violation"

    def test_bare_mention_reported_not_failed(self):
        assert classify_credential_line("# set your token here") == "report"

    def test_private_key_header_is_violation(self):
        assert classify_credential_line("-----BEGIN RSA PRIVATE KEY-----") == "violation"


# ── precheck: mount points + large downloads ──────────────────────────────────

class TestPrecheckMounts:
    def test_worktree_mountpoint_warns(self, tmp_path):
        mounts = [("/mnt/codespace/_worktree/hikari/hikari", "nfs4")]
        warnings = precheck_mounts(mounts, tmp_path)
        assert len(warnings) == 1
        assert "_worktree" in warnings[0]

    def test_cwd_mountpoint_warns(self, tmp_path):
        mounts = [(str(tmp_path), "nfs")]
        warnings = precheck_mounts(mounts, tmp_path)
        assert any("current directory" in w for w in warnings)

    def test_plain_nfs_ignored(self, tmp_path):
        assert precheck_mounts([("/data", "nfs")], tmp_path) == []

    def test_non_nfs_ignored(self, tmp_path):
        mounts = [("/mnt/codespace/_worktree/foo", "ext4")]
        assert precheck_mounts(mounts, tmp_path) == []


class TestFindmntMounts:
    def test_parses_output(self, monkeypatch):
        import celestia_devtools.build.gate as gate_mod

        class FakeProc:
            returncode = 0
            stdout = "/mnt/codespace nfs4\n/tmp/foo ext4\n"

        def fake_run(cmd, **kwargs):
            assert cmd == ["findmnt", "-rn", "-o", "TARGET,FSTYPE"]
            return FakeProc()

        monkeypatch.setattr(gate_mod.subprocess, "run", fake_run)
        assert gate_mod.findmnt_mounts() == [
            ("/mnt/codespace", "nfs4"),
            ("/tmp/foo", "ext4"),
        ]

    def test_missing_findmnt_returns_empty(self, monkeypatch):
        import celestia_devtools.build.gate as gate_mod

        def fake_run(cmd, **kwargs):
            raise OSError("findmnt not found")

        monkeypatch.setattr(gate_mod.subprocess, "run", fake_run)
        assert gate_mod.findmnt_mounts() == []


class TestLargeDownloadScan:
    def test_warns_without_hint(self, tmp_path):
        (tmp_path / "dl.py").write_text(
            "from huggingface_hub import hf_hub_download\nhf_hub_download('x')\n"
        )
        warnings = scan_large_downloads(tmp_path)
        assert len(warnings) == 1
        assert "dl.py" in warnings[0]

    def test_no_warning_with_hint(self, tmp_path):
        (tmp_path / "dl.py").write_text(
            "import os\nos.environ['HF_HUB_DISABLE_XET'] = '1'\n"
            "from huggingface_hub import hf_hub_download\n"
        )
        assert scan_large_downloads(tmp_path) == []

    def test_ignores_other_files(self, tmp_path):
        (tmp_path / "notes.txt").write_text("huggingface_hub download")
        assert scan_large_downloads(tmp_path) == []


# ── Credential scan: embedded tokens (.git/config form) ───────────────────────
#
# Synthetic token values only — never a real credential (§10.1).

FAKE_TOKEN = "gho_0123456789abcdefghij"
FAKE_PAT = "github_pat_0123456789abcdefghijkl"


def make_checkout(root, name="leaky", remote_url=None, extra="", files=None):
    """Create a minimal checkout: <root>/<name>/.git/config + optional files."""
    repo = root / name
    (repo / ".git").mkdir(parents=True)
    if remote_url is not None:
        (repo / ".git" / "config").write_text(
            '[core]\n\trepositoryformatversion = 0\n[remote "origin"]\n\turl = %s\n%s'
            % (remote_url, extra),
            encoding="utf-8",
        )
    for rel, text in (files or {}).items():
        target = repo / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, encoding="utf-8")
    return repo


class TestEmbeddedCredentialUrls:
    def test_git_config_remote_with_token_is_violation(self):
        line = "url = https://oauth2:%s@github.com/celestia-island/arona.git" % FAKE_TOKEN
        assert classify_credential_line(line) == "violation"

    def test_x_access_token_userinfo_is_violation(self):
        line = "\turl = https://x-access-token:%s@github.com/o/r.git" % FAKE_TOKEN
        assert classify_credential_line(line) == "violation"

    def test_placeholder_userinfo_is_report(self):
        assert classify_credential_line(
            "url = https://oauth2:<your-token>@github.com/o/r.git") == "report"
        assert classify_credential_line(
            "url = https://oauth2:${GITHUB_TOKEN}@github.com/o/r.git") == "report"
        assert classify_credential_line(
            "url = https://oauth2:gho_xxxxxxxxxxxxxxxxxxxx@github.com/o/r.git") == "report"

    def test_bare_provider_tokens_are_violations(self):
        assert classify_credential_line("remote: %s" % FAKE_TOKEN) == "violation"
        assert classify_credential_line(FAKE_PAT) == "violation"
        assert classify_credential_line("token = os.getenv('GH_TOKEN')") == "report"

    def test_ordinary_remotes_stay_clean(self):
        assert classify_credential_line("url = git@github.com:celestia-island/arona.git") == "clean"
        assert classify_credential_line('[remote "origin"]') == "clean"
        assert classify_credential_line("url = https://github.com/celestia-island/arona.git") == "clean"

    def test_concatenated_url_parts_stay_clean(self):
        assert classify_credential_line('"https://" + user + ":" + pw + "@host"') == "clean"

    def test_your_dash_placeholders_stay_reports(self):
        """The real ``your-<noun>-secret`` convention must stay whitelisted.

        easy-hydro-erp/.env.example (tracked) uses exactly this shape; an
        over-tightened placeholder pattern would flag it as a live secret.
        """
        assert classify_credential_line("UNIFIED_APP_SECRET=your-unified-app-secret") == "report"
        assert classify_credential_line("MP_JWT_SECRET=your-jwt-secret") == "report"
        assert classify_credential_line("ERP_JWT_SECRET=your_app_token") == "report"

    def test_literals_starting_with_your_stay_violations(self):
        """A placeholder alternative must not swallow real values.

        ``TOKEN="yoursecret-real-value"`` contains "your…" — an unanchored
        ``your(token|secret|api_key)`` alternative would downgrade it to
        "report" and silently stop flagging a committed secret.
        """
        assert classify_credential_line('TOKEN="yoursecret-real-value"') == "violation"
        assert classify_credential_line('API_KEY="yourapikey-9f2b"') == "violation"

    def test_short_userinfo_examples_stay_reports(self):
        """Doc-shaped URLs must keep their pre-extension verdict.

        AGENTS §10.1.2 requires RFC 5737 addresses in examples, so a URL
        userinfo there is a placeholder, not a credential.
        """
        assert classify_credential_line(
            "proxy = http://user:pass@192.0.2.1:3128") == "report"
        assert classify_credential_line(
            "proxy = http://user:pass@example.internal:3128") == "report"
        assert classify_credential_line(
            "url = https://oauth2:<your-token>@github.com/o/r.git") == "report"


class TestGitConfigSweep:
    def test_git_config_paths_for_plain_checkout(self, tmp_path):
        repo = make_checkout(tmp_path, remote_url="https://github.com/x/y.git")
        assert git_config_paths(repo) == [repo / ".git" / "config"]

    def test_git_config_paths_follows_worktree_pointer(self, tmp_path):
        main = tmp_path / "main"
        (main / ".git" / "worktrees" / "wt").mkdir(parents=True)
        (main / ".git" / "config").write_text("[core]\n", encoding="utf-8")
        worktree = tmp_path / "wt"
        worktree.mkdir()
        (worktree / ".git").write_text("gitdir: %s\n" % (main / ".git" / "worktrees" / "wt"))
        assert git_config_paths(worktree) == [main / ".git" / "config"]

    def test_scan_file_credentials_reports_only_non_clean(self, tmp_path):
        config = tmp_path / "config"
        config.write_text(
            "[remote \"origin\"]\n\turl = https://oauth2:%s@github.com/o/r.git\n"
            "[user]\n\tname = dev\n" % FAKE_TOKEN,
            encoding="utf-8",
        )
        findings = scan_file_credentials(config)
        assert len(findings) == 1
        assert findings[0].verdict == "violation"
        assert findings[0].line == 2
        assert FAKE_TOKEN in findings[0].render()

    def test_sweep_flags_embedded_git_config_token(self, tmp_path):
        repo = make_checkout(
            tmp_path, remote_url="https://oauth2:%s@github.com/o/r.git" % FAKE_TOKEN
        )
        violations = [f for f in credential_sweep([repo]) if f.verdict == "violation"]
        assert len(violations) == 1
        assert violations[0].path == repo / ".git" / "config"

    def test_sweep_of_clean_checkout_is_empty(self, tmp_path):
        repo = make_checkout(
            tmp_path,
            remote_url="https://github.com/celestia-island/arona.git",
            files={"src/main.rs": "fn main() {}\n", "README.md": "no secrets here\n"},
        )
        assert [f for f in credential_sweep([repo]) if f.verdict == "violation"] == []

    def test_sweep_can_skip_git_config(self, tmp_path):
        repo = make_checkout(
            tmp_path, remote_url="https://oauth2:%s@github.com/o/r.git" % FAKE_TOKEN
        )
        assert credential_sweep([repo], include_git_config=False) == []

    def test_sweep_can_skip_sources(self, tmp_path):
        repo = make_checkout(
            tmp_path,
            remote_url="https://github.com/o/r.git",
            files={"docs/x.md": "FEISHU_APP_SECRET=%s\n" % FAKE_TOKEN},
        )
        assert credential_sweep([repo], include_sources=False) == []
        assert [f for f in credential_sweep([repo]) if f.verdict == "violation"]


class TestLangyoRepoCoverage:
    def test_langyo_remote_detected(self, tmp_path):
        repo = make_checkout(
            tmp_path, name="easy-hydro-erp", remote_url="https://github.com/langyo/easy-hydro-erp.git"
        )
        assert is_langyo_repo(repo)

    def test_langyo_remote_with_embedded_token_detected(self, tmp_path):
        repo = make_checkout(
            tmp_path,
            name="biz",
            remote_url="https://oauth2:%s@github.com/langyo/easy-hydro-nas.git" % FAKE_TOKEN,
        )
        assert is_langyo_repo(repo)

    def test_org_repo_not_langyo(self, tmp_path):
        repo = make_checkout(
            tmp_path, name="arona", remote_url="https://github.com/celestia-island/arona.git"
        )
        assert not is_langyo_repo(repo)

    def test_easy_hydro_name_fallback(self, tmp_path):
        repo = make_checkout(tmp_path, name="easy-hydro-stations-2")
        assert is_langyo_repo(repo)

    def test_discover_langyo_repos(self, tmp_path):
        make_checkout(tmp_path, name="arona", remote_url="https://github.com/celestia-island/arona.git")
        make_checkout(tmp_path, name="biz", remote_url="https://github.com/langyo/easy-hydro-erp.git")
        found = [p.name for p in discover_langyo_repos(tmp_path)]
        assert found == ["biz"]

    def test_discover_ignores_missing_scan_root(self, tmp_path):
        assert discover_langyo_repos(tmp_path / "nope") == []


class TestCredentialScanCli:
    def test_flags_langyo_checkout_and_exits_one(self, tmp_path, capsys):
        scan_root = tmp_path / "ws"
        scan_root.mkdir()
        repo = make_checkout(
            scan_root,
            name="easy-hydro-erp",
            remote_url="https://oauth2:%s@github.com/langyo/easy-hydro-erp.git" % FAKE_TOKEN,
            files={"src/app.py": "TOKEN = '%s'\n" % FAKE_TOKEN},
        )
        rc = gate_main(
            ["credential-scan", "--repo-root", str(tmp_path), "--scan-root", str(scan_root),
             "--repo", repo.name]
        )
        err = capsys.readouterr().err
        assert rc == 1
        assert ".git/config" in err and "src/app.py" in err

    def test_clean_checkout_exits_zero(self, tmp_path, capsys):
        scan_root = tmp_path / "ws"
        scan_root.mkdir()
        repo = make_checkout(
            scan_root, name="arona", remote_url="https://github.com/celestia-island/arona.git"
        )
        rc = gate_main(
            ["credential-scan", "--repo-root", str(tmp_path), "--scan-root", str(scan_root),
             "--repo", repo.name]
        )
        assert rc == 0
        captured = capsys.readouterr()
        assert "1 path(s)" in captured.out
        assert "[violation]" not in captured.err

    def test_unknown_repo_is_usage_error(self, tmp_path, capsys):
        rc = gate_main(
            ["credential-scan", "--repo-root", str(tmp_path), "--scan-root", str(tmp_path),
             "--repo", "absent"]
        )
        assert rc == 2
        assert "no checkout at" in capsys.readouterr().err

    def test_all_langyo_sweeps_business_repos(self, tmp_path, capsys):
        scan_root = tmp_path / "ws"
        scan_root.mkdir()
        make_checkout(scan_root, name="arona", remote_url="https://github.com/celestia-island/arona.git")
        make_checkout(
            scan_root, name="biz",
            remote_url="https://oauth2:%s@github.com/langyo/easy-hydro-erp.git" % FAKE_TOKEN,
        )
        rc = gate_main(
            ["credential-scan", "--repo-root", str(tmp_path), "--scan-root", str(scan_root),
             "--all-langyo"]
        )
        assert rc == 1
        assert "1 path(s)" in capsys.readouterr().out

    def test_git_config_only_skips_sources(self, tmp_path, capsys):
        scan_root = tmp_path / "ws"
        scan_root.mkdir()
        repo = make_checkout(
            scan_root, name="biz",
            remote_url="https://github.com/langyo/easy-hydro-erp.git",
            files={"docs/nas-integration.md": "FEISHU_APP_SECRET=%s\n" % FAKE_TOKEN},
        )
        rc = gate_main(
            ["credential-scan", "--repo-root", str(tmp_path), "--scan-root", str(scan_root),
             "--repo", repo.name, "--git-config-only"]
        )
        assert rc == 0
        capsys.readouterr()

    def test_reports_are_not_fatal(self, tmp_path, capsys):
        scan_root = tmp_path / "ws"
        scan_root.mkdir()
        repo = make_checkout(
            scan_root, name="biz",
            remote_url="https://github.com/langyo/easy-hydro-erp.git",
            files={"docs/x.md": "api_key = \"<your-api-key>\"\n"},
        )
        rc = gate_main(
            ["credential-scan", "--repo-root", str(tmp_path), "--scan-root", str(scan_root),
             "--repo", repo.name, "--show-reports"]
        )
        assert rc == 0
        assert "your-api-key" in capsys.readouterr().err

    def test_contradictory_scan_flags_are_usage_errors(self, tmp_path, capsys):
        rc = gate_main(
            ["credential-scan", "--repo-root", str(tmp_path), "--git-config-only",
             "--no-git-config"]
        )
        assert rc == 2
        assert "mutually exclusive" in capsys.readouterr().err

    def test_all_langyo_without_matches_is_an_error(self, tmp_path, capsys):
        scan_root = tmp_path / "ws"
        scan_root.mkdir()
        make_checkout(
            scan_root, name="arona", remote_url="https://github.com/celestia-island/arona.git"
        )
        rc = gate_main(
            ["credential-scan", "--repo-root", str(tmp_path), "--scan-root", str(scan_root),
             "--all-langyo"]
        )
        assert rc == 2
        assert "refusing to report a clean sweep of nothing" in capsys.readouterr().err

    def test_unreadable_subtree_is_reported_and_does_not_abort(self, tmp_path, capsys):
        if os.geteuid() == 0:
            pytest.skip("running as root: mode 000 does not deny access")
        scan_root = tmp_path / "ws"
        scan_root.mkdir()
        repo = make_checkout(
            scan_root, name="biz", remote_url="https://github.com/langyo/easy-hydro-erp.git"
        )
        blocked = repo / "src" / "blocked"
        blocked.mkdir(parents=True)
        (blocked / "leak.py").write_text('TOKEN = "%s"\n' % FAKE_TOKEN, encoding="utf-8")
        os.chmod(blocked, 0o000)
        try:
            rc = gate_main(
                ["credential-scan", "--repo-root", str(tmp_path), "--scan-root", str(scan_root),
                 "--repo", repo.name]
            )
            err = capsys.readouterr().err
            assert rc == 0, "an unreadable subtree must not crash the sweep"
            assert "skipped:" in err
            assert "blocked" in err
        finally:
            os.chmod(blocked, 0o755)


class TestSweepCrashSafety:
    """A scanner that dies mid-sweep reports nothing — every loss must be loud."""

    def test_discover_langyo_repos_survives_unreadable_sibling(self, tmp_path):
        if os.geteuid() == 0:
            pytest.skip("running as root: mode 000 does not deny access")
        scan_root = tmp_path / "ws"
        scan_root.mkdir()
        make_checkout(
            scan_root, name="biz", remote_url="https://github.com/langyo/easy-hydro-erp.git"
        )
        # Reproduces /mnt/codespace/lost+found: stat-able itself, but
        # <child>/.git raises PermissionError from Path.exists().
        lost = scan_root / "lost+found"
        lost.mkdir()
        os.chmod(lost, 0o000)
        skipped = []
        try:
            repos = discover_langyo_repos(scan_root, skipped=skipped)
            assert [repo.name for repo in repos] == ["biz"]
            assert [entry.path.name for entry in skipped] == ["lost+found"]
            assert "PermissionError" in skipped[0].reason
        finally:
            os.chmod(lost, 0o755)

    def test_broken_symlink_is_skipped_with_reason(self, tmp_path):
        root = tmp_path / "repo"
        root.mkdir()
        (root / "ok.py").write_text("x = 1\n", encoding="utf-8")
        os.symlink(root / "gone.py", root / "dangling.py")
        skipped = []
        files = list(iter_sweep_files(root, skipped=skipped))
        assert [path.name for path in files] == ["ok.py"]
        assert [entry.path.name for entry in skipped] == ["dangling.py"]
        assert "broken symlink" in skipped[0].reason

    def test_fifo_is_skipped_with_reason(self, tmp_path):
        root = tmp_path / "repo"
        root.mkdir()
        os.mkfifo(root / "pipe")
        skipped = []
        assert list(iter_sweep_files(root, skipped=skipped)) == []
        assert [entry.path.name for entry in skipped] == ["pipe"]
        assert "not a regular file" in skipped[0].reason

    def test_unreadable_file_is_a_reported_skip_not_a_clean_read(self, tmp_path):
        if os.geteuid() == 0:
            pytest.skip("running as root: mode 000 does not deny access")
        root = tmp_path / "repo"
        root.mkdir()
        secret = root / "leak.py"
        secret.write_text('TOKEN = "%s"\n' % FAKE_TOKEN, encoding="utf-8")
        os.chmod(secret, 0o000)
        skipped = []
        try:
            findings = credential_sweep([root], include_git_config=False, skipped=skipped)
            assert findings == []
            assert [entry.path.name for entry in skipped] == ["leak.py"]
            assert "PermissionError" in skipped[0].reason
            with pytest.raises(OSError):
                scan_file_credentials(secret)
        finally:
            os.chmod(secret, 0o644)

    def test_fail_on_skip_turns_an_incomplete_sweep_into_a_failure(self, tmp_path, capsys):
        if os.geteuid() == 0:
            pytest.skip("running as root: mode 000 does not deny access")
        scan_root = tmp_path / "ws"
        scan_root.mkdir()
        repo = make_checkout(
            scan_root, name="biz", remote_url="https://github.com/langyo/easy-hydro-erp.git"
        )
        blocked = repo / "docs"
        blocked.mkdir()
        os.chmod(blocked, 0o000)
        try:
            rc = gate_main(
                ["credential-scan", "--repo-root", str(tmp_path), "--scan-root", str(scan_root),
                 "--repo", repo.name, "--fail-on-skip"]
            )
            err = capsys.readouterr().err
            # exit 1 is the --fail-on-skip contract; the skip list itself is
            # printed directly (the shared logger binds sys.stdout at import).
            assert rc == 1
            assert "skipped:" in err and "docs" in err
        finally:
            os.chmod(blocked, 0o755)

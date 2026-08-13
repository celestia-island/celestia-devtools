"""Tests for the local gate orchestrator (scheduler + build/gate)."""

import threading
import time

import pytest

from celestia_devtools.build.gate import (
    UsageError,
    build_python_graph,
    build_rust_graph,
    build_web_graph,
    classify_credential_line,
    detect_modes,
    precheck_mounts,
    resolve_modes,
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

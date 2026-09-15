#!/usr/bin/env python3
"""Tests for ``celestia_devtools.ci.job_timeouts``.

Covers: every row of the policy table, insertion point and indentation, idempotency,
multi-line ``runs-on``, caller jobs, quoted job names, CRLF, a missing trailing newline,
tab indentation rejection, ``--dry-run`` not writing, a directory without
``.github/workflows``, "still valid YAML with the same job count" after insertion,
self-hosted-only-by-default scope, ``--policy-override`` and the exit codes.
"""

import os

import pytest

from celestia_devtools.ci.job_timeouts import (
    WorkflowParseError,
    main,
    scan_workflow_text,
    suggest,
)

try:  # PyYAML 用于交叉校验“插入后仍是合法 YAML”——见下面的 ImportError 说明。
    import yaml
except ImportError:  # pragma: no cover - 环境不完整时必须在收集阶段就炸掉
    raise ImportError(
        "PyYAML is required by this test module: it cross-checks that every insertion keeps "
        "the workflow valid YAML. Without it that guarantee would silently stop being tested, "
        "so a missing PyYAML is a hard error rather than a skip. "
        "Install the dev extras:  pip install -e '.[dev]'  (pytest + ruff + PyYAML)."
    ) from None


# ── fixture 辅助 ─────────────────────────────────────────────────────────────


def _workflow(root, text, filename="ci.yml"):
    """Write ``<root>/.github/workflows/<filename>`` (``newline=""`` to control endings exactly)."""
    directory = root / ".github" / "workflows"
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / filename
    with path.open("w", encoding="utf-8", newline="") as handle:
        handle.write(text)
    return path


def _read(path):
    """Read with ``newline=""`` so universal newlines cannot turn CRLF into LF and hide bugs."""
    with path.open("r", encoding="utf-8", newline="") as handle:
        return handle.read()


def _simple(text):
    """A minimal single-job self-hosted workflow."""
    return "name: CI\non:\n  push:\n    branches: [master]\njobs:\n  test:\n" + text


# ── 策略表：9 条逐条 ─────────────────────────────────────────────────────────


class TestSuggestRule1Bench:
    """Rule 1: a name containing bench -> 360."""

    def test_run_benchmarks_gets_360(self):
        assert suggest("Run Benchmarks", "ubuntu-latest", "") == 360

    def test_bench_case_insensitive(self):
        assert suggest("BENCH harness", "ubuntu-latest", "") == 360


class TestSuggestRule2Fuzz:
    """Rule 2: a name containing fuzz -> 180."""

    def test_fuzz_targets_get_180(self):
        assert suggest("Fuzz targets", "[self-hosted, linux]", "") == 180


class TestSuggestRule3Coverage:
    """Rule 3: a name containing coverage -> 180."""

    def test_coverage_gets_180(self):
        assert suggest("Coverage", "[self-hosted, linux]", "") == 180


class TestSuggestRule4HeavyJobs:
    """Rule 4: install / e2e / integration / smoke / drill / docker -> 180."""

    @pytest.mark.parametrize(
        "name",
        ["Install test", "E2E", "integration suite", "smoke test", "drill", "docker"],
    )
    def test_heavy_names_get_180(self, name):
        assert suggest(name, "[self-hosted, linux]", "") == 180


class TestSuggestRule5ReleaseJobs:
    """Rule 5: release / publish / deploy -> 120."""

    @pytest.mark.parametrize("name", ["Release", "Publish crates", "deploy"])
    def test_release_names_get_120(self, name):
        assert suggest(name, "ubuntu-latest", "") == 120


class TestSuggestRule6CargoBody:
    """Rule 6: the job body contains cargo -> 300."""

    def test_clippy_stable_with_cargo_body_gets_300(self):
        # 真实样本：tairitsu `Clippy (stable)` 有 2h23m 的成功记录。
        assert suggest("Clippy (stable)", "[self-hosted, linux, x64, local]", "run: cargo clippy") == 300

    def test_cargo_body_beats_light_name(self):
        # 规则 6 排在规则 8 之前：`Fmt` 体内跑 cargo 时按编译档给 300。
        assert suggest("Fmt", "[self-hosted, linux]", "steps:\n  - run: cargo fmt --check") == 300

    def test_cargo_keyword_is_case_insensitive(self):
        assert suggest("test", "[self-hosted, linux]", "uses: CARGO_HOME=/x") == 300


class TestSuggestRule7FrontendBody:
    """Rule 7: the job body contains pnpm / npm / yarn / vite / vue-tsc -> 120."""

    def test_lint_name_with_pnpm_body_gets_120(self):
        assert suggest("Lint", "[self-hosted, linux]", "run: pnpm build") == 120

    @pytest.mark.parametrize("tool", ["npm ci", "yarn install", "vite build", "vue-tsc -b"])
    def test_other_frontend_tools_get_120(self, tool):
        assert suggest("build", "[self-hosted, linux]", f"run: {tool}") == 120


class TestSuggestRule8LightNames:
    """Rule 8: a light job name and no cargo / frontend toolchain in the body -> 60."""

    def test_lint_name_without_toolchain_gets_60(self):
        assert suggest("Lint", "[self-hosted, linux]", "run: python3 -m ruff check .") == 60

    @pytest.mark.parametrize(
        "name",
        ["fmt", "format", "Lint commit messages", "PR Title Check", "secrets-scan",
         "docs", "audit", "deny", "markdown", "i18n", "sign", "verify-versions",
         "sync"],
    )
    def test_light_names_get_60(self, name):
        assert suggest(name, "[self-hosted, linux]", "") == 60

    def test_cargo_only_counts_in_the_body_not_the_name(self):
        # ``cargo deny`` 是轻量作业（实测 evernight 的 deny 作业）；只有当它**体内**
        # 真的跑 cargo 时才升到 300。名称里的 "cargo" 不参与第 6 条判定。
        assert suggest("cargo deny", "[self-hosted, linux]", "") == 60
        assert suggest("cargo deny", "[self-hosted, linux]", "run: cargo deny check") == 300


class TestSuggestRule9Default:
    """Rule 9: everything else -> 120."""

    def test_unknown_job_gets_120(self):
        assert suggest("probe-plana", "[self-hosted, linux]", "run: echo hi") == 120

    def test_default_is_120(self):
        assert suggest("whatever", None, "") == 120


class TestSuggestPrecedence:
    """Name rules take precedence over job-body rules."""

    def test_bench_beats_cargo_body(self):
        assert suggest("bench", "[self-hosted, linux]", "cargo bench") == 360

    def test_docker_beats_cargo_body(self):
        assert suggest("docker", "[self-hosted, linux]", "cargo build") == 180


# ── 插入行为 ─────────────────────────────────────────────────────────────────


class TestFixSimpleJob:
    """(1) Simple job: correct value and indentation matching ``runs-on``."""

    def test_value_and_indent(self, tmp_path):
        path = _workflow(tmp_path, _simple("    runs-on: [self-hosted, linux, x64, local]\n"
                                           "    steps:\n      - run: cargo test\n"))
        assert main(["fix", str(tmp_path)]) == 0
        text = _read(path)
        assert "    timeout-minutes: 300\n" in text
        assert text.index("runs-on:") < text.index("timeout-minutes:")
        assert text.index("timeout-minutes:") < text.index("steps:")

    def test_single_line_inserted_immediately_after_runs_on(self, tmp_path):
        path = _workflow(tmp_path, _simple("    runs-on: [self-hosted, linux]\n"
                                           "    steps:\n      - run: echo hi\n"))
        main(["fix", str(tmp_path)])
        lines = _read(path).split("\n")
        index = lines.index("    runs-on: [self-hosted, linux]")
        assert lines[index + 1] == "    timeout-minutes: 120"

    def test_deeper_indentation_is_preserved(self, tmp_path):
        path = _workflow(tmp_path, "jobs:\n  test:\n      runs-on: [self-hosted, linux]\n")
        main(["fix", str(tmp_path)])
        assert "      timeout-minutes: 120" in _read(path)


class TestFixIdempotent:
    """(2) An existing timeout is untouched; a second run changes nothing."""

    def test_existing_timeout_untouched(self, tmp_path):
        original = _simple("    runs-on: [self-hosted, linux]\n    timeout-minutes: 7\n")
        path = _workflow(tmp_path, original)
        before = os.stat(path).st_mtime_ns
        assert main(["fix", str(tmp_path)]) == 0
        assert _read(path) == original
        assert os.stat(path).st_mtime_ns == before

    def test_second_run_changes_nothing(self, tmp_path, capsys):
        path = _workflow(tmp_path, _simple("    runs-on: [self-hosted, linux]\n"
                                           "    steps:\n      - run: cargo test\n"))
        assert main(["fix", str(tmp_path)]) == 0
        first = _read(path)
        capsys.readouterr()
        assert main(["fix", str(tmp_path)]) == 0
        assert _read(path) == first
        assert "inserted 0 line(s) in 0 file(s)" in capsys.readouterr().out


class TestFixMultilineRunsOn:
    """(3) Multi-line ``runs-on``: insert after the last item, otherwise the YAML becomes invalid."""

    def test_insert_after_last_list_item(self, tmp_path):
        path = _workflow(tmp_path, "jobs:\n"
                                   "  test:\n"
                                   "    runs-on:\n"
                                   "      - self-hosted\n"
                                   "      - linux\n"
                                   "    steps:\n"
                                   "      - run: echo hi\n")
        assert main(["fix", str(tmp_path)]) == 0
        lines = _read(path).split("\n")
        assert lines.index("    timeout-minutes: 120") == lines.index("      - linux") + 1

    def test_multiline_result_is_still_valid_yaml(self, tmp_path):
        path = _workflow(tmp_path, "jobs:\n"
                                   "  test:\n"
                                   "    runs-on:\n"
                                   "      - self-hosted\n"
                                   "      - linux\n")
        main(["fix", str(tmp_path)])
        document = yaml.safe_load(_read(path))
        assert document["jobs"]["test"]["runs-on"] == ["self-hosted", "linux"]
        assert document["jobs"]["test"]["timeout-minutes"] == 120


class TestFixCallerJob:
    """(4) Caller job (``uses:`` only): inserted after ``uses:``; out of scope by default."""

    CALLER = ("jobs:\n"
              "  verify-versions:\n"
              "    uses: celestia-island/celestia-devtools/.github/workflows/verify-versions.yml@master\n"
              "    secrets: inherit\n")

    def test_caller_skipped_by_default(self, tmp_path):
        path = _workflow(tmp_path, self.CALLER)
        assert main(["fix", str(tmp_path)]) == 0
        assert _read(path) == self.CALLER

    def test_caller_fixed_with_include_callers(self, tmp_path):
        path = _workflow(tmp_path, self.CALLER)
        assert main(["fix", str(tmp_path), "--include-callers"]) == 0
        lines = _read(path).split("\n")
        index = lines.index(
            "    uses: celestia-island/celestia-devtools/.github/workflows/verify-versions.yml@master"
        )
        assert lines[index + 1] == "    timeout-minutes: 60"


class TestFixQuotedJobName:
    """(5) Quoted job names."""

    def test_double_quoted_name(self, tmp_path):
        path = _workflow(tmp_path, 'jobs:\n  "test":\n    runs-on: [self-hosted, linux]\n')
        assert main(["fix", str(tmp_path)]) == 0
        assert "    timeout-minutes: 120\n" in _read(path)

    def test_single_quoted_name(self, tmp_path):
        path = _workflow(tmp_path, "jobs:\n  'Run Benchmarks':\n"
                                   "    runs-on: [self-hosted, linux]\n")
        assert main(["fix", str(tmp_path)]) == 0
        assert "    timeout-minutes: 360\n" in _read(path)


class TestFixDryRun:
    """(7) ``--dry-run`` writes nothing (content and mtime both unchanged)."""

    def test_dry_run_does_not_write(self, tmp_path, capsys):
        original = _simple("    runs-on: [self-hosted, linux]\n"
                           "    steps:\n      - run: cargo test\n")
        path = _workflow(tmp_path, original)
        before = os.stat(path).st_mtime_ns
        assert main(["fix", str(tmp_path), "--dry-run"]) == 0
        assert _read(path) == original
        assert os.stat(path).st_mtime_ns == before
        out = capsys.readouterr().out
        assert "timeout-minutes: 300" in out
        assert "would insert 1 line(s) in 1 file(s)" in out
        assert "dry-run, no file written" in out


class TestCrlfPreserved:
    """(8) A CRLF file keeps CRLF endings after insertion."""

    def test_crlf_stays_crlf(self, tmp_path):
        original = ("name: CI\r\non:\r\n  push:\r\n    branches: [master]\r\n"
                    "jobs:\r\n  test:\r\n    runs-on: [self-hosted, linux]\r\n"
                    "    steps:\r\n      - run: cargo test\r\n")
        path = _workflow(tmp_path, original)
        assert main(["fix", str(tmp_path)]) == 0
        text = _read(path)
        assert "\r\n" in text
        assert "\n" not in text.replace("\r\n", "")  # 没有裸 LF
        assert "    timeout-minutes: 300\r\n" in text
        # 除新增行外逐字节不变
        assert text.replace("    timeout-minutes: 300\r\n", "") == original

    def test_crlf_is_idempotent(self, tmp_path):
        path = _workflow(tmp_path, "jobs:\r\n  test:\r\n    runs-on: [self-hosted, linux]\r\n")
        main(["fix", str(tmp_path)])
        after_first = _read(path)
        main(["fix", str(tmp_path)])
        assert _read(path) == after_first


class TestNoTrailingNewline:
    """A file without a trailing newline still yields parseable YAML and gains no trailing newline."""

    def test_no_trailing_newline(self, tmp_path):
        path = _workflow(tmp_path, "jobs:\n  test:\n    runs-on: [self-hosted, linux]")
        assert main(["fix", str(tmp_path)]) == 0
        text = _read(path)
        assert text == ("jobs:\n  test:\n    runs-on: [self-hosted, linux]\n"
                        "    timeout-minutes: 120")
        assert yaml.safe_load(text)["jobs"]["test"]["timeout-minutes"] == 120

    def test_no_trailing_newline_is_idempotent(self, tmp_path):
        path = _workflow(tmp_path, "jobs:\n  test:\n    runs-on: [self-hosted, linux]")
        main(["fix", str(tmp_path)])
        first = _read(path)
        main(["fix", str(tmp_path)])
        assert _read(path) == first


class TestNoWorkflowsDirectory:
    """(9) A directory without ``.github/workflows`` is not an error."""

    def test_audit_without_workflows(self, tmp_path, capsys):
        assert main(["audit", str(tmp_path)]) == 0
        assert "0/0 jobs missing timeout-minutes" in capsys.readouterr().out

    def test_fix_without_workflows(self, tmp_path, capsys):
        assert main(["fix", str(tmp_path)]) == 0
        assert "inserted 0 line(s) in 0 file(s)" in capsys.readouterr().out

    def test_yaml_extension_is_scanned(self, tmp_path):
        path = _workflow(tmp_path, _simple("    runs-on: [self-hosted, linux]\n"),
                         filename="build.yaml")
        assert main(["fix", str(tmp_path)]) == 0
        assert "timeout-minutes: 120" in _read(path)


class TestYamlStillValid:
    """(10) After insertion the file is still valid YAML with an unchanged job count."""

    def test_valid_yaml_and_job_count(self, tmp_path):
        path = _workflow(tmp_path, "name: CI\n"
                                   "on: push\n"
                                   "jobs:\n"
                                   "  alpha:\n"
                                   "    runs-on: [self-hosted, linux]\n"
                                   "    steps:\n"
                                   "      - run: cargo test\n"
                                   "  beta:\n"
                                   "    runs-on: [self-hosted, linux]\n"
                                   "    timeout-minutes: 9\n"
                                   "  gamma:\n"
                                   "    runs-on:\n"
                                   "      - self-hosted\n"
                                   "      - linux\n")
        before = scan_workflow_text(_read(path), path, "ci.yml")
        assert main(["fix", str(tmp_path)]) == 0
        after = scan_workflow_text(_read(path), path, "ci.yml")
        assert [job.name for job in after.jobs] == [job.name for job in before.jobs]

        document = yaml.safe_load(_read(path))
        assert set(document["jobs"]) == {"alpha", "beta", "gamma"}
        assert document["jobs"]["alpha"]["timeout-minutes"] == 300
        assert document["jobs"]["beta"]["timeout-minutes"] == 9
        assert document["jobs"]["gamma"]["timeout-minutes"] == 120

    def test_only_whole_lines_are_added(self, tmp_path):
        original = _simple("    runs-on: [self-hosted, linux]\n"
                           "    steps:\n      - run: echo hi\n")
        path = _workflow(tmp_path, original)
        main(["fix", str(tmp_path)])
        original_lines = original.split("\n")
        new_lines = [line for line in _read(path).split("\n") if line not in original_lines]
        assert new_lines == ["    timeout-minutes: 120"]


class TestTabIndentationRejected:
    """Tab indentation exits 2 without guessing the width and without touching the file."""

    def test_tab_indentation_exits_2(self, tmp_path, capsys):
        path = _workflow(tmp_path, "jobs:\n  test:\n\truns-on: [self-hosted, linux]\n")
        original = _read(path)
        assert main(["fix", str(tmp_path)]) == 2
        assert "tab indentation" in capsys.readouterr().err
        assert _read(path) == original

    def test_audit_tab_exits_2(self, tmp_path):
        _workflow(tmp_path, "jobs:\n  test:\n\truns-on: [self-hosted, linux]\n")
        assert main(["audit", str(tmp_path)]) == 2

    def test_scan_raises(self, tmp_path):
        with pytest.raises(WorkflowParseError):
            scan_workflow_text("jobs:\n\ttest:\n", tmp_path / "x.yml", "x.yml")


# ── 范围：自托管 / hosted / caller ───────────────────────────────────────────


class TestScopeSelfHostedByDefault:
    """(5, revised) A hosted job is not fixed by default; ``--include-hosted`` opts in."""

    WORKFLOW = ("jobs:\n"
                "  hosted-job:\n"
                "    runs-on: macos-latest\n"
                "    steps:\n"
                "      - run: cargo test\n"
                "  self-job:\n"
                "    runs-on: [self-hosted, linux, x64, local]\n"
                "    steps:\n"
                "      - run: cargo test\n")

    def test_hosted_not_fixed_by_default(self, tmp_path):
        path = _workflow(tmp_path, self.WORKFLOW)
        assert main(["fix", str(tmp_path)]) == 0
        text = _read(path)
        assert text.count("timeout-minutes:") == 1
        assert text.index("timeout-minutes:") > text.index("self-job")

    def test_hosted_fixed_with_include_hosted(self, tmp_path):
        path = _workflow(tmp_path, self.WORKFLOW)
        assert main(["fix", str(tmp_path), "--include-hosted"]) == 0
        assert _read(path).count("timeout-minutes:") == 2

    def test_audit_reports_both_counts(self, tmp_path, capsys):
        _workflow(tmp_path, self.WORKFLOW)
        assert main(["audit", str(tmp_path)]) == 1
        out = capsys.readouterr().out
        assert "1/1 jobs missing timeout-minutes (self-hosted: 1/1, hosted: 1/1)" in out

    def test_audit_json_has_self_hosted_flag(self, tmp_path, capsys):
        import json

        _workflow(tmp_path, self.WORKFLOW)
        assert main(["audit", str(tmp_path), "--json"]) == 1
        records = json.loads(capsys.readouterr().out)
        by_name = {record["job"]: record for record in records}
        assert by_name["hosted-job"]["self_hosted"] is False
        assert by_name["self-job"]["self_hosted"] is True
        assert by_name["hosted-job"]["runs_on"] == ["macos-latest"]
        assert by_name["self-job"]["runs_on"] == ["self-hosted", "linux", "x64", "local"]
        assert by_name["self-job"]["timeout_minutes"] is None
        assert by_name["self-job"]["suggested"] == 300


class TestMatrixAndStepTimeout:
    """A ``strategy.matrix`` does not affect insertion; a step-level timeout is not a job-level one."""

    def test_matrix_job_is_handled(self, tmp_path):
        path = _workflow(tmp_path, "jobs:\n"
                                   "  test:\n"
                                   "    strategy:\n"
                                   "      matrix:\n"
                                   "        os: [self-hosted]\n"
                                   "    runs-on: [self-hosted, linux]\n"
                                   "    steps:\n"
                                   "      - run: cargo test\n")
        assert main(["fix", str(tmp_path)]) == 0
        assert "    timeout-minutes: 300\n" in _read(path)

    def test_step_level_timeout_does_not_count_as_job_level(self, tmp_path):
        path = _workflow(tmp_path, "jobs:\n"
                                   "  test:\n"
                                   "    runs-on: [self-hosted, linux]\n"
                                   "    steps:\n"
                                   "      - name: slow\n"
                                   "        timeout-minutes: 5\n"
                                   "        run: cargo test\n")
        assert main(["fix", str(tmp_path)]) == 0
        text = _read(path)
        assert "    timeout-minutes: 300\n" in text   # 新增的 job 级
        assert "        timeout-minutes: 5\n" in text  # 原有步骤级原样保留
        assert text.count("timeout-minutes:") == 2

    def test_job_with_job_level_timeout_is_untouched_even_with_step_timeout(self, tmp_path):
        original = ("jobs:\n"
                    "  test:\n"
                    "    runs-on: [self-hosted, linux]\n"
                    "    timeout-minutes: 30\n"
                    "    steps:\n"
                    "      - timeout-minutes: 5\n"
                    "        run: echo hi\n")
        path = _workflow(tmp_path, original)
        assert main(["fix", str(tmp_path)]) == 0
        assert _read(path) == original


# ── 策略覆盖与退出码 ─────────────────────────────────────────────────────────


class TestPolicyOverride:
    """``--policy-override JOB=MINUTES`` forces the value of a same-named job."""

    def test_override_wins(self, tmp_path):
        path = _workflow(tmp_path, _simple("    runs-on: [self-hosted, linux]\n"
                                           "    steps:\n      - run: cargo test\n"))
        assert main(["fix", str(tmp_path), "--policy-override", "test=42"]) == 0
        assert "    timeout-minutes: 42\n" in _read(path)

    def test_override_matches_case_insensitively(self, tmp_path):
        path = _workflow(tmp_path, "jobs:\n  Test:\n    runs-on: [self-hosted, linux]\n")
        main(["fix", str(tmp_path), "--policy-override", "test=42"])
        assert "    timeout-minutes: 42\n" in _read(path)

    def test_override_only_affects_named_job(self, tmp_path):
        path = _workflow(tmp_path, "jobs:\n"
                                   "  test:\n"
                                   "    runs-on: [self-hosted, linux]\n"
                                   "  other:\n"
                                   "    runs-on: [self-hosted, linux]\n")
        main(["fix", str(tmp_path), "--policy-override", "test=42"])
        text = _read(path)
        assert "    timeout-minutes: 42\n" in text
        assert "    timeout-minutes: 120\n" in text

    def test_override_reported_by_audit(self, tmp_path, capsys):
        import json

        _workflow(tmp_path, _simple("    runs-on: [self-hosted, linux]\n"))
        main(["audit", str(tmp_path), "--json", "--policy-override", "test=42"])
        records = json.loads(capsys.readouterr().out)
        assert records[0]["suggested"] == 42

    def test_malformed_override_exits_2(self, tmp_path):
        _workflow(tmp_path, _simple("    runs-on: [self-hosted, linux]\n"))
        with pytest.raises(SystemExit) as excinfo:
            main(["fix", str(tmp_path), "--policy-override", "test"])
        assert excinfo.value.code == 2
        with pytest.raises(SystemExit):
            main(["fix", str(tmp_path), "--policy-override", "test=abc"])


class TestExitCodes:
    """Exit codes: 0 success / 1 missing timeout (audit only) / 2 usage or parse error."""

    def test_audit_exit_0_when_all_have_timeout(self, tmp_path, capsys):
        _workflow(tmp_path, _simple("    runs-on: [self-hosted, linux]\n"
                                    "    timeout-minutes: 30\n"))
        assert main(["audit", str(tmp_path)]) == 0
        assert "0/1 jobs missing timeout-minutes" in capsys.readouterr().out

    def test_audit_exit_1_when_missing(self, tmp_path):
        _workflow(tmp_path, _simple("    runs-on: [self-hosted, linux]\n"))
        assert main(["audit", str(tmp_path)]) == 1

    def test_fix_exit_0_even_when_missing(self, tmp_path):
        _workflow(tmp_path, _simple("    runs-on: [self-hosted, linux]\n"))
        assert main(["fix", str(tmp_path)]) == 0

    def test_missing_path_exits_2(self, tmp_path, capsys):
        assert main(["audit", str(tmp_path / "nope")]) == 2
        assert "no such file or directory" in capsys.readouterr().err

    def test_no_subcommand_exits_2(self, capsys):
        assert main([]) == 2
        assert "usage" in capsys.readouterr().out.lower()

    def test_paths_default_to_cwd(self, tmp_path, monkeypatch):
        _workflow(tmp_path, _simple("    runs-on: [self-hosted, linux]\n"))
        monkeypatch.chdir(tmp_path)
        assert main(["fix"]) == 0
        assert "timeout-minutes: 120" in _read(tmp_path / ".github/workflows/ci.yml")


class TestAuditListing:
    """``audit`` must list every job's ``runs-on`` and its timeout state."""

    WORKFLOW = ("jobs:\n"
                "  check:\n"
                "    runs-on: [self-hosted, linux, x64, local]\n"
                "    steps:\n"
                "      - run: cargo test\n"
                "  e2e:\n"
                "    runs-on: [self-hosted, linux]\n"
                "    timeout-minutes: 60\n"
                "  windows:\n"
                "    runs-on: windows-latest\n"
                "  caller:\n"
                "    uses: celestia-island/celestia-devtools/.github/workflows/p0-gate.yml@master\n")

    def test_shows_runs_on_value(self, tmp_path, capsys):
        _workflow(tmp_path, self.WORKFLOW)
        main(["audit", str(tmp_path)])
        out = capsys.readouterr().out
        assert "self-hosted, linux, x64, local" in out
        assert "windows-latest" in out

    def test_shows_existing_timeout(self, tmp_path, capsys):
        _workflow(tmp_path, self.WORKFLOW)
        main(["audit", str(tmp_path)])
        out = capsys.readouterr().out
        assert "ok (timeout-minutes: 60)" in out
        assert "timeout-minutes" in out  # 逐 job 行也带字面量，便于 grep

    def test_shows_uses_target_for_caller(self, tmp_path, capsys):
        _workflow(tmp_path, self.WORKFLOW)
        main(["audit", str(tmp_path)])
        out = capsys.readouterr().out
        assert "uses: celestia-island/celestia-devtools/.github/workflows/p0-gate.yml@master" in out

    def test_marks_out_of_scope_jobs(self, tmp_path, capsys):
        _workflow(tmp_path, self.WORKFLOW)
        main(["audit", str(tmp_path)])
        out = capsys.readouterr().out
        assert "  - windows" in out      # hosted 默认不在范围内
        assert "  - caller" in out       # caller 默认不在范围内
        assert "  - check" not in out    # self-hosted 在范围内，不应带 - 标记
        assert "(`-` marks jobs outside the current scope)" in out

    def test_all_jobs_are_listed_even_out_of_scope(self, tmp_path, capsys):
        _workflow(tmp_path, self.WORKFLOW)
        assert main(["audit", str(tmp_path), "--json"]) == 1
        import json

        records = json.loads(capsys.readouterr().out)
        assert {record["job"] for record in records} == {"check", "e2e", "windows", "caller"}


class TestJoblessWorkflowIsWarned:
    """A workflow that scans to 0 jobs warns but is not an error (the org really has one collapsed onto a single line)."""

    def test_warning_on_stderr_without_error(self, tmp_path, capsys):
        _workflow(tmp_path, "name: broken\non: push\n", filename="broken.yml")
        assert main(["audit", str(tmp_path)]) == 0
        captured = capsys.readouterr()
        assert "no top-level `jobs:` found" in captured.err
        assert "0/0 jobs missing timeout-minutes" in captured.out

    def test_fix_does_not_fail_on_jobless_workflow(self, tmp_path, capsys):
        _workflow(tmp_path, "name: broken\n", filename="broken.yml")
        assert main(["fix", str(tmp_path)]) == 0
        assert "inserted 0 line(s) in 0 file(s)" in capsys.readouterr().out

    def test_empty_jobs_block_is_reported_as_zero(self, tmp_path, capsys):
        _workflow(tmp_path, "jobs:\n")
        assert main(["audit", str(tmp_path)]) == 0
        assert "0/0 jobs missing timeout-minutes" in capsys.readouterr().out


class TestRunsOnExpression:
    """A ``runs-on`` that is a GitHub expression cannot be classified, so it is treated as not self-hosted."""

    def test_matrix_expression_job_is_out_of_scope(self, tmp_path, capsys):
        path = _workflow(tmp_path, "jobs:\n"
                                   "  clippy-official:\n"
                                   "    runs-on: ${{ matrix.os }}\n"
                                   "    strategy:\n"
                                   "      matrix:\n"
                                   "        os: [windows-latest, macos-latest]\n")
        assert main(["fix", str(tmp_path)]) == 0
        assert "timeout-minutes" not in _read(path)

    def test_matrix_expression_job_handled_with_include_hosted(self, tmp_path):
        path = _workflow(tmp_path, "jobs:\n"
                                   "  clippy-official:\n"
                                   "    runs-on: ${{ matrix.os }}\n"
                                   "    strategy:\n"
                                   "      matrix:\n"
                                   "        os: [windows-latest, macos-latest]\n")
        assert main(["fix", str(tmp_path), "--include-hosted"]) == 0
        assert "    timeout-minutes: 120\n" in _read(path)


class TestInlineComments:
    """A trailing comment on a key line does not affect parsing; a ``#`` without preceding whitespace belongs to the value."""

    def test_trailing_comment_on_runs_on(self, tmp_path):
        path = _workflow(tmp_path, "jobs:\n"
                                   "  test:\n"
                                   "    runs-on: [self-hosted, linux]  # farm labels\n"
                                   "    steps:\n"
                                   "      - run: cargo test\n")
        assert main(["fix", str(tmp_path)]) == 0
        lines = _read(path).split("\n")
        index = lines.index("    runs-on: [self-hosted, linux]  # farm labels")
        assert lines[index + 1] == "    timeout-minutes: 300"

    def test_comment_on_job_key(self, tmp_path):
        path = _workflow(tmp_path, "jobs:\n"
                                   "  test:  # the only job\n"
                                   "    runs-on: [self-hosted, linux]\n")
        assert main(["fix", str(tmp_path)]) == 0
        assert "    timeout-minutes: 120\n" in _read(path)

    def test_runs_on_with_only_a_comment_value_is_multiline(self, tmp_path):
        path = _workflow(tmp_path, "jobs:\n"
                                   "  test:\n"
                                   "    runs-on:  # see below\n"
                                   "      - self-hosted\n"
                                   "      - linux\n")
        assert main(["fix", str(tmp_path)]) == 0
        lines = _read(path).split("\n")
        assert lines.index("    timeout-minutes: 120") == lines.index("      - linux") + 1

    def test_trailing_comment_on_timeout_minutes(self, tmp_path):
        original = ("jobs:\n"
                    "  test:\n"
                    "    runs-on: [self-hosted, linux]\n"
                    "    timeout-minutes: 30  # tuned\n")
        path = _workflow(tmp_path, original)
        assert main(["fix", str(tmp_path)]) == 0
        assert _read(path) == original

    def test_trailing_comment_is_stripped_from_reported_values(self, tmp_path, capsys):
        """M24 coverage hole: the comment must really be stripped from the ``--json`` values."""
        import json

        _workflow(tmp_path, "jobs:\n"
                            "  test:\n"
                            "    runs-on: [self-hosted, linux]  # farm labels\n"
                            "    timeout-minutes: 30  # tuned\n")
        main(["audit", str(tmp_path), "--json"])
        record = json.loads(capsys.readouterr().out)[0]
        assert record["runs_on"] == ["self-hosted", "linux"]
        assert record["timeout_minutes"] == 30

    def test_hash_without_whitespace_is_not_a_comment(self, tmp_path, capsys):
        """M25 coverage hole: the ``#`` in ``uses: x#y`` belongs to the value itself."""
        import json

        target = ("celestia-island/celestia-devtools/"
                  ".github/workflows/verify-versions.yml#v1")
        _workflow(tmp_path, f"jobs:\n  v:\n    uses: {target}\n")
        main(["audit", str(tmp_path), "--json"])
        captured = capsys.readouterr()
        record = json.loads(captured.out)[0]
        assert record["runs_on"] is None
        assert record["uses"] == target          # #v1 未被当成注释砍掉
        assert record["caller"] is True

    def test_uses_target_is_reported_in_human_output(self, tmp_path, capsys):
        target = ("celestia-island/celestia-devtools/"
                  ".github/workflows/verify-versions.yml@master")
        _workflow(tmp_path, f"jobs:\n  v:\n    uses: {target}\n")
        main(["audit", str(tmp_path)])
        assert f"uses: {target}" in capsys.readouterr().out


class TestBomBeforeJobsKey:
    """D7 regression: a UTF-8 BOM before ``jobs:`` must still be recognised."""

    def test_bom_before_jobs_key(self, tmp_path):
        path = _workflow(tmp_path, "\ufeffjobs:\n  test:\n    runs-on: [self-hosted, linux]\n")
        assert main(["fix", str(tmp_path)]) == 0
        text = _read(path)
        assert "\ufeffjobs:" in text          # BOM 原样保留
        assert "    timeout-minutes: 120\n" in text

    def test_bom_before_name_key_still_works(self, tmp_path):
        path = _workflow(tmp_path, "\ufeffname: CI\njobs:\n  test:\n"
                                   "    runs-on: [self-hosted, linux]\n")
        assert main(["fix", str(tmp_path)]) == 0
        assert "    timeout-minutes: 120\n" in _read(path)


class TestCrlfWriteIsByteExact:
    """M18 coverage hole: writing back must preserve CRLF byte-exactly, not rely on the platform happening to use LF."""

    def test_written_bytes_keep_crlf(self, tmp_path):
        path = _workflow(tmp_path, "jobs:\r\n  test:\r\n    runs-on: [self-hosted, linux]\r\n")
        assert main(["fix", str(tmp_path)]) == 0
        raw = path.read_bytes()
        assert b"    timeout-minutes: 120\r\n" in raw
        assert b"\n" not in raw.replace(b"\r\n", b"")  # 没有任何裸 LF
        assert yaml.safe_load(raw.decode("utf-8"))["jobs"]["test"]["timeout-minutes"] == 120


class TestMultilineFlowCollection:
    """D1 regression: with a flow collection spanning lines the insertion must follow the whole collection.

    Independent verification measured that an earlier revision inserted ``timeout-minutes``
    *inside* the collection, rewriting the runner label set into
    ``['self-hosted', {'timeout-minutes': '120 linux'}]`` — silently destroying ``runs-on``
    while leaving no real job-level timeout.
    """

    def test_multiline_flow_list(self, tmp_path):
        path = _workflow(tmp_path, "jobs:\n"
                                   "  test:\n"
                                   "    runs-on: [self-hosted,\n"
                                   "              linux]\n"
                                   "    steps:\n"
                                   "      - run: echo hi\n")
        assert main(["fix", str(tmp_path)]) == 0
        lines = _read(path).split("\n")
        assert lines.index("    timeout-minutes: 120") == lines.index("              linux]") + 1
        document = yaml.safe_load(_read(path))
        assert document["jobs"]["test"]["runs-on"] == ["self-hosted", "linux"]
        assert document["jobs"]["test"]["timeout-minutes"] == 120

    def test_multiline_flow_list_closing_bracket_on_own_line(self, tmp_path):
        path = _workflow(tmp_path, "jobs:\n"
                                   "  test:\n"
                                   "    runs-on: [self-hosted,\n"
                                   "              linux\n"
                                   "             ]\n")
        assert main(["fix", str(tmp_path)]) == 0
        assert yaml.safe_load(_read(path))["jobs"]["test"]["runs-on"] == [
            "self-hosted", "linux",
        ]

    def test_multiline_flow_list_with_comment_inside(self, tmp_path):
        path = _workflow(tmp_path, "jobs:\n"
                                   "  test:\n"
                                   "    runs-on: [self-hosted,\n"
                                   "              # the farm pool\n"
                                   "              linux]\n")
        assert main(["fix", str(tmp_path)]) == 0
        assert yaml.safe_load(_read(path))["jobs"]["test"]["runs-on"] == [
            "self-hosted", "linux",
        ]

    def test_multiline_flow_uses_with_include_callers(self, tmp_path):
        path = _workflow(tmp_path, "jobs:\n"
                                   "  verify:\n"
                                   "    uses: celestia-island/celestia-devtools/"
                                   ".github/workflows/verify-versions.yml@master\n"
                                   "    with: {a: 1}\n")
        assert main(["fix", str(tmp_path), "--include-callers"]) == 0
        lines = _read(path).split("\n")
        assert lines.index("    timeout-minutes: 60") == lines.index(
            "    uses: celestia-island/celestia-devtools/.github/workflows/verify-versions.yml@master"
        ) + 1

    def test_multiline_flow_hosted(self, tmp_path):
        path = _workflow(tmp_path, "jobs:\n"
                                   "  win:\n"
                                   "    runs-on: [windows-latest,\n"
                                   "              x64]\n")
        assert main(["fix", str(tmp_path), "--include-hosted"]) == 0
        document = yaml.safe_load(_read(path))
        assert document["jobs"]["win"]["runs-on"] == ["windows-latest", "x64"]
        assert document["jobs"]["win"]["timeout-minutes"] == 120


class TestBlockScalarValue:
    """D2 regression: block-scalar forms such as ``runs-on: >`` / ``|-``."""

    def test_folded_scalar_runs_on(self, tmp_path):
        path = _workflow(tmp_path, "jobs:\n"
                                   "  test:\n"
                                   "    runs-on: >\n"
                                   "      self-hosted\n"
                                   "    steps:\n"
                                   "      - run: echo hi\n")
        # 值里含 self-hosted，应被识别为自托管并走默认范围
        assert main(["fix", str(tmp_path)]) == 0
        lines = _read(path).split("\n")
        assert lines.index("    timeout-minutes: 120") == lines.index("      self-hosted") + 1
        document = yaml.safe_load(_read(path))
        assert document["jobs"]["test"]["runs-on"] == "self-hosted\n"
        assert document["jobs"]["test"]["timeout-minutes"] == 120

    @pytest.mark.parametrize("indicator", ["|", "|-", "|+", ">-"])
    def test_literal_scalar_indicators(self, tmp_path, indicator):
        path = _workflow(tmp_path, "jobs:\n"
                                   "  test:\n"
                                   f"    runs-on: {indicator}\n"
                                   "      self-hosted\n")
        assert main(["fix", str(tmp_path)]) == 0
        text = _read(path)
        assert "    timeout-minutes: 120\n" in text
        # 插入行不得落在指示符与正文之间
        assert text.index("runs-on: " + indicator) < text.index("self-hosted") < text.index(
            "timeout-minutes"
        )


class TestSymlinkEscape:
    """D3 regression: a symlink escaping ``.github/workflows`` must be refused, not written through."""

    def test_symlink_escaping_workflows_is_refused(self, tmp_path, capsys):
        outside = tmp_path / "outside.yml"
        outside.write_text("jobs:\n  test:\n    runs-on: [self-hosted, linux]\n",
                           encoding="utf-8")
        directory = tmp_path / ".github" / "workflows"
        directory.mkdir(parents=True)
        (directory / "link.yml").symlink_to(outside)

        assert main(["fix", str(tmp_path)]) == 2
        assert "symlink escapes" in capsys.readouterr().err
        assert outside.read_text(encoding="utf-8") == (
            "jobs:\n  test:\n    runs-on: [self-hosted, linux]\n"
        )

    def test_symlink_inside_workflows_is_allowed_and_counted_once(self, tmp_path, capsys):
        directory = tmp_path / ".github" / "workflows"
        directory.mkdir(parents=True)
        real = directory / "real.yml"
        real.write_text("jobs:\n  test:\n    runs-on: [self-hosted, linux]\n", encoding="utf-8")
        (directory / "alias.yml").symlink_to(real)

        # 目录内互链：不算越界；且按真实落盘路径去重，只算一个文件
        assert main(["fix", str(tmp_path)]) == 0
        assert "inserted 1 line(s) in 1 file(s)" in capsys.readouterr().out
        assert real.read_text(encoding="utf-8").count("timeout-minutes") == 1


class TestSymlinkedWorkflowsDirectory:
    """F1 regression: the ``.github/workflows`` directory itself must not escape the root."""

    def _fixture(self, tmp_path):
        """``.github/workflows`` 变成指向仓库外目录的软链。"""
        outside = tmp_path / "outside" / "workflows"
        outside.mkdir(parents=True)
        target = outside / "ci.yml"
        target.write_text("jobs:\n  test:\n    runs-on: [self-hosted, linux]\n", encoding="utf-8")

        repo = tmp_path / "repo"
        (repo / ".github").mkdir(parents=True)
        (repo / ".github" / "workflows").symlink_to(outside)
        return repo, target

    def test_fix_refuses_and_writes_nothing_outside(self, tmp_path, capsys):
        repo, target = self._fixture(tmp_path)
        original = target.read_text(encoding="utf-8")

        assert main(["fix", str(repo)]) == 2
        assert "outside" in capsys.readouterr().err
        assert target.read_text(encoding="utf-8") == original  # 外部零改动

    def test_audit_also_refuses(self, tmp_path):
        repo, target = self._fixture(tmp_path)
        original = target.read_text(encoding="utf-8")
        assert main(["audit", str(repo)]) == 2
        assert target.read_text(encoding="utf-8") == original

    def test_dry_run_also_refuses(self, tmp_path):
        repo, target = self._fixture(tmp_path)
        original = target.read_text(encoding="utf-8")
        assert main(["fix", str(repo), "--dry-run"]) == 2
        assert target.read_text(encoding="utf-8") == original

    def test_symlinked_workflows_inside_root_is_allowed(self, tmp_path):
        """目录软链只要仍在仓库根之内就放行。"""
        repo = tmp_path / "repo"
        (repo / ".github").mkdir(parents=True)
        real_dir = repo / "wf"
        real_dir.mkdir()
        (real_dir / "ci.yml").write_text(
            "jobs:\n  test:\n    runs-on: [self-hosted, linux]\n", encoding="utf-8"
        )
        (repo / ".github" / "workflows").symlink_to(real_dir)

        assert main(["fix", str(repo)]) == 0
        assert "timeout-minutes: 120" in (real_dir / "ci.yml").read_text(encoding="utf-8")


class TestTabInsideBlockScalar:
    """D4 regression: a tab inside a block-scalar body is content, not indentation, and must not stall the batch."""

    WORKFLOW = ("jobs:\n"
                "  test:\n"
                "    runs-on: [self-hosted, linux]\n"
                "    steps:\n"
                "      - run: |\n"
                "          if true; then\n"
                "          \techo hi\n"
                "          fi\n")

    def test_tab_as_block_scalar_content_is_accepted(self, tmp_path):
        path = _workflow(tmp_path, self.WORKFLOW)
        assert main(["fix", str(tmp_path)]) == 0
        text = _read(path)
        assert "    timeout-minutes: 120\n" in text
        assert "\techo hi" in text  # Tab 内容原样保留

    def test_tab_content_does_not_block_other_repos(self, tmp_path):
        # 第二个 PATH 有 Tab 内容也不应让第一个 PATH 失败
        first = tmp_path / "a"
        second = tmp_path / "b"
        _workflow(first, "jobs:\n  test:\n    runs-on: [self-hosted, linux]\n")
        _workflow(second, self.WORKFLOW)
        assert main(["fix", str(first), str(second)]) == 0

    def test_real_indentation_tab_still_rejected(self, tmp_path, capsys):
        path = _workflow(tmp_path, "jobs:\n  test:\n\truns-on: [self-hosted, linux]\n")
        original = _read(path)
        assert main(["fix", str(tmp_path)]) == 2
        assert "tab indentation" in capsys.readouterr().err
        assert _read(path) == original

    def test_timeout_like_text_inside_block_scalar_is_ignored(self, tmp_path):
        path = _workflow(tmp_path, "jobs:\n"
                                   "  test:\n"
                                   "    runs-on: [self-hosted, linux]\n"
                                   "    steps:\n"
                                   "      - run: |\n"
                                   "          echo 'timeout-minutes: 999'\n"
                                   "          echo 'runs-on: [self-hosted]'\n")
        assert main(["fix", str(tmp_path)]) == 0
        text = _read(path)
        assert text.count("timeout-minutes") == 2  # 原有 echo 文本 + 新插入的一行
        assert "    timeout-minutes: 120\n" in text


class TestQuotedJobNameIdentity:
    """M22 coverage hole: quotes must be stripped from the job name (affects ``--policy-override`` and ``--json``)."""

    def test_quoted_name_is_stripped_in_json(self, tmp_path, capsys):
        import json

        _workflow(tmp_path, 'jobs:\n  "test":\n    runs-on: [self-hosted, linux]\n')
        main(["audit", str(tmp_path), "--json"])
        records = json.loads(capsys.readouterr().out)
        assert records[0]["job"] == "test"

    @pytest.mark.parametrize("quoted", ['"test"', "'test'"])
    def test_override_matches_quoted_job_name(self, tmp_path, quoted):
        path = _workflow(tmp_path, f"jobs:\n  {quoted}:\n    runs-on: [self-hosted, linux]\n")
        assert main(["fix", str(tmp_path), "--policy-override", "test=42"]) == 0
        assert "    timeout-minutes: 42\n" in _read(path)


class TestEntryPointRegistered:
    """M23 coverage hole: the entry point must really be registered in ``pyproject.toml``."""

    def test_console_script_declared(self):
        from pathlib import Path as _Path

        # 用文本断言而不是 tomllib：本包声明支持 3.9，而 tomllib 是 3.11+ 才有的。
        pyproject = _Path(__file__).resolve().parent.parent / "pyproject.toml"
        text = pyproject.read_text(encoding="utf-8")
        assert 'celestia-job-timeouts = "celestia_devtools.ci.job_timeouts:main"' in text
        # 且必须在 [project.scripts] 段内
        scripts = text.split("[project.scripts]", 1)[1].split("\n[", 1)[0]
        assert "celestia-job-timeouts" in scripts

    def test_entry_point_target_is_importable_and_callable(self):
        from celestia_devtools.ci.job_timeouts import main as entry

        assert callable(entry)


class TestNonWorkflowFilesUntouched:
    """No file outside ``.github/workflows`` is modified."""

    def test_other_files_untouched(self, tmp_path):
        _workflow(tmp_path, _simple("    runs-on: [self-hosted, linux]\n"))
        other = tmp_path / ".github" / "dependabot.yml"
        other.write_text("version: 2\n", encoding="utf-8")
        readme = tmp_path / "README.md"
        readme.write_text("# hi\n", encoding="utf-8")
        assert main(["fix", str(tmp_path)]) == 0
        assert other.read_text(encoding="utf-8") == "version: 2\n"
        assert readme.read_text(encoding="utf-8") == "# hi\n"

    def test_fix_only_touches_changed_files(self, tmp_path):
        untouched = _workflow(tmp_path, _simple("    runs-on: [self-hosted, linux]\n"
                                               "    timeout-minutes: 30\n"),
                              filename="ok.yml")
        before = os.stat(untouched).st_mtime_ns
        main(["fix", str(tmp_path)])
        assert os.stat(untouched).st_mtime_ns == before


class TestOnKeyNotMisparsed:
    """A top-level ``on:`` must not affect the ``jobs`` detection."""

    def test_on_with_nested_jobs_like_keys(self, tmp_path):
        path = _workflow(tmp_path, "name: CI\n"
                                   "on:\n"
                                   "  push:\n"
                                   "    branches: [master]\n"
                                   "  workflow_dispatch:\n"
                                   "jobs:\n"
                                   "  test:\n"
                                   "    runs-on: [self-hosted, linux]\n")
        assert main(["fix", str(tmp_path)]) == 0
        lines = _read(path).split("\n")
        assert lines.index("    timeout-minutes: 120") == lines.index(
            "    runs-on: [self-hosted, linux]"
        ) + 1

    def test_run_block_containing_jobs_text(self, tmp_path):
        path = _workflow(tmp_path, "jobs:\n"
                                   "  test:\n"
                                   "    runs-on: [self-hosted, linux]\n"
                                   "    steps:\n"
                                   "      - run: |\n"
                                   "          echo 'jobs:'\n"
                                   "          echo '  fake:'\n")
        assert main(["fix", str(tmp_path)]) == 0
        assert _read(path).count("timeout-minutes:") == 1

    def test_comment_between_job_key_and_runs_on(self, tmp_path):
        path = _workflow(tmp_path, "jobs:\n"
                                   "  test:\n"
                                   "  # a comment\n"
                                   "    runs-on: [self-hosted, linux]\n")
        assert main(["fix", str(tmp_path)]) == 0
        lines = _read(path).split("\n")
        assert lines.index("    timeout-minutes: 120") == lines.index(
            "    runs-on: [self-hosted, linux]"
        ) + 1

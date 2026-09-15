#!/usr/bin/env python3
"""Tests for ``celestia_devtools.ci.workflow_audit`` (the ``celestia-ci-audit`` CLI).

Covers the four rules and the false-positive guards that keep the gate usable:

1. the 2026-09-15 accident shape — a reusable-workflow caller job carrying
   ``timeout-minutes`` — is reported, and the offending key is named;
2. a caller job carrying **only** the nine legal keys is clean;
3. a ``workflow_call`` callee's self-hosted job without ``timeout-minutes`` is reported,
   with one is not (in all three ``on`` shapes);
4. a plain self-hosted job without ``timeout-minutes`` is reported;
5. a workflow flattened onto a single line (the real ``sysl`` shape) is **in the findings**
   rather than silently skipped;
6. the ``--json`` document and the exit codes;
7. mutation style: taking a clean fixture and breaking it (dropping the timeout / adding the
   illegal key back) turns the gate red again.

Plus the guards a red gate depends on: a step-level ``timeout-minutes`` does not satisfy the
job-level rule, ``- uses: actions/checkout`` inside ``steps`` does not make a job a caller,
GitHub-hosted jobs are out of scope, the ``--allow`` list suppresses loudly, and every
finding carries a repository-relative ``path:line``.
"""

import json

import pytest

from celestia_devtools.ci.workflow_audit import (
    CALLER_LEGAL_KEYS,
    RULE_CALLEE_MISSING_TIMEOUT,
    RULE_INVALID_CALLER_KEY,
    RULE_SELF_HOSTED_MISSING_TIMEOUT,
    RULE_YAML_PARSE_ERROR,
    Finding,
    audit_text,
    has_failure,
    main,
)

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
    """Read with ``newline=""`` so universal newlines cannot hide a rewrite."""
    with path.open("r", encoding="utf-8", newline="") as handle:
        return handle.read()


def _replace(path, old, new):
    """Mutate a fixture on disk: the "make the fix wrong" primitive."""
    text = _read(path)
    assert old in text, f"mutation anchor not found: {old!r}"
    with path.open("w", encoding="utf-8", newline="") as handle:
        handle.write(text.replace(old, new, 1))
    return path


def _findings(capsys):
    """Parse the ``--json`` report already printed to stdout."""
    return json.loads(capsys.readouterr().out)


def _run(argv, capsys):
    """Run the CLI and return ``(exit_code, stdout, stderr)``, consuming the capture."""
    code = main(argv)
    captured = capsys.readouterr()
    return code, captured.out, captured.err


def _rules(findings):
    return [finding["rule"] for finding in findings]


#: The accident: a caller job plus the key GitHub rejects. This is a faithful
#: reduction of ``shittim-chest/.github/workflows/ci.yml`` (PR #870 / #872).
ACCIDENT = (
    "name: CI\n"
    "on:\n"
    "  push:\n"
    "    branches: [master]\n"
    "jobs:\n"
    "  verify-versions:\n"
    "    uses: celestia-island/celestia-devtools/.github/workflows/verify-versions.yml@master\n"
    "    timeout-minutes: 90\n"
)

#: A caller job carrying **only** the nine legal keys (every one of them, on purpose).
LEGAL_CALLER = (
    "name: CI\n"
    "on: [push, workflow_dispatch]\n"
    "jobs:\n"
    "  lint-commits:\n"
    "    name: Lint commit messages\n"
    "    uses: celestia-island/celestia-devtools/.github/workflows/commit-msg-lint.yml@master\n"
    "    with:\n"
    "      fetch-depth: 0\n"
    "    secrets: inherit\n"
    "    strategy:\n"
    "      fail-fast: false\n"
    "    needs: [build]\n"
    "    if: github.event_name == 'pull_request'\n"
    "    concurrency:\n"
    "      group: lint-${{ github.ref }}\n"
    "    permissions:\n"
    "      contents: read\n"
)

#: A reusable-workflow callee whose self-hosted job has no upper bound.
CALLEE_NO_TIMEOUT = (
    "name: Verify versions\n"
    "on:\n"
    "  workflow_call:\n"
    "    inputs:\n"
    "      ref:\n"
    "        type: string\n"
    "jobs:\n"
    "  verify-versions:\n"
    "    runs-on: [self-hosted, linux, x64, local]\n"
    "    steps:\n"
    "      - uses: actions/checkout@v7\n"
    "      - run: cargo run -p verify-versions\n"
)

#: The same callee, fixed: the upper bound can only live here (a caller cannot set one).
CALLEE_WITH_TIMEOUT = CALLEE_NO_TIMEOUT.replace(
    "    runs-on: [self-hosted, linux, x64, local]\n",
    "    runs-on: [self-hosted, linux, x64, local]\n    timeout-minutes: 60\n",
)

#: A plain (non-``workflow_call``) workflow with a self-hosted job.
PLAIN_NO_TIMEOUT = (
    "name: CI\n"
    "on:\n"
    "  push:\n"
    "    branches: [master]\n"
    "jobs:\n"
    "  build:\n"
    "    runs-on: [self-hosted, linux, x64, local]\n"
    "    steps:\n"
    "      - run: cargo test --workspace\n"
)

#: The real ``sysl/.github/workflows/validate.yml`` shape: the whole file on one line, so
#: PyYAML stops at the second ``key: value`` and every parse-then-check auditor is blind to it.
ONE_LINE_INVALID = (
    "name: Validate on: push: branches: [master, dev] pull_request: branches: [master, dev] "
    "jobs: validate: runs-on: ubuntu-latest steps: - uses: actions/checkout@v4\n"
)


# ── ① 真实事故形态：caller 作业带非法键 ──────────────────────────────────────


class TestInvalidCallerKey:
    """Rule 1: ``uses:`` plus any key outside the nine caller keys."""

    def test_accident_shape_is_reported(self, tmp_path, monkeypatch, capsys):
        _workflow(tmp_path, ACCIDENT)
        monkeypatch.chdir(tmp_path)
        assert main(["--json"]) == 1
        report = _findings(capsys)
        assert _rules(report["findings"]) == [RULE_INVALID_CALLER_KEY]

    def test_reported_line_points_at_the_offending_key(self, tmp_path, monkeypatch, capsys):
        _workflow(tmp_path, ACCIDENT)
        monkeypatch.chdir(tmp_path)
        main(["--json"])
        finding = _findings(capsys)["findings"][0]
        assert finding["line"] == 8  # ``timeout-minutes: 90``
        assert finding["path"] == ".github/workflows/ci.yml"

    def test_message_names_the_key_and_the_job(self, tmp_path, monkeypatch, capsys):
        _workflow(tmp_path, ACCIDENT)
        monkeypatch.chdir(tmp_path)
        main(["--json"])
        message = _findings(capsys)["findings"][0]["message"]
        assert "'timeout-minutes'" in message  # 点名该键
        assert "'verify-versions'" in message
        assert "name/uses/with/secrets/strategy/needs/if/concurrency/permissions" in message

    def test_severity_is_error_and_recomputable(self, tmp_path, monkeypatch, capsys):
        _workflow(tmp_path, ACCIDENT)
        monkeypatch.chdir(tmp_path)
        main(["--json"])
        finding = _findings(capsys)["findings"][0]
        assert finding["severity"] == "error"
        # 判据可复算：人读输出给出出错行原文。
        assert "timeout-minutes: 90" in _human(tmp_path, monkeypatch, capsys)

    def test_every_illegal_key_is_named_separately(self, tmp_path, monkeypatch, capsys):
        _workflow(
            tmp_path,
            "jobs:\n"
            "  caller:\n"
            "    uses: ./.github/workflows/checks.yml\n"
            "    timeout-minutes: 30\n"
            "    runs-on: [self-hosted, linux]\n"
            "    env:\n"
            "      RUST_LOG: debug\n",
        )
        monkeypatch.chdir(tmp_path)
        assert main(["--json"]) == 1
        report = _findings(capsys)
        assert _rules(report["findings"]) == [RULE_INVALID_CALLER_KEY] * 3
        assert [finding["line"] for finding in report["findings"]] == [4, 5, 6]

    def test_local_reusable_workflow_caller_is_covered(self, tmp_path, monkeypatch, capsys):
        # aoba 的真实形态：`uses: ./.github/workflows/checks.yml` 同样是 caller 作业。
        _workflow(
            tmp_path,
            "jobs:\n"
            "  checks:\n"
            "    uses: ./.github/workflows/checks.yml\n"
            "    timeout-minutes: 330\n",
        )
        monkeypatch.chdir(tmp_path)
        assert main(["--json"]) == 1
        assert _rules(_findings(capsys)["findings"]) == [RULE_INVALID_CALLER_KEY]


def _human(tmp_path, monkeypatch, capsys):
    """Run the human (non-JSON) report and return stdout."""
    monkeypatch.chdir(tmp_path)
    main([])
    return capsys.readouterr().out


# ── ② 合法 caller 键不得命中（防假阳） ──────────────────────────────────────


class TestLegalCallerIsClean:
    """Rule 1 must not fire on the legal caller key set — the whole org is full of these."""

    def test_all_nine_legal_keys_are_clean(self, tmp_path, monkeypatch, capsys):
        _workflow(tmp_path, LEGAL_CALLER)
        monkeypatch.chdir(tmp_path)
        assert main(["--json"]) == 0
        report = _findings(capsys)
        assert report["findings"] == []
        assert report["summary"]["failed"] is False

    def test_legal_key_set_is_the_documented_nine(self):
        # 判据写死在测试里：改动合法集必须先改这里，防止悄悄放宽。
        assert set(CALLER_LEGAL_KEYS) == {
            "name", "uses", "with", "secrets", "strategy",
            "needs", "if", "concurrency", "permissions",
        }

    def test_steps_uses_does_not_make_a_job_a_caller(self, tmp_path, monkeypatch, capsys):
        # 关键防假阳：`- uses: actions/checkout` 是 step 不是 job 级 uses。
        _workflow(
            tmp_path,
            "jobs:\n"
            "  build:\n"
            "    runs-on: [self-hosted, linux, x64, local]\n"
            "    timeout-minutes: 300\n"
            "    steps:\n"
            "      - uses: actions/checkout@v7\n"
            "      - uses: pnpm/action-setup@v6\n",
        )
        monkeypatch.chdir(tmp_path)
        assert main([]) == 0

    def test_hosted_workflow_with_many_uses_is_clean(self, tmp_path, monkeypatch, capsys):
        _workflow(
            tmp_path,
            "jobs:\n"
            "  lint:\n"
            "    runs-on: ubuntu-latest\n"
            "    steps:\n"
            "      - uses: actions/checkout@v7\n"
            "      - uses: actions/setup-python@v6\n",
        )
        monkeypatch.chdir(tmp_path)
        assert main([]) == 0


# ── ③ callee 缺 timeout 命中、有 timeout 不命中 ────────────────────────────


class TestCalleeMissingTimeout:
    """Rule 2: a ``workflow_call`` callee's self-hosted job must carry the upper bound."""

    def test_missing_timeout_is_reported(self, tmp_path, monkeypatch, capsys):
        _workflow(tmp_path, CALLEE_NO_TIMEOUT)
        monkeypatch.chdir(tmp_path)
        assert main(["--json"]) == 1
        finding = _findings(capsys)["findings"][0]
        assert finding["rule"] == RULE_CALLEE_MISSING_TIMEOUT
        assert finding["line"] == 9  # the job's ``runs-on``
        assert "self-hosted, linux, x64, local" in finding["message"]
        assert "caller job cannot set one" in finding["message"]

    def test_with_timeout_is_clean(self, tmp_path, monkeypatch, capsys):
        _workflow(tmp_path, CALLEE_WITH_TIMEOUT)
        monkeypatch.chdir(tmp_path)
        assert main(["--json"]) == 0
        assert _findings(capsys)["findings"] == []

    @pytest.mark.parametrize(
        "on_block",
        [
            "on: workflow_call\n",
            "on: [push, workflow_call]\n",
            "on:\n  workflow_call:\n    inputs:\n      ref:\n        type: string\n",
        ],
    )
    def test_all_workflow_call_shapes_are_detected(self, tmp_path, monkeypatch, capsys, on_block):
        _workflow(tmp_path, on_block + CALLEE_NO_TIMEOUT.split("jobs:\n", 1)[0].split("on:", 1)[0]
                  + "jobs:\n" + CALLEE_NO_TIMEOUT.split("jobs:\n", 1)[1])
        monkeypatch.chdir(tmp_path)
        assert main(["--json"]) == 1
        assert _rules(_findings(capsys)["findings"]) == [RULE_CALLEE_MISSING_TIMEOUT]

    def test_callee_rule_wins_over_the_plain_rule(self, tmp_path, monkeypatch, capsys):
        _workflow(tmp_path, CALLEE_NO_TIMEOUT)
        monkeypatch.chdir(tmp_path)
        main(["--json"])
        assert RULE_SELF_HOSTED_MISSING_TIMEOUT not in _rules(_findings(capsys)["findings"])


# ── ④ 普通自托管作业缺 timeout ──────────────────────────────────────────────


class TestSelfHostedMissingTimeout:
    """Rule 3: the same check for workflows that are not ``workflow_call`` callees."""

    def test_plain_self_hosted_job_is_reported(self, tmp_path, monkeypatch, capsys):
        _workflow(tmp_path, PLAIN_NO_TIMEOUT)
        monkeypatch.chdir(tmp_path)
        assert main(["--json"]) == 1
        finding = _findings(capsys)["findings"][0]
        assert finding["rule"] == RULE_SELF_HOSTED_MISSING_TIMEOUT
        assert finding["line"] == 7
        assert "360-minute default" in finding["message"]

    def test_with_timeout_is_clean(self, tmp_path, monkeypatch, capsys):
        _workflow(tmp_path, PLAIN_NO_TIMEOUT.replace(
            "    runs-on: [self-hosted, linux, x64, local]\n",
            "    runs-on: [self-hosted, linux, x64, local]\n    timeout-minutes: 300\n",
        ))
        monkeypatch.chdir(tmp_path)
        assert main([]) == 0

    def test_block_sequence_runs_on_is_detected(self, tmp_path, monkeypatch, capsys):
        _workflow(
            tmp_path,
            "jobs:\n"
            "  build:\n"
            "    runs-on:\n"
            "      - self-hosted\n"
            "      - linux\n"
            "    steps:\n"
            "      - run: cargo build\n",
        )
        monkeypatch.chdir(tmp_path)
        assert main(["--json"]) == 1
        assert _rules(_findings(capsys)["findings"]) == [RULE_SELF_HOSTED_MISSING_TIMEOUT]

    def test_scalar_self_hosted_label_is_detected(self, tmp_path, monkeypatch, capsys):
        _workflow(tmp_path, "jobs:\n  build:\n    runs-on: self-hosted\n")
        monkeypatch.chdir(tmp_path)
        assert main(["--json"]) == 1
        assert _rules(_findings(capsys)["findings"]) == [RULE_SELF_HOSTED_MISSING_TIMEOUT]

    def test_hosted_job_without_timeout_is_out_of_scope(self, tmp_path, monkeypatch, capsys):
        _workflow(tmp_path, "jobs:\n  lint:\n    runs-on: ubuntu-latest\n    steps:\n      - run: true\n")
        monkeypatch.chdir(tmp_path)
        assert main([]) == 0

    def test_step_level_timeout_does_not_count_as_job_level(self, tmp_path, monkeypatch, capsys):
        # 判据是 **job 级** timeout-minutes；step 级的上界管不到 D 态进程。
        _workflow(
            tmp_path,
            "jobs:\n"
            "  build:\n"
            "    runs-on: [self-hosted, linux]\n"
            "    steps:\n"
            "      - run: cargo test\n"
            "        timeout-minutes: 30\n",
        )
        monkeypatch.chdir(tmp_path)
        assert main(["--json"]) == 1
        assert _rules(_findings(capsys)["findings"]) == [RULE_SELF_HOSTED_MISSING_TIMEOUT]

    def test_matrix_expression_runs_on_is_not_guessed(self, tmp_path, monkeypatch, capsys):
        # `runs-on: ${{ matrix.os }}` 无法离线判定自托管，不猜（与 celestia-job-timeouts 同口径）。
        _workflow(
            tmp_path,
            "jobs:\n"
            "  clippy:\n"
            "    runs-on: ${{ matrix.os }}\n"
            "    strategy:\n"
            "      matrix:\n"
            "        os: [windows-latest, macos-latest]\n",
        )
        monkeypatch.chdir(tmp_path)
        assert main([]) == 0


# ── ⑤ 单行非法 YAML 必须出现在 findings 里（禁止静默跳过） ──────────────────


class TestYamlParseError:
    """Rule 4: a file that cannot be parsed is reported, never skipped."""

    def test_single_line_file_is_reported_not_skipped(self, tmp_path, monkeypatch, capsys):
        _workflow(tmp_path, ONE_LINE_INVALID, filename="validate.yml")
        monkeypatch.chdir(tmp_path)
        assert main(["--json"]) == 1
        report = _findings(capsys)
        assert len(report["findings"]) == 1
        finding = report["findings"][0]
        assert finding["rule"] == RULE_YAML_PARSE_ERROR
        assert finding["path"] == ".github/workflows/validate.yml"
        assert finding["line"] == 1
        assert finding["severity"] == "error"

    def test_message_carries_the_first_line_summary(self, tmp_path, monkeypatch, capsys):
        _workflow(tmp_path, ONE_LINE_INVALID, filename="validate.yml")
        monkeypatch.chdir(tmp_path)
        main(["--json"])
        message = _findings(capsys)["findings"][0]["message"]
        assert "first line:" in message
        assert "name: Validate on: push:" in message
        assert "mapping values are not allowed here" in message

    def test_long_first_line_is_truncated(self, tmp_path, monkeypatch, capsys):
        _workflow(tmp_path, "name: " + "x" * 500 + " on: push: 1\n", filename="long.yml")
        monkeypatch.chdir(tmp_path)
        main(["--json"])
        message = _findings(capsys)["findings"][0]["message"]
        assert "..." in message
        assert len(message) < 400

    def test_broken_yaml_is_reported_even_with_a_legal_rest(self, tmp_path, monkeypatch, capsys):
        # 解析失败的文件绝不能因为"看起来像 workflow"而被放过。
        _workflow(tmp_path, "jobs:\n  build:\n    runs-on: [self-hosted\n", filename="broken.yml")
        monkeypatch.chdir(tmp_path)
        assert main(["--json"]) == 1
        assert _rules(_findings(capsys)["findings"]) == [RULE_YAML_PARSE_ERROR]

    def test_tab_indentation_is_a_parse_error_not_a_skip(self, tmp_path, monkeypatch, capsys):
        _workflow(tmp_path, "jobs:\n\tbuild:\n\t\truns-on: ubuntu-latest\n", filename="tabs.yml")
        monkeypatch.chdir(tmp_path)
        assert main(["--json"]) == 1
        assert _rules(_findings(capsys)["findings"]) == [RULE_YAML_PARSE_ERROR]

    def test_invalid_utf8_is_reported_not_crashed(self, tmp_path, monkeypatch, capsys):
        path = _workflow(tmp_path, "jobs:\n", filename="binary.yml")
        path.write_bytes(b"jobs:\n  build:\n    \xff\xfe runs-on: ubuntu-latest\n")
        monkeypatch.chdir(tmp_path)
        assert main(["--json"]) == 1
        finding = _findings(capsys)["findings"][0]
        assert finding["rule"] == RULE_YAML_PARSE_ERROR
        assert "UTF-8" in finding["message"]

    def test_parse_error_does_not_hide_a_sibling_file(self, tmp_path, monkeypatch, capsys):
        _workflow(tmp_path, ONE_LINE_INVALID, filename="validate.yml")
        _workflow(tmp_path, PLAIN_NO_TIMEOUT, filename="ci.yml")
        monkeypatch.chdir(tmp_path)
        assert main(["--json"]) == 1
        report = _findings(capsys)
        assert sorted(_rules(report["findings"])) == [
            RULE_SELF_HOSTED_MISSING_TIMEOUT, RULE_YAML_PARSE_ERROR,
        ]
        assert report["summary"]["files"] == 2

    def test_empty_and_non_mapping_files_are_not_parse_errors(self, tmp_path, monkeypatch, capsys):
        # 空文档/null 根能正常解析，不是 yaml-parse-error（规则 4 只覆盖"无法解析"）。
        _workflow(tmp_path, "", filename="empty.yml")
        _workflow(tmp_path, "- just\n- a list\n", filename="list.yml")
        monkeypatch.chdir(tmp_path)
        assert main([]) == 0


# ── ⑥ --json 结构与退出码 ───────────────────────────────────────────────────


class TestJsonContractAndExitCodes:
    """The frozen document CI consumes, and the 0/1/2 exit codes."""

    def test_json_shape_is_exactly_the_contract(self, tmp_path, monkeypatch, capsys):
        _workflow(tmp_path, ACCIDENT)
        _workflow(tmp_path, PLAIN_NO_TIMEOUT, filename="ci2.yml")
        monkeypatch.chdir(tmp_path)
        main(["--json"])
        report = _findings(capsys)
        assert set(report) == {"findings", "summary"}
        for finding in report["findings"]:
            assert set(finding) == {"path", "line", "rule", "severity", "message"}
            assert isinstance(finding["line"], int)
            assert finding["severity"] in {"error", "warning"}
        summary = report["summary"]
        assert set(summary) == {
            "files", "findings", "errors", "warnings", "suppressed", "failed", "rules",
        }
        assert summary["files"] == 2
        assert summary["findings"] == 2
        assert summary["errors"] == 2
        assert summary["warnings"] == 0
        assert summary["suppressed"] == 0
        assert summary["failed"] is True
        assert set(summary["rules"]) == {
            RULE_INVALID_CALLER_KEY, RULE_CALLEE_MISSING_TIMEOUT,
            RULE_SELF_HOSTED_MISSING_TIMEOUT, RULE_YAML_PARSE_ERROR,
        }
        assert summary["rules"][RULE_INVALID_CALLER_KEY] == 1

    def test_stdout_is_pure_json(self, tmp_path, monkeypatch, capsys):
        _workflow(tmp_path, ACCIDENT)
        monkeypatch.chdir(tmp_path)
        main(["--json"])
        captured = capsys.readouterr()
        assert json.loads(captured.out)  # 整份 stdout 就是 JSON
        assert "celestia-ci-audit:" in captured.err  # 摘要走 stderr

    def test_clean_tree_exits_0(self, tmp_path, monkeypatch, capsys):
        _workflow(tmp_path, LEGAL_CALLER)
        monkeypatch.chdir(tmp_path)
        assert main(["--json"]) == 0
        assert _findings(capsys)["summary"]["failed"] is False

    def test_error_exits_1(self, tmp_path, monkeypatch):
        _workflow(tmp_path, ACCIDENT)
        monkeypatch.chdir(tmp_path)
        assert main([]) == 1

    def test_missing_path_exits_2(self, tmp_path, monkeypatch, capsys):
        monkeypatch.chdir(tmp_path)
        assert main([str(tmp_path / "nope")]) == 2
        assert "no such file or directory" in capsys.readouterr().err

    def test_missing_default_workflows_directory_exits_2(self, tmp_path, monkeypatch, capsys):
        # fail-closed：默认路径不存在时不能"扫了 0 个文件然后报绿"。
        monkeypatch.chdir(tmp_path)
        assert main([]) == 2
        assert ".github/workflows" in capsys.readouterr().err

    def test_directory_without_workflows_exits_0_with_a_note(self, tmp_path, monkeypatch, capsys):
        empty = tmp_path / "empty"
        empty.mkdir()
        monkeypatch.chdir(tmp_path)
        assert main([str(empty)]) == 0
        assert "no workflow file found" in capsys.readouterr().err

    def test_strict_promotes_warnings_only(self):
        warning = Finding("a.yml", 1, RULE_SELF_HOSTED_MISSING_TIMEOUT, "warning", "w")
        error = Finding("a.yml", 1, RULE_INVALID_CALLER_KEY, "error", "e")
        assert has_failure([warning]) is False
        assert has_failure([warning], strict=True) is True
        assert has_failure([error]) is True
        assert has_failure([]) is False

    def test_strict_is_accepted_and_clean_tree_still_exits_0(self, tmp_path, monkeypatch):
        _workflow(tmp_path, CALLEE_WITH_TIMEOUT)
        monkeypatch.chdir(tmp_path)
        assert main(["--strict"]) == 0

    def test_human_output_lists_findings_then_summary(self, tmp_path, monkeypatch, capsys):
        _workflow(tmp_path, ACCIDENT)
        monkeypatch.chdir(tmp_path)
        main([])
        out = capsys.readouterr().out
        assert ".github/workflows/ci.yml:8: [invalid-caller-key]" in out
        assert "    timeout-minutes: 90" in out  # 原文摘录
        assert "1 finding(s) in 1 workflow file(s)" in out

    def test_clean_human_output_says_clean(self, tmp_path, monkeypatch, capsys):
        _workflow(tmp_path, CALLEE_WITH_TIMEOUT)
        monkeypatch.chdir(tmp_path)
        main([])
        assert "celestia-ci-audit: clean (1 workflow file(s) scanned)" in capsys.readouterr().out


# ── ⑦ 变异：把修复改坏必须变红 ──────────────────────────────────────────────


class TestMutationTurnsTheGateRed:
    """Start from a green tree, break the fix, and require the gate to notice."""

    def test_dropping_the_callee_timeout_turns_it_red(self, tmp_path, monkeypatch, capsys):
        path = _workflow(tmp_path, CALLEE_WITH_TIMEOUT)
        monkeypatch.chdir(tmp_path)
        assert _run([], capsys)[0] == 0  # 绿色基线

        _replace(path, "    timeout-minutes: 60\n", "")
        code, out, _err = _run(["--json"], capsys)
        assert code == 1
        finding = json.loads(out)["findings"][0]
        assert finding["rule"] == RULE_CALLEE_MISSING_TIMEOUT
        assert finding["line"] == 9

    def test_adding_the_illegal_key_back_turns_it_red(self, tmp_path, monkeypatch, capsys):
        path = _workflow(tmp_path, LEGAL_CALLER)
        monkeypatch.chdir(tmp_path)
        assert _run([], capsys)[0] == 0  # 绿色基线

        _replace(path, "    permissions:\n      contents: read\n",
                 "    permissions:\n      contents: read\n    timeout-minutes: 90\n")
        code, out, _err = _run(["--json"], capsys)
        assert code == 1
        finding = json.loads(out)["findings"][0]
        assert finding["rule"] == RULE_INVALID_CALLER_KEY
        assert "'timeout-minutes'" in finding["message"]
        assert finding["line"] == 18  # 追加在 permissions 之后

    def test_removing_the_illegal_key_turns_it_green_again(self, tmp_path, monkeypatch, capsys):
        # 前向修复方向：删掉非法键即归零（工具不是无条件红）。
        path = _workflow(tmp_path, ACCIDENT)
        monkeypatch.chdir(tmp_path)
        assert _run([], capsys)[0] == 1

        _replace(path, "    timeout-minutes: 90\n", "")
        code, out, _err = _run(["--json"], capsys)
        assert code == 0
        assert json.loads(out)["findings"] == []

    def test_collapsing_a_valid_file_onto_one_line_turns_it_red(self, tmp_path, monkeypatch, capsys):
        path = _workflow(tmp_path, CALLEE_WITH_TIMEOUT)
        monkeypatch.chdir(tmp_path)
        assert _run([], capsys)[0] == 0

        collapsed = " ".join(_read(path).split()) + "\n"  # 先读再写，别把文件截断成空
        with path.open("w", encoding="utf-8", newline="") as handle:
            handle.write(collapsed)
        code, out, _err = _run(["--json"], capsys)
        assert code == 1
        assert _rules(json.loads(out)["findings"]) == [RULE_YAML_PARSE_ERROR]


# ── --allow 允许列表 ────────────────────────────────────────────────────────


class TestAllowList:
    """``--allow RULE=GLOB``: loud, rule-checked, counted — never a silent pass."""

    def test_allow_suppresses_and_exits_0(self, tmp_path, monkeypatch, capsys):
        _workflow(tmp_path, ACCIDENT)
        monkeypatch.chdir(tmp_path)
        assert main(["--json", "--allow", "invalid-caller-key=.github/workflows/ci.yml"]) == 0
        captured = capsys.readouterr()
        report = json.loads(captured.out)
        assert report["findings"] == []
        assert report["summary"]["suppressed"] == 1
        assert "suppressed 1 finding(s) via --allow" in captured.err

    def test_allow_glob_matches_across_slashes(self, tmp_path, monkeypatch, capsys):
        _workflow(tmp_path, ACCIDENT)
        monkeypatch.chdir(tmp_path)
        assert main(["--json", "--allow", "invalid-caller-key=*.yml"]) == 0
        assert _findings(capsys)["summary"]["suppressed"] == 1

    def test_wildcard_rule_suppresses_anything(self, tmp_path, monkeypatch):
        _workflow(tmp_path, ACCIDENT)
        monkeypatch.chdir(tmp_path)
        assert main(["--allow", "*=.github/workflows/ci.yml"]) == 0

    def test_other_rule_does_not_suppress(self, tmp_path, monkeypatch, capsys):
        _workflow(tmp_path, ACCIDENT)
        monkeypatch.chdir(tmp_path)
        assert main(["--json", "--allow", "yaml-parse-error=.github/workflows/ci.yml"]) == 1
        assert _findings(capsys)["summary"]["suppressed"] == 0

    def test_other_path_does_not_suppress(self, tmp_path, monkeypatch):
        _workflow(tmp_path, ACCIDENT)
        monkeypatch.chdir(tmp_path)
        assert main(["--allow", "invalid-caller-key=frozen-repo/*"]) == 1

    def test_unknown_rule_is_a_usage_error(self, tmp_path, monkeypatch):
        _workflow(tmp_path, ACCIDENT)
        monkeypatch.chdir(tmp_path)
        with pytest.raises(SystemExit) as excinfo:
            main(["--allow", "typo-rule=*.yml"])
        assert excinfo.value.code == 2

    def test_malformed_allow_is_a_usage_error(self, tmp_path, monkeypatch):
        _workflow(tmp_path, ACCIDENT)
        monkeypatch.chdir(tmp_path)
        with pytest.raises(SystemExit) as excinfo:
            main(["--allow", "no-equals-sign"])
        assert excinfo.value.code == 2

    def test_allow_does_not_mask_an_unlisted_finding(self, tmp_path, monkeypatch, capsys):
        _workflow(tmp_path, ACCIDENT)
        _workflow(tmp_path, PLAIN_NO_TIMEOUT, filename="ci2.yml")
        monkeypatch.chdir(tmp_path)
        assert main(["--json", "--allow", "invalid-caller-key=.github/workflows/ci.yml"]) == 1
        report = _findings(capsys)
        assert _rules(report["findings"]) == [RULE_SELF_HOSTED_MISSING_TIMEOUT]
        assert report["summary"]["suppressed"] == 1


# ── 路径解析与 CLI 约定 ─────────────────────────────────────────────────────


class TestPathsAndCli:
    """PATHS handling: files, directories, repository roots, dedupe, relative reporting."""

    def test_default_path_is_github_workflows(self, tmp_path, monkeypatch, capsys):
        _workflow(tmp_path, CALLEE_WITH_TIMEOUT)
        monkeypatch.chdir(tmp_path)
        assert main([]) == 0
        assert "1 workflow file(s) scanned" in capsys.readouterr().out

    def test_explicit_file_argument(self, tmp_path, monkeypatch, capsys):
        _workflow(tmp_path, ACCIDENT)
        monkeypatch.chdir(tmp_path)
        assert main(["--json", ".github/workflows/ci.yml"]) == 1
        assert len(_findings(capsys)["findings"]) == 1

    def test_repository_root_means_dot_github_workflows(self, tmp_path, monkeypatch, capsys):
        _workflow(tmp_path, ACCIDENT)
        (tmp_path / "docker-compose.yml").write_text("services: {}\n", encoding="utf-8")
        monkeypatch.chdir(tmp_path)
        assert main(["--json", str(tmp_path)]) == 1
        report = _findings(capsys)
        # 仓库根参数只读 .github/workflows，不会把 docker-compose.yml 当 workflow。
        assert report["summary"]["files"] == 1
        assert report["findings"][0]["rule"] == RULE_INVALID_CALLER_KEY

    def test_multiple_paths_and_dedupe(self, tmp_path, monkeypatch, capsys):
        _workflow(tmp_path, ACCIDENT)
        _workflow(tmp_path, PLAIN_NO_TIMEOUT, filename="other.yml")
        monkeypatch.chdir(tmp_path)
        assert main(["--json", ".", ".github/workflows/ci.yml"]) == 1
        report = _findings(capsys)
        assert report["summary"]["files"] == 2  # 重复参数不会重复计数

    def test_paths_outside_cwd_are_absolute(self, tmp_path, monkeypatch, capsys):
        outside = tmp_path / "elsewhere"
        _workflow(outside, ACCIDENT)
        workdir = tmp_path / "work"
        workdir.mkdir()
        monkeypatch.chdir(workdir)
        assert main(["--json", str(outside)]) == 1
        assert _findings(capsys)["findings"][0]["path"].startswith("/")

    def test_findings_are_sorted_and_deterministic(self, tmp_path, monkeypatch, capsys):
        _workflow(tmp_path, ACCIDENT, filename="b.yml")
        _workflow(tmp_path, PLAIN_NO_TIMEOUT, filename="a.yml")
        monkeypatch.chdir(tmp_path)
        assert main(["--json"]) == 1
        paths = [finding["path"] for finding in _findings(capsys)["findings"]]
        assert paths == sorted(paths)

    def test_unreadable_directory_exits_2_instead_of_skipping(self, tmp_path, monkeypatch, capsys):
        import os

        locked = tmp_path / "locked"
        (locked / ".github" / "workflows").mkdir(parents=True)
        os.chmod(locked, 0o000)
        try:
            monkeypatch.chdir(tmp_path)
            if os.access(locked, os.R_OK):  # root 会无视权限位，跳过断言
                pytest.skip("running as root: permission bits are not enforced")
            assert main([str(locked)]) == 2
            assert "locked" in capsys.readouterr().err
        finally:
            os.chmod(locked, 0o755)

    def test_audit_text_helper_is_usable_directly(self):
        findings = audit_text(ACCIDENT, "ci.yml")
        assert [finding.rule for finding in findings] == [RULE_INVALID_CALLER_KEY]
        assert findings[0].path == "ci.yml"
        assert findings[0].to_json().keys() == {"path", "line", "rule", "severity", "message"}


class TestEntryPointRegistered:
    """The console script must really be declared in ``pyproject.toml`` (and the version bumped)."""

    def test_console_script_declared(self):
        from pathlib import Path as _Path

        # 用文本断言而不是 tomllib：本包声明支持 3.9，而 tomllib 是 3.11+ 才有的。
        pyproject = _Path(__file__).resolve().parent.parent / "pyproject.toml"
        text = pyproject.read_text(encoding="utf-8")
        assert 'celestia-ci-audit = "celestia_devtools.ci.workflow_audit:main"' in text
        scripts = text.split("[project.scripts]", 1)[1].split("\n[", 1)[0]
        assert "celestia-ci-audit" in scripts

    def test_pyyaml_is_a_runtime_dependency(self):
        from pathlib import Path as _Path

        pyproject = _Path(__file__).resolve().parent.parent / "pyproject.toml"
        text = pyproject.read_text(encoding="utf-8")
        dependencies = text.split("dependencies = [", 1)[1].split("]", 1)[0]
        assert "PyYAML>=6" in dependencies

    def test_entry_point_target_is_importable_and_callable(self):
        from celestia_devtools.ci.workflow_audit import main as entry

        assert callable(entry)

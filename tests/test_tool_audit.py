"""Tests for the tool-exemption audit (lint/tool_audit.py, SR-32)."""

from __future__ import annotations
import json
import subprocess
import pytest
from pathlib import Path
from celestia_devtools.lint.tool_audit import load_ledger, main, tool_audit_error

BEGIN = "<!-- tool-audit:begin -->"
END = "<!-- tool-audit:end -->"

def write_ledger(workspace: Path, toml_body: str) -> Path:
    ledger_dir = workspace / "_ledger"
    ledger_dir.mkdir(parents = True, exist_ok = True)
    ledger = ledger_dir / "tool-exemptions.md"
    ledger.write_text("# Tool exemptions\n\n%s\n```toml\n%s\n```\n%s\n" % (BEGIN, toml_body, END), encoding = "utf-8")
    return ledger

def make_repo(workspace: Path, name: str, files: dict) -> Path:
    repo = workspace / name
    repo.mkdir(parents = True, exist_ok = True)

    for relative, content in files.items():
        target = repo / relative
        target.parent.mkdir(parents = True, exist_ok = True)
        target.write_text(content, encoding = "utf-8")

    subprocess.run(["git", "init", "-q"], cwd = repo, check = True)
    subprocess.run(["git", "add", "-A"], cwd = repo, check = True)
    return repo

def workspace_with(tmp_path: Path, body: str) -> Path:
    write_ledger(tmp_path, body)
    return tmp_path

def test_registered_exact_path_passes(tmp_path, capsys):
    workspace = workspace_with(tmp_path, '[exempt]\n"app/tools/run.sh" = "installer"\n')
    make_repo(workspace, "app", {"tools/run.sh": "#!/bin/sh\necho hi\n"})
    assert main([str(workspace)]) == 0
    captured = capsys.readouterr()
    assert "1 script(s) across 1 repositories, 1 exempted, 0 unregistered" in captured.err

def test_unregistered_script_fails(tmp_path, capsys):
    workspace = workspace_with(tmp_path, '[exempt]\n"app/tools/other.sh" = "installer"\n')
    make_repo(workspace, "app", {"tools/run.sh": "#!/bin/sh\necho hi\n"})
    assert main([str(workspace)]) == 1
    captured = capsys.readouterr()
    assert "app/tools/run.sh: unregistered .sh script" in captured.err

def test_glob_pattern_registers_a_tree(tmp_path):
    workspace = workspace_with(tmp_path, '[exempt]\n"app/scripts/*.sh" = "rootfs build"\n')
    make_repo(workspace, "app", {"scripts/a.sh": "x\n", "scripts/nested/b.sh": "x\n"})
    assert main([str(workspace)]) == 0

def test_ps1_glob_registers_windows_scripts(tmp_path):
    workspace = workspace_with(tmp_path, '[exempt]\n"*.ps1" = "Windows-only"\n')
    make_repo(workspace, "app", {"install.ps1": "Write-Host hi\n"})
    assert main([str(workspace)]) == 0

def test_frozen_repository_is_exempt(tmp_path):
    workspace = workspace_with(tmp_path, '[exempt_repos]\nkei = "frozen repository"\n')
    make_repo(workspace, "kei", {"kernel/tools/a.sh": "x\n", "tools/b.sh": "x\n"})
    assert main([str(workspace)]) == 0

def test_missing_ledger_fails_closed(tmp_path, capsys):
    make_repo(tmp_path, "app", {"a.sh": "x\n"})
    assert main([str(tmp_path)]) == 2
    captured = capsys.readouterr()
    assert "ledger not found" in captured.err

def test_corrupt_toml_fails_closed(tmp_path, capsys):
    workspace = workspace_with(tmp_path, '[exempt\n"a.sh" = "x"\n')
    make_repo(workspace, "app", {"a.sh": "x\n"})

    # The parser must RAISE, not print-and-return an empty table (R1 m4: the CLI
    # assertion alone cannot distinguish the two, so pin the contract directly).
    with pytest.raises(tool_audit_error, match = "not valid TOML"):
        load_ledger(workspace / "_ledger" / "tool-exemptions.md")

    assert main([str(workspace)]) == 2
    captured = capsys.readouterr()
    assert "not valid TOML" in captured.err

def test_empty_block_fails_closed(tmp_path, capsys):
    workspace = workspace_with(tmp_path, "")
    make_repo(workspace, "app", {"a.sh": "x\n"})
    assert main([str(workspace)]) == 2
    captured = capsys.readouterr()
    assert "empty" in captured.err

def test_reason_is_required(tmp_path, capsys):
    workspace = workspace_with(tmp_path, '[exempt]\n"a.sh" = "   "\n')
    make_repo(workspace, "app", {"a.sh": "x\n"})
    assert main([str(workspace)]) == 2
    captured = capsys.readouterr()
    assert "has no reason" in captured.err

def test_workspace_without_repositories_fails_closed(tmp_path, capsys):
    workspace = workspace_with(tmp_path, '[exempt]\n"a.sh" = "x"\n')
    assert main([str(workspace)]) == 2
    captured = capsys.readouterr()
    assert "nothing to audit" in captured.err

def test_untracked_script_is_ignored(tmp_path):
    workspace = workspace_with(tmp_path, '[exempt]\n"app/tracked.sh" = "installer"\n')
    repo = make_repo(workspace, "app", {"tracked.sh": "x\n"})
    (repo / "generated.sh").write_text("x\n", encoding = "utf-8")
    assert main([str(workspace)]) == 0

def test_extensionless_script_is_out_of_scope(tmp_path):
    workspace = workspace_with(tmp_path, '[exempt]\n"app/README.md" = "placeholder"\n')
    make_repo(workspace, "app", {"office-render": "#!/bin/sh\nx\n"})
    assert main([str(workspace)]) == 0

def test_json_report(tmp_path, capsys):
    workspace = workspace_with(tmp_path, '[exempt]\n"app/ok.sh" = "installer"\n')
    make_repo(workspace, "app", {"ok.sh": "x\n", "bad.sh": "x\n"})
    assert main([str(workspace), "--json"]) == 1
    payload = json.loads(capsys.readouterr().out)
    assert payload["ok"] is False
    assert payload["repositories"] == 1
    assert payload["checked"] == 2
    assert payload["exempted"] == 1
    assert payload["findings"] == [{"path": "app/bad.sh", "repo": "app", "kind": ".sh"}]

def test_warn_only_exits_zero(tmp_path):
    workspace = workspace_with(tmp_path, '[exempt]\n"app/ok.sh" = "installer"\n')
    make_repo(workspace, "app", {"bad.sh": "x\n"})
    assert main([str(workspace), "--warn-only"]) == 0

def test_ledger_override(tmp_path):
    workspace = workspace_with(tmp_path, '[exempt]\n"app/ok.sh" = "installer"\n')
    make_repo(workspace, "app", {"ok.sh": "x\n"})
    other = tmp_path / "other-ledger.md"
    other.write_text("%s\n```toml\n[exempt_repos]\napp = 'all'\n```\n%s\n" % (BEGIN, END), encoding = 'utf-8')
    assert main([str(workspace), "--ledger", str(other)]) == 0

def test_workspace_tools_are_scanned(tmp_path, capsys):
    workspace = workspace_with(tmp_path, '[exempt]\n"app/a.sh" = "installer"\n')
    make_repo(workspace, "app", {"a.sh": "x\n"})
    tools = workspace / "_tools"
    tools.mkdir()
    (tools / "rogue.sh").write_text("x\n", encoding = "utf-8")
    assert main([str(workspace), "--json"]) == 1
    payload = json.loads(capsys.readouterr().out)
    assert {"path": "_tools/rogue.sh", "repo": "_tools", "kind": ".sh"} in payload["findings"]

def test_workspace_tools_glob_registration(tmp_path):
    workspace = workspace_with(tmp_path, '[exempt]\n"_tools/*.sh" = "workspace shim"\n')
    tools = workspace / "_tools"
    tools.mkdir()
    (tools / "registered.sh").write_text("x\n", encoding = "utf-8")
    assert main([str(workspace)]) == 0

def test_extensionless_hooks_are_out_of_scope(tmp_path):
    workspace = workspace_with(tmp_path, '[exempt]\n"app/a.sh" = "installer"\n')
    make_repo(workspace, "app", {"a.sh": "x\n"})
    hooks = workspace / "_hooks"
    hooks.mkdir()
    (hooks / "pre-commit").write_text("#!/bin/sh\nx\n", encoding = "utf-8")
    assert main([str(workspace)]) == 0

def test_git_timeout_is_a_gate_error(tmp_path, monkeypatch, capsys):
    workspace = workspace_with(tmp_path, '[exempt]\n"app/a.sh" = "x"\n')
    make_repo(workspace, "app", {"a.sh": "x\n"})

    def explode(*_args, **_kwargs):
        raise subprocess.TimeoutExpired(cmd = "git", timeout = 1)

    monkeypatch.setattr(subprocess, "run", explode)
    assert main([str(workspace)]) == 2
    captured = capsys.readouterr()
    assert "timed out" in captured.err

def test_json_error_payload_has_the_full_schema(tmp_path, capsys):
    make_repo(tmp_path, "app", {"a.sh": "x\n"})

    for argv in ([str(tmp_path), "--json"], [str(tmp_path / "missing"), "--json"]):
        assert main(argv) == 2
        payload = json.loads(capsys.readouterr().out)
        assert set(payload) == {"workspace", "ledger", "repositories", "checked", "exempted", "findings", "errors", "ok"}
        assert payload["ok"] is False
        assert payload["findings"] == []
        assert Path(payload["workspace"]).is_absolute()

def test_symlink_loop_stays_a_json_gate_error(tmp_path, capsys):
    """R3 nit: a path that cannot be resolved must still be rc=2 JSON, not a traceback."""
    loop_a = tmp_path / "loop-a"
    loop_b = tmp_path / "loop-b"
    loop_a.symlink_to(loop_b)
    loop_b.symlink_to(loop_a)
    assert main([str(loop_a), "--json"]) == 2
    payload = json.loads(capsys.readouterr().out)
    assert payload["ok"] is False
    assert set(payload) == {"workspace", "ledger", "repositories", "checked", "exempted", "findings", "errors", "ok"}

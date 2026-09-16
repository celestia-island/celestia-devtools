"""Tests for tools/external_review.py — the port of _tools/external-review.sh."""

from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

TOOLS_DIR = Path(__file__).resolve().parent.parent / "tools"
sys.path.insert(0, str(TOOLS_DIR))

import external_review  # noqa: E402


# ── helpers ───────────────────────────────────────────────────────────────────


def run_cli(argv, workspace: Path, extra_env=None):
    env = dict(os.environ)
    env["WORKSPACE_ROOT"] = str(workspace)
    env.pop("EXTERNAL_REVIEW_DAYS", None)
    if extra_env:
        env.update(extra_env)
    return subprocess.run(
        [sys.executable, str(TOOLS_DIR / "external_review.py"), *argv],
        capture_output=True,
        text=True,
        env=env,
    )


@pytest.fixture
def workspace(tmp_path: Path) -> Path:
    ws = tmp_path / "ws"
    ws.mkdir()
    return ws


def make_report(ws: Path, name: str, mtime_epoch: int) -> Path:
    reports = ws / "_reports"
    reports.mkdir(parents=True, exist_ok=True)
    path = reports / name
    path.write_text("record\n", encoding="utf-8")
    os.utime(path, (mtime_epoch, mtime_epoch))
    return path


def run_main(argv, workspace: Path, monkeypatch):
    """Run main() in-process (so monkeypatched helpers apply); SystemExit = code."""
    monkeypatch.setenv("WORKSPACE_ROOT", str(workspace))
    monkeypatch.delenv("EXTERNAL_REVIEW_DAYS", raising=False)
    try:
        return external_review.main(list(argv))
    except SystemExit as exc:  # die() path
        return exc.code


# ── due-date calculation from the 14-day cycle ───────────────────────────────


def test_cycle_days_left_matches_bash_integer_division():
    now = 1_000_000_000
    assert external_review.cycle_days_left(now, now, 14) == 14
    # 3.5 days old -> integer division truncates to 3 -> 11 left
    assert external_review.cycle_days_left(now - 3 * 86400 - 43200, now, 14) == 11
    # exactly 14 days old -> 0 -> overdue
    assert external_review.cycle_days_left(now - 14 * 86400, now, 14) == 0
    # 15.5 days old -> age 15 -> -1
    assert external_review.cycle_days_left(now - 15 * 86400 - 43200, now, 14) == -1


def test_status_line_fresh_and_overdue():
    now = int(time.time())
    line, rc = external_review.status_line(now, now, 14)
    assert rc == 0
    assert "未到期" in line and "还有 14 天" in line
    line, rc = external_review.status_line(now - 20 * 86400, now, 14)
    assert rc == 1
    assert "已超期 6 天" in line
    line, rc = external_review.status_line(0, now, 14)
    assert rc == 1
    assert "从未做过" in line


def test_last_review_epoch_picks_newest_and_tolerates_bad_files(workspace: Path):
    old = int(time.time()) - 100_000
    new = int(time.time())
    make_report(workspace, "external-review-2026-01-01.md", old)
    make_report(workspace, "external-review-2026-02-02.md", new)
    make_report(workspace, "unrelated.md", int(time.time()) + 500)
    make_report(workspace, "external-review-broken.md", new)  # glob still matches
    assert external_review.last_review_epoch(workspace / "_reports") >= new


def test_status_exit_codes_and_due_file(workspace: Path):
    # never reviewed -> exit 1 and DUE.md written
    proc = run_cli(["status"], workspace)
    assert proc.returncode == 1
    assert "从未做过" in proc.stdout
    due = workspace / "_lens" / "DUE.md"
    assert due.is_file() and "exit 1" in due.read_text(encoding="utf-8")

    # fresh record -> exit 0
    make_report(workspace, "external-review-x.md", int(time.time()))
    proc = run_cli(["status"], workspace)
    assert proc.returncode == 0
    assert "exit 0" in due.read_text(encoding="utf-8")

    # due is silent but keeps the exit code
    proc = run_cli(["due"], workspace)
    assert proc.returncode == 0
    assert proc.stdout == ""

    # overdue -> due exits 1 silently
    make_report(
        workspace, "external-review-old.md", int(time.time()) - 30 * 86400
    ).stat()
    Path(workspace / "_reports" / "external-review-x.md").unlink()
    proc = run_cli(["due"], workspace)
    assert proc.returncode == 1
    assert proc.stdout == ""


def test_external_review_days_env_override(workspace: Path):
    make_report(workspace, "external-review-x.md", int(time.time()) - 10 * 86400)
    proc = run_cli(["status"], workspace)
    assert proc.returncode == 0  # 10 days old is within the default 14
    proc = run_cli(["status"], workspace, {"EXTERNAL_REVIEW_DAYS": "7"})
    assert proc.returncode == 1  # ...but overdue on a 7-day cadence


# ── pack command file assembly ───────────────────────────────────────────────


@pytest.fixture
def fake_ws(workspace: Path, monkeypatch) -> Path:
    """A tiny fake workspace: one repo, one sampled file, HTTP calls stubbed."""
    repo = workspace / "entelecheia"
    (repo / ".git").mkdir(parents=True)
    # use one of the real SAMPLE_FILES paths so pack() picks it up
    sample = repo / "scripts" / "deploy" / "backup.py"
    sample.parent.mkdir(parents=True)
    sample.write_text("# example\n", encoding="utf-8")

    def fake_git(*args, repo=None):  # noqa: F811
        if "--verify" in args or "rev-list" in args:
            return None
        if args[:1] == ("ls-files",):
            return "a\nb\nc"
        if "--format=%ad" in args:
            return "2026-09-14"
        if args[:1] == ("log",):
            return "2026-09-14 something"
        return None

    monkeypatch.setattr(external_review, "git", fake_git)
    # never touch the network from tests
    monkeypatch.setattr(
        external_review, "write_live_surface", lambda out: (out / "03-live-surface.md")
        .write_text("stub\n", encoding="utf-8")
    )
    return workspace


def test_pack_assembles_expected_files(fake_ws: Path, monkeypatch, capsys):
    out = fake_ws / "pack"
    rc = run_main(["pack", "--out", str(out)], fake_ws, monkeypatch)
    assert rc == 0, capsys.readouterr().err
    for name in (
        "00-code-provenance.md",
        "01-repos.md",
        "02-recent-changes.md",
        "03-live-surface.md",
        "REVIEWER-PROMPT.md",
        "00-manifest.md",
    ):
        assert (out / name).is_file(), name
    # the sampled working-tree file lands under code/ (no origin ref in fake repo)
    assert (out / "code" / "entelecheia" / "scripts" / "deploy" / "backup.py").is_file()
    provenance = (out / "00-code-provenance.md").read_text(encoding="utf-8")
    assert "工作树（无 origin ref）" in provenance  # explicit downgrade, not silent
    repos = (out / "01-repos.md").read_text(encoding="utf-8")
    assert "| entelecheia | 3 | 1 | 2026-09-14 |" in repos
    changes = (out / "02-recent-changes.md").read_text(encoding="utf-8")
    assert "## entelecheia" in changes and "2026-09-14 something" in changes
    # isolation gate passed -> manifest notes no internal references
    assert "（无）" in (out / "00-manifest.md").read_text(encoding="utf-8")


def test_pack_refuses_existing_output(fake_ws: Path, monkeypatch, capsys):
    out = fake_ws / "pack"
    out.mkdir()
    rc = run_main(["pack", "--out", str(out)], fake_ws, monkeypatch)
    assert rc == 2
    assert "目标已存在" in capsys.readouterr().err


def test_pack_isolation_gate_fails_and_wipes_output(fake_ws: Path, monkeypatch, capsys):
    # simulate a leaked rule FILE inside the pack (gate runs before the manifest)
    def leak(ws, out):
        (out / "AGENTS.md").write_text("leak", encoding="utf-8")

    monkeypatch.setattr(external_review, "write_recent_changes", leak)
    out = fake_ws / "pack"
    rc = run_main(["pack", "--out", str(out)], fake_ws, monkeypatch)
    assert rc == 1
    assert "隔离失败" in capsys.readouterr().err
    assert not out.exists()  # 证据包已作废


def test_pack_content_leak_gate(fake_ws: Path, monkeypatch, capsys):
    monkeypatch.setattr(
        external_review,
        "write_manifest",
        lambda out: None,  # keep files, but inject a rule reference into 01-repos.md
    )
    original = external_review.write_repos_table

    def inject(ws, out):
        original(ws, out)
        p = out / "01-repos.md"
        p.write_text(p.read_text(encoding="utf-8") + "see AGENTS.md\n", encoding="utf-8")

    monkeypatch.setattr(external_review, "write_repos_table", inject)
    out = fake_ws / "pack"
    rc = run_main(["pack", "--out", str(out)], fake_ws, monkeypatch)
    assert rc == 1
    assert "引用了工作区规则" in capsys.readouterr().err


# ── record command state update ──────────────────────────────────────────────


def test_record_writes_report_and_resets_cycle(workspace: Path):
    src = workspace / "answer.md"
    src.write_text("外部验证者的回答\n", encoding="utf-8")
    proc = run_cli(["record", str(src)], workspace)
    assert proc.returncode == 0
    reports = sorted((workspace / "_reports").glob("external-review-*.md"))
    assert len(reports) == 1
    text = reports[0].read_text(encoding="utf-8")
    assert "外部视角验证记录" in text
    assert "外部验证者的回答" in text
    # the new record resets the cadence -> status green again
    proc = run_cli(["status"], workspace)
    assert proc.returncode == 0


def test_record_rejects_missing_file(workspace: Path):
    proc = run_cli(["record", str(workspace / "nope.md")], workspace)
    assert proc.returncode == 2
    assert "用法" in proc.stderr
    assert not (workspace / "_reports").exists()


# ── CLI surface parity with the bash script ──────────────────────────────────


def test_help_and_unknown_subcommand(workspace: Path):
    proc = run_cli(["--help"], workspace)
    assert proc.returncode == 0
    assert "pack" in proc.stdout and "record" in proc.stdout
    # bare invocation prints the usage like the bash script
    proc = run_cli([], workspace)
    assert proc.returncode == 0
    assert "用法" in proc.stdout
    proc = run_cli(["bogus"], workspace)
    assert proc.returncode == 2


def test_unknown_subcommand_message_parity(workspace: Path):
    proc = run_cli(["frobnicate"], workspace)
    assert "未知子命令" in proc.stderr

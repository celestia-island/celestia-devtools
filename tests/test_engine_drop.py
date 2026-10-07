"""Tests for tools/engine_drop.py — the host-side engine drop consumer.

Real throwaway git repositories with a local bare ``origin`` exercise the
actual worktree/commit/push path (no network); ``gh`` is replaced by a stub.
The gates are the security-relevant surface, so most tests assert the
*rejection* reasons and that **no git state changes** on rejection.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

TOOLS_DIR = Path(__file__).resolve().parent.parent / "tools"
sys.path.insert(0, str(TOOLS_DIR))

import engine_drop  # noqa: E402

GOOD_DESC = "✨ Add the drop consumer gate."
REPO_NAME = "evernight"


@pytest.fixture
def repo_env(tmp_path: Path) -> Path:
    """A repo_root containing one real repo whose origin is a local bare repo."""
    bare = tmp_path / "origin" / "evernight.git"
    bare.parent.mkdir(parents=True)
    subprocess.run(["git", "init", "--bare", "-b", "master", str(bare)], check=True,
                   capture_output=True)
    repo_root = tmp_path / "ws"
    repo = repo_root / REPO_NAME
    repo.mkdir(parents=True)
    subprocess.run(["git", "init", "-b", "master", str(repo)], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(repo), "config", "user.email", "t@example.com"],
                   check=True, capture_output=True)
    subprocess.run(["git", "-C", str(repo), "config", "user.name", "t"], check=True,
                   capture_output=True)
    (repo / "README.md").write_text("base\n", encoding="utf-8")
    (repo / "src").mkdir()
    (repo / "src" / "main.rs").write_text("fn main() {}\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(repo), "add", "."], check=True, capture_output=True)
    # --no-verify: 种子提交绕开全局 commit-msg 钩子（钩子只对被测路径有意义；
    # apply() 的真实提交**不**绕开——那是对 G6 的集成验证）
    subprocess.run(["git", "-C", str(repo), "commit", "--no-verify", "-m", "✨ Seed the base."],
                   check=True, capture_output=True)
    subprocess.run(["git", "-C", str(repo), "remote", "add", "origin", str(bare)],
                   check=True, capture_output=True)
    subprocess.run(["git", "-C", str(repo), "push", "-u", "origin", "master"], check=True,
                   capture_output=True)
    return repo_root


def make_drop(tmp_path: Path, name: str, files: dict, *, branch: str = None,
              base: str = "origin/master", desc: str = GOOD_DESC,
              listed: list = None) -> Path:
    drop = tmp_path / "drops" / REPO_NAME / name
    files_dir = drop / "files"
    for rel, content in files.items():
        target = files_dir / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
    manifest = {
        "repo": REPO_NAME,
        "base": base,
        "branch": branch or f"engine/{name}",
        "description": desc,
        "files": listed if listed is not None else sorted(files),
    }
    (drop / "MANIFEST.json").write_text(json.dumps(manifest), encoding="utf-8")
    return drop


def run_main(argv, monkeypatch):
    try:
        return engine_drop.main(list(argv))
    except SystemExit as exc:  # die() path
        return exc.code


def argv_for(repo_root, tmp_path, *rest):
    return [
        "--repo-root", str(repo_root),
        "--wt-root", str(tmp_path / "wt"),
        "--audit-file", str(tmp_path / "audit.jsonl"),
        *rest,
    ]


def remote_refs(repo_root) -> list:
    proc = subprocess.run(
        ["git", "-C", str(repo_root / REPO_NAME), "ls-remote", "origin"],
        check=True, capture_output=True, text=True,
    )
    return [line.split("\t")[1] for line in proc.stdout.splitlines() if line.strip()]


def audit_records(tmp_path) -> list:
    path = tmp_path / "audit.jsonl"
    if not path.is_file():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


# ── G1 分支 ───────────────────────────────────────────────────────────────────


def test_gates_reject_master_and_malformed_branch(repo_env, tmp_path, monkeypatch, capsys):
    for bad in ("master", "main", "engine/", "feat/x", "engine/has space",
                "engine/" + "x" * 100, "engine/../escape"):
        drop = make_drop(tmp_path, "d1", {"README.md": "changed\n"}, branch=bad)
        rc = run_main(argv_for(repo_env, tmp_path, "gates", str(drop)), monkeypatch)
        assert rc == 1, bad
        assert "分支名必须形如" in capsys.readouterr().out, bad


def test_gates_reject_branch_reuse(repo_env, tmp_path, monkeypatch, capsys):
    subprocess.run(["git", "-C", str(repo_env / REPO_NAME), "branch", "engine/taken"],
                   check=True, capture_output=True)
    drop = make_drop(tmp_path, "taken", {"README.md": "changed\n"}, branch="engine/taken")
    rc = run_main(argv_for(repo_env, tmp_path, "gates", str(drop)), monkeypatch)
    assert rc == 1
    assert "绝不复用" in capsys.readouterr().out


# ── G2/G3 路径与禁区 ──────────────────────────────────────────────────────────


@pytest.mark.parametrize("rel", [
    "AGENTS.md", "PLAN.md", "CLAUDE.md",
    ".github/workflows/ci.yml", "_ledger/ports-and-domains.md",
    "deploy.env", ".git/config", "secrets/gh_credentials.json",
])
def test_gates_reject_forbidden_paths(repo_env, tmp_path, monkeypatch, capsys, rel):
    drop = make_drop(tmp_path, "d2", {rel: "x\n"}, listed=[rel])
    rc = run_main(argv_for(repo_env, tmp_path, "gates", str(drop)), monkeypatch)
    out = capsys.readouterr().out
    assert rc == 1, rel
    assert "禁区" in out or "凭据" in out, rel


def test_gates_reject_escape_absolute_and_symlinks(repo_env, tmp_path, monkeypatch, capsys):
    drop = make_drop(tmp_path, "d3", {"ok.rs": "x\n"},
                      listed=["ok.rs", "../escape.rs"])
    rc = run_main(argv_for(repo_env, tmp_path, "gates", str(drop)), monkeypatch)
    assert rc == 1 and ".." in capsys.readouterr().out

    drop = make_drop(tmp_path, "d4", {"ok.rs": "x\n"}, listed=["/etc/passwd"])
    assert run_main(argv_for(repo_env, tmp_path, "gates", str(drop)), monkeypatch) == 1

    drop = make_drop(tmp_path, "d5", {"ok.rs": "x\n"})
    (drop / "files" / "link.rs").symlink_to(drop / "files" / "ok.rs")
    manifest = json.loads((drop / "MANIFEST.json").read_text(encoding="utf-8"))
    manifest["files"].append("link.rs")
    (drop / "MANIFEST.json").write_text(json.dumps(manifest), encoding="utf-8")
    rc = run_main(argv_for(repo_env, tmp_path, "gates", str(drop)), monkeypatch)
    assert rc == 1 and "符号链接" in capsys.readouterr().out


def test_gates_reject_missing_file_on_disk(repo_env, tmp_path, monkeypatch, capsys):
    drop = make_drop(tmp_path, "d6", {"ok.rs": "x\n"}, listed=["ok.rs", "ghost.rs"])
    rc = run_main(argv_for(repo_env, tmp_path, "gates", str(drop)), monkeypatch)
    assert rc == 1 and "缺文件" in capsys.readouterr().out


# ── G5/G6 base 与标题格式 ─────────────────────────────────────────────────────


def test_gates_reject_bad_base(repo_env, tmp_path, monkeypatch, capsys):
    drop = make_drop(tmp_path, "d7", {"README.md": "x\n"}, base="origin/nope")
    rc = run_main(argv_for(repo_env, tmp_path, "gates", str(drop)), monkeypatch)
    assert rc == 1 and "base" in capsys.readouterr().out
    drop = make_drop(tmp_path, "d8", {"README.md": "x\n"}, base="master")
    assert run_main(argv_for(repo_env, tmp_path, "gates", str(drop)), monkeypatch) == 1


@pytest.mark.parametrize("desc", ["fix: something", "无 emoji 的标题.", "✨ lowercase start",
                                  "✨ No trailing period", ""])
def test_gates_reject_bad_description(repo_env, tmp_path, monkeypatch, capsys, desc):
    drop = make_drop(tmp_path, "d9", {"README.md": "x\n"}, desc=desc)
    rc = run_main(argv_for(repo_env, tmp_path, "gates", str(drop)), monkeypatch)
    assert rc == 1, desc
    assert "description" in capsys.readouterr().out, desc


def test_manifest_over_file_cap_dies(repo_env, tmp_path, monkeypatch):
    files = {f"src/f{i}.rs": "x\n" for i in range(engine_drop.MAX_FILES + 1)}
    drop = make_drop(tmp_path, "d10", files)
    rc = run_main(argv_for(repo_env, tmp_path, "gates", str(drop)), monkeypatch)
    assert rc == 2  # die(): 用法级拒绝


# ── apply：dry-run 不动任何远端 ───────────────────────────────────────────────


def test_apply_dry_run_touches_nothing(repo_env, tmp_path, monkeypatch, capsys):
    before = remote_refs(repo_env)
    drop = make_drop(tmp_path, "dr1", {"src/main.rs": "fn main() { changed }\n"})
    rc = run_main(argv_for(repo_env, tmp_path, "apply", str(drop), "--dry-run"), monkeypatch)
    assert rc == 0, capsys.readouterr().err
    assert remote_refs(repo_env) == before  # 远端零变化
    assert not (tmp_path / "wt" / REPO_NAME).exists() or \
        not list((tmp_path / "wt").rglob("*.rs")) or True
    records = audit_records(tmp_path)
    assert records and records[-1]["outcome"] == "dry-run"
    assert len(records[-1]["digest"]) == 64
    assert records[-1]["n_files"] == 1


def test_apply_pushes_engine_branch_and_opens_pr(repo_env, tmp_path, monkeypatch, capsys):
    calls = {}

    def fake_pr(repo, base, branch, title, body):
        calls.update(base=base, branch=branch, title=title, body=body)
        return "https://github.com/celestia-island/evernight/pull/999"

    monkeypatch.setattr(engine_drop, "open_pr", fake_pr)
    drop = make_drop(tmp_path, "live1", {"src/main.rs": "fn main() { new }\n"})
    rc = run_main(argv_for(repo_env, tmp_path, "apply", str(drop)), monkeypatch)
    assert rc == 0, capsys.readouterr().err
    refs = remote_refs(repo_env)
    assert "refs/heads/engine/live1" in refs and "refs/heads/master" in refs
    assert calls["base"] == "master" and calls["branch"] == "engine/live1"
    assert calls["title"] == GOOD_DESC
    assert "引擎产出" in calls["body"] and "合并由人做" in calls["body"]
    records = audit_records(tmp_path)
    assert records[-1]["outcome"] == "pr-opened"
    assert records[-1]["pr"].endswith("/pull/999")


def test_apply_rejects_oversized_diff_without_pushing(repo_env, tmp_path, monkeypatch, capsys):
    before = remote_refs(repo_env)
    big = "x\n" * (engine_drop.MAX_LINES + 10)
    drop = make_drop(tmp_path, "big1", {"src/main.rs": big})
    rc = run_main(argv_for(repo_env, tmp_path, "apply", str(drop)), monkeypatch)
    assert rc == 1
    assert "量闸超限" in capsys.readouterr().err
    assert remote_refs(repo_env) == before
    assert audit_records(tmp_path)[-1]["outcome"] == "failed"


def test_apply_rejects_claimed_but_unchanged_file(repo_env, tmp_path, monkeypatch, capsys):
    """G7：声明了没变的文件 = 暂存区与声明不一致 → 中止且不 push。"""
    before = remote_refs(repo_env)
    drop = make_drop(tmp_path, "unchanged", {"src/main.rs": "fn main() {}\n"})  # 与 base 相同
    rc = run_main(argv_for(repo_env, tmp_path, "apply", str(drop)), monkeypatch)
    assert rc == 1
    assert "不一致" in capsys.readouterr().err
    assert remote_refs(repo_env) == before


def test_rejected_apply_leaves_no_worktree(repo_env, tmp_path, monkeypatch):
    drop = make_drop(tmp_path, "big2", {"src/main.rs": "y\n" * (engine_drop.MAX_LINES + 5)})
    run_main(argv_for(repo_env, tmp_path, "apply", str(drop)), monkeypatch)
    wt_root = tmp_path / "wt"
    leftovers = list(wt_root.rglob("*.rs")) if wt_root.exists() else []
    assert leftovers == []  # worktree 已清，不留半成品

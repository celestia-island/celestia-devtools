"""Tests for tools/engine_drop.py — the host-side engine drop consumer.

Real throwaway git repositories with a local bare ``origin`` exercise the
actual worktree/commit/push path (no network); ``gh`` is replaced by a stub.
The gates are the security-relevant surface, so most tests assert the
*rejection* reasons and that **no git state changes** on rejection.
"""

from __future__ import annotations

import json
import os
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
              listed: list = None, repo: str = REPO_NAME) -> Path:
    drop = tmp_path / "drops" / REPO_NAME / name
    files_dir = drop / "files"
    for rel, content in files.items():
        target = files_dir / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
    manifest = {
        "repo": repo,
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
        "--drop-root", str(tmp_path / "drops"),
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
    assert rc == 1 and rel in out, rel


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


def test_manifest_over_file_cap_is_rejected_and_audited(repo_env, tmp_path, monkeypatch):
    files = {f"src/f{i}.rs": "x\n" for i in range(engine_drop.MAX_FILES + 1)}
    drop = make_drop(tmp_path, "d10", files)
    rc = run_main(argv_for(repo_env, tmp_path, "gates", str(drop)), monkeypatch)
    assert rc == 1  # 违规（不是 die）：畸形 drop 也要能进审计
    assert run_main(argv_for(repo_env, tmp_path, "apply", str(drop)), monkeypatch) == 1
    assert audit_records(tmp_path)[-1]["outcome"] == "gates-rejected"
    assert len(audit_records(tmp_path)[-1]["digest"]) == 64


# ── apply：dry-run 不动任何远端 ───────────────────────────────────────────────


def test_apply_dry_run_touches_nothing(repo_env, tmp_path, monkeypatch, capsys):
    before = remote_refs(repo_env)
    drop = make_drop(tmp_path, "dr1", {"src/main.rs": "fn main() { changed }\n"})
    rc = run_main(argv_for(repo_env, tmp_path, "apply", str(drop), "--dry-run"), monkeypatch)
    assert rc == 0, capsys.readouterr().err
    assert remote_refs(repo_env) == before  # 远端零变化
    assert not (tmp_path / "wt" / REPO_NAME).exists() or \
        not any((tmp_path / "wt" / REPO_NAME).iterdir())  # worktree 已清空
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


# ── 评审 R3（2026-10-08）抓到的洞的钉子 ──────────────────────────────────────


def test_repo_pointer_cannot_escape(repo_env, tmp_path, monkeypatch, capsys):
    """P0：repo 指针必须与 drop 物理位置绑定——`../别的仓` / 绝对路径 / 前导点全拒。"""
    for bad in ("../celestia-devtools", "/etc", ".git", "a/b", "..", ""):
        drop = make_drop(tmp_path, "esc", {"README.md": "x\n"}, repo=bad)
        rc = run_main(argv_for(repo_env, tmp_path, "gates", str(drop)), monkeypatch)
        out = capsys.readouterr().out
        # 空串走 manifest 类型校验、其余走单段名正则——都是拒绝，措辞二选一
        assert rc == 1 and ("单段名" in out or "非空字符串" in out), bad


def test_repo_must_match_drop_location(repo_env, tmp_path, monkeypatch, capsys):
    """drop 放在 evernight/ 名下、manifest 却声明别的合法单段名 → 拒。"""
    drop = make_drop(tmp_path, "mism", {"README.md": "x\n"}, repo="shittim-chest")
    rc = run_main(argv_for(repo_env, tmp_path, "gates", str(drop)), monkeypatch)
    assert rc == 1
    assert "物理位置不符" in capsys.readouterr().out


@pytest.mark.parametrize("rel", [
    "foo/AGENTS.md", "deep/a/b/PLAN.md", "x/CLAUDE.md",
    "AGENTS.MD", "Agents.md", "Plan.MD",
    ".env.local", ".env.production", "deploy.ENV", "conf/app.env",
    "my-credential.txt", "keys/gh_credentials.json",
    "a/.git/config", "sub/.github/workflows/ci.yml", "x/_ledger/ports.md",
])
def test_forbidden_bypass_matrix_is_closed(repo_env, tmp_path, monkeypatch, rel):
    """P1：嵌套治理文件 / 大小写变体 / dotenv 家族 / 单数 credential / 任意层级组件。"""
    drop = make_drop(tmp_path, "fz", {rel: "x\n"}, listed=[rel])
    rc = run_main(argv_for(repo_env, tmp_path, "gates", str(drop)), monkeypatch)
    assert rc == 1, rel


def test_unnormalized_path_shape_is_rejected(repo_env, tmp_path, monkeypatch):
    """`./AGENTS.md` 这类未正规化路径在 gates 层就拒，不留到 G7 侥幸拦。"""
    drop = make_drop(tmp_path, "norm", {"ok.rs": "x\n"}, listed=["./ok.rs"])
    rc = run_main(argv_for(repo_env, tmp_path, "gates", str(drop)), monkeypatch)
    assert rc == 1  # 形状违规（不是禁区误报也算拒）


def test_hardlink_is_rejected(repo_env, tmp_path, monkeypatch, capsys):
    """P1：硬链接过 is_symlink 检查——宿主机任意可读文件的外泄通道，必须拒。"""
    secret = tmp_path / "host-secret.txt"
    secret.write_text("TOPSECRET-ssh-key-material\n", encoding="utf-8")
    drop = make_drop(tmp_path, "hl", {"notes.txt": "placeholder\n"})
    target = drop / "files" / "notes.txt"
    target.unlink()
    os.link(secret, target)
    assert target.stat().st_nlink == 2
    rc = run_main(argv_for(repo_env, tmp_path, "gates", str(drop)), monkeypatch)
    assert rc == 1 and "硬链接" in capsys.readouterr().out
    # apply 也必须拒（即使 gates 被绕过，拷贝端还有 st_nlink 复核）
    rc = run_main(argv_for(repo_env, tmp_path, "apply", str(drop)), monkeypatch)
    assert rc == 1
    assert "refs/heads/engine/hl" not in remote_refs(repo_env)


def test_byte_caps_close_the_line_collapse_hole(repo_env, tmp_path, monkeypatch, capsys):
    """P2：numstat 对二进制/单行文本塌缩为 0/1 行——字节闸补位。"""
    blob = "x" * (engine_drop.MAX_FILE_BYTES + 1)
    drop = make_drop(tmp_path, "bigblob", {"src/blob.bin": blob})
    rc = run_main(argv_for(repo_env, tmp_path, "gates", str(drop)), monkeypatch)
    assert rc == 1 and "字节" in capsys.readouterr().out

    many = {f"src/f{i}.bin": "y" * 100 for i in range(20)}  # 每个都合法，总量也合法
    drop = make_drop(tmp_path, "oksize", many)
    assert run_main(argv_for(repo_env, tmp_path, "gates", str(drop)), monkeypatch) == 0


def test_drop_digest_is_creation_order_independent(tmp_path):
    """P3/Mut-C：同内容不同创建序 → 同摘要（排序必须钉住）。"""
    contents = {"src/a.rs": "A\n", "src/b.rs": "B\n", "c.md": "C\n"}

    def build(tag, reverse):
        drop = tmp_path / "drops" / REPO_NAME / tag  # 两个不同目录、同一 MANIFEST 内容
        files_dir = drop / "files"
        names = list(contents) if not reverse else list(reversed(list(contents)))
        for rel in names:
            target = files_dir / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(contents[rel], encoding="utf-8")
        manifest = {"repo": REPO_NAME, "base": "origin/master",
                    "branch": "engine/ord", "description": GOOD_DESC,
                    "files": sorted(contents)}
        (drop / "MANIFEST.json").write_text(json.dumps(manifest), encoding="utf-8")
        return engine_drop.drop_digest(drop)

    assert build("ord-a", reverse=False) == build("ord-b", reverse=True)


def test_description_allows_version_numbers(repo_env, tmp_path, monkeypatch):
    """P3：句中句点（v1.1）合法——旧正则过严。"""
    drop = make_drop(tmp_path, "ver", {"README.md": "x\n"}, desc="✨ Fix the v1.1 parser.")
    assert run_main(argv_for(repo_env, tmp_path, "gates", str(drop)), monkeypatch) == 0


def test_branch_git_ref_rules(repo_env, tmp_path, monkeypatch, capsys):
    """P3：BRANCH_RE 放行但 git 拒绝的名字（x.lock）必须在 gates 层报。"""
    drop = make_drop(tmp_path, "lock", {"README.md": "x\n"}, branch="engine/x.lock")
    rc = run_main(argv_for(repo_env, tmp_path, "gates", str(drop)), monkeypatch)
    assert rc == 1 and "check-ref-format" in capsys.readouterr().out


def test_non_string_manifest_values_are_rejected_not_crash(repo_env, tmp_path, monkeypatch):
    """P3：files 含非字符串 → 违规 + 审计，不是裸 traceback。"""
    drop = make_drop(tmp_path, "badtype", {"README.md": "x\n"})
    manifest = json.loads((drop / "MANIFEST.json").read_text(encoding="utf-8"))
    manifest["files"] = [1, {"x": 2}, None]
    (drop / "MANIFEST.json").write_text(json.dumps(manifest), encoding="utf-8")
    rc = run_main(argv_for(repo_env, tmp_path, "apply", str(drop)), monkeypatch)
    assert rc == 1
    assert audit_records(tmp_path)[-1]["outcome"] == "gates-rejected"


def test_malformed_manifest_is_audited(repo_env, tmp_path, monkeypatch):
    """P2：坏 JSON / 缺字段也要进审计（畸形 drop 恰是最该留痕的信号）。"""
    drop = make_drop(tmp_path, "badjson", {"README.md": "x\n"})
    (drop / "MANIFEST.json").write_text("{ truncated", encoding="utf-8")
    rc = run_main(argv_for(repo_env, tmp_path, "apply", str(drop)), monkeypatch)
    assert rc == 1
    record = audit_records(tmp_path)[-1]
    assert record["outcome"] == "gates-rejected" and record["violations"]


def test_repo_leading_dot_directory_is_rejected(repo_env, tmp_path, monkeypatch, capsys):
    """N2 钉子：drop 物理放在 `.git/` 名下、repo 也如实声明——绑定判据满足，
    必须由单段名正则（拒前导点）拦下。"""
    drop = tmp_path / "drops" / ".git" / "esc2"
    files_dir = drop / "files" / "src"
    files_dir.mkdir(parents=True)
    (files_dir / "main.rs").write_text("x\n", encoding="utf-8")
    (drop / "MANIFEST.json").write_text(json.dumps({
        "repo": ".git", "base": "origin/master", "branch": "engine/esc2",
        "description": GOOD_DESC, "files": ["src/main.rs"],
    }), encoding="utf-8")
    rc = run_main(argv_for(repo_env, tmp_path, "gates", str(drop)), monkeypatch)
    out = capsys.readouterr().out
    assert rc == 1 and ("单段名" in out or "不是 git 仓" in out)


def test_copy_file_strict_refuses_symlink_swap(tmp_path):
    """N7 钉子：O_NOFOLLOW——把文件换成符号链接后走拷贝端必须炸，而不是跟着链接走。"""
    import pytest as _pytest
    real = tmp_path / "real.txt"
    real.write_text("SECRET", encoding="utf-8")
    link = tmp_path / "link.txt"
    link.symlink_to(real)
    dst = tmp_path / "dst.txt"
    with _pytest.raises((RuntimeError, OSError)):
        engine_drop.copy_file_strict(link, dst)
    assert not dst.exists()  # 拷贝失败不得留下半成品
    # 真文件仍然可拷
    dst2 = tmp_path / "dst2.txt"
    engine_drop.copy_file_strict(real, dst2)
    assert dst2.read_text(encoding="utf-8") == "SECRET"


def test_check_repo_regex_unit(tmp_path):
    """单段名正则的单元钉：绑定判据满足时，形状仍必须把关（前导点/斜杠/空）。"""
    drop_root = tmp_path / "drops"
    for bad in (".git", "../evil", "/abs", "a/b", "", ".."):
        violations = engine_drop.check_repo(drop_root / "x" / "d", drop_root, bad)
        assert any("单段名" in v for v in violations), bad
    # 合法形状 + 位置一致 + 在 drop-root 下 → 无违规
    ok = drop_root / "evernight" / "d"
    assert engine_drop.check_repo(ok, drop_root, "evernight") == []


def test_drop_outside_drop_root_is_rejected(repo_env, tmp_path, monkeypatch, capsys):
    """R3 复审 P3-1：drop-root 包含判定要有自己的钉子（删掉检查必须红）。"""
    outside = tmp_path / "elsewhere" / REPO_NAME / "outsider"
    files_dir = outside / "files" / "src"
    files_dir.mkdir(parents=True)
    (files_dir / "main.rs").write_text("x\n", encoding="utf-8")
    (outside / "MANIFEST.json").write_text(json.dumps({
        "repo": REPO_NAME, "base": "origin/master", "branch": "engine/outsider",
        "description": GOOD_DESC, "files": ["src/main.rs"],
    }), encoding="utf-8")
    rc = run_main(argv_for(repo_env, tmp_path, "gates", str(outside)), monkeypatch)
    assert rc == 1
    assert "drop-root" in capsys.readouterr().out


def test_copy_file_strict_rejects_hardlink(tmp_path):
    """R3 复审 P3-2：拷贝端的 st_nlink 复核要有独立钉子（不只靠门禁侧）。"""
    import pytest as _pytest
    real = tmp_path / "origin.txt"
    real.write_text("TOPSECRET", encoding="utf-8")
    link = tmp_path / "hard.txt"
    os.link(real, link)
    dst = tmp_path / "out.txt"
    with _pytest.raises((RuntimeError, OSError)):
        engine_drop.copy_file_strict(link, dst)
    assert not dst.exists()

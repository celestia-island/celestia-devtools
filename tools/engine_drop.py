#!/usr/bin/env python3
"""engine_drop.py — 宿主机侧「引擎回写 → 分支 → PR」消费端。

自迭代引擎（scepter 容器）产出的修订**不直接进任何主 checkout**：容器经 evernight
下行把成果写进宿主机暂存区（drop 目录），本工具消费 drop，在宿主机上完成
鉴权、分支与 PR——**凭据永远不出宿主机**（用户 2026-10-08 裁决：容器只思考，
认证与合并在宿主机）。

Drop 目录布局::

    <drop-root>/<repo>/<drop-id>/
      MANIFEST.json    {"repo","base","branch","description","files":[repo 相对路径]}
      files/<repo 相对路径>

子命令::

    gates <drop-dir>                 只跑门禁（只读；不建 worktree、不写审计）
    apply <drop-dir> [--dry-run]     门禁 → 专用 worktree → 提交 → push → 开 PR
    audit [--limit N]                读审计流水

机械门（fail-closed，**全部先于任何 git 写操作**；引擎改不了自己的门——
门所在的 celestia-devtools 仓对 drop 而言是禁区）：

  G1 分支必须形如 ``engine/<id>``，且本地/远程都不存在同名分支（绝不复用、绝不 master）
  G2 files 必须是 repo 相对路径：拒绝绝对路径、``..``、任何组件为符号链接、``.git/**``
  G3 禁区路径：``AGENTS.md``/``PLAN.md``/``CLAUDE.md``/``.github/workflows/**``/
     ``*.env``/``_ledger/**``/``*_credentials*``/``.git/**``（治理面与凭据面）
  G4 量闸：文件数 ≤ 200、变更行（增+删）≤ 2000（以 ``git diff --cached --numstat`` 计）
  G5 base 只接受 ``origin/*`` 下真实存在的引用
  G6 description 必须形如 ``<gitmoji> 英文陈述句.``（与人类提交同一条格式规则）
  G7 worktree 从干净状态开始；除 drop 声明的文件外不得有任何改动

绝不：push master、merge、force-push、删分支。合并永远由人做。

Python stdlib only（3.9+）。
"""

from __future__ import annotations

import argparse
import json
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import List, Optional, Sequence, Tuple

EXIT_OK = 0
EXIT_FAILURE = 1
EXIT_USAGE = 2

MAX_FILES = 200
MAX_LINES = 2000

BRANCH_RE = re.compile(r"^engine/[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
# <gitmoji> 英文陈述句. —— 与仓规则同形：一个 emoji、空格、大写开头、句号收尾、无冒号前缀
DESC_RE = re.compile(
    r"^[\U0001F300-\U0001FAFF\u2600-\u27BF\u2190-\u21FF\u2B00-\u2BFF]"
    r" [A-Z][^.]*\.$"
)

FORBIDDEN_EXACT = ("AGENTS.md", "PLAN.md", "CLAUDE.md")
FORBIDDEN_PREFIXES = (
    ".github/workflows/",
    "_ledger/",
    ".git/",
)
FORBIDDEN_SUFFIXES = (".env",)


def die(message: str) -> None:
    print(f"engine_drop: {message}", file=sys.stderr)
    raise SystemExit(EXIT_USAGE)


def sh(cmd: Sequence[str], *, cwd: Optional[Path] = None, check: bool = True):
    """Run a command; on failure raise with the captured output."""
    proc = subprocess.run(
        list(cmd), cwd=str(cwd) if cwd else None, capture_output=True, text=True
    )
    if check and proc.returncode != 0:
        raise RuntimeError(
            f"命令失败 ({' '.join(cmd[:3])}…): rc={proc.returncode}\n"
            f"  stdout: {proc.stdout.strip()[:400]}\n"
            f"  stderr: {proc.stderr.strip()[:400]}"
        )
    return proc


def sh_ok(cmd: Sequence[str], *, cwd: Optional[Path] = None) -> bool:
    try:
        sh(cmd, cwd=cwd)
        return True
    except RuntimeError:
        return False


# ── 门禁 ─────────────────────────────────────────────────────────────────────


def forbidden_path(rel: str) -> Optional[str]:
    """返回违禁原因；干净路径返回 None。rel 必须已通过 G2 的形状检查。"""
    parts = rel.split("/")
    if rel in FORBIDDEN_EXACT:
        return f"禁区文件（治理面）：{rel}"
    for prefix in FORBIDDEN_PREFIXES:
        if rel == prefix.rstrip("/") or rel.startswith(prefix):
            return f"禁区前缀：{rel}（{prefix}…）"
    if rel.endswith(FORBIDDEN_SUFFIXES):
        return f"禁区后缀：{rel}（凭据/环境面）"
    if any(part.endswith("credentials") or "credentials" in part for part in parts):
        return f"疑似凭据文件：{rel}"
    return None


def check_paths(files_dir: Path, files: List[str]) -> List[str]:
    """G2+G3：路径形状与禁区，逐文件核对磁盘真实存在且非符号链接。"""
    violations: List[str] = []
    seen = set()
    for rel in files:
        if rel in seen:
            violations.append(f"重复列出：{rel}")
            continue
        seen.add(rel)
        if not rel or rel.startswith("/") or rel.startswith("~"):
            violations.append(f"必须是与仓库根相对的路径：{rel!r}")
            continue
        if ".." in rel.split("/"):
            violations.append(f"路径不得包含 ..：{rel}")
            continue
        if "\\" in rel or "\0" in rel:
            violations.append(f"路径含非法字符：{rel!r}")
            continue
        reason = forbidden_path(rel)
        if reason:
            violations.append(reason)
            continue
        src = files_dir / rel
        if not src.is_file():
            violations.append(f"files/ 下缺文件：{rel}")
            continue
        if src.is_symlink():
            violations.append(f"拒绝符号链接：{rel}")
            continue
        # 父目录任一组件是符号链接同样拒绝（防穿越）
        cur = files_dir
        for part in rel.split("/")[:-1]:
            cur = cur / part
            if cur.is_symlink():
                violations.append(f"父目录是符号链接：{rel}（{cur.name}）")
                break
    return violations


def check_branch(repo: Path, branch: str) -> List[str]:
    """G1：形状 + 本地/远端都无同名分支。"""
    if not BRANCH_RE.match(branch):
        return [f"分支名必须形如 engine/<id>（拒绝 master/裸名/特殊字符）：{branch!r}"]
    for ref in (f"refs/heads/{branch}", f"refs/remotes/origin/{branch}"):
        if sh_ok(["git", "show-ref", "--verify", "--quiet", ref], cwd=repo):
            return [f"分支已存在，绝不复用（引擎分支一次性）：{branch}"]
    return []


def check_base(repo: Path, base: str) -> List[str]:
    """G5：base 只能是 origin/* 下真实存在的引用。"""
    if not base.startswith("origin/"):
        return [f"base 必须形如 origin/<分支>：{base!r}"]
    if not sh_ok(["git", "show-ref", "--verify", "--quiet", f"refs/remotes/{base}"], cwd=repo):
        return [f"base 引用不存在（先 fetch）：{base}"]
    return []


def check_description(description: str) -> List[str]:
    """G6：提交标题格式与人类同一条规则。"""
    if not description or not DESC_RE.match(description):
        return [
            "description 必须形如「<gitmoji> 英文陈述句.」（无冒号前缀、句号收尾）："
            f"{description!r}"
        ]
    return []


def load_manifest(drop: Path) -> dict:
    path = drop / "MANIFEST.json"
    if not path.is_file():
        die(f"drop 缺 MANIFEST.json：{drop}")
    try:
        manifest = json.loads(path.read_text(encoding="utf-8"))
    except ValueError as exc:
        die(f"MANIFEST.json 不是合法 JSON：{exc}")
    for key in ("repo", "base", "branch", "description", "files"):
        if key not in manifest:
            die(f"MANIFEST.json 缺字段：{key}")
    if not isinstance(manifest["files"], list) or not manifest["files"]:
        die("MANIFEST.json 的 files 必须是非空列表")
    if len(manifest["files"]) > MAX_FILES:
        die(f"文件数超上限：{len(manifest['files'])} > {MAX_FILES}")
    return manifest


def run_gates(drop: Path, repo_root: Path) -> Tuple[Path, dict, List[str]]:
    """全部只读门禁。返回 (repo, manifest, violations)。"""
    manifest = load_manifest(drop)
    repo = repo_root / manifest["repo"]
    if not (repo / ".git").exists():
        return repo, manifest, [f"不是 git 仓：{repo}"]
    violations: List[str] = []
    violations += check_paths(drop / "files", list(manifest["files"]))
    violations += check_branch(repo, str(manifest["branch"]))
    violations += check_base(repo, str(manifest["base"]))
    violations += check_description(str(manifest["description"]))
    return repo, manifest, violations


# ── apply ────────────────────────────────────────────────────────────────────


def drop_digest(drop: Path) -> str:
    """MANIFEST + files 内容的 sha256 —— 审计流水里钉住「这批改动的身份」。"""
    import hashlib

    digest = hashlib.sha256()
    digest.update((drop / "MANIFEST.json").read_bytes())
    for path in sorted(p for p in (drop / "files").rglob("*") if p.is_file()):
        digest.update(path.relative_to(drop).as_posix().encode())
        digest.update(path.read_bytes())
    return digest.hexdigest()


def worktree_path(wt_root: Path, repo_name: str, branch: str) -> Path:
    safe = re.sub(r"[^A-Za-z0-9._-]", "_", f"{repo_name}-{branch}")
    return wt_root / repo_name / safe


def audit_append(audit_file: Path, record: dict) -> None:
    audit_file.parent.mkdir(parents=True, exist_ok=True)
    with audit_file.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n")


def open_pr(repo: Path, base: str, branch: str, title: str, body: str) -> str:
    proc = sh(["gh", "pr", "create", "--base", base, "--head", branch,
               "--title", title, "--body", body], cwd=repo)
    return proc.stdout.strip().splitlines()[-1] if proc.stdout.strip() else ""


PR_BODY_TEMPLATE = """### 引擎产出（自迭代回写）

- **来源**：drop `{drop_id}`（repo `{repo}`）
- **drop 摘要**：`{digest}`
- **base**：`{base}`
- **机械门**：G1 分支前缀/不复用 · G2 路径形状/无符号链接 · G3 治理与凭据禁区 ·
  G4 量闸（≤{max_files} 文件 / ≤{max_lines} 行）· G5 base 限 origin/* · G6 标题格式 · G7 干净 worktree
- **变更**：{n_files} 文件 / {n_lines} 行（增+删）

> 本 PR 由引擎产出、宿主机侧 `engine_drop.py apply` 创建。**合并由人做**；
> 门禁只保证形状与边界，不保证正确性——评审请按常规三轮验证对待。
"""


def cmd_apply(args: argparse.Namespace) -> int:
    drop = args.drop_dir
    if not drop.is_dir():
        die(f"drop 目录不存在：{drop}")
    repo, manifest, violations = run_gates(drop, args.repo_root)
    if violations:
        print(f"❌ 门禁未过（{len(violations)} 条），未做任何 git 操作：", file=sys.stderr)
        for violation in violations:
            print(f"  - {violation}", file=sys.stderr)
        audit_append(args.audit_file, {
            "ts": int(time.time()), "drop": drop.name, "repo": manifest.get("repo"),
            "branch": manifest.get("branch"), "outcome": "gates-rejected",
            "violations": violations, "digest": drop_digest(drop) if (drop / "files").is_dir() else None,
        })
        return EXIT_FAILURE

    branch = str(manifest["branch"])
    base = str(manifest["base"])
    description = str(manifest["description"])
    wt = worktree_path(args.wt_root, manifest["repo"], branch)
    if wt.exists():
        print(f"❌ worktree 路径已占用（先清理）：{wt}", file=sys.stderr)
        return EXIT_FAILURE

    created = False
    try:
        sh(["git", "worktree", "add", str(wt), "-b", branch, base], cwd=repo)
        created = True
        # G7 前半：新 worktree 必须干净
        status = sh(["git", "status", "--porcelain"], cwd=wt).stdout.strip()
        if status:
            raise RuntimeError(f"新 worktree 不干净（基线污染）：{status}")

        for rel in manifest["files"]:
            src = drop / "files" / rel
            dst = wt / rel
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(src, dst)

        sh(["git", "add", "--", *manifest["files"]], cwd=wt)

        # G7 后半：暂存区必须恰好是声明的文件
        staged = [
            line.strip() for line in sh(["git", "diff", "--cached", "--name-only"], cwd=wt).stdout.splitlines()
            if line.strip()
        ]
        extra = [p for p in staged if p not in set(manifest["files"])]
        missing = [p for p in manifest["files"] if p not in set(staged)]
        if extra or missing:
            raise RuntimeError(f"暂存区与声明不一致：多 {extra} / 少 {missing}")

        # G4：量闸以真实 diff 计
        numstat = sh(["git", "diff", "--cached", "--numstat"], cwd=wt).stdout.splitlines()
        n_files = len(numstat)
        n_lines = 0
        for line in numstat:
            added, deleted, _ = line.split("\t", 2)
            for value in (added, deleted):
                n_lines += int(value) if value.isdigit() else 0
        if n_files > MAX_FILES or n_lines > MAX_LINES:
            raise RuntimeError(
                f"量闸超限：{n_files} 文件 / {n_lines} 行（上限 {MAX_FILES}/{MAX_LINES}）"
            )

        digest = drop_digest(drop)
        body = PR_BODY_TEMPLATE.format(
            drop_id=drop.name, repo=manifest["repo"], digest=digest, base=base,
            max_files=MAX_FILES, max_lines=MAX_LINES, n_files=n_files, n_lines=n_lines,
        )
        print(f"✅ 门禁全过：{n_files} 文件 / {n_lines} 行 → {branch}（base {base}）")

        if args.dry_run:
            print("（dry-run：到 diff 为止，不提交、不 push、不开 PR）")
            audit_append(args.audit_file, {
                "ts": int(time.time()), "drop": drop.name, "repo": manifest["repo"],
                "branch": branch, "outcome": "dry-run", "digest": digest,
                "n_files": n_files, "n_lines": n_lines,
            })
            return EXIT_OK

        sh(["git", "commit", "-m", description], cwd=wt)
        sh(["git", "push", "-u", "origin", branch], cwd=wt)
        url = open_pr(repo, base[len("origin/"):], branch, description, body)
        print(f"✅ 已推送并开 PR：{url or '（gh 无输出，去仓里看）'}")
        audit_append(args.audit_file, {
            "ts": int(time.time()), "drop": drop.name, "repo": manifest["repo"],
            "branch": branch, "outcome": "pr-opened", "digest": digest,
            "n_files": n_files, "n_lines": n_lines, "pr": url,
        })
        return EXIT_OK
    except (RuntimeError, OSError, ValueError) as exc:
        print(f"❌ apply 失败：{exc}", file=sys.stderr)
        audit_append(args.audit_file, {
            "ts": int(time.time()), "drop": drop.name, "repo": manifest.get("repo"),
            "branch": manifest.get("branch"), "outcome": "failed", "error": str(exc)[:500],
        })
        return EXIT_FAILURE
    finally:
        if created:
            sh(["git", "worktree", "remove", "--force", str(wt)], cwd=repo, check=False)


def cmd_gates(args: argparse.Namespace) -> int:
    drop = args.drop_dir
    if not drop.is_dir():
        die(f"drop 目录不存在：{drop}")
    _, manifest, violations = run_gates(drop, args.repo_root)
    if violations:
        print(f"❌ {len(violations)} 条违禁：")
        for violation in violations:
            print(f"  - {violation}")
        return EXIT_FAILURE
    print(f"✅ 门禁全过：{manifest['repo']} → {manifest['branch']}（{len(manifest['files'])} 文件）")
    return EXIT_OK


def cmd_audit(args: argparse.Namespace) -> int:
    if not args.audit_file.is_file():
        print(f"（暂无审计流水：{args.audit_file}）")
        return EXIT_OK
    lines = args.audit_file.read_text(encoding="utf-8").splitlines()
    for line in lines[-args.limit:]:
        print(line)
    return EXIT_OK


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="engine_drop.py",
        description="宿主机侧引擎回写消费端：drop → 机械门 → worktree → engine/* 分支 → PR",
    )
    parser.add_argument("--repo-root", type=Path, default=Path("/mnt/codespace"))
    parser.add_argument("--wt-root", type=Path, default=Path("/mnt/work/engine-wt"))
    parser.add_argument("--audit-file", type=Path, default=Path("/mnt/work/engine-drop/audit.jsonl"))
    sub = parser.add_subparsers(dest="command", metavar="{gates,apply,audit}")

    p_gates = sub.add_parser("gates", help="只跑门禁（只读）")
    p_gates.add_argument("drop_dir", type=Path)
    p_gates.set_defaults(func=cmd_gates)

    p_apply = sub.add_parser("apply", help="门禁 + worktree + 提交 + push + PR")
    p_apply.add_argument("drop_dir", type=Path)
    p_apply.add_argument("--dry-run", action="store_true",
                         help="到 diff 为止：不提交、不 push、不开 PR")
    p_apply.set_defaults(func=cmd_apply)

    p_audit = sub.add_parser("audit", help="读审计流水")
    p_audit.add_argument("--limit", type=int, default=20)
    p_audit.set_defaults(func=cmd_audit)
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = build_parser().parse_args(list(sys.argv[1:] if argv is None else argv))
    func = getattr(args, "func", None)
    if func is None:
        build_parser().print_help()
        return EXIT_USAGE
    return func(args)


if __name__ == "__main__":
    sys.exit(main())

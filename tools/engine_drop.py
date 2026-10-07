#!/usr/bin/env python3
"""engine_drop.py — 宿主机侧「引擎回写 → 分支 → PR」消费端。

自迭代引擎（scepter 容器）产出的修订**不直接进任何主 checkout**：容器经 evernight
下行把成果写进宿主机暂存区（drop 目录），本工具消费 drop，在宿主机上完成
鉴权、分支与 PR——**凭据永远不出宿主机**（2026-10-08 裁决：容器只思考，
认证与合并在宿主机；引擎自产出也按同一规则合并，合并永远由人做）。

Drop 目录布局（repo 与 drop 的物理位置绑定，见 G0）::

    <drop-root>/<repo>/<drop-id>/
      MANIFEST.json    {"repo","base","branch","description","files":[repo 相对路径]}
      files/<repo 相对路径>

子命令::

    gates <drop-dir>                 只跑门禁（只读；不建 worktree）
    apply <drop-dir> [--dry-run]     门禁 → 专用 worktree → 提交 → push → 开 PR
    audit [--limit N]                读审计流水

机械门（fail-closed，**全部先于任何 git 写操作**）：

  G0 repo 指针绑定：repo 必须是单段名 ``[A-Za-z0-9][A-Za-z0-9._-]*``，且与 drop 的
     物理位置一致（``drop.parent.name == repo``、drop 必须在 drop-root 之下）——
     R3 实证过 ``repo="../别的仓"`` 能让七道门全绿后把分支推进**门所在的仓**
  G1 分支必须形如 ``engine/<id>`` 且过 ``git check-ref-format``；本地/远端已存在即拒
  G2 路径必须 repo 相对且正规化：拒绝绝对路径、``..``、空/``.`` 组件、符号链接
     （含父目录组件）、**硬链接（st_nlink>1）**；拷贝用 ``O_NOFOLLOW`` 防 TOCTOU
  G3 禁区（NFC 归一 + 大小写折叠 + **任意层级**）：``AGENTS.md``/``PLAN.md``/
     ``CLAUDE.md``、``.git`` 组件、``.github/workflows``、``_ledger``、
     dotenv 家族（``.env``/``*.env``/``.env.*``）、含 ``credential`` 的文件名
  G4 量闸：文件数 ≤ 200、变更行 ≤ 2000、单文件 ≤ 1MiB、总量 ≤ 10MiB
     （行数对二进制/单行文本会塌缩，字节闸补位）
  G5 base 只接受 ``origin/*`` 下真实存在的引用
  G6 description 形如 ``<gitmoji> 英文陈述句.``（与人类同一条格式规则）
  G7 worktree 从干净状态开始；暂存区与 MANIFEST 声明逐路径一致

绝不：push master、merge、force-push、删分支。合并永远由人做。

Python stdlib only（3.9+）。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import stat as stat_mod
import subprocess
import sys
import time
import unicodedata
from pathlib import Path
from typing import List, Optional, Sequence, Tuple

EXIT_OK = 0
EXIT_FAILURE = 1
EXIT_USAGE = 2

MAX_FILES = 200
MAX_LINES = 2000
MAX_FILE_BYTES = 1_000_000
MAX_TOTAL_BYTES = 10_000_000

REPO_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
BRANCH_RE = re.compile(r"^engine/[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
DESC_RE = re.compile(
    r"^[\U0001F300-\U0001FAFF\u2600-\u27BF\u2190-\u21FF\u2B00-\u2BFF]" r" [A-Z].+\.$"
)

GOVERNANCE_NAMES = {"agents.md", "plan.md", "claude.md"}  # casefolded 比较


def die(message: str) -> None:
    print(f"engine_drop: {message}", file=sys.stderr)
    raise SystemExit(EXIT_USAGE)


def sh(cmd: Sequence[str], *, cwd: Optional[Path] = None, check: bool = True):
    """Run a command; on failure raise with the captured output (4KiB，根因常在尾部)。"""
    proc = subprocess.run(
        list(cmd), cwd=str(cwd) if cwd else None, capture_output=True, text=True
    )
    if check and proc.returncode != 0:
        raise RuntimeError(
            f"命令失败 ({' '.join(cmd[:3])}…): rc={proc.returncode}\n"
            f"  stdout: {proc.stdout.strip()[:4000]}\n"
            f"  stderr: {proc.stderr.strip()[:4000]}"
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
    """G3：禁区匹配——NFC 归一 + 大小写折叠 + 任意层级组件。返回违禁原因或 None。

    R3 实证过旧实现的绕过矩阵：``foo/AGENTS.md``（嵌套治理文件）、``AGENTS.MD``
    （大小写）、``.env.local``（dotenv 家族）、``my-credential.txt``（单数）、
    ``deploy.ENV``——这里全部按折叠后的组件匹配。
    """
    norm = unicodedata.normalize("NFC", rel)
    parts = [p.casefold() for p in norm.split("/")]
    for part in parts:
        if part in GOVERNANCE_NAMES:
            return f"治理面禁区文件（任意层级）：{rel}"
        if part == ".git":
            return f".git 组件（任意层级）：{rel}"
        if "credential" in part:
            return f"疑似凭据文件：{rel}"
    for part in parts:
        if part == ".env" or part.endswith(".env") or part.startswith(".env."):
            return f"环境/凭据面（dotenv 家族）：{rel}"
    if "_ledger" in parts:
        return f"账本禁区：{rel}"
    for i in range(len(parts) - 1):
        if parts[i] == ".github" and parts[i + 1] == "workflows":
            return f"CI 面禁区（.github/workflows 任意层级）：{rel}"
    return None


def check_paths(files_dir: Path, files: List[str]) -> List[str]:
    """G2+G3+G4(字节)：路径形状、禁区、符号/硬链接、字节上限。"""
    violations: List[str] = []
    seen = set()
    total_bytes = 0
    for rel in files:
        if rel in seen:
            violations.append(f"重复列出：{rel}")
            continue
        seen.add(rel)
        if not rel or rel.startswith("/") or rel.startswith("~"):
            violations.append(f"必须是与仓库根相对的路径：{rel!r}")
            continue
        if "\\" in rel or "\0" in rel:
            violations.append(f"路径含非法字符：{rel!r}")
            continue
        components = rel.split("/")
        if ".." in components or "" in components or "." in components:
            violations.append(f"路径必须正规化（拒绝 .. / 空组件 / ./ 前缀）：{rel!r}")
            continue
        reason = forbidden_path(rel)
        if reason:
            violations.append(reason)
            continue
        src = files_dir / rel
        try:
            st = src.lstat()
        except OSError:
            violations.append(f"files/ 下缺文件：{rel}")
            continue
        if stat_mod.S_ISLNK(st.st_mode):
            violations.append(f"拒绝符号链接：{rel}")
            continue
        if not src.is_file():
            violations.append(f"不是普通文件：{rel}")
            continue
        if st.st_nlink > 1:
            violations.append(f"拒绝硬链接（st_nlink={st.st_nlink}）：{rel}")
            continue
        if st.st_size > MAX_FILE_BYTES:
            violations.append(f"单文件超 {MAX_FILE_BYTES} 字节：{rel}（{st.st_size}）")
            continue
        total_bytes += st.st_size
        # 父目录任一组件是符号链接同样拒绝（防穿越）
        cur = files_dir
        for part in components[:-1]:
            cur = cur / part
            if cur.is_symlink():
                violations.append(f"父目录是符号链接：{rel}（{cur.name}）")
                break
    if total_bytes > MAX_TOTAL_BYTES:
        violations.append(f"总量超 {MAX_TOTAL_BYTES} 字节：{total_bytes}")
    return violations


def copy_file_strict(src: Path, dst: Path) -> None:
    """O_NOFOLLOW 打开 + fstat 复核：检查与拷贝之间不允许换成链接、不允许硬链接。"""
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(src, flags)
    try:
        st = os.fstat(fd)
        if not stat_mod.S_ISREG(st.st_mode):
            raise RuntimeError(f"不是普通文件（TOCTOU）：{src}")
        if st.st_nlink > 1:
            raise RuntimeError(f"硬链接（TOCTOU，st_nlink={st.st_nlink}）：{src}")
        with os.fdopen(fd, "rb", closefd=False) as reader:
            dst.parent.mkdir(parents=True, exist_ok=True)
            with open(dst, "wb") as writer:
                shutil.copyfileobj(reader, writer)
    finally:
        os.close(fd)


def check_repo(drop: Path, drop_root: Path, repo_name: str) -> List[str]:
    """G0：repo 指针绑定——R3 的 P0 对症件。"""
    violations: List[str] = []
    if not REPO_RE.match(repo_name):
        violations.append(
            f"repo 必须是单段名 [A-Za-z0-9][A-Za-z0-9._-]*（拒绝路径/绝对/前导点）：{repo_name!r}"
        )
    if drop.parent.name != repo_name:
        violations.append(f"repo 与 drop 物理位置不符：{drop.parent.name} ≠ {repo_name}")
    try:
        drop.parent.resolve().relative_to(drop_root.resolve())
    except ValueError:
        violations.append(f"drop 不在 drop-root 之下：{drop.parent} ⊄ {drop_root}")
    return violations


def check_branch(repo: Path, branch: str) -> List[str]:
    """G1：形状 + git 自己的 ref 规则 + 本地/远端都无同名分支。"""
    if not BRANCH_RE.match(branch):
        return [f"分支名必须形如 engine/<id>（拒绝 master/裸名/特殊字符）：{branch!r}"]
    if not sh_ok(["git", "check-ref-format", "--branch", branch]):
        return [f"git 拒绝该分支名（check-ref-format）：{branch}"]
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
    """G6：提交标题格式与人类同一条规则（gitmoji + 大写陈述句 + 句号收尾）。"""
    if not description or not DESC_RE.match(description):
        return [
            "description 必须形如「<gitmoji> 英文陈述句.」（句号收尾、无 type: 前缀）："
            f"{description!r}"
        ]
    return []


def load_manifest(drop: Path) -> Tuple[Optional[dict], List[str]]:
    """读 MANIFEST；畸形一律作为 violations 返回（要进审计，不能只 die）。"""
    path = drop / "MANIFEST.json"
    if not path.is_file():
        return None, [f"drop 缺 MANIFEST.json：{drop}"]
    try:
        manifest = json.loads(path.read_text(encoding="utf-8"))
    except ValueError as exc:
        return None, [f"MANIFEST.json 不是合法 JSON：{exc}"]
    if not isinstance(manifest, dict):
        return None, ["MANIFEST.json 必须是 JSON 对象"]
    violations: List[str] = []
    for key in ("repo", "base", "branch", "description", "files"):
        if key not in manifest:
            violations.append(f"MANIFEST.json 缺字段：{key}")
        elif key == "files":
            if not isinstance(manifest["files"], list) or not manifest["files"]:
                violations.append("files 必须是非空列表")
            elif not all(isinstance(item, str) for item in manifest["files"]):
                violations.append("files 的每个元素都必须是字符串")
        elif not isinstance(manifest[key], str) or not manifest[key]:
            violations.append(f"{key} 必须是非空字符串")
    if violations:
        return None, violations
    if len(manifest["files"]) > MAX_FILES:
        violations.append(f"文件数超上限：{len(manifest['files'])} > {MAX_FILES}")
    return manifest, violations


def run_gates(drop: Path, repo_root: Path, drop_root: Path) -> Tuple[Optional[Path], Optional[dict], List[str]]:
    """全部只读门禁。返回 (repo, manifest, violations)。"""
    manifest, violations = load_manifest(drop)
    if manifest is None:
        return None, None, violations
    violations += check_repo(drop, drop_root, str(manifest["repo"]))
    repo = repo_root / str(manifest["repo"])
    if not REPO_RE.match(str(manifest["repo"])) or not (repo / ".git").exists():
        return repo, manifest, violations + [f"不是 git 仓：{repo}"]
    violations += check_paths(drop / "files", list(manifest["files"]))
    violations += check_branch(repo, str(manifest["branch"]))
    violations += check_base(repo, str(manifest["base"]))
    violations += check_description(str(manifest["description"]))
    return repo, manifest, violations


# ── apply ────────────────────────────────────────────────────────────────────


def drop_digest(drop: Path) -> Optional[str]:
    """MANIFEST + files 内容的 sha256（按排序路径钉住顺序，创建序无关）。"""
    if not (drop / "MANIFEST.json").is_file():
        return None
    digest = hashlib.sha256()
    digest.update((drop / "MANIFEST.json").read_bytes())
    files_dir = drop / "files"
    if files_dir.is_dir():
        for path in sorted(
            (p for p in files_dir.rglob("*") if p.is_file()),
            key=lambda p: p.relative_to(files_dir).as_posix(),
        ):
            digest.update(path.relative_to(drop).as_posix().encode())
            digest.update(path.read_bytes())
    return digest.hexdigest()


def worktree_path(wt_root: Path, repo_name: str, branch: str) -> Path:
    safe = re.sub(r"[^A-Za-z0-9._-]", "_", f"{repo_name}-{branch}")
    return wt_root / repo_name / safe


def audit_append(audit_file: Path, record: dict) -> None:
    try:
        audit_file.parent.mkdir(parents=True, exist_ok=True)
        with audit_file.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n")
    except OSError as exc:  # 审计失败不吞掉主结果，但要响
        print(f"⚠️ 审计流水写入失败：{exc}", file=sys.stderr)


def open_pr(repo: Path, base: str, branch: str, title: str, body: str) -> str:
    proc = sh(["gh", "pr", "create", "--base", base, "--head", branch,
               "--title", title, "--body", body], cwd=repo)
    return proc.stdout.strip().splitlines()[-1] if proc.stdout.strip() else ""


PR_BODY_TEMPLATE = """### 引擎产出（自迭代回写）

- **来源**：drop `{drop_id}`（repo `{repo}`）
- **drop 摘要**：`{digest}`
- **base**：`{base}`
- **机械门**：G0 repo 绑定 · G1 分支前缀/不复用 · G2 路径形状/无链接 · G3 治理与凭据禁区
  · G4 量闸（≤{max_files} 文件 / ≤{max_lines} 行 / ≤{max_file_mb}MiB 单文件 / ≤{max_total_mb}MiB 总量）
  · G5 base 限 origin/* · G6 标题格式 · G7 干净 worktree
- **变更**：{n_files} 文件 / {n_lines} 行（增+删）

> 本 PR 由引擎产出、宿主机侧 `engine_drop.py apply` 创建。**合并由人做**；
> 门禁只保证形状与边界，不保证正确性——评审请按常规三轮验证对待。
"""


def cmd_apply(args: argparse.Namespace) -> int:
    drop = args.drop_dir
    if not drop.is_dir():
        die(f"drop 目录不存在：{drop}")
    base_record = {
        "ts": int(time.time()), "drop": drop.name, "digest": drop_digest(drop),
    }
    repo, manifest, violations = run_gates(drop, args.repo_root, args.drop_root)
    if violations:
        print(f"❌ 门禁未过（{len(violations)} 条），未做任何 git 操作：", file=sys.stderr)
        for violation in violations:
            print(f"  - {violation}", file=sys.stderr)
        audit_append(args.audit_file, {
            **base_record, "repo": manifest.get("repo") if manifest else None,
            "branch": manifest.get("branch") if manifest else None,
            "outcome": "gates-rejected", "violations": violations,
        })
        return EXIT_FAILURE

    branch = str(manifest["branch"])
    base = str(manifest["base"])
    description = str(manifest["description"])
    wt = worktree_path(args.wt_root, str(manifest["repo"]), branch)
    if wt.exists():
        print(f"❌ worktree 路径已占用（先清理）：{wt}", file=sys.stderr)
        audit_append(args.audit_file, {
            **base_record, "repo": manifest["repo"], "branch": branch,
            "outcome": "wt-occupied", "wt": str(wt),
        })
        return EXIT_FAILURE

    created = False
    pushed = False
    try:
        sh(["git", "worktree", "add", str(wt), "-b", branch, base], cwd=repo)
        created = True
        status = sh(["git", "status", "--porcelain"], cwd=wt).stdout.strip()
        if status:
            raise RuntimeError(f"新 worktree 不干净（基线污染）：{status}")

        for rel in manifest["files"]:
            copy_file_strict(drop / "files" / rel, wt / rel)

        sh(["git", "add", "--", *manifest["files"]], cwd=wt)

        staged = [
            line.strip() for line in sh(["git", "diff", "--cached", "--name-only"], cwd=wt).stdout.splitlines()
            if line.strip()
        ]
        declared = set(manifest["files"])
        extra = [p for p in staged if p not in declared]
        missing = [p for p in manifest["files"] if p not in set(staged)]
        if extra or missing:
            raise RuntimeError(f"暂存区与声明不一致：多 {extra} / 少 {missing}")

        numstat = sh(["git", "diff", "--cached", "--numstat"], cwd=wt).stdout.splitlines()
        n_files = len(numstat)
        n_lines = 0
        staged_bytes = 0
        for line in numstat:
            added, deleted, _ = line.split("\t", 2)
            for value in (added, deleted):
                n_lines += int(value) if value.isdigit() else 0
        for rel in manifest["files"]:
            staged_bytes += (drop / "files" / rel).stat().st_size
        if n_files > MAX_FILES or n_lines > MAX_LINES:
            raise RuntimeError(
                f"量闸超限：{n_files} 文件 / {n_lines} 行（上限 {MAX_FILES}/{MAX_LINES}）"
            )
        if staged_bytes > MAX_TOTAL_BYTES:
            raise RuntimeError(f"总量超限：{staged_bytes} > {MAX_TOTAL_BYTES} 字节")

        digest = drop_digest(drop) or ""
        body = PR_BODY_TEMPLATE.format(
            drop_id=drop.name, repo=manifest["repo"], digest=digest, base=base,
            max_files=MAX_FILES, max_lines=MAX_LINES,
            max_file_mb=MAX_FILE_BYTES // 1024 // 1024, max_total_mb=MAX_TOTAL_BYTES // 1024 // 1024,
            n_files=n_files, n_lines=n_lines,
        )
        print(f"✅ 门禁全过：{n_files} 文件 / {n_lines} 行 / {staged_bytes} 字节 → {branch}（base {base}）")

        if args.dry_run:
            print("（dry-run：到 diff 为止，不提交、不 push、不开 PR）")
            audit_append(args.audit_file, {
                **base_record, "repo": manifest["repo"], "branch": branch,
                "outcome": "dry-run", "n_files": n_files, "n_lines": n_lines,
                "n_bytes": staged_bytes,
            })
            return EXIT_OK

        sh(["git", "commit", "-m", description], cwd=wt)
        sh(["git", "push", "-u", "origin", branch], cwd=wt)
        pushed = True
        url = open_pr(repo, base[len("origin/"):], branch, description, body)
        print(f"✅ 已推送并开 PR：{url or '（gh 无输出，去仓里看）'}")
        audit_append(args.audit_file, {
            **base_record, "repo": manifest["repo"], "branch": branch,
            "outcome": "pr-opened", "n_files": n_files, "n_lines": n_lines,
            "pr": url,
        })
        return EXIT_OK
    except (RuntimeError, OSError, ValueError) as exc:
        print(f"❌ apply 失败：{exc}", file=sys.stderr)
        if pushed:
            print(
                f"   分支已推上远端但 PR 未开成：请人工对 {branch} 执行 "
                f"`gh pr create`（本工具按设计绝不删分支）。",
                file=sys.stderr,
            )
        audit_append(args.audit_file, {
            **base_record, "repo": manifest.get("repo"), "branch": manifest.get("branch"),
            "outcome": "pushed-no-pr" if pushed else "failed",
            "error": str(exc)[:4000],
        })
        return EXIT_FAILURE
    finally:
        if created:
            sh(["git", "worktree", "remove", "--force", str(wt)], cwd=repo, check=False)


def cmd_gates(args: argparse.Namespace) -> int:
    drop = args.drop_dir
    if not drop.is_dir():
        die(f"drop 目录不存在：{drop}")
    _, manifest, violations = run_gates(drop, args.repo_root, args.drop_root)
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
    parser.add_argument("--drop-root", type=Path, default=Path("/mnt/work/engine-drop"))
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

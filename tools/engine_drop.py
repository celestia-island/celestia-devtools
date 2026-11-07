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
    stage <worktree> --repo R --description D
                                     从脏 worktree 构造 drop（status 清单 → MANIFEST + files/；
                                     重命名取新侧、删除不进 drop、拒绝链接/目录/禁区条目）
    sweep [--ws-root P] [--apply]    巡航引擎暂存 ws-root（巡航→装配线宿主侧）：逐仓
                                      fetch → 改动/领先检测 → 描述（领先提交标题 /
                                      .drop-description 首行）→ stage → gates →
                                      （--apply）开 PR + 复位回收工作区
    apply <drop-dir> [--dry-run]     门禁 → 专用 worktree → 提交 → push → 开 PR
    audit [--limit N]                读审计流水

sweep 语义（2026-10-24 巡航化）：``<ws-root>/<repo>/`` 一级子目录是**管线自有**的
引擎暂存 worktree。描述绝不编造（无来源即 warn 跳过）；违禁/门禁失败**不回收**
工作区（留人看）；``--apply`` 且 PR 开成才 ``reset --hard origin/master`` +
``clean -fd`` 复位。drop id 内容寻址（``sweep-<diff 哈希>``）→ 同内容重复 sweep
幂等跳过。每仓一行进 audit.jsonl（``event: sweep`` 与 apply 运行记录区分）。

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
from typing import List, Sequence, Tuple

EXIT_OK = 0
EXIT_FAILURE = 1
EXIT_USAGE = 2

MAX_FILES = 200
MAX_LINES = 2000
MAX_FILE_BYTES = 1_000_000
MAX_TOTAL_BYTES = 10_000_000

REPO_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
BRANCH_RE = re.compile(r"^engine/[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
_EMOJI_CLASS = "\U0001F300-\U0001FAFF\u2600-\u27BF\u2190-\u21FF\u2B00-\u2BFF"
# 基 emoji 后可跟最多 6 个续字符（VS16 / ZWJ / 序列里的后续 emoji）——
# 🛡️(U+1F6E1+FE0F) 与 🏳️‍🌈(1F3F3 FE0F 200D 1F308) 都是生态常见标题写法；
# 首个真实 drop（selfanalysis-01）就因 VS16 被误拒过。
DESC_RE = re.compile(rf"^[{_EMOJI_CLASS}][{_EMOJI_CLASS}\uFE0F\u200D]{{0,6}} [A-Z].+\.$")

GOVERNANCE_NAMES = {"agents.md", "plan.md", "claude.md"}  # casefolded 比较

# sweep 的默认巡航根：引擎暂存 worktree（管线自有，非人类工作区）。
DEFAULT_WS_ROOT = Path("/mnt/work/engine-ws")
# sweep 的描述回落控制面文件（工作区根首行）；绝不作为内容进 drop。
DROP_DESCRIPTION_FILE = ".drop-description"

class stage_error(RuntimeError):
    """stage 的 fail-closed 拒绝：CLI（cmd_stage）转 die()；sweep 转 warn 跳过。"""

class drop_exists_error(stage_error):
    """drop 目录已存在——幂等信号（同内容处理过），sweep 视为「本轮已处理」。"""

    def __init__(self, message: str, drop: Path) -> None:
        super().__init__(message)
        self.drop = drop

def die(message: str) -> None:
    print(f"engine_drop: {message}", file = sys.stderr)
    raise SystemExit(EXIT_USAGE)

def sh(cmd: Sequence[str], *, cwd: Path | None = None, check: bool = True):
    """Run a command; on failure raise with the captured output (4KiB，根因常在尾部)。"""
    proc = subprocess.run(list(cmd), cwd = str(cwd)if cwd else None, capture_output = True, text = True)

    if check and (proc.returncode != 0):
        raise RuntimeError(f"命令失败 ({' '.join(cmd[:3])}…): rc={proc.returncode}\n" f"  stdout: {proc.stdout.strip()[:4000]}\n" f"  stderr: {proc.stderr.strip()[:4000]}")

    return proc

def sh_ok(cmd: Sequence[str], *, cwd: Path | None = None) -> bool:
    try:
        sh(cmd, cwd = cwd)
        return True
    except RuntimeError:
        return False

# ── 门禁 ─────────────────────────────────────────────────────────────────────

def forbidden_path(rel: str) -> str | None:
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
        if (part == ".env") or part.endswith(".env") or part.startswith(".env."):
            return f"环境/凭据面（dotenv 家族）：{rel}"

    if "_ledger" in parts:
        return f"账本禁区：{rel}"

    for i in range(len(parts) - 1):
        if (parts[i] == ".github") and (parts[i + 1] == "workflows"):
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

        if ("\\" in rel) or ("\0" in rel):
            violations.append(f"路径含非法字符：{rel!r}")
            continue

        components = rel.split("/")

        if (".." in components) or ("" in components) or ("." in components):
            violations.append(f"路径必须正规化（拒绝 .. / 空组件 / ./ 前缀）：{rel!r}")
            continue

        reason = forbidden_path(rel)

        if reason:
            violations.append(reason)
            continue

        # 父目录任一组件是符号链接同样拒绝（防穿越）。必须在 lstat 之前独立检查、
        # 与其它门并列——放在末尾会被前面所有 continue 短路成不可达兜底
        # （自迭代引擎自分析发现，2026-10-08：穿越目标只要再命中任一前置拒绝，
        # 该独立风险就被静默吞掉）。
        cur = files_dir
        parent_symlink = False

        for part in components[:-1]:
            cur = cur / part

            if cur.is_symlink():
                violations.append(f"父目录是符号链接：{rel}（{cur.name}）")
                parent_symlink = True
                break

        if parent_symlink:
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

        with os.fdopen(fd, "rb", closefd = False) as reader:
            dst.parent.mkdir(parents = True, exist_ok = True)

            with open(dst, "wb") as writer:
                shutil.copyfileobj(reader, writer)
    finally:
        os.close(fd)

def check_repo(drop: Path, drop_root: Path, repo_name: str) -> List[str]:
    """G0：repo 指针绑定——R3 的 P0 对症件。"""
    violations: List[str] = []

    if not REPO_RE.match(repo_name):
        violations.append(f"repo 必须是单段名 [A-Za-z0-9][A-Za-z0-9._-]*（拒绝路径/绝对/前导点）：{repo_name!r}")

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
        if sh_ok(["git", "show-ref", "--verify", "--quiet", ref], cwd = repo):
            return [f"分支已存在，绝不复用（引擎分支一次性）：{branch}"]

    return []

def check_base(repo: Path, base: str) -> List[str]:
    """G5：base 只能是 origin/* 下真实存在的引用。"""

    if not base.startswith("origin/"):
        return [f"base 必须形如 origin/<分支>：{base!r}"]

    if not sh_ok(["git", "show-ref", "--verify", "--quiet", f"refs/remotes/{base}"], cwd = repo):
        return [f"base 引用不存在（先 fetch）：{base}"]

    return []

def check_description(description: str) -> List[str]:
    """G6：提交标题格式与人类同一条规则（gitmoji + 大写陈述句 + 句号收尾）。"""

    if not description or not DESC_RE.match(description):
        return ["description 必须形如「<gitmoji> 英文陈述句.」（句号收尾、无 type: 前缀）：" f"{description!r}"]

    return []

def load_manifest(drop: Path) -> Tuple[dict | None, List[str]]:
    """读 MANIFEST；畸形一律作为 violations 返回（要进审计，不能只 die）。"""
    path = drop / "MANIFEST.json"

    if not path.is_file():
        return None, [f"drop 缺 MANIFEST.json：{drop}"]

    try:
        manifest = json.loads(path.read_text(encoding = "utf-8"))
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

def run_gates(drop: Path, repo_root: Path, drop_root: Path) -> Tuple[Path | None, dict | None, List[str]]:
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

# ── stage ────────────────────────────────────────────────────────────────────

def _git(worktree: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["git", "-C", str(worktree), *args], capture_output = True, text = True, timeout = 60)

def _unquote(path: str) -> str:
    """porcelain 对含特殊字符的路径加 C 风格引号（``"a b.txt"``）——剥掉外层引号。"""

    if (len(path) >= 2) and path.startswith('"') and path.endswith('"'):
        return path[1:-1]

    return path

def stage_worktree(worktree: Path, repo: str, description: str, drop_root: Path, *, base: str = "origin/master", branch: str | None = None, drop_id: str | None = None, content_addressed: bool = False) -> Path | None:
    """从一个脏 worktree 构造 drop，返回 drop 路径；无可搬运内容时返回 None。

    ``cmd_stage``（CLI）与 ``sweep`` 共用的内部实现。fail-closed 条件抛
    :class:`StageError`（CLI 侧转 die()，sweep 侧转 warn 跳过）；drop 目录已
    存在抛 :class:`DropExistsError`（sweep 的幂等信号）。stage 自身零 git 写
    操作。

    管线自动化的一环（2026-10-24）：引擎在干净 worktree 里写完改动后，本函数
    消费改动清单生成 ``<drop-root>/<repo>/<drop-id>/``，随后调用方跑 ``gates``
    / ``apply`` 完成门禁与 PR。

    改动集合 = ``git status --porcelain --untracked-files=all`` 的内容条目 ∪
    ``git diff --name-only <base>``（**领先提交的内容也进 drop**——引擎 SoP 会
    先以 gitmoji 提交；diff 比较的是工作树 vs base，含已暂存+未暂存），去重
    排序。磁盘上不存在的路径（删除、重命名旧侧）静默剔除——drop 只承载现存
    内容文件，删除类改动留给人工。

    边界（fail-closed，全部在 mkdir 之前，任何拒绝不留半成品 drop）：重命名取
    新侧（``旧 -> 新`` 的右侧）；``.drop-description`` 是 sweep 的控制面文件，
    绝不作为内容进 drop；G3 禁区、符号链接（防借道读取 worktree 之外的文件）、
    目录（嵌套 git 仓/子模）、status 后消失的路径 → StageError。拷贝用
    ``copy_file_strict``（O_NOFOLLOW + fstat 复核，防 TOCTOU 调包）。

    ``content_addressed=True``（sweep）时 drop id 不带时间戳（``sweep-<内容
    哈希>``）：同内容重复 stage 命中同一 drop 目录 → 幂等；默认（CLI stage）
    为 ``auto-<UTC 时间戳>-<短哈希>``。内容哈希除 diff 输出外还掺入每个条目
    的内容摘要——untracked 新文件不进 diff 输出，只看 diff 会让不同内容撞出
    同一 id。
    """

    if not worktree.is_dir():
        raise stage_error(f"worktree 不存在：{worktree}")

    if not (worktree / ".git").exists():
        raise stage_error(f"不是 git worktree：{worktree}")

    if not REPO_RE.match(repo):
        raise stage_error(f"repo 名不合法：{repo!r}")

    if drop_id and not REPO_RE.match(drop_id):
        raise stage_error(f"drop id 必须是单段名（拒绝路径穿越）：{drop_id!r}")

    if not base.startswith("origin/"):
        raise stage_error(f"base 必须形如 origin/<分支>（与 G5 同口径）：{base!r}")

    desc_violations = check_description(description)

    if desc_violations:
        raise stage_error(desc_violations[0])

    # Full refname for show-ref --verify (G5 与 check_base 同口径：短名不解析)。
    base_check = _git(worktree, "show-ref", "--verify", "--quiet", f"refs/remotes/{base}")

    if base_check.returncode != 0:
        raise stage_error(f"base 引用不存在于 worktree：{base}")

    # -uall：默认 porcelain 把 untracked 目录折叠成 `?? dir/`——新目录里的
    # 新文件会不可见，目录条目本身也无从拷贝。
    status = _git(worktree, "status", "--porcelain", "--untracked-files=all")

    if status.returncode != 0:
        raise stage_error(f"git status 失败：{status.stderr.strip()}")

    porcelain: List[str] = []
    saw_entries = False

    for line in status.stdout.splitlines():
        code, path_part = line[:2], line[3:].strip()
        saw_entries = True
        # 重命名/拷贝记作 `R  旧 -> 新`：磁盘上现存的只有新侧，旧侧视同删除。
        path = _unquote(path_part.rsplit(" -> ", 1)[-1])

        # 只搬运内容文件：删除（D）无文件可拷——含上面重命名的旧侧。
        if (code.strip() == "D") or (code[0] == "D"):
            continue

        if path and (path not in porcelain):
            porcelain.append(path)

    # 领先提交（已提交未推送）的内容：diff --name-only <base>（工作树 vs base）。
    # renames 显式关掉——rename 检测会吞掉旧侧路径，而旧侧该按删除形态出现，
    # 由下面的磁盘存在性过滤统一剔除。-z 免引号转义。
    diff_names = _git(worktree, "-c", "diff.renames=false", "diff", "--name-only", "-z", base)

    if diff_names.returncode != 0:
        raise stage_error(f"git diff --name-only {base} 失败：{diff_names.stderr.strip()}")

    diff_paths = [p for p in diff_names.stdout.split("\0") if p]
    changed: List[str] = []

    for rel in porcelain + diff_paths:
        if rel == DROP_DESCRIPTION_FILE:
            continue  # sweep 的控制面文件，绝不作为内容进 drop

        if rel in changed:
            continue

        if not (worktree / rel).exists():
            continue  # 删除/重命名旧侧：磁盘上无文件可拷

        changed.append(rel)

    changed.sort()

    if not changed:
        if saw_entries or diff_paths:
            print("改动只含删除/控制面条目：drop 只承载内容文件，没有可 stage 的改动")
        else:
            print("工作区干净：没有可 stage 的改动")

        return None

    # 条目级闸门：G3 禁区 + 形状（普通文件、非符号链接、磁盘上存在）。
    for rel in changed:
        reason = forbidden_path(rel)

        if reason:
            raise stage_error(f"{reason}——stage 拒绝把禁区文件写进 drop")

        src = worktree / rel

        try:
            st = src.lstat()
        except OSError:
            raise stage_error(f"条目在磁盘上不存在（净删除类改动？）：{rel}") from None

        if stat_mod.S_ISLNK(st.st_mode):
            raise stage_error(f"拒绝符号链接条目（drop 不承载链接，防借道读取 worktree 之外）：{rel}")

        if not stat_mod.S_ISREG(st.st_mode):
            raise stage_error(f"只搬运普通文件（目录/嵌套 git 仓/子模不进 drop）：{rel}")

    # drop id：改动内容哈希（diff + 逐条目内容摘要，见 docstring）；
    # content_addressed（sweep）不带时间戳 → 同内容幂等。
    diff = _git(worktree, "diff", base, "--", *changed)
    digest = hashlib.sha256()
    digest.update((diff.stdout or "").encode("utf-8"))

    for rel in changed:
        digest.update(rel.encode("utf-8"))
        digest.update(hashlib.sha256((worktree / rel).read_bytes()).digest())

    short = digest.hexdigest()[:8]

    if drop_id is None:
        if content_addressed:
            drop_id = f"sweep-{short}"
        else:
            stamp = time.strftime("%Y%m%d-%H%M%S", time.gmtime())
            drop_id = f"auto-{stamp}-{short}"

    drop = drop_root / repo / drop_id

    if drop.exists():
        raise drop_exists_error(f"drop 目录已存在（ refusing to overwrite ）：{drop}", drop)

    drop.mkdir(parents = True)

    try:
        for rel in changed:
            copy_file_strict(worktree / rel, drop / "files" / rel)
    except (RuntimeError, OSError) as exc:
        shutil.rmtree(drop, ignore_errors = True)  # 清掉半成品，不留误导性 drop
        raise stage_error(f"快照拷贝失败（半成品 drop 已清理）：{exc}") from exc

    manifest = {"repo": repo, "base": base, "branch": branch or f"engine/{drop_id}", "description": description, "files": changed}
    (drop / "MANIFEST.json").write_text(json.dumps(manifest, ensure_ascii = False, indent = 2) + "\n", encoding = "utf-8")
    print(f"✅ 已构造 drop：{drop}")
    print(f"   {len(changed)} 文件；下一步：")
    print(f"   python3 tools/engine_drop.py gates {drop}")
    print(f"   python3 tools/engine_drop.py apply {drop}")
    return drop

def cmd_stage(args: argparse.Namespace) -> int:
    """CLI 包装：stage_worktree 的 StageError 统一转 die()（exit 2 + stderr）。"""

    try:
        drop = stage_worktree(args.worktree, args.repo, args.description, args.drop_root, base = args.base, branch = args.branch, drop_id = args.drop_id)
    except stage_error as exc:
        die(str(exc))

    return EXIT_OK if drop is not None else EXIT_FAILURE

# ── apply ────────────────────────────────────────────────────────────────────

def drop_digest(drop: Path) -> str | None:
    """MANIFEST + files 内容的 sha256（按排序路径钉住顺序，创建序无关）。"""

    if not (drop / "MANIFEST.json").is_file():
        return None

    digest = hashlib.sha256()
    digest.update((drop / "MANIFEST.json").read_bytes())
    files_dir = drop / "files"

    if files_dir.is_dir():
        for path in sorted((p for p in files_dir.rglob("*")if p.is_file()), key = lambda p: p.relative_to(files_dir).as_posix()):
            digest.update(path.relative_to(drop).as_posix().encode())
            digest.update(path.read_bytes())

    return digest.hexdigest()

def worktree_path(wt_root: Path, repo_name: str, branch: str) -> Path:
    safe = re.sub(r"[^A-Za-z0-9._-]", "_", f"{repo_name}-{branch}")
    return wt_root / repo_name / safe

def audit_append(audit_file: Path, record: dict) -> None:
    try:
        audit_file.parent.mkdir(parents = True, exist_ok = True)

        with audit_file.open("a", encoding = "utf-8") as fh:
            fh.write(json.dumps(record, ensure_ascii = False, sort_keys = True) + "\n")
    except OSError as exc:  # 审计失败不吞掉主结果，但要响
        print(f"⚠️ 审计流水写入失败：{exc}", file = sys.stderr)

def open_pr(repo: Path, base: str, branch: str, title: str, body: str) -> str:
    proc = sh(["gh", "pr", "create", "--base", base, "--head", branch, "--title", title, "--body", body], cwd = repo)
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

def apply_drop(drop: Path, *, repo_root: Path, drop_root: Path, wt_root: Path, audit_file: Path, dry_run: bool = False) -> int:
    """门禁 → 专用 worktree → 提交 → push → 开 PR（cmd_apply 与 sweep 共用）。

    自身会再跑一遍 run_gates（幂等只读）并落自己的运行审计（dry-run /
    pr-opened / failed…）；sweep 只取退出码决定是否回收工作区。
    """
    base_record = {"ts": int(time.time()), "drop": drop.name, "digest": drop_digest(drop)}
    repo, manifest, violations = run_gates(drop, repo_root, drop_root)

    if violations:
        print(f"❌ 门禁未过（{len(violations)} 条），未做任何 git 操作：", file = sys.stderr)

        for violation in violations:
            print(f"  - {violation}", file = sys.stderr)

        audit_append(audit_file, {**base_record, "repo": manifest.get("repo")if manifest else None, "branch": manifest.get("branch")if manifest else None, "outcome": "gates-rejected", "violations": violations})
        return EXIT_FAILURE

    branch = str(manifest["branch"])
    base = str(manifest["base"])
    description = str(manifest["description"])
    wt = worktree_path(wt_root, str(manifest["repo"]), branch)

    if wt.exists():
        print(f"❌ worktree 路径已占用（先清理）：{wt}", file = sys.stderr)
        audit_append(audit_file, {**base_record, "repo": manifest["repo"], "branch": branch, "outcome": "wt-occupied", "wt": str(wt)})
        return EXIT_FAILURE

    created = False
    pushed = False

    try:
        sh(["git", "worktree", "add", str(wt), "-b", branch, base], cwd = repo)
        created = True
        status = sh(["git", "status", "--porcelain"], cwd = wt).stdout.strip()

        if status:
            raise RuntimeError(f"新 worktree 不干净（基线污染）：{status}")

        for rel in manifest["files"]:
            copy_file_strict(drop / "files" / rel, wt / rel)

        sh(["git", "add", "--", *manifest["files"]], cwd = wt)

        staged = [line.strip()for line in sh(["git", "diff", "--cached", "--name-only"], cwd = wt).stdout.splitlines()if line.strip()]
        declared = set(manifest["files"])
        extra = [p for p in staged if p not in declared]
        missing = [p for p in manifest["files"] if p not in set(staged)]

        if extra or missing:
            raise RuntimeError(f"暂存区与声明不一致：多 {extra} / 少 {missing}")

        numstat = sh(["git", "diff", "--cached", "--numstat"], cwd = wt).stdout.splitlines()
        n_files = len(numstat)
        n_lines = 0
        staged_bytes = 0

        for line in numstat:
            added, deleted, _ = line.split("\t", 2)

            for value in (added, deleted):
                n_lines += int(value) if value.isdigit() else 0

        for rel in manifest["files"]:
            staged_bytes += (drop / "files" / rel).stat().st_size

        if (n_files > MAX_FILES) or (n_lines > MAX_LINES):
            raise RuntimeError(f"量闸超限：{n_files} 文件 / {n_lines} 行（上限 {MAX_FILES}/{MAX_LINES}）")

        if staged_bytes > MAX_TOTAL_BYTES:
            raise RuntimeError(f"总量超限：{staged_bytes} > {MAX_TOTAL_BYTES} 字节")

        digest = drop_digest(drop) or ""
        body = PR_BODY_TEMPLATE.format(drop_id = drop.name, repo = manifest["repo"], digest = digest, base = base, max_files = MAX_FILES, max_lines = MAX_LINES, max_file_mb = MAX_FILE_BYTES // 1024 // 1024, max_total_mb = MAX_TOTAL_BYTES // 1024 // 1024, n_files = n_files, n_lines = n_lines)
        print(f"✅ 门禁全过：{n_files} 文件 / {n_lines} 行 / {staged_bytes} 字节 → {branch}（base {base}）")

        if dry_run:
            print("（dry-run：到 diff 为止，不提交、不 push、不开 PR）")
            audit_append(audit_file, {**base_record, "repo": manifest["repo"], "branch": branch, "outcome": "dry-run", "n_files": n_files, "n_lines": n_lines, "n_bytes": staged_bytes})
            return EXIT_OK

        sh(["git", "commit", "-m", description], cwd = wt)
        sh(["git", "push", "-u", "origin", branch], cwd = wt)
        pushed = True
        url = open_pr(repo, base[len("origin/"):], branch, description, body)
        print(f"✅ 已推送并开 PR：{url or '（gh 无输出，去仓里看）'}")
        audit_append(audit_file, {**base_record, "repo": manifest["repo"], "branch": branch, "outcome": "pr-opened", "n_files": n_files, "n_lines": n_lines, "pr": url})
        return EXIT_OK
    except (RuntimeError, OSError, ValueError) as exc:
        print(f"❌ apply 失败：{exc}", file = sys.stderr)

        if pushed:
            print(f"   分支已推上远端但 PR 未开成：请人工对 {branch} 执行 " f"`gh pr create`（本工具按设计绝不删分支）。", file = sys.stderr)

        audit_append(audit_file, {**base_record, "repo": manifest.get("repo"), "branch": manifest.get("branch"), "outcome": "pushed-no-pr" if pushed else "failed", "error": str(exc)[: 4000]})
        return EXIT_FAILURE
    finally:
        if created:
            sh(["git", "worktree", "remove", "--force", str(wt)], cwd = repo, check = False)

def cmd_apply(args: argparse.Namespace) -> int:
    drop = args.drop_dir

    if not drop.is_dir():
        die(f"drop 目录不存在：{drop}")

    return apply_drop(drop, repo_root = args.repo_root, drop_root = args.drop_root, wt_root = args.wt_root, audit_file = args.audit_file, dry_run = args.dry_run)

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

    lines = args.audit_file.read_text(encoding = "utf-8").splitlines()

    for line in lines[-args.limit:]:
        print(line)

    return EXIT_OK

# ── sweep（巡航 → 装配线宿主侧）──────────────────────────────────────────────

def _sweep_one(worktree: Path, args: argparse.Namespace) -> str:
    """处理 ws-root 下单个引擎暂存工作区；返回 outcome（每仓一行进审计）。"""
    repo = worktree.name

    def record(outcome: str, **extra) -> None:
        audit_append(args.audit_file, {"ts": int(time.time()), "event": "sweep", "repo": repo, "outcome": outcome, **extra})

    if not (worktree / ".git").exists():
        print(f"⚠️ {repo}: 不是 git worktree/checkout，跳过", file = sys.stderr)
        record("not-git")
        return "not-git"

    if not REPO_RE.match(repo):
        print(f"⚠️ {repo}: 目录名不是合法单段仓名，跳过", file = sys.stderr)
        record("invalid-name")
        return "invalid-name"

    # fetch 失败只废本轮该仓（离线/远端变动不中断整轮巡航）。
    fetch = _git(worktree, "fetch", "--quiet", "origin", "master")

    if fetch.returncode != 0:
        print(f"⚠️ {repo}: git fetch origin master 失败，本轮跳过：" f"{fetch.stderr.strip()[:200]}", file = sys.stderr)
        record("fetch-failed", error = fetch.stderr.strip()[:2000])
        return "fetch-failed"

    status = _git(worktree, "status", "--porcelain", "--untracked-files=all")

    if status.returncode != 0:
        print(f"⚠️ {repo}: git status 失败，本轮跳过：{status.stderr.strip()[:200]}", file = sys.stderr)
        record("status-failed", error = status.stderr.strip()[:2000])
        return "status-failed"

    # 引擎 SoP：改动可能未提交（porcelain 脏）也可能已以 gitmoji 提交（领先
    # origin/master）——两种形态都算「有待收的改动」。
    ahead = _git(worktree, "log", "--oneline", "origin/master..HEAD")
    has_ahead = (ahead.returncode == 0) and bool(ahead.stdout.strip())

    if not status.stdout.strip() and not has_ahead:
        print(f"ℹ️ {repo}: 干净，跳过")
        record("clean")
        return "clean"

    # 描述来源（绝不编造）：领先提交标题 → 工作区根 .drop-description 首行 →
    # 无来源即跳过。标题必须过 G6（与人类提交同一条规则）。
    description: str | None = None

    if has_ahead:
        subject = _git(worktree, "log", "--format=%s", "-1", "origin/master..HEAD")

        if subject.returncode == 0:
            lines = subject.stdout.strip().splitlines()

            if lines:
                description = lines[0].strip()

    if description is None:
        desc_file = worktree / DROP_DESCRIPTION_FILE

        if desc_file.is_file():
            first_lines = desc_file.read_text(encoding = "utf-8", errors = "replace").splitlines()

            if first_lines and first_lines[0].strip():
                description = first_lines[0].strip()

    if not description:
        print(f"⚠️ {repo}: 有改动但拿不到描述（领先提交标题 / " f"{DROP_DESCRIPTION_FILE} 首行），跳过——绝不编造描述", file = sys.stderr)
        record("skipped-no-description")
        return "skipped-no-description"

    if check_description(description):
        print(f"⚠️ {repo}: 描述未过 G6（<gitmoji> 英文陈述句.）：{description!r}，跳过", file = sys.stderr)
        record("skipped-bad-description", description = description[:200])
        return "skipped-bad-description"

    # stage：与 CLI stage 同一内部实现；content_addressed → 同内容幂等。
    try:
        drop = stage_worktree(worktree, repo, description, args.drop_root, content_addressed = True)
    except drop_exists_error as exc:
        print(f"ℹ️ {repo}: 同内容 drop 已存在（{exc.drop.name}），本轮已处理，跳过")
        record("already-staged", drop = exc.drop.name)
        return "already-staged"
    except stage_error as exc:
        # 违禁（禁区/链接/目录…）→ warn 跳过，**不回收**工作区——留人看。
        print(f"⚠️ {repo}: stage 拒绝，工作区保留待人工：{exc}", file = sys.stderr)
        record("gates-failed", error = str(exc)[:2000])
        return "gates-failed"

    if drop is None:
        if has_ahead:
            print(f"⚠️ {repo}: 领先提交只含删除/控制面条目（删除类改动留给人工），跳过", file = sys.stderr)
            record("skipped-deletions-only")
            return "skipped-deletions-only"

        print(f"ℹ️ {repo}: 无可搬运的内容改动，跳过")
        record("clean")
        return "clean"

    # 只读门禁：全过才继续；违禁 → warn 跳过且不回收（同上，留人看）。
    _, manifest, violations = run_gates(drop, args.repo_root, args.drop_root)

    if violations:
        print(f"⚠️ {repo}: 门禁未过（{len(violations)} 条），工作区保留待人工", file = sys.stderr)

        for violation in violations[:5]:
            print(f"   - {violation}", file = sys.stderr)

        record("gates-failed", drop = drop.name, violations = violations[:10])
        return "gates-failed"

    if not args.apply:
        print(f"✅ {repo}: 已 stage（dry——未开 PR、未回收）：{drop.name}")
        record("staged", drop = drop.name, branch = str(manifest["branch"]))
        return "staged"

    # apply 内部会再跑一遍 gates + 落自己的运行审计（pr-opened/failed…）；
    # sweep 只取退出码决定回收与否。
    if apply_drop(drop, repo_root = args.repo_root, drop_root = args.drop_root, wt_root = args.wt_root, audit_file = args.audit_file) != EXIT_OK:
        print(f"⚠️ {repo}: apply 未成功，工作区保留待人工", file = sys.stderr)
        record("apply-failed", drop = drop.name)
        return "apply-failed"

    # 回收：ws-root 下是**管线自有**的暂存 worktree（引擎写一轮、sweep 收一轮），
    # PR 开成后复位到 origin/master 基线并清未跟踪文件，供引擎下一轮复用——
    # 这与人类 worktree 的只读/新建纪律无关：对象本就是自动化暂存区，不是人的
    # 工作区。失败（PR 未开 / apply 出错）一律不回收，留人看。
    reset = _git(worktree, "reset", "--hard", "origin/master")
    clean = _git(worktree, "clean", "-fd")

    if (reset.returncode != 0) or (clean.returncode != 0):
        print(f"⚠️ {repo}: PR 已开但工作区回收失败，需人工复位", file = sys.stderr)
        record("recycle-failed", drop = drop.name, error = (reset.stderr or clean.stderr).strip()[: 2000])
        return "recycle-failed"

    print(f"✅ {repo}: PR 已开（drop {drop.name}），工作区已回收至 origin/master")
    record("pr-opened", drop = drop.name, branch = str(manifest["branch"]))
    return "pr-opened"

def cmd_sweep(args: argparse.Namespace) -> int:
    """巡航一轮 ws-root：逐仓收集改动→drop→（--apply）PR；整轮恒 exit 0。

    单仓的任何意外（含未预期异常）都不中断整轮；逐仓结果以审计流水
    （event: sweep）为准。只有 ws-root 不存在才是用法错误（die）。
    """
    ws_root: Path = args.ws_root

    if not ws_root.is_dir():
        die(f"ws-root 不存在：{ws_root}")

    outcomes: List[str] = []

    for entry in sorted(p for p in ws_root.iterdir() if p.is_dir()):
        try:
            outcomes.append(_sweep_one(entry, args))
        except Exception as exc:  # 单仓意外不中断整轮（审计留痕）
            print(f"⚠️ {entry.name}: sweep 未预期的失败：{exc}", file = sys.stderr)
            audit_append(args.audit_file, {"ts": int(time.time()), "event": "sweep", "repo": entry.name, "outcome": "error", "error": str(exc)[: 4000]})
            outcomes.append("error")

    if not outcomes:
        print(f"ℹ️ ws-root 下没有一级子目录：{ws_root}")
        return EXIT_OK

    summary = " / ".join(f"{name}×{outcomes.count(name)}" for name in sorted(set(outcomes)))
    print(f"ℹ️ sweep 本轮 {len(outcomes)} 仓：{summary}（详见审计流水）")
    return EXIT_OK

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog = "engine_drop.py", description = "宿主机侧引擎回写消费端：drop → 机械门 → worktree → engine/* 分支 → PR")
    parser.add_argument("--repo-root", type = Path, default = Path("/mnt/codespace"))
    parser.add_argument("--wt-root", type = Path, default = Path("/mnt/work/engine-wt"))
    parser.add_argument("--drop-root", type = Path, default = Path("/mnt/work/engine-drop"))
    parser.add_argument("--audit-file", type = Path, default = Path("/mnt/work/engine-drop/audit.jsonl"))
    sub = parser.add_subparsers(dest = "command", metavar = "{stage,sweep,gates,apply,audit}")

    p_gates = sub.add_parser("gates", help = "只跑门禁（只读）")
    p_gates.add_argument("drop_dir", type = Path)
    p_gates.set_defaults(func = cmd_gates)

    p_sweep = sub.add_parser("sweep", help = "巡航引擎暂存 ws-root：改动→drop→门禁→（--apply）PR+回收")
    p_sweep.add_argument("--ws-root", type = Path, default = DEFAULT_WS_ROOT, help = "引擎暂存工作区根（一级子目录 = 各仓工作区，" f"默认 {DEFAULT_WS_ROOT}）")
    p_sweep.add_argument("--apply", action = "store_true", help = "门禁全过后开 PR 并回收工作区；缺省只 stage+gates（dry）")
    p_sweep.set_defaults(func = cmd_sweep)

    p_apply = sub.add_parser("apply", help = "门禁 + worktree + 提交 + push + PR")
    p_apply.add_argument("drop_dir", type = Path)
    p_apply.add_argument("--dry-run", action = "store_true", help = "到 diff 为止：不提交、不 push、不开 PR")
    p_apply.set_defaults(func = cmd_apply)

    p_stage = sub.add_parser("stage", help = "从脏 worktree 构造 drop（diff → MANIFEST + files/）")
    p_stage.add_argument("worktree", type = Path)
    p_stage.add_argument("--repo", required = True, help = "仓库名（单段，须在 repo-root 下）")
    p_stage.add_argument("--description", required = True, help = "<gitmoji> 英文陈述句.（与 G6 同格式）")
    p_stage.add_argument("--base", default = "origin/master")
    p_stage.add_argument("--branch", default = None, help = "默认 engine/<drop-id>")
    p_stage.add_argument("--drop-id", default = None, help = "默认 auto-<UTC 时间戳>-<diff 短哈希>")
    p_stage.set_defaults(func = cmd_stage)

    p_audit = sub.add_parser("audit", help = "读审计流水")
    p_audit.add_argument("--limit", type = int, default = 20)
    p_audit.set_defaults(func = cmd_audit)
    return parser

def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(list(sys.argv[1:] if argv is None else argv))
    func = getattr(args, "func", None)

    if func is None:
        build_parser().print_help()
        return EXIT_USAGE

    return func(args)

if __name__ == "__main__":
    sys.exit(main())

#!/usr/bin/env python3
"""external_review.py — 半月度「外部视角」验证：把同族自证换成一个没读过规则的陌生人。

Python port of ``_tools/external-review.sh`` (stdlib only, Python 3.9+).
The CLI surface, output files and exit codes are preserved exactly:

    external_review.py pack [--out DIR]     # 打证据包（默认 <WS>/_lens/<日期>/）
    external_review.py status               # 距上次外部验证多久；超期退出码 1
    external_review.py due                  # 仅供 timer/cron 判断（静默，超期退出 1）
    external_review.py record <文件>        # 把外部验证者的结论收进 <WS>/_reports/

Exit codes: 0 ok, 1 overdue / isolation failure, 2 usage error.
Environment: ``WORKSPACE_ROOT`` (default /mnt/codespace), ``EXTERNAL_REVIEW_DAYS``
(default 14).
"""

from __future__ import annotations

import argparse
import os
import re
import shutil
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import List, Optional, Sequence

EXIT_OK = 0
EXIT_FAILURE = 1
EXIT_USAGE = 2

SECONDS_PER_DAY = 86400

# 硬性排除清单：证据包里**绝不允许**出现的东西（出现即视为打包失败）
EXCLUDE_NAMES = ("AGENTS.md", "PLAN.md", "CLAUDE.md")
EXCLUDE_DIRS = ("_plan-archive", "_reports", "_worktree", "_locks", "_hooks", "_backups")
# 内容级（硬门）：本工具生成的叙述文件不得引用工作区规则
CONTENT_LEAK_RE = re.compile(r"AGENTS\.md|§6\.4|认领|claim\.sh")
# 逐字源码里对内部文档的引用：只登记、不判失败
CODE_REF_RE = re.compile(r"AGENTS\.md|PLAN\.md|§[0-9]")

SAMPLE_FILES = (
    "entelecheia/packages/domain_agents/industrial_iot/src/tools/tools/modbus_write.rs",
    "entelecheia/packages/shared/security_policy/src/industrial_write_policy.rs",
    "evernight/src/serial/modbus.rs",
    "entelecheia/scripts/deploy/backup.py",
)
LOG_REPOS = ("entelecheia", "evernight", "plana", "shittim-chest", "arona", "hikari")
# 健康探针目标：默认 RFC 5737 文档地址；真实部署用环境变量 EXTERNAL_REVIEW_HEALTH_URLS
# （空格分隔）注入，禁止把内网地址写进仓库树。
DEFAULT_HEALTH_URLS = (
    "http://192.0.2.10:3005/api/health",
    "http://192.0.2.10:3009/api/health",
    "https://dev.celestia.world/api/health",
    "https://gateway.celestia.world/api/health",
)
HEALTH_URLS = tuple(
    os.environ.get("EXTERNAL_REVIEW_HEALTH_URLS", "").split()
) or DEFAULT_HEALTH_URLS

REVIEWER_PROMPT = """# 给验证者的提示词（请在工作区之外、用另一个模型或另一个人执行）

你要做的是**独立评估**，不是复述。你没有、也不需要这个项目的任何内部文档。
下面四个文件是你唯一的输入：

- `00-manifest.md` —— 本包构成说明，以及**请忽略**的注释内引用
- `01-repos.md` —— 有哪些仓、多大、最近动没动
- `02-recent-changes.md` —— 最近这些人在改什么（只看标题）
- `code/` —— 若干关键实现的**原文**
- `03-live-surface.md` —— 线上服务的实际响应

请回答这四个问题，每条都要**指到具体文件与行**，不要给泛泛的好话：

1. **这东西在解决什么问题？** 只从代码与线上响应推断。如果推不出来，说清楚哪一步断了。
2. **它宣称的能力，代码真的做到了吗？** 挑三处你认为**最可能是"文档/注释说得比实现多"**的地方，
   逐行核对，给出结论（成立 / 不成立 / 无法判定，以及为什么）。
3. **如果一个陌生人明天要接手并上线它，他最可能在哪儿出事？** 按后果严重度排序，给三条。
4. **代码里有没有"看起来是安全措施、实际不起作用"的东西？** 这类东西最危险，因为审计者会因为它
   的存在而放心。逐条列出你的怀疑与证据。

约束：
- 不要猜你没看到的文件；明说"输入不足"比编一个合理答案有价值。
- 不要提"最佳实践""建议增加测试"这类通用话；只说你在这份输入里**看到了什么**。
- 结尾用一段话回答：**基于你看到的这些，你会不会把自己的设备交给它控制？为什么。**
"""

USAGE_TEXT = """external-review.py — 半月度「外部视角」验证：把同族自证换成一个没读过规则的陌生人

为什么需要它
------------
工作区的验证循环（§6.2 三轮 + 子代理交叉验证）对**可判定问题**有效：编译、格式、
死代码、变异测试——这些有客观答案，谁验都一样。
对**方向性问题**近乎无效：这个设计对不对？这个 P0 该不该先做？因为验证者与作者
共享同一套前提、同一份 AGENTS.md、同一批盲区。
直接后果实测于 2026-09-10 → 09-13：一份 458 行、证据可复算的审计把五条 P0 列得清清楚楚，
三天后真正被做的是图标按钮组的像素级打磨——**没有外力时，选择函数只剩"什么最好做"**。

关键约束（决定了它不能是一个子代理）
------------------------------------
本工作区的任何子代理都会被自动注入 AGENTS.md 与 PLAN.md。一个"读过规则的验证者"
无法回答"这套东西在外人眼里成不成立"——它已经被规训成内部人。
所以本工具**不派发验证者**，它只做三件事：
  ① 打一个**证据包**（只有代码、产物、数字；显式排除 AGENTS/PLAN/_reports/归档）；
  ② 写一份**验证者提示词**（不含任何内部术语与规则）；
  ③ 校验隔离性，并维护到期节奏。
真正的验证者由用户在**工作区之外**（另一台机 / 另一个模型会话 / 一个人）启动。

用法
  external-review.py pack [--out DIR]     # 打证据包（默认 _lens/<日期>/）
  external-review.py status               # 距上次外部验证多久；超期退出码 1
  external-review.py due                  # 仅供 timer/cron 判断（静默，超期退出 1）
  external-review.py record <文件>        # 把外部验证者的结论收进 _reports/

节奏：每 14 天一次（可用 EXTERNAL_REVIEW_DAYS 覆盖）。
"""


def die(message: str) -> "NoReturn":  # type: ignore[name-defined]
    print(f"external-review: {message}", file=sys.stderr)
    raise SystemExit(EXIT_USAGE)


def git(*args: str, repo: Optional[Path] = None) -> Optional[str]:
    """Run git and return stripped stdout, or None on any failure."""
    cmd = ["git"]
    if repo is not None:
        cmd += ["-C", str(repo)]
    cmd += list(args)
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.TimeoutExpired):
        return None
    if proc.returncode != 0:
        return None
    return proc.stdout.strip()


# ── 节奏 ──────────────────────────────────────────────────────────────────────


def last_review_epoch(reports_dir: Path) -> int:
    """mtime of the newest ``external-review-*.md`` record, or 0 if none."""
    latest = 0
    if not reports_dir.is_dir():
        return 0
    for path in reports_dir.glob("external-review-*.md"):
        try:
            epoch = int(path.stat().st_mtime)
        except OSError:
            continue
        if epoch > latest:
            latest = epoch
    return latest


def epoch_to_date(epoch: int) -> str:
    """``date -d "@$epoch" +%Y-%m-%d`` equivalent (never fails)."""
    if epoch <= 0:
        return "?"
    try:
        return datetime.fromtimestamp(epoch).strftime("%Y-%m-%d")
    except (OSError, OverflowError, ValueError):
        return "?"


def cycle_days_left(last_epoch: int, now_epoch: int, days: int) -> int:
    """Days left in the 14-day cycle; <= 0 means overdue.

    Mirrors bash: days_left = DAYS - (now - last) / 86400 (integer division).
    """
    age_days = int((now_epoch - last_epoch) / SECONDS_PER_DAY)
    return days - age_days


def status_line(last_epoch: int, now_epoch: int, days: int) -> "tuple[str, int]":
    if last_epoch == 0:
        return (
            f"⚠️  从未做过外部视角验证（节奏 {days} 天）——立即执行 "
            "`_tools/external-review.sh pack`",
            EXIT_FAILURE,
        )
    days_left = cycle_days_left(last_epoch, now_epoch, days)
    last_date = epoch_to_date(last_epoch)
    if days_left <= 0:
        return (
            f"❌ 外部视角验证已超期 {-days_left} 天（上次 {last_date}，节奏 {days} 天）"
            f"——跑 `_tools/external-review.sh pack`",
            EXIT_FAILURE,
        )
    return (
        f"✅ 外部视角验证未到期：上次 {last_date}，还有 {days_left} 天",
        EXIT_OK,
    )


def write_due_file(ws: Path, line: str, now_epoch: int, days: int, rc: int) -> None:
    """Atomically (tmp + rename) refresh ``<WS>/_lens/DUE.md``; failures ignored."""
    lens = ws / "_lens"
    try:
        lens.mkdir(parents=True, exist_ok=True)
        generated = datetime.now().astimezone().isoformat(timespec="seconds")
        body = (
            "# 外部视角验证：到期状态（由 `_tools/external-review.sh` 生成，勿手改）\n\n"
            f"{line}\n\n"
            f"生成于 {generated} ｜ 节奏 {days} 天 ｜ 判定 `exit {rc}`\n\n"
            "## 为什么需要它\n\n"
            "工作区的三轮验证与子代理交叉验证对**可判定问题**（编译 / 格式 / 死代码 / 变异）有效，\n"
            "对**方向性问题**（这个设计对不对、这个 P0 该不该先做）近乎无效——"
            "验证者与作者共享同一批盲区。\n"
            "实证：2026-09-10 一份 458 行、可复算的审计列出五条 P0，"
            "三天后真正被做的是图标按钮组的像素打磨。\n\n"
            "## 怎么做\n\n"
            "1. `_tools/external-review.sh pack` 打证据包（只有代码 / 数字 / 线上响应）\n"
            "2. 把整个目录交给**工作区之外**的一个模型或一个人（**不能是本工作区的子代理**——\n"
            "   它们都会被自动注入 AGENTS.md，已经被规训成内部人）\n"
            "3. `_tools/external-review.sh record <它的回答.md>` 收录结论\n"
        )
        tmp = lens / f"DUE.md.tmp.{os.getpid()}"
        tmp.write_text(body, encoding="utf-8")
        os.replace(tmp, lens / "DUE.md")
    except OSError:
        pass


def cmd_status(args: argparse.Namespace, quiet: bool = False) -> int:
    ws = args.workspace
    now_epoch = args.now_epoch if args.now_epoch is not None else int(time.time())
    last = last_review_epoch(ws / "_reports")
    line, rc = status_line(last, now_epoch, args.days)
    if not quiet:
        print(line)
    write_due_file(ws, line, now_epoch, args.days, rc)
    return rc


def cmd_due(args: argparse.Namespace) -> int:
    with open(os.devnull, "w") as devnull:
        old_stdout = sys.stdout
        sys.stdout = devnull
        try:
            return cmd_status(args, quiet=True)
        finally:
            sys.stdout = old_stdout


# ── 打包 ──────────────────────────────────────────────────────────────────────


def write_repos_table(ws: Path, out: Path) -> None:
    lines = [
        "# 证据包：仓库清单与规模\n",
        "",
        "> 本文件由 `_tools/external-review.sh` 生成。**只有代码、产物与数字**，",
        "> 不含任何工作区规则、计划或历史审计。\n",
        "| 仓库 | 源文件 | 提交数 | 最后提交 |",
        "|---|---|---|---|",
    ]
    for entry in sorted(ws.iterdir()):
        if not entry.is_dir() or not (entry / ".git").is_dir():
            continue
        n_files = git("ls-files", repo=entry)
        n_log = git("log", "--oneline", repo=entry)
        last = git("log", "-1", "--format=%ad", "--date=short", repo=entry)
        n_files = str(len(n_files.splitlines())) if n_files is not None else "0"
        n_log = str(len(n_log.splitlines())) if n_log is not None else "0"
        last = last if last is not None else ""
        lines.append(f"| {entry.name} | {n_files} | {n_log} | {last} |")
    (out / "01-repos.md").write_text("\n".join(lines) + "\n", encoding="utf-8")


def write_recent_changes(ws: Path, out: Path) -> None:
    lines = ["# 证据包：近期变更（提交标题，倒序）\n", "", "```"]
    for repo in LOG_REPOS:
        repo_dir = ws / repo
        if not (repo_dir / ".git").is_dir():
            continue
        lines.append(f"\n## {repo}")
        log = git("log", "-40", "--format=%ad %s", "--date=short", repo=repo_dir)
        if log:
            lines.append(log)
    lines.append("```")
    (out / "02-recent-changes.md").write_text("\n".join(lines) + "\n", encoding="utf-8")


def origin_ref(repo: Path) -> "tuple[Optional[str], Optional[str]]":
    for base in ("master", "main"):
        sha = git("rev-parse", "--verify", f"origin/{base}", repo=repo)
        if sha:
            return f"origin/{base}", sha
    return None, None


def write_code_sample(ws: Path, out: Path) -> None:
    code_dir = out / "code"
    code_dir.mkdir(parents=True, exist_ok=True)
    provenance: List[str] = [
        f"# 证据包：`code/` 每个文件的出处（生成于 {now_iso()}）\n",
        "",
        "抽样一律取自 `origin/<默认分支>` 的提交对象，而非工作树——工作树可能停在旧提交上。\n",
        "| 文件 | 仓库 | ref | commit |",
        "|---|---|---|---|",
    ]
    for rel in SAMPLE_FILES:
        src = ws / rel
        if not src.is_file():
            continue
        repo = ws / rel.split("/", 1)[0]
        repo_rel = rel.split("/", 1)[1]
        ref, sha = origin_ref(repo)
        (code_dir / rel).parent.mkdir(parents=True, exist_ok=True)
        content: Optional[bytes] = None
        if sha:
            try:
                proc = subprocess.run(
                    ["git", "-C", str(repo), "show", f"{sha}:{repo_rel}"],
                    capture_output=True,
                    timeout=30,
                )
                if proc.returncode == 0:
                    content = proc.stdout
            except (OSError, subprocess.TimeoutExpired):
                content = None
        if content is not None:
            (code_dir / rel).write_bytes(content)
            provenance.append(f"| `{rel}` | {repo.name} | `{ref}` | `{sha[:12]}` |")
        else:
            # 取不到 origin（新仓 / 无远端）就退回工作树，并且**明写出来**——不静默降级
            shutil.copyfile(src, code_dir / rel)
            provenance.append(f"| `{rel}` | {repo.name} | **工作树（无 origin ref）** | — |")
    provenance.append("\n## 工作树 vs origin（只记录，不评判）\n")
    provenance.append("```")
    for repo_name in ("entelecheia", "evernight"):
        repo = ws / repo_name
        ref, _sha = origin_ref(repo)
        if not ref:
            continue
        counts = git("rev-list", "--left-right", "--count", f"HEAD...{ref}", repo=repo)
        if counts:
            parts = counts.split()
            ahead, behind = (parts + ["0", "0"])[:2]
            provenance.append(f"{repo_name:<14} ahead {ahead} / behind {behind}")
    provenance.append("```")
    (out / "00-code-provenance.md").write_text(
        "\n".join(provenance) + "\n", encoding="utf-8"
    )


def write_live_surface(out: Path) -> None:
    import urllib.request

    lines = [f"# 证据包：线上可观测面（生成于 {now_iso()}）\n"]
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    for url in HEALTH_URLS:
        lines.append(f"## {url}\n")
        lines.append("```")
        body = ""
        try:
            req = urllib.request.Request(url, headers={"Accept": "application/json"})
            with opener.open(req, timeout=8) as resp:
                body = resp.read(1200).decode("utf-8", errors="replace")
        except Exception as exc:  # noqa: BLE001 — 与 bash 版一致：失败即 (不可达)
            body = str(exc)[:1200]
        lines.append(body if body else "(不可达)")
        lines.append("```\n")
    (out / "03-live-surface.md").write_text("\n".join(lines) + "\n", encoding="utf-8")


def now_iso() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def isolation_violations(out: Path) -> List[str]:
    """Return human-readable violations; empty list means the pack is clean."""
    problems: List[str] = []
    names = {p.name for p in out.rglob("*")}
    for name in EXCLUDE_NAMES:
        if name in names:
            problems.append(f"❌ 隔离失败：证据包里出现了 {name}")
    dirs = {p.name for p in out.rglob("*") if p.is_dir()}
    for dirname in EXCLUDE_DIRS:
        if dirname in dirs:
            problems.append(f"❌ 隔离失败：证据包里出现了 {dirname}/")
    for path in sorted(out.glob("0*.md")):
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        if CONTENT_LEAK_RE.search(text):
            problems.append(f"❌ 隔离失败：生成的 {path.name} 引用了工作区规则")
    return problems


def write_manifest(out: Path) -> None:
    code_files = sorted(
        str(p.relative_to(out)) for p in (out / "code").rglob("*") if p.is_file()
    )
    lines = [
        "# 证据包构成说明\n",
        f"生成于 {now_iso()}。本包**只有**四样东西：仓库规模数字、近期提交标题、"
        "若干关键源码原文、线上响应。\n",
        "**不含**任何项目内部规则、计划、路线或历史审计。\n",
        '## 本包**不含**什么（"没看到"不等于"没实现"）\n',
        '第一轮外部验证（2026-09-14）把"包里没有"读成了"代码里没有"，并据此给出了三条高危结论。\n',
        "那是**取材**的问题，不是验证者的问题——所以边界必须写在包内：\n",
        "- `code/` 只有下面这些文件，**不是**每个仓都取，也**不是**沿调用链取全：\n\n```",
        *code_files,
        "```\n",
        "- 其它仓库（plana / kirino / hikari / shittim-chest / arona / malkuth 等）"
        "**只有提交标题**，没有源码。\n",
        "- **外部依赖的源码完全没有**。若代码调用了一个包外实现（第三方 crate / 另一个服务），\n"
        "  本包**无法**证实或证伪那个实现的行为——请把这类判断标成'输入不足'，而不是'未实现'。\n",
        "- 服务端入口（RPC / HTTP handler）、CI workflow、systemd 单元、部署脚本："
        "除上面列出的文件外都没有。\n",
        "- 测试只以'被抽样文件内部的 `#[cfg(test)]`'形式出现，没有单独的测试目录。\n",
        '请在结论里区分"本包没给"与"代码没做"，并写明你还需要哪些文件才能判定。\n',
        "## 已知的内部引用（出现在逐字源码的注释里，请忽略）\n",
    ]
    hits: List[str] = []
    for path in sorted((out / "code").rglob("*")):
        if not path.is_file():
            continue
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        for lineno, line in enumerate(text.splitlines(), 1):
            if CODE_REF_RE.search(line):
                rel = path.relative_to(out)
                hits.append(f"{rel}:{lineno}:{line.strip()}")
    if hits:
        lines.append("```")
        lines.extend(hits)
        lines.append("```\n")
        lines.append("这些只是注释里对内部文档的引用，**与代码行为无关**；不要据此推断设计意图。\n")
    else:
        lines.append("（无）\n")
    (out / "00-manifest.md").write_text("\n".join(lines) + "\n", encoding="utf-8")


def cmd_pack(args: argparse.Namespace) -> int:
    ws = args.workspace
    out = args.out if args.out is not None else ws / "_lens" / datetime.now().strftime("%Y-%m-%d")
    if out.exists():
        die(f"目标已存在：{out}（换个 --out，或先删掉）")
    try:
        out.mkdir(parents=True)
    except OSError as exc:
        die(f"无法创建 {out}\n  {exc}")

    print(f"正在打证据包 → {out}")

    write_repos_table(ws, out)
    write_recent_changes(ws, out)
    write_code_sample(ws, out)
    (out / "REVIEWER-PROMPT.md").write_text(REVIEWER_PROMPT, encoding="utf-8")
    write_live_surface(out)

    problems = isolation_violations(out)
    if problems:
        for problem in problems:
            print(problem, file=sys.stderr)
        print("证据包已作废（隔离性是硬门）。", file=sys.stderr)
        shutil.rmtree(out, ignore_errors=True)
        return EXIT_FAILURE

    write_manifest(out)

    print(f"✅ 证据包就绪：{out}")
    print("   隔离自检通过：无 AGENTS.md / PLAN.md / _reports / 归档 / 认领痕迹")
    print("   下一步：把整个目录交给**工作区之外**的一个模型或一个人，")
    print("           用 REVIEWER-PROMPT.md 提问，然后")
    print("           `_tools/external-review.sh record <它的回答.md>`")
    return EXIT_OK


def cmd_record(args: argparse.Namespace) -> int:
    src = args.file
    if not src or not src.is_file():
        die("用法：external-review.sh record <验证者回答的 .md 文件>")
    reports = args.workspace / "_reports"
    reports.mkdir(parents=True, exist_ok=True)
    today = datetime.now().strftime("%Y-%m-%d")
    dst = reports / f"external-review-{today}.md"
    body = (
        f"# 外部视角验证记录（{today}）\n\n"
        "> 由 `_tools/external-review.sh record` 收录。验证者**未接触**本工作区任何规则、\n"
        f"> 计划或历史审计，输入仅为当日证据包（`_lens/{today}/`）。\n\n"
        "---\n\n" + src.read_text(encoding="utf-8", errors="replace")
    )
    dst.write_text(body, encoding="utf-8")
    print(f"✅ 已收录到 {dst}")
    print("   提醒：外部验证的价值在于**它的结论没被内部共识污染**——")
    print("   对它的处置应当是「逐条回应」而不是「逐条解释掉」。")
    return EXIT_OK


# ── CLI ───────────────────────────────────────────────────────────────────────


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="external_review.py",
        description="半月中「外部视角」验证证据包工具（stdlib only, port of external-review.sh）.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=USAGE_TEXT,
    )
    parser.add_argument(
        "--workspace-root",
        type=Path,
        default=None,
        help=argparse.SUPPRESS,
    )
    sub = parser.add_subparsers(dest="command", metavar="{pack,status,due,record}")

    p_pack = sub.add_parser("pack", help="打证据包（默认 _lens/<日期>/）")
    p_pack.add_argument("--out", type=Path, default=None, help="输出目录（默认 _lens/<日期>/）")
    p_pack.set_defaults(func=cmd_pack)

    for name, func, help_ in (
        ("status", cmd_status, "距上次外部验证多久；超期退出码 1"),
        ("due", cmd_due, "仅供 timer/cron 判断（静默，超期退出 1）"),
    ):
        p = sub.add_parser(name, help=help_)
        p.set_defaults(func=func)

    p_record = sub.add_parser("record", help="把外部验证者的结论收进 _reports/")
    p_record.add_argument("file", type=Path, nargs="?", default=None)
    p_record.set_defaults(func=cmd_record)
    return parser


KNOWN_COMMANDS = ("pack", "status", "due", "record", "help", "-h", "--help")


def main(argv: Optional[Sequence[str]] = None) -> int:
    ws = Path(os.environ.get("WORKSPACE_ROOT", "/mnt/codespace"))
    ws = ws if ws.is_absolute() else Path.cwd() / ws
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv and argv[0] not in KNOWN_COMMANDS:
        die(f"未知子命令：{argv[0]}（pack/status/due/record）")
    if argv and argv[0] == "help":
        print(USAGE_TEXT, end="")
        return EXIT_OK
    args = build_parser().parse_args(argv)
    args.workspace = args.workspace_root if args.workspace_root else ws
    args.days = int(os.environ.get("EXTERNAL_REVIEW_DAYS", "14"))
    args.now_epoch = None
    func = getattr(args, "func", None)
    if func is None:
        print(USAGE_TEXT, end="")
        return EXIT_OK
    return func(args)


if __name__ == "__main__":
    sys.exit(main())

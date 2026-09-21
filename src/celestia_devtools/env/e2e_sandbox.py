#!/usr/bin/env python3
"""e2e-sandbox — Playwright/e2e 临时文件的统一管理（跑完即清）。

背景（2026-09-21 根盘事故，43 GB 残骸）：全工作区的验证作业大量使用
Playwright/chromium，浏览器 profile 落在 ``/tmp``；作业被 timeout/kill 或
浏览器异常退出时无人清理，累计把 node-1 根盘吃到 97%（ENOSPC 实错）。
chromium 与 Playwright 都尊重 ``TMPDIR``，所以把每个运行的 ``TMPDIR``
指到本地大盘上的独立沙箱、结束时**无条件**清除，就能从机制上消灭残骸。

用法::

    # 包装任意命令（推荐——一行接线，正常/失败/超时都清）
    celestia-devtools e2e-sandbox run --label my-e2e -- node script.cjs
    celestia-devtools e2e-sandbox run -- pytest -q tests/e2e

    # 调试时保留沙箱
    celestia-devtools e2e-sandbox run --keep -- node script.cjs

    # 清扫沙箱根下的陈旧目录（兜底：被 kill 的 run）
    celestia-devtools e2e-sandbox sweep --max-age 6

    # 清扫 /tmp 里的孤儿 chromium profile（兜底：不经过包装器的作业）
    celestia-devtools e2e-sandbox sweep-tmp --max-age 60

设计要点：
- 沙箱根默认 ``/mnt/work/e2e-sandbox``（本地大盘，可 ``E2E_SANDBOX_ROOT`` 覆盖）；
- ``run`` 对子进程设置 ``TMPDIR``/``TMP``/``TEMP``，chromium 的 profile、
  Playwright 的 artifacts、任何 ``tempfile`` 都落在沙箱内；
- 清理用 ``finally`` 语义：正常退出、非零退出、超时（``--timeout``）、
  SIGTERM/SIGINT 全都触发；除非 ``--keep``；
- ``sweep-tmp`` 只删「当前用户所有 + 名字形态像 chromium 临时目录
  （≥20 位 ``[0-9A-Za-z_-]``）+ 超过 ``--max-age`` 分钟未动」的**目录**，
  不碰文件、不碰 systemd/X11 等系统套接字、权限不足的自动跳过。
"""

from __future__ import annotations

import argparse
import os
import re
import shutil
import signal
import subprocess
import sys
import time
from pathlib import Path
from typing import Sequence

DEFAULT_ROOT = "/mnt/work/e2e-sandbox"
# chromium 的 mkdtemp 形态：≥20 位 [0-9A-Za-z_-]，无点前缀、无常见系统名
_CHROMIUM_DIR_SHAPE = re.compile(r"^[0-9A-Za-z_-]{20,}$")
# 永不匹配的名字（双保险；形态正则本身已排除带前缀/点号的名字）
_NEVER_TOUCH = {
    "systemd-private-",
    ".X11-unix",
    ".ICE-unix",
    ".font-unix",
    ".Test-unix",
    ".XIM-unix",
    "snap-private-tmp",
    ".X0-lock",
}


def _kill_tree(child: subprocess.Popen | None, sig: int = signal.SIGKILL) -> None:
    """Signal the child's whole process group (start_new_session put it alone)."""
    if child is None or child.poll() is not None:
        return
    try:
        os.killpg(child.pid, sig)
    except (ProcessLookupError, PermissionError, OSError):
        try:
            child.send_signal(sig)
        except (ProcessLookupError, OSError):
            pass


def sandbox_root() -> Path:
    return Path(os.environ.get("E2E_SANDBOX_ROOT", DEFAULT_ROOT))


def _new_sandbox(label: str) -> Path:
    root = sandbox_root()
    stamp = time.strftime("%Y%m%d-%H%M%S")
    box = root / f"{stamp}-{os.getpid()}-{re.sub(r'[^0-9A-Za-z_.-]', '_', label)[:40] or 'run'}"
    (box / "tmp").mkdir(parents=True, exist_ok=True)
    return box


class _Terminated(BaseException):
    """Raised by SIGTERM/SIGHUP so the finally-cleanup path runs (2026-09-21:
    without this, a SIGTERM from CI's `timeout` command killed the wrapper
    outright, the finally never ran, the sandbox leaked, and the child became
    an orphan — exactly the incident this facility exists to prevent)."""

    def __init__(self, signum: int):
        super().__init__(signum)
        self.signum = signum


def _term_handler(signum: int, frame) -> None:  # noqa: ARG001
    raise _Terminated(signum)


def cmd_run(argv: Sequence[str]) -> int:
    """Wrap a command with an isolated, always-cleaned TMPDIR sandbox."""
    parser = argparse.ArgumentParser(
        prog="e2e-sandbox run",
        description="Run a command with TMPDIR isolated to a per-run sandbox "
        "under /mnt/work; the sandbox is removed unconditionally "
        "after the command exits (unless --keep).",
    )
    parser.add_argument("--label", default="", help="short label for the sandbox dir name")
    parser.add_argument(
        "--keep", action="store_true", help="keep the sandbox for debugging (prints its path)"
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=0,
        metavar="SECONDS",
        help="kill the child after SECONDS (0 = no timeout)",
    )
    parser.add_argument("cmd", nargs=argparse.REMAINDER, help="command to run (after --)")
    args = parser.parse_args(argv)
    if not args.cmd or args.cmd[0] == "--":
        args.cmd = args.cmd[1:] if args.cmd else []
    if not args.cmd:
        parser.error("no command given; use: e2e-sandbox run [--label L] -- <cmd...>")

    box = _new_sandbox(args.label)
    tmp = box / "tmp"
    env = dict(os.environ)
    env["TMPDIR"] = str(tmp)
    env["TMP"] = str(tmp)
    env["TEMP"] = str(tmp)
    env.setdefault("E2E_SANDBOX_DIR", str(box))

    child = None
    rc: int | None = None
    old_handlers: dict[int, object] = {}
    try:
        # SIGTERM/SIGHUP must go through the exception path so `finally` runs.
        # SIGINT already arrives as KeyboardInterrupt; SIGKILL cannot be caught
        # (the `sweep` subcommand is the backstop for that).
        for sig in (signal.SIGTERM, signal.SIGHUP):
            try:
                old_handlers[sig] = signal.signal(sig, _term_handler)
            except (ValueError, OSError):
                pass  # not in main thread — sweep is the backstop
        # New session: the child (and any grandchildren) form their own process
        # group, so we can kill the whole tree without signaling ourselves.
        child = subprocess.Popen(args.cmd, env=env, start_new_session=True)
        if args.timeout > 0:
            try:
                rc = child.wait(timeout=args.timeout)
            except subprocess.TimeoutExpired:
                _kill_tree(child)
                rc = child.wait()
                print(f"e2e-sandbox: timed out after {args.timeout}s, killed", file=sys.stderr)
                rc = rc if rc is not None else 124
        else:
            rc = child.wait()
    except KeyboardInterrupt:
        _kill_tree(child, signal.SIGINT)
        rc = child.wait() if child else 130
        rc = rc if rc >= 0 else 130
    except _Terminated as exc:
        _kill_tree(child, exc.signum)
        rc = child.wait() if child else 128 + exc.signum
        rc = rc if rc >= 0 else 128 + exc.signum
    finally:
        for sig, old in old_handlers.items():
            try:
                signal.signal(sig, old)  # type: ignore[arg-type]
            except (ValueError, OSError):
                pass
        if args.keep:
            print(f"e2e-sandbox: kept {box}")
        else:
            shutil.rmtree(box, ignore_errors=True)
    return rc if rc is not None and rc >= 0 else (rc & 0xFF if rc is not None else 1)


def cmd_sweep(argv: Sequence[str]) -> int:
    """Remove stale sandbox dirs under the sandbox root."""
    parser = argparse.ArgumentParser(
        prog="e2e-sandbox sweep",
        description="Remove sandbox directories older than --max-age hours.",
    )
    parser.add_argument(
        "--max-age",
        type=float,
        default=6.0,
        metavar="HOURS",
        help="age threshold in hours (default 6)",
    )
    parser.add_argument("--dry-run", action="store_true", help="list, do not delete")
    args = parser.parse_args(argv)

    root = sandbox_root()
    if not root.is_dir():
        print(f"e2e-sandbox: no sandbox root at {root}")
        return 0
    cutoff = time.time() - args.max_age * 3600
    removed = kept = 0
    freed_bytes = 0
    for entry in sorted(root.iterdir()):
        if not entry.is_dir():
            continue
        try:
            mtime = entry.stat().st_mtime
        except OSError:
            continue
        if mtime > cutoff:
            kept += 1
            continue
        if args.dry_run:
            print(f"would remove: {entry}")
            removed += 1
            continue
        size = sum(f.stat().st_size for f in entry.rglob("*") if f.is_file())
        shutil.rmtree(entry, ignore_errors=True)
        freed_bytes += size
        removed += 1
    print(
        f"e2e-sandbox: swept {removed}, kept {kept}"
        + (f", freed {freed_bytes / 1e6:.0f} MB" if freed_bytes else "")
    )
    return 0


def cmd_sweep_tmp(argv: Sequence[str]) -> int:
    """Remove stale chromium-shaped temp dirs from /tmp (the backstop).

    Only touches directories that: belong to the current user, are older than
    --max-age minutes, and have a name shaped like chromium's mkdtemp output
    (>=20 chars of [0-9A-Za-z_-]). Never touches files or known system names.
    """
    parser = argparse.ArgumentParser(
        prog="e2e-sandbox sweep-tmp",
        description="Backstop: remove orphan chromium profile dirs from /tmp.",
    )
    parser.add_argument(
        "--max-age",
        type=float,
        default=60.0,
        metavar="MINUTES",
        help="age threshold in minutes (default 60)",
    )
    parser.add_argument("--tmp-dir", default="/tmp", help="temp dir to sweep (default /tmp)")
    parser.add_argument("--dry-run", action="store_true", help="list, do not delete")
    args = parser.parse_args(argv)

    tmp = Path(args.tmp_dir)
    cutoff = time.time() - args.max_age * 60
    me = os.getuid()
    removed = skipped = 0
    for entry in sorted(tmp.iterdir()):
        name = entry.name
        if not _CHROMIUM_DIR_SHAPE.match(name):
            continue
        if any(name.startswith(p) for p in _NEVER_TOUCH):
            continue
        if not entry.is_dir() or entry.is_symlink():
            continue
        try:
            st = entry.stat()
        except OSError:
            continue
        if st.st_uid != me:
            skipped += 1
            continue
        if st.st_mtime > cutoff:
            continue  # active (recently touched) — never remove
        if args.dry_run:
            print(f"would remove: {entry}")
            removed += 1
            continue
        shutil.rmtree(entry, ignore_errors=True)
        removed += 1
    print(
        f"e2e-sandbox: sweep-tmp removed {removed}"
        + (f", skipped(not ours) {skipped}" if skipped else "")
    )
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    argv = list(argv if argv is not None else sys.argv[1:])
    if not argv or argv[0] in ("-h", "--help"):
        print(__doc__)
        return 0
    sub, rest = argv[0], argv[1:]
    if sub == "run":
        return cmd_run(rest)
    if sub == "sweep":
        return cmd_sweep(rest)
    if sub == "sweep-tmp":
        return cmd_sweep_tmp(rest)
    print(f"error: unknown subcommand '{sub}' (run | sweep | sweep-tmp)", file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())

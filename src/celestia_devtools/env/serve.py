#!/usr/bin/env python3
"""Shared process-supervision facility for the celestia ecosystem.

A generic ``ProcessManager`` that multiple ``dev.py`` scripts (shittim-chest,
entelecheia, evernight) consume so they share the same tagged-output,
graceful-shutdown, readiness-detection, and restart semantics — without each
repo carrying its own copy.

Lifecycle:
  start(tag, cmd)     → spawn a child, stream its stdout with [tag] prefix,
                        optionally detect a readiness marker.
  shutdown()          → SIGTERM every live child, wait, SIGKILL stragglers.
  restart(tag, cmd)   → kill the child with *tag*, start a new one.
  wait_forever()      → block until any child exits, then shut them all down.

The manager installs its own SIGINT/SIGTERM handler on ``wait_forever`` so
Ctrl-C cleanly drains all children.

Usage as a library::

    from celestia_devtools.env.serve import ProcessManager
    mgr = ProcessManager()
    mgr.start("backend", ["cargo", "run", "--", "serve"], ready_patterns=[r"Listening on"])
    mgr.start("frontend", ["npm", "run", "dev"])
    mgr.wait_forever()

CLI::

    celestia-devtools serve --help
"""

from __future__ import annotations

import os
import re
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Callable

from celestia_devtools.core import logger as _logger

_log = _logger


def _print_tagged(tag: str, text: str, *, file=None) -> None:
    """Print a line with a [tag] prefix."""
    print(f"[{tag}] {text}", file=file or sys.stdout, flush=True)


def _stream_tagged(
    tag: str,
    proc: subprocess.Popen,
    stop: threading.Event,
    ready_patterns: list[re.Pattern] | None = None,
    ready_event: threading.Event | None = None,
    log_fn: "_LogFn | None" = None,
) -> None:
    """Read proc.stdout line-by-line, print with [tag], detect readiness.

    ``log_fn`` overrides the default ``_print_tagged`` (e.g. for project-specific
    formatting / filtering). Signature: ``log_fn(tag: str, text: str) -> None``.
    """
    print_fn = log_fn or _print_tagged
    reader = proc.stdout
    if reader is None:
        return
    try:
        for raw in reader:
            if stop.is_set():
                break
            try:
                line = raw.decode(errors="replace")
            except AttributeError:
                line = raw
            text = line.rstrip("\n\r")
            if text:
                print_fn(tag, text)
            if ready_event is not None and not ready_event.is_set() and text:
                if any(p.search(text) for p in ready_patterns or ()):
                    ready_event.set()
    except ValueError:
        pass


# Type alias for the per-line log function override.
_LogFn = Callable[[str, str], None]


class ProcessManager:
    """Manages multiple child processes with tagged output and graceful shutdown.

    Shared across shittim-chest / entelecheia / evernight dev scripts so they
    have identical lifecycle semantics.

    ``log_fn`` (optional, set per-instance or per-start) overrides the default
    ``_print_tagged`` for project-specific formatting / filtering.
    """

    def __init__(self, *, log_fn: "_LogFn | None" = None) -> None:
        self._procs: list[tuple[str, subprocess.Popen]] = []
        self._threads: list[threading.Thread] = []
        self._stop = threading.Event()
        self._log_fn = log_fn

    def start(
        self,
        tag: str,
        cmd: list[str],
        *,
        cwd: Path | str | None = None,
        env: dict[str, str] | None = None,
        ready_patterns: list[re.Pattern | str] | None = None,
        ready_event: threading.Event | None = None,
        log_fn: "_LogFn | None" = None,
    ) -> subprocess.Popen:
        """Spawn *cmd* as a child, stream stdout with [tag].

        ``ready_patterns`` is a list of regex (compiled or raw strings); when
        any pattern matches a stdout line, ``ready_event`` is set (if given).
        ``log_fn`` overrides the instance-level log function for this process.
        """
        effective_env = env if env is not None else os.environ.copy()
        compiled = [
            p if isinstance(p, re.Pattern) else re.compile(p)
            for p in (ready_patterns or ())
        ]
        proc = subprocess.Popen(
            cmd,
            cwd=str(cwd) if cwd else None,
            env=effective_env,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
        self._procs.append((tag, proc))
        fn = log_fn or self._log_fn
        t = threading.Thread(
            target=_stream_tagged,
            args=(tag, proc, self._stop, compiled, ready_event, fn),
            daemon=True,
        )
        t.start()
        self._threads.append(t)
        return proc

    def shutdown(self) -> None:
        """SIGTERM every live child, wait (5s), SIGKILL stragglers."""
        self._stop.set()
        fn = self._log_fn or _print_tagged
        for _tag, proc in self._procs:
            if proc.poll() is None:
                try:
                    if hasattr(os, "killpg"):
                        os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
                    else:
                        proc.terminate()
                except (ProcessLookupError, PermissionError, OSError):
                    try:
                        proc.terminate()
                    except OSError:
                        pass
        for tag, proc in self._procs:
            if proc.poll() is not None:
                fn(tag, f"already exited (pid={proc.pid})")
                continue
            try:
                proc.wait(timeout=5)
                fn(tag, f"stopped (pid={proc.pid})")
            except subprocess.TimeoutExpired:
                proc.kill()
                try:
                    proc.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    pass
                fn(tag, f"killed after timeout (pid={proc.pid})")
        for t in self._threads:
            t.join(timeout=2)

    def any_dead(self) -> tuple[str, int] | None:
        """Return (tag, returncode) of the first dead child, or None."""
        for tag, proc in self._procs:
            if proc.poll() is not None:
                return (tag, proc.returncode)
        return None

    def restart(
        self,
        tag: str,
        cmd: list[str],
        *,
        cwd: Path | str | None = None,
        env: dict[str, str] | None = None,
    ) -> subprocess.Popen:
        """Kill any existing process with *tag*, then start a new one."""
        remaining: list[tuple[str, subprocess.Popen]] = []
        for t, proc in self._procs:
            if t == tag:
                if proc.poll() is None:
                    try:
                        if hasattr(os, "killpg"):
                            os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
                        else:
                            proc.terminate()
                    except (ProcessLookupError, PermissionError, OSError):
                        proc.terminate()
                    try:
                        proc.wait(timeout=5)
                    except subprocess.TimeoutExpired:
                        proc.kill()
            else:
                remaining.append((t, proc))
        self._procs = remaining
        return self.start(tag, cmd, cwd=cwd, env=env)

    def wait_forever(self) -> None:
        """Block until any child exits, then shut them all down.

        Installs SIGINT/SIGTERM handlers so Ctrl-C cleanly drains.
        """
        def _on_signal(signum, frame):
            raise KeyboardInterrupt

        signal.signal(signal.SIGINT, _on_signal)
        if hasattr(signal, "SIGTERM"):
            signal.signal(signal.SIGTERM, _on_signal)
        try:
            fn = self._log_fn or _print_tagged
            while True:
                dead = self.any_dead()
                if dead:
                    dead_tag, rc = dead
                    fn(dead_tag, f"process exited (rc={rc}) — shutting down")
                    break
                time.sleep(0.5)
        except KeyboardInterrupt:
            pass
        finally:
            self.shutdown()


def main() -> int:
    """CLI entry — minimal, mainly for help/listing."""
    import argparse

    p = argparse.ArgumentParser(
        prog="celestia-devtools serve",
        description="Shared process-supervision facility (import ProcessManager in dev scripts).",
    )
    p.add_argument(
        "--help-full",
        action="store_true",
        help="show detailed usage",
    )
    args = p.parse_args()
    if args.help_full:
        print(__doc__)
    else:
        p.print_help()
    return 0


if __name__ == "__main__":
    sys.exit(main())

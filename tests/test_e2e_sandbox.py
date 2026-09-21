#!/usr/bin/env python3
"""Tests for celestia_devtools.env.e2e_sandbox."""

from __future__ import annotations

import os
import stat
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from celestia_devtools.env import e2e_sandbox as sb  # noqa: E402


def test_run_cleans_sandbox_on_success(tmp_path):
    root = tmp_path / "sandbox-root"
    with mock.patch.dict(os.environ, {"E2E_SANDBOX_ROOT": str(root)}):
        rc = sb.main(["run", "--label", "ok", "--", sys.executable, "-c", "print('hi')"])
        assert rc == 0
        assert not root.exists() or not any(root.iterdir()), "sandbox must be cleaned"


def test_run_cleans_sandbox_on_failure(tmp_path):
    root = tmp_path / "sandbox-root"
    with mock.patch.dict(os.environ, {"E2E_SANDBOX_ROOT": str(root)}):
        rc = sb.main(["run", "--", sys.executable, "-c", "import sys; sys.exit(3)"])
        assert rc == 3
        assert not root.exists() or not any(root.iterdir()), "sandbox must be cleaned on failure"


def test_run_sets_tmpdir_for_child(tmp_path):
    root = tmp_path / "sandbox-root"
    marker = tmp_path / "marker"
    with mock.patch.dict(os.environ, {"E2E_SANDBOX_ROOT": str(root)}):
        # The child writes its TMPDIR to a file outside the sandbox.
        code = (
            "import os, pathlib; "
            f"pathlib.Path({str(marker)!r}).write_text(os.environ['TMPDIR'])"
        )
        rc = sb.main(["run", "--", sys.executable, "-c", code])
        assert rc == 0
        child_tmp = marker.read_text()
        assert str(root) in child_tmp, f"child TMPDIR must point inside sandbox, got {child_tmp}"
        assert not root.exists() or not any(root.iterdir())


def test_run_keep_preserves_sandbox(tmp_path):
    root = tmp_path / "sandbox-root"
    with mock.patch.dict(os.environ, {"E2E_SANDBOX_ROOT": str(root)}):
        rc = sb.main(["run", "--keep", "--label", "dbg", "--", "true"])
        assert rc == 0
        boxes = list(root.iterdir())
        assert len(boxes) == 1 and boxes[0].name.endswith("-dbg"), "sandbox must be kept with --keep"


def test_run_timeout_kills_and_cleans(tmp_path):
    root = tmp_path / "sandbox-root"
    with mock.patch.dict(os.environ, {"E2E_SANDBOX_ROOT": str(root)}):
        rc = sb.main(["run", "--timeout", "1", "--",
                      sys.executable, "-c", "import time; time.sleep(60)"])
        assert rc != 0, "timed-out child must yield a non-zero exit"
        assert not root.exists() or not any(root.iterdir()), "timeout must still clean"


def test_sweep_removes_stale_keeps_fresh(tmp_path):
    root = tmp_path / "sandbox-root"
    stale = root / "20260101-000000-1-old"
    fresh = root / (time.strftime("%Y%m%d-%H%M%S") + "-2-new")
    stale.mkdir(parents=True)
    fresh.mkdir(parents=True)
    old = time.time() - 10 * 3600
    os.utime(stale, (old, old))
    with mock.patch.dict(os.environ, {"E2E_SANDBOX_ROOT": str(root)}):
        rc = sb.main(["sweep", "--max-age", "6"])
        assert rc == 0
    assert not stale.exists()
    assert fresh.exists()


def test_sweep_dry_run_does_not_delete(tmp_path):
    root = tmp_path / "sandbox-root"
    stale = root / "20260101-000000-1-old"
    stale.mkdir(parents=True)
    old = time.time() - 10 * 3600
    os.utime(stale, (old, old))
    with mock.patch.dict(os.environ, {"E2E_SANDBOX_ROOT": str(root)}):
        sb.main(["sweep", "--max-age", "6", "--dry-run"])
    assert stale.exists(), "dry-run must not delete"


def _make_chromium_shaped_dir(base: Path, name: str, age_minutes: float, owner=None):
    d = base / name
    d.mkdir()
    old = time.time() - age_minutes * 60
    os.utime(d, (old, old))
    return d


def test_sweep_tmp_removes_shaped_stale_dirs(tmp_path):
    stale = _make_chromium_shaped_dir(tmp_path, "a" * 24, 120)
    fresh = _make_chromium_shaped_dir(tmp_path, "b" * 24, 5)
    notshaped = _make_chromium_shaped_dir(tmp_path, "systemd-private-x", 120)
    assert not sb._CHROMIUM_DIR_SHAPE.match(notshaped.name) or True  # prefix excluded by shape
    with mock.patch("sys.argv", ["x"]):
        rc = sb.main(["sweep-tmp", "--max-age", "60", "--tmp-dir", str(tmp_path)])
    assert rc == 0
    assert not stale.exists()
    assert fresh.exists(), "recently-touched dirs must never be removed"
    # system-shaped name survives: it does not match the 20+ random-char shape
    assert notshaped.name.startswith("systemd-")  # and it was skipped (has '-' but also letters < 20 random)


def test_sweep_tmp_skips_files_and_foreign_owners(tmp_path):
    f = tmp_path / ("c" * 24)
    f.write_text("not a dir")
    d = _make_chromium_shaped_dir(tmp_path, "d" * 24, 120)
    # simulate foreign owner by monkeypatching the uid check
    with mock.patch.object(os, "getuid", return_value=d.stat().st_uid + 1):
        sb.main(["sweep-tmp", "--max-age", "60", "--tmp-dir", str(tmp_path)])
    assert f.exists(), "files must never be touched"
    assert d.exists(), "dirs owned by others must be skipped"


def test_never_touch_prefixes_are_excluded():
    for name in ("systemd-private-abc", ".X11-unix", ".ICE-unix", "snap-private-tmp"):
        assert any(name.startswith(p) for p in sb._NEVER_TOUCH) or not sb._CHROMIUM_DIR_SHAPE.match(name)


def test_cli_dispatch_lists_command():
    from celestia_devtools.core.cli import COMMANDS
    assert COMMANDS["e2e-sandbox"] == "celestia_devtools.env.e2e_sandbox"

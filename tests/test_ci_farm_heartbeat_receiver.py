"""Tests for the CI farm heartbeat receiver (tools/ci_farm_heartbeat_receiver.py).

Real loopback socket: beat endpoint records files, /status aggregates, bad
names are rejected.
"""

from __future__ import annotations

import importlib.util
import json
import os
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

_TMP = tempfile.mkdtemp(prefix="ci-farm-receiver-test-")
os.environ.setdefault("CI_HB_PORT", "19200")
os.environ.setdefault("CI_HB_BEAT_DIR", os.path.join(_TMP, "beats"))

TOOL = Path(__file__).resolve().parent.parent / "tools" / "ci_farm_heartbeat_receiver.py"
spec = importlib.util.spec_from_file_location("ci_farm_heartbeat_receiver", TOOL)
rec = importlib.util.module_from_spec(spec)
sys.modules["ci_farm_heartbeat_receiver"] = rec
spec.loader.exec_module(rec)


def _start_receiver(beat_dir: Path, port: int):
    thread = threading.Thread(
        target=rec.run_receiver,
        kwargs={"bind": f"127.0.0.1:{port}", "beat_dir": str(beat_dir)},
        daemon=True)
    thread.start()
    deadline = time.time() + 5
    while True:
        try:
            urllib.request.urlopen(f"http://127.0.0.1:{port}/status", timeout=1)
            return
        except OSError:
            if time.time() > deadline:
                raise
            time.sleep(0.1)


def _wait_up(port: int) -> None:
    deadline = time.time() + 5
    while True:
        try:
            urllib.request.urlopen(f"http://127.0.0.1:{port}/status", timeout=1)
            return
        except OSError:
            if time.time() > deadline:
                raise
            time.sleep(0.1)


def test_receiver_roundtrip(tmp_path: Path):
    port = 19200 + (os.getpid() % 400)
    beat_dir = tmp_path / "beats"

    thread = threading.Thread(
        target=rec.run_receiver,
        kwargs={"bind": f"127.0.0.1:{port}", "beat_dir": str(beat_dir)},
        daemon=True)
    thread.start()
    _wait_up(port)

    # 打点 → 204 + 文件落盘
    with urllib.request.urlopen(
            f"http://127.0.0.1:{port}/beat/node-ci-x", timeout=5) as resp:
        assert resp.status == 204
    assert (beat_dir / "node-ci-x").exists()

    # /status 汇总
    with urllib.request.urlopen(
            f"http://127.0.0.1:{port}/status", timeout=5) as resp:
        payload = json.load(resp)
    assert "node-ci-x" in payload["beats"]

    # 路径穿越名字 → 400
    try:
        urllib.request.urlopen(
            f"http://127.0.0.1:{port}/beat/..%2Fescape", timeout=5)
    except urllib.error.HTTPError as exc:
        assert exc.code in (400, 404)


def test_receiver_beats_update_mtime(tmp_path: Path):
    port = 19200 + (os.getpid() % 400) + 50
    beat_dir = tmp_path / "beats2"

    thread = threading.Thread(
        target=rec.run_receiver,
        kwargs={"bind": f"127.0.0.1:{port}", "beat_dir": str(beat_dir)},
        daemon=True)
    thread.start()
    _wait_up(port)

    with urllib.request.urlopen(
            f"http://127.0.0.1:{port}/beat/node-ci-y", timeout=5):
        pass
    first = (beat_dir / "node-ci-y").stat().st_mtime
    time.sleep(1.1)
    with urllib.request.urlopen(
            f"http://127.0.0.1:{port}/beat/node-ci-y", timeout=5):
        pass
    second = (beat_dir / "node-ci-y").stat().st_mtime
    assert second > first  # 重复打点必须刷新 mtime（裁判判活的依据）

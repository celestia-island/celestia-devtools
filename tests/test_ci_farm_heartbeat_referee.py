#!/usr/bin/env python3
"""Tests for the CI farm heartbeat referee (tools/ci_farm_heartbeat_referee.py).

Covers the decision matrix (fresh/stale/trigger, budget, cooldown, snapshot
invariant), the probe-failure exponential backoff, first-beat registration
(never touch a node without a beat file), and the post-revert re-arm.
"""

from __future__ import annotations

import importlib.util
import json
import os
import sys
import time
import tempfile
from pathlib import Path

import pytest

# 模块在 import 时对 CI_HB_* 做必填校验 —— 先给全套临时值（fixture 再覆盖属性）。
_TMP = tempfile.mkdtemp(prefix="ci-farm-referee-test-")
for _name in ("CI_HB_ESXI_HOST", "CI_HB_BEAT_DIR", "CI_HB_STATE_FILE",
              "CI_HB_LEDGER_FILE", "CI_HB_LOCK_FILE", "CI_HB_INVENTORY",
              "CI_HB_GUEST_PASSWORD_FILE", "CI_HB_NODE1_ADDR",
              "CI_HB_ESXI_KEY_FILE"):
    os.environ.setdefault(_name, os.path.join(_TMP, _name))

TOOL = Path(__file__).resolve().parent.parent / "tools" / "ci_farm_heartbeat_referee.py"
spec = importlib.util.spec_from_file_location("ci_farm_heartbeat_referee", TOOL)
ref = importlib.util.module_from_spec(spec)
sys.modules["ci_farm_heartbeat_referee"] = ref
spec.loader.exec_module(ref)


class FakeEsxi:
    """Records issued command keys; snapshot/probe outcomes are scripted."""

    def __init__(self) -> None:
        self.commands: list[str] = []
        self.snaps: dict[int, tuple[bool, str]] = {}
        self.probe_ok = True

    def set_snaps(self, vmid: int, ok: bool, count: int) -> None:
        self.snaps[vmid] = (ok, "".join(
            f"--Snapshot Name : base{i}\n--Snapshot Id : {i}\n" for i in range(count)))

    def __call__(self, command_key: str, vmid: int, remote_cmd: str,
                 timeout: int | None = None):
        self.commands.append(f"{command_key} {vmid}")
        if command_key == "probe":
            return self.probe_ok, ""
        if command_key == ref.CMD_SNAPSHOTS:
            ok, out = self.snaps.get(vmid, (True, ""))
            return ok, out
        if command_key == ref.CMD_RECOVER:
            return True, "Powered on"
        return False, ""


@pytest.fixture()
def farm(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict:
    """Isolated farm: beat dir + state/ledger files + fake hypervisor + spies."""
    root = tmp_path / "farm"
    beats = root / "beats"
    beats.mkdir(parents=True)

    monkeypatch.setattr(ref, "BEAT_DIR", str(beats))
    monkeypatch.setattr(ref, "STATE_FILE", str(root / "state.json"))
    monkeypatch.setattr(ref, "LEDGER_FILE", str(root / "ledger.json"))
    monkeypatch.setattr(ref, "LOCK_FILE", str(root / "lock"))
    monkeypatch.setattr(ref, "INVENTORY_FILE", str(root / "inventory.json"))
    monkeypatch.setattr(ref, "ESXI_HOST", "hypervisor.test")

    (root / "inventory.json").write_text(json.dumps([
        {"name": "node-ci-3", "vmid": 14, "guest_ip": "192.0.2.19"},
    ]))

    calls: list[str] = []

    class FakeEsxi:
        probe_ok = True

        def __init__(self) -> None:
            self.snaps: dict[int, tuple[bool, str]] = {}

        def set_snaps(self, vmid: int, ok: bool, count: int) -> None:
            self.snaps[vmid] = (ok, "".join(
                f"--Snapshot Name : base{i}\n--Snapshot Id : {i}\n" for i in range(count)))

        def __call__(self, command_key: str, vmid: int, remote_cmd: str,
                     timeout: int | None = None):
            calls.append(f"{command_key} {vmid}")
            if command_key == "probe":
                return self.probe_ok, ""
            if command_key == ref.CMD_SNAPSHOTS:
                ok, out = self.snaps.get(vmid, (True, ""))
                return ok, out
            if command_key == ref.CMD_RECOVER:
                return True, "Powered on"
            return False, ""

    fake = FakeEsxi()
    monkeypatch.setattr(ref, "run_esxi", fake)

    rearm_calls: list[str] = []
    monkeypatch.setattr(ref, "rearm_guest",
                        lambda name, opts: rearm_calls.append(name))

    clock = {"t": 10000.0}
    monkeypatch.setattr(ref, "now_ts", lambda: clock["t"])

    def referee() -> int:
        return ref.main([])

    def read_state() -> dict:
        return json.loads(Path(ref.STATE_FILE).read_text())

    return {"fake": fake, "calls": calls, "referee": referee,
            "read_state": read_state, "rearm_calls": rearm_calls,
            "clock": clock, "beats": beats}


def set_beat(beats: Path, name: str, age_sec: float) -> None:
    path = beats / name
    path.write_text(str(int(time.time())))
    t = time.time() - age_sec
    os.utime(path, (t, t))


# ── 决策纯函数 ───────────────────────────────────────────────────────────────
def test_decide_fresh_resets_stale():
    d, s, _ = ref.decide("green", 2, 0, None, True, "", 1000.0)
    assert (d, s) == ("ok", 0)


def test_decide_single_stale_does_not_trigger():
    d, s, _ = ref.decide("red", 0, 0, None, True, "", 1000.0)
    assert (d, s) == ("stale", 1)


def test_decide_two_stale_rounds_trigger():
    d, s, reason = ref.decide("red", 1, 0, None, True, "", 1000.0)
    assert d == "trigger" and s == 0 and "forced rollback" in reason


def test_decide_snapshot_invariant_broken_blocks():
    d, _, reason = ref.decide("red", 2, 0, None, False, "snapshot-count=3", 1000.0,
                              cooldown=0)
    assert d == "refused-snapshot" and "count=3" in reason


def test_decide_budget_exhausted_blocks():
    now = 1000.0
    d, _, reason = ref.decide("red", 2, 3, None, True, "", now, cooldown=0)
    assert d == "refused-budget" and "24h" in reason


def test_decide_cooldown_blocks():
    d, _, _ = ref.decide("red", 2, 0, 1000.0 - 100, True, "", 1000.0)
    assert d == "refused-cooldown"


def test_reverts_window_filters_old_entries():
    now = 1000.0
    mixed = [now - 3600, now - 25 * 3600, now - 7200]  # 1 条在窗内，2 条窗外
    assert ref.reverts_in_last_24h(mixed, now) == 2
    assert ref.reverts_in_last_24h([], now) == 0


def test_decide_old_reverts_do_not_count():
    # 调用方先用 reverts_in_last_24h 过滤窗口，decide 只看过滤后的计数。
    now = 1000.0
    old = [now - 25 * 3600] * 3  # 3 次，但全部超出 24h 窗口
    in_window = ref.reverts_in_last_24h(old, now)
    assert in_window == 0
    d, _, _ = ref.decide("red", 2, in_window, None, True, "", now, cooldown=0)
    assert d == "trigger"


def test_decide_mutation_boundary_threshold_5():
    d, _, _ = ref.decide("red", 1, 0, None, True, "", 1000.0, stale_threshold=5)
    assert d == "stale"


def test_decide_missing_signal_counts_as_stale():
    d, s, _ = ref.decide(None, 0, 0, None, True, "", 1000.0)
    assert (d, s) == ("stale", 1)


# ── 裁判端到端（fake hypervisor + 真 beat 文件）──────────────────────────────
def test_referee_stale_then_triggers_real_revert(farm, tmp_path):
    farm["fake"].set_snaps(14, True, 1)
    set_beat(farm["beats"], "node-ci-3", 400)

    assert farm["referee"]() == 0  # 第一轮：stale 计数，不触发
    assert [c for c in farm["calls"] if c.startswith(f"{ref.CMD_RECOVER} 14")] == []

    assert farm["referee"]() == 0  # 第二轮：触发真实回滚
    assert [c for c in farm["calls"] if c.startswith(f"{ref.CMD_RECOVER} 14")]
    assert farm["rearm_calls"] == ["node-ci-3"]

    state = farm["read_state"]()
    assert state["node-ci-3"]["stale_polls"] == 0


def test_referee_never_reverts_fresh_guest(farm, tmp_path):
    set_beat(farm["beats"], "node-ci-3", 10)
    assert farm["referee"]() == 0
    assert [c for c in farm["calls"] if c.startswith(f"{ref.CMD_RECOVER} 14")] == []


def test_referee_never_touches_unregistered_nodes(farm, tmp_path):
    # ghost 节点（node-ci-x）从未打过心跳 → 不在监管集合 → 完全不被触碰
    set_beat(farm["beats"], "node-ci-x", 400)
    set_beat(farm["beats"], "node-ci-3", 10)
    assert farm["referee"]() == 0
    assert all(not c.startswith(f"{ref.CMD_SNAPSHOTS} 99") for c in farm["calls"])


# ── 探活失败指数退避 ─────────────────────────────────────────────────────────
def test_referee_probe_failure_backs_off(farm, tmp_path, monkeypatch):
    farm["fake"].probe_ok = False
    set_beat(farm["beats"], "node-ci-3", 400)

    clock = {"t": 10000.0}
    monkeypatch.setattr(ref, "now_ts", lambda: clock["t"])

    assert farm["referee"]() == 2  # 第 1 轮：探活失败 exit 2，内部记录退避
    state = farm["read_state"]()
    assert state["probe_fail_streak"] == 1
    assert abs(state["probe_backoff_until"] - 10060.0) < 1

    # 退避期内：静默零探测
    calls_before = len(farm["calls"])
    clock["t"] = 10030.0
    assert farm["referee"]() == 0
    assert len(farm["calls"]) == calls_before

    # 退避过期：重探仍失败 → 退避翻倍
    clock["t"] = 10061.0
    assert farm["referee"]() == 2  # 退避过期重探仍失败 → exit 2
    state = json.loads(Path(ref.STATE_FILE).read_text())
    assert state["probe_fail_streak"] == 2
    assert abs(state["probe_backoff_until"] - 10181.0) < 1

    # 恢复：探活成功 → streak 清零，guest 正常被处理
    farm["fake"].probe_ok = True
    clock["t"] = 10200.0
    assert farm["referee"]() == 0
    state = json.loads(Path(ref.STATE_FILE).read_text())
    assert state.get("probe_fail_streak", 0) == 0


# ── 心跳单元 URL 归一化（2026-09-20 事故回归） ────────────────────────────────
#
# 事故：node-ci-3 revert 后自动 re-arm，写出的单元是
#   ExecStart=... http://http://192.0.2.14:9120/beat/node-ci-3
# curl 退出码 6（无法解析主机 "http"），beat 一分钟都没成功过 ⇒ 心跳文件停在旧时间戳
# ⇒ 裁判每分钟判该节点 stale，又被 24h 预算挡住（3/3）⇒ 一直打印 needs human。
# 而节点本身是健康的：文件里那个 `http://` 是模板加的，环境变量里**也已经有一个**。
# 自愈机制写出了故障本身 —— 四台里只有被 revert 过的那台中招。


@pytest.mark.parametrize(
    "configured",
    [
        "http://192.0.2.14:9120",   # 已部署单元的真实取值
        "192.0.2.14:9120",          # 旧形态（只是地址）
        "https://192.0.2.14:9120",  # 换 scheme 也不该叠加
        "http://192.0.2.14:9120/",  # 尾斜杠不该产生 //
        "  192.0.2.14:9120  ",      # 容错两侧空白
    ],
)
def test_receiver_url_normalises_every_configured_spelling(monkeypatch, configured):
    monkeypatch.setattr(ref, "NODE1_ADDR", configured)
    assert ref.receiver_url() == "http://192.0.2.14:9120"


def test_beat_unit_contains_exactly_one_scheme(monkeypatch):
    """承重断言：写进 guest 的 ExecStart 里 scheme 只能出现一次。

    只断言 `receiver_url()` 会漏掉模板那一侧 —— 缺陷正是"两边各加一次"，
    所以这里断言**最终单元文本**。
    """
    monkeypatch.setattr(ref, "NODE1_ADDR", "http://192.0.2.14:9120")
    unit = ref.BEAT_SERVICE_TEMPLATE.format(receiver_url=ref.receiver_url(), name="node-ci-3")
    exec_line = next(line for line in unit.splitlines() if line.startswith("ExecStart="))
    assert exec_line.count("http://") == 1, exec_line
    assert exec_line.endswith("/beat/node-ci-3")
    assert "http://http" not in unit


def test_deploy_writes_a_working_url(monkeypatch):
    """端到端：装出来的单元必须能被 curl 直接解析（把两处拼接都走一遍）。"""
    monkeypatch.setattr(ref, "NODE1_ADDR", "http://192.0.2.14:9120")
    sent: list[str] = []

    def fake_run_guest(guest_ip, remote_cmd, stdin_payload=None):
        if stdin_payload is not None:
            sent.append(stdin_payload)
        return True, ""

    monkeypatch.setattr(ref, "run_guest", fake_run_guest)
    ok, _ = ref.deploy_guest_beat_unit("192.0.2.109", "node-ci-3")
    assert ok
    unit = next(payload for payload in sent if "ExecStart=" in payload)
    assert "http://192.0.2.14:9120/beat/node-ci-3" in unit
    assert unit.count("http://") == 1

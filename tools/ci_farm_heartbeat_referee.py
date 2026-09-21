#!/usr/bin/env python3
"""ci_heartbeat_revert.py — CI 农场心跳裁判：guest 心跳丢失 → ESXi 强制回滚 baseline + 重启。

设计（2026-09-17，node-ci-2/3 双双挂掉次日的落地）：
  分层修复的 Tier 2。Tier 1 = ci-farm-watch（SSH 进 guest，细粒度：load/D 态/runner unit，
  可修 runner 服务）。本工具 = 裁判：读每个 guest 每 60 秒打到 node-1 接收器
  （ci_heartbeat_receiver.py，端口 9120）的心跳文件 mtime 判活；超时（STALE_SEC，默认
  300s ≈ 5 次失联）→ 经 ESXi 强制回滚 baseline + 开机。

  信号载体的实测取舍：ESXi 原生 guestHeartBeatStatus（vim-cmd get.summary）确实会随
  vmtoolsd 停止在 20–40s 内 green→red，但该字段在 vim-cmd 输出中的**存在性**本身分钟级
  波动（实测 0/20 连续缺失，全 4 台 VM 同状）——不能当每分钟轮询的载体，故否决。
  beat 文件方案：guest 网络出方向通即可（不需要 guest sshd——node-ci-3 事故里 Tier 1
  恰好被卡死的 guest 玩弄于股掌），mtime 一目了然。

  触发动作（§8.1 恢复动作，实测可用）：power.off → snapshot.revert <vmid> <baselineId> 1
  → power.on。节点无状态（§8.1 用户定案）：revert 丢改动即设计。

安全护栏（全部硬性）：
  1. 白名单 + 首跳注册：只操作「曾打过心跳」的 guest（beat 文件存在）；从未部署 beat
     单元的节点（如暂不可 SSH 的 node-ci-3）天然不受监管、也绝不会被误回滚。
  2. 快照不变量：目标 VM 必须恰好 1 个快照（node-ci 的 baseline 纪律）；0 或 ≥2 拒绝并告警
     （≥2 = 有人加了快照，属 §10.2 待修状态，回滚会选错基线）。
  3. 预算：每 VM 滚动 24h 最多 revert_budget 次（默认 3）；超限只告警不再回滚
     （反复回滚 = 系统性故障，回滚循环只会烧光 CI 吞吐）。
  4. 冷却：动作后 cooldown 秒内该 VM 不再触发（guest 启动 + runner 重注册 ≈ 2–3 分钟；
     STALE_SEC=300 > 启动时间，正常重启不会在启动期被二次回滚）。
  5. 心跳源不可达（ESXi SSH 失败）= 全局跳过本轮：强制通道瞎了不能执行回滚。
  6. 串行：flock 锁，防与自身/看门狗并发动作。

凭据只走 600 权限的文件（env 指定路径），绝不写死；SSH 一律 farm 标准参数（StrictHostKeyChecking=no + UserKnownHostsFile=/dev/null
  —— revert 轮换 guest host key 后不得失聪）；状态/台账路径由 env 指定；
  支持 --dry-run 与 CI_HB_FIXTURE 注入（selftest 用）。

已知边界：node-ci-3 尚未部署 beat 单元（baseline 镜像拒绝 node-1 的密码 SSH），暂不受
  监管——CI runner 本身可用；待通过 console/CI 通道补装后自动纳入。
"""

from __future__ import annotations

import fcntl
import json
import os
import re
import subprocess
import sys
import time
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

# ── 约定路径与默认值 ────────────────────────────────────────────────────────────
def _require(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        raise SystemExit(f"ci-farm-heartbeat referee: missing required env {name}")
    return value


ESXI_HOST = _require("CI_HB_ESXI_HOST")
ESXI_USER = os.environ.get("CI_HB_ESXI_USER", "root")
BEAT_DIR = _require("CI_HB_BEAT_DIR")
STALE_SEC = int(os.environ.get("CI_HB_STALE_SEC", "300"))
STATE_FILE = _require("CI_HB_STATE_FILE")
LEDGER_FILE = _require("CI_HB_LEDGER_FILE")
LOCK_FILE = _require("CI_HB_LOCK_FILE")
INVENTORY_FILE = _require("CI_HB_INVENTORY")
GUEST_PASSWORD_FILE = _require("CI_HB_GUEST_PASSWORD_FILE")
GUEST_USER = os.environ.get("CI_HB_GUEST_USER", "lab")

STALE_ROUNDS = int(os.environ.get("CI_HB_STALE_ROUNDS", "2"))
NAME_OK_RE = re.compile(r"^[a-zA-Z0-9_-]+$")  # beat 文件名 = guest 名
REVERT_BUDGET_24H = int(os.environ.get("CI_HB_REVERT_BUDGET_24H", "3"))
COOLDOWN_SEC = int(os.environ.get("CI_HB_COOLDOWN_SEC", "900"))
REARM_WAIT_SEC = int(os.environ.get("CI_HB_REARM_WAIT_SEC", "240"))  # 等 guest 起来
REARM_TRIES = int(os.environ.get("CI_HB_REARM_TRIES", "8"))
CONNECT_TIMEOUT = int(os.environ.get("CI_HB_CONNECT_TIMEOUT", "20"))

# ESXi 命令短键（fixture 模式按 key 读伪输出；真实模式拼成 vim-cmd 命令）。
CMD_SNAPSHOTS = "snaps"
CMD_RECOVER = "recover"

FIXTURE_DIR = os.environ.get("CI_HB_FIXTURE", "")
RECORD_FILE = os.environ.get("CI_HB_RECORD_FILE", "")  # fixture 模式下记录发出的命令 key

ESXI_KEY_FILE = _require("CI_HB_ESXI_KEY_FILE")

# ESXi 强制通道：RSA 公钥认证（BatchMode，无 sshpass/密码）。
# 为什么不用密码：ESXi 7 开着 FIPS（ed25519 被拒）+ SecurityAccountLockout
# （密码方式的失败尝试会锁 root，锁定期内正确密码也被拒）。
# 部署：node-1 的 id_rsa_esxi.pub 已装入 /etc/ssh/keys-root/authorized_keys。
SSH_BASE = [
    "ssh",
    "-i",
    ESXI_KEY_FILE,
    "-o",
    "BatchMode=yes",
    "-o",
    f"ConnectTimeout={CONNECT_TIMEOUT}",
    "-o",
    "StrictHostKeyChecking=no",
    "-o",
    "UserKnownHostsFile=/dev/null",
    "-o",
    "LogLevel=ERROR",
    f"{ESXI_USER}@{ESXI_HOST}",
]


def now_ts() -> float:
    return time.time()


def iso(ts: float) -> str:
    return datetime.fromtimestamp(ts, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def load_json(path: str, default: Any) -> Any:
    try:
        with open(path, "r", encoding="utf-8") as fh:
            return json.load(fh)
    except FileNotFoundError:
        return default
    except (json.JSONDecodeError, OSError):
        return default


def save_json(path: str, data: Any) -> None:
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(data, fh, ensure_ascii=False, indent=1)
    os.replace(tmp, path)


def log(msg: str) -> None:
    print(f"[{iso(now_ts())}] {msg}", flush=True)


def _read_secret(path: str) -> str:
    with open(path, "r", encoding="utf-8") as fh:
        return fh.read().strip()


def run_esxi(command_key: str, vmid: int, remote_cmd: str, timeout: Optional[int] = None) -> Tuple[bool, str]:
    """在 ESXi 上执行一条 vim-cmd 命令。fixture 模式读伪输出并记录 key。"""
    if FIXTURE_DIR:
        if RECORD_FILE:
            with open(RECORD_FILE, "a", encoding="utf-8") as fh:
                fh.write(f"{command_key} {vmid}\n")
        path = os.path.join(FIXTURE_DIR, f"{command_key}.{vmid}")
        try:
            with open(path, "r", encoding="utf-8") as fh:
                return True, fh.read()
        except FileNotFoundError:
            return False, ""
    try:
        proc = subprocess.run(
            SSH_BASE + [remote_cmd],
            capture_output=True,
            text=True,
            timeout=timeout or (CONNECT_TIMEOUT + 30),
        )
        return proc.returncode == 0, proc.stdout
    except subprocess.TimeoutExpired:
        return False, ""


# ── 采集 ────────────────────────────────────────────────────────────────────────
def read_beat(name: str) -> Optional[str]:
    """beat 文件存在 → "green"（新鲜）/"red"（超时）；无文件 = None（未纳管，跳过）。

    ESXi 不可达时对 beat 判活无影响——beat 是 guest→node-1 的本地文件；但触发后的
    强制动作需要 ESXi，所以 main() 仍先探活 ESXi。
    """
    path = os.path.join(BEAT_DIR, name)
    try:
        age = time.time() - os.path.getmtime(path)
    except FileNotFoundError:
        return None
    return "green" if age <= STALE_SEC else "red"


SNAP_NAME_RE = re.compile(r"Snapshot Name\s*:\s*(\S+)")
SNAP_ID_RE = re.compile(r"Snapshot Id\s*:\s*(\d+)")


def snapshot_baseline(vmid: int) -> Tuple[bool, str, Optional[int]]:
    """快照不变量：恰好 1 个快照时返回 (True, 名称, id)；否则 (False, 原因, None)。"""
    ok, out = run_esxi(
        CMD_SNAPSHOTS,
        vmid,
        f"vim-cmd vmsvc/snapshot.get {vmid} 2>/dev/null",
    )
    if not ok:
        return False, "esxi-error", None
    names = SNAP_NAME_RE.findall(out)
    ids = SNAP_ID_RE.findall(out)
    if len(names) == 1 and len(ids) == 1:
        return True, names[0], int(ids[0])
    return False, f"snapshot-count={len(names)}", None


def recover_vm(vmid: int, snap_id: Optional[int]) -> Tuple[bool, str]:
    """§8.1 恢复动作：power.off → snapshot.revert → power.on。fixture 模式只记录。"""
    snap_arg = str(snap_id if snap_id is not None else 1)
    remote = (
        f"vim-cmd vmsvc/power.off {vmid} 2>/dev/null; "
        f"vim-cmd vmsvc/snapshot.revert {vmid} {snap_arg} 1; "
        f"sleep 5; vim-cmd vmsvc/power.on {vmid}; "
        f"vim-cmd vmsvc/power.getstate {vmid} | tail -1"
    )
    # 回滚序列含 sleep + 两轮 hostd 操作，50s 默认超时不够 → 单独放宽。
    ok, out = run_esxi(
        CMD_RECOVER,
        vmid,
        remote,
        timeout=CONNECT_TIMEOUT + 90,
    )
    return ok, out if ok else out + "recover-timeout-or-ssh-failure"


# ── guest 侧 beat 单元自愈部署 ──────────────────────────────────────────────────
BEAT_SERVICE_TEMPLATE = """[Unit]
Description=CI farm heartbeat beat

[Service]
Type=oneshot
ExecStart=/usr/bin/curl -fsS -m 10 -o /dev/null {receiver_url}/beat/{name}

[Install]
WantedBy=timers.target
"""
BEAT_TIMER_UNIT = """[Unit]
Description=Beat the CI farm heartbeat receiver every minute

[Timer]
OnBootSec=45s
OnUnitActiveSec=60s
AccuracySec=10s

[Install]
WantedBy=timers.target
"""
NODE1_ADDR = _require("CI_HB_NODE1_ADDR")


def run_guest(guest_ip: str, remote_cmd: str, stdin_payload: Optional[str] = None) -> Tuple[bool, str]:
    """SSH 进 guest 执行命令（farm 标准参数；revert 会轮换 guest host key，
    故 known_hosts 必须 /dev/null）。"""
    password = _read_secret(GUEST_PASSWORD_FILE)
    cmd = [
        "sshpass",
        "-p",
        password,
        "ssh",
        "-o",
        f"ConnectTimeout={CONNECT_TIMEOUT}",
        "-o",
        "StrictHostKeyChecking=no",
        "-o",
        "UserKnownHostsFile=/dev/null",
        "-o",
        "LogLevel=ERROR",
        f"{GUEST_USER}@{guest_ip}",
        remote_cmd,
    ]
    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            input=stdin_payload,
            timeout=CONNECT_TIMEOUT + 30,
        )
        return proc.returncode == 0, proc.stdout
    except subprocess.TimeoutExpired:
        return False, ""


def receiver_url() -> str:
    """The receiver base URL, tolerant of the scheme being supplied either way.

    ``CI_HB_NODE1_ADDR`` is documented as an address but the deployed referee unit carries
    ``http://192.0.2.14:9120``, and the template used to prepend ``http://`` itself. The
    two disagreed silently: on 2026-09-19 node-ci-3 was re-armed after a revert and got

        ExecStart=... curl ... http://http://192.0.2.14:9120/beat/node-ci-3

    curl exits 6 (cannot resolve host "http") in under a second, so the beat never reaches
    the receiver -- and because the heartbeat is the referee's own liveness signal, the
    referee then judged that node stale every minute, refused to revert it
    (``revert-budget-exhausted 3/3``), and logged "needs human" indefinitely while the node
    itself was healthy. **The self-heal wrote the breakage**: nodes whose unit predated the
    environment change kept the correct line, which is why only one of four was affected.

    Normalising here means both spellings work, and a future change to the environment
    cannot reintroduce a doubled scheme.
    """
    value = NODE1_ADDR.strip()
    for prefix in ("http://", "https://"):
        if value.startswith(prefix):
            value = value[len(prefix) :]
            break
    return "http://" + value.rstrip("/")


def deploy_guest_beat_unit(guest_ip: str, name: str) -> Tuple[bool, str]:
    """把 beat 单元装进 guest 并启用（revert 后 baseline 里没有它）。"""
    service = BEAT_SERVICE_TEMPLATE.format(receiver_url=receiver_url(), name=name)
    ok1, out1 = run_guest(
        guest_ip,
        "sudo -n tee /etc/systemd/system/ci-beat.service >/dev/null",
        stdin_payload=service,
    )
    ok2, out2 = run_guest(
        guest_ip,
        "sudo -n tee /etc/systemd/system/ci-beat.timer >/dev/null",
        stdin_payload=BEAT_TIMER_UNIT,
    )
    ok3, out3 = run_guest(
        guest_ip,
        "sudo -n systemctl daemon-reload && sudo -n systemctl enable --now ci-beat.timer",
    )
    if ok1 and ok2 and ok3:
        return True, "beat unit deployed"
    return False, f"deploy failed (service={ok1} timer={ok2} enable={ok3}) {out1}{out2}{out3}"[:200]


def rearm_guest(name: str, opts: Dict[str, Any]) -> None:
    """revert 后自愈：等 guest 起来，把 beat 单元重装回去，让监管自动恢复。"""
    guest_ip = opts.get("guest_ip")
    if not guest_ip:
        log(f"{name}: no guest_ip in inventory — cannot re-arm; ALERT: node silently unmonitored")
        return
    for attempt in range(1, REARM_TRIES + 1):
        time.sleep(max(1, REARM_WAIT_SEC // REARM_TRIES))
        ok, _ = run_guest(guest_ip, "systemctl is-active --quiet open-vm-tools")
        if not ok:
            continue
        ok2, _ = deploy_guest_beat_unit(guest_ip, name)
        if ok2:
            log(f"{name}: beat unit re-armed after revert (attempt {attempt})")
            return
    log(f"{name}: ALERT — could not re-arm beat unit after revert; node silently unmonitored")


# ── 纯决策函数（selftest 的主靶）──────────────────────────────────────────────
def decide(
    hb: Optional[str],
    stale_polls: int,
    reverts_24h: int,
    last_action_ts: Optional[float],
    snap_ok: bool,
    snap_reason: str,
    now_ts_: float,
    stale_threshold: int = STALE_ROUNDS,
    budget: int = REVERT_BUDGET_24H,
    cooldown: int = COOLDOWN_SEC,
) -> Tuple[str, int, str]:
    """返回 (decision, new_stale_polls, reason)。

    decision ∈ {"ok", "stale", "trigger", "refused-budget", "refused-cooldown",
                "refused-snapshot"}。hb 非 "green"（red/gray/None=字段缺失）一律
    计入 stale——探活已前置，ESXi 可达时字段缺失本身就是异常。
    """
    if hb == "green":
        return "ok", 0, "heartbeat-green"

    stale = stale_polls + 1
    observed = hb if hb is not None else "missing"
    if stale < stale_threshold:
        return "stale", stale, f"heartbeat-{observed} ({stale}/{stale_threshold})"

    # 达到阈值 → 检查护栏（顺序：冷却 → 预算 → 快照不变量）。
    if last_action_ts is not None and (now_ts_ - last_action_ts) < cooldown:
        return "refused-cooldown", stale, "cooldown-active"
    if reverts_24h >= budget:
        return (
            "refused-budget",
            stale,
            f"revert-budget-exhausted ({reverts_24h}/{budget} in 24h) — needs human",
        )
    if not snap_ok:
        return "refused-snapshot", stale, f"snapshot-invariant-broken ({snap_reason})"

    return "trigger", 0, f"heartbeat-{observed} x{stale} — forced rollback"


def reverts_in_last_24h(revert_ts_list: List[float], now_ts_: float) -> int:
    horizon = now_ts_ - 24 * 3600
    return sum(1 for ts in revert_ts_list if ts >= horizon)


# ── 主流程 ──────────────────────────────────────────────────────────────────────
def process_vm(
    name: str,
    vmid: int,
    state: Dict[str, Any],
    actions: List[Dict[str, Any]],
    opts: Dict[str, Any],
    guest_ip: Optional[str] = None,
) -> None:
    # 首跳注册制：从未打过心跳的节点 = 未部署 beat 单元 = 不受监管。
    # 绝不对它做任何判定/动作（防误伤未纳管节点）。
    if not os.path.isfile(os.path.join(BEAT_DIR, name)):
        return
    now_ts_ = now_ts()
    entry = state.setdefault(name, {"stale_polls": 0, "reverts": [], "last_action_ts": None})
    stale_polls = int(entry.get("stale_polls", 0))
    reverts = [ts for ts in entry.get("reverts", []) if isinstance(ts, (int, float))]
    last_action_ts = entry.get("last_action_ts")

    hb = read_beat(name)
    decision, new_stale, reason = decide(
        hb,
        stale_polls,
        reverts_in_last_24h(reverts, now_ts_),
        last_action_ts,
        True,
        "",
        now_ts_,
        stale_threshold=opts["stale_rounds"],
        budget=opts["budget"],
        cooldown=opts["cooldown"],
    )

    if decision == "trigger":
        snap_ok, snap_name, snap_id = snapshot_baseline(vmid)
        if not snap_ok:
            decision, reason = "refused-snapshot", f"snapshot-invariant-broken ({snap_name})"

    entry["stale_polls"] = new_stale
    entry["last_hb"] = hb
    entry["last_decision"] = decision
    entry["last_reason"] = reason
    entry["last_seen_ts"] = now_ts_

    if decision == "trigger":
        if opts["dry_run"]:
            log(f"[dry-run] {name}(vmid={vmid}): would REVERT to baseline ({reason})")
            actions.append({"vm": name, "action": "would-revert", "reason": reason})
            return  # dry-run 不清零 stale，便于连续观察
        ok, out = recover_vm(vmid, snap_id)
        reverts.append(now_ts_)
        entry["reverts"] = reverts[-10:]
        entry["last_action_ts"] = now_ts_
        if ok:
            log(f"{name}(vmid={vmid}): REVERTED to baseline ({reason}); out={out.strip()[:120]}")
            actions.append({"vm": name, "action": "reverted", "reason": reason})
            # 自愈：baseline 里没有 beat 单元，revert 后必须重装，否则该节点
            # 静默脱离监管（机制会在首次真实触发后自我失效）。
            rearm_guest(name, {**opts, "guest_ip": guest_ip})
        else:
            log(f"{name}(vmid={vmid}): REVERT command FAILED ({reason})")
            actions.append({"vm": name, "action": "revert-failed", "reason": reason})
        return

    if decision != "ok":
        log(f"{name}(vmid={vmid}): {decision} — {reason}")
        actions.append({"vm": name, "action": decision, "reason": reason})


def main(argv: List[str]) -> int:
    dry_run = "--dry-run" in argv
    verbose = "--verbose" in argv
    opts = {
        "dry_run": dry_run,
        "stale_rounds": STALE_ROUNDS,
        "budget": REVERT_BUDGET_24H,
        "cooldown": COOLDOWN_SEC,
    }

    lock_fh = open(LOCK_FILE, "w")
    try:
        fcntl.flock(lock_fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        log("another instance holds the lock — skipping this round")
        return 0

    # 护栏 5：强制通道探活。ESXi 不可达 → 整轮跳过（通道瞎了不能执行回滚）。
    # 锁定期内的每次探测（哪怕密码正确）都会被拒并刷新账号锁窗 —— 所以探活
    # 失败后必须指数退避静默，而不是每分钟硬敲（node-ci-3 次日僵局的教训）。
    state = load_json(STATE_FILE, {})
    actions: List[Dict[str, Any]] = []

    now_ts_ = now_ts()
    fail_streak = int(state.get("probe_fail_streak", 0))
    backoff_until = state.get("probe_backoff_until")
    if backoff_until and now_ts_ < float(backoff_until):
        lock_fh.close()
        if verbose:
            log(f"ESXi probe backoff until {iso(float(backoff_until))} — skipping quietly")
        return 0
    probe_ok, _ = run_esxi("probe", 0, "vim-cmd hostsvc/hostsummary 2>/dev/null")
    if not probe_ok:
        fail_streak += 1
        backoff = min(60 * (2 ** (fail_streak - 1)), 1800)
        state["probe_fail_streak"] = fail_streak
        state["probe_backoff_until"] = now_ts_ + backoff
        save_json(STATE_FILE, state)
        log(f"ESXi unreachable ({ESXI_HOST}) — probe failed x{fail_streak}, "
            f"backing off {backoff}s (enforcement channel down)")
        lock_fh.close()
        return 2
    state["probe_fail_streak"] = 0
    state.pop("probe_backoff_until", None)

    # 受监管集合 = 有心跳文件的 guest（首跳注册制：从未部署 beat 单元的节点不受
    # 监管、也绝不会被误回滚）；vmid 映射来自 inventory，缺映射的 beat 只告警。
    try:
        entries = sorted(os.listdir(BEAT_DIR))
    except FileNotFoundError:
        entries = []
    monitored = [e for e in entries if NAME_OK_RE.match(e)]
    inventory = {item.get("name"): item
                 for item in load_json(INVENTORY_FILE, []) if isinstance(item, dict)}

    if not monitored:
        log(f"no beats under {BEAT_DIR} — nothing monitored yet")
    for name in monitored:
        info = inventory.get(name) or {}
        vmid = info.get("vmid")
        if not isinstance(vmid, int):
            log(f"{name}: beat present but no inventory vmid — cannot enforce; skipped")
            continue
        process_vm(name, vmid, state, actions, opts, guest_ip=info.get("guest_ip"))

    save_json(STATE_FILE, state)
    save_json(LEDGER_FILE, {"ts": iso(now_ts()), "actions": actions, "state": state})
    lock_fh.close()

    if verbose:
        for a in actions:
            print(json.dumps(a, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))

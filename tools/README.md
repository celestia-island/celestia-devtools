# Engine packs (`tools/engine_pack.py`)

Declarative install/verify tool for local inference engine packs (LLM/ASR) on
GPU nodes. Single file, Python **3.11+ stdlib only** — no third-party
dependencies. Run it as root on the target node.

## Pack layout

```text
<pack-dir>/
├── engine.meta        # TOML manifest (required)
├── asr_server.py      # engine entrypoint — whatever the engine needs
└── ...                # remaining engine files (code, vendored binary, ...)
```

After `install`, the persistent footprint is exactly: one systemd unit in
`/etc/systemd/system`, the conda env prefix (none for native-binary packs) and
the pack directory itself. The tool can be deleted afterwards without
affecting the running service.

## `engine.meta` schema

| Section | Field | Type | Required | Notes |
|---|---|---|---|---|
| `[pack]` | `name` | string | yes | pack id; used for the env dir name |
| `[pack]` | `engine` | string | yes | free-form engine family tag |
| `[pack]` | `version` | string | yes | pack version |
| `[env]` | `python` | string | if `[env]` | python version for the conda env |
| `[env]` | `packages` | string list | if `[env]` | pip packages, pinned versions |
| `[service]` | `unit` | string | yes | systemd unit name → `<unit>.service` |
| `[service]` | `command` | string list | yes | argv, cwd = pack dir; first token may be `python` (→ env python) or an absolute path; with no `[env]` it must be absolute |
| `[service]` | `port` | integer | yes | local service port |
| `[service]` | `health_path` | string | yes | health path, starts with `/` |
| `[systemd]` | `user` | string | no | default `root` |
| `[systemd]` | `after` | string | no | default `network-online.target` |
| `[systemd]` | `description` | string | no | default derived from pack/engine |

Omit `[env]` entirely for native-binary engines (conda is skipped completely).

## Commands

```bash
python3 tools/engine_pack.py plan   <pack-dir>               # parse + print planned actions, offline
python3 tools/engine_pack.py install <pack-dir>              # env + unit + service + health check
python3 tools/engine_pack.py verify  <pack-dir> --timeout 60 # poll http://127.0.0.1:<port><health_path>
```

`install` options: `--miniforge-root`, `--env-root`, `--unit-dir`,
`--mirror-base`, `--force-env` (recreate an existing env), `--timeout`
(health check seconds, default 120). Exit code is non-zero when any step
fails; every step prints a `[PASS]`/`[SKIP]`/`[FAIL]` line
(conda / env / pip / unit / service / health).

## Miniforge bootstrap

- If `<miniforge-root>/bin/mamba` is missing, the installer
  (`Miniforge3-Linux-<arch>.sh`) is downloaded from the Tsinghua mirror
  `https://mirrors.tuna.tsinghua.edu.cn/github-release/conda-forge/miniforge/LatestRelease/`
  (direct connection, no proxy) and run as `bash <installer> -b -p <root>`.
- Default roots: miniforge `/mnt/work/miniforge3`, envs `/mnt/work/engine-envs`.
- Envs are created at an explicit prefix: `mamba create -y -p
  /mnt/work/engine-envs/<pack-name> python=<ver>`. `conda activate` is never
  used; the service runs `<prefix>/bin/python` and pip is
  `<prefix>/bin/pip`.

## Idempotency

Re-running `install` is safe: an existing env with matching packages is
skipped (the installed set is recorded in
`<prefix>/.engine-pack-installed.json` and compared against `engine.meta`),
the unit file is always re-rendered, and the service is reloaded, re-enabled
and restarted, followed by a fresh health check.

## CI farm heartbeat (`tools/ci_farm_heartbeat_referee.py` + `tools/ci_farm_heartbeat_receiver.py`)

Self-hosted CI farm watchdog pair: dead-guest detection with automatic
hypervisor snapshot rollback. Python 3.9+ stdlib only. Every node runs the
same scripts; every site-specific value comes from environment variables
(nothing hardcoded).

- **receiver** (management node): HTTP endpoint recording guest heartbeats.
  Guests hit `GET /beat/<name>` once a minute (systemd timer + curl);
  the last-beat time is the mtime of `<beat-dir>/<name>`.
  `GET /status` dumps the mtimes. Env: `CI_HB_PORT`, `CI_HB_BEAT_DIR`.
- **referee** (management node, 1-minute timer): a guest whose beat is older
  than `CI_HB_STALE_SEC` (default 300) for `CI_HB_STALE_ROUNDS` (default 2)
  consecutive rounds is force-rolled-back on the hypervisor:
  `power off → revert to the single baseline snapshot → power on`, then the
  beat unit is re-installed (baselines predate the beat unit). Env:
  `CI_HB_ESXI_HOST/_USER/_KEY_FILE`, `CI_HB_BEAT_DIR/_STATE_FILE/_LEDGER_FILE/
  _LOCK_FILE/_INVENTORY`, `CI_HB_NODE1_ADDR` (receiver URL used in the
  re-installed unit), `CI_HB_GUEST_USER/_PASSWORD_FILE`,
  `CI_HB_STALE_SEC/_ROUNDS`, `CI_HB_REVERT_BUDGET_24H` (default 3),
  `CI_HB_COOLDOWN_SEC` (default 900), `CI_HB_REARM_WAIT_SEC/_TRIES`,
  `CI_HB_CONNECT_TIMEOUT`.
- **beat** (guest side): keep it dead simple — a oneshot systemd service
  `curl -fsS -m 10 http://<receiver>/beat/<guest-name>` on a 60s timer
  (the referee's re-arm re-installs exactly this unit after a revert).

Safety rails: first-beat registration (a node without a beat file is never
touched), snapshot invariant (exactly one snapshot or refuse), 24h revert
budget per guest, post-action cooldown, hypervisor-reachability probe with
exponential backoff (probing with valid credentials DURING an account
lockout refreshes the lock window — back off silently instead), flock.
Hypervisor auth is SSH public key only: ESXi runs FIPS (ed25519 denied —
use RSA) and password auth turns account lockout into a self-extending
lock loop.

Example guest beat units and the systemd units for both roles are in the
receiver/referee module docstrings.

## e2e-sandbox（统一 e2e 临时目录设施，2026-09-21）

**背景**：全工作区验证作业的 Playwright/chromium profile 落在 `/tmp`，被 timeout/kill
时无人清理——2026-09-21 实测累计 **43GB 残骸**把 node-1 根盘吃到 97%（ENOSPC 实错）。

**用法**（agent 写 e2e 脚本时一行接线）：

```bash
# 包装任意命令（正常/失败/超时/SIGTERM 都清）
celestia-devtools e2e-sandbox run --label my-e2e -- node probe.cjs

# 调试时保留
celestia-devtools e2e-sandbox run --keep -- node probe.cjs

# 带超时
celestia-devtools e2e-sandbox run --timeout 120 -- pytest -q tests/e2e

# 兜底：清扫沙箱根下陈旧目录（默认 >6h）
celestia-devtools e2e-sandbox sweep

# 兜底：清扫 /tmp 里的孤儿 chromium profile（默认 >60min 未动、当前用户、名字形态匹配）
celestia-devtools e2e-sandbox sweep-tmp
```

**机制**：chromium 与 Playwright 都尊重 `TMPDIR`（实测验证：`os.tmpdir()` →
`pw-<hash>` 临时目录在 TMPDIR 下创建）。包装器把每个运行的 `TMPDIR`/`TMP`/`TEMP`
指到 `/mnt/work/e2e-sandbox/<时间戳-PID-标签>/tmp`，结束用 `finally` 语义无条件清除。
`sweep-tmp` 只删「≥20 位随机名 + 超过 max-age 未动 + 当前用户所有」的**目录**，
不碰文件、不碰 systemd/X11 套接字、权限不足自动跳过。

**建议**：e2e 作业的 cron / 农场看门狗定期跑 `sweep-tmp` 作为最后防线。

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

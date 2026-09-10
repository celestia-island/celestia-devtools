# ci-fleet — Win11 闲置机一键并网（WSL2 Linux self-hosted runner）

给实验室闲置的 Windows 11 电脑快速并入 celestia-island 自托管 CI 池：
在 Windows 上装 **WSL2 + Ubuntu-22.04**，在 WSL 里装 **GitHub Actions Linux
runner**（systemd 常驻 + 开机自启 + 10 分钟看门狗）。

## 快速开始（每台机器 ~15 分钟，其中下载 ~10 分钟）

1. 拷贝本目录到目标机器（U 盘/共享盘均可）。
2. 双击 `bootstrap-ci.cmd`（或右键"使用 Python 运行" `bootstrap-ci.py`）。会弹 UAC，请点"是"。
3. 按提示输入/回车确认四项：
   - **GitHub org**（默认 `celestia-island`）
   - **Runner 名字**（默认 `<主机名>-wsl`）
   - **Labels**（默认 `self-hosted,linux,x64,local,wsl`——直接吃现有队列）
   - **Registration token**（输入隐藏）：到
     `https://github.com/organizations/celestia-island/settings/actions/runners`
     → New runner → 复制 `--token` 后面那串（**1 小时内有效**，一次只配一台就现取现用）。
4. 结束后到 runner 池页面确认机器上线（Idle 状态）。

## 脚本结构

| 文件 | 作用 | 运行在 |
|---|---|---|
| `bootstrap-ci.py` | 编排器：UAC 提权、交互问询、串联两阶段（纯标准库，兼容 Win11 商店版 Python 3.11+） | Windows |
| `win-install-wsl.ps1` | 阶段 1：启用 WSL2/VirtualMachinePlatform、装 Ubuntu-22.04、开 systemd、写 `.wslconfig`（内存=物理一半、上限 8GB、CPU 一半） | Windows（管理员） |
| `win-register-runner.ps1` | 阶段 2：把 `wsl-runner-install.sh` 注入 distro、WSLENV 转发 token、装两个计划任务（开机自启 + 10 分钟看门狗） | Windows（管理员） |
| `wsl-runner-install.sh` | WSL 侧：apt 基础件、node22+corepack、just、celestia-devtools、runner 下载/注册/系统服务 | WSL Ubuntu-22.04 |
| `bootstrap-ci.cmd` | 没有 Python 时的兜底入口：直接按顺序跑两个 ps1（token 在 ps1 里交互输入） | Windows |

没有 Python 也没关系：双击 `bootstrap-ci.cmd` 即可，功能一致（token 由
`win-register-runner.ps1` 隐藏输入）。

## 前提与边界

- Windows 11 22H2+（build ≥ 22000）、管理员权限、BIOS 虚拟化已开（绝大多数办公机默认开启）。
- 磁盘预留 ≥ 20GB（WSL 镜像 + runner + 工作区）。
- 内存 ≤ 8GB 的机器请把 `.wslconfig` 里 memory 调低（脚本自动写为物理一半、4–8GB）。
- **WSL runner 的边界**：无 Docker-in-Docker（需要 docker 的 job 不要派给它）、不支持嵌套虚拟化；
  仓库现有 cargo/vue/jest/pnpm 类 job 全部可跑。
- Labels 默认带 `local`，因此会接 org 现有队列（`runs-on: [self-hosted, linux, x64, local]`）；
  加了 `wsl` 标签，将来想限制/排除这批机器时有抓手。
- runner 数据在 WSL 发行版里；整机重装 = 重跑本脚本即可（`--replace` 幂等重注册）。

## 事后运维

- 服务：`wsl -d Ubuntu-22.04 -u root -- systemctl status actions-runner.service`
- 计划任务：`CIFleet-RunnerWatchdog`（10 分钟）/ `CIFleet-RunnerBoot`（开机）
- 下线一台机器：`wsl -d Ubuntu-22.04 -u root -- /home/runner/actions-runner/config.sh remove --token <token>`，
  或直接在 GitHub runner 池页面 Remove。

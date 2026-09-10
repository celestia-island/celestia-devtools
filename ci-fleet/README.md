# ci-fleet — Win11 闲置机一键并网（WSL2 Linux self-hosted runner）

给实验室闲置的 Windows 11 电脑快速并入 celestia-island 自托管 CI 池：
一条命令完成 **WSL2 + Ubuntu-22.04 安装、Linux runner 注册、systemd 常驻、
开机自启 + 10 分钟看门狗计划任务**。

## 一键部署（管理员 PowerShell）

**网络直连可用时：**

```powershell
irm https://raw.githubusercontent.com/celestia-island/celestia-devtools/master/ci-fleet/bootstrap-ci.ps1 | iex
```

**被墙环境：用镜像取脚本 + 镜像加速下载 + 代理管运行时：**

```powershell
& ([scriptblock]::Create((irm https://ghfast.top/https://raw.githubusercontent.com/celestia-island/celestia-devtools/master/ci-fleet/bootstrap-ci.ps1))) -GHProxy https://ghfast.top -Proxy http://<代理主机>:<端口>
```

执行后弹 UAC（点"是"），随后在**弹出的管理员窗口**里按提示输入：

| 项 | 默认 | 说明 |
|---|---|---|
| Runner 名字 | `<主机名>-wsl` | 回车即可 |
| Labels | `self-hosted,linux,x64,local,wsl` | 带 `local` 直接吃现有队列，`wsl` 留作定向抓手 |
| **Registration token** | （必填，隐藏输入） | `https://github.com/organizations/celestia-island/settings/actions/runners` → New runner → 复制 `--token` 后那串，**1 小时内有效**，现取现用 |

全程约 15 分钟（其中下载 ~10 分钟）。结束看到 `actions-runner.service: running`
即上线，到 runner 池页面确认 Idle。

## 参数（免交互 / 自动化）

| 参数 | 说明 |
|---|---|
| `-Org` | GitHub org，默认 `celestia-island` |
| `-RunnerName` | runner 名，默认 `<主机名>-wsl` |
| `-Labels` | 默认 `self-hosted,linux,x64,local,wsl` |
| `-Token` | 注册 token（不给则隐藏交互输入） |
| `-Proxy` | HTTP(S) 代理：**下载与 runner 运行时轮询都走它**，派生的 job 也继承 |
| `-GHProxy` | GitHub 下载镜像前缀（拼 `<mirror>/https://github.com/...`），只加速下载 |
| `-NoProxy` | 默认 `localhost,127.0.0.1` |
| `-SkipWslStage` | 跳过阶段 1（WSL 已备好时） |

## 网络（防火墙环境）怎么选

| 场景 | 建议 |
|---|---|
| 直连 GitHub 可用 | 什么都不填 |
| github.com 被墙、代理可用 | `-Proxy http://<代理>:<端口>`（**必选**，runner 运行时轮询必须走它） |
| 想加速大文件下载 | 追加 `-GHProxy <镜像前缀>`（镜像只管下载，救不了运行时连接） |

实验室内网已有统一代理出口（daemon 节点 sing-box），具体地址找值班 agent /
看 PLAN 网络章节，**不要写进任何仓库文件**。

## 脚本行为（单文件 `bootstrap-ci.ps1`）

1. 非管理员运行 → 自动 UAC 提权重启自己（iex 场景会重新取回脚本）；
2. 阶段 1：启用 WSL2/VirtualMachinePlatform → 装 Ubuntu-22.04（免交互）→
   `/etc/wsl.conf` 开 systemd + 默认用户 runner → `.wslconfig` 自动配额
   （内存=物理一半 4–8GB、CPU 一半）；
3. 阶段 2：内嵌 bash 安装器注入 distro 执行——基础件、node22+corepack、just、
   celestia-devtools 预装、runner 下载（`-GHProxy` 可加速）/ 注册（`--replace` 幂等）/
   systemd 常驻；
4. 计划任务：`CIFleet-RunnerWatchdog`（10 分钟）/ `CIFleet-RunnerBoot`（开机）；
5. token 仅经进程环境传递（WSLENV），不落盘、不进命令行历史。

## 前提与边界

- Windows 11 22H2+（build ≥ 22000）、管理员、BIOS 虚拟化开启（办公机默认开）；
- 磁盘预留 ≥ 20GB；内存 ≤ 8GB 的机器 `.wslconfig` 会自动压到物理一半；
- **无 Docker-in-Docker、无嵌套虚拟化**：docker 类 job 不要派；现有
  cargo/vue/jest/pnpm 类 job 全部可跑；
- 整机重装 = 重跑同一条命令（`--replace` 幂等重注册）。

## 事后运维

- 服务状态：`wsl -d Ubuntu-22.04 -u root -- systemctl status actions-runner.service`
- 计划任务：`CIFleet-RunnerWatchdog` / `CIFleet-RunnerBoot`
- 下线机器：`wsl -d Ubuntu-22.04 -u root -- /home/runner/actions-runner/config.sh remove --token <token>`，
  或在 GitHub runner 池页面 Remove。

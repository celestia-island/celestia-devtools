# bootstrap-ci.ps1 — one-click onboarding for a Windows 11 host joining the
# celestia-island self-hosted CI fleet as a WSL2-hosted Linux runner.
#
# Quick start (elevates itself via UAC; prompts for the registration token):
#
#   irm https://raw.githubusercontent.com/celestia-island/celestia-devtools/master/ci-fleet/bootstrap-ci.ps1 | iex
#
# Firewalled hosts — fetch through a mirror and/or proxy, and pass them on:
#
#   & ([scriptblock]::Create((irm https://ghfast.top/https://raw.githubusercontent.com/celestia-island/celestia-devtools/master/ci-fleet/bootstrap-ci.ps1))) -GHProxy https://ghfast.top -Proxy http://<proxy-host>:<port>
#
# Parameters:
#   -Org         GitHub org                      (default celestia-island)
#   -RunnerName  runner display name             (default <hostname>-wsl)
#   -Labels      runs-on labels                  (default self-hosted,linux,x64,local,wsl)
#   -Token       registration token              (default: hidden prompt)
#   -Proxy       HTTP(S) proxy for downloads AND the runner service
#   -GHProxy     mirror prefix for github downloads (download speed-up only)
#   -NoProxy     NO_PROXY value                 (default localhost,127.0.0.1)
#   -SkipWslStage
#                skip stage 1 (when WSL2/Ubuntu is already prepared)

param(
  [string]$Org = "celestia-island",
  [string]$RunnerName = "",
  [string]$Labels = "self-hosted,linux,x64,local,wsl",
  [string]$Token,
  [string]$Proxy,
  [string]$GHProxy,
  [string]$NoProxy = "localhost,127.0.0.1",
  [switch]$SkipWslStage,
  [string]$SelfUrl = "https://raw.githubusercontent.com/celestia-island/celestia-devtools/master/ci-fleet/bootstrap-ci.ps1"
)

$distro = "Ubuntu-22.04"
$runnerUser = "runner"
$installDir = "/home/$runnerUser/actions-runner"
$runnerVersion = "2.337.0"

# ---------------------------------------------------------------------------
# elevation: the whole setup needs Administrator. When launched unelevated
# (the usual irm|iex case), re-launch the same script elevated and exit.
# ---------------------------------------------------------------------------
$identity = [Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()
if (-not $identity.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
  Write-Host "Administrator rights required - relaunching elevated (accept the UAC prompt)..." -ForegroundColor Yellow
  $fetch = "irm '$SelfUrl'"
  if ($Proxy) { $fetch = "irm '$SelfUrl' -Proxy '$Proxy'" }
  $argPass = @()
  if ($GHProxy) { $argPass += "-GHProxy '$GHProxy'" }
  if ($Proxy) { $argPass += "-Proxy '$Proxy'" }
  $cmd = "& ([scriptblock]::Create(($fetch))) $(($argPass -join ' '))".Trim()
  Start-Process powershell.exe -Verb RunAs -ArgumentList "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", $cmd
  exit
}

$ErrorActionPreference = "Stop"

# ---------------------------------------------------------------------------
# interactive defaults (run in the ELEVATED window)
# ---------------------------------------------------------------------------
if (-not $RunnerName) { $RunnerName = "$env:COMPUTERNAME-wsl".ToLower() }

Write-Host "== celestia-island CI fleet bootstrap (WSL2 runner) ==" -ForegroundColor Cyan
Write-Host "org:     $Org"
Write-Host "name:    $RunnerName"
Write-Host "labels:  $Labels"
if (-not $Token) {
  Write-Host "Get the registration token at:"
  Write-Host "  https://github.com/organizations/$Org/settings/actions/runners"
  Write-Host "  -> New runner -> copy the token from the --token value (expires ~1h)"
  $sec = Read-Host -AsSecureString "Registration token (input hidden)"
  $b = [System.Runtime.InteropServices.Marshal]::SecureStringToBSTR($sec)
  $Token = [System.Runtime.InteropServices.Marshal]::PtrToStringBSTR($b)
}
if (-not $Token) { throw "A registration token is required." }
if ($Proxy) { Write-Host "proxy:   $Proxy" }
if ($GHProxy) { Write-Host "mirror:  $GHProxy" }

# ---------------------------------------------------------------------------
# embedded linux installer (single source of truth; piped into the distro)
# ---------------------------------------------------------------------------
$wslInstallerScript = @'
#!/usr/bin/env bash
# wsl-runner-install.sh — Linux-side installer, runs INSIDE WSL2 as root.
# Inputs come in as environment variables forwarded by the Windows stage.
set -euo pipefail

RUNNER_TOKEN="${RUNNER_TOKEN:?RUNNER_TOKEN is required}"
RUNNER_ORG="${RUNNER_ORG:-celestia-island}"
RUNNER_NAME="${RUNNER_NAME:-$(hostname)-wsl}"
RUNNER_LABELS="${RUNNER_LABELS:-self-hosted,linux,x64,local,wsl}"
RUNNER_VERSION="${RUNNER_VERSION:-2.337.0}"
RUNNER_USER="runner"
INSTALL_DIR="/home/${RUNNER_USER}/actions-runner"
GH_PROXY="${GH_PROXY:-}"

gh_url() {
  if [ -n "$GH_PROXY" ]; then
    echo "${GH_PROXY%/}/$1"
  else
    echo "$1"
  fi
}

if [ -n "${HTTPS_PROXY:-}" ]; then
  export https_proxy="$HTTPS_PROXY" http_proxy="${HTTP_PROXY:-$HTTPS_PROXY}"
fi

echo "[1/7] base packages"
export DEBIAN_FRONTEND=noninteractive
apt-get update -qq
apt-get install -y -qq curl git jq tar ca-certificates build-essential \
  libicu-dev liblttng-ust0 libssl-dev rsync unzip >/dev/null

echo "[2/7] node 22 + corepack pnpm (house toolchain)"
if ! command -v node >/dev/null 2>&1 || [ "$(node -v | cut -c2- | cut -d. -f1)" -lt 22 ]; then
  curl -fsSL https://deb.nodesource.com/setup_22.x | bash - >/dev/null
  apt-get install -y -qq nodejs >/dev/null
fi
corepack enable >/dev/null 2>&1 || npm install -g corepack >/dev/null

echo "[3/7] just (recipe runner used by repo justfiles)"
if ! command -v just >/dev/null 2>&1; then
  curl -fsSL -o /usr/local/bin/just \
    "$(gh_url "https://github.com/casey/just/releases/download/1.25.2/just-1.25.2-x86_64-unknown-linux-musl")" \
    && chmod +x /usr/local/bin/just || echo "WARN: just install failed; just-recipes jobs will fail"
fi

echo "[3b/7] celestia-devtools (org workflows assume it is preinstalled)"
command -v celestia-devtools >/dev/null 2>&1 || python3 -m pip install --break-system-packages celestia-devtools 2>/dev/null \
  || python3 -m pip install celestia-devtools

echo "[4/7] runner user + passwordless sudo (workflow apt/sudo steps)"
id -u "${RUNNER_USER}" >/dev/null 2>&1 || useradd -m -s /bin/bash "${RUNNER_USER}"
echo "${RUNNER_USER} ALL=(ALL) NOPASSWD:ALL" > /etc/sudoers.d/${RUNNER_USER}
chmod 440 /etc/sudoers.d/${RUNNER_USER}

echo "[5/7] download actions-runner ${RUNNER_VERSION}"
if [ ! -x "${INSTALL_DIR}/config.sh" ]; then
  mkdir -p "${INSTALL_DIR}"
  curl -fsSL -o /tmp/runner.tgz \
    "$(gh_url "https://github.com/actions/runner/releases/download/v${RUNNER_VERSION}/actions-runner-linux-x64-${RUNNER_VERSION}.tar.gz")"
  tar -xzf /tmp/runner.tgz -C "${INSTALL_DIR}"
  rm -f /tmp/runner.tgz
fi
chown -R "${RUNNER_USER}:${RUNNER_USER}" "${INSTALL_DIR}"

echo "[6/7] register runner '${RUNNER_NAME}' (labels: ${RUNNER_LABELS})"
cd "${INSTALL_DIR}"
sudo -u "${RUNNER_USER}" env RUNNER_ALLOW_RUNASROOT=1 ./config.sh \
  --url "https://github.com/${RUNNER_ORG}" \
  --token "${RUNNER_TOKEN}" \
  --name "${RUNNER_NAME}" \
  --labels "${RUNNER_LABELS}" \
  --work "_work" \
  --unattended \
  --replace

echo "[7/7] systemd service + start"
proxy_env=""
if [ -n "${HTTPS_PROXY:-}" ]; then
  proxy_env="Environment=HTTPS_PROXY=${HTTPS_PROXY}${HTTP_PROXY:+
Environment=HTTP_PROXY=${HTTP_PROXY}}${NO_PROXY:+
Environment=NO_PROXY=${NO_PROXY}}"
fi
cat > /etc/systemd/system/actions-runner.service <<UNIT
[Unit]
Description=GitHub Actions Runner (celestia-island, wsl fleet)
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=${RUNNER_USER}
WorkingDirectory=${INSTALL_DIR}
ExecStart=${INSTALL_DIR}/runsvc.sh
Restart=always
RestartSec=30s
${proxy_env}

[Install]
WantedBy=multi-user.target
UNIT
systemctl daemon-reload
systemctl enable --now actions-runner.service

sleep 3
systemctl --no-pager status actions-runner.service | head -5 || true
echo "DONE: runner '${RUNNER_NAME}' is registered and running."
'@

# ---------------------------------------------------------------------------
# stage 1: WSL2 + Ubuntu-22.04 + systemd + .wslconfig
# ---------------------------------------------------------------------------
function Invoke-WslStage {
  Write-Host "== stage 1: WSL2 + $distro ==" -ForegroundColor Cyan

  $wslStatus = ((wsl --status) -join " ") -replace "\0", ""
  if ($wslStatus -notmatch "Ubuntu|Default Version|WSL") {
    Write-Host "WSL core missing - installing..."
    dism.exe /online /enable-feature /featurename:Microsoft-Windows-Subsystem-Linux /all /norestart | Out-Null
    dism.exe /online /enable-feature /featurename:VirtualMachinePlatform /all /norestart | Out-Null
    wsl --install --no-distribution | Out-Null
    Write-Host "If Windows asked for a reboot: reboot, then re-run this script."
  }

  wsl --set-default-version 2
  if ($LASTEXITCODE -ne 0) { throw "wsl --set-default-version 2 failed ($LASTEXITCODE)" }
  Write-Host "WSL default version = 2"

  wsl --update | Select-Object -First 3 | Out-Host

  $distros = (wsl --list --quiet) | ForEach-Object { $_ -replace "\0", "" } | Where-Object { $_ }
  if ($distros -notcontains $distro) {
    Write-Host "Installing $distro (no-launch)..."
    wsl --install -d $distro --no-launch | Out-Null
    $launcher = Get-Command "ubuntu2204.exe" -ErrorAction SilentlyContinue
    if ($launcher) {
      & ubuntu2204.exe install --root | Out-Null
    } else {
      wsl -d $distro -u root -- true | Out-Null
    }
  }
  wsl -d $distro -u root -- true | Out-Null
  if ($LASTEXITCODE -ne 0) { throw "$distro did not register properly" }
  Write-Host "$distro registered"

  wsl -d $distro -u root -- bash -c "printf '[boot]\nsystemd=true\n\n[user]\ndefault=runner\n' > /etc/wsl.conf"
  Write-Host "/etc/wsl.conf written (systemd=true, default user=$runnerUser)"

  $totalGB = [math]::Round((Get-CimInstance Win32_ComputerSystem).TotalPhysicalMemory / 1GB)
  $memGB = [math]::Max(4, [math]::Min(8, [math]::Floor($totalGB / 2)))
  $cpu = [Environment]::ProcessorCount
  $procN = [math]::Max(2, [math]::Floor($cpu / 2))
  $wslconfig = @"
[wsl2]
memory=${memGB}GB
processors=$procN
swap=2GB
"@
  Set-Content -Path "$env:USERPROFILE\.wslconfig" -Value $wslconfig -Encoding ASCII
  Write-Host ".wslconfig written (memory=${memGB}GB, processors=$procN of $cpu)"

  wsl --terminate $distro | Out-Null
  wsl -d $distro -u root -- echo "WSL round-trip OK" | Out-Host
  Write-Host "== stage 1 complete ==" -ForegroundColor Green
}

# ---------------------------------------------------------------------------
# stage 2: install + register the runner inside the distro
# ---------------------------------------------------------------------------
function Register-RunnerStage {
  Write-Host "== stage 2: register runner '$RunnerName' ==" -ForegroundColor Cyan

  $env:RUNNER_TOKEN = $Token
  $env:RUNNER_ORG = $Org
  $env:RUNNER_NAME = $RunnerName
  $env:RUNNER_LABELS = $Labels
  $env:WSLENV = "RUNNER_TOKEN/u:RUNNER_ORG/u:RUNNER_NAME/u:RUNNER_LABELS/u"
  if ($Proxy) {
    $env:HTTPS_PROXY = $Proxy
    $env:HTTP_PROXY = $Proxy
    if (-not $NoProxy) { $NoProxy = "localhost,127.0.0.1" }
    $env:NO_PROXY = $NoProxy
    $env:WSLENV = "$env:WSLENV:HTTP_PROXY/u:HTTPS_PROXY/u:NO_PROXY/u"
    Write-Host "Proxy enabled: $Proxy"
  }
  if ($GHProxy) {
    $env:GH_PROXY = $GHProxy
    $env:WSLENV = "$env:WSLENV:GH_PROXY/u"
    Write-Host "GitHub mirror enabled: $GHProxy (downloads only)"
  }

  Write-Host "Installing (apt + node22 + runner download; 5-15 minutes on first run)..."
  $wslInstallerScript | wsl -d $distro -u root -- bash
  if ($LASTEXITCODE -ne 0) { throw "Linux-side installer failed with exit code $LASTEXITCODE" }

  $bootCmd = "wsl -d $distro -u root -- systemctl start actions-runner.service"
  schtasks /Create /F /TN "CIFleet-RunnerWatchdog" /RU SYSTEM /SC MINUTE /MO 10 /TR $bootCmd | Out-Null
  schtasks /Create /F /TN "CIFleet-RunnerBoot" /RU SYSTEM /SC ONSTART /DELAY 0001:00 /TR $bootCmd | Out-Null
  Write-Host "Scheduled tasks CIFleet-RunnerWatchdog / CIFleet-RunnerBoot installed"

  $state = "unknown"
  try {
    $state = ((wsl -d $distro -u root -- systemctl is-active actions-runner.service) 2>&1 | Out-String).Trim()
  } catch {
    $state = "query failed: $_"
  }
  Write-Host "actions-runner.service: $state" -ForegroundColor $(if ($state -match "active") { "Green" } else { "Red" })
  Write-Host "Runner pool: https://github.com/organizations/$Org/settings/actions/runners"
}

# ---------------------------------------------------------------------------
# run
# ---------------------------------------------------------------------------
if (-not $SkipWslStage) { Invoke-WslStage }
Register-RunnerStage

Write-Host ""
Write-Host "== this host has joined the CI fleet ==" -ForegroundColor Green
if ($Host.Name -eq "ConsoleHost") {
  Read-Host "Done. Press Enter to close"
}

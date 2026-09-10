# win-register-runner.ps1 — stage 2: register + start the GitHub Actions runner
# inside the WSL2 Ubuntu-22.04 distro prepared by win-install-wsl.ps1.
#
# Parameters (all optional — interactive prompts fill the gaps):
#   -Org         GitHub org                       (default celestia-island)
#   -RunnerName  runner display name              (default <hostname>-wsl)
#   -Labels      comma-separated runs-on labels   (default self-hosted,linux,x64,local,wsl)
#   -Token       registration token; when omitted a hidden prompt asks for it.
#                Get one at: https://github.com/<org>/settings/actions/runners
#                ("New runner" -> copy the --token value). Tokens expire in ~1h.
#
# Also installs two SYSTEM scheduled tasks so the runner survives reboots:
#   CIFleet-RunnerWatchdog : every 10 minutes, start the runner service if down
#   CIFleet-RunnerBoot     : at system startup, same

param(
  # Defaults read CI_* environment variables when the python orchestrator
  # drives this stage; fall back to the interactive prompts otherwise.
  [string]$Org = $(if ($env:CI_ORG) { $env:CI_ORG } else { "celestia-island" }),
  [string]$RunnerName = $(if ($env:CI_RUNNER_NAME) { $env:CI_RUNNER_NAME } else { "$env:COMPUTERNAME-wsl".ToLower() }),
  [string]$Labels = $(if ($env:CI_LABELS) { $env:CI_LABELS } else { "self-hosted,linux,x64,local,wsl" }),
  [string]$Token = $env:CI_TOKEN,
  # Optional network knobs for firewalled hosts:
  #   -Proxy   URL of an HTTP(S) proxy (e.g. http://<proxy-host>:<port>) —
  #            used for downloads AND baked into the runner service so its
  #            GitHub polling and spawned jobs all go through it.
  #   -GHProxy mirror prefix for github release downloads (download speed-up
  #            only; the runner runtime does not use mirrors).
  [string]$Proxy = $env:CI_PROXY,
  [string]$GHProxy = $env:CI_GH_PROXY,
  [string]$NoProxy = $(if ($env:CI_NO_PROXY) { $env:CI_NO_PROXY } else { "localhost,127.0.0.1" })
)
$ErrorActionPreference = "Stop"
$distro = "Ubuntu-22.04"
$scriptDir = $PSScriptRoot

if (-not $Token) {
  $sec = Read-Host -AsSecureString "Registration token (input hidden)"
  $b = [System.Runtime.InteropServices.Marshal]::SecureStringToBSTR($sec)
  $Token = [System.Runtime.InteropServices.Marshal]::PtrToStringBSTR($b)
}
if (-not $Token) { throw "A registration token is required." }
if (-not (Get-Command wsl -ErrorAction SilentlyContinue)) { throw "WSL is not installed; run win-install-wsl.ps1 first." }

Write-Host "== stage 2: register runner '$RunnerName' ==" -ForegroundColor Cyan

# --- copy the linux installer into the distro --------------------------------
$installer = Join-Path $scriptDir "wsl-runner-install.sh"
Get-Content $installer -Raw | wsl -d $distro -u root -- bash -c "mkdir -p /opt/ci-fleet && cat > /opt/ci-fleet-install.sh && chmod +x /opt/ci-fleet-install.sh"

# --- optional network knobs (proxy / mirror) ---------------------------------
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

# --- forward configuration into WSL (WSLENV /u = one-way into the distro) ----
$env:RUNNER_TOKEN = $Token
$env:RUNNER_ORG = $Org
$env:RUNNER_NAME = $RunnerName
$env:RUNNER_LABELS = $Labels
$env:WSLENV = "RUNNER_TOKEN/u:RUNNER_ORG/u:RUNNER_NAME/u:RUNNER_LABELS/u"

Write-Host "Installing (apt + node22 + runner download; 5-15 minutes on first run)..."
wsl -d $distro -u root -- bash /opt/ci-fleet-install.sh
if ($LASTEXITCODE -ne 0) { throw "Linux-side installer failed with exit code $LASTEXITCODE" }

# --- keep it alive across reboots --------------------------------------------
# systemd starts actions-runner.service on distro boot; these tasks make sure
# the distro itself is woken up after a host reboot and the service is
# re-started every 10 minutes if it ever goes down.
$bootCmd = "wsl -d $distro -u root -- systemctl start actions-runner.service"
schtasks /Create /F /TN "CIFleet-RunnerWatchdog" /RU SYSTEM /SC MINUTE /MO 10 /TR $bootCmd | Out-Null
schtasks /Create /F /TN "CIFleet-RunnerBoot" /RU SYSTEM /SC ONSTART /DELAY 0001:00 /TR $bootCmd | Out-Null
Write-Host "Scheduled tasks CIFleet-RunnerWatchdog / CIFleet-RunnerBoot installed"

# --- report -------------------------------------------------------------------
$state = "unknown"
try {
  $state = ((wsl -d $distro -u root -- systemctl is-active actions-runner.service) 2>&1 | Out-String).Trim()
} catch {
  $state = "query failed: $_"
}
Write-Host "actions-runner.service: $state" -ForegroundColor $(if ($state -match "active") { "Green" } else { "Red" })
Write-Host "Runner pool: https://github.com/organizations/$Org/settings/actions/runners"
Write-Host "== stage 2 complete ==" -ForegroundColor Green

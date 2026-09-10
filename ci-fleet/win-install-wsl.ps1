# win-install-wsl.ps1 — stage 1 (run as Administrator): prepare Windows 11 for
# a WSL2-hosted Linux CI runner. Idempotent; safe to re-run.
#
# - enables VirtualMachinePlatform + WSL
# - installs Ubuntu-22.04, registers it non-interactively (root)
# - enables systemd inside the distro, default user = runner
# - writes %USERPROFILE%\.wslconfig with sane CPU/memory caps
#
# No arguments. Reboot is NOT normally required on Win11 22H2+; if Windows asks
# for one, re-run this script after the reboot.
#
# PS 5.1 note: with $ErrorActionPreference="Stop", a REDIRECTED native stderr
# (2>&1) escalates into a terminating error. This script therefore avoids
# 2>&1 on native calls and checks $LASTEXITCODE explicitly instead.

#Requires -RunAsAdministrator
$ErrorActionPreference = "Stop"
$distro = "Ubuntu-22.04"

Write-Host "== stage 1: WSL2 + Ubuntu-22.04 base ==" -ForegroundColor Cyan

# --- Windows 11 sanity -------------------------------------------------------
$build = [System.Environment]::OSVersion.Version.Build
if ($build -lt 22000) { throw "Windows 11 (build >= 22000) required; this is $build" }
Write-Host "Windows build $build OK"

# --- enable WSL machinery ----------------------------------------------------
# wsl --status output is UTF-16; strip null padding before matching.
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

# --- install Ubuntu-22.04 if absent ------------------------------------------
$distros = (wsl --list --quiet) | ForEach-Object { $_ -replace "\0", "" } | Where-Object { $_ }
$haveDistro = $distros -contains $distro
if (-not $haveDistro) {
  Write-Host "Installing $distro (no-launch)..."
  wsl --install -d $distro --no-launch | Out-Null
  # Non-interactive registration: the appx launcher supports `install --root`,
  # which skips the OOBE user-creation prompt and defaults to root.
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

# --- systemd on, default user = runner ---------------------------------------
wsl -d $distro -u root -- bash -c "printf '[boot]\nsystemd=true\n\n[user]\ndefault=runner\n' > /etc/wsl.conf"
Write-Host "/etc/wsl.conf written (systemd=true, default user=runner)"

# --- .wslconfig: cap WSL so it never fights Windows --------------------------
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

# --- apply configuration ------------------------------------------------------
wsl --terminate $distro | Out-Null
wsl -d $distro -u root -- echo "WSL round-trip OK" | Out-Host

Write-Host "== stage 1 complete ==" -ForegroundColor Green

#!/usr/bin/env python3
"""bootstrap-ci.py — one-click onboarding for a Windows 11 host joining the
celestia-island self-hosted CI fleet as a WSL2-hosted Linux runner.

Uses only the Python standard library (works with the Microsoft Store Python
3.11+ that ships with Windows 11). If Python is unavailable, run the two
PowerShell stages directly:

    powershell -ExecutionPolicy Bypass -File win-install-wsl.ps1
    powershell -ExecutionPolicy Bypass -File win-register-runner.ps1

Flow:
    1. relaunch itself elevated (UAC) when not run as Administrator
    2. stage 1: win-install-wsl.ps1      (WSL2 + Ubuntu-22.04 + systemd)
    3. prompts: org / runner name / labels / registration token
    4. stage 2: win-register-runner.ps1  (runner install + systemd + watchdog)
"""

from __future__ import annotations

import ctypes
import os
import socket
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))

DEFAULT_ORG = "celestia-island"
DEFAULT_LABELS = "self-hosted,linux,x64,local,wsl"
TOKEN_HELP = (
    "Get the registration token at:\n"
    "  https://github.com/organizations/{org}/settings/actions/runners\n"
    "  -> New runner -> copy the token from the `--token` value (expires ~1h)."
)


def is_admin() -> bool:
    try:
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:
        return False


def relaunch_elevated() -> None:
    params = " ".join(f'"{a}"' for a in sys.argv)
    ret = ctypes.windll.shell32.ShellExecuteW(
        None, "runas", sys.executable, params, None, 1  # SW_SHOWNORMAL
    )
    if ret <= 32:
        sys.exit("Elevation was cancelled; the bootstrap needs Administrator rights.")
    sys.exit(0)


def ask(prompt: str, default: str) -> str:
    raw = input(f"{prompt} [{default}]: ").strip()
    return raw or default


def run_ps(stage: str, env: dict[str, str] | None = None) -> None:
    script = os.path.join(HERE, stage)
    if not os.path.isfile(script):
        sys.exit(f"missing stage script: {script}")
    merged = os.environ | (env or {})
    print(f"\n--- running {stage} ---")
    proc = subprocess.run(
        ["powershell.exe", "-ExecutionPolicy", "Bypass", "-File", script],
        env=merged,
    )
    if proc.returncode != 0:
        sys.exit(f"{stage} failed with exit code {proc.returncode}")


def main() -> None:
    if os.name != "nt":
        sys.exit("Run this on the Windows 11 host (the Linux side is wsl-runner-install.sh).")
    if not is_admin():
        print("Administrator rights are required - relaunching elevated (accept the UAC prompt).")
        relaunch_elevated()

    print("== celestia-island CI fleet bootstrap (WSL2 runner) ==")

    org = ask("GitHub org", DEFAULT_ORG)
    name = ask("Runner name", f"{socket.gethostname().lower()}-wsl")
    labels = ask("Labels", DEFAULT_LABELS)
    print("\nNetwork (both optional, Enter = skip; needed on firewalled hosts):")
    print("  proxy accepts an HTTP(S) proxy URL, e.g. http://<proxy-host>:<port>")
    print("  mirror accepts a GitHub download prefix, e.g. https://ghfast.top")
    proxy = ask("Proxy URL", "")
    gh_mirror = ask("GitHub mirror prefix", "")
    print("\n" + TOKEN_HELP.format(org=org) + "\n")
    token = ""
    while not token:
        import getpass

        token = getpass.getpass("Registration token (input hidden): ").strip()

    # Stage 1 needs no secrets.
    run_ps("win-install-wsl.ps1")

    # Stage 2 receives the token and network knobs through the environment
    # (the ps1 params read CI_* first; the ps1 itself forwards RUNNER_* and
    # the proxy/mirror knobs into WSL via WSLENV).
    env = {
        "CI_ORG": org,
        "CI_RUNNER_NAME": name,
        "CI_LABELS": labels,
        "CI_TOKEN": token,
    }
    if proxy:
        env["CI_PROXY"] = proxy
    if gh_mirror:
        env["CI_GH_PROXY"] = gh_mirror
    run_ps("win-register-runner.ps1", env)

    print("\n== this host has joined the CI fleet ==")
    print("Runner pool: https://github.com/organizations/{}/settings/actions/runners".format(org))


if __name__ == "__main__":
    main()

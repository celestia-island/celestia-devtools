#!/usr/bin/env python3
"""Port of ``_tools/ci-runner-credentials.sh`` (Python 3 stdlib only).

Idempotently installs the read-only deploy-key SSH credentials for the org's
PRIVATE git dependencies onto a celestia-island self-hosted CI runner
(node-ci-1..4, AGENTS §8.4), so ``cargo`` can fetch private git deps:

  1. ``~/.ssh/id_ed25519_ci_<repo>``      the five private keys, mode 600
  2. ``~/.ssh/config``                    five managed ``Host gh-ci-<repo>`` blocks
  3. ``~/.gitconfig``                     exactly five ``url.<alias>.insteadOf`` rules
  4. ``~/.cargo/config.toml``             ``[net] git-fetch-with-cli = true``
                                          (existing content preserved, backup kept)
  5. runner-unit env drop-in              ``CARGO_NET_GIT_FETCH_WITH_CLI=true``
                                          (+ conditional restart policy)
  6. non-login PATH symlinks              nvm node/npm/npx/corepack/pnpm -> /usr/local/bin
  7. python3-venv + python3-pip           LAST, deliberately (see the bash original)

Design notes carried over verbatim from the bash original (do not "improve" away):

* ONE KEY PER REPO (a GitHub deploy key cannot be reused across repos).
* NO BLANKET ``https://github.com/`` rewrite -- only the five exact private repo
  URLs are rewritten; public repos keep anonymous HTTPS.
* ADDITIVE ONLY: existing ssh config / gitconfig / cargo config content is
  preserved; only the marked managed block is replaced.
* NO key material and NO password in this file: keys are copied from
  ``CI_KEYS_DIR`` at run time, the SSH password comes from the environment.

Usage::

    CI_RUNNER_PASSWORD='<password>' tools/ci_runner_credentials.py <host>

``<host>`` is a runner hostname/IP; ``all`` applies to the known runners listed
in ``CI_RUNNER_ALL_HOSTS`` (space-separated env var; unreachable ones are
skipped with a non-zero exit).  Internal IPs are runtime values, never
hardcoded here (AGENTS §10.1).

Exit codes: 0 all requested hosts configured; 1 at least one host failed;
2 usage error (argparse / preflight).
"""

from __future__ import annotations

import argparse
import os
import shlex
import subprocess
import sys
from pathlib import Path
from typing import Sequence

DEFAULT_RUNNER_USER = "lab"
DEFAULT_KEYS_DIR = Path("/mnt/work/ci-keys")
DEFAULT_RUNNER_PASSWORD_FILE = Path("/mnt/work/ci-keys/runner-password.txt")
DEFAULT_CONNECT_TIMEOUT = 20
DEFAULT_NO_RESTART = "0"
DEFAULT_FORCE_RESTART = "0"
DEFAULT_PNPM_VERSION = "11.18.0"  # /usr/local/bin/pnpm wrapper pin (§7.4)
EXIT_OK = 0
EXIT_FAILURE = 1
EXIT_USAGE = 2

# The five private repos Rust CI actually needs, and the local key basename for
# each.  akivili's key is the pre-existing gh-ci-readonly; the other four are
# gh-ci-readonly-<repo>.
REPOS = ("akivili", "arona", "entelecheia", "evernight", "shittim-chest")

MANAGED_BEGIN = "# >>> celestia-ci-readonly (managed by _tools/ci-runner-credentials.sh)"
MANAGED_END = "# <<< celestia-ci-readonly (managed by _tools/ci-runner-credentials.sh)"


class CredentialsError(Exception):
    """Actionable ci-runner-credentials error (exit 1)."""


def key_basename(repo: str) -> str:
    """Local key filename (without extension) for one private repo."""
    if repo == "akivili":
        return "gh-ci-readonly"
    return f"gh-ci-readonly-{repo}"


# ── Pure rendering helpers (no I/O; unit-tested) ─────────────────────────────


def render_managed_ssh_block() -> str:
    """Render the managed ``~/.ssh/config`` block (between the markers).

    Deterministic: rendering twice yields byte-identical text, which is what
    makes re-running the tool idempotent.
    """
    lines = [MANAGED_BEGIN]
    for repo in REPOS:
        lines += [
            f"Host gh-ci-{repo}",
            "  HostName github.com",
            "  User git",
            f"  IdentityFile ~/.ssh/id_ed25519_ci_{repo}",
            "  IdentitiesOnly yes",
            "  StrictHostKeyChecking accept-new",
        ]
    lines.append(MANAGED_END)
    return "\n".join(lines) + "\n"


def strip_managed_ssh_block(text: str) -> str:
    """Remove the previously managed block from ``~/.ssh/config`` content."""
    out: list[str] = []
    skipping = False
    for line in text.splitlines():
        if line.startswith(MANAGED_BEGIN):
            skipping = True
            continue
        if line.startswith(MANAGED_END):
            skipping = False
            continue
        if not skipping:
            out.append(line)
    return "\n".join(out) + ("\n" if out else "")


def apply_managed_ssh_block(existing: str | None) -> str:
    """Return new ``~/.ssh/config`` content: old managed block replaced.

    Idempotent: applying the block to already-applied content is a no-op.
    """
    if existing is None:
        existing = ""
    return strip_managed_ssh_block(existing) + render_managed_ssh_block()


def insteadof_rules() -> list[tuple[str, str]]:
    """The exactly-five gitconfig rewrite rules (key, value) pairs."""
    return [
        (
            f"url.gh-ci-{repo}:celestia-island/{repo}.git.insteadOf",
            f"https://github.com/celestia-island/{repo}.git",
        )
        for repo in REPOS
    ]


def patch_cargo_net_config(text: str) -> str:
    """Ensure ``[net] git-fetch-with-cli = true`` in ~/.cargo/config.toml.

    Ports the embedded python3 heredoc of the bash script: an existing
    ``[net]`` section keeps its other keys, an existing
    ``git-fetch-with-cli`` line is normalized, and everything else in the
    file (e.g. the tuna crates mirror) is preserved byte-for-byte.
    Idempotent: patching patched text returns it unchanged.
    """
    lines = text.split("\n")
    net_start = None
    for i, line in enumerate(lines):
        if line.startswith("[net]"):
            net_start = i
            break
    if net_start is None:
        out = text
        if out and not out.endswith("\n"):
            out += "\n"
        return out + "\n[net]\ngit-fetch-with-cli = true\n"

    # Find the end of the [net] section (next section header or EOF).
    net_end = len(lines)
    for j in range(net_start + 1, len(lines)):
        if lines[j].startswith("["):
            net_end = j
            break
    body = lines[net_start + 1 : net_end]
    has_key = False
    patched: list[str] = []
    for line in body:
        if line.strip().startswith("git-fetch-with-cli"):
            has_key = True
            patched.append("git-fetch-with-cli = true")
        else:
            patched.append(line)
    if not has_key:
        patched = ["git-fetch-with-cli = true"] + patched
    return "\n".join(lines[: net_start + 1] + patched + lines[net_end:])


# ── Preflight (local) ────────────────────────────────────────────────────────


def check_key_material(keys_dir: Path) -> None:
    """Die like the bash script when the key directory or any key is missing."""
    if not keys_dir.is_dir():
        raise CredentialsError(f"key directory not found: {keys_dir}")
    missing: list[str] = []
    for repo in REPOS:
        key = keys_dir / key_basename(repo)
        if not key.is_file():
            missing.append(str(key))
        if not key.with_suffix(key.suffix + ".pub").is_file():
            missing.append(str(key) + ".pub")
    if missing:
        raise CredentialsError("missing key material: " + " ".join(missing))


def resolve_password(env: dict) -> str:
    """Password from ``CI_RUNNER_PASSWORD`` or the 600-mode password file."""
    password = env.get("CI_RUNNER_PASSWORD", "")
    if password:
        return password
    path = Path(env.get("CI_RUNNER_PASSWORD_FILE", str(DEFAULT_RUNNER_PASSWORD_FILE)))
    try:
        if path.is_file() and os.access(path, os.R_OK):
            return path.read_text(encoding="utf-8").strip()
    except OSError:
        pass
    return ""


def build_ssh_command(env: dict) -> list[str]:
    """The ssh (optionally sshpass-wrapped) argv used to reach each runner."""
    timeout = env.get("CI_RUNNER_CONNECT_TIMEOUT", str(DEFAULT_CONNECT_TIMEOUT))
    ssh_opts = [
        "-o", "StrictHostKeyChecking=accept-new",
        "-o", f"ConnectTimeout={timeout}",
        "-o", "LogLevel=ERROR",
    ]
    password = resolve_password(env)
    if password:
        if not any(
            os.access(os.path.join(dirpath, "sshpass"), os.X_OK)
            for dirpath in os.get_exec_path()
        ):
            raise CredentialsError("sshpass not found but a password is set")
        return ["sshpass", "-p", password, "ssh", *ssh_opts]
    return ["ssh", *ssh_opts]


# ── Remote script emission ───────────────────────────────────────────────────


def remote_header(no_restart: str, force_restart: str, pnpm_version: str) -> str:
    """Fixed remote head plus locally-switched values, shlex-quoted."""
    lines = [
        "set -euo pipefail",
        "umask 077",
        "REMOTE_FAIL=0",
        "REPOS=" + shlex.quote(" ".join(REPOS)),
        'mkdir -p "$HOME/.ssh" "$HOME/.cargo"',
        'chmod 700 "$HOME/.ssh"',
        # Local switches must be re-declared on the remote side: the remote
        # script runs in its own shell (set -u), so referencing a local-only
        # variable aborts it (measured 2026-09-15).
        "CI_RUNNER_NO_RESTART=" + shlex.quote(no_restart),
        "CI_RUNNER_FORCE_RESTART=" + shlex.quote(force_restart),
        "CI_RUNNER_PNPM_VERSION=" + shlex.quote(pnpm_version),
    ]
    return "\n".join(lines) + "\n"


def key_heredocs(keys_dir: Path) -> str:
    """Stream each private key as one strict heredoc, then chmod 600 it."""
    parts: list[str] = []
    for repo in REPOS:
        key = keys_dir / key_basename(repo)
        tag = f"CIKEY_EOF_{repo}"
        parts.append(f'cat > "$HOME/.ssh/id_ed25519_ci_{repo}" <<{tag}\n')
        parts.append(key.read_text(encoding="utf-8"))
        parts.append(f"{tag}\n")
        parts.append(f'chmod 600 "$HOME/.ssh/id_ed25519_ci_{repo}"\n')
    return "".join(parts)


REMOTE_SSH_GIT_CARGO = r'''
# --- 2. ~/.ssh/config: replace only our managed block -------------------------
CFG="$HOME/.ssh/config"
BEGIN='__BEGIN__'
END='__END__'
touch "$CFG"
chmod 600 "$CFG"
awk -v b="$BEGIN" -v e="$END" '
  index($0,b)==1 {skip=1; next}
  index($0,e)==1 {skip=0; next}
  !skip {print}
' "$CFG" > "$CFG.ci-tmp.$$"
mv "$CFG.ci-tmp.$$" "$CFG"
{
  printf '%s\n' "$BEGIN"
'''.replace("__BEGIN__", MANAGED_BEGIN).replace("__END__", MANAGED_END)


def ssh_config_emitter() -> str:
    lines = [REMOTE_SSH_GIT_CARGO.rstrip("\n")]
    for repo in REPOS:
        lines += [
            f'  printf \'Host gh-ci-%s\\n\'          "{repo}"',
            '  printf \'  HostName github.com\\n\'',
            '  printf \'  User git\\n\'',
            f'  printf \'  IdentityFile ~/.ssh/id_ed25519_ci_{repo}\\n\'',
            '  printf \'  IdentitiesOnly yes\\n\'',
            '  printf \'  StrictHostKeyChecking accept-new\\n\'',
        ]
    lines += [
        '  printf \'%s\\n\' "$END"',
        '} >> "$CFG"',
        "",
        "# --- 3. ~/.gitconfig: exactly five insteadOf rules, nothing global -----------",
    ]
    for key, value in insteadof_rules():
        lines.append("git config --global " + shlex.quote(key) + " " + shlex.quote(value))
    return "\n".join(lines) + "\n"


REMOTE_TAIL = r'''
# --- 4. ~/.cargo/config.toml: add [net] git-fetch-with-cli, preserve the rest -
CC="$HOME/.cargo/config.toml"
[ -f "$CC" ] || : > "$CC"
[ -f "$CC.pre-ci-credentials.bak" ] || cp "$CC" "$CC.pre-ci-credentials.bak"
python3 - "$CC" <<'PYEOF'
import re, sys
p = sys.argv[1]
s = open(p).read()
m = re.search(r'(?m)^\[net\][^\n]*\n', s)
if m:
    start = m.end()
    nxt = re.search(r'(?m)^\[', s[start:])
    end = start + (nxt.start() if nxt else len(s) - start)
    body = s[start:end]
    if re.search(r'(?m)^\s*git-fetch-with-cli\s*=', body):
        body = re.sub(r'(?m)^\s*git-fetch-with-cli\s*=.*$',
                      'git-fetch-with-cli = true', body)
    else:
        body = 'git-fetch-with-cli = true\n' + body
    s = s[:start] + body + s[end:]
else:
    if s and not s.endswith('\n'):
        s += '\n'
    s += '\n[net]\ngit-fetch-with-cli = true\n'
open(p, 'w').write(s)
PYEOF

# --- 5. runner-unit env: CARGO_NET_GIT_FETCH_WITH_CLI --------------------------
# Step 4 writes `[net] git-fetch-with-cli` into ~/.cargo/config.toml, and a
# workflow-level cache can silently undo it (evernight caches the whole
# ~/.cargo directory).  An environment variable on the runner service cannot be
# clobbered by a cache restore.
UNIT="actions.runner.celestia-island.$(hostname).service"
DROPIN="/etc/systemd/system/$UNIT.d/env-cargo.conf"
if systemctl list-unit-files "$UNIT" >/dev/null 2>&1; then
  WANT='[Service]
Environment=CARGO_NET_GIT_FETCH_WITH_CLI=true'
  HAVE="$(sudo -n cat "$DROPIN" 2>/dev/null || true)"
  if [ "$HAVE" = "$WANT" ]; then
    echo "--- runner env drop-in: already correct, no restart"
  else
    sudo -n mkdir -p "/etc/systemd/system/$UNIT.d"
    printf '%s\n' "$WANT" | sudo -n tee "$DROPIN" >/dev/null
    sudo -n systemctl daemon-reload
    # Never restart when a job is running unless explicitly forced; never claim
    # success when the restart failed (a failed restart means the env var is
    # not active yet).
    if [ "$CI_RUNNER_NO_RESTART" = "1" ]; then
      echo "--- runner env drop-in: (re)written; restart SKIPPED (CI_RUNNER_NO_RESTART=1)"
      echo "    NOTE: the env var is only visible to processes started after a restart."
    elif pgrep -f "Runner.Worker" >/dev/null 2>&1 && [ "$CI_RUNNER_FORCE_RESTART" != "1" ]; then
      echo "--- runner env drop-in: (re)written; restart SKIPPED (a job is running: Runner.Worker present)"
      echo "    NOTE: re-run when the runner is idle (or set CI_RUNNER_FORCE_RESTART=1) to activate it."
    elif sudo -n systemctl restart "$UNIT"; then
      echo "--- runner env drop-in: (re)written, runner restarted"
      sudo -n systemctl show -p Environment "$UNIT" 2>/dev/null | grep -q CARGO_NET_GIT_FETCH_WITH_CLI \
        || { echo "!! restart done but the unit env still lacks CARGO_NET_GIT_FETCH_WITH_CLI"; REMOTE_FAIL=1; }
    else
      echo "!! runner restart FAILED — the env var is NOT active"; REMOTE_FAIL=1
    fi
  fi
  echo "--- runner env drop-in lines: $(sudo -n grep -c CARGO_NET_GIT_FETCH_WITH_CLI "$DROPIN" 2>/dev/null)"
else
  echo "!! runner unit $UNIT not found (systemd unit names do not always match the hostname)"
  echo "    -> the env drop-in was NOT installed; pass the real unit name or fix the mismatch"
  REMOTE_FAIL=1
fi

# --- 6. non-login PATH: expose the nvm toolchain through /usr/local/bin ---------
NODE_BIN="$(ls -d "$HOME"/.nvm/versions/node/*/bin 2>/dev/null | sort -V | tail -1)"
if [ -n "$NODE_BIN" ]; then
  for t in node npm npx corepack; do
    [ -e "$NODE_BIN/$t" ] && sudo -n ln -sfn "$NODE_BIN/$t" "/usr/local/bin/$t"
  done
  # nvm ships no pnpm; corepack enable would resolve to the LATEST pnpm.  Pin a
  # tiny wrapper instead -- deterministic, offline, honours caller COREPACK_HOME.
  if [ ! -e "$NODE_BIN/pnpm" ] && [ -x "$NODE_BIN/corepack" ]; then
    printf '#!/bin/sh\nexec corepack pnpm@%s "$@"\n' "$CI_RUNNER_PNPM_VERSION" \
      | sudo -n tee /usr/local/bin/pnpm >/dev/null
    sudo -n chmod 755 /usr/local/bin/pnpm
    echo "--- /usr/local/bin/pnpm: wrapper pinned to pnpm@$CI_RUNNER_PNPM_VERSION"
  fi
  echo "--- /usr/local/bin toolchain <- $NODE_BIN"
else
  echo "--- no nvm node found under $HOME/.nvm; skipped the /usr/local/bin symlinks"
fi

# --- summary -----------------------------------------------------------------
echo "=== applied on $(hostname) ==="
echo "--- key modes ---"
ls -l "$HOME/.ssh"/id_ed25519_ci_* | awk '{print $1, $NF}'
echo "--- insteadOf rules ---"
git config --global --get-regexp '^url\..*\.insteadof$' | sort
echo "--- cargo [net] ---"
grep -A2 '^\[net\]' "$CC"
echo "--- /usr/local/bin toolchain ---"
ls -l /usr/local/bin/node /usr/local/bin/pnpm /usr/local/bin/corepack 2>/dev/null \
  | awk '{print $9, "->", $11}' || true
if [ "$REMOTE_FAIL" = "1" ]; then
  echo "!! some steps failed / did not take effect (see the !! lines above) -- exiting non-zero"
  exit 1
fi
'''

REMOTE_PACKAGES = r'''
# --- 1b. python3-venv + python3-pip: the baseline ships neither pip nor ensurepip ---
# Packages LAST, deliberately: this is the only step that can fail for reasons
# outside our control; credentials first means a package failure costs only the
# venv, and the exit code still tells the caller.
sudo -n dpkg --configure -a >/dev/null 2>&1 || true
missing=""
dpkg -s python3-venv >/dev/null 2>&1 || missing="$missing python3-venv"
dpkg -s python3-pip  >/dev/null 2>&1 || missing="$missing python3-pip"
if [ -z "$missing" ]; then
  echo "--- python3-venv + python3-pip: already installed"
else
  echo "--- python3-venv + python3-pip: installing ($missing) (direct fetch; the image's apt proxy stalls USTC)"
  sudo -n apt-get -o Acquire::http::Proxy=false -o Acquire::https::Proxy=false \
       -o DPkg::Lock::Timeout=180 update
  sudo -n apt-get -o Acquire::http::Proxy=false -o Acquire::https::Proxy=false \
       -o DPkg::Lock::Timeout=180 -f install -y >/dev/null 2>&1 || true
  sudo -n apt-get -o Acquire::http::Proxy=false -o Acquire::https::Proxy=false \
       -o DPkg::Lock::Timeout=180 install -y $missing
fi
rm -rf "$HOME/.ci-venv-probe"
if python3 -m venv "$HOME/.ci-venv-probe" >/dev/null 2>&1; then
  echo "--- python3 -m venv: OK"
else
  echo "!!! python3 -m venv still FAILS — every python CI job will die at Setup python" >&2
  rm -rf "$HOME/.ci-venv-probe"
  exit 1
fi
rm -rf "$HOME/.ci-venv-probe"
if command -v pip >/dev/null 2>&1; then
  echo "--- pip: OK ($(pip --version 2>&1 | head -1))"
else
  echo "!!! pip BINARY still MISSING — every workflow running 'pip install' dies at exit 127" >&2
  exit 1
fi
'''

REMOTE_PACKAGES_SKIPPED = 'echo "--- system packages: skipped (CI_RUNNER_SKIP_PACKAGES=1)"\n'


def emit_remote_script(env: dict, keys_dir: Path) -> str:
    """Build the full remote apply script streamed over ssh's stdin.

    Nothing is staged in a temp file on either side; key material is only
    inlined into the ssh stdin stream.
    """
    parts = [
        remote_header(
            env.get("CI_RUNNER_NO_RESTART", DEFAULT_NO_RESTART),
            env.get("CI_RUNNER_FORCE_RESTART", DEFAULT_FORCE_RESTART),
            env.get("CI_RUNNER_PNPM_VERSION", DEFAULT_PNPM_VERSION),
        ),
        key_heredocs(keys_dir),
        ssh_config_emitter(),
        REMOTE_TAIL,
    ]
    if env.get("CI_RUNNER_SKIP_PACKAGES") == "1":
        parts.append(REMOTE_PACKAGES_SKIPPED)
    else:
        parts.append(REMOTE_PACKAGES)
    return "".join(parts)


# ── Apply ────────────────────────────────────────────────────────────────────


def all_hosts(env: dict) -> list[str]:
    """Runner list for ``all``; runtime values from CI_RUNNER_ALL_HOSTS."""
    raw = env.get("CI_RUNNER_ALL_HOSTS", "").strip()
    if not raw:
        raise CredentialsError(
            "CI_RUNNER_ALL_HOSTS is not set; 'all' needs the runner list "
            "(space-separated hostnames/IPs, e.g. from AGENTS §8.1)"
        )
    return raw.split()


def apply_host(host: str, ssh_cmd: list[str], user: str, remote_script: str) -> bool:
    print(f"############ configuring {host} ############", flush=True)
    cmd = [*ssh_cmd, f"{user}@{host}", "bash -s"]
    try:
        proc = subprocess.run(cmd, input=remote_script, text=True)
    except OSError as exc:
        print(f"ci-runner-credentials: FAILED on {host}: {exc}", file=sys.stderr)
        return False
    if proc.returncode != 0:
        print(f"ci-runner-credentials: FAILED on {host}", file=sys.stderr)
        return False
    return True


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="ci_runner_credentials.py",
        description=(
            "Install read-only deploy-key SSH credentials for private git deps "
            "onto a celestia-island self-hosted CI runner (idempotent)."
        ),
        epilog=(
            "Re-run freely after any ESXi baseline revert; the runners are "
            "stateless. Credentials come from CI_KEYS_DIR at run time; the SSH "
            "password comes from CI_RUNNER_PASSWORD / CI_RUNNER_PASSWORD_FILE."
        ),
    )
    parser.add_argument("host", help="runner hostname or IP, or 'all'")
    parser.add_argument(
        "--user", default=os.environ.get("CI_RUNNER_USER", DEFAULT_RUNNER_USER),
        help="runner login user (default: %(default)s)",
    )
    parser.add_argument(
        "--keys-dir", type=Path,
        default=Path(os.environ.get("CI_KEYS_DIR", str(DEFAULT_KEYS_DIR))),
        help="local key material directory (default: %(default)s)",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    env = os.environ
    try:
        check_key_material(args.keys_dir)
        ssh_cmd = build_ssh_command(env)
        remote_script = emit_remote_script(env, args.keys_dir)
        hosts = all_hosts(env) if args.host == "all" else [args.host]
    except CredentialsError as exc:
        print(f"ci-runner-credentials: {exc}", file=sys.stderr)
        return EXIT_FAILURE

    rc = EXIT_OK
    for host in hosts:
        if not apply_host(host, ssh_cmd, args.user, remote_script):
            rc = EXIT_FAILURE
    return rc


if __name__ == "__main__":
    sys.exit(main())

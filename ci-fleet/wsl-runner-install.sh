#!/usr/bin/env bash
# wsl-runner-install.sh — Linux-side installer, runs INSIDE WSL2 (Ubuntu 22.04) as root.
#
# Called by win-register-runner.ps1 (or by hand). Installs a GitHub Actions
# self-hosted runner as a systemd service and all the house tooling the
# celestia-island workflows expect (node 22, pnpm via corepack, python3-pip,
# celestia-devtools, just).
#
# Inputs (environment, forwarded over the WSL boundary via WSLENV):
#   RUNNER_TOKEN   (required) registration token from
#                  https://github.com/organizations/<org>/settings/actions/runners
#   RUNNER_ORG     (default celestia-island)
#   RUNNER_NAME    (default <hostname>-wsl)
#   RUNNER_LABELS  (default self-hosted,linux,x64,local,wsl)
#   RUNNER_VERSION (default 2.337.0 — same series as the node-ci fleet)
#   GH_PROXY       (optional) mirror prefix for github downloads, e.g.
#                  https://ghfast.top  ->  $GH_PROXY/https://github.com/...
#                  Accelerates downloads ONLY (the runner's runtime polling
#                  always talks to api.github.com directly or via the proxy).
#   HTTPS_PROXY / HTTP_PROXY / NO_PROXY
#                  (optional) forwarded to apt/curl/node/pip AND baked into
#                  the runner systemd unit, so the runner service and its
#                  jobs reach GitHub through the lab proxy. This is what
#                  keeps the runner connected on firewalled networks.
#
# Idempotent: re-running re-registers (--replace) and restarts the service.

set -euo pipefail

RUNNER_TOKEN="${RUNNER_TOKEN:?RUNNER_TOKEN is required}"
RUNNER_ORG="${RUNNER_ORG:-celestia-island}"
RUNNER_NAME="${RUNNER_NAME:-$(hostname)-wsl}"
RUNNER_LABELS="${RUNNER_LABELS:-self-hosted,linux,x64,local,wsl}"
RUNNER_VERSION="${RUNNER_VERSION:-2.337.0}"
RUNNER_USER="runner"
INSTALL_DIR="/home/${RUNNER_USER}/actions-runner"
GH_PROXY="${GH_PROXY:-}"

# gh-url <absolute-github-url>: prepend the mirror prefix when configured.
gh_url() {
  if [ -n "$GH_PROXY" ]; then
    echo "${GH_PROXY%/}/$1"
  else
    echo "$1"
  fi
}

# Route the script's own downloads (apt/curl/node/pip) through the proxy.
if [ -n "${HTTPS_PROXY:-}" ]; then
  export https_proxy="$HTTPS_PROXY" http_proxy="${HTTP_PROXY:-$HTTPS_PROXY}"
fi

echo "[0/7] connectivity"
gh_gh="https://github.com"
probe_code=$(curl -s -o /dev/null -w '%{http_code}' --max-time 10 "$gh_gh" || true)
echo "github.com direct: ${probe_code:-unreachable} (a blocked network is fine when HTTPS_PROXY/GH_PROXY is set)"

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
# Proxy environment is baked into the unit: the runner's api.github.com
# long-poll AND the job processes it spawns inherit these, which is what
# keeps a firewalled host connected. NO_PROXY keeps local/LAN traffic out.
proxy_env=""
if [ -n "${HTTPS_PROXY:-}" ]; then
  proxy_env="Environment=HTTPS_PROXY=${HTTPS_PROXY}${HTTP_PROXY:+
Environment=HTTP_PROXY=${HTTP_PROXY}}${NO_PROXY:+
Environment=NO_PROXY=${NO_PROXY}}"
fi
cat > /etc/systemd/system/actions-runner.service <<EOF
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
EOF
systemctl daemon-reload
systemctl enable --now actions-runner.service

sleep 3
systemctl --no-pager status actions-runner.service | head -5 || true
echo "DONE: runner '${RUNNER_NAME}' is registered and running."
echo "Watch it join the pool: https://github.com/organizations/${RUNNER_ORG}/settings/actions/runners"

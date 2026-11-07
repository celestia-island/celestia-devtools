#!/usr/bin/env bash
# Idempotent post-revert bootstrap for the self-hosted CI VMs (node-ci-1..4).
#
# WHY THIS EXISTS
#   The CI VMs are stateless by design: when one misbehaves it is reverted to its
#   baseline snapshot (see the `ci-ops` skill). Anything hand-edited on the guest
#   is therefore lost on revert. Two pieces of guest state are load-bearing for
#   every Rust job and every cross-repo checkout, and both have broken in
#   production:
#
#   1. ~/.cargo/config.toml — the crates.io source replacement. It pointed at
#      TUNA until 2026-11-01, when TUNA began answering 403 to this fleet
#      (the domain root 403s too, so it is the mirror blocking our egress, not
#      our proxy). Every Rust job then died at dependency resolution, which
#      looks exactly like a code failure. `index.crates.io` works directly as a
#      fallback but rsproxy is the CN-fast default.
#
#   2. ~/.gitconfig url.<alias>.insteadOf — the org's internal linkage (see
#      celestia-devtools README / `npm/link_siblings.py`): github.com URLs are
#      rewritten onto `gh-ci-<repo>` SSH aliases so private org repos can be
#      cloned with per-repo deploy keys and no cross-repo PAT.
#      NOTE: `actions/checkout` overrides HOME, so a checkout of ANOTHER repo
#      misses ~/.ssh/config and cannot resolve the alias — cross-repo checkouts
#      must use a plain `git clone gh-ci-<repo>:celestia-island/<repo>.git`.
#
# Run as the runner user (lab) after a baseline revert, or as the last step of
# any VM provisioning. Safe to re-run: every change is compared before writing.
#
#   ./vm-runner-bootstrap.sh          # apply
#   ./vm-runner-bootstrap.sh --check  # report only, exit 1 if drift found

set -euo pipefail

REPOS=(akivili arona entelecheia evernight kei-wire shittim-chest)
CARGO_MIRROR_NAME="rsproxy"
CARGO_MIRROR_URL="sparse+https://rsproxy.cn/index/"
CHECK_ONLY=0
[ "${1:-}" = "--check" ] && CHECK_ONLY=1

drift=0
changed=0

say()  { printf '%s\n' "$*"; }
ok()   { printf '  [ok]    %s\n' "$*"; }
warn() { printf '  [warn]  %s\n' "$*"; drift=$((drift + 1)); }
act()  { printf '  [apply] %s\n' "$*"; changed=$((changed + 1)); }

# ── 1. cargo source mirror ────────────────────────────────────────────────────
want_cargo() {
  cat <<EOF
# Managed by celestia-devtools ci-fleet/vm-runner-bootstrap.sh — do not hand-edit.
# TUNA started answering 403 to this fleet on 2026-11-01; rsproxy is the tested
# replacement (index.crates.io also works directly if rsproxy ever dies).
[source.crates-io]
replace-with = "$CARGO_MIRROR_NAME"

[source.$CARGO_MIRROR_NAME]
registry = "$CARGO_MIRROR_URL"

[net]
git-fetch-with-cli = true
EOF
}

say "== cargo source replacement =="
cfg="$HOME/.cargo/config.toml"
if [ -f "$cfg" ] && grep -qF "$CARGO_MIRROR_URL" "$cfg"; then
  ok "already on $CARGO_MIRROR_NAME"
else
  if [ "$CHECK_ONLY" = 1 ]; then
    warn "$cfg is not using $CARGO_MIRROR_NAME"
  else
    mkdir -p "$HOME/.cargo"
    [ -f "$cfg" ] && cp -p "$cfg" "$cfg.bak-$(date +%Y%m%d-%H%M%S)"
    want_cargo > "$cfg"
    act "wrote $cfg"
  fi
fi

# ── 2. internal linkage: url.<alias>.insteadOf ───────────────────────────────
say "== internal repo linkage (insteadOf) =="
for r in "${REPOS[@]}"; do
  for suffix in "" ".git"; do
    src="https://github.com/celestia-island/${r}${suffix}"
    dst="gh-ci-${r}:celestia-island/${r}${suffix}"
    cur="$(git config --global --get "url.${dst}.insteadOf" 2>/dev/null || true)"
    if printf '%s\n' "$cur" | grep -qxF "$src"; then
      continue
    fi
    if [ "$CHECK_ONLY" = 1 ]; then
      warn "missing insteadOf: $src -> $dst"
    else
      git config --global --add "url.${dst}.insteadOf" "$src"
      act "added insteadOf $src"
    fi
  done
done

# ── 3. per-repo deploy keys / ssh aliases (presence only) ────────────────────
say "== ssh aliases and deploy keys =="
for r in "${REPOS[@]}"; do
  alias_="gh-ci-${r}"
  if grep -qE "^Host[[:space:]]+(${alias_}|.*[[:space:]]${alias_})$" "$HOME/.ssh/config" 2>/dev/null; then
    if ls "$HOME/.ssh/id_ed25519_ci_${r}" >/dev/null 2>&1; then
      ok "$alias_ + key present"
    else
      warn "$alias_ configured but ~/.ssh/id_ed25519_ci_${r} is missing (deploy key not provisioned)"
    fi
  else
    warn "$alias_ alias missing from ~/.ssh/config (deploy key not provisioned)"
  fi
done

# ── 4. live checks (only when we can actually reach out) ────────────────────
if [ "$CHECK_ONLY" = 0 ]; then
  say "== live checks =="
  code="$(curl -sS -o /dev/null -w '%{http_code}' --max-time 15 "${CARGO_MIRROR_URL#sparse+}config.json" || echo "000")"
  if [ "$code" = "200" ]; then ok "$CARGO_MIRROR_NAME index answers 200"; else warn "$CARGO_MIRROR_NAME index answered $code"; fi
  if timeout 25 git ls-remote "gh-ci-entelecheia:celestia-island/entelecheia.git" refs/heads/master >/dev/null 2>&1; then
    ok "cross-repo alias resolves (entelecheia)"
  else
    warn "cross-repo alias for entelecheia does not resolve"
  fi
fi

say ""
if [ "$CHECK_ONLY" = 1 ]; then
  say "check complete: $drift drift item(s)"
  [ "$drift" = 0 ] || exit 1
else
  say "apply complete: $changed change(s), $drift warning(s)"
fi

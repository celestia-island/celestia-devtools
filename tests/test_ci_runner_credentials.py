"""Tests for tools/ci_runner_credentials.py (port of ci-runner-credentials.sh).

Covers: generated ssh_config text structure (alias blocks, insteadOf lines,
git-fetch-with-cli), idempotency of config rendering, and error handling when
the key directory is missing.  Everything remote is mocked; only pure text
rendering and local preflight logic are exercised.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

_TOOLS = Path(__file__).resolve().parent.parent / "tools"
_SPEC = importlib.util.spec_from_file_location(
    "ci_runner_credentials", _TOOLS / "ci_runner_credentials.py"
)
crc = importlib.util.module_from_spec(_SPEC)
sys.modules.setdefault("ci_runner_credentials", crc)
_SPEC.loader.exec_module(crc)

REPOS = ("akivili", "arona", "entelecheia", "evernight", "shittim-chest")


# ── ssh_config structure ─────────────────────────────────────────────────────


def test_managed_block_has_all_alias_blocks():
    block = crc.render_managed_ssh_block()
    lines = block.splitlines()
    assert lines[0] == crc.MANAGED_BEGIN
    assert lines[-1] == crc.MANAGED_END
    for repo in REPOS:
        assert f"Host gh-ci-{repo}" in lines
        idx = lines.index(f"Host gh-ci-{repo}")
        assert lines[idx + 1] == "  HostName github.com"
        assert lines[idx + 2] == "  User git"
        assert lines[idx + 3] == f"  IdentityFile ~/.ssh/id_ed25519_ci_{repo}"
        assert lines[idx + 4] == "  IdentitiesOnly yes"
        assert lines[idx + 5] == "  StrictHostKeyChecking accept-new"


def test_apply_replaces_only_managed_block():
    existing = (
        "# my local tweaks\n"
        "Host my-box\n"
        "  HostName example.invalid\n"
        + crc.MANAGED_BEGIN
        + "\nHost gh-ci-akivili\n  HostName stale.invalid\n"
        + crc.MANAGED_END
        + "\n"
    )
    merged = crc.apply_managed_ssh_block(existing)
    # stale managed content is gone, user content preserved, new block appended
    assert "stale.invalid" not in merged
    assert "HostName example.invalid" in merged
    assert crc.render_managed_ssh_block() in merged
    assert merged.endswith("\n")


def test_apply_to_empty_and_missing_content():
    assert crc.apply_managed_ssh_block(None) == crc.render_managed_ssh_block()
    assert crc.apply_managed_ssh_block("") == crc.render_managed_ssh_block()


# ── idempotency ──────────────────────────────────────────────────────────────


def test_managed_block_rendering_is_idempotent():
    once = crc.render_managed_ssh_block()
    assert once == crc.render_managed_ssh_block()
    # applying the rendered block to itself changes nothing
    assert crc.apply_managed_ssh_block(once) == once


def test_apply_idempotent_over_user_content():
    existing = "# user stuff\n"
    once = crc.apply_managed_ssh_block(existing)
    twice = crc.apply_managed_ssh_block(once)
    assert once == twice
    assert once.count(crc.MANAGED_BEGIN) == 1


def test_strip_block_idempotent():
    text = "keep\n" + crc.render_managed_ssh_block() + "tail\n"
    once = crc.strip_managed_ssh_block(text)
    assert crc.MANAGED_BEGIN not in once
    assert "keep" in once and "tail" in once
    assert crc.strip_managed_ssh_block(once) == once


# ── gitconfig insteadOf rules ────────────────────────────────────────────────


def test_insteadof_rules_cover_exactly_five_private_repos():
    rules = crc.insteadof_rules()
    assert len(rules) == 5
    for (key, value), repo in zip(rules, REPOS, strict=True):
        assert key == f"url.gh-ci-{repo}:celestia-island/{repo}.git.insteadOf"
        assert value == f"https://github.com/celestia-island/{repo}.git"
    # no blanket github.com rewrite
    assert not any(v == "https://github.com/" for _, v in rules)


def test_key_basenames_match_bash_mapping():
    assert crc.key_basename("akivili") == "gh-ci-readonly"
    assert crc.key_basename("arona") == "gh-ci-readonly-arona"
    assert crc.key_basename("shittim-chest") == "gh-ci-readonly-shittim-chest"


# ── cargo config patch ───────────────────────────────────────────────────────


def test_cargo_patch_adds_net_section():
    original = "[registry]\nindex = \"sparrow\"\n"
    patched = crc.patch_cargo_net_config(original)
    assert "[net]" in patched
    assert "git-fetch-with-cli = true" in patched
    assert 'index = "sparrow"' in patched


def test_cargo_patch_preserves_existing_net_keys():
    original = "[net]\nretry = 3\n[registries.tuna]\nindex = \"x\"\n"
    patched = crc.patch_cargo_net_config(original)
    assert "retry = 3" in patched
    assert "git-fetch-with-cli = true" in patched
    assert "[registries.tuna]" in patched


def test_cargo_patch_idempotent():
    original = "[net]\ngit-fetch-with-cli = false\n"
    once = crc.patch_cargo_net_config(original)
    assert "git-fetch-with-cli = true" in once
    assert "false" not in once
    assert crc.patch_cargo_net_config(once) == once


# ── preflight: missing key directory / keys ─────────────────────────────────


def test_missing_key_directory_raises(tmp_path: Path):
    with pytest.raises(crc.CredentialsError, match="key directory not found"):
        crc.check_key_material(tmp_path / "does-not-exist")


def test_missing_key_files_reported(tmp_path: Path):
    (tmp_path / "gh-ci-readonly").write_text("KEY")
    (tmp_path / "gh-ci-readonly.pub").write_text("PUB")
    with pytest.raises(crc.CredentialsError, match="missing key material") as excinfo:
        crc.check_key_material(tmp_path)
    message = str(excinfo.value)
    for repo in REPOS[1:]:
        assert f"gh-ci-readonly-{repo}" in message


def test_complete_key_directory_passes(tmp_path: Path):
    for repo in REPOS:
        base = tmp_path / crc.key_basename(repo)
        base.write_text("KEY")
        base.with_suffix(base.suffix + ".pub").write_text("PUB")
    crc.check_key_material(tmp_path)  # must not raise


def test_main_exits_nonzero_when_key_dir_missing(tmp_path: Path, monkeypatch, capsys):
    monkeypatch.setenv("CI_RUNNER_ALL_HOSTS", "runner1.invalid")
    monkeypatch.setattr(sys, "argv", ["prog"])
    rc = crc.main(["--keys-dir", str(tmp_path / "absent"), "runner1.invalid"])
    assert rc == crc.EXIT_FAILURE
    assert "key directory not found" in capsys.readouterr().err


# ── remote script emission (structure only; nothing executed) ───────────────


@pytest.fixture()
def keys_dir(tmp_path: Path) -> Path:
    for repo in REPOS:
        base = tmp_path / crc.key_basename(repo)
        base.write_text(f"PRIVATE-KEY-{repo}\n")
        base.with_suffix(base.suffix + ".pub").write_text(f"PUBLIC-KEY-{repo}\n")
    return tmp_path


def test_remote_script_contains_all_steps(keys_dir: Path):
    script = crc.emit_remote_script({}, keys_dir)
    for repo in REPOS:
        assert f"id_ed25519_ci_{repo}" in script
        assert f"PRIVATE-KEY-{repo}" in script
        assert f'printf \'Host gh-ci-%s\\n\'          "{repo}"' in script
        assert f"url.gh-ci-{repo}:celestia-island/{repo}.git.insteadOf" in script
    assert "git-fetch-with-cli = true" in script
    assert "CARGO_NET_GIT_FETCH_WITH_CLI=true" in script
    assert "CI_RUNNER_PNPM_VERSION" in script
    assert "apt-get" in script  # packages step included by default


def test_remote_script_skip_packages(keys_dir: Path):
    script = crc.emit_remote_script({"CI_RUNNER_SKIP_PACKAGES": "1"}, keys_dir)
    assert "CI_RUNNER_SKIP_PACKAGES=1" in script
    assert "apt-get" not in script


def test_remote_script_honours_env_switches(keys_dir: Path):
    script = crc.emit_remote_script(
        {"CI_RUNNER_NO_RESTART": "1", "CI_RUNNER_FORCE_RESTART": "0",
         "CI_RUNNER_PNPM_VERSION": "11.18.0"},
        keys_dir,
    )
    assert "CI_RUNNER_NO_RESTART=1" in script
    assert "CI_RUNNER_FORCE_RESTART=0" in script
    assert "CI_RUNNER_PNPM_VERSION=11.18.0" in script
    # determinism: same inputs, same script
    assert script == crc.emit_remote_script(
        {"CI_RUNNER_NO_RESTART": "1", "CI_RUNNER_FORCE_RESTART": "0",
         "CI_RUNNER_PNPM_VERSION": "11.18.0"},
        keys_dir,
    )


def test_no_real_credentials_in_module_source():
    source = (_TOOLS / "ci_runner_credentials.py").read_text(encoding="utf-8")
    assert "hydroSinap" not in source
    for prefix in ("192.168.", "10.0.", "172.16."):
        assert prefix not in source


# ── ssh command construction ────────────────────────────────────────────────


def test_ssh_command_key_auth(monkeypatch):
    monkeypatch.setattr(crc, "resolve_password", lambda env: "")
    cmd = crc.build_ssh_command({})
    assert cmd[0] == "ssh"
    assert "-o" in cmd and "StrictHostKeyChecking=accept-new" in cmd
    assert any(c.startswith("ConnectTimeout=") for c in cmd)


def test_ssh_command_requires_sshpass_for_password(monkeypatch):
    monkeypatch.setattr(crc, "resolve_password", lambda env: "test-password")
    # fake a findable sshpass
    monkeypatch.setattr(crc.os, "get_exec_path", lambda: ["/nonexistent-bin"])
    real_access = crc.os.access

    def fake_access(path, mode):
        if str(path).endswith("sshpass"):
            return True
        return real_access(path, mode)

    monkeypatch.setattr(crc.os, "access", fake_access)
    cmd = crc.build_ssh_command({})
    assert cmd[:4] == ["sshpass", "-p", "test-password", "ssh"]


def test_ssh_command_fails_without_sshpass(monkeypatch):
    monkeypatch.setattr(crc, "resolve_password", lambda env: "test-password")
    real_access = crc.os.access

    def fake_access(path, mode):
        if str(path).endswith("sshpass"):
            return False
        return real_access(path, mode)

    monkeypatch.setattr(crc.os, "access", fake_access)
    with pytest.raises(crc.CredentialsError, match="sshpass not found"):
        crc.build_ssh_command({})

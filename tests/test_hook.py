#!/usr/bin/env python3
"""Tests for hook lifecycle management (``celestia_devtools.vcs.hook``)."""

import os
import tempfile
from pathlib import Path

import pytest

from celestia_devtools.vcs import hook


@pytest.fixture
def tmp_git_repo():
    """Create a temporary directory with a .git/hooks subdirectory."""
    with tempfile.TemporaryDirectory() as tmp:
        hooks_dir = Path(tmp) / ".git" / "hooks"
        hooks_dir.mkdir(parents=True)
        yield Path(tmp)


class TestInstallHook:
    def test_installs_and_writes_sentinel(self, tmp_git_repo):
        hook_path = tmp_git_repo / ".git" / "hooks" / "commit-msg"
        hook.install_hook(hook_path)
        content = hook_path.read_text(encoding="utf-8")
        assert hook.HOOK_SENTINEL in content
        assert "@DEVTOOLS_BIN@" not in content  # substituted

    def test_refuses_overwrite_non_managed(self, tmp_git_repo):
        hook_path = tmp_git_repo / ".git" / "hooks" / "commit-msg"
        hook_path.write_text("#!/bin/bash\necho custom")
        with pytest.raises(SystemExit):
            hook.install_hook(hook_path, force=False)

    def test_force_overwrites_non_managed(self, tmp_git_repo):
        hook_path = tmp_git_repo / ".git" / "hooks" / "commit-msg"
        hook_path.write_text("#!/bin/bash\necho custom")
        hook.install_hook(hook_path, force=True)
        content = hook_path.read_text(encoding="utf-8")
        assert hook.HOOK_SENTINEL in content

    def test_reinstalls_own_managed_hook(self, tmp_git_repo):
        hook_path = tmp_git_repo / ".git" / "hooks" / "commit-msg"
        hook.install_hook(hook_path)
        # reinstall without force should succeed (own managed)
        hook.install_hook(hook_path, force=False)
        content = hook_path.read_text(encoding="utf-8")
        assert hook.HOOK_SENTINEL in content

    def test_refuses_noa_managed_overwrite(self, tmp_git_repo):
        hook_path = tmp_git_repo / ".git" / "hooks" / "commit-msg"
        hook_path.write_text("#!/bin/bash\n# noa-managed\n echo noa")
        with pytest.raises(SystemExit):
            hook.install_hook(hook_path, force=False)


class TestUninstallHook:
    def test_uninstalls_managed_hook(self, tmp_git_repo):
        hook_path = tmp_git_repo / ".git" / "hooks" / "commit-msg"
        hook.install_hook(hook_path)
        assert hook_path.is_file()
        hook.uninstall_hook(hook_path)
        assert not hook_path.exists()

    def test_refuses_uninstall_non_managed(self, tmp_git_repo):
        hook_path = tmp_git_repo / ".git" / "hooks" / "commit-msg"
        hook_path.write_text("#!/bin/bash\necho custom")
        with pytest.raises(SystemExit):
            hook.uninstall_hook(hook_path, force=False)

    def test_force_uninstalls_non_managed(self, tmp_git_repo):
        hook_path = tmp_git_repo / ".git" / "hooks" / "commit-msg"
        hook_path.write_text("#!/bin/bash\necho custom")
        hook.uninstall_hook(hook_path, force=True)
        assert not hook_path.exists()


class TestCheckHook:
    def test_up_to_date_passes(self, tmp_git_repo):
        hook_path = tmp_git_repo / ".git" / "hooks" / "commit-msg"
        hook.install_hook(hook_path)
        rc = hook.check_hook(hook_path)
        assert rc == 0

    def test_missing_fails(self, tmp_git_repo):
        hook_path = tmp_git_repo / ".git" / "hooks" / "commit-msg"
        rc = hook.check_hook(hook_path)
        assert rc == 1

    def test_drifted_fails(self, tmp_git_repo):
        hook_path = tmp_git_repo / ".git" / "hooks" / "commit-msg"
        hook.install_hook(hook_path)
        # overwrite to cause drift
        hook_path.write_text("# celestia-devtools-managed\n# changed content")
        rc = hook.check_hook(hook_path)
        assert rc == 1


class TestShowTemplate:
    def test_contains_sentinel(self):
        template = hook.show_template()
        assert hook.HOOK_SENTINEL in template
        assert "@DEVTOOLS_BIN@" not in template  # substituted


class TestResolveDevtoolsBin:
    def test_explicit_bin(self):
        bin_val = hook._resolve_devtools_bin("celestia-devtools")
        assert bin_val == "celestia-devtools"

    def test_fallback(self):
        bin_val = hook._resolve_devtools_bin(None)
        # Should contain either 'celestia-devtools' or 'python'
        assert "celestia" in bin_val.lower() or "python" in bin_val.lower()

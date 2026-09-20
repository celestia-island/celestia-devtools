"""Tests for celestia_devtools.pyshim.

The syntax-floor test is the load-bearing one: the shim's whole reason to
exist is running on ancient interpreters, and the gate must have teeth —
which the walrus self-proof asserts (a checker that accepts everything is a
checker that checks nothing).
"""

from __future__ import annotations

import ast
import io
import re
import sys
from pathlib import Path

import pytest

from celestia_devtools import pyshim

SHIM = Path(pyshim.__file__).read_text(encoding="utf-8")

# The grammar floor is ONE constant shared by the gate and its self-proof.
# R1 mutation c+ proved the danger of two literals: raising only the gate's
# version to (3, 12) while keeping the self-proof's own (3, 6) left the whole
# suite green with a real walrus in the shim. With a shared constant, raising
# it disarms the gate AND arms the self-proof's failure in the same breath.
PYSHIM_GRAMMAR_FLOOR = (3, 6)


def _parse_floor(source: str):
    """Parse under the shared floor, degrading one notch only if this
    interpreter refuses the exact version (unsupported feature_version)."""
    try:
        return ast.parse(source, feature_version=PYSHIM_GRAMMAR_FLOOR)
    except (ValueError, TypeError):
        return ast.parse(source, feature_version=(PYSHIM_GRAMMAR_FLOOR[0],
                                                   PYSHIM_GRAMMAR_FLOOR[1] + 1))


class TestSyntaxFloor:
    def test_parses_under_floor_grammar(self):
        _parse_floor(SHIM)

    def test_gate_actually_rejects_newer_syntax(self):
        # Self-proof (zero-hit rule), sharing the gate's constant: if the
        # floor is raised enough to accept a walrus, THIS test goes red —
        # a disarmed gate can no longer pass silently.
        with pytest.raises(SyntaxError):
            _parse_floor("if (x := 1):\n    pass\n")

    def test_no_f_strings_or_future_annotations(self):
        # Belt next to braces: the shim promises .format() and no postponed
        # annotations (3.7+ only). Line-anchored regexes so the *prose* in the
        # module docstring (which mentions these constructs) cannot trip them.
        assert not re.search(r"^\s*from\s+__future__", SHIM, re.MULTILINE)
        assert not re.search(r"('''|\"\"\")[^\n]*\bf[\"']", SHIM)  # no f-string literals
        for line in SHIM.splitlines():
            code = line.strip()
            assert not code.startswith("f\"") and not code.startswith("f'"), line


class TestAssetPicker:
    NAMES = [
        "cpython-3.10.21+20260901-x86_64-unknown-linux-gnu-install_only.tar.gz",
        "cpython-3.11.16+20260901-x86_64-unknown-linux-gnu-install_only.tar.gz",
        "cpython-3.14.7+20260901-x86_64-unknown-linux-gnu-install_only.tar.gz",
        "cpython-3.14.7+20260901-x86_64-unknown-linux-gnu-install_only_stripped.tar.gz",
        "cpython-3.14.7+20260901-x86_64-unknown-linux-gnu-debug-full.tar.zst",
        "cpython-3.14.7+20260901-aarch64-apple-darwin-install_only.tar.gz",
        "cpython-3.9.99+20260901-x86_64-unknown-linux-gnu-install_only.tar.gz",
    ]

    def test_picks_newest_at_or_above_floor(self):
        hit = pyshim.pick_pbs_asset(self.NAMES, "x86_64", "Linux")
        assert hit is not None
        (version, name) = hit
        assert version == (3, 14, 7)
        assert name.endswith("x86_64-unknown-linux-gnu-install_only.tar.gz")

    def test_arm_machine_maps_to_aarch64(self):
        hit = pyshim.pick_pbs_asset(self.NAMES + [
            "cpython-3.13.15+20260901-aarch64-unknown-linux-gnu-install_only.tar.gz"],
            "arm64", "Linux")
        assert hit is not None and hit[0] == (3, 13, 15)

    def test_below_floor_or_wrong_shape_never_selected(self):
        assert pyshim.pick_pbs_asset(self.NAMES, "x86_64", "Haiku") is None
        only_old = [self.NAMES[0], self.NAMES[-1]]
        assert pyshim.pick_pbs_asset(only_old, "x86_64", "Linux") is None


class TestTriple:
    def test_mappings(self):
        assert pyshim._triple("x86_64", "Linux") == "x86_64-unknown-linux-gnu"
        assert pyshim._triple("amd64", "Linux") == "x86_64-unknown-linux-gnu"
        assert pyshim._triple("aarch64", "Linux") == "aarch64-unknown-linux-gnu"
        assert pyshim._triple("arm64", "Darwin") == "aarch64-apple-darwin"
        assert pyshim._triple("x86_64", "Plan9") == ""


class TestRunGuards:
    def test_sentinel_loop_detected(self, monkeypatch):
        monkeypatch.setenv(pyshim.SENTINEL, "1")
        with pytest.raises(SystemExit) as ei:
            pyshim.run([])
        assert ei.value.code == 1

    def test_venv_present_dry_run_never_reexecs(self, monkeypatch):
        monkeypatch.setattr(pyshim, "venv_python_bin", lambda: "/opt/celestia-devtools/venv/bin/celestia-devtools")
        monkeypatch.setattr(pyshim.os.path, "exists", lambda p: p.endswith("celestia-devtools"))
        monkeypatch.setattr(pyshim, "_reexec", _no_reexec)
        assert pyshim.run(["doctor"], dry_run=True) == 0

    def test_floor_met_dry_run_only_creates_venv(self, monkeypatch):
        monkeypatch.setattr(pyshim, "venv_python_bin", lambda: "/nonexistent/tool")
        monkeypatch.setattr(pyshim, "_create_venv", _no_create)
        monkeypatch.setattr(pyshim, "_reexec", _no_reexec)
        assert pyshim.run([], assume_yes=True, dry_run=True) == 0

    def test_floor_unmet_dry_run_names_strategies(self, monkeypatch, capsys):
        monkeypatch.setattr(pyshim, "venv_python_bin", lambda: "/nonexistent/tool")
        monkeypatch.setattr(pyshim, "_uv_python", _no_install)
        monkeypatch.setattr(pyshim, "_pbs_python", _no_install)
        monkeypatch.setattr(pyshim, "_pkg_python", _no_install)
        monkeypatch.setattr(pyshim, "_create_venv", _no_create)
        monkeypatch.setattr(pyshim, "_reexec", _no_reexec)
        rc = pyshim.run(["--version"], assume_yes=True, dry_run=True,
                        version_info=(3, 6, 9, "final", 0))
        assert rc == 0
        out = capsys.readouterr().out
        assert "needs Python >= 3.11" in out
        assert "3.6.9" in out
        assert "uv" in out and "python-build-standalone" in out

    def test_declined_consent_exits_cleanly(self, monkeypatch):
        monkeypatch.setattr(pyshim, "venv_python_bin", lambda: "/nonexistent/tool")
        monkeypatch.setattr(pyshim, "_consent", lambda *a, **k: False)
        with pytest.raises(SystemExit) as ei:
            pyshim.run([], assume_yes=False, dry_run=False,
                       version_info=(3, 6, 9, "final", 0))
        assert ei.value.code == 2


class TestSafeExtract:
    """R1-F6: validated extraction — no traversal, no links, no devices."""

    def _tar_bytes(self, infos):
        import io
        import tarfile
        buf = io.BytesIO()
        with tarfile.open(fileobj=buf, mode="w:gz") as tf:
            for info, data in infos:
                tf.addfile(info, io.BytesIO(data) if data is not None else None)
        buf.seek(0)
        return tarfile.open(fileobj=buf, mode="r:gz")

    def test_rejects_path_traversal(self, tmp_path):
        import tarfile
        with self._tar_bytes([(tarfile.TarInfo("../../evil.sh"), b"x")]) as tf:
            with pytest.raises(IOError):
                pyshim._safe_extract(tf, str(tmp_path))

    def test_rejects_symlink_member(self, tmp_path):
        import tarfile
        info = tarfile.TarInfo("link")
        info.type = tarfile.SYMTYPE
        info.linkname = "/etc/passwd"
        with self._tar_bytes([(info, None)]) as tf:
            with pytest.raises(IOError):
                pyshim._safe_extract(tf, str(tmp_path))

    def test_extracts_normal_file(self, tmp_path):
        import tarfile
        info = tarfile.TarInfo("bin/python3")
        info.size = 2
        with self._tar_bytes([(info, b"py")]) as tf:
            pyshim._safe_extract(tf, str(tmp_path))
        assert (tmp_path / "bin" / "python3").read_bytes() == b"py"


class TestConsent:
    def test_non_tty_never_asks(self, monkeypatch):
        monkeypatch.setattr(sys, "stdin", io.StringIO(""))  # isatty() is False
        assert pyshim._consent("proceed?", assume_yes=False) is False

    def test_assume_yes_short_circuits(self):
        assert pyshim._consent("proceed?", assume_yes=True) is True


def _no_reexec(*_a, **_k):  # test belt: re-exec must never happen in tests
    raise AssertionError("_reexec must not run during tests")


def _no_create(*_a, **_k):
    raise AssertionError("_create_venv must not run during dry-run tests")


def _no_install(*_a, **_k):
    return None

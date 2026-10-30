"""Tests for build.cross_deps — the cross-compilation prerequisite gate.

*Why this exists:* `check-cross-deps` is a decision gate (it installs targets and
exits non-zero when it cannot) and it had no test. Every branch below is a way
the gate can quietly stop gating: rustup missing, the target already installed,
and the install failing after the target was absent.
"""

from __future__ import annotations
from celestia_devtools.build import cross_deps

class _result:
    def __init__(self, returncode: int, stdout: str = "") -> None:
        self.returncode = returncode
        self.stdout = stdout

def _fake_run(monkeypatch, results):
    calls = []

    def run(args, **kwargs):
        calls.append(list(args))
        assert results, "the tool under test made more subprocess calls than this test scripted"
        return results.pop(0)

    monkeypatch.setattr(cross_deps.subprocess, "run", run)
    return calls

def test_missing_rustup_fails_the_gate(monkeypatch):
    _fake_run(monkeypatch, [_result(1)])
    assert cross_deps.check_rustup_target("x86_64-unknown-linux-musl") is False

def test_already_installed_target_passes_without_installing(monkeypatch):
    calls = _fake_run(monkeypatch, [_result(0, "wasm32-unknown-unknown\n")])
    assert cross_deps.check_rustup_target("wasm32-unknown-unknown") is True
    assert len(calls) == 1  # no `rustup target add`

def test_failed_install_fails_the_gate(monkeypatch):
    calls = _fake_run(monkeypatch, [_result(0, ""), _result(1)])
    assert cross_deps.check_rustup_target("aarch64-unknown-linux-musl") is False
    assert calls[1][:3] == ["rustup", "target", "add"]

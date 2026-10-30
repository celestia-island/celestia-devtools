"""Tests for env.preflight — the dev-tool prerequisite probe.

*Why this exists:* `preflight` returns an exit code that scripts branch on, and
the "unknown requirement" guard is the only thing standing between a typo and a
silent pass. Both are pinned here.
"""

from __future__ import annotations
import pytest
from celestia_devtools.env import preflight

def test_unknown_requirement_is_a_failure_not_a_pass(monkeypatch):
    monkeypatch.setattr(preflight, "CHECKERS", {})
    missing = preflight.check("definitely-not-a-tool", quiet = True)
    assert len(missing) == 1
    assert missing[0].ok is False
    assert "unknown requirement" in missing[0].detail

def test_missing_tool_is_reported(monkeypatch):
    monkeypatch.setattr(preflight, "CHECKERS", {"node": lambda: preflight.probe_record("node", False)})
    assert [record.name for record in preflight.check("node", quiet = True)] == ["node"]

def test_present_tool_passes(monkeypatch):
    monkeypatch.setattr(preflight, "CHECKERS", {"node": lambda: preflight.probe_record("node", True, version = "v20.0.0")})
    assert preflight.check("node", quiet = True) == []

def test_require_exits_two_when_something_is_missing(monkeypatch):
    monkeypatch.setattr(preflight, "CHECKERS", {"node": lambda: preflight.probe_record("node", False)})

    with pytest.raises(SystemExit) as excinfo:
        preflight.require("node")

    assert excinfo.value.code == 2

def test_main_returns_one_when_a_requirement_is_missing(monkeypatch):
    monkeypatch.setattr(preflight, "CHECKERS", {"node": lambda: preflight.probe_record("node", False)})
    monkeypatch.setattr("sys.argv", ["preflight", "--quiet", "node"])
    assert preflight.main() == 1

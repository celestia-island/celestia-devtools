"""Tests for the pglite facility (env/pglite.py) — named instances.

Three layers, mirroring the module's seams:

* name/sidecar layout — validation of names, per-name sidecar paths,
  iteration over recorded instances, default keeping its historical
  single-sidecar location;
* start semantics — reuse of a live same-name instance, independent
  slots for different names (a live ``arona`` does not satisfy a
  ``scepter`` start);
* stop/status — per-name teardown clears only its own sidecar,
  ``--all`` sweeps every recorded instance, JSON output shape.

Everything runs against a temp data dir via the ``_data_dir`` seam; the
Node process is faked (a Popen stub that prints a READY line), so no
node/npm is needed here — the real-process behaviour is covered by
manual use and the e2e suites.
"""

from __future__ import annotations

import io
import json
from pathlib import Path

import pytest

from celestia_devtools.env import pglite


class _FakeProc:
    """Stands in for the detached Node server process."""

    def __init__(self, pid: int, url: str, exit_after_read: bool = False):
        self.pid = pid
        self.returncode = None
        self.stdout = io.BytesIO((f"READY:{url}\n").encode())
        self._exit_after_read = exit_after_read

    def poll(self):
        return self.returncode

    def kill(self):
        self.returncode = -9


def _patch_env(monkeypatch, tmp_path: Path, procs: list[_FakeProc]):
    """Point the module at a temp data dir and stub process spawning."""
    monkeypatch.setattr(pglite, "_data_dir", lambda: tmp_path)
    monkeypatch.setattr(pglite, "_require_tools", lambda *a: None)
    monkeypatch.setattr(pglite, "_ensure_node_packages", lambda: True)
    monkeypatch.setattr(pglite, "_write_launcher", lambda: tmp_path / "l.mjs")
    monkeypatch.setattr(pglite, "_free_port", lambda: 15432)
    real_popen = pglite.subprocess.Popen

    def fake_popen(*args, **kwargs):
        proc = procs.pop(0) if procs else _FakeProc(
            4242, "postgresql://postgres:postgres@127.0.0.1:15432/postgres")
        # keep the log-file handle the caller opened so it isn't leaked
        proc._stderr = kwargs.get("stderr")
        return proc

    monkeypatch.setattr(pglite.subprocess, "Popen", fake_popen)
    return real_popen


@pytest.fixture
def data_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(pglite, "_data_dir", lambda: tmp_path)
    return tmp_path


# ── name & sidecar layout ────────────────────────────────────────────


def test_sidecar_layout_default_keeps_historical_path(data_dir):
    assert pglite._sidecar_path() == data_dir / "pglite-instance.json"
    assert pglite._sidecar_path("default") == data_dir / "pglite-instance.json"


def test_sidecar_layout_named_gets_own_file(data_dir):
    assert (pglite._sidecar_path("arona")
            == data_dir / "pglite-instance-arona.json")


@pytest.mark.parametrize("bad", ["", "Arona", "a b", "a/b", "-x", "x" * 33, "a_b"])
def test_name_validation_rejects_unsafe(bad):
    with pytest.raises(SystemExit):
        pglite._validate_name(bad)


@pytest.mark.parametrize("good", ["a", "arona", "neo-scepter", "n0", "a" * 32])
def test_name_validation_accepts_safe(good):
    assert pglite._validate_name(good) == good


def test_iter_sidecar_names_round_trip(data_dir):
    pglite._write_sidecar({"pid": 1}, "arona")
    pglite._write_sidecar({"pid": 2}, "scepter")
    pglite._write_sidecar({"pid": 3})  # default
    assert pglite._iter_sidecar_names() == ["arona", "default", "scepter"]


# ── start semantics ──────────────────────────────────────────────────


def test_start_reuses_live_instance_of_same_name(tmp_path, monkeypatch, capsys):
    _patch_env(monkeypatch, tmp_path, procs=[])
    pglite._write_sidecar(
        {"pid": 999, "url": "postgresql://x@127.0.0.1:1/postgres"}, "arona")
    monkeypatch.setattr(pglite, "_proc_alive", lambda pid: True)
    # Reuse must not even require node (require is stubbed, but Popen would
    # pop from an empty list → any spawn attempt fails the test).
    assert pglite.start(name="arona") == 0
    assert "postgresql://x@127.0.0.1:1/postgres" in capsys.readouterr().out


def test_start_different_name_spawns_its_own(tmp_path, monkeypatch, capsys):
    fake = _FakeProc(4242, "postgresql://postgres:postgres@127.0.0.1:15432/postgres")
    _patch_env(monkeypatch, tmp_path, procs=[fake])
    pglite._write_sidecar({"pid": 999, "url": "postgresql://x@127.0.0.1:1/postgres"},
                          "arona")
    monkeypatch.setattr(pglite, "_proc_alive", lambda pid: True)
    assert pglite.start(name="scepter") == 0
    out = capsys.readouterr().out
    assert "127.0.0.1:15432" in out
    # both sidecars now exist independently
    assert pglite._read_sidecar("arona")["pid"] == 999
    assert pglite._read_sidecar("scepter")["pid"] == 4242
    assert pglite._read_sidecar("scepter")["name"] == "scepter"


def test_start_records_url_and_pid(tmp_path, monkeypatch, capsys):
    fake = _FakeProc(777, "postgresql://postgres:postgres@127.0.0.1:15440/postgres")
    _patch_env(monkeypatch, tmp_path, procs=[fake])
    monkeypatch.setattr(pglite, "_proc_alive", lambda pid: False)
    assert pglite.start(name="neo") == 0
    sc = pglite._read_sidecar("neo")
    assert sc["pid"] == 777
    assert sc["url"].endswith(":15440/postgres")
    assert sc["data_dir"] == "memory://"


# ── stop & status ────────────────────────────────────────────────────


def test_stop_clears_only_its_own_sidecar(data_dir, monkeypatch):
    killed = []
    monkeypatch.setattr(pglite.os, "kill", lambda pid, sig: killed.append(pid))
    monkeypatch.setattr(pglite, "_proc_alive",
                        lambda pid: pid not in killed)
    pglite._write_sidecar({"pid": 11}, "arona")
    pglite._write_sidecar({"pid": 22}, "scepter")
    assert pglite.stop(name="arona") == 0
    assert killed == [11]
    assert pglite._read_sidecar("arona") == {}
    assert pglite._read_sidecar("scepter") == {"pid": 22}


def test_stop_all_sweeps_every_instance(data_dir, monkeypatch, capsys):
    killed = []
    monkeypatch.setattr(pglite.os, "kill", lambda pid, sig: killed.append(pid))
    monkeypatch.setattr(pglite, "_proc_alive",
                        lambda pid: pid not in killed)
    pglite._write_sidecar({"pid": 11}, "arona")
    pglite._write_sidecar({"pid": 22}, "scepter")
    pglite._write_sidecar({"pid": 33})  # default
    assert pglite.stop(all_=True) == 0
    assert sorted(killed) == [11, 22, 33]
    assert pglite._iter_sidecar_names() == []


def test_status_all_reports_live_instances(data_dir, monkeypatch, capsys):
    monkeypatch.setattr(pglite, "_proc_alive", lambda pid: pid == 22)
    pglite._write_sidecar({"pid": 11, "url": "u1"}, "arona")
    pglite._write_sidecar({"pid": 22, "url": "u2"}, "scepter")
    assert pglite.status(all_=True) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["running"] is True
    assert [i["name"] for i in payload["instances"]] == ["scepter"]
    assert payload["instances"][0]["url"] == "u2"


def test_status_single_instance_exit_code(data_dir, monkeypatch, capsys):
    monkeypatch.setattr(pglite, "_proc_alive", lambda pid: False)
    pglite._write_sidecar({"pid": 11, "url": "u1"}, "arona")
    assert pglite.status(name="arona") == 1
    assert json.loads(capsys.readouterr().out) == {"running": False}

"""Tests for the per-app port allocator (env/ports.py).

The allocator must guarantee: two apps never share a port for the same
kind (the WoWSP × shittim-chest cross-wiring), the same app gets the
SAME port across calls (URLs and CDP targets feel permanent), dead-owner
claims are garbage-collected, and the env contract carries the derived
WebView2 CDP line.
"""

from __future__ import annotations
import json
import os
import socket
import pytest
from celestia_devtools.env import ports

@pytest.fixture()
def registry(tmp_path, monkeypatch):
    path = tmp_path / "ports.json"
    monkeypatch.setenv("CELESTIA_PORTS_REGISTRY", str(path))
    yield path

class test_acquire:
    def test_two_apps_never_share_a_kind_port(self, registry):
        a = ports.acquire("shittim-chest", "vite")
        b = ports.acquire("wowsp", "vite")
        assert a != b

    def test_same_app_same_kind_is_stable(self, registry):
        first = ports.acquire("shittim-chest", "vite")
        assert ports.acquire("shittim-chest", "vite") == first

    def test_preferred_port_is_deterministic(self):
        assert ports._preferred("shittim-chest", "vite") == ports._preferred("shittim-chest", "vite")
        assert ports._preferred("shittim-chest", "vite") != ports._preferred("wowsp", "vite")

    def test_kinds_are_independent(self, registry):
        vite = ports.acquire("shittim-chest", "vite")
        cdp = ports.acquire("shittim-chest", "cdp")
        assert vite != cdp

    def test_live_listener_forces_next_port(self, registry, monkeypatch):
        # Park a real listener on the app's preferred port; the allocator
        # must walk forward instead of handing out a port it cannot own.
        with socket.socket() as s:
            s.bind(("127.0.0.1", 0))
            s.listen(1)
            blocked = s.getsockname()[1]
            monkeypatch.setattr(ports, "_preferred", lambda app, kind: blocked)
            got = ports.acquire("fresh-app", "vite")
            assert got != blocked

    def test_dead_owner_claim_is_reclaimed(self, registry):
        # Claim with a pid that is definitely not alive, then acquire for
        # the SAME key: the GC drops the stale claim and re-allocates
        # (deterministically the preferred port).
        stale_pid = 4_000_000  # far beyond any real pid
        registry.write_text(json.dumps({"app:vite": {"port": 5999, "pid": stale_pid, "ts": 0}}), encoding = "utf-8")
        got = ports.acquire("app", "vite")
        assert got == ports._preferred("app", "vite")
        data = json.loads(registry.read_text(encoding = "utf-8"))
        assert data["app:vite"]["port"] == got
        assert data["app:vite"]["pid"] != stale_pid

class test_release_and_list:
    def test_release_one_kind_keeps_the_other(self, registry):
        ports.acquire("app", "vite")
        ports.acquire("app", "cdp")
        ports.release("app", "vite")
        entries = ports.list_entries()
        assert "app:vite" not in entries
        assert "app:cdp" in entries

    def test_release_all(self, registry):
        ports.acquire("app", "vite")
        ports.acquire("app", "cdp")
        ports.release("app", None)
        assert ports.list_entries() == {}

class test_env_contract:
    def test_env_carries_both_kinds_and_webview2_line(self, registry):
        exports = ports.env_exports("shittim-chest")
        assert exports["CELESTIA_VITE_PORT"].isdigit()
        assert exports["CELESTIA_CDP_PORT"].isdigit()
        assert exports["CELESTIA_VITE_PORT"] != exports["CELESTIA_CDP_PORT"]
        assert exports["WEBVIEW2_ADDITIONAL_BROWSER_ARGUMENTS"] == f"--remote-debugging-port={exports['CELESTIA_CDP_PORT']}"

    def test_env_is_stable_per_app(self, registry):
        assert ports.env_exports("app") == ports.env_exports("app")

class test_cli:
    def test_acquire_prints_bare_port(self, registry, capsys):
        assert ports.main(["acquire", "--app", "app", "--kind", "vite"]) == 0
        out = capsys.readouterr().out.strip()
        assert out.isdigit()

    def test_env_export_lines(self, registry, capsys):
        assert ports.main(["env", "--app", "app"]) == 0
        out = capsys.readouterr().out
        assert "export CELESTIA_VITE_PORT='" in out
        assert "export WEBVIEW2_ADDITIONAL_BROWSER_ARGUMENTS='" in out

    def test_list_json(self, registry, capsys):
        ports.main(["acquire", "--app", "app", "--kind", "vite"])
        capsys.readouterr()  # drop the bare-port line of the acquire
        assert ports.main(["list", "--json"]) == 0
        data = json.loads(capsys.readouterr().out)
        assert "app:vite" in data

class test_claim:
    def test_claim_records_the_launched_app_pid(self, registry):
        # claim() exists for launchers (app-drive) whose own pid exits at
        # once: the port must stay claimed while the LAUNCHED app lives.
        ports.claim("app", "cdp", 9333, os.getpid())
        entries = ports.list_entries()
        assert entries["app:cdp"]["port"] == 9333
        assert entries["app:cdp"]["pid"] == os.getpid()

    def test_claim_with_a_dead_pid_is_garbage_collected(self, registry):
        ports.claim("app", "cdp", 9333, 4_000_000)
        assert ports.list_entries() == {}

    def test_claim_replaces_an_existing_acquire_claim(self, registry):
        ports.acquire("app", "cdp")
        ports.claim("app", "cdp", 9444, os.getpid())
        entries = ports.list_entries()
        assert entries["app:cdp"]["port"] == 9444

    def test_claim_rejects_an_unknown_kind(self, registry):
        with pytest.raises(SystemExit):
            ports.claim("app", "nonsense", 9444, os.getpid())

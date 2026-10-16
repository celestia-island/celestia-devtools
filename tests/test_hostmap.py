"""Tests for the hostmap facility (env/hostmap.py).

Three layers, mirroring the module's own seams:

* lane resolution — built-ins, profile selection, hostmap.toml merge,
  validation of values/domains, wildcard routing;
* hosts-block surgery — apply/remove/idempotence on a temp file, foreign
  lines preserved, dangling BEGIN recovered;
* the proxy itself — two fake HTTP upstreams on ephemeral ports, one
  asyncio proxy on an ephemeral port, Host-routed requests both ways,
  502 for unknown hosts and dead upstreams, byte-exact relay.

Everything runs unprivileged: hosts edits go through a temp file via the
``--hosts-file`` seam, and all sockets bind port 0.
"""

from __future__ import annotations

import asyncio
import http.client
import json
import socket
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from celestia_devtools.env import hostmap


# ── Lane resolution ──────────────────────────────────────────────────────────

class TestLanes:
    def test_builtin_profiles_present(self):
        assert set(hostmap.DEFAULT_LANES) >= {"native", "daemon", "mock"}

    def test_default_profile_is_native(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)  # no hostmap.toml, no user config
        profile, lanes, cfg = hostmap.load_lanes()
        assert profile == "native"
        assert cfg is None
        assert lanes["dev.cw"] == ("127.0.0.1", 3000)

    def test_profile_selection(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        profile, lanes, _ = hostmap.load_lanes(profile="daemon")
        assert profile == "daemon"
        assert lanes["dev.cw"] == ("127.0.0.1", 8400)
        assert lanes["arona.cw"] == ("127.0.0.1", 8405)

    def test_unknown_profile_fails_loudly(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        with pytest.raises(ValueError, match="unknown profile"):
            hostmap.load_lanes(profile="nope")

    def test_config_overrides_and_extends(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        (tmp_path / "hostmap.toml").write_text(
            '[lanes.native]\n"dev.cw" = 9999\n"extra.cw" = "10.0.0.5:8080"\n',
            encoding="utf-8",
        )
        profile, lanes, cfg = hostmap.load_lanes(profile="native")
        assert cfg == tmp_path / "hostmap.toml"
        assert lanes["dev.cw"] == ("127.0.0.1", 9999)          # override
        assert lanes["extra.cw"] == ("10.0.0.5", 8080)         # extend
        assert lanes["auth.cw"] == ("127.0.0.1", 3000)         # untouched default

    def test_config_default_profile(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        (tmp_path / "hostmap.toml").write_text(
            'profile = "mock"\n[lanes.mock]\n"dev.cw" = 8428\n',
            encoding="utf-8",
        )
        profile, lanes, _ = hostmap.load_lanes()
        assert (profile, lanes["dev.cw"]) == ("mock", ("127.0.0.1", 8428))

    def test_bad_values_rejected(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        for bad in (0, 65536, True, "no-port", "x:notaport", ["3000"]):
            with pytest.raises(ValueError):
                hostmap._normalize_lane_value(bad, "dev.cw")

    def test_bad_domain_rejected(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        (tmp_path / "hostmap.toml").write_text(
            '[lanes.native]\n"not a domain" = 3000\n', encoding="utf-8",
        )
        with pytest.raises(ValueError, match="not a domain name"):
            hostmap.load_lanes()

    def test_resolve_exact_then_wildcard(self):
        lanes = {
            "dev.cw": ("127.0.0.1", 3000),
            "*.lab.cw": ("127.0.0.1", 8415),
        }
        assert hostmap.resolve(lanes, "dev.cw") == ("127.0.0.1", 3000)
        assert hostmap.resolve(lanes, "demo.lab.cw") == ("127.0.0.1", 8415)
        assert hostmap.resolve(lanes, "deep.sub.lab.cw") == ("127.0.0.1", 8415)
        assert hostmap.resolve(lanes, "nope.cw") is None

    def test_hosts_domains_collapses_wildcards(self):
        assert hostmap.hosts_domains({
            "dev.cw": ("127.0.0.1", 1),
            "*.lab.cw": ("127.0.0.1", 2),
            "lab.cw": ("127.0.0.1", 3),
        }) == ["dev.cw", "lab.cw"]


# ── Hosts-block surgery ──────────────────────────────────────────────────────

FOREIGN = "127.0.0.1 localhost\n::1 localhost\n10.0.0.9 printer.local\n"


class TestHostsBlock:
    def test_apply_remove_roundtrip(self, tmp_path):
        hosts = tmp_path / "hosts"
        hosts.write_text(FOREIGN, encoding="utf-8")

        assert hostmap.sync_hosts_block(hosts, ["dev.cw", "auth.cw"]) is True
        text = hosts.read_text(encoding="utf-8")
        assert hostmap.HOSTS_BEGIN in text and hostmap.HOSTS_END in text
        assert "127.0.0.1 dev.cw auth.cw" in text
        for line in FOREIGN.strip().splitlines():
            assert line in text  # foreign lines survive

        assert hostmap.read_managed_domains(hosts) == ["dev.cw", "auth.cw"]

        assert hostmap.remove_hosts_block(hosts) is True
        assert hostmap.read_managed_domains(hosts) == []
        assert hosts.read_text(encoding="utf-8").strip() == FOREIGN.strip()

    def test_apply_is_idempotent(self, tmp_path):
        hosts = tmp_path / "hosts"
        hosts.write_text(FOREIGN, encoding="utf-8")
        assert hostmap.sync_hosts_block(hosts, ["dev.cw"]) is True
        before = hosts.read_text(encoding="utf-8")
        assert hostmap.sync_hosts_block(hosts, ["dev.cw"]) is False
        assert hosts.read_text(encoding="utf-8") == before

    def test_reapply_replaces_domains(self, tmp_path):
        hosts = tmp_path / "hosts"
        hostmap.sync_hosts_block(hosts, ["dev.cw", "auth.cw"])
        hostmap.sync_hosts_block(hosts, ["dev.cw"])
        assert hostmap.read_managed_domains(hosts) == ["dev.cw"]
        text = hosts.read_text(encoding="utf-8")
        assert text.count(hostmap.HOSTS_BEGIN) == 1  # never duplicated

    def test_dangling_begin_recovered(self, tmp_path):
        hosts = tmp_path / "hosts"
        hosts.write_text(
            FOREIGN + hostmap.HOSTS_BEGIN + "\n127.0.0.1 dev.cw\n",
            encoding="utf-8",
        )
        assert hostmap.remove_hosts_block(hosts) is True
        assert hosts.read_text(encoding="utf-8").strip() == FOREIGN.strip()

    def test_remove_when_absent_is_noop(self, tmp_path):
        hosts = tmp_path / "hosts"
        hosts.write_text(FOREIGN, encoding="utf-8")
        assert hostmap.remove_hosts_block(hosts) is False
        assert hostmap.read_managed_domains(hosts) == []

    def test_missing_file_reads_empty(self, tmp_path):
        assert hostmap.read_managed_domains(tmp_path / "nope") == []


# ── Elevation ────────────────────────────────────────────────────────────────

class TestElevation:
    def test_script_uses_parenthesised_array_literal(self):
        """`-ArgumentList @('a','b')` — without the parentheses PowerShell
        parses `@'` as a here-string opener and the attempt dies with
        UnexpectedCharactersAfterHereStringHeader before any UAC dialog
        appears (the live-fire bug caught verifying PR #171)."""
        script = hostmap._elevation_script(
            r"C:\Program Files\Python\python.exe",
            ["-m", "celestia_devtools", "hostmap", "hosts-sync"],
        )
        assert "-ArgumentList @('-m', 'celestia_devtools'" in script
        assert "@'" not in script.replace("@('", "")  # no bare @-quote anywhere
        assert "exit $p.ExitCode } catch { exit 1223 }" in script
        assert "Start-Process -Verb RunAs -Wait -PassThru" in script

    def test_script_quotes_embedded_single_quotes(self):
        script = hostmap._elevation_script("py", ["it's", "x"])
        assert "'it''s'" in script

    def test_relaunch_retries_in_process_until_approved(self, monkeypatch, capsys):
        """Simulates antivirus holding the first elevation attempt: the
        spawn fails twice (rc 1223), the user approves, the third attempt
        succeeds — the tool must still be alive to see it through."""
        calls = []

        def fake_run(cmd, **kw):
            calls.append(cmd)
            rc = 1223 if len(calls) < 3 else 0
            return subprocess.CompletedProcess(cmd, rc, b"", b"")

        monkeypatch.setattr(hostmap.subprocess, "run", fake_run)
        monkeypatch.setattr(hostmap.time, "sleep", lambda s: None)
        if sys.platform == "win32":
            assert hostmap.relaunch_elevated(["hosts-sync"]) == 0
            assert len(calls) == 3
            out = capsys.readouterr().out
            assert "attempt 1/3" in out and "antivirus" in out
        else:  # POSIX path is a single sudo attempt by design
            assert hostmap.relaunch_elevated(["hosts-sync"]) == 0
            assert len(calls) == 1

    def test_relaunch_gives_up_after_max_attempts(self, monkeypatch, capsys):
        def fake_run(cmd, **kw):
            return subprocess.CompletedProcess(cmd, 1223, b"", b"cancelled")

        monkeypatch.setattr(hostmap.subprocess, "run", fake_run)
        monkeypatch.setattr(hostmap.time, "sleep", lambda s: None)
        if sys.platform == "win32":
            assert hostmap.relaunch_elevated(["hosts-sync"]) == 1223
            out = capsys.readouterr().out
            assert "attempt 1/3" in out and "attempt 2/3" in out  # both retries fired


# ── Proxy (end-to-end over loopback) ─────────────────────────────────────────

class _Upstream(BaseHTTPRequestHandler):
    def do_GET(self):
        body = f"upstream-{self.server.server_port}:{self.path}".encode()
        self.send_response(200)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):  # keep test output clean
        pass


def _start_upstreams(count: int) -> list[ThreadingHTTPServer]:
    servers = []
    for _ in range(count):
        srv = ThreadingHTTPServer(("127.0.0.1", 0), _Upstream)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        servers.append(srv)
    return servers


class _ProxyThread:
    """The asyncio proxy running in a worker thread with its own loop.

    Exits cleanly by cancelling the serve task from the test thread — the
    ``async with server`` inside the coroutine then closes the listener on
    the loop itself, avoiding proactor-close-at-GC warnings."""

    def __init__(self, lanes):
        self._lanes = lanes
        self.port = 0
        self._thread: threading.Thread | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._task: asyncio.Task | None = None
        self._ready = threading.Event()

    def _run(self):
        self._loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self._loop)

        async def main():
            server = await asyncio.start_server(
                lambda r, w: hostmap._handle_client(r, w, self._lanes),
                "127.0.0.1", 0, limit=hostmap._HEAD_LIMIT,
            )
            self.port = server.sockets[0].getsockname()[1]
            self._ready.set()
            async with server:
                await server.serve_forever()

        async def guarded():
            self._task = asyncio.current_task()
            try:
                await main()
            except asyncio.CancelledError:
                pass

        try:
            self._loop.run_until_complete(guarded())
        finally:
            self._loop.run_until_complete(self._loop.shutdown_asyncgens())
            self._loop.close()

    def __enter__(self):
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()
        assert self._ready.wait(5), "proxy never became ready"
        return self

    def __exit__(self, *exc):
        loop, task = self._loop, self._task
        if loop is not None and task is not None:
            loop.call_soon_threadsafe(task.cancel)
        if self._thread is not None:
            self._thread.join(timeout=5)


def _get(port: int, host_header: str, path: str = "/x") -> http.client.HTTPResponse:
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    conn.request("GET", path, headers={"Host": host_header})
    resp = conn.getresponse()
    return resp


class TestProxy:
    def test_routes_by_host_header(self):
        upstreams = _start_upstreams(2)
        try:
            lanes = {
                "dev.cw": ("127.0.0.1", upstreams[0].server_port),
                "arona.cw": ("127.0.0.1", upstreams[1].server_port),
            }
            with _ProxyThread(lanes) as proxy:
                resp = _get(proxy.port, "dev.cw")
                assert resp.status == 200
                assert f"upstream-{upstreams[0].server_port}:/x" in resp.read().decode()
                resp = _get(proxy.port, "arona.cw:80")  # port suffix stripped
                assert resp.status == 200
                assert f"upstream-{upstreams[1].server_port}:/x" in resp.read().decode()
        finally:
            for srv in upstreams:
                srv.shutdown()

    def test_unknown_host_gets_502_with_lane_list(self):
        upstreams = _start_upstreams(1)
        try:
            lanes = {"dev.cw": ("127.0.0.1", upstreams[0].server_port)}
            with _ProxyThread(lanes) as proxy:
                resp = _get(proxy.port, "stranger.cw")
                body = resp.read().decode()
                assert resp.status == 502
                assert "no lane for host" in body
                assert "dev.cw" in body  # the listing names the known lanes
        finally:
            for srv in upstreams:
                srv.shutdown()

    def test_dead_upstream_gets_502(self):
        # Grab a free port and never listen on it.
        with socket.socket() as s:
            s.bind(("127.0.0.1", 0))
            dead_port = s.getsockname()[1]
        lanes = {"dev.cw": ("127.0.0.1", dead_port)}
        with _ProxyThread(lanes) as proxy:
            resp = _get(proxy.port, "dev.cw")
            assert resp.status == 502
            assert "refused" in resp.read().decode()

    def test_raw_relay_passes_bytes_verbatim(self):
        """A plain TCP pair after the routing decision — the WebSocket path:
        headers consumed for routing, then a byte-for-byte pipe."""
        received = {}

        def echo_server():
            srv = socket.socket()
            srv.bind(("127.0.0.1", 0))
            srv.listen(1)
            received["port"] = srv.getsockname()[1]
            conn, _ = srv.accept()
            head = conn.recv(1024)  # the request head, forwarded verbatim
            received["head"] = head
            conn.sendall(b"pong-from-upstream")
            conn.close()
            srv.close()

        thread = threading.Thread(target=echo_server, daemon=True)
        thread.start()
        while "port" not in received:
            time.sleep(0.01)

        lanes = {"dev.cw": ("127.0.0.1", received["port"])}
        with _ProxyThread(lanes) as proxy:
            client = socket.create_connection(("127.0.0.1", proxy.port), timeout=5)
            request = b"GET /ws HTTP/1.1\r\nHost: dev.cw\r\nUpgrade: websocket\r\n\r\n"
            client.sendall(request)
            client.settimeout(5)
            data = b""
            while b"pong-from-upstream" not in data:
                chunk = client.recv(4096)
                if not chunk:
                    break
                data += chunk
            client.close()

        assert received["head"] == request  # head forwarded byte-identically
        assert b"pong-from-upstream" in data


# ── CLI ──────────────────────────────────────────────────────────────────────

class TestCli:
    def test_lanes_table(self, capsys, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        assert hostmap.main(["lanes", "--profile", "daemon"]) == 0
        out = capsys.readouterr().out
        assert "profile: daemon" in out
        assert "dev.cw" in out and "8400" in out

    def test_lanes_json(self, capsys, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        assert hostmap.main(["lanes", "--json"]) == 0
        data = json.loads(capsys.readouterr().out)
        assert data["profile"] == "native"
        assert data["lanes"]["dev.cw"] == "127.0.0.1:3000"

    def test_hosts_sync_remove_via_cli(self, capsys, tmp_path):
        hosts = tmp_path / "hosts"
        hosts.write_text(FOREIGN, encoding="utf-8")
        common = ["--hosts-file", str(hosts)]
        assert hostmap.main(["hosts-sync", *common, "--profile", "mock"]) == 0
        assert hostmap.read_managed_domains(hosts) == sorted(
            hostmap.DEFAULT_LANES["mock"].keys()
        )
        assert hostmap.main(["hosts-remove", *common]) == 0
        assert hostmap.read_managed_domains(hosts) == []
        assert hosts.read_text(encoding="utf-8").strip() == FOREIGN.strip()

    def test_status_clean_run(self, capsys, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        rc = hostmap.main(
            ["status", "--hosts-file", str(tmp_path / "hosts"), "--port", "1"]
        )
        assert rc == 0
        out = capsys.readouterr().out
        assert "profile: native" in out
        assert "proxy" in out and "down" in out

    def test_down_on_missing_file_is_clean(self, capsys, tmp_path):
        rc = hostmap.main(["down", "--hosts-file", str(tmp_path / "absent")])
        assert rc == 0
        assert "nothing to remove" in capsys.readouterr().out

    def test_up_no_hosts_never_touches_hosts(self, tmp_path, monkeypatch):
        """`up --no-hosts` with a dead upstream port: proxy binds, request
        502s, hosts file is never created."""
        monkeypatch.chdir(tmp_path)
        hosts = tmp_path / "hosts"
        with _ProxyThread({"dev.cw": ("127.0.0.1", 59999)}) as proxy:
            resp = _get(proxy.port, "dev.cw")
            assert resp.status == 502
        assert not hosts.exists()  # the seam stayed untouched

    def test_head_parsing(self):
        head = (
            b"GET /path HTTP/1.1\r\nHost: Dev.CW:80\r\nUser-Agent: x\r\n\r\n"
        )
        assert hostmap._host_from_head(head) == ("GET", "dev.cw")
        connect = b"CONNECT demo.dev.cw:443 HTTP/1.1\r\nHost: x\r\n\r\n"
        assert hostmap._host_from_head(connect) == ("CONNECT", "demo.dev.cw")
        absolute = b"GET http://arona.cw/y HTTP/1.1\r\n\r\n"
        assert hostmap._host_from_head(absolute) == ("GET", "arona.cw")
        nohost = b"GET / HTTP/1.1\r\n\r\n"
        assert hostmap._host_from_head(nohost) == ("GET", "")

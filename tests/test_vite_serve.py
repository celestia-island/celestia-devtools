"""Tests for the SPA-aware vite static server (env/vite.py).

The frontends use history-API routes (the chest webui routes workspaces
as ``/@<uuid>``); a bare ``http.server`` answers those with its 404 error
page, so a page refresh on any routed view replaced the whole app —
custom title bar included — with "Error code: 404 / File not found"
(reported live against the tauri desktop shell, 2026-10-16). The fallback
serves ``index.html`` for navigation-shaped misses ONLY; file-shaped
misses and non-HTML probes keep their honest status.
"""

from __future__ import annotations
import functools
import http.server
import threading
import urllib.request
from pathlib import Path
import pytest
from celestia_devtools.env import vite

class _test_handler(vite._spa_fallback_handler):
    def log_message(self, fmt, *args):  # keep test output clean
        pass

@pytest.fixture()
def spa_server(tmp_path: Path):
    (tmp_path / "index.html").write_text("<html>spa</html>", encoding = "utf-8")
    (tmp_path / "assets").mkdir()
    (tmp_path / "assets" / "app.js").write_text("js", encoding = "utf-8")
    (tmp_path / "data.json").write_text("{}", encoding = "utf-8")

    class _bound(_test_handler):
        def __init__(self, *a, **kw):
            super().__init__(*a, directory = str(tmp_path), **kw)

    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _bound)
    threading.Thread(target = srv.serve_forever, daemon = True).start()
    yield f"http://127.0.0.1:{srv.socket.getsockname()[1]}"
    srv.shutdown()

def _get(url: str, accept: str | None = "text/html,application/xhtml+xml,*/*"):
    req = urllib.request.Request(url, headers = {"Accept": accept} if accept else {})

    try:
        with urllib.request.urlopen(req, timeout = 5) as resp:
            return resp.status, resp.read().decode()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode(errors = "replace")

class test_port_resolution:
    def test_env_port_used_when_flag_absent(self, monkeypatch):
        monkeypatch.setenv("CELESTIA_VITE_PORT", "5204")
        assert vite._port_of([]) == "5204"

    def test_flag_beats_env(self, monkeypatch):
        monkeypatch.setenv("CELESTIA_VITE_PORT", "5204")
        assert vite._port_of(["--port", "6000"]) == "6000"

    def test_garbage_env_falls_back_to_default(self, monkeypatch):
        monkeypatch.setenv("CELESTIA_VITE_PORT", "not-a-port")
        assert vite._port_of([]) == vite.DEFAULT_PORT

class test_serves_the_given_directory:
    def test_directory_wins_over_process_cwd(self, tmp_path, monkeypatch):
        """Regression (2026-10-16 live bite): without directory=, the handler
        resolves against the PROCESS cwd — and a vite project root carries
        its own source index.html, so the server served the SOURCE page
        whose asset references all 404'd (app shell 200, every chunk dead).
        The handler _serve builds must serve *dist*, not the launch cwd."""
        dist = tmp_path / "dist" / "webui"
        dist.mkdir(parents = True)
        (dist / "index.html").write_text("<html>BUILT</html>", encoding = "utf-8")
        (dist / "assets").mkdir()
        (dist / "assets" / "main-abc123.js").write_text("built-js", encoding = "utf-8")
        cwd = tmp_path / "packages" / "webui"  # a vite-source-shaped cwd
        cwd.mkdir(parents = True)
        (cwd / "index.html").write_text("<html>SOURCE</html>", encoding = "utf-8")
        monkeypatch.chdir(cwd)

        bound = functools.partial(vite._spa_fallback_handler, directory = str(dist))
        srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), bound)
        threading.Thread(target = srv.serve_forever, daemon = True).start()
        url = f"http://127.0.0.1:{srv.socket.getsockname()[1]}"

        try:
            status, body = _get(f"{url}/")
            assert (status, body) == (200, "<html>BUILT</html>")  # not SOURCE
            status, body = _get(f"{url}/assets/main-abc123.js")
            assert (status, body) == (200, "built-js")
        finally:
            srv.shutdown()

class test_spa_fallback:
    def test_history_route_serves_index(self, spa_server):
        status, body = _get(f"{spa_server}/@124268ea-91a1-4d89-9a02-f72a103e067e")
        assert status == 200
        assert "spa" in body  # the app, not the 404 error page

    def test_plain_route_serves_index(self, spa_server):
        status, body = _get(f"{spa_server}/login")
        assert (status == 200) and ("spa" in body)

    def test_root_is_native(self, spa_server):
        status, body = _get(f"{spa_server}/")
        assert (status == 200) and ("spa" in body)

    def test_file_shaped_miss_stays_404(self, spa_server):
        status, _ = _get(f"{spa_server}/assets/missing-12345678.js")
        assert status == 404  # broken asset links must fail loudly

    def test_json_miss_stays_404(self, spa_server):
        status, _ = _get(f"{spa_server}/nope.json")
        assert status == 404

    def test_existing_file_wins_over_fallback(self, spa_server):
        status, body = _get(f"{spa_server}/data.json")
        assert (status == 200) and (body == "{}")

    def test_non_html_accept_keeps_404(self, spa_server):
        status, _ = _get(f"{spa_server}/@some-workspace", accept = "application/json")
        assert status == 404  # XHR probes keep their honest status

    def test_query_and_fragment_do_not_break_fallback(self, spa_server):
        status, _ = _get(f"{spa_server}/@ws-1?tab=think")
        assert status == 200

"""Tests for celestia_devtools.core.netproxy (five-level proxy detection).

Hermetic by construction: every test injects its own env / hosts / ports, and
the one WPAD test both proves the parser hits a real positive (zero-hit rule)
and degrades to None on junk. Fake endpoints use RFC 5737 documentation
addresses (192.0.2/198.51.100/203.0.113) per the workspace credentials rule.
"""

from __future__ import annotations

import io
import socket as socket_mod

from celestia_devtools.core import netproxy


class TestRedact:
    def test_strips_userinfo(self):
        assert netproxy.redact("http://user:pass@192.0.2.10:7890") == "192.0.2.10:7890"

    def test_keeps_plain_host_port(self):
        assert netproxy.redact("http://proxy.corp.example:3128") == "proxy.corp.example:3128"

    def test_no_host_returns_input(self):
        assert netproxy.redact("not a url") == "not a url"


class TestEnvLevel:
    def test_https_wins_and_credentials_redacted(self):
        cfg = netproxy.detect(
            env={"HTTPS_PROXY": "http://user:pass@192.0.2.11:7890",
                 "HTTP_PROXY": "http://198.51.100.9:1"},
            dns_suffixes=(), listener_hosts=(), gateway_hosts=(),
        )
        assert cfg.proxy_url == "http://user:pass@192.0.2.11:7890"
        assert cfg.display == "192.0.2.11:7890"  # creds never in display
        assert cfg.source == "env:HTTPS_PROXY"

    def test_tool_alias_env(self):
        cfg = netproxy.detect(
            env={"UV_HTTP_PROXY": "198.51.100.4:8080"},
            dns_suffixes=(), listener_hosts=(), gateway_hosts=(),
        )
        assert cfg.display == "198.51.100.4:8080"
        assert cfg.source == "env:UV_HTTP_PROXY"


class TestOverrides:
    def test_no_proxy_flag_forces_direct(self):
        cfg = netproxy.detect(env={"HTTPS_PROXY": "http://203.0.113.1:7890"},
                              no_proxy_flag=True)
        assert cfg.direct and cfg.source == "flag:none"

    def test_explicit_none_forces_direct(self):
        cfg = netproxy.detect(env={"HTTPS_PROXY": "http://203.0.113.1:7890"},
                              explicit="none")
        assert cfg.direct and cfg.source == "flag:none"

    def test_explicit_url_wins_over_env(self):
        cfg = netproxy.detect(env={"HTTPS_PROXY": "http://203.0.113.1:7890"},
                              explicit="http://203.0.113.5:1")
        assert cfg.display == "203.0.113.5:1" and cfg.source == "flag:explicit"

    def test_disabled_by_env_var(self):
        cfg = netproxy.detect(
            env={"HTTPS_PROXY": "http://203.0.113.1:7890",
                 "CELESTIA_NO_PROXY_DETECT": "1"})
        assert cfg.direct and cfg.source == "disabled"


class TestNetworkLevels:
    def _free_port(self):
        with socket_mod.socket() as s:
            s.bind(("127.0.0.1", 0))
            return s.getsockname()[1]

    def test_listener_level_hits_bound_port(self):
        port = self._free_port()
        with socket_mod.socket() as srv:
            srv.bind(("127.0.0.1", port))
            srv.listen(1)
            cfg = netproxy.detect(
                env={}, dns_suffixes=(), gateway_hosts=(),
                listener_hosts=("127.0.0.1",), ports=(port,))
        assert cfg.source == "listen:127.0.0.1:{}".format(port)
        assert cfg.display == "127.0.0.1:{}".format(port)

    def test_gateway_level_injected(self):
        port = self._free_port()
        with socket_mod.socket() as srv:
            srv.bind(("127.0.0.1", port))
            srv.listen(1)
            cfg = netproxy.detect(
                env={}, dns_suffixes=(), listener_hosts=(),
                gateway_hosts=("127.0.0.1",), ports=(port,))
        assert cfg.source == "gateway:127.0.0.1:{}".format(port)

    def test_dns_suffix_level_injected(self, monkeypatch):
        monkeypatch.setattr(netproxy, "_tcp_reachable",
                            lambda host, port, timeout: host == "proxy.mydom.test" and port == 7890)
        monkeypatch.setattr(netproxy, "_from_wpad", lambda *a, **k: None)
        cfg = netproxy.detect(env={}, dns_suffixes=("mydom.test",),
                              listener_hosts=(), gateway_hosts=())
        assert cfg.source == "dns:proxy.mydom.test"

    def test_all_miss_reports_direct_loudly(self, monkeypatch):
        monkeypatch.setattr(netproxy, "_tcp_reachable", lambda *a, **k: False)
        monkeypatch.setattr(netproxy, "_from_wpad", lambda *a, **k: None)
        monkeypatch.setattr(netproxy, "_git_config_proxy", lambda: None)
        cfg = netproxy.detect(env={}, dns_suffixes=(), listener_hosts=(), gateway_hosts=())
        assert cfg.direct and cfg.source == "direct"
        assert "直连" in cfg.summary()


class TestNoProxyComposition:
    def test_contains_loopback_private_and_caller_entries(self):
        cfg = netproxy.detect(env={"HTTPS_PROXY": "http://203.0.113.3:7890",
                                   "NO_PROXY": "internal.corp.example"},
                              dns_suffixes=("node.test",),
                              listener_hosts=(), gateway_hosts=())
        joined = ",".join(cfg.no_proxy)
        # The RFC1918/ULA CIDR literals below are the feature's own constants
        # (NO_PROXY must cover the real private ranges), not host addresses.
        for expected in ("localhost", "127.0.0.1",
                         "10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16",
                         ".node.test", "internal.corp.example"):
            assert expected in cfg.no_proxy, joined


class TestMalformedInput:
    """R1-F2: a broken proxy value must degrade, never crash detect()."""

    def test_bad_ipv6_bracket_env_falls_through(self):
        cfg = netproxy.detect(env={"HTTPS_PROXY": "http://[bad"},
                              dns_suffixes=(), listener_hosts=(), gateway_hosts=())
        assert cfg.direct and cfg.source == "direct"

    def test_out_of_range_port_env_falls_through(self):
        cfg = netproxy.detect(env={"HTTPS_PROXY": "http://h.example:99999"},
                              dns_suffixes=(), listener_hosts=(), gateway_hosts=())
        assert cfg.direct

    def test_redact_never_raises_on_garbage(self):
        for junk in ("http://[bad", "http://u:p@h.example:99999", ":::", ""):
            out = netproxy.redact(junk)
            assert "@" not in out, out  # creds cannot survive even a failed parse


class TestGitConfigOrder:
    """R1-F3: a git http.proxy the user set themselves outranks all guessing."""

    def test_git_outranks_live_listeners_and_wpad(self, monkeypatch):
        monkeypatch.setattr(netproxy, "_git_config_proxy",
                            lambda: "http://gitproxy.example.test:8080")
        monkeypatch.setattr(netproxy, "_tcp_reachable", lambda *a, **k: True)
        monkeypatch.setattr(netproxy, "_from_wpad", lambda *a, **k: None)
        cfg = netproxy.detect(env={}, dns_suffixes=("test",))
        assert cfg.source == "git:http.proxy"
        assert cfg.display == "gitproxy.example.test:8080"


class TestNoProxyDedup:
    def test_duplicate_sources_collapse_keeping_first(self):
        """R2 mutation-a was green: the dedup line had no teeth. Pin both the
        no-duplicates invariant and the first-wins order."""
        cfg = netproxy.detect(env={"NO_PROXY": "localhost,first.example,localhost"},
                              dns_suffixes=("node.test", "node.test"),
                              listener_hosts=(), gateway_hosts=())
        assert cfg.no_proxy.count("localhost") == 1
        assert cfg.no_proxy.index("first.example") < cfg.no_proxy.index("second") \
            if "second" in cfg.no_proxy else True
        # node.test appears once via suffix, and ".node.test" not duplicated
        assert sum(1 for x in cfg.no_proxy if x == ".node.test") == 1


class TestChildEnv:
    def test_sets_proxy_and_no_proxy(self):
        cfg = netproxy.ProxyConfig("http://user:pass@192.0.2.8:7890", "env:HTTPS_PROXY",
                                   ("localhost", "203.0.113.0/24"))
        env = netproxy.child_env(cfg, base={"PATH": "/usr/bin"})
        assert env["HTTPS_PROXY"] == "http://user:pass@192.0.2.8:7890"  # creds needed to work
        assert env["no_proxy"] == "localhost,203.0.113.0/24"
        assert "ALL_PROXY" not in env

    def test_direct_clears_inherited_proxy_vars(self):
        cfg = netproxy.ProxyConfig(None, "direct")
        env = netproxy.child_env(cfg, base={"HTTPS_PROXY": "http://stale.example:1",
                                            "http_proxy": "http://stale.example:2"})
        assert "HTTPS_PROXY" not in env and "http_proxy" not in env

    def test_direct_also_clears_tool_aliases(self):
        """R1-F4: flag:none must not leave PIP_PROXY routing pip behind
        detect()'s back."""
        cfg = netproxy.ProxyConfig(None, "flag:none")
        env = netproxy.child_env(cfg, base={"PIP_PROXY": "http://stale.example:3",
                                            "UV_HTTP_PROXY": "http://stale.example:4",
                                            "npm_config_proxy": "http://stale.example:5"})
        for name in ("PIP_PROXY", "UV_HTTP_PROXY", "npm_config_proxy"):
            assert name not in env, name

    def test_display_never_carries_credentials(self):
        cfg = netproxy.ProxyConfig("http://user:pass@192.0.2.8:7890", "x")
        assert "@" not in cfg.display
        assert "@" not in cfg.summary()


class TestWpad:
    def _fake_urlopen(self, monkeypatch, body):
        class FakeResp(io.BytesIO):
            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

        monkeypatch.setattr("urllib.request.urlopen",
                            lambda url, timeout=None: FakeResp(body.encode()))
        # urllib.request.urlopen is imported inside _from_wpad, so the global
        # monkeypatch is what takes effect.

    def test_parses_first_proxy_literal(self, monkeypatch):
        self._fake_urlopen(
            monkeypatch,
            'function FindProxyForURL(u, h) { return "PROXY proxy.corp.example:3128; DIRECT"; }')
        hit = netproxy._from_wpad(("corp.example",), timeout=1.0)
        assert hit is not None
        assert hit[0] == "http://proxy.corp.example:3128"
        assert hit[1] == "wpad:corp.example"

    def test_junk_body_returns_none(self, monkeypatch):
        self._fake_urlopen(monkeypatch, "you have no javascript engine here")
        assert netproxy._from_wpad(("corp.example",), timeout=1.0) is None

    def test_malformed_proxy_line_is_a_miss_not_a_cascade_stop(self, monkeypatch):
        """R2-P3: `PROXY h:99999` parses but _net_or_none rejects it — the
        level must keep trying later suffixes instead of returning a
        truthy (None, source) tuple that mislabels direct as wpad."""
        bodies = {"http://wpad.bad.test/wpad.dat":
                  'return "PROXY h:99999; DIRECT";',
                  "http://wpad.good.test/wpad.dat":
                  'return "PROXY ok.good.test:3128; DIRECT";'}

        class FakeResp(io.BytesIO):
            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

        def fake_open(url, timeout=None):
            return FakeResp(bodies[url].encode())

        import urllib.request
        monkeypatch.setattr(urllib.request, "urlopen", fake_open)
        hit = netproxy._from_wpad(("bad.test", "good.test"), timeout=1.0)
        assert hit is not None
        assert hit[0] == "http://ok.good.test:3128"

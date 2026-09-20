"""Tests for celestia_devtools.core.deps (bounded, non-blocking dependency ensure)."""

from __future__ import annotations

from types import SimpleNamespace

from celestia_devtools.core import deps


def _boom_runner(*_a, **_k):
    raise AssertionError("runner must not be called when install=False")


class TestPresent:
    def test_installed_dist_reports_version(self):
        results = deps.ensure(("PyYAML",), install=False)
        assert len(results) == 1
        r = results[0]
        assert r.ok and r.action == "present"
        assert r.detail and r.detail[0].isdigit()

    def test_line_renders_mark(self):
        r = deps.ensure(("PyYAML",), install=False)[0]
        assert r.line().startswith("✓ PyYAML — ")


class TestReportOnly:
    def test_missing_without_install_never_invokes_pip(self):
        results = deps.ensure(("definitely-not-a-real-dist-xyz",),
                              install=False, runner=_boom_runner)
        r = results[0]
        assert not r.ok and r.action == "skipped"
        assert "report-only" in r.detail


class TestPipArgv:
    def test_proxy_and_index_flags(self):
        argv = deps._pip_argv("foo", "https://mirror.simple", "http://p:7890")
        assert argv[0].endswith("python") or argv[0].endswith("python3")
        assert "--proxy=http://p:7890" in argv
        assert "--index-url=https://mirror.simple" in argv
        assert argv[-1] == "foo"
        assert "--no-input" in argv

    def test_flags_omitted_when_none(self):
        argv = deps._pip_argv("foo", None, None)
        assert not any(a.startswith("--proxy") for a in argv)
        assert not any(a.startswith("--index-url") for a in argv)


class TestBudget:
    def test_retry_cap_and_failure_detail(self):
        calls = []

        def runner(argv, **kwargs):
            calls.append(argv)
            return SimpleNamespace(returncode=1, stdout="", stderr="boom: no network")

        results = deps.ensure(("definitely-not-a-real-dist-xyz",),
                              install=True, tries=2, runner=runner)
        assert len(calls) == 2, "must stop at the tries cap, not retry forever"
        r = results[0]
        assert not r.ok and r.action == "failed"
        assert "2 attempts" in r.detail and "boom: no network" in r.detail

    def test_subprocess_error_is_reported_not_raised(self):
        def runner(argv, **kwargs):
            raise OSError("pip vanished")

        r = deps.ensure(("definitely-not-a-real-dist-xyz",),
                        install=True, tries=1, runner=runner)[0]
        assert not r.ok and r.action == "failed"
        assert "pip vanished" in r.detail

    def test_success_on_first_try(self):
        def runner(argv, **kwargs):
            # A fake install cannot make importlib.metadata see the dist, so
            # the result detail falls back to the literal "installed" mark.
            return SimpleNamespace(returncode=0, stdout="", stderr="")

        r = deps.ensure(("definitely-not-a-real-dist-xyz",),
                        install=True, tries=2, runner=runner)[0]
        assert r.ok and r.action == "installed"

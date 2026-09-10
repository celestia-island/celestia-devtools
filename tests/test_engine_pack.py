"""Stdlib-unittest tests for tools/engine_pack.py (no pytest, no network, no root).

Tests that exercise the CLI subprocess or tomllib parsing require Python 3.11+
and are skipped cleanly on older interpreters; the pure render/validation logic
is exercised through plain dicts so it runs on 3.9+.
"""

import importlib.util
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
MODULE_PATH = REPO_ROOT / "tools" / "engine_pack.py"
PY311 = sys.version_info >= (3, 11)


def _load_module():
    spec = importlib.util.spec_from_file_location("engine_pack_under_test", MODULE_PATH)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules[spec.name] = module  # dataclasses resolves string annotations via sys.modules
    spec.loader.exec_module(module)
    return module


engine_pack = _load_module()

VALID_META = """\
[pack]
name = "cep-speech"
engine = "faster-whisper-fastapi"
version = "1.0.0"

[env]
python = "3.11"
packages = [
  "faster-whisper==1.2.1",
  "fastapi>=0.110",
]

[service]
unit = "cep-speech"
command = ["python", "asr_server.py"]
port = 3004
health_path = "/healthz"

[systemd]
user = "root"
after = "network-online.target"
description = "CEP Speech local ASR"
"""

FAKE_SERVER = "print('cep-speech asr_server stub')\n"


def env_meta_dict() -> dict:
    """Same manifest as VALID_META, as a plain dict (no tomllib needed)."""
    return {
        "pack": {
            "name": "cep-speech",
            "engine": "faster-whisper-fastapi",
            "version": "1.0.0",
        },
        "env": {"python": "3.11", "packages": ["faster-whisper==1.2.1", "fastapi>=0.110"]},
        "service": {
            "unit": "cep-speech",
            "command": ["python", "asr_server.py"],
            "port": 3004,
            "health_path": "/healthz",
        },
        "systemd": {
            "user": "root",
            "after": "network-online.target",
            "description": "CEP Speech local ASR",
        },
    }


class PackFixtureCase(unittest.TestCase):
    """Base: a temp pack directory written by the test itself."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory(prefix="engine-pack-test-")
        self.addCleanup(tmp.cleanup)
        self.tmpdir = Path(tmp.name)
        self.pack_dir = self.tmpdir / "cep-speech"
        self.pack_dir.mkdir()

    def write_pack(self, meta: str = VALID_META, with_server: bool = True) -> Path:
        (self.pack_dir / "engine.meta").write_text(meta, encoding="utf-8")
        if with_server:
            (self.pack_dir / "asr_server.py").write_text(FAKE_SERVER, encoding="utf-8")
        return self.pack_dir

    def run_cli(self, *cli_args: str) -> subprocess.CompletedProcess:
        return subprocess.run(
            [sys.executable, str(MODULE_PATH), *cli_args],
            cwd=str(REPO_ROOT),
            text=True,
            capture_output=True,
            timeout=60,
        )


@unittest.skipUnless(PY311, "engine-pack CLI needs Python 3.11+ (tomllib)")
class TestPlanOffline(PackFixtureCase):
    def test_plan_exits_zero_and_mentions_name_and_port(self):
        proc = self.run_cli("plan", str(self.write_pack()))
        self.assertEqual(proc.returncode, 0, msg=proc.stderr)
        self.assertIn("cep-speech", proc.stdout)
        self.assertIn("3004", proc.stdout)
        self.assertIn("/healthz", proc.stdout)

    def test_plan_is_offline_and_touches_nothing(self):
        pack = self.write_pack()
        proc = self.run_cli("plan", str(pack))
        self.assertEqual(proc.returncode, 0, msg=proc.stderr)
        # plan must not create or modify anything inside the pack dir
        self.assertEqual(
            sorted(p.name for p in pack.iterdir()), ["asr_server.py", "engine.meta"]
        )

    def test_plan_native_binary_pack(self):
        native = (
            '[pack]\nname = "llama-chat"\nengine = "llama.cpp"\nversion = "1.0.0"\n\n'
            '[service]\nunit = "llama-chat"\n'
            'command = ["/opt/llama.cpp/bin/llama-server", "--port", "8080"]\n'
            'port = 8080\nhealth_path = "/health"\n'
        )
        (self.pack_dir / "engine.meta").write_text(native, encoding="utf-8")
        proc = self.run_cli("plan", str(self.pack_dir))
        self.assertEqual(proc.returncode, 0, msg=proc.stderr)
        self.assertIn("llama-chat", proc.stdout)
        self.assertIn("8080", proc.stdout)
        self.assertIn("native", proc.stdout)


@unittest.skipUnless(PY311, "engine-pack CLI needs Python 3.11+ (tomllib)")
class TestMetaErrors(PackFixtureCase):
    def test_missing_engine_meta_fails_with_clear_stderr(self):
        proc = self.run_cli("plan", str(self.pack_dir))  # no engine.meta written
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("engine.meta", proc.stderr)
        self.assertIn(str(self.pack_dir), proc.stderr)

    def test_missing_service_section_fails(self):
        meta = '[pack]\nname = "x"\nengine = "y"\nversion = "1"\n'
        proc = self.run_cli("plan", str(self.write_pack(meta=meta)))
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("service", proc.stderr)

    def test_toml_syntax_error_reports_line_info(self):
        proc = self.run_cli("plan", str(self.write_pack(meta='[pack]\nname = \n')))
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("invalid TOML", proc.stderr)
        self.assertIn("line", proc.stderr.lower())

    def test_native_pack_rejects_relative_command(self):
        native = (
            '[pack]\nname = "llama-chat"\nengine = "llama.cpp"\nversion = "1.0.0"\n\n'
            '[service]\nunit = "llama-chat"\ncommand = ["llama-server"]\n'
            'port = 8080\nhealth_path = "/health"\n'
        )
        proc = self.run_cli("plan", str(self.write_pack(meta=native)))
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("absolute path", proc.stderr)


class TestRenderUnit(PackFixtureCase):
    """Pure render logic — no tomllib, no systemd, runs on 3.9+."""

    def test_render_unit_env_python_prefix(self):
        spec = engine_pack.parse_spec(env_meta_dict())
        text = engine_pack.render_unit(
            spec,
            pack_dir=Path("/srv/packs/cep-speech"),
            env_prefix=Path("/mnt/work/engine-envs/cep-speech"),
        )
        self.assertIn(
            "ExecStart=/mnt/work/engine-envs/cep-speech/bin/python asr_server.py", text
        )
        self.assertIn("WorkingDirectory=/srv/packs/cep-speech", text)
        self.assertIn("Restart=always", text)
        self.assertIn("RestartSec=5", text)
        self.assertIn("User=root", text)
        self.assertIn("After=network-online.target", text)
        self.assertIn("WantedBy=multi-user.target", text)

    def test_render_unit_is_deterministic_and_pure(self):
        spec = engine_pack.parse_spec(env_meta_dict())
        first = engine_pack.render_unit(
            spec, pack_dir=Path("/srv/packs/cep-speech"), env_prefix=Path("/mnt/work/envs/x")
        )
        second = engine_pack.render_unit(
            spec, pack_dir=Path("/srv/packs/cep-speech"), env_prefix=Path("/mnt/work/envs/x")
        )
        self.assertEqual(first, second)

    def test_render_unit_native_binary_uses_verbatim_command(self):
        meta = env_meta_dict()
        del meta["env"]
        meta["service"]["command"] = ["/opt/llama.cpp/bin/llama-server", "--port", "8080"]
        spec = engine_pack.parse_spec(meta)
        text = engine_pack.render_unit(
            spec, pack_dir=Path("/srv/packs/llama-chat"), env_prefix=None
        )
        self.assertIn(
            "ExecStart=/opt/llama.cpp/bin/llama-server --port 8080", text
        )
        self.assertNotIn("bin/python", text)

    def test_parse_spec_rejects_relative_native_command(self):
        meta = env_meta_dict()
        del meta["env"]
        meta["service"]["command"] = ["llama-server"]
        with self.assertRaises(engine_pack.MetaError) as ctx:
            engine_pack.parse_spec(meta)
        self.assertIn("absolute path", str(ctx.exception))

    def test_parse_spec_collects_multiple_errors(self):
        with self.assertRaises(engine_pack.MetaError) as ctx:
            engine_pack.parse_spec({"pack": {}})
        message = str(ctx.exception)
        self.assertIn("service", message)
        self.assertIn("pack.name", message)


class TestPipRecordLogic(PackFixtureCase):
    """Record-file round trip and skip logic — filesystem only, no subprocess."""

    def test_record_roundtrip_is_sorted(self):
        prefix = self.tmpdir / "env"
        prefix.mkdir()
        env = engine_pack.EnvSpec(python="3.11", packages=("b-pkg==2", "a-pkg==1"))
        engine_pack.write_installed_record(prefix, "cep-speech", env)
        record = json.loads((prefix / ".engine-pack-installed.json").read_text())
        self.assertEqual(record["pack"], "cep-speech")
        self.assertEqual(record["packages"], ["a-pkg==1", "b-pkg==2"])
        self.assertEqual(engine_pack.read_installed_packages(prefix), ["a-pkg==1", "b-pkg==2"])

    def test_matching_record_skips_pip(self):
        prefix = self.tmpdir / "env"
        prefix.mkdir()
        env = engine_pack.EnvSpec(python="3.11", packages=("a-pkg==1",))
        engine_pack.write_installed_record(prefix, "cep-speech", env)
        self.assertFalse(engine_pack.ensure_pip_packages(prefix, env, "cep-speech"))

    def test_missing_record_reports_none(self):
        prefix = self.tmpdir / "env"
        prefix.mkdir()
        self.assertIsNone(engine_pack.read_installed_packages(prefix))


class TestSpecHelpers(PackFixtureCase):
    def test_health_url_and_unit_name(self):
        spec = engine_pack.parse_spec(env_meta_dict())
        self.assertEqual(spec.health_url, "http://127.0.0.1:3004/healthz")
        self.assertEqual(spec.unit_name, "cep-speech.service")

    def test_resolved_command_binds_python_to_prefix(self):
        spec = engine_pack.parse_spec(env_meta_dict())
        resolved = spec.resolved_command(Path("/mnt/work/engine-envs/cep-speech"))
        self.assertEqual(
            resolved,
            ("/mnt/work/engine-envs/cep-speech/bin/python", "asr_server.py"),
        )


if __name__ == "__main__":
    unittest.main()

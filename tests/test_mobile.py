"""Tests for the mobile build facility (ascii infra, doctor, adapters)."""

from __future__ import annotations
from pathlib import Path
import pytest
from celestia_devtools.mobile import (APPS, ascii_path_error, ascii_cargo_target_candidates, audit_ascii, build, ensure_ascii_dir, is_ascii, ndk_trap_lines, resolve_ascii_cargo_target)
from celestia_devtools.mobile.toolchain import toolchain_report, doctor

class test_ascii_infra:
    def test_is_ascii(self):
        assert is_ascii("/mnt/codespace/wowsp")
        assert is_ascii("C:/Users/langy/build")
        assert not is_ascii("D:/源代码/wowsp")
        assert not is_ascii("/home/ユーザー")

    def test_ensure_ascii_dir_rejects_non_ascii_with_remedy(self, tmp_path):
        target = Path(str(tmp_path) + "/非ascii")

        with pytest.raises(ascii_path_error) as exc:
            ensure_ascii_dir(target, "mobile CARGO_TARGET_DIR")

        # The message must tell the operator the override env var.
        assert "WOWSP_ANDROID_CARGO_TARGET" in str(exc.value)

    def test_ensure_ascii_dir_creates(self, tmp_path):
        made = ensure_ascii_dir(tmp_path / "nested" / "target", "test")
        assert made.is_dir()

    def test_candidate_order_is_wowsp_compatible(self, monkeypatch):
        monkeypatch.setenv("WOWSP_ANDROID_CARGO_TARGET", "/opt/override")
        monkeypatch.setenv("LOCALAPPDATA", "/fake/la")
        monkeypatch.setenv("TEMP", "/fake/temp")
        cands = ascii_cargo_target_candidates()
        # Env override first; the family alias second; wowsp's own alias
        # third (compat: both resolvers may coexist on one machine).
        assert cands[0] == Path("/opt/override")
        assert cands[1] == Path("/fake/la/celestia/mobile-target")
        assert cands[2] == Path("/fake/la/wowsp/android-target")

    def test_celestia_alias_env_also_wins(self, monkeypatch, tmp_path):
        monkeypatch.setenv("CELESTIA_MOBILE_CARGO_TARGET", str(tmp_path / "a"))
        assert resolve_ascii_cargo_target() == (tmp_path / "a")

    def test_audit_ascii_names_offenders(self):
        problems = audit_ascii({"repo root": Path("/mnt/x/源代码"), "JAVA_HOME": "/opt/jdk", "NDK_HOME": None})
        assert len(problems) == 1
        assert "repo root" in problems[0]
        assert "non-ASCII" in problems[0]

class test_doctor:
    def test_doctor_reports_unset_envs_and_ascii_repo(self, tmp_path, monkeypatch):
        for var in ("JAVA_HOME", "ANDROID_HOME", "NDK_HOME", "ANDROID_NDK_ROOT", "ANDROID_NDK_HOME"):
            monkeypatch.delenv(var, raising = False)

        report = doctor(tmp_path)
        assert report.java_home is None
        assert not report.ok_for_android()

    def test_doctor_flags_non_ascii_repo(self, tmp_path, monkeypatch):
        monkeypatch.setenv("JAVA_HOME", "/opt/jdk")
        monkeypatch.setenv("ANDROID_HOME", "/opt/android-sdk")

        for var in ("NDK_HOME", "ANDROID_NDK_ROOT", "ANDROID_NDK_HOME"):
            monkeypatch.delenv(var, raising = False)

        report = doctor(Path("/fake/非ascii"))
        assert any("repo root" in p for p in report.ascii_problems)

    def test_ndk_trap_when_newest_differs_from_pin(self):
        report = toolchain_report(ndk_home = "/opt/android-sdk/ndk/26.1.10909125", ndk_versions = ["25.2.9519653", "26.1.10909125", "27.0.1"], newest_ndk = "27.0.1")
        lines = ndk_trap_lines(report)
        assert any(("27.0.1" in line) and ("trap" in line) for line in lines)
        # Multi-NDK alone is not a warning anymore — only the real trap is.
        assert len(lines) == 1

    def test_no_trap_when_single_pinned(self):
        report = toolchain_report(ndk_home = "/opt/android-sdk/ndk/26.1.10909125", ndk_versions = ["26.1.10909125"], newest_ndk = "26.1.10909125")
        assert ndk_trap_lines(report) == []

class test_adapters:
    def test_registry_shape(self):
        assert set(APPS) == {"wowsp-android", "chest-android", "chest-harmony"}

        for app in APPS.values():
            assert app.build_command, f"{app.key} must declare a build command"
            assert app.artifact_globs, f"{app.key} must declare artifact globs"

        assert APPS["wowsp-android"].experimental is False
        assert APPS["chest-android"].experimental is True
        assert APPS["chest-harmony"].experimental is True

    def test_unknown_app_exits_loudly(self):
        assert build("nope") == 2

    def test_missing_workspace_exits_loudly(self, tmp_path):
        assert build("wowsp-android", repo_root = tmp_path / "absent") == 2

    def test_wowsp_build_refuses_without_toolchain(self, tmp_path, monkeypatch):
        # A workspace that exists but no Android env: the adapter must run
        # the audit and refuse (exit 1) rather than shell out and fail
        # confusingly inside gradle.
        for var in ("JAVA_HOME", "ANDROID_HOME", "ANDROID_SDK_ROOT", "NDK_HOME"):
            monkeypatch.delenv(var, raising = False)

        ws = tmp_path / "wowsp"
        ws.mkdir()
        assert build("wowsp-android", repo_root = ws) == 1

    def test_wowsp_build_refuses_on_ndk_trap(self, tmp_path, monkeypatch, capsys):
        # The gen tree EXISTS here — without the refusal the build would
        # proceed past the scaffold gate, so this pins the refusal itself
        # (the R2 m2 mutation survived exactly this vacuity).
        ws = tmp_path / "wowsp"
        (ws / "packages/app/tauri/gen/android").mkdir(parents = True)
        monkeypatch.setenv("JAVA_HOME", "/opt/jdk")
        sdk = tmp_path / "sdk"
        ndk_root = sdk / "ndk"
        (ndk_root / "26.1.10909125").mkdir(parents = True)
        (ndk_root / "27.0.1").mkdir()
        monkeypatch.setenv("ANDROID_HOME", str(sdk))
        monkeypatch.setenv("NDK_HOME", str(ndk_root / "26.1.10909125"))
        assert build("wowsp-android", repo_root = ws) == 1
        assert "refusing to continue" in capsys.readouterr().out

    def test_no_trap_when_pin_selects_newest(self):
        # An older leftover with the pin ON the newest is deterministic —
        # not a warning (the R2 over-refusal finding).
        report = toolchain_report(ndk_home = "/sdk/ndk/26.1.10909125", ndk_versions = ["25.2.9519653", "26.1.10909125"], newest_ndk = "26.1.10909125")
        assert ndk_trap_lines(report) == []

    def test_dry_run_prints_plan(self, tmp_path, monkeypatch, capsys):
        ws = tmp_path / "wowsp"
        ws.mkdir()
        monkeypatch.setenv("JAVA_HOME", "/opt/jdk")
        sdk = tmp_path / "sdk"
        ndk_root = sdk / "ndk"
        (ndk_root / "26.1.10909125").mkdir(parents = True)
        monkeypatch.setenv("ANDROID_HOME", str(sdk))
        monkeypatch.setenv("NDK_HOME", str(ndk_root / "26.1.10909125"))
        # The tauri gen tree check needs packages/app/tauri/gen/android.
        (ws / "packages/app/tauri/gen/android").mkdir(parents = True)
        assert build("wowsp-android", repo_root = ws, dry_run = True) == 0
        out = capsys.readouterr().out
        assert "just build android-apk" in out
        # The convergence contract: BOTH env names ride the adapter.
        assert "cargo target dir pinned" in out

    def test_chest_android_requires_scaffold(self, tmp_path, monkeypatch):
        ws = tmp_path / "chest"
        ws.mkdir()
        monkeypatch.setenv("JAVA_HOME", "/opt/jdk")
        sdk = tmp_path / "sdk"
        (sdk / "ndk/26.1.10909125").mkdir(parents = True)
        monkeypatch.setenv("ANDROID_HOME", str(sdk))
        monkeypatch.setenv("NDK_HOME", str(sdk / "ndk/26.1.10909125"))
        assert build("chest-android", repo_root = ws) == 1

    def test_harmony_requires_hvigorw(self, tmp_path, monkeypatch):
        ws = tmp_path / "chest"
        ws.mkdir()
        monkeypatch.setattr("shutil.which", lambda name: None)
        assert build("chest-harmony", repo_root = ws) == 1

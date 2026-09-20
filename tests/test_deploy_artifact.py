"""Tests for the artifact channel: build → index → fetch with sha256 teeth."""

from __future__ import annotations

import io
import os
import tarfile

import pytest

from celestia_devtools.deploy import artifact


def _fake_bin(tmp_path, payload=b"#!/bin/sh\necho chest-ok\n"):
    p = tmp_path / "chest"
    p.write_bytes(payload)
    os.chmod(p, 0o755)
    return p


class TestBuildIndexFetch:
    def test_round_trip_verifies_and_extracts(self, tmp_path):
        out = tmp_path / "out"
        entry = artifact.build(_fake_bin(tmp_path), "0.1.5",
                               "x86_64-unknown-linux-gnu", out)
        assert entry.file == "chest-0.1.5-x86_64-unknown-linux-gnu.tar.gz"
        assert (out / (entry.file + ".sha256")).exists()

        artifact.write_index(out, {"stable": artifact.scan_dir(out)})
        index = artifact.read_index(out / "index.toml")
        picked = artifact.resolve(index, "stable", "x86_64-unknown-linux-gnu")
        assert picked.version == "0.1.5"

        dest = tmp_path / "dest"
        bin_path = artifact.fetch(picked, str(out), dest)
        assert bin_path == dest / "chest"
        assert bin_path.read_bytes().startswith(b"#!/bin/sh")
        assert os.stat(bin_path).st_mode & 0o111, "binary must stay executable"
        assert (dest / "chest.version").read_text().strip() == \
            "chest-0.1.5-x86_64-unknown-linux-gnu"

    def test_resolve_picks_newest_version(self, tmp_path):
        out = tmp_path / "out"
        artifact.build(_fake_bin(tmp_path), "0.1.5", "t1", out)
        artifact.build(_fake_bin(tmp_path), "0.2.0", "t1", out)
        artifact.build(_fake_bin(tmp_path), "0.1.9", "t1", out)
        index = artifact.read_index(artifact.write_index(out, {"stable": artifact.scan_dir(out)}))
        assert artifact.resolve(index, "stable", "t1").version == "0.2.0"

    def test_channel_and_target_filters(self, tmp_path):
        out = tmp_path / "out"
        artifact.build(_fake_bin(tmp_path), "0.1.5", "t1", out)
        index = artifact.read_index(artifact.write_index(out, {"internal": artifact.scan_dir(out)}))
        with pytest.raises(artifact.ArtifactError, match="stable"):
            artifact.resolve(index, "stable", "t1")
        with pytest.raises(artifact.ArtifactError, match="target"):
            artifact.resolve(index, "internal", "other-target")

    def test_sha_mismatch_refuses_and_cleans_up(self, tmp_path):
        out = tmp_path / "out"
        entry = artifact.build(_fake_bin(tmp_path), "0.1.5", "t1", out)
        archive = out / entry.file
        raw = bytearray(archive.read_bytes())
        raw[-20] ^= 0xFF  # corrupt the gzip tail
        archive.write_bytes(bytes(raw))
        dest = tmp_path / "dest"
        with pytest.raises(artifact.ArtifactError, match="sha256 mismatch"):
            artifact.fetch(entry, str(out), dest)
        assert not (dest / entry.file).exists(), "a failed fetch leaves nothing behind"

    def test_traversal_member_is_refused(self, tmp_path):
        out = tmp_path / "out"
        entry = artifact.build(_fake_bin(tmp_path), "0.1.5", "t1", out)
        # re-pack the sidecar tar with an evil member appended
        evil = out / "chest-0.1.5-t1.evil.tar.gz"
        with tarfile.open(out / entry.file, "r:gz") as src, \
                tarfile.open(evil, "w:gz") as dst:
            for m in src.getmembers():
                f = src.extractfile(m) if m.isfile() else None
                dst.addfile(m, f)
            info = tarfile.TarInfo("../../evil.sh")
            info.size = 2
            dst.addfile(info, io.BytesIO(b"hi"))
        evil_entry = artifact.Entry(file=evil.name, version="0.1.5", target="t1",
                                    sha256=artifact.sha256_file(evil),
                                    size=evil.stat().st_size, date="2026-09-20")
        dest = tmp_path / "dest2"
        with pytest.raises(IOError, match="traversal"):
            artifact.fetch(evil_entry, str(out), dest)

    def test_scan_dir_skips_non_artifacts(self, tmp_path):
        out = tmp_path / "out"
        out.mkdir()
        (out / "readme.txt").write_text("junk", encoding="utf-8")
        artifact.build(_fake_bin(tmp_path), "0.1.5", "t1", out)
        names = [e.file for e in artifact.scan_dir(out)]
        assert names == ["chest-0.1.5-t1.tar.gz"]


class TestFileUrl:
    def test_file_scheme_loads_like_plain_path(self, tmp_path):
        out = tmp_path / "out"
        artifact.build(_fake_bin(tmp_path), "0.1.5", "t1", out)
        path = artifact.write_index(out, {"stable": artifact.scan_dir(out)})
        via_file = artifact.read_index("file://{}".format(path))
        via_plain = artifact.read_index(path)
        assert via_file["stable"][0].sha256 == via_plain["stable"][0].sha256


class TestCli:
    def test_registry_and_dispatch(self, monkeypatch, capsys):
        from celestia_devtools.core import cli as core_cli
        assert core_cli.COMMANDS["deploy"] == "celestia_devtools.deploy.cli"
        monkeypatch.setattr("sys.argv", ["deploy", "artifact"])
        from celestia_devtools.deploy import cli as deploy_cli
        with pytest.raises(SystemExit) as ei:  # argparse: required subcommand
            deploy_cli.main()
        assert ei.value.code == 2

    def test_index_then_fetch_via_cli(self, tmp_path, monkeypatch, capsys):
        out = tmp_path / "out"
        artifact.build(_fake_bin(tmp_path), "0.1.5", "t1", out)
        from celestia_devtools.deploy import cli as deploy_cli
        monkeypatch.setattr("sys.argv", ["deploy", "artifact", "index",
                                         "--dir", str(out), "--channel", "stable"])
        assert deploy_cli.main() == 0
        monkeypatch.setattr("sys.argv", ["deploy", "artifact", "fetch",
                                         "--source", str(out), "--channel", "stable",
                                         "--target", "t1", "--dest", str(tmp_path / "d")])
        assert deploy_cli.main() == 0
        assert (tmp_path / "d" / "chest").exists()

"""Tests for the deploy profile (TOML round-trip, validation, permissions)."""

from __future__ import annotations

import os
import stat

import pytest

from celestia_devtools.deploy.profile import (
    DeployProfile, FrontSection, HostSection, ProfileError,
)


def _valid_profile() -> DeployProfile:
    return DeployProfile(
        host=HostSection(face="chest", level="selfhosted", listen=3000),
        front=FrontSection(enabled=True, domain="acme.example",
                           email="ops@acme.example"),
    )


class TestRoundTrip:
    def test_toml_round_trip_preserves_sections(self):
        p = _valid_profile()
        q = DeployProfile.from_toml(p.to_toml())
        assert q.host == p.host
        assert q.front == p.front
        assert q.admin == p.admin
        assert q.artifact == p.artifact
        assert q.database == p.database

    def test_write_is_0600_and_has_no_secret_marker(self, tmp_path):
        p = _valid_profile()
        path = tmp_path / "chest.profile.toml"
        p.write(path)
        mode = stat.S_IMODE(os.stat(path).st_mode)
        assert mode == 0o600
        text = path.read_text(encoding="utf-8")
        assert "NOT a secret store" in text  # the warning rides along
        assert "JWT_SECRET" not in text      # and no secret keys ever appear

    def test_read_rejects_unknown_keys(self):
        p = _valid_profile()
        text = p.to_toml().replace("[host]", "[host]\nnope = 1")
        with pytest.raises(ProfileError, match="host.nope"):
            DeployProfile.from_toml(text)

    def test_read_rejects_wrong_types(self):
        """R2-F2: hand-edited wrong types must be refused — a string "n" in
        password_to_file would flip the print-once password semantics."""
        p = _valid_profile()
        text = p.to_toml().replace("listen = 3000", 'listen = "3000"')
        with pytest.raises(ProfileError, match="host.listen"):
            DeployProfile.from_toml(text)
        text = p.to_toml().replace("password_to_file = false",
                                   'password_to_file = "n"')
        with pytest.raises(ProfileError, match="password_to_file"):
            DeployProfile.from_toml(text)

    def test_read_rejects_invalid_embedded_values(self):
        p = _valid_profile()
        text = p.to_toml().replace('face = "chest"', 'face = "Bad Face!"')
        with pytest.raises(ProfileError, match="host.face"):
            DeployProfile.from_toml(text)


class TestValidation:
    def test_valid_profile_has_no_errors(self):
        assert _valid_profile().validate() == []

    def test_front_enabled_requires_domain(self):
        p = _valid_profile()
        p.front.domain = ""
        assert any("front.domain" in e for e in p.validate())

    def test_listen_range_checked(self):
        p = _valid_profile()
        p.host.listen = 70000
        assert any("host.listen" in e for e in p.validate())

    def test_face_slug_checked(self):
        p = _valid_profile()
        p.host.face = "Chest"
        assert any("host.face" in e for e in p.validate())

    def test_bad_admin_email_checked(self):
        p = _valid_profile()
        p.admin.email = "not-an-email"
        assert any("admin.email" in e for e in p.validate())


class TestDerivedPaths:
    def test_unit_and_paths_derive_from_face(self):
        p = _valid_profile()
        assert p.host.unit_name() == "chest.service"
        assert str(p.host.deploy_root()) == "/srv/celestia/chest"
        assert str(p.host.etc_env()) == "/etc/celestia/chest.env"
        assert str(p.host.profile_path()) == "/etc/celestia/chest.profile.toml"

"""Tests for dated `[weld_window]` exemptions in family-versions (2026-11-07 ruling)."""

from __future__ import annotations
import json
import subprocess
from datetime import date
from pathlib import Path
import celestia_devtools.repo.family_versions as fv
from celestia_devtools.repo.family_versions import check, main

def _cargo(root: Path, body: str = '[dependencies]\nplana = "^0.2"\n') -> Path:
    path = root / "Cargo.toml"
    path.write_text(body, encoding = "utf-8")
    return path

def _levels(findings) -> list[str]:
    return [finding.level for finding in findings]

def _entry(until: str, lines: tuple[str, ...] = ("plana",), reason: str = "test weld") -> dict:
    return {"until": until, "reason": reason, "lines": lines}

def _weld(monkeypatch, table: dict) -> None:
    monkeypatch.setattr(fv, "WELD_WINDOW", table)

# ── a hit is an INFO line, not red ───────────────────────────────────────────

def test_a_welded_generation_is_an_info_line_not_a_violation(tmp_path, monkeypatch):
    _cargo(tmp_path)
    _weld(monkeypatch, {"celestia-island/arona": _entry("external-beta")})
    findings = check(tmp_path, repo = "celestia-island/arona")
    assert _levels(findings) == ["info"], findings
    assert "welded until external-beta" in findings[0].message

def test_the_weld_message_names_the_deadline_and_reason(tmp_path, monkeypatch):
    _cargo(tmp_path)
    _weld(monkeypatch, {"celestia-island/arona": _entry("external-beta", reason = "ruling-2026-11-07")})
    [finding] = check(tmp_path, repo = "celestia-island/arona")
    assert finding.level == "info"
    assert "welded until external-beta (ruling-2026-11-07)" in finding.message

def test_the_family_token_covers_every_plana_member(tmp_path, monkeypatch):
    _cargo(tmp_path, '[dependencies]\nplana-celestia-types = "^0.1"\nplana-build-info = "^0.1"\n')
    _weld(monkeypatch, {"celestia-island/arona": _entry("2099-01-01")})
    findings = check(tmp_path, repo = "celestia-island/arona")
    assert _levels(findings) == ["info", "info"], findings

def test_non_generational_violations_are_not_welded(tmp_path, monkeypatch):
    # A wrong git source is a configuration error, not a generational difference.
    _cargo(tmp_path, '[dependencies]\nplana = { git = "https://github.com/other/plana.git", branch = "master" }\n')
    _weld(monkeypatch, {"celestia-island/arona": _entry("external-beta")})
    findings = check(tmp_path, repo = "celestia-island/arona")
    assert _levels(findings) == ["violation"], findings

# ── expiry / deletion restores the violation ────────────────────────────────

def test_an_expired_date_restores_the_violation(tmp_path, monkeypatch):
    _cargo(tmp_path)
    _weld(monkeypatch, {"celestia-island/arona": _entry("2020-01-01")})
    findings = check(tmp_path, repo = "celestia-island/arona", today = date(2026, 11, 7))
    assert _levels(findings) == ["violation"], findings

def test_the_deadline_day_itself_is_still_welded(tmp_path, monkeypatch):
    _cargo(tmp_path)
    _weld(monkeypatch, {"celestia-island/arona": _entry("2026-11-07")})
    assert _levels(check(tmp_path, repo = "celestia-island/arona", today = date(2026, 11, 7))) == ["info"]

def test_removing_the_row_restores_the_violation(tmp_path, monkeypatch):
    _cargo(tmp_path)
    _weld(monkeypatch, {})
    assert _levels(check(tmp_path, repo = "celestia-island/arona")) == ["violation"]

def test_a_milestone_label_does_not_auto_expire(tmp_path, monkeypatch):
    _cargo(tmp_path)
    _weld(monkeypatch, {"celestia-island/arona": _entry("external-beta")})
    assert _levels(check(tmp_path, repo = "celestia-island/arona", today = date(2099, 1, 1))) == ["info"]

# ── scoping and negative controls ───────────────────────────────────────────

def test_a_non_welded_repo_is_unaffected(tmp_path, monkeypatch):
    _cargo(tmp_path)
    _weld(monkeypatch, {"celestia-island/arona": _entry("external-beta")})
    assert _levels(check(tmp_path, repo = "evernight")) == ["violation"]

def test_a_typo_in_the_repo_name_welds_nobody(tmp_path, monkeypatch):
    _cargo(tmp_path)
    _weld(monkeypatch, {"celestia-island/aronaa": _entry("external-beta")})
    assert _levels(check(tmp_path, repo = "celestia-island/arona")) == ["violation"]

def test_a_line_token_typo_welds_nothing(tmp_path, monkeypatch):
    _cargo(tmp_path)
    _weld(monkeypatch, {"celestia-island/arona": _entry("external-beta", lines = ("plan",))})
    assert _levels(check(tmp_path, repo = "celestia-island/arona")) == ["violation"]

# ── caller identity: CI env wins, git remote is the local fallback ──────────

def test_github_repository_names_the_caller(tmp_path, monkeypatch):
    _cargo(tmp_path)
    _weld(monkeypatch, {"celestia-island/arona": _entry("external-beta")})
    monkeypatch.setenv("GITHUB_REPOSITORY", "celestia-island/arona")
    assert _levels(check(tmp_path)) == ["info"]

def test_the_git_remote_names_the_caller_locally(tmp_path, monkeypatch):
    _cargo(tmp_path)
    monkeypatch.delenv("GITHUB_REPOSITORY", raising = False)
    subprocess.run(["git", "init", "-q"], cwd = tmp_path, check = True)
    subprocess.run(["git", "remote", "add", "origin", "https://github.com/celestia-island/shittim-chest.git"], cwd = tmp_path, check = True)
    _weld(monkeypatch, {"celestia-island/shittim-chest": _entry("external-beta")})
    assert _levels(check(tmp_path)) == ["info"]

def test_github_repository_wins_over_the_target_remote(tmp_path, monkeypatch):
    _cargo(tmp_path)
    subprocess.run(["git", "init", "-q"], cwd = tmp_path, check = True)
    subprocess.run(["git", "remote", "add", "origin", "https://github.com/celestia-island/arona.git"], cwd = tmp_path, check = True)
    monkeypatch.setenv("GITHUB_REPOSITORY", "celestia-island/celestia-erp")
    _weld(monkeypatch, {"celestia-island/celestia-erp": _entry("external-beta")})
    assert _levels(check(tmp_path)) == ["info"]

# ── CLI: exit codes, INFO output, JSON ──────────────────────────────────────

def test_the_cli_exits_zero_and_prints_the_weld_info(tmp_path, monkeypatch, capsys):
    _cargo(tmp_path)
    _weld(monkeypatch, {"celestia-island/arona": _entry("2099-01-01")})
    monkeypatch.setenv("GITHUB_REPOSITORY", "celestia-island/arona")
    assert main([str(tmp_path)]) == 0
    captured = capsys.readouterr()
    combined = captured.out + captured.err
    assert "welded until 2099-01-01 (test weld)" in combined
    assert "0 violation(s), 0 warning(s), 1 welded" in combined

def test_the_cli_fails_once_the_window_expired(tmp_path, monkeypatch, capsys):
    _cargo(tmp_path)
    _weld(monkeypatch, {"celestia-island/arona": _entry("2020-01-01")})
    monkeypatch.setenv("GITHUB_REPOSITORY", "celestia-island/arona")
    assert main([str(tmp_path)]) == 1
    assert "1 violation(s)" in capsys.readouterr().err

def test_json_reports_the_welded_count(tmp_path, monkeypatch, capsys):
    _cargo(tmp_path)
    _weld(monkeypatch, {"celestia-island/arona": _entry("external-beta")})
    monkeypatch.setenv("GITHUB_REPOSITORY", "celestia-island/arona")
    assert main([str(tmp_path), "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["violations"] == 0
    assert payload["welded"] == 1
    assert payload["findings"][0]["level"] == "info"
    assert payload["findings"][0]["welded_until"] == "external-beta"
    assert payload["findings"][0]["weld_key"] == "plana"

def test_an_unrelated_info_line_is_not_counted_as_welded(tmp_path, monkeypatch, capsys):
    # A nested workspace lock emits an INFO line of its own; only weld-generated
    # INFO findings may be reported as welded.
    _cargo(tmp_path)
    (tmp_path / "fuzz").mkdir()
    (tmp_path / "fuzz" / "Cargo.lock").write_text('version = 3\n', encoding = "utf-8")
    _weld(monkeypatch, {"celestia-island/arona": _entry("external-beta")})
    monkeypatch.setenv("GITHUB_REPOSITORY", "celestia-island/entelecheia")
    main([str(tmp_path)])
    captured = capsys.readouterr()
    assert "welded" not in (captured.out + captured.err)

# ── the shipped table ───────────────────────────────────────────────────────

def test_the_shipped_table_welds_the_three_ruling_repos():
    assert set(fv.WELD_WINDOW) == {
        "celestia-island/arona",
        "celestia-island/shittim-chest",
        "celestia-island/celestia-erp",
    }

    for repo, entry in fv.WELD_WINDOW.items():
        assert entry["until"] == "external-beta", repo
        assert "plana" in entry["lines"], repo

    assert "kirino_session" in fv.WELD_WINDOW["celestia-island/shittim-chest"]["lines"]
    assert "kirino_session" in fv.WELD_WINDOW["celestia-island/arona"]["lines"]
    assert "kirino_session" in fv.WELD_WINDOW["celestia-island/celestia-erp"]["lines"]

# ── S2 hardening (R2 2026-11-07): no silent permanent exemption ─────────────

_MINIMAL_HEAD = (
    'updated_at = "2026-11-07"\n'
    'source = "test fixture"\n'
    '[plana_published]\n'
    'plana = "0.3"\n'
    '[rust_layers.plana]\n'
    'line = "0.3"\n'
    'why = "fixture"\n'
    '[npm_layers."@celestia-island/x"]\n'
    'line = "0.1"\n'
    'why = "fixture"\n'
)

def _load_with(tmp_path: Path, weld_body: str):
    path = tmp_path / "family_layers.toml"
    path.write_text(_MINIMAL_HEAD + weld_body, encoding = "utf-8")
    return fv.load_family_layers(path)

def test_a_malformed_date_does_not_become_a_permanent_exemption(tmp_path):
    # `2026-11-7` is not ISO: read as a milestone it would never expire while
    # the message still claims "welded until 2026-11-7". The loader must reject
    # any digit-leading `until` that is not strict `YYYY-MM-DD`.
    base = '[weld_window]\n"celestia-island/arona" = { until = "%s", reason = "x", lines = ["plana"] }\n'

    for bad in ("2026-11-7", "2026/11/07", "2026-11-07T00:00:00", "2026-13-01", "2"):
        try:
            _load_with(tmp_path, base % bad)
        except fv.family_layers_error as exc:
            assert "ISO" in str(exc) or "real date" in str(exc), bad
        else:
            raise AssertionError("%r was accepted as a weld deadline" % bad)

    loaded = _load_with(tmp_path, base % "2026-11-07")
    assert loaded["weld_window"]["celestia-island/arona"]["until"] == "2026-11-07"

def test_a_bare_repository_key_is_rejected(tmp_path):
    # A bare name would exempt any repository that happens to be called `arona`.
    body = '[weld_window]\narona = { until = "external-beta", reason = "x", lines = ["plana"] }\n'

    try:
        _load_with(tmp_path, body)
    except fv.family_layers_error as exc:
        assert "owner/repo" in str(exc)
    else:
        raise AssertionError("a bare-name weld key was accepted")

def test_an_unprovable_identity_welds_nothing(tmp_path, monkeypatch):
    # No GITHUB_REPOSITORY and no origin remote (just a directory that happens
    # to be called `arona`): the gate must not guess.
    root = tmp_path / "arona"
    root.mkdir()
    _cargo(root)
    monkeypatch.delenv("GITHUB_REPOSITORY", raising = False)
    _weld(monkeypatch, {"celestia-island/arona": _entry("external-beta")})

    assert _levels(check(root)) == ["violation"]

def test_a_fork_does_not_inherit_the_weld(tmp_path, monkeypatch):
    # `someone/arona` is a different repository identity than the ruled one.
    _cargo(tmp_path)
    subprocess.run(["git", "init", "-q"], cwd = tmp_path, check = True)
    subprocess.run(["git", "remote", "add", "origin", "https://github.com/someone/arona.git"], cwd = tmp_path, check = True)
    monkeypatch.delenv("GITHUB_REPOSITORY", raising = False)
    _weld(monkeypatch, {"celestia-island/arona": _entry("external-beta")})

    assert _levels(check(tmp_path)) == ["violation"]

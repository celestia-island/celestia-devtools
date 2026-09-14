"""Tests for the P0 ledger gate (lint/p0_gate.py)."""

import shutil
import subprocess
import textwrap
from pathlib import Path

import pytest

from celestia_devtools.lint.p0_gate import (
    ACK_ENV,
    LEDGER_PATH_ENV,
    PR_BODY_ENV,
    LedgerError,
    bundled_ledger_path,
    collect_acks,
    detect_repo_name,
    entries_for_repo,
    load_ledger,
    main,
    normalize_repo_name,
    open_entries_for_repo,
    parse_ack_markers,
    parse_ledger_text,
    render_list,
    repo_name_from_url,
    resolve_ledger_path,
)

LEDGER = """\
version = 1
updated_at = "2026-09-13"

[[p0]]
id = "P0-A"
title = "Open finding on entelecheia"
severity = "p0"
scope = ["entelecheia"]
status = "open"
opened_at = "2026-09-10"
closed_by = ""
evidence = "reviewed 2026-09-13: still present"

[[p0]]
id = "P0-Z"
title = "Closed finding on arona"
severity = "p0"
scope = ["arona"]
status = "closed"
opened_at = "2026-09-01"
closed_by = "#123"
evidence = "closed by #123"
"""


def write_ledger(tmp_path, text=LEDGER):
    path = tmp_path / "p0-ledger.toml"
    path.write_text(textwrap.dedent(text), encoding="utf-8")
    return path


# ── Ledger parsing / validation ───────────────────────────────────────────────

class TestLedgerParsing:
    def test_parses_entries(self, tmp_path):
        entries = load_ledger(write_ledger(tmp_path))
        assert [entry.id for entry in entries] == ["P0-A", "P0-Z"]
        assert entries[0].is_open and not entries[1].is_open
        assert entries[0].scope == ("entelecheia",)

    def test_bundled_ledger_matches_plan_item_18(self):
        """Content tracks PLAN.md §1.2 item 18 at its 2026-09-14 revision.

        P0-A and P0-B were closed by entelecheia #285 on 2026-09-14 after four
        independent adversarial rounds — the last of which found the value binding
        real and could not break the shared addressing. P0-B's evidence carries an
        explicit residual list, so that "closed" is not read as "perfect".
        P0-C is closed by easy-hydro-miniprogram #27 and P0-D by entelecheia
        #284; P0-E is still open and scoped to evernight because its original
        master-CI numbers were measured on rewritten commits (AGENTS §8.3.7).
        """
        entries = load_ledger(bundled_ledger_path())
        by_id = {entry.id: entry for entry in entries}
        assert set(by_id) == {"P0-A", "P0-B", "P0-C", "P0-D", "P0-E"}
        assert {entry.id for entry in entries if entry.is_open} == {"P0-E"}
        assert by_id["P0-A"].status == "closed"
        assert by_id["P0-A"].closed_by == "celestia-island/entelecheia#285"
        assert by_id["P0-B"].status == "closed"
        assert by_id["P0-B"].closed_by == "celestia-island/entelecheia#285"
        # A closed P0 must still say what it left behind.
        assert "RESIDUALS" in by_id["P0-B"].evidence
        assert by_id["P0-C"].status == "closed"
        assert by_id["P0-C"].closed_by == "langyo/easy-hydro-miniprogram#27"
        assert by_id["P0-D"].status == "closed"
        assert by_id["P0-D"].closed_by == "celestia-island/entelecheia#284"
        assert all(entry.opened_at == "2026-09-10" for entry in entries)
        assert all(entry.evidence for entry in entries)
        assert by_id["P0-A"].covers("entelecheia")
        assert by_id["P0-C"].covers("easy-hydro-miniprogram")
        assert by_id["P0-D"].covers("arona") and by_id["P0-D"].covers("entelecheia")
        assert by_id["P0-E"].covers("evernight") and not by_id["P0-E"].covers("entelecheia")

    def test_missing_file_fails_closed(self, tmp_path):
        with pytest.raises(LedgerError, match="not found"):
            load_ledger(tmp_path / "nope.toml")

    def test_invalid_toml_fails_closed(self, tmp_path):
        with pytest.raises(LedgerError, match="not valid TOML"):
            load_ledger(write_ledger(tmp_path, "version = = 1\n[[p0]\n"))

    def test_missing_entries_fails_closed(self, tmp_path):
        with pytest.raises(LedgerError, match="no \\[\\[p0\\]\\] entries"):
            load_ledger(write_ledger(tmp_path, 'version = 1\n'))

    def test_empty_entries_fails_closed(self, tmp_path):
        with pytest.raises(LedgerError, match="no \\[\\[p0\\]\\] entries"):
            parse_ledger_text("version = 1\np0 = []\n")

    def test_unsupported_version_fails_closed(self, tmp_path):
        with pytest.raises(LedgerError, match="version"):
            parse_ledger_text(LEDGER.replace("version = 1", "version = 2"))

    def test_unknown_top_level_key_fails_closed(self):
        with pytest.raises(LedgerError, match="unknown top-level key"):
            parse_ledger_text(LEDGER + "\n[typo]\nx = 1\n")

    def test_missing_field_fails_closed(self):
        broken = LEDGER.replace('severity = "p0"\n', "")
        with pytest.raises(LedgerError, match="missing required key"):
            parse_ledger_text(broken)

    def test_unknown_entry_key_fails_closed(self):
        with pytest.raises(LedgerError, match="unknown key"):
            parse_ledger_text(LEDGER.replace('severity = "p0"', 'severity = "p0"\nstatuss = "open"'))

    def test_bad_status_fails_closed(self):
        with pytest.raises(LedgerError, match="status"):
            parse_ledger_text(LEDGER.replace('status = "open"', 'status = "maybe"'))

    def test_empty_scope_fails_closed(self):
        with pytest.raises(LedgerError, match="scope"):
            parse_ledger_text(LEDGER.replace('scope = ["entelecheia"]', "scope = []"))

    def test_bad_date_fails_closed(self):
        with pytest.raises(LedgerError, match="opened_at"):
            parse_ledger_text(LEDGER.replace('opened_at = "2026-09-10"', 'opened_at = "yesterday"'))

    def test_duplicate_ids_fail_closed(self):
        first_entry = LEDGER.split("[[p0]]")[1]
        with pytest.raises(LedgerError, match="duplicate id"):
            parse_ledger_text(LEDGER + "\n[[p0]]" + first_entry)

    def test_closed_entry_without_pr_fails_closed(self):
        with pytest.raises(LedgerError, match="closed_by"):
            parse_ledger_text(LEDGER.replace('closed_by = "#123"', 'closed_by = ""'))

    def test_open_entry_with_closed_by_fails_closed(self):
        with pytest.raises(LedgerError, match="closed_by"):
            parse_ledger_text(LEDGER.replace('closed_by = ""', 'closed_by = "#9"'))


class TestScopeMatching:
    def test_scope_patterns_match_repo_family(self):
        entries = parse_ledger_text(LEDGER.replace('["entelecheia"]', '["easy-hydro-*"]'))
        assert entries_for_repo(entries, "easy-hydro-erp")
        assert not entries_for_repo(entries, "hikari")

    def test_open_entries_filter_out_closed(self, tmp_path):
        entries = load_ledger(write_ledger(tmp_path))
        assert open_entries_for_repo(entries, "arona") == []
        assert [entry.id for entry in open_entries_for_repo(entries, "entelecheia")] == ["P0-A"]

    def test_repo_name_from_url(self):
        assert repo_name_from_url("https://github.com/celestia-island/arona.git") == "arona"
        assert repo_name_from_url("git@github.com:celestia-island/arona.git") == "arona"
        assert repo_name_from_url("") is None

    def test_normalize_repo_name_accepts_what_ci_passes(self):
        assert normalize_repo_name("entelecheia") == "entelecheia"
        assert normalize_repo_name("ENTelECHEia") == "entelecheia"
        assert normalize_repo_name(" entelecheia ") == "entelecheia"
        assert normalize_repo_name("entelecheia.git") == "entelecheia"
        assert normalize_repo_name("celestia-island/entelecheia") == "entelecheia"
        assert normalize_repo_name("https://github.com/celestia-island/entelecheia.git") == "entelecheia"
        assert normalize_repo_name("") == ""

    def test_spelling_variants_still_cover_the_repo(self, tmp_path, capsys, monkeypatch):
        monkeypatch.delenv(ACK_ENV, raising=False)
        ledger = write_ledger(tmp_path)
        for variant in ("ENTelECHEia", "entelecheia.git", "celestia-island/entelecheia"):
            assert main(["--repo", variant, "--ledger", str(ledger)]) == 1, variant
        capsys.readouterr()

    def test_scope_spelling_variants_are_matched(self):
        entries = parse_ledger_text(LEDGER.replace('["entelecheia"]', '["Entelecheia/"]'))
        assert open_entries_for_repo(entries, "entelecheia")

    def test_detect_repo_name_from_checkout(self, tmp_path):
        if not shutil.which("git"):
            pytest.skip("git not available")
        repo = tmp_path / "arona"
        repo.mkdir()
        subprocess.run(["git", "init", "-q", str(repo)], check=True)
        subprocess.run(
            ["git", "-C", str(repo), "remote", "add", "origin",
             "https://github.com/celestia-island/arona.git"],
            check=True,
        )
        assert detect_repo_name(repo) == "arona"

    def test_detect_repo_name_without_remote(self, tmp_path):
        assert detect_repo_name(tmp_path) is None


class TestAcknowledgmentParsing:
    def test_pr_body_marker(self):
        acks = parse_ack_markers("Body\n\nP0-ACK: P0-A P0-B because the demo runs on RTU\n", "pr")
        assert [ack.id for ack in acks] == ["P0-A", "P0-B"]
        assert acks[0].reason == "because the demo runs on RTU"
        assert acks[0].source == "pr"

    def test_marker_without_reason(self):
        assert parse_ack_markers("P0-ACK: P0-C", "pr")[0].reason == ""

    def test_case_insensitive_marker(self):
        assert parse_ack_markers("p0-ack: P0-E", "pr")[0].id == "P0-E"

    def test_no_marker(self):
        assert parse_ack_markers("nothing here", "pr") == []

    def test_collect_acks_merges_channels(self, monkeypatch):
        monkeypatch.setenv(ACK_ENV, "P0-C, P0-D")
        acks = collect_acks(["P0-A"], [("P0-ACK: P0-B\n", "--pr-body")])
        assert sorted(ack.id for ack in acks) == ["P0-A", "P0-B", "P0-C", "P0-D"]


class TestLedgerResolution:
    def test_explicit_wins(self, tmp_path, monkeypatch):
        monkeypatch.setenv(LEDGER_PATH_ENV, str(tmp_path / "env.toml"))
        assert resolve_ledger_path("/x/y.toml") == Path("/x/y.toml")

    def test_env_second(self, tmp_path, monkeypatch):
        monkeypatch.setenv(LEDGER_PATH_ENV, str(tmp_path / "env.toml"))
        assert resolve_ledger_path(None, repo_root=tmp_path) == tmp_path / "env.toml"

    def test_repo_local_third(self, tmp_path, monkeypatch):
        monkeypatch.delenv(LEDGER_PATH_ENV, raising=False)
        local = tmp_path / "p0-ledger.toml"
        local.write_text("version = 1\n", encoding="utf-8")
        assert resolve_ledger_path(None, repo_root=tmp_path) == local

    def test_bundled_default(self, tmp_path, monkeypatch):
        monkeypatch.delenv(LEDGER_PATH_ENV, raising=False)
        assert resolve_ledger_path(None, repo_root=tmp_path) == bundled_ledger_path()


# ── CLI semantics ─────────────────────────────────────────────────────────────

class TestCliPassAndBlock:
    def test_repo_without_open_p0_passes(self, tmp_path, capsys, monkeypatch):
        monkeypatch.delenv(ACK_ENV, raising=False)
        monkeypatch.delenv(PR_BODY_ENV, raising=False)
        ledger = write_ledger(tmp_path)
        rc = main(["--repo", "hikari", "--ledger", str(ledger)])
        assert rc == 0
        assert "clear" in capsys.readouterr().err

    def test_repo_with_open_p0_exits_non_zero(self, tmp_path, capsys, monkeypatch):
        monkeypatch.delenv(ACK_ENV, raising=False)
        ledger = write_ledger(tmp_path)
        rc = main(["--repo", "entelecheia", "--ledger", str(ledger)])
        err = capsys.readouterr().err
        assert rc == 1
        assert "P0-A" in err
        assert "Open finding on entelecheia" in err
        assert "reviewed 2026-09-13: still present" in err

    def test_other_repos_open_p0_does_not_block(self, tmp_path, capsys, monkeypatch):
        monkeypatch.delenv(ACK_ENV, raising=False)
        ledger = write_ledger(tmp_path)
        assert main(["--repo", "arona", "--ledger", str(ledger)]) == 0
        assert main(["--repo", "hikari", "--ledger", str(ledger)]) == 0
        capsys.readouterr()

    def test_multiple_repos_block_on_any_of_them(self, tmp_path, capsys, monkeypatch):
        monkeypatch.delenv(ACK_ENV, raising=False)
        ledger = write_ledger(tmp_path)
        rc = main(["--repo", "hikari", "--repo", "entelecheia", "--ledger", str(ledger)])
        assert rc == 1
        assert "P0-A" in capsys.readouterr().err

    def test_resolved_ledger_is_echoed_on_every_run(self, tmp_path, capsys, monkeypatch):
        """A ledger override must never be invisible on a green run."""
        monkeypatch.delenv(ACK_ENV, raising=False)
        ledger = write_ledger(tmp_path)
        assert main(["--repo", "hikari", "--ledger", str(ledger)]) == 0
        err = capsys.readouterr().err
        assert "p0-gate: ledger %s" % ledger in err
        assert "2 entries, 1 open" in err

    def test_one_entry_covering_two_repos_is_listed_once(self, tmp_path, capsys, monkeypatch):
        monkeypatch.delenv(ACK_ENV, raising=False)
        ledger = write_ledger(
            tmp_path,
            LEDGER.replace('scope = ["entelecheia"]', 'scope = ["entelecheia", "arona"]'),
        )
        rc = main(["--repo", "entelecheia", "--repo", "arona", "--ledger", str(ledger)])
        err = capsys.readouterr().err
        assert rc == 1
        assert "covered by 1 unresolved P0 finding(s)" in err
        assert err.count("P0-A:") == 1


class TestCliAcknowledgment:
    def test_ack_flag_passes_and_echoes(self, tmp_path, capsys, monkeypatch):
        monkeypatch.delenv(ACK_ENV, raising=False)
        ledger = write_ledger(tmp_path)
        rc = main(["--repo", "entelecheia", "--ledger", str(ledger), "--ack", "P0-A"])
        err = capsys.readouterr().err
        assert rc == 0
        assert "acknowledged" in err and "P0-A" in err and "--ack" in err

    def test_ack_env_passes(self, tmp_path, capsys, monkeypatch):
        monkeypatch.setenv(ACK_ENV, "P0-A")
        ledger = write_ledger(tmp_path)
        rc = main(["--repo", "entelecheia", "--ledger", str(ledger)])
        err = capsys.readouterr().err
        assert rc == 0
        assert ACK_ENV in err

    def test_ack_pr_body_marker_passes(self, tmp_path, capsys, monkeypatch):
        monkeypatch.delenv(ACK_ENV, raising=False)
        monkeypatch.delenv(PR_BODY_ENV, raising=False)
        body = tmp_path / "body.md"
        body.write_text("Fixes stuff.\n\nP0-ACK: P0-A demo runs on RTU this week\n", encoding="utf-8")
        ledger = write_ledger(tmp_path)
        rc = main(["--repo", "entelecheia", "--ledger", str(ledger),
                   "--pr-body-file", str(body)])
        err = capsys.readouterr().err
        assert rc == 0
        assert "demo runs on RTU this week" in err

    def test_ack_pr_body_env_passes(self, tmp_path, capsys, monkeypatch):
        monkeypatch.delenv(ACK_ENV, raising=False)
        monkeypatch.setenv(PR_BODY_ENV, "P0-ACK: P0-A")
        ledger = write_ledger(tmp_path)
        assert main(["--repo", "entelecheia", "--ledger", str(ledger)]) == 0
        assert "acknowledged" in capsys.readouterr().err

    def test_partial_ack_still_blocks_the_rest(self, tmp_path, capsys, monkeypatch):
        monkeypatch.delenv(ACK_ENV, raising=False)
        ledger = write_ledger(
            tmp_path,
            LEDGER.replace('id = "P0-Z"', 'id = "P0-B"').replace(
                'status = "closed"', 'status = "open"'
            ).replace('scope = ["arona"]', 'scope = ["entelecheia"]').replace(
                'closed_by = "#123"', 'closed_by = ""'
            ),
        )
        rc = main(["--repo", "entelecheia", "--ledger", str(ledger), "--ack", "P0-A"])
        err = capsys.readouterr().err
        assert rc == 1
        assert "acknowledged P0-A" in err
        assert "covered by 1 unresolved P0 finding(s)" in err
        assert "P0-B" in err.split("covered by")[-1]

    def test_unknown_ack_is_reported_not_silent(self, tmp_path, capsys, monkeypatch):
        monkeypatch.delenv(ACK_ENV, raising=False)
        ledger = write_ledger(tmp_path)
        rc = main(["--repo", "hikari", "--ledger", str(ledger), "--ack", "P0-Q"])
        err = capsys.readouterr().err
        assert rc == 0
        assert "matches no ledger entry" in err

    def test_ack_for_other_repos_p0_is_noted(self, tmp_path, capsys, monkeypatch):
        monkeypatch.delenv(ACK_ENV, raising=False)
        ledger = write_ledger(tmp_path)
        rc = main(["--repo", "hikari", "--ledger", str(ledger), "--ack", "P0-A"])
        assert rc == 0
        assert "did not cover an open P0" in capsys.readouterr().err


class TestCliFailClosed:
    def test_missing_ledger_exits_two(self, tmp_path, capsys):
        rc = main(["--repo", "entelecheia", "--ledger", str(tmp_path / "nope.toml")])
        err = capsys.readouterr().err
        assert rc == 2
        assert "not found" in err and "fail-closed" in err

    def test_malformed_ledger_exits_two(self, tmp_path, capsys):
        ledger = write_ledger(tmp_path, "version = 1\n[[p0]\nid = \n")
        rc = main(["--repo", "entelecheia", "--ledger", str(ledger)])
        err = capsys.readouterr().err
        assert rc == 2
        assert "not valid TOML" in err and "fail-closed" in err

    def test_schema_invalid_ledger_exits_two(self, tmp_path, capsys):
        ledger = write_ledger(tmp_path, LEDGER.replace('status = "open"', 'status = "who knows"'))
        rc = main(["--repo", "entelecheia", "--ledger", str(ledger)])
        err = capsys.readouterr().err
        assert rc == 2
        assert "status" in err and "fail-closed" in err

    def test_empty_ledger_exits_two(self, tmp_path, capsys):
        ledger = write_ledger(tmp_path, "version = 1\n")
        rc = main(["--repo", "entelecheia", "--ledger", str(ledger)])
        err = capsys.readouterr().err
        assert rc == 2
        assert "not a pass" in err

    def test_unidentifiable_repo_exits_two(self, tmp_path, capsys, monkeypatch):
        monkeypatch.delenv(ACK_ENV, raising=False)
        ledger = write_ledger(tmp_path)
        rc = main(["--ledger", str(ledger), "--repo-root", str(tmp_path)])
        err = capsys.readouterr().err
        assert rc == 2
        assert "--repo" in err

    def test_unreadable_pr_body_file_exits_two(self, tmp_path, capsys):
        ledger = write_ledger(tmp_path)
        rc = main(["--repo", "hikari", "--ledger", str(ledger),
                   "--pr-body-file", str(tmp_path / "absent.md")])
        assert rc == 2
        assert "pr-body-file unreadable" in capsys.readouterr().err


class TestCliList:
    def test_list_groups_by_status(self, tmp_path, capsys, monkeypatch):
        monkeypatch.delenv(ACK_ENV, raising=False)
        ledger = write_ledger(tmp_path)
        rc = main(["--list", "--ledger", str(ledger)])
        out = capsys.readouterr().out
        assert rc == 0
        assert "open (1):" in out and "closed (1):" in out
        assert "P0-A" in out and "closed by #123" in out
        assert "reviewed 2026-09-13: still present" in out

    def test_render_list_orders_by_opened_at(self, tmp_path, capsys, monkeypatch):
        monkeypatch.delenv(ACK_ENV, raising=False)
        first, second = LEDGER.split("[[p0]]")[1], LEDGER.split("[[p0]]")[2]
        text = (
            'version = 1\n\n[[p0]]'
            + first.replace('id = "P0-A"', 'id = "P0-NEW"')
            .replace('opened_at = "2026-09-10"', 'opened_at = "2026-09-12"')
            + "[[p0]]"
            + second.replace('id = "P0-Z"', 'id = "P0-OLD"')
            .replace('status = "closed"', 'status = "open"')
            .replace('closed_by = "#123"', 'closed_by = ""')
        )
        ledger = write_ledger(tmp_path, text)
        assert main(["--list", "--ledger", str(ledger)]) == 0
        out = capsys.readouterr().out
        assert out.index("P0-OLD") < out.index("P0-NEW")

    def test_render_list_marks_missing_entries(self):
        text = render_list(parse_ledger_text(LEDGER.replace('status = "open"', 'status = "closed"')
                                             .replace('closed_by = ""', 'closed_by = "#1"')))
        assert "open (0):\n  (none)" in text

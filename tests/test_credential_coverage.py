#!/usr/bin/env python3
"""AGENTS red line 1 has four clauses; this file pins which scanner detects each.

The workspace is guarded by **three** credential scanners, and they are not the same
tool — that is deliberate, because they sweep different surfaces with different
exemption rules:

===================  ============================================  ===================
scanner              surface                                       exemptions
===================  ============================================  ===================
``lint/rules_lint``  the surfaces injected into every prompt      placeholders only
``build/gate_cred``  ``.git/config`` remote URLs + sibling trees   placeholder / env-ref
                                                                    / RFC 5737 doc IP
``_tools/cred_scan``  the workspace's pre-commit / pre-archive      ledger registrations
                      scan (not importable here)
===================  ============================================  ===================

The 2026-11-12 cross-repo audit found that the **internal-IPv4** clause had no
detector at all in the middle column, so every path that scanner alone guarded was
blind to it. Fixing that is worth nothing without a pin: a rule deleted silently next
month puts us back where we were. These tests are that pin.

They deliberately do **not** assert that the three scanners agree on everything — they
should not. What they assert is that no clause is unattended, and that the sweep's
verdict stays graded rather than binary (see `test_a_bare_private_literal_is_reported`).
"""

import pytest
from celestia_devtools.build.gate_cred import classify_credential_line
from celestia_devtools.build.gate_sweep import scan_file_credentials
from celestia_devtools.lint.rules_lint import SECRET_PATTERNS, is_placeholder

#: A vector that must come out as a violation; the marker keeps this repo's own sweep
#: clean without weakening the rule (the marker is line-scoped, see the tests below).
ENDPOINT_SAMPLE = "upstream = 10.2.3.4"  # credential-scan: ignore

#: (clause, canonical sample) — every one must be a **violation** for `gate_cred`.
CLAUSES = (("literal-password", 'db_password = "hunter2hunter2"'), ("private-key", "-----BEGIN RSA PRIVATE KEY-----"), ("internal-ipv4", ENDPOINT_SAMPLE), ("provider-token", "token = gho_0123456789abcdefghij"))  # credential-scan: ignore

#: Samples that must NOT be violations anywhere: prose, placeholders, doc addresses.
NOT_VIOLATIONS = ("the scanner refuses a line that names a password", 'db_password = "<your-password>"', 'db_password = "${DB_PASSWORD}"', "callback = 192.0.2.7")

#: What `rules_lint` actually carries. It is a **narrower** scanner than `gate_cred`:
#: its surface is the text injected into every prompt, where PEM blocks and provider
#: tokens do not appear. Pinning the narrow set keeps that blindness deliberate rather
#: than accidental.
RULES_LINT_CLAUSES = ("private-ipv4", "long-hex-secret", "inline-password")

#: A bare RFC 1918 literal is network math far more often than it is a leak.
BARE_LITERALS = ("MASKS = [\"10.0.0.0/8\", \"172.16.0.0/12\"]", "docker0 = 172.17.0.1")

@pytest.mark.parametrize("label", RULES_LINT_CLAUSES)
def test_rules_lint_carries_the_clauses_its_surface_needs(label: str) -> None:
    assert label in [name for name, _ in SECRET_PATTERNS], label

def test_rules_lint_does_not_claim_the_secret_shapes_it_cannot_see() -> None:
    """PEM blocks and provider tokens are `gate_cred`'s job; asserting the gap keeps it honest."""
    labels = {name for name, _ in SECRET_PATTERNS}

    assert ("private-key" not in labels) and ("provider-token" not in labels), labels

@pytest.mark.parametrize("sample", ["10.2.3.4", "192.168.1.9", "172.16.0.1", "172.31.255.254"])
def test_gate_cred_flags_an_internal_address_used_as_an_endpoint(sample: str) -> None:
    """`gate credential-scan` guarded `.git/config` and sibling trees with no such rule."""
    assert classify_credential_line("upstream = " + sample) == "violation", sample
    assert classify_credential_line('url = "http://' + sample + ':8080/x"') == "violation", sample

#: The config shapes an operator actually writes. `\b` does not survive `DB_HOST`
#: (underscore is a word character) and JSON keys put a quote before the colon, so both
#: were silently falling into the non-fatal bucket until an adversarial pass caught it.
ENDPOINT_SHAPES = ('DB_HOST = "10.2.3.4"', '{"host": "10.2.3.4"}', 'DATABASE_URL = "postgresql://10.2.3.4:5432/x"', "ip_addr = 10.2.3.4")  # credential-scan: ignore

@pytest.mark.parametrize("sample", ENDPOINT_SHAPES)
def test_the_config_shapes_operators_actually_write_are_fatal(sample: str) -> None:
    assert classify_credential_line(sample) == "violation", sample

@pytest.mark.parametrize("sample", BARE_LITERALS)
def test_a_bare_private_literal_is_reported_not_fatal(sample: str) -> None:
    """The sweep walks whole source trees; network math must be visible, not fatal.

    A gate that fails on `10.0.0.0/8` gets switched off within a week, and then it
    guards nothing at all.
    """
    assert classify_credential_line(sample) == "report", sample

@pytest.mark.parametrize("clause,sample", CLAUSES)
def test_every_clause_has_a_detector_in_gate_cred(clause: str, sample: str) -> None:
    assert classify_credential_line(sample) == "violation", f"{clause} has no detector: {sample!r}"

@pytest.mark.parametrize("sample", NOT_VIOLATIONS)
def test_nothing_innocent_is_escalated(sample: str) -> None:
    """A gate that cries wolf on documentation gets switched off, so pin the negative side."""
    verdict = classify_credential_line(sample)

    assert verdict != "violation", f"{sample!r} was escalated to {verdict}"

    for label, pattern in SECRET_PATTERNS:
        match = pattern.search(sample)

        if match is None:
            continue

        if match.groups():
            assert is_placeholder(match.group(1)), f"{sample!r} tripped {label}"
        else:
            # The clause patterns (private-ipv4, long-hex) have no capture group, so a
            # sample that reaches here is a real finding, not a placeholder.
            pytest.fail(f"{sample!r} tripped {label}, which has no placeholder channel")

def test_the_union_covers_all_four_clauses() -> None:
    """Red line 1 is satisfied by the pair, not by either scanner alone.

    `gate_cred` is asserted for **all four** on purpose: if it loses one, the other
    scanner does not pick the shape up (that was the original defect), so an `or`
    here would hide exactly the regression this file exists to catch.
    """

    for clause, sample in CLAUSES:
        assert classify_credential_line(sample) == "violation", clause

    # …and the internal-address clause additionally has a second, independent detector.
    line = ENDPOINT_SAMPLE
    assert any((label == "private-ipv4") and pattern.search(line) for label, pattern in SECRET_PATTERNS)

def test_the_inline_opt_out_is_explicit_and_narrow() -> None:
    """A test vector needs a stated exemption, not a magic context.

    Three properties, all pinned: it downgrades to **report** (never to `clean`), it only
    applies when the caller allows it (tests/docs), and it is line-scoped.
    """
    marked = "upstream = 10.2.3.4  # credential-scan: ignore"

    assert classify_credential_line(marked, allow_ignore = True) == "report"
    # Without the caller's permission the marker is inert -- `.git/config` uses `#` too.
    assert classify_credential_line(marked) == "violation"
    # And it excuses only the line it sits on.
    assert classify_credential_line("upstream = 10.2.3.4") == "violation"  # credential-scan: ignore

def test_a_marker_outside_tests_and_docs_does_not_excuse(tmp_path) -> None:
    """The scan decides permission from the path, so `src/` cannot opt out of the gate."""
    source = tmp_path / "src" / "config.py"
    source.parent.mkdir()
    source.write_text('upstream = "10.2.3.4"  # credential-scan: ignore\n', encoding = "utf-8")
    assert scan_file_credentials(source), "a src/ line must stay a finding"

    spec = tmp_path / "tests" / "test_x.py"
    spec.parent.mkdir()
    spec.write_text('upstream = "10.2.3.4"  # credential-scan: ignore\n', encoding = "utf-8")
    assert [f.verdict for f in scan_file_credentials(spec)] == ["report"]

def test_ipv6_link_local_and_ula_are_out_of_scope_on_purpose() -> None:
    """Both devtools scanners are IPv4-only. Pinning the gap beats pretending it is covered."""

    for sample in ("fd00::1", "fe80::1"):
        line = "upstream = " + sample

        assert classify_credential_line(line) == "clean", sample
        assert not any(pattern.search(line) for _, pattern in SECRET_PATTERNS), sample

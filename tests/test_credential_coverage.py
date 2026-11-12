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
blind to half of red line 1. Fixing that is worth nothing without a pin: a rule that
is deleted silently next month puts us back exactly where we were. These tests are
that pin.

They deliberately do **not** assert that the three scanners agree on everything —
they should not. What they assert is that no clause is unattended in either
devtools-side scanner.
"""

import pytest
from celestia_devtools.build.gate_cred import classify_credential_line
from celestia_devtools.lint.rules_lint import SECRET_PATTERNS, is_placeholder

#: (clause, canonical sample). Every one of these must be a violation somewhere.
CLAUSES = (("literal-password", 'db_password = "hunter2hunter2"'), ("private-key", "-----BEGIN RSA PRIVATE KEY-----"), ("internal-ipv4", "upstream = 10.2.3.4"), ("provider-token", "token = gho_0123456789abcdefghij"))

#: Samples that must NOT be violations anywhere: prose, placeholders, doc addresses.
NOT_VIOLATIONS = ("the scanner refuses a line that names a password", 'db_password = "<your-password>"', 'db_password = "${DB_PASSWORD}"', "callback = 192.0.2.7")

def test_rules_lint_still_carries_the_internal_ip_clause() -> None:
    """The CI-side scanner defines private-ipv4 three times, one per RFC 1918 range."""
    labels = [label for label, _ in SECRET_PATTERNS]

    assert labels.count("private-ipv4") == 3, labels

    for sample in ("10.2.3.4", "192.168.1.9", "172.16.0.1"):
        hits = [label for label, pattern in SECRET_PATTERNS if pattern.search("x = " + sample)]
        assert "private-ipv4" in hits, f"{sample} is not caught by rules_lint"

@pytest.mark.parametrize("sample", ["10.2.3.4", "192.168.1.9", "172.16.0.1", "172.31.255.254"])
def test_gate_cred_catches_every_rfc1918_range(sample: str) -> None:
    """`gate credential-scan` guarded `.git/config` and sibling trees with no such rule."""
    assert classify_credential_line("upstream = " + sample) == "violation", sample

@pytest.mark.parametrize("clause,sample", CLAUSES)
def test_every_clause_has_a_detector_in_gate_cred(clause: str, sample: str) -> None:
    assert classify_credential_line(sample) == "violation", f"{clause} has no detector: {sample!r}"

#: What `rules_lint` actually detects. It is a **narrower** scanner than `gate_cred`:
#: its surface is the text injected into every prompt, where PEM blocks and provider
#: tokens do not appear. Pinning the narrow set keeps that blindness deliberate rather
#: than accidental, and `test_the_union_covers_all_four_clauses` proves the delegation
#: is not a hole.
#: `rules_lint` calls the internal-address clause `private-ipv4`; the clause name in
#: `CLAUSES` is `internal-ipv4`. Both spellings are pinned so the mapping cannot drift.
RULES_LINT_CLAUSES = ("private-ipv4", "long-hex-secret", "inline-password")

@pytest.mark.parametrize("label", RULES_LINT_CLAUSES)
def test_rules_lint_carries_the_clauses_its_surface_needs(label: str) -> None:
    assert label in [name for name, _ in SECRET_PATTERNS], label

def test_rules_lint_does_not_claim_the_secret_shapes_it_cannot_see() -> None:
    """PEM blocks and provider tokens are `gate_cred`'s job; asserting the gap keeps it honest."""
    labels = {name for name, _ in SECRET_PATTERNS}

    assert ("private-key" not in labels) and ("provider-token" not in labels), labels

def test_the_union_covers_all_four_clauses() -> None:
    """Red line 1 is satisfied by the pair, not by either scanner alone."""

    for clause, sample in CLAUSES:
        by_gate = classify_credential_line(sample) == "violation"
        by_lint = any(pattern.search(sample) for _, pattern in SECRET_PATTERNS)

        assert by_gate or by_lint, f"{clause} is unattended by both devtools scanners"

@pytest.mark.parametrize("sample", NOT_VIOLATIONS)
def test_nothing_innocent_is_escalated(sample: str) -> None:
    """A gate that cries wolf on documentation gets switched off, so pin the negative side."""
    verdict = classify_credential_line(sample)

    assert verdict != "violation", f"{sample!r} was escalated to {verdict}"

    for label, pattern in SECRET_PATTERNS:
        match = pattern.search(sample)

        if match is None:
            continue

        assert is_placeholder(match.group(1)) if match.groups() else False, f"{sample!r} tripped {label}"

def test_the_two_devtools_scanners_agree_on_the_internal_ip_clause() -> None:
    """Different rules, same verdict — if either side drops the clause this fails.

    This is the seam that mattered: `gate_cred` had no internal-IP rule at all while
    `rules_lint` had three, and each scanner had its own passing tests.
    """

    for sample in ("10.2.3.4", "192.168.1.9", "172.20.1.1"):
        line = "upstream = " + sample
        by_gate = classify_credential_line(line) == "violation"
        by_lint = any((label == "private-ipv4") and pattern.search(line) for label, pattern in SECRET_PATTERNS)

        assert by_gate and by_lint, f"{sample}: gate={by_gate} lint={by_lint}"

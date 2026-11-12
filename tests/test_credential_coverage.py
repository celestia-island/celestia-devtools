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

The samples below are assembled at import time on purpose. A test file that has to
carry a literal internal address would itself need an exemption, and an exemption is
exactly how a gate stops testing what it claims to test.
"""

import pytest
from celestia_devtools.build.gate_cred import classify_credential_line
from celestia_devtools.lint.rules_lint import SECRET_PATTERNS, is_placeholder

#: Assembled, never written out: the scanner sweeps this very file.
TEN = ".".join(("10", "2", "3", "4"))
LAN = ".".join(("192", "168", "1", "9"))
PRIVATE_172 = ".".join(("172", "16", "0", "1"))
PRIVATE_172_HIGH = ".".join(("172", "31", "255", "254"))
DOC_ADDRESS = ".".join(("192", "0", "2", "7"))

#: Every secret-shaped vector is assembled for the same reason as the addresses: this
#: file is swept too, and a fixture that needs an exemption is a fixture that hides.
VALUE = "hunter" + "2hunter2"
PEM_HEADER = "-----BEGIN RSA " + "PRIVATE KEY-----"
PROVIDER_TOKEN = "gho_" + "0123456789abcdefghij"

#: (clause, canonical sample) — every one must be a **violation** for `gate_cred`.
CLAUSES = (("literal-password", 'db_password = "' + VALUE + '"'), ("private-key", PEM_HEADER), ("internal-ipv4", "upstream = " + TEN), ("provider-token", "token = " + PROVIDER_TOKEN))

#: The config shapes an operator actually writes. `\\b` does not survive `DB_HOST`
#: (underscore is a word character) and a prefix like `PGHOST` has no separator at all;
#: both were silently landing in the non-fatal bucket until an adversarial pass.
ENDPOINT_SHAPES = ('DB_HOST = "%s"' % TEN, '{"host": "%s"}' % TEN, 'DATABASE_URL = "postgresql://%s:5432/x"' % TEN, "ip_addr = " + TEN, "PGHOST=" + TEN, 'DBHOST="%s"' % TEN, "ip: " + PRIVATE_172, "mysql://user@%s/db" % TEN, "ssh://git@%s/r.git" % TEN)

#: Samples that must NOT be violations anywhere: prose, placeholders, doc addresses.
NOT_VIOLATIONS = ("the scanner refuses a line that names a password", 'db_password = "<your-password>"', 'db_password = "${DB_PASSWORD}"', "callback = " + DOC_ADDRESS)

#: What `rules_lint` actually carries. It is a **narrower** scanner than `gate_cred`:
#: its surface is the text injected into every prompt, where PEM blocks and provider
#: tokens do not appear. Pinning the narrow set keeps that blindness deliberate.
RULES_LINT_CLAUSES = ("private-ipv4", "long-hex-secret", "inline-password")

#: A bare RFC 1918 literal is network math far more often than it is a leak.
BARE_LITERALS = ('MASKS = ["10.0.0.0/8", "172.16.0.0/12"]', "docker0 = 172.17.0.1")

@pytest.mark.parametrize("label", RULES_LINT_CLAUSES)
def test_rules_lint_carries_the_clauses_its_surface_needs(label: str) -> None:
    assert label in [name for name, _ in SECRET_PATTERNS], label

def test_rules_lint_does_not_claim_the_secret_shapes_it_cannot_see() -> None:
    """PEM blocks and provider tokens are `gate_cred`'s job; asserting the gap keeps it honest."""
    labels = {name for name, _ in SECRET_PATTERNS}

    assert ("private-key" not in labels) and ("provider-token" not in labels), labels

@pytest.mark.parametrize("sample", [TEN, LAN, PRIVATE_172, PRIVATE_172_HIGH])
def test_gate_cred_flags_an_internal_address_used_as_an_endpoint(sample: str) -> None:
    """`gate credential-scan` guarded `.git/config` and sibling trees with no such rule."""
    assert classify_credential_line("upstream = " + sample) == "violation", sample
    assert classify_credential_line('url = "http://' + sample + ':8080/x"') == "violation", sample

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
            pytest.fail(f"{sample!r} tripped {label}, which has no placeholder channel")

def test_the_union_covers_all_four_clauses() -> None:
    """Red line 1 is satisfied by the pair, not by either scanner alone.

    `gate_cred` is asserted for **all four** on purpose: if it loses one, the other
    scanner does not pick the shape up (that was the original defect), so an `or` here
    would hide exactly the regression this file exists to catch.
    """

    for clause, sample in CLAUSES:
        assert classify_credential_line(sample) == "violation", clause

    line = "upstream = " + TEN
    assert any((label == "private-ipv4") and pattern.search(line) for label, pattern in SECRET_PATTERNS)

def test_ipv6_link_local_and_ula_are_out_of_scope_on_purpose() -> None:
    """Both devtools scanners are IPv4-only. Pinning the gap beats pretending it is covered."""

    for sample in ("fd00::1", "fe80::1"):
        line = "upstream = " + sample

        assert classify_credential_line(line) == "clean", sample
        assert not any(pattern.search(line) for _, pattern in SECRET_PATTERNS), sample

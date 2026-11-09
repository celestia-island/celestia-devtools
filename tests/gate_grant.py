"""P0b grant fixtures: the canonical message, the test signer, and the cross-repo key pins.

This module mirrors malkuth's ``src/gate_grant.rs`` grant format (the frozen P0b contract)
so tests and the fake MCP server build exactly what the real gate verifies:

- ``grant_message`` — the canonical message text, field for field;
- ``sign_grant`` — an ed25519 signature over it with the published RFC 8032 §7.1
  test-1 key (the same key malkuth's own tests pin as ``TEST_VERIFY_KEY_HEX``);
- ``MALKUTH_DEFAULT_GATE_VERIFY_KEY_HEX`` — malkuth's embedded *production* gate
  verify key, a PUBLIC key by the 2026-11-08 ruling (the signing seed never leaves
  ``_secure/``). Pinned here for drift detection: if malkuth rotates its embedded key,
  ``tests/test_ed25519_ref.py`` forces a conscious update on this side too.

The reference implementation lives in ``tests/ed25519_ref.py`` (pure-python RFC 8032,
no dependencies — the devtools zero-transitive-deps policy applies to tests too).
"""

from __future__ import annotations
import time
import ed25519_ref as ed

#: malkuth ``src/gate_grant.rs`` ``DEFAULT_GATE_VERIFY_KEY_HEX`` (PUBLIC verify key).
MALKUTH_DEFAULT_GATE_VERIFY_KEY_HEX = "31513b3d792003474bc584120306d5f1be28f1638c0e4351b837522f333e6548"

#: The RFC 8032 §7.1 test-1 seed (a published vector) — malkuth's tests sign with the
#: same seed, so signatures built here verify against a gate keyed to the test pubkey.
GATE_TEST_SEED_HEX = "9d61b19deffd5a60ba844af492ec2cc44449c5697b326919703bac031cae7f60"

#: The public half of the seed above — what a test-scoped gate verifies with
#: (``MALKUTH_GATE_VERIFY_KEY`` set to this value, or the fake server's builtin key).
GATE_TEST_VERIFY_KEY_HEX = "d75a980182b10ab7d54bfed3c964073a0ee172f3daa62325af021a68f707511a"

def grant_message(proposal_id: str, worker_id: str, action: str, pid: int, starttime: int, issued_at: int, budget: int = 0) -> str:
    """The canonical grant message, byte for byte as malkuth's ``grant_message`` builds it.

    ``budget`` is the v2 spend envelope: a drain grant names the maximum drain budget
    the approval may ever be spent with (scepter issues malkuth's service default 30),
    a restart grant carries the documented no-envelope shape (0). ``/v1`` signatures
    are no longer accepted anywhere.
    """

    return f"malkuth-gate-grant/v2\nproposal_id:{proposal_id}\nworker_id:{worker_id}\naction:{action}\npid:{pid}\nstarttime:{starttime}\nissued_at:{issued_at}\nbudget:{budget}\n"

def sign_grant(proposal_id: str, worker_id: str, action: str, pid: int, starttime: int, issued_at: int | None = None, budget: int = 0) -> str:
    """Sign the canonical message with the RFC 8032 test key (128 lowercase hex).

    Drain callers pass ``budget=30`` (the service default envelope scepter signs);
    restart callers keep the ``0`` default. The budget MUST match the ``budget_secs``
    the approve payload carries — malkuth binds the payload to the signed message.
    """

    if issued_at is None:
        issued_at = int(time.time())

    return ed.sign(bytes.fromhex(GATE_TEST_SEED_HEX), grant_message(proposal_id, worker_id, action, pid, starttime, issued_at, budget).encode()).hex()

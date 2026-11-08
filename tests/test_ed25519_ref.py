"""Pin tests/ed25519_ref.py to the RFC 8032 §7.1 test vectors.

The reference implementation is the fake gate's signature checker and the
independent oracle for the grant golden vectors. If these tests fail, the
fixture drifted from the RFC and every signature comparison in the fake is
meaningless — so this file fails hard, with no skips.
"""

from __future__ import annotations
import hashlib
import ed25519_ref as ed
import gate_grant

# RFC 8032 §7.1: (seed, public key, message, signature)
VECTOR_1 = ("9d61b19deffd5a60ba844af492ec2cc44449c5697b326919703bac031cae7f60", "d75a980182b10ab7d54bfed3c964073a0ee172f3daa62325af021a68f707511a", "", "e5564300c360ac729086e2cc806e828a84877f1eb8e5d974d873e065224901555fb8821590a33bacc61e39701cf9b46bd25bf5f0595bbe24655141438e7a100b")
VECTOR_2 = ("4ccd089b28ff96da9db6c346ec114e0f5b8a319f35aba624da8cf6ed4fb8a6fb", "3d4017c3e843895a92b70aa74d1b7ebc9c982ccf2ec4968cc0cd55f12af4660c", "72", "92a009a9f0d4cab8720e820b5f642540a2b27b5416503f8fb3762223ebdb69da085ac1e43e15996e458f3613d0f11d8c387b2eaeb4302aeeb00d291612bb0c00")
VECTOR_3 = ("c5aa8df43f9f837bedb7442f31dcb7b166d38535076f094b85ce3a2e0b4458f7", "fc51cd8e6218a1a38da47ed00230f0580816ed13ba3303ac5deb911548908025", "af82", "6291d657deec24024827e69c3abe01a30ce548a284743a445e3680d7db5ac3ac18ff9b538d16f290ae67f760984dc6594a7c15e9716ed28dc027beceea1ec40a")
RFC_8032_VECTORS = [VECTOR_1, VECTOR_2, VECTOR_3]

def test_rfc8032_public_keys():
    for i, (seed_h, pub_h, _msg, _sig) in enumerate(RFC_8032_VECTORS, 1):
        assert ed.public_key(bytes.fromhex(seed_h)).hex() == pub_h, f"vector {i}"

def test_rfc8032_signatures():
    for i, (seed_h, _pub_h, msg_h, sig_h) in enumerate(RFC_8032_VECTORS, 1):
        sig = ed.sign(bytes.fromhex(seed_h), bytes.fromhex(msg_h))
        assert sig.hex() == sig_h, f"vector {i}"

def test_rfc8032_verify_roundtrip():
    for i, (_seed_h, pub_h, msg_h, sig_h) in enumerate(RFC_8032_VECTORS, 1):
        pub, msg, sig = (bytes.fromhex(x) for x in (pub_h, msg_h, sig_h))
        assert ed.verify(pub, msg, sig), f"vector {i} should verify"
        # negative controls: tampered message and tampered signature
        assert not ed.verify(pub, msg + b"\x00", sig), f"vector {i} tampered msg"
        flipped = sig[:-1] + bytes([sig[-1] ^ 1])
        assert not ed.verify(pub, msg, flipped), f"vector {i} tampered sig"

def test_base_point_is_canonical():
    assert ed._BASE is not None
    bx, by = ed._BASE
    assert bx == 15112221349535400772501151409588531511454012693041857206046113283949847762202
    assert by == 46316835694926478169428394003475163141307993866256225615783033603165251855960
    assert ed.decode_point(ed.encode_point(ed._BASE)) == ed._BASE

def test_malleated_signature_rejected():
    # RFC 8032 allows implementations to reject (or accept) S >= L, but a
    # signature whose S is reduced must never verify against a different hash
    # intent — pin that flipping the R half alone fails verification.
    _seed_h, pub_h, msg_h, sig_h = RFC_8032_VECTORS[0]
    pub, msg, sig = (bytes.fromhex(x) for x in (pub_h, msg_h, sig_h))
    r_flipped = bytes([sig[0] ^ 1]) + sig[1:]
    assert not ed.verify(pub, msg, r_flipped + sig[32:])

# --- the malkuth gate cross-pins (P0b) ---------------------------------------
#
# The two pins below are the cross-repo seam malkuth's gate_grant.rs:232-235
# comment promises: the frozen golden grant vector (built by the same RFC 8032
# reference malkuth's rust tests cite) and the production verify key malkuth
# embeds as DEFAULT_GATE_VERIFY_KEY_HEX. If either side rotates, these tests
# fail on this side too — no silent drift across the repos.
#: malkuth src/gate_grant.rs GOLDEN: the canonical message, its sha256 digest, and the signature.
GOLDEN_MESSAGE = "malkuth-gate-grant/v1\nproposal_id:0123456789abcdef0123456789abcdef\nworker_id:w-alpha-1\naction:restart\npid:4242\nstarttime:987654\nissued_at:1762610000\n"
GOLDEN_DIGEST = "3fc9ff018ff2baba21c744ec2ce4867db493048af4df11bf9ae501af4eef48d5"
GOLDEN_SIG = "17d864db178f5ee0d72a12d6d69a5b3a558027e3679d08ac332f93368142f34382084fd0f26a0af523ef467658bb13fa292bd94cdb0ab363a7a123f3b55c520c"

def test_malkuth_golden_grant_vector():
    message = GOLDEN_MESSAGE
    assert hashlib.sha256(message.encode()).hexdigest() == GOLDEN_DIGEST, "the canonical message text drifted from malkuth's"
    seed, pub = RFC_8032_VECTORS[0][0], RFC_8032_VECTORS[0][1]
    assert ed.sign(bytes.fromhex(seed), message.encode()).hex() == GOLDEN_SIG, "the golden signature drifted (malkuth re-signed with a different key/message)"
    assert ed.verify(bytes.fromhex(pub), message.encode(), bytes.fromhex(GOLDEN_SIG))
    # one byte off the canonical text must break the grant
    assert not ed.verify(bytes.fromhex(pub), message.encode().replace(b"restart", b"restarT"), bytes.fromhex(GOLDEN_SIG))

def test_malkuth_production_verify_key_pin():
    key = gate_grant.MALKUTH_DEFAULT_GATE_VERIFY_KEY_HEX
    assert key == "31513b3d792003474bc584120306d5f1be28f1638c0e4351b837522f333e6548", "malkuth rotated DEFAULT_GATE_VERIFY_KEY_HEX: update tests/gate_grant.py consciously"
    assert len(bytes.fromhex(key)) == 32
    assert ed.decode_point(bytes.fromhex(key)) is not None, "the pinned production verify key is not a valid ed25519 point"
    assert key != gate_grant.GATE_TEST_VERIFY_KEY_HEX, "the production key must never silently become the published test key"

"""RFC 8032 Ed25519 reference implementation (test-only).

This module exists for exactly two reasons:

1. The fake malkuth gate (``fake_mcp_server.py``) must mirror the *real*
   gate's signature check, and ``celestia-devtools`` keeps a zero-transitive
   dependency policy — so there is no ``cryptography``/``pynacl`` to lean on.
2. It is the independent oracle the golden grant vectors are cross-pinned
   against (the other two spellings live in malkuth's Rust tests and scepter's
   signing path).

It follows the RFC 8032 reference pseudocode directly (no attempt at speed:
a verify takes a few milliseconds, which is fine for a test fixture). Every
function here was checked against the RFC 8032 §7.1 test vectors by
``tests/test_ed25519_ref.py`` — if you change anything, re-run it.

Not for production use. Never used outside ``tests/``.
"""

from __future__ import annotations
import hashlib
__all__ = ["public_key", "sign", "verify", "decode_point", "encode_point", "sha512"]

# ── field arithmetic: p = 2^255 - 19, little-endian scalars ──────────────

P = (2 ** 255) - 19
L = (2 ** 252) + 27742317777372353535851937790883648493  # group order
D = (-121665 * pow(121666, P - 2, P)) % P
SQRT_M1 = pow(2, (P - 1) // 4, P)

def _inv(x: int) -> int:
    return pow(x, P - 2, P)

def _recover_x(y: int, sign: int) -> int:
    if y >= P:
        return None

    x2 = ((y * y) - 1) * _inv((D * y * y) + 1) % P

    if x2 == 0:
        if sign:
            return None

        return 0

    x = pow(x2, (P + 3) // 8, P)

    if (((x * x) - x2) % P) != 0:
        x = x * SQRT_M1 % P

    if (((x * x) - x2) % P) != 0:
        return None

    if (x & 1) != sign:
        x = P - x

    return x

# ── (de)compression ──────────────────────────────────────────────────────

_BY = (1 << 255) - 1

def encode_point(pt: tuple[int, int]) -> bytes:
    x, y = pt
    return ((y | ((x & 1) << 255))).to_bytes(32, "little")

def decode_point(s: bytes) -> tuple[int, int]:
    if len(s) != 32:
        return None

    y = int.from_bytes(s, "little")
    sign = y >> 255
    y &= _BY
    x = _recover_x(y, sign)

    if x is None:
        return None

    # RFC 8032 on-curve check: -x^2 + y^2 - 1 - d*x^2*y^2 == 0 (mod p).
    if not (0 <= x < P) or ((((-x * x) + (y * y) - 1 - (D * x * x * y * y)) % P) != 0):
        return None

    return (x, y)

# The standard Ed25519 base point B (y = 4/5 mod p, x even), compressed as
# 0x58 followed by 0x66 bytes — the canonical constant from RFC 8032 §5.
_BASE = decode_point(bytes.fromhex("5866666666666666666666666666666666666666666666666666666666666666"))

def _edwards_add(p: tuple[int, int], q: tuple[int, int]) -> tuple[int, int]:
    x1, y1 = p
    x2, y2 = q
    x3 = ((x1 * y2) + (x2 * y1)) * _inv(1 + (D * x1 * x2 * y1 * y2))
    y3 = ((y1 * y2) + (x1 * x2)) * _inv(1 - (D * x1 * x2 * y1 * y2))
    return (x3 % P, y3 % P)

def _scalarmult(pt: tuple[int, int], e: int) -> tuple[int, int]:
    if ((e % L) == 0) or (pt is None):
        return None

    q = (0, 1)

    while e > 0:
        if e & 1:
            q = _edwards_add(q, pt)

        pt = _edwards_add(pt, pt)
        e >>= 1

    return q

# ── RFC 8032 API ─────────────────────────────────────────────────────────

def sha512(data: bytes, half: bool = False) -> int:
    d = hashlib.sha512(data).digest()
    return int.from_bytes(d[:32] if half else d, "little")

def secret_expand(seed: bytes) -> tuple[int, bytes]:
    if len(seed) != 32:
        raise ValueError("seed must be 32 bytes")

    h = hashlib.sha512(seed).digest()
    a = int.from_bytes(h[:32], "little")
    a &= (1 << 254) - 8
    a |= 1 << 254
    return a, h[32:]

def public_key(seed: bytes) -> bytes:
    a, _ = secret_expand(seed)
    return encode_point(_scalarmult(_BASE, a))

def sign(seed: bytes, msg: bytes) -> bytes:
    a, prefix = secret_expand(seed)
    public = encode_point(_scalarmult(_BASE, a))
    r = sha512(prefix + msg) % L
    rp = encode_point(_scalarmult(_BASE, r))
    k = sha512(rp + public + msg) % L
    s = (r + (k * a)) % L
    return rp + s.to_bytes(32, "little")

def verify(public: bytes, msg: bytes, sig: bytes) -> bool:
    if (len(public) != 32) or (len(sig) != 64):
        return False

    apoint = decode_point(public)
    rpoint = decode_point(sig[:32])

    if (apoint is None) or (rpoint is None):
        return False

    s = int.from_bytes(sig[32:], "little")

    if s >= L:
        return False

    k = sha512(sig[:32] + public + msg) % L
    left = _scalarmult(_BASE, s)
    right = _edwards_add(rpoint, _scalarmult(apoint, k))
    return (left is not None) and (right is not None) and (encode_point(left) == encode_point(right))

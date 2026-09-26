"""Pure-Python Ed25519 (RFC 8032) — verification for counsel decisions.

REVIEWER-SIDE NOTE: signing happens on the REVIEWER's own infrastructure,
never on this machine. This module ships the sign() routine only so counsel
(and this package's tests, as fixtures) can produce signatures with a
reference implementation. The controller itself calls verify() only.

Signatures bind decision fields to a registered reviewer key. The registry,
controller code and caller integration remain trusted; signatures alone do
not prevent a privileged local process from replacing those trust inputs.

Stdlib only (hashlib.sha512). Signing is not constant-time and is provided
for fixtures/reference interoperability; production signing requires an
appropriate reviewer-side implementation and key custody. This module is
not a certified cryptographic implementation.
"""

from __future__ import annotations

import hashlib
import os

# ---------------------------------------------------------------------------
# Curve parameters (RFC 8032, Section 5.1).
# ---------------------------------------------------------------------------

_Q = 2 ** 255 - 19
_L = 2 ** 252 + 27742317777372353535851937790883648493
_D = (-121665 * pow(121666, _Q - 2, _Q)) % _Q
_IDENTITY = (0, 1, 1, 0)  # extended coordinates (x, y, z, t)


def _inv(x: int) -> int:
    return pow(x, _Q - 2, _Q)


def _xrecover(y: int) -> int:
    xx = (y * y - 1) * _inv(_D * y * y + 1) % _Q
    x = pow(xx, (_Q + 3) // 8, _Q)
    if (x * x - xx) % _Q != 0:
        x = (x * pow(2, (_Q - 1) // 4, _Q)) % _Q
    if x & 1:
        x = _Q - x
    return x


def _edwards_add(P: tuple, Q: tuple) -> tuple:
    x1, y1, z1, t1 = P
    x2, y2, z2, t2 = Q
    a = (y1 - x1) * (y2 - x2) % _Q
    b = (y1 + x1) * (y2 + x2) % _Q
    c = t1 * 2 * _D * t2 % _Q
    d_ = z1 * 2 * z2 % _Q
    e = (b - a) % _Q
    f = (d_ - c) % _Q
    g = (d_ + c) % _Q
    h = (b + a) % _Q
    return (e * f % _Q, g * h % _Q, f * g % _Q, e * h % _Q)


def _scalarmult(P: tuple, e: int) -> tuple:
    Q = _IDENTITY
    for i in reversed(range(e.bit_length())):
        Q = _edwards_add(Q, Q)
        if (e >> i) & 1:
            Q = _edwards_add(Q, P)
    return Q


def _encodepoint(P: tuple) -> bytes:
    x, y, z, _t = P
    zi = _inv(z)
    x = x * zi % _Q
    y = y * zi % _Q
    return ((y | ((x & 1) << 255))).to_bytes(32, "little")


def _decodepoint(s: bytes) -> tuple:
    if len(s) != 32:
        raise ValueError("point must be 32 bytes")
    y = int.from_bytes(s, "little") & ((1 << 255) - 1)
    sign = s[31] >> 7
    # RFC 8032 section 5.1.3: noncanonical y and negative zero fail decoding.
    if y >= _Q:
        raise ValueError("noncanonical point coordinate")
    x = _xrecover(y)
    if x == 0 and sign:
        raise ValueError("noncanonical point sign")
    if (x & 1) != sign:
        x = _Q - x
    P = (x, y, 1, x * y % _Q)
    # On-curve check: -x^2 + y^2 = 1 + d*x^2*y^2
    if (-x * x + y * y - 1 - _D * x * x % _Q * y * y) % _Q != 0:
        raise ValueError("point not on curve")
    return P


_BASE_POINT = _decodepoint(bytes.fromhex(
    "5866666666666666666666666666666666666666666666666666666666666666"))


def _sha512(data: bytes) -> bytes:
    return hashlib.sha512(data).digest()


def _clamp(h: bytes) -> int:
    a = int.from_bytes(h[:32], "little")
    a &= (1 << 254) - 8
    a |= 1 << 254
    return a


def derive_public_key(secret_seed: bytes) -> bytes:
    """Ed25519 public key from a 32-byte secret seed (reviewer-side)."""
    if len(secret_seed) != 32:
        raise ValueError("secret seed must be 32 bytes")
    a = _clamp(_sha512(secret_seed))
    return _encodepoint(_scalarmult(_BASE_POINT, a))


def generate_keypair() -> tuple[bytes, bytes]:
    """(secret_seed, public_key). REVIEWER-SIDE ONLY — never on this machine
    in production; tests use it for fixtures."""
    seed = os.urandom(32)
    return seed, derive_public_key(seed)


def sign(secret_seed: bytes, message: bytes) -> bytes:
    """Ed25519 signature. REVIEWER-SIDE ONLY."""
    if len(secret_seed) != 32:
        raise ValueError("secret seed must be 32 bytes")
    h = _sha512(secret_seed)
    a = _clamp(h)
    prefix = h[32:]
    r = int.from_bytes(_sha512(prefix + message), "little") % _L
    R = _encodepoint(_scalarmult(_BASE_POINT, r))
    A = _encodepoint(_scalarmult(_BASE_POINT, a))
    S = (r + int.from_bytes(_sha512(R + A + message), "little") * a) % _L
    return R + S.to_bytes(32, "little")


def _decode_public_key(public_key: bytes) -> tuple:
    point = _decodepoint(public_key)
    identity = _encodepoint(_IDENTITY)
    # This authority-key profile requires a nonidentity prime-order key.
    # Generated Ed25519 public keys satisfy it; low-order and mixed-order
    # registry entries must never become signature authorities.
    if (_encodepoint(point) == identity or
            _encodepoint(_scalarmult(point, _L)) != identity):
        raise ValueError("public key must be nonidentity and prime order")
    return point


def is_valid_public_key(public_key: bytes) -> bool:
    """Validate a reviewer key before recording it as a trusted authority."""
    try:
        _decode_public_key(public_key)
        return True
    except Exception:
        return False


def verify(public_key: bytes, message: bytes, signature: bytes) -> bool:
    """Ed25519 verification with strict authority-key validation.

    Canonical decoding follows RFC 8032 section 5.1.3. Public keys must be
    nonidentity and prime order. The signature R is canonically decoded;
    the existing strong verification equation is unchanged.
    """
    try:
        if len(public_key) != 32 or len(signature) != 64:
            return False
        A = _decode_public_key(public_key)
        R = _decodepoint(signature[:32])
        S = int.from_bytes(signature[32:], "little")
        if S >= _L:
            return False
        h = int.from_bytes(_sha512(signature[:32] + public_key + message),
                           "little")
        lhs = _scalarmult(_BASE_POINT, S)
        rhs = _edwards_add(R, _scalarmult(A, h))
        return _encodepoint(lhs) == _encodepoint(rhs)
    except Exception:
        return False

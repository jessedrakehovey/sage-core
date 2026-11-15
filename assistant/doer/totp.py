"""totp.py - the six digits of an authenticator app (RFC 6238 over RFC 4226), standard library only (2026-11-14).

Replaces the pyotp package, which login_fill.py used for exactly this. Takes what a login vault entry holds: an
`otpauth://totp/...?secret=...` address (its algorithm, digits and period are honoured) or the bare base32 secret.

    now("JBSWY3DPEHPK3PXP")                       -> "492039"
    now("otpauth://totp/Site:me?secret=...&digits=8&algorithm=SHA256&period=60")
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import struct
import time
from urllib.parse import parse_qs, urlparse

ALGOS = {"SHA1": hashlib.sha1, "SHA256": hashlib.sha256, "SHA512": hashlib.sha512}


def parse(uri_or_secret: str) -> dict:
    """-> {secret: bytes, digits, period, algo}. Raises ValueError on anything that is not a time-based secret."""
    s = str(uri_or_secret or "").strip()
    digits, period, algo = 6, 30, "SHA1"
    if s.lower().startswith("otpauth://"):
        u = urlparse(s)
        if u.netloc.lower() != "totp":
            raise ValueError("only time-based codes (otpauth://totp/) are supported")
        q = {k.lower(): v[0] for k, v in parse_qs(u.query).items()}
        s = q.get("secret", "")
        digits = int(q.get("digits", digits))
        period = int(q.get("period", period))
        algo = q.get("algorithm", algo).upper().replace("-", "")
    if algo not in ALGOS:
        raise ValueError(f"unsupported algorithm {algo}")
    if not 6 <= digits <= 10 or period <= 0:
        raise ValueError("digits or period out of range")
    b32 = "".join(s.split()).replace("-", "").upper()
    if not b32:
        raise ValueError("no secret")
    try:
        key = base64.b32decode(b32 + "=" * (-len(b32) % 8))
    except Exception:
        raise ValueError("the secret is not base32") from None
    return {"secret": key, "digits": digits, "period": period, "algo": algo}


def code(key: bytes, counter: int, digits: int = 6, algo: str = "SHA1") -> str:
    """RFC 4226: the HOTP value for one counter."""
    mac = hmac.new(key, struct.pack(">Q", counter), ALGOS[algo]).digest()
    off = mac[-1] & 0x0F
    num = struct.unpack(">I", mac[off:off + 4])[0] & 0x7FFFFFFF
    return str(num % (10 ** digits)).zfill(digits)


def now(uri_or_secret: str, at: float | None = None) -> str:
    """The code that is valid at `at` (default: this moment)."""
    p = parse(uri_or_secret)
    return code(p["secret"], int((time.time() if at is None else at) // p["period"]), p["digits"], p["algo"])

"""totp.py (2026-11-14): the authenticator code, standard library only, in place of the pyotp package.

1. RFC 6238 appendix B: every published value for SHA1, SHA256 and SHA512.
2. What a login vault entry holds: a bare base32 secret (any case, spaces, no padding) or an otpauth:// address whose
   digits, period and algorithm are honoured.
3. Refusals: nothing, a counter-based (hotp) address, a secret that is not base32.
4. Where the old package is still installed, the same answers as it gave.
   python assistant/tests/test_totp.py
"""
import base64
import calendar
import hashlib
import sys
import time
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")
ROOT = Path(__file__).resolve().parent.parent
FAILS = []
sys.path.insert(0, str(ROOT / "doer"))
import totp  # noqa: E402


def check(name, cond, extra=""):
    print(("ok   " if cond else "FAIL ") + name + (f"  {extra}" if extra and not cond else ""))
    if not cond:
        FAILS.append(name)


def b32(raw):
    return base64.b32encode(raw).decode()


K1, K256, K512 = b32(b"12345678901234567890"), b32(b"12345678901234567890123456789012"), b32(b"1234567890123456789012345678901234567890123456789012345678901234")
# the RFC's table by its UTC date column (the seconds column is ten bare digits a row, which core_sync reads as phone numbers)
ROWS = [((1970, 1, 1, 0, 0, 59), "94287082", "46119246", "90693936"),
        ((2005, 3, 18, 1, 58, 29), "07081804", "68084774", "25091201"),
        ((2005, 3, 18, 1, 58, 31), "14050471", "67062674", "99943326"),
        ((2009, 2, 13, 23, 31, 30), "89005924", "91819424", "93441116"),
        ((2033, 5, 18, 3, 33, 20), "69279037", "90698825", "38618901"),
        ((2603, 10, 11, 11, 33, 20), "65353130", "77737706", "47863826")]
for when, a, b, c in ROWS:
    t = calendar.timegm(when + (0, 0, 0))
    check(f"RFC 6238 SHA1 at {t}", totp.now(f"otpauth://totp/x?secret={K1}&digits=8", t) == a)
    check(f"RFC 6238 SHA256 at {t}", totp.now(f"otpauth://totp/x?secret={K256}&digits=8&algorithm=SHA256", t) == b)
    check(f"RFC 6238 SHA512 at {t}", totp.now(f"otpauth://totp/x?secret={K512}&digits=8&algorithm=SHA512", t) == c)

SEC = "JBSWY3DPEHPK3PXP"
check("six digits by default, leading zeros kept", len(totp.now(SEC, 0)) == 6 and totp.now(SEC, 0).isdigit())
check("a lower-case, spaced secret is the same secret", totp.now("jbsw y3dp ehpk 3pxp", 1e9) == totp.now(SEC, 1e9))
check("a secret without padding decodes", totp.now("GEZDGNBVGY3TQOJQGEZDGNBV", 1e9).isdigit())
check("the code holds for its 30 seconds and then changes", totp.now(SEC, 60) == totp.now(SEC, 89) and totp.now(SEC, 60) != totp.now(SEC, 90))
check("a 60-second period from the address", totp.now(f"otpauth://totp/x?secret={SEC}&period=60", 60) == totp.now(f"otpauth://totp/x?secret={SEC}&period=60", 119))
for bad in ("", "otpauth://hotp/x?secret=" + SEC, "not base32 !!!", f"otpauth://totp/x?secret={SEC}&algorithm=MD5"):
    try:
        totp.now(bad)
        check(f"refuses {bad[:24]!r}", False)
    except ValueError:
        check(f"refuses {bad[:24]!r}", True)
check("login_fill.py no longer imports the package", "import pyotp" not in (ROOT / "doer" / "login_fill.py").read_text(encoding="utf-8"))
check("requirements.txt no longer lists it", "pyotp" not in (ROOT / "requirements.txt").read_text(encoding="utf-8"))

try:
    import pyotp
except Exception:
    pyotp = None
if pyotp:
    t = time.time()
    check("same as pyotp: bare secret", totp.now(SEC, t) == pyotp.TOTP(SEC).at(t))
    uri = pyotp.TOTP(SEC).provisioning_uri(name="me@x.test", issuer_name="Site")
    check("same as pyotp: otpauth address", totp.now(uri, t) == pyotp.parse_uri(uri).at(t))
    uri2 = pyotp.TOTP("GEZDGNBVGY3TQOJQ", digits=8, interval=60, digest=hashlib.sha256).provisioning_uri(name="a", issuer_name="b")
    check("same as pyotp: digits, period and algorithm from the address", totp.now(uri2, t) == pyotp.parse_uri(uri2).at(t))

print()
print("ALL PASSED" if not FAILS else f"{len(FAILS)} FAILED: {FAILS}")
sys.exit(1 if FAILS else 0)

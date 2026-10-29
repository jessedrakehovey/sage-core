"""account.py - which Claude subscription the agent's model turns run on.

the owner, 2026-10-05: "use the stored usage key on property mission control for the 5X claude
account". The machine keeps one long-lived token per Claude account, encrypted with Windows
DPAPI (current-user scope), in ~/.claude/accounts.dpapi.json. That store is machine-wide
plumbing written by the account switcher; it is read here, never written.

profile.json `claude_account` names the stored account (e.g. "max5x"). The token is
decrypted in this process and handed to the model subprocess through its environment only.
It is never printed, logged, written to disk or passed on a command line. With no
`claude_account`, or if the store or the name is missing, the turn runs on the machine's
default token and the reason is returned so the caller can log it (the reason, not a token).

`claude_account_fallback` names a second stored account. Every model runner (the brain, the
builder, the doer) runs a turn that hit the usage limit once more on it (the owner 2026-10-26:
"the doer and builder need to be able to switch too").

  python assistant/agent/account.py        # which account, and whether it resolves (no secret shown)
"""
from __future__ import annotations

import ctypes
import ctypes.wintypes as wintypes
import json
import os
import re
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import core  # noqa: E402

STORE = Path(os.environ["AGENT_ACCOUNT_STORE"]) if os.environ.get("AGENT_ACCOUNT_STORE") \
    else Path.home() / ".claude" / "accounts.dpapi.json"
VAR = "CLAUDE_CODE_OAUTH_TOKEN"
METERED = ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_BASE_URL")
LIMIT = re.compile(r"(session|usage|rate) limit|hit your limit|limit reached|too many requests", re.I)   # same as held.py


def fallback() -> str:
    """The stored account a turn that hit the usage limit runs once more on ('' = none)."""
    return str(core.profile().get("claude_account_fallback") or "").strip()


def is_limit(*texts) -> bool:
    return any(LIMIT.search(str(t or "")) for t in texts)


# an account REFUSED, not limited (2026-10-27 16:55-17:52: "Your organization has disabled Claude subscription
# access for Claude Code" on max5x, three turns in a row; the code called it a one-off, never tried max20x, and
# the owner's stop request, his plant photo and a voice note were all answered with "didn't go through"). A refusal
# puts that account out for REFUSAL_WAIT_S; the next account runs the same turn; both out = the question is kept.
REFUSAL = re.compile(r"organization has disabled|subscription access|not logged in|please (?:run )?/?login|invalid (?:api key|token)|"
                     r"unauthori[sz]ed|authentication (?:error|failed)|token (?:is |has )?expired|\b40[13]\b|oauth token", re.I)
REFUSAL_WAIT_S = 15 * 60


def is_refusal(*texts) -> bool:
    return any(REFUSAL.search(str(t or "")) for t in texts) and not is_limit(*texts)


def out_until(error: str, now: float | None = None) -> float:
    """The epoch until which the account that answered `error` is out: a limit's reset, a refusal's wait, else 0."""
    until = limit_reset(error, now)
    if until:
        return until
    if is_refusal(error):
        return (now or time.time()) + REFUSAL_WAIT_S
    return 0.0


# ------------------------------------------------------------------ which account has room (the reflex, 2026-10-27)
# A limit seen on one account is remembered with its reset time, so the NEXT turn goes straight to the account
# that has room instead of paying for a refused call first (2026-10-26: every turn for two hours tried max5x,
# was refused, then ran on max20x; the doer and the builder tried max5x alone and stopped).
LIMITS = core.DATA / "limits.json"
_RESET = re.compile(r"resets?\s+(?:at\s+)?(\d{1,2})(?::(\d{2}))?\s*([ap])\.?m", re.I)
UNKNOWN_WAIT_S = 30 * 60


def limit_reset(error: str, now: float | None = None) -> float:
    """The epoch the limit named in `error` resets (today's or tomorrow's clock time); 0 when it is not a limit."""
    e = str(error or "")
    if not LIMIT.search(e):
        return 0.0
    now = now or time.time()
    m = _RESET.search(e)
    if not m:
        return now + UNKNOWN_WAIT_S
    h, mi, ap = int(m.group(1)), int(m.group(2) or 0), m.group(3).lower()
    if ap == "p" and h != 12:
        h += 12
    if ap == "a" and h == 12:
        h = 0
    t = time.localtime(now)
    reset = time.mktime((t.tm_year, t.tm_mon, t.tm_mday, h, mi, 0, 0, 0, -1))
    if reset <= now - 120:
        reset += 86400
    return reset


def _primary() -> str:
    return str(core.profile().get("claude_account") or "").strip() or "default"


def note_limit(name: str | None, error: str, now: float | None = None) -> float:
    """Remember that `name` (the account a turn ran on; None = the profile's account) is at its limit until the
    reset the error names."""
    until = out_until(error, now)
    if not until:
        return 0.0
    d = core.load(LIMITS, {}) or {}
    d[str(name or _primary())] = {"until": until, "seen": core.stamp(), "error": str(error or "")[:160],
                                  "kind": "refused" if is_refusal(error) else "limit"}
    core.save(LIMITS, d)
    return until


def clear(name: str | None) -> bool:
    """Forget that `name` is out (a probe just ran on it)."""
    d = core.load(LIMITS, {}) or {}
    if str(name or _primary()) not in d:
        return False
    d.pop(str(name or _primary()), None)
    core.save(LIMITS, d)
    return True


def out_now(now: float | None = None) -> dict:
    """{account: entry} for every account known to be out right now."""
    now = now or time.time()
    d = core.load(LIMITS, {}) or {}
    out = {}
    for k, v in d.items():
        try:
            if float((v or {}).get("until") or 0) > now:
                out[k] = v
        except (TypeError, ValueError):
            continue
    return out


def probe(name: str | None, timeout: int = 90) -> tuple[bool, str]:
    """One cheap headless turn on `name` (haiku, one turn) -> (ok, note). The reflex's check that an account that
    refused a turn is back; the note never carries a token."""
    import subprocess
    e, note = env(name)
    cli = os.path.expandvars(r"%APPDATA%\npm\claude.cmd")            # the same CLI the brain runs (brain.CLAUDE)
    if not Path(cli).exists():
        cli = "claude"
    try:
        p = subprocess.run([cli, "-p", "reply with the single word ok", "--model", "haiku", "--max-turns", "1"],
                           env=e, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=timeout,
                           stdin=subprocess.DEVNULL, creationflags=getattr(core, "NO_WINDOW", 0))
    except (OSError, subprocess.TimeoutExpired) as ex:
        return False, f"{note}: {type(ex).__name__}"
    out = (p.stdout or "").strip()
    err = (p.stderr or "").strip()
    if p.returncode == 0 and out:
        return True, f"{note}: ok"
    return False, f"{note}: " + (err or out or f"exit {p.returncode}")[:160]


def limited_until(name: str | None) -> float:
    """The epoch until which `name` is known to be at its limit (0 = has room as far as we know)."""
    try:
        return float(((core.load(LIMITS, {}) or {}).get(str(name or _primary())) or {}).get("until") or 0)
    except (TypeError, ValueError):
        return 0.0


def order(now: float | None = None) -> list:
    """The accounts to try, in order: the profile's account (as None: env(None) resolves it) unless it is
    known-limited, then the fallback by name. Every runner (the brain, the doer, the builder) walks this list once."""
    now = now or time.time()
    fb = fallback() or None
    slots = [None] + ([fb] if fb else [])
    free = [n for n in slots if limited_until(n) <= now]
    return free + [n for n in slots if n not in free]


class _Blob(ctypes.Structure):
    _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_char))]


def _unprotect(hexblob: str) -> str:
    raw = bytes.fromhex(hexblob)
    buf = ctypes.create_string_buffer(raw, len(raw))
    src = _Blob(len(raw), ctypes.cast(buf, ctypes.POINTER(ctypes.c_char)))
    dst = _Blob()
    if not ctypes.windll.crypt32.CryptUnprotectData(ctypes.byref(src), None, None, None, None, 0x01, ctypes.byref(dst)):
        raise OSError("DPAPI could not decrypt (only the Windows user that stored it can)")
    out = ctypes.string_at(dst.pbData, dst.cbData)
    ctypes.windll.kernel32.LocalFree(dst.pbData)
    return out.decode("utf-8")


def resolve(name: str | None = None) -> tuple[str | None, str]:
    """-> (token or None, note). The note never contains the token. `name` overrides the
    profile's account (the fallback account when the first is at its usage limit)."""
    name = str(name if name is not None else core.profile().get("claude_account") or "").strip()
    if not name:
        return None, "default account (no claude_account in profile.json)"
    try:
        acct = (json.loads(STORE.read_text(encoding="utf-8")).get("accounts") or {}).get(name)
    except (OSError, ValueError) as e:
        return None, f"default account (account store unreadable: {type(e).__name__})"
    if not acct or not acct.get("blob"):
        return None, f"default account (no stored account named {name!r})"
    try:
        return _unprotect(acct["blob"]), f"account {name}"
    except (OSError, ValueError) as e:
        return None, f"default account ({name}: {type(e).__name__})"


def env(name: str | None = None) -> tuple[dict, str]:
    """The environment for a model turn, and a note saying which account it is on."""
    e = {**os.environ, "WV_HEADLESS": "1"}
    for k in METERED:                                     # a stray API key would silently mean metered billing
        e.pop(k, None)
    tok, note = resolve(name)
    if tok:
        e[VAR] = tok
    return e, note


if __name__ == "__main__":
    tok, note = resolve()
    print(("RESOLVED " if tok else "FALLBACK ") + note)

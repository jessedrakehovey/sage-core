"""held.py - questions kept while the model's account is at its usage limit, answered when it resets.

Sage's own build request (nightly review, 2026-10-15): "the owner asked three times and got the same
useless line six times. Nothing was saved, so the lens rental check, the delivery-reminder request
and the Korean grass question were all lost." The builder could not take it (protected core), and
on 2026-10-16 18:58 and 19:15 it happened again: 'You've hit your session limit · resets 7:50pm'.

What happens now, in code around the unchanged brain:
  - the failure line names the reset time and says the question is kept, no need to resend;
  - the message (its place, thread, text) goes into data/agent/held.json;
  - until the reset no other message spends a model turn: it is kept too, with one short line;
  - the bridge's minute tick re-runs each held message once the limit has reset (plus a grace
    minute), in the place it was asked; a limit on the retry re-holds it with the new reset time,
    MAX_ATTEMPTS times at most, then one honest line and the row closes.

A limit is recorded with the fallback account it was seen under (profile.json
`claude_account_fallback`; brain.py already tried that account before the limit got here). If
the fallback has changed since (the owner named max20x on 2026-10-26 while max5x was held until
7:50 pm and Sage kept saying "out of steam"), the old limit no longer gates anything and its
held messages are due at once.

The model never sees any of this; nothing here sends outside the household.
"""
from __future__ import annotations

import re
import secrets
import time
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent))
import core  # noqa: E402

STORE = core.DATA / "held.json"
GRACE_S = 90                 # the reset minute itself can still refuse; wait a little past it
MAX_ATTEMPTS = 3
UNKNOWN_WAIT_S = 30 * 60     # a limit message with no reset time: try again in half an hour

_LIMIT = re.compile(r"(session|usage|rate) limit|hit your limit|limit reached|too many requests", re.I)
_RESET = re.compile(r"resets?\s+(?:at\s+)?(\d{1,2})(?::(\d{2}))?\s*([ap])\.?m", re.I)


def limit_reset(error: str, now: float | None = None) -> float | None:
    """The epoch time the limit named in `error` resets, or None when the error is not a limit.
    'resets 7:50pm' is today's 7:50 pm, or tomorrow's if that has already passed."""
    e = str(error or "")
    if not _LIMIT.search(e):
        return None
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
    if reset <= now - 120:                                  # already behind us: it means tomorrow
        reset += 86400
    return reset


def fmt(ts: float) -> str:
    t = time.localtime(ts)
    h = t.tm_hour % 12 or 12
    day = "" if time.localtime().tm_yday == t.tm_yday else (" tomorrow" if t.tm_yday == time.localtime().tm_yday + 1 else time.strftime(" on %a", t))
    return f"{h}:{t.tm_min:02d} {'am' if t.tm_hour < 12 else 'pm'}{day}"


def _state() -> dict:
    d = core.load(STORE, {})
    return d if isinstance(d, dict) else {}


def _fallback() -> str:
    return str(core.profile().get("claude_account_fallback") or "").strip()


def blocked_until() -> float:
    """The reset time of the limit currently in force, or 0. A limit seen under another fallback
    setting is not in force: the account now named may have room."""
    d = _state()
    if str(d.get("fb") or "") != _fallback():
        return 0.0
    try:
        return float(d.get("until") or 0)
    except (TypeError, ValueError):
        return 0.0


def hold(msg: dict, text: str, until: float, reason: str = "limit") -> dict:
    """Keep this message for a retry at `until`. A retry that is held again carries its attempt
    count in msg['_attempts']; past MAX_ATTEMPTS nothing is kept and the row returned is marked
    'given_up' so the caller can say so. `reason` is 'limit' or 'refused' (account.is_refusal:
    the account turned the turn away, 2026-10-27); it picks the owner's line, nothing else."""
    attempts = int(msg.get("_attempts") or 0) + 1
    row = {"id": "h" + secrets.token_hex(3), "ts": core.stamp(), "until": until, "attempts": attempts,
           "text": str(text or ""), "msg": {k: msg.get(k) for k in ("channel", "space", "thread", "convo", "msg_name", "direct", "why")},
           "person_key": (msg.get("person") or {}).get("key", ""), "fb": _fallback(), "reason": reason}
    if attempts > MAX_ATTEMPTS:
        row["status"] = "given_up"
        return row
    d = _state()
    if str(d.get("fb") or "") != row["fb"]:
        d["until"] = 0                                   # an old limit from another fallback setting is gone
    d["fb"] = row["fb"]
    d["until"] = max(float(d.get("until") or 0), until)
    d["reason"] = reason
    d.setdefault("rows", []).append(row)
    core.save(STORE, d)
    return row


def reason_in_force() -> str:
    """'limit' or 'refused': why the block that blocked_until() reports is there."""
    return str(_state().get("reason") or "limit")


def line(until: float, again: bool = False, name: str = "", reason: str = "") -> str:
    when = fmt(until)
    reason = reason or reason_in_force()
    both = ""
    try:                      # the truth about WHY (2026-10-26: "There is no session limit!!! Use the 20x token")
        import account
        fb = account.fallback()
        if fb and account.limited_until(fb) > time.time():
            both = " (both of my accounts are at their limit)" if reason == "limit" else " (neither of my accounts will take a turn)"
    except Exception:  # noqa: BLE001
        pass
    if reason == "refused":
        # not a limit: the account turned the turn away (a sign-in gone, Claude Code switched off for it)
        if again:
            return f"I still can't reach my model{both}; I've got this one too and will answer it the minute I'm back, by {when} at the latest."
        return (f"I can't reach my model right now{both}. I've kept your question and will answer it the minute I'm back "
                f"(I check every few minutes; by {when} at the latest); no need to send it again.")
    if again:
        return f"Still out of steam until {when}{both}; I've got this one too and will come back to it then."
    return f"I'm out of steam until {when}{both}. I've kept your question and will answer it then; no need to send it again."


def gave_up_line() -> str:
    return "I couldn't get to this one; I ran out of steam three times in a row. Ask me again when you have a minute and I'll take it fresh."


def due(now: float | None = None) -> list:
    now = now or time.time()
    fb = _fallback()
    return [r for r in _state().get("rows", [])
            if float(r.get("until") or 0) + GRACE_S <= now or str(r.get("fb") or "") != fb]


def take(row_id: str) -> dict | None:
    """Remove one row (it is about to be re-run) and return it. The block lifts when no row waits."""
    d = _state()
    rows = d.get("rows", [])
    hit = next((r for r in rows if r.get("id") == row_id), None)
    if hit is None:
        return None
    d["rows"] = [r for r in rows if r.get("id") != row_id]
    if not d["rows"]:
        d["until"] = 0
    core.save(STORE, d)
    return hit


def pending() -> list:
    return list(_state().get("rows", []))


if __name__ == "__main__":
    u = blocked_until()
    print(f"limit until {fmt(u)}" if u > time.time() else "no limit in force")
    for r in pending():
        print(f"  {r['id']} {r['ts']} attempt {r['attempts']} in {r['msg'].get('convo')}: {r['text'][:80]}")

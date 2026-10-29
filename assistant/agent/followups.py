"""followups.py - the agent checks back on something a person said would be done or answered,
and keeps every check-back promise it makes.

Ported from the property OS's shared chat-agent core (scripts/lib/chat_agent/followups.py,
the code example and example run), with the property specifics taken out: no units, no vendor
roles, and the working window comes from profile.json `followups` (a household is not
weekdays-only).

How it works, all inside the bridge:
  1. Every ask carries hint() in its header. When a message gives ANOTHER owner something
     to do or answer, or the speaker defers their own task ("I'll do it tonight"), the brain
     adds "followups": [{"to", "about", "at"}] to its SUGGEST line.
  2. actions.apply hands those rows to add(), which validates the person, clamps the time
     into the window and writes the ledger (assistant/data/agent/followups.json).
  3. Every message the bridge sees goes through note_reply(): a reply in the same thread
     closes the follow-up; a message elsewhere is only noted (the brain judges at check-in).
  4. Each bridge minute calls due(); for a due item the bridge asks the brain to look at the
     conversation and the records and either answer NO_REPLY (settled) or write the one-line
     check-in, which goes to that person's own chat. One check-in per follow-up.
  5. keep_promises() rewrites or removes any "I'll check back" in a reply so the agent never
     says it will check back when nothing is tracking it.

Never sends anything itself: posting stays in the bridge, and only to an owner.
"""
from __future__ import annotations

import re
import secrets
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import core  # noqa: E402

LEDGER = core.DATA / "followups.json"
MIN_LEAD_MIN = 15                   # never check back sooner than this
MAX_DAYS = 30                       # further out than this is a schedule, not a follow-up
MAX_TRIES = 3
FMT = "%Y-%m-%dT%H:%M"


def _cfg() -> dict:
    return core.profile().get("followups") or {}


def _window() -> tuple:
    return tuple(_cfg().get("window") or ("08:00", "20:30"))


def _weekdays_only() -> bool:
    return bool(_cfg().get("weekdays_only", False))


def _load() -> dict:
    d = core.load(LEDGER, {"items": []})
    return d if isinstance(d, dict) else {"items": []}


def _hm(s: str) -> tuple:
    h, m = [int(x) for x in s.split(":")]
    return h, m


def _off(dt: datetime) -> bool:
    return _weekdays_only() and dt.weekday() >= 5


def clamp_work(dt: datetime, now: datetime) -> datetime:
    """The brain's time, moved into the window: not sooner than MIN_LEAD_MIN from now, not
    before the window opens or after it closes (then it is the next allowed morning)."""
    dt = max(dt, now + timedelta(minutes=MIN_LEAD_MIN)).replace(second=0, microsecond=0)
    (h0, m0), (h1, m1) = _hm(_window()[0]), _hm(_window()[1])
    for _ in range(10):
        if _off(dt) or (dt.hour, dt.minute) > (h1, m1):
            dt = (dt + timedelta(days=1)).replace(hour=h0, minute=m0) + timedelta(minutes=30)
            while _off(dt):
                dt += timedelta(days=1)
            continue
        if (dt.hour, dt.minute) < (h0, m0):
            dt = dt.replace(hour=h0, minute=m0) + timedelta(minutes=15)
        break
    return dt


def in_window(now: datetime) -> bool:
    return not _off(now) and _hm(_window()[0]) <= (now.hour, now.minute) <= _hm(_window()[1])


def resolve_person(name: str) -> dict | None:
    """An owner in people.json by key or first name; None when unknown."""
    want = re.sub(r"[^a-z ]", "", str(name or "").lower()).strip()
    if not want:
        return None
    for key, u in (core.owners() or core.people().get("users") or {}).items():   # owners only (a staff member is not checked back with, 2026-10-24)
        full = str(u.get("name") or "").lower()
        if want in (key, full) or (full and want == full.split()[0]):
            return {"key": key, "name": u.get("name"), "first": str(u.get("name")).split()[0], "role": u.get("role")}
    return None


def add(rows, convo: str, thread: str, asker: dict | None, now: datetime | None = None) -> list:
    """Register (or cancel) follow-ups the brain emitted. Returns the rows added or re-timed."""
    now = now or datetime.now()
    asker = asker or {}
    if not core.can(asker, "schedule") or not isinstance(rows, list):
        return []
    led = _load()
    items = led.setdefault("items", [])
    out = []
    for r in rows[:3]:
        if not isinstance(r, dict):
            continue
        p = resolve_person(r.get("to"))
        if not p:
            continue
        if r.get("cancel"):
            # a cancel reaches only the follow-up in this thread or with this subject, never
            # every pending follow-up to that person (vault mistake 2026-10-02)
            abt = re.sub(r"\s+", " ", str(r.get("about") or "")).strip().lower()
            for it in items:
                if it.get("status") != "pending" or it.get("to_key") != p["key"]:
                    continue
                same_thread = bool(thread) and it.get("thread") == thread
                same_about = len(abt) >= 15 and abt[:60] in str(it.get("about") or "").lower()
                if same_thread or same_about:
                    it.update(status="cancelled", closed=now.strftime(FMT))
            continue
        about = re.sub(r"\s+", " ", str(r.get("about") or "")).strip()[:240]
        try:
            at = datetime.fromisoformat(str(r.get("at") or "")[:16])
        except ValueError:
            at = None
        if not about or at is None or at > now + timedelta(days=MAX_DAYS):
            continue
        at = clamp_work(at, now)
        row = {"to_key": p["key"], "to": p["name"], "from_key": asker.get("key") or "", "from": asker.get("name") or "",
               "about": about, "at": at.strftime(FMT), "convo": convo, "thread": thread or "",
               "status": "pending", "tries": 0}
        # one pending follow-up per person per thread (or per subject when unthreaded): a second
        # directive re-times the first instead of stacking
        old = next((it for it in items if it.get("status") == "pending" and it.get("to_key") == p["key"]
                    and ((row["thread"] and it.get("thread") == row["thread"])
                         or (not row["thread"] and core.similar(it.get("about", ""), about)))), None)
        if old:
            old.update({k: row[k] for k in ("about", "at")})
            out.append(old)
        else:
            row.update(id="fu-" + secrets.token_hex(4), created=now.strftime(FMT), created_ts=time.time())
            items.append(row)
            out.append(row)
            core.log_change("followup", f"follow-up {row['id']}: check back with {row['to']} at {row['at']} ({about[:80]})",
                            {"op": "followup_cancel", "id": row["id"]}, asker.get("key") or "")
    led["items"] = items[-200:]
    core.save(LEDGER, led)
    return out


def cancel(fid: str) -> bool:
    led = _load()
    for it in led.get("items") or []:
        if it.get("id") == fid and it.get("status") == "pending":
            it.update(status="cancelled", closed=datetime.now().strftime(FMT))
            core.save(LEDGER, led)
            return True
    return False


def note_reply(person_key: str, convo: str, thread: str = "") -> int:
    """A message from this person was seen. In the follow-up's own thread it closes it;
    anywhere else it is only noted (the brain judges at check-in time)."""
    led = _load()
    n = 0
    for it in led.get("items") or []:
        if it.get("status") != "pending" or it.get("to_key") != person_key:
            continue
        if time.time() - float(it.get("created_ts") or 0) < 5:
            continue                                    # the very message that created it
        if thread and thread == it.get("thread") and convo == it.get("convo"):
            it.update(status="answered", closed=datetime.now().strftime(FMT))
        else:
            it["spoke_since"] = True
        n += 1
    if n:
        core.save(LEDGER, led)
    return n


def due(now: datetime | None = None) -> list:
    """Pending follow-ups whose time has come, inside the window."""
    now = now or datetime.now()
    led = _load()
    changed, out = False, []
    for it in led.get("items") or []:
        if it.get("status") != "pending":
            continue
        try:
            at = datetime.fromisoformat(it["at"])
        except (KeyError, ValueError):
            it["status"] = "invalid"
            changed = True
            continue
        if now - at > timedelta(days=3):
            it.update(status="expired", closed=now.strftime(FMT))
            changed = True
            continue
        nt = str(it.get("next_try") or "")
        if at <= now and in_window(now) and (not nt or nt <= now.strftime(FMT)):
            out.append(dict(it))
    if changed:
        core.save(LEDGER, led)
    return out


def mark(fid: str, status: str = "", retry_min: int = 0, **kw) -> None:
    led = _load()
    for it in led.get("items") or []:
        if it.get("id") != fid:
            continue
        if retry_min:
            it["tries"] = int(it.get("tries") or 0) + 1
            it["next_try"] = (datetime.now() + timedelta(minutes=retry_min)).strftime(FMT)
            if it["tries"] >= MAX_TRIES:
                status = "failed"
        if status:
            it.update(status=status, closed=datetime.now().strftime(FMT))
        it.update(kw)
    core.save(LEDGER, led)


def pending() -> list:
    return [it for it in (_load().get("items") or []) if it.get("status") == "pending"]


def hint(now: datetime | None = None) -> str:
    """The protocol, carried in the header of every ask."""
    now = now or datetime.now()
    names = ", ".join(u["name"] for u in (core.people().get("users") or {}).values())
    pend = "; ".join(f"{str(p.get('to')).split()[0]} at {p.get('at')} ({p.get('about')[:50]})" for p in pending()[:4])
    return (
        "[FOLLOW-UPS, tracked by code. Now is " + now.strftime("%a %Y-%m-%d %H:%M") + ". When this message "
        f"gives another owner ({names}) something to do or asks them for an answer (\"ask owner6 whether "
        "Saturday works\"), or when the SPEAKER defers their own task (\"I'll send it tonight\"), add "
        "\"followups\": [{\"to\": \"<their first name>\", \"about\": \"<what is being waited on and why it "
        "matters, one plain sentence>\", \"at\": \"YYYY-MM-DDTHH:MM\"}] to the SUGGEST line. \"at\" is when a "
        "good assistant would check back if it is still open, judged from the deadline in the message: wanted "
        "today means late afternoon; before something tomorrow means this evening; urgent means within the "
        "hour; no deadline means tomorrow morning; a deferral means just after the time they named. The code "
        "sends the check-in to that person at that time only if it is still open. To drop one: {\"to\": "
        "\"<name>\", \"cancel\": true, \"about\": \"<its subject>\"}. Promise a check-back only if you put it "
        "in followups; code removes any promise that is not tracked. A plain timed reminder with no question "
        "of whether something got done is a `schedule`, not a follow-up."
        + (f" Pending now: {pend}." if pend else "") + "]\n")


_PROMISE = re.compile(
    r"(?<![A-Za-z])I(?:'ll| will|'m going to) (?:also |then |just )?(?:check (?:back|in)|follow up|circle back|ask again|"
    r"nudge|ping|remind)[^.?!\n]*[.?!]?", re.I)


def when_text(at: str, now: datetime | None = None) -> str:
    now = now or datetime.now()
    try:
        dt = datetime.fromisoformat(at)
    except ValueError:
        return "later"
    s = dt.strftime("%I:%M %p").lstrip("0")
    return s if dt.date() == now.date() else dt.strftime("%A ") + s


def keep_promises(text: str, added: list | None = None, speaker: str = "", now: datetime | None = None) -> str:
    """Every check-back promise in a reply must match a follow-up the code is tracking. A
    promise naming a person with a pending follow-up is rewritten to the ledger's own time
    (the brain's time may have been clamped); a promise with nothing behind it is removed."""
    now = now or datetime.now()
    text = str(text or "")
    if not _PROMISE.search(text):
        return text
    live = {}
    fresh = [it for it in (added or []) if it.get("status", "pending") == "pending"]
    for it in list(added or []) + pending():
        if it.get("status", "pending") != "pending":
            continue
        first = str(it.get("to") or "").split()[0].lower() if it.get("to") else ""
        if first and first not in live:
            live[first] = it

    used = set()                                # one sentence per tracked follow-up: a second promise that
                                                # can only map to one already spoken for is not tracked, so it goes

    def _fix(m):
        sent = m.group(0)
        words = re.findall(r"[A-Za-z]+", sent.lower())
        hit = next((live[w] for w in words if w in live), None)
        if hit is None and len(live) == 1 and re.search(r"\b(him|her|them|you)\b", sent, re.I):
            hit = next(iter(live.values()))
        if hit is None and len(fresh) == 1:
            hit = fresh[0]                      # names nobody: it is the one registered on this turn
        if hit is None or id(hit) in used:
            return ""
        used.add(id(hit))
        first = str(hit.get("to")).split()[0]
        who = "you" if speaker and first.lower() == speaker.lower() else first
        return f"I'll check back with {who} at {when_text(hit.get('at', ''), now)} if it's still open."
    out = _PROMISE.sub(_fix, text)
    return re.sub(r"[ \t]{2,}", " ", re.sub(r"\n{3,}", "\n\n", out)).strip()


def due_question(it: dict) -> str:
    """What the bridge asks the brain when a follow-up comes due."""
    first = str(it.get("to") or "").split()[0]
    giver = str(it.get("from") or "an owner").split()[0]
    if it.get("from_key") and it.get("from_key") == it.get("to_key"):
        return (
            f"[FOLLOW-UP DUE. On {it.get('created')} {it.get('to')} told you this was not finished yet, and you "
            f"said you would check back: {it.get('about')}. FIRST check the conversation above and the records "
            "you can read: if it is done, settled, or no longer matters, reply exactly NO_REPLY. Otherwise your "
            f"WHOLE reply is the check-in to {first}: one short line asking plainly whether it is done. No "
            "SUGGEST line, no greeting.]")
    return (
        f"[FOLLOW-UP DUE. On {it.get('created')} {giver} asked for this to be put to {it.get('to')}: "
        f"{it.get('about')}. "
        + (f"{first} has written to you since then (see the conversation above). " if it.get("spoke_since") else "")
        + "FIRST check the conversation above and the records you can read: if it was answered, settled, or no "
        f"longer matters, reply exactly NO_REPLY. Otherwise your WHOLE reply is the check-in to {first}: one "
        f"short line saying what {giver} is waiting on and why it matters now. No SUGGEST line, no greeting.]")


if __name__ == "__main__":
    for p in pending():
        print(f"{p['id']} {p['at']} -> {p['to']}: {p['about']}")
    print(f"pending={len(pending())}")

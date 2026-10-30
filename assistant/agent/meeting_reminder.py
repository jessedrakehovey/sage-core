"""meeting_reminder.py - a nudge in the Sage chat ~10 minutes before a lead call. Zero-LLM.

2026-10-12: the 3 PM vision call with Arden and Reese started with nobody hosting.
Reese waited about 10 minutes alone, a team member could not get in, and the couple had to text
for the link. Now, about LEAD_MIN minutes before any lead vision call or CRM meeting,
one short line goes to the owner hosting it: when it starts, the Meet link, and who else
is expected (so someone waiting in the lobby gets let in).

Sources, read only:
  - the owners' Google calendars (google_api.events): an event counts when its title,
    place or notes say vision / CRM / consultation;
  - the CRM snapshot's upcoming meetings (the same list lead_meetings.py matches to leads;
    the CRM's scheduler bookings).
The same meeting often sits in both: everything starting in the same minute is one meeting
and gets one message. Posted once per meeting (data/agent/meeting_reminders.json).

The bridge calls tick(post) once a minute. Google is read at most every FETCH_S seconds.

  python assistant/agent/meeting_reminder.py      (what would be reminded in the next hour)
"""
from __future__ import annotations

import re
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import core  # noqa: E402

SENT = core.DATA / "meeting_reminders.json"
LEAD_MIN = 10                 # remind when the meeting starts within this many minutes
FETCH_S = 240                 # Google calendars are read at most every 4 min (the tick is every minute)
def _leadish_re():
    try:
        sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
        import crm as _crm_m
        crm_word = re.escape(_crm_m.name().lower())
    except Exception:  # noqa: BLE001
        crm_word = ""
    pat = r"\bvision\b|consultation"
    if crm_word and crm_word not in ("the crm", ""):
        pat += "|" + crm_word
    return re.compile(pat, re.I)


LEADISH = _leadish_re()
_cache = {"at": 0.0, "rows": []}


def _dt(s):
    try:
        dt = datetime.fromisoformat(str(s).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    return dt if dt.tzinfo else dt.astimezone()


def _pacific(dt):
    try:
        from zoneinfo import ZoneInfo
        return dt.astimezone(ZoneInfo("America/Los_Angeles"))
    except Exception:  # noqa: BLE001
        return dt.astimezone()


def _google_rows() -> list:
    if time.time() - _cache["at"] < FETCH_S:
        return _cache["rows"]
    try:
        import google_api
        rows = [e for e in google_api.events(days=1, who="both") if not e.get("error")]
    except Exception:  # noqa: BLE001   a dead token must never stop the bridge's minute tick
        rows = []
    _cache.update(at=time.time(), rows=rows)
    return rows


def _snapshot() -> dict:
    try:
        sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
        import crm as _crm_m
        return _crm_m.snapshot() or {}
    except Exception:  # noqa: BLE001
        return {}


def _owner_of(name_or_email: str) -> str:
    """people.json key for an owner's name or address, '' for anyone else."""
    s = str(name_or_email or "").strip().lower()
    for key, u in (core.owners() or core.people().get("users") or {}).items():   # owners only (a staff member is not checked back with, 2026-10-24)
        if s and (s in [e.lower() for e in u.get("emails", [])] or s.split()[0] == str(u.get("name") or "").lower()):
            return key
    return ""


def candidates(now: datetime, google: list, snap: dict) -> list:
    """[{start, title, host, link, people}] for lead meetings starting within LEAD_MIN minutes."""
    names = {str(c.get("email") or "").lower(): c.get("name") for c in snap.get("contacts") or []
             if isinstance(c, dict) and c.get("email") and c.get("name")}
    out = []
    for e in google:
        start = _dt(e.get("start"))
        text = " ".join(str(e.get(k) or "") for k in ("title", "location", "notes", "meet"))
        if e.get("all_day") or not start or not LEADISH.search(text):
            continue
        guests = [names.get(str(g).lower()) or g for g in e.get("guests") or []]
        out.append({"start": start, "title": e.get("title") or "", "link": e.get("meet") or "",
                    "host": _owner_of(e.get("organizer")) or e.get("owner") or "",
                    "people": [g for g in guests if not _owner_of(g)]})
    for m in snap.get("meetings") or []:
        if not isinstance(m, dict) or m.get("_error") or m.get("all_day"):
            continue
        start = _dt(m.get("start"))
        if not start:
            continue
        hosts = [h for h in m.get("hosts") or [] if h]
        loc = str(m.get("location") or "")
        out.append({"start": start, "title": m.get("title") or str(m.get("type") or "meeting").replace("_", " "),
                    "link": loc if loc.startswith("http") else "",
                    "host": next((_owner_of(h) for h in hosts if _owner_of(h)), ""),
                    "people": [c.get("name") for c in m.get("clients") or [] if isinstance(c, dict) and c.get("name")]
                              + [h for h in hosts if not _owner_of(h)]})
    return [c for c in out if timedelta(0) < c["start"] - now <= timedelta(minutes=LEAD_MIN)]


def _merge(cands: list) -> dict:
    """One meeting per start minute: every source's link, host and people folded together."""
    by = {}
    for c in cands:
        key = c["start"].astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M")
        m = by.setdefault(key, {"key": key, "start": c["start"], "titles": [], "link": "", "host": "", "people": []})
        if c["title"] and c["title"] not in m["titles"]:
            m["titles"].append(c["title"])
        m["link"] = m["link"] or c["link"]
        m["host"] = m["host"] or c["host"]
        for p in c["people"]:
            if p and p.lower() not in [x.lower() for x in m["people"]]:
                m["people"].append(p)
    return by


def message(m: dict, now: datetime) -> str:
    host = (core.people().get("users") or {}).get(m["host"] or "owner1", {}).get("name") or "owner1"
    mins = max(1, round((m["start"] - now).total_seconds() / 60))
    at = _pacific(m["start"]).strftime("%I:%M %p").lstrip("0")
    line = f"{host}, {m['titles'][0] if m['titles'] else 'your call'} starts at {at}, in {mins} min."
    line += f" Meet link: {m['link']}" if m["link"] else " I couldn't find a meeting link on it."
    if m["people"]:
        line += f" Expected: {', '.join(m['people'])}. Let anyone waiting in the lobby in."
    return line


def tick(post, now: datetime | None = None, google: list | None = None, snap: dict | None = None) -> int:
    """Post the reminder for each lead meeting about to start, once. Returns how many went out."""
    now = now or datetime.now(timezone.utc)
    google = _google_rows() if google is None else google
    snap = _snapshot() if snap is None else snap
    sent = core.load(SENT, {})
    n = 0
    for key, m in _merge(candidates(now, google, snap)).items():
        if key in sent:
            continue
        to, text = m["host"] or "owner1", message(m, now)
        post(to, text)
        core.say(f"dm:{to}", core.profile().get("name", "Agent"), text, channel="meeting-reminder")
        sent[key] = core.stamp()
        n += 1
    if n:
        cutoff = (now - timedelta(days=2)).strftime("%Y-%m-%dT%H:%M")
        core.save(SENT, {k: v for k, v in sent.items() if k >= cutoff})
    return n


if __name__ == "__main__":
    LEAD_MIN = 60
    _now = datetime.now(timezone.utc)
    for _m in _merge(candidates(_now, _google_rows(), _snapshot())).values():
        print(message(_m, _now))
    print("MEETING_REMINDER done")

"""event_reminders.py - a reminder before every timed event on the owners' calendars (2026-10-24).

the owner, by voice note after Tony's costume party: "You should have given me reminders about it since I gave
you the information." The party was on his Google calendar (Sage had added it) and nothing reminded him.
Now: every timed event on a connected owner calendar gets ONE line in that owner's chat LEAD_MIN minutes
before it starts, with the time and the place. Client meetings are meeting_reminder.py's (a Meet link and
"who is expected"), so an event that one would handle is skipped here. All-day events are skipped (the
morning brief lists them). Quiet hours are respected: an event before 07:30 is reminded at 07:00 the same
day, not at night. Switch: config `reminders.events` (true) and `reminders.events_lead_min` (45).

Runs from the bridge's minute tick next to meeting_reminder.tick (post = the bridge's DM poster).
"""
from __future__ import annotations

import json
import re
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import core  # noqa: E402

SENT = core.DATA / "event_reminders.json"
CONFIG = core.AGENT.parent / "config.json"
LEAD_MIN = 45
FETCH_S = 240
_cache = {"at": 0.0, "rows": []}
_CLIENT = re.compile(r"'s project|vision (call|meeting)|touch point|consult|discovery call", re.I)


def _cfg() -> dict:
    try:
        c = json.loads(CONFIG.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        return {}
    return c.get("reminders") or {}


def _rows() -> list:
    if time.time() - _cache["at"] < FETCH_S:
        return _cache["rows"]
    try:
        import google_api
        rows = [e for e in google_api.events(days=1, who="both") if not e.get("error")]
    except Exception:  # noqa: BLE001
        rows = []
    _cache.update(at=time.time(), rows=rows)
    return rows


def _dt(s):
    try:
        d = datetime.fromisoformat(str(s).replace("Z", "+00:00"))
        return d if d.tzinfo else d.replace(tzinfo=timezone.utc)
    except (TypeError, ValueError):
        return None


def _local(d: datetime) -> datetime:
    return d.astimezone()


def due(rows: list, now: datetime, lead_min: int) -> list:
    """Events starting within lead_min minutes (and not yet started) that are timed and not a client meeting."""
    out = []
    for e in rows:
        if e.get("all_day") or not e.get("start"):
            continue
        s = _dt(e["start"])
        if not s:
            continue
        mins = (s - now).total_seconds() / 60
        if not (0 < mins <= lead_min):
            continue
        if _CLIENT.search(str(e.get("title") or "")) or e.get("meet"):
            continue           # meeting_reminder.py's, with the Meet link and who is expected
        out.append(e)
    return out


def message(e: dict, now: datetime) -> str:
    s = _local(_dt(e["start"]))
    at = s.strftime("%I:%M %p").lstrip("0")
    mins = max(1, round((_dt(e["start"]) - now).total_seconds() / 60))
    line = f"{e.get('title') or 'Your event'} is at {at}, in {mins} min."
    if e.get("location"):
        line += f" {str(e['location']).strip()}."
    return line


def tick(post, now: datetime | None = None, rows: list | None = None) -> int:
    c = _cfg()
    if c.get("events") is False:
        return 0
    lead = int(c.get("events_lead_min") or LEAD_MIN)
    now = now or datetime.now(timezone.utc)
    local_h = _local(now).hour + _local(now).minute / 60
    if local_h < 7.0 or local_h >= 22.0:
        return 0
    rows = _rows() if rows is None else rows
    sent = core.load(SENT, {})
    n = 0
    for e in due(rows, now, lead):
        key = f"{e.get('id') or e.get('title')}|{str(e.get('start'))[:16]}"
        if key in sent:
            continue
        to = str(e.get("owner") or core.first_owner())
        if to not in core.owners():
            to = core.first_owner()
        text = message(e, now)
        post(to, text)
        core.say(f"dm:{to}", core.profile().get("name", "Agent"), text, channel="event-reminder")
        sent[key] = core.stamp()
        n += 1
    if n:
        cutoff = (now - timedelta(days=2)).strftime("%Y-%m-%dT%H:%M")
        core.save(SENT, {k: v for k, v in sent.items() if str(v) >= cutoff})
    return n

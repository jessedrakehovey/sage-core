"""routines.py - schedules the agent creates for itself (reminders and recurring look-ups).

A routine is a row in state/routines.json, not a Windows task: the bridge loop calls tick()
every minute, so a new schedule needs no registration and no terminal. A routine can only
do one thing: put a message in front of an owner. `say` posts fixed text (zero LLM);
`prompt` runs one read-only model turn and posts what it found. Nothing here can send to
anyone outside the household. A routine with `space` (2026-11-02: "tag him in a message every day in
our group chat") posts its fixed `say` in a team space the app is in instead, never the owners' home
space or a DM, and never a `prompt` (a team space is business only). `until` (YYYY-MM-DD) ends it.

  when: {"daily": "07:30"} | {"weekly": "mon 07:30"} | {"once": "2026-10-12 09:00"} | {"every_hours": 4}
        | {"on": "<event>", "match": "<words>"}

An EVENT routine (2026-11-09, "when a new lead comes in, tell me") fires on something the system did or saw instead
of a clock: `on` names one of EVENTS (card, approved, declined, done, failed, grant, work) and `match` holds words
that must all appear in the event (a card's header, the option tapped, a job's result, a work-log line); no words =
every event of that kind. Events are read from the audit ledger and the work log, append-only files, from a cursor
(data/routine_events.json) that starts at the end: history is never replayed. Up to MAX_EVENT_FIRES per routine per
tick post; the rest of a burst is one line ("and N more like it"). Quiet hours hold them like any routine.

  python assistant/agent/routines.py list
  python assistant/agent/routines.py off s3
  python assistant/agent/routines.py change s3 '{"when": {"every_hours": 3}}'

A schedule is named by its id (s3), by a dict carrying the id, or by its title or part of it (find). It can be
CHANGED in place (update: when / to / say / prompt / until), not only stopped: 2026-11-06, the hourly "owner
loves you" reminder could be neither stopped nor moved for five days because the stop arrived as {'id': 's8'} and
there was no change at all; every refusal said "no active schedule".
"""
from __future__ import annotations

import json
import re
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import core  # noqa: E402

FILE = core.STATE / "routines.json"
RUNS = core.DATA / "routine_runs.json"
HISTORY = core.DATA / "routine_history.json"      # the last HISTORY_N runs of each routine (upgrade 11, 2026-11-08)
HISTORY_N = 20
DOW = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]
FMT = "%Y-%m-%d %H:%M"
EVENTS_CURSOR = core.DATA / "routine_events.json"   # where the event reader is in the ledger and the work log
EVENT_DUE = "9999-12-31 23:59"                        # an event routine's next_due: never by the clock
MAX_EVENT_FIRES = 3                                   # per routine per tick; the rest of a burst is one line
# the events an owner can hook a routine to, in plain words -> what the engine records
EVENTS = {
    "card": "a card comes up for you",
    "approved": "a card is approved (your tap, your words, or a standing yes)",
    "declined": "a card is skipped or declined",
    "done": "an approved action went through (a message sent, a job finished, a record written)",
    "failed": "an approved action failed",
    "grant": "a standing yes is given",
    "work": "anything I did (the work log)",
}
_NO = re.compile(r"^\s*(skip|drop|dismiss|ignore|no\b|not now|later|leave|cancel|never mind|keep asking|decline|close)", re.I)
# a short line that only says there is nothing ("Nothing new today.", "No news."); a line that goes on to say
# something ("Nothing new on the venue, but the florist confirmed") is a message
# a look-up routine ends ITSELF with this line once its reason is gone (2026-11-07..11: "Ask ... for a drone update"
# ran four mornings after the wedding it was for, each time saying it had tried to switch itself off and was refused:
# the turn had no way to stop its own schedule)
_SELF_DONE = re.compile(r"^[ \t>*`-]*ROUTINE_DONE[ \t`*.]*$", re.M)
_MONTHS = {m: i + 1 for i, m in enumerate(("jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"))}
_LAST_DAY = re.compile(r"\b(?:stops?|ends?|end it|until|through|no later than)\s+(?:after\s+|on\s+|by\s+)?"
                       r"(?:(\d{4})-(\d{2})-(\d{2})|([A-Za-z]{3})[a-z]*\.?\s+(\d{1,2})(?:st|nd|rd|th)?(?:,?\s+(\d{4}))?)", re.I)


def last_day_from_words(text: str, now: datetime | None = None) -> str:
    """'... Stop after Nov 7' / 'until 2026-11-07' -> '2026-11-07' ('' when the words name no last day). The words an
    owner used to say when a schedule ends are its `until`, by code: the model forgot to set it and the schedule ran on."""
    now = now or datetime.now()
    m = _LAST_DAY.search(str(text or ""))
    if not m:
        return ""
    try:
        if m.group(1):
            d = datetime(int(m.group(1)), int(m.group(2)), int(m.group(3)))
        else:
            mon = _MONTHS.get(m.group(4).lower())
            if not mon:
                return ""
            d = datetime(int(m.group(6) or now.year), mon, int(m.group(5)))
            if not m.group(6) and (now - d).days > 180:      # "Jan 3" said in December is next January
                d = d.replace(year=now.year + 1)
    except ValueError:
        return ""
    return d.strftime("%Y-%m-%d")


_NOTHING = re.compile(r"^\s*(?:nothing|no news|no updates?|no change|none)\b[^.!,;:\n]{0,30}[.!]?\s*$", re.I)


def _history_add(sid: str, ok: bool, text: str, note: str = "", now: datetime | None = None) -> None:
    """One row per run, newest last, at most HISTORY_N per routine. Never raises."""
    try:
        with core.locked:
            h = core.load(HISTORY, {})
            rows = list(h.get(sid) or [])
            rows.append({"ts": (now or datetime.now()).strftime(FMT), "ok": bool(ok), "text": str(text or "")[:200], **({"note": note[:120]} if note else {})})
            h[sid] = rows[-HISTORY_N:]
            core.save(HISTORY, h)
    except Exception:  # noqa: BLE001
        pass


def history(ref) -> list:
    r = find(ref, active_only=False)
    return list((core.load(HISTORY, {}) or {}).get(r["id"]) or []) if r else []


def _idle_days(key: str, now: datetime | None = None) -> float:
    """Days since the owner `key` last wrote in any conversation (the transcript); 0 when they never wrote (a new pack
    is never 'idle')."""
    try:
        name = (core.people().get("users") or {}).get(key, {}).get("name")
        last = ""
        for row in reversed(core.rows(core.TRANSCRIPT)[-4000:]):
            if row.get("who") == name and row.get("channel") not in ("routine", "system"):
                last = str(row.get("ts") or "")
                break
        if not last:
            return 0.0
        return ((now or datetime.now()) - datetime.strptime(last[:16], FMT)).total_seconds() / 86400
    except Exception:  # noqa: BLE001
        return 0.0


def _idle_paused(r: dict, now: datetime) -> bool:
    """profile.json -> routines.idle_pause_days (0 = off): a routine addressed to an owner who has not written in that
    long is held, not fired (Grok Bot pauses routines for an idle user; a reminder nobody reads is noise)."""
    days = int((core.profile().get("routines") or {}).get("idle_pause_days", 0) or 0)
    if not days or r.get("space"):
        return False
    targets = list(core.owners() or (core.people().get("users") or {})) if r.get("to") == "both" else [r.get("to")]
    idle = [t for t in targets if t and _idle_days(t, now) >= days]
    return bool(idle) and len(idle) == len([t for t in targets if t])


def test(ref, run_prompt=None, now: datetime | None = None) -> dict:
    """What a routine WOULD post, without posting, counting or moving it (the Test button). A `say` routine returns its
    text; a `prompt` routine runs one read-only model turn (one of today's budget) and returns the reply."""
    now = now or datetime.now()
    r = find(ref, active_only=False)
    if not r:
        return {"ok": False, "reason": f"no schedule {ref!r}"}
    if r.get("say"):
        text = r["say"] + (f" (I'd add what happened, e.g. {_event_words(_sample_event(r))}.)" if is_event(r) else "")
        return {"ok": True, "id": r["id"], "text": text, "next_due": r.get("next_due", ""), "kind": "say"}
    if run_prompt is None:
        import brain
        run_prompt = lambda p: brain.run_once(p)  # noqa: E731
    sample = f"\nWhat happened (a sample, for this test): {_event_words(_sample_event(r))}" if is_event(r) else ""
    res = run_prompt(f"[TEST of routine {r['id']} '{r['title']}', {now.strftime('%Y-%m-%d %H:%M %a')}; nothing you say is posted]\n"
                     f"{r['prompt']}{sample}\nReply with the message for {r['to']} only. No SUGGEST line.")
    if not res.get("ok"):
        return {"ok": False, "id": r["id"], "reason": str(res.get("error"))[:200], "kind": "prompt"}
    import brain as _b
    said, _ = _b.split_suggest(res.get("text", ""))
    return {"ok": True, "id": r["id"], "text": said, "next_due": r.get("next_due", ""), "kind": "prompt",
            "would_skip": bool(_NOTHING.match(said or "")) and str(r.get("on_missing") or "skip") == "skip"}


def _load() -> dict:
    return core.load(FILE, {"next_id": 1, "routines": []})


def _hm(s: str) -> tuple[int, int]:
    h, m = str(s).strip().split(":")
    h, m = int(h), int(m)
    if not (0 <= h < 24 and 0 <= m < 60):
        raise ValueError("bad time")
    return h, m


def next_due(when: dict, after: datetime) -> datetime | None:
    """The first firing strictly after `after`. None = never again. Raises ValueError on a bad spec.
    An event routine ({"on": ...}) is never due by the clock: EVENT_DUE, far in the future, so the time loop skips it."""
    if "on" in when:
        ev = str(when.get("on") or "").strip().lower()
        if ev not in EVENTS:
            raise ValueError(f"on must be one of {', '.join(EVENTS)}")
        return datetime.strptime(EVENT_DUE, FMT)
    if "daily" in when:
        h, m = _hm(when["daily"])
        d = after.replace(hour=h, minute=m, second=0, microsecond=0)
        return d if d > after else d + timedelta(days=1)
    if "weekly" in when:
        day, hm = str(when["weekly"]).lower().split()
        h, m = _hm(hm)
        d = after.replace(hour=h, minute=m, second=0, microsecond=0) + timedelta(days=(DOW.index(day[:3]) - after.weekday()) % 7)
        return d if d > after else d + timedelta(days=7)
    if "once" in when:
        d = datetime.strptime(str(when["once"]).strip()[:16].replace("T", " "), FMT)
        return d if d > after else None
    if "every_hours" in when:
        n = float(when["every_hours"])
        if n < float((core.profile().get("routines") or {}).get("min_every_hours", 1)):
            raise ValueError("too frequent")
        return after + timedelta(hours=n)
    raise ValueError("when needs daily, weekly, once or every_hours")


def describe(when: dict) -> str:
    if "on" in when:
        words = str(when.get("match") or "").strip()
        return f"when {EVENTS.get(str(when.get('on') or '').lower(), when.get('on'))}" + (f", about '{words}'" if words else "")
    k, v = next(iter(when.items()))
    return f"{k.replace('_', ' ')} {v}"


def is_event(r: dict) -> bool:
    return isinstance(r.get("when"), dict) and "on" in r["when"]


def event_from_words(text: str) -> dict:
    """An owner's plain words -> {"on", "match"} or {} (a helper for the model and for tests): "when a new lead card
    comes up" -> card / "new lead"; "whenever something I approved fails" -> failed; "when a job finishes" -> done."""
    t = " ".join(str(text or "").lower().split())
    m = re.search(r"\b(?:when(?:ever)?|every time|each time|as soon as|the minute|the moment|once)\b(?P<rest>.+)", t)
    if not m:
        return {}
    rest = m.group("rest")
    rest = re.split(r",|\b(?:tell|text|send|say|let|remind|post|ping|message|give|put|write|note)\b", rest, 1)[0].strip()
    on = ""
    if re.search(r"\b(declin|skip|dismiss|turn(?:ed)? down|said no)", rest):
        on = "declined"
    elif re.search(r"\b(fail|error|didn.t go|bounce|refus)", rest):
        on = "failed"
    elif re.search(r"\b(approv|okay|ok.d|tap|yes to|sign(?:ed)? off)", rest):
        on = "approved"
    elif re.search(r"\b(standing yes|grant|let you)", rest):
        on = "grant"
    elif re.search(r"\b(?:sent|went out|finish|done|complet|lands?\b|landed|written|through\b)", rest):
        on = "done"
    elif re.search(r"\b(card|lead|inquir|decision|asks? (?:me|you)|needs? (?:me|my))", rest):
        on = "card"
    elif re.search(r"\b(you do|i do|did|work|anything)\b", rest):
        on = "work"
    if not on:
        return {}
    words = re.sub(r"\b(a|an|the|new|comes?|came|up|in|is|are|gets?|got|my|your|you|i|me|something|anything|that|which|"
                   r"card|cards|approv\w*|declin\w*|skip\w*|fail\w*|done|finish\w*|sent|went|out|lands?|landed|job|jobs|one|it|"
                   r"for|on|of|to|with|from|about|happens?|do|did|does|work|say|says|there|here|again)\b", " ", rest)
    words = " ".join(w for w in re.sub(r"[^\w\s'-]", " ", words).split() if len(w) > 1)
    return {"on": on, **({"match": words[:80]} if words else {})}


def team_space(ref: str) -> tuple[str, str]:
    """(space name, title) of a shared space the app is in other than the owners' home space, from its
    name (spaces/...), its title (or a phrase containing it) or "team" when there is just
    one. ('', '') when nothing matches, more than one does, or it is the home space or a DM."""
    ref = str(ref or "").strip()
    home = core.home_space()
    dms = set(core.load(core.DATA / "gchat_spaces.json", {}).values())
    if not ref or ref == home or ref in dms:
        return "", ""
    try:
        import space_reader
        rows = [r for r in space_reader.spaces() if r.get("name") and r["name"] != home and r["name"] not in dms]
    except Exception:  # noqa: BLE001
        rows = []
    if ref.startswith("spaces/"):
        return ref, next((r.get("title", "") for r in rows if r["name"] == ref), "")
    low = ref.lower()
    hits = [r for r in rows if r.get("title") and (r["title"].lower() in low or low in r["title"].lower())]
    if not hits and low in ("team", "team space", "group", "group chat") and len(rows) == 1:
        hits = rows
    return (hits[0]["name"], hits[0].get("title", "")) if len(hits) == 1 else ("", "")


def add(spec: dict, by: str = "", now: datetime | None = None) -> dict:
    """-> {"status": applied|refused, "id", "reason"}"""
    now = now or datetime.now()
    title = str(spec.get("title") or "").strip()[:80]
    say, prompt = str(spec.get("say") or "").strip()[:1500], str(spec.get("prompt") or "").strip()[:1500]
    to = str(spec.get("to") or by or "both").lower()
    users = list(core.owners() or (core.people().get("users") or {}))
    space, stitle = "", ""
    if str(spec.get("space") or "").strip():
        space, stitle = team_space(spec["space"])
        if not space:
            return {"status": "refused", "reason": "I don't see that team space; name it as it shows in Chat"}
        if not say:
            return {"status": "refused", "reason": "a team space schedule posts fixed text (say), never a look-up"}
        to = "space"
    elif to not in users + ["both"]:
        return {"status": "refused", "reason": f"'to' must be one of {users + ['both']}"}
    if not title or not (say or prompt):
        return {"status": "refused", "reason": "needs a title and either say or prompt"}
    until = str(spec.get("until") or "").strip()[:10]
    if until:
        try:
            datetime.strptime(until, "%Y-%m-%d")
        except ValueError:
            return {"status": "refused", "reason": "until must be YYYY-MM-DD"}
    when = spec.get("when") if isinstance(spec.get("when"), dict) else {}
    if not until and "once" not in when:
        until = last_day_from_words(f"{title}. {say} {prompt}", now)      # "Stop after Nov 7" is the last day, by code
    try:
        due = next_due(when, now)
    except (ValueError, KeyError, IndexError, TypeError) as e:
        return {"status": "refused", "reason": f"could not read the time ({e})"}
    if due is None or (until and due.strftime("%Y-%m-%d") > until):
        return {"status": "refused", "reason": "that time is already past"}
    on_missing = str(spec.get("on_missing") or "").strip().lower()
    if on_missing and on_missing not in ("skip", "say"):
        return {"status": "refused", "reason": "on_missing must be skip (post nothing when there is nothing) or say"}
    extra = {k: v for k, v in (("space", space), ("space_title", stitle), ("until", until), ("on_missing", on_missing)) if v}
    with core.locked:
        return _add(title, say, prompt, to, when, due, by, extra)


def _where(r: dict) -> str:
    return f"in {r.get('space_title') or 'the team space'}" if r.get("space") else f"to {r['to']}"


def _add(title, say, prompt, to, when, due, by, extra=None) -> dict:
    d = _load()
    act = [r for r in d["routines"] if r.get("active", True)]
    if len(act) >= int((core.profile().get("routines") or {}).get("max_active", 25)):
        return {"status": "refused", "reason": "schedule limit reached; stop one first"}
    if any(r["title"].lower() == title.lower() and r["when"] == when for r in act):
        return {"status": "refused", "reason": "that schedule already exists"}
    sid = f"s{d['next_id']}"
    d["next_id"] = int(d["next_id"]) + 1
    row = {"id": sid, "title": title, "when": when, "to": to, "by": by, "added": core.stamp(),
           "active": True, "next_due": due.strftime(FMT), "runs": 0,
           **({"say": say} if say else {"prompt": prompt}), **(extra or {})}
    d["routines"].append(row)
    core.save(FILE, d)
    end = f", until {row['until']}" if row.get("until") else ""
    cid = core.log_change("routine", f"schedule {sid}: {title} ({describe(when)}{end}, {_where(row)})", {"op": "routine_off", "id": sid}, by)
    return {"status": "applied", "id": sid, "change": cid, "next_due": due.strftime(FMT)}


def find(ref, rows: list | None = None, active_only: bool = True) -> dict:
    """The routine a reference names: an id ("s8"), a dict with an id or title, or (part of) a title, case
    ignored; an active one wins over a stopped one with the same words. {} when nothing (or more than one) matches."""
    if isinstance(ref, dict):
        ref = ref.get("id") or ref.get("title") or ref.get("schedule") or ""
    ref = str(ref or "").strip().strip("[]").lower()
    if not ref:
        return {}
    rows = _load()["routines"] if rows is None else rows
    pool = [r for r in rows if r.get("active", True)] if active_only else list(rows)
    hit = [r for r in pool if str(r.get("id", "")).lower() == ref]
    if not hit:
        hit = [r for r in pool if str(r.get("title", "")).lower() == ref]
    if not hit:
        hit = [r for r in pool if ref in str(r.get("title", "")).lower() or str(r.get("title", "")).lower() in ref]
    return dict(hit[0]) if len(hit) == 1 else {}


_CHANGEABLE = ("when", "to", "say", "prompt", "until", "space", "on_missing")


def update(ref, changes: dict, by: str = "", now: datetime | None = None) -> dict:
    """Change a schedule in place (its time, who it goes to, its words, its last day). -> {"status": applied|refused,
    "id", "reason", "next_due"}. Ledgered with a real undo (the old fields put back)."""
    now = now or datetime.now()
    ch = {k: v for k, v in (changes or {}).items() if k in _CHANGEABLE}
    if not ch:
        return {"status": "refused", "reason": "nothing to change: give when, to, say, prompt or until"}
    with core.locked:
        d = _load()
        r = find(ref, d["routines"])
        if not r:
            return {"status": "refused", "reason": f"I don't have an active schedule {ref!r}"}
        row = next(x for x in d["routines"] if x["id"] == r["id"])
        old = {k: row.get(k) for k in ch}
        if "when" in ch:
            if not isinstance(ch["when"], dict):
                return {"status": "refused", "reason": "when must be {\"daily\": \"07:30\"} | {\"weekly\": \"mon 07:30\"} | {\"once\": \"2026-10-12 09:00\"} | {\"every_hours\": 4}"}
            try:
                nxt = next_due(ch["when"], now)
            except (ValueError, KeyError, IndexError, TypeError) as e:
                return {"status": "refused", "reason": f"bad when ({str(e)[:60]})"}
            if nxt is None:
                return {"status": "refused", "reason": "that when never comes due"}
            row["when"], row["next_due"] = ch["when"], nxt.strftime(FMT)
        if "to" in ch:
            to = str(ch["to"] or "").lower()
            users = list(core.owners() or (core.people().get("users") or {}))
            if to not in users + ["both"]:
                return {"status": "refused", "reason": f"'to' must be one of {users + ['both']}"}
            row["to"] = to
        for k in ("say", "prompt"):
            if k in ch:
                row[k] = str(ch[k] or "").strip()[:1500]
        if "on_missing" in ch:
            om = str(ch["on_missing"] or "").strip().lower()
            if om and om not in ("skip", "say"):
                return {"status": "refused", "reason": "on_missing must be skip or say"}
            if om:
                row["on_missing"] = om
            else:
                row.pop("on_missing", None)
        if "until" in ch:
            until = str(ch["until"] or "").strip()[:10]
            if until:
                try:
                    datetime.strptime(until, "%Y-%m-%d")
                except ValueError:
                    return {"status": "refused", "reason": "until must be YYYY-MM-DD"}
                row["until"] = until
            else:
                row.pop("until", None)
        if "space" in ch:
            sp = str(ch["space"] or "").strip()
            if sp:
                space, _t = team_space(sp)
                if not space:
                    return {"status": "refused", "reason": "I don't see that team space; name it as it shows in Chat"}
                row["space"], row["to"] = space, "space"
            else:
                row.pop("space", None)
        if not (row.get("say") or row.get("prompt")):
            return {"status": "refused", "reason": "a schedule needs say or prompt"}
        row["changed"] = core.stamp()
        core.save(FILE, d)
        what = ", ".join(f"{k}={describe(v) if k == 'when' and isinstance(v, dict) else v}" for k, v in ch.items())
        cid = core.log_change("routine_change", f"changed schedule {row['id']}: {row['title']} ({what})",
                              {"op": "routine_restore", "id": row["id"], "fields": old}, by)
        return {"status": "applied", "id": row["id"], "change": cid, "next_due": row.get("next_due", "")}


def restore(sid: str, fields: dict, now: datetime | None = None) -> bool:
    """Undo of a change: the old fields back (a missing old value removes the field)."""
    now = now or datetime.now()
    with core.locked:
        d = _load()
        for r in d["routines"]:
            if r["id"] == sid:
                for k, v in (fields or {}).items():
                    if k not in _CHANGEABLE:
                        continue
                    if v is None:
                        r.pop(k, None)
                    else:
                        r[k] = v
                if "when" in (fields or {}) and r.get("active", True):
                    try:
                        nxt = next_due(r["when"], now)
                        r["next_due"] = nxt.strftime(FMT) if nxt else r.get("next_due", "")
                    except (ValueError, KeyError, IndexError, TypeError):
                        pass
                core.save(FILE, d)
                return True
    return False


def disable(sid, by: str = "", ledger: bool = True) -> bool:
    with core.locked:
        d = _load()
        sid = str((find(sid, d["routines"]) or {}).get("id") or (sid if isinstance(sid, str) else ""))
        for r in d["routines"]:
            if r["id"] == sid and r.get("active", True):
                r["active"] = False
                r["stopped"] = core.stamp()
                core.save(FILE, d)
                if ledger:
                    core.log_change("routine_off", f"stopped schedule {sid}: {r['title']}", {"op": "routine_on", "id": sid}, by)
                return True
    return False


def enable(sid: str, now: datetime | None = None) -> bool:
    """Undo of a stop: re-arm from now. False when it is active already or a one-off whose time has passed."""
    now = now or datetime.now()
    with core.locked:
        d = _load()
        for r in d["routines"]:
            if r["id"] == sid and not r.get("active", True):
                try:
                    nxt = next_due(r["when"], now)
                except (ValueError, KeyError, IndexError, TypeError):
                    return False
                if nxt is None:
                    return False
                r["active"] = True
                r["next_due"] = nxt.strftime(FMT)
                r.pop("stopped", None)
                core.save(FILE, d)
                return True
    return False


def active() -> list:
    return [r for r in _load()["routines"] if r.get("active", True)]


def render() -> str:
    rs = active()
    parts = []
    if rs:
        parts.append("\n".join(["ACTIVE SCHEDULES (yours; stop one with unschedule):"] +
                                [f"- [{r['id']}] {r['title']} ({describe(r['when'])}, {_where(r)}"
                                 + ("" if is_event(r) else f", next {r['next_due']}")
                                 + (f", until {r['until']}" if r.get("until") else "") + ")" for r in rs]))
    try:                                   # the system's standing jobs the owners may switch (switches.py, 2026-10-23)
        import switches
        sw = switches.render()
    except Exception:  # noqa: BLE001
        sw = ""
    if sw:
        parts.append(sw)
    return "\n\n".join(parts)


def _quiet(now: datetime) -> bool:
    q = core.profile().get("quiet_hours") or [21, 7]
    return now.hour >= q[0] or now.hour < q[1]


def _routine_card(r: dict, sug: dict) -> list:
    """The ONE draft card a prompt routine may put up: its `task` (nothing else in the SUGGEST is honoured), applied
    as the owner who set the routine up, through the same guards as a card asked for in chat, and registered as a
    promise so a card that never arrives is said (commitments.py). -> lines the reply must carry (a refusal). Never
    raises: a routine's message still goes out when its card could not."""
    task = sug.get("task") if isinstance(sug, dict) else None
    task = task[0] if isinstance(task, list) and task else task
    if not isinstance(task, dict) or not str(task.get("text") or "").strip():
        return []
    try:
        import actions
        import commitments
        person = core.person_for(handle=str(r.get("by") or ""))
        if not core.is_owner(person):
            return []
        ctx = {"convo": f"dm:{r['to'] if r.get('to') not in ('both', '') else r.get('by')}", "thread": ""}
        lines = actions.apply({"task": [task]}, person, ctx)
        commitments.register_applied(list(actions.LAST_APPLIED), person, ctx)
        return [str(x).strip() for x in lines or [] if str(x).strip()]
    except Exception as e:  # noqa: BLE001
        core.log_change("routine_card_error", f"{r.get('id')}: {type(e).__name__}: {str(e)[:120]}", {"op": "none"}, "code")
        return []


# ------------------------------------------------------------------ events (2026-11-09)
def _scripts():
    p = str(Path(__file__).resolve().parent.parent / "scripts")
    if p not in sys.path:
        sys.path.insert(0, p)


def _sources() -> list:
    """[(key, path)] of the append-only files events are read from."""
    out = []
    try:
        _scripts()
        import exec_store as es
        out.append(("ledger", es.LEDGER))
    except Exception:  # noqa: BLE001
        pass
    try:
        import worklog
        out.append(("worklog", worklog.FILE))
    except Exception:  # noqa: BLE001
        pass
    return out


def _new_rows(cursor: dict) -> list:
    """The rows written since the cursor, oldest first, each tagged with its source; the cursor moves to the end.
    A source never seen before (or a file that shrank) only sets its cursor: nothing from the past is replayed."""
    out = []
    for key, path in _sources():
        try:
            lines = path.read_text(encoding="utf-8-sig").splitlines() if path.exists() else []
        except OSError:
            continue
        n = len(lines)
        was = cursor.get(key)
        if was is None or int(was) > n:
            cursor[key] = n
            continue
        for ln in lines[int(was):n]:
            try:
                row = json.loads(ln)
            except ValueError:
                continue
            if isinstance(row, dict):
                row["_src"] = key
                out.append(row)
        cursor[key] = n
    return out


def _card_header(qid: str, cache: dict) -> str:
    if qid in cache:
        return cache[qid]
    hdr = ""
    try:
        _scripts()
        import cards
        for folder in (cards.LIVE, cards.DONE, cards.ARCHIVE):
            c = cards._read(folder / f"{qid}.json") or {}
            if c:
                hdr = str(((c.get("questions") or [{}])[0] or {}).get("header") or "")
                break
    except Exception:  # noqa: BLE001
        pass
    cache[qid] = hdr
    return hdr


def _ticket_kind(tid: str, cache: dict) -> str:
    if "_fold" not in cache:
        try:
            _scripts()
            import exec_store as es
            cache["_fold"] = es.fold()
        except Exception:  # noqa: BLE001
            cache["_fold"] = {}
    return str((cache["_fold"].get(tid) or {}).get("kind") or "")


_KIND_WORDS = {"email_send": "an email", "imessage_send": "a text", "agent_run": "a browser job", "calendar_hold": "a calendar hold"}


def _event_of(row: dict, cache: dict) -> dict:
    """One ledger or work-log row -> {"on", "text"} (what an owner would call it), or {} when it is nothing to them."""
    src, ev = row.get("_src"), str(row.get("event") or "")
    if src == "worklog":
        if str(row.get("kind") or "") == "routine":          # a routine's own posting is never an event (no loops)
            return {}
        return {"on": "work", "text": str(row.get("what") or "")}
    if ev == "card_post":
        hdr = _card_header(str(row.get("qid") or ""), cache)
        return {"on": "card", "text": f"a card came up: '{hdr}'" if hdr else "a card came up"}
    if ev == "card_answer":
        hdr, opt = _card_header(str(row.get("qid") or ""), cache), str(row.get("option") or "").replace(" (Recommended)", "")
        on = "declined" if _NO.match(opt) else "approved"
        return {"on": on, "text": f"'{hdr}' was {'declined' if on == 'declined' else 'approved'} ({opt})"}
    if ev == "card_auto":
        hdr, opt = _card_header(str(row.get("qid") or ""), cache), str(row.get("option") or "").replace(" (Recommended)", "")
        return {"on": "approved", "text": f"'{hdr}' was approved by a standing yes ({opt})"}
    if ev == "status":
        st = str(row.get("status") or "")
        kind = _ticket_kind(str(row.get("ticket") or ""), cache)
        what = _KIND_WORDS.get(kind, kind.replace("_", " ") or "an action")
        if st == "done":
            return {"on": "done", "text": f"{what} went through: {str(row.get('result') or '')[:120]}"}
        if st in ("failed", "error"):
            return {"on": "failed", "text": f"{what} failed: {str(row.get('result') or '')[:120]}"}
        return {}
    if ev == "grant":
        return {"on": "grant", "text": f"a standing yes was given on '{str(row.get('key') or '').split('|')[0]}' ({row.get('scope')})"}
    return {}


def _sample_event(r: dict) -> dict:
    on = str((r.get("when") or {}).get("on") or "card")
    words = str((r.get("when") or {}).get("match") or "something")
    return {"on": on, "text": {"card": f"a card came up: '{words}'", "approved": f"'{words}' was approved",
                               "declined": f"'{words}' was declined", "done": f"{words} went through", "failed": f"{words} failed",
                               "grant": f"a standing yes was given on '{words}'", "work": words}.get(on, words)}


def _event_words(e: dict) -> str:
    return str((e or {}).get("text") or "").strip().rstrip(".")


def _matches(r: dict, e: dict) -> bool:
    when = r.get("when") or {}
    if str(when.get("on") or "").lower() != e.get("on"):
        return False
    words = [w for w in re.sub(r"[^\w\s'-]", " ", str(when.get("match") or "").lower()).split() if w]
    text = str(e.get("text") or "").lower()
    return all(w in text for w in words)


def _tick_events(d: dict, post, post_space, now: datetime, run_prompt, llm_today: int) -> tuple:
    """Fire the event routines on what was recorded since the last tick. -> (fired, touched ids, llm_today, changed)."""
    evs = [r for r in d["routines"] if r.get("active", True) and is_event(r)]
    cursor = core.load(EVENTS_CURSOR, {}) or {}
    rows = _new_rows(cursor)
    core.save(EVENTS_CURSOR, cursor)
    if not evs or not rows:
        return 0, set(), llm_today, False
    cache: dict = {}
    events = [e for e in (_event_of(r, cache) for r in rows) if e]
    fired, touched, changed = 0, set(), False
    name = core.profile().get("name", "Agent")
    for r in evs:
        hits = [e for e in events if _matches(r, e)]
        if not hits:
            continue
        if r.get("until") and now.strftime("%Y-%m-%d") > r["until"]:
            r["active"], r["stopped"] = False, now.strftime(FMT) + " (done)"
            changed = True
            touched.add(r["id"])
            continue
        if _idle_paused(r, now):
            _history_add(r["id"], True, "", note="held: the owner has been away", now=now)
            touched.add(r["id"])
            continue
        shown, more = hits[:MAX_EVENT_FIRES], len(hits) - min(len(hits), MAX_EVENT_FIRES)
        happened = "; ".join(_event_words(e) for e in shown) + (f"; and {more} more like it" if more > 0 else "")
        if r.get("say"):
            text = f"{r['title']}: {r['say']}" if r["say"].lower() != r["title"].lower() else r["say"]
            text += f"\n({happened[0].upper() + happened[1:]}.)"
        else:
            if llm_today >= int((core.profile().get("routines") or {}).get("llm_runs_per_day_max", 12)):
                _history_add(r["id"], False, "", note="over today's budget; the event passed", now=now)
                touched.add(r["id"])
                continue
            if run_prompt is None:
                import brain
                run_prompt = lambda p: brain.run_once(p)  # noqa: E731
            res = run_prompt(f"[Event routine {r['id']} '{r['title']}', {now.strftime('%Y-%m-%d %H:%M %a')}]\n{r['prompt']}\n"
                             f"What just happened: {happened}.\nReply with the message for {r['to']} only. No SUGGEST line.")
            llm_today += 1
            core.save(RUNS, {now.strftime('%Y-%m-%d'): llm_today})
            if not res.get("ok"):
                r["last_error"] = str(res.get("error"))[:200]
                _history_add(r["id"], False, "", note=r["last_error"], now=now)
                changed = True
                touched.add(r["id"])
                continue
            import brain as _b
            said, _sug = _b.split_suggest(res.get("text", ""))
            if _NOTHING.match(said or "") and str(r.get("on_missing") or "skip") == "skip":
                _history_add(r["id"], True, said, note="nothing to post", now=now)
                touched.add(r["id"])
                continue
            text = f"{r['title']}\n{said}"
        if r.get("space"):
            if post_space is None:
                continue
            post_space(r["space"], text)
            core.say(r["space"], name, text, channel="routine", meta={"routine": r["id"]})
        else:
            targets = list(core.owners() or (core.people().get("users") or {})) if r["to"] == "both" else [r["to"]]
            for t in targets:
                post(t, text)
                core.say(f"dm:{t}", name, text, channel="routine", meta={"routine": r["id"]})
        r["runs"] = int(r.get("runs", 0)) + 1
        r["last_run"] = now.strftime(FMT)
        _history_add(r["id"], True, text, note=f"{len(hits)} event(s)", now=now)
        try:
            import worklog
            worklog.add(f"routine '{r['title']}' posted {_where(r)} on an event", lane="", ref=r["id"], by="routine", kind="routine")
        except Exception:  # noqa: BLE001
            pass
        fired += 1
        changed = True
        touched.add(r["id"])
    return fired, touched, llm_today, changed


def tick(post, now: datetime | None = None, run_prompt=None, post_space=None) -> int:
    """Fire every due routine through post(person_key, text), a team-space one through
    post_space(space, text). Returns how many fired."""
    now = now or datetime.now()
    if _quiet(now):
        return 0                                           # due items wait for morning, they are not lost
    d = _load()
    runs = core.load(RUNS, {})
    day = now.strftime("%Y-%m-%d")
    llm_today = int(runs.get(day, 0))
    fired, changed = 0, False
    touched = set()
    try:
        ef, et, llm_today, ec = _tick_events(d, post, post_space, now, run_prompt, llm_today)
        fired, changed = fired + ef, changed or ec
        touched |= et
    except Exception as e:  # noqa: BLE001
        core.log_change("routine_event_error", f"{type(e).__name__}: {str(e)[:120]}", {"op": "none"}, "code")
    for r in d["routines"]:
        if not r.get("active", True) or r.get("next_due", "9999") > now.strftime(FMT):
            continue
        if r.get("until") and now.strftime("%Y-%m-%d") > r["until"]:
            r["active"] = False                            # its last day has passed: ends without a post
            r["stopped"] = now.strftime(FMT) + " (done)"
            changed = True
            touched.add(r["id"])
            continue
        name = core.profile().get("name", "Agent")
        text = ""
        if _idle_paused(r, now):
            # the owner it is for has not written in a while: this occurrence is skipped (not fired, not shifted into the
            # night); the routine keeps its natural next time, and the hold is in its history
            try:
                skip_to = next_due(r["when"], now)
            except (ValueError, KeyError, IndexError, TypeError):
                skip_to = None
            r["next_due"] = (skip_to or (now + timedelta(hours=6))).strftime(FMT)
            _history_add(r["id"], True, "", note="held: the owner has been away", now=now)
            changed = True
            touched.add(r["id"])
            continue
        # re-arm BEFORE the run (upgrade 5): a prompt routine holds a model turn for minutes; a bridge that dies inside it
        # used to leave next_due in the past and fire the routine again on restart. The next time is written first.
        pre = next_due(r["when"], now)
        if pre is not None and r.get("until") and pre.strftime("%Y-%m-%d") > r["until"]:
            pre = None
        if not r.get("say") and not r.get("space"):
            with core.locked:
                cur = _load()
                for row in cur["routines"]:
                    if row["id"] == r["id"] and row.get("active", True):
                        row["next_due"] = pre.strftime(FMT) if pre is not None else row.get("next_due", "")
                        row["last_started"] = now.strftime(FMT)
                core.save(FILE, cur)
        if r.get("space"):
            if post_space is None:
                continue                                   # no way into that space this tick: stays due
            post_space(r["space"], r["say"])               # addressed to a person by name ("Name, ..."), no title
            core.say(r["space"], name, r["say"], channel="routine", meta={"routine": r["id"]})
        elif r.get("say"):
            text = f"{r['title']}: {r['say']}" if r["say"].lower() != r["title"].lower() else r["say"]
        else:
            if llm_today >= int((core.profile().get("routines") or {}).get("llm_runs_per_day_max", 12)):
                continue                                   # over today's budget: stays due, runs tomorrow
            if run_prompt is None:
                import brain
                run_prompt = lambda p: brain.run_once(p)  # noqa: E731
            # a routine an owner set up to DRAFT a message ("ask him for today's update") may put up that one draft
            # card: until 2026-11-04 the turn was told "No SUGGEST line", so it could only say "the system refused
            # it, so no card is up", every morning. A card is not a send: the owner's tap still is.
            may_card = core.is_owner(core.person_for(handle=str(r.get("by") or "")))
            res = run_prompt(f"[Scheduled routine {r['id']} '{r['title']}', {now.strftime('%Y-%m-%d %H:%M %a')}]\n"
                             f"{r['prompt']}\nReply with the message for {r['to']} only. "
                             + ("A SUGGEST line is allowed ONLY when this routine asks for a message to someone to be drafted: "
                                "one `task` with its `draft`, which puts the card up for the owner's tap. No other SUGGEST key; "
                                "never say a card is up unless you gave that `task`." if may_card else "No SUGGEST line.")
                             + " If this check is no longer needed (what it was for has happened or its day has passed), say so "
                             "in one line and end your reply with a last line that is exactly ROUTINE_DONE: that stops this "
                             "schedule. Never say you tried to stop it or could not.")
            llm_today += 1
            runs = {day: llm_today}
            core.save(RUNS, runs)
            if not res.get("ok"):
                r["last_error"] = str(res.get("error"))[:200]
                r["next_due"] = (now + timedelta(minutes=30)).strftime(FMT)   # retry later, never spin
                _history_add(r["id"], False, "", note=r["last_error"], now=now)
                changed = True
                touched.add(r["id"])
                continue
            import brain as _b
            said, sug = _b.split_suggest(res.get("text", ""))
            if _SELF_DONE.search(said or ""):
                said = _SELF_DONE.sub("", said).strip()
                pre = None                                 # the routine said its reason is gone: this run is its last
                _history_add(r["id"], True, "", note="ended itself: no longer needed", now=now)
            if _NOTHING.match(said or "") and str(r.get("on_missing") or "skip") == "skip":
                # nothing to say today: post nothing (on_missing=skip, the default); the run still counts and shows in history
                _history_add(r["id"], True, said, note="nothing to post", now=now)
                r["runs"] = int(r.get("runs", 0)) + 1
                r["last_run"] = now.strftime(FMT)
                if pre is None:
                    r["active"] = False
                    r["stopped"] = now.strftime(FMT) + " (done)"
                else:
                    r["next_due"] = pre.strftime(FMT)
                changed = True
                touched.add(r["id"])
                continue
            more = _routine_card(r, sug) if may_card else []
            text = f"{r['title']}\n{said}" + ("\n\n" + " ".join(more) if more else "")
        targets = ([] if r.get("space") else
                   list(core.owners() or (core.people().get("users") or {})) if r["to"] == "both" else [r["to"]])
        for t in targets:
            post(t, text)
            core.say(f"dm:{t}", name, text, channel="routine", meta={"routine": r["id"]})
        r["runs"] = int(r.get("runs", 0)) + 1
        r["last_run"] = now.strftime(FMT)
        _history_add(r["id"], True, text or r.get("say", ""), now=now)
        try:
            import worklog
            worklog.add(f"routine '{r['title']}' posted {_where(r)}", lane="", ref=r["id"], by="routine", kind="routine")
        except Exception:  # noqa: BLE001
            pass
        nxt = pre
        if nxt is None:
            r["active"] = False
            r["stopped"] = now.strftime(FMT) + " (done)"
        else:
            r["next_due"] = nxt.strftime(FMT)
        fired += 1
        touched.add(r["id"])
    if fired or changed:                                   # an idle minute must not rewrite a tracked file
        # a prompt routine holds a model turn between the load above and here: merge only the routines
        # this tick touched into a fresh read, so a schedule added or stopped meanwhile survives
        mine = {r["id"]: r for r in d["routines"] if r["id"] in touched}
        keys = ("runs", "last_run", "next_due", "active", "stopped", "last_error", "last_started")
        with core.locked:
            cur = _load()
            for r in cur["routines"]:
                m = mine.get(r["id"])
                if m and r.get("active", True):            # stopped meanwhile: the stop wins
                    for k in keys:
                        if k in m:
                            r[k] = m[k]
            core.save(FILE, cur)
    return fired


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    a = sys.argv[1:]
    if a[:1] == ["off"] and len(a) == 2:
        print("stopped" if disable(a[1], by="cli") else "no such active schedule")
    elif a[:1] == ["change"] and len(a) == 3:
        import json as _json
        print(update(a[1], _json.loads(a[2]), by="cli"))
    elif a[:1] == ["test"] and len(a) == 2:
        import json as _json
        print(_json.dumps(test(a[1]), indent=1, ensure_ascii=False))
    elif a[:1] == ["history"] and len(a) == 2:
        for h in history(a[1]):
            print(f"{h['ts']}  {'ok ' if h.get('ok') else 'ERR'}  {h.get('note') or h.get('text', '')[:100]}")
    else:
        print(render() or "(no schedules)")

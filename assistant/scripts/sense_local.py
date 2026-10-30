"""sense_local.py - calendar + wedding-project sensors. Zero-LLM, read-only.

calendar : fetches each configured *secret iCal URL* (read-only by nature; stored in
           Windows Credential Manager by setup_creds.py) and, when no feed is readable,
           the owners' Google calendars through Sage's read token (assistant/agent/
           google_api.py). Writes the next N days to data/calendar.json for the morning
           brief. A wedding-looking event is labelled booked / inquiry / unconfirmed from
           the CRM's own lead-vs-client status (not every wedding on the calendar is a
           booking: the CRM's calendar sync puts leads there too), and only a BOOKED one
           tickets shoot prep.
clash    : a booked wedding whose date falls inside an owner's all-day / multi-day / long
           event (a trip) posts its own card, "Oct 10 Glenrock: you are on Catalina. Who
           shoots?", once per wedding+date. Zero-LLM.
           Recurring events (RRULE DAILY/WEEKLY/MONTHLY/YEARLY, INTERVAL/COUNT/UNTIL/
           BYDAY, EXDATE) are expanded into the lookahead window; exotic rules fall
           back to the first date only.
projects : reads scripts/projects/project_state.json (the delivery-tracker ledger) and
           tickets deliveries that are due soon / overdue and shoots entering prep.

  sense_local.py            -> runs both sensors (calendar includes the clash check)
"""
import re
import sys
import urllib.request
from datetime import date, datetime, timedelta
from pathlib import Path

from alib import ensure_venv
ensure_venv()

import tickets
import crm
from alib import DATA, VAULT, config, load_json, now_iso, save_json, secret


def _unfold(text):
    return re.sub(r"\r?\n[ \t]", "", text)


def _parse_dt(val):
    m = re.match(r"(\d{4})(\d{2})(\d{2})(?:T(\d{2})(\d{2}))?", val)
    if not m:
        return None, None
    d = date(int(m[1]), int(m[2]), int(m[3]))
    return d, (f"{m[4]}:{m[5]}" if m[4] else None)


_WD = {"MO": 0, "TU": 1, "WE": 2, "TH": 3, "FR": 4, "SA": 5, "SU": 6}
_MAX_OCCURRENCES = 400


def _add_months(d, n):
    m = d.month - 1 + n
    y, m = d.year + m // 12, m % 12 + 1
    try:
        return d.replace(year=y, month=m)
    except ValueError:            # e.g. the 31st in a 30-day month: skip that month
        return None


def expand_rrule(first, rrule, win_start, win_end, exdates=()):
    """Occurrence dates of a simple RRULE inside [win_start, win_end].
    Supports FREQ=DAILY|WEEKLY|MONTHLY|YEARLY with INTERVAL, COUNT, UNTIL and (weekly)
    BYDAY. Anything fancier falls back to the first date only, never an error."""
    r = {}
    for part in rrule.split(";"):
        k, _, v = part.partition("=")
        r[k.upper()] = v.upper()
    freq, interval = r.get("FREQ"), max(1, int(r.get("INTERVAL", "1") or 1))
    until = _parse_dt(r["UNTIL"])[0] if r.get("UNTIL") else None
    count = int(r["COUNT"]) if r.get("COUNT", "").isdigit() else None
    if freq not in ("DAILY", "WEEKLY", "MONTHLY", "YEARLY"):
        return [first] if win_start <= first <= win_end else []
    days = sorted({_WD[x] for x in r.get("BYDAY", "").split(",") if x in _WD}) if freq == "WEEKLY" else []
    out, n, i = [], 0, 0
    if not count and win_start > first:          # skip straight to the window (COUNT needs every step)
        gap = (win_start - first).days
        step = {"DAILY": 1, "WEEKLY": 7, "MONTHLY": 31, "YEARLY": 366}[freq] * interval   # longest unit: land before the window, never past it
        i = max(0, gap // step - 1)
    end_i = i + _MAX_OCCURRENCES
    while i < end_i:
        if freq == "DAILY":
            d = first + timedelta(days=i * interval)
            cands = [d]
        elif freq == "WEEKLY":
            week0 = first - timedelta(days=first.weekday()) + timedelta(weeks=i * interval)
            cands = [week0 + timedelta(days=w) for w in days] if days else [first + timedelta(weeks=i * interval)]
            cands = [c for c in cands if c >= first]
        elif freq == "MONTHLY":
            d = _add_months(first, i * interval)
            cands = [d] if d else []
        else:
            d = _add_months(first, 12 * i * interval)
            cands = [d] if d else []
        i += 1
        if not cands:                 # short month, or a first partial week with no BYDAY hit
            continue
        stop = False
        for c in cands:
            if until and c > until:
                stop = True
                break
            n += 1
            if count and n > count:
                stop = True
                break
            if c > win_end:
                stop = True
                break
            if c >= win_start and c not in exdates:
                out.append(c)
        if stop:
            break
    return out


def parse_ics(text, win_start=None, win_end=None):
    """Events as dicts. With a window, recurring events are expanded into it; without
    one, every event (recurring or not) is listed on its first date only."""
    events = []
    for block in re.findall(r"BEGIN:VEVENT(.*?)END:VEVENT", _unfold(text), re.S):
        f, exdates = {}, set()
        for line in block.strip().splitlines():
            k, _, v = line.partition(":")
            key = k.split(";")[0].upper()
            if key == "EXDATE":
                exdates.update(x for x in (_parse_dt(p)[0] for p in v.split(",")) if x)
                continue
            f.setdefault(key, v.strip())
        d, tm = _parse_dt(f.get("DTSTART", ""))
        if not d:
            continue
        base = {"time": tm, "uid": f.get("UID", ""),
                "title": f.get("SUMMARY", "(untitled)").replace("\\,", ","),
                "location": f.get("LOCATION", "").replace("\\,", ",")}
        if f.get("RRULE") and win_start and win_end:
            dates = expand_rrule(d, f["RRULE"], win_start, win_end, exdates)
        else:
            dates = [d]
        for od in dates:
            events.append(dict(base, date=od.isoformat(), recurring=bool(f.get("RRULE"))))
    return events


TERMINAL_STAGES = ("delivered", "archived", "cancelled")
NOT_BOOKED_STAGES = TERMINAL_STAGES + ("inquiry",)     # an inquiry is a lead: no prep, no couple follow-ups
CLASH_DAYS = 60
AWAY_MIN_HOURS = 6                                     # a timed event this long counts as being away (a hike day)
CLASH_LATCH = DATA / "clash_latch.json"


def _tokens(s):
    return {w for w in re.sub(r"[^a-z]", " ", str(s or "").lower()).split() if len(w) > 2} - {
        "project", "wedding", "the", "and", "shoot", "ceremony", "reception"}


def classify_booking(ev, snap_projects):
    """('booked' | 'inquiry' | 'unconfirmed', CRM project name) for a wedding-looking
    calendar event, from the CRM's workspace status (client = signed or paid). Matched on
    the date plus a name word in the title; a lone CRM project on that date also
    matches. No match, or a snapshot without status, is 'unconfirmed': never a booking."""
    same_day = [p for p in snap_projects if str(p.get("date") or "")[:10] == ev["date"]]
    words = _tokens(ev.get("title"))
    hits = [p for p in same_day if _tokens(p.get("name")) & words] or (same_day if len(same_day) == 1 else [])
    sts = {p.get("status") for p in hits}
    if sts & {"client", "client_archived"}:
        return "booked", next(p.get("name") for p in hits if p.get("status") in ("client", "client_archived"))
    if sts & {"lead", "lead_sent"}:
        return "inquiry", hits[0].get("name")
    return "unconfirmed", ""


def google_events(days):
    """The owners' Google calendars as calendar.json rows (Sage's token; reading only).
    -> (rows, note)."""
    try:
        saved = list(sys.path)          # google_api puts assistant/agent FIRST on the path, where its
        sys.path.append(str(Path(__file__).resolve().parent.parent / "agent"))   # followups.py would shadow ours
        try:
            import google_api
        finally:
            sys.path[:] = saved
        evs = google_api.events(days=days, who="both")
    except Exception as e:  # noqa: BLE001
        return [], f"google=[ERROR {type(e).__name__}]"
    rows, errs = [], [e for e in evs if e.get("error")]
    for e in evs:
        if e.get("error") or not e.get("start"):
            continue
        s, en = str(e["start"]), str(e.get("end") or e["start"])
        d0 = s[:10]
        if e.get("all_day"):          # an all-day end date is exclusive
            d1 = (date.fromisoformat(en[:10]) - timedelta(days=1)).isoformat() if en[:10] > d0 else d0
            hours = 24.0 * ((date.fromisoformat(d1) - date.fromisoformat(d0)).days + 1)
        else:
            d1 = en[:10]
            try:
                hours = (datetime.fromisoformat(en) - datetime.fromisoformat(s)).total_seconds() / 3600
            except ValueError:
                hours = 0.0
        rows.append({"date": d0, "end_date": d1, "time": None if e.get("all_day") else s[11:16],
                     "uid": str(e.get("id") or ""), "title": e.get("title") or "(no title)",
                     "location": e.get("location") or "", "owner": e.get("owner") or "",
                     "all_day": bool(e.get("all_day")), "hours": round(hours, 1), "feed": f"google:{e.get('account')}"})
    note = f"google=[ok events={len(rows)}]" if rows or not errs else f"google=[ERROR {errs[0]['error'][:40]}]"
    return rows, note


def sense_calendar():
    cfg = config()["calendar"]
    today, horizon = date.today(), date.today() + timedelta(days=cfg["lookahead_days"])
    upcoming, notes, items = [], [], []
    snap_projects = crm.snapshot().get("projects") or []
    for feed in cfg["feeds"]:
        if not feed.get("enabled"):
            continue
        url = secret(feed["secret"])
        if not url:
            notes.append(f"{feed['name']}=[NOT_CONFIGURED]")
            continue
        try:
            with urllib.request.urlopen(urllib.request.Request(url, headers={"User-Agent": "owner1-assistant"}), timeout=45) as r:
                evs = parse_ics(r.read().decode("utf-8", "replace"), today, horizon)
        except Exception as e:
            notes.append(f"{feed['name']}=[ERROR {type(e).__name__}]")
            continue
        win = [e for e in evs if today.isoformat() <= e["date"] <= horizon.isoformat()]
        for e in win:
            e["feed"] = feed["name"]
        upcoming += win
        notes.append(f"{feed['name']}=[ok events={len(win)}]")
    away = []
    if not any(n.split("=", 1)[1].startswith("[ok") for n in notes):
        # no iCal feed readable (none was ever stored): read the owners' Google calendars, which
        # Sage can already see - the sensor logged personal=[NOT_CONFIGURED] every tick until 10/06
        rows, note = google_events(max(cfg["lookahead_days"], CLASH_DAYS))
        notes.append(note)
        away = rows
        upcoming += [e for e in rows if e["date"] <= horizon.isoformat()]
    for e in upcoming + [r for r in away if r not in upcoming]:
        if any(w in e["title"].lower() for w in cfg["wedding_words"]):
            e["booking"], e["crm_project"] = classify_booking(e, snap_projects)
            days = (date.fromisoformat(e["date"]) - today).days
            if 0 <= days <= cfg["shoot_prep_days"] and e["booking"] == "booked" and e["date"] <= horizon.isoformat():
                items.append({"source": "calendar", "source_id": f"prep|{e['uid']}|{e['date']}", "lane": "wedding",
                              "kind": "shoot_prep", "title": f"Shoot prep: {e['title']} on {e['date']}",
                              "summary": f"{days} days out. Location: {e['location'] or 'n/a'}. Run the shoot-prep skill.",
                              "payload": e})
    upcoming.sort(key=lambda e: (e["date"], e["time"] or ""))
    try:
        clash_n = check_clashes(away, today)
    except Exception as e:  # noqa: BLE001        a clash check must never take the calendar sensor down
        clash_n = f"ERROR-{type(e).__name__}"
    notes.append(f"clashes={clash_n}")
    # "feeds" says which calendars were actually read: an empty event list from an unconnected
    # calendar is not "nothing scheduled" (the brief said "calendar connected, 0 events", 2026-10-02)
    feeds = dict(n.split("=", 1) for n in notes if "=" in n and not n.startswith("clashes="))
    save_json(DATA / "calendar.json", {"ts": now_iso(), "events": upcoming, "feeds": feeds,
                                       "connected": any(v.startswith("[ok") for v in feeds.values())})
    added = tickets.add_many(items, "sense.calendar") if items else 0
    print(f"SENSE_RESULT calendar new={max(added, 0)} " + " ".join(notes))


def sense_projects():
    cfg = config()["projects"]
    projects = load_json(VAULT / cfg["state_file"], {}).get("projects", {})
    today, items = date.today(), []
    for key, p in projects.items():
        if not isinstance(p, dict) or p.get("stage") in NOT_BOOKED_STAGES:
            continue
        name = p.get("couple") or key
        for field, kind, warn, verb in (("promised_delivery", "delivery_due", cfg["delivery_warn_days"], "Delivery"),
                                        ("wedding_date", "shoot_prep", cfg["shoot_prep_days"], "Shoot")):
            try:      # the booking-confirmation skill used to write "date"; accept it as the wedding date
                d = date.fromisoformat(str(p.get(field) or (p.get("date") if field == "wedding_date" else "") or "")[:10])
            except ValueError:
                continue
            days = (d - today).days
            if kind == "shoot_prep" and days < 0:
                continue
            if days <= warn:
                # bucket the id so an overdue delivery re-tickets weekly instead of once forever
                bucket = "overdue-w%s" % today.strftime("%G%V") if days < 0 else "due"
                items.append({"source": "projects", "source_id": f"{kind}|{key}|{d}|{bucket}", "lane": "wedding", "kind": kind,
                              "title": f"{verb} {'OVERDUE' if days < 0 else 'in'} {abs(days)}d: {name} ({d})",
                              "summary": f"stage={p.get('stage', '?')}. Use the {'delivery-tracker' if kind == 'delivery_due' else 'shoot-prep'} skill.",
                              "payload": {"project": key, field: str(d), "stage": p.get("stage")}})
    added = tickets.add_many(items, "sense.projects") if items else 0
    armed = arm_lifecycle(projects, cfg)
    print(f"SENSE_RESULT projects new={max(added, 0)} tracked={len(projects)} followups_armed={armed}")


def arm_lifecycle(projects, cfg):
    """Every tracked, undelivered wedding arms its lifecycle follow-ups (idempotent).
    Delivery anchor = promised_delivery when set, else wedding_date + the default
    turnaround (an assumption the owner confirms on the card, never a promise)."""
    try:
        import followups
    except Exception:
        return 0
    weeks = int(cfg.get("default_turnaround_weeks", 8))
    n = 0
    for key, p in projects.items():
        if not isinstance(p, dict):
            continue
        if p.get("stage") == "inquiry":
            # still a lead in the CRM (no signed contract or payment): no timeline request,
            # no week-of confirmation to a couple who has not booked. Held, not dismissed, so
            # the chain re-arms once the CRM re-stages it "booked".
            followups.cancel_unit("wedding-lifecycle", key, "still an inquiry in the CRM", hold=True)
            continue
        if p.get("stage") in TERMINAL_STAGES:
            # stop the chain that was armed while the project was live. A delivered film keeps
            # its review request (armed on the delivery date by the delivery step).
            keep = ("review-request",) if p.get("stage") == "delivered" else ()
            followups.cancel_unit("wedding-lifecycle", key, f"project is {p.get('stage')}", keep_steps=keep)
            continue
        wd = str(p.get("wedding_date") or p.get("date") or "")[:10]
        try:
            wdate = date.fromisoformat(wd)
        except ValueError:
            continue
        couple = p.get("couple") or key
        base = {"unit": key, "couple": couple, "wedding_date": wd, "anchor": wd,
                "cancel": "python assistant/scripts/followups.py mark <id> --status done"}
        res = followups.arm_chain("wedding-lifecycle", base, "sense.projects",
                                  steps=["finishing-touches", "timeline-request", "questionnaire-reminder", "questionnaire-reminder-2",
                                         "checkin-text", "prep-checkin", "week-of-confirm", "ingest-gate", "thank-you", "edit-start"])
        n += sum(1 for r in res.values() if r.get("armed"))
        anchor = str(p.get("promised_delivery") or "")[:10]
        try:                                   # "TODO" or any non-date is "not set", never an anchor
            date.fromisoformat(anchor)
        except ValueError:
            anchor = (wdate + timedelta(weeks=weeks)).isoformat()
        res = followups.arm_chain("wedding-lifecycle", dict(base, anchor=anchor), "sense.projects", steps=["delivery-check"])
        n += sum(1 for r in res.values() if r.get("armed"))
    return n


def _away_label(e):
    return e["title"] if e["date"] == e["end_date"] else f"{e['title']} ({e['date'][5:]} to {e['end_date'][5:]})"


def check_clashes(away, today):
    """A BOOKED wedding (ledger stage not inquiry / terminal) whose date falls inside an owner's
    all-day, multi-day or long event posts ONE standalone card per wedding+date. An inquiry
    never clashes: an unsigned lead on a trip day is not a shoot. A guest trip to somebody
    else's wedding IS an away day; only an event matched to a CRM project is not."""
    if not away:
        return 0
    projects = load_json(VAULT / config()["projects"]["state_file"], {}).get("projects", {})
    latch = load_json(CLASH_LATCH, {})
    posted = 0
    for key, p in projects.items():
        if not isinstance(p, dict) or p.get("stage") in NOT_BOOKED_STAGES:
            continue
        if crm.status(p) in ("lead", "lead_sent") and not p.get("confirmed_by"):
            continue                   # the CRM says unsigned: the weekly drift ticket asks, a clash card would assume
        wd = str(p.get("wedding_date") or p.get("date") or "")[:10]
        try:
            wdate = date.fromisoformat(wd)
        except ValueError:
            continue
        if not 0 <= (wdate - today).days <= CLASH_DAYS:
            continue
        hits = [e for e in away if e["date"] <= wd <= e["end_date"] and (e["all_day"] or e["hours"] >= AWAY_MIN_HOURS)
                and e.get("booking") not in ("booked", "inquiry")      # a CRM shoot, not a trip
                and not (_tokens(e["title"]) & _tokens(p.get("couple") or key))]
        sid = f"clash|{key}|{wd}"
        if not hits or sid in latch:
            continue
        who = sorted({e["owner"] or "?" for e in hits})
        couple = re.sub(r"'s project$", "", str(p.get("couple") or key), flags=re.I)
        day = wdate.strftime("%a %b %d").replace(" 0", " ")
        names = " and ".join(o.capitalize() for o in who)
        q = [{"header": f"Shoot clash {wdate.strftime('%m/%d')}", "lane": "wedding", "subject": key,
              "question": (f"{day}: {couple}'s wedding is booked, but {names} "
                           f"{'is' if len(who) == 1 else 'are'} away: {'; '.join(_away_label(e) for e in hits[:3])}. "
                           "Who shoots it?"),
              "details": (f"Ledger: {key}, stage '{p.get('stage')}', {crm.name()} status '{crm.status(p) or '?'}'. "
                          "Calendar: " + "; ".join(f"{e['title']} {e['date']}..{e['end_date']} ({e['feed']})" for e in hits[:5])),
              "no_recommendation": "only the owners know who is free to cover it",
              "options": [{"label": "owner6 shoots it"}, {"label": "Hiring a shooter"},
                          {"label": "Trip moves"}, {"label": "Not a real booking"}]}]
        try:
            import cards
            r = cards.post(q, "sense.calendar", lane="wedding")
        except Exception as e:  # noqa: BLE001
            r = {"posted": False, "reason": f"{type(e).__name__}: {e}"}
        if r.get("posted"):
            latch[sid] = {"ts": now_iso(), "qid": r.get("qid")}
            posted += 1
        else:                          # the card path failed: an operator ticket, so a clash is never silent
            tickets.add_many([{"source": "calendar", "source_id": sid, "lane": "wedding", "kind": "wedding_clash",
                               "title": q[0]["question"][:200], "summary": "Post this as ONE decision card. " + q[0]["details"][:400],
                               "payload": {"project": key, "date": wd, "card_error": str(r.get("reason"))[:200]}}], "sense.calendar")
            latch[sid] = {"ts": now_iso(), "ticket": True}
        save_json(CLASH_LATCH, latch)
    return posted


if __name__ == "__main__":
    for fn in (sense_calendar, sense_projects):
        try:
            fn()
        except Exception as e:                       # a sensor must never kill the cron
            print(f"SENSE_RESULT {fn.__name__} ERROR {type(e).__name__}: {e}")

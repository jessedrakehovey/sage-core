"""scheduling.py - Sage sets up a call (ruling 12, 2026-10-15), zero-LLM.

A lead or a contact asks for a call ("can we hop on a call this week?"), or an owner says "set up
a call with example": this module reads BOTH owners' calendars (Google when connected, else the
calendar file the spine keeps), the HoneyBook meetings in the desk snapshot, our own holds and
the times already offered to someone else, picks 2-3 open slots on different days inside the
hours in tracking.json -> scheduling.windows, and posts ONE card carrying the drafted message with
those slots. The exec is whatever the thread dictates: `imessage_send` for a text, `email_send`
for mail, `hb_message` for a HoneyBook conversation. Nothing goes out without the tap.

When their reply picks one of the offered times ("Tuesday works", "the 2 pm one", "option 2"),
the tick posts the confirmation message as a card AND a `calendar_hold` card for the slot. A
reply that turns them all down is logged and left to the operator (their text is already a
ticket). Offers nobody answered expire after proposal_ttl_days and free their slots.

Who may trigger it: the operator (prompts/operator.md, "Scheduling a call") through the CLI, and
Sage through the SUGGEST `call` field (agent/actions.py). State: data/tracking/scheduling.json.

  python assistant/scripts/tracking/scheduling.py propose --ticket <id> [--dry-run]
  python assistant/scripts/tracking/scheduling.py propose --with "example example" [--channel text|email|honeybook]
         [--to <phone|email>] [--lane wedding] [--dry-run]
  python assistant/scripts/tracking/scheduling.py slots [--days 7]      # the open slots now
  python assistant/scripts/tracking/scheduling.py tick                  # picks, confirmations, expiries (the spine)
  python assistant/scripts/tracking/scheduling.py status
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from datetime import date, datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import advice  # noqa: E402
import common as C  # noqa: E402

AGENT = C.ROOT / "agent"
WEEKDAYS = ("monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday")
_ABBR = {"mon": 0, "tue": 1, "tues": 1, "wed": 2, "thu": 3, "thur": 3, "thurs": 3, "fri": 4, "sat": 5, "sun": 6}
_EMAIL = re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+")
_HEX24 = re.compile(r"[0-9a-f]{24}")


def scfg() -> dict:
    return C.cfg()["scheduling"]


def _state() -> dict:
    st = C.state("scheduling", {})
    st.setdefault("proposals", {})
    return st


def _save(st: dict) -> None:
    C.save_state("scheduling", st)


def _hm(s: str) -> tuple:
    return C.hm(s)


def _digits(v) -> str:
    return re.sub(r"\D", "", str(v or ""))[-10:]


def _first(name: str) -> str:
    n = re.sub(r"\s+", " ", str(name or "")).strip()
    n = re.split(r"\s*(?:&|and)\s*", n)[0]
    return n.split(" ")[0] if n else "there"


# ------------------------------------------------------------------ busy time
def _iso_local(s) -> datetime | None:
    """A Google / HoneyBook ISO stamp (with offset) or a plain local stamp -> naive local datetime."""
    if not s:
        return None
    s = str(s)
    try:
        dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        return C.parse_any(s)
    if dt.tzinfo:
        return dt.astimezone().replace(tzinfo=None)
    return dt


def busy_blocks(start: datetime, days: int, sc: dict | None = None, google=None, cal: dict | None = None,
                snap: dict | None = None, holds: dict | None = None, proposals: dict | None = None) -> list:
    """[(start, end, label)] for both owners inside start..start+days. Every source is read on its
    own and a missing one never hides the others. All-day events block the day only when they are
    a booking / shoot (the owners go to friends' weddings; a birthday does not fill the day)."""
    sc = sc or scfg()
    end_win = start + timedelta(days=days + 1)
    out = []
    # 1. Google Calendar, both owners, when the agent's token is there
    evs = google
    if evs is None and sc.get("google", True):
        try:
            sys.path.insert(0, str(AGENT))
            import google_api
            evs = google_api.events(days=days + 1, who="both", start=start.astimezone())
        except Exception:  # noqa: BLE001
            evs = []
    wedding_words = re.compile("|".join(re.escape(w) for w in (sc.get("all_day_blocks") or ["wedding", "shoot", "elopement", "ceremony"])), re.I)
    for ev in evs or []:
        if ev.get("error"):
            continue
        s, e = _iso_local(ev.get("start")), _iso_local(ev.get("end"))
        if not s:
            continue
        if ev.get("all_day"):
            if wedding_words.search(str(ev.get("title") or "")):
                out.append((s.replace(hour=0, minute=0), (e or s + timedelta(days=1)).replace(hour=0, minute=0), str(ev.get("title") or "")[:60]))
            continue
        out.append((s, e or s + timedelta(hours=1), str(ev.get("title") or "")[:60]))
    # 2. the calendar file the spine keeps (always; it also carries the ICS feed)
    cal = cal if cal is not None else (C.load_json(C.DATA / "calendar.json", {}) or {})
    for ev in cal.get("events") or []:
        d = C.parse_any(ev.get("date"))
        if not d:
            continue
        if ev.get("all_day") or not ev.get("time"):
            if str(ev.get("booking") or "") in ("booked", "confirmed") or wedding_words.search(str(ev.get("title") or "")):
                out.append((d, d + timedelta(days=1), str(ev.get("title") or "")[:60]))
            continue
        try:
            h, m = _hm(ev["time"])
        except (ValueError, AttributeError):
            continue
        s = d.replace(hour=h, minute=m)
        hours = float(ev.get("hours") or 1.0)
        out.append((s, s + timedelta(hours=min(hours, 12.0)), str(ev.get("title") or "")[:60]))
    # 3. HoneyBook meetings in the desk snapshot
    snap = snap if snap is not None else (C.load_json(C.vault_path(sc.get("hb_snapshot") or "dashboard/data/honeybook.json"), {}) or {})
    for m in (snap.get("meetings") or []) if isinstance(snap, dict) else []:
        if not isinstance(m, dict) or m.get("_error"):
            continue
        s, e = _iso_local(m.get("start")), _iso_local(m.get("end"))
        if s:
            out.append((s, e or s + timedelta(minutes=30), "HoneyBook: " + str(m.get("title") or "meeting")[:50]))
    # 4. our own holds
    holds = holds if holds is not None else (C.load_json(C.DATA / "holds.json", {}) or {})
    for h in holds.get("holds") or []:
        d = C.parse_any(h.get("date"))
        if not d:
            continue
        if h.get("time"):
            try:
                hh, mm = _hm(h["time"])
                s = d.replace(hour=hh, minute=mm)
                out.append((s, s + timedelta(minutes=int(h.get("duration_min") or 60)), "hold: " + str(h.get("title") or "")[:50]))
            except (ValueError, AttributeError):
                continue
    # 5. times already offered to someone else and not yet declined / expired
    props = proposals if proposals is not None else _state()["proposals"]
    for p in props.values():
        if p.get("status") in ("proposed", "sent", "picked"):
            for sl in p.get("slots") or []:
                s = C.parse_any(sl.get("start"))
                if s:
                    out.append((s, s + timedelta(minutes=int(sl.get("duration_min") or 30)), "offered to " + str(p.get("who") or "")[:40]))
    return [(s, e, l) for s, e, l in out if e > start - timedelta(days=1) and s < end_win]


def _free(s: datetime, e: datetime, busy: list, buffer_min: int) -> bool:
    b = timedelta(minutes=buffer_min)
    return not any(bs - b < e and be + b > s for bs, be, _ in busy)


def open_slots(now: datetime | None = None, sc: dict | None = None, busy: list | None = None, days: int | None = None) -> list:
    """Every free slot start inside the windows, soonest first: [{"start", "duration_min"}]."""
    sc = sc or scfg()
    now = now or C.now_local()
    days = int(days or sc.get("days_ahead") or 7)
    dur = int(sc.get("duration_min") or 30)
    step = int(sc.get("slot_step_min") or 30)
    earliest = now + timedelta(hours=float(sc.get("min_lead_hours") or 20))
    earliest = earliest.replace(second=0, microsecond=0)
    if earliest.minute % step:
        earliest += timedelta(minutes=step - earliest.minute % step)
    busy = busy if busy is not None else busy_blocks(now, days, sc)
    windows = sc.get("windows") or {}
    out = []
    for i in range(days + 1):
        day = (now + timedelta(days=i)).date()
        win = windows.get(C.DOW[day.weekday()])
        if not win:
            continue
        (h1, m1), (h2, m2) = _hm(win[0]), _hm(win[1])
        t = datetime(day.year, day.month, day.day, h1, m1)
        close = datetime(day.year, day.month, day.day, h2, m2)
        while t + timedelta(minutes=dur) <= close:
            if t >= earliest and _free(t, t + timedelta(minutes=dur), busy, int(sc.get("buffer_min") or 30)):
                out.append({"start": t.strftime("%Y-%m-%d %H:%M"), "duration_min": dur})
            t += timedelta(minutes=step)
    return out


def pick_slots(free: list, sc: dict | None = None) -> list:
    """2-3 slots on different days, each the free time nearest the next preferred hour."""
    sc = sc or scfg()
    want = int(sc.get("slots") or 3)
    prefer = [_hm(x) for x in (sc.get("prefer_times") or ["10:00", "14:00", "16:00"])]
    by_day = {}
    for s in free:
        by_day.setdefault(s["start"][:10], []).append(s)
    out = []
    for i, day in enumerate(sorted(by_day)):
        if len(out) >= want:
            break
        ph, pm = prefer[i % len(prefer)]
        best = min(by_day[day], key=lambda s: abs((C.parse_any(s["start"]).hour * 60 + C.parse_any(s["start"]).minute) - (ph * 60 + pm)))
        out.append(best)
    if len(out) < 2 and len(free) >= 2:          # one day only: two times that day
        extra = [s for s in free if s not in out]
        out += extra[:2 - len(out)]
    return out


def slot_text(sl: dict, with_date: bool = True) -> str:
    d = C.parse_any(sl["start"])
    ap = "AM" if d.hour < 12 else "PM"
    h = d.hour % 12 or 12
    t = f"{h}:{d.minute:02d} {ap}" if d.minute else f"{h} {ap}"
    return f"{d.strftime('%a')} {d.month}/{d.day} at {t}" if with_date else t


def slots_sentence(slots: list) -> str:
    parts = [slot_text(s) for s in slots]
    if len(parts) <= 1:
        return parts[0] if parts else ""
    return ", ".join(parts[:-1]) + f" or {parts[-1]}"


# ------------------------------------------------------------------ who / how to reach them
def _contact_for(name: str) -> dict:
    """{name, phone, email, workspace_id, cls} for a person named in words, from the leads, the
    owners' phone book, HoneyBook contacts and the phone threads. Empty fields when unknown."""
    want = re.sub(r"\s+", " ", str(name or "")).strip().lower()
    out = {"name": name.strip(), "phone": "", "email": "", "workspace_id": "", "cls": ""}
    if not want:
        return out
    try:
        for f in sorted((C.DATA / "leads").glob("*.json")):
            L = (C.load_json(f, {}) or {}).get("lead") or {}
            nm = " ".join(x for x in (L.get("name"), L.get("partner")) if x).lower()
            if want in nm or (L.get("name") and str(L["name"]).lower() in want):
                out.update({"name": L.get("name") or name, "phone": str(L.get("phone") or ""), "email": str(L.get("email") or ""), "cls": "lead",
                            "workspace_id": str(L.get("workspace_id") or L.get("hb_workspace_id") or "")})
                break
    except OSError:
        pass
    if not (out["phone"] or out["email"]):
        try:
            import contacts
            hits = contacts.search(want)
            if hits:
                c = hits[0]
                out["name"] = c.get("name") or out["name"]
                out["phone"] = next((e.get("value") for e in c.get("phones", []) if e.get("value")), "")
                out["email"] = next((e.get("value") for e in c.get("emails", []) if e.get("value")), "")
        except Exception:  # noqa: BLE001
            pass
    if not (out["phone"] or out["email"]):
        th = C.load_json(C.DATA / "phone_threads.json", {}) or {}
        for handle, t in th.items():
            if want in str(t.get("name") or "").lower():
                out["phone"] = handle
                out["name"] = t.get("name") or out["name"]
                break
    if not out["workspace_id"]:
        snap = C.load_json(C.vault_path(scfg().get("hb_snapshot") or "dashboard/data/honeybook.json"), {}) or {}
        for p in snap.get("projects") or []:
            if want.split(" ")[0] in str(p.get("name") or "").lower() and str(p.get("status") or "") != "archived":
                out["workspace_id"] = str(p.get("id") or "")
                break
    return out


def _mail_from_for(lane: str, account_name: str = "") -> str:
    accts = (C.config().get("mail") or {}).get("accounts") or []
    for a in accts:
        if account_name and a.get("name") == account_name:
            return str(a.get("address") or "")
    for a in accts:
        if (a.get("default_lane") == lane or a.get("lane") == lane):
            return str(a.get("address") or "")
    return str((accts[0].get("address") if accts else "") or "")


def _text_account_for(lane: str) -> str:
    if lane == "owner6":
        return "owner6-iphone"
    return str((C.config().get("leads") or {}).get("text_account") or "owner1-iphone")


def request_from_ticket(t: dict) -> dict:
    """What `propose` needs, read off a ticket: who, channel, to, from/account, lane, thread ids."""
    p = t.get("payload") or {}
    kind, lane = str(t.get("kind") or ""), str(t.get("lane") or "personal")
    req = {"ticket": t.get("id"), "lane": lane, "last_inbound_ts": p.get("received") or t.get("created")}
    if kind == "imessage_in":
        req.update({"who": p.get("name") or "", "channel": "text", "to": p.get("from"), "account": p.get("account") or _text_account_for(lane)})
        if not req["who"]:
            try:
                import contacts
                req["who"] = contacts.name_for(str(p.get("from") or ""))
            except Exception:  # noqa: BLE001
                pass
    elif kind == "email":
        frm = str(p.get("from") or "")
        em = _EMAIL.search(frm)
        req.update({"who": re.sub(r"<.*?>", "", frm).strip(" \"'"), "channel": "email", "to": em.group(0) if em else frm,
                    "from": _mail_from_for(lane, str(p.get("account") or "")), "in_reply_to": p.get("message_id"),
                    "subject": ("Re: " + str(p.get("subject") or "")).replace("Re: Re:", "Re:")})
    elif kind in ("inquiry_new", "hb_message_in"):
        req.update({"who": p.get("name") or p.get("couple") or str(t.get("title") or "").split(":")[0], "channel": "honeybook",
                    "workspace_id": p.get("workspace_id") or p.get("project_id") or "", "in_reply_to_id": p.get("comm_id") or ""})
    elif kind == "owner1_request":
        m = re.search(r"\b(?:call|meeting|chat|zoom)\s+with\s+([A-Z][\w'’-]+(?:\s+(?:&|and)\s+[A-Z][\w'’-]+)?(?:\s+[A-Z][\w'’-]+)?)", str(p.get("text") or t.get("summary") or ""))
        name = m.group(1) if m else ""
        c = _contact_for(name)
        req.update({"who": c["name"] or name, "cls": c["cls"]})
        if c["phone"]:
            req.update({"channel": "text", "to": c["phone"], "account": _text_account_for(lane)})
        elif c["email"]:
            req.update({"channel": "email", "to": c["email"], "from": _mail_from_for(lane), "subject": scfg().get("templates", {}).get("email_subject", "A quick call")})
        elif c["workspace_id"]:
            req.update({"channel": "honeybook", "workspace_id": c["workspace_id"]})
    return req


def request_for(name: str, channel: str = "", to: str = "", lane: str = "wedding") -> dict:
    """What `propose` needs, from an owner's words ("set up a call with example", Sage's `call` field)."""
    c = _contact_for(name)
    req = {"who": c["name"] or name, "lane": lane, "cls": c["cls"]}
    to = str(to or "")
    channel = channel or ("text" if (to and "@" not in to) or (not to and c["phone"]) else "email" if (to or c["email"]) else "honeybook" if c["workspace_id"] else "")
    if channel == "text":
        req.update({"channel": "text", "to": to or c["phone"], "account": _text_account_for(lane)})
    elif channel == "email":
        req.update({"channel": "email", "to": to or c["email"], "from": _mail_from_for(lane), "subject": scfg().get("templates", {}).get("email_subject", "A quick call")})
    elif channel == "honeybook":
        req.update({"channel": "honeybook", "workspace_id": c["workspace_id"]})
    return req


# ------------------------------------------------------------------ the card
def _render(tpl: str, **kw) -> str:
    try:
        return str(tpl or "").format(**kw)
    except (KeyError, IndexError):
        return str(tpl or "")


def _audience(req: dict) -> str:
    return "client" if req.get("lane") == "wedding" else "personal"


def message_for(req: dict, slots: list, sc: dict | None = None) -> dict:
    """{body, subject?} in the voice the lane dictates, with the slots in words."""
    sc = sc or scfg()
    tp = sc.get("templates") or {}
    first = _first(req.get("who") or "")
    aud = _audience(req)
    kw = {"first": first, "name": req.get("who") or first, "slots": slots_sentence(slots),
          "slots_lines": "\n".join(f"- {slot_text(s)}" for s in slots), "n": len(slots)}
    ch = req["channel"]
    if ch == "text":
        body = _render(tp.get("lead_text") if aud == "client" else tp.get("personal_text"), **kw)
        return {"body": body}
    if ch == "email":
        subj = tp.get("email_subject") if aud == "client" else (tp.get("personal_email_subject") or "A quick call")
        return {"subject": req.get("subject") or _render(subj, **kw),
                "body": _render(tp.get("email_body") if aud == "client" else tp.get("personal_email_body"), **kw)}
    return {"body": _render(tp.get("honeybook_body"), **kw), "subject": req.get("subject") or _render(tp.get("email_subject"), **kw)}


def card_for(req: dict, slots: list, msg: dict, sc: dict | None = None) -> dict:
    sc = sc or scfg()
    who = str(req.get("who") or req.get("to") or "them")
    lane = str(req.get("lane") or "personal")
    ask = f"{who} and a call: offer these {len(slots)} times? They are open on both calendars and the HoneyBook meetings."
    label = "Send (Recommended)"
    q = {"header": f"Call with {_first(who)}"[:40], "lane": lane, "audience": _audience(req),
         "options": [{"label": label, "description": "sends exactly this; the hold goes on once they pick"},
                     {"label": "Skip"}],
         "details": "Slots: " + "; ".join(slot_text(s) for s in slots) + (f"\nTicket: {req.get('ticket')}" if req.get("ticket") else ""),
         "scheduling_key": req.get("key", "")}
    if req.get("last_inbound_ts"):
        q["last_inbound_ts"] = str(req["last_inbound_ts"])
    ch = req["channel"]
    if ch == "text":
        payload = {"account": req.get("account") or _text_account_for(lane), "to": str(req.get("to") or ""), "body": msg["body"]}
        q["question"] = f"{ask}\n[TEXT PREVIEW] To {who} ({payload['to']}):\n{msg['body']}"
        q["exec"] = {"kind": "imessage_send", "approve_label": label, "payload": payload}
    elif ch == "email":
        payload = {"from": str(req.get("from") or ""), "to": str(req.get("to") or ""), "subject": msg["subject"], "body": msg["body"]}
        if req.get("in_reply_to"):
            payload["in_reply_to"] = str(req["in_reply_to"])
        q["question"] = (f"{ask}\n[EMAIL PREVIEW]\nFrom: {payload['from']}\nTo: {payload['to']}\nSubject: {payload['subject']}\n\n{msg['body']}")
        q["exec"] = {"kind": "email_send", "approve_label": label, "payload": payload}
    else:
        payload = {"workspace_id": str(req.get("workspace_id") or ""), "body": msg["body"]}
        if req.get("in_reply_to_id"):
            payload["in_reply_to_id"] = str(req["in_reply_to_id"])
        else:
            payload["subject"] = msg.get("subject") or "A quick call"
        q["question"] = f"{ask}\n[HONEYBOOK MESSAGE PREVIEW] To {who}:\n{msg['body']}"
        q["exec"] = {"kind": "hb_message", "approve_label": label, "payload": payload}
    q["advice"] = advice.line({"kind": "offer", "lane": lane, "title": q["header"], "age_days": C.age_days(req.get("last_inbound_ts")) if req.get("last_inbound_ts") else 0,
                               "why": "Send offers only times that are free now; nothing is booked until they answer",
                               "waiting_on": "a lead" if req.get("cls") == "lead" else ("a client" if lane == "wedding" else "")})
    return q


def _hb_meeting(req: dict) -> dict | None:
    """The meeting a lead already booked through the HoneyBook scheduler, if any (lead_meetings.py):
    2026-10-08, example & Dean had booked a call and the first draft still offered one."""
    if str(req.get("lane") or "") != "wedding" or not req.get("who"):
        return None
    try:
        import lead_meetings
        return lead_meetings.for_unit(req.get("workspace_id") or "", couple=str(req.get("who") or ""))
    except Exception:  # noqa: BLE001
        return None


def _post(q: dict, lane: str, dry: bool) -> dict:
    if dry:
        print("WOULD POST " + json.dumps(q, ensure_ascii=False)[:1500])
        return {"posted": True, "qid": "dry"}
    import cards
    return cards.post([q], owner="tracking.scheduling", lane=lane)


def propose(req: dict, now: datetime | None = None, dry: bool = False, busy: list | None = None) -> dict:
    """Pick the slots, draft the message, post ONE card. -> {ok, qid, slots, reason}."""
    sc = scfg()
    if not sc.get("enabled", True):
        return {"ok": False, "reason": "scheduling disabled in tracking.json"}
    now = now or C.now_local()
    ch = str(req.get("channel") or "")
    if ch not in ("text", "email", "honeybook"):
        return {"ok": False, "reason": f"no way to reach {req.get('who') or 'them'}: give a phone number, an email or a HoneyBook project"}
    if ch == "text" and not _digits(req.get("to")) and "@" not in str(req.get("to") or ""):
        return {"ok": False, "reason": "a text needs their number"}
    if ch == "email" and not _EMAIL.search(str(req.get("to") or "")):
        return {"ok": False, "reason": "an email needs their address"}
    if ch == "honeybook" and not _HEX24.fullmatch(str(req.get("workspace_id") or "")):
        return {"ok": False, "reason": "a HoneyBook message needs the 24-hex workspace id"}
    booked = _hb_meeting(req)
    if booked:
        return {"ok": False, "reason": f"{req.get('who') or 'they'} already booked a meeting in HoneyBook: {booked.get('when')}. Confirm that one instead of offering times."}
    st = _state()
    key = f"{ch}:{_digits(req.get('to')) or str(req.get('to') or req.get('workspace_id') or '').lower()}"
    live = st["proposals"].get(key)
    if live and live.get("status") in ("proposed", "sent") and C.age_days(live.get("ts"), now) < float(sc.get("proposal_ttl_days") or 5):
        return {"ok": False, "reason": f"times were already offered to {live.get('who')} on {live.get('ts')} (card {live.get('qid')}); wait for their answer", "qid": live.get("qid")}
    free = open_slots(now, sc, busy=busy)
    slots = pick_slots(free, sc)
    if len(slots) < 2:
        return {"ok": False, "reason": "fewer than two open slots in the next week; widen scheduling.windows or days_ahead"}
    req = dict(req, key=key)
    msg = message_for(req, slots, sc)
    q = card_for(req, slots, msg, sc)
    r = _post(q, str(req.get("lane") or "personal"), dry)
    if not r.get("posted"):
        C.log("scheduling_post_refused", key=key, reason=str(r.get("reason"))[:200])
        return {"ok": False, "reason": str(r.get("reason") or "card refused")}
    if not dry:
        st["proposals"][key] = {"key": key, "who": req.get("who"), "channel": ch, "to": req.get("to"), "account": req.get("account"),
                                "from": req.get("from"), "workspace_id": req.get("workspace_id"), "lane": req.get("lane"),
                                "slots": slots, "qid": r.get("qid"), "ticket": req.get("ticket"), "status": "proposed",
                                "ts": now.strftime("%Y-%m-%d %H:%M"), "in_reply_to": req.get("in_reply_to"), "subject": msg.get("subject")}
        _save(st)
        C.log("scheduling_proposed", key=key, qid=r.get("qid"), slots=[s["start"] for s in slots])
    return {"ok": True, "qid": r.get("qid"), "slots": slots, "card": q}


# ------------------------------------------------------------------ their answer
_NO = re.compile(r"\b(none|neither|nope|no(?:ne)? of (?:those|them|these)|don'?t work|doesn'?t work|won'?t work|can'?t (?:do|make)|cannot|"
                 r"not (?:free|available|able)|unavailable|busy (?:all|those|then)|other times?|different (?:time|day)|another (?:time|day))\b", re.I)
_ORD = {"first": 0, "1st": 0, "earliest": 0, "second": 1, "2nd": 1, "middle": 1, "third": 2, "3rd": 2, "last": -1, "latest": -1,
        "one": 0, "two": 1, "three": 2}            # "last one" must read as last, so the bare numbers come after
_ANY = re.compile(r"\b(any|either|all|whichever|any of (?:those|them|these)|all work|both work|either works?)\b", re.I)


def pick_slot(text: str, slots: list) -> tuple:
    """-> (index or None, 'picked' | 'declined' | 'any' | ''), reading a reply against the offered slots."""
    t = str(text or "").lower()
    if not t.strip() or not slots:
        return None, ""
    if _NO.search(t):
        return None, "declined"
    hits = set()
    for i, sl in enumerate(slots):
        d = C.parse_any(sl["start"])
        day_full, day_abbr = WEEKDAYS[d.weekday()], WEEKDAYS[d.weekday()][:3]
        md = f"{d.month}/{d.day}"
        h12 = d.hour % 12 or 12
        ap = "am" if d.hour < 12 else "pm"
        times = {f"{h12}{ap}", f"{h12} {ap}", f"{h12}:{d.minute:02d}{ap}", f"{h12}:{d.minute:02d} {ap}", f"{h12}:{d.minute:02d}"}
        if d.minute == 0:
            times |= {f"{h12} o'clock", f"{h12}oclock"}
        day_hit = re.search(rf"\b{day_full}\b|\b{day_abbr}\b(?!\w)|{re.escape(md)}\b", t) is not None
        time_hit = any(x in t for x in times)
        same_day_others = [j for j, o in enumerate(slots) if j != i and C.parse_any(o["start"]).date() == d.date()]
        if (day_hit and (time_hit or not same_day_others)) or (time_hit and not day_hit and not any(
                C.parse_any(o["start"]).hour == d.hour and j != i for j, o in enumerate(slots))):
            hits.add(i)
    for word, idx in _ORD.items():
        if re.search(rf"\b(?:the )?{word}(?: one| option| time| slot)?\b", t) and not hits:
            hits.add(idx % len(slots))
            break
    m = re.search(r"\boption\s*([123])\b|\b#\s*([123])\b", t)
    if m and not hits:
        hits.add(int(m.group(1) or m.group(2)) - 1)
    if len(hits) == 1:
        return hits.pop(), "picked"
    if _ANY.search(t):
        return 0, "any"
    return None, ""


def _inbound_since(p: dict, since: datetime) -> list:
    """[(datetime, text)] from the person a proposal went to, after `since`, on the same channel."""
    out = []
    if p.get("channel") == "text":
        th = C.load_json(C.DATA / "phone_threads.json", {}) or {}
        key = next((k for k in th if _digits(k) == _digits(p.get("to")) or k.lower() == str(p.get("to") or "").lower()), None)
        for m in (th.get(key) or {}).get("messages") or [] if key else []:
            ts = C.parse_any(m.get("ts"))
            if m.get("dir") == "in" and ts and ts > since:
                out.append((ts, str(m.get("text") or "")))
    elif p.get("channel") == "email":
        for t in C.tickets_all().values():
            if t.get("kind") != "email":
                continue
            ts = C.parse_any(t.get("created"))
            frm = str((t.get("payload") or {}).get("from") or "").lower()
            if ts and ts > since and str(p.get("to") or "").lower() in frm:
                out.append((ts, f"{(t.get('payload') or {}).get('subject') or ''}\n{t.get('summary') or ''}"))
    else:
        snap = C.load_json(C.vault_path(scfg().get("hb_snapshot") or "dashboard/data/honeybook.json"), {}) or {}
        for m in snap.get("communications") or []:
            ts = C.parse_any(m.get("created_at"))
            if ts and ts > since and str(m.get("workspace_id") or "") == str(p.get("workspace_id") or "") \
                    and str(m.get("direction") or "in").lower() in ("in", "inbound", "client"):
                out.append((ts, str(m.get("body") or "")))
    return sorted(out)


def confirm_cards(p: dict, slot: dict, sc: dict | None = None) -> list:
    """The confirmation message card and the calendar-hold card for a picked slot."""
    sc = sc or scfg()
    tp = sc.get("templates") or {}
    who = str(p.get("who") or p.get("to") or "them")
    first = _first(who)
    lane = str(p.get("lane") or "personal")
    aud = "client" if lane == "wedding" else "personal"
    when = slot_text(slot)
    kw = {"first": first, "name": who, "slot": when}
    label = "Send (Recommended)"
    ch = p.get("channel")
    q = {"header": f"Confirm call: {first}"[:40], "lane": lane, "audience": aud, "scheduling_key": p.get("key"),
         "options": [{"label": label, "description": "confirms the time they picked"}, {"label": "Skip"}],
         "details": f"They picked {when} from: " + "; ".join(slot_text(s) for s in p.get("slots") or [])}
    ask = f"{who} picked {when}. Confirm it?"
    if ch == "text":
        body = _render(tp.get("confirm_lead_text") if aud == "client" else tp.get("confirm_personal_text"), **kw)
        payload = {"account": p.get("account") or _text_account_for(lane), "to": str(p.get("to") or ""), "body": body}
        q["question"] = f"{ask}\n[TEXT PREVIEW] To {who} ({payload['to']}):\n{body}"
        q["exec"] = {"kind": "imessage_send", "approve_label": label, "payload": payload}
    elif ch == "email":
        body = _render(tp.get("confirm_email_body"), **kw)
        subj = str(p.get("subject") or "Our call")
        payload = {"from": str(p.get("from") or ""), "to": str(p.get("to") or ""), "subject": subj, "body": body}
        q["question"] = f"{ask}\n[EMAIL PREVIEW]\nFrom: {payload['from']}\nTo: {payload['to']}\nSubject: {subj}\n\n{body}"
        q["exec"] = {"kind": "email_send", "approve_label": label, "payload": payload}
    else:
        body = _render(tp.get("confirm_honeybook_body"), **kw)
        payload = {"workspace_id": str(p.get("workspace_id") or ""), "body": body, "subject": str(p.get("subject") or "Our call")}
        q["question"] = f"{ask}\n[HONEYBOOK MESSAGE PREVIEW] To {who}:\n{body}"
        q["exec"] = {"kind": "hb_message", "approve_label": label, "payload": payload}
    q["advice"] = advice.line({"kind": "offer", "lane": lane, "title": q["header"], "why": f"they chose {when}; the hold card below keeps the slot"})
    d = C.parse_any(slot["start"])
    hold_note = f"Picked by {who} from the times offered on {p.get('ts', '')[:10]}."
    hold = {"title": _render(sc.get("hold_title") or "Call: {name}", **kw)[:90], "date": d.strftime("%Y-%m-%d"), "time": d.strftime("%H:%M"),
            "duration_min": int(slot.get("duration_min") or sc.get("duration_min") or 30), "note": hold_note}
    hq = {"header": f"Hold: call with {first}"[:40], "lane": lane, "scheduling_key": p.get("key"),
          "question": f"Put the hold on the calendar: \"{hold['title']}\" {when}, {hold['duration_min']} min. {hold_note} Nobody is notified.",
          "options": [{"label": "Add to calendar (Recommended)", "description": "a hold on our own calendar"}, {"label": "Skip"}],
          "exec": {"kind": "calendar_hold", "approve_label": "Add to calendar (Recommended)", "payload": hold},
          "advice": advice.line({"kind": "offer", "lane": lane, "title": "hold", "why": "without the hold the slot can be offered to someone else"})}
    return [q, hq]


def harvest(st: dict, answers: list | None = None) -> int:
    answers = answers if answers is not None else C.card_answers(14)
    by_qid = {}
    for a in answers:
        by_qid[str(a.get("qid"))] = a
    n = 0
    for p in st["proposals"].values():
        a = by_qid.get(str(p.get("qid") or ""))
        if not a or p.get("status") not in ("proposed",):
            continue
        opt = str(a.get("option") or "").lower()
        if opt.startswith("send") or a.get("revised"):
            p["status"], p["sent_ts"] = "sent", str(a.get("ts") or "")
        else:
            p["status"] = "skipped"
        n += 1
    return n


def tick(now: datetime | None = None, dry: bool = False) -> dict:
    sc = scfg()
    now = now or C.now_local()
    st = _state()
    harvested = harvest(st)
    picked, declined, expired = [], [], []
    ttl = float(sc.get("proposal_ttl_days") or 5)
    for key, p in st["proposals"].items():
        if p.get("status") in ("proposed", "sent") and C.age_days(p.get("ts"), now) > ttl:
            p["status"] = "expired"
            expired.append(key)
            continue
        if p.get("status") != "sent":
            continue
        since = C.parse_any(p.get("sent_ts")) or C.parse_any(p.get("ts")) or now
        for ts, text in _inbound_since(p, since):
            idx, how = pick_slot(text, p.get("slots") or [])
            if how == "declined":
                p["status"], p["answer"] = "declined", text[:200]
                declined.append(key)
                C.log("scheduling_declined", key=key, text=text[:120])
                break
            if how in ("picked", "any") and idx is not None:
                slot = (p.get("slots") or [])[idx]
                cards_out = confirm_cards(p, slot, sc)
                qids = []
                for q in cards_out:
                    r = _post(q, str(p.get("lane") or "personal"), dry)
                    qids.append(str(r.get("qid") or ""))
                    if not r.get("posted"):
                        C.log("scheduling_confirm_refused", key=key, reason=str(r.get("reason"))[:200])
                p.update({"status": "picked", "picked": slot, "answer": text[:200], "confirm_qids": qids})
                picked.append({"key": key, "slot": slot["start"], "qids": qids})
                C.log("scheduling_picked", key=key, slot=slot["start"], qids=qids)
                break
    if not dry:
        _save(st)
    out = {"harvested": harvested, "picked": picked, "declined": declined, "expired": expired,
           "open": sum(1 for p in st["proposals"].values() if p.get("status") in ("proposed", "sent"))}
    print("SCHEDULING " + json.dumps(out, ensure_ascii=False))
    return out


def status() -> None:
    for key, p in sorted(_state()["proposals"].items(), key=lambda kv: kv[1].get("ts") or ""):
        print(f"{p.get('status', '?'):9} {p.get('lane', ''):8} {key:36} {str(p.get('who', ''))[:24]:24} {p.get('ts', '')}  "
              f"{', '.join(slot_text(s) for s in p.get('slots') or [])}")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("cmd", nargs="?", default="tick", choices=["tick", "propose", "slots", "status"])
    ap.add_argument("--ticket")
    ap.add_argument("--with", dest="with_name", default="")
    ap.add_argument("--channel", default="", choices=["", "text", "email", "honeybook"])
    ap.add_argument("--to", default="")
    ap.add_argument("--lane", default="")
    ap.add_argument("--days", type=int, default=None)
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args()
    if a.cmd == "status":
        status(); return 0
    if a.cmd == "slots":
        free = open_slots(days=a.days)
        for s in pick_slots(free):
            print("PICK  " + slot_text(s))
        for s in free[:40]:
            print("      " + slot_text(s))
        print(f"{len(free)} open slots")
        return 0
    if a.cmd == "propose":
        if a.ticket:
            t = C.tickets_all().get(a.ticket)
            if not t:
                print(json.dumps({"ok": False, "reason": f"no ticket {a.ticket}"})); return 1
            req = request_from_ticket(t)
            if a.lane:
                req["lane"] = a.lane
            if a.channel and a.to:
                req.update(request_for(req.get("who") or a.with_name, a.channel, a.to, req["lane"]))
        else:
            req = request_for(a.with_name, a.channel, a.to, a.lane or "wedding")
        r = propose(req, dry=a.dry_run)
        print(json.dumps({k: v for k, v in r.items() if k != "card"}, ensure_ascii=False))
        return 0 if r.get("ok") else 1
    tick(dry=a.dry_run)
    return 0


if __name__ == "__main__":
    sys.exit(main())

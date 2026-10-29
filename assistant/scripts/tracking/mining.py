"""mining.py - what the other party told us, mined for dates and commitments (ruling 3, 2026-10-15).

The property OS's vendor_threads.py read Charles's texts and found the Wednesday he agreed to;
nothing in Sage read the threads that way. This is that first pass, zero-LLM, over everything
new since the last run:

  phone threads   every inbound text (assistant/data/phone_threads.json), including the personal
                  ones text_gate never tickets (ruling 13: Sage still mines them for dates)
  tickets         email and HoneyBook inquiry tickets (the mail sensor's subject + snippet)
  chat            the owners' own lines in the shared space (data/agent/transcript.jsonl); OFF since
                  2026-10-17 (mining.json sources.chat): Sage already hears those lines, a hold card quoting
                  the owner's own words back is noise
  HoneyBook       client messages in the desk snapshot, when the snapshot carries them

A date beside a commitment word from a couple / planner / vendor / family / friend / owner
becomes a PROPOSED calendar hold; a money sender's due / renewal / action-required date becomes a
PROPOSED follow-up. A lead's wedding date never is (an inquiry is not a booking), nor a quiz alert's
'Flags:' line, nor a message with nobody to name on the card (2026-10-20). Both are cards (never silent): the hold is a `calendar_hold` exec card, the
follow-ups ride ONE `tracking_fix` card whose fixes are `followup_arm` rows (drift.py apply arms
them on the tap). mail_arm.py keeps arming plain bills / renewals / deliveries by itself; mining
skips what mail_arm already classified and only adds the shapes in mining.json -> money.extra.

Vocabulary: assistant/mining.json. Dates: mail_arm.find_dates (one parser for both). State:
data/tracking/mining.json (seen keys + cursors). Nothing here sends or writes a record.

  python assistant/scripts/tracking/mining.py                # tick (the spine runs this)
  python assistant/scripts/tracking/mining.py --dry-run      # what it would propose, posts nothing
  python assistant/scripts/tracking/mining.py scan --days 7  # every finding in the window, seen or not
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from datetime import date, datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import advice  # noqa: E402
import common as C  # noqa: E402

sys.path.insert(0, str(C.SCRIPTS))
import mail_arm  # noqa: E402

CFG_PATH = C.ROOT / "mining.json"
WEEKDAYS = ("monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday")
_ABBR = {"mon": 0, "tue": 1, "tues": 1, "wed": 2, "thu": 3, "thur": 3, "thurs": 3, "fri": 4, "sat": 5, "sun": 6}
_SENT_SPLIT = re.compile(r"(?<=[.!?])\s+|\n+|(?<=[.!?])(?=[A-Z])")
_EMAIL = re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+")
_STOP = {"the", "and", "for", "with", "your", "this", "that", "from", "you", "are", "our", "will", "have", "about", "there"}


def cfg() -> dict:
    return C.load_json(CFG_PATH, {}) or {}


def _rx(pattern: str, flags=re.I):
    try:
        return re.compile(pattern, flags) if pattern else None
    except re.error:
        return None


# ------------------------------------------------------------------ dates and times
def rel_date(word: str, said: date, weekday_means_next: bool = True):
    """'tomorrow' / 'friday' / 'next week' -> a date counted from the day it was said. A weekday
    already past this week means next week's (said Thursday, 'Wednesday' is next Wednesday)."""
    w = word.lower().strip()
    if w in ("today", "tonight", "this morning", "this afternoon", "this evening"):
        return said
    if w in ("tomorrow", "tmrw", "tmr"):
        return said + timedelta(days=1)
    if w == "this weekend":
        return said + timedelta(days=(5 - said.weekday()) % 7)
    if w == "next week":
        return said + timedelta(days=7 - said.weekday())
    if w == "next weekend":
        return said + timedelta(days=(5 - said.weekday()) % 7 + 7)
    if w == "next month":
        return (said.replace(day=1) + timedelta(days=32)).replace(day=1)
    idx = WEEKDAYS.index(w) if w in WEEKDAYS else _ABBR.get(w)
    if idx is None:
        return None
    ahead = (idx - said.weekday()) % 7
    if ahead == 0 and weekday_means_next:
        ahead = 7
    return said + timedelta(days=ahead)


def time_in(sentence: str, time_rx, bare_rx=None, evening_rx=None) -> str:
    """'HH:MM' (24 h) when the sentence names a clock time, else ''. A bare hour after "at" /
    "around" ("dinner at 5", "be there at 8") is read by the hour and the words around it:
    1-6 is the afternoon, 7-11 is the evening only beside an evening word, 12 is noon."""
    if not time_rx:
        return ""
    found = _time_in(sentence, time_rx)
    if found or not bare_rx:
        return found
    m = bare_rx.search(sentence)
    if not m:
        return ""
    h = int(m.group(1))
    if h < 1 or h > 12:
        return ""
    if h == 12:
        return "12:00"
    if h <= 6 or (evening_rx and evening_rx.search(sentence)):
        h += 12
    return f"{h:02d}:00"


def _time_in(sentence: str, time_rx) -> str:
    for m in time_rx.finditer(sentence):
        if m.group(6):
            return "12:00" if m.group(6).lower() == "noon" else "00:00"
        if m.group(1):
            h, mi, ap = int(m.group(1)), int(m.group(2) or 0), m.group(3).lower().replace(".", "")
            if h > 12 or mi > 59:
                continue
            h = (h % 12) + (12 if ap == "pm" else 0)
            return f"{h:02d}:{mi:02d}"
        if m.group(4):
            h, mi = int(m.group(4)), int(m.group(5))
            if h <= 23 and mi <= 59:
                if 1 <= h <= 7:                   # "until 5:30 today" is the afternoon, not dawn
                    h += 12
                return f"{h:02d}:{mi:02d}"
    return ""


def dates_in(sentence: str, said: date, dcfg: dict) -> list:
    """[(date, word)] for every absolute or relative date in the sentence, text order."""
    out = []
    for pos, _end, d, _y in mail_arm.find_dates(sentence, said, window_days=int(dcfg.get("max_days_ahead") or 365), allow_past_days=400):
        out.append((pos, d, sentence[pos:_end]))
    rel = _rx(dcfg.get("relative") or "")
    if rel:
        for m in rel.finditer(sentence):
            d = rel_date(m.group(1), said, bool(dcfg.get("weekday_means_next", True)))
            if d:
                out.append((m.start(), d, m.group(1)))
    out.sort(key=lambda x: x[0])
    seen, uniq = set(), []
    for _p, d, w in out:
        if d not in seen:
            seen.add(d)
            uniq.append((d, w))
    return uniq


_QUOTED = re.compile(r"^\s*>|\bwrote:|^\s*on .{3,80}\b(?:wrote|sent)\b|^-+\s*(?:original|forwarded) message", re.I)


def sentences(text: str, min_chars: int = 8) -> list:
    """Sentences worth reading: a quoted reply header ("On Friday, Aug 28 ... wrote:") and any
    quoted (">") line are the OTHER message, never this sender's commitment."""
    out = []
    for s in _SENT_SPLIT.split(str(text or "")):
        s = s.strip()
        if len(s) < min_chars or _QUOTED.search(s):
            continue
        out.append(s)
    return out


# ------------------------------------------------------------------ who is talking
class Context:
    """The lookups every message is classified against, loaded once per run."""

    def __init__(self, c: dict | None = None, now: datetime | None = None):
        self.c = c or cfg()
        self.now = now or C.now_local()
        self.today = self.now.date()
        self.leads, self.couples, self.vendors = {}, {}, {}
        self.owners, self.household = {}, set()
        self.lead_dates = {}                   # lead handle -> {their wedding date}: an inquiry is not a booking
        self.money_from = _rx((self.c.get("money") or {}).get("senders") or "")
        self.money_subj = _rx((self.c.get("money") or {}).get("subject") or "")
        self._load()

    @staticmethod
    def _k(v) -> str:
        v = str(v or "").strip().lower()
        if "@" in v:
            return v
        d = re.sub(r"\D", "", v)
        return d[-10:] if len(d) >= 10 else ""

    def _load(self):
        try:
            import comms_state
            self.leads = {k: v for k, v in comms_state._leads().items() if k}
            self.couples = {k: v for k, v in comms_state._couples().items() if k}
        except Exception:  # noqa: BLE001
            pass
        try:
            for f in (C.DATA / "leads").glob("*.json"):
                lead = (C.load_json(f, {}) or {}).get("lead") or {}
                for h in (lead.get("phone"), lead.get("email")):
                    if h and lead.get("date"):
                        self.lead_dates.setdefault(self._k(h), set()).add(str(lead["date"])[:10])
        except OSError:
            pass
        try:
            import text_gate
            self.household = set(text_gate.household_numbers())
        except Exception:  # noqa: BLE001
            pass
        try:
            people = C.load_json(C.ROOT / "agent" / "people.json", {}) or {}
            for key, u in (people.get("users") or {}).items():
                for e in u.get("emails") or []:
                    self.owners[str(e).lower()] = key
                if u.get("phone"):
                    self.owners[self._k(u["phone"])] = key
                self.owners[str(u.get("name") or key).lower()] = key
        except Exception:  # noqa: BLE001
            pass
        phone = re.compile(r"\+?1?[\s(.-]*\d{3}[\s).-]*\d{3}[\s.-]*\d{4}")
        try:
            for f in (C.VAULT / "Wiki" / "Vendors").rglob("*.md"):
                txt = f.read_text(encoding="utf-8", errors="replace")
                kind = "planner" if re.search(r"\bplanner|coordinator\b", txt[:1500], re.I) else "vendor"
                for m in phone.findall(txt):
                    self.vendors[self._k(m)] = (kind, f.stem)
                for m in _EMAIL.findall(txt):
                    self.vendors[m.lower()] = (kind, f.stem)
        except OSError:
            pass

    def classify(self, handle: str, name: str = "", lane: str = "", subject: str = "", bulk: bool = False) -> tuple:
        """-> (class, display name)."""
        k = self._k(handle)
        h = str(handle or "").lower()
        if k in self.owners or h in self.owners:
            return self.owners.get(k) or self.owners.get(h), name or (self.owners.get(k) or "").capitalize()
        if k in self.leads:
            return "lead", self.leads[k] or name
        if k in self.couples:
            return "couple", self.couples[k] or name
        if k in self.vendors:
            kind, nm = self.vendors[k]
            return kind, name or nm
        if "@" in h:
            if (self.money_from and self.money_from.search(h + " " + str(name or ""))) or \
                    (lane == "money" and self.money_subj and self.money_subj.search(subject or "")):
                return "money", name or h.split("@")[-1]
        if k in self.household:
            return "family", name or handle
        if lane == "owner6" and "@" not in h:
            return "friend", name or handle
        if lane == "wedding":
            return "vendor", name or handle
        return "unknown", name or handle

    def known_subject(self, handle: str) -> tuple:
        """An unknown number's subject from Sage's memory and the wiki -> (subject, suspect), ('', False)
        when neither mentions it. Suspect = a mention calls it unchecked / a possible scam (the
        '+1555-051-2351: Coverage will end on 10/' hold, 2026-10-19)."""
        k = self._k(handle)
        if not k or "@" in k:
            return "", False
        if not hasattr(self, "_subjects"):
            self._subjects = _subjects_by_number(self.c.get("unknown_sender") or {})
        return self._subjects.get(k, ("", False))


def _subjects_by_number(ucfg: dict) -> dict:
    """digits -> (subject, suspect): memory facts first (their own subject), then wiki note titles."""
    phone = re.compile(r"\+?1?[\s(.-]*\d{3}[\s).-]*\d{3}[\s.-]*\d{4}")
    suspect_rx = _rx(ucfg.get("suspect") or r"\b(scam|phish\w*|unverified|not (?:yet )?(?:been )?(?:confirmed|checked|verified)|could be fake|suspicious)\b")
    out = {}

    def add(text, subject):
        subject = re.sub(r"\s*\([^)]*\)\s*$", "", str(subject or "")).strip()
        if not subject:
            return
        sus = bool(suspect_rx and suspect_rx.search(text))
        for m in phone.findall(text):
            k = Context._k(m)
            if not k:
                continue
            old = out.get(k)
            out[k] = (old[0] if old else subject, sus or bool(old and old[1]))

    mem = C.load_json(C.ROOT / "agent" / "state" / "memory.json", {}) or {}
    for f in mem.get("facts") or []:
        if isinstance(f, dict):
            add(str(f.get("fact") or ""), f.get("subject"))
    try:
        for f in (C.VAULT / "Wiki").rglob("*.md"):
            if "_meta" in f.parts or f.stem.lower().startswith("build request"):
                continue
            add(f.read_text(encoding="utf-8", errors="replace"), f.stem)
    except OSError:
        pass
    return out


# ------------------------------------------------------------------ the messages
def _msg(source, ref, handle, name, lane, said, text, subject="", bulk=False, channel="text", extra=None):
    return {"source": source, "ref": str(ref), "handle": str(handle or ""), "name": str(name or ""), "lane": str(lane or "personal"),
            "said": said, "text": str(text or ""), "subject": str(subject or ""), "bulk": bool(bulk), "channel": channel,
            **(extra or {})}


def _name_for(handle) -> str:
    try:
        import contacts
        return str(contacts.name_for(str(handle or "")) or "")
    except Exception:  # noqa: BLE001
        return ""


def messages_phone(since: datetime | None, threads: dict | None = None) -> list:
    out = []
    th = threads if threads is not None else (C.load_json(C.DATA / "phone_threads.json", {}) or {})
    for handle, t in th.items():
        if not isinstance(t, dict):
            continue
        for m in t.get("messages") or []:
            if m.get("dir") != "in":
                continue
            ts = C.parse_any(m.get("ts"))
            if not ts or (since and ts <= since):
                continue
            out.append(_msg("phone", f"{handle}|{m.get('ts')}", handle, t.get("name") or _name_for(handle), "", ts, m.get("text"), channel="text"))
    return out


def messages_tickets(since: datetime | None, tickets: dict | None = None) -> list:
    out = []
    tix = tickets if tickets is not None else C.tickets_all()
    for t in tix.values():
        kind, src = str(t.get("kind") or ""), str(t.get("source") or "")
        if kind not in ("email", "inquiry_new", "hb_message_in", "imessage_in"):
            continue
        p = t.get("payload") or {}
        ts = C.parse_any(p.get("received") if kind == "imessage_in" else t.get("created"))
        if not ts or (since and ts <= since):
            continue
        if kind == "imessage_in":          # the thread log keeps 60 per contact; the ticket keeps the rest
            out.append(_msg("ticket", t.get("id"), p.get("from"), p.get("name") or _name_for(p.get("from")), t.get("lane"), ts, p.get("text"),
                            channel="text", extra={"ticket": t.get("id"), "account": p.get("account")}))
            continue
        frm = str(p.get("from") or "")
        em = _EMAIL.search(frm)
        handle = em.group(0).lower() if em else frm
        name = re.sub(r"<.*?>", "", frm).strip(" \"'") if em else str(p.get("name") or "")
        body = str(p.get("text") or p.get("body") or t.get("summary") or "")
        extra = {"ticket": t.get("id"), "ticket_kind": kind, "account": p.get("account"), "message_id": p.get("message_id"),
                 "workspace_id": p.get("workspace_id") or p.get("project_id")}
        out.append(_msg("ticket", t.get("id"), handle, name, t.get("lane"), ts, body, subject=p.get("subject") or "",
                        bulk=bool(p.get("bulk")), channel="honeybook" if src.startswith("honeybook") else "email", extra=extra))
    return out


def messages_chat(since: datetime | None, rows: list | None = None, owners: dict | None = None) -> list:
    out = []
    rows = rows if rows is not None else C.transcript(14)
    names = {str(v).lower() for v in (owners or {}).values()} if owners else set()
    for r in rows:
        who = str(r.get("who") or "")
        if str(r.get("channel") or "") not in ("gchat", "chat", "mc") or (names and who.lower() not in names):
            continue
        ts = C.parse_any(r.get("ts"))
        if not ts or (since and ts <= since) or str(r.get("text") or "").lstrip().startswith("["):
            continue                                   # "[tapped ...]" lines are the bridge's echoes, not the owner's words
        out.append(_msg("chat", f"{who}|{r.get('ts')}", who.lower(), who, "owner6" if who.lower() == "owner6" else "personal",
                        ts, r.get("text"), channel="chat", extra={"convo": r.get("convo"), "thread": r.get("thread")}))
    return out


def messages_honeybook(since: datetime | None, snap: dict | None = None) -> list:
    """Client messages in the desk snapshot (`communications`), when hb_snapshot carries them."""
    out = []
    snap = snap if snap is not None else (C.load_json(C.VAULT / "dashboard" / "data" / "honeybook.json", {}) or {})
    for m in snap.get("communications") or snap.get("messages") or []:
        if not isinstance(m, dict) or str(m.get("direction") or "in").lower() not in ("in", "inbound", "client"):
            continue
        ts = C.parse_any(m.get("created_at") or m.get("ts"))
        if not ts or (since and ts <= since):
            continue
        frm = m.get("from") or {}
        handle = str((frm.get("email") if isinstance(frm, dict) else frm) or m.get("email") or "").lower()
        name = str((frm.get("name") if isinstance(frm, dict) else "") or m.get("name") or "")
        out.append(_msg("honeybook", m.get("id") or f"{handle}|{m.get('created_at')}", handle, name, "wedding", ts,
                        m.get("body") or m.get("text"), subject=m.get("subject") or "", channel="honeybook",
                        extra={"workspace_id": m.get("workspace_id"), "comm_id": m.get("id")}))
    return out


# ------------------------------------------------------------------ the mining
def _gist(sentence: str, n: int) -> str:
    words = [w for w in re.findall(r"[A-Za-z0-9'’$:/-]+", sentence)]
    return " ".join(words[:n]).rstrip(",.;:")


def _key(handle: str, d: date, sentence: str) -> str:
    return hashlib.sha1(f"{handle}|{d.isoformat()}|{C.slug(sentence)[:60]}".encode("utf-8")).hexdigest()[:12]


def mine(msg: dict, ctx: Context) -> list:
    """Findings in one message: [{key, kind, class, who, handle, lane, said, date, time, text, word, ...}]."""
    c = ctx.c
    skip = c.get("skip") or {}
    noise = _rx(skip.get("noise") or "")
    text = msg["text"]
    if not text.strip() or (noise and noise.search(text)):
        return []
    if msg.get("ticket_kind") == "inquiry_new":
        return []                              # a lead's form / notification: its date is their wedding, not a booking (2026-10-20)
    cls, who = ctx.classify(msg["handle"], msg["name"], msg["lane"], msg["subject"], msg["bulk"])
    if not str(who or msg["handle"]).strip():
        return []                              # nobody to name on the card (the ': Flags: ...' card, 2026-10-20)
    if msg["source"] == "chat":
        cls = msg["handle"] if msg["handle"] in ("owner1", "owner6") else cls
        agent_rx = _rx(skip.get("chat_handled_by_agent") or "")
        if agent_rx and agent_rx.search(text):
            return []
    elif cls in ("owner1", "owner6") and msg["source"] != "chat":
        cls = "owner6" if cls == "owner6" else None      # the owner's own words outside chat are not mined
        if cls is None:
            return []
    if msg["bulk"] and cls != "money":
        return []
    suspect = False
    if cls == "unknown" and Context._k(who) and Context._k(who) == Context._k(msg["handle"]):
        subject, suspect = ctx.known_subject(msg["handle"])
        who = subject or who                   # a raw number is no title; memory / the wiki know what it is about
    lane = str(((c.get("classes") or {}).get(cls) or {}).get("lane") or msg["lane"] or "personal")
    if msg["lane"] == "owner6" and cls not in ("money",):
        lane = "owner6"                       # a friend texting owner's phone is owner's date (ruling 16)
    said = msg["said"].date() if isinstance(msg["said"], datetime) else msg["said"]
    dcfg = dict(c.get("dates") or {})
    dcfg["max_days_ahead"] = c.get("max_days_ahead", 365)
    time_rx = _rx(dcfg.get("time") or "")
    bare_rx, evening_rx = _rx(dcfg.get("bare_hour") or ""), _rx(dcfg.get("evening_words") or "")
    out = []
    if cls == "money":
        out.extend(_mine_money(msg, ctx, said, lane, who, dcfg, time_rx))
        return out
    pcfg = c.get("people") or {}
    commit, past, future = _rx(pcfg.get("commitment") or ""), _rx(pcfg.get("past") or ""), _rx(pcfg.get("future") or "")
    keep_q = _rx(pcfg.get("keep_questions_with") or "")
    flag_rx, wed_rx = _rx(skip.get("flag_line") or ""), _rx(pcfg.get("lead_wedding_day") or "")
    meet_rx = _rx(pcfg.get("lead_meeting") or "")
    other_msg_rx, other_rx = _rx(pcfg.get("third_party_message") or ""), _rx(pcfg.get("third_party") or "")
    if other_msg_rx and other_msg_rx.search(text):
        return out                             # a prayer request is someone else's day, never our hold (Emily's text, 2026-10-20)
    lead_days = ctx.lead_dates.get(Context._k(msg["handle"]), set()) if cls == "lead" else set()
    sents = [s for s in sentences(text, int(skip.get("min_sentence_chars") or 8)) if not (flag_rx and flag_rx.search(s))]
    prev_commit, prev_found = False, []
    for s in sents:
        if other_rx and other_rx.search(s):
            prev_commit, prev_found = False, []   # a surgery / hospital line is theirs, and lends nothing to the next one
            continue
        has_commit = bool(commit and commit.search(s))
        found = dates_in(s, said, dcfg)
        own_date = bool(found)
        if not found and has_commit and prev_found and time_in(s, time_rx, bare_rx, evening_rx):
            found = prev_found                 # "Are you coming Sunday? Dinner at 5.": the day is one sentence back (a line with no time borrows nothing)
        if not found or not (has_commit or prev_commit):
            prev_commit, prev_found = has_commit, (found if own_date else prev_found)
            continue
        prev_commit, prev_found = has_commit, (found if own_date else prev_found)
        if s.rstrip().endswith("?") and not (keep_q and keep_q.search(s)):
            continue
        if past and past.search(s) and not (future and future.search(s)):
            continue
        if cls == "lead" and wed_rx and wed_rx.search(s) and not (meet_rx and meet_rx.search(s)):
            continue                           # a lead talking about their wedding day: not ours to hold yet (a call about it is)
        for d, word in found:
            if d < said or d.isoformat() in lead_days:
                continue
            t = time_in(s, time_rx, bare_rx, evening_rx)
            out.append({"key": _key(msg["handle"], d, s), "kind": "event", "class": cls, "who": who or msg["handle"],
                        "handle": msg["handle"], "lane": lane, "said": said.isoformat(), "date": d.isoformat(), "time": t,
                        "text": s[:240], "word": word, "source": msg["source"], "ref": msg["ref"], "channel": msg["channel"],
                        "past": d < ctx.today, "suspect": suspect})
    return out


def _mine_money(msg: dict, ctx: Context, said: date, lane: str, who: str, dcfg: dict, time_rx) -> list:
    mcfg = ctx.c.get("money") or {}
    text = f"{msg['subject']}\n{msg['text']}"
    if msg["source"] == "ticket" and msg["channel"] == "email":
        try:
            if mail_arm.classify(msg["subject"], msg["text"], said):
                return []                      # mail_arm arms this one by itself (bill / renewal / shipment)
        except Exception:  # noqa: BLE001
            pass
    extra = _rx(mcfg.get("extra") or "")
    out = []
    for s in sentences(text, 6):
        hit = (extra and extra.search(s)) or mail_arm._DUE.search(s) or mail_arm.RENEW.search(s)
        if not hit:
            continue
        found = [(d, w) for d, w in dates_in(s, said, dcfg) if d >= said]
        if not found:
            continue
        d, word = found[0]
        shape = "renewal" if mail_arm.RENEW.search(s) else ("due" if mail_arm._DUE.search(s) else "action")
        amt = mail_arm._AMOUNT.search(s) or mail_arm._AMOUNT.search(text)
        out.append({"key": _key(msg["handle"], d, s), "kind": "money", "shape": shape, "class": "money", "who": who or msg["handle"],
                    "handle": msg["handle"], "lane": lane, "said": said.isoformat(), "date": d.isoformat(), "time": time_in(s, time_rx),
                    "text": s[:240], "word": word, "amount": ("$" + amt.group(1)) if amt else "", "source": msg["source"],
                    "ref": msg["ref"], "channel": msg["channel"], "subject": msg["subject"][:120], "past": d < ctx.today})
        break                                   # one duty per message
    return out


# ------------------------------------------------------------------ already known?
def _calendar_titles(day: str) -> list:
    cal = C.load_json(C.DATA / "calendar.json", {}) or {}
    out = [str(e.get("title") or "") for e in cal.get("events") or [] if str(e.get("date") or "") == day]
    holds = C.load_json(C.DATA / "holds.json", {}) or {}
    out += [str(h.get("title") or "") for h in holds.get("holds") or [] if str(h.get("date") or "") == day]
    return out


def _reply_handle(ex: dict) -> str:
    """The person an outbound (reply) card writes to, keyed like Context._k ('' when it is not one)."""
    if ex.get("kind") not in ("email_send", "imessage_send", "hb_message"):
        return ""
    to = str((ex.get("payload") or {}).get("to") or "")
    em = _EMAIL.search(to)
    return Context._k(em.group(0) if em else to)


def already_known(f: dict) -> bool:
    """A hold or event on that day sharing two words with the sentence, an open card for the same key,
    or an open reply card to the same person (answering them covers the ask; a hold beside it is noise)."""
    who = Context._k(f.get("handle"))
    def _w(s):
        return {x.replace("'s", "").replace("’s", "") for x in C.words(s)}
    w = _w(f["text"]) | _w(f.get("who"))
    for title in _calendar_titles(f["date"]):
        if len(_w(title) & w) >= 2:
            return True
    for cd in C.cards_live():
        q = (cd.get("questions") or [{}])[0]
        if str(q.get("mining_key") or "") == f["key"]:
            return True
        if who and any(_reply_handle(qq.get("exec") or {}) == who for qq in cd.get("questions") or []):
            return True
        ex = (q.get("exec") or {}).get("payload") or {}
        if (q.get("exec") or {}).get("kind") == "calendar_hold" and str(ex.get("date")) == f["date"] and len(C.words(ex.get("title")) & w) >= 2:
            return True
    return False


# ------------------------------------------------------------------ cards
def event_card(f: dict, ecfg: dict) -> dict:
    who = str(f["who"])[:40]
    gist = _gist(f["text"], int(ecfg.get("gist_words") or 7))
    title = str(ecfg.get("title") or "{who}: {gist}").format(who=who, gist=gist)
    tag = str(ecfg.get("suspect_suffix") or " (unverified)") if f.get("suspect") else ""
    if len(title) + len(tag) > 90:
        title = title[:90 - len(tag)].rsplit(" ", 1)[0].rstrip(",.;:")   # a whole word, never '10/'
    title += tag
    when = C.shortdate(f["date"]) + (f" at {_ampm(f['time'])}" if f.get("time") else "")
    quote = f["text"].strip()
    payload = {"title": title, "date": f["date"], "duration_min": int(ecfg.get("duration_min") or 60),
               "note": f"{who} ({f['channel']}, {C.shortdate(f['said'])}): \"{quote}\""}
    if f.get("time"):
        payload["time"] = f["time"]
    q = {"header": f"Date from {who}"[:40], "lane": f["lane"], "mining_key": f["key"],
         "question": f"{payload['note']} Put a hold on the calendar for {when}: \"{title}\"? Nobody is notified.",
         "details": f"Source: {f['source']} {f['ref']}\nMatched: '{f.get('word')}' near a commitment word.\nHold: {json.dumps(payload, ensure_ascii=False)}",
         "options": [{"label": "Add to calendar (Recommended)", "description": "a hold on our own calendar; nobody is notified"},
                     {"label": "Skip"}],
         "exec": {"kind": "calendar_hold", "approve_label": "Add to calendar (Recommended)", "payload": payload}}
    q["advice"] = advice.line({"kind": "offer", "lane": f["lane"], "title": title, "age_days": C.age_days(f["said"]),
                               "why": f"{who} named {when}; a hold keeps it from being double-booked"})
    return q


def _ampm(hhmm: str) -> str:
    try:
        h, m = C.hm(hhmm)
    except (ValueError, AttributeError):
        return hhmm
    ap = "AM" if h < 12 else "PM"
    h12 = h % 12 or 12
    return f"{h12}:{m:02d} {ap}" if m else f"{h12} {ap}"


def money_fix(f: dict, mcfg: dict) -> dict:
    sop, step = (mcfg.get("followup") or {}).get(f.get("shape") or "action") or ["personal", "reminder"]
    lead = int((mcfg.get("lead_days") or {}).get(f.get("shape") or "", 0) or 0)
    anchor = f["date"]
    if lead and step == "reminder":
        anchor = (date.fromisoformat(f["date"]) - timedelta(days=lead)).isoformat()
    what = (f.get("subject") or f["text"])[:80]
    vars_ = {"what": what, "anchor": anchor, "unit": f"{f['handle']}|{f['date']}", "amount": f.get("amount") or "amount not stated",
             "cancel": "python assistant/scripts/followups.py mark <id> --status done"}
    desc = f"{f['who']}: {what} ({f['shape']} {C.shortdate(f['date'])}{', ' + f['amount'] if f.get('amount') else ''}) -> remind {C.shortdate(anchor)}"
    return {"op": "followup_arm", "sop": sop, "step": step, "vars": vars_, "desc": desc, "mining_key": f["key"]}


def money_card(fs: list, mcfg: dict) -> dict:
    fixes = [money_fix(f, mcfg) for f in fs]
    rows = [f"{i}. {fx['desc']}. \"{f['text'][:140]}\"" for i, (fx, f) in enumerate(zip(fixes, fs), 1)]
    n = len(fixes)
    q = {"header": "Money dates", "lane": "money", "mining_key": ",".join(f["key"] for f in fs),
         "question": f"{n} date{'s' if n != 1 else ''} in money mail. Approve and I arm the reminder{'s' if n != 1 else ''}; nothing is paid and nobody is contacted.\n" + "\n".join(rows),
         "details": "\n".join(f"{i}. {f['who']} ({f['channel']}, {C.shortdate(f['said'])}) source {f['source']} {f['ref']}" for i, f in enumerate(fs, 1)),
         "options": [{"label": f"Remind me ({n}) (Recommended)", "description": "arms the follow-ups; you still pay or act yourself"}, {"label": "Skip"}],
         "exec": {"kind": "tracking_fix", "approve_label": f"Remind me ({n}) (Recommended)", "payload": {"fixes": fixes}}}
    total = 0.0
    for f in fs:
        try:
            total += float(str(f.get("amount") or "0").replace("$", "").replace(",", ""))
        except ValueError:
            pass
    q["advice"] = advice.line({"kind": "offer", "lane": "money", "title": "money dates", "amount": total or None,
                               "why": "each row becomes a reminder before its date; the reminder card is where you pay"})
    return q


# ------------------------------------------------------------------ the run
def _state() -> dict:
    st = C.state("mining", {})
    st.setdefault("seen", {})
    st.setdefault("cursor", {})
    return st


def gather(ctx: Context, since: datetime | None, sources: dict) -> list:
    out = []
    if sources.get("phone_threads", True):
        out += messages_phone(since)
    if sources.get("tickets", True):
        out += messages_tickets(since)
    if sources.get("chat", False):
        out += messages_chat(since, owners={k: v for k, v in ctx.owners.items() if "@" not in k and not k.isdigit()} or {"owner1": "owner1", "owner6": "owner6"})
    if sources.get("honeybook_messages", True):
        out += messages_honeybook(since)
    return out


def findings(ctx: Context, msgs: list) -> list:
    out, keys = [], set()
    for m in msgs:
        for f in mine(m, ctx):
            if f["key"] in keys:
                continue
            keys.add(f["key"])
            out.append(f)
    return out


def tick(now: datetime | None = None, dry: bool = False, days: int | None = None) -> dict:
    c = cfg()
    if not c.get("enabled", True):
        return {"skipped": "disabled"}
    ctx = Context(c, now)
    st = _state()
    lookback = int(days or c.get("lookback_days") or 7)
    floor = ctx.now - timedelta(days=lookback)
    since = C.parse_any(st["cursor"].get("ts")) if not days else None
    since = max(since, floor) if since else floor
    msgs = gather(ctx, since, c.get("sources") or {})
    fs = findings(ctx, msgs)
    fresh = [f for f in fs if (days or f["key"] not in st["seen"]) and not f.get("past")]
    posted, skipped = [], []
    budget = int(c.get("max_cards_per_run") or 3)
    money = [f for f in fresh if f["kind"] == "money"]
    events = [f for f in fresh if f["kind"] == "event"]
    cards_out = []
    if money and budget > 0:
        cards_out.append(("money", money, money_card(money, c.get("money") or {})))
        budget -= 1
    for f in events:
        if budget <= 0:
            break
        if already_known(f):
            skipped.append({"key": f["key"], "why": "already on the calendar / a card is open"})
            st["seen"][f["key"]] = {"ts": ctx.now.strftime("%Y-%m-%d %H:%M"), "skipped": "known"}
            continue
        cards_out.append(("event", [f], event_card(f, c.get("event") or {})))
        budget -= 1
    for kind, group, q in cards_out:
        if dry:
            print("WOULD POST " + json.dumps(q, ensure_ascii=False)[:600])
            qid = "dry"
        else:
            import cards
            r = cards.post([q], owner="tracking.mining", lane=q["lane"])
            qid = str(r.get("qid") or "")
            if not r.get("posted"):
                C.log("mining_post_refused", lane=q["lane"], reason=str(r.get("reason"))[:200])
                for f in group:
                    st["seen"][f["key"]] = {"ts": ctx.now.strftime("%Y-%m-%d %H:%M"), "refused": str(r.get("reason"))[:120]}
                continue
        for f in group:
            st["seen"][f["key"]] = {"ts": ctx.now.strftime("%Y-%m-%d %H:%M"), "qid": qid, "date": f["date"], "who": f["who"]}
        posted.append({"kind": kind, "qid": qid, "n": len(group), "lane": q["lane"]})
        C.log("mining_posted", kind=kind, qid=qid, n=len(group), lane=q["lane"])
    for f in fs:
        if f.get("past") and f["key"] not in st["seen"]:
            st["seen"][f["key"]] = {"ts": ctx.now.strftime("%Y-%m-%d %H:%M"), "skipped": "past"}
    if not dry:
        st["cursor"]["ts"] = ctx.now.strftime("%Y-%m-%d %H:%M:%S")
        if len(st["seen"]) > 4000:
            st["seen"] = dict(sorted(st["seen"].items(), key=lambda kv: kv[1].get("ts", ""))[-3000:])
        C.save_state("mining", st)
    out = {"messages": len(msgs), "findings": len(fs), "fresh": len(fresh), "posted": posted, "skipped": len(skipped)}
    print("MINING " + json.dumps(out, ensure_ascii=False))
    return out


def scan(days: int = 7, now: datetime | None = None) -> list:
    c = cfg()
    ctx = Context(c, now)
    msgs = gather(ctx, ctx.now - timedelta(days=days), c.get("sources") or {})
    fs = findings(ctx, msgs)
    for f in sorted(fs, key=lambda x: (x["date"], x["said"])):
        flag = "past " if f.get("past") else ""
        print(f"{flag}{f['kind']:5} {f['lane']:8} {f['class']:8} {str(f['who'])[:22]:22} {f['date']} {f.get('time') or '     '} "
              f"[{f.get('word')}] {f['text'][:90]!r}")
    print(f"{len(msgs)} messages, {len(fs)} findings")
    return fs


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("cmd", nargs="?", default="tick", choices=["tick", "scan"])
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--days", type=int, default=None)
    a = ap.parse_args()
    if a.cmd == "scan":
        scan(a.days or 7)
    else:
        tick(dry=a.dry_run, days=a.days)

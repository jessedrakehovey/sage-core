"""client_texts.py - the standard client texts, sent without a card under the owner's standing
rule of 2026-10-17. Zero-LLM.

the owner (2026-10-17): "It is my goal and vision for the entire process to be completely
automated with texts to reach out to the client." The words live in
assistant/data/client_texts.json (his voice, his sign-off); this module only decides WHEN
each one goes and to WHOM, and mints the sha-bound `imessage_send` executor ticket from his
own iPhone (`config.json -> leads.text_account`, the owner-iphone) so drain -> phone outbox ->
the "the organization" Shortcut sends it. Quiet hours (21:00-07:00) are held by the
executor as for every outbound; the exec_store resend guards still apply.

Heads-up first (the owner, 2026-10-18: "I would just like to be sure I see the full draft ahead of time
for sends like this and be aware of exactly when the text will go out."): every text is announced in
the shared chat with its full wording, who it goes to and the exact time it goes. None
goes between 21:00 and 09:00 local: one minted then waits in client_texts_state.json `scheduled`
(no ticket yet), the heads-up names 9:00 AM, and `tick` (Day Spine, every 10 min, right before
drain) mints it then.

Lead time + Hold (plan 2026-10-18 section 4): the kinds known days ahead (`texts.preview_lead_kinds`,
default questionnaire_reminder / checkin / meeting_reminder / touches_followup) go no sooner than
`texts.preview_lead_min` minutes (default 60) after their preview, inside the same 09:00-21:00 window.
A text that waits (lead time or overnight) is previewed as ONE card with the full wording and a
"Hold this one" button; the card is not an approval (the text goes by itself at its time) but Hold
pulls it back as a normal imessage_send exec card for the owner's tap, and nothing goes until he taps it.
`tick` reads the card before minting (a Hold tapped minutes before the send still counts); the card
closes itself when the text has gone. The immediate kinds (lead_intro, collection_sent) keep the plain
chat heads-up and go at once.

Kinds and their triggers
  lead_intro              sense_typeform.py the minute a quiz lead with a phone lands (and
                          sense_honeybook.py for a HoneyBook inquiry whose contact came in
                          with the project): "I just saw your wedding form come through..."
  collection_sent         doer.py right after the standing Collection Details send landed
                          (DOER_DONE), and sense_honeybook.py when a lead moves to
                          "Proposal Sent" (the owner sent it himself): "I just sent our collection..."
  questionnaire_reminder  followups wedding-lifecycle T-30 / T-21: the operator checks the
                          Wedding Questionnaire in HoneyBook first, then runs
                          `client_texts.py send questionnaire_reminder --unit <project>`; it is held
                          (CLIENT_TEXT_HELD, an offer-to-send card instead) unless the snapshot shows a
                          sent questionnaire smart file in their project (example, 2026-10-18)
  checkin                 followups wedding-lifecycle T-14 (auto, zero-LLM): sets up the check-in call
  meeting_reminder        `client_texts.py tick` (Day Spine, every 10 min): the day before a
                          HoneyBook meeting / call with a client, or a hold on our calendar
  touches_followup        the text that follows the Finishing Touches file (T-60), after HoneyBook
                          confirms the send: `client_texts.py touches --unit <project> --package <id>
                          [--has a,b] [--skip c]` mints the hb_touches_send ticket (pricing/hb_touches.py);
                          nothing client-facing says "add-ons" (the owner: it has to sound upscale)

Every send is keyed (kind + who) in assistant/data/client_texts_state.json so a sensor
that runs again never texts twice; `texts.kinds.<kind>: false` in config.json switches one
off; `texts.enabled: false` switches them all off (then every kind falls back to the old
path: a ticket for the operator and a card for the owner). The owners' own numbers are never texted.

CLI
  tick                                   meeting reminders due + waiting texts whose time has come (Day Spine)
  hold --key "<kind>|<who>"              pull a waiting text back into an exec card (what the Hold button does)
  send <kind> --unit <project key> [--dry-run]
  touches --unit <project key> --package <id> [--has a,b] [--skip a,b] [--dry-run] [--print]
  preview <kind> [--first Name ...]      render a template with sample vars
  status                                 what went out, latest first
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import alib  # noqa: E402
from alib import DATA, QueueLock, config, load_json, now_iso, save_json  # noqa: E402

try:                                     # the body carries an emoji; a cp1252 console must not crash a sensor
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except (AttributeError, ValueError):
    pass

WORDS = Path(__file__).resolve().parent.parent / "data" / "client_texts.json"   # tracked, next to sop_map.json
STATE = DATA / "client_texts_state.json"
LEADS = DATA / "leads"
STANDING_REF = "standing:client-texts (the owners' standing rule of 2026-10-17: the whole client process is texted from their phone; standard reminders go out without a card)"
IMMEDIATE_KINDS = ("lead_intro", "collection_sent")   # the engine's own kinds: they go at once (or as the urgent card)
CARD_KINDS = ("lead_intro",)             # never by themselves: an urgent send card at once, the owner's tap sends (2026-10-20)
SEND_NOW_LABEL = "Send now (Recommended)"
HOLD_LABEL, GO_LABEL = "Hold this one", "Let it go as planned"                                # the preview card's two buttons
SEND_HOURS = (9, 21)                     # texts go 09:00-21:00 local; outside, they wait for 09:00 (2026-10-18)
REPEAT_DAYS = 7                          # a kind that may go more than once never goes twice inside this many days


# ------------------------------------------------------------------ words + switches
def words() -> dict:
    w = load_json(WORDS, None)
    if not isinstance(w, dict) or not isinstance(w.get("texts"), dict):
        raise RuntimeError(f"missing or unreadable {WORDS}")
    return w


def _kinds() -> tuple:
    """Every kind the words file has a template for (the organization's own list, never code)."""
    try:
        return tuple(str(k) for k in words()["texts"])
    except (RuntimeError, TypeError):
        return IMMEDIATE_KINDS


KINDS = _kinds()


def _ext(name: str):
    """The first plugin (config.json -> plugins.modules) that has `name`, or None: the organization's own resolver."""
    for m in alib.plugins():
        f = getattr(m, name, None)
        if callable(f):
            return f
    return None


def _owner_key() -> str:
    """The first owner's key (people.json): the standing rule is theirs; the record names them."""
    try:
        sys.path.insert(0, str(HERE.parent / "agent"))
        import core
        return str(core.first_owner() or "owner")
    except Exception:  # noqa: BLE001
        return "owner"


def _owner_first_name() -> str:
    try:
        sys.path.insert(0, str(HERE.parent / "agent"))
        import core
        u = (core.people().get("users") or {}).get(_owner_key()) or {}
        return str(u.get("name") or _owner_key()).split()[0]
    except Exception:  # noqa: BLE001
        return "the owner"


def settings() -> dict:
    c = (config().get("texts") or {}) if isinstance(config().get("texts"), dict) else {}
    try:
        lead_min = max(0, int(c.get("preview_lead_min", 60)))
    except (TypeError, ValueError):
        lead_min = 60
    kinds = c.get("preview_lead_kinds")
    ck = c.get("card_kinds")
    out = {"enabled": bool(c.get("enabled", True)), "kinds": dict(c.get("kinds") or {}),
           # kinds that never go by themselves: an URGENT send card the minute they are due; the owner's tap sends
           # (2026-10-20: "I need to have an approval to text new prospects ASAP")
           "card_kinds": tuple(str(k) for k in ck) if isinstance(ck, list) else CARD_KINDS,
           "account": str((config().get("leads") or {}).get("text_account") or ""),
           "from_email": str((config().get("leads") or {}).get("alert_from") or alib._org("alert_from", "") or ""),
           "touches_file": bool(c.get("touches_file", True)),
           "lead_min": lead_min,
           # the T-14 check-in: the owner's words without the rose + the booking link (2026-10-24)
           "checkin_body": str(c.get("checkin_body") or ""),
           "checkin_scheduler_link": str(c.get("checkin_scheduler_link") or "").strip(),
           # numbers the owners said never to text again (2026-10-20): no standing kind goes
           "do_not_text": tuple(_digits(n) for n in (c.get("do_not_text") or []) if _digits(n)),
           "lead_kinds": tuple(str(k) for k in kinds) if isinstance(kinds, list) else tuple(alib.plugin_set("LEAD_KINDS"))}
    for k in alib.plugin_set("SETTING_FLAGS"):          # the resolver's own switches (its sensors read them here)
        out[str(k)] = bool(c.get(k, True))
    return out


def on(kind: str, s: dict | None = None) -> bool:
    s = s or settings()
    return s["enabled"] and bool(s["kinds"].get(kind, True)) and bool(s["account"])


def scheduler_link(s: dict | None = None) -> str:
    """The check-in call booking link from config, or '' when it is not a real booking link on the resolver's
    booking domain (a check-in never goes with a placeholder or without the link: 2026-10-24)."""
    u = (s or settings())["checkin_scheduler_link"]
    m = re.fullmatch(r"https://([a-z0-9.-]+)/\S+", u, re.I)
    host = m.group(1).lower() if m else ""
    if not host or re.search(r"[{}<>]|example|placeholder|paste|xxx", u, re.I):
        return ""
    host_ok = _ext("link_host_ok")
    if host_ok is not None and not host_ok(host):
        return ""
    return u


def first_name(name: str) -> str:
    n = re.sub(r"'s project$", "", str(name or "").strip(), flags=re.I).strip()
    n = re.split(r"\s*(?:&|\band\b|\+|,)\s*", n)[0].strip()
    w = (n.split()[0] if n else "").strip(" .")
    return w.capitalize() if w.islower() else w


def party_names(name: str, partner: str = "") -> str:
    """'example & Dean' from the two names, or whichever one is there."""
    a, b = first_name(name), first_name(partner)
    return f"{a} & {b}" if a and b else a or b


def long_date(d) -> str:
    try:
        dd = date.fromisoformat(str(d)[:10])
    except ValueError:
        return str(d or "")
    return f"{dd:%B} {dd.day}"


def render(kind: str, vars: dict, w: dict | None = None) -> str:
    w = w or words()
    t = (settings()["checkin_body"] if kind == "checkin" else "") or (w["texts"].get(kind) or {}).get("body")
    if not t:
        raise ValueError(f"no template for {kind!r} in {WORDS}")
    v = {"sign": w.get("sign", _owner_first_name()), **{k: ("" if x is None else str(x)) for k, x in vars.items()}}
    out = re.sub(r"\{([a-z_]+)\}", lambda m: v.get(m.group(1), m.group(0)), t)
    if "{" in out and re.search(r"\{[a-z_]+\}", out):
        raise ValueError(f"{kind}: unfilled placeholder in " + ", ".join(sorted(set(re.findall(r"\{([a-z_]+)\}", out)))))
    return out.strip()


def _party_vars(lead: dict) -> dict:
    """Template vars for a lead dict {name, partner?, email?, date?}: the engine's own keys plus the resolver's
    (its words file names them: lead_vars)."""
    out = {"first": first_name(lead.get("name")), "names": party_names(lead.get("name"), lead.get("partner")),
           "email": str(lead.get("email") or ""), "date": long_date(lead.get("date"))}
    f = _ext("lead_vars")
    if f is not None:
        out.update(f(lead) or {})
    return out


# ------------------------------------------------------------------ state
def _state() -> dict:
    st = load_json(STATE, None)
    st = st if isinstance(st, dict) and isinstance(st.get("sent"), dict) else {"sent": {}}
    if not isinstance(st.get("scheduled"), dict):
        st["scheduled"] = {}
    return st


def _now() -> datetime:
    return datetime.now().astimezone()


def send_time(now: datetime | None = None) -> datetime:
    """When a text minted at `now` (local, aware) goes: now inside 09:00-21:00, else the next 09:00."""
    now = now or _now()
    if SEND_HOURS[0] <= now.hour < SEND_HOURS[1]:
        return now
    day = now.date() + timedelta(days=1 if now.hour >= SEND_HOURS[1] else 0)
    return now.replace(year=day.year, month=day.month, day=day.day, hour=SEND_HOURS[0], minute=0, second=0, microsecond=0)


def planned_send(kind: str, now: datetime | None = None, s: dict | None = None) -> datetime:
    """When a text of `kind` minted at `now` goes. A kind known days ahead (texts.preview_lead_kinds)
    goes no sooner than texts.preview_lead_min after its preview, so the owner can hold it; then the
    09:00-21:00 window applies to the result (20:30 + 60 min -> 09:00 tomorrow)."""
    now = now or _now()
    s = s or settings()
    earliest = now + timedelta(minutes=s["lead_min"]) if kind in s["lead_kinds"] and s["lead_min"] > 0 else now
    return send_time(earliest)


def _at(dt: datetime, now: datetime) -> str:
    """'today at 2:05 PM' / 'tomorrow, Sunday October 19 at 9:00 AM'."""
    t = f"{dt:%I:%M %p}".lstrip("0")
    if dt.date() == now.date():
        return f"today at {t}"
    lead = "tomorrow, " if dt.date() == now.date() + timedelta(days=1) else ""
    return f"{lead}{dt:%A %B} {dt.day} at {t}"


def _digits(v) -> str:
    return re.sub(r"\D", "", str(v or ""))[-10:]


def sent_key(kind: str, who: str) -> str:
    return f"{kind}|{who}"


def already(kind: str, who: str, st: dict | None = None) -> dict | None:
    return (st or _state())["sent"].get(sent_key(kind, who))


def repeat_key(kind: str, who: str, days: int = REPEAT_DAYS) -> str:
    """A kind that may go more than once to the same person (a reminder at T-30 and T-21): one key per send,
    never a second inside `days` (then the last key comes back -> "already sent")."""
    st = _state()
    n = sum(1 for k in st["sent"] if k.startswith(f"{kind}|{who}#"))
    last = max((v.get("ts", "") for k, v in st["sent"].items() if k.startswith(f"{kind}|{who}#")), default="")
    if last and last > (datetime.now(timezone.utc) - timedelta(days=days)).strftime("%Y-%m-%dT%H:%M:%SZ"):
        return f"{who}#{n - 1}"            # same key as the last one -> "already sent"
    return f"{who}#{n}"


# ------------------------------------------------------------------ who: phone resolution
def _valid(to: str) -> str:
    import phone_bridge
    h = phone_bridge.norm_handle(to)
    return h if h and "@" not in h and phone_bridge.valid_handle(h) else ""


def _owner_numbers() -> set:
    try:
        import text_gate
        return text_gate.owner_numbers() | text_gate.household_numbers()
    except Exception:  # noqa: BLE001
        return set()


def _lead_files() -> list:
    out = []
    try:
        for f in sorted(LEADS.glob("*.json")):
            q = load_json(f, None)
            if isinstance(q, dict) and isinstance(q.get("lead"), dict):
                out.append((f.stem, q))
    except OSError:
        pass
    return out


def resolve_phone(project: dict | None = None, **kw) -> dict:
    """Who a text goes to, by the organization's resolver (plugins): {"to", "first", "email", "via", ...} or {}."""
    f = _ext("resolve_phone")
    return (f(project, **kw) or {}) if f is not None else {}


# ------------------------------------------------------------------ the mint
def mint_text(kind: str, who: str, to: str, vars: dict, *, label: str = "", resend_ok: bool = False, details: str = "") -> dict:
    """ONE standing imessage_send ticket. -> {"ok", "ticket"|"reason", "body"}. Never raises.
    A kind in card_kinds mints nothing: it is posted as an URGENT send card at once (-> {"ok", "card"})."""
    s = settings()
    if not on(kind, s):
        return {"ok": False, "reason": f"{kind} is switched off (assistant/config.json -> texts)"}
    to = _valid(to)
    if not to:
        return {"ok": False, "reason": "no valid phone number"}
    if _digits(to) in _owner_numbers():
        return {"ok": False, "reason": "that is an owner's own number"}
    if _digits(to) in s["do_not_text"]:
        return {"ok": False, "reason": "the owners said not to text this number (config texts.do_not_text)"}
    try:
        body = render(kind, vars)
    except (ValueError, RuntimeError) as e:
        return {"ok": False, "reason": str(e)}
    key = sent_key(kind, who)
    now = _now()
    at = planned_send(kind, now, s)
    with QueueLock("client_texts") as ok:
        if not ok:
            return {"ok": False, "reason": "busy, retry"}
        st = _state()
        prior = st["sent"].get(key)
        if prior and not resend_ok:
            return {"ok": False, "reason": f"already sent {prior.get('ts')} (ticket {prior.get('ticket')})", "ticket": prior.get("ticket"),
                    "card": prior.get("card"), "dup": True}
        if kind in s["card_kinds"]:
            # 2026-10-20: the new-prospect text is the OWNER'S tap, as fast as the lead lands. The dedupe
            # record is written first (one card per number, ever), then the urgent card; a card that
            # could not be posted leaves the words in chat so the owner can send them.
            st["sent"][key] = {"ts": now_iso(), "kind": kind, "to": to, "ticket": None, "label": label[:120], "body": body[:400],
                               "carded": now_iso()}
            save_json(STATE, st)
            row, card_it = None, True
        else:
            card_it = False
        if card_it:
            pass
        elif at > now:
            # lead time (a kind known days ahead) or after 21:00 / before 09:00: held here with no
            # ticket, so the phone cannot send it before its time, and the owner can still hold it
            st["scheduled"][key] = {"key": key, "kind": kind, "to": to, "body": body, "account": s["account"], "label": label[:120],
                                    "resend_ok": bool(resend_ok), "send_at": at.isoformat(timespec="seconds")}
            st["sent"][key] = {"ts": now_iso(), "kind": kind, "to": to, "ticket": None, "label": label[:120], "body": body[:400],
                               "scheduled_for": at.isoformat(timespec="seconds")}
            save_json(STATE, st)
            row = None
        else:
            try:
                import exec_store as es
                row = es.enqueue("imessage_send", {"account": s["account"], "to": to, "body": body}, es.APPROVER, STANDING_REF, resend_ok=resend_ok)
            except (ValueError, PermissionError) as e:
                return {"ok": False, "reason": f"refused: {str(e)[:200]}", "body": body}
            st["sent"][key] = {"ts": now_iso(), "kind": kind, "to": to, "ticket": row["id"], "label": label[:120], "body": body[:400]}
            save_json(STATE, st)
    if card_it:
        r = _lead_card(kind, label or who, to, body, key, s["account"], details=details)
        if r.get("card"):
            with QueueLock("client_texts") as ok:
                if ok:
                    st = _state()
                    if isinstance(st["sent"].get(key), dict):
                        st["sent"][key]["card"] = r["card"]
                        save_json(STATE, st)
        return {"ok": bool(r.get("card")), "ticket": None, "card": str(r.get("card") or ""), "body": body, "to": to,
                "reason": r.get("reason", "")}
    if row is None:
        qid = _preview_card(kind, label or who, to, body, at, now, key)
        if qid:
            _attach_card(key, qid)
        return {"ok": True, "ticket": None, "scheduled_for": at.isoformat(timespec="seconds"), "body": body, "to": to, "card": qid}
    _heads_up(kind, label or who, to, body, f"now ({_at(now, now)})")
    return {"ok": True, "ticket": row["id"], "body": body, "to": to}


def _lead_card(kind: str, who: str, to: str, body: str, key: str, account: str, details: str = "") -> dict:
    """The URGENT send card for a kind in card_kinds (the new-prospect intro, 2026-10-20: "I need
    to have an approval to text new prospects ASAP when a new one comes in with phone number"). Reaches
    Chat the minute it is posted, on its own (cards.is_urgent: "urgent": true + the "New lead" header),
    never waits for the weekly digest. The owner's tap mints the sha-bound imessage_send; Skip = it never goes.
    `details` carries the lead brief (date, place, budget, proposal status) so no second card is needed."""
    kw = kind.replace("_", " ")
    lane = alib.business_lane()
    q = {"header": f"New lead: {who}"[:40], "lane": lane, "subject": key, "lead_text": key, "urgent": True, "audience": "client",
         "question": (f"New lead with a phone number: {who}. Text them the {kw} now?\n"
                      f"[TEXT PREVIEW] To {who} ({to}):\n{body}"),
         "advice": "Goes from your phone the moment you tap (after 7 AM if it is night). Edit & send changes the words first.",
         "options": [{"label": SEND_NOW_LABEL, "description": "sends exactly this from your phone"},
                     {"label": "Skip", "description": "it never goes"}],
         "exec": {"kind": "imessage_send", "approve_label": SEND_NOW_LABEL, "payload": {"account": account, "to": to, "body": body}}}
    if details:
        q["details"] = str(details)[:3000]
    reason = ""
    try:
        import cards
        r = cards.post([q], "client_texts", lane=lane)
        if r.get("posted"):
            return {"ok": True, "card": str(r["qid"])}
        reason = str(r.get("reason") or "")[:160]
        if r.get("qid") and reason.startswith(("DUPLICATE_SUBJECT", "ALREADY_ANSWERED")):
            return {"ok": False, "card": "", "reason": reason}       # that number already has its card / answer
    except Exception as e:  # noqa: BLE001
        reason = f"{type(e).__name__}: {str(e)[:120]}"
    try:
        import cards
        cards.say("client_texts", lane, f"New lead {who} ({to}) - I could not post the send card for the {kw} text "
                                        f"({reason}). Nothing went. The words, if you want to send them yourself:\n\n{body}")
    except Exception:  # noqa: BLE001
        pass
    return {"ok": False, "card": "", "reason": reason, "body": body, "to": to}


def release_due(now: datetime | None = None) -> dict:
    """tick: mint every waiting text whose time has come, unless the owner tapped Hold on its preview
    card (then it comes back as an exec card for their tap). -> {"released", "held", "refused"}"""
    now = now or _now()
    out = {"released": 0, "held": 0, "refused": []}
    if not SEND_HOURS[0] <= now.hour < SEND_HOURS[1]:
        return out
    to_hold, closed = [], []
    with QueueLock("client_texts") as ok:
        if not ok:
            return out
        st = _state()
        for key, v in list(st["scheduled"].items()):
            at = _iso(v.get("send_at"))
            if not at or at > now:
                continue
            qid = str(v.get("card") or "") or _card_for(key)
            if _card_answer(qid) == "hold":
                to_hold.append(key)                              # the sweep has not converted it yet: do it now, never send
                continue
            del st["scheduled"][key]
            if _digits(v.get("to")) in settings()["do_not_text"]:
                out["refused"].append(f"{v.get('label')}: on the do-not-text list")
                closed.append((qid, "not sent: the owners said not to text them"))
                continue
            if not on(str(v.get("kind") or "")):
                st["sent"].pop(key, None)                    # switched off overnight: it never went
                out["refused"].append(f"{v.get('label')}: {v.get('kind')} is switched off")
                closed.append((qid, f"not sent: {v.get('kind')} is switched off"))
                continue
            try:
                import exec_store as es
                row = es.enqueue("imessage_send", {"account": v["account"], "to": v["to"], "body": v["body"]}, es.APPROVER, STANDING_REF,
                                 resend_ok=bool(v.get("resend_ok")))
            except (ValueError, PermissionError, KeyError) as e:
                st["sent"].pop(key, None)
                out["refused"].append(f"{v.get('label')}: refused: {str(e)[:200]}")
                closed.append((qid, f"not sent: {str(e)[:120]}"))
                continue
            if isinstance(st["sent"].get(key), dict):
                st["sent"][key]["ticket"] = row["id"]
            out["released"] += 1
            closed.append((qid, "went out from your phone as previewed"))
        save_json(STATE, st)
    for key in to_hold:
        if hold(key, by=f"{_owner_key()} (Hold on the preview card)").get("ok"):
            out["held"] += 1
    for qid, why in closed:
        _close_card(qid, why)
    for r in out["refused"]:
        _note(f"A text held for the morning did not go: {r}")
    return out


def _heads_up(kind: str, who: str, to: str, body: str, when: str) -> None:
    """Before a standard text goes NOW: one plain chat line with the full wording, who and when.
    Informational, never a card (the text stays automatic); Mission Control gets it as a note."""
    text = f"Heads-up: the {kind.replace('_', ' ')} text to {who} ({to}) goes out from your phone {when}. It says:\n\n{body}"
    try:
        import cards
        cards.say("client_texts", alib.business_lane(), text[:1500])
    except Exception:  # noqa: BLE001
        _note(text)


def _preview_card(kind: str, who: str, to: str, body: str, at: datetime, now: datetime, key: str) -> str:
    """Before a standard text that WAITS (lead time or overnight): ONE card with the full wording, who
    and exactly when, and a Hold button (2026-10-18: see the full draft ahead of time, know
    exactly when it goes). Not an approval: the text goes by itself at `at` unless the owner taps Hold,
    which pulls it back as a normal exec card for their tap. -> qid, or '' (the plain heads-up line
    went instead and the text still goes at its time)."""
    when = _at(at, now)
    kw = kind.replace("_", " ")
    overnight = not SEND_HOURS[0] <= now.hour < SEND_HOURS[1]
    q = {"header": f"{kw.capitalize()}: {who}"[:40], "lane": alib.business_lane(), "subject": key, "hold_text": key,
         "question": (f"The standard {kw} text to {who} goes out from your phone {when}, by itself under your standing rule"
                      + (" (texts never go between 9 PM and 9 AM)" if overnight else "")
                      + f". Tap {HOLD_LABEL} and it waits for your tap instead.\n[TEXT PREVIEW] To {who} ({to}):\n{body}"),
         "no_recommendation": "the text goes by itself under the standing rule; this card only exists to pull it back",
         "options": [{"label": HOLD_LABEL, "description": "it comes back as a card for your tap; nothing goes until then"},
                     {"label": GO_LABEL, "description": f"it goes {when} as written"}]}
    try:
        import cards
        # open until the text has gone (release_due closes it); the slack covers a late spine tick
        r = cards.post([q], "client_texts", expires_s=max(900, int((at - now).total_seconds()) + 1800), lane=alib.business_lane())
        if r.get("posted"):
            return str(r["qid"])
        reason = str(r.get("reason") or "")[:100]
    except Exception as e:  # noqa: BLE001
        reason = f"{type(e).__name__}"
    _heads_up(kind, who, to, body, f"{when} (no Hold button this time: {reason})")
    return ""


def _attach_card(key: str, qid: str) -> None:
    with QueueLock("client_texts") as ok:
        if not ok:
            return                                           # _card_for finds it by key when needed
        st = _state()
        if key in st["scheduled"]:
            st["scheduled"][key]["card"] = qid
            save_json(STATE, st)


def _card_for(key: str) -> str:
    """The open preview card for a waiting text, when the state file does not name it."""
    try:
        import cards
        for c in cards._live():                          # open, or answered and not yet swept
            if c.get("status") in ("open", "answered") and (c.get("questions") or [{}])[0].get("hold_text") == key:
                return str(c["id"])
    except Exception:  # noqa: BLE001
        pass
    return ""


def _card_answer(qid: str) -> str:
    """'hold' / 'go' / '' for a preview card (the owner's last tap, undo respected)."""
    if not qid:
        return ""
    try:
        import cards
        a = str(cards.answer_of(qid) or "").strip().lower()
    except Exception:  # noqa: BLE001
        return ""
    return "hold" if a.startswith("hold") else ("go" if a else "")


def _close_card(qid: str, why: str) -> None:
    if not qid:
        return
    try:
        import cards
        cards.close_card(qid, why)
    except Exception:  # noqa: BLE001
        pass


def hold(key: str, by: str = "") -> dict:
    """The owner tapped Hold on a preview card (or ran `hold --key`): the waiting text is taken off the
    standing path and comes back as a normal imessage_send exec card for their tap. Nothing goes until
    then. The dedupe record stays, so the standing path never re-mints it. -> {"ok", "card"|"reason"}"""
    by = by or _owner_key()
    with QueueLock("client_texts") as ok:
        if not ok:
            return {"ok": False, "reason": "busy, retry"}
        st = _state()
        v = st["scheduled"].pop(key, None)
        if not v:
            return {"ok": False, "reason": "that text is not waiting any more (already sent, or already held)"}
        rec = st["sent"].get(key)
        if isinstance(rec, dict):
            rec.update(held=now_iso(), held_by=str(by)[:60], scheduled_for=None)
        save_json(STATE, st)
    r = _exec_card(v)
    if r.get("card"):
        with QueueLock("client_texts") as ok:
            if ok:
                st = _state()
                if isinstance(st["sent"].get(key), dict):
                    st["sent"][key]["card"] = r["card"]
                    save_json(STATE, st)
    _close_card(str(v.get("card") or ""), "held: waiting for your tap on the send card")
    try:
        import exec_store as es
        es.ledger("client_text_held", key=key, by=str(by)[:60], card=str(r.get("card") or ""), ok=bool(r.get("ok")))
    except Exception:  # noqa: BLE001
        pass
    return r


def _exec_card(v: dict) -> dict:
    """The held text as a normal send card: the owner's tap mints the sha-bound imessage_send."""
    kw, who, to, body = str(v.get("kind") or "").replace("_", " "), str(v.get("label") or "them"), str(v.get("to") or ""), str(v.get("body") or "")
    label = "Send (Recommended)"
    lane = alib.business_lane()
    q = {"header": f"Text to {who}"[:40], "lane": lane, "subject": str(v.get("key") or ""),
         "question": f"Held at your request: the standard {kw} text to {who}. Send it?\n[TEXT PREVIEW] To {who} ({to}):\n{body}",
         "advice": "Tap Edit & send to change the wording first.",
         "options": [{"label": label, "description": "sends exactly this from your phone"},
                     {"label": "Skip", "description": "it never goes"}],
         "exec": {"kind": "imessage_send", "approve_label": label, "payload": {"account": str(v.get("account") or ""), "to": to, "body": body}}}
    reason = ""
    try:
        import cards
        r = cards.post([q], "client_texts", lane=lane)
        if r.get("posted"):
            return {"ok": True, "card": str(r["qid"])}
        reason = str(r.get("reason") or "")[:160]
    except Exception as e:  # noqa: BLE001
        reason = f"{type(e).__name__}: {str(e)[:120]}"
    # the hold stands: nothing goes by itself. Say so, with the words, so the owner can send it.
    try:
        import cards
        cards.say("client_texts", lane, f"I held the {kw} text to {who} ({to}) as you asked, but couldn't post it as a send card "
                                        f"({reason}). Nothing went. The words, if you want to send them yourself:\n\n{body}")
    except Exception:  # noqa: BLE001
        pass
    return {"ok": False, "reason": reason, "body": body, "to": to}


def _note(text: str) -> None:
    try:
        import cards
        cards.note("client_texts", text[:600])
    except Exception:  # noqa: BLE001
        pass


# ------------------------------------------------------------------ the kinds
def lead_intro(lead: dict, who: str, *, label: str = "", details: str = "") -> dict:
    """lead: {name, partner?, phone, email?}. who: the quiz token / project id (dedupe key is the number).
    `details`: the lead brief for the card (date, place, budget, what happened in the CRM)."""
    to = _valid(lead.get("phone"))
    if not to:
        return {"ok": False, "reason": "the lead gave no usable phone number"}
    v = _party_vars(lead)
    return mint_text("lead_intro", _digits(to), to, v, label=label or v["names"] or who, details=details)


def collection_sent(lead: dict, who: str = "", *, label: str = "") -> dict:
    """lead: {name, partner?, phone, email, date?}: the person the proposal went to."""
    to = _valid(lead.get("phone"))
    if not to:
        return {"ok": False, "reason": "no usable phone number for them"}
    v = _party_vars(lead)
    v["email"] = str(lead.get("email") or "your email")
    return mint_text("collection_sent", _digits(to), to, v, label=label or v["names"] or who)


def unit_kinds() -> tuple:
    """The kinds sent per booked unit (the resolver's UNIT_KINDS)."""
    return tuple(alib.plugin_set("UNIT_KINDS"))


def send_for_unit(kind: str, unit: str, *, dry: bool = False, resend_ok: bool = False) -> dict:
    """A reminder kind for a booked unit in the organization's ledger: the resolver says who and with which
    words (unit_target); the engine mints."""
    if kind not in unit_kinds():
        return {"ok": False, "reason": f"{kind} is not a per-unit kind"}
    f = _ext("unit_target")
    if f is None:
        return {"ok": False, "reason": "no resolver for per-unit texts (assistant/config.json -> plugins.modules)"}
    r = f(kind, unit) or {}
    if not r.get("ok") or not r.get("to"):
        return r if r else {"ok": False, "reason": f"nobody to text for {unit}"}
    vars = dict(r.get("vars") or {})
    if dry:
        return {"ok": True, "dry": True, "to": r["to"], "body": render(kind, vars), "via": r.get("via", "")}
    res = mint_text(kind, str(r.get("who") or _digits(r["to"])), r["to"], vars, label=str(r.get("label") or ""), resend_ok=resend_ok)
    res["via"] = r.get("via", "")
    return res


# ------------------------------------------------------------------ reminders (tick)
def _iso(s):
    try:
        return datetime.fromisoformat(str(s).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None


def _when(dt_utc: datetime, ref: datetime) -> str:
    """'tomorrow at 3:00 PM' as read at `ref` (the moment the text GOES, not the moment it is
    minted: a reminder minted at 20:30 and sent at 09:00 says 'today')."""
    loc, r = dt_utc.astimezone(), ref.astimezone()
    if loc.date() == r.date():
        day = "today"
    elif loc.date() == (r + timedelta(days=1)).date():
        day = "tomorrow"
    else:
        day = f"on {loc:%A}"
    return f"{day} at {loc:%I:%M %p}".replace(" 0", " ")


def due_reminders(now: datetime | None = None, say_at: datetime | None = None) -> list:
    """Every reminder due now, from every resolver: [{kind, id, to, label, vars}]."""
    out = []
    for rows in alib.plugin_each("due_reminders", now, say_at):
        out += [r for r in (rows or []) if isinstance(r, dict) and r.get("kind") and r.get("to")]
    return out


def tick(now: datetime | None = None, dry: bool = False) -> dict:
    out = {"due": 0, "sent": 0, "skipped": 0, "refused": []}
    if not dry:
        rel = release_due()
        out["released"], out["held"] = rel["released"], rel["held"]
        out["refused"] += rel["refused"]
    s = settings()
    # say_at: when a reminder of that kind will actually GO (its "tomorrow"/"today" is read from then)
    for m in due_reminders(now, say_at=lambda kind: planned_send(str(kind), _now(), s)):
        kind = str(m["kind"])
        if not on(kind, s):
            continue
        if True:
            out["due"] += 1
            if already(kind, str(m["id"])):
                out["skipped"] += 1
                continue
            if dry:
                continue
            r = mint_text(kind, str(m["id"]), m["to"], dict(m.get("vars") or {}), label=str(m.get("label") or ""))
            if r.get("ok"):
                out["sent"] += 1
            elif r.get("dup"):
                out["skipped"] += 1
            else:
                out["refused"].append(f"{m.get('label')}: {r.get('reason')}")
    return out


# ------------------------------------------------------------------ the T-60 file and its follow-up text (the resolver's)
def touches_for(package_id: str, has: list | None = None, skip: list | None = None) -> list:
    f = _ext("touches_for")
    return list(f(package_id, has, skip) or []) if f is not None else []


def touches_email(project: dict, *a, **kw) -> tuple:
    f = _ext("touches_email")
    if f is None:
        raise ValueError("no resolver for the T-60 file (assistant/config.json -> plugins.modules)")
    return f(project, *a, **kw)


def touches(unit: str, package_id: str, has: list | None = None, skip: list | None = None, dry: bool = False) -> dict:
    """Mint ONE standing ticket that places the T-60 file in the unit's CRM project and sends it; the
    follow-up text goes once the CRM confirms the send (drain)."""
    f = _ext("touches")
    if f is None:
        return {"ok": False, "reason": "no resolver for the T-60 file (assistant/config.json -> plugins.modules)"}
    return f(unit, package_id, has, skip, dry=dry)


def touches_sent(payload: dict) -> dict:
    """drain.py after the file's send landed: the follow-up text."""
    f = _ext("touches_sent")
    return f(payload) if f is not None else {"ok": False, "reason": "no resolver for the follow-up text"}


# ------------------------------------------------------------------ followups hook (zero-LLM steps)
def auto_fire(step: str, unit: str, vars: dict) -> dict:
    """followups._fire calls this for a lifecycle step whose action has `auto: client_texts:<kind>`.
    ok -> the duty is done without a ticket; anything else -> the normal operator ticket."""
    kind = str(vars.get("kind") or step)
    if kind not in KINDS:
        return {"ok": False, "reason": f"{kind} is not a text kind"}
    return send_for_unit(kind, unit)


# ------------------------------------------------------------------ CLI
def main() -> int:
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("tick"); p.add_argument("--dry-run", action="store_true")
    p = sub.add_parser("send"); p.add_argument("kind", choices=KINDS); p.add_argument("--unit", required=True)
    p.add_argument("--dry-run", action="store_true"); p.add_argument("--resend", action="store_true", help="a second send inside the dedupe window (an owner asked)")
    p = sub.add_parser("touches"); p.add_argument("--unit", required=True); p.add_argument("--package", required=True)
    p.add_argument("--has", default="", help="item ids they already bought on top of the package, comma-separated")
    p.add_argument("--skip", default="", help="item ids not to offer, comma-separated")
    p.add_argument("--dry-run", action="store_true"); p.add_argument("--print", action="store_true")
    p = sub.add_parser("preview"); p.add_argument("kind", choices=KINDS); p.add_argument("--first", default="example")
    p = sub.add_parser("hold", help="pull a waiting text back into a send card for the owner's tap (the preview card's Hold button)")
    p.add_argument("--key", required=True, help="'<kind>|<who>' as `status` lists it")
    sub.add_parser("status")
    a = ap.parse_args()
    if a.cmd == "tick":
        r = tick(dry=a.dry_run)
        print("CLIENT_TEXTS tick " + json.dumps(r))
        return 0
    if a.cmd == "hold":
        r = hold(a.key, by="cli")
        print(("CLIENT_TEXT_HELD " if r.get("ok") else "CLIENT_TEXT_NOT_HELD ") + json.dumps({k: v for k, v in r.items() if k != "body"}, ensure_ascii=False))
        return 0 if r.get("ok") else 1
    if a.cmd == "send":
        r = send_for_unit(a.kind, a.unit, dry=a.dry_run, resend_ok=a.resend)
        print(("CLIENT_TEXT_OK " if r.get("ok") else "CLIENT_TEXT_HELD ") + json.dumps({k: v for k, v in r.items() if k != "body"}, ensure_ascii=False))
        if r.get("body"):
            print(r["body"])
        return 0 if r.get("ok") else 1
    if a.cmd == "touches":
        r = touches(a.unit, a.package, [x.strip() for x in a.has.split(",") if x.strip()], [x.strip() for x in a.skip.split(",") if x.strip()], dry=a.dry_run)
        print(("TOUCHES_OK " if r.get("ok") else "TOUCHES_HELD ") + json.dumps({k: v for k, v in r.items() if k not in ("body", "text_body")}, ensure_ascii=False))
        if a.print or a.dry_run:
            print("\n" + r.get("subject", "") + "\n\n" + r.get("body", "") + ("\n\n[TEXT] " + r["text_body"] if r.get("text_body") else ""))
        return 0 if r.get("ok") else 1
    if a.cmd == "preview":
        vars = {"first": a.first, "names": f"{a.first} & Dean", "email": "them@example.com", "date": "July 30",
                "what": "check-in call", "when": "tomorrow at 3:00 PM",
                "scheduler_link": scheduler_link() or "(the booking link is not set yet)"}
        f = _ext("sample_vars")
        if f is not None:
            vars.update(f(a.first) or {})
        print(render(a.kind, vars))
        return 0
    if a.cmd == "status":
        rows = sorted(_state()["sent"].values(), key=lambda v: v.get("ts", ""), reverse=True)
        for k, v in sorted(_state()["scheduled"].items(), key=lambda kv: kv[1].get("send_at", "")):
            print(f"waits for {v.get('send_at', '')[:16]}  {v.get('kind', ''):22} {str(v.get('label') or '')[:28]:28} ...{str(v.get('to', ''))[-4:]}"
                  f"  card {v.get('card') or '-'}  key {k}")
        for v in rows[:40]:
            print(f"{v.get('ts', '')[:16]}  {v.get('kind', ''):22} {str(v.get('label') or '')[:28]:28} ...{str(v.get('to', ''))[-4:]}  {v.get('ticket', '')}")
        print(f"{len(rows)} standing sends on record")
        return 0
    return 0


if __name__ == "__main__":
    sys.exit(main())

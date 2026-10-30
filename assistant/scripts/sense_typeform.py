"""sense_typeform.py - new Package Finder quiz leads, minutes after they submit. Zero-LLM.

Replaces the Typeform -> Apps Script -> Sheet -> Zapier relay as the alert path (that relay
went dark for a month in Aug 2026 and its "Email a team member" Zap never fired for a real lead).

Every run: read completed responses of the Film Package Finder form straight from the
Typeform API, and for each one not seen before:
  1. price it (pricing/engine.py - the owner's sheet is the source of every number)
  2. mint ONE CRM draft exec ticket (connector's PROPOSAL_DRAFT_KIND): the CRM executor puts a priced DRAFT of
     "Videography Collection Details" in the couple's CRM project. This program never publishes.
     Provenance: the owner's standing rule of 2026-09-29 (every CRM action except deletions).
     2026-10-06: collection_autosend.decide() rides on that payload; for a weekend wedding date that
     is open in the CRM, drain.py then hands the doer ONE pre-approved Publish & share (no card).
  3. add an `inquiry_new` ticket (lane wedding) carrying the brief for a team member + the first text
  4. arm the inquiry reply-SLA follow-up (2 h) and put a card on Mission Control
  5. when config leads.auto_email is true, mint an `email_send` to config leads.alert_to
     (internal brief only; a couple is never a recipient of anything from this script)

The first run only records what already exists (no backfill). State: data/typeform_seen.json.
Health: data/typeform_status.json (sentinels.py reads it).

  python assistant/scripts/sense_typeform.py [--dry-run] [--replay <response token>]
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parent.parent / "pricing"))

from alib import DATA, config, load_json, now_iso, save_json  # noqa: E402
import crm

VAULT = HERE.parent.parent
FORM_ID = "SnWy5WAe"
SEEN = DATA / "typeform_seen.json"
STATUS = DATA / "typeform_status.json"
LEADS = DATA / "leads"
STANDING_REF = "standing:crm-actions-allowed (owner1 2026-09-29: all CRM actions except deletions)"
SOLICIT_MARKS = ("white-label", "white label", "editing team", "outsourc", "trial edit", "our services",
                 "post production house", "seo ", "backlink")

def _orgcfg():
    """assistant/orgcfg.py (WP5): the organization's own values from assistant/org.json; the literal below is the fallback."""
    import sys as _sys
    from pathlib import Path as _P
    root = str(_P(__file__).resolve().parents[1])
    if root not in _sys.path:
        _sys.path.insert(0, root)
    import orgcfg
    return orgcfg


try:   # assistant/org.json (WP5): the organization's own addresses; the literals are the fallback
    INTERNAL_ONLY = set(_orgcfg().get("internal_emails") or []) or {"owner2@example.invalid", "owner3@example.invalid", "owner4@example.invalid"}
    DEFAULTS = {"alert_to": list(_orgcfg().get("alert_to") or ["owner2@example.invalid", "owner3@example.invalid"]),
                "alert_from": str(_orgcfg().get("alert_from") or "owner3@example.invalid"), "auto_email": False, "create_drafts": True}
except Exception:  # noqa: BLE001
    INTERNAL_ONLY = {"owner2@example.invalid", "owner3@example.invalid", "owner4@example.invalid"}
    DEFAULTS = {"alert_to": ["owner2@example.invalid", "owner3@example.invalid"], "alert_from": "owner3@example.invalid",
                "auto_email": False, "create_drafts": True}


def _token() -> str:
    t = os.environ.get("TYPEFORM_TOKEN", "")
    if not t:                       # setx'd after this process tree started: read the User env directly
        try:
            import winreg
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, "Environment") as k:
                t = winreg.QueryValueEx(k, "TYPEFORM_TOKEN")[0]
        except OSError:
            t = ""
    return t.strip()


def fetch(token: str) -> list[dict]:
    req = urllib.request.Request(
        f"https://api.typeform.com/forms/{FORM_ID}/responses?page_size=50&completed=true",
        headers={"Authorization": f"Bearer {token}"})
    with urllib.request.urlopen(req, timeout=40) as r:
        return json.load(r).get("items", [])


def pacific(iso: str) -> str:
    """Typeform stamps UTC; the owner reads Pacific."""
    try:
        dt = datetime.fromisoformat(iso.replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return iso or ""
    try:
        from zoneinfo import ZoneInfo
        dt = dt.astimezone(ZoneInfo("America/Los_Angeles"))
    except Exception:
        dt = dt.astimezone(timezone(timedelta(hours=-7 if 3 <= dt.month <= 10 else -8)))
    return dt.strftime("%a %b %d, %I:%M %p PT").replace(" 0", " ")


def availability(d: str | None) -> tuple[str, str | None]:
    """(line for the brief, flag or None). Is that wedding date already booked? Read-only, from
    the CRM snapshot (booked-category stages, test projects and locally cancelled ones
    excluded). The old Apps Script 'failed open'; this never says a date is free when it could
    not look: an unreadable or stale snapshot is itself a flag."""
    if not d:
        return "no wedding date given", None
    try:
        snap = load_json(crm.snapshot_path(), None)
        ts = datetime.fromisoformat(str(snap["ts"])[:19]).replace(tzinfo=timezone.utc)
        age_h = (datetime.now(timezone.utc) - ts).total_seconds() / 3600
        booked_stage = {s["stage_id"] for s in snap["stages"] if s.get("category") == "booked" and s.get("name") != "Completed"}
        state = load_json(VAULT / "scripts" / "projects" / "project_state.json", {}).get("projects", {})
        cancelled = {crm.project_id(v) for v in state.values() if isinstance(v, dict) and v.get("stage") == "cancelled" and crm.project_id(v)}
        undated = sum(1 for p in snap["projects"] if not p.get("date") and p.get("stage_id") in booked_stage)
        hits = [p.get("name", "?") for p in snap["projects"]
                if p.get("date") == d and p.get("stage_id") in booked_stage
                and str(p.get("project_id") or p.get("id")) not in cancelled and str(p.get("id")) not in cancelled
                and not re.match(r"\s*(zz[\s_-]*test|test automation)", str(p.get("name") or ""), re.I)]
    except Exception as e:
        return "NOT checked", f"availability NOT checked ({type(e).__name__}) - confirm {d} is open before replying"
    if hits:
        return f"ALREADY BOOKED that day: {'; '.join(hits)[:120]}", f"DATE CONFLICT: {d} already has a booked project ({'; '.join(hits)[:80]}) - check before replying"
    if age_h > 36:
        return f"NOT checked ({crm.name()} snapshot is {age_h:.0f} h old)", f"availability NOT checked ({crm.name()} snapshot {age_h:.0f} h old) - confirm {d} is open"
    return (f"no booked project on {d} ({crm.name()}, {age_h:.0f} h ago"
            + (f"; {undated} booked project(s) have no date and were not checked" if undated else "") + ")"), None


def brief(q: dict, when: str) -> tuple[str, str]:
    """(subject, body) of the internal hand-off to a team member. Plain text, no client is addressed."""
    L, a = q["lead"], q.get("answers", {})
    couple = L["name"] + (f" & {L['partner']}" if L.get("partner") else "")
    lst = lambda v: ", ".join(v) if isinstance(v, list) else (v or "-")  # noqa: E731
    rank = a.get("shot_rank") or []
    tiers = "\n".join(
        f"- {t['name']} ${t['price']:,}"
        + (" (recommended)" if t["package_id"] == q["recommended"] else "")
        + (" - within their budget" if t.get("in_budget") else "")
        + (f" - add for them: {', '.join(t['added_for_couple'])}" if t["added_for_couple"] else "")
        + (f" - they also want (not in this tier): {', '.join(t['wanted_but_not_offered'])}" if t["wanted_but_not_offered"] else "")
        for t in q["tiers"])
    first_name = L["name"].split(" ")[0]
    meeting = (q["meeting_note"] + "\n\n") if q.get("meeting") else ""
    body = f"""NEW PROSPECT - text them now. Quiz submitted {when}.

CONTACT
- {couple}
- Phone: {L.get('phone') or '-'}
- Email: {L.get('email') or '-'}
- Wedding: {L.get('date') or 'no date given'}, {a.get('guests') or '?'} guests
- Venue: {L.get('venue') or 'not given - ask (needed for travel and the $2,000 vs $1,000 retainer)'}
- Found us: {a.get('source') or '-'}

WHAT THEY TOLD US
- Vision (their words): "{(a.get('vision') or '-').strip()}"
- #1 value: {rank[0] if rank else '-'} (mirror this back). Then: {lst(rank[1:3])}
- Style: {a.get('style') or '-'}
- Vows: {a.get('vows') or '-'}
- Ceremony must-haves: {lst(a.get('ceremony_shots'))}
- Additions: {lst(a.get('additions'))}
- Film: {a.get('film') or '-'}
- Coverage: {L.get('hours')} hours
- Looks: {lst(a.get('enhancements'))}
- Music: {lst(a.get('music'))}
- Getting ready: {lst(a.get('getting_ready'))}
- Pinterest: {a.get('pinterest') or '-'}
- Investment: {L.get('budget') or '-'}

PRICING (from the pricing sheet; date multiplier x{q['multipliers']['total']}, travel ${q['logistics']['total']})
{tiers}
{('CHECK FIRST: ' + '; '.join(q['flags'])) if q['flags'] else 'Nothing flagged.'}
Date check: {q.get('availability') or 'not checked'}

CRM
{q.get('draft_note') or q.get('send_note') or DRAFT_NOTE_YES}

{meeting}FIRST TEXT
"Hi {first_name}! I just saw your wedding form come through on my phone. Real quick, could you just confirm your wedding date for me?"
"""
    subject = f"New prospect: {couple} - wedding {L.get('date') or 'date TBD'} - text them now"
    return subject, body


POISON_AFTER = 3   # runs (30 min) a response may fail before it is ticketed for a human and skipped


class QueueBusy(RuntimeError):
    """The ticket queue lock could not be had: not the lead's fault, retry next run."""


DRAFT_NOTE_YES = ('A priced DRAFT of "Videography Collection Details" is being placed in their project (it is not\n'
                  'sent). Open the project, read the four collections, then publish and send it yourself.')
DRAFT_NOTE_AUTO = ('A priced "Videography Collection Details" is being placed in their project and will be SENT to them\n'
                   'automatically (Publish & share, Custom Package email - quiz version): weekend date, open in the CRM.\n'
                   "owner1's standing rule of 2026-10-06. You get a 'Done' message when it has gone.")


def handle(item: dict, cfg: dict, opts: dict, dry: bool, progress: dict | None = None) -> dict:
    """progress: what an earlier, interrupted attempt at this lead already minted. A retry
    must not mint a second CRM draft or a second alert email."""
    progress = progress if progress is not None else {}
    import engine
    lead = engine.parse_response(item)
    tok = item.get("token", "")
    q = engine.quote(lead, cfg)
    q["availability"], conflict = availability(q["lead"].get("date"))
    avail_conflict = conflict
    if conflict:
        q["flags"].append(conflict)
        q["status"] = "needs_review"
    q["answers"] = lead.answers
    q["lead"].update({"phone": lead.phone, "submitted_at": item.get("submitted_at", "")})
    try:                                  # booked a call in the CRM already? (lead_meetings.py, 2026-10-08)
        import lead_meetings as lm
        snap = load_json(lm.SNAP, None)
        if lm.usable(snap) is not None:
            lm.apply(q, lm.usable(snap), snap.get("projects") or [], post=(lambda c: "") if dry else lm._post)
    except Exception:  # noqa: BLE001   a meeting lookup must never cost the lead
        pass
    when = pacific(item.get("submitted_at", ""))
    vision = str(lead.answers.get("vision") or "").lower()
    solicit = any(m in vision for m in SOLICIT_MARKS)
    # the brief must say what will really happen in the CRM, not always promise a draft
    if solicit:
        q["draft_note"] = "NO draft was placed: the answers read like a solicitation. If this is a real couple, price it by hand."
    elif not opts["create_drafts"]:
        q["draft_note"] = "NO draft was placed (drafts are switched off in assistant/config.json -> leads)."
    elif not lead.email:
        q["draft_note"] = "NO draft was placed: the quiz has no email for them. Create the project by hand."
    import collection_autosend as cas
    auto = cas.decide(q["lead"].get("date"), q["availability"], avail_conflict, bool(lead.email), solicit, opts)
    q["auto_send"] = auto
    q["send_note"] = DRAFT_NOTE_AUTO if auto["ok"] else DRAFT_NOTE_YES + f"\nNOT auto-sent: {auto['reason']}."
    out = {"token": tok, "name": lead.name, "solicitation": solicit, "status": q["status"], "exec": None, "email": None}
    if dry:
        out["subject"], out["body"] = brief(q, when)
        return out

    import exec_store as es
    import followups
    import tickets
    # the owner's standing rule (2026-10-17): a lead with a phone number is texted the minute the quiz
    # lands ("I just saw your wedding form come through..."), from his own iPhone, no card. The
    # words live in assistant/data/client_texts.json; client_texts dedupes on the number.
    if "text" in progress:
        out["text"] = progress["text"]
    else:
        out["text"] = None
        if lead.phone and not solicit:
            try:
                import client_texts
                # 2026-10-20: the intro is an URGENT send card (the owner's tap), carrying the lead brief as
                # its details so the "Hot lead - what next?" card is not needed on top of it
                r = client_texts.lead_intro({"name": lead.name, "partner": lead.partner, "phone": lead.phone, "email": lead.email}, tok,
                                            details=_lead_brief(lead, q, when, auto))
                out["text"] = ((r.get("ticket") or (f"CARD {r['card']}" if r.get("card") else f"AT {r.get('scheduled_for')}")) if r.get("ok")
                               else (f"CARD {r['card']}" if r.get("dup") and r.get("card") else f"HELD {r.get('reason')}"))   # AT: waits for 09:00
                out["text_body"] = r.get("body") or ""
            except Exception as e:  # noqa: BLE001   a texting fault must never cost the lead
                out["text"] = f"HELD {type(e).__name__}: {str(e)[:140]}"
        progress["text"] = out["text"]
    texted = str(out["text"] or "").startswith(("tk-", "AT "))
    carded = str(out["text"] or "").startswith("CARD ")
    q["intro_text"] = out["text"]
    if texted:
        q["send_note"] = (q.get("send_note") or "") + "\nTEXTED them the intro from owner1's phone the minute this came in (standing rule 2026-10-17)."
    elif carded:
        q["send_note"] = (q.get("send_note") or "") + "\nThe intro text is on owner1's phone as an urgent send card (2026-10-20): his tap sends it."
    # the draft ticket is decided BEFORE the brief is written, so the brief (emailed to a team member and
    # kept on the ticket) can never promise a draft that was refused
    if "exec" in progress:
        out["exec"] = progress["exec"]
    elif opts["create_drafts"] and lead.email and not solicit:
        draft_kind = crm._hook("PROPOSAL_DRAFT_KIND") or ""
        try:
            if not draft_kind:
                raise ValueError("no CRM connector provides PROPOSAL_DRAFT_KIND")
            out["exec"] = es.enqueue(draft_kind, {"response_token": tok, "quote": {k: v for k, v in q.items() if k not in ("draft_note", "send_note", "auto_send", "meeting", "meeting_note", "meeting_card", "intro_text")},
                                                  "auto_send": auto},
                                     es.APPROVER, STANDING_REF)["id"]
        except (ValueError, PermissionError) as e:
            out["exec"] = f"REFUSED {e}"
        progress["exec"] = out["exec"]
    if str(out["exec"] or "").startswith("REFUSED"):
        q["draft_note"] = f"NO draft was placed: the draft was refused ({str(out['exec'])[8:160]}). Create it by hand."
    subject, body = brief(q, when)
    LEADS.mkdir(parents=True, exist_ok=True)
    save_json(LEADS / f"{tok}.json", q)
    # Standing internal alert (the owner 2026-10-01: "Yes, send automatically"). Only ever to these inboxes.
    if "email" in progress:
        out["email"] = progress["email"]
    elif opts["auto_email"] and not solicit and {str(x).lower() for x in opts["alert_to"]} <= INTERNAL_ONLY:
        try:
            out["email"] = es.enqueue("email_send", {"from": opts["alert_from"], "to": opts["alert_to"],
                                                     "subject": subject, "body": body}, es.APPROVER,
                                      "standing:lead-alert-internal (owner1 2026-10-01: send the brief to staff1 + owner1 automatically)")["id"]
        except (ValueError, PermissionError) as e:
            out["email"] = f"REFUSED {e}"
        progress["email"] = out["email"]
    title = ("Possible solicitation via quiz: " if solicit else "New quiz lead: ") + f"{lead.name} ({when})"
    queued = tickets.add_many([{"source": "typeform", "source_id": tok, "lane": "wedding", "kind": "inquiry_new",
                       "title": title,
                       "summary": f"{lead.name} & {lead.partner}, wedding {q['lead'].get('date')}, budget {lead.budget_label}. "
                                  f"Recommended {q['recommended']}. Flags: {'; '.join(q['flags']) or 'none'}.",
                       "payload": {"brief_subject": subject, "brief_body": body, "alert_to": opts["alert_to"],
                                   "alert_emailed": str(out["email"] or "").startswith("tk-"), "exec_ticket": out["exec"],
                                   "lead_file": f"assistant/data/leads/{tok}.json", "solicitation": solicit,
                                   "instruction": "If alert_emailed is false, card the brief to alert_to as an email_send "
                                                  "exec card (internal hand-off, from owner3@example.invalid). Never write "
                                                  "to the couple without a card."}}], "sense.typeform")
    if queued < 0:
        raise QueueBusy("the inquiry ticket was not written")   # main() retries this lead next run
    if not solicit:
        if not texted and not carded:      # the intro text IS the first reply; the 2 h SLA is for leads nobody could text
            try:
                followups.arm_chain("inquiry", {"unit": tok, "couple": lead.name, "anchor": item.get("submitted_at", now_iso())},
                                    "sense.typeform", steps=["reply-sla"], grace_days=0)
            except Exception:
                pass
        if not carded:
            # the urgent "New lead" send card already carries the brief and the one decision that matters
            # now (the owner 2026-10-20: fewer, singular cards); the what-next card is for leads with no number
            hot_lead_card(lead, q, when, subject, body, out)
    return out


def _lead_brief(lead, q, when, auto) -> str:
    """The lead facts for the urgent send card's details (what the what-next card used to say)."""
    try:
        L = q.get("lead") or {}
        draft = (f"No {crm.name()} draft (see the brief)." if q.get("draft_note")
                 else f"The priced Collection Details goes into {crm.name()} and is SENT to them automatically (weekend date, open)."
                 if (auto or {}).get("ok")
                 else f"A priced DRAFT proposal goes into {crm.name()}, not sent: " + str((auto or {}).get("reason") or "held") + ".")
        tiers = ", ".join(f"{t['name']} ${t['price']:,}" for t in (q.get("tiers") or []) if t.get("price"))
        return (f"Quiz lead submitted {when}. Wedding {L.get('date') or '?'}"
                f"{(' at ' + str(L.get('venue'))) if L.get('venue') else ''}, budget {getattr(lead, 'budget_label', '?')}, "
                f"{L.get('email') or 'no email'}. Recommended: {str(q.get('recommended') or '?').capitalize()}. {draft}\n"
                f"Pricing: {tiers or 'n/a'}. Wants: {', '.join(L.get('wants') or []) or 'n/a'}. Flags: {'; '.join(q.get('flags') or []) or 'none'}."
                + (("\n" + str(q.get("meeting_note"))) if q.get("meeting") else ""))
    except Exception:  # noqa: BLE001
        return ""


def hot_lead_card(lead, q, when, subject, body, out) -> str:
    """The lead reaches the owners' phones within a minute, as a DECISION card with buttons
    (Google Chat + Mission Control), asking what to do. A tap becomes a `card_answer` ticket
    the operator carries out (a drafted text or email comes back as an exec card to approve).
    Zero-LLM. Added 2026-10-05 after an $8,000 lead sat unseen on Mission Control."""
    qid = ""
    try:
        import cards
        draft = (f"No {crm.name()} draft (see the brief)." if q.get("draft_note")
                 else f"The priced Collection Details is going into {crm.name()} and will be SENT to them automatically (weekend date, open)."
                 if (q.get("auto_send") or {}).get("ok")
                 else f"A priced DRAFT proposal is going into {crm.name()} (not sent: "
                      + str((q.get("auto_send") or {}).get("reason") or "held") + ").")
        tiers = ", ".join(f"{t['name']} ${t['price']:,}" for t in (q.get("tiers") or []) if t.get("price"))
        texted = str(out.get("text") or "").startswith(("tk-", "AT "))
        textline = (f"I texted them from your phone right away: \"{str(out.get('text_body') or '').strip()}\". " if str(out.get("text") or "").startswith("tk-")
                    else f"The intro text goes from your phone at 9:00 AM (no texts after 9 PM): \"{str(out.get('text_body') or '').strip()}\". " if texted
                    else (f"No intro text went out ({str(out.get('text'))[5:120]}). " if str(out.get("text") or "").startswith("HELD") else ""))
        options = ([
                {"label": "Nothing more for now (Recommended)", "description": "the intro text is out; their reply comes back to you as a card"},
                {"label": "Draft an email reply", "description": "Sage drafts the reply from owner3@example.invalid; you approve it"},
                {"label": "Not a real lead", "description": "Drop it: no follow-ups"},
            ] if texted else [
                {"label": "Draft a text to them (Recommended)", "description": "Sage drafts the first text; you approve it before it goes"},
                {"label": "Draft an email reply", "description": "Sage drafts the reply from owner3@example.invalid; you approve it"},
                {"label": "I'll reach out myself", "description": "No draft; the 2 h reply reminder stays on"},
                {"label": "Not a real lead", "description": "Drop it: no follow-ups"},
            ])
        r = cards.post([{
            "question": (f"New quiz lead: {lead.name} & {lead.partner}, wedding {q['lead'].get('date') or '?'}"
                         f"{(' at ' + str(q['lead'].get('venue'))) if q['lead'].get('venue') else ''}, budget {lead.budget_label}, "
                         f"submitted {when}. Phone {lead.phone or '?'}, {q['lead'].get('email') or '?'}. "
                         f"Recommended: {str(q.get('recommended') or '?').capitalize()}. {textline}{draft} "
                         f"{(q['meeting_note'] + ' ') if q.get('meeting') else ''}What should I do?"),
            "header": f"Hot lead: {lead.name}{(' & ' + lead.partner) if lead.partner else ''} - what next?", "lane": "wedding", "audience": "client",   # one subject per lead: a second lead the same day is its own card
            "details": (f"Pricing: {tiers or 'n/a'}. Wants: {', '.join(q['lead'].get('wants') or []) or 'n/a'}. "
                        f"Flags: {'; '.join(q.get('flags') or []) or 'none'}.\n\n{body}")[:3000],
            "options": options}], "sense.typeform", lane="wedding")
        qid = str(r.get("qid") or "") if r.get("posted") else ""
        if not qid:
            raise RuntimeError(r.get("reason") or "card not posted")
    except Exception as e:  # noqa: BLE001   the plain note is the fallback, never a lost lead
        try:
            with open(DATA / "cards.jsonl", "a", encoding="utf-8") as f:
                f.write(json.dumps({"ts": now_iso(), "text": f"NEW QUIZ LEAD - {lead.name} & {lead.partner} | {lead.phone} | "
                                    f"wedding {q['lead'].get('date')} | budget {lead.budget_label} | text them now. (card failed: {str(e)[:120]})"},
                                   ensure_ascii=False) + "\n")
        except OSError:
            pass
    out["card"] = qid
    return qid


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true", help="price + print, write nothing")
    ap.add_argument("--replay", default="", help="treat this response token as new (testing)")
    a = ap.parse_args()
    opts = {**DEFAULTS, **(config().get("leads") or {})}
    status = {"ts": now_iso(), "ok": False, "new": 0, "error": ""}
    try:
        token = _token()
        if not token:
            raise RuntimeError("TYPEFORM_TOKEN is not set (User environment variable)")
        items = fetch(token)
        seen = load_json(SEEN, None)
        if seen is None and load_json(STATUS, {}).get("last_ok"):
            # the sensor has worked before, so a missing / unreadable seen-file is damage, not a
            # first run: re-seeding now would mark every unprocessed lead as seen, silently
            raise RuntimeError("typeform_seen.json is missing or unreadable after earlier good runs - not re-seeding. "
                               "Restore it (VAULT_RESCUE) or delete typeform_status.json to seed again on purpose")
        if seen is None:                                  # first run: remember, do not backfill
            seen = {"seeded": now_iso(), "tokens": [i["token"] for i in items]}
            if not a.dry_run:
                save_json(SEEN, seen)
            print(f"SENSE_RESULT typeform seeded={len(seen['tokens'])} new=0")
            status["ok"] = True
        else:
            known = set(seen["tokens"]) - ({a.replay} if a.replay else set())
            fresh = [i for i in reversed(items) if i["token"] not in known]
            if fresh:
                import engine
                cfg = engine.load_config()
            results, failed = [], []
            fails = seen.setdefault("fails", {})
            for it in fresh:
                tok = it["token"]
                prog = seen.setdefault("progress", {}).setdefault(tok, {})
                try:
                    results.append(handle(it, cfg, opts, a.dry_run, prog))
                except QueueBusy as e:                    # not the lead's fault: retry, never counts toward poison
                    failed.append(f"{tok[:8]} QUEUE_BUSY")
                    if not a.dry_run:
                        save_json(SEEN, seen)             # remembers what was already minted
                    continue
                except Exception as e:                    # one odd response must not block the leads behind it
                    err = f"{type(e).__name__}: {e}"[:200]
                    failed.append(f"{tok[:8]} {err}")
                    if a.dry_run:
                        continue
                    fails[tok] = int(fails.get(tok, 0)) + 1
                    if fails[tok] >= POISON_AFTER:        # give up on it, loudly: a person must look at this lead
                        import tickets
                        if tickets.add_many([{"source": "typeform", "source_id": f"poison|{tok}", "lane": "wedding", "kind": "inquiry_new",
                                              "title": "Quiz lead could NOT be processed - open it in Typeform",
                                              "summary": f"Response {tok} failed {fails[tok]} times ({err}). It may not have been priced "
                                                         f"(minted so far: {prog or 'nothing'}). Post a card telling owner1 to open the "
                                                         "response in Typeform and reply to the couple by hand.",
                                              "payload": {"response_token": tok, "error": err, "submitted_at": it.get("submitted_at", "")}}],
                                            "sense.typeform") >= 0:
                            seen["tokens"].append(tok)
                            fails.pop(tok, None)
                            seen["progress"].pop(tok, None)
                    save_json(SEEN, seen)
                    continue
                if not a.dry_run:
                    seen["tokens"].append(tok)
                    fails.pop(tok, None)
                    seen["progress"].pop(tok, None)
                    save_json(SEEN, seen)                 # per lead: a crash never re-fires earlier ones
            status.update(ok=not failed, new=len(results))
            if failed:
                status["error"] = ("lead(s) failed: " + " | ".join(failed))[:300]
            print(f"SENSE_RESULT typeform new={len(results)} failed={len(failed)} " + json.dumps(
                [{k: r[k] for k in ("name", "status", "exec", "email", "solicitation")} for r in results]))
            if a.dry_run:
                for r in results:
                    print("\n" + r["subject"] + "\n" + r["body"])
    except Exception as e:                                # a sensor must never take the spine down
        status["error"] = f"{type(e).__name__}: {e}"[:300]
        print("SENSE_RESULT typeform ERROR " + status["error"])
    if not a.dry_run:
        prev = load_json(STATUS, {})
        status["last_ok"] = status["ts"] if status["ok"] else prev.get("last_ok", "")
        save_json(STATUS, status)
    return 0


if __name__ == "__main__":
    sys.exit(main())

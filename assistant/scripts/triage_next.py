"""triage_next.py - every client input leads to its next action (the property OS's triage lanes, ported).

the owner, 2026-10-18: "Take a lesson from the triage lanes in property management and learn how every input
leads to the next logical action as opposed to our system that received example's contact info and did
nothing with it when it could have added her to the HoneyBook project file."

The property side's rule (triage-and-propose.md): a reply is ONE possible action, never the default; every
inbound message is classified into FACTS, each fact maps to ACTIONS, the actions are ranked and the reply is
one of them. This is that engine for the personal OS, zero-LLM:

  input   a new ticket of an inbound kind (a text, an email, a HoneyBook inquiry) the moment tickets.add_many
          stores it (the hook), or a replay from the CLI
  party   who it is from and which couple / lead it belongs to (ledger phones, lead files, HoneyBook contacts)
  facts   what the message carries, from the vocabulary in data/sop_map.json -> inputs.facts:
            contact            an email / phone (not the sender's) and the name it belongs to
            copy_on_everything "copy X on everything", "cc her on all the emails"
            questionnaire_done "we filled out the questionnaire"
            lost               "we went another direction", "decided on another videographer"
            date_change        "we moved the wedding to ..."     (operator: hb_project_update card)
            venue_change       "new venue is ..."                (operator: hb_project_update card)
            reply              always, unless the text is a pure ack
  actions what each fact leads to, from data/sop_map.json -> inputs.actions, three gates:
            auto      code does it now and records it: also_notify on the couple's ledger entry, the contact
                      on the couple's wedding page (Story and notes), closing a questionnaire reminder, stopping
                      a lost lead's follow-ups, an internal HoneyBook note (the owner's standing rule: notes need no card)
            card      code posts the exec card now (hb_add_participant, hb_set_stage lost): the owner's tap runs it
            operator  left for the drafter, named: the reply, a date / venue update
  record  the ticket's payload gets `triage` {party, facts, actions[{do, gate, status, detail}]}: the operator
          reads what code already did and does only what is left; Sage's chat line lists all of it.

Vocabulary and the fact->action map live in sop_map.json (data), so another business changes words and
targets there, never here. Nothing in this file sends anything: cards carry the outward steps.

  python assistant/scripts/triage_next.py --dry --from "+1555-031-6831" --text "Here is example's number: (555) 018-0318"
  python assistant/scripts/triage_next.py replay <ticket id> [--dry]
  python assistant/scripts/triage_next.py drain              # the spine: file queued couple-page notes (wiki writes are slow)
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from alib import DATA, VAULT, load_json, save_json  # noqa: E402

SOP_MAP = DATA / "sop_map.json"
PROJECTS = Path(os.environ.get("WV_PROJECT_STATE") or (VAULT / "scripts" / "projects" / "project_state.json"))
LEADS = DATA / "leads"
SNAP = Path(os.environ.get("WV_HB_SNAPSHOT") or (VAULT / "dashboard" / "data" / "honeybook.json"))
QUEUE = DATA / "triage_queue.json"              # couple-page notes waiting for the spine (wiki writes lint + commit: slow)
INPUT_KINDS = {"imessage_in", "email", "inquiry_new", "hb_message_in"}
_PHONE = re.compile(r"\+?1?[\s(.-]*\d{3}[\s).-]*\d{3}[\s.-]*\d{4}")
_EMAIL = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")


def _digits(v) -> str:
    d = re.sub(r"\D", "", str(v or ""))
    return d[-10:] if len(d) >= 10 else ""


def _toks(s) -> list:
    return [t for t in re.findall(r"[a-z]{2,}", str(s or "").lower()) if t not in ("and", "the", "project", "wedding", "partner")]


def vocab() -> dict:
    return ((load_json(SOP_MAP, {}) or {}).get("inputs") or {})


# ------------------------------------------------------------------ who is it from
def party_for(handle: str = "", email: str = "", name: str = "") -> dict:
    """{"kind": booked|lead|contact|unknown, "unit", "couple", "project_id", "workspace_id", "lead", "name"}.
    Sources in order: the project ledger (phones / email), the quiz lead files, the HoneyBook contacts."""
    key, em = _digits(handle), str(email or "").strip().lower()
    out = {"kind": "unknown", "unit": "", "couple": "", "project_id": "", "workspace_id": "", "lead": "", "name": str(name or "")}
    ps = (load_json(PROJECTS, {}) or {}).get("projects") or {}
    snap = load_json(SNAP, {}) or {}
    ws_for = {str(p.get("project_id") or ""): str(p.get("id") or "") for p in (snap.get("projects") or []) if isinstance(p, dict)}
    for unit, p in ps.items():
        if not isinstance(p, dict) or p.get("stage") in ("cancelled", "archived", "lapsed"):
            continue
        phones = {_digits(x) for x in ([p.get("phone")] + list(p.get("phones") or []) + list(p.get("also_phones") or []))} - {""}
        emails = {str(x).lower() for x in ([p.get("email")] + list(p.get("emails") or [])) if x}
        if (key and key in phones) or (em and em in emails):
            pid = str(p.get("honeybook_id") or "")
            out.update(kind="booked" if p.get("stage") not in ("inquiry", "lead") else "lead", unit=unit, couple=str(p.get("couple") or unit),
                       project_id=pid, workspace_id=ws_for.get(pid, ""))
            return out
    try:
        for f in sorted(LEADS.glob("*.json")):
            q = load_json(f, None)
            L = (q or {}).get("lead") if isinstance(q, dict) else None
            if not isinstance(L, dict):
                continue
            if (key and _digits(L.get("phone")) == key) or (em and str(L.get("email") or "").lower() == em):
                out.update(kind="lead", lead=f.stem, couple=" & ".join(x for x in (L.get("name"), L.get("partner")) if x) or str(L.get("name") or ""))
                # the lead's HoneyBook project, when the snapshot has one by name
                toks = _toks(L.get("name"))
                for p in snap.get("projects") or []:
                    if toks and toks[0] in _toks(p.get("name")):
                        out.update(project_id=str(p.get("project_id") or ""), workspace_id=str(p.get("id") or ""))
                        break
                return out
    except OSError:
        pass
    for c in snap.get("contacts") or []:
        if not isinstance(c, dict):
            continue
        if (key and _digits(c.get("phone")) == key) or (em and str(c.get("email") or "").lower() == em):
            out.update(kind="contact", couple=str(c.get("name") or ""), name=str(c.get("name") or name or ""))
            wids = [str(x) for x in (c.get("workspace_ids") or [])]
            if wids:
                out["workspace_id"] = wids[0]
            else:                                   # the one project whose name shares the contact's first name
                toks = _toks(c.get("name"))
                hits = [p for p in (snap.get("projects") or []) if toks and toks[0] in _toks(p.get("name"))]
                if len(hits) == 1:
                    out.update(project_id=str(hits[0].get("project_id") or ""), workspace_id=str(hits[0].get("id") or ""))
            # a booked ledger entry whose HoneyBook project matches makes it a booked couple
            for unit, p in ps.items():
                if isinstance(p, dict) and out["project_id"] and str(p.get("honeybook_id") or "") == out["project_id"]:
                    out.update(kind="booked", unit=unit, couple=str(p.get("couple") or unit))
                    break
            return out
    return out


# ------------------------------------------------------------------ what the message carries
def _rx(words: list, flags=re.I):
    return re.compile("|".join(f"(?:{w})" for w in words), flags) if words else None


def facts_in(text: str, sender_handle: str = "", sender_name: str = "", prior: list | None = None, v: dict | None = None) -> list:
    """The facts one message carries. `prior` = the other recent inbound texts of the same thread (a number in one
    text and the email in the next belong to the same person)."""
    v = v if v is not None else vocab()
    fv = v.get("facts") or {}
    text = str(text or "")
    blob = " \n".join([text] + [str(x) for x in (prior or [])])
    out = []
    sender_key = _digits(sender_handle)
    # contact: an email or a phone that is not the sender's own
    emails = [e for e in _EMAIL.findall(text) if e.lower() != str(sender_handle or "").lower()]
    phones = [m.group(0).strip() for m in _PHONE.finditer(text) if _digits(m.group(0)) and _digits(m.group(0)) != sender_key]
    if emails or phones:
        name = ""
        m = re.search(r"\b([A-Z][a-z]+)(?:'s|’s)\s+(?:number|phone|cell|email|e-mail|contact|info)", blob)
        if m:
            name = m.group(1)
        else:
            m = re.search(r"\b(?:here(?:'s| is)|this is)\s+([A-Z][a-z]+)\b", blob)
            name = m.group(1) if m and m.group(1).lower() not in ("my", "her", "his", "the") else ""
        # the same person's email in the previous text, or number in this one, folds into one fact
        pe = [e for x in (prior or []) for e in _EMAIL.findall(str(x))]
        pp = [m.group(0).strip() for x in (prior or []) for m in _PHONE.finditer(str(x)) if _digits(m.group(0)) not in (sender_key, "")]
        out.append({"type": "contact", "name": name, "email": (emails or pe or [""])[0].lower(), "phone": (phones or pp or [""])[0],
                    "source": f"text from {sender_name or sender_handle}"})
    for ftype in ("copy_on_everything", "questionnaire_done", "lost", "date_change", "venue_change", "file_received", "yes_to_proposal"):
        rx = _rx((fv.get(ftype) or {}).get("any") or [])
        if rx and rx.search(blob):
            f = {"type": ftype, "quote": text.strip()[:200]}
            if ftype == "copy_on_everything":
                m = re.search(r"\b(?:copy|cc|include|add)\s+([A-Z][a-z]+)\b", blob)
                if m:
                    f["name"] = m.group(1)
                em = _EMAIL.findall(blob)
                if em:
                    f["email"] = em[0].lower()
            out.append(f)
    try:
        sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "agent"))
        import ack
        pure_ack = ack.is_pure_ack(text)
    except Exception:  # noqa: BLE001
        pure_ack = False
    if not pure_ack:
        out.append({"type": "reply"})
    return out


# ------------------------------------------------------------------ what each fact leads to
def plan(facts: list, party: dict, v: dict | None = None) -> list:
    """[{do, gate, detail, ...}] in execution order: the record-keeping and HoneyBook steps first, the reply
    last (the property rule: a promise is sent only after its backing action exists)."""
    v = v if v is not None else vocab()
    amap = v.get("actions") or {}
    out, seen = [], set()
    for f in facts:
        for a in amap.get(f["type"]) or []:
            a = dict(a)
            need = a.pop("needs", "")
            if need == "workspace" and not party.get("workspace_id"):
                a.update(gate="operator", detail=f"{a.get('detail') or a['do']}: no HoneyBook workspace found for this sender; find the project first")
            elif need == "unit" and not party.get("unit"):
                if a["do"] in ("also_notify", "couple_page", "followups_close"):
                    continue                       # nothing to write to for a stranger
            if a["do"] in ("hb_add_participant", "also_notify", "couple_page"):
                if f["type"] == "contact" and not (f.get("email") or f.get("phone")):
                    continue
                if f["type"] == "copy_on_everything" and not f.get("email"):
                    # the email came in another fact (the contact one) or is unknown: the contact fact carries the add
                    if not any(x["type"] == "contact" and x.get("email") for x in facts):
                        a.update(gate="operator", detail="copy them on everything: no email on file yet; ask for it")
                    else:
                        continue
            k = (a["do"], f.get("email") or f.get("name") or f["type"])
            if k in seen:
                continue
            seen.add(k)
            a["fact"] = f["type"]
            a["who"] = {k2: f.get(k2) for k2 in ("name", "email", "phone") if f.get(k2)}
            out.append(a)
    # WP6: a kind the owner graduated (graduation.py, their tap on "Make it automatic?") runs as a standing action
    try:
        import graduation
        for a in out:
            if a.get("gate") == "card" and graduation.granted(str(a.get("do") or "")):
                a["gate"], a["graduated"] = "auto", True
    except Exception:  # noqa: BLE001
        pass
    order = {"auto": 0, "card": 1, "operator": 2}
    out.sort(key=lambda a: (a["do"] == "reply_draft", order.get(a.get("gate"), 3)))
    return out


# ------------------------------------------------------------------ doing the auto steps, posting the cards
def _ledger():
    return load_json(PROJECTS, {}) or {"projects": {}}


def _also_notify(party: dict, who: dict, dry: bool) -> str:
    led = _ledger()
    p = (led.get("projects") or {}).get(party.get("unit"))
    if not isinstance(p, dict):
        return "no ledger entry"
    em = str(who.get("email") or "").lower()
    if not em:
        return "no email to add"
    lst = [str(x).lower() for x in (p.get("also_notify") or [])]
    if em in lst:
        return f"already copies {em}"
    if not dry:
        p["also_notify"] = lst + [em]
        if who.get("phone"):
            p["also_phones"] = sorted(set((p.get("also_phones") or []) + [who["phone"]]))
        p["note"] = (str(p.get("note") or "") + f" | also_notify {em} ({who.get('name') or 'partner'}) from their text {time.strftime('%Y-%m-%d')}").strip(" |")
        save_json(PROJECTS, led)
    return f"every email-type send to {party.get('couple') or party.get('unit')} now copies {em}"


def _followups_close(party: dict, steps_prefix: str, why: str, dry: bool) -> str:
    if not party.get("unit"):
        return "no unit"
    try:
        import followups
    except Exception as e:  # noqa: BLE001
        return f"followups unavailable: {type(e).__name__}"
    n = 0
    with followups.StateLock():
        st = followups.load_state()
        for it in st["items"].values():
            if it.get("unit") == party["unit"] and str(it.get("step") or "").startswith(steps_prefix) and it.get("status") in ("waiting", "fired"):
                n += 1
                if not dry:
                    it["status"] = "done"
                    followups.log("mark", it, {"note": f"triage_next: {why}"})
        if n and not dry:
            followups.save_state(st)
    return f"closed {n} reminder{'s' if n != 1 else ''} ({steps_prefix}*)" if n else f"no open {steps_prefix}* reminder"


def _followups_stop(party: dict, why: str, dry: bool) -> str:
    unit = party.get("unit") or party.get("lead") or ""
    if not unit:
        return "no unit"
    try:
        import followups
    except Exception as e:  # noqa: BLE001
        return f"followups unavailable: {type(e).__name__}"
    if dry:
        st = followups.load_state()
        n = sum(1 for it in st["items"].values() if it.get("unit") == unit and it.get("status") in ("waiting", "fired"))
        return f"would stop {n} follow-up(s) for {unit}"
    total = 0
    for sop in {it.get("sop") for it in followups.load_state()["items"].values() if it.get("unit") == unit}:
        total += followups.cancel_unit(sop, unit, why)
    return f"stopped {total} follow-up(s) for {unit}"


def _queue_couple_page(party: dict, body: str, dry: bool) -> str:
    if not (party.get("unit") or party.get("couple")):
        return "no couple"
    if dry:
        return f"would file on {party.get('couple') or party.get('unit')}'s page: {body[:80]}"
    q = load_json(QUEUE, {}) or {}
    rows = q.setdefault("couple_notes", [])
    rows.append({"ts": time.strftime("%Y-%m-%dT%H:%M:%S"), "couple": party.get("unit") or party.get("couple"), "body": body})
    save_json(QUEUE, q)
    return f"queued for {party.get('couple') or party.get('unit')}'s wedding page (filed within 10 min)"


def drain_queue(dry: bool = False) -> dict:
    """The spine: file queued couple-page notes through wiki_writer.couple_notes (lint + commit: slow, so never
    inline in a sensor)."""
    q = load_json(QUEUE, {}) or {}
    rows = list(q.get("couple_notes") or [])
    out = {"filed": 0, "refused": [], "left": 0}
    if not rows:
        return out
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "agent"))
    import wiki_writer
    keep = []
    for r in rows:
        if dry:
            keep.append(r)
            continue
        x = wiki_writer.couple_notes(str(r.get("couple") or ""), str(r.get("body") or ""), by="triage_next")
        if x.get("status") in ("applied", "appended"):
            out["filed"] += 1
        else:
            r["tries"] = int(r.get("tries") or 0) + 1
            r["last_reason"] = str(x.get("reason") or "")[:160]
            out["refused"].append({"couple": r.get("couple"), "reason": r["last_reason"]})
            if r["tries"] < 3:
                keep.append(r)
    out["left"] = len(keep)
    if not dry:
        q["couple_notes"] = keep
        save_json(QUEUE, q)
    return out


def _post_card(a: dict, party: dict, ticket: dict | None, dry: bool) -> str:
    """An exec card for a gated step. Returns the card id or the refusal."""
    who = a.get("who") or {}
    couple = party.get("couple") or party.get("unit") or "the couple"
    if a["do"] == "hb_add_participant":
        name = who.get("name") or who.get("email") or "them"
        q = {"header": f"{name} on {couple}'s project",
             "question": (f"{couple} sent {name}'s details: {who.get('email', '')}" + (f", {who['phone']}" if who.get("phone") else "")
                          + f". Add {name} ({who.get('email', '')}) to their HoneyBook project, so every message, file and reminder in it reaches {name} too? HoneyBook mails {name} its own project invite."),
             "options": [{"label": "Add them (Recommended)"}, {"label": "Skip"}], "lane": "wedding", "audience": "client",
             "advice": "💡 Add: one connector call with a read-back; nothing else is sent. This is what 'copy them on everything' means in HoneyBook.",
             "exec": {"kind": "hb_add_participant", "approve_label": "Add them (Recommended)",
                      "payload": {k: v for k, v in {"workspace_id": party.get("workspace_id"), "email": who.get("email"), "name": who.get("name"), "phone": who.get("phone")}.items() if v}}}
    elif a["do"] == "hb_set_stage":
        reason = f"{a.get('reason') or 'went another direction'}, said {time.strftime('%Y-%m-%d')}"
        q = {"header": f"{couple}: mark lost",
             "question": f"{couple} say they went another direction. Mark their HoneyBook project lost (archive it with the reason '{reason}') and stop their follow-ups?",
             "options": [{"label": "Mark lost (Recommended)"}, {"label": "Skip"}], "lane": "wedding", "audience": "internal",
             "advice": "💡 Reversible (HoneyBook can unarchive). Nothing goes to the couple.",
             "exec": {"kind": "hb_set_stage", "approve_label": "Mark lost (Recommended)",
                      "payload": {"workspace_id": party.get("workspace_id"), "stage": "lost", "reason": reason}}}
    else:
        return f"no card template for {a['do']}"
    if dry:
        return f"would post card: {q['header']}"
    try:
        import cards
        # one subject, one card: the same exec (kind + workspace + person/stage) already open means no second card
        want = (q["exec"]["kind"], q["exec"]["payload"].get("workspace_id"), q["exec"]["payload"].get("email") or q["exec"]["payload"].get("stage"))
        for c in cards._live():
            if c.get("status") != "open":
                continue
            for x in c.get("questions") or []:
                ex = x.get("exec") or {}
                pl = ex.get("payload") or {}
                if (ex.get("kind"), pl.get("workspace_id"), pl.get("email") or pl.get("stage")) == want:
                    return f"card {c['id']} (already up)"
        r = cards.post([q], "triage_next", lane="wedding", thread=str(((ticket or {}).get("payload") or {}).get("thread_id") or ""))
    except Exception as e:  # noqa: BLE001
        return f"card failed: {type(e).__name__}: {str(e)[:120]}"
    if r.get("posted"):
        return f"card {r['qid']}"
    return f"card not posted: {str(r.get('reason') or '')[:160]}"


def act(actions: list, party: dict, facts: list, ticket: dict | None = None, dry: bool = False) -> list:
    """Run the auto steps, post the cards, mark the rest for the operator. Returns the actions with status + detail."""
    out = []
    contact = next((f for f in facts if f["type"] == "contact"), {})
    for a in actions:
        a = dict(a)
        who = a.get("who") or {k: contact.get(k) for k in ("name", "email", "phone") if contact.get(k)}
        try:
            if a.get("gate") == "auto":
                if a["do"] == "also_notify":
                    a["detail"] = _also_notify(party, who, dry)
                elif a["do"] == "couple_page":
                    body = a.get("body") or ""
                    if a["fact"] == "contact":
                        body = (f"**{who.get('name') or 'Partner'}** (from {party.get('couple') + chr(39) + 's' if party.get('couple') else 'their'} text, {time.strftime('%Y-%m-%d')}): "
                                + ", ".join(x for x in (who.get("email"), who.get("phone")) if x) + ". Copied on every email-type send.")
                    elif a["fact"] == "questionnaire_done":
                        body = f"Questionnaire: they said it is done ({time.strftime('%Y-%m-%d')}, by text)."
                    elif a["fact"] == "lost":
                        body = f"Went another direction ({time.strftime('%Y-%m-%d')}): \"{(facts[0].get('quote') if facts else '')[:120]}\""
                    elif a["fact"] == "file_received":
                        body = f"Timeline / list received ({time.strftime('%Y-%m-%d')}, by message): \"{str(next((x.get('quote') for x in facts if x['type'] == 'file_received'), ''))[:120]}\""
                    a["detail"] = _queue_couple_page(party, body, dry)
                elif a["do"] == "followups_close":
                    a["detail"] = _followups_close(party, str(a.get("steps") or "questionnaire-reminder"), a.get("why") or "they said the questionnaire is done", dry)
                elif a["do"] == "followups_stop":
                    a["detail"] = _followups_stop(party, a.get("why") or "went another direction", dry)
                elif a.get("graduated") and a["do"] in ("hb_add_participant", "hb_set_stage"):
                    a["detail"] = _graduated_exec(a, party, who, dry)
                else:
                    a["detail"] = _plugin_auto(a, party, facts, dry) or f"no auto handler for {a['do']}"
                a["status"] = "done" if not dry else "dry"
            elif a.get("gate") == "card":
                a["who"] = who
                a["detail"] = _post_card(a, party, ticket, dry)
                a["status"] = "carded" if a["detail"].startswith("card q") or a["detail"].startswith("would post") else "card_failed"
            else:
                a["status"] = "for_operator"
                a.setdefault("detail", a["do"])
        except Exception as e:  # noqa: BLE001  one step failing never loses the ticket
            a["status"], a["detail"] = "error", f"{type(e).__name__}: {str(e)[:160]}"
        out.append(a)
    return out


def _graduated_exec(a: dict, party: dict, who: dict, dry: bool) -> str:
    """The same payload the card would have carried, minted under the owner's graduation (standing ref); the outcome
    ledger verifies it like any other and one failure demotes the kind (graduation.tick)."""
    if a["do"] == "hb_add_participant":
        payload = {k: v for k, v in {"workspace_id": party.get("workspace_id"), "email": who.get("email"), "name": who.get("name"), "phone": who.get("phone")}.items() if v}
    else:
        payload = {"workspace_id": party.get("workspace_id"), "stage": "lost", "reason": f"{a.get('reason') or 'went another direction'}, said {time.strftime('%Y-%m-%d')}"}
    if dry:
        return f"would queue {a['do']} (graduated)"
    try:
        import exec_store as es
        import graduation
        g = (graduation._state().get("granted") or {}).get(a["do"]) or {}
        row = es.enqueue(a["do"], payload, es.APPROVER, f"standing:graduation {a['do']} (owner tap {g.get('ts', '')}) via triage_next")
        return f"{a['do']} ticket {row['id']} queued (graduated)"
    except Exception as e:  # noqa: BLE001
        return f"{a['do']} refused: {str(e)[:120]}"


def _plugin_auto(a: dict, party: dict, facts: list, dry: bool) -> str:
    """An auto step one of the organization's extensions owns (config.json -> plugins.modules, hook `triage_auto`):
    the first that answers; '' when none does."""
    try:
        import alib
        return str(alib.plugin_call("triage_auto", a, party, facts, dry) or "")
    except Exception as e:  # noqa: BLE001
        return f"{a.get('do')} refused: {str(e)[:120]}"


# ------------------------------------------------------------------ the hook and the CLI
def triage(text: str, handle: str = "", name: str = "", email: str = "", prior: list | None = None, ticket: dict | None = None, dry: bool = False) -> dict:
    v = vocab()
    party = party_for(handle=handle, email=email, name=name)
    facts = facts_in(text, handle, name or party.get("couple", ""), prior, v)
    actions = act(plan(facts, party, v), party, facts, ticket, dry)
    return {"party": party, "facts": facts, "actions": actions, "line": summary_line(party, facts, actions)}


def summary_line(party: dict, facts: list, actions: list) -> str:
    """One plain line for the operator / Sage: what came in and what was done with it."""
    who = party.get("couple") or party.get("name") or "an unknown sender"
    done = [a for a in actions if a.get("status") in ("done", "carded", "dry")]
    left = [a for a in actions if a.get("status") == "for_operator"]
    kinds = ", ".join(sorted({f["type"].replace("_", " ") for f in facts if f["type"] != "reply"})) or "a message"
    parts = [f"{who}: {kinds}."]
    if done:
        parts.append("Done by code: " + "; ".join(f"{a['do']} ({a.get('detail', '')[:70]})" for a in done) + ".")
    if left:
        parts.append("Left for you: " + ", ".join(a["do"].replace("_", " ") for a in left) + ".")
    return " ".join(parts)


def on_new_tickets(items: list, dry: bool = False) -> int:
    """tickets.add_many's hook: for every newly stored inbound ticket, triage and record the result on the ticket.
    Never raises; a triage fault never loses a ticket."""
    n = 0
    for t in items:
        try:
            if t.get("kind") not in INPUT_KINDS:
                continue
            p = t.get("payload") or {}
            if t.get("kind") == "imessage_in":
                prior = [m.get("text") for m in (p.get("thread") or [])[-3:] if m.get("dir") == "in"]
                r = triage(str(p.get("text") or ""), handle=str(p.get("from") or ""), name=str(p.get("name") or ""), prior=prior, ticket=t, dry=dry)
            elif t.get("kind") == "email":
                r = triage(f"{p.get('subject') or ''}\n{t.get('summary') or ''}", email=_EMAIL.search(str(p.get("from") or "")).group(0) if _EMAIL.search(str(p.get("from") or "")) else "",
                           name=str(p.get("from") or ""), ticket=t, dry=dry)
            else:
                r = triage(str(t.get("summary") or ""), email=str(p.get("email") or ""), name=str(p.get("name") or ""), ticket=t, dry=dry)
            if not dry:
                import tickets
                tickets.annotate(t["id"], "triage", {"party": r["party"], "facts": r["facts"], "actions": r["actions"], "line": r["line"],
                                                      "ts": time.strftime("%Y-%m-%dT%H:%M:%S")})
            n += 1
        except Exception as e:  # noqa: BLE001
            try:
                import tickets
                tickets.annotate(t["id"], "triage", {"error": f"{type(e).__name__}: {str(e)[:160]}"})
            except Exception:  # noqa: BLE001
                pass
    return n


def main() -> int:
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd")
    ap.add_argument("--dry", action="store_true")
    ap.add_argument("--from", dest="frm", default="")
    ap.add_argument("--name", default="")
    ap.add_argument("--email", default="")
    ap.add_argument("--text", default="")
    ap.add_argument("--prior", action="append", default=[])
    rp = sub.add_parser("replay"); rp.add_argument("ticket"); rp.add_argument("--dry", action="store_true")
    sub.add_parser("drain")
    a = ap.parse_args()
    if a.cmd == "drain":
        print("TRIAGE_DRAIN " + json.dumps(drain_queue(dry=a.dry), ensure_ascii=False))
        return 0
    if a.cmd == "replay":
        import tickets
        t = tickets._load().get(a.ticket)
        if not t:
            print("NOT FOUND"); return 2
        p = t.get("payload") or {}
        r = triage(str(p.get("text") or t.get("summary") or ""), handle=str(p.get("from") or ""), name=str(p.get("name") or ""),
                   prior=[m.get("text") for m in (p.get("thread") or [])[-3:] if m.get("dir") == "in"], ticket=t, dry=a.dry or getattr(a, "dry", False))
    else:
        r = triage(a.text, handle=a.frm, name=a.name, email=a.email, prior=a.prior, dry=a.dry)
    print(json.dumps(r, indent=1, ensure_ascii=False, default=str))
    return 0


if __name__ == "__main__":
    sys.exit(main())

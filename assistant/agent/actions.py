"""actions.py - apply the model's SUGGEST line, and say truthfully what happened.

The model proposes; this code decides. Each field is validated, capped per message and
routed to its one writer (core rules/memory, wiki_writer, routines, the operator's ticket
queue). A success adds nothing under the model's reply (the owner 2026-10-07: no "Learned r6 (undo:
/undo c15)" lines, nothing robotic; the model says it in its own words and the ledger holds the
undo ids for /changes). Only what could NOT be done comes back as a line, so a problem is never
hidden behind the model's "got it".

Nothing here sends. `task` hands outward work to the operator, whose drafts come back as
cards an owner approves (assistant/README.md, "Approval chain").
"""
from __future__ import annotations

import re
import secrets
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import core  # noqa: E402
import routines  # noqa: E402
import skills  # noqa: E402
import wiki_writer  # noqa: E402

CAPS = {"learn": 3, "unlearn": 3, "remember": 5, "forget": 5, "unschedule": 3}


def _lanes() -> tuple:
    try:
        import intents
        return intents.lanes()
    except Exception:  # noqa: BLE001
        return ("wedding", "personal", "home", "money", "owner6")


class _Lanes(tuple):
    """`x in LANES` reads the organization's lanes (org.json) at call time, so a scaffolded org is not
    pinned to this one's lane names."""
    def __contains__(self, item):
        return item in _lanes()


LANES = _Lanes()
import threading


class _PerThread(threading.local):
    """What the last apply() ON THIS THREAD recorded: the bridge runs one conversation per worker thread
    (2026-10-21), so two turns must not share these."""
    def __init__(self):
        self.followups: list = []
        self.applied: list = []


_tls = _PerThread()


class _TLList(list):
    """`actions.LAST_APPLIED` / `LAST_FOLLOWUPS` read as before (tests, the bridge) but resolve to this
    thread's list."""
    def __init__(self, attr):
        super().__init__()
        self._attr = attr

    def _cur(self) -> list:
        return getattr(_tls, self._attr)

    def __iter__(self):
        return iter(self._cur())

    def __len__(self):
        return len(self._cur())

    def __getitem__(self, i):
        return self._cur()[i]

    def __bool__(self):
        return bool(self._cur())

    def append(self, x):
        self._cur().append(x)

    def clear(self):
        self._cur().clear()

    def __eq__(self, other):
        return list(self._cur()) == other


LAST_FOLLOWUPS = _TLList("followups")   # what the last apply() registered; the bridge hands it to keep_promises
LAST_APPLIED = _TLList("applied")       # work really handed off this turn [{"kind", "what", "ref"}]; the bridge registers it as a commitment
_BUILD_REQ = re.compile(r"(?im)^\s*(?:#+\s*)?build request\b")


def _list(v) -> list:
    return v if isinstance(v, list) else ([v] if v else [])


def revise_card(qid: str, new_text: str, person: dict, ctx: dict) -> list:
    """An owner's feedback in a card's thread -> the same card with the outgoing text replaced,
    posted in that thread; the old card retired as superseded. The owner's tap on the new card is
    still the approval: nothing here answers or sends (the owner 2026-10-08, example's thread flow)."""
    import copy
    sys.path.insert(0, str(core.AGENT.parent / "scripts"))
    import cards
    import chat_push
    card = cards._read(cards.LIVE / f"{qid}.json") if re.fullmatch(r"q[0-9a-f]{12}", qid) else None
    if card and card.get("status") == "answered":
        # approved a minute ago and the ticket has not run yet (drain is on a 10-min tick): the owner's revision
        # pulls it back (cards.undo recalls the ticket) and the revised card takes its place (Teo's "please ignore
        # the previous email", 2026-10-24 18:07, refused ALREADY_ANSWERED while the email was still queued)
        u = cards.undo(qid, by=f"agent:{person['key']} (revised in chat)")
        if not u.get("ok"):
            return [f"That one already went out, so I can't change it ({str(u.get('reason') or '')[:100]}). Tell me what to send next and I'll make a new card."]
        card = cards._read(cards.LIVE / f"{qid}.json")
    if not card or card.get("status") != "open":
        return [f"I couldn't revise that card ({qid}): it is no longer open."]
    q = copy.deepcopy((card.get("questions") or [{}])[0])
    key, old = chat_push.exec_text(q)
    if not key:
        return ["That card has no outgoing text to revise; answer it with a tap."]
    q["exec"]["payload"][key] = new_text
    # swap the text inside the question as it stands (the from/to/subject line and the preview tag
    # stay exactly where they were: the send guard requires the owner to read them on the card)
    qtext = str(q.get("question") or "")
    if old and old in qtext:
        q["question"] = qtext.replace(old, new_text, 1)
    else:
        ask, to, _ = chat_push._split_preview(qtext)
        tag = "[EMAIL PREVIEW]" if "[EMAIL PREVIEW]" in qtext else "[TEXT PREVIEW]"
        q["question"] = (ask.rstrip() + "\n" if ask.strip() else "") + f"{tag} {to + ':' if to else ''}\n{new_text}".rstrip()
    q["details"] = (f"Revised per {person['name']} in this thread. Previous text: {old[:400]}" + ("\n\n" + str(q.get("details") or "") if q.get("details") else ""))[:2500]
    for k in ("rulings", "chat_thread"):
        q.pop(k, None)
    # asked=True (2026-10-23): an owner asked for this revision in the thread, so it is urgent there and
    # SUPERSEDES rather than trips the same-recipient / same-subject guards (Corwin's revised proposal card
    # was refused twice on 10-21/22 by the 24-hour recipient limit while Sage said it was below).
    r = cards.post([q], owner=f"agent:{person['key']}", lane=q.get("lane") or "personal", thread=str(ctx.get("thread") or card.get("chat_thread") or ""),
                   replaces=qid, space=str(ctx.get("space") or card.get("chat_space") or ""), asked=True)
    if not r.get("posted"):
        return [f"The revised card didn't post ({r.get('reason')}); the original is still open."]
    cards.supersede(qid, r["qid"], by=f"agent:{person['key']}")
    core.log_change("card_revise", f"card {qid} revised in thread -> {r['qid']}", {"op": "none"}, person["key"])
    return []


def _queue_task(lane: str, text: str, person: dict, ctx: dict | None = None) -> str:
    """-> the ticket id ('' when it was already queued). The conversation, thread and space the
    owner asked in ride on the payload, so the operator's card lands there (the owner 2026-10-17: the
    re-drafted mechanic text and Justin's card came back top-level, not in the thread; 2026-10-21:
    a card asked for in a DM is posted in that DM, at once)."""
    sys.path.insert(0, str(core.AGENT.parent / "scripts"))
    import tickets
    ctx = ctx or {}
    sid = f"{time.time():.0f}|{text[:40]}"
    n = tickets.add_many([{"source": "agent", "source_id": sid, "lane": lane,
                           "kind": "owner1_request", "title": f"{person['name']} asked: {text[:120]}",
                           "summary": text[:600],
                           "payload": {"text": text, "via": core.profile().get("slug", "agent"), "from": person["key"],
                                       "convo": str(ctx.get("convo") or ""), "thread": str(ctx.get("thread") or ""),
                                       "space": str(ctx.get("space") or ""), "asked": True}}],
                         "agent")
    return tickets.make_id("agent", sid) if n > 0 else ""


# ------------------------------------------------------------------ a draft the brain wrote -> the card, now
_PREVIEW = {"text": "[TEXT PREVIEW]", "email": "[EMAIL PREVIEW]"}   # service preview tag is crm.preview_tag() at call time


def _phone_account(person: dict) -> str:
    """The owner's own iPhone (phone_bridge.ACCOUNTS by owner name; config leads.text_account as the default)."""
    sys.path.insert(0, str(core.AGENT.parent / "scripts"))
    import phone_bridge
    first = str(person.get("name") or "").split()[0].lower() if person.get("name") else ""
    for acct, meta in phone_bridge.ACCOUNTS.items():
        if str(meta.get("owner") or "").lower() == first:
            return acct
    try:
        cfg = core.load(core.AGENT.parent / "config.json", {})
        return str((cfg.get("leads") or {}).get("text_account") or phone_bridge.DEFAULT_ACCOUNT)
    except Exception:  # noqa: BLE001
        return phone_bridge.DEFAULT_ACCOUNT


def _mail_from(person: dict, lane: str, account: str = "") -> str:
    """The address an email goes out from: the named account, else the owner's work mailbox for the
    organization's lanes and their personal one otherwise (google.json)."""
    import google_api
    accts = google_api.accounts()
    if account in accts:
        return str(accts[account].get("address") or "")
    if "@" in account:                                      # the draft named the address itself (exec_store still checks it)
        return account.strip()
    mine = [(n, a) for n, a in accts.items() if a.get("person") == person.get("key")]
    if not mine:
        # no Google account on record (a new organization): the person's own address that org.json allows to send
        try:
            sys.path.insert(0, str(core.AGENT.parent))
            import orgcfg
            allowed = [str(x).lower() for x in orgcfg.get("send_identities", []) or []]
        except Exception:  # noqa: BLE001
            allowed = []
        own = [str(e) for e in (person.get("emails") or []) if str(e).lower() in allowed]
        return own[0] if own else ""
    work = [a for n, a in mine if n.endswith("-films") or "work" in n or "business" in n]
    if lane not in ("personal", "home", "money") and work:
        return str(work[0].get("address") or "")
    personal = [a for n, a in mine if "personal" in n]
    return str((personal or [mine[0][1]])[0].get("address") or "")


def team_member(to: str) -> str:
    """The organization's own person (org.json `people`) a phone number or email belongs to, '' when it is
    nobody of ours. The handle must be theirs: written in their org entry or their Wiki/People page, or
    saved in the contacts under their full name. A drafted name alone never counts (2026-10-24: a pay
    offer to Marlo was refused by the lead price lint)."""
    sys.path.insert(0, str(core.AGENT.parent / "scripts"))
    import contacts
    import org
    h = str(to or "").strip()
    key = contacts.email_key(h) if "@" in h else contacts.phone_key(h)
    if len(key) < 7:
        return ""
    try:
        saved = str((contacts.lookup(h) or {}).get("name") or "").lower().split()
    except Exception:  # noqa: BLE001
        saved = []
    for p in org.load().get("people") or []:
        name = str(p.get("name") or "").strip()
        if not name:
            continue
        page = core.VAULT / "Wiki" / "People" / f"{name}.md"
        try:
            text = str(p.get("about") or "") + "\n" + (page.read_text(encoding="utf-8") if page.is_file() else "")
        except OSError:
            text = str(p.get("about") or "")
        if "@" in h:
            mine = {contacts.email_key(e) for e in re.findall(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+", text)}
        else:
            mine = {contacts.phone_key(n) for n in re.findall(r"\+?\d[\d ().-]{6,}\d", text)}
        toks = name.lower().split()
        if key in mine or (len(toks) >= 2 and all(t in saved for t in toks)):
            return name
    return ""


def draft_card(lane: str, text: str, draft: dict, person: dict, ctx: dict) -> dict:
    """Post the card for a message the brain drafted itself, in the place it was asked (thread or DM),
    urgent, superseding an older open card on the same subject. -> cards.post's result. Nothing is sent:
    the owner's tap mints the exec ticket exactly as before."""
    sys.path.insert(0, str(core.AGENT.parent / "scripts"))
    import cards
    question = _draft_question(lane, text, draft, person)
    return cards.post([question], owner=f"agent:{person['key']}", lane=lane, thread=str(ctx.get("thread") or ""),
                      space=str(ctx.get("space") or ""), asked=True)


def draft_blocker(lane: str, text: str, draft: dict, person: dict) -> str:
    """Why cards.post would refuse this drafted card even though the owner asked for it, '' when it would post.
    Read-only: the exec check and the draft lint, the guards an owner's ask does not supersede (2026-10-22 12:49:
    Corwin's proposal email was refused DRAFT_LINT for its "$" price after the task tool had said "posted below")."""
    sys.path.insert(0, str(core.AGENT.parent / "scripts"))
    import cards
    import draft_lint
    q = _draft_question(lane, text, draft, person)
    _, err = cards._sanitize_exec(q)
    if err:
        return err
    blocks, _ = draft_lint.check_card(q)
    return ("DRAFT_LINT: " + " | ".join(blocks)) if blocks else ""


def _draft_question(lane: str, text: str, draft: dict, person: dict) -> dict:
    """The card question for a drafted message (draft_card posts it; draft_blocker checks it)."""
    ch = str(draft.get("channel") or "").lower()
    to, body = str(draft.get("to") or "").strip(), str(draft.get("text") or "").strip()
    name = str(draft.get("name") or "").strip()
    lead = re.sub(r"\s+", " ", text).strip()[:220]
    if ch == "text":
        ex = {"kind": "imessage_send", "approve_label": "Send (Recommended)",
              "payload": {"account": _phone_account(person), "to": to, "body": body}}
        q = f"{lead}\n{_PREVIEW['text']} To {name + ' ' if name else ''}{to}:\n{body}"
        header = f"Text to {name or to}"[:60]
    elif ch == "email":
        frm = _mail_from(person, lane, str(draft.get("account") or ""))
        subject = str(draft.get("subject") or "").strip()
        ex = {"kind": "email_send", "approve_label": "Send (Recommended)",
              "payload": {"from": frm, "to": to, "subject": subject, "body": body}}
        q = f"{lead}\n{_PREVIEW['email']} From: {frm} To: {to}\nSubject: {subject}\n{body}"
        header = f"Email to {name or to}"[:60]
    else:
        sys.path.insert(0, str(core.AGENT.parent / "scripts"))
        import crm as _crm_m
        _tid = str(draft.get("thread_id") or draft.get("workspace_id") or "")
        _subj = str(draft.get("subject") or "")
        _irt = str(draft.get("in_reply_to_id") or "")
        ex = _crm_m.message_exec(_tid, body, _subj, _irt)
        if ex is None:
            ex = {"kind": "crm_message", "approve_label": "Send (Recommended)",
                  "payload": {"thread_id": _tid, "body": body, "subject": _subj}}
        else:
            ex["approve_label"] = ex.get("approve_label") or "Send (Recommended)"
        _ptag = _crm_m.preview_tag()
        q = f"{lead}\n{_ptag} To {name or to}:\n{body}"
        header = f"{_crm_m.name()} message to {name or to}"[:60]
    if core.is_owner(person):
        mark = _resend_mark(ex)
        if mark:
            q += "\n\n" + mark
    sa = str(draft.get("send_at") or "").strip()
    if sa:
        # a send the owner wants LATER (2026-10-24): the card says the time in words, the tap mints a
        # ticket that drain holds until then (exec_store.not_before); undo cancels it until it goes
        import exec_store as es
        ex["send_at"] = sa
        q += f"\n\nSends at {es.when_text(sa)}"
        header = f"{header} at {es.when_text(sa)}"[:60]
    question = {"question": q, "header": header, "lane": lane, "audience": "client" if lane not in ("personal", "home") else "personal",
                "details": f"Asked by {person['name']} in chat: {text[:800]}",
                "options": [{"label": "Send (Recommended)", "description": "goes out exactly as written" + (" at the time shown" if sa else "")}, {"label": "Skip"}],
                "exec": ex, "urgent": True}
    import draft_lint
    if ch in ("text", "email") and draft_lint.audience_for(lane) == "lead" and team_member(to):
        # our own shooter / editor, not a lead: their pay is not a price promise, so the draft lint
        # checks it as a vendor message (the lead and couple rules stay as they are for everyone else)
        question["thread_class"] = "vendor"
    return question


def _resend_mark(ex: dict) -> str:
    """The RESEND line for a further message an owner asked for in chat to someone who already got one today
    (or has one queued), '' otherwise. cards.answer reads the word and the first tap mints past the same-thread
    guard (2026-10-25: an owner's asked-for follow-up was refused and cost a rebuilt card). The exact same
    message never gets it, so a true duplicate is still refused; cards the system posts never come here."""
    sys.path.insert(0, str(core.AGENT.parent / "scripts"))
    import exec_store as es
    kind, pay = str(ex.get("kind") or ""), ex.get("payload") or {}
    if kind not in es.OUTBOUND_KINDS:
        return ""
    target = es._outbound_target(kind, pay)
    if not target or es.recent_done_by_payload_sha(kind, pay):
        return ""
    sha, cutoff, when = es.payload_sha(pay), time.time() - 86400, ""
    for t in es.fold().values():
        if t.get("kind") != kind or es._outbound_target(kind, t.get("payload") or {}) != target:
            continue
        if t.get("status") in ("queued", "running"):
            if t.get("payload_sha") == sha:
                return ""                 # the very same message is already on its way
            when = when or "is on its way"
        elif t.get("status") == "done" and es._done_ts(t) >= cutoff:
            when = "went at " + time.strftime("%I:%M %p", time.localtime(es._done_ts(t))).lstrip("0")
    return f"RESEND: you asked for this one; the earlier message to them {when}." if when else ""


def _remember_hold(qid: str, draft: dict, since: float = 0) -> None:
    """'Text him at 4 if they haven't replied' (2026-10-28): the condition is stored with the card, and
    scripts/send_hold.py, right before the drain, holds the timed send if the person wrote in first."""
    sys.path.insert(0, str(core.AGENT.parent / "scripts"))
    try:
        import send_hold
        send_hold.remember(qid, {"to": draft.get("to"), "name": draft.get("name"), "channel": draft.get("channel"),
                                 "workspace_id": draft.get("workspace_id")}, since=since)
    except Exception as e:  # noqa: BLE001
        core.log_change("hold_if_reply", f"card {qid}: the condition could not be stored ({type(e).__name__})", {"op": "none"}, "code")


def job_card(jb: dict, person: dict, ctx: dict) -> dict:
    """ONE card to run a registered local job (jobs.json) now or at a time; the tap mints a `local`
    ticket drain launches when due (2026-10-24: "schedule ... everything else I've asked it to do")."""
    sys.path.insert(0, str(core.AGENT.parent / "scripts"))
    import cards
    import exec_store as es
    import jobs
    j = jobs.find(str(jb.get("job") or ""))
    if not j:
        return {"posted": False, "reason": f"no job {jb.get('job')!r}"}
    args = {str(k): str(v).strip() for k, v in (jb.get("args") or {}).items() if str(v or "").strip()}
    at = str(jb.get("at") or "").strip()
    desc = jobs.describe(j, args)
    when = f"Runs at {es.when_text(at)}." if at else "Runs right after your tap."
    q = f"{person['name']} asked to run {desc}.\n{when}\n{j.get('what', '')}"
    lane = jb.get("lane") if jb.get("lane") in LANES else str(j.get("lane") or "personal")
    ex = {"kind": "local", "payload": {"op": j["key"], "args": args}, "approve_label": "Run (Recommended)"}
    if at:
        ex["send_at"] = at
    question = {"question": q, "header": (f"Run: {j.get('name', j['key'])}" + (f" at {es.when_text(at)}" if at else ""))[:60],
                "lane": lane, "audience": "personal", "details": f"Command: {' '.join(jobs.build_cmd(j, args))}",
                "options": [{"label": "Run (Recommended)", "description": "starts the job" + (" at the time shown" if at else " now")}, {"label": "Skip"}],
                "exec": ex, "urgent": True}
    return cards.post([question], owner=f"agent:{person['key']}", lane=lane, thread=str(ctx.get("thread") or ""),
                      space=str(ctx.get("space") or ""), asked=True)


def decide_card(lane: str, d: dict, person: dict, ctx: dict) -> dict:
    """Post a decision card for a choice that is the owner's (the owner, 2026-10-22: "instead of ending with
    'would you like this or that', make approval cards for the next decision with a recommendation").
    The recommended option goes FIRST with the "(Recommended)" tag cards.py's NO_RECOMMENDATION rule wants;
    urgent, in the place it was asked. The card has NO exec: a tap only records the answer, which the
    bridge posts back into the agent's conversation ("[tapped ...]"), and the next turn acts on it with
    the usual tools. So no decision button sends anything by itself (the send gate is unchanged)."""
    sys.path.insert(0, str(core.AGENT.parent / "scripts"))
    import cards
    import intents
    rec = intents.plain_label(d.get("recommended"))
    opts = []
    for o in d.get("options") or []:
        o = o if isinstance(o, dict) else {"label": str(o)}
        label = intents.plain_label(o.get("label"))
        row = {"label": label}
        if str(o.get("description") or "").strip():
            row["description"] = str(o["description"]).strip()[:160]
        opts.append(row)
    opts.sort(key=lambda o: 0 if o["label"].lower() == rec.lower() else 1)
    if opts and opts[0]["label"].lower() == rec.lower():
        opts[0]["label"] = f"{opts[0]['label']} (Recommended)"
    why = str(d.get("why") or "").strip()
    details = f"Why {rec!r}: {why}" if why else ""
    if str(d.get("details") or "").strip():
        details += ("\n\n" if details else "") + str(d["details"]).strip()[:1500]
    details += ("\n\n" if details else "") + f"Asked by {person['name']} in chat; the answer goes back to the conversation and {core.profile().get('name', 'the agent')} acts on it."
    question = {"question": str(d.get("question") or "").strip(), "header": str(d.get("header") or "")[:60] or rec[:60],
                "lane": lane, "audience": "client" if lane not in ("personal", "home") else "personal",
                "details": details, "options": opts, "urgent": True}
    return cards.post([question], owner=f"agent:{person['key']}", lane=lane, thread=str(ctx.get("thread") or ""),
                      space=str(ctx.get("space") or ""), asked=True)


def _queue_do(lane: str, text: str, person: dict, ctx: dict, where: str = "") -> bool:
    """Hand work that must be DONE somewhere (a site, an account) to the doer (assistant/doer):
    one agent_run ticket, then wake the doer so it starts within seconds instead of at the
    next spine tick. The doer answers in this same conversation."""
    sys.path.insert(0, str(core.AGENT.parent / "scripts"))
    import tickets
    where = "computer" if str(where or "").lower() == "computer" else ""
    sid = f"do|{where or 'web'}|{time.time():.0f}|{text[:40]}" if where else f"do|{time.time():.0f}|{text[:40]}"
    skill = skills.match(text)                             # what was learned on that site last time rides along
    full = text + ("\n\n" + skills.steps_block(skill) if skill else "")
    n = tickets.add_many([{"source": "agent", "source_id": sid, "lane": lane,
                           "kind": "agent_run", "title": f"{person['name']}: {text[:120]}",
                           "summary": text[:600],
                           "payload": {"text": full, "via": core.profile().get("slug", "agent"), "from": person["key"],
                                       "skill": skill["slug"] if skill else "", "where": where,
                                       "convo": str(ctx.get("convo") or ""), "thread": str(ctx.get("thread") or ""),
                                       "space": str(ctx.get("space") or "")}}],
                        "agent")
    if n > 0 and skill:
        skills.note_use(skill)
    if n > 0 and WAKE_DOER:
        try:
            sys.path.insert(0, str(core.AGENT.parent / "doer"))
            import doer
            doer.wake()
        except Exception:  # noqa: BLE001  the spine's doer-tick picks it up within 10 minutes
            pass
    return tickets.make_id("agent", sid) if n > 0 else ""


WAKE_DOER = True          # tests switch this off


def made_events(days: int = 60) -> list:
    """Calendar events this agent created and has not removed, from the change ledger."""
    out, gone = [], set()
    for r in core.rows(core.CHANGES):
        u = r.get("undo") or {}
        if r.get("kind") in ("event_remove",) and u.get("id"):
            gone.add(u["id"])
    cut = time.time() - days * 86400
    for r in core.rows(core.CHANGES):
        u = r.get("undo") or {}
        if r.get("kind") != "event" or u.get("op") != "event_remove" or not u.get("id") or u["id"] in gone:
            continue
        try:
            if time.mktime(time.strptime(r.get("ts", ""), "%Y-%m-%d %H:%M")) < cut:
                continue
        except ValueError:
            pass
        m = re.match(r"calendar event '(.*)' (\S+(?: \S+)?) on (\w+)'s calendar", str(r.get("summary") or ""))
        title, when, owner = (m.group(1), m.group(2), m.group(3)) if m else (str(r.get("summary") or "")[:80], "", "")
        out.append({"id": u["id"], "account": u.get("account", ""), "title": title, "when": when, "owner": owner, "change": r.get("id")})
    return out


def find_made_event(match: str, date: str = "") -> tuple:
    """-> (event or None, reason). The agent may only touch events it created; `match` is a
    title fragment (case-insensitive), `date` narrows to YYYY-MM-DD."""
    cands = made_events()
    m = (match or "").strip().lower()
    if m:
        cands = [e for e in cands if m in e["title"].lower()]
    if date:
        cands = [e for e in cands if e["when"].startswith(date[:10])]
    if len(cands) == 1:
        return cands[0], ""
    if not cands:
        return None, (f"I don't have a calendar event of mine matching '{match}'" + (f" on {date[:10]}" if date else "")
                      + ". I can only change events I added; this one you'd change yourself.")
    return None, "Which one: " + "; ".join(f"'{e['title']}' ({e['when']})" for e in cands[:4]) + "?"


EVENT_RETRY = core.DATA / "event_retry.json"     # calendar adds that timed out, retried by the bridge's tick
EVENT_BACKOFF_S = (2, 5)        # in-turn waits before the 2nd and 3rd attempt
EVENT_RETRY_MIN = 5             # then the queue: one attempt this many minutes apart
EVENT_RETRY_TRIES = 3


def _timed_out(e: Exception) -> bool:
    return isinstance(e, TimeoutError) or "timed out" in str(e).lower()


def _create_event(account: str, ev: dict, eid: str, backoff=()) -> dict:
    """create_event, retried after each wait in `backoff` while Google times out; any other error at once."""
    import google_api
    for wait in (*backoff, None):
        try:
            return google_api.create_event(account, str(ev.get("title") or "").strip() or "Untitled", str(ev.get("start") or ""),
                                           str(ev.get("end") or ""), str(ev.get("location") or ""), str(ev.get("notes") or ""),
                                           bool(ev.get("all_day")), event_id=eid)
        except Exception as e:  # noqa: BLE001
            if wait is None or not _timed_out(e):
                raise
            time.sleep(wait)


def _event_reminder(ev: dict, owner: str, by: str) -> None:
    """A timed event an owner gave Sage gets a reminder 45 min before it, to that owner (the owner 2026-10-24, Tony's
    party: "You should have given me reminders about it since I gave you the information"). Never fatal."""
    try:
        from datetime import datetime as _dt, timedelta as _td
        if ev.get("all_day") or not str(ev.get("start") or "").strip():
            return
        start = _dt.strptime(str(ev["start"])[:16].replace("T", " "), "%Y-%m-%d %H:%M")
        at = start - _td(minutes=45)
        if at <= _dt.now() + _td(minutes=2):
            return
        if at.hour < 7:
            at = at.replace(hour=7, minute=0)
        where = f", {str(ev.get('location')).strip()}" if ev.get("location") else ""
        routines.add({"title": f"{str(ev.get('title') or 'Your event')[:60]}, coming up", "when": {"once": at.strftime("%Y-%m-%d %H:%M")},
                      "to": owner, "say": f"{ev.get('title') or 'Your event'} is at {start.strftime('%I:%M %p').lstrip('0')}{where}."}, by=by)
    except Exception:  # noqa: BLE001
        pass


def _log_event(ev: dict, owner: str, account: str, made: dict, by: str) -> None:
    core.log_change("event", f"calendar event '{ev.get('title')}' {ev.get('start')} on {owner}'s calendar",
                    {"op": "event_remove", "account": account, "id": made.get("id") or ""}, by)


def retry_events(post_in, now: float | None = None) -> int:
    """The bridge's minute tick: one more attempt for each queued calendar add that is due. The
    result, good or bad, goes into the thread it was asked in. -> how many were settled."""
    q = core.load(EVENT_RETRY, [])
    if not q:
        return 0
    now = time.time() if now is None else now
    keep, n = [], 0
    for it in q:
        if float(it.get("next_try") or 0) > now:
            keep.append(it)
            continue
        ev, title = it.get("ev") or {}, (it.get("ev") or {}).get("title") or "the event"
        it["tries"] = int(it.get("tries") or 0) + 1
        try:
            made = _create_event(it["account"], ev, it["eid"])
        except Exception as e:  # noqa: BLE001
            if _timed_out(e) and it["tries"] < EVENT_RETRY_TRIES:
                it["next_try"] = now + EVENT_RETRY_MIN * 60
                keep.append(it)
                continue
            post_in(it["convo"], it["thread"], f"I still couldn't get '{title}' onto the calendar"
                    + (" (Google kept timing out)" if _timed_out(e) else f" ({str(e)[:80]})")
                    + ". It's not on there; add it yourself or ask me to try again.")
            n += 1
            continue
        _log_event(ev, it.get("owner", ""), it["account"], made, it.get("by", ""))
        post_in(it["convo"], it["thread"], f"'{title}' is on the calendar now; the second try went through.")
        n += 1
    core.save(EVENT_RETRY, keep)
    return n


def events_block() -> str:
    ev = made_events()
    if not ev:
        return ""
    return ("CALENDAR EVENTS YOU ADDED (you may change or remove these with event_change / event_remove, by title and date; "
            "anything else on the calendar is theirs to change):\n" + "\n".join(f"- {e['title']} | {e['when']} | {e['owner']}" for e in ev[-20:]))


def identify(idn: dict, person: dict) -> str:
    """An owner said who a number is (the `identify` deed, 2026-10-23): whois.learn keeps it (aliases
    file + the thread's name, so cards and the comms board show it), memory gets one fact, and the
    change is ledgered with an undo. Returns a line only when something went wrong."""
    sys.path.insert(0, str(core.VAULT / "assistant" / "scripts"))
    try:
        import whois
    except Exception as e:  # noqa: BLE001
        return f"I couldn't save who that number is ({type(e).__name__})."
    handle, name = str(idn.get("handle") or ""), str(idn.get("name") or "")
    rel, kind = str(idn.get("relation") or ""), str(idn.get("kind") or "")
    res = whois.learn(handle, name, rel, kind, by=person.get("key", ""), evidence=f"{person.get('name', '')} in chat {core.stamp()}")
    if not res.get("ok"):
        return f"I couldn't save who that number is ({res.get('reason', '')})."
    fact = f"{name}" + (f" ({rel})" if rel else "") + f" texts from {whois.pretty(handle)}"
    core.mem_upsert("person", name[:40], fact, evidence=f"{person.get('name', '')} in chat {core.stamp()}", by=person.get("key", ""))
    core.log_change("identify", f"{whois.pretty(handle)} is {name}" + (f" ({rel})" if rel else ""),
                    {"op": "alias_forget", "handle": handle, "before": res.get("replaced")}, person.get("key", ""))
    return ""


def apply(sug: dict, person: dict, ctx: dict | None = None) -> list:
    """-> confirmation lines for the chat reply. `ctx` is where the message came from
    ({"convo", "thread"}), which a follow-up needs to know."""
    _tls.followups = []
    _tls.applied = []
    out = []
    if not sug:
        return out
    by = person["key"]
    if "_bad" in sug:
        return ["Something I meant to save didn't go through; say it again and I'll get it right."]
    if not core.can(person, "teach"):
        # a staff member (2026-10-24): only the deeds their role allows survive; the card they asked for is posted to
        # the OWNERS (the lane's usual space), never into the staff member's own chat, with their name on it
        keep = {k: v for k, v in sug.items() if k in ("task", "note") and core.can(person, k)}
        if not keep:
            return ["Only an owner can change what I know or do."]
        sug = keep
        ctx = {**(ctx or {}), "thread": "", "space": "", "asked_by": person.get("name") or person.get("key")}
        for t in _list(sug.get("task")):
            if isinstance(t, dict) and t.get("text"):
                t["text"] = f"Asked by {person.get('name')} (team) in chat: {str(t['text']).strip()}"

    for r in _list(sug.get("learn"))[:CAPS["learn"]]:
        r = r if isinstance(r, dict) else {"rule": str(r)}
        res = core.rule_add(r.get("rule", ""), f"{person['name']}: {r.get('why', '')}".strip(), by=by)
        line = {"applied": "", "folded": "",
                "pending": "That one loosens a safety rule, so it's waiting for your hand on Mission Control rather than mine.",
                }.get(res["status"], f"I couldn't save that rule ({res.get('reason')}).")
        if line:
            out.append(line)
    for rid in _list(sug.get("unlearn"))[:CAPS["unlearn"]]:
        if not core.rule_retire(str(rid), f"{person['name']} in chat", by=by):
            out.append(f"I don't have an active rule {rid} to drop.")
    for f in _list(sug.get("remember"))[:CAPS["remember"]]:
        if not isinstance(f, dict):
            continue
        res = core.mem_upsert(f.get("kind", ""), f.get("subject", ""), f.get("fact", ""),
                              evidence=f"{person['name']} in chat {core.stamp()}", by=by)
        if res["status"] not in ("applied", "folded"):
            out.append(f"I couldn't save that ({res.get('reason')}).")
    for mid in _list(sug.get("forget"))[:CAPS["forget"]]:
        if not core.mem_forget(str(mid), f"{person['name']} in chat", by=by):
            out.append(f"I don't have a fact {mid} to forget.")
    for idn in _list(sug.get("identify"))[:CAPS.get("identify", 5)]:
        if not isinstance(idn, dict):
            continue
        line = identify(idn, person)
        if line:
            out.append(line)

    # long-running problems (projects.py, 2026-10-27): opened, updated, parked or closed by the owner's words; the pass works them
    for pr in _list(sug.get("project"))[:3]:
        if not isinstance(pr, dict) or not core.can(person, "teach"):
            continue
        try:
            import projects
            line = projects.apply_deed(pr, person, ctx or {})
        except Exception as e:  # noqa: BLE001
            line = f"I couldn't record that project change ({type(e).__name__})."
        if line:
            out.append(line)

    note = sug.get("note")
    if isinstance(note, dict) and core.can(person, "note"):
        if _BUILD_REQ.search(str(note.get("body") or "")) and (ctx or {}).get("text") and "<!-- capability:" not in str(note.get("body") or ""):
            # the ask that led to this request rides in the note (capability.py, 2026-10-25): the builder reads it as the
            # acceptance test and code replays the ask to the brain when the build lands, so the thing gets done
            try:
                import capability
                g = {"id": "note", "convo": str(ctx.get("convo") or ""), "thread": str(ctx.get("thread") or ""), "space": str(ctx.get("space") or ""),
                     "person": person.get("key", ""), "ask": str(ctx.get("text") or "")}
                note = dict(note, body=str(note["body"]).rstrip() + f"\n\n**Ask ({person.get('name', 'owner')}, {core.stamp()}, in chat):** \"{capability._short(g['ask'], 500)}\""
                            + "\n\n**Acceptance:** when the ask above is replayed, it completes with a deed and no \"can't\" line." + capability.footer(g))
            except Exception:  # noqa: BLE001
                pass
        res = wiki_writer.write(note, by=by, ask=str((ctx or {}).get("text") or ""))
        if res["status"] == "refused" and res.get("suggest") and res.get("gap"):
            # a type the schema lacks is a recorded gap (the nightly growth may add it); the note is filed now under the
            # nearest allowed type so nothing the person said is lost (schema_growth.py, 2026-10-29)
            res = wiki_writer.write(dict(note, type=res["suggest"]), by=by)
        if res["status"] not in ("applied", "appended"):
            out.append(f"The note didn't save ({res.get('reason')}).")
        elif _BUILD_REQ.search(str(note.get("body") or "")):
            # a build request is a promise: the builder's outcome gets reported back in this thread
            LAST_APPLIED.append({"kind": "build", "what": str(note.get("title") or "")[:120], "ref": res.get("path", "")})
            # the owner, 2026-10-22: "Sage should build things right away when it solves an immediate problem." An
            # owner's ask in chat is by definition immediate, so it is kicked now; `force` only skips the hourly
            # gap and the daily cap (the protected-core fence, the needs-owner card and MAX_ATTEMPTS still hold).
            # Dream-filed requests do not pass through here and keep the throttled path.
            try:
                import builder
                core.log_change("build_kick", f"build started at once for '{str(note.get('title') or '')[:80]}': "
                                f"{builder.kick(force=True, first=str(res.get('path') or ''))}",
                                {"op": "none"}, by)
            except Exception as e:  # noqa: BLE001
                out.append(f"The build request is filed, but it didn't start right away ({type(e).__name__}); it goes on the next run.")

    sk = sug.get("skill")
    if isinstance(sk, dict) and core.can(person, "note"):
        res = skills.write(sk, by=by)
        if res["status"] not in ("applied", "appended"):
            out.append(f"I couldn't keep that skill ({res.get('reason')}).")

    # the agent named its own blockers (capability.py, 2026-10-25): a capability files its build now and is
    # replayed when it lands; an owner-only blocker is recorded so the regex backstop stays quiet
    for b in _list(sug.get("blocked"))[:3]:
        if not isinstance(b, dict) or not core.is_owner(person):
            continue
        try:
            import capability
            r = capability.from_blocked(b, person, ctx or {})
        except Exception as e:  # noqa: BLE001
            r = {"status": "error", "reason": f"{type(e).__name__}: {str(e)[:120]}"}
        LAST_APPLIED.append({"kind": "blocked", "what": str(b.get("what") or "")[:160], "ref": str(r.get("build") or "")})
        if r.get("build"):
            LAST_APPLIED.append({"kind": "build", "what": str(b.get("what") or "")[:120], "ref": str(r["build"])})
        elif r.get("status") == "error":
            out.append(f"I couldn't file the build for that step ({r.get('reason')}).")

    # what the owners teach the agent about the organization itself (org.py): name, lanes,
    # services, duties, never-list. Applied quietly; ledgered; undoable.
    og = sug.get("org")
    if isinstance(og, dict) and og:
        import org
        res = org.apply(og, by=by)
        if res["status"] == "refused":
            out.append(f"I couldn't record that about the business ({res.get('reason')}).")
        elif res["status"] == "applied":
            LAST_APPLIED.append({"kind": "org", "what": ", ".join(res.get("changed") or [])[:160], "ref": res.get("change", "")})
            # a duty with a time becomes a schedule the same minute, so "every Monday send me the week's orders"
            # is kept by code, not by the model's memory
            for d in (og.get("duties") if isinstance(og.get("duties"), list) else [og.get("duties")] if og.get("duties") else []):
                if isinstance(d, dict) and isinstance(d.get("schedule"), dict) and core.can(person, "schedule"):
                    routines.add({"title": str(d.get("text") or "")[:80], "when": d["schedule"],
                                  "to": str(d.get("to") or "both"), "prompt": str(d.get("text") or "")}, by=by)

    for s in _list(sug.get("schedule"))[:2]:
        if isinstance(s, dict) and core.can(person, "schedule"):
            res = routines.add(s, by=by)
            if res["status"] != "applied":
                out.append(f"I couldn't set that schedule ({res.get('reason')}).")
    for sid in _list(sug.get("unschedule"))[:CAPS["unschedule"]]:
        if not routines.disable(str(sid), by=by):
            out.append(f"I don't have an active schedule {sid} to stop.")
    for sw in _list(sug.get("switch"))[:2]:
        if isinstance(sw, dict) and core.can(person, "schedule"):
            import switches
            if sw.get("run") is True and sw.get("on") in (None, ""):
                res = switches.run_now(str(sw.get("job") or ""), by=by)
                if res["status"] != "started":
                    out.append(f"I couldn't start that ({res.get('reason')}).")
                continue
            res = switches.set_on(str(sw.get("job") or ""), bool(sw.get("on")), by=by)
            if res["status"] == "refused":
                out.append(f"I couldn't switch that ({res.get('reason')}).")
            elif res.get("reason"):
                out.append(f"Half done: {res['reason']}.")

    for ev in _list(sug.get("event"))[:8]:
        if not isinstance(ev, dict) or not core.can(person, "schedule"):
            continue
        import google_api
        owner = ev.get("calendar") if ev.get("calendar") in (core.people().get("users") or {}) else by
        accts = google_api.accounts_of(owner)
        if not accts:
            out.append(f"Not added to the calendar: {owner}'s Google calendar is not connected yet.")
            continue
        eid = secrets.token_hex(16)                     # one id for every attempt: a retry can never double the event
        try:
            made = _create_event(accts[0], ev, eid, EVENT_BACKOFF_S)
        except Exception as e:  # noqa: BLE001
            if _timed_out(e):
                # still timing out after the quick retries: the bridge tries again next minute-tick
                # and says how it went in this thread (2026-10-14: a one-off timeout dropped an event)
                c = ctx or {}
                q = core.load(EVENT_RETRY, [])
                q.append({"ev": ev, "eid": eid, "owner": owner, "account": accts[0], "by": by, "tries": 0,
                          "convo": str(c.get("convo") or f"dm:{by}"), "thread": str(c.get("thread") or ""),
                          "next_try": time.time() + EVENT_RETRY_MIN * 60})
                core.save(EVENT_RETRY, q)
                out.append(f"The calendar timed out on '{ev.get('title')}', so it isn't on yet. I'll try again in a few minutes and tell you here.")
            else:
                out.append(f"That didn't make it onto the calendar ({str(e)[:80]}).")
            continue
        _log_event(ev, owner, accts[0], made, by)
        _event_reminder(ev, owner, by)

    # edit / remove events the agent itself created (the owner 2026-10-07; the ledger is the allow-list)
    for ev in _list(sug.get("event_remove"))[:4]:
        if not core.can(person, "schedule"):
            continue
        spec = ev if isinstance(ev, dict) else {"match": str(ev)}
        found, why = find_made_event(str(spec.get("match") or ""), str(spec.get("date") or spec.get("start") or ""))
        if not found:
            out.append(why)
            continue
        import google_api
        if google_api.remove_event(found["account"], found["id"]):
            core.log_change("event_remove", f"removed calendar event '{found['title']}' ({found['when']})", {"op": "none", "id": found["id"]}, by)
        else:
            out.append(f"I couldn't remove '{found['title']}' from the calendar just now.")
    for ev in _list(sug.get("event_change"))[:4]:
        if not isinstance(ev, dict) or not core.can(person, "schedule"):
            continue
        found, why = find_made_event(str(ev.get("match") or ""), str(ev.get("date") or ""))
        if not found:
            out.append(why)
            continue
        import google_api
        try:
            google_api.update_event(found["account"], found["id"], str(ev.get("title") or ""), str(ev.get("start") or ""),
                                    str(ev.get("end") or ""), ev.get("location"), ev.get("notes"),
                                    ev.get("all_day") if "all_day" in ev else (False if ev.get("start") and len(str(ev.get("start"))) > 10 else None))
        except Exception as e:  # noqa: BLE001
            out.append(f"I couldn't change '{found['title']}' on the calendar ({str(e)[:80]}).")
            continue
        core.log_change("event_change", f"changed calendar event '{found['title']}' ({found['when']}) -> "
                        + ", ".join(f"{k}={v}" for k, v in ev.items() if k not in ("match", "date") and v not in (None, "")),
                        {"op": "none", "id": found["id"]}, by)

    if sug.get("followups") and core.can(person, "schedule"):
        import followups
        _tls.followups = followups.add(_list(sug.get("followups")), (ctx or {}).get("convo", ""),
                                       (ctx or {}).get("thread", ""), person)
        # no line: Sage already said it would check back; the tracker's keep_promises keeps that honest

    for jb in _list(sug.get("job"))[:2]:
        if not (isinstance(jb, dict) and core.can(person, "task")):
            continue
        try:
            r = job_card(jb, person, ctx or {})
        except Exception as e:  # noqa: BLE001
            r = {"posted": False, "reason": f"{type(e).__name__}: {str(e)[:120]}"}
        if r.get("posted"):
            LAST_APPLIED.append({"kind": "card", "what": f"run {jb.get('job')}"[:160], "ref": str(r.get("qid") or "")})
        else:
            out.append(f"The job card didn't post ({str(r.get('reason') or '')[:140]}).")

    # an owner's "send it" / "send it at 8am" in a card's thread (2026-10-24): the card is answered on their words
    for ap in _list(sug.get("approve"))[:2]:
        if not (isinstance(ap, dict) and core.can(person, "approve")):
            continue
        sys.path.insert(0, str(core.AGENT.parent / "scripts"))
        import cards
        qid = str(ap.get("qid") or "")
        if ap.get("reopen") and re.fullmatch(r"q[0-9a-f]{12}", qid):
            # "Fix it and send" after an approved send failed (2026-10-28): the same card comes back, answered just below
            try:
                rr = cards.reopen(qid, f"{str(ap.get('why') or 'it failed')[:120]}; {person['name']} said to send it again",
                                  by=f"{by}:chat")
            except Exception as e:  # noqa: BLE001
                rr = {"ok": False, "reason": f"{type(e).__name__}: {str(e)[:120]}"}
            if not rr.get("ok") and rr.get("reason") != "already open":
                out.append(f"I couldn't bring that send back ({str(rr.get('reason') or '')[:140]}), so nothing went out.")
                continue
        card = cards._read(cards.LIVE / f"{qid}.json") if re.fullmatch(r"q[0-9a-f]{12}", qid) else None
        if not card or card.get("status") != "open":
            out.append(f"That card ({qid}) is no longer open, so nothing was sent.")
            continue
        q = (card.get("questions") or [{}])[0]
        words = str(ap.get("words") or "").strip()[:80]
        if ap.get("skip"):
            # "he probably signed it already" in the thread of a text card (2026-10-28): closed unsent on their words
            try:
                r = cards.answer(qid, str(ap.get("option") or "Skip"), by=f"{by}:chat" + (f" ({words})" if words else ""))
            except Exception as e:  # noqa: BLE001
                r = {"ok": False, "reason": f"{type(e).__name__}: {str(e)[:120]}"}
            if r.get("ok"):
                LAST_APPLIED.append({"kind": "card", "what": f"skipped in chat: {str(q.get('header') or '')[:120]}", "ref": qid})
                core.log_change("card_skip", f"card {qid} skipped on {person['name']}'s words", {"op": "none"}, by)
            else:
                out.append(f"I couldn't skip that card ({str(r.get('reason') or '')[:140]}); it is still open.")
            continue
        label = str((q.get("exec") or {}).get("approve_label") or "Approve")
        try:
            r = cards.answer(qid, label, by=f"{by}:chat" + (f" ({words})" if words else ""), send_at=str(ap.get("at") or ""))
        except Exception as e:  # noqa: BLE001
            r = {"ok": False, "reason": f"{type(e).__name__}: {str(e)[:120]}"}
        if r.get("ok") and not r.get("mint_error"):
            if ap.get("hold_if_reply") and ap.get("at"):
                pay = (q.get("exec") or {}).get("payload") or {}
                _remember_hold(qid, {"to": pay.get("to") or pay.get("workspace_id"), "workspace_id": pay.get("workspace_id"),
                                     "name": re.sub(r"^.*? to\s+", "", str(q.get("header") or "")).split(" at ")[0]},
                               since=float(card.get("ts") or 0))
            LAST_APPLIED.append({"kind": "card", "what": f"approved in chat: {str(q.get('header') or '')[:120]}", "ref": qid})
            core.log_change("card_approve", f"card {qid} approved on {person['name']}'s words" + (f", goes out {ap.get('at')}" if ap.get("at") else ""),
                            {"op": "none"}, by)
        else:
            out.append(f"I couldn't approve that card ({str(r.get('mint_error') or r.get('reason') or '')[:140]}); it is still open for your tap.")

    for task in _list(sug.get("task"))[:3]:
        if not (isinstance(task, dict) and str(task.get("text") or "").strip() and core.can(person, "task")):
            continue
        lane = task.get("lane") if task.get("lane") in LANES else "personal"
        draft = task.get("draft") if isinstance(task.get("draft"), dict) else None
        if draft and draft.get("text"):
            # the brain drafted the message itself: the card is posted NOW, where it was asked (2026-10-21)
            try:
                r = draft_card(lane, str(task["text"]).strip(), draft, person, ctx or {})
            except Exception as e:  # noqa: BLE001
                r = {"posted": False, "reason": f"{type(e).__name__}: {str(e)[:120]}"}
            if r.get("posted"):
                LAST_APPLIED.append({"kind": "card", "what": str(task["text"]).strip()[:160], "ref": str(r.get("qid") or "")})
                if draft.get("hold_if_reply") and draft.get("send_at") and r.get("qid"):
                    _remember_hold(str(r["qid"]), draft)
                continue
            # the card was refused: hand the same ask to the operator so a card still comes (one line says so)
            out.append(f"The card didn't post ({str(r.get('reason') or '')[:140]}); I've handed the draft on instead, so one still comes.")
        try:
            tid = _queue_task(lane, str(task["text"]).strip(), person, ctx or {})
        except Exception as e:  # noqa: BLE001
            tid = ""
            out.append("I couldn't hand that off just now; ask me again in a minute.")
        else:
            if not tid:
                out.append("That one's already in the queue.")
            else:
                LAST_APPLIED.append({"kind": "task", "what": str(task["text"]).strip()[:160], "ref": tid})

    # a decision that is the owner's: the card with the recommendation first, where it was asked (2026-10-23)
    for d in _list(sug.get("decide"))[:2]:
        if not (isinstance(d, dict) and str(d.get("question") or "").strip() and core.can(person, "task")):
            continue
        lane = d.get("lane") if d.get("lane") in LANES else "personal"
        try:
            r = decide_card(lane, d, person, ctx or {})
        except Exception as e:  # noqa: BLE001
            r = {"posted": False, "reason": f"{type(e).__name__}: {str(e)[:120]}"}
        if r.get("posted"):
            LAST_APPLIED.append({"kind": "card", "what": str(d.get("header") or d.get("question") or "").strip()[:160], "ref": str(r.get("qid") or "")})
        else:
            out.append(f"The decision card didn't post ({str(r.get('reason') or '')[:140]}), so for now: I'd go with "
                       f"{str(d.get('recommended') or '').strip()}; say the word and I'll act on it.")

    # "set up a call with X" (ruling 12): zero-LLM slot finding + ONE card with the message (tracking/scheduling.py)
    call = sug.get("call")
    if isinstance(call, dict) and str(call.get("with") or "").strip() and core.can(person, "task"):
        lane = call.get("lane") if call.get("lane") in LANES else "wedding"
        try:
            sys.path.insert(0, str(core.AGENT.parent / "scripts" / "tracking"))
            import scheduling
            req = scheduling.request_for(str(call["with"]).strip(), str(call.get("channel") or ""), str(call.get("to") or ""), lane)
            r = scheduling.propose(req)
        except Exception as e:  # noqa: BLE001
            r = {"ok": False, "reason": f"{type(e).__name__}: {str(e)[:80]}"}
        if r.get("ok"):
            LAST_APPLIED.append({"kind": "task", "what": f"call times for {call['with']}"[:160], "ref": str(r.get("qid") or "")})
        else:
            out.append(f"I couldn't line up times for {call['with']}: {r.get('reason')}")

    adr = sug.get("addressed")
    if isinstance(adr, dict) and str(adr.get("text") or "").strip() and core.can(person, "teach"):
        import addressed
        to = str(adr.get("to") or "").strip()
        if to in ("agent", "group") or to.startswith("person:"):
            addressed.label_example(str(adr["text"]), to, f"{person['name']}: {adr.get('note', '')}")

    rev = sug.get("card")
    if isinstance(rev, dict) and str(rev.get("revise") or "").strip() and core.can(person, "approve"):
        out.extend(revise_card(str(rev.get("qid") or ""), str(rev["revise"]).strip(), person, ctx or {}))

    for do in _list(sug.get("do"))[:3]:
        if not (isinstance(do, dict) and str(do.get("text") or "").strip() and core.can(person, "task")):
            continue
        lane = do.get("lane") if do.get("lane") in LANES else "personal"
        try:
            tid = _queue_do(lane, str(do["text"]).strip(), person, ctx or {}, where=str(do.get("where") or ""))
        except Exception as e:  # noqa: BLE001
            tid = ""
            out.append("I couldn't start on that just now; ask me again in a minute.")
        else:
            if not tid:
                out.append("I'm already on that one.")
            else:
                LAST_APPLIED.append({"kind": "do", "what": str(do["text"]).strip()[:160], "ref": tid})
    return out

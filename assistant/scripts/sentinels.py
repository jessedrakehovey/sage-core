"""sentinels.py - zero-LLM watchdogs: stuck work, stale cards, stranded exec tickets, quiet sensors.

Ported in spirit from the property OS's deadline / input-SLA sentinels and stuck_watchdog.
Runs every operator tick. Notify-only: it writes ONE digest ticket per day (kind
stuck_work, lane personal) when something has gone quiet, latched so it never repeats the
same item inside `reping_days`. It cannot send, answer a card, or run a ticket.

  python assistant/scripts/sentinels.py [--dry-run]
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
import time
from datetime import date, datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import alib  # noqa: E402
from alib import DATA, load_json, save_json  # noqa: E402

LATCH = DATA / "sentinels_latch.json"
STAGED_STALE_DAYS = 5      # a staged ticket nobody acted on
CARD_STALE_H = 36          # an open card the owner has not answered
EXEC_STALE_H = 6           # an approved ticket still queued (quiet hours excluded by drain)
SENSOR_STALE_H = 26        # a sensor that has not reported
REPING_DAYS = 2
SPINE_FAILS = 3            # consecutive failures of one spine job
ALERT_JOBS = {"gate-health"}   # a job whose failure IS the alarm: reported on the first failure
SPINE_QUIET_MIN = 45       # the spine fires every 10 min, 06:00-23:30

def _orgcfg():
    """assistant/orgcfg.py (WP5): the organization's own values from assistant/org.json; the literal below is the fallback."""
    import sys as _sys
    from pathlib import Path as _P
    root = str(_P(__file__).resolve().parents[1])
    if root not in _sys.path:
        _sys.path.insert(0, root)
    import orgcfg
    return orgcfg


try:   # assistant/org.json -> watched_tasks / mission_control_url / phone_send_shortcut (WP5); no org.json = nothing watched
    WATCHED_TASKS = list(_orgcfg().get("watched_tasks") or [])
    _MC = str(_orgcfg().get("mission_control_url") or "")
    _SHORTCUT = str(_orgcfg().get("phone_send_shortcut") or "Send Texts")
except Exception:  # noqa: BLE001
    WATCHED_TASKS, _MC, _SHORTCUT = [], "", "Send Texts"
TASK_OK_CODES = {"0", "267009", "267011"}   # ok, running, never run (267014 "terminated" = killed by its time limit: a finding)


def _people() -> dict:
    try:
        return load_json(Path(__file__).resolve().parents[1] / "agent" / "people.json", {}) or {}
    except Exception:  # noqa: BLE001
        return {}


def _owner_keys() -> list:
    users = _people().get("users") or {}
    return [k for k, u in users.items() if str((u or {}).get("role") or "") == "owner"] or list(users)


def _first_owner() -> str:
    return next(iter(_owner_keys()), "owner")


def _owner_names() -> list:
    """The owners' first names as written in people.json (the "the owner asked:" prefix an ask carries)."""
    out = []
    for k in _owner_keys():
        n = str(((_people().get("users") or {}).get(k) or {}).get("name") or "").split(" ")[0]
        if n and n not in out:
            out.append(n)
    return out


def _owner_words() -> set:
    """The owners' keys and first names, lower-case: never a subject word, never a name in an ask."""
    out = set()
    users = _people().get("users") or {}
    for k, u in ((k, users.get(k)) for k in _owner_keys()):      # owners only: a staff member is someone an ask is about
        for w in (str(k), str((u or {}).get("name") or "").split(" ")[0]):
            w = re.sub(r"[^a-z]", "", str(w).lower())
            if w:
                out.add(w)
    return out


def _lanes() -> set:
    try:
        return {str(x).lower() for x in (alib.config().get("lanes") or [])}
    except BaseException:  # noqa: BLE001
        return set()


def _age_h(iso):
    """exec_store stamps tickets in LOCAL time (time.strftime); reading them as UTC made every ticket look
    7 h old the minute it was minted ("approved ... still queued after 7h", 2026-10-24 16:45 and 18:08)."""
    try:
        t = datetime.strptime(str(iso)[:19], "%Y-%m-%dT%H:%M:%S")
        return (datetime.now() - t).total_seconds() / 3600
    except (TypeError, ValueError):
        return 0.0


_MEMO = {"ts": 0.0, "findings": None}
MEMO_S = 120               # one sweep calls collect() once per alarm card (22 on 2026-10-26 x 6.6 s = the 120 s timeout, 56 runs in a row)


def collect(memo: bool = False):
    """The findings. `memo=True` returns the list read inside the last MEMO_S in this process: cards.resolve_alarms
    asks once per open alarm card, and each full collect runs the scheduled-task query (seconds). Default: live."""
    if memo and _MEMO["findings"] is not None and time.time() - _MEMO["ts"] < MEMO_S:
        return _MEMO["findings"]
    out = _collect()
    _MEMO.update(ts=time.time(), findings=out)
    return out


def _collect():
    findings = []
    tickets = load_json(DATA / "tickets.json", {})
    facts = _memory_facts()
    for t in tickets.values():
        if t.get("status") == "staged" and _age_h(t.get("updated")) > STAGED_STALE_DAYS * 24 and not _resolved(t) \
                and not _settled(t, tickets, facts):
            findings.append({"key": f"staged:{t['id']}", "text": f"staged {int(_age_h(t['updated']) / 24)}d, nothing since: [{t.get('lane')}] {t.get('title', '')[:80]}"})
    try:
        import cards
        for c in cards.pending():
            if c.get("held"):
                continue          # waiting for the weekly digest by design (the owner 2026-10-20); the digest's own expiry applies
            if c["age_min"] > CARD_STALE_H * 60:
                findings.append({"key": f"card:{c['qid']}", "text": f"card open {c['age_min'] / 60:.0f}h unanswered: [{c.get('lane')}] {c.get('header') or c['qid']}"})
    except Exception:
        pass
    try:
        import exec_store as es
        for t in es.pending():
            if not es.due(t):
                continue          # approved for later (not_before, 2026-10-24): waiting is the plan, not a failure
            if _age_h(t.get("ts")) > EXEC_STALE_H:
                findings.append({"key": f"exec:{t['id']}", "text": f"approved {t['kind']} {t['id']} still queued after {_age_h(t['ts']):.0f}h (drain failing?)"})
        for t in es.fold().values():
            if t.get("status") == "exception" and _age_h(t.get("ts_done")) < 24:
                findings.append({"key": f"exc:{t['id']}", "text": f"exec exception {t['kind']} {t['id']}: {str(t.get('result', ''))[:100]}"})
    except Exception:
        pass
    try:
        import phone_bridge
        import text_gate
        nums = None
        for i in phone_bridge.stale():
            if text_gate.applies(i.get("account")):
                nums = text_gate.business_numbers() if nums is None else nums
                if not text_gate.is_business(i.get("to"), nums):
                    continue                 # a text to a friend on an owner's line is not chased (2026-10-08)
            acct = str(i.get("account") or phone_bridge.DEFAULT_ACCOUNT)
            owner = phone_bridge.ACCOUNTS.get(acct, {}).get("owner") or "the owner"
            when = i["ts_handed"][11:16] if i["ts_handed"][:10] == date.today().isoformat() else f"{i['ts_handed'][5:10].replace('-', '/')} {i['ts_handed'][11:16]}"
            if phone_bridge.acks_ever(acct):
                why = "but never confirmed sent: check Messages on that phone before re-sending"
            else:
                # the Shortcut has never acked once: the text almost certainly went (the phone fetched it), the
                # confirmation step is what is missing. Said ONCE per text (once: True), with the fix on the phone.
                why = ("and the phone never confirms sends (its Shortcut has never acked one): it almost certainly went, "
                       f"so check Messages once. To stop these, on {owner}'s iPhone open Shortcuts > {_SHORTCUT} and, after "
                       f"Send Message, add a Get Contents of URL step: POST {_MC}/api/phone/ack "
                       f"with the {phone_bridge.TOKEN_HEADER} header and a JSON body id = the message id (the phone setup note, step 5)")
            findings.append({"key": f"phone:{i['id']}", "once": True,
                             "text": f"text to ...{i['to'][-4:]} was handed to {owner}'s iPhone at {when} {why} ({i['body'][:60]!r})"})
    except Exception:
        pass
    ms = load_json(DATA / "mail_status.json", {})
    for name, st in (ms.items() if isinstance(ms, dict) else []):
        if isinstance(st, dict) and st.get("ts") and _age_h(st["ts"]) > SENSOR_STALE_H:
            findings.append({"key": f"sensor:mail:{name}", "text": f"mail sensor '{name}' last reported {_age_h(st['ts']):.0f}h ago"})
    tf = load_json(DATA / "typeform_status.json", {})
    if tf:
        if tf.get("error"):
            findings.append({"key": "sensor:typeform:error", "text": f"quiz-lead sensor failing: {tf['error'][:160]} - new leads are NOT being picked up"})
        elif tf.get("last_ok") and _age_h(tf["last_ok"].replace("Z", "")) > 3:
            findings.append({"key": "sensor:typeform:quiet", "text": f"quiz-lead sensor last succeeded {_age_h(tf['last_ok'].replace('Z', '')):.0f}h ago"})
    for extra in alib.plugin_each("sentinel_findings"):        # the organization's own sensors (this organization: the CRM snapshot)
        findings.extend(x for x in (extra or []) if isinstance(x, dict) and x.get("key"))
    # conn_check.py proves each login alive hourly; a configured connection that stops working
    # is a finding (never-configured ones are known backlog and only show on Mission Control)
    cs = load_json(DATA / "connections_status.json", {})
    conns = (cs.get("connections") or {}) if isinstance(cs, dict) else {}
    # Relay goes through Plaid now: once a Plaid feed is set up, a dead SimpleFIN feed is not carded (2026-10-15)
    plaid_set_up = str((conns.get("bank-plaid") or {}).get("state") or "not_configured") != "not_configured"
    for cid, c in (conns.items() if isinstance(conns, dict) else []):
        if cid == "bank-simplefin" and plaid_set_up:
            continue
        if isinstance(c, dict) and c.get("state") == "dead":
            fix = f" Fix ({c.get('owner') or _first_owner()}): {c['renew']}" if c.get("renew") else ""
            findings.append({"key": f"conn:{cid}", "text": f"connection '{c.get('name', cid)}' is down: {str(c.get('detail', ''))[:120]}.{fix}"[:300],
                             "conn": {"id": cid, "name": str(c.get("name") or cid), "detail": str(c.get("detail") or ""),
                                      "renew": str(c.get("renew") or "")}})
    findings += _machinery()
    return findings


_CAL_SYNC = re.compile(r"calendar sync", re.I)
_CAL_SYNC_ON = re.compile(r"calendar sync (?:is on|was (?:already )?(?:done|turned on)|has been turned on)", re.I)


def _resolved(t) -> bool:
    """A staged ticket whose subject live status shows is already fixed: 'the CRM connector is
    down' while its last refresh is recent, or 'turn on calendar sync' once memory records it on.
    The 10-16 18:45 stuck card told the owner to re-auth in a terminal a connector that was live and
    to start a calendar sync that had been on since 10-14."""
    words = f"{t.get('title', '')} {(t.get('payload') or {}).get('text', '')}"
    if alib.plugin_call("resolved", words):          # the organization's own services know when their ask is stale
        return True
    if _CAL_SYNC.search(words):
        state = Path(os.environ["AGENT_STATE"]) if os.environ.get("AGENT_STATE") else Path(__file__).resolve().parents[1] / "agent" / "state"
        facts = (load_json(state / "memory.json", {}) or {}).get("facts") or []
        if any(isinstance(f, dict) and f.get("active", True) and _CAL_SYNC_ON.search(str(f.get("fact") or "")) for f in facts):
            return True
    return False


# ------------------------------------------------------------------ a week-old ask that already landed (2026-10-26)
# Three 'Waiting 5d: add Wren to Teo and Wren's the CRM project' cards went up on 10-25/26 for a change made
# and read back on 10-18 (and seen in the activity feed by the doer on 10-20), plus one for an expired date card from
# a number memory flags as a likely scam. A staged ask is now matched on its subject: the names in it and its words.
_STOP = {"the", "and", "for", "with", "his", "her", "she", "him", "they", "them", "their", "this", "that", "from", "into",
         "onto", "about", "asked", "chat", "retry", "different", "route", "please", "can", "you", "new",
         "now", "card", "ticket", "sage", "phone", "email", "com", "who", "what", "when", "then", "also", "all"} | _owner_words() | _lanes()
_NOT_NAMES = {"sage", "google", "gmail", "in", "retry", "the", "add", "send", "card",
              "date", "text", "a", "an", "on", "to"} | _owner_words() | {w + " asked" for w in _owner_words()} \
    | {str(w).lower() for w in alib.plugin_set("SERVICE_WORDS")}
_ASKED = re.compile(r"^\s*(?:" + ("|".join(re.escape(n) for n in _owner_names()) or "(?!x)x") + r")(?: asked)?:\s*", re.I)
_OUTCOME_NOTE = re.compile(r"^\s*(?:DOER_DONE|done\b|already (?:handled|done|there)|verified|checked\b)", re.I)
_DONE_FACT = re.compile(r"\b(?:was|were|been|got|is now|are now)\s+(?:\w+\s+)?(?:added|sent|booked|done|paid|signed|fixed|delivered|"
                        r"created|scheduled|set up|turned on)\b|\bread back\b|\bactivity feed shows\b", re.I)
_NOT_DONE = re.compile(r"\b(?:still not|not yet|isn't|wasn't|hasn't|failed|blocked|couldn't|could not add|waiting on)\b", re.I)
_SCAM = re.compile(r"\b(?:scam|phishing|fraud)", re.I)
_PHONE = re.compile(r"\+?1?[\s.-]*\(?\d{3}\)?[\s.-]*\d{3}[\s.-]*\d{4}")
_SKIP_KINDS = {"imessage_in", "email", "card_answer", "card_expired", "stuck_work"}


def _stem(w: str) -> str:
    w = w.lower().removesuffix("'s").removesuffix("\u2019s")
    if len(w) > 5 and w.endswith("ing"):
        w = w[:-3]
    elif len(w) > 4 and w.endswith("ed"):
        w = w[:-2]
    elif len(w) > 3 and w.endswith("s") and not w.endswith("ss"):
        w = w[:-1]
    return w


def _subject(text: str) -> tuple:
    """-> (words, names) of an ask: its content words, stemmed, and the capitalised names in it (no ids, mail, numbers)."""
    s = re.sub(r"\S+@\S+|https?://\S+|\b[0-9a-f]{12,}\b", " ", str(text or ""))
    s = _ASKED.sub("", s)
    toks = re.findall(r"[A-Za-z][A-Za-z'\u2019]+", s)
    words = {_stem(w) for w in toks if len(w) >= 3} - _STOP
    names = {_stem(w) for w in toks if w[0].isupper() and w.lower() not in _NOT_NAMES and _stem(w) not in _STOP}
    return words, names


def _same_job(a: tuple, b: tuple, need: float) -> bool:
    """Two subjects name the same job: every name of one is in the other, and their words overlap by `need`."""
    (wa, na), (wb, nb) = a, b
    if not (na and nb) or not (na <= wb or nb <= wa) or not (wa and wb):
        return False
    return len(wa & wb) / len(wa | wb) >= need


def _covers(ask: tuple, words: set) -> bool:
    """A finished outcome / memory fact covers the ask: it holds every name and nearly all words of the ask."""
    wa, na = ask
    return bool(na) and na <= words and len(wa) >= 3 and len(wa & words) / len(wa) >= 0.8


# An ask the owner closed himself (2026-10-26): 'Waiting 10d: a team member: camera 2' came back after the owner tapped Skip (10-16)
# and made a never-raise rule (10-23), and 'Waiting 6d' for the insurance payment after he said he'd handle it (10-17).
_OWNER_CLOSED = re.compile(r"\btapped (?:skip|drop)\b|\bnever (?:bring|raise|mention|chase)|\b(?:don['’]?t|do not|stop) "
                           r"(?:bring|rais|chas|follow|nudg|send the doer|ask)\w*|\bno (?:other|more|further) (?:nudges|follow-?ups|"
                           r"check-?ins)\b|\b(?:has|have) closed\b|\b(?:is|are|he['’]?s|she['’]?s|he['’]?ll|she['’]?ll|"
                           r"will) (?:\w+ ){0,3}(?:handl|check|sort|deal)\w*\b[^.]{0,80}?\b(?:himself|herself|themselves)\b", re.I)
_CLOSING_WORDS = {_stem(w) for w in ("tapped", "skip", "drop", "never", "bring", "raise", "raises", "again", "back", "unless",
                                     "card", "follow", "following", "nudges", "question", "ask", "asks", "said", "check", "handle",
                                     "handling", "himself", "herself", "don't", "stop", "closed", "chase", "other", "open", "item",
                                     "items", "list", "digest", "any", "must", "still", "until", "while", "now")}
_CALENDAR = {_stem(w) for w in ("jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "sept", "oct", "nov", "dec", "january",
                                "february", "march", "april", "june", "july", "august", "september", "october", "november",
                                "december", "monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday")}
_SKIP_TAP = re.compile(r"^\s*(?:skip|drop)\b", re.I)


def _local_stamp(iso) -> str:
    """A ticket's UTC stamp as local 'YYYY-MM-DD HH:MM', the form memory facts and rules are stamped in."""
    s = str(iso or "")
    try:
        t = datetime.strptime(s[:19], "%Y-%m-%dT%H:%M:%S")
        if s.endswith("Z"):
            t = t.replace(tzinfo=timezone.utc).astimezone()
        return t.strftime("%Y-%m-%d %H:%M")
    except ValueError:
        return s[:16].replace("T", " ")


def _rules() -> list:
    state = Path(os.environ["AGENT_STATE"]) if os.environ.get("AGENT_STATE") else Path(__file__).resolve().parents[1] / "agent" / "state"
    return [r for r in (load_json(state / "rules.json", {}) or {}).get("rules") or [] if isinstance(r, dict) and r.get("active", True)]


def _owner_closed(t: dict, facts: list, rules: list) -> str:
    """Why an owner already closed this ask ('' = he did not): a Skip / Drop tap on its own card, or, made after the
    ask, a standing rule ('never bring up ...', 'stop following up on ...') or a memory line ('tapped Skip', 'is
    handling it himself') that names the same person or thing and the same matter."""
    for h in t.get("history") or []:
        for qid in re.findall(r"\bcard (q[0-9a-f]{6,})", str((h or {}).get("note") or "")):
            try:
                import cards
                if _SKIP_TAP.search(cards.answer_of(qid)):
                    return f"the owner tapped {cards.answer_of(qid)} on its card"
            except Exception:  # noqa: BLE001
                pass
    words, _ = _subject(f"{t.get('title', '')} {t.get('summary', '')}")
    asked = _local_stamp(t.get("created"))
    for kind, items in (("memory", facts), ("a standing rule", rules)):
        for c in items:
            text = str(c.get("fact") or c.get("rule") or "")
            if not _OWNER_CLOSED.search(f"{text} {c.get('why') or ''}") or str(c.get("added") or "9") < asked:
                continue                   # not a closing line, or older than the ask (a fresh ask overrides it)
            # every name in the line (capitalised words not starting a sentence; else the fact's subject) is in the ask,
            # and so is one plain word of the matter: a Skip on 'Erin and Kit' does not close '19 cold leads'
            names = (_subject(re.sub(r"(^|[.!?:]\s+)\S+", r"\1", text))[1] - _CALENDAR) or (_subject(c.get("subject"))[1] - _CALENDAR)
            ws, caps = _subject(text)
            if names and names <= words and (ws - caps - names - _CLOSING_WORDS) & words:
                return f"the owner closed it ({kind}: {text[:80]})"
    return ""


def _settled(t: dict, db: dict, facts: list, rules: list | None = None) -> str:
    """Why a staged ask needs no card any more ('' = it does): an owner already closed it (a Skip / Drop tap, a
    never-raise rule, 'handling it himself'), a later finished ticket or an active memory fact shows the same change
    landed, or it is an expired card about a number memory flags as a scam."""
    closed = _owner_closed(t, facts, _rules() if rules is None else rules)
    if closed:
        return closed
    if t.get("kind") == "card_expired":
        nums = {re.sub(r"\D", "", m)[-10:] for m in _PHONE.findall(f"{t.get('title', '')} {t.get('summary', '')}")}
        nums.discard("")
        for f in facts:
            text = str(f.get("fact") or "")
            if nums and _SCAM.search(text) and nums & {re.sub(r"\D", "", m)[-10:] for m in _PHONE.findall(text)}:
                return "about a number Sage's memory flags as a likely scam"
        return ""
    ask = _subject(t.get("title"))
    for o in db.values():
        if o.get("id") == t.get("id") or o.get("status") not in ("done", "verified") or o.get("kind") in _SKIP_KINDS:
            continue
        if str(o.get("updated") or "") <= str(t.get("created") or ""):
            continue                       # an outcome from before the ask was made does not answer it
        note = str(((o.get("history") or [{}])[-1] or {}).get("note") or "")
        if not _OUTCOME_NOTE.search(note):
            continue                       # closed as expired / obsolete / no answer: not a done outcome
        ow, _ = _subject(f"{o.get('title', '')} {o.get('summary', '')} {note}")
        if _covers(ask, ow):
            return f"already done: {str(o.get('title') or '')[:80]}"
    for f in facts:
        text = str(f.get("fact") or "")
        if _DONE_FACT.search(text) and not _NOT_DONE.search(text):
            if _covers(ask, _subject(text)[0]):
                return f"already done per memory: {text[:80]}"
    return ""


def _memory_facts() -> list:
    state = Path(os.environ["AGENT_STATE"]) if os.environ.get("AGENT_STATE") else Path(__file__).resolve().parents[1] / "agent" / "state"
    return [f for f in (load_json(state / "memory.json", {}) or {}).get("facts") or [] if isinstance(f, dict) and f.get("active", True)]


def _close_settled() -> int:
    """Close, quietly, every staged ask that already landed (no card, no chat line). -> how many."""
    db = load_json(DATA / "tickets.json", {})
    facts = _memory_facts()
    n = 0
    for t in list(db.values()):
        if t.get("status") != "staged" or _age_h(t.get("updated")) <= STAGED_STALE_DAYS * 24:
            continue                       # only what would become a 'Waiting Nd' card
        why = _settled(t, db, facts)
        if why:
            import tickets
            if tickets._transition(str(t["id"]), "done", "sentinels", f"closed quietly, {why}", {"staged"}):
                n += 1
    return n


_TERMINAL = re.compile(r"\bpython\b|\bpowershell\b|\bschtasks\b|\bclaude -p\b|/mcp\b|\bterminal\b|\bclaude(?: code)? session\b|~/\.claude", re.I)
_DANGLING = re.compile(r"(?:[\s\-:;,(]|\b(?:see|check its log in|fix \(\w+\)|fix|run|from|in|a|re-?auth\w*|sign in again)\b)+$", re.I)


def _no_terminal(text: str) -> str:
    """A finding line without terminal or /mcp steps: the owners act from their phone, so the
    fix points at the Setup page instead (2026-10-16: 're-auth in a terminal' on a stuck card)."""
    m = _TERMINAL.search(str(text or ""))
    if not m:
        return text
    try:
        import connect
        mc = connect._mc_url()
    except Exception:  # noqa: BLE001
        mc = ""
    head = _DANGLING.sub("", text[:m.start()]) or "something needs fixing"
    return f"{head} - open Setup on Mission Control{f' ({mc}/setup)' if mc else ''}"


def _allowed(cls: str) -> bool:
    """A code-posted line of this class may go out (reflex.py: an owner's rule switches a class off by code)."""
    try:
        sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "agent"))
        import reflex
        return reflex.allowed(cls)
    except Exception:  # noqa: BLE001
        return True


def _reflex_in_hand(key: str) -> bool:
    """True while the reflex (assistant/agent/reflex.py) has this finding in hand: a fix was applied inside the last hour
    and is being verified. A fault here never hides a finding."""
    try:
        sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "agent"))
        import reflex
        return reflex.in_hand(key)
    except Exception:  # noqa: BLE001
        return False


def _machinery():
    """The machinery watching itself: spine jobs that keep failing, a spine that stopped
    firing, and scheduled tasks whose last run failed. Without this a broken job only shows
    up in a log nobody reads (the heartbeat job failed 13 runs unnoticed, 2026-10-01)."""
    out = []
    sp = load_json(DATA / "spine_state.json", {})
    newest = None
    for name, st in (sp.items() if isinstance(sp, dict) else []):
        if not isinstance(st, dict):
            continue
        if int(st.get("fails", 0) or 0) >= (1 if name in ALERT_JOBS else SPINE_FAILS):
            if _reflex_in_hand(f"job:{name}"):
                continue          # the reflex is fixing it by code this hour; the owner hears only if that fails (rule: restart quietly)
            out.append({"key": f"spine:{name}", "text": f"spine job '{name}' has failed {st['fails']} runs in a row: {str(st.get('result', ''))[:120]}"})
        try:
            t = datetime.strptime(str(st.get("last"))[:19], "%Y-%m-%dT%H:%M:%S")   # spine stamps local time
            newest = t if newest is None or t > newest else newest
        except ValueError:
            pass
    if os.environ.get("ASSISTANT_DATA"):
        return out          # scratch/test mode: never judge the real machine
    now = datetime.now()
    if newest and 6.75 <= now.hour + now.minute / 60 < 23.5 and (now - newest).total_seconds() > SPINE_QUIET_MIN * 60:
        out.append({"key": "spine:stalled", "text": f"the Day Spine has not run a job since {newest:%m-%d %H:%M} - approved sends, card sweeps and the lead sensor are stopped"})
    out += _task_failures()
    out += _dreaming_health()
    out += _selftest_fails()
    return out


def _dreaming_health():
    """Dreaming is launched through a fire-and-forget wrapper, so its scheduled task always
    reports exit 0: read its own log instead. It sat disabled for three months (2026-06-23 to
    09-30) with nothing saying so."""
    log = Path.home() / ".claude" / "dreaming-wv.log"
    try:
        lines = log.read_text(encoding="utf-8", errors="replace").splitlines()[-400:]
    except OSError:
        return []
    start = next((i for i in range(len(lines) - 1, -1, -1) if "=== dreaming start ===" in lines[i]), None)
    if start is None:
        return []
    out = []
    try:
        age_h = (datetime.now() - datetime.strptime(lines[start][:19], "%Y-%m-%d %H:%M:%S")).total_seconds() / 3600
    except ValueError:
        age_h = 0
    if age_h > 27:
        out.append({"key": "dreaming:quiet", "text": f"dreaming (nightly memory consolidation) last ran {age_h:.0f}h ago - is its task disabled?"})
    # only the cron's own stamped lines count (model OUTPUT and test dumps are continuation lines)
    tail = [ln for ln in lines[start:] if re.match(r"\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}  ", ln)]
    bad = next((ln for ln in tail if ln[21:].startswith("ERROR:") or ("dreaming done (exit" in ln and "(exit 0" not in ln)), "")
    if bad:
        out.append({"key": "dreaming:failed:" + lines[start][:10], "text": f"dreaming's last run failed: {bad[21:160]}"})
    elif age_h > 2 and not any("dreaming done (exit" in ln for ln in tail):
        # killed by its time limit or hung: it never wrote a "done" line and never will
        out.append({"key": "dreaming:unfinished:" + lines[start][:10], "text": f"dreaming started {age_h:.0f}h ago and never finished (killed or hung) - see ~/.claude/dreaming-wv.log"})
    return out


def _selftest_fails():
    """The nightly self-test (dream_cron.ps1) records each suite in the eval ledger; a suite whose
    latest verdict is FAIL is a finding. Covers the send-gate tripwire (check_gates)."""
    led = Path(__file__).resolve().parents[2] / "scripts" / "evals" / "eval_ledger.jsonl"
    last = {}
    try:
        for ln in led.read_text(encoding="utf-8-sig", errors="replace").splitlines():
            try:
                r = json.loads(ln)
            except ValueError:
                continue
            if isinstance(r, dict) and str(r.get("pipeline", "")).startswith("selftest-"):
                last[r["pipeline"]] = r
    except OSError:
        return []
    return [{"key": f"selftest:{p}:{str(r.get('ts') or r.get('date') or '')[:10]}",
             "text": f"nightly self-test {p[9:]} FAILED: {str(r.get('note') or '')[:120]}"}
            for p, r in sorted(last.items()) if str(r.get("verdict", "")).upper() == "FAIL"]


TASK_CACHE = DATA / "task_status_cache.json"
TASK_CACHE_S = 600         # the PowerShell task query takes 2-6 s; its answer is good for ten minutes


def _task_failures():
    """Enabled scheduled tasks on this side whose last run exited non-zero. The query's lines are cached on disk
    for TASK_CACHE_S (2026-10-27: every sweep paid for it several times over and timed out)."""
    cache = load_json(TASK_CACHE, {}) or {}
    lines = None
    if isinstance(cache, dict) and time.time() - float(cache.get("ts") or 0) < TASK_CACHE_S and isinstance(cache.get("lines"), list) \
            and cache.get("tasks") == list(WATCHED_TASKS):
        lines = cache["lines"]
    if lines is None:
        ps = ("$n=@(" + ",".join("'" + t + "'" for t in WATCHED_TASKS) + ");"
              "foreach($t in $n){$x=Get-ScheduledTask -TaskName $t -ErrorAction SilentlyContinue;"
              "if(-not $x){\"$t|MISSING|0\";continue};$i=$x|Get-ScheduledTaskInfo;\"$t|$($x.State)|$($i.LastTaskResult)\"}")
        try:
            r = subprocess.run(["powershell", "-NoProfile", "-Command", ps], capture_output=True, text=True, timeout=40,
                               creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        except (OSError, subprocess.TimeoutExpired):
            return []
        lines = (r.stdout or "").splitlines()
        save_json(TASK_CACHE, {"ts": time.time(), "tasks": list(WATCHED_TASKS), "lines": lines})
    out = []
    for line in lines:
        parts = line.strip().split("|")
        if len(parts) != 3:
            continue
        name, state, code = parts
        if state == "MISSING":
            out.append({"key": f"task:{name}:missing", "text": f"scheduled task '{name}' is not registered"})
        elif state != "Disabled" and code not in TASK_OK_CODES:
            out.append({"key": f"task:{name}:{code}", "text": f"scheduled task '{name}' failed its last run (exit {code}) - check its log in ~/.claude/"})
    return out


def _on_open_card():
    """Finding keys an earlier stuck-work digest still covers: its ticket is new/claimed (the
    operator is about to post it) or staged with its card still open, in any lane. Also the
    keys 'card:<qid>' of those digest cards themselves. The same four items were posted twice,
    30 min apart, while the first digest card was still open (2026-10-11)."""
    try:
        import cards
        live = {c["qid"] for c in cards.pending()}
    except Exception:
        return set()
    keys = set()
    for t in load_json(DATA / "tickets.json", {}).values():
        if t.get("kind") != "stuck_work" or t.get("status") not in ("new", "claimed", "staged"):
            continue
        if t["status"] == "staged":
            qids = [m.group(1) for h in t.get("history") or [] for m in [re.search(r"\bcard (q[0-9a-f]{6,})", str(h.get("note") or ""))] if m]
            if not qids or qids[-1] not in live:
                continue                  # its card was answered or expired: not covered any more
            keys.add(f"card:{qids[-1]}")
        keys |= {str(i.get("key")) for i in (t.get("payload") or {}).get("items") or [] if isinstance(i, dict)}
    return keys


_CHASE_PREFIXES = ("card:", "staged:", "phone:")


def _is_chase(f) -> bool:
    return str(f.get("key") or "").startswith(_CHASE_PREFIXES) and not f.get("conn")


def _local_day(iso) -> str:
    """The local calendar day of a ticket timestamp (tickets stamp UTC with a Z; 21:00 here is tomorrow in UTC)."""
    s = str(iso or "")
    try:
        t = datetime.strptime(s[:19], "%Y-%m-%dT%H:%M:%S")
        if s.endswith("Z") or len(s) == 19:
            t = t.replace(tzinfo=timezone.utc).astimezone()
        return t.date().isoformat()
    except ValueError:
        return s[:10]


def _chase_digest_today() -> bool:
    """A stuck_work ticket made today (local day) that holds only chase items."""
    today = date.today().isoformat()
    for t in load_json(DATA / "tickets.json", {}).values():
        if t.get("kind") != "stuck_work" or t.get("source") != "sentinel" or _local_day(t.get("created")) != today:
            continue
        items = (t.get("payload") or {}).get("items") or []
        if items and all(_is_chase(i) for i in items if isinstance(i, dict)):
            return True
    return False


# ------------------------------------------------------------------ findings become cards by code (WP2, 2026-10-24)
_SENSOR_CMDS = {"typeform": ["python", "assistant/scripts/sense_typeform.py"],
                "mail": ["python", "assistant/scripts/mail.py", "sense"], **alib.plugin_map("SENSOR_CMDS")}
# the spine and the dreaming task by their names in org.json -> watched_tasks
_SPINE_TASK = next((t for t in WATCHED_TASKS if "Day Spine (assistant)" in t), "")
_DREAM_TASK = next((t for t in WATCHED_TASKS if "Dreaming" in t), "")


def _plan(f: dict) -> tuple:
    """-> (header, options, act) for a finding, by its key: every first button is something code DOES on the tap
    (the button grammar), the last is Leave it. '' header = no card for this kind (chase items ride the daily line)."""
    key = str(f.get("key") or "")
    leave = {"label": "Leave it", "description": "latched for 5 days"}
    if key.startswith("spine:stalled"):
        return "Day Spine stopped", [{"label": "Start it now (Recommended)", "description": "Start-ScheduledTask on the spine"}, leave], {"task": _SPINE_TASK}
    if key.startswith("spine:"):
        name = key.split(":", 1)[1]
        return f"Spine job {name} failing", [{"label": "Run it now (Recommended)", "description": f"spine.py --force {name}"}, leave], {"spine_job": name}
    if key.startswith("task:"):
        name = key.split(":")[1]
        return f"Task {name[:40]} failed", [{"label": "Start it now (Recommended)", "description": "Start-ScheduledTask"}, leave], {"task": name}
    if key.startswith("exec:"):
        tid = key.split(":", 1)[1]
        return "Approved item not run", [{"label": "Run the drain now (Recommended)", "description": "drain.py this minute"},
                                         {"label": "Cancel it", "description": "the approved ticket is cancelled"}, leave], {"drain": True, "ticket": tid}
    if key.startswith("exc:"):
        return "An action failed", [{"label": "Hand it to Sage (Recommended)", "description": "an operator ticket; it comes back here"}, leave], {}
    if key.startswith("sensor:"):
        which = key.split(":")[1]
        cmd = _SENSOR_CMDS.get(which)
        if cmd:
            return f"{which.capitalize()} sensor quiet", [{"label": "Run the sensor now (Recommended)", "description": " ".join(cmd[1:])}, leave], {"cmd": cmd}
        return f"{which.capitalize()} sensor quiet", [{"label": "Hand it to Sage (Recommended)", "description": "an operator ticket"}, leave], {}
    if key.startswith("dreaming:"):
        return "Dreaming did not run", [{"label": "Run it now (Recommended)", "description": "Start-ScheduledTask on the dreaming task"}, leave], {"task": _DREAM_TASK}
    if key.startswith("selftest:"):
        return "A nightly self-test failed", [{"label": "Hand it to Sage (Recommended)", "description": "an operator ticket; it comes back here"}, leave], {}
    return "", [], {}


def _staged_card(f: dict, same: list | None = None) -> dict:
    """A ticket staged for days with nothing since, as ONE held card (not urgent: it waits for the Monday digest) with
    buttons that act: Sage works it, or it closes. The finding key is the probe, so the card clears itself once the
    ticket moves, and DUPLICATE_ALARM keeps it to one card per ticket. `same`: the other tickets asking for the same
    job in other words (2026-10-26), carried on this one card and closed with it."""
    tid = str(f.get("key") or "").split(":", 1)[1]
    t = load_json(DATA / "tickets.json", {}).get(tid) or {}
    title = str(t.get("title") or f.get("text") or "")[:120]
    days = int(_age_h(t.get("updated")) / 24) if t.get("updated") else STAGED_STALE_DAYS
    times = f" (asked {len(same) + 1} times)" if same else ""
    q = {"header": f"Waiting {days}d: {title}"[:60], "lane": str(t.get("lane") or "personal"), "subject": str(f["key"]),
         "question": f"This has waited {days} days with nothing since: {title}{times}.",
         "options": [{"label": "Hand it to Sage (Recommended)", "description": "an operator ticket; what needs you comes back as a card"},
                     {"label": "Close it", "description": "the item is closed; nothing goes out"}],
         "clears_when": {"sentinel": str(f["key"])}, "sentinel_key": str(f["key"]), "sentinel_act": {"close": [tid] + list(same or [])}}
    try:
        import cards
        return cards.post([q], "sentinels", lane=q["lane"])
    except Exception as e:  # noqa: BLE001   a card fault falls back to the daily line
        return {"posted": False, "reason": f"{type(e).__name__}: {str(e)[:120]}"}


def act(a: dict, by: str = "owner") -> str:
    """Carry a sentinel card's button out. -> one plain line for chat."""
    a = a or {}
    if a.get("spine_job"):
        r = subprocess.run([sys.executable, str(Path(__file__).resolve().parent / "spine.py"), "--force", str(a["spine_job"])],
                           capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=600, cwd=str(Path(__file__).resolve().parents[2]))
        last = ((r.stdout or "") + (r.stderr or "")).strip().splitlines()[-1:] or [""]
        return f"Ran {a['spine_job']}: {last[0][:160]}"
    if a.get("task"):
        r = subprocess.run(["powershell", "-NoProfile", "-Command", f"Start-ScheduledTask -TaskName '{a['task']}'"], capture_output=True, text=True, timeout=60)
        return f"Started '{a['task']}'." if r.returncode == 0 else f"Could not start '{a['task']}': {(r.stderr or r.stdout or '')[:120]}"
    if a.get("drain"):
        r = subprocess.run([sys.executable, str(Path(__file__).resolve().parent / "drain.py")], capture_output=True, text=True, encoding="utf-8",
                           errors="replace", timeout=600, cwd=str(Path(__file__).resolve().parents[2]))
        return "Drain ran: " + (((r.stdout or "").strip().splitlines() or [""])[-1][:160])
    if a.get("close"):
        import tickets
        ids = a["close"] if isinstance(a["close"], list) else [a["close"]]
        ok = [tickets._transition(str(i), "done", "sentinels", f"closed from its weekly card by {by}", {"staged", "new"}) for i in ids]
        return "Closed it." if any(ok) else "That one was already closed."
    if a.get("handoff"):
        # a weekly card's first button (a connector sensor's weekly card, the staged-item cards): the work goes to Sage as
        # an operator ticket, one per card
        import tickets
        h = dict(a["handoff"])
        text = str(h.get("text") or h.get("title") or "")
        n = tickets.add_many([{"source": str(h.get("source") or "sentinel-card"), "source_id": str(h.get("source_id") or text[:60]),
                               "lane": str(h.get("lane") or "personal"), "kind": str(h.get("kind") or "owner1_request"),
                               "title": str(h.get("title") or text)[:200], "summary": text[:600],
                               "payload": {"text": text, "from": by, "via": "card"}}], "sentinels")
        return "Okay, Sage is on it; anything that needs you comes back as its own card." if n else "Sage is already on that one."
    if a.get("cmd"):
        cmd = [sys.executable if c == "python" else c for c in a["cmd"]]
        r = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=600, cwd=str(Path(__file__).resolve().parents[2]))
        return "Sensor ran: " + ((((r.stdout or "") + (r.stderr or "")).strip().splitlines() or [""])[-1][:160])
    return "Nothing to run for that one."


def act_cancel(ticket_id: str, by: str = "owner") -> str:
    try:
        import exec_store as es
        es.set_status(ticket_id, "cancelled", f"cancelled from the sentinel card by {by}")
        return f"Cancelled {ticket_id}."
    except Exception as e:  # noqa: BLE001
        return f"Could not cancel {ticket_id}: {str(e)[:120]}"


def _chase_line_today(latch: dict) -> bool:
    return str(latch.get("chase-line:day") or "") == date.today().isoformat()


def _same_job_groups(found: list) -> dict:
    """Staged findings that name the same job (the same couple and the same change, in other words) -> {key of the
    oldest ask: [the other ticket ids]}; a finding alone is its own group. Grouped over every staged finding, not only
    the fresh ones, so a re-pinged duplicate lands on the card its oldest sibling already has."""
    db = load_json(DATA / "tickets.json", {})
    staged = sorted((db.get(f["key"].split(":", 1)[1]) or {"id": f["key"].split(":", 1)[1]} for f in found
                     if str(f.get("key")).startswith("staged:")), key=lambda t: str(t.get("created") or ""))
    subj = {t["id"]: _subject(t.get("title")) for t in staged}
    groups: list = []
    for t in staged:
        g = next((g for g in groups if any(_same_job(subj[t["id"]], subj[o], 0.6) for o in g)), None)
        (g.append(t["id"]) if g is not None else groups.append([t["id"]]))
    return {f"staged:{g[0]}": g[1:] for g in groups}


def run(dry=False):
    latch = load_json(LATCH, {})
    now = time.time()
    if not dry:
        _close_settled()            # an ask that already landed closes quietly: no card, no line (2026-10-26)
    covered = _on_open_card()
    found = collect()
    # `once`: said one time, never re-pinged (a text the phone never confirms is not news every two days)
    fresh = [f for f in found if f["key"] not in covered
             and now - float(latch.get(f["key"], 0)) > REPING_DAYS * 86400
             and not (f.get("once") and latch.get(f["key"]))]
    on_card = sum(1 for f in found if f["key"] in covered)
    for f in fresh:
        f["text"] = _no_terminal(f["text"])
    if not fresh:
        return {"stuck": 0, "pinged": 0, "on_card": on_card}
    if dry:
        for f in fresh:
            print("WOULD PING", f["text"])
        return {"stuck": len(fresh), "pinged": 0, "on_card": on_card}
    import cards
    # Chase findings (an open card, a staged ticket, an unconfirmed text) are ONE plain line a day in Sage's voice
    # (WP2; the stuck_work ticket is gone: on 2026-10-23 the operator relisted the same items four times).
    chase = [f for f in fresh if _is_chase(f)]
    # A ticket staged for days is a held card that rides the Monday approvals digest, never a mid-week line
    # (2026-10-20 14:46: "three things have sat for a week ... say 'nudge the cold leads'").
    # Several asks for the same job ride ONE card, on the oldest ask (three Wren cards on 10-25/26).
    groups = _same_job_groups(found)
    for f in [f for f in chase if str(f.get("key")).startswith("staged:")]:
        head = next((k for k, rest in groups.items() if k == f["key"] or f["key"].split(":", 1)[1] in rest), f["key"])
        if head != f["key"] and float(latch.get(head, 0)) > now:
            latch[f["key"]] = now        # the owner said Leave it on that job's card: its rewordings wait with it
            chase.remove(f)
            fresh.remove(f)
            continue
        r = _staged_card(dict(f, key=head), groups.get(head) or [])
        if r.get("posted") or "DUPLICATE_ALARM" in str(r.get("reason") or ""):
            latch[f["key"]] = now
            chase.remove(f)
            fresh.remove(f)
    if chase:
        if not _chase_line_today(latch) and _allowed("chase_line"):
            cards.say("sentinels", "personal", "Still waiting on you: " + "; ".join(f["text"][:140] for f in chase[:6])
                      + (f"; and {len(chase) - 6} more" if len(chase) > 6 else "") + ".")
            latch["chase-line:day"] = date.today().isoformat()
        for f in chase:
            latch[f["key"]] = now
        fresh = [f for f in fresh if not _is_chase(f)]
    if not fresh:
        save_json(LATCH, latch)
        return {"stuck": len(chase), "pinged": 0, "on_card": on_card, "folded": len(chase)}
    import tickets
    # a dead connection is carded BY CODE, with buttons that act (cards.connection_card): the operator
    # used to write "Bank feed down: I'll reconnect it (Recommended)" from the digest, three times in two
    # days (the owner 2026-10-17). DUPLICATE_ALARM keeps it to one open card per connection.
    carded = 0
    for f in [f for f in fresh if f.get("conn")]:
        if _conn_card(f["conn"]):
            carded += 1
        latch[f["key"]] = now
    fresh = [f for f in fresh if not f.get("conn")]
    if not fresh:
        save_json(LATCH, latch)
        return {"stuck": carded, "pinged": 0, "on_card": on_card, "conn_cards": carded}
    # Every other finding (a failing spine job, a stalled spine, a quiet sensor, an approved item not run, a failed
    # task, dreaming, a self-test) is ONE card by code with a button that acts and a probe that clears it (WP2).
    # Nothing waits for an LLM run between a failure and the owner any more.
    posted, rest = 0, []
    for f in fresh:
        header, options, a = _plan(f)
        if not header:
            rest.append(f)
            continue
        r = cards.sentinel_card({**f, "header": header, "act": a}, options)
        if r.get("posted"):
            posted += 1
            latch[f["key"]] = now
        elif "DUPLICATE_ALARM" in str(r.get("reason") or ""):
            latch[f["key"]] = now
        else:
            rest.append(f)
    added = 0
    if rest:
        # a finding with no card shape, or whose card the guards refused: the old digest ticket, so nothing is lost
        sig = hashlib.sha1(",".join(sorted(f["key"] for f in rest)).encode()).hexdigest()[:8]
        added = tickets.add_many([{"source": "sentinel", "source_id": f"{date.today().isoformat()}|{sig}", "lane": "personal",
                                   "kind": "stuck_work", "title": f"{len(rest)} stuck item(s) need a look",
                                   "summary": "; ".join(f["text"] for f in rest)[:600],
                                   "payload": {"items": rest}}], "sentinels")
        if added is not None and added >= 0:
            for f in rest:
                latch[f["key"]] = now
    save_json(LATCH, latch)
    return {"stuck": len(fresh) + carded + len(chase), "pinged": max(added or 0, 0), "cards": posted, "on_card": on_card, "conn_cards": carded, "folded": len(chase)}


def _plain(detail: str) -> str:
    """A connection's fault detail without the command it suggests or any long id (owners never get terminal steps)."""
    d = re.split(r"\s*(?:-\s*)?(?:sign in again:\s*)?\bpython\b", str(detail or ""), maxsplit=1)[0]
    d = re.sub(r"\b(?=[A-Za-z0-9_-]*\d)[A-Za-z0-9_-]{20,}\b", "the account", d)
    return d.strip(" -:;,") or "it stopped answering"


def _conn_card(conn: dict) -> bool:
    """ONE alarm card per dead connection, by code, with buttons that act; the sweep carries the
    answer out (cards._act_on_setup). Refused as DUPLICATE_ALARM while one is open -> False."""
    try:
        import cards
        how = ""
        try:
            import setup_actions
            reg = next((r for r in setup_actions._registry() if r.get("id") == conn["id"]), None)
            how = str((setup_actions.recipe(reg) or {}).get("how") or "") if reg else ""
        except Exception:  # noqa: BLE001
            how = ""
        detail, renew = conn["detail"], conn["renew"]
        # a connection with a chat setup (connect.py) gets chat words, never a command line or an item id:
        # the Plaid card said "re-link it from a terminal: python ... plaid.py setup" (the owner 2026-10-17)
        try:
            import connect
            ch = ((connect.row(conn["id"]) or {}).get("setup") or {}).get("chat")
        except Exception:  # noqa: BLE001
            ch = None
        if isinstance(ch, dict) and ch.get("steps"):
            say = str(ch.get("say") or f"set up {conn['name']}")
            renew, how = f"Say '{say}' in chat and I'll walk you through setting it back up from your phone.", ""
            detail = _plain(detail)
        if conn["id"] == "bank-simplefin":
            # point at the Setup page's Bank feed button, never a terminal or a command (2026-10-15)
            try:
                import connect
                mc = connect._mc_url()
            except Exception:  # noqa: BLE001
                mc = ""
            renew, how = f"Open Setup on Mission Control{f' ({mc}/setup)' if mc else ''} and tap the Bank feed button; it works from your phone.", ""
            detail = _plain(detail)
        q = cards.connection_card(conn["id"], conn["name"], detail, renew, how)
        r = cards.post([q], "sentinels", lane=q["lane"])
        return bool(r.get("posted"))
    except Exception:  # noqa: BLE001
        return False


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args()
    r = run(a.dry_run)
    print("SENTINELS " + " ".join(f"{k}={v}" for k, v in r.items()))

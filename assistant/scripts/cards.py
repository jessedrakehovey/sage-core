"""cards.py - question cards: the approval surface of the personal OS.

Ported from the property OS's question_bridge (ask_card.py contract), file-based so the
Phase 4 Mission Control server and the phone UI can serve the same store. A card is for
a DECISION; everything else is a note. Nothing here sends: an Approve on an exec card
mints a sha-bound executor ticket (exec_store.enqueue, approval_ref card:<qid>) and
drain.py runs it through a registered script.

Store (assistant/data/cards/):
  live/q*.json      open cards            answers.jsonl   every tap, last entry wins
  superseded/       consumed/expired      archive/        full text of every card, never deleted
  notes.jsonl       lane notes (no decision needed)

  cards.py post --file card.json --owner <lane|session> [--expires 21600]   -> {"qid":...}
  cards.py note --owner <lane> "<what you did>"
  cards.py say --lane <lane> [--thread T] "<one plain line>"   Sage says it in chat (not a card)
  cards.py pending [--json]                 open cards, oldest first
  cards.py answer <qid> --option "<label>" [--text "..."] [--revised-file f]   (the owner only)
  cards.py undo <qid>                       take an answer back until it has taken effect (cancel / recall its ticket)
  cards.py harvest --owner <lane> [--json]  answered cards -> JSON, retired once
  cards.py expire                           tombstone cards past their expiry
  cards.py reopen <qid> --why "..."         a consumed card whose action was NOT confirmed done comes back (same card)

Delivery (the owner 2026-10-20, config.json -> cards): with delivery "weekly" every card is HELD - open on
Mission Control and in the agent's transcript at once, in Chat only on digest day, when digest.py posts
ONE collapsible message with every held card and its buttons - unless it is URGENT ("urgent": true, a
header starting with an urgent prefix such as "New lead", a card asked for in a chat thread), which
goes to Chat the minute it is posted, on its own. A decision on a digest card is final: nothing re-asks
it; an unanswered one rides along carry_over_weeks more digests, then is dropped. A card whose approved
action provably failed comes back by itself (reopen, from drain) - "unless they are not confirmed done".

card.json = a list of up to 4 questions:
  [{"question": "full text the owner reads", "header": "short chip", "lane": "wedding",
    "details": "evidence, folded shut", "audience": "client|vendor|personal",
    "last_inbound_ts": "...", "last_outbound_ts": "...", "freshness_ack": "...",
    "options": [{"label": "Approve (Recommended)", "description": "..."}, {"label": "Skip"}],
    "exec": {"kind": "email_send", "payload": {...}, "approve_label": "Approve (Recommended)"}}]

Guards at post time (same rules as the property OS, adapted):
  RULING            rulings.py matches -> printed; post the card pre-decided (never blocks)
  FRESHNESS         outbound newer than inbound, or 3rd card to one recipient in 24 h -> refused
                    unless freshness_ack
  NO_RECOMMENDATION a judgment card with 2+ options must mark one "(Recommended)"
  NOT_A_DECISION    one option ("Got it") is not a decision -> say it in chat (cards.py say)
  NEEDS_ACTION      every button is something the SYSTEM does, or a Skip/Drop; a card whose
                    only real button is the owner's own work ("I'll reconnect it") is refused
                    (the owner 2026-10-17): say it in chat instead
  TEXT_NEEDS_DRAFT  a reply card ("Text reply: X", "Reply to X") carries the draft (exec +
                    [TEXT PREVIEW]); options-as-drafts are refused (OPTIONS_ARE_DRAFTS too)
  DUPLICATE_SUBJECT a decision card whose header is already open in that lane -> refused
  ALREADY_ANSWERED  the same header was answered in the last 20 h -> refused with the answer
  EXEC_VERBATIM     an exec card's payload text must sit verbatim in the question
  EXEC_KEYS         exec payload validated by exec_store before the owner can tap
  CARD_TOO_LONG     warning only: explanation prose over 350 chars
  DUPLICATE_ALARM   an alarm card ("clears_when") while one for the same check is open -> refused
  ENTITY            decision_keys.py: the same person / lead under any header (open card, a Drop
                    still standing, a nudge right after a send) and a reply draft already open or
                    already sent to that recipient -> DUPLICATE_SUBJECT / ALREADY_ANSWERED

Alarm cards (2026-10-06): an infrastructure alarm ("the CRM connector down", "Sage bridge
down") carries "clears_when": {"connection": "<id from assistant/connections.json>"}. The
sweep retires it as "resolved" (and edits the Chat message to say so) once conn_check has
proven THAT connection live after the card was posted - the same check that would raise the
alarm, never a proxy. {"disk": "C:", "min_free_gb": 5} does the same for free space.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import crm  # noqa: E402
import exec_store as es  # noqa: E402
import rulings  # noqa: E402

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

HERE = Path(__file__).resolve().parent
ROOT = Path(os.environ.get("ASSISTANT_CARDS") or (HERE.parent / "data" / "cards"))
LIVE, DONE, ARCHIVE = ROOT / "live", ROOT / "superseded", ROOT / "archive"
ANSWERS, NOTES = ROOT / "answers.jsonl", ROOT / "notes.jsonl"
SPILL = ROOT / "unqueued_answers.json"   # card answers whose ticket could not be queued yet
UNDO_S = 30                           # the grace before anything acts on a tap (harvest, drain floor)
UNDO_DECISION_MAX_S = 24 * 3600      # a decision (no send) can still be taken back this long after the tap
EXPLANATION_MAX = 350
DEFAULT_EXPIRES_S = 6 * 3600          # the value that means "the caller did not choose": post() picks by card type
# How long a card stays answerable on the owner's phone when the poster did not choose. Six hours
# (a number ported from a system with 140 cards a day) meant a card posted at 07:15 was gone by
# 13:15, and one posted in the evening expired overnight - before he had looked. This OS posts a
# handful a day: a decision stays until answered, up to two days (the sentinel names it at 36 h);
# a card that SENDS something on approve stays one day, so a stale message is not approved late.
DECISION_EXPIRES_S = 48 * 3600
EXEC_EXPIRES_S = 24 * 3600
REPEAT_WINDOW_S, REPEAT_LIMIT = 24 * 3600, 3
_EXEC_TEXT_KEYS = ("body", "text", "note", "description")
_EMAIL = re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+")
# Button grammar (the owner 2026-10-17 evening: "Approvals should always be actions that you take. If you
# need me to do something, it's not an approval card"). A PASSIVE label is the owner reporting or
# promising his own work; a NO-ACTION label declines. A decision card needs at least one button
# that is neither: something the system does on the tap.
_PASSIVE = re.compile(r"^\s*(i['\u2019]?ll\b|i will\b|i['\u2019]?m (on it|going to|handling)|on it\b|noted\b|got it\b|ok(ay)?\b|"
                      r"understood\b|wait\b|leave (it|them|that)\b|let me\b|yes,? i['\u2019]?ll\b|handled\b|already\b|done\b|"
                      r"i did\b|i sent\b|sent it\b|i['\u2019]?ve\b|will do\b|sure\b|it went out\b|i['\u2019]?m on\b)", re.I)
_NO_ACTION_OPT = re.compile(r"^\s*(skip|drop|leave|not now|no\b|ignore|dismiss|later|remind me|never mind|nothing|not a real|cancel)", re.I)
_REPLY_HEADER = re.compile(r"^\s*(text reply|reply to|reply in|text to|text|reply|email reply|email to|message to|respond)\b", re.I)
REASK_WINDOW_S = 20 * 3600

# ------------------------------------------------------------------ delivery: the weekly digest + urgent cards
# the owner 2026-10-20: "I'm getting overwhelmed with them ... I only want a drop down long list of cards
# once a week and making a decision on those should be the final close to each unless they are not
# confirmed done. I need to have an approval to text new prospects ASAP when a new one comes in with
# phone number. Normal approvals will be singular cards from now on besides one drop down card with
# them all together once a week." So a card still enters the store the moment it is posted (open on
# Mission Control, in the agent's transcript, every guard above applied) but reaches Chat only in the
# weekly digest (digest.py: ONE collapsible message, every held card inside, each with its own
# buttons) - unless it is URGENT, which goes to Chat at once, on its own, never stacked. Config:
# assistant/config.json -> cards. A decision on a digest card is final: nothing re-asks it; a card
# whose approved action provably did not happen comes back by itself (reopen, called by drain).
DIGEST_HOLD_MAX_S = 21 * 86400      # a held card the digest never showed (the spine was down) still dies
REOPEN_MAX = 2                      # a card comes back at most this many times after a failed action
CHAT_ASK_H = 48                     # a ticket staged "asked in chat" closes after this long (the reply or the silence is the answer)
_URGENT_DEFAULT_PREFIXES = ("New lead", "Text not confirmed", "Text unconfirmed")

# A card about the machinery, not about the owners' life or business (the owner 2026-10-27: "Sage still sends me lots
# of system noise ... interacting with Sage should feel exactly like interacting with a real person"): a sentinel
# finding, an outcome the ledger could not confirm, a connection down, a channel paused, a failed self-test, a
# spine job failing. Whatever its poster said, it is NEVER urgent and never in the weekly digest: delivery "mc",
# open on Mission Control and in the agent's transcript, worked by the reflex (reflex.d_system_cards), which says
# one plain line in Chat only when an owner has to act. The owner's own decisions (a send, a reply, a build that
# needs his OK, a stalled ask "Waiting Nd", a lead's next step) are not system cards.
_SYSTEM_HDR = re.compile(r"^(?:Not confirmed|Did this text go|Did not go|An action failed|Approved item not run|A nightly self-test"
                         r"|Spine job |Day Spine|Dreaming did not run|Mission Control|Task .{0,40} failed|Sage \(the household agent\))"
                         r"|(?:sensor quiet| down| paused| failing| stopped)$", re.I)


def is_system(q) -> bool:
    if not isinstance(q, dict):
        return False
    hdr = str(q.get("header") or "").strip()
    if hdr.lower().startswith("waiting ") or q.get("hold_text") or q.get("lead_text"):
        return False
    if q.get("outcome_id") or q.get("setup_rid") or q.get("breaker_lane") or q.get("sentinel_key"):
        return True
    return bool(_SYSTEM_HDR.search(hdr))


def system_to_chat() -> bool:
    """True only when an owner flipped cards.system_to_chat on (or a test pins the old behaviour)."""
    e = os.environ.get("ASSISTANT_CARDS_SYSTEM")
    if e is not None:
        return e.strip().lower() in ("1", "on", "chat", "true")
    try:
        from alib import config
        return bool((config().get("cards") or {}).get("system_to_chat"))
    except Exception:  # noqa: BLE001
        return False
_DAYS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")


def delivery_policy() -> dict:
    """{"delivery": "weekly"|"now", "day": 0-6, "hour", "prefixes", "max_per_message", "carry_over_weeks",
    "say_empty", "when"}. Env ASSISTANT_CARDS_DELIVERY wins (tests pin the old behaviour)."""
    c = {}
    try:
        from alib import config
        c = dict(config().get("cards") or {})
    except Exception:  # noqa: BLE001
        c = {}
    mode = str(os.environ.get("ASSISTANT_CARDS_DELIVERY") or c.get("delivery") or "now").strip().lower()
    mode = "weekly" if mode == "weekly" else "now"
    day = str(c.get("digest_day") or "mon").strip().lower()[:3]
    day_i = _DAYS.index(day) if day in _DAYS else 0
    try:
        hour = min(23, max(0, int(c.get("digest_hour", 8))))
    except (TypeError, ValueError):
        hour = 8
    pref = c.get("urgent_header_prefixes")
    pref = tuple(str(x) for x in pref if str(x).strip()) if isinstance(pref, list) else _URGENT_DEFAULT_PREFIXES
    try:
        per = max(2, int(c.get("max_per_message", 10)))
    except (TypeError, ValueError):
        per = 10
    try:
        carry = max(0, int(c.get("carry_over_weeks", 1)))
    except (TypeError, ValueError):
        carry = 1
    return {"delivery": mode, "day": day_i, "hour": hour, "prefixes": pref, "max_per_message": per,
            "carry_over_weeks": carry, "say_empty": bool(c.get("say_empty", True)),
            "when": f"{_DAYS[day_i].capitalize()} {hour:02d}:00"}


def is_urgent(q, owner="", thread="", replaces="", asked=False) -> bool:
    """Goes to Chat the minute it is posted: a poster's "urgent": true, a header that starts with an
    urgent prefix (the new-lead text), a card an owner asked for in chat (`asked`, in a thread or a
    DM: the owner is waiting on it; the owner 2026-10-21 "post the approval card instantly here in the
    thread, where we are talking about it"), or a revision of a card that already reached Chat."""
    if not isinstance(q, dict):
        return False
    if q.get("urgent") or thread or asked:
        return True
    if q.get("hold_text"):
        return True      # a standing text's preview (Hold button): time-bound, gone by itself before any digest
    hdr = str(q.get("header") or "").strip().lower()
    if any(hdr.startswith(p.lower()) for p in delivery_policy()["prefixes"]):
        return True
    if replaces:
        old = _read(LIVE / f"{replaces}.json") or _read(DONE / f"{replaces}.json")
        if isinstance(old, dict) and (old.get("urgent") or old.get("chat") or old.get("delivery") == "now"):
            return True
    return False


def digest_expired(c, now=None) -> bool:
    """A held card lives until it has had its digests: never before the first (unless the digest has
    not run for DIGEST_HOLD_MAX_S), then carry_over_weeks more, then a week after the last one."""
    now = now or time.time()
    shown = int(c.get("digest_count") or 0)
    if not shown:
        return now - float(c.get("held_ts") or c.get("ts") or 0) > DIGEST_HOLD_MAX_S
    carry = delivery_policy()["carry_over_weeks"]
    return shown >= carry + 1 and now - float(c.get("digest_ts") or 0) >= 7 * 86400 - 3600



def _asked_replaces(q, lane) -> str:
    """The open card an owner-asked card stands in for: same recipient (an exec card), or the same
    header + subject in the lane (a decision card). '' when there is none."""
    rcp = _recipient_any(q)
    key, subj = _hkey(q.get("header")), _subject(q)
    lane_i = str(q.get("lane") or lane or "personal")
    best = ""
    for c in sorted(_live(), key=lambda c: float(c.get("ts") or 0)):
        if c.get("status") != "open":
            continue
        cq = (c.get("questions") or [{}])[0]
        if rcp and any(_recipient_any(x) == rcp for x in c.get("questions") or []):
            best = c["id"]
        elif key and _hkey(cq.get("header")) == key and str(cq.get("lane") or "personal") == lane_i and _same_subject(cq, q, subj):
            best = c["id"]
    return best


def _recipient_any(q) -> str:
    """The recipient of an exec card as a comparable key: an email, or a phone's last ten digits. `_recipient`
    knows emails only, so an owner-asked TEXT never superseded the older open text to the same number and fell
    into the duplicate guard instead (test_intents 17, 2026-10-25)."""
    r = _recipient(q)
    if r:
        return r
    ex = q.get("exec") or {}
    pay = ex.get("payload") if isinstance(ex, dict) else None
    to = pay.get("to") if isinstance(pay, dict) else None
    to = to[0] if isinstance(to, (list, tuple)) and to else to
    digits = re.sub(r"\D", "", str(to or ""))
    return "phone:" + digits[-10:] if len(digits) >= 10 else ""


def _hkey(h) -> str:
    return re.sub(r"[^a-z0-9]+", " ", str(h or "").lower()).strip()


def _label(o) -> str:
    return re.sub(r"\s*\((?:recommended|rec\.?)[^)]*\)\s*$", "", str(o.get("label") or ""), flags=re.I).strip()


def _active_options(opts) -> list:
    return [o for o in opts if not _PASSIVE.match(_label(o)) and not _NO_ACTION_OPT.match(_label(o))]


def _subject(q) -> str:
    """The entity a decision card is about, when the poster names one: `subject`, a chain key, a
    probation class. Two cards with different subjects are two decisions whatever their wording."""
    return str(q.get("subject") or q.get("chain_key") or q.get("probation_class") or q.get("mining_key") or "").strip().lower()


_ENT_STOP_BASE = frozenset({
    "monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday", "mon", "tue", "wed", "thu", "fri", "sat", "sun",
    "january", "february", "march", "april", "may", "june", "july", "august", "september", "october", "november", "december",
    "jan", "feb", "mar", "apr", "jun", "jul", "aug", "sep", "sept", "oct", "nov", "dec", "the", "this", "that", "what", "who",
    "which", "when", "where", "how", "why", "owner1", "owner6", "sage", "mission", "control", "edit", "send",
    "skip", "reply", "text", "approve", "example", "films", "recommended", "yes", "nothing", "fix", "still", "also", "your",
    "you", "they", "she", "his", "her", "their", "our", "and", "but", "for", "with", "from", "about", "after", "before",
    "tap", "card", "both", "new", "old", "one", "two", "three", "today", "tomorrow", "tonight", "yesterday", "pick", "put"})


def _ent_stop() -> frozenset:
    """Words that are NOT entity names in card questions: common words plus the CRM channel name and its aliases."""
    return _ENT_STOP_BASE | {crm.CHANNEL} | crm.kinds("CHANNEL_ALIASES")


def _entities(text) -> set:
    """Who or what a question is about: numbers of 4+ digits (a phone's tail), emails, and proper
    names (capitalised words not in the stoplist). Two questions about different people are
    different decisions even when their wording is a template."""
    t = str(text or "")
    ents = set(re.findall(r"\d{4,}|[\w.+-]+@[\w-]+\.\w+", t))
    stop = _ent_stop()
    for w in re.findall(r"\b[A-Z][a-z]{2,}\b", t):
        if w.lower() not in stop:
            ents.add(w.lower())
    return ents


def _same_subject(a, b, subj_b: str = "") -> bool:
    sa, sb = _subject(a), subj_b or _subject(b)
    if sa and sb:
        return sa == sb
    ea, eb = _entities(a.get("question")), _entities(b.get("question"))
    if ea and eb and ea != eb:
        return False
    return _similar(a.get("question"), b.get("question"))


def _similar(a, b, floor: float = 0.6) -> bool:
    """Two questions about the same thing (difflib ratio over squashed text)."""
    import difflib
    a, b = _squash(a)[:400], _squash(b)[:400]
    return bool(a) and bool(b) and difflib.SequenceMatcher(None, a, b).ratio() >= floor


def _draftlike(label: str) -> bool:
    """A button label that is really a message ("Regular it is, tie comes later")."""
    return len(label.split()) >= 7 or len(label) > 45


def _read(p):
    try:
        return json.loads(Path(p).read_text(encoding="utf-8-sig"))
    except Exception:
        return None


def _write(p: Path, obj):
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(obj, ensure_ascii=False, indent=1), encoding="utf-8")
    for _ in range(20):
        try:
            os.replace(tmp, p)
            return
        except PermissionError:
            time.sleep(0.05)
    os.replace(tmp, p)


def _append(p: Path, row):
    p.parent.mkdir(parents=True, exist_ok=True)
    with open(p, "a", encoding="utf-8") as f:
        f.write(json.dumps(row, ensure_ascii=False) + "\n")


def _squash(s):
    return re.sub(r"\s+", " ", str(s or "")).strip().lower()


def _any_ts(v) -> float:
    try:
        t = float(v)
        return t / 1000.0 if t > 1e12 else t
    except (TypeError, ValueError):
        pass
    for fmt, n in (("%Y-%m-%dT%H:%M:%S", 19), ("%Y-%m-%dT%H:%M", 16), ("%Y-%m-%d %H:%M", 16)):
        try:
            return time.mktime(time.strptime(str(v)[:n], fmt))
        except (TypeError, ValueError):
            continue
    return 0.0


# ------------------------------------------------------------------ guards
def _recipient(q) -> str:
    ex = q.get("exec") or {}
    pay = ex.get("payload") if isinstance(ex, dict) else None
    if isinstance(pay, dict):
        to = pay.get("to")
        to = to[0] if isinstance(to, (list, tuple)) and to else to
        m = _EMAIL.search(str(to or ""))
        if m:
            return m.group(0).lower()
    m = re.search(r"\[[A-Z ]*PREVIEW\]", str(q.get("question") or ""))
    if m:
        e = _EMAIL.search(str(q["question"])[m.end():m.end() + 300])
        if e:
            return e.group(0).lower()
    return ""


def _fill_inbound_ts(q) -> None:
    """A reply to someone who just wrote in is never "the 3rd card to them in 24 h" (Corwin said yes at
    16:10 on 2026-10-23 and the card answering him was refused FRESHNESS_REPEAT_RECIPIENT; Sage had to ask
    in chat). The brain never fills last_inbound_ts, so take it from the comms board
    (data/comms_state.json, written zero-LLM by the comms probe: the newest line each way per contact).
    Only the inbound side is filled here: it can only relax the repeat guard, never refuse a card."""
    if not isinstance(q, dict) or str(q.get("last_inbound_ts") or "").strip():
        return
    rcp = _recipient(q)
    if not rcp:
        return
    try:
        from alib import DATA as _ADATA                  # assistant/data (ASSISTANT_DATA in tests), not the cards root
        threads = (_read(_ADATA / "comms_state.json") or {}).get("threads") or {}
    except Exception:  # noqa: BLE001
        return
    keys = [f"email:{rcp}"] if "@" in rcp else []
    digits = re.sub(r"\D", "", rcp)
    if digits:
        keys += [f"text:+{digits}", f"text:+1{digits}" if len(digits) == 10 else ""]
    for k in [k for k in keys if k]:
        li = ((threads.get(k) or {}).get("last_inbound") or {}).get("ts")
        if li:
            q["last_inbound_ts"] = str(li)
            return


def _freshness(q, now) -> str:
    if str(q.get("freshness_ack") or "").strip():
        return ""
    li, lo = _any_ts(q.get("last_inbound_ts")), _any_ts(q.get("last_outbound_ts"))
    if li and lo and lo > li:
        return ("FRESHNESS_ALREADY_REPLIED: newest outgoing %s is after their last incoming %s; "
                "somebody already answered them." % (time.strftime("%m/%d %H:%M", time.localtime(lo)),
                                                     time.strftime("%m/%d %H:%M", time.localtime(li))))
    rcp = _recipient(q)
    if not rcp:
        return ""
    hits = []
    for d in (LIVE, DONE):
        try:
            files = list(d.glob("q*.json"))
        except OSError:
            continue
        for f in files:
            c = _read(f)
            if not isinstance(c, dict) or now - float(c.get("ts") or 0) > REPEAT_WINDOW_S:
                continue
            if any(_recipient(x) == rcp for x in c.get("questions") or []):
                hits.append(c.get("id"))
    if _is_reply(q):
        return ""                 # answering someone who wrote in is a conversation, not a chase (Corwin, 2026-10-24 16:46)
    if len(hits) + 1 >= REPEAT_LIMIT and not (li and li > max(
            float((_read(LIVE / f"{h}.json") or _read(DONE / f"{h}.json") or {}).get("ts") or 0) for h in hits)):
        return "FRESHNESS_REPEAT_RECIPIENT: this would be card #%d to ...%s in 24h (%s). Bundle, do not chain." % (
            len(hits) + 1, rcp[-6:], ", ".join(hits[-3:]))
    return ""


_REPLY_HEAD = re.compile(r"^\s*(reply to|text reply|reply:|re:|answer to)", re.I)


def _is_reply(q) -> bool:
    """A card that answers something the recipient just said (its header says so, or the brain marked it)."""
    return bool(q.get("in_reply") or _REPLY_HEAD.match(str(q.get("header") or "")) or
                re.match(r"^\s*\w+ (replied|texted|asked|wrote)", str(q.get("question") or ""), re.I))


def _live_conversation(q, hours: float = 24.0) -> bool:
    """We sent this recipient something in the last `hours` (a done outbound ticket): a reply card is then part
    of a conversation the owner is in, and goes to Chat now, not to Monday's digest (Marlo, 2026-10-24 15:35)."""
    pay = ((q.get("exec") or {}).get("payload") or {}) if isinstance(q.get("exec"), dict) else {}
    to = pay.get("to")
    to = to[0] if isinstance(to, (list, tuple)) and to else to
    rcp = str(to or "").strip() or _recipient(q)
    if not rcp:
        return False
    digits = re.sub(r"\D", "", rcp)
    try:
        for t in es.fold().values():
            if t.get("status") != "done" or t.get("kind") not in es.OUTBOUND_KINDS:
                continue
            p = t.get("payload") or {}
            to = str(p.get("to") or p.get("workspace_id") or "")
            if not to or (digits and re.sub(r"\D", "", to)[-10:] != digits[-10:]) or (not digits and to.lower() != rcp.lower()):
                continue
            done = time.mktime(time.strptime(str(t.get("ts_done") or "")[:19], "%Y-%m-%dT%H:%M:%S"))
            if time.time() - done <= hours * 3600:
                return True
    except Exception:  # noqa: BLE001
        return False
    return False


def _sanitize_exec(q):
    ex = q.get("exec")
    if not ex:
        return None, None
    if not isinstance(ex, dict):
        return None, "exec must be an object"
    kind, payload = str(ex.get("kind") or "").strip(), ex.get("payload")
    if not kind or not isinstance(payload, dict) or not payload:
        return None, "exec needs kind + payload"
    if kind not in es.KIND_LANES:
        return None, f"exec kind {kind!r} unknown; kinds: {sorted(es.KIND_LANES)}"
    why = es.validate_payload(kind, payload)
    if why:
        return None, "EXEC_KEYS: " + why
    msg = next((str(payload[k]) for k in _EXEC_TEXT_KEYS if payload.get(k)), "")
    if ex.get("send_at"):
        # a send / job approved for LATER: the time is checked now and must be on the card in words
        try:
            sa = es.parse_not_before(ex["send_at"])
        except ValueError as e:
            return None, f"EXEC_KEYS: {e}"
        if _squash(es.when_text(sa)) not in _squash(q.get("question")) and _squash(sa) not in _squash(q.get("question")):
            return None, f"EXEC_VERBATIM: the send time {es.when_text(sa)} is not in the card question; the owner must read when it goes out"
        ex["send_at"] = sa
    if kind == "local":
        import jobs
        j = jobs.find(str(payload.get("op") or ""))
        if j:
            try:
                jobs.build_cmd(j, payload.get("args") or {})
            except ValueError as e:
                return None, f"EXEC_KEYS: {e}"
            qsq = _squash(q.get("question"))
            for k, v in (payload.get("args") or {}).items():
                if _squash(str(v)) not in qsq:
                    return None, f"EXEC_VERBATIM: job value {k}={str(v)[:50]!r} is not in the card question; the owner must read every value the job runs with"
    if kind == "email_send":
        qsq = _squash(q.get("question"))
        for k in ("from", "to", "subject", "cc"):
            if k == "cc" and not payload.get("cc"):
                continue
            v = payload[k]
            for one in (v if isinstance(v, (list, tuple)) else [v]):      # each address on its own: a list may be laid out any way
                if _squash(str(one)) not in qsq:
                    return None, f"EXEC_VERBATIM: payload {k}={str(one)[:50]!r} is not in the card question; owner1 must read it"
    if kind == "imessage_send":
        qtext = str(q.get("question") or "")
        if "[TEXT PREVIEW]" not in qtext:
            return None, "EXEC_VERBATIM: an imessage_send card must contain [TEXT PREVIEW] followed by the recipient and the exact text"
        to = str(payload["to"])
        digits = re.sub(r"\D", "", to)
        if _squash(to) not in _squash(qtext) and (not digits or digits[-10:] not in re.sub(r"\D", "", qtext)):
            return None, f"EXEC_VERBATIM: imessage_send to={to!r} is not in the card question; the approver must see who it goes to"
    if kind in crm.kinds("MESSAGE_KINDS"):
        qtext = str(q.get("question") or "")
        tag = crm.preview_tag()
        if tag not in qtext:
            return None, f"EXEC_VERBATIM: a {crm.name()} message card must contain {tag} followed by who it goes to and the exact text"
    if kind in crm.kinds("FIELD_UPDATE_KINDS"):
        qsq = _squash(q.get("question"))
        for k, v in (payload.get("fields") or {}).items():
            if _squash(str(v)) not in qsq:
                return None, f"EXEC_VERBATIM: {kind} {k}={str(v)[:50]!r} is not in the card question; owner1 must read every value this writes"
    if kind in crm.kinds("PARTICIPANT_KINDS"):
        qsq = _squash(q.get("question"))
        for k in ("email", "name"):
            v = str(payload.get(k) or "").strip()
            if v and _squash(v) not in qsq:
                return None, f"EXEC_VERBATIM: {kind} {k}={v[:50]!r} is not in the card question; the approver must read who is being added"
    if kind in crm.kinds("STAGE_KINDS"):
        qsq = _squash(q.get("question"))
        if _squash(str(payload.get("stage") or "")) not in qsq:
            return None, f"EXEC_VERBATIM: {kind} stage={str(payload.get('stage'))[:40]!r} is not in the card question; the approver must read where the project goes"
    if kind == "sheet_set":
        # every cell this writes, with its value, sits verbatim on the card: the owner reads each one
        qsq = _squash(q.get("question"))
        try:
            cells = es.sheet_cells(payload)
            g = es._google_api()
        except Exception as e:  # noqa: BLE001
            return None, f"EXEC_KEYS: sheet_set {e}"
        for ref, value, _before in cells:
            if _squash(ref) not in qsq:
                return None, f"EXEC_VERBATIM: sheet cell {ref} is not in the card question; owner1 must read every cell this writes"
            if _squash(g.cell_text(value)) not in qsq:
                return None, f"EXEC_VERBATIM: the value {g.cell_text(value)[:40]!r} for {ref} is not in the card question; owner1 must read every value this writes"
        # the account's token must hold the Sheets scope now, or an approval lands on a write that cannot run
        ok, why = g.sheet_ready(str(payload.get("account") or ""))
        if not ok:
            return None, why
    _path_check = crm._hook("path_ok")
    if callable(_path_check):
        # the daily self-test found this kind's connector path dead: no card may be posted for it,
        # or an approval lands on a send that cannot run (Emery, 2026-10-18, approved twice, sent never)
        try:
            ok, why = _path_check(kind)
        except Exception:  # noqa: BLE001
            ok, why = True, ""
        if not ok:
            return None, why
    if kind == "agent_run":
        qsq = _squash(q.get("question"))
        if _squash(str(payload.get("step"))) not in qsq:
            return None, "EXEC_VERBATIM: the agent_run step sentence is not in the card question; the owner must read the exact action"
        for k, v in (payload.get("values") or {}).items():
            if _squash(str(v)) not in qsq:
                return None, f"EXEC_VERBATIM: agent_run value {k}={str(v)[:50]!r} is not in the card question; the owner must read every value this commits"
    if kind == "tracking_fix":
        qsq = _squash(q.get("question"))
        for fx in payload["fixes"]:
            if not isinstance(fx, dict) or not str(fx.get("desc") or "").strip():
                return None, "EXEC_KEYS: every tracking fix needs a 'desc' (the words owner1 reads)"
            if _squash(str(fx["desc"])) not in qsq:
                return None, f"EXEC_VERBATIM: tracking fix {str(fx['desc'])[:60]!r} is not in the card question; owner1 must read every record this changes"
    if kind == "ads_apply":
        qsq = _squash(q.get("question"))
        for st in payload["steps"]:
            if _squash(str(st["desc"])) not in qsq:
                return None, f"EXEC_VERBATIM: ads step {str(st['desc'])[:60]!r} is not in the card question; owner1 must read every change"
        if _squash(f"${float(payload['total']):.2f}/day") not in qsq:
            return None, "EXEC_VERBATIM: the new daily total ($X.XX/day) must be in the card question"
    if msg and _squash(msg) not in _squash(q.get("question")):     # the WHOLE text, not its first 4000 chars
        return None, ("EXEC_VERBATIM: the payload text is not verbatim inside the card question. owner1 must read "
                      "exactly what will be sent; \"details\" does NOT count (it is folded shut).")
    approve_label = str(ex.get("approve_label") or "Approve")[:60]
    labels = [str((o or {}).get("label") or "").strip() for o in (q.get("options") or []) if isinstance(o, dict)]
    if labels and approve_label.strip().lower() not in [x.lower() for x in labels]:
        return None, f"exec approve_label {approve_label!r} matches none of the options {labels}"
    edity = [x for x in labels + [approve_label] if re.search(r"\bedit", x, re.I)]
    if edity:
        # Marisa, 2026-10-18: an option labelled "Edit & send (Recommended)" was the one-tap approve,
        # so tapping it sent the draft unedited, blanks and all. Chat and Mission Control add the
        # real edit button themselves; no option may be named like it.
        return None, (f"EDIT_LABEL: {edity[0]!r} - no button may say Edit; the edit box is added to every exec card "
                      "automatically. Name the approve Send / Approve, and say in `advice` to tap Edit & send.")
    clean = {"kind": kind, "payload": payload, "approve_label": approve_label}
    if ex.get("send_at"):
        clean["send_at"] = ex["send_at"]          # normalized above; the tap mints the ticket with it as not_before
    return clean, None


def _warn_long(q, ex):
    t = str(q.get("question") or "")
    for marker in ("[EMAIL PREVIEW]", "[TEXT PREVIEW]", "[SHEET CELLS]"):      # [SHEET CELLS]: a sheet_set card's cell list (every cell verbatim by rule)
        i = t.find(marker)
        if i != -1:
            t = t[:i]
    if ex:
        for k in _EXEC_TEXT_KEYS:
            v = (ex.get("payload") or {}).get(k)
            if v and str(v) in t:
                t = t.replace(str(v), "")
    prose = " ".join(t.split())
    if len(prose) > EXPLANATION_MAX:
        print(f"CARD_TOO_LONG [{str(q.get('header') or '')[:20]}] explanation={len(prose)} chars (limit "
              f"{EXPLANATION_MAX}). Keep the summary to the decision; move evidence into \"details\".", file=sys.stderr)


# ------------------------------------------------------------------ post / note
CONN_STATUS = Path(os.environ.get("ASSISTANT_DATA") or (HERE.parent / "data")) / "connections_status.json"


def _probe_key(q) -> str:
    cw = q.get("clears_when") if isinstance(q, dict) else None
    if not isinstance(cw, dict):
        return ""
    if cw.get("connection"):
        return "connection:" + str(cw["connection"])
    if cw.get("disk"):
        return "disk:" + str(cw["disk"]).upper().rstrip(":\\/")
    if cw.get("breaker"):
        return "breaker:" + str(cw["breaker"])
    if cw.get("sentinel"):
        return "sentinel:" + str(cw["sentinel"])
    return ""


def _probe_clear(q, since: float):
    """'' while the fault stands, else a short 'recovered' line. Only ever reads what the
    zero-LLM checks already proved; an unknown or malformed probe never clears anything."""
    cw = q.get("clears_when") if isinstance(q, dict) else None
    if not isinstance(cw, dict):
        return ""
    if cw.get("connection"):
        row = ((_read(CONN_STATUS) or {}).get("connections") or {}).get(str(cw["connection"])) or {}
        try:                 # conn_check stamps UTC ("...Z"); read as local it would look 7 h newer
            import calendar
            checked = calendar.timegm(time.strptime(str(row.get("checked") or "")[:19], "%Y-%m-%dT%H:%M:%S"))
        except (TypeError, ValueError):
            checked = 0.0
        if row.get("state") == "live" and checked > since:
            return f"{row.get('name') or cw['connection']} proven live again ({row.get('detail', '')[:80]})"
        return ""
    if cw.get("disk"):
        import shutil
        try:
            free = shutil.disk_usage(str(cw["disk"]).rstrip("\\/") + "\\").free / 1e9
        except OSError:
            return ""
        return f"{cw['disk']} has {free:.0f} GB free" if free >= float(cw.get("min_free_gb", 5)) else ""
    if cw.get("breaker"):
        try:
            import drain
            return "" if drain.breaker_tripped(str(cw["breaker"])) else f"the {cw['breaker']} channel is sending again"
        except Exception:  # noqa: BLE001
            return ""
    if cw.get("sentinel"):
        try:
            import sentinels
            keys = {str(f.get("key")) for f in sentinels.collect(memo=True)}   # once per sweep, not once per card (2026-10-27)
            return "" if str(cw["sentinel"]) in keys else "the check passes again"
        except Exception:  # noqa: BLE001
            return ""
    return ""


def _recently_answered(key: str, lane: str, window_s: float = REASK_WINDOW_S, question=None) -> dict:
    """The consumed (answered) card with this header key + lane retired inside the window, with
    the owner's answer, or {}."""
    now = time.time()
    try:
        files = list(DONE.glob("q*.json"))
    except OSError:
        return {}
    idx = None
    best = {}
    for f in files:
        try:
            if now - f.stat().st_mtime > window_s:
                continue
        except OSError:
            continue
        c = _read(f)
        if not isinstance(c, dict) or c.get("status") != "consumed" or now - float(c.get("retired_ts") or 0) > window_s:
            continue
        q = (c.get("questions") or [{}])[0]
        if q.get("exec") or _hkey(q.get("header")) != key or str(q.get("lane") or "personal") != lane:
            continue
        if isinstance(question, dict) and not _same_subject(q, question):
            continue
        if idx is None:
            idx = _answers_index()
        a = idx.get(c["id"]) or {}
        if a.get("undone"):
            continue
        if not best or float(c.get("retired_ts") or 0) > best["_ts"]:
            best = {"qid": c["id"], "option": str(a.get("option") or ""), "_ts": float(c.get("retired_ts") or 0),
                    "when": time.strftime("%a %H:%M", time.localtime(float(c.get("retired_ts") or now)))}
    best.pop("_ts", None)
    return best


def say(owner, lane, text, thread=""):
    """Sage says one plain line in chat, and the same line lands on Mission Control as a note.
    The path for everything that is not a decision the system can act on (the owner 2026-10-17):
    a fact only the owner can supply, a thing only he can do, a heads-up. Never a card."""
    t = re.sub(r"\s+\n", "\n", str(text or "")).strip()
    if not t:
        return {"said": False, "reason": "empty"}
    row = note(owner, t)
    n = _push("push_say", t, str(lane or ""), str(thread or "")) or 0
    es.ledger("card_say", owner=str(owner or ""), lane=str(lane or ""), n=int(n), text=t[:160])
    return {"said": True, "chat": int(n), "note_ts": row["ts"]}


def post(questions, owner, expires_s=DEFAULT_EXPIRES_S, lane="", thread="", replaces="", space="", asked=False):
    """`thread`: a Google Chat thread name; the card's Chat message is posted there (a revision
    asked for in a card's thread lands in that thread, 2026-10-08). `replaces`: the open card this
    one revises (actions.revise_card): the duplicate-subject guards skip it.
    `space`: post the card's Chat message only into this space (an owner's DM, when the ask was made
    there) instead of the lane's usual targets. `asked`: an owner asked for this card in chat
    (2026-10-21): it is urgent wherever it was asked, and the chase guards (a third card to one
    recipient in a day, the same subject already open, the entity rules) SUPERSEDE the older open card
    instead of refusing, because the owner is waiting on this one. The send path, drain and the tap are
    unchanged: every send still needs the tap."""
    r = _post(questions, owner, expires_s, lane, thread, replaces, space=space, asked=asked)
    if not r.get("posted"):
        es.ledger("card_refused", owner=str(owner or ""), reason=str(r.get("reason") or "")[:200],
                  header=str(((questions or [{}])[0] if isinstance(questions, list) and questions else {}).get("header") or "")[:80])
        if os.environ.get("CARDS_TRACE"):
            print(f"CARD REFUSED ({owner}): {r.get('reason')}", file=sys.stderr)
    return r


def _post(questions, owner, expires_s, lane, thread, replaces, space="", asked=False):
    if asked and isinstance(questions, list) and questions and isinstance(questions[0], dict):
        # an owner-asked card: the older open card on the same subject / to the same recipient is what the
        # owner is replacing, so find it and supersede it rather than refuse (Corwin's proposal, 2026-10-21:
        # four asks, FRESHNESS_REPEAT_RECIPIENT each time, no card)
        if not replaces:
            replaces = _asked_replaces(questions[0], lane)
        questions[0].setdefault("freshness_ack", "owner asked for this card in chat")
    if isinstance(questions, list) and questions and isinstance(questions[0], dict):
        _fill_inbound_ts(questions[0])
    if not isinstance(questions, list) or len(questions) != 1 or not isinstance(questions[0], dict):
        return {"posted": False, "reason": "ONE_QUESTION: card.json must be a list with exactly ONE question. The page shows and "
                                           "answers only the first one, so a second question (and its exec) would be dropped "
                                           "silently. Post a separate card for each decision."}
    now = time.time()
    execs, advice = [], []
    for i, q in enumerate(questions):
        if not str(q.get("question") or "").strip():
            return {"posted": False, "reason": f"q{i}: empty question"}
        opts = [o for o in (q.get("options") or []) if isinstance(o, dict) and o.get("label")]
        ex, err = _sanitize_exec(q)
        if err:
            return {"posted": False, "reason": err, "q": i}
        execs.append(ex)
        if not ex:
            # the button grammar (the owner 2026-10-17 evening). An exec card is an action by
            # construction (Send / Approve); a decision card has to earn it.
            if len(opts) < 2:
                return {"posted": False, "q": i, "reason": "NOT_A_DECISION: a card with nothing to decide is not a card. Say it in chat: "
                                                            "python assistant/scripts/cards.py say --lane <lane> \"<one plain line>\""}
            if not _active_options(opts):
                return {"posted": False, "q": i, "reason": "NEEDS_ACTION: every button must be something the system DOES on the tap "
                                                            "(\"Set it up now\", \"Draft the reply\", \"Book it for me\") or a Skip/Drop; a card whose only "
                                                            "real button is the owner's own work (\"I'll reconnect it\", \"Got it\", \"I'll look\") tells him to do "
                                                            "something without offering the help. If only he can do it, say so in chat: cards.py say"}
            hdr = str(q.get("header") or "")
            if _REPLY_HEADER.match(hdr):
                return {"posted": False, "q": i, "reason": "TEXT_NEEDS_DRAFT: a reply card carries the draft itself (exec imessage_send / "
                                                            "email_send / a CRM message kind with [TEXT PREVIEW] and the full text), never a choice of "
                                                            "replies as buttons. Write the likeliest safe reply, put the alternative in `advice` "
                                                            "(Edit & send is on every card); if one fact is missing, ask for it in chat with "
                                                            "cards.py say and post the draft once it is known."}
            bad = [_label(o) for o in opts if _draftlike(_label(o))]
            if bad:
                return {"posted": False, "q": i, "reason": f"OPTIONS_ARE_DRAFTS: {bad[0][:60]!r} is a message, not a button. The draft goes in the "
                                                            "body (exec + [TEXT PREVIEW]); buttons are short actions (Send / Skip; Edit & send is added by code)."}
        if ex:
            # the draft lint (ported from the property comms lane): the owner's recurring corrections,
            # applied before he sees the card. A block refuses the card; warnings ride as advice.
            try:
                import draft_lint
                blocks, warns = draft_lint.check_card(q)
            except Exception:  # noqa: BLE001
                blocks, warns = [], []
            if blocks:
                return {"posted": False, "q": i, "reason": "DRAFT_LINT: fix and re-post: " + " | ".join(blocks)}
            advice.extend(f"q{i}: DRAFT_LINT warn {w}" for w in warns)
        fr = _freshness(q, now)
        if fr:
            return {"posted": False, "reason": fr, "q": i}
        if len(opts) >= 2 and not ex and not str(q.get("no_recommendation") or "").strip() \
                and not any("(recommended)" in str(o.get("label", "")).lower() for o in opts):
            return {"posted": False, "reason": "NO_RECOMMENDATION: a judgment card with 2+ options must mark one "
                                               "\"(Recommended)\" and put it first (or set no_recommendation: why)", "q": i}
        key = _probe_key(q)
        if key:
            dup = next((c["id"] for c in _live() if c.get("status") == "open"
                        and any(_probe_key(x) == key for x in c.get("questions") or [])), None)
            if dup:
                return {"posted": False, "qid": dup, "q": i,
                        "reason": f"DUPLICATE_ALARM: card {dup} is already open for {key}; it closes itself when the check passes. Do not post another."}
        if not ex and not replaces:
            hdr = str(q.get("header") or "")
            key, subj = _hkey(hdr), _subject(q)
            lane_i = str(q.get("lane") or lane or "personal")
            for c in _live():
                if c.get("status") != "open":
                    continue
                cq = (c.get("questions") or [{}])[0]
                if _hkey(cq.get("header")) == key and str(cq.get("lane") or "personal") == lane_i and _same_subject(cq, q, subj):
                    return {"posted": False, "q": i, "qid": c["id"],
                            "reason": f"DUPLICATE_SUBJECT: card {c['id']} ({hdr!r}) is already open in front of owner1; it is one decision, "
                                      "not two. Stage the ticket on that card (note 'card <qid>') instead of posting another."}
            prev = _recently_answered(key, lane_i, question=q)
            if prev:
                return {"posted": False, "q": i, "qid": prev["qid"],
                        "reason": f"ALREADY_ANSWERED: owner1 chose {prev['option']!r} on {prev['qid']} ({hdr!r}) at {prev['when']}. "
                                  "Act on that answer; never ask the same question again (owner1 2026-10-17: 'I'm getting the same "
                                  "questions over and over')."}
        # the same decision under a NEW header, or a reply card for a conversation already on a card
        # or already sent (the owner 2026-10-19: "I still get repeat approval questions"): keyed on WHO the
        # card is about (decision_keys.py), not on its wording. Reads only; a fault never blocks.
        try:
            import decision_keys
            dk = decision_keys.check(q, lane, sys.modules[__name__], replaces=replaces)
        except Exception:  # noqa: BLE001
            dk = None
        if dk:
            return {"posted": False, "q": i, "qid": dk.get("qid", ""), "reason": dk["reason"]}
        hits = rulings.find(q["question"], lane=q.get("lane") or lane)
        if hits:
            q["rulings"] = [{"id": h["id"], "ruling": h["ruling"], "quote": h["quote"]} for h in hits[:3]]
            advice.append(f"q{i}: RULING {hits[0]['id']} applies -- \"{hits[0]['ruling'][:120]}\"; card posted pre-decided")
        _warn_long(q, ex)
        if ex:
            q["exec"] = ex
        q.setdefault("lane", lane or "personal")
    qid = "q" + uuid.uuid4().hex[:12]
    if int(expires_s) == DEFAULT_EXPIRES_S:
        expires_s = EXEC_EXPIRES_S if any(execs) else DECISION_EXPIRES_S
    card = {"id": qid, "ts": now, "owner": str(owner or ""), "status": "open",
            "wait_s": float(expires_s), "questions": questions}
    if thread:
        card["chat_thread"] = str(thread)
    if space:
        card["chat_space"] = str(space)
    if asked:
        card["asked"] = True
    pol = delivery_policy()
    if not asked and not thread and _is_reply(questions[0]) and _live_conversation(questions[0]):
        questions[0]["urgent"] = True             # a reply inside a live conversation never waits for the digest
    system = is_system(questions[0]) and not system_to_chat()
    if system:
        questions[0].pop("urgent", None)          # the machinery's own idea of urgency does not count (2026-10-27)
    urgent = (not system) and is_urgent(questions[0], owner, thread, replaces, asked=asked)
    hold = system or (pol["delivery"] == "weekly" and not urgent)
    if system:
        # Mission Control only: never in Chat, never in the digest; the reflex works it (reflex.d_system_cards)
        card.update(delivery="mc", held_ts=now, digest_count=0)
    elif hold:
        # held for the weekly digest: open on Mission Control now, in Chat on digest day. The
        # digest's own expiry rules apply (digest_expired); wait_s is only the outer bound.
        card.update(delivery="weekly", held_ts=now, digest_count=0, wait_s=float(DIGEST_HOLD_MAX_S))
    else:
        card["delivery"] = "now"
        if urgent:
            card["urgent"] = True
    _write(ARCHIVE / f"{qid}.json", card)
    _write(LIVE / f"{qid}.json", card)
    es.ledger("card_post", qid=qid, owner=str(owner or ""), n=len(questions),
              execs=[e["kind"] for e in execs if e], delivery=card["delivery"])
    if hold:
        try:                                        # the same one-live-text-per-number rule push_card applies
            import text_gate
            text_gate.supersede_older(card)
        except Exception:  # noqa: BLE001
            pass
        _push("record_card", card)
        if system:
            advice.append("A system card: on Mission Control only, never in Chat or the digest; the reflex works it. "
                          "Never tell an owner it is on his phone.")
            return {"posted": True, "qid": qid, "advice": advice, "chat": [], "delivery": "mc"}
        advice.append(f"HELD for the weekly digest ({pol['when']}): not in Chat yet, open on Mission Control now. "
                      "Say so if you mention it; never say it is on his phone.")
        return {"posted": True, "qid": qid, "advice": advice, "chat": [], "delivery": "weekly"}
    chat = _push("push_card", card)                 # the owners' phones (Google Chat), buttons included
    if chat:
        card["chat"] = chat
        _write(LIVE / f"{qid}.json", card)
    _push("record_card", card)                      # and the agent's conversation (with its Chat thread), so it can talk about the card
    if asked and replaces:
        try:
            supersede(replaces, qid, by=f"asked:{owner}")
        except Exception:  # noqa: BLE001
            pass
    return {"posted": True, "qid": qid, "advice": advice, "chat": sorted(chat or {}), "delivery": "now"}


def _push(fn, *args):
    """Google Chat is a mirror of the store, never a dependency: any failure is logged by
    chat_push itself and the card path continues."""
    try:
        import chat_push
        return getattr(chat_push, fn)(*args)
    except Exception:  # noqa: BLE001
        return None


def note(owner, text):
    row = {"ts": time.strftime("%Y-%m-%dT%H:%M:%S"), "owner": owner, "text": str(text)[:600]}
    _append(NOTES, row)
    return row


# ------------------------------------------------------------------ answer / undo
def _answers_index():
    idx = {}
    try:
        for ln in ANSWERS.read_text(encoding="utf-8-sig").splitlines():
            try:
                e = json.loads(ln)
            except ValueError:
                continue
            if e.get("qid"):
                idx[e["qid"]] = e            # last entry wins (the owner can reverse himself)
    except OSError:
        pass
    return idx


def _drain_phone_after_undo():
    """the owner approved a text: once the undo window has passed, release it to the phone outbox
    right away (drain + nudge) instead of waiting for the Day Spine's 10-minute tick. A
    background timer in whichever process took the tap; never raises, off in test runs."""
    if os.environ.get("ASSISTANT_CARDS") or os.environ.get("ASSISTANT_DATA"):
        return
    try:
        import threading

        def _go():
            try:
                import drain as _drain
                _drain.drain(lane="phone", min_age_s=UNDO_S + 5)
            except Exception:  # noqa: BLE001
                pass
        threading.Timer(UNDO_S + 6, _go).start()
    except Exception:  # noqa: BLE001
        pass


def answer(qid, option, text="", revised="", q_index=0, by="owner1", send_at=""):
    """One answer at a time per card, across threads and processes: Mission Control is a
    threading server, so two taps arriving together could both read the card as 'open' and
    both mint a ticket. The marker file is the mutex; _answer_locked does the work."""
    mark = ROOT / "locks" / f"{re.sub(r'[^A-Za-z0-9]', '', str(qid))[:40]}.answering"
    try:
        mark.parent.mkdir(parents=True, exist_ok=True)
        if mark.exists() and time.time() - mark.stat().st_mtime > 30:
            mark.unlink()
        with open(mark, "x"):
            pass
    except OSError:
        return {"ok": False, "reason": "this card is being answered right now - reload the page"}
    try:
        return _answer_locked(qid, option, text, revised, q_index, by, send_at)
    finally:
        try:
            mark.unlink()
        except OSError:
            pass


def _answer_locked(qid, option, text="", revised="", q_index=0, by="owner1", send_at=""):
    """the owner's tap. If the tapped option is the exec approve_label, mint the ticket here
    (zero-LLM) with the exact approved payload; `revised` replaces the message text
    (the pencil on the card) and is what gets minted."""
    card = _read(LIVE / f"{qid}.json")
    if not isinstance(card, dict):
        return {"ok": False, "reason": "card not open"}
    if card.get("status") != "open":
        # a double tap / reload / second approver used to mint a SECOND ticket for the same
        # card, and a later "Skip" did not cancel the first Approve
        return {"ok": False, "reason": "already answered - tap Undo first to change it"}
    qs = card.get("questions") or []
    if not 0 <= q_index < len(qs):
        return {"ok": False, "reason": "bad question index"}
    q = qs[q_index]
    labels = [str(o.get("label") or "") for o in q.get("options") or []]
    entry = {"ts": time.strftime("%Y-%m-%dT%H:%M:%S"), "epoch": time.time(), "qid": qid, "q": q_index,
             "by": by, "option": option, "text": text, "revised": bool(revised), "revised_text": revised[:2000]}
    minted = None
    ex = q.get("exec")
    if ex and send_at:
        # an owner approved in words WITH a time ("send it at 8am", 2026-10-24): the ticket is minted with that
        # not_before, exactly as a card posted with exec.send_at would be
        try:
            ex = {**ex, "send_at": es.parse_not_before(send_at)}
        except ValueError as e:
            return {"ok": False, "reason": f"not approved: {e}"}
        entry["send_at"] = ex["send_at"]
    if ex and option.strip().lower() == str(ex.get("approve_label") or "Approve").lower():
        payload = dict(ex["payload"])
        if ex.get("kind") == "agent_run" and payload.get("ask_code"):
            # the verification code the owner typed in the note box IS part of the approved step;
            # it is minted into the sha-bound payload and never kept in answers.jsonl
            payload["code"] = re.sub(r"\D", "", str(text or ""))[:10]
            text = entry["text"] = ""
        if revised:
            key = next((k for k in _EXEC_TEXT_KEYS if payload.get(k)), "body")
            payload[key] = revised
        left = es.unfilled_blanks(payload) if ex.get("kind") in es.OUTBOUND_KINDS else []
        if left:
            # a draft with an unfilled [the owner: ...] blank cannot be approved as written, and an edit that
            # left one in cannot either; the card stays OPEN so the owner can fill it (Marisa, 2026-10-18)
            return {"ok": False, "blanks": left,
                    "reason": (f"not sent - the message still has a blank to fill: {left[0][:60]}. "
                               "Tap Edit & send, replace every [ ... ] with your words, then Send.")}
        try:
            resend_ok = bool(re.search(r"\bresend\b", str(q.get("question") or ""), re.I))
            minted = es.enqueue(ex["kind"], payload, es.APPROVER, f"card:{qid}", resend_ok=resend_ok,
                                not_before=str(ex.get("send_at") or ""))
            entry["ticket"] = minted["id"]
        except (PermissionError, ValueError) as e:
            entry["mint_error"] = str(e)
    _append(ANSWERS, entry)
    card["status"] = "answered"
    _write(LIVE / f"{qid}.json", card)
    es.ledger("card_answer", qid=qid, option=option, ticket=entry.get("ticket", ""), by=by)
    if entry.get("ticket") and ex.get("kind") == "imessage_send":
        _drain_phone_after_undo()                 # a text waits 30 s, not the spine's next 10-min tick
    if not str(by).startswith("gchat:"):            # a Chat tap edits its own message in the bridge
        _push("mark_answered", card, {"option": option, "by": by, "ticket": entry.get("ticket"),
                                      "mint_error": entry.get("mint_error")})
    return {"ok": True, "qid": qid, "option": option, "ticket": entry.get("ticket"),
            "mint_error": entry.get("mint_error"), "undo_until": time.strftime("%H:%M:%S", time.localtime(time.time() + UNDO_S))}


def _took_back_ticket(e):
    """Pull back the ticket an approval minted, if what it does has not happened yet.
    -> (ok, reason, recalled). A queued ticket is cancelled; a text already handed to the phone
    outbox but not yet fetched by the phone is recalled from it; anything that ran is final."""
    tid = e.get("ticket")
    if not tid:
        return True, "", False
    cur = es.fold().get(tid) or {}
    st = cur.get("status")
    if st in ("cancelled", "exception"):
        return True, "", False                    # never went out: nothing to pull back
    if st == "queued":
        try:
            es.set_status(tid, "cancelled", "undo tap")
            return True, "", False
        except ValueError:                        # drain picked it up between fold() and here
            cur = es.fold().get(tid) or {}
            st = cur.get("status")
    if st == "done" and cur.get("kind") == "imessage_send":
        try:
            import phone_bridge
            if phone_bridge.recall(tid):
                es.mark_recalled(tid, "undo tap: pulled from the phone outbox before the phone fetched it")
                return True, "", True
        except Exception:  # noqa: BLE001
            pass
    if st == "running":
        return False, "it is going out right now, so it can't be stopped", False
    when = str(cur.get("ts_done") or "")[11:16]
    _k = str(cur.get("kind") or "")
    what = "text" if _k == "imessage_send" else "email" if _k == "email_send" \
           else (f"{crm.name()} message" if _k in crm.kinds("MESSAGE_KINDS") else "action")
    return False, (f"the {what} already went out" + (f" at {when}" if when else "")
                   + f", and a sent {what} can't be pulled back. Tell me in chat if a correction should go"), False


def _undo_followup(qid, card, e, by):
    """An answer taken back after the sweep harvested it. Close its operator ticket if nobody has
    started on it; otherwise the operator gets told to reverse whatever was already done on it."""
    import tickets
    q = (card.get("questions") or [{}])[int(e.get("q") or 0)] if isinstance(card, dict) else {}
    header = str(q.get("header") or qid)
    rnd = int(card.get("round") or 0) if isinstance(card, dict) else 0
    tid = tickets.make_id("card", qid if not rnd else f"{qid}#{rnd}")
    t = (tickets._load() or {}).get(tid)
    if t and t.get("status") == "new":
        tickets._transition(tid, "done", "cards.undo", f"owner1 undid his answer '{e.get('option')}' before it was worked")
        return "closed"
    acted = bool(t) or bool(q.get("hold_text") and _squash(e.get("option")).startswith("hold")) \
        or bool(q.get("setup_rid") or q.get("build_key") or q.get("pending_key"))
    if not acted:
        return ""
    added = tickets.add_many([{
        "source": "card_undo", "source_id": f"{qid}@{int(time.time())}", "lane": q.get("lane") or "personal",
        "kind": "card_answer", "title": f"{str(by).split(':')[-1].capitalize()} took back '{e.get('option')}' on: {header}",
        "summary": (f"He undid his answer '{e.get('option')}' on card {qid} after it had been handed on. Check what was "
                    "already done because of that answer and reverse what can still be reversed (cancel a queued step, "
                    "put a held text back, undo a change). The card is open again for his new answer. Tell him "
                    "plainly in chat anything that could not be reversed."),
        "payload": {"qid": qid, "undone_answer": e.get("option"), "answered_ts": e.get("ts"), "ticket": e.get("ticket")}}],
        "cards.undo")
    return "operator" if added else ""


def undo(qid, by="owner1"):
    """Take back an answer. Works for as long as the answer has not taken effect (the owner 2026-10-20:
    "undo function on approval cards is not working but it is vital that it does" - the Undo on the
    Odette intro-text card was refused at 08:35 for a text due at 09:00, because only the first 30 s
    counted). An approved send can be undone until it actually goes out; a decision, up to
    UNDO_DECISION_MAX_S later, even after the sweep has handed it on (its follow-up is closed or
    reversed). The card comes back open, in the same Chat message, for a new answer."""
    e = _answers_index().get(qid)
    if not e or e.get("undone"):
        return {"ok": False, "reason": "there is no answer on this card to undo"}
    age = time.time() - float(e.get("epoch") or 0)
    if not e.get("ticket") and age > UNDO_DECISION_MAX_S:
        return {"ok": False, "reason": f"that answer is over {UNDO_DECISION_MAX_S // 3600} hours old. Tell me in chat what should change"}
    live = _read(LIVE / f"{qid}.json")
    done = None if isinstance(live, dict) else _read(DONE / f"{qid}.json")
    card = live if isinstance(live, dict) else done
    if isinstance(done, dict) and done.get("status") != "consumed":
        return {"ok": False, "reason": f"this card is {done.get('status') or 'closed'} now, so there is nothing to reopen"}
    ok, why, recalled = _took_back_ticket(e)
    if not ok:
        return {"ok": False, "reason": why}
    follow = _undo_followup(qid, done, e, by) if isinstance(done, dict) else ""
    now = time.time()
    _append(ANSWERS, {"ts": time.strftime("%Y-%m-%dT%H:%M:%S"), "epoch": now, "qid": qid, "undone": True, "by": str(by)[:60]})
    if isinstance(card, dict):
        card["status"] = "open"
        card.pop("retired_ts", None)
        card["undo_count"] = int(card.get("undo_count") or 0) + 1
        card["round"] = int(card.get("round") or 0) + 1   # the next answer's operator ticket gets its own id
        if isinstance(done, dict):
            card["ts"] = max(float(card.get("ts") or 0), now - float(card.get("wait_s") or DECISION_EXPIRES_S) + 6 * 3600)
        _write(LIVE / f"{qid}.json", card)
        if isinstance(done, dict):
            try:
                (DONE / f"{qid}.json").unlink(missing_ok=True)
            except OSError:
                pass
        if not str(by).startswith("gchat:"):          # a Chat tap redraws its own message in the bridge
            _push("mark_undone", card)
    es.ledger("card_undo", qid=qid, ticket=e.get("ticket", ""), by=str(by)[:60], age_s=int(age),
              recalled=recalled, reopened=isinstance(done, dict), followup=follow)
    return {"ok": True, "qid": qid, "cancelled_ticket": e.get("ticket"), "recalled": recalled, "followup": follow}


# ------------------------------------------------------------------ harvest / pending / expire
def _live(owner=""):
    out = []
    try:
        files = list(LIVE.glob("q*.json"))
    except OSError:
        return out
    for f in files:
        c = _read(f)
        if isinstance(c, dict) and c.get("id") and (not owner or c.get("owner") == owner):
            out.append(c)
    out.sort(key=lambda c: float(c.get("ts") or 0))
    return out


def answer_of(qid) -> str:
    """The option the owner chose on a card, '' when unanswered or undone (the last entry wins)."""
    e = _answers_index().get(str(qid or "")) if qid else None
    return "" if not e or e.get("undone") else str(e.get("option") or "")


def close_card(qid, why="done"):
    """Retire an OPEN card that no longer needs an answer because the thing it was about happened by
    itself (a previewed standing text went out at its time). Answered cards are left alone. -> bool"""
    card = _read(LIVE / f"{qid}.json")
    if not isinstance(card, dict) or card.get("status") != "open":
        return False
    card["resolved_why"] = str(why or "")[:200]
    es.ledger("card_resolved", qid=card["id"], why=card["resolved_why"])
    _retire(card, "resolved")
    return True


def supersede(old_qid, new_qid, by="agent"):
    """An open card replaced by a revised one (asked for in its Chat thread). The old card is
    retired as 'superseded' and its Chat message says so; nothing is answered or sent."""
    card = _read(LIVE / f"{old_qid}.json")
    if not card or card.get("status") != "open":
        return False
    card["superseded_by"] = new_qid
    es.ledger("card_superseded", qid=old_qid, by=by, new=new_qid)
    _retire(card, "superseded")
    return True


def _retire(card, status):
    card["status"], card["retired_ts"] = status, time.time()
    _write(DONE / f"{card['id']}.json", card)
    if status in ("expired", "resolved", "superseded"):   # a consumed card already shows its answer in Chat
        _push("mark_closed", card, status)
    try:
        (LIVE / f"{card['id']}.json").unlink(missing_ok=True)
    except OSError:
        pass


def harvest(owner=""):
    idx, now, out = _answers_index(), time.time(), []
    for c in _live(owner):
        e = idx.get(c["id"])
        if not e or e.get("undone"):
            continue
        if now - float(e.get("epoch") or 0) < UNDO_S:
            continue                              # still inside the undo window
        q = (c.get("questions") or [{}])[int(e.get("q") or 0)]
        out.append({"qid": c["id"], "header": q.get("header", ""), "lane": q.get("lane", ""),
                    "age_min": round((now - float(c.get("ts") or 0)) / 60, 1),
                    "answer": e.get("option"), "text": e.get("text", ""), "revised": e.get("revised", False),
                    "exec": bool(q.get("exec")), "n_options": len(q.get("options") or []),
                    "revised_text": e.get("revised_text", ""), "draft_text": _draft_text(q),
                    "ticket": e.get("ticket"), "mint_error": e.get("mint_error"), "question": q.get("question", "")[:400],
                    "round": int(c.get("round") or 0)})
        _retire(c, "consumed")
    return out


def _card_image(c):
    """The doer's review-page screenshot, served by Mission Control (/api/doer/shot/...)."""
    ex = (c.get("questions") or [{}])[0].get("exec") or {}
    p = ex.get("payload") or {}
    if ex.get("kind") == "agent_run" and p.get("screenshot") and p.get("ticket"):
        name = re.sub(r"[^A-Za-z0-9._-]", "", str(p["screenshot"]).replace("\\", "/").rsplit("/", 1)[-1])
        return f"/api/doer/shot/{p['ticket']}/{name}" if name else ""
    return ""


def pending(owner=""):
    now = time.time()
    return [{"qid": c["id"], "owner": c.get("owner"), "age_min": round((now - float(c.get("ts") or 0)) / 60, 1),
             "status": c.get("status"), "lane": (c.get("questions") or [{}])[0].get("lane", ""),
             "header": (c.get("questions") or [{}])[0].get("header", ""),
             "question": (c.get("questions") or [{}])[0].get("question", ""),
             "options": [o.get("label") for o in (c.get("questions") or [{}])[0].get("options") or []],
             "exec": bool((c.get("questions") or [{}])[0].get("exec")),
             "exec_kind": ((c.get("questions") or [{}])[0].get("exec") or {}).get("kind", ""),
             "image": _card_image(c),
             "delivery": str(c.get("delivery") or "now"), "held": c.get("delivery") in ("weekly", "mc"),
             "digest_count": int(c.get("digest_count") or 0), "urgent": bool(c.get("urgent")),
             "reopened": int(c.get("reopen_count") or 0)}
            for c in _live(owner) if c.get("status") == "open"]


def resolve_alarms():
    """Retire every open alarm card whose clears_when probe passes. -> how many."""
    n = 0
    for c in _live():
        if c.get("status") != "open":
            continue
        q = (c.get("questions") or [{}])[0]
        why = _probe_clear(q, float(c.get("ts") or 0))
        if why:
            c["resolved_why"] = why
            _retire(c, "resolved")
            es.ledger("card_resolved", qid=c["id"], why=why[:200])
            n += 1
    return n


def expire():
    now, n = time.time(), 0
    for c in _live():
        if c.get("status") != "open":
            continue
        if c.get("delivery") == "weekly":
            dead = digest_expired(c, now)
        else:
            dead = now - float(c.get("ts") or 0) > float(c.get("wait_s") or DEFAULT_EXPIRES_S)
        if dead:
            _retire(c, "expired")
            n += 1
    return n


def _same_failure(a: str, b: str) -> bool:
    """Two failure lines that say the same thing (ids, numbers and timestamps aside)."""
    def norm(s):
        s = re.sub(r"\b[0-9a-f]{8,}\b|\btk-[0-9a-f]+\b|\d{1,2}:\d{2}(?::\d{2})?|\d+(?:\.\d+)?\s*(?:min|s|h|ms)\b|\d{4}-\d{2}-\d{2}", " ", str(s or "").lower())
        return re.sub(r"[^a-z]+", " ", s).strip()
    x, y = norm(a), norm(b)
    return bool(x) and bool(y) and (x == y or x[:60] == y[:60])


def reopen(qid, why, by="drain", resend=False):
    """'A decision is the final close to each unless they are not confirmed done' (the owner 2026-10-20):
    a consumed card whose approved action provably did NOT happen (drain: a PERMANENT failure before
    anything was handed over) comes back as the SAME card, open again, its question headed by what went
    wrong, so a tap retries it. Its old answer is voided (answers.jsonl, like an undo) so nothing
    harvests it twice and ALREADY_ANSWERED does not stand in the way. An urgent card goes straight back
    to Chat; a weekly one waits for the next digest. At most REOPEN_MAX times. Never for an UNVERIFIED
    result (the message may have landed; the operator checks before any re-card)."""
    card = _read(DONE / f"{qid}.json")
    if not isinstance(card, dict) or card.get("status") != "consumed":
        return {"ok": False, "reason": "not a consumed card"}
    if _read(LIVE / f"{qid}.json"):
        return {"ok": False, "reason": "already open"}
    n = int(card.get("reopen_count") or 0) + 1
    if n > REOPEN_MAX:
        return {"ok": False, "reason": f"came back {REOPEN_MAX} times already; a fresh card is the operator's call"}
    if n > 1 and _same_failure(str(card.get("reopened_why") or ""), str(why or "")):
        # the same failure twice (the reflex, 2026-10-27: an email card came back "NOT_CONFIGURED smtp" three mornings
        # running, each tap failing the same way): never a third identical try; the caller says the portal once
        return {"ok": False, "reason": "SAME_FAILURE: " + str(why or "")[:160], "same": True}
    q = (card.get("questions") or [{}])[0]
    orig = str(q.get("question_orig") or q.get("question") or "")
    q["question_orig"] = orig
    q["retry_note"] = (f"Not confirmed done last time ({str(why or 'no result')[:160]}). "
                       + ("Tap again to RESEND it." if resend else "Tap again to retry."))   # RESEND lifts the same-text guard at mint
    q["question"] = q["retry_note"] + "\n" + orig
    now = time.time()
    if card.get("chat"):
        # the old message must not keep saying "goes out in about 35 s" above the card that comes back (2026-10-21)
        _push("mark_closed", card, "not confirmed done; the card is posted again below")
    card.update(status="open", ts=now, reopen_count=n, reopened_why=str(why or "")[:200], chat={})
    card.pop("retired_ts", None)
    if card.get("delivery") == "weekly":
        card.update(held_ts=now, digest_count=0)
        card.pop("digest_ts", None)
    _append(ANSWERS, {"ts": time.strftime("%Y-%m-%dT%H:%M:%S"), "epoch": now, "qid": qid, "undone": True,
                      "reopened": True, "by": str(by)[:60]})
    _write(ARCHIVE / f"{qid}.json", card)
    _write(LIVE / f"{qid}.json", card)
    try:
        (DONE / f"{qid}.json").unlink(missing_ok=True)
    except OSError:
        pass
    es.ledger("card_reopen", qid=qid, n=n, why=str(why or "")[:160], delivery=str(card.get("delivery") or "now"))
    if card.get("delivery") not in ("weekly", "mc"):
        chat = _push("push_card", card)
        if chat:
            card["chat"] = chat
            _write(LIVE / f"{qid}.json", card)
    _push("record_card", card)
    return {"ok": True, "qid": qid, "n": n, "delivery": str(card.get("delivery") or "now")}


LEARN_DIR = Path(os.environ.get("ASSISTANT_LEARNINGS") or (HERE.parent / "learnings"))


def _draft_text(q):
    ex = q.get("exec") or {}
    pay = ex.get("payload") if isinstance(ex, dict) else None
    if isinstance(pay, dict):
        return next((str(pay[k]) for k in _EXEC_TEXT_KEYS if pay.get(k)), "")
    return ""


def record_correction(h):
    """Every edit or typed answer is a correction worth remembering: append the RAW signal to
    assistant/learnings/<lane>.md (scripts/lib/learnings.py: deduped, capped at 50). The
    operator distills it into a generalizable lesson when it works the card_answer ticket
    (learn.py record); dreaming reads both. Never blocks a sweep."""
    try:
        sys.path.insert(0, str(HERE.parent.parent / "scripts" / "lib"))
        import learnings
        lane = h.get("lane") or "personal"
        if h.get("revised_text"):
            lesson = f"[raw edit on '{h.get('header') or h['qid']}'] owner1 changed the draft to: {h['revised_text'][:220]}"
        else:
            # a typed answer is usually a one-off fact ("not booked", "I will clear it later"),
            # not a drafting lesson; the operator distils real lessons with learn.py when it
            # works the card_answer ticket. Only an EDITED draft is recorded raw.
            return False
        return learnings.record_lesson(LEARN_DIR / f"{lane}.md", lesson, context=h["qid"], title=f"{lane} lane learnings")
    except Exception:
        return False


def sweep():
    """The zero-LLM tick step: expire dead cards, harvest every answered card, and turn
    the answers that need a HUMAN-LIKE follow-up (typed free text, an exec that failed to
    mint) into assistant tickets so the operator acts on them. Plain taps that minted a
    ticket need nothing more: drain.py runs them."""
    expired = expire()
    resolved = resolve_alarms()
    items = []
    learned = 0
    for h in harvest():
        if record_correction(h):
            learned += 1
        if _act_on_setup(h):
            continue                                  # a connection card: code did what the button says
        if _act_on_build(h):
            continue                                  # a build card: the builder took the owner's decision
        if _act_on_pending(h):
            continue                                  # an approval-queue card: pending.answer did what the button says
        if _act_on_capability(h):
            continue                                  # a 'Build stuck' card (capability.py): another way refiles, drop closes
        if _act_on_hold(h):
            continue                                  # a standing-text preview: Hold pulled it back as a send card
        if _act_on_outcome(h):
            continue                                  # a "did this land?" card: the answer is the outcome's evidence
        if _act_on_breaker(h):
            continue                                  # a paused channel: Try again now resets the breaker
        if _act_on_sentinel(h):
            continue                                  # a machinery finding: the button ran the job / sensor / task
        if _act_on_graduation(h):
            continue                                  # "make it automatic?": the owner's tap flips the gate
        # A tap on a DECISION card (2+ options, no exec) is an instruction: "start prep", "not
        # booked", "wait a week". Nothing else reads answers.jsonl, so without a ticket the
        # decision was recorded and then never acted on. A single-option ack ("Noted") and a
        # plain exec tap (drain runs the ticket, or Skip means nothing) need no follow-up.
        decision = (not h.get("exec") and int(h.get("n_options") or 0) >= 2
                    and not _NO_ACTION.fullmatch(str(h.get("answer") or "").strip()))
        # A Skip on a drafted TEXT/EMAIL is feedback too (the owner 2026-10-06, "it should use better
        # judgement about what to respond to"): the operator distils why into the lane's learnings.
        skipped = bool(h.get("exec")) and str(h.get("answer") or "").strip().lower() == "skip"
        need = bool(h.get("text")) or bool(h.get("mint_error")) or bool(h.get("revised_text")) or decision or skipped
        if not need:
            continue
        items.append({"source": "card", "source_id": h["qid"] if not h.get("round") else f"{h['qid']}#{h['round']}", "lane": h.get("lane") or "personal",
                      "kind": "card_answer", "title": f"owner1 answered '{h['answer']}' on: {h['header'] or h['qid']}",
                      "summary": (f"He chose '{h['answer']}' on a decision card: carry out what that option means (or close this if it means no action). " if decision else "") +
                                 (f"He typed: {h['text'][:300]}" if h.get("text") else "") +
                                 (f" | he EDITED the draft (ticket {h.get('ticket')} carries his version); distill the lesson with learn.py" if h.get("revised_text") else "") +
                                 (" | he SKIPPED this draft: decide whether this kind of message should not have been drafted (or the wording was off) and record ONE lesson with learn.py; no card back" if skipped else "") +
                                 (f" | mint error: {h['mint_error'][:200]}" if h.get("mint_error") else ""),
                      "payload": h})
    # harvest() has already retired these cards, so an answer whose ticket cannot be queued
    # right now (QUEUE_BUSY) is parked in a spill file and retried on the next sweep instead
    # of being dropped. add_many dedupes on source+source_id, so a retry never doubles.
    # The parked items are CLAIMED (moved aside under the spill lock) before add_many, which
    # can wait 20 s on a busy queue: an item parked during that wait (an inbound text) lands
    # in a fresh spill file and is neither deleted nor overwritten by this sweep.
    items = _take_spill(items)
    added = 0
    if items:
        # harvest() already retired these cards: until add_many returns, the claim file is the
        # only copy of the owner's answers. A crash or an exception in here leaves the claim on disk
        # and the next sweep picks it up.
        try:
            import tickets
            added = tickets.add_many(items, "cards.sweep")
        except Exception:
            added = -1
        if added < 0:
            spill_items(items)
        _drop_taken()
    closed = close_answered()
    return {"expired": expired, "resolved": resolved, "queued": max(added, 0), "learned": learned, "closed": closed,
            "open": len(_live())}


SETUP_NOW, SETUP_LEAVE, SETUP_ELSE = "Set it up now (Recommended)", "Leave it off for now", "Something else"


def connection_card(cid: str, name: str, detail: str, renew: str, how: str = "") -> dict:
    """The alarm card for a connection that stopped working, with buttons that DO something
    (the owner 2026-10-17: "if it says bank feed is down, then it should have a button that says let's
    set it up now, or drop it, or I'd like to do something else"). Posted by sentinels.run by code,
    answered by code in the sweep (_act_on_setup); clears itself when conn_check proves it live."""
    lane = "money" if cid.startswith("bank-") else ("owner6" if cid.endswith("owner6") else "personal")
    return {"header": f"{name} down", "lane": lane, "clears_when": {"connection": cid}, "setup_rid": cid,
            "question": f"{name} has stopped working ({detail.rstrip('.')}). Set it back up now? I'll walk you through it here, step by step.",
            "details": (f"How it is set up: {how}\n\n" if how else "") + (f"Renew: {renew}" if renew else ""),
            "advice": "💡 Set it up now: the steps come to this chat, nothing else changes. Leave it off: I stay quiet about it for a few days.",
            "options": [{"label": SETUP_NOW, "description": "the steps come here"},
                        {"label": SETUP_LEAVE, "description": "no nagging for a few days"},
                        {"label": SETUP_ELSE, "description": "tell me here what you'd rather do"}]}


def _act_on_setup(h) -> bool:
    """The connection card's buttons, done by code: the setup steps (connect.py's chat steps, else
    setup_actions' recipe) land in chat as Sage's words; 'Leave it off' latches the sentinel; 'Something
    else' asks in chat. True when this answer needed no operator ticket."""
    c = _read(DONE / f"{h['qid']}.json")
    q = (c.get("questions") or [{}])[0] if isinstance(c, dict) else {}
    rid = str(q.get("setup_rid") or "")
    if not rid:
        return False
    ans = _squash(h.get("answer"))
    lane = str(q.get("lane") or "personal")
    name = str(q.get("header") or rid).replace(" down", "")
    try:
        if ans.startswith("set it up"):
            text = ""
            try:
                import connect
                text = connect.steps(rid)
            except Exception:  # noqa: BLE001
                text = ""
            if text:
                hint = connect.paste_hint(rid)
                text += ("\n" + hint) if hint else ""
            else:
                import setup_actions
                r = setup_actions.run(rid, by="card")
                text = f"{name}: {r.get('message') or 'started the setup step on the PC.'}"
            say("cards.sweep", lane, text)
        elif ans.startswith("leave it"):
            try:
                import sentinels
                latch = sentinels.load_json(sentinels.LATCH, {})
                latch[f"conn:{rid}"] = time.time() + 5 * 86400       # the re-ping clock starts 5 days out
                sentinels.save_json(sentinels.LATCH, latch)
            except Exception:  # noqa: BLE001
                pass
            say("cards.sweep", lane, f"Okay, {name} stays off for now. I'll mention it again in about a week if it's still down.")
        elif ans.startswith("something else"):
            say("cards.sweep", lane, f"Tell me here what you'd like done about {name} instead, and I'll take it from there.")
        else:
            return False
    except Exception:  # noqa: BLE001
        return False
    es.ledger("card_setup_answer", qid=h["qid"], rid=rid, option=str(h.get("answer") or "")[:60])
    return True


def _act_on_build(h) -> bool:
    """A 'Build needs your OK' card (builder.ask_owner): the tap goes straight to builder.owner_answer,
    which re-arms or drops the request and kicks the builder. True when no operator ticket is needed."""
    c = _read(DONE / f"{h['qid']}.json")
    q = (c.get("questions") or [{}])[0] if isinstance(c, dict) else {}
    key = str(q.get("build_key") or "")
    if not key:
        return False
    try:
        sys.path.insert(0, str(HERE.parent / "agent"))
        import builder
        r = builder.owner_answer(key, str(h.get("answer") or ""), by=str(h.get("by") or "owner"))
    except Exception as e:  # noqa: BLE001
        es.ledger("card_build_answer_error", qid=h["qid"], key=key, error=f"{type(e).__name__}: {str(e)[:160]}")
        return False
    es.ledger("card_build_answer", qid=h["qid"], key=key, option=str(h.get("answer") or "")[:60], result=str(r.get("status") or r.get("reason") or "")[:80])
    if r.get("ok"):
        lane = str(q.get("lane") or "personal")
        said = {"approved": "Got it, building it now with your OK. You'll hear from me here when it lands, with an undo.",
                "retry": "Understood, I'll find another way that leaves those files alone and tell you here how it went.",
                "dropped": "Dropped; I won't bring that one up again."}.get(r.get("status"), "")
        if said:
            say("builder", lane, said)
    return bool(r.get("ok"))


def _act_on_capability(h) -> bool:
    """A 'Build stuck' card from capability.py (an ask whose builds kept failing): the tap goes to
    capability.owner_answer; 'Try it another way' files a fresh build that knows what was tried (and the
    owner's words in the thread, if any), 'Drop it' closes the gap. True when no operator ticket is needed."""
    c = _read(DONE / f"{h['qid']}.json")
    q = (c.get("questions") or [{}])[0] if isinstance(c, dict) else {}
    gid = str(q.get("capability_gap") or "")
    if not gid:
        return False
    try:
        sys.path.insert(0, str(HERE.parent / "agent"))
        import capability
        r = capability.owner_answer(gid, str(h.get("answer") or ""), by=str(h.get("by") or "owner"), text=str(h.get("text") or ""))
    except Exception as e:  # noqa: BLE001
        es.ledger("card_capability_answer_error", qid=h["qid"], gap=gid, error=f"{type(e).__name__}: {str(e)[:160]}")
        return False
    es.ledger("card_capability_answer", qid=h["qid"], gap=gid, option=str(h.get("answer") or "")[:60], result=str(r.get("status") or r.get("reason") or "")[:80])
    return bool(r.get("ok"))


def _act_on_pending(h) -> bool:
    """A card from the owners' approval queue (agent/pending.py: a protected rule, a rule retirement,
    a review ask, a protected-core patch). The tap goes to pending.answer, which does what the button
    says and takes the entry out of Memory/skill-suggestions.md ## Pending. True when handled."""
    c = _read(DONE / f"{h['qid']}.json")
    q = (c.get("questions") or [{}])[0] if isinstance(c, dict) else {}
    key = str(q.get("pending_key") or "")
    if not key:
        return False
    try:
        sys.path.insert(0, str(HERE.parent / "agent"))
        import pending
        r = pending.answer(key, str(h.get("answer") or ""), text=str(h.get("text") or ""), by=str(h.get("by") or "owner1"))
    except Exception as e:  # noqa: BLE001
        es.ledger("card_pending_answer_error", qid=h["qid"], key=key, error=f"{type(e).__name__}: {str(e)[:160]}")
        return False
    es.ledger("card_pending_answer", qid=h["qid"], key=key, option=str(h.get("answer") or "")[:60], result=str(r.get("status") or r.get("reason") or "")[:80])
    return bool(r.get("ok")) or r.get("reason") == "no longer pending"


def _act_on_hold(h) -> bool:
    """A standing-text preview card (client_texts._preview_card, `hold_text` = the text's key): Hold
    takes the text off the standing path and posts it as a normal send card for the owner's tap;
    'Let it go as planned' needs nothing (release_due sends it at its time). True when this answer
    needed no operator ticket (typed text still goes to the operator)."""
    c = _read(DONE / f"{h['qid']}.json")
    q = (c.get("questions") or [{}])[0] if isinstance(c, dict) else {}
    key = str(q.get("hold_text") or "")
    if not key:
        return False
    ans = _squash(h.get("answer"))
    if ans.startswith("hold"):
        try:
            import client_texts
            r = client_texts.hold(key, by=str(h.get("by") or "owner1"))
        except Exception as e:  # noqa: BLE001
            es.ledger("card_hold_error", qid=h["qid"], key=key, error=f"{type(e).__name__}: {str(e)[:160]}")
            return False
        es.ledger("card_hold", qid=h["qid"], key=key, result=str(r.get("card") or r.get("reason") or "")[:80])
    return not h.get("text")


def breaker_card(lane: str, fails: int, held: int) -> dict:
    """ONE card by code when a send channel trips its breaker (drain._breaker_note): what is paused, how many
    wait, and buttons that act. Clears itself when the channel works again (clears_when breaker)."""
    names = {"email": "Email", "phone": "Texts (the phone outbox)", "sheets": "Google Sheets",
             "ads": "Google Ads", "doer": "The doer", "local": "Local jobs",
             crm.CHANNEL: crm.name()}
    name = names.get(lane, lane)
    q = {"header": f"{name} paused"[:60], "lane": "personal", "audience": "personal", "urgent": True,
         "question": (f"{name} failed {fails} times in a row, so I have paused that channel: {held} approved item{'s' if held != 1 else ''} "
                      f"wait untouched. I try one every 10 minutes and resume on the first success."),
         "options": [{"label": "Try again now (Recommended)", "description": "close the breaker and run the waiting items on the next pass"},
                     {"label": "Leave it until it recovers", "description": "keep probing one item every 10 minutes"}],
         "clears_when": {"breaker": lane}, "breaker_lane": lane}
    return post([q], "drain", lane="personal")


def _act_on_breaker(h) -> bool:
    c = _read(DONE / f"{h['qid']}.json")
    q = (c.get("questions") or [{}])[0] if isinstance(c, dict) else {}
    lane = str(q.get("breaker_lane") or "")
    if not lane:
        return False
    ans = _squash(h.get("answer"))
    if ans.startswith("try again"):
        try:
            import drain
            drain.breaker_reset(lane, by=str(h.get("by") or "owner"))
            say("cards.sweep", "personal", f"Okay, trying {lane} again on the next pass.")
        except Exception as e:  # noqa: BLE001
            es.ledger("card_breaker_error", qid=h["qid"], lane=lane, error=f"{type(e).__name__}: {str(e)[:120]}")
            return False
    es.ledger("card_breaker_answer", qid=h["qid"], lane=lane, option=str(h.get("answer") or "")[:60])
    return True


def sentinel_card(f: dict, options: list, lane: str = "personal") -> dict:
    """A machinery / sensor / exec finding as ONE card by code (WP2; until now a stuck_work ticket waited for an LLM
    operator run): the finding's key is the probe, so the card clears itself when the check passes again
    (clears_when sentinel) and DUPLICATE_ALARM keeps it to one open card per finding."""
    q = {"header": str(f.get("header") or f.get("text") or "Something stopped")[:60], "lane": lane, "audience": "personal",
         "urgent": True, "question": str(f.get("text") or "")[:600], "options": options,
         "clears_when": {"sentinel": str(f.get("key") or "")}, "sentinel_key": str(f.get("key") or ""), "sentinel_act": f.get("act") or {}}
    return post([q], "sentinels", lane=lane)


def _act_on_sentinel(h) -> bool:
    """The sentinel card's buttons, done by code: run the spine job / sensor / scheduled task now, run the drain,
    cancel a stranded ticket, hand an exception to Sage, or leave it (latched 5 days). True when no operator
    ticket is needed."""
    c = _read(DONE / f"{h['qid']}.json")
    q = (c.get("questions") or [{}])[0] if isinstance(c, dict) else {}
    key = str(q.get("sentinel_key") or "")
    if not key:
        return False
    ans = _squash(h.get("answer"))
    act = q.get("sentinel_act") or {}
    lane = str(q.get("lane") or "personal")
    try:
        import sentinels
        if ans.startswith("leave") or ans.startswith("drop"):
            latch = sentinels.load_json(sentinels.LATCH, {})
            latch[key] = time.time() + 5 * 86400
            sentinels.save_json(sentinels.LATCH, latch)
            say("cards.sweep", lane, "Okay, leaving that one; I'll mention it again in about a week if it is still so.")
        elif ans.startswith("cancel") and act.get("ticket"):
            say("cards.sweep", lane, sentinels.act_cancel(str(act["ticket"]), by=str(h.get("by") or "owner")))
        elif ans.startswith("hand it"):
            import tickets
            tickets.add_many([{"source": "sentinel-card", "source_id": key, "lane": lane, "kind": "stuck_work",
                               "title": str(q.get("header") or "stuck item"), "summary": str(q.get("question") or "")[:600],
                               "payload": {"items": [{"key": key, "text": str(q.get("question") or "")}]}}], "cards.sweep")
            say("cards.sweep", lane, "Handing that to Sage to work through; it comes back here with what was done.")
        else:
            r = sentinels.act(act, by=str(h.get("by") or "owner"))
            say("cards.sweep", lane, r or "Started it.")
    except Exception as e:  # noqa: BLE001
        es.ledger("card_sentinel_error", qid=h["qid"], key=key, error=f"{type(e).__name__}: {str(e)[:160]}")
        return False
    es.ledger("card_sentinel_answer", qid=h["qid"], key=key, option=str(h.get("answer") or "")[:60])
    return True


def _act_on_graduation(h) -> bool:
    c = _read(DONE / f"{h['qid']}.json")
    q = (c.get("questions") or [{}])[0] if isinstance(c, dict) else {}
    kind = str(q.get("graduation_class") or "")
    if not kind:
        return False
    try:
        import graduation
        r = graduation.answer(kind, str(h.get("answer") or ""), by=str(h.get("by") or "owner1"))
        say("cards.sweep", str(q.get("lane") or "wedding"),
            (f"Okay, {kind.replace('_', ' ')} is automatic from now on; it stays in the ledger and comes back to a card on the first failure."
             if r.get("granted") else f"Okay, I'll keep asking for {kind.replace('_', ' ')}."))
    except Exception as e:  # noqa: BLE001
        es.ledger("card_graduation_error", qid=h["qid"], kind=kind, error=f"{type(e).__name__}: {str(e)[:120]}")
        return False
    es.ledger("card_graduation_answer", qid=h["qid"], kind=kind, option=str(h.get("answer") or "")[:60])
    return True


def _act_on_outcome(h) -> bool:
    """A card the outcome ledger posted (outcomes.ask_owner, `outcome_id`): the owner's answer is the evidence
    the record could not give (It went / Send it again / Drop it). True when no operator ticket is needed."""
    c = _read(DONE / f"{h['qid']}.json")
    q = (c.get("questions") or [{}])[0] if isinstance(c, dict) else {}
    oid = str(q.get("outcome_id") or "")
    if not oid:
        return False
    try:
        import outcomes
        r = outcomes.answer(oid, str(h.get("answer") or ""), by=str(h.get("by") or "owner1"))
    except Exception as e:  # noqa: BLE001
        es.ledger("card_outcome_answer_error", qid=h["qid"], oid=oid, error=f"{type(e).__name__}: {str(e)[:160]}")
        return False
    es.ledger("card_outcome_answer", qid=h["qid"], oid=oid, option=str(h.get("answer") or "")[:60], result=str(r.get("state") or r.get("reason") or "")[:80])
    return bool(r.get("ok")) and not h.get("text")


def _outcome_state(ticket_id: str) -> str:
    """The ledger's state for an exec ticket ('' when the ticket predates the ledger)."""
    try:
        import outcomes
        return outcomes.state_for_ticket(str(ticket_id or ""))
    except Exception:  # noqa: BLE001
        return ""


# option labels that mean "do nothing": tapping one needs no operator pass. The WHOLE label
# must be one of these (plus an optional "(Recommended...)" tag): "Wait" is no action, but
# "Wait a week, then nudge" and "Done - mark delivered" are decisions and get a ticket.
_NO_ACTION = re.compile(r"(skip|noted|got it|ok(ay)?|dismiss|ignore|no action|nothing|not now|later|wait|"
                        r"remind me later|leave it|i'?ll handle it|i'?ll do it|done|already done)"
                        r"[\s.!]*(\(recommended[^)]*\))?[\s.!]*", re.I)

SPILL_TAKEN = ROOT / "unqueued_answers.taken.json"   # items a sweep has claimed and not yet queued
SPILL_LOCK = ROOT / "unqueued_answers.lock"


class _SpillLock:
    """Short cross-process mutex for the spill files (mc_server threads park items, the spine's
    sweep claims them). On a 5 s timeout it proceeds unlocked: parking the item matters more."""

    def __enter__(self):
        self.held = False
        SPILL_LOCK.parent.mkdir(parents=True, exist_ok=True)
        end = time.time() + 5
        while time.time() < end:
            try:
                os.close(os.open(SPILL_LOCK, os.O_CREAT | os.O_EXCL | os.O_WRONLY))
                self.held = True
                break
            except FileExistsError:
                try:
                    if time.time() - SPILL_LOCK.stat().st_mtime > 30:   # a holder that died
                        SPILL_LOCK.unlink()
                        continue
                except OSError:
                    pass
            except OSError:
                pass
            time.sleep(0.05)
        return self

    def __exit__(self, *exc):
        if self.held:
            try:
                SPILL_LOCK.unlink()
            except OSError:
                pass


def _take_spill(fresh=()) -> list:
    """Claim every parked item for this sweep (plus any a dead sweep left claimed, plus the
    answers this sweep just harvested): one claim file holds them all until they are queued."""
    with _SpillLock():
        left, cur = _read(SPILL_TAKEN), _read(SPILL)
        items = (left if isinstance(left, list) else []) + (cur if isinstance(cur, list) else [])
        singles = []
        try:
            singles = [f for f in ROOT.glob("unqueued_answers.one-*.json")]
        except OSError:
            pass
        for f in singles:                       # items parked one-per-file (see spill_items)
            it = _read(f)
            if isinstance(it, list):
                items += it
        items += list(fresh)
        if items:
            _write(SPILL_TAKEN, items)
            if isinstance(cur, list):           # an unreadable spill file is left for the next sweep, never deleted
                try:
                    SPILL.unlink(missing_ok=True)
                except OSError:
                    pass
            for f in singles:
                if isinstance(_read(f), list):
                    try:
                        f.unlink(missing_ok=True)
                    except OSError:
                        pass
        return items


def _drop_taken():
    with _SpillLock():
        try:
            SPILL_TAKEN.unlink(missing_ok=True)
        except OSError:
            pass


def spill_items(items):
    """Park ticket items that could not be queued (QUEUE_BUSY); the next sweep queues them."""
    with _SpillLock() as lk:
        cur = _read(SPILL)
        if lk.held and (isinstance(cur, list) or not SPILL.exists()):
            _write(SPILL, (cur or []) + list(items))
            return
    # Without the lock, or when the spill file exists but could not be read just now (antivirus,
    # indexer), appending would be a read-modify-write that can lose what is already parked.
    # A file of its own cannot collide with anything; the next sweep collects it.
    _write(ROOT / f"unqueued_answers.one-{uuid.uuid4().hex}.json", list(items))


_CARD_NOTE = re.compile(r"\bcard (q[0-9a-f]{12})\b")
NO_CARD_DAYS = 2
EXPIRED_NOTE = "card {qid} expired unanswered: if this is still true post ONE fresh card, else done/dismiss"
RETRIAGE_NOTE = "staged with no card and no draft: re-triage - post ONE card (decision or FYI digest) or dismiss"
BLOCKED_NOTE = "blocked by the doer; the owner already has the link and the steps (portal rule 2026-10-20), not re-queued"


def close_answered():
    """A ticket staged with the note 'card <qid>' is waiting on that card. Once the owner has
    answered the card (it is in superseded/ as 'consumed') the ticket is finished: an approved
    exec is drain's job, typed text became its own card_answer ticket. Without this the
    ticket stayed 'staged' forever and resurfaced as stuck work 5 days later (2026-10-01:
    five tickets still staged for cards answered the day before). A ticket staged with
    NEITHER a card nor a draft goes back to the operator once after NO_CARD_DAYS. An expired,
    unanswered card closes nothing. Idempotent; runs every sweep."""
    import tickets
    n = 0
    try:
        db = tickets._load()
    except Exception:
        return 0
    answers = _answers_index()
    for t in list(db.values()):
        if t.get("status") != "staged":
            continue
        note = next((h.get("note", "") for h in reversed(t.get("history") or []) if h.get("to") == "staged"), "")
        m = _CARD_NOTE.search(note)
        again = any(h.get("by") == "cards.sweep" and h.get("to") == "new" for h in t.get("history") or [])
        if m:
            c = _read(DONE / f"{m.group(1)}.json")
            if isinstance(c, dict) and c.get("status") == "consumed":
                a = answers.get(m.group(1)) or {}
                ans, tk = a.get("option", ""), str(a.get("ticket") or "")
                ostate = _outcome_state(tk) if tk else ""
                if ostate and ostate not in ("verified", "failed", "abandoned"):
                    continue                          # the action is in flight / unconfirmed: the ledger closes it (2026-10-24)
                if ostate == "failed" and _read(LIVE / f"{m.group(1)}.json"):
                    continue                          # the card came back for a retry; the ticket stays staged on it
                note = f"card {m.group(1)} answered: {ans}" + (f"; outcome {ostate}" if ostate else "")
                if tickets._transition(t["id"], "done", "cards.sweep", note[:200], {"staged"}):
                    n += 1
            elif isinstance(c, dict) and c.get("status") == "resolved":
                # an alarm card that cleared itself: the fault is gone, nothing left to do
                if tickets._transition(t["id"], "done", "cards.sweep", f"card {m.group(1)} resolved: {c.get('resolved_why', '')}"[:200], {"staged"}):
                    n += 1
            elif isinstance(c, dict) and c.get("status") == "expired" and c.get("delivery") == "weekly" and int(c.get("digest_count") or 0) >= 1:
                # it had its weekly digests and was not decided: dropped, never re-asked (the owner 2026-10-20)
                if tickets._transition(t["id"], "done", "cards.sweep", f"card {m.group(1)} had its weekly digests unanswered: dropped", {"staged"}):
                    n += 1
            elif isinstance(c, dict) and c.get("status") == "expired" and not again:
                # the card timed out unanswered, so the decision is no longer on the owner's phone.
                # Hand the ticket back ONCE to re-post it if it is still true.
                if tickets._transition(t["id"], "new", "cards.sweep", EXPIRED_NOTE.format(qid=m.group(1)), {"staged"}):
                    n += 1
        elif re.search(r"\b(asked|answered|said) in chat\b", note, re.I) and t.get("updated") and tickets._age_s(t["updated"]) > CHAT_ASK_H * 3600:
            # staged as "asked in chat": the owner's reply, or their silence, is the answer; 27 such tickets had
            # never closed by 2026-10-24 (the outcome ledger's WP1)
            if tickets._transition(t["id"], "done", "cards.sweep", f"asked in chat {CHAT_ASK_H} h ago; nothing else tracks it (closed by the ledger rule)", {"staged"}):
                n += 1
        elif not note.lower().lstrip().startswith("draft in gmail") and t.get("updated") and tickets._age_s(t["updated"]) > NO_CARD_DAYS * 86400:
            # staged with neither a card nor a Gmail draft: nothing can reach the owner's phone and
            # nothing will ever close it. Hand it back to the operator ONCE to post a card or
            # dismiss it; if it comes back card-less again the sentinel reports it.
            if again:
                continue
            if note.lower().lstrip().startswith("blocked:"):
                # the doer stopped and its blocked line already gave the owner the link and the steps
                # (the portal rule, the owner 2026-10-20): never hand it back to be refused again
                if tickets._transition(t["id"], "dismissed", "cards.sweep", BLOCKED_NOTE, {"staged"}):
                    n += 1
                continue
            if tickets._transition(t["id"], "new", "cards.sweep", RETRIAGE_NOTE, {"staged"}):
                n += 1
    _ticket_orphan_expiries(tickets, db)
    return n


def _ticket_orphan_expiries(tickets, db):
    """A card that expired unanswered with NO ticket standing behind it (nothing was staged on
    it) would simply vanish. It becomes a card_expired ticket so the operator re-posts it if
    it is still true. Deduped on the card id; only cards retired in the last 3 days."""
    try:
        referenced = " ".join(h.get("note", "") for t in db.values() for h in t.get("history") or [])
        items, now = [], time.time()
        for f in DONE.glob("q*.json"):
            if now - f.stat().st_mtime > 3 * 86400:
                continue
            c = _read(f)
            if not isinstance(c, dict) or c.get("status") != "expired" or c.get("id", "?") in referenced:
                continue
            q = (c.get("questions") or [{}])[0]
            if q.get("setup_rid") or q.get("clears_when") or q.get("outcome_id"):
                continue              # an alarm card is re-posted by the sentinel itself when still true; an outcome card by outcomes.settle
            if q.get("hold_text"):
                continue              # a standing-text preview: the text went at its time; nothing to re-post
            if c.get("delivery") == "weekly" and int(c.get("digest_count") or 0) >= 1:
                continue              # it had its weekly digests unanswered: dropped for good (the owner 2026-10-20)
            items.append({"source": "card_expired", "source_id": c["id"], "lane": q.get("lane") or "personal", "kind": "card_expired",
                          "title": f"Card expired unanswered: {q.get('header') or c['id']}",
                          "summary": "owner1 never answered this card and nothing else tracks it. If it is still true, post ONE "
                                     "fresh card; otherwise close this. It asked: " + str(q.get("question") or "")[:300],
                          "payload": {"qid": c["id"], "question": str(q.get("question") or "")[:2000], "had_exec": bool(q.get("exec"))}})
        if items:
            tickets.add_many(items, "cards.sweep")
    except Exception:
        pass


# ------------------------------------------------------------------ CLI
def main():
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("post"); p.add_argument("--file", required=True); p.add_argument("--owner", default="operator")
    p.add_argument("--lane", default=""); p.add_argument("--expires", type=int, default=DEFAULT_EXPIRES_S)
    p.add_argument("--thread", default="", help="Google Chat thread the request came in (payload.thread): the card lands there")
    p.add_argument("--space", default="", help="post only into this Chat space (payload.space: the owner's DM the ask came from)")
    p.add_argument("--asked", action="store_true", help="an owner asked for this card in chat: urgent where asked; supersedes the older open card instead of a refusal")
    p.add_argument("--replaces", default="", help="the open card this one revises (its subject guards are skipped)")
    p = sub.add_parser("note"); p.add_argument("text"); p.add_argument("--owner", default="operator")
    p = sub.add_parser("say", help="Sage says one plain line in chat (what is NOT a decision: a fact only the owner has, a thing only he can do)")
    p.add_argument("text"); p.add_argument("--lane", default=""); p.add_argument("--owner", default="operator"); p.add_argument("--thread", default="")
    p = sub.add_parser("pending"); p.add_argument("--owner", default=""); p.add_argument("--json", action="store_true")
    p = sub.add_parser("answer"); p.add_argument("qid"); p.add_argument("--option", required=True)
    p.add_argument("--text", default=""); p.add_argument("--revised-file", default=""); p.add_argument("--q", type=int, default=0)
    p = sub.add_parser("undo"); p.add_argument("qid")
    p = sub.add_parser("reopen", help="a consumed card whose action was not confirmed done comes back as the same card")
    p.add_argument("qid"); p.add_argument("--why", required=True); p.add_argument("--by", default="owner1")
    p = sub.add_parser("harvest"); p.add_argument("--owner", default=""); p.add_argument("--json", action="store_true")
    sub.add_parser("expire")
    sub.add_parser("sweep")
    a = ap.parse_args()

    if a.cmd == "post":
        qs = _read(a.file)
        if isinstance(qs, dict):
            qs = [qs]
        r = post(qs, a.owner, a.expires, a.lane, thread=a.thread, replaces=a.replaces, space=a.space, asked=a.asked)
        print(json.dumps(r, ensure_ascii=False))
        return 0 if r.get("posted") else 2
    if a.cmd == "say":
        r = say(a.owner, a.lane, a.text, thread=a.thread)
        print(json.dumps(r, ensure_ascii=False))
        return 0 if r.get("said") else 2
    if a.cmd == "note":
        print(json.dumps(note(a.owner, a.text), ensure_ascii=False)); return 0
    if a.cmd == "pending":
        rows = pending(a.owner)
        if a.json:
            print(json.dumps(rows, ensure_ascii=False, indent=1))
        else:
            for r in rows:
                print(f"{r['qid']} [{r['lane']}/{r['header']}] {r['age_min']:.0f}m {'EXEC ' if r['exec'] else ''}"
                      f"{'HELD(weekly) ' if r.get('held') else ''}{'URGENT ' if r.get('urgent') else ''}{r['question'][:110]!r} -> {r['options']}")
            if not rows:
                print("NO_OPEN_CARDS")
        return 0
    if a.cmd == "answer":
        revised = Path(a.revised_file).read_text(encoding="utf-8-sig") if a.revised_file else ""
        print(json.dumps(answer(a.qid, a.option, a.text, revised, a.q), ensure_ascii=False)); return 0
    if a.cmd == "undo":
        print(json.dumps(undo(a.qid), ensure_ascii=False)); return 0
    if a.cmd == "reopen":
        r = reopen(a.qid, a.why, by=a.by)
        print(json.dumps(r, ensure_ascii=False)); return 0 if r.get("ok") else 2
    if a.cmd == "harvest":
        rows = harvest(a.owner)
        print(json.dumps(rows, ensure_ascii=False, indent=1) if a.json else (f"HARVESTED {len(rows)}" if rows else "NO_ANSWERS"))
        return 0
    if a.cmd == "expire":
        print(f"EXPIRED {expire()}"); return 0
    if a.cmd == "sweep":
        print("SWEEP " + " ".join(f"{k}={v}" for k, v in sweep().items())); return 0
    return 1


if __name__ == "__main__":
    sys.exit(main())

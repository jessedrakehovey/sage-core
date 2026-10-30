"""bridge.py - the agent's loop: read every channel, ask the brain, apply, reply, run schedules.

Channels
  local   assistant/data/agent/inbox.jsonl -> outbox.jsonl. Always on. Used by chat.py (the
          terminal), by tests, and by anything on this machine that wants to talk to the agent.
  gchat   the Google Chat app (gchat.py). On once gchat.local.json exists.

One long-running process, started and kept alive by the Day Spine (job `agent-bridge`,
`bridge.py --ensure`). It must be started from the spine or a terminal, NOT from inside a
Claude session: the model turns authenticate with the User-env token, which a session
scrubs from its children.

  python assistant/agent/bridge.py --ensure     # start detached if not already running
  python assistant/agent/bridge.py --loop       # run in the foreground
  python assistant/agent/bridge.py --step       # one pass (tests, debugging)
  python assistant/agent/bridge.py --status
  python assistant/agent/bridge.py --stop

Only people in people.json are answered. Slash commands are handled here with no model turn:
  /rules  /memory  /schedules  /changes  /undo cN  /promises  /skills  /help

Bursts (2026-10-21, the way example and example answer): a Google Chat message is not answered the second
it lands. It waits in a buffer (data/agent/burst_pending.json) until its sender has been quiet for
profile.json burst.settle_s, so three quick lines, even 5-30 s apart, become ONE brain turn and ONE
reply. A typing placeholder goes up at once so Chat never shows "not responding". The local channel
(terminal, tests) and slash commands are answered at once, as before.
"""
from __future__ import annotations

import calendar
import hashlib
import json
import os
import queue
import re
import subprocess
import sys
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import account  # noqa: E402
import ack as ackgate  # noqa: E402  (handle() has a local named ack: the placeholder)
import actions  # noqa: E402
import addressed  # noqa: E402
import brain  # noqa: E402
import capability  # noqa: E402
import claims  # noqa: E402
import commitments  # noqa: E402
import core  # noqa: E402
import correction  # noqa: E402
import followups  # noqa: E402
import held  # noqa: E402
import meeting_reminder  # noqa: E402
import event_reminders  # noqa: E402
import place  # noqa: E402
import receipts  # noqa: E402
import routines  # noqa: E402
import skills  # noqa: E402
import space_reader  # noqa: E402

INBOX = core.DATA / "inbox.jsonl"
OUTBOX = core.DATA / "outbox.jsonl"
CURSOR = core.DATA / "cursors.json"
SPACES = core.DATA / "gchat_spaces.json"         # person key -> their DM space with the app
ALIVE = core.DATA / "bridge_alive.json"
STOP = core.DATA / "bridge.stop"
# tests set WV_LOG_DIR: a test run must never write fixture lines into the live log
LOG = Path(os.environ.get("WV_LOG_DIR") or (Path.home() / ".claude")) / "agent-bridge.log"
POLL_S = 3
PROMISES_S = 300          # how often the commitments ledger is checked against the queues (no model)
# Unblocked loop (2026-10-21): loop() runs each conversation's turns on its own worker thread (one in
# flight per conversation, the rest queued behind it) and the minute ticks (routines, follow-ups, the
# commitments check) on a tick thread, so a long turn, a voice transcription or a follow-up fire never
# holds up an answer in another conversation. Tests call step() with ASYNC off and get the old
# synchronous behaviour.
ASYNC = False
_workers: dict = {}
_WLOCK = threading.Lock()
_PLACEHOLDERS: dict = {}          # placeholder message name -> {"ts", "nudged"}; the watchdog edits a stale one
PLACEHOLDER_NUDGE_S = 90
PLACEHOLDER_NUDGE = "Still on it; this one needs a look at the records."
CRASH_LINE = "That one didn't go through on my side; ask me again in a minute."
INFLIGHT = core.DATA / "inflight.json"      # turns handed to a worker and not yet answered (replayed after a crash)
REPLAY_MAX_S = 3 * 3600                     # an owner's line older than this is not answered late by itself
REPLAY_LINE = "My first try at this never posted, so here it is."


def log(msg: str) -> None:
    try:                                          # logging must never be fatal (mistakes.md 2026-09-06)
        LOG.parent.mkdir(parents=True, exist_ok=True)
        with open(LOG, "a", encoding="utf-8") as f:
            f.write(f"{time.strftime('%Y-%m-%dT%H:%M:%S')}  {msg}\n")
    except OSError:
        pass


def _gchat():
    try:
        import gchat
        return gchat if gchat.configured() else None
    except Exception as e:  # noqa: BLE001
        log(f"gchat unavailable: {type(e).__name__}: {e}")
        return None


# ------------------------------------------------------------------ channels
def poll() -> list:
    """New messages from every channel, normalised."""
    out = []
    cur = core.load(CURSOR, {})
    rows = core.rows(INBOX)
    for r in rows[int(cur.get("local", 0)):]:
        person = core.person_for(handle=str(r.get("from") or ""))
        out.append({"channel": "local", "person": person, "text": str(r.get("text") or "").strip(),
                    "convo": f"dm:{person['key']}" if person else "", "raw_from": r.get("from")})
    if len(rows) != int(cur.get("local", 0)):
        cur["local"] = len(rows)
        core.save(CURSOR, cur)
    g = _gchat()
    if g:
        try:
            msgs, clicks = g.pull(with_clicks=True)
            msgs = g.take_inbound() + msgs            # the HTTP endpoint (mc_server) queues messages here too
            for k in clicks:                       # a button tap on a card = an answer, no model turn
                try:
                    click(k)
                except Exception as e:  # noqa: BLE001
                    log(f"click crashed: {type(e).__name__}: {str(e)[:200]}")
            for m in msgs:
                person = core.person_for(email=m["email"], chat_user=m["chat_user"])
                if person and m["dm"]:
                    sp = core.load(SPACES, {})
                    if sp.get(person["key"]) != m["space"]:
                        sp[person["key"]] = m["space"]
                        core.save(SPACES, sp)
                out.append({"channel": "gchat", "person": person, "text": m["text"], "space": m["space"],
                            "thread": "" if m["dm"] else m["thread"],
                            "convo": (f"dm:{person['key']}" if person and m["dm"] else m["space"]),
                            "raw_from": m["email"] or m["chat_user"], "direct": True, "why": "DM" if m["dm"] else "@mention",
                            "msg_name": m.get("msg_name", ""), "attachments": m.get("attachments") or [],
                            "created": str(m.get("created") or "")})
        except Exception as e:  # noqa: BLE001
            log(f"gchat pull failed: {type(e).__name__}: {str(e)[:160]}")
        try:   # the shared spaces, read with the owner's sign-in (example's way): people talking to each other too
            for m in space_reader.poll():
                if m.get("error"):
                    log(f"space reader: {m['error']}")
                    continue
                person = core.person_for(email=m["email"], chat_user=m["chat_user"])
                direct, why = m["direct"], m["why"]
                if not direct and person and core.is_owner(person):
                    # an owner's un-mentioned line in ANY shared space the app is in (the home space, or a team space
                    # like "the organization", 2026-10-26) is read the same way: for the other owner, or for the agent
                    # (addressed.decide). A team member's line is for the agent only when it names or @mentions it.
                    if _to_other_owner(m["text"], person):
                        why = "addressed to the other owner"
                    else:
                        # the ambiguous middle: a small model reads the room (example's tier 2, 2026-10-08);
                        # on any failure the rule applies: an owner's line in the home space is for the agent
                        direct, why = addressed.decide(m["text"], person, addressed.history_block(m["space"]), bool(m["thread"]))
                out.append({"channel": "gchat", "person": person, "text": m["text"], "space": m["space"],
                            "thread": m["thread"], "convo": m["space"], "raw_from": m["email"] or m["chat_user"],
                            "direct": direct, "why": why, "msg_name": m["msg_name"], "attachments": m["attachments"],
                            "created": str(m.get("created") or "")})
        except Exception as e:  # noqa: BLE001
            log(f"space reader failed: {type(e).__name__}: {str(e)[:160]}")
    out = _dedup(out)
    names = [m.get("msg_name") for m in out if m.get("msg_name")]
    if names:
        space_reader.mark_seen(names)
    return out


def _dedup(msgs: list) -> list:
    """One copy of each message. The HTTP endpoint, Pub/Sub and the space reader can all hand
    over the same Chat message, and Chat re-delivers what it thinks went unanswered (2026-10-07:
    'Fix the CRM connector' x3 in one turn, then three more answers). A message already in
    seen_messages.json, or repeated in this batch by name, or by (place, sender, text) when it
    has no name, is dropped."""
    seen = set(space_reader._seen())
    out, keys = [], set()
    for m in msgs:
        name = m.get("msg_name") or ""
        key = ("n", name) if name else ("t", m.get("channel"), m.get("convo"), (m.get("person") or {}).get("key"), m.get("text"))
        if (name and name in seen) or key in keys:
            continue
        keys.add(key)
        out.append(m)
    return out


def _to_other_owner(text: str, person: dict) -> bool:
    """'owner, can you...' in the shared space is for owner, not the agent."""
    others = [u["name"] for k, u in (core.people().get("users") or {}).items() if k != person.get("key")]
    return bool(others) and bool(re.match(r"^\s*@?(?:" + "|".join(re.escape(n) for n in others) + r")\b", text, re.I))


def _human_exchange_in_progress(convo: str, thread: str, person: dict) -> str:
    """The name of the human who wrote the LAST line of this thread when that line @mentions (or
    opens with the name of) another human, not the agent and not themselves: the sender's next
    line answers THAT person, not the agent. '' otherwise. Ported from example's bridge
    (2026-10-16, her dream c20-c24: Hollis '@the owner ... better question for Lucan', the owner 'I will
    ask him' -> example posted 'Got it.' into their exchange)."""
    name = core.profile().get("name", "Agent")
    rows = core.transcript_thread(convo, thread, 6) if thread else core.transcript_tail(convo, 6)
    # the sender's own earlier lines in the burst are skipped: what matters is the last line by
    # someone else (their 'I will ask him' followed by 'can you check the sitter?' is one reply)
    rows = [r for r in rows if r.get("who") and r.get("channel") not in ("card",) and r.get("who") != person.get("name")]
    if not rows:
        return ""
    last = rows[-1]
    who, txt = str(last.get("who") or ""), str(last.get("text") or "")
    if who == name:
        return ""
    others = [u["name"] for k, u in (core.people().get("users") or {}).items() if u.get("name") != who]
    m = re.search(r"(?:^|\s)@([A-Za-z][\w'.-]*)", txt)
    if m and m.group(1).lower() != name.lower():
        return who
    if others and re.match(r"^\s*(?:" + "|".join(re.escape(n.split()[0]) for n in others) + r")\b[,:]", txt, re.I):
        return who
    return ""


def _ledger_words(r: dict, person: dict, convo: str, thread: str) -> None:
    """The property rule, ported (exec_verify): a completed-action sentence with no deed behind it is its own
    failure row in the outcome ledger (outcomes.said_without_deed, 2026-10-24). Nothing is posted here; the
    scorecard counts these, so "filed" over a refused write can never be said twice unnoticed."""
    try:
        if not r.get("ok"):
            return
        gaps = claims.missing(r.get("reply"), r.get("suggest"))
        if not gaps:
            return
        kinds = {str(a.get("kind") or "") for a in actions.LAST_APPLIED}
        sys.path.insert(0, str(core.VAULT / "assistant" / "scripts"))
        import outcomes
        for g in gaps:
            needs = set(g.get("needs") or [])
            if kinds & needs or (g.get("claim") == "draft" and kinds & {"task", "card"}) or (g.get("claim") == "do" and kinds & {"do", "task"}):
                continue
            outcomes.said_without_deed(str(person.get("key") or ""), convo, thread, str(g.get("sentence") or ""), sorted(needs))
    except Exception as e:  # noqa: BLE001
        log(f"ledger words failed: {type(e).__name__}: {str(e)[:120]}")


def _deed_lead(applied: list) -> str:
    """The words above a card when the brain gave none (NO_REPLY with a task, do or build among its
    deeds). example, the owner 2026-10-14: 'example sometimes doesn't respond and just sends an action card'.
    One plain line per deed."""
    rows = []
    for a in applied[:4]:
        what = re.sub(r"\s+", " ", str(a.get("what") or "")).strip().rstrip(".")
        if not what:
            continue
        k = a.get("kind")
        if k == "card":
            rows.append(f"Here's the card for that: {what}.")
        elif k == "task":
            rows.append(f"I'm getting that drafted: {what}. You'll have the card here in a few minutes.")
        elif k == "do":
            rows.append(f"I've sent that off: {what}. I'll tell you here how it went.")
        elif k == "build":
            rows.append(f"That's in to be built: {what}.")
    return "\n".join(rows)


def _cards():
    sys.path.insert(0, str(core.AGENT.parent / "scripts"))
    import cards
    import chat_push
    return cards, chat_push


def click(k: dict) -> None:
    """A tap on a card button in Chat. Only an owner who may approve; the answer goes through
    cards.answer, the same guarded path Mission Control uses (undo window, one answer per card,
    sha-bound ticket). The card message is then edited in place to show the result."""
    person = core.person_for(email=k.get("email", ""), chat_user=k.get("chat_user", ""))
    fn, p = k.get("function", ""), k.get("params") or {}
    if not person or not core.can(person, "approve"):
        log(f"ignored click {fn} from unknown {k.get('email') or k.get('chat_user')!r}")
        return
    cards, chat_push = _cards()
    qid = str(p.get("qid") or "")
    card = cards._read(cards.LIVE / f"{qid}.json") if re.fullmatch(r"q[0-9a-f]{12}", qid) else None
    convo = chat_push.agent_convo(person["key"])
    if fn == "card_answer":
        option = str(p.get("option") or "")
        r = cards.answer(qid, option, q_index=int(p.get("q") or 0), by=f"gchat:{person['key']}")
        log(f"click answer {qid} {option!r} by {person['key']}: ok={r.get('ok')} {r.get('reason') or r.get('ticket') or ''}")
        if not r.get("ok") and _dup_tap(r.get("reason")):
            return                                   # the relay re-delivered the tap: nothing to say
        core.say(convo, person["name"], chat_push.tap_line(card, option, qid), channel="gchat", meta={"click": True})
        if r.get("ok"):
            card = cards._read(cards.LIVE / f"{qid}.json") or card
            if card:
                chat_push.mark_answered(card, {"option": option, "by": person["key"], "ticket": r.get("ticket"),
                                               "mint_error": r.get("mint_error")})
            if r.get("mint_error"):
                post(person["key"], f"Noted, but nothing was queued: {r['mint_error']}")
        else:
            post(person["key"], f"That card could not be answered: {r.get('reason')}")
    elif fn == "card_undo":
        r = cards.undo(qid)
        log(f"click undo {qid} by {person['key']}: ok={r.get('ok')} {r.get('reason') or ''}")
        core.say(convo, person["name"], f"[undid the answer on card {qid}]", channel="gchat", meta={"click": True})
        if r.get("ok") and card:
            chat_push.mark_undone(cards._read(cards.LIVE / f"{qid}.json") or card)
        elif not r.get("ok"):
            post(person["key"], f"Undo did not go through: {r.get('reason')}")
    else:
        log(f"ignored click function {fn!r} from {person['key']}")


def _root_as_owner(message_name: str) -> dict:
    """The app may only read its own messages (403 on a human's); the space reader holds the
    owner's own Google sign-in and can read any message in the space. Read-only, {} on failure."""
    try:
        d = space_reader._get(f"https://chat.googleapis.com/v1/{message_name}")
    except Exception:  # noqa: BLE001
        return {}
    snd = d.get("sender") or {}
    return {"text": str(d.get("text") or d.get("formattedText") or ("[card]" if d.get("cardsV2") else "")),
            "sender": str(snd.get("displayName") or snd.get("name") or ""), "bot": str(snd.get("type") or "") == "BOT"}


def _thread_of(message_name: str) -> str:
    """spaces/S/messages/ID.ID -> spaces/S/threads/ID (local, so a test's fake client need not have it)."""
    m = re.match(r"(spaces/[^/]+)/messages/([^/.]+)", str(message_name or ""))
    return f"{m.group(1)}/threads/{m.group(2)}" if m else ""


def _is_thread_root(message_name: str) -> bool:
    """A top-level Chat message is named spaces/S/messages/ID.ID (the thread's id twice); a reply
    in that thread is spaces/S/messages/ID.OTHER. Comparing only the thread part called every
    reply a root, so a reply in a doer report's thread was never looked up (2026-10-16 21:15:
    the owner's 'Please stop trying to do this' met a guess about the CRM template)."""
    m = re.match(r"spaces/[^/]+/messages/([^/.]+)\.([^/.]+)$", str(message_name or ""))
    return bool(m) and m.group(1) == m.group(2)


def resolve_thread_root(msg: dict, convo: str, thread: str) -> str:
    """A reply in a thread whose first message is on record nowhere (the transcript has no row
    with this thread, no card started it): fetch the root from Chat once and remember it, so
    the WHERE block can show what they are replying to. -> an extra line for the brain ('' when
    the root is now known and place.block will show it)."""
    NOT_ON_RECORD = ("\n[THE MESSAGE THAT STARTED THIS THREAD IS NOT ON RECORD. Do not guess what 'it' or 'this' is: answer in one short "
                     "line asking which message they are replying to, naming the two newest things you posted as the likely candidates.]")
    if _is_thread_root(str(msg.get("msg_name") or "")):
        return ""                                   # a new top-level message: it IS its thread's root (every fresh Chat message is one)
    known = core.thread_root(thread)
    if known.get("text"):
        return ""
    try:                                            # the thread as Chat holds it (chat_thread, 2026-10-29): place.block showed it already
        import chat_thread
        space = str(msg.get("space") or (convo if str(convo).startswith("spaces/") else ""))
        rows = chat_thread.thread_lines(space, thread, exclude=str(msg.get("msg_name") or ""))
        if rows is not None:
            return ""                               # read from Chat: what is there is shown; nothing to guess about
    except Exception as e:  # noqa: BLE001
        log(f"chat_thread read failed: {type(e).__name__}: {str(e)[:120]}")
    rows = core.transcript_thread(convo, thread, 50)
    if rows:
        return ""                                   # the thread's earlier lines are on record (the owner's own, or the agent's)
    if known:                                       # tried before: Chat would not show it (a deleted message, or not the app's)
        return NOT_ON_RECORD
    m = re.search(r"^(spaces/[^/]+)/threads/([^/]+)$", str(thread or ""))
    g = _gchat()
    if m and g and hasattr(g, "get"):
        root = g.get(f"{m.group(1)}/messages/{m.group(2)}.{m.group(2)}") or g.get(f"{m.group(1)}/messages/{m.group(2)}")
        if not root.get("text"):
            root = _root_as_owner(f"{m.group(1)}/messages/{m.group(2)}.{m.group(2)}")
        if root.get("text"):
            who = core.profile().get("name", "Agent") if root.get("bot") else (root.get("sender") or "someone")
            core.note_thread_root(thread, who, root["text"], "fetched", convo)
            log(f"thread root fetched from Chat for {thread.rsplit('/', 1)[-1]} ({len(root['text'])} chars, {'bot' if root.get('bot') else 'human'})")
            return ""
        core.note_thread_root(thread, "", "", "unreadable", convo)        # remember the miss: one API call, not one per message
        log(f"thread root for {thread.rsplit('/', 1)[-1]} could not be read from Chat (deleted, or not the app's message)")
    return NOT_ON_RECORD


def card_for_thread(thread: str) -> dict | None:
    """The open card whose Chat message started this thread, if any. A card's message name is
    spaces/S/messages/ID.ID and its thread spaces/S/threads/ID, so the ids match. the owner
    2026-10-08: "if I reply in thread, interact with me in thread and edit the approval based on
    my feedback" (what example does)."""
    m = re.search(r"/threads/([^/]+)$", str(thread or ""))
    if not m:
        return None
    tid = m.group(1)
    try:
        cards, _ = _cards()
        for p in sorted(cards.LIVE.glob("q*.json")):
            c = cards._read(p) or {}
            for name in (c.get("chat") or {}).values():
                mm = re.search(r"/messages/([^/.]+)", str(name))
                if mm and mm.group(1) == tid:
                    return c
    except Exception as e:  # noqa: BLE001
        log(f"card_for_thread failed: {type(e).__name__}: {str(e)[:120]}")
    return None


def card_context(card: dict) -> str:
    """The block the brain gets when an owner writes in a card's thread."""
    cards, chat_push = _cards()
    q = (card.get("questions") or [{}])[0]
    key, text = chat_push.exec_text(q)
    status = str(card.get("status") or "open")
    lines = [f"[THIS REPLY IS IN THE THREAD OF CARD {card['id']}" + ("" if status == "open" else f", which is already {status}") + "] "
             + chat_push.card_line(card)]
    if status != "open":
        try:                     # how it closed, truly: a tapped Send is never "closed unsent" (2026-10-18, the Hollis text)
            import exec_store as es
            import place
            lines.append("Where it stands: " + place._card_state(card, cards._answers_index(), es.fold()) + ".")
        except Exception:  # noqa: BLE001
            pass
        lines.append("The card is closed: they are talking about it, not answering it, and their message here does not change it. "
                     "Nothing to tap; answer in words from where it stands, never make it again unless they ask for a new one, "
                     "and if they want the matter dropped, drop it and say so in a few words.")
        return "\n".join(lines)
    if text:
        lines.append(f"The exact message that card would send: \"{text}\"")
        lines.append("If they want it changed, call card_revise with the complete new message text (qid " + card["id"] + ") and say in one short "
                     "line what you changed; the revised card lands in this thread and the old one is retired. Their tap on the new card is what "
                     "sends. If they ask you to send it, call approve (a card they asked for); if the system posted it, their tap on Send is what counts.")
    else:
        lines.append("If they answer the card in words, say which button it matches and that a tap on it is what counts.")
    lines.append(f"If their words mean it is not needed (skip it, don't send, it's already done), call approve with skip true (qid {card['id']}): "
                 "the card closes unsent. Never tell them to tap Skip.")
    return "\n".join(lines)


def _dup_tap(reason) -> bool:
    """A second delivery of the same tap (Chat and the relay retry): already answered, or being
    answered this second. 2026-10-07 the second copy posted 'That card could not be answered'."""
    return bool(re.search(r"being answered|already answered|already been answered|not open", str(reason or ""), re.I))


_recent_posts: list = []          # (ts, space, text): one copy of a broadcast in the home space


def post(person_key: str, text: str, channel: str = "") -> None:
    """Put a message in front of one owner on every channel they can be reached on. With a home
    space, that is the shared space, once: callers that post to each owner in turn (digest,
    routines to 'both') land one message, not two."""
    core.append(OUTBOX, {"ts": core.stamp(), "to": person_key, "text": text})
    g = _gchat()
    space = core.home_space() or core.load(SPACES, {}).get(person_key)
    try:                                       # a person's words or nothing (voice.py, 2026-10-27)
        import voice
        text = voice.gate(text, channel or "post", space or "")
    except Exception as e:  # noqa: BLE001
        log(f"voice gate failed: {type(e).__name__}: {str(e)[:120]}")
    if not text:
        return
    if space == core.home_space() and space:
        now = time.time()
        _recent_posts[:] = [p for p in _recent_posts if now - p[0] < 120]
        if any(p[1] == space and p[2] == text for p in _recent_posts):
            return
        _recent_posts.append((now, space, text))
    if g and space:
        try:
            name = g.post(space, text)
            core.note_thread_root(_thread_of(name), core.profile().get("name", "Agent"), text, channel or "post", space)
        except Exception as e:  # noqa: BLE001
            log(f"gchat post to {person_key} failed: {type(e).__name__}: {str(e)[:160]}")


def post_in(convo: str, thread: str, text: str) -> None:
    """One line into a specific conversation and thread: the place a promise was made (the
    commitments check). A DM convo is dm:<key>; anything else is a space name."""
    name = core.profile().get("name", "Agent")
    core.say(convo, name, text, channel="commitment", meta={"thread": thread or ""})
    to = convo[3:] if convo.startswith("dm:") else ""
    core.append(OUTBOX, {"ts": core.stamp(), "to": to or "space", "text": text, "via": "commitment", "convo": convo, "thread": thread})
    g = _gchat()
    space = core.load(SPACES, {}).get(to) if to else convo
    if g and space and str(space).startswith("spaces/"):
        try:
            name2 = g.post(space, text, thread or "")
            if not thread:
                core.note_thread_root(_thread_of(name2), name, text, "commitment", convo)
        except Exception as e:  # noqa: BLE001
            log(f"gchat post_in to {convo} failed: {type(e).__name__}: {str(e)[:160]}")


def _ack(msg: dict) -> str:
    """No typing indicator exists for Chat apps, so a placeholder IS the answer's message: it
    is posted at once and edited in place when the answer lands (example's bridge, 2026-09-03).
    Returns the placeholder's message name, '' on any other channel."""
    if msg["channel"] != "gchat":
        return ""
    if msg.get("_ack"):
        return str(msg["_ack"])                       # posted early (an attachment to transcribe): reuse it
    try:
        name = _gchat().post(msg["space"], f"_{core.profile().get('name', 'Agent')} is typing\u2026_", msg.get("thread", "")) or ""
    except Exception as e:  # noqa: BLE001
        log(f"gchat placeholder failed: {type(e).__name__}: {str(e)[:120]}")
        return ""
    if name:
        _PLACEHOLDERS[name] = {"ts": time.time(), "nudged": False}
        msg["_ack"] = name
    return name


def _drop_ack(ack: str) -> None:
    _PLACEHOLDERS.pop(ack, None)
    if ack:
        try:
            _gchat().delete(ack)
        except Exception as e:  # noqa: BLE001
            log(f"gchat placeholder delete failed: {type(e).__name__}: {str(e)[:120]}")


def _edit_placeholder(ack: str, text: str) -> bool:
    """A placeholder that cannot become an answer becomes one honest line instead of 'is typing' forever."""
    _PLACEHOLDERS.pop(ack, None)
    if not ack:
        return False
    try:
        g = _gchat()
        return bool(g and g.patch(ack, text))
    except Exception as e:  # noqa: BLE001
        log(f"gchat placeholder edit failed: {type(e).__name__}: {str(e)[:120]}")
        return False


def placeholder_watch(now: float | None = None) -> int:
    """The watchdog (tick thread): a 'typing' placeholder older than PLACEHOLDER_NUDGE_S is edited once
    to say the turn is still running; a crashed turn's placeholder is edited by _run_handle. Before
    2026-10-21 a crash after the placeholder left 'Sage is typing...' up for good. -> how many nudged."""
    now = now or time.time()
    n = 0
    for name, st in list(_PLACEHOLDERS.items()):
        if st.get("nudged") or now - float(st.get("ts") or now) < PLACEHOLDER_NUDGE_S:
            continue
        st["nudged"] = True
        try:
            g = _gchat()
            if g and g.patch(name, PLACEHOLDER_NUDGE):
                n += 1
        except Exception as e:  # noqa: BLE001
            log(f"placeholder nudge failed: {type(e).__name__}: {str(e)[:120]}")
    return n


def reply(msg: dict, text: str, ack: str = "") -> None:
    """Deliver one reply. On Google Chat the placeholder is edited into the answer; if that
    fails the answer is posted as its own message and the placeholder is removed."""
    core.append(OUTBOX, {"ts": core.stamp(), "to": msg["person"]["key"], "text": text, "via": msg["channel"]})
    if msg["channel"] != "gchat":
        return
    g = _gchat()
    name = core.profile().get("name", "Agent")
    _PLACEHOLDERS.pop(ack, None)
    try:   # tag whoever the reply is for in a shared space, unless it already tags someone (example's bridge)
        text = g.ensure_mention(text, msg.get("space", ""), str(msg["person"].get("chat_user") or msg.get("raw_from") or ""))
    except Exception as e:  # noqa: BLE001
        log(f"mention skipped: {type(e).__name__}: {str(e)[:120]}")
    try:
        if ack and g.patch(ack, text):
            if not msg.get("thread"):                       # the placeholder's message IS the reply: it starts a thread
                core.note_thread_root(_thread_of(ack), name, text, "gchat", msg.get("convo", ""))
            return
    except Exception as e:  # noqa: BLE001
        log(f"gchat edit failed ({type(e).__name__}); posting instead")
    try:
        posted = g.post(msg["space"], text, msg.get("thread", ""))
        if not msg.get("thread"):
            core.note_thread_root(_thread_of(posted), name, text, "gchat", msg.get("convo", ""))
        _drop_ack(ack)
    except Exception as e:  # noqa: BLE001
        log(f"gchat reply failed: {type(e).__name__}: {str(e)[:160]}")


# ------------------------------------------------------------------ handling
def command(text: str, person: dict) -> str | None:
    parts = text.strip().split()
    cmd = parts[0].lower()
    if cmd == "/rules":
        return core.render_rules() or "No learned rules yet."
    if cmd == "/memory":
        return core.render_memory() or "Nothing in long-term memory yet."
    if cmd == "/schedules":
        return routines.render() or "No schedules."
    if cmd == "/changes":
        ch = core.changes_since(24 * 7)[-15:]
        return "\n".join(f"{c['id']} {c['ts'][5:]} {c['summary'][:110]}" for c in ch) or "No changes in the last week."
    if cmd == "/undo" and len(parts) == 2:
        return core.undo(parts[1], by=person["key"]) if core.can(person, "undo") else "Only an owner can undo."
    if cmd == "/promises":
        return commitments.render()
    if cmd == "/reflex":
        import reflex
        return reflex.status()
    if cmd == "/projects":
        import projects
        return projects.render_list()
    if cmd in ("/open", "/failed"):
        sys.path.insert(0, str(core.AGENT.parent / "scripts"))
        import outcomes
        return outcomes.render_open() if cmd == "/open" else outcomes.render_failed()
    if cmd == "/evidence" and len(parts) == 2:
        return evidence_text(parts[1])
    if cmd == "/who" and len(parts) >= 2:
        sys.path.insert(0, str(core.AGENT.parent / "scripts"))
        import whois
        try:
            return whois.explain(" ".join(parts[1:])) or "I have nothing on that one."
        except Exception as e:  # noqa: BLE001
            return f"I could not look that up just now ({type(e).__name__})."
    if cmd == "/switch" and len(parts) >= 3 and core.can(person, "task"):
        import switches
        key, what = parts[1], parts[2].lower()
        if what == "run":
            r = switches.run_now(key, by=person["key"])
            return f"{r.get('job') or key}: {r.get('status')}" + (f" ({r.get('reason')})" if r.get("reason") else "")
        r = switches.set_on(key, what in ("on", "true", "1", "start"), by=person["key"])
        return f"{r.get('job') or key}: {r.get('status')}" + (f" ({r.get('reason')})" if r.get("reason") else "")
    if cmd == "/digest" and len(parts) == 2 and parts[1].lower() == "now" and core.can(person, "task"):
        sys.path.insert(0, str(core.AGENT.parent / "scripts"))
        import digest
        r = digest.post(force=True)
        return f"Posted the approvals digest now: {r.get('cards', 0)} card(s) in {r.get('messages', 0)} message(s)." if r.get("posted") else f"No digest went out: {r.get('reason')}."
    if cmd == "/quiet" and len(parts) >= 2 and core.can(person, "task"):
        return quiet_until(" ".join(parts[1:]), person)
    if cmd == "/signin" and len(parts) >= 2 and core.can(person, "task"):
        return signin_text(" ".join(parts[1:]))
    if cmd == "/skills":
        return skills.render() or "No skills yet. Teach me one by walking me through it, or ask for something and I'll learn as I go."
    if cmd == "/org":
        import org
        return org.render() + "\n\n" + org.status_text()
    if cmd == "/setup":
        import brain as _b
        return (_b.connections_block() or "No connections registered yet.") + f"\n\nOne button for each, from your phone: {_mc_url()}/setup"
    if cmd == "/health":
        return health_text()
    if cmd == "/library":
        import resources
        return resources.status_text()
    if cmd == "/connect":
        sys.path.insert(0, str(core.AGENT.parent / "scripts"))
        import connect
        if len(parts) == 2:
            rid = parts[1] if connect.row(parts[1]) else next((r["id"] for r in connect.chat_rows() if parts[1].lower() in r["id"]), "")
            return connect.steps(rid) or "I don't have chat steps for that one; its button is on the Setup page."
        rows_ = connect.chat_rows()
        return ("I can set these up right here: " + "; ".join(f"{r.get('name')} (/connect {r['id'].split('-')[-1]})" for r in rows_)) if rows_ else "Nothing has chat steps yet; everything is on the Setup page."
    if cmd == "/help":
        return ("/rules  /memory  /schedules  /changes  /undo cN  /promises  /reflex  /projects  /open  /failed  /evidence <card>  /who <number or name>  "
                "/switch <job> on|off|run  /digest now  /quiet until <time>  /signin <site>  /skills  /org  /setup  /health  /connect  /library. Anything else, just ask.")
    return None


QUIET_FILE = core.DATA / "quiet_until.json"


def evidence_text(qid: str) -> str:
    """/evidence <card>: what the ledger holds for the card's action: when it ran, what was read back, the ids."""
    sys.path.insert(0, str(core.AGENT.parent / "scripts"))
    import cards
    import outcomes
    q = qid.strip()
    a = cards._answers_index().get(q)
    if not a:
        return f"I have no answer on record for card {q}."
    tk = str(a.get("ticket") or "")
    if not tk:
        return f"Card {q}: you chose \"{a.get('option')}\" at {str(a.get('ts') or '')[11:16]}; it queued nothing (a decision, not a send)."
    row = outcomes.by_ticket(tk)
    if not row:
        return f"Card {q}: \"{a.get('option')}\" queued ticket {tk}; the ledger has no row for it (it predates the ledger)."
    ev = row.get("evidence") or {}
    last = (row.get("checks") or [{}])[-1]
    bits = [f"Card {q}: \"{a.get('option')}\" at {str(a.get('ts') or '')[11:16]} -> {row.get('kind', '').replace('_', ' ')} {outcomes.icon(row.get('state'))} {row.get('state')}"]
    if last.get("detail"):
        bits.append(f"last check {str(last.get('ts') or '')[11:16]}: {last['detail']}")
    ids = {k: v for k, v in ev.items() if k in ("message_id", "crm_id", "outbox", "sent", "confirmed_by", "cells", "retry_ticket", "owner_said")}
    if ids:
        bits.append("evidence: " + ", ".join(f"{k} {v}" for k, v in ids.items()))
    return ". ".join(bits) + "."


def quiet_until(spec: str, person: dict) -> str:
    """/quiet until <time|off>: hold every line the system says on its own (chases, outcome asks, stall lines) until
    then. Cards still post (Mission Control) and urgent ones still reach Chat: a quiet hour is not a missed lead."""
    import re as _re
    from datetime import datetime, timedelta
    s = spec.strip().lower().replace("until", "").strip()
    if s in ("off", "none", "cancel", "stop"):
        core.save(QUIET_FILE, {})
        return "Okay, I'm back to speaking up as usual."
    m = _re.match(r"^(\d{1,2})(?::(\d{2}))?\s*(am|pm)?$", s)
    if not m:
        return "Say a time, like /quiet until 7pm or /quiet until 09:00, or /quiet off."
    h, mi, ap = int(m.group(1)), int(m.group(2) or 0), m.group(3)
    if ap == "pm" and h < 12:
        h += 12
    if ap == "am" and h == 12:
        h = 0
    now = datetime.now()
    until = now.replace(hour=h % 24, minute=mi, second=0, microsecond=0)
    if until <= now:
        until += timedelta(days=1)
    core.save(QUIET_FILE, {"until": until.strftime("%Y-%m-%dT%H:%M:%S"), "by": person.get("key", ""), "set": now.strftime("%Y-%m-%dT%H:%M:%S")})
    when = until.strftime("%I:%M %p").lstrip("0") + (" tomorrow" if until.date() != now.date() else " today")
    return f"Quiet until {when}: no chases or check-ins from me; urgent cards still come through."


def is_quiet_until(now=None) -> bool:
    from datetime import datetime
    try:
        d = core.load(QUIET_FILE, {}) or {}
        return bool(d.get("until")) and (now or datetime.now()) < datetime.strptime(d["until"], "%Y-%m-%dT%H:%M:%S")
    except (ValueError, TypeError):
        return False


def signin_text(site: str) -> str:
    """/signin <site>: open the agent's browser ON the PC screen at that site (setup_actions 'signin', the session persists in the
    doer's profile) and hand the owner the Setup page where 'What the PC shows' follows it. The sign-in itself is typed at the
    PC: a screenshot cannot take input, so a true from-the-phone sign-in stays a build request (HANDOFF 2026-10-24)."""
    sys.path.insert(0, str(core.AGENT.parent / "scripts"))
    try:
        import setup_actions
        rows = setup_actions.rows()
        want = site.strip().lower()
        row = next((r for r in rows if want in str(r.get("id") or "").lower() or want in str(r.get("name") or "").lower()
                    or want in str(r.get("url") or "").lower()), None)
        if not row:
            return f"I don't have a sign-in row for \"{site}\". Tell me its site and I'll add it (org services), or use the Setup page: {_mc_url()}/setup"
        r = setup_actions.run(str(row.get("id")), "signin", by="chat")
        return (f"{row.get('name') or row.get('id')}: {r.get('message') or 'the browser is on the PC screen at the sign-in page.'} "
                f"Sign in there; 'What the PC shows' on the Setup page ({_mc_url()}/setup) follows it, and the session is kept.")
    except Exception as e:  # noqa: BLE001
        return f"I could not open that sign-in ({type(e).__name__}); its button is on the Setup page: {_mc_url()}/setup"


def _mc_url() -> str:
    try:
        sys.path.insert(0, str(core.AGENT.parent / "scripts"))
        import chat_push
        return chat_push.MC_URL
    except Exception:  # noqa: BLE001
        return "Mission Control"


def health_text() -> str:
    """/health: how the agent is doing, zero-LLM, in plain words (2026-10-21: everything from the phone)."""
    L = []
    try:
        a = core.load(ALIVE, {})
        age = time.time() - float(a.get("ts") or 0)
        L.append("I'm up" + (f" (last heartbeat {int(age)} s ago)" if age < 600 else f", but my last heartbeat was {int(age // 60)} min ago"))
    except Exception:  # noqa: BLE001
        L.append("I can't read my own heartbeat")
    try:
        day = time.strftime("%Y-%m-%d")
        secs = [int(m.group(1)) for ln in LOG.read_text(encoding="utf-8", errors="replace").splitlines()[-2000:]
                if ln.startswith(day) for m in [re.search(r"brain turn (\d+)s", ln)] if m]
        if secs:
            secs.sort()
            L.append(f"{len(secs)} turns today, typically {secs[len(secs) // 2]} s, slowest {secs[-1]} s")
    except Exception:  # noqa: BLE001
        pass
    try:
        cards, _ = _cards()
        live = [c for c in cards._live() if c.get("status") == "open"]
        held_n = sum(1 for c in live if c.get("delivery") == "weekly")
        L.append(f"{len(live)} open card{'s' if len(live) != 1 else ''}" + (f", {held_n} waiting for Monday's approvals" if held_n else ""))
    except Exception:  # noqa: BLE001
        pass
    try:
        n = len(held.pending())
        if n:
            L.append(f"{n} question{'s' if n != 1 else ''} of yours kept until my usage limit resets")
    except Exception:  # noqa: BLE001
        pass
    try:
        rows = commitments.open_rows()
        if rows:
            L.append(f"{len(rows)} thing{'s' if len(rows) != 1 else ''} I've handed on and am still waiting to hear back on (/promises)")
    except Exception:  # noqa: BLE001
        pass
    try:
        sys.path.insert(0, str(core.AGENT.parent / "scripts"))
        import setup_actions
        bad = [r for r in setup_actions.rows() if r.get("state") in ("dead", "not_configured")]
        if bad:
            L.append("Not connected: " + ", ".join(r["name"] for r in bad[:8]) + f". One button each at {_mc_url()}/setup")
        else:
            L.append("Every connection I know of is live")
    except Exception:  # noqa: BLE001
        pass
    if ASYNC:
        n = in_flight()
        if n:
            L.append(f"{n} turn{'s' if n != 1 else ''} in flight right now")
    return ". ".join(L) + "."


def _is_vcf(att: dict) -> bool:
    name = str(att.get("contentName") or att.get("name") or "").lower()
    ctype = str(att.get("contentType") or "").lower()
    return name.endswith((".vcf", ".vcard")) or ctype in ("text/vcard", "text/x-vcard", "text/directory")


def _import_contacts(atts: list, person: dict, stamp: str) -> tuple:
    """Every .vcf among the attachments -> contacts.py import for this owner; the downloaded copy is deleted.
    -> (lines to say, the attachments that were not contacts files)."""
    lines, rest = [], []
    for att in atts or []:
        if not isinstance(att, dict) or not _is_vcf(att):
            rest.append(att)
            continue
        if not core.is_owner(person):
            lines.append("A contacts file is an owner's to hand over, so I left it alone.")
            continue
        path = None
        try:
            sys.path.insert(0, str(core.AGENT.parent / "scripts"))
            import contacts
            path = space_reader.download(att, stamp)
            if not path:
                lines.append("I could not download the contacts file; share it again or upload it on Mission Control /contacts.")
                continue
            r = contacts.import_file(path, owner=person["key"], source="chat")
            if r.get("ok"):
                lines.append(f"Imported {r['count']} contacts for {person['name']} ({r.get('with_phone', 0)} with a phone number); my copy of the file is deleted. "
                             "Delete it from this chat too if you like: I only keep names, numbers, emails and relations.")
                log(f"contacts: imported {r['count']} for {person['key']} from a chat attachment")
            else:
                lines.append(f"That contacts file did not import: {r.get('reason')}.")
        except Exception as e:  # noqa: BLE001
            log(f"contacts import from chat failed: {type(e).__name__}: {str(e)[:160]}")
            lines.append("The contacts import failed on my side; upload the file on Mission Control /contacts instead.")
        finally:
            try:
                if path and Path(path).exists():
                    Path(path).unlink()
            except OSError:
                pass
    return lines, rest


def _vault_offer(msg: dict | None, text: str, person: dict) -> str:
    """A password pasted into chat is refused and scrubbed BY CODE before any model turn, and an owner asking to hand
    over a login gets the one card whose button opens the vault dialog (assistant/scripts/vault_chat.py, sage-core
    2026-10-28). Returns the line to say, '' when the message is neither."""
    try:
        sys.path.insert(0, str(core.AGENT.parent / "scripts"))
        import vault_chat
        secret, site = vault_chat.detect_secret(text)
        ask = "" if secret else vault_chat.asks_login(text)
        if not secret and not ask:
            return ""
        if not core.can(person, "approve"):
            return "Logins are the owners' to hand over." if secret else ""
        n = 0
        if secret:
            n = vault_chat.scrub(secret)
            log(f"vault: a pasted password from {person['key']} refused and scrubbed from {n} file(s) (value not logged)")
        posted = False
        if msg and msg.get("channel") == "gchat" and str(msg.get("space") or "").startswith("spaces/"):
            try:
                import gchat
                posted = bool(gchat.post_body(msg["space"], vault_chat.card(site or ask, core.profile().get("name", "the agent")), str(msg.get("thread") or "")))
            except Exception as e:  # noqa: BLE001
                log(f"vault card not posted: {type(e).__name__}: {str(e)[:120]}")
        line = vault_chat.refusal_line(site, n) if secret else vault_chat.offer_line(ask)
        if not posted:
            line += " (The Enter login button is on Mission Control's Setup page too.)"
        return line
    except Exception as e:  # noqa: BLE001
        log(f"vault offer failed: {type(e).__name__}: {str(e)[:160]}")
    return ""


def _chat_setup(text: str, person: dict, msg: dict | None = None) -> str:
    """Steps for a connection the owner asked about, or the claim of a one-time token they pasted."""
    line = _vault_offer(msg, text, person)
    if line:
        return line
    try:
        sys.path.insert(0, str(core.AGENT.parent / "scripts"))
        import connect
        rid, value = connect.detect(text)
        if rid and value:
            if not core.can(person, "approve"):
                return ""
            log(f"chat setup: claiming a one-time token for {rid} from {person['key']} (value not logged)")
            out = connect.claim(rid, value)
            n = connect.scrub(value)
            log(f"chat setup: {rid} claim done; scrubbed {n} file(s)")
            return out
        return connect.answer(text)
    except Exception as e:  # noqa: BLE001
        log(f"chat setup failed: {type(e).__name__}: {str(e)[:160]}")
    return ""


_CODE_LINE = re.compile(r"^\s*(?:(?:the\s+|my\s+)?code(?:\s+is)?\s*[:\-]?\s*|it'?s\s+|here'?s\s+)?(\d[\d \-]{2,14}\d)\s*[.!]?\s*$", re.I)


def _code_for_card(text: str, person: dict) -> str:
    """A verification code an owner typed in chat while the doer waits on a code card (the owner, 2026-10-25:
    "552182" was answered "I can't use a code from chat"). Code answers the newest open code card with it,
    exactly as the note box on Mission Control does: the code is minted into the sha-bound ticket, never
    kept in answers.jsonl and never shown to the model. '' when the line is not a bare code or no card waits."""
    m = _CODE_LINE.match(str(text or ""))
    digits = re.sub(r"\D", "", m.group(1)) if m else ""
    if not 4 <= len(digits) <= 10 or not core.can(person, "approve"):
        return ""
    try:
        sys.path.insert(0, str(core.AGENT.parent / "scripts"))
        import cards
        waiting = [c for c in cards._live() if c.get("status") == "open"
                   and (((c.get("questions") or [{}])[0].get("exec") or {}).get("kind") == "agent_run")
                   and ((((c.get("questions") or [{}])[0].get("exec") or {}).get("payload") or {}).get("ask_code"))]
        if not waiting:
            return ""
        card = waiting[-1]                                # _live is oldest first: the newest code card
        ex = card["questions"][0]["exec"]
        r = cards.answer(card["id"], str(ex.get("approve_label") or "Approve"), text=digits, by=person["key"])
        log(f"chat code: answered code card {card['id']} for {person['key']}: ok={r.get('ok')} "
            f"{r.get('reason') or r.get('ticket') or r.get('mint_error') or ''} (value not logged)")
        if r.get("ok") and r.get("ticket"):
            return "Got the code. I'm putting it in now and carrying on with the sign-in; I'll tell you here when it's through."
        return f"I couldn't put that code in: {r.get('reason') or r.get('mint_error') or 'the card would not take it'}."
    except Exception as e:  # noqa: BLE001
        log(f"chat code failed: {type(e).__name__}: {str(e)[:160]}")
    return ""


def _is_no_reply(text: str) -> bool:
    return re.sub(r"^[^A-Za-z]+", "", str(text or "")).strip().upper().startswith("NO_REPLY")


AMBIENT_PER_HOUR = 4
FOLLOWUP_S = 600          # a line within 10 min of the agent's own reply in that space continues the conversation
_ambient_asks: list = []


def _just_spoke(convo: str, name: str) -> bool:
    """True when the agent posted in this convo within FOLLOWUP_S: an un-@mentioned line right after
    its answer is a follow-up to it, not people talking among themselves."""
    for r in reversed(core.transcript_tail(convo, 20)):
        if r.get("who") == name:
            try:
                return time.time() - time.mktime(time.strptime(r["ts"], "%Y-%m-%d %H:%M")) < FOLLOWUP_S + 60
            except (KeyError, ValueError):
                return False
    return False


REPEAT_WINDOW_S = 15 * 60


def recently_answered(convo: str, person: dict, text: str, now: float | None = None) -> bool:
    """Chat re-delivers a message it thinks went unanswered, and owners resend when a reply is
    slow (2026-10-07: 'What do you mean unanswered vendor email?' x4, answered four times). The
    same words from the same person inside REPEAT_WINDOW_S, already followed by a reply from the
    agent, are a repeat: not answered again. Unanswered repeats ('Hello?') still get a turn."""
    sq = " ".join(str(text or "").lower().split())
    if len(sq) < 4:
        return False
    now = now or time.time()
    name = core.profile().get("name", "Agent")
    rows = core.transcript_tail(convo, 40)
    for i, r in enumerate(rows):
        if r.get("who") != person.get("name") or " ".join(str(r.get("text") or "").lower().split()) != sq:
            continue
        try:
            age = now - time.mktime(time.strptime(str(r.get("ts") or ""), "%Y-%m-%d %H:%M"))
        except ValueError:
            continue
        if age > REPEAT_WINDOW_S or age < 0:
            continue
        if any(x.get("who") == name and x.get("channel") not in brain.SYSTEM_CHANNELS for x in rows[i + 1:]):
            return True
    return False


CROSSED_NOTE = ("[these lines arrived while you were still answering their previous message and they have not "
                "seen your last reply yet: answer everything once, do not repeat what you already said]")


def handle(msg: dict, pre_ack: str = "") -> None:
    """One message (or one merged burst) end to end. `pre_ack` is a typing placeholder the burst
    buffer already posted when the first line landed: the reply edits it instead of posting a
    second one, and it comes down when the answer is silence."""
    person, text = msg.get("person"), msg.get("text", "")
    if pre_ack and not msg.get("_ack"):
        msg["_ack"] = pre_ack                         # _ack(msg) reuses it; every early exit drops it
        _PLACEHOLDERS.setdefault(pre_ack, {"ts": time.time(), "nudged": False})
    if not person or not core.can(person, "ask"):
        log(f"ignored message from unknown sender {msg.get('raw_from')!r} on {msg['channel']}")
        _drop_ack(pre_ack)
        return
    # a message re-run from the held queue (held.py) was recorded and acknowledged when it first
    # arrived: no repeat check, no second transcript line, no follow-up note
    rerun = bool(msg.get("_held"))
    if text and not rerun and not text.startswith("/") and not msg.get("attachments") and not correction.kind(text) \
            and recently_answered(msg.get("convo", ""), person, text):
        # ("Try again" twice in fifteen minutes is two corrections, never a repeat: correction.py, 2026-10-27)
        log(f"repeat from {person['key']} already answered within {REPEAT_WINDOW_S // 60} min: not answered again")
        core.say(msg.get("convo", ""), person["name"], text, channel=msg["channel"], meta={"thread": msg.get("thread", ""), "repeat": "skipped"})
        _drop_ack(pre_ack)
        return
    if msg.get("attachments"):
        # a voice note can take minutes to transcribe: the owner sees 'typing' from the start, not after
        if msg.get("channel") == "gchat" and msg.get("direct", True) and str(msg.get("why") or "") in ("DM", "@mention"):
            _ack(msg)
        stamp = time.strftime("%Y%m%d-%H%M%S")
        # a contacts file (.vcf) shared into chat is the phone book, not something to describe (sage-core 2026-10-28:
        # "collect phone contacts securely"): imported by code for the owner who sent it, the copy deleted, one line back
        vcf_lines, rest = _import_contacts(msg["attachments"], person, stamp)
        msg["attachments"] = rest
        if vcf_lines and not rest and not text.strip():
            out = " ".join(vcf_lines)
            core.say(msg["convo"], core.profile().get("name", "Agent"), out, channel=msg["channel"], meta={"thread": msg.get("thread", "")})
            reply(msg, out, pre_ack)
            return
        parts = (space_reader.describe_attachments(rest, person["name"], stamp) if rest else []) + vcf_lines
        text = "\n".join([text] + parts if text else parts)
    if not text:
        _drop_ack(str(msg.get("_ack") or pre_ack or ""))
        return
    name = core.profile().get("name", "Agent")
    convo, thread = msg["convo"], msg.get("thread", "")
    direct = msg.get("direct", True)
    if not direct and _just_spoke(convo, name):
        between = _human_exchange_in_progress(convo, thread, person)
        if between and not _to_other_owner(text, person):
            # the last line in the agent's thread is a human @mentioning another human: this line
            # answers that person. With a question or some substance it still reaches the brain as a
            # WEAK direct (no placeholder, NO_REPLY stays silent); a plain line stays ambient.
            if "?" in text or len(text.split()) >= 8:
                direct, msg["why"] = True, f"reply in your thread, answering {between} (weak)"
            else:
                msg["why"] = f"reply to {between} in your thread, not you"
        else:
            direct, msg["why"] = True, "follow-up to its own reply" + (" (weak)" if _to_other_owner(text, person) else "")
    # a verification code typed in chat while the doer waits on a code card answers that card by code
    # (no model turn); the transcript keeps a placeholder, not the code
    out = _code_for_card(text, person) if not rerun else ""
    if out:
        core.say(convo, person["name"], "(a verification code, handed to the waiting sign-in)", channel=msg["channel"], meta={"thread": thread})
        core.say(convo, name, out, channel=msg["channel"], meta={"thread": thread})
        reply(msg, out, pre_ack)
        return
    if not direct:
        # a chime-in budget, as in example's bridge: people talking to each other cost a model turn
        # only a few times an hour, and the brain answers NO_REPLY unless it holds a fact they need
        now = time.time()
        _ambient_asks[:] = [t for t in _ambient_asks if now - t < 3600]
        if len(_ambient_asks) >= AMBIENT_PER_HOUR:
            log(f"ambient budget spent ({AMBIENT_PER_HOUR}/h): skipped {person['key']} in {convo}")
            core.say(convo, person["name"], text, channel=msg["channel"], meta={"ambient": "skipped", "thread": thread})
            followups.note_reply(person["key"], convo, thread)
            _drop_ack(pre_ack)
            return
        _ambient_asks.append(now)
    if not rerun:
        core.say(convo, person["name"], text, channel=msg["channel"], meta={"thread": thread})
        followups.note_reply(person["key"], convo, thread)      # they spoke: an awaited answer may be here
    if not msg.get("attachments") and ackgate.is_pure_ack(text, names=(name, person["name"])):
        # "ok thanks" / a thumbs-up: consumed silently, no placeholder, no model turn (example's ack gate)
        log(f"ack from {person['key']} consumed without a turn")
        _drop_ack(pre_ack)
        return
    if text.startswith("/"):
        out = command(text, person)
        if out is not None:
            core.say(convo, name, out, channel=msg["channel"], meta={"thread": thread})
            reply(msg, out, pre_ack)
            return
    # chat setup of a connection (assistant/scripts/connect.py, the owner 2026-10-16 "stupid simple from
    # inside Sage chat"): code answers "set up simplefin" with the steps and links, and a pasted
    # one-time setup token is claimed, stored and scrubbed from the record. No model turn, so the
    # token never reaches the model's context either.
    out = _chat_setup(text, person, msg)
    if out:
        core.say(convo, name, out, channel=msg["channel"], meta={"thread": thread})
        reply(msg, out, pre_ack)
        return
    # an owner's "stop sending me messages like this" under a code-posted line switches that line's class off
    # BY CODE, this turn, model or no model (reflex.mute_from_thread; 2026-10-27 16:55: the turn that should have
    # done it never ran, the account had refused it, and the "Dropped: ..." lines went on). The owner hears one
    # line; whatever else the message asks still goes to the brain, with a note that this part is done.
    muted_line, mute_note = "", ""
    if direct and thread:
        try:
            import reflex
            mres = reflex.mute_from_thread(text, convo, thread, by=person["key"])
            if mres:
                muted_line, mute_note = mres.get("line", ""), mres.get("note", "")
                log(f"muted by code from {person['key']}'s thread: {mres.get('cls')} ({mres.get('status')})")
                if not mres.get("rest"):
                    core.say(convo, name, muted_line, channel=msg["channel"], meta={"thread": thread})
                    reply(msg, muted_line, pre_ack)
                    return
        except Exception as e:  # noqa: BLE001
            log(f"mute from thread crashed: {type(e).__name__}: {str(e)[:160]}")
    # a correction ("that's not what I asked", "where is the card", "try again") is the original ask coming back
    # (correction.py, the owner 2026-10-27: "Sage needs to fix whatever it was; it cannot just say sorry and never change"):
    # the brain gets the ask again with what is expected, an apology-only reply is re-run once, and the deed the ask
    # implied is queued by code when the turn still has none.
    corr_kind, corr_ask, corr_expected, corr_rerun, corr_here = "", {}, "", False, {}
    if direct:
        try:
            corr_kind = correction.kind(text)
            if corr_kind:
                corr_ask = correction.original_ask(convo, thread, person)
                corr_expected = correction.expected_deed(corr_ask.get("text", "")) if corr_ask else ""
                # "where is the card" in a thread the card already landed in: point at it, never a second card
                # (2026-10-18, the Hollis text: tapped Send, then told it "closed unsent" and made again)
                corr_here = correction.card_in_thread(thread, corr_ask) if corr_kind == "card" and thread else {}
                if corr_here:
                    corr_expected = ""
                    log(f"correction (card) from {person['key']}: the card {corr_here.get('id')} is already in this thread")
                log(f"correction ({corr_kind}) from {person['key']}: " + (f"the ask was {corr_ask['text'][:60]!r}, expected {corr_expected or 'words'}" if corr_ask else "no earlier ask found"))
        except Exception as e:  # noqa: BLE001
            log(f"correction check crashed: {type(e).__name__}: {str(e)[:160]}")
    # the model's account is at its usage limit, or refused the last turn (held.py): nothing spends a turn until
    # it resets / is back. The message is kept and answered then, in this place; the owner hears so once, in one line.
    until = held.blocked_until()
    if until > time.time() and not rerun:
        reason = held.reason_in_force()
        held.hold(msg, text, until, reason=reason)
        if direct:
            out = held.line(until, again=True, reason=reason)
            if muted_line:
                out = muted_line + " " + out
            core.say(convo, name, out, channel=msg["channel"], meta={"thread": thread})
            reply(msg, out, pre_ack)
        else:
            _drop_ack(pre_ack)
        log(f"held {person['key']}'s message until {held.fmt(until)} ({reason} still in force)")
        return
    # a chime-in never announces itself; neither does a WEAK direct (group talk, or a reply in the
    # agent's thread meant for someone else): no placeholder, and NO_REPLY stays silent (example, 2026-09-30).
    # A placeholder the burst buffer already posted is reused, never doubled.
    weak = str(msg.get("why") or "").endswith("(weak)")
    ack = pre_ack or (_ack(msg) if direct and not weak else "")
    if not ack and msg.get("_ack"):
        _drop_ack(str(msg["_ack"]))                   # posted early, but the line turned out not to be for the agent
    # the addressed field drives answer-vs-NO_REPLY (example's bridge): DMs, @mentions and the agent's
    # name are direct; anything else the space reader saw is AMBIENT
    extra = (f"[addressed: {'direct' if direct else 'AMBIENT'} ({msg.get('why', 'DM')})]\n"
             + place.block({**msg, "convo": convo, "thread": thread, "text": text}, person) + "\n" + followups.hint())
    if mute_note:
        extra = mute_note + "\n" + extra
    if corr_here:
        extra = correction.here_note(corr_here) + "\n" + extra
    elif corr_ask:
        extra = correction.note(corr_kind, text, corr_ask, corr_expected) + "\n" + extra
    if msg.get("_crossed"):
        # the whole burst was typed before the agent's last reply in this convo went up (bridge._crossed)
        extra = CROSSED_NOTE + "\n" + extra
    if msg.get("_capability_replay"):
        # an ask the agent could not do earlier, brought back by code now that the missing piece is built (capability.py)
        extra = capability.replay_note(str(msg["_capability_replay"])) + "\n" + extra
    if msg.get("_project_plan"):
        # a step of a long-running project ended with nothing on record for what comes next (projects.py): one planning turn
        import projects
        extra = projects.plan_note(str(msg["_project_plan"])) + "\n" + extra
    tcard = card_for_thread(thread) if thread else None
    if tcard:
        extra += "\n" + card_context(tcard)
    elif thread:
        extra += resolve_thread_root(msg, convo, thread)
    try:
        evb = actions.events_block()
        if evb:
            extra += "\n" + evb
    except Exception:  # noqa: BLE001
        pass
    if direct and not weak:
        # while the agent is still learning the organization, one onboarding question a turn, a few a day (org.py)
        try:
            import org
            hint = org.interview_hint()
            if hint:
                extra += "\n" + hint
        except Exception:  # noqa: BLE001
            pass
    # where the ask was made rides into the turn (intents.begin) so a deed lands there: a thread, or the
    # owner's DM (dm_space) when they wrote there, never "somewhere else later"
    dm_space = str(msg.get("space") or "") if str(convo).startswith("dm:") else ""
    t0 = time.time()
    r = brain.ask(person, text, convo, channel=msg["channel"], extra=extra, thread=thread, card=tcard, space=dm_space,
                  scope=core.scope_of(convo, str(msg.get("space") or "")))
    log(f"brain turn {time.time() - t0:.0f}s for {person['key']}")      # the owner 2026-10-07: 'Sage feels slow' - measure it
    if corr_ask and not corr_here and r.get("ok") and corr_kind in ("card", "redo", "fix") and correction.apology_only(r.get("reply"), r.get("suggest")):
        # an apology with nothing put forward, after a correction: ONE re-run of the original ask (correction.py)
        corr_rerun = True
        log(f"correction: the reply was an apology with nothing done; re-running the ask once for {person['key']}")
        t0 = time.time()
        r = brain.ask(person, f"{corr_ask['text']}\n\n({person['name']} followed up: {text.strip()[:200]})", convo, channel=msg["channel"],
                      extra=correction.note(corr_kind, text, corr_ask, corr_expected, rerun=True) + "\n" + extra, thread=thread, card=tcard,
                      space=dm_space, scope=core.scope_of(convo, str(msg.get("space") or "")))
        log(f"brain re-run {time.time() - t0:.0f}s for {person['key']}")
    # ONE turn (2026-10-21). The deed tools answered the brain inside the turn, so its words already match
    # its deeds; the honesty-check and card-check re-asks (two more model turns each) are gone. The one
    # safety net left costs no turn: an owner asked for a message, the brain gave wording and no card ->
    # the ask is handed to the operator as it stands, so a card still comes.
    forced = bool(r.get("ok") and direct and claims.asked_for_message(text) and not tcard
                  and not claims.card_bound(r.get("suggest")) and not claims.is_clarifying(r.get("reply")))
    if not r.get("ok") and not r.get("reply"):
        log(f"brain failed for {person['key']} on {r.get('account')}: {r.get('error')}")
        err = r.get("error") or ""
        until = held.limit_reset(err) or account.out_until(err)
        if until:
            # a usage limit, or an account that turned the turn away (2026-10-27): keep the question and answer
            # it when the limit resets / the account is back (held.py; the reflex probes the account meanwhile).
            # Until 2026-10-16 this was 'Give me a minute and ask again', six times, and the asks were lost.
            reason = "limit" if held.limit_reset(err) else "refused"
            row = held.hold(msg, text, until, reason=reason)
            if row.get("status") == "given_up":
                out = held.gave_up_line()
                log(f"gave up on {person['key']}'s held message after {row['attempts'] - 1} limit retries")
            else:
                out = held.line(until, again=rerun, reason=reason)
                log(f"held {person['key']}'s message until {held.fmt(until)} ({reason}; attempt {row['attempts']})")
            if not direct or weak:
                _drop_ack(ack)
                return
        else:
            out = "That one didn't go through on my side. Ask me again in a minute and I'll take it fresh."
        if muted_line:
            out = muted_line + " " + out                 # the switch-off already happened in code; say so even now
    else:
        if r.get("account"):
            log(f"turn ran on {r['account']}")
        lines = actions.apply(r.get("suggest") or {}, person, ctx={"convo": convo, "thread": thread, "card": tcard, "space": dm_space, "text": text})
        if forced and not any(a.get("kind") in ("task", "card") for a in actions.LAST_APPLIED):
            lane = claims.lane_guess(text)
            tid = actions._queue_task(lane, claims.forced_task_text(person["name"], text, r.get("reply")), person,
                                      {"convo": convo, "thread": thread, "space": dm_space})
            if tid:
                actions.LAST_APPLIED.append({"kind": "task", "what": text.strip()[:160], "ref": tid})
                log(f"forced a task for {person['key']}'s message ask (lane {lane}): the brain gave words and no card")
        # the same net for the doer (2026-10-23): "that goes to the doer, I'll tell you once it's done" with no
        # `do` deed left the price-sheet change unqueued on 10-19 until the owner asked again. The owner's words
        # plus the brain's sentence become the job; the doer reports back here like any other.
        if r.get("ok") and direct and not tcard and core.can(person, "task") \
                and any(c["claim"] == "do" for c in claims.missing(r.get("reply"), r.get("suggest"))) \
                and not any(a.get("kind") in ("do", "task", "card") for a in actions.LAST_APPLIED):
            lane = claims.lane_guess(text)
            try:
                tid = actions._queue_do(lane, claims.forced_task_text(person["name"], text, r.get("reply")), person,
                                        {"convo": convo, "thread": thread, "space": dm_space})
            except Exception as e:  # noqa: BLE001
                tid, _ = "", log(f"forced do failed: {type(e).__name__}: {str(e)[:120]}")
            if tid:
                actions.LAST_APPLIED.append({"kind": "do", "what": text.strip()[:160], "ref": tid})
                log(f"forced a doer job for {person['key']} (lane {lane}): the brain promised the doer and sent nothing")
        # a forwarded marketplace lead (a zola.com/z link, 2026-10-27) is a doer job by code, never "what's the date
        # and venue?": the doer reads the page, checks the CRM, and a paid Connect is its checkpoint card here
        lead_url = claims.marketplace_lead(text) if r.get("ok") and direct and not tcard and core.can(person, "task") else ""
        if lead_url and not any(a.get("kind") == "do" for a in actions.LAST_APPLIED):
            try:
                tid = actions._queue_do("wedding", claims.lead_intake_text(person["name"], text, lead_url), person,
                                        {"convo": convo, "thread": thread, "space": dm_space})
            except Exception as e:  # noqa: BLE001
                tid, _ = "", log(f"lead intake do failed: {type(e).__name__}: {str(e)[:120]}")
            if tid:
                actions.LAST_APPLIED.append({"kind": "do", "what": "reading the new lead's page and checking the date in the CRM", "ref": tid})
                r["reply"] = _deed_lead(actions.LAST_APPLIED[-1:])
                log(f"lead intake: sent {person['key']}'s forwarded marketplace lead to the doer by code")
        if corr_kind:
            # the deed the original ask implied, by code, when the turn still has none; and the ledger row the reflex reads
            try:
                forced_kind = correction.force(corr_expected, person, corr_ask, text, r.get("reply"), {"convo": convo, "thread": thread, "space": dm_space},
                                               list(actions.LAST_APPLIED)) if corr_ask and r.get("ok") else ""
                if forced_kind:
                    log(f"correction: forced the {forced_kind} from the original ask for {person['key']}")
                    lead = _deed_lead([a for a in actions.LAST_APPLIED if a.get("kind") == forced_kind])
                    if lead:
                        r["reply"] = ((r.get("reply") or "").strip() + " " + lead).strip()
                correction.record(person, corr_kind, text, corr_ask, corr_expected, list(actions.LAST_APPLIED), forced_kind, corr_rerun,
                                  r.get("reply"), convo=convo, thread=thread, here=str(corr_here.get("id") or ""))
            except Exception as e:  # noqa: BLE001
                log(f"correction net crashed: {type(e).__name__}: {str(e)[:160]}")
        commitments.register_applied(actions.LAST_APPLIED, person, {"convo": convo, "thread": thread})
        _ledger_words(r, person, convo, thread)
        # the capability loop (2026-10-25): a reply that says it could not do the ask, with nothing handed on,
        # files the build for that ability now and the ask comes back by itself when it lands; a standing
        # preference the turn did not `learn` is kept as a rule by code
        gap_line = ""
        if direct and r.get("ok"):
            try:
                gap_line = capability.after_turn(person, text, r.get("reply"), list(actions.LAST_APPLIED),
                                                 {"convo": convo, "thread": thread, "space": dm_space, "replay": msg.get("_capability_replay")})
                if gap_line:
                    log(f"capability gap filed for {person['key']}: {text[:60]!r}")
                res = capability.correction(person, text, r.get("suggest"))
                if res.get("status") == "applied":
                    log(f"preference kept as rule {res.get('id')} from {person['key']}'s words")
            except Exception as e:  # noqa: BLE001
                log(f"capability check crashed: {type(e).__name__}: {str(e)[:160]}")
        said = (r.get("reply") or "").strip()
        silent = _is_no_reply(said)
        out = "" if silent else followups.keep_promises(said, actions.LAST_FOLLOWUPS, speaker=person["name"].split()[0])
        if silent and actions.LAST_APPLIED:
            # never a bare action card: a NO_REPLY that still hands work off gets a plain lead line
            # so the owner sees words before the card (example, 2026-10-14)
            out = _deed_lead(actions.LAST_APPLIED)
            if out:
                silent = False
                log("NO_REPLY with a deed: lead line posted instead of silence")
        if lines:
            # only what could NOT be done after the turn (a calendar timeout, a refused card): plain
            # sentences in the agent's own voice, never a bulleted system block (2026-10-21)
            try:                     # "the card is below" while the card was refused this turn: the truth, by code (reflex.py)
                import reflex
                out = reflex.fix_reply(out, list(actions.LAST_APPLIED), lines)
            except Exception:  # noqa: BLE001
                pass
            out = (out + "\n\n" if out else "") + " ".join(ln.strip() for ln in lines if ln.strip())
        try:                     # ids, ticket codes and preview markers never reach a person (voice.py, 2026-10-27)
            import voice
            out = voice.polish(out) if out else out
        except Exception:  # noqa: BLE001
            pass
        if gap_line:
            out = (out + " " if out else "") + gap_line
            silent = False
        if not out and (silent or not direct):
            # NO_REPLY means silence in every mode: nothing is posted and the placeholder comes down
            _drop_ack(ack)
            log(f"silent ({'NO_REPLY' if silent else 'ambient, nothing to add'}) for {person['key']} on {msg['channel']}")
            return
        if not out:
            out = _deed_lead(actions.LAST_APPLIED) or "Got it."
    if msg.get("_replay") and out:
        out = REPLAY_LINE + " " + out
    core.say(convo, name, out, channel=msg["channel"], meta={"thread": thread})
    reply(msg, out, ack)
    log(f"answered {person['key']} on {msg['channel']} ({len(text)} -> {len(out)} chars)")


def _grouped(msgs: list) -> list:
    """A burst from one person in one place is ONE ask (three quick lines get one answer, not
    three). Slash commands stay on their own."""
    out = []
    for m in msgs:
        key = (m["channel"], m.get("convo"), (m.get("person") or {}).get("key"), m.get("space"), m.get("thread"))
        prev = out[-1] if out else None
        if (prev and prev["_key"] == key and m.get("person") and not m["text"].startswith("/")
                and not prev["text"].startswith("/")):
            prev["text"] += "\n" + m["text"]
        else:
            out.append({**m, "_key": key})
    return out


# ------------------------------------------------------------------ in-flight turns (2026-10-23)
# 14:24 the owner asked for the meeting notes; the bridge process died with that turn in a worker thread and
# nothing was logged (stderr went to DEVNULL). The spine restarted it at 15:06 and the question was simply
# gone until he typed "Try again". Every turn handed to a worker is written here first and removed once
# answered (or dropped on purpose); a fresh bridge answers what the old one left behind, saying so.
_INFLIGHT_LOCK = threading.Lock()
_INFLIGHT_SKIP = ("_ack", "_pre_ack", "attachments")      # a dead placeholder / a file handle is not replayed


STALL_LINE_S = 45 * 60      # an owner line with no reply after this gets ONE plain line in its place (WP2, the property doctor's SLA)


def stalled_turns(now: float | None = None) -> int:
    """An owner's line that is still in flight after STALL_LINE_S: one honest line where it was asked, once, and a
    failed `turn` row in the outcome ledger so the scorecard counts it. The inflight replay covers a dead bridge;
    this covers a live one that is stuck on a turn. -> lines posted."""
    now = now or time.time()
    n = 0
    with _INFLIGHT_LOCK:
        st = core.load(INFLIGHT, {})
        for key, row in list(st.items()):
            if not isinstance(row, dict) or row.get("stalled_said") or now - float(row.get("ts") or now) < STALL_LINE_S:
                continue
            m = row.get("m") or {}
            try:
                post_in(str(m.get("convo") or ""), str(m.get("thread") or ""), "Still on it; this one is taking far longer than it should. If nothing lands here in a bit, ask me again and I'll take it fresh.")
                n += 1
            except Exception as e:  # noqa: BLE001
                log(f"stall line failed: {type(e).__name__}: {str(e)[:120]}")
            row["stalled_said"] = now
            try:
                sys.path.insert(0, str(core.AGENT.parent / "scripts"))
                import outcomes
                r = outcomes.claim("turn", key, str(m.get("text") or "")[:160], by=str((m.get("person") or {}).get("key") or ""),
                                   convo=str(m.get("convo") or ""), thread=str(m.get("thread") or ""), route="brain", verifier="none")
                if r:
                    outcomes.set_state(r["id"], "failed", {}, f"no reply after {STALL_LINE_S // 60} min", by="bridge")
            except Exception:  # noqa: BLE001
                pass
        if n:
            core.save(INFLIGHT, st)
    return n


def _inflight_key(m: dict) -> str:
    raw = json.dumps([m.get("channel"), m.get("convo"), m.get("thread") or "", (m.get("person") or {}).get("key"),
                      str(m.get("text") or "")[:400], m.get("ts") or ""], sort_keys=True)
    return hashlib.sha1(raw.encode("utf-8", "replace")).hexdigest()[:12]


def _inflight_add(m: dict) -> None:
    if m.get("_held") or m.get("channel") == "local" or str(m.get("text") or "").lstrip().startswith("/"):
        return                                 # held re-runs have their own queue; the terminal and slash commands do not replay
    try:
        with _INFLIGHT_LOCK:
            st = core.load(INFLIGHT, {})
            st[_inflight_key(m)] = {"ts": time.time(), "m": {k: v for k, v in m.items() if k not in _INFLIGHT_SKIP}}
            core.save(INFLIGHT, st)
    except Exception as e:  # noqa: BLE001
        log(f"inflight add failed: {type(e).__name__}")


def _inflight_done(m: dict) -> None:
    try:
        with _INFLIGHT_LOCK:
            st = core.load(INFLIGHT, {})
            if st.pop(_inflight_key(m), None) is not None:
                core.save(INFLIGHT, st)
    except Exception as e:  # noqa: BLE001
        log(f"inflight done failed: {type(e).__name__}")


def replay_inflight(max_age_s: int = REPLAY_MAX_S) -> int:
    """Hand every turn the previous bridge never finished back to a worker, oldest first; the reply
    opens with REPLAY_LINE. Stale ones are dropped with a log line, never answered hours late."""
    st = core.load(INFLIGHT, {})
    if not st:
        return 0
    n, now = 0, time.time()
    for key, row in sorted(st.items(), key=lambda kv: float((kv[1] or {}).get("ts") or 0)):
        m = dict((row or {}).get("m") or {})
        age = now - float((row or {}).get("ts") or 0)
        if not m.get("text") or not m.get("person") or age > max_age_s:
            log(f"inflight {key} dropped (age {age / 60:.0f} min): {str(m.get('text') or '')[:60]!r}")
            st.pop(key, None)
            continue
        st.pop(key, None)                                    # re-added by _dispatch under the same key
        m["_replay"], m["_held"] = True, m.get("_held") or "replay"     # recorded in the transcript already: no second line
        log(f"inflight {key} replayed after {age / 60:.0f} min: {str(m.get('text') or '')[:60]!r}")
        core.save(INFLIGHT, st)
        _dispatch(m)
        n += 1
    core.save(INFLIGHT, st) if not n else None
    return n


def _run_handle(m: dict) -> None:
    try:
        handle(m, str(m.get("_pre_ack") or ""))
    except Exception as e:  # noqa: BLE001
        log(f"handle crashed: {type(e).__name__}: {str(e)[:200]}")
        if m.get("_ack"):
            _edit_placeholder(str(m["_ack"]), CRASH_LINE)
    finally:
        _inflight_done(m)


def _worker(key: str, q: "queue.Queue") -> None:
    while True:
        m = q.get()
        try:
            _run_handle(m)
        finally:
            q.task_done()


def _dispatch(m: dict) -> None:
    """One worker per conversation: turns in the same conversation stay in order, different
    conversations never wait on each other."""
    _inflight_add(m)
    if not ASYNC:
        _run_handle(m)
        return
    key = str(m.get("convo") or m.get("channel") or "local")
    with _WLOCK:
        w = _workers.get(key)
        if not w or not w["t"].is_alive():
            q: "queue.Queue" = queue.Queue()
            t = threading.Thread(target=_worker, args=(key, q), name=f"turn:{key[-12:]}", daemon=True)
            t.start()
            w = _workers[key] = {"q": q, "t": t}
    w["q"].put(m)


def in_flight() -> int:
    return sum(w["q"].unfinished_tasks for w in _workers.values())


# ------------------------------------------------------------------ bursts (2026-10-21)
# A Chat message waits here until its sender has gone quiet, so "three quick lines" typed 5-30 s
# apart (one poll is 3 s) become ONE brain turn and ONE reply, the way example and example answer. The
# buffer lives on disk: the bridge restarts itself whenever agent code changes, and no line may be
# lost in that gap. The local channel (terminal, tests) and slash commands never wait.
BURST = core.DATA / "burst_pending.json"
BURST_DEFAULTS = {"settle_s": 20, "max_wait_s": 120, "pace_factor": 1.5}
_now = time.time                  # tests swap in a fake clock


def burst_cfg() -> dict:
    """profile.json "burst": settle_s (quiet time before a group is answered), max_wait_s (no group
    waits longer than this, whatever the pace), pace_factor (a slow typist's lines wait pace_factor
    x their largest gap, so a line every 40 s still gets one answer)."""
    b = core.profile().get("burst") or {}
    out = {}
    for k, d in BURST_DEFAULTS.items():
        try:
            out[k] = float(b.get(k, d))
        except (TypeError, ValueError):
            out[k] = float(d)
    out["max_wait_s"] = max(out["max_wait_s"], out["settle_s"])
    return out


def _burst_key(m: dict) -> str:
    return json.dumps([m.get("channel"), m.get("convo"), (m.get("person") or {}).get("key"), m.get("space"), m.get("thread") or ""])


def _bufferable(m: dict) -> bool:
    """Only a known person's ordinary Google Chat line waits. Slash commands, the local channel,
    strangers (handle ignores them at once) and held re-runs go straight through."""
    return (m.get("channel") == "gchat" and bool(m.get("person")) and core.can(m.get("person"), "ask")
            and not str(m.get("text") or "").lstrip().startswith("/") and not m.get("_held"))


def _wants_ack(m: dict) -> bool:
    """A placeholder goes up the moment a DIRECT, non-weak, non-slash line that is more than
    'ok thanks' is buffered: Chat shows 'not responding' when nothing is posted within ~30 s,
    and the settle window alone can be longer than that."""
    if m.get("channel") != "gchat" or not m.get("direct", True) or str(m.get("why") or "").endswith("(weak)"):
        return False
    if m.get("attachments"):
        return True
    name = core.profile().get("name", "Agent")
    return not ackgate.is_pure_ack(str(m.get("text") or ""), names=(name, (m.get("person") or {}).get("name", "")))


def _burst_load() -> dict:
    d = core.load(BURST, {})
    if not isinstance(d, dict):
        d = {}
    d.setdefault("groups", {})
    d.setdefault("pace", {})
    return d


def _burst_add(store: dict, m: dict, now: float) -> None:
    key = _burst_key(m)
    pkey = (m.get("person") or {}).get("key") or ""
    cfg = burst_cfg()
    g = store["groups"].get(key)
    if not g:
        g = store["groups"][key] = {"key": key, "msgs": [], "ack": "", "first": now, "gap": 0.0}
    prev_at = float((store["pace"].get(pkey) or {}).get("at") or 0)
    if g["msgs"]:
        g["gap"] = max(float(g.get("gap") or 0), now - float(g["msgs"][-1]["_seen"]))
    elif prev_at and cfg["settle_s"] < now - prev_at <= cfg["max_wait_s"] / 2:
        # the gap to their previous line (already answered or not) is this person's typing pace:
        # a slow typist's second line waits for the third instead of getting its own answer. A gap
        # inside settle_s is no pace (those lines merge anyway) and one past half of max_wait_s is
        # a new question, not a slow continuation: either way a lone line still waits settle_s.
        g["gap"] = now - prev_at
    g["msgs"].append({**m, "_seen": now})
    store["pace"][pkey] = {"at": now}
    if not g["ack"] and _wants_ack(m):
        g["ack"] = _ack(m) or ""
        if g["ack"]:
            log(f"placeholder up for {pkey}'s burst in {m.get('convo')}; answering once they go quiet")


def _burst_wait(g: dict, cfg: dict) -> float:
    gap = float(g.get("gap") or 0)
    wait = max(cfg["settle_s"], cfg["pace_factor"] * gap) if gap else cfg["settle_s"]
    return min(wait, cfg["max_wait_s"])


def _parse_created(s: str) -> float:
    """Chat's createTime (RFC 3339, UTC) -> epoch; 0 when it cannot be read."""
    try:
        return float(calendar.timegm(time.strptime(str(s or "")[:19], "%Y-%m-%dT%H:%M:%S")))
    except (ValueError, TypeError, OverflowError):
        return 0.0


def _crossed(msgs: list, convo: str) -> bool:
    """True when every line in the group was created before the agent's latest reply in this
    convo went up: they typed while it was still answering and have not seen that reply. The
    transcript ts is minute-granular, so a line only counts as earlier when its createTime is at
    least 60 s older; anything unreadable -> False (no note rather than a wrong one)."""
    name = core.profile().get("name", "Agent")
    last = 0.0
    for r in core.transcript_tail(convo, 40):
        if r.get("who") != name or r.get("channel") in brain.SYSTEM_CHANNELS:
            continue
        try:
            last = max(last, time.mktime(time.strptime(str(r.get("ts") or ""), "%Y-%m-%d %H:%M")))
        except (ValueError, OverflowError):
            continue
    if not last:
        return False
    times = [_parse_created(m.get("created")) for m in msgs]
    return bool(times) and all(t and t <= last - 60 for t in times)


def _burst_merge(g: dict) -> dict:
    """One message out of a group, the way _grouped joins a same-poll burst: texts on their own
    lines, attachments concatenated, the first line's place; direct when any line was."""
    msgs = g["msgs"]
    first = {k: v for k, v in msgs[0].items() if k != "_seen"}
    direct = [m for m in msgs if m.get("direct", True)]
    out = {**first, "text": "\n".join(str(m.get("text") or "") for m in msgs if str(m.get("text") or "").strip()),
           "attachments": [a for m in msgs for a in (m.get("attachments") or [])],
           "direct": bool(direct), "why": (direct[0] if direct else msgs[0]).get("why", "DM"),
           "_burst": len(msgs), "_created": [m.get("created") or "" for m in msgs]}
    if g.get("ack"):
        out["_pre_ack"] = out["_ack"] = g["ack"]
    if _crossed(msgs, str(out.get("convo") or "")):
        out["_crossed"] = True
    return out


def _burst_due(store: dict, now: float) -> list:
    """Pop and return the groups whose sender has gone quiet (or that have waited max_wait_s),
    oldest first, each merged into one message."""
    cfg = burst_cfg()
    out = []
    for key in sorted(store["groups"], key=lambda k: float(store["groups"][k].get("first") or 0)):
        g = store["groups"][key]
        if not g.get("msgs"):
            store["groups"].pop(key, None)
            continue
        newest = float(g["msgs"][-1]["_seen"])
        if now - newest >= _burst_wait(g, cfg) or now - float(g.get("first") or newest) >= cfg["max_wait_s"]:
            out.append(_burst_merge(g))
            store["groups"].pop(key, None)
    return out


def burst_pending() -> int:
    return sum(len(g.get("msgs") or []) for g in _burst_load()["groups"].values())


def step() -> int:
    """One pass: poll every channel; answer the local channel, slash commands and strangers at
    once; buffer the rest per (channel, convo, person, space, thread) and hand each group to
    handle() as one message once its sender has been quiet for the settle window."""
    n = 0
    now = _now()
    store = _burst_load()
    before = json.dumps(store, sort_keys=True)
    at_once = []
    for m in poll():
        if _bufferable(m):
            _burst_add(store, m, now)
        else:
            at_once.append(m)
    for m in _grouped(at_once):
        _dispatch(m)
        n += 1
    for m in _burst_due(store, now):
        log(f"burst of {m['_burst']} line(s) from {(m.get('person') or {}).get('key')} in {m.get('convo')} answered as one"
            + (" (crossed its last reply)" if m.get("_crossed") else ""))
        _dispatch(m)
        n += 1
    if json.dumps(store, sort_keys=True) != before:
        core.save(BURST, store)
    return n


def followups_fire() -> int:
    """Send the check-in for a follow-up that came due. The brain looks at that person's
    conversation and the records first and answers NO_REPLY when it is settled; otherwise its
    one line goes to that person. One per pass."""
    rows = followups.due()
    if not rows:
        return 0
    it = rows[0]
    target = core.person_for(handle=it.get("to_key", ""))
    if not target:
        followups.mark(it["id"], status="invalid")
        return 0
    convo = f"dm:{target['key']}"
    r = brain.ask(target, followups.due_question(it), convo, channel="follow-up", sender="the follow-up tracker (code)")
    if not r.get("ok"):
        followups.mark(it["id"], retry_min=10)          # busy or failed brain: again shortly, three times at most
        return 0
    body = (r.get("reply") or "").strip()
    if not body or _is_no_reply(body):
        followups.mark(it["id"], status="settled")
        log(f"follow-up {it['id']} settled without a post")
        return 1
    post(target["key"], body)
    core.say(convo, core.profile().get("name", "Agent"), body, channel="follow-up")
    followups.mark(it["id"], status="sent", sent_text=body[:300])
    log(f"follow-up {it['id']} sent to {target['key']}")
    return 1


def held_retry() -> int:
    """Re-run every held message whose limit has reset (held.py), in the place it was asked.
    One pass; a limit on the retry re-holds it with the new reset time."""
    n = 0
    for row in held.due():
        row = held.take(row["id"])
        if not row:
            continue
        person = core.person_for(handle=row.get("person_key", ""))
        if not person:
            continue
        m = {**(row.get("msg") or {}), "person": person, "text": row.get("text", ""),
             "_held": row["id"], "_attempts": int(row.get("attempts") or 0)}
        m.setdefault("channel", "local")
        m.setdefault("convo", f"dm:{person['key']}")
        log(f"retrying held message {row['id']} from {person['key']} (attempt {m['_attempts']})")
        try:
            handle(m)
        except Exception as e:  # noqa: BLE001
            log(f"held retry crashed: {type(e).__name__}: {str(e)[:200]}")
        n += 1
    return n


def _single_instance():
    """Hold an OS lock for the life of the process. Two spine passes can call --ensure in the
    same second (seen live 2026-10-05: two bridges came up one second apart); the loser exits."""
    fh = open(core.DATA / "bridge.lock", "a+")
    try:
        if os.name == "nt":
            import msvcrt
            fh.seek(0)
            msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        fh.close()
        return None
    return fh


# One live Google Chat poller per machine, whatever data dir it runs from. bridge.lock lives in
# core.DATA, so a bridge started under a test's scratch store got its own lock and still answered the
# live space: 2026-10-25 seven such bridges (spawned by test runs, then self-respawning on every code
# change with the test env) plus the real one posted 8 replies to one message.
GCHAT_LOCK = Path.home() / ".claude" / "sage-gchat-bridge.lock"


def _gchat_lock():
    fh = open(GCHAT_LOCK, "a+")
    try:
        if os.name == "nt":
            import msvcrt
            fh.seek(0)
            msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        fh.close()
        return None
    return fh


def loop() -> None:
    core.DATA.mkdir(parents=True, exist_ok=True)
    lock = _single_instance()
    if lock is None:
        log(f"another bridge holds the lock; pid={os.getpid()} exiting")
        return
    glock = _gchat_lock() if _gchat() else None
    if _gchat() and glock is None:
        log(f"another bridge already polls the live Google Chat; pid={os.getpid()} data={core.DATA} exiting")
        lock.close()
        return
    STOP.unlink(missing_ok=True)
    log(f"=== bridge up pid={os.getpid()} gchat={'on' if _gchat() else 'off'} ===")
    last_tick = 0.0
    last_promises = 0.0
    started = time.time()
    respawn = False
    global ASYNC
    ASYNC = True
    threading.Thread(target=_tick_loop, name="ticks", daemon=True).start()
    try:
        n = replay_inflight()
        if n:
            log(f"replayed {n} turn(s) the previous bridge never answered")
    except Exception as e:  # noqa: BLE001
        log(f"inflight replay crashed: {type(e).__name__}: {str(e)[:200]}")
    while True:
        if STOP.exists():
            # a stop file (the builder after a merge, `--stop`) used to end the loop and leave the owners unanswered
            # until the spine's 10-minute --ensure (four gaps of 10-18 min on 2026-10-26, one of them the owner's "configure
            # yourself to respond in the organization"): the loop now restarts itself in place, like a code change
            log("stop file seen; restarting myself")
            respawn = True
            break
        if _code_changed(started):
            # a fix to any agent module lands at once instead of living only on disk: 2026-10-07 the
            # bridge ran 10:30's code all morning through three fixes. Since 2026-10-18 the dying
            # bridge starts its replacement itself (the spine's 10-minute restart left owners'
            # messages unanswered in the gap)
            log("agent code changed on disk; restarting myself on the new code")
            respawn = True
            break
        core.save(ALIVE, {"pid": os.getpid(), "ts": time.time(), "at": core.stamp()})
        try:
            step()
        except Exception as e:  # noqa: BLE001
            # one bad poll must not take the whole bridge down (and with it every turn in flight)
            log(f"step crashed: {type(e).__name__}: {str(e)[:200]}")
            time.sleep(POLL_S)
        if False:                                      # the ticks run on their own thread now (_tick_loop)
            last_tick = time.time()
            try:
                fired = routines.tick(post)
                if fired:
                    log(f"routines fired={fired}")
                followups_fire()
            except Exception as e:  # noqa: BLE001
                log(f"routines tick crashed: {type(e).__name__}: {str(e)[:200]}")
            try:
                if held_retry():
                    log("held messages re-run")
            except Exception as e:  # noqa: BLE001
                log(f"held retry crashed: {type(e).__name__}: {str(e)[:200]}")
            try:
                if actions.retry_events(post_in):
                    log("calendar retry settled")
            except Exception as e:  # noqa: BLE001
                log(f"calendar retry crashed: {type(e).__name__}: {str(e)[:200]}")
            try:
                if meeting_reminder.tick(post):
                    log("meeting reminder posted")
            except Exception as e:  # noqa: BLE001
                log(f"meeting reminder crashed: {type(e).__name__}: {str(e)[:200]}")
            try:
                if event_reminders.tick(post):
                    log("event reminder posted")
            except Exception as e:  # noqa: BLE001
                log(f"event reminder crashed: {type(e).__name__}: {str(e)[:200]}")
        if time.time() - last_promises >= PROMISES_S:
            last_promises = time.time()
            try:
                for s in commitments.check(receipts.poster()):          # one editable line per hand-off, where it was asked
                    log(f"commitment {s['id']} {s['verdict']}: {s['text'][:100]}")
            except Exception as e:  # noqa: BLE001
                log(f"commitments check crashed: {type(e).__name__}: {str(e)[:200]}")
        time.sleep(POLL_S)
    STOP.unlink(missing_ok=True)
    lock.close()                                   # the single-instance lock is free before the replacement starts
    if glock:
        glock.close()
    log("=== bridge stopped ===")
    if respawn:
        try:
            core.save(ALIVE, {})                   # ensure() must not see this pid as alive
            log(ensure())
        except Exception as e:  # noqa: BLE001
            log(f"respawn failed ({type(e).__name__}); the spine restarts me within 10 min")


def _tick_loop() -> None:
    """The minute ticks and the commitments check, off the reply path."""
    last_tick, last_promises = 0.0, 0.0
    while not STOP.exists():
        try:
            if time.time() - last_tick >= 60:
                last_tick = time.time()
                try:
                    fired = routines.tick(lambda k, t: post(k, t, "routine"))
                    if fired:
                        log(f"routines fired={fired}")
                    followups_fire()
                except Exception as e:  # noqa: BLE001
                    log(f"routines tick crashed: {type(e).__name__}: {str(e)[:200]}")
                try:
                    if held_retry():
                        log("held messages re-run")
                except Exception as e:  # noqa: BLE001
                    log(f"held retry crashed: {type(e).__name__}: {str(e)[:200]}")
                try:
                    if actions.retry_events(post_in):
                        log("calendar retry settled")
                except Exception as e:  # noqa: BLE001
                    log(f"calendar retry crashed: {type(e).__name__}: {str(e)[:200]}")
                try:
                    if meeting_reminder.tick(post):
                        log("meeting reminder posted")
                except Exception as e:  # noqa: BLE001
                    log(f"meeting reminder crashed: {type(e).__name__}: {str(e)[:200]}")
                try:
                    if event_reminders.tick(post):
                        log("event reminder posted")
                except Exception as e:  # noqa: BLE001
                    log(f"event reminder crashed: {type(e).__name__}: {str(e)[:200]}")
                try:
                    import intents
                    intents.sweep()
                except Exception:  # noqa: BLE001
                    pass
                try:
                    stalled_turns()
                except Exception as e:  # noqa: BLE001
                    log(f"stalled-turn check crashed: {type(e).__name__}: {str(e)[:200]}")
                try:
                    if capability.tick(_dispatch):
                        log("capability: an earlier ask replayed to the brain (its build landed)")
                except Exception as e:  # noqa: BLE001
                    log(f"capability tick crashed: {type(e).__name__}: {str(e)[:200]}")
                try:
                    # a long-running project whose step ended with no next step on record gets ONE planning turn (projects.py)
                    import projects
                    if projects.tick_plans(_dispatch):
                        log("projects: a planning turn dispatched to the brain")
                except Exception as e:  # noqa: BLE001
                    log(f"projects plan tick crashed: {type(e).__name__}: {str(e)[:200]}")
                try:
                    # the reflex's cheap detectors every minute (a usage limit with an account idle); the full pass
                    # with the job probes runs on the spine every 10 min (reflex.py, 2026-10-27)
                    import reflex
                    rr = reflex.tick(only=("limit", "account"))
                    if rr.get("steps") or rr.get("fixed"):
                        log(f"reflex: {rr}")
                except Exception as e:  # noqa: BLE001
                    log(f"reflex tick crashed: {type(e).__name__}: {str(e)[:200]}")
            if time.time() - last_promises >= PROMISES_S:
                last_promises = time.time()
                try:
                    for s in commitments.check(receipts.poster()):          # one editable line per hand-off, where it was asked
                        log(f"commitment {s['id']} {s['verdict']}: {s['text'][:100]}")
                except Exception as e:  # noqa: BLE001
                    log(f"commitments check crashed: {type(e).__name__}: {str(e)[:200]}")
            try:
                if placeholder_watch():
                    log("placeholder nudged")
            except Exception:  # noqa: BLE001
                pass
        except Exception as e:  # noqa: BLE001
            log(f"tick loop crashed: {type(e).__name__}: {str(e)[:200]}")
        time.sleep(5)


_code_check = {"at": 0.0}


def _code_changed(started: float) -> bool:
    """True when any .py or .md under assistant/agent (or chat_push/cards in scripts) is newer than
    this process. Checked at most every 30 s."""
    if time.time() - _code_check["at"] < 30:
        return False
    _code_check["at"] = time.time()
    try:
        files = list(core.AGENT.glob("*.py")) + [core.AGENT / "prompt.md", core.AGENT / "prompt.local.md", core.AGENT / "profile.json",
                                                core.AGENT.parent / "scripts" / "chat_push.py", core.AGENT.parent / "scripts" / "cards.py"]
        return any(p.exists() and p.stat().st_mtime > started + 2 for p in files)
    except OSError:
        return False


def _pid_running(pid: int) -> bool:
    if os.name != "nt":
        try:
            os.kill(pid, 0)
            return True
        except OSError:
            return False
    import ctypes                                 # os.kill(pid, 0) TERMINATES the process on Windows
    h = ctypes.windll.kernel32.OpenProcess(0x1000, False, int(pid))    # PROCESS_QUERY_LIMITED_INFORMATION
    if not h:
        return False
    code = ctypes.c_ulong()
    ok = ctypes.windll.kernel32.GetExitCodeProcess(h, ctypes.byref(code))
    ctypes.windll.kernel32.CloseHandle(h)
    return bool(ok) and code.value == 259         # STILL_ACTIVE


def alive(max_age_s: int = 1800) -> bool:
    """Running = a recent heartbeat AND that process still exists. A model turn can hold the
    loop for minutes, so the heartbeat window is generous; the pid check catches a crash."""
    a = core.load(ALIVE, {})
    return bool(a) and time.time() - float(a.get("ts", 0)) < max_age_s and _pid_running(int(a.get("pid", 0)))


def ensure() -> str:
    if alive():
        return "RUNNING"
    exe = Path(sys.executable)
    pyw = exe.with_name("pythonw.exe")
    flags = 0x00000008 | 0x00000200 if os.name == "nt" else 0          # DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP
    try:
        err = open(LOG.with_name("agent-bridge.err.log"), "a", encoding="utf-8")   # a crash leaves its traceback here, not in DEVNULL
    except OSError:
        err = subprocess.DEVNULL
    subprocess.Popen([str(pyw if pyw.exists() else exe), str(Path(__file__).resolve()), "--loop"],
                     cwd=str(core.VAULT), creationflags=flags, close_fds=err is subprocess.DEVNULL,
                     stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=err)
    return "STARTED"


if __name__ == "__main__":
    a = sys.argv[1:]
    if a[:1] == ["--loop"]:
        loop()
    elif a[:1] == ["--step"]:
        print(f"STEP handled={step()}")
    elif a[:1] == ["--stop"]:
        core.DATA.mkdir(parents=True, exist_ok=True)
        STOP.write_text("stop", encoding="utf-8")
        print("STOP requested")
    elif a[:1] == ["--health"]:                           # conn_check: one line + exit code
        print("running" if alive() else "down")
        sys.exit(0 if alive() else 1)
    elif a[:1] == ["--status"]:
        print(("RUNNING " if alive() else "DOWN ") + str(core.load(ALIVE, {})) + f" gchat={'on' if _gchat() else 'off'}"
              + f" burst_pending={burst_pending()}")
    else:
        print(ensure())

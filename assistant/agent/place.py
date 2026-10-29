"""place.py - where this message is, and what that asks of the reply.

the owner 2026-10-09: Sage should know "exactly the appropriate way of responding to everyone,
whether in a group chat where a message was received or in an email thread", replying in a
thread or directly. The model only sees text; this block, written by code from the message's
place (a DM, the shared home space, a thread, a card's thread) and the thread's own lines,
tells it who is reading, what the thread is about, and how to answer there.

Pure reads of people.json and the transcript. Nothing posted, nothing stored.
"""
from __future__ import annotations

import re
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import chat_thread  # noqa: E402
import core  # noqa: E402

THREAD_LINES = 10
OWN_LINES = 15
# "I said...", "I told you...", "like I said": the owner points back at their own earlier words
REFER_BACK = re.compile(r"\b(i|i've|i have|i'd|i had)\s+(already\s+|just\s+)?(said|told|mentioned|asked|wrote|sent)\b"
                        r"|\b(like|as|what) i (said|told|mentioned|meant|asked)\b", re.I)


def _others(person: dict) -> list:
    return [u["name"] for k, u in (core.people().get("users") or {}).items() if k != (person or {}).get("key")]


def _clip(s: str, n: int = 320) -> str:
    s = re.sub(r"\s+", " ", str(s or "")).strip()
    return s if len(s) <= n else s[:n].rsplit(" ", 1)[0] + "..."


def _thread_of(message_name: str) -> str:
    m = re.match(r"(spaces/[^/]+)/messages/([^/.]+)", str(message_name or ""))
    return f"{m.group(1)}/threads/{m.group(2)}" if m else ""


def _is_root(message_name: str) -> bool:
    """A top-level Chat message is spaces/S/messages/ID.ID; a reply in its thread is spaces/S/messages/ID.OTHER.
    Until 2026-10-29 this file compared only the thread part, so EVERY reply in a thread counted as a fresh
    top-level message: THIS THREAD SO FAR was never built for a reply, and the brain was handed the space's
    newest three lines instead (the Lensrentals / example / Chris guess under the back-taxes line)."""
    m = re.match(r"spaces/[^/]+/messages/([^/.]+)\.([^/.]+)$", str(message_name or ""))
    return bool(m) and m.group(1) == m.group(2)


def block(msg: dict, person: dict) -> str:
    """The WHERE block for one message. msg: {"channel", "convo", "thread", "direct", "why", "space"}."""
    convo, thread = str(msg.get("convo") or ""), str(msg.get("thread") or "")
    name = person.get("name", "the owner")
    others = _others(person)
    lines = []
    if msg.get("channel") == "local":
        lines.append(f"[WHERE: a private terminal chat with {name}. Only {name} reads this.]")
    elif convo.startswith("dm:"):
        lines.append(f"[WHERE: {name}'s private DM with you. Only {name} reads this; answer them directly.]")
    elif convo and convo == core.home_space():
        who = " and ".join([name] + others) if others else name
        if thread:
            lines.append(f"[WHERE: the shared home space ({who} all read it), INSIDE A THREAD. {name} wrote this in the thread, "
                         "so your reply lands in the same thread: keep to what this thread is about, and don't restart from the top.]")
        else:
            lines.append(f"[WHERE: the shared home space, where {who} all read. {name} wrote this at the top level. "
                         "Keep it short; everyone sees it. If it concerns the other owner, you can speak to both.]")
    elif convo:
        title = str(msg.get("space_title") or "").strip()
        lines.append(f"[WHERE: in {title or 'a shared space'}{' (in a thread)' if thread else ''}; {name} wrote this. A TEAM space: "
                     "team members who are not owners read everything here, so business only, nothing from the owners' personal lanes; "
                     + ("your reply lands in the same thread." if thread else "keep it short.") + "]")
    own_root = bool(thread) and _is_root(str(msg.get("msg_name") or ""))
    space = str(msg.get("space") or (convo if convo.startswith("spaces/") else "")) if msg.get("channel") == "gchat" else ""
    msg_name = str(msg.get("msg_name") or "")
    if not thread or own_root:
        # a fresh top-level message (on a phone every new message is one): it most likely answers the newest
        # thing posted here before it. 2026-10-11 the owner's "I don't need this anymore" was one of these, and the
        # brain was left to guess among everything in the space. Since 2026-10-29 the newest things come from
        # Chat itself (chat_thread.space_tail), what is really on their screen; the transcript is the fallback.
        prior = None
        if space:
            tail = chat_thread.space_tail(space, 3 + 2, exclude=msg_name)
            if tail is not None:
                local = core.transcript_tail(convo, 60)
                prior = [r for r in chat_thread.merge_local(tail, local) if r.get("who") != name][-3:]
        if prior is None:
            prior = [r for r in core.transcript_tail(convo, 30) if r.get("who") != name][-3:]     # name = this owner; keep the agent's, cards', reports'
        if prior:
            lines.append("WHAT THEY ARE MOST LIKELY RESPONDING TO (the newest things posted here before this message, oldest first"
                         + (", read from Google Chat itself" if space and prior and prior[0].get("name") else "")
                         + "; 'it', 'this', 'that' point at the last one unless their words say otherwise):")
            lines += [f"  [{r.get('ts', '')[5:16]}]{' (in a thread)' if r.get('thread') and not r.get('root') else ''} {r.get('who')}: {_clip(r.get('text'))}"
                      for r in prior]
    if thread and not own_root:
        # the thread as Google Chat holds it, complete (2026-10-29); the transcript only when Chat cannot be read
        rows = chat_thread.thread_lines(space, thread, exclude=msg_name) if space else None
        from_chat = rows is not None
        if from_chat:
            rows = chat_thread.merge_local(rows, core.transcript_thread(convo, thread, 60))[-THREAD_LINES:]
        else:
            rows = core.transcript_thread(convo, thread, THREAD_LINES)
        rows = [r for r in rows if not (r.get("who") == name and _clip(r.get("text"), 100) == _clip(msg.get("text"), 100))]
        if rows:
            lines.append("THIS THREAD SO FAR (" + ("read from Google Chat itself, complete; " if from_chat else "")
                         + "oldest first; the newest is what they are replying to"
                         + ("; the first line is the message that started the thread" if rows[0].get("root") else "") + "):")
            lines += [f"  [{r.get('ts', '')[5:16]}] {r.get('who')}: {_clip(r.get('text'))}" for r in rows]
            if len(rows) == 1:
                lines.append(f"They are replying to this ONE message. 'It', 'they', 'this', 'that' and 'the ...' mean what it says "
                             f"({_clip(rows[0].get('text'), 80)}); nothing else in the space is what they mean. Answer about it; do not ask which. "
                             "Their words refer to THIS thread's message, not to the newest thing elsewhere in the space.")
            else:
                lines.append("Their words refer to THIS thread's messages, not to the newest thing elsewhere in the space.")
        elif from_chat:
            lines.append("THIS THREAD SO FAR: Google Chat shows no earlier message in this thread (the message they replied under was deleted).")
    try:
        rb = recent_block(convo, thread, name)
        if rb:
            lines.append(rb)
        cb = cards_block(convo)
        if cb:
            lines.append(cb)
    except Exception:  # noqa: BLE001   never let context assembly stop a reply
        pass
    if REFER_BACK.search(str(msg.get("text") or "")):
        # 2026-10-12 "I said..." met two wrong guesses: the owner's words were in another thread or before the
        # visible window. Their own lines from the last 24 hours, every thread and the top level.
        since = time.strftime("%Y-%m-%d %H:%M", time.localtime(time.time() - 86400))
        own = [r for r in core.transcript_tail(convo, 400) if r.get("who") == name and str(r.get("ts") or "") >= since
               and _clip(r.get("text"), 100) != _clip(msg.get("text"), 100)][-OWN_LINES:]
        if own:
            lines.append(f"THEY ARE REFERRING BACK TO SOMETHING THEY SAID. {name}'s own messages here in the last 24 hours "
                         "(oldest first, from every thread and the top level); find what they mean in these before asking:")
            lines += [f"  [{r.get('ts', '')[5:16]}]{' (in a thread)' if r.get('thread') else ''} {name}: {_clip(r.get('text'))}" for r in own]
    return "\n".join(lines)

# ------------------------------------------------------------------ what was just going on, and the last cards (2026-10-25)
# the owner: "It should never have to assume what I am talking about: it should be able to tell by what thread it is
# and what we were just talking about or what card it just sent me to approve." Two more blocks on every message,
# written by code from the transcript and the card store: the last hour of this conversation across every thread
# (each line tagged with its thread's subject) and the cards posted in the last few hours with where each stands.
RECENT_MIN = 60
RECENT_LINES = 10
CARDS_HOURS = 3.0
CARDS_MAX = 6


def _ts_epoch(ts: str) -> float:
    try:
        return time.mktime(time.strptime(str(ts or "")[:16], "%Y-%m-%d %H:%M"))
    except ValueError:
        return 0.0


def _thread_subject(convo: str, thread: str, cache: dict) -> str:
    """A few words that say what a thread is about: its first line on record (the owner's ask, the card's header)."""
    if not thread:
        return "top level"
    if thread in cache:
        return cache[thread]
    rows = core.transcript_thread(convo, thread, 60)          # the FIRST line on record (the owner's ask, the card), not the newest
    txt = str(rows[0].get("text") or "") if rows else str(core.thread_root(thread).get("text") or "")
    m = re.match(r"\[card q[0-9a-f]{12} \S* ?\S*\] ([^:\n]{3,70})", txt)
    subj = m.group(1) if m else re.sub(r"^\[[^\]]*\]\s*", "", txt)
    subj = _clip(subj, 48) or "thread " + thread.rsplit("/", 1)[-1][-6:]
    cache[thread] = subj
    return subj


def recent_block(convo: str, thread: str, name: str, now: float | None = None) -> str:
    """JUST BEFORE THIS: the last RECENT_MIN minutes of this conversation, every thread and the top level, newest
    last, each line tagged with its thread's subject; the current thread's own lines are left out (THIS THREAD
    SO FAR has them). So "the project", "the card", "him" resolve to what was just on, not to a guess."""
    now = now or time.time()
    since = now - RECENT_MIN * 60
    rows = [r for r in core.transcript_tail(convo, 80) if _ts_epoch(r.get("ts")) >= since and str(r.get("thread") or "") != thread]
    rows = rows[:-1] if rows and rows[-1].get("who") == name and not thread else rows     # the line being answered now
    rows = rows[-RECENT_LINES:]
    if not rows:
        return ""
    cache: dict = {}
    out = ["JUST BEFORE THIS (the last hour here, every thread, oldest first; each line says which thread it was in; "
           "a short line, 'it', 'the project', 'him', 'the card' points at the newest of these that fits, once the lines of the current thread above do not settle it):"]
    for r in rows:
        tag = _thread_subject(convo, str(r.get("thread") or ""), cache)
        out.append(f"  [{str(r.get('ts') or '')[5:16]}] ({tag}) {r.get('who')}: {_clip(r.get('text'), 200)}")
    return "\n".join(out)


def _card_state(card: dict, idx: dict, tickets: dict) -> str:
    st = str(card.get("status") or "open")
    e = idx.get(card.get("id")) or {}
    if st == "open":
        return "open, waiting for a tap" + (" (" + ", ".join(o.get("label", "") for o in ((card.get("questions") or [{}])[0].get("options") or [])[:3]) + ")" if (card.get("questions") or [{}])[0].get("options") else "")
    if st in ("answered", "consumed"):
        when = str(e.get("ts") or "")[11:16]
        opt = str(e.get("option") or "").replace(" (Recommended)", "")
        t = tickets.get(str(e.get("ticket") or "")) or {}
        if t:
            ts_ = str(t.get("status") or "")
            if ts_ == "done":
                return f"tapped '{opt}' at {when}; went out {str(t.get('ts_done') or '')[11:16]} ({_clip(t.get('result'), 60)})"
            if ts_ == "queued":
                return f"tapped '{opt}' at {when}; waiting to go out" + (f" at {t.get('not_before')}" if t.get("not_before") else " (next drain, within 10 min)")
            return f"tapped '{opt}' at {when}; {ts_}: {_clip(t.get('result'), 80)}"
        return f"tapped '{opt}' at {when}" if opt else "answered"
    if st == "superseded":
        return "superseded by a revised card"
    return st


def cards_block(convo: str = "", hours: float = CARDS_HOURS, now: float | None = None) -> str:
    """YOUR LAST CARDS: every card posted in the last `hours`, newest last, with where it stands (open / tapped and
    sent / tapped and waiting / superseded). "Remake the card", "the clarification card", "the one you just sent me"
    resolve here, and a card that already went is never described as waiting."""
    try:
        sys.path.insert(0, str(core.AGENT.parent / "scripts"))
        import cards
        import exec_store as es
    except Exception:  # noqa: BLE001
        return ""
    now = now or time.time()
    since = now - hours * 3600
    found = []
    for d in (cards.LIVE, cards.DONE):
        try:
            files = list(d.glob("q*.json"))
        except OSError:
            continue
        for f in files:
            try:
                if f.stat().st_mtime < since - 6 * 3600:
                    continue
            except OSError:
                continue
            c = cards._read(f)
            if not isinstance(c, dict) or float(c.get("ts") or 0) < since:
                continue
            found.append(c)
    if not found:
        return ""
    found.sort(key=lambda c: float(c.get("ts") or 0))
    found = found[-CARDS_MAX:]
    idx = cards._answers_index()
    tickets = es.fold()
    cache: dict = {}
    out = ["YOUR LAST CARDS (posted in the last few hours, oldest first, with where each stands now; 'the card' / 'remake it' / "
           "'the one you sent me' means the newest that fits):"]
    for c in found:
        q = (c.get("questions") or [{}])[0]
        where = _thread_subject(convo, str(c.get("chat_thread") or ""), cache) if c.get("chat_thread") else "top level"
        out.append(f"  [{time.strftime('%m-%d %H:%M', time.localtime(float(c.get('ts') or 0)))}] {c['id']} '{_clip(q.get('header'), 60)}' "
                   f"(in: {where}): {_card_state(c, idx, tickets)}")
    return "\n".join(out)


"""correction.py - an owner's follow-up that says the last answer did not do it is the ask again, by code.

the owner, 2026-10-27: "When I have an interaction with Sage where I ask it to do something and it replies and I respond
'no that's not what I asked' or 'where is the approval card' or whatever it is I follow up with, Sage needs to fix
whatever it was. It cannot just respond, say sorry, and never change."

The week before, from the transcript: "You told me the card is below, but you never sent one" (10-22) -> "You're right,
and I'm sorry ... I can't see why", the card came 90 minutes later; "Why is it not built?" (10-25) -> "I can't see why
from here"; "Try again" (10-26) -> "Sorry, I told you the card was below and I can't back that up"; "Ticket refused.
Fix it so that doesn't happen again" -> "Which ticket do you mean?". The model took each correction as a conversation
and answered it with words. Nothing in code treated it as the original ask coming back.

What happens now, around the unchanged brain:

  detect   `kind(text)`: a correction is a follow-up that points back at the last answer and says it did not do it:
           card   "where is the (approval) card", "no card", "you never sent one", "it never came / posted"
           redo   "that's not what I asked", "try again", "do it again", "that's wrong", "redo"
           build  "why is it not built", "still not built", "not working"
           fix    "fix it", "fix it so that doesn't happen again"
           ''     anything else (a factual "No, Diane texted us" is a correction of a FACT: `learn` / `remember`
                  is its deed and nothing is forced)
  the ask  `original_ask(convo, thread, person)`: the owner's last real ask before the correction, in the same thread
           first (the card thread, the DM), then the conversation: not a tap, not an ack, not itself a correction.
  the turn the brain gets a CORRECTION block carrying the original ask and what is expected; its reply must do it this
           turn ("sorry" is three words at most). Code then checks the deeds the turn put forward:
             - an ask for a message with no card/task -> the task is queued from the owner's words (the bridge's net);
             - a job for the doer with no `do` -> the job is queued;
             - an apology with nothing done (kind card/redo/fix) -> ONE re-run of the ORIGINAL ask with the apology
               named as what not to do; the second reply replaces the first.
  ledger   data/agent/corrections.jsonl: one row per correction (kind, the ask, what was expected, what the turn put
           forward, what code forced, whether a re-run was needed). The reflex reads it: a correction that still ended
           in words alone is a finding -> ONE build request with the pair (the ask, the reply), so the gap is fixed in
           code, not in chat.

Generic: no organization, person or service is named here.
"""
from __future__ import annotations

import re
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import core  # noqa: E402

LEDGER = core.DATA / "corrections.jsonl"
LOOKBACK_ROWS = 80
LOOKBACK_S = 48 * 3600

_KINDS = (
    ("card", re.compile(r"where(?:'s| is| are)? (?:the |my |that |your )?(?:approval |send )?cards?\b|\bno cards?\b|never (?:sent|came|posted|showed|arrived|appeared|got) (?:one|a card|it|through|here)?|"
                        r"(?:it|that|the card|card) (?:was|were)? ?never (?:sent|posted|came|made)|didn'?t (?:send|post|make|get) (?:one|a card|it|the card)|"
                        r"(?:card|cards) (?:is|are|was|were)? ?not (?:posting|posted|here|showing|coming)|not posting|you (?:told|said) .{0,40}\b(?:card|below)\b.{0,40}\b(?:but|never|didn'?t|no)\b", re.I)),
    ("build", re.compile(r"(?:why|how come) (?:is|isn'?t|was|wasn'?t) (?:it|that|this) (?:still )?not built|(?:still )?not built|isn'?t built|not working|still (?:doesn'?t|does not|isn'?t|is not) work", re.I)),
    ("fix", re.compile(r"\bfix (?:it|that|this)\b|so (?:that |it )?(?:doesn'?t|does not|won'?t|never) happens? again|make sure (?:it|that) (?:doesn'?t|never)", re.I)),
    ("redo", re.compile(r"not what I (?:asked|meant|said|wanted)|that'?s (?:wrong|not it|not right|not what)|wrong (?:person|number|card|one|thread|draft)|\b(?:try|do (?:it|that)|send (?:it|that)|make (?:it|that)|post (?:it|that)|run (?:it|that)) again\b|"
                        r"\bredo (?:it|that|the)\b|didn'?t (?:do|work|happen|go)\b|still (?:nothing|waiting|no(?:thing)?\b|not (?:done|here|there))|you (?:didn'?t|did not|never) (?:do|send|make|post|put|answer|reply)", re.I)),
)
# "Again" / "Redo" on its own line is a redo; "Again, I keep saying this, you can deduce..." is a preference, not one
_REDO_SHORT = re.compile(r"^(?:ok(?:ay)?,? |sorry,? |please |yes,? )?(?:again|redo|retry|try again|do it again|do it over|one more time)\b", re.I)
_SHORT_WORDS = 8
_NOT_A_CORRECTION = re.compile(r"^\[(?:tapped|undid|voice note)|^/", re.I)
_DOER = re.compile(r"\b(?:upload|download|sign (?:in|into)|log (?:in|into)|look (?:it |that |this )?up|look up|check (?:the |my |our )?(?:site|page|portal|account|status|listing|order|calendar|dmv|bank)|"
                   r"fill (?:out|in)|submit|renew|book|order|buy|purchase|pay (?!attention)|search|open (?:the |my )?(?:site|page|portal|account)|go to|register|cancel|schedule (?:the|an?|my)|"
                   r"update (?:the |my )?(?:site|page|portal|listing|profile|price)|change (?:the |my )?(?:price|listing|profile|template)|put (?:it|them|the \w+) (?:in|into|on)|move (?:it|them|the \w+) (?:in|into|to)|export|import|copy (?:it|them|the \w+) (?:to|into))\b", re.I)
_APOLOGY = re.compile(r"\b(?:sorry|apolog|you'?re right|you are right|I was wrong|my mistake|my fault|I can'?t see why|I can'?t tell why|I don'?t know why)\b", re.I)
_DEED_KEYS = ("task", "do", "card", "call", "note", "build", "project", "schedule", "event", "approve", "card_revise", "switch")


def kind(text: str) -> str:
    """'card' | 'build' | 'fix' | 'redo' | '' for an owner's line."""
    t = re.sub(r"\s+", " ", str(text or "")).strip()
    if not t or _NOT_A_CORRECTION.match(t) or len(t) > 400:
        return ""
    for k, rx in _KINDS:
        if rx.search(t):
            return k
    if _REDO_SHORT.match(t) and len(t.split()) <= _SHORT_WORDS:
        return "redo"
    return ""


def _is_ask(text: str) -> bool:
    t = str(text or "").strip()
    if not t or _NOT_A_CORRECTION.match(t) or kind(t):
        return False
    words = t.split()
    if len(words) < 3:
        return False
    return not re.match(r"^(?:ok|okay|thanks|thank you|yes|no|yep|nope|got it|great|perfect|cool|sure)\b[\w\s!.]{0,12}$", t, re.I)


def original_ask(convo: str, thread: str, person: dict, before: str = "", rows: list | None = None) -> dict:
    """The owner's last real ask before `before` (a stamp, '' = now): in this thread first, then the conversation.
    -> {text, ts, thread} or {}."""
    rows = rows if rows is not None else core.rows(core.TRANSCRIPT)[-LOOKBACK_ROWS * 4:]
    name = str((person or {}).get("name") or "")
    cut = time.strftime("%Y-%m-%d %H:%M", time.localtime(time.time() - LOOKBACK_S))
    mine = [r for r in rows if str(r.get("who") or "") == name and str(r.get("convo") or "") == str(convo or "")
            and str(r.get("ts") or "") >= cut and (not before or str(r.get("ts") or "") < before)]
    for scope in ((thread,) if thread else ()) + ("",):
        pool = [r for r in mine if str(r.get("thread") or "") == scope] if scope else mine
        for r in reversed(pool[-LOOKBACK_ROWS:]):
            if _is_ask(r.get("text")):
                return {"text": str(r.get("text") or "").strip(), "ts": str(r.get("ts") or ""), "thread": str(r.get("thread") or "")}
    return {}


def expected_deed(ask: str) -> str:
    """What the original ask implied: 'task' (a message out), 'do' (work somewhere), '' (words or a decision)."""
    import claims
    a = str(ask or "")
    if claims.asked_for_message(a):
        return "task"
    if _DOER.search(a):
        return "do"
    return ""


HERE_LOOKBACK_S = 3 * 3600


def card_in_thread(thread: str, ask: dict | None = None, now: float | None = None) -> dict:
    """The card that already landed in this thread since the ask (open, or tapped), or {}. "Where is the card"
    in a thread that has one is not a missing card: 2026-10-18 16:07 the example card posted two minutes after the reply,
    the owner asked where it was in that thread (he had tapped Send the same minute), and the reply said it "closed unsent"
    and made a second one, which could have sent the same text twice."""
    m = re.search(r"/threads/([^/]+)$", str(thread or ""))
    if not m:
        return {}
    try:
        sys.path.insert(0, str(core.AGENT.parent / "scripts"))
        import cards
    except Exception:  # noqa: BLE001
        return {}
    now = now or time.time()
    since = now - HERE_LOOKBACK_S
    if ask and ask.get("ts"):
        try:
            since = max(since, time.mktime(time.strptime(str(ask["ts"])[:16], "%Y-%m-%d %H:%M")))
        except ValueError:
            pass
    best = {}
    for d in (cards.LIVE, cards.DONE):
        try:
            files = [f for f in d.glob("q*.json") if f.stat().st_mtime >= since - 60]
        except OSError:
            continue
        for f in files:
            c = cards._read(f)
            if not isinstance(c, dict) or float(c.get("ts") or 0) < since - 60 or c.get("status") in ("expired", "resolved", "superseded"):
                continue                              # a card that closed untapped is gone: making it again is right
            names = [str(n) for n in (c.get("chat") or {}).values()]
            if str(c.get("chat_thread") or "") == thread or any(re.search(r"/messages/" + re.escape(m.group(1)) + r"\.", n) for n in names):
                if float(c.get("ts") or 0) >= float(best.get("ts") or 0):
                    best = c
    return best


def here_note(card: dict) -> str:
    """The block for 'where is the card' when the card is right there: say where and how it stands, make nothing."""
    try:
        sys.path.insert(0, str(core.AGENT.parent / "scripts"))
        import cards
        import exec_store as es
        import place
        state = place._card_state(card, cards._answers_index(), es.fold())
    except Exception:  # noqa: BLE001
        state = str(card.get("status") or "open")
    q = (card.get("questions") or [{}])[0]
    at = time.strftime("%H:%M", time.localtime(float(card.get("ts") or 0)))
    return ("[CORRECTION, from the owner: they ask where the card is. It is already in this thread: "
            f"'{str(q.get('header') or 'the card')[:120]}', posted {at}, now {state}. Do NOT make another card (a second one "
            "could send the same message twice) and never say it closed unsent unless the state above says so. Tell them in "
            "one short line where it is and how it stands; if it is still open, the tap on it is what sends.]")


def note(k: str, text: str, ask: dict, expected: str, rerun: bool = False) -> str:
    """The CORRECTION block for the brain, with the original ask inside."""
    want = {"task": "the message, with the `task` (and its `draft`) so the card is right here this turn",
            "do": "the job, with `do`, so it starts now",
            "": "whatever it asked for, with the deed that does it (`task`, `do`, `card`, `note` with a build request, `project`)"}[expected]
    head = ("[CORRECTION, from the owner: their last line says your previous answer did not do what they asked"
            + (" (a card they expected is not there: make it again now, this turn, whatever your record says about it)" if k == "card" else "")
            + (" (something keeps failing: say in one line what you are changing, by a `note` build request if code must change, and do the ask again)" if k == "fix" else "")
            + (" (a build they are waiting on: answer from BUILDS, and if it is not in there, file it again now)" if k == "build" else "")
            + f".\nTheir ask was ({ask.get('ts', '')[11:16]}): \"{str(ask.get('text') or '')[:500]}\"\n"
            f"Do it now, this turn: {want}. \"Sorry\" is three words at most; an apology is never the answer by itself, "
            "and never \"I can't see why\". If something stops you, say the one thing and do every other part.")
    if rerun:
        head += ("\nYour reply a moment ago was an apology with nothing put forward; it was not sent. This is the one "
                 "re-run: the deed this time, or the one fact that stops you, nothing else.")
    return head + "]"


def apology_only(reply: str, sug: dict | None) -> bool:
    """The reply says sorry / I can't see why and puts nothing forward, and asks nothing back."""
    r = str(reply or "").strip()
    sug = sug if isinstance(sug, dict) else {}
    if not r or any(sug.get(k) for k in _DEED_KEYS):
        return False
    if r.rstrip("`").rstrip().endswith("?") and r.count("?") <= 2:
        return False
    return bool(_APOLOGY.search(r))


def force(expected: str, person: dict, ask: dict, text: str, reply: str, ctx: dict, applied: list) -> str:
    """The deed the original ask implied, queued by code when the turn still has none. -> 'task' | 'do' | ''."""
    import actions
    import claims
    kinds = {str(a.get("kind") or "") for a in (applied or [])}
    if not expected or kinds & {"task", "card", "do"}:
        return ""
    # "Again:" first, so the ticket's id and title differ from the first try's (tickets dedupe on their opening words)
    words = f"Again: {str(ask.get('text') or '').strip()[:600]}\n\n({person.get('name', 'The owner')} followed up: {str(text or '').strip()[:200]})"
    lane = claims.lane_guess(str(ask.get("text") or "") + " " + str(text or ""))
    try:
        if expected == "task":
            tid = actions._queue_task(lane, claims.forced_task_text(person.get("name", "The owner"), words, reply), person, ctx)
        else:
            tid = actions._queue_do(lane, words, person, ctx)
    except Exception as e:  # noqa: BLE001
        core.log_change("correction_error", f"force {expected}: {type(e).__name__}: {str(e)[:120]}", {"op": "none"}, "code")
        return ""
    if tid:
        actions.LAST_APPLIED.append({"kind": expected, "what": str(ask.get("text") or "").strip()[:160], "ref": tid if isinstance(tid, str) else ""})
        return expected
    return ""


def record(person: dict, k: str, text: str, ask: dict, expected: str, applied: list, forced: str, rerun: bool, reply: str,
           convo: str = "", thread: str = "", here: str = "") -> dict:
    row = {"ts": core.stamp(), "who": str(person.get("key") or ""), "kind": k, "text": str(text or "")[:300],
           "ask": str(ask.get("text") or "")[:300], "ask_ts": str(ask.get("ts") or ""), "expected": expected,
           "applied": sorted({str(a.get("kind") or "") for a in (applied or [])}), "forced": forced, "rerun": rerun,
           "words_only": bool(expected or k in ("card", "redo", "fix")) and not (applied or forced or here),
           "reply": str(reply or "")[:300], "convo": convo, "thread": thread}
    if here:
        row["card_here"] = here                       # the card was in the thread: words were the right answer
    core.append(LEDGER, row)
    return row


def handed_on(row: dict) -> str:
    """The capability gap this correction's turn opened (capability.py, same person, conversation and thread, at or
    after the ask, about the ask or the correction) -> its id, or ''. A reply that says it cannot do the ask and
    files the build for that ability did not end in words alone: 2026-10-29 09:49 "fix that context awareness" met
    "I can't see the message at the top of a thread"; gap g-bd526d was filed that turn, built and replayed by 11:27,
    yet the ledger row (written before the capability check runs) still sent the reflex after it."""
    try:
        import capability
        gaps = capability._load().get("gaps") or {}
    except Exception:  # noqa: BLE001
        return ""
    ts, ask_ts = str(row.get("ts") or "")[:16], str(row.get("ask_ts") or row.get("ts") or "")[:16]
    for g in gaps.values():
        if g.get("status") == "dropped" or str(g.get("person") or "") != str(row.get("who") or ""):
            continue
        if str(g.get("convo") or "") != str(row.get("convo") or "") or str(g.get("thread") or "") != str(row.get("thread") or ""):
            continue
        when = str(g.get("when") or "")[:16]
        if when < ask_ts:
            continue
        if when == ts or capability.same_ask(g.get("ask", ""), row.get("ask", "")) or capability.same_ask(g.get("ask", ""), row.get("text", "")):
            return str(g.get("id") or "")
    return ""


def recent(hours: float = 24.0) -> list:
    cut = time.strftime("%Y-%m-%d %H:%M", time.localtime(time.time() - hours * 3600))
    return [r for r in core.rows(LEDGER) if str(r.get("ts") or "") >= cut]


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    if len(sys.argv) > 1 and sys.argv[1] == "kind":
        print(kind(" ".join(sys.argv[2:])) or "-")
    else:
        for r in recent(float(sys.argv[1]) if len(sys.argv) > 1 else 24):
            print(f"{r['ts']} {r['who']} {r['kind']:5} expected={r['expected'] or '-':4} applied={','.join(r['applied']) or '-'} forced={r['forced'] or '-'} "
                  f"rerun={r['rerun']} words_only={r['words_only']} | {r['text'][:60]} <- {r['ask'][:60]}")

"""resume.py - "I'm done" after a takeover: the owner finished a step on the PC, and the job carries on, by code.

2026-11-09. Two hand-overs leave a browser job waiting on the owner: a sign-in they take themselves (signin.py ->
the Carry on card) and a step only they can take, left open in the browser with Control PC and Do later
(doer/leftopen.py). Both expected a tap; the owner, back from Chrome Remote Desktop, says it in words instead:
"done", "I'm done", "finished", "signed in", "I'm in", "carry on", "your turn", "all yours", "back to you".

`handle(text, person)` -> the reply line, or '' when the message is not that or nothing is waiting:
  a Carry on card still open (sign-in by hand)  -> answered 'Carry on' by code (what the tap does), the browser agent woken
  a page left open for the owner                 -> the hold is released (leftopen.resume), the job is staged again from its
                                                    notes, the browser agent woken
  nothing waiting                                -> '' (the brain answers a plain "done" as conversation)

Owners only, zero LLM, org-free. The job's own irreversible steps still stop at a card; this only lets it go on.
"""
from __future__ import annotations

import re
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
for _p in (HERE, HERE.parent / "scripts", HERE.parent / "doer"):
    if str(_p) not in sys.path:
        sys.path.append(str(_p))
import core  # noqa: E402

LOOKBACK_H = 12.0
_CURLY = str.maketrans({"’": "'", "‘": "'"})
_RESUME = re.compile(r"^(?:ok(?:ay)?,? |alright,? |so,? |yes,? |yep,? |done,? )*(?:"
                     r"(?:i'?m|i am|im|it'?s|its|that'?s|all) (?:done|finished|in|signed in|logged in|sorted|set|good to go|complete)|"
                     r"done(?: with (?:it|that|the (?:step|sign.?in|login|page)))?|finished(?: it| that| the step)?|did it|i did (?:it|that|the step|my part)|"
                     r"signed in|logged in|(?:you can )?(?:carry on|go on|go ahead|continue|proceed|take (?:it )?(?:from here|over|it back))|"
                     r"your turn|all yours|back to you|over to you|pick it (?:back )?up|resume|keep going|handled(?: it)?|took care of it)"
                     r"(?: now)?[.!]*\s*$", re.I)
_NOT = re.compile(r"\b(?:not|never|isn'?t|aren'?t|haven'?t|can'?t|couldn'?t|didn'?t|won'?t|no)\b", re.I)
MAX_WORDS = 10


def asks(text: str) -> bool:
    t = " ".join(str(text or "").translate(_CURLY).split())
    if not t or t.startswith(("[", "/")) or len(t.split()) > MAX_WORDS or _NOT.search(t):
        return False
    return bool(_RESUME.match(t))


def _carry_on_cards(now: float) -> list:
    """Open Carry on cards (report.carry_on_card: agent_run + login + self), newest first."""
    out = []
    try:
        import cards
        import report
    except Exception:  # noqa: BLE001
        return out
    try:
        files = sorted(cards.LIVE.glob("q*.json"), key=lambda p: p.stat().st_mtime, reverse=True)[:120]
    except OSError:
        return out
    for f in files:
        c = cards._read(f) or {}
        if not c or c.get("status") != "open" or now - float(c.get("ts") or 0) > LOOKBACK_H * 3600:
            continue
        q = (c.get("questions") or [{}])[0] or {}
        ex = q.get("exec") if isinstance(q.get("exec"), dict) else {}
        p = ex.get("payload") or {}
        labels = [str(o.get("label") or "") for o in q.get("options") or []]
        if ex.get("kind") == "agent_run" and p.get("login") and p.get("self") and report.CARRY_ON in labels:
            out.append((c, p))
    out.sort(key=lambda x: float(x[0].get("ts") or 0), reverse=True)
    return out


def _wake() -> None:
    try:
        import doer
        doer.wake()
    except Exception:  # noqa: BLE001
        pass


def handle(text: str, person: dict, now: float | None = None) -> str:
    if not person or not core.is_owner(person) or not asks(text):
        return ""
    now = now or time.time()
    by = f"{person.get('key') or 'owner'}:chat (said done)"
    try:
        found = _carry_on_cards(now)
        if found:
            import cards
            import report
            card, p = found[0]
            r = cards.answer(str(card.get("id") or ""), report.CARRY_ON, by=by)
            if r.get("ok"):
                _wake()
                host = str(p.get("host") or "the site")
                return f"Got it, you're in at {host}. I'm picking the job up from where it stopped; I'll report here when it's through."
    except Exception:  # noqa: BLE001
        pass
    try:
        import leftopen
        open_ids = leftopen.holding()
        if open_ids:
            tid = open_ids[-1]
            r = leftopen.resume(tid, person)
            if r.get("ok"):
                _wake()
                job = f" on {r['job']}" if r.get("job") else ""
                return f"Thanks, I'll carry on{job} from where you left it and report here."
    except Exception:  # noqa: BLE001
        pass
    return ""


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    print("yes" if asks(" ".join(sys.argv[1:])) else "no")

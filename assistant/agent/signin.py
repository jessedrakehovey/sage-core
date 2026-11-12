"""signin.py - "let me sign in" is one step, done by code (2026-11-04).

An owner who says they will sign in themselves ("Let me sign in the browser", "I'll log in myself", "let me do it"
under a sign-in card) wants ONE thing: the sign-in page open in the agent's browser, in front, and a card to tap
when they are in. On 2026-11-04 that took five messages and two hours forty: the reply was "Go ahead" twice with
nothing open, because the model answered in words and nothing pressed the sign-in card's own "I'll sign in myself".

`owner_signs_in` is that press, by code, no model turn:

  a sign-in card of a browser job, still open           -> it is answered "I'll sign in myself"
  the job's sign-in card is closed (answered, refused)  -> the card is posted again for that job, then answered
  no sign-in card in the last LOOKBACK_H hours          -> '' (the message goes to the model as before)

Either way the browser agent is woken: it puts its browser at the sign-in page, in front, and posts the Carry on card
in the job's thread (doer.self_signin). The reply names Control PC as the window, never the computer as the step
(chatfirst.py).

Zero LLM, org-free. Reads the cards and the job tickets; the only writes are cards.answer / report.login, the same
two calls a tap on the card makes.
  python assistant/agent/signin.py "<text>"       # does this line ask for it? (prints yes / no; changes nothing)
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

LOOKBACK_H = 6.0
_CURLY = str.maketrans({"’": "'", "‘": "'"})
# the owner takes the sign-in: "let me sign in ...", "I'll log in myself", "I want to sign in", "let me do it"
_ASK = re.compile(r"^(?:ok(?:ay)?,? |no,? |just |please |so )*(?:let me|i(?:'ll| will| can| want to| would rather)|i'd rather) (?:just )?"
                  r"(?:(?:sign|log)[ -]?(?:in|on)(?:to)?\b|do (?:it|that|the (?:sign|log)[ -]?in)\b)"
                  r"|\bi(?:'ll| will) (?:sign|log) in (?:myself|by hand)\b|\blet me (?:sign|log) in\b", re.I)


def asks(text: str) -> bool:
    t = " ".join(str(text or "").translate(_CURLY).split())
    return bool(t) and len(t) <= 200 and not t.startswith(("[", "/")) and bool(_ASK.search(t))


def _login_cards(cards, self_label: str, now: float) -> list:
    """Sign-in cards (report.build_login_question) from the last LOOKBACK_H hours, newest first:
    [(card, payload, still open?)]."""
    out = []
    seen = set()
    pools = [(cards.LIVE, True), (cards.DONE, False), (cards.ARCHIVE, False)]
    for folder, live in pools:
        try:
            files = sorted(folder.glob("q*.json"), key=lambda p: p.stat().st_mtime, reverse=True)[:120]
        except OSError:
            continue
        for f in files:
            c = cards._read(f) or {}
            if not c or c.get("id") in seen or now - float(c.get("ts") or 0) > LOOKBACK_H * 3600:
                continue
            q = (c.get("questions") or [{}])[0] or {}
            ex = q.get("exec") if isinstance(q.get("exec"), dict) else {}
            p = ex.get("payload") or {}
            labels = [str(o.get("label") or "") for o in q.get("options") or []]
            if ex.get("kind") != "agent_run" or not p.get("login") or p.get("self") or self_label not in labels:
                continue
            seen.add(c.get("id"))
            out.append((c, p, live))
    out.sort(key=lambda x: float(x[0].get("ts") or 0), reverse=True)
    return out


def owner_signs_in(text: str, person: dict, now: float | None = None) -> str:
    """The reply line when the owner's words were turned into the sign-in hand-over; '' when this is not that ask
    or there is no sign-in to hand over. Never raises."""
    if not asks(text):
        return ""
    try:
        import cards
        import report
        import tickets
    except Exception:  # noqa: BLE001
        return ""
    now = now or time.time()
    try:
        found = _login_cards(cards, report.SELF, now)
        if not found:
            return ""
        answers = cards._answers_index() or {}
        by = f"{(person or {}).get('key') or 'owner'}:chat (asked to sign in)"
        card, p, live = found[0]
        qid = str(card.get("id") or "")
        a = answers.get(qid) or {}
        host = str(p.get("host") or "the site")
        if live and not (a.get("option") and not a.get("undone")):
            r = cards.answer(qid, report.SELF, by=by)
            if not r.get("ok"):
                return ""
        else:
            job = tickets._load().get(str(p.get("ticket") or "")) or {}
            if not job or job.get("status") not in ("staged", "done", "claimed"):
                return ""
            line = report.login(str(p.get("ticket")), host, str(p.get("url") or ""), "you asked to sign in yourself")
            if not line.startswith("LOGIN="):
                return ""
            r = cards.answer(line.split("=", 1)[1].strip(), report.SELF, by=by)
            if not r.get("ok"):
                return ""
        try:
            import doer
            doer.wake()
        except Exception:  # noqa: BLE001
            pass
        return (f"Opening the {host} sign-in page in my browser now, in front. Control PC shows it from your phone. "
                "A Carry on card comes here in a moment; tap it once you're in and I pick the job up from where it stopped.")
    except Exception:  # noqa: BLE001
        return ""


if __name__ == "__main__":
    print("yes" if asks(" ".join(sys.argv[1:])) else "no")

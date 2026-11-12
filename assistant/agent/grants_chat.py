"""grants_chat.py - an owner's standing yeses, listed and taken back in plain words, by code.

2026-11-09. A tap on "Always" (or "until …") writes a grant (scripts/policy.py); the policy then answers later cards
like it by itself. Until now the only ways to see or undo one were /undo with the change id or Mission Control.
Grok Bot lets a user say what the bot may do without asking; here the words work the other way too:

  list    "what do you do on your own", "what have I let you do without asking", "what standing yeses do I have",
          "list my grants", "/grants"
  revoke  "stop doing X on your own", "take back the yes on X", "ask me again about X", "don't X without asking
          any more", "revoke X", "take them all back"

`handle(text, person)` -> the reply line, or '' when the message is neither (the brain takes it). Owners only;
`find(words)` is also what the `revoke` deed (gmcp/intents/actions) uses when the model reads an indirect ask.
Org-free: headers and names come from the grants themselves. Nothing here grants anything: a yes is a tap.
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
for _p in (HERE, HERE.parent / "scripts"):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))
import core  # noqa: E402

_CURLY = str.maketrans({"’": "'", "‘": "'", "“": '"', "”": '"'})
_LIST = re.compile(r"^(?:/grants\b|(?:ok(?:ay)?,? |so,? |hey,? )*(?:what|which|list|show(?: me)?|tell me)\b.{0,40}\b(?:"
                   r"standing (?:yes(?:es)?|grants?|permissions?|okays?)|grants?\b|(?:do|doing|done) on (?:your|its) own|without (?:asking|me|a tap)|"
                   r"let you do|allowed (?:you )?to do|can you do (?:by yourself|alone|on your own)|do by yourself)|"
                   r"(?:what|anything) (?:do|can) you (?:do|handle) (?:without (?:asking|me)|on your own|by yourself))", re.I)
_REVOKE = (
    re.compile(r"^(?:ok(?:ay)?,? |so,? |please,? |hey,? )*(?:stop|quit) (?:doing |handling |answering |approving )?(?P<w>.+?) (?:on your own|by yourself|without (?:asking|me|a tap)|automatically|yourself)\.?$", re.I),
    re.compile(r"^(?:ok(?:ay)?,? |so,? |please,? )*(?:take back|withdraw|revoke|cancel|undo|drop|remove|end) (?:the |my |that |your )?(?:standing )?(?:yes|okay|ok|grant|permission|approval)(?: on| for| about| to)? (?P<w>.+?)\.?$", re.I),
    re.compile(r"^(?:ok(?:ay)?,? |so,? |please,? )*(?:take back|withdraw|revoke) (?P<w>.+?)\.?$", re.I),
    re.compile(r"^(?:ok(?:ay)?,? |so,? |please,? )*ask (?:me )?(?:again|first|before|each time|every time)(?: about| for| on| before)? (?P<w>.+?)\.?$", re.I),
    re.compile(r"^(?:ok(?:ay)?,? |so,? |please,? )*(?:don'?t|do not|never) (?P<w>.+?) (?:without (?:asking|me|a tap)|on your own|by yourself|automatically)(?: any ?more| again)?\.?$", re.I),
    re.compile(r"^(?:ok(?:ay)?,? |so,? |please,? )*no more (?:standing )?(?:yes(?:es)?|grants?|automatic (?:yes(?:es)?|approvals?)|auto[- ]approv\w*)(?: on| for| about)? (?P<w>.+?)\.?$", re.I),
)
_ALL = re.compile(r"^(?:all|everything|them all|all of them|all of it|every one|the lot)$", re.I)
_FILLER = {"the", "a", "an", "my", "our", "your", "that", "this", "those", "these", "one", "ones", "card", "cards", "yes", "on",
           "for", "about", "to", "of", "in", "it", "them", "stuff", "things", "thing", "please", "again", "doing", "handling"}
MAX_WORDS = 20


def _norm(text: str) -> str:
    return " ".join(str(text or "").translate(_CURLY).split())


def _policy():
    import policy
    return policy


def is_list(text: str) -> bool:
    t = _norm(text)
    return bool(t) and len(t.split()) <= MAX_WORDS and not t.startswith("[") and bool(_LIST.match(t))


def revoke_words(text: str) -> str:
    """The words naming what to take back, '' when the line is not a revoke."""
    t = _norm(text)
    if not t or len(t.split()) > MAX_WORDS or t.startswith("["):
        return ""
    for rx in _REVOKE:
        m = rx.match(t)
        if m:
            w = m.group("w").strip(" .,!?'\"")
            return w or ""
    return ""


def _words(s: str) -> list:
    return [w for w in re.sub(r"[^\w\s'-]", " ", str(s or "").lower()).split() if w not in _FILLER and len(w) > 1]


def find(words: str, grants: list | None = None) -> list:
    """The active grants whose header (or kind, option or lane) carries every meaningful word; every grant for 'all'."""
    p = _policy()
    gs = list(grants if grants is not None else p.active_grants())
    if _ALL.match(_norm(words)):
        return gs
    want = _words(words)
    if not want:
        return []
    out = []
    for g in gs:
        hay = " ".join(str(g.get(k) or "") for k in ("header", "kind", "option", "lane")).lower().replace("_", " ")
        if all(w in hay for w in want):
            out.append(g)
    if not out:                                  # a looser pass: most of the words
        for g in gs:
            hay = " ".join(str(g.get(k) or "") for k in ("header", "kind", "option", "lane")).lower().replace("_", " ")
            n = sum(1 for w in want if w in hay)
            if n and n >= max(1, len(want) - 1) and len(want) > 1:
                out.append(g)
    return out


def _when(g: dict) -> str:
    if g.get("until"):
        return f"until {g['until'][:10]}"
    if g.get("scope") == "this_task":
        return "for that one task"
    return f"since {str(g.get('ts') or '')[:10]}" if g.get("ts") else "always"


def _opt(g: dict) -> str:
    return re.sub(r"\s*\((?:recommended)[^)]*\)", "", str(g.get("option") or ""), flags=re.I).strip()


def render(grants: list | None = None) -> str:
    p = _policy()
    gs = list(grants if grants is not None else p.active_grants())
    if not gs:
        return "Nothing stands right now: every card waits for your tap. Tap Always on a card when you want me to handle that kind myself."
    lines = [f"You've let me handle {len(gs)} thing{'s' if len(gs) != 1 else ''} on my own:"]
    for g in gs:
        lines.append(f"- {g.get('header')}: {_opt(g) or 'the recommended choice'} ({_when(g)})")
    lines.append("Say 'ask me again about …' and I'll take that one back.")
    return "\n".join(lines)


def revoke(words: str, person: dict) -> str:
    """Take back the grant(s) the words name. -> the reply line."""
    p = _policy()
    gs = p.active_grants()
    hits = find(words, gs)
    if not gs:
        return "Nothing stands right now, so there is nothing to take back: every card already waits for your tap."
    if not hits:
        return "Nothing standing matches that. " + render(gs)
    if len(hits) > 1 and not _ALL.match(_norm(words)):
        return "Which one? " + "; ".join(f"'{g.get('header')}'" for g in hits[:5]) + ". Name it and I'll take it back."
    by = str((person or {}).get("key") or "owner")
    done = [g for g in hits if p.revoke(g["id"], by=by, why="asked in chat")]
    if not done:
        return "I couldn't take that back just now; try once more."
    if len(done) == 1:
        return f"Done: I'll ask you each time again for '{done[0].get('header')}'."
    return f"Done: I'll ask you each time again for all {len(done)} of them."


def handle(text: str, person: dict) -> str:
    """The code reply to a list or revoke line from an owner, '' otherwise."""
    if not person or not core.is_owner(person):
        return ""
    try:
        if is_list(text):
            return render()
        w = revoke_words(text)
        if w:
            return revoke(w, person)
    except Exception:  # noqa: BLE001
        return ""
    return ""


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    a = " ".join(sys.argv[1:])
    print("list" if is_list(a) else f"revoke {revoke_words(a)!r}" if revoke_words(a) else "neither")

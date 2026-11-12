"""chatfirst.py - the chat-first rule, enforced at the doors (2026-11-04).

The owner's rule: the agent is 100% inside Google Chat, a portal into the PC. No line, card, dialog or prompt may
tell an owner to do something at the computer: open a terminal or a console, run a command, look at or sign in on the
PC's screen, open the Setup page or Mission Control, press something in Windows. What IS allowed: a Chat card button,
a Chat dialog (forms, the vault's Enter login), a line that says the agent is doing it itself, and "Control PC"
(Chrome Remote Desktop) as a window the owner may open, never as the step.

Sources are fixed where they are written (connections' setup words, the doer's cards, the prompts); this module is the
net under them, at the doors where words reach Chat:

  violations(text)  the rule names a text breaks ([] = clean); the guard test runs it over every owner-facing source
  clean(text)       the text without its PC-pointing sentences; when one was dropped, HANDLED says the agent has it
                    (voice.gate for every code line, chat_push.card_body for a card's ask / details / advice,
                    doer.pc_only for a job's result, the bridge for the model's own reply)

Zero LLM, pure functions, org-free.
  python assistant/agent/chatfirst.py "<text>"      # the violations and the cleaned text
"""
from __future__ import annotations

import re
import sys

HANDLED = "That part is mine to sort out from here; nothing for you to do at a computer."
# what may be said: the window (never the step), and the agent's own actions
_ALLOWED = re.compile(r"Control PC|Watch my PC|Also on <a [^>]*>Mission Control</a>|Also on Mission Control", re.I)
_RULES = (
    # "a terminal" the program, never "Terminal 4" the airport gate
    ("terminal", re.compile(r"\b(?:in|into|open|from|use|a|the|your|new|fresh) terminal\b(?! \d)|\bterminal (?:window|session)\b"
                            r"|\b(?:console window|a console|command prompt|command line|powershell|cmd\.exe)\b", re.I)),
    ("command", re.compile(r"\b(?:run|type|paste|enter)\b[^.!?\n]{0,30}\bcommands?\b|\bpython\s+\S+\.py\b|\bschtasks\b|\bclaude (?:login|setup-token|-p)\b"
                           r"|\b\w+\.py --\w+|`[^`\n]*(?:python |\.py|--\w)[^`\n]*`|\bin a (?:Claude(?: Code)? )?session\b", re.I)),
    ("at_the_pc", re.compile(r"\b(?:on|at|from|to|in front of) (?:the|your|this|that) (?:PC|computer|desktop|machine)\b(?! screen)"
                             r"|\b(?:needs|need|use|open|go to|sit at|walk to|restart|reboot) (?:the|your) (?:PC|computer)\b", re.I)),
    ("pc_screen", re.compile(r"\bPC screen\b|\bon (?:your|the) (?:PC's )?(?:screen|main monitor|monitor)\b|\b(?:is|are|comes?|came|put|bring|brings|brought) (?:\w+ ){0,3}on screen\b"
                             r"|\bonto (?:your|the) (?:screen|main monitor|monitor)\b", re.I)),
    ("pc_window", re.compile(r"\b(?:sign-?in |console )window\b|\bwindow (?:that )?(?:opens|is opening|will be up|is up|opened|is open|stays up)\b", re.I)),
    ("setup_page", re.compile(r"\bSetup page\b|\bMission Control\s*(?:>|/)\s*\w+|\bopen Setup\b|\bSetup on Mission Control\b|(?<![\w.])/setup\b", re.I)),
    ("mission_control", re.compile(r"\b(?:on|from|in|to|open|at|use) Mission Control\b", re.I)),
    ("by_hand", re.compile(r"\bsign(?:s|ed)? in by hand\b|\bby hand (?:at|on|in) the\b|(?<!\w)--show\b|\bsign in (?:there|on it) yourself\b", re.I)),
    ("windows", re.compile(r"\bSettings, Time\b|\bTask Scheduler\b|\bDevice Manager\b|\bControl Panel\b|\bCredential Manager\b(?= (?:and|then|to) (?:add|type|enter))", re.I)),
    ("config", re.compile(r"\b(?:raise|set|change|edit) (?:the )?[a-z_]+\.[a-z_]+(?:\.[a-z_]+)?\b|\bconfig\.json\b", re.I)),
)
_SPLIT = re.compile(r"(?<=[.!?])\s+|\n+")


# "a line that says the agent is doing it itself" is allowed (the rule above): a sentence whose subject is the agent
# or its browser, and that asks nothing of the owner, may say where the browser is. 2026-11-04: "I've put the sign-in
# page up in front on the PC screen" was cut from a reply to an owner who had asked to sign in himself, and "nothing
# for you to do at a computer" was put in its place, three times.
_OWN_DOING = re.compile(r"^\W*(?:I(?:'ve|'m|'ll| have| am| will)?|My (?:browser|window)|The (?:browser|sign-in page|page|job)|It(?:'s| is| stays| won't| will not)?)\b", re.I)
_ASKS_OWNER = re.compile(r"\byou(?:'ll| will| can| could| should| must| need| have to)?\b(?! (?:asked|said|told|brought|signed|tapped))|\bplease\b|\byour (?:PC|computer|desktop|monitor|screen)\b", re.I)
_SCREEN_RULES = {"pc_screen", "pc_window"}
_STILL_ASKS = re.compile(r"^(?:Then |Now |Please )?(?:Sign|Log) in\b|^(?:Then |Now |Please )?(?:Tap|Tell me|Reply|Choose|Pick)\b", re.I)


def _masked(text: str) -> str:
    return _ALLOWED.sub(lambda m: " " * len(m.group(0)), str(text or ""))


def _sentence_violations(sentence: str) -> list:
    t = _masked(sentence)
    hit = [name for name, rx in _RULES if rx.search(t)]
    if hit and set(hit) <= _SCREEN_RULES and _OWN_DOING.match(t.strip()) and not _ASKS_OWNER.search(t):
        return []                          # the agent saying what it did with its own browser: not a step for the owner
    return hit


def violations(text: str) -> list:
    """The names of the rules this text breaks, in rule order; [] when it is chat-first."""
    hit = set()
    for p in _SPLIT.split(str(text or "").strip()):
        if p and p.strip():
            hit.update(_sentence_violations(p))
    hit.update(n for n in _sentence_violations(str(text or "")) if n not in _SCREEN_RULES)   # a rule that spans a line break
    return [name for name, _ in _RULES if name in hit]


def clean(text: str, handled: str = HANDLED) -> str:
    """The text without any sentence that points the owner at the computer. A lead-in ("...from your phone:") goes with
    the step it introduced. When something was dropped, a caller's own `handled` is appended once ('' to append
    nothing); the stock HANDLED line is used only when nothing else is left to say. Text that breaks no rule comes
    back unchanged."""
    s = str(text or "")
    if not violations(s):
        return s
    parts = [p for p in _SPLIT.split(s.strip()) if p and p.strip()]
    bad = [bool(violations(p)) for p in parts] + [False]
    kept = [p for i, p in enumerate(parts) if not bad[i] and not (p.rstrip().endswith(":") and bad[i + 1])]
    out = " ".join(x.strip() for x in kept).strip()
    if handled == HANDLED and any(_STILL_ASKS.match(x.strip()) for x in kept):
        return out                         # what is left still gives the owner a step: "nothing for you to do" would be false
    if handled == HANDLED and out:
        # the stock line only stands in for a message that would otherwise be EMPTY. Tacked onto every trimmed
        # progress line it was noise (2026-11-10: seven times in one day, once after "a team member's request link is done.")
        return out
    if handled and handled not in out:
        out = (out + " " + handled).strip()
    return out


def changed(text: str) -> bool:
    return bool(violations(text))


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    t = " ".join(sys.argv[1:])
    print("violations:", violations(t))
    print("clean:", clean(t))

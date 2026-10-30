"""claims.py - a reply may only say work is in motion when the SUGGEST line really puts it in motion.

the owner 2026-10-08: "you said you are changing the text to say that, but you didn't give me a new
approval card, so nothing will actually happen." The model writes the words and the code does
the work, so the two can drift: "I'm sending the builder a request" with no build note, "the
operator will draft that" with no task, "I'm putting it on your calendar" with no event.

This is the deterministic check between them. Each claim shape maps to the SUGGEST fields that
would honour it. missing() returns the claims nothing backs; the bridge then asks the model ONCE
to either add the SUGGEST line or take the words back, and if it still will not, the bridge adds
one honest line under the reply. The claim regexes are deliberately narrow: a false positive costs
a second model turn, a false negative costs the owners' trust.
"""
from __future__ import annotations

import re

# claim name -> (regex over the reply text, the SUGGEST fields that satisfy it)
CLAIMS = {
    "build": (re.compile(
        r"(?:sending|send|putting in|put in|filing|file|making|make|writing|write)\b[^.!?\n]{0,40}\bbuild(?:er)? request|"
        r"\b(?:have|having|ask|asking|tell|telling|send|sending)\b[^.!?\n]{0,20}\bthe builder\b|"
        r"\bbeing built\b|\bI'?ll (?:get|have) (?:it|that|this) built\b|\bthe builder (?:will|is going to)\b", re.I),
        ("note",)),
    "draft": (re.compile(
        r"\b(?:having|have|asking|ask|telling|tell|getting|get)\b[^.!?\n]{0,20}\bthe operator\b|"
        r"\bI'?m (?:having|getting)\b[^.!?\n]{0,30}\bdraft(?:ed)?\b|\bI'?(?:ll|m) draft(?:ing)?\b|"
        r"\b(?:come|comes|coming|will come) back (?:here |to you )?as a (?:fresh |new )?card\b|"
        r"\b(?:a |the |that )?(?:draft|card) (?:will|should|is going to) (?:come|show up|land|be (?:here|back))\b", re.I),
        ("task", "do", "card")),
    "do": (re.compile(
        r"\b(?:sending|send|handing|hand|giving|give)\b[^.!?\n]{0,30}\b(?:to )?the doer\b|\bthe doer (?:will|is|'s|is going to)\b|"
        r"\b(?:goes|go|going|it'?s off|that'?s off) to the doer\b|\b(?:once|when|after) the doer (?:has|is|does|finishes)\b|"
        r"\bI'?ll (?:tell|let) you (?:once|when|after) (?:it'?s|the doer|that'?s) (?:done|been done|finished|changed)\b|"
        r"\bI'?m on it\b|\bI'?ll (?:go )?(?:log in|sign in|check|update|change|cancel|renew|fill|look)\b[^.!?\n]{0,50}"
        r"\b(?:site|website|portal|account|online|page|app|lensrentals|dmv)\b", re.I),
        ("do",)),
    "call": (re.compile(
        r"\b(?:set(?:ting)? up|arrange|arranging|propose|proposing|offer|offering|send(?:ing)?|put(?:ting)?)\b[^.!?\n]{0,30}"
        r"\b(?:a few |some |a couple of |2-3 |three |two )?(?:times?|slots?|options?)\b[^.!?\n]{0,30}\b(?:call|meeting|chat)\b|"
        r"\bset(?:ting)? up (?:a|the) (?:call|meeting)\b|\bI'?ll (?:find|pick|propose|offer) (?:a few |some )?times\b", re.I),
        ("call", "task")),
    "calendar": (re.compile(
        r"\b(?:putting|put|adding|add|I'?ll (?:put|add))\b[^.!?\n]{0,30}\bon (?:your|the|his|her|both|owner1'?s|owner6'?s) calendars?\b|"
        r"\b(?:adding|add|I'?ll add)\b[^.!?\n]{0,30}\bto (?:your|the|his|her) calendar\b", re.I),
        ("event", "task")),
    "schedule": (re.compile(
        r"\bI'?ll remind you\b|\bI'?ll (?:set|put) (?:up )?a reminder\b|\bremind(?:er|ing)? (?:you )?(?:every|each|at|on|tomorrow|tonight)\b", re.I),
        ("schedule", "followups")),
    "remember": (re.compile(
        r"\bI'?ll remember\b|\bI won'?t forget\b|\bfrom now on I'?(?:ll|m)\b|\bI'?ll keep that in mind\b|\bI'?ll (?:hold|stick) to that\b", re.I),
        ("remember", "learn", "note", "skill", "addressed")),
    "note": (re.compile(
        r"\b(?:writing|write|putting|put|adding|add|making|make)\b[^.!?\n]{0,30}\b(?:wiki|a note in the wiki|wiki note)\b|"
        r"\bwrit(?:e|ing) (?:it|that|this) (?:up|down) as a note\b", re.I),
        ("note", "skill")),
}
_NEGATED = re.compile(r"\b(?:can'?t|cannot|couldn'?t|won'?t|not going to|unable to|didn'?t|never|no longer|instead of)\b[^.!?\n]{0,25}$", re.I)


def _sentences(text: str) -> list:
    return [s.strip() for s in re.split(r"(?<=[.!?])\s+|\n+", str(text or "")) if s.strip()]


def missing(reply: str, sug: dict) -> list:
    """Claims in `reply` that `sug` does not honour -> [{"claim", "sentence", "needs"}]."""
    sug = sug if isinstance(sug, dict) else {}
    have = {k for k, v in sug.items() if v}
    out = []
    for sent in _sentences(reply):
        if sent.startswith("SUGGEST"):
            continue
        for name, (rx, fields) in CLAIMS.items():
            m = rx.search(sent)
            if not m:
                continue
            if _NEGATED.search(sent[:m.start()]):
                continue                                   # "I can't send the builder..." is not a claim
            if sent.lower().startswith(("if ", "when ", "once ", "unless ")) and name in ("draft", "do", "build"):
                continue                                   # conditional: "if it's still missing I'll have the operator..."
            if not (have & set(fields)):
                out.append({"claim": name, "sentence": sent[:200], "needs": fields})
            break
    return out


# ------------------------------------------------------------------ the owner asked for a message
# the owner 2026-10-17 evening: "sometimes when I tell Sage to do something, like send a text, Sage will just
# send me a paragraph about 'here's how I would write the text', and there's no approval card to follow.
# Anytime I ask Sage to do something that requires approval, there should be a decision card. Every
# single time." The owner's ASK is matched here (an imperative to text / email / reply / draft / tell
# someone); a reply to it must carry a SUGGEST field that makes a card (`task`, `do`, `card`, `call`).
# A short clarifying question with no wording in it is the one thing allowed instead.
_LEAD = r"(?:^|[.!?;:]\s+|\b(?:please|can you|could you|would you|let'?s|we need to|you need to|go ahead and|just|so|then|and|also)\s+)"
_VERB = (r"(?:text|txt|message|email|e-mail|reply|respond|write back|get back to|draft|re-?draft|redo|rewrite|send(?! (?:me|us)\b)|tell(?! (?:me|us)\b)|answer(?! (?:me|us)\b)|"
         r"follow up with|let (?:him|her|them|\w+) know|make sure (?:\w+ ){0,3}(?:replies|reply|text|texts|email|emails|respond|responds|hear|hears|know|knows))")
ASK_FOR_MESSAGE = re.compile(_LEAD + _VERB + r"\b", re.I)
_NOT_AN_ASK = re.compile(r"^\s*(?:did|do|does|have|has|is|are|was|were|what|when|who|why|how|which|any)\b", re.I)
CARD_FIELDS = ("task", "do", "card", "call")
_QUOTED = re.compile(r"[\"“']([^\"”']{30,})[\"”']")
def _wedding_re():
    try:
        import sys
        from pathlib import Path
        sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
        import crm as _crm_m
        crm_word = re.escape(_crm_m.name().lower())
    except Exception:  # noqa: BLE001
        crm_word = ""
    pat = r"\b(wedding|lead|couple|bride|groom|client|venue|film|shoot|inquir|retainer|proposal|contract|deliver"
    if crm_word and crm_word not in ("the crm", ""):
        pat += "|" + crm_word
    pat += ")"
    return re.compile(pat, re.I)


_WEDDING = _wedding_re()


def asked_for_message(text: str) -> bool:
    """True when the owner's line tells the agent to get a message out (a text, an email, a reply,
    a draft) rather than asking about one."""
    t = str(text or "").strip()
    if not t or t.startswith("[") or _NOT_AN_ASK.match(t):
        return False
    return bool(ASK_FOR_MESSAGE.search(t))


def card_bound(sug: dict) -> bool:
    """The SUGGEST line puts a card in motion."""
    sug = sug if isinstance(sug, dict) else {}
    return any(sug.get(k) for k in CARD_FIELDS)


def is_clarifying(reply: str) -> bool:
    """One short question back, with no wording in it: the one acceptable answer without a card
    ("Who should hear it, Justin or the group chat?")."""
    r = str(reply or "").strip()
    if not r or len(r) > 260 or not r.rstrip("`").rstrip().endswith("?"):
        return False
    if _QUOTED.search(r):
        return False
    return r.count("?") <= 2 and "\n\n" not in r


def lane_guess(text: str) -> str:
    return "wedding" if _WEDDING.search(str(text or "")) else "personal"


def card_retry_note(text: str) -> str:
    return ("[CHECK BEFORE THIS GOES OUT. The owner asked for a message to go out (\"" + str(text or "")[:160].replace("\n", " ") +
            "\"), and your reply carries no `task`, `do`, `card` or `call` in its SUGGEST line, so no card will appear and "
            "nothing will happen. Words in chat are not a draft. Reply with the complete message again WITH the SUGGEST line "
            "that hands the draft on (a `task` for the operator, with the facts you have). If exactly one fact stops you, ask "
            "only that, in one line, with no wording of the message.]")


def forced_task_text(person_name: str, text: str, reply: str) -> str:
    """The operator ticket code writes when the model still would not: the owner's words, plus the
    model's wording as a hint the operator must verify."""
    words = re.sub(r"\s+", " ", str(reply or "")).strip()
    words = re.sub(r"\s*SUGGEST\s*\{.*$", "", words).strip()
    return (f"{person_name} asked in chat: {str(text or '').strip()[:600]}\n\n"
            + (f"Sage's suggested wording (check every fact before drafting; it is a hint, not a source): {words[:700]}" if words else ""))


def retry_note(gaps: list) -> str:
    """The one-time correction sent back to the model, in the same session."""
    parts = []
    for g in gaps[:3]:
        parts.append(f"- \"{g['sentence'][:140]}\" (that needs {' or '.join('`' + f + '`' for f in g['needs'])})")
    return ("[CHECK BEFORE THIS GOES OUT. Your reply says work is in motion, but your SUGGEST line puts forward nothing that "
            "does it:\n" + "\n".join(parts) + "\nNothing happens by words alone. Reply with the complete message again, either "
            "with a SUGGEST line that really does each thing, or reworded so you do not say it is happening. If you cannot "
            "do it, say so plainly and say what would get it done.]")


def honest_line(gaps: list) -> str:
    """When the model still will not back its words: one short line the owners can trust."""
    kinds = {g["claim"] for g in gaps}
    what = {"build": "the build request", "draft": "that draft", "do": "that job", "calendar": "that calendar event",
            "schedule": "that reminder", "remember": "that", "note": "that note"}
    names = [what[k] for k in ("build", "draft", "do", "calendar", "schedule", "note", "remember") if k in kinds]
    thing = " and ".join(names[:2]) if names else "that"
    return f"(Correction: {thing} is not actually in motion; nothing was queued for it. Tell me again and I'll do it properly.)"


# A lead forwarded from a wedding marketplace (the owner, 2026-10-27): Paige and Casey's Zola text (a zola.com/z link)
# went back to him at 12:52 asking for the date and venue; the doer read the page in three minutes once he asked
# again at 2:35. The link itself is the job: code sends it to the doer when the turn did not.
MARKETPLACE_LEAD = re.compile(r"https?://(?:www\.)?(?:zola\.com/z/|theknot\.com/|weddingwire\.com/)\S+", re.I)


def marketplace_lead(text: str) -> str:
    """The first marketplace lead link in an owner's message, or ''."""
    m = MARKETPLACE_LEAD.search(str(text or ""))
    return m.group(0).rstrip(".,;:)!?\"'") if m else ""


def lead_intake_text(person_name: str, text: str, url: str) -> str:
    """The doer job for a forwarded marketplace lead: read the page, check the date, and stop at the paid
    step (Zola's Connect spends credits) so that decision is a checkpoint card in this thread."""
    return (f"{person_name} forwarded a new wedding lead: {str(text or '').strip()[:600]}\n\n"
            f"Open the lead page {url} in the signed-in browser and read it. Report the couple's names, the wedding date, "
            "the venue and city, the guest count and budget if shown, what coverage they asked for, their message, and their "
            f"phone and email (or say they are hidden). Then check that date in the CRM's calendar and say whether it is open "
            "or what is booked on it. If the contact details are hidden behind a paid step (Zola's Connect spends credits), do "
            "not take it: stop there as a checkpoint so the owner decides on the card. Never reply to the couple.")

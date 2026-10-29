"""ack.py - a message that is ONLY an acknowledgment never reaches the brain.

Ported from the property OS's chat agent (scripts/lib/chat_agent/ack.py, the owner 2026-10-08
"read how it does it and learn from it"). Two uses here:

  * bridge.py: "ok thanks" from an owner in Chat gets no typing placeholder and no model turn;
    it is recorded and consumed silently (before this, the room saw Sage start a reply and drop it).
  * comms_probe.py: a text to the iPhone that is only an ack or a tapback ("Thank youuuu",
    "Liked an image", a thumbs-up) is dismissed by code, zero-LLM, so the drafter is never
    woken for it and never has to judge it.

Bare "ok", "yes", "sure", "k" are NOT acks on their own: they can be the answer to a question
the agent asked. They count only next to a real ack word ("ok thanks", "ok copy").
"""
from __future__ import annotations

import re

_ACK_PHRASES = (
    "copy that", "copy", "copied", "roger that", "roger", "10-4", "10 4",
    "thanks", "thank you", "thank u", "thx", "ty", "tysm", "much appreciated", "appreciate it",
    "got it", "gotcha", "noted", "will do", "on it", "sounds good", "sounds great",
    "perfect", "great", "cool", "nice", "awesome", "excellent", "good stuff", "all good",
    "love it", "love that", "haha", "lol", "lmao",
    "gracias", "muchas gracias", "entendido", "de nada",
)
_SOFT = ("ok", "okay", "k", "kk", "sure", "yes", "yep", "yeah", "alright", "all right", "si", "sí", "no worries", "np")
_FILLER = ("you", "man", "bro", "boss", "guys", "all", "so", "much", "very", "again", "for that", "for the update",
           "dude", "brother", "sis", "mate")
# iMessage tapbacks arrive as text ("Liked “see you at 6”", "Emphasized an image")
_TAPBACK = re.compile(r"^\s*(?:liked|loved|laughed at|emphasized|disliked|questioned)\s+(?:an image|a video|a photo|“|\")", re.I)


def is_tapback(text: str) -> bool:
    return bool(_TAPBACK.match(str(text or "")))


def is_pure_ack(text: str, names: tuple | list = ()) -> bool:
    """True when the whole message is an acknowledgment and/or emoji.
    `names` = first names to ignore ("thanks Sage", "got it the owner")."""
    t = str(text or "").strip()
    if not t:
        return False
    if is_tapback(t):
        return True
    low = t.lower()
    for n in sorted((str(x) for x in names if x), key=len, reverse=True):   # full names first
        low = low.replace("@" + n.lower(), " ")
    low = re.sub(r"@\S+", " ", low)
    for n in names:
        if n:
            low = re.sub(r"\b" + re.escape(str(n).lower()) + r"\b", " ", low)
    low = re.sub(r"(\w)\1{2,}", r"\1", low)           # "thank youuuu" -> "thank you"
    low = re.sub(r"[^\w\s'-]", " ", low)             # emoji / punctuation -> space
    low = re.sub(r"\s+", " ", low).strip()
    if not low:
        return True                                   # emoji / punctuation only
    if len(low) > 60 or re.search(r"\d{2,}", low.replace("10-4", "").replace("10 4", "")):
        return False                                  # carries a number, a time, an amount
    rest, hard = " " + low + " ", 0
    for p in sorted(_ACK_PHRASES, key=len, reverse=True):
        rest, n = re.subn(r"(?<=\s)" + re.escape(p) + r"(?=\s)", " ", rest)
        hard += n
    for p in sorted(_SOFT + _FILLER, key=len, reverse=True):
        rest = re.sub(r"(?<=\s)" + re.escape(p) + r"(?=\s)", " ", rest)
    return hard > 0 and not rest.strip()

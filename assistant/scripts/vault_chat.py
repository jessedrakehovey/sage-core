"""vault_chat.py - a login reaches the vault from chat without ever being a chat message (sage-core, 2026-10-28).

the owner: "sage-core should be able to collect phone contacts and passwords securely." A password typed into Google
Chat sits in Google's message store, in the transcript and in the model's context; no scrub undoes the first two.
So the rule stays: no password in chat. What chat carries is the BUTTON. An owner who says "save my Stripe login"
(or who pastes a password anyway) gets one card with "Enter login"; the tap opens a Chat DIALOG (chat_push.vault_dialog)
whose fields go to the app, not into the conversation; the submit (gchat_events: vault_add_submit) hands them to
`doer/vault.py add-stdin` in the keyring interpreter, and the dialog closes with "saved, nothing kept in chat".
A pasted password is refused: scrubbed from every agent record by code, before any model turn, and the card posted.

Zero LLM. Pure functions; bridge.py calls them before the brain sees the message.
  python assistant/scripts/vault_chat.py detect "<text>"     # what a message would trigger (nothing stored)
"""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
from alib import DATA  # noqa: E402

AGENT_DATA = DATA / "agent"
SCRUBBED = "[a password, removed by code before it was read]"
# an owner pasting a credential: "password: hunter2", "pw is hunter2", "pass=hunter2", "login: me@x.com / hunter2"
_PW = re.compile(r"\b(?:pass(?:word|wd|code)?|pwd?)\b(?:\s+(?:for|to|on|at)\s+\S+)?\s*(?:\bis\b|=|:|-)\s*[\"'`]?([^\s\"'`,;]{4,})", re.I)
_SITE_WORD = re.compile(r"\b([\w.\-]+)\s+(?:login|password|credentials?|creds|account)\b", re.I)
_NOT_WORD = {"my", "the", "a", "an", "new", "this", "that", "their", "your", "our", "his", "her", "its", "another", "same", "old"}
_PAIR = re.compile(r"\b(?:login|creds?|credentials?)\b\s*(?:is|are|=|:)?\s*([\w.+\-]+@[\w.\-]+\.\w+|[\w.\-]{3,})\s*(?:/|,|\band\b|\s)\s*(?:pass(?:word)?\s*[:=]?\s*)?([^\s\"'`,;]{6,})", re.I)
# an owner asking to store one: "save my stripe login", "add a password for quickbooks", "here's the login for X"
_ASK = re.compile(r"\b(?:save|add|store|remember|keep|set\s*up|here(?:'s| is)|give you|enter)\b.{0,40}\b(?:login|password|passwords|credentials?|creds)\b(?:\s+(?:for|to|on)\s+([\w.\-]+(?:\.[a-z]{2,})?))?", re.I)
_SITE = re.compile(r"\b(?:for|to|on|at)\s+((?:https?://)?[\w\-]+(?:\.[\w\-]+)+(?:/\S*)?|[A-Z][\w\-]{2,})", re.I)
_NOT_SITE = {"me", "you", "it", "this", "that", "the", "my", "our", "them", "us", "chat", "now", "later", "today"}


def site_guess(text: str) -> str:
    m = _SITE.search(text or "")
    if m and m.group(1).lower() not in _NOT_SITE:
        return m.group(1).strip().rstrip(".,")
    return ""


def detect_secret(text: str) -> tuple:
    """(the secret as typed, the site guess) when a message carries a password; ("", "") otherwise.
    A one-time setup token (connect.py) is not a password: those are long base64 blobs with no password word."""
    t = str(text or "")
    if len(t) > 4000:
        return "", ""
    m = _PAIR.search(t)
    if m:
        return m.group(2), site_guess(t)
    m = _PW.search(t)
    if m:
        v = m.group(1)
        if v.lower() in ("reset", "manager", "field", "page", "form", "protected", "for", "and", "the", "is", "was", "to", "it", "this", "that"):
            return "", ""
        return v, site_guess(t)
    return "", ""


def asks_login(text: str) -> str:
    """The site an owner wants to hand a login for ("?" when they did not name one), "" when not an ask."""
    t = str(text or "")
    if detect_secret(t)[0]:
        return ""
    m = _ASK.search(t)
    if not m:
        return ""
    site = m.group(1) or site_guess(t)
    if not site:
        w = _SITE_WORD.search(t)
        if w and w.group(1).lower() not in _NOT_WORD:
            site = w.group(1)
    return (site or "?").rstrip(".,")


def card(site: str = "", agent: str = "the agent") -> dict:
    """The one message chat carries: a button that opens the vault dialog. No field here, nothing to type here."""
    where = f" for {site}" if site and site != "?" else ""
    return {"text": f"A login{where}: tap Enter login below.",
            "cardsV2": [{"cardId": "vault-add", "card": {
                "header": {"title": f"Login{where}", "subtitle": "goes straight to the vault, never into this chat"},
                "sections": [{"widgets": [
                    {"textParagraph": {"text": f"The form opens on top of Chat. What you type there goes to {agent}'s vault on its computer and "
                                               "is not posted here. Have a code-generator secret? There is a field for it."}},
                    {"buttonList": {"buttons": [{"text": "Enter login",
                                                 "onClick": {"action": {"function": "vault_add", "interaction": "OPEN_DIALOG",
                                                                        "parameters": [{"key": "site", "value": (site if site != "?" else "")[:200]}]}}}]}}]}]}}]}


def refusal_line(site: str = "", scrubbed: int = 0) -> str:
    where = f" for {site}" if site and site != "?" else ""
    return (f"I never keep a password from chat, so I removed it from my records{' (' + str(scrubbed) + ' file' + ('s' if scrubbed != 1 else '') + ')' if scrubbed else ''}. "
            f"Delete your message here too. Tap Enter login{where} and type it there: that goes straight to my vault.")


def offer_line(site: str = "") -> str:
    where = f" for {site}" if site and site != "?" else ""
    return f"Tap Enter login{where}. The form goes straight to my vault; nothing you type there lands in this chat."


def scrub(value: str) -> int:
    """Remove a pasted password from every agent record it could sit in (transcript, inboxes, held messages).
    Returns files touched. Shorter than 4 characters is left alone (it would match everywhere)."""
    v = str(value or "")
    if len(v) < 4:
        return 0
    n = 0
    for p in list(AGENT_DATA.glob("*.jsonl")) + list(AGENT_DATA.glob("inbox*")) + list(AGENT_DATA.glob("gchat_inbox*")) + list(AGENT_DATA.glob("burst_pending*")):
        try:
            t = p.read_text(encoding="utf-8")
        except OSError:
            continue
        enc = json.dumps(v)[1:-1]
        hit = False
        for form in {v, enc}:
            if form in t:
                t = t.replace(form, SCRUBBED)
                hit = True
        if hit:
            p.write_text(t, encoding="utf-8")
            n += 1
    return n


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    if len(sys.argv) >= 3 and sys.argv[1] == "detect":
        s, site = detect_secret(sys.argv[2])
        print(json.dumps({"secret_seen": bool(s), "site": site, "asks_login": asks_login(sys.argv[2])}))
    else:
        print(__doc__)

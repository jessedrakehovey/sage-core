"""text_gate.py - texts to the owner's iPhone are triaged only when they are wedding work (2026-10-08).

the owner 2026-10-08: "leave text triage for wedding clients, not normal conversations" (the
camping group, owner and friends were filling Chat with reply cards). A text that reaches
the `the owner-iphone` line becomes a ticket (and so a card, an SLA and a chase) only when its
number belongs to a wedding party:

  - a quiz lead          assistant/data/leads/*.json
  - a booked couple      scripts/projects/project_state.json (phones)
  - a couple / vendor    any phone number written in Wiki/Couples/ or Wiki/Vendors/
  - a CRM contact        the desk snapshot's contacts (contacts.crm_people)

Everything else is still written to the thread log by phone_bridge.py, so Sage and the
operator can draft or triage any thread when an owner asks in chat; it is just never
ticketed, carded, chased or turned into a sentinel on its own. A matched text is laned
`wedding`. owner's iPhone is not touched.

Family is never a wedding party, even when a source above lists them (2026-10-10: owner is a
CRM contact, so her texts were carded): the owners (assistant/agent/people.json, and any
CRM contact on an owner's email) and every phone in Wiki/People/Family and Friends.md are
taken out of the wedding set, and their texts are never ticketed on any phone or lane (2026-10-11).
Since 2026-10-11 the wedding-only rule covers owner's iPhone too and any lane a text arrives
in: an unmatched number is logged only, never carded, unless an owner asks in chat.

Switch: assistant/config.json -> phone.triage_all_texts = true turns the old behaviour
(every text gets a reply card) back on. Absent or false = wedding only.

The one exception (ruling 13, 2026-10-15, TRACKING_DESIGN.md): a personal text that is
TIME-RELATED (a date or clock time AND a plan word: "dinner Sunday at 5", "boat leaves 8am
Saturday") or MONEY-TIED (an amount, or pay / owe / venmo words) MAY become a ticket, and so a
draft card, marked `payload.exception = "time" | "money"` so the operator knows why it came
through. Everything else personal stays logged only. Switch and sub-switches:
config.json -> phone.personal_exceptions {enabled, time, money, include_family}; the words are
in assistant/mining.json -> personal_text_exception. The owners' own numbers never come through.
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from alib import DATA, VAULT, config, load_json  # noqa: E402

ACCOUNT = "owner1-iphone"
_PHONE = re.compile(r"\+?1?[\s(.-]*\d{3}[\s).-]*\d{3}[\s.-]*\d{4}")


def _key(v) -> str:
    d = re.sub(r"\D", "", str(v or ""))
    return d[-10:] if len(d) >= 10 else ""


def business_only() -> bool:
    try:
        return not bool((config().get("phone") or {}).get("triage_all_texts", False))
    except BaseException:  # noqa: BLE001  (config() sys.exits on a broken file: keep the safe default)
        return True


def exception_cfg() -> dict:
    """config.json -> phone.personal_exceptions, with safe defaults (off when absent)."""
    try:
        pe = (config().get("phone") or {}).get("personal_exceptions") or {}
    except BaseException:  # noqa: BLE001
        pe = {}
    if not isinstance(pe, dict):
        pe = {}
    return {"enabled": bool(pe.get("enabled", False)), "time": bool(pe.get("time", True)),
            "money": bool(pe.get("money", True)), "include_family": bool(pe.get("include_family", True))}


_VOCAB = {}


def _vocab() -> dict:
    """The exception's word lists (assistant/mining.json -> personal_text_exception), compiled once."""
    if _VOCAB:
        return _VOCAB
    v = (load_json(VAULT / "assistant" / "mining.json", {}) or {}).get("personal_text_exception") or {}
    for k in ("time_words", "plan_words", "money_words"):
        try:
            _VOCAB[k] = re.compile(str(v.get(k) or "(?!x)x"), re.I)
        except re.error:
            _VOCAB[k] = re.compile("(?!x)x")
    return _VOCAB


def personal_exception(text: str, pe: dict | None = None) -> str:
    """'time' when the text names a date or time AND a plan, 'money' when it carries an amount or
    a payment word, else ''. Pure; the switches decide whether the answer is used."""
    pe = pe or exception_cfg()
    t = str(text or "")
    if not t.strip():
        return ""
    v = _vocab()
    if pe.get("time", True) and v["time_words"].search(t) and v["plan_words"].search(t):
        return "time"
    if pe.get("money", True) and v["money_words"].search(t):
        return "money"
    return ""


def owner_numbers() -> set:
    """Last-10-digit keys of the owners themselves (assistant/agent/people.json): never ticketed."""
    out = set()
    try:
        people = load_json(VAULT / "assistant" / "agent" / "people.json", {}) or {}
        for u in (people.get("users") or {}).values():
            out.add(_key(u.get("phone")))
    except Exception:  # noqa: BLE001
        pass
    out.discard("")
    return out


def business_numbers() -> set:
    """Last-10-digit keys of every wedding party's phone. Each source is read on its own so a
    missing or broken one never hides the others."""
    out = set()
    try:
        for f in (DATA / "leads").glob("*.json"):
            out.add(_key(((load_json(f, {}) or {}).get("lead") or {}).get("phone")))
    except OSError:
        pass
    try:
        ps = load_json(VAULT / "scripts" / "projects" / "project_state.json", {}) or {}
        for p in (ps.get("projects") or ps).values() if isinstance(ps, dict) else []:
            if isinstance(p, dict):
                for ph in (p.get("phones") or []) + [p.get("phone")]:
                    out.add(_key(ph))
    except Exception:  # noqa: BLE001
        pass
    for folder in ("Couples", "Vendors"):
        try:
            for f in (VAULT / "Wiki" / folder).rglob("*.md"):
                out.update(_key(m) for m in _PHONE.findall(f.read_text(encoding="utf-8", errors="replace")))
        except OSError:
            pass
    try:
        import contacts
        for c in contacts.crm_people():
            out.update(_key(e.get("value")) for e in c.get("phones", []))
    except Exception:  # noqa: BLE001
        pass
    out -= household_numbers()
    out.discard("")
    return out


def household_numbers() -> set:
    """Last-10-digit keys of the owners and their family / friends: never wedding work."""
    out, emails = set(), set()
    try:
        people = load_json(VAULT / "assistant" / "agent" / "people.json", {}) or {}
        for u in (people.get("users") or {}).values():
            out.add(_key(u.get("phone")))
            emails.update(str(e).strip().lower() for e in u.get("emails") or [])
    except Exception:  # noqa: BLE001
        pass
    try:
        import contacts
        for c in contacts.crm_people():
            if any(str(e.get("value") or "").strip().lower() in emails for e in c.get("emails", [])):
                out.update(_key(e.get("value")) for e in c.get("phones", []))
    except Exception:  # noqa: BLE001
        pass
    try:
        f = VAULT / "Wiki" / "People" / "Family and Friends.md"
        out.update(_key(m) for m in _PHONE.findall(f.read_text(encoding="utf-8", errors="replace")))
    except OSError:
        pass
    try:   # a friend's or relative's own note (2026-10-15: Casey Juniper, a past CRM contact, still got cards)
        for f in (VAULT / "Wiki" / "People").glob("*.md"):
            if f.name == "Contacts.md":   # the whole phone book (2026-10-23), clients included: not a family list
                continue
            txt = f.read_text(encoding="utf-8", errors="replace")
            head = txt.split("\n---", 2)[0] if txt.startswith("---") else ""
            tags = re.search(r"^tags:\s*\[([^\]]*)\]", head, re.M)
            if tags and {"family", "friends"} & {t.strip().lower() for t in tags.group(1).split(",")}:
                out.update(_key(m) for m in _PHONE.findall(txt))
    except OSError:
        pass
    out.discard("")
    return out


def team_member(handle) -> str:
    """Our own shooter / editor / admin (org.json `people`, actions.team_member), '' for anyone else.
    Their texts are wedding work (2026-10-24: Marlo asked about his pay for Nov 7 and the card was
    filed personal, as a money exception). Never raises."""
    try:
        agent = str(Path(__file__).resolve().parent.parent / "agent")
        if agent not in sys.path:
            sys.path.insert(0, agent)
        import actions
        return actions.team_member(str(handle or ""))
    except Exception:  # noqa: BLE001
        return ""


def is_business(handle, numbers: set | None = None) -> bool:
    k = _key(handle)
    return bool(k) and k in (numbers if numbers is not None else business_numbers())


def applies(account) -> bool:
    """True when texts on this phone line are filtered (the owner's line, switch on)."""
    return str(account or "") == ACCOUNT and business_only()


def keep(items: list) -> list:
    """tickets.add_many's filter: drops every `imessage_in` item, on either phone and whatever lane
    it arrived in, whose number is not a wedding party (the owner 2026-10-11: "I only want you to draft
    for wedding clients stop handling personal text messages"). A text from an owner or a family /
    friends number is always dropped. Matches on the owner's line are relaned `wedding`; on owner's
    they keep her lane. A text from one of our own team (not an owner) always comes through, laned
    `wedding` on either phone. Every other item passes untouched."""
    texts = [it for it in items if it.get("kind") == "imessage_in"]
    if not texts or not business_only():
        return items
    home, nums, out = household_numbers(), None, []
    pe = exception_cfg()
    owners = owner_numbers()
    for it in items:
        if it in texts:
            p = it.get("payload") or {}
            k = _key(p.get("from"))
            if k and k not in owners and k not in home and team_member(p.get("from")):
                out.append({**it, "lane": "wedding"})
                continue
            if k in home and not (pe["enabled"] and pe["include_family"] and k not in owners):
                continue
            nums = business_numbers() if nums is None else nums
            if not is_business(p.get("from"), nums):
                # ruling 13: a time-related or money-tied personal text may still get a draft card
                exc = personal_exception(p.get("text"), pe) if pe["enabled"] and k not in owners else ""
                if not exc:
                    continue
                it = {**it, "payload": {**p, "exception": exc}}
            elif applies(p.get("account")):
                it = {**it, "lane": "wedding"}
        out.append(it)
    return out


PLAIN_ADVICE = "Edit & send changes the words first."
_WORD = re.compile(r"[a-z']{4,}")


def _own_text(q: dict) -> str:
    ex = q.get("exec") or {}
    return " ".join(str(x or "") for x in (q.get("question"), q.get("details"), q.get("header"),
                                           (ex.get("payload") or {}).get("body"))).lower()


def own_advice(card) -> bool:
    """A text card's hint must come from its own conversation (2026-10-17: AJ's card said "Edit & send
    if you want to answer the gift exchange or holidays now", copied from Marisa's card a second
    earlier). When the advice is word for word another open card's to a different number, and every
    word it shares with that card's conversation is missing from this one's, it is replaced with a
    plain hint, in the card and its files. True when it was replaced. Never raises."""
    try:
        q = ((card.get("questions") or [{}])[0] or {})
        ex = q.get("exec") or {}
        adv = str(q.get("advice") or "").strip()
        k = _key((ex.get("payload") or {}).get("to"))
        if ex.get("kind") != "imessage_send" or not adv or adv == PLAIN_ADVICE:
            return False
        import cards
        mine = set(_WORD.findall(_own_text(q)))
        for c in cards._live():
            if c.get("id") == card.get("id"):
                continue
            o = ((c.get("questions") or [{}])[0] or {})
            if str(o.get("advice") or "").strip() != adv or _key(((o.get("exec") or {}).get("payload") or {}).get("to")) == k:
                continue
            topic = set(_WORD.findall(adv.lower())) & set(_WORD.findall(_own_text(o)))
            if topic and not topic & mine:
                q["advice"] = PLAIN_ADVICE
                for p in (cards.LIVE / f"{card['id']}.json", cards.ARCHIVE / f"{card['id']}.json"):
                    f = cards._read(p)
                    if isinstance(f, dict) and f.get("questions"):
                        f["questions"][0]["advice"] = PLAIN_ADVICE
                        cards._write(p, f)
                return True
        return False
    except Exception:  # noqa: BLE001  never in the way of posting a card
        return False


def supersede_older(card) -> list:
    """A new text card retires any older open text card to the same number on the same phone, so one
    conversation never has two live drafts (2026-10-15: Casey Juniper got 07:15 and 08:50 cards, both open).
    Called from chat_push.push_card, which every cards.post goes through. Returns the retired ids.
    It first makes sure the card's hint is its own (own_advice)."""
    own_advice(card)
    try:
        ex = ((card.get("questions") or [{}])[0] or {}).get("exec") or {}
        p = ex.get("payload") or {}
        k = _key(p.get("to"))
        if ex.get("kind") != "imessage_send" or not k:
            return []
        import cards
        out = []
        for c in cards._live():
            if c.get("id") == card.get("id") or c.get("status") != "open" or float(c.get("ts") or 0) > float(card.get("ts") or 0):
                continue
            o = ((c.get("questions") or [{}])[0] or {}).get("exec") or {}
            op = o.get("payload") or {}
            if o.get("kind") == "imessage_send" and _key(op.get("to")) == k \
                    and str(op.get("account") or "") == str(p.get("account") or ""):
                if cards.supersede(c["id"], card["id"], by="text_gate"):
                    out.append(c["id"])
        return out
    except Exception:  # noqa: BLE001  never in the way of posting a card
        return []

"""whois.py - who is this number? One answer, with the evidence, from everything the house knows (2026-10-23).

the owner: "Sage has a difficult time sorting through and deducing who people are from inbound text
messages." Until now the only name lookup was the phone book (never imported) plus the CRM's
contacts, so 50 of 59 text threads had no name, and what Sage had already learned about a number
("the owner's dad texts from +1 555-063-5563") sat in memory prose that nothing read back. This module
is the one place that question is answered, for the phone bridge, the operator and Sage alike.

Sources, strongest first (each read on its own; a missing one never hides the others):
  owners            assistant/agent/people.json
  told in chat      assistant/data/people_aliases.json   ("that's my dad" -> identify; this module's learn())
  phone books       assistant/data/contacts.json         (contacts.py; iPhone exports)
  the wiki          Wiki/People/*.md (Family and Friends: table rows and headings), Wiki/Couples, Wiki/Vendors
  memory            assistant/agent/state/memory.json    (numbers next to a name or "the owner's dad")
  leads / tracker   assistant/data/leads/*.json, scripts/projects/project_state.json
  CRM               snapshot contacts (crm.snapshot, contacts.crm_people)
  the texts         assistant/data/phone_threads.json: the name the phone sent; how the owner addressed
                    them ("Hey Marisa", "I love you too Dad"); how they signed ("Hey, it's Dave",
                    "Mira Tsai: Hi the owner"); group texts (tapbacks quoting another thread's message);
                    automated senders (short codes, codes, promos, campaign texts)
  neighbours        a number one digit from a known person's (a family plan) is a hint, never a name

  python assistant/scripts/whois.py who <number|email|name|"my dad"|"the mechanic">
  python assistant/scripts/whois.py directory [--days 30]      # the PEOPLE WHO TEXT block Sage sees
  python assistant/scripts/whois.py harvest [--dry]            # write sure names into the thread log
  python assistant/scripts/whois.py learn <number> "<name>" [--relation ..] [--kind ..] [--by ..]
  python assistant/scripts/whois.py stats

Read-only except learn() (the aliases file + the thread's name) and harvest() (thread names).
Never sends, never calls a model.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from alib import DATA, VAULT, load_json, save_json  # noqa: E402
import crm

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:  # noqa: BLE001
    pass

# ---- paths (module attributes so tests can point them at scratch copies)
THREADS = DATA / "phone_threads.json"
ALIASES = DATA / "people_aliases.json"
LEADS = DATA / "leads"
PEOPLE = VAULT / "assistant" / "agent" / "people.json"
MEMORY = VAULT / "assistant" / "agent" / "state" / "memory.json"
WIKI = VAULT / "Wiki"
TRACKER = VAULT / "scripts" / "projects" / "project_state.json"

KINDS_ORDER = ("owner", "family", "friend", "client", "lead", "vendor", "contact", "business", "automated", "unknown")
SURE, LIKELY, MAYBE = 0.85, 0.6, 0.4

_PHONE = re.compile(r"\+?1?[\s(.-]*\d{3}[\s).-]*\d{3}[\s.-]*\d{4}\b")
_TAPBACK = re.compile(r"^\s*(liked|loved|laughed at|emphasized|disliked|questioned)\s+(?:an image|a video|a photo|an attachment|[“\"](.*)[”\"])\s*$",
                      re.I | re.S)
_AUTOMATED = re.compile(r"verification code|security code|login code|your code|2fa|two-factor|one-time|passcode|don'?t share"
                        r"|paid for by|text stop|reply stop|stop to (?:end|opt|cancel|unsub)|stop2end|unsub\b|unsubscribe|opt out"
                        r"|fb\.me/|bit\.ly/|\battn\.tv|hotplate\.com|cash\.app/|\d+% off|promo code|use code|order now|flash sale"
                        r"|loyalty|rewards|earned \d+ star|download (?:the )?\S* ?app|you have \d+ new notification|mentioned you in a comment"
                        r"|commented on|click here|limited time|final notice|payment failure", re.I)
_SPAM = re.compile(r"searching for mature men|fresh out of university|flexible gig work|hot singles|crypto rules|pro-crypto", re.I)
_RELATION_WORDS = ("dad", "father", "mom", "mother", "stepmom", "stepmother", "stepdad", "stepfather", "mama", "papa", "pops",
                   "grandma", "grandpa", "nana", "grandmother", "grandfather", "sister", "brother", "aunt", "uncle", "cousin",
                   "mother-in-law", "father-in-law", "sister-in-law", "brother-in-law", "wife", "husband", "son", "daughter",
                   "mechanic", "landlord", "dentist", "doctor", "boss", "editor", "pastor", "neighbor", "neighbour", "realtor",
                   "accountant", "lawyer", "plumber", "gardener", "babysitter", "nanny")
_FAMILY_WORDS = {"dad", "father", "mom", "mother", "stepmom", "stepmother", "stepdad", "stepfather", "mama", "papa", "pops",
                 "grandma", "grandpa", "nana", "grandmother", "grandfather", "sister", "brother", "aunt", "uncle", "cousin",
                 "mother-in-law", "father-in-law", "sister-in-law", "brother-in-law", "wife", "husband", "son", "daughter"}
_VOCATIVE_STOP = {"lord", "god", "jesus", "amen", "praying", "praise", "haha", "lol", "yes", "yeah", "yep", "nope", "okay", "love",
                  "thanks", "thank", "congrats", "welcome", "morning", "hello", "hey", "also", "and", "but", "the", "this", "that",
                  "please", "sorry", "wow", "aww", "oh", "ok", "mom", "dad", "mama", "everyone", "guys", "friend", "babe", "brother", "sister",
                  "well", "good", "happy", "hmm", "then", "sure", "awesome", "amazing", "great", "perfect", "nice", "right", "ugh", "omg", "yay",
                  "alright", "first", "second", "finally", "anyway", "honestly", "seriously", "absolutely", "totally", "definitely", "exactly",
                  "same", "agreed", "update", "reminder", "note", "monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday",
                  "january", "february", "march", "april", "may", "june", "july", "august", "september", "october", "november", "december",
                  "today", "tomorrow", "tonight", "thankful", "phew", "bless", "blessings", "hallelujah", "hugs", "xoxo", "cheers", "yikes",
                  "please", "sweet", "cool", "done", "got", "will", "can", "all", "you", "her", "him", "them", "she", "mama", "dear"}
_RELATION_RX = re.compile(r"\b(owner1|owner6|Jess|Jo)'?s\s+(" + "|".join(re.escape(w) for w in sorted(_RELATION_WORDS, key=len, reverse=True)) + r")\b", re.I)
_NAME = r"[A-Z][a-zA-Z'’-]+(?:\s+(?:[A-Z][a-zA-Z'’-]+|[a-z]{1,3}\s+[A-Z][a-zA-Z'’-]+)){0,3}"
_NOT_NAMES = {"there", "guys", "all", "friend", "friends", "man", "babe", "love", "you", "everyone", "team", "both", "hey", "hi",
              "hello", "yes", "no", "ok", "okay", "thanks", "thank", "good", "morning", "afternoon", "evening", "owner1", "owner6",
              "sorry", "please", "just", "the", "this", "that", "it", "we", "i", "so", "and", "also", "congrats", "happy", "merry",
              "dear", "example", "films", "yep", "sure", "great", "awesome", "see", "talk", "sounds", "perfect", "same", "on",
              "my", "our", "your", "a", "an", "to", "for", "of", "in", "with", "from", "at", "by", "up", "out", "me", "us", "them"}
_GREET = re.compile(r"^\s*(?:hey|hi|hello|hiya|yo|howdy|good\s+(?:morning|afternoon|evening)|happy\s+\w+day)\s*[,!]?\s+(" + _NAME + r")\b", re.I)
_FAMILY_SIGN = re.compile(r"\b(?:love\s+you(?:\s+too)?|miss\s+you(?:\s+too)?|thanks|thank\s+you|night|morning|ok|okay|yes|no|bye|see\s+you)[,!. ]+"
                          r"(dad|daddy|mom|mommy|mama|papa|pops|grandma|grandpa|nana|son|sis|bro|sweetheart)\b[.!]*\s*$", re.I)
_ITS_ME = re.compile(r"^\s*(?:hey|hi|hello|howdy)?\s*[,!]?\s*(?:it'?s|this is|it is)\s+(" + _NAME + r")(?:\s+(?:from|with|at)\s+(" + _NAME + r"))?[.,!: ]", re.I)
_PREFIX_SIGN = re.compile(r"^\s*(" + _NAME + r"):\s+\S")
_TRAIL_SIGN = re.compile(r"(?:^|\n)\s*(?:[-–—~]\s*|(?:thanks|thank you|best|cheers|love|blessings|sincerely),?\s+)(" + _NAME + r")\s*[.!]?\s*$")
_OWNER_INTRO = re.compile(r"\bthis is (?:owner1|owner6)\b.*\b(?:with|from)\s+([A-Z][\w&' ]{2,40}?)(?:[.,!]|\s+(?:and|I|we)\b)", re.I)
_CLUES = [
    (re.compile(r"\b(babe|baby|honey|hon|sweetheart|my love)\b", re.I), "writes like a partner or close family ('babe')"),
    (re.compile(r"\b(first draft|rough cut|timeline|color grade|colou?r|sound design|artlist|musicbed|export|render|b-?roll|premiere|resolve)\b", re.I),
     "talks like an editor on a job"),
    (re.compile(r"\b(how much|rate|quote|do you (?:do|shoot|film|rent)|available|availability|book you|pricing|price)\b", re.I),
     "asks about hiring or renting (a lead?)"),
    (re.compile(r"\b(invoice|balance|payment|pay you|venmo|zelle|owe)\b", re.I), "money is involved"),
    (re.compile(r"\b(church|worship|praying|prayer|pastor|bible|jesus)\b", re.I), "church circle"),
    (re.compile(r"\b(wedding|bride|groom|venue|ceremony|reception|vows)\b", re.I), "talks about a wedding"),
    (re.compile(r"\b(camping|tent|sleeping bag|packing list|bachelor|suit|groomsm[ae]n|hike)\b", re.I), "a trip or group plan"),
    (re.compile(r"\b(car|oil|tire|brake|engine|diagnostic|shop)\b", re.I), "about a car or a repair"),
]


# ------------------------------------------------------------------ keys and formatting
def key(handle) -> str:
    """One key per handle: the last 10 digits of a phone, a lowercase email, a short code as-is."""
    h = str(handle or "").strip()
    if "@" in h:
        return h.lower()
    d = re.sub(r"\D", "", h)
    if len(d) >= 10:
        return d[-10:]
    return d


def pretty(handle) -> str:
    h = str(handle or "").strip()
    if "@" in h:
        return h.lower()
    d = re.sub(r"\D", "", h)
    if len(d) == 11 and d[0] == "1":
        d = d[1:]
    if len(d) == 10:
        return f"+1 {d[:3]}-{d[3:6]}-{d[6:]}"
    return h or ""


def is_shortcode(handle) -> bool:
    h = str(handle or "").strip()
    if "@" in h:
        return bool(re.search(r"no-?reply|rbm\.goog|notification|alerts?@|donotreply", h, re.I))
    d = re.sub(r"\D", "", h)
    return 3 <= len(d) <= 6


def _norm_name(n: str) -> str:
    return re.sub(r"[^a-z0-9 ]", "", re.sub(r"\(.*?\)", "", str(n or "")).lower()).strip()


def _first(n: str) -> str:
    return (_norm_name(n).split() or [""])[0]


def _namey(s: str) -> bool:
    """A capitalised one-to-four-word thing that is not a greeting word."""
    s = str(s or "").strip()
    if not s or len(s) > 48 or re.search(r"\d", s):
        return False
    words = s.split()
    if not words or len(words) > 4:
        return False
    if _norm_name(words[0]) in _NOT_NAMES:
        return False
    return words[0][:1].isupper()


def _clip(s: str, n: int = 80) -> str:
    s = re.sub(r"\s+", " ", str(s or "")).strip()
    return s if len(s) <= n else s[: n - 1].rstrip() + "…"


def _date(ts: str) -> str:
    ts = str(ts or "")
    return f"{ts[5:7].lstrip('0')}/{ts[8:10].lstrip('0')}" if len(ts) >= 10 else ""


# ------------------------------------------------------------------ candidate records
def _clean_name(n) -> str:
    n = re.sub(r"\s+", " ", str(n or "")).strip().strip("-–—:;,.!?\"' ")
    n = re.sub(r"['’]s$", "", n)                                   # "Gray Brightwater's" -> Gray Brightwater
    n = re.sub(r"\s+(?:and|&)\s*$", "", n)
    return n[:80]


def _clean_relation(r) -> str:
    r = re.split(r"[;(\[]", str(r or ""))[0].strip().strip(".,")   # "Friend (Nico Dunmore ...); texts from" -> Friend
    if re.search(r"\d[\d\s().-]{6,}\d", re.sub(r"\d{4}-\d{2}-\d{2}", "", r)) or len(r) > 60:   # a phone number is not a relation (a date is fine)
        return ""
    return r


def _cand(name, source, conf, kind="contact", relation="", evidence="", saved=False) -> dict:
    return {"name": _clean_name(name), "source": source, "confidence": round(float(conf), 2), "kind": kind,
            "relation": _clean_relation(relation), "evidence": str(evidence or "").strip()[:200], "saved": bool(saved)}


class Index:
    """Everything known about every handle, built once per call (cheap: a few hundred people)."""

    def __init__(self):
        self.by_key: dict[str, list[dict]] = {}
        self.owner_emails: set[str] = set()
        self.owner_keys: set[str] = set()
        self.known_names: dict[str, str] = {}      # first name (lower) -> full name, for "mentions X" hints
        self.relations: dict[str, tuple] = {}      # normalised name -> (relation, kind) from the People tables
        self.relation_names: dict[str, str] = {}   # normalised name -> the name as written there
        self.owner_first: set[str] = set()
        self._groups: dict = {}
        self.book_loaded = False
        self.threads: dict = {}
        self.errors: list[str] = []

    def add(self, handle, cand: dict) -> None:
        k = key(handle)
        if not k or len(k) < 5 and "@" not in k:
            return
        if not cand.get("name"):
            return
        self.by_key.setdefault(k, []).append(cand)
        if cand.get("kind") not in ("automated", "business") and cand.get("source") in ("owners", "told", "phone book", "wiki people", "memory", "phone") \
                and len(_norm_name(cand["name"]).split()) >= 2:
            f = _first(cand["name"])
            if f and len(f) > 2 and f not in self.known_names:
                self.known_names[f] = cand["name"]

    # -- sources -----------------------------------------------------------
    def load_owners(self):
        try:
            people = load_json(PEOPLE, {}) or {}
            for u in (people.get("users") or {}).values():
                self.owner_first.add(_first(u.get("name") or ""))
                if u.get("phone"):
                    self.add(u["phone"], _cand(u.get("name"), "owners", 1.0, "owner", "owner", "the owners' own number", True))
                    self.owner_keys.add(key(u["phone"]))
                for e in u.get("emails") or []:
                    self.owner_emails.add(str(e).lower())
                    self.add(e, _cand(u.get("name"), "owners", 1.0, "owner", "owner", "the owners' own email", True))
        except Exception as e:  # noqa: BLE001
            self.errors.append(f"owners: {type(e).__name__}")

    def load_aliases(self):
        try:
            for k, a in (load_json(ALIASES, {}) or {}).items():
                if not isinstance(a, dict) or not a.get("name"):
                    continue
                who = a.get("by") or "an owner"
                self.add(k, _cand(a["name"], "told", 0.97, a.get("kind") or "contact", a.get("relation", ""),
                                  f"{who} said so in chat" + (f" ({str(a.get('ts', ''))[:10]})" if a.get("ts") else ""), False))
        except Exception as e:  # noqa: BLE001
            self.errors.append(f"aliases: {type(e).__name__}")

    def load_books(self):
        try:
            import contacts
            db = contacts.load()
            if db.get("books"):
                self.book_loaded = True
            for p in contacts.everyone(db):
                nm = p.get("name") or ""
                rel = ""
                for r in p.get("related") or []:
                    if r.get("label"):
                        rel = f"{r['label']}: {r['name']}"
                        break
                owner = p.get("owner", "owner1")
                ev = f"saved in {owner.title()}'s phone" + (f" ({p['org']})" if p.get("org") else "")
                for ph in p.get("phones") or []:
                    self.add(ph.get("value"), _cand(nm, "phone book", 0.92, "contact", rel, ev, True))
                    if p.get("nickname"):      # "Jer" finds him too; the plain name stays the name
                        self.add(ph.get("value"), _cand(p["nickname"], "phone book", 0.3, "contact", rel, f"saved as '{p['nickname']}'", True))
                for em in p.get("emails") or []:
                    self.add(em.get("value"), _cand(nm, "phone book", 0.9, "contact", rel, ev, True))
        except Exception as e:  # noqa: BLE001
            self.errors.append(f"books: {type(e).__name__}")

    def load_wiki(self):
        people_dir = WIKI / "People"
        try:
            for f in sorted(people_dir.glob("*.md")) if people_dir.exists() else []:
                txt = f.read_text(encoding="utf-8", errors="replace")
                if f.stem.lower() in ("contacts", "about"):
                    if f.stem.lower() == "contacts":
                        self._wiki_contacts_table(txt)
                    continue
                self._wiki_note(f, txt)
        except Exception as e:  # noqa: BLE001
            self.errors.append(f"wiki people: {type(e).__name__}")
        for folder, kind in (("Couples", "client"), ("Vendors", "vendor")):
            try:
                d = WIKI / folder
                for f in sorted(d.rglob("*.md")) if d.exists() else []:
                    txt = f.read_text(encoding="utf-8", errors="replace")
                    title = re.sub(r"\s*\((lead|client|vendor)\)\s*$", "", f.stem, flags=re.I)
                    k2 = "lead" if "(lead)" in f.stem.lower() else kind
                    for m in _PHONE.finditer(txt):
                        self.add(m.group(0), _cand(title, f"wiki {folder}", 0.86, k2, "", f"their number is on Wiki/{folder}/{f.stem}"))
                    for m in re.finditer(r"[\w.+-]+@[\w-]+\.[\w.-]+", txt):
                        if m.group(0).lower() not in self.owner_emails:
                            self.add(m.group(0), _cand(title, f"wiki {folder}", 0.8, k2, "", f"their email is on Wiki/{folder}/{f.stem}"))
            except Exception as e:  # noqa: BLE001
                self.errors.append(f"wiki {folder}: {type(e).__name__}")

    def _wiki_contacts_table(self, txt: str):
        """Wiki/People/Contacts.md (written by contacts.py): one row per person, phones in column 3."""
        for line in txt.splitlines():
            if not line.startswith("|") or line.startswith("|--") or line.startswith("| Name"):
                continue
            cells = [c.strip() for c in line.strip("|").split("|")]
            if len(cells) < 3 or not cells[0]:
                continue
            for m in _PHONE.finditer(cells[2]):
                self.add(m.group(0), _cand(cells[0], "phone book", 0.9, "contact", "", "saved in the phone (Contacts.md)", True))
            if len(cells) > 3:
                for m in re.finditer(r"[\w.+-]+@[\w-]+\.[\w.-]+", cells[3]):
                    self.add(m.group(0), _cand(cells[0], "phone book", 0.88, "contact", "", "saved in the phone (Contacts.md)", True))

    def _wiki_note(self, f: Path, txt: str):
        head = txt.split("\n---", 2)[0] if txt.startswith("---") else ""
        tags = set()
        mt = re.search(r"^tags:\s*\[([^\]]*)\]", head, re.M)
        if mt:
            tags = {t.strip().lower() for t in mt.group(1).split(",")}
        title = re.sub(r"\s*\((lead|client)\)\s*$", "", f.stem, flags=re.I)
        is_lead = "(lead)" in f.stem.lower() or "lead" in tags
        note_kind = "lead" if is_lead else ("family" if "family" in tags and "friends" not in tags else "friend" if "friends" in tags else "contact")
        section, section_kind, owner, row_name, row_rel, heading_name = "", note_kind, "", "", "", ""
        for line in txt.splitlines():
            hm = re.match(r"^(#{1,6})\s+(.*?)\s*$", line)
            if hm:
                section = hm.group(2)
                sl = section.lower()
                owner = "owner1" if "owner1" in sl else "owner6" if "owner6" in sl else owner
                section_kind = ("family" if "family" in sl or "dad" in sl or "mom" in sl else "friend" if "friend" in sl or "church" in sl or "community" in sl
                                else note_kind)
                rel = _RELATION_RX.search(section)
                generic = re.search(r"\b(family|friends?|community|confirm|dates?|added|notes?|contacts?|both|church|open|threads?|to do|suits?|trip|party)\b", sl)
                if rel:
                    heading_name = rel.group(2).title()      # "## the owner's dad" -> Dad
                elif len(hm.group(1)) >= 2 and not generic and _namey(section.strip()):
                    heading_name = section.strip()           # "## Marisa" -> Marisa
                else:
                    heading_name = ""
                row_name, row_rel = "", ""
                continue
            if line.startswith("|") and not line.startswith("|--"):
                cells = [c.strip() for c in line.strip("|").split("|")]
                if len(cells) >= 2 and cells[0] and cells[0].lower() not in ("who", "name", "date"):
                    row_name = re.sub(r"\[\[|\]\]", "", cells[0]).strip()
                    row_rel = re.sub(r"\[\[|\]\]|\"", "", cells[1]).strip()[:60]
                    rel_clean = _clean_relation(row_rel)
                    if rel_clean and _namey(row_name) and "," not in row_name:
                        rel_full = rel_clean
                        if owner and section_kind == "family" and not rel_clean.lower().startswith(("owner1", "owner6")) and len(rel_clean) < 30:
                            rel_full = f"{owner}'s {rel_clean.lower()}"
                        self.relations.setdefault(_norm_name(row_name), (rel_full, section_kind))
                        self.relation_names.setdefault(_norm_name(row_name), row_name)
            for m in _PHONE.finditer(line):
                num = m.group(0)
                if line.startswith("|"):
                    nm, rel = row_name, row_rel
                    ev = f"Wiki/People/{f.stem}: '{_clip(row_name, 30)}' row"
                else:
                    nm, rel = heading_name or title, (f"{owner}'s {heading_name.lower()}" if heading_name and heading_name.lower() in _FAMILY_WORDS and owner else "")
                    ev = f"Wiki/People/{f.stem}" + (f" under '{section}'" if section else "")
                if not nm:
                    continue
                if owner and rel and not rel.lower().startswith(("owner1", "owner6")) and section_kind == "family":
                    rel = f"{owner}'s {rel.lower()}" if len(rel) < 30 else rel
                self.add(num, _cand(nm, "wiki people", 0.9, section_kind if section_kind != "contact" else note_kind, rel, ev))
            for m in re.finditer(r"[\w.+-]+@[\w-]+\.[\w.-]+", line):
                em = m.group(0)
                if em.lower() in self.owner_emails:
                    continue
                nm = row_name if line.startswith("|") else (heading_name or title)
                if nm:
                    self.add(em, _cand(nm, "wiki people", 0.85, note_kind, row_rel if line.startswith("|") else "", f"Wiki/People/{f.stem}"))

    def load_memory(self):
        try:
            d = load_json(MEMORY, {}) or {}
            facts = d.get("facts") if isinstance(d, dict) else d
            for fct in facts or []:
                if not isinstance(fct, dict) or not fct.get("active", True):
                    continue
                self._memory_fact(fct)
        except Exception as e:  # noqa: BLE001
            self.errors.append(f"memory: {type(e).__name__}")

    def _memory_fact(self, fct: dict):
        text = str(fct.get("fact") or "")
        mid = str(fct.get("id") or "m?")
        subject = str(fct.get("subject") or "").strip()
        for m in _PHONE.finditer(text):
            num = m.group(0)
            before, after = text[max(0, m.start() - 90):m.start()].rstrip(" (,"), text[m.end():m.end() + 60]
            # a number given to call back is not the sender ("gave 555-010-8010 as the number to call")
            if re.search(r"\b(?:call|dial|phone)\s*(?:them\s+)?(?:at|on)?$", before, re.I) or re.match(r"\s*(?:as|is) the number to call", after, re.I):
                continue
            name, rel, kind, conf = "", "", "contact", 0.0
            rm = _RELATION_RX.search(before[-60:])
            nm = re.search(r"(" + _NAME + r")(?:['’]s)?(?:\s+(?:number|phone|cell)(?:\s+is)?)?$", before) \
                or re.search(r"(" + _NAME + r")\s+(?:texts?|texted|wrote|called|is|at)(?:\s+(?:him|her|owner1|owner6))?(?:\s+(?:from|at|on))?$", before) \
                or re.search(r"(?:^|[.;!?]\s*)(" + _NAME + r")\b[^.;!?]{0,90}\b(?:texts?|texted|wrote|called)(?:\s+(?:him|her|owner1|owner6))?\s+(?:from|at|on)$", before)
            am = re.match(r"\s*\)?\s*(?:is|as|was|belongs to)\s+(" + _NAME + r")(?:['’]s number)?", after) or re.match(r"\s*\)?\s*\(?(" + _NAME + r")\)?\s*(?:texted|texts|wrote|said)", after)
            one_number = len(_PHONE.findall(text)) == 1
            unsure = bool(re.search(r"\b(may be|might be|unconfirmed|not confirmed|confirm before|unknown number|claims? to be|scam)\b", text, re.I))
            if nm:
                cand_name = re.split(r"\s+(?:and|&)\s+|,\s*", nm.group(1))[-1]      # "A and B (number)" -> B
                if _namey(cand_name):
                    name, conf = cand_name, 0.86
            if not name and am and _namey(am.group(1)):
                name, conf = am.group(1), 0.84
            if rm:
                owner, word = rm.group(1).title(), rm.group(2).lower()
                rel = f"{owner}'s {word}"
                kind = "family" if word in _FAMILY_WORDS else "contact"
                if not name or _clean_name(name).lower() in ("owner1", "owner6", "jess", "jo"):
                    name, conf = word.title() if word in _FAMILY_WORDS else f"{owner}'s {word}", 0.86
            if not name and one_number and subject and _namey(subject) and re.search(r"texts? from|texted|number|phone", text, re.I):
                name, conf = subject, 0.6
            if not name:
                continue
            if _clean_name(name).lower() in ("owner1", "owner6") and not rm:
                continue
            if unsure:
                conf = min(conf, 0.5)
            self.add(num, _cand(name, "memory", conf, kind, rel, f"memory {mid}: {_clip(text, 110)}"))
        for m in re.finditer(r"[\w.+-]+@[\w-]+\.[\w.-]+", text):
            em = m.group(0)
            if em.lower() in self.owner_emails:
                continue
            before = text[max(0, m.start() - 80):m.start()]
            nm = re.search(r"(" + _NAME + r")(?:'s)?\s*(?:email\s*(?:is)?)?\s*(?:\(|is\s*|at\s*)$", before)
            if nm and _namey(nm.group(1)):
                self.add(em, _cand(nm.group(1), "memory", 0.8, "contact", "", f"memory {mid}: {_clip(text, 110)}"))

    def load_leads(self):
        try:
            for f in sorted(LEADS.glob("*.json")) if LEADS.exists() else []:
                d = load_json(f, {}) or {}
                lead = d.get("lead") or {}
                nm = str(lead.get("name") or "").strip()
                if not nm:
                    continue
                partner = str(lead.get("partner") or "").strip()
                status = str(d.get("status") or "").strip()
                kind = "client" if status.lower() in ("booked", "client", "signed") else "lead"
                rel = ("with " + partner if partner else "") + (f", wedding {lead.get('date')}" if lead.get("date") else "")
                rel = rel.strip(", ")
                ev = f"quiz lead ({f.stem[:8]}…)" + (f", {status}" if status else "")
                if lead.get("phone"):
                    self.add(lead["phone"], _cand(nm, "lead", 0.93, kind, rel, ev))
                if lead.get("email"):
                    self.add(lead["email"], _cand(nm, "lead", 0.9, kind, rel, ev))
        except Exception as e:  # noqa: BLE001
            self.errors.append(f"leads: {type(e).__name__}")

    def load_tracker(self):
        try:
            ps = load_json(TRACKER, {}) or {}
            projects = ps.get("projects") if isinstance(ps, dict) and isinstance(ps.get("projects"), dict) else ps
            for p in (projects.values() if isinstance(projects, dict) else []):
                if not isinstance(p, dict):
                    continue
                couple = re.sub(r"'s Project$", "", str(p.get("couple") or "")).strip()
                if not couple:
                    continue
                stage = str(p.get("stage") or "")
                kind = "client" if stage not in ("inquiry", "lead", "cancelled", "lost") else "lead"
                rel = f"wedding {p.get('wedding_date')}" if p.get("wedding_date") else ""
                for ph in (p.get("phones") or []) + [p.get("phone")]:
                    if ph:
                        self.add(ph, _cand(couple, "tracker", 0.9, kind, rel, f"wedding tracker, stage {stage or '?'}"))
                for em in (p.get("emails") or []) + [p.get("email")]:
                    if em:
                        self.add(em, _cand(couple, "tracker", 0.88, kind, rel, f"wedding tracker, stage {stage or '?'}"))
        except Exception as e:  # noqa: BLE001
            self.errors.append(f"tracker: {type(e).__name__}")

    def load_crm(self):
        crm_src = crm.name().lower()
        try:
            import contacts
            for c in contacts.crm_people():
                if any(str(e.get("value") or "").lower() in self.owner_emails for e in c.get("emails", [])):
                    continue
                ev = f"a {crm.name()} contact" + (f" ({c['org']})" if c.get("org") else "")
                for ph in c.get("phones") or []:
                    self.add(ph.get("value"), _cand(c["name"], crm_src, 0.85, "client", "", ev))
                for em in c.get("emails") or []:
                    self.add(em.get("value"), _cand(c["name"], crm_src, 0.83, "client", "", ev))
        except Exception as e:  # noqa: BLE001
            self.errors.append(f"crm: {type(e).__name__}")

    def load_threads(self):
        try:
            self.threads = load_json(THREADS, {}) or {}
        except Exception as e:  # noqa: BLE001
            self.errors.append(f"threads: {type(e).__name__}")
            self.threads = {}
        for handle, th in self.threads.items():
            if not isinstance(th, dict):
                continue
            if th.get("name"):
                self.add(handle, _cand(th["name"], "phone", 0.9, "contact", "", "saved in the phone (the name came with their text)", True))
            self._thread_clues(handle, th)

    def _thread_clues(self, handle: str, th: dict):
        """Names in the conversation itself: how the owner addressed them, how they signed."""
        ins = [str(m.get("text") or "") for m in th.get("messages") or [] if m.get("dir") == "in" and not _TAPBACK.match(str(m.get("text") or ""))]
        real_in = len(ins)
        prefixed = sum(1 for t in ins if _PREFIX_SIGN.match(t))
        for m in th.get("messages") or []:
            text, d, ts = str(m.get("text") or ""), m.get("dir"), str(m.get("ts") or "")
            if not text.strip() or _TAPBACK.match(text):
                continue
            if d == "out":
                g = _GREET.match(text)
                if g and _namey(g.group(1)):
                    nm = g.group(1)
                    if re.match(r"^(?:guys|all|there|both|everyone|team)$", nm, re.I):
                        continue
                    self.add(handle, _cand(nm, "texts", 0.72, "contact", "", f"you opened a text to them with '{_clip(text, 40)}' ({_date(ts)})"))
                fs = _FAMILY_SIGN.search(text)
                if fs:
                    word = fs.group(1).lower()
                    word = {"daddy": "dad", "mommy": "mom"}.get(word, word)
                    self.add(handle, _cand(word.title(), "texts", 0.85, "family", f"the owner's {word}", f"you wrote '{_clip(text, 40)}' to them ({_date(ts)})"))
            else:
                im = _ITS_ME.match(text)
                if im and _namey(im.group(1)):
                    nm = im.group(1) + (f" ({im.group(2)})" if im.group(2) else "")
                    kind = "business" if _AUTOMATED.search(text) or _SPAM.search(text) or len(im.group(1).split()) >= 3 else "contact"
                    self.add(handle, _cand(nm, "texts", 0.66 if kind == "contact" else 0.8, kind, "", f"they wrote '{_clip(text, 40)}' ({_date(ts)})"))
                    continue
                pm = _PREFIX_SIGN.match(text)
                if pm and _namey(pm.group(1)) and pm.group(1).lower() not in ("owner1", "owner6", "note", "ps", "update", "reminder"):
                    kind = "business" if (_AUTOMATED.search(text) or is_shortcode(handle)) else "contact"
                    conf = 0.8 if kind == "business" else (0.62 if prefixed >= max(1, real_in // 2) else 0.3)
                    self.add(handle, _cand(pm.group(1), "texts", conf, kind, "", f"their text starts '{_clip(text, 40)}' ({_date(ts)})"))
                    continue
                tm = _TRAIL_SIGN.search(text)
                if tm and _namey(tm.group(1)) and len(text) > 40 and tm.group(1).lower() not in ("owner1", "owner6"):
                    self.add(handle, _cand(tm.group(1), "texts", 0.55, "contact", "", f"they signed '{tm.group(1)}' ({_date(ts)})"))

    def build(self) -> "Index":
        self.load_owners()
        self.load_aliases()
        self.load_books()
        self.load_wiki()
        self.load_memory()
        self.load_leads()
        self.load_tracker()
        self.load_crm()
        self.load_threads()
        self._groups = _group_threads(self.threads)
        self._group_vocatives()
        return self

    def _group_vocatives(self):
        """In a group text the members address the one who started it ("Praying for you, Nicole!"):
        a name two or more members use lands on the starter."""
        done = set()
        for h, g in self._groups.items():
            if id(g) in done:
                continue
            done.add(id(g))
            starter = g["starter"]
            votes: dict[str, set] = {}
            for mem in g["members"]:
                if mem == starter:
                    continue
                th = self.threads.get(mem) or {}
                for m in th.get("messages") or []:
                    t = str(m.get("text") or "")
                    if m.get("dir") != "in" or _TAPBACK.match(t):
                        continue
                    for nm in re.findall(r"\b([A-Z][a-z]{2,})(?=\s*[,!]|\s*[.?]?\s*(?:$|[^\w\s]))", t, re.M):
                        low = nm.lower()
                        if low in _NOT_NAMES or low in _VOCATIVE_STOP or low in self.owner_first:
                            continue
                        votes.setdefault(nm, set()).add(mem)
            if not votes:
                continue
            nm, who = max(votes.items(), key=lambda kv: len(kv[1]))
            if len(who) >= 2:
                self.add(starter, _cand(nm, "texts", 0.72, "contact", "", f"{len(who)} people in the group text address them as {nm}"))

    def relation_for(self, name: str) -> tuple:
        """(relation, kind, full name as the People tables write it) for a name; a lone first name counts
        when it is unique there."""
        nn = _norm_name(name)
        if nn in self.relations:
            rel, kd = self.relations[nn]
            return rel, kd, self.relation_names.get(nn, "")
        f = _first(name)
        hits = [(k2, v) for k2, v in self.relations.items() if k2.split()[0] == f]
        if len(hits) == 1 and (len(nn.split()) == 1 or hits[0][0].startswith(nn) or nn.startswith(hits[0][0])):
            return hits[0][1][0], hits[0][1][1], self.relation_names.get(hits[0][0], "")
        return ("", "", "")

    # -- the answer ----------------------------------------------------------
    def activity(self, handle) -> dict:
        th = self.threads.get(handle) or self.threads.get(_thread_handle(self.threads, handle)) or {}
        msgs = th.get("messages") or [] if isinstance(th, dict) else []
        ins = [m for m in msgs if m.get("dir") == "in"]
        outs = [m for m in msgs if m.get("dir") == "out"]
        real_in = [m for m in ins if not _TAPBACK.match(str(m.get("text") or ""))]
        last = msgs[-1] if msgs else {}
        last_in = (real_in or ins)[-1] if ins else {}
        return {"n_in": len(ins), "n_out": len(outs), "first": str((msgs[0] if msgs else {}).get("ts") or "")[:10],
                "last": str(last.get("ts") or "")[:16], "last_dir": last.get("dir", ""),
                "last_text": _clip(last.get("text"), 90), "last_in": _clip(last_in.get("text"), 90), "last_in_ts": str(last_in.get("ts") or "")[:16],
                "tapbacks_only": bool(ins) and not real_in}

    def automated(self, handle) -> tuple[bool, str]:
        """(is automated/business, why)."""
        if key(handle) in self.owner_keys:
            return False, ""
        if is_shortcode(handle):
            return True, "a short code (codes, alerts, promos)"
        th = self.threads.get(handle) or {}
        msgs = [str(m.get("text") or "") for m in (th.get("messages") or []) if m.get("dir") == "in"] if isinstance(th, dict) else []
        if not msgs:
            return False, ""
        hits = sum(1 for t in msgs if _AUTOMATED.search(t))
        spam = sum(1 for t in msgs if _SPAM.search(t))
        if spam and spam >= max(1, len(msgs) // 2):
            return True, "reads like spam"
        if hits and hits >= max(1, (len(msgs) + 1) // 2) and not any(m.get("dir") == "out" for m in th.get("messages") or []):
            if any(re.search(r"paid for by|vote|ballot|prop \d+|campaign", t, re.I) for t in msgs):
                return True, "a political campaign text"
            if any(re.search(r"verification code|security code|login code|2fa|two-factor|your code", t, re.I) for t in msgs):
                return True, "sign-in codes"
            if any(re.search(r"payment failure|final notice|policy", t, re.I) for t in msgs):
                return True, "an unverified billing notice (treat as 'a text claiming to be from X' until checked)"
            return True, "marketing from a business"
        return False, ""

    def resolve(self, handle, text: str | None = None) -> dict:
        k = key(handle)
        th_handle = _thread_handle(self.threads, handle)
        cands = list(self.by_key.get(k) or [])
        # clues from a text that has not been logged yet (the bridge calls before it logs)
        if text:
            tmp = Index()
            tmp.owner_emails = self.owner_emails
            tmp._thread_clues(th_handle or handle, {"messages": [{"dir": "in", "text": text, "ts": time.strftime("%Y-%m-%dT%H:%M")}]})
            cands += tmp.by_key.get(k, [])
        auto, why = self.automated(th_handle or handle)
        if text and not auto and (_AUTOMATED.search(text) or _SPAM.search(text)) and is_shortcode(handle):
            auto, why = True, "a short code"
        act = self.activity(th_handle or handle)
        merged = _merge(cands)
        for g in merged:
            if not g["relation"] or g["kind"] == "contact":
                rel, kd, full = self.relation_for(g["name"])
                if rel and not g["relation"]:
                    g["relation"] = rel
                if kd in ("family", "friend") and g["kind"] in ("contact", "unknown"):
                    g["kind"] = kd
                if full and g["sources"] == ["texts"] and g["confidence"] < SURE:
                    # "Hey Marisa" + a Marisa in the People notes: that is her
                    g["confidence"] = round(min(0.99, 1 - (1 - g["confidence"]) * 0.4), 2)
                    g["evidence"].append(f"a {full} is in the People notes" + (f" ({rel})" if rel else ""))
                if full and len(_norm_name(g["name"]).split()) == 1 and len(_norm_name(full).split()) > 1:
                    g["name"] = full
            if g["kind"] not in ("business", "automated") and _first(g["name"]) in self.owner_first and g["confidence"] >= LIKELY \
                    and g["sources"] != ["texts"]:
                g["kind"], g["confidence"], g["relation"] = "owner", max(g["confidence"], 0.95), g["relation"] or "owner"
        best = merged[0] if merged else None
        if best and best["kind"] not in ("business", "automated"):
            for g in list(merged[1:]):
                word = _norm_name(g["name"])
                if word in _FAMILY_WORDS and g["confidence"] >= MAYBE:      # "Dad" + "David example" = David example, Dad
                    best["relation"] = best["relation"] or g["relation"]
                    best["kind"] = "family"
                    best["nick"] = g["name"]
                    best["confidence"] = round(min(0.99, 1 - (1 - best["confidence"]) * (1 - g["confidence"])), 2)
                    best["evidence"] += [e for e in g["evidence"] if e not in best["evidence"]]
                    merged.remove(g)
        out = {"handle": th_handle or str(handle or ""), "key": k, "pretty": pretty(handle),
               "name": "", "relation": "", "kind": "unknown", "confidence": 0.0, "saved": False,
               "evidence": [], "candidates": merged[:5], "hints": [], "group": None, "activity": act,
               "automated": auto, "automated_why": why}
        if best and not (auto and best["kind"] not in ("business", "automated")):
            out.update({"name": best["name"], "relation": best["relation"], "kind": best["kind"],
                        "confidence": best["confidence"], "saved": best["saved"], "evidence": best["evidence"],
                        **({"nick": best["nick"]} if best.get("nick") else {})})
        elif best and auto:
            out.update({"name": best["name"], "kind": "business", "confidence": max(best["confidence"], 0.8),
                        "evidence": best["evidence"]})
        if auto:
            out["kind"] = "automated" if out["kind"] in ("unknown", "contact") else out["kind"]
            if not out["name"]:
                out["name"] = _business_name(self.threads.get(th_handle or handle) or {}, text)
                out["confidence"] = max(out["confidence"], 0.7) if out["name"] else 0.0
            out["hints"].append(why)
        if k in self.owner_keys:
            out["kind"] = "owner"
        # second opinion: a different person also fits
        if len(merged) > 1 and merged[1]["confidence"] >= MAYBE and _first(merged[1]["name"]) != _first(merged[0]["name"]):
            out["also"] = merged[1]
        # group texts
        g = self._groups.get(th_handle or handle)
        if g:
            out["group"] = g
        # hints for the unsure
        if out["confidence"] < SURE and not auto:
            out["hints"] += self._hints(th_handle or handle, text)
        out["label"] = label_of(out, self.book_loaded)
        out["short"] = label_of(out, self.book_loaded, short=True)
        return out

    def _hints(self, handle, text: str | None) -> list[str]:
        hints, k = [], key(handle)
        th = self.threads.get(handle) or {}
        texts_in = [str(m.get("text") or "") for m in (th.get("messages") or []) if m.get("dir") == "in"] if isinstance(th, dict) else []
        if text:
            texts_in.append(text)
        blob = "\n".join(t for t in texts_in if not _TAPBACK.match(t))
        for rx, hint in _CLUES:
            if rx.search(blob) and hint not in hints:
                hints.append(hint)
        # a known first name they mention or address ("Thanks for doing all this Hollis!")
        mentioned = []
        for w in re.findall(r"\b([A-Z][a-z]{2,})\b", blob):
            full = self.known_names.get(w.lower())
            if full and w.lower() not in ("owner1", "owner6") and full not in mentioned:
                mentioned.append(full)
        if mentioned:
            hints.append("mentions " + ", ".join(mentioned[:3]))
        # one digit from a known person's number (a family plan, a work block)
        if k and k.isdigit() and len(k) == 10:
            for k2, cs in self.by_key.items():
                if k2 == k or not k2.isdigit() or len(k2) != 10 or k2[:9] != k[:9]:
                    continue
                top = _merge(cs)
                if top and top[0]["confidence"] >= LIKELY and top[0]["kind"] not in ("business", "automated"):
                    hints.append(f"one digit from {top[0]['name']}'s number ({pretty(k2)})")
                    break
        act = self.activity(handle)
        if act.get("tapbacks_only"):
            hints.append("only reactions so far (a group-text member)")
        return hints[:5]


def _thread_handle(threads: dict, handle) -> str:
    """The thread log's own key for this handle (+1XXXXXXXXXX, an email, a short code)."""
    k = key(handle)
    if not k:
        return ""
    h = str(handle or "").strip()
    if h in threads:
        return h
    for t in threads:
        if key(t) == k:
            return t
    return ""


def _merge(cands: list[dict]) -> list[dict]:
    """Fold candidates that name the same person (same first name, one a prefix of the other) into
    one, combining confidence noisy-or style and keeping every piece of evidence."""
    groups: list[dict] = []
    for c in sorted([c for c in cands if c.get("name")], key=lambda c: -c["confidence"]):
        nn, fn = _norm_name(c["name"]), _first(c["name"])
        home = None
        for g in groups:
            gn = _norm_name(g["name"])
            if nn == gn or (fn == _first(g["name"]) and (gn.startswith(nn) or nn.startswith(gn) or len(nn.split()) == 1 or len(gn.split()) == 1)):
                home = g
                break
        if home is None:
            groups.append({"name": c["name"], "confidence": c["confidence"], "kind": c["kind"], "relation": c["relation"],
                           "saved": c["saved"], "evidence": [c["evidence"]] if c["evidence"] else [], "sources": [c["source"]],
                           "_miss": 1 - c["confidence"]})
            continue
        home["_miss"] *= (1 - c["confidence"])
        home["confidence"] = round(min(0.99, 1 - home["_miss"]), 2)
        if len(_norm_name(c["name"])) > len(_norm_name(home["name"])) and c["kind"] != "business":
            home["name"] = c["name"]
        if not home["relation"] and c["relation"]:
            home["relation"] = c["relation"]
        if KINDS_ORDER.index(c["kind"]) < KINDS_ORDER.index(home["kind"]) if c["kind"] in KINDS_ORDER and home["kind"] in KINDS_ORDER else False:
            home["kind"] = c["kind"]
        home["saved"] = home["saved"] or c["saved"]
        if c["evidence"] and c["evidence"] not in home["evidence"]:
            home["evidence"].append(c["evidence"])
        if c["source"] not in home["sources"]:
            home["sources"].append(c["source"])
    for g in groups:
        g.pop("_miss", None)
    groups.sort(key=lambda g: -g["confidence"])
    return groups


def _business_name(th: dict, text: str | None) -> str:
    msgs = [str(m.get("text") or "") for m in (th.get("messages") or []) if m.get("dir") == "in"] if isinstance(th, dict) else []
    if text:
        msgs.append(text)
    for t in msgs:
        pm = _PREFIX_SIGN.match(t)
        if pm and _namey(pm.group(1)) and pm.group(1).lower() not in ("owner1", "owner6"):
            return pm.group(1)
        im = _ITS_ME.match(t)
        if im:
            return im.group(1)
        m = re.search(r"\b(?:from|at|with|love,?)\s+([A-Z][\w&'’.-]+(?:\s+[A-Z][\w&'’.-]+){0,3})\b[!.,]", t)
        if m and _namey(m.group(1)):
            return m.group(1)
        m = re.search(r"(?:Welcome to|Thanks for (?:joining|shopping at)|UNSUB)\s+([A-Z][\w&'’.-]+(?:\s+[A-Z][\w&'’.-]+){0,3})", t)
        if m:
            return m.group(1).title() if m.group(1).isupper() else m.group(1)
    blob = " ".join(msgs)
    for brand, rx in (("Facebook", r"fb\.me/"), ("Google", r"google verification"), ("PayPal", r"paypal"), ("CA DMV", r"CA DMV"),
                      ("Plaid", r"plaid"), ("Cash App", r"cash\.app"), ("Relay", r"\bRelay:")):
        if re.search(rx, blob, re.I):
            return brand
    if re.search(r"paid for by", blob, re.I):
        return "a political campaign"
    if re.search(r"verification code|security code|login code", blob, re.I):
        return "a sign-in code sender"
    return ""


def _group_threads(threads: dict) -> dict:
    """Group texts: iMessage forwards each member's lines as its own thread; a member's tapback
    ('Loved “…”') quotes a line that another member sent. Threads whose reactions quote each other
    are one group. -> {handle: {"members": [...], "starter": handle, "topic": first line}}"""
    quoted_by: dict[str, str] = {}        # normalised text -> handle that wrote it (inbound, not a tapback)
    first_line: dict[str, tuple[str, str]] = {}
    for h, th in threads.items():
        if not isinstance(th, dict):
            continue
        for m in th.get("messages") or []:
            t = str(m.get("text") or "")
            if m.get("dir") != "in" or _TAPBACK.match(t) or len(t) < 25:
                continue
            quoted_by.setdefault(_qkey(t), h)
            if h not in first_line or str(m.get("ts") or "") < first_line[h][0]:
                first_line[h] = (str(m.get("ts") or ""), t)
    parent: dict[str, str] = {}

    def find(x):
        parent.setdefault(x, x)
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def union(a, b):
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[rb] = ra

    starts: dict[str, int] = {}
    for h, th in threads.items():
        if not isinstance(th, dict):
            continue
        for m in th.get("messages") or []:
            t = str(m.get("text") or "")
            tb = _TAPBACK.match(t) if m.get("dir") == "in" else None
            if not tb or not tb.group(2):
                continue
            src = quoted_by.get(_qkey(tb.group(2)))
            if src and src != h:
                union(src, h)
                starts[src] = starts.get(src, 0) + 1
    groups: dict[str, list] = {}
    for h in list(parent):
        groups.setdefault(find(h), []).append(h)
    out = {}
    for members in groups.values():
        if len(members) < 2:
            continue
        starter = max(members, key=lambda h: (starts.get(h, 0), -len(h)))
        topic = first_line.get(starter, ("", ""))[1]
        rec = {"members": sorted(members), "starter": starter, "topic": _clip(topic, 70), "size": len(members)}
        for h in members:
            out[h] = rec
    return out


def _qkey(t: str) -> str:
    return re.sub(r"\W+", " ", str(t or "").lower()).strip()[:60]


# ------------------------------------------------------------------ labels
def label_of(r: dict, book_loaded: bool = False, short: bool = False) -> str:
    """'Dad (the owner's dad, +1 555-063-5563)'; 'probably Marisa (+1 ...)'; '+1 555-072-6472 (not known; writes like a partner)'."""
    num = r.get("pretty") or r.get("handle") or ""
    name, conf, kind = r.get("name") or "", float(r.get("confidence") or 0), r.get("kind") or "unknown"
    rel = r.get("relation") or ""
    if kind in ("automated", "business"):
        what = name or "an automated sender"
        return f"{what} ({num}, {r.get('automated_why') or 'automated'})" if not short else f"{what} ({num})"
    if name and conf >= SURE:
        if short:
            return f"{name} ({r['nick'] + ', ' if r.get('nick') else ''}{num})"
        bits = [b for b in (r.get("nick") or "", rel if rel and _norm_name(rel) not in _norm_name(name) else "", num) if b]
        if book_loaded and not r.get("saved") and kind != "owner":
            bits.append("not saved in the phone")
        return f"{name} ({', '.join(bits)})" if bits else name
    if name and conf >= LIKELY:
        s = f"probably {name} ({num})"
        if r.get("also") and not short:
            s = f"probably {name}, or {r['also']['name']} ({num})"
        return s
    if name and conf >= MAYBE:
        s = f"{num} (maybe {name}" + (f" or {r['also']['name']}" if r.get("also") and not short else "") + ")"
        return s
    hints = r.get("hints") or []
    if short or not hints:
        return f"{num} (not known)"
    return f"{num} (not known; {'; '.join(hints[:2])})"


# ------------------------------------------------------------------ public API
_INDEX: dict = {"built": 0.0, "idx": None, "sig": None}


def _signature() -> tuple:
    out = []
    for p in (THREADS, ALIASES, PEOPLE, MEMORY, TRACKER, DATA / "contacts.json"):
        try:
            out.append(p.stat().st_mtime_ns)
        except OSError:
            out.append(0)
    try:
        out.append(max([f.stat().st_mtime_ns for f in LEADS.glob("*.json")] or [0]))
    except OSError:
        out.append(0)
    return tuple(out)


def index(fresh: bool = False) -> Index:
    """The merged index, rebuilt when any source file changed (or after 5 minutes: the wiki is not stat'ed)."""
    sig = _signature()
    if fresh or _INDEX["idx"] is None or _INDEX["sig"] != sig or time.time() - _INDEX["built"] > 300:
        _INDEX.update({"idx": Index().build(), "sig": sig, "built": time.time()})
    return _INDEX["idx"]


def resolve(handle, text: str | None = None, fresh: bool = False) -> dict:
    """Who a phone number or email is, with confidence and evidence. Never raises."""
    try:
        return index(fresh).resolve(handle, text)
    except Exception as e:  # noqa: BLE001
        return {"handle": str(handle or ""), "key": key(handle), "pretty": pretty(handle), "name": "", "relation": "", "kind": "unknown",
                "confidence": 0.0, "saved": False, "evidence": [], "candidates": [], "hints": [], "group": None, "automated": False,
                "automated_why": "", "activity": {}, "label": f"{pretty(handle)} (not known)", "short": f"{pretty(handle)} (not known)",
                "error": f"{type(e).__name__}: {str(e)[:120]}"}


def name_for(handle, text: str | None = None, min_conf: float = LIKELY) -> str:
    """The display name for a sender, '' when nobody knows (used by the phone bridge for the ticket title)."""
    r = resolve(handle, text)
    if r.get("kind") in ("automated", "business"):
        return r.get("name") or ""
    return r.get("name") if float(r.get("confidence") or 0) >= min_conf else ""


def who_block(handle, text: str | None = None) -> dict:
    """The compact `who` record a ticket carries: name, relation, kind, confidence, the first reason."""
    r = resolve(handle, text)
    return {"name": r.get("name", ""), "relation": r.get("relation", ""), "kind": r.get("kind", "unknown"),
            "confidence": r.get("confidence", 0.0), "label": r.get("label", ""),
            "why": (r.get("evidence") or [""])[0][:160], "hints": (r.get("hints") or [])[:3],
            "group": (f"group text ({r['group']['size']} people), started by {pretty(r['group']['starter'])}: {r['group']['topic']}" if r.get("group") else "")}


_SYN = {"dad": ("dad", "father", "papa", "pops"), "father": ("dad", "father"), "mom": ("mom", "mother", "mama"), "mother": ("mom", "mother"),
        "stepmom": ("stepmom", "stepmother", "marisa"), "stepmother": ("stepmom", "stepmother"), "wife": ("owner6", "wife"),
        "husband": ("owner1", "husband"), "mil": ("mother-in-law",), "mechanic": ("mechanic", "car", "oil", "shop"),
        "editor": ("editor", "draft", "edit"), "grandma": ("grandma", "grandmother", "nana"), "grandpa": ("grandpa", "grandfather")}


def find(query: str, days: int = 90, limit: int = 5) -> list[dict]:
    """Who an owner means: a number, an email, a saved name, a relationship ('my dad'), a role ('the
    mechanic'), or words from what they wrote ('the one about the gift exchange'). Ranked candidates."""
    q = str(query or "").strip()
    if not q:
        return []
    idx = index()
    k = key(q)
    if ("@" in q or len(re.sub(r"\D", "", q)) >= 7) and (k in idx.by_key or _thread_handle(idx.threads, q)):
        return [idx.resolve(q)]
    words = [w for w in re.split(r"[^a-z0-9'-]+", q.lower()) if len(w) > 1 and w not in ("my", "the", "our", "who", "is", "texted", "text", "me", "that", "from", "a", "an")]
    expanded = set(words)
    for w in words:
        expanded.update(_SYN.get(w, ()))
    since = (datetime.now() - timedelta(days=max(1, days))).strftime("%Y-%m-%dT%H:%M:%S")
    scored = []
    handles = set(idx.threads) | {h for h in idx.by_key}
    for h in handles:
        th = idx.threads.get(h) if h in idx.threads else idx.threads.get(_thread_handle(idx.threads, h))
        r = idx.resolve(h)
        if r["kind"] in ("automated",) and not any(w in (r.get("name") or "").lower() for w in expanded):
            continue
        hay_name = " ".join([r.get("name") or "", r.get("relation") or "", r.get("kind") or ""] + [c["name"] + " " + c["relation"] for c in r.get("candidates") or []]).lower()
        hay_hints = " ".join(r.get("hints") or []).lower()
        score = 0.0
        for w in expanded:
            if re.search(r"\b" + re.escape(w), hay_name):
                score += 3
            if w in hay_hints:
                score += 1.5
        if th:
            recent = [m for m in th.get("messages") or [] if str(m.get("ts") or "") >= since]
            blob_in = " ".join(str(m.get("text") or "").lower() for m in recent if m.get("dir") == "in")
            blob_out = " ".join(str(m.get("text") or "").lower() for m in recent if m.get("dir") == "out")
            for w in expanded:
                if re.search(r"\b" + re.escape(w), blob_in):
                    score += 1
                if re.search(r"\b" + re.escape(w), blob_out):
                    score += 1.2
            if recent and score > 0:
                score += 0.3      # a live thread beats a cold record
        if score > 0:
            scored.append((score, r.get("activity", {}).get("last", ""), r))
    scored.sort(key=lambda s: (-s[0], s[1]), reverse=False)
    scored.sort(key=lambda s: -s[0])
    out = []
    seen = set()
    for sc, _, r in scored:
        if r["key"] in seen:
            continue
        seen.add(r["key"])
        r = dict(r)
        r["match"] = round(sc, 1)
        out.append(r)
        if len(out) >= max(1, limit):
            break
    return out


def recent_texts(handle, n: int = 6) -> list[str]:
    idx = index()
    h = _thread_handle(idx.threads, handle)
    th = idx.threads.get(h) or {}
    rows = []
    for m in (th.get("messages") or [])[-n:]:
        rows.append(f"[{str(m.get('ts') or '')[:16].replace('T', ' ')}] {'them' if m.get('dir') == 'in' else 'you'}: {_clip(m.get('text'), 160)}")
    return rows


def explain(handle_or_query: str, n_texts: int = 6) -> str:
    """One readable answer for Sage / the operator / the CLI."""
    rs = find(handle_or_query)
    if not rs:
        return f"Nothing known about {handle_or_query!r}: not in the contacts, the wiki, memory, the CRM, the leads or the text log."
    out = []
    for i, r in enumerate(rs[:3]):
        head = r["label"] if i == 0 else f"also possible: {r['label']}"
        out.append(head)
        if r.get("kind") and r["kind"] not in ("unknown",):
            out.append(f"  kind: {r['kind']}" + (f"; relation: {r['relation']}" if r.get("relation") else ""))
        for e in (r.get("evidence") or [])[:4]:
            out.append(f"  because: {e}")
        if r.get("also"):
            out.append(f"  or maybe: {r['also']['name']} ({', '.join(r['also'].get('evidence') or [])[:120]})")
        if r.get("group"):
            g = r["group"]
            out.append(f"  group text of {g['size']} people, started by {pretty(g['starter'])}: \"{g['topic']}\"")
        for h in (r.get("hints") or [])[:3]:
            out.append(f"  clue: {h}")
        a = r.get("activity") or {}
        if a.get("n_in") or a.get("n_out"):
            out.append(f"  texts: {a.get('n_in', 0)} in, {a.get('n_out', 0)} out, {a.get('first', '')} to {a.get('last', '')[:10]}")
        if i == 0:
            for row in recent_texts(r["handle"], n_texts):
                out.append("  " + row)
    return "\n".join(out)


def directory(days: int = 30, limit: int = 45) -> str:
    """The PEOPLE WHO TEXT block for the agent's context: every human thread of the last `days`,
    best name first with how sure, the last line, group markers; automated senders folded into one line."""
    idx = index()
    since = (datetime.now() - timedelta(days=max(1, days))).strftime("%Y-%m-%dT%H:%M:%S")
    humans, autos, groups_seen = [], [], {}
    for h, th in idx.threads.items():
        if not isinstance(th, dict) or not th.get("messages"):
            continue
        last = str(th["messages"][-1].get("ts") or "")
        if last < since:
            continue
        r = idx.resolve(h)
        if r["kind"] == "owner":
            continue
        if r["kind"] in ("automated", "business") or r.get("automated"):
            autos.append(r["name"] or pretty(h))
            continue
        humans.append(r)
    humans.sort(key=lambda r: r.get("activity", {}).get("last", ""), reverse=True)
    lines = [f"PEOPLE WHO TEXT (the owners' iPhones, last {days} days; the name is the best match from contacts, memory, the wiki, "
             "the CRM, the leads and the texts themselves. 'probably' = say your best guess in one clause and act on it; "
             "'not known' = use the clue, never ask who it is if the texts say. `who_is` gives the evidence; `identify` saves a name an owner tells you.):"]
    for r in humans[:limit]:
        a = r.get("activity") or {}
        who = r["label"]
        tail = []
        if r.get("group"):
            g = r["group"]
            gid = g["starter"]
            tail.append(f"group text started by {idx.resolve(gid)['short'] if gid != r['handle'] else 'them'} ({g['size']} people)")
        last_dir = "them" if a.get("last_dir") == "in" else "you"
        lines.append(f"- {who} · {a.get('n_in', 0)} in/{a.get('n_out', 0)} out · last {_date(a.get('last', ''))} {last_dir}: \"{_clip(a.get('last_text'), 70)}\""
                     + (" · " + "; ".join(tail) if tail else ""))
    if len(humans) > limit:
        lines.append(f"- … and {len(humans) - limit} more; `who_is` or `texts_search` finds them")
    if autos:
        names = sorted({n for n in autos if n})
        lines.append(f"- {len(autos)} automated/business senders (codes, promos, campaigns; never a reply): " + ", ".join(names[:14]) + ("…" if len(names) > 14 else ""))
    if idx.book_loaded is False:
        lines.append("- (no iPhone contacts have been imported yet: 'saved' names come from the texts, memory and the records; "
                     "the owners can upload the phone's contacts on Mission Control /contacts)")
    return "\n".join(lines)


# ------------------------------------------------------------------ learning
def learn(handle, name: str, relation: str = "", kind: str = "", by: str = "", evidence: str = "") -> dict:
    """An owner said who a number is: keep it (aliases file) and name the thread. Returns the record."""
    k = key(handle)
    name = re.sub(r"\s+", " ", str(name or "")).strip()[:80]
    if not k or (len(k) < 5 and "@" not in k) or not name:
        return {"ok": False, "reason": "needs a phone number or email and a name"}
    kind = kind if kind in KINDS_ORDER else ""
    if not kind:
        rl = relation.lower()
        kind = "family" if any(w in rl for w in _FAMILY_WORDS) else "contact"
    db = load_json(ALIASES, {}) or {}
    rec = {"name": name, "relation": str(relation or "").strip()[:80], "kind": kind, "by": str(by or "")[:40],
           "ts": time.strftime("%Y-%m-%dT%H:%M:%S"), "evidence": str(evidence or "")[:200], "handle": pretty(handle)}
    old = db.get(k)
    db[k] = rec
    save_json(ALIASES, db)
    _name_thread(handle, name)
    _INDEX["sig"] = None
    return {"ok": True, "key": k, "record": rec, "replaced": old}


def unlearn(handle) -> bool:
    k = key(handle)
    db = load_json(ALIASES, {}) or {}
    if k not in db:
        return False
    db.pop(k)
    save_json(ALIASES, db)
    _INDEX["sig"] = None
    return True


def _name_thread(handle, name: str) -> bool:
    """Write the name into the thread log so every reader (cards, the comms board, the operator) sees it."""
    try:
        from alib import QueueLock
    except Exception:  # noqa: BLE001
        QueueLock = None  # noqa: N806
    lock = QueueLock("phone_bridge") if QueueLock else None
    try:
        if lock is not None:
            with lock as ok:
                if not ok:
                    return False
                return _write_thread_name(handle, name)
        return _write_thread_name(handle, name)
    except Exception:  # noqa: BLE001
        return False


def _write_thread_name(handle, name) -> bool:
    db = load_json(THREADS, {}) or {}
    h = _thread_handle(db, handle)
    if not h:
        return False
    if db[h].get("name") == name:
        return True
    db[h]["name"] = str(name)[:80]
    save_json(THREADS, db)
    return True


def harvest(write: bool = True) -> dict:
    """Name every unnamed thread the index is sure about (>= SURE, a person, not the phone's own
    name source). Zero-LLM; a spine job and every inbound text run it. -> {named: [...], unsure: n}"""
    idx = index(fresh=True)
    named, unsure = [], 0
    for h, th in list(idx.threads.items()):
        if not isinstance(th, dict) or th.get("name"):
            continue
        r = idx.resolve(h)
        if r["kind"] in ("automated", "business", "owner"):
            continue
        if r["name"] and r["confidence"] >= SURE and not r.get("also"):
            named.append({"handle": h, "name": r["name"], "why": (r["evidence"] or [""])[0]})
            if write:
                _name_thread(h, r["name"])
        elif r["name"]:
            unsure += 1
    if named and write:
        _INDEX["sig"] = None
    return {"named": named, "unsure": unsure}


def stats() -> dict:
    idx = index(fresh=True)
    th = {h: t for h, t in idx.threads.items() if isinstance(t, dict)}
    rs = {h: idx.resolve(h) for h in th}
    return {"threads": len(th), "named_in_log": sum(1 for t in th.values() if t.get("name")),
            "sure": sum(1 for r in rs.values() if r["confidence"] >= SURE and r["kind"] not in ("automated", "business")),
            "likely": sum(1 for r in rs.values() if LIKELY <= r["confidence"] < SURE),
            "automated": sum(1 for r in rs.values() if r["kind"] in ("automated", "business")),
            "unknown": sum(1 for r in rs.values() if not r["name"] and r["kind"] not in ("automated", "business")),
            "groups": len({id(g) for g in idx._groups.values()}), "aliases": len(load_json(ALIASES, {}) or {}),
            "phone_book": idx.book_loaded, "errors": idx.errors}


# ------------------------------------------------------------------ CLI
def main() -> int:
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("who"); p.add_argument("query", nargs="+"); p.add_argument("--json", action="store_true")
    p = sub.add_parser("directory"); p.add_argument("--days", type=int, default=30)
    p = sub.add_parser("harvest"); p.add_argument("--dry", action="store_true")
    p = sub.add_parser("learn"); p.add_argument("handle"); p.add_argument("name"); p.add_argument("--relation", default="")
    p.add_argument("--kind", default=""); p.add_argument("--by", default="cli")
    p = sub.add_parser("forget"); p.add_argument("handle")
    sub.add_parser("stats")
    a = ap.parse_args()
    if a.cmd == "who":
        q = " ".join(a.query)
        if a.json:
            print(json.dumps(find(q), ensure_ascii=False, indent=1, default=str))
        else:
            print(explain(q))
    elif a.cmd == "directory":
        print(directory(a.days))
    elif a.cmd == "harvest":
        print(json.dumps(harvest(write=not a.dry), ensure_ascii=False, indent=1))
    elif a.cmd == "learn":
        print(json.dumps(learn(a.handle, a.name, a.relation, a.kind, a.by), ensure_ascii=False, indent=1))
    elif a.cmd == "forget":
        print("forgotten" if unlearn(a.handle) else "nothing to forget")
    elif a.cmd == "stats":
        print(json.dumps(stats(), ensure_ascii=False, indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main())

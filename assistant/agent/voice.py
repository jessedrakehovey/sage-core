"""voice.py - the one door between code and the owners' chat: what a person would say, the way a person says it.

the owner 2026-10-27: "Sage still sends me lots of system noise I don't want to see. Interacting with Sage should feel
exactly like interacting with a real person." In the three days before, 81 cards and 128 code-posted lines reached
the home space, and about half were the machinery talking: "Heads up: the comms probe has stopped", "Drain ran:
DRAIN ran=0 done=0", "Still waiting on you: card open 39h unanswered", "Still not built: ...", "Not done:
NOT_CONFIGURED smtp:personal (set with setup_creds.py ...)", "Text to +1555-073-4973 handed to the phone", the same
"I built a fix" twice in two threads, cards headed "Not confirmed: agent run", "A nightly self-test failed",
"Spine job cards-sweep failing". Each poster had its own words and its own idea of urgency.

Now every line code posts to Chat passes through ONE gate (chat_push.push_say / push_result / push_text,
receipts.say, routines) and every card's urgency is decided by what the card IS (cards.is_system), not by the
poster's flag:

  voice.gate(text, channel="", space="") -> str   the line as a person would say it; '' when it must not reach Chat
  voice.classify(text) -> str                      its class ('' = ordinary words)
  voice.names(text) -> str                         +1555-049-1349 -> example example IV (contacts); an unknown number is
                                                   shown as (555) 049-1349
  voice.polish(text) -> str                        ids, ticket codes, markers and Message-IDs a person never says
  voice.plain(error) -> str                        an error code in plain words
  voice.fresh(space, text) -> bool                 False when the same words went to that space in the last hour

The policy is hard, not a rule the model keeps: system talk (status, raw output, chase lines, scorecards, review
summaries, slipped builds, unconfirmed outcomes) never reaches Chat; the poster's Mission Control note is written
before the gate, so nothing is lost. Lines a person WOULD say are reworded (a send result names the person and
drops the Message-ID; a build receipt says what changed; a doer waiting on a code asks for it plainly). Everything
else passes untouched. Switches: config.json -> push.voice {system_lines: false, dedupe_minutes: 60}. Zero-LLM.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
for _p in (HERE, HERE.parent / "scripts"):
    if str(_p) not in sys.path:
        sys.path.append(str(_p))      # at the end: never shadow a same-named module (followups.py) already on the path

DEDUPE_MIN = 60
SEEN_KEEP = 400

# ------------------------------------------------------------------ the classes
# Blocked: the machinery talking. Each has a Mission Control note at its poster; none is a thing a person says.
BLOCKED = {
    "system_status": re.compile(r"^Heads up: .*(?:has stopped|is down)|is back; the gap was|^The \S+ (?:probe|spine) is back", re.I | re.S),
    "raw_output": re.compile(r"^(?:Drain ran|Sensor ran|Ran \S+):|\b(?:SPINE|DRAIN|RESULT|REFLEX) ran=|\bran=\d+ ok=\d+", re.I),
    "chase_line": re.compile(r"^(?:Still )?[Ww]aiting on you:|^Still open, asked \d+ times", re.I),
    "scorecard": re.compile(r"^Scorecard:", re.I),
    "review_summary": re.compile(r"^From my review of", re.I),
    "build_slipped": re.compile(r"^Still not built:", re.I),
    "unconfirmed": re.compile(r"^I could not confirm (?:that|the) |^Not confirmed:|^I could not confirm this landed", re.I),
    "morning_digest": re.compile(r"^Morning\. A few things I picked up yesterday", re.I),
    "operator_report": re.compile(r"^OPERATOR:|^\[operator\]", re.I),
    # the ledger gave up verifying a hand-off: nothing for a person to do (a handed text is sent in practice;
    # the owner 2026-10-27 16:55 under one of these: "Stop sending me messages like this")
    "dropped": re.compile(r"^Dropped: .*\(nobody could confirm it\)", re.I),
}

_BUILT = re.compile(r"^(?:That's built now, the one you asked for|I built a fix for this one): [\"“](?P<t>.*?)[\"”]\.?\s*"
                    r"(?:What changed: (?P<w>.*?))?(?:\s*If anything feels off.*)?$", re.S)
_BUILT_PLAIN = re.compile(r"^I built (?:that thing|a couple of things) you asked for: (?P<w>.*?)\.? It's live now; if anything feels off.*$", re.S)
_HANDED = re.compile(r"^Text to (?P<to>\S+) handed to the phone at (?P<when>[^;]+); the phone's Shortcut sends it\.?$")
_EMAILED = re.compile(r"^Sent by email to (?P<to>.+?) at (?P<when>\d{1,2}:\d\d ?[AP]M)(?: \(Message-ID [^)]*\))?(?P<rest>.*)$", re.S)
_HB = re.compile(r"^Sent in HoneyBook at (?P<when>\d{1,2}:\d\d ?[AP]M)(?: \(HoneyBook message [^)]*\))?(?:; HoneyBook logged it at [^.]*)?\.?$")
_CONFIRMED_TEXT = re.compile(r"^Confirmed at \d{1,2}:\d\d ?[AP]M: the text to (?P<to>\S+) went \(they replied, so it went\)\.?$")
_CONFIRMED = re.compile(r"^Confirmed at \d{1,2}:\d\d ?[AP]M: (?P<kind>.+?) landed \((?P<d>.*)\)\.?$", re.S)
_NOT_DONE = re.compile(r"^Not done: (?P<why>.+?)\.? The card is back (?P<where>.+)$", re.S)
_NOT_DONE2 = re.compile(r"^Not done: (?P<why>.+)$", re.S)
_CODE_WAIT = re.compile(r"^I am waiting for a verification code: (?P<step>.*?)(?:\s*It was sent to your phone or email\.)?(?:\s*Open the card on Mission Control.*)?$", re.S)
_DOER_BLOCKED = re.compile(r"^I could not finish on [\"“](?P<job>.*?)[\"”…]*: (?P<why>.*)$", re.S)
_DOER_FAILED = re.compile(r"^I couldn't finish on [\"“](?P<job>.*?)[\"”…]*: (?P<why>.*)$", re.S)
_ACKS = (
    (re.compile(r"^Handing that to Sage to work through; it comes back here with what was done\.?$"), "On it. I'll come back here with what I did."),
    (re.compile(r"^Got it, building it now with your OK\. You'll hear from me here when it lands, with an undo\.?$"), "On it. I'll tell you here when it's live."),
    (re.compile(r"^Okay, Sage is on it; anything that needs you comes back as its own card\.?$"), "On it; anything that needs you comes back here."),
    (re.compile(r"^Started it\.$"), "Started it."),
)
_ROUTINE = re.compile(r"^(?P<title>[^\n:]{3,70}?)(?::\s+|\n)(?P<body>[A-Z\"“].{19,})$", re.S)
_PHONE = re.compile(r"(?<![\w(])\+?1?\s?\(?(\d{3})\)?[\s.-]?(\d{3})[\s.-]?(\d{4})(?!\d)")
_E164 = re.compile(r"\+\d{10,15}")
_SHA_DATE = re.compile(r"^[0-9a-f]{7,40} \d{4}-\d\d-\d\d(?: \d\d:\d\d)?:\s*")
_IDS = (
    (re.compile(r"\s*\(card q[0-9a-f]{6,}\)"), ""),
    (re.compile(r"\s*\(Message-ID <?[^>)]+>?\)"), ""),
    (re.compile(r"\s*\((?:HoneyBook )?(?:message|file|ticket|outcome) [0-9a-f]{6,}(?:, [^)]*)?\)"), ""),
    (re.compile(r"\[ACTION PREVIEW\]\s*"), ""),
    (re.compile(r"\[(?:EMAIL|TEXT|HB) PREVIEW\]\s*"), ""),
    (re.compile(r"\s?\btk-[0-9a-f]{8,}\b"), ""),
    (re.compile(r"\b(?:qid|ticket id|outcome id)\s*[:=]?\s*q?[0-9a-f]{8,}\b", re.I), ""),
    (re.compile(r"\s*\(\s*\)"), ""),
    (re.compile(r"[ \t]{2,}"), " "),
)
_CODES = (
    (re.compile(r"NOT_CONFIGURED smtp:(?P<a>\w+)(?:\s*\(set with [^)]*\))?", re.I), lambda m: f"the {m.group('a')} mail account has no send login yet (Mission Control > Setup fixes that)"),
    (re.compile(r"NOT_CONFIGURED (?P<a>[\w:-]+)(?:\s*\(set with [^)]*\))?", re.I), lambda m: f"{m.group('a').replace(':', ' ')} isn't set up yet (Mission Control > Setup)"),
    (re.compile(r"DELIVERY UNVERIFIED:?", re.I), lambda m: "I couldn't confirm it was delivered;"),
    (re.compile(r"PERMANENT:\s*", re.I), lambda m: ""),
    (re.compile(r"exec exception \w+ tk-[0-9a-f]+:?\s*", re.I), lambda m: "it hit an error: "),
    (re.compile(r"\bHTTPError: HTTP Error (\d+)[^.;]*", re.I), lambda m: f"the service answered with an error ({m.group(1)})"),
    (re.compile(r"\b(?:TimeoutError|timed out|timeout)\b[^.;]*", re.I), lambda m: "it stopped answering in time"),
    (re.compile(r"\b[A-Z][A-Z_]{4,}\b(?::\S*)?"), lambda m: ""),
    (re.compile(r"\(set with [^)]*\)"), lambda m: ""),
    (re.compile(r"\b[\w./\\-]+\.py\b( \w+)*"), lambda m: ""),
)


# ------------------------------------------------------------------ config
def _cfg() -> dict:
    try:
        from alib import config
        v = (config().get("push") or {}).get("voice") or {}
        return v if isinstance(v, dict) else {}
    except Exception:  # noqa: BLE001
        return {}


def system_lines_on() -> bool:
    """True only when an owner flipped push.voice.system_lines on (or a test asks for the old behaviour)."""
    e = os.environ.get("ASSISTANT_VOICE_SYSTEM")
    if e is not None:
        return e.strip().lower() in ("1", "on", "chat", "true")
    return bool(_cfg().get("system_lines"))


def _dedupe_s() -> float:
    try:
        return max(0.0, float(_cfg().get("dedupe_minutes", DEDUPE_MIN))) * 60
    except (TypeError, ValueError):
        return DEDUPE_MIN * 60


# ------------------------------------------------------------------ classify
def classify(text: str) -> str:
    t = str(text or "").strip()
    if not t:
        return ""
    for cls, rx in BLOCKED.items():
        if rx.search(t):
            return cls
    if _BUILT.match(t) or _BUILT_PLAIN.match(t):
        return "build_landed"
    if _HANDED.match(t) or _EMAILED.match(t) or _HB.match(t) or _CONFIRMED_TEXT.match(t) or _CONFIRMED.match(t):
        return "send_result"
    if _NOT_DONE.match(t) or _NOT_DONE2.match(t):
        return "send_failed"
    if _CODE_WAIT.match(t):
        return "doer_code"
    if _DOER_BLOCKED.match(t) or _DOER_FAILED.match(t):
        return "doer_stopped"
    if any(rx.match(t) for rx, _ in _ACKS):
        return "ack"
    return ""


def blocked(text: str) -> bool:
    return classify(text) in BLOCKED and not system_lines_on()


# ------------------------------------------------------------------ names, ids, codes
_NAME_CACHE: dict = {}


def person(handle: str) -> str:
    """The contact's name for a phone number or email, '' when unknown. Cached per process."""
    h = str(handle or "").strip()
    if not h:
        return ""
    if h in _NAME_CACHE:
        return _NAME_CACHE[h]
    name = ""
    try:
        import contacts
        name = str(contacts.name_for(h) or "")
    except Exception:  # noqa: BLE001
        name = ""
    _NAME_CACHE[h] = name
    return name


def pretty_number(num: str) -> str:
    d = re.sub(r"\D", "", str(num or ""))
    if len(d) == 11 and d.startswith("1"):
        d = d[1:]
    if len(d) == 10:
        return f"({d[:3]}) {d[3:6]}-{d[6:]}"
    return str(num or "")


def names(text: str) -> str:
    """Every +1XXXXXXXXXX (and +<country> numbers) becomes the person's name when the contacts know it, else a
    readable number. A recipient line "To +1805...:" reads "To example example IV:"."""
    t = str(text or "")
    if not t:
        return t

    def sub(m):
        raw = m.group(0)
        n = person(raw)
        return n if n else pretty_number(raw)
    return _E164.sub(sub, t)


def polish(text: str) -> str:
    """Ids, ticket codes, preview markers and Message-IDs out of a line meant for a person."""
    t = str(text or "")
    for rx, rep in _IDS:
        t = rx.sub(rep, t)
    return t.strip()


def plain(error: str) -> str:
    """An error code in plain words: NOT_CONFIGURED smtp:personal (set with setup_creds.py mail personal) ->
    the personal mail account has no send login yet (Mission Control > Setup fixes that)."""
    s = " ".join(str(error or "").split())
    for rx, fn in _CODES:
        s = rx.sub(fn, s)
    s = re.sub(r"\s+", " ", s).strip(" :;,.-")
    s = re.sub(r"\(\s*\)", "", s).strip(" :;,.-")
    return s or "it hit an error"


def _first_clause(s: str, n: int = 60) -> str:
    s = " ".join(str(s or "").split()).strip(" \"“”…")
    cut = re.split(r"[,:;(]| from | into | for | on | at | so | with | and ", s, maxsplit=1)[0].strip()
    if len(cut) > n:
        cut = cut[:n].rsplit(" ", 1)[0]
    return cut.rstrip(" .…")


# ------------------------------------------------------------------ rewording
def _built(m) -> str:
    t = str(m.group("t") or "").strip()
    w = " ".join(str(m.group("w") or "").split()).strip()
    w = _SHA_DATE.sub("", w)
    if (not w or w.lower().startswith("build request") or len(w) < 25 or t.lower().startswith(w.lower()[:30])
            or w.lower().startswith("built by hand")):
        return f"Done, that's built: \"{t}\". Say if anything feels off and I'll undo it."
    w = w[0].upper() + w[1:]
    return f"Done: {w.rstrip('.')}. Say if anything feels off and I'll undo it."


def reword(text: str, channel: str = "") -> str:
    """A line a person would say, in a person's words. Unchanged when no pattern fits."""
    t = str(text or "").strip()
    if not t:
        return ""
    m = _BUILT.match(t)
    if m:
        return _built(m)
    m = _BUILT_PLAIN.match(t)
    if m:
        w = str(m.group("w") or "").strip()
        return f"Done: {w[0].upper() + w[1:]}. Say if anything feels off and I'll undo it." if w else t
    m = _HANDED.match(t)
    if m:
        to, when = m.group("to"), m.group("when").strip()
        who = person(to) or pretty_number(to)
        return f"Texted {who} at {when}, from your phone."
    m = _EMAILED.match(t)
    if m:
        to, when, rest = m.group("to").strip(), m.group("when"), " ".join(str(m.group("rest") or "").split())
        who = ", ".join((person(x.strip()) or x.strip()) for x in to.split(","))
        tail = ""
        if "NEW thread" in rest:
            tail = ", though it went as a new thread rather than a reply"
        elif "could not confirm it landed" in rest:
            tail = "; I'm not sure it landed in the same thread, so a glance at Sent mail would tell"
        return f"Emailed {who} at {when}{tail}."
    m = _HB.match(t)
    if m:
        return f"Sent in HoneyBook at {m.group('when')}."
    m = _CONFIRMED_TEXT.match(t)
    if m:
        who = person(m.group("to")) or pretty_number(m.group("to"))
        return f"{who} replied, so that text went."
    m = _CONFIRMED.match(t)
    if m:
        return f"Confirmed: the {m.group('kind').strip()} landed."
    m = _NOT_DONE.match(t)
    if m:
        where = m.group("where").strip()
        where = "in Monday's approvals" if "next week" in where else "for another tap"
        return f"That didn't go: {plain(m.group('why'))}. The card is back {where}."
    m = _NOT_DONE2.match(t)
    if m:
        return f"That didn't go: {plain(m.group('why'))}."
    m = _CODE_WAIT.match(t)
    if m:
        step = " ".join(str(m.group("step") or "").split()).strip()
        step = re.sub(r"^(?:Type|Enter|Give me|Paste) (?:in )?(?:the |a )?", "", step)
        step = step[0].lower() + step[1:] if step else "the code"
        return f"I need the {step.rstrip('.')}. Reply with it here and I'll carry on."
    m = _DOER_BLOCKED.match(t) or _DOER_FAILED.match(t)
    if m:
        job = _first_clause(m.group("job"))
        why = " ".join(str(m.group("why") or "").split())
        return f"I couldn't finish \"{job}\": {why}" if job else f"I couldn't finish that one: {why}"
    for rx, rep in _ACKS:
        if rx.match(t):
            return rep
    if channel == "routine":
        m = _ROUTINE.match(t)
        if m and m.group("title").strip().lower() not in m.group("body").lower():
            body = m.group("body").strip()
            return body
        if m:
            return m.group("body").strip()
    return t


# ------------------------------------------------------------------ dedupe
def _seen_path() -> Path:
    d = os.environ.get("AGENT_DATA")
    return (Path(d) if d else HERE.parent / "data" / "agent") / "voice_seen.json"


def _key(space: str, text: str) -> str:
    norm = re.sub(r"\d{1,2}:\d\d ?[AP]M|\s+", " ", str(text or "").lower()).strip()
    return hashlib.sha1((str(space or "*") + "|" + norm).encode("utf-8")).hexdigest()[:20]


def fresh(space: str, text: str, now: float | None = None) -> bool:
    """True the first time these words go to this space within the dedupe window; records the send."""
    win = _dedupe_s()
    if win <= 0 or not str(text or "").strip():
        return True
    now = now or time.time()
    p = _seen_path()
    try:
        d = json.loads(p.read_text(encoding="utf-8")) if p.exists() else {}
        if not isinstance(d, dict):
            d = {}
    except (OSError, ValueError):
        d = {}
    k = _key(space, text)
    if now - float(d.get(k) or 0) < win:
        return False
    d[k] = now
    if len(d) > SEEN_KEEP:
        for old in sorted(d, key=lambda x: d[x])[:len(d) - SEEN_KEEP]:
            d.pop(old, None)
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(d), encoding="utf-8")
    except OSError:
        pass
    return True


# ------------------------------------------------------------------ the gate
def gate(text: str, channel: str = "", space: str = "") -> str:
    """The line as it may reach Chat: '' for system talk (its Mission Control note is already written), otherwise
    reworded, with names for numbers and no ids. `space` adds the one-hour dedupe (the same words twice)."""
    t = str(text or "").strip()
    if not t:
        return ""
    cls = classify(t)
    if cls in BLOCKED and not system_lines_on():
        _ledger("quiet", cls, t)
        return ""
    # a class an owner switched off ("stop sending me messages like this": reflex.mute_from_thread, or a rule naming
    # the class) is enforced HERE, at the one door, whatever code posts it (2026-10-27)
    try:
        import reflex
        rc = reflex.line_class(t)
        if rc and not reflex.allowed(rc):
            _ledger("muted", rc, t)
            return ""
    except Exception:  # noqa: BLE001
        pass
    out = polish(names(reword(t, channel)))
    if space and not fresh(space, out):
        _ledger("dedupe", cls or "line", out)
        return ""
    return out


def _ledger(kind: str, cls: str, text: str) -> None:
    try:
        import core
        core.append(core.DATA / "voice.jsonl", {"ts": core.stamp(), "kind": kind, "cls": cls, "text": str(text)[:300]})
    except Exception:  # noqa: BLE001
        pass


def recent(hours: float = 24.0) -> dict:
    """{kind: count} of what the gate held back lately (for /reflex and the scorecard)."""
    out: dict = {}
    try:
        import core
        cut = time.time() - hours * 3600
        for r in core.rows(core.DATA / "voice.jsonl")[-2000:]:
            ts = time.mktime(time.strptime(str(r.get("ts") or "")[:16], "%Y-%m-%d %H:%M")) if r.get("ts") else 0
            if ts >= cut:
                out[f"{r.get('kind')}:{r.get('cls')}"] = out.get(f"{r.get('kind')}:{r.get('cls')}", 0) + 1
    except Exception:  # noqa: BLE001
        pass
    return out


if __name__ == "__main__":
    args = sys.argv[1:]
    if args and args[0] == "try":
        line = " ".join(args[1:])
        print(f"class: {classify(line) or '-'}")
        print(f"chat:  {gate(line) or '(held back: Mission Control only)'}")
    elif args and args[0] == "recent":
        print(json.dumps(recent(), indent=1))
    else:
        print("voice.py try <line> | recent")

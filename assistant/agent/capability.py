"""capability.py - the agent grows a new ability the moment it is asked for one it does not have. Built 2026-10-25.

the owner, 2026-10-25: "When I tell it to do something new it can't do yet it should rapidly adapt to fulfil that
request ... if there are any roadblocks they should be solvable in chat. I should never have to open Claude
and say 'Sage couldn't do this, fix it' like I have been doing all week." All week the loop was a human one:
Sage (or the doer) said "I can't", an owner noticed, opened a coding session, and that session read the
transcript, found the gap and built the fix. This module is that loop as code, zero-LLM, around the same
read-only brain:

  gap      a line that says the system could not do what an owner asked. Found at three seams, by regex:
             - the brain's own reply ("I can't ... from here", "outside what I may do", "nothing like that
               exists yet"), when no deed was recorded for it;
             - a doer report ("I could not finish ... : that is outside what I may do / route it through ...");
             - an owner's complaint right after a reply ("you fail again", "it was never sent", "just fix it",
               "still isn't", "I don't want to see messages like this").
           A line whose blocker is the owner's own (a sign-in, a password, a fact only they hold, a tap on a
           card, a connection that is down) is NOT a gap: that is the portal, said once (prompt rung 6).
  build    every gap becomes ONE wiki `## Build request` at once (the same request an owner files in chat),
           carrying the ask verbatim, what was answered and an acceptance line; de-duplicated against open
           gaps and open build notes; `builder.kick(force=True, first=...)` starts it that minute. The builder's
           fences are unchanged: a protected file still needs the owner's tap on its card, in chat.
  replay   when the build lands, code hands the ORIGINAL ask back to the brain in the place it was asked
           (marked REPLAY), so the thing actually gets done; the owner never has to ask twice. A replay that
           still ends in a "can't" files a second build with the first one's outcome; after MAX_ATTEMPTS the
           owner gets ONE card in that thread: Try it another way / Drop it.
  prefer   a correction or a standing preference in an owner's line ("never use semicolons in texts", "from
           now on ...", "I only want ...") that the turn did not `learn` is saved as a rule by code the same
           turn (core.rule_add: ledgered, undoable, the protected-core refusals hold).

Nothing here sends, taps a card, or edits code: it files requests the builder builds under its fences and
puts the owner's decisions on cards in chat. State: data/agent/capability.json. Generic: no organization,
person or service is named here; names come from profile.json / people.json.

  python assistant/agent/capability.py status
  python assistant/agent/capability.py scan [--dry-run]      # the transcript seams, from the cursor
  python assistant/agent/capability.py replay [--dry-run]
"""
from __future__ import annotations

import argparse
import json
import re
import secrets
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import core  # noqa: E402

STORE = core.DATA / "capability.json"
MAX_ATTEMPTS = 2            # builds for one ask before the owner is asked how to go on
FILED_PER_DAY = 8           # auto-filed build requests a day (an owner's own `## Build request` note never counts)
REPLAY_AFTER_S = 150        # after a build lands: the bridge restarts on new agent code first
REPLAY_MAX_AGE_H = 72       # an ask older than this is not replayed on its own
FOLD_DAYS = 7               # a second "can't" on the same ask within this window is the same gap
FOOTER = "<!-- capability:"

# ------------------------------------------------------------------ the regexes (deliberately narrow)
# the brain or the doer saying it could not do the thing
_CANT = re.compile(
    r"\bI (?:still |just |simply |really |also |truly )?(?:can'?t|cannot|couldn'?t|could not|am not able to|'m not able to|am unable to|have no way to|don'?t have a way to)\b|"
    r"\b(?:there'?s|there is) no way for me to\b|\bnot something I can\b|\boutside what I (?:may|can) do\b|\bnothing like that exists\b|"
    r"\b(?:isn'?t|is not|not) built yet\b|\bfailed twice with an internal error\b", re.I)
# a first-person can't that is about an ABILITY, not knowledge: "from here", "myself", a limit it cannot get around
_STRONG = re.compile(r"from here|\bmyself\b|from this (?:chat|turn|side)|get around|no way for me|outside what I|nothing like that exists|"
                     r"not built yet|not something I can|internal error|\bno (?:tool|route|way) (?:for|to)\b", re.I)
# a first-person can't about what it knows or can see: a lookup miss or a judgement call, never a build
_FACT_VERB = re.compile(r"\bI (?:still |just |simply |really |also |truly )?(?:can'?t|cannot|couldn'?t|could not) (?:see|tell|say|confirm|find|know|judge|be sure|vouch|speak to|read|"
                        r"put a|give you a|promise|guess|rank|verify)\b", re.I)
# ... but the blocker is the owner's own: not a gap, the chat is their portal (said once)
_OWNER_ONLY = {
    "protected": re.compile(r"\breset\b.*\bpassword\b|\b(?:pay|payment|transfer|purchase|buy)\b|\bdelete\b|\bfootage\b|"
                            r"never change|not allowed to|\bprice\b.*\bpromise\b|\bcredential|permission or a secret|protected core", re.I),
    "signin": re.compile(r"\bsign(?:ed)?[- ]?(?:in|out)\b|\blog(?:ged)? ?in\b|\bpassword\b|\bpasscode\b|\bverification code\b|"
                         r"\b2fa\b|two-factor|setup page|\bsign-in\b", re.I),
    "tap": re.compile(r"\btap (?:send|approve|skip|hold|the|that|this|on|it so)\b|\bplease tap\b|needs your (?:ok|yes|approval|say)|only you may approve|"
                      r"card is below|answer cards for you|\bfor your tap\b|\byour tap on\b", re.I),
    "connection": re.compile(r"connection (?:is |was )?(?:down|dead|off)|\b(?:not|isn'?t) connected\b|\btimed? ?out\b|\btimeout\b|"
                             r"failed to connect|\bat (?:its|the|your) (?:usage|session) limit\b|session limit", re.I),
    "fact": re.compile(r"\btell me\b|what'?s (?:his|her|their|the) (?:number|email|address|name)|\bpaste\b|\bsend me\b|"
                       r"\b(?:couldn'?t|can'?t|could not|cannot) find\b|\bno record\b|\bnothing (?:in|from) (?:the|either|both|your)\b|"
                       r"\bisn'?t in (?:the|his|her|your)\b|\bnot in (?:the records|the wiki|the tracker|the records)\b|\bdon'?t have the file\b", re.I),
}
_BLOCKED_LINE = re.compile(r"(?im)^\s*-?\s*blocked because:?\s*(.+)$")
_NEED_LINE = re.compile(r"(?im)^\s*-?\s*need from you:?\s*(.+)$")
# an owner saying the last thing did not happen (the loop an owner ran by hand all week)
_FRUSTRATION = re.compile(
    r"^(?:you fail(?:ed)?\b|fail again|stop trying and just fix|just fix it\b|fix it\b|fix this\b|it was never sent\b|"
    r"(?:it |that |this )?never (?:got|went|sent|posted|happened|came)\b|you never\b|i never got\b|"
    r"why (?:are|is|did|didn'?t|isn'?t|aren'?t|wasn'?t|won'?t|can'?t)\b|still (?:isn'?t|hasn'?t|not|no|didn'?t|doesn'?t|can'?t)\b|"
    r"i don'?t want to see messages like this|(?:that|this|it) (?:didn'?t|doesn'?t|does not|did not|isn'?t) work|not working\b|"
    r"you (?:told|said) .{0,60}\bbut\b|where is the card|wrong again|you should (?:have|be able to)\b|you have an? \w+ switch)", re.I)
_PREF = re.compile(r"^(?:please\s+|ok\s+|okay\s+)?(?:never|always|from now on|every time|each time|stop (?:doing|saying|sending|posting|asking|making|telling|using)|"
                   r"i don'?t want to see|i only want|no more|quit (?:doing|saying|sending|posting|asking))\b", re.I)
# an owner asking about a build (why, when, is it done): answered from the build records, never a gap (2026-10-25)
_BUILD_QUESTION = re.compile(
    r"^(?:but |so |and |ok,? |okay,? )?(?:why|when|is|isn'?t|are|aren'?t|did|didn'?t|does|doesn'?t|has|hasn'?t|have|haven'?t|was|wasn'?t|"
    r"what(?:'?s| is| happened)|where(?:'?s| is)|how (?:long|come|far|is)|any (?:news|update))\b.{0,160}?"
    r"\b(?:built|build|building|builder|landed|shipped|done|finished|ready|fixed)\b", re.I)
_ONE_OFF = re.compile(r"\b(?:yet|right now|today|tonight|tomorrow|this one|this time|him|her|them|that one)\b|\?\s*$", re.I)
_DOER = re.compile(r"^I could not finish(?: on (?P<job>\".*?\"))?:\s*(?P<why>.+)$", re.S)
_DOER_POLICY = re.compile(r"outside what I may do|\bnever:|connector job|route it through|exec kinds|no tool|not a browser one|"
                          r"isn'?t something the doer|cannot be changed by the doer|can'?t be changed by the doer|off limits", re.I)


# ------------------------------------------------------------------ store
def _load() -> dict:
    d = core.load(STORE, {})
    if not isinstance(d, dict):
        d = {}
    d.setdefault("gaps", {})
    d.setdefault("replays", [])
    d.setdefault("cursor", -1)
    d.setdefault("_filed", {})
    return d


def _save(d: dict) -> None:
    core.save(STORE, d)


def _owners_lower() -> set:
    return {str(v.get("name") or "").lower() for v in (core.owners() or core.people().get("users") or {}).values()}


def _short(s: str, n: int = 200) -> str:
    s = re.sub(r"\s+", " ", str(s or "")).strip()
    return s if len(s) <= n else s[:n].rsplit(" ", 1)[0] + "..."


def _terms(s: str) -> set:
    return {w[:6] for w in re.findall(r"[a-z0-9]+", str(s or "").lower()) if len(w) >= 4}


def same_ask(a: str, b: str) -> bool:
    """The same ask in other words (content stems; the pending.py tuning)."""
    x, y = _terms(a), _terms(b)
    if len(x) < 3 or len(y) < 3:
        return core._norm(a) == core._norm(b)
    inter = len(x & y)
    return inter / len(x | y) >= 0.4 or inter / min(len(x), len(y)) >= 0.7


# ------------------------------------------------------------------ classify
def classify(reply: str) -> tuple:
    """-> (cls, quote). cls: 'capability' (a gap to build), one of the owner-only blockers ('signin', 'tap',
    'connection', 'fact', 'protected'), or 'none'. The quote is the sentence that decided it."""
    text = str(reply or "")
    if not text:
        return "none", ""
    m = _BLOCKED_LINE.search(text)
    if m:
        line = m.group(1).strip()
        need = _NEED_LINE.search(text)
        probe = line + (" " + need.group(1) if need else "")
        for cls, rx in _OWNER_ONLY.items():
            if rx.search(probe):
                return cls, _short(line)
        return "capability", _short(line)
    found = "none", ""
    for sent in [s.strip() for s in re.split(r"(?<=[.!?])\s+|\n+", text) if s.strip()]:
        if not _CANT.search(sent):
            continue
        if re.search(r"\b(?:if|when|once|unless)\b", sent[:12], re.I):
            continue                                         # conditional, not a report
        cls = next((c for c, rx in _OWNER_ONLY.items() if rx.search(sent)), "")
        if not cls and _FACT_VERB.search(sent) and not _STRONG.search(sent):
            cls = "fact"
        if not cls:
            return "capability", _short(sent)                # the first real ability gap decides
        if found[0] == "none":
            found = cls, _short(sent)                        # an owner-only blocker, unless a later sentence is an ability gap
    return found


def about_a_build(text: str) -> bool:
    """An owner's question about an existing build or its status ("Why is it not built?", "is it done?").
    The brain answers it from BUILDS; it is never a missing ability, so it never files a build of its own."""
    t = str(text or "").strip()
    return bool(t) and len(t) <= 240 and bool(_BUILD_QUESTION.match(t))


def _deed_kinds(applied: list) -> set:
    return {str((a or {}).get("kind") or "") for a in (applied or [])}


# ------------------------------------------------------------------ gaps
def _find(d: dict, person: str, ask: str, days: int = FOLD_DAYS) -> dict | None:
    cut = time.time() - days * 86400
    best = None
    for g in d["gaps"].values():
        if float(g.get("ts") or 0) < cut or g.get("status") in ("dropped",):
            continue
        if g.get("person") == person and same_ask(g.get("ask", ""), ask):
            if not best or float(g.get("ts") or 0) > float(best.get("ts") or 0):
                best = g
    return best


def _open_build_keys() -> list:
    try:
        import builder
        return [(r["key"], r.get("text") or r.get("title") or "") for r in builder.collect()]
    except Exception:  # noqa: BLE001
        return []


def _filed_today(d: dict) -> int:
    return int((d.get("_filed") or {}).get(time.strftime("%Y-%m-%d"), 0))


def _count_filed(d: dict) -> None:
    day = time.strftime("%Y-%m-%d")
    d["_filed"] = {day: _filed_today(d) + 1}


def open_gap(d: dict, cls: str, ask: str, quote: str, person: str, ctx: dict, source: str) -> dict:
    gid = "g-" + secrets.token_hex(3)
    g = {"id": gid, "ts": time.time(), "when": core.stamp(), "cls": cls, "source": source, "ask": _short(ask, 600),
         "quote": _short(quote, 400), "person": person, "convo": str(ctx.get("convo") or ""), "thread": str(ctx.get("thread") or ""),
         "space": str(ctx.get("space") or ""), "status": "open", "attempts": 0, "builds": []}
    d["gaps"][gid] = g
    return g


def _lane(ask: str, ctx: dict) -> str:
    try:
        import intents
        return intents._guess_lane(ask) or "personal"
    except Exception:  # noqa: BLE001
        return str(ctx.get("lane") or "personal")


def footer(g: dict) -> str:
    meta = {"gap": g["id"], "convo": g.get("convo", ""), "thread": g.get("thread", ""), "space": g.get("space", ""),
            "person": g.get("person", ""), "ask": _short(g.get("ask", ""), 500)}
    return f"\n\n{FOOTER} {json.dumps(meta, ensure_ascii=False)} -->\n"


def parse_footer(text: str) -> dict:
    m = re.search(re.escape(FOOTER) + r"\s*(\{.*?\})\s*-->", str(text or ""), re.S)
    if not m:
        return {}
    try:
        d = json.loads(m.group(1))
        return d if isinstance(d, dict) else {}
    except ValueError:
        return {}


def build_body(g: dict, who: str, previous: str = "") -> str:
    """The build request the builder reads: the ask verbatim is the acceptance test."""
    what = {"capability": "do what the ask below says, end to end, with the system's own tools (a deed, an executor kind, "
                          "a doer skill or tool, a connector action, a sensor); add whatever is missing",
            "regression": "make the ask below actually happen: the owner says it did not",
            "doer": "give the doer (or an executor kind) a route for the job below instead of a refusal"}.get(g.get("cls"), "close the gap below")
    lines = ["## Build request", "",
             f"**Ask ({who}, {g.get('when', '')}, in chat):** \"{g.get('ask', '')}\"", "",
             f"**What was answered:** \"{g.get('quote', '')}\"", "",
             f"**What to build:** {what}. Smallest change that makes the ask above succeed when it is asked again; "
             "if the capability exists and only the brain's prompt or a rung is missing, fix that instead.", "",
             "**Acceptance:** when the ask above is replayed to the brain, it completes it with a deed (`task`, `do`, `event`, "
             "`job`, an executor kind, a doer run) and writes no line like the one answered above; add a test that encodes this. "
             "If a step is truly an owner's own (a password, a tap, a fact only they have), name exactly that step in the summary.", ""]
    if previous:
        lines += [f"**Previous build:** {previous}", ""]
    lines += [f"**Evidence:** transcript {g.get('when', '')}, {g.get('source', 'chat')}; gap {g['id']}"]
    return "\n".join(lines)


def file_build(d: dict, g: dict, by: str = "", previous: str = "", kick: bool = True) -> str:
    """ONE build request for the gap -> the note's path ('' when folded into an open request or capped)."""
    ask = g.get("ask", "")
    for key, text in _open_build_keys():
        if same_ask(text, ask) or same_ask(key, ask):
            g.setdefault("builds", []).append(key)
            g["status"], g["build"] = "building", key
            return key                                       # the owner (or the dream) already filed this one
    if _filed_today(d) >= FILED_PER_DAY and not previous:
        g["status"], g["note"] = "capped", f"{FILED_PER_DAY} auto-filed builds today already"
        return ""
    person = core.person_for(handle=g.get("person") or "") or {}
    who = str(person.get("name") or g.get("person") or "an owner")
    prefix = {"regression": "Fix", "doer": "Route"}.get(str(g.get("cls")), "Build request")
    title = f"{prefix} {_short(ask, 70)}".rstrip(".")
    import wiki_writer
    x = wiki_writer.write({"title": title[:90], "type": "workflow", "lane": _lane(ask, g),
                           "body": build_body(g, who, previous) + footer(g)}, by=by or g.get("person", ""))
    if x.get("status") not in ("applied", "appended"):
        g["status"], g["note"] = "unfiled", str(x.get("reason") or "")[:200]
        return ""
    key = str(x.get("path") or "")
    g.setdefault("builds", []).append(key)
    g["status"], g["build"], g["attempts"] = "building", key, int(g.get("attempts", 0)) + 1
    _count_filed(d)
    try:
        import commitments
        commitments.register("build", title, key, person or {"key": g.get("person", "")}, {"convo": g.get("convo", ""), "thread": g.get("thread", "")})
    except Exception:  # noqa: BLE001
        pass
    core.log_change("capability_build", f"gap {g['id']} ({g.get('cls')}): build request filed {key}", {"op": "none"}, by or "code")
    if kick:
        try:
            import builder
            builder.kick(force=True, first=key)
        except Exception:  # noqa: BLE001
            pass
    return key


# ------------------------------------------------------------------ seam 1: the brain's own reply
LINE_BUILDING = "That piece isn't something I can do yet, so I'm building it now; I'll pick this up myself here once it's in."
LINE_AGAIN = "That still didn't take, so I'm building it a different way; I'll finish this here when it lands."


def after_turn(person: dict, text: str, reply: str, applied: list, ctx: dict) -> str:
    """Called by the bridge after actions.apply. A reply that says it could not do the ask, with no build or
    hand-off behind it, opens a gap and files its build. -> one sentence to add under the reply, or ''."""
    if not person or not core.is_owner(person) or not str(reply or "").strip():
        return ""
    if about_a_build(text) and not ctx.get("replay"):
        return ""                                            # a question about a build is answered, never built
    kinds = _deed_kinds(applied)
    if "blocked" in kinds:
        return ""                                            # the brain named its blockers itself; actions.apply acted on them
    if "build" in kinds:
        # the brain filed the request itself: the gap exists for the replay, nothing more to file
        d = _load()
        g = _find(d, person.get("key", ""), text)
        if not g:
            g = open_gap(d, "asked", text, "", person.get("key", ""), ctx, "chat")
        for a in applied or []:
            if a.get("kind") == "build" and a.get("ref"):
                g.setdefault("builds", []).append(str(a["ref"]))
                g["status"], g["build"] = "building", str(a["ref"])
        _save(d)
        return ""
    cls, quote = classify(reply)
    replayed = bool(ctx.get("replay"))
    if cls != "capability" or (kinds & {"task", "do", "card", "job"} and not replayed):
        # the ask went somewhere real, or the blocker is the owner's own (the portal). A replayed ask that
        # now took a route closes its gap.
        if replayed:
            d = _load()
            g = d["gaps"].get(str(ctx.get("replay")))
            if g:
                g["status"], g["done"] = "done", core.stamp()
                _save(d)
        return ""
    d = _load()
    g = d["gaps"].get(str(ctx.get("replay"))) if replayed else None
    g = g or _find(d, person.get("key", ""), text)
    if g and g.get("status") in ("building", "capped", "unfiled") and not replayed:
        _save(d)
        return ""                                            # already being built: the receipt in the thread says so
    if g and (replayed or g.get("status") == "built"):
        g["quote"] = _short(quote, 400)
        if int(g.get("attempts", 0)) >= MAX_ATTEMPTS:
            stuck_card(d, g, quote)
            _save(d)
            return ""
        prev = f"{g.get('build', '')} landed and the ask still failed; the brain then answered: \"{_short(quote, 220)}\""
        key = file_build(d, g, by=person.get("key", ""), previous=prev)
        _save(d)
        return LINE_AGAIN if key else ""
    g = g or open_gap(d, "capability", text, quote, person.get("key", ""), ctx, "chat")
    key = file_build(d, g, by=person.get("key", ""))
    _save(d)
    return LINE_BUILDING if key and key == g.get("build") and g.get("attempts", 0) == 1 else ""


def from_blocked(b: dict, person: dict, ctx: dict) -> dict:
    """The brain's own `blocked` deed (intents validated it): kind capability -> a gap and its build now;
    an owner-only kind -> a gap recorded as the portal, nothing filed. -> {"status", "build", "gap"}."""
    kind = str(b.get("kind") or "").lower()
    what, why = _short(b.get("what", ""), 600), _short(b.get("why", ""), 400)
    ask = str(ctx.get("text") or "") or what
    if about_a_build(str(ctx.get("text") or "")) and not ctx.get("replay"):
        return {"status": "none"}                            # the owner asked about a build: an answer, not a new one
    d = _load()
    g = _find(d, person.get("key", ""), what)
    if kind != "capability":
        if not g:
            g = open_gap(d, kind, what, why, person.get("key", ""), ctx, "blocked deed")
            g["status"] = "portal"
        _save(d)
        return {"status": "portal", "gap": g["id"]}
    replayed = str(ctx.get("replay") or "")
    if not g and replayed:
        g = d["gaps"].get(replayed)                          # the replayed ask's own gap, whatever words the step uses now
    if g and g.get("status") in ("building", "capped", "unfiled") and not replayed:
        _save(d)
        return {"status": "building", "gap": g["id"], "build": g.get("build", "")}
    if g and (replayed or g.get("status") in ("built", "replayed")):
        g["quote"] = why
        if int(g.get("attempts", 0)) >= MAX_ATTEMPTS:
            stuck_card(d, g, why)
            _save(d)
            return {"status": "stuck", "gap": g["id"]}
        key = file_build(d, g, by=person.get("key", ""), previous=f"{g.get('build', '')} landed and the step still failed: \"{why}\"")
        _save(d)
        return {"status": "refiled", "gap": g["id"], "build": key}
    g = g or open_gap(d, "capability", what, why, person.get("key", ""), ctx, "blocked deed")
    g["ask_full"] = _short(ask, 600)
    key = file_build(d, g, by=person.get("key", ""))
    _save(d)
    return {"status": "filed" if key else g.get("status", "unfiled"), "gap": g["id"], "build": key}


def correction(person: dict, text: str, sug: dict | None) -> dict:
    """A standing preference in an owner's line that the turn did not `learn` becomes a rule by code."""
    if not person or not core.is_owner(person) or (sug or {}).get("learn"):
        return {"status": "none"}
    first = next((s.strip() for s in re.split(r"(?<=[.!?])\s+|\n+", str(text or "")) if s.strip()), "")
    if not first or not _PREF.match(first) or _ONE_OFF.search(first) or not 15 <= len(first) <= 280:
        return {"status": "none"}
    try:
        import claims
        if claims.asked_for_message(first):
            return {"status": "none"}
    except Exception:  # noqa: BLE001
        pass
    rule = re.sub(r"^(?:please|ok|okay)\s+", "", first, flags=re.I).strip().rstrip(".!")
    rule = rule[0].upper() + rule[1:]
    res = core.rule_add(rule, why=f"{person.get('name', 'owner')} in chat, kept by code the same turn", by=person.get("key", ""))
    return res


# ------------------------------------------------------------------ seam 2 + 3: the transcript (doer reports, owner complaints)
def _rows():
    return core.rows(core.TRANSCRIPT)


def scan(dry: bool = False, rows: list | None = None) -> list:
    """Rows since the cursor: a doer refusal on policy -> a gap + build; an owner's complaint right after the
    agent's reply -> a 'regression' gap + build carrying the ask and the reply. The first run starts at the end
    of the transcript (nothing is back-filled). -> what was filed."""
    d = _load()
    rows = rows if rows is not None else _rows()
    cur = int(d.get("cursor", -1))
    if cur < 0:
        d["cursor"] = len(rows)
        if not dry:
            _save(d)
        return []
    out = []
    owners = _owners_lower()
    agent = str(core.profile().get("name") or "").lower()
    for i in range(cur, len(rows)):
        r = rows[i] or {}
        who, text, ch = str(r.get("who") or ""), str(r.get("text") or ""), str(r.get("channel") or "")
        ctx = {"convo": r.get("convo", ""), "thread": r.get("thread", ""), "space": ""}
        if ch == "doer" and text.startswith("I could not finish"):
            m = _DOER.match(text.strip())
            why = (m.group("why") if m else text)
            if m and _DOER_POLICY.search(why) and not any(rx.search(why) for k, rx in _OWNER_ONLY.items() if k != "protected"):
                job = (m.group("job") or "").strip('"') or _short(why, 120)
                pk = core.first_owner()
                g = _find(d, pk, job)
                if not g:
                    g = open_gap(d, "doer", job, why, pk, ctx, "doer")
                    key = "" if dry else file_build(d, g, by=pk)
                    out.append({"gap": g["id"], "cls": "doer", "build": key, "ask": job})
            continue
        if (ch == "gchat" and who.lower() in owners and not text.startswith("[") and _FRUSTRATION.match(text.strip())
                and not about_a_build(text)):
            prev_reply, prev_ask = _before(rows, i, who, agent)
            if not prev_reply:
                continue
            ask = prev_ask or text
            pkey = (core.person_for(handle=who.lower()) or {}).get("key") or who.lower()
            g = _find(d, pkey, ask)
            if g and g.get("status") in ("building",):
                continue
            if g and g.get("status") == "built" and int(g.get("attempts", 0)) >= MAX_ATTEMPTS:
                if not dry:
                    stuck_card(d, g, text)
                continue
            if not g:
                g = open_gap(d, "regression", ask, f"{text} (after: {_short(prev_reply, 260)})", pkey, ctx, "owner complaint")
            key = "" if dry else file_build(d, g, by=pkey, previous=(g.get("build") or "") and f"{g.get('build')} landed; the owner still says: \"{_short(text, 160)}\"")
            out.append({"gap": g["id"], "cls": "regression", "build": key, "ask": ask})
    d["cursor"] = len(rows)
    if not dry:
        _save(d)
    return out


def _before(rows: list, i: int, owner: str, agent: str) -> tuple:
    """The agent's last reply before row i in the same conversation/thread, and the owner's ask before that."""
    r0 = rows[i] or {}
    convo, thread = str(r0.get("convo") or ""), str(r0.get("thread") or "")
    reply, ask = "", ""
    for j in range(i - 1, max(-1, i - 40), -1):
        r = rows[j] or {}
        if str(r.get("convo") or "") != convo or str(r.get("thread") or "") != thread:
            continue
        who = str(r.get("who") or "").lower()
        if not reply:
            if who == agent and str(r.get("channel") or "") == "gchat":
                reply = str(r.get("text") or "")
            continue
        if who == owner.lower() and not str(r.get("text") or "").startswith("["):
            ask = str(r.get("text") or "")
            break
    return reply, ask


# ------------------------------------------------------------------ the build landed: bring the ask back
def on_built(key: str) -> bool:
    """builder.merge landed `key`: schedule the replay of the ask it came from (the footer, or the gap that filed it)."""
    d = _load()
    g = next((x for x in d["gaps"].values() if x.get("build") == key or key in (x.get("builds") or [])), None)
    if not g:
        try:
            meta = parse_footer((core.VAULT / key).read_text(encoding="utf-8"))
        except OSError:
            meta = {}
        if not meta.get("ask"):
            return False
        g = open_gap(d, "asked", meta.get("ask", ""), "", str(meta.get("person") or core.first_owner()),
                     {"convo": meta.get("convo", ""), "thread": meta.get("thread", ""), "space": meta.get("space", "")}, "note")
        g["build"] = key
    g["status"] = "built"
    if time.time() - float(g.get("ts") or 0) > REPLAY_MAX_AGE_H * 3600 or not g.get("ask"):
        g["status"] = "built_stale"
        _save(d)
        return False
    d["replays"] = [r for r in d["replays"] if r.get("gap") != g["id"]] + [{"gap": g["id"], "due": time.time() + REPLAY_AFTER_S}]
    _save(d)
    core.log_change("capability_built", f"gap {g['id']}: {key} landed; the ask is replayed in {REPLAY_AFTER_S}s", {"op": "none"}, "builder")
    return True


REPLAY_NOTE = ("[REPLAY, from code: the owner asked this earlier and you could not do it then ({quote}). The missing piece was "
               "just built. Do it NOW with your tools, in this same place; do not mention the wait or the build, do not ask "
               "again what they meant, and if one step is truly theirs (a password, a tap, a fact only they have) name exactly that step.]")


def replay_note(gid: str) -> str:
    g = _load()["gaps"].get(gid) or {}
    return REPLAY_NOTE.format(quote=_short(g.get("quote") or "it was not something you could do", 160))


def replay_message(g: dict) -> dict | None:
    person = core.person_for(handle=g.get("person") or "")
    if not person or not g.get("ask"):
        return None
    convo = str(g.get("convo") or "")
    space = str(g.get("space") or "")
    if not space:
        space = core.load(core.DATA / "gchat_spaces.json", {}).get(convo[3:]) if convo.startswith("dm:") else convo
    channel = "gchat" if str(space or "").startswith("spaces/") else "local"
    return {"channel": channel, "person": person, "text": str(g["ask"]), "space": space or "", "thread": str(g.get("thread") or ""),
            "convo": convo or f"dm:{person['key']}", "direct": True, "why": "DM" if convo.startswith("dm:") else "@mention",
            "_held": "replay", "_capability_replay": g["id"], "raw_from": person.get("key", "")}


def tick(dispatch, dry: bool = False) -> int:
    """The bridge's minute tick: scan the transcript seams, then hand every due replay to `dispatch(msg)`."""
    try:
        scan(dry=dry)
    except Exception as e:  # noqa: BLE001
        core.log_change("capability_error", f"scan: {type(e).__name__}: {str(e)[:160]}", {"op": "none"}, "code") if not dry else None
    d = _load()
    now, n, keep = time.time(), 0, []
    for r in d["replays"]:
        g = d["gaps"].get(str(r.get("gap") or ""))
        if not g or float(r.get("due") or 0) > now:
            keep.append(r)
            continue
        m = replay_message(g)
        if m and not dry:
            g["status"], g["replayed"] = "replayed", core.stamp()
            try:
                dispatch(m)
                n += 1
            except Exception as e:  # noqa: BLE001
                g["note"] = f"replay failed: {type(e).__name__}"
        elif m:
            n += 1
    if not dry:
        d["replays"] = keep
        _save(d)
    return n


# ------------------------------------------------------------------ the owner's decision, as a card in that thread
OPT_OTHER, OPT_DROP = "Try it another way (Recommended)", "Drop it"


def stuck_card(d: dict, g: dict, why: str) -> str:
    """After MAX_ATTEMPTS builds the ask still fails: ONE card where it was asked. The tap is acted on by code
    (cards.sweep -> owner_answer): another way files a fresh build with everything tried so far; drop closes it."""
    if g.get("card"):
        return str(g["card"])
    q = (f"I've built for \"{_short(g.get('ask', ''), 120)}\" {int(g.get('attempts', 0))} times and it still doesn't do it. "
         f"Last time: {_short(why, 160)}. Try it another way, or drop it?")
    card = {"header": "Build stuck", "lane": _lane(g.get("ask", ""), g), "subject": f"capability:{g['id']}", "capability_gap": g["id"],
            "question": q, "urgent": True,
            "options": [{"label": OPT_OTHER, "description": "a fresh build that knows what was tried"},
                        {"label": OPT_DROP, "description": "forget this ask"}],
            "advice": "💡 If you know what's missing, say it in this thread first; the next build will carry it.",
            "details": f"Ask: {g.get('ask', '')}\n\nBuilds: {', '.join(g.get('builds') or [])}\n\nLast answer: {g.get('quote', '')}"}
    try:
        sys.path.insert(0, str(core.AGENT.parent / "scripts"))
        import cards
        r = cards.post([card], "capability", lane=card["lane"], thread=str(g.get("thread") or ""), space=str(g.get("space") or ""), asked=True)
    except Exception as e:  # noqa: BLE001
        r = {"posted": False, "reason": f"{type(e).__name__}: {e}"}
    g["status"] = "stuck"
    if r.get("qid"):
        g["card"] = r["qid"]
        return str(r["qid"])
    g["card_error"] = str(r.get("reason") or "")[:200]
    return ""


def owner_answer(gid: str, option: str, by: str = "", text: str = "") -> dict:
    opt = re.sub(r"\s*\((?:recommended)[^)]*\)\s*$", "", str(option or ""), flags=re.I).strip().lower()
    d = _load()
    g = d["gaps"].get(gid)
    if not g:
        return {"ok": False, "reason": "no such gap"}
    g.pop("card", None)
    if opt.startswith("drop"):
        g["status"] = "dropped"
        _save(d)
        return {"ok": True, "status": "dropped"}
    if opt.startswith("try"):
        prev = ("builds " + ", ".join(g.get("builds") or []) + " landed and the ask still failed"
                + (f"; the owner says: \"{_short(text, 200)}\"" if text else "") + ". Take a different route (another layer, another tool).")
        g["attempts"] = 0
        key = file_build(d, g, by=by, previous=prev)
        _save(d)
        return {"ok": bool(key), "status": "refiled", "build": key}
    return {"ok": False, "reason": f"unknown option {option!r}"}


# ------------------------------------------------------------------ cli / render
def status() -> str:
    d = _load()
    out = [f"cursor {d.get('cursor')} | replays due {len(d.get('replays') or [])} | filed today {_filed_today(d)}/{FILED_PER_DAY}"]
    for g in sorted(d["gaps"].values(), key=lambda x: -float(x.get("ts") or 0))[:20]:
        out.append(f"- {g['id']} {g.get('when', '')} {g.get('cls')} {g.get('status')} x{g.get('attempts', 0)}: {_short(g.get('ask', ''), 90)}"
                   + (f" -> {g.get('build')}" if g.get("build") else ""))
    return "\n".join(out)


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    ap = argparse.ArgumentParser()
    ap.add_argument("cmd", choices=["status", "scan", "replay"])
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args()
    if a.cmd == "status":
        print(status())
    elif a.cmd == "scan":
        for x in scan(dry=a.dry_run):
            print(x)
    else:
        print(tick(lambda m: print("would replay:", m["text"][:80]), dry=a.dry_run))

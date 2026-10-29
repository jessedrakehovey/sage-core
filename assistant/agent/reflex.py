"""reflex.py - the agent notices it is failing and changes course by code, within the hour. Built 2026-10-27.

the owner, 2026-10-27: "Sage was unable to do most of what I asked it today in both google spaces. I need sage to be
able to recognize when it is failing or should be adapting and change quickly. Sage should be able to recognize a
failure and correct itself nearly 100% within the hour."

What 2026-10-26 looked like from the stores: the card sweep timed out 56 runs in a row (every sweep re-ran the
sentinel checks once per open alarm card) and nothing changed course; "no more of these messages" became a model
rule while the lines kept coming from code; a usage limit held his questions 50 minutes with the second account
idle, and the doer retried straight into the same limit; builds that conflicted with a moved master (or were done
by hand) were rebuilt again and again without landing; a card refused by the draft lint was announced as "below".
Every one of those had a signal in a file this system already writes. Nobody read them for an hour.

This module is that reader, zero-LLM, on the spine every 10 minutes and on the bridge's minute tick:

  detect   each detector reads live stores for one failure class and returns findings keyed so the same fault is
           one finding across passes:
             job:<name>          a spine job failing SPINE_FAILS runs in a row (spine_state.json)
             limit:held          questions held on a usage limit while another account has room (held.json)
             limit:doer:<id>     a doer ticket staged on a usage limit whose reset has passed (tickets.json)
             build:<key>         a build that is built but unlanded (master moved under it) for an hour (builds.json)
             gap:<id>            an auto-filed build request the builder never picked up in 6 h (capability.json)
             leak:<class>        a code-posted line of a class an owner switched off, posted after the switch
             account:<name>      an account that refused a turn (not a limit); probed hourly, cleared when back
             correction:<ts>     an owner's correction that still ended in words alone (correction.py ledger)
  fix      a ladder of steps by code, one per pass, cheapest first: run the job alone with a stack dump and raise its
           budget / release the held questions / re-queue the doer job / land the commit (cherry-pick + the touched
           tests) / kick the builder / file ONE build request carrying the evidence (the stack, the row) so the code
           is fixed without an owner opening a session.
  verify   a finding has VERIFY_MIN (60) to clear: the detector no longer reports it -> fixed (the minutes are kept
           for the scorecard). Still there -> the next step; out of steps -> escalate ONCE: a Mission Control note,
           and a chat line only when the owner has to do something (rule: restart quietly, speak when it stays broken).
  mute     the message classes an owner can switch off with a sentence ("no more 'still not built' reminders")
           bound to the code that posts them: core.rule_add -> bind_rule -> muted at once, ledgered, undoable;
           every poster asks allowed(cls) first. A muted class that posts anyway is a leak finding (a build request).
           "Stop sending me messages like this" in a thread (mute_from_thread, 2026-10-27): the class comes from the
           thread's last code-posted line (a custom class from its opening words when none is known), by code,
           before any model turn; voice.gate enforces every mute at the one door to Chat.

Nothing here sends outside the household, taps a card, or edits code: it changes runtime state (held rows,
tickets, job budgets, the builder's queue) and files build requests the builder builds under its fences.
State: data/agent/reflex.json. Generic: no organization, person or service is named here.

  python assistant/agent/reflex.py status | tick [--dry-run] | stats | muted | replay <YYYY-MM-DD>
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import core  # noqa: E402

STORE = core.DATA / "reflex.json"
ADATA = Path(os.environ["ASSISTANT_DATA"]) if os.environ.get("ASSISTANT_DATA") else core.AGENT.parent / "data"
SPINE_STATE = ADATA / "spine_state.json"
SPINE_JOBS = core.AGENT.parent / "spine_jobs.json"
TICKETS = ADATA / "tickets.json"
VERIFY_MIN = 60              # a fix has an hour to prove itself before the next step or the owner
SPINE_FAILS = 3
GAP_STALL_H = 6
BUILD_UNLANDED_MIN = 60
REESCALATE_H = 24
KEEP_DAYS = 30
MAX_TIMEOUT_S = 900
LIMIT = re.compile(r"(session|usage|rate) limit|hit your limit|limit reached|too many requests", re.I)
FOOTER = "<!-- reflex:"
# transcript channels code writes but never posts (builder.note_owners: "nothing is posted", 2026-10-08)
TRANSCRIPT_ONLY = ("builder",)

# ------------------------------------------------------------------ the message classes an owner can switch off
# rx: the owner's sentence (a rule) that names the class. line: the wording the code posts (leak detection).
CLASSES = {
    "build_slipped": {"what": "'Still not built' reminders",
                      "rx": r"still not built|not[- ]built reminder|build reminder",
                      "line": r"^Still not built:"},
    "build_landed": {"what": "build-landed announcements",
                     "rx": r"build[- ]landed|build announcement|announce\w*.{0,20}build|built-now line|that's built now",
                     "line": r"^(?:That's built now|I built a fix for this one|I built (?:that thing|a couple of things) you asked for)"},
    "build_failed": {"what": "'I couldn't get this built' notices",
                     "rx": r"couldn'?t get this built|build fail\w* notice",
                     "line": r"^I couldn't get this built:"},
    "build_unconfirmed": {"what": "'Not confirmed: build' cards",
                          "rx": r"not confirmed.{0,40}build|build.{0,40}not confirmed|could not confirm this landed|confirm\w*.{0,20}(?:a|the) build",
                          "line": r"^\[card [^\]]*\] Not confirmed: build"},
    "text_unconfirmed": {"what": "'Did this text go?' / 'text not confirmed' notices",
                         "rx": r"text not confirmed|did this text go|check messages|confirm\w* (?:whether |that )?(?:an? |the |outgoing )?texts?\b|texts? (?:send|went|sent)\b.{0,20}confirm",
                         "line": r"^\[card [^\]]*\] (?:Did this text go|Text not confirmed|Text unconfirmed)|^Not confirmed: text"},
    "scorecard": {"what": "scorecard lines outside the Monday digest",
                  "rx": r"scorecard",
                  "line": r"^Scorecard:"},
    "review_summary": {"what": "'From my review of yesterday's chats' summaries",
                       "rx": r"review summar|from my review|nightly review|review of yesterday",
                       "line": r"^From my review of"},
    "system_status": {"what": "system-status lines (a probe or spine is back / stopped)",
                      "rx": r"system[- ]status|probe is back|spine stoppage|stoppage heads|is back; the gap|heads-ups? about|restart (?:the job|it) quietly",
                      "line": r"is back; the gap was|^Heads up: .* has stopped|^\[card [^\]]*\] (?:Day Spine stopped|Spine job .* failing|Task .* failed)"},
    "chase_line": {"what": "'Still waiting on you' chase lines",
                   "rx": r"still waiting on you|chase line|waiting-on-you",
                   "line": r"^Still waiting on you:"},
    "dropped": {"what": "'dropped, nobody could confirm it' notes",
                "rx": r"\bdropped\b.{0,40}(?:lines?|messages?|notices?)|nobody could confirm|dropped:",
                "line": r"^Dropped: .*\(nobody could confirm it\)"},
    "unconfirmed_ask": {"what": "'I could not confirm ... went; check once and tell me' lines",
                        "rx": r"could not confirm.{0,30}(?:went|landed)|check once and tell me",
                        "line": r"^I could not confirm (?:that|the) .*(?:went|landed)"},
}
_TIGHT = re.compile(r"\b(?:never|no more|stop|don'?t|do not|fewer|quit|not)\b", re.I)
# "stop sending me messages like this" / "no more of these" / "don't send me this kind of thing": a tightening word
# plus a pointer at THIS message (the thread it was said in), naming no class. The class comes from the thread's
# last code-posted line (mute_from_thread, 2026-10-27).
_POINTED = re.compile(r"\b(?:like (?:this|these|that|those)|(?:this|these|that|those) (?:kind|sort|type)s?\b|of (?:these|those|this)\b|"
                      r"(?:messages?|lines?|notes?|notices?|texts?|posts?|reminders?|updates?|stuff|things?) (?:like )?(?:this|these|those)\b|"
                      r"^(?:stop|no more|quit)(?: (?:sending|posting|doing)(?: me)?(?: (?:this|these|those|that))?)?[.!]*$)", re.I)


def classes() -> dict:
    """The built-in classes plus the ones an owner's "like this" made from a thread (reflex.json `custom`)."""
    try:
        return {**CLASSES, **{k: v for k, v in (_load().get("custom") or {}).items() if isinstance(v, dict) and v.get("line")}}
    except Exception:  # noqa: BLE001
        return dict(CLASSES)


# ------------------------------------------------------------------ store
def _load() -> dict:
    d = core.load(STORE, {})
    if not isinstance(d, dict):
        d = {}
    for k, v in (("findings", {}), ("muted", {}), ("job_overrides", {}), ("released", {}), ("kicked", {}), ("stats", []), ("leaks", {}), ("custom", {})):
        d.setdefault(k, v)
    return d


def _save(d: dict) -> None:
    cut = time.time() - KEEP_DAYS * 86400
    d["findings"] = {k: f for k, f in d["findings"].items() if float(f.get("last_seen") or f.get("first") or 0) >= cut or not f.get("fixed")}
    d["stats"] = [s for s in d["stats"] if float(s.get("ts") or 0) >= cut][-500:]
    core.save(STORE, d)


def _short(s, n=200) -> str:
    s = re.sub(r"\s+", " ", str(s or "")).strip()
    return s if len(s) <= n else s[:n].rsplit(" ", 1)[0] + "..."


def _log(msg: str) -> None:
    core.log_change("reflex", _short(msg, 300), {"op": "none"}, "code")


def _note(text: str) -> None:
    """A line on Mission Control only (never Chat): the reflex's own record of what it did."""
    try:
        sys.path.insert(0, str(core.AGENT.parent / "scripts"))
        import cards
        cards.note("reflex", text)
    except Exception:  # noqa: BLE001
        pass


# ------------------------------------------------------------------ muting: an owner's sentence becomes a code switch
def muted() -> dict:
    return dict(_load().get("muted") or {})


def allowed(cls: str) -> bool:
    """Posters ask this before a code-initiated line of class `cls`. Never raises; unknown classes are allowed."""
    try:
        return str(cls) not in (_load().get("muted") or {})
    except Exception:  # noqa: BLE001
        return True


def mute(cls: str, why: str = "", by: str = "", rule: str = "") -> dict:
    all_ = classes()
    if cls not in all_:
        return {"status": "refused", "reason": f"no such class {cls!r}"}
    d = _load()
    if cls in d["muted"]:
        return {"status": "already", "cls": cls}
    d["muted"][cls] = {"ts": time.time(), "when": core.stamp(), "why": _short(why, 300), "by": by, "rule": rule}
    _save(d)
    cid = core.log_change("reflex_mute", f"switched off {all_[cls]['what']} ({cls}): {_short(why, 160)}", {"op": "reflex_unmute", "cls": cls}, by or "code")
    return {"status": "applied", "cls": cls, "change": cid}


# ------------------------------------------------------------------ "stop sending me messages like this" (2026-10-27)
def custom_class(parent: str) -> tuple[str, dict]:
    """A class made from one code-posted line: its opening words (a card's header, or the first five words) become
    the line pattern, so every later line with that opening is muted. -> (cls, spec)."""
    p = re.sub(r"\s+", " ", str(parent or "")).strip()
    m = re.match(r"^\[card [^\]]*\]\s*(?P<h>[^:]{3,80})", p)
    if m:
        head = m.group("h").strip()
        rx = r"^\[card [^\]]*\]\s*" + re.escape(head)
        what = f"'{head}' cards"
    else:
        head = re.split(r"[:(—–-]| - ", p, 1)[0].strip()
        head = re.split(r"\d", head, 1)[0].strip()          # a time, a count, a date: the next line's will differ
        words = head.split(" ")
        head = " ".join(words[:5]) if len(words) > 5 else head
        head = head[:60].strip(" ,;")
        rx = r"^" + re.escape(head)
        what = f"'{head}...' lines"
    cls = "custom:" + hashlib.sha1(rx.encode("utf-8")).hexdigest()[:8]
    return cls, {"what": what, "rx": "", "line": rx, "from": _short(p, 200), "when": core.stamp()}


def thread_parent(convo: str, thread: str, before: str = "") -> dict:
    """The last line in `thread` that the agent or a card posted before `before` (a stamp): {text, channel, who};
    {} when the thread holds none."""
    if not thread:
        return {}
    agent = str(core.profile().get("name") or "").lower()
    hit = {}
    for r in core.rows(core.TRANSCRIPT):
        if str(r.get("thread") or "") != thread:
            continue
        if before and str(r.get("ts") or "") >= before:
            break
        if str(r.get("who") or "").lower() not in (agent, "card"):
            continue
        if _BOILERPLATE.search(str(r.get("text") or "")):
            continue                        # "didn't go through" / "out of steam" / "Done. No more": the bridge's own lines, never what "this" points at
        hit = {"text": str(r.get("text") or ""), "channel": str(r.get("channel") or ""), "who": str(r.get("who") or ""), "ts": str(r.get("ts") or "")}
    return hit


_BOILERPLATE = re.compile(r"didn't go through on my side|out of steam|can't reach my model|couldn't get to this one|^Done\. No more |^Got it\. No more like that", re.I)


def stop_sentence(text: str) -> str:
    """The sentence of `text` that is a 'stop sending me these' ('' when there is none)."""
    for s in re.split(r"(?<=[.!?])\s+|\n+", str(text or "").strip()):
        s = s.strip()
        if s and _TIGHT.search(s) and _POINTED.search(s) and len(s) <= 160 and not s.startswith("["):
            return s
    return ""


def named_sentence(text: str) -> str:
    """The sentence of `text` that names a class and tightens ("no more 'still not built' reminders"); '' when none."""
    for s in re.split(r"(?<=[.!?])\s+|\n+", str(text or "").strip()):
        s = s.strip()
        if s and len(s) <= 200 and classes_for(s):
            return s
    return ""


def mute_from_thread(text: str, convo: str, thread: str, by: str = "") -> dict:
    """An owner wrote 'stop sending me messages like this' in a thread: switch off, by code, this turn, whatever the
    thread's last code-posted line was (a known class, or a custom class made from its opening words). When that
    line was the model's own words, a rule for the model is the only switch there is. -> {} when the text is not
    such a sentence, else {status, cls, what, line (the owner's one line), note (for the brain), rest (what else
    the message asks)}."""
    s = stop_sentence(text) or named_sentence(text)
    if not s:
        return {}
    rest = str(text or "").replace(s, "", 1).strip()
    rest = re.sub(r"^\[.*?\]\s*$", "", rest, flags=re.S).strip() if rest.startswith("[") and rest.count("\n") == 0 and "LOOK AT IT" not in rest else rest
    why = f"{by or 'an owner'} said in a thread: {s}"
    named = classes_for(s)
    if named:
        res = [mute(c, why=why, by=by) for c in named]
        cls, what = named[0], classes()[named[0]]["what"]
        status = "applied" if any(r.get("status") == "applied" for r in res) else "already"
    else:
        parent = thread_parent(convo, thread)
        if not parent:
            return {}
        if parent["channel"] in ("gchat", "") and parent["who"].lower() != "card":
            # the model's own words: no code posts them, so the switch is a rule the model reads every turn
            rule = f"Never send messages like this one again: \"{_short(parent['text'], 110)}\""
            r = core.rule_add(rule, why=why, by=by)
            if r.get("status") not in ("applied", "folded"):
                return {}
            return {"status": r["status"], "cls": "rule", "what": "messages like that", "rest": rest,
                    "line": "Got it. No more like that from me.",
                    "note": f"[done by code this turn: the owner's \"{s}\" is now a standing rule against messages like the one in this thread; "
                            "say so in a few words and answer the rest, don't ask which messages]"}
        cls = line_class(parent["text"])
        if cls:
            what = classes()[cls]["what"]
        else:
            cls, spec = custom_class(parent["text"])
            d = _load()
            d["custom"].setdefault(cls, spec)
            _save(d)
            what = spec["what"]
        r = mute(cls, why=why, by=by)
        status = r.get("status", "refused")
        if status == "refused":
            return {}
    line = f"Done. No more {what} from me."
    return {"status": status, "cls": cls, "what": what, "rest": rest, "line": line,
            "note": f"[done by code this turn: the owner's \"{s}\" switched off {what} for good; say so in a few words "
                    "(\"Done, no more of those\") and answer the rest, don't ask which messages]"}


def unmute(cls: str) -> bool:
    d = _load()
    if cls not in d["muted"]:
        return False
    d["muted"].pop(cls, None)
    _save(d)
    return True


def classes_for(sentence: str) -> list:
    """The classes an owner's sentence switches off: it must tighten (never / no more / stop) and name the class."""
    s = str(sentence or "")
    if not _TIGHT.search(s):
        return []
    return [c for c, spec in CLASSES.items() if re.search(spec["rx"], s, re.I)]


def bind_rule(rule: str, rid: str = "", by: str = "") -> list:
    """Called by core.rule_add the moment a rule is applied: the classes it names are muted by code the same turn."""
    out = []
    for c in classes_for(rule):
        r = mute(c, why=f"rule {rid}: {rule}" if rid else rule, by=by, rule=rid)
        if r.get("status") == "applied":
            out.append(c)
    return out


def bind_rules() -> list:
    """Every active rule, on each tick: a rule written before this module existed (r36, r99, r100, r110, r116 on
    2026-10-26) still switches its classes off. Idempotent."""
    out = []
    try:
        rules = core.rules_active()
    except Exception:  # noqa: BLE001
        return out
    for r in rules:
        out += bind_rule(str(r.get("rule") or ""), rid=str(r.get("id") or ""), by=str(r.get("by") or ""))
    return out


def line_class(text: str) -> str:
    """The class of a line the code posted (the transcript wording), '' when it is not one of ours."""
    t = str(text or "").strip()
    for c, spec in classes().items():
        try:
            if re.search(spec["line"], t, re.I | re.S):
                return c
        except re.error:
            continue
    return ""


# ------------------------------------------------------------------ detectors
def _jobs_cfg() -> dict:
    try:
        return {j.get("name"): j for j in (core.load(SPINE_JOBS, {}) or {}).get("jobs", []) if isinstance(j, dict)}
    except Exception:  # noqa: BLE001
        return {}


def d_jobs() -> list:
    out = []
    st = core.load(SPINE_STATE, {}) or {}
    cfg = _jobs_cfg()
    for name, s in (st.items() if isinstance(st, dict) else []):
        if not isinstance(s, dict) or int(s.get("fails") or 0) < SPINE_FAILS:
            continue
        if not (cfg.get(name) or {}).get("enabled", True):
            continue
        out.append({"key": f"job:{name}", "kind": "job", "what": f"spine job {name} failed {s.get('fails')} runs in a row ({_short(s.get('result'), 100)})",
                    "data": {"name": name, "result": str(s.get("result") or ""), "fails": int(s.get("fails") or 0)}})
    return out


def d_limits() -> list:
    out = []
    try:
        import held
        import account
    except Exception:  # noqa: BLE001
        return out
    fb = account.fallback()
    rows = [r for r in held.pending() if float(r.get("until") or 0) > time.time()]   # a released row is the bridge's within the minute
    if rows and fb:
        # questions wait on a limit; is the other account known to be limited too? (account.limited_until)
        limited = {a: account.limited_until(a) for a in {str(core.profile().get("claude_account") or ""), fb} if a}
        free = [a for a, u in limited.items() if u <= time.time()]
        if free:
            out.append({"key": "limit:held", "kind": "limit_held",
                        "what": f"{len(rows)} question{'s' if len(rows) != 1 else ''} held on a usage limit while account {free[0]} has room",
                        "data": {"rows": [r.get("id") for r in rows], "free": free[0]}})
    tickets = core.load(TICKETS, {}) or {}
    for tid, t in (tickets.items() if isinstance(tickets, dict) else []):
        if not isinstance(t, dict) or t.get("kind") != "agent_run" or t.get("status") != "staged":
            continue
        hist = t.get("history") or []
        note = str((hist[-1] if hist else {}).get("note") or "")
        if not LIMIT.search(note):
            continue
        try:
            import held as _h
            reset = _h.limit_reset(note, now=_iso_epoch(t.get("updated"))) or 0
        except Exception:  # noqa: BLE001
            reset = 0
        if reset and reset > time.time() and not fb:
            continue                                    # the limit still stands and there is no other account: waiting is right
        out.append({"key": f"limit:doer:{tid}", "kind": "limit_doer",
                    "what": f"doer job staged on a usage limit: {_short(t.get('title'), 90)}",
                    "data": {"ticket": tid, "title": str(t.get("title") or ""), "updated": str(t.get("updated") or "")}})
    return out


def _iso_epoch(s) -> float:
    s = str(s or "")
    try:
        if s.endswith("Z"):
            import calendar
            return calendar.timegm(time.strptime(s[:19], "%Y-%m-%dT%H:%M:%S"))
        return time.mktime(time.strptime(s[:19], "%Y-%m-%dT%H:%M:%S"))
    except ValueError:
        return time.time()


def d_builds() -> list:
    out = []
    try:
        import builder
        st = core.load(builder.STATE, {}) or {}
    except Exception:  # noqa: BLE001
        return out
    for key, s in st.items():
        if key.startswith("_") or not isinstance(s, dict):
            continue
        if s.get("status") in ("waiting", "retry") and s.get("commit") and re.search(r"master moved|no longer applies|overlap", str(s.get("summary") or ""), re.I):
            age = (time.time() - _stamp_epoch(s.get("last"))) / 60
            if age >= BUILD_UNLANDED_MIN or s.get("status") == "waiting":
                out.append({"key": f"build:{key}", "kind": "build_unlanded", "what": f"built {int(age)} min ago but not landed: {_short(s.get('title') or key, 80)}",
                            "data": {"key": key, "commit": str(s.get("commit") or ""), "files": list(s.get("files") or [])}})
    return out


def _stamp_epoch(s) -> float:
    try:
        return time.mktime(time.strptime(str(s or "")[:16], "%Y-%m-%d %H:%M"))
    except ValueError:
        return time.time()


def d_gaps() -> list:
    out = []
    try:
        import capability
        import builder
        d = capability._load()
        open_keys = {r["key"] for r in builder.collect()}
        bst = core.load(builder.STATE, {}) or {}
    except Exception:  # noqa: BLE001
        return out
    for g in d.get("gaps", {}).values():
        if g.get("status") != "building" or not g.get("build") or g["build"] not in open_keys:
            continue
        age_h = (time.time() - float(g.get("ts") or 0)) / 3600
        last = _stamp_epoch((bst.get(g["build"]) or {}).get("last")) if (bst.get(g["build"]) or {}).get("last") else 0
        if age_h >= GAP_STALL_H and time.time() - last >= GAP_STALL_H * 3600:
            out.append({"key": f"gap:{g['id']}", "kind": "gap_stalled", "what": f"build request not picked up in {int(age_h)} h: {_short(g.get('ask'), 80)}",
                        "data": {"gap": g["id"], "build": g["build"]}})
    return out


def d_leaks(rows: list | None = None) -> list:
    """A code-posted line of a muted class, posted after the mute: the gate at that poster is missing."""
    d = _load()
    if not d["muted"]:
        return []
    rows = rows if rows is not None else core.rows(core.TRANSCRIPT)[-400:]
    agent = str(core.profile().get("name") or "").lower()
    out, seen = [], {}
    for r in rows:
        if str(r.get("who") or "").lower() not in (agent, "card"):
            continue
        if r.get("held"):
            continue                                   # on Mission Control only (a held or system card, a quiet receipt): not a leak
        if str(r.get("channel") or "") in TRANSCRIPT_ONLY:
            continue                                   # written for Sage's memory, never posted to Chat: not a leak
        cls = line_class(r.get("text"))
        if not cls or cls not in d["muted"]:
            continue
        ts = _stamp_epoch(r.get("ts"))
        if ts <= float(d["muted"][cls].get("ts") or 0) + 60:
            continue
        if ts <= float(seen.get(cls) or 0):
            continue
        seen[cls] = ts
        out.append({"key": f"leak:{cls}", "kind": "leak", "what": f"a {classes()[cls]['what']} line went out after it was switched off",
                    "data": {"cls": cls, "text": _short(r.get("text"), 200), "channel": str(r.get("channel") or ""), "ts": str(r.get("ts") or "")}})
    return out


def d_system_cards() -> list:
    """A system card open on Mission Control (cards.is_system: never in Chat, never in the digest, 2026-10-27): a
    sentinel finding with a button code can press, a connection that needs a sign-in, a channel paused, a failed
    self-test. Each is the system's own fault to work: press its recommended button once, give it an hour, file a
    build with the evidence, and only then one plain line to an owner, only when the owner has to act."""
    out = []
    try:
        sys.path.insert(0, str(core.AGENT.parent / "scripts"))
        import cards
        live = cards._live()
    except Exception:  # noqa: BLE001
        return out
    for c in live:
        if c.get("status") != "open" or c.get("delivery") != "mc":
            continue
        q = (c.get("questions") or [{}])[0]
        if q.get("outcome_id"):
            continue                                   # the outcome ledger's own loop; its evidence is the owner's
        act = q.get("sentinel_act") or {}
        kind = "conn" if q.get("setup_rid") else "breaker" if q.get("breaker_lane") else "sentinel"
        out.append({"key": f"card:{c['id']}", "kind": "system_card",
                    "what": f"{_short(q.get('header'), 60)}: {_short(q.get('question'), 120)}",
                    "data": {"qid": c["id"], "sentinel_key": str(q.get("sentinel_key") or ""), "act": act, "kind": kind,
                             "lane": str(q.get("lane") or "personal"), "header": str(q.get("header") or ""),
                             "question": str(q.get("question") or "")[:600], "setup_rid": str(q.get("setup_rid") or "")}})
    return out


def d_accounts() -> list:
    """An account that REFUSED a turn (not a limit: a sign-in gone, Claude Code switched off for it) is out for a
    while (account.note_limit). The finding probes it cheaply every hour and clears the note the moment it is back,
    so questions held on it run; both accounts out = the owner hears the one thing only they can do."""
    out = []
    try:
        import account
    except Exception:  # noqa: BLE001
        return out
    for name, e in account.out_now().items():
        if str((e or {}).get("kind") or "") != "refused":
            continue
        out.append({"key": f"account:{name}", "kind": "account_out",
                    "what": f"account {name} refused a turn: {_short((e or {}).get('error'), 100)}",
                    "data": {"account": name, "error": str((e or {}).get("error") or ""), "seen": str((e or {}).get("seen") or "")}})
    return out


def d_corrections() -> list:
    """An owner's correction ("where is the card", "try again") that still ended in words alone after the bridge's
    re-run and its forced deed (correction.py ledger): the gap is in code, so ONE build request carries the pair."""
    out = []
    try:
        import correction
    except Exception:  # noqa: BLE001
        return out
    for r in correction.recent(24):
        if not r.get("words_only") or correction.handed_on(r):
            continue                                  # a turn that filed its capability build was not words alone
        out.append({"key": f"correction:{r.get('ts')}", "kind": "correction_unfixed",
                    "what": f"a correction ({r.get('kind')}) ended in words alone: {_short(r.get('text'), 70)}",
                    "data": {"ts": r.get("ts"), "ckind": r.get("kind"), "text": r.get("text"), "ask": r.get("ask"), "reply": r.get("reply"),
                             "expected": r.get("expected"), "rerun": r.get("rerun"), "convo": r.get("convo"), "thread": r.get("thread")}})
    return out


DETECTORS = {"job": d_jobs, "limit": d_limits, "build": d_builds, "gap": d_gaps, "leak": d_leaks, "card": d_system_cards,
             "account": d_accounts, "correction": d_corrections}


def detect(only: tuple = ()) -> list:
    out = []
    for name, fn in DETECTORS.items():
        if only and name not in only:
            continue
        try:
            out += fn()
        except Exception as e:  # noqa: BLE001
            _log(f"detector {name} crashed: {type(e).__name__}: {str(e)[:120]}")
    return out


# ------------------------------------------------------------------ fixes (one step per pass, cheapest first)
def _probe_job(name: str) -> dict:
    """Run the spine job alone with three times its budget and a stack dump at the old budget -> {ok, secs, stack, last}."""
    job = _jobs_cfg().get(name) or {}
    cmd = [sys.executable if c == "python" else c for c in (job.get("cmd") or [])]
    if not cmd:
        return {"ok": False, "secs": 0, "stack": "", "last": "no such job"}
    budget = int(job.get("timeout_s") or 300)
    if cmd[0] == sys.executable and len(cmd) >= 2 and str(cmd[1]).endswith(".py"):
        # a Python job: faulthandler prints every thread's stack at the old budget, the job runs on to 3x
        code = ("import faulthandler,runpy,sys; faulthandler.dump_traceback_later(%d, exit=False); sys.argv=%r; "
                "runpy.run_path(sys.argv[0], run_name='__main__')" % (budget, cmd[1:]))
        run = [sys.executable, "-X", "faulthandler", "-c", code]
    else:
        run = cmd
    t0 = time.time()
    try:
        r = subprocess.run(run, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=min(budget * 3, MAX_TIMEOUT_S),
                           cwd=str(core.VAULT), creationflags=core.NO_WINDOW)
        out = (r.stdout or "") + (r.stderr or "")
        secs = time.time() - t0
        stack = out[out.find("Timeout ("):][:3000] if "Timeout (" in out else ""
        exp = str(job.get("expect") or "")
        lines = [ln for ln in out.strip().splitlines() if ln.strip()]
        silent = bool(exp) and not any(ln.strip().startswith(exp) for ln in lines[-6:])
        return {"ok": r.returncode == 0 and not silent, "secs": round(secs, 1), "stack": stack, "last": (lines[-1] if lines else "")[:200]}
    except subprocess.TimeoutExpired as e:
        out = ((e.stdout or b"") if isinstance(e.stdout, bytes) else (e.stdout or "")) if e.stdout else ""
        out = out.decode("utf-8", "replace") if isinstance(out, bytes) else str(out)
        err = ((e.stderr or b"") if isinstance(e.stderr, bytes) else (e.stderr or "")) if e.stderr else ""
        err = err.decode("utf-8", "replace") if isinstance(err, bytes) else str(err)
        both = out + err
        return {"ok": False, "secs": round(time.time() - t0, 1), "stack": both[both.find("Timeout ("):][:3000] if "Timeout (" in both else "", "last": "timeout x3"}
    except OSError as e:
        return {"ok": False, "secs": 0, "stack": "", "last": f"{type(e).__name__}"}


def job_override(name: str) -> int:
    """The timeout the spine should use for `name` (0 = the job's own)."""
    o = (_load().get("job_overrides") or {}).get(name) or {}
    try:
        return int(o.get("timeout_s") or 0)
    except (TypeError, ValueError):
        return 0


def _reset_fails(name: str) -> None:
    st = core.load(SPINE_STATE, {}) or {}
    if isinstance(st, dict) and isinstance(st.get(name), dict):
        st[name]["fails"] = 0
        core.save(SPINE_STATE, st)


def fix_job(f: dict, d: dict, dry: bool) -> dict:
    name = f["data"]["name"]
    step = int(f.get("step") or 0)
    if step == 0:
        if dry:
            return {"what": f"would run {name} alone with a stack dump", "ok": True}
        p = _probe_job(name)
        budget = int((_jobs_cfg().get(name) or {}).get("timeout_s") or 300)
        f["probe"] = {k: p[k] for k in ("ok", "secs", "last")}
        f["stack"] = p.get("stack", "")
        if p["ok"] and p["secs"] <= budget:
            _reset_fails(name)                        # it runs clean alone: the pass was crowded; the counter restarts
            return {"what": f"ran {name} alone: clean in {p['secs']} s (its budget is {budget} s); counter reset", "ok": True}
        if p["ok"]:
            new = min(int(p["secs"] * 1.5) + 30, MAX_TIMEOUT_S)
            d["job_overrides"][name] = {"timeout_s": new, "ts": time.time(), "why": f"ran clean alone in {p['secs']} s, over its {budget} s budget"}
            _reset_fails(name)
            return {"what": f"ran {name} alone: clean in {p['secs']} s, over its {budget} s budget; budget raised to {new} s", "ok": True}
        return {"what": f"ran {name} alone: still fails ({p['last'][:80]}); stack kept", "ok": False}
    if step == 1:
        if dry:
            return {"what": f"would file a build request for {name} with the stack", "ok": True}
        key = _file_build(f, f"Spine job {name} keeps failing: {_short(f['data'].get('result'), 60)}",
                          f"The spine job `{name}` ({' '.join((_jobs_cfg().get(name) or {}).get('cmd') or [])}) has failed "
                          f"{f['data'].get('fails')} runs in a row with: {f['data'].get('result')}. Run alone with three times its budget it "
                          f"{'passed' if (f.get('probe') or {}).get('ok') else 'still failed'} ({(f.get('probe') or {}).get('last', '')}).\n\n"
                          + ("Stack at its budget (where it hangs):\n\n```\n" + f.get("stack", "")[:2500] + "\n```\n\n" if f.get("stack") else "")
                          + "Fix the cause in the job's code (not the budget): make it finish inside its budget, with a test that drives the slow path.")
        return {"what": f"filed the build request for {name}" + (f": {key}" if key else " (not filed: queue full or duplicate)"), "ok": bool(key), "build": key}
    return {"what": "out of steps", "ok": False, "final": True}


def fix_limit_held(f: dict, d: dict, dry: bool) -> dict:
    import held
    rows = [r for r in held.pending() if r.get("id") not in d["released"]]
    if not rows:
        return {"what": "nothing left to release", "ok": True, "final": True}
    if dry:
        return {"what": f"would release {len(rows)} held question(s) to run now on {f['data'].get('free')}", "ok": True}
    st = held._state()
    st["until"] = 0
    for r in st.get("rows", []):
        r["until"] = 0
        d["released"][r.get("id")] = time.time()
    core.save(held.STORE, st)
    return {"what": f"released {len(rows)} held question(s): the bridge re-runs them this minute on account {f['data'].get('free')}", "ok": True}


def fix_limit_doer(f: dict, d: dict, dry: bool) -> dict:
    tid = f["data"]["ticket"]
    if dry:
        return {"what": f"would re-queue doer job {tid}", "ok": True}
    sys.path.insert(0, str(core.AGENT.parent / "scripts"))
    import tickets as tk
    db = core.load(TICKETS, {}) or {}
    me = db.get(tid) or {}
    # a second ticket for the same job (the owner said "try again" into the same limit): ONE runs, the others close.
    # A twin already re-queued (status new/claimed) means this one is the duplicate.
    same = [(k, t) for k, t in db.items() if k != tid and isinstance(t, dict) and t.get("kind") == "agent_run"
            and core._norm(str(t.get("title") or "")) == core._norm(str(me.get("title") or ""))]
    if any(t.get("status") in ("new", "claimed") for _, t in same):
        live = next(k for k, t in same if t.get("status") in ("new", "claimed"))
        tk._transition(tid, "done", "reflex", f"duplicate of {live}, which runs", {"staged"})
        return {"what": f"closed doer job {tid} as a duplicate of {live}, which runs", "ok": True}
    twins = [k for k, t in same if t.get("status") == "staged" and str(t.get("updated") or "") < str(me.get("updated") or "")]
    for k in twins:
        tk._transition(k, "done", "reflex", f"duplicate of {tid}, which the reflex re-queued", {"staged"})
    r = tk._transition(tid, "new", "reflex", "re-queued by the reflex: the usage limit has reset / the other account has room", {"staged"})
    return {"what": f"re-queued doer job {tid}" + (f" and closed {len(twins)} duplicate(s)" if twins else ""), "ok": bool(r)}


def fix_build(f: dict, d: dict, dry: bool) -> dict:
    import builder
    key = f["data"]["key"]
    if dry:
        return {"what": f"would land {f['data'].get('commit')} for {key}", "ok": True}
    r = builder.land(key)
    return {"what": f"land {key}: {r.get('why') or r.get('status')}", "ok": bool(r.get("ok")), "final": not r.get("ok")}


GAP_KICKS_PER_DAY = 3       # forced builder runs the reflex may start a day for stalled requests (the builder's own cap is 4)


def fix_gap(f: dict, d: dict, dry: bool) -> dict:
    import builder
    key = f["data"]["build"]
    last = float(d["kicked"].get(key) or 0)
    if time.time() - last < GAP_STALL_H * 3600:
        return {"what": "kicked already this window", "ok": False, "final": True}
    day = time.strftime("%Y-%m-%d")
    today = sum(1 for k, ts in d["kicked"].items() if not k.startswith("_") and time.strftime("%Y-%m-%d", time.localtime(float(ts or 0))) == day)
    if today >= GAP_KICKS_PER_DAY:
        return {"what": f"{GAP_KICKS_PER_DAY} forced builder runs today already; it waits for the builder's own schedule", "ok": False, "final": True}
    if dry:
        return {"what": f"would kick the builder on {key}", "ok": True}
    if _PASS.get("kicked"):
        return {"skip": True}                              # one builder start a pass; this one goes next pass
    r = builder.kick(force=True, first=key)
    if r != "BUILDER started":
        return {"what": f"builder: {r}", "ok": False}       # running / idle: not counted; tried again next window
    _PASS["kicked"] = key
    d["kicked"][key] = time.time()
    return {"what": f"builder started on {key}", "ok": True}


_PASS: dict = {}            # per-tick scratch (one builder start a pass)


def fix_leak(f: dict, d: dict, dry: bool) -> dict:
    cls = f["data"]["cls"]
    day = time.strftime("%Y-%m-%d")
    if d["leaks"].get(cls) == day:
        return {"what": "build request filed today already", "ok": False, "final": True}
    if dry:
        return {"what": f"would file a build request: gate the {cls} poster", "ok": True}
    d["leaks"][cls] = day
    key = _file_build(f, f"{classes()[cls]['what']} still post after the owner switched them off",
                      f"An owner's rule switched off {classes()[cls]['what']} (reflex class `{cls}`, muted {d['muted'][cls].get('when')}), "
                      f"yet this line was posted by code at {f['data'].get('ts')} on channel {f['data'].get('channel')}:\n\n> {f['data'].get('text')}\n\n"
                      f"Find the poster of that wording and make it ask `reflex.allowed(\"{cls}\")` first (assistant/agent/reflex.py); add a test "
                      f"that mutes the class and proves the line is not posted.")
    return {"what": f"filed the build request to gate {cls}" + (f": {key}" if key else ""), "ok": bool(key), "build": key, "final": True}


def fix_system_card(f: dict, d: dict, dry: bool) -> dict:
    """Step 0: the card's own recommended button, pressed by code (run the job / the task / the drain / the sensor);
    a connection that needs a sign-in has no code step: final at once, so the owner hears one line. Step 1: the
    finding is still there an hour later: ONE build request with the evidence (the capability loop), final."""
    data = f.get("data") or {}
    step = int(f.get("step") or 0)
    act = data.get("act") or {}
    if data.get("kind") == "conn":
        return {"what": "a sign-in only the owner can do", "ok": False, "final": True}
    if data.get("kind") == "breaker":
        return {"what": "a channel paused on repeated failures; the owner decides", "ok": False, "final": True}
    runnable = any(act.get(k) for k in ("spine_job", "task", "drain", "cmd"))
    if step == 0 and runnable:
        if dry:
            return {"what": "would press its button", "ok": True}
        try:
            sys.path.insert(0, str(core.AGENT.parent / "scripts"))
            import sentinels
            r = sentinels.act(act, by="reflex")
        except Exception as e:  # noqa: BLE001
            r = f"{type(e).__name__}: {str(e)[:100]}"
        return {"what": f"pressed its button: {_short(r, 120)}", "ok": True}
    if dry:
        return {"what": "would file a build request", "ok": True, "final": True}
    title = f"Fix: {_short(data.get('header'), 50)}"
    body = ("The system card below stayed open on Mission Control after its own button was pressed by the reflex (or it had none). "
            f"Card {data.get('qid')}, finding `{data.get('sentinel_key') or '-'}`, lane {data.get('lane')}.\n\n> " + _short(data.get("question"), 500)
            + "\n\nFind the cause in the job, sensor or test it names and fix it so the finding clears by itself; "
            "a test encodes the failing path.")
    key = _file_build(f, title, body)
    return {"what": f"filed the build request" + (f": {key}" if key else " (one is open already)"), "ok": bool(key), "build": key, "final": True}


def fix_account_out(f: dict, d: dict, dry: bool) -> dict:
    """One cheap turn on the account (account.probe). Back -> its note is cleared, the next turn runs on it and the
    held questions release through limit:held. Still refused -> keep it out another REFUSAL_WAIT and try again in
    an hour; the third refusal is final (the owner hears it only if the other account is out too)."""
    import account
    name = f["data"]["account"]
    step = int(f.get("step") or 0)
    if dry:
        return {"what": f"would probe account {name}", "ok": True}
    ok, note = account.probe(name if name != "default" else None)
    if ok:
        account.clear(name if name != "default" else None)
        return {"what": f"probed {name}: back ({note}); its note cleared", "ok": True}
    account.note_limit(name if name != "default" else None, f["data"].get("error") or note)
    return {"what": f"probed {name}: still refused ({_short(note, 100)})", "ok": False, "final": step >= 2}


def fix_correction(f: dict, d: dict, dry: bool) -> dict:
    """One build request with the pair (the ask, the correction, the reply): what the turn should have put forward and
    did not, so the code (a deed, a net, a prompt line) is fixed once instead of the owner asking a third time. Final."""
    data = f.get("data") or {}
    day = time.strftime("%Y-%m-%d")
    if d.setdefault("correction_builds", {}).get(str(data.get("ckind"))) == day:
        return {"what": "a build request for this kind of correction was filed today already", "ok": False, "final": True}
    if dry:
        return {"what": "would file a build request with the ask and the reply", "ok": True, "final": True}
    d["correction_builds"][str(data.get("ckind"))] = day
    title = f"A correction still ended in words: {_short(data.get('text'), 50)}"
    body = (f"An owner corrected the agent ({data.get('ckind')}) at {data.get('ts')} and the turn still put nothing forward, after the bridge's "
            f"re-run ({'yes' if data.get('rerun') else 'no'}) and the forced deed (expected: {data.get('expected') or 'none known'}).\n\n"
            f"Their ask:\n\n> {_short(data.get('ask'), 400)}\n\nTheir correction:\n\n> {_short(data.get('text'), 300)}\n\nThe reply that went out:\n\n> {_short(data.get('reply'), 400)}\n\n"
            "Fix in code (assistant/agent/correction.py: `expected_deed`, `kind`, or the net in bridge.handle; or the prompt's correction line) "
            "so this ask shape carries its deed on a correction; a test replays this pair.")
    key = _file_build(f, title, body)
    return {"what": "filed the build request" + (f": {key}" if key else " (one is open already)"), "ok": bool(key), "build": key, "final": True}


FIXES = {"job": fix_job, "limit_held": fix_limit_held, "limit_doer": fix_limit_doer, "build_unlanded": fix_build, "gap_stalled": fix_gap,
         "leak": fix_leak, "system_card": fix_system_card, "account_out": fix_account_out, "correction_unfixed": fix_correction}
REESCALATE = {"system_card": 7 * 24}       # hours before the same unfixable finding may be said to an owner again (default REESCALATE_H)


def _file_build(f: dict, title: str, body: str) -> str:
    """ONE wiki build request carrying the evidence; the builder is kicked on it. '' when an open request already covers it."""
    try:
        import builder
        import wiki_writer
        for r in builder.collect():
            if core.similar(str(r.get("title") or ""), title) or f"reflex:{f['key']}" in str(r.get("text") or ""):
                return str(r.get("key") or "")
        meta = {"finding": f["key"], "kind": f.get("kind"), "first": f.get("first_when")}
        text = ("## Build request\n\n**Ask (the reflex, by code, " + core.stamp() + "):** " + title + "\n\n" + body
                + "\n\n**Acceptance:** the finding `" + f["key"] + "` clears on the next reflex pass (python assistant/agent/reflex.py status) "
                "and the test encodes the slow or missing path.\n\n**Evidence:** " + _short(f.get("what"), 200)
                + f"\n\n{FOOTER} {json.dumps(meta, ensure_ascii=False)} -->\n")
        x = wiki_writer.write({"title": title[:90], "type": "workflow", "lane": "personal", "body": text}, by="reflex")
        if x.get("status") not in ("applied", "appended"):
            return ""
        key = str(x.get("path") or "")
        try:
            builder.kick(force=True, first=key)
        except Exception:  # noqa: BLE001
            pass
        return key
    except Exception as e:  # noqa: BLE001
        _log(f"file_build failed for {f.get('key')}: {type(e).__name__}: {str(e)[:120]}")
        return ""


# ------------------------------------------------------------------ the owner, once
def _window_ok() -> bool:
    q = core.profile().get("quiet_hours") or [21, 7]
    h = time.localtime().tm_hour
    return not (h >= q[0] or h < q[1])


def _need_line(f: dict) -> str:
    """What the owner has to do, when the system cannot: '' when nothing (then only Mission Control hears)."""
    k = f.get("kind")
    if k == "limit_held":
        return "Both Claude accounts are at their usage limit; your questions run the minute one resets."
    if k == "account_out":
        try:
            import account
            others = [a for a in {str(core.profile().get("claude_account") or "default"), account.fallback()} if a and a != f["data"].get("account")]
            if others and all(account.limited_until(a if a != "default" else None) <= time.time() for a in others):
                return ""                      # the other account carries the turns: nothing for the owner to do
        except Exception:  # noqa: BLE001
            pass
        import voice
        return (f"Neither of my Claude accounts will take a turn right now ({voice.plain(f['data'].get('error') or '')}). "
                "I keep trying every hour and your questions wait with me; a fresh sign-in on the account is the fix if it stays this way.")
    if k == "system_card":
        data = f.get("data") or {}
        if data.get("kind") == "conn":
            # the card's own words already are a person's: "<name> has stopped working (...). Set it back up now? ..."
            return _short(data.get("question"), 300)
        if data.get("kind") == "breaker" and not re.search(r"\b0 approved items?\b", str(data.get("question") or "")):
            return _short(data.get("question"), 300)       # something of theirs is waiting behind the pause
        return ""
    return ""


def escalate(f: dict, dry: bool) -> str:
    """Mission Control gets the whole story (what, what was tried); an owner hears only the one sentence that says
    what only they can do, in a person's words, inside the day window, and nothing when there is nothing to do."""
    tried = "; ".join(_short(x.get("what"), 90) for x in (f.get("fixes") or [])[-3:]) or "nothing could be tried"
    need = _need_line(f)
    text = f"Couldn't fix this myself in the last hour: {f.get('what')}. Tried: {tried}." + (f" {need}" if need else "")
    if dry:
        return "dry"
    _note("REFLEX escalated: " + text)
    if need and _window_ok():
        try:
            sys.path.insert(0, str(core.AGENT.parent / "scripts"))
            import cards
            cards.say("reflex", str((f.get("data") or {}).get("lane") or "personal"), need, thread=str(f.get("thread") or ""))
            return "chat"
        except Exception:  # noqa: BLE001
            pass
    return "note"


# ------------------------------------------------------------------ the tick
def in_hand(key: str) -> bool:
    f = (_load().get("findings") or {}).get(str(key)) or {}
    if not f or f.get("fixed") or f.get("escalated"):
        return False
    return time.time() - float(f.get("first") or 0) < 2 * VERIFY_MIN * 60


def tick(dry: bool = False, only: tuple = (), now: float | None = None) -> dict:
    """One pass: bind rules, detect, fix one step per open finding, verify, escalate. -> counts."""
    now = now or time.time()
    _PASS.clear()
    d = _load()
    bound = [] if dry else bind_rules()
    found = detect(only)
    keys = {f["key"] for f in found}
    out = {"found": 0, "fixed": 0, "steps": 0, "escalated": 0, "open": 0, "bound": len(bound)}
    for f in found:
        cur = d["findings"].get(f["key"])
        if cur and not cur.get("fixed"):
            cur.update(last_seen=now, what=f["what"], data=f["data"])
            continue
        if cur and cur.get("fixed") and now - float(cur.get("fixed") or 0) < REESCALATE_H * 3600:
            # it came back: the fix did not hold; carry on from the next step, not from scratch
            cur.update(fixed=None, fixed_when=None, last_seen=now, what=f["what"], data=f["data"], returned=int(cur.get("returned") or 0) + 1)
            continue
        d["findings"][f["key"]] = {**f, "first": now, "first_when": core.stamp(), "last_seen": now, "step": 0, "fixes": [], "fixed": None, "escalated": None}
        out["found"] += 1
        _log(f"found {f['key']}: {f['what']}")
    for key, f in list(d["findings"].items()):
        if f.get("fixed"):
            continue
        if only and f.get("kind", "").split("_")[0] not in only and key.split(":")[0] not in only:
            continue
        if key not in keys:
            f["fixed"], f["fixed_when"] = now, core.stamp()
            mins = (now - float(f.get("first") or now)) / 60
            d["stats"].append({"ts": now, "key": key, "kind": f.get("kind"), "outcome": "fixed", "minutes": round(mins, 1), "steps": len(f.get("fixes") or [])})
            out["fixed"] += 1
            _log(f"fixed {key} after {mins:.0f} min ({len(f.get('fixes') or [])} step(s))")
            if not dry:
                _note(f"REFLEX fixed {key} in {mins:.0f} min: " + "; ".join(_short(x.get("what"), 80) for x in f.get("fixes") or []))
            continue
        out["open"] += 1
        if f.get("escalated"):
            if now - float(f["escalated"]) >= REESCALATE.get(str(f.get("kind") or ""), REESCALATE_H) * 3600:
                f["escalated"] = None            # a day (a week for a system card) later it may be said again
            continue
        age_min = (now - float(f.get("first") or now)) / 60
        last_fix = float((f.get("fixes") or [{}])[-1].get("ts") or 0) if f.get("fixes") else 0
        fresh = not f.get("fixes")
        due_next = fresh or (now - last_fix >= VERIFY_MIN * 60)
        if not due_next:
            continue
        if f.get("fixes") and f["fixes"][-1].get("final"):
            f["escalated"] = now
            d["stats"].append({"ts": now, "key": key, "kind": f.get("kind"), "outcome": "escalated", "minutes": round(age_min, 1), "steps": len(f["fixes"])})
            out["escalated"] += 1
            if not dry:
                escalate(f, dry)
            continue
        fn = FIXES.get(str(f.get("kind") or ""))
        if not fn:
            f["fixes"].append({"ts": now, "what": "no fix for this kind", "ok": False, "final": True})
            continue
        try:
            r = fn(f, d, dry)
        except Exception as e:  # noqa: BLE001
            r = {"what": f"fix crashed: {type(e).__name__}: {str(e)[:100]}", "ok": False}
        if r.get("skip"):
            continue                                    # nothing tried this pass (a budget shared with another finding)
        f["fixes"].append({"ts": now, "what": r.get("what", ""), "ok": bool(r.get("ok")), "final": bool(r.get("final")), "build": r.get("build", "")})
        f["step"] = int(f.get("step") or 0) + 1
        out["steps"] += 1
        _log(f"{key} step {f['step']}: {r.get('what', '')}")
    if not dry:
        _save(d)
    return out


# ------------------------------------------------------------------ the reply, by code
_CARD_CLAIM = re.compile(r"[^.!?\n]*\b(?:card(?:'s| is| should be| will be)? (?:just )?(?:below|above|up|here)|on the card below|tap the card below|the card below)\b[^.!?\n]*[.!?]?", re.I)


def fix_reply(reply: str, applied: list, refused: list) -> str:
    """The brain said the card is below while the card was refused this turn (DRAFT_LINT, a guard) and handed on
    instead (2026-10-26 18:34, 'There's no approval card here in this thread. Where is it?'): the claim is replaced by
    the truth, by code. Only when no card was posted this turn and a refusal line exists."""
    text = str(reply or "")
    if not refused or any((a or {}).get("kind") == "card" for a in (applied or [])) or not _CARD_CLAIM.search(text):
        return text
    fixed = _CARD_CLAIM.sub("", text)
    fixed = re.sub(r"[ \t]{2,}", " ", fixed).strip()
    line = "The card didn't post on the first try, so I've handed the draft on; it comes here in a few minutes."
    return (fixed + ("\n\n" if fixed else "") + line) if line not in fixed else fixed


# ------------------------------------------------------------------ reading
def stats(days: int = 7) -> dict:
    cut = time.time() - days * 86400
    rows = [s for s in _load().get("stats") or [] if float(s.get("ts") or 0) >= cut]
    fixed = [s for s in rows if s.get("outcome") == "fixed"]
    in_hour = [s for s in fixed if float(s.get("minutes") or 0) <= VERIFY_MIN]
    esc = [s for s in rows if s.get("outcome") == "escalated"]
    open_ = [f for f in _load()["findings"].values() if not f.get("fixed")]
    found = len({s["key"] for s in rows} | {f["key"] for f in open_ if float(f.get("first") or 0) >= cut})
    pct = round(100.0 * len(in_hour) / found) if found else None
    return {"found": found, "fixed_in_hour": len(in_hour), "fixed_later": len(fixed) - len(in_hour), "escalated": len(esc), "open": len(open_), "pct_in_hour": pct}


def scorecard_bit(days: int = 7) -> str:
    s = stats(days)
    if not s["found"]:
        return ""
    return (f"{s['found']} fault{'s' if s['found'] != 1 else ''} I caught myself, {s['fixed_in_hour']} fixed within the hour"
            + (f", {s['fixed_later']} later" if s["fixed_later"] else "") + (f", {s['escalated']} needed you" if s["escalated"] else "")
            + (f", {s['open']} in hand" if s["open"] else ""))


def render() -> str:
    """The block in the brain's context: what is being fixed by code right now, so the agent never says 'I can't'
    about a job, an account or a build the reflex already has in hand."""
    d = _load()
    open_ = sorted([f for f in d["findings"].values() if not f.get("fixed")], key=lambda f: -float(f.get("first") or 0))[:5]
    bits = []
    for f in open_:
        last = (f.get("fixes") or [{}])[-1].get("what") if f.get("fixes") else "just found"
        bits.append(f"- {f.get('what')} (since {str(f.get('first_when') or '')[11:16]}; {_short(last, 110)})")
    mute = ", ".join(classes()[c]["what"] for c in d["muted"] if c in classes())
    if not bits and not mute:
        return ""
    head = "REFLEX (faults I am fixing by code myself; say so in one line if asked, never 'I can't'; nothing here needs a tap):"
    return "\n".join([head] + (bits or ["- nothing open"]) + ([f"- switched off at an owner's word: {mute}"] if mute else []))


def status() -> str:
    d = _load()
    s = stats(7)
    out = [f"reflex: {s['found']} found / {s['fixed_in_hour']} fixed in the hour / {s['fixed_later']} later / {s['escalated']} escalated / {s['open']} open (7 d)",
           "muted: " + (", ".join(f"{c} ({v.get('rule') or v.get('by')})" for c, v in d["muted"].items()) or "none"),
           "job budgets raised: " + (", ".join(f"{k}={v.get('timeout_s')}s" for k, v in d["job_overrides"].items()) or "none")]
    try:
        import voice
        held = voice.recent(24)
        out.append("voice (24 h): " + (", ".join(f"{k} x{n}" for k, n in sorted(held.items())) or "nothing held back"))
    except Exception:  # noqa: BLE001
        pass
    for f in sorted(d["findings"].values(), key=lambda f: -float(f.get("first") or 0))[:15]:
        st = "fixed " + str(f.get("fixed_when") or "")[5:16] if f.get("fixed") else ("escalated" if f.get("escalated") else f"open step {f.get('step')}")
        out.append(f"- {f['key']} [{st}] {_short(f.get('what'), 90)}" + ("".join(f"\n    {i + 1}. {_short(x.get('what'), 120)}" for i, x in enumerate(f.get("fixes") or []))))
    return "\n".join(out)


def replay(day: str) -> list:
    """What the detectors would have found in the transcript of one day (leak lines only; the other stores are live).
    A read-only look for the owner / the tests."""
    rows = [r for r in core.rows(core.TRANSCRIPT) if str(r.get("ts") or "").startswith(day)]
    agent = str(core.profile().get("name") or "").lower()
    out = []
    for r in rows:
        if str(r.get("who") or "").lower() in (agent, "card"):
            c = line_class(r.get("text"))
            if c:
                out.append({"ts": r.get("ts"), "cls": c, "text": _short(r.get("text"), 100)})
    return out


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    ap = argparse.ArgumentParser()
    ap.add_argument("cmd", choices=["status", "tick", "stats", "muted", "replay", "bind"])
    ap.add_argument("arg", nargs="?", default="")
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args()
    if a.cmd == "status":
        print(status())
    elif a.cmd == "tick":
        r = tick(dry=a.dry_run)
        print(f"RESULT REFLEX found={r['found']} fixed={r['fixed']} steps={r['steps']} escalated={r['escalated']} open={r['open']}")
    elif a.cmd == "stats":
        print(json.dumps(stats(int(a.arg or 7)), indent=1))
    elif a.cmd == "muted":
        print(json.dumps(muted(), indent=1))
    elif a.cmd == "bind":
        print(bind_rules())
    else:
        for x in replay(a.arg or time.strftime("%Y-%m-%d")):
            print(x)

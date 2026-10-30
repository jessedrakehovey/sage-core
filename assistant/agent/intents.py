"""intents.py - the agent's deeds as validated tool calls, not a line of prose.

Until 2026-10-21 everything the brain wanted changed rode on ONE trailing `SUGGEST {json}` line
parsed out of its reply by regex: a slipped quote dropped every deed ("I tried to save something
but the request was malformed"), and the model never learned whether a deed was accepted, so it
said "done" before anything ran and two more model turns existed only to police that.

Now each deed is a tool the model calls DURING its turn (gmcp.py, the per-turn MCP server):
  remember / learn / note / skill / org / schedule / followup / event / event_change /
  event_remove / task / call / do / card_revise
The tool validates the payload with the same rules actions.apply uses, appends the accepted
intent to this turn's file, and answers the model in plain words ("accepted: a card lands under
your reply, in this thread" / "refused: owner's calendar is not connected"), so the reply it
then writes is true by construction. NOTHING is applied inside the model's process: after the
turn the bridge reads the intents back (`collect`) as the same dict shape actions.apply has
always taken, and applies them through the same code.

Per turn: brain.ask -> begin() writes data/agent/ask/turns/<id>.json (who, where, the card in
this thread); the MCP server gets AGENT_TURN=<that path>; record() appends to <id>.intents.jsonl;
collect() folds them; the files are dropped after a day (sweep()).
"""
from __future__ import annotations

import json
import os
import re
import sys
import time
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import core  # noqa: E402

TURNS = core.DATA / "ask" / "turns"
KEEP_S = 86400

LIST_KINDS = {"learn", "unlearn", "remember", "forget", "schedule", "unschedule", "event", "event_change",
              "event_remove", "followups", "switch", "identify", "job", "approve", "blocked", "project"}
# tool name -> the SUGGEST key actions.apply reads (unchanged shapes, so nothing downstream moves)
KEY_OF = {"remember": "remember", "forget": "forget", "learn": "learn", "unlearn": "unlearn", "note": "note",
          "skill": "skill", "org": "org", "schedule": "schedule", "unschedule": "unschedule", "followup": "followups",
          "event": "event", "event_change": "event_change", "event_remove": "event_remove", "task": "task",
          "call": "call", "do": "do", "card_revise": "card", "addressed": "addressed", "decide": "decide",
          "switch": "switch", "identify": "identify", "job": "job", "approve": "approve", "blocked": "blocked", "project": "project"}
CAPS = {"learn": 3, "unlearn": 3, "remember": 5, "forget": 5, "schedule": 2, "unschedule": 3, "event": 8,
        "event_change": 4, "event_remove": 4, "followup": 4, "task": 3, "do": 3, "note": 2, "skill": 2, "decide": 2, "switch": 2,
        "identify": 5, "job": 2, "approve": 2, "blocked": 3, "project": 3}
PROJECT_OPS = ("open", "update", "park", "close", "resume")
# a project step that pays, moves money, deletes or handles a credential is never a job of the system's (the money lane never pays)
_PROJECT_OWNER_ONLY = re.compile(r"\b(?:pay|pays|paying|paid|transfer|wire|venmo|zelle|withdraw|purchase|buy|delete|erase|wipe|format|password|passcode|credential)\b", re.I)
# a change to the system itself sent as a `task` (2026-10-28: "Fix the safety hook" went to the operator twice, which never
# builds code, and "Results??" got only the silence): refused, so the brain files the `## Build request` note instead
_CODE_CHANGE = re.compile(r"\bbuild[- ]request\b|\bcode change\b|\b(?:safety|policy|tool) hook\b|\b[\w./-]+\.py\b", re.I)
BLOCKED_KINDS = ("capability", "signin", "fact", "tap", "connection")
# a decision card's buttons (cards.py: a label under 7 words and 46 chars, "(Recommended)" included)
DECIDE_OPTIONS = (2, 3)
DECIDE_LABEL_WORDS, DECIDE_LABEL_CHARS = 5, 30
def _channels() -> tuple:
    """Valid channel names, including any old aliases the CRM connector registers."""
    try:
        import alib
        aliases = alib.plugin_set("CHANNEL_ALIASES")
    except Exception:  # noqa: BLE001
        aliases = set()
    return ("text", "email", "service") + tuple(a for a in sorted(aliases) if a not in ("text", "email", "service"))


CHANNELS = _channels()   # built once at import; includes the connector's legacy channel aliases


def lanes() -> tuple:
    """The organization's lanes (org.json), with the default set as the floor so an old rule or a
    scaffolded org never fails validation."""
    base = ["wedding", "personal", "home", "money", "owner6"]
    try:
        import org
        for ln in (org.load().get("lanes") or []):
            k = str((ln or {}).get("key") or (ln or {}).get("name") or "").strip().lower()
            if k and k not in base:
                base.append(k)
    except Exception:  # noqa: BLE001
        pass
    return tuple(base)


def failed_send(qid: str = "", hours: float = 24.0) -> tuple:
    """(qid, ticket) of a card an owner approved whose send then failed: the card is consumed, its newest ticket is an
    exception and nothing for it is queued, running or done. Without a qid: the ONE such card failed in the last
    `hours` (none or several -> ("", {})). An UNVERIFIED failure may have landed, so then only a CRM message kind qualifies
    (its executor reads the client's thread in the service before it sends, so a landed message is never sent twice)."""
    sys.path.insert(0, str(core.AGENT.parent / "scripts"))
    import cards
    import exec_store as es
    by: dict = {}
    for tk in es.fold().values():
        ref = str(tk.get("approval_ref") or "")
        if ref.startswith("card:") and (not qid or ref[5:] == qid):
            by.setdefault(ref[5:], []).append(tk)
    hits, cutoff = [], time.time() - hours * 3600
    for q, tks in by.items():
        if any(x.get("status") in ("queued", "running", "done") for x in tks):
            continue
        last = max(tks, key=lambda x: str(x.get("ts_done") or x.get("ts") or ""))
        if last.get("status") != "exception" or (not qid and es._done_ts(last) < cutoff):
            continue
        _msg_kinds = set()
        try:
            import alib
            _msg_kinds = alib.plugin_set("MESSAGE_KINDS")
        except Exception:  # noqa: BLE001
            pass
        if "UNVERIFIED" in str(last.get("result") or "").upper() and last.get("kind") not in _msg_kinds:
            continue
        if (cards._read(cards.DONE / f"{q}.json") or {}).get("status") != "consumed" or cards._read(cards.LIVE / f"{q}.json"):
            continue
        hits.append((q, last))
    return hits[0] if len(hits) == 1 else ("", {})


# ------------------------------------------------------------------ the turn file
def skip_label(q: dict) -> str:
    """The button that closes a card without doing it: its own Skip / Cancel / No, else any option that is not the
    approve label, else "Skip"."""
    approve = str((q.get("exec") or {}).get("approve_label") or "").strip().lower()
    labels = [str(o.get("label") or "") for o in q.get("options") or [] if str(o.get("label") or "").strip().lower() != approve]
    return (next((x for x in labels if re.search(r"\b(skip|cancel|no|don'?t|drop|not now)\b", x, re.I)), "")
            or (labels[-1] if labels else "Skip"))


def begin(person: dict, convo: str, thread: str = "", card: dict | None = None, space: str = "") -> Path:
    """Open a turn: who is asking, where, and the open card (if any) this thread belongs to.
    -> the turn file's path (the value of AGENT_TURN for the model's tools)."""
    TURNS.mkdir(parents=True, exist_ok=True)
    tid = time.strftime("%Y%m%d-%H%M%S") + "-" + uuid.uuid4().hex[:6]
    p = TURNS / f"{tid}.json"
    core.save(p, {"id": tid, "ts": time.time(), "person": person.get("key", ""), "name": person.get("name", ""),
                  "convo": convo, "thread": thread or "", "space": space or "",
                  "card": (card or {}).get("id", "") if isinstance(card, dict) else ""})
    return p


def turn(path: str | Path | None = None) -> dict:
    p = Path(path or os.environ.get("AGENT_TURN") or "")
    if not str(p) or not p.exists():
        return {}
    return core.load(p, {}) or {}


def _intents_path(p: Path) -> Path:
    return p.with_suffix(".intents.jsonl")


def record(kind: str, payload, path: str | Path | None = None) -> dict:
    """The MCP tool's work: validate, append, answer. -> {"ok", "line"}; `line` is what the model
    reads. Never raises (a tool error would be a mystery to the model)."""
    t = turn(path)
    if not t:
        return {"ok": False, "line": "refused: this turn has no context to act in (no AGENT_TURN). Say what you meant to do in words instead."}
    p = Path(path or os.environ.get("AGENT_TURN"))
    person = core.person_for(handle=t.get("person", "")) or {}
    try:
        ok, line, payload = validate(kind, payload, person, t)
    except Exception as e:  # noqa: BLE001
        ok, line = False, f"refused: {type(e).__name__}: {str(e)[:160]}"
    if ok:
        n = sum(1 for r in core.rows(_intents_path(p)) if r.get("kind") == kind)
        cap = CAPS.get(kind, 4)
        if n >= cap:
            return {"ok": False, "line": f"refused: at most {cap} {kind} per reply; fold this into one of the ones you already put forward."}
        core.append(_intents_path(p), {"ts": core.stamp(), "kind": kind, "payload": payload})
    return {"ok": ok, "line": line}


def collect(path: str | Path) -> dict:
    """Every accepted intent of this turn, folded into the dict actions.apply takes."""
    p = Path(path)
    out: dict = {}
    for r in core.rows(_intents_path(p)):
        kind, pay = str(r.get("kind") or ""), r.get("payload")
        key = KEY_OF.get(kind)
        if not key:
            continue
        if key in LIST_KINDS:
            out.setdefault(key, []).append(pay)
        elif key in out and key in ("task", "do", "decide"):
            # a second task / do / decide in one reply: keep both (actions.apply loops over a list)
            cur = out[key]
            out[key] = (cur if isinstance(cur, list) else [cur]) + [pay]
        else:
            out[key] = pay
    return out


def count(path: str | Path) -> int:
    try:
        return len(core.rows(_intents_path(Path(path))))
    except Exception:  # noqa: BLE001
        return 0


def sweep(now: float | None = None) -> int:
    """Drop turn files older than a day. -> how many."""
    now = now or time.time()
    n = 0
    try:
        for f in TURNS.glob("*.json"):
            if now - f.stat().st_mtime > KEEP_S:
                for g in (f, _intents_path(f)):
                    try:
                        g.unlink(missing_ok=True)
                        n += 1
                    except OSError:
                        pass
    except OSError:
        pass
    return n


# ------------------------------------------------------------------ validation (the same rules actions.apply keeps)
def _s(v) -> str:
    return str(v or "").strip()


def _place(t: dict) -> str:
    return "in this thread" if t.get("thread") else ("here in this chat" if str(t.get("convo", "")).startswith("dm:") else "here in the space")


def _card_ticket(card: dict) -> dict:
    """The doer ticket a checkpoint card belongs to (exec.payload.ticket), or {}."""
    q = (card.get("questions") or [{}])[0] if isinstance(card, dict) else {}
    tid = _s(((q.get("exec") or {}).get("payload") or {}).get("ticket"))
    if not re.fullmatch(r"[0-9a-f]{16}", tid):
        return {}
    try:
        sys.path.insert(0, str(core.AGENT.parent / "scripts"))
        import tickets
        return tickets._load().get(tid) or {}
    except Exception:  # noqa: BLE001
        return {}


def own_ask_card(card: dict, person: dict, convo: str = "") -> bool:
    """A card the doer posted for a step of THIS owner's own chat ask is theirs to approve in words, like a card
    they asked for outright (the owner 2026-10-30 13:56, "Make the freaking link already" on the copy step of his own
    "text a dropbox link" ask, answered with "I can't approve that one for you, the system posted it"). The ticket
    records who asked (payload.from) and where (payload.convo); a card from anyone else's ask, or from the
    machinery, still needs the tap."""
    t = _card_ticket(card)
    p = t.get("payload") or {}
    if t.get("source") != "agent" or not _s(p.get("from")) or _s(p.get("from")).lower() != _s(person.get("key")).lower():
        return False
    return not convo or _s(p.get("convo")) == convo


def own_ask_cards(person: dict, convo: str, hours: float = 48.0) -> list:
    """Open cards (newest first) in this conversation that belong to the owner's own asks, within `hours`."""
    sys.path.insert(0, str(core.AGENT.parent / "scripts"))
    import cards
    out = []
    for c in cards._live():
        if c.get("status") != "open" or time.time() - float(c.get("ts") or 0) > hours * 3600:
            continue
        if own_ask_card(c, person, convo):          # a card asked for in a thread resolves through that thread
            out.append(c)
    out.sort(key=lambda c: -float(c.get("ts") or 0))
    return [str(c.get("id")) for c in out]


def validate(kind: str, payload, person: dict, t: dict) -> tuple:
    """-> (ok, line for the model, cleaned payload). Reads only; the one place a refusal is phrased."""
    if not isinstance(payload, dict):
        payload = {"text": _s(payload)} if kind in ("task", "do") else {}
    if kind in ("learn", "unlearn", "remember", "forget", "addressed", "org", "identify", "project") and not core.can(person, "teach"):
        return False, "refused: only an owner can change what I know or do.", payload
    if kind in ("note", "skill") and not core.can(person, "note"):
        return False, "refused: only an owner can file that.", payload
    if kind in ("schedule", "unschedule", "event", "event_change", "event_remove", "followup", "switch") and not core.can(person, "schedule"):
        return False, "refused: only an owner can set that.", payload
    if kind in ("task", "do", "call", "decide", "job") and not core.can(person, "task"):
        return False, "refused: only an owner can hand work on.", payload
    if not core.is_owner(person):
        # a staff member (role staff, 2026-10-24): task and note in their own lane; nothing that runs a browser, sets
        # a reminder, decides for an owner, teaches the agent, approves a card or files a build
        if kind not in ("task", "note"):
            return False, "refused: only an owner can do that; a team member can ask, hand a task on for the owner's tap, or file a note.", payload
        my_lanes = core.person_lanes(person) or []
        if my_lanes:
            payload["lane"] = my_lanes[0] if payload.get("lane") not in my_lanes else payload["lane"]
        if kind == "note" and re.search(r"(?im)^\s*(?:#+\s*)?build request\b", _s(payload.get("body"))):
            return False, "refused: a build request is the owners' to file. Say so in one line.", payload
    if kind == "card_revise" and not core.can(person, "approve"):
        return False, "refused: only an owner can revise a card.", payload
    if kind == "approve" and not core.can(person, "approve"):
        return False, "refused: only an owner can approve a card.", payload

    if kind == "learn":
        rule = _s(payload.get("rule"))
        if len(rule) < 8:
            return False, "refused: a rule needs one full sentence.", payload
        if core.is_protected(rule) and not core.tightens(rule):
            return False, ("refused: that would loosen a safety rule (sending, money, credentials, approvals), which only an owner "
                           "changes by hand on Mission Control. Say so in one line; do not say it is saved."), payload
        return True, "accepted: I'll hold to that from the next message on. Acknowledge it in your own words; no rule numbers.", payload
    if kind == "unlearn":
        rid = _s(payload.get("id"))
        if not re.fullmatch(r"r\d+", rid):
            return False, "refused: name the rule id (r12) from YOUR RULES.", payload
        return True, f"accepted: {rid} is retired after this reply.", {"id": rid}
    if kind == "remember":
        if not _s(payload.get("fact")):
            return False, "refused: a fact needs text.", payload
        if core.is_protected(_s(payload.get("fact"))):
            return False, "refused: that touches a safety rule; it is not for memory.", payload
        payload.setdefault("kind", "household")
        payload.setdefault("subject", _s(payload.get("fact"))[:40])
        return True, "accepted: remembered after this reply. Don't announce it; just use it.", payload
    if kind == "forget":
        mid = _s(payload.get("id"))
        if not re.fullmatch(r"m\d+", mid):
            return False, "refused: name the memory id (m4) from LONG-TERM MEMORY.", payload
        return True, f"accepted: {mid} is dropped after this reply.", {"id": mid}
    if kind == "note":
        if not _s(payload.get("title")) or not _s(payload.get("body")):
            return False, "refused: a note needs a title and a body.", payload
        if re.search(r"(?im)^\s*(?:#+\s*)?build request\b", _s(payload.get("body"))):
            return True, ("accepted: the build starts right after this reply (an owner's ask in chat is built at once, not queued); "
                          "code attaches the owner's ask as the acceptance test, reports the outcome in this conversation and brings "
                          "the ask back to you to finish when it lands. Say it is being built now, nothing about how; do every other "
                          "part of the ask in this same turn."), payload
        return True, "accepted: the note is filed in the wiki after this reply (as a draft, linted). Say in one clause where it went.", payload
    if kind == "skill":
        if not _s(payload.get("title")) or not _s(payload.get("body")):
            return False, "refused: a skill needs a title and the steps.", payload
        if core.is_protected(_s(payload.get("body"))):
            return False, ("refused: that how-to touches the protected core (footage handling, sending, pricing, the gates); a skill "
                           "cannot change it. If the owner wants a tool or a program built, it is a `note` with a '## Build request' "
                           "heading (the builder makes it, tested); say it is being built, not that a skill failed."), payload
        return True, "accepted: the skill is written after this reply.", payload
    if kind == "blocked":
        # the agent names its own blocker (capability.py, 2026-10-25): code files the build or records the portal
        if not core.is_owner(person):
            return False, "refused: only an owner's ask can be marked blocked.", payload
        bk = _s(payload.get("kind")).lower().replace("-", "").replace("_", "")
        bk = {"signin": "signin", "login": "signin", "password": "signin", "fact": "fact", "tap": "tap", "approval": "tap",
              "connection": "connection", "capability": "capability", "ability": "capability", "build": "capability"}.get(bk, "")
        if not bk or len(_s(payload.get("what"))) < 4 or len(_s(payload.get("why"))) < 8:
            return False, f"refused: blocked needs what, why and a kind from {', '.join(BLOCKED_KINDS)}.", payload
        payload["kind"] = bk
        if bk == "capability":
            return True, ("accepted: the build for that step starts right after this reply and the ask comes back to you when it lands. "
                          "Say in one clause that it is being built; do every other step now."), payload
        return True, ("accepted: recorded. Say Tried / Blocked because / Need from you in one line each, with the shortest way from the "
                      "phone; once. Do every other step now."), payload
    if kind == "org":
        if not payload:
            return False, "refused: nothing to record.", payload
        return True, "accepted: recorded about the organization after this reply.", payload
    if kind == "schedule":
        import routines
        when = payload.get("when")
        if not isinstance(when, dict) or not _s(payload.get("title")):
            return False, "refused: a schedule needs a title and a when ({\"daily\": \"07:30\"} / {\"weekly\": \"mon 07:30\"} / {\"once\": \"2026-10-12 09:00\"} / {\"every_hours\": 4}).", payload
        if not (_s(payload.get("say")) or _s(payload.get("prompt"))):
            return False, "refused: a schedule needs `say` (fixed text) or `prompt` (what to look up each time).", payload
        try:
            from datetime import datetime
            if routines.next_due(when, datetime.now()) is None:
                return False, "refused: that `when` never comes due; check the format.", payload
        except Exception as e:  # noqa: BLE001
            return False, f"refused: bad `when` ({str(e)[:80]}).", payload
        payload.setdefault("to", person.get("key", "both"))
        return True, f"accepted: scheduled after this reply ({routines.describe(when)}). Say it in one clause.", payload
    if kind == "unschedule":
        sid = _s(payload.get("id"))
        if not re.fullmatch(r"s\d+", sid):
            return False, "refused: name the schedule id (s2) from YOUR SCHEDULES.", payload
        return True, f"accepted: {sid} stops after this reply.", {"id": sid}
    if kind == "switch":
        import switches
        j = switches.find(_s(payload.get("job")))
        if not j:
            return False, "refused: no job by that name; use a key from JOBS YOU CAN SWITCH.", payload
        name = j.get("name", j["key"])
        run = payload.get("run")
        if isinstance(run, str):
            run = run.strip().lower() in ("true", "now", "yes", "1")
        if run is True and payload.get("on") in (None, ""):
            if not j.get("task"):
                return False, f"refused: {name} has no scheduled task to start; it runs on its own switch.", payload
            if switches.config_state(j) is False:
                return False, f"refused: {name} is switched off; turn it on first (switch {{job, on: true}}), then run it.", payload
            return True, f"accepted: {name} starts right after this reply, one run, schedule unchanged. Say it in one clause; its own report comes when it is done.", {"job": j["key"], "run": True}
        on = payload.get("on")
        if isinstance(on, str):
            on = on.strip().lower() in ("true", "on", "yes", "1")
        if not isinstance(on, bool):
            return False, "refused: say on (true) or off (false), or run: true to start it now.", payload
        if switches.state(j) == on:
            return True, f"accepted: {name} is already {'on' if on else 'off'}; nothing changes. Say so in one clause.", {"job": j["key"], "on": on}
        return True, f"accepted: {name} turns {'on' if on else 'off'} right after this reply. Say it in one clause.", {"job": j["key"], "on": on}
    if kind == "event":
        import google_api
        owner = payload.get("calendar") if payload.get("calendar") in (core.people().get("users") or {}) else person.get("key", "")
        if not _s(payload.get("title")) or not _s(payload.get("start")):
            return False, "refused: an event needs a title and a start (YYYY-MM-DD HH:MM, or YYYY-MM-DD with all_day).", payload
        if not google_api.accounts_of(owner):
            return False, f"refused: {owner}'s Google calendar is not connected (the Setup page has the button). Say so in one line; do not say it is on the calendar.", payload
        payload["calendar"] = owner
        return True, f"accepted: it goes on {owner}'s calendar right after this reply. Say it in one clause, as something you've put on.", payload
    if kind in ("event_change", "event_remove"):
        import actions
        found, why = actions.find_made_event(_s(payload.get("match")), _s(payload.get("date") or payload.get("start")))
        if not found:
            return False, "refused: " + why, payload
        return True, f"accepted: '{found['title']}' ({found['when']}) is {'changed' if kind == 'event_change' else 'removed'} after this reply.", payload
    if kind == "followup":
        if not (_s(payload.get("about")) and _s(payload.get("at")) and _s(payload.get("to"))):
            return False, "refused: a follow-up needs `to` (owner key), `about` and `at` (YYYY-MM-DDTHH:MM).", payload
        return True, "accepted: I'll check back then, only if it is still open.", payload
    if kind == "task":
        text = _s(payload.get("text"))
        if len(text) < 6:
            return False, "refused: say what the draft or change is, with the facts you found.", payload
        draft = payload.get("draft")
        if not (isinstance(draft, dict) and draft) and _CODE_CHANGE.search(text):
            return False, ("refused: an operator drafts messages, it never changes code, so this would get no answer. A change to the "
                           "system itself (a script, a hook, a safety rule) is a `note` with a '## Build request' heading: file that "
                           "now in this turn and say it is being built."), payload
        if payload.get("lane") not in lanes():
            payload["lane"] = _guess_lane(text)
        if isinstance(draft, dict) and draft:
            err = _check_draft(draft, _s(payload.get('text')))
            if err:
                return False, "refused: " + err, payload
            # the card's own guards, checked now: the tool never says "posted below" for a card cards.post will refuse
            try:
                import actions
                block = actions.draft_blocker(payload["lane"], text, draft, person)
            except Exception:  # noqa: BLE001
                block = ""
            if block:
                return False, ("refused: no card was posted, because " + block[:400] + " Tell the owner plainly that no card "
                               "was posted and why; fix the draft and call `task` again if you can."), payload
            ch = _s(draft.get("channel"))
            if draft.get("send_at"):
                sys.path.insert(0, str(core.AGENT.parent / "scripts"))
                import exec_store as es
                return True, (f"accepted: a {ch} card with exactly this wording is posted {_place(t)} right under your reply, for their tap; "
                              f"after the tap it goes out at {es.when_text(draft['send_at'])}, not before"
                              + (", and only if nothing came in from them by then (code checks their texts, mail and client service first)"
                                 if draft.get("hold_if_reply") else "") + ". In your reply say in one clause "
                              "that the card is below and when it will go out; never repeat the message text in chat."), payload
            return True, (f"accepted: a {ch} card with exactly this wording is posted {_place(t)} right under your reply, for their tap. "
                          "In your reply say in one clause that the card is below, and never repeat the message text in chat."), payload
        return True, ("accepted: an operator drafts it after this reply and the card lands " + _place(t) + " in a few minutes. "
                      "Say that in one clause ('you'll get the card here in a few minutes'). Prefer `draft` when you have the "
                      "facts: then the card is posted at once."), payload
    if kind == "job":
        sys.path.insert(0, str(core.AGENT.parent / "scripts"))
        import exec_store as es
        import jobs
        j = jobs.find(_s(payload.get("job")))
        if not j:
            return False, "refused: no job by that name; use a key from JOBS YOU CAN RUN.", payload
        args = payload.get("args") if isinstance(payload.get("args"), dict) else {}
        try:
            jobs.build_cmd(j, args)
        except ValueError as e:
            return False, f"refused: {e}.", payload
        at = ""
        if _s(payload.get("at")):
            try:
                at = es.parse_not_before(_s(payload.get("at")))
            except ValueError as e:
                return False, f"refused: {e}.", payload
        lane = payload.get("lane") if payload.get("lane") in lanes() else str(j.get("lane") or "personal")
        when = f"at {es.when_text(at)}" if at else "right after their tap"
        return True, (f"accepted: a card to run {jobs.describe(j, args)} {when} is posted {_place(t)} under your reply, for their tap. "
                      "Say it in one clause; the job reports in chat when it is done."), {"job": j["key"], "args": args, "at": at, "lane": lane}
    if kind == "call":
        if not _s(payload.get("with")):
            return False, "refused: who is the call with?", payload
        if payload.get("lane") not in lanes():
            payload["lane"] = "wedding"
        return True, f"accepted: a card with two or three open times goes {_place(t)} after this reply; their tap sends it. Never name times yourself.", payload
    if kind == "do":
        text = _s(payload.get("text"))
        if len(text) < 6:
            return False, "refused: say exactly what to do, where, with which values.", payload
        if payload.get("lane") not in lanes():
            payload["lane"] = _guess_lane(text)
        where = "computer" if _s(payload.get("where")).lower() == "computer" else "browser"
        payload["where"] = where
        return True, (f"accepted: the job starts within seconds ({where}); it reports back {_place(t)}. Say you've SENT it, never that it is done; "
                      "anything irreversible stops at a card."), payload
    if kind == "approve":
        # the owner 2026-10-23 22:51 "Send it at 8am" on the Marlo card; Sage: "I can't time a send". An owner's words in
        # the card's own thread are the approval (2026-10-24): the card is answered on them, with `at` as the ticket's
        # not_before. Only a card an owner ASKED for (asked / freshness_ack): a card the system posted by itself keeps
        # needing the tap, so a stray "send it" about something else can never fire it.
        qid = _s(payload.get("qid")) or _s(t.get("card"))
        skip = payload.get("skip") is True or _s(payload.get("skip")).lower() == "true"
        if not re.fullmatch(r"q[0-9a-f]{12}", qid) and not skip:
            qid, _ = failed_send()
        if not re.fullmatch(r"q[0-9a-f]{12}", qid) and not skip:
            # words at the top of the conversation, not in a card's thread ("Make the freaking link already" in the
            # DM, 2026-10-30): the one open card of the owner's own asks here is the card they mean
            mine = own_ask_cards(person, _s(t.get("convo")))
            if len(mine) == 1:
                qid = mine[0]
            elif len(mine) > 1:
                return False, f"refused: {len(mine)} cards from their own asks are open here; name the one (its header) or the card id.", payload
        if not re.fullmatch(r"q[0-9a-f]{12}", qid):
            return False, "refused: there is no open card in this thread to approve; post it first with `task`, or name the card id.", payload
        sys.path.insert(0, str(core.AGENT.parent / "scripts"))
        import cards
        import exec_store as es
        card = cards._read(cards.LIVE / f"{qid}.json")
        if (not card or card.get("status") != "open") and not skip:
            # the owner 2026-10-28 20:27 "Fix it and send" after the approved Teo & Wren follow-up failed:
            # Sage promised "the card here in a few minutes" and the operator reopened it in another thread. A card the
            # owner already approved whose send provably failed comes back and is approved on their words, same turn.
            fq, ft = failed_send(qid)
            if fq == qid:
                head = _s(((cards._read(cards.DONE / f"{qid}.json") or {}).get("questions") or [{}])[0].get("header")) or "the card"
                return True, (f"accepted: '{head}' failed last time ({_s(ft.get('result'))[:120]}); it is brought back and approved "
                              "on their words, and goes out right after this reply. Say that in one clause; never promise a card "
                              "and never say they have to tap."), {"qid": qid, "reopen": True, "why": _s(ft.get("result"))[:160],
                                                                   "words": _s(payload.get("words"))[:200]}
        if not card or card.get("status") != "open":
            return False, f"refused: card {qid} is no longer open (answered, expired or superseded).", payload
        q = (card.get("questions") or [{}])[0]
        ex = q.get("exec") or {}
        if skip:
            # the owner 2026-10-28 18:45 "he probably signed it already and I just have to find it" in the thread of the
            # Brian text card; Sage: "tap Skip on that card. I can't skip it for you." Skipping sends nothing, so an
            # owner's words close any open card in their thread, asked for or not; its own non-approve button is used.
            label = skip_label(q)
            return True, (f"accepted: '{_s(q.get('header')) or 'the card'}' is skipped on their words; nothing is sent. Say that in one "
                          "clause; never say they have to tap."), {"qid": qid, "skip": True, "option": label, "words": _s(payload.get("words"))[:200]}
        if not ex:
            return False, "refused: that card has no action to approve (a decision card): their answer comes back to you as a tap.", payload
        if not (card.get("asked") or _s(q.get("freshness_ack")) or own_ask_card(card, person)):
            return False, "refused: that card was posted by the system, not asked for in chat, so it needs their tap on the card itself. Say so in one line.", payload
        at = _s(payload.get("at"))
        if at:
            if re.fullmatch(r"\d{1,2}:\d{2}", at):
                from datetime import datetime, timedelta
                now = datetime.now()
                d = datetime.strptime(f"{now:%Y-%m-%d} {at.zfill(5)}", "%Y-%m-%d %H:%M")
                if d <= now:
                    d += timedelta(days=1)
                at = d.strftime("%Y-%m-%d %H:%M")
            try:
                at = es.parse_not_before(at)
            except ValueError as e:
                return False, f"refused: {e}.", payload
            if es.in_quiet(at):
                return False, f"refused: {at} is inside quiet hours (21:00-07:00); pick a time between 07:00 and 21:00.", payload
        hold = payload.get("hold_if_reply") is True or _s(payload.get("hold_if_reply")).lower() == "true"
        if hold and not at:
            return False, "refused: hold_if_reply needs `at`, the time to check by.", payload
        head = _s(q.get("header")) or "the card"
        when = f"it goes out at {es.when_text(at)}, not before" if at else "it goes out right after this reply (30 s undo)"
        if hold:
            when += ", and only if nothing came in from them by then (code checks their texts, mail and client service first)"
        return True, (f"accepted: '{head}' is approved on their words; {when}. Say that in one clause; never repeat the message text "
                      "and never say they have to tap."), {"qid": qid, "at": at, "hold_if_reply": hold, "words": _s(payload.get("words"))[:200]}
    if kind == "card_revise":
        qid = _s(payload.get("qid")) or _s(t.get("card"))
        new = _s(payload.get("text") or payload.get("revise"))
        if not re.fullmatch(r"q[0-9a-f]{12}", qid):
            return False, "refused: there is no open card in this thread to revise.", payload
        if len(new) < 2:
            return False, "refused: give the complete new message text.", payload
        sys.path.insert(0, str(core.AGENT.parent / "scripts"))
        import cards
        import chat_push
        card = cards._read(cards.LIVE / f"{qid}.json")
        if card and card.get("status") == "answered":
            # approved but not yet run: the revision recalls the ticket (actions.revise_card -> cards.undo)
            import exec_store as es
            tk = (cards._answers_index().get(qid) or {}).get("ticket")
            if not (tk and (es.fold().get(tk) or {}).get("status") == "queued"):
                return False, f"refused: card {qid} was approved and already went out; say what to send next and make a new card with `task`.", payload
        elif not card or card.get("status") != "open":
            return False, f"refused: card {qid} is no longer open.", payload
        key, _ = chat_push.exec_text((card.get("questions") or [{}])[0])
        if not key:
            return False, "refused: that card has no outgoing text; it is answered with a tap.", payload
        return True, "accepted: the revised card is posted in this thread after your reply and the old one retired. Say in one line what you changed.", {"qid": qid, "revise": new}
    if kind == "decide":
        return _validate_decide(payload, t)
    if kind == "addressed":
        to = _s(payload.get("to"))
        if not _s(payload.get("text")) or not (to in ("agent", "group") or to.startswith("person:")):
            return False, "refused: needs `text` (a few words of the message) and `to` (agent | group | person:<Name>).", payload
        return True, "accepted.", payload
    if kind == "identify":
        handle, name = _s(payload.get("handle")), _s(payload.get("name"))
        digits = re.sub(r"\D", "", handle)
        if not ("@" in handle and "." in handle) and len(digits) < 7:
            return False, "refused: `handle` must be the phone number or email, as it shows in the text log.", payload
        if len(name) < 2 or re.search(r"\d{4,}", name) or len(name) > 80:
            return False, "refused: `name` is what the owner calls them (a name, not a number).", payload
        if core.is_protected(name + " " + _s(payload.get("relation"))):
            return False, "refused: that is not a name.", payload
        return True, (f"accepted: from the next message on {name} is who {handle} is, in the texts and the records. "
                      "Say it naturally in a few words; no ids."), {"handle": handle, "name": name, "relation": _s(payload.get("relation"))[:80],
                                                                  "kind": _s(payload.get("kind"))}
    if kind == "project":
        return _validate_project(payload, person)
    return False, f"refused: unknown deed {kind!r}.", payload


def _validate_project(payload: dict, person: dict) -> tuple:
    """A long-running problem (projects.py, 2026-10-27): open needs a title and a goal; every other op names the project;
    steps carry a known kind; nothing here sends (a `task` or `owner` step still ends in a card or one line)."""
    import projects
    op = _s(payload.get("op")).lower()
    if op not in PROJECT_OPS:
        return False, f"refused: op must be one of {', '.join(PROJECT_OPS)}.", payload
    payload["op"] = op
    if payload.get("lane") and payload["lane"] not in lanes():
        payload["lane"] = _guess_lane(_s(payload.get("title")) + " " + _s(payload.get("goal")))
    nxt = payload.get("next")
    if nxt is not None:
        if not isinstance(nxt, dict) or len(_s(nxt.get("what"))) < 4:
            return False, "refused: `next` needs `what` (the step) and a `kind`.", payload
        k = _s(nxt.get("kind")).lower() or "doer"
        if k not in projects.KINDS:
            return False, f"refused: next.kind must be one of {', '.join(projects.KINDS)}.", payload
        nxt["kind"] = k
        if _s(nxt.get("due")) and not projects.parse_due(_s(nxt.get("due"))):
            return False, "refused: next.due is now, 2h, 3d, or YYYY-MM-DD HH:MM.", payload
        if k == "decide" and len([o for o in (nxt.get("options") or []) if _s(o)]) < 2:
            return False, "refused: a decide step needs 2-3 short options, the recommended one first.", payload
        if k in ("doer", "task") and _PROJECT_OWNER_ONLY.search(_s(nxt.get("what"))):
            return False, "refused: that step touches money, credentials or deleting; it is the owner's own (kind owner), never a job of mine.", payload
    for a in (payload.get("approaches") or []):
        if isinstance(a, dict) and a.get("kind") and _s(a.get("kind")).lower() not in projects.KINDS:
            a["kind"] = "doer"
    if op == "open":
        if len(_s(payload.get("title"))) < 4 or len(_s(payload.get("goal"))) < 4:
            return False, "refused: a project needs a `title` and a `goal` (what done looks like).", payload
        if not payload.get("lane"):
            payload["lane"] = _guess_lane(_s(payload.get("title")) + " " + _s(payload.get("goal")))
        ex = projects.find(_s(payload.get("title")))
        if ex:
            return True, (f"accepted: that is already on my board as '{ex['title']}' ({ex['id']}); what you gave folds into it after this reply. "
                          "Say where it stands in one clause from PROJECTS."), payload
        if not nxt and not payload.get("approaches"):
            return True, ("accepted: the project is on my board after this reply; it has no next step yet, so I will plan one at the next pass. "
                          "Better: give `next` now. Say in one clause that you are on it and what you will try first."), payload
        return True, "accepted: the project is on my board after this reply and the first step runs at its due time. Say in one clause what you will try first.", payload
    ref = _s(payload.get("id")) or _s(payload.get("title"))
    if not ref:
        return False, "refused: name the project (`id` from PROJECTS, or its title).", payload
    ex = projects.find(ref, live_only=(op != "resume"))
    if not ex:
        return False, f"refused: no project on my board matches {ref!r}; open it first.", payload
    payload["id"] = ex["id"]
    if op == "update":
        if not any(payload.get(k) for k in ("hypothesis", "goal", "solved_when", "note", "next", "approaches", "tried", "lane")):
            return False, "refused: an update changes something: hypothesis, goal, solved_when, note, next, approaches or tried.", payload
        return True, f"accepted: '{ex['title']}' is updated after this reply. Say the new thinking or the next step in one clause.", payload
    if op == "close":
        return True, f"accepted: '{ex['title']}' is closed as solved after this reply. Say so in one clause.", payload
    if op == "park":
        return True, f"accepted: '{ex['title']}' rests after this reply; say resume to pick it up. Say so in one clause.", payload
    return True, f"accepted: '{ex['title']}' is back on my board after this reply.", payload


_REC_TAG = re.compile(r"\s*\((recommended)[^)]*\)\s*$", re.I)


def plain_label(label: str) -> str:
    """A button label without its "(Recommended)" tag."""
    return _REC_TAG.sub("", _s(label)).strip()


def _validate_decide(payload: dict, t: dict) -> tuple:
    """A decision that is the owner's: 2-3 short buttons, one of them recommended (it goes first on the
    card), the decision in two sentences, one sentence why. The card has no exec: a tap only answers,
    and the answer comes back into the conversation, so nothing is sent by a decision button."""
    sys.path.insert(0, str(core.AGENT.parent / "scripts"))
    import cards
    header = _s(payload.get("header"))[:60]
    question = _s(payload.get("question"))
    if len(question) < 12:
        return False, "refused: `question` is the decision itself, in two sentences, the decision first.", payload
    if not header:
        header = question.split(".")[0][:60]
    if cards._REPLY_HEADER.match(header):
        return False, ("refused: a message to someone is not a decision card; put the wording in `task` with `draft` "
                       "(one message per card) instead."), payload
    raw = payload.get("options") if isinstance(payload.get("options"), list) else []
    opts = []
    for o in raw:
        if isinstance(o, str):
            o = {"label": o}
        if not isinstance(o, dict) or not _s(o.get("label")):
            continue
        opts.append({"label": plain_label(o.get("label")), "description": _s(o.get("description"))[:160]})
    lo, hi = DECIDE_OPTIONS
    if not (lo <= len(opts) <= hi):
        return False, f"refused: a decision card has {lo} or {hi} options, each a short button label.", payload
    long = [o["label"] for o in opts if len(o["label"].split()) > DECIDE_LABEL_WORDS or len(o["label"]) > DECIDE_LABEL_CHARS]
    if long:
        return False, (f"refused: {long[0]!r} is a sentence, not a button; a label is at most {DECIDE_LABEL_WORDS} words. "
                       "Put the explanation in that option's `description`."), payload
    labels = [o["label"].lower() for o in opts]
    if len(set(labels)) != len(labels):
        return False, "refused: two options have the same label.", payload
    rec = plain_label(payload.get("recommended"))
    if not rec or rec.lower() not in labels:
        return False, "refused: `recommended` must be exactly one of the option labels; a decision card always carries your recommendation.", payload
    if cards._PASSIVE.match(rec) or cards._NO_ACTION_OPT.match(rec):
        return False, "refused: the recommended option has to be something that happens on the tap, not a Skip or a 'got it'.", payload
    why = _s(payload.get("why"))
    if len(why) < 8:
        return False, "refused: `why` is one sentence on why you recommend that option.", payload
    if payload.get("lane") not in lanes():
        payload["lane"] = _guess_lane(f"{header} {question} {why}")
    clean = {"header": header, "question": question, "options": opts, "recommended": rec, "why": why,
             "lane": payload["lane"], "details": _s(payload.get("details"))[:1500]}
    return True, (f"accepted: the decision card is posted {_place(t)} right under your reply, your recommendation first; "
                  f"their tap comes back to you here and you act on it then. In your reply say in one clause that the card is below "
                  f"and that you'd go with {rec!r}. Do not ask the question in chat as well, and never end on 'would you like'."), clean


def _wedding_re():
    try:
        import crm as _crm_m
        crm_word = re.escape(_crm_m.name().lower())
    except Exception:  # noqa: BLE001
        crm_word = ""
    pat = r"\b(wedding|lead|couple|bride|groom|client|venue|film|shoot|inquir|retainer|proposal|contract|deliver|editor"
    if crm_word and crm_word not in ("the crm", ""):
        pat += "|" + crm_word
    pat += ")"
    return re.compile(pat, re.I)


_WEDDING = _wedding_re()


def _guess_lane(text: str) -> str:
    try:
        import org
        for ln in (org.load().get("lanes") or []):
            k = str(ln.get("key") or ln.get("name") or "").lower()
            words = [w for w in re.split(r"\W+", str(ln.get("about") or "").lower()) if len(w) > 4]
            if k and k != "personal" and any(w in str(text).lower() for w in words[:12]):
                return k
    except Exception:  # noqa: BLE001
        pass
    return "wedding" if _WEDDING.search(str(text or "")) else "personal"


def recent_text_to(to: str, hours: float = 24.0) -> dict:
    """The newest text this OS sent to `to` in the last `hours` (a done imessage_send ticket), or {}.
    Marlo got two near-identical texts 50 min apart on 2026-10-24 (one approved on the first card, one
    from a fresh card Sage drafted off a reminder without looking): a second draft to the same number in
    a day is refused unless the owner says it is a second text."""
    import time as _t
    sys.path.insert(0, str(core.AGENT.parent / "scripts"))
    try:
        import exec_store as es
        import phone_bridge
        want = phone_bridge.norm_handle(to)
    except Exception:  # noqa: BLE001
        return {}
    best = {}
    for tk in es.fold().values():
        if tk.get("kind") != "imessage_send" or tk.get("status") != "done":
            continue
        pay = tk.get("payload") or {}
        try:
            if phone_bridge.norm_handle(pay.get("to")) != want:
                continue
            done = _t.mktime(_t.strptime(str(tk.get("ts_done") or "")[:19], "%Y-%m-%dT%H:%M:%S"))
        except Exception:  # noqa: BLE001
            continue
        if _t.time() - done <= hours * 3600 and str(tk.get("ts_done")) > str(best.get("ts_done") or ""):
            best = {"ts_done": tk.get("ts_done"), "body": str(pay.get("body") or ""), "ticket": tk.get("id")}
    return best


_RESEND = re.compile(r"\b(resend|second text|again|follow[- ]?up|one more)\b", re.I)


def wrote_since(to: str, ts: str) -> bool:
    """True when `to` texted us after `ts` (our last text to them): a reply to their new message is a new turn,
    not a repeat (2026-10-29: a team member's new question needed 'resend' at the top of the draft to pass the same-day
    guard, and the card's preview began with it)."""
    sys.path.insert(0, str(core.AGENT.parent / "scripts"))
    try:
        import phone_bridge
        msgs = phone_bridge.thread(to, 40)["messages"]
    except Exception:  # noqa: BLE001
        return False
    return any(m.get("dir") == "in" and str(m.get("ts") or "")[:19] > str(ts or "")[:19] for m in msgs)


def _check_draft(d: dict, ask_text: str = "") -> str:
    """A draft the brain wrote itself: enough to post a card (cards.py validates the rest)."""
    ch = _s(d.get("channel")).lower()
    try:
        import alib
        _aliases = alib.plugin_set("CHANNEL_ALIASES")
    except Exception:  # noqa: BLE001
        _aliases = set()
    if ch in _aliases:
        ch = d["channel"] = "service"
    # a second text is allowed on the owner's words, the separate `resend` flag (never a word in the body), or
    # when the person wrote to us after our last text
    if ch == "text" and _s(d.get("to")) and not d.get("resend") and not _RESEND.search(ask_text):
        prev = recent_text_to(_s(d.get("to")))
        if prev and not wrote_since(_s(d.get("to")), str(prev.get("ts_done") or "")):
            import time as _t
            try:
                when = _t.strftime("%I:%M %p", _t.strptime(str(prev["ts_done"])[:19], "%Y-%m-%dT%H:%M:%S")).lstrip("0")
            except Exception:  # noqa: BLE001
                when = str(prev.get("ts_done"))[:16]
            return (f"a text already went to {d.get('to')} today at {when}: \"{prev['body'][:90]}\". Tell the owner that one went "
                    "and what it said; draft another only if they want a second text (then set draft.resend true; never a marker word in `text`).")
    if ch not in CHANNELS:
        return "draft.channel must be text, email or service."
    if len(_s(d.get("text"))) < 2:
        return "draft.text is the full message; it is empty."
    if ch == "email":
        if "@" not in _s(d.get("to")):
            return "draft.to must be their email address."
        if not _s(d.get("subject")):
            return "draft.subject is required for an email."
    if ch == "text":
        sys.path.insert(0, str(core.AGENT.parent / "scripts"))
        try:
            import phone_bridge
            if not phone_bridge.valid_handle(_s(d.get("to"))):
                return "draft.to must be one phone number (+1XXXXXXXXXX); look it up in Contacts or the text log first."
        except Exception:  # noqa: BLE001
            if not re.fullmatch(r"\+?\d{10,15}", re.sub(r"[\s().-]", "", _s(d.get("to")))):
                return "draft.to must be one phone number."
    if ch == "service":
        sys.path.insert(0, str(core.AGENT.parent / "scripts"))
        try:
            import crm as _crm_m
            tid = _s(d.get("thread_id") or d.get("workspace_id"))
            if not _crm_m.valid_thread(tid):
                return "draft.thread_id must be the client's CRM thread id (from the read-only connector)."
        except Exception:  # noqa: BLE001
            if not re.fullmatch(r"[0-9a-f]{24}", _s(d.get("thread_id") or d.get("workspace_id"))):
                return "draft.thread_id must be the client's CRM thread id (from the read-only connector)."
    if re.search(r"\[(?:owner1|OWNER|TODO|FILL)[^\]]*\]", _s(d.get("text")), re.I):
        return "the draft still has a blank in square brackets; fill it or ask the one question."
    if _s(d.get("send_at")):
        sys.path.insert(0, str(core.AGENT.parent / "scripts"))
        import exec_store as es
        try:
            sa = es.parse_not_before(_s(d.get("send_at")))
        except ValueError as e:
            return f"draft.send_at: {e}."
        if es.in_quiet(sa):
            return f"draft.send_at {sa} is inside quiet hours (21:00-07:00); pick a time between 07:00 and 21:00 or leave it out."
        d["send_at"] = sa
    if d.get("hold_if_reply") and not _s(d.get("send_at")):
        return "draft.hold_if_reply needs draft.send_at (the time to check by); for 'now' there is nothing to wait for."
    return ""

"""commitments.py - what the agent said it would get done, and whether it actually happened.

the owner 2026-10-09: "never saying that something was done and not being able to do it and then not
telling us later. Sage should always let us know if something was not done, with human follow-ups
and confirmations on work that we request." Before this, Sage could say "I'm sending the builder a
request" or "the operator will draft that and it'll come back as a card" and nothing checked
whether the request was ever built, the draft ever came back, or the doer ever finished. A build
that failed twice was dropped in silence; a build that landed was only written into the transcript.

Every piece of work the agent hands off from a chat turn becomes one row here, written by code
from what actions.apply REALLY queued (never from the model's words):

  task    an operator ticket      kept when the ticket is staged with a card or done
  do      a doer ticket           kept when done, or staged at a checkpoint card (the owner's turn)
  build   a wiki build request    kept when builds.json says built; needs_owner / failed / error
                                  are reported in the thread it was asked in

check() runs from the bridge every few minutes with no model: a row past its deadline that is not
kept gets ONE plain line in the conversation (and thread) it came from, in the agent's voice, and
a kept build gets its one-line confirmation there too (requested work is confirmed; nothing else
is). Each row is reported at most twice (first slip, then the give-up), then closed. Nothing here
sends to anyone outside the household; posting stays in the bridge.

  python assistant/agent/commitments.py            # open rows
  python assistant/agent/commitments.py --check    # evaluate without posting (prints what would be said)
"""
from __future__ import annotations

import json
import re
import secrets
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import core  # noqa: E402

LEDGER = core.DATA / "commitments.json"
# build requests whose "That's built now" line already went out, keyed on the request's title AND the
# build's commit, so a second row for the same build (chat + nightly review, 2026-10-10) cannot
# announce it twice. The builder's own combined "I built ..." note counts too (2026-10-10 18:25 it
# covered two builds, then each was announced again at 18:30): see _builder_said.
ANNOUNCED = core.DATA / "announced_builds.json"
FMT = "%Y-%m-%dT%H:%M"
# how long each kind may take before the agent says it has not happened (minutes); the operator
# ticks every 30 min in its window, the doer wakes at once, the builder runs hourly (4/day)
DEADLINE_MIN = {"task": 150, "do": 90, "build": 26 * 60}
SECOND_CHANCE_MIN = {"task": 240, "do": 120, "build": 24 * 60}
MAX_REPORTS = 1   # one slipped notice, never a second (the owner, 2026-10-20)
KEEP_DAYS = 14


def _load() -> dict:
    d = core.load(LEDGER, {"items": []})
    return d if isinstance(d, dict) and isinstance(d.get("items"), list) else {"items": []}


def _save(d: dict) -> None:
    cut = (datetime.now() - timedelta(days=KEEP_DAYS)).strftime(FMT)
    d["items"] = [it for it in d["items"] if it.get("status") == "open" or str(it.get("created") or "") >= cut][-400:]
    core.save(LEDGER, d)


def _window_ok(now: datetime) -> bool:
    """Say nothing in quiet hours: the row waits for morning."""
    q = core.profile().get("quiet_hours") or [21, 7]
    if now.hour >= q[0] or now.hour < q[1]:
        return False
    try:                      # /quiet until <time> (bridge.quiet_until)
        d = core.load(core.DATA / "quiet_until.json", {}) or {}
        if d.get("until") and now < datetime.strptime(d["until"], "%Y-%m-%dT%H:%M:%S"):
            return False
    except (ValueError, TypeError):
        pass
    return True


# ------------------------------------------------------------------ registering
def register(kind: str, what: str, ref: str, person: dict, ctx: dict | None = None, now: datetime | None = None) -> dict | None:
    """One row for work really handed off. `ref` is the ticket id or the build note's path."""
    if kind not in DEADLINE_MIN or not ref:
        return None
    now = now or datetime.now()
    d = _load()
    for it in d["items"]:
        if it.get("status") == "open" and it.get("kind") == kind and it.get("ref") == ref:
            return it                                      # the same hand-off twice is one promise
    row = {"id": "p-" + secrets.token_hex(4), "kind": kind, "what": re.sub(r"\s+", " ", str(what or "")).strip()[:240],
           "ref": str(ref), "by": str((person or {}).get("key") or ""), "convo": str((ctx or {}).get("convo") or ""),
           "thread": str((ctx or {}).get("thread") or ""), "created": now.strftime(FMT),
           "due": (now + timedelta(minutes=DEADLINE_MIN[kind])).strftime(FMT), "status": "open", "reports": 0}
    d["items"].append(row)
    _save(d)
    _outcome_open(row)
    return row


def register_applied(applied: list, person: dict, ctx: dict | None = None) -> list:
    """actions.apply hands over what it queued: [{"kind", "what", "ref"}]."""
    out = []
    for a in applied or []:
        r = register(str(a.get("kind") or ""), str(a.get("what") or ""), str(a.get("ref") or ""), person, ctx)
        if r:
            out.append(r)
    return out


def open_rows() -> list:
    return [it for it in _load()["items"] if it.get("status") == "open"]


def recent(hours: float = 26.0, now: datetime | None = None) -> list:
    """Closed rows from the last `hours`, for the nightly review (outcomes it should learn from)."""
    cut = ((now or datetime.now()) - timedelta(hours=hours)).strftime(FMT)
    return [it for it in _load()["items"] if it.get("status") != "open" and str(it.get("closed") or "") >= cut]


def _outcomes_mod():
    sys.path.insert(0, str(core.AGENT.parent / "scripts"))
    import outcomes
    return outcomes


def _outcome_open(row: dict) -> None:
    """Mirror the hand-off into the outcome ledger (outcomes.py, 2026-10-24): one row per claim, so /open and
    the scorecard see the agent's promises beside the executor's actions. Never raises."""
    try:
        _outcomes_mod().from_commitment(str(row.get("kind")), str(row.get("ref")), str(row.get("what")), by=str(row.get("by") or ""),
                                        convo=str(row.get("convo") or ""), thread=str(row.get("thread") or ""))
    except Exception:  # noqa: BLE001
        pass


def _outcome_close(it: dict, verdict: str, detail: str = "") -> None:
    try:
        _outcomes_mod().close_commitment(str(it.get("kind")), str(it.get("ref")), verdict, detail)
    except Exception:  # noqa: BLE001
        pass


# ------------------------------------------------------------------ looking up outcomes
def _ticket(tid: str) -> dict | None:
    try:
        sys.path.insert(0, str(core.AGENT.parent / "scripts"))
        import tickets
        return tickets._load().get(tid)
    except Exception:  # noqa: BLE001
        return None


def _last_note(t: dict) -> str:
    for h in reversed(t.get("history") or []):
        if h.get("note"):
            return str(h["note"])
    return ""


def _build_state(key: str) -> dict:
    try:
        import builder
        st = core.load(builder.STATE, {})
        return st.get(key) or {}
    except Exception:  # noqa: BLE001
        return {}


def _card_build_ask(key: str) -> bool:
    """A build waiting on an owner's yes or no with no card in front of them (a bare needs_owner, or a
    card that failed to post) gets the builder's own card, whose buttons the builder acts on. A plain
    line or a 'Got it' records nothing (2026-10-17). True when the card is up."""
    try:
        import builder
        if builder.running():
            return False
        st = core.load(builder.STATE, {})
        s = st.get(key)
        if not isinstance(s, dict) or not (s.get("status") == "needs_owner" or (s.get("status") == "asked" and not s.get("card"))):
            return False
        rel = key.replace("\\", "/")
        p = core.VAULT / rel
        wiki = rel.startswith("Wiki/")
        text = p.read_text(encoding="utf-8")[:6000] if wiki and p.exists() else ""
        req = {"key": key, "title": Path(rel).stem, "source": "wiki" if wiki else "pending", "text": text}
        question = str(s.get("question") or s.get("summary") or "")
        hits = [h for h in (s.get("hits") or builder._named_paths(question)) if h]
        qid = builder.ask_owner(req, s, hits, question)
        core.save(builder.STATE, st)
        return bool(qid)
    except Exception:  # noqa: BLE001
        return False


def outcome(it: dict, now: datetime | None = None) -> tuple:
    """-> (verdict, detail). verdict: kept | waiting | slipped | failed | reported | owner.
    'owner' = it is now waiting on the owner (a card is up): kept from the agent's side.
    'reported' = failed, but the doer's own report already said so in chat (closed quietly)."""
    now = now or datetime.now()
    kind, ref = it.get("kind"), str(it.get("ref") or "")
    if kind in ("task", "do"):
        t = _ticket(ref)
        if not t:
            return "failed", "it never made it into the queue"
        st, note = str(t.get("status") or ""), _last_note(t)
        if kind == "task" and st in ("done", "staged") and _REFUSED.search(note) and not re.search(r"\bcard q[0-9a-f]{12}\b", note, re.I):
            # the card was refused (the 24-hour recipient limit, 2026-10-21: Corwin's revised proposal never came and
            # nothing said so): no card is in front of the owner, so this is a failure the thread hears about at once
            return "failed", "the card was blocked: " + note
        if st == "done":
            return "kept", note
        if st == "dismissed":
            return "failed", note or "it was dismissed as noise"
        if st == "staged":
            if re.search(r"\bcard q[0-9a-f]{12}\b|checkpoint", note, re.I):
                return "owner", note
            if re.search(r"blocked|budget|browser|no result|no login|failed", note, re.I):
                last = (t.get("history") or [{}])[-1]
                if kind == "do" and last.get("by") == "doer":
                    return "reported", note                # the doer already told them; a second line is noise
                return "failed", note
            return "owner", note
        said = _answered_in_chat(it)
        if said:
            # 2026-10-23 22:55: Sage answered "Add Wren ... send the questionnaire" in the thread (Wren already on,
            # the rest blocked, "Need from you: ..."), the operator ticket stayed new, and at 07:00 the receipt said
            # "That draft never came back, so nothing went out". An ask already answered in its thread is with the
            # owner, not slipped.
            return "owner", "answered in chat: " + said[:160]
        return "slipped", f"still {st or 'waiting'}"      # new / claimed past the deadline
    if kind == "build":
        s = _build_state(ref)
        st = str(s.get("status") or "")
        if st == "built":
            return "kept", str(s.get("summary") or "")
        if st == "needs_owner":
            return "owner", str(s.get("question") or s.get("summary") or "")
        if st in ("asked", "approved", "retry"):
            # the owner has a 'Build needs your OK' card (or tapped it): nothing to say until it runs
            return ("owner", "") if st == "asked" else ("waiting", str(s.get("summary") or ""))
        if st == "dropped":
            return "kept", "dropped at the owner's word"
        if st == "not_code":
            return "failed", str(s.get("summary") or "it turned out not to be a code change")
        if st in ("failed", "error"):
            try:
                import builder
                cap = builder.MAX_ATTEMPTS
            except Exception:  # noqa: BLE001
                cap = 2
            if int(s.get("attempts", 0)) >= cap:
                return "failed", str(s.get("summary") or "")
            return "waiting", str(s.get("summary") or "")
        if st == "waiting":
            return "waiting", str(s.get("summary") or "")
        return "slipped", "the builder has not picked it up"
    return "waiting", ""


# a card refusal in a ticket's note (cards.py reason codes, or the operator's own words)
_REFUSED = re.compile(r"(?i:card (?:was )?(?:refused|blocked|didn't post|did not post|not posted))|"
                      r"\b(?:FRESHNESS_[A-Z_]+|DRAFT_LINT|NOT_A_DECISION|NEEDS_ACTION|NO_RECOMMENDATION|ONE_QUESTION|"
                      r"OPTIONS_ARE_DRAFTS|TEXT_NEEDS_DRAFT)\b")


# ------------------------------------------------------------------ the lines, in the agent's voice
_ANSWERED = re.compile(r"need from you|blocked because|tried:|i can't|i cannot|can't send|already on|nothing to add|there's nothing to", re.I)


def _answered_in_chat(it: dict) -> str:
    """The agent's own reply in this hand-off's thread, within 20 minutes of the hand-off, that already told the
    owner where it stands (blocked / need from you / already done). '' when there is none."""
    thread = str(it.get("thread") or "")
    if not thread:
        return ""
    try:
        t0 = datetime.strptime(str(it.get("created")), FMT)
    except (TypeError, ValueError):
        return ""
    owners = {str(v.get("name") or "").lower() for v in (core.people().get("users") or {}).values()}
    for r in core.rows(core.DATA / "transcript.jsonl"):
        if str(r.get("thread") or "") != thread or str(r.get("who") or "").lower() in owners | {"card", "code"}:
            continue
        try:
            ts = datetime.strptime(str(r.get("ts"))[:16], "%Y-%m-%d %H:%M")
        except (TypeError, ValueError):
            continue
        if t0 - timedelta(minutes=3) <= ts <= t0 + timedelta(minutes=20) and _ANSWERED.search(str(r.get("text") or "")):
            return re.sub(r"\s+", " ", str(r.get("text") or "")).strip()
    return ""


def _short(s: str, n: int = 140) -> str:
    s = re.sub(r"\s+", " ", str(s or "")).strip().rstrip(".")
    return s if len(s) <= n else s[:n].rsplit(" ", 1)[0] + "..."


def _sentence(s: str, n: int = 400) -> str:
    """The whole summary, not cut mid-sentence ('....', 2026-10-10); only a very long one is
    trimmed, at a sentence end where there is one."""
    s = re.sub(r"\s+", " ", str(s or "")).strip().rstrip(".")
    if len(s) <= n:
        return s
    cut = s[:n].rfind(". ")
    return s[:cut] if cut > n // 3 else s[:n].rsplit(" ", 1)[0] + "..."


_EMPTY_SUMMARY = re.compile(r"^(done|built|fixed|landed|shipped)?\s*(it\s*)?(in|from|during|by)?\s*(a|the)?\s*"
                            r"(claude|work|terminal|desktop|coding)?\s*(session|by hand|manually)?\s*"
                            r"(\(?\d{4}-\d{2}-\d{2}\)?)?\s*[.:]?\s*$", re.I)
_SESSION_PREFIX = re.compile(r"^(done|built)\s+in\s+a\s+(claude|work)\s+session\s*(\(?\d{4}-\d{2}-\d{2}\)?)?\s*[:,.-]\s*", re.I)


def _asked(it: dict) -> str:
    """The request in its own words, without the note-title prefix ("Build request ...")."""
    w = re.sub(r"^\s*build request[:\s-]*", "", str(it.get("what") or ""), flags=re.I).strip()
    return _short(w, 140)


# the builder states its assumptions in its summary ("..., assuming ...", "Assumed ..."): that is for the
# log, not the owner's chat line (2026-10-10: the 'Duplicate doer failure messages' line read robotic)
_ASSUMPTION = re.compile(r"\s*(?:[;,(-]\s*|\.\s+)(?:i\s+)?(?:assum|default(?:ed|s)?\s+to\b|chose\b|picked\b)[^.]*\)?", re.I)
_SHORT_DONE = 160


def _brief(s: str) -> str:
    """One short plain line from the builder's summary: its first sentence, without the assumptions,
    cut at a clause end when long. The full summary stays in builds.json and on Mission Control."""
    s = _ASSUMPTION.sub("", " " + s).strip()
    s = re.split(r"(?<=[a-z0-9)])\.\s+(?=[A-Z])", s, 1)[0].strip().rstrip(".")
    if len(s) <= _SHORT_DONE:
        return s
    cut = max(s[:_SHORT_DONE].rfind(m) for m in (", ", "; ", " - ", " so "))
    return s[:cut] if cut > 50 else _short(s, _SHORT_DONE)


def _what_changed(it: dict, detail: str) -> str:
    s = _SESSION_PREFIX.sub("", str(detail or "").strip())
    if s and not _EMPTY_SUMMARY.match(s) and len(_norm(s)) >= 25:
        s = _brief(s)
        return s[:1].upper() + s[1:]
    sha = str(_build_state(str(it.get("ref") or "")).get("commit") or "").strip()
    if sha:
        try:
            import subprocess
            msg = subprocess.run(["git", "log", "-1", "--format=%s", sha], cwd=str(core.VAULT),
                                 capture_output=True, text=True, timeout=10).stdout.strip()
            msg = re.sub(r"^[\w/ .-]{1,30}:\s+", "", msg)          # "assistant/builder: ..." -> the words
            if len(_norm(msg)) >= 15:
                msg = _brief(msg)
                return msg[:1].upper() + msg[1:]
        except Exception:  # noqa: BLE001
            pass
    return ""


def _allowed(cls: str) -> bool:
    """A code-posted line of this class may go out (reflex.py: an owner's rule switches a class off by code)."""
    try:
        import reflex
        return reflex.allowed(cls)
    except Exception:  # noqa: BLE001
        return True


def line_for(it: dict, verdict: str, detail: str) -> str:
    what = _short(it.get("what"), 110)
    kind = it.get("kind")
    if verdict == "kept":
        if kind == "build" and detail == "dropped at the owner's word":
            return ""
        if kind == "build" and not _allowed("build_landed"):
            return ""                                      # the ask itself is replayed and done (capability.py); no announcement
        if kind == "build":
            # Always name what was asked for, then what changed: "That's built now: done in a Claude
            # session." told the owner nothing (2026-10-19). A summary that only says where/when it was
            # built is replaced by the build's commit message, else left out.
            asked = _asked(it)
            done = _what_changed(it, detail)
            mine = str(it.get("by") or "") in (core.load(core.PEOPLE, {}).get("users") or {})
            # One short natural line (2026-10-10): what was asked, the gist of the change, no assumptions.
            line = (f"That's built now: \"{asked}\"." if mine
                    else f"I built a fix for this one: \"{asked}\".")
            if done and _norm(done) != _norm(asked):
                line += f" {done}{'' if done.endswith(('...', '.', '!', '?')) else '.'}"
            return line + " Say if anything's off."
        return ""                                          # a finished task/do already showed up as a card or a report
    if verdict == "owner":
        if kind == "build" and detail:
            return f"One thing I need from you before I can build \"{what}\": {_short(detail, 220)}"
        return ""                                          # a card is in front of them already
    if verdict == "reported":
        return ""                                          # one failure message, not two (2026-10-09)
    if verdict == "failed":
        why = _short(detail, 160)
        if kind == "build":
            if not _allowed("build_failed"):
                return ""
            return f"I couldn't get this built: {what}." + (f" What stopped it: {why}." if why else "") + " Tell me if you want it another way, or if it needs your yes."
        if kind == "do":
            return f"I didn't manage to get this done: {what}." + (f" {why[0].upper() + why[1:]}." if why else "") + " Ask me for the link and the steps and you can finish it from your phone, or tell me how you'd rather handle it."
        if detail.startswith("the card was blocked: "):
            return (f"The card for this didn't post, so there's nothing to tap yet: {what}. "
                    f"What blocked it: {_short(detail[len('the card was blocked: '):], 160)}. Ask me again here and I'll put it straight up.")
        return f"That didn't get done: {what}." + (f" ({why})" if why else "") + " Ask me again and I'll set it up properly."
    # slipped: ONE line, then silence (the owner, 2026-10-20: nothing that could not be done goes on a loop;
    # the chat is their portal). The first report is the only one; the item is closed as unkept right after.
    if int(it.get("reports", 0)) >= 1:
        return ""
    if kind == "build":
        if not _allowed("build_slipped"):
            return ""                                      # switched off at an owner's word (reflex.py, 2026-10-26 11:51)
        return (f"Still not built: {what}. If you still want it, say so once more and I'll file it again; "
                f"if it's something you'd rather set by hand, tell me where and I'll give you the link and the steps.")
    if kind == "do":
        return f"Still not done: {what}. Say the word and I'll start it fresh, or ask me for the link and the steps to finish it from your phone."
    return f"That draft never came back, so nothing went out: {what}. Ask me again and I'll redo it."


def _announce_keys(it: dict) -> list:
    """The build itself (its commit) as well as its title: either one already announced is enough."""
    keys = [Path(str(it.get("ref") or "").replace("\\", "/")).stem.strip().lower()]
    sha = str(_build_state(str(it.get("ref") or "")).get("commit") or "").strip()
    return [k for k in keys + (["commit:" + sha] if sha else []) if k]


def _norm(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", str(s or "").lower()).strip()


def _builder_said(detail: str) -> bool:
    """The builder's own note never reaches Chat any more (builder.note_owners writes the transcript and
    Mission Control only), so it is no longer an announcement: until 2026-10-21 this made a successful
    build vanish from the conversation it was asked in. announced_builds.json still keeps one line per build."""
    return False
    want = _norm(detail)[:80]
    if len(want) < 20:
        return False
    for r in core.rows(core.TRANSCRIPT)[-300:]:
        if r.get("channel") == "builder" and want in _norm(r.get("text")):
            return True
    return False


# ------------------------------------------------------------------ the tick
def check(post, now: datetime | None = None, dry: bool = False) -> list:
    """Evaluate every open row. post(convo, thread, text) delivers a line. Returns what was said."""
    now = now or datetime.now()
    if not _window_ok(now) and not dry:
        return []                                          # nothing at night; the morning tick says it
    d = _load()
    said, changed = [], False
    for it in d["items"]:
        if it.get("status") != "open":
            continue
        verdict, detail = outcome(it, now)
        due = str(it.get("due") or "") <= now.strftime(FMT)
        if verdict in ("waiting", "slipped") and not due:
            continue
        if verdict == "waiting":                           # past due but the system is still on it: give it the second window
            if int(it.get("reports", 0)) >= MAX_REPORTS:
                verdict, detail = "failed", detail or "it kept waiting"
            else:
                it["due"] = (now + timedelta(minutes=SECOND_CHANCE_MIN.get(it["kind"], 120))).strftime(FMT)
                it["reports"] = int(it.get("reports", 0)) + 1
                changed = True
                continue
        text = line_for(it, verdict, detail)
        if verdict == "owner" and it.get("kind") == "build" and not dry and _card_build_ask(str(it.get("ref") or "")):
            text = ""                                      # the card with its buttons is the ask
        akeys = _announce_keys(it) if it.get("kind") == "build" and verdict == "kept" else []
        announced = (core.load(ANNOUNCED, {}) if akeys else {}) or {}
        announced = announced if isinstance(announced, dict) else {}
        if akeys and (any(k in announced for k in akeys) or _builder_said(detail)):
            text = ""                                      # this build was already confirmed once
            if not dry and not all(k in announced for k in akeys):
                core.save(ANNOUNCED, {**announced, **{k: now.strftime(FMT) for k in akeys}})
        if text and not dry:
            try:
                _deliver(post, it, text)
            except Exception as e:  # noqa: BLE001
                text = ""
                it["post_error"] = f"{type(e).__name__}: {str(e)[:120]}"
            if text and akeys:
                core.save(ANNOUNCED, {**announced, **{k: now.strftime(FMT) for k in akeys}})
        if text:
            said.append({"id": it["id"], "verdict": verdict, "text": text, "convo": it.get("convo"), "thread": it.get("thread")})
        it["last_verdict"], it["detail"] = verdict, _short(detail, 300)
        if verdict == "slipped":
            it["reports"] = int(it.get("reports", 0)) + 1
            if it["reports"] >= MAX_REPORTS:
                it.update(status="unkept", closed=now.strftime(FMT))
                if not dry:
                    _outcome_close(it, "unkept", detail)
            else:
                it["due"] = (now + timedelta(minutes=SECOND_CHANCE_MIN.get(it["kind"], 120))).strftime(FMT)
        else:
            it.update(status={"kept": "kept", "owner": "with_owner", "failed": "failed", "reported": "failed"}[verdict], closed=now.strftime(FMT))
            if not dry and verdict != "owner":
                _outcome_close(it, verdict, detail)
        changed = True
    if changed and not dry:
        _save(d)
    return said


def _deliver(post, it: dict, text: str) -> None:
    """post(convo, thread, text[, ref]): a poster that takes the hand-off's reference (receipts.poster)
    edits the one line this hand-off owns; an older three-argument poster (tests) just posts."""
    import inspect
    args = (it.get("convo") or "", it.get("thread") or "", text)
    try:
        n = len(inspect.signature(post).parameters)
    except (TypeError, ValueError):
        n = 3
    if n >= 4:
        post(*args, str(it.get("ref") or ""))
    else:
        post(*args)


def render() -> str:
    rows = open_rows()
    if not rows:
        return "Nothing outstanding that I've promised."
    return "\n".join(f"{it['kind']}: {it['what'][:90]} (since {it['created'][5:16].replace('T', ' ')}, due {it['due'][5:16].replace('T', ' ')})"
                     for it in rows)


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    if "--check" in sys.argv:
        for s in check(lambda c, t, x: None, dry=True):
            print(json.dumps(s, ensure_ascii=False))
    else:
        print(render())

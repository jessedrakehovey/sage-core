"""projects.py - long-running problems the agent keeps working on its own, with a memory of what was tried. Built 2026-10-27.

the owner, 2026-10-27: "example has long term problem solving capabilities under projects. I want Sage to manage long term
issues and problem solve the same way." The reflex (reflex.py) is minutes-to-an-hour faults in the machinery; this is
days-to-months problems in the owners' life and business: a car registration that keeps failing, a film stuck in
review, ad leads that go quiet. Until now each of those lived in the chat and in the owner's head: the Elantra got
eleven doer jobs in one afternoon and nothing on record said which approaches were dead; the back-tax upload was
re-queued into the same wall twice.

One record per problem (assistant/data/agent/projects.json): title, goal, lane, status (open / waiting / done /
parked), the current hypothesis, every step tried with its outcome, the next step with a due time and a kind, the
untried approaches in the order to try them, what would count as solved, the owners' notes. Every change is a ledger
row with an undo (core.undo -> project_restore), like a rule or a memory.

  the deed   the brain opens, updates, parks or closes a project with the `project` tool (gmcp -> intents -> actions
             -> apply_deed): an owner describing a recurring or long-running problem becomes a project; "where are we
             on X" is answered from the PROJECTS block; a correction updates the hypothesis.
  the pass   tick(): zero-LLM, on the spine every 30 min plus the bridge's planning hook. A project whose next step is
             due is run through the existing hands: `doer` (an agent_run ticket), `task` (an operator ticket -> a card),
             `build` (a build request, the builder), `owner` (ONE plain line in chat through cards.say; the voice gate
             applies), `decide` (ONE card with buttons), `wait` (check back at the due time). The outcome is read back
             from the ticket, the builder's state, the card's answer or the owner's next word, and written on the step.
  the clock  a step past its wait fails ("no result by ..."); a failed step moves the project to the NEXT untried
             approach, never the same one again; a step the owner was asked for is asked once.
  planning   when a step ends and nothing says what comes next, ONE short brain turn (the bridge's minute tick,
             tick_plans) reads the case and sets the next step with the tool; it replies only when the owner should
             hear something. Capped per project and per day. Out of approaches and the turn set nothing -> escalate
             ONCE: one plain line (what was tried, the one thing that would move it) and the project waits on the owner.
             Nothing loops; nothing re-asks (README "blocked work becomes a portal").

Nothing here sends to anyone outside the household: a message is a `task` card, a browser job stops at its own
checkpoint card, a build goes through the builder's fences. Generic: no organization, person or service is named here.

  python assistant/agent/projects.py list | show <id> | tick [--dry-run] | status | context
                                      open --title .. --goal .. [--lane ..] [--hypothesis ..] [--solved-when ..] [--next "..." --kind doer|task|build|owner|decide|wait] [--opened YYYY-MM-DD]
                                      tried --id p1 --what ".." --result ".." [--ok] [--when "YYYY-MM-DD HH:MM"]
                                      set --id p1 [--hypothesis ..] [--goal ..] [--solved-when ..] [--note ..] [--next .. --kind ..] [--approach "what|kind"]...
                                      park --id p1 --why .. | close --id p1 --result .. | resume --id p1
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import core  # noqa: E402

STORE = core.DATA / "projects.json"
ADATA = Path(os.environ["ASSISTANT_DATA"]) if os.environ.get("ASSISTANT_DATA") else core.AGENT.parent / "data"
TICKETS = ADATA / "tickets.json"
STATUSES = ("open", "waiting", "done", "parked")
KINDS = ("doer", "task", "build", "owner", "decide", "wait")
# how long a hand gets before the clock fails the step (hours); a doer job at a checkpoint card gets a day
WAIT_H = {"doer": 4, "doer_checkpoint": 24, "task": 72, "build": 36, "owner": 72, "decide": 8 * 24, "wait": 0}   # a decision card rides the weekly digest
PLAN_GRACE_MIN = 25            # a planning turn has this long to set the next step before code moves on
PLAN_FALLBACK_H = 2            # no planning turn came at all (bridge down, caps hit): code takes the next approach itself
PLANS_PER_DAY = 12             # planning turns a day, every project together
PLANS_PER_PROJECT_DAY = 3
KEEP_DONE_DAYS = 60
MAX_TRIED = 60
MAX_NOTES = 30
FOOTER = "<!-- project:"


# ------------------------------------------------------------------ store
def _load() -> dict:
    d = core.load(STORE, {})
    if not isinstance(d, dict):
        d = {}
    d.setdefault("next_id", 1)
    d.setdefault("projects", {})
    d.setdefault("plans", {})          # pid -> {"ts", "day", "n_today"}: planning turns dispatched
    d.setdefault("stats", [])
    return d


def _save(d: dict) -> None:
    cut = time.time() - KEEP_DONE_DAYS * 86400
    for pid in list(d["projects"]):
        p = d["projects"][pid]
        if p.get("status") in ("done", "parked") and float(p.get("closed_ts") or 0) and float(p.get("closed_ts") or 0) < cut:
            d["projects"].pop(pid, None)
    d["stats"] = [s for s in d["stats"] if float(s.get("ts") or 0) >= cut][-500:]
    core.save(STORE, d)


def _short(s, n=200) -> str:
    s = re.sub(r"\s+", " ", str(s or "")).strip()
    return s if len(s) <= n else s[:n].rsplit(" ", 1)[0] + "..."


def _stamp(ts: float | None = None) -> str:
    return time.strftime("%Y-%m-%d %H:%M", time.localtime(ts if ts is not None else time.time()))


def _epoch(stamp: str) -> float:
    for fmt in ("%Y-%m-%d %H:%M", "%Y-%m-%dT%H:%M", "%Y-%m-%d"):
        try:
            return time.mktime(time.strptime(str(stamp or "")[:len(fmt) + 2], fmt))
        except ValueError:
            continue
    return 0.0


def parse_due(spec, now: float | None = None) -> float:
    """'' / 'now' -> now; '2h' '3d' '30m' '+1d' -> relative; 'YYYY-MM-DD HH:MM' / 'YYYY-MM-DD' (09:00) -> that time. 0 = unparseable."""
    now = now or time.time()
    s = str(spec or "").strip().lower()
    if not s or s == "now":
        return now
    m = re.fullmatch(r"\+?\s*(\d+)\s*(m|min|h|hr|hour|hours|d|day|days|w|week|weeks)", s)
    if m:
        n, u = int(m.group(1)), m.group(2)[0]
        return now + n * {"m": 60, "h": 3600, "d": 86400, "w": 7 * 86400}[u]
    e = _epoch(s)
    if e and len(s) <= 10:
        e += 9 * 3600              # a bare date is 09:00 that day
    return e


def _note(text: str) -> None:
    """A line on Mission Control only (never Chat): the pass's own record."""
    try:
        sys.path.insert(0, str(core.AGENT.parent / "scripts"))
        import cards
        cards.note("projects", _short(text, 500))
    except Exception:  # noqa: BLE001
        pass


def _log(msg: str) -> None:
    core.log_change("projects", _short(msg, 300), {"op": "none"}, "code")


# ------------------------------------------------------------------ records
def _lanes() -> tuple:
    try:
        import intents
        return intents.lanes()
    except Exception:  # noqa: BLE001
        return ("wedding", "personal", "home", "money")


def _step(what: str, kind: str, due: float, options=None, then=None) -> dict:
    kind = kind if kind in KINDS else "doer"
    s = {"what": _short(what, 400), "kind": kind, "due": float(due), "then": _then(then)}
    if kind == "decide":
        opts = [_short(o, 40) for o in (options or []) if str(o or "").strip()][:3]
        s["options"] = opts
    return s


def _then(then) -> dict | None:
    """What follows a step that succeeds: {"what", "kind"}; a bare string is a doer step."""
    if isinstance(then, dict):
        what, kind = str(then.get("what") or ""), str(then.get("kind") or "doer")
    else:
        what, kind = str(then or ""), "doer"
    what = _short(what, 300)
    return {"what": what, "kind": kind if kind in KINDS else "doer"} if len(what) >= 4 else None


def find(ref: str, d: dict | None = None, live_only: bool = True) -> dict | None:
    d = d or _load()
    ref = str(ref or "").strip()
    if not ref:
        return None
    if ref in d["projects"]:
        return d["projects"][ref]
    low = ref.lower()
    rows = [p for p in d["projects"].values() if not live_only or p.get("status") in ("open", "waiting")]
    hits = [p for p in rows if low in str(p.get("title") or "").lower()]
    if len(hits) == 1:
        return hits[0]
    sim = [p for p in rows if core.similar(p.get("title", ""), ref) or core.similar(p.get("goal", ""), ref)]
    if len(sim) == 1:
        return sim[0]
    # a few content words in common with one title only
    words = {w for w in core._norm(ref).split() if len(w) > 3}
    if words:
        scored = sorted(((len(words & set(core._norm(p.get("title", "")).split())), p) for p in rows), key=lambda x: -x[0])
        if scored and scored[0][0] >= 2 and (len(scored) == 1 or scored[1][0] < scored[0][0]):
            return scored[0][1]
    return None


def _snapshot(p: dict | None):
    return json.loads(json.dumps(p)) if p else None


def _ledger(pid: str, before: dict | None, summary: str, by: str) -> str:
    return core.log_change("project", _short(summary, 280), {"op": "project_restore", "id": pid, "before": before}, by)


def restore(pid: str, before: dict | None) -> bool:
    """core.undo: put the row back as it was (None = the project did not exist: it is removed)."""
    with core.locked:
        d = _load()
        if before is None:
            if pid not in d["projects"]:
                return False
            d["projects"].pop(pid, None)
        else:
            d["projects"][pid] = before
        _save(d)
        return True


def open_(title: str, goal: str, lane: str = "", by: str = "", hypothesis: str = "", solved_when: str = "",
          next_step: dict | None = None, approaches=(), ctx: dict | None = None, opened: str = "", notes=()) -> dict:
    """Open a project; a live one with the same title folds into an update instead (no duplicates)."""
    title = _short(title, 120)
    goal = _short(goal, 400)
    if len(title) < 4 or len(goal) < 4:
        return {"status": "refused", "reason": "a project needs a title and a goal"}
    with core.locked:
        d = _load()
        for p in d["projects"].values():
            if p.get("status") in ("open", "waiting") and (core.similar(p.get("title", ""), title) or p.get("title", "").lower() == title.lower()):
                return _update_locked(d, p["id"], by, hypothesis=hypothesis, solved_when=solved_when, next_step=next_step,
                                      approaches=approaches, notes=notes, folded=True)
        pid = f"p{d['next_id']}"
        d["next_id"] = int(d["next_id"]) + 1
        now = time.time()
        ctx = ctx or {}
        p = {"id": pid, "title": title, "goal": goal, "lane": lane if lane in _lanes() else "personal", "status": "open",
             "hypothesis": _short(hypothesis, 400), "solved_when": _short(solved_when, 300) or goal,
             "opened": opened[:16] if opened and _epoch(opened) else _stamp(now), "updated": _stamp(now), "by": by,
             "convo": str(ctx.get("convo") or ""), "thread": str(ctx.get("thread") or ""), "space": str(ctx.get("space") or ""),
             "notes": [], "tried": [], "next": None, "running": None, "approaches": [], "needs_plan": False,
             "owner_touched": now, "escalated": None, "closed_ts": None, "result": ""}
        for n in notes or []:
            if str(n or "").strip():
                p["notes"].append({"ts": _stamp(now), "by": by, "text": _short(n, 400)})
        for a in approaches or []:
            _add_approach(p, a)
        if isinstance(next_step, dict) and str(next_step.get("what") or "").strip():
            p["next"] = _step(next_step.get("what"), str(next_step.get("kind") or "doer"), parse_due(next_step.get("due"), now) or now,
                              next_step.get("options"), str(next_step.get("then") or ""))
        elif p["approaches"]:
            p["next"] = _pop_approach(p, now)
        else:
            p["needs_plan"] = True
        d["projects"][pid] = p
        _save(d)
        cid = _ledger(pid, None, f"project {pid} opened: {title}", by)
        d["stats"].append({"ts": now, "pid": pid, "event": "opened"})
        _save(d)
        return {"status": "applied", "id": pid, "change": cid, "project": p}


def _add_approach(p: dict, a) -> None:
    if isinstance(a, str):
        what, kind, opts = a, "doer", None
        if "|" in a:
            what, kind = a.rsplit("|", 1)
    elif isinstance(a, dict):
        what, kind, opts = a.get("what"), a.get("kind") or "doer", a.get("options")
    else:
        return
    what = _short(what, 400)
    if len(what) < 4:
        return
    if any(core.similar(x.get("what", ""), what) for x in p["approaches"]) or any(core.similar(t.get("what", ""), what) for t in p.get("tried") or []):
        return                                     # never the same approach twice
    p["approaches"].append({"what": what, "kind": kind.strip().lower() if kind in KINDS else "doer", **({"options": opts} if opts else {})})


def _pop_approach(p: dict, now: float) -> dict | None:
    while p["approaches"]:
        a = p["approaches"].pop(0)
        if any(core.similar(t.get("what", ""), a.get("what", "")) and not t.get("ok") for t in p.get("tried") or []):
            continue                               # tried and failed already: skip it
        return _step(a["what"], a.get("kind", "doer"), now, a.get("options"))
    return None


def update(ref: str, by: str = "", **fields) -> dict:
    with core.locked:
        d = _load()
        p = find(ref, d, live_only=False)
        if not p:
            return {"status": "refused", "reason": f"no project matches {ref!r}"}
        return _update_locked(d, p["id"], by, **fields)


def _update_locked(d: dict, pid: str, by: str, hypothesis: str = "", goal: str = "", solved_when: str = "", title: str = "", lane: str = "",
                   notes=(), next_step: dict | None = None, approaches=(), tried=(), status: str = "", result: str = "",
                   folded: bool = False, owner: bool = True) -> dict:
    p = d["projects"][pid]
    before = _snapshot(p)
    now = time.time()
    changed = []
    if owner and (p.get("running") or {}).get("kind") == "owner" and status not in ("done", "parked"):
        # the owner came back on it: that IS the outcome of the step that was theirs (said once, never chased)
        _record(p, p["running"], True, "the owner came back on it", by=by, ts=now)
        p["running"], p["status"], p["needs_plan"] = None, "open", True
        changed.append("their step done")
    if hypothesis and _short(hypothesis, 400) != p.get("hypothesis"):
        p["hypothesis"] = _short(hypothesis, 400)
        changed.append("hypothesis")
    if goal and _short(goal, 400) != p.get("goal"):
        p["goal"] = _short(goal, 400)
        changed.append("goal")
    if solved_when:
        p["solved_when"] = _short(solved_when, 300)
        changed.append("solved when")
    if title and len(_short(title, 120)) >= 4:
        p["title"] = _short(title, 120)
        changed.append("title")
    if lane and lane in _lanes():
        p["lane"] = lane
        changed.append("lane")
    for n in (notes if isinstance(notes, (list, tuple)) else [notes]):
        if str(n or "").strip():
            p["notes"].append({"ts": _stamp(now), "by": by, "text": _short(n, 400)})
            p["notes"] = p["notes"][-MAX_NOTES:]
            changed.append("note")
    for t in (tried if isinstance(tried, (list, tuple)) else [tried]):
        if isinstance(t, dict) and str(t.get("what") or "").strip():
            when = parse_due(t.get("when")) if t.get("when") else now
            _record(p, {"what": _short(t.get("what"), 400), "kind": str(t.get("kind") or "owner"), "due": when, "started": when},
                    bool(t.get("ok")), _short(t.get("result"), 400) or ("done" if t.get("ok") else "did not work"), by=by, ts=when)
            changed.append("tried")
    for a in (approaches if isinstance(approaches, (list, tuple)) else [approaches]):
        n0 = len(p["approaches"])
        _add_approach(p, a)
        if len(p["approaches"]) > n0:
            changed.append("approach")
    if isinstance(next_step, dict) and str(next_step.get("what") or "").strip():
        step = _step(next_step.get("what"), str(next_step.get("kind") or "doer"), parse_due(next_step.get("due"), now) or now,
                     next_step.get("options"), str(next_step.get("then") or ""))
        if p.get("running"):
            # a running step is not thrown away: the new one queues as the first approach, unless it is the same thing
            if not core.similar(p["running"].get("what", ""), step["what"]):
                p["approaches"].insert(0, {"what": step["what"], "kind": step["kind"], **({"options": step.get("options")} if step.get("options") else {})})
                changed.append("next (after the running step)")
        else:
            p["next"] = step
            p["needs_plan"] = False
            changed.append("next")
    if status in STATUSES and status != p.get("status"):
        p["status"] = status
        if status in ("done", "parked"):
            p["closed_ts"] = now
            p["result"] = _short(result, 400) or p.get("result", "")
            p["next"], p["running"], p["needs_plan"] = None, None, False
        else:
            p["closed_ts"] = None
        changed.append(status)
    elif result:
        p["result"] = _short(result, 400)
        changed.append("result")
    if owner:
        p["owner_touched"] = now
        if p.get("status") == "waiting" and status not in ("done", "parked"):
            # the owner spoke: a project waiting on them is open again and may be escalated once more later
            p["status"] = "open"
            p["escalated"] = None
            if not p.get("next") and not p.get("running"):
                p["next"] = _pop_approach(p, now)
                p["needs_plan"] = p["next"] is None
            changed.append("resumed")
    if not changed:
        return {"status": "folded" if folded else "unchanged", "id": pid, "project": p}
    p["updated"] = _stamp(now)
    _save(d)
    cid = _ledger(pid, before, f"project {pid} {'opened again' if folded else 'updated'} ({', '.join(changed)}): {p['title']}", by)
    return {"status": "folded" if folded else "applied", "id": pid, "change": cid, "changed": changed, "project": p}


def park(ref: str, why: str = "", by: str = "") -> dict:
    return update(ref, by=by, status="parked", result=why or "parked", notes=[why] if why else ())


def close(ref: str, result: str = "", by: str = "") -> dict:
    r = update(ref, by=by, status="done", result=result or "solved")
    if r.get("status") == "applied":
        with core.locked:
            d = _load()
            d["stats"].append({"ts": time.time(), "pid": r["id"], "event": "done"})
            _save(d)
    return r


def resume(ref: str, by: str = "") -> dict:
    return update(ref, by=by, status="open")


def _record(p: dict, step: dict, ok: bool, result: str, by: str = "code", ts: float | None = None) -> dict:
    ts = ts or time.time()
    row = {"n": len(p.get("tried") or []) + 1, "what": step.get("what", ""), "kind": step.get("kind", ""), "ok": bool(ok),
           "result": _short(result, 400), "ref": str(step.get("ref") or ""), "started": _stamp(step.get("started") or ts), "ts": _stamp(ts), "by": by}
    p.setdefault("tried", []).append(row)
    p["tried"] = p["tried"][-MAX_TRIED:]
    p["updated"] = _stamp(ts)
    return row


# ------------------------------------------------------------------ the deed (gmcp -> intents -> actions)
def apply_deed(pr: dict, person: dict, ctx: dict | None = None) -> str:
    """One `project` intent from the brain. Returns a line only when something went wrong."""
    op = str(pr.get("op") or "").strip().lower()
    by = str(person.get("key") or "")
    try:
        if op == "open":
            r = open_(str(pr.get("title") or ""), str(pr.get("goal") or ""), str(pr.get("lane") or ""), by=by,
                      hypothesis=str(pr.get("hypothesis") or ""), solved_when=str(pr.get("solved_when") or ""),
                      next_step=pr.get("next") if isinstance(pr.get("next"), dict) else None,
                      approaches=pr.get("approaches") if isinstance(pr.get("approaches"), list) else (),
                      ctx=ctx or {}, notes=[pr.get("note")] if pr.get("note") else (), opened=str(pr.get("opened") or ""))
        elif op in ("update", "park", "close", "resume"):
            ref = str(pr.get("id") or pr.get("title") or "")
            if op == "update":
                r = update(ref, by=by, hypothesis=str(pr.get("hypothesis") or ""), goal=str(pr.get("goal") or ""),
                           solved_when=str(pr.get("solved_when") or ""), title=str(pr.get("new_title") or ""), lane=str(pr.get("lane") or ""),
                           notes=[pr.get("note")] if pr.get("note") else (), next_step=pr.get("next") if isinstance(pr.get("next"), dict) else None,
                           approaches=pr.get("approaches") if isinstance(pr.get("approaches"), list) else (),
                           tried=pr.get("tried") if isinstance(pr.get("tried"), list) else ())
            elif op == "park":
                r = park(ref, str(pr.get("why") or pr.get("note") or ""), by=by)
            elif op == "close":
                r = close(ref, str(pr.get("result") or pr.get("note") or ""), by=by)
            else:
                r = resume(ref, by=by)
        else:
            return f"I couldn't record that project change (unknown op {op!r})."
    except Exception as e:  # noqa: BLE001
        return f"I couldn't record that project change ({type(e).__name__})."
    if r.get("status") == "refused":
        return f"I couldn't record that project change ({r.get('reason')})."
    return ""


# ------------------------------------------------------------------ the hands
def _owner_person(p: dict) -> dict:
    return core.person_for(handle=str(p.get("by") or "")) or {"key": core.first_owner(), "name": core.first_owner().title()}


def _ctx(p: dict) -> dict:
    return {"convo": p.get("convo") or f"dm:{core.first_owner()}", "thread": p.get("thread") or "", "space": p.get("space") or ""}


def _run_doer(p: dict, step: dict) -> dict:
    sys.path.insert(0, str(core.AGENT.parent / "scripts"))
    import tickets
    person = _owner_person(p)
    text = f"{step['what']}\n\nThis is a step of a long-running project: {p['title']}. Goal: {p['goal']}." + \
           (f" Current thinking: {p['hypothesis']}." if p.get("hypothesis") else "") + \
           (" Already tried: " + "; ".join(f"{t['what']} -> {t['result']}" for t in (p.get("tried") or [])[-4:]) + "." if p.get("tried") else "")
    sid = f"project|{p['id']}|{time.time():.0f}|{step['what'][:40]}"
    ctx = _ctx(p)
    n = tickets.add_many([{"source": "agent", "source_id": sid, "lane": p["lane"], "kind": "agent_run",
                           "title": f"{person.get('name', 'Owner')}: {step['what'][:120]}", "summary": step["what"][:600],
                           "payload": {"text": text, "via": core.profile().get("slug", "agent"), "from": person.get("key", ""), "skill": "",
                                       "where": "computer" if re.search(r"\b(?:on (?:the|this) (?:pc|computer)|in resolve|the editor)\b", step["what"], re.I) else "",
                                       "convo": ctx["convo"], "thread": ctx["thread"], "space": ctx["space"], "project": p["id"]}}], "agent")
    if n <= 0:
        return {"ok": False, "why": "already queued"}
    try:
        sys.path.insert(0, str(core.AGENT.parent / "doer"))
        import doer
        doer.wake()
    except Exception:  # noqa: BLE001
        pass
    return {"ok": True, "ref": tickets.make_id("agent", sid)}


def _run_task(p: dict, step: dict) -> dict:
    sys.path.insert(0, str(core.AGENT.parent / "scripts"))
    import tickets
    person = _owner_person(p)
    sid = f"project|{p['id']}|{time.time():.0f}|{step['what'][:40]}"
    ctx = _ctx(p)
    text = f"{step['what']} (a step of the long-running project '{p['title']}': {p['goal']})"
    n = tickets.add_many([{"source": "agent", "source_id": sid, "lane": p["lane"], "kind": "owner1_request",
                           "title": f"{person.get('name', 'Owner')} asked: {text[:120]}", "summary": text[:600],
                           "payload": {"text": text, "via": core.profile().get("slug", "agent"), "from": person.get("key", ""),
                                       "convo": ctx["convo"], "thread": ctx["thread"], "space": ctx["space"], "asked": True, "project": p["id"]}}], "agent")
    return {"ok": n > 0, "ref": tickets.make_id("agent", sid), "why": "" if n > 0 else "already queued"}


def _run_build(p: dict, step: dict) -> dict:
    try:
        import builder
        import wiki_writer
        title = _short(step["what"], 90)
        for r in builder.collect():
            if core.similar(str(r.get("title") or ""), title) or f"{FOOTER} {p['id']}" in str(r.get("text") or ""):
                return {"ok": True, "ref": str(r.get("key") or "")}
        text = ("## Build request\n\n**Ask (a project step, by code, " + core.stamp() + "):** " + step["what"] + "\n\n"
                f"Part of the long-running project '{p['title']}'. Goal: {p['goal']}." + (f" Current thinking: {p['hypothesis']}." if p.get("hypothesis") else "")
                + ("\n\nAlready tried: " + "; ".join(f"{t['what']} -> {t['result']}" for t in (p.get("tried") or [])[-4:]) if p.get("tried") else "")
                + "\n\n**Acceptance:** the step above can be done by the system and a test drives it.\n\n"
                + f"{FOOTER} {json.dumps({'pid': p['id'], 'step': step['what'][:80]}, ensure_ascii=False)} -->\n")
        x = wiki_writer.write({"title": title, "type": "workflow", "lane": p["lane"] if p["lane"] in ("personal", "home", "money", "wedding") else "personal", "body": text}, by="projects")
        if x.get("status") not in ("applied", "appended"):
            return {"ok": False, "why": str(x.get("reason") or "the note did not save")}
        key = str(x.get("path") or "")
        try:
            builder.kick(force=True, first=key)
        except Exception:  # noqa: BLE001
            pass
        return {"ok": True, "ref": key}
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "why": f"{type(e).__name__}: {str(e)[:100]}"}


def _window_ok() -> bool:
    q = core.profile().get("quiet_hours") or [21, 7]
    h = time.localtime().tm_hour
    return not (h >= q[0] or h < q[1])


def _say(p: dict, text: str) -> bool:
    """ONE plain line to the owners, in the project's thread when it has one; the voice gate applies in cards.say.
    Tests replace this."""
    try:
        sys.path.insert(0, str(core.AGENT.parent / "scripts"))
        import cards
        r = cards.say("projects", p.get("lane") or "personal", text, thread=str(p.get("thread") or ""))
        return bool(r.get("said"))
    except Exception as e:  # noqa: BLE001
        _log(f"say failed for {p.get('id')}: {type(e).__name__}: {str(e)[:100]}")
        return False


def _owner_line(p: dict, step: dict) -> str:
    what = step["what"].rstrip(".")
    t = p["title"]
    if any(w in what.lower() for w in core._norm(t).split() if len(w) > 3):
        return what + "."
    return f"{t}: {what}."


def _card(p: dict, step: dict) -> dict:
    """ONE decision card with the step's options, the first one recommended; a tap only records the answer. Tests replace this."""
    sys.path.insert(0, str(core.AGENT.parent / "scripts"))
    import cards
    opts = list(step.get("options") or [])
    if len(opts) < 2:
        opts = (opts + ["Yes, go ahead", "Not now"])[:2]
    options = [{"label": f"{opts[0]} (Recommended)"}] + [{"label": o} for o in opts[1:3]]
    tried = "; ".join(f"{t['what']} -> {t['result']}" for t in (p.get("tried") or [])[-4:])
    # not urgent: a project's decision is an ordinary approval, so it rides the weekly digest (the owner's rule, 2026-10-20) and is
    # open on Mission Control at once; its clock (WAIT_H decide) outlasts one digest cycle
    q = {"question": step["what"], "header": _short(p["title"], 58), "lane": p["lane"],
         "audience": "client" if p["lane"] not in ("personal", "home", "money") else "personal",
         "details": f"Part of the long-running project '{p['title']}'. Goal: {p['goal']}." + (f" Tried so far: {tried}" if tried else ""),
         "options": options, "project": p["id"]}
    return cards.post([q], owner="projects", lane=p["lane"], thread=str(p.get("thread") or ""), space=str(p.get("space") or ""))


def _start(p: dict, step: dict, now: float, dry: bool) -> str:
    """Hand the due step to its hand. Returns a short what-happened line."""
    kind = step["kind"]
    step["started"] = now
    if dry:
        return f"would start {kind}: {_short(step['what'], 80)}"
    if kind == "doer":
        r = _run_doer(p, step)
    elif kind == "task":
        r = _run_task(p, step)
    elif kind == "build":
        r = _run_build(p, step)
    elif kind == "owner":
        if not _window_ok():
            step["started"] = None
            return "owner line waits for the day window"
        ok = _say(p, _owner_line(p, step))
        r = {"ok": ok, "ref": "said", "why": "" if ok else "the line did not post"}
    elif kind == "decide":
        try:
            x = _card(p, step)
        except Exception as e:  # noqa: BLE001
            x = {"posted": False, "reason": f"{type(e).__name__}: {str(e)[:100]}"}
        r = {"ok": bool(x.get("posted")), "ref": str(x.get("qid") or ""), "why": str(x.get("reason") or "")}
    else:                                          # wait: nothing to do until due
        r = {"ok": True, "ref": "wait"}
    if not r.get("ok"):
        _record(p, step, False, f"could not start: {r.get('why') or 'unknown'}", ts=now)
        p["next"] = None
        _advance(p, now, failed=True)
        return f"{kind} step could not start ({r.get('why')})"
    step["ref"] = str(r.get("ref") or "")
    p["running"], p["next"] = step, None
    p["status"] = "waiting" if kind in ("owner", "decide", "wait") else "open"
    return f"started {kind}: {_short(step['what'], 80)}"


# ------------------------------------------------------------------ reading outcomes
def _iso_epoch(s) -> float:
    s = str(s or "")
    try:
        if s.endswith("Z"):
            import calendar
            return calendar.timegm(time.strptime(s[:19], "%Y-%m-%dT%H:%M:%S"))
        return time.mktime(time.strptime(s[:19], "%Y-%m-%dT%H:%M:%S"))
    except ValueError:
        return 0.0


def _ticket_outcome(ref: str, kind: str, now: float, started: float) -> tuple:
    """-> (settled, ok, result). Reads tickets.json for a doer / operator ticket."""
    db = core.load(TICKETS, {}) or {}
    t = db.get(ref) if isinstance(db, dict) else None
    if not t:
        return (True, False, "the job was never queued") if now - started > 600 else (False, False, "")
    hist = t.get("history") or []
    note = str((hist[-1] if hist else {}).get("note") or "")
    st = str(t.get("status") or "")
    if st == "done":
        if re.match(r"\s*duplicate of", note, re.I):
            return True, False, _short(note, 300)
        return True, True, _short(re.sub(r"^(?:DOER_DONE|DOER_ANSWER|DONE)\s*:?\s*", "", note) or "done", 400)
    if st == "dismissed":
        return True, False, _short(note or "dismissed", 300)
    if st == "staged":
        if re.match(r"\s*blocked\s*:", note, re.I):
            return True, False, _short(re.sub(r"^\s*blocked\s*:\s*", "", note, flags=re.I), 400)
        if re.search(r"session limit|usage limit|no result:", note, re.I):
            return False, False, ""                # the reflex re-queues it; the clock is the backstop
        # a checkpoint card / a card for the owner: give it the longer wait
        lim = WAIT_H["doer_checkpoint" if kind == "doer" else "task"] * 3600
        if now - started > lim:
            return True, False, f"no answer on its card by {_stamp(now)}"
        return False, False, ""
    if now - started > WAIT_H[kind] * 3600:            # new / claimed for too long: the hand never got to it
        return True, False, f"no result by {_stamp(now)}"
    return False, False, ""


def _build_outcome(ref: str, now: float, started: float) -> tuple:
    try:
        import builder
        s = (core.load(builder.STATE, {}) or {}).get(ref) or {}
    except Exception:  # noqa: BLE001
        s = {}
    st = str(s.get("status") or "")
    if st == "built":
        return True, True, _short(s.get("summary") or "built", 400)
    if st in ("failed", "error", "dropped", "not_code"):
        return True, False, _short(s.get("summary") or st, 400)
    if st in ("needs_owner", "asked"):
        return (True, False, "the build waited on an OK that never came") if now - started > WAIT_H["build"] * 3600 * 2 else (False, False, "")
    return False, False, ""


def _card_outcome(qid: str, now: float, started: float) -> tuple:
    try:
        sys.path.insert(0, str(core.AGENT.parent / "scripts"))
        import cards
        a = cards._answers_index().get(qid)
        if a:
            return True, True, f"picked: {_short(a.get('option') or a.get('answer') or '', 80)}" + (f" ({_short(a.get('text'), 160)})" if a.get("text") else "")
        live = (cards.LIVE / f"{qid}.json").exists()
        if not live and now - started > 600:
            return True, False, "the card expired unanswered"
    except Exception:  # noqa: BLE001
        pass
    return False, False, ""


def _settle(p: dict, now: float) -> str:
    """Read the running step's outcome; record it; advance. Returns a line or ''."""
    s = p.get("running")
    if not s:
        return ""
    kind, started = s.get("kind"), float(s.get("started") or now)
    settled, ok, result = False, False, ""
    if kind in ("doer", "task"):
        settled, ok, result = _ticket_outcome(str(s.get("ref") or ""), kind, now, started)
    elif kind == "build":
        settled, ok, result = _build_outcome(str(s.get("ref") or ""), now, started)
    elif kind == "decide":
        settled, ok, result = _card_outcome(str(s.get("ref") or ""), now, started)
    elif kind == "owner":
        if float(p.get("owner_touched") or 0) > started + 1:
            settled, ok, result = True, True, "the owner came back on it"
    elif kind == "wait":
        if now >= float(s.get("due") or 0):
            settled, ok, result = True, True, "the wait ended"
    if not settled and kind in ("build", "decide", "owner"):
        # the ticket readers keep their own clocks (a checkpoint card gets a day); these three get the plain one
        lim = WAIT_H.get(kind, 24) * 3600
        if lim and now - started > lim:
            settled, ok, result = True, False, f"no result by {_stamp(now)}"
    if not settled:
        return ""
    _record(p, s, ok, result, ts=now)
    p["running"] = None
    if ok and s.get("then"):
        t = s["then"] if isinstance(s["then"], dict) else _then(s["then"])
        if t:
            p["next"] = _step(t["what"], t.get("kind", "doer"), now)
            p["status"], p["needs_plan"] = "open", False
            return f"step done ({_short(result, 60)}); next: {_short(t['what'], 60)}"
    _advance(p, now, failed=not ok)
    return f"step {'done' if ok else 'failed'} ({_short(result, 80)})"


def _advance(p: dict, now: float, failed: bool) -> None:
    """After a step: the next untried approach on a failure, else a planning turn; never the same thing again."""
    p["status"] = "open"
    if failed:
        nxt = _pop_approach(p, now)
        if nxt:
            p["next"], p["needs_plan"] = nxt, False
            return
    p["next"] = None
    p["needs_plan"] = True


# ------------------------------------------------------------------ planning turns (the bridge's minute tick)
PLAN_NOTE = ("[PROJECT CHECK-IN, from code: a step of the long-running project \"{title}\" just ended; nothing on record says what "
             "comes next. Read the case below. Set the next step with the `project` tool (op update, `next` with what / kind / due; "
             "add fallback `approaches` if you see them; `tried` for anything you now know was done elsewhere; `hypothesis` when the "
             "thinking changed; op close when it is solved, op park when it should rest). A kind: doer = a site or this PC, task = a message "
             "or a change on a card, build = the system needs an ability, owner = only they can do it (say exactly what, once), decide = "
             "their choice with 2-3 options, wait = check back at `due`. Reply in ONE short line only if the owner should hear it now "
             "(it is solved, or a step is theirs); otherwise reply NO_REPLY.]\n{case}")


def plan_note(pid: str) -> str:
    p = (_load()["projects"] or {}).get(pid) or {}
    return PLAN_NOTE.format(title=p.get("title", ""), case=case_text(p)) if p else ""


def _plan_message(p: dict) -> dict | None:
    person = _owner_person(p)
    if not person.get("key"):
        return None
    convo = str(p.get("convo") or f"dm:{person['key']}")
    space = str(p.get("space") or "")
    if not space:
        space = core.load(core.DATA / "gchat_spaces.json", {}).get(convo[3:]) if convo.startswith("dm:") else convo
    channel = "gchat" if str(space or "").startswith("spaces/") else "local"
    return {"channel": channel, "person": person, "text": f"(checking in on the project: {p['title']})", "space": space or "",
            "thread": str(p.get("thread") or ""), "convo": convo, "direct": True, "why": "DM" if convo.startswith("dm:") else "@mention",
            "_held": "project", "_project_plan": p["id"], "raw_from": person.get("key", "")}


def tick_plans(dispatch, now: float | None = None, dry: bool = False) -> int:
    """Hand every project that needs a next step to ONE brain turn; capped. Called by the bridge every minute."""
    now = now or time.time()
    day = time.strftime("%Y-%m-%d", time.localtime(now))
    with core.locked:
        d = _load()
        today = sum(int(v.get("n_today") or 0) for v in d["plans"].values() if v.get("day") == day)
        n = 0
        for p in d["projects"].values():
            if p.get("status") != "open" or not p.get("needs_plan") or p.get("running") or p.get("next"):
                continue
            pl = d["plans"].get(p["id"]) or {}
            if now - float(pl.get("ts") or 0) < PLAN_GRACE_MIN * 60:
                continue                            # a turn is out on it already
            if pl.get("day") == day and int(pl.get("n_today") or 0) >= PLANS_PER_PROJECT_DAY:
                continue
            if today >= PLANS_PER_DAY:
                break
            m = _plan_message(p)
            if not m:
                continue
            if not dry:
                try:
                    dispatch(m)
                except Exception as e:  # noqa: BLE001
                    _log(f"plan turn for {p['id']} failed: {type(e).__name__}")
                    continue
            d["plans"][p["id"]] = {"ts": now, "day": day, "n_today": (int(pl.get("n_today") or 0) + 1) if pl.get("day") == day else 1}
            n += 1
            today += 1
        if not dry:
            _save(d)
    return n


def _escalate(p: dict, now: float, dry: bool) -> str:
    """Out of approaches and no plan came: ONE line to the owners (what was tried, the one thing that would move it); the
    project waits on them. Never twice while they are silent."""
    tried = [t for t in (p.get("tried") or []) if not t.get("ok")]
    names = [_short(t["what"], 60) for t in tried[-3:]]
    ask = next((a["what"] for a in (p.get("approaches") or []) if a.get("kind") == "owner"), "")
    line = f"I've run out of ways on {p['title']}."
    if names:
        line += " I tried " + (", ".join(names[:-1]) + " and " + names[-1] if len(names) > 1 else names[0]) + "."
    line += f" What would move it: {ask.rstrip('.')}." if ask else " A steer from you on what to try next would move it."
    p["status"], p["escalated"], p["needs_plan"] = "waiting", now, False
    _note(f"PROJECT {p['id']} escalated: {line}")
    if dry:
        return "would escalate"
    if _window_ok():
        _say(p, line)
    else:
        p["say_later"] = line
    return "escalated once"


# ------------------------------------------------------------------ the tick
def tick(now: float | None = None, dry: bool = False) -> dict:
    now = now or time.time()
    out = {"settled": 0, "started": 0, "escalated": 0, "open": 0, "waiting": 0, "needs_plan": 0}
    with core.locked:
        d = _load()
        for p in d["projects"].values():
            if p.get("status") not in ("open", "waiting"):
                continue
            if p.get("say_later") and _window_ok() and not dry:
                if _say(p, p["say_later"]):
                    p.pop("say_later", None)
            line = _settle(p, now)
            if line:
                out["settled"] += 1
                _log(f"{p['id']} {line}")
            if p.get("status") == "open" and p.get("needs_plan") and not p.get("next") and not p.get("running"):
                # a planning turn had its grace (or none came within PLAN_FALLBACK_H: the bridge was down, the caps were
                # hit) and nothing was set: the next untried approach by code, else the owner hears it ONCE
                pl = d["plans"].get(p["id"]) or {}
                since_plan = now - float(pl.get("ts") or 0)
                since_upd = now - float(_epoch(p.get("updated")) or now)
                planned = bool(pl) and float(pl.get("ts") or 0) >= float(_epoch(p.get("updated")) or 0) - 60
                if (planned and since_plan >= PLAN_GRACE_MIN * 60) or (not planned and since_upd >= PLAN_FALLBACK_H * 3600):
                    if p.get("approaches"):
                        p["next"], p["needs_plan"] = _pop_approach(p, now), False
                        if p["next"] is None:
                            p["needs_plan"] = True
                    if p.get("needs_plan") and not p.get("next") and not p.get("escalated"):
                        _escalate(p, now, dry)
                        out["escalated"] += 1
                        d["stats"].append({"ts": now, "pid": p["id"], "event": "escalated"})
            if p.get("status") == "open" and p.get("next") and not p.get("running") and float(p["next"].get("due") or 0) <= now:
                line = _start(p, p["next"], now, dry)
                if line.startswith("started"):
                    out["started"] += 1
                _log(f"{p['id']} {line}")
            out[p.get("status") if p.get("status") in ("open", "waiting") else "open"] += 1
            if p.get("needs_plan"):
                out["needs_plan"] += 1
        if not dry:
            _save(d)
    return out


# ------------------------------------------------------------------ reading
def live(d: dict | None = None) -> list:
    d = d or _load()
    rows = [p for p in d["projects"].values() if p.get("status") in ("open", "waiting")]
    return sorted(rows, key=lambda p: (0 if p.get("status") == "open" else 1, -float(_epoch(p.get("updated")) or 0)))


def _age(stamp: str) -> str:
    e = _epoch(stamp)
    if not e:
        return ""
    days = int((time.time() - e) / 86400)
    return "today" if days < 1 else f"{days} d"


def _now_line(p: dict) -> str:
    r = p.get("running")
    if r:
        who = {"doer": "my hands are on it", "task": "a card is up for their tap", "build": "it is being built", "owner": "waiting on the owner",
               "decide": "a decision card is up", "wait": "checking back"}.get(r.get("kind"), "in progress")
        due = f" until {_stamp(float(r['due']))}" if r.get("kind") == "wait" and r.get("due") else ""
        return f"now: {_short(r['what'], 90)} ({who}{due}, since {str(r.get('started_stamp') or _stamp(float(r.get('started') or time.time())))[5:16]})"
    n = p.get("next")
    if n:
        due = float(n.get("due") or 0)
        when = "now" if due <= time.time() else _stamp(due)[5:16]
        return f"next: {_short(n['what'], 90)} ({n.get('kind')}, {when})"
    if p.get("status") == "waiting" and p.get("escalated"):
        return "waiting on the owners: I ran out of ways and said so"
    if p.get("needs_plan"):
        return "next step not set yet (decide it with the project tool)"
    return "nothing scheduled"


def case_text(p: dict) -> str:
    out = [f"PROJECT {p['id']}: {p['title']} [{p.get('lane')}, {p.get('status')}, since {str(p.get('opened') or '')[:10]}]",
           f"GOAL: {p.get('goal')}", f"SOLVED WHEN: {p.get('solved_when')}"]
    if p.get("hypothesis"):
        out.append(f"THINKING: {p['hypothesis']}")
    out.append(f"TRIED ({len(p.get('tried') or [])}):")
    for t in (p.get("tried") or [])[-8:]:
        out.append(f"  {t['n']}. {t['ts'][5:16]} {t['kind']}: {t['what']} -> {'OK' if t.get('ok') else 'no'}: {t.get('result')}")
    if not p.get("tried"):
        out.append("  (nothing yet)")
    out.append(_now_line(p).upper()[:1] + _now_line(p)[1:])
    if p.get("approaches"):
        out.append("UNTRIED APPROACHES: " + "; ".join(f"{a['what']} ({a.get('kind')})" for a in p["approaches"][:5]))
    for n in (p.get("notes") or [])[-4:]:
        out.append(f"NOTE {n['ts'][5:16]} {n.get('by')}: {n['text']}")
    if p.get("result"):
        out.append(f"RESULT: {p['result']}")
    return "\n".join(out)


def render(limit: int = 8) -> str:
    """The PROJECTS block in the brain's context."""
    rows = live()
    if not rows:
        return ""
    head = ("PROJECTS (long-running problems I keep working on my own, on a schedule, with every try on record; answer \"where are we "
            "on X\" from here, never \"I don't know where that stands\"; when an owner adds a fact, corrects the thinking or names "
            "a way to try, `project` update it the same turn; a recurring or long-running problem they describe is `project` open):")
    lines = [head]
    for p in rows[:limit]:
        tried = (p.get("tried") or [])
        last = f"; last: {_short(tried[-1]['what'], 60)} -> {'ok' if tried[-1].get('ok') else 'no'}: {_short(tried[-1].get('result'), 90)}" if tried else ""
        lines.append(f"- [{p['id']}] {p['title']} ({p.get('lane')}, {p.get('status')}, since {str(p.get('opened') or '')[:10]}): goal {_short(p.get('goal'), 120)}"
                     + (f"; thinking: {_short(p.get('hypothesis'), 120)}" if p.get("hypothesis") else "")
                     + f"; tried {len(tried)}{last}; {_now_line(p)}; solved when {_short(p.get('solved_when'), 90)}")
    if len(rows) > limit:
        lines.append(f"- and {len(rows) - limit} more (/projects)")
    return "\n".join(lines)


def render_list() -> str:
    """/projects in chat: a person's words, no ids beyond the short handle."""
    rows = live()
    if not rows:
        return "Nothing long-running on my board right now. Tell me about a problem that keeps coming back and I'll keep working it."
    out = []
    for p in rows:
        tried = p.get("tried") or []
        out.append(f"{p['title']} ({p['id']}, {p.get('status')}, {_age(p.get('opened'))} on the board): {_short(p.get('goal'), 100)}. "
                   f"Tried {len(tried)}" + (f", last: {_short(tried[-1]['what'], 60)}, {'worked' if tried[-1].get('ok') else 'did not'}" if tried else "")
                   + f". {_now_line(p)[0].upper() + _now_line(p)[1:]}.")
    d = _load()
    done = [p for p in d["projects"].values() if p.get("status") == "done" and float(p.get("closed_ts") or 0) > time.time() - 7 * 86400]
    if done:
        out.append("Solved this week: " + "; ".join(p["title"] for p in done) + ".")
    return "\n".join(out)


def scorecard_bit(days: int = 7) -> str:
    d = _load()
    cut = time.time() - days * 86400
    rows = live(d)
    moved = [p for p in rows if any(_epoch(t.get("ts")) >= cut for t in (p.get("tried") or []))]
    done = [s for s in d["stats"] if s.get("event") == "done" and float(s.get("ts") or 0) >= cut]
    waiting = [p for p in rows if p.get("status") == "waiting" and p.get("escalated")]
    if not rows and not done:
        return ""
    bits = [f"{len(rows)} long-running problem{'s' if len(rows) != 1 else ''} on my board"]
    if moved:
        bits.append(f"{len(moved)} moved this week")
    if done:
        bits.append(f"{len(done)} solved")
    if waiting:
        bits.append(f"{len(waiting)} waiting on you")
    return ", ".join(bits)


def status() -> str:
    d = _load()
    rows = live(d)
    out = [f"projects: {len(rows)} live ({sum(1 for p in rows if p['status'] == 'open')} open, {sum(1 for p in rows if p['status'] == 'waiting')} waiting), "
           f"{sum(1 for p in d['projects'].values() if p.get('status') == 'done')} done, {sum(1 for p in d['projects'].values() if p.get('status') == 'parked')} parked; "
           f"planning turns today: {sum(int(v.get('n_today') or 0) for v in d['plans'].values() if v.get('day') == time.strftime('%Y-%m-%d'))}"]
    for p in rows:
        out.append(case_text(p))
        out.append("")
    return "\n".join(out).rstrip()


# ------------------------------------------------------------------ cli
def main() -> int:
    sys.stdout.reconfigure(encoding="utf-8")
    ap = argparse.ArgumentParser()
    ap.add_argument("cmd", choices=["list", "show", "tick", "status", "context", "open", "tried", "set", "park", "close", "resume"])
    ap.add_argument("--id", default="")
    ap.add_argument("--title", default="")
    ap.add_argument("--goal", default="")
    ap.add_argument("--lane", default="")
    ap.add_argument("--hypothesis", default="")
    ap.add_argument("--solved-when", default="")
    ap.add_argument("--note", default="")
    ap.add_argument("--next", default="")
    ap.add_argument("--kind", default="doer")
    ap.add_argument("--due", default="")
    ap.add_argument("--approach", action="append", default=[], help='"what|kind"')
    ap.add_argument("--what", default="")
    ap.add_argument("--result", default="")
    ap.add_argument("--ok", action="store_true")
    ap.add_argument("--when", default="")
    ap.add_argument("--why", default="")
    ap.add_argument("--opened", default="")
    ap.add_argument("--by", default="")
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args()
    by = a.by or core.first_owner()
    if a.cmd == "list":
        print(render_list())
    elif a.cmd == "show":
        p = find(a.id, live_only=False)
        print(case_text(p) if p else f"no project {a.id!r}")
    elif a.cmd == "tick":
        r = tick(dry=a.dry_run)
        print(f"RESULT PROJECTS settled={r['settled']} started={r['started']} escalated={r['escalated']} open={r['open']} waiting={r['waiting']} needs_plan={r['needs_plan']}")
    elif a.cmd == "status":
        print(status())
    elif a.cmd == "context":
        print(render())
    elif a.cmd == "open":
        nxt = {"what": a.next, "kind": a.kind, "due": a.due} if a.next else None
        r = open_(a.title, a.goal, a.lane, by=by, hypothesis=a.hypothesis, solved_when=a.solved_when, next_step=nxt, approaches=a.approach,
                  opened=a.opened, notes=[a.note] if a.note else ())
        print(json.dumps({k: v for k, v in r.items() if k != "project"}, ensure_ascii=False))
    elif a.cmd == "tried":
        r = update(a.id, by=by, tried=[{"what": a.what, "result": a.result, "ok": a.ok, "when": a.when, "kind": a.kind if a.kind != "doer" or not a.when else a.kind}])
        print(json.dumps({k: v for k, v in r.items() if k != "project"}, ensure_ascii=False))
    elif a.cmd == "set":
        nxt = {"what": a.next, "kind": a.kind, "due": a.due} if a.next else None
        r = update(a.id, by=by, hypothesis=a.hypothesis, goal=a.goal, solved_when=a.solved_when, notes=[a.note] if a.note else (), next_step=nxt,
                   approaches=a.approach, lane=a.lane, title=a.title)
        print(json.dumps({k: v for k, v in r.items() if k != "project"}, ensure_ascii=False))
    elif a.cmd == "park":
        print(json.dumps({k: v for k, v in park(a.id, a.why, by=by).items() if k != "project"}, ensure_ascii=False))
    elif a.cmd == "close":
        print(json.dumps({k: v for k, v in close(a.id, a.result, by=by).items() if k != "project"}, ensure_ascii=False))
    elif a.cmd == "resume":
        print(json.dumps({k: v for k, v in resume(a.id, by=by).items() if k != "project"}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())

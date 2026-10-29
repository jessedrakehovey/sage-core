"""routines.py - schedules the agent creates for itself (reminders and recurring look-ups).

A routine is a row in state/routines.json, not a Windows task: the bridge loop calls tick()
every minute, so a new schedule needs no registration and no terminal. A routine can only
do one thing: put a message in front of an owner. `say` posts fixed text (zero LLM);
`prompt` runs one read-only model turn and posts what it found. Nothing here can send to
anyone outside the household.

  when: {"daily": "07:30"} | {"weekly": "mon 07:30"} | {"once": "2026-10-12 09:00"} | {"every_hours": 4}

  python assistant/agent/routines.py list
  python assistant/agent/routines.py off s3
"""
from __future__ import annotations

import sys
import time
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import core  # noqa: E402

FILE = core.STATE / "routines.json"
RUNS = core.DATA / "routine_runs.json"
DOW = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]
FMT = "%Y-%m-%d %H:%M"


def _load() -> dict:
    return core.load(FILE, {"next_id": 1, "routines": []})


def _hm(s: str) -> tuple[int, int]:
    h, m = str(s).strip().split(":")
    h, m = int(h), int(m)
    if not (0 <= h < 24 and 0 <= m < 60):
        raise ValueError("bad time")
    return h, m


def next_due(when: dict, after: datetime) -> datetime | None:
    """The first firing strictly after `after`. None = never again. Raises ValueError on a bad spec."""
    if "daily" in when:
        h, m = _hm(when["daily"])
        d = after.replace(hour=h, minute=m, second=0, microsecond=0)
        return d if d > after else d + timedelta(days=1)
    if "weekly" in when:
        day, hm = str(when["weekly"]).lower().split()
        h, m = _hm(hm)
        d = after.replace(hour=h, minute=m, second=0, microsecond=0) + timedelta(days=(DOW.index(day[:3]) - after.weekday()) % 7)
        return d if d > after else d + timedelta(days=7)
    if "once" in when:
        d = datetime.strptime(str(when["once"]).strip()[:16].replace("T", " "), FMT)
        return d if d > after else None
    if "every_hours" in when:
        n = float(when["every_hours"])
        if n < float((core.profile().get("routines") or {}).get("min_every_hours", 1)):
            raise ValueError("too frequent")
        return after + timedelta(hours=n)
    raise ValueError("when needs daily, weekly, once or every_hours")


def describe(when: dict) -> str:
    k, v = next(iter(when.items()))
    return f"{k.replace('_', ' ')} {v}"


def add(spec: dict, by: str = "", now: datetime | None = None) -> dict:
    """-> {"status": applied|refused, "id", "reason"}"""
    now = now or datetime.now()
    title = str(spec.get("title") or "").strip()[:80]
    say, prompt = str(spec.get("say") or "").strip()[:1500], str(spec.get("prompt") or "").strip()[:1500]
    to = str(spec.get("to") or by or "both").lower()
    users = list(core.owners() or (core.people().get("users") or {}))
    if to not in users + ["both"]:
        return {"status": "refused", "reason": f"'to' must be one of {users + ['both']}"}
    if not title or not (say or prompt):
        return {"status": "refused", "reason": "needs a title and either say or prompt"}
    when = spec.get("when") if isinstance(spec.get("when"), dict) else {}
    try:
        due = next_due(when, now)
    except (ValueError, KeyError, IndexError, TypeError) as e:
        return {"status": "refused", "reason": f"could not read the time ({e})"}
    if due is None:
        return {"status": "refused", "reason": "that time is already past"}
    with core.locked:
        return _add(title, say, prompt, to, when, due, by)


def _add(title, say, prompt, to, when, due, by) -> dict:
    d = _load()
    act = [r for r in d["routines"] if r.get("active", True)]
    if len(act) >= int((core.profile().get("routines") or {}).get("max_active", 25)):
        return {"status": "refused", "reason": "schedule limit reached; stop one first"}
    if any(r["title"].lower() == title.lower() and r["when"] == when for r in act):
        return {"status": "refused", "reason": "that schedule already exists"}
    sid = f"s{d['next_id']}"
    d["next_id"] = int(d["next_id"]) + 1
    d["routines"].append({"id": sid, "title": title, "when": when, "to": to, "by": by, "added": core.stamp(),
                          "active": True, "next_due": due.strftime(FMT), "runs": 0,
                          **({"say": say} if say else {"prompt": prompt})})
    core.save(FILE, d)
    cid = core.log_change("routine", f"schedule {sid}: {title} ({describe(when)}, to {to})", {"op": "routine_off", "id": sid}, by)
    return {"status": "applied", "id": sid, "change": cid, "next_due": due.strftime(FMT)}


def disable(sid: str, by: str = "", ledger: bool = True) -> bool:
    with core.locked:
        d = _load()
        for r in d["routines"]:
            if r["id"] == sid and r.get("active", True):
                r["active"] = False
                r["stopped"] = core.stamp()
                core.save(FILE, d)
                if ledger:
                    core.log_change("routine_off", f"stopped schedule {sid}: {r['title']}", {"op": "routine_on", "id": sid}, by)
                return True
    return False


def enable(sid: str, now: datetime | None = None) -> bool:
    """Undo of a stop: re-arm from now. False when it is active already or a one-off whose time has passed."""
    now = now or datetime.now()
    with core.locked:
        d = _load()
        for r in d["routines"]:
            if r["id"] == sid and not r.get("active", True):
                try:
                    nxt = next_due(r["when"], now)
                except (ValueError, KeyError, IndexError, TypeError):
                    return False
                if nxt is None:
                    return False
                r["active"] = True
                r["next_due"] = nxt.strftime(FMT)
                r.pop("stopped", None)
                core.save(FILE, d)
                return True
    return False


def active() -> list:
    return [r for r in _load()["routines"] if r.get("active", True)]


def render() -> str:
    rs = active()
    parts = []
    if rs:
        parts.append("\n".join(["ACTIVE SCHEDULES (yours; stop one with unschedule):"] +
                                [f"- [{r['id']}] {r['title']} ({describe(r['when'])}, to {r['to']}, next {r['next_due']})" for r in rs]))
    try:                                   # the system's standing jobs the owners may switch (switches.py, 2026-10-23)
        import switches
        sw = switches.render()
    except Exception:  # noqa: BLE001
        sw = ""
    if sw:
        parts.append(sw)
    return "\n\n".join(parts)


def _quiet(now: datetime) -> bool:
    q = core.profile().get("quiet_hours") or [21, 7]
    return now.hour >= q[0] or now.hour < q[1]


def tick(post, now: datetime | None = None, run_prompt=None) -> int:
    """Fire every due routine through post(person_key, text). Returns how many fired."""
    now = now or datetime.now()
    if _quiet(now):
        return 0                                           # due items wait for morning, they are not lost
    d = _load()
    runs = core.load(RUNS, {})
    day = now.strftime("%Y-%m-%d")
    llm_today = int(runs.get(day, 0))
    fired, changed = 0, False
    touched = set()
    for r in d["routines"]:
        if not r.get("active", True) or r.get("next_due", "9999") > now.strftime(FMT):
            continue
        name = core.profile().get("name", "Agent")
        if r.get("say"):
            text = f"{r['title']}: {r['say']}" if r["say"].lower() != r["title"].lower() else r["say"]
        else:
            if llm_today >= int((core.profile().get("routines") or {}).get("llm_runs_per_day_max", 12)):
                continue                                   # over today's budget: stays due, runs tomorrow
            if run_prompt is None:
                import brain
                run_prompt = lambda p: brain.run_once(p)  # noqa: E731
            res = run_prompt(f"[Scheduled routine {r['id']} '{r['title']}', {now.strftime('%Y-%m-%d %H:%M %a')}]\n"
                             f"{r['prompt']}\nReply with the message for {r['to']} only. No SUGGEST line.")
            llm_today += 1
            runs = {day: llm_today}
            core.save(RUNS, runs)
            if not res.get("ok"):
                r["last_error"] = str(res.get("error"))[:200]
                r["next_due"] = (now + timedelta(minutes=30)).strftime(FMT)   # retry later, never spin
                changed = True
                touched.add(r["id"])
                continue
            import brain as _b
            text = f"{r['title']}\n{_b.split_suggest(res.get('text', ''))[0]}"
        targets = list(core.owners() or (core.people().get("users") or {})) if r["to"] == "both" else [r["to"]]
        for t in targets:
            post(t, text)
            core.say(f"dm:{t}", name, text, channel="routine", meta={"routine": r["id"]})
        r["runs"] = int(r.get("runs", 0)) + 1
        r["last_run"] = now.strftime(FMT)
        nxt = next_due(r["when"], now)
        if nxt is None:
            r["active"] = False
            r["stopped"] = now.strftime(FMT) + " (done)"
        else:
            r["next_due"] = nxt.strftime(FMT)
        fired += 1
        touched.add(r["id"])
    if fired or changed:                                   # an idle minute must not rewrite a tracked file
        # a prompt routine holds a model turn between the load above and here: merge only the routines
        # this tick touched into a fresh read, so a schedule added or stopped meanwhile survives
        mine = {r["id"]: r for r in d["routines"] if r["id"] in touched}
        keys = ("runs", "last_run", "next_due", "active", "stopped", "last_error")
        with core.locked:
            cur = _load()
            for r in cur["routines"]:
                m = mine.get(r["id"])
                if m and r.get("active", True):            # stopped meanwhile: the stop wins
                    for k in keys:
                        if k in m:
                            r[k] = m[k]
            core.save(FILE, cur)
    return fired


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    a = sys.argv[1:]
    if a[:1] == ["off"] and len(a) == 2:
        print("stopped" if disable(a[1], by="cli") else "no such active schedule")
    else:
        print(render() or "(no schedules)")

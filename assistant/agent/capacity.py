"""capacity.py - the agent judges its own capacity: when one queue is not enough it opens a lane, when one kind of
work keeps going wrong it opens a specialist, and it closes either when the need has passed. Zero LLM.

An owner (2026-11-14): the agent should "come natively with the ability to judge when capacity requires making
multiple lanes (specialized lanes for throughput) and agents (that operate as [the agent's] name but are more
specialized at certain tasks) by perceiving when work or problems frequently arise that need more throughput or
specialization."

The design this grows out of is "no standing lanes": nothing loops, one queue, one operator pass when there is work.
That stays the default. What changes is that the question "is one queue still enough?" is now asked by code, every
hour, from the records the system already writes, instead of waiting for a person to notice:

  SENSE     per stream of work, over a rolling window:
              tickets (lane/kind)   arrivals a week, minutes from arrival to first claim, slow days, what waits now
              asks and hand jobs    how often one kind of work (a playbook, or a word the asks share) failed or had
                                    to be asked again
  JUDGE     throughput      a lane whose work waited past `wait_min` on `slow_days` days, with real volume
            specialization  a kind of work with `trouble_min_jobs` tries of which `trouble_rate` went wrong
            A verdict must hold on `confirm_days` separate days before anything is opened (no reacting to one
            bad afternoon), and stay inside `max_lanes` / `max_specialists`.
  ACT       lane         a second operator pass that claims only that lane (or one kind in it), in parallel with
                         the general one, still wake-on-work: no tickets, no run, no tokens. The general pass
                         leaves that work alone unless it has sat `orphan_min` minutes (nothing starves).
            specialist   a focus brief (agent/specialists/<id>.md, the organization's own, written from what
                         went wrong on record plus the lane's lessons and the playbooks that fit) that rides
                         along on every turn doing that work: the desk's, and the lane's operator pass.
            Both are the same agent: same name, same voice, same rules, same approval gates. A brief never
            widens what may be sent or done; to people there is one agent.
  REVIEW    a lane or specialist whose work went quiet for `retire_quiet_days` goes back into the one queue;
            one that did not help after `review_days` says so once.

Mode (profile.json -> capacity.mode): "auto" opens and closes by itself inside the caps, "suggest" only says the
idea once (agent/ideas.py) and waits for an owner's word, "off" measures nothing. Every open and close is one line
in the work log and one idea line under the next digest; never a card.

  python assistant/agent/capacity.py tick [--dry]      # measure, judge, open / close (the spine job)
  python assistant/agent/capacity.py status            # what is open, what is being watched, the busiest streams
  python assistant/agent/capacity.py report [--json]   # the measurements
  python assistant/agent/capacity.py open <key|id>     # an owner's word: open it now
  python assistant/agent/capacity.py retire <id> [--why "..."]
  python assistant/agent/capacity.py dispatch [--dry]  # start a pass for every open lane with work waiting
  python assistant/agent/capacity.py brief <id>        # the focus brief as the prompt block

State: data/agent/capacity.json. Org-free: lanes, names and playbooks come from the pack.
"""
from __future__ import annotations

import json
import os
import re
import statistics
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.append(str(HERE))          # appended, never inserted first: scripts/ modules of the same name must win there
import core  # noqa: E402

STORE = core.DATA / "capacity.json"
ADATA = Path(os.environ["ASSISTANT_DATA"]) if os.environ.get("ASSISTANT_DATA") else core.AGENT.parent / "data"
TICKETS = ADATA / "tickets.json"
RUNS = ADATA / "learn" / "runs.jsonl"
DESK_JOBS = core.DATA / "desk"
CORRECTIONS = core.DATA / "corrections.jsonl"
LEARNINGS = Path(os.environ.get("ASSISTANT_LEARNINGS") or (core.VAULT / "assistant" / "learnings"))
DIR = core.AGENT / "specialists"
if core.STATE != core.AGENT / "state":                     # a test's scratch store keeps its briefs out of the live folder
    DIR = core.STATE.parent / "specialists"
CRON = core.AGENT.parent / "scripts" / "operator_cron.ps1"

DEFAULTS = {
    "mode": "auto",              # auto | suggest | off
    "window_days": 14,           # how far back the records are read
    "hours": [8, 21],            # only work that arrived in these local hours counts toward waiting (nights wait by design)
    "wait_min": 45,              # a day is slow for a lane when its median wait from arrival to first claim passes this
    "slow_days": 3,              # slow days in the window before a lane is judged short of capacity
    "min_per_week": 8,           # arrivals a week; below it a slow lane is not a capacity problem
    "kind_share": 0.6,           # one kind carrying this share of a lane's slow work scopes the new lane to that kind
    "trouble_min_jobs": 6,       # tries of one kind of work in the window before trouble is judged
    "trouble_rate": 0.3,         # the share of them that failed or had to be asked again
    "topic_max_share": 0.4,      # a word in more than this share of all asks is not a kind of work
    "topic_word_factor": 1.5,    # a shared word needs this many times more tries than a playbook before it counts as one
    "confirm_days": 3,           # separate days a verdict must hold before anything is opened
    "recent_days": 7,            # and the evidence must include something this recent
    "max_lanes": 2,
    "max_specialists": 4,
    "lane_runs_per_day": 24,     # passes a day one lane may start (each also counts against the operator's own daily budget)
    "dispatch_hours": [7, 22],
    "orphan_min": 90,            # the general pass takes a lane's ticket that has waited this long
    "review_days": 14,           # days open before "did it help" is judged
    "retire_quiet_days": 14,     # days of quiet before a lane or specialist is closed
    "cooldown_days": 21,         # a closed one is not reopened by itself sooner than this
    "brief_cap": 3500,
}
ID_RX = re.compile(r"^[a-z0-9][a-z0-9-]{0,59}$")
HAND_KINDS = {"agent_run"}                                  # worked by the hands (one browser), never by an operator pass
TROUBLE_KINDS = ("stuck_work", "sla_breach", "exec_exception")
_STOP = set("""about above after again against also another around because been before being below between both could does doing
done down during each every from further have having here into itself just like make made many more most much must need needs
only other over please really same should some such than that their them then there these they this those through under until
very want wants were what when where which while with without would your yours yourself asked says said tell told
card cards text texts email emails send sent message messages link today tomorrow tonight morning thing things still back
going know take look work check find give show sure okay yeah thanks thank will cant dont didnt doesnt wont isnt wasnt
them they'll you'll it's that's i'll we'll let's can't don't didn't doesn't won't isn't wasn't""".split())


# ------------------------------------------------------------------ config and state
def cfg() -> dict:
    c = dict(DEFAULTS)
    try:
        own = core.profile().get("capacity") or {}
        c.update({k: v for k, v in own.items() if not str(k).startswith("_")})
    except Exception:  # noqa: BLE001
        pass
    if os.environ.get("AGENT_CAPACITY") in ("off", "auto", "suggest"):
        c["mode"] = os.environ["AGENT_CAPACITY"]
    return c


def _load() -> dict:
    d = core.load(STORE, {}) or {}
    d.setdefault("workers", {})
    d.setdefault("verdicts", {})
    d.setdefault("history", [])
    d.setdefault("said", {})
    return d


def _save(d: dict) -> None:
    d["history"] = d["history"][-200:]
    core.save(STORE, d)


def _today(now: float | None = None) -> str:
    return time.strftime("%Y-%m-%d", time.localtime(now or time.time()))


def _days_between(a: str, b: str) -> int:
    try:
        return abs((datetime.strptime(b, "%Y-%m-%d") - datetime.strptime(a, "%Y-%m-%d")).days)
    except (TypeError, ValueError):
        return 10 ** 6


def slug(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", str(s or "").lower()).strip("-")[:60] or "work"


def _words(kind: str) -> str:
    return str(kind or "").replace("_", " ").strip()


# ------------------------------------------------------------------ sense: tickets
def _iso_epoch(s) -> float:
    try:
        return datetime.strptime(str(s), "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc).timestamp()
    except (TypeError, ValueError):
        return 0.0


def _first_claim(t: dict) -> tuple[float, str]:
    for h in t.get("history") or []:
        if h.get("to") == "claimed":
            return _iso_epoch(h.get("ts")), str(h.get("by") or "")
    return 0.0, ""


def _in_hours(ts: float, hours) -> bool:
    try:
        h = time.localtime(ts).tm_hour
        return int(hours[0]) <= h < int(hours[1])
    except (TypeError, ValueError, IndexError):
        return True


def ticket_streams(now: float | None = None, c: dict | None = None) -> dict:
    """{"lane/kind": {lane, kind, hands, n, per_week, waits: {day: [min]}, slow: {day: median}, wait_med, waiting,
    oldest_min, trouble}} for tickets that arrived inside the window."""
    now, c = now or time.time(), c or cfg()
    cut = now - float(c["window_days"]) * 86400
    out = {}
    for t in (core.load(TICKETS, {}) or {}).values():
        if not isinstance(t, dict):
            continue
        made = _iso_epoch(t.get("created"))
        if not made or made < cut:
            continue
        lane, kind = str(t.get("lane") or ""), str(t.get("kind") or "")
        s = out.setdefault(f"{lane}/{kind}", {"lane": lane, "kind": kind, "hands": kind in HAND_KINDS, "n": 0, "waits": {},
                                                "waiting": 0, "oldest_min": 0, "claimed": 0})
        s["n"] += 1
        at, by = _first_claim(t)
        if at and by.split(":")[0] in ("operator", "worker", "doer", "lane"):
            s["claimed"] += 1
            if _in_hours(made, c["hours"]):
                s["waits"].setdefault(_today(made), []).append(max(0.0, (at - made) / 60))
        elif t.get("status") == "new":
            s["waiting"] += 1
            s["oldest_min"] = max(s["oldest_min"], int((now - made) / 60))
    for s in out.values():
        s["per_week"] = round(s["n"] * 7 / float(c["window_days"]), 1)
        allw = [w for ws in s["waits"].values() for w in ws]
        s["wait_med"] = round(statistics.median(allw), 1) if allw else 0.0
        s["slow"] = {d: round(statistics.median(ws), 1) for d, ws in s["waits"].items()
                     if len(ws) >= 2 and statistics.median(ws) >= float(c["wait_min"])}
    return out


def lane_view(streams: dict, lane: str, kinds: list | None = None, c: dict | None = None) -> dict:
    """A lane's operator-worked streams (or only `kinds` of it) rolled into one: per_week, slow days, median wait."""
    c = c or cfg()
    rows = [s for s in streams.values() if s["lane"] == lane and not s["hands"] and s["kind"] not in TROUBLE_KINDS
            and s["claimed"] and (not kinds or s["kind"] in kinds)]
    by_day, by_kind = {}, {}
    for s in rows:
        for d, ws in s["waits"].items():
            by_day.setdefault(d, []).extend(ws)
            by_kind.setdefault(d, {}).setdefault(s["kind"], []).extend(ws)
    slow = {d: round(statistics.median(ws), 1) for d, ws in by_day.items()
            if len(ws) >= 2 and statistics.median(ws) >= float(c["wait_min"])}
    slow_by_kind = {}
    for d in slow:
        for k, ws in by_kind.get(d, {}).items():
            slow_by_kind[k] = slow_by_kind.get(k, 0) + sum(1 for w in ws if w >= float(c["wait_min"]))
    allw = [w for ws in by_day.values() for w in ws]
    return {"lane": lane, "kinds": sorted({s["kind"] for s in rows}), "per_week": round(sum(s["per_week"] for s in rows), 1),
            "n": sum(s["n"] for s in rows), "slow": slow, "slow_by_kind": slow_by_kind,
            "wait_med": round(statistics.median(allw), 1) if allw else 0.0,
            "waiting": sum(s["waiting"] for s in rows), "oldest_min": max([s["oldest_min"] for s in rows] or [0]),
            "trouble": sum(s["n"] for s in streams.values() if s["lane"] == lane and s["kind"] in TROUBLE_KINDS)}


# ------------------------------------------------------------------ sense: asks and hand jobs
def _stamp_epoch(s) -> float:
    try:
        return time.mktime(time.strptime(str(s)[:16], "%Y-%m-%d %H:%M"))
    except (TypeError, ValueError):
        return 0.0


def work_rows(now: float | None = None, c: dict | None = None) -> list:
    """Every try at a piece of work inside the window: {ts, text, lane, skill, bad, src}. `bad` = it failed, or it
    was the owner asking again for something already answered."""
    now, c = now or time.time(), c or cfg()
    cut = now - float(c["window_days"]) * 86400
    out = []
    try:
        for p in DESK_JOBS.glob("d*.json"):
            j = core.load(p, {}) or {}
            ts = float(j.get("ts") or 0)
            if ts < cut or j.get("resume") or j.get("why") == "pick-up" or j.get("status") == "running":
                continue
            out.append({"ts": ts, "text": str(j.get("text") or ""), "lane": "", "skill": "", "src": "ask",
                        "bad": j.get("status") == "failed" or bool(j.get("correction"))})
    except OSError:
        pass
    for r in core.rows(RUNS):
        ts = _stamp_epoch(r.get("ts"))
        if ts < cut or r.get("label") != "result":
            continue
        marker = str(r.get("marker") or "").upper()
        bad = bool(int(r.get("never") or 0)) or any(w in marker for w in ("FAIL", "BLOCK", "STUCK", "ERROR", "TIMEOUT", "GAVE_UP"))
        out.append({"ts": ts, "text": str(r.get("task") or ""), "lane": str(r.get("lane") or ""), "skill": str(r.get("skill") or ""),
                    "bad": bad, "src": "hands"})
    for r in core.rows(CORRECTIONS):
        ts = _stamp_epoch(r.get("ts"))
        if ts < cut or not str(r.get("ask") or r.get("text") or "").strip():
            continue
        out.append({"ts": ts, "text": str(r.get("ask") or r.get("text") or ""), "lane": "", "skill": "", "bad": True, "src": "redo"})
    try:
        import skills
        for r in out:
            if not r["skill"]:
                m = skills.match(r["text"])
                r["skill"] = str((m or {}).get("slug") or "")
    except Exception:  # noqa: BLE001
        pass
    return out


_VERBS = set("""fix build rebuild make create setup send resend reply respond answer follow check find look pull grab book rent order
cancel update change edit remove delete purge clear upload download copy move open close run rerun launch restart install register
write draft prepare package export import sync print generate redo finish complete handle sort figure solve debug investigate
verify test compile search locate keep post save store drop turn switch start stop list summarize summarise compare rename organize
organise archive hold renew return submit fill apply convert render release publish calculate ping nudge chase invite replace attach
share forward schedule added sign signed asked asking wanted trying tried getting putting sending making using used""".split())
_NAMES: set | None = None


def _names() -> set:
    """Words that name a person, the agent or a lane: never a kind of work."""
    global _NAMES
    if _NAMES is not None:
        return _NAMES
    out = set()

    def walk(x):
        if isinstance(x, dict):
            for k, v in x.items():
                if k in ("users", "people") and isinstance(v, dict):
                    out.update(w for h in v for w in re.findall(r"[a-z]+", str(h).lower()))
                if k in ("name", "first", "last", "nick", "aliases", "household"):
                    out.update(re.findall(r"[a-z]+", json.dumps(v).lower()))
                walk(v)
        elif isinstance(x, list):
            for v in x:
                walk(v)
    try:
        walk(core.people() or {})
        walk({"name": core.profile().get("name"), "household": core.profile().get("household")})
        lanes = (core.load(core.AGENT.parent / "config.json", {}) or {}).get("lanes") or []
        out.update(w for ln in lanes for w in re.findall(r"[a-z]+", str(ln).lower()))
    except Exception:  # noqa: BLE001
        pass
    _NAMES = out
    return out


_GENERIC = set("""account accounts site website page pages form forms file files folder folders browser login password chat thread
phone number numbers date dates time times name names item items step steps info information details question answer
request requests version copy note notes line lines week month year minute minutes hour hours first last next same""".split())
TOPIC_HEAD = 240      # a long brief's first lines say what the work is; the rest is detail (addresses, amounts, history)


def _tokens(text: str) -> set:
    """The words of an ask that could name a kind of work: no links, no mail addresses, no names, no verbs, no filler."""
    skip = _names()
    text = re.sub(r"https?://\S+|\S+@\S+", " ", str(text or "").lower())[:TOPIC_HEAD]
    return {w for w in re.findall(r"[a-z][a-z'-]{3,}", text)
            if w not in _STOP and w not in _VERBS and w not in _GENERIC and w not in skip}


def work_topics(rows: list, c: dict | None = None) -> dict:
    """{"s:<playbook>" | "k:<word>": {title, triggers, skill, n, bad, rate, days, last, examples}} for the kinds of work
    that came up at least `trouble_min_jobs` times. A playbook is a topic; so is a word the asks share that is not in
    most of them. Words whose asks overlap fold into the stronger one as extra triggers."""
    c = c or cfg()
    need = int(c["trouble_min_jobs"])
    out, total = {}, max(1, len(rows))

    def topic(key, title, idx, triggers, skill=""):
        rs = [rows[i] for i in idx]
        bad = [r for r in rs if r["bad"]]
        out[key] = {"title": title, "triggers": triggers, "skill": skill, "n": len(rs), "bad": len(bad),
                    "rate": round(len(bad) / len(rs), 2), "days": sorted({_today(r["ts"]) for r in bad}),
                    "last": max([r["ts"] for r in bad] or [0]), "idx": set(idx),
                    "examples": [re.sub(r"\s+", " ", r["text"])[:160] for r in sorted(bad, key=lambda r: -r["ts"])[:6]],
                    "lanes": sorted({r["lane"] for r in rs if r["lane"]})}

    by_skill = {}
    for i, r in enumerate(rows):
        if r["skill"]:
            by_skill.setdefault(r["skill"], []).append(i)
    for sk, idx in by_skill.items():
        if len(idx) >= need:
            topic("s:" + sk, sk.replace("-", " "), idx, [], sk)
    by_word = {}
    for i, r in enumerate(rows):
        if r["skill"] and "s:" + r["skill"] in out:
            continue                                     # already counted under its playbook
        for w in _tokens(r["text"]):
            by_word.setdefault(w, []).append(i)
    need_w = int(round(need * float(c["topic_word_factor"])))
    cand = [(w, idx) for w, idx in by_word.items() if len(idx) >= need_w and len(idx) / total <= float(c["topic_max_share"])]
    cand.sort(key=lambda x: (-sum(1 for i in x[1] if rows[i]["bad"]), -len(x[1]), x[0]))
    kept = []
    for w, idx in cand:
        s = set(idx)
        fold = next((k for k in kept if len(s & out[k]["idx"]) / len(s) >= 0.7), None)
        if fold:
            if len(out[fold]["triggers"]) < 4:
                out[fold]["triggers"].append(w)
            continue
        topic("k:" + w, w, idx, [w])
        kept.append("k:" + w)
    for t in out.values():
        t.pop("idx", None)
    return out


# ------------------------------------------------------------------ judge
def judge(now: float | None = None, c: dict | None = None) -> list:
    """The verdicts the records support right now: [{key, type: lane | specialist | hands, title, lane, kinds, triggers,
    skill, why, evidence}]. Nothing is opened here."""
    now, c = now or time.time(), c or cfg()
    recent_cut = _today(now - float(c["recent_days"]) * 86400)
    streams = ticket_streams(now, c)
    out = []
    for lane in sorted({s["lane"] for s in streams.values() if s["lane"]}):
        v = lane_view(streams, lane, None, c)
        if v["per_week"] < float(c["min_per_week"]) or len(v["slow"]) < int(c["slow_days"]) or max(v["slow"]) < recent_cut:
            continue
        kinds, total_slow = [], sum(v["slow_by_kind"].values())
        if total_slow:
            top = max(v["slow_by_kind"], key=v["slow_by_kind"].get)
            if len(v["kinds"]) > 1 and v["slow_by_kind"][top] / total_slow >= float(c["kind_share"]):
                kinds = [top]
                v = lane_view(streams, lane, kinds, c)
                if v["per_week"] < float(c["min_per_week"]) or len(v["slow"]) < int(c["slow_days"]):
                    kinds, v = [], lane_view(streams, lane, None, c)
        title = f"{lane} {_words(kinds[0])}" if kinds else f"{lane} work"
        out.append({"key": "lane:" + lane + ("/" + kinds[0] if kinds else ""), "type": "lane", "title": title, "lane": lane,
                    "kinds": kinds, "triggers": [], "skill": "",
                    "why": f"{title} waited a median {max(v['slow'].values()):.0f} min or more before I got to it on "
                           f"{len(v['slow'])} of the last {int(c['window_days'])} days ({v['per_week']:.0f} a week coming in)",
                    "evidence": {"per_week": v["per_week"], "slow_days": sorted(v["slow"]), "wait_med": v["wait_med"],
                                 "waiting": v["waiting"], "trouble": v["trouble"]}})
    rows = work_rows(now, c)
    for key, t in work_topics(rows, c).items():
        if t["rate"] < float(c["trouble_rate"]) or len(t["days"]) < 2 or _today(t["last"]) < recent_cut:
            continue
        out.append({"key": "spec:" + key[2:], "type": "specialist", "title": t["title"], "lane": (t["lanes"] or [""])[0] if len(t["lanes"]) == 1 else "",
                    "kinds": [], "triggers": t["triggers"], "skill": t["skill"],
                    "why": f"work about '{t['title']}' went wrong or had to be asked again {t['bad']} of the last {t['n']} times",
                    "evidence": {"n": t["n"], "bad": t["bad"], "rate": t["rate"], "days": t["days"], "examples": t["examples"]}})
    hands = [s for s in streams.values() if s["hands"]]
    slow = sorted({d for s in hands for d in s["slow"]})
    if len(slow) >= int(c["slow_days"]) and max(slow) >= recent_cut and sum(s["per_week"] for s in hands) >= float(c["min_per_week"]):
        out.append({"key": "hands", "type": "hands", "title": "browser and PC jobs", "lane": "", "kinds": [], "triggers": [], "skill": "",
                    "why": f"browser and PC jobs queued behind each other on {len(slow)} of the last {int(c['window_days'])} days",
                    "evidence": {"slow_days": slow}})
    return out


# ------------------------------------------------------------------ the workers
def workers(active_only: bool = True) -> list:
    ws = list((_load().get("workers") or {}).values())
    return [w for w in ws if w.get("status") == "active"] if active_only else ws


def owner_of(ticket: dict, ws: list | None = None) -> str:
    """The id of the open lane that owns this ticket ('' = the general queue). The narrowest scope wins."""
    ws = workers() if ws is None else ws
    best = ""
    for w in ws:
        if w.get("type") != "lane" or w.get("status") != "active" or w.get("lane") != ticket.get("lane"):
            continue
        if ticket.get("kind") in HAND_KINDS:
            continue
        kinds = w.get("kinds") or []
        if kinds and ticket.get("kind") not in kinds:
            continue
        if kinds or not best:
            best = str(w.get("id") or "")
    return best


def orphaned(ticket: dict, now: float | None = None, c: dict | None = None) -> bool:
    """A lane's ticket nobody got to: the general pass may take it, so opening a lane can never make work wait longer."""
    c = c or cfg()
    made = _iso_epoch(ticket.get("created"))
    return bool(made) and ((now or time.time()) - made) / 60 >= float(c["orphan_min"])


def specialist_for(text: str, ws: list | None = None) -> dict | None:
    """The open specialist whose work this ask is, else None (a playbook match first, then the longest trigger)."""
    ws = workers() if ws is None else ws
    specs = [w for w in ws if w.get("type") == "specialist" and w.get("status") == "active"]
    if not specs or not str(text or "").strip():
        return None
    sk = ""
    if any(w.get("skill") for w in specs):
        try:
            import skills
            sk = str((skills.match(text) or {}).get("slug") or "")
        except Exception:  # noqa: BLE001
            sk = ""
    best, best_len = None, 0
    low = str(text).lower()
    for w in specs:
        if sk and w.get("skill") == sk:
            return w
        for tr in w.get("triggers") or []:
            if len(tr) > best_len and re.search(r"(?<![a-z])" + re.escape(str(tr).lower()) + r"(?![a-z])", low):
                best, best_len = w, len(tr)
    return best


def _id_for(v: dict) -> str:
    return ("lane-" if v["type"] == "lane" else "spec-") + slug(v["key"].split(":", 1)[-1])


def _lessons(lane: str, words: list, n: int = 8) -> list:
    out = []
    for name in ([lane] if lane else []) + ["general", "all"]:
        p = LEARNINGS / f"{name}.md"
        try:
            lines = [ln.strip() for ln in p.read_text(encoding="utf-8").splitlines() if ln.strip().startswith(("-", "*"))]
        except OSError:
            continue
        hit = [ln for ln in lines if any(w and w.lower() in ln.lower() for w in words)] if words else []
        out += (hit or lines)[-n:]
        if len(out) >= n:
            break
    return [re.sub(r"\s+", " ", ln)[:240] for ln in out[-n:]]


def _playbooks(v: dict) -> list:
    try:
        import skills
        rows = skills.all_skills()
    except Exception:  # noqa: BLE001
        return []
    out = []
    for s in rows:
        trig = " ".join(s["meta"].get("triggers") or []).lower() + " " + str(s.get("title") or "").lower()
        if (v.get("skill") and s["slug"] == v["skill"]) or any(t.lower() in trig for t in v.get("triggers") or []) \
                or (v.get("type") == "lane" and v.get("lane") and s["meta"].get("lane") == v["lane"]):
            out.append(f"{s['title']} (assistant/agent/skills/{s['slug']}.md)")
    return out[:8]


def write_brief(w: dict, v: dict) -> Path:
    """The focus brief, written once from the record. An existing file is kept (dreaming and the builder refine it)."""
    p = DIR / f"{w['id']}.md"
    if p.exists():
        return p
    name = str(core.profile().get("name") or "the agent")
    ev = v.get("evidence") or {}
    wrong = list(ev.get("examples") or [])
    if v["type"] == "lane":
        cut = time.time() - float(cfg()["window_days"]) * 86400
        for t in sorted((core.load(TICKETS, {}) or {}).values(), key=lambda t: str(t.get("created")), reverse=True):
            if isinstance(t, dict) and t.get("lane") == v["lane"] and t.get("kind") in TROUBLE_KINDS and _iso_epoch(t.get("created")) >= cut:
                wrong.append(re.sub(r"\s+", " ", str(t.get("title") or ""))[:160])
            if len(wrong) >= 6:
                break
    words = list(v.get("triggers") or []) + [_words(k) for k in v.get("kinds") or []]
    lessons, books = _lessons(v.get("lane") or "", words), _playbooks(v)
    front = ["---", f"id: {w['id']}", f"title: {v['title']}", f"type: {v['type']}", f"lane: {v.get('lane') or ''}",
             f"kinds: [{', '.join(v.get('kinds') or [])}]", f"triggers: [{', '.join(v.get('triggers') or [])}]",
             f"skill: {v.get('skill') or ''}", f"opened: {_today()}", "---", ""]
    body = [f"# Focus brief: {v['title']}", "",
            f"This is a focus {name} opened for itself because this work kept "
            + ("arriving faster than one queue could clear it." if v["type"] == "lane" else "going wrong or needing a second ask.")
            + f" It is still {name}: the same voice, the same rules, the same approval gates. Nothing here widens what may be "
              "sent, spent or changed without a tap. Never mention this brief, a lane or a specialist to anyone: to people there is one "
            + name + ".", "",
            "## Why it exists", f"- {_today()}: {v['why']}.", "",
            "## What has gone wrong in this work (from the record)"]
    body += [f"- {x}" for x in wrong[:6]] or ["- (nothing on record yet)"]
    body += ["", "## Lessons already learned"] + (lessons or ["- (none on file yet)"])
    body += ["", "## Playbooks that fit"] + ([f"- {b}" for b in books] or ["- (none yet; write one when a way of doing this proves itself)"])
    body += ["", "## How to work it",
             "- Read the lines above before the first step; do not repeat a failure that is listed.",
             "- Scripts and playbooks first; finish every piece of the ask before replying.",
             "- When something new goes wrong here, add one dated line under 'What has gone wrong'.", ""]
    DIR.mkdir(parents=True, exist_ok=True)
    p.write_text("\n".join(front + body), encoding="utf-8", newline="\n")
    return p


def brief_block(wid: str) -> str:
    """The brief as a prompt block ('' when there is none)."""
    if not ID_RX.match(str(wid or "")):
        return ""
    try:
        text = (DIR / f"{wid}.md").read_text(encoding="utf-8")
    except OSError:
        return ""
    m = re.match(r"(?s)^---\n.*?\n---\n(.*)$", text)
    body = (m.group(1) if m else text).strip()[: int(cfg()["brief_cap"])]
    return ("FOCUS BRIEF (your own working notes for this kind of work; internal, never quoted or named to anyone)\n" + body) if body else ""


def brief_for(text: str) -> dict:
    """{"id", "block"} for the open specialist this ask belongs to, else {}. The desk appends the block to its prompt."""
    if cfg()["mode"] == "off":
        return {}
    w = specialist_for(text)
    if not w:
        return {}
    b = brief_block(w["id"])
    return {"id": w["id"], "block": b} if b else {}


# ------------------------------------------------------------------ open, close, say
def _say(text: str, key: str, say: str = "") -> None:
    """One plain line, once: the work log now, the ideas feed under the next digest. Never a card."""
    try:
        import worklog
        worklog.add(text, kind="capacity", ref=key, by="capacity")
    except Exception:  # noqa: BLE001
        pass
    try:
        import ideas
        ideas.suggest(text, "capacity", key=key, say=say)
    except Exception:  # noqa: BLE001
        pass


def open_worker(v: dict, by: str = "capacity", d: dict | None = None, quiet: bool = False) -> dict:
    own = d is None
    d = _load() if own else d
    wid = _id_for(v)
    ev = v.get("evidence") or {}
    w = {"id": wid, "key": v["key"], "type": v["type"], "title": v["title"], "lane": v.get("lane") or "", "kinds": list(v.get("kinds") or []),
         "triggers": list(v.get("triggers") or []), "skill": v.get("skill") or "", "status": "active", "opened": _today(), "by": by,
         "why": v.get("why") or "", "baseline": {k: ev.get(k) for k in ("per_week", "wait_med", "rate", "n") if k in ev},
         "runs": {}, "quiet_since": "", "helped": None}
    w["brief"] = str(write_brief(w, v).name)
    d["workers"][wid] = w
    d["history"].append({"ts": core.stamp(), "what": "opened", "id": wid, "by": by, "why": w["why"]})
    if own:
        _save(d)
    if not quiet:
        if v["type"] == "lane":
            _say(f"{w['why'][0].upper() + w['why'][1:]}, so I now work it side by side with everything else instead of after it.",
                 wid, say=f"go back to one queue for {v['title']}")
        else:
            _say(f"{w['why'][0].upper() + w['why'][1:]}, so I now bring my own notes on what went wrong to it every time.",
                 wid, say=f"drop the extra notes for {v['title']}")
    return w


def retire(wid: str, why: str = "", by: str = "capacity", d: dict | None = None, quiet: bool = False) -> bool:
    own = d is None
    d = _load() if own else d
    w = d["workers"].get(wid)
    if not w or w.get("status") != "active":
        return False
    w.update({"status": "retired", "retired": _today(), "retired_why": why, "retired_by": by})
    d["history"].append({"ts": core.stamp(), "what": "retired", "id": wid, "by": by, "why": why})
    if own:
        _save(d)
    if not quiet:
        _say(f"{w['title'][0].upper() + w['title'][1:]} has been quiet, so it is back in my one queue." if not why
             else f"{w['title'][0].upper() + w['title'][1:]} is back in my one queue: {why}.", wid + ":retired")
    return True


def find(d: dict, word: str) -> tuple[dict | None, dict | None]:
    """(worker, verdict) an owner's word points at: an id, a key or a title."""
    word = str(word or "").strip().lower()
    w = next((x for x in d["workers"].values() if word in (x["id"], x["key"].lower(), x["title"].lower())), None)
    v = next((x for x in d["verdicts"].values() if word in (x["key"].lower(), x["title"].lower(), _id_for(x))), None)
    return w, v


# ------------------------------------------------------------------ the pass
def _review(d: dict, now: float, c: dict, dry: bool) -> int:
    """Close what went quiet; say once when a thing that has been open a while did not help."""
    closed, today = 0, _today(now)
    streams = ticket_streams(now, c)
    topics = work_topics(work_rows(now, c), c)
    for w in list(d["workers"].values()):
        if w.get("status") != "active":
            continue
        if w["type"] == "lane":
            v = lane_view(streams, w["lane"], w.get("kinds") or None, c)
            quiet = v["per_week"] < float(c["min_per_week"]) / 2 and not v["slow"] and not v["waiting"]
            worse = len(v["slow"]) >= int(c["slow_days"]) and v["wait_med"] >= 0.9 * float((w.get("baseline") or {}).get("wait_med") or 0) > 0
        else:
            t = topics.get(("s:" + w["skill"]) if w.get("skill") else ("k:" + (w.get("triggers") or [""])[0])) or {}
            quiet = int(t.get("n") or 0) < int(c["trouble_min_jobs"]) / 2
            worse = bool(t) and float(t.get("rate") or 0) >= max(float(c["trouble_rate"]), float((w.get("baseline") or {}).get("rate") or 0))
        w["quiet_since"] = (w.get("quiet_since") or today) if quiet else ""
        if w.get("by") == "capacity" and quiet and _days_between(w["quiet_since"], today) >= int(c["retire_quiet_days"]):
            if not dry:
                retire(w["id"], "", "capacity", d)
            closed += 1
            continue
        if _days_between(w["opened"], today) >= int(c["review_days"]) and w.get("helped") is None:
            w["helped"] = not worse
            if worse and not dry:
                _say(f"Working {w['title']} separately has not fixed it yet; the hold-up is somewhere else, and I'm keeping it open while I look.",
                     w["id"] + ":nohelp")
    return closed


def tick(dry: bool = False, now: float | None = None) -> dict:
    now, c = now or time.time(), cfg()
    out = {"mode": c["mode"], "verdicts": 0, "watching": 0, "opened": 0, "retired": 0, "active": 0, "said": 0}
    if c["mode"] == "off":
        return out
    d = _load()
    today = _today(now)
    found = judge(now, c)
    out["verdicts"] = len(found)
    window_cut = _today(now - float(c["window_days"]) * 86400)
    for v in found:
        cur = d["verdicts"].get(v["key"]) or {"first": today, "days": []}
        days = sorted({x for x in cur.get("days", []) if x >= window_cut} | {today})
        d["verdicts"][v["key"]] = {**v, "first": cur.get("first") or today, "days": days, "last": today}
    for k in [k for k, v in d["verdicts"].items() if str(v.get("last") or "") < window_cut]:
        d["verdicts"].pop(k)
    active = [w for w in d["workers"].values() if w.get("status") == "active"]
    for v in sorted(d["verdicts"].values(), key=lambda v: (-len(v.get("days") or []), v["key"])):
        if v.get("last") != today:
            continue
        wid = _id_for(v) if v["type"] != "hands" else "hands"
        prev = d["workers"].get(wid) or {}
        if prev.get("status") == "active":
            continue
        if len(v.get("days") or []) < int(c["confirm_days"]):
            out["watching"] += 1
            continue
        if prev.get("status") == "retired" and _days_between(str(prev.get("retired") or ""), today) < int(c["cooldown_days"]):
            continue
        if v["type"] == "hands":
            if not d["said"].get("hands") and not dry:
                d["said"]["hands"] = today
                _say(f"{v['why'][0].upper() + v['why'][1:]}. I can only drive one browser at a time, so those still go one after another.", "hands")
                out["said"] += 1
            continue
        cap = int(c["max_lanes"] if v["type"] == "lane" else c["max_specialists"])
        if sum(1 for w in active if w["type"] == v["type"]) >= cap:
            if d["said"].get("cap:" + v["key"]) != v.get("first") and not dry:
                d["said"]["cap:" + v["key"]] = v.get("first")
                _say(f"{v['why'][0].upper() + v['why'][1:]}. I'm already at my limit of {cap} "
                     + ("separate lanes" if v["type"] == "lane" else "sets of extra notes") + ", so it stays in the one queue.", "cap:" + v["key"])
                out["said"] += 1
            continue
        if c["mode"] == "suggest":
            if d["said"].get("suggest:" + v["key"]) != v.get("first") and not dry:
                d["said"]["suggest:" + v["key"]] = v.get("first")
                _say(f"{v['why'][0].upper() + v['why'][1:]}.", "suggest:" + v["key"],
                     say=(f"open a lane for {v['title']}" if v["type"] == "lane" else f"keep notes on {v['title']}"))
                out["said"] += 1
            continue
        if not dry:
            active.append(open_worker(v, "capacity", d))
        out["opened"] += 1
    out["retired"] = _review(d, now, c, dry)
    out["active"] = sum(1 for w in d["workers"].values() if w.get("status") == "active")
    d["last_tick"] = now
    if not dry:
        _save(d)
    return out


def stale(hours: float = 3.0) -> bool:
    """True when the hourly pass has not run for a while (the reflex runs it then: an updated engine on a pack
    whose job list predates this module still judges its capacity)."""
    return time.time() - float(_load().get("last_tick") or 0) > hours * 3600


# ------------------------------------------------------------------ dispatch: a pass per open lane with work waiting
def _marker(wid: str) -> Path:
    return ADATA / f".operator_running.{wid}"


def dispatch(dry: bool = False, now: float | None = None) -> list:
    now, c = now or time.time(), cfg()
    out = []
    if c["mode"] == "off" or os.environ.get("CAPACITY_DISPATCH") == "off":
        return out
    h = time.localtime(now).tm_hour
    if not int(c["dispatch_hours"][0]) <= h < int(c["dispatch_hours"][1]):
        return out
    d = _load()
    lanes = [w for w in d["workers"].values() if w.get("status") == "active" and w.get("type") == "lane"]
    if not lanes:
        return out
    sys.path.append(str(core.AGENT.parent / "scripts"))
    import tickets
    today, changed = _today(now), False
    for w in lanes:
        wid = w["id"]
        n = tickets.pending(worker=wid)
        if not n:
            continue
        m = _marker(wid)
        try:
            if m.exists() and now - m.stat().st_mtime < 25 * 60:
                out.append(f"{wid}: a pass is already running")
                continue
        except OSError:
            pass
        runs = int((w.get("runs") or {}).get(today) or 0)
        if runs >= int(c["lane_runs_per_day"]):
            out.append(f"{wid}: {runs} passes today, at its limit; the general pass picks up what waits")
            continue
        if dry or not CRON.exists() or os.name != "nt":
            out.append(f"{wid}: would start a pass for {n} waiting")
            continue
        flags = 0x00000200 | 0x08000000                    # NEW_PROCESS_GROUP + NO_WINDOW (never DETACHED_PROCESS)
        subprocess.Popen(["powershell.exe", "-NoProfile", "-WindowStyle", "Hidden", "-ExecutionPolicy", "Bypass",
                          "-File", str(CRON), "-Worker", wid], cwd=str(core.VAULT), creationflags=flags, close_fds=True,
                         stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        w["runs"] = {today: runs + 1}
        changed = True
        out.append(f"{wid}: started a pass for {n} waiting")
    if changed:
        _save(d)
    return out


# ------------------------------------------------------------------ for the brain, and for people
def render() -> str:
    """The prompt block: how the agent is working right now, so 'why was that slow' is answered from the record."""
    if cfg()["mode"] == "off":
        return ""
    ws = workers()
    if not ws:
        return ""
    lines = ["HOW YOU ARE WORKING RIGHT NOW (internal; to people there is one of you: say 'I', never 'a lane' or 'a specialist' unless they use the word first)"]
    for w in ws:
        lines.append(f"- {w['title']}: " + ("worked side by side with everything else" if w["type"] == "lane" else "worked with your own notes on what went wrong")
                     + f" since {w['opened']}, because {w['why']}.")
    return "\n".join(lines)


def status() -> str:
    c, d = cfg(), _load()
    lines = [f"capacity: mode {c['mode']}, window {c['window_days']} days"]
    act = [w for w in d["workers"].values() if w.get("status") == "active"]
    lines.append(f"open ({len(act)}):" if act else "open: none (one queue)")
    for w in act:
        lines.append(f"  {w['id']:<28} {w['type']:<10} since {w['opened']} by {w['by']}: {w['why']}"
                     + ("" if w.get("helped") is None else ("  [helped]" if w["helped"] else "  [not yet helped]")))
    watch = [v for v in d["verdicts"].values() if (d["workers"].get(_id_for(v)) or {}).get("status") != "active"]
    if watch:
        lines.append("watching (a verdict must hold on %d separate days):" % int(c["confirm_days"]))
        for v in sorted(watch, key=lambda v: -len(v.get("days") or [])):
            lines.append(f"  {v['key']:<28} {len(v.get('days') or [])} day(s), last {v.get('last')}: {v['why']}")
    for w in [w for w in d["workers"].values() if w.get("status") == "retired"][-5:]:
        lines.append(f"  closed {w['id']} on {w.get('retired')}: {w.get('retired_why') or 'went quiet'}")
    return "\n".join(lines)


def report(now: float | None = None) -> dict:
    c = cfg()
    streams = ticket_streams(now, c)
    lanes = {lane: lane_view(streams, lane, None, c) for lane in sorted({s["lane"] for s in streams.values() if s["lane"]})}
    topics = work_topics(work_rows(now, c), c)
    return {"lanes": lanes, "streams": {k: {x: v[x] for x in ("n", "per_week", "wait_med", "waiting", "oldest_min", "hands")} | {"slow_days": sorted(v["slow"])}
                                        for k, v in streams.items()},
            "topics": topics, "verdicts": judge(now, c)}


def _report_text(r: dict) -> str:
    lines = ["lanes (operator-worked tickets):"]
    for lane, v in r["lanes"].items():
        lines.append(f"  {lane:<10} {v['per_week']:>5.1f}/wk  median wait {v['wait_med']:>6.1f} min  slow days {len(v['slow'])}  waiting now {v['waiting']}")
    lines.append("busiest streams:")
    for k, s in sorted(r["streams"].items(), key=lambda x: -x[1]["n"])[:12]:
        lines.append(f"  {k:<28} {s['n']:>4} in window  median wait {s['wait_med']:>6.1f} min  slow days {len(s['slow_days'])}" + ("  (hands)" if s["hands"] else ""))
    lines.append("kinds of work (asks and hand jobs):")
    for k, t in sorted(r["topics"].items(), key=lambda x: -x[1]["bad"])[:12]:
        lines.append(f"  {k:<28} {t['n']:>3} tries, {t['bad']} went wrong ({t['rate']:.0%})")
    lines.append(f"verdicts now: {len(r['verdicts'])}")
    for v in r["verdicts"]:
        lines.append(f"  {v['key']}: {v['why']}")
    return "\n".join(lines)


def main() -> int:
    a = sys.argv[1:]
    cmd = a[0] if a else "status"
    if cmd == "tick":
        r = tick(dry="--dry" in a)
        print(f"RESULT CAPACITY mode={r['mode']} verdicts={r['verdicts']} watching={r['watching']} opened={r['opened']} "
              f"retired={r['retired']} active={r['active']} said={r['said']}")
        return 0
    if cmd == "status":
        print(status())
        return 0
    if cmd == "report":
        r = report()
        print(json.dumps(r, indent=1, ensure_ascii=False, default=list) if "--json" in a else _report_text(r))
        return 0
    if cmd == "dispatch":
        rows = dispatch(dry="--dry" in a)
        for ln in rows:
            print(ln)
        print(f"RESULT DISPATCH lanes={len(rows)}")
        return 0
    if cmd == "brief" and len(a) > 1:
        print(brief_block(a[1]))
        return 0
    if cmd == "open" and len(a) > 1:
        d = _load()
        w, v = find(d, " ".join(x for x in a[1:] if not x.startswith("--")))
        if w and w.get("status") == "active":
            print(f"already open: {w['id']}")
            return 0
        v = v or (dict(w, evidence=w.get("baseline") or {}) if w else None)
        if not v or v.get("type") == "hands":
            print("nothing on record by that name; `capacity.py status` lists what is being watched")
            return 1
        w = open_worker(v, "owner", quiet=True)
        print(f"opened {w['id']} ({w['type']}): {w['why']}")
        return 0
    if cmd == "retire" and len(a) > 1:
        d = _load()
        w, _ = find(d, a[1])
        why = a[a.index("--why") + 1] if "--why" in a and a.index("--why") + 1 < len(a) else "an owner asked"
        okd = bool(w) and retire(w["id"], why, "owner", quiet=True)
        print(f"closed {w['id']}" if okd else "nothing open by that name")
        return 0 if okd else 1
    print(__doc__)
    return 2


if __name__ == "__main__":
    sys.exit(main())

"""learn.py - the doer's self-learning engine (zero-LLM). Built 2026-10-11.

the owner: "how fast will it learn new skills - make the self learning engine". Before this, learning
was optional and one-shot: a tool was kept only if the model chose to save it, a how-to skill
appeared only if Sage chose to write one from the report, nothing measured outcomes, nothing
noticed a job that kept coming back, nothing repaired a tool that failed. This closes the loop
with code, the same minute a run ends:

  record    every doer run (browser or computer) -> data/learn/runs.jsonl: task, lane, where,
            skill used, tools used, hook denials, result, duration.
  harvest   the turn's own lesson.json (written before its last line: a skill, a tool verdict,
            a note) -> skills.write(...) and tools meta, deterministically. A skill exists the
            moment the run ends, not when someone remembers to write it.
  score     per tool: runs, ok, failed, last error -> tools/meta.json; a tool that failed since
            it last worked is marked needs_fix and the next prompt says so first.
  repeats   the same kind of computer task done >= REPEAT_MIN times without a tool -> ONE
            agent_run ticket "build a tool for this" (max one a day, never twice for one cluster).
  repair    a needs_fix tool -> ONE agent_run ticket "fix this tool" (max one a day per tool).
  digest    once a day, if anything was learned: one plain line to the owners' chat.
  pace      self-paced (the owner, 2026-10-11: "have it self pace based on lessons and improvement
            opportunities"): a pressure score (lessons not yet harvested, tools needing a repair,
            repeated jobs without a tool, recent failures and denials, how much ran lately) sets
            how often the engine wakes (10 min .. 6 h) and how many build/repair jobs it may queue a
            day (0 .. 4). Nothing to learn -> it sleeps; a busy day with failures -> it runs hot.
            A build or repair job that came back BLOCKED backs the pace off for a day.

Rails: a skill goes through skills.write (its refusals for secrets and permissions hold); the
engine never runs code itself, never answers a card, never sends; it queues the same gated
agent_run tickets an owner's `do` would. Every ticket it queues is visible as a ticket and in the
owners' chat when it reports. State under assistant/data/learn/ (git-ignored, machine-local).

  python assistant/doer/learn.py --tick        # harvest + score + repeats + repair when the pace says so (spine job learn-tick, every 10 min)
  python assistant/doer/learn.py --pace        # the current level and why
  python assistant/doer/learn.py --digest      # the daily line (spine job learn-digest, evenings)
  python assistant/doer/learn.py --status
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
VAULT = ROOT.parent
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT / "agent"))
sys.path.insert(0, str(HERE))
from alib import load_json, save_json  # noqa: E402
import tickets  # noqa: E402

DATA = Path(os.environ.get("LEARN_DATA") or os.environ.get("DOER_DATA") and (Path(os.environ["DOER_DATA"]).parent / "learn") or (ROOT / "data" / "learn"))
RUNS = DATA / "runs.jsonl"
STATE = DATA / "state.json"             # toolify clusters, repair asks, digest day, lessons harvested
TOOLS = HERE / "tools"
META = TOOLS / "meta.json"              # per-tool scores (runtime state next to the tools; git-ignored since 2026-10-18)
OUT = ROOT / "out" / "doer"
HOOK_LOG = Path(os.environ.get("WV_LOG_DIR") or (Path.home() / ".claude")) / "doer-computer-wv.log"
LOG = Path(os.environ.get("WV_LOG_DIR") or (Path.home() / ".claude")) / "doer-learn-wv.log"
REPEAT_MIN = 2
REPEAT_DAYS = 60
SIM_MIN = 0.5
DAILY_MAX_TICKETS = 2
_STOP = set("the a an and or of to in on for with my our me us is are be it this that at by from as into onto up down "
            "please can you could would check see look find what how many much all every each any some its their his her "
            "owner1 owner6 sage do does done go get make run show tell give".split())
_TOOL_RX = re.compile(r"assistant/doer/tools/([\w-]+)\.py", re.I)


def log(msg: str) -> None:
    try:
        LOG.parent.mkdir(parents=True, exist_ok=True)
        with LOG.open("a", encoding="utf-8") as f:
            f.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')} {msg}\n")
    except OSError:
        pass


def _state() -> dict:
    return load_json(STATE, {}) or {}


def _save_state(s: dict) -> None:
    DATA.mkdir(parents=True, exist_ok=True)
    save_json(STATE, s)


def _meta() -> dict:
    return load_json(META, {}) if META.is_file() else {}


def _save_meta(m: dict) -> None:
    TOOLS.mkdir(parents=True, exist_ok=True)
    META.write_text(json.dumps(m, indent=1, ensure_ascii=False, sort_keys=True), encoding="utf-8")


def keywords(text: str) -> set:
    return {w for w in re.findall(r"[a-z0-9][a-z0-9._-]{2,}", str(text or "").lower()) if w not in _STOP}


def similar(a: set, b: set) -> float:
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


# ------------------------------------------------------------------ record
def hook_lines(ticket: str) -> list:
    """The hook's decisions for one ticket (tool, tier, allow/deny, the command)."""
    out = []
    try:
        for ln in HOOK_LOG.read_text(encoding="utf-8", errors="replace").splitlines()[-4000:]:
            if f" {ticket} " in ln:
                out.append(ln)
    except OSError:
        pass
    return out


def tools_used(ticket: str, text: str = "") -> list:
    found = []
    for ln in hook_lines(ticket) + [text or ""]:
        for m in _TOOL_RX.finditer(ln):
            if m.group(1) not in found and m.group(1) != "README":
                found.append(m.group(1))
    return found


def record(t: dict, r: dict, where: str, started: float = 0.0, label: str = "result") -> dict:
    """One row per finished turn (browser or computer). Zero-LLM; never raises."""
    try:
        p = t.get("payload") or {}
        tid = t["id"]
        hl = hook_lines(tid) if where == "computer" else []
        row = {"ts": time.strftime("%Y-%m-%d %H:%M"), "ticket": tid, "where": where, "label": label,
               "lane": t.get("lane") or "", "from": str(p.get("from") or ""), "task": str(p.get("text") or t.get("summary") or "")[:300].split("\n\nHOW WE DID")[0],
               "skill": str(p.get("skill") or ""), "tools": tools_used(tid, r.get("text", "")),
               "marker": r.get("marker") or ("ERROR" if r.get("error") else "NONE"), "arg": str(r.get("arg") or r.get("error") or "")[:300],
               "denied": sum(1 for ln in hl if " DENY " in ln), "never": sum(1 for ln in hl if " NEVER DENY" in ln),
               "secs": int(time.time() - started) if started else 0}
        DATA.mkdir(parents=True, exist_ok=True)
        with RUNS.open("a", encoding="utf-8") as f:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")
        log(f"record {tid} {where} {row['marker']} tools={row['tools']} denied={row['denied']}")
        return row
    except Exception as e:  # noqa: BLE001
        log(f"record failed: {type(e).__name__}: {e}")
        return {}


def runs(days: int = REPEAT_DAYS) -> list:
    out = []
    if not RUNS.is_file():
        return out
    cut = time.strftime("%Y-%m-%d", time.localtime(time.time() - days * 86400))
    for ln in RUNS.read_text(encoding="utf-8", errors="replace").splitlines():
        try:
            d = json.loads(ln)
        except ValueError:
            continue
        if str(d.get("ts", "")) >= cut:
            out.append(d)
    return out


# ------------------------------------------------------------------ harvest
def harvest(ticket: str, by: str = "") -> dict:
    """The turn's lesson.json -> a skill (skills.write) + tool verdicts (meta). Idempotent per
    ticket. -> {"skill": title|"", "skill_status", "tool": slug|"", "note"}."""
    res = {"skill": "", "skill_status": "", "tool": "", "note": ""}
    f = OUT / ticket / "lesson.json"
    st = _state()
    done = st.setdefault("harvested", {})
    if not f.is_file() or ticket in done:
        return res
    try:
        les = json.loads(f.read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        log(f"harvest {ticket}: bad lesson.json ({type(e).__name__})")
        done[ticket] = "bad"
        _save_state(st)
        return res
    if not isinstance(les, dict):
        done[ticket] = "bad"
        _save_state(st)
        return res
    sk = les.get("skill")
    if isinstance(sk, dict) and sk.get("title") and sk.get("body"):
        try:
            import skills
            w = skills.write({"title": sk.get("title"), "triggers": sk.get("triggers") or [], "lane": sk.get("lane") or "personal",
                              "via": sk.get("via") or "do", "body": str(sk.get("body"))[:5000]}, by=by or f"doer run {ticket[:6]}")
            res["skill"], res["skill_status"] = str(sk.get("title"))[:80], w.get("status", "")
            log(f"harvest {ticket}: skill '{res['skill']}' {res['skill_status']} {w.get('reason', '')}")
        except Exception as e:  # noqa: BLE001
            log(f"harvest {ticket}: skill write failed {type(e).__name__}: {e}")
    tl = les.get("tool")
    if isinstance(tl, dict) and tl.get("file"):
        slug = _TOOL_RX.search(str(tl["file"]).replace("\\", "/"))
        slug = slug.group(1) if slug else Path(str(tl["file"])).stem
        m = _meta()
        e = m.setdefault(slug, {"runs": 0, "ok": 0, "failed": 0, "last_error": "", "needs_fix": False, "last_task": "", "history": []})
        worked = bool(tl.get("worked", True))
        e["runs"] += 1
        e["ok" if worked else "failed"] += 1
        e["last_error"] = "" if worked else str(tl.get("error") or "failed")[:200]
        e["needs_fix"] = not worked
        e["last_task"] = str(les.get("task") or "")[:120]
        e["history"] = (e.get("history") or [])[-9:] + [f"{time.strftime('%Y-%m-%d')} {'ok' if worked else 'FAILED'} {ticket[:6]}"]
        _save_meta(m)
        res["tool"] = slug
    res["note"] = str(les.get("note") or "")[:200]
    done[ticket] = time.strftime("%Y-%m-%d %H:%M")
    _save_state(st)
    return res


def harvest_pending() -> list:
    out = []
    st = _state()
    done = st.get("harvested", {})
    if OUT.is_dir():
        for f in OUT.glob("*/lesson.json"):
            if f.parent.name not in done:
                r = harvest(f.parent.name)
                if r.get("skill") or r.get("tool"):
                    out.append(r)
    return out


# ------------------------------------------------------------------ score
def score() -> dict:
    """Tool outcomes from the run ledger (a tool used in a DONE run counts ok; in a BLOCKED or
    empty run, failed) merged with the turns' own verdicts (harvest). -> meta."""
    m = _meta()
    seen = set()
    for row in runs(365):
        key = row.get("ticket", "") + row.get("label", "")
        for slug in row.get("tools") or []:
            e = m.setdefault(slug, {"runs": 0, "ok": 0, "failed": 0, "last_error": "", "needs_fix": False, "last_task": "", "history": []})
            mark = f"{row.get('ticket', '')[:6]}:{row.get('label', '')}"
            if any(mark in h for h in e.get("history") or []) or key in seen:
                continue
            good = row.get("marker") in ("DOER_DONE", "DOER_ANSWER", "DOER_CHECKPOINT")
            e["runs"] += 1
            e["ok" if good else "failed"] += 1
            if not good:
                e["last_error"] = str(row.get("arg") or "")[:200]
                e["needs_fix"] = True
            elif row.get("marker") in ("DOER_DONE", "DOER_ANSWER"):
                e["needs_fix"] = False
            e["last_task"] = str(row.get("task") or "")[:120]
            e["history"] = (e.get("history") or [])[-9:] + [f"{row.get('ts', '')[:10]} {'ok' if good else 'FAILED'} {mark}"]
        seen.add(key)
    _save_meta(m)
    return m


def tool_notes() -> dict:
    """slug -> one short line for the prompt (failures first)."""
    out = {}
    for slug, e in _meta().items():
        if e.get("needs_fix"):
            out[slug] = f"FAILED last time ({e.get('last_error') or 'no detail'}); read it and fix it before you rely on it"
        elif e.get("runs"):
            out[slug] = f"{e.get('ok', 0)}/{e.get('runs', 0)} runs ok"
    return out


# ------------------------------------------------------------------ repeats -> a tool
def _clusters(rows: list) -> list:
    """Greedy keyword clusters of computer tasks that were done by hand (no tool)."""
    cl = []
    for r in rows:
        if r.get("where") != "computer" or r.get("tools") or r.get("marker") not in ("DOER_DONE", "DOER_ANSWER"):
            continue
        kw = keywords(r.get("task"))
        if len(kw) < 2:
            continue
        for c in cl:
            if similar(c["kw"], kw) >= SIM_MIN:
                c["rows"].append(r)
                c["kw"] |= kw
                break
        else:
            cl.append({"kw": set(kw), "rows": [r]})
    return cl


def _sig(c: dict) -> str:
    return "-".join(sorted(c["kw"]))[:120]


def _queue(title: str, text: str, lane: str, who: str, sid: str) -> str:
    n = tickets.add_many([{"source": "learn", "source_id": sid, "lane": lane if lane in ("wedding", "personal", "home", "money", "owner6") else "personal",
                           "kind": "agent_run", "title": title[:120], "summary": text[:600],
                           "payload": {"text": text, "via": "learn", "from": who or "owner1", "where": "computer", "skill": "",
                                       "convo": f"dm:{who or 'owner1'}", "thread": ""}}], "learn")
    return tickets.make_id("learn", sid) if n > 0 else ""


def _today_budget(st: dict) -> bool:
    day = time.strftime("%Y-%m-%d")
    b = st.setdefault("tickets_today", {})
    if b.get("day") != day:
        b.clear()
        b.update({"day": day, "n": 0})
    return int(b.get("n", 0)) < int(st.get("pace", {}).get("daily_max", DAILY_MAX_TICKETS))


def _spend(st: dict) -> None:
    st["tickets_today"]["n"] = int(st["tickets_today"].get("n", 0)) + 1


def repeats(dry: bool = False) -> list:
    """A job asked REPEAT_MIN+ times by hand -> one 'build a tool' ticket for the computer lane."""
    out = []
    st = _state()
    done = st.setdefault("toolified", {})
    for c in _clusters(runs()):
        if len(c["rows"]) < REPEAT_MIN:
            continue
        sig = _sig(c)
        if sig in done or any(similar(set(k.split("-")), c["kw"]) >= 0.7 for k in done):
            continue
        if not _today_budget(st):
            break
        last = c["rows"][-1]
        examples = "\n".join(f"- {r.get('ts', '')[:10]}: {r.get('task', '')[:160]}" for r in c["rows"][-3:])
        text = (f"Build a reusable tool for a job the household has asked {len(c['rows'])} times and that was done by hand each time:\n{examples}\n\n"
                "Write it under assistant/doer/tools/<slug>.py with the header (# tool / # triggers / # tier / # usage / # needs), arguments in, "
                "a clear printout out, any delete or move behind --apply. Dry-run it on the real data the last ask used, make sure the output "
                "answers the question, then write lesson.json with the tool verdict and a skill describing when to use it. Report the tool's name.")
        if dry:
            out.append({"sig": sig, "n": len(c["rows"]), "dry": True})
            continue
        tid = _queue(f"Learn: build a tool for '{last.get('task', '')[:60]}'", text, last.get("lane", "personal"), last.get("from") or "owner1", f"toolify|{sig}")
        if tid:
            done[sig] = {"ticket": tid, "ts": time.strftime("%Y-%m-%d %H:%M"), "n": len(c["rows"])}
            _spend(st)
            out.append({"sig": sig, "n": len(c["rows"]), "ticket": tid})
            log(f"repeats: toolify '{last.get('task', '')[:60]}' x{len(c['rows'])} -> {tid}")
    _save_state(st)
    return out


def repair(dry: bool = False) -> list:
    """A tool marked needs_fix -> one 'fix this tool' ticket (once per failure)."""
    out = []
    st = _state()
    asked = st.setdefault("repairs", {})
    for slug, e in _meta().items():
        if not e.get("needs_fix"):
            continue
        key = f"{slug}|{e.get('failed', 0)}"
        if key in asked or not (TOOLS / f"{slug}.py").is_file():
            continue
        if not _today_budget(st):
            break
        text = (f"Fix the tool assistant/doer/tools/{slug}.py: it failed the last time it ran ({e.get('last_error') or 'no detail'}; "
                f"task was: {e.get('last_task') or '-'}). Read it, reproduce the failure on a dry run, fix the cause (not the symptom), "
                "keep the header, re-run the dry run, then write lesson.json with the tool verdict (worked true/false and what was wrong). "
                "If the fix needs something only an owner can give, say exactly what in DOER_BLOCKED.")
        if dry:
            out.append({"tool": slug, "dry": True})
            continue
        tid = _queue(f"Learn: repair tool {slug}", text, "personal", "owner1", f"repair|{key}")
        if tid:
            asked[key] = {"ticket": tid, "ts": time.strftime("%Y-%m-%d %H:%M")}
            _spend(st)
            out.append({"tool": slug, "ticket": tid})
            log(f"repair: {slug} -> {tid}")
    _save_state(st)
    return out


# ------------------------------------------------------------------ pace
PACE = [  # (pressure at least, name, minutes between ticks, build/repair jobs a day)
    (6, "hot", 10, 4),
    (3, "busy", 30, 3),
    (1, "steady", 60, 2),
    (0, "idle", 360, 0),
]


def pressure() -> dict:
    """What there is to learn from right now. Each item is one unit of pressure."""
    st = _state()
    done = st.get("harvested", {})
    lessons = sum(1 for f in OUT.glob("*/lesson.json") if f.parent.name not in done) if OUT.is_dir() else 0
    m = _meta()
    fixes = sum(1 for v in m.values() if v.get("needs_fix"))
    toolified = st.get("toolified", {})
    rep = sum(1 for c in _clusters(runs()) if len(c["rows"]) >= REPEAT_MIN and _sig(c) not in toolified)
    recent = [r for r in runs(2)]
    fails = sum(1 for r in recent if r.get("marker") not in ("DOER_DONE", "DOER_ANSWER", "DOER_CHECKPOINT"))
    denied = sum(int(r.get("denied") or 0) for r in recent)
    volume = min(3, len(recent) // 3)
    backoff = 2 if str(st.get("backoff_until") or "") > time.strftime("%Y-%m-%d %H:%M") else 0
    score_ = lessons * 2 + fixes * 2 + rep * 2 + fails + (1 if denied >= 3 else 0) + volume - backoff
    return {"lessons": lessons, "fixes": fixes, "repeats": rep, "fails_48h": fails, "denied_48h": denied,
            "runs_48h": len(recent), "backoff": bool(backoff), "score": max(0, score_)}


def pace() -> dict:
    """Pick the level for the current pressure, store it, -> {"level", "every_min", "daily_max", ...}."""
    pr = pressure()
    for floor, name, every, daily in PACE:
        if pr["score"] >= floor:
            lvl = {"level": name, "every_min": every, "daily_max": daily}
            break
    st = _state()
    st["pace"] = {**lvl, "score": pr["score"], "ts": time.strftime("%Y-%m-%d %H:%M"), "why": pr}
    _save_state(st)
    return st["pace"]


def due() -> bool:
    """Is a tick due under the current pace? (the spine fires every 10 min; this is the throttle)"""
    st = _state()
    last = str(st.get("last_tick") or "")
    every = int((st.get("pace") or {}).get("every_min") or 60)
    if not last:
        return True
    try:
        return time.time() - time.mktime(time.strptime(last, "%Y-%m-%d %H:%M")) >= every * 60 - 30
    except ValueError:
        return True


def learn_outcomes() -> None:
    """A build/repair job the engine queued that came back BLOCKED (or never finished) backs the pace
    off for a day: the opportunities are there but the hands cannot use them yet."""
    st = _state()
    seen = st.setdefault("own_outcomes", {})
    for row in runs(7):
        tid = row.get("ticket", "")
        if tid in seen or not tid:
            continue
        t = tickets._load().get(tid) or {}
        if t.get("source") != "learn":
            continue
        seen[tid] = row.get("marker", "")
        if row.get("marker") not in ("DOER_DONE", "DOER_ANSWER"):
            st["backoff_until"] = time.strftime("%Y-%m-%d %H:%M", time.localtime(time.time() + 86400))
            log(f"outcome: own job {tid[:6]} ended {row.get('marker')} -> backing off until {st['backoff_until']}")
    _save_state(st)


# ------------------------------------------------------------------ the loop
def after_run(t: dict, r: dict, where: str, started: float = 0.0, label: str = "result") -> str:
    """Called by the doer the moment a turn ends. -> a short 'Learned: ...' suffix for the owner's
    line (empty when nothing was learned). Never raises."""
    try:
        record(t, r, where, started, label)
        h = harvest(t["id"])
        score()
        bits = []
        if h.get("skill"):
            bits.append(f"skill '{h['skill']}' {'updated' if h.get('skill_status') == 'appended' else 'saved'}")
        if h.get("tool"):
            m = _meta().get(h["tool"], {})
            bits.append(f"tool {h['tool']} {'works' if not m.get('needs_fix') else 'needs a fix, queued'}")
        learn_outcomes()
        pace()
        if where == "computer":
            repeats()
            repair()
        return (" Learned: " + "; ".join(bits) + ".") if bits else ""
    except Exception as e:  # noqa: BLE001
        log(f"after_run failed: {type(e).__name__}: {e}")
        return ""


def tick(force: bool = False) -> dict:
    """The spine calls this every 10 min; the pace decides whether anything happens."""
    if not force and not due():
        return {"skipped": "not due", "pace": _state().get("pace", {})}
    h = harvest_pending()
    m = score()
    learn_outcomes()
    pc = pace()
    out = {"harvested": len(h), "tools": len(m), "needs_fix": sorted(k for k, v in m.items() if v.get("needs_fix")),
           "toolify": repeats(), "repair": repair(), "pace": pc}
    st = _state()
    st["last_tick"] = time.strftime("%Y-%m-%d %H:%M")
    _save_state(st)
    log(f"tick pace={pc['level']} score={pc['score']} harvested={len(h)} toolify={len(out['toolify'])} repair={len(out['repair'])}")
    return out


def digest(dry: bool = False) -> str:
    """One line a day, only when something was learned since the last one."""
    st = _state()
    day = time.strftime("%Y-%m-%d")
    if st.get("digest_day") == day and not dry:
        return ""
    since = st.get("digest_ts") or time.strftime("%Y-%m-%d 00:00", time.localtime(time.time() - 86400))
    rows = [r for r in runs(3) if r.get("ts", "") > since]
    done = sum(1 for r in rows if r.get("marker") in ("DOER_DONE", "DOER_ANSWER"))
    cards = sum(1 for r in rows if r.get("marker") == "DOER_CHECKPOINT")
    blocked = sum(1 for r in rows if r.get("marker") == "DOER_BLOCKED")
    harvested = [v for k, v in (st.get("harvested") or {}).items() if str(v) > since]
    m = _meta()
    fixes = [k for k, v in m.items() if v.get("needs_fix")]
    new_tools = [k for k, v in m.items() if any(str(h)[:10] >= since[:10] for h in (v.get("history") or [])[-1:])]
    if not rows and not harvested:
        return ""
    parts = [f"{done} done" if done else "", f"{cards} waited for your tap" if cards else "", f"{blocked} blocked" if blocked else ""]
    text = (f"Today's learning: {len(rows)} job{'s' if len(rows) != 1 else ''} ({', '.join(p for p in parts if p) or 'no result'}), "
            f"{len(harvested)} lesson{'s' if len(harvested) != 1 else ''} kept, {len(new_tools)} tool{'s' if len(new_tools) != 1 else ''} touched"
            + (f", {len(fixes)} tool{'s' if len(fixes) != 1 else ''} waiting for a repair ({', '.join(fixes[:3])})" if fixes else "") + ".")
    if dry:
        return text
    try:
        import bridge
        bridge.post("owner1", text)
    except Exception as e:  # noqa: BLE001
        log(f"digest post failed: {type(e).__name__}")
    st["digest_day"], st["digest_ts"] = day, time.strftime("%Y-%m-%d %H:%M")
    _save_state(st)
    return text


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--tick", action="store_true")
    ap.add_argument("--digest", action="store_true")
    ap.add_argument("--status", action="store_true")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--force", action="store_true")
    ap.add_argument("--pace", action="store_true")
    a = ap.parse_args()
    if a.tick:
        print(json.dumps(tick(force=a.force), indent=1, ensure_ascii=False))
        return 0
    if a.pace:
        print(json.dumps(pace(), indent=1, ensure_ascii=False))
        return 0
    if a.digest:
        print(digest(dry=a.dry_run) or "(nothing to say today)")
        return 0
    rs = runs(30)
    print(json.dumps({"pace": pace(), "runs_30d": len(rs), "done": sum(1 for r in rs if r.get("marker") in ("DOER_DONE", "DOER_ANSWER")),
                      "tools": _meta(), "state": {k: v for k, v in _state().items() if k != "harvested"},
                      "clusters": [{"n": len(c["rows"]), "kw": sorted(c["kw"])[:8]} for c in _clusters(rs)], "log": str(LOG)}, indent=1, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())

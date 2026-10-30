"""chains.py - declared next-step chains: book the next stage before this one ends (ruling 4,
2026-10-15; the shape is example's next_step.py).

A chain (assistant/chains.json) is an ordered list of gates for one kind of entity. Each gate has a
due date (an offset from the chain's anchor date, or a window from the previous gate) and the
evidence that satisfies it. Walking the chain for one entity: the first gate that is not done, whose
predecessor is done or ENDING (its due date within ending_days), is "up next". Up next with no
evidence by its ask time -> ONE card: Booked / Book it for me / Not yet / Drop. A gate still running
(up next but not yet at its ask time) stops the walk: nothing to ask yet.

Evidence is read, never written: the delivery ledger, the CRM snapshot (crm.py), the editor pipeline,
the calendar file, follow-ups, tickets, the executor ledger, the owners' own chat lines, the
registers (assistant/data/registers/*.json) and the owner's tap on the gate's own card. "Book it for
me" mints a `chain_book` ticket for the operator / doer (never a send); every other tap changes only
this ledger (data/tracking/chains.json).

  python assistant/scripts/tracking/chains.py            # tick (what the spine runs)
  python assistant/scripts/tracking/chains.py --dry-run  # print the gaps and the cards it would post
  python assistant/scripts/tracking/chains.py gaps       # every entity, every gate, its state
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import advice  # noqa: E402
import common as C  # noqa: E402
import crm  # noqa: E402

CHAINS_CFG = C.ROOT / "chains.json"
REGISTERS = C.DATA / "registers"
LEDGER_ORDER = ["inquiry", "booked", "pre-shoot", "ingest", "edit", "review", "revisions", "delivered"]
TERMINAL = {"delivered", "cancelled", "lost", "archived"}
ASK_DEFAULTS = {"lead_days": 7, "reask_days": 7, "max_asks": 3, "ending_days": 2, "max_per_lane_per_day": 2,
                "options": {"booked": "Booked", "book": "Book it for me", "not_yet": "Not yet", "drop": "Drop"}}
_GENERIC = {"project", "projects", "wedding", "the", "and", "with", "for"}


def cfg() -> dict:
    raw = C.load_json(CHAINS_CFG, {}) or {}
    ask = dict(ASK_DEFAULTS)
    ask.update({k: v for k, v in (raw.get("ask") or {}).items() if not k.startswith("_")})
    ask["options"] = dict(ASK_DEFAULTS["options"], **(ask.get("options") or {}))
    return {"ask": ask, "chains": {k: v for k, v in (raw.get("chains") or {}).items() if isinstance(v, dict)}}


def _order(stage: str) -> int:
    return LEDGER_ORDER.index(stage) if stage in LEDGER_ORDER else -1


def _d(v):
    dd = C.parse_any(v)
    return dd.date() if dd else None


def name_words(s: str) -> set:
    """The words that identify an entity ('Tatum Rookwood's Project' -> {tatum, rookwood})."""
    s = re.sub(r"'s\b", "", str(s or "").lower())
    return {w for w in re.findall(r"[a-z]{3,}", s) if w not in _GENERIC}


def _mentions(words: set, text: str) -> bool:
    if not words:
        return False
    return bool(words & set(re.findall(r"[a-z]{3,}", str(text or "").lower())))


def _has_words(text: str, needles: list) -> bool:
    t = str(text or "").lower()
    return any(str(n).lower() in t for n in needles or [])


# ------------------------------------------------------------------ the world the chains read
class World:
    """Every record source, loaded once. Tests build one from dicts; the tick builds it from disk."""

    def __init__(self, now=None, ledger=None, crm_snap=None, editor=None, calendar=None, followups=None, tickets=None,
                 execs=None, chat=None, registers=None, state=None, projects_root=None):
        self.now = now or C.now_local()
        self.today = self.now.date()
        self.ledger = ledger if ledger is not None else (C.load_json(C.vault_path(_ledger_path()), {}) or {})
        self.crm = crm_snap if crm_snap is not None else crm.snapshot()
        self.crm_stages = {s.get("stage_id"): s for s in self.crm.get("stages") or []}
        self.editor = editor if editor is not None else _editor_projects(Path(projects_root or _projects_root()))
        self.calendar = calendar if calendar is not None else ((C.load_json(C.vault_path("assistant/data/calendar.json"), {}) or {}).get("events") or [])
        self.followups = followups if followups is not None else list((C.followups_state().get("items") or {}).values())
        self.tickets = tickets if tickets is not None else list(C.tickets_all().values())
        self.execs = execs if execs is not None else [t for t in C.exec_tickets(120) if t.get("status") == "done"]
        self.chat = chat if chat is not None else C.transcript(120)
        self.registers = registers if registers is not None else load_registers()
        self.state = state if state is not None else _load_state()


def _ledger_path() -> str:
    try:
        return str((C.config().get("projects") or {}).get("state_file") or "scripts/projects/project_state.json")
    except SystemExit:
        return "scripts/projects/project_state.json"


def _projects_root() -> str:
    try:
        return str(((C.cfg().get("drift") or {}).get("ledger_stage") or {}).get("projects_root") or "D:/WeddingEditor/projects")
    except Exception:  # noqa: BLE001
        return "D:/WeddingEditor/projects"


def _editor_projects(root: Path) -> dict:
    """folder -> {wedding: wedding.json, done: pipeline_state.json['done'] keys}."""
    out = {}
    try:
        dirs = [p for p in root.iterdir() if p.is_dir() and not p.name.startswith("_") and (p / "wedding.json").exists()]
    except OSError:
        return out
    for p in dirs:
        w = C.load_json(p / "wedding.json", {}) or {}
        st = C.load_json(p / "pipeline_state.json", {}) or {}
        out[p.name] = {"wedding": w, "done": sorted((st.get("done") or {}).keys())}
    return out


def load_registers() -> dict:
    """assistant/data/registers/*.json -> {name: [entries]}. Shapes in entities.py init()."""
    out = {}
    try:
        files = sorted(REGISTERS.glob("*.json"))
    except OSError:
        files = []
    for f in files:
        d = C.load_json(f, {}) or {}
        rows = d.get("entries") if isinstance(d, dict) else d
        out[f.stem] = [r for r in (rows or []) if isinstance(r, dict)]
    return out


def _load_state() -> dict:
    st = C.state("chains", {})
    st.setdefault("gates", {})        # "<chain>:<entity>:<gate>" -> {status, asks, qid, ...}
    st.setdefault("lane_day", {})
    return st


# ------------------------------------------------------------------ entities per chain
def entities_for(name: str, ch: dict, w: World) -> list:
    """Rows {id, title, lane, anchor (date), words, ledger_stage, crm (project), amount, extra}."""
    kind = str(ch.get("entity") or "")
    ap = ch.get("applies") or {}
    out = []
    if kind == "couple":
        lo, hi = (ap.get("anchor_window_days") or [-365, 365])
        stages = set(ap.get("ledger_stages") or [])
        for key, p in (w.ledger.get("projects") or {}).items():
            stage = str(p.get("stage") or "")
            if stage in TERMINAL or (stages and stage not in stages):
                continue
            booked = crm.status(p) == "client" or _order(stage) >= _order("booked")
            if not booked:
                continue
            ad = _d(p.get("wedding_date"))
            if not ad:
                continue
            off = (ad - w.today).days * -1        # days since the wedding (negative = still ahead)
            if not (lo <= off <= hi):
                continue
            who = str(p.get("couple") or key).replace("'s Project", "").strip()
            out.append({"id": key, "title": who, "lane": str(ch.get("lane") or "wedding"), "anchor": ad, "words": name_words(who),
                        "ledger_stage": stage, "crm": _crm_project(w, crm.project_id(p), who), "amount": p.get("amount"),
                        "waiting_kind": "client"})
    elif kind == "lead":
        statuses = set(ap.get("crm_status") or ["lead"])
        quiet = int(ap.get("max_quiet_days") or 45)
        skip = [str(s) for s in ap.get("skip_name_prefixes") or []]
        for p in w.crm.get("projects") or []:
            if str(p.get("status") or "") not in statuses:
                continue
            nm = str(p.get("name") or "")
            if any(nm.startswith(s) for s in skip):
                continue
            wd = _d(p.get("date"))
            if wd and wd < w.today:
                continue
            last = C.parse_any(p.get("last_activity_at") or p.get("stage_moved_at"))
            if last and (w.now - last).days > quiet:
                continue
            moved = _d(p.get("stage_moved_at") or p.get("created_at")) or w.today
            who = nm.replace("'s Project", "").strip()
            st = w.crm_stages.get(p.get("stage_id")) or {}
            out.append({"id": str(p.get("id") or nm), "title": who, "lane": str(ch.get("lane") or "wedding"), "anchor": moved,
                        "words": name_words(who), "crm": p, "crm_order": int(st.get("order") or 0), "crm_stage_name": str(st.get("name") or ""),
                        "wedding_date": wd, "waiting_kind": "client"})
    elif kind == "obligation":
        horizon = int(ap.get("horizon_days") or 60)
        for sk, sched in (ch.get("schedule") or {}).items():
            nxt = _next_date(sched.get("dates") or [], w.today, horizon)
            if not nxt:
                continue
            title = f"{sched.get('title') or sk} due {C.shortdate(nxt)}"
            amt = None
            for r in w.registers.get("money") or []:
                if str(r.get("kind") or "") == sk and _d(r.get("due")) == nxt:
                    amt = r.get("amount") or amt
            out.append({"id": f"{sk}:{nxt.isoformat()}", "title": title, "lane": str(ch.get("lane") or "money"), "anchor": nxt,
                        "words": set(), "schedule": sk, "amount": amt, "waiting_kind": "admin", "gates_override": sched.get("gates")})
    return out


def _crm_project(w: World, crm_id, who: str):
    for p in w.crm.get("projects") or []:
        if crm_id and str(p.get("id")) == str(crm_id):
            return p
    ww = name_words(who)
    for p in w.crm.get("projects") or []:
        if ww and ww <= name_words(p.get("name")):
            return p
    return None


def _next_date(mmdd: list, today, horizon: int):
    best = None
    for s in mmdd:
        try:
            m, d = int(str(s)[:2]), int(str(s)[3:5])
        except ValueError:
            continue
        for y in (today.year, today.year + 1):
            try:
                cand = today.replace(year=y, month=m, day=d)
            except ValueError:
                continue
            if 0 <= (cand - today).days <= horizon and (best is None or cand < best):
                best = cand
    return best


# ------------------------------------------------------------------ evidence
def evidence(rule: dict, ent: dict, gate: dict, w: World, due) -> str:
    """'' when the rule is not met, else one short phrase saying what proved it."""
    src = str(rule.get("source") or "")
    words = ent.get("words") or set()
    win = rule.get("within_days")
    anchor = ent.get("anchor")

    def in_win(d) -> bool:
        if not win or not anchor or not d:
            return True
        off = (d - anchor).days
        return int(win[0]) <= off <= int(win[1])

    if src == "ledger":
        if ent.get("ledger_stage") and _order(ent["ledger_stage"]) >= _order(str(rule.get("stage_min") or "")):
            return f"the ledger says {ent['ledger_stage']}"
    elif src == "crm":
        p = ent.get("crm") or {}
        st = w.crm_stages.get(p.get("stage_id")) or {}
        order = int(st.get("order") or ent.get("crm_order") or 0)
        if "stage_min_order" in rule and p and order >= int(rule["stage_min_order"]):
            return f"{crm.name()} is at {st.get('name') or order}"
        if rule.get("status") and str(p.get("status") or "") in rule["status"]:
            return f"{crm.name()} status {p.get('status')}"
    elif src == "editor":
        for folder, pr in w.editor.items():
            if not (words & name_words(folder.replace("_", " "))):
                continue
            if rule.get("exists"):
                return f"editor project {folder}"
            if rule.get("stage") and str(rule["stage"]) in (pr.get("done") or []):
                return f"editor stage {rule['stage']} done"
            if rule.get("field") and (pr.get("wedding") or {}).get(str(rule["field"])):
                return f"wedding.json has {rule['field']}"
    elif src == "calendar":
        for e in w.calendar:
            if rule.get("owner") and str(e.get("owner") or "") != str(rule["owner"]):
                continue
            title = str(e.get("title") or "")
            if rule.get("match_entity") and not _mentions(words, title):
                continue
            if rule.get("words") and not _has_words(title, rule["words"]):
                continue
            if not rule.get("match_entity") and not rule.get("words"):
                continue
            if in_win(_d(e.get("date"))):
                return f"calendar: {title[:40]} {C.shortdate(e.get('date'))}"
    elif src == "followup":
        for f in w.followups:
            if rule.get("sop") and str(f.get("sop") or "") != rule["sop"]:
                continue
            if rule.get("step") and str(f.get("step") or "") != rule["step"]:
                continue
            if rule.get("status") and str(f.get("status") or "") not in rule["status"]:
                continue
            if not rule.get("status") and str(f.get("status") or "") in ("expired", "dismissed"):
                continue
            if str(f.get("status") or "") == "done" and not rule.get("any_done") and not str(f.get("evidence") or "").startswith("by_record") \
                    and int(f.get("fire_count") or 0) and not _ticket_done_for(w, f):
                continue              # WP3: "the reminder fired" is not "the step happened"; a record read or a completed ticket is
            unit_hit = ent.get("id") and str(f.get("unit") or "") == str(ent["id"])
            if unit_hit or _mentions(words, str(f.get("subject") or "") + " " + str(f.get("unit") or "")):
                return f"follow-up {f.get('step')} {f.get('status')}"
    elif src == "ticket":
        for t in w.tickets:
            if rule.get("kinds") and str(t.get("kind") or "") not in rule["kinds"]:
                continue
            if rule.get("status") and str(t.get("status") or "") not in rule["status"]:
                continue
            blob = str(t.get("title") or "") + " " + str(t.get("summary") or "")
            if rule.get("words") and not _has_words(blob, rule["words"]):
                continue
            if words and not _mentions(words, blob):
                continue
            if in_win(_d(t.get("created"))):
                return f"'{str(t.get('title') or '')[:40]}' {C.shortdate(t.get('created'))}"
    elif src == "exec":
        for t in w.execs:
            if rule.get("kinds") and str(t.get("kind") or "") not in rule["kinds"]:
                continue
            p = t.get("payload") or {}
            blob = " ".join(str(p.get(k) or "") for k in ("to", "subject", "body", "text", "workspace_id", "recipient")) + " " + str(t.get("title") or "")
            if rule.get("words") and not _has_words(blob, rule["words"]):
                continue
            if words and not _mentions(words, blob):
                continue
            if in_win(_d(t.get("ts_done") or t.get("ts"))):
                return f"sent {C.shortdate(t.get('ts_done') or t.get('ts'))}"
    elif src == "chat":
        for r in w.chat:
            who = str(r.get("who") or "").lower()
            if who in ("sage", "agent", "") or str(r.get("channel") or "") in ("result", "card"):
                continue
            text = str(r.get("text") or "")
            if not _has_words(text, rule.get("words") or []):
                continue
            if words and not _mentions(words, text):
                continue
            if in_win(_d(r.get("ts"))):
                return f"{r.get('who')} said so {C.shortdate(r.get('ts'))}"
    elif src == "register":
        for r in w.registers.get(str(rule.get("register") or "")) or []:
            if str(rule.get("piece") or "").lower() not in str(r.get("piece") or r.get("step") or "").lower():
                continue
            if not (r.get("done") or r.get("booked") or r.get("scheduled")):
                continue
            key = str(r.get("entity") or r.get("for") or r.get("kind") or "")
            if ent.get("schedule") and str(r.get("kind") or "") == ent["schedule"] and (not r.get("due") or _d(r.get("due")) == anchor):
                return f"register: {rule['piece']} done"
            if words and _mentions(words, key + " " + str(r.get("name") or "")):
                return f"register: {rule['piece']} booked"
    return ""


# ------------------------------------------------------------------ the walk
def _ticket_done_for(w: World, f: dict) -> bool:
    """The follow-up's own ticket was completed by the operator (the best word a fired step has)."""
    for t in w.tickets:
        if (t.get("payload") or {}).get("followup") == f.get("id"):
            return str(t.get("status") or "") == "done"
    return False


def gate_due(ent: dict, gate: dict, prev_open_due=None):
    if "due_offset_days" in gate and ent.get("anchor"):
        return ent["anchor"] + timedelta(days=int(gate["due_offset_days"]))
    if "window_days" in gate:
        # the window runs from the anchor (for a lead: the day it reached its current stage); a gate
        # behind another open gate runs from that one's due date
        base = prev_open_due or ent.get("anchor")
        return (base + timedelta(days=int(gate["window_days"]))) if base else None
    return ent.get("anchor")


def walk(name: str, ch: dict, ent: dict, w: World, ask: dict) -> list:
    """Every gate for one entity: {id, label, due, status: done|up_next|ask|later|dropped|booking, why}."""
    gates = ent.get("gates_override") or ch.get("gates") or []
    rows = []
    prev_state, prev_due, prev_done_at = "start", None, None
    stopped = False
    for g in gates:
        key = f"{name}:{ent['id']}:{g['id']}"
        st = (w.state.get("gates") or {}).get(key) or {}
        due = gate_due(ent, g, prev_due if prev_state != "done" and prev_state != "start" else None)
        row = {"key": key, "chain": name, "entity": ent["id"], "title": ent["title"], "lane": ent["lane"], "gate": g["id"],
               "amount": ent.get("amount") or ch.get("default_amount"),
               "label": str(g.get("label") or g["id"]), "due": due.isoformat() if due else None, "optional": bool(g.get("optional")),
               "hard": bool(g.get("hard")), "bookable": bool(g.get("bookable")), "why": "", "status": "later", "question": str(g.get("question") or "Book it?"),
               "days_left": (due - w.today).days if due else None}
        # the owner's own word first
        if st.get("status") == "done":
            row.update(status="done", why=f"you said booked {str(st.get('ts') or '')[:10]}")
        elif st.get("status") == "dropped":
            row.update(status="dropped", why="dropped")
        else:
            if "crm_min_order" in g and int(ent.get("crm_order") or 0) >= int(g["crm_min_order"]):
                row.update(status="done", why=f"{crm.name()} is at {ent.get('crm_stage_name') or ent.get('crm_order')}")
            else:
                for rule in g.get("evidence") or []:
                    why = evidence(rule, ent, g, w, due)
                    if why:
                        row.update(status="done", why=why)
                        break
        if row["status"] == "done":
            prev_state, prev_due, prev_done_at = "done", due, (_d(st.get("ts")) if st.get("status") == "done" else None) or (due if due and due <= w.today else w.today)
            rows.append(row)
            continue
        if row["status"] == "dropped":
            # a lead the owner dropped a chase on is latched: nothing behind it is asked (no retainer
            # card after a dropped contract) until the CRM shows a new stage, payment or message
            # (2026-10-17: Arden / Erin retainer cards came back after the contract was dropped)
            if ch.get("entity") == "lead" and not _crm_moved_since(ent, st.get("ts")):
                row.update(why=f"dropped; nothing after it is asked until {crm.name()} moves", latched=True)
                stopped = True
            prev_state, prev_due, prev_done_at = "done", due, prev_done_at
            rows.append(row)
            continue
        if stopped:
            prev_state, prev_due = "open", due
            rows.append(row)
            continue
        if st.get("status") == "booking":
            row.update(status="booking", why="a ticket is out for it")
            rows.append(row)
            prev_state, prev_due, stopped = "open", due, True
            continue
        ending = prev_state == "done" or prev_state == "start" or (prev_due is not None and (prev_due - w.today).days <= int(ask["ending_days"]))
        if not ending:
            rows.append(row)
            prev_state, prev_due, stopped = "open", due, True
            continue
        row["status"] = "up_next"
        row["ask_due"] = _ask_now(ch, ent, g, due, st, w, ask)
        if row["ask_due"]:
            row["status"] = "ask"
            stale = _stale_lead(ch, ent, due, st, w)
            if stale:
                # no booking card for a lead that went cold: one "archive this lead?" in the digest
                # instead (2026-10-29: Lane Calloway-Brook / Ellis Oakhurst-Vane carded 265 / 124 days after the step was due)
                row.update(status="stale", why=stale)
        rows.append(row)
        prev_state, prev_due, stopped = "open", due, True      # one open gate per entity: nothing behind it is asked
    return rows


def _crm_moved_since(ent: dict, ts) -> bool:
    """True when the lead's CRM project changed stage or had activity after the drop."""
    since = C.parse_any(ts)
    if not since:
        return False
    p = ent.get("crm") or {}
    return any((d := C.parse_any(p.get(k))) and d > since for k in ("stage_moved_at", "last_activity_at"))


def _hold_lead_followups(rows: list, st: dict, cancel=None) -> int:
    """A latched lead's proposal nudges (followups 'inquiry' quote-followup-*) stop too, once per drop."""
    n = 0
    for r in rows:
        g = st["gates"].get(r["key"]) or {}
        if not r.get("latched") or g.get("followups_held") == g.get("ts"):
            continue
        try:
            if cancel is None:
                import followups
                cancel = followups.cancel_unit
            cancel("inquiry", r["entity"], why="owner dropped the lead's chase", keep_steps=("reply-sla",))
            g["followups_held"] = g.get("ts")
            n += 1
        except Exception as e:  # noqa: BLE001
            C.log("chain_hold_error", key=r["key"], error=f"{type(e).__name__}: {str(e)[:120]}")
    return n


def _stale_lead(ch: dict, ent: dict, due, st: dict, w: World) -> str:
    """'' or why a lead's next step is too old to card: the step was due more than stale_days ago, or
    the CRM shows no activity in that time. An owner's Keep on the archive card turns it off."""
    if ch.get("entity") != "lead" or st.get("stale_kept"):
        return ""
    days = int((ch.get("applies") or {}).get("stale_days") or 60)
    if due is not None and (w.today - due).days > days:
        return f"the step was due {(w.today - due).days} days ago"
    p = ent.get("crm") or {}
    last = C.parse_any(p.get("last_activity_at") or p.get("stage_moved_at") or p.get("created_at"))
    if last and (w.now - last).days > days:
        return f"no {crm.name()} activity in {(w.now - last).days} days"
    return ""


def _archive_question(row: dict) -> dict:
    """The one card a stale lead gets: held for the weekly digest (not an urgent header)."""
    return {"header": f"Archive this lead? · {row['title'][:30]}", "lane": row["lane"],
            "question": f"{row['title']}: {row['why']}. Archive this lead?",
            "details": f"Chain {row['chain']}, gate {row['gate']} ({row['label']}). No booking card was posted for it.",
            "options": [{"label": "Archive (Recommended)", "description": f"stop asking about this lead; {crm.name()} activity brings it back"},
                        {"label": "Keep", "description": "the lead is still live; ask about its next step as usual"}],
            "chain_key": row["key"]}


def _ask_now(ch: dict, ent: dict, g: dict, due, st: dict, w: World, ask: dict) -> str:
    """'' or the reason this gate is asked now ('T-7', 'due 10/20', 'overdue 3 d')."""
    asks = int(st.get("asks") or 0)
    if asks >= int(ask["max_asks"]):
        return ""
    last = C.parse_any(st.get("last_ask"))
    if st.get("status") == "not_yet" and last and (w.now - last).days < int(ask["reask_days"]):
        return ""
    offsets = ch.get("offsets_days")
    if offsets and ent.get("anchor"):
        crossed = [int(o) for o in offsets if w.today >= ent["anchor"] + timedelta(days=int(o))]
        if not crossed:
            return ""
        o = max(crossed)
        if o in (st.get("offsets_asked") or []):
            return ""
        return f"T{o:+d}"
    if due is None:
        return "no date"
    if (due - w.today).days > int(ask["lead_days"]):
        return ""
    if last and (w.now - last).days < int(ask["reask_days"]):
        return ""
    left = (due - w.today).days
    return f"due {C.shortdate(due)}" if left >= 0 else f"overdue {-left} d"


def gaps(w: World | None = None, c: dict | None = None) -> list:
    """Every chain, every entity, every gate (the dry-run view and entities.py's source)."""
    c = c or cfg()
    w = w or World()
    out = []
    for name, ch in c["chains"].items():
        for ent in entities_for(name, ch, w):
            out.extend(walk(name, ch, ent, w, c["ask"]))
    return out


# ------------------------------------------------------------------ the card
def _question(row: dict, ent_rows: list, ch: dict, g: dict, ask: dict) -> dict:
    opts = ask["options"]
    prev = [r for r in ent_rows if r["status"] == "done"]
    prev_txt = f"{prev[-1]['label'].capitalize()} is done ({prev[-1]['why']}). " if prev else ""
    when = ""
    if row.get("due"):
        dl = int(row.get("days_left") or 0)
        when = (f"It is due {C.shortdate(row['due'])}" + (f" ({dl} days)" if dl > 0 else (" (today)" if dl == 0 else f" ({-dl} days ago)")) + ". ")
    anchor_txt = ""
    if ch.get("entity") == "couple" and row.get("days_left") is not None:
        anchor_txt = ""
    q = (f"{row['title']}: {prev_txt}{row['label'].capitalize()} has nothing showing it is booked. {when}"
         f"{'Optional: say Drop if this shoot does not need it. ' if row.get('optional') else ''}{row.get('question') or 'Book it?'}")
    rec = "book" if row.get("bookable") else "booked"
    labels = {k: v for k, v in opts.items()}
    labels[rec] = labels[rec] + " (Recommended)"
    options = [{"label": labels[rec]}] + [{"label": labels[k]} for k in ("booked", "book", "not_yet", "drop") if k != rec]
    desc = {"booked": "it's done; I'll mark the gate passed", "book": "I queue it for the operator / doer; anything irreversible stops at a checkpoint card",
            "not_yet": f"ask again in {ask['reask_days']} days", "drop": "this entity does not need it; never asked again"}
    for o in options:
        k = next(k for k, v in labels.items() if v == o["label"])
        o["description"] = desc[k]
    waits = ent_rows[ent_rows.index(row) + 1:ent_rows.index(row) + 2]
    item = {"kind": "chain", "lane": row["lane"], "title": row["title"], "age_days": max(0, -(row.get("days_left") or 0)),
            "why": ("the one before it is done; booking this now keeps the chain from stalling" if prev else "nothing in the chain has started"),
            "blocks": (waits[0]["label"] if waits else ""), "waiting_on": "the couple" if ch.get("entity") in ("couple", "lead") else ""}
    return {"header": f"Next step · {ch.get('title') or row['chain']} · {row['title'][:30]}", "lane": row["lane"], "question": q,
            "details": f"Chain {row['chain']}, gate {row['gate']}. " + "; ".join(f"{r['label']}: {r['status']}" + (f" ({r['why']})" if r['why'] else "") for r in ent_rows),
            "advice": advice.line(item), "options": options, "chain_key": row["key"], "book_instruction": str(g.get("book_instruction") or "")}


def _post(q: dict, dry: bool) -> str:
    if dry:
        print("WOULD POST " + json.dumps({k: v for k, v in q.items() if k not in ("details",)}, ensure_ascii=False)[:500])
        return "dry"
    import cards
    r = cards.post([q], owner="tracking.chains", lane=q["lane"])
    if not r.get("posted"):
        C.log("chain_post_refused", key=q.get("chain_key"), reason=str(r.get("reason"))[:200])
        return ""
    return str(r.get("qid") or "")


# ------------------------------------------------------------------ the owner's answers
def harvest(st: dict, answers: list | None = None, opts: dict | None = None, mint=None) -> int:
    opts = opts or ASK_DEFAULTS["options"]
    answers = answers if answers is not None else C.card_answers(30)
    by_qid = {}
    for a in answers:
        by_qid.setdefault(str(a.get("qid")), a)
    n = 0
    for key, g in st["gates"].items():
        sq = str(g.get("stale_qid") or "")
        if sq and sq in by_qid and not g.get("stale_answer"):
            a = by_qid[sq]
            g["stale_answer"] = str(a.get("option") or "").replace("(Recommended)", "").strip().lower() or "archive"
            g["ts"] = str(a.get("ts") or C.now_local().strftime("%Y-%m-%dT%H:%M:%S"))
            if g["stale_answer"].startswith("keep"):
                g["stale_kept"] = g["ts"]
            else:
                g["status"] = "dropped"
            n += 1
            C.log("chain_stale_answer", key=key, option=g["stale_answer"])
            continue
        qid = str(g.get("qid") or "")
        if not qid or g.get("status") != "asked" or qid not in by_qid:
            continue
        a = by_qid[qid]
        opt = str(a.get("option") or "").replace("(Recommended)", "").strip().lower()
        ts = str(a.get("ts") or C.now_local().strftime("%Y-%m-%dT%H:%M:%S"))
        if opt.startswith(opts["booked"].lower()):
            g["status"] = "done"
        elif opt.startswith(opts["book"].lower()):
            g["status"] = "booking"
            g["ticket"] = (mint or _mint_ticket)(key, g)
        elif opt.startswith(opts["not_yet"].lower()):
            g["status"] = "not_yet"
        elif opt.startswith(opts["drop"].lower()):
            g["status"] = "dropped"
        else:
            g["status"] = "not_yet"
        g["ts"] = ts
        if a.get("text"):
            g["answer_text"] = str(a["text"])[:300]
        n += 1
        C.log("chain_answer", key=key, option=opt)
    return n


def _mint_ticket(key: str, g: dict) -> str:
    """'Book it for me' -> one staged chain_book ticket the operator / doer picks up. Never a send."""
    try:
        import tickets
        chain, entity, gate = key.split(":", 2)
        tid = tickets.make_id("tracking.chains", key)
        tickets.add_many([{"source": "tracking.chains", "source_id": key, "lane": str(g.get("lane") or "personal"), "kind": "chain_book",
                           "title": f"Book {g.get('label') or gate} for {g.get('title') or entity}"[:200],
                           "summary": str(g.get("book_instruction") or "")[:600],
                           "payload": {"chain": chain, "entity": entity, "gate": gate, "title": g.get("title"), "due": g.get("due")}}],
                          owner="tracking.chains")
        return tid
    except Exception as e:  # noqa: BLE001
        C.log("chain_mint_error", key=key, error=f"{type(e).__name__}: {str(e)[:120]}")
        return ""


def _booking_still_open(st: dict, tix: dict | None = None) -> int:
    """A 'Book it for me' whose ticket was dismissed goes back to open so it can be asked again."""
    tix = tix if tix is not None else C.tickets_all()
    n = 0
    for key, g in st["gates"].items():
        if g.get("status") != "booking":
            continue
        t = tix.get(str(g.get("ticket") or ""))
        if t and t.get("status") == "dismissed":
            g["status"] = "not_yet"; n += 1
        elif t and t.get("status") == "done":
            g["status"] = "done"; g["ts"] = str(t.get("updated") or ""); n += 1
    return n


# ------------------------------------------------------------------ tick
def tick(now=None, dry: bool = False, w: World | None = None, c: dict | None = None) -> dict:
    c = c or cfg()
    ask = c["ask"]
    w = w or World(now=now)
    st = w.state
    harvested = harvest(st, opts=ask["options"]) if not dry else 0
    _booking_still_open(st) if not dry else 0
    rows = gaps(w, c)
    _hold_lead_followups(rows, st) if not dry else 0
    day = w.today.isoformat()
    posted = []
    quiet = C.in_quiet(w.now, (C.cfg().get("rhythm") or {}).get("quiet_hours") or (21, 7))
    by_ent: dict = {}
    for r in rows:
        by_ent.setdefault((r["chain"], r["entity"]), []).append(r)
    for (chain, ent_id), ent_rows in by_ent.items():
        r = next((x for x in ent_rows if x["status"] == "stale"), None)
        if r and not (st["gates"].get(r["key"]) or {}).get("stale_qid") and not (quiet and not dry):
            qid = _post(_archive_question(r), dry)
            if qid and not dry:
                st["gates"].setdefault(r["key"], {"asks": 0}).update(stale_qid=qid, lane=r["lane"], label=r["label"], title=r["title"])
            if qid:
                posted.append({"key": r["key"], "qid": qid, "why": "stale"})
                C.log("chain_stale_posted", key=r["key"], qid=qid, why=r["why"])
            continue
        r = next((x for x in ent_rows if x["status"] == "ask"), None)
        if not r or (quiet and not dry):
            continue
        lane = r["lane"]
        ld = st["lane_day"].get(lane) or {}
        n_today = int(ld.get("n") or 0) if ld.get("day") == day else 0
        if n_today >= int(ask["max_per_lane_per_day"]):
            continue
        ch = c["chains"][chain]
        ent = next((e for e in entities_for(chain, ch, w) if e["id"] == ent_id), None)
        gates = (ent or {}).get("gates_override") or ch.get("gates") or []
        g = next((x for x in gates if x["id"] == r["gate"]), {})
        q = _question(r, ent_rows, ch, g, ask)
        qid = _post(q, dry)
        if not qid:
            continue
        gs = st["gates"].setdefault(r["key"], {"asks": 0})
        gs.update({"status": "asked", "qid": qid, "asks": int(gs.get("asks") or 0) + 1, "last_ask": w.now.strftime("%Y-%m-%d %H:%M"),
                   "lane": lane, "label": r["label"], "title": r["title"], "due": r.get("due"), "book_instruction": q.get("book_instruction")})
        if r["ask_due"].startswith("T"):
            gs.setdefault("offsets_asked", []).append(int(r["ask_due"][1:]))
        st["lane_day"][lane] = {"day": day, "n": n_today + 1}
        posted.append({"key": r["key"], "qid": qid, "why": r["ask_due"]})
        C.log("chain_posted", key=r["key"], qid=qid, why=r["ask_due"])
    if not dry:
        C.save_state("chains", st)
        C.save_state("chain_gaps", {"ts": w.now.strftime("%Y-%m-%d %H:%M"), "rows": rows})
    out = {"harvested": harvested, "entities": len(by_ent), "asks": sum(1 for r in rows if r["status"] == "ask"), "posted": posted}
    print("CHAINS " + json.dumps(out))
    return out


def cached_gaps() -> list:
    return list((C.state("chain_gaps", {}) or {}).get("rows") or [])


def print_gaps(rows: list) -> None:
    cur = None
    for r in rows:
        head = (r["chain"], r["entity"])
        if head != cur:
            cur = head
            print(f"\n[{r['chain']}] {r['title']}")
        mark = {"done": "done", "ask": "ASK ", "up_next": "next", "later": "    ", "dropped": "drop", "booking": "book"}.get(r["status"], "?")
        print(f"  {mark} {r['label'][:44]:44} due {r['due'] or '-':10} {r['why'] or ''}" + (f"  <- {r['ask_due']}" if r.get("ask_due") else ""))


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("cmd", nargs="?", default="tick", choices=["tick", "gaps"])
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args()
    if a.cmd == "gaps":
        print_gaps(gaps())
    else:
        if a.dry_run:
            print_gaps(gaps())
        tick(dry=a.dry_run)

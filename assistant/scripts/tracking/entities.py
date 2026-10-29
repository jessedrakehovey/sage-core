"""entities.py - the world model (ruling 11, 2026-10-15; the shape is the property OS's world_model.py).

One row per thing in motion, with its stage, the next required action, its deadline and how many
days it is overdue: leads and couples / edits (from the chains), money obligations (follow-ups +
the money register + the tax schedule), people (Wiki/People + the people register + personal
follow-ups), trips and appointments (the calendar + the trips register). The priority brain
(priority.py) scores every row; the result is assistant/data/tracking/entities.json (git-ignored),
read by the Friday readback (Q2 nudges), the day plan and the chaser's advice.

Registers (assistant/data/registers/*.json) are what the owner fills from chat for things no
record source knows: `init` writes empty ones with their shape documented inside.

  python assistant/scripts/tracking/entities.py            # build + write the ledger, print a summary
  python assistant/scripts/tracking/entities.py --json     # the rows
  python assistant/scripts/tracking/entities.py init       # create the empty registers
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import chains  # noqa: E402
import common as C  # noqa: E402
import priority  # noqa: E402

REGISTER_SHAPES = {
    "money": {"_doc": "Money obligations the bank feed and mail do not show on their own. One entry per obligation: "
                      "{name, kind: bill|subscription|insurance|registration|loan|quarterly|tax_season, amount, due: YYYY-MM-DD, "
                      "recur: monthly|yearly|quarterly|'' , piece: bookkeeping|estimate|payment|documents|prepared|filed (for the tax "
                      "chains), done: true/false, scheduled: YYYY-MM-DD, note}. Sage adds entries when you tell it in chat.", "entries": []},
    "people": {"_doc": "People to keep up with. {name, relation, birthday: MM-DD, last_contact: YYYY-MM-DD, cadence_days, "
                       "promised: [{what, by: YYYY-MM-DD, done}], note}.", "entries": []},
    "trips": {"_doc": "Trips and appointments with pieces to book. {name, start: YYYY-MM-DD, end, entity (a couple / shoot it belongs to), "
                      "pieces: [{piece: travel|lodging|rentals|second shooter|tickets|..., booked, confirmation, note}], "
                      "checklist: true to get a day-before checklist line, note}.", "entries": []},
}


def init(force: bool = False) -> list:
    made = []
    chains.REGISTERS.mkdir(parents=True, exist_ok=True)
    for name, shape in REGISTER_SHAPES.items():
        p = chains.REGISTERS / f"{name}.json"
        if p.exists() and not force:
            continue
        C.save_json(p, shape)
        made.append(str(p))
    return made


def _row(kind, ident, title, lane, stage, next_action, deadline, now, **kw) -> dict:
    dl = C.parse_any(deadline)
    r = {"kind": kind, "id": str(ident), "title": str(title)[:80], "lane": lane, "stage": stage, "next_action": next_action,
         "deadline": dl.strftime("%Y-%m-%d") if dl else None,
         "days_overdue": (now.date() - dl.date()).days if dl else None, "owner": kw.pop("owner", "owner1")}
    r.update(kw)
    return r


# ------------------------------------------------------------------ leads, couples, edits (from the chains)
def from_chains(rows: list, now) -> list:
    out = []
    by_ent: dict = {}
    for r in rows:
        by_ent.setdefault((r["chain"], r["entity"]), []).append(r)
    for (chain, ent), gs in by_ent.items():
        done = [g for g in gs if g["status"] == "done"]
        open_ = next((g for g in gs if g["status"] in ("ask", "up_next", "booking", "later")), None)
        kind = {"lead": "lead", "shoot": "couple", "edit": "edit", "money": "obligation"}.get(chain, chain)
        stage = done[-1]["label"] if done else "start"
        if open_ is None or any(g["status"] == "stale" for g in gs):
            continue                                   # every gate passed, or a cold lead (its archive card is in the digest)
        amt = open_.get("amount")
        out.append(_row(kind, f"{chain}:{ent}", gs[0]["title"], gs[0]["lane"], stage,
                        f"book {open_['label']}" if open_["status"] != "booking" else f"{open_['label']} (ticket out)",
                        open_.get("due"), now, waiting_kind="client" if kind in ("lead", "couple", "edit") else "admin",
                        hard=bool(open_.get("hard")), amount=amt, chain=chain, gate=open_["gate"], gate_status=open_["status"],
                        sla_days=7 if kind != "lead" else 3))
    return out


# ------------------------------------------------------------------ money obligations
_AMT = re.compile(r"\$\s?([0-9][0-9,]*(?:\.[0-9]{2})?)")
_DUE = re.compile(r"due\s+(\d{4}-\d{2}-\d{2}|\d{1,2}/\d{1,2}(?:/\d{2,4})?)", re.I)


def _amount(s: str):
    m = _AMT.search(str(s or ""))
    return float(m.group(1).replace(",", "")) if m else None


def money_rows(fus: list, registers: dict, now) -> list:
    out = []
    for f in fus:
        if str(f.get("sop") or "") != "money" or str(f.get("status") or "") not in ("waiting", "fired"):
            continue
        subj = str(f.get("subject") or "")
        due = None
        m = _DUE.search(subj)
        if m:
            due = C.parse_any(m.group(1)) or _mdy(m.group(1), now)
        due = due or C.parse_any(f.get("fire_at"))
        out.append(_row("obligation", f"fu:{f.get('id')}", subj[:70], str(f.get("lane") or "money"), str(f.get("step") or "bill"),
                        "pay / confirm it is paid", due, now, waiting_kind="vendor", amount=_amount(subj), source="followup"))
    for r in registers.get("money") or []:
        if r.get("done") or str(r.get("kind") or "") in ("quarterly", "tax_season"):
            continue
        due = C.parse_any(r.get("due"))
        if due and (now.date() - due.date()).days > 60:
            continue
        kind = str(r.get("kind") or "bill")
        out.append(_row("obligation", f"reg:{r.get('name')}", f"{r.get('name')} ({kind})", "money", kind,
                        "schedule the payment" if not r.get("scheduled") else f"paid on {r.get('scheduled')}", due, now,
                        waiting_kind="vendor", amount=r.get("amount"), hard=kind in ("insurance", "registration", "loan"), source="register"))
    return out


def _mdy(s: str, now):
    try:
        parts = [int(x) for x in s.split("/")]
        y = parts[2] if len(parts) == 3 else now.year
        y = y + 2000 if y < 100 else y
        return C.parse_any(f"{y:04d}-{parts[0]:02d}-{parts[1]:02d}")
    except (ValueError, IndexError):
        return None


# ------------------------------------------------------------------ people
_BDAY = re.compile(r"\*\*Birthday:\*\*\s*([0-9]{1,2}[-/][0-9]{1,2}(?:[-/][0-9]{2,4})?|[A-Z][a-z]+ [0-9]{1,2})", re.M)
_CADENCE_DEFAULT = 60


def _wiki_people() -> list:
    out = []
    try:
        files = sorted((C.VAULT / "Wiki" / "People").glob("*.md"))
    except OSError:
        return out
    for f in files:
        if f.name.lower().startswith("about") or f.stem in ("owner1 example", "owner6 example", "Family and Friends"):
            continue
        try:
            txt = f.read_text(encoding="utf-8")
        except OSError:
            continue
        if not re.search(r"^type:\s*person", txt, re.M):
            continue
        m = _BDAY.search(txt)
        upd = re.search(r"^updated:\s*(\d{4}-\d{2}-\d{2})", txt, re.M)
        out.append({"name": f.stem, "birthday": m.group(1) if m else "", "updated": upd.group(1) if upd else "",
                    "lead": "(lead)" in f.stem.lower()})
    return out


def _next_birthday(s: str, today: date):
    s = str(s or "").strip()
    m = re.match(r"(\d{1,2})[-/](\d{1,2})", s)
    if m:
        mo, d = int(m.group(1)), int(m.group(2))
    else:
        try:
            dt = C.parse_any(f"{today.year}-01-01") and __import__("datetime").datetime.strptime(s + f" {today.year}", "%B %d %Y")
            mo, d = dt.month, dt.day
        except (ValueError, TypeError):
            return None
    for y in (today.year, today.year + 1):
        try:
            cand = date(y, mo, d)
        except ValueError:
            continue
        if cand >= today:
            return cand
    return None


def people_rows(registers: dict, fus: list, chat: list, now, wiki: list | None = None) -> list:
    out = []
    today = now.date()
    last_seen: dict = {}
    for r in chat:
        who = str(r.get("who") or "")
        if who.lower() in ("sage", "agent", ""):
            continue
        for w in re.findall(r"\b[A-Z][a-z]{2,}\b", str(r.get("text") or "")):
            last_seen[w.lower()] = max(last_seen.get(w.lower(), ""), str(r.get("ts") or "")[:10])
    people = list(registers.get("people") or [])
    names = {str(p.get("name") or "").lower() for p in people}
    for wp in (wiki if wiki is not None else _wiki_people()):
        if wp["name"].lower() in names or wp.get("lead"):
            continue
        people.append({"name": wp["name"], "birthday": wp.get("birthday"), "last_contact": wp.get("updated"), "source": "wiki"})
    for p in people:
        name = str(p.get("name") or "").strip()
        if not name:
            continue
        first = name.split()[0].lower()
        last_contact = max(str(p.get("last_contact") or ""), last_seen.get(first, ""))
        cadence = int(p.get("cadence_days") or _CADENCE_DEFAULT)
        actions = []                   # (rank, deadline, what, who waits): a promise beats a birthday beats a quiet spell
        for pr in p.get("promised") or []:
            if pr.get("done"):
                continue
            actions.append((0, C.parse_any(pr.get("by")), f"you promised: {pr.get('what')}", "household"))
        bd = _next_birthday(p.get("birthday"), today)
        if bd and (bd - today).days <= 30:
            actions.append((1, C.parse_any(bd.isoformat()), f"birthday {C.shortdate(bd)}: a message or a gift", "household"))
        lc = C.parse_any(last_contact)
        if lc and (today - lc.date()).days >= cadence:
            actions.append((2, C.parse_any((lc.date() + timedelta(days=cadence)).isoformat()), f"reach out, last contact {C.shortdate(lc)}", "none"))
        if not actions:
            continue
        actions.sort(key=lambda a: (a[0], a[1] is None, a[1] or now))
        _, dl, what, wk = actions[0]
        out.append(_row("person", f"person:{name}", name, "personal", f"last contact {C.shortdate(lc) if lc else 'unknown'}",
                        what, dl.strftime("%Y-%m-%d") if dl else None, now, waiting_kind=wk, sla_days=14, source=p.get("source") or "register"))
    for f in fus:
        if str(f.get("sop") or "") != "personal" or str(f.get("status") or "") not in ("waiting", "fired"):
            continue
        out.append(_row("person", f"fu:{f.get('id')}", str(f.get("subject") or "")[:70], "personal", str(f.get("step") or ""),
                        "follow up" if f.get("step") == "awaiting-reply" else "do the reminder", f.get("fire_at"), now,
                        waiting_kind="household", sla_days=3, source="followup"))
    return out


# ------------------------------------------------------------------ trips and appointments
_SKIP_TITLES = ("church", "birthday", "reminder")


def trip_rows(cal: list, registers: dict, now, lookahead_days: int = 30) -> list:
    out = []
    today = now.date()
    seen = set()
    for r in registers.get("trips") or []:
        start = C.parse_any(r.get("start"))
        if not start or (start.date() - today).days > lookahead_days or (today - start.date()).days > 1:
            continue
        unbooked = [p for p in r.get("pieces") or [] if not p.get("booked")]
        name = str(r.get("name") or "trip")
        seen.add(name.lower())
        if unbooked:
            out.append(_row("trip", f"trip:{name}", name, "personal", f"{len(r.get('pieces') or [])-len(unbooked)}/{len(r.get('pieces') or [])} booked",
                            f"book {unbooked[0].get('piece')}", (start.date() - timedelta(days=3)).isoformat(), now,
                            waiting_kind="none", amount=r.get("amount"), source="register", starts=start.strftime("%Y-%m-%d")))
        elif r.get("checklist") and 0 <= (start.date() - today).days <= 1:
            out.append(_row("trip", f"trip:{name}", name, "personal", "all booked", "day-before checklist: confirmations, chargers, cards, addresses",
                            (start.date() - timedelta(days=1)).isoformat(), now, waiting_kind="none", source="register", starts=start.strftime("%Y-%m-%d")))
    for e in cal:
        d = C.parse_any(e.get("date"))
        if not d or not (0 <= (d.date() - today).days <= lookahead_days):
            continue
        title = str(e.get("title") or "")
        key = (d.strftime("%Y-%m-%d"), title.lower())
        if any(w in title.lower() for w in _SKIP_TITLES) or title.lower() in seen or e.get("booking") in ("booked", "inquiry") or key in seen:
            continue                                   # weddings and leads ride their own chains; one row per event
        seen.add(key)
        out.append(_row("appointment", f"event:{e.get('uid') or title}", title[:70], "owner6" if e.get("owner") == "owner6" else "personal",
                        "on the calendar", "day before: confirm time, place and what to bring", (d.date() - timedelta(days=1)).isoformat(), now,
                        owner=str(e.get("owner") or "owner1"), waiting_kind="none", sla_days=1, source="calendar", starts=d.strftime("%Y-%m-%d"), time=e.get("time")))
    return out


# ------------------------------------------------------------------ build
def build(w: chains.World | None = None, c: dict | None = None, wiki: list | None = None) -> dict:
    c = c or C.cfg()
    w = w or chains.World()
    gap_rows = chains.gaps(w)
    rows = []
    rows += from_chains(gap_rows, w.now)
    rows += money_rows(w.followups, w.registers, w.now)
    rows += people_rows(w.registers, w.followups, w.chat, w.now, wiki=wiki)
    rows += trip_rows(w.calendar, w.registers, w.now)
    for r in rows:
        if r.get("days_overdue") is not None:
            r["age_days"] = max(0, r["days_overdue"])
        priority.assess(r, w.now, c)
    rows = priority.rank(rows, w.now, c)
    by_kind: dict = {}
    for r in rows:
        by_kind[r["kind"]] = by_kind.get(r["kind"], 0) + 1
    by_q: dict = {}
    for r in rows:
        by_q[r["quadrant"]] = by_q.get(r["quadrant"], 0) + 1
    return {"ts": w.now.strftime("%Y-%m-%d %H:%M"), "rows": rows, "gaps": gap_rows,
            "summary": {"rows": len(rows), "by_kind": by_kind, "by_quadrant": by_q,
                        "overdue": sum(1 for r in rows if (r.get("days_overdue") or 0) > 0)}}


def run(w=None) -> dict:
    model = build(w)
    C.save_state("entities", {k: v for k, v in model.items() if k != "gaps"})
    C.save_state("chain_gaps", {"ts": model["ts"], "rows": model["gaps"]})
    return model


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("cmd", nargs="?", default="run", choices=["run", "init"])
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--top", type=int, default=25)
    a = ap.parse_args()
    if a.cmd == "init":
        made = init()
        print("REGISTERS " + json.dumps(made)); return 0
    if not chains.REGISTERS.exists():
        init()
    m = run()
    s = m["summary"]
    print(f"WORLD MODEL rows={s['rows']} by_kind={s['by_kind']} by_quadrant={s['by_quadrant']} overdue={s['overdue']}")
    if a.json:
        print(json.dumps(m["rows"], indent=1, ensure_ascii=False))
    else:
        print(priority.render(m["rows"], a.top))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

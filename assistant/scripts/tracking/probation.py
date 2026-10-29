"""probation.py - the 20-right-in-a-row tally (ruling 9, 2026-10-15). Zero-LLM, read-only.

example's probation: a class of action earns automation only after the owner approved it unchanged
twenty times running. the owner kept the number and named two classes (tracking.json -> probation):

  record_write          internal record writes (the weekly record check, mined dates, calendar
                        holds, HoneyBook notes / field updates)
  routine_client_send   the post-wedding thank-you, the "footage is safe" note, the 2-day quote
                        follow-up

The tally reads the card answers (data/cards/answers.jsonl) joined to the cards themselves and the
executor ledger. An AS-IS approval is the exec's own approve label, untouched: no edit, no typed
note, no Skip / "not yet", and the minted ticket was not undone or cancelled. Anything else resets
that class to zero. At `threshold` Sage posts ONE card asking the owner whether to make the class
automatic, once per run of approvals; his answer is recorded here and nowhere else.

THE CODE NEVER FLIPS A GATE. A "yes" changes no behaviour: exec_store.enqueue still refuses
anything that is not the owner's tap on a card. How a flip is made by hand is written in
assistant/README.md under "Probation". State: data/tracking/probation.json.

  python assistant/scripts/tracking/probation.py            # the tally per class
  python assistant/scripts/tracking/probation.py tick       # tally + the ask card when a class is due (the spine)
  python assistant/scripts/tracking/probation.py --json
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import advice  # noqa: E402
import common as C  # noqa: E402

_CARD_NOTE = re.compile(r"\bcard (q[0-9a-f]{12})\b")
OPT_KEEP, OPT_AUTO = "Keep asking me (Recommended)", "Yes, make it automatic"


def pcfg() -> dict:
    return C.cfg()["probation"]


def _state() -> dict:
    st = C.state("probation", {})
    st.setdefault("classes", {})
    return st


# ------------------------------------------------------------------ which class is a card
def card_tickets(tickets: dict | None = None) -> dict:
    """qid -> the assistant ticket staged on it (its history says 'card <qid>')."""
    out = {}
    for t in (tickets if tickets is not None else C.tickets_all()).values():
        for h in t.get("history") or []:
            for qid in _CARD_NOTE.findall(str(h.get("note") or "")):
                out.setdefault(qid, t)
    return out


def classify(card: dict, ticket: dict | None, classes: dict) -> str:
    """The probation class of a card, or ''. Exec kind first; a send also needs its follow-up step
    or header words to match (a reply to a lead is never 'routine')."""
    q = (card.get("questions") or [{}])[0] if isinstance(card, dict) else {}
    ex = q.get("exec") or {}
    kind = str(ex.get("kind") or "")
    if not kind:
        return ""
    header = str(q.get("header") or "").lower()
    step = ""
    if ticket and ticket.get("kind") == "followup":
        p = ticket.get("payload") or {}
        step = f"{p.get('sop') or ''}/{p.get('step') or ''}"
    for name, spec in classes.items():
        if not isinstance(spec, dict) or kind not in (spec.get("exec_kinds") or []):
            continue
        steps, words = spec.get("followup_steps") or [], [str(w).lower() for w in spec.get("header_words") or []]
        if not steps and not words:
            return name
        if (step and step in steps) or any(w in header for w in words):
            return name
    return ""


def _exec_cancelled(qid: str, ledger: list) -> bool:
    """The ticket minted for this card was undone / cancelled (an approval taken back)."""
    tickets = {}
    for r in ledger:
        if r.get("approval_ref") == f"card:{qid}":
            tickets[r["id"]] = dict(tickets.get(r["id"], {}), **r)
        elif r.get("id") in tickets:
            tickets[r["id"]].update(r)
    return any(t.get("status") == "cancelled" for t in tickets.values())


def as_is(answer: dict, card: dict, ledger: list) -> bool:
    q = (card.get("questions") or [{}])[0]
    ex = q.get("exec") or {}
    label = str(ex.get("approve_label") or "").strip().lower()
    opt = str(answer.get("option") or "").strip().lower()
    if not label or opt != label:
        return False
    if answer.get("revised") or str(answer.get("revised_text") or "").strip() or str(answer.get("text") or "").strip():
        return False
    if _exec_cancelled(str(answer.get("qid") or ""), ledger):
        return False
    return True


def tally(now=None, answers: list | None = None, cards: dict | None = None, tickets: dict | None = None,
          ledger: list | None = None, c: dict | None = None) -> dict:
    """{class: {streak, total, last_qid, last_ts, resets, history[-5:]}} from the answers in order."""
    pc = (c or C.cfg())["probation"]
    classes = pc.get("classes") or {}
    days = float(pc.get("lookback_days") or 180)
    answers = answers if answers is not None else C.card_answers(days)
    ledger = ledger if ledger is not None else C.exec_tickets(days)
    by_ticket = card_tickets(tickets)
    latest = {}
    for a in answers:                                   # the last tap on a card is the one that counts
        latest[str(a.get("qid"))] = a
    rows = sorted(latest.values(), key=lambda a: float(a.get("epoch") or C.parse_any(a.get("ts")).timestamp() if C.parse_any(a.get("ts")) else 0))
    out = {name: {"title": spec.get("title") or name, "streak": 0, "total": 0, "as_is": 0, "resets": 0, "last_qid": "", "last_ts": "", "history": []}
           for name, spec in classes.items() if isinstance(spec, dict)}
    for a in rows:
        qid = str(a.get("qid") or "")
        card = (cards or {}).get(qid) if cards is not None else C.card(qid)
        if not card:
            continue
        who = str(a.get("by") or "")
        if not (who.endswith("owner1") or who.endswith("owner6")):
            continue
        cls = classify(card, by_ticket.get(qid), classes)
        if not cls:
            continue
        row = out[cls]
        ok = as_is(a, card, ledger)
        row["total"] += 1
        if ok:
            row["streak"] += 1
            row["as_is"] += 1
        else:
            row["streak"] = 0
            row["resets"] += 1
        row["last_qid"], row["last_ts"] = qid, str(a.get("ts") or "")
        row["history"] = (row["history"] + [{"qid": qid, "ok": ok, "option": str(a.get("option") or "")[:40], "ts": str(a.get("ts") or "")}])[-5:]
    return out


# ------------------------------------------------------------------ the ask
def ask_card(name: str, row: dict, threshold: int) -> dict:
    title = row.get("title") or name
    return {"header": "Make it automatic?", "lane": "personal", "probation_class": name,
            "question": f"You approved {row['streak']} {title} in a row without changing one (last: card {row['last_qid']}, {row['last_ts'][:16]}). "
                        f"Should this class go out without a card from now on? Nothing changes by itself: a yes is recorded, and the switch is made by hand "
                        f"(README, Probation) so you can see exactly what it opens.",
            "details": "Last five: " + "; ".join(f"{h['qid']} {h['option']}" for h in row.get("history") or []),
            "options": [{"label": OPT_KEEP, "description": "the cards keep coming; the count keeps running"},
                        {"label": OPT_AUTO, "description": "recorded here; a session makes the change and shows you the diff"}],
            "advice": advice.line({"kind": "offer", "lane": "personal", "title": title,
                                   "why": f"{threshold} unchanged approvals is the bar you set; an edit or Skip at any point restarts the count"})}


def harvest(st: dict, answers: list | None = None) -> int:
    answers = answers if answers is not None else C.card_answers(60)
    by_qid = {str(a.get("qid")): a for a in answers}
    n = 0
    for name, row in st["classes"].items():
        qid = str(row.get("asked_qid") or "")
        if not qid or row.get("answer") or qid not in by_qid:
            continue
        a = by_qid[qid]
        row["answer"] = {"option": str(a.get("option") or ""), "ts": str(a.get("ts") or ""), "text": str(a.get("text") or "")[:200]}
        row["owner_said_yes"] = str(a.get("option") or "").lower().startswith("yes")
        n += 1
        C.log("probation_answer", cls=name, option=row["answer"]["option"], qid=qid)
    return n


def tick(now=None, dry: bool = False) -> dict:
    c = C.cfg()
    pc = c["probation"]
    if not pc.get("enabled", True):
        return {"skipped": "disabled"}
    now = now or C.now_local()
    st = _state()
    harvested = harvest(st)
    t = tally(now, c=c)
    threshold = int(pc.get("threshold") or 20)
    posted = []
    for name, row in t.items():
        s = st["classes"].setdefault(name, {})
        s.update({"streak": row["streak"], "total": row["total"], "last_qid": row["last_qid"], "last_ts": row["last_ts"], "checked": now.strftime("%Y-%m-%d %H:%M")})
        if row["streak"] < threshold:
            if int(s.get("asked_streak") or 0) and row["streak"] < int(s.get("asked_streak") or 0):
                s["asked_streak"] = 0            # the run broke: the next time it reaches the bar, ask again
            continue
        if int(s.get("asked_streak") or 0) and row["streak"] >= int(s["asked_streak"]):
            continue                              # asked already for this run
        q = ask_card(name, row, threshold)
        if dry:
            print("WOULD POST " + json.dumps(q, ensure_ascii=False)[:600])
            qid = "dry"
        else:
            import cards
            r = cards.post([q], owner="tracking.probation", lane="personal")
            qid = str(r.get("qid") or "")
            if not r.get("posted"):
                C.log("probation_post_refused", cls=name, reason=str(r.get("reason"))[:200])
                continue
        s.update({"asked_qid": qid, "asked_streak": row["streak"], "asked_ts": now.strftime("%Y-%m-%d %H:%M"), "answer": None, "owner_said_yes": False})
        posted.append({"cls": name, "qid": qid, "streak": row["streak"]})
        C.log("probation_asked", cls=name, qid=qid, streak=row["streak"])
    if not dry:
        C.save_state("probation", st)
    out = {"harvested": harvested, "posted": posted, "tally": {k: v["streak"] for k, v in t.items()}}
    print("PROBATION " + json.dumps(out, ensure_ascii=False))
    return out


def report(as_json: bool = False) -> dict:
    c = C.cfg()
    t = tally(c=c)
    th = int(c["probation"].get("threshold") or 20)
    st = _state()
    if as_json:
        print(json.dumps({"threshold": th, "tally": t, "state": st["classes"]}, ensure_ascii=False, indent=1))
        return t
    print(f"Probation tally (bar: {th} as-is approvals in a row; the code never flips a gate)")
    for name, row in t.items():
        s = st["classes"].get(name) or {}
        asked = f"  asked on {s.get('asked_qid')} ({s.get('asked_ts')})" if s.get("asked_qid") else ""
        ans = f"  answer: {s['answer']['option']}" if s.get("answer") else ""
        print(f"  {name:22} {row['streak']:3}/{th} in a row   {row['as_is']} as-is of {row['total']} ({row['resets']} resets)"
              f"   last {row['last_qid'] or '-'} {row['last_ts'][:16]}{asked}{ans}")
        for h in row.get("history") or []:
            print(f"      {'ok  ' if h['ok'] else 'RESET'} {h['qid']} {h['ts'][:16]} {h['option']}")
    return t


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("cmd", nargs="?", default="report", choices=["report", "tick"])
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args()
    if a.cmd == "tick":
        tick(dry=a.dry_run)
    else:
        report(a.json)

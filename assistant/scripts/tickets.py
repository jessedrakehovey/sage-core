"""tickets.py - the assistant's single work queue (id-keyed, lane-tagged).

Sensors add tickets; the operator claims, then stages / dismisses / completes them.
Tickets are ALWAYS addressed by `id` (content hash), never by position - the queue
can change between a read and a write (Schema/mistakes.md 2026-08-25).

Status flow:  new -> claimed -> staged (waiting on the owner) -> done
                             +-> dismissed (noise)          +-> dismissed
A claim older than CLAIM_TTL_S is treated as abandoned and becomes claimable again.

CLI (what the headless operator is allowed to run):
  tickets.py list [--status new] [--lane wedding] [--json]
  tickets.py pending                      -> prints count of claimable tickets
  tickets.py claim [--lane L] [--limit N] -> claims + prints tickets as JSON
  tickets.py show <id>
  tickets.py stage <id> --note "..."      -> needs the owner (draft ready / decision)
  tickets.py dismiss <id> --note "..."
  tickets.py done <id> [--note "..."]
  tickets.py relane <id> <lane>
  tickets.py stats
"""
import argparse
import hashlib
import json
import os
import sys
import time
from datetime import datetime, timezone

from alib import DATA, QueueLock, config, load_json, now_iso, save_json

TICKETS = DATA / "tickets.json"
CLAIM_TTL_S = 3600
OPEN = {"new", "claimed", "staged"}
DOER_KINDS = {"agent_run"}      # worked by assistant/doer, never by the operator


def make_id(source, source_id):
    return hashlib.sha1(f"{source}|{source_id}".encode("utf-8")).hexdigest()[:16]


def _load():
    return load_json(TICKETS, {})


def _age_s(iso):
    try:
        t = datetime.strptime(iso, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
        return time.time() - t.timestamp()
    except (TypeError, ValueError):
        return 1e9


def _claimable(t):
    return t["status"] == "new" or (t["status"] == "claimed" and _age_s(t["updated"]) > CLAIM_TTL_S)


def add_many(items, owner):
    """items: dicts with source, source_id, lane, kind, title, summary, payload.
    Dedups on id - re-sensing the same thing is a no-op. Returns # newly added."""
    lanes = set(config()["lanes"])
    try:
        import text_gate        # the owner's iPhone: only wedding texts become tickets (2026-10-08)
        items = text_gate.keep(list(items))
    except Exception:           # the gate failing must never lose a text: ticket everything
        pass
    new_rows = []
    with QueueLock(owner) as ok:
        if not ok:
            return -1
        db, added = _load(), 0
        for it in items:
            tid = make_id(it["source"], it["source_id"])
            if tid in db:
                continue
            ts = now_iso()
            db[tid] = {
                "id": tid, "source": it["source"], "source_id": str(it["source_id"]),
                "lane": it["lane"] if it.get("lane") in lanes else "personal",
                "kind": it.get("kind", "item"), "title": it.get("title", "")[:200],
                "summary": it.get("summary", "")[:600], "payload": it.get("payload", {}),
                "status": "new", "created": ts, "updated": ts,
                "history": [{"ts": ts, "by": owner, "to": "new"}],
            }
            new_rows.append(db[tid])
            added += 1
        if added:
            save_json(TICKETS, db)
    # every input leads to its next action (triage_next.py, 2026-10-18): a new inbound ticket is sorted the
    # moment it lands (facts -> auto steps done, cards posted, the rest named for the operator). Outside the
    # lock: the steps take their own locks. A triage fault never loses the ticket.
    if new_rows and not os.environ.get("WV_NO_TRIAGE"):
        try:
            import triage_next
            triage_next.on_new_tickets(new_rows)
        except Exception:  # noqa: BLE001
            pass
    return added


def annotate(tid, key, value, owner="triage"):
    """Attach one payload field to a ticket (payload[key] = value) under the queue lock; -1 when busy."""
    with QueueLock(owner) as ok:
        if not ok:
            return -1
        db = _load()
        t = db.get(tid)
        if not t:
            return 0
        t.setdefault("payload", {})[key] = value
        t["updated"] = now_iso()
        save_json(TICKETS, db)
        return 1


def _spent_text_exception(t, note):
    """A personal text drafted only because an owner asked in chat (texts outside wedding work
    get no cards, text_gate.py) is a ONE-time exception: once its card expires unanswered the
    exception is used, so cards.sweep's "re-post it" hand-back closes the ticket instead
    (2026-10-13: Finley's reply card q18cdc65b4246 expired and came back as qbb4dea647851).
    Returns the card id, or "" when this ticket is not one."""
    import re
    m = re.match(r"card (q[0-9a-f]{12}) expired unanswered", note or "")
    if not m or t.get("kind") != "owner1_request":
        return ""
    try:
        import cards
        import text_gate
        c = load_json(cards.DONE / f"{m.group(1)}.json", {}) or {}
        ex = ((c.get("questions") or [{}])[0] or {}).get("exec") or {}
        if ex.get("kind") != "imessage_send" or not text_gate.business_only():
            return ""
        return "" if text_gate.is_business((ex.get("payload") or {}).get("to")) else m.group(1)
    except Exception:  # noqa: BLE001  unsure -> the normal hand-back
        return ""


def _transition(tid, to, by, note="", allowed_from=None, lane=None, note_max=500):
    with QueueLock(by) as ok:
        if not ok:
            return None
        db = _load()
        t = db.get(tid)
        if not t:
            print(f"NOT_FOUND: {tid}")
            return None
        if allowed_from and t["status"] not in allowed_from:
            print(f"BAD_STATE: {tid} is {t['status']}, cannot -> {to}")
            return None
        if by == "cards.sweep" and to == "new" and not lane:
            qid = _spent_text_exception(t, note)
            if qid:
                to, note = "done", f"card {qid} expired unanswered: one-time personal text, not re-posted; their texts are logged only again"
        ts = now_iso()
        if lane:
            t["lane"] = lane
        else:
            t["status"] = to
        t["updated"] = ts
        t["history"].append({"ts": ts, "by": by, "to": lane and f"lane:{lane}" or to, "note": note[:note_max]})
        save_json(TICKETS, db)
        return t


def claim(lane=None, limit=10, by="operator", kind=None, where=None):
    """kind=None claims every kind except DOER_KINDS (the operator); kind="agent_run" claims only those (the doer).
    where: optional predicate; a ticket it rejects is left waiting (e.g. a client send during quiet hours)."""
    with QueueLock(by) as ok:
        if not ok:
            return []
        db, ts, got = _load(), now_iso(), []
        for t in sorted(db.values(), key=lambda x: x["created"]):
            if len(got) >= limit:
                break
            if kind is None and t.get("kind") in DOER_KINDS:
                continue
            if kind is not None and t.get("kind") != kind:
                continue
            if where is not None and not where(t):
                continue
            if _claimable(t) and (lane is None or t["lane"] == lane):
                t["status"], t["updated"] = "claimed", ts
                t["history"].append({"ts": ts, "by": by, "to": "claimed"})
                got.append(t)
        if got:
            save_json(TICKETS, db)
        return got


def pending(lane=None, kind=None):
    """The operator's count leaves out the doer's kinds, so a waiting browser task never wakes an LLM operator that cannot work it."""
    return sum(1 for t in _load().values() if _claimable(t) and (lane is None or t["lane"] == lane)
               and ((t.get("kind") not in DOER_KINDS) if kind is None else t.get("kind") == kind))


def prune(days=30):
    """Drop closed tickets older than `days` (their ids stay in the sensors' seen-sets)."""
    with QueueLock("prune") as ok:
        if not ok:
            return 0
        db = _load()
        keep = {k: t for k, t in db.items() if t["status"] in OPEN or _age_s(t["updated"]) < days * 86400}
        if len(keep) != len(db):
            save_json(TICKETS, keep)
        return len(db) - len(keep)


def main():
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("list"); p.add_argument("--status"); p.add_argument("--lane"); p.add_argument("--json", action="store_true")
    p = sub.add_parser("pending"); p.add_argument("--lane")
    p = sub.add_parser("claim"); p.add_argument("--lane"); p.add_argument("--limit", type=int, default=10)
    p = sub.add_parser("show"); p.add_argument("id")
    for name in ("stage", "dismiss", "done"):
        p = sub.add_parser(name); p.add_argument("id"); p.add_argument("--note", default="", required=(name != "done"))
    p = sub.add_parser("relane"); p.add_argument("id"); p.add_argument("lane")
    sub.add_parser("stats")
    p = sub.add_parser("prune"); p.add_argument("--days", type=int, default=30)
    a = ap.parse_args()

    if a.cmd == "list":
        rows = [t for t in _load().values()
                if (not a.status or t["status"] == a.status) and (not a.lane or t["lane"] == a.lane)]
        rows.sort(key=lambda x: x["created"])
        if a.json:
            print(json.dumps(rows, indent=1, ensure_ascii=False))
        else:
            for t in rows:
                print(f"{t['id']}  {t['status']:<9} {t['lane']:<9} {t['kind']:<14} {t['title'][:70]}")
            print(f"({len(rows)} tickets)")
    elif a.cmd == "pending":
        print(pending(a.lane))
    elif a.cmd == "claim":
        print(json.dumps(claim(a.lane, a.limit), indent=1, ensure_ascii=False))
    elif a.cmd == "show":
        print(json.dumps(_load().get(a.id) or {"error": "NOT_FOUND"}, indent=1, ensure_ascii=False))
    elif a.cmd in ("stage", "dismiss", "done"):
        to = {"stage": "staged", "dismiss": "dismissed", "done": "done"}[a.cmd]
        frm = {"stage": {"claimed", "new"}, "dismiss": OPEN, "done": OPEN}[a.cmd]
        t = _transition(a.id, to, "operator", a.note, frm)
        print(f"OK {a.id} -> {to}" if t else "FAILED")
        sys.exit(0 if t else 1)
    elif a.cmd == "relane":
        if a.lane not in config()["lanes"]:
            sys.exit(f"unknown lane {a.lane}; valid: {config()['lanes']}")
        t = _transition(a.id, "", "operator", "", None, lane=a.lane)
        print(f"OK {a.id} lane -> {a.lane}" if t else "FAILED")
    elif a.cmd == "stats":
        c = {}
        for t in _load().values():
            c[(t["lane"], t["status"])] = c.get((t["lane"], t["status"]), 0) + 1
        for (lane, st), n in sorted(c.items()):
            print(f"{lane:<10} {st:<10} {n}")
        print(f"claimable now: {pending()}")
    elif a.cmd == "prune":
        print(f"pruned {prune(a.days)}")


if __name__ == "__main__":
    main()

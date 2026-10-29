"""send_hold.py - a timed send that holds itself back if the person wrote first (2026-10-28). Zero-LLM.

"Text him at 4 PM if they haven't replied": the owner still taps the card, the tap still mints the ticket with its
not_before, and nothing new goes out without that tap. What this adds is the condition. Sage's `task` draft (or
`approve` deed) with `hold_if_reply` stores it here, keyed by the card; this job runs in the spine's long group
right BEFORE the drain and, for every conditional ticket due within the next pass, looks for anything from that
person since the card was made:

  texts        an inbound line in the phone threads, or an imessage_in ticket, from their number
  email        a mail ticket from their address (every inbox the mail sensor reads)
  the CRM      the organization's plugins (alib.plugins): `reply_watch(w)` adds the other addresses / ids it
               knows for them, `reply_baseline(w)` records its state when the card is made, and
               `reply_evidence(rec, w, tickets)` names what came in there since (a message, a form, a booking)

Something there -> the ticket is cancelled (never sent) and ONE line goes in the card's own thread saying what
came in. Nothing -> nothing is touched; the drain sends it as approved. Never sends, never mints.

  python assistant/scripts/send_hold.py [--dry-run]     # prints SEND_HOLD checked=N held=M
"""
from __future__ import annotations

import argparse
import re
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import alib  # noqa: E402
from alib import DATA, load_json, save_json  # noqa: E402

FILE = DATA / "send_conditions.json"
AHEAD_S = 12 * 60          # one spine pass (10 min) + slack: checked again on the pass before the send minute
KEEP_DAYS = 14
NAME_STOP = {"and", "the", "text", "email", "message", "to", "at", "for", "reply", "with"}


def digits(s) -> str:
    return re.sub(r"\D", "", str(s or ""))[-10:]


def ts(iso) -> float:
    import comms_state
    return comms_state._ts(iso)


def names(*parts) -> set:
    return {w for p in parts for w in re.findall(r"[a-z]{3,}", str(p or "").lower()) if w not in NAME_STOP}


def clock(t: float) -> str:
    return time.strftime("%I:%M %p", time.localtime(t)).lstrip("0")


def watch(rec: dict) -> dict:
    """Everything that counts as 'them': the card's own recipient, plus what the plugins know for that person
    (a partner's address, the client's ids in the organization's CRM)."""
    to = str(rec.get("to") or "").strip()
    w = {"emails": {to.lower()} if "@" in to else set(),
         "phones": {digits(to)} if "@" not in to and len(digits(to)) == 10 else set(),
         "ids": {str(rec.get("workspace_id") or "")} - {""}, "names": names(rec.get("name"))}
    alib.plugin_each("reply_watch", w)
    return w


def remember(qid: str, rec: dict, since: float = 0) -> dict:
    """Store the condition for card `qid` (actions.apply, when the brain's draft or approve carries hold_if_reply).
    rec: {to, name, channel, workspace_id?}. The clock starts when the card was made (`since`, else now): only what
    comes in after it counts."""
    rec = {k: str(v)[:200] for k, v in rec.items() if v}
    rec["since"] = float(since or time.time())
    rec["since_text"] = time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(rec["since"]))
    base = {}
    for b in alib.plugin_each("reply_baseline", watch(rec)):
        if isinstance(b, dict):
            base.update(b)
    rec["baseline"] = base
    d = load_json(FILE, {}) or {}
    d[str(qid)] = rec
    save_json(FILE, d)
    return rec


def evidence(rec: dict) -> str:
    """One plain clause naming what came in from them since the card was made; '' when nothing did."""
    w, since = watch(rec), float(rec.get("since") or 0)
    who = str(rec.get("name") or rec.get("to") or "they").strip()
    for handle, th in (load_json(DATA / "phone_threads.json", {}) or {}).items():
        if digits(handle) not in w["phones"]:
            continue
        for m in reversed(th.get("messages") or []):
            if m.get("dir") == "in" and ts(m.get("ts")) >= since:
                return f'{who} texted at {clock(ts(m.get("ts")))} ("{str(m.get("text") or "")[:80]}")'
    fresh = [t for t in (load_json(DATA / "tickets.json", {}) or {}).values()
             if isinstance(t, dict) and ts(t.get("created")) >= since]
    for t in fresh:
        p, k, at = t.get("payload") or {}, t.get("kind"), clock(ts(t.get("created")))
        if k == "imessage_in" and digits(p.get("from")) in w["phones"]:
            return f'{who} texted at {at} ("{str(p.get("text") or "")[:80]}")'
        if k == "email" and not p.get("bulk"):
            frm = str(p.get("from") or "").lower()
            if (re.findall(r"[\w.+-]+@[\w-]+\.[\w.-]+", frm) or [frm])[0] in w["emails"]:
                return f'{who} emailed at {at} ("{str(p.get("subject") or "")[:80]}")'
    return str(alib.plugin_call("reply_evidence", dict(rec, who=who), w, fresh) or "")


def _post(qid: str, line: str) -> None:
    try:
        import cards
        import chat_push
        card = cards._read(cards.DONE / f"{qid}.json") or cards._read(cards.LIVE / f"{qid}.json") \
            or cards._read(cards.ARCHIVE / f"{qid}.json")
        cards.note("send_hold", line + f" (card {qid})")
        if card:
            chat_push.push_result(card, line)
    except Exception:  # noqa: BLE001
        pass


def tick(dry: bool = False, now: float | None = None) -> dict:
    """Check every conditional ticket due within AHEAD_S; hold the ones whose person already wrote."""
    import exec_store as es
    from datetime import datetime
    now = now or time.time()
    conds = load_json(FILE, {}) or {}
    out = {"checked": 0, "held": 0}
    if not conds:
        return out
    soon = datetime.fromtimestamp(now + AHEAD_S)
    tickets = es.fold()
    for qid, rec in list(conds.items()):
        if rec.get("resolved"):
            continue
        mine = [t for t in tickets.values() if str(t.get("approval_ref") or "") == f"card:{qid}"]
        q = [t for t in mine if t.get("status") == "queued" and t.get("not_before")]
        if not q:
            if mine and all(t.get("status") in ("done", "cancelled", "exception") for t in mine):
                rec["resolved"] = "ticket " + str(mine[-1].get("status"))
            continue
        t = q[-1]
        if not es.due(t, soon):
            continue
        out["checked"] += 1
        ev = evidence(rec)
        if not ev:
            continue
        out["held"] += 1
        if dry:
            continue
        try:
            es.set_status(t["id"], "cancelled", f"held: {ev}"[:500])
        except ValueError:
            continue                       # it went (or is going) out already; nothing to hold
        es.ledger("send_held", ticket=t["id"], card=qid, why=ev[:200])
        rec["resolved"] = "held"
        rec["held_why"] = ev
        _post(qid, f"Not done: I held this back because {ev} since you asked. Nothing went out; "
                   "tell me if you still want it sent.")
    cut = now - KEEP_DAYS * 86400
    conds = {k: v for k, v in conds.items() if float(v.get("since") or 0) >= cut}
    if not dry:
        save_json(FILE, conds)
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args()
    r = tick(a.dry_run)
    print(f"SEND_HOLD checked={r['checked']} held={r['held']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

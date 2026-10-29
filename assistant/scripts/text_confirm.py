"""text_confirm.py - a reply proves the text went out (2026-10-10).

An approved text is `handed` to an iPhone and stays unconfirmed until the phone's Shortcut
acks it; when the ack never comes, sentinels.py chases it and the operator cards the owner to
"check Messages". But a text FROM that number after the hand-over already answers the
question (example replied to the intro text on Oct 8 and the "text unconfirmed" card still
sat open two days later, warning not to re-send). So, zero LLM:

  1. every handed outbox item whose recipient has texted in since it was handed is marked
     sent (`confirmed_by: reply`) and logged in the thread at its hand-over time, BEFORE
     the reply, so the comms board still sees the reply as the newest line (needs_reply);
  2. every open card about such a text (no exec; names the outbox id, or the number's last
     four digits plus "unconfirmed" / "never confirmed" and was posted after the hand-over) is retired as resolved, with the reason, like resolve_alarms.

A reply only proves a handed text went out, never a `ready` one (the phone has not fetched
it yet). Run from Mission Control right after an inbound text, and by the Day Spine
(`text-confirm`) as the safety net. Never sends anything. ALWAYS exits 0.

  python assistant/scripts/text_confirm.py [--dry-run]
"""
from __future__ import annotations

import argparse
import re
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from alib import QueueLock, load_json, save_json  # noqa: E402
import phone_bridge as pb  # noqa: E402


# how the sentinel and the operator word it: "never confirmed sent", "Text not confirmed", "unconfirmed"
UNCONFIRMED = re.compile(r"unconfirmed|n(?:ot|ever) confirmed", re.I)


def _epoch(ts) -> float:
    try:
        return time.mktime(time.strptime(str(ts)[:19], "%Y-%m-%dT%H:%M:%S"))
    except ValueError:
        return 0.0


def _replied_since(msgs, ts) -> dict | None:
    return next((m for m in msgs if m.get("dir") == "in" and str(m.get("ts", "")) > str(ts)), None)


def confirm(dry=False) -> list:
    """Mark handed texts that have a later reply as sent. -> the items confirmed."""
    done = []
    with QueueLock("text_confirm") as ok:
        if not ok:
            return done
        box = load_json(pb.OUTBOX, {"items": []})
        threads = load_json(pb.THREADS, {})
        now = time.strftime("%Y-%m-%dT%H:%M:%S")
        for i in box["items"]:
            if i.get("status") != "handed" or not i.get("ts_handed"):
                continue
            th = threads.get(i.get("to")) or {}
            msgs = th.get("messages") or []
            reply = _replied_since(msgs, i["ts_handed"])
            if not reply:
                continue
            done.append(i)
            if dry:
                continue
            i["status"], i["ts_sent"], i["confirmed_by"] = "sent", now, "reply"
            # the text went out when it was handed: log it there, ahead of the reply
            at = next((n for n, m in enumerate(msgs) if str(m.get("ts", "")) > i["ts_handed"]), len(msgs))
            msgs.insert(at, {"ts": i["ts_handed"], "dir": "out", "text": str(i.get("body", ""))[:pb.TEXT_MAX],
                             "ref": i.get("ticket", "")})
            th["messages"] = msgs[-pb.THREAD_KEEP:]
        if done and not dry:
            save_json(pb.OUTBOX, box)
            save_json(pb.THREADS, threads)
    return done


def _card_text(c) -> str:
    q = (c.get("questions") or [{}])[0]
    return " ".join(str(q.get(k) or "") for k in ("header", "question", "details"))


def close_cards(dry=False, just=()) -> list:
    """Retire open 'text unconfirmed' cards whose text a reply has since confirmed. -> qids.
    `just`: items confirm() found on a dry run (not yet written as sent)."""
    import cards
    import exec_store as es
    ids = {i["id"] for i in just}
    items = load_json(pb.OUTBOX, {"items": []})["items"]
    confirmed = [i for i in items if i.get("confirmed_by") == "reply" or i["id"] in ids]
    # a number with a text STILL unconfirmed keeps its cards unless one names the confirmed id
    open_to = {i.get("to") for i in items if i.get("status") == "handed" and i["id"] not in ids}
    out = []
    for c in cards._live():
        q = (c.get("questions") or [{}])[0]
        if c.get("status") != "open" or q.get("exec"):
            continue
        text = _card_text(c)
        for i in confirmed:
            by_id = i["id"] in text
            by_num = (i["to"] not in open_to and i["to"][-4:] in text and UNCONFIRMED.search(text)
                      and float(c.get("ts") or 0) >= _epoch(i.get("ts_handed")))
            if by_id or by_num:
                out.append(c["id"])
                if not dry:
                    c["resolved_why"] = f"they replied after the text to ...{i['to'][-4:]} was handed over, so it went out"
                    cards._retire(c, "resolved")
                    es.ledger("card_resolved", qid=c["id"], why=c["resolved_why"], outbox=i["id"])
                break
    return out


def run(dry=False) -> dict:
    n = confirm(dry)
    qids = close_cards(dry, n)
    return {"confirmed": len(n), "cards_closed": len(qids)}


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args()
    try:
        r = run(a.dry_run)
        print("TEXT_CONFIRM " + " ".join(f"{k}={v}" for k, v in r.items()))
    except Exception as e:  # noqa: BLE001
        print(f"TEXT_CONFIRM error {e}")
    sys.exit(0)

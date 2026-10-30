"""Offline tests for Undo on cards (the owner 2026-10-20: "undo function on approval cards is not working
but it is vital that it does"). Undo must work for as long as the answer has not taken effect, not
only in the first 30 s. Scratch store; never sends.   python assistant/tests/test_undo.py
"""
import os as _os, tempfile as _tf
_os.environ.setdefault("WV_LOG_DIR", _tf.mkdtemp(prefix="wv_logs_"))
import json
import os
import sys
import tempfile
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
TMP = Path(tempfile.mkdtemp(prefix="wv_undo_"))
os.environ["ASSISTANT_DATA"] = str(TMP)
os.environ["ASSISTANT_CARDS"] = str(TMP / "cards")
os.environ["ASSISTANT_LEARNINGS"] = str(TMP / "learnings")
os.environ["RULINGS_FILE"] = str(HERE.parent / "data" / "rulings.json")
os.environ["WV_NO_TRIAGE"] = "1"
sys.path.insert(0, str(HERE.parent / "scripts"))
import cards  # noqa: E402
import exec_store as es  # noqa: E402
import phone_bridge  # noqa: E402
import tickets  # noqa: E402

FAILS = []


def check(name, cond, extra=""):
    print(("ok   " if cond else "FAIL ") + name + (f"  {extra}" if extra and not cond else ""))
    if not cond:
        FAILS.append(name)


def age_answer(qid, seconds):
    """Pretend the last tap on qid happened `seconds` ago."""
    rows = [json.loads(l) for l in cards.ANSWERS.read_text(encoding="utf-8").splitlines() if l.strip()]
    for r in reversed(rows):
        if r.get("qid") == qid:
            r["epoch"] = float(r["epoch"]) - seconds
            break
    cards.ANSWERS.write_text("".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8")


# 1. the Odette case: a standing-text preview, 'Let it go' tapped, swept (consumed), Undo an hour later
PREVIEW = {"header": "Lead intro: Test Couple", "lane": "wedding", "subject": "lead_intro|5550001111",
           "hold_text": "lead_intro|5550001111", "no_recommendation": "the text goes by itself; this card only pulls it back",
           "question": "The intro text goes out at 9:00 AM by itself. Tap Hold this one and it waits for your tap.",
           "options": [{"label": "Hold this one"}, {"label": "Let it go as planned"}]}
r = cards.post([dict(PREVIEW)], "client_texts")
check("preview card posts", r.get("posted"), r)
qid = r["qid"]
check("answer", cards.answer(qid, "Let it go as planned")["ok"])
age_answer(qid, 3600)
cards.sweep()
check("swept: card consumed", (cards.DONE / f"{qid}.json").exists() and not (cards.LIVE / f"{qid}.json").exists())
u = cards.undo(qid)
check("Undo an hour later still works (was: 'undo window (30s) passed')", u["ok"], u)
live = cards._read(cards.LIVE / f"{qid}.json") or {}
check("card is open again in the live store", live.get("status") == "open" and not (cards.DONE / f"{qid}.json").exists())
check("its answer is void", cards.answer_of(qid) == "")
check("a second Undo says there is nothing to undo", not cards.undo(qid)["ok"])
check("it can be answered again", cards.answer(qid, "Hold this one")["ok"] and cards.answer_of(qid) == "Hold this one")
check("pending shows nothing stuck", all(p["qid"] != qid for p in cards.pending()))

# 2. a decision whose operator ticket nobody has started: Undo closes that ticket
DEC = {"header": "Prep for Sam", "lane": "wedding", "question": "Start shoot prep for Sam now?",
       "options": [{"label": "Start prep (Recommended)"}, {"label": "Wait a week, then nudge"}]}
r = cards.post([dict(DEC)], "test")
q2 = r["qid"]
cards.answer(q2, "Start prep (Recommended)")
age_answer(q2, 120)
sw = cards.sweep()
tid = tickets.make_id("card", q2)
check("sweep queued the operator ticket", tickets._load().get(tid, {}).get("status") == "new", sw)
u = cards.undo(q2)
check("Undo after the sweep works", u["ok"] and u["followup"] == "closed", u)
check("the untouched operator ticket is closed", tickets._load()[tid]["status"] == "done")
cards.answer(q2, "Wait a week, then nudge")
age_answer(q2, 120)
cards.sweep()
check("the new answer gets its OWN operator ticket (not deduped away)",
      tickets._load().get(tickets.make_id("card", f"{q2}#1"), {}).get("status") == "new")

# 2b. a decision the operator already started on: Undo tells the operator to reverse it
r = cards.post([dict(DEC, header="Prep for Lee", question="Start shoot prep for Lee now?")], "test")
q3 = r["qid"]
cards.answer(q3, "Start prep (Recommended)")
age_answer(q3, 120)
cards.sweep()
tickets._transition(tickets.make_id("card", q3), "claimed", "operator")
u = cards.undo(q3)
check("Undo on a claimed decision hands the reversal to the operator", u["ok"] and u["followup"] == "operator", u)
check("that reversal ticket exists", any(t.get("source") == "card_undo" and t["payload"].get("qid") == q3
                                         for t in tickets._load().values()))

# 2c. too old
r = cards.post([dict(DEC, header="Prep for Kim", question="Start shoot prep for Kim now?")], "test")
q4 = r["qid"]
cards.answer(q4, "Start prep (Recommended)")
age_answer(q4, cards.UNDO_DECISION_MAX_S + 60)
check("a decision past UNDO_DECISION_MAX_S is refused in plain words", "hours old" in cards.undo(q4)["reason"])

# 3. an approved email whose ticket is still queued ten minutes later: Undo cancels it
BODY = "Hi Sara, confirming we are set for Saturday. Call time 1:30pm at the venue. - owner1"
EXEC_Q = {"question": "[EMAIL PREVIEW]\nfrom owner3@example.invalid to sara@example.com\nsubject: Saturday details\n\n" + BODY,
          "header": "Sara prep", "lane": "wedding", "audience": "client",
          "options": [{"label": "Approve"}, {"label": "Skip"}],
          "exec": {"kind": "email_send", "approve_label": "Approve",
                   "payload": {"from": "owner3@example.invalid", "to": "sara@example.com",
                               "subject": "Saturday details", "body": BODY}}}
r = cards.post([json.loads(json.dumps(EXEC_Q))], "test")
q5 = r["qid"]
a = cards.answer(q5, "Approve")
age_answer(q5, 600)
u = cards.undo(q5)
check("Undo 10 min later cancels a still-queued send", u["ok"] and es.fold()[a["ticket"]]["status"] == "cancelled", u)
check("and the card is open again", (cards._read(cards.LIVE / f"{q5}.json") or {}).get("status") == "open")

# 3b. once it has gone out, Undo says so plainly
a = cards.answer(q5, "Approve")
check("re-approve mints", a["ok"] and a["ticket"], a)
if a.get("ticket"):
    es.set_status(a["ticket"], "done", "SENT")
    u = cards.undo(q5)
    check("Undo after the email went out is refused, saying it went out", not u["ok"] and "already went out" in u["reason"], u)
    check("the answer stands", cards.answer_of(q5) == "Approve")

# 4. a text handed to the phone outbox but not yet fetched by the phone is recalled
t = es.enqueue("imessage_send", {"to": "+15105550101", "body": "See you Saturday!", "account": "owner1-iphone"}, "owner1", "card:qtestrecall01")
ok, _ = phone_bridge.queue_outbound({"to": "+15105550101", "body": "See you Saturday!", "account": "owner1-iphone"}, t["id"])
es.set_status(t["id"], "done", "PHONE_QUEUED")
okb, why, recalled = cards._took_back_ticket({"ticket": t["id"]})
check("a text still waiting in the phone outbox is recalled", ok and okb and recalled and es.fold()[t["id"]]["status"] == "cancelled", why)
box = json.loads(phone_bridge.OUTBOX.read_text(encoding="utf-8"))
check("the outbox item is no longer ready", all(i["status"] != "ready" for i in box["items"] if i["ticket"] == t["id"]))
check("the phone never gets it", all(i["to"] != "+15105550101" for i in phone_bridge.fetch_outbox("owner1-iphone")) if not phone_bridge._quiet() else True)
t2 = es.enqueue("imessage_send", {"to": "+15105550102", "body": "Running late", "account": "owner1-iphone"}, "owner1", "card:qtestrecall02")
phone_bridge.queue_outbound({"to": "+15105550102", "body": "Running late", "account": "owner1-iphone"}, t2["id"])
es.set_status(t2["id"], "done", "PHONE_QUEUED")
box = json.loads(phone_bridge.OUTBOX.read_text(encoding="utf-8"))
for i in box["items"]:
    if i["ticket"] == t2["id"]:
        i["status"] = "handed"
phone_bridge.OUTBOX.write_text(json.dumps(box), encoding="utf-8")
okb, why, recalled = cards._took_back_ticket({"ticket": t2["id"]})
check("a text the phone already fetched is not claimed as recalled", not okb and not recalled and "already went out" in why, why)

print(f"\n{'ALL PASS' if not FAILS else 'FAILURES: ' + ', '.join(FAILS)} (scratch {TMP})")
sys.exit(1 if FAILS else 0)

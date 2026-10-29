"""Offline tests for the probation tally (tracking/probation.py, ruling 9): class detection, what counts
as an as-is approval, resets, the ask card at the bar, and that nothing ever flips. Scratch data.
  python assistant/tests/test_probation.py
"""
import os as _os, tempfile as _tf
_os.environ.setdefault("WV_LOG_DIR", _tf.mkdtemp(prefix="wv_logs_"))
import json
import os
import sys
import tempfile
from datetime import datetime, timedelta
from pathlib import Path

HERE = Path(__file__).resolve().parent
TMP = Path(tempfile.mkdtemp(prefix="wv_prob_"))
os.environ["ASSISTANT_DATA"] = str(TMP)
os.environ["ASSISTANT_CARDS"] = str(TMP / "cards")
os.environ["ASSISTANT_LEARNINGS"] = str(TMP / "learnings")
sys.path.insert(0, str(HERE.parent / "scripts"))
sys.path.insert(0, str(HERE.parent / "scripts" / "tracking"))
import common as C  # noqa: E402
import probation as P  # noqa: E402
import cards  # noqa: E402
import exec_store as es  # noqa: E402

FAILS = []
NOW = datetime(2026, 10, 15, 12, 0)
c = C.cfg()
classes = c["probation"]["classes"]


def check(name, cond, extra=""):
    print(("ok   " if cond else "FAIL ") + name + (f"  {extra}" if extra and not cond else ""))
    if not cond:
        FAILS.append(name)


def card(qid, kind, header, label="Approve (Recommended)"):
    return {"id": qid, "status": "open", "questions": [{"header": header, "lane": "wedding", "exec": {"kind": kind, "approve_label": label, "payload": {"x": 1}}}]}


check("two classes with a threshold of 20", set(classes) == {"record_write", "routine_client_send"} and c["probation"]["threshold"] == 20)

# ------------------------------------------------------------------ classes
check("a tracking_fix card is a record write", P.classify(card("q1", "tracking_fix", "Weekly record check"), None, classes) == "record_write")
check("a calendar hold is a record write", P.classify(card("q2", "calendar_hold", "Date from Mom"), None, classes) == "record_write")
fu = {"kind": "followup", "payload": {"sop": "wedding-lifecycle", "step": "thank-you"}}
check("the thank-you email (by its follow-up step) is a routine client send", P.classify(card("q3", "email_send", "example thank you"), fu, classes) == "routine_client_send")
check("a 'footage is safe' text (by header words) is a routine client send", P.classify(card("q4", "imessage_send", "Footage is safe: Sara"), None, classes) == "routine_client_send")
check("a reply to a lead is NOT routine (no step, no words)", P.classify(card("q5", "imessage_send", "Text reply: example"), None, classes) == "")
check("a card with no exec is in no class", P.classify({"questions": [{"header": "Hot lead - what next?"}]}, None, classes) == "")

# ------------------------------------------------------------------ as-is
A = lambda qid, option, ts, **kw: {"qid": qid, "option": option, "ts": ts, "by": "gchat:owner1", "text": "", "revised": False, "revised_text": "", **kw}
cd = card("q1", "tracking_fix", "Weekly record check", "Fix 2 records (Recommended)")
check("approve label untouched = as-is", P.as_is(A("q1", "Fix 2 records (Recommended)", "2026-10-10T10:00:00"), cd, []))
check("Skip is not", not P.as_is(A("q1", "Skip this week", "x"), cd, []))
check("an edited draft is not", not P.as_is(A("q1", "Fix 2 records (Recommended)", "x", revised=True, revised_text="other"), cd, []))
check("a typed note is not", not P.as_is(A("q1", "Fix 2 records (Recommended)", "x", text="not yet"), cd, []))
ledger = [{"id": "tk-1", "approval_ref": "card:q1", "status": "queued"}, {"id": "tk-1", "status": "cancelled"}]
check("an undone approval (ticket cancelled) is not", not P.as_is(A("q1", "Fix 2 records (Recommended)", "x"), cd, ledger))

# ------------------------------------------------------------------ the tally over a history
cards_db, answers = {}, {}
t0 = NOW - timedelta(days=30)
for i in range(22):
    qid = f"q{i:012d}"
    cards_db[qid] = card(qid, "calendar_hold", "Date from Mom", "Add to calendar (Recommended)")
    opt = "Skip" if i == 1 else "Add to calendar (Recommended)"
    answers[qid] = A(qid, opt, (t0 + timedelta(hours=i)).strftime("%Y-%m-%dT%H:%M:%S"), epoch=(t0 + timedelta(hours=i)).timestamp())
# a routine send with one edit in the middle, two as-is after
for i, (opt, rev) in enumerate([("Approve (Recommended)", False), ("Approve (Recommended)", True), ("Approve (Recommended)", False), ("Approve (Recommended)", False)]):
    qid = f"s{i:012d}"
    cards_db[qid] = card(qid, "email_send", "example thank you")
    answers[qid] = A(qid, opt, (t0 + timedelta(days=2, hours=i)).strftime("%Y-%m-%dT%H:%M:%S"), epoch=(t0 + timedelta(days=2, hours=i)).timestamp(), revised=rev)
# a lead reply: never counted
cards_db["lead0000000000"] = card("lead0000000000", "imessage_send", "Text reply: example", "Send (Recommended)")
answers["lead0000000000"] = A("lead0000000000", "Send (Recommended)", "2026-10-14T10:00:00", epoch=(NOW - timedelta(days=1)).timestamp())
# a tap by someone who is not an owner
cards_db["x0000000000000"] = card("x0000000000000", "calendar_hold", "Date", "Add to calendar (Recommended)")
answers["x0000000000000"] = dict(A("x0000000000000", "Add to calendar (Recommended)", "2026-10-14T11:00:00", epoch=(NOW - timedelta(hours=20)).timestamp()), by="agent")
tix = {"t1": {"id": "t1", "kind": "followup", "payload": {"sop": "wedding-lifecycle", "step": "thank-you"},
              "history": [{"note": f"card {q}: thank you" } for q in cards_db if q.startswith("s")]}}
t = P.tally(NOW, answers=list(answers.values()), cards=cards_db, tickets=tix, ledger=[], c=c)
rw, rc = t["record_write"], t["routine_client_send"]
check("record writes: 22 taps, one Skip early -> a run of 20 since", rw["total"] == 22 and rw["resets"] == 1 and rw["streak"] == 20, rw)
check("routine sends: the edit reset the run; two as-is since", rc["total"] == 4 and rc["streak"] == 2 and rc["resets"] == 1, rc)
check("the lead reply and the non-owner tap are not counted", rw["total"] + rc["total"] == 26)
check("history keeps the last five with ok / reset", len(rw["history"]) == 5 and all("qid" in h for h in rw["history"]))

# ------------------------------------------------------------------ the ask at the bar (never a flip)
q = P.ask_card("record_write", rw, 20)
check("ask card: one question, Keep asking first and recommended, a yes is recorded only",
      q["options"][0]["label"] == P.OPT_KEEP and "(Recommended)" in q["options"][0]["label"] and q["options"][1]["label"] == P.OPT_AUTO
      and "Nothing changes by itself" in q["question"] and "exec" not in q and q["advice"].startswith("💡"))
cards.rulings.find = lambda *a, **k: []
r = cards.post([dict(q, probation_class="fixture-copy")], "test", lane="personal")   # its own subject: the tick below asks for the real class
check("the ask card passes cards.post", r["posted"], r)
# the gate is untouched whatever the answer: exec_store still refuses anything but the owner's tap on a card
try:
    es.enqueue("tracking_fix", {"fixes": [{"op": "x", "desc": "d"}]}, approved_by="rule:record_write", approval_ref="probation")
    flipped = True
except PermissionError:
    flipped = False
check("exec_store still refuses a non-tap provenance (no flip exists in code)", not flipped)

# tick: ask once per run; a reset re-arms the ask
P.tally = lambda now=None, c=None, **kw: {"record_write": dict(rw), "routine_client_send": dict(rc)}
out = P.tick(now=NOW, dry=False)
st = C.state("probation", {})
check("tick posts ONE ask for the class at the bar and records the streak it asked at",
      len(out["posted"]) == 1 and out["posted"][0]["cls"] == "record_write" and st["classes"]["record_write"]["asked_streak"] == 20, out)
out2 = P.tick(now=NOW + timedelta(hours=1), dry=False)
check("the next tick does not ask again for the same run", out2["posted"] == [], out2)
cards.answer(st["classes"]["record_write"]["asked_qid"], P.OPT_AUTO, by="owner1")
out3 = P.tick(now=NOW + timedelta(hours=2), dry=False)
st = C.state("probation", {})
check("owner1's answer is harvested and recorded; still no flip", out3["harvested"] == 1 and st["classes"]["record_write"]["owner_said_yes"] is True
      and st["classes"]["record_write"]["answer"]["option"] == P.OPT_AUTO)
P.tally = lambda now=None, c=None, **kw: {"record_write": dict(rw, streak=3), "routine_client_send": dict(rc)}
P.tick(now=NOW + timedelta(hours=3), dry=False)
check("a reset below the bar re-arms the ask for the next run", C.state("probation", {})["classes"]["record_write"]["asked_streak"] == 0)
P.tally = lambda now=None, c=None, **kw: {"record_write": dict(rw, streak=21), "routine_client_send": dict(rc)}
out4 = P.tick(now=NOW + timedelta(hours=4), dry=True)
check("...and asks again when the next run reaches it", len(out4["posted"]) == 1)

print(f"\n{'ALL OK' if not FAILS else 'FAILED: ' + ', '.join(FAILS)}  ({len(FAILS)} failures)")
sys.exit(1 if FAILS else 0)

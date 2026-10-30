"""Offline tests for the outcome ledger (assistant/scripts/outcomes.py, WP1 of the stand-alone proposal,
2026-10-24): "done" means read back. Scratch ASSISTANT_DATA / ASSISTANT_CARDS; chat push off; nothing sends.
   python assistant/tests/test_outcomes.py
"""
import os as _os, tempfile as _tf
_os.environ.setdefault("WV_LOG_DIR", _tf.mkdtemp(prefix="wv_logs_"))
import json
import os
import sys
import tempfile
import time
from datetime import datetime, timedelta
from pathlib import Path

HERE = Path(__file__).resolve().parent
SCRIPTS = HERE.parent / "scripts"
TMP = Path(tempfile.mkdtemp(prefix="wv_outcomes_"))
os.environ["ASSISTANT_DATA"] = str(TMP)
os.environ["ASSISTANT_CARDS"] = str(TMP / "cards")
os.environ["ASSISTANT_LEARNINGS"] = str(TMP / "learnings")
os.environ["RULINGS_FILE"] = str(HERE.parent / "data" / "rulings.json")
os.environ["CHAT_PUSH"] = "off"
sys.path.insert(0, str(SCRIPTS))
import cards  # noqa: E402
import chat_push  # noqa: E402
import drain  # noqa: E402
import exec_store as es  # noqa: E402
import outcomes as oc  # noqa: E402
import phone_bridge as pb  # noqa: E402
import tickets  # noqa: E402
from alib import save_json  # noqa: E402

FAILS = []


def check(name, cond, extra=""):
    print(("ok   " if cond else "FAIL ") + name + (f"  {extra}" if extra and not cond else ""))
    if not cond:
        FAILS.append(name)


def ago(minutes):
    return (datetime.now() - timedelta(minutes=minutes)).strftime(oc.FMT)


# ------------------------------------------------------------------ 1. what an executor line proves, by kind
v = oc.verdict_from_exec
# a fake CRM connector (the engine ships none): its kinds verify by the record id its transport prints
import re as _re  # noqa: E402
import types as _types  # noqa: E402
import alib as _alib  # noqa: E402
import crm as _crm  # noqa: E402
_FAKE = _types.SimpleNamespace(CRM=True, SERVICE="AcmeCRM", PARTICIPANT_KINDS={"acme_add_participant"}, STAGE_KINDS={"acme_set_stage"},
                               MESSAGE_KINDS={"acme_message"}, DONE_PATTERN=_re.compile(r"ACME_DONE=(\w+)"),
                               owns_kind=lambda k: str(k).startswith("acme_"), RETRY_KINDS={"acme_add_participant", "acme_message"})
_alib.plugins = lambda: [_FAKE]
_crm._plugins = lambda: [_FAKE]
_crm._ALIASES = None
check("email: SENT=1 + thread ok -> verified with the Message-ID",
      v("email_send", True, "SENT=1 <abc@example.invalid> THREAD=ok")[0] == "verified" and v("email_send", True, "SENT=1 <abc@x> THREAD=ok")[1]["message_id"] == "<abc@x>")
check("email: ok without a Message-ID is unverified", v("email_send", True, "ok")[0] == "unverified")
check("participant: MEMBER=no is a FAILURE, not a done with a warning", v("acme_add_participant", True, "ACME_DONE=fa8da98a5bdb77b4902cbb43 MEMBER=no")[0] == "failed")
check("participant: MEMBER=yes verified", v("acme_add_participant", True, "ACME_DONE=fa8da98a5bdb77b4902cbb43 CONTACT=x MEMBER=yes")[0] == "verified")
check("stage: VERIFIED=no failed / ARCHIVED=yes verified",
      v("acme_set_stage", True, "ACME_DONE=fa8da98a5bdb77b4902cbb43 STAGE=Booked VERIFIED=no")[0] == "failed"
      and v("acme_set_stage", True, "ACME_DONE=fa8da98a5bdb77b4902cbb43 ARCHIVED=yes REASON=x")[0] == "verified")
check("sheet: VERIFIED=no failed, VERIFIED=yes verified with the count",
      v("sheet_set", True, "SHEET_DONE=5 VERIFIED=no")[0] == "failed" and v("sheet_set", True, "SHEET_DONE=5 VERIFIED=yes") == ("verified", {"cells": 5}, "5 cells read back as written"))
check("text: handed to the phone is only CLAIMED, with the outbox id", v("imessage_send", True, "PHONE_QUEUED m-abc123 -> ...0123")[:2] == ("claimed", {"outbox": "m-abc123"}))
check("crm message: the id verifies it", v("acme_message", True, "ACME_DONE=fa8da98a5bdb77b4902cbb43 AT=t STATUS=sent")[0] == "verified")
check("a not-ok UNVERIFIED result is unverified, any other not-ok is failed",
      v("acme_note", False, "PERMANENT: DELIVERY UNVERIFIED: timed out")[0] == "unverified" and v("acme_note", False, "PERMANENT: refused")[0] == "failed")
check("doer run stays claimed for its own evidence", v("agent_run", True, "DOER_DONE: x")[0] == "claimed")

# ------------------------------------------------------------------ 2. rows: idempotent claim, terminal never rewritten
r1 = oc.claim("task", "tk-1", "draft to Sara", lane="wedding", by="owner1", convo="dm:owner1")
r1b = oc.claim("task", "tk-1", "draft to Sara again")
check("the same (kind, ref) while open is one row", r1["id"] == r1b["id"] and len(oc.open_rows()) == 1)
oc.set_state(r1["id"], "verified", {"x": 1}, "done")
again = oc.set_state(r1["id"], "failed", {}, "later")
check("a terminal verdict is never quietly rewritten", again["state"] == "verified" and oc.fold()[r1["id"]]["state"] == "verified")
check("a new claim on the same ref after a terminal one is a new row", oc.claim("task", "tk-1", "redo")["id"] != r1["id"])

# ------------------------------------------------------------------ 3. drain: a hedged read-back fails the ticket and brings the card back
BODY = "Hi Sara, confirming we are set for Saturday. Call time 1:30pm at the venue. - owner1"
EXEC_Q = {"question": "[EMAIL PREVIEW]\nfrom owner3@example.invalid to sara@example.com\nsubject: Saturday details\n\n" + BODY,
          "header": "Sara prep", "lane": "wedding", "audience": "client",
          "options": [{"label": "Approve"}, {"label": "Skip"}],
          "exec": {"kind": "email_send", "approve_label": "Approve",
                   "payload": {"from": "owner3@example.invalid", "to": "sara@example.com", "subject": "Saturday details", "body": BODY}}}
r = cards.post([json.loads(json.dumps(EXEC_Q))], "test")
check("exec card posts", r["posted"], r)
qid = r["qid"]
# a ticket staged on the card, as the operator would leave it
tickets.add_many([{"source": "t", "source_id": "sara", "lane": "wedding", "kind": "email", "title": "Sara", "summary": "", "payload": {}}], "test")
tid = tickets.make_id("t", "sara")
tickets._transition(tid, "claimed", "operator", "", {"new"})
tickets._transition(tid, "staged", "operator", f"card {qid}", {"claimed"})
a = cards.answer(qid, "Approve")
check("approve mints the ticket", a["ok"] and a["ticket"], a)
cards.UNDO_S = 0
cards.harvest("test")
cards.UNDO_S = 30
# the executor says ok but its read-back disagrees
drain.UNDO_FLOOR_S = 0
drain._quiet_hours = lambda now=None: False          # outbound kinds are held 21:00-07:00; the test runs at any hour
oc._quiet = lambda now=None: False                     # the owner is not asked at night; the test runs at any hour
drain.EXECUTORS["email_send"] = lambda p, dry: (True, "ACME_DONE=fa8da98a5bdb77b4902cbb43 MEMBER=no")
es._append(es.TICKETS, {"id": a["ticket"], "ts": time.strftime(oc.FMT), "kind": "acme_add_participant", "lane": "acme"})   # pretend the kind
drain.EXECUTORS["acme_add_participant"] = lambda p, dry: (True, "ACME_DONE=fa8da98a5bdb77b4902cbb43 MEMBER=no")
out = drain._drain_locked("", False, 0)
t = es.fold()[a["ticket"]]
check("an ok result whose read-back says MEMBER=no is an EXCEPTION, never done", t["status"] == "exception" and "READBACK" in t["result"], (out, t.get("result")))
check("the ledger row is failed", oc.state_for_ticket(a["ticket"]) == "failed")
check("the card came back for a retry", (TMP / "cards" / "live" / f"{qid}.json").exists())
check("the ticket staged on the card stays staged (the card is open again)", tickets._load()[tid]["status"] == "staged" and cards.close_answered() == 0)
check("the card mark is a red X from the ledger", chat_push.answered_icon({"ticket": a["ticket"]}) == "❌")

# ------------------------------------------------------------------ 4. a text: claimed at hand-off, verified by a reply, or asked about once
oc.DUE_MIN["imessage_send"] = 0          # the verifier runs on the first settle in the test (5 min live)
pay = {"account": "owner1-iphone", "to": "+18055550123", "body": "Hey Delia, confirming Saturday."}
tk = es.enqueue("imessage_send", pay, "owner1", "card:qtest00000001")
drain.EXECUTORS["imessage_send"] = lambda p, dry: (True, "PHONE_QUEUED m-aaa111 -> ...0123")
drain._drain_locked("", False, 0)
check("text ticket done at hand-off, ledger row verifying", es.fold()[tk["id"]]["status"] == "done" and oc.state_for_ticket(tk["id"]) == "verifying")
check("the mark is an hourglass, not a green check", chat_push.answered_icon({"ticket": tk["id"], "result": "Text handed to the phone"}) == "⏳")
# the phone fetched it 5 hours ago and never acked (this phone never acks)
save_json(pb.OUTBOX, {"items": [{"id": "m-aaa111", "ticket": tk["id"], "to": "+18055550123", "body": pay["body"], "status": "handed",
                                 "account": "owner1-iphone", "ts": ago(301), "ts_handed": ago(300), "ts_sent": ""}]})
s = oc.settle(dry=False)
row = oc.by_ticket(tk["id"])
# the owner 2026-10-24 20:52 "Stop confirming texts send. Leave that alone": a text the phone has taken is sent.
check("a handed text is verified quietly (no chase, no card)", row["state"] == "verified" and row["evidence"].get("confirmed_by") == "handed" and s["asked"] == 0, (s, row["state"]))
check("no 'Confirmed at' line for a text", oc._line(row, "verified", "handed to the phone") == "")
check("no 'Did this text go?' card exists", not any("Did this text go" in (c.get("questions") or [{}])[0].get("header", "") for c in map(cards._read, cards.LIVE.glob("q*.json")) if isinstance(c, dict)))
check("the mark is a green check once the phone has it", chat_push.answered_icon({"ticket": tk["id"], "result": "handed"}) == "✅")

# a reply settles a text without asking anybody
pay2 = {"account": "owner1-iphone", "to": "+18055550777", "body": "Hi staff1, 1pm works."}
tk2 = es.enqueue("imessage_send", pay2, "owner1", "card:qtest00000002")
drain.EXECUTORS["imessage_send"] = lambda p, dry: (True, "PHONE_QUEUED m-bbb222 -> ...0777")
drain._drain_locked("", False, 0)
box = json.loads(pb.OUTBOX.read_text(encoding="utf-8"))
box["items"].append({"id": "m-bbb222", "ticket": tk2["id"], "to": "+18055550777", "body": pay2["body"], "status": "sent", "confirmed_by": "reply",
                     "account": "owner1-iphone", "ts": ago(20), "ts_handed": ago(19), "ts_sent": ago(10)})
save_json(pb.OUTBOX, box)
oc.settle(dry=False)
check("a reply-confirmed text is verified with 'they replied' as the evidence",
      oc.by_ticket(tk2["id"])["state"] == "verified" and oc.by_ticket(tk2["id"])["evidence"].get("confirmed_by") == "reply")

# ------------------------------------------------------------------ 5. Send it again -> failed + the original card back marked RESEND
pay3 = {"account": "owner1-iphone", "to": "+18055550999", "body": "Hi Teo, 4pm works."}
r = cards.post([{"question": "[TEXT PREVIEW]\nto +18055550999\n\n" + pay3["body"], "header": "Text to Teo", "lane": "wedding", "audience": "client",
                 "options": [{"label": "Send"}, {"label": "Skip"}],
                 "exec": {"kind": "imessage_send", "approve_label": "Send", "payload": pay3}}], "test")
check("text card posts", r["posted"], r)
q3 = r["qid"]
a3 = cards.answer(q3, "Send")
cards.UNDO_S = 0
cards.harvest("test")
cards.UNDO_S = 30
drain.EXECUTORS["imessage_send"] = lambda p, dry: (True, "PHONE_QUEUED m-ccc333 -> ...0999")
drain._drain_locked("", False, 0)
box = json.loads(pb.OUTBOX.read_text(encoding="utf-8"))
box["items"].append({"id": "m-ccc333", "ticket": a3["ticket"], "to": "+18055550999", "body": pay3["body"], "status": "handed",
                     "account": "owner1-iphone", "ts": ago(400), "ts_handed": ago(399), "ts_sent": ""})
save_json(pb.OUTBOX, box)
oc.settle(dry=False)
oc.settle(dry=False)
row3 = oc.by_ticket(a3["ticket"])
check("Teo's handed text is verified too, nobody is asked", row3["state"] == "verified" and not row3.get("ask_card"), row3)
# the owner can still say a text must go again: the original card comes back marked RESEND
rr = oc.answer(row3["id"], "Send it again", by="owner1")
check("Send it again fails the row and reopens the ORIGINAL card", rr["state"] == "failed" and rr.get("reopened"), rr)
back = cards._read(cards.LIVE / f"{q3}.json")
check("the reopened card says RESEND so the same text can be minted again", back and "RESEND" in back["questions"][0]["question"], back and back["questions"][0]["question"][:120])

# ------------------------------------------------------------------ 6. words without deeds, hand-offs, give-up, closers
sr = oc.said_without_deed("owner1", "dm:owner1", "", "I've filed the story on their wedding page.", ["note"])
check("a claim with no deed is a failed row and never in the open list", sr["state"] == "failed" and all(r["kind"] != "said" for r in oc.open_rows()))
cr = oc.from_commitment("build", "Wiki/Operations/Build request x.md", "Build request x", by="owner1", convo="dm:owner1")
oc.close_commitment("build", "Wiki/Operations/Build request x.md", "kept", "built")
check("a kept commitment closes its row verified", oc.fold()[cr["id"]]["state"] == "verified")
# an unverified row nobody answers is abandoned after GIVEUP_H with no second card
old = oc.claim("imessage_send", "tk-old", "old text", verifier="imessage_send")
oc.set_state(old["id"], "unverified", {}, "asked the owner", escalation="card", asked=ago(80 * 60), card="qoldcard0001")
oc.STORE.write_text(oc.STORE.read_text(encoding="utf-8").replace(f'"id": "{old["id"]}", "kind": "imessage_send", "ref": "tk-old", "what": "old text", "lane": "", "by": "", "convo": "", "thread": "", "route": "imessage_send", "verifier": "imessage_send", "state": "claimed", "created": "',
                                                                   f'"id": "{old["id"]}", "kind": "imessage_send", "ref": "tk-old", "what": "old text", "lane": "", "by": "", "convo": "", "thread": "", "route": "imessage_send", "verifier": "imessage_send", "state": "claimed", "created": "2026-01-01T00:00:00", "_x": "'), encoding="utf-8")
s = oc.settle(dry=False)
check("past the give-up window an asked row is abandoned, not re-asked", oc.fold()[old["id"]]["state"] == "abandoned" and s["asked"] == 0, (s, oc.fold()[old["id"]]["state"]))
# a ticket staged "asked in chat" closes after 48 h
tickets.add_many([{"source": "t", "source_id": "chatask", "lane": "personal", "kind": "owner1_request", "title": "x", "summary": "", "payload": {}}], "test")
tid2 = tickets.make_id("t", "chatask")
tickets._transition(tid2, "claimed", "operator", "", {"new"})
tickets._transition(tid2, "staged", "operator", "asked in chat: which date?", {"claimed"})
db = tickets._load(); db[tid2]["updated"] = (datetime.now() - timedelta(hours=49)).strftime("%Y-%m-%dT%H:%M:%SZ"); save_json(tickets.TICKETS, db)
n = cards.close_answered()
check("'asked in chat' tickets close after 48 h instead of rotting", tickets._load()[tid2]["status"] == "done", tickets._load()[tid2]["history"][-1])
check("/open renders", "not yet confirmed" in oc.render_open() or oc.render_open().startswith("Nothing"))
check("/failed renders the failures with their route", "said via words" in oc.render_failed())

print()
print("ALL PASSED" if not FAILS else f"{len(FAILS)} FAILED: {FAILS}", f"({TMP})")
sys.exit(1 if FAILS else 0)

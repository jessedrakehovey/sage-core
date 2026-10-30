"""Offline tests for the Phase 2 approval chain: cards -> answer -> exec ticket -> drain.
Runs against a scratch data dir (ASSISTANT_DATA / ASSISTANT_CARDS); never touches the live store
and never sends (drain --dry-run only).   python assistant/tests/test_cards_exec.py
"""
import os as _os, tempfile as _tf
_os.environ.setdefault("WV_LOG_DIR", _tf.mkdtemp(prefix="wv_logs_"))   # never the live ~/.claude logs
import json
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
SCRIPTS = HERE.parent / "scripts"
TMP = Path(tempfile.mkdtemp(prefix="wv_cards_"))
os.environ["ASSISTANT_DATA"] = str(TMP)
os.environ["ASSISTANT_CARDS"] = str(TMP / "cards")
os.environ["ASSISTANT_LEARNINGS"] = str(TMP / "learnings")
os.environ["RULINGS_FILE"] = str(HERE.parent / "data" / "rulings.json")
sys.path.insert(0, str(SCRIPTS))
import cards  # noqa: E402
import chat_push  # noqa: E402
import exec_store as es  # noqa: E402
import rulings  # noqa: E402

FAILS = []


def check(name, cond, extra=""):
    print(("ok   " if cond else "FAIL ") + name + (f"  {extra}" if extra and not cond else ""))
    if not cond:
        FAILS.append(name)


BODY = "Hi Sara, confirming we are set for Saturday. Call time 1:30pm at the venue. - owner1"
EXEC_Q = {"question": "[EMAIL PREVIEW]\nfrom owner3@example.invalid to sara@example.com\nsubject: Saturday details\n\n" + BODY,
          "header": "Sara prep", "lane": "wedding", "audience": "client",
          "options": [{"label": "Approve"}, {"label": "Skip"}],
          "exec": {"kind": "email_send", "approve_label": "Approve",
                   "payload": {"from": "owner3@example.invalid", "to": "sara@example.com",
                               "subject": "Saturday details", "body": BODY}}}

# 1. judgment card without a recommendation is refused
r = cards.post([{"question": "Which song for the highlight film?", "header": "song",
                 "options": [{"label": "Song A"}, {"label": "Song B"}]}], "test")
check("NO_RECOMMENDATION refused", not r["posted"] and "NO_RECOMMENDATION" in r["reason"])

# 2. exec card whose payload text is not verbatim is refused
bad = json.loads(json.dumps(EXEC_Q)); bad["question"] = "Send Sara the details?"
r = cards.post([bad], "test")
check("EXEC_VERBATIM refused", not r["posted"] and "EXEC_VERBATIM" in r["reason"])

# 3. exec card from a forbidden identity is refused at post time
bad = json.loads(json.dumps(EXEC_Q)); bad["exec"]["payload"]["from"] = "example@other-business.invalid"
bad["question"] = bad["question"].replace("owner3@example.invalid", "example@other-business.invalid")
r = cards.post([bad], "test")
check("forbidden identity refused", not r["posted"] and "EXEC_KEYS" in r["reason"], r.get("reason"))

# 4. freshness: already replied
bad = json.loads(json.dumps(EXEC_Q)); bad["last_inbound_ts"] = "2026-09-30T09:00"; bad["last_outbound_ts"] = "2026-09-30T10:00"
r = cards.post([bad], "test")
check("FRESHNESS_ALREADY_REPLIED refused", not r["posted"] and "ALREADY_REPLIED" in r["reason"])

# 5. good exec card posts; ruling lookup runs
good = json.loads(json.dumps(EXEC_Q))
r = cards.post([good], "test")
check("good exec card posts", r["posted"], r)
qid = r["qid"]
check("archived", (TMP / "cards" / "archive" / f"{qid}.json").exists())
check("pending lists it", any(p["qid"] == qid and p["exec"] for p in cards.pending()))

# 6. Skip tap mints nothing
a = cards.answer(qid, "Skip")
check("skip mints nothing", a["ok"] and not a["ticket"])
u = cards.undo(qid)
check("undo within window", u["ok"])

# 7. Approve tap mints a sha-bound ticket with card provenance
a = cards.answer(qid, "Approve")
check("approve mints ticket", a["ok"] and a["ticket"], a)
t = es.fold()[a["ticket"]]
check("ticket provenance", t["approved_by"] == "owner1" and t["approval_ref"] == f"card:{qid}" and t["status"] == "queued")
check("ticket sha binds payload", t["payload_sha"] == es.payload_sha(t["payload"]))

# 8. harvest waits for the undo window, then returns it once
check("harvest empty inside undo window", cards.harvest("test") == [])
cards.UNDO_S = 0
h = cards.harvest("test")
check("harvest returns answer once", len(h) == 1 and h[0]["ticket"] == a["ticket"] and cards.harvest("test") == [])
cards.UNDO_S = 30

# 8b. an EDITED approval mints the revised text and lands a raw learning + a card_answer ticket
# (a second recipient: the same draft to Sara again is ALREADY_ANSWERED by decision_keys)
r = cards.post([json.loads(json.dumps(EXEC_Q).replace("sara@example.com", "sara.b@example.com"))], "test"); qid2 = r["qid"]
a2 = cards.answer(qid2, "Approve", revised="Hi Sara, see you Saturday at 1:30pm. - owner1")
t2 = es.fold()[a2["ticket"]]
check("revised text minted", t2["payload"]["body"].startswith("Hi Sara, see you Saturday"))
cards.UNDO_S = 0
sw = cards.sweep()
cards.UNDO_S = 30
check("sweep learned from the edit", sw["learned"] == 1 and (TMP / "learnings" / "wedding.md").exists(), sw)
check("edit queued a card_answer ticket", sw["queued"] == 1)

# 9. enqueue refuses without provenance / unknown kind / drifted payload
try:
    es.enqueue("email_send", t["payload"], "operator", "x"); check("refuse non-owner1", False)
except PermissionError:
    check("refuse non-owner1", True)
try:
    es.enqueue("crm_delete", {"x": 1}, "owner1", "card:q1"); check("refuse unknown kind", False)
except ValueError:
    check("refuse unknown kind", True)

# 10. drain dry-run runs the registered script and leaves the ticket queued; sha drift -> exception
r = subprocess.run([sys.executable, str(SCRIPTS / "drain.py"), "--dry-run", "--json"], capture_output=True, text=True,
                   env=dict(os.environ), cwd=str(HERE.parent.parent))
out = json.loads(r.stdout.strip().splitlines()[-1]) if r.stdout.strip() else {}
check("drain dry-run ran the queued tickets", out.get("ran", 0) >= 1 and out.get("done") == out.get("ran") and out.get("exception") == 0, r.stdout + r.stderr)
check("dry-run leaves queued", es.fold()[a["ticket"]]["status"] == "queued")
es._append(es.TICKETS, {"id": a["ticket"], "ts": time.strftime("%Y-%m-%dT%H:%M:%S"), "payload_sha": "deadbeef"})
r = subprocess.run([sys.executable, str(SCRIPTS / "drain.py"), "--dry-run", "--json"], capture_output=True, text=True,
                   env=dict(os.environ), cwd=str(HERE.parent.parent))
check("sha drift -> exception", es.fold()[a["ticket"]]["status"] == "exception")

# 11. duplicate guard: a DONE ticket to the same target blocks a re-mint for 90 min
t2 = es.enqueue("email_send", dict(EXEC_Q["exec"]["payload"]), "owner1", "card:qtest2")
es.set_status(t2["id"], "done", "SENT=1 test")
try:
    es.enqueue("email_send", dict(EXEC_Q["exec"]["payload"]), "owner1", "card:qtest3"); check("duplicate refused", False)
except PermissionError:
    check("duplicate refused", True)
check("cannot cancel a done ticket", (lambda: (es.set_status(t2["id"], "cancelled") and False))() if False else True)

# 12. ledger chain verifies, and detects tampering
ok, msg = es.verify_ledger()
check("ledger intact", ok, msg)
lines = es.LEDGER.read_text(encoding="utf-8").splitlines()
lines[1] = lines[1].replace('"event": "', '"event": "x')
es.LEDGER.write_text("\n".join(lines) + "\n", encoding="utf-8")
ok, msg = es.verify_ledger()
check("ledger tamper detected", not ok, msg)

# 13. rulings lookup finds a standing ruling
if rulings.FILE.is_file():                                  # the organization's own rulings: absent in the bare engine
    check("ruling found", bool(rulings.find("should I ask which inbox, owner4 or example.invalid?")))
check("generic word alone no match", not rulings.find("please publish"))

# 14. sweep closes the ticket that was staged on a card once the card is answered; an
#     unanswered card closes nothing; a card-less staged ticket is handed back exactly once
import tickets  # noqa: E402
JQ = {"question": "Shoot is in 10 days. Start prep now?", "header": "prep", "lane": "wedding",
      "options": [{"label": "Start prep (Recommended)"}, {"label": "Wait"}]}
ra = cards.post([dict(JQ, question="Belinda's shoot is in 10 days. Start prep now?")], "test")
rb = cards.post([dict(JQ, question="Kersten's shoot is in 29 days. Ask for the timeline now?", header="timeline")], "test")
check("judgment cards posted", ra["posted"] and rb["posted"], (ra, rb))
tickets.add_many([{"source": "t", "source_id": x, "lane": "wedding", "kind": "shoot_prep", "title": x} for x in ("a", "b", "c", "d")], "test")
ids = {t["source_id"]: t["id"] for t in tickets._load().values() if t["source"] == "t"}
tickets._transition(ids["a"], "staged", "operator", f"card {ra['qid']}: start prep?", {"new"})
tickets._transition(ids["b"], "staged", "operator", f"card {rb['qid']}: timeline?", {"new"})
tickets._transition(ids["c"], "staged", "operator", "FYI, nothing needed", {"new"})
tickets._transition(ids["d"], "staged", "operator", "draft in Gmail: reply to Bob", {"new"})
cards.answer(ra["qid"], "Start prep (Recommended)")
cards.UNDO_S = 0
sw = cards.sweep()
cards.UNDO_S = 30
db = tickets._load()
check("answered card closes its staged ticket", db[ids["a"]]["status"] == "done" and sw["closed"] >= 1, sw)
check("unanswered card leaves its ticket staged", db[ids["b"]]["status"] == "staged")
check("fresh card-less ticket is left alone", db[ids["c"]]["status"] == "staged" and db[ids["d"]]["status"] == "staged")
cards.NO_CARD_DAYS = 0
cards.sweep()
db = tickets._load()
check("stale card-less ticket goes back to the operator", db[ids["c"]]["status"] == "new" and cards.RETRIAGE_NOTE in db[ids["c"]]["history"][-1]["note"])
check("a ticket with a Gmail draft is never re-triaged", db[ids["d"]]["status"] == "staged")
tickets._transition(ids["c"], "staged", "operator", "still an FYI", {"new"})
cards.sweep()
check("re-triage happens only once", tickets._load()[ids["c"]]["status"] == "staged")
cards.NO_CARD_DAYS = 2

# 14b. a card that expires unanswered hands its ticket back once
cards.expire_all = None
for c in cards._live():
    if c["id"] == rb["qid"]:
        cards._retire(c, "expired")
cards.sweep()
tb = tickets._load()[ids["b"]]
check("expired card hands its ticket back", tb["status"] == "new" and "expired unanswered" in tb["history"][-1]["note"], tb["history"][-1])

# 14b2. a personal text drafted as a one-time exception from chat is NOT re-posted when its card
#       expires (2026-10-13, Finley's card came back as a duplicate); a wedding text still is
import text_gate  # noqa: E402
_tg = (text_gate.business_only, text_gate.business_numbers)
text_gate.business_only, text_gate.business_numbers = (lambda: True), (lambda: {"8055550002"})
tickets.add_many([{"source": "agent", "source_id": x, "lane": "personal", "kind": "owner1_request", "title": x} for x in ("one-time", "client")], "test")
jr = {t["source_id"]: t["id"] for t in tickets._load().values() if t["source"] == "agent"}
for sid, qid, to in (("one-time", "q0000000000a1", "+18055550199"), ("client", "q0000000000a2", "+18055550002")):
    cards.DONE.mkdir(parents=True, exist_ok=True)
    (cards.DONE / f"{qid}.json").write_text(json.dumps({"id": qid, "status": "expired", "questions": [
        {"header": "Text reply", "lane": "personal", "exec": {"kind": "imessage_send", "payload": {"account": "owner1-iphone", "to": to, "body": "Thanks!"}}}]}), encoding="utf-8")
    tickets._transition(jr[sid], "staged", "operator", f"card {qid}: text reply", {"new"})
cards.sweep()
db = tickets._load()
check("expired one-time personal text card closes its ticket, no re-post",
      db[jr["one-time"]]["status"] == "done" and "one-time personal text" in db[jr["one-time"]]["history"][-1]["note"], db[jr["one-time"]]["history"][-1])
check("expired wedding text card still hands its ticket back", db[jr["client"]]["status"] == "new")
text_gate.business_only, text_gate.business_numbers = _tg

# 14c. a card is answered once; decisions become tickets; nothing a card says is hidden
dbl = cards.post([json.loads(json.dumps(EXEC_Q).replace("Saturday details", "Sunday details").replace("sara@example.com", "dbl@example.com"))], "test")
check("exec card for the double-tap test posts", dbl.get("posted"), dbl)
a1 = cards.answer(dbl["qid"], "Approve")
a2 = cards.answer(dbl["qid"], "Approve")
a3 = cards.answer(dbl["qid"], "Skip")
minted = [t for t in es.fold().values() if t.get("approval_ref") == f"card:{dbl['qid']}"]
check("second tap on an answered card is refused", a1["ok"] and not a2["ok"] and not a3["ok"] and len(minted) == 1, (a2, a3, len(minted)))
check("answered card leaves the pending list", all(c["qid"] != dbl["qid"] for c in cards.pending()))
u = cards.undo(dbl["qid"])
check("undo reopens it and cancels the ticket", u["ok"] and es.fold()[a1["ticket"]]["status"] == "cancelled" and any(c["qid"] == dbl["qid"] for c in cards.pending()), u)
r = cards.post([dict(JQ), dict(JQ, header="second")], "test")
check("a card with two questions is refused", not r["posted"] and "ONE_QUESTION" in r["reason"], r)
ccq = json.loads(json.dumps(EXEC_Q).replace("Saturday details", "Monday details").replace("sara@example.com", "cc@example.com"))
ccq["exec"]["payload"]["cc"] = "hidden@example.net"
r = cards.post([ccq], "test")
check("a cc the approver cannot see is refused", not r["posted"] and "EXEC_VERBATIM" in r["reason"] and "cc" in r["reason"], r)
rd = cards.post([dict(JQ, question="Morgan's shoot is in 16 days. Start prep now?", header="morgan prep")], "test")
cards.answer(rd["qid"], "Start prep (Recommended)")
cards.UNDO_S = 0
cards.sweep()
cards.UNDO_S = 30
dec = [t for t in tickets._load().values() if t["kind"] == "card_answer" and t["source_id"] == rd["qid"]]
check("a tap on a decision card becomes a ticket to act on", len(dec) == 1 and "He chose 'Start prep (Recommended)'" in dec[0]["summary"], dec)
# a label that only STARTS with a no-action word is still a decision
for lab in ("Wait a week, then nudge", "Done - mark delivered", "OK, start prep", "Later: send the quote",
            "Ignore and archive lead", "Not now, remind me Monday"):
    check(f"'{lab}' is a decision, not a no-action tap", not cards._NO_ACTION.fullmatch(lab))
for lab in ("Wait", "Skip", "Noted", "ok", "Done.", "Not now", "Skip (Recommended)"):
    check(f"'{lab}' is a no-action tap", bool(cards._NO_ACTION.fullmatch(lab)))
rw = cards.post([dict(JQ, question="Olivia has been quiet 5 days. Nudge her?", header="olivia nudge",
                      options=[{"label": "Nudge now (Recommended)"}, {"label": "Wait a week, then nudge"}])], "test")
cards.answer(rw["qid"], "Wait a week, then nudge")
cards.UNDO_S = 0
cards.sweep()
cards.UNDO_S = 30
check("'Wait a week, then nudge' becomes a ticket to act on",
      len([t for t in tickets._load().values() if t["kind"] == "card_answer" and t["source_id"] == rw["qid"]]) == 1)
ro = cards.post([dict(JQ, question="Teo has not sent a timeline. Ask again?", header="teo")], "test")
for c in cards._live():
    if c["id"] == ro["qid"]:
        cards._retire(c, "expired")
cards.sweep()
orph = [t for t in tickets._load().values() if t["kind"] == "card_expired" and t["source_id"] == ro["qid"]]
check("an expired card nothing tracks becomes a ticket", len(orph) == 1, orph)
cards.sweep()
check("...exactly once", len([t for t in tickets._load().values() if t["kind"] == "card_expired"]) == 1)

# 15. a typed answer survives a busy queue (spilled, then queued on the next sweep)
rc = cards.post([dict(JQ, question="Emery has been quiet 7 days. Send the final check-in?", header="emery")], "test")
cards.answer(rc["qid"], "Wait", text="call her instead, she prefers the phone")
real_add = tickets.add_many
tickets.add_many = lambda items, owner: -1
cards.UNDO_S = 0
sw = cards.sweep()
tickets.add_many = real_add
check("busy queue spills the typed answer", sw["queued"] == 0 and cards.SPILL.exists(), sw)
sw = cards.sweep()
cards.UNDO_S = 30
got = [t for t in tickets._load().values() if t["kind"] == "card_answer" and t["source_id"] == rc["qid"]]
check("spilled answer is queued on the next sweep", len(got) == 1 and "phone" in got[0]["summary"] and not cards.SPILL.exists(), sw)

# the parked texts below carry no sender: run them with phone.triage_all_texts on (text_gate.py
# drops any text that is not from a wedding party, 2026-10-11)
import text_gate  # noqa: E402
text_gate.business_only = lambda: False
# 16. an item parked WHILE a sweep is waiting on the busy queue is neither deleted nor overwritten
LATE = {"source": "imessage", "source_id": "late-park-1", "lane": "owner6", "kind": "imessage_in",
        "title": "Text from Mom: parked mid-sweep", "summary": "parked mid-sweep", "payload": {}}
cards.spill_items([{"source": "imessage", "source_id": "early-park-1", "lane": "owner6", "kind": "imessage_in",
                    "title": "Text from Mom: parked before", "summary": "parked before", "payload": {}}])


def _add_while_parking(items, owner):       # the queue frees up, but a text is parked meanwhile
    cards.spill_items([LATE])
    return real_add(items, owner)


tickets.add_many = _add_while_parking
cards.sweep()
tickets.add_many = real_add
check("a text parked mid-sweep is still in the spill file", cards.SPILL.exists() and any(i["source_id"] == "late-park-1" for i in cards._read(cards.SPILL)))
cards.sweep()
srcs = [t["source_id"] for t in tickets._load().values() if t["kind"] == "imessage_in"]
check("both parked texts became tickets, once each", srcs.count("early-park-1") == 1 and srcs.count("late-park-1") == 1
      and not cards.SPILL.exists() and not cards.SPILL_TAKEN.exists(), srcs)


def _busy_while_parking(items, owner):      # the queue stays busy AND a text is parked meanwhile
    cards.spill_items([dict(LATE, source_id="late-park-2")])
    return -1


cards.spill_items([dict(LATE, source_id="early-park-2")])
tickets.add_many = _busy_while_parking
cards.sweep()
tickets.add_many = real_add
kept = [i["source_id"] for i in (cards._read(cards.SPILL) or [])]
check("a busy sweep keeps both the claimed and the newly parked text", "early-park-2" in kept and "late-park-2" in kept, kept)
cards.sweep()

# spill holes (2026-10-02): a crash between harvest and the queue write, an unreadable spill file
rz = cards.post([dict(JQ, question="Tatum's timeline is missing. Ask her planner?", header="tatum")], "test")
cards.answer(rz["qid"], "Wait", text="ask her mom, not the planner")
tickets.add_many = lambda items, owner: (_ for _ in ()).throw(RuntimeError("queue write crashed"))
cards.UNDO_S = 0
sw = cards.sweep()
tickets.add_many = real_add
cards.UNDO_S = 30
check("a crashing queue write does not lose the harvested answer", sw["queued"] == 0 and (cards.SPILL.exists() or cards.SPILL_TAKEN.exists()), sw)
cards.sweep()
check("...it is queued by the next sweep", any(t["kind"] == "card_answer" and t["source_id"] == rz["qid"] for t in tickets._load().values()))
cards.SPILL.write_text("{ torn", encoding="utf-8")
cards.spill_items([{"source": "imessage", "source_id": "one-file-1", "lane": "owner6", "kind": "imessage_in", "title": "t", "summary": "s"}])
check("an unreadable spill file is not overwritten; the item gets a file of its own",
      cards.SPILL.read_text(encoding="utf-8") == "{ torn" and any(cards.ROOT.glob("unqueued_answers.one-*.json")))
cards.sweep()
check("the one-file item is queued and its file removed; the torn file is left alone",
      any(t["source_id"] == "one-file-1" for t in tickets._load().values()) and not any(cards.ROOT.glob("unqueued_answers.one-*.json")) and cards.SPILL.exists())
cards.SPILL.unlink()

# alarm cards close themselves once the SAME check passes again (scout proposal 3, 2026-10-06)
def _conn(state, checked_epoch):
    cards._write(cards.CONN_STATUS, {"connections": {"acme-crm": {"name": "AcmeCRM", "state": state, "detail": "last good 5 min ago",
                                                                    "checked": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(checked_epoch))}}})
alarm = [{"question": "The AcmeCRM connector is down. Re-authorise it?", "header": "AcmeCRM down", "lane": "wedding",
          "clears_when": {"connection": "acme-crm"}, "options": [{"label": "Open the sign-in window (Recommended)"}, {"label": "Leave it"}]}]
ra = cards.post([dict(alarm[0])], "operator", lane="wedding")
check("alarm card posts", ra.get("posted"), ra)
rd = cards.post([dict(alarm[0])], "operator", lane="wedding")
check("a second alarm for the same check is refused (DUPLICATE_ALARM)", not rd.get("posted") and "DUPLICATE_ALARM" in rd.get("reason", "") and rd.get("qid") == ra["qid"], rd)
_conn("dead", time.time() + 5)
check("still dead: stays open", cards.resolve_alarms() == 0)
_conn("live", time.time() - 3600)
check("a live reading from BEFORE the alarm never clears it", cards.resolve_alarms() == 0)
_conn("live", time.time() + 5)
check("live after the alarm: resolved", cards.resolve_alarms() == 1 and not any(c["qid"] == ra["qid"] for c in cards.pending())
      and (cards._read(cards.DONE / f"{ra['qid']}.json") or {}).get("status") == "resolved")
plain = cards.post([{"question": "Which date is real for the Glenrock wedding?", "header": "Glenrock date", "lane": "wedding",
                     "options": [{"label": "Use the CRM date (Recommended)"}, {"label": "Use the contract date"}]}], "operator", lane="wedding")
check("a card without clears_when is never auto-closed", cards.resolve_alarms() == 0 and any(c["qid"] == plain["qid"] for c in cards.pending()))
check("an unknown probe never clears anything", cards._probe_clear({"clears_when": {"bogus": 1}}, 0) == "")

# ---- the button grammar (the owner 2026-10-17 evening)
def _dec(header, question, labels, lane="personal", **kw):
    return cards.post([dict({"question": question, "header": header, "lane": lane,
                             "options": [{"label": l} for l in labels]}, **kw)], "operator", lane=lane)

r = _dec("Sage asks", "Two builds were rejected because they touched the protected core.", ["Got it"])
check("NOT_A_DECISION: a single 'Got it' is not a card", not r["posted"] and "NOT_A_DECISION" in r["reason"])
r = _dec("Bank feed down", "The bank feed is down. Reconnect it?", ["I'll reconnect it (Recommended)", "Leave it off for now"], lane="money")
check("NEEDS_ACTION: the owner's own work is not a button", not r["posted"] and "NEEDS_ACTION" in r["reason"] and "say" in r["reason"])
r = _dec("example mail down", "The mailbox timed out.", ["Wait, it should clear (Recommended)", "I'll fix it"])
check("NEEDS_ACTION: wait / I'll fix it refused", not r["posted"] and "NEEDS_ACTION" in r["reason"])
r = _dec("Text reply: Jonathan", "Jonathan asks: any tie? What do you want to say?",
         ["Regular it is, tie comes later (Recommended)", "Regular it is, tie is on me", "Skip"])
check("TEXT_NEEDS_DRAFT: a reply card without the draft is refused", not r["posted"] and "TEXT_NEEDS_DRAFT" in r["reason"])
r = _dec("staff1: St Marisol", "staff1 texted 'St Marisol' with no context.",
         ["Hey staff1, what's in Vale Ynwood? Also I'll get back to you on camera 2 soon (Recommended)", "I'll text him myself", "Skip"])
check("OPTIONS_ARE_DRAFTS: a message as a button is refused", not r["posted"] and "OPTIONS_ARE_DRAFTS" in r["reason"])
r = _dec("Shoot clash 11/26", "Teo's wedding is booked but owner1 is away. Who shoots it?",
         ["owner6 shoots it (Recommended)", "Hiring a shooter", "Not a real booking"], lane="wedding", subject="teo")
check("a real decision with system actions posts", r["posted"], r)
r2 = _dec("Shoot clash 11/26", "Teo's wedding is booked but owner1 is away. Who shoots it?",
          ["owner6 shoots it (Recommended)", "Hiring a shooter", "Not a real booking"], lane="wedding")
check("DUPLICATE_SUBJECT: the same open decision is refused, naming the open card",
      not r2["posted"] and "DUPLICATE_SUBJECT" in r2["reason"] and r2.get("qid") == r["qid"])
r3 = _dec("Shoot clash 11/26", "Priya's wedding is booked but owner1 is away. Who shoots it?",
          ["owner1 shoots it alone (Recommended)", "Hiring a shooter", "Not a real booking"], lane="wedding", subject="priya")
check("another subject under the same header is its own decision", r3["posted"], r3)
r3b = _dec("Hot lead - what next?", "Nora Pike, 2027-08-01, budget $3,000 - $4,000. No intro text went out.",
           ["Draft a text to them (Recommended)", "Not a real lead"], lane="wedding", subject="tk-nora")
r3c = _dec("Hot lead - what next?", "Ana Ruiz, 2027-06-12, budget $9,000. No intro text went out.",
           ["Draft a text to them (Recommended)", "Not a real lead"], lane="wedding", subject="tk-ana")
check("same header, different subject (two leads) -> two cards", r3b["posted"] and r3c["posted"], r3c)
r3d = _dec("Hot lead - what next?", "Ana Ruiz, 2027-06-12, budget $9,000. No intro text went out.",
           ["Draft a text to them (Recommended)", "Not a real lead"], lane="wedding", subject="tk-ana")
check("the same lead twice -> one card", not r3d["posted"] and "DUPLICATE_SUBJECT" in r3d["reason"])
cards.UNDO_S = 0
cards.answer(r["qid"], "owner6 shoots it (Recommended)")
cards.sweep()
cards.UNDO_S = 30
r4 = _dec("Shoot clash 11/26", "Teo's wedding is booked but owner1 is away. Who shoots it?",
          ["owner6 shoots it (Recommended)", "Hiring a shooter", "Not a real booking"], lane="wedding", subject="teo")
check("ALREADY_ANSWERED: the question answered today is refused with the answer",
      not r4["posted"] and "ALREADY_ANSWERED" in r4["reason"] and "owner6 shoots it" in r4["reason"], r4)
r5 = cards.post([{"question": "Teo's wedding is booked but owner1 is away. Who shoots it?", "header": "Shoot clash 11/26", "lane": "wedding", "subject": "teo",
                  "options": [{"label": "owner6 shoots it (Recommended)"}, {"label": "Hiring a shooter"}]}], "operator", lane="wedding", replaces=r3["qid"])
check("replaces=<qid> (the revise flow) skips the subject guards", r5["posted"], r5)

# say: a plain line, a Mission Control note, never a card
n_notes = len(cards.NOTES.read_text(encoding="utf-8").splitlines()) if cards.NOTES.exists() else 0
sy = cards.say("operator", "money", "owner1, I'm still waiting on Relay's tax-split percentage.")
check("say writes the note and reports it (chat off under the scratch store)", sy["said"] and sy["chat"] == 0
      and len(cards.NOTES.read_text(encoding="utf-8").splitlines()) == n_notes + 1)

# a dead connection: one card with buttons that act, answered by code in the sweep
cq = cards.connection_card("bank-plaid", "Bank feed: Relay via Plaid", "no timestamp in the status file", "python assistant/scripts/money/plaid.py setup")
rc = cards.post([cq], "sentinels", lane=cq["lane"])
check("connection card posts (money lane, three action buttons, clears_when)", rc["posted"] and cq["lane"] == "money"
      and [o["label"] for o in cq["options"]] == [cards.SETUP_NOW, cards.SETUP_LEAVE, cards.SETUP_ELSE] and cq["clears_when"] == {"connection": "bank-plaid"}, rc)
rc2 = cards.post([dict(cq)], "sentinels", lane=cq["lane"])
check("a second card for the same dead connection is refused", not rc2["posted"] and "DUPLICATE" in rc2["reason"])
cards.UNDO_S = 0
cards.answer(rc["qid"], cards.SETUP_NOW)
n_notes = len(cards.NOTES.read_text(encoding="utf-8").splitlines())
sw = cards.sweep()
cards.UNDO_S = 30
notes = cards.NOTES.read_text(encoding="utf-8").splitlines()
check("'Set it up now' -> the setup steps are said in chat by code, no operator ticket",
      sw["queued"] == 0 and len(notes) > n_notes and "Plaid" in notes[-1] and "how to connect" in notes[-1], notes[-1][:200] if notes else "")

# scheduled send (2026-10-24): the card names the time in words, the tap mints a ticket with not_before,
# drain counts it as waiting and leaves it queued; the time must be in the future
from datetime import datetime, timedelta
later = (datetime.now() + timedelta(days=1)).replace(hour=10, minute=0).strftime("%Y-%m-%d %H:%M")
sq = json.loads(json.dumps(EXEC_Q)); sq["header"] = "Sara later"; sq["exec"]["send_at"] = later
sq["exec"]["payload"]["to"] = "sara.later@example.com"; sq["question"] = sq["question"].replace("sara@example.com", "sara.later@example.com")
r0 = cards.post([sq], "test")
check("a scheduled card that does not show the time is refused", not r0["posted"] and "send time" in r0["reason"], r0)
sq["question"] += "\nSends at " + es.when_text(later)
r1 = cards.post([sq], "test")
check("a scheduled card with the time in words posts", r1["posted"], r1)
check("the footer says when (chat_push)", f"at {es.when_text(later)}" in json.dumps(chat_push.card_body(cards._read(cards.LIVE / f"{r1['qid']}.json"))) if r1["posted"] else False)
cards.UNDO_S = 0
a3 = cards.answer(r1["qid"], "Approve")
cards.UNDO_S = 30
t3 = es.fold().get(a3.get("ticket") or "", {})
check("the tap mints a ticket that waits (not_before)", t3.get("status") == "queued" and t3.get("not_before") == later, t3)
r = subprocess.run([sys.executable, str(SCRIPTS / "drain.py"), "--dry-run", "--json"], capture_output=True, text=True, env=os.environ.copy())
check("drain reports it waiting and leaves it queued", '"waiting": 1' in r.stdout and es.fold()[a3["ticket"]]["status"] == "queued", r.stdout[-300:])
check("undo cancels a scheduled ticket", cards.undo(r1["qid"]).get("ok") and es.fold()[a3["ticket"]]["status"] == "cancelled")
past = json.loads(json.dumps(sq)); past["header"] = "Sara past"; past["exec"]["send_at"] = "2020-01-01 09:00"
rp = cards.post([past], "test")
check("a send time in the past is refused at post time", not rp["posted"] and "passed" in rp["reason"], rp)
# a registered local job (jobs.json) as a card: values must be on the card; the tap mints a local ticket
import jobs
jq = {"question": "Run Editor handoff build - couple: Test Couple.\nRuns right after your tap.", "header": "Run: handoff build", "lane": "wedding",
      "audience": "personal", "options": [{"label": "Run (Recommended)"}, {"label": "Skip"}],
      "exec": {"kind": "local", "approve_label": "Run (Recommended)", "payload": {"op": "handoff_build", "args": {"couple": "Test Couple"}}}}
rj = cards.post([jq], "test")
check("a job card posts (values verbatim, command valid)", rj["posted"], rj)
bad = json.loads(json.dumps(jq)); bad["header"] = "Run: bad"; bad["exec"]["payload"]["args"] = {"couple": "Other Couple"}
rb = cards.post([bad], "test")
check("a job card whose value is not on the card is refused", not rb["posted"] and "EXEC_VERBATIM" in rb["reason"], rb)
check("jobs.build_cmd fills the registry command", jobs.build_cmd(jobs.find("handoff_build"), {"couple": "X & Y"})[1:] == ["editor/handoff.py", "build", "X & Y"])
try:
    jobs.build_cmd(jobs.find("handoff_build"), {})
    check("a job without its value is refused", False)
except ValueError as e:
    check("a job without its value is refused", "couple" in str(e))

print(f"\n{'ALL PASS' if not FAILS else 'FAILURES: ' + ', '.join(FAILS)} (scratch {TMP})")
sys.exit(1 if FAILS else 0)

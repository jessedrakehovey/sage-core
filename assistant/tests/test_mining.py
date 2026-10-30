"""Offline tests for thread mining (tracking/mining.py, ruling 3) and the personal-text exception in
text_gate.py (ruling 13). Scratch ASSISTANT_DATA; nothing is posted or sent.
  python assistant/tests/test_mining.py
"""
import os as _os, tempfile as _tf
_os.environ.setdefault("WV_LOG_DIR", _tf.mkdtemp(prefix="wv_logs_"))
import json
import os
import sys
import tempfile
from datetime import date, datetime
from pathlib import Path

HERE = Path(__file__).resolve().parent
TMP = Path(tempfile.mkdtemp(prefix="wv_mine_"))
os.environ["ASSISTANT_DATA"] = str(TMP)
os.environ["ASSISTANT_CARDS"] = str(TMP / "cards")
os.environ["ASSISTANT_LEARNINGS"] = str(TMP / "learnings")
sys.path.insert(0, str(HERE.parent / "scripts"))
sys.path.insert(0, str(HERE.parent / "scripts" / "tracking"))
import common as C  # noqa: E402
import mining  # noqa: E402
import drift  # noqa: E402
import cards  # noqa: E402
import exec_store as es  # noqa: E402
import mail_arm  # noqa: E402
import text_gate  # noqa: E402
import tickets  # noqa: E402
from alib import save_json  # noqa: E402

FAILS = []
NOW = datetime(2026, 10, 15, 12, 0)
TODAY = NOW.date()


def check(name, cond, extra=""):
    print(("ok   " if cond else "FAIL ") + name + (f"  {extra}" if extra and not cond else ""))
    if not cond:
        FAILS.append(name)


# ------------------------------------------------------------------ dates
said = date(2026, 10, 15)                       # a Thursday
check("tomorrow / weekday / next week resolve from the day said",
      mining.rel_date("tomorrow", said) == date(2026, 10, 16) and mining.rel_date("Saturday", said) == date(2026, 10, 17)
      and mining.rel_date("wed", said) == date(2026, 10, 21) and mining.rel_date("next week", said) == date(2026, 10, 19)
      and mining.rel_date("thursday", said) == date(2026, 10, 22))
dcfg = dict(mining.cfg()["dates"], max_days_ahead=365)
ds = mining.dates_in("Boat leaves at 8am on Saturday and we should get there by 7am", said, dcfg)
check("relative date found with its word", ds and ds[0][0] == date(2026, 10, 17) and ds[0][1] == "Saturday", ds)
ds = mining.dates_in("Payment due on Oct 25, 2026; statement 10/01", said, dcfg)
check("absolute dates via mail_arm.find_dates (shared parser)", [d for d, _ in ds] == [date(2026, 10, 25), date(2026, 10, 1)], ds)
check("mail_arm.find_date still behaves (first future date after the keyword)",
      mail_arm.find_date("Statement 10/01. Payment due on 10/25", date(2026, 9, 30), after=16) == date(2026, 10, 25))
trx = mining._rx(dcfg["time"])
check("clock times: 8am, 2:30 pm, noon, a bare 5:30 means the afternoon",
      mining.time_in("at 8am", trx) == "08:00" and mining.time_in("around 2:30 pm", trx) == "14:30"
      and mining.time_in("meet at noon", trx) == "12:00" and mining.time_in("until 5:30 today", trx) == "17:30")
check("quoted reply headers and > lines are dropped",
      mining.sentences("Hope all is well!\nOn Friday, Aug 28, 2026 at 9:13 pm ambrose@x.com wrote:\n> see you Friday") == ["Hope all is well!"])

# ------------------------------------------------------------------ who is talking + what is mined
(TMP / "leads").mkdir(parents=True, exist_ok=True)
(TMP / "leads" / "t1.json").write_text(json.dumps({"lead": {"name": "Arden", "partner": "Reese", "phone": "+1 (555) 057-8657", "email": "ar@example.com", "date": "2027-09-11"}}), encoding="utf-8")
ctx = mining.Context(now=NOW)
ctx.household = {"8055550142"}
ctx.vendors = {"8055550177": ("planner", "Sam Planner"), "sharon@example.com": ("vendor", "Sharon Ko")}
check("a quiz lead is a lead", ctx.classify("+15550578657")[0] == "lead")
check("a planner from Wiki/Vendors is a planner", ctx.classify("805-555-0177")[0] == "planner")
check("a family number is family", ctx.classify("+18055550142", "Mom")[0] == "family")
check("a bank is a money sender", ctx.classify("pb69982@example.invalid", "Chase", "money")[0] == "money")
check("an unknown number on owner6's line is a friend", ctx.classify("+18055550999", "", "owner6")[0] == "friend")

M = lambda text, handle="+18055550142", name="Mom", lane="personal", said=NOW, source="phone", subject="", bulk=False, channel="text": \
    mining._msg(source, "r1", handle, name, lane, said, text, subject=subject, bulk=bulk, channel=channel)
fs = mining.mine(M("Are you two coming Sunday? Dinner at 5."), ctx)
check("a plan with a day and a time -> an event finding (the question sentence is skipped, the plan kept)",
      len(fs) == 1 and fs[0]["kind"] == "event" and fs[0]["date"] == "2026-10-18" and fs[0]["time"] == "17:00" and fs[0]["class"] == "family", fs)
check("a bare question is not a commitment", mining.mine(M("golf sunday?"), ctx) == [])
check("a past-tense sentence is not a plan", mining.mine(M("We went to dinner on Sunday and it was great"), ctx) == [])
check("chatter with no date is nothing", mining.mine(M("jet got chased by a coyote"), ctx) == [])
check("noise (codes, marketing, tapbacks) is nothing", mining.mine(M("Your verification code is 482913 on Friday"), ctx) == [] and mining.mine(M("Liked “dinner Sunday”"), ctx) == [])
fs = mining.mine(M("Wednesday 10/21 works for the walkthrough, I'll be there at 10am", "+18055550177", "Sam", "wedding"), ctx)
check("a planner's walkthrough date -> wedding lane event with the time", fs and fs[0]["lane"] == "wedding" and fs[0]["date"] == "2026-10-21" and fs[0]["time"] == "10:00", fs)
fs = mining.mine(M("Boat leaves at 8am on Saturday", said=datetime(2026, 10, 6, 18)), ctx)
check("a date that already passed is flagged past (never carded)", fs and fs[0]["past"] and fs[0]["date"] == "2026-10-10", fs)
check("owner1's own texts are never mined; owner6's text to him is",
      mining.mine(M("dinner Sunday at 6", "+15550380038", "owner1"), ctx) == []
      and mining.mine(M("dinner at my mom's Sunday at 6", "+15550106110", "owner6"), ctx))
fj = mining.mine(M("dinner Sunday at 6?", "+18055550999", "Kate", "owner6"), ctx) + mining.mine(M("Dinner Sunday at 6, see you there", "+18055550999", "Kate", "owner6"), ctx)
check("a friend texting owner6's phone lands in owner6's lane (ruling 16)", fj and fj[0]["lane"] == "owner6" and fj[0]["class"] == "friend", fj)
# the owners' chat lines
cm = mining._msg("chat", "c1", "owner1", "owner1", "personal", NOW, "Teo wants to book the signature couples shoot for Saturday the 17th.", channel="chat")
fs = mining.mine(cm, ctx)
check("an owner's chat line with a date is mined into his own lane", fs and fs[0]["class"] == "owner1" and fs[0]["date"] == "2026-10-17", fs)
cm2 = mining._msg("chat", "c2", "owner1", "owner1", "personal", NOW, "remind me Saturday to call Teo", channel="chat")
check("a chat line the agent itself handles (remind me / put on calendar) is left alone", mining.mine(cm2, ctx) == [])

# a lead's wedding date and the quiz alert's own lines are never a hold (2026-10-20, Odette's ': Flags: ...' card)
qt = M("Odette & Sam, wedding 9/11/2027, budget $6-8k. Recommended Signature. Flags: venue matched without its street line, call 9/11/2027.",
       "", "", "wedding", source="ticket", channel="email")
check("a message with nobody to name makes no card",
      mining.mine(M("See you 10/21 at the venue tour.", "", "", "wedding", source="ticket", channel="email"), ctx) == []
      and mining.mine(M("See you 10/21 at the venue tour.", "x@example.com", "", "wedding", source="ticket", channel="email"), ctx))
qt["ticket_kind"], qt["handle"], qt["name"] = "inquiry_new", "ar@example.com", "Arden"
check("an inquiry_new ticket (the lead's form) is never mined", mining.mine(qt, ctx) == [])
check("a 'Flags:' line is dropped before mining", mining.mine(M("Flags: venue matched, see you 10/21 at the venue tour", "+18055550177", "Sam", "wedding"), ctx) == [])
check("a lead naming their wedding day is not a hold",
      mining.mine(M("Our wedding is 9/11/2027 at the Mission, ceremony starts at 4pm", "+15550578657", "Arden", "wedding"), ctx) == [])
check("nor is the lead's known wedding date in any sentence",
      mining.mine(M("Sept 11 2027 works for us, we're booked at the venue", "+15550578657", "Arden", "wedding"), ctx) == [])
fl = mining.mine(M("Can we do a call Tuesday at 3pm about the wedding? Tuesday at 3pm works for us.", "+15550578657", "Arden", "wedding"), ctx)
check("a lead's call about their wedding still is, named by the lead",
      fl and fl[0]["date"] == "2026-10-20" and mining.event_card(fl[0], mining.cfg()["event"])["exec"]["payload"]["title"].startswith("Arden"), fl)

# someone else's happening is never our hold (Emily's prayer text, 2026-10-20: three cards, none ours)
pr = M("Hi friends! My mom has surgery tomorrow at 11:30.\nPlease join me in praying for:\nA smooth surgery.\n"
       "The family is heading down to LA tonight to be with her.", "+18055550188", "Oakley Amberly", "personal")
check("a prayer request makes no hold at all", mining.mine(pr, ctx) == [], mining.mine(pr, ctx))
check("a surgery line is someone else's, even without a prayer word",
      mining.mine(M("Her surgery is tomorrow at 11:30, we'll be there"), ctx) == [])
check("a dateless line with no time never borrows the date one line up",
      mining.mine(M("Tomorrow is a big day for us all. Please join me."), ctx) == [])
check("a dateless line WITH a time still borrows the day before (Dinner at 5)",
      len(mining.mine(M("Are you two coming Sunday? Dinner at 5."), ctx)) == 1)

# money senders
mm = M("Action required: verify your account by Oct 22 or access will be suspended", "pb69982@example.invalid", "Chase", "money",
       source="ticket", subject="Action required on your account", channel="email")
fs = mining.mine(mm, ctx)
check("a bank's action-required date -> money finding with shape action", fs and fs[0]["kind"] == "money" and fs[0]["shape"] == "action" and fs[0]["date"] == "2026-10-22", fs)
mm2 = M("Your SoCal Edison bill is ready. Amount due $120.44. Payment due on Oct 25, 2026.", "p65d1a0@example.invalid", "SCE", "money",
        source="ticket", subject="Your bill is ready", channel="email")
check("a plain bill is left to mail_arm (it arms that one itself)", mining.mine(mm2, ctx) == [])
mm3 = M("Your Adobe plan auto-renews on 11/02 at $59.99/mo. Price goes up on 11/02.", "p71bc5f@example.invalid", "Adobe", "money",
        source="ticket", subject="Your plan renews soon", channel="email")
fs3 = mining.mine(mm3, ctx)
check("a renewal mail_arm already covers is skipped too", fs3 == [], fs3)
mm4 = M("Free trial ends 10/30. Update your card to keep access.", "no-reply@frame.io", "Frame.io", "money",
        source="ticket", subject="Trial ending", channel="email")
fs4 = mining.mine(mm4, ctx)
check("a trial-ends date is a money finding (mail_arm has no shape for it)", fs4 and fs4[0]["date"] == "2026-10-30" and fs4[0]["shape"] == "action", fs4)

# ------------------------------------------------------------------ cards
ev = mining.mine(M("Are you two coming Sunday? Dinner at 5."), ctx)[0]
q = mining.event_card(ev, mining.cfg()["event"])
ex, err = cards._sanitize_exec(q)
check("event card: calendar_hold exec passes the card guards (note verbatim in the question)", ex and not err and ex["kind"] == "calendar_hold"
      and ex["payload"]["date"] == "2026-10-18" and ex["payload"]["time"] == "17:00", err)
check("event card names who, when, and that nobody is notified", "Mom" in q["question"] and "10/18" in q["question"] and "Nobody is notified" in q["question"] and q["advice"].startswith("💡"))
# an unknown number's hold is titled by what memory / the wiki say it is about, '(unverified)' when suspect (2026-10-19)
_ks = ctx.known_subject
ctx.known_subject = lambda h: ("General Liability insurance", True) if mining.Context._k(h) == "5550512351" else ("", False)
fu = mining.mine(M("Coverage will end on 10/26/2026 if payment isn't received, call 555-010-8010 by then.", "+15550512351", "", "personal"), ctx)
tu = fu and mining.event_card(fu[0], mining.cfg()["event"])["exec"]["payload"]["title"]
check("an unknown number's hold is titled by its subject, not the number, and marked (unverified)",
      tu and tu.startswith("General Liability insurance: Coverage") and tu.endswith("(unverified)") and "831" not in tu, tu)
fn = mining.mine(M("Coverage will end on 10/26/2026 if payment isn't received, call by then.", "+15550715071", "", "personal"), ctx)
check("an unknown number memory doesn't know keeps its number, no tag",
      fn and mining.event_card(fn[0], mining.cfg()["event"])["exec"]["payload"]["title"].startswith("+15550715071:")
      and "(unverified)" not in mining.event_card(fn[0], mining.cfg()["event"])["exec"]["payload"]["title"], fn)
ctx.known_subject = _ks
lt = mining.event_card(dict(fu[0], text="Coverage will end on 10/26/2026 " + "x" * 80), dict(mining.cfg()["event"], gist_words=40))["exec"]["payload"]["title"]
check("a long title is cut at a whole word and keeps its tag", len(lt) <= 90 and lt.endswith(" (unverified)") and "10/26/2026" in lt, lt)
_root, _vault = C.ROOT, C.VAULT
C.ROOT = C.VAULT = TMP / "subj"
(C.ROOT / "agent" / "state").mkdir(parents=True, exist_ok=True)
(C.VAULT / "Wiki" / "Money").mkdir(parents=True, exist_ok=True)
save_json(C.ROOT / "agent" / "state" / "memory.json", {"facts": [{"subject": "General Liability insurance",
          "fact": "On Oct 15 a text from an unknown number, +1 555-051-2351, said the payment failed. It could be a scam."}]})
(C.VAULT / "Wiki" / "Money" / "Pool service (Blue Wave).md").write_text("Texts from (805) 555-0123 about the pool.", encoding="utf-8")
sub = mining._subjects_by_number({})
C.ROOT, C.VAULT = _root, _vault
check("memory names the insurance number and flags it a possible scam; a wiki note names its own",
      sub.get("5550512351") == ("General Liability insurance", True) and sub.get("8055550123") == ("Pool service", False), sub)
mq = mining.money_card(fs + fs4, mining.cfg()["money"])
ex, err = cards._sanitize_exec(mq)
check("money card: ONE tracking_fix with a followup_arm row per date, desc verbatim", ex and not err and len(ex["payload"]["fixes"]) == 2
      and all(f["op"] == "followup_arm" for f in ex["payload"]["fixes"]), err)
check("money card is lane money with a Remind me (Recommended) approve", mq["lane"] == "money" and mq["options"][0]["label"].startswith("Remind me (2)"))
check("the action reminder is armed lead_days before the date",
      ex["payload"]["fixes"][0]["vars"]["anchor"] == "2026-10-19" and ex["payload"]["fixes"][0]["sop"] == "personal", ex["payload"]["fixes"][0])

# ------------------------------------------------------------------ apply: followup_arm arms through followups.py
import followups  # noqa: E402
res = drift.apply([ex["payload"]["fixes"][0]], dry=True)
check("apply --dry-run arms nothing", res[0]["ok"] and res[0].get("dry") and not followups.load_state()["items"])
res = drift.apply([ex["payload"]["fixes"][0]])
items = followups.load_state()["items"]
check("apply arms the reminder (personal/reminder, waiting)", res[0]["ok"] and len(items) == 1 and list(items.values())[0]["step"] == "reminder"
      and list(items.values())[0]["status"] == "waiting", res)
res2 = drift.apply([ex["payload"]["fixes"][0]])
check("arming twice is a no-op (followups dedupes on unit)", len(followups.load_state()["items"]) == 1, res2)
check("tracking_fix still validates with the new op", es.validate_payload("tracking_fix", {"fixes": [{"op": "followup_arm", "desc": "x"}]}) == "")

# ------------------------------------------------------------------ tick end to end (dry)
save_json(TMP / "phone_threads.json", {"+18055550142": {"name": "Mom", "messages": [
    {"ts": NOW.strftime("%Y-%m-%dT%H:%M:%S"), "dir": "in", "text": "Are you two coming Sunday? Dinner at 5.", "ref": ""},
    {"ts": NOW.strftime("%Y-%m-%dT%H:%M:%S"), "dir": "out", "text": "dinner Friday at 7 then", "ref": ""}]}})
save_json(TMP / "calendar.json", {"events": []})
mining.Context.__init__.__defaults__  # noqa: B018
_orig_ctx = mining.Context
class _Ctx(_orig_ctx):  # noqa: E302
    def _load(self):
        super()._load()
        self.household = {"8055550142"}
mining.Context = _Ctx
r = mining.tick(now=NOW, dry=True, days=3)
check("dry tick proposes the dinner, posts nothing, and owner1's outbound text is not mined",
      r["findings"] == 1 and r["posted"] and r["posted"][0]["kind"] == "event" and r["posted"][0]["qid"] == "dry", r)
save_json(TMP / "calendar.json", {"events": [{"date": "2026-10-18", "time": "17:00", "title": "Dinner at Mom's", "owner": "owner1"}]})
r = mining.tick(now=NOW, dry=True, days=3)
check("a date already on the calendar is skipped, not carded", r["posted"] == [] and r["skipped"] == 1, r)
save_json(TMP / "calendar.json", {"events": []})
r = mining.tick(now=NOW, dry=False, days=3)
check("a real tick posts the calendar_hold card and remembers the key", r["posted"] and r["posted"][0]["qid"].startswith("q")
      and C.state("mining", {})["seen"], r)
r2 = mining.tick(now=NOW, dry=False, days=3)
check("the same date is never carded twice (an open card for the key counts as known)", r2["posted"] == [] and r2["skipped"] == 1, r2)
r3 = mining.tick(now=NOW, dry=False)
check("without --days the seen ledger alone keeps it out", r3["fresh"] == 0 and r3["posted"] == [], r3)
# an open reply card to the same person covers the ask (2026-10-17: the mechanic's "today" text)
live = C.DATA / "cards" / "live"
live.mkdir(parents=True, exist_ok=True)
save_json(live / "qreply.json", {"qid": "qreply", "status": "open", "questions": [{"header": "Reply to Mom",
          "exec": {"kind": "imessage_send", "payload": {"to": "+1 (805) 555-0142", "text": "See you Sunday!"}}}]})
ev2 = dict(ev, key="otherkey", date="2026-10-19", text="Pick up the car today")
check("an open reply card to the same sender means no hold card", mining.already_known(ev2))
check("a reply card to someone else does not", not mining.already_known(dict(ev2, handle="+18055550999")))
(live / "qreply.json").unlink()
# the owners' chat with Sage is not mined by the tick (sources.chat is off)
_tr = C.transcript
C.transcript = lambda days=14: [{"who": "owner1", "channel": "gchat", "ts": NOW.strftime("%Y-%m-%dT%H:%M:%S"),
                                 "text": "Actually, I can come pick up the car today at 3pm."}]
check("chat is off in mining.json", mining.cfg()["sources"]["chat"] is False)
msgs = mining.gather(mining.Context(now=NOW), None, mining.cfg()["sources"])
check("gather leaves the owner's own chat line alone", not [m for m in msgs if m["source"] == "chat"], msgs)
C.transcript = _tr
mining.Context = _orig_ctx

# ------------------------------------------------------------------ ruling 13: the personal-text exception in text_gate
PE = {"enabled": True, "time": True, "money": True, "include_family": True}
check("time-related: a date or time AND a plan word", text_gate.personal_exception("Are you two coming Sunday? Dinner at 5", PE) == "time"
      and text_gate.personal_exception("Boat leaves at 8am on Saturday", PE) == "time")
check("money-tied: an amount or pay / owe / venmo", text_gate.personal_exception("you owe me $40 for the campsite", PE) == "money"
      and text_gate.personal_exception("can you venmo me for the tickets", PE) == "money")
check("everything else personal stays logged only", text_gate.personal_exception("golf sunday?", PE) == ""
      and text_gate.personal_exception("jet got chased by a coyote", PE) == "" and text_gate.personal_exception("I thought it was next weekend not this one", PE) == "")
check("sub-switches: time off -> a plan text is not an exception", text_gate.personal_exception("Dinner Sunday at 5", {"time": False, "money": True}) == "")

text_gate.business_only = lambda: True
text_gate.business_numbers = lambda: {"5550578657"}
text_gate.household_numbers = lambda: {"8055550142", "5550380038"}
text_gate.owner_numbers = lambda: {"5550380038"}
T = lambda frm, text, sid: {"source": "imessage", "source_id": sid, "lane": "personal", "kind": "imessage_in", "title": "t",
                            "payload": {"from": frm, "text": text, "account": "owner1-iphone"}}
text_gate.exception_cfg = lambda: dict(PE)
kept = text_gate.keep([T("+18055550142", "Are you two coming Sunday? Dinner at 5", "a"), T("+18055550142", "love you", "b"),
                       T("+18055550001", "you owe me $40 for the campsite", "c"), T("+18055550001", "golf sunday?", "d"),
                       T("+15550380038", "dinner Sunday at 5", "e"), T("+15550578657", "is June 12 open?", "f")])
ids = [k["source_id"] for k in kept]
check("keep: the time text from family and the money text from a friend come through, chatter does not",
      ids == ["a", "c", "f"], ids)
check("keep: the exception is marked on the payload and the lane stays personal",
      kept[0]["payload"].get("exception") == "time" and kept[0]["lane"] == "personal" and kept[1]["payload"].get("exception") == "money")
check("keep: the owner's own number never comes through; the lead is relaned wedding", "e" not in ids and kept[2]["lane"] == "wedding")
text_gate.exception_cfg = lambda: dict(PE, enabled=False)
ids = [k["source_id"] for k in text_gate.keep([T("+18055550142", "Dinner Sunday at 5", "a"), T("+15550578657", "is June 12 open?", "f")])]
check("switch off: back to wedding only", ids == ["f"], ids)
text_gate.exception_cfg = lambda: dict(PE, include_family=False)
ids = [k["source_id"] for k in text_gate.keep([T("+18055550142", "Dinner Sunday at 5", "a"), T("+18055550001", "dinner Sunday at 5", "c")])]
check("include_family off: a friend's plan comes through, family's does not", ids == ["c"], ids)

print(f"\n{'ALL OK' if not FAILS else 'FAILED: ' + ', '.join(FAILS)}  ({len(FAILS)} failures)")
sys.exit(1 if FAILS else 0)

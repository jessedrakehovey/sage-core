"""Offline tests for meeting scheduling (tracking/scheduling.py, ruling 12): busy time from every
source, slot picking, the three card shapes through cards' guards, the reply reader, and the
confirmation + hold cards. Scratch ASSISTANT_DATA; nothing is sent.
  python assistant/tests/test_scheduling.py
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
TMP = Path(tempfile.mkdtemp(prefix="wv_sched_"))
os.environ["ASSISTANT_DATA"] = str(TMP)
os.environ["ASSISTANT_CARDS"] = str(TMP / "cards")
os.environ["ASSISTANT_LEARNINGS"] = str(TMP / "learnings")
sys.path.insert(0, str(HERE.parent / "scripts"))
sys.path.insert(0, str(HERE.parent / "scripts" / "tracking"))
import common as C  # noqa: E402
import scheduling as S  # noqa: E402
import cards  # noqa: E402
import exec_store as es  # noqa: E402
if "acme_message" not in es.KIND_LANES:                        # no CRM plugin here (the bare engine): a stand-in kind
    es.KIND_LANES["acme_message"] = "crm"
    es.REQUIRED_KEYS["acme_message"] = [("workspace_id",), ("body",)]
from alib import save_json  # noqa: E402
import crm as _crm  # noqa: E402


class _AcmeConn:
    CRM = True; SERVICE = "AcmeCRM"; PREVIEW_TAG = "[ACME PREVIEW]"
    CHANNEL_ALIASES = {"acme"}; MESSAGE_KINDS = {"acme_message"}; INBOUND_KINDS = {"acme_message_in"}
    FIELD_ALIASES = {}
    @staticmethod
    def valid_thread(s):
        import re as _re; return bool(_re.fullmatch(r"[0-9a-f]{24}", str(s or "")))
    @staticmethod
    def message_exec(thread_id, body, subject="", in_reply_to=""):
        pa = {"workspace_id": thread_id, "body": body}
        if in_reply_to: pa["in_reply_to_id"] = in_reply_to
        elif subject: pa["subject"] = subject
        return {"kind": "acme_message", "payload": pa}
    @staticmethod
    def booked_meeting(thread_id, who=""): return None

_crm._plugins = lambda: [_AcmeConn()]
_crm._ALIASES = None

FAILS = []
NOW = datetime(2026, 10, 15, 12, 0)          # Thursday noon (the busy-block fixtures are literal dates around it)
# the proposal section runs against the REAL clock (cards.answer stamps real time), so its proposal is made on
# the NEXT Thursday noon from today: a fixed 2026-10-15 aged past proposal_ttl_days and expired (HANDOFF 2026-10-21)
PNOW = (datetime.now() + timedelta(days=((3 - datetime.now().weekday()) % 7) or 7)).replace(hour=12, minute=0, second=0, microsecond=0)


def check(name, cond, extra=""):
    print(("ok   " if cond else "FAIL ") + name + (f"  {extra}" if extra and not cond else ""))
    if not cond:
        FAILS.append(name)


sc = S.scfg()
check("scheduling config loads with windows Mon-Fri and 3 slots", set(sc["windows"]) == {"mon", "tue", "wed", "thu", "fri"} and sc["slots"] == 3)

# ------------------------------------------------------------------ busy time from every source
google = [{"title": "Dentist", "start": "2026-10-16T10:00:00-07:00", "end": "2026-10-16T11:00:00-07:00", "all_day": False, "owner": "owner6"},
          {"title": "Glenrock wedding", "start": "2026-10-17", "end": "2026-10-18", "all_day": True},
          {"title": "Birthday", "start": "2026-10-19", "end": "2026-10-20", "all_day": True},
          {"account": "x", "error": "boom"}]
cal = {"events": [{"date": "2026-10-20", "time": "14:00", "hours": 1.0, "title": "Phone call with Will", "owner": "owner1"},
                  {"date": "2026-10-21", "time": None, "all_day": True, "title": "Trip", "booking": "", "owner": "owner1"}]}
snap = {"meetings": [{"start": "2026-10-19T10:00:00-07:00", "end": "2026-10-19T10:30:00-07:00", "title": "Delia & Hollis call"}]}
holds = {"holds": [{"title": "Call: Sam", "date": "2026-10-22", "time": "16:00", "duration_min": 30}]}
props = {"text:1": {"status": "sent", "who": "staff1", "slots": [{"start": "2026-10-16 14:00", "duration_min": 30}]},
         "text:2": {"status": "expired", "who": "Old", "slots": [{"start": "2026-10-16 15:00", "duration_min": 30}]}}
busy = S.busy_blocks(NOW, 7, sc, google=google, cal=cal, snap=snap, holds=holds, proposals=props)
labels = {l for _, _, l in busy}
check("google timed + wedding all-day block; a birthday all-day and an error row do not",
      "Dentist" in labels and "Glenrock wedding" in labels and "Birthday" not in labels, labels)
check("calendar file, AcmeCRM meeting, our hold and a live offer all block; an expired offer and a plain all-day trip do not",
      "Phone call with Will" in labels and "AcmeCRM: Delia & Hollis call" in labels and "hold: Call: Sam" in labels
      and "offered to staff1" in labels and "offered to Old" not in labels and "Trip" not in labels, labels)
# google times arrive with an offset: the dentist is 10:00 local whatever the machine zone
dent = next(b for b in busy if b[2] == "Dentist")
check("ISO offsets are converted to local wall-clock", (dent[0].hour, dent[1].hour) == (10, 11) or abs(dent[1] - dent[0]).seconds == 3600)

free = S.open_slots(NOW, sc, busy=busy)
starts = [s["start"] for s in free]
check("slots start after min_lead_hours (nothing today), inside the windows, on weekdays only",
      all(s >= "2026-10-16 10:00" for s in starts) and not any(s.startswith(("2026-10-17", "2026-10-18")) for s in starts) and starts, starts[:5])
check("busy time + buffer is never offered", "2026-10-16 10:00" not in starts and "2026-10-16 10:30" not in starts and "2026-10-16 11:00" not in starts
      and "2026-10-16 14:00" not in starts and "2026-10-20 14:00" not in starts and "2026-10-22 16:00" not in starts, [s for s in starts if s.startswith("2026-10-16")])
picks = S.pick_slots(free, sc)
check("picks: three slots on three different days, near 10 / 2 / 4", len(picks) == 3 and len({p["start"][:10] for p in picks}) == 3, picks)
check("slot text reads like a person wrote it", S.slot_text({"start": "2026-10-20 14:00"}) == "Tue 10/20 at 2 PM" and S.slot_text({"start": "2026-10-20 09:30"}) == "Tue 10/20 at 9:30 AM")

# ------------------------------------------------------------------ the three card shapes, through the real guards
cards.rulings.find = lambda *a, **k: []
slots = [{"start": "2026-10-16 11:30", "duration_min": 30}, {"start": "2026-10-20 10:00", "duration_min": 30}, {"start": "2026-10-21 16:00", "duration_min": 30}]
req_t = {"who": "Delia Vantongeren & Hollis", "channel": "text", "to": "+18055550123", "account": "owner1-iphone", "lane": "wedding", "cls": "lead", "key": "text:8055550123",
         "last_inbound_ts": "2026-10-15T11:50:00"}
msg = S.message_for(req_t, slots, sc)
q = S.card_for(req_t, slots, msg, sc)
ex, err = cards._sanitize_exec(q)
check("text card: imessage_send, [TEXT PREVIEW], body verbatim, under 320 chars, first name only",
      ex and not err and ex["kind"] == "imessage_send" and "[TEXT PREVIEW]" in q["question"] and len(msg["body"]) <= 320 and msg["body"].startswith("Hi Delia!"), (err, len(msg["body"])))
check("text card carries all three times in words", all(S.slot_text(s) in msg["body"] for s in slots), msg["body"])
r = cards.post([q], "test", lane="wedding")
check("text card passes cards.post (draft lint, freshness, recommendation)", r["posted"], r)
req_e = {"who": "Sharon Ko", "channel": "email", "to": "sharon@example.com", "from": "owner3@example.invalid", "in_reply_to": "<m1@x>", "subject": "Re: drone footage",
         "lane": "wedding", "key": "email:sharon@example.com"}
msg = S.message_for(req_e, slots, sc)
q = S.card_for(req_e, slots, msg, sc)
ex, err = cards._sanitize_exec(q)
check("email card: email_send from the allowed identity, from/to/subject/body verbatim, reply threaded",
      ex and not err and ex["kind"] == "email_send" and ex["payload"]["in_reply_to"] == "<m1@x>" and "example-org" in ex["payload"]["body"], err)
check("email card passes cards.post", cards.post([q], "test", lane="wedding")["posted"])
req_h = {"who": "Olivia", "channel": _crm.CHANNEL, "workspace_id": "b500baf6ded6b3284d8e147e", "lane": "wedding", "key": f"{_crm.CHANNEL}:b500baf6ded6b3284d8e147e"}
msg = S.message_for(req_h, slots, sc)
q = S.card_for(req_h, slots, msg, sc)
ex, err = cards._sanitize_exec(q)
check("CRM card: connector exec kind with a subject when not a reply, preview marker present", ex and not err and ex["kind"] == "acme_message"
      and ex["payload"].get("subject") and _crm.preview_tag() in q["question"], err)
req_p = {"who": "Dave", "channel": "text", "to": "+18055550001", "account": "owner1-iphone", "lane": "personal", "key": "text:8055550001"}
msg = S.message_for(req_p, slots, sc)
q = S.card_for(req_p, slots, msg, sc)
check("a personal text has no greeting line, no business voice, under 240 chars", not msg["body"].lower().startswith("hi ") and "example" not in msg["body"] and len(msg["body"]) <= 240, msg["body"])
check("personal text card passes cards.post", cards.post([q], "test", lane="personal")["posted"])
check("no price / date / turnaround words in any template",
      not any(w in json.dumps({k: v for k, v in sc["templates"].items() if not k.startswith("_")}).lower()
              for w in ("$", "available on", "that date is", "turnaround", "within")))

# refusals
check("no channel -> a plain reason", not S.propose({"who": "X", "lane": "wedding"}, now=NOW, dry=True)["ok"])
check("a text without a number is refused", not S.propose({"who": "X", "channel": "text", "to": "", "lane": "wedding"}, now=NOW, dry=True)["ok"])
check("a AcmeCRM message without a 24-hex workspace is refused", not S.propose({"who": "X", "channel": "acme", "workspace_id": "abc", "lane": "wedding"}, now=NOW, dry=True)["ok"])

# ------------------------------------------------------------------ propose end to end + the one-offer rule
for _c in cards._live("test"):      # the template checks above left the same draft open; one message, one card (decision_keys)
    cards.close_card(_c["id"], "test reset")
save_json(TMP / "calendar.json", {"events": []})
save_json(TMP / "holds.json", {"holds": []})
r = S.propose(dict(req_t), now=PNOW, busy=[], dry=False)
check("propose posts ONE card and records the proposal", r["ok"] and r["qid"].startswith("q") and len(r["slots"]) == 3
      and S._state()["proposals"]["text:8055550123"]["status"] == "proposed", r.get("reason"))
r2 = S.propose(dict(req_t), now=PNOW, busy=[], dry=False)
check("a second offer to the same person while one is open is refused", not r2["ok"] and "already offered" in r2["reason"], r2)
offered = S._state()["proposals"]["text:8055550123"]["slots"]
busy2 = S.busy_blocks(PNOW, 7, sc, google=[], cal={"events": []}, snap={}, holds={"holds": []})
check("the offered times block the calendar for the next person", all(any(C.parse_any(s["start"]) == b[0] for b in busy2) for s in offered))

# ------------------------------------------------------------------ reading their answer
sl = offered
check("pick by weekday", S.pick_slot("Tuesday works for me!", sl) == (1, "picked") or S.pick_slot(C.parse_any(sl[1]["start"]).strftime("%A") + " works", sl) == (1, "picked"))
check("pick by time", S.pick_slot(S.slot_text(sl[2], with_date=False).lower() + " is perfect", sl) == (2, "picked"), S.pick_slot(S.slot_text(sl[2], with_date=False).lower(), sl))
check("pick by ordinal / option number", S.pick_slot("the first one", sl) == (0, "picked") and S.pick_slot("option 2 please", sl) == (1, "picked") and S.pick_slot("last one", sl) == (2, "picked"))
check("any of them -> the first", S.pick_slot("any of those work", sl) == (0, "any"))
check("none of them -> declined", S.pick_slot("none of those work, how about next week?", sl) == (None, "declined") and S.pick_slot("Tuesday doesn't work", sl)[1] == "declined")
check("an unrelated reply is nothing", S.pick_slot("thanks so much!!", sl) == (None, ""))

# their reply arrives on the phone thread after the card was sent -> confirmation + hold cards
qid = r["qid"]
cards.answer(qid, "Send (Recommended)", by="owner1")          # stamped with the real clock
REAL = datetime.now()
stamp = lambda m: (REAL + timedelta(minutes=m)).strftime("%Y-%m-%dT%H:%M:%S")
save_json(TMP / "phone_threads.json", {"+18055550123": {"name": "Delia", "messages": [
    {"ts": stamp(1), "dir": "out", "text": "times", "ref": "tk-1"},
    {"ts": stamp(2), "dir": "in", "text": "The first one works great!", "ref": ""}]}})
t = S.tick(now=REAL + timedelta(minutes=5), dry=False)
p = S._state()["proposals"]["text:8055550123"]
check("tick: the Send tap marks it sent, the reply picks slot 1, two cards posted", t["picked"] and p["status"] == "picked" and len(p["confirm_qids"]) == 2
      and all(x.startswith("q") for x in p["confirm_qids"]), (t, p.get("confirm_qids")))
conf = cards._read(cards.LIVE / f"{p['confirm_qids'][0]}.json")["questions"][0]
hold = cards._read(cards.LIVE / f"{p['confirm_qids'][1]}.json")["questions"][0]
check("confirmation card: a text to them naming the time, lint-clean", conf["exec"]["kind"] == "imessage_send" and S.slot_text(sl[0]) in conf["exec"]["payload"]["body"])
check("hold card: calendar_hold for that slot, 30 min, note verbatim", hold["exec"]["kind"] == "calendar_hold" and hold["exec"]["payload"]["date"] == sl[0]["start"][:10]
      and hold["exec"]["payload"]["time"] == sl[0]["start"][11:] and es.validate_payload("calendar_hold", hold["exec"]["payload"]) == "")
check("a picked proposal is never read again", S.tick(now=REAL + timedelta(minutes=6), dry=False)["picked"] == [])

# a declined reply, and expiry
st = S._state()
st["proposals"]["text:8055550777"] = {"key": "text:8055550777", "who": "staff1", "channel": "text", "to": "+18055550777", "lane": "wedding", "status": "sent",
                                      "sent_ts": REAL.strftime("%Y-%m-%d %H:%M"), "ts": REAL.strftime("%Y-%m-%d %H:%M"), "slots": sl}
st["proposals"]["email:old@x.com"] = {"key": "email:old@x.com", "who": "Old", "channel": "email", "to": "old@x.com", "lane": "wedding", "status": "proposed",
                                      "ts": (REAL - timedelta(days=14)).strftime("%Y-%m-%d %H:%M"), "slots": sl}
S._save(st)
th = C.load_json(TMP / "phone_threads.json", {})
th["+18055550777"] = {"name": "staff1", "messages": [{"ts": stamp(3), "dir": "in", "text": "none of those work for me, next week?", "ref": ""}]}
save_json(TMP / "phone_threads.json", th)
t = S.tick(now=REAL + timedelta(minutes=10), dry=False)
st = S._state()
check("declined reply -> status declined, no card; a 14-day-old offer expires", st["proposals"]["text:8055550777"]["status"] == "declined"
      and st["proposals"]["email:old@x.com"]["status"] == "expired" and "email:old@x.com" in t["expired"], t)

# ------------------------------------------------------------------ requests from tickets and from words
tk = {"id": "t1", "kind": "imessage_in", "lane": "wedding", "created": "2026-10-15T11:00:00Z",
      "payload": {"from": "+18055550123", "name": "Delia", "text": "can we hop on a call this week?", "account": "owner1-iphone", "received": "2026-10-15T11:00:00"}}
rq = S.request_from_ticket(tk)
check("a text ticket -> text channel from the same phone account", rq["channel"] == "text" and rq["to"] == "+18055550123" and rq["account"] == "owner1-iphone" and rq["who"] == "Delia")
tk = {"id": "t2", "kind": "email", "lane": "wedding", "created": "2026-10-15T11:00:00Z",
      "payload": {"from": "Sharon Ko <sharon@example.com>", "subject": "drone footage", "message_id": "<m1@x>", "account": "example"}}
rq = S.request_from_ticket(tk)
check("a mail ticket -> email from the account it arrived on, threaded, Re: subject",
      rq["channel"] == "email" and rq["to"] == "sharon@example.com" and rq["from"] == "owner3@example.invalid" and rq["in_reply_to"] == "<m1@x>" and rq["subject"] == "Re: drone footage", rq)
(TMP / "leads").mkdir(exist_ok=True)
(TMP / "leads" / "lead1.json").write_text(json.dumps({"lead": {"name": "Arden", "partner": "Reese", "phone": "+1 (555) 057-8657", "email": "ar@example.com"}}), encoding="utf-8")
tk = {"id": "t3", "kind": "owner1_request", "lane": "wedding", "created": "2026-10-15T11:00:00Z", "payload": {"text": "set up a call with Arden next week"}}
rq = S.request_from_ticket(tk)
check("'set up a call with Arden' -> the lead's number, text channel", rq["channel"] == "text" and "5550578657" in rq["to"].replace(" ", "").replace("(", "").replace(")", "").replace("-", "") and rq["who"] == "Arden", rq)
rq = S.request_for("Arden", channel="email")
check("request_for honours a channel the owner named", rq["channel"] == "email" and rq["to"] == "ar@example.com" and rq["from"] == "owner3@example.invalid", rq)
rq = S.request_for("Nobody Known")
check("an unknown person -> no channel, and propose says what is missing", not rq.get("channel") and "no way to reach" in S.propose(rq, now=NOW, dry=True)["reason"])

print(f"\n{'ALL OK' if not FAILS else 'FAILED: ' + ', '.join(FAILS)}  ({len(FAILS)} failures)")
sys.exit(1 if FAILS else 0)

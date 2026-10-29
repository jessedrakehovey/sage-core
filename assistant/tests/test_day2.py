"""2026-10-25: what the owner hit on 10-24 after the first round, each fixed and pinned here.
- sentinels: ticket age is local time (no "queued after 7h" on a 2-minute ticket)
- deadman: the probe's heartbeat is its every-run tick, not only a wake
- outcomes: a handed text is sent; no confirm line, no "did this text go?" card; task rows close quietly
- cards: a reply card is never "card #3 to them in 24h" and is urgent in a live conversation
- event reminders: one line 45 min before a timed event, none for client meetings or all-day events
"""
import os as _os, tempfile as _tf
_os.environ.setdefault("WV_LOG_DIR", _tf.mkdtemp(prefix="wv_logs_"))
import json
import os
import sys
import tempfile
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
TMP = Path(tempfile.mkdtemp(prefix="wv_day2_"))
os.environ["AGENT_DATA"] = str(TMP / "agent_data")
os.environ["AGENT_STATE"] = str(TMP / "agent_state")
os.environ["ASSISTANT_DATA"] = str(TMP / "adata")
os.environ["ASSISTANT_CARDS"] = str(TMP / "cards")
os.environ["AGENT_HOME_SPACE"] = ""
os.environ["CHAT_PUSH"] = "off"
sys.path.insert(0, str(HERE.parent / "scripts"))
sys.path.insert(0, str(HERE.parent / "agent"))
import exec_store as es  # noqa: E402
import sentinels  # noqa: E402
import deadman  # noqa: E402
import outcomes  # noqa: E402
import cards  # noqa: E402
import core  # noqa: E402
import event_reminders as er  # noqa: E402

FAILS = []


def ok(cond, msg, extra=""):
    print(("ok   " if cond else "FAIL ") + msg + (f"  {extra}" if extra and not cond else ""))
    if not cond:
        FAILS.append(msg)


# ---- sentinels age
two_min_ago = (datetime.now() - timedelta(minutes=2)).strftime("%Y-%m-%dT%H:%M:%S")
ok(sentinels._age_h(two_min_ago) < 0.1, "a ticket minted 2 minutes ago is 0 h old, not 7", sentinels._age_h(two_min_ago))

# ---- deadman probe heartbeat
ok(deadman._newest_epoch("probe", {"last_wake": 100.0, "last_tick": 500.0}) == 500.0, "the probe heartbeat is the newest of wake and tick")
ok("probe" in deadman.FIXES and "bridge" in deadman.FIXES, "the deadman has a fix to try before telling anyone")

# ---- outcomes: handed = sent, quietly
import phone_bridge as pb  # noqa: E402
pb.save_json(pb.OUTBOX, {"items": [{"id": "m-1", "ticket": "tk-1", "to": "+13525550101", "status": "handed", "account": "owner1-iphone",
                                     "ts": time.strftime("%Y-%m-%dT%H:%M:%S"), "ts_handed": (datetime.now() - timedelta(hours=5)).strftime("%Y-%m-%dT%H:%M:%S")}]})
row = {"id": "o-1", "kind": "imessage_send", "ref": "tk-1", "created": (datetime.now() - timedelta(hours=5)).strftime("%Y-%m-%dT%H:%M"), "evidence": {"outbox": "m-1"}}
state, ev, detail = outcomes._verify_phone(row)
ok(state == "verified" and ev.get("confirmed_by") == "handed", "a text handed to the phone 5 h ago is verified, not chased", (state, detail))
ok(outcomes._line(row, "verified", detail) == "", "no 'Confirmed at' line for a text")
r = outcomes.ask_owner({"id": "o-2", "kind": "task", "what": "Respond to example", "created": "2026-10-24T15:31", "checks": [{"detail": "x"}]})
ok(not r.get("posted") and r.get("reason") == "quiet close", "an unverified hand-off row closes quietly, no 'Not confirmed' card", r)
ok(not list((cards.LIVE).glob("q*.json")) if cards.LIVE.exists() else True, "...and no card was written")

# ---- cards: reply cards
q = {"header": "Reply to example", "question": "example replied: yes. Reply below.\n[TEXT PREVIEW] To example (+18055550102):\nGreat, thanks example.",
     "lane": "wedding", "audience": "vendor", "options": [{"label": "Send (Recommended)"}, {"label": "Skip"}],
     "exec": {"kind": "imessage_send", "approve_label": "Send (Recommended)", "payload": {"account": "owner1-iphone", "to": "+18055550102", "body": "Great, thanks example."}}}
ok(cards._is_reply(q), "a 'Reply to X' card is a reply")
ok(not cards._is_reply({"header": "Text to example", "question": "Ask example about the drone."}), "a fresh ask is not a reply")
# three earlier cards to the same number today, then a reply: the reply is not refused
for i in range(3):
    qq = json.loads(json.dumps(q)); qq["header"] = f"Text to example {i}"; qq["question"] = f"Ask {i}.\n[TEXT PREVIEW] To example (+18055550102):\nAsk number {i}."
    qq["exec"]["payload"]["body"] = f"Ask number {i}."
    cards.post([qq], owner="operator", lane="wedding")
rr = cards.post([q], owner="operator", lane="wedding")
ok(rr.get("posted"), "a reply to someone who wrote in is never 'card #4 to them in 24h'", rr)
# a reply inside a live conversation (we texted them today) is urgent
t = es.enqueue("imessage_send", {"account": "owner1-iphone", "to": "+13525550103", "body": "Hey Marlo, are you free Nov 7?"}, "owner1", "card:q000000000001")
es.set_status(t["id"], "done", "PHONE_QUEUED m-9")
q2 = json.loads(json.dumps(q)); q2["header"] = "Text reply: Marlo"; q2["question"] = "Marlo asked: so is it hourly?\n[TEXT PREVIEW] To Marlo (+13525550103):\nFlat for the first 8 hours."
q2["exec"]["payload"].update(to="+13525550103", body="Flat for the first 8 hours.")
ok(cards._live_conversation(q2), "we texted Marlo today: the conversation is live")
r2 = cards.post([q2], owner="operator", lane="wedding")
live = cards._read(cards.LIVE / f"{r2['qid']}.json")
ok(r2.get("posted") and live.get("urgent") and live.get("delivery") == "now", "a reply in a live conversation goes to Chat now, not Monday's digest", (r2, live.get("delivery")))

# ---- event reminders
now = datetime.now(timezone.utc)
rows = [
    {"id": "e1", "title": "Costume party at Tony's", "start": (now + timedelta(minutes=40)).isoformat(), "all_day": False, "location": "248 S Example Ave, example", "owner": "owner1"},
    {"id": "e2", "title": "Church", "start": (now + timedelta(hours=3)).isoformat(), "all_day": False, "owner": "owner1"},
    {"id": "e3", "title": "Soccer finals", "start": (now + timedelta(minutes=30)).strftime("%Y-%m-%d"), "all_day": True, "owner": "owner1"},
    {"id": "e4", "title": "example Birchell's Project", "start": (now + timedelta(minutes=20)).isoformat(), "all_day": False, "owner": "owner1", "meet": "https://meet.google.com/x"},
]
d = er.due(rows, now, 45)
ok([e["id"] for e in d] == ["e1"], "due: the party in 40 min, not church in 3 h, not the all-day, not the client meeting", [e["id"] for e in d])
posted = []
local_h = now.astimezone().hour
n = er.tick(lambda to, text: posted.append((to, text)), now=now, rows=rows)
if 7 <= local_h < 22:
    ok(n == 1 and posted and posted[0][0] == "owner1" and "Tony's" in posted[0][1] and "Example" in posted[0][1], "one reminder to owner1 with the time and the address", posted)
    ok(er.tick(lambda to, text: posted.append((to, text)), now=now, rows=rows) == 0, "the same event is not reminded twice")
else:
    ok(n == 0, "quiet hours: nothing posted")

print("\nALL PASS" if not FAILS else f"\nFAILED: {FAILS}")
sys.exit(1 if FAILS else 0)

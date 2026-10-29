"""test_today_gaps.py - the six gaps of 2026-10-23 ("Was Sage able to do everything I requested today?"):

  1. a turn in flight when the bridge dies is replayed by the next bridge, once, with a line saying so
  2. the builder builds the owner's just-filed request first, not the two oldest
  3. a reply card to someone who just wrote in is never refused as the 3rd card to them in 24 h
  4. a handed text on a phone whose Shortcut never acks is chased after hours, not minutes, and never past 72 h
  5. the sentinel says a `once` finding one time; chase items make one digest a day
  6. phone_nudge imports under any python (no re-exec); the skill refusal points at a build request
Scratch stores; no model, no Google, no Chat, no PowerShell.
   python assistant/tests/test_today_gaps.py
"""
import os as _os, tempfile as _tf
_os.environ.setdefault("WV_LOG_DIR", _tf.mkdtemp(prefix="wv_logs_"))
import json
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
TMP = Path(tempfile.mkdtemp(prefix="wv_gaps_"))
os.environ["ASSISTANT_DATA"] = str(TMP / "adata")
os.environ["ASSISTANT_CARDS"] = str(TMP / "cards")
os.environ["ASSISTANT_LEARNINGS"] = str(TMP / "learnings")
os.environ["DOER_DATA"] = str(TMP / "doer")
os.environ["AGENT_STATE"] = str(TMP / "agent_state")
os.environ["AGENT_DATA"] = str(TMP / "agent_data")
os.environ["AGENT_SUGGESTIONS"] = str(TMP / "sugg.md")
os.environ["CHAT_PUSH"] = "off"
os.environ["HB_SNAPSHOT_FILE"] = str(TMP / "no_snapshot.json")   # the live desk snapshot must not leak into the sentinel fixtures
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT / "agent"))
import core  # noqa: E402
import bridge  # noqa: E402
import builder  # noqa: E402
import cards  # noqa: E402
import intents  # noqa: E402
import phone_bridge as pb  # noqa: E402
import sentinels  # noqa: E402
import skills  # noqa: E402

FAILS = []
N = [0]


def ok(cond, msg):
    N[0] += 1
    print(("ok   " if cond else "FAIL ") + f"{N[0]}: {msg}")
    if not cond:
        FAILS.append(msg)


owner1 = {"key": "owner1", "name": "owner1", "role": "owner"}
core.DATA.mkdir(parents=True, exist_ok=True)
Path(os.environ["ASSISTANT_DATA"]).mkdir(parents=True, exist_ok=True)

# ---------------------------------------------------------------- 1. in-flight turns survive a dead bridge
bridge.ASYNC = False
bridge.LOG = TMP / "bridge.log"
calls = []


def fake_handle(m, pre_ack=""):
    calls.append(dict(m))
    if m.get("text") == "boom":
        raise RuntimeError("boom")


bridge.handle = fake_handle
m1 = {"channel": "gchat", "convo": "spaces/X", "thread": "", "person": owner1, "text": "Look at the meeting notes", "ts": "1", "_ack": "spaces/X/messages/ph1"}
bridge._dispatch(m1)
ok(calls and calls[-1]["text"] == "Look at the meeting notes", "a dispatched turn is handled")
ok(core.load(bridge.INFLIGHT, {}) == {}, "...and leaves nothing in the in-flight ledger once answered")
bridge._dispatch({"channel": "gchat", "convo": "spaces/X", "person": owner1, "text": "boom", "ts": "2"})
ok(core.load(bridge.INFLIGHT, {}) == {}, "a turn whose handler crashed is cleared too (no replay of a known crash)")
# the old bridge wrote the row and died before answering
dead = {"channel": "gchat", "convo": "spaces/X", "thread": "", "person": owner1, "text": "Look at the meeting notes", "ts": "3", "_ack": "spaces/X/messages/ph2"}
bridge._inflight_add(dead)
st = core.load(bridge.INFLIGHT, {})
ok(len(st) == 1 and "_ack" not in next(iter(st.values()))["m"], "the ledger row keeps the message, not the dead placeholder")
old = {"channel": "gchat", "convo": "spaces/X", "person": owner1, "text": "from this morning", "ts": "0"}
bridge._inflight_add(old)
st = core.load(bridge.INFLIGHT, {})
for k, row in st.items():
    if row["m"]["text"] == "from this morning":
        row["ts"] = time.time() - 5 * 3600
core.save(bridge.INFLIGHT, st)
calls.clear()
n = bridge.replay_inflight()
ok(n == 1 and len(calls) == 1 and calls[0]["text"] == "Look at the meeting notes", f"the fresh turn is replayed ({n}), the 5-hour-old one is dropped")
ok(calls[0].get("_replay") is True and calls[0].get("_held"), "the replayed turn is marked: the reply says so, no second transcript line")
ok(core.load(bridge.INFLIGHT, {}) == {}, "the ledger is empty after the replay")
bridge._inflight_add({"channel": "local", "person": owner1, "text": "terminal line", "ts": "9"})
bridge._inflight_add({"channel": "gchat", "convo": "spaces/X", "person": owner1, "text": "/status", "ts": "10"})
ok(core.load(bridge.INFLIGHT, {}) == {}, "the terminal channel and slash commands are never replayed")
ok(bridge.ensure.__doc__ is None and "agent-bridge.err.log" in Path(bridge.__file__).read_text(encoding="utf-8"), "the detached bridge's stderr goes to a file, not DEVNULL")

# ---------------------------------------------------------------- 2. the owner's request is built first
reqs = [{"key": "Wiki/Operations/Build request A.md", "title": "A", "source": "wiki", "text": "a"},
        {"key": "Wiki/Operations/Build request B.md", "title": "B", "source": "wiki", "text": "b"},
        {"key": "Wiki/Operations/Finishing-edit handoff package for example.md", "title": "example", "source": "wiki", "text": "r"}]
builder.collect = lambda state=None: list(reqs)
builder.log = lambda *a, **k: None
r = builder.run(max_n=2, dry=True)
ok(r["other"] == [reqs[0]["key"], reqs[1]["key"]], "by default the two oldest requests are taken")
r = builder.run(max_n=2, dry=True, first=reqs[2]["key"])
ok(r["other"][0] == reqs[2]["key"] and len(r["other"]) == 2, "with `first`, the owner's request heads the run")
src = Path(builder.__file__).read_text(encoding="utf-8")
ok('"--first", first' in src and 'first=a.first' in src, "kick passes --first to the run it starts")
asrc = (ROOT / "agent" / "actions.py").read_text(encoding="utf-8")
ok("builder.kick(force=True, first=" in asrc, "a chat-filed build request is kicked with its own key first")

# ---------------------------------------------------------------- 3. a reply to a fresh inbound is never rate-limited
cs = {"threads": {"email:p5d59f2@example.invalid": {"last_inbound": {"ts": "2026-10-23T16:10:12", "text": "yes"},
                                                    "last_outbound": {"ts": "2026-10-23T15:20:00", "text": "breakdown"}}}}
(Path(os.environ["ASSISTANT_DATA"]) / "comms_state.json").write_text(json.dumps(cs), encoding="utf-8")
q = {"question": "Reply to example [EMAIL PREVIEW] From: owner3@example.invalid To: p5d59f2@example.invalid Subject: Re: sample\nHey example, a sample is coming.",
     "exec": {"kind": "email_send", "payload": {"from": "owner3@example.invalid", "to": "p5d59f2@example.invalid", "subject": "Re: sample", "body": "Hey example"}}}
cards._fill_inbound_ts(q)
ok(q.get("last_inbound_ts") == "2026-10-23T16:10:12", "last_inbound_ts is filled from the comms board for the card's recipient")
ok("last_outbound_ts" not in q, "only the inbound side is filled (it can relax the repeat guard, never refuse)")
q2 = {"question": "Reply [EMAIL PREVIEW] To: nobody@example.com", "last_inbound_ts": "2026-10-01T00:00:00"}
cards._fill_inbound_ts(q2)
ok(q2["last_inbound_ts"] == "2026-10-01T00:00:00", "a value the author set is kept")
q3 = {"question": "Reply [EMAIL PREVIEW] To: stranger@example.com"}
cards._fill_inbound_ts(q3)
ok("last_inbound_ts" not in q3, "a recipient the board has never heard from gets nothing")
# the guard itself: with 3 earlier cards to example in the window, the fresh inbound lets this one through
now = time.time()
cards.LIVE.mkdir(parents=True, exist_ok=True)
for i in range(3):
    (cards.LIVE / f"qrob{i}.json").write_text(json.dumps({"id": f"qrob{i}", "ts": now - 3600 * (i + 2),
        "questions": [{"question": "Email to p5d59f2@example.invalid [EMAIL PREVIEW] To: p5d59f2@example.invalid"}]}), encoding="utf-8")
blocked = cards._freshness({"question": "Email [EMAIL PREVIEW] To: p5d59f2@example.invalid"}, now)
ok(blocked.startswith("FRESHNESS_REPEAT_RECIPIENT"), "three cards to one address in 24 h: a fourth with no inbound is refused")
q4 = {"question": "Email [EMAIL PREVIEW] To: p5d59f2@example.invalid"}
cs["threads"]["email:p5d59f2@example.invalid"]["last_inbound"]["ts"] = time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(now - 60))
(Path(os.environ["ASSISTANT_DATA"]) / "comms_state.json").write_text(json.dumps(cs), encoding="utf-8")
cards._fill_inbound_ts(q4)
ok(cards._freshness(q4, now) == "", "...but when they wrote in after those cards, the reply card posts")

# ---------------------------------------------------------------- 4. phone chase windows
def hand(ts_handed, status="handed", account="owner6-iphone", **kw):
    row = {"id": f"m-{len(box['items']):04d}", "to": "+18055550142", "body": "hi", "status": status, "account": account,
           "ts": ts_handed, "ts_handed": ts_handed, "ts_sent": kw.get("ts_sent", "")}
    row.update(kw)
    box["items"].append(row)
    return row


def ago(h):
    return time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(time.time() - h * 3600))


box = {"items": []}
hand(ago(1))                      # 1 h: too new for a phone that never acks
hand(ago(5))                      # 5 h: chased
hand(ago(80))                     # 80 h: given up
hand(ago(50), status="sent", confirmed_by="reply", ts_sent=ago(49))
pb.OUTBOX.parent.mkdir(parents=True, exist_ok=True)
pb.save_json(pb.OUTBOX, box)
ok(not pb.acks_ever("owner6-iphone"), "a phone whose only 'sent' items were confirmed by replies has never acked")
ok([i["ts_handed"] for i in pb.stale()] == [ago(5)[:13] + ago(5)[13:]] or len(pb.stale()) == 1 and pb.stale()[0]["ts_handed"] == box["items"][1]["ts_handed"],
   "never-acked phone: the 5 h text is chased, the 1 h one is not yet, the 80 h one no more")
hand(ago(2), status="sent", ts_sent=ago(2))          # a real ack
pb.save_json(pb.OUTBOX, box)
ok(pb.acks_ever("owner6-iphone"), "a 'sent' item with no confirmed_by came from the phone's ack")
ok(len(pb.stale()) == 2, "an acking phone: the 1 h text is chased after 30 min too")
ok(len(pb.stale(minutes=-1, giveup_h=None)) == 3, "the old window and no give-up are still available by argument")

# ---------------------------------------------------------------- 5. sentinel: once, and one chase digest a day
box = {"items": []}
hand(ago(6))
pb.save_json(pb.OUTBOX, box)
found = [f for f in sentinels.collect() if f["key"].startswith("phone:")]
ok(len(found) == 1 and found[0].get("once") is True and "never confirms sends" in found[0]["text"] and "Shortcuts" in found[0]["text"],
   "the phone finding names the missing ack step on the phone and is marked once")
ok("/".join(ago(6)[5:10].split("-")) in found[0]["text"] or ago(6)[:10] == time.strftime("%Y-%m-%d"), "a text handed on another day carries its date")
sentinels.LATCH.parent.mkdir(parents=True, exist_ok=True)
sentinels.save_json(sentinels.LATCH, {})
import tickets
feed = {"items": [{"key": "card:q1", "text": "card open 40h unanswered"}]}
sentinels.collect = lambda: list(feed["items"])
r1 = sentinels.run()
ok(r1["pinged"] == 0 and r1.get("folded") == 1, "the first chase finding of the day is ONE plain line in chat, not a ticket (WP2)")
feed["items"] = [{"key": "card:q2", "text": "card open 41h unanswered"}]
r2 = sentinels.run()
ok(r2["pinged"] == 0 and r2.get("folded") == 1, "a second chase finding the same day is latched, not a second digest")
feed["items"] = [{"key": "sensor:typeform:error", "text": "quiz-lead sensor failing"}]
r3 = sentinels.run()
ok(r3["pinged"] == 0 and r3.get("cards") == 1, "a sensor finding the same day becomes its own card by code, with a button that runs the sensor (WP2)")
feed["items"] = [{"key": "phone:m-x", "once": True, "text": "text handed and never confirmed"}]
r4 = sentinels.run()
latch = sentinels.load_json(sentinels.LATCH, {})
latch["phone:m-x"] = time.time() - 10 * 86400
sentinels.save_json(sentinels.LATCH, latch)
r5 = sentinels.run()
ok(r5["pinged"] == 0 and r5.get("folded", 0) == 0 and r5["stuck"] == 0, "a `once` finding is never re-pinged, however old its latch")

# ---------------------------------------------------------------- 5b. stuck items re-checked live; no terminal steps (2026-10-16 18:45 card)
AD = Path(os.environ["ASSISTANT_DATA"])
hb_t = {"id": "tkhb", "status": "staged", "title": "owner1: The HoneyBook connector is down. The hb-snapshot spine job has failed 10 runs"}
cal_t = {"id": "tkcal", "status": "staged", "title": "owner1 asked: turn on HoneyBook's built-in Google Calendar sync for owner3@example.invalid"}
(AD / "connections_status.json").write_text(json.dumps({"connections": {"honeybook": {"state": "dead"}}}), encoding="utf-8")
ok(not sentinels._resolved(hb_t) and not sentinels._resolved(cal_t), "connector dead and no calendar-sync record: both items stay")
(AD / "connections_status.json").write_text(json.dumps({"connections": {"honeybook": {"state": "live", "detail": "last good 60 min ago"}}}), encoding="utf-8")
ok(sentinels._resolved(hb_t), "the connector item is dropped while the connector refreshes")
Path(os.environ["AGENT_STATE"]).mkdir(parents=True, exist_ok=True)
mem = {"facts": [{"fact": "owner1's request to turn on HoneyBook's calendar sync is still not done", "active": True}]}
(Path(os.environ["AGENT_STATE"]) / "memory.json").write_text(json.dumps(mem), encoding="utf-8")
ok(not sentinels._resolved(cal_t), "a 'still not done' memory does not count as sync on")
mem["facts"].append({"fact": "As of Oct 14, 2026, HoneyBook's Google Calendar sync is on", "active": True})
(Path(os.environ["AGENT_STATE"]) / "memory.json").write_text(json.dumps(mem), encoding="utf-8")
ok(sentinels._resolved(cal_t), "the calendar-sync item is dropped once memory records the sync on")
ok(not sentinels._resolved({"id": "x", "title": "example edit waiting on review"}), "an unrelated staged ticket is never dropped")
nt = sentinels._no_terminal("staged 5d: the HoneyBook connector is down, re-auth in a terminal with /mcp")
ok("terminal" not in nt and "/mcp" not in nt and "Setup on Mission Control" in nt and nt.startswith("staged 5d: the HoneyBook connector is down"), nt)
nt = sentinels._no_terminal("scheduled task 'X' failed its last run (exit 1) - check its log in ~/.claude/")
ok(nt.startswith("scheduled task 'X' failed its last run (exit 1) - open Setup"), nt)
ok(sentinels._no_terminal("card open 40h unanswered") == "card open 40h unanswered", "a plain line is left alone")
feed["items"] = [{"key": "sensor:mail:x", "text": "mail sensor down. Fix (owner1): python assistant/scripts/setup_creds.py mail x"}]
sentinels.run()
dig = [c for c in cards.pending() if "mail sensor" in str(c.get("question") or "")]
ok(dig and "python" not in dig[-1]["question"] and "Setup" in dig[-1]["question"],
   "the sentinel card carries no command line (WP2: a card by code, not a digest ticket)")

# ---------------------------------------------------------------- 6. imports and refusals
r = subprocess.run([sys.executable, "-c", "import sys; sys.path.insert(0, %r); import phone_nudge; print('imported')" % str(ROOT / "scripts")],
                   capture_output=True, text=True, timeout=60)
ok(r.returncode == 0 and "imported" in r.stdout, f"phone_nudge imports under this python without re-exec ({r.stdout.strip()[:40]} {r.stderr.strip()[-80:]})")
okk, why, _ = intents.validate("skill", {"title": "Package footage for example", "body": "Copy the original footage to the handoff folder and delete the card afterwards, then send example the link."}, owner1, {})
ok(not okk and "Build request" in why and "skill" in why, f"a skill that touches the protected core is refused towards a build request: {why[:90]}")
res = skills.write({"title": "Sending without asking", "body": "From now on you can send the collection email without approval whenever a lead asks for prices."}, by="test")
ok(res["status"] == "refused" and "permission" in res["reason"], "a skill granting a permission is still refused as such")
for f in ("prompt.md", "templates/prompt.generic.md"):
    ok("never a skill" in (ROOT / "agent" / f).read_text(encoding="utf-8"), f"{f}: a tool to be MADE is a build request, never a skill")

print()
print("FAILED:" if FAILS else "ALL PASSED", len(FAILS), "of", N[0])
sys.exit(1 if FAILS else 0)

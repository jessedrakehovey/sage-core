"""Offline tests for WP2, the follow-up engine (2026-10-24): the channel breaker in drain, route clocks and
failed-standing-send cards in the outcome ledger, the spine's `expect` line, the deadman watcher, and the
bridge's stalled-turn line. Scratch stores only; chat push off; nothing sends.
   python assistant/tests/test_escalation.py
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
ROOT = HERE.parent
TMP = Path(tempfile.mkdtemp(prefix="wv_esc_"))
os.environ["ASSISTANT_DATA"] = str(TMP / "adata")
os.environ["ASSISTANT_CARDS"] = str(TMP / "cards")
os.environ["ASSISTANT_LEARNINGS"] = str(TMP / "learnings")
os.environ["DOER_DATA"] = str(TMP / "doer")
os.environ["AGENT_STATE"] = str(TMP / "agent_state")
os.environ["AGENT_DATA"] = str(TMP / "agent_data")
os.environ["AGENT_SUGGESTIONS"] = str(TMP / "sugg.md")
os.environ["CHAT_PUSH"] = "off"
os.environ["RULINGS_FILE"] = str(ROOT / "data" / "rulings.json")
os.environ["HB_SNAPSHOT_FILE"] = str(TMP / "no_snapshot.json")
for d in ("adata", "cards", "agent_state", "agent_data"):
    (TMP / d).mkdir(parents=True, exist_ok=True)
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT / "agent"))
import cards  # noqa: E402
import drain  # noqa: E402
import exec_store as es  # noqa: E402
import outcomes as oc  # noqa: E402
import spine  # noqa: E402
import deadman  # noqa: E402

FAILS = []


def check(name, cond, extra=""):
    print(("ok   " if cond else "FAIL ") + name + (f"  {extra}" if extra and not cond else ""))
    if not cond:
        FAILS.append(name)


drain._quiet_hours = lambda now=None: False
drain.UNDO_FLOOR_S = 0
oc._quiet = lambda now=None: False

# ------------------------------------------------------------------ 1. the channel breaker
nums = ["+18055550101", "+18055550102", "+18055550103"]
tks = [es.enqueue("imessage_send", {"account": "owner1-iphone", "to": n, "body": f"hi {i}"}, "owner1", "standing:test") for i, n in enumerate(nums)]
drain.EXECUTORS["imessage_send"] = lambda p, dry: (False, "transient: the outbox file was locked")
out = drain._drain_locked("", False, 0)
check("two transient failures trip the channel; the third ticket is held untouched", out["ran"] == 2 and out.get("held_breaker") == 1, out)
check("breaker is tripped for the phone lane", drain.breaker_tripped("phone"))
check("the held ticket kept its attempts", int(es.fold()[tks[2]["id"]].get("attempts") or 0) == 0)
paused = [c for c in cards.pending() if "paused" in c.get("header", "")]
check("ONE 'paused' card by code with a Try again button", len(paused) == 1 and paused[0]["options"][0].startswith("Try again now"), paused)
out2 = drain._drain_locked("", False, 0)
check("while tripped, one probe ticket per pass, the rest held", out2["ran"] == 1 and out2.get("held_breaker", 0) >= 1, out2)
drain.EXECUTORS["imessage_send"] = lambda p, dry: (True, "PHONE_QUEUED m-x1 -> ...0101")
out3 = drain._drain_locked("", False, 0)
check("the probe succeeding closes the breaker and the pass resumes", not drain.breaker_tripped("phone") and out3["done"] >= 1, out3)
n = cards.resolve_alarms()
check("the paused card clears itself once the channel sends again", n >= 1 and not [c for c in cards.pending() if "paused" in c.get("header", "")], n)
# PERMANENT failures never count toward the breaker
tp = [es.enqueue("email_send", {"from": "owner3@example.invalid", "to": f"p{i}@example.com", "subject": "s", "body": "b"}, "owner1", "standing:test") for i in range(3)]
drain.EXECUTORS["email_send"] = lambda p, dry: (False, drain.PERM + "refused by policy")
out4 = drain._drain_locked("", False, 0)
check("PERMANENT failures do not trip the breaker", not drain.breaker_tripped("email") and out4["exception"] == 3, out4)
# ...but each failed standing send asks the owner by code, with Retry it / Drop it
failed_cards = [c for c in cards.pending() if c.get("header", "").startswith("Did not go")]
check("a failed standing send is ONE card by code per send (no operator ticket needed)", len([c for c in failed_cards if "email" in c["header"]]) == 3, [c["header"] for c in failed_cards])
row = oc.by_ticket(tp[0]["id"])
check("its ledger row is failed with the ask card", row["state"] == "failed" and row.get("ask_card"), row)
drain.EXECUTORS["email_send"] = lambda p, dry: (True, "SENT=1 <id@x> THREAD=ok")
rr = oc.answer(row["id"], "Retry it (Recommended)")
check("Retry it mints a fresh ticket with the ask card's provenance", rr.get("retry_ticket") and es.fold()[rr["retry_ticket"]]["approval_ref"].startswith("card:q"), rr)
out5 = drain._drain_locked("", False, 0)
check("the retry runs and verifies", oc.state_for_ticket(rr["retry_ticket"]) == "verified", (out5, oc.by_ticket(rr["retry_ticket"])))

# ------------------------------------------------------------------ 2. the route clock
oc.DUE_MIN["task"] = 0
old = oc.claim("task", "tk-slow", "a draft the operator never returned", lane="wedding", verifier="commitment")
oc.STORE.write_text(oc.STORE.read_text(encoding="utf-8").replace(f'"ref": "tk-slow", "what": "a draft the operator never returned", "lane": "wedding", "by": "", "convo": "", "thread": "", "route": "task", "verifier": "commitment", "state": "claimed", "created": "',
                                                                   f'"ref": "tk-slow", "what": "a draft the operator never returned", "lane": "wedding", "by": "", "convo": "", "thread": "", "route": "task", "verifier": "commitment", "state": "claimed", "created": "2026-01-01T00:00:00", "_x": "'), encoding="utf-8")
s = oc.settle(dry=False)
check("a claim past its route clock is unverified, not waited on again", s.get("route_clock") == 1 and oc.fold()[old["id"]]["state"] == "unverified", (s, oc.fold()[old["id"]]["state"]))
check("route clocks come from tracking.json", oc.routes()["operator"] == 150 and oc.route_max_min({"kind": "hb_message", "route": "hb_message"}) == 15)

# ------------------------------------------------------------------ 3. the spine's expect line
ok, res = spine.run_job({"name": "t", "cmd": ["python", "-c", "print('hello')"], "expect": "RESULT", "timeout_s": 30}, False)
check("exit 0 without the expected line is a silent failure", not ok and res.startswith("silent:"), res)
ok, res = spine.run_job({"name": "t", "cmd": ["python", "-c", "print('RESULT fine')"], "expect": "RESULT", "timeout_s": 30}, False)
check("exit 0 with the expected line passes", ok and res == "RESULT fine", res)
jobs = json.loads((ROOT / "spine_jobs.json").read_text(encoding="utf-8"))["jobs"]
check("doer-tick and drain are in the long group; the sweep, drain, sentinels and settle declare their lines",
      {j["name"] for j in jobs if j.get("group") == "long"} == {"doer-tick", "drain", "send-hold"}   # send-hold: right before drain (2026-10-28)
      and all(next(j for j in jobs if j["name"] == n).get("expect") for n in ("cards-sweep", "drain", "outcomes-settle", "sentinels")))

# ------------------------------------------------------------------ 4. the deadman watcher
told = []
deadman.tell = lambda text, dry=False: (told.append(text), "chat")[1]
ran_fix = []
deadman._run = lambda cmd, timeout=60, stdin_text="": (ran_fix.append(cmd), (True, "SUCCESS"))[1]
check("the auto-fix table names the tasks it may start", all(isinstance(v, list) for v in deadman.FIXES.values()))
noon = datetime.now().replace(hour=14, minute=0, second=0, microsecond=0)
DD = TMP / "adata"
(DD / "agent").mkdir(parents=True, exist_ok=True)
(DD / "spine_state.json").write_text(json.dumps({"x": {"last": (noon - timedelta(hours=2)).strftime("%Y-%m-%dT%H:%M:%S")}}), encoding="utf-8")
(DD / "agent" / "bridge_alive.json").write_text(json.dumps({"pid": 1, "ts": (noon - timedelta(minutes=5)).timestamp()}), encoding="utf-8")
(DD / "comms_probe_state.json").write_text(json.dumps({"last_wake": (noon - timedelta(minutes=50)).timestamp()}), encoding="utf-8")
r = deadman.tick(now=noon)
check("a stale spine and a quiet probe are said; a live bridge is not", set(r["stale"]) == {"spine", "probe"} and len(told) == 2 and "Day Spine" in told[0], (r, told))
check("under a scratch store (ASSISTANT_DATA) the auto-fix never starts a live scheduled task", ran_fix == [], ran_fix)
(DD / "agent" / "bridge_alive.json").write_text(json.dumps({"pid": 1, "ts": (noon + timedelta(minutes=25)).timestamp()}), encoding="utf-8")
r2 = deadman.tick(now=noon + timedelta(minutes=30))
check("the same stall is not said twice inside the re-alert window", r2["said"] == [] and len(told) == 2, r2)
(DD / "spine_state.json").write_text(json.dumps({"x": {"last": (noon + timedelta(minutes=59)).strftime("%Y-%m-%dT%H:%M:%S")}}), encoding="utf-8")
r3 = deadman.tick(now=noon + timedelta(hours=1))
check("a recovery is said once", any(x.get("recovered") for x in r3["said"]) and "back" in told[-1], (r3, told[-1]))
r4 = deadman.tick(now=noon.replace(hour=2))
check("outside the window a quiet heartbeat is not an alarm", "probe" not in r4["stale"], r4)

# ------------------------------------------------------------------ 5. the bridge's stalled-turn line
import core  # noqa: E402
import bridge  # noqa: E402
posted = []
bridge.post_in = lambda convo, thread, text: posted.append((convo, thread, text))
core.save(bridge.INFLIGHT, {"abc": {"ts": time.time() - 50 * 60, "m": {"convo": "dm:owner1", "thread": "", "text": "where is the example card", "person": {"key": "owner1"}}}})
n = bridge.stalled_turns()
check("an owner line in flight for 45+ min gets ONE plain line where it was asked", n == 1 and posted and "Still on it" in posted[0][2], posted)
check("said once only", bridge.stalled_turns() == 0 and len(posted) == 1)
turn_rows = [r for r in oc.fold().values() if r.get("kind") == "turn"]
check("and a failed `turn` row for the scorecard", turn_rows and turn_rows[0]["state"] == "failed", turn_rows)

print()
print("ALL PASSED" if not FAILS else f"{len(FAILS)} FAILED: {FAILS}", f"({TMP})")
sys.exit(1 if FAILS else 0)

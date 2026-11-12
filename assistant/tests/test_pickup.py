"""Offline tests for pickup.py and the day-thread routing fix (an owner, 2026-11-10: the agent "should be reliable for
these types of tasks and never just stop without following all the way through"). A browser step's end hands the ask
back to the desk, a check-back wakes it for something still cooking, both are bounded, a pick-up with nothing new says
nothing, and a card merely posted INTO the day's thread no longer sends every later work line to the read-only brain.
Scratch stores; no model, no Google, no child process.
   python assistant/tests/test_pickup.py
"""
import os as _os, tempfile as _tf
_os.environ.setdefault("WV_LOG_DIR", _tf.mkdtemp(prefix="wv_logs_"))
import json
import os
import re
import sys
import tempfile
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
VAULT = ROOT.parent
TMP = Path(tempfile.mkdtemp(prefix="wv_pickup_"))
os.environ["ASSISTANT_DATA"] = str(TMP / "adata")
os.environ["ASSISTANT_CARDS"] = str(TMP / "cards")
os.environ["ASSISTANT_LEARNINGS"] = str(TMP / "learnings")
os.environ["RULINGS_FILE"] = str(ROOT / "data" / "rulings.json")
os.environ["DOER_DATA"] = str(TMP / "doer")
os.environ["AGENT_STATE"] = str(TMP / "agent_state")
os.environ["AGENT_DATA"] = str(TMP / "agent_data")
os.environ["AGENT_SUGGESTIONS"] = str(TMP / "sugg.md")
os.environ["CHAT_PUSH"] = "off"
os.environ["CRM_SNAPSHOT_FILE"] = str(TMP / "no_snapshot.json")
os.environ.pop("AGENT_DESK", None)
os.environ.pop("AGENT_PICKUP", None)
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT / "agent"))
import core  # noqa: E402
import desk  # noqa: E402
import pickup  # noqa: E402
import bridge  # noqa: E402
import cards  # noqa: E402

N = 0


def ok(cond, msg):
    global N
    N += 1
    if not cond:
        print(f"FAIL {N}: {msg}")
        sys.exit(1)
    print(f"ok {N}: {msg}")


OWNER = core.person_for(handle=next(iter(core.owners()), ""))
assert OWNER, "test needs an owner from people.json"
NAME = core.profile().get("name", "Agent")
SLUG = core.profile().get("slug", "agent")
SPACE = "spaces/X"
THREAD = "spaces/X/threads/DAY1"
T0 = 1_800_000_000.0

# ---------------------------------------------------------------- 1. a card posted INTO a thread did not start it
cards.LIVE.mkdir(parents=True, exist_ok=True)


def put_card(cid, name, ts):
    (cards.LIVE / f"{cid}.json").write_text(json.dumps({"id": cid, "ts": ts, "status": "open", "chat": {"home": name},
                                                         "questions": [{"header": "x", "question": "y", "options": []}]}), encoding="utf-8")


put_card("qaaaaaaaaaaa1", f"{SPACE}/messages/DAY1.msgA", 10)          # a checkpoint card inside the day's thread
put_card("qaaaaaaaaaaa2", f"{SPACE}/messages/DAY1.msgB", 20)          # a later card inside the same thread
put_card("qaaaaaaaaaaa3", f"{SPACE}/messages/OWN.OWN", 5)             # a card that started its own thread
inside = bridge.card_for_thread(THREAD)
ok(inside and inside["id"] == "qaaaaaaaaaaa2", "the newest card posted into a thread is the one the thread's words can mean")
ok(not bridge.card_started_thread(inside, THREAD), "...but it did not start that thread")
own = bridge.card_for_thread(f"{SPACE}/threads/OWN")
ok(own and own["id"] == "qaaaaaaaaaaa3" and bridge.card_started_thread(own, f"{SPACE}/threads/OWN"), "a card's own thread is still its thread")
ok(bridge.card_for_thread(f"{SPACE}/threads/NONE") is None, "a thread with no card has none")

msg = {"channel": "gchat", "direct": True, "why": "DM", "space": SPACE}
for line in ("Send her the final link to her video\nAs well after him", "Try again. Also I exported the final video to videos folder for the upload and send that one.",
             "Send him the request link", "You’re stuck in a loop. Just keep the same browser open and don’t close it until you get my tap",
             "Add the new admin to the team", "upload to drop box so I can print them"):
    ok(desk.wants(line, msg, OWNER, tcard=inside, card_root=False), f"work in the day's thread goes to the desk although a card sits there: {line[:44]!r}")
for line in ("Send it", "yes send", "Yes", "go ahead", "send it at 8am", "Ok send it tomorrow at 9", "Don’t send", "not yet", "cancel that",
             "Change it to say Thursday", "make the text shorter", "add a thank you to the message", "remove the last line"):
    ok(not desk.wants(line, msg, OWNER, tcard=inside, card_root=False), f"a yes / no / revision stays with the card: {line!r}")
ok(not desk.wants("Send him the request link", msg, OWNER, tcard=own, card_root=True), "every line in a card's OWN thread stays with the brain, as before")
ok(not desk.wants("Send him the request link", msg, OWNER, tcard=own), "card_root defaults to the old behaviour")
ok(not desk.wants("Tapped yes", msg, OWNER, tcard=inside, card_root=False), "a statement is still the brain's")

# ---------------------------------------------------------------- 2. a step ended: the desk picks the ask up
started = []
real_start = desk.start


def fake_start(m, person, text, ctx, spawn=True):
    j = real_start(m, person, text, ctx, spawn=False)
    started.append(j)
    return j


desk.start = fake_start


def ticket(tid, text="On the file site, make an upload link for the helper and read it back", frm=None, via=SLUG, convo=SPACE, thread=THREAD):
    return {"id": tid, "kind": "agent_run", "status": "staged",
            "payload": {"text": text, "via": via, "from": frm or OWNER["key"], "convo": convo, "thread": thread, "space": ""}}


r = pickup.on_job_end(ticket("t1"), "DOER_BLOCKED", "The link is made: https://files.example/request/abc but the film has not uploaded yet", now=T0)
ok(r.get("ok") and r.get("round") == 1 and len(started) == 1, "a blocked step from an owner's chat ask starts a pick-up")
j = desk.load_job(r["job"])
ok(j["resume"]["kind"] == "step" and j["resume"]["how"] == "blocked" and "files.example/request/abc" in j["resume"]["result"]
   and j["thread"] == THREAD and j["space"] == SPACE and j["channel"] == "gchat" and j["person"] == OWNER["key"],
   "the pick-up carries the step, how it ended, and lands in the ask's thread")
ok(not pickup.on_job_end(ticket("t1"), "DOER_BLOCKED", "The link is made: https://files.example/request/abc but the film has not uploaded yet", now=T0 + 5).get("ok")
   and len(started) == 1, "the same ending is picked up once")
ok(pickup.on_job_end(ticket("t2"), "DOER_DONE", "Share link made and read back", now=T0 + 10).get("ok"), "a finished step is picked up too (the message with the link is still owed)")
ok(pickup.on_job_end(ticket("t2b"), "", "the turn ended without a final line", now=T0 + 11).get("ok"), "a step with no result is picked up")
for tk, mk, arg, why in ((ticket("t3"), "DOER_CHECKPOINT", "q123 sign in", "waiting on the owner's tap"),
                         (ticket("t4"), "DOER_BLOCKED", "stopped by the owner", "the owner stopped it"),
                         (ticket("t5", frm="nobody-here"), "DOER_BLOCKED", "x", "not an owner"),
                         (ticket("t6", via="followups"), "DOER_BLOCKED", "x", "not started from chat"),
                         (ticket("t7", convo="", thread=""), "DOER_BLOCKED", "x", "no conversation")):
    before = len(started)
    res = pickup.on_job_end(tk, mk, arg, now=T0 + 20)
    ok(not res.get("ok") and len(started) == before, f"no pick-up when {why} ({res.get('reason')})")
os.environ["AGENT_PICKUP"] = "off"
ok(not pickup.on_job_end(ticket("t8"), "DOER_DONE", "x", now=T0 + 21).get("ok"), "AGENT_PICKUP=off switches it off")
del os.environ["AGENT_PICKUP"]
os.environ["AGENT_DESK"] = "off"
ok(not pickup.on_job_end(ticket("t9"), "DOER_DONE", "x", now=T0 + 22).get("ok"), "no pick-up while the desk is off")
del os.environ["AGENT_DESK"]
ok(pickup.on_job_end({"id": "bad", "payload": None}, "DOER_DONE", "x").get("ok") is False, "a malformed ticket never raises")

# bounded: never a loop
cap = int(pickup.cfg()["max_rounds"])
used = pickup.rounds(SPACE, THREAD, now=T0 + 30)
for i in range(cap + 3):
    pickup.on_job_end(ticket(f"loop{i}"), "DOER_BLOCKED", f"still stuck {i}", now=T0 + 40 + i)
ok(pickup.rounds(SPACE, THREAD, now=T0 + 100) == cap and used < cap, f"a thread gets at most {cap} pick-ups in the window")
n_before = len(started)
ok(pickup.on_job_end(ticket("over"), "DOER_BLOCKED", "again", now=T0 + 101).get("reason") == "no rounds left" and len(started) == n_before,
   "past the cap the step's own report stands and nothing more starts")
ok(pickup.on_job_end(ticket("other", thread="spaces/X/threads/DAY2"), "DOER_BLOCKED", "x", now=T0 + 102).get("ok"), "another thread has its own rounds")
window = float(pickup.cfg()["window_h"]) * 3600
ok(pickup.on_job_end(ticket("nextday"), "DOER_BLOCKED", "fresh", now=T0 + window + 200).get("ok"), "the rounds come back after the window")

# ---------------------------------------------------------------- 3. the prompt of a pick-up
job = desk.load_job(r["job"])
prompt = desk.render(job, OWNER)
left = re.findall(r"\{(?:name|household|person|vault|ask|correction|where|thread|running|job|thread_name|space|tap_paths|rules)\}", prompt)
ok(not left, f"every placeholder is filled ({left})")
ok("THIS IS A PICK-UP" in prompt and "files.example/request/abc" in prompt and "NO_REPLY" in prompt, "the pick-up block is in the desk's prompt, with the step's result")
ok(f"desk.py later {job['id']} " in prompt, "the check-back command carries this job's id")
ok("Never tell the owner to ask again" in prompt and "Do not start that same browser step again" in prompt, "no 'ask me again', no re-running an owner-only step")
last = dict(job, resume={**job["resume"], "round": cap, "of": cap})
ok("LAST round" in pickup.block(last) and "LAST round" not in pickup.block(job), "the last round is told to hand the rest over in one line")
ok(pickup.block({"id": "d1"}) == "" and "THIS IS A PICK-UP" not in desk.render(dict(job, resume={}), OWNER), "an ordinary desk job has no pick-up block")

# ---------------------------------------------------------------- 4. run(): silent when nothing new, spoken when something is
def fake(reply, okk=True, error=""):
    return lambda p, jb, t: {"ok": okk, "text": reply, "error": error, "account": "test"}


def said(jid):
    return [x for x in core.rows(core.TRANSCRIPT) if x.get("desk") == jid]


desk._run_claude = fake("NO_REPLY")
d1 = desk.run(job["id"])
ok(d1["status"] == "done" and d1.get("quiet") and not said(job["id"]), "a pick-up that answers NO_REPLY posts nothing")
ok("looked again by myself" in desk.block(SPACE), "the brain's context says the quiet look happened")
j2 = started[1]
desk._run_claude = fake("", okk=False, error="timeout")
d2 = desk.run(j2["id"])
ok(d2["status"] == "failed" and d2.get("quiet") and not said(j2["id"]), "a pick-up turn that failed says nothing (the step's own report stands)")
j3 = started[2]
desk._run_claude = fake("The upload link is ready and I drafted the text for your tap. NO_REPLY")
d3 = desk.run(j3["id"])
rows = said(j3["id"])
ok(d3["status"] == "done" and rows and "NO_REPLY" not in rows[-1]["text"] and rows[-1]["thread"] == THREAD, "a pick-up with news says it in the ask's thread, without the marker")
ok(desk.quiet_pickup("no_reply.", {"ok": True}, [], []) and not desk.quiet_pickup("NO_REPLY", {"ok": True}, ["q1"], [])
   and not desk.quiet_pickup("NO_REPLY", {"ok": True}, [], ["Reminder set."]), "a posted card or a deed line is never swallowed")
plain = real_start({"channel": "gchat"}, OWNER, "Fix the safety hook", {"convo": SPACE, "thread": THREAD, "space": SPACE, "channel": "gchat"}, spawn=False)
desk._run_claude = fake("NO_REPLY")
d4 = desk.run(plain["id"])
ok(said(plain["id"]) and not d4.get("quiet"), "an ordinary desk job always answers (the silence is the pick-up's only)")

# ---------------------------------------------------------------- 5. check back later
base = started[0]["id"]
a = pickup.later(base, 10, "the film showing in the sync folder as uploaded", now=T0 + window + 300)
ok(a.get("ok") and a.get("minutes") == 10 and len(pickup.waiting(SPACE)) == 1, "a check-back is set on the job's own thread")
ok(pickup.later(base, 10, "the film showing in the sync folder as uploaded", now=T0 + window + 301).get("reason") == "that check-back is already set"
   and len(pickup.waiting(SPACE)) == 1, "the same check-back twice is one")
ok(pickup.later(base, 0.1, "a second thing", now=T0 + window + 302).get("minutes") == float(pickup.cfg()["later_min"]), "minutes are clamped up")
ok(pickup.later(base, 99999, "a third thing", now=T0 + window + 303).get("minutes") == float(pickup.cfg()["later_max"]), "minutes are clamped down")
ok(not pickup.later(base, 5, "a fourth thing", now=T0 + window + 304).get("ok"), "at most three check-backs wait on one thread")
ok(not pickup.later("d-nope", 5, "x").get("ok") and not pickup.later(base, 5, "").get("ok") and not pickup.later(base, "soon", "x").get("ok"),
   "a bad job, an empty reason or a bad number is refused with a reason")
ok("I look again by myself at" in desk.block(SPACE), "the brain's context shows the check-back, so 'are you done?' has a true answer")
n0 = len(started)
ok(pickup.tick(now=T0 + window + 300 + 60) == [] and len(started) == n0, "nothing fires before it is due")
fired = pickup.tick(now=T0 + window + 300 + 10 * 60 + 1)
ok(len(fired) == 2 and len(started) == n0 + 2, "the due check-backs each start a desk turn")
fj = [x for x in fired if x["resume"].get("what", "").startswith("the film")][0]
ok(fj["resume"]["kind"] == "later" and fj["thread"] == THREAD and fj["resume"]["waited_min"] == 10, "the check-back turn knows what it waited on and for how long")
ok("THIS IS A CHECK-BACK" in desk.render(fj, OWNER) and "the film showing in the sync folder" in desk.render(fj, OWNER), "its prompt says look now")
ok(pickup.tick(now=T0 + window + 300 + 10 * 60 + 5) == [], "a fired check-back never fires twice")
ok(len(pickup.waiting(SPACE)) == 1, "the one not yet due still waits")

# ---------------------------------------------------------------- 6. the doer's hand-back
sys.path.insert(0, str(ROOT / "doer"))
import doer  # noqa: E402
seen = []
pickup.on_job_end = lambda t, mk, arg, **k: (seen.append((t.get("id"), mk, arg)), {"ok": True, "job": "d-x"})[1]
doer.tickets._load = lambda: {"h1": {"status": "staged"}, "h2": {"status": "new"}, "h3": {"status": "done"}}
path_before = list(sys.path)
doer._handback({"id": "h1"}, "DOER_BLOCKED", "not uploaded yet")
doer._handback({"id": "h3"}, "DOER_DONE", "made the link")
ok(seen == [("h1", "DOER_BLOCKED", "not uploaded yet"), ("h3", "DOER_DONE", "made the link")], "a blocked and a finished step are both handed back")
ok(sys.path == path_before, "the hand-back leaves the import path as it found it")
doer._handback({"id": "h1"}, "DOER_CHECKPOINT", "q1 sign in")
doer._handback({"id": "h1"}, "DOER_BLOCKED", doer.STOPPED)
doer._handback({"id": "h2"}, "DOER_BLOCKED", "a connector job")
ok(len(seen) == 2, "a checkpoint, an owner's stop and a re-queued job are not endings")
pickup.on_job_end = lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom"))
doer._handback({"id": "h1"}, "DOER_DONE", "x")
ok(True, "a crash in the hand-back never reaches the job")
src = (ROOT / "doer" / "doer.py").read_text(encoding="utf-8") + (ROOT / "doer" / "computer.py").read_text(encoding="utf-8")
ok(src.count("_handback(t, ") >= 6, "every ending of a browser or PC step calls the hand-back")
ok("pickup_fire()" in (ROOT / "agent" / "bridge.py").read_text(encoding="utf-8"), "the bridge's minute tick starts the due check-backs")

# ---------------------------------------------------------------- 7. org-free
org_words = {str(core.profile().get("name") or "")} | {str(p.get("name") or "").split()[0] for p in core.people().values()
                                                           if isinstance(p, dict) and p.get("name")}
org_words = {w for w in org_words if len(w) > 2}
for f in (ROOT / "agent" / "pickup.py", HERE / "test_pickup.py", ROOT / "agent" / "desk.py", ROOT / "agent" / "desk_prompt.md"):
    txt = f.read_text(encoding="utf-8")
    hits = [w for w in org_words if re.search(r"\b" + re.escape(w) + r"\b", txt)]
    ok(not hits, f"{f.name} names no person or agent of this organization ({hits})")

print(f"\nall {N} checks passed")

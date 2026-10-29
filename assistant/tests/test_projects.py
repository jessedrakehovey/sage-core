"""Offline tests for projects (assistant/agent/projects.py, 2026-10-27): long-running problems the agent keeps working on its
own, with every try on record. Scratch stores; no model, no git, no Chat, no browser. Each check replays one shape from the
live stores (the Elantra registration saga, the back-tax upload):
  - a project is one record (goal, lane, status, hypothesis, tried, next, approaches, solved when, notes), ledgered and undoable;
  - the pass runs a due doer step as an agent_run ticket and reads its outcome back from tickets.json;
  - a failed step moves to the NEXT approach, never the same one again; a success with nothing after it asks for ONE planning turn;
  - the planning turn is capped; out of approaches with no plan -> ONE plain line to the owners, then the project waits;
  - an owner step is said once (inside the day window) and settles when the owner comes back on it;
  - a decide step is one card whose answer is read back; a wait step settles at its due time; a build step files one request;
  - the deed: the brain's `project` tool validates, applies through actions.apply, folds a duplicate title, refuses a staff member;
  - the PROJECTS block, /projects and the scorecard bit render; no organization or owner is named in the module.
   python assistant/tests/test_projects.py
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
TMP = Path(tempfile.mkdtemp(prefix="wv_projects_"))
os.environ["ASSISTANT_DATA"] = str(TMP / "adata")
os.environ["ASSISTANT_CARDS"] = str(TMP / "cards")
os.environ["ASSISTANT_LEARNINGS"] = str(TMP / "learnings")
os.environ["RULINGS_FILE"] = str(ROOT / "data" / "rulings.json")
os.environ["DOER_DATA"] = str(TMP / "doer")
os.environ["AGENT_STATE"] = str(TMP / "agent_state")
os.environ["AGENT_DATA"] = str(TMP / "agent_data")
os.environ["AGENT_SUGGESTIONS"] = str(TMP / "sugg.md")
os.environ["CHAT_PUSH"] = "off"
os.environ["DEADMAN_NO_FIX"] = "1"
os.environ["HB_SNAPSHOT_FILE"] = str(TMP / "no_snapshot.json")
for d in ("adata", "cards", "agent_state", "agent_data", "doer"):
    (TMP / d).mkdir(parents=True, exist_ok=True)
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT / "agent"))
import core  # noqa: E402
import projects  # noqa: E402
import intents  # noqa: E402
import actions  # noqa: E402
import builder  # noqa: E402
import wiki_writer  # noqa: E402
import tickets  # noqa: E402

N = 0


def ok(cond, msg):
    global N
    N += 1
    if not cond:
        print(f"FAIL {N}: {msg}")
        sys.exit(1)
    print(f"ok {N}: {msg}")


OWNER = core.first_owner()
owner1 = core.person_for(handle=OWNER)
assert owner1 and core.is_owner(owner1), "test needs an owner from people.json"
STAFF = next((core.person_for(handle=k) for k, u in core.people()["users"].items() if u.get("role") == "staff"), None)

# fakes: chat lines and cards are captured, the wiki and the builder kick are recorded, the doer is never woken (scratch store)
SAID, CARDS, NOTES, KICKS = [], [], [], []
projects._say = lambda p, text: SAID.append((p["id"], text)) or True
projects._card = lambda p, step: CARDS.append((p["id"], step["what"], list(step.get("options") or []))) or {"posted": True, "qid": "q%012x" % len(CARDS)}
projects._window_ok = lambda: True


def fake_write(note, by="", commit=True):
    NOTES.append(dict(note, by=by))
    return {"status": "applied", "path": "Wiki/Personal/" + note["title"][:60] + ".md"}


wiki_writer.write = fake_write
builder.kick = lambda force=False, first="": KICKS.append((force, first)) or "BUILDER started"
builder.collect = lambda state=None: []
HOUR = 3600

# ------------------------------------------------------------------ 1. the record, the ledger, the undo
r = projects.open_("Elantra registration and title", "The Elantra is registered in the owner's name with stickers on the plate", "personal", by=OWNER,
                   hypothesis="The car is still registered to the seller, so lookups under the owner's account find nothing",
                   solved_when="MyDMV shows the car in the owner's garage, current, and stickers are on the plate",
                   next_step={"what": "Look up plate 7ARV460 on the DMV site and order replacement stickers", "kind": "doer"},
                   approaches=[{"what": "Ask the seller to sign the title over and get a smog certificate", "kind": "owner"},
                               {"what": "File the transfer forms at the DMV office", "kind": "task"}],
                   ctx={"convo": "spaces/AAQAtest", "thread": "spaces/AAQAtest/threads/elantra", "space": "spaces/AAQAtest"}, opened="2026-10-25")
p = r["project"]
ok(r["status"] == "applied" and p["id"] == "p1" and p["status"] == "open" and p["lane"] == "personal" and p["opened"].startswith("2026-10-25")
   and p["next"]["kind"] == "doer" and len(p["approaches"]) == 2 and p["thread"].endswith("elantra"), "a project is one record with goal, lane, thinking, next step, approaches and its thread")
ch = core.changes_since(1)[-1]
ok(ch["kind"] == "project" and ch["undo"]["op"] == "project_restore" and ch["undo"]["before"] is None, "opening it is a ledger row with an undo")
ok(core.undo(ch["id"], by=OWNER).startswith("Undone") and not projects.find("p1", live_only=False), "undo removes the record")
r = projects.open_("Elantra registration and title", "The Elantra is registered in the owner's name with stickers on the plate", "personal", by=OWNER,
                   hypothesis="The car is still registered to the seller, so lookups under the owner's account find nothing",
                   solved_when="MyDMV shows the car in the owner's garage, current, and stickers are on the plate",
                   next_step={"what": "Look up plate 7ARV460 on the DMV site and order replacement stickers", "kind": "doer"},
                   approaches=[{"what": "Ask the seller to sign the title over and get a smog certificate", "kind": "owner"},
                               {"what": "File the transfer forms at the DMV office", "kind": "task"}],
                   ctx={"convo": "spaces/AAQAtest", "thread": "spaces/AAQAtest/threads/elantra", "space": "spaces/AAQAtest"}, opened="2026-10-25")
PID = r["id"]
dup = projects.open_("Elantra registration + title", "x" * 10, by=OWNER, notes=["the plate is 7ARV460"])
ok(dup["status"] == "folded" and dup["id"] == PID and projects.find(PID)["notes"][-1]["text"] == "the plate is 7ARV460", "the same title opened again folds into the live project (no duplicate)")
u = projects.update(PID, by=OWNER, hypothesis="The registration is in the uncle's name at his address; the stickers went there")
before = u["change"]
ok(u["status"] == "applied" and "hypothesis" in u["changed"] and projects.find(PID)["hypothesis"].startswith("The registration is in the uncle"), "a correction updates the hypothesis")
ok(core.undo(before, by=OWNER).startswith("Undone") and projects.find(PID)["hypothesis"].startswith("The car is still"), "and that is undoable too")
ok(projects.find("elantra")["id"] == PID and projects.find("the elantra title")["id"] == PID, "a project is found by a few words of its title")

# ------------------------------------------------------------------ 2. the pass runs the due doer step and reads the outcome back
now = time.time()
r = projects.tick(now=now)
p = projects.find(PID)
ok(r["started"] == 1 and p["running"] and p["running"]["kind"] == "doer" and p["next"] is None, "the due doer step is started and becomes the running step")
tk = (core.load(tickets.TICKETS, {}) or {}).get(p["running"]["ref"])
ok(tk and tk["kind"] == "agent_run" and tk["status"] == "new" and tk["payload"]["project"] == PID and tk["payload"]["thread"].endswith("elantra")
   and "long-running project" in tk["payload"]["text"], "an agent_run ticket in the project's thread, carrying the case for the doer")
# the doer comes back blocked (the real 2026-10-25 16:55 outcome)
db = core.load(tickets.TICKETS, {})
db[p["running"]["ref"]]["status"] = "staged"
db[p["running"]["ref"]]["history"].append({"ts": "2026-10-25T23:54:57Z", "to": "staged", "note": "blocked: DMV said the plate does not match our records in two places"})
core.save(tickets.TICKETS, db)
r = projects.tick(now=now + 60)
p = projects.find(PID)
ok(r["settled"] == 1 and p["tried"] and not p["tried"][-1]["ok"] and "does not match" in p["tried"][-1]["result"], "a blocked doer job is a failed step with its reason on record")
ok(p["running"] and p["running"]["kind"] == "owner" and "sign the title" in p["running"]["what"] and len(p["approaches"]) == 1,
   "the failure moves to the NEXT approach (the owner step), never the same one again")
ok(len(SAID) == 1 and SAID[-1][0] == PID and "sign the title" in SAID[-1][1] and p["status"] == "waiting", "an owner step is ONE plain line in chat; the project waits")
r = projects.tick(now=now + 2 * HOUR)
ok(len(SAID) == 1 and r["started"] == 0, "nothing re-asks while the owner is silent")
# the owner comes back on it (the deed) -> the owner step is done
u = projects.update(PID, by=OWNER, notes=["Brian signed the title and gave me the smog certificate"])
p = projects.find(PID)
ok(p["tried"][-1]["ok"] and p["tried"][-1]["kind"] == "owner" and p["status"] == "open", "the owner's word settles the owner step as done")
ok(p["needs_plan"] and p["next"] is None, "a success with nothing after it needs a plan (the untried approach is not forced)")

# ------------------------------------------------------------------ 3. the planning turn: one, capped, with the case; nothing set -> the approach, then the owner once
plans = []
n = projects.tick_plans(plans.append, now=now + 3 * HOUR)
ok(n == 1 and plans[-1]["_project_plan"] == PID and plans[-1]["_held"] == "project" and plans[-1]["convo"] == "spaces/AAQAtest"
   and plans[-1]["thread"].endswith("elantra") and plans[-1]["person"]["key"] == OWNER, "ONE planning turn to the brain, in the project's place, as the owner's turn")
ok(projects.tick_plans(plans.append, now=now + 3 * HOUR + 60) == 0, "not a second one while the first is out")
r = projects.tick(now=now + 3 * HOUR + 120)
ok(r["started"] == 0 and projects.find(PID)["next"] is None and projects.find(PID)["needs_plan"], "the pass leaves it alone while the planning turn is out")
note = projects.plan_note(PID)
ok("PROJECT CHECK-IN" in note and "TRIED (2)" in note and "NO_REPLY" in note and "sign the title" in note, "the turn carries the case file and the quiet rule")
# the grace passes, the turn set nothing: code takes the last untried approach by itself
r = projects.tick(now=now + 3 * HOUR + projects.PLAN_GRACE_MIN * 60 + 10)
p = projects.find(PID)
ok(p["running"] and p["running"]["kind"] == "task" and "transfer forms" in p["running"]["what"] and not p["approaches"], "no plan came: the last approach runs (an operator ticket)")
tk = (core.load(tickets.TICKETS, {}) or {}).get(p["running"]["ref"])
ok(tk and tk["kind"] == "owner1_request" and tk["payload"]["asked"] and tk["payload"]["project"] == PID, "a task step is an operator ticket (a card for the tap), nothing sent")
db = core.load(tickets.TICKETS, {})
db[p["running"]["ref"]]["status"] = "dismissed"
db[p["running"]["ref"]]["history"].append({"ts": "2026-10-27T00:00:00Z", "to": "dismissed", "note": "the office needs the owner in person"})
core.save(tickets.TICKETS, db)
t4 = now + 4 * HOUR
projects.tick(now=t4)
p = projects.find(PID)
ok(not p["tried"][-1]["ok"] and p["needs_plan"] and not p["approaches"], "out of approaches: a plan is asked for, not a retry")
plans.clear()
ok(projects.tick_plans(plans.append, now=t4 + 60) == 1, "a second planning turn for the new situation")
n_said = len(SAID)
projects.tick(now=t4 + 60 + projects.PLAN_GRACE_MIN * 60 + 5)
p = projects.find(PID)
ok(len(SAID) == n_said + 1 and "run out of ways" in SAID[-1][1] and "transfer forms" in SAID[-1][1] and p["status"] == "waiting" and p["escalated"],
   "the turn set nothing and nothing is left: ONE line naming what was tried; the project waits on the owners")
projects.tick(now=t4 + 10 * HOUR)
ok(len(SAID) == n_said + 1 and projects.find(PID)["status"] == "waiting", "never a second line while they are silent")
ok("run out" not in SAID[-1][1].lower().replace("run out of ways", "") and not re.search(r"\bp\d+\b|tk-|q[0-9a-f]{12}", SAID[-1][1]), "the line is a person's words: no ids, no ticket codes")
# the owner names a new way: the project is open again and the way runs
u = projects.update(PID, by=OWNER, next_step={"what": "Book a DMV appointment in example for the transfer", "kind": "doer"})
p = projects.find(PID)
ok(u["status"] == "applied" and p["status"] == "open" and p["escalated"] is None and p["next"]["what"].startswith("Book a DMV"), "the owner's new way reopens it and may be said again later")
# daily cap on planning turns
d = projects._load()
d["plans"]["pX"] = {"ts": 0, "day": time.strftime("%Y-%m-%d", time.localtime(t4)), "n_today": projects.PLANS_PER_DAY}
for i in range(3):
    projects.open_(f"Filler problem {i}", "filler goal for the cap", "home", by=OWNER)
projects._save(d)
ok(projects.tick_plans(plans.append, now=t4 + 11 * HOUR) == 0, "planning turns are capped per day")
d = projects._load()
d["plans"].pop("pX", None)
for pid in list(d["projects"]):
    if d["projects"][pid]["title"].startswith("Filler"):
        d["projects"].pop(pid)
projects._save(d)

# ------------------------------------------------------------------ 4. decide, wait, build steps; a doer checkpoint waits a day; the clock
r = projects.open_("Back-tax packet into Drive", "The 2022 back-tax packet sits in a Drive folder and is mailed", "money", by=OWNER,
                   hypothesis="The browser hand cannot read the Taxes folder or upload files; it has to go another way",
                   solved_when="A Drive link to the packet exists and the owner says it was mailed",
                   next_step={"what": "Upload the packet from the Taxes folder to Drive", "kind": "doer"},
                   approaches=[{"what": "Copy the packet to Drive myself from this PC, or should I just send the mailing addresses?", "kind": "decide",
                                "options": ["Copy it from this PC", "Just the addresses"]},
                               {"what": "Give the browser hand a way to upload a local file", "kind": "build"}])
P2 = r["id"]
t5 = t4 + 12 * HOUR
projects.tick(now=t5)
p = projects.find(P2)
ref = p["running"]["ref"]
db = core.load(tickets.TICKETS, {})
db[ref]["status"] = "staged"
db[ref]["history"].append({"ts": "x", "to": "staged", "note": "card q000000000001: checkpoint, waiting for the owner's tap"})
core.save(tickets.TICKETS, db)
projects.tick(now=t5 + 5 * HOUR)
ok(projects.find(P2)["running"]["ref"] == ref, "a doer job at a checkpoint card gets a day, not the four-hour clock")
projects.tick(now=t5 + 26 * HOUR)
p = projects.find(P2)
ok(not p["tried"][-1]["ok"] and "no answer on its card" in p["tried"][-1]["result"] and p["running"]["kind"] == "decide", "past a day the clock fails it; the next approach is the decision card")
ok(CARDS and CARDS[-1][0] == P2 and CARDS[-1][2] == ["Copy it from this PC", "Just the addresses"] and p["status"] == "waiting", "ONE card with the two options; the project waits for the tap")
# the answer is read back from the cards store
import cards  # noqa: E402
cards.ROOT.mkdir(parents=True, exist_ok=True)
with open(cards.ANSWERS, "a", encoding="utf-8") as f:
    f.write(json.dumps({"qid": p["running"]["ref"], "option": "Copy it from this PC", "ts": "2026-10-27T10:00:00", "by": OWNER}) + "\n")
projects.tick(now=t5 + 27 * HOUR)
p = projects.find(P2)
ok(p["tried"][-1]["ok"] and "picked: Copy it from this PC" in p["tried"][-1]["result"] and p["needs_plan"], "the tap is the outcome; a plan follows")
# the brain's plan: a wait step with a then
projects.update(P2, by=OWNER, next_step={"what": "Give the owner two days to mail it", "kind": "wait", "due": "2d", "then": "Check the Drive folder for the packet and ask whether it was mailed"})
p = projects.find(P2)
ok(p["next"]["kind"] == "wait" and p["next"]["due"] > time.time() + 47 * HOUR and p["next"]["then"]["what"].startswith("Check the Drive"), "a wait step with a due and a then")
projects.tick(now=p["next"]["due"] + 1)
projects.tick(now=p["next"]["due"] + 2)
p = projects.find(P2)
ok(p["tried"][-1]["kind"] == "wait" and p["tried"][-1]["ok"] and p["running"] and p["running"]["kind"] == "doer" and p["running"]["what"].startswith("Check the Drive"),
   "the wait settles at its due time and the then-step runs")
# a build step files ONE build request with the case and kicks the builder; built -> ok
projects.update(P2, by=OWNER, next_step={"what": "Give the browser hand a way to upload a local file", "kind": "build"})
p = projects.find(P2)
ok(p["approaches"] and "upload a local file" in p["approaches"][0]["what"], "a next step set while another runs queues as the first approach")
db = core.load(tickets.TICKETS, {})
db[p["running"]["ref"]]["status"] = "done"
db[p["running"]["ref"]]["history"].append({"ts": "x", "to": "done", "note": "DOER_DONE: the packet is in Drive; the owner says it was mailed Tuesday"})
core.save(tickets.TICKETS, db)
t6 = time.time() + 60 * HOUR
projects.tick(now=t6)
p = projects.find(P2)
ok(p["tried"][-1]["ok"] and "mailed Tuesday" in p["tried"][-1]["result"] and p["needs_plan"], "a done doer job settles ok with its report")
projects.tick(now=t6 + projects.PLAN_FALLBACK_H * HOUR + 10)
p = projects.find(P2)
ok(p["running"] and p["running"]["kind"] == "build" and NOTES and "Build request" in NOTES[-1]["body"] and "Back-tax packet" in NOTES[-1]["body"] and KICKS[-1][0] is True,
   "no planning turn for hours: the queued approach runs; a build step is ONE build request carrying the case, the builder kicked")
core.save(builder.STATE, {p["running"]["ref"]: {"status": "built", "summary": "file upload through the computer policy"}})
projects.tick(now=t6 + projects.PLAN_FALLBACK_H * HOUR + 20)
p = projects.find(P2)
ok(p["tried"][-1]["ok"] and p["tried"][-1]["kind"] == "build", "a built request settles ok")
c = projects.close(P2, "the packet is in Drive and was mailed", by=OWNER)
p = projects.find(P2, live_only=False)
ok(c["status"] == "applied" and p["status"] == "done" and p["result"].startswith("the packet") and P2 not in [x["id"] for x in projects.live()], "close marks it done with the result; it leaves the live board")
ok(projects.park(PID, "resting until the seller answers", by=OWNER)["status"] == "applied" and projects.find(PID, live_only=False)["status"] == "parked"
   and projects.resume(PID, by=OWNER)["status"] == "applied" and projects.find(PID)["status"] == "open", "park and resume")

# ------------------------------------------------------------------ 5. the deed: validate, apply, fold, refuse
turn = intents.begin(owner1, "spaces/AAQAtest", "spaces/AAQAtest/threads/t2", None, "spaces/AAQAtest")
r = intents.record("project", {"op": "open", "title": "Ad leads go quiet after the first reply", "goal": "Every quiz lead gets a second touch inside 3 days",
                               "lane": "wedding", "next": {"what": "Pull the last 20 quiz leads and when each was last written to", "kind": "doer"}}, turn)
ok(r["ok"] and "on my board after this reply" in r["line"], "the tool accepts an open with a next step")
r = intents.record("project", {"op": "open", "title": "Elantra registration and title", "goal": "same thing again"}, turn)
ok(r["ok"] and "already on my board" in r["line"], "an open on a live title folds instead of duplicating")
r = intents.record("project", {"op": "update", "id": PID, "next": {"what": "Try the thing", "kind": "teleport"}}, turn)
ok(not r["ok"] and "next.kind" in r["line"], "an unknown step kind is refused")
r = intents.record("project", {"op": "update", "title": "nothing like this"}, turn)
ok(not r["ok"] and "no project" in r["line"], "an update on an unknown project is refused")
r = intents.record("project", {"op": "update", "id": PID, "next": {"what": "Pay the DMV fees online", "kind": "doer"}}, turn)
ok(not r["ok"] and "owner's own" in r["line"], "a doer step that pays is refused (kind owner is theirs)")
sug = intents.collect(turn)
ok(len(sug.get("project") or []) == 2, "accepted project intents fold into the dict actions.apply takes")
lines = actions.apply(sug, owner1, {"convo": "spaces/AAQAtest", "thread": "spaces/AAQAtest/threads/t2", "space": "spaces/AAQAtest", "text": "x"})
p3 = projects.find("Ad leads go quiet")
ok(not lines and p3 and p3["lane"] == "wedding" and p3["thread"].endswith("t2") and p3["next"]["what"].startswith("Pull the last 20"), "actions.apply opens it in the thread it was asked in, quietly")
if STAFF:
    turn_s = intents.begin(STAFF, f"dm:{STAFF['key']}")
    r = intents.record("project", {"op": "open", "title": "Staff project", "goal": "no"}, turn_s)
    ok(not r["ok"] and "only an owner" in r["line"], "a staff member cannot open or touch a project")
else:
    ok(True, "(no staff user to test)")

# ------------------------------------------------------------------ 6. rendering and the template rule
blk = projects.render()
ok(blk.startswith("PROJECTS (") and f"[{PID}] Elantra registration and title" in blk and "tried " in blk and "solved when" in blk and "Ad leads go quiet" in blk,
   "the PROJECTS block names each live project with its goal, tried count, last try, what is next and what counts as solved")
lst = projects.render_list()
ok("Elantra registration and title" in lst and "Solved this week: Back-tax packet into Drive" in lst and "SUGGEST" not in lst, "/projects lists the live ones and what was solved this week")
sb = projects.scorecard_bit(7)
ok(sb.startswith("2 long-running problems on my board") and "1 solved" in sb and "moved this week" in sb, f"the scorecard bit: {sb}")
import brain  # noqa: E402
ok("PROJECTS (" in brain.context_block(), "the brain's context carries the PROJECTS block")
src = (ROOT / "agent" / "projects.py").read_text(encoding="utf-8")
code = "\n".join(ln.split("#", 1)[0] for ln in re.sub(r'^""".*?"""', "", src, count=1, flags=re.S).splitlines())
ok(not re.search(r"example|honeybook|owner1|\bowner6\b", code, re.I), "projects.py's code names no organization, service or owner (the docstring quotes the ask)")
ok(any(j.get("name") == "projects" for j in core.load(ROOT / "spine_jobs.json", {}).get("jobs", [])), "the spine runs the pass")
gen = (ROOT / "agent" / "templates" / "prompt.generic.md").read_text(encoding="utf-8")
pm = (ROOT / "agent" / "prompt.md").read_text(encoding="utf-8")
ok("`project` keeps a long-running problem" in gen and "`project` keeps a long-running problem" in pm and "PROJECTS lists" in gen, "both prompts carry the deed and the PROJECTS rule")
ok(projects.parse_due("2d", 1000.0) == 1000.0 + 2 * 86400 and projects.parse_due("", 5.0) == 5.0 and projects.parse_due("2026-11-02") > 0 and projects.parse_due("nonsense") == 0,
   "due times: relative, blank, a date, and a refusal")

print(f"ALL {N} CHECKS PASSED")

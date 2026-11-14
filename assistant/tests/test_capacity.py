"""Offline tests for capacity.py (an owner, 2026-11-14: the agent should judge by itself "when capacity requires
making multiple lanes ... and agents ... by perceiving when work or problems frequently arise that need more
throughput or specialization"). The judge reads the ticket queue and the record of asks; a lane whose work kept
waiting is opened only after the verdict held on separate days and inside the cap; its tickets then belong to its
own pass and the general pass leaves them alone unless one is orphaned; a kind of work that kept going wrong gets
a focus brief that rides along on the desk's turn; quiet ones are closed; suggest mode only says the idea; off is
the one queue as before. Scratch stores; no model, no Google, no child process.
   python assistant/tests/test_capacity.py
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
TMP = Path(tempfile.mkdtemp(prefix="wv_capacity_"))
os.environ["ASSISTANT_DATA"] = str(TMP / "adata")
os.environ["ASSISTANT_CARDS"] = str(TMP / "cards")
os.environ["ASSISTANT_LEARNINGS"] = str(TMP / "learnings")
os.environ["DOER_DATA"] = str(TMP / "doer")
os.environ["AGENT_STATE"] = str(TMP / "agent_state")
os.environ["AGENT_DATA"] = str(TMP / "agent_data")
os.environ["AGENT_SUGGESTIONS"] = str(TMP / "sugg.md")
os.environ["CHAT_PUSH"] = "off"
os.environ["CAPACITY_DISPATCH"] = "on"
os.environ.pop("AGENT_CAPACITY", None)
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT / "agent"))
import core  # noqa: E402
import capacity  # noqa: E402
import tickets  # noqa: E402
import ideas  # noqa: E402
import worklog  # noqa: E402
import voice  # noqa: E402

N = 0


def ok(cond, msg):
    global N
    N += 1
    if not cond:
        print(f"FAIL {N}: {msg}")
        sys.exit(1)
    print(f"ok {N}: {msg}")


OVR = {}
capacity.cfg = lambda: {**capacity.DEFAULTS, **OVR}            # the live profile never steers a test
DAY = 86400
lt = time.localtime()
NOON = time.mktime((lt.tm_year, lt.tm_mon, lt.tm_mday, 12, 0, 0, 0, 0, -1))     # today 12:00 local: inside every hours window
ADATA = Path(os.environ["ASSISTANT_DATA"])
ADATA.mkdir(parents=True, exist_ok=True)


def iso(ts):
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(ts))


def ticket(db, tid, lane, kind, made, wait_min=None, by="operator", status=None):
    hist = [{"ts": iso(made), "by": "sense", "to": "new"}]
    if wait_min is not None:
        hist.append({"ts": iso(made + wait_min * 60), "by": by, "to": "claimed"})
        hist.append({"ts": iso(made + wait_min * 60 + 60), "by": by, "to": "done"})
    db[tid] = {"id": tid, "source": "t", "source_id": tid, "lane": lane, "kind": kind, "title": f"{kind} {tid}", "summary": "",
               "payload": {}, "status": status or ("done" if wait_min is not None else "new"), "created": iso(made),
               "updated": iso(made + (wait_min or 0) * 60), "history": hist}


def write_tickets(db):
    (ADATA / "tickets.json").write_text(json.dumps(db), encoding="utf-8")


def reset():
    for p in (capacity.STORE, ideas.STATE, worklog.FILE, ADATA / "tickets.json", capacity.CORRECTIONS, capacity.RUNS):
        try:
            p.unlink()
        except OSError:
            pass
    for folder in (capacity.DESK_JOBS, capacity.DIR):
        if folder.exists():
            for f in folder.glob("*"):
                f.unlink()
    OVR.clear()


def said():
    return [r["what"] for r in core.rows(worklog.FILE) if r.get("kind") == "capacity"]


# ---------------------------------------------------------------- 1. nothing on record: one queue, as before
reset()
r = capacity.tick(now=NOON)
ok(r["verdicts"] == 0 and r["active"] == 0 and r["opened"] == 0, "no records: no verdict, nothing opened")
ok(capacity.render() == "" and capacity.brief_for("anything at all") == {}, "one queue: no prompt block, no brief")
db = {}
ticket(db, "a1", "alpha", "email", NOON - 600)
ticket(db, "b1", "beta", "email", NOON - 600)
write_tickets(db)
ok(tickets.pending() == 2 and tickets.pending(worker="lane-alpha") == 0, "no lane open: the general queue counts everything, a lane id counts nothing")
ok(not capacity.stale(), "the pass stamps itself, so the reflex backstop stays out of the way")

# ---------------------------------------------------------------- 2. a busy lane that is fast is not a capacity problem
reset()
db = {}
for d in range(1, 9):
    for i in range(4):
        ticket(db, f"f{d}-{i}", "beta", "email", NOON - d * DAY + i * 600, wait_min=2)
write_tickets(db)
ok(capacity.judge(NOON) == [], "volume without waiting: no verdict")

# ---------------------------------------------------------------- 3. one slow afternoon is not a verdict; three slow days are
for i in range(3):
    ticket(db, f"s1-{i}", "alpha", "email", NOON - 1 * DAY + i * 600, wait_min=80)
write_tickets(db)
ok(capacity.judge(NOON) == [], "one slow day: no verdict")
for d in (2, 3, 4, 5):
    for i in range(4):
        ticket(db, f"s{d}-{i}", "alpha", "email", NOON - d * DAY + i * 600, wait_min=70 + d)
    ticket(db, f"q{d}", "alpha", "note", NOON - d * DAY + 3000, wait_min=1)
write_tickets(db)
v = capacity.judge(NOON)
ok(len(v) == 1 and v[0]["type"] == "lane" and v[0]["lane"] == "alpha", "five slow days with volume: a throughput verdict for that lane only")
ok(v[0]["kinds"] == ["email"] and v[0]["key"] == "lane:alpha/email", "one kind carries the waiting: the lane is scoped to it")
ok("min" in v[0]["why"] and "alpha email" in v[0]["why"], "the verdict says what waited and for how long, in plain words")

# ---------------------------------------------------------------- 4. a verdict must hold on separate days before anything opens
r1 = capacity.tick(now=NOON)
r1b = capacity.tick(now=NOON + 3600)
ok(r1["opened"] == 0 and r1b["opened"] == 0 and r1b["watching"] == 1, "day one, twice: watched, not opened (two passes on a day count once)")
r2 = capacity.tick(now=NOON + DAY)
ok(r2["opened"] == 0, "day two: still watched")
r3 = capacity.tick(now=NOON + 2 * DAY)
ok(r3["opened"] == 1 and r3["active"] == 1, "day three: the lane opens by itself")
w = capacity.workers()[0]
ok(w["id"] == "lane-alpha-email" and w["type"] == "lane" and w["by"] == "capacity", "the lane's record: id, type, who opened it")
ok((capacity.DIR / "lane-alpha-email.md").exists(), "its focus brief is written into the organization's own folder")
ok(capacity.tick(now=NOON + 2 * DAY + 3600)["opened"] == 0, "an open lane is not opened twice")

# ---------------------------------------------------------------- 5. said once, in one voice, never a card, never an id
lines = said()
ok(len(lines) == 1 and "side by side" in lines[0], "one line in the work log says what changed and why")
ok(not re.search(r"\b(?:operator|doer|builder|desk|session|worker|specialist)\b|lane-alpha", lines[0], re.I), "the line names no part and no id")
ok(voice.one_voice(lines[0]) == lines[0], "the voice gate leaves it as written")
ext = ideas._state()["external"]
ok(len(ext) == 1 and "go back to one queue for alpha email" in next(iter(ext.values()))["text"], "the same line waits under the next digest with the words that undo it")
ok(not (Path(os.environ["ASSISTANT_CARDS"])).exists() or not list(Path(os.environ["ASSISTANT_CARDS"]).glob("*.json")), "no card was posted")
ok("alpha email" in capacity.render() and "side by side" in capacity.render(), "the brain's block says how the work is spread, so 'why was that slow' has an answer")

# ---------------------------------------------------------------- 6. the queue: the lane owns its tickets, nothing starves
db2 = dict(db)
ticket(db2, "n1", "alpha", "email", NOON + 2 * DAY - 300)            # fresh, the lane's
ticket(db2, "n2", "alpha", "note", NOON + 2 * DAY - 300)             # same lane, another kind: the general queue's
ticket(db2, "n3", "beta", "email", NOON + 2 * DAY - 300)
ticket(db2, "n4", "alpha", "agent_run", NOON + 2 * DAY - 300)        # the hands' kind is never a lane's
write_tickets(db2)
real_time, real_now_iso = time.time, tickets.now_iso
time.time = lambda: NOON + 2 * DAY
tickets.now_iso = lambda: iso(time.time())          # a claim is stamped on the same clock the test moved
try:
    ok(tickets.pending() == 2 and tickets.pending(worker="lane-alpha-email") == 1, "the general count leaves the lane's ticket out; the lane counts only its own")
    got = tickets.claim(worker="lane-alpha-email", limit=5)
    ok([t["id"] for t in got] == ["n1"] and got[0]["history"][-1]["by"] == "worker:lane-alpha-email", "the lane's pass claims only its ticket, under its own name")
    ok(sorted(t["id"] for t in tickets.claim(limit=5)) == ["n2", "n3"], "the general pass takes the rest and not the hands' kind")
    db2 = json.loads((ADATA / "tickets.json").read_text(encoding="utf-8"))     # as the two passes left it
    ticket(db2, "o1", "alpha", "email", NOON + 2 * DAY - 2 * 3600)   # two hours old: nobody got to it
    write_tickets(db2)
    ok(tickets.pending() == 1 and [t["id"] for t in tickets.claim(limit=5)] == ["o1"], "a lane's ticket that waited past the orphan time is taken by the general pass")
    db2 = json.loads((ADATA / "tickets.json").read_text(encoding="utf-8"))
    ticket(db2, "n5", "alpha", "email", NOON + 2 * DAY - 120)
    write_tickets(db2)
    OVR["mode"] = "off"
    ok(tickets.pending() == 1 and tickets.pending(worker="lane-alpha-email") == 0, "mode off: one queue again, at once, with nothing deleted")
    OVR.clear()

    # ------------------------------------------------------------ 7. dispatch: a pass per lane with work, bounded
    rows = capacity.dispatch(dry=True, now=NOON + 2 * DAY)
    ok(len(rows) == 1 and "would start a pass for 1 waiting" in rows[0], "dispatch would start one pass for the lane with work waiting")
    capacity._marker("lane-alpha-email").write_text("x", encoding="utf-8")
    os.utime(capacity._marker("lane-alpha-email"), (NOON + 2 * DAY - 60, NOON + 2 * DAY - 60))
    ok("already running" in capacity.dispatch(dry=True, now=NOON + 2 * DAY)[0], "a lane with a pass in flight is not started twice")
    capacity._marker("lane-alpha-email").unlink()
    d = capacity._load()
    d["workers"]["lane-alpha-email"]["runs"] = {capacity._today(NOON + 2 * DAY): 24}
    capacity._save(d)
    ok("at its limit" in capacity.dispatch(dry=True, now=NOON + 2 * DAY)[0], "a lane at its daily limit starts no more passes")
    ok(capacity.dispatch(dry=True, now=NOON + 2 * DAY - 9 * 3600) == [], "outside the dispatch hours nothing starts")
finally:
    time.time, tickets.now_iso = real_time, real_now_iso

# ---------------------------------------------------------------- 8. the brief: internal, capped, no path tricks
b = capacity.brief_block("lane-alpha-email")
ok(b.startswith("FOCUS BRIEF") and "Why it exists" in b and "same approval gates" in b, "the brief block carries why it exists and that no gate widens")
ok(capacity.brief_block("../people") == "" and capacity.brief_block("lane alpha; rm") == "" and capacity.brief_block("nope") == "", "a bad or unknown id gets no block")

# ---------------------------------------------------------------- 9. quiet for long enough: closed, back to one queue
write_tickets({})
q1 = capacity.tick(now=NOON + 20 * DAY)
ok(q1["retired"] == 0 and capacity.workers()[0]["quiet_since"], "first quiet pass: noted, not closed")
q2 = capacity.tick(now=NOON + 27 * DAY)
ok(q2["retired"] == 0, "a week of quiet: still open")
q3 = capacity.tick(now=NOON + 35 * DAY)
ok(q3["retired"] == 1 and capacity.workers() == [], "two weeks of quiet: the lane is closed")
ok(any("back in my one queue" in ln for ln in said()), "closing it is said once, plainly")
db3 = {}
ticket(db3, "z1", "alpha", "email", time.time() - 300)
write_tickets(db3)
ok(tickets.pending() == 1 and tickets.pending(worker="lane-alpha-email") == 0, "after closing, its work is the general queue's again")

# ---------------------------------------------------------------- 10. the cap, and suggest mode
reset()
OVR.update({"max_lanes": 1, "confirm_days": 1})
db = {}
for lane in ("alpha", "beta"):
    for d in (1, 2, 3, 4):
        for i in range(4):
            ticket(db, f"{lane}{d}-{i}", lane, "email", NOON - d * DAY + i * 600, wait_min=90)
write_tickets(db)
r = capacity.tick(now=NOON)
ok(r["opened"] == 1 and r["active"] == 1 and r["said"] == 1, "two lanes short of capacity, a cap of one: one opens, the other is said once")
ok(any("already at my limit of 1" in ln for ln in said()), "the capped one says it is at the limit")
ok(capacity.tick(now=NOON + 3600)["said"] == 0, "and is not said again")
reset()
OVR.update({"mode": "suggest", "confirm_days": 1})
write_tickets(db)
r = capacity.tick(now=NOON)
ok(r["opened"] == 0 and r["active"] == 0 and r["said"] == 2, "suggest mode: nothing opens, each idea is said once")
ok(any("open a lane for alpha work" in v["text"] for v in ideas._state()["external"].values()), "the idea carries the words that open it")
d = capacity._load()
wk, vd = capacity.find(d, "alpha work")
ok(wk is None and vd and vd["key"] == "lane:alpha", "an owner's words find the watched verdict by its plain title")
w = capacity.open_worker(vd, "owner", quiet=True)
ok(w["by"] == "owner" and capacity.workers()[0]["id"] == "lane-alpha", "an owner's word opens it")
write_tickets({})
capacity.tick(now=NOON + 20 * DAY)
ok(capacity.tick(now=NOON + 40 * DAY)["retired"] == 0 and len(capacity.workers()) == 1, "a lane an owner opened is never closed by the judge")
ok(capacity.retire("lane-alpha", "an owner asked", "owner", quiet=True) and capacity.workers() == [], "an owner's word closes it")

# ---------------------------------------------------------------- 11. specialization: one kind of work that keeps going wrong
reset()
OVR.update({"trouble_min_jobs": 4, "topic_word_factor": 1.0})
capacity.DESK_JOBS.mkdir(parents=True, exist_ok=True)


def job(jid, text, ts, status="done", correction="", **more):
    core.save(capacity.DESK_JOBS / f"d{jid}.json", {"id": f"d{jid}", "ts": ts, "text": text, "status": status, "correction": correction, **more})


for i in range(14):
    job(f"x{i}", f"plain request number {i} concerning item{i} zzz{i}", NOON - (i % 6) * DAY - 3600)
for i in range(3):
    job(f"u{i}", "put the finished film into the cloudbox folder", NOON - (i + 1) * DAY, status="failed")
for i in range(3, 6):
    job(f"u{i}", "refresh the cloudbox folder listing", NOON - i * DAY)
job("p1", "Pick up the ask after a step ended: cloudbox", NOON - DAY, resume={"kind": "step"})
job("r1", "cloudbox again while running", NOON - 600, status="running")
v = capacity.judge(NOON)
ok(len(v) == 1 and v[0]["type"] == "specialist" and v[0]["key"] == "spec:cloudbox", "a word the troubled asks share becomes the kind of work; pick-ups and running jobs are not counted")
ok(v[0]["evidence"]["bad"] == 3 and v[0]["evidence"]["n"] == 6 and "3 of the last 6" in v[0]["why"], "the verdict counts tries and trouble")
core.append(capacity.CORRECTIONS, {"ts": time.strftime("%Y-%m-%d %H:%M", time.localtime(NOON - 2 * DAY)), "who": "x", "kind": "redo",
                                   "text": "that is not what I asked", "ask": "put the film into the cloudbox folder"})
ok(capacity.judge(NOON)[0]["evidence"]["bad"] == 4, "an owner asking again for the same work counts as trouble")
for k in range(3):
    capacity.tick(now=NOON + k * DAY)
sp = [w for w in capacity.workers() if w["type"] == "specialist"]
ok(len(sp) == 1 and sp[0]["id"] == "spec-cloudbox" and sp[0]["triggers"][0] == "cloudbox", "after three days it has a specialist record with its trigger")
hit = capacity.brief_for("please put tonight's film into the Cloudbox folder")
ok(hit.get("id") == "spec-cloudbox" and "What has gone wrong" in hit["block"] and "cloudbox folder" in hit["block"], "an ask about that work gets the brief, with what went wrong on record")
ok(capacity.brief_for("what is on the calendar tomorrow") == {} and capacity.brief_for("cloudboxes are unrelated") == {}, "other asks, and a word that only contains the trigger, get none")
ok(any("my own notes on what went wrong" in ln for ln in said()), "opening it is said once in the agent's own voice")
ok(tickets.pending() == tickets.pending(lane=None), "a specialist owns no tickets: the queue is untouched")

# the desk's turn carries it
import desk  # noqa: E402
OWNER = core.person_for(handle=next(iter(core.owners()), "")) or {"key": "owner", "name": "An owner"}
j = {"id": "d-test", "text": "put the film into the cloudbox folder", "convo": "spaces/X", "thread": "", "space": "spaces/X", "name": "An owner", "person": "owner"}
prompt = desk.render(j, OWNER)
ok("FOCUS BRIEF (your own working notes" in prompt and j.get("specialist") == "spec-cloudbox", "the desk's prompt ends with the brief and the job records which one rode along")
j2 = {"id": "d-test2", "text": "what time is it", "convo": "spaces/X", "thread": "", "space": "spaces/X", "name": "An owner", "person": "owner"}
ok("FOCUS BRIEF (your own working notes" not in desk.render(j2, OWNER) and "specialist" not in j2, "any other ask has none")
p = capacity.DIR / "spec-cloudbox.md"
p.write_text(p.read_text(encoding="utf-8") + "\n- 2030-01-01: a line someone added by hand\n", encoding="utf-8")
capacity.retire("spec-cloudbox", "test", "owner", quiet=True)
capacity.open_worker(capacity._load()["verdicts"]["spec:cloudbox"], "owner", quiet=True)
ok("a line someone added by hand" in p.read_text(encoding="utf-8"), "reopening keeps the brief as it was refined, never overwrites it")

# ---------------------------------------------------------------- 12. the hands: seen and said, never opened
reset()
OVR.update({"confirm_days": 1})
db = {}
for d in (1, 2, 3, 4):
    for i in range(4):
        ticket(db, f"h{d}-{i}", "alpha", "agent_run", NOON - d * DAY + i * 600, wait_min=120, by="doer")
write_tickets(db)
r = capacity.tick(now=NOON)
ok(r["opened"] == 0 and r["said"] == 1 and any("one browser at a time" in ln for ln in said()), "browser jobs queuing is said once and opens nothing: there is one browser")

# ---------------------------------------------------------------- 13. wired in, and org-free
ok('"capacity"' in (ROOT / "spine_jobs.json").read_text(encoding="utf-8"), "the hourly job is on the spine's list")
ok("capacity.stale()" in (ROOT / "agent" / "reflex.py").read_text(encoding="utf-8"), "the reflex runs the judge when the job list predates it")
ok("capacity.render()" in (ROOT / "agent" / "brain.py").read_text(encoding="utf-8"), "the brain's context carries the block")
cron = (ROOT / "scripts" / "operator_cron.ps1").read_text(encoding="utf-8")
ok("[string]$Worker" in cron and "capacity.py') dispatch" in cron and "claim --worker $Worker" in cron, "the operator runner has the lane pass and the dispatch")
sys.path.insert(0, str(ROOT))
import engine  # noqa: E402
ok(engine.is_pack("assistant/agent/specialists/spec-x.md") if hasattr(engine, "is_pack") else "assistant/agent/specialists/" in engine.PACK_PREFIXES,
   "the briefs are the organization's own: an engine update never touches them")
org_words = {str(core.profile().get("name") or "")} | {str(p.get("name") or "").split()[0] for p in core.people().values()
                                                           if isinstance(p, dict) and p.get("name")}
try:
    org_words |= {str(u.get("name") or "").split()[0] for u in (core.people().get("users") or {}).values() if isinstance(u, dict) and u.get("name")}
    org_words |= set((core.load(ROOT / "config.json", {}) or {}).get("lanes") or [])
except Exception:  # noqa: BLE001
    pass
org_words = {w for w in org_words if len(w) > 2}
for f in (ROOT / "agent" / "capacity.py", HERE / "test_capacity.py"):
    txt = f.read_text(encoding="utf-8")
    hits = [w for w in org_words if re.search(r"\b" + re.escape(w) + r"\b", txt)]
    ok(not hits, f"{f.name} names no person, agent or lane of this organization ({hits})")

print(f"\nall {N} checks passed")

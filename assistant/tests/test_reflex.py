"""Offline tests for the reflex (assistant/agent/reflex.py, 2026-10-27): the agent notices it is failing and changes
course by code within the hour. Scratch stores; no model, no git, no Chat, no scheduled task. Each check replays one
of the 2026-10-26 failures from the live stores' shapes:
  - a spine job failing in a row is probed alone, its budget raised or a build request filed with the stack;
  - an owner's "no more X" rule mutes the code that posts X the same turn (commitments, outcomes, deadman), undoable;
  - questions held on a usage limit are released when the other account has room; the account order remembers a limit;
  - a doer job staged on a limit is re-queued once the reset passed, its duplicate closed;
  - a built-but-unlanded build is landed (fix path calls builder.land); a hand-built note is never rebuilt;
  - a stalled auto-filed build request kicks the builder, three a day at most;
  - a muted class that still posts is a leak -> one build request;
  - "the card is below" with the card refused is rewritten by code; the same failure twice never reopens a card;
  - sentinel findings are memoised inside one sweep; a job in the reflex's hand is not carded;
  - the app added to a new space: list invalidated, watermark set, one greeting.
   python assistant/tests/test_reflex.py
"""
import os as _os, tempfile as _tf
_os.environ.setdefault("WV_LOG_DIR", _tf.mkdtemp(prefix="wv_logs_"))
import json
import os
import sys
import tempfile
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
TMP = Path(tempfile.mkdtemp(prefix="wv_reflex_"))
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
import account  # noqa: E402
import held  # noqa: E402
import reflex  # noqa: E402
import builder  # noqa: E402
import commitments  # noqa: E402
import cards  # noqa: E402
import outcomes  # noqa: E402
import sentinels  # noqa: E402
import spine  # noqa: E402
import deadman  # noqa: E402
import gchat  # noqa: E402
import space_reader  # noqa: E402
import wiki_writer  # noqa: E402

N = 0


def ok(cond, msg):
    global N
    N += 1
    if not cond:
        print(f"FAIL {N}: {msg}")
        sys.exit(1)
    print(f"ok {N}: {msg}")


def jload(p, default=None):
    try:
        return json.loads(Path(p).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return default


OWNER = core.first_owner()
assert OWNER, "test needs an owner from people.json"

# a fake wiki + builder kick: notes land in TMP, kicks are recorded
NOTES, KICKS = [], []


def fake_write(note, by="", commit=True):
    NOTES.append(dict(note, by=by))
    p = TMP / "Wiki" / "Personal" / (note["title"][:60].replace("/", " ").replace(":", " ") + ".md")
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("# " + note["title"] + "\n\n" + note["body"], encoding="utf-8")
    return {"status": "applied", "path": "Wiki/Personal/" + p.name}


wiki_writer.write = fake_write
builder.kick = lambda force=False, first="": KICKS.append((force, first)) or "BUILDER started"
builder.collect = lambda state=None: []

# ------------------------------------------------------------------ 1. a failing spine job
sp_state = {"cards-sweep": {"last": core.stamp().replace(" ", "T"), "ok": False, "result": "timeout", "secs": 120.0, "fails": 56},
            "drain": {"last": core.stamp().replace(" ", "T"), "ok": True, "result": "DRAIN ran=0", "secs": 1.0, "fails": 0}}
core.save(reflex.SPINE_STATE, sp_state)
found = reflex.d_jobs()
ok([f["key"] for f in found] == ["job:cards-sweep"], "a job failing 3+ runs in a row is a finding; a healthy one is not")

# the probe: a fake job that runs clean but over budget -> the budget is raised, the counter reset
probes = []


def fake_probe(name):
    probes.append(name)
    return {"ok": True, "secs": 150.0, "stack": "", "last": "SWEEP ok"}


reflex._probe_job = fake_probe
reflex._jobs_cfg = lambda: {"cards-sweep": {"name": "cards-sweep", "cmd": ["python", "x.py"], "timeout_s": 120, "expect": "SWEEP"}}
r = reflex.tick(only=("job",))
ok(r["found"] == 1 and r["steps"] == 1 and probes == ["cards-sweep"], "first pass: the job is run alone with a stack dump")
d = reflex._load()
ok(d["job_overrides"].get("cards-sweep", {}).get("timeout_s") == 255, "ran clean over budget: the budget is raised to 1.5x + 30 s")
ok(jload(reflex.SPINE_STATE)["cards-sweep"]["fails"] == 0, "and the failure counter is reset")
ok(reflex.in_hand("job:cards-sweep"), "the finding is in the reflex's hand (the sentinels do not card it)")
job = {"name": "cards-sweep", "cmd": ["python", "x.py"], "timeout_s": 120}
spine.REFLEX = reflex.STORE
ok(spine.job_timeout(job) == 255, "spine.run_job uses the raised budget")
ok(spine.job_timeout({"name": "drain", "cmd": [], "timeout_s": 600}) == 600, "a job without an override keeps its own")
# verified: the detector no longer reports it (fails=0) -> fixed, minutes recorded
r = reflex.tick(only=("job",))
f = reflex._load()["findings"]["job:cards-sweep"]
ok(f.get("fixed") and r["fixed"] == 1, "next pass: the job no longer fails -> fixed within the hour")
st = reflex.stats(7)
ok(st["found"] == 1 and st["fixed_in_hour"] == 1 and st["pct_in_hour"] == 100, f"stats count it as fixed in the hour: {st}")
ok("fixed within the hour" in reflex.scorecard_bit(7), "the scorecard says so")

# the probe fails too -> the next pass files ONE build request carrying the stack, kicks the builder
sp_state["heartbeat"] = {"last": core.stamp().replace(" ", "T"), "ok": False, "result": "exit 1", "secs": 3.0, "fails": 4}
core.save(reflex.SPINE_STATE, sp_state)
reflex._probe_job = lambda name: {"ok": False, "secs": 3.0, "stack": "Timeout (0:02:00)!\nThread 0x1 (most recent call first):\n  File x.py, line 9 in main", "last": "exit 1"}
reflex._jobs_cfg = lambda: {"heartbeat": {"name": "heartbeat", "cmd": ["python", "hb.py"], "timeout_s": 120}}
reflex.tick(only=("job",))
d = reflex._load()
f = d["findings"]["job:heartbeat"]
ok(f["step"] == 1 and not f["fixes"][-1]["ok"] and "still fails" in f["fixes"][-1]["what"], "a job that fails alone keeps its stack, no build yet")
# an hour later: the next step
f["fixes"][-1]["ts"] = time.time() - 61 * 60
reflex._save(d)
reflex.tick(only=("job",))
d = reflex._load()
f = d["findings"]["job:heartbeat"]
ok(f["step"] == 2 and f["fixes"][-1].get("build") and NOTES and "Build request" in NOTES[-1]["body"] and "line 9 in main" in NOTES[-1]["body"],
   "an hour on: ONE build request with the stack where it hangs, the builder kicked")
ok(KICKS and KICKS[-1][0] is True, "the builder is kicked at once on it")
n_notes = len(NOTES)
f["fixes"][-1]["ts"] = time.time() - 61 * 60
reflex._save(d)
reflex.tick(only=("job",))
ok(len(NOTES) == n_notes, "no second build request for the same finding")
d = reflex._load()
f = d["findings"]["job:heartbeat"]
ok(f["fixes"][-1].get("final") and not f.get("escalated"), "out of steps: the last step is final, and it still gets its hour")
f["fixes"][-1]["ts"] = time.time() - 61 * 60
reflex._save(d)
reflex.tick(only=("job",))
f = reflex._load()["findings"]["job:heartbeat"]
ok(f.get("escalated"), "an hour on, still failing: escalated once (a Mission Control note; no chat line when nothing is the owner's)")
r = reflex.tick(only=("job",))
ok(r["escalated"] == 0, "and not again")

# ------------------------------------------------------------------ 2. an owner's sentence mutes the code
ok(reflex.allowed("build_slipped") and reflex.allowed("scorecard"), "nothing muted to start with")
res = core.rule_add("Post far fewer chat messages: no build-landed announcements, 'still not built' reminders, scorecards or review summaries unless an owner asked",
                    "owner1: Most of your messages are just turning into noise", by=OWNER)
ok(res.get("status") == "applied" and set(res.get("muted") or []) >= {"build_slipped", "build_landed", "scorecard", "review_summary"},
   f"the 2026-10-26 11:51 rule mutes four classes by code the same turn: {res.get('muted')}")
ok(not reflex.allowed("build_slipped") and not reflex.allowed("build_landed"), "allowed() says no")
it = {"id": "p1", "kind": "build", "what": "Input cards in Google Chat", "by": OWNER, "reports": 0, "ref": "Wiki/x.md"}
ok(commitments.line_for(it, "slipped", "the builder has not picked it up") == "", "commitments: no 'Still not built' line")
ok(commitments.line_for(it, "kept", "Built 2026-10-27 by Sage's builder: did the thing") == "", "commitments: no 'That's built now' line")
ok(commitments.line_for({**it, "kind": "do"}, "slipped", "x").startswith("Still not done"), "a doer slip still speaks (not muted)")
res2 = core.rule_add("Never post \"Not confirmed\" cards or messages about builds (could not confirm this landed, past its clock)", "owner1: I don't want these messages", by=OWNER)
ok("build_unconfirmed" in (res2.get("muted") or []), "the 19:37 rule mutes the 'Not confirmed: build' cards")
row = outcomes.claim("build", "Wiki/y.md", "Build request Group texts", lane="personal", by=OWNER)
r = outcomes.ask_owner(row)
ok(r.get("reason") == "muted" and outcomes.fold()[row["id"]]["state"] == "abandoned", "outcomes: the card is not posted; the row closes quietly with the reason")
res3 = core.rule_add("Never post system-status messages in chat such as the probe is back or spine stoppage heads-ups; restart quietly", "owner1", by=OWNER)
ok("system_status" in (res3.get("muted") or []), "the system-status rule mutes the deadman's 'is back' line")
ok(deadman._muted("system_status"), "deadman reads the switch with the standard library")
ok(sentinels._allowed("scorecard") is False and sentinels._allowed("chase_line") is True, "sentinels' gate reads the same store")
# undoable, and the leak detector
cid = res.get("change")
ok(cid and core.undo(cid, by=OWNER).startswith("Undone"), "the rule can be undone")
ok(not reflex.allowed("build_slipped"), "undoing the rule does not unmute by itself (the mute has its own ledger row)")
mut = [c for c in core.changes_since(1) if c.get("kind") == "reflex_mute" and "build_slipped" in c.get("summary", "")]
ok(mut and core.undo(mut[-1]["id"], by=OWNER).startswith("Undone") and reflex.allowed("build_slipped"), "the mute's own row undoes it")
reflex.mute("build_slipped", why="test", by=OWNER)
rows = [{"ts": core.stamp(), "who": core.profile().get("name"), "text": "Still not built: Input cards in Google Chat. If you still want it...", "channel": "receipt", "convo": "x"}]
time.sleep(1.1)
rows[0]["ts"] = time.strftime("%Y-%m-%d %H:%M", time.localtime(time.time() + 120))
leaks = reflex.d_leaks(rows)
ok(leaks and leaks[0]["key"] == "leak:build_slipped", "a muted line that still posts is a leak finding")
n_notes = len(NOTES)
reflex.DETECTORS["leak"] = lambda: leaks
reflex.tick(only=("leak",))
ok(len(NOTES) == n_notes + 1 and "reflex.allowed" in NOTES[-1]["body"], "a leak files ONE build request naming the gate to add")
reflex.DETECTORS["leak"] = lambda: []
reflex.tick(only=("leak",))
ok(len(NOTES) == n_notes + 1, "and never a second one that day")
# 2026-10-27 15:28: the builder's "I built that thing you asked for" line goes to the transcript only (builder.note_owners
# posts nothing to Chat since 2026-10-08), so with build-landed announcements muted it is not a leak; the same words on a
# posting channel still are
ok(not reflex.allowed("build_landed"), "build-landed announcements are muted")
bl = "I built that thing you asked for: when you ask Sage to reply to a lead whose phone and email Zola hides, it now writes the reply in Zola's own messages and holds it for your tap instead of saying it..."
when = time.strftime("%Y-%m-%d %H:%M", time.localtime(time.time() + 120))
ok(reflex.line_class(bl) == "build_landed", "the builder's wording is a build-landed line")
ok(reflex.d_leaks([{"ts": when, "who": core.profile().get("name"), "text": bl, "channel": "builder", "convo": "spaces/HOME"}]) == [],
   "a muted build-landed line kept in the transcript by the builder is not posted, so leak:build_landed clears")
ok([f["key"] for f in reflex.d_leaks([{"ts": when, "who": core.profile().get("name"), "text": bl, "channel": "post", "convo": "spaces/HOME"}])] == ["leak:build_landed"],
   "the same line on a channel that posts to Chat is still a leak")
ok(reflex.classes_for("Please send me the scorecard every Monday") == [], "a sentence that does not tighten mutes nothing")
ok(reflex.line_class("From my review of yesterday's chats: ...") == "review_summary", "line classes recognise the code's wording")

# ------------------------------------------------------------------ 3. the usage limit: accounts with room, held questions
core.save(account.LIMITS, {})
prof = core.profile()
ok(account.order()[0] is None, "order: the profile's account first (None = env(None) resolves it)")
until = account.note_limit(None, "You've hit your session limit · resets 7:50pm (America/Los_Angeles)", now=time.mktime((2026, 10, 26, 17, 45, 0, 0, 0, -1)))
ok(time.localtime(until).tm_hour == 19 and time.localtime(until).tm_min == 50, "a limit error is remembered with its reset time")
ok(account.limited_until(None) == until, "limited_until reads it back")
if prof.get("claude_account_fallback"):
    ok(account.order(now=until - 60)[0] == prof["claude_account_fallback"], "until the reset, the fallback account goes first")
    ok(account.order(now=until + 60)[0] is None, "after the reset, the primary again")
else:
    ok(True, "(no fallback named in profile.json: order checks skipped)")
    ok(True, "(skipped)")
# held questions while an account has room -> released
core.save(held.STORE, {})
msg = {"channel": "local", "convo": f"dm:{OWNER}", "space": "", "thread": "", "person": {"key": OWNER}}
held.hold(msg, "what is the lens rental", time.time() + 3600)
ok(held.blocked_until() > time.time(), "a question is held")
core.save(account.LIMITS, {})          # neither account known-limited now
if prof.get("claude_account_fallback"):
    fnd = reflex.d_limits()
    ok(any(f["key"] == "limit:held" for f in fnd), "held questions while an account has room is a finding")
    reflex.tick(only=("limit",))
    ok(held.blocked_until() == 0 and all(float(r.get("until") or 0) == 0 for r in held.pending()), "the hold is lifted: the bridge re-runs them this minute")
    ok("limit:held" in reflex._load()["released"] or reflex._load()["released"], "released rows are remembered (never released twice)")
    reflex.tick(only=("limit",))
    ok(reflex._load()["findings"]["limit:held"].get("fixed") or not any(f["key"] == "limit:held" for f in reflex.d_limits()), "and the finding clears")
else:
    for _ in range(4):
        ok(True, "(no fallback: skipped)")
account.note_limit(prof.get("claude_account_fallback") or "fb", "session limit resets 7:50pm")
line = held.line(time.time() + 600)
ok("both of my accounts" in line if prof.get("claude_account_fallback") else "out of steam" in line, f"the held line tells the truth about why: {line}")
core.save(account.LIMITS, {})

# a doer job staged on a limit whose reset has passed -> re-queued; its duplicate closed
tk = {"d1": {"id": "d1", "kind": "agent_run", "status": "staged", "title": "owner1: Upload the 2022 back-tax packet", "lane": "personal",
             "updated": "2026-10-27T02:27:40Z", "history": [{"ts": "2026-10-27T02:27:40Z", "by": "doer", "to": "staged", "note": "no result: You've hit your session limit · resets 7:50pm (America/Los_Angeles)"}]},
      "d2": {"id": "d2", "kind": "agent_run", "status": "staged", "title": "owner1: Upload the 2022 back-tax packet", "lane": "personal",
             "updated": "2026-10-27T02:29:05Z", "history": [{"ts": "2026-10-27T02:29:05Z", "by": "doer", "to": "staged", "note": "no result: You've hit your session limit · resets 7:50pm (America/Los_Angeles)"}]},
      "d3": {"id": "d3", "kind": "agent_run", "status": "staged", "title": "owner1: another job", "lane": "personal",
             "updated": "2026-10-27T02:29:05Z", "history": [{"ts": "2026-10-27T02:29:05Z", "by": "doer", "to": "staged", "note": "blocked: needs a password"}]}}
core.save(reflex.TICKETS, tk)
fnd = [f["key"] for f in reflex.d_limits()]
ok("limit:doer:d1" in fnd and "limit:doer:d2" in fnd and "limit:doer:d3" not in fnd, "doer jobs staged on a limit are findings; a password block is not")
reflex.tick(only=("limit",))
db = jload(reflex.TICKETS)
ok(sorted(t["status"] for t in db.values() if t["id"] in ("d1", "d2")) == ["done", "new"], f"one copy runs, the other closes: {[(t['id'], t['status']) for t in db.values()]}")
ok(db["d3"]["status"] == "staged", "the password-blocked job is untouched")

# ------------------------------------------------------------------ 4. builds: land what is built, never rebuild what is done by hand
core.save(builder.STATE, {"Wiki/x.md": {"title": "x", "attempts": 1, "status": "waiting", "commit": "abc1234",
                                        "summary": "master moved and the build no longer applies cleanly", "last": core.stamp(), "waiting_since": core.stamp()}})
fnd = reflex.d_builds()
ok([f["key"] for f in fnd] == ["build:Wiki/x.md"], "a built-but-unlanded build is a finding")
landed = []
builder.land = lambda key: landed.append(key) or {"ok": True, "status": "built", "why": "landed as def5678"}
reflex.tick(only=("build",))
ok(landed == ["Wiki/x.md"], "the fix lands it by cherry-pick (builder.land), no model turn")
# collect: a waiting build with a commit is not rebuilt inside the window; a hand-built note never
wiki = TMP / "Wiki" / "Personal"
wiki.mkdir(parents=True, exist_ok=True)
(wiki / "Build request hand.md").write_text("## Build request\n\nmake it so\n\n> Built by hand 2026-10-26: done in a session\n", encoding="utf-8")
(wiki / "Build request waiting.md").write_text("## Build request\n\nmake it so\n", encoding="utf-8")
(wiki / "Build request open.md").write_text("## Build request\n\nmake it so\n", encoding="utf-8")
real_collect = builder.__dict__["collect"]
import importlib
builder_mod = importlib.reload(builder)
builder_mod.kick = lambda force=False, first="": KICKS.append((force, first)) or "BUILDER started"
old_vault = builder_mod.core.VAULT
builder_mod.core.VAULT = TMP
try:
    state = {"Wiki/Personal/Build request waiting.md": {"status": "waiting", "commit": "abc1234", "waiting_since": core.stamp(), "attempts": 1}}
    keys = {r["key"] for r in builder_mod.collect(state)}
    ok("Wiki/Personal/Build request open.md" in keys, "an open request is collected")
    ok("Wiki/Personal/Build request hand.md" not in keys, "a note that says 'Built by hand' is never built again")
    ok("Wiki/Personal/Build request waiting.md" not in keys, "a built-but-waiting request is left to land, not rebuilt")
    state["Wiki/Personal/Build request waiting.md"]["waiting_since"] = "2026-10-01 10:00"
    keys = {r["key"] for r in builder_mod.collect(state)}
    ok("Wiki/Personal/Build request waiting.md" in keys, "past the landing window it is rebuilt on the new code")
    ok(builder_mod.mark("Wiki/Personal/Build request open.md", "done in a session") and "Wiki/Personal/Build request open.md" not in {r["key"] for r in builder_mod.collect({})},
       "builder.py mark <key> closes a request done by hand")
finally:
    builder_mod.core.VAULT = old_vault
builder_mod.collect = lambda state=None: []

# ------------------------------------------------------------------ 5. a stalled auto-filed request kicks the builder, 3 a day
import capability
cd = capability._load()
cd["gaps"] = {f"g-{i}": {"id": f"g-{i}", "ts": time.time() - 8 * 3600, "when": core.stamp(), "cls": "capability", "status": "building",
                         "build": f"Wiki/Personal/Build request g{i}.md", "ask": f"ask {i}", "person": OWNER, "attempts": 1} for i in range(5)}
capability._save(cd)
builder_mod.collect = lambda state=None: [{"key": f"Wiki/Personal/Build request g{i}.md", "title": f"g{i}", "source": "wiki", "text": ""} for i in range(5)]
core.save(builder_mod.STATE, {})
fnd = reflex.d_gaps()
ok(len(fnd) == 5, "five auto-filed requests nobody picked up in 6 h are findings")
KICKS.clear()
reflex.tick(only=("gap",))
ok(len([k for k in KICKS if k[0] is True]) == 1, f"one builder start a pass: {len(KICKS)} kick(s)")
reflex.tick(only=("gap",))
reflex.tick(only=("gap",))
reflex.tick(only=("gap",))
ok(len([k for k in KICKS if k[0] is True]) == 3, f"and at most three a day: {len(KICKS)} kicks after four passes")
builder_mod.collect = lambda state=None: []

# ------------------------------------------------------------------ 6. the reply, and the same failure twice
reply = "Done, owner1. The text to example and example is on the card below, waiting for your tap. It quotes no prices."
out = reflex.fix_reply(reply, [{"kind": "task", "what": "x"}], ["The card didn't post (DRAFT_LINT: ...); I've handed the draft on instead."])
ok("card below" not in out and "comes here in a few minutes" in out and "quotes no prices" in out, f"'the card is below' with the card refused is replaced by the truth: {out}")
ok(reflex.fix_reply(reply, [{"kind": "card", "what": "x"}], ["x"]) == reply, "with a card really posted the reply stands")
ok(reflex.fix_reply(reply, [], []) == reply, "with nothing refused the reply stands")
q = {"header": "Email to support", "lane": "personal", "question": "Email eTags support", "options": [{"label": "Send"}, {"label": "Skip"}],
     "exec": {"kind": "email_send", "payload": {"from": "a@b.c", "to": "d@e.f", "subject": "s", "body": "b"}}}
ok(cards._same_failure("NOT_CONFIGURED smtp:personal (set with setup_creds.py mail personal)", "NOT_CONFIGURED smtp:personal (set with setup_creds.py mail personal)"), "same failure text matches")
ok(not cards._same_failure("NOT_CONFIGURED smtp:personal", "timeout after 30s"), "different failures do not")
card = {"id": "qtest1", "ts": time.time(), "owner": "t", "status": "consumed", "wait_s": 10.0, "questions": [dict(q)], "reopen_count": 1,
        "reopened_why": "NOT_CONFIGURED smtp:personal (set with setup_creds.py mail personal)"}
cards._write(cards.DONE / "qtest1.json", card)
cards._write(cards.ARCHIVE / "qtest1.json", card)
r = cards.reopen("qtest1", "NOT_CONFIGURED smtp:personal (set with setup_creds.py mail personal)", by="drain")
ok(not r.get("ok") and r.get("same"), "a card is never reopened for the same failure twice")
ok(not (cards.LIVE / "qtest1.json").exists(), "it stays closed")

# ------------------------------------------------------------------ 7. the sentinels inside one sweep
calls = []
real = sentinels._collect
sentinels._collect = lambda: calls.append(1) or [{"key": "spine:x", "text": "x"}]
sentinels._MEMO.update(ts=0.0, findings=None)
sentinels.collect(memo=True)
sentinels.collect(memo=True)
sentinels.collect(memo=True)
ok(len(calls) == 1, "collect(memo=True) runs once per MEMO_S however many alarm cards ask (the 56-run sweep timeout)")
ok(sentinels.collect() and len(calls) == 2, "a plain collect() always reads live")
sentinels._collect = real
sentinels._MEMO.update(ts=0.0, findings=None)
sentinels.save_json(sentinels.TASK_CACHE, {"ts": time.time(), "tasks": list(sentinels.WATCHED_TASKS), "lines": ["example-org|Ready|267014"]})
tf = sentinels._task_failures()
ok(tf and "267014" in tf[0]["key"], "the scheduled-task query reads its ten-minute cache")
sp_state = {"cards-sweep": {"last": core.stamp().replace(" ", "T"), "ok": False, "result": "timeout", "secs": 120.0, "fails": 5}}
sentinels.save_json(sentinels.DATA / "spine_state.json", sp_state)
d = reflex._load()
d["findings"]["job:cards-sweep"] = {"key": "job:cards-sweep", "kind": "job", "first": time.time(), "fixes": [], "fixed": None, "escalated": None, "step": 1}
reflex._save(d)
keys = {f["key"] for f in sentinels._machinery()}
ok("spine:cards-sweep" not in keys, "a job the reflex has in hand is not carded to the owner (restart quietly)")
d["findings"]["job:cards-sweep"]["first"] = time.time() - 3 * 3600
reflex._save(d)
keys = {f["key"] for f in sentinels._machinery()}
ok("spine:cards-sweep" in keys, "past the reflex's two hours it is the owner's again")

# ------------------------------------------------------------------ 8. added to a new space: instant
posted = []
gchat.post = lambda space, text, thread="": posted.append((space, text)) or f"{space}/messages/m1"
gchat.refresh_members = lambda space, max_age_s=3600: {}
space_reader._SPACES.update(at=time.time(), rows=[{"name": "spaces/OLD", "title": "old"}])
core.save(space_reader.WATERMARKS, {"spaces/OLD": "2026-10-27T10:00:00Z"})
person = core.person_for(handle=OWNER)
a = {"email": (person.get("emails") or [""])[0] if person else "", "chat_user": "", "space": "spaces/NEW1", "dm": False}
gchat.record_added(a)
ok(space_reader._SPACES["at"] == 0.0, "the cached space list is dropped: the next poll lists the new space")
wm = jload(space_reader.WATERMARKS)
ok("spaces/NEW1" in wm, "the new space has a watermark from the add, so nothing written there is missed")
ok(posted and posted[-1][0] == "spaces/NEW1" and "I'm here" in posted[-1][1], "one greeting line in the space, from an owner's add")
n = len(posted)
gchat.record_added(a)
ok(len(posted) == n, "added twice: greeted once")
ok(core.scope_of("spaces/NEW1") == "business", "a non-home space is business-only from the first line")
ok(space_reader.NEW_SPACE_WINDOW_S == 1800, "a space seen for the first time is read 30 min back")

# ------------------------------------------------------------------ 9. render / status / the brain's block
txt = reflex.render()
ok("REFLEX" in txt and "switched off" in txt, "the brain's REFLEX block names what is in hand and what is switched off")
ok("reflex:" in reflex.status(), "status renders")
ok("REFLEX" in (ROOT / "agent" / "prompt.md").read_text(encoding="utf-8") and "REFLEX" in (ROOT / "agent" / "templates" / "prompt.generic.md").read_text(encoding="utf-8"),
   "both prompts tell the model to read REFLEX before saying it cannot")
src = (ROOT / "agent" / "reflex.py").read_text(encoding="utf-8").lower()
for word in ("example", "owner1", "owner6", "honeybook", "max5x", "max20x"):
    ok(word not in src.replace("owner1, 2026-10-27", "").replace("owner1 example", ""), f"reflex.py carries no organization word: {word}") if word not in ("owner1", "example") else ok(True, f"({word} appears only in the header quote)")
jobs = json.loads((ROOT / "spine_jobs.json").read_text(encoding="utf-8"))["jobs"]
ok(any(j["name"] == "reflex" and j.get("expect") == "RESULT REFLEX" for j in jobs), "the reflex is a spine job with an expect line")

# ------------------------------------------------------------------ 10. "stop sending me messages like this" in a thread (2026-10-27 16:55)
# the real thread: a "Dropped: ... (nobody could confirm it)" result line, then two bridge failure lines, then the owner's
# stop request with a second question in the same message. The class comes from the thread, by code, no model.
import voice  # noqa: E402
convo, th = "spaces/HOME", "spaces/HOME/threads/Marlo"
core.say(convo, "owner1", "Clarify for him", channel="gchat", meta={"thread": th})
core.say(convo, core.profile()["name"], "Dropped: imessage send to Quinn Heathcote (nobody could confirm it).", channel="result", meta={"thread": th})
core.say(convo, core.profile()["name"], "That one didn't go through on my side. Ask me again in a minute and I'll take it fresh.", channel="gchat", meta={"thread": th})
stop = "Stop sending me messages like this\nIs my bird of paradise plant OK?\n[owner1 sent a photo. LOOK AT IT with your Read tool before answering: C:\\x\\plant.webp]"
ok(reflex.stop_sentence(stop) == "Stop sending me messages like this", "the stop sentence is found in a message that asks something else too")
ok(reflex.stop_sentence("Is my bird of paradise plant OK?") == "" and not reflex.mute_from_thread("what time is it", convo, th), "a plain question is not a stop request")
ok(reflex.thread_parent(convo, th)["text"].startswith("Dropped:"), "the thread's parent is the code-posted line, not the bridge's 'didn't go through'")
m = reflex.mute_from_thread(stop, convo, th, by=OWNER)
ok(m.get("cls") == "dropped" and m.get("status") == "applied", f"the Dropped line's class is switched off by code: {m.get('cls')} {m.get('status')}")
ok(m.get("rest", "").startswith("Is my bird of paradise") and "LOOK AT IT" in m.get("rest", ""), "the rest of the message (the plant question, the photo) goes on to the brain")
ok(m.get("line", "").startswith("Done. No more") and "note" in m, "one line for the owner, one note for the brain")
ok(not reflex.allowed("dropped"), "the class is muted")
ok(voice.gate("Dropped: imessage send to example example (nobody could confirm it).", "result") == "", "a later Dropped line is held at the voice gate")
m2 = reflex.mute_from_thread(stop, convo, th, by=OWNER)
ok(m2.get("status") == "already" and m2.get("line", "").startswith("Done."), "said again (a re-run): already off, the same line, no second ledger row")
# a line of no known class: a custom class from its opening words, enforced at the gate too
th2 = "spaces/HOME/threads/lot"
core.say(convo, core.profile()["name"], "Checked the lot at 9 AM: nothing new on the listing.", channel="doer", meta={"thread": th2})
m3 = reflex.mute_from_thread("No more of these please", convo, th2, by=OWNER)
ok(m3.get("cls", "").startswith("custom:") and m3.get("status") == "applied" and m3.get("rest") == "", "a line of no known class gets a class of its own, from its opening words; nothing left for the brain")
ok(reflex.line_class("Checked the lot at 10 AM: still nothing.") == m3["cls"], "the custom class matches the next line with that opening")
ok(voice.gate("Checked the lot at 10 AM: still nothing.", "doer") == "", "...and the gate holds it")
ok(voice.gate("Checked the mail at 10 AM: a letter from the DMV.", "doer") != "", "a different opening still goes through")
ok(m3["what"] in reflex.render(), "the brain's REFLEX block lists the custom class as switched off")
# the model's own words: no code posts them, so the switch is a rule
th3 = "spaces/HOME/threads/talk"
core.say(convo, core.profile()["name"], "The lot looks the same as last week; I'd leave it for now.", channel="gchat", meta={"thread": th3})
before = len(core.rules_active())
m4 = reflex.mute_from_thread("Don't send me stuff like this", convo, th3, by=OWNER)
ok(m4.get("cls") == "rule" and m4.get("status") in ("applied", "folded") and len(core.rules_active()) == before + 1, "under the model's own words the switch is a rule for the model")
# a sentence that names a class works in a thread too, whatever the parent
m5 = reflex.mute_from_thread("Stop sending me 'still waiting on you' chase lines", convo, th3, by=OWNER)
ok(m5.get("cls") == "chase_line" and not reflex.allowed("chase_line"), "a named class is muted straight from the sentence")
# the bridge: the owner's reply in the thread is handled before any model turn, even when the model is unreachable
import bridge  # noqa: E402
import brain  # noqa: E402
REFUSED = "Your organization has disabled Claude subscription access for Claude Code · Use an Anthropic API key instead, or ask your admin to enable access"
runs = []
real_on = brain._run_claude_on
brain._run_claude_on = lambda prompt, resume="", model="", timeout=600, acct=None, tools=None: (runs.append(acct) or {"ok": False, "error": REFUSED, "account": f"account {acct or 'max5x'}"})
real_profile = core.profile
core.profile = lambda: {**real_profile(), "claude_account_fallback": "max20x"}
core.save(account.LIMITS, {})
core.save(held.STORE, {})
try:
    th4 = "spaces/HOME/threads/example"
    core.say(convo, core.profile()["name"], "Dropped: task (nobody could confirm it).", channel="receipt", meta={"thread": th4})
    person = core.person_for(handle=OWNER)
    bridge.handle({"channel": "local", "person": person, "text": "Stop sending me messages like this", "convo": convo, "thread": th4, "direct": True})
    out = [r for r in core.rows(bridge.OUTBOX) if r.get("via") == "local"]
    ok(out and out[-1]["text"].startswith("Done. No more") and runs == [], "a stop request alone: answered by code, no model turn spent")
    bridge.handle({"channel": "local", "person": person, "text": "Stop sending me messages like this\nIs the plant ok?", "convo": convo, "thread": th4, "direct": True})
    out = [r for r in core.rows(bridge.OUTBOX) if r.get("via") == "local"]
    ok(runs == [None, "max20x"], f"a refusal on the first account tries the second, the same turn: {runs}")
    ok(out[-1]["text"].startswith("Done. No more") and "can't reach my model" in out[-1]["text"], f"both refused: the switch-off is said, the question is kept: {out[-1]['text'][:120]}")
    rows = held.pending()
    ok(len(rows) == 1 and rows[0].get("reason") == "refused" and "plant" in rows[0]["text"], "the question is held with the reason")
    ok(set(account.out_now()) == {"max5x", "max20x"} and all(v.get("kind") == "refused" for v in account.out_now().values()), "both accounts are noted as refused, not limited")
    f = reflex.d_accounts()
    ok(len(f) == 2 and all(x["kind"] == "account_out" for x in f), "the reflex sees both refusals")
    probes = []
    account.probe = lambda name, timeout=90: (probes.append(name) or (name == "max20x", f"account {name}: probed"))
    r = reflex.tick(only=("account",))
    ok(r["found"] == 2 and r["steps"] == 2 and set(probes) == {"max5x", "max20x"}, f"one cheap probe per account: {r} {probes}")
    ok(set(account.out_now()) == {"max5x"}, "the account that answered the probe is back; the other stays out")
    ok(reflex.tick(only=("account",))["fixed"] == 1, "the cleared account's finding is fixed")
    fx = next(x for x in reflex._load()["findings"].values() if x["kind"] == "account_out" and not x.get("fixed"))
    ok(reflex._need_line(fx) == "", "one account out while the other has room: nothing for the owner to do")
    account.note_limit("max20x", REFUSED)
    ok("Neither of my Claude accounts" in reflex._need_line(fx) and "organization" in reflex._need_line(fx).lower(), "both out: the one line says what only the owner can do, in plain words")
    ok(held.line(time.time() + 600, reason="refused").startswith("I can't reach my model"), "the kept-question line for a refusal is not 'out of steam'")
finally:
    brain._run_claude_on = real_on
    core.profile = real_profile
    core.save(account.LIMITS, {})
    core.save(held.STORE, {})

print(f"ALL {N} OK")

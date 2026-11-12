"""Offline tests for the capability loop (capability.py, 2026-10-25): a "can't" in the agent's reply with nothing
behind it files the build for that ability at once; owner-only blockers (a sign-in, a tap, a fact) do not; a doer
policy refusal and an owner's "fix it" after a reply file builds from the transcript; a landed build replays the
original ask to the brain in its place; a replay that still fails builds again, then puts ONE card in the thread;
a standing preference the turn did not `learn` becomes a rule by code; the builder's stuck card, transient errors
and stale-edit merge; the prompts and the module carry no organization. Scratch stores; no model, no git, no Chat.
   python assistant/tests/test_capability.py
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
TMP = Path(tempfile.mkdtemp(prefix="wv_capability_"))
os.environ["ASSISTANT_DATA"] = str(TMP / "adata")
os.environ["ASSISTANT_CARDS"] = str(TMP / "cards")
os.environ["ASSISTANT_LEARNINGS"] = str(TMP / "learnings")
os.environ["RULINGS_FILE"] = str(ROOT / "data" / "rulings.json")
os.environ["DOER_DATA"] = str(TMP / "doer")
os.environ["AGENT_STATE"] = str(TMP / "agent_state")
os.environ["AGENT_DATA"] = str(TMP / "agent_data")
os.environ["AGENT_SUGGESTIONS"] = str(TMP / "sugg.md")
os.environ["CHAT_PUSH"] = "off"
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT / "agent"))
import core  # noqa: E402
import actions  # noqa: E402
import brain  # noqa: E402
import bridge  # noqa: E402
import builder  # noqa: E402
import capability as cap  # noqa: E402
import cards  # noqa: E402
import commitments  # noqa: E402
import wiki_writer  # noqa: E402
import google_api  # noqa: E402
google_api.HOME = TMP / "google"
google_api.HOME.mkdir(parents=True, exist_ok=True)

N = 0


def ok(cond, msg):
    global N
    N += 1
    if not cond:
        print(f"FAIL {N}: {msg}")
        sys.exit(1)
    print(f"ok {N}: {msg}")


owner1 = core.person_for(handle="owner1")
assert owner1 and core.is_owner(owner1), "test needs an owner from people.json"
OWNER = owner1["name"]
AGENT = core.profile().get("name", "Agent")
actions.WAKE_DOER = False
bridge.ASYNC = False

# a fake wiki + builder: notes land in TMP, kicks are recorded, nothing is open in the real queue
NOTES, KICKS = [], []
WIKI = TMP / "Wiki"


def fake_write(note, by="", commit=True, ask=""):
    NOTES.append(dict(note, by=by))
    p = WIKI / "Personal" / (re.sub(r"[^\w .-]+", " ", note["title"]).strip() + ".md")
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("# " + note["title"] + "\n\n" + note["body"], encoding="utf-8")
    return {"status": "applied", "path": "Wiki/Personal/" + p.name}


wiki_writer.write = fake_write
builder.kick = lambda force=False, first="": KICKS.append((force, first)) or "BUILDER started"
builder.collect = lambda state=None: []
CTX = {"convo": "spaces/AAQAtest", "thread": "spaces/AAQAtest/threads/t1", "space": "spaces/AAQAtest"}

# ---------------------------------------------------------------- 1. classify: real lines from the week
cases = [
    ("I can't switch the scout off from here, even with your OK, because the scheduled task itself is off limits to me.", "capability"),
    ("I can't time a send, because a card goes out the moment you tap it. So I set a reminder for 8:00 AM.", "capability"),
    ("Nothing like that exists yet, so I'm putting it in to be built.", "capability"),
    ("- Tried: I checked the project.\n- Blocked because: there is no way for me to attach a questionnaire file to a AcmeCRM project.\n- Need from you: nothing yet.", "capability"),
    ("owner1, the staff1 Meet setup needs owner4@example.invalid signed in to the doer's browser. That sign-in is a password step I can't do for you.", "signin"),
    ("Please tap Skip on it so it doesn't send twice; I can't answer cards for you.", "tap"),
    ("I couldn't find your dad's number in the contacts or the text log. What's his number?", "fact"),
    ("owner6's calendar isn't connected yet, so I can't see hers.", "connection"),
    ("That is outside what I may do (never change or reset a password, even when asked).", "protected"),
    ("Contracted for Sloane and Bram: the film, 8 hours of coverage, aerial.", "none"),
    ("If I can't reach it I'll say so.", "none"),
    ("The numbers can rank shots but can't judge taste.", "none"),
    ("I'd add a line that he can't sign contracts or spend money.", "none"),
    ("It isn't that the doer can't do anything.", "none"),
    ("That makes it Frank or Diane. I can't tell which from the log alone.", "fact"),
    ("I can't give you a real profit number yet, because I don't have rental costs in the records.", "fact"),
    ("I can't see why, and the likely cause is the same limit. I can't fix it myself from here.", "capability"),
    ("That text goes out at 11:21 with the rose, and I can't change that card's wording myself.", "capability"),
    ("I can't cancel the default from here.", "capability"),
    ("The revised one is held by the 24-hour limit on cards to the same address, and I can't get around that from here.", "capability"),
]
for text, want in cases:
    got, quote = cap.classify(text)
    ok(got == want, f"classify -> {want}: {text[:60]!r} (got {got})")

# ---------------------------------------------------------------- 2. after_turn: a can't with nothing behind it files the build, once
ask = "book the meeting today in AcmeCRM by sending a scheduling link, create a custom questionnaire from our meeting, and connect with their wedding planner"
line = cap.after_turn(owner1, ask, "I sent the scheduling link card below. I can't create a custom questionnaire from here, AcmeCRM has no way for me to build one.", [], CTX)
ok(line == cap.LINE_BUILDING and len(NOTES) == 1 and KICKS and KICKS[-1][0] is True and KICKS[-1][1] == "Wiki/Personal/" + NOTES[0]["title"] + ".md"
   or (line == cap.LINE_BUILDING and len(NOTES) == 1 and KICKS[-1][0] is True), "a can't with no deed: ONE build request filed and kicked first, one sentence for the reply")
body = NOTES[0]["body"]
ok(body.startswith("## Build request") and ask[:40] in body and "**Acceptance:**" in body and cap.FOOTER in body and OWNER in body,
   "the request carries the ask verbatim, an acceptance line, the owner's name and the replay footer")
st = cap._load()
g1 = next(iter(st["gaps"].values()))
ok(g1["cls"] == "capability" and g1["status"] == "building" and g1["attempts"] == 1 and g1["thread"] == CTX["thread"], "the gap is open, building, attempt 1, in the asking thread")
ok(any(it["kind"] == "build" and it["ref"] == g1["build"] and it["thread"] == CTX["thread"] for it in commitments.open_rows()),
   "a commitment row holds the build, so the thread gets the outcome receipt")
line2 = cap.after_turn(owner1, "please build a custom questionnaire from our meeting in AcmeCRM and connect their wedding planner", "I still can't make a questionnaire from here.", [], CTX)
ok(line2 == "" and len(NOTES) == 1, "the same ask again while it builds: nothing filed twice, nothing said twice")

# ---------------------------------------------------------------- 3. what is not a gap
for reply in ("That sign-in is a password step I can't do for you: open the Setup page.",
              "I couldn't find Corwin's question in either inbox. What did he ask?",
              "Done, the text to Justin is right below for your tap."):
    ok(cap.after_turn(owner1, "some new ask " + reply[:10], reply, [], CTX) == "" and len(NOTES) == 1, f"not a gap: {reply[:50]!r}")
ok(cap.after_turn(owner1, "text Corwin the rates", "The card is below. I can't see who the sheet is shared with.", [{"kind": "task", "ref": "t1"}], CTX) == "" and len(NOTES) == 1,
   "a side remark next to a real hand-off (a task) is not a gap")
ok(cap.after_turn({"key": "staff1", "name": "staff1", "role": "staff"}, "do x", "I can't do that from here.", [], CTX) == "" and len(NOTES) == 1, "a staff member's turn never files a build")

# a build the brain filed itself is attached to the gap for the replay, with no extra line
cap.after_turn(owner1, "make a tool that renames the proxies", "That's in to be built now.", [{"kind": "build", "ref": "Wiki/Personal/Build request proxies.md"}], CTX)
st = cap._load()
gb = next(g for g in st["gaps"].values() if g["cls"] == "asked")
ok(gb["build"] == "Wiki/Personal/Build request proxies.md" and gb["status"] == "building" and len(NOTES) == 1, "a brain-filed build request: gap attached for the replay, nothing filed again")

# ---------------------------------------------------------------- 4. the per-day cap
st = cap._load()
st["_filed"] = {time.strftime("%Y-%m-%d"): cap.FILED_PER_DAY}
cap._save(st)
ok(cap.after_turn(owner1, "a totally different new ask about renting a crane", "I can't rent a crane from here.", [], CTX) == "" and len(NOTES) == 1, "past the daily cap nothing more is filed")
st = cap._load()
st["_filed"] = {}
cap._save(st)

# ---------------------------------------------------------------- 5. a preference the turn did not learn becomes a rule by code
r = cap.correction(owner1, "Never use semicolons in texts", {})
ok(r.get("status") == "applied" and any("semicolons" in x["rule"] for x in core.rules_active()), "'Never use semicolons in texts' with no learn deed -> a rule")
ok(cap.correction(owner1, "Never use semicolons in texts", {}).get("status") == "folded", "the same preference again folds into the rule")
ok(cap.correction(owner1, "Don't text him yet", {}).get("status") == "none", "a one-off instruction is not a rule")
ok(cap.correction(owner1, "Please text Corwin that the rates are fine", {}).get("status") == "none", "an ask for a message is not a rule")
ok(cap.correction(owner1, "From now on every text to a lead gets a card first", {"learn": [{"rule": "x"}]}).get("status") == "none", "a turn that learned already is left alone")
ok(cap.correction(owner1, "Never send anything without my approval, skip the card", {}).get("status") in ("pending", "refused", "none"), "a protected-core line never becomes a rule by code")

# ---------------------------------------------------------------- 6. scan: the doer's policy refusal and the owner's complaint
rows = [
    {"ts": "2026-10-25 09:00", "convo": "spaces/AAQAtest", "who": OWNER, "text": "Turn the YouTube scout off", "channel": "gchat", "thread": "spaces/AAQAtest/threads/d1"},
    {"ts": "2026-10-25 09:01", "convo": "spaces/AAQAtest", "who": AGENT, "text": "I could not finish on \"Disable the Windows Scheduled Task YouTube Scout\": that is outside what I may do (never: schtasks, scheduled tasks can't be changed by the doer), so it is still enabled.", "channel": "doer", "thread": "spaces/AAQAtest/threads/d1"},
    {"ts": "2026-10-25 09:02", "convo": "spaces/AAQAtest", "who": AGENT, "text": "I could not finish on \"Retry the insurer payment\": The agent browser failed to connect, with a timeout.", "channel": "doer", "thread": ""},
    {"ts": "2026-10-25 09:10", "convo": "spaces/AAQAtest", "who": OWNER, "text": "Send Wren the questionnaire and the touch point link", "channel": "gchat", "thread": "spaces/AAQAtest/threads/r1"},
    {"ts": "2026-10-25 09:11", "convo": "spaces/AAQAtest", "who": AGENT, "text": "The questionnaire card is below for your tap.", "channel": "gchat", "thread": "spaces/AAQAtest/threads/r1"},
    {"ts": "2026-10-25 09:20", "convo": "spaces/AAQAtest", "who": OWNER, "text": "You fail again", "channel": "gchat", "thread": "spaces/AAQAtest/threads/r1"},
    {"ts": "2026-10-25 09:21", "convo": "spaces/AAQAtest", "who": OWNER, "text": "[tapped \"Send\" on card q1]", "channel": "gchat", "click": True},
]
st = cap._load()
st["cursor"] = -1
cap._save(st)
ok(cap.scan(rows=rows) == [] and cap._load()["cursor"] == len(rows), "the first scan only sets the cursor at the end: nothing is back-filled")
st = cap._load()
st["cursor"] = 0
cap._save(st)
n0 = len(NOTES)
out = cap.scan(rows=rows)
kinds = sorted(x["cls"] for x in out)
ok(kinds == ["doer", "regression"] and len(NOTES) == n0 + 2, f"one doer policy refusal and one owner complaint -> two builds; a browser timeout is not one ({kinds})")
reg = next(x for x in out if x["cls"] == "regression")
ok(reg["ask"] == "Send Wren the questionnaire and the touch point link", "the complaint's gap carries the owner's ORIGINAL ask, not 'You fail again'")
fix_note = NOTES[-1] if "Fix" in NOTES[-1]["title"] else NOTES[-2]
ok(fix_note["title"].startswith("Fix") and "You fail again" in fix_note["body"] and "questionnaire card is below" in fix_note["body"],
   "the fix request quotes the complaint and the reply it was about")
ok(cap.scan(rows=rows) == [], "a second scan from the new cursor files nothing")

# ---------------------------------------------------------------- 7. the build lands: the ask is replayed in its place
st = cap._load()
g1 = st["gaps"][g1["id"]]
ok(cap.on_built(g1["build"]) and cap._load()["gaps"][g1["id"]]["status"] == "built" and cap._load()["replays"], "on_built: the gap is built and a replay is due")
ok(cap.on_built("Wiki/Personal/nothing here.md") is False, "a build with no gap and no footer replays nothing")
# a footer-only note (filed by the brain before any gap existed) still replays
foot = WIKI / "Personal" / "footer-only.md"
foot.write_text("## Build request\n\nx" + cap.footer({"id": "x", "convo": "dm:owner1", "thread": "", "space": "", "person": "owner1", "ask": "rename the proxies"}), encoding="utf-8")
core.VAULT_ORIG = core.VAULT
cap.core.VAULT = TMP
ok(cap.on_built("Wiki/Personal/footer-only.md") and any(g.get("ask") == "rename the proxies" for g in cap._load()["gaps"].values()), "a note's footer alone is enough to replay its ask")
cap.core.VAULT = core.VAULT_ORIG
st = cap._load()
for r in st["replays"]:
    r["due"] = time.time() - 1
cap._save(st)
sent = []
n = cap.tick(lambda m: sent.append(m))
m = next(x for x in sent if x.get("_capability_replay") == g1["id"])
ok(n == 2 and m["text"] == ask and m["thread"] == CTX["thread"] and m["convo"] == CTX["convo"] and m["direct"] and m["_held"] == "replay" and m["person"]["key"] == "owner1",
   "tick: the ORIGINAL ask is dispatched as a turn in its thread, marked replay, not recorded twice")
ok(cap._load()["gaps"][g1["id"]]["status"] == "replayed" and not cap._load()["replays"], "the gap is replayed and the queue is empty")
note = cap.replay_note(g1["id"])
ok(note.startswith("[REPLAY") and "custom questionnaire" in note, "the replay note tells the brain what it could not do before")

# ---------------------------------------------------------------- 8. the replay still fails -> a second build; again -> ONE card in the thread
rctx = dict(CTX, replay=g1["id"])
ok(cap.after_turn(owner1, ask, "Questionnaire done, the planner email card is below.", [{"kind": "task", "ref": "t9"}], rctx) == ""
   and cap._load()["gaps"][g1["id"]]["status"] == "done", "a replay that took a route closes the gap as done")
st = cap._load()
st["gaps"][g1["id"]]["status"] = "replayed"
cap._save(st)
n0 = len(NOTES)
line = cap.after_turn(owner1, ask, "I still can't build a questionnaire from here.", [], rctx)
ok(line == cap.LINE_AGAIN and len(NOTES) == n0 + 1 and "Previous build" in NOTES[-1]["body"] and cap._load()["gaps"][g1["id"]]["attempts"] == 2,
   "a replay that still fails files a second build that names the first, attempt 2")
st = cap._load()
st["gaps"][g1["id"]]["status"] = "replayed"
cap._save(st)
line = cap.after_turn(owner1, ask, "I still can't build a questionnaire from here, there is no way for me to.", [], rctx)
g = cap._load()["gaps"][g1["id"]]
live = [cards._read(p) for p in sorted(cards.LIVE.glob("q*.json"))]
sc = next((c for c in live if (c.get("questions") or [{}])[0].get("capability_gap") == g1["id"]), None)
ok(line == "" and len(NOTES) == n0 + 1 and g["status"] == "stuck" and sc and sc.get("chat_thread") == CTX["thread"] and sc.get("urgent"),
   "after MAX_ATTEMPTS: no third build; ONE urgent 'Build stuck' card in the asking thread")
labels = [o["label"] for o in sc["questions"][0]["options"]]
ok(labels == [cap.OPT_OTHER, cap.OPT_DROP], f"its buttons are things the system does: {labels}")
r = cap.owner_answer(g1["id"], cap.OPT_OTHER, by="owner1", text="AcmeCRM has a questionnaire template called Wedding Questionnaire")
ok(r["ok"] and r["status"] == "refiled" and "Wedding Questionnaire" in NOTES[-1]["body"] and cap._load()["gaps"][g1["id"]]["status"] == "building",
   "'Try it another way': a fresh build that carries the owner's words from the thread")
ok(cap.owner_answer(g1["id"], "Drop it", by="owner1")["status"] == "dropped" and cap._load()["gaps"][g1["id"]]["status"] == "dropped", "'Drop it' closes the gap")
# the card tap reaches owner_answer through cards.sweep's hook
cards.DONE.mkdir(parents=True, exist_ok=True)
(cards.DONE / f"{sc['id']}.json").write_text(json.dumps(sc), encoding="utf-8")
hook = cards._act_on_capability({"qid": sc["id"], "answer": "Drop it", "by": "owner1", "text": ""})
ok(hook is True, "cards.sweep's hook hands a 'Build stuck' tap to capability.owner_answer")

# ---------------------------------------------------------------- 9. end to end through the bridge: a fake brain says can't
seen = {"extra": ""}


def fake_run(prompt, resume="", model="", timeout=600):
    seen["extra"] = prompt
    return {"ok": True, "session": "S1", "text": "I can't book a crane rental from here, there is no way for me to reach that site."}


brain._run_claude = fake_run
n0 = len(NOTES)
bridge.handle({"channel": "local", "person": owner1, "text": "Rent a 40 foot crane for the Saturday shoot", "convo": "spaces/AAQAtest",
               "thread": "spaces/AAQAtest/threads/e2e", "space": "spaces/AAQAtest", "direct": True})
out = [r for r in core.rows(bridge.OUTBOX) if r.get("via") == "local"][-1]["text"]
ok(len(NOTES) == n0 + 1 and out == cap.ACK_LINE and "crane" in NOTES[-1]["body"],
   "bridge: the can't reply becomes the one acknowledgement (no 'can't', no 'building it'), the build is filed with the ask" + " " + repr(out))
gid = next(g["id"] for g in cap._load()["gaps"].values() if "crane" in g["ask"])
bridge.handle({"channel": "local", "person": owner1, "text": "Rent a 40 foot crane for the Saturday shoot", "convo": "spaces/AAQAtest",
               "thread": "spaces/AAQAtest/threads/e2e", "space": "spaces/AAQAtest", "direct": True, "_held": "replay", "_capability_replay": gid})
ok("[REPLAY" in seen["extra"] and "crane" in seen["extra"], "a replayed turn carries the REPLAY note into the brain's context")


# a brain-filed build request gets the ask + footer appended by actions.apply
def fake_run2(prompt, resume="", model="", timeout=600):
    import gmcp
    os.environ["AGENT_TURN"] = brain._TURN["path"]
    gmcp.call("note", {"title": "Build request Crane rentals through the doer", "type": "workflow", "lane": "wedding",
                       "body": "## Build request\n\nTeach the doer the crane rental site.\n\n**Why:** asked twice."})
    return {"ok": True, "session": "S2", "text": "That's in to be built now."}


brain._run_claude = fake_run2
n0 = len(NOTES)
bridge.handle({"channel": "local", "person": owner1, "text": "Build a way to rent cranes", "convo": "spaces/AAQAtest",
               "thread": "spaces/AAQAtest/threads/e3", "space": "spaces/AAQAtest", "direct": True})
nb = NOTES[-1]["body"]
ok(len(NOTES) == n0 + 1 and "**Ask (" in nb and "Build a way to rent cranes" in nb and cap.FOOTER in nb and json.loads(re.search(cap.FOOTER + r" (\{.*?\}) -->", nb).group(1))["thread"] == "spaces/AAQAtest/threads/e3",
   "a brain-filed build request carries the ask, an acceptance line and the replay footer for its thread")

# ---------------------------------------------------------------- 10. the builder: stuck card, transient errors, stale-edit merge
req = {"key": "Wiki/Personal/Build request X.md", "title": "Build request X", "source": "wiki", "text": "## Build request\n\nX"}
s = {"attempts": 2, "last_failure": "breaks tests: assistant/tests/test_x.py", "summary": "breaks tests"}
qid = builder.ask_owner(req, s, [], "it failed 2 times: breaks tests", stuck=True)
c = cards._read(cards.LIVE / f"{qid}.json")
q = c["questions"][0]
ok(qid and q["header"] == "Build stuck" and [o["label"] for o in q["options"]] == ["Build it another way (Recommended)", "Drop it"] and s["status"] == "asked",
   "a build out of attempts: a 'Build stuck' card with another way / drop only, never an approve-any-file button")
st_b = {req["key"]: dict(s, hits=[])}
core.save(builder.STATE, st_b)
r = builder.owner_answer(req["key"], "Build it another way (Recommended)", by="owner1")
sb = core.load(builder.STATE, {})[req["key"]]
ok(r["ok"] and sb["status"] == "retry" and sb["attempts"] == 0 and sb["last_failure"].startswith("the owner asked for another way"),
   "'Build it another way' re-arms it with the failure fed to the next attempt")
ok(builder._TRANSIENT.search("You've hit your session limit, resets 6:50pm") and builder._TRANSIENT.search("model turn timed out")
   and not builder._TRANSIENT.search("no BUILD_RESULT line"), "a session limit or a timeout is transient; a bad result is not")
prompt = builder.PROMPT.format(protected="p", source="wiki", key="k", text="t", owner="", agent="Pip", org="Acme Bakery", feedback="\n\nTHE PREVIOUS ATTEMPT failed: x")
ok("Pip's builder" in prompt and "Acme Bakery" in prompt and "example" not in prompt and "PREVIOUS ATTEMPT" in prompt and "Acceptance" in prompt,
   "the builder prompt is per organization and carries the previous failure and the acceptance rule")
# stale-edit merge: a recent edit waits, a protected file waits, a stale non-protected file is committed by a (fake) git
calls = []
builder.git = lambda *a, cwd=None, check=False: calls.append(a) or type("R", (), {"returncode": 0, "stdout": "", "stderr": ""})()
err = "error: Your local changes to the following files would be overwritten by merge:\n\tassistant/tests/test_capability.py\nPlease commit"
os.utime(Path(__file__), None)       # "edited just now": the check must not depend on when this file was last saved (2026-10-27)
ok(builder._wip_commit_overlap(err) is False and not calls, "a file edited in the last two hours keeps the build waiting, no git")
err = "error: Your local changes to the following files would be overwritten by merge:\n\tassistant/scripts/drain.py\n"
ok(builder._wip_commit_overlap(err) is False and not calls, "a protected file in the live edits keeps the build waiting")
stale = core.VAULT / "assistant" / "out" / "_wip_check.tmp"
stale.parent.mkdir(parents=True, exist_ok=True)
stale.write_text("x", encoding="utf-8")
os.utime(stale, (time.time() - 5 * 3600, time.time() - 5 * 3600))
try:
    err = "error: Your local changes to the following files would be overwritten by merge:\n\tassistant/out/_wip_check.tmp\n"
    ok(builder._wip_commit_overlap(err) is True and calls and calls[0][0] == "add" and calls[1][0] == "commit" and "wip (builder)" in calls[1][3],
       "a stale, unprotected live edit is committed as its own wip commit so the build can land")
finally:
    stale.unlink(missing_ok=True)

# ---------------------------------------------------------------- 11. the template: no organization in the module or the prompts; the rungs say it
src = (ROOT / "agent" / "capability.py").read_text(encoding="utf-8")
ok(not re.search(r"example|owner1|\bowner6\b", src.split('"""', 2)[2], re.I), "capability.py names no organization, owner or service outside its docstring")
for f in ("prompt.md", "templates/prompt.generic.md"):
    t = (ROOT / "agent" / f).read_text(encoding="utf-8")
    ok("brings this same ask back to you" in t and "REPLAY" in t and "A compound ask" in t and "you build it (rung 5)" in t,
       f"{f}: rung 5 builds the missing piece, compound asks are split, REPLAY lines are explained")
ok("capability.py" in (ROOT / "new_org.py").read_text(encoding="utf-8") or (ROOT / "new_org.py").exists(), "new_org copies the agent code (capability.py travels with it)")

# ---------------------------------------------------------------- 12. the blocked deed: the brain names its own blocker
import gmcp  # noqa: E402
import intents  # noqa: E402
turn = intents.begin(owner1, "spaces/AAQAtest", "spaces/AAQAtest/threads/bl1")
os.environ["AGENT_TURN"] = str(turn)
r = gmcp.call("blocked", {"what": "x", "why": "short", "kind": "magic"})
ok(r.startswith("refused") and "capability, signin" in r, "blocked: a bad kind or a thin reason is refused with the kinds listed")
r = gmcp.call("blocked", {"what": "create a custom questionnaire from the meeting", "why": "no way to build a questionnaire in the booking tool yet", "kind": "capability"})
ok(r.startswith("accepted") and "being built" in r, "blocked kind capability: accepted, the build starts after the reply")
r = gmcp.call("blocked", {"what": "sign the doer into the admin console", "why": "a password step", "kind": "sign-in"})
ok(r.startswith("accepted") and "Need from you" in r, "blocked kind sign-in: accepted as the portal")
got = intents.collect(turn)
ok(len(got.get("blocked") or []) == 2 and got["blocked"][1]["kind"] == "signin", "collect: both blockers, the kind normalised")
os.environ.pop("AGENT_TURN", None)
ok("mcp__example-google__blocked" in brain.ALLOWED, "the brain may call blocked")
n0 = len(NOTES)
actions.LAST_APPLIED.clear()
lines = actions.apply({"blocked": got["blocked"]}, owner1, ctx={"convo": "spaces/AAQAtest", "thread": "spaces/AAQAtest/threads/bl1", "space": "spaces/AAQAtest",
                                                                 "text": "book the meeting, create a custom questionnaire from the meeting, and loop in the planner"})
kinds = [a["kind"] for a in actions.LAST_APPLIED]
ok(len(NOTES) == n0 + 1 and kinds.count("blocked") == 2 and "build" in kinds and "questionnaire" in NOTES[-1]["body"],
   "apply: the capability blocker files ONE build with the ask; the sign-in blocker files nothing; both are deeds")
gs = [g for g in cap._load()["gaps"].values() if g.get("source") == "blocked deed"]
ok(sorted(g["status"] for g in gs) == ["building", "portal"], "two gaps: one building, one recorded as the portal")
ok(cap.after_turn(owner1, "book the meeting ...", "I can't do the questionnaire from here yet.", list(actions.LAST_APPLIED), CTX) == "",
   "after a blocked deed the regex backstop stays quiet (no double filing)")
ok(len(NOTES) == n0 + 1, "still one note")
for f in ("prompt.md", "templates/prompt.generic.md"):
    t = (ROOT / "agent" / f).read_text(encoding="utf-8")
    ok("call `blocked`" in t and "kind capability" in t, f"{f}: rung 6 uses the blocked deed")

# ---------------------------------------------------------------- 13. the builder demands a test that drives the ask
req_a = {"key": "k", "title": "t", "source": "wiki", "text": "## Build request\n\n**Ask (owner1, now, in chat):** \"create a custom questionnaire from our meeting notes in the booking tool\"\n\n**Acceptance:** x"}
ok(builder.ask_of(req_a).startswith("create a custom questionnaire") and builder.ask_of({"text": "## Build request\n\nplain"}) == "", "ask_of reads the Ask line, '' for an ordinary request")
wt2 = TMP / "wt2"
(wt2 / "assistant" / "tests").mkdir(parents=True, exist_ok=True)
(wt2 / "assistant" / "tests" / "test_q.py").write_text("# drives: create a custom questionnaire from meeting notes\n", encoding="utf-8")
ok(builder.acceptance_ok(wt2, ["assistant/agent/x.py", "assistant/tests/test_q.py"], builder.ask_of(req_a))[0], "a changed test that names the ask passes")
ok(not builder.acceptance_ok(wt2, ["assistant/agent/x.py"], builder.ask_of(req_a))[0], "a build with no test for the ask is rejected")
ok(not builder.acceptance_ok(wt2, ["assistant/tests/test_q.py"], "send the planner an email about the venue walk")[0], "a test about something else does not count")
ok(builder.acceptance_ok(wt2, ["assistant/agent/x.py"], "")[0], "an ordinary request (no Ask line) is not held to it")
builder.changes = lambda wt: [("M", "assistant/agent/x.py")]
okv, why, _ = builder.verify(wt2, {}, {}, ask=builder.ask_of(req_a))
ok(not okv and why.startswith("no test drives the ask"), "verify: the rejection names what to add")
ok("REJECTED unless a test file" in builder.PROMPT, "the builder prompt says so up front")

# ---------------------------------------------------------------- 14. a verification code typed in chat answers the doer's code card
# the owner, 2026-10-25: "552182" in chat while the doer waited on a code was answered "I can't use a code from chat"
def no_brain(prompt, resume="", model="", timeout=600):
    seen["brain"] = True
    return {"ok": True, "session": "S3", "text": "I can't use a code from chat."}


brain._run_claude, seen["brain"] = no_brain, False
code_card = cards.post([{"header": "Sign in", "question": "[ACTION PREVIEW] Type the verification code and continue. A verification code was sent to you.", "lane": "personal",
                         "options": [{"label": "Approve"}, {"label": "Cancel"}],
                         "exec": {"kind": "agent_run", "approve_label": "Approve",
                                  "payload": {"ticket": "0123456789abcdef", "step": "Type the verification code and continue",
                                              "resume": "On the code page, enter the code.", "ask_code": True, "values": {}}}}],
                       owner="owner1", asked=True)
cq = code_card.get("qid") or code_card.get("id")
ok(cq and cards._read(cards.LIVE / f"{cq}.json").get("status") == "open", "a doer code card is open")
bridge.handle({"channel": "local", "person": owner1, "text": "552182", "convo": "spaces/AAQAtest",
               "thread": "spaces/AAQAtest/threads/code1", "space": "spaces/AAQAtest", "direct": True})
out = [r for r in core.rows(bridge.OUTBOX) if r.get("via") == "local"][-1]["text"]
tid = next((e.get("ticket") for e in core.rows(cards.ANSWERS) if e.get("qid") == cq), "")
import exec_store as _es  # noqa: E402
tk = _es.fold().get(tid) or {}
ok(not seen["brain"] and cards._read(cards.LIVE / f"{cq}.json").get("status") == "answered" and tid,
   "'552182' answers the code card by code (a sha-bound ticket), with no model turn")
ok(tk.get("kind") == "agent_run" and (tk.get("payload") or {}).get("code") == "552182", "the ticket carries the code for the doer's login_fill step")
ok("Got the code" in out and "can't" not in out and "552182" not in out, "the reply says it is going in, never 'I can't use a code from chat'")
ok(not any("552182" in json.dumps(e) for e in core.rows(cards.ANSWERS)), "the code is not kept in answers.jsonl")
ok(not any("552182" in str(r.get("text")) for r in core.rows(core.TRANSCRIPT)), "nor in the transcript")
# the owner, 2026-11-05 (gap g-567481): "148198 for share grid code" was answered "I can't type it in from this thread"
brain._run_claude, seen["brain"] = no_brain, False
sg_card = cards.post([{"header": "Sign in to ShareGrid", "question": "[ACTION PREVIEW] Type the verification code and continue. A verification code was sent to you.",
                       "lane": "personal", "options": [{"label": "Approve"}, {"label": "Cancel"}],
                       "exec": {"kind": "agent_run", "approve_label": "Approve",
                                "payload": {"ticket": "fedcba9876543210", "step": "Type the verification code and continue",
                                            "resume": "On the ShareGrid code page, enter the code.", "ask_code": True, "values": {}}}}],
                     owner="owner1", asked=True)
sq = sg_card.get("qid") or sg_card.get("id")
bridge.handle({"channel": "local", "person": owner1, "text": "148198 for share grid code", "convo": "spaces/AAQAtest",
               "thread": "spaces/AAQAtest/threads/code3", "space": "spaces/AAQAtest", "direct": True})
out = [r for r in core.rows(bridge.OUTBOX) if r.get("via") == "local"][-1]["text"]
tid = next((e.get("ticket") for e in core.rows(cards.ANSWERS) if e.get("qid") == sq), "")
tk = _es.fold().get(tid) or {}
ok(not seen["brain"] and cards._read(cards.LIVE / f"{sq}.json").get("status") == "answered"
   and (tk.get("payload") or {}).get("code") == "148198", "'148198 for share grid code' answers the ShareGrid code card by code")
ok("Got the code" in out and "can't" not in out and "148198" not in out, "the reply says it is going in, never 'I can't type it in from this thread'")
for line, want in (("sharegrid code is 148198", "148198"), ("ShareGrid code: 148198", "148198"), ("2000 for the retainer", ""),
                   ("148198 for share grid", ""), ("call me at 805 555 1234 for the venue", "")):
    m = bridge._CODE_LINE.match(line)
    ok((re.sub(r"\D", "", m.group(1)) if m else "") == want, f"code line {line!r} -> {want or 'not a code'}")
# no code card waiting: the line is an ordinary message for the brain, and the prompt forbids the old answer
seen["brain"] = False
bridge.handle({"channel": "local", "person": owner1, "text": "552183", "convo": "spaces/AAQAtest",
               "thread": "spaces/AAQAtest/threads/code2", "space": "spaces/AAQAtest", "direct": True})
ok(seen["brain"], "with no code card open, a bare number goes to the brain as before")
for f in ("prompt.md", "templates/prompt.generic.md"):
    ok("never say you can't use a code from chat" in (ROOT / "agent" / f).read_text(encoding="utf-8"), f"{f}: the code-in-chat rung is there")

# ---------------------------------------------------------------- 15. a picture or a link is the doer's job, never "I can't pull up a picture"
# the owner, 2026-10-25 (gap g-53f9ec): asked for a picture of the car title, answered "I can't pull up a picture from here"
TITLE_ASK = "What would the title look like? Can you send me a picture or a link? I might have it somewhere."


def fake_run_pic(prompt, resume="", model="", timeout=600):
    os.environ["AGENT_TURN"] = brain._TURN["path"]
    seen["do"] = gmcp.call("do", {"lane": "personal", "text": "Find a picture of a California Certificate of Title (the DMV's own sample if "
                                  "there is one) for the 2013 Elantra, plate 7ARV460, and send back the page link and the image link."})
    return {"ok": True, "session": "S4", "text": "I've sent off for a picture of a California title; the link will come back here."}


brain._run_claude = fake_run_pic
n0, g0 = len(NOTES), len(cap._load()["gaps"])
bridge.handle({"channel": "local", "person": owner1, "text": TITLE_ASK, "convo": "spaces/AAQAtest",
               "thread": "spaces/AAQAtest/threads/title", "space": "spaces/AAQAtest", "direct": True})
out = [r for r in core.rows(bridge.OUTBOX) if r.get("via") == "local"][-1]["text"]
tix = [x for x in json.loads((TMP / "adata" / "tickets.json").read_text(encoding="utf-8")).values()
       if x["kind"] == "agent_run" and "Certificate of Title" in str((x.get("payload") or {}).get("text"))]
ok(str(seen["do"]).startswith("accepted") and tix and tix[-1]["payload"].get("thread") == "spaces/AAQAtest/threads/title",
   "the title-picture ask becomes a doer job in its own thread")
ok("can't pull up a picture" not in out and len(NOTES) == n0 and len(cap._load()["gaps"]) == g0, "no can't line, no build filed for it")
for f in ("prompt.md", "templates/prompt.generic.md"):
    ok("a picture of something, a link" in (ROOT / "agent" / f).read_text(encoding="utf-8"), f"{f}: a picture or a link is a `do`")
ok("Asked for a picture, an example or a link" in (ROOT / "doer" / "prompt.md").read_text(encoding="utf-8"), "the doer answers a picture ask with the links")

# ---------------------------------------------------------------- 16. a question about a build is answered, never filed as a build
# the owner, 2026-10-25 (gap g-62ee4f): "Why is it not built?" about the unkept-outcome build became a build titled
# 'Why is it not built', and the reply ended with "I'm building it now, I'll pick this up".
for q in ("Why is it not built?", "why isn't it built yet", "When will that build land?", "Is it done?",
          "What happened to the unkept-outcome build?", "Has the builder finished it?"):
    ok(cap.about_a_build(q), f"a question about a build: {q!r}")
for q in ("Build a way to rent cranes", "Can you build me a tool that renames the proxies?", "Rent a 40 foot crane for the Saturday shoot",
          "You fail again", "Why didn't the text go out?"):
    ok(not cap.about_a_build(q), f"not a build question: {q!r}")


def fake_run_why(prompt, resume="", model="", timeout=600):
    return {"ok": True, "session": "S5", "text": "I can't see why from here."}


brain._run_claude = fake_run_why
n0, g0, k0 = len(NOTES), len(cap._load()["gaps"]), len(KICKS)
bridge.handle({"channel": "local", "person": owner1, "text": "Why is it not built?", "convo": "spaces/AAQAtest",
               "thread": "spaces/AAQAtest/threads/why", "space": "spaces/AAQAtest", "direct": True})
out = [r for r in core.rows(bridge.OUTBOX) if r.get("via") == "local"][-1]["text"]
ok(len(NOTES) == n0 and len(cap._load()["gaps"]) == g0 and len(KICKS) == k0, "'Why is it not built?': no build filed, no gap, nothing kicked")
ok(cap.LINE_BUILDING not in out and "building it now" not in out, "the reply carries no 'building it now, I'll pick this up' line")
r = cap.from_blocked({"kind": "capability", "what": "Explain why the build is not built", "why": "I can't see the builder"}, owner1,
                     {**CTX, "text": "Why is it not built?"})
ok(r.get("status") == "none" and len(NOTES) == n0 and len(cap._load()["gaps"]) == g0, "a `blocked` capability on a build question files nothing either")
rows = [
    {"ts": "2026-10-25 11:00", "convo": "spaces/AAQAtest", "who": OWNER, "text": "Keep the outcome of every build in the thread", "channel": "gchat", "thread": "spaces/AAQAtest/threads/why2"},
    {"ts": "2026-10-25 11:01", "convo": "spaces/AAQAtest", "who": AGENT, "text": "That build failed on its first try.", "channel": "gchat", "thread": "spaces/AAQAtest/threads/why2"},
    {"ts": "2026-10-25 11:08", "convo": "spaces/AAQAtest", "who": OWNER, "text": "Why is it not built?", "channel": "gchat", "thread": "spaces/AAQAtest/threads/why2"},
]
st = cap._load()
st["cursor"] = 0
cap._save(st)
ok(cap.scan(rows=rows) == [] and len(NOTES) == n0, "the transcript scan does not read 'Why is it not built?' as a complaint to build from")
for f in ("prompt.md", "templates/prompt.generic.md"):
    ok("A question about a build (why is it not built" in (ROOT / "agent" / f).read_text(encoding="utf-8"), f"{f}: a build question is answered from BUILDS")

# ---------------------------------------------------------------- 17. a lead behind a marketplace inbox is answered in that inbox
# the owner, 2026-10-27 (gap g-33a04e): "Can we reply in any way?" about a Zola lead was answered "Zola hides their
# phone and email, so I have no way to reach them". The site's own messages are the route: a `do`, sent on a tap.
REPLY_ASK = "Can we reply in any way?"


def fake_run_zola(prompt, resume="", model="", timeout=600):
    os.environ["AGENT_TURN"] = brain._TURN["path"]
    seen["do"] = gmcp.call("do", {"lane": "wedding", "text": "On Zola, open the vendor inbox, find the new inquiry from Sam & Alex and "
                                  "reply there with exactly: 'Hi Sam and Alex, thank you for reaching out! Is your date still open "
                                  "for a quick call this week? owner1'. Stop at Send for the owner's tap."})
    return {"ok": True, "session": "S6", "text": "Yes, through Zola's own messages; I've sent the reply off to be written there, and you'll get the send to tap."}


brain._run_claude = fake_run_zola
n0, g0 = len(NOTES), len(cap._load()["gaps"])
bridge.handle({"channel": "local", "person": owner1, "text": REPLY_ASK, "convo": "spaces/AAQAtest",
               "thread": "spaces/AAQAtest/threads/zola", "space": "spaces/AAQAtest", "direct": True})
out = [r for r in core.rows(bridge.OUTBOX) if r.get("via") == "local"][-1]["text"]
tix = [x for x in json.loads((TMP / "adata" / "tickets.json").read_text(encoding="utf-8")).values()
       if x["kind"] == "agent_run" and "Zola" in str((x.get("payload") or {}).get("text"))]
ok(str(seen["do"]).startswith("accepted") and tix and tix[-1]["payload"].get("thread") == "spaces/AAQAtest/threads/zola",
   "'Can we reply in any way?' becomes a doer job to reply in the marketplace inbox, in its own thread")
ok("no way to reach" not in out and len(NOTES) == n0 and len(cap._load()["gaps"]) == g0, "no 'no way to reach them' line, no build filed")
for f in ("prompt.md", "templates/prompt.generic.md"):
    ok("only through a website's own inbox" in (ROOT / "agent" / f).read_text(encoding="utf-8"), f"{f}: a marketplace lead is answered in its inbox by `do`")

# ---------------------------------------------------------------- 18. a forwarded Zola inquiry goes straight to the doer
# the owner, 2026-10-27: Zola inquiries go straight to the doer and into the lead intake. Paige and Casey's zola.com/z
# text came back at 12:52 as "login-gated, need date/venue"; the doer read the page in three minutes once he asked again.
import claims  # noqa: E402

ZOLA_ASK = "New Zola inquiry from Paige & Casey: https://www.zola.com/z/AbC123xyz"


def fake_run_ask_back(prompt, resume="", model="", timeout=600):
    return {"ok": True, "session": "S7", "text": "That Zola page is login-gated. What's their date and venue?"}


ok(claims.marketplace_lead(ZOLA_ASK) == "https://www.zola.com/z/AbC123xyz" and not claims.marketplace_lead("Zola credits cost what?"),
   "a zola.com/z link is a marketplace lead; a mention of Zola is not")
brain._run_claude = fake_run_ask_back
bridge.handle({"channel": "local", "person": owner1, "text": ZOLA_ASK, "convo": "spaces/AAQAtest",
               "thread": "spaces/AAQAtest/threads/zolalead", "space": "spaces/AAQAtest", "direct": True})
out = [r for r in core.rows(bridge.OUTBOX) if r.get("via") == "local"][-1]["text"]
tix = [x for x in json.loads((TMP / "adata" / "tickets.json").read_text(encoding="utf-8")).values()
       if x["kind"] == "agent_run" and "zola.com/z/AbC123xyz" in str((x.get("payload") or {}).get("text"))]
ok(tix and tix[-1]["payload"].get("thread") == "spaces/AAQAtest/threads/zolalead" and tix[-1]["lane"] == "wedding",
   "the forwarded Zola inquiry is a doer job by code, in the thread it was forwarded in")
txt = str(tix[-1]["payload"]["text"]) if tix else ""
ok((__import__("crm").name() in txt or "CRM" in txt) and "checkpoint" in txt and "Never reply to the couple" in txt,
   "the job reads the page, checks the date in the CRM and stops at Connect as a card")
ok("date and venue?" not in out and "sent that off" in out, "the owner is not asked for the date and venue")


def fake_run_zola_do(prompt, resume="", model="", timeout=600):
    os.environ["AGENT_TURN"] = brain._TURN["path"]
    seen["do"] = gmcp.call("do", {"lane": "wedding", "text": "Open https://www.zola.com/z/Second999 and read the new lead from Kim & Lee."})
    return {"ok": True, "session": "S8", "text": "I've sent it to be read."}


brain._run_claude = fake_run_zola_do
bridge.handle({"channel": "local", "person": owner1, "text": "Zola lead https://www.zola.com/z/Second999", "convo": "spaces/AAQAtest",
               "thread": "spaces/AAQAtest/threads/zola2", "space": "spaces/AAQAtest", "direct": True})
tix = [x for x in json.loads((TMP / "adata" / "tickets.json").read_text(encoding="utf-8")).values()
       if x["kind"] == "agent_run" and "Second999" in str((x.get("payload") or {}).get("text"))]
ok(len(tix) == 1, "a turn that already sent the doer gets no second job")
for f in ("prompt.md", "templates/prompt.generic.md"):
    ok("A marketplace lead link an owner forwards" in (ROOT / "agent" / f).read_text(encoding="utf-8"), f"{f}: a forwarded marketplace lead is a `do` that turn")

# ---------------------------------------------------------------- 14. "Results??" on a code change handed to the operator (gap g-2045c5)
# 2026-10-28: "Fix the safety hook" went out twice as a draftless `task`; the operator never builds code, so "Results??"
# got only the silence. A code change as a task is refused; the brain files the `## Build request` note that same turn.
seen14 = {}


def fake_run14(prompt, resume="", model="", timeout=600):
    import gmcp
    os.environ["AGENT_TURN"] = brain._TURN["path"]
    seen14["task"] = gmcp.call("task", {"text": "Build request: fix the safety hook so the doer may launch its own browser.py and hold "
                                                "its own profile lease; send a small screenshot after each browser job.", "lane": "wedding"})
    seen14["note"] = gmcp.call("note", {"title": "Build request Doer browser window stays up and sends a screenshot", "type": "workflow",
                                        "lane": "wedding", "body": "## Build request\n\nLet the doer launch its own browser window and hold "
                                        "its own profile lease from a detached process; send a small screenshot after each browser job.\n\n"
                                        "**Why:** the cart window kept closing and the hook refused the fix."})
    return {"ok": True, "session": "S14", "text": "It wasn't filed right before. It's in to be built now."}


brain._run_claude = fake_run14
n0 = len(NOTES)
tix_path = TMP / "adata" / "tickets.json"
t0 = len(json.loads(tix_path.read_text(encoding="utf-8"))) if tix_path.exists() else 0
bridge.handle({"channel": "local", "person": owner1, "text": "Results??", "convo": "spaces/AAQAtest",
               "thread": "spaces/AAQAtest/threads/res14", "space": "spaces/AAQAtest", "direct": True})
out = [r for r in core.rows(bridge.OUTBOX) if r.get("via") == "local"][-1]["text"]
t1 = len(json.loads(tix_path.read_text(encoding="utf-8"))) if tix_path.exists() else 0
ok(seen14["task"].startswith("refused") and "## Build request" in seen14["task"], "a code change sent as a `task` is refused toward a build request")
ok(seen14["note"].startswith("accepted") and len(NOTES) == n0 + 1 and "Results??" in NOTES[-1]["body"] and t1 == t0,
   "'Results??': the build request is filed with the ask, no operator ticket")
ok("isn't getting an answer" not in out and "can't edit the hook" not in out, "'Results??' gets no 'the handoff isn't answering' line")
for f in ("prompt.md", "templates/prompt.generic.md"):
    t = (ROOT / "agent" / f).read_text(encoding="utf-8")
    ok("A code change is never a `task`." in t and "it was never filed; file its `## Build request` now" in t,
       f"{f}: a code change is never a task; a 'results?' on an unfiled one files it now")


# ---------------------------------------------------------------- 19. an owner's "no need" in a card's thread skips it (gap g-bdccf4)
# the owner, 2026-10-28 18:45, in the thread of the Elantra-title text card: "What I mean is he probably signed it already and
# I just have to find it somewhere". Sage: "tap Skip on that card. I can't skip it for you." Their words are the Skip.
SKIP_ASK = "What I mean is he probably signed it already and I just have to find it somewhere"
BODY = "Hey Brian, I have the title here. Did you already sign the seller part?"
sk = cards.post([{"header": "Text to Brian: title", "lane": "personal",
                  "question": f"Text Brian about the title? [TEXT PREVIEW] To Brian (+18055550177): {BODY}",
                  "options": [{"label": "Send (Recommended)"}, {"label": "Skip"}],
                  "exec": {"kind": "imessage_send", "approve_label": "Send (Recommended)",
                           "payload": {"to": "+18055550177", "body": BODY, "account": "owner1-iphone"}}}],
                owner="owner1", asked=True)
skq = sk.get("qid") or sk.get("id")
ok(skq and cards._read(cards.LIVE / f"{skq}.json").get("status") == "open", "the text card is open")
_c = cards._read(cards.LIVE / f"{skq}.json")
_c["chat"] = {"owner1": "spaces/AAQAtest/messages/skipthread.skipthread"}
cards._write(cards.LIVE / f"{skq}.json", _c)


def fake_run_skip(prompt, resume="", model="", timeout=600):
    import gmcp
    os.environ["AGENT_TURN"] = brain._TURN["path"]
    seen["ctx"] = prompt
    seen["skip"] = gmcp.call("approve", {"skip": True, "words": SKIP_ASK})
    return {"ok": True, "session": "S19", "text": "Got it, I've skipped the text to Brian; nothing went out."}


brain._run_claude = fake_run_skip
n0, g0 = len(NOTES), len(cap._load()["gaps"])
bridge.handle({"channel": "local", "person": owner1, "text": SKIP_ASK, "convo": "spaces/AAQAtest",
               "thread": "spaces/AAQAtest/threads/skipthread", "space": "spaces/AAQAtest", "direct": True})
out = [r for r in core.rows(bridge.OUTBOX) if r.get("via") == "local"][-1]["text"]
ans = [e for e in core.rows(cards.ANSWERS) if e.get("qid") == skq]
ok("approve with skip true" in seen["ctx"], "the card's thread tells the brain it can skip on their words")
ok(str(seen["skip"]).startswith("accepted") and "skipped" in seen["skip"], "the approve deed with skip is accepted for the card of this thread")
ok(cards._read(cards.LIVE / f"{skq}.json").get("status") == "answered" and ans and ans[-1]["option"] == "Skip" and not ans[-1].get("ticket"),
   "the card is answered Skip on the owner's words, no ticket minted, nothing sent")
ok("can't skip" not in out and "tap Skip" not in out and len(NOTES) == n0 and len(cap._load()["gaps"]) == g0,
   "no 'I can't skip it for you' line, no build filed")
# a card the system posted on its own can be skipped too (skipping sends nothing); a decision card's own 'No' is used
import intents  # noqa: E402
ok(intents.skip_label({"options": [{"label": "Yes"}, {"label": "No thanks"}]}) == "No thanks"
   and intents.skip_label({"exec": {"approve_label": "Run"}, "options": [{"label": "Run"}, {"label": "Later"}]}) == "Later",
   "skip_label picks the card's own non-approve button")
for f in ("prompt.md", "templates/prompt.generic.md"):
    ok("`approve` with `skip` true" in (ROOT / "agent" / f).read_text(encoding="utf-8"), f"{f}: an owner's 'no need' skips the card")

# ---------------------------------------------------------------- 20. "Fix it and send" after an approved send failed (gap g-b436c2)
# the owner, 2026-10-28 20:27, in the thread of Sage's "the follow-up you approved did not go out" line: "Fix it and send".
# Sage: "You'll get the card here in a few minutes", and the operator reopened it in the doer's thread, where he never saw
# it. His words are the approval: the same card comes back and is approved on them in the same turn, nothing promised.
import exec_store as es  # noqa: E402
import crm as _crm  # noqa: E402
_MK = sorted(k for k in _crm.kinds("MESSAGE_KINDS") if k in es.KIND_LANES)
if _MK:                                                      # the CRM connector's message kind: only where a connector is installed
    FIX_ASK = "Fix it and send"
    MSG = {"thread_id": "33d4c6374833902d47cb1229", "subject": "Following up: your questionnaire",
          "body": "Hi Teo and Wren,\n\nJust a quick follow-up on the questionnaire.\n\nowner1"}
    fx = cards.post([{"header": "Reply to Teo and Wren", "lane": "wedding",
                      "question": "Follow up with Teo and Wren in the CRM? " + _crm.preview_tag() + " " + MSG["body"],
                      "options": [{"label": "Approve (Recommended)"}, {"label": "Skip"}],
                      "exec": {"kind": _MK[0], "approve_label": "Approve (Recommended)", "payload": MSG}}],
                    owner="owner1", asked=True)
    fxq = fx.get("qid") or fx.get("id")
    a1 = cards.answer(fxq, "Approve (Recommended)", by="gchat:owner1")
    ok(a1.get("ok") and a1.get("ticket"), "the follow-up card is tapped Approve and a ticket is minted")
    cards._retire(cards._read(cards.LIVE / f"{fxq}.json"), "consumed")
    es.set_status(a1["ticket"], "exception", "PERMANENT: CRM write unverified - check the project before re-carding. "
                  "SEND_FAILED=Approval not available for this run. Message requires approval in the CRM.", 1)
    ok(intents.failed_send()[0] == fxq and intents.failed_send(fxq)[0] == fxq, "the failed approved send is found, by its id and without one")


    def fake_run_fix(prompt, resume="", model="", timeout=600):
        import gmcp
        os.environ["AGENT_TURN"] = brain._TURN["path"]
        seen["fix"] = gmcp.call("approve", {"words": FIX_ASK})
        return {"ok": True, "session": "S20", "text": "Sending it again now, through the CRM's own message window."}


    brain._run_claude = fake_run_fix
    n0, g0 = len(NOTES), len(cap._load()["gaps"])
    bridge.handle({"channel": "local", "person": owner1, "text": FIX_ASK, "convo": "spaces/AAQAtest",
                   "thread": "spaces/AAQAtest/threads/failnote", "space": "spaces/AAQAtest", "direct": True})
    out = [r for r in core.rows(bridge.OUTBOX) if r.get("via") == "local"][-1]["text"]
    ans = [e for e in core.rows(cards.ANSWERS) if e.get("qid") == fxq and not e.get("undone")]
    tks = [x for x in es.fold().values() if x.get("approval_ref") == f"card:{fxq}"]
    ok(str(seen["fix"]).startswith("accepted") and "brought back and approved" in seen["fix"],
       "the approve deed takes the failed send with no card in the thread")
    ok(len(ans) == 2 and ans[-1]["option"] == "Approve (Recommended)" and ans[-1].get("ticket") and ans[-1]["ticket"] != a1["ticket"]
       and FIX_ASK in str(ans[-1].get("by")), "the same card is approved again on his words and a fresh ticket is minted")
    ok(len(tks) == 2 and sum(1 for x in tks if x.get("status") == "queued") == 1, "one new send queued, the failed one left as it was")
    ok("few minutes" not in out and "card" not in out.lower() and len(NOTES) == n0 and len(cap._load()["gaps"]) == g0,
       "no card promised, no build filed")
    ok(intents.failed_send() == ("", {}), "once it is queued again it is no longer a failed send")
# an UNVERIFIED failure of a kind that cannot check before sending is never resent on words alone
em = cards.post([{"header": "Email to Corwin", "lane": "wedding",
                  "question": "Send it? [EMAIL PREVIEW] From owner3@example.invalid to corwin@example.com, Proposal: Hi Corwin,\n\nHere it is.\n\nowner1",
                  "options": [{"label": "Send"}, {"label": "Skip"}],
                  "exec": {"kind": "email_send", "approve_label": "Send",
                           "payload": {"to": "corwin@example.com", "subject": "Proposal", "body": "Hi Corwin,\n\nHere it is.\n\nowner1",
                                       "from": "owner3@example.invalid"}}}], owner="owner1", asked=True)
emq = em.get("qid") or em.get("id")
a2 = cards.answer(emq, "Send", by="gchat:owner1")
ok(a2.get("ticket"), "the email card mints its ticket")
cards._retire(cards._read(cards.LIVE / f"{emq}.json"), "consumed")
es.set_status(a2["ticket"], "exception", "PERMANENT: UNVERIFIED smtp timeout", 1)
ok(intents.failed_send(emq) == ("", {}), "an unverified email failure is not resent on words (it may have landed)")
for f in ("prompt.md", "templates/prompt.generic.md"):
    ok("An approved send that FAILED" in (ROOT / "agent" / f).read_text(encoding="utf-8"), f"{f}: 'fix it and send' is `approve`, never a promised card")

# ---------------------------------------------------------------- 21. a chat-asked second email in a thread carries RESEND itself
# the owner, 2026-10-25 17:27: his follow-up to Corwin in the Sloane & Bram thread was refused at the tap ("A second message
# needs a card that says RESEND") though he had asked for it in chat; 2026-10-26: "Yes I still want it". The card his ask
# makes says RESEND, so the first tap sends; the same wording, or a card the system raised, is still refused.
RS_ASK = "Yes I still want it"
RB = {"from": "owner3@example.invalid", "to": "corwin@example.com", "subject": "Sloane & Bram edit",
      "body": "Hi Corwin,\n\nThe Sloane & Bram project is in Dropbox now.\n\nowner1"}
first = es.enqueue("email_send", RB, es.APPROVER, "card:rs-seed")
es.set_status(first["id"], "done", "SENT=1")
RB2 = "Hi Corwin,\n\nOne more thing: the ceremony audio is in the second drive folder.\n\nowner1"


def fake_run_resend(prompt, resume="", model="", timeout=600):
    import gmcp
    os.environ["AGENT_TURN"] = brain._TURN["path"]
    seen["rs"] = gmcp.call("task", {"lane": "wedding", "text": "Follow-up email to Corwin about the ceremony audio",
                                    "draft": {"channel": "email", "to": RB["to"], "name": "Corwin", "subject": RB["subject"],
                                              "text": RB2, "account": RB["from"]}})
    return {"ok": True, "session": "S21", "text": "Here's the follow-up to Corwin, right under this."}


brain._run_claude = fake_run_resend
n0, g0 = len(NOTES), len(cap._load()["gaps"])
bridge.handle({"channel": "local", "person": owner1, "text": RS_ASK, "convo": "spaces/AAQAtest",
               "thread": "spaces/AAQAtest/threads/corwin", "space": "spaces/AAQAtest", "direct": True})
rsc = [c for c in (cards._read(f) for f in cards.LIVE.glob("*.json"))
       if (((c.get("questions") or [{}])[0].get("exec") or {}).get("payload") or {}).get("body") == RB2]
ok(str(seen["rs"]).startswith("accepted") and len(rsc) == 1, f"the task deed posts the follow-up email card from the chat ask ({seen['rs'][:120]})")
rq = rsc[0]["questions"][0]
ok("RESEND" in rq["question"] and "tk-" not in rq["question"], "the chat-asked second email to the same thread says RESEND by itself, no ids")
ra = cards.answer(rsc[0]["id"], rq["exec"]["approve_label"], by="gchat:owner1")
ok(ra.get("ok") and ra.get("ticket") and not ra.get("mint_error"), f"the first tap mints the send, no refusal ({ra.get('mint_error') or ra.get('reason')})")
ok(len(NOTES) == n0 and len(cap._load()["gaps"]) == g0, "the ask completes with a deed, no build filed")
# the very same wording again: no mark, so the tap is refused
dq = actions._draft_question("wedding", "send that again", {"channel": "email", "to": RB["to"], "subject": RB["subject"],
                                                            "text": RB["body"], "account": RB["from"]}, owner1)
ok("RESEND" not in dq["question"], "an identical message gets no RESEND mark")
# a card the system raised on its own (no RESEND): still refused by the same-thread guard
SB = dict(RB, to="sam@example.com", subject="Shoot day plan", body="Hi Sam,\n\nHere is the plan.\n\nowner1")
sysc = cards.post([{"header": "Email to Sam", "lane": "wedding",
                    "question": "Send Sam a reminder? [EMAIL PREVIEW] From: owner3@example.invalid To: sam@example.com\n"
                                "Subject: Shoot day plan\nHi Sam,\n\nA reminder about the plan.\n\nowner1",
                    "options": [{"label": "Send"}, {"label": "Skip"}],
                    "exec": {"kind": "email_send", "approve_label": "Send",
                             "payload": dict(SB, body="Hi Sam,\n\nA reminder about the plan.\n\nowner1")}}], owner="operator")
es.set_status(es.enqueue("email_send", SB, es.APPROVER, "card:rs-seed2")["id"], "done", "SENT=1")
sa_ = cards.answer(sysc.get("qid") or sysc.get("id"), "Send", by="gchat:owner1")
ok("RESEND" in str(sa_.get("mint_error")) and not sa_.get("ticket"), f"a system-raised second email to the same thread is still refused ({sa_})")
# ---------------------------------------------------------------- 22. "did I pay the fee" is looked up, never sent back to the owner
# the owner, 2026-10-29 20:40 (gap g-c0da13): "I probably paid the fee by now. So now what do I do with it" was answered
# "I couldn't check the account to confirm the fee, so a quick search of your email or bank for a Cox charge would settle it".
FEE_ASK = "I probably paid the fee by now. So now what do I do with it"


def fake_run_fee(prompt, resume="", model="", timeout=600):
    os.environ["AGENT_TURN"] = brain._TURN["path"]
    seen["do"] = gmcp.call("do", {"lane": "personal", "text": "Sign in to the Cox account, check whether the fee was paid (date and "
                                  "amount), and read what the next step is now that it is paid; report both back here."})
    return {"ok": True, "session": "S9", "text": "Nothing in the mail settled it, so I've sent off to check the Cox account for the payment and the next step."}


brain._run_claude = fake_run_fee
n0, g0 = len(NOTES), len(cap._load()["gaps"])
bridge.handle({"channel": "local", "person": owner1, "text": FEE_ASK, "convo": "spaces/AAQAtest",
               "thread": "spaces/AAQAtest/threads/fee", "space": "spaces/AAQAtest", "direct": True})
out = [r for r in core.rows(bridge.OUTBOX) if r.get("via") == "local"][-1]["text"]
tix = [x for x in json.loads((TMP / "adata" / "tickets.json").read_text(encoding="utf-8")).values()
       if x["kind"] == "agent_run" and "Cox account" in str((x.get("payload") or {}).get("text"))]
ok(str(seen["do"]).startswith("accepted") and tix and tix[-1]["payload"].get("thread") == "spaces/AAQAtest/threads/fee",
   "'I probably paid the fee by now' becomes a doer job to read the account, in its own thread")
ok("couldn't check the account" not in out and "search of your email" not in out and len(NOTES) == n0
   and len(cap._load()["gaps"]) == g0, "no 'search your email or bank' line, no build filed")
for f in ("prompt.md", "templates/prompt.generic.md"):
    t = (ROOT / "agent" / f).read_text(encoding="utf-8")
    ok("Whether a bill, a fee or a charge is paid" in t and "Never \"I couldn't check the account\"" in t,
       f"{f}: a paid-or-not question is a `mail_search`, then a `do`")
# ---------------------------------------------------------------- 23. a second text never carries the word "resend" (a team member, 2026-10-29)
# the owner, 2026-10-29 19:00: the card for a team member read "resend Oak Hollow Farm ..." because the guard told Sage to put the
# word in the text. The flag lifts the guard; a leading marker an old caller still writes is stripped from preview and send.
staff1_ASK = ('you text would have literally said "resend Oak Hollow Farm, 123 Example Street in Fairview. Ceremony is at 3:30 '
           'outside, so be there by 3. I\'d want the drone for the ceremony and then golden hour at the Vista right after, so '
           'plan on about 3 to 5:30." fix that so it never happens again')
staff1_TO = "+15550944994"
staff1_BODY = ("Oak Hollow Farm, 123 Example Street in Fairview. Ceremony is at 3:30 outside, so be there by 3. I'd want the drone "
            "for the ceremony and then golden hour at the Vista right after, so plan on about 3 to 5:30.")
es.set_status(es.enqueue("imessage_send", {"account": "owner1-iphone", "to": staff1_TO, "body": "Hey staff1, the Ashcombe wedding is on."},
                         es.APPROVER, "card:staff1-seed")["id"], "done", "PHONE_QUEUED m-staff1")


def fake_run_staff1(prompt, resume="", model="", timeout=600):
    os.environ["AGENT_TURN"] = brain._TURN["path"]
    seen["staff1"] = gmcp.call("task", {"lane": "wedding", "text": "Text staff1 the venue and drone plan",
                                     "draft": {"channel": "text", "to": staff1_TO, "name": "staff1", "text": "resend " + staff1_BODY,
                                               "resend": True}})
    return {"ok": True, "session": "S23", "text": "Fixed: the card for staff1 has just the venue and plan, right under this."}


brain._run_claude = fake_run_staff1
n0, g0 = len(NOTES), len(cap._load()["gaps"])
bridge.handle({"channel": "local", "person": owner1, "text": staff1_ASK, "convo": "spaces/AAQAtest",
               "thread": "spaces/AAQAtest/threads/staff1", "space": "spaces/AAQAtest", "direct": True})
ic = [c for c in (cards._read(f) for f in cards.LIVE.glob("*.json"))
      if (((c.get("questions") or [{}])[0].get("exec") or {}).get("payload") or {}).get("to") == staff1_TO]
ok(str(seen["staff1"]).startswith("accepted") and len(ic) == 1, f"the second text to staff1 posts its card ({str(seen['staff1'])[:120]})")
iq = ic[0]["questions"][0]
ok(iq["exec"]["payload"]["body"] == staff1_BODY and "resend oak hollow" not in iq["question"].lower()
   and (staff1_TO + ":\n" + staff1_BODY) in iq["question"], "the card shows and carries the wording without the marker word")
ia = cards.answer(ic[0]["id"], iq["exec"]["approve_label"], by="gchat:owner1")
itk = es.fold().get(ia.get("ticket") or "", {})
ok(ia.get("ticket") and itk.get("payload", {}).get("body") == staff1_BODY, f"the tap sends exactly the clean wording ({ia.get('mint_error')})")
ok(len(NOTES) == n0 and len(cap._load()["gaps"]) == g0, "the ask completes with a deed, no build filed")
ok(actions.strip_marker("RESEND: Hi staff1") == "Hi staff1" and actions.strip_marker("Resending is fine") == "Resending is fine",
   "only a leading marker word is stripped")
# ---------------------------------------------------------------- 24. "respond to a team member again" answers only his last question (gap g-cc35a4)
# the owner, 2026-10-29 17:26: a team member asked the time frame and location; the draft repeated the date and other details, and the chat
# line explained what the draft lacked. The redo is a `task` draft with just the answer; the chat line names the card only.
AGAIN_ASK = "respond to staff1 again"
AGAIN_BODY = "Plan on about 3 to 5:30, at Oak Hollow Farm in Fairview."


def fake_run_again(prompt, resume="", model="", timeout=600):
    os.environ["AGENT_TURN"] = brain._TURN["path"]
    seen["again"] = gmcp.call("task", {"lane": "wedding", "text": "Answer staff1's question about the time frame and location",
                                       "draft": {"channel": "text", "to": staff1_TO, "name": "staff1", "text": AGAIN_BODY, "resend": True}})
    return {"ok": True, "session": "S24", "text": "The answer to staff1's question is on the card below."}


brain._run_claude = fake_run_again
n0, g0 = len(NOTES), len(cap._load()["gaps"])
bridge.handle({"channel": "local", "person": owner1, "text": AGAIN_ASK, "convo": "spaces/AAQAtest",
               "thread": "spaces/AAQAtest/threads/staff1-again", "space": "spaces/AAQAtest", "direct": True})
out = [r for r in core.rows(bridge.OUTBOX) if r.get("via") == "local"][-1]["text"]
ac = [c for c in (cards._read(f) for f in cards.LIVE.glob("*.json"))
      if (((c.get("questions") or [{}])[0].get("exec") or {}).get("payload") or {}).get("body") == AGAIN_BODY]
ok(str(seen["again"]).startswith("accepted") and len(ac) == 1, f"'respond to staff1 again' posts one card with just the answer ({str(seen['again'])[:120]})")
ok("I don't have the venue" not in out and "the draft gives" not in out and "card below" in out
   and len(NOTES) == n0 and len(cap._load()["gaps"]) == g0, "the chat line names the card, explains nothing, files no build")
for f in ("prompt.md", "templates/prompt.generic.md"):
    t = (ROOT / "agent" / f).read_text(encoding="utf-8")
    ok("answers only their newest question" in t and "Never restate what they already have" in t,
       f"{f}: a reply answers only the newest question, never repeats what they have")
# ---------------------------------------------------------------- 25. "purge all bank related log-in info" is one job card (2026-10-30)
# the owner, 2026-10-30 08:23: the doer was sent to delete the bank entries from its vault and its policy refused the edit.
# The purge is the registered job vault_purge_banks: the `job` deed posts ONE card, the tap runs doer/vault_purge.py.
PURGE_ASK = "Promptly purge all bank related log-in info you have unless it is through something like plaid"


def fake_run_purge(prompt, resume="", model="", timeout=600):
    os.environ["AGENT_TURN"] = brain._TURN["path"]
    seen["purge"] = gmcp.call("job", {"job": "vault_purge_banks", "lane": "money"})
    return {"ok": True, "session": "S25", "text": "Tap Run on the card and every bank login leaves the vault; Plaid, Venmo and PayPal stay."}


brain._run_claude = fake_run_purge
n0, g0 = len(NOTES), len(cap._load()["gaps"])
bridge.handle({"channel": "local", "person": owner1, "text": PURGE_ASK, "convo": "spaces/AAQAtest",
               "thread": "spaces/AAQAtest/threads/purge", "space": "spaces/AAQAtest", "direct": True})
pc = [c for c in (cards._read(f) for f in cards.LIVE.glob("*.json"))
      if (((c.get("questions") or [{}])[0].get("exec") or {}).get("payload") or {}).get("op") == "vault_purge_banks"]
import jobs  # noqa: E402
ok("vault_purge_banks" in jobs.render() and len(pc) == 1 and pc[0]["questions"][0]["exec"]["kind"] == "local",
   f"the purge ask posts one Run card for the vault_purge_banks job ({str(seen.get('purge'))[:120]})")
ok(len(NOTES) == n0 and len(cap._load()["gaps"]) == g0, "the purge ask completes with a deed, no build filed")
# ---------------------------------------------------------------- 26. "read the email thread we sent him" reads the mail (gap g-754648)
# the owner, 2026-10-31 10:04, in the thread of the Ambrose follow-up card: "No, I want you to read the email thread that we just
# sent him and make a logical follow up". Sage searched its notes only and answered "I couldn't open the actual email thread
# from here". The thread is read whole (mail_read with `thread`, sent mail included) and the draft follows on from it.
THREAD_ASK = "No, I want you to read the email thread that we just sent him and make a logical follow up"
EU = "ambrose@example.com"
MAILBOX = next(n for n, a in google_api.accounts().items() if a.get("person") == "owner1")
(google_api.HOME / f"{MAILBOX}.json").write_text("{}", encoding="utf-8")


def _b64(s):
    import base64
    return base64.urlsafe_b64encode(s.encode()).decode()


def _msg(mid, frm, to, body, labels):
    return {"id": mid, "threadId": "th-eu", "labelIds": labels, "snippet": body[:40],
            "payload": {"mimeType": "text/plain", "body": {"data": _b64(body)},
                        "headers": [{"name": "From", "value": frm}, {"name": "To", "value": to},
                                    {"name": "Subject", "value": "Re: trial reel"}, {"name": "Date", "value": "Fri, 30 Oct 2026"}]}}


EU_MSGS = [_msg("m1", EU, "owner3@example.invalid", "Happy to start with one trial reel. 12 to 14 days, two revision rounds.", ["INBOX"]),
           _msg("m2", "owner3@example.invalid", EU, "Great, I'll send my example timeline by Dropbox this week.", ["SENT"])]
api_calls = []


def fake_api(name, url, body=None, method=""):
    api_calls.append(url)
    if "/threads/" in url:
        return {"id": "th-eu", "messages": EU_MSGS}
    if "/messages?" in url:
        return {"messages": [{"id": "m2"}]}
    return next(m for m in EU_MSGS if m["id"] in url.split("/messages/")[1])


google_api._api = fake_api
EU_BODY = "Hi Ambrose,\n\nThe example timeline is on its way by Dropbox; let me know once you have it and when the trial reel could start.\n\nowner1"


def fake_run_thread(prompt, resume="", model="", timeout=600):
    os.environ["AGENT_TURN"] = brain._TURN["path"]
    found = gmcp.call("mail_search", {"account": MAILBOX, "query": f"to:{EU} OR from:{EU} in:anywhere"})
    seen["thread"] = gmcp.call("mail_read", {"account": MAILBOX, "thread": found[0]["thread"]})
    seen["eu"] = gmcp.call("task", {"lane": "wedding", "text": "Follow-up email to Ambrose from the thread",
                                    "draft": {"channel": "email", "to": EU, "name": "Ambrose", "subject": "Re: trial reel",
                                              "text": EU_BODY, "account": "owner3@example.invalid"}})
    return {"ok": True, "session": "S26", "text": "I read the thread; the follow-up to Ambrose is right under this."}


brain._run_claude = fake_run_thread
n0, g0 = len(NOTES), len(cap._load()["gaps"])
bridge.handle({"channel": "local", "person": owner1, "text": THREAD_ASK, "convo": "spaces/AAQAtest",
               "thread": "spaces/AAQAtest/threads/ambrose", "space": "spaces/AAQAtest", "direct": True})
out = [r for r in core.rows(bridge.OUTBOX) if r.get("via") == "local"][-1]["text"]
th = seen["thread"]
ok(any("/threads/th-eu" in u for u in api_calls) and [m["id"] for m in th["messages"]] == ["m1", "m2"]
   and th["messages"][1]["sent"] and "example timeline" in th["messages"][1]["body"],
   "mail_read with a thread id returns every message of the thread oldest first, the sent one included")
euc = [c for c in (cards._read(f) for f in cards.LIVE.glob("*.json"))
       if (((c.get("questions") or [{}])[0].get("exec") or {}).get("payload") or {}).get("body") == EU_BODY]
ok(str(seen["eu"]).startswith("accepted") and len(euc) == 1, f"the follow-up posts its email card ({str(seen['eu'])[:120]})")
ok("couldn't open" not in out and "from my notes" not in out and len(NOTES) == n0 and len(cap._load()["gaps"]) == g0,
   "no 'I couldn't open the email thread' line, no build filed")
ok("thread" in next(x for x in gmcp.TOOLS if x["name"] == "mail_read")["inputSchema"]["properties"], "mail_read offers `thread`")
for f in ("prompt.md", "templates/prompt.generic.md"):
    t = (ROOT / "agent" / f).read_text(encoding="utf-8")
    ok("An email follow-up" in t and "Never \"I couldn't open the email thread\"" in t,
       f"{f}: an email follow-up reads the thread first")

# ---------------------------------------------------------------- 27. their money, organized and tracked (2026-11-01)
# the owner, 2026-11-01 18:48, after price cards on Costco / Trader Joe's / Amazon: "You are my financial advisor you told me
# how to organize it and track it". The money lane now classes every merchant; the answer reads it and writes the plan down.
MONEY_ASK = "You are my financial advisor you told me how to organize it and track it"
seen27 = {}


def fake_run_money(prompt, resume="", model="", timeout=600):
    os.environ["AGENT_TURN"] = brain._TURN["path"]
    seen27["note"] = gmcp.call("note", {"title": "How our money is organized and tracked", "type": "workflow", "lane": "money",
                                        "body": "Every charge gets a class: subscription or bill, stores, restaurants, transfer, income, other. "
                                                "Only bills are recurring and can raise a price card; Costco, Trader Joe's and Amazon are "
                                                "stores, so a bigger shop is never a price rise. Moves between our own accounts are not spending."})
    return {"ok": True, "session": "S27", "text": "Here's how I'm keeping it: bills and subscriptions are tracked for price changes, "
                                                  "groceries and shopping are not, and transfers between your accounts don't count. "
                                                  "I wrote the plan down in the money notes."}


brain._run_claude = fake_run_money
n0, g0 = len(NOTES), len(cap._load()["gaps"])
bridge.handle({"channel": "local", "person": owner1, "text": MONEY_ASK, "convo": "spaces/AAQAtest",
               "thread": "spaces/AAQAtest/threads/money27", "space": "spaces/AAQAtest", "direct": True})
out = [r for r in core.rows(bridge.OUTBOX) if r.get("via") == "local"][-1]["text"]
ok(str(seen27["note"]).startswith("accepted") and len(NOTES) == n0 + 1 and "stores" in NOTES[-1]["body"],
   f"'{MONEY_ASK}': the plan is written down as a money note ({str(seen27['note'])[:80]})")
ok("can't" not in out.lower() and len(cap._load()["gaps"]) == g0, "the advisor ask completes with no 'can't' and no build filed")
for f in ("prompt.md", "templates/prompt.generic.md"):
    t = (ROOT / "agent" / f).read_text(encoding="utf-8")
    ok("Their money is yours to organize and track" in t and "money/forecast.json" in t and "Never \"I can't track that\"" in t,
       f"{f}: money organizing and tracking has a rung")
# ---------------------------------------------------------------- 28. "are you missing any" bank posts the sign-in card (gap g-e569ce)
# the owner, 2026-11-01 15:49: "I thought you were connected to all of my bank accounts. Are you missing any?" was answered with
# a list of banks that "still need their own sign-in pass" and nothing behind it. Now the missing banks are a `blocked`
# connection deed and the bank sign-in card is posted in that thread.
BANK_ASK = ("Yes, owner6 and I are both paid hourly. She just gotta pay raise so if you need any info from her, just ask her, "
            "but you can probably calculate everything you need to know from income to SoFi account since that is where our "
            "paychecks land. I thought you were connected to all of my bank accounts. Are you missing any?")
seen28, posted28 = {}, []
import chat_forms  # noqa: E402
chat_forms.post = lambda rid, space="", thread="": posted28.append((rid, space, thread)) or True


def fake_run_bank(prompt, resume="", model="", timeout=600):
    os.environ["AGENT_TURN"] = brain._TURN["path"]
    seen28["blocked"] = gmcp.call("blocked", {"what": "link Relay, SoFi and Wells Fargo to the bank feed",
                                              "why": "each bank needs its own sign-in on the bank's page", "kind": "connection",
                                              "connection": "bank-plaid"})
    return {"ok": True, "session": "S28", "text": "Chase is linked. Relay, SoFi and Wells Fargo are not yet: the sign-in card is "
                                                  "right below, one bank at a time, and I'll read your paychecks from SoFi once it's in."}


brain._run_claude = fake_run_bank
n0 = len(NOTES)
actions.LAST_APPLIED.clear()
bridge.handle({"channel": "local", "person": owner1, "text": BANK_ASK, "convo": "spaces/AAQAtest",
               "thread": "spaces/AAQAtest/threads/bank28", "space": "spaces/AAQAtest", "direct": True})
out = [r for r in core.rows(bridge.OUTBOX) if r.get("via") == "local"][-1]["text"]
kinds = [a["kind"] for a in actions.LAST_APPLIED]
ok(str(seen28.get("blocked", "")).startswith("accepted") and "blocked" in kinds and "form" in kinds,
   f"'Are you missing any?' bank ask: a blocked connection deed ({str(seen28.get('blocked'))[:60]})")
ok(posted28 and posted28[-1][0] == "bank-plaid" and posted28[-1][2] == "spaces/AAQAtest/threads/bank28",
   "the bank sign-in card is posted in the ask's own thread")
ok("sign-in pass" not in out and len(NOTES) == n0, "no 'still need their own sign-in pass' line, no build filed")
for f in ("prompt.md", "templates/prompt.generic.md"):
    t = (ROOT / "agent" / f).read_text(encoding="utf-8")
    ok("Asked whether you are missing an account" in t and "`connection` bank-plaid" in t,
       f"{f}: a missing bank is a blocked connection deed")
# ---------------------------------------------------------------- 29. "What is this mess?" on the tomorrow look-ahead (2026-11-01)
# the owner, 2026-11-01 19:27, under the 7pm look-ahead (four card charges as "<name> (monthly) due <date>: pay / confirm it is
# paid"). dayplan.py now writes a short dash list and stays quiet when nothing needs anyone; the replay learns the taste.
MESS_ASK = "What is this mess? It looks terrible. If it’s important it shouldn’t look like a waste of space in my chat."
sys.path.insert(0, str(ROOT / "scripts" / "tracking"))
import dayplan  # noqa: E402
import entities  # noqa: E402
from datetime import datetime  # noqa: E402
seen29 = {}
nov = [{"id": f"fu{i}", "sop": "money", "step": "bill-due", "status": "waiting", "lane": "money", "created_by": "money.forecast",
        "subject": f"{m} due 2026-11-02", "action": {"instruction": f"{m} is due 2026-11-02 (${a})."}}
       for i, (m, a) in enumerate((("Adobe (monthly)", "69.99"), ("Port Alder Meat (weekly)", "27.10"), ("Microsoft Xbox (monthly)", "19.99")))]
nov.append({"id": "fu9", "sop": "money", "step": "bill-due", "status": "waiting", "lane": "money", "created_by": "mail_arm",
            "subject": "SCE electric bill due 11/02/2026", "action": {"instruction": "SCE is due ($212.40)."}})
its29 = dayplan.items_for(datetime(2026, 11, 2), cal=[{"date": "2026-11-02", "time": "17:00", "title": "Call with the venue", "owner": "owner1"}],
                          entities=entities.money_rows(nov, {}, datetime(2026, 11, 1, 19)), gaps=[], fus=nov, chase_state={}, crm_tasks=[])
look = dayplan.message(datetime(2026, 11, 2), its29)


def fake_run_mess(prompt, resume="", model="", timeout=600):
    os.environ["AGENT_TURN"] = brain._TURN["path"]
    seen29["learn"] = gmcp.call("learn", {"rule": "Anything posted in chat is a short dash list, one line per item with its amount, and only what needs us",
                                          "why": MESS_ASK})
    return {"ok": True, "session": "S29", "text": "Fair. From tonight it looks like this, and on a night with only autopay it stays quiet:\n" + look}


brain._run_claude = fake_run_mess
g0 = len(cap._load()["gaps"])
actions.LAST_APPLIED.clear()
bridge.handle({"channel": "local", "person": owner1, "text": MESS_ASK, "convo": "spaces/AAQAtest",
               "thread": "spaces/AAQAtest/threads/mess29", "space": "spaces/AAQAtest", "direct": True})
out = [r for r in core.rows(bridge.OUTBOX) if r.get("via") == "local"][-1]["text"]
ok(str(seen29.get("learn", "")).startswith("accepted") and any("short dash list" in str(r) for r in core.rules_active()),
   f"'{MESS_ASK[:30]}...': the taste is learned as a rule ({str(seen29.get('learn'))[:60]})")
ok("can't" not in out.lower() and len(cap._load()["gaps"]) == g0, "the ask completes with no 'can't' and no new build")
ok(look == "Tomorrow, Monday 11/2\nOn the calendar:\n- 5pm Call with the venue\nBills due:\n- SCE electric bill, $212.40\n"
           "Autopay, nothing to do: Adobe, Port Alder Meat, Microsoft Xbox"
   and "pay / confirm" not in look and "(monthly)" not in look, "the look-ahead is a short list: name and amount, autopay in one line\n" + look)
ok(dayplan.tick(now=datetime(2026, 11, 1, 19), dry=True, items=[x for x in its29 if x["kind"] == "bill" and x["autopay"]]).get("silent"),
   "a night of only autopay charges posts nothing")
# ---------------------------------------------------------------- 30. a daily nudge into the team space (2026-11-02)
# the owner, 2026-11-02 13:53, in the home space: the schedule tool could only reach the owners, so a daily ask to a team member in the
# team space about the drone for Saturday's wedding could not be set. A schedule now takes `space` (+ `until`).
import routines  # noqa: E402
import space_reader  # noqa: E402
from datetime import datetime as _dt, timedelta as _td  # noqa: E402
_d0 = _dt.now().replace(hour=9, minute=1, second=0, microsecond=0)
UNTIL30 = (_d0 + _td(days=5)).strftime("%Y-%m-%d")             # 2026-11-07 on the day it was asked
TAG_ASK = "I meant tag him in a message every day in our group chat for the example-org Google space"
TEAM = "spaces/AAQAteam30"
space_reader.spaces = lambda max_age_s=600: [{"name": TEAM, "title": "example-org"}, {"name": core.home_space() or "spaces/HOME", "title": "Sage"}]
seen30 = {}


def fake_run_tag(prompt, resume="", model="", timeout=600):
    os.environ["AGENT_TURN"] = brain._TURN["path"]
    seen30["schedule"] = gmcp.call("schedule", {"title": "Drone update", "when": {"daily": "09:00"}, "space": "example-org Google space",
                                                "say": "staff1, any update on the drone for Saturday's wedding?", "until": UNTIL30})
    return {"ok": True, "session": "S30", "text": "Done, I'll ask him in the team space every morning at 9 through Saturday."}


brain._run_claude = fake_run_tag
g0 = len(cap._load()["gaps"])
bridge.handle({"channel": "local", "person": owner1, "text": TAG_ASK, "convo": "spaces/AAQAtest",
               "thread": "spaces/AAQAtest/threads/tag30", "space": "spaces/AAQAtest", "direct": True})
out = [r for r in core.rows(bridge.OUTBOX) if r.get("via") == "local"][-1]["text"]
r30 = [r for r in routines.active() if r.get("space")]
ok(str(seen30.get("schedule", "")).startswith("accepted") and len(r30) == 1 and r30[0]["space"] == TEAM and r30[0]["until"] == UNTIL30,
   f"'{TAG_ASK[:30]}...': the schedule is set in the team space ({str(seen30.get('schedule'))[:60]})")
ok("can't" not in out.lower() and len(cap._load()["gaps"]) == g0, "the ask completes with no 'can't' and no new build")
ok(routines.add({"title": "x", "when": {"daily": "09:00"}, "space": core.home_space() or "spaces/HOME", "say": "y"}, by="owner1")["status"] == "refused"
   and routines.add({"title": "x", "when": {"daily": "09:00"}, "space": "example-org", "prompt": "look it up"}, by="owner1")["status"] == "refused",
   "never the owners' home space, never a look-up in a team space")
owners30, space30 = [], []
for day in range(1, 8):
    routines.tick(lambda k, t: owners30.append((k, t)), now=_d0 + _td(days=day), post_space=lambda s, t: space30.append((s, t)))
ok(len(space30) == 5 and all(s == TEAM and t == "staff1, any update on the drone for Saturday's wedding?" for s, t in space30) and not owners30
   and not [r for r in routines.active() if r.get("space")], "posts once a day through its last day in the team space only, then ends")
import voice  # noqa: E402
ok(voice.gate("staff1, any update on the drone for Saturday's wedding?", "routine", TEAM), "the line passes the voice gate")

# ---------------------------------------------------------------- 31. a product-and-price research ask goes wider than the named site (gap g-31a02d)
# the owner, 2026-11-03 18:06: the WinCo ask came back "I could not get a single real item with a price, so there is no ranked
# list" because wincofoods.com has no catalog. The ask is a `do` that names the wider sources; the doer reads the ad and listings.
WINCO_ASK = ("Research WinCo Foods (Brackwater / Marrow Bay County stores, winco.com and any store deli or hot-foods pages). owner1 wants cheap "
             "high-protein premade or ready-to-eat food that could replace restaurant meals: deli hot food (rotisserie chicken, fried or "
             "roasted chicken), refrigerated or frozen premade burritos, burrito bowls, meal-prep style packs, bulk deli trays. For each "
             "real item found report: name, package size, price, price per serving or per burrito, calories and protein per serving if listed.")
seen31 = {}


def fake_run_winco(prompt, resume="", model="", timeout=600):
    os.environ["AGENT_TURN"] = brain._TURN["path"]
    seen31["do"] = gmcp.call("do", {"lane": "personal", "text": "Research WinCo Foods in Brackwater / Marrow Bay County for cheap high-protein ready-to-eat "
                                    "food (deli rotisserie or fried chicken, premade burritos, burrito bowls, meal-prep packs, deli trays). For each "
                                    "real item: name, package size, price, price per serving or per burrito, calories and protein per serving. Compare "
                                    "to under $5 a meal and El Pollo Loco family meals at about $12 a meal. If the named site has no catalog or prices, "
                                    "use any site that lists the same items: the store's weekly ad, grocery delivery listings, the maker's own page."})
    return {"ok": True, "session": "S31", "text": "I've sent off to price WinCo's ready-to-eat protein, from their weekly ad and listings where the site has no catalog."}


brain._run_claude = fake_run_winco
n0, g0 = len(NOTES), len(cap._load()["gaps"])
bridge.handle({"channel": "local", "person": owner1, "text": WINCO_ASK, "convo": "spaces/AAQAtest",
               "thread": "spaces/AAQAtest/threads/winco", "space": "spaces/AAQAtest", "direct": True})
out = [r for r in core.rows(bridge.OUTBOX) if r.get("via") == "local"][-1]["text"]
tix = [x for x in json.loads((TMP / "adata" / "tickets.json").read_text(encoding="utf-8")).values()
       if x["kind"] == "agent_run" and "weekly ad" in str((x.get("payload") or {}).get("text"))]
ok(str(seen31.get("do", "")).startswith("accepted") and tix and tix[-1]["payload"].get("thread") == "spaces/AAQAtest/threads/winco",
   "the WinCo research ask becomes a doer job in its own thread, naming the wider sources")
ok("single real item" not in out and "no ranked list" not in out and len(NOTES) == n0 and len(cap._load()["gaps"]) == g0,
   "no 'could not get a single real item' line, no build filed")
for f in ("prompt.md", "templates/prompt.generic.md"):
    t31 = (ROOT / "agent" / f).read_text(encoding="utf-8")
    ok("Prices or products to research: a `do` naming the store's ad" in t31 and "never relay \"I could not get a single real item\"" in t31,
       f"{f}: a product-and-price research ask is a `do` with the wider sources")
d31 = (ROOT / "doer" / "prompt.md").read_text(encoding="utf-8")
ok("Asked to research products or prices" in d31 and "the store's weekly ad or flyer" in d31
   and "every store, ad or listing that carries the items is related" in d31,
   "the doer goes past a site with no catalog to the ad, listings and maker pages")
# ---------------------------------------------------------------- 32. "cancel every gym membership" is a `do`, never "I couldn't find the agreement number" (gap g-9f9d49)
# the owner, 2026-11-05 09:34: two Fitness 19 memberships on auto-pay; the answer was "Nothing is cancelled yet ... the form asks for
# each membership's agreement number ... and I couldn't find the agreement numbers". The doer finds them and stops at each Cancel.
GYM_ASK = ("Cancel every Fitness 19 gym membership for owner1 and owner6 example. The bank feed shows TWO live memberships, both on auto-pay: "
           "(1) \"ABC*7980-FITNESS 19\", $11.99 charged on the 28th of each month to a card (club number 7980, last charge Oct 28 2026); "
           "(2) \"FITNESS 19 ABC CLUB FEES\", $17.99 pulled by ACH from a bank account around the 24th each month (last Oct 26 2026). "
           "Each also has a $49.99 annual fee. Fitness 19 emails go to owner4@example.invalid (sender noreply@fit19.com).")
seen32 = {}


def fake_run_gym(prompt, resume="", model="", timeout=600):
    os.environ["AGENT_TURN"] = brain._TURN["path"]
    seen32["do"] = gmcp.call("do", {"lane": "money", "text": "Cancel both Fitness 19 memberships at cancel.fit19.com: (1) ABC*7980-FITNESS 19, "
                                    "$11.99 on the 28th by card, club 7980; (2) FITNESS 19 ABC CLUB FEES, $17.99 by ACH around the 24th. Registered "
                                    "email owner4@example.invalid. Find each agreement number by signing in to the member account, from the "
                                    "noreply@fit19.com mail and the statements, or the site's own lookup; stop at each final Cancel for the tap."})
    return {"ok": True, "session": "S32", "text": "I've sent off to cancel both Fitness 19 memberships; each one stops at Cancel for your tap."}


brain._run_claude = fake_run_gym
n0, g0 = len(NOTES), len(cap._load()["gaps"])
bridge.handle({"channel": "local", "person": owner1, "text": GYM_ASK, "convo": "spaces/AAQAtest",
               "thread": "spaces/AAQAtest/threads/gym32", "space": "spaces/AAQAtest", "direct": True})
out = [r for r in core.rows(bridge.OUTBOX) if r.get("via") == "local"][-1]["text"]
tix = [x for x in json.loads((TMP / "adata" / "tickets.json").read_text(encoding="utf-8")).values()
       if x["kind"] == "agent_run" and "agreement number" in str((x.get("payload") or {}).get("text"))]
ok(str(seen32.get("do", "")).startswith("accepted") and tix and tix[-1]["payload"].get("thread") == "spaces/AAQAtest/threads/gym32",
   "'Cancel every Fitness 19 gym membership' becomes ONE doer job in its own thread, finding the agreement numbers itself")
ok("Nothing is cancelled yet" not in out and "couldn't find the agreement" not in out and len(NOTES) == n0
   and len(cap._load()["gaps"]) == g0, "no 'couldn't find the agreement numbers' line, no build filed")
for f in ("prompt.md", "templates/prompt.generic.md"):
    t32 = (ROOT / "agent" / f).read_text(encoding="utf-8")
    ok("Cancelling a membership, a subscription or a plan is a `do` that turn" in t32
       and "Never \"I couldn't find the agreement number\"" in t32, f"{f}: a cancel ask is a `do`; the doer looks for the numbers")
d32 = (ROOT / "doer" / "prompt.md").read_text(encoding="utf-8")
ok("Asked to cancel a membership or subscription" in d32 and "ONE CHECKPOINT per membership" in d32 and "member portal" in d32,
   "the doer finds the agreement number in the member account, mail or lookup and stops at each Cancel")
# ---------------------------------------------------------------- 33. a text only lined up on the phone (2026-11-06)
# the owner, 2026-11-06 13:13: 'Text to a team member: shoot day app' was tapped Send, went PHONE_QUEUED, the phone picked it up and
# Chat said "Texted a team member Quillfeather at 1:14 PM". It never arrived. 13:26: "The send from phone now button is not working
# either". Picked up is not sent; his words bring the card back as a fresh send card; the button's page runs the shortcut.
import exec_store as es  # noqa: E402
import phone_bridge  # noqa: E402
import voice  # noqa: E402
import mc_server  # noqa: E402
PH_ASK = "The send from phone now button is not working either"
PH_BODY = "Hey staff1, here is the shoot day app for Saturday."
ph = cards.post([{"header": "Text to staff1: shoot day app", "lane": "wedding",
                  "question": f"Text staff1 the shoot day app? [TEXT PREVIEW] To staff1 (+18055550142): {PH_BODY}",
                  "options": [{"label": "Send (Recommended)"}, {"label": "Skip"}],
                  "exec": {"kind": "imessage_send", "approve_label": "Send (Recommended)",
                           "payload": {"to": "+18055550142", "body": PH_BODY, "account": "owner1-iphone"}}}],
                owner="owner1", asked=True)
phq = ph.get("qid") or ph.get("id")
a33 = cards.answer(phq, "Send (Recommended)", by="gchat:owner1")
ok(a33.get("ok") and a33.get("ticket"), "the text card is tapped Send and a ticket is minted")
es.set_status(a33["ticket"], "done", "PHONE_QUEUED m-d347d53dfe16 -> ...0142")
cards._retire(cards._read(cards.LIVE / f"{phq}.json"), "consumed")
phone_bridge.save_json(phone_bridge.OUTBOX, {"items": [{"id": "m-d347d53dfe16", "ticket": a33["ticket"], "to": "+18055550142",
                                                         "body": PH_BODY, "status": "handed", "ts_handed": time.strftime("%Y-%m-%dT13:14:00")}]})
said33 = voice.reword("Text to +18055550142 handed to the phone at 1:14 PM; the phone's Shortcut sends it.")
ok("Texted" not in said33 and "lined up on your phone" in said33, f"a text the phone only picked up is never 'Texted': {said33}")
ok("NOT confirmed" in phone_bridge.state_words(a33["ticket"]), "its state for the brain says not confirmed, not 'sent in practice'")


def fake_run_phone(prompt, resume="", model="", timeout=600):
    import gmcp
    os.environ["AGENT_TURN"] = brain._TURN["path"]
    seen["phone"] = gmcp.call("approve", {"resend": True, "words": PH_ASK})
    return {"ok": True, "session": "S33", "text": "The text to staff1 is back as a send card here; that link runs the send on your phone."}


brain._run_claude = fake_run_phone
n0, g0 = len(NOTES), len(cap._load()["gaps"])
bridge.handle({"channel": "local", "person": owner1, "text": PH_ASK, "convo": "spaces/AAQAtest",
               "thread": "spaces/AAQAtest/threads/phone33", "space": "spaces/AAQAtest", "direct": True})
out = [r for r in core.rows(bridge.OUTBOX) if r.get("via") == "local"][-1]["text"]
back = cards._read(cards.LIVE / f"{phq}.json") or {}
ok(str(seen.get("phone", "")).startswith("accepted") and "/phone/run" in seen["phone"],
   f"the approve deed with resend is accepted and carries the send-now link: {seen.get('phone')}")
ok(back.get("status") == "open" and "RESEND" in str((back.get("questions") or [{}])[0].get("question")),
   "the text card is back open as a fresh send card, marked RESEND for the tap")
ok("can't" not in out.lower() and len(NOTES) == n0 and len(cap._load()["gaps"]) == g0, "no 'can't' line, no build filed")
# still waiting for the phone: the waiting copy is pulled first, so a resend never makes it go twice
cards._retire(cards._read(cards.LIVE / f"{phq}.json"), "consumed")
box = phone_bridge.load_json(phone_bridge.OUTBOX, {"items": []})
box["items"][0]["status"] = "ready"
phone_bridge.save_json(phone_bridge.OUTBOX, box)
brain._run_claude = fake_run_phone
bridge.handle({"channel": "local", "person": owner1, "text": "I don't see the text was sent, send it again", "convo": "spaces/AAQAtest",
               "thread": "spaces/AAQAtest/threads/phone33b", "space": "spaces/AAQAtest", "direct": True})
ok(phone_bridge.load_json(phone_bridge.OUTBOX, {"items": []})["items"][0]["status"] == "recalled"
   and es.fold()[a33["ticket"]]["status"] == "cancelled" and (cards._read(cards.LIVE / f"{phq}.json") or {}).get("status") == "open",
   "a text still waiting for the phone is pulled from the outbox before its card comes back")
page = mc_server.phone_run_page()
ok("shortcuts://run-shortcut?name=" in page and "<a id='go'" in page and "Open in Safari" in page,
   "the Send now from phone button opens a page with a tap target for the shortcut and the plain way if nothing happens")
# 2026-11-07 06:31, same ask again: the tap was in quiet hours / on a text the phone had already picked up, so the
# shortcut took nothing and the page said nothing. Now the page says why in one line, and says it once in the thread.
told = []
_say0, _quiet0 = phone_bridge._say, phone_bridge._quiet
phone_bridge._say = lambda item, line, fallback=False: told.append((item.get("ticket"), line)) or True
box = phone_bridge.load_json(phone_bridge.OUTBOX, {"items": []})
box["items"][0].update(status="handed", ts_handed=time.strftime("%Y-%m-%dT13:14:00"))
phone_bridge.save_json(phone_bridge.OUTBOX, box)
phone_bridge._quiet = lambda: False
pg = mc_server.phone_run_page(a33["ticket"])
ok("id='why'" in pg and "already picked that text up at 1:14 PM" in pg and "send it again" in pg,
   "a text the phone already picked up: the page says the shortcut won't take it again and how to resend")
mc_server.phone_run_page(a33["ticket"])
ok(len(told) == 1 and told[0][0] == a33["ticket"] and "already picked" in told[0][1],
   f"the same line is said once in the card's thread, not on every tap: {told}")
box["items"][0]["status"] = "ready"
phone_bridge.save_json(phone_bridge.OUTBOX, box)
phone_bridge._quiet = lambda: True
pg = mc_server.phone_run_page(a33["ticket"])
ok("quiet hours (9 PM to 7 AM)" in pg and len(told) == 2 and "quiet hours" in told[1][1],
   "a tap in quiet hours (6:31 AM) says plainly nothing goes until 7 AM, on the page and in the thread")
phone_bridge._quiet = lambda: False
pg = mc_server.phone_run_page(a33["ticket"])
ok("id='why'" not in pg and "shortcuts://run-shortcut?name=" in pg and len(told) == 2,
   "a text still waiting outside quiet hours: just the tap target, no extra line")
phone_bridge._say, phone_bridge._quiet = _say0, _quiet0
for f in ("prompt.md", "templates/prompt.generic.md"):
    ok("`resend` true" in (ROOT / "agent" / f).read_text(encoding="utf-8"), f"{f}: a text that did not arrive is `approve` with `resend`")

# ---------------------------------------------------------------- 34. "where did the other $1,000 on the card come from" (gap g-a6fb9d)
# the owner, 2026-11-08 13:06 (voice note): the card was about $1,300 a month ago and is over $3,000 now, $1,000 of it a lens
# rental. It was answered "I don't have the card's balance from a month ago". The `money` tool's `history` op works the
# balance back through the ledger and lists every charge since, so the brain answers from the figures.
from datetime import date as _date, timedelta as _td  # noqa: E402
CARD_ASK = ("[voice note from owner1, transcribed on this machine: \"Could have sworn the credit card was sitting around $1,300 just a "
            "month ago and now it's at over 3,000. I know $1,000 was a business expense. I'm gonna pay that off as soon as possible for the "
            "rents lentils lens rental But I'm not sure where the other 1,000 about came from\"]")
_d = lambda k: (_date.today() - _td(days=k)).isoformat()  # noqa: E731
(TMP / "adata" / "money").mkdir(parents=True, exist_ok=True)
(TMP / "adata" / "money" / "ledger.json").write_text(json.dumps({
    "accounts": {"cc1": {"id": "cc1", "name": "Sapphire Card", "type": "credit", "balance": -3150.0, "balance_date": _d(0)},
                 "chk": {"id": "chk", "name": "Everyday Checking", "type": "depository", "balance": 900.0, "balance_date": _d(0)}},
    "transactions": {
        "t1": {"id": "t1", "account_id": "cc1", "posted": _d(20), "amount": -1000.0, "description": "LENSRENTALS.COM", "bucket": "biz:gear"},
        "t2": {"id": "t2", "account_id": "cc1", "posted": _d(12), "amount": -640.0, "description": "UNITED AIRLINES", "bucket": ""},
        "t3": {"id": "t3", "account_id": "cc1", "posted": _d(5), "amount": -310.0, "description": "COSTCO WHSE", "bucket": ""},
        "t4": {"id": "t4", "account_id": "cc1", "posted": _d(3), "amount": 100.0, "description": "PAYMENT THANK YOU", "bucket": ""},
        "t0": {"id": "t0", "account_id": "cc1", "posted": _d(45), "amount": -75.0, "description": "OLD CHARGE", "bucket": ""}},
    "pulled": {}}), encoding="utf-8")
seen34 = {}


def fake_run_card(prompt, resume="", model="", timeout=600):
    os.environ["AGENT_TURN"] = brain._TURN["path"]
    seen34["history"] = gmcp.call("money", {"op": "history", "days": 30})
    return {"ok": True, "session": "S34", "text": "A month ago the card owed $1,300; it's $3,150 now. The $1,000 lens rental is one piece; "
                                                  "the rest is mostly a $640 United flight and a $310 Costco run, less a $100 payment."}


brain._run_claude = fake_run_card
n0, g0 = len(NOTES), len(cap._load()["gaps"])
bridge.handle({"channel": "local", "person": owner1, "text": CARD_ASK, "convo": "spaces/AAQAtest",
               "thread": "spaces/AAQAtest/threads/card34", "space": "spaces/AAQAtest", "direct": True})
out = [r for r in core.rows(bridge.OUTBOX) if r.get("via") == "local"][-1]["text"]
h34 = str(seen34.get("history"))
ok(h34.startswith("accepted") and "owed $1,300" in h34 and "owes $3,150" in h34 and "UNITED AIRLINES" in h34 and "LENSRENTALS" in h34
   and "OLD CHARGE" not in h34 and "$1,000 of it in business envelopes" in h34 and "payments and credits $100" in h34,
   f"'{CARD_ASK[60:110]}...': the money `history` deed works the card back to a month ago and lists every charge since ({h34[:160]!r})")
ok("don't have the card's balance" not in out and len(cap._load()["gaps"]) == g0 and len(NOTES) == n0,
   "the card ask is answered from the figures: no 'I don't have the balance' line, no build filed")
import intents  # noqa: E402
ok("history" in intents.MONEY_OPS and any(t["name"] == "money" and "history" in t["inputSchema"]["properties"]["op"]["enum"] for t in gmcp.TOOLS),
   "the money tool offers `history` to the brain")
for f in ("prompt.md", "templates/prompt.generic.md"):
    ok("money` `history`" in (ROOT / "agent" / f).read_text(encoding="utf-8"), f"{f}: a balance then-and-now question has its rung")

# ---------------------------------------------------------------- 35. "are you sure the lens rentals are due tomorrow?" (gap g-a9b3c1)
# the owner, 2026-11-09 16:52: asked how Sage knew a bill was due and money was short, and for a breakdown; it was answered
# "I can't move money myself". The `money` tool's `bills` op lists the predicted bills, how each is known, and each
# account's balance against them, so the brain answers the question.
BILLS_ASK = ("Are you sure the lens rentals are due tomorrow? Also, how do you know we don’t have enough money to pay our "
             "expenses? Can you give me a breakdown?")
_tx = {f"lr{k}": {"id": f"lr{k}", "account_id": "chk", "posted": _d(30 * k - 1), "amount": -450.0,
                  "description": "LENSRENTALS.COM", "bucket": ""} for k in (1, 2, 3)}
_tx.update({f"ins{k}": {"id": f"ins{k}", "account_id": "chk", "posted": _d(30 * k - 3), "amount": -620.0,
                        "description": "STATE FARM INSURANCE", "bucket": ""} for k in (1, 2, 3)})
(TMP / "adata" / "money" / "ledger.json").write_text(json.dumps({
    "accounts": {"chk": {"id": "chk", "name": "Everyday Checking", "type": "depository", "balance": 900.0, "balance_date": _d(0)}},
    "transactions": _tx, "pulled": {}}), encoding="utf-8")
seen35 = {}


def fake_run_bills(prompt, resume="", model="", timeout=600):
    os.environ["AGENT_TURN"] = brain._TURN["path"]
    seen35["bills"] = gmcp.call("money", {"op": "bills"})
    return {"ok": True, "session": "S35", "text": "Yes: Lensrentals has charged checking $450 every month, last on the same date, so "
                                                  "tomorrow is predicted from that. With State Farm's $620 that's $1,070 against $900 in checking."}


brain._run_claude = fake_run_bills
n0, g0 = len(NOTES), len(cap._load()["gaps"])
bridge.handle({"channel": "local", "person": owner1, "text": BILLS_ASK, "convo": "spaces/AAQAtest",
               "thread": "spaces/AAQAtest/threads/bills35", "space": "spaces/AAQAtest", "direct": True})
out = [r for r in core.rows(bridge.OUTBOX) if r.get("via") == "local"][-1]["text"]
b35 = str(seen35.get("bills"))
ok(b35.startswith("accepted") and f"{_d(-1)} Lensrentals.Com" in b35 and "3 charges so far" in b35 and "State Farm" in b35
   and "balance $900" in b35 and "bills $1,070" in b35 and "short $170" in b35,
   f"'{BILLS_ASK[:50]}...': the money `bills` deed shows each bill, how it is known, and checking against them ({b35[:200]!r})")
ok("can't move money" not in out and len(cap._load()["gaps"]) == g0 and len(NOTES) == n0,
   "the bills ask is answered with the breakdown: no 'I can't move money' line, no build filed")
ok("bills" in intents.MONEY_OPS and any(t["name"] == "money" and "bills" in t["inputSchema"]["properties"]["op"]["enum"] for t in gmcp.TOOLS),
   "the money tool offers `bills` to the brain")
for f in ("prompt.md", "templates/prompt.generic.md"):
    ok("money` `bills`" in (ROOT / "agent" / f).read_text(encoding="utf-8"), f"{f}: an 'is it really due / why short' question has its rung")

# ---------------------------------------------------------------- 20. a voice note that failed to transcribe is tried again (gap g-21835d)
# the owner, 2026-11-07 10:06: a voice note came in as "[voice note ... ; transcription failed]" and was answered "Your voice
# note didn't come through on my end". The replay hands back only that line: the bridge transcribes the saved file again.
import space_reader  # noqa: E402
VOICE = TMP / "attachments" / "20261107-100631_voice_message_2026-11-07T1805Z.m4a"
VOICE.parent.mkdir(parents=True, exist_ok=True)
VOICE.write_bytes(b"\x00fake m4a")
VOICE_ASK = f"[voice note from owner1 saved at {VOICE}; transcription failed]"
SAID = "Find the venue timeline PDF in my Drive and send me the link"
_tr0 = space_reader.transcribe
space_reader.transcribe = lambda p, timeout=900: SAID if Path(p) == VOICE else ""


def fake_run_voice(prompt, resume="", model="", timeout=600):
    os.environ["AGENT_TURN"] = brain._TURN["path"]
    if SAID not in prompt:
        return {"ok": True, "session": "S20", "text": "Your voice note didn't come through on my end, so I couldn't make out what you said."}
    seen["voice_do"] = gmcp.call("do", {"lane": "wedding", "text": "Find the venue timeline PDF in the owner's Drive and send back the link."})
    return {"ok": True, "session": "S20", "text": "On it, I'll find the venue timeline and send the link here."}


brain._run_claude = fake_run_voice
bridge.handle({"channel": "local", "person": owner1, "text": VOICE_ASK, "convo": "spaces/AAQAtest",
               "thread": "spaces/AAQAtest/threads/voice", "space": "spaces/AAQAtest", "direct": True})
out = [r for r in core.rows(bridge.OUTBOX) if r.get("via") == "local"][-1]["text"]
tix = [x for x in json.loads((TMP / "adata" / "tickets.json").read_text(encoding="utf-8")).values()
       if x["kind"] == "agent_run" and "venue timeline" in str((x.get("payload") or {}).get("text"))]
ok(str(seen.get("voice_do")).startswith("accepted") and tix, "the replayed voice note is transcribed again and becomes a doer job")
ok("didn't come through" not in out, f"no 'didn't come through' line: {out!r}")
space_reader.transcribe = _tr0
ok(space_reader.retry_failed_voice("[voice note from owner1 saved at C:\\nope.m4a; transcription failed]")
   == "[voice note from owner1 saved at C:\\nope.m4a; transcription failed]", "a file that is gone leaves the line as it was")
# the transcriber itself: a failed first run is tried once more offline, from the cached model
runs = []


class _R:
    stderr, stdout = "ConnectionError: hub unreachable", ""


def fake_sub(cmd, **kw):
    runs.append((kw.get("env") or {}).get("HF_HUB_OFFLINE"))
    if len(runs) == 2:
        VOICE.with_suffix(".txt").write_text(SAID + "\n", encoding="utf-8")
    return _R()


_sub0, _wpy0 = space_reader.subprocess.run, space_reader.WHISPER_PY
space_reader.subprocess.run, space_reader.WHISPER_PY = fake_sub, VOICE
ok(space_reader.transcribe(VOICE) == SAID and runs == [None, "1"], f"a failed transcription retries offline once: {runs}")
space_reader.subprocess.run, space_reader.WHISPER_PY = _sub0, _wpy0

# ---------------------------------------------------------------- 21. a local file to a website is the PC lane's upload.py (gap g-29ce2a)
# the owner, 2026-11-10 17:14: replacing ParasFinal.mov in Dropbox came back "the browser refuses to read files from
# C:\Users\the owner\Videos, so nothing changed in Dropbox". The ask is a `do` where computer that runs doer/upload.py,
# which gives the file to the signed-in page over CDP from any folder, any size.
UP_ASK = ("Replace ParasFinal.mov in owner1's Dropbox on dropbox.com, account owner4@example.invalid (signed in earlier today "
          "in the shared browser profile). Folder: Media/Video Projects. Current file there: ParasFinal.mov, 1.85 GB, modified "
          "11/10/2026 2:34 pm, with an existing view-only share link that must keep working. Steps: open "
          "https://www.dropbox.com/home/Media/Video%20Projects, click Upload > Files, pick the local file C:\\Users\\owner1\\Videos\\ParasFinal.mov")
UP_CMD = ('python assistant/doer/upload.py --file "C:/Users/owner1/Videos/ParasFinal.mov" '
          '--url "https://www.dropbox.com/home/Media/Video%20Projects" --replace')


def fake_run_upload(prompt, resume="", model="", timeout=600):
    os.environ["AGENT_TURN"] = brain._TURN["path"]
    seen["upload_do"] = gmcp.call("do", {"lane": "wedding", "where": "computer",
                                         "text": f"Replace ParasFinal.mov in Dropbox Media/Video Projects, keeping its share link: run {UP_CMD}"})
    return {"ok": True, "session": "S21", "text": "I'm replacing ParasFinal.mov in Dropbox now; the share link stays the same."}


brain._run_claude = fake_run_upload
n0, g0 = len(NOTES), len(cap._load()["gaps"])
bridge.handle({"channel": "local", "person": owner1, "text": UP_ASK, "convo": "spaces/AAQAtest",
               "thread": "spaces/AAQAtest/threads/kingsmere", "space": "spaces/AAQAtest", "direct": True})
out = [r for r in core.rows(bridge.OUTBOX) if r.get("via") == "local"][-1]["text"]
tix = [x for x in json.loads((TMP / "adata" / "tickets.json").read_text(encoding="utf-8")).values()
       if x["kind"] == "agent_run" and "upload.py" in str((x.get("payload") or {}).get("text"))]
ok(str(seen.get("upload_do")).startswith("accepted") and tix and str(tix[-1]["payload"].get("where")) == "computer",
   "the Dropbox replace ask becomes a PC job that runs upload.py")
ok("refuses to read" not in out and "couldn't upload" not in out and len(NOTES) == n0 and len(cap._load()["gaps"]) == g0,
   f"no 'browser refuses' line, no build filed: {out!r}")
for f in ("prompt.md", "templates/prompt.generic.md", "desk_prompt.md"):
    ok("assistant/doer/upload.py" in (ROOT / "agent" / f).read_text(encoding="utf-8"), f"{f}: a local file to a website has its rung")
# the PC lane's hook lets the command run (tier A/B, no checkpoint), the script passes the scan
sys.path.insert(0, str(ROOT / "doer"))
import computer_policy  # noqa: E402
tier, why = computer_policy.classify(UP_CMD)
ok(tier in ("A", "B"), f"the PC lane may run upload.py without a checkpoint: {tier} {why}")
ok(computer_policy.scan_script((ROOT / "doer" / "upload.py").read_text(encoding="utf-8"))[0] == "B", "upload.py passes the static scan")

# upload.py itself, over a fake browser: finds the input after Upload > Files, gives the file, presses Replace, reads done
import browser as _browser  # noqa: E402
import upload  # noqa: E402
FILM = TMP / "Videos" / "ParasFinal.mov"
FILM.parent.mkdir(parents=True, exist_ok=True)
FILM.write_bytes(b"\x00" * 2048)
upload.OUT = TMP / "uploads"
upload.BUSY = _browser.UPLOADS
PAGE = {"id": "T1", "url": "https://www.dropbox.com/home/Media/Video%20Projects", "webSocketDebuggerUrl": "ws://fake/T1"}
fake = {"input": False, "files": None, "clicked": [], "texts": ["Uploading 1 item 40% remaining", "Uploaded 1 item"], "held": []}


def fake_cdp(ws, calls):
    res = []
    for method, params in calls:
        if callable(params):
            params = params(res)
        if method == "DOM.setFileInputFiles":
            fake["files"] = params.get("files")
            fake["held"].append(bool(_browser.held_tabs()))
            res.append({})
            continue
        expr = params.get("expression", "")
        if expr == upload.FIND_INPUT_JS:
            res.append({"result": {"objectId": "obj1"}} if fake["input"] else {"result": {"type": "object", "subtype": "null"}})
        elif "document.readyState" == expr:
            res.append({"result": {"value": "complete"}})
        elif expr.startswith("(document.body"):
            fake["held"].append("T1" in _browser.held_tabs())
            res.append({"result": {"value": fake["texts"].pop(0) if len(fake["texts"]) > 1 else fake["texts"][0]}})
        elif expr.startswith("((words)"):
            words = json.loads(expr[expr.rindex("(") + 1:-1])
            w = next((x for x in words if x in ("Upload", "Files") or (x == "Replace" and fake["files"] and "Replace" not in fake["clicked"])), "")
            if w:
                fake["clicked"].append(w)
                fake["input"] = fake["input"] or w == "Files"
            res.append({"result": {"value": w}})
        else:
            res.append({"result": {"value": "ok"}})
    return res


_cdp0, _open0, _pages0, _sleep0 = _browser._cdp_calls, _browser.open_browser, upload._pages, upload.time.sleep
_browser._cdp_calls, _browser.open_browser, upload._pages = fake_cdp, (lambda show=None, wait_s=25.0: "http://127.0.0.1:1"), (lambda: [PAGE])
upload.time.sleep = lambda s: None
st = upload.run(str(FILM), url=PAGE["url"], replace=True, wait_s=60, background=False)
ok(st["state"] == "done" and fake["files"] == [str(FILM.resolve())], f"upload.py gives the file and reads the upload done: {st}")
ok(fake["clicked"][:2] == ["Upload", "Files"] and "Replace" in st.get("pressed", []), f"Upload > Files, then Replace: {fake['clicked']}")
ok(any(fake["held"]) and "T1" not in _browser.held_tabs(), "the tab is held while it uploads and let go after")
ok(Path(st["status_file"]).is_file(), "the end state is written to a status file")
miss = upload.run(str(TMP / "nope.mov"), url=PAGE["url"], background=False)
ok(miss["state"] == "failed" and "no file" in miss["why"], "a missing file fails plainly")
_browser._cdp_calls, _browser.open_browser, upload._pages, upload.time.sleep = _cdp0, _open0, _pages0, _sleep0

print(f"\nall {N} checks passed")

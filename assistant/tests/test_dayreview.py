"""test_dayreview.py - offline checks for the review that acts (assistant/agent/dayreview.py, 2026-11-14) and the
three things it leans on: causes read from the ledger (scorecard), "fixed" that means the symptom is gone (reflex),
and a build queue ranked by what the review found (builder.pick is covered in test_builder_order.py).

  python assistant/tests/test_dayreview.py
"""
import json
import os
import re
import sys
import tempfile
import time
from datetime import datetime, timedelta
from pathlib import Path

TMP = Path(tempfile.mkdtemp(prefix="dayreview_test_"))
os.environ["WV_LOG_DIR"] = str(TMP / "logs")
os.environ["AGENT_STATE"] = str(TMP / "state")
os.environ["AGENT_DATA"] = str(TMP / "data")
os.environ["AGENT_SUGGESTIONS"] = str(TMP / "skill-suggestions.md")
os.environ["ASSISTANT_DATA"] = str(TMP / "adata")
os.environ["ASSISTANT_CARDS"] = str(TMP / "cards")
os.environ["AGENT_PEOPLE"] = str(TMP / "people.json")
os.environ["AGENT_HOME_SPACE"] = ""
os.environ["CHAT_PUSH"] = "off"
(TMP / "people.json").write_text(json.dumps({"roles": {}, "users": {
    "pat": {"name": "Pat Example", "role": "owner"}, "sam": {"name": "Sam Example", "role": "staff"}}}), encoding="utf-8")

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "agent"))
sys.path.insert(0, str(ROOT / "scripts"))
import core  # noqa: E402
import dayreview  # noqa: E402
import outcomes  # noqa: E402
import reflex  # noqa: E402
import scorecard  # noqa: E402

PASS = FAIL = 0


def check(name, cond, extra=""):
    global PASS, FAIL
    PASS += bool(cond)
    FAIL += (not cond)
    print(("ok   " if cond else "FAIL ") + name + (f"  {extra}" if extra and not cond else ""))


NOW = datetime.now().replace(hour=12, minute=0, second=0, microsecond=0)
DAY = (NOW - timedelta(days=1)).strftime("%Y-%m-%d")
T = "spaces/S/threads/day"          # one thread a day for the space, as the chat keeps it
CURLY = chr(0x2019)


def say(hhmm, who, text, thread=T, **extra):
    core.append(core.TRANSCRIPT, {"ts": f"{DAY} {hhmm}", "convo": "spaces/S", "who": who, "text": text, "channel": "gchat",
                                  "thread": thread, **extra})


def outcome(hhmm, kind, what, state, detail, thread=T, ref=None, closed=None):
    row = {"id": "o-" + str(abs(hash((hhmm, what))))[:10], "kind": kind, "ref": ref or f"{kind}:{hhmm}", "what": what, "by": "pat",
           "thread": thread, "state": state, "created": f"{DAY}T{hhmm}:00", "updated": f"{DAY}T{closed or hhmm}:30",
           "checks": [{"ts": f"{DAY}T{hhmm}:30", "result": state, "detail": detail, "by": "code"}]}
    if state in outcomes.TERMINAL:
        row["closed"] = row["updated"]
    outcomes._append(row)


# ------------------------------------------------------------------ a day of chat
say("08:00", "Pat", "What time is the site visit on Friday?")
say("08:02", "Agent", "It is at 2 PM on Friday at the office.")
say("08:03", "Pat", "thanks")                                                        # an acknowledgement, not an ask
say("09:00", "Pat", "Upload the final report to the shared folder")
say("09:01", "Pat", "and send the link to the client")                               # same burst
say("09:01", "Agent", "On it. I'll put what I did here when it's done.")
outcome("09:01", "do", "Upload the final report", "failed", "blocked: I couldn't upload the report, because the browser refuses to read files from that folder")
say("09:20", "Agent", "I couldn't upload it; the browser can't read that folder.")
say("11:00", "Pat", "Draft a reply to the supplier about the late order")
say("11:50", "Agent", "Here is the draft for the supplier.")                          # 50 minutes for a plain answer
say("13:00", "Pat", "Put the Saturday meeting on the calendar")
say("13:01", "Agent", "Added: meeting Saturday at 4 PM.")
say("13:30", "Pat", f"You didn{CURLY}t add it, I still see nothing on Saturday")       # typed on a phone: a curly apostrophe
say("13:31", "Agent", "You're right, it is on now.")
say("15:30", "Pat", "Remind the helper about the start time tomorrow")        # nobody answers; a new sitting
say("16:00", "Pat", "Text the organiser the schedule we agreed on")
say("16:01", "Agent", "Texting the organiser now.")
outcome("16:01", "do", "Text the organiser the schedule", "abandoned", "closed quietly: the hand-off's own receipt is the record")
say("17:00", "Pat", "Approve", click=True)                                           # a tap on a card
say("17:05", "Sam", "Can you resend me the shot list?")                              # staff, not an owner

asks = dayreview.asks(DAY)
check("asks: an owner's lines only; a burst is one ask; a thank-you and a tap are not asks",
      len(asks) == 7 and "send the link" in asks[1]["text"] and not any("thanks" == a["text"] for a in asks), str([a["text"][:30] for a in asks]))

g = {a["text"][:20]: a for a in dayreview.grade(DAY, now=NOW.timestamp())}
check("grade: answered in two minutes is done", g["What time is the sit"]["grade"] == "done" and g["What time is the sit"]["minutes"] == 2.0)
up = g["Upload the final rep"]
check("grade: work the ledger shows failed is not done, with its cause read from the row",
      up["grade"] == "not done" and up["cause"] == "file" and "refuses to read files" in up["detail"], str(up))
check("grade: a plain answer after fifty minutes is late", g["Draft a reply to the"]["grade"] == "late")
check("grade: the owner saying it again makes the ask before it 'repeated' (curly apostrophe and all)",
      g["Put the Saturday mee"]["grade"] == "repeated")
check("grade: a complaint belongs to the ask just before it, not to every earlier line in the day's thread",
      g["What time is the sit"]["grade"] == "done" and g["Draft a reply to the"]["grade"] == "late")
check("grade: nothing came back at all is 'no answer'", g["Remind the helper ab"]["grade"] == "no answer")
check("grade: a quiet close is not a failure", g["Text the organiser t"]["grade"] == "done", str(g["Text the organiser t"]))

# ------------------------------------------------------------------ causes, read instead of lumped
for d, want in (("not started yet", "never started"), ("blocked; the portal line gave the owner the steps", "left to the owner"),
                ("blocked: the site wants a login before it will go on", "sign-in"),
                ("blocked: I filled nothing, because my setup holds the open tabs for the owner", "own rule in the way"),
                ("the doer said done and wrote no evidence", "no evidence"),
                ("waited 96 min on route do, past its 90 min clock", "timeout"),
                ("blocked: the shared browser stopped responding (its debug port refused connections)", "browser")):
    check(f"cause: {want}", scorecard._classify(d) == want, scorecard._classify(d))
cz = dayreview.causes(7, now=NOW.timestamp())
check("causes: the week's failures by cause, quiet closes left out", cz.get("file", {}).get("n") == 1 and len(cz) == 1, str(cz))
kinds, classes, _ = scorecard._outcome_metrics(NOW.timestamp() - 7 * 86400)
check("scorecard: a quiet close is settled, neither kept nor failed",
      kinds["do"]["settled"] == 1 and kinds["do"]["failed"] == 1 and kinds["do"]["abandoned"] == 0 and classes == {"file": 1}, f"{kinds} {classes}")
check("scorecard: a minute-only chat stamp is a real time (it read as 0, so repeats were never counted)",
      scorecard._ts(f"{DAY} 13:30") > 0 and scorecard._ts(f"{DAY}T13:30:00") > 0)

# ------------------------------------------------------------------ the run: the worst become ranked code fixes
FILED = []
OPEN = [{"key": "Wiki/Personal/Build request Old review fix.md", "title": "Build request Old review fix", "text": "x"}]


def _file(title, body, issue):
    key = f"Wiki/Personal/{title[:40]}.md"
    FILED.append((key, issue, body))
    return key


dayreview.FILE_HOOK, dayreview.OPEN_HOOK = _file, (lambda: OPEN)
outcome("07:00", "build", "Old review fix", "failed", "not started yet", thread="", ref=OPEN[0]["key"])
rec = dayreview.run(DAY, now=NOW.timestamp())
check("run: the day is counted", rec["counts"].get("asks") == 7 and rec["counts"].get("repeated") == 1 and rec["counts"].get("not done") == 1, str(rec["counts"]))
check("run: the worst three asks are filed, the one said twice first", len(FILED) == 3 and "meeting" in FILED[0][2], str([f[0] for f in FILED]))
check("run: each is ranked ahead of the queue by how bad it was",
      dayreview.priority_of(FILED[0][0]) == dayreview.SCORE["repeated"] and dayreview.priority_of(FILED[1][0]) == dayreview.SCORE["not done"])
check("run: a fix the review asked for that nobody started is moved up, not counted and forgotten",
      dayreview.priority_of(OPEN[0]["key"]) == dayreview.SCORE["slipped"] and any(a["issue"] == "slipped" for a in rec["acted"]))
check("run: the request says a rule is not a fix", "A rule or a reworded prompt is not a fix" in open(dayreview.__file__, encoding="utf-8").read())
n = len(FILED)
dayreview.run(DAY, now=NOW.timestamp())
check("run: the same miss is never filed twice", len(FILED) == n)
rows = dayreview.dream_rows()
check("the nightly review reads the grade and what code already did",
      rows and rows[0].startswith(f"DAYREVIEW {DAY}: 7 asks") and any("handled by code" in r for r in rows) and any("not done" in r for r in rows))
check("status reads back in plain words", "said twice" in dayreview.status() and "build queue first" in dayreview.status())

check("a pasted link and a reference number are cleaned out of a filed request (the wiki refused both, 2026-11-15)",
      dayreview.clean("apply here https://jobs.example.com/jobs/96ac18f0-0c31-49ec-83d1-e4df4dba45f7?utm=x requisition PROPE004958")
      == "apply here jobs.example.com requisition (a number)", dayreview.clean("https://jobs.example.com/jobs/96ac18f0-0c31-49ec-83d1-e4df4dba45f7 PROPE004958"))

TT = dayreview._title("It applies to that too. It" + CURLY + "s fine (see https://x.example.com/a?b=1) and then some more words to run long")
check("a filed request's title is a safe file name: no ellipsis, no curly apostrophe, no link, cut at a word",
      TT.startswith("It applies to that too Its fine see and then") and re.fullmatch(r"[A-Za-z0-9 ]+", TT) and len(TT) <= 60
      and not TT.endswith(" ") and "example" not in TT, TT)
check("every title the run filed is a safe file name", all(re.fullmatch(r"[A-Za-z0-9 /.]+", f[0]) for f in FILED), str([f[0] for f in FILED]))

# an ask only the owner can clear is graded, never filed
FILED.clear()
core.save(dayreview.STATE, {})
D2 = (NOW - timedelta(days=2)).strftime("%Y-%m-%d")
core.append(core.TRANSCRIPT, {"ts": f"{D2} 10:00", "convo": "spaces/S", "who": "Pat", "text": "Cancel the gym membership on their site", "channel": "gchat", "thread": "spaces/S/threads/d2"})
outcomes._append({"id": "o-signin", "kind": "do", "ref": "do:signin", "what": "Cancel the gym membership", "by": "pat", "thread": "spaces/S/threads/d2",
                  "state": "failed", "created": f"{D2}T10:00:30", "updated": f"{D2}T10:05:00", "closed": f"{D2}T10:05:00",
                  "checks": [{"result": "failed", "detail": "blocked: the site wants a login before it will go on"}]})
rec2 = dayreview.run(D2, now=NOW.timestamp())
check("run: a sign-in only the owner can do is graded not done and no code fix is filed for it",
      rec2["counts"].get("not done") == 1 and not [f for f in FILED if f[1].startswith("ask:")], str(FILED))

# one job that fails three times in an afternoon is one miss
D3 = (NOW - timedelta(days=3)).strftime("%Y-%m-%d")
for i, hh in enumerate(("14:00", "14:20", "14:40")):
    core.append(core.TRANSCRIPT, {"ts": f"{D3} {hh}", "convo": "spaces/S", "who": "Pat", "text": ["Apply to the posting with my resume attached", "Go ahead and apply to it now please", "Submit the application form as it is"][i],
                                  "channel": "gchat", "thread": "spaces/S/threads/d3"})
    outcomes._append({"id": f"o-job{i}", "kind": "do", "ref": f"do:job{i}", "what": "apply", "by": "pat", "thread": "spaces/S/threads/d3", "state": "failed",
                      "created": f"{D3}T{hh}:10", "updated": f"{D3}T{hh}:50", "closed": f"{D3}T{hh}:50",
                      "checks": [{"result": "failed", "detail": "blocked: the resume does not show under Resume after two upload tries"}]})
FILED.clear()
rec3 = dayreview.run(D3, now=NOW.timestamp())
check("run: three failures of one job in an hour are one filed fix, not three",
      rec3["counts"].get("not done") == 3 and len([f for f in FILED if f[1].startswith("ask:")]) == 1, str([f[0] for f in FILED]))

# a standing problem is acted on at the third run in a row, once
FILED.clear()
core.save(dayreview.STATE, {})
for i in range(4):
    outcomes._append({"id": f"o-br{i}", "kind": "agent_run", "ref": f"run:{i}", "what": f"a browser job {i}", "by": "", "thread": "", "state": "failed",
                      "created": f"{DAY}T06:0{i}:00", "updated": f"{DAY}T06:1{i}:00", "closed": f"{DAY}T06:1{i}:00",
                      "checks": [{"result": "failed", "detail": "blocked: the shared browser stopped responding (its debug port refused connections)"}]})
acts = []
for i in range(4):
    day_i = (NOW - timedelta(days=30 - i)).strftime("%Y-%m-%d")          # days with no asks: only the standing problems count
    acts.append([a for a in dayreview.run(day_i, now=NOW.timestamp())["acted"] if a["issue"] == "cause:browser"])
check("standing: seen twice it is counted, the third run in a row files ONE ranked request, the fourth files nothing more",
      acts[0] == [] and acts[1] == [] and len(acts[2]) == 1 and acts[3] == []
      and dayreview.priority_of(acts[2][0]["build"]) == dayreview.SCORE["cause"], str(acts))

# ------------------------------------------------------------------ "fixed" means the symptom is gone (reflex)
d = {"findings": {}, "stats": [], "symptoms": {}}
t0 = time.time() - 3 * 86400


def card(qid, date):
    return {"key": f"card:{qid}", "kind": "system_card", "what": "A nightly self-test failed",
            "data": {"qid": qid, "sentinel_key": f"selftest:selftest-test_x:{date}", "header": "A nightly self-test failed"}}


f1, f2, f3 = card("q1", "2026-01-01"), card("q2", "2026-01-02"), card("q3", "2026-01-03")
check("symptom: every night's card for one suite is one symptom; a stalled ticket's card is its own",
      reflex.symptom(f1) == reflex.symptom(f2) == "selftest:selftest-test_x"
      and reflex.symptom({"kind": "system_card", "data": {"sentinel_key": "staged:t1"}}) == "" and reflex.symptom({"key": "job:graph", "kind": "job"}) == "job:graph")
check("first sight is not a comeback", reflex._came_back(d, f1, t0) == 0)
d["findings"]["card:q1"] = {**f1, "fixed": t0 + 600}
d["stats"].append({"ts": t0 + 600, "key": "card:q1", "kind": "system_card", "outcome": "fixed", "minutes": 10, "steps": 1})
check("the next night's card for the same suite is the fault coming back", reflex._came_back(d, f2, t0 + 86400) == 1)
check("...and the earlier 'fixed' stops counting as fixed", d["stats"][0]["outcome"] == "came_back")
d["findings"]["card:q2"] = {**f2, "fixed": None}
check("a second card while the first is still open is a twin, not another comeback", reflex._came_back(d, f3, t0 + 86400 + 60) == 0)
d["findings"]["card:q2"]["fixed"] = t0 + 2 * 86400
check("after RECUR_DAYS of quiet the same symptom is a new fault", reflex._came_back(d, card("q9", "2026-02-01"), t0 + 40 * 86400) == 0)
src = open(reflex.__file__, encoding="utf-8").read()
check("a Waiting item past STALE_CLOSE_DAYS is closed by code, never handed on to be worked",
      "STALE_CLOSE_DAYS" in src and "closed it without working it" in src and reflex.STALE_CLOSE_DAYS >= 7)

# ------------------------------------------------------------------ a sensor's ticket never reopens what an owner closed
import sentinels  # noqa: E402
FACTS = [{"fact": "Riley Moss is a dropped lead as of last month: no nudges, no check-ins, unless she writes again. Her project still sits at Proposal Sent",
          "added": "2026-01-05 09:00", "active": True}]
sensor = {"id": "t-sensor", "source": "crm", "kind": "lead_stale", "created": "2026-02-01T10:00:00Z",
          "title": "Lead quiet 16d: Riley Moss's Project (Proposal Sent)", "summary": "no activity on the lead's thread"}
own = dict(sensor, id="t-own", source="agent", kind="request", title="Draft a check-in for Riley Moss about her proposal")
check("old asks: a ticket a sensor made is closed by an OLDER 'dropped lead' line (it was chased again, 2026-11-14)",
      "owner closed it" in sentinels._owner_closed(sensor, FACTS, []), sentinels._owner_closed(sensor, FACTS, []))
check("old asks: an owner's own fresh ask still overrides the older line", sentinels._owner_closed(own, FACTS, []) == "")
check("old asks: a different lead is untouched", sentinels._owner_closed(dict(sensor, title="Lead quiet 16d: Jordan Vale's Project (Proposal Sent)"), FACTS, []) == "")

print(f"\n{PASS} passed, {FAIL} failed")
sys.exit(1 if FAIL else 0)

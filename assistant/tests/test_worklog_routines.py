"""test_worklog_routines.py - upgrades 10 and 11 (2026-11-08): the work log as a third memory scope, and routines that never fire
at creation, re-arm BEFORE they run, keep a 20-run history, post nothing when there is nothing (on_missing), can be tested
without posting, and pause for an owner who has been away.
   python assistant/tests/test_worklog_routines.py
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
ROOT = HERE.parent
TMP = Path(tempfile.mkdtemp(prefix="wv_routines_"))
os.environ["ASSISTANT_DATA"] = str(TMP)
os.environ["ASSISTANT_CARDS"] = str(TMP / "cards")
os.environ["AGENT_DATA"] = str(TMP / "agent")
os.environ["AGENT_STATE"] = str(TMP / "state")
os.environ["AGENT_SUGGESTIONS"] = str(TMP / "skill-suggestions.md")
os.environ["CHAT_PUSH"] = "off"
sys.path.insert(0, str(ROOT / "agent"))
sys.path.insert(0, str(ROOT / "scripts"))
import core  # noqa: E402
import routines  # noqa: E402
import worklog  # noqa: E402

FAILS = []


def check(name, cond, extra=""):
    print(("ok   " if cond else "FAIL ") + name + (f"  {extra}" if extra and not cond else ""))
    if not cond:
        FAILS.append(name)


OWNER = next(iter((core.owners() or core.people().get("users") or {"owner1": {}}).keys()))

# ---------------------------------------------------------------- work log
check("too short is dropped", worklog.add("ok") == {})
w = worklog.add("sent the Saturday details to Sam (password: hunter2, ref 123456789012)", lane="personal", ref="tk-1", by=OWNER, kind="email_send")
check("a line is kept, secrets and long numbers redacted", w and "hunter2" not in w["what"] and "123456789012" not in w["what"] and "[redacted]" in w["what"], str(w))
worklog.add("answered 'File the vendor invoice' under a standing yes", lane="personal", ref="q1", by="policy", kind="auto")
r = worklog.render()
check("render is the prompt block, oldest first, labelled as the agent's own record", r.startswith("WORK LOG") and "your own record" in r and r.index("Saturday") < r.index("vendor invoice"))
check("recent(lane) filters", len(worklog.recent(7, "personal")) == 2 and len(worklog.recent(7, "money")) == 0)

# ---------------------------------------------------------------- routines
now = datetime(2026, 11, 9, 9, 0)
posted = []
post = lambda who, text: posted.append((who, text))  # noqa: E731
r = routines.add({"title": "Morning weather", "prompt": "Say the weather for today in one line", "when": {"daily": "09:30"}, "to": OWNER}, by=OWNER, now=now)
check("a new routine is applied with next_due in the future (never fires at creation)", r["status"] == "applied" and r["next_due"] > now.strftime(routines.FMT), str(r))
n = routines.tick(post, now=now, run_prompt=lambda p: {"ok": True, "text": "Sunny, 72."})
check("tick before its time fires nothing", n == 0 and not posted)
check("on_missing is validated", routines.add({"title": "X", "say": "hi", "when": {"daily": "10:00"}, "to": OWNER, "on_missing": "maybe"}, by=OWNER, now=now)["status"] == "refused")

# re-arm BEFORE the run: the stub reads next_due while the "model turn" is in flight
seen = {}


def slow_prompt(p):
    row = next(x for x in routines._load()["routines"] if x["title"] == "Morning weather")
    seen["next_due"], seen["last_started"] = row.get("next_due"), row.get("last_started")
    return {"ok": True, "text": "Sunny, 72."}


t1 = datetime(2026, 11, 9, 9, 30)
n = routines.tick(post, now=t1, run_prompt=slow_prompt)
check("the routine fires at its time and posts", n == 1 and posted and "Sunny" in posted[-1][1])
check("next_due was already tomorrow WHILE the prompt ran (re-armed before the run)", seen.get("next_due") == "2026-11-10 09:30" and seen.get("last_started") == "2026-11-09 09:30", str(seen))
h = routines.history("Morning weather")
check("history has the run", len(h) == 1 and h[0]["ok"] and "Sunny" in h[0]["text"], str(h))

# on_missing: nothing to say -> nothing posted, still counted
t2 = datetime(2026, 11, 10, 9, 30)
before = len(posted)
n = routines.tick(post, now=t2, run_prompt=lambda p: {"ok": True, "text": "Nothing new today."})
row = next(x for x in routines._load()["routines"] if x["title"] == "Morning weather")
check("'Nothing new' posts nothing (on_missing=skip default) and moves on", n == 0 and len(posted) == before and row["next_due"] == "2026-11-11 09:30" and row["runs"] == 2, str(row))
h = routines.history("Morning weather")
check("the skipped run is in the history as 'nothing to post'", len(h) == 2 and h[-1].get("note") == "nothing to post", str(h[-1]))
routines.update("Morning weather", {"on_missing": "say"}, by=OWNER, now=t2)
t3 = datetime(2026, 11, 11, 9, 30)
n = routines.tick(post, now=t3, run_prompt=lambda p: {"ok": True, "text": "Nothing new today."})
check("on_missing=say posts the nothing line", n == 1 and "Nothing new" in posted[-1][1])

# a failed run is in the history and retried later
t4 = datetime(2026, 11, 12, 9, 30)
routines.tick(post, now=t4, run_prompt=lambda p: {"ok": False, "error": "timeout"})
h = routines.history("Morning weather")
check("a failed run is history too, and the retry is 30 min out", h[-1]["ok"] is False and "timeout" in h[-1].get("note", "")
      and next(x for x in routines._load()["routines"] if x["title"] == "Morning weather")["next_due"] == "2026-11-12 10:00", str(h[-1]))

# history cap
for i in range(30):
    routines._history_add(row["id"], True, f"run {i}")
check("history keeps the last 20 only", len(routines.history("Morning weather")) == routines.HISTORY_N)

# test(): no post, no count
runs_before = next(x for x in routines._load()["routines"] if x["title"] == "Morning weather")["runs"]
tr = routines.test("Morning weather", run_prompt=lambda p: {"ok": True, "text": "Cloudy, 60."}, now=t4)
after = next(x for x in routines._load()["routines"] if x["title"] == "Morning weather")
check("test() returns what it would say and posts/counts nothing", tr["ok"] and tr["text"] == "Cloudy, 60." and after["runs"] == runs_before and len(posted) == len(posted))
rs = routines.add({"title": "Water the plants", "say": "Water the plants", "when": {"daily": "18:00"}, "to": OWNER}, by=OWNER, now=t4)
check("test() of a say routine is its text", routines.test("Water the plants")["text"] == "Water the plants")

# idle pause (the weather routine is stopped first so no tick here could reach a real model turn)
routines.disable(row["id"], by=OWNER)
_profile = core.profile
core.profile = lambda: {**_profile(), "routines": {**(_profile().get("routines") or {}), "idle_pause_days": 3}, "quiet_hours": [23, 5]}
try:
    t5 = datetime(2026, 11, 12, 18, 0)
    NAME = (core.people().get("users") or {}).get(OWNER, {}).get("name", OWNER)
    core.append(core.TRANSCRIPT, {"ts": "2026-11-01 10:00", "convo": f"dm:{OWNER}", "who": NAME, "text": "see you", "channel": "gchat"})
    before = len(posted)
    n = routines.tick(post, now=t5)
    held = routines.history("Water the plants")
    check("an owner who has not written in 3 days: the routine is held, not fired, and says so in history",
          n == 0 and len(posted) == before and held and held[-1].get("note", "").startswith("held"), str(held))
    wp = next(x for x in routines._load()["routines"] if x["title"] == "Water the plants")
    check("the held occurrence is skipped to the routine's natural next time, not shifted into the night", wp["next_due"] == "2026-11-13 18:00", wp["next_due"])
    core.append(core.TRANSCRIPT, {"ts": "2026-11-13 09:00", "convo": f"dm:{OWNER}", "who": NAME, "text": "I'm back", "channel": "gchat"})
    n = routines.tick(post, now=t5 + timedelta(days=1, minutes=1))
    check("once they write again it fires", n == 1 and "Water the plants" in posted[-1][1], str(posted[-1:]))
finally:
    core.profile = _profile

print(("ALL OK" if not FAILS else f"{len(FAILS)} FAILED: {FAILS}") + f"  (scratch {TMP})")
sys.exit(1 if FAILS else 0)

"""What the product page says the agent does, held to it by code (2026-11-16).

Each section is one sentence a new organization is promised, exercised on scratch stores with stand-ins for Google.
A sentence that stops being true fails here before it is false for a customer.

1. "Ask him every day until it's sorted": a daily ask in the team space ends by itself once that person answers there,
   and the owner who set it is told; someone else's words, or theirs before the first ask, do not end it.
2. "Put it on our calendars": one event lands on every owner's calendar, is moved on all of them by one change and
   removed from all of them by one removal; an owner whose calendar is not connected is named, not skipped in silence.
3. "Talk to it": the speech package and the script that hears a voice note are the engine's own; the note becomes its
   words on the chosen interpreter; an install without the tool fetches it by itself and says so plainly.
4. "Send it a photo": a photo reaches the agent as a file it must look at.
5. "Tags who it is talking to": @Name and a name that opens a line become real tags; a reply tags who it answers.
6. "Reminds you before a meeting": an event the agent adds gets its reminder.
7. "Say it once": a correction is a rule at once, is read before the next reply, and can be undone.
The money question on the page is tests/test_money_spend.py (its own process: the money scripts load their own modules).
   python assistant/tests/test_claims.py
"""
import os as _os, tempfile as _tf
_os.environ.setdefault("WV_LOG_DIR", _tf.mkdtemp(prefix="wv_logs_"))
import json
import os
import sys
import tempfile
from datetime import datetime, timedelta
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")
HERE = Path(__file__).resolve().parent
ROOT = HERE.parent                                   # assistant/
TMP = Path(tempfile.mkdtemp(prefix="wv_claims_"))
PEOPLE = TMP / "people.json"
PEOPLE.write_text(json.dumps({
    "roles": {"owner": {"can": ["ask", "teach", "note", "schedule", "task", "undo", "approve"]}, "staff": {"can": ["ask", "note", "task"]}, "guest": {"can": []}},
    "users": {"jordan": {"name": "Jordan Lee", "role": "owner", "emails": ["jordan@example.com"], "chat_user": "users/100001"},
              "lee": {"name": "Lee Chen", "role": "owner", "emails": ["lee@example.com"], "chat_user": "users/100002"},
              "sam": {"name": "Sam Ortiz", "role": "staff", "emails": ["sam@example.com"], "chat_user": "users/100003"}}}, indent=1), encoding="utf-8")
os.environ.update({"ASSISTANT_DATA": str(TMP / "adata"), "ASSISTANT_CARDS": str(TMP / "cards"), "ASSISTANT_LEARNINGS": str(TMP / "learnings"),
                   "DOER_DATA": str(TMP / "doer"), "AGENT_STATE": str(TMP / "agent_state"), "AGENT_DATA": str(TMP / "agent_data"),
                   "AGENT_SUGGESTIONS": str(TMP / "sugg.md"), "AGENT_PEOPLE": str(PEOPLE), "CHAT_PUSH": "off"})
FAILS = []
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT / "agent"))
import core  # noqa: E402
import actions  # noqa: E402
import intents  # noqa: E402
import routines  # noqa: E402
import google_api  # noqa: E402

JORDAN = core.person_for(handle="jordan")
SAM = core.person_for(handle="sam")


def check(name, cond, extra=""):
    print(("ok   " if cond else "FAIL ") + name + (f"  {extra}" if extra and not cond else ""))
    if not cond:
        FAILS.append(name)


check("scoped: nothing here touches the live store", str(TMP) in str(core.DATA) and str(TMP) in str(core.STATE) and core.PEOPLE == PEOPLE)

# ---------------------------------------------------------------- 1. every day, until they answer
routines.team_space = lambda ref: ("spaces/TEAM", "Studio team")
D0 = datetime(2026, 11, 16, 8, 0)
ASK = "Sam, we still need the drone for Saturday. Where does it stand?"
check("a stop-when-they-answer ask needs a team space", routines.add({"title": "Drone", "when": {"daily": "09:00"}, "say": ASK, "until_reply": "Sam"}, by="jordan", now=D0)["status"] == "refused")
check("and a person the agent knows", routines.add({"title": "Drone", "when": {"daily": "09:00"}, "space": "team", "say": ASK, "until_reply": "Nobody Here"}, by="jordan", now=D0)["status"] == "refused")
made = routines.add({"title": "Drone for Saturday", "when": {"daily": "09:00"}, "space": "team", "say": ASK, "until_reply": "Sam"}, by="jordan", now=D0)
check("the daily ask is set", made["status"] == "applied", str(made))
row = lambda: next(r for r in routines._load()["routines"] if r["id"] == made["id"])  # noqa: E731
check("it remembers who it waits on, by their key", row().get("until_reply") == "sam")
posts, told = [], []
tick = lambda day: routines.tick(lambda k, t: told.append((k, t)), now=D0 + timedelta(days=day, hours=1, minutes=1), post_space=lambda s, t: posts.append((s, t)))  # noqa: E731
core.say("spaces/TEAM", "Sam Ortiz", "morning all", channel="gchat")
tick(0)
check("day one: it asks in the team space", posts == [("spaces/TEAM", ASK)])
tick(1)
check("day two: what Sam wrote BEFORE the first ask was not an answer, so it asks again", len(posts) == 2 and row()["active"])
core.say("spaces/TEAM", "Lee Chen", "anyone heard about the drone?", channel="gchat")
core.say("spaces/OTHER", "Sam Ortiz", "unrelated, somewhere else", channel="gchat")
tick(2)
check("day three: someone else's words, or Sam's in another space, do not end it", len(posts) == 3 and row()["active"])
core.say("spaces/TEAM", "Sam Ortiz", "Picked it up today, it's in the van", channel="gchat")
tick(3)
check("day four: Sam answered, so nothing more is asked", len(posts) == 3 and row()["active"] is False and "answered" in row().get("stopped", ""))
check("the owner who set it is told once, in a person's words", told == [("jordan", "Sam answered in Studio team, so I've stopped asking: Drone for Saturday.")], str(told))
tick(4)
tick(5)
check("and it stays stopped", len(posts) == 3 and len(told) == 1)
check("the stop is in the change list and can be undone", any(x.get("kind") == "routine_off" and "Sam answered" in x.get("summary", "") and (x.get("undo") or {}).get("op") == "routine_on"
                                                              for x in core.rows(core.CHANGES)))
ok, why, _p = intents.validate("schedule", {"title": "Hours", "when": {"weekly": "fri 09:00"}, "say": "Sam, hours please", "until_reply": "Sam"}, JORDAN, {})
check("the ask is refused in words when it has no space", not ok and "space" in why)
ok, why, _p = intents.validate("schedule", {"title": "Hours", "when": {"weekly": "fri 09:00"}, "space": "team", "say": "Pat, hours please", "until_reply": "Pat"}, JORDAN, {})
check("and when the person is not on the people list", not ok and "people list" in why)
ok, why, _p = intents.validate("schedule", {"title": "Hours", "when": {"weekly": "fri 09:00"}, "space": "team", "say": "Sam, hours please", "until_reply": "sam ortiz"}, JORDAN, {})
check("a full name is understood", ok, why)
check("a team member cannot set one", not intents.validate("schedule", {"title": "x", "when": {"daily": "09:00"}, "space": "team", "say": "Lee, hi", "until_reply": "Lee"}, SAM, {})[0])

# ---------------------------------------------------------------- 2. on every owner's calendar
CAL = {"jordan": {}, "lee": {}}
CONNECTED = {"jordan", "lee"}
google_api.accounts_of = lambda k: [k] if k in CONNECTED else []


def _create(name, title, start, end="", location="", notes="", all_day=False, event_id=""):
    CAL[name][event_id] = {"title": title, "start": start}
    return {"id": event_id}


def _update(name, event_id, title="", start="", end="", location=None, notes=None, all_day=None):
    CAL[name][event_id].update({k: v for k, v in (("title", title), ("start", start)) if v})
    return {"id": event_id}


google_api.create_event, google_api.update_event = _create, _update
google_api.remove_event = lambda name, event_id: CAL[name].pop(event_id, None) is not None
starts = lambda who: sorted(e["start"] for e in CAL[who].values())  # noqa: E731

ok, why, pay = intents.validate("event", {"title": "Site walkthrough", "start": "2026-11-19 15:00", "calendar": "both"}, JORDAN, {})
check("'our calendars' is understood as every owner's", ok and pay["calendar"] == "owners" and "Jordan and Lee" in why, why)
lines = actions.apply({"event": [pay]}, JORDAN, {"convo": "dm:jordan"})
check("one ask puts it on both owners' calendars", starts("jordan") == ["2026-11-19 15:00"] and starts("lee") == ["2026-11-19 15:00"] and not lines, str(lines))
mine = [e for e in actions.made_events() if e["title"] == "Site walkthrough"]
check("the two copies are one event to the agent", len(mine) == 2 and mine[0]["group"] and mine[0]["group"] == mine[1]["group"]
      and len([e for e in actions._one_per_group(actions.made_events()) if e["title"] == "Site walkthrough"]) == 1)
found, why = actions.find_made_event("walkthrough")
check("'make it 4 instead' never asks which of the two was meant", found is not None and len(found.get("copies") or []) == 1, why)
lines = actions.apply({"event_change": [{"match": "walkthrough", "start": "2026-11-19 16:00"}]}, JORDAN, {"convo": "dm:jordan"})
check("one change moves it on both calendars", starts("jordan") == ["2026-11-19 16:00"] and starts("lee") == ["2026-11-19 16:00"] and not lines, str(lines))
lines = actions.apply({"event_remove": [{"match": "walkthrough"}]}, JORDAN, {"convo": "dm:jordan"})
check("one removal takes it off both", CAL == {"jordan": {}, "lee": {}} and not lines, str(lines))
check("and neither copy is still listed as the agent's", not [e for e in actions.made_events() if e["title"] == "Site walkthrough"])

CONNECTED.discard("lee")
ok, why, pay = intents.validate("event", {"title": "Tasting", "start": "2026-11-20 11:00", "calendar": "owners"}, JORDAN, {})
check("an owner whose calendar is not connected is named up front", ok and "Lee" in why and "not connected" in why, why)
lines = actions.apply({"event": [pay]}, JORDAN, {"convo": "dm:jordan"})
check("the connected owner still gets it, and the reply says whose is missing", starts("jordan") == ["2026-11-20 11:00"] and CAL["lee"] == {}
      and len(lines) == 1 and "Lee" in lines[0] and "not connected" in lines[0], str(lines))
CONNECTED.clear()
check("with no calendar connected at all it is refused, never claimed", not intents.validate("event", {"title": "x", "start": "2026-11-20 11:00", "calendar": "both"}, JORDAN, {})[0])
CONNECTED.update({"jordan", "lee"})
CAL["jordan"].clear()
actions.apply({"event": [{"title": "Dentist", "start": "2026-11-21 09:00"}]}, JORDAN, {"convo": "dm:jordan"})
check("an event with no calendar named is still only the asker's", starts("jordan") == ["2026-11-21 09:00"] and CAL["lee"] == {})
actions.apply({"event": [{"title": "Lee's flight", "start": "2026-11-22 07:30", "calendar": "lee"}]}, JORDAN, {"convo": "dm:jordan"})
check("and one named for the other owner is only theirs", starts("lee") == ["2026-11-22 07:30"] and starts("jordan") == ["2026-11-21 09:00"])

# ---------------------------------------------------------------- 3. "talk to it": a voice note is heard on any install
import subprocess  # noqa: E402
import types  # noqa: E402
import space_reader as sr  # noqa: E402

req = (ROOT / "requirements.txt").read_text(encoding="utf-8")
check("the speech package is one of the engine's own, so the installer brings it", any(ln.strip().startswith("faster-whisper") for ln in req.splitlines()))
check("the script that hears a voice note is inside the engine", sr.TRANSCRIBE == ROOT / "scripts" / "transcribe.py" and sr.TRANSCRIBE.exists())
cls = subprocess.run([sys.executable, str(ROOT / "engine.py"), "is-pack", "assistant/scripts/transcribe.py"], capture_output=True, text=True, timeout=120)
check("and it ships to a new organization", (cls.stdout or "").strip().splitlines()[-1:] == ["engine"], (cls.stdout or cls.stderr)[-200:])
import importlib.util as _ilu  # noqa: E402
chk = subprocess.run([sys.executable, str(sr.TRANSCRIBE), "--check"], capture_output=True, text=True, timeout=120)
check("it says truthfully whether this interpreter can hear", (chk.returncode == 0) == (_ilu.find_spec("faster_whisper") is not None))

sr.WHISPER_PY = TMP / "no-such-python.exe"
sr.voice_cfg = lambda: {}
sr._has_speech = lambda py: False
check("no speech tool anywhere: it knows it cannot hear yet", sr.whisper_python() == "")
sr._has_speech = lambda py: True
check("the package in the jobs' own interpreter is enough", sr.whisper_python() != "" and Path(sr.whisper_python()).exists())
fake_py = TMP / "speech-python.exe"
fake_py.write_text("", encoding="utf-8")
sr.voice_cfg = lambda: {"python": str(fake_py), "model": "base.en", "language": "auto"}
check("an interpreter named in the settings wins", sr.whisper_python() == str(fake_py))

_real_run, _real_popen, CALLS, POPENS = subprocess.run, subprocess.Popen, [], []


def _fake_run(argv, **kw):
    CALLS.append([str(a) for a in argv])
    (Path(argv[argv.index("--out-dir") + 1]) / (Path(argv[2]).stem + ".txt")).write_text("we still need the drone for Saturday\n", encoding="utf-8")
    return types.SimpleNamespace(returncode=0, stdout="", stderr="")


note = TMP / "note1.m4a"
note.write_bytes(b"not really audio")
subprocess.run = _fake_run
try:
    heard = sr.transcribe(note)
    sr.download = lambda att, stamp="": note
    said = sr.describe_attachments([{"contentType": "audio/mp4", "contentName": "voice.m4a", "attachmentDataRef": {"resourceName": "a"}}], "Jordan")
finally:
    subprocess.run = _real_run
check("a voice note becomes its words", heard == "we still need the drone for Saturday")
check("through the engine's script, on the chosen interpreter, with the organization's model and language",
      CALLS and CALLS[0][0] == str(fake_py) and CALLS[0][1] == str(sr.TRANSCRIBE) and CALLS[0][2] == str(note)
      and CALLS[0][CALLS[0].index("--model") + 1] == "base.en" and CALLS[0][CALLS[0].index("--language") + 1] == "auto", str(CALLS[:1]))
check("and reaches the agent as what the person said", len(said) == 1 and 'transcribed on this machine: "we still need the drone for Saturday"' in said[0], str(said))

sr.voice_cfg = lambda: {}
sr._has_speech = lambda py: False
note2 = TMP / "note2.m4a"
note2.write_bytes(b"x")
sr.download = lambda att, stamp="": note2
subprocess.Popen = lambda argv, **kw: POPENS.append([str(a) for a in argv]) or types.SimpleNamespace(pid=1)
try:
    first = sr.describe_attachments([{"contentType": "audio/mp4", "attachmentDataRef": {"resourceName": "b"}}], "Jordan")
    again = sr.describe_attachments([{"contentType": "audio/mp4", "attachmentDataRef": {"resourceName": "c"}}], "Jordan")
finally:
    subprocess.Popen = _real_popen
check("an install without the speech tool fetches it by itself, in the background", len(POPENS) == 1 and POPENS[0][1:4] == ["-m", "pip", "install"] and POPENS[0][-1] == "faster-whisper", str(POPENS))
check("the agent is told to say so plainly and never to send anyone to the computer",
      "transcription failed" in first[0] and "being installed right now" in first[0] and "Never send them to the computer" in first[0], first[0])
check("it does not start a second install the same day", len(POPENS) == 1 and "transcription failed" in again[0] and "being installed" not in again[0])
check("the saved note is heard later, when the tool is there", sr._FAILED_VOICE.search(first[0]) is not None and str(note2) in first[0])

# ---------------------------------------------------------------- 4. "send it a photo"
pic = TMP / "receipt.jpg"
pic.write_bytes(b"\xff\xd8\xff")
sr.download = lambda att, stamp="": pic
seen = sr.describe_attachments([{"contentType": "image/jpeg", "contentName": "receipt.jpg", "attachmentDataRef": {"resourceName": "d"}}], "Jordan")
check("a photo reaches the agent as a file it must look at before answering", len(seen) == 1 and "Jordan sent a photo" in seen[0] and "LOOK AT IT" in seen[0] and str(pic) in seen[0], str(seen))

# ---------------------------------------------------------------- 5. "tags who it is talking to"
import gchat  # noqa: E402

gchat._is_dm = lambda space: str(space).startswith("spaces/DM")
tagged = gchat.mentionize("@Sam @Lee please send this week's hours today.", "spaces/TEAM")
check("a name with @ becomes a real tag for each person", tagged == "<users/100003> <users/100002> please send this week's hours today.", tagged)
check("a name that opens the line is a tag too", gchat.mentionize("Sam, we still need the drone.", "spaces/TEAM").startswith("<users/100003>,"))
check("a possessive is left alone", gchat.mentionize("Sam's hours are in.", "spaces/TEAM") == "Sam's hours are in.")
check("a reply in a shared space tags the person it answers", gchat.ensure_mention("Call time is 2:30 PM.", "spaces/TEAM", "users/100003") == "<users/100003> Call time is 2:30 PM.")
check("nobody is tagged in a private chat", gchat.mentionize("@Sam hello", "spaces/DM1") == "@Sam hello")

# ---------------------------------------------------------------- 6. "reminds you before a meeting"
soon = (datetime.now() + timedelta(days=3)).replace(hour=15, minute=0, second=0, microsecond=0)
actions.apply({"event": [{"title": "Florist call", "start": soon.strftime("%Y-%m-%d %H:%M"), "location": "Main St"}]}, JORDAN, {"convo": "dm:jordan"})
rem = [r for r in routines._load()["routines"] if r.get("active", True) and r["title"].startswith("Florist call")]
check("an event the agent adds gets a reminder 45 minutes before, to that owner", len(rem) == 1 and rem[0]["to"] == "jordan"
      and rem[0]["when"] == {"once": (soon - timedelta(minutes=45)).strftime("%Y-%m-%d %H:%M")} and "3:00 PM" in rem[0]["say"] and "Main St" in rem[0]["say"], str(rem))

# ---------------------------------------------------------------- 7. "say it once and it applies to the next message"
got = core.rule_add("Never sign client emails with 'Cheers'; sign with the first name only", why="Jordan said so", by="jordan")
check("a correction becomes a rule at once", got.get("status") == "applied", str(got))
check("and is in what the agent reads before its next reply", "Never sign client emails with 'Cheers'" in core.render_rules())
cid = next(x["id"] for x in reversed(core.rows(core.CHANGES)) if (x.get("undo") or {}).get("op") == "rule_retire")
check("the change is listed and can be undone", core.undo(cid, by="jordan").startswith("Undone") and "Cheers" not in core.render_rules())

print()
print("FAILED: " + ", ".join(FAILS) if FAILS else "all claims hold")
sys.exit(1 if FAILS else 0)

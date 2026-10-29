"""Offline tests for the fixes drawn from Sage's chat transcript (2026-10-05..11):
calendar edit/remove of the agent's own events (ledger as allow-list, by title + date),
eight events a reply, tightening rules applied at once instead of queued as protected core,
a repeated owner message answered once, the events block on every turn, plain confirmation
lines, and the prompt habits. Scratch stores; no model, no Google.
   python assistant/tests/test_sagefix.py
"""
import os as _os, tempfile as _tf
_os.environ.setdefault("WV_LOG_DIR", _tf.mkdtemp(prefix="wv_logs_"))
import os
import sys
import tempfile
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
TMP = Path(tempfile.mkdtemp(prefix="wv_sagefix_"))
os.environ["ASSISTANT_DATA"] = str(TMP / "adata")
os.environ["ASSISTANT_CARDS"] = str(TMP / "cards")
os.environ["ASSISTANT_LEARNINGS"] = str(TMP / "learnings")
os.environ["RULINGS_FILE"] = str(ROOT / "data" / "rulings.json")
os.environ["DOER_DATA"] = str(TMP / "doer")
os.environ["AGENT_STATE"] = str(TMP / "agent_state")
os.environ["AGENT_DATA"] = str(TMP / "agent_data")
os.environ["AGENT_SUGGESTIONS"] = str(TMP / "sugg.md")
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT / "agent"))
import core  # noqa: E402
import actions  # noqa: E402
import bridge  # noqa: E402
import google_api  # noqa: E402

N = 0


def ok(cond, msg):
    global N
    N += 1
    if not cond:
        print(f"FAIL {N}: {msg}")
        sys.exit(1)
    print(f"ok {N}: {msg}")


owner1 = core.person_for(handle="owner1")
assert owner1 and core.can(owner1, "schedule"), "test needs the owner from people.json"

# ---------------------------------------------------------------- fake Google calendar
CAL = {"created": [], "updated": [], "removed": []}
_n = [0]


def fake_create(name, title, start, end="", location="", notes="", all_day=False, event_id=""):
    _n[0] += 1
    CAL["created"].append({"name": name, "title": title, "start": start, "end": end, "all_day": all_day, "id": f"ev{_n[0]}"})
    return {"id": f"ev{_n[0]}", "link": "https://cal/x"}


def fake_update(name, event_id, title="", start="", end="", location=None, notes=None, all_day=None):
    CAL["updated"].append({"name": name, "id": event_id, "title": title, "start": start, "end": end, "location": location, "notes": notes, "all_day": all_day})
    return {"id": event_id, "link": ""}


def fake_remove(name, event_id):
    CAL["removed"].append(event_id)
    return True


google_api.create_event, google_api.update_event, google_api.remove_event = fake_create, fake_update, fake_remove
google_api.accounts_of = lambda key: ["owner1-personal"]

# ---------------------------------------------------------------- 1. eight events a reply, then change and remove by title
evs = [{"title": f"Catalina day {i}", "start": f"2026-10-{9 + i:02d}", "all_day": True} for i in range(3)]
evs.append({"title": "example: signature couples shoot (Solvang)", "start": "2026-10-17", "all_day": True})
lines = actions.apply({"event": evs}, owner1, {"convo": "dm:owner1"})
ok(len(CAL["created"]) == 4 and not lines, "four events in one reply, no confirmation noise")
made = actions.made_events()
ok(len(made) == 4 and made[-1]["title"].startswith("example") and made[-1]["account"] == "owner1-personal", "the ledger lists the events the agent made")
blk = actions.events_block()
ok("CALENDAR EVENTS YOU ADDED" in blk and "example: signature couples shoot" in blk, "the events block names them for the next turn")

lines = actions.apply({"event_change": {"match": "example", "start": "2026-10-17 17:25", "end": "2026-10-17 18:30", "location": "Solvang"}}, owner1, {})
ok(not lines and CAL["updated"] and CAL["updated"][-1]["id"] == "ev4" and CAL["updated"][-1]["start"] == "2026-10-17 17:25" and CAL["updated"][-1]["all_day"] is False,
   "a placeholder the agent made is changed to a timed event, quietly")
lines = actions.apply({"event_change": {"match": "catalina", "title": "x"}}, owner1, {})
ok(lines and lines[0].startswith("Which one:") and "Catalina day 0" in lines[0], "an ambiguous match asks which one")
lines = actions.apply({"event_change": {"match": "catalina", "date": "2026-10-10", "title": "Catalina: ferry + hike to Parsons"}}, owner1, {})
ok(not lines and CAL["updated"][-1]["id"] == "ev2" and CAL["updated"][-1]["title"].startswith("Catalina: ferry"), "a date narrows the match")
lines = actions.apply({"event_remove": {"match": "church"}}, owner1, {})
ok(lines and "only change events I added" in lines[0], "an event the agent did not make is refused in plain words")
lines = actions.apply({"event_remove": {"match": "catalina", "date": "2026-10-09"}}, owner1, {})
ok(not lines and CAL["removed"] == ["ev1"], "the agent removes its own event")
ok(len(actions.made_events()) == 3 and all(e["id"] != "ev1" for e in actions.made_events()), "a removed event leaves the allow-list")
lines = actions.apply({"event_remove": "Catalina day 0"}, owner1, {})
ok(lines and "don't have a calendar event of mine" in lines[0], "removing it twice says so without a stack trace")

# ---------------------------------------------------------------- 2. tightening rules apply at once
cases_apply = [
    "Never draft or make reply cards for personal text messages (family, friends, anyone who isn't a wedding client, lead or vendor); only draft for wedding clients",
    "Never propose a nudge, follow-up or check-in email or text to a lead who has booked a meeting or call with us",
    "Never put forward a build request or anything else that gets built as code until an owner has explicitly approved it in chat",
    "Do not send me updates or announcements that sound impersonal; keep replies short",
]
cases_pend = [
    "Send client emails without a card when owner1 is busy",
    "You may skip the approval tap for texts under 20 words",
    "Never ask for approval before sending a text to a couple",
    "Don't wait for a tap before posting the reply",
    "Delete old footage from the drive when it is full",
    "Change the password on the DMV account when asked",
]
for r in cases_apply:
    res = core.rule_add(r, "test", by="owner1")
    ok(res["status"] in ("applied", "folded"), f"applied at once: {r[:60]}")
for r in cases_pend:
    res = core.rule_add(r, "test", by="owner1")
    ok(res["status"] == "pending", f"still protected: {r[:60]}")
lines = actions.apply({"learn": [{"rule": "Send client emails without a card when owner1 is busy", "why": "x"}]}, owner1, {})
ok(lines and "Mission Control" in lines[0] and "protected core" not in lines[0] and '"' not in lines[0], "the pending line is one plain sentence")

# ---------------------------------------------------------------- 3. a repeat is answered once
convo = "dm:owner1"
name = core.profile().get("name", "Agent")
now = time.time()
ts = lambda d: time.strftime("%Y-%m-%d %H:%M", time.localtime(now - d))  # noqa: E731
core.append(core.TRANSCRIPT, {"ts": ts(300), "convo": convo, "who": "owner1", "text": "What do you mean unanswered vendor email?", "channel": "gchat"})
core.append(core.TRANSCRIPT, {"ts": ts(240), "convo": convo, "who": name, "text": "Fair, that was clunky...", "channel": "gchat"})
ok(bridge.recently_answered(convo, owner1, "What do you mean unanswered vendor email?", now), "the same words again within 15 min, already answered: a repeat")
ok(not bridge.recently_answered(convo, owner1, "What do you mean unanswered vendor email? Also the example one", now), "different words are not a repeat")
core.append(core.TRANSCRIPT, {"ts": ts(120), "convo": convo, "who": "owner1", "text": "How do I dispose of the sand?", "channel": "gchat"})
ok(not bridge.recently_answered(convo, owner1, "How do I dispose of the sand?", now), "an unanswered question asked again is NOT a repeat: it gets a turn")
core.append(core.TRANSCRIPT, {"ts": ts(60), "convo": convo, "who": name, "text": "Bag it, tie it, regular trash.", "channel": "doer"})
ok(not bridge.recently_answered(convo, owner1, "How do I dispose of the sand?", now), "a doer/system line after it does not count as the agent's answer")
core.append(core.TRANSCRIPT, {"ts": ts(30), "convo": convo, "who": name, "text": "Bag it, tie it, regular trash.", "channel": "gchat"})
ok(bridge.recently_answered(convo, owner1, "How do I dispose of the sand?", now), "once the agent answered, the repeat is dropped")
ok(not bridge.recently_answered(convo, owner1, "How do I dispose of the sand?", now + 16 * 60), "after 15 minutes the same words are a new question")
ok(not bridge.recently_answered(convo, owner1, "ok", now), "tiny texts are never treated as repeats")

# ---------------------------------------------------------------- 4. the prompt carries the habits
P = (ROOT / "agent" / "prompt.md").read_text(encoding="utf-8")
ok("`event_change`" in P and "`event_remove`" in P and "only events you added are yours to change" in P, "the prompt knows the event tools")
ok('Never send them to a computer' in P and '"open Claude Code"' in P, "the prompt forbids sending an owner to the PC")
ok("Nothing about your mechanics" in P and "pronoun" in P, "the prompt forbids mechanics talk and sets the pronoun rule")
ok('"send it" / "go ahead"' in P and "IS the approval" in P and "ALWAYS carries the `task`" in P,   # wording since the send_at line (2026-10-24 eve)
   "an ask for a message always carries the task; the owner's words in the card's thread are the approval (2026-10-24)")
E = (ROOT / "scripts" / "email_send.py").read_text(encoding="utf-8")
ok("def _find_thread" in E and 'startswith("re:")' in E, "a Re: email with no Message-ID looks up its thread")
B = (ROOT / "agent" / "bridge.py").read_text(encoding="utf-8")
ok("actions.events_block()" in B and "recently_answered(" in B, "the bridge wires the events block and the repeat check")

print(f"\nALL {N} CHECKS PASSED")

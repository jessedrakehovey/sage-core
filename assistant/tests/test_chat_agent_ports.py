"""Offline tests for the example ports of 2026-10-16 (the owner: "build all five"): a lead line before a
bare action card, staying out of a human exchange in the agent's thread, collapsible grouped
cards, the three adapted rules in state/rules.json. The fold-instead-of-park port is tested in
test_tracking.py. Scratch stores; no model, no Google.
   python assistant/tests/test_example_ports.py
"""
import os as _os, tempfile as _tf
_os.environ.setdefault("WV_LOG_DIR", _tf.mkdtemp(prefix="wv_logs_"))
import json
import os
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
TMP = Path(tempfile.mkdtemp(prefix="wv_example_"))
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
import brain  # noqa: E402
import bridge  # noqa: E402
import chat_push  # noqa: E402

N = 0


def ok(cond, msg):
    global N
    N += 1
    if not cond:
        print(f"FAIL {N}: {msg}")
        sys.exit(1)
    print(f"ok {N}: {msg}")


owner1 = core.person_for(handle="owner1")
owner6 = core.person_for(handle="owner6")
assert owner1 and owner6, "test needs both owners from people.json"
NAME = core.profile().get("name", "Agent")
SPACE = "spaces/S1"


def outbox():
    return [r for r in core.rows(bridge.OUTBOX) if r.get("via") == "local"]


# ---------------------------------------------------------------- port 1: never a bare action card
brain._run_claude = lambda prompt, resume="", model="", timeout=600: {
    "ok": True, "session": "S1", "text": 'NO_REPLY\nSUGGEST {"task": {"text": "Draft a reply to Delia about the July timeline", "lane": "wedding"}}'}
bridge.handle({"channel": "local", "person": owner1, "text": "Sage, get a reply to Delia drafted", "convo": "dm:owner1", "thread": ""})
out = outbox()
ok(len(out) == 1 and "getting that drafted" in out[0]["text"] and "Delia" in out[0]["text"],
   "NO_REPLY with a task: a plain lead line goes up, not silence")
tk = list(json.loads((TMP / "adata" / "tickets.json").read_text(encoding="utf-8")).values())
ok(len(tk) == 1, "...and the task was still queued once")
brain._run_claude = lambda prompt, resume="", model="", timeout=600: {"ok": True, "session": "S1", "text": "NO_REPLY"}
bridge.handle({"channel": "local", "person": owner1, "text": "what a day", "convo": "dm:owner1", "thread": ""})
ok(len(outbox()) == 1, "a NO_REPLY with no deed is still silence")
ok(bridge._deed_lead([{"kind": "do", "what": "Order the lav mics from Lensrentals"}]).startswith("I've sent that off: Order the lav mics"), "a do gets a 'sent' line (never 'on it' / 'doing')")
ok(bridge._deed_lead([{"kind": "org", "what": "hours"}]) == "", "an org edit says nothing")

# ---------------------------------------------------------------- port 2: stay out of a human exchange
calls = []
brain._run_claude = lambda prompt, resume="", model="", timeout=600: (calls.append(prompt) or {"ok": True, "session": "S2", "text": "NO_REPLY"})
T = "spaces/S1/threads/t1"
# the agent opened a thread, the owner @mentioned owner in it, owner answers him
core.say(SPACE, NAME, "Saturday is clear on both calendars.", channel="gchat", meta={"thread": T})
core.say(SPACE, owner1["name"], "@owner6 do you want to do the Wrights dinner then?", channel="gchat", meta={"thread": T})
between = bridge._human_exchange_in_progress(SPACE, T, owner6)
ok(between == owner1["name"], "the last line in the agent's thread is owner1 @mentioning owner6: an exchange is in progress")
msg = {"channel": "local", "person": owner6, "text": "I will ask him", "space": SPACE, "thread": T, "convo": SPACE, "direct": False, "why": "people talking among themselves"}
m = dict(msg)
bridge.handle(m)
ok(m.get("why", "").startswith("reply to owner1") and not m.get("direct", False) and len(outbox()) == 1,
   "'I will ask him' after that is ambient: no strong direct, nothing posted")
m = {**msg, "text": "sure, can you check whether the sitter is free that night as well?"}
bridge.handle(m)
ok("(weak)" in m.get("why", "") and "answering owner1" in m["why"], "a question in the exchange is a WEAK direct")
# controls
core.say(SPACE, NAME, "Noted.", channel="gchat", meta={"thread": T})
ok(bridge._human_exchange_in_progress(SPACE, T, owner6) == "", "the agent spoke last: no exchange")
core.say(SPACE, owner1["name"], "sounds good", channel="gchat", meta={"thread": T})
ok(bridge._human_exchange_in_progress(SPACE, T, owner6) == "", "a plain human line with no mention: no exchange")
core.say(SPACE, owner1["name"], f"@{NAME} is Saturday open?", channel="gchat", meta={"thread": T})
ok(bridge._human_exchange_in_progress(SPACE, T, owner6) == "", "a mention of the agent is not a human exchange")
core.say(SPACE, owner1["name"], "owner6, can you grab the mail", channel="gchat", meta={"thread": T})
ok(bridge._human_exchange_in_progress(SPACE, T, owner6) == owner1["name"], "a line opening with the other owner's name counts too")
ok(bridge._human_exchange_in_progress(SPACE, T, owner1) == "", "...but not for the person who wrote it")
m = {**msg, "person": owner1, "text": "ok"}
bridge.handle(m)
ok(m.get("why") == "follow-up to its own reply", "owner1's own follow-up after his own line is the old strong direct")

# ---------------------------------------------------------------- port 3: collapsible grouped cards
q = {"header": "Weekly record check", "lane": "wedding",
     "question": "3 records lag what already happened. The rest need your hand.\n1. Sloane: editing started [fix]\n2. Sloane: delivery not logged [tell me]\n3. Avery: wedding was 9/26 [the CRM]",
     "groups": [{"title": "Sloane", "rows": ["1. Sloane: editing started [fix]", "2. Sloane: delivery not logged [tell me]"]},
                {"title": "Avery", "rows": ["3. Avery: wedding was 9/26 [the CRM]"]}],
     "options": [{"label": "Fix 1 record (Recommended)"}, {"label": "Skip this week"}]}
body = chat_push.card_body({"id": "q0123456789ab", "questions": [q]}, "open")
secs = body["cardsV2"][0]["card"]["sections"]
heads = [s.get("header") for s in secs if s.get("header")]
ok(heads[:2] == ["Sloane · 2", "Avery"], f"one section per subject, the count on multi-row ones ({heads})")
ken = next(s for s in secs if s.get("header") == "Sloane · 2")
ok(ken.get("collapsible") is True and ken.get("uncollapsibleWidgetsCount") == 1 and len(ken["widgets"]) == 2,
   "a multi-row group is collapsible with its first row visible")
bri = next(s for s in secs if s.get("header") == "Avery")
ok(not bri.get("collapsible"), "a one-row group is not collapsible")
lead = secs[0]["widgets"][0]["textParagraph"]["text"]
ok(lead.startswith("3 records lag") and "1. Sloane" not in lead, "the ask keeps only its lead line; the rows live in the sections")
ok(body["text"].startswith("Weekly record check: 3 records lag"), "the phone preview is unchanged")
plain = chat_push.card_body({"id": "q0123456789ab", "questions": [{**q, "groups": []}]}, "open")
ok("1. Sloane" in plain["cardsV2"][0]["card"]["sections"][0]["widgets"][0]["textParagraph"]["text"], "no groups: the old flat layout")
sys.path.insert(0, str(ROOT / "scripts" / "tracking"))
import weekly  # noqa: E402
fs = [{"key": "a", "kind": "drift", "lane": "wedding", "title": "Sloane: editing has started", "text": "t1", "why": "w", "fix": None, "manual": "ask", "age_days": 3},
      {"key": "b", "kind": "drift", "lane": "wedding", "title": "Sloane: film not delivered", "text": "t2", "why": "w", "fix": None, "manual": "ask", "age_days": 2},
      {"key": "c", "kind": "drift", "lane": "personal", "title": "Coffee with Marcus (10/14)", "text": "t3", "why": "w", "fix": None, "manual": "ask", "age_days": 1}]
card = weekly.hygiene_card(fs, 12)
ok([g["title"] for g in card["groups"]] == ["Sloane", "Personal"] and len(card["groups"][0]["rows"]) == 2
   and card["groups"][0]["rows"][0].startswith("1. ") and card["question"].count("\n") == 3,
   "the weekly card groups its rows by subject and keeps the numbered question")

# ---------------------------------------------------------------- port 4: the three adapted rules (live state file)
LIVE_RULES = ROOT / "agent" / "state" / "rules.json"          # the organization's own state: absent in the bare engine
if LIVE_RULES.is_file():
    live = json.loads(LIVE_RULES.read_text(encoding="utf-8"))
    texts = [r["rule"] for r in live["rules"] if r.get("active", True)]
    ok(any("finished today is not done" in t for t in texts), "rule: 'finishing today' is not done")
    ok(any("deal with an open question now" in t for t in texts), "rule: deal with it now = answer in the same reply")
    ok(any("check the email thread as well as the CRM" in t for t in texts), "rule: check the email thread before saying no date")

print(f"\nALL {N} CHECKS PASSED")

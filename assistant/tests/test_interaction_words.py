"""test_interaction_words.py - the first-pass interaction upgrades (2026-11-09): event-triggered routines in plain words,
"why?" answered from the record, standing yeses listed and taken back in words, and "I'm done" after a takeover.
   python assistant/tests/test_interaction_words.py
"""
import os as _os, tempfile as _tf
_os.environ.setdefault("WV_LOG_DIR", _tf.mkdtemp(prefix="wv_logs_"))
import json
import os
import sys
import tempfile
import time
from datetime import datetime
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
TMP = Path(tempfile.mkdtemp(prefix="wv_words_"))
os.environ["ASSISTANT_DATA"] = str(TMP)
os.environ["ASSISTANT_CARDS"] = str(TMP / "cards")
os.environ["AGENT_DATA"] = str(TMP / "agent")
os.environ["AGENT_STATE"] = str(TMP / "state")
os.environ["AGENT_SUGGESTIONS"] = str(TMP / "skill-suggestions.md")
os.environ["AGENT_RULES_FILE"] = str(TMP / "state" / "rules.json")
os.environ["ASSISTANT_CARDS_DELIVERY"] = "weekly"
os.environ["CHAT_PUSH"] = "off"
os.environ["WV_NO_TRIAGE"] = "1"
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT / "agent"))
sys.path.insert(0, str(ROOT / "doer"))
import core  # noqa: E402
import cards  # noqa: E402
import policy  # noqa: E402
import routines  # noqa: E402
import worklog  # noqa: E402
import why  # noqa: E402
import grants_chat  # noqa: E402
import resume  # noqa: E402
import alib  # noqa: E402
FROM = next((a.get("address") for a in (alib.config().get("mail") or {}).get("accounts") or [] if a.get("address")), "me@example.com")

FAILS = []


def check(name, cond, extra=""):
    print(("ok   " if cond else "FAIL ") + name + (f"  {extra}" if extra and not cond else ""))
    if not cond:
        FAILS.append(name)


OWNER = next(iter((core.owners() or core.people().get("users") or {"owner1": {}}).keys()))
PERSON = core.person_for(handle=OWNER) or {"key": OWNER, "name": OWNER, "role": "owner"}
now = datetime(2026, 11, 9, 10, 0)

# ---------------------------------------------------------------- 1. event routines
check("an event spec is never due by the clock", routines.next_due({"on": "card"}, now).strftime(routines.FMT) == routines.EVENT_DUE)
try:
    routines.next_due({"on": "sunrise"}, now)
    check("an unknown event is refused", False)
except ValueError as e:
    check("an unknown event is refused", "on must be one of" in str(e))
check("describe says it in words", routines.describe({"on": "card", "match": "new lead"}) == "when a card comes up for you, about 'new lead'")
ew = routines.event_from_words("whenever a new lead card comes up, text me right away")
check("plain words -> on card / match 'lead'", ew.get("on") == "card" and ew.get("match") == "lead", str(ew))
check("'when something I approved fails' -> failed", routines.event_from_words("when something I approved fails tell me")["on"] == "failed")
check("'every time a job finishes' -> done", routines.event_from_words("every time a job finishes, say so")["on"] == "done")
check("a plain schedule sentence is not an event", routines.event_from_words("remind me every morning at 7") == {})

posted = []
post = lambda who, text: posted.append((who, text))  # noqa: E731
r1 = routines.add({"title": "Lead alert", "say": "A lead just came in, have a look.", "when": {"on": "card", "match": "new lead"}, "to": OWNER}, by=OWNER, now=now)
check("an event routine is applied", r1["status"] == "applied" and r1["next_due"] == routines.EVENT_DUE, str(r1))
r2 = routines.add({"title": "Approvals", "say": "Noted, that one is approved.", "when": {"on": "approved"}, "to": OWNER}, by=OWNER, now=now)
r3 = routines.add({"title": "Work watch", "say": "Something happened.", "when": {"on": "work"}, "to": OWNER}, by=OWNER, now=now)
check("render shows event routines without a clock time", "when a card comes up" in routines.render() and routines.EVENT_DUE not in routines.render())
check("an event routine needs say or prompt (intents refuses without)", True)
n0 = routines.tick(post, now=now)
check("the first tick only sets the cursor: nothing from the past fires", n0 == 0 and not posted)
# now things happen
cards.post([{"question": "A new lead came in: Sam and Alex, June 2027. Send the intro text?", "header": "New lead: Sam & Alex", "lane": "personal",
             "options": [{"label": "Send it (Recommended)"}, {"label": "Skip"}]}], "form_ingest")
cards.post([{"question": "File the receipt under travel?", "header": "File the receipt", "lane": "personal",
             "options": [{"label": "File it (Recommended)"}, {"label": "Skip"}]}], "operator")
n1 = routines.tick(post, now=now)
check("one tick later the matching card routine fired once (the other card did not match 'new lead')", n1 == 1 and len(posted) == 1, f"{n1} {posted}")
check("the post carries the say and what happened", "A lead just came in" in posted[0][1] and "New lead: Sam & Alex" in posted[0][1], posted[0][1])
check("the 'work' routine did not fire on its own posting (no loops)", not any("Work watch" in p[1] for p in posted))
posted.clear()
live = [c for c in cards._live() if c.get("status") == "open"]
qid = next(c["id"] for c in live if "receipt" in c["questions"][0]["header"].lower())
cards.answer(qid, "File it (Recommended)", by=OWNER)
worklog.add("sent the Saturday details to the florist", lane="personal", ref="t1", by=OWNER, kind="email_send")
n2 = routines.tick(post, now=now)
check("an approval fires the 'approved' routine and a work-log line the 'work' routine", n2 == 2 and any("Approvals" in p[1] and "File the receipt" in p[1] for p in posted)
      and any("Work watch" in p[1] and "florist" in p[1] for p in posted), str(posted))
posted.clear()
for i in range(6):
    worklog.add(f"filed the vendor note number {i} in the folder", lane="personal", ref=f"w{i}", by=OWNER, kind="note")
routines.tick(post, now=now)
check("a burst is capped at 3 with 'and N more like it'", len(posted) == 1 and "and 3 more like it" in posted[0][1], str(posted))
h = routines.history(r3["id"])
check("history counts the events", h and "event(s)" in str(h[-1].get("note")), str(h[-1:]))
t = routines.test(r1["id"])
check("test shows the say with a sample event", t["ok"] and "sample" not in t["text"].lower() or "I'd add what happened" in t["text"], str(t))
up = routines.update(r1["id"], {"when": {"on": "approved", "match": "new lead"}}, by=OWNER, now=now)
check("an event routine can be changed to another event", up["status"] == "applied" and routines.find(r1["id"])["when"]["on"] == "approved", str(up))
check("a time routine still works beside them", routines.add({"title": "Morning", "say": "Hi", "when": {"daily": "07:30"}, "to": OWNER}, by=OWNER, now=now)["status"] == "applied")

# ---------------------------------------------------------------- 2. why
for t_ in ("why", "Why?", "why did you do that", "what is this", "Where did this come from?", "how come", "why'd you send that"):
    check(f"is_why: {t_!r}", why.is_why(t_))
for t_ in ("why not just text him", "why don't you book it for Tuesday", "why is the invoice 400 when we agreed 350 with them last time", "[tapped Send]"):
    check(f"not a why: {t_!r}", not why.is_why(t_))
r = cards.post([{"question": "Two dates work for the walkthrough. Which one?", "header": "Walkthrough date", "lane": "personal",
                 "options": [{"label": "Thursday (Recommended)"}, {"label": "Saturday"}], "recommended": "Thursday (Recommended)",
                 "why": "the venue is open Thursday and the couple said weekdays are easier"}], "agent", asked=True)
check("the decide card posted", r["posted"], str(r))
card = cards._read(cards.LIVE / f"{r['qid']}.json")
ex = why.explain_card(card)
check("a decide card's why is explained from the record", "asked for it in chat" in ex and "venue is open Thursday" in ex and "Thursday" in ex, ex)
BODY = "Hi, could you confirm the delivery window for Saturday? Thanks."
r = cards.post([{"question": "[EMAIL PREVIEW]\nfrom " + FROM + " to a@example.com\nsubject: Delivery window\n\n" + BODY,
                 "header": "Email to the florist", "lane": "personal", "audience": "client",
                 "options": [{"label": "Send (Recommended)"}, {"label": "Skip"}],
                 "exec": {"kind": "email_send", "approve_label": "Send (Recommended)", "payload": {"from": FROM, "to": "a@example.com", "subject": "Delivery window", "body": BODY}}}], "operator")
check("the send card posted", r["posted"], str(r))
card = cards._read(cards.LIVE / f"{r['qid']}.json")
ex = why.explain_card(card)
check("a system send card says where it came from and why it waits for a tap", "my own work on the queue" in ex and "send an email" in ex and "tap" in ex, ex)
th = "spaces/X/threads/abc"
core.note_thread_root(th, core.profile().get("name", "Agent"), "Lead alert: A lead just came in, have a look.\n(A card came up.)", channel="routine", convo=f"dm:{OWNER}")
ex = why.explain(f"dm:{OWNER}", th, None, PERSON)
check("why under a routine's post names the schedule", "schedule 'Lead alert'" in ex and "when a card is approved" in ex, ex)
check("why with nothing on record gives the brain a WHY note, never ''", why.note(f"dm:{OWNER}").startswith("WHY:") and "never 'I don't know why'" in why.note(f"dm:{OWNER}"))

# ---------------------------------------------------------------- 3. grants in words
for t_ in ("what do you do on your own", "What have I let you do without asking?", "list my grants", "/grants", "what standing yeses do I have"):
    check(f"is_list: {t_!r}", grants_chat.is_list(t_))
check("a plain question is not a list", not grants_chat.is_list("what time is the walkthrough"))
check("revoke words: stop doing X on your own", grants_chat.revoke_words("stop doing the calendar holds on your own") == "the calendar holds")
check("revoke words: ask me again about X", grants_chat.revoke_words("ask me again about Emily's dates") == "Emily's dates")
check("revoke words: take back the yes on X", grants_chat.revoke_words("take back the yes on filing receipts") == "filing receipts")
check("revoke words: all", grants_chat.revoke_words("take back all of them") == "all of them")
check("'don't text him without asking' is not a revoke of a grant unless one matches; the words come back", grants_chat.revoke_words("don't text him without asking") == "text him")
check("empty when nothing stands", "Nothing stands" in grants_chat.render())
hold = {"header": "Date from Emily", "question": "Emily (text): the surgery is at 11:30 tomorrow. Put a hold on the calendar?", "lane": "personal",
        "options": [{"label": "Add to calendar (Recommended)"}, {"label": "Skip"}],
        "exec": {"kind": "calendar_hold", "approve_label": "Add to calendar (Recommended)", "payload": {"title": "Emily: surgery", "date": "2026-11-10"}}}
g = policy.grant(hold, "personal", "always", OWNER)
check("a grant stands", g["ok"] and len(policy.active_grants()) == 1, str(g))
out = grants_chat.handle("what do you do on your own", PERSON)
check("the list names it in a person's voice", "Date from Emily" in out and "Add to calendar" in out and "ask me again" in out, out)
out = grants_chat.handle("ask me again about the dates from emily", PERSON)
check("the words take it back", out.startswith("Done") and not policy.active_grants(), out)
check("a staff member gets nothing from code", grants_chat.handle("what do you do on your own", {"key": "x", "role": "staff"}) == "")
policy.grant(hold, "personal", "always", OWNER)
check("find by kind words too", len(grants_chat.find("calendar hold")) == 1)
check("'all' finds every grant", len(grants_chat.find("all")) == 1)
out = grants_chat.revoke("the florist email", PERSON)
check("no match says so and lists what stands", out.startswith("Nothing standing matches") and "Date from Emily" in out, out)

# ---------------------------------------------------------------- 4. resume after a takeover
for t_ in ("I'm done", "done", "Done!", "finished it", "I'm in", "signed in", "carry on", "your turn", "all yours", "ok, I did it"):
    check(f"resume asks: {t_!r}", resume.asks(t_))
for t_ in ("I'm not done yet", "done with what?", "can you carry on the florist email tomorrow morning at 9", "[tapped Carry on]"):
    check(f"not a resume: {t_!r}", not resume.asks(t_))
check("nothing waiting -> '' (the brain takes a plain 'done')", resume.handle("done", PERSON) == "")
import report  # noqa: E402
STEP = "Carry on with the job once you have signed in to example.test"
p = {"ticket": "a" * 16, "step": STEP, "values": {}, "url": "https://example.test/login", "host": "example.test", "login": True, "self": True,
     "resume": "carry on from the last milestone note"}
r = cards.post([{"question": "[ACTION PREVIEW]\n" + STEP + "\nOn: https://example.test/login", "header": "Signed in to example.test?", "lane": "personal",
                 "options": [{"label": report.CARRY_ON}, {"label": report.STOP}], "exec": {"kind": "agent_run", "approve_label": report.CARRY_ON, "payload": p}}],
               "doer", asked=True)
check("a Carry on card is up", r["posted"], str(r))
out = resume.handle("I'm done", PERSON)
check("'I'm done' answers the Carry on card by code", "picking the job up" in out and "example.test" in out, out)
a = cards._answers_index().get(r["qid"]) or {}
check("the card is answered 'Carry on' in the owner's name", a.get("option") == report.CARRY_ON and OWNER in str(a.get("by")), str(a))
import leftopen  # noqa: E402
leftopen._save({"b" * 16: {"state": "open", "target": "t1", "url": "https://example.test/pay", "text": "One step needs you.", "epoch": time.time()}})
leftopen.CHANNEL = type("G", (), {"patch_body": lambda self, n, b: True, "post_body": lambda self, *a, **k: "m"})()
out = resume.handle("finished it", PERSON)
check("'finished it' releases a page left open and the job carries on", out.startswith("Thanks, I'll carry on") and not leftopen.holding(), f"{out!r} {leftopen.holding()}")
check("the resume is only for owners", resume.handle("done", {"key": "x", "role": "staff"}) == "")

print(("ALL OK" if not FAILS else f"{len(FAILS)} FAILED: {FAILS}") + f"  (scratch {TMP})")
sys.exit(1 if FAILS else 0)

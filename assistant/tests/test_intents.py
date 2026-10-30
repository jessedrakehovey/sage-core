"""Offline tests for the one-turn design (2026-10-21): deeds are tool calls validated in the turn
(intents.py / gmcp.py), a drafted message becomes the card under the reply in the place it was
asked (thread or DM), owner-asked cards supersede instead of being refused, no system bullets
under a reply, the rule ceiling refuses instead of retiring, and the prompt is generic.
Scratch stores; no model, no Google, no Chat.
   python assistant/tests/test_intents.py
"""
import os as _os, tempfile as _tf
_os.environ.setdefault("WV_LOG_DIR", _tf.mkdtemp(prefix="wv_logs_"))
import json
import re
import os
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
TMP = Path(tempfile.mkdtemp(prefix="wv_intents_"))
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
import brain  # noqa: E402
import gmcp  # noqa: E402
import intents  # noqa: E402
import cards  # noqa: E402
import exec_store as es  # noqa: E402
import google_api  # noqa: E402
google_api.HOME = TMP / "google"          # no token on disk: nothing is connected in this test
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
assert owner1 and core.can(owner1, "task"), "test needs the owner from people.json"
actions.WAKE_DOER = False
THREAD = "spaces/AAQAtest/threads/abc123"
DM = "spaces/AAQAdmowner1"


def outbox():
    return [r for r in core.rows(bridge.OUTBOX) if r.get("via") == "local"]


def live_cards():
    return [cards._read(p) for p in sorted(cards.LIVE.glob("q*.json"))]


# ---------------------------------------------------------------- 1. the tools validate and record; the model is answered in words
turn = intents.begin(owner1, "spaces/AAQAtest", THREAD)
os.environ["AGENT_TURN"] = str(turn)
r = gmcp.call("learn", {"rule": "short"})
ok(r.startswith("refused") and "sentence" in r, "learn: a too-short rule is refused with a reason the model can say")
r = gmcp.call("learn", {"rule": "Always answer a yes-or-no in one line", "why": "owner1: one line is plenty"})
ok(r.startswith("accepted") and "no rule numbers" in r, "learn: accepted, with how to phrase it")
r = gmcp.call("task", {"lane": "personal", "text": "Text Justin that the tie comes later",
                       "draft": {"channel": "text", "to": "not a number", "text": "The tie comes later, no stress."}})
ok(r.startswith("refused") and "phone number" in r, "task: a draft to a bad number is refused before any card exists")
r = gmcp.call("task", {"lane": "personal", "text": "Text Justin that the tie comes later",
                       "draft": {"channel": "text", "to": "+18055550123", "text": "Hey Justin, the tie comes later, no stress. [owner1: time?]"}})
ok(r.startswith("refused") and "blank" in r, "task: a draft with a blank is refused")
r = gmcp.call("card_revise", {"text": "shorter"})
ok(r.startswith("refused") and "no open card" in r, "card_revise: nothing to revise in this thread -> refused")
r = gmcp.call("event", {"title": "Dentist", "start": "2026-11-02 09:00", "calendar": "nobody"})
ok(r.startswith("refused") and "not connected" in r, "event: a disconnected calendar is refused, never 'it's on'")
r = gmcp.call("task", {"lane": "personal", "text": "Text Justin that the tie comes later",
                       "draft": {"channel": "text", "to": "+18055550123", "text": "Hey Justin, the tie comes later, no stress at all."}})
ok(r.startswith("accepted") and "right under your reply" in r and "in this thread" in r, "task+draft in a thread: accepted, the card lands under the reply in this thread")
got = intents.collect(turn)
ok(set(got) == {"learn", "task"} and got["learn"][0]["rule"].startswith("Always") and got["task"]["draft"]["to"] == "+18055550123",
   "collect: only the accepted deeds, in the shape actions.apply takes")
for _ in range(5):
    gmcp.call("remember", {"kind": "household", "subject": "cats", "fact": "the cats eat at 7"})
r = gmcp.call("remember", {"kind": "household", "subject": "cats", "fact": "the cats eat at 7 again"})
ok(r.startswith("refused") and "at most" in r, "a per-reply cap refuses the sixth remember")
os.environ.pop("AGENT_TURN", None)
ok(gmcp.call("learn", {"rule": "Always answer a yes-or-no in one line"}).startswith("refused"), "no turn (a sessionless run): the deed tools record nothing")

# ---------------------------------------------------------------- 2. a turn: the brain calls the tool, the card lands in the thread with the reply
turns = {"n": 0}


def fake_run(prompt, resume="", model="", timeout=600):
    turns["n"] += 1
    os.environ["AGENT_TURN"] = brain._TURN["path"]
    ans = gmcp.call("task", {"lane": "personal", "text": "Text Justin that the tie comes later",
                             "draft": {"channel": "text", "to": "+18055550123", "text": "Hey Justin, the tie comes later, no stress at all."}})
    assert ans.startswith("accepted"), ans
    return {"ok": True, "session": "S1", "text": "Done, the text to Justin is right below for your tap."}


brain._run_claude = fake_run
bridge.handle({"channel": "local", "person": owner1, "text": "Text Justin that the tie comes later", "convo": "spaces/AAQAtest",
               "thread": THREAD, "space": "spaces/AAQAtest", "direct": True})
out = outbox()
cs = live_cards()
ok(turns["n"] == 1, "one model turn, no re-ask")
ok(len(cs) == 1 and cs[0].get("chat_thread") == THREAD and cs[0].get("asked") and cs[0].get("urgent") and cs[0].get("delivery") == "now",
   "the card exists, in the asking thread, urgent, never held for the digest")
q = cs[0]["questions"][0]
ok(q["exec"]["kind"] == "imessage_send" and q["exec"]["payload"]["account"] == "owner1-iphone" and q["exec"]["payload"]["to"] == "+18055550123"
   and "[TEXT PREVIEW]" in q["question"] and "no stress at all" in q["question"] and q["options"][0]["label"].startswith("Send"),
   "the card is a real send card from the owner's own phone, the text verbatim on it, Send / Skip")
ok(out[-1]["text"] == "Done, the text to Justin is right below for your tap.", "the reply is the brain's own words: no bullet, no system line")
ok(any(r.get("channel") == "card" and r.get("thread") == THREAD for r in core.rows(core.TRANSCRIPT)),
   "the card is in the agent's transcript with its thread")
ok(any(it["kind"] == "card" for it in actions.LAST_APPLIED), "the deed is recorded as a card hand-off for the commitments ledger")

# ---------------------------------------------------------------- 3. the same ask in a DM: the card goes to the DM, at once
def fake_run_dm(prompt, resume="", model="", timeout=600):
    os.environ["AGENT_TURN"] = brain._TURN["path"]
    ans = gmcp.call("task", {"lane": "personal", "text": "Text Justin that the tie comes later",
                             "draft": {"channel": "text", "to": "+18055550123", "text": "Hey Justin, the tie comes later, no stress at all. See you Saturday."}})
    assert ans.startswith("accepted") and "here in this chat" in ans, ans
    return {"ok": True, "session": "S2", "text": "The text is below."}


brain._run_claude = fake_run_dm
bridge.handle({"channel": "gchat", "person": owner1, "text": "Text Justin that the tie comes later", "convo": "dm:owner1",
               "thread": "", "space": DM, "direct": True, "why": "DM"})
cs = live_cards()
newest = max(cs, key=lambda c: c["ts"])
ok(newest.get("chat_space") == DM and newest.get("urgent") and newest.get("delivery") == "now" and not newest.get("chat_thread"),
   "a DM ask: the card is posted into that DM, urgent, not held for Monday")
olds = [cards._read(p) for p in cards.DONE.glob("q*.json")]
ok(any(c.get("status") == "superseded" and c.get("superseded_by") == newest["id"] for c in olds),
   "an owner-asked card to the same recipient SUPERSEDES the older open one instead of a FRESHNESS refusal")
ok(len([c for c in cs if c.get("status") == "open"]) == 1, "one open card to Justin, not two")

# ---------------------------------------------------------------- 4. a task without a draft still goes to the operator, with the place
def fake_run_task(prompt, resume="", model="", timeout=600):
    os.environ["AGENT_TURN"] = brain._TURN["path"]
    ans = gmcp.call("task", {"lane": "wedding", "text": "Reply to Corwin about the editing proposal; I could not find his email"})
    assert ans.startswith("accepted") and "few minutes" in ans, ans
    return {"ok": True, "session": "S2", "text": "I'm getting that drafted; you'll have the card here in a few minutes."}


brain._run_claude = fake_run_task
bridge.handle({"channel": "gchat", "person": owner1, "text": "reply to Corwin", "convo": "dm:owner1", "thread": "", "space": DM, "direct": True, "why": "DM"})
tix = [t for t in json.loads((TMP / "adata" / "tickets.json").read_text(encoding="utf-8")).values() if t["kind"] == "owner1_request"]
ok(tix and tix[-1]["payload"].get("space") == DM and tix[-1]["payload"].get("asked") is True, "the operator ticket carries the DM and the asked flag")

# ---------------------------------------------------------------- 5. NO_REPLY with a card deed: a plain lead line, no silence
def fake_run_silent(prompt, resume="", model="", timeout=600):
    os.environ["AGENT_TURN"] = brain._TURN["path"]
    gmcp.call("do", {"lane": "home", "text": "Check the water bill balance on the city portal"})
    return {"ok": True, "session": "S2", "text": "NO_REPLY"}


brain._run_claude = fake_run_silent
bridge.handle({"channel": "local", "person": owner1, "text": "check the water bill", "convo": "dm:owner1", "thread": "", "direct": True})
ok("sent that off" in outbox()[-1]["text"] and "\n- " not in outbox()[-1]["text"], "NO_REPLY with a deed: one plain lead line")

# ---------------------------------------------------------------- 6. the rule ceiling refuses; nothing is retired
d = core.load(core.RULES, {"rules": [], "next_id": 1})
before = [r["id"] for r in d.get("rules", []) if r.get("active", True)]
saved = core.MAX_RULES
core.MAX_RULES = len(before) + 1
core.rule_add("Always put the couple's names first in a film title", "test", by="owner1")
r = core.rule_add("Never schedule a call before nine in the morning", "test", by="owner1")
after = [x["id"] for x in core.load(core.RULES, {}).get("rules", []) if x.get("active", True)]
ok(r["status"] == "refused" and "active" in r["reason"] and set(before) <= set(after), "the ceiling refuses the rule that would pass it; no rule is retired")
core.MAX_RULES = saved
LIVE_RULES = ROOT / "agent" / "state" / "rules.json"          # the organization's own state: absent in the bare engine
if LIVE_RULES.is_file():
    live = json.loads(LIVE_RULES.read_text(encoding="utf-8"))
    act = {x["id"] for x in live["rules"] if x.get("active", True)}
    ok({"r4", "r5", "r6", "r12"} <= act, "the four owner rules the old cap retired are active again")
    ok(len([x for x in live["rules"] if x["id"] == "r67"]) == 1, "no duplicate rule id")

# ---------------------------------------------------------------- 7. the prompt is generic + local, and says nothing about SUGGEST
def _org_words(root):
    """This organization's own name and people (org.json, people.json): what a generic file must never carry."""
    out = set()
    try:
        n = str(json.loads((root / "org.json").read_text(encoding="utf-8-sig")).get("name") or "")
        out |= {w for w in [n, n.split(" ")[0]] if len(w) >= 4}
    except (OSError, ValueError):
        pass
    try:
        for k, u in (json.loads((root / "agent" / "people.json").read_text(encoding="utf-8-sig")).get("users") or {}).items():
            out |= {w for w in (k, str(u.get("name") or "").split(" ")[0]) if len(w) >= 3}
    except (OSError, ValueError):
        pass
    return sorted(out, key=len, reverse=True)


gen = (ROOT / "agent" / "templates" / "prompt.generic.md").read_text(encoding="utf-8")
pm = (ROOT / "agent" / "prompt.md").read_text(encoding="utf-8")
ok(not re.search(r"\b(?:" + "|".join(map(re.escape, _org_words(ROOT) + ["the CRM"])) + r")\b", gen), "the generic prompt names no organization, service or person")
ok("SUGGEST {" not in pm and "`task`" in pm and "`draft`" in pm, "prompt.md teaches the deed tools, not a SUGGEST line")
ok("prompt.local.md" in brain.__file__ or "ORGANIZATION NOTES" in brain.context_block(), "the org notes ride into the context")
ok(len(pm) < 21300, f"prompt.md is lean ({len(pm)} chars)")   # 21000 until 2026-10-30 (a reply answers only the newest question, gap g-cc35a4); 20900 until 2026-10-30 (draft.resend flag, a reply to their newer text passes); 20600 until 2026-10-30 (a bill or fee paid is looked up, gap g-c0da13); 20100 until 2026-10-29 (the thread is read from Chat itself, chat_thread tool); 19900 until 2026-10-28 late (hold_if_reply, send_hold.py); 19750 until 2026-10-28 night (fix it and send = approve, gap g-b436c2); 19600 until 2026-10-28 eve (skip a card in words, gap g-bdccf4); 19500 until 2026-10-28 (a code change is never a task; "results?" files the unfiled build); 19000 until 2026-10-27 night (the correction line, correction.py); 18000 until 2026-10-27 eve (the project deed, projects.py); 17800 until 2026-10-27 pm (the no-ids voice sentence, voice.py); 17500 until 2026-10-27 (the REFLEX line, reflex.py); 16600 until 2026-10-25 pm (approve-in-words, the no-guess resolution order over THIS THREAD / cards / JUST BEFORE THIS); 14000 until 2026-10-23 (the "who is texting" paragraph, whois.py); 16000 until 2026-10-25 (rung 5 builds the missing piece and compound asks, capability.py)

# ---------------------------------------------------------------- 8. build now: a chat-filed build request kicks the builder at once (2026-10-23)
import builder  # noqa: E402
import wiki_writer  # noqa: E402
kicks = []
builder.kick = lambda force=False, first="": kicks.append(force) or "BUILDER started"
wiki_writer.write = lambda note, by="", commit=True, ask="": {"status": "applied", "path": f"Wiki/Builder/{note['title']}.md"}
turn = intents.begin(owner1, "dm:owner1", "", space=DM)
os.environ["AGENT_TURN"] = str(turn)
r = gmcp.call("note", {"title": "Build request lead text timing", "body": "## Build request\nSend the intro text within a minute.\n\nEvidence: 40 min today."})
ok(r.startswith("accepted") and "starts right after this reply" in r and "being built now" in r, "a build note is accepted as building NOW, not 'in to be built'")
lines = actions.apply(intents.collect(turn), owner1, {"convo": "dm:owner1", "thread": "", "space": DM})
ok(kicks == [True] and lines == [], "actions.apply kicks the builder with force=True right after the build note saves, no line under the reply")
ok(any(a["kind"] == "build" for a in actions.LAST_APPLIED), "the build is still a tracked promise")
kicks.clear()
actions.apply({"note": {"title": "The cats' vet", "body": "Dr. Lee on Milpas, open until six."}}, owner1, {"convo": "dm:owner1"})
ok(kicks == [], "a plain note never kicks the builder")
builder.kick = lambda force=False: (_ for _ in ()).throw(OSError("no pythonw"))
lines = actions.apply({"note": {"title": "Build request two", "body": "## Build request\nx"}}, owner1, {"convo": "dm:owner1"})
ok(len(lines) == 1 and "didn't start right away" in lines[0] and any(a["kind"] == "build" for a in actions.LAST_APPLIED),
   "a kick that fails is said in one plain line; the request stays filed for the next run")
os.environ.pop("AGENT_TURN", None)

# ---------------------------------------------------------------- 9. decide: a decision card with the recommendation first, never a chat question
turn = intents.begin(owner1, "spaces/AAQAtest", THREAD)
os.environ["AGENT_TURN"] = str(turn)
good = {"header": "Second shooter for the Ashcombe wedding", "question": "Book Maya as second shooter for the Ashcombe wedding. Dan is free too but has not shot a ceremony with us.",
        "options": [{"label": "Book Maya", "description": "she shot the last three with us"}, {"label": "Book Dan", "description": "cheaper, untested"}, {"label": "Skip"}],
        "recommended": "Book Maya", "why": "She knows the ceremony setup and the venue.", "lane": "wedding"}
r = gmcp.call("decide", dict(good, options=good["options"] + [{"label": "Ask both"}]))
ok(r.startswith("refused") and "2 or 3 options" in r, "decide: four options are refused")
r = gmcp.call("decide", dict(good, recommended="Book Sam"))
ok(r.startswith("refused") and "recommended" in r, "decide: a recommendation that is not one of the options is refused")
r = gmcp.call("decide", dict(good, recommended="Skip"))
ok(r.startswith("refused") and "Skip" in r, "decide: recommending the no-action button is refused")
r = gmcp.call("decide", dict(good, options=[{"label": "Book Maya because she knows the venue and the setup"}, {"label": "Book Dan"}], recommended="Book Maya because she knows the venue and the setup"))
ok(r.startswith("refused") and "button" in r, "decide: a sentence as a button is refused")
r = gmcp.call("decide", dict(good, header="Text to Maya about Saturday"))
ok(r.startswith("refused") and "task" in r, "decide: a message dressed as a decision is sent to task+draft")
r = gmcp.call("decide", dict(good, why=""))
ok(r.startswith("refused") and "why" in r, "decide: no why, no card")
r = gmcp.call("decide", good)
ok(r.startswith("accepted") and "in this thread" in r and "'Book Maya'" in r and "would you like" in r, "decide: accepted with the place, the recommendation and the no-question rule")
got = intents.collect(turn)
ok(got["decide"]["recommended"] == "Book Maya" and len(got["decide"]["options"]) == 3 and got["decide"]["lane"] == "wedding", "collect carries the cleaned decide")
os.environ.pop("AGENT_TURN", None)


def fake_run_decide(prompt, resume="", model="", timeout=600):
    os.environ["AGENT_TURN"] = brain._TURN["path"]
    ans = gmcp.call("decide", good)
    assert ans.startswith("accepted"), ans
    return {"ok": True, "session": "S3", "text": "I'd go with Maya; the card is right below."}


brain._run_claude = fake_run_decide
bridge.handle({"channel": "local", "person": owner1, "text": "who should second shoot the Ashcombe wedding", "convo": "spaces/AAQAtest",
               "thread": THREAD, "space": "spaces/AAQAtest", "direct": True})
cs = [c for c in live_cards() if c.get("status") == "open"]
dc = max(cs, key=lambda c: c["ts"])
q = dc["questions"][0]
ok(dc.get("chat_thread") == THREAD and dc.get("urgent") and dc.get("delivery") == "now" and dc.get("asked"),
   "the decision card is in the asking thread, urgent, never held for Monday")
ok(q["options"][0]["label"] == "Book Maya (Recommended)" and [o["label"] for o in q["options"][1:]] == ["Book Dan", "Skip"],
   "the recommended option is first and tagged; the rest keep their order")
ok(not q.get("exec") and "Why 'Book Maya'" in q["details"] and "ceremony setup" in q["details"], "no exec on a decision card (a tap sends nothing); the why is on the card")
ok(outbox()[-1]["text"] == "I'd go with Maya; the card is right below." and "would you like" not in outbox()[-1]["text"].lower(),
   "the reply is the brain's words with the recommendation, no question")
ok(any(it["kind"] == "card" and it["ref"] == dc["id"] for it in actions.LAST_APPLIED), "the decision card is a recorded hand-off")
r = cards.answer(dc["id"], "Book Dan", by="gchat:owner1")
ok(r.get("ok") and cards.answer_of(dc["id"]) == "Book Dan" and not r.get("ticket"), "a tap answers the card and mints no ticket: the answer goes back to the conversation")
ok("`decide`" in pm and "Triage every message first" in pm and "Tried / Blocked because / Need from you" in pm
   and "`decide`" in gen and "starts building at once" in gen, "the prompt teaches triage, decide and build-now (generic and live)")

# ---------------------------------------------------------------- 10. a doer promise with no job becomes the job (2026-10-23)
import claims  # noqa: E402
for s in ("That's a browser edit, so it goes to the doer, not the operator.", "I'll tell you once the doer has done it.",
          "I'm sending that to the doer now.", "The doer will change the 0.95 to 1.0 on the sheet."):
    ok(any(c["claim"] == "do" for c in claims.missing(s, {})), f"claim shape caught: {s[:50]!r}")
ok(not claims.missing("I can't send that to the doer from here.", {}), "a negated doer line is not a claim")
tix_before = len([t for t in json.loads((TMP / "adata" / "tickets.json").read_text(encoding="utf-8")).values() if t["kind"] == "agent_run"])


def fake_run_promise(prompt, resume="", model="", timeout=600):
    os.environ["AGENT_TURN"] = brain._TURN["path"]
    return {"ok": True, "session": "S4", "text": "That's a browser edit, so it goes to the doer. I'll tell you once it's changed."}


brain._run_claude = fake_run_promise
bridge.handle({"channel": "local", "person": owner1, "text": "change the 0.95 season multipliers on the live price sheet to 1.0", "convo": "dm:owner1",
               "thread": "", "direct": True})
tix = [t for t in json.loads((TMP / "adata" / "tickets.json").read_text(encoding="utf-8")).values() if t["kind"] == "agent_run"]
ok(len(tix) == tix_before + 1 and "0.95 season multipliers" in tix[-1]["title"] + json.dumps(tix[-1].get("payload") or {}),
   "the brain promised the doer and sent nothing: the owner's words become the doer job")
ok(any(it["kind"] == "do" for it in actions.LAST_APPLIED), "and it is a tracked hand-off (the doer reports back here)")

# ---------------------------------------------------------------- 11. a revision asked for in a thread posts as owner-asked
captured = {}
_real_post = cards.post


def spy_post(questions, owner, expires_s=cards.DEFAULT_EXPIRES_S, lane="", thread="", replaces="", space="", asked=False):
    captured.update(thread=thread, replaces=replaces, space=space, asked=asked)
    return _real_post(questions, owner, expires_s, lane, thread, replaces, space=space, asked=asked)


cards.post = spy_post
open_send = next(c for c in live_cards() if c.get("status") == "open" and (c["questions"][0].get("exec") or {}).get("kind") == "imessage_send")
lines = actions.revise_card(open_send["id"], "Hey Justin, no stress at all, the tie comes later.", owner1, {"thread": THREAD, "space": "spaces/AAQAtest"})
cards.post = _real_post
ok(lines == [] and captured.get("asked") is True and captured.get("replaces") == open_send["id"] and captured.get("thread") == THREAD,
   "card_revise posts with asked=True in the thread, superseding the old card instead of tripping the recipient limit")

# scheduled sends and jobs (2026-10-24)
from datetime import datetime, timedelta
tmrw = (datetime.now() + timedelta(days=1)).strftime("%Y-%m-%d")
turn = intents.begin(owner1, "spaces/AAQAtest", THREAD)
os.environ["AGENT_TURN"] = str(turn)
r = gmcp.call("task", {"lane": "personal", "text": "Text Justin tonight at 11 that the tie comes later",
                       "draft": {"channel": "text", "to": "+18055550199", "text": "Hey Justin, the tie comes later, no stress.", "send_at": f"{tmrw} 23:00"}})
ok(r.startswith("refused") and "quiet hours" in r, "task: a send_at inside quiet hours is refused with the window")
r = gmcp.call("task", {"lane": "personal", "text": "Text Justin tomorrow at 9 that the tie comes later",
                       "draft": {"channel": "text", "to": "+18055550199", "text": "Hey Justin, the tie comes later, no stress.", "send_at": f"{tmrw} 09:00"}})
ok(r.startswith("accepted") and "goes out at" in r, "task: a daytime send_at is accepted and the model is told when it goes out")
r = gmcp.call("job", {"job": "nothing like this"})
ok(r.startswith("refused") and "JOBS YOU CAN RUN" in r, "job: an unknown job is refused")
r = gmcp.call("job", {"job": "handoff build", "args": {"couple": "Test & Couple"}, "at": f"{tmrw} 21:00"})
ok(r.startswith("accepted") and "Editor handoff build" in r and "9:00 PM" in r, "job: a registered job by alias with a time is accepted")
got = intents.collect(turn)
ok(got.get("job") and got["job"][0]["job"] == "handoff_build" and got["job"][0]["at"] == f"{tmrw} 21:00", "job intent folds with key, args and time")
lines = actions.apply(got, owner1, {"convo": "dm:owner1", "thread": THREAD, "space": "spaces/AAQAtest"})
live = live_cards()
jc = [c for c in live if c.get("status") == "open" and (c["questions"][0].get("exec") or {}).get("kind") == "local"]
tc = [c for c in live if c.get("status") == "open" and (c["questions"][0].get("exec") or {}).get("send_at") == f"{tmrw} 09:00"]
ok(jc and jc[0]["questions"][0]["exec"].get("send_at") == f"{tmrw} 21:00" and "Test & Couple" in jc[0]["questions"][0]["question"],
   "the job card is posted with the time and the values on it")
ok(tc and "Sends at" in tc[0]["questions"][0]["question"], "the scheduled text card says when it sends")

# ---------------------------------------------------------------- 12. approve: an owner's words in a card's thread are the tap (2026-10-24)
turn = intents.begin(owner1, "spaces/AAQAtest", THREAD)
os.environ["AGENT_TURN"] = str(turn)
r = gmcp.call("task", {"lane": "wedding", "text": "Text Marlo to ask if he is free Nov 7",
                       "draft": {"channel": "text", "to": "+13525550101", "text": "Hey Marlo, it's owner1. Are you free to shoot second on Saturday Nov 7?"}})
ok(r.startswith("accepted"), "a fresh text draft is accepted")
got = intents.collect(turn)
actions.apply(got, owner1, {"convo": "dm:owner1", "thread": THREAD, "space": "spaces/AAQAtest"})
mcard = next(c for c in live_cards() if c.get("status") == "open" and (c["questions"][0].get("exec") or {}).get("payload", {}).get("to") == "+13525550101")
turn2 = intents.begin(owner1, "spaces/AAQAtest", THREAD, card=mcard)
os.environ["AGENT_TURN"] = str(turn2)
r = gmcp.call("approve", {"at": "25:99", "words": "send it at 8am"})
ok(r.startswith("refused"), "approve: a bad time is refused")
r = gmcp.call("approve", {"at": f"{tmrw} 23:30", "words": "send it tonight"})
ok(r.startswith("refused") and "quiet hours" in r, "approve: a quiet-hours time is refused")
r = gmcp.call("approve", {"at": f"{tmrw} 08:00", "words": "Send it at 8am"})
ok(r.startswith("accepted") and "8:00 AM" in r and "tap" in r, "approve: the card of this thread is approved with a time; the model is told when it goes")
got = intents.collect(turn2)
ok(got.get("approve") and got["approve"][0]["qid"] == mcard["id"] and got["approve"][0]["at"] == f"{tmrw} 08:00", "approve intent folds with the card id and the time")
lines = actions.apply(got, owner1, {"convo": "dm:owner1", "thread": THREAD, "space": "spaces/AAQAtest"})
after = json.loads((cards.LIVE / f"{mcard['id']}.json").read_text(encoding="utf-8"))
tk = next((t for t in es.fold().values() if t.get("approval_ref") == f"card:{mcard['id']}"), None)
ok(lines == [] and after.get("status") == "answered" and tk and tk.get("not_before") == f"{tmrw} 08:00" and tk.get("approved_by") == "owner1", "the card is answered on the owner's words and the ticket waits for 8:00" + " " + repr((lines, after.get("status"), tk and tk.get("not_before"))))
r = gmcp.call("approve", {"words": "send it"})
ok(r.startswith("refused") and "no longer open" in r, "approve: an answered card cannot be approved twice")
# a card the system posted on its own (not asked) still needs the tap
sysq = json.loads(json.dumps(mcard["questions"][0])); sysq.pop("freshness_ack", None); sysq["header"] = "System card"
sysq["exec"]["payload"]["to"] = "+13525550102"; sysq["question"] = sysq["question"].replace("+13525550101", "+13525550102")
rs = cards.post([sysq], owner="operator", lane="wedding")
turn3 = intents.begin(owner1, "spaces/AAQAtest", THREAD, card={"id": rs.get("qid")})
os.environ["AGENT_TURN"] = str(turn3)
r = gmcp.call("approve", {"words": "send it"})
ok(not rs.get("posted") or (r.startswith("refused") and "tap" in r), "approve: a system-posted card is refused; it needs the tap" + " " + repr((rs, r)))

# ---------------------------------------------------------------- 13. a second text to the same number today is refused unless it is a resend (Marlo x2, 2026-10-24)
es.set_status(tk["id"], "done", "PHONE_QUEUED m-test")
turn4 = intents.begin(owner1, "spaces/AAQAtest", THREAD)
os.environ["AGENT_TURN"] = str(turn4)
r = gmcp.call("task", {"lane": "wedding", "text": "Text Marlo to ask if he is free",
                       "draft": {"channel": "text", "to": "(352) 555-0101", "text": "Hey Marlo, are you free Nov 7?"}})
ok(r.startswith("refused") and "already went" in r and "Hey Marlo" in r, "a second text to the same number today is refused with the earlier text quoted" + " " + repr(r))
r = gmcp.call("task", {"lane": "wedding", "text": "Resend: text Marlo again, he did not answer",
                       "draft": {"channel": "text", "to": "+13525550101", "text": "Hey Marlo, just checking you saw my text about Nov 7."}})
ok(r.startswith("accepted"), "the owner's 'resend' / 'again' lifts the guard" + " " + repr(r))
# 2026-10-29 (a team member's new question): a reply to a text they sent after ours needs no marker word, and a wanted second
# text carries `resend` as a flag, never as a word in the body or the card's preview
import phone_bridge as _pb
import time
_prev = intents.recent_text_to("+13525550101")
_th = json.loads(_pb.THREADS.read_text(encoding="utf-8")) if _pb.THREADS.exists() else {}
_th.setdefault("+13525550101", {"name": "Marlo", "messages": []})["messages"].append(
    {"ts": "2000-01-01T00:00:00", "dir": "in", "text": "older message, before our text", "ref": ""})
_pb.THREADS.write_text(json.dumps(_th), encoding="utf-8")
os.environ["AGENT_TURN"] = str(intents.begin(owner1, "spaces/AAQAtest", THREAD))
_args = {"lane": "wedding", "text": "Reply to the text Marlo sent about the call time",
         "draft": {"channel": "text", "to": "+13525550101", "text": "Hey Marlo, the call time is 2 pm."}}
r = gmcp.call("task", _args)
ok(r.startswith("refused") and "already went" in r, "a message they sent BEFORE our text does not lift the guard" + " " + repr(r))
_after = time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(time.mktime(time.strptime(_prev["ts_done"][:19], "%Y-%m-%dT%H:%M:%S")) + 5))
_th["+13525550101"]["messages"].append({"ts": _after, "dir": "in", "text": "What time is the call?", "ref": ""})
_pb.THREADS.write_text(json.dumps(_th), encoding="utf-8")
os.environ["AGENT_TURN"] = str(intents.begin(owner1, "spaces/AAQAtest", THREAD))
r = gmcp.call("task", _args)
ok(r.startswith("accepted"), "a reply to a text they sent after our last text today posts its card with no marker word" + " " + repr(r))
_th["+13525550101"]["messages"] = [m for m in _th["+13525550101"]["messages"] if m["ts"] != _after]
_pb.THREADS.write_text(json.dumps(_th), encoding="utf-8")
os.environ["AGENT_TURN"] = str(intents.begin(owner1, "spaces/AAQAtest", THREAD))
r = gmcp.call("task", {"lane": "wedding", "text": "Text Marlo the parking details",
                       "draft": {"channel": "text", "to": "+13525550101", "text": "Hey Marlo, parking is behind the barn.", "resend": True}})
ok(r.startswith("accepted"), "the separate resend flag lifts the guard without a word in the body" + " " + repr(r))
_q = actions._draft_question("wedding", "Text Marlo the parking details",
                             {"channel": "text", "to": "+13525550101", "text": "Hey Marlo, parking is behind the barn.", "resend": True}, owner1)
ok(_q["exec"]["payload"]["body"] == "Hey Marlo, parking is behind the barn." and "resend" not in _q["exec"]["payload"]["body"].lower(),
   "the flag never reaches the message body" + " " + repr(_q["exec"]["payload"]["body"]))
r = gmcp.call("task", {"lane": "wedding", "text": "Text Marlo the parking details",
                       "draft": {"channel": "text", "to": "+13525550101", "text": "resend Hey Marlo, parking is behind the barn."}})
ok(r.startswith("refused") and "already went" in r, "a marker word in the body no longer lifts the guard" + " " + repr(r))

# ---------------------------------------------------------------- 14. no local in validate shadows a module function (2026-10-24)
# a half-made edit bound `lanes = ...` inside validate, so every `lanes()` there raised UnboundLocalError: two text
# drafts to Corwin were refused at 15:35 (lane "wedding" and no lane alike) and no card could be made for minutes.
import inspect as _inspect
_fns = {n for n, v in vars(intents).items() if _inspect.isfunction(v) and v.__module__ == intents.__name__}
_shadow = [n for n in intents.validate.__code__.co_varnames if n in _fns]
ok(not _shadow, "validate binds no local named after a module function (lanes, _s, ...)" + " " + repr(_shadow))
for _lane in ("wedding", None):
    os.environ["AGENT_TURN"] = str(intents.begin(owner1, "spaces/AAQAtest", THREAD))
    _args = {"text": "Reply to Corwin about the second shooter day",
             "draft": {"channel": "text", "to": "+18055550177", "text": "Hey Corwin, thanks for getting back to me."}}
    if _lane:
        _args["lane"] = _lane
    r = gmcp.call("task", _args)
    ok(r.startswith("accepted") and "Error" not in r, f"task+draft with lane {_lane!r} is accepted, never a Python error" + " " + repr(r))

# ---------------------------------------------------------------- 15. a card that would be refused is refused by the tool (2026-10-22)
# 12:49: the owner asked for the revised proposal card to Corwin; the task tool said "a email card ... is posted in this
# thread", then cards.post refused it DRAFT_LINT (a "$" price in a wedding-lane email) and no card ever came.
os.environ["AGENT_TURN"] = str(intents.begin(owner1, "spaces/AAQAtest", THREAD))
_before = intents.count(os.environ["AGENT_TURN"])
r = gmcp.call("task", {"text": "Revised proposal to Corwin Castellan IV", "lane": "wedding",
                       "draft": {"channel": "email", "to": "lead.couple@example.com", "subject": "Revised proposal",
                                 "text": "Hi Corwin, the revised proposal is $1,800 for the full day. Let me know what you think."}})
ok(r.startswith("refused") and "no card was posted" in r and "DRAFT_LINT" in r and "posted in this thread" not in r,
   "task+draft the draft lint would block: refused with the reason, never 'posted below'" + " " + repr(r))
ok(intents.count(os.environ["AGENT_TURN"]) == _before, "the refused draft is not recorded, so apply posts nothing and says nothing false")
r = gmcp.call("task", {"text": "Revised proposal to Corwin Castellan IV", "lane": "wedding",
                       "draft": {"channel": "email", "to": "lead.couple@example.com", "subject": "Revised proposal",
                                 "text": "Hi Corwin, the revised proposal is attached. Let me know what you think."}})
ok(r.startswith("accepted"), "the same card without the price is accepted" + " " + repr(r))

# ---------------------------------------------------------------- 16. a doer card for a step of the owner's OWN ask is theirs to approve in words (2026-10-30)
# 13:56 "Make the freaking link already" in the DM, on the copy step of his own "text a dropbox link" ask: "I can't
# approve that one for you. The copy card was posted by the system". The ticket says who asked and where.
import tickets  # noqa: E402
tickets.add_many([{"source": "agent", "source_id": "do|computer|1|loop", "lane": "wedding", "kind": "agent_run",
                   "title": "owner1: the loop video to Dropbox", "summary": "copy the loop, make the link",
                   "payload": {"text": "copy the loop", "via": "sage", "from": "owner1", "where": "computer", "convo": "dm:owner1", "thread": "", "space": "spaces/AAQAtest"}},
                  {"source": "agent", "source_id": "do|computer|2|other", "lane": "personal", "kind": "agent_run",
                   "title": "owner6: her own job", "summary": "x",
                   "payload": {"text": "x", "via": "sage", "from": "owner6", "where": "computer", "convo": "dm:owner6", "thread": "", "space": ""}}], "test")
_tj = tickets.make_id("agent", "do|computer|1|loop")
_to = tickets.make_id("agent", "do|computer|2|other")
_cpq = {"question": "[ACTION PREVIEW]\nCopy the loop video into the Dropbox folder\nRuns exactly: cp a b\nTap Approve and the doer performs exactly this step.",
        "header": "Doer: owner1: the loop video to Dropbox", "lane": "wedding", "audience": "owner",
        "options": [{"label": "Approve"}, {"label": "Cancel"}],
        "exec": {"kind": "agent_run", "approve_label": "Approve", "payload": {"ticket": _tj, "step": "Copy the loop", "resume": "make the link", "command": "cp a b"}}}
_rj = cards.post([_cpq], owner="doer", lane="wedding")
_oq = json.loads(json.dumps(_cpq)); _oq["header"] = "Doer: owner6: her own job"; _oq["exec"]["payload"]["ticket"] = _to
_ro = cards.post([_oq], owner="doer", lane="personal")
ok(_rj.get("posted") and _ro.get("posted"), "two system-posted doer checkpoint cards are up (one from owner1's ask, one from owner6's)")
_cj = json.loads((cards.LIVE / f"{_rj['qid']}.json").read_text(encoding="utf-8"))
ok(not _cj.get("asked") and intents.own_ask_card(_cj, owner1, "dm:owner1") and not intents.own_ask_card(_cj, owner1, "dm:other"),
   "own_ask_card: the ticket's asker and conversation make the card the owner's own")
_co = json.loads((cards.LIVE / f"{_ro['qid']}.json").read_text(encoding="utf-8"))
ok(not intents.own_ask_card(_co, owner1), "own_ask_card: another person's ask is not his")
# words at the top of the DM, no card thread: the one open card of his own asks here is the one he means
os.environ["AGENT_TURN"] = str(intents.begin(owner1, "dm:owner1", ""))
r = gmcp.call("approve", {"words": "Make the freaking link already"})
ok(r.startswith("accepted") and "tap" in r, "approve: his words in the DM approve the doer card of his own ask" + " " + repr(r))
got = intents.collect(os.environ["AGENT_TURN"])
ok(got.get("approve") and got["approve"][0]["qid"] == _rj["qid"], "the approve intent carries that card's id")
# in the other card's thread, owner's card is still a tap for the owner
os.environ["AGENT_TURN"] = str(intents.begin(owner1, "dm:owner6", "", card={"id": _ro["qid"]}))
r = gmcp.call("approve", {"words": "go ahead"})
ok(r.startswith("refused") and "tap" in r, "approve: a doer card from someone else's ask still needs the tap" + " " + repr(r))

print(f"\nall {N} checks passed")

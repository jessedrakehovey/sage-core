"""Offline tests for correction.py (the owner 2026-10-27: "when I respond 'no that's not what I asked' or 'where is the
approval card', Sage needs to fix whatever it was; it cannot just say sorry and never change"). A correction is the
original ask again: the brain gets it with a CORRECTION block, an apology-only reply is re-run once, the deed the ask
implied is queued by code when the turn still has none, and a correction that still ends in words is a reflex finding
that files one build request. Scratch stores; no model, no Google.
   python assistant/tests/test_correction.py
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
TMP = Path(tempfile.mkdtemp(prefix="wv_corr_"))
os.environ["ASSISTANT_DATA"] = str(TMP / "adata")
os.environ["ASSISTANT_CARDS"] = str(TMP / "cards")
os.environ["ASSISTANT_LEARNINGS"] = str(TMP / "learnings")
os.environ["RULINGS_FILE"] = str(ROOT / "data" / "rulings.json")
os.environ["DOER_DATA"] = str(TMP / "doer")
os.environ["AGENT_STATE"] = str(TMP / "agent_state")
os.environ["AGENT_DATA"] = str(TMP / "agent_data")
os.environ["AGENT_SUGGESTIONS"] = str(TMP / "sugg.md")
os.environ["CHAT_PUSH"] = "off"
os.environ["CRM_SNAPSHOT_FILE"] = str(TMP / "no_snapshot.json")
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT / "agent"))
import core  # noqa: E402
import actions  # noqa: E402
import brain  # noqa: E402
import bridge  # noqa: E402
import correction  # noqa: E402
import reflex  # noqa: E402

N = 0


def ok(cond, msg):
    global N
    N += 1
    if not cond:
        print(f"FAIL {N}: {msg}")
        sys.exit(1)
    print(f"ok {N}: {msg}")


owner1 = core.person_for(handle="owner1")
assert owner1, "test needs the owner from people.json"
NAME = core.profile().get("name", "Agent")

# ---------------------------------------------------------------- 1. what counts as a correction (the real lines, Oct 21-27)
for line, want in (
    ("You told me the card is below, but you never sent one", "card"),
    ("Why are the cards not posting?", "card"),
    ("It was never sent", "card"),
    ("Where is the approval card?", "card"),
    ("where's the card", "card"),
    ("Why is it not built?", "build"),
    ("It's still not working we need to build this capability", "build"),
    ("Try again", "redo"),
    ("Okay try again", "redo"),
    ("Sorry do it again", "redo"),
    ("Again", "redo"),
    ("No that's not what I asked", "redo"),
    ("Ticket refused. Fix it so that doesn't happen again", "fix"),
    ("No, Diane texted us and Paired us with them", ""),
    ("No, he's not getting angles and behind the scenes. He's just not the front man", ""),
    ("Again, I keep saying this, you can deduce who someone is logically by reading my text message log", ""),
    ("Make card", ""),
    ("Please reply to them asap", ""),
    ("Where's Victor from?", ""),
    ("[tapped \"Send (Recommended)\" on the card \"Reply to Ambrose\"]", ""),
    ("How much are credits or membership?", ""),
):
    got = correction.kind(line)
    ok(got == want, f"kind({line[:50]!r}) = {got!r} (want {want!r})")

# ---------------------------------------------------------------- 2. the original ask: this thread first, then the conversation
convo, th = "spaces/HOME", "spaces/HOME/threads/corwin"
core.say(convo, "owner1", "Provide the revised card for Corwin with all five prices", channel="gchat", meta={"thread": th})
core.say(convo, NAME, "Done, the card is below.", channel="gchat", meta={"thread": th})
core.say(convo, "owner1", "What's the weather for Saturday's shoot?", channel="gchat", meta={"thread": ""})
core.say(convo, NAME, "Clear, high of 71.", channel="gchat", meta={"thread": ""})
core.say(convo, "owner1", "[tapped \"Send (Recommended)\" on the card \"Text to Corwin\"]", channel="gchat", meta={"thread": th})
a = correction.original_ask(convo, th, owner1)
ok(a.get("text", "").startswith("Provide the revised card"), "in a thread the last real ask of that thread wins (a tap is not an ask)")
a2 = correction.original_ask(convo, "", owner1)
ok(a2.get("text", "").startswith("What's the weather"), "top level: the last real ask in the conversation")
ok(correction.expected_deed("Provide the revised card for Corwin with all five prices") == "" and
   correction.expected_deed("Text Corwin that the $750 flat covers 8 hours") == "task" and
   correction.expected_deed("Upload the 2022 taxes folder into Google Drive") == "do" and
   correction.expected_deed("What's the weather for Saturday?") == "", "expected deed: a message -> task, work somewhere -> do, a question -> none")
ok(correction.apology_only("You're right, and I'm sorry. My side showed the card as posted, but I can't see why.", {}), "an apology with nothing put forward")
ok(not correction.apology_only("Which ticket do you mean? There are two.", {}), "one question back is not an apology")
ok(not correction.apology_only("Sorry, here it is again; the card is below.", {"task": {"text": "x"}}), "sorry WITH a deed is fine")
ok("CORRECTION" in correction.note("card", "where is the card", a, "task") and "Provide the revised card" in correction.note("card", "x", a, "task")
   and "three words at most" in correction.note("card", "x", a, ""), "the note carries the ask and the rule")

# ---------------------------------------------------------------- 3. the bridge: an apology-only reply is re-run once, then the deed is forced
calls = []
replies = []


def fake(prompt, resume="", model="", timeout=600):
    calls.append(prompt)
    text = replies.pop(0) if replies else "Got it."
    return {"ok": True, "session": "S1", "text": text}


brain._run_claude = fake
dm = "dm:owner1"
msg = {"channel": "local", "person": owner1, "text": "Text Corwin that the $750 flat covers the first 8 hours", "convo": dm, "thread": "", "direct": True}
replies[:] = ["Done, the card is below.\nSUGGEST {\"task\": {\"text\": \"Text Corwin: the $750 flat covers the first 8 hours\", \"lane\": \"wedding\"}}"]
bridge.handle(dict(msg))
ok(len(calls) == 1 and any(a.get("kind") == "task" for a in actions.LAST_APPLIED), "the ask puts a task forward")
n_before = len(calls)
replies[:] = ["You're right, and I'm sorry. My side showed the card as posted, but it never reached the chat. I can't see why.",
              "Sorry again, I really can't tell what happened to it."]
bridge.handle({**msg, "text": "You told me the card is below, but you never sent one"})
ok(len(calls) == n_before + 2, f"an apology with nothing done is re-run once: {len(calls) - n_before} turns")
ok("CORRECTION" in calls[-2] and "Text Corwin that the $750 flat" in calls[-2], "the first turn carried the CORRECTION block with the original ask")
ok("re-run" in calls[-1] and "was not sent" in calls[-1], "the re-run names the apology as what not to do")
ok(any(a.get("kind") == "task" for a in actions.LAST_APPLIED), "still nothing put forward: the task is queued by code from the original ask")
out = [r for r in core.rows(bridge.OUTBOX) if r.get("via") == "local"]
ok("card" in out[-1]["text"].lower() and "sorry" in out[-1]["text"].lower(), f"the owner hears the deed, not only the apology: {out[-1]['text'][:120]}")
rows = correction.recent(1)
ok(rows and rows[-1]["kind"] == "card" and rows[-1]["expected"] == "task" and rows[-1]["forced"] == "task" and rows[-1]["rerun"] and not rows[-1]["words_only"],
   f"the ledger row: {rows[-1]['kind']} expected={rows[-1]['expected']} forced={rows[-1]['forced']} rerun={rows[-1]['rerun']}")
ok(len(core.rows(bridge.OUTBOX)) == len(out) + 0 and sum(1 for r in out if "sorry" in r["text"].lower()) == 1, "one reply went out for the correction, not two")

# the brain does it on the first go: no re-run, nothing forced
n_before = len(calls)
replies[:] = ["Made it again, the card is below.\nSUGGEST {\"task\": {\"text\": \"Again, for Corwin: the $750 flat covers the first 8 hours\", \"lane\": \"wedding\"}}"]
bridge.handle({**msg, "text": "Try again"})
rows = correction.recent(1)
ok(len(calls) == n_before + 1 and rows[-1]["kind"] == "redo" and rows[-1]["forced"] == "" and not rows[-1]["rerun"] and "task" in rows[-1]["applied"],
   "a correction the brain answers with the deed costs one turn and forces nothing")

# a job for the doer: "try again" after an apology forces the do
msg2 = {**msg, "text": "Upload the 2022 taxes folder into Google Drive", "convo": "dm:owner1", "thread": ""}
replies[:] = ["Sent, it will report back here."]
bridge.handle(dict(msg2))
replies[:] = ["Sorry, that one didn't start; I can't see why from here.", "I'm sorry, I still can't tell."]
bridge.handle({**msg2, "text": "Try again"})
rows = correction.recent(1)
ok(rows[-1]["expected"] == "do" and rows[-1]["forced"] == "do" and any(a.get("kind") == "do" for a in actions.LAST_APPLIED),
   f"work somewhere with no `do` after the correction: the doer job is queued by code ({rows[-1]['forced']})")

# a factual correction is not a correction of a deed: no CORRECTION block, nothing forced, no ledger row
n_before, n_rows = len(calls), len(correction.recent(1))
replies[:] = ["Got it, Diane paired you."]
bridge.handle({**msg, "text": "No, Diane texted us and paired us with them"})
ok(len(calls) == n_before + 1 and "CORRECTION" not in calls[-1] and len(correction.recent(1)) == n_rows, "a fact correction is a normal turn")

# ---------------------------------------------------------------- 3b. "where is the card" in a thread the card already landed in
# 2026-10-18 16:04-16:08, the Hollis text: the card posted in the ask's thread two minutes after the reply, the owner tapped Send
# the same minute he asked "Where is the card", and the reply said it "closed unsent" and made a second one.
import cards  # noqa: E402
TH = "spaces/S/threads/JER1"
core.say(dm, "owner1", "Respond to Hollis's text with something that would be very difficult for a human to type", channel="gchat", meta={"thread": TH})
r = cards.post([{"question": "Hollis texted: \"6 : 4 5\".\n[TEXT PREVIEW] To +18055550142:\ngot it, see you at 6:45 (upside down)", "header": "Text reply: Hollis",
                 "lane": "personal", "options": [{"label": "Send (Recommended)"}, {"label": "Skip"}],
                 "exec": {"kind": "imessage_send", "approve_label": "Send (Recommended)",
                          "payload": {"account": "owner1-iphone", "to": "+18055550142", "body": "got it, see you at 6:45"}}}],
               owner="operator", lane="personal", thread=TH)
qid = r.get("qid")
ok(r.get("posted") and qid, f"fixture: the reply card posted in the ask's thread ({str(r)[:80]})")
c = cards._read(cards.LIVE / f"{qid}.json"); c["chat"] = {"home": "spaces/S/messages/JER1.X1"}; cards._write(cards.LIVE / f"{qid}.json", c)
ok(correction.card_in_thread(TH).get("id") == qid and not correction.card_in_thread("spaces/S/threads/OTHER"), "the card is found from the thread it landed in")

where = {**msg, "convo": dm, "thread": TH, "text": "Where is the card. They should send at the same time as your reply"}
n_before = len(calls)
replies[:] = ["Sorry, it's right here in this thread, waiting for your Send tap.", "Sorry again."]
bridge.handle(dict(where))
ok(len(calls) == n_before + 1, f"the card is there: one turn, no apology re-run ({len(calls) - n_before})")
ok("already in this thread" in calls[-1] and "Do NOT make another card" in calls[-1] and "make it again now" not in calls[-1],
   "the brain is told the card is here and not to make another")
ok(not any(a.get("kind") in ("task", "card") for a in actions.LAST_APPLIED), "nothing forced: no second card for the same message")
row = correction.recent(1)[-1]
ok(row["kind"] == "card" and row.get("card_here") == qid and not row["words_only"] and not row["forced"],
   "the ledger row says the card was here, so the reflex files no build for it")

# he tapped Send the same minute: the card is answered, and the thread's context says so (never "closed unsent")
ar = cards.answer(qid, "Send (Recommended)", by="gchat:owner1")
ok(ar.get("ok"), f"fixture: the tap ({str(ar)[:80]})")
replies[:] = ["You tapped Send at that minute, so it went to the phone."]
bridge.handle(dict(where))
ok("tapped 'Send' at" in calls[-1] and "already in this thread" in calls[-1], "the correction note carries the tap, not a missing card")
ctx = bridge.card_context(cards._read(cards.LIVE / f"{qid}.json") or {**c, "status": "answered"})
ok("Where it stands: tapped 'Send'" in ctx and "does not change it" in ctx and "never make it again" in ctx,
   f"a closed card's thread context tells how it closed: {ctx[:160]!r}")
ok(not any(a.get("kind") in ("task", "card") for a in actions.LAST_APPLIED), "still no second card after the tap")
c2 = {**c, "id": "q" + "e" * 12, "status": "expired"}
cards._write(cards.DONE / f"{c2['id']}.json", c2)
cards._write(cards.LIVE / f"{qid}.json", {**c, "status": "expired"})
ok(not correction.card_in_thread(TH), "a card that expired untapped is not 'here': the usual correction makes it again")

# ---------------------------------------------------------------- 4. the reflex: words alone after everything -> one build request
correction.record(owner1, "redo", "That's not what I asked", {"text": "Decide whether he emailed us and draft the email instead", "ts": core.stamp()}, "",
                  [], "", True, "I'm sorry, I misread that.", convo=dm)
f = reflex.d_corrections()
ok(f and f[-1]["kind"] == "correction_unfixed" and "words alone" in f[-1]["what"], "a correction that ended in words alone is a finding")
filed = []
reflex._file_build = lambda f, title, body: filed.append((title, body)) or "Wiki/Personal/Build request x.md"
r = reflex.tick(only=("correction",))
ok(r["steps"] >= 1 and filed and "A correction still ended in words" in filed[-1][0] and "Decide whether he emailed" in filed[-1][1] and "I'm sorry, I misread" in filed[-1][1],
   "ONE build request carries the ask, the correction and the reply")
correction.record(owner1, "redo", "Again", {"text": "another ask", "ts": core.stamp()}, "", [], "", True, "Sorry.", convo=dm)
n = len(filed)
reflex.tick(only=("correction",))
ok(len(filed) == n, "a second words-only correction of the same kind the same day files nothing more")

# ---------------------------------------------------------------- 4b. a correction whose turn filed its capability build is not words alone
# 2026-10-29 09:44-09:49, the real pair: "Give me link to where they are saved" in the thread of the 09:00 back-taxes nudge,
# a guess back, then "No you should know what I'm talking about based on the thread ... fix that context awareness".
# The reply said it could not see the top of the thread; the capability loop filed that build the same turn (built and
# replayed by 11:27), but the ledger row is written first, so the reflex kept a finding for a gap already handed on.
import builder  # noqa: E402
import capability  # noqa: E402
import chat_thread  # noqa: E402
import wiki_writer  # noqa: E402
NOTES = []
wiki_writer.write = lambda note, by="", commit=True: NOTES.append(note) or {"status": "applied", "path": "Wiki/Personal/Build request x.md"}
builder.kick = lambda force=False, first="": "BUILDER started"
builder.collect = lambda state=None: []
SP, TID = "spaces/HOMEC", "5r1S5_RsTws"
TH2 = f"{SP}/threads/{TID}"
NUDGE = "Back taxes nudge: Morning. Have the back taxes gone in yet? Tell me when they're sent and I'll stop asking."
ASK = "Give me link to where they are saved"
FIX = "No you should know what I'm talking about based on the thread I am replying to a message in\nfix that context awareness"
CHAT = [{"name": f"{SP}/messages/{TID}.{TID}", "text": NUDGE, "sender": {"type": "BOT", "displayName": NAME},
         "createTime": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() - 3600)), "thread": {"name": TH2}}]
chat_thread._list = lambda space, filt, order="createTime asc", n=40: list(CHAT)
chat_thread.available = lambda: True
m3 = {"channel": "local", "person": owner1, "convo": SP, "space": SP, "thread": TH2, "direct": True}
replies[:] = ["Do you mean the rental cart screenshots, the follow-up in the booking system, or the lead files?"]
bridge.handle({**m3, "text": ASK, "msg_name": f"{SP}/messages/{TID}.ASK1"})
n_before = len(calls)
replies[:] = ["I can't see the message at the top of a thread, only the cards under it. Those are saved on Mission Control. "
              "I can't confirm the exact page from here."]
bridge.handle({**m3, "text": FIX, "msg_name": f"{SP}/messages/{TID}.FIX1"})
ok(len(calls) == n_before + 1 and "CORRECTION" in calls[-1] and ASK in calls[-1] and "back taxes" in calls[-1],
   "the correction turn carries the original ask and the thread's first message (the back-taxes nudge)")
row = correction.recent(1)[-1]
gid = correction.handed_on(row)
ok(row["kind"] == "fix" and row["ask"] == ASK and gid and capability._load()["gaps"][gid]["thread"] == TH2,
   f"the turn that said it could not filed its capability build, and the ledger row finds it ({gid})")
ok(not any(f["data"]["thread"] == TH2 for f in reflex.d_corrections()),
   "a correction handed on to a build is no reflex finding: correction:2026-10-29 09:49 clears")
correction.record(owner1, "fix", FIX, {"text": "Book the venue walkthrough", "ts": core.stamp()}, "", [], "", False, "Sorry.",
                  convo="dm:other", thread="")
ok(any(f["data"]["convo"] == "dm:other" for f in reflex.d_corrections()), "words alone with no gap behind them is still a finding")

# ---------------------------------------------------------------- 5. the prompt and the template carry the rule
for p in (ROOT / "agent" / "prompt.md", ROOT / "agent" / "templates" / "prompt.generic.md"):
    P = p.read_text(encoding="utf-8")
    ok("is the ask again" in P and "three words at most" in P and "CORRECTION" in P, f"{p.name} carries the correction rule")
src = (ROOT / "agent" / "correction.py").read_text(encoding="utf-8").lower()
_org = json.loads((ROOT / "org.json").read_text(encoding="utf-8-sig")).get("name") or ""
_ppl = list((json.loads((ROOT / "agent" / "people.json").read_text(encoding="utf-8-sig")).get("users") or {}).values())[1:]
_words = {w.lower() for w in [_org.split(" ")[0]] + [str(u.get("name") or "").split(" ")[0] for u in _ppl] if len(w) >= 4}
for word in sorted(_words | {"max5x"}):
    ok(word not in src, f"correction.py carries no organization word: {word}")

print(f"ALL {N} OK")

"""Offline tests for the follow-through fixes of 2026-11-04, each one a failure an owner had to chase that day:

  1. an approved text was reported "Texted ... at 4:00 PM" when it was only queued; the phone took it 54 minutes
     later (six wake-up texts never arrived) and nobody said it was waiting
  2. "one still comes" was promised for a card that was then refused, and the promise closed itself
  3. "let me sign in" got "Go ahead" twice with nothing open, and "nothing for you to do at a computer" was stuck
     on messages that asked the owner to sign in
  4. four complaints typed on a phone (curly apostrophes) and one ask repeated three times were not corrections
  5. a job's report reached chat garbled, with the machinery's words in it

Scratch stores; no model, no Google, no phone.
   python assistant/tests/test_followthrough.py
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
TMP = Path(tempfile.mkdtemp(prefix="wv_follow_"))
os.environ["ASSISTANT_DATA"] = str(TMP / "adata")
os.environ["ASSISTANT_CARDS"] = str(TMP / "cards")
os.environ["ASSISTANT_LEARNINGS"] = str(TMP / "learnings")
os.environ["DOER_DATA"] = str(TMP / "doer")
os.environ["AGENT_STATE"] = str(TMP / "agent_state")
os.environ["AGENT_DATA"] = str(TMP / "agent_data")
os.environ["AGENT_SUGGESTIONS"] = str(TMP / "sugg.md")
os.environ["CHAT_PUSH"] = "off"
os.environ["AGENT_DESK"] = "off"
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT / "agent"))
import actions  # noqa: E402
import chatfirst  # noqa: E402
import commitments  # noqa: E402
import correction  # noqa: E402
import drain  # noqa: E402
import outcomes  # noqa: E402
import phone_bridge as pb  # noqa: E402
import phone_nudge  # noqa: E402
import routines  # noqa: E402
import signin  # noqa: E402
import voice  # noqa: E402

N = 0


def ok(cond, msg):
    global N
    N += 1
    if not cond:
        print(f"FAIL {N}: {msg}")
        sys.exit(1)
    print(f"ok {N}: {msg}")


NUM = "+15550100123"
FMT = "%Y-%m-%dT%H:%M:%S"

# ------------------------------------------------------------------ 1. queued is not sent
line = drain._result_line({"kind": "imessage_send", "payload": {"to": NUM}}, "PHONE_QUEUED m-abc -> ...0123")
ok("queued for the phone" in line and "handed" not in line, f"the executor's line for a text says queued, never handed: {line}")
said = voice.reword(line)
ok("Texted" not in said and "lined up" in said, f"...and reads as lined up, not texted: {said}")
ok(voice.classify(line) == "send_result", "the queued line is a send result like the others")
said = voice.reword(f"Text to {NUM} handed to the phone at 4:54 PM; the phone's Shortcut sends it.")
ok("Texted" not in said and "lined up on your phone" in said and "4:54 PM" in said,
   f"picked up is not sent either (2026-11-06): 'lined up', never 'Texted', until something confirms it: {said}")
said = voice.gate(f"Text to {NUM} is still waiting for the phone after 3 min; send it now from the phone: https://mc.example.com/phone/run")
ok("hasn't gone yet" in said and "https://mc.example.com/phone/run" in said, f"a waiting text is said with its send-now link: {said}")

pb._quiet = lambda: False
SAID = []
pb._say = lambda item, line, fallback=False: SAID.append((item.get("id"), line, fallback)) or True
good, msg = pb.queue_outbound({"to": NUM, "body": "Are you free Saturday?", "account": pb.DEFAULT_ACCOUNT}, "tk-aaaaaaaaaaaa")
ok(good and "PHONE_QUEUED" in msg, "a text is queued")
ok(pb.state_words("tk-aaaaaaaaaaaa").startswith("STILL QUEUED"), "its state says still queued, not sent")
ok(pb.tell_waiting(now=time.time() + 60) == 0, "a text queued a minute ago is not yet called waiting")
ok(pb.tell_waiting(now=time.time() + 4 * 60) == 1 and SAID and "still waiting for the phone" in SAID[-1][1] and SAID[-1][2] is True,
   f"after the waiting window the owner is told, with the plain-line fallback for a text that had no card: {SAID[-1:]}")
ok(pb.tell_waiting(now=time.time() + 9 * 60) == 0, "...once per text, never again")
line = actions._already_approved_line("ALREADY_ANSWERED: this draft was approved at 2026-11-04T16:00:21 (q0123456789ab, ticket tk-aaaaaaaaaaaa done). It is already sent or on its way.")
ok("already approved" in line and "has not gone yet" in line and "q0123456789ab" not in line and "tk-" not in line,
   f"'it never sent' on a queued text: the truth, no second card, no ids: {line}")
got = pb.fetch_outbox(pb.DEFAULT_ACCOUNT)
ok(len(got) == 1 and "handed to the phone at" in SAID[-1][1] and SAID[-1][2] is False, f"when the phone fetches it, the handed line is said (card thread only): {SAID[-1:]}")
ok(pb.state_words("tk-aaaaaaaaaaaa").startswith("picked up by the phone at"), "its state says picked up, with the time")
line = actions._already_approved_line("ALREADY_ANSWERED: this draft was approved at 2026-11-04T16:00:21 (q0123456789ab, ticket tk-aaaaaaaaaaaa done).")
ok("took it at" in line and "second card" in line, f"once picked up, the reply says when the phone took it: {line}")
box = json.loads(pb.OUTBOX.read_text(encoding="utf-8"))
box["items"][0]["status"] = "expired"
pb.OUTBOX.write_text(json.dumps(box), encoding="utf-8")
ok(actions._already_approved_line("ALREADY_ANSWERED: this draft was approved at x (ticket tk-aaaaaaaaaaaa done).") == "",
   "a text that never left is not 'already approved': a fresh card is right")
ok(actions._already_approved_line("DRAFT_LINT: the body names a price") == "", "another refusal is not this case")
plain = actions._plain_reason("DRAFT_LINT: the body names a price (q0123456789ab) at 2026-11-04T16:00:21")
ok("q0123456789ab" not in plain and "2026" not in plain and "DRAFT_LINT" not in plain and "price" in plain, f"a refusal reason carries no code, id or stamp: {plain}")

# the wake-up path: every nudge of a spell went out and the phone never fetched -> one miss; a fetch right after a nudge clears it
day = time.mktime(time.strptime("2026-11-04T16:20:00", FMT))
pb.OUTBOX.write_text(json.dumps({"items": [{"id": "m-1", "ticket": "tk-bbbbbbbbbbbb", "to": NUM, "body": "x", "status": "ready",
                                            "account": pb.DEFAULT_ACCOUNT, "ts": "2026-11-04T16:00:19", "ts_handed": "", "ts_sent": ""}]}), encoding="utf-8")
phone_nudge.STATE.parent.mkdir(parents=True, exist_ok=True)
phone_nudge.STATE.write_text(json.dumps({"ts": day - 300, "renudges": phone_nudge.RENUDGE_MAX, "renudge_ts": day - 300}), encoding="utf-8")
ok(phone_nudge.health()["missed"] == 0, "no miss before the spell is judged")
phone_nudge.renudge_if_waiting(now=day)
ok(phone_nudge.health()["missed"] == 1, "every nudge sent and still no fetch: the wake-up path has missed once")
phone_nudge.renudge_if_waiting(now=day + 60)
ok(phone_nudge.health()["missed"] == 1, "...counted once per spell")
phone_nudge.woke(now=day + 3600)
ok(phone_nudge.health()["missed"] == 1, "a fetch long after the last nudge (run by hand) does not prove the wake-up works")
st = json.loads(phone_nudge.STATE.read_text(encoding="utf-8"))
st["ts"] = day + 7000
phone_nudge.STATE.write_text(json.dumps(st), encoding="utf-8")
phone_nudge.woke(now=day + 7030)
ok(phone_nudge.health()["missed"] == 0, "a fetch right after a nudge does: the miss count clears")

# the phone's fetch is held through a text's undo window (the phone asks when its owner leaves the chat app, seconds after the tap)
import exec_store as es  # noqa: E402
SLEPT = []
_stamp = lambda ago: time.strftime(FMT, time.localtime(time.time() - ago))  # noqa: E731
_tk = lambda ago, **kw: {"kind": "imessage_send", "status": "queued", "ts": _stamp(ago), "approval_ref": "card:q0123456789ab",  # noqa: E731
                         "payload": {"to": NUM, "body": "x", "account": pb.DEFAULT_ACCOUNT}, **kw}
_real_pending = es.pending
es.pending = lambda lane="": [_tk(10)]
w = pb.wait_for_release(pb.DEFAULT_ACCOUNT, sleep=SLEPT.append)
ok(24 <= w <= 27 and SLEPT and abs(SLEPT[-1] - w) < 0.01, f"a text approved 10 s ago holds the fetch until its undo window is out: {w:.0f}s")
es.pending = lambda lane="": [_tk(120)]
ok(pb.wait_for_release(pb.DEFAULT_ACCOUNT, sleep=SLEPT.append) == 0, "a text already past its undo window holds nothing")
es.pending = lambda lane="": [_tk(10, payload={"to": NUM, "body": "x", "account": "another-phone"})]
ok(pb.wait_for_release(pb.DEFAULT_ACCOUNT, sleep=SLEPT.append) == 0, "another phone's text holds nothing")
es.pending = lambda lane="": [_tk(10, not_before="2099-01-01 08:00")]
ok(pb.wait_for_release(pb.DEFAULT_ACCOUNT, sleep=SLEPT.append) == 0, "a text approved for later holds nothing")
es.pending = lambda lane="": [_tk(2)]
ok(pb.wait_for_release(pb.DEFAULT_ACCOUNT, max_s=5, sleep=SLEPT.append) == 5, "the hold never passes its cap")
ok(pb.wait_for_release(pb.DEFAULT_ACCOUNT, max_s=0, sleep=SLEPT.append) == 0, "and 0 switches it off")
es.pending = lambda lane="": []
ok(pb.wait_for_release(pb.DEFAULT_ACCOUNT, sleep=SLEPT.append) == 0, "an empty queue is answered at once")
es.pending = _real_pending

# ------------------------------------------------------------------ 2. a promise is not its own answer
promise = ("I can't confirm it was sent. My record only shows the text was handed to your phone's send queue. "
           "The card didn't post on the first try, so I've handed the draft on; it comes here in a few minutes.")
ok(commitments._ANSWERED.search(promise) and commitments._PROMISED.search(promise), "a reply that promises the card is a promise, however it starts")
ok(commitments._ANSWERED.search("Need from you: the street address.") and not commitments._PROMISED.search("Need from you: the street address."),
   "a reply that only says what it needs is still an answer")
ok(routines._routine_card({"id": "s1", "by": "nobody", "to": "nobody"}, {"task": [{"text": "draft it"}]}) == [], "a routine nobody known set up puts up no card")
ok(routines._routine_card({"id": "s1", "by": "nobody", "to": "nobody"}, {"learn": {"rule": "x"}}) == [], "a routine's SUGGEST without a task does nothing")

# ------------------------------------------------------------------ 3. "let me sign in" and the PC line
for t in ("Let me sign in the browser with Google", "Let me do it", "I’ll sign in myself", "ok let me log in"):
    ok(signin.asks(t), f"the owner takes the sign-in: {t!r}")
for t in ("let me know when it's done", "I logged in", "Let me see the draft", "Can you sign in to the site", "[tapped \"Enter login\"]"):
    ok(not signin.asks(t), f"not that ask: {t!r}")
ok(signin.owner_signs_in("Let me sign in", {"key": "avery", "name": "Avery", "role": "owner"}) == "", "with no sign-in card up, the message goes to the model as before")
own = "You're right, nothing was open for you. I've sent a job that puts the sign-in page up in front on the PC screen. It won't type or click anything."
ok(chatfirst.violations(own) == [] and chatfirst.clean(own) == own, "the agent saying where its own browser is, is not a step for the owner")
ok(chatfirst.violations("I need you to sign in on the PC screen.") == ["pc_screen"], "...but 'I need you to' at the screen still is")
ok(chatfirst.clean("Open a terminal and type the command.") == chatfirst.HANDLED, "a step at the computer is still dropped and handled")
left = chatfirst.clean("The browser is on the PC screen if you're away from it. Sign in with the account that owns the site, then tell me here.")
ok(chatfirst.HANDLED not in left and left.startswith("Sign in"), f"'nothing for you to do' is never stuck on a line that still asks for the sign-in: {left}")

# ------------------------------------------------------------------ 4. complaints and repeats are corrections
for t in ("Seems like the set up button didn’t work", "Avery was never texted",
          "You haven’t given me anything to sign into yet I’m telling you to let me do it", "Let’s finish the project I told you to do"):
    ok(correction.kind(t) == "redo", f"a complaint typed on a phone is a correction: {t!r}")
for t in ("Ask Avery if he can fly the drone this weekend", "I logged in", "Actually, a wide shot is fine if he doesn’t get close"):
    ok(correction.kind(t) == "", f"an ordinary line is not: {t!r}")
person = {"name": "Avery", "key": "avery"}
agent = __import__("core").profile().get("name", "Agent")
now = time.mktime(time.strptime("2026-11-04 13:04", "%Y-%m-%d %H:%M"))
rows = [{"ts": "2026-11-04 11:20", "convo": "c1", "who": "Avery", "text": "Set up the screenshots drive", "thread": "t1"},
        {"ts": "2026-11-04 11:20", "convo": "c1", "who": agent, "text": "Tap Set up the screenshots drive below and I do it myself.", "thread": "t1"},
        {"ts": "2026-11-04 11:49", "convo": "c1", "who": "Avery", "text": "Set up screenshots drive button", "thread": "t2"},
        {"ts": "2026-11-04 11:49", "convo": "c1", "who": agent, "text": "Tap Set up the screenshots drive below and I do it myself.", "thread": "t2"}]
rep = correction.repeat_of("c1", "t2", person, "Set up screenshots drive button", rows=rows, now=now)
ok(rep.get("n") == 2 and rep.get("reply", "").startswith("Tap Set up"), f"the same ask a third time is seen, across threads, with what was answered: {rep}")
ok(correction.same_answer(rep["reply"], "Tap Set up the screenshots drive below and I do it myself; the card says how it went."), "and the same answer again is recognised")
ok(correction.repeat_of("c1", "t2", person, "What is on the calendar today?", rows=rows, now=now) == {}, "a different line is not a repeat")
ok(correction.repeat_of("c1", "t2", person, "Set up screenshots drive button", rows=rows, now=now + 3 * 3600) == {}, "three hours on it is a new ask")
ok(correction.repeat_of("c2", "", person, "Set up screenshots drive button", rows=rows, now=now) == {}, "another conversation is not a repeat")
ok("SAME thing again" in correction.note("repeat", "Set up screenshots drive button", rep, ""), "the model is told the last answer did not work")

# ------------------------------------------------------------------ 5. a job's report in a person's words
raw = ("One step needs you on \"Run the drive setup again and read back the result. On the PC: run `<venv python> x.py setup` and report…\": "
       "The setup run was refused by the hook (\"no card can unlock this: never: compile(\"), so I did not run it. "
       "`configured: false` correctly maps to not_configured. That part is blocked and needs the PC.")
said = voice.gate(raw)
ok(said.startswith("I couldn't finish \"Run the drive setup again\"") and "safety rule" in said, f"a block that is not the owner's is said as the agent's own: {said}")
ok(not any(w in said for w in ("hook", "compile(", "not_configured", "`", "computer", "One step needs you")), f"...with none of the machinery's words: {said}")
step = voice.gate("One step needs you on \"Renew the plates: the registration…\": the site wants the code it texted you. Reply with it here.")
ok(step.startswith("One step needs you on \"Renew the plates\"") and "Reply with it here" in step, f"a real step for the owner still reads as one: {step}")
ok(voice.human("It failed. `x_y` maps to z. Traceback follows.") == "It failed.", "sentences that are only code are dropped from a reason")
ok(outcomes._job_reported({"ref": "0000000000000000", "created": "2026-11-04T16:39:07"}) is False, "a job with no line of its own in the transcript has not reported")

print(f"ALL {N} OK")

"""Offline tests for held.py: a question asked while the model's account is at its usage limit
is kept, the owner is told the reset time once, nothing else spends a turn until then, and the
bridge answers the kept question in its own place once the limit resets. Scratch stores; no
model, no Google.
   python assistant/tests/test_held.py
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
TMP = Path(tempfile.mkdtemp(prefix="wv_held_"))
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
import brain  # noqa: E402
import bridge  # noqa: E402
import held  # noqa: E402

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
REAL_RUN = brain._run_claude
LIMIT = "You've hit your session limit · resets 7:50pm (America/Los_Angeles)"

# ---------------------------------------------------------------- parsing
now = time.mktime((2026, 10, 16, 18, 58, 0, 0, 0, -1))
r = held.limit_reset(LIMIT, now)
ok(r and time.localtime(r)[3:5] == (19, 50) and time.localtime(r)[2] == 16, "'resets 7:50pm' at 18:58 is today's 19:50")
r = held.limit_reset("You've hit your usage limit · resets 12:20am", now)
ok(r and time.localtime(r)[2:5] == (17, 0, 20), "'resets 12:20am' in the evening is tomorrow 00:20")
r = held.limit_reset("rate limit reached", now)
ok(r and abs(r - (now + held.UNKNOWN_WAIT_S)) < 1, "a limit with no reset time waits the default half hour")
ok(held.limit_reset("exit 0xC000013A", now) is None, "a crash is not a limit")
ok(held.limit_reset("", now) is None and held.limit_reset("timeout", now) is None, "empty / timeout are not limits")
ok(held.fmt(time.mktime((2026, 10, 16, 19, 50, 0, 0, 0, -1))).startswith("7:50 pm"), "fmt says 7:50 pm")

# ---------------------------------------------------------------- the bridge holds, tells once, then answers
calls = []


def limited(prompt, resume="", model="", timeout=600):
    calls.append(prompt)
    return {"ok": False, "error": LIMIT, "account": "account max5x"}


brain._run_claude = limited
reset_at = held.limit_reset(LIMIT)
msg = {"channel": "local", "person": owner1, "text": "help me think of a kickback percentage", "convo": "dm:owner1", "thread": ""}
bridge.handle(dict(msg))
out = [r for r in core.rows(bridge.OUTBOX) if r.get("via") == "local"]
ok(len(out) == 1 and "kept your question" in out[0]["text"] and held.fmt(reset_at) in out[0]["text"],
   "limit hit: the reply names the reset time and says the question is kept")
ok("Give me a minute" not in out[0]["text"], "the old 'give me a minute' line is gone")
rows = held.pending()
ok(len(rows) == 1 and rows[0]["text"] == msg["text"] and rows[0]["msg"]["convo"] == "dm:owner1" and rows[0]["attempts"] == 1,
   "the question is in held.json with its place")
ok(held.blocked_until() == reset_at, "the block lasts until the reset")
n_calls = len(calls)
bridge.handle({**msg, "text": "Try again"})
out = [r for r in core.rows(bridge.OUTBOX) if r.get("via") == "local"]
ok(len(calls) == n_calls, "a second message while the limit is in force spends no model turn")
ok(len(out) == 2 and out[1]["text"].startswith("Still out of steam") and "got this one too" in out[1]["text"],
   "...and gets the short 'still out of steam' line")
ok(len(held.pending()) == 2, "both questions wait")
# an ambient line in the space is held silently
bridge.handle({"channel": "local", "person": owner1, "text": "owner6 I'll grab milk", "convo": "spaces/S1", "thread": "t1", "direct": False, "why": "ambient"})
out = [r for r in core.rows(bridge.OUTBOX) if r.get("via") == "local"]
ok(len(out) == 2 and len(held.pending()) == 3, "an ambient line is kept without a line in the space")
ok(bridge.held_retry() == 0, "nothing is re-run before the reset")

# the limit resets: the kept questions are answered where they were asked
answers = []


def answering(prompt, resume="", model="", timeout=600):
    calls.append(prompt)
    answers.append(prompt)
    return {"ok": True, "session": "S1", "text": "NO_REPLY" if "milk" in prompt else "A 10% referral fee is the usual range."}


brain._run_claude = answering
d = core.load(held.STORE, {})
for r in d["rows"]:
    r["until"] = time.time() - held.GRACE_S - 1
core.save(held.STORE, d)
n = bridge.held_retry()
out = [r for r in core.rows(bridge.OUTBOX) if r.get("via") == "local"]
ok(n == 3 and len(answers) == 3, "after the reset every kept question gets its model turn")
ok(len(out) == 4 and "referral fee" in out[2]["text"] and "referral fee" in out[3]["text"], "the answers land in the asker's conversation")
ok(not held.pending() and held.blocked_until() == 0, "the queue is empty and the block is lifted")
tr = [r for r in core.rows(core.TRANSCRIPT) if r.get("text") == msg["text"]]
ok(len(tr) == 1, "the owner's line is in the transcript once, not again on the retry")

# a limit on the retry re-holds with the new reset time; three limits in a row and it gives up honestly
brain._run_claude = limited
bridge.handle({**msg, "text": "what about 15 percent"})
for attempt in (2, 3):
    d = core.load(held.STORE, {})
    for r in d["rows"]:
        r["until"] = time.time() - held.GRACE_S - 1
    core.save(held.STORE, d)
    bridge.held_retry()
    rows = held.pending()
    ok(len(rows) == 1 and rows[0]["attempts"] == attempt, f"limit on the retry: re-held as attempt {attempt}")
d = core.load(held.STORE, {})
for r in d["rows"]:
    r["until"] = time.time() - held.GRACE_S - 1
core.save(held.STORE, d)
bridge.held_retry()
out = [r for r in core.rows(bridge.OUTBOX) if r.get("via") == "local"]
ok(not held.pending() and "couldn't get to this one" in out[-1]["text"], "a fourth limit: one honest line, the row closes")

# a non-limit failure still gets the plain retry line and holds nothing
brain._run_claude = lambda prompt, resume="", model="", timeout=600: {"ok": False, "error": "exit 0xC000013A"}
bridge.handle({**msg, "text": "are we free sunday"})
out = [r for r in core.rows(bridge.OUTBOX) if r.get("via") == "local"]
ok("didn't go through on my side" in out[-1]["text"] and not held.pending(), "a crash is not held: the plain line, nothing kept")

# ---------------------------------------------------------------- the fallback account (off by default)
runs = []


def on_account(prompt, resume="", model="", timeout=600, acct=None, tools=None):   # tools: the brain passes its tool set through (2026-10-25)
    runs.append(acct)
    if acct is None:
        return {"ok": False, "error": LIMIT, "account": "account max5x"}
    return {"ok": True, "text": "fine", "session": "S2", "account": f"account {acct}"}


brain._run_claude_on = on_account
brain._run_claude = REAL_RUN                          # the real wrapper, saved before the tests above replaced it
real_profile = core.profile
core.profile = lambda: {**real_profile(), "claude_account_fallback": ""}   # the live profile may name one (max20x, 2026-10-26)
r = brain._run_claude("hi")
ok(not r.get("ok") and runs == [None], "no fallback named: the limit is returned as is")
core.profile = lambda: {**real_profile(), "claude_account_fallback": "max20x"}
try:
    runs.clear()
    import account
    core.save(account.LIMITS, {})          # the limit just seen is remembered (reflex, 2026-10-27); this is a fresh scenario
    r = brain._run_claude("hi")
    ok(r.get("ok") and runs == [None, "max20x"], "fallback named: the same turn runs once more on it")
    runs.clear()
    r = brain._run_claude("hi")
    ok(r.get("ok") and runs == ["max20x"], "the limit is remembered: the next turn goes straight to the account with room")
    core.save(account.LIMITS, {})
finally:
    core.profile = real_profile

# ---------------------------------------------------------------- a limit from before the fallback was named (2026-10-26)
# max5x was held until 7:50 pm with no fallback; the owner then named max20x, and "Try again" still got
# "out of steam" without a turn. A limit recorded under another fallback setting gates nothing.
real_profile = core.profile
later = time.time() + 3600
try:
    core.profile = lambda: {**real_profile(), "claude_account_fallback": ""}
    for r in held.pending():
        held.take(r["id"])
    held.hold({"channel": "local", "convo": "dm:owner1", "person": owner1}, "follow up with the couple", later)
    ok(held.blocked_until() == later and not held.due(), "no fallback: the limit gates and the question waits")
    core.profile = lambda: {**real_profile(), "claude_account_fallback": "max20x"}
    ok(held.blocked_until() == 0, "fallback named since: the old limit no longer gates new messages")
    ok([r["text"] for r in held.due()] == ["follow up with the couple"], "...and the kept question is due at once")
    held.take(held.pending()[0]["id"])
    held.hold({"channel": "local", "convo": "dm:owner1", "person": owner1}, "both out", later)
    ok(held.blocked_until() == later and not held.due(), "a limit seen with the fallback named (both accounts out) still gates")
finally:
    core.profile = real_profile

print(f"\nALL {N} CHECKS PASSED")

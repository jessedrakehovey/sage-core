"""Offline tests for the burst buffer (2026-10-21, the way example and example answer): a run of Google
Chat lines from one person becomes ONE brain turn and ONE reply once they go quiet; the typing
placeholder goes up at once and is edited into the answer; the buffer survives a bridge restart;
slash commands and the local channel never wait. Scratch stores, a fake clock, a fake Chat client
that records instead of posting, a fake brain. No model, no Google.
   python assistant/tests/test_burst.py
"""
import os as _os, tempfile as _tf
_os.environ.setdefault("WV_LOG_DIR", _tf.mkdtemp(prefix="wv_logs_"))
import importlib
import json
import os
import sys
import tempfile
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
TMP = Path(tempfile.mkdtemp(prefix="wv_burst_"))
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
import space_reader  # noqa: E402

N = 0


def ok(cond, msg):
    global N
    N += 1
    if not cond:
        print(f"FAIL {N}: {msg}")
        sys.exit(1)
    print(f"ok {N}: {msg}")


owner1 = core.person_for(handle="owner1")
assert owner1 and owner1.get("emails"), "test needs owner1 (with an email) in people.json"
EMAIL = owner1["emails"][0]
NAME = core.profile().get("name", "Agent")
SPACE = "spaces/DM1"
CLOCK = {"t": time.time()}
SEQ = {"n": 0}


class FakeChat:
    """Stands in for gchat: pull() hands over queued lines; post/patch/delete record."""
    def __init__(self):
        self.queue, self.posts, self.patches, self.deletes = [], [], [], []

    def configured(self):
        return True

    def take_inbound(self):
        return []

    def pull(self, max_messages=20, with_clicks=False):
        out, self.queue = self.queue, []
        return (out, []) if with_clicks else out

    def post(self, space, text, thread=""):
        SEQ["n"] += 1
        name = f"{space}/messages/p{SEQ['n']}.p{SEQ['n']}"
        self.posts.append({"name": name, "space": space, "text": text, "thread": thread})
        return name

    def patch(self, name, text):
        self.patches.append({"name": name, "text": text})
        return True

    def delete(self, name):
        self.deletes.append(name)
        return True

    def get(self, name):
        return {}


CHAT = FakeChat()
CALLS = []


def fake_brain(prompt, resume="", model="", timeout=600):
    CALLS.append(prompt)
    return {"ok": True, "session": "S1", "text": "Got it, one answer."}


def wire():
    """Point the (re)loaded bridge at the fakes; never the real channels, doer or model."""
    bridge._gchat = lambda: CHAT
    bridge._now = lambda: CLOCK["t"]
    bridge.LOG = TMP / "bridge.log"
    space_reader.poll = lambda: []
    space_reader.TOKEN = TMP / "no_reader_token.json"
    brain._run_claude = fake_brain
    actions.WAKE_DOER = False


wire()
sys.path.insert(0, str(ROOT / "doer"))
import doer as _doer  # noqa: E402
_doer.wake = lambda: (_ for _ in ()).throw(RuntimeError("test tried to wake the real doer"))


def line(text, created_ago=0.0, dm=True, thread=""):
    """Queue one Chat line from the owner. Its createTime is `created_ago` seconds before the REAL
    clock: the crossed check compares it with transcript stamps, which core.say takes from real time."""
    SEQ["n"] += 1
    created = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() - created_ago))
    CHAT.queue.append({"email": EMAIL, "chat_user": "users/1", "name": owner1["name"], "space": SPACE, "dm": dm,
                       "thread": thread, "text": text, "msg_name": f"{SPACE}/messages/m{SEQ['n']}.m{SEQ['n']}",
                       "attachments": [], "created": created})


def tick(seconds=0.0):
    CLOCK["t"] += seconds
    return bridge.step()


def replies():
    return [r for r in core.rows(bridge.OUTBOX) if r.get("via") == "gchat"]


cfg = bridge.burst_cfg()
ok(cfg["settle_s"] == 20 and cfg["max_wait_s"] == 120 and cfg["pace_factor"] == 1.5, "profile.json carries the burst block (20 / 120 / 1.5)")

# ---------------------------------------------------------------- two lines 5 s apart -> one answer
line("can you check Saturday for me")
ok(tick() == 0 and len(CALLS) == 0, "a fresh line is buffered, not answered in the same poll")
ok(len(CHAT.posts) == 1 and "typing" in CHAT.posts[0]["text"], "the typing placeholder went up the moment it was buffered")
line("and whether the sitter is free that night")
ok(tick(5) == 0 and len(CHAT.posts) == 1, "a second line 5 s later joins the group; still one placeholder")
ok(tick(10) == 0, "15 s after the last line: still settling")
n = tick(10)
ok(n == 1 and len(CALLS) == 1, "25 s after the last line: ONE handle, ONE brain turn")
ok("can you check Saturday for me\nand whether the sitter is free that night" in CALLS[-1], "both lines reached the brain as one ask")
ok(len(replies()) == 1 and len(CHAT.patches) == 1 and CHAT.patches[-1]["name"] == CHAT.posts[0]["name"]
   and CHAT.patches[-1]["text"] == "Got it, one answer.", "one reply, edited into the early placeholder")
ok(not CHAT.deletes and len(CHAT.posts) == 1, "no second placeholder, nothing deleted")
ok(not bridge._burst_load()["groups"], "the group left the buffer")

# ---------------------------------------------------------------- a lone line waits settle_s, no more
n_calls, n_posts = len(CALLS), len(CHAT.posts)
line("what is on tomorrow?")
tick(0)
ok(tick(19) == 0 and len(CALLS) == n_calls, "a lone line is not answered before settle_s")
ok(tick(1) == 1 and len(CALLS) == n_calls + 1 and len(CHAT.posts) == n_posts + 1, "...and is answered once settle_s has passed")

# ---------------------------------------------------------------- pace: a line every 40 s
n_calls = len(CALLS)
line("first slow line")
tick(0)
tick(40)                                                    # 40 s of silence: the first line goes alone, the pace is now known
ok(len(CALLS) == n_calls + 1, "a slow typist's first line is answered after settle_s (nothing to pace it on yet)")
line("second slow line")
tick(0)
ok(bridge._burst_load()["groups"] and abs(list(bridge._burst_load()["groups"].values())[0]["gap"] - 40) < 1,
   "the gap to their previous line (40 s) is remembered as the typing pace")
ok(tick(39) == 0 and len(CALLS) == n_calls + 1, "with pace 40 s x 1.5 the next line waits 60 s, not 20")
line("third slow line")
tick(1)
ok(len(CALLS) == n_calls + 1, "the third line, 40 s later, joined the second")
line("fourth slow line")
tick(40)
ok(len(CALLS) == n_calls + 1, "so did the fourth")
tick(40)                                                    # 120 s since the group's first line: max_wait_s
ok(len(CALLS) == n_calls + 2 and "second slow line\nthird slow line\nfourth slow line" in CALLS[-1],
   "lines 2-4 at 40 s apart were answered as ONE group (max_wait_s ends it)")

# ---------------------------------------------------------------- a slash command never waits
n_calls, n_rep = len(CALLS), len(replies())
line("/help")
ok(tick(0) == 1 and len(CALLS) == n_calls and len(replies()) == n_rep + 1 and "/rules" in replies()[-1]["text"],
   "a slash command is answered in the same poll, no model turn")

# ---------------------------------------------------------------- the local channel never waits
core.append(bridge.INBOX, {"from": "owner1", "text": "terminal line, answer now"})
ok(tick(0) == 1 and len(CALLS) == n_calls + 1, "a local-channel line is handled at once")
ok(not bridge._bufferable({"channel": "gchat", "person": owner1, "text": "x", "_held": "h1"}), "a held re-run bypasses the buffer")

# ---------------------------------------------------------------- the buffer survives a restart
n_calls = len(CALLS)
CLOCK["t"] += 70                                            # a quiet minute: no typing pace carries over
line("will you remember this across a restart?")
tick(0)
saved = json.loads(bridge.BURST.read_text(encoding="utf-8"))
ok(len(saved["groups"]) == 1 and list(saved["groups"].values())[0]["ack"] == CHAT.posts[-1]["name"],
   "the pending line and its placeholder name are on disk")
bridge = importlib.reload(bridge)                           # the bridge restarts itself on a code change
wire()
ok(not bridge._PLACEHOLDERS and bridge._burst_load()["groups"], "a fresh process starts with the buffer read back from disk")
early_ack = list(saved["groups"].values())[0]["ack"]
ok(tick(25) == 1 and len(CALLS) == n_calls + 1 and CHAT.patches[-1]["name"] == early_ack,
   "after the restart the line is still answered, into the placeholder posted before it")

# ---------------------------------------------------------------- an ack alone: no placeholder, no turn
n_calls, n_posts = len(CALLS), len(CHAT.posts)
CLOCK["t"] += 70
line("ok thanks!")
tick(0)
ok(len(CHAT.posts) == n_posts, "a pure ack gets no placeholder")
tick(25)
ok(len(CALLS) == n_calls and len(CHAT.posts) == n_posts and not bridge._burst_load()["groups"], "...and no model turn: consumed silently")

# ---------------------------------------------------------------- crossed messages
n_calls = len(CALLS)
CLOCK["t"] += 70                                                          # quiet: no pace carries over
core.say("dm:owner1", NAME, "Saturday is clear.", channel="gchat")          # the agent's latest reply, stamped now
line("oh and one more thing", created_ago=200)                            # typed before that reply went up
line("is the drone charged", created_ago=190)
tick(0)
tick(25)
ok(len(CALLS) == n_calls + 1 and bridge.CROSSED_NOTE in CALLS[-1], "lines created before the agent's last reply carry the crossed note")
n_calls = len(CALLS)
CLOCK["t"] += 70
line("fresh line after the reply", created_ago=0)
tick(0)
tick(25)
ok(len(CALLS) == n_calls + 1 and bridge.CROSSED_NOTE not in CALLS[-1], "a line typed after the reply does not")
ok(not bridge._crossed([{"created": "garbage"}], "dm:owner1"), "an unreadable createTime: no note")

print(f"\nALL {N} CHECKS PASSED")

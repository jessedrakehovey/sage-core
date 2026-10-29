"""2026-10-29, an owner replying in a thread under the agent's 09:00 'Have the back taxes gone in yet?': "No you should
know what I'm talking about based on the thread I am replying to a message in / fix that context awareness."
The thread now comes from Google Chat itself (chat_thread.py): THIS THREAD SO FAR is complete, a one-message
thread says so, the newest lines of the space come from Chat for a top-level message, the `chat_thread` tool
reads the whole thread, and the record bugs that hid the root are closed. Scratch stores, a fake Chat, no model.
The fixture has the shape of that real thread; the people come from people.json, nothing else of the day."""
import os as _os, tempfile as _tf
_os.environ.setdefault("WV_LOG_DIR", _tf.mkdtemp(prefix="wv_logs_"))
import json
import os
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
TMP = Path(tempfile.mkdtemp(prefix="wv_cth_"))
os.environ["ASSISTANT_DATA"] = str(TMP / "adata")
os.environ["ASSISTANT_CARDS"] = str(TMP / "cards")
os.environ["AGENT_STATE"] = str(TMP / "agent_state")
os.environ["AGENT_DATA"] = str(TMP / "agent_data")
os.environ["AGENT_HOME_SPACE"] = "spaces/HOME1"
os.environ["CHAT_PUSH"] = "off"
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT / "agent"))
import core  # noqa: E402
import chat_thread  # noqa: E402
import place  # noqa: E402
import bridge  # noqa: E402
import gmcp  # noqa: E402
import intents  # noqa: E402

FAILS = []


def ok(cond, msg, extra=""):
    print(("ok   " if cond else "FAIL ") + msg + (f"  {extra}" if extra and not cond else ""))
    if not cond:
        FAILS.append(msg)


OWNER_KEY = core.first_owner()
OWNER = core.person_for(handle=OWNER_KEY)
ONAME = OWNER.get("name", "Owner")
NAME = core.profile().get("name", "Agent")
SPACE = "spaces/HOME1"
TID = "5r1S5_RsTws"
THREAD = f"{SPACE}/threads/{TID}"


def _m(mid, text, bot, t, thread=TID):
    snd = {"type": "BOT", "displayName": NAME} if bot else {"type": "HUMAN", "displayName": ONAME + " Owner", "name": "users/1", "email": OWNER.get("email", "")}
    return {"name": f"{SPACE}/messages/{thread}.{mid}", "text": text, "sender": snd, "createTime": t,
            "thread": {"name": f"{SPACE}/threads/{thread}"}}


GUESS = "Do you mean the rental cart screenshots, the follow-up in the booking system, or the two lead files?"
# the real thread's shape, as Chat listed it (UTC)
REAL = [_m(TID, "Morning. Have the back taxes gone in yet? Tell me when they're sent and I'll stop asking.", True, "2026-10-29T16:00:18Z"),
        _m("W9upaQUin08", "Give me link to where they are saved", False, "2026-10-29T16:44:03Z")]
ASK_NAME = REAL[1]["name"]
CALLS = []
EXTRA = []


def fake_list(space, filt, order="createTime asc", n=40):
    CALLS.append((space, filt, order))
    if "thread.name" in filt:
        tid = filt.split("/threads/")[1].rstrip('"')
        if tid == "GONE":
            raise RuntimeError("HTTP 503")
        rows = [m for m in REAL + EXTRA if m["thread"]["name"].endswith("/" + tid)]
        return sorted(rows, key=lambda m: m["createTime"])
    rows = sorted(REAL + EXTRA, key=lambda m: m["createTime"], reverse=True)
    return rows[:n]


chat_thread._list = fake_list
chat_thread.available = lambda: True
core.append(core.TRANSCRIPT, {"ts": "2026-10-29 06:10", "convo": SPACE, "who": NAME, "text": "Morning. A few things I picked up yesterday. Remembered: the waiting card...", "channel": "digest"})
core.append(core.TRANSCRIPT, {"ts": "2026-10-29 08:00", "convo": SPACE, "who": "card", "text": "[card q3f7c195942bb · business] Next step · Lead · Chris: The first reply is done.", "channel": "card"})
core.append(core.TRANSCRIPT, {"ts": "2026-10-29 09:44", "convo": SPACE, "who": ONAME, "text": "Give me link to where they are saved", "channel": "gchat", "thread": THREAD})

# ---- 1. the real event: a reply under a one-message thread whose root is NOT in the transcript
msg = {"channel": "gchat", "convo": SPACE, "space": SPACE, "thread": THREAD, "msg_name": ASK_NAME, "direct": True,
       "text": "Give me link to where they are saved"}
blk = place.block(msg, OWNER)
ok("THIS THREAD SO FAR (read from Google Chat itself" in blk, "the thread block is read from Chat")
ok("back taxes" in blk, "the root message (posted by a cron, never in the transcript) is in the block", blk)
ok("replying to this ONE message" in blk and "back taxes" in blk.split("ONE message")[1][:200], "a one-message thread says so, naming the message", blk)
ok("rental cart" not in blk and "Chris" not in blk.split("JUST BEFORE THIS")[0], "nothing from elsewhere in the space is offered as what they mean")
ok(core.thread_root(THREAD).get("text", "").startswith("Morning. Have the back taxes"), "the root is now remembered from Chat")
ok(any(f'thread.name = "{THREAD}"' in c[1] for c in CALLS), "one filtered read of the thread")

# ---- 2. the bridge's NOT ON RECORD line never appears when Chat has the thread
core.save(core.THREAD_ROOTS, {})
bridge._gchat = lambda: None
line = bridge.resolve_thread_root(msg, SPACE, THREAD)
ok(line == "", "resolve_thread_root has nothing to add once Chat gave the thread", line)

# ---- 3. a longer thread: every message, oldest first, the ask itself left out (the cache refreshes for a new message)
EXTRA[:] = [_m("y4ruqN7HbvE", f"@{ONAME} Owner {GUESS}", True, "2026-10-29T16:45:00Z"),
            _m("geM6EraRu9A", "No you should know what I'm talking about based on the thread I am replying to a message in", False, "2026-10-29T16:46:40Z")]
msg2 = dict(msg, msg_name=EXTRA[1]["name"], text="No you should know what I'm talking about based on the thread I am replying to a message in")
blk2 = place.block(msg2, OWNER)
part = blk2.split("THIS THREAD SO FAR", 1)[1].split("JUST BEFORE THIS")[0]
ok(part.index("back taxes") < part.index("Give me link") < part.index("Do you mean"), "oldest first, complete", part)
ok("ONE message" not in blk2 and "THIS thread's messages" in blk2, "a longer thread is not called one message")

# ---- 4. a top-level message: the newest lines of the space come from Chat (what is on their screen)
CALLS.clear()
top = {"channel": "gchat", "convo": SPACE, "space": SPACE, "thread": f"{SPACE}/threads/NEWTOP", "msg_name": f"{SPACE}/messages/NEWTOP.NEWTOP",
       "direct": True, "text": "I don't need this anymore"}
blk3 = place.block(top, OWNER)
ok("WHAT THEY ARE MOST LIKELY RESPONDING TO" in blk3 and "read from Google Chat itself" in blk3, "a top-level message gets the space's newest lines from Chat")
ok("Do you mean the rental cart" in blk3 and "(in a thread)" in blk3, "a reply inside a thread is marked as such", blk3)
ok(any("createTime >" in c[1] for c in CALLS), "one read of the space's newest messages")

# ---- 5. Chat unreadable: the transcript still answers, and the NOT ON RECORD line only when nothing anywhere knows
gone = f"{SPACE}/threads/GONE"
core.append(core.TRANSCRIPT, {"ts": "2026-10-29 10:00", "convo": SPACE, "who": NAME, "text": "The call sheet for Saturday is below.", "channel": "gchat", "thread": gone})
blk4 = place.block(dict(msg, thread=gone, msg_name=f"{SPACE}/messages/GONE.x1", text="Send it to her"), OWNER)
ok("THIS THREAD SO FAR (oldest first" in blk4 and "call sheet" in blk4, "with Chat down the transcript's thread lines are used", blk4)
line2 = bridge.resolve_thread_root(dict(msg, thread=gone, msg_name=f"{SPACE}/messages/GONE.x1"), SPACE, gone)
ok(line2 == "", "...and no NOT ON RECORD line while the transcript has the thread")
line3 = bridge.resolve_thread_root(dict(msg, thread=gone, msg_name=f"{SPACE}/messages/GONE.x2", convo="spaces/OTHER"), "spaces/OTHER", gone)
ok("NOT ON RECORD" in line3, "only when Chat is down AND nothing is on record does the brain get told to ask")

# ---- 6. the record bugs: a later line in a thread never replaces the remembered root; push_say files its thread
core.save(core.THREAD_ROOTS, {})
core.note_thread_root("spaces/S/threads/R", NAME, "first", "say", "spaces/S")
core.note_thread_root("spaces/S/threads/R", NAME, "a later line in the same thread", "say", "spaces/S")
ok(core.thread_root("spaces/S/threads/R").get("text") == "first", "the first message stays the thread's root")
core.note_thread_root("spaces/S/threads/U", "", "", "unreadable", "spaces/S")
core.note_thread_root("spaces/S/threads/U", NAME, "now readable", "chat", "spaces/S")
ok(core.thread_root("spaces/S/threads/U").get("text") == "now readable", "an unreadable mark gives way to the real root")

import chat_push  # noqa: E402
posted = []


class _G:
    def post(self, space, text, thread=""):
        posted.append((space, thread))
        return f"{space}/messages/ROOTX.ROOTX"

    def to_gchat(self, t):
        return t


_sv = (chat_push.enabled, chat_push._g, chat_push.targets, chat_push._voice)
chat_push.enabled, chat_push._g, chat_push.targets, chat_push._voice = (lambda: True), (lambda: _G()), (lambda lane, what: [("home", SPACE)]), (lambda t, k: t)
n = chat_push.push_say("Morning. Have the back taxes gone in yet?", "money")
chat_push.enabled, chat_push._g, chat_push.targets, chat_push._voice = _sv
row = [r for r in core.rows(core.TRANSCRIPT) if r.get("text", "").startswith("Morning. Have the back taxes")][-1]
ok(n == 1 and row.get("thread") == f"{SPACE}/threads/ROOTX", "a cron's top-level line is filed in the thread it starts", json.dumps(row))
ok(core.thread_root(f"{SPACE}/threads/ROOTX").get("text", "").startswith("Morning. Have"), "...and is that thread's root")

# ---- 7. the brain's tool: the turn's own thread, or the space; nothing else; staff may use it
EXTRA[:] = []
tp = intents.begin(OWNER, SPACE, THREAD, None, "")
os.environ["AGENT_TURN"] = str(tp)
out = gmcp.call("chat_thread", {})
ok(out.startswith("THIS THREAD, from Google Chat") and "back taxes" in out and "Give me link" in out, "the tool reads the turn's thread from Chat", out)
out2 = gmcp.call("chat_thread", {"what": "space", "max": 5})
ok("NEWEST MESSAGES" in out2 and "back taxes" in out2, "the tool reads the space's newest lines", out2)
tp2 = intents.begin(OWNER, f"dm:{OWNER_KEY}", "", None, "")
os.environ["AGENT_TURN"] = str(tp2)
ok("not in Google Chat" in gmcp.call("chat_thread", {}) or "top level" in gmcp.call("chat_thread", {}), "a DM turn with no space is told so, never another space")
ok("chat_thread" in gmcp.STAFF_TOOLS, "a team member's turn may read its own team-space thread")
ok(any(t["name"] == "chat_thread" for t in gmcp.TOOLS), "the tool is served")
import brain  # noqa: E402
tool = f"mcp__{brain.MCP_SERVER}__chat_thread"
ok(tool in brain.ALLOWED.split(",") and tool in brain.STAFF_ALLOWED.split(",") and tool in brain.BUSINESS_ALLOWED.split(","),
   "the tool is allowed in every turn kind, under the server's own name")

# ---- 8. the prompt says it
pm = (ROOT / "agent" / "prompt.md").read_text(encoding="utf-8")
ok("read from Google Chat itself" in pm and "never say you cannot see the top of a thread" in pm and "`chat_thread`" in pm,
   "the prompt: the thread is complete, the tool exists, never 'I can't see the top of a thread'")
ok("only the thread's own messages are candidates" in pm, "the prompt: inside a thread nothing from elsewhere is offered")

print("\nALL PASS" if not FAILS else f"\nFAILED: {FAILS}")
sys.exit(1 if FAILS else 0)

"""Offline tests for thread awareness (2026-10-11: the owner replied 'I don't need this anymore' in the
thread of a message code had posted and Sage guessed): thread roots are remembered for every
posted message, transcript_thread falls back to the root, an unknown root is fetched from Chat
once, closed cards still resolve, and the WHERE block tells the brain what the thread is (or that
nothing is on record, so it must ask, not guess). Scratch stores; a fake Chat client; no model.
   python assistant/tests/test_threadfix.py
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
TMP = Path(tempfile.mkdtemp(prefix="wv_thread_"))
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
import bridge  # noqa: E402
import gchat  # noqa: E402
import place  # noqa: E402
import chat_thread  # noqa: E402
chat_thread._list = lambda *a, **k: (_ for _ in ()).throw(RuntimeError("offline in tests"))   # Chat is never read from a test
import cards  # noqa: E402

N = 0


def ok(cond, msg):
    global N
    N += 1
    if not cond:
        print(f"FAIL {N}: {msg}")
        sys.exit(1)
    print(f"ok {N}: {msg}")


owner1 = core.person_for(handle="owner1")
SPACE = "spaces/HOME1"
NAME = core.profile().get("name", "Agent")


class FakeChat:
    def __init__(self):
        self.log, self.n, self.messages = [], 0, {}

    def post(self, space, text, thread=""):
        self.n += 1
        name = f"{space}/messages/M{self.n}.M{self.n}"
        self.log.append(("post", space, text, thread))
        self.messages[f"{space}/messages/M{self.n}"] = {"text": text, "bot": True, "sender": NAME}
        return name

    def patch(self, name, text):
        self.log.append(("patch", name, text))
        return True

    def delete(self, name):
        self.log.append(("delete", name))
        return True

    thread_of = staticmethod(gchat.thread_of)

    def get(self, name):
        return dict(self.messages.get(name) or {})


fc = FakeChat()
bridge._gchat = lambda: fc
core.save(bridge.SPACES, {"owner1": SPACE})
core.PROFILE_CACHE = None
_home = core.home_space


def home():
    return SPACE


core.home_space = home

# ---------------------------------------------------------------- 1. a code-posted message remembers its thread
ok(gchat.thread_of("spaces/S/messages/AbC.AbC") == "spaces/S/threads/AbC" and gchat.thread_of("nope") == "", "a message name maps to its thread")
bridge.post("owner1", "I built that thing you asked for: texts from family are no longer carded.", "builder")
th = gchat.thread_of(f"{SPACE}/messages/M1.M1")
root = core.thread_root(th)
ok(root and root["who"] == NAME and "family are no longer carded" in root["text"] and root["channel"] == "builder", "bridge.post remembers the root of the thread it started")
rows = core.transcript_thread(SPACE, th)
ok(len(rows) == 1 and rows[0].get("root") and rows[0]["who"] == NAME, "transcript_thread falls back to the root when the transcript has no rows for the thread")
blk = place.block({"channel": "gchat", "convo": SPACE, "thread": th, "direct": True, "space": SPACE, "text": "I don't need this anymore"}, owner1)
ok("THIS THREAD SO FAR" in blk and "family are no longer carded" in blk and "started the thread" in blk and "refer to THIS thread" in blk,
   "the WHERE block shows the message they are replying to")

# ---------------------------------------------------------------- 2. the agent's own reply (placeholder edited) starts a thread too
fc.log.clear()
msg = {"channel": "gchat", "person": owner1, "text": "ok", "space": SPACE, "thread": "", "convo": SPACE}
ack = fc.post(SPACE, "_typing_")
bridge.reply(msg, "Done, it's on your calendar.", ack)
th2 = gchat.thread_of(ack)
ok(core.thread_root(th2).get("text") == "Done, it's on your calendar.", "a reply delivered by editing the placeholder is remembered as that thread's root")
fc.log.clear()
bridge.reply({**msg, "thread": th2}, "Yes, Saturday.", "")
ok(not any(k == th2 and v.get("text") == "Yes, Saturday." for k, v in core.load(core.THREAD_ROOTS, {}).items()), "a reply INSIDE a thread does not overwrite that thread's root")

# ---------------------------------------------------------------- 3. an unknown thread is fetched from Chat once
fc.messages[f"{SPACE}/messages/Z9"] = {"text": "example mail down: the inbox can't be reached. Leave it?", "bot": True, "sender": NAME}
thz = f"{SPACE}/threads/Z9"
extra = bridge.resolve_thread_root({"space": SPACE}, SPACE, thz)
ok(extra == "" and "inbox can't be reached" in core.thread_root(thz).get("text", ""), "a thread with no record is resolved from Chat and remembered")
extra = bridge.resolve_thread_root({"space": SPACE}, SPACE, f"{SPACE}/threads/NOPE")
ok("NOT ON RECORD" in extra and "Do not guess" in extra, "a thread nobody can resolve tells the brain to ask, not guess")
fc.messages[f"{SPACE}/messages/H7"] = {"text": "Hello owner6 and Sage", "bot": False, "sender": "owner1 example"}
bridge.resolve_thread_root({"space": SPACE}, SPACE, f"{SPACE}/threads/H7")
ok(core.thread_root(f"{SPACE}/threads/H7").get("who") == "owner1 example", "a human-started thread keeps the human as its root author")

# ---------------------------------------------------------------- 4. closed cards still resolve, and say so
r = cards.post([{"question": "owner6 texted 'I love you'.\n[TEXT PREVIEW] To owner6 (+18055550100):\nLove you too", "header": "Text reply: owner6",
                 "lane": "personal", "audience": "owner", "options": [{"label": "Send (Recommended)"}, {"label": "Skip"}],
                 "exec": {"kind": "imessage_send", "approve_label": "Send (Recommended)",
                          "payload": {"account": "owner1-iphone", "to": "+18055550100", "body": "Love you too"}}}], owner="operator", lane="personal")
qid = r["qid"]
cpath = next(p for p in cards.LIVE.glob("q*.json") if qid in p.name)
c = json.loads(cpath.read_text(encoding="utf-8"))
c["chat"] = {"owner1": f"{SPACE}/messages/C1.C1"}
c["status"] = "expired"
cpath.write_text(json.dumps(c), encoding="utf-8")
found = bridge.card_for_thread(f"{SPACE}/threads/C1")
ok(found and found["id"] == qid, "an expired card is still found from its thread")
ctx = bridge.card_context(found)
ok("already expired" in ctx and "talking about it, not answering it" in ctx, "the brain is told the card is closed and what that means")

# ---------------------------------------------------------------- 4b. an owner's fresh message is its own thread's root
own = f"{SPACE}/messages/J1.J1"
extra = bridge.resolve_thread_root({"space": SPACE, "msg_name": own}, SPACE, f"{SPACE}/threads/J1")
ok(extra == "" and not core.thread_root(f"{SPACE}/threads/J1"), "a new top-level message from an owner is not an unknown thread (2026-10-12 07:17 regression)")
core.say(SPACE, "owner1", "any messages from Finley?", channel="gchat", meta={"thread": f"{SPACE}/threads/J2"})
core.note_thread_root(f"{SPACE}/threads/J2", "", "", "unreadable", SPACE)
extra = bridge.resolve_thread_root({"space": SPACE, "msg_name": f"{SPACE}/messages/J3.J3"}, SPACE, f"{SPACE}/threads/J2")
ok(extra == "", "a thread whose earlier lines are in the transcript needs no warning even when Chat would not show its root")
# 4b'. a REPLY (ID.OTHER) in a thread nothing recorded is not its own root: the root is fetched (2026-10-16 21:15, the 35mm report)
fc.messages[f"{SPACE}/messages/D1"] = {"text": "I could not finish: Lensrentals has many 35mm lenses, and I can't tell which one you mean.", "bot": True, "sender": NAME}
extra = bridge.resolve_thread_root({"space": SPACE, "msg_name": f"{SPACE}/messages/D1.R2"}, SPACE, f"{SPACE}/threads/D1")
ok(extra == "" and "35mm lenses" in core.thread_root(f"{SPACE}/threads/D1").get("text", ""),
   "a reply in a doer report's thread fetches the report as the root instead of passing as a fresh message")
ok(bridge._is_thread_root(f"{SPACE}/messages/K1.K1") and not bridge._is_thread_root(f"{SPACE}/messages/K1.K2") and not bridge._is_thread_root(""),
   "only ID.ID is a top-level message")

# 4b''. a doer report posted into the home space without a thread remembers the thread it starts
sys.path.insert(0, str(ROOT / "doer"))
import doer as _doer  # noqa: E402
_doer.subprocess.run = lambda *a, **k: None                         # no Mission Control notify in tests
_n0 = fc.n
_doer.deliver({"id": "t35mm", "payload": {"from": "owner1", "convo": SPACE, "thread": ""}}, "I could not finish: which 35mm?")
_thr = f"{SPACE}/threads/M{fc.n}"
ok(fc.n == _n0 + 1 and "which 35mm" in core.thread_root(_thr).get("text", "")
   and any(r.get("thread") == _thr and r.get("channel") == "doer" for r in core.transcript_thread(SPACE, _thr, 5)),
   "a doer report that starts a thread is on record as that thread's root, with the transcript row in the thread")

# ---------------------------------------------------------------- 4c. a fresh top-level message is shown what it most likely answers
core.say(SPACE, "card", "[card q1 · wedding] example mail down: the inbox can't be reached. Leave it?", channel="card", meta={"thread": f"{SPACE}/threads/CARD1"})
core.say(SPACE, NAME, "I'll only draft for wedding clients; the personal cards are being shut off.", channel="gchat", meta={"thread": f"{SPACE}/threads/R1"})
blk = place.block({"channel": "gchat", "convo": SPACE, "thread": f"{SPACE}/threads/J9", "msg_name": f"{SPACE}/messages/J9.J9",
                   "direct": True, "space": SPACE, "text": "I don't need this anymore"}, owner1)
ok("MOST LIKELY RESPONDING TO" in blk and "personal cards are being shut off" in blk and "example mail down" in blk,
   "a top-level message gets the newest posts before it, so 'this' has a referent")
ok("THIS THREAD SO FAR" not in blk, "a top-level message is not treated as a thread reply")
blk2 = place.block({"channel": "gchat", "convo": SPACE, "thread": f"{SPACE}/threads/R1", "msg_name": f"{SPACE}/messages/R1.J10",
                    "direct": True, "space": SPACE, "text": "That's not what I'm talking about"}, owner1)
ok("THIS THREAD SO FAR" in blk2 and "MOST LIKELY RESPONDING TO" not in blk2, "a real in-thread reply gets the thread, not the guess list")

# ---------------------------------------------------------------- 4d. "I said..." pulls in the owner's own earlier lines
core.say(SPACE, "owner1", "move the Lensrentals return to Thursday", channel="gchat", meta={"thread": f"{SPACE}/threads/OLD1"})
core.append(core.TRANSCRIPT, {"ts": "2020-01-01 09:00", "convo": SPACE, "who": "owner1", "text": "ancient ask about tripods", "channel": "gchat"})
blk3 = place.block({"channel": "gchat", "convo": SPACE, "thread": f"{SPACE}/threads/R1", "msg_name": f"{SPACE}/messages/R1.J11",
                    "direct": True, "space": SPACE, "text": "I told you to do it Thursday"}, owner1)
ok("REFERRING BACK" in blk3 and "Lensrentals return to Thursday" in blk3, "'I told you' shows the owner's own lines from other threads")
ok("ancient ask about tripods" not in blk3 and "I told you to do it Thursday" not in blk3.split("REFERRING BACK", 1)[1],
   "only the last 24 hours, and not the message itself")
ok("REFERRING BACK" not in blk2, "an ordinary message gets no refer-back block")

# ---------------------------------------------------------------- 5. bounded store
for i in range(420):
    core.note_thread_root(f"spaces/X/threads/T{i}", NAME, f"t{i}", "post", "spaces/X")
ok(len(core.load(core.THREAD_ROOTS, {})) <= 400, "the root store stays bounded")

print(f"\nALL {N} CHECKS PASSED")

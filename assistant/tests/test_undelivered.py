"""Offline tests for undelivered replies (2026-11-01: the owner had to ask 'you never replied how to
respond to Harper' before he got the answer): a reply Chat refuses is queued, the minute tick
posts it into its thread once Chat is back, and one that never gets through is flagged on
Mission Control instead of lost. Scratch stores; a fake Chat client; no model.
   python assistant/tests/test_undelivered.py
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
TMP = Path(tempfile.mkdtemp(prefix="wv_undeliv_"))
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
import cards  # noqa: E402

N = 0


def ok(cond, msg):
    global N
    N += 1
    if not cond:
        print(f"FAIL {N}: {msg}")
        sys.exit(1)
    print(f"ok {N}: {msg}")


OWNER = core.person_for(handle=core.first_owner())       # the primary owner, whoever the organization's is
SPACE = "spaces/HOME1"
THREAD = f"{SPACE}/threads/K1"


class FakeChat:
    def __init__(self):
        self.log, self.n, self.down = [], 0, True

    def post(self, space, text, thread=""):
        if self.down:
            raise RuntimeError("chat 503")
        self.n += 1
        self.log.append((space, text, thread))
        return f"{space}/messages/M{self.n}.M{self.n}"

    def patch(self, name, text):
        if self.down:
            raise RuntimeError("chat 503")
        return True

    def delete(self, name):
        return True

    def ensure_mention(self, text, space, user):
        return text

    thread_of = staticmethod(gchat.thread_of)


fc = FakeChat()
bridge._gchat = lambda: fc
msg = {"channel": "gchat", "person": OWNER, "text": "you never replied how to respond to Harper",
       "space": SPACE, "thread": THREAD, "convo": SPACE}

bridge.reply(msg, "Here's how to answer Harper: thank her and confirm Saturday.", "")
q = core.load(bridge.UNDELIVERED, [])
ok(len(q) == 1 and "answer Harper" in q[0]["text"] and q[0]["thread"] == THREAD, "a reply Chat refused is queued, not dropped")
ok(bridge.undelivered_retry() == 0 and core.load(bridge.UNDELIVERED, [])[0]["tries"] == 2, "Chat still down: it stays queued, one more try counted")

fc.down = False
ok(bridge.undelivered_retry() == 1 and fc.log and "answer Harper" in fc.log[-1][1] and fc.log[-1][2] == THREAD,
   "the minute tick posts the queued reply into its own thread once Chat is back")
ok(core.load(bridge.UNDELIVERED, []) == [], "a delivered retry leaves the queue")

bridge.reply(msg, "All set.", "")
ok(core.load(bridge.UNDELIVERED, []) == [], "a reply that posts the first time is never queued")

fc.down = True
bridge.reply(msg, "The caterer call is at 3pm.", "")
for _ in range(bridge.UNDELIVERED_TRIES):
    bridge.undelivered_retry()
ok(core.load(bridge.UNDELIVERED, []) == [], "after the last try it leaves the queue")
notes = [json.loads(ln) for ln in Path(cards.NOTES).read_text(encoding="utf-8").splitlines() if ln.strip()] if Path(cards.NOTES).exists() else []
ok(any("caterer call" in n.get("text", "") and "never reached Chat" in n.get("text", "") for n in notes),
   "a reply that never gets through is flagged on Mission Control")

print(f"\nALL {N} CHECKS PASSED")

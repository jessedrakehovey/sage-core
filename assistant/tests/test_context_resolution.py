"""2026-10-25, the owner: "It should never have to assume what I am talking about: it should be able to tell by what
thread it is and what we were just talking about or what card it just sent me to approve."
place.block now carries JUST BEFORE THIS (the last hour across threads, each line tagged with its thread's
subject) and YOUR LAST CARDS (recent cards with where each stands). Scratch stores, no model."""
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
TMP = Path(tempfile.mkdtemp(prefix="wv_ctx_"))
os.environ["ASSISTANT_DATA"] = str(TMP / "adata")
os.environ["ASSISTANT_CARDS"] = str(TMP / "cards")
os.environ["AGENT_STATE"] = str(TMP / "agent_state")
os.environ["AGENT_DATA"] = str(TMP / "agent_data")
os.environ["AGENT_HOME_SPACE"] = "spaces/HOME1"
os.environ["CHAT_PUSH"] = "off"
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT / "agent"))
import core  # noqa: E402
import place  # noqa: E402
import cards  # noqa: E402
import exec_store as es  # noqa: E402

FAILS = []


def ok(cond, msg, extra=""):
    print(("ok   " if cond else "FAIL ") + msg + (f"  {extra}" if extra and not cond else ""))
    if not cond:
        FAILS.append(msg)


owner1 = core.person_for(handle="owner1")
NAME = core.profile().get("name", "Agent")
SPACE = "spaces/HOME1"
T_MARLO = f"{SPACE}/threads/MAT1"
T_CORWIN = f"{SPACE}/threads/ROB1"
T_NEW = f"{SPACE}/threads/NEW1"


def say(mins_ago, who, text, thread=""):
    ts = time.strftime("%Y-%m-%d %H:%M", time.localtime(time.time() - mins_ago * 60))
    core.append(core.TRANSCRIPT, {"ts": ts, "convo": SPACE, "who": who, "text": text, "channel": "gchat", "thread": thread})


say(50, "owner1", "Send Corwin the Sloane & Bram handoff package", T_CORWIN)
say(49, NAME, "The email to Corwin with the 78 GB Dropbox export is on the card below.", T_CORWIN)
say(12, "owner1", "Ask Marlo if he is free Nov 7", T_MARLO)
say(11, NAME, "The text to Marlo is on the card below for your tap.", T_MARLO)
say(3, "owner1", "Say it is flat for the day if his camera shoots 4K raw", T_MARLO)
say(2, NAME, "I changed the card: flat for the day on his 4K raw camera. The new card is below.", T_MARLO)

# two cards: one answered and sent, one still open
q = {"header": "Reply to Marlo", "question": "Reply to Marlo.\n[TEXT PREVIEW] To Marlo (+13525550101):\nFlat for the whole day on your 4K raw camera.",
     "lane": "wedding", "audience": "vendor", "options": [{"label": "Send (Recommended)"}, {"label": "Skip"}],
     "exec": {"kind": "imessage_send", "approve_label": "Send (Recommended)", "payload": {"account": "owner1-iphone", "to": "+13525550101", "body": "Flat for the whole day on your 4K raw camera."}}}
r1 = cards.post([q], owner="agent:owner1", lane="wedding", thread=T_MARLO, asked=True)
ok(r1.get("posted"), "the Marlo card posts", r1)
live = cards._read(cards.LIVE / f"{r1['qid']}.json")
live["chat_thread"] = T_MARLO
cards._write(cards.LIVE / f"{r1['qid']}.json", live)
a1 = cards.answer(r1["qid"], "Send (Recommended)")
es.set_status(a1["ticket"], "done", "PHONE_QUEUED m-1 -> ...0101")
q2 = json.loads(json.dumps(q)); q2["header"] = "Email to Corwin: Sloane & Bram handoff"; q2["question"] = "Email Corwin.\n[EMAIL PREVIEW] From: owner3@example.invalid To: corwin@example.com\nSubject: Sloane & Bram files\nHere is the export."
q2["exec"] = {"kind": "email_send", "approve_label": "Send (Recommended)", "payload": {"from": "owner3@example.invalid", "to": "corwin@example.com", "subject": "Sloane & Bram files", "body": "Here is the export."}}
r2 = cards.post([q2], owner="agent:owner1", lane="wedding", thread=T_CORWIN, asked=True)
ok(r2.get("posted"), "the Corwin card posts", r2)
live2 = cards._read(cards.LIVE / f"{r2['qid']}.json")
live2["chat_thread"] = T_CORWIN
cards._write(cards.LIVE / f"{r2['qid']}.json", live2)

# ---- a brand-new thread, a bare noun: the blocks say what was just on and where the cards stand
blk = place.block({"channel": "gchat", "convo": SPACE, "thread": T_NEW, "msg_name": f"{SPACE}/messages/NEW1.NEW1", "direct": True,
                   "space": SPACE, "text": "Look at the most recent export file name for the project"}, owner1)
ok("JUST BEFORE THIS" in blk, "a new thread gets JUST BEFORE THIS")
ok("(Ask Marlo if he is free Nov 7)" in blk or "(Ask Marlo" in blk, "lines are tagged with their thread's subject", blk)
ok("Sloane & Bram handoff" in blk and "4K raw" in blk, "both recent threads are in view, so 'the project' and 'the export' resolve to the handoff, not a guess", blk)
ok("YOUR LAST CARDS" in blk, "a new thread gets YOUR LAST CARDS")
ok("Reply to Marlo" in blk and "went out" in blk, "the sent card shows as tapped and gone", blk)
ok("Email to Corwin" in blk and "open, waiting for a tap" in blk, "the open card shows as waiting", blk)
i_mat = blk.index("Reply to Marlo"); i_rob = blk.index("Email to Corwin")
ok(i_mat < i_rob, "cards are oldest first (the newest is last)")

# ---- inside the Marlo thread: its own lines are in THIS THREAD SO FAR, not repeated in JUST BEFORE THIS
blk2 = place.block({"channel": "gchat", "convo": SPACE, "thread": T_MARLO, "msg_name": f"{SPACE}/messages/MAT1.X9", "direct": True,
                    "space": SPACE, "text": "Remake the card"}, owner1)
ok("THIS THREAD SO FAR" in blk2 and "4K raw" in blk2.split("JUST BEFORE THIS")[0], "the thread's own lines lead")
after = blk2.split("JUST BEFORE THIS", 1)[1] if "JUST BEFORE THIS" in blk2 else ""
ok("Ask Marlo if he is free" not in after.split("YOUR LAST CARDS")[0], "the current thread's lines are not repeated under JUST BEFORE THIS", after[:300])
ok("YOUR LAST CARDS" in blk2 and "went out" in blk2, "inside the thread the card state is still there: 'remake' can be answered without guessing")

# ---- the prompt carries the resolution order and bans guessing
pm = (ROOT / "agent" / "prompt.md").read_text(encoding="utf-8")
ok("I'm guessing" in pm and "YOUR LAST CARDS" in pm and "JUST BEFORE THIS" in pm, "the prompt names the blocks and the order")

print("\nALL PASS" if not FAILS else f"\nFAILED: {FAILS}")
sys.exit(1 if FAILS else 0)

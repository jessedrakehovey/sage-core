"""Offline tests for receipts (2026-10-21): one status line per hand-off, in the place it was asked,
edited in place by every later outcome; commitments and the doer report through it; the reopened
card's old message stops saying 'goes out in 35 s'; the honest timing copy. Scratch stores; a fake
Chat client; no model.
   python assistant/tests/test_receipts.py
"""
import os as _os, tempfile as _tf
_os.environ.setdefault("WV_LOG_DIR", _tf.mkdtemp(prefix="wv_logs_"))
import os
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
TMP = Path(tempfile.mkdtemp(prefix="wv_receipts_"))
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
import receipts  # noqa: E402
import commitments  # noqa: E402
import chat_push  # noqa: E402

N = 0


def ok(cond, msg):
    global N
    N += 1
    if not cond:
        print(f"FAIL {N}: {msg}")
        sys.exit(1)
    print(f"ok {N}: {msg}")


# ---------------------------------------------------------------- a fake Chat client
POSTS, PATCHES = [], []


class FakeChat:
    @staticmethod
    def configured():
        return True

    @staticmethod
    def post(space, text, thread=""):
        POSTS.append((space, text, thread))
        return f"{space}/messages/m{len(POSTS)}.m{len(POSTS)}"

    @staticmethod
    def patch(name, text):
        PATCHES.append((name, text))
        return True


receipts._gchat = lambda: FakeChat
core.save(core.DATA / "gchat_spaces.json", {"owner1": "spaces/DMOWNER1"})
HOME = "spaces/HOME"
TH = "spaces/HOME/threads/t1"

# ---------------------------------------------------------------- 1. one message per ref: post, then edits
m1 = receipts.say("tk-1", HOME, TH, "I'm getting that drafted.")
m2 = receipts.say("tk-1", HOME, TH, "The card is below.")
m3 = receipts.say("tk-1", HOME, TH, "The card is below.")
ok(len(POSTS) == 1 and POSTS[0][2] == TH and len(PATCHES) == 1 and PATCHES[0][0] == m1 and m2 == m1,
   "the first outcome posts in the asking thread; the next EDITS that message; the same text again is a no-op")
ok(m3 == m1 and len(PATCHES) == 1, "an unchanged outcome never re-posts or re-edits")
rows = [r for r in core.rows(core.TRANSCRIPT) if r.get("ref") == "tk-1"]
ok(len(rows) == 2 and rows[1].get("edited") is True and rows[1].get("thread") == TH, "the transcript carries both states, the edit marked, with the thread")
ok(receipts.status("tk-1")["edits"] == 1 and receipts.status("tk-1")["text"] == "The card is below.", "the store holds the current state and the edit count")

# ---------------------------------------------------------------- 2. a DM ask reports into the DM, never the home space
POSTS.clear()
receipts.say("tk-2", "dm:owner1", "", "Done: the water bill shows $0 due.")
ok(POSTS and POSTS[-1][0] == "spaces/DMOWNER1" and POSTS[-1][2] == "", "a DM hand-off's line goes to that owner's DM, top of the DM")
POSTS.clear()
receipts.say("tk-3", "dm:owner1", "", "On it.", space="spaces/DMOWNER1")
ok(POSTS[-1][0] == "spaces/DMOWNER1", "an explicit space (the ask's own) wins")

# ---------------------------------------------------------------- 3. commitments report through the receipt of the row's ref
owner1 = core.person_for(handle="owner1")
POSTS.clear(); PATCHES.clear()
commitments.register_applied([{"kind": "task", "what": "a reply to example about the proposal", "ref": "tk-example"}], owner1,
                             {"convo": HOME, "thread": TH})
from datetime import datetime, timedelta  # noqa: E402
later = datetime.now() + timedelta(minutes=commitments.DEADLINE_MIN["task"] + 5)
said = commitments.check(receipts.poster(), now=later.replace(hour=12))
ok(said and POSTS and POSTS[-1][2] == TH and POSTS[-1][1] == said[0]["text"] and "not going to keep repeating" not in said[0]["text"],
   "a task that never made it says so ONCE, in its thread, through the receipt, in plain words")
ok(receipts.status("tk-example")["text"] == said[0]["text"], "the slipped line is the row's receipt (a later outcome edits it, never a second line)")
lines = [commitments.line_for({"kind": k, "what": "x", "reports": 0}, "slipped", "") for k in ("build", "do", "task")]
ok(all("repeating" not in ln and "retrying on my own" not in ln for ln in lines), "no 'I'm not going to keep repeating it' anywhere")
ok(commitments._builder_said("anything at all that used to match") is False, "the builder's transcript note no longer silences the build's confirmation")

# ---------------------------------------------------------------- 4. the doer's report is the job's receipt, in the DM for a DM ask
sys.path.insert(0, str(ROOT / "doer"))
import doer  # noqa: E402
POSTS.clear()
doer.deliver({"id": "ab12cd34ef56ab12", "payload": {"from": "owner1", "convo": "dm:owner1", "thread": "", "space": "spaces/DMOWNER1"}}, "Done: lens rental booked for Friday.")
ok(POSTS and POSTS[-1][0] == "spaces/DMOWNER1" and "lens rental" in POSTS[-1][1], "a doer report for a DM ask lands in the DM (was: top-level in the home space)")
doer.deliver({"id": "ab12cd34ef56ab12", "payload": {"from": "owner1", "convo": "dm:owner1", "thread": "", "space": "spaces/DMOWNER1"}}, "Done: lens rental booked for Friday; confirmation #4411.")
ok(len(POSTS) == 1 and PATCHES and "4411" in PATCHES[-1][1], "a second report on the same job edits the first line")

# ---------------------------------------------------------------- 5. honest timing copy
ok("about 36 s" in chat_push.goes_out_line("imessage_send", hour=12) and "phone" in chat_push.goes_out_line("imessage_send", hour=12), "a text says about 36 s to the phone")
ok("within 10 minutes" in chat_push.goes_out_line("email_send", hour=12) and "overnight" in chat_push.goes_out_line("email_send", hour=12), "an email says the next pass, within 10 minutes, held overnight")
ok("Held until 7:00 AM" in chat_push.goes_out_line("imessage_send", hour=22) and "phone" in chat_push.goes_out_line("imessage_send", hour=22), "a text tapped at night says it is held until 7:00 AM (owner1 2026-10-24)")
ok("35 s" not in chat_push.goes_out_line("hb_note"), "nothing says 35 s any more")

# ---------------------------------------------------------------- 6. notes: new lines while a job runs, capped, in the thread (2026-10-28)
POSTS.clear(); PATCHES.clear()
REF = "cafe0000cafe0000"
for i in range(1, 7):
    receipts.note(REF, "dm:owner1", "spaces/DMOWNER1/threads/t1", f"Milestone {i}: item {i} is in the cart.", space="spaces/DMOWNER1", cap=4)
ok(len(POSTS) == 4 and all(p[2] == "spaces/DMOWNER1/threads/t1" for p in POSTS) and "item 4" in POSTS[-1][1] and not PATCHES,
   "a note is a NEW line in the job's thread, never an edit; only the first four per job reach Chat")
ok(receipts.status(REF)["notes"] == 4 and not receipts.status(REF)["name"], "the row counts the notes that reached Chat and holds no receipt yet")
rows = [r for r in core.rows(core.TRANSCRIPT) if r.get("ref") == REF]
ok(len(rows) == 6 and all(r.get("note") for r in rows) and rows[-1].get("held") and not rows[3].get("held"),
   "every note gets a transcript row; the ones past the cap are marked held")
receipts.say(REF, "dm:owner1", "spaces/DMOWNER1/threads/t1", "Done: cart built, stopped at checkout.", space="spaces/DMOWNER1", channel="doer")
ok(len(POSTS) == 5 and not PATCHES and [r for r in core.rows(core.TRANSCRIPT) if r.get("ref") == REF][-1].get("edited") is None,
   "the receipt after the notes is a first line, not an edit of a note")
receipts.say(REF, "dm:owner1", "spaces/DMOWNER1/threads/t1", "Done: cart built; total $1,180.", space="spaces/DMOWNER1", channel="doer")
ok(len(POSTS) == 5 and PATCHES and "1,180" in PATCHES[-1][1] and receipts.status(REF)["notes"] == 4, "the next outcome edits the receipt; the note count stays")
POSTS.clear()
REF2 = "cafe1111cafe1111"
receipts.note(REF2, "dm:owner1", "", "Signed in; dates set.", space="spaces/DMOWNER1")
th = receipts.status(REF2)["thread"]
receipts.say(REF2, "dm:owner1", "", "Done.", space="spaces/DMOWNER1", channel="doer")
ok(POSTS[0][2] == "" and th.startswith("spaces/DMOWNER1/threads/") and POSTS[1][2] == th, "a first note with no thread starts one, and the receipt joins it")
ok(receipts.note(REF2, "dm:owner1", th, "", space="spaces/DMOWNER1") == "" and receipts.note("", "dm:owner1", th, "x", space="spaces/DMOWNER1") != "" , "an empty note is nothing; a note needs no ref to post")

print(f"\nall {N} checks passed")

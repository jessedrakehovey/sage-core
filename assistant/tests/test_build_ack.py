"""test_build_ack.py - build taps in a row share one line that names each build (2026-10-19).

the owner tapped "Build it, I approve" on four cards within a minute and got four identical "Got it, building it now"
lines, none saying which build it meant. Now the first tap's line names its build and each later tap within
voice.BATCH_MIN edits that same message: "Building all four: search-terms report, retainer conversions, ...".
Scratch stores, the null channel; no model, no Chat.
   python assistant/tests/test_build_ack.py
"""
import os
import sys
import tempfile
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
TMP = Path(tempfile.mkdtemp(prefix="wv_buildack_"))
os.environ.setdefault("WV_LOG_DIR", str(TMP / "logs"))
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
os.environ["ASSISTANT_CARDS_DELIVERY"] = "weekly"
for d in ("logs", "adata", "cards", "agent_state", "agent_data", "doer"):
    (TMP / d).mkdir(parents=True, exist_ok=True)
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT / "agent"))
sys.path.insert(0, str(ROOT))
import core  # noqa: E402
import voice  # noqa: E402
import cards  # noqa: E402
import chat_push  # noqa: E402
import builder  # noqa: E402
from channels.base import NullChannel  # noqa: E402

N = 0


def ok(cond, msg):
    global N
    N += 1
    if not cond:
        print(f"FAIL {N}: {msg}")
        sys.exit(1)
    print(f"ok {N}: {msg}")


ACK = "Got it, building it now with your OK. You'll hear from me here when it lands, with an undo."
nc = NullChannel()
chat_push.enabled = lambda: True
chat_push._g = lambda: nc
chat_push.targets = lambda lane="", kind="card": [("owner1", "spaces/HOME")]
builder.kick = lambda force=False: "kicked"

core.save(builder.STATE, {"Wiki/Personal/Old one.md": {"title": "Old one", "status": "approved", "answered": "2026-01-01 09:00"}})
ok(voice.gate(ACK) == "On it. I'll tell you here when it's live.", "no approval in this run of taps: the plain ack stands")

titles = ["Build request Search-terms report", "Build request Retainer conversions",
          "Build request Session-start memory", "Build request Mid-edit note"]
for t in titles:
    st = core.load(builder.STATE, {})
    st[f"Wiki/Personal/{t}.md"] = {"title": t, "attempts": 0}
    core.save(builder.STATE, st)
    r = builder.owner_answer(f"Wiki/Personal/{t}.md", builder.BUILD_OK, by="owner1")   # the tap, as cards._act_on_build
    assert r.get("status") == "approved", r
    cards.say("builder", "personal", ACK)                                              # then its ack line

ok(len(nc.posted) == 1, f"four taps in a row post ONE message: {[x['text'] for x in nc.posted]}")
ok(nc.posted[0]["text"] == "On it: building \"search-terms report\". I'll tell you here when it's live.",
   f"the first tap names its build: {nc.posted[0]['text']!r}")
ok(len(nc.patched) == 3 and all(x["name"] == nc.posted[0]["name"] for x in nc.patched), "the later taps edit that same message")
ok(nc.patched[-1]["text"] == "Building all four: search-terms report, retainer conversions, session-start memory and "
   "mid-edit note. I'll tell you here as each one goes live.", f"the message names every build: {nc.patched[-1]['text']!r}")
ok("Old one" not in nc.patched[-1]["text"], "a build approved long ago is not in the batch")
ok(voice.build_ack(["a", "b"]).startswith("Building both: a and b."), "two taps read 'both'")
ok(voice.batch_message("spaces/HOME", now=time.time() + (voice.BATCH_MIN + 2) * 60) == "",
   "a tap after the window starts a fresh message")
ok(voice.classify(ACK) == "ack", "the ack is still an ack to the gate")

print(f"\nALL OK ({N} checks)")

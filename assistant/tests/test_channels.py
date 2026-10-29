"""Offline tests for WP4 (2026-10-24): the channel seam (assistant/channels), the slash surface and /quiet.
   python assistant/tests/test_channels.py
"""
import os as _os, tempfile as _tf
_os.environ.setdefault("WV_LOG_DIR", _tf.mkdtemp(prefix="wv_logs_"))
import json
import os
import sys
import tempfile
import time
from datetime import datetime, timedelta
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
TMP = Path(tempfile.mkdtemp(prefix="wv_chan_"))
os.environ["ASSISTANT_DATA"] = str(TMP / "adata")
os.environ["ASSISTANT_CARDS"] = str(TMP / "cards")
os.environ["ASSISTANT_LEARNINGS"] = str(TMP / "learnings")
os.environ["AGENT_STATE"] = str(TMP / "agent_state")
os.environ["AGENT_DATA"] = str(TMP / "agent_data")
os.environ["AGENT_SUGGESTIONS"] = str(TMP / "sugg.md")
os.environ["RULINGS_FILE"] = str(ROOT / "data" / "rulings.json")
os.environ["ASSISTANT_CHANNEL"] = "null"
for d in ("adata", "cards", "agent_state", "agent_data"):
    (TMP / d).mkdir(parents=True, exist_ok=True)
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT / "agent"))
sys.path.insert(0, str(ROOT))
import channels  # noqa: E402
from channels.base import Channel, NullChannel  # noqa: E402
import chat_push  # noqa: E402
import cards  # noqa: E402

FAILS = []


def check(name, cond, extra=""):
    print(("ok   " if cond else "FAIL ") + name + (f"  {extra}" if extra and not cond else ""))
    if not cond:
        FAILS.append(name)


# ------------------------------------------------------------------ 1. the seam
ch = channels.get()
check("ASSISTANT_CHANNEL=null picks the null channel", isinstance(ch, NullChannel) and ch.name == "null")
check("chat_push reaches the channel through the seam", chat_push._g() is ch)
check("the null channel is 'not configured', so nothing is pushed in tests", not chat_push.enabled())
n = ch.post("spaces/X", "hello **there**\n- one", "")
check("post returns a message name and remembers it", n.startswith("spaces/X/messages/") and ch.get(n)["text"].startswith("hello"))
iface = {m for m in dir(Channel) if not m.startswith("_")}
from channels.gchat import GChat  # noqa: E402
from channels.slack import Slack, render_card  # noqa: E402
check("gchat and slack implement every interface method", iface <= set(dir(GChat)) and iface <= set(dir(Slack)), iface - set(dir(Slack)))
g = GChat()
check("the gchat adapter delegates markup to the live module", g.to_markup("**b**") == "*b*" and g.to_gchat("# H") == "*H*")
check("the gchat adapter still answers the module's other names (pull, parse_click)", callable(getattr(g, "parse_click", None)))
sl = Slack()
check("slack is a stub until a token is stored", not sl.configured() and sl.post("C1", "x") == "")

# ------------------------------------------------------------------ 2. a card body renders in both channels
BODY = "Hi Sara, confirming Saturday 1:30pm. - owner1"
q = {"question": "[EMAIL PREVIEW]\nfrom owner3@example.invalid to sara@example.com\nsubject: Saturday\n\n" + BODY, "header": "Sara prep",
     "lane": "wedding", "audience": "client", "options": [{"label": "Approve"}, {"label": "Skip"}],
     "exec": {"kind": "email_send", "approve_label": "Approve", "payload": {"from": "owner3@example.invalid", "to": "sara@example.com", "subject": "Saturday", "body": BODY}}}
r = cards.post([q], "test")
check("card posts", r["posted"], r)
card = cards._read(cards.LIVE / f"{r['qid']}.json")
body = chat_push.card_body(card, "open")
check("the Chat body is a cardsV2 message (unchanged by the seam)", isinstance(body.get("cardsV2"), list) and body["cardsV2"])
blocks = render_card(body)
kinds = [b["type"] for b in blocks]
check("the same body renders to Slack blocks with a button per option", "actions" in kinds and any(e["text"]["text"] == "Approve" for b in blocks if b["type"] == "actions" for e in b["elements"]), kinds)
btn = next(e for b in blocks if b["type"] == "actions" for e in b["elements"] if e["text"]["text"] == "Approve")
check("a Slack button carries the same {qid, option} the Chat button does", json.loads(btn["value"]).get("qid") == r["qid"] and json.loads(btn["value"]).get("option") == "Approve", btn)

# ------------------------------------------------------------------ 3. the slash surface
import core  # noqa: E402
import bridge  # noqa: E402
import outcomes as oc  # noqa: E402
owner1 = core.people()["users"]["owner1"] | {"key": "owner1"}
a = cards.answer(r["qid"], "Approve")
row = oc.from_exec({"id": a["ticket"], "kind": "email_send", "lane": "email", "approval_ref": f"card:{r['qid']}", "payload": q["exec"]["payload"]}, True, "SENT=1 <m1@x> THREAD=ok")
t = bridge.command(f"/evidence {r['qid']}", owner1)
check("/evidence names the tap, the state and the Message-ID", "Approve" in t and "verified" in t and "<m1@x>" in t, t)
check("/evidence on an unknown card says so", "no answer on record" in bridge.command("/evidence qnope", owner1))
t = bridge.command("/quiet until 11pm", owner1)
check("/quiet until sets a window", t.startswith("Quiet until 11:00 PM") and bridge.is_quiet_until(), t)
check("the ledger honours it", oc._quiet(datetime.now().replace(hour=12)))
check("/quiet off clears it", "back to speaking" in bridge.command("/quiet off", owner1) and not bridge.is_quiet_until())
check("/help lists the new commands", all(x in bridge.command("/help", owner1) for x in ("/open", "/evidence", "/quiet", "/signin", "/switch")))
check("/switch needs an owner", bridge.command("/switch x on", {"key": "guest", "name": "G", "role": "guest"}) is None)

print()
print("ALL PASSED" if not FAILS else f"{len(FAILS)} FAILED: {FAILS}", f"({TMP})")
sys.exit(1 if FAILS else 0)

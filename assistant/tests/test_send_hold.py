"""Offline tests for the conditional timed send (send_hold.py, 2026-10-28): "text example at 4 PM only if he and example
haven't replied" posts the card with `hold_if_reply`, the condition is stored with the card, the tap still mints the
ticket for 4 PM, and right before the drain the send is held (ticket cancelled, one line for the card's thread) when
anything came in from them since the card was made: a text, a mail from their address, or what a plugin reports.
With nothing in, the ticket is left for the drain. The approve deed ("send it at 4 if they haven't replied") stores
it too. The organization's CRM checks are tested with its plugin. Scratch stores; no model, no Chat, nothing sent.
   python assistant/tests/test_send_hold.py
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
TMP = Path(tempfile.mkdtemp(prefix="wv_send_hold_"))
os.environ["ASSISTANT_DATA"] = str(TMP / "adata")
os.environ["ASSISTANT_CARDS"] = str(TMP / "cards")
os.environ["ASSISTANT_LEARNINGS"] = str(TMP / "learnings")
os.environ["RULINGS_FILE"] = str(ROOT / "data" / "rulings.json")
os.environ["DOER_DATA"] = str(TMP / "doer")
os.environ["AGENT_STATE"] = str(TMP / "agent_state")
os.environ["AGENT_DATA"] = str(TMP / "agent_data")
os.environ["AGENT_SUGGESTIONS"] = str(TMP / "sugg.md")
os.environ["CHAT_PUSH"] = "off"
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT / "agent"))
import core  # noqa: E402
import actions  # noqa: E402
import bridge  # noqa: E402
import brain  # noqa: E402
import gmcp  # noqa: E402
import intents  # noqa: E402
import cards  # noqa: E402
import exec_store as es  # noqa: E402
import send_hold  # noqa: E402
import alib  # noqa: E402
alib.plugins = lambda: []          # the engine alone: the CRM half has its own test
import google_api  # noqa: E402
google_api.HOME = TMP / "google"
google_api.HOME.mkdir(parents=True, exist_ok=True)
from alib import DATA, save_json  # noqa: E402

N = 0


def ok(cond, msg):
    global N
    N += 1
    if not cond:
        print(f"FAIL {N}: {msg}")
        sys.exit(1)
    print(f"ok {N}: {msg}")


OWNER_KEY = next(iter(core.owners()))
OWNER = core.person_for(handle=OWNER_KEY)
assert OWNER and core.is_owner(OWNER), "test needs an owner from people.json"
actions.WAKE_DOER = False
bridge.ASYNC = False
THREAD = "spaces/AAQAtest/threads/danny4pm"
example, example_MAIL = "+18055550142", "example@example.com"
ASK = "Text example at 4 PM only if he and example haven't replied"
DAY = (datetime.now() + timedelta(days=1)).strftime("%Y-%m-%d")
SEND_AT = f"{DAY} 16:00"
BEFORE = datetime.strptime(f"{DAY} 15:55", "%Y-%m-%d %H:%M").timestamp()      # the spine pass before the send minute
BODY = "Hey example, just checking you saw the form for Saturday. Happy to help with anything."


# ---------------------------------------------------------------- 1. the deed: hold_if_reply needs a time, and is accepted with one
turn = intents.begin(OWNER, "spaces/AAQAtest", THREAD)
os.environ["AGENT_TURN"] = str(turn)
r = gmcp.call("task", {"lane": "personal", "text": ASK, "draft": {"channel": "text", "to": example, "name": "example", "text": BODY, "hold_if_reply": True}})
ok(r.startswith("refused") and "send_at" in r, "hold_if_reply without a send time is refused")
os.environ.pop("AGENT_TURN", None)


# ---------------------------------------------------------------- 2. end to end through the bridge with the owner's own words
def fake_run(prompt, resume="", model="", timeout=600):
    os.environ["AGENT_TURN"] = brain._TURN["path"]
    ans = gmcp.call("task", {"lane": "personal", "text": ASK,
                             "draft": {"channel": "text", "to": example, "name": "example", "text": BODY, "send_at": SEND_AT, "hold_if_reply": True}})
    assert ans.startswith("accepted") and "only if nothing came in" in ans, ans
    return {"ok": True, "session": "S1", "text": "The text to example is below; after your tap it goes at 4:00 PM, only if they haven't written by then."}


brain._run_claude = fake_run
bridge.handle({"channel": "local", "person": OWNER, "text": ASK, "convo": "spaces/AAQAtest", "thread": THREAD,
               "space": "spaces/AAQAtest", "direct": True})
cs = [cards._read(p) for p in sorted(cards.LIVE.glob("q*.json"))]
ok(len(cs) == 1 and cs[0]["questions"][0]["exec"].get("send_at") == SEND_AT, "the text card is posted with its 4 PM send time")
qid = cs[0]["id"]
conds = json.loads((DATA / "send_conditions.json").read_text(encoding="utf-8"))
ok(qid in conds and conds[qid]["to"] == example and conds[qid]["name"] == "example" and conds[qid]["since"] <= time.time(),
   "the condition is stored with the card, the clock started when the card was made")

# the owner still taps; the tap mints the ticket for 4 PM
a = cards.answer(qid, "Send (Recommended)", by="gchat:" + OWNER_KEY)
tk = es.fold()[a["ticket"]]
ok(a.get("ok") and tk["status"] == "queued" and tk["not_before"] == SEND_AT, "the tap mints the ticket, held until 4 PM")
ok(send_hold.tick(now=time.time()) == {"checked": 0, "held": 0}, "hours before the send minute nothing is checked yet")

# ---------------------------------------------------------------- 3. nothing came in: left for the drain, sent as approved
ok(send_hold.tick(now=BEFORE) == {"checked": 1, "held": 0} and es.fold()[a["ticket"]]["status"] == "queued",
   "no reply: checked and left queued, the drain sends it at 4")


# ---------------------------------------------------------------- 4. each kind of reply holds it
def fresh_ts(fmt="%Y-%m-%dT%H:%M:%S"):
    return time.strftime(fmt, time.localtime(time.time() + 2))


rec = dict(conds[qid], to=example)
save_json(DATA / "phone_threads.json", {example: {"messages": [{"ts": "2020-01-01T09:00:00", "dir": "in", "text": "old one"}]}})
ok(send_hold.evidence(rec) == "", "a text from before the card was made does not count")
save_json(DATA / "tickets.json", {"m1": {"kind": "email", "created": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() + 2)),
                                         "payload": {"from": f"example <{example_MAIL}>", "subject": "Re: Saturday"}}})
ok("emailed" in send_hold.evidence(dict(rec, to=example_MAIL)), "a mail from their address counts (any inbox the mail sensor reads)")
save_json(DATA / "tickets.json", {"i1": {"kind": "imessage_in", "created": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() + 2)),
                                         "payload": {"from": example, "text": "On it"}}})
ok("texted" in send_hold.evidence(rec), "an inbound text ticket from their number counts")
save_json(DATA / "tickets.json", {})
alib.plugins = lambda: [type("P", (), {"reply_evidence": staticmethod(lambda r, w, t: f"{r['who']} answered in the client service")})]
ok(send_hold.evidence(rec) == "example answered in the client service", "a plugin's reply_evidence is asked last, and counts")
alib.plugins = lambda: []

save_json(DATA / "phone_threads.json", {example: {"messages": [{"ts": fresh_ts(), "dir": "in", "text": "Yes! Filling it out tonight"}]}})
ok(send_hold.tick(now=BEFORE) == {"checked": 1, "held": 1}, "example texted back: the send is held")
tk = es.fold()[a["ticket"]]
ok(tk["status"] == "cancelled" and "texted" in tk["result"] and "Filling it out" in tk["result"], "the ticket is cancelled, never sent, the reason on it")
ok(json.loads((DATA / "send_conditions.json").read_text(encoding="utf-8"))[qid]["resolved"] == "held", "the condition is closed as held")
ok(send_hold.tick(now=BEFORE) == {"checked": 0, "held": 0}, "a held send is not checked again")

# ---------------------------------------------------------------- 5. "send it at 4 if they haven't replied" in a card's thread
save_json(DATA / "phone_threads.json", {})
c2 = cards.post([{"header": "Text to example", "lane": "personal", "question": f"Text example? [TEXT PREVIEW] To example {example}: {BODY} Again.",
                  "options": [{"label": "Send (Recommended)"}, {"label": "Skip"}],
                  "exec": {"kind": "imessage_send", "approve_label": "Send (Recommended)",
                           "payload": {"to": example, "body": BODY + " Again.", "account": actions._phone_account(OWNER)}}}],
                owner=OWNER_KEY, asked=True)
q2 = c2.get("qid") or c2.get("id")
_c = cards._read(cards.LIVE / f"{q2}.json")
_c["chat"] = {OWNER_KEY: "spaces/AAQAtest/messages/approve4.approve4"}
cards._write(cards.LIVE / f"{q2}.json", _c)
APPROVE = "Send it at 4 if they haven't replied"


def fake_run_approve(prompt, resume="", model="", timeout=600):
    os.environ["AGENT_TURN"] = brain._TURN["path"]
    ans = gmcp.call("approve", {"at": "16:00" if datetime.now().hour < 16 else SEND_AT, "hold_if_reply": True, "words": APPROVE})
    assert ans.startswith("accepted") and "only if nothing came in" in ans, ans
    return {"ok": True, "session": "S2", "text": "Approved; it goes at 4:00 PM unless they write first."}


brain._run_claude = fake_run_approve
bridge.handle({"channel": "local", "person": OWNER, "text": APPROVE, "convo": "spaces/AAQAtest",
               "thread": "spaces/AAQAtest/threads/approve4", "space": "spaces/AAQAtest", "direct": True})
conds = json.loads((DATA / "send_conditions.json").read_text(encoding="utf-8"))
tk2 = [t for t in es.fold().values() if t.get("approval_ref") == f"card:{q2}"]
ok(q2 in conds and conds[q2]["to"] == example and conds[q2]["name"] == "example", "the approve deed stores the condition for that card")
ok(len(tk2) == 1 and tk2[0]["status"] == "queued" and tk2[0]["not_before"].endswith("16:00"), "and the card is approved for 4 PM on his words")

# ---------------------------------------------------------------- 6. the prompts, the spine order, the README
for f in ("prompt.md", "templates/prompt.generic.md"):
    ok("hold_if_reply" in (ROOT / "agent" / f).read_text(encoding="utf-8"), f"{f}: a conditional timed send uses hold_if_reply")
names = [j["name"] for j in json.loads((ROOT / "spine_jobs.json").read_text(encoding="utf-8"))["jobs"] if j.get("group") == "long"]
ok("send-hold" in names and names.index("send-hold") < names.index("drain"), "the check runs in the long group right before the drain")
ok("send_hold.py" in (ROOT / "README.md").read_text(encoding="utf-8"), "the README says how it works")
print(f"\nall {N} checks passed")

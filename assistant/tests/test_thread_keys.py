"""test_thread_keys.py - one Chat thread per subject, named by its header (the owner 2026-11-04:
people were lost in the hundreds of threads the agents opened). A fake Chat API, a scratch store,
an org.json with two subjects; nothing posted.   python assistant/tests/test_thread_keys.py
"""
import os as _os, tempfile as _tf
_os.environ.setdefault("WV_LOG_DIR", _tf.mkdtemp(prefix="wv_logs_"))
import json
import os
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
TMP = Path(tempfile.mkdtemp(prefix="wv_tk_"))
os.environ["ASSISTANT_DATA"] = str(TMP / "adata")
os.environ["AGENT_STATE"] = str(TMP / "agent_state")
os.environ["AGENT_DATA"] = str(TMP / "agent_data")
ORG = TMP / "org.json"
ORG.write_text(json.dumps({
    "name": "example-org", "chat_thread_keys": True,
    "chat_thread_subjects": [
        {"name": "ticket", "regex": r"\b(\d{6}-\d{1,2})\b", "days": 30, "label": "WO {0}"},
        {"name": "unit", "regex": r"\b([A-D]\d{2})\b", "days": 7, "label": "{0}"}]}), encoding="utf-8")
os.environ["ASSISTANT_ORG"] = str(ORG)
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "agent"))
import core  # noqa: E402
import gchat  # noqa: E402
import thread_keys as tk  # noqa: E402

(core.DATA).mkdir(parents=True, exist_ok=True)
core.save(core.DATA / "gchat_spaces.json", {"owner1": "spaces/DM"})
N = 0
FAILS = []


def ok(cond, msg, got=""):
    global N
    N += 1
    print(("ok   " if cond else "FAIL ") + f"{N}: {msg}" + ("" if cond else f"  got={got!r}"))
    if not cond:
        FAILS.append(msg)


CALLS = []
C = {"i": 0}


def fake_req(url, body=None, method="", idempotent=False):
    C["i"] += 1
    CALLS.append({"url": url, "body": json.loads(json.dumps(body)) if body is not None else None, "method": method})
    if method == "PATCH":
        return {"name": url.split("?")[0].split("/v1/")[-1]}
    th = (body or {}).get("thread") or {}
    return {"name": f"spaces/G/messages/M{C['i']}", "thread": {"name": th.get("name") or f"spaces/G/threads/T{C['i']}"}}


gchat._req = fake_req
gchat.mentionize = lambda text, space="": text
SPACE = "spaces/G"

# keys and titles
ok(tk.base_key("B43 faucet, WO 184741-5", "sage") == ("ticket-184741-5", 30), "the first subject in order wins")
ok(tk.base_key("Sam, B43 faucet is leaking", "sage") == ("unit-B43", 7), "the next subject")
d = tk.base_key("Good morning", "sage")
ok(d[0].startswith("day-sage-20") and d[1] == 1, "nothing named = the agent's day", d)
t = tk.title_for("B43 faucet leaking, WO 184741-5. Can you look today?", "sage", "Sage")
ok(t.startswith("WO 184741-5 · B43 · faucet leaking") and t.count("184741-5") == 1, "the header names the subjects then the topic", t)
ok(tk.title_for("Good morning", "sage", "Sage").startswith("Sage · "), "the day header")
ok("<users/" not in tk.title_for("<users/1> B43 keys", "sage"), "mentions are stripped from the header")

# header + reply, then one call
CALLS.clear()
name = gchat.post(SPACE, "B43 faucet leaking, WO 184741-5")
ok(len(CALLS) == 2 and CALLS[0]["body"]["thread"] == {"threadKey": "ticket-184741-5"}
   and CALLS[0]["body"]["text"].startswith("WO 184741-5 · B43") and "REPLY_MESSAGE_FALLBACK_TO_NEW_THREAD" in CALLS[0]["url"],
   "the first post sends the header with the threadKey", CALLS)
ok(CALLS[1]["body"]["thread"] == {"name": "spaces/G/threads/T1"} and name == "spaces/G/messages/M2",
   "the message replies in the header's thread", CALLS)
CALLS.clear()
gchat.post(SPACE, "Any update on WO 184741-5?")
ok(len(CALLS) == 1 and CALLS[0]["body"]["thread"] == {"name": "spaces/G/threads/T1"}, "the second post: no header, same thread", CALLS)
ent = tk.Registry().dump()[SPACE]["ticket-184741-5"]
ok(ent["thread"] == "spaces/G/threads/T1" and ent["header"] == "spaces/G/messages/M1" and ent["n"] == 2, "the registry", ent)

# cards, the day thread, explicit thread, DM, off
CALLS.clear()
gchat.post_body(SPACE, {"cardsV2": [{"cardId": "c1", "card": {"header": {"title": "Done?"},
                "sections": [{"widgets": [{"textParagraph": {"text": "C14 paint, WO 190001-1: done?"}}]}]}}]})
ok(len(CALLS) == 2 and CALLS[0]["body"]["thread"] == {"threadKey": "ticket-190001-1"}, "a card is keyed by its words", CALLS)
CALLS.clear()
gchat.post(SPACE, "Good morning, on the property yet?")
gchat.post(SPACE, "Readback: 3 jobs done")
ok(len(CALLS) == 3 and CALLS[0]["body"]["thread"]["threadKey"].startswith("day-sage-")
   and CALLS[2]["body"]["thread"] == CALLS[1]["body"]["thread"], "unkeyed posts share the agent's day thread", CALLS)
CALLS.clear()
gchat.post(SPACE, "B43 reply", "spaces/G/threads/HUMAN")
ok(len(CALLS) == 1 and CALLS[0]["body"]["thread"] == {"name": "spaces/G/threads/HUMAN"}, "an explicit thread is untouched", CALLS)
CALLS.clear()
gchat.post("spaces/DM", "B43 faucet")
ok(len(CALLS) == 1 and "thread" not in CALLS[0]["body"] and CALLS[0]["url"].endswith("/messages"), "a DM is never keyed", CALLS)
ORG.write_text(json.dumps({"name": "example-org", "chat_thread_keys": False}), encoding="utf-8")
os.utime(ORG, (0, 0))
CALLS.clear()
gchat.post(SPACE, "B53 keys")
ok(len(CALLS) == 1 and "thread" not in CALLS[0]["body"], "chat_thread_keys=false posts unthreaded", CALLS)
ORG.write_text(json.dumps({"name": "example-org", "chat_thread_keys": True, "chat_thread_subjects": [
    {"name": "ticket", "regex": r"\b(\d{6}-\d{1,2})\b", "days": 30, "label": "WO {0}"},
    {"name": "unit", "regex": r"\b([A-D]\d{2})\b", "days": 7, "label": "{0}"}]}), encoding="utf-8")

# no subjects configured = the day thread only
ORG2 = TMP / "org2.json"
ORG2.write_text(json.dumps({"name": "x", "chat_thread_keys": True}), encoding="utf-8")
import orgcfg  # noqa: E402
orgcfg.FILE = ORG2
orgcfg._CACHE["mtime"] = None
ok(tk.base_key("B43 WO 184741-5", "sage")[0].startswith("day-"), "an organization with no subjects keys by day")
orgcfg.FILE = ORG
orgcfg._CACHE["mtime"] = None

# aging
CALLS.clear()
gchat.post(SPACE, "A21 keys at the office")
r = tk.Registry()
old = (datetime.now(timezone.utc) - timedelta(days=8)).strftime("%Y-%m-%dT%H:%M:%SZ")
r._load()[SPACE]["unit-A21"]["last"] = old
r._save()
CALLS.clear()
gchat.post(SPACE, "A21 move-in Thursday")
e2 = tk.Registry().dump()[SPACE]["unit-A21"]
ok(len(CALLS) == 2 and CALLS[0]["body"]["thread"]["threadKey"].startswith("unit-A21-") and e2["gen"] == 2,
   "an idle subject thread gets a new generation", (CALLS, e2))

# adoption and titles
ok(gchat.adopt_thread("", "spaces/G/threads/SAM", "C05 is painted"), "adopt a person's thread about C05")
CALLS.clear()
gchat.post(SPACE, "C05: cleaners booked")
ok(len(CALLS) == 1 and CALLS[0]["body"]["thread"] == {"name": "spaces/G/threads/SAM"}, "the agent follows there, no header", CALLS)
ok(not gchat.adopt_thread("", "spaces/DM/threads/X", "C05 done"), "a DM thread is not adopted")
CALLS.clear()
ok(gchat.thread_title(SPACE, "ticket-184741-5", mark="✅ done") and CALLS[0]["method"] == "PATCH"
   and "/messages/M1?updateMask=text" in CALLS[0]["url"] and CALLS[0]["body"]["text"].startswith("✅ done WO 184741-5"),
   "a mark edits the header in place", CALLS)
CALLS.clear()
gchat.thread_title(SPACE, "184741-5", mark="✅ done")
ok(CALLS and CALLS[0]["body"]["text"].count("✅") == 1, "a mark never stacks; a number finds its thread", CALLS)
CALLS.clear()
ok(gchat.thread_title(SPACE, "WO 184741-5", title="WO 184741-5 · B43 · faucet replaced")
   and CALLS[0]["body"]["text"] == "✅ done WO 184741-5 · B43 · faucet replaced", "a retitle keeps the mark", CALLS)
ok(not gchat.thread_title(SPACE, "unit-C05", mark="✅"), "an adopted thread has no header to edit")

print("\n" + ("ALL PASS" if not FAILS else f"{len(FAILS)} FAILED: {FAILS}"))
sys.exit(1 if FAILS else 0)

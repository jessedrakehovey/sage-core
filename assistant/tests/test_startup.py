"""Offline tests for the start-up sequence in Chat (scripts/startup.py, 2026-11-04): the one "Getting set up" card,
its run in order, the stop at a login the vault lacks, the offer on first contact, and the chat phrases that bring it.
Scratch stores; no browser, no Google.   python assistant/tests/test_startup.py
"""
import os as _os, tempfile as _tf
_os.environ.setdefault("WV_LOG_DIR", _tf.mkdtemp(prefix="wv_logs_"))   # never the live ~/.claude logs
import json
import os
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
TMP = Path(tempfile.mkdtemp(prefix="wv_startup_"))
os.environ["ASSISTANT_DATA"] = str(TMP / "adata")
os.environ["ASSISTANT_CARDS"] = str(TMP / "cards")
os.environ["AGENT_STATE"] = str(TMP / "agent_state")
os.environ["AGENT_DATA"] = str(TMP / "agent_data")
os.environ["AGENT_HOME_SPACE"] = ""
os.environ["STARTUP_STATE"] = str(TMP / "startup_state.json")
os.environ["HOME_TAB_CACHE"] = str(TMP / "home_cache.json")
os.environ["ASSISTANT_CHANNEL"] = "null"
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT / "doer"))
sys.path.insert(0, str(ROOT / "agent"))
import startup  # noqa: E402
import chat_forms  # noqa: E402
import gchat  # noqa: E402
import gchat_events  # noqa: E402
import core  # noqa: E402

FAILS = []


def check(name, cond, extra=""):
    print(("ok   " if cond else "FAIL ") + name + (f"  {extra}" if extra and not cond else ""))
    if not cond:
        FAILS.append(name)


def widgets(body):
    return [w for s in body["cardsV2"][0]["card"]["sections"] for w in s["widgets"]]


def buttons(body):
    return [b for w in widgets(body) if "buttonList" in w for b in w["buttonList"]["buttons"]]


def params(b):
    return {p["key"]: p["value"] for p in b["onClick"]["action"]["parameters"]}


# a two-job sequence, independent of the organization's own registry
ROWS = [{"id": "job-a", "name": "The first setup", "setup": {"kind": "job", "startup": True, "label": "Do A", "how": "I do the first thing myself. More words."}, "check": {"type": "x"}},
        {"id": "job-b", "name": "The second setup", "setup": {"kind": "job", "startup": True, "label": "Do B", "how": "I do the second thing myself."}, "check": {"type": "x"}},
        {"id": "plain", "name": "Not a start-up job", "setup": {"kind": "job", "label": "x", "how": "x"}},
        {"id": "form", "name": "A form row", "setup": {"kind": "text", "how": "x"}}]
startup._registry = lambda: ROWS
done = set()
startup._passes = lambda row: row["rid"] in done

# ------------------------------------------------------------------ 1. the sequence and the card
check("only job rows flagged startup are in the sequence, in registry order", [r["rid"] for r in startup.rows()] == ["job-a", "job-b"])
c = startup.card()
txt = json.dumps(c, ensure_ascii=False)
check("the card: a row per job with what it does, '0 of 2 done', ONE filled button, nothing about a computer to go to",
      c["cardsV2"][0]["card"]["header"]["subtitle"] == "0 of 2 done" and "The first setup" in txt and "I do the first thing myself." in txt
      and [b["text"] for b in buttons(c)] == ["Set up everything"] and params(buttons(c)[0]) == {"rid": "startup", "do": "run"} and buttons(c)[0].get("color")
      and "PC screen" not in txt and "terminal" not in txt.lower())

# ------------------------------------------------------------------ 2. the run: in order, the card after each step
ran, updates = [], []
startup._run_row = lambda rid, by: (ran.append(rid) or done.add(rid) or {"ok": True, "state": "ok", "message": f"{rid}: done."})
res = startup.run_all("owner", updates.append)
subs = [u["cardsV2"][0]["card"]["header"]["subtitle"] for u in updates]
check("run: both jobs in order; the card shows working, then working, then 2 of 2 done", ran == ["job-a", "job-b"] and subs == ["working", "working", "2 of 2 done"], repr(subs))
check("the last card has no button and says it all happens in chat", not buttons(updates[-1]) and "happens in this chat" in json.dumps(updates[-1]))
check("a second run has nothing to do", startup.pending() == [] and (ran.clear() or startup.run_all("owner", updates.append) == {}) and ran == [])

# ------------------------------------------------------------------ 3. a login the vault lacks stops the sequence at that job
done.clear()
ran.clear()
updates.clear()


def stuck_run(rid, by):
    ran.append(rid)
    if rid == "job-a":
        return {"ok": False, "state": "needs_login", "site": "accounts.example.com", "message": "accounts.example.com wants a sign-in and my vault has no login for it."}
    done.add(rid)
    return {"ok": True, "state": "ok", "message": "done"}


startup._run_row = stuck_run
startup.run_all("owner", updates.append)
last = updates[-1]
bl = buttons(last)
check("a job that needs a login stops the sequence there; the card offers Enter login (the vault dialog, tied to the sequence) and Try again",
      ran == ["job-a"] and [b["text"] for b in bl] == ["Enter login", "Try again"] and bl[0]["onClick"]["action"]["function"] == "vault_add"
      and bl[0]["onClick"]["action"].get("interaction") == "OPEN_DIALOG" and params(bl[0]) == {"site": "accounts.example.com", "rid": "startup"}
      and "🔑" in json.dumps(last, ensure_ascii=False), json.dumps(last, ensure_ascii=False)[:300])

# ------------------------------------------------------------------ 4. the offer: once, where the owner is
posts = []
gchat.post_body = lambda space, body, thread="": posts.append((space, body, thread)) or f"{space}/messages/s{len(posts)}"
check("offer: posted when something is to do", startup.offer("spaces/DM1") and posts and posts[0][0] == "spaces/DM1")
check("offer: not a second time by itself", not startup.offer("spaces/DM1") and len(posts) == 1)
check("offer: again when the owner asks (force)", startup.offer("spaces/DM1", force=True) and len(posts) == 2)
done.update({"job-a", "job-b"})
os.remove(os.environ["STARTUP_STATE"])
check("offer: nothing to do, nothing posted", not startup.offer("spaces/DM2") and len(posts) == 2)
done.clear()

# ------------------------------------------------------------------ 5. Chat: the phrases, first contact, the taps
check("'finish setting up' / 'set everything up' / 'get me set up' bring the start-up card; a named job still brings its own",
      chat_forms.match("can you finish setting up") == "startup" and chat_forms.match("set everything up") == "startup"
      and chat_forms.match("get me set up") == "startup" and chat_forms.match("add a form question") == "")
check("chat_forms.card('startup') is the sequence's card", chat_forms.card("startup")["cardsV2"][0]["cardId"] == "startup")
gchat_events._core = lambda: core
_person = {"key": "owner", "name": "Owner", "role": "owner"}
core.person_for = lambda handle="", email="", chat_user="": _person
core.can = lambda person, what: True
gchat.record_added = lambda a: "owner"
os.remove(os.environ["STARTUP_STATE"]) if os.path.exists(os.environ["STARTUP_STATE"]) else None
posts.clear()
added = {"chat": {"user": {"email": "owner@example.com", "name": "users/1"}, "addedToSpacePayload": {"space": {"name": "spaces/NEWDM", "type": "DM", "spaceType": "DIRECT_MESSAGE"}}}}
r = gchat_events.handle(added)
check("first contact: the owner adds the agent and the start-up card arrives in that DM by itself",
      "Connected" in json.dumps(r) and posts and posts[0][0] == "spaces/NEWDM" and posts[0][1]["cardsV2"][0]["cardId"] == "startup", json.dumps(r)[:160] + repr(posts)[:120])
patched = []
gchat.patch_body = lambda name, body: patched.append((name, body)) or True
started = []
gchat_events._run_startup = lambda by, on_update: (started.append(by) or on_update(startup.card(running="job-a")) or True)
tap = {"relayFunction": "form_action", "chat": {"user": {"email": "owner@example.com"}, "buttonClickedPayload": {"message": {"name": "spaces/NEWDM/messages/s1", "space": {"name": "spaces/NEWDM"}}}},
       "commonEventObject": {"parameters": {"rid": "startup", "do": "run"}}}
r = gchat_events.handle(tap)
check("Set up everything: answered at once with the working card; the sequence runs and edits the same message",
      started == ["owner"] and "updateMessageAction" in json.dumps(r) and "working" in json.dumps(r) and patched and patched[-1][0] == "spaces/NEWDM/messages/s1")
gchat_events._vault_add = lambda fields: "ADDED vault accounts.example.com owner@example.com totp=no"
started.clear()
vsub = {"chat": {"user": {"email": "owner@example.com"}, "buttonClickedPayload": {"message": {"name": "spaces/NEWDM/messages/s1", "space": {"name": "spaces/NEWDM"}}}},
        "commonEventObject": {"parameters": {"site": "accounts.example.com", "rid": "startup"}, "dialogEventType": "SUBMIT_DIALOG",
                              "formInputs": {"site": {"stringInputs": {"value": ["accounts.example.com"]}}, "username": {"stringInputs": {"value": ["owner@example.com"]}},
                                             "password": {"stringInputs": {"value": ["hunter2"]}}, "totp": {"stringInputs": {"value": [""]}}}},
        "relayFunction": "vault_add_submit"}
r = gchat_events.handle(vsub)
check("Enter login from the start-up card: saved, the dialog closes, the sequence carries on by itself",
      "CLOSE_DIALOG" in json.dumps(r) and "Carrying on with the setup" in json.dumps(r) and started == ["owner"], json.dumps(r)[:200])

# ------------------------------------------------------------------ 6. the organization's own registry carries the two jobs
import importlib  # noqa: E402
import setup_actions  # noqa: E402
real = [r for r in setup_actions._registry() if (r.get("setup") or {}).get("startup")]
check("this organization's registry flags its start-up jobs (the Chat app's Home tab, the screenshots drive)",
      {r["id"] for r in real} >= {"chat-home", "shots-drive"} and all((r["setup"].get("kind") in ("job", "job_form")) for r in real))
check("every start-up job ships with a new pack", all(r["id"] in __import__("new_org").GENERIC_CONNECTIONS for r in real))
check("no start-up job's words send the owner to a computer",
      not any(w in (str(r["setup"].get("how")) + str(r.get("renew"))).lower() for r in real for w in ("pc screen", "on the pc", "terminal", "console window")))

print()
print("ALL PASSED" if not FAILS else f"{len(FAILS)} FAILED: {FAILS}", f" ({TMP})")
sys.exit(1 if FAILS else 0)

"""Offline tests for Control PC from chat (2026-11-04): the administrator helper's allow-list and PIN hash
(scripts/elevated.py), the PIN flow (scripts/remote_desktop.py), the form row in Chat (setup kind job_form), its place
in the start-up sequence. Scratch stores; nothing is elevated, installed or registered.
   python assistant/tests/test_remote_desktop.py
"""
import os as _os, tempfile as _tf
_os.environ.setdefault("WV_LOG_DIR", _tf.mkdtemp(prefix="wv_logs_"))   # never the live ~/.claude logs
import base64
import json
import os
import sys
import tempfile
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
TMP = Path(tempfile.mkdtemp(prefix="wv_rdp_"))
os.environ["ASSISTANT_DATA"] = str(TMP / "adata")
os.environ["ASSISTANT_CARDS"] = str(TMP / "cards")
os.environ["AGENT_STATE"] = str(TMP / "agent_state")
os.environ["AGENT_DATA"] = str(TMP / "agent_data")
os.environ["AGENT_HOME_SPACE"] = ""
os.environ["ELEVATED_DIR"] = str(TMP / "elevated")
os.environ["REMOTE_DESKTOP_STATE"] = str(TMP / "remote_desktop_state.json")
os.environ["STARTUP_STATE"] = str(TMP / "startup_state.json")
os.environ["ASSISTANT_CHANNEL"] = "null"
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT / "doer"))
sys.path.insert(0, str(ROOT / "agent"))
import elevated  # noqa: E402
import remote_desktop  # noqa: E402
import chat_forms  # noqa: E402
import chatfirst  # noqa: E402
import gchat  # noqa: E402
import gchat_events  # noqa: E402
import startup  # noqa: E402
import core  # noqa: E402

FAILS = []


def check(name, cond, extra=""):
    print(("ok   " if cond else "FAIL ") + name + (f"  {extra}" if extra and not cond else ""))
    if not cond:
        FAILS.append(name)


def buttons(body):
    return [b for s in body["cardsV2"][0]["card"]["sections"] for w in s["widgets"] if "buttonList" in w for b in w["buttonList"]["buttons"]]


def params(b):
    return {p["key"]: p["value"] for p in b["onClick"]["action"]["parameters"]}


# ------------------------------------------------------------------ 1. the helper: the PIN hash, the allow-list, the request file
h = elevated.pin_hash("host-id-1", "123456")
check("the PIN hash is Chrome's form: hmac: + base64 of a SHA-256 HMAC keyed by the host id",
      h.startswith("hmac:") and len(base64.b64decode(h[5:])) == 32 and h == elevated.pin_hash("host-id-1", "123456")
      and h != elevated.pin_hash("host-id-2", "123456") and h != elevated.pin_hash("host-id-1", "654321"))
elevated.installed = lambda: False
check("no helper yet: a request says so and writes nothing", elevated.request("crd_status").get("state") == "no_helper" and not elevated.REQ.exists())
check("an action off the list is refused before anything else", not elevated.request("format_disk").get("ok"))
# the elevated side, on a scratch host file
elevated.CRD_DATA = TMP / "crd"
elevated.CRD_DATA.mkdir()
(elevated.CRD_DATA / "host.json").write_text(json.dumps({"host_id": "host-id-1", "host_owner": "owner@example.com", "host_name": "the-pc", "host_secret_hash": "hmac:old"}), encoding="utf-8")
elevated._restart_host = lambda: ""
elevated.DIR.mkdir(parents=True, exist_ok=True)
elevated.REQ.write_text(json.dumps({"nonce": "n1", "action": "crd_pin", "args": {"pin": "246810"}, "ts": time.time()}), encoding="utf-8")
rc = elevated.serve()
res = json.loads(elevated.RES.read_text(encoding="utf-8"))
host = json.loads((elevated.CRD_DATA / "host.json").read_text(encoding="utf-8"))
check("serve crd_pin: the host file gets the new hash, the old file is kept as a backup, the result names the owner, the request (with the PIN) is gone",
      rc == 0 and res["ok"] and res["nonce"] == "n1" and host["host_secret_hash"] == elevated.pin_hash("host-id-1", "246810") and res["owner"] == "owner@example.com"
      and (elevated.CRD_DATA / "host.json.bak").exists() and not elevated.REQ.exists() and "246810" not in json.dumps(res), json.dumps(res))
elevated.REQ.write_text(json.dumps({"nonce": "n2", "action": "crd_pin", "args": {"pin": "12"}, "ts": time.time()}), encoding="utf-8")
elevated.serve()
check("serve refuses a PIN of the wrong shape", not json.loads(elevated.RES.read_text(encoding="utf-8"))["ok"])
elevated.REQ.write_text(json.dumps({"nonce": "n3", "action": "run_anything", "args": {"cmd": "calc"}, "ts": time.time()}), encoding="utf-8")
elevated.serve()
check("serve refuses anything off the allow-list", json.loads(elevated.RES.read_text(encoding="utf-8")) == {"ok": False, "message": "not an allowed action", "nonce": "n3"})
elevated.REQ.write_text(json.dumps({"nonce": "n4", "action": "crd_pin", "args": {"pin": "246810"}, "ts": time.time() - 900}), encoding="utf-8")
check("serve ignores a stale request", elevated.serve() == 1 and not elevated.REQ.exists())

# ------------------------------------------------------------------ 2. the PIN flow
check("a PIN that is not 6-12 digits is turned away in plain words", remote_desktop.set_pin("12ab")["state"] == "failed")
r = remote_desktop.set_pin("246 810", by="owner")
check("no helper: needs_admin, with the one button that raises Windows' own prompt, and no step at a computer in the words",
      r["state"] == "needs_admin" and r["action"] == {"label": "Ask Windows now", "do": "admin"} and not chatfirst.violations(r["message"]), r["message"])
asked = []
elevated.install = lambda wait_s=150: (asked.append(1) or {"ok": False, "message": "not answered"})
r = remote_desktop.on_action("admin", by="owner")
check("Ask Windows now, not approved: the same card again, plainly", asked == [1] and r["state"] == "needs_admin" and r["action"]["do"] == "admin" and not chatfirst.violations(r["message"]))
calls = []


def fake_request(action, args=None, timeout=180):
    calls.append((action, dict(args or {})))
    if action == "crd_status":
        return {"ok": True, "installed": True, "registered": True, "has_pin": True, "owner": "owner@example.com", "host_name": "the-pc"}
    if action == "crd_pin":
        return {"ok": True, "message": "PIN set", "owner": "owner@example.com", "host_name": "the-pc"}
    return {"ok": False, "message": "unexpected"}


elevated.install = lambda wait_s=150: {"ok": True, "message": "installed"}
elevated.installed = lambda: True
elevated.request = fake_request
r = remote_desktop.on_action("admin", by="owner")
st = json.loads(Path(os.environ["REMOTE_DESKTOP_STATE"]).read_text(encoding="utf-8"))
check("approved: the kept PIN is set by the helper without asking again; the card says it is ready and whose account it is under",
      r["ok"] and calls == [("crd_status", {}), ("crd_pin", {"pin": "246810"})] and "ready" in r["message"] and "owner@example.com" in r["message"]
      and st["pin_set_at"] and st["owner"] == "owner@example.com" and "246810" not in json.dumps(st) and remote_desktop._kept() == "", r["message"])
calls.clear()
r = remote_desktop.set_pin("135790", by="owner")
check("with the helper in place a new PIN is one form and done", r["ok"] and calls[-1] == ("crd_pin", {"pin": "135790"}))
remote_desktop._keep("999999")
remote_desktop._PENDING["exp"] = time.time() - 1
check("a PIN is held in memory ten minutes at most", remote_desktop._kept() == "" and remote_desktop._apply()["state"] == "failed")

# ------------------------------------------------------------------ 3. the form in Chat
s = chat_forms.spec("remote-desktop")
check("the registry row is a job that needs a typed value: one PIN field, handled by remote_desktop.set_pin",
      s and s["store"] == "job_form" and s["fields"] == [("pin", "PIN (6 or more digits)")] and (s["module"], s["func"]) == ("remote_desktop", "set_pin"))
c = chat_forms.card("remote-desktop")
b = buttons(c)
check("its card: one button that opens the form on top of chat", [x["text"] for x in b] == ["Set the Control PC PIN"] and b[0]["onClick"]["action"]["function"] == "form_open"
      and b[0]["onClick"]["action"].get("interaction") == "OPEN_DIALOG" and not chatfirst.violations(json.dumps(c, ensure_ascii=False)))
c2 = chat_forms.job_card("remote-desktop", result={"ok": False, "state": "needs_admin", "message": remote_desktop.NEEDS_ADMIN, "action": remote_desktop.ASK})
b2 = buttons(c2)
check("a needs_admin result: Ask Windows now first (a tap that acts), the form again second",
      [x["text"] for x in b2] == ["Ask Windows now", "Set the Control PC PIN"] and params(b2[0]) == {"rid": "remote-desktop", "do": "admin"}
      and b2[1]["onClick"]["action"]["function"] == "form_open" and not chatfirst.violations(json.dumps(c2, ensure_ascii=False)))
check("'I forgot my Control PC PIN' and 'set up remote desktop' bring the form; 'pin this message' does not",
      chat_forms.match("I forgot my control pc pin") == "remote-desktop" and chat_forms.match("set up remote desktop") == "remote-desktop"
      and chat_forms.match("pin this message") == "" and chat_forms.match("set a reminder for the desktop cleanup") == "")

gchat_events._core = lambda: core
_person = {"key": "owner", "name": "Owner", "role": "owner"}
core.person_for = lambda handle="", email="", chat_user="": _person
core.can = lambda person, what: True
patched, logs, got = [], [], []
gchat.patch_body = lambda name, body: patched.append((name, body)) or True
gchat_events._job_form_call = lambda spec, what, values, by: (got.append((what, dict(values), by)) or {"ok": True, "state": "ok", "message": "Control PC is ready: the PIN is set on this computer."})
sub = {"relayFunction": "form_submit", "chat": {"user": {"email": "owner@example.com"}, "buttonClickedPayload": {"message": {"name": "spaces/DM1/messages/w1", "space": {"name": "spaces/DM1"}}}},
       "commonEventObject": {"parameters": {"rid": "remote-desktop"}, "dialogEventType": "SUBMIT_DIALOG", "formInputs": {"pin": {"stringInputs": {"value": ["246810"]}}}}}
r = gchat_events.handle(sub, log=logs.append)
for _ in range(30):
    if patched:
        break
    time.sleep(0.1)
check("the form's submit: the dialog closes at once, the value goes to the row's module, the card is edited with the result",
      "CLOSE_DIALOG" in json.dumps(r) and got == [("submit", {"pin": "246810"}, "owner")] and patched and patched[-1][0] == "spaces/DM1/messages/w1"
      and "ready" in json.dumps(patched[-1][1]), json.dumps(r)[:200])
check("the PIN is in no log line and in no card", "246810" not in " ".join(logs) and "246810" not in json.dumps(patched))
empty = dict(sub, commonEventObject=dict(sub["commonEventObject"], formInputs={"pin": {"stringInputs": {"value": [""]}}}))
check("an empty box keeps the dialog open", "pushCard" in json.dumps(gchat_events.handle(empty)))
patched.clear()
got.clear()
act = {"relayFunction": "form_action", "chat": {"user": {"email": "owner@example.com"}, "buttonClickedPayload": {"message": {"name": "spaces/DM1/messages/w1", "space": {"name": "spaces/DM1"}}}},
       "commonEventObject": {"parameters": {"rid": "remote-desktop", "do": "admin"}}}
r = gchat_events.handle(act)
for _ in range(30):
    if patched:
        break
    time.sleep(0.1)
check("Ask Windows now: answered at once with the working card; the module's on_action runs and the card follows",
      "Working on it" in json.dumps(r) and got == [("admin", {}, "owner")] and patched)

# ------------------------------------------------------------------ 4. the start-up sequence carries it, with its own form button
row = next((x for x in startup.rows() if x["rid"] == "remote-desktop"), None)
check("Control PC is a step of the start-up sequence (a typed one)", row and row["kind"] == "job_form")
startup._passes = lambda r: r["rid"] != "remote-desktop"
card = startup.card()
sb = buttons(card)
check("the start-up card: the PIN step has its own form button (opened from the sequence), and nothing to run by itself",
      [x["text"] for x in sb] == ["Set the Control PC PIN"] and params(sb[0]) == {"rid": "remote-desktop", "from": "startup"}
      and sb[0]["onClick"]["action"]["function"] == "form_open", json.dumps([x["text"] for x in sb]))
ran = []
startup._run_row = lambda rid, by: ran.append(rid) or {"ok": True}
startup.run_all("owner", lambda body: None)
check("Set up everything never runs a step that needs a typed value", ran == [])
check("the row ships with every new pack", "remote-desktop" in __import__("new_org").GENERIC_CONNECTIONS)

print()
print("ALL PASSED" if not FAILS else f"{len(FAILS)} FAILED: {FAILS}", f" ({TMP})")
sys.exit(1 if FAILS else 0)

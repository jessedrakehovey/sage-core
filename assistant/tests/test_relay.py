"""sage-core (2026-10-28): card taps without Tailscale, passwords through a Chat dialog, contacts from a chat attachment.

1. relay_poll.py fetches queued events from a (fake) relay web app, hands each to a (fake) Mission Control with the
   shared secret (X-Sage-Late when old) and posts the reply back.
2. vault_chat: a pasted password is detected and scrubbed from the agent's records; "save my stripe login" is an ask;
   the card carries an OPEN_DIALOG button; chat_push.vault_dialog has the four fields and the submit button.
3. gchat_events.vault_click: the dialog opens; a submit with the fields reaches the vault (stubbed) and closes with
   "nothing kept in chat"; a submit missing a field re-opens the dialog with the reason.
4. doer/vault.py add-stdin writes a real KeePass entry (in the keyring interpreter; skipped when pykeepass is absent).
5. contacts.import_file imports a .vcf for an owner from people.json.
6. The Apps Script relay carries pull mode (doGet / doPost / vault functions) and the manifest a web app block.
   python assistant/tests/test_relay.py
"""
import os as _os, tempfile as _tf
_os.environ.setdefault("WV_LOG_DIR", _tf.mkdtemp(prefix="wv_logs_"))
import json
import os
import subprocess
import sys
import tempfile
import threading
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")
HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
TMP = Path(tempfile.mkdtemp(prefix="wv_relay_"))
os.environ["ASSISTANT_DATA"] = str(TMP / "adata")
os.environ["AGENT_DATA"] = str(TMP / "adata" / "agent")
os.environ["AGENT_STATE"] = str(TMP / "astate")
os.environ["ASSISTANT_CARDS"] = str(TMP / "cards")
os.environ["DOER_DATA"] = str(TMP / "doer")
os.environ["CHAT_PUSH"] = "off"
(TMP / "adata" / "agent").mkdir(parents=True)
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT / "agent"))
FAILS = []


def check(name, cond, extra=""):
    print(("ok   " if cond else "FAIL ") + name + (f"  {extra}" if extra and not cond else ""))
    if not cond:
        FAILS.append(name)


# ---------------------------------------------------------------- 1. the poller against a fake relay and a fake Mission Control
SECRET = "test-relay-secret"
relay_state = {"events": [{"id": "e1", "at": int(time.time() * 1000), "fn": "card_answer", "event": {"relayFunction": "card_answer", "x": 1}},
                          {"id": "e2", "at": int((time.time() - 120) * 1000), "fn": "card_answer", "event": {"relayFunction": "card_answer", "x": 2}}],
               "replies": {}, "polls": 0}
mc_state = {"seen": []}


class FakeRelay(BaseHTTPRequestHandler):
    def log_message(self, *a):  # noqa: D401
        pass

    def _json(self, obj):
        b = json.dumps(obj).encode()
        self.send_response(200); self.send_header("Content-Type", "application/json"); self.send_header("Content-Length", str(len(b))); self.end_headers(); self.wfile.write(b)

    def do_GET(self):
        q = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
        if q.get("secret", [""])[0] != SECRET:
            return self._json({"error": "unauthorized"})
        op = q.get("op", [""])[0]
        if op == "health":
            return self._json({"ok": True, "mode": "pull", "pending": len(relay_state["events"])})
        if op == "poll":
            relay_state["polls"] += 1
            ev, relay_state["events"] = relay_state["events"], []
            return self._json({"ok": True, "events": ev})
        self._json({"error": "unknown"})

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)) or b"{}")
        if body.get("secret") != SECRET:
            return self._json({"error": "unauthorized"})
        if body.get("op") == "reply":
            relay_state["replies"][body["id"]] = body.get("body")
        self._json({"ok": True})


class FakeMC(BaseHTTPRequestHandler):
    def log_message(self, *a):  # noqa: D401
        pass

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)) or b"{}")
        mc_state["seen"].append({"path": self.path, "relay": self.headers.get("X-Sage-Relay"), "late": self.headers.get("X-Sage-Late"), "body": body})
        b = json.dumps({"answered": body.get("x")}).encode()
        self.send_response(200); self.send_header("Content-Type", "application/json"); self.send_header("Content-Length", str(len(b))); self.end_headers(); self.wfile.write(b)


relay_srv = ThreadingHTTPServer(("127.0.0.1", 0), FakeRelay)
mc_srv = ThreadingHTTPServer(("127.0.0.1", 0), FakeMC)
threading.Thread(target=relay_srv.serve_forever, daemon=True).start()
threading.Thread(target=mc_srv.serve_forever, daemon=True).start()
gl = TMP / "gchat.local.json"
gl.write_text(json.dumps({"relay_webapp_url": f"http://127.0.0.1:{relay_srv.server_address[1]}/exec", "relay_secret": SECRET}), encoding="utf-8")
env = dict(os.environ, AGENT_GCHAT_CONFIG=str(gl), MC_PORT=str(mc_srv.server_address[1]))
r = subprocess.run([sys.executable, str(ROOT / "scripts" / "relay_poll.py"), "--once"], capture_output=True, text=True, encoding="utf-8", env=env, timeout=60)
out = (r.stdout or "") + (r.stderr or "")
check("relay_poll --once polls the relay, hands both events to Mission Control and replies for each",
      r.returncode == 0 and "RELAY_POLL polls=1 events=2 replied=2" in out and len(mc_state["seen"]) == 2 and set(relay_state["replies"]) == {"e1", "e2"}, out[-300:] + str(mc_state["seen"])[:200])
check("every event reaches /gchat/events with the shared secret; only the old one is marked X-Sage-Late",
      all(s["path"] == "/gchat/events" and s["relay"] == SECRET for s in mc_state["seen"]) and [bool(s["late"]) for s in mc_state["seen"]] == [False, True], str(mc_state["seen"])[:300])
check("Mission Control's reply is what goes back to the relay", relay_state["replies"]["e1"] == {"answered": 1} and relay_state["replies"]["e2"] == {"answered": 2})
r = subprocess.run([sys.executable, str(ROOT / "scripts" / "relay_poll.py"), "--health"], capture_output=True, text=True, encoding="utf-8", env=env, timeout=60)
check("relay_poll --health reads the relay's health", r.returncode == 0 and '"ok": true' in r.stdout, r.stdout)
r = subprocess.run([sys.executable, str(ROOT / "scripts" / "relay_poll.py"), "--once"], capture_output=True, text=True, encoding="utf-8",
                   env=dict(env, AGENT_GCHAT_CONFIG=str(TMP / "missing.json")), timeout=60)
check("unconfigured: the poller exits at once with its line, no error", r.returncode == 0 and "not configured" in r.stdout, r.stdout)
relay_srv.shutdown(); mc_srv.shutdown()

# ---------------------------------------------------------------- 2. vault_chat
import vault_chat  # noqa: E402
s, site = vault_chat.detect_secret("here is my login: ana@acme.test / Sunflower9!")
check("a pasted login pair is detected", s == "Sunflower9!", (s, site))
s, site = vault_chat.detect_secret("the password for stripe.com is Tr0ub4dor&3 thanks")
check("a pasted password with a site is detected with the site", s == "Tr0ub4dor&3" and site == "stripe.com", (s, site))
check("talk about passwords is not a password", vault_chat.detect_secret("I need to reset my password today")[0] == "" and vault_chat.detect_secret("the password reset flow is broken")[0] == "")
check("'save my stripe login' is an ask for stripe", vault_chat.asks_login("save my stripe login") == "stripe", vault_chat.asks_login("save my stripe login"))
check("'add a password for app.hubspot.com' names the site", vault_chat.asks_login("add a password for app.hubspot.com") == "app.hubspot.com", vault_chat.asks_login("add a password for app.hubspot.com"))
check("a question about passwords is not an ask", vault_chat.asks_login("what's the password reset flow") == "")
rec = TMP / "adata" / "agent" / "transcript.jsonl"
rec.write_text(json.dumps({"text": "the password for stripe.com is Tr0ub4dor&3 thanks"}) + "\n", encoding="utf-8")
(TMP / "adata" / "agent" / "gchat_inbox.jsonl").write_text(json.dumps({"text": "pw: Tr0ub4dor&3"}) + "\n", encoding="utf-8")
n = vault_chat.scrub("Tr0ub4dor&3")
check("scrub removes the pasted password from every agent record (json-escaped too)", n == 2 and "Tr0ub4dor" not in rec.read_text(encoding="utf-8") and vault_chat.SCRUBBED in rec.read_text(encoding="utf-8"), n)
c = json.dumps(vault_chat.card("stripe.com", "Pip"))
check("the card chat carries has one OPEN_DIALOG button and no field", '"interaction": "OPEN_DIALOG"' in c and '"function": "vault_add"' in c and "textInput" not in c)
import chat_push  # noqa: E402
d = json.dumps(chat_push.vault_dialog("stripe.com"))
check("the vault dialog has site, username, password, totp and the Save button", all(f'"name": "{k}"' in d for k in ("site", "username", "password", "totp")) and '"function": "vault_add_submit"' in d)

# ---------------------------------------------------------------- 3. gchat_events.vault_click with the vault stubbed
import gchat_events  # noqa: E402
calls = []
gchat_events._vault_add = lambda fields: (calls.append(dict(fields)) or "ADDED Stripe stripe.com ana@acme.test totp=no")
person = {"key": "ana", "name": "Ana", "role": "owner"}
ev = {"chat": {"user": {"email": "ana@acme.test"}, "buttonClickedPayload": {"message": {"name": "spaces/X/messages/1", "space": {"name": "spaces/X"}}}},
      "commonEventObject": {"parameters": {"site": "stripe.com"}}}
k = {"function": "vault_add", "params": {"site": "stripe.com"}, "form": {}, "space": "spaces/X", "email": "ana@acme.test"}
r1 = json.dumps(gchat_events.vault_click(ev, k, person))
check("Enter login opens the dialog with the site prefilled", "textInput" in r1 and "stripe.com" in r1, r1[:200])
k2 = dict(k, function="vault_add_submit", form={"site": "stripe.com", "username": "ana@acme.test", "password": "Tr0ub4dor&3", "totp": ""}, dialog="SUBMIT_DIALOG")
r2 = json.dumps(gchat_events.vault_click(ev, k2, person))
check("the submit hands the fields to the vault once and closes with 'nothing kept in chat'",
      len(calls) == 1 and calls[0]["password"] == "Tr0ub4dor&3" and "Saved the stripe.com login for ana@acme.test" in r2 and "Nothing was kept in chat" in r2, r2[:300])
k3 = dict(k2, form={"site": "stripe.com", "username": "", "password": "x", "totp": ""})
r3 = json.dumps(gchat_events.vault_click(ev, k3, person))
check("a submit missing a field re-opens the dialog with the reason, nothing saved", len(calls) == 1 and "textInput" in r3 and "all needed" in r3, r3[:200])
gchat_events._vault_add = lambda fields: "VAULT_ERROR the site needs a web address (like stripe.com)"
r4 = json.dumps(gchat_events.vault_click(ev, dict(k2, form=dict(k2["form"], site="stripe")), person))
check("a vault refusal comes back in the dialog as 'Not saved: ...'", "Not saved:" in r4 and "web address" in r4, r4[:200])

# ---------------------------------------------------------------- 4. vault.py add-stdin for real (keyring interpreter)
from alib import VENV_PY  # noqa: E402
if VENV_PY.exists():
    venv_env = dict(os.environ, DOER_VAULT_KEY="test-key-only", DOER_DATA=str(TMP / "doer"))
    r = subprocess.run([str(VENV_PY), str(ROOT / "doer" / "vault.py"), "add-stdin"], input=json.dumps({"site": "stripe.com", "username": "ana@acme.test", "password": "Tr0ub4dor&3"}),
                       capture_output=True, text=True, encoding="utf-8", env=venv_env, timeout=120)
    check("vault.py add-stdin writes the entry and prints ADDED without the password", r.returncode == 0 and r.stdout.strip().endswith("ADDED Stripe stripe.com ana@acme.test totp=no") and "Tr0ub4dor" not in r.stdout + r.stderr, (r.stdout + r.stderr)[-300:])
    r = subprocess.run([str(VENV_PY), str(ROOT / "doer" / "vault.py"), "has", "https://dashboard.stripe.com/login"], capture_output=True, text=True, encoding="utf-8", env=venv_env, timeout=120)
    check("the login is found for the site afterwards", r.returncode == 0 and "ENTRY Stripe user=ana@acme.test" in r.stdout, (r.stdout + r.stderr)[-200:])
    r = subprocess.run([str(VENV_PY), str(ROOT / "doer" / "vault.py"), "add-stdin"], input=json.dumps({"site": "stripe", "username": "a", "password": "b"}),
                       capture_output=True, text=True, encoding="utf-8", env=venv_env, timeout=120)
    check("a site without a web address is refused", r.returncode == 1 and "VAULT_ERROR" in r.stdout, r.stdout)
else:
    print("skip vault add-stdin: no keyring interpreter on this machine")

# ---------------------------------------------------------------- 5. contacts from a .vcf for a people.json owner
os.environ["WV_CONTACTS_WIKI"] = str(TMP / "Contacts.md")
import contacts  # noqa: E402
vcf = TMP / "book.vcf"
vcf.write_text("BEGIN:VCARD\nVERSION:3.0\nFN:Pat Baker\nTEL;TYPE=CELL:+1 805 555 0100\nEMAIL:pat@acme.test\nEND:VCARD\nBEGIN:VCARD\nVERSION:3.0\nFN:Sam Supplier\nORG:Flour Co\nTEL:+1 805 555 0101\nEND:VCARD\n", encoding="utf-8")
r = contacts.import_file(vcf, owner=contacts.OWNERS[0], source="chat")
check("contacts.import_file imports a .vcf for the first people.json owner", r.get("ok") and r.get("count") == 2 and r.get("with_phone") == 2 and r.get("owner") == contacts.OWNERS[0], r)
check("the owners come from people.json (role owner), never a literal", isinstance(contacts.OWNERS, tuple) and len(contacts.OWNERS) >= 1 and all(isinstance(x, str) for x in contacts.OWNERS))
import bridge  # noqa: E402
check("bridge recognises a contacts attachment by name or type", bridge._is_vcf({"contentName": "All Contacts.vcf"}) and bridge._is_vcf({"contentType": "text/x-vcard"}) and not bridge._is_vcf({"contentName": "voice.m4a"}))

# ---------------------------------------------------------------- 6. the relay script carries pull mode
gs = (ROOT / "agent" / "gchat_relay" / "Code.gs").read_text(encoding="utf-8")
man = json.loads((ROOT / "agent" / "gchat_relay" / "appsscript.json").read_text(encoding="utf-8"))
check("Code.gs has pull mode (doGet / doPost / queue), the vault functions, and still push mode", all(s in gs for s in ("function doGet", "function doPost", "function pull_", "function push_", "function vault_add_submit", "popEvents_", "optimistic_")))
check("a vault submit is never held: dropped from the queue on timeout, in both modes", "fn === 'vault_add_submit' || fn === 'vault_add' || fn === 'card_edit') dequeue_(id)" in gs and "fn !== 'vault_add_submit'" in gs)
check("the manifest deploys as a web app too and names no host", man.get("webapp", {}).get("access") == "ANYONE_ANONYMOUS" and "urlFetchWhitelist" not in man)

print()
print("ALL PASSED" if not FAILS else f"{len(FAILS)} FAILED: {FAILS}", f"({TMP})")
sys.exit(1 if FAILS else 0)

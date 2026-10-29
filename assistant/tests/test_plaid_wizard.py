"""Offline tests for the phone wizard that connects Relay through Plaid (scripts/money/plaid_wizard.py)
and its chat entry (connections.json bank-plaid setup.chat + connect.py): status by step, keys stored
and verified (bad keys removed), Link token, exchange -> item + first pull + chat line, the page and the
server routes. Fake Plaid opener, stubbed Credential Manager, scratch ASSISTANT_DATA."""
import os as _os, tempfile as _tf
_os.environ.setdefault("WV_LOG_DIR", _tf.mkdtemp(prefix="wv_logs_"))
import io
import json
import os
import sys
import tempfile
import urllib.error
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent
TMP = Path(tempfile.mkdtemp(prefix="wv_pw_"))
os.environ["ASSISTANT_DATA"] = str(TMP)
os.environ["ASSISTANT_CARDS"] = str(TMP / "cards")
os.environ["CHAT_PUSH"] = "off"
os.environ["AGENT_HOME_SPACE"] = ""
sys.path.insert(0, str(HERE.parent / "scripts"))
sys.path.insert(0, str(HERE.parent / "scripts" / "money"))
import plaid  # noqa: E402
import plaid_wizard as W  # noqa: E402
import connect  # noqa: E402
import setup_actions  # noqa: E402

FAILS = []


def check(name, cond, extra=""):
    print(("ok   " if cond else "FAIL ") + name + (f"  {extra}" if extra and not cond else ""))
    if not cond:
        FAILS.append(name)


# stub Credential Manager
VAULT = {}
plaid.store_secret = lambda n, v: VAULT.__setitem__(n, v)
plaid.clear_secret = lambda n: VAULT.pop(n, None)
plaid.secret = lambda n: VAULT.get(n)
posted = []
W.bridge_post = None


class _Resp:
    def __init__(self, body): self.body = body
    def read(self): return self.body
    def __enter__(self): return self
    def __exit__(self, *a): return False


ACCOUNTS = [{"account_id": "rel-op", "name": "Operating", "type": "depository", "subtype": "checking", "balances": {"current": 5120.55, "available": 5120.55, "iso_currency_code": "USD"}},
            {"account_id": "rel-tax", "name": "Tax", "type": "depository", "subtype": "savings", "balances": {"current": 2100.0, "iso_currency_code": "USD"}}]


class Fake:
    def __init__(self):
        self.calls, self.reject = [], False

    def __call__(self, req, timeout=0):
        path = urllib.request.urlparse(req.full_url).path
        body = json.loads(req.data.decode("utf-8"))
        self.calls.append((req.get_method(), path, body))
        if self.reject or body.get("secret") == "bad-secret-bad-secret":
            err = json.dumps({"error_type": "INVALID_INPUT", "error_code": "INVALID_API_KEYS", "error_message": "invalid client_id or secret provided"}).encode()
            raise urllib.error.HTTPError(req.full_url, 400, "Bad Request", {}, io.BytesIO(err))
        if path == "/link/token/create":
            return _Resp(json.dumps({"link_token": "link-production-abc"}).encode())
        if path == "/item/public_token/exchange":
            return _Resp(json.dumps({"access_token": "access-production-SECRET", "item_id": "item-relay-1"}).encode())
        if path == "/accounts/balance/get":
            return _Resp(json.dumps({"accounts": ACCOUNTS, "item": {"item_id": "item-relay-1", "institution_id": "ins_relay"}}).encode())
        if path == "/transactions/sync":
            return _Resp(json.dumps({"added": [], "modified": [], "removed": [], "next_cursor": "c1", "has_more": False, "accounts": ACCOUNTS}).encode())
        raise AssertionError(f"unexpected {path}")


fake = Fake()

# ------------------------------------------------------------------ step 1: nothing yet
s = W.status()
check("status: no keys -> step 1", s["step"] == 1 and not s["keys"] and not s["items"], s)
check("status carries the page url", s["page_url"].endswith("/setup/plaid"))
r = W.link_token(opener=fake)
check("link token refused without keys", not r["ok"] and "keys" in r["message"].lower())

# ------------------------------------------------------------------ step 2: keys
r = W.save_keys("", "", opener=fake)
check("empty keys refused", not r["ok"])
r = W.save_keys("short", "short", opener=fake)
check("implausible keys refused before storing", not r["ok"] and not VAULT)
r = W.save_keys("5f1c2e3d4b5a69788796a5b4c3d2e1f0", "bad-secret-bad-secret", opener=fake)
check("bad keys: Plaid rejects, both removed again", not r["ok"] and "rejected" in r["message"].lower() and not VAULT, (r, VAULT))
r = W.save_keys("5f1c2e3d4b5a69788796a5b4c3d2e1f0", "a1b2c3d4e5f60718293a4b5c6d7e8f90", opener=fake)
check("good keys: stored + verified by /link/token/create only", r["ok"] and VAULT.get("plaid:client_id") and VAULT.get("plaid:secret")
      and [c[1] for c in fake.calls][-1] == "/link/token/create", (r, fake.calls[-1][1]))
check("status: keys -> step 2 done, step 3 on", W.status()["step"] == 2)
check("no secret in any log line", not any("a1b2c3d4e5f6" in ln for ln in (TMP / "money" / "log.jsonl").read_text(encoding="utf-8").splitlines()) if (TMP / "money" / "log.jsonl").exists() else True)

# ------------------------------------------------------------------ step 3: Link + exchange
r = W.link_token(opener=fake)
check("link token minted for the phone", r["ok"] and r["token"].startswith("link-") and r["env"] == "production", r)

# a stub agent bridge so the "connected" line is captured, not posted
import types
stub = types.ModuleType("bridge")
stub.post = lambda who, text, channel="": posted.append((who, text, channel))
sys.modules["bridge"] = stub
r = W.exchange("public-production-xyz", {"institution": {"name": "Relay", "institution_id": "ins_relay"}, "accounts": [{"name": "Operating"}, {"name": "Tax"}]},
               opener=fake, background=False)
check("exchange: item stored, access token in the vault, first pull ran, chat line posted",
      r["ok"] and r["institution"] == "Relay" and VAULT.get("plaid:access_token:item-relay-1") == "access-production-SECRET"
      and posted and "Relay is connected" in posted[0][1] and "Operating" in posted[0][1], (r, posted))
s3 = W.status()
check("status: linked -> step 3 with accounts and balances", s3["step"] == 3 and len(s3["accounts"]) == 2 and s3["items"][0]["institution"] == "Relay", s3)
check("no page or wizard call touched a non-allow-listed path", all(c[1] in plaid.ALLOWED_PATHS for c in fake.calls))

# ------------------------------------------------------------------ the page + routing
code, html, ct = W.handle("GET", "/setup/plaid")
check("page served", code == 200 and ct == "text/html" and plaid.LINK_JS in html and "Connect Relay" in html and "/api/setup/plaid" in html)
code, body, ct = W.handle("GET", "/api/setup/plaid/status")
check("status route", code == 200 and json.loads(body)["step"] == 3)
check("unknown path -> None", W.handle("GET", "/api/setup/plaid/nope") is None and W.handle("POST", "/setup", {}) is None)
code, body, ct = W.handle("GET", "/api/setup/plaid/link-token", {"update": "item-relay-1"}, opener=fake)
check("re-sign-in link token (update mode) carries the access token", json.loads(body)["ok"] and fake.calls[-1][2].get("access_token") == "access-production-SECRET")
src = (HERE.parent / "scripts" / "mc_server.py").read_text(encoding="utf-8")
check("mc_server routes /setup/plaid + /api/setup/plaid/*", '"/setup/plaid"' in src and '"/api/setup/plaid/"' in src and "plaid_wizard.handle" in src)
rows = {r["id"]: r for r in setup_actions.rows()}
check("Setup page row links to the phone page", rows["bank-plaid"].get("page") == "/setup/plaid" and rows["bank-plaid"].get("page_label"))
sp = (HERE.parent / "scripts" / "setup_page.py").read_text(encoding="utf-8")
check("setup page renders the page link", "r.page" in sp and "pagebtn" in sp)

# ------------------------------------------------------------------ chat entry
check("'set up plaid' / 'connect relay' are recognised", connect.asked("can you set up plaid for me") == "bank-plaid" and connect.asked("connect Relay") == "bank-plaid"
      and connect.asked("relay: how do I connect it") == "bank-plaid", (connect.asked("can you set up plaid for me"), connect.asked("connect Relay")))
check("'set up simplefin' still goes to SimpleFIN", connect.asked("set up simplefin") == "bank-simplefin")
check("unrelated text -> nothing", connect.asked("the relay race is Saturday") == "" and connect.asked("plaid shirt") == "")
st = connect.steps("bank-plaid")
check("steps carry the real Mission Control page link, no placeholder", "/setup/plaid" in st and "{mc_url}" not in st and "https://" in st, st)
check("steps say keys never go in chat", "never see them in chat" in st.lower() or "credential manager" in st.lower())
check("no paste block for Plaid (long-lived keys stay out of chat)", "paste" not in (connect.row("bank-plaid")["setup"]["chat"]) and connect.detect("5f1c2e3d4b5a69788796a5b4c3d2e1f0")[0] == "")
check("keys stored: chat steps skip the Plaid sign-up and name the linked bank", "sign-up" not in st and "already stored" in st and "Already linked: Relay" in st, st)
check("chat steps offer another bank (Plaid Link again)", "Connect another bank" in st, st)
saved = {k: VAULT.pop(k) for k in ("plaid:client_id", "plaid:secret")}
st0 = connect.steps("bank-plaid")
check("no keys: chat steps keep the sign-up + paste steps", "Open Plaid sign-up" in st0 and "paste the client id" in st0 and "already stored" not in st0, st0)
VAULT.update(saved)
check("unknown state shows the first-time path", connect._shown({"if": "!keys", "text": "a"}, {}) == "a" and connect._shown({"if": "keys", "text": "b"}, {}) == "")
check("page offers another bank and hides sign-up once keys are stored", "Connect another bank" in html and 'S.keys ? "none"' in html)
r = W.exchange("public-production-xyz", {"institution": {"name": "Relay"}}, opener=fake, background=False)
check("after a link the page says how to add the next bank", "Connect another bank" in r.get("message", ""), r)
hint = connect.chat_hint()
check("brain hint names plaid", "plaid" in hint.lower())

# ------------------------------------------------------------------ phone only (2026-10-20: "Your instructions involve using a PC.")
check("page: Paste button on both key boxes + Keys page link + show secret",
      html.count("pasteInto(") >= 3 and "clipboard.readText" in html and W.KEYS_PAGE in html and "toggleSecret()" in html)
check("page: every Plaid link opens in a new tab (the wizard stays put)",
      all('target="_blank"' in seg.split(">")[0] for seg in html.split("<a ")[1:] if 'href="https://' in seg.split(">")[0]))
stp = connect.steps("bank-plaid")
st0p = st0
check("chat steps: no PC or terminal, direct Keys link, Paste buttons",
      all(w not in (stp + st0p).lower() for w in ("terminal", "python ", "console", "team settings")) and W.KEYS_PAGE in st0p
      and "tap Paste" in st0p and "nothing needs the PC" in stp, st0p)
fake.reject = False
_lt = plaid.link_token
plaid.link_token = lambda *a, **k: (_ for _ in ()).throw(plaid.PlaidError("INVALID_FIELD", "redirect_uri must be registered"))
r = W.link_token(opener=fake)
check("redirect_uri miss: page gets a Copy address + Open API page fix, no PC wording",
      not r["ok"] and r.get("link") == W.API_PAGE and r.get("copy", "").endswith("/setup/plaid") and "Team Settings" not in r["message"], r)
VAULT_KEEP = dict(VAULT)
r = W.save_keys("a1b2c3d4e5f6a7b8c9d0", "s1s2s3s4s5s6s7s8s9s0", opener=fake)
check("redirect_uri miss after keys: same phone fix", r["ok"] and r.get("warn") == "redirect_uri" and r.get("link") == W.API_PAGE and r.get("copy"), r)
plaid.link_token = _lt
VAULT.clear(); VAULT.update(VAULT_KEEP)
plaid._status(error="Relay: ITEM_LOGIN_REQUIRED - sign in again: python assistant/scripts/money/plaid.py setup --update item-relay-1")
s4 = W.status()
check("login-required: page says Re-sign-in (no terminal command) and targets that bank",
      "plaid.py" not in s4["error"] and "Re-sign-in" in s4["error"] and s4["relink"] == "item-relay-1", s4)

# ------------------------------------------------------------------ Plaid Link's mobile web flow (2026-10-21)
plaid._status(error="")
W.mc_url = lambda: "https://this-pc.example.ts.net:9443"
check("the page's https address is the default redirect_uri", W.page_url() == "https://this-pc.example.ts.net:9443/setup/plaid"
      and W.link_feed({"redirect_uri": ""})["redirect_uri"] == W.page_url() and W.link_feed({"redirect_uri": "https://x/y"})["redirect_uri"] == "https://x/y")
r = W.link_token(opener=fake)
check("link token minted WITH the page as redirect_uri when Plaid allows it; oauth ready",
      r["ok"] and r.get("oauth") is True and fake.calls[-1][2].get("redirect_uri") == W.page_url() and r.get("redirect_uri") == W.page_url(), (r, fake.calls[-1][2]))
check("status says the OAuth return is ready", W.status()["oauth_ready"] is True and W.status()["redirect_uri"] == W.page_url() and W.status()["api_page"] == W.API_PAGE)


class RedirectFussy(Fake):
    """Plaid before the address is registered: refuses a redirect_uri, happy without one."""
    def __call__(self, req, timeout=0):
        body = json.loads(req.data.decode("utf-8"))
        if urllib.request.urlparse(req.full_url).path == "/link/token/create" and body.get("redirect_uri"):
            self.calls.append((req.get_method(), "/link/token/create", body))
            err = json.dumps({"error_type": "INVALID_REQUEST", "error_code": "INVALID_FIELD", "error_message": "redirect_uri must be configured in the developer dashboard"}).encode()
            raise urllib.error.HTTPError(req.full_url, 400, "Bad Request", {}, io.BytesIO(err))
        return super().__call__(req, timeout)


fussy = RedirectFussy()
r = W.link_token(opener=fussy)
check("address not allowed yet: minted again WITHOUT redirect_uri (Relay still links), fix offered, no PC wording",
      r["ok"] and r.get("oauth") is False and [c[2].get("redirect_uri") for c in fussy.calls[-2:]] == [W.page_url(), None]
      and r.get("link") == W.API_PAGE and r.get("copy") == W.page_url() and "hint" in r and "Team Settings" not in r["hint"], (r, fussy.calls[-2:]))
check("status then says the OAuth return is not ready (page shows Copy address)", W.status()["oauth_ready"] is False)
r = W.save_keys("5f1c2e3d4b5a69788796a5b4c3d2e1f0", "a1b2c3d4e5f60718293a4b5c6d7e8f90", opener=fussy)
check("keys verified through the fallback: stored, ok, soft redirect warning with the phone fix",
      r["ok"] and VAULT.get("plaid:secret") and r.get("warn") == "redirect_uri" and r.get("link") == W.API_PAGE and "Relay works without it" in r["message"], r)
r = W.save_keys("5f1c2e3d4b5a69788796a5b4c3d2e1f0", "a1b2c3d4e5f60718293a4b5c6d7e8f90", opener=fake)
check("keys verified with the address allowed: plain ok, no warning", r["ok"] and not r.get("warn"), r)
html = W.page()
check("page re-opens Link with the SAME token on the OAuth return (localStorage + receivedRedirectUri)",
      "localStorage.setItem" in html and "savedToken()" in html and "receivedRedirectUri" in html and 'returning()' in html and "oauth_state_id=" in html)
check("page: phone width (viewport, 375px button rule, no sideways overflow on long text)",
      "width=device-width" in html and "max-width:420px" in html and "overflow-wrap:anywhere" in html)
check("page shows the one-time address fix only when the OAuth return is not ready", "S.oauth_ready === false" in html and "Open API page" in html)
check("page: Google Chat in-app browser hint", "Open in Safari" in html)
stp, st0p = connect.steps("bank-plaid"), st0
check("chat steps: phone only - no PC/terminal/console/Team Settings, no 'tailnet only', say the page is the same place as the cards",
      all(w not in (stp + st0p).lower() for w in ("terminal", "python ", "console", "team settings", "tailnet only")) and "same place as your cards" in stp
      and "nothing is opened or run on the PC" in stp and "Store and verify" in st0p and "sends you back to the page by itself" in stp, (stp, st0p))
check("no 'paste what it gives you here' after the Plaid steps; SimpleFIN keeps it",
      connect.paste_hint("bank-plaid") == "" and "Paste what it gives you here" in connect.paste_hint("bank-simplefin") and connect.paste_hint("nope") == "")
csrc = (HERE.parent / "scripts" / "cards.py").read_text(encoding="utf-8")
check("the setup card's chat reply uses connect.paste_hint (no fixed paste line)", "connect.paste_hint(rid)" in csrc and csrc.count("Paste what it gives you here") == 0)
W.mc_url = lambda: ""
check("no Mission Control address known: no redirect_uri, token still minted", W.page_url() == "" and W.link_token(opener=fake)["ok"] and fake.calls[-1][2].get("redirect_uri") is None)

print()
print("ALL OK" if not FAILS else f"{len(FAILS)} FAILED: {FAILS}")
sys.exit(1 if FAILS else 0)

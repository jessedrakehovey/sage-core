"""Offline tests for data entry in Google Chat, never on Mission Control (scripts/chat_forms.py, an owner 2026-11-01:
"Make all data entry for anything go through google chat never mission control."): which connections get a form,
what a setup ask brings, the card -> dialog -> submit path through gchat_events (keys stored, values never echoed or
logged), Plaid's Hosted Link end to end (keys dialog -> sign-in card -> /link/token/get -> exchange -> chat line),
the brain's `blocked` deed posting the form, and Mission Control's read-only switch. Fake Plaid, stubbed keyring,
stubbed Chat posts, scratch ASSISTANT_DATA."""
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
TMP = Path(tempfile.mkdtemp(prefix="wv_forms_"))
os.environ["ASSISTANT_DATA"] = str(TMP)
os.environ["ASSISTANT_CARDS"] = str(TMP / "cards")
os.environ["CHAT_PUSH"] = "off"
os.environ["AGENT_HOME_SPACE"] = ""
sys.path.insert(0, str(HERE.parent / "scripts"))
sys.path.insert(0, str(HERE.parent / "scripts" / "money"))
sys.path.insert(0, str(HERE.parent / "agent"))
import plaid  # noqa: E402
import plaid_wizard as W  # noqa: E402
import chat_forms as F  # noqa: E402
import gchat_events as E  # noqa: E402
import gchat  # noqa: E402
import setup_actions  # noqa: E402

FAILS = []


def check(name, cond, extra=""):
    print(("ok   " if cond else "FAIL ") + name + (f"  {extra}" if extra and not cond else ""))
    if not cond:
        FAILS.append(name)


# stubs: Credential Manager, env store, Chat posts, the owner, the after-link post
VAULT, ENV, POSTS = {}, {}, []
plaid.store_secret = lambda n, v: VAULT.__setitem__(n, v)
plaid.clear_secret = lambda n: VAULT.pop(n, None)
plaid.secret = lambda n: VAULT.get(n)
F._keyring_set = lambda k, v: VAULT.__setitem__(k, v)
F._env_set = lambda n, v: ENV.__setitem__(n, v)
gchat.post_body = lambda space, body, thread="": POSTS.append((space, body, thread)) or "spaces/X/messages/1"
gchat.thread_of = lambda name: "spaces/X/threads/T"
OWNER = {"key": "owner1", "name": "Sam"}
E._core = lambda: type("C", (), {"person_for": staticmethod(lambda **k: OWNER), "can": staticmethod(lambda p, a: True),
                                  "say": staticmethod(lambda *a, **k: None)})
AFTER = []
W._after_link = lambda opener=None: AFTER.append(1)


class _Resp:
    def __init__(self, body): self.body = body
    def read(self): return self.body
    def __enter__(self): return self
    def __exit__(self, *a): return False


class FakePlaid:
    """Hosted Link: /link/token/create hands back a secure.plaid.com page; /link/token/get reports the session."""
    def __init__(self):
        self.calls, self.finished = [], False

    def __call__(self, req, timeout=0):
        path = urllib.request.urlparse(req.full_url).path
        body = json.loads(req.data.decode("utf-8"))
        self.calls.append((path, body))
        if body.get("secret") == "bad-secret-bad-secret":
            err = json.dumps({"error_type": "INVALID_INPUT", "error_code": "INVALID_API_KEYS", "error_message": "invalid client_id or secret provided"}).encode()
            raise urllib.error.HTTPError(req.full_url, 400, "Bad Request", {}, io.BytesIO(err))
        if path == "/link/token/create":
            r = {"link_token": f"link-production-{len(self.calls)}"}
            if "hosted_link" in body:
                r["hosted_link_url"] = f"https://secure.plaid.com/hl/{len(self.calls)}"
            return _Resp(json.dumps(r).encode())
        if path == "/link/token/get":
            sess = [{"link_session_id": "s1", "finished_at": "2026-11-01T12:00:00Z",
                     "results": {"item_add_results": [{"public_token": "public-production-1", "institution": {"name": "First Bank", "institution_id": "ins_r"},
                                                       "accounts": [{"name": "Account 1"}]}]}}] if self.finished else []
            return _Resp(json.dumps({"link_token": body["link_token"], "link_sessions": sess}).encode())
        if path == "/item/public_token/exchange":
            return _Resp(json.dumps({"access_token": "access-production-SECRET", "item_id": "item-bank-1"}).encode())
        raise AssertionError(f"unexpected {path}")


fake = FakePlaid()
_client = plaid.Client
plaid.Client = lambda env, cid, sec, opener=None: _client(env, cid, sec, opener=fake)


def tap(fn, params, form=None):
    """A Chat button tap / dialog submit, as the relay delivers it (add-on shape)."""
    ev = {"chat": {"buttonClickedPayload": {"message": {"name": "spaces/X/messages/9", "space": {"name": "spaces/X"}}},
                   "user": {"email": "owner@example.com", "name": "users/1", "displayName": "Sam"}},
          "commonEventObject": {"invokedFunction": fn, "parameters": params,
                                "formInputs": {k: {"stringInputs": {"value": [v]}} for k, v in (form or {}).items()}}}
    logs = []
    return E.handle(ev, logs.append), logs


# ------------------------------------------------------------------ which connections take what
ids = {s["rid"]: s for s in F.rows()}
check("Plaid, SimpleFIN, the app passwords and the tokens all have a Chat form",
      {"bank-plaid", "bank-simplefin", "typeform"} <= set(ids) and any(s["store"].startswith("keyring:imap:") for s in ids.values()), sorted(ids))
check("sign-in windows and console-only jobs get no form", not F.spec("agent-bridge") and not F.spec("pw-profiles"))
check("a setup ask brings its form; ordinary chat does not",
      F.match("can you set up plaid") == "bank-plaid" and F.match("fix the typeform token") == "typeform"
      and F.match("add a typeform question") == "" and F.match("link Sam to the doc") == "" and F.match("the relay race is saturday") == "")

# ------------------------------------------------------------------ Plaid: keys dialog
body = F.card("bank-plaid")
btn = body["cardsV2"][0]["card"]["sections"][0]["widgets"][-1]["buttonList"]["buttons"][0]
check("no keys yet: the card's button opens the keys dialog", btn["onClick"]["action"]["function"] == "form_open"
      and btn["onClick"]["action"]["interaction"] == "OPEN_DIALOG" and "textInput" not in json.dumps(body), body)
r, logs = tap("form_open", {"rid": "bank-plaid"})
dlg = r["action"]["navigations"][0]["pushCard"]
names = [w["textInput"]["name"] for s in dlg["sections"] for w in s["widgets"] if "textInput" in w]
check("the dialog asks for client_id + secret", names == ["client_id", "secret"], names)
r, logs = tap("form_submit", {"rid": "bank-plaid"}, {"client_id": "5f1c2e3d4b5a69788796a5b4c3d2e1f0", "secret": "bad-secret-bad-secret"})
dlg = r["action"]["navigations"][0]["pushCard"]
check("bad keys: the dialog stays open with Plaid's reason, nothing kept, the secret never echoed",
      "INVALID_API_KEYS" in json.dumps(dlg) and "bad-secret" not in json.dumps(dlg) and not VAULT.get("plaid:secret"), dlg)
POSTS.clear()
r, logs = tap("form_submit", {"rid": "bank-plaid"}, {"client_id": "5f1c2e3d4b5a69788796a5b4c3d2e1f0", "secret": "a1b2c3d4e5f60718293a4b5c6d7e8f90"})
check("good keys: stored, the dialog closes with one line", VAULT.get("plaid:secret") == "a1b2c3d4e5f60718293a4b5c6d7e8f90"
      and r["action"]["navigations"][0]["endNavigation"]["action"] == "CLOSE_DIALOG", r)
check("no typed value in any log line", not any("a1b2c3d4e5f6" in ln or "5f1c2e3d" in ln for ln in logs), logs)
nxt = json.dumps(POSTS[-1][1]) if POSTS else ""
check("then the bank sign-in card lands in the same thread with Plaid's own page", POSTS and POSTS[-1][2] == "spaces/X/threads/T"
      and "https://secure.plaid.com/hl/" in nxt and "Sign in to your bank" in nxt, POSTS[-1:] if POSTS else POSTS)

# ------------------------------------------------------------------ Plaid: Hosted Link result
mint = [c for c in fake.calls if c[0] == "/link/token/create" and "hosted_link" in c[1]]
check("the sign-in page is Hosted Link with no redirect address of ours", mint and "redirect_uri" not in mint[-1][1], mint[-1:] if mint else mint)
check("keys stored: asking again brings the sign-in card, not the keys form", "secure.plaid.com" in json.dumps(F.card("bank-plaid")))
r = W.hosted_check()
check("nothing finished yet: still pending, nothing linked", r["linked"] == [] and r["pending"] >= 1 and not AFTER, r)
fake.finished = True
r = W.hosted_check()
check("finished on Plaid's page: public token exchanged, access token stored, chat told",
      r["linked"] and r["linked"][0]["institution"] == "First Bank" and VAULT.get("plaid:access_token:item-bank-1") and AFTER, r)
check("done sessions are cleared", W.hosted_check()["pending"] == 0)
check("every Plaid call stayed on the allow-list", all(c[0] in plaid.ALLOWED_PATHS for c in fake.calls), [c[0] for c in fake.calls])
POSTS.clear()
r, logs = tap("form_action", {"rid": "bank-plaid", "do": "connect"})
check("Another bank: the card is replaced by a fresh sign-in page",
      "secure.plaid.com" in json.dumps(r) and "updateMessageAction" in json.dumps(r), r)

# ------------------------------------------------------------------ the other forms
r, logs = tap("form_submit", {"rid": "typeform"}, {"value": "tfp_SECRETVALUE_123"})
check("a token form stores the env var and closes", ENV.get("TYPEFORM_TOKEN") == "tfp_SECRETVALUE_123" and "CLOSE_DIALOG" in json.dumps(r)
      and "tfp_SECRET" not in json.dumps(r) and not any("tfp_SECRET" in ln for ln in logs), r)
mail = next(s for s in F.rows() if s["store"].startswith("keyring:imap:"))
tap("form_submit", {"rid": mail["rid"]}, {"value": "abcd efgh ijkl mnop"})
check("an app password is stored without its spaces", VAULT.get(mail["store"].split(":", 1)[1]) == "abcdefghijklmnop")
r, logs = tap("form_submit", {"rid": "typeform"}, {"value": ""})
check("an empty box keeps the dialog open", "pushCard" in json.dumps(r), r)
r, logs = tap("form_open", {"rid": "no-such-row"})
check("an unknown form is refused, nothing opened", "pushCard" not in json.dumps(r))

# ------------------------------------------------------------------ the brain's blocked deed
POSTS.clear()
ok = F.post_for({"kind": "connection", "connection": F.spec("typeform")["name"], "what": "read the new leads", "why": "the token is dead"},
                {"space": "spaces/X", "thread": "spaces/X/threads/Q"})
check("`blocked` naming a connection posts its form in the asking thread", ok and POSTS[-1][0] == "spaces/X" and POSTS[-1][2] == "spaces/X/threads/Q"
      and "form-typeform" in json.dumps(POSTS[-1][1]))
check("`blocked` of another kind posts nothing", not F.post_for({"kind": "fact", "what": "x", "why": "y"}, {"space": "spaces/X"}))

# ------------------------------------------------------------------ Mission Control takes no typing
rows = {r["id"]: r for r in setup_actions.rows()}
check("Setup page: form rows have no typing button, only 'in Google Chat'",
      all(rows[i]["kind"] == "chat" and not rows[i]["label"] and "Google Chat" in rows[i]["how"] for i in ids if i in rows and ids[i]["store"] != "job"))
check("Setup page: a job row keeps its own Run button (the agent does it; no typing)", all(rows[i]["kind"] == "job" for i in ids if i in rows and ids[i]["store"] == "job"))
check("Setup page run on a form row only points to Chat", "Google Chat" in setup_actions.run("typeform")["message"])
code, html, ct = W.handle("GET", "/setup/plaid")
check("the old Plaid page has nothing to type", "<input" not in html and "Google Chat" in html)
src = (HERE.parent / "scripts" / "mc_server.py").read_text(encoding="utf-8")
check("mc_server refuses typed POSTs and serves Chat pointers when read-only",
      "ENTRY_POSTS" in src and "mc_read_only()" in src and '"/api/ask"' in src and "_chat_only_page(" in src)
dsrc = (HERE.parent / "scripts" / "dashboard.py").read_text(encoding="utf-8")
check("dashboard: the note and ask boxes go when read-only", "def _note_box" in dsrc and "def _ask_box" in dsrc and dsrc.count('<input class="note"') == 1)
import alib  # noqa: E402
check("this pack is read-only", alib.mc_read_only() is True)
for p in ("agent/prompt.md", "agent/templates/prompt.generic.md"):
    t = (HERE.parent / p).read_text(encoding="utf-8")
    check(f"{p}: data entry is in chat, never Mission Control", "All data entry happens in this chat, never on Mission Control" in t
          and "its button is on the Setup page" not in t)

print()
print("ALL OK" if not FAILS else f"{len(FAILS)} FAILED: {FAILS}")
sys.exit(1 if FAILS else 0)

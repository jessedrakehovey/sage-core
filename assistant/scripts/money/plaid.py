"""plaid.py - the second READ-ONLY bank feed (Plaid) -> data/money/ledger.json, for Relay.

Relay (the business bank: Operating / Tax / Savings buckets) is not on SimpleFIN; Relay supports Plaid only
(Wiki/Integrations/Bank Feed (SimpleFIN).md, 2026-10-15). This adapter writes the SAME ledger shape as
simplefin.py (common.py docstring); nothing downstream (rules, forecast, quarterlies, readback) knows which
feed a row came from beyond the `feed` tag on each account.

Read-only by construction. `Client.post` refuses every path but these four, and the two setup paths only
inside the one-time Link flow:
  /link/token/create           setup: mints the token Plaid Link needs (no bank data)
  /item/public_token/exchange  setup: turns Link's public token into the item's access token (stored, never printed)
  /accounts/balance/get        pull:  live balances
  /transactions/sync           pull:  added / modified / removed transactions behind a cursor
No endpoint that moves money (/transfer/*, /payment_initiation/*, /processor/*) or changes an item
(/item/remove, /item/webhook/update, ...) exists in this module, and the allow-list is asserted on every call.

Secrets live in Windows Credential Manager (service the owner-assistant): plaid:client_id, plaid:secret
(`setup_creds.py money plaid`) and plaid:access_token:<item_id>, written by the Link flow. The owner's bank
login is typed ONLY inside Plaid's own Link window: `plaid.py setup` serves a one-page site on 127.0.0.1,
opens the browser, and receives just the public token back. No bank credential ever touches this code.

State: data/money/plaid_state.json {items: {item_id: {institution, env, linked, cursor, accounts}}, pulls}.
Status for /setup + conn_check: data/money/plaid_status.json {last_ok, error, configured, items, accounts}.
Plaid's sign convention (positive = money OUT of the account) is flipped to the ledger's (outflow negative).
A pending transaction that posts arrives as `removed` + a new row carrying pending_transaction_id: the bucket
the owner (or a rule) already gave it moves to the posted row.

  python assistant/scripts/money/plaid.py                 # pull every linked item (the spine runs this)
  python assistant/scripts/money/plaid.py status          # what the ledger holds from Plaid (no network)
  python assistant/scripts/money/plaid.py setup           # one-time Link in the browser (the owner signs in to Relay there)
  python assistant/scripts/money/plaid.py setup --update <item_id>   # re-sign-in after ITEM_LOGIN_REQUIRED
  python assistant/scripts/money/plaid.py unlink <item_id>           # forget the local access token (nothing at Plaid changes)
  python assistant/scripts/money/plaid.py --dry-run | --full | --env sandbox
"""
from __future__ import annotations

import argparse
import json
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import webbrowser
from datetime import date, datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import common as C  # noqa: E402
from alib import KEYRING_SERVICE, ensure_venv, now_iso, save_json, secret  # noqa: E402

PROVIDER = "plaid"
STATUS = C.DIR / "plaid_status.json"
STATE = C.DIR / "plaid_state.json"
USER_AGENT = "sage-money-lane/1 (read-only)"
PLAID_VERSION = "2020-09-14"
ENV_URLS = {"sandbox": "https://sandbox.plaid.com", "production": "https://production.plaid.com"}
READ_PATHS = frozenset({"/accounts/balance/get", "/transactions/sync"})
SETUP_PATHS = frozenset({"/link/token/create", "/item/public_token/exchange"})
ALLOWED_PATHS = READ_PATHS | SETUP_PATHS
LINK_JS = "https://cdn.plaid.com/link/v2/stable/link-initialize.js"

DEFAULT_FEED = {"provider": PROVIDER, "env": "production", "client_name": "Sage money lane (read-only)",
                "secrets": {"client_id": "plaid:client_id", "secret": "plaid:secret", "access_token_prefix": "plaid:access_token:"},
                "country_codes": ["US"], "language": "en", "products": ["transactions"], "days_requested": 730,
                "link_port": 8765, "link_timeout_s": 900, "redirect_uri": "", "sync_count": 500, "max_pulls_per_day": 6}


class PlaidError(Exception):
    def __init__(self, code: str, message: str, http: int = 0, error_type: str = ""):
        super().__init__(f"{code}: {message}")
        self.code, self.message, self.http, self.error_type = code, message, http, error_type


# ------------------------------------------------------------------ config + secrets
def feed() -> dict:
    f = dict(DEFAULT_FEED)
    blk = C.feed_cfg(None, PROVIDER)
    if blk:
        f.update({k: v for k, v in blk.items() if not str(k).startswith("_")})
    f["secrets"] = {**DEFAULT_FEED["secrets"], **(blk.get("secrets") if blk and isinstance(blk.get("secrets"), dict) else {})}
    return f


def store_secret(name: str, value: str) -> None:
    """Credential Manager write. Only the Link flow (access tokens) and setup_creds.py call this."""
    import keyring
    keyring.set_password(KEYRING_SERVICE, name, value)


def clear_secret(name: str) -> None:
    import keyring
    try:
        keyring.delete_password(KEYRING_SERVICE, name)
    except Exception:  # noqa: BLE001  (already gone)
        pass


def creds(f: dict | None = None) -> tuple:
    f = f or feed()
    return secret(f["secrets"]["client_id"]), secret(f["secrets"]["secret"])


def state() -> dict:
    st = C.load_json(STATE, {}) or {}
    st.setdefault("items", {})
    st.setdefault("pulls", [])
    return st


def save_state(st: dict) -> None:
    save_json(STATE, st)


def _status(**kw) -> None:
    st = C.load_json(STATUS, {}) or {}
    st.update(kw)
    save_json(STATUS, st)


# ------------------------------------------------------------------ the client (allow-listed POSTs)
class Client:
    """Every Plaid call is a JSON POST carrying client_id + secret. `post` asserts the path against the
    allow-list and refuses the two setup paths unless the caller is the Link flow (setup=True)."""

    def __init__(self, env: str, client_id: str, secret_: str, opener=None):
        if env not in ENV_URLS:
            raise ValueError(f"unknown Plaid environment {env!r}")
        self.env, self.base = env, ENV_URLS[env]
        self._id, self._secret = client_id, secret_
        self._open = opener or urllib.request.urlopen
        self.calls = 0

    def post(self, path: str, body: dict, setup: bool = False) -> dict:
        assert path in ALLOWED_PATHS, f"money lane: {path} is not a read-only Plaid endpoint; refused"
        assert path in READ_PATHS or setup, f"money lane: {path} is only allowed inside the Link flow"
        assert urllib.parse.urlsplit(self.base + path).path == path
        data = json.dumps({"client_id": self._id, "secret": self._secret, **body}).encode("utf-8")
        req = urllib.request.Request(self.base + path, data=data, method="POST",
                                     headers={"Content-Type": "application/json", "Plaid-Version": PLAID_VERSION, "User-Agent": USER_AGENT})
        assert req.get_method() == "POST"
        self.calls += 1
        try:
            with self._open(req, timeout=90) as r:
                return json.loads(r.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            try:
                err = json.loads(e.read().decode("utf-8"))
            except Exception:  # noqa: BLE001
                err = {}
            raise PlaidError(str(err.get("error_code") or f"HTTP_{e.code}"), str(err.get("error_message") or e.reason)[:200],
                             e.code, str(err.get("error_type") or "")) from None


def make_client(f: dict | None = None, env: str = "", opener=None) -> Client | None:
    f = f or feed()
    cid, sec = creds(f)
    if not cid or not sec:
        return None
    return Client(env or str(f.get("env") or "production"), cid, sec, opener=opener)


# ------------------------------------------------------------------ setup: Link (the owner signs in at Plaid, never here)
def link_token(client: Client, f: dict, access_token: str = "") -> str:
    """POST /link/token/create. With access_token: update mode (re-sign-in for an existing item)."""
    body = {"client_name": str(f.get("client_name") or DEFAULT_FEED["client_name"]), "user": {"client_user_id": "owner"},
            "country_codes": list(f.get("country_codes") or ["US"]), "language": str(f.get("language") or "en")}
    if access_token:
        body["access_token"] = access_token
    else:
        body["products"] = list(f.get("products") or ["transactions"])
        body["transactions"] = {"days_requested": int(f.get("days_requested") or 730)}
    if f.get("redirect_uri"):
        body["redirect_uri"] = str(f["redirect_uri"])
    r = client.post("/link/token/create", body, setup=True)
    tok = str(r.get("link_token") or "")
    if not tok:
        raise PlaidError("NO_LINK_TOKEN", "Plaid returned no link_token")
    return tok


def exchange(client: Client, public_token: str) -> tuple:
    r = client.post("/item/public_token/exchange", {"public_token": str(public_token)}, setup=True)
    at, iid = str(r.get("access_token") or ""), str(r.get("item_id") or "")
    if not at or not iid:
        raise PlaidError("NO_ACCESS_TOKEN", "Plaid returned no access token")
    return at, iid


def on_public_token(client: Client, f: dict, st: dict, public_token: str, meta: dict | None = None, update_item: str = "") -> dict:
    """What the Link page hands back -> access token in Credential Manager + an item row in the state.
    Update mode (re-sign-in) keeps the existing item and its cursor; nothing is exchanged."""
    meta = meta or {}
    inst = (meta.get("institution") or {}) if isinstance(meta.get("institution"), dict) else {}
    if update_item:
        row = st["items"].setdefault(update_item, {})
        row.update({"relinked": now_iso(), "error": ""})
        return {"ok": True, "item_id": update_item, "update": True}
    at, iid = exchange(client, public_token)
    store_secret(f["secrets"]["access_token_prefix"] + iid, at)
    prev = st["items"].get(iid) or {}
    st["items"][iid] = {"institution": str(inst.get("name") or prev.get("institution") or ""),
                        "institution_id": str(inst.get("institution_id") or prev.get("institution_id") or ""),
                        "env": client.env, "linked": now_iso(), "cursor": "", "error": "",
                        "accounts": [str(a.get("name") or a.get("id") or "") for a in (meta.get("accounts") or []) if isinstance(a, dict)]}
    C.log("plaid_linked", item=iid, institution=st["items"][iid]["institution"], env=client.env)
    return {"ok": True, "item_id": iid, "institution": st["items"][iid]["institution"]}


LINK_HTML = """<!doctype html><html><head><meta charset="utf-8"><title>Link a bank (read-only)</title>
<style>body{font-family:Poppins,system-ui,sans-serif;background:#FBFFFA;color:#050B07;max-width:560px;margin:48px auto;padding:0 16px}
h1{font-family:'Playfair Display',serif;font-weight:500}button{background:#4A5C43;color:#fff;border:0;border-radius:8px;padding:12px 20px;font-size:16px;cursor:pointer}
p.small{color:#3F503A;font-size:14px}#out{margin-top:20px;font-size:15px}</style></head><body>
<h1>Link a bank, read-only</h1>
<p>Plaid's own window opens next. Sign in to your bank <b>there</b>; this page never sees the login. The assistant will only be able to read balances and transactions. It cannot move money.</p>
<button id="go">Open Plaid Link</button>
<p class="small">Environment: __ENV__ __MODE__</p>
<div id="out"></div>
<script src="__LINK_JS__"></script>
<script>
const out = document.getElementById('out');
const token = __TOKEN__;
const cfg = {token: token,
  onSuccess: function(public_token, metadata) {
    out.textContent = 'Linked. Storing the read-only access token on this PC...';
    fetch('/exchange', {method: 'POST', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({public_token: public_token, metadata: metadata})})
      .then(r => r.json()).then(j => { out.textContent = j.ok ? ('Done: ' + (j.institution || 'bank') + ' is linked. You can close this tab; the first pull runs now.') : ('Could not store the link: ' + (j.error || '?')); })
      .catch(e => { out.textContent = 'Could not reach the assistant: ' + e; });
  },
  onExit: function(err, metadata) {
    fetch('/exit', {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({error: err, metadata: metadata})});
    out.textContent = err ? ('Plaid Link closed with an error: ' + (err.display_message || err.error_code || '') + '. Close this tab and run setup again.') : 'Closed without linking. Close this tab and run setup again when ready.';
  }};
if (window.location.href.indexOf('oauth_state_id=') >= 0) { cfg.receivedRedirectUri = window.location.href; }
let handler = null;
try { handler = Plaid.create(cfg); } catch (e) { out.textContent = 'Plaid Link could not load: ' + e; }
document.getElementById('go').onclick = function() { if (handler) handler.open(); };
if (cfg.receivedRedirectUri && handler) { handler.open(); }
</script></body></html>"""


def link_page(token: str, env: str, update: bool = False) -> str:
    return (LINK_HTML.replace("__TOKEN__", json.dumps(token)).replace("__ENV__", env)
            .replace("__MODE__", "(re-sign-in)" if update else "").replace("__LINK_JS__", LINK_JS))


def serve_link(client: Client, f: dict, st: dict, token: str, update_item: str = "", port: int = 0,
               timeout_s: float = 900, open_browser: bool = True, result: dict | None = None) -> dict:
    """A one-page site on 127.0.0.1 that hosts Plaid Link and receives the public token. Returns the result
    of on_public_token (or {"ok": False, "error": ...}). Loopback only; stops after the first result or
    the timeout. `result` may be a caller's dict: it gets "port" and "url" as soon as the server listens
    (tests run this in a thread, open_browser=False, and talk to that port)."""
    result = result if result is not None else {}
    done = threading.Event()
    env, update = client.env, bool(update_item)

    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):  # quiet
            pass

        def _send(self, code: int, body: bytes, ctype: str = "text/html; charset=utf-8"):
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            if urllib.parse.urlsplit(self.path).path not in ("/", "/index.html"):
                return self._send(404, b"not found", "text/plain")
            self._send(200, link_page(token, env, update).encode("utf-8"))

        def do_POST(self):
            n = int(self.headers.get("Content-Length") or 0)
            try:
                body = json.loads(self.rfile.read(n).decode("utf-8") or "{}")
            except ValueError:
                body = {}
            path = urllib.parse.urlsplit(self.path).path
            if path == "/exchange":
                try:
                    r = on_public_token(client, f, st, str(body.get("public_token") or ""), body.get("metadata") or {}, update_item)
                except (PlaidError, AssertionError, ValueError, OSError) as e:
                    r = {"ok": False, "error": str(e)[:200]}
                result.update(r)
                self._send(200, json.dumps(r).encode("utf-8"), "application/json")
                done.set()
            elif path == "/exit":
                err = body.get("error") or {}
                result.update({"ok": False, "error": "closed: " + str((err or {}).get("error_code") or (err or {}).get("display_message") or "no link made")[:160]})
                self._send(200, b'{"ok":false}', "application/json")
                done.set()
            else:
                self._send(404, b"not found", "text/plain")

    srv = ThreadingHTTPServer(("127.0.0.1", int(port)), H)
    srv.daemon_threads = True
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    url = f"http://127.0.0.1:{srv.server_address[1]}/"
    if f.get("redirect_uri") and "localhost" in str(f["redirect_uri"]):
        url = str(f["redirect_uri"]).rstrip("/") + "/"       # OAuth banks come back to the registered host name
    result["port"] = srv.server_address[1]
    result["url"] = url
    if open_browser:
        print(f"opening {url}  (sign in to the bank inside Plaid's window; nothing is typed here)")
        try:
            webbrowser.open(url)
        except Exception:  # noqa: BLE001
            print("could not open a browser; paste the address above into one")
    done.wait(timeout=timeout_s)
    srv.shutdown()
    srv.server_close()
    if not done.is_set():
        result.update({"ok": False, "error": f"no link within {int(timeout_s)} s"})
    return result


def setup(update_item: str = "", env: str = "", open_browser: bool = True, opener=None, port: int | None = None, timeout_s: float | None = None) -> dict:
    f = feed()
    client = make_client(f, env=env, opener=opener)
    if client is None:
        print("NOT_CONFIGURED: run  python assistant/scripts/setup_creds.py money plaid   (client id + secret from dashboard.plaid.com)")
        return {"ok": False, "reason": "not_configured"}
    st = state()
    access = ""
    if update_item:
        access = secret(f["secrets"]["access_token_prefix"] + update_item) or ""
        if not access:
            print(f"UNKNOWN_ITEM: no access token stored for {update_item}; run setup without --update to link afresh")
            return {"ok": False, "reason": "unknown_item"}
    try:
        tok = link_token(client, f, access_token=access)
    except PlaidError as e:
        hint = ""
        if "redirect_uri" in e.message.lower():
            hint = "  (register the redirect URI in dashboard.plaid.com > Team Settings > API, or clear money.json feeds[plaid].redirect_uri)"
        print(f"LINK_TOKEN_ERROR {e}{hint}")
        _status(error=f"Link could not start: {e.code}", configured=True)
        return {"ok": False, "reason": e.code}
    r = serve_link(client, f, st, tok, update_item=update_item, port=int(f.get("link_port") or 0) if port is None else port,
                   timeout_s=float(f.get("link_timeout_s") or 900) if timeout_s is None else timeout_s, open_browser=open_browser)
    if r.get("ok"):
        save_state(st)
        _status(error="", configured=True, items=len(st["items"]))
        print("LINKED " + json.dumps({k: v for k, v in r.items() if k in ("item_id", "institution", "update")}))
        print("next: python assistant/scripts/money/plaid.py --full   (first pull)")
    else:
        print("NOT_LINKED " + str(r.get("error") or ""))
    return r


def unlink(item_id: str) -> dict:
    """Forget the local access token + item row. Nothing is sent to Plaid (no /item/remove here, ever);
    the owner removes the connection at my.plaid.com or the Plaid dashboard if they want it gone there too."""
    f, st = feed(), state()
    clear_secret(f["secrets"]["access_token_prefix"] + item_id)
    st["items"].pop(item_id, None)
    save_state(st)
    C.log("plaid_unlinked", item=item_id)
    print(f"forgot {item_id} locally (remove the connection at my.plaid.com if you want Plaid to drop it too)")
    return {"ok": True}


# ------------------------------------------------------------------ pull: /transactions/sync + /accounts/balance/get
def sync_item(client: Client, access_token: str, cursor: str, count: int = 500) -> dict:
    """Walk /transactions/sync from `cursor` to the end. Returns {added, modified, removed, next_cursor,
    accounts}. A mutation mid-pagination restarts from the ORIGINAL cursor (Plaid's documented rule)."""
    for attempt in range(3):
        cur, added, modified, removed, accounts = cursor, [], [], [], []
        try:
            while True:
                body = {"access_token": access_token, "count": int(count), "options": {"include_personal_finance_category": True}}
                if cur:
                    body["cursor"] = cur
                r = client.post("/transactions/sync", body)
                added += r.get("added") or []
                modified += r.get("modified") or []
                removed += r.get("removed") or []
                accounts = r.get("accounts") or accounts
                cur = str(r.get("next_cursor") or cur)
                if not r.get("has_more"):
                    return {"added": added, "modified": modified, "removed": removed, "next_cursor": cur, "accounts": accounts,
                            "update_status": str(r.get("transactions_update_status") or "")}
        except PlaidError as e:
            if e.code == "TRANSACTIONS_SYNC_MUTATION_DURING_PAGINATION" and attempt < 2:
                continue
            raise
    raise PlaidError("SYNC_RESTARTED", "transactions changed under pagination three times; try again later")


def balances(client: Client, access_token: str) -> dict:
    return client.post("/accounts/balance/get", {"access_token": access_token})


def _kind(name: str, org: str, aid: str, c: dict, atype: str = "") -> tuple:
    """money.json -> accounts (overrides by id, then name patterns), the institution name as a fallback.
    Plaid also says what an account IS: a `credit` account is a card whatever its name ("Relay Visa"
    would otherwise match the relay -> operating pattern); its side follows the name match."""
    kind, side = C.account_kind(name, aid, c)
    if kind == "unknown" and org:
        kind, side = C.account_kind(f"{org} {name}", aid, c)
    if atype == "credit" and not (c["accounts"].get("overrides") or {}).get(aid):
        return "card", side if kind in ("operating", "tax", "savings") else side
    return kind, side


def merge(led: dict, item_id: str, org: str, accounts: list, added: list, modified: list | None = None,
          removed: list | None = None, c: dict | None = None, as_of: str = "") -> dict:
    """Fold one item's balances + sync delta into the ledger. Pure: no network, no disk. Same row shape as
    simplefin.merge, plus feed/item tags on the account. Plaid amounts are flipped (outflow negative).
    A removed id that a new row names as its pending_transaction_id hands its bucket to that row."""
    c = c or C.cfg()
    as_of = as_of or date.today().isoformat()
    led.setdefault("accounts", {})
    led.setdefault("transactions", {})
    new_t = upd_t = gone = 0
    for a in accounts or []:
        aid = str(a.get("account_id") or "")
        if not aid:
            continue
        name = str(a.get("name") or a.get("official_name") or aid)
        kind, side = _kind(name, org, aid, c, str(a.get("type") or ""))
        prev = led["accounts"].get(aid) or {}
        b = a.get("balances") or {}
        try:
            bal = float(b.get("current"))
        except (TypeError, ValueError):
            bal = prev.get("balance")
        if bal is not None and str(a.get("type") or "") in ("credit", "loan"):
            bal = -bal                                   # Plaid reports what is OWED as positive; the ledger keeps net sign
        try:
            avail = float(b.get("available")) if b.get("available") is not None else prev.get("available")
        except (TypeError, ValueError):
            avail = prev.get("available")
        bd = str(b.get("last_updated_datetime") or "")[:10] or as_of
        led["accounts"][aid] = {"id": aid, "name": name, "org": str(org or ""), "kind": kind, "side": side,
                               "currency": str(b.get("iso_currency_code") or prev.get("currency") or "USD"),
                               "balance": bal, "available": avail, "balance_date": bd,
                               "first_seen": prev.get("first_seen") or now_iso(),
                               "feed": PROVIDER, "item_id": item_id, "mask": str(a.get("mask") or ""),
                               "type": str(a.get("type") or ""), "subtype": str(a.get("subtype") or "")}
    carried = {}
    for t in list(added or []) + list(modified or []):
        tid = str(t.get("transaction_id") or "")
        aid = str(t.get("account_id") or "")
        if not tid or not aid:
            continue
        try:
            amt = round(-float(t.get("amount")), 2)
        except (TypeError, ValueError):
            continue
        posted = str(t.get("date") or t.get("authorized_date") or "")[:10]
        desc = str(t.get("merchant_name") or t.get("name") or "").strip()
        pfc = t.get("personal_finance_category") or {}
        row = {"id": tid, "account_id": aid, "posted": posted, "amount": amt, "description": desc,
               "payee": str(t.get("merchant_name") or ""), "memo": str(pfc.get("detailed") or "") if isinstance(pfc, dict) else "",
               "pending": bool(t.get("pending"))}
        old = led["transactions"].get(tid)
        if old:
            changed = any(old.get(k) != v for k, v in row.items())
            row = {**old, **row}
            upd_t += changed
        else:
            row.update({"bucket": "", "bucket_src": "none", "rule_id": "", "seen": now_iso()})
            pend = str(t.get("pending_transaction_id") or "")
            pold = led["transactions"].get(pend) if pend else None
            if pold:
                row.update({k: pold.get(k, row[k]) for k in ("bucket", "bucket_src", "rule_id")})
                carried[pend] = tid
            new_t += 1
        led["transactions"][tid] = row
    for rm in removed or []:
        rid = str(rm.get("transaction_id") if isinstance(rm, dict) else rm)
        if led["transactions"].pop(rid, None) is not None:
            gone += 1
    for pend in carried:
        if led["transactions"].pop(pend, None) is not None:
            gone += 1
    return {"accounts": len(accounts or []), "new": new_t, "updated": upd_t, "removed": gone, "carried": len(carried)}


def pull(dry: bool = False, full: bool = False, client: Client | None = None, env: str = "", today: date | None = None, opener=None) -> dict:
    c = C.cfg()
    f = feed()
    client = client or make_client(f, env=env, opener=opener)
    if client is None:
        _status(error="", last_ok=(C.load_json(STATUS, {}) or {}).get("last_ok", ""), configured=False, items=0)
        print("NOT_CONFIGURED: run  python assistant/scripts/setup_creds.py money plaid")
        return {"ok": False, "reason": "not_configured"}
    st = state()
    items = {k: v for k, v in st["items"].items() if not env or str(v.get("env") or "production") == env}
    if not items:
        _status(error="", configured=True, items=0, last_ok=(C.load_json(STATUS, {}) or {}).get("last_ok", ""))
        print("NOT_LINKED: run  python assistant/scripts/money/plaid.py setup   (Relay signs in inside Plaid Link)")
        return {"ok": False, "reason": "not_linked"}
    today = today or date.today()
    pulls = [p for p in st.get("pulls") or [] if p[:10] == today.isoformat()]
    if not dry and len(pulls) >= int(f.get("max_pulls_per_day") or 6):
        print(f"QUOTA: {len(pulls)} pulls today already (feeds[plaid].max_pulls_per_day)")
        return {"ok": False, "reason": "quota"}
    led = C.ledger()
    totals = {"items": 0, "accounts": 0, "new": 0, "updated": 0, "removed": 0, "errors": []}
    for iid, row in items.items():
        access = secret(f["secrets"]["access_token_prefix"] + iid)
        if not access:
            totals["errors"].append(f"{iid}: no access token stored (run setup)")
            continue
        try:
            s = sync_item(client, access, "" if full else str(row.get("cursor") or ""), int(f.get("sync_count") or 500))
            b = balances(client, access)
        except PlaidError as e:
            msg = f"{row.get('institution') or iid}: {e.code}"
            if e.code == "ITEM_LOGIN_REQUIRED":
                msg += f" - sign in again: python assistant/scripts/money/plaid.py setup --update {iid}"
            elif e.code in ("INVALID_API_KEYS", "INVALID_ACCESS_TOKEN"):
                msg += " - the Plaid keys or token are no longer valid"
            totals["errors"].append(msg)
            if not dry:
                row["error"] = msg
            C.log("plaid_pull_error", item=iid, error=msg)
            continue
        except (OSError, ValueError, AssertionError) as e:
            msg = f"{row.get('institution') or iid}: Plaid unreachable: {type(e).__name__}: {str(e)[:100]}"
            totals["errors"].append(msg)
            C.log("plaid_pull_error", item=iid, error=msg)
            continue
        org = str(row.get("institution") or "")
        r = merge(led, iid, org, b.get("accounts") or s.get("accounts") or [], s["added"], s["modified"], s["removed"], c, as_of=today.isoformat())
        totals["items"] += 1
        totals["accounts"] += r["accounts"]
        totals["new"] += r["new"]
        totals["updated"] += r["updated"]
        totals["removed"] += r["removed"]
        if not dry:
            row.update({"cursor": s["next_cursor"], "last_ok": now_iso(), "error": "",
                        "accounts": [str(a.get("name") or "") for a in (b.get("accounts") or [])],
                        "update_status": s.get("update_status", "")})
    if dry:
        print("DRY " + json.dumps(totals))
        return {"ok": True, "dry": True, **totals}
    ok = totals["items"] > 0
    if ok:
        led.setdefault("feeds", {})[PROVIDER] = {"last_ok": today.isoformat(), "at": now_iso(), "items": totals["items"], "errors": totals["errors"][:5]}
        C.save_ledger(led)
    st["pulls"] = [p for p in st.get("pulls") or [] if p[:10] >= today.isoformat()] + [today.isoformat() + "T" + time.strftime("%H:%M:%S")]
    save_state(st)
    err = "; ".join(totals["errors"][:3])
    _status(**({"last_ok": now_iso()} if ok else {}), error=err, configured=True, items=len(st["items"]),
            accounts=sum(1 for a in led["accounts"].values() if a.get("feed") == PROVIDER),
            transactions=sum(1 for t in led["transactions"].values() if (led["accounts"].get(t.get("account_id")) or {}).get("feed") == PROVIDER))
    C.log("plaid_pull", **{k: v for k, v in totals.items() if k != "errors"}, errors=len(totals["errors"]))
    print(("PULL " if ok else "PULL_ERROR ") + json.dumps({k: (v if k != "errors" else v[:3]) for k, v in totals.items()}))
    return {"ok": ok, **totals}


def status() -> dict:
    led = C.ledger()
    s = C.load_json(STATUS, {}) or {}
    st = state()
    accts = [a for a in led["accounts"].values() if a.get("feed") == PROVIDER]
    return {"configured": bool(s.get("configured")), "last_ok": s.get("last_ok", ""), "error": s.get("error", ""),
            "items": {k: {"institution": v.get("institution", ""), "env": v.get("env", ""), "linked": v.get("linked", ""),
                          "last_ok": v.get("last_ok", ""), "error": v.get("error", "")} for k, v in st["items"].items()},
            "accounts": [{"name": a["name"], "kind": a["kind"], "balance": a.get("balance"), "as_of": a.get("balance_date")} for a in accts],
            "transactions": sum(1 for t in led["transactions"].values() if (led["accounts"].get(t.get("account_id")) or {}).get("feed") == PROVIDER)}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("cmd", nargs="?", default="pull", choices=["pull", "status", "setup", "unlink"])
    ap.add_argument("item", nargs="?", default="")
    ap.add_argument("--update", default="", help="setup: re-sign-in for this item id (ITEM_LOGIN_REQUIRED)")
    ap.add_argument("--env", default="", choices=["", "sandbox", "production"])
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--full", action="store_true")
    ap.add_argument("--no-browser", action="store_true")
    a = ap.parse_args()
    if a.cmd == "status":
        print(json.dumps(status(), indent=1))
        return 0
    ensure_venv()
    if a.cmd == "setup":
        return 0 if setup(update_item=a.update, env=a.env, open_browser=not a.no_browser).get("ok") else 1
    if a.cmd == "unlink":
        if not a.item:
            print("usage: plaid.py unlink <item_id>")
            return 2
        unlink(a.item)
        return 0
    r = pull(dry=a.dry_run, full=a.full, env=a.env)
    return 0 if r.get("ok") or r.get("reason") in ("not_configured", "not_linked", "quota") else 1


if __name__ == "__main__":
    sys.exit(main())

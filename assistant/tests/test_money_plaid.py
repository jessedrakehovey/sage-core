"""Offline tests for the Plaid feed adapter (assistant/scripts/money/plaid.py): the endpoint allow-list, the
Link flow on a loopback server (fake Plaid behind the real Client), cursor pagination, the merge into the
shared ledger (sign flip, Relay kinds, pending -> posted bucket carry, removals), pull / quota / status file,
re-sign-in errors, and the done-criterion: Relay accounts show in simplefin.py status next to the SimpleFIN
ones and categorize.py places them. Scratch ASSISTANT_DATA; a dict stands in for Credential Manager;
nothing touches a bank or Plaid."""
import os as _os, tempfile as _tf
_os.environ.setdefault("WV_LOG_DIR", _tf.mkdtemp(prefix="wv_logs_"))
import io
import json
import os
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
from datetime import date, timedelta
from pathlib import Path

HERE = Path(__file__).resolve().parent
TMP = Path(tempfile.mkdtemp(prefix="wv_plaid_"))
os.environ["ASSISTANT_DATA"] = str(TMP)
os.environ["ASSISTANT_CARDS"] = str(TMP / "cards")
os.environ["CHAT_PUSH"] = "off"
os.environ["AGENT_HOME_SPACE"] = ""
sys.path.insert(0, str(HERE.parent / "scripts"))
sys.path.insert(0, str(HERE.parent / "scripts" / "money"))
import common as C  # noqa: E402
import plaid  # noqa: E402
import simplefin  # noqa: E402
import categorize  # noqa: E402

FAILS = []
TODAY = date(2026, 10, 16)


def check(name, cond, extra=""):
    print(("ok   " if cond else "FAIL ") + name + (f"  {extra}" if extra and not cond else ""))
    if not cond:
        FAILS.append(name)


RULES_SRC = C.RULES_PATH
C.RULES_PATH = TMP / "money_rules.json"
C.RULES_PATH.write_text(RULES_SRC.read_text(encoding="utf-8-sig"), encoding="utf-8")
c = C.cfg()
C.cfg = lambda: c

# ------------------------------------------------------------------ config: feeds list, back-compat
check("money.json lists both feeds", [f["provider"] for f in c["feeds"]] == ["simplefin", "plaid"], c["feeds"])
check("cfg()['feed'] is still the SimpleFIN block", c["feed"]["provider"] == "simplefin" and c["feed"]["secret"] == "simplefin:access_url")
f = plaid.feed()
check("plaid feed block: env, secrets, products", f["env"] == "production" and f["secrets"]["access_token_prefix"] == "plaid:access_token:"
      and f["products"] == ["transactions"] and f["max_pulls_per_day"] == 6, f)
check("feed_cfg(None, 'teller') is None", C.feed_cfg(c, "teller") is None)

# ------------------------------------------------------------------ Credential Manager stand-in
VAULT = {}
plaid.secret = lambda name: VAULT.get(name)
plaid.store_secret = lambda name, val: VAULT.__setitem__(name, val)
plaid.clear_secret = lambda name: VAULT.pop(name, None)


# ------------------------------------------------------------------ fake Plaid behind the REAL Client (asserts + HTTP path run for real)
class _Resp:
    def __init__(self, body): self.body = body
    def read(self): return self.body
    def __enter__(self): return self
    def __exit__(self, *a): return False


ACCOUNTS = [
    {"account_id": "rel-op", "name": "Operating", "official_name": "Relay Operating", "mask": "0001", "type": "depository", "subtype": "checking",
     "balances": {"current": 5120.55, "available": 5120.55, "iso_currency_code": "USD"}},
    {"account_id": "rel-tax", "name": "Tax", "mask": "0002", "type": "depository", "subtype": "savings", "balances": {"current": 2100.0, "available": 2100.0, "iso_currency_code": "USD"}},
    {"account_id": "rel-sav", "name": "Savings", "mask": "0003", "type": "depository", "subtype": "savings", "balances": {"current": 900.0, "available": 900.0, "iso_currency_code": "USD"}},
    {"account_id": "rel-card", "name": "Relay Visa", "mask": "0004", "type": "credit", "subtype": "credit card", "balances": {"current": 250.0, "available": 4750.0, "iso_currency_code": "USD"}},
]


def tx(tid, aid, days_ago, plaid_amount, name, pending=False, pending_id="", merchant=""):
    d = (TODAY - timedelta(days=days_ago)).isoformat()
    row = {"transaction_id": tid, "account_id": aid, "amount": plaid_amount, "date": d, "authorized_date": d, "name": name,
           "merchant_name": merchant, "pending": pending, "iso_currency_code": "USD",
           "personal_finance_category": {"primary": "GENERAL_SERVICES", "detailed": "GENERAL_SERVICES_OTHER"}}
    if pending_id:
        row["pending_transaction_id"] = pending_id
    return row


PAGE1 = [tx("t-hb1", "rel-op", 3, -2400.0, "HONEYBOOK PAYMENT 8821"), tx("t-adobe", "rel-op", 4, 69.99, "ADOBE *CREATIVE CLOUD", merchant="Adobe"),
         tx("t-xfer-out", "rel-op", 2, 600.0, "Transfer to Tax"), tx("t-xfer-in", "rel-tax", 1, -600.0, "Transfer from Operating"),
         tx("t-coffee-p", "rel-op", 1, 6.50, "BLUE BOTTLE COFFEE", pending=True)]
PAGE2 = [tx("t-irs", "rel-tax", 100, 900.0, "IRS USATAXPYMT"), tx("t-card", "rel-card", 5, 120.0, "MUSICBED SUBSCRIPTION")]


class Fake:
    """Routes the real Client's POSTs. `pages`: the /transactions/sync answers in order (cursor -> page);
    `fail`: {path: (http, error_code)} to raise once."""

    def __init__(self):
        self.calls, self.fail = [], {}
        self.sync_script = [{"added": PAGE1, "modified": [], "removed": [], "next_cursor": "c1", "has_more": True},
                            {"added": PAGE2, "modified": [], "removed": [], "next_cursor": "c2", "has_more": False}]
        self.sync_by_cursor = {}
        self.accounts = ACCOUNTS

    def __call__(self, req, timeout=0):
        path = urllib.request.urlparse(req.full_url).path
        body = json.loads(req.data.decode("utf-8"))
        self.calls.append((req.get_method(), path, body, dict(req.header_items())))
        if path in self.fail:
            http, code = self.fail.pop(path)
            err = json.dumps({"error_type": "ITEM_ERROR", "error_code": code, "error_message": f"fake {code}"}).encode()
            raise urllib.error.HTTPError(req.full_url, http, "Bad Request", {}, io.BytesIO(err))
        if path == "/link/token/create":
            return _Resp(json.dumps({"link_token": "link-sandbox-abc", "expiration": "2026-10-16T20:00:00Z"}).encode())
        if path == "/item/public_token/exchange":
            assert body["public_token"] == "public-sandbox-xyz"
            return _Resp(json.dumps({"access_token": "access-sandbox-SECRET", "item_id": "item-relay-1"}).encode())
        if path == "/accounts/balance/get":
            assert body["access_token"] == "access-sandbox-SECRET"
            return _Resp(json.dumps({"accounts": self.accounts, "item": {"item_id": "item-relay-1", "institution_id": "ins_relay"}}).encode())
        if path == "/transactions/sync":
            cur = body.get("cursor", "")
            if cur in self.sync_by_cursor:
                page = self.sync_by_cursor[cur]
            else:
                page = {"": self.sync_script[0], "c1": self.sync_script[1]}.get(cur) or {"added": [], "modified": [], "removed": [], "next_cursor": cur or "c2", "has_more": False}
            return _Resp(json.dumps({**page, "accounts": self.accounts, "transactions_update_status": "HISTORICAL_UPDATE_COMPLETE"}).encode())
        raise AssertionError(f"fake plaid: unexpected path {path}")


fake = Fake()
client = plaid.Client("sandbox", "cid", "sec", opener=fake)

# ------------------------------------------------------------------ allow-list by construction
for bad in ("/item/remove", "/transfer/create", "/transfer/authorization/create", "/processor/token/create", "/item/webhook/update", "/accounts/get"):
    try:
        client.post(bad, {})
        check(f"refuses {bad}", False)
    except AssertionError:
        check(f"refuses {bad}", True)
try:
    client.post("/link/token/create", {})
    check("setup paths refused outside the Link flow", False)
except AssertionError:
    check("setup paths refused outside the Link flow", True)
check("no request left the fake for a refused path", not fake.calls)
try:
    plaid.Client("development", "cid", "sec")
    check("unknown environment refused", False)
except ValueError:
    check("unknown environment refused", True)

# ------------------------------------------------------------------ link token + exchange + state
tok = plaid.link_token(client, f)
m, p_, body, hdr = fake.calls[-1]
check("link token: POST /link/token/create with products, days_requested, no redirect_uri when empty",
      tok == "link-sandbox-abc" and p_ == "/link/token/create" and body["products"] == ["transactions"] and body["transactions"]["days_requested"] == 730
      and "redirect_uri" not in body and body["client_id"] == "cid" and hdr.get("Plaid-version") == plaid.PLAID_VERSION, body)
plaid.link_token(client, f, access_token="access-old")
body = fake.calls[-1][2]
check("update mode carries access_token and no products", body.get("access_token") == "access-old" and "products" not in body, body)
f2 = {**f, "redirect_uri": "http://localhost:8765/"}
plaid.link_token(client, f2)
check("redirect_uri sent only when configured", fake.calls[-1][2].get("redirect_uri") == "http://localhost:8765/")

st = plaid.state()
r = plaid.on_public_token(client, f, st, "public-sandbox-xyz", {"institution": {"name": "Relay", "institution_id": "ins_relay"},
                                                                 "accounts": [{"id": "rel-op", "name": "Operating"}]})
check("exchange stores the access token under the prefix and records the item", r["ok"] and r["item_id"] == "item-relay-1"
      and VAULT.get("plaid:access_token:item-relay-1") == "access-sandbox-SECRET" and st["items"]["item-relay-1"]["institution"] == "Relay"
      and st["items"]["item-relay-1"]["cursor"] == "" and st["items"]["item-relay-1"]["env"] == "sandbox", (r, st))
check("the access token is never in the state file", "SECRET" not in json.dumps(st))

# ------------------------------------------------------------------ the Link page on loopback (no browser)
st2 = plaid.state()
res = {}
th = threading.Thread(target=lambda: plaid.serve_link(client, f, st2, "link-sandbox-abc", port=0, timeout_s=20, open_browser=False, result=res), daemon=True)
th.start()
for _ in range(100):
    if res.get("port"):
        break
    time.sleep(0.05)
port = res.get("port")
page = urllib.request.urlopen(f"http://127.0.0.1:{port}/", timeout=5).read().decode("utf-8") if port else ""
check("link page serves Plaid Link with the token and never a bank field", port and "link-sandbox-abc" in page and plaid.LINK_JS in page
      and "receivedRedirectUri" in page and "<input" not in page.lower(), page[:200])
req = urllib.request.Request(f"http://127.0.0.1:{port}/exchange", data=json.dumps({"public_token": "public-sandbox-xyz", "metadata": {"institution": {"name": "Relay"}}}).encode(),
                             headers={"Content-Type": "application/json"}, method="POST")
ans = json.loads(urllib.request.urlopen(req, timeout=5).read().decode("utf-8"))
th.join(timeout=10)
check("page -> /exchange -> token stored, server stops", ans.get("ok") and res.get("ok") and res.get("item_id") == "item-relay-1" and not th.is_alive(), (ans, res))
check("loopback page is 127.0.0.1 only", res.get("url", "").startswith("http://127.0.0.1:"))

# ------------------------------------------------------------------ sync pagination + mutation restart
fake.calls.clear()
s = plaid.sync_item(client, "access-sandbox-SECRET", "", count=500)
syncs = [x for x in fake.calls if x[1] == "/transactions/sync"]
check("sync walks every page and lands on the last cursor", len(syncs) == 2 and s["next_cursor"] == "c2" and len(s["added"]) == len(PAGE1) + len(PAGE2)
      and "cursor" not in syncs[0][2] and syncs[1][2]["cursor"] == "c1" and syncs[0][2]["count"] == 500, (len(syncs), s["next_cursor"]))
fake.calls.clear()
fake.fail["/transactions/sync"] = (400, "TRANSACTIONS_SYNC_MUTATION_DURING_PAGINATION")
s = plaid.sync_item(client, "access-sandbox-SECRET", "", count=500)
check("a mutation mid-pagination restarts from the original cursor", len(s["added"]) == len(PAGE1) + len(PAGE2)
      and [x[2].get("cursor", "") for x in fake.calls] == ["", "", "c1"], [x[2].get("cursor", "") for x in fake.calls])

# ------------------------------------------------------------------ merge: shape, signs, kinds, pending -> posted, removals
led = C.ledger()
r = plaid.merge(led, "item-relay-1", "Relay", ACCOUNTS, PAGE1 + PAGE2, c=c, as_of=TODAY.isoformat())
A = led["accounts"]
check("merge: 4 accounts, every txn new", r["accounts"] == 4 and r["new"] == len(PAGE1) + len(PAGE2) and r["removed"] == 0, r)
check("Relay buckets by name -> operating / tax / savings, side business, feed plaid",
      A["rel-op"]["kind"] == "operating" and A["rel-tax"]["kind"] == "tax" and A["rel-sav"]["kind"] == "savings"
      and all(A[k]["side"] == "business" and A[k]["feed"] == "plaid" and A[k]["org"] == "Relay" for k in ("rel-op", "rel-tax", "rel-sav")), {k: (v["kind"], v["side"]) for k, v in A.items()})
check("a Relay credit account is a card (Plaid type beats the relay name pattern), business side, balance owed negative",
      A["rel-card"]["kind"] == "card" and A["rel-card"]["side"] == "business" and A["rel-card"]["balance"] == -250.0, A["rel-card"])
check("balances + as_of + mask carried", A["rel-op"]["balance"] == 5120.55 and A["rel-op"]["balance_date"] == TODAY.isoformat() and A["rel-op"]["mask"] == "0001")
T = led["transactions"]
check("Plaid sign flipped: deposit positive, charge negative", T["t-hb1"]["amount"] == 2400.0 and T["t-adobe"]["amount"] == -69.99 and T["t-xfer-out"]["amount"] == -600.0 and T["t-xfer-in"]["amount"] == 600.0)
check("row shape matches the SimpleFIN rows", set(T["t-adobe"]) >= {"id", "account_id", "posted", "amount", "description", "payee", "memo", "pending", "bucket", "bucket_src", "rule_id", "seen"}
      and T["t-adobe"]["payee"] == "Adobe" and T["t-adobe"]["memo"] == "GENERAL_SERVICES_OTHER" and T["t-coffee-p"]["pending"] is True, T["t-adobe"])
r2 = plaid.merge(led, "item-relay-1", "Relay", ACCOUNTS, PAGE1 + PAGE2, c=c, as_of=TODAY.isoformat())
check("merge is idempotent", r2["new"] == 0 and r2["updated"] == 0 and r2["removed"] == 0, r2)
T["t-coffee-p"].update({"bucket": "biz:meals", "bucket_src": "learned", "rule_id": "L1"})
T["t-hb1"]["bucket"] = "income:honeybook"
posted = tx("t-coffee", "rel-op", 0, 6.50, "BLUE BOTTLE COFFEE 0421", pending_id="t-coffee-p")
r3 = plaid.merge(led, "item-relay-1", "Relay", ACCOUNTS, [posted], removed=[{"transaction_id": "t-coffee-p"}, {"transaction_id": "t-card"}], c=c, as_of=TODAY.isoformat())
check("pending -> posted carries the bucket, drops the pending row; a removed id is dropped",
      "t-coffee-p" not in T and T["t-coffee"]["bucket"] == "biz:meals" and T["t-coffee"]["bucket_src"] == "learned" and "t-card" not in T
      and r3["carried"] == 1 and r3["removed"] == 2 and r3["new"] == 1, (r3, T.get("t-coffee")))
check("a bucket already placed survives a re-merge", plaid.merge(led, "item-relay-1", "Relay", ACCOUNTS, PAGE1[:1], c=c)["updated"] == 0 and T["t-hb1"]["bucket"] == "income:honeybook")

# ------------------------------------------------------------------ pull: not configured / not linked / linked / incremental / quota / dry
C.save_ledger({"accounts": {}, "transactions": {}, "pulled": {}})
plaid.save_state({"items": {}, "pulls": []})
VAULT.clear()
fake.calls.clear()
rp = plaid.pull(opener=fake, today=TODAY)
check("no keys: NOT_CONFIGURED, no call, status file says so", rp.get("reason") == "not_configured" and not fake.calls and C.load_json(plaid.STATUS, {}).get("configured") is False)
VAULT.update({"plaid:client_id": "cid", "plaid:secret": "sec"})
rp = plaid.pull(opener=fake, today=TODAY)
check("keys but no item: NOT_LINKED, no call", rp.get("reason") == "not_linked" and not fake.calls and C.load_json(plaid.STATUS, {}).get("configured") is True)
st = plaid.state()
st["items"]["item-relay-1"] = {"institution": "Relay", "institution_id": "ins_relay", "env": "sandbox", "linked": "2026-10-15T00:00:00Z", "cursor": "", "error": ""}
plaid.save_state(st)
VAULT["plaid:access_token:item-relay-1"] = "access-sandbox-SECRET"
rp = plaid.pull(client=client, today=TODAY)
paths = [x[1] for x in fake.calls]
check("first pull: only /transactions/sync + /accounts/balance/get, ledger written", rp.get("ok") and set(paths) == {"/transactions/sync", "/accounts/balance/get"}
      and rp["new"] == len(PAGE1) + len(PAGE2) and rp["accounts"] == 4, (rp, paths))
led = C.ledger()
check("ledger on disk holds the Relay accounts + feed stamp", len([a for a in led["accounts"].values() if a.get("feed") == "plaid"]) == 4 and led["feeds"]["plaid"]["items"] == 1)
st = plaid.state()
check("cursor saved after a good pull, token still only in the vault", st["items"]["item-relay-1"]["cursor"] == "c2" and st["items"]["item-relay-1"]["last_ok"] and "SECRET" not in json.dumps(st))
sf = C.load_json(plaid.STATUS, {})
check("status file for /setup + conn_check", sf.get("last_ok") and sf.get("configured") and sf.get("items") == 1 and sf.get("accounts") == 4 and sf.get("error") == "", sf)
fake.calls.clear()
rp2 = plaid.pull(client=client, today=TODAY)
syncs = [x for x in fake.calls if x[1] == "/transactions/sync"]
check("second pull is incremental from the saved cursor, nothing new", rp2.get("ok") and syncs and syncs[0][2].get("cursor") == "c2" and rp2["new"] == 0, (rp2, [x[2].get("cursor") for x in syncs]))
fake.calls.clear()
rp3 = plaid.pull(client=client, today=TODAY, dry=True, full=True)
check("dry-run full: fetches from the start, writes nothing", rp3.get("dry") and "cursor" not in [x for x in fake.calls if x[1] == "/transactions/sync"][0][2]
      and plaid.state()["items"]["item-relay-1"]["cursor"] == "c2")
for _ in range(10):
    plaid.pull(client=client, today=TODAY)
check("daily pull quota holds", plaid.pull(client=client, today=TODAY).get("reason") == "quota")
plaid.save_state({**plaid.state(), "pulls": []})

# re-sign-in needed
fake.calls.clear()
fake.fail["/transactions/sync"] = (400, "ITEM_LOGIN_REQUIRED")
rp4 = plaid.pull(client=client, today=TODAY)
sf = C.load_json(plaid.STATUS, {})
check("ITEM_LOGIN_REQUIRED: pull fails soft, status names the re-link command, cursor kept",
      not rp4.get("ok") and "setup --update item-relay-1" in sf.get("error", "") and plaid.state()["items"]["item-relay-1"]["cursor"] == "c2"
      and "ITEM_LOGIN_REQUIRED" in plaid.state()["items"]["item-relay-1"]["error"], (rp4, sf))
plaid.save_state({**plaid.state(), "pulls": []})
rp5 = plaid.pull(client=client, today=TODAY)
check("next good pull clears the error", rp5.get("ok") and C.load_json(plaid.STATUS, {}).get("error") == "")

# ------------------------------------------------------------------ done criterion: alongside SimpleFIN, and categorize places them
led = C.ledger()
simplefin.merge(led, {"errors": [], "accounts": [{"org": {"name": "Chase"}, "id": "chk", "name": "Chase Total Checking", "currency": "USD", "balance": "400.00",
                                                  "balance-date": int(time.time()), "transactions": [{"id": "chk-1", "posted": int(time.time()), "amount": "-14.99", "description": "NETFLIX.COM"}]}]}, c)
C.save_ledger(led)
stt = simplefin.status()
names = {a["name"]: a for a in stt["accounts"]}
check("simplefin.py status lists Relay Operating / Tax / Savings next to the SimpleFIN account, feed tagged",
      {"Operating", "Tax", "Savings", "Chase Total Checking"} <= set(names) and names["Operating"]["feed"] == "plaid" and names["Operating"]["kind"] == "operating"
      and names["Chase Total Checking"]["feed"] == "simplefin" and stt["transactions"] == len(led["transactions"]), names)
categorize.apply(led, c=c)
bk = {t["id"]: t["bucket"] for t in led["transactions"].values()}
check("categorize places the Relay rows: HoneyBook income, Adobe software, IRS federal, the Operating->Tax transfer paired",
      bk["t-hb1"] == "income:honeybook" and bk["t-adobe"] == "biz:software" and bk["t-irs"] == "tax:federal" and bk["t-xfer-out"] == "transfer" and bk["t-xfer-in"] == "transfer"
      and bk["chk-1"] == "personal:subscriptions", bk)
ps = plaid.status()
check("plaid.py status: items + accounts + count, no token", ps["items"]["item-relay-1"]["institution"] == "Relay" and len(ps["accounts"]) == 4 and ps["transactions"] == 7 and "SECRET" not in json.dumps(ps), ps)

# ------------------------------------------------------------------ unlink is local only
fake.calls.clear()
plaid.unlink("item-relay-1")
check("unlink forgets the token + row locally and sends nothing to Plaid", "plaid:access_token:item-relay-1" not in VAULT and "item-relay-1" not in plaid.state()["items"] and not fake.calls)

print()
print("ALL OK" if not FAILS else f"{len(FAILS)} FAILED: " + ", ".join(FAILS))
sys.exit(1 if FAILS else 0)

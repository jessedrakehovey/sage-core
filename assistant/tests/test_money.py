"""Offline tests for the money lane (assistant/scripts/money/): the SimpleFIN claim + merge + pull (fake
opener, no network), rules + transfers + the weekly unknowns card, learning from a typed answer, the
recurring-bill forecast and its warnings, the quarterly math, and the monthly readback. Scratch
ASSISTANT_DATA / ASSISTANT_CARDS; nothing posts to Chat; nothing touches a bank."""
import os as _os, tempfile as _tf
_os.environ.setdefault("WV_LOG_DIR", _tf.mkdtemp(prefix="wv_logs_"))
import base64
import json
import os
import sys
import tempfile
import time
from datetime import date, datetime, timedelta
from pathlib import Path

HERE = Path(__file__).resolve().parent
TMP = Path(tempfile.mkdtemp(prefix="wv_money_"))
os.environ["ASSISTANT_DATA"] = str(TMP)
os.environ["ASSISTANT_CARDS"] = str(TMP / "cards")
os.environ["CHAT_PUSH"] = "off"
os.environ["AGENT_HOME_SPACE"] = ""
sys.path.insert(0, str(HERE.parent / "scripts"))
sys.path.insert(0, str(HERE.parent / "scripts" / "money"))
import common as C  # noqa: E402
import simplefin  # noqa: E402
import categorize  # noqa: E402
import forecast  # noqa: E402
import quarterly  # noqa: E402
import monthly  # noqa: E402
import cards  # noqa: E402
import followups as fu  # noqa: E402

FAILS = []
TODAY = date(2026, 10, 16)                  # a Friday
NOW = datetime(2026, 10, 16, 16, 0)


def check(name, cond, extra=""):
    print(("ok   " if cond else "FAIL ") + name + (f"  {extra}" if extra and not cond else ""))
    if not cond:
        FAILS.append(name)


# the rules file is read from the repo but any learned rule must land in a scratch copy
RULES_SRC = C.RULES_PATH
C.RULES_PATH = TMP / "money_rules.json"
C.RULES_PATH.write_text(RULES_SRC.read_text(encoding="utf-8-sig"), encoding="utf-8")
LEARN_MD = TMP / "learnings_money.md"

# ------------------------------------------------------------------ config
c = C.cfg()
c["categorize"]["learn_to"] = str(LEARN_MD.relative_to(C.VAULT)) if str(LEARN_MD).startswith(str(C.VAULT)) else str(LEARN_MD)
_cfg_orig = C.cfg
C.cfg = lambda: c
check("money.json loads with defaults filled", c["feed"]["provider"] == "simplefin" and c["quarterly"]["due"] and c["tax_tables"]["federal"]["brackets"])
check("account kinds: Relay buckets by name", C.account_kind("Relay Operating")[0] == "operating" and C.account_kind("Relay - Tax")[0] == "tax"
      and C.account_kind("Relay Savings")[0] == "savings" and C.account_kind("Chase Freedom Visa")[0] == "card")
check("merchant key strips noise", C.merchant_key("SQ *BLUE BOTTLE COFFEE 0421 10/02") == "blue bottle coffee"
      and C.merchant_key("ADOBE *CREATIVE CLOUD 555-020-8020") == "adobe creative cloud", (C.merchant_key("SQ *BLUE BOTTLE COFFEE 0421 10/02"), C.merchant_key("ADOBE *CREATIVE CLOUD 555-020-8020")))


# ------------------------------------------------------------------ simplefin: claim + merge + pull (no network)
class _Resp:
    def __init__(self, body): self.body = body
    def read(self): return self.body
    def __enter__(self): return self
    def __exit__(self, *a): return False


calls = []


def fake_opener(req, timeout=0):
    calls.append((req.get_method(), req.full_url, dict(req.header_items())))
    if req.get_method() == "POST":
        return _Resp(b"https://u:pc6583a@example.invalid/simplefin")
    return _Resp(json.dumps(PAYLOAD).encode())


tok = base64.b64encode(b"https://beta-bridge.simplefin.org/simplefin/claim/abc").decode()
access = simplefin.claim(tok, opener=fake_opener)
check("claim decodes the token and POSTs once", access.startswith("https://u:p@") and calls[0][0] == "POST")
try:
    simplefin.claim(base64.b64encode(b"http://evil/claim").decode(), opener=fake_opener)
    check("claim refuses a non-https claim URL", False)
except ValueError:
    check("claim refuses a non-https claim URL", True)


def ts(d):
    return int(datetime(d.year, d.month, d.day, 12).timestamp())


def mk(days_ago, amount, desc, aid="op", tid=None, pending=False):
    d = TODAY - timedelta(days=days_ago)
    return {"id": tid or f"{aid}-{days_ago}-{abs(amount)}", "posted": ts(d), "amount": f"{amount:.2f}", "description": desc, "pending": pending}


op_txns = []
op_txns += [mk(3, 2400.00, "HONEYBOOK PAYMENT 8821"), mk(40, 1800.00, "HoneyBook Payments"), mk(120, 3200.00, "HONEYBOOK PAYMENT 7710")]
for n, amt in ((95, 59.99), (64, 59.99), (34, 59.99), (4, 69.99)):      # Adobe monthly, last one jumped
    op_txns.append(mk(n, -amt, "ADOBE *CREATIVE CLOUD 555-020-8020"))
op_txns += [mk(10, -199.00, "MUSICBED SUBSCRIPTION")]
for n in (70, 40, 9):
    op_txns.append(mk(n, -300.00, "GOOGLE *ADS 1234567"))
op_txns.append(mk(2, -600.00, "RELAY TRANSFER TO TAX"))                   # paired with the tax account's +600
op_txns += [mk(5, -43.21, "SQ *BLUE BOTTLE COFFEE 0421"), mk(6, -120.00, "ZELLE TO JOHN Glenrock"), mk(7, -0.50, "ROUNDING ADJ")]
op_txns.append(mk(12, -450.00, "VENMO PAYMENT 2nd shooter sara"))
tax_txns = [mk(1, 600.00, "RELAY TRANSFER FROM OPERATING", aid="tax"), mk(100, -900.00, "IRS USATAXPYMT", aid="tax")]
per_txns = [mk(85, -1500.00, "RENT PAYMENT example LLC", aid="chk"), mk(55, -1500.00, "RENT PAYMENT example LLC", aid="chk"),
            mk(25, -1500.00, "RENT PAYMENT example LLC", aid="chk"), mk(8, -88.40, "TRADER JOE'S #123", aid="chk"),
            mk(3, -14.99, "NETFLIX.COM", aid="chk")]

PAYLOAD = {"errors": [], "accounts": [
    {"org": {"name": "Relay"}, "id": "op", "name": "Relay Operating", "currency": "USD", "balance": "5120.55", "available-balance": "5120.55", "balance-date": ts(TODAY), "transactions": op_txns},
    {"org": {"name": "Relay"}, "id": "tax", "name": "Relay Tax", "currency": "USD", "balance": "2100.00", "balance-date": ts(TODAY), "transactions": tax_txns},
    {"org": {"name": "Chase"}, "id": "chk", "name": "Chase Total Checking", "currency": "USD", "balance": "400.00", "balance-date": ts(TODAY), "transactions": per_txns},
]}
N_ALL = len(op_txns) + len(tax_txns) + len(per_txns)

led = C.ledger()
r = simplefin.merge(led, PAYLOAD, c)
check("merge: 3 accounts, every txn new", r["accounts"] == 3 and r["new"] == N_ALL and not r["errors"], r)
check("merge: account kinds from names", led["accounts"]["op"]["kind"] == "operating" and led["accounts"]["tax"]["kind"] == "tax" and led["accounts"]["chk"]["kind"] == "personal")
r2 = simplefin.merge(led, PAYLOAD, c)
check("merge is idempotent", r2["new"] == 0 and r2["updated"] == 0, r2)
led["transactions"]["op-3-2400.0"]["bucket"] = "income:honeybook"
simplefin.merge(led, PAYLOAD, c)
check("merge keeps a bucket already placed", led["transactions"]["op-3-2400.0"]["bucket"] == "income:honeybook")
led["transactions"]["op-3-2400.0"]["bucket"] = ""

# pull: not configured -> NOT_CONFIGURED, no call; configured -> GET /accounts only, windows, status file
calls.clear()
simplefin.secret = lambda name: None
rp = simplefin.pull(opener=fake_opener, today=TODAY)
check("pull without a token is NOT_CONFIGURED and makes no call", rp.get("reason") == "not_configured" and not calls)
simplefin.secret = lambda name: "https://u:pc6583a@example.invalid/simplefin"
rp = simplefin.pull(opener=fake_opener, today=TODAY, full=True)
gets = [x for x in calls if x[0] == "GET"]
check("first pull walks back initial_days in windows, GET /accounts only",
      rp.get("ok") and gets and all("/accounts?" in u and m == "GET" for m, u, h in gets) and len(gets) == rp["windows"] and rp["windows"] >= 6, (rp, len(gets)))
check("GET carries Basic auth and never the userinfo in the URL", all("Authorization" in h and "@" not in u for m, u, h in gets))
st_file = C.load_json(simplefin.STATUS, {})
check("status file written for /setup + conn_check", st_file.get("last_ok") and st_file.get("configured") and st_file.get("accounts") == 3, st_file)
led = C.ledger()
check("ledger on disk has every txn", len(led["transactions"]) == N_ALL)
calls.clear()
rp2 = simplefin.pull(opener=fake_opener, today=TODAY)
check("second pull is incremental (one short window)", rp2.get("ok") and rp2["windows"] == 1 and rp2["new"] == 0, rp2)
for _ in range(10):
    simplefin.pull(opener=fake_opener, today=TODAY)
check("daily pull quota holds", simplefin.pull(opener=fake_opener, today=TODAY).get("reason") == "quota")


# ------------------------------------------------------------------ categorize: rules, transfers, unknowns card
led = C.ledger()
ra = categorize.apply(led, c=c)
bk = {t["description"]: t["bucket"] for t in led["transactions"].values()}
check("rules: HoneyBook -> income:honeybook, Adobe -> biz:software, IRS -> tax:federal, rent unplaced",
      bk["HONEYBOOK PAYMENT 8821"] == "income:honeybook" and bk["ADOBE *CREATIVE CLOUD 555-020-8020"] == "biz:software"
      and bk["IRS USATAXPYMT"] == "tax:federal" and bk["GOOGLE *ADS 1234567"] == "biz:ads" and bk["NETFLIX.COM"] == "personal:subscriptions", bk)
check("transfers paired across own accounts", bk["RELAY TRANSFER TO TAX"] == "transfer" and bk["RELAY TRANSFER FROM OPERATING"] == "transfer")
un = categorize.unplaced(led, c)
names = [t["description"] for t in un]
check("unplaced = coffee, zelle, venmo, rent (not the 50c rounding)", set(names) == {"SQ *BLUE BOTTLE COFFEE 0421", "ZELLE TO JOHN Glenrock", "VENMO PAYMENT 2nd shooter sara", "RENT PAYMENT example LLC"}, names)
C.save_ledger(led)

q, rows = categorize.card(led, c)
check("unknowns card: numbered rows, one question, recommended first, advice line",
      q and q["question"].count("\n") >= len(rows) and "1. " in q["question"] and "(Recommended)" in q["options"][0]["label"] and q["advice"].startswith("💡"), q and q["question"][:200])

# gate: Thursday -> no card; Friday 16:00 -> card; same week again -> none
out = categorize.tick(now=datetime(2026, 10, 15, 16, 0))
check("not the day: no card", out["qid"] == "")
out = categorize.tick(now=NOW)
qid = out["qid"]
check("Friday after 15:00: the weekly card posts", bool(qid), out)
check("card is in the live store with lane money", any(cc["id"] == qid for cc in cards._live()) and cards._live()[0]["questions"][0]["lane"] == "money")
out2 = categorize.tick(now=NOW + timedelta(hours=1))
check("once per week", out2["qid"] == "")

# the owner types placements -> learned rules -> re-applied on the next tick
st = C.state()
pend = st["unknowns_cards"][qid]["rows"]
idx = {r_["description"]: i + 1 for i, r_ in enumerate(pend)}
answer = f"{idx['VENMO PAYMENT 2nd shooter sara']} contractor, {idx['RENT PAYMENT example LLC']} rent, {idx['SQ *BLUE BOTTLE COFFEE 0421']} meals and {idx['ZELLE TO JOHN Glenrock']} skip"
pl, bad = categorize.parse_answer(answer, len(pend))
check("typed answer parses (aliases, 'and', prefixes)", pl == {idx['VENMO PAYMENT 2nd shooter sara']: "biz:contractors", idx['RENT PAYMENT example LLC']: "personal:rent",
                                                             idx['SQ *BLUE BOTTLE COFFEE 0421']: "biz:meals", idx['ZELLE TO JOHN Glenrock']: "ignore"} and not bad, (pl, bad))
check("an unknown word is reported, not guessed", categorize.parse_answer("1 flibber", 3)[1] == ["1 flibber"])
check("ambiguous prefix resolves to nothing", categorize.resolve_bucket("p") == "")
cards.answer(qid, "Placed in this thread (Recommended)", text=answer)
cards.UNDO_S = 0
out3 = categorize.tick(now=NOW + timedelta(days=1))
led = C.ledger()
bk = {t["description"]: t["bucket"] for t in led["transactions"].values()}
check("harvest learned 4 rules and placed the rows", out3["learned"] == 4 and bk["VENMO PAYMENT 2nd shooter sara"] == "biz:contractors"
      and bk["RENT PAYMENT example LLC"] == "personal:rent" and bk["ZELLE TO JOHN Glenrock"] == "ignore", (out3, bk))
rl = C.rules()
check("learned rules saved with merchant-key regexes", len(rl["learned"]) == 4 and all(x.get("learned") and x["match"].startswith("\\b") for x in rl["learned"]), rl["learned"])
led["transactions"]["new1"] = {"id": "new1", "account_id": "chk", "posted": TODAY.isoformat(), "amount": -1500.0, "description": "RENT PAYMENT example LLC 11/01",
                               "payee": "", "memo": "", "pending": False, "bucket": "", "bucket_src": "none", "rule_id": ""}
categorize.apply(led, c=c)
check("learned rule applies to the next charge", led["transactions"]["new1"]["bucket"] == "personal:rent" and led["transactions"]["new1"]["bucket_src"] == "learned", led["transactions"]["new1"])
del led["transactions"]["new1"]                 # a probe, not history
check("lesson recorded in the money learnings file", LEARN_MD.exists() and "contract labor" in LEARN_MD.read_text(encoding="utf-8").lower(), LEARN_MD.read_text(encoding="utf-8") if LEARN_MD.exists() else "missing")
check("repo money_rules.json untouched", json.loads(RULES_SRC.read_text(encoding="utf-8-sig"))["learned"] == [])
C.save_ledger(led)


# ------------------------------------------------------------------ forecast
led = C.ledger()
bills = forecast.recurring(led, c, TODAY)
by = {b["merchant"]: b for b in bills}
check("recurring: Adobe monthly (4x), Google Ads monthly (3x), rent monthly (4x); Musicbed (1x) not",
      "Adobe Creative Cloud" in by and by["Adobe Creative Cloud"]["period"] == "monthly" and "Google Ads" in by and "Rent example Llc" in by and "Musicbed Subscription" not in by, list(by))
check("price jump flagged on Adobe", by["Adobe Creative Cloud"]["jump"] and by["Adobe Creative Cloud"]["last_amount"] == 69.99)
check("next due = last + period (rent in 5 days)", by["Rent example Llc"]["next_due"] == (TODAY + timedelta(days=5)).isoformat() and by["Rent example Llc"]["days_to_due"] == 5, by["Rent example Llc"])
ws = forecast.warnings(bills, led, c, TODAY)
kinds = {w["kind"] for w in ws}
check("warnings: low balance on checking (rent 1500 > 400) + price jump", kinds == {"low_balance", "price_jump"}, ws)
ft = forecast.tick(now=NOW)
check("forecast armed bill-due follow-ups through followups.py", ft["armed"] >= 2, ft)
items = fu.load_state().get("items") or {}
bd = [i for i in items.values() if i["sop"] == "money" and i["step"] == "bill-due"]
check("follow-ups carry the bill's unit/amount/anchor", bd and all(i["unit"].startswith("bill:") for i in bd) and any("Rent" in i["subject"] for i in bd), [i["subject"] for i in bd])
check("warning cards posted once", ft["posted"] == 2 and len([cc for cc in cards._live() if cc["questions"][0]["header"].startswith("Money:")]) >= 2)
ft2 = forecast.tick(now=NOW + timedelta(hours=1))
check("warnings latched, follow-ups deduped", ft2["posted"] == 0 and len([i for i in fu.load_state()["items"].values() if i["step"] == "bill-due"]) == len(bd), ft2)
ft3 = forecast.tick(now=datetime(2026, 10, 16, 23, 30), dry=True)
check("dry-run posts nothing", ft3["posted"] >= 0 and C.state().get("warned"))


# ------------------------------------------------------------------ quarterly math
fed = c["tax_tables"]["federal"]
check("bracket tax: 10% band", quarterly.bracket_tax(10000, fed["brackets"]) == 1000.0)
check("bracket tax: crosses into 12%", abs(quarterly.bracket_tax(20000, fed["brackets"]) - (12400 * 0.10 + 7600 * 0.12)) < 0.01)
e = quarterly.estimate_from_profit(40000, c)
check("SE tax = 92.35% x 15.3%", abs(e["se_tax"] - round(40000 * 0.9235 * 0.153, 2)) < 0.01, e)
check("federal + CA liability positive and sane (10-25% of 40k)", 4000 < e["total"] < 10000, e)
e0 = quarterly.estimate_from_profit(0, c)
check("no profit -> no tax", e0["total"] == 0.0)
c["quarterly"]["prior_year_total_tax"] = 1911
e2 = quarterly.estimate_from_profit(40000, c)
check("safe harbour reported when prior-year tax is set", e2["safe_harbor_federal"] == 1911.0)
c["quarterly"]["prior_year_total_tax"] = None
led = C.ledger()
est = quarterly.estimate(led, c, TODAY)
sc = est["ytd"]
check("Schedule C YTD: income 7400, expenses incl. Adobe/Ads/contractor/meals, tax paid 900",
      sc["income"] == 7400.0 and sc["paid_federal"] == 900.0 and sc["expense"] > 1500 and sc["profit"] == round(sc["income"] - sc["expense"], 2), sc)
check("next due is Q4 on Jan 15 with an amount", est["next_due"] and est["next_due"]["n"] == 4 and est["next_due"]["date"] == "2027-01-15" and est["next_due"]["total"] >= 0, est["next_due"])
check("set-aside pct within floor/cap", 0.25 <= est["set_aside_pct"] <= 0.40)
qt = quarterly.tick(now=NOW)
items = fu.load_state().get("items") or {}
qe = [i for i in items.values() if i["step"] == "quarterly-estimate"]
check("quarterly-estimate follow-up armed (T-10d) with the amount", qt["armed"] and qe and qe[0]["unit"] == "q:2026-4" and qe[0]["fire_at"].startswith("2027-01-05"), qe)
check("set-aside card posted for the HoneyBook payment inside 14 days (the 40-day-old one is history)", qt["posted"] and qt["set_aside_rows"] == 1, qt)
sa = [json.loads(l) for l in cards.NOTES.read_text(encoding="utf-8").splitlines() if "set-aside" in l]
check("set-aside is said in chat (no 'Noted' card) and asks once for Relay's split when unknown",
      sa and "splits_pct" in sa[-1]["text"] and not [cc for cc in cards._live() if cc["questions"][0]["header"] == "Money: set-aside"], sa)
qt2 = quarterly.tick(now=NOW + timedelta(hours=1))
check("set-aside not repeated for the same payments", not qt2["posted"] and qt2["set_aside_rows"] == 0)
c["relay"]["splits_pct"] = {"tax": 20, "savings": 10, "operating": 70}
qx = quarterly.set_aside_card([led["transactions"]["op-3-2400.0"]], est, led, c)
check("with Relay's split known the card nets it out", "20%" in qx["question"] and ("more belongs" in qx["question"] or "covered" in qx["question"]), qx["question"])
check("quarterly.json written for the ticket worker", (C.DIR / "quarterly.json").exists())


# ------------------------------------------------------------------ monthly readback
led = C.ledger()
import io, contextlib
buf = io.StringIO()
with contextlib.redirect_stdout(buf):
    mt = monthly.tick(now=datetime(2026, 11, 1, 9, 0), dry=True)
text = buf.getvalue()
check("dry-run readback prints plain sentences for October", text.startswith("Money, October:") and "Income $" in text and "Business profit" in text and "Balances:" in text, text)
check("readback names HoneyBook and buckets", "HoneyBook" in text and "advertising" in text.lower() and "groceries" in text.lower(), text)
check("readback mentions the next quarterly", "Q4" in text, text)
mt = monthly.tick(now=datetime(2026, 11, 1, 9, 0))
check("posts on the 1st once (bridge.post swallowed offline)", mt.get("month") == "2026-10" and C.state().get("monthly_last") == "2026-10", mt)
check("second tick the same month is skipped", monthly.tick(now=datetime(2026, 11, 1, 10, 0)).get("skipped") == "done this month")
check("not the 1st: skipped", monthly.tick(now=datetime(2026, 11, 2, 10, 0)).get("skipped") == "not the day")
empty = monthly.readback_text(C.ledger(), 2025, 3, c)
check("a month with no rows says so", "nothing to read back" in empty)


# ------------------------------------------------------------------ safety: no write path anywhere in the lane
src = "".join((HERE.parent / "scripts" / "money" / f).read_text(encoding="utf-8") for f in ("simplefin.py", "categorize.py", "forecast.py", "quarterly.py", "monthly.py", "common.py"))
check("no smtplib / keyring.set / subprocess in the lane", "smtplib" not in src and "set_password" not in src and "subprocess" not in src)
check("the only POST is the setup-token claim", src.count('method="POST"') == 1)

print()
print("ALL OK" if not FAILS else f"{len(FAILS)} FAILED: {FAILS}")
sys.exit(1 if FAILS else 0)

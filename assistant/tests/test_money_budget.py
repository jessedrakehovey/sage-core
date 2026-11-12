"""Offline tests for the envelope budget (budget.py) and the weekly reconciliation (reconcile.py):
account roles, ready-to-assign math (a card charge and a card payment both leave it unchanged), carry-over
and overspend, the monthly fund plan and card, the owner's typed words, the account check with its
suspects and the baseline, age of money, the merchant-level bulk card, and the Mission Control page.
Scratch ASSISTANT_DATA / ASSISTANT_CARDS; nothing posts to Chat; nothing touches a bank."""
import os as _os, tempfile as _tf
_os.environ.setdefault("WV_LOG_DIR", _tf.mkdtemp(prefix="wv_logs_"))
import json
import os
import sys
import tempfile
import time
from datetime import date, datetime
from pathlib import Path

HERE = Path(__file__).resolve().parent
TMP = Path(tempfile.mkdtemp(prefix="wv_budget_"))
os.environ["ASSISTANT_DATA"] = str(TMP)
os.environ["ASSISTANT_CARDS"] = str(TMP / "cards")
os.environ["CHAT_PUSH"] = "off"
os.environ["AGENT_HOME_SPACE"] = ""
sys.path.insert(0, str(HERE.parent / "scripts"))
sys.path.insert(0, str(HERE.parent / "scripts" / "money"))
import common as C  # noqa: E402
import budget as B  # noqa: E402
import reconcile as R  # noqa: E402
import categorize  # noqa: E402

FAILS = []
TODAY = date(2026, 11, 15)
NOW = datetime(2026, 11, 15, 19, 0)            # a Sunday evening


def check(name, cond, extra=""):
    print(("ok   " if cond else "FAIL ") + name + (f"  {extra}" if extra and not cond else ""))
    if not cond:
        FAILS.append(name)


# ------------------------------------------------------------------ fixture: rules + config, no organization names
C.RULES_PATH = TMP / "money_rules.json"
C.RULES_PATH.write_text(json.dumps({
    "buckets": {
        "income:personal": {"label": "Personal income", "side": "personal", "kind": "income"},
        "personal:groceries": {"label": "Groceries", "side": "personal", "kind": "expense"},
        "personal:dining": {"label": "Dining out", "side": "personal", "kind": "expense"},
        "personal:car": {"label": "Car & fuel", "side": "personal", "kind": "expense"},
        "biz:software": {"label": "Software & subscriptions", "side": "business", "kind": "expense"},
        "transfer": {"label": "Transfer", "side": "none", "kind": "transfer"},
    },
    "aliases": {"food": "personal:groceries"},
    "rules": [{"id": "r-g", "match": "grocer", "sign": "out", "bucket": "personal:groceries"},
              {"id": "r-d", "match": "cafe", "sign": "out", "bucket": "personal:dining"},
              {"id": "r-pay", "match": "payroll", "sign": "in", "bucket": "income:personal"}],
    "learned": []}), encoding="utf-8")
c = C.cfg()
c["budget"]["start_month"] = "2026-10"
c["budget"]["sides"] = ["personal", "business"]
c["budget"]["fund_card"] = True                      # the organization's money.json may switch the forms off; the engine tests them
c["reconcile"]["card"] = True
c["categorize"]["weekly_card"] = True
c["categorize"]["bulk_card"] = True
c["categorize"]["learn_to"] = str(TMP / "learnings.md")
c["categorize"]["bulk_min_merchants"] = 2
c["categorize"]["bulk_min_count"] = 2
c["accounts"]["kinds"] = [{"match": "credit|card", "kind": "card", "side": "personal"},
                          {"match": "operating", "kind": "operating", "side": "business"},
                          {"match": "checking", "kind": "personal", "side": "personal"}]
c["accounts"]["overrides"] = {"a-rainy": {"kind": "savings", "side": "personal", "budget": "vault"}}
C.cfg = lambda: c


def acct(aid, name, bal, typ="depository", sub="checking", bd="2026-11-15"):
    return {"id": aid, "name": name, "org": "Bank", "balance": bal, "balance_date": bd, "type": typ, "subtype": sub, "currency": "USD"}


def txn(aid, posted, amount, desc, bucket="", pending=False, tid=""):
    return {"id": tid or C.txn_id(aid, posted, amount, desc), "account_id": aid, "posted": posted, "amount": amount,
            "description": desc, "pending": pending, "bucket": bucket, "bucket_src": "manual" if bucket else "none", "rule_id": ""}


def ledger():
    led = {"accounts": {}, "transactions": {}, "pulled": {}}
    for a in (acct("a-chk", "Everyday Checking", 1000.0), acct("a-card", "Rewards Credit Card", -300.0, "credit", "credit card"),
              acct("a-rainy", "Rainy day", 500.0, sub="cash management"), acct("a-brk", "Brokerage", 9000.0, "investment", "brokerage"),
              acct("a-ops", "Operating", 2000.0)):
        led["accounts"][a["id"]] = a
    rows = [txn("a-chk", "2026-10-03", 3000.0, "ACME PAYROLL", "income:personal"),
            txn("a-chk", "2026-10-05", -200.0, "GROCER MART", "personal:groceries"),
            txn("a-card", "2026-10-12", -100.0, "GROCER MART", "personal:groceries"),
            txn("a-chk", "2026-10-20", -40.0, "CORNER CAFE", "personal:dining"),
            txn("a-chk", "2026-10-25", -500.0, "Transfer to Rainy day", "transfer"),
            txn("a-rainy", "2026-10-25", 500.0, "Transfer from Everyday Checking", "transfer"),
            txn("a-chk", "2026-11-02", 3000.0, "ACME PAYROLL", "income:personal"),
            txn("a-chk", "2026-11-04", -250.0, "GROCER MART", "personal:groceries"),
            txn("a-card", "2026-11-08", -60.0, "CORNER CAFE", "personal:dining"),
            txn("a-chk", "2026-11-10", -300.0, "CREDIT CRD AUTOPAY", "transfer"),
            txn("a-card", "2026-11-10", 300.0, "PAYMENT THANK YOU", "transfer"),
            txn("a-chk", "2026-11-12", -45.0, "FUEL STOP", ""),
            txn("a-chk", "2026-11-13", -45.0, "FUEL STOP", ""),
            txn("a-ops", "2026-11-05", -120.0, "SOFTWARE CO", "biz:software"),
            txn("a-ops", "2026-11-06", 900.0, "CLIENT PAY", "")]
    for t in rows:
        led["transactions"][t["id"]] = t
    return led


led = ledger()

# ------------------------------------------------------------------ roles
roles = {a["name"]: a["role"] for a in B.accounts(led, c)}
check("roles: checking spends, card is a card, cash-management vault, brokerage off, operating spends",
      roles == {"Everyday Checking": "spending", "Rewards Credit Card": "card", "Rainy day": "vault", "Brokerage": "off", "Operating": "spending"}, roles)

# ------------------------------------------------------------------ the model
b = B.load()
m = B.model(led, b, c, today=TODAY)
p = m["sides"]["personal"]
env = {e["key"]: e for e in p["envelopes"]}
check("budget month and start", m["month"] == "2026-11" and m["start"] == "2026-10")
check("cash = spending accounts only (vault + brokerage excluded)", p["cash"] == 1000.0, p["cash"])
check("card debt = what the card owes", p["card_debt"] == 300.0, p["card_debt"])
check("groceries: October overspend floors to 0, November activity counted on checking and card",
      env["personal:groceries"]["carried"] == 0 and env["personal:groceries"]["activity"] == -250.0 and env["personal:groceries"]["available"] == -250.0, env["personal:groceries"])
check("dining counts the card charge", env["personal:dining"]["activity"] == -60.0, env["personal:dining"])
check("transfers never touch an envelope", all(e["activity"] >= -250 for e in p["envelopes"]))
check("nothing assigned yet: ready = cash - card debt", p["ready"] == 700.0, p["ready"])
check("unplaced this month counted per side", p["unplaced"] == 90.0 and m["sides"]["business"]["unplaced"] == 0, (p["unplaced"], m["sides"]["business"]["unplaced"]))
check("vault listed with its balance", p["vaults"] and p["vaults"][0]["name"] == "Rainy day" and p["vaults"][0]["balance"] == 500.0)
check("targets default to the median of past months' activity", env["personal:groceries"]["target"] == 300.0 and env["personal:dining"]["target"] == 40.0,
      (env["personal:groceries"]["target"], env["personal:dining"]["target"]))

# a card charge from an envelope leaves ready-to-assign unchanged; so does paying the card
led2 = ledger()
t = txn("a-card", "2026-11-14", -80.0, "GROCER MART", "personal:groceries")
led2["transactions"][t["id"]] = t
led2["accounts"]["a-card"]["balance"] = -380.0
bf = B.load()
B.assign(bf, "personal:groceries", 400.0, "2026-11", "test")          # an envelope with money in it
before = B.model(led, bf, c, today=TODAY)["sides"]["personal"]["ready"]
m2 = B.model(led2, bf, c, today=TODAY)
check("a card charge from a funded envelope: envelope down, debt up, ready unchanged", m2["sides"]["personal"]["ready"] == before
      and m2["sides"]["personal"]["card_debt"] == 380.0, (m2["sides"]["personal"]["ready"], before))
m2b = B.model(led2, B.load(), c, today=TODAY)
check("a card charge from an EMPTY envelope is uncovered debt: ready drops", m2b["sides"]["personal"]["ready"] == p["ready"] - 80.0)
led3 = ledger()
led3["accounts"]["a-card"]["balance"] = 0.0
led3["accounts"]["a-chk"]["balance"] = 700.0
m3 = B.model(led3, B.load(), c, today=TODAY)
check("paying the card: cash and debt down together, ready unchanged", m3["sides"]["personal"]["ready"] == p["ready"], m3["sides"]["personal"]["ready"])

# ------------------------------------------------------------------ assigning, moving, carry-over
b = B.load()
B.assign(b, "personal:groceries", 400.0, "2026-11", "test")
B.assign(b, "personal:dining", 100.0, "2026-11", "test")
m = B.model(led, b, c, today=TODAY)
p = m["sides"]["personal"]
env = {e["key"]: e for e in p["envelopes"]}
check("assigned shows and available = assigned + activity", env["personal:groceries"]["assigned"] == 400.0 and env["personal:groceries"]["available"] == 150.0)
check("ready drops by the positive availables", p["ready"] == 700.0 - 150.0 - 40.0, p["ready"])
B.move(b, 50.0, "personal:dining", "personal:groceries", "2026-11", "test")
m = B.model(led, b, c, today=TODAY)
env = {e["key"]: e for e in m["sides"]["personal"]["envelopes"]}
check("move: one down, one up, ready the same", env["personal:groceries"]["available"] == 200.0 and env["personal:dining"]["available"] == -10.0
      and m["sides"]["personal"]["ready"] == 500.0, (env["personal:groceries"]["available"], env["personal:dining"]["available"], m["sides"]["personal"]["ready"]))
check("overspent list names dining", [e["key"] for e in m["sides"]["personal"]["overspent"]] == ["personal:dining"])
b2 = B.load()
B.assign(b2, "personal:groceries", 500.0, "2026-10", "test")
mm = B.model(led, b2, c, today=TODAY)
g = next(e for e in mm["sides"]["personal"]["envelopes"] if e["key"] == "personal:groceries")
check("carry-over: October's leftover (500-300) arrives in November", g["carried"] == 200.0 and g["available"] == -50.0, g)
c["budget"]["carry_overspend"] = True
mm = B.model(ledger(), B.load(), c, today=TODAY)
g = next(e for e in mm["sides"]["personal"]["envelopes"] if e["key"] == "personal:groceries")
check("carry_overspend: October's -300 carries into November", g["carried"] == -300.0, g)
c["budget"]["carry_overspend"] = False

# ------------------------------------------------------------------ the fund plan + card
b = B.load()
b["targets"]["personal:groceries"] = 320.0
m = B.model(led, b, c, today=TODAY)
pl = B.plan("personal", m)
check("plan refills to targets in order, within ready", pl["fund"] == {"personal:groceries": 320.0, "personal:dining": 40.0} and pl["total"] == 360.0 and not pl["underfunded"], pl)
pl2 = B.plan("personal", m, ready=330.0)
check("short of money: fund in order, name the shortfall", pl2["fund"] == {"personal:groceries": 320.0, "personal:dining": 10.0} and pl2["underfunded"] == [("personal:dining", 30.0)], pl2)
q = B.fund_question("personal", m, C.rules(), pl)
check("fund card: a short question, the plan in the details", "Ready to assign for November (personal): $700" in q["question"] and len(q["question"]) <= 350
      and "Groceries $320" in q["details"] and q["options"][0]["label"].startswith("Fund it"), (q["question"][:200], q["details"][:200]))
posted = B.post_fund_cards(b, led, c, C.rules(), datetime(2026, 11, 1, 9, 0), dry=True, force=False)
check("fund cards due on fund_day after fund_after, one per side", posted == ["personal", "business"], posted)
check("not due before fund_after", B.post_fund_cards(b, led, c, C.rules(), datetime(2026, 11, 1, 8, 0), dry=True, force=False) == [])

# the tap funds the plan; typed lines override; typed moves apply
b = B.load()
b["targets"]["personal:groceries"] = 320.0
b["fund_cards"]["q-fund"] = {"side": "personal", "month": "2026-11", "plan": pl["fund"], "ts": time.time()}
n = B.harvest(b, led, c, C.rules(), answers=[{"qid": "q-fund", "option": "Fund it (Recommended)", "text": "groceries 300, move 10 from dining to groceries", "epoch": time.time()}])
a = (b["months"]["2026-11"]["assigned"])
check("Fund it + typed override + move", n == 1 and a["personal:groceries"] == 310.0 and a["personal:dining"] == 30.0 and not b["fund_cards"], a)
b = B.load()
b["fund_cards"]["q-skip"] = {"side": "personal", "month": "2026-11", "plan": pl["fund"], "ts": time.time()}
B.harvest(b, led, c, C.rules(), answers=[{"qid": "q-skip", "option": "Skip this month", "text": "", "epoch": time.time()}])
check("Skip assigns nothing", not (b["months"].get("2026-11") or {}).get("assigned"))

# ------------------------------------------------------------------ the owner's words
r = C.rules()
ops, bad = B.parse_words("move 50 from dining to groceries, assign $200 to car and target groceries 600. cover dining from car", r)
check("parse: move / assign / target / cover", ("move", 50.0, "personal:dining", "personal:groceries") in ops and ("assign", 200.0, "personal:car") in ops
      and ("target", "personal:groceries", 600.0) in ops and ("cover", "personal:dining", "personal:car") in ops and not bad, (ops, bad))
ops, bad = B.parse_words("move 20 from dining to nowhere", r)
check("an unknown envelope is reported, not guessed", not ops and bad == ["move 20 from dining to nowhere"], (ops, bad))
ops, _ = B.parse_words("groceries 500, food 20, dining: 150", r, allow_pairs=True)
check("the 'groceries 500' form only on a fund card, aliases map to their envelope",("set", "personal:groceries", 500.0) in ops and ("set", "personal:groceries", 20.0) in ops and ("set", "personal:dining", 150.0) in ops, ops)
b = B.load()
B.assign(b, "personal:dining", 20.0, "2026-11", "t")
said = B.run_ops(b, [("cover", "personal:dining", "personal:car")], led, "2026-11", c, r, "t")
mm = B.model(led, b, c, today=TODAY)
d = next(e for e in mm["sides"]["personal"]["envelopes"] if e["key"] == "personal:dining")
check("cover takes exactly the shortfall", d["available"] == 0.0 and "covered Dining out's $40" in said[0], (d, said))

# ------------------------------------------------------------------ reconciliation
b = B.load()
chk = R.check(led, b, c, TODAY)
check("no baseline yet on every spending account and card", {x["state"] for x in chk["rows"]} == {"no baseline"} and len(chk["rows"]) == 3, chk["rows"])
n = R.baseline(b, led, c, TODAY)
check("baseline takes every spending account and card", n == 3 and b["reconciled"]["a-chk"]["balance"] == 1000.0 and b["reconciled"]["a-chk"]["through"] == "2026-11-15")
chk = R.check(led, b, c, TODAY)
check("right after a baseline everything is ok", all(x["state"] == "ok" for x in chk["rows"]))
# a week later: two posted rows arrive, the bank balance moved by the same amount -> ok
led4 = ledger()
for t in (txn("a-chk", "2026-11-18", -30.0, "GROCER MART", "personal:groceries"), txn("a-chk", "2026-11-20", -12.5, "CORNER CAFE", "personal:dining")):
    led4["transactions"][t["id"]] = t
led4["accounts"]["a-chk"]["balance"] = 957.5
led4["accounts"]["a-chk"]["balance_date"] = "2026-11-22"
for a in led4["accounts"].values():
    a["balance_date"] = "2026-11-22"
chk = R.check(led4, b, c, date(2026, 11, 22))
row = next(x for x in chk["rows"] if x["id"] == "a-chk")
check("posted rows explain the balance change", row["state"] == "ok" and row["expected"] == 957.5 and row["since"] == 2, row)
# the feed sends one row twice: off, and the duplicate is named
dup = txn("a-chk", "2026-11-19", -30.0, "GROCER MART", "personal:groceries", tid="dup-1")
led4["transactions"][dup["id"]] = dup
chk = R.check(led4, b, c, date(2026, 11, 22))
row = next(x for x in chk["rows"] if x["id"] == "a-chk")
check("a duplicate row puts the account off by its amount and names it", row["state"] == "off" and row["diff"] == 30.0
      and any("appears twice" in s for s in row["suspects"]) and any("exactly" in s for s in row["suspects"]), row)
# a pending row never posting, and a stale balance
led4["transactions"].pop("dup-1")
old = txn("a-chk", "2026-11-05", -9.0, "HOLD CAFE", "", pending=True, tid="pend-1")
led4["transactions"][old["id"]] = old
led4["accounts"]["a-chk"]["balance"] = 950.0
chk = R.check(led4, b, c, date(2026, 11, 22))
row = next(x for x in chk["rows"] if x["id"] == "a-chk")
check("a stuck pending row is a suspect (pending never counts in expected)", row["state"] == "off" and any("pending since" in s for s in row["suspects"]), row)
led4["accounts"]["a-card"]["balance_date"] = "2026-11-10"
chk = R.check(led4, b, c, date(2026, 11, 22))
row = next(x for x in chk["rows"] if x["id"] == "a-card")
check("an old balance is stale, a feed problem", row["state"] == "stale" and "2026-11-10" in row["note"], row)
check("age of money is FIFO days, a number", isinstance(chk["age_of_money"], int) and chk["age_of_money"] >= 0, chk["age_of_money"])
check("the week's unplaced rows are counted", R.unplaced_week(led, c, TODAY) == {"n": 2, "amount": 90.0}, R.unplaced_week(led, c, TODAY))

# the weekly card + harvest
b = B.load()
B.assign(b, "personal:dining", 20.0, "2026-11", "t")
B.assign(b, "personal:car", 200.0, "2026-11", "t")
q = R.card(led, b, c, C.rules(), TODAY)
check("weekly card: baseline note, overspend with a cover suggestion, unplaced, age of money, two options",
      "awaiting a first baseline" in q["question"] and "Overspent: Groceries, Dining out" in q["question"] and len(q["question"]) <= 350
      and "Dining out (personal) is overspent by $40" in q["details"] and "cover dining out from car & fuel" in q["details"]
      and "2 of this week's transactions" in q["details"] and "Age of money" in q["details"] and q["options"][0]["label"].startswith("Reconciled"), (q["question"], q["details"]))
out = R.tick(NOW, dry=True, force=False)
check("tick: due Sunday 18:00 once a week (dry run prints, posts nothing)", out["qid"] == "")
b["week_cards"]["q-week"] = {"week": "2026-W46", "ts": time.time()}
n = R.harvest(b, led, c, C.rules(), answers=[{"qid": "q-week", "option": "Reconciled, all good (Recommended)", "text": "cover dining from car", "epoch": time.time()}])
mm = B.model(led, b, c, today=TODAY)
d = next(e for e in mm["sides"]["personal"]["envelopes"] if e["key"] == "personal:dining")
check("Reconciled: baselines every account and applies the typed cover", n == 1 and len(b["reconciled"]) == 3 and d["available"] == 0.0, (n, len(b["reconciled"]), d))
b = B.load()
R.harvest(b, led, c, C.rules(), answers=[{"qid": "q-hold", "option": "Hold on", "text": "", "epoch": time.time()}]) if b["week_cards"].update({"q-hold": {"ts": time.time()}}) is None else None
check("Hold on leaves the baselines alone", not b.get("reconciled"))

# ------------------------------------------------------------------ the merchant-level bulk card
led5 = ledger()
for i in range(3):
    t = txn("a-chk", f"2026-11-0{i + 1}", -15.0 - i, "BIG BOX STORE", "")
    led5["transactions"][t["id"]] = t
q, rows = categorize.bulk_card(led5, c, C.rules())
check("bulk card lists repeat merchants with counts, most rows first", q is not None and rows[0]["merchant"] == "big box store" and rows[0]["n"] == 3
      and rows[1]["merchant"] == "fuel stop" and "3 rows" in q["details"] and len(q["question"]) <= 350, (q or {}).get("details", "")[:300])
st = {"unknowns_cards": {"q-bulk": {"ts": time.time(), "rows": rows, "bulk": True}}}
categorize.harvest(led5, st, answers=[{"qid": "q-bulk", "option": "Placed in this thread (Recommended)", "text": "1 groceries, 2 car", "epoch": time.time()}])
placed = {t["description"]: t["bucket"] for t in led5["transactions"].values() if t["description"] in ("BIG BOX STORE", "FUEL STOP")}
check("one typed word places every row of that merchant", placed == {"BIG BOX STORE": "personal:groceries", "FUEL STOP": "personal:car"}, placed)
check("a learned rule per merchant", {x["bucket"] for x in C.rules()["learned"]} == {"personal:groceries", "personal:car"})
c["categorize"]["bulk_min_merchants"] = 50
check("a small backlog gets no bulk card", categorize.bulk_card(led5, c, C.rules())[0] is None)

# ------------------------------------------------------------------ the money talk (brief.py): questions, deeds, undo
import brief  # noqa: E402
led6 = ledger()
for i in range(3):
    t = txn("a-chk", f"2026-11-0{i + 2}", -20.0 - i, "BIG BOX STORE", "")
    led6["transactions"][t["id"]] = t
C.save_ledger(led6)
b = B.load(); b["months"] = {}; b["targets"] = {}; b["reconciled"] = {}; b.pop("fund_asked", None); B.save(b)
qs = brief.questions(led6, b, c, C.rules(), n=9)
kinds = [q["kind"] for q in qs]
check("questions: a merchant to place, an overspend, a missing target, the baseline", "merchant" in kinds and "overspent" in kinds and "baseline" in kinds, kinds)
mq = next(q for q in qs if q["kind"] == "merchant")
check("a merchant question carries count, total, a monthly figure and the accounts", mq["name"] == "BIG BOX STORE" and mq["n"] == 3 and mq["total"] == -63.0 and mq["per_month"] < 0 and mq["accounts"] == ["Everyday Checking"], mq)
blk = brief.block()
check("the context block: MONEY figures, envelope names, the questions", blk.startswith("MONEY") and "ready to assign" in blk and "envelope names:" in blk and "MONEY QUESTIONS" in blk and "1. [" in blk, blk[:300])
check("lookup: merchant by name, by key, by part; envelope by label", brief.resolve_merchant("BIG BOX STORE")["merchant"] == "big box store"
      and brief.resolve_merchant("big box")["merchant"] == "big box store" and brief.resolve_merchant("nothing like it") is None and brief.resolve_envelope("Groceries") == "personal:groceries")
r1 = brief.apply("place", {"merchant": "Big Box Store", "envelope": "groceries"}, by="t1")
led6 = C.ledger()
placed = [t for t in led6["transactions"].values() if t["description"] == "BIG BOX STORE"]
check("place: every charge of that merchant lands in the envelope, a learned rule exists", r1["ok"] and all(t["bucket"] == "personal:groceries" for t in placed) and "3 charges" in r1["line"]
      and any(x["bucket"] == "personal:groceries" and "big" in x["match"] for x in C.rules()["learned"]), (r1, [t["bucket"] for t in placed]))
check("place: undo removes the rule and unplaces the rows", brief.undo(r1["undo"]) and all(not t["bucket"] for t in C.ledger()["transactions"].values() if t["description"] == "BIG BOX STORE")
      and not any("big" in x["match"] for x in C.rules()["learned"]))
r2 = brief.apply("target", {"envelope": "dining", "amount": "150"}, by="t1")
check("target: set and undone", r2["ok"] and B.load()["targets"]["personal:dining"] == 150.0 and brief.undo(r2["undo"]) and "personal:dining" not in B.load()["targets"], r2)
r3 = brief.apply("move", {"amount": 25, "from": "car", "to": "groceries"}, by="t1")
mm = B.model(C.ledger(), B.load(), c, today=TODAY)
g = next(e for e in mm["sides"]["personal"]["envelopes"] if e["key"] == "personal:groceries")
check("move: envelopes change, undo reverses", r3["ok"] and g["assigned"] == 25.0 and brief.undo(r3["undo"])
      and next(e for e in B.model(C.ledger(), B.load(), c, today=TODAY)["sides"]["personal"]["envelopes"] if e["key"] == "personal:groceries")["assigned"] == 0.0, (r3, g))
r4 = brief.apply("cover", {"envelope": "groceries", "from": "ready"}, by="t1")
check("cover: takes the shortfall from ready to assign", r4["ok"] and "moved from ready to assign to Groceries" in r4["line"], r4)
brief.undo(r4["undo"])
r5 = brief.apply("reconciled", {}, by="t1")
check("reconciled: every account baselined, undo clears it", r5["ok"] and len(B.load()["reconciled"]) == 3 and brief.undo(r5["undo"]) and not B.load()["reconciled"], r5)
b = B.load(); b["targets"]["personal:groceries"] = 320.0; B.save(b)
r6 = brief.apply("fund", {"side": "personal"}, by="t1")
a = B.load()["months"].get("2026-11", {}).get("assigned", {})
check("fund: the plan is assigned, the side is marked asked, undo takes it back", r6["ok"] and a.get("personal:groceries") == 320.0 and B.load()["fund_asked"]["personal"] == "2026-11"
      and brief.undo(r6["undo"]) and not B.load()["months"]["2026-11"]["assigned"].get("personal:groceries"), (r6, a))
check("an unknown op is refused in words", not brief.apply("pay", {}, by="t1")["ok"])
check("cards off by switch: no fund card, no weekly card, no bulk card",
      (c["budget"].__setitem__("fund_card", False) or B.post_fund_cards(B.load(), ledger(), c, C.rules(), datetime(2026, 11, 1, 9, 0), dry=True, force=False) == [])
      and (c["categorize"].__setitem__("bulk_card", False) or categorize.post_bulk(led5, {}, c, NOW, True, False) == ""))
c["budget"]["fund_card"] = True; c["categorize"]["bulk_card"] = True
C.save_ledger(led)

# ------------------------------------------------------------------ page + text + safety
C.save_ledger(led)
html = B.page()
check("page renders both sides with the figures", "<title>Money</title>" in html and "Ready to assign" in html and "Groceries" in html and "Rainy day" in html and "Everyday Checking" in html)
check("api status is the model", B.status()["month"] == B.model(led, B.load(), c)["month"])
check("text shows envelopes and vaults", "Groceries" in B.text() and "(vault)" in B.text())
src = "".join((HERE.parent / "scripts" / "money" / f).read_text(encoding="utf-8") for f in ("budget.py", "reconcile.py"))
check("no network, no credential, no subprocess in the budget or the reconciliation", all(w not in src for w in ("urllib", "requests", "smtplib", "keyring", "subprocess", "set_password")))
sys.path.insert(0, str(HERE.parent))
import engine as _eng  # noqa: E402
_ov = _eng.org_values()
_words = {str(w).lower() for k in ("names", "words", "domains") for w in (_ov.get(k) or [])} | {str(p["name"]).split()[0].lower() for p in _ov.get("persons") or [] if p.get("name")}
_hits = sorted(w for w in _words if len(w) >= 4 and __import__("re").search(r"(?<![a-z])" + __import__("re").escape(w) + r"(?![a-z])", src.lower()))
check("no organization name in the engine code (the words come from the pack, never a literal list)", not _hits, _hits)

print()
print("ALL OK" if not FAILS else f"{len(FAILS)} FAILED: {FAILS}")
sys.exit(1 if FAILS else 0)

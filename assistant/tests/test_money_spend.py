"""The money question the product page shows, answered from the ledger (scripts/money/brief.py spend, 2026-11-16).

"How much did we spend on software last month, across everything?" -> the total on every linked account, each account's
share, the month before, and the subscription whose price went up. Read only: the ledger file is byte-for-byte the same
afterwards. A scratch ledger with four accounts; no organization's names.
   python assistant/tests/test_money_spend.py
"""
import os as _os, tempfile as _tf
_os.environ.setdefault("WV_LOG_DIR", _tf.mkdtemp(prefix="wv_logs_"))
import json
import os
import sys
import tempfile
from datetime import date
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")
HERE = Path(__file__).resolve().parent
TMP = Path(tempfile.mkdtemp(prefix="wv_spend_"))
os.environ["ASSISTANT_DATA"] = str(TMP)
os.environ["ASSISTANT_CARDS"] = str(TMP / "cards")
os.environ["CHAT_PUSH"] = "off"
sys.path.insert(0, str(HERE.parent / "scripts"))
sys.path.insert(0, str(HERE.parent / "scripts" / "money"))
import brief  # noqa: E402

FAILS = []
TODAY = date(2026, 11, 16)
RULES = {"buckets": {"biz:software": {"label": "Software"}, "personal:software": {"label": "Software"},
                     "personal:groceries": {"label": "Groceries"}, "biz:travel": {"label": "Travel"}}, "aliases": {}, "learned": []}
ACCTS = {"chk": "Business checking", "bcard": "Business card", "pcard": "Personal card", "app": "Payment app"}
N = [0]


def txn(acct, posted, amount, desc, bucket):
    N[0] += 1
    return {"id": f"t{N[0]}", "account_id": acct, "posted": posted, "amount": amount, "description": desc, "bucket": bucket}


def check(name, cond, extra=""):
    print(("ok   " if cond else "FAIL ") + name + (f"  {extra}" if extra and not cond else ""))
    if not cond:
        FAILS.append(name)


ROWS = [
    # October: $412 of software on four accounts
    txn("chk", "2026-10-03", -49.0, "CLOUD DRIVE PRO", "biz:software"), txn("chk", "2026-10-11", -30.0, "INVOICE TOOL", "biz:software"),
    txn("chk", "2026-10-20", -50.0, "SITE HOSTING", "biz:software"),
    txn("bcard", "2026-10-02", -58.0, "EDIT SUITE MONTHLY", "biz:software"), txn("bcard", "2026-10-05", -40.0, "FONT LIBRARY", "biz:software"),
    txn("bcard", "2026-10-09", -36.0, "MUSIC LICENCE", "biz:software"), txn("bcard", "2026-10-14", -30.0, "FORM BUILDER", "biz:software"),
    txn("bcard", "2026-10-21", -25.0, "PASSWORD KEEPER", "biz:software"), txn("bcard", "2026-10-28", -25.0, "BACKUP SERVICE", "biz:software"),
    txn("pcard", "2026-10-06", -34.0, "PHOTO STORAGE", "personal:software"), txn("pcard", "2026-10-19", -20.0, "NOTES APP", "personal:software"),
    txn("app", "2026-10-12", -15.0, "DESIGN TEMPLATES", "biz:software"),
    # the same month, other things: never counted as software
    txn("pcard", "2026-10-08", -212.4, "BIG BOX STORE", "personal:groceries"), txn("chk", "2026-10-15", 2500.0, "CLIENT PAYMENT", ""),
    txn("bcard", "2026-10-30", -80.0, "MYSTERY CHARGE", ""), txn("bcard", "2026-10-25", 300.0, "CARD PAYMENT THANK YOU", ""),
    # September: $374, with the edit suite at $20
    txn("chk", "2026-09-03", -49.0, "CLOUD DRIVE PRO", "biz:software"), txn("chk", "2026-09-11", -30.0, "INVOICE TOOL", "biz:software"),
    txn("chk", "2026-09-20", -50.0, "SITE HOSTING", "biz:software"),
    txn("bcard", "2026-09-02", -20.0, "EDIT SUITE MONTHLY", "biz:software"), txn("bcard", "2026-09-05", -40.0, "FONT LIBRARY", "biz:software"),
    txn("bcard", "2026-09-09", -36.0, "MUSIC LICENCE", "biz:software"), txn("bcard", "2026-09-14", -30.0, "FORM BUILDER", "biz:software"),
    txn("bcard", "2026-09-21", -25.0, "PASSWORD KEEPER", "biz:software"), txn("bcard", "2026-09-28", -25.0, "BACKUP SERVICE", "biz:software"),
    txn("pcard", "2026-09-06", -34.0, "PHOTO STORAGE", "personal:software"), txn("pcard", "2026-09-19", -20.0, "NOTES APP", "personal:software"),
    txn("app", "2026-09-12", -15.0, "DESIGN TEMPLATES", "biz:software"),
]
LED = {"accounts": {k: {"id": k, "name": v} for k, v in ACCTS.items()}, "transactions": {t["id"]: t for t in ROWS}}
frozen = json.dumps(LED, sort_keys=True)

r = brief.spend("software", "", LED, RULES, TODAY)
line = r["line"]
print(line)
check("it answers", r["ok"] is True)
check("the last full month, named", line.startswith("Software, October 2026: $412 on 4 accounts (12 charges)."), line.splitlines()[0])
check("each account's share, biggest first", [x.strip() for x in line.splitlines()[1:5]] ==
      ["Business card: $214 (6 charges)", "Business checking: $129 (3 charges)", "Personal card: $54 (2 charges)", "Payment app: $15 (1 charge)"], str(line.splitlines()[1:5]))
check("business and personal software are one answer", "$54" in line and "$214" in line)
check("the month before, and the difference", "The month before (September 2026): $374, so $38 more this time." in line, line)
check("the subscription that went up is named, with both prices and its account",
      "Charged more than the month before: EDIT SUITE MONTHLY went from $20.00 to $58.00 (Business card)." in line, line)
check("nothing else is called a rise", line.count("went from") == 1)
check("groceries, money coming in, a card payment and an unplaced charge are not software", "BIG BOX" not in line and "MYSTERY" not in line and "412" in line)
check("read only: the ledger is untouched", json.dumps(LED, sort_keys=True) == frozen)

s = brief.spend("Software", "2026-09", LED, RULES, TODAY)["line"]
check("a named month", s.startswith("Software, September 2026: $374 on 4 accounts") and "nothing of this kind on record" in s, s)
g = brief.spend("groceries", "", LED, RULES, TODAY)["line"]
check("another envelope by name", g.startswith("Groceries, October 2026: $212 on 1 account (1 charge)."), g)
m = brief.spend("edit suite", "", LED, RULES, TODAY)["line"]
check("a word in the charge when no envelope has that name", m.startswith("Edit suite, October 2026: $58 on 1 account") and "went from $20.00 to $58.00" in m, m)
a = brief.spend("", "", LED, RULES, TODAY)["line"]
check("nothing named: everything that sits in an envelope, and only that", a.startswith("Everything in an envelope, October 2026: $624 on 4 accounts (13 charges)."), a.splitlines()[0])
z = brief.spend("travel", "", LED, RULES, TODAY)
check("a kind with no charges says so plainly", z["ok"] and "no charges on the 4 linked accounts" in z["line"], z["line"])
check("no accounts linked is said, not answered with zero", brief.spend("software", "", {"accounts": {}, "transactions": {}}, RULES, TODAY)["ok"] is False)
check("January looks back to December", brief._prev_month("2026-01") == "2025-12" and brief._month_name("2025-12") == "December 2025")
check("the chat tool knows the question", "spend" in brief.OPS)

print()
print("FAILED: " + ", ".join(FAILS) if FAILS else "all money-question checks passed")
sys.exit(1 if FAILS else 0)

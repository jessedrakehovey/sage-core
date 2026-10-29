"""Offline tests for mail_arm (money/home follow-ups armed from mail tickets)."""
import os as _os, tempfile as _tf
_os.environ.setdefault("WV_LOG_DIR", _tf.mkdtemp(prefix="wv_logs_"))   # never the live ~/.claude logs
import os
import sys
import tempfile
from datetime import date
from pathlib import Path

HERE = Path(__file__).resolve().parent
TMP = Path(tempfile.mkdtemp(prefix="wv_ma_"))
os.environ["ASSISTANT_DATA"] = str(TMP)
sys.path.insert(0, str(HERE.parent / "scripts"))
import followups as fu  # noqa: E402
import mail_arm  # noqa: E402
import tickets  # noqa: E402

FAILS = []
TODAY = date(2026, 9, 30)


def check(name, cond, extra=""):
    print(("ok   " if cond else "FAIL ") + name + (f"  {extra}" if extra and not cond else ""))
    if not cond:
        FAILS.append(name)


c = mail_arm.classify("Your SoCal Edison bill is ready", "Amount due $120.44. Payment due on Oct 15, 2026.", TODAY)
check("bill with due date", c and c[:2] == ("money", "bill-due") and c[2]["anchor"] == "2026-10-15" and c[2]["amount"] == "$120.44", c)
c = mail_arm.classify("Your order has shipped", "Estimated delivery 10/03. Track your package.", TODAY)
check("shipment with slash date", c and c[:2] == ("home", "delivery-arriving") and c[2]["anchor"] == "2026-10-03", c)
c = mail_arm.classify("Your policy renews on January 5", "No action needed.", TODAY)
check("renewal rolls to next year", c and c[:2] == ("money", "renewal") and c[2]["anchor"] == "2027-01-05", c)
check("no date -> nothing", mail_arm.classify("Your bill is ready", "Log in to view your statement.", TODAY) is None)
check("a yearless date just past is not rolled to next year",
      mail_arm.classify("Payment received", "Thanks - your payment due Sep 28 was received.", TODAY) is None)
c = mail_arm.classify("Statement ready", "Statement date 10/01. Your payment due on 10/25 is $88.10", TODAY)
check("the date after the keyword wins", c and c[2]["anchor"] == "2026-10-25" and c[2]["amount"] == "$88.10", c)
c = mail_arm.classify("Renewal notice", "Your policy renews on 01/05/27", TODAY)
check("a 2-digit year is taken as written", c and c[2]["anchor"] == "2027-01-05", c)
check("past date -> nothing", mail_arm.classify("Payment due", "due on 2026-09-01", TODAY) is None)
check("date but no bill/ship/renew words -> nothing", mail_arm.classify("Dinner Oct 12?", "see you then", TODAY) is None)

tickets.add_many([
    {"source": "mail:personal", "source_id": "<b1>", "lane": "money", "kind": "mail", "title": "Your SoCal Edison bill is ready",
     "summary": "Amount due $120.44. Payment due on Oct 15, 2026.", "payload": {"subject": "Your SoCal Edison bill is ready", "snippet": "Amount due $120.44. Payment due on Oct 15, 2026."}},
    {"source": "mail:personal", "source_id": "<n1>", "lane": "personal", "kind": "mail", "title": "Sunday?", "summary": "Dinner at 6", "payload": {"subject": "Sunday?"}},
], "test")
r = mail_arm.run(TODAY)
check("run arms one of two", r == {"scanned": 2, "armed": 1}, r)
items = [it for it in fu.load_state()["items"].values() if it["sop"] == "money"]
check("bill follow-up fires 3 days before", len(items) == 1 and items[0]["fire_at"].startswith("2026-10-12"), items)
check("second run scans nothing new", mail_arm.run(TODAY) == {"scanned": 0, "armed": 0})

print(f"\n{'ALL PASS' if not FAILS else 'FAILURES: ' + ', '.join(FAILS)} (scratch {TMP})")
sys.exit(1 if FAILS else 0)

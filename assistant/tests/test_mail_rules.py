"""Offline test for mail.dismissed_by_rule (config mail.dismiss_rules): an owner's standing "we don't use X" keeps the
matching mail from ever becoming a ticket (2026-10-30: a CRM's reconnect-Zoom notice was relayed to chat two days running
after the owner said every call is on Google Meet).   python assistant/tests/test_mail_rules.py
"""
import os as _os, tempfile as _tf
_os.environ.setdefault("WV_LOG_DIR", _tf.mkdtemp(prefix="wv_logs_"))
import os
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
os.environ["ASSISTANT_DATA"] = tempfile.mkdtemp(prefix="wv_mailrules_")
sys.path.insert(0, str(HERE.parent / "scripts"))
import mail  # noqa: E402

FAILS = 0


def ok(cond, msg):
    global FAILS
    print(("ok   " if cond else "FAIL ") + msg)
    if not cond:
        FAILS += 1


cfg = {"dismiss_rules": [{"from": "concierge@crm.example", "subject": "zoom", "why": "no video-call vendor X"},
                         {"from": "newsletter@shop.example"},
                         {"subject": r"^\[auto\]"},
                         "not a rule", {"from": "", "subject": ""}, {"subject": "("}]}
ok(mail.dismissed_by_rule("CRM Concierge <concierge@crm.example>", "Action needed: Reconnect your Zoom account", cfg) == "no video-call vendor X",
   "sender + subject rule matches, case-insensitive, and returns its why")
ok(mail.dismissed_by_rule("CRM Concierge <concierge@crm.example>", "Your contract was signed", cfg) == "",
   "the same sender with another subject is kept")
ok(mail.dismissed_by_rule("Shop <newsletter@shop.example>", "Anything", cfg) == "dismiss rule", "a sender-only rule matches any subject")
ok(mail.dismissed_by_rule("a@b.example", "[auto] nightly report", cfg) == "dismiss rule", "a subject-only rule matches any sender")
ok(mail.dismissed_by_rule("a@b.example", "hello", cfg) == "", "no rule, kept")
ok(mail.dismissed_by_rule("a@b.example", "hello", {}) == "", "no rules configured, kept")

print(f"\n{'ALL OK' if not FAILS else str(FAILS) + ' FAILED'}")
sys.exit(1 if FAILS else 0)

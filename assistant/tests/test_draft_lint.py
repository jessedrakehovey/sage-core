"""draft_lint: the owner's recurring corrections are caught before a draft becomes a card."""
import os, sys, tempfile, json
from pathlib import Path
HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent / "scripts"))
TMP = Path(tempfile.mkdtemp(prefix="wv_lint_"))
os.environ["ASSISTANT_CARDS"] = str(TMP / "cards"); os.environ["ASSISTANT_DATA"] = str(TMP / "data")
import draft_lint, cards  # noqa: E402
FAILS = []
def check(name, cond):
    print(("ok   " if cond else "FAIL ") + name); 
    if not cond: FAILS.append(name)
f = draft_lint.lint("Hey — happy to help, let me know if you need anything!", "personal", "text")
ids = {x["rule"] for x in f}
check("em dash blocks", any(x["rule"] == "no-em-dash" and x["severity"] == "block" for x in f))
check("open offer is info only", "no-open-offers" in ids and all(x["severity"] != "block" for x in f if x["rule"] == "no-open-offers"))
f = draft_lint.lint("Packages start at $3,200 and that date is open, you'll have it within 6 weeks.", "lead", "text")
check("price/date/turnaround to a lead blocks", any(x["rule"] == "no-price-date-turnaround-promise" and x["severity"] == "block" for x in f))
f = draft_lint.lint("We're so excited for you two. What venue are you looking at?", "lead", "text")
check("warm lead text is clean", not [x for x in f if x["severity"] in ("block", "warn")])
f = draft_lint.lint("Best regards, example-org", "personal", "text")
check("business voice in a personal text warns", any(x["rule"] == "no-example-voice-in-personal" for x in f))
check("long personal text warns", any(x["rule"] == "too-long" for x in draft_lint.lint("x" * 300, "personal", "text")))
check("audience from lane", draft_lint.audience_for("wedding") == "lead" and draft_lint.audience_for("personal") == "personal" and draft_lint.audience_for("owner6") == "owner6")
Q = {"header": "Text reply", "lane": "personal", "question": "Reply?\n[TEXT PREVIEW] To +18055550100:\nSure — see you at 6.",
     "options": [{"label": "Send (Recommended)"}, {"label": "Skip"}],
     "exec": {"kind": "imessage_send", "payload": {"account": "owner1-iphone", "to": "+18055550100", "body": "Sure — see you at 6."}, "approve_label": "Send (Recommended)"}}
r = cards.post([Q], "test")
check("a card whose draft has a block is refused with the rule named", not r.get("posted") and "DRAFT_LINT" in r.get("reason", "") and "no-em-dash" in r.get("reason", ""))
Q2 = json.loads(json.dumps(Q)); Q2["exec"]["payload"]["body"] = "Sure, see you at 6. Best regards"; Q2["question"] = Q2["question"].replace("Sure — see you at 6.", Q2["exec"]["payload"]["body"])
r = cards.post([Q2], "test")
check("a warning posts with advice", r.get("posted") and any("DRAFT_LINT warn" in a for a in r.get("advice", [])))
print("\n" + ("ALL OK" if not FAILS else "FAILED: " + ", ".join(FAILS)))
sys.exit(1 if FAILS else 0)

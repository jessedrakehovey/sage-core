"""test_pending.py - offline checks for the owners' approval queue (assistant/agent/pending.py):
no duplicate filing, the backlog fold, age-out, cards in chat and what each tap does.

  python assistant/tests/test_pending.py
"""
import os
import sys
import tempfile
import time
from pathlib import Path

TMP = Path(tempfile.mkdtemp(prefix="pending_test_"))
os.environ["WV_LOG_DIR"] = str(TMP / "logs")
os.environ["AGENT_STATE"] = str(TMP / "state")
os.environ["AGENT_DATA"] = str(TMP / "data")
os.environ["AGENT_SUGGESTIONS"] = str(TMP / "skill-suggestions.md")
os.environ["ASSISTANT_DATA"] = str(TMP / "adata")
os.environ["ASSISTANT_CARDS"] = str(TMP / "cards")
os.environ["AGENT_HOME_SPACE"] = ""
os.environ["CHAT_PUSH"] = "off"

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "agent"))
sys.path.insert(0, str(ROOT / "scripts"))
import core, pending, actions  # noqa: E402,E401
import cards  # noqa: E402
import chat_push  # noqa: E402
chat_push.push_card = chat_push.push_say = lambda *a, **k: None       # never the real Google Chat
cards.say = lambda owner, lane, text, thread="": {"said": True}
actions.WAKE_DOER = False

PASS = FAIL = 0


def check(name, cond):
    global PASS, FAIL
    PASS += bool(cond)
    FAIL += (not cond)
    print(("ok   " if cond else "FAIL ") + name)


SUGG = Path(os.environ["AGENT_SUGGESTIONS"])
SUGG.write_text("# Skill suggestions\n\n## Pending\n\n## Applied automatically\n\n## Dismissed (latched — never re-propose)\n", encoding="utf-8")
day = time.strftime("%Y%m%d")
old = time.strftime("%Y%m%d", time.localtime(time.time() - 12 * 86400))


def ask(key, text):
    core.log_suggestion("## Pending", key, ["**The agent's nightly review proposes a change only an owner may make (not applied).**",
                                            f"*Proposal:* {text}", "*Evidence:* t1"])


PRICE = "Change the live price-engine Google Sheet and Apps Script from 0.95 to 1.0 for Jan, Feb, Jul, Aug and Mon-Thu."
ask(f"sage-dream-ask-{day}-060000-1", PRICE)
ask(f"sage-dream-ask-{day}-090000-1", "The live price-engine Google Sheet and Apps Script still use 0.95 for Jan, Feb, Jul, Aug and Mon-Thu; change them to 1.0.")
check("filing: a reworded repeat of an open ask is not filed twice", len(pending.entries()) == 1)
ask(f"sage-dream-ask-{day}-100000-1", "staff1 still has to be let into each vision-call Meet by hand; make him a co-host.")
check("filing: a different ask is filed", len(pending.entries()) == 2)
core.pend("rule", "Never send a nudge to a lead who booked a meeting", "dream [t1]", "sage-dream")
core.pend("rule", "Never send a nudge to a lead who booked a meeting", "dream [t2]", "sage-dream")
check("filing: the same protected rule is queued once", sum(e["kind"] == "rule" for e in pending.entries()) == 1)
ask(f"sage-dream-ask-{old}-060000-2", "Only the Friday Catalina event got onto the calendar; add Saturday and Sunday.")
ask(f"sage-dream-ask-{day}-110000-3", "Build request (protected core, not filed): Doer report cut off: fix how the doer's report is relayed into chat.")
kinds = {e["key"]: e["kind"] for e in pending.entries()}
check("parse: kinds are told apart (ask / rule / build)",
      sorted(set(kinds.values())) == ["ask", "build", "rule"])

# the backlog fold: two copies already in the file (written before the guard) become one
text = SUGG.read_text(encoding="utf-8")
dup = (f"- key: sage-dream-retire-r9-{old}\n  **The nightly review suggests retiring rule r9 (owner1 taught it, so it was not retired).**\n"
       "  *Rule:* Always say hi\n  *Why:* stale [t1]\n")
text = text.replace("## Pending\n", "## Pending\n" + dup + dup.replace(old, day), 1)
SUGG.write_text(text, encoding="utf-8")
folds = pending.dedupe()
es = [e for e in pending.entries() if e["kind"] == "retire"]
check("dedupe: two retire entries for the same rule fold into the newest, with a 'raised' line",
      len(es) == 1 and day in es[0]["key"] and "Raised 2 times" in es[0]["raw"] and folds)

exp = pending.age_out()
check("age-out: an ask older than a week leaves the queue, a fresh one stays",
      len(exp) == 1 and old in exp[0] and any("staff1" in e["text"] for e in pending.entries()))
ask(f"sage-dream-ask-{day}-120000-9", "Only the Friday Catalina event got onto the calendar; add Saturday and Sunday.")
check("latch: an expired ask is not filed again", not any("Catalina" in e["text"] for e in pending.entries()))

posted = pending.surface()
live = {c["id"]: c for c in cards._live()}
qs = [c["questions"][0] for c in live.values()]
check("surface: cards go up, at most MAX_OPEN, never for the builder's own requests",
      0 < len(posted) <= pending.MAX_OPEN and not any(kinds.get(k) == "build" for k in posted)
      and all(q.get("pending_key") for q in qs))
check("surface: every card has an action button marked Recommended",
      all(any("(Recommended)" in o["label"] for o in q["options"]) for q in qs))
check("surface: a second tick posts nothing new while those are open", pending.surface() == [])

# taps
rule_key = next(e["key"] for e in pending.entries() if e["kind"] == "rule")
r = pending.answer(rule_key, pending.ADD, by="owner1")
check("tap: 'Add the rule' adds it as an active rule and closes the entry",
      r["ok"] and any("nudge" in x["rule"] for x in core.rules_active()) and rule_key not in [e["key"] for e in pending.entries()])
core.pend("rule", "Never send a nudge to a lead who booked a meeting", "dream [t3]", "sage-dream")
check("tap: an approved rule is never queued again", not any(e["kind"] == "rule" for e in pending.entries()))

price_key = next(e["key"] for e in pending.entries() if "price-engine" in e["text"])
r = pending.answer(price_key, pending.DO_IT, text="sheet is in Drive", by="owner1")
import tickets  # noqa: E402
tk = [t for t in tickets._load().values() if "price-engine" in str(t.get("summary")) and "sheet is in Drive" in str(t.get("summary"))]
check("tap: 'Have Sage do it' queues an operator ticket and closes the entry",
      r["ok"] and price_key not in [e["key"] for e in pending.entries()] and tk)

check("tap: an ordinary ask goes to the operator, not the doer", tk and all(t.get("kind") == "owner1_request" for t in tk))
ask(f"sage-dream-ask-{day}-110000-1", "Put staff1 on the Vision Meeting in the HoneyBook Scheduler as a second host (Multiple hosts, Add host).")
host_key = next(e["key"] for e in pending.entries() if "Scheduler" in e["text"])
r = pending.answer(host_key, pending.DO_IT, by="owner1")
dk = [t for t in tickets._load().values() if "Scheduler as a second host" in str(t.get("summary"))]
check("tap: 'Have Sage do it' on Scheduler hosts goes to the doer's browser with the slot-count steps (2026-10-20)",
      r["ok"] and dk and dk[0].get("kind") == "agent_run" and dk[0].get("lane") == "wedding"
      and "Preview slot count before and after" in str((dk[0].get("payload") or {}).get("text"))
      and host_key not in [e["key"] for e in pending.entries()])

ask(f"sage-dream-ask-{day}-115000-1", "Check cells A1-A4 on the pricing Google Sheet and confirm they show the season multipliers.")
look_key = next(e["key"] for e in pending.entries() if "A1-A4" in e["text"])
r = pending.answer(look_key, pending.DO_IT, by="owner1")
lk = [t for t in tickets._load().values() if "A1-A4" in str(t.get("summary"))]
check("tap: 'Have Sage do it' on a sheet check goes to the doer, read-only, not back to the owner (2026-10-19)",
      r["ok"] and lk and lk[0].get("kind") == "agent_run"
      and "Change, save or send nothing" in str((lk[0].get("payload") or {}).get("text"))
      and look_key not in [e["key"] for e in pending.entries()]
      and "handed to the doer" in SUGG.read_text(encoding="utf-8"))
check("routing: a change to a sheet is not a read-only look", not pending._is_look(PRICE)
      and pending._is_look("Look at example.invalid and see if the pricing page loads"))

staff1_key = next(e["key"] for e in pending.entries() if "staff1" in e["text"])
r = pending.answer(staff1_key, pending.DROP, by="owner1")
ask(f"sage-dream-ask-{day}-130000-1", "staff1 still has to be let into each vision-call Meet by hand; make him a co-host.")
check("tap: 'Drop it' closes the entry for good (recorded under Dismissed, not re-filed)", r["ok"]
      and not any("staff1" in e["text"] for e in pending.entries())
      and f"{staff1_key}-dropped" in SUGG.read_text(encoding="utf-8").split("## Dismissed")[1])

ret_key = next(e["key"] for e in pending.entries() if e["kind"] == "retire")
check("tap: an unknown button is refused, nothing closes", not pending.answer(ret_key, "Banana")["ok"]
      and ret_key in [e["key"] for e in pending.entries()])

# the sweep hands a tap on a pending card to pending.answer
c = next(c for c in cards._live() if c["questions"][0]["pending_key"] == ret_key) if any(
    c["questions"][0]["pending_key"] == ret_key for c in cards._live()) else None
if c is None:
    core.save(pending.CARDS, {})
    for x in cards._live():
        cards._retire(x, "superseded")
    pending.surface()
    c = next(c for c in cards._live() if c["questions"][0]["pending_key"] == ret_key)
cards.answer(c["id"], pending.KEEP)
cards.UNDO_S = 0
cards.sweep()
check("sweep: a tap on a queue card is done by code (the entry closes, no operator ticket)",
      ret_key not in [e["key"] for e in pending.entries()])

check("the section keeps its headings", all(h in SUGG.read_text(encoding="utf-8") for h in ("## Pending", "## Applied automatically", "## Dismissed")))

print(f"\n{PASS} passed, {FAIL} failed")
sys.exit(1 if FAIL else 0)

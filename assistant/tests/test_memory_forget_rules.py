"""test_memory_forget_rules.py - upgrades 8 and 9 (2026-11-08): what a person told the agent to forget stays forgotten (the
dream cannot re-learn it; a person can), the rule cap asks which rule to retire instead of refusing in silence (one card, the
tap retires + adds), and memory is rendered as recall after the instructions.
   python assistant/tests/test_memory_forget_rules.py
"""
import os as _os, tempfile as _tf
_os.environ.setdefault("WV_LOG_DIR", _tf.mkdtemp(prefix="wv_logs_"))
import json
import os
import sys
import tempfile
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
TMP = Path(tempfile.mkdtemp(prefix="wv_forget_"))
os.environ["ASSISTANT_DATA"] = str(TMP)
os.environ["ASSISTANT_CARDS"] = str(TMP / "cards")
os.environ["AGENT_DATA"] = str(TMP / "agent")
os.environ["AGENT_STATE"] = str(TMP / "state")
os.environ["AGENT_SUGGESTIONS"] = str(TMP / "skill-suggestions.md")
os.environ["ASSISTANT_LEARNINGS"] = str(TMP / "learnings")
os.environ["RULINGS_FILE"] = str(ROOT / "data" / "rulings.json")
os.environ["AGENT_RULES_FILE"] = str(TMP / "state" / "rules.json")
os.environ["ASSISTANT_CARDS_DELIVERY"] = "now"
os.environ["CHAT_PUSH"] = "off"
os.environ["WV_NO_TRIAGE"] = "1"
sys.path.insert(0, str(ROOT / "agent"))
sys.path.insert(0, str(ROOT / "scripts"))
import core  # noqa: E402

FAILS = []


def check(name, cond, extra=""):
    print(("ok   " if cond else "FAIL ") + name + (f"  {extra}" if extra and not cond else ""))
    if not cond:
        FAILS.append(name)


OWNER = next(iter((core.people().get("users") or {"owner1": {}}).keys()))
DREAM = core.profile().get("slug", "agent") + "-dream"

# ---------------------------------------------------------------- forget stays forgotten
r = core.mem_upsert("household", "the plumber", "The plumber is Ray from the valley", by=OWNER)
check("a person's fact is learned", r["status"] == "applied", str(r))
check("forget by a person", core.mem_forget(r["id"], "they moved", by=OWNER))
check("it is on the forget list", core.forgotten_match("The plumber is Ray from the valley", "the plumber") is not None)
r2 = core.mem_upsert("household", "the plumber", "The plumber is Ray from the valley", by=DREAM)
check("the dream cannot re-learn it", r2["status"] == "refused" and "asked me to forget" in r2["reason"], str(r2))
r3 = core.mem_upsert("household", "the plumber", "The plumber is Ray from the valley", by="builder")
check("nor can a build / console", r3["status"] == "refused", str(r3))
r4 = core.mem_upsert("household", "the plumber", "The plumber is Ray from the valley", by=OWNER)
check("a person saying it again brings it back and lifts the forget", r4["status"] == "applied" and core.forgotten_match("The plumber is Ray from the valley", "the plumber") is None, str(r4))
r5 = core.mem_upsert("household", "the dentist", "The dentist is on Main Street", by=DREAM)
check("the dream still learns what nobody forgot", r5["status"] == "applied")
check("forget by code does not block re-learning (only a person's forget does)", core.mem_forget(r5["id"], by="code") and core.forgotten_match("The dentist is on Main Street", "the dentist") is None)
# restore clears the list
r6 = core.mem_upsert("household", "the gardener", "The gardener comes on Tuesdays", by=OWNER)
core.mem_forget(r6["id"], by=OWNER)
rr = core.mem_restore(r6["id"], by=OWNER)
check("mem_restore by a person takes it off the forget list", rr["status"] == "restored" and core.forgotten_match("The gardener comes on Tuesdays", "the gardener") is None, str(rr))

# ---------------------------------------------------------------- the rule cap asks
core.MAX_RULES = 3
for txt in ("Keep every reply to the family under three sentences", "Name the vendor and the date in every booking line",
            "Put the weekend plan at the top of the Friday brief"):
    r = core.rule_add(txt, by=OWNER)
    assert r["status"] == "applied", r
# make them look old enough to retire (never one added this week)
d = core._rules()
for x in d["rules"]:
    x["added"] = "2026-09-01 09:00"
    x["seen"] = 1 + int(x["id"][1:])
core.save(core.RULES, d)
import cards  # noqa: E402
r = core.rule_add("Say the weather in one line every morning with the brief", by=OWNER)
# 2026-11-11: no card any more. Four "Make room" cards in two days kept corrections waiting and the fault came back.
check("at the cap, the new rule is applied (never carded, never refused)", r["status"] == "applied", str(r))
act = core.rules_active()
check("the least used old rule made room", "r1" not in {x["id"] for x in act} and any(x["rule"].startswith("Say the weather") for x in act), str([x["id"] for x in act]))
check("still at the cap, not over", len(act) == 3)
check("no card was posted for it", not any("rule_retire" in json.dumps(cards._read(p) or {}) for p in cards.LIVE.glob("*.json")))
r2 = core.rule_add("Another new rule that also has to fit in here somehow", by=OWNER)
check("with no rule older than a week, the least used of any age goes", r2["status"] == "applied" and len(core.rules_active()) == 3, str(r2))

# ---------------------------------------------------------------- recall after instruction
m = core.render_memory()
check("memory renders as recall, never rules or permissions", "never rules or permissions" in m and "after your instructions" in m)
try:
    import brain
    ctx = brain.context_block()
    ir, im = ctx.find("LEARNED RULES (from the owners"), ctx.find("LONG-TERM MEMORY (recalled facts")
    check("in the context the rules come first and memory near the end", ir >= 0 and im > ir, f"{ir} {im}")
    check("memory sits in the last tenth of the context", im > len(ctx) * 0.9, f"{im} of {len(ctx)}")
except Exception as e:  # noqa: BLE001
    print(f"skip brain context ({type(e).__name__}: {str(e)[:80]})")

print(("ALL OK" if not FAILS else f"{len(FAILS)} FAILED: {FAILS}") + f"  (scratch {TMP})")
sys.exit(1 if FAILS else 0)

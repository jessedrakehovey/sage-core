"""test_memory_cap.py - the memory cap retires only what the nightly dream inferred, and a retired fact can be
restored (ported from the property OS 2026-10-30, where the cap had aged out eleven of the owner's own rules).

Scratch AGENT_DATA / AGENT_STATE; MAX_MEMORY lowered to 4 for the test. Never the live store.
  python assistant/tests/test_memory_cap.py
"""
import os
import sys
import tempfile
from pathlib import Path

TMP = Path(tempfile.mkdtemp(prefix="memcap_test_"))
os.environ["WV_LOG_DIR"] = str(TMP / "logs")
os.environ["AGENT_DATA"] = str(TMP / "data")
os.environ["AGENT_STATE"] = str(TMP / "state")
os.environ["AGENT_SUGGESTIONS"] = str(TMP / "skill-suggestions.md")
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "agent"))
import core  # noqa: E402

FAILS = []


def check(name, cond, extra=""):
    print(("ok   " if cond else "FAIL ") + name + (f"  {extra}" if extra and not cond else ""))
    if not cond:
        FAILS.append(name)


def active_ids():
    return [f["id"] for f in core.memory_active()]


def by_id(mid):
    return next(f for f in core._mem()["facts"] if f["id"] == mid)


core.MAX_MEMORY = 4
DREAM = core.profile().get("slug", "agent") + "-dream"
check("the dream's author tag is a cap candidate", core.mem_cap_retirable({"by": DREAM}) and core.mem_cap_retirable({"by": "sage-dream"}))
check("a person's words are never a cap candidate",
      not any(core.mem_cap_retirable({"by": b}) for b in ("owner1", "owner1-cli", "builder", "staff2", "", None)))

# ---------------------------------------------------------------- 1. the cap takes the least-seen oldest DREAM fact
m1 = core.mem_upsert("household", "Trash day", "Trash goes out on Tuesday night", by="owner1")
m2 = core.mem_upsert("process", "Invoices", "Invoices are numbered by year then sequence", by=DREAM)
m3 = core.mem_upsert("preference", "Coffee", "Owner one takes coffee black in the morning", by="owner1-cli")
m4 = core.mem_upsert("business", "Hours", "The office closes at four on Fridays", by=DREAM)
core.mem_upsert("business", "Hours", "The office closes at four on Fridays", by=DREAM)       # seen twice now
check("four facts active under a cap of 4", len(active_ids()) == 4, str(active_ids()))
m5 = core.mem_upsert("person", "Plumber", "The plumber answers texts faster than calls", by="owner1")
check("the fifth fact is applied", m5["status"] == "applied")
check("the cap retired the least-seen dream fact, not the older owner fact",
      m2["id"] not in active_ids() and m1["id"] in active_ids() and m4["id"] in active_ids(), str(active_ids()))
check("the victim is marked (cap)", str(by_id(m2["id"]).get("retired", "")).endswith("(cap)"))
check("the retirement is ledgered with a restore undo",
      any(r.get("kind") == "memory_forget" and r.get("undo", {}).get("op") == "mem_restore" and r["undo"].get("id") == m2["id"]
          for r in core.rows(core.CHANGES)))

# ---------------------------------------------------------------- 2. only people's facts left: over the cap, no victim
m6 = core.mem_upsert("process", "Keys", "Spare keys hang in the hall cupboard", by="owner1")
check("with one dream fact left the cap takes it", m4["id"] not in active_ids() and m6["id"] in active_ids(), str(active_ids()))
m7 = core.mem_upsert("process", "Bins", "Recycling is collected on alternate weeks", by="owner1")
check("every active fact is a person's: the set runs over the cap with no victim",
      m7["status"] == "applied" and len(active_ids()) == 5 and all(f["id"] in active_ids() for f in (m1, m3, m5, m6, m7)), str(active_ids()))
check("no person's fact carries a (cap) mark", not any(str(f.get("retired", "")).endswith("(cap)") for f in core._mem()["facts"] if not core.mem_cap_retirable(f)))

# ---------------------------------------------------------------- 3. restore
r = core.mem_restore(m2["id"], "the owner wants it back", by="owner1")
check("restore re-activates a cap victim", r["status"] == "restored" and m2["id"] in active_ids(), str(r))
check("restore clears the retired mark and stamps restored", "retired" not in by_id(m2["id"]) and by_id(m2["id"]).get("restored"))
check("restore is ledgered with a forget undo",
      any(r.get("kind") == "memory_restore" and r.get("undo", {}).get("op") == "mem_forget" for r in core.rows(core.CHANGES)))
check("restore refuses an active fact", core.mem_restore(m2["id"])["status"] == "refused")
check("restore refuses an unknown id", core.mem_restore("m999")["status"] == "refused")
# a similar active fact on the same subject blocks the restore unless forced
core.mem_forget(m4["id"], "test", by="owner1")
dup = core.mem_upsert("business", "Hours", "The office closes at 4 on Fridays", by="owner1")
check("a reworded twin folds or applies", dup["status"] in ("applied", "folded"))
if dup["status"] == "applied":
    r = core.mem_restore(m4["id"])
    check("restore says similar when an active twin exists", r["status"] == "similar" and r["similar_to"] == dup["id"], str(r))
    r = core.mem_restore(m4["id"], force=True)
    check("--force restores past a similar twin", r["status"] == "restored" and m4["id"] in active_ids(), str(r))
else:
    check("restore of a folded-away fact works when nothing similar is active", core.mem_restore(m4["id"])["status"] in ("restored", "similar"))

# ---------------------------------------------------------------- 4. undo of a cap retirement still works
victim_row = next(r for r in core.rows(core.CHANGES) if r.get("kind") == "memory_forget" and r["undo"].get("id") == m2["id"])
core.mem_forget(m2["id"], "test", by="owner1")
check("undo of the cap row restores the fact", core.undo(victim_row["id"], by="owner1").startswith("Undone") and m2["id"] in active_ids())

print(f"\n{len(FAILS)} failures" if FAILS else "\nALL OK")
sys.exit(1 if FAILS else 0)

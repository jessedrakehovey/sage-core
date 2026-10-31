"""test_builder_order.py - the builder's queue is oldest-first and keeps one slot for another agent's upgrade.

2026-10-30: 92 build requests were open in Sage's wiki; the builder takes two per run, a few runs a day, and read
them in path order, so the 30 "Build request Port from <another agent> ..." notes core_sync's relay had filed
(Wiki/Personal/Build request P...) sat behind forty others and none was ever attempted -- the fixes they carried
were live in the other agent for days. collect() now orders by the note's age; pick() keeps `first` (an owner's
ask in chat) first and one slot for the oldest port request.
  python assistant/tests/test_builder_order.py
"""
import os
import sys
import tempfile
import time
from pathlib import Path

TMP = Path(tempfile.mkdtemp(prefix="builder_order_"))
os.environ["WV_LOG_DIR"] = str(TMP / "logs")
os.environ["AGENT_STATE"] = str(TMP / "state")
os.environ["AGENT_DATA"] = str(TMP / "data")
os.environ["AGENT_SUGGESTIONS"] = str(TMP / "skill-suggestions.md")
os.environ["ASSISTANT_DATA"] = str(TMP / "adata")
os.environ["CHAT_PUSH"] = "off"
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "agent"))
sys.path.insert(0, str(ROOT / "scripts"))
import core  # noqa: E402
import builder  # noqa: E402

FAILS = []


def check(name, cond, extra=""):
    print(("ok   " if cond else "FAIL ") + name + (f"  {extra}" if extra and not cond else ""))
    if not cond:
        FAILS.append(name)


# ---------------------------------------------------------------- pick(): pure, on request dicts
def req(key, ts, title=None):
    return {"key": key, "title": title or Path(key).stem, "source": "wiki", "text": "## Build request\nx", "ts": ts}


OLD_OWN = req("Wiki/Personal/Build request Zebra stripes in the digest.md", 100)
MID_OWN = req("Wiki/Personal/Build request Alpha sorting of cards.md", 200)
NEW_OWN = req("Wiki/Operations/Build request Beta feature.md", 300)
OLD_PORT = req("Wiki/Personal/Build request Port from Other agent - rulings.find a lone word.md", 150)
NEW_PORT = req("Wiki/Personal/Build request Port from Other agent - chat agent rules the cap.md", 250)
ALL = [NEW_OWN, NEW_PORT, MID_OWN, OLD_PORT, OLD_OWN]

got = [r["key"] for r in builder.pick(ALL, 2)]
check("oldest first, not path order; one slot is the oldest port request",
      got == [OLD_OWN["key"], OLD_PORT["key"]], str(got))
got = [r["key"] for r in builder.pick([NEW_OWN, MID_OWN, OLD_OWN], 2)]
check("no port request open: the two oldest", got == [OLD_OWN["key"], MID_OWN["key"]], str(got))
got = [r["key"] for r in builder.pick(ALL, 2, first=NEW_OWN["key"])]
check("an owner's ask in chat is first, the port slot stays", got == [NEW_OWN["key"], OLD_PORT["key"]], str(got))
got = [r["key"] for r in builder.pick([NEW_PORT, OLD_PORT, OLD_OWN], 2)]
check("port requests are oldest-first among themselves and never take both slots",
      got == [OLD_OWN["key"], OLD_PORT["key"]], str(got))
got = [r["key"] for r in builder.pick([NEW_PORT, OLD_PORT], 2)]
check("only port requests open: both slots are theirs, oldest first", got == [OLD_PORT["key"], NEW_PORT["key"]], str(got))
got = [r["key"] for r in builder.pick(ALL, 1)]
check("a one-slot run takes the oldest request; no slot is reserved", got == [OLD_OWN["key"]], str(got))
got = [r["key"] for r in builder.pick(ALL, 3)]
check("three slots: two oldest plus the oldest port (already among them) and the next oldest",
      got == [OLD_OWN["key"], OLD_PORT["key"], MID_OWN["key"]], str(got))
check("pick of nothing is nothing", builder.pick([], 2) == [])

# ---------------------------------------------------------------- collect(): the wiki notes carry their age
wiki = core.VAULT / "Wiki" / "Personal"
wiki.mkdir(parents=True, exist_ok=True)
names = ["Build request Order test A newest.md", "Build request Order test B oldest.md", "Build request Port from Order test agent - C.md"]
paths = [wiki / n for n in names]
try:
    now = time.time()
    for p, age in zip(paths, (10, 3000, 1000)):
        p.write_text(f"# {p.stem}\n\n## Build request\n- order test\n", encoding="utf-8")
        os.utime(p, (now - age, now - age))
    reqs = [r for r in builder.collect({}) if "Order test" in r["key"]]
    keys = [r["key"] for r in reqs]
    check("collect returns the test notes oldest first",
          keys == [f"Wiki/Personal/{names[1]}", f"Wiki/Personal/{names[2]}", f"Wiki/Personal/{names[0]}"], str(keys))
    check("every wiki request carries its age", all(r.get("ts") for r in reqs))
    got = [r["key"] for r in builder.pick(reqs, 2)]
    check("pick on collected notes: oldest own note, then the port note", got == [f"Wiki/Personal/{names[1]}", f"Wiki/Personal/{names[2]}"], str(got))
finally:
    for p in paths:
        try:
            p.unlink()
        except OSError:
            pass

print(f"\n{len(FAILS)} failures" if FAILS else "\nALL OK")
sys.exit(1 if FAILS else 0)

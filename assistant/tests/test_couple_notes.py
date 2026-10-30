"""Offline tests for wiki_writer's couple notes (2026-10-18): the sync-safe '## Story and notes' region on a
Wiki/Couples page, the loose couple lookup, and proof that the CRM sync block rewrite leaves the region
alone. Pure functions + a scratch Couples folder; no git, no lint, no model.
    python assistant/tests/test_couple_notes.py
"""
import os as _os, tempfile as _tf
_os.environ.setdefault("WV_LOG_DIR", _tf.mkdtemp(prefix="wv_logs_"))
import re
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
TMP = Path(tempfile.mkdtemp(prefix="wv_cn_"))

_os.environ["AGENT_STATE"] = str(TMP / "state")
_os.environ["AGENT_DATA"] = str(TMP / "data")
sys.path.insert(0, str(ROOT / "agent"))
import wiki_writer as ww  # noqa: E402

FAILS = []


def check(name, cond, extra=""):
    print(("ok   " if cond else "FAIL ") + name + (f"  {extra}" if extra and not cond else ""))
    if not cond:
        FAILS.append(name)


PAGE = """---
type: couple
area: couples
tags: [booking]
created: 2026-09-25
updated: 2026-10-16
status: draft
crm_id: "9ce6fbde7ad780639e5b729f"
---

# Ashcombe — 2026-11-07

<!-- hb:begin -->
> [!info] CRM (auto-synced)
> - **Stage:** Questionnaire Sent
<!-- hb:end -->

## The couple

- **Names:** Teo Ashcombe & partner

## Open items

- [ ] Venue: `TODO`

## Related

- [[CRM]]
"""

# 1. first note creates the region before ## Open items
t1 = ww.insert_couple_notes(PAGE, "They met through Sav, who is officiating.", "agent:20261018-120000", "Sage", "2026-10-18")
check("region created with heading + markers", ww.NOTES_HEADING in t1 and ww.NOTES_BEGIN in t1 and ww.NOTES_END in t1)
check("region sits before Open items", t1.index(ww.NOTES_END) < t1.index("## Open items"))
check("dated section inside the region with its own undo marker",
      t1.index(ww.NOTES_BEGIN) < t1.index("<!-- agent:20261018-120000:begin -->") < t1.index("officiating") < t1.index("<!-- agent:20261018-120000:end -->") < t1.index(ww.NOTES_END))
check("the CRM block is untouched", t1.count("<!-- hb:begin -->") == 1 and "Questionnaire Sent" in t1)

# 2. a second note lands inside the same region, after the first
t2 = ww.insert_couple_notes(t1, "Two dogs; the small one is shy for five minutes.", "agent:20261018-130000", "Sage", "2026-10-18")
check("one region only", t2.count(ww.NOTES_BEGIN) == 1 and t2.count(ww.NOTES_END) == 1 and t2.count(ww.NOTES_HEADING) == 1)
check("second note after the first, inside the region", t2.index("officiating") < t2.index("Two dogs") < t2.index(ww.NOTES_END))

# 3. the CRM sync's rewrite (only the hb block) preserves the region exactly
BEGIN, END = "<!-- hb:begin -->", "<!-- hb:end -->"
block = "<!-- hb:begin -->\n> [!info] CRM (auto-synced)\n> - **Stage:** Questionnaire Done\n<!-- hb:end -->"
t3 = re.sub(re.escape(BEGIN) + r".*?" + re.escape(END), lambda _: block, t2, count=1, flags=re.S)
region = lambda s: s[s.index(ww.NOTES_HEADING):s.index(ww.NOTES_END) + len(ww.NOTES_END)]
check("sync rewrote the stage", "Questionnaire Done" in t3 and "Questionnaire Sent" not in t3)
check("sync left the story region byte-identical", region(t3) == region(t2))

# 4. undo takes exactly one dated section out (same regex wiki_writer.remove uses)
marker = "agent:20261018-120000"
t4 = re.sub(r"\n*<!-- " + re.escape(marker) + r":begin -->.*?<!-- " + re.escape(marker) + r":end -->\n?", "\n", t2, flags=re.S)
check("undo removes only its section", "officiating" not in t4 and "Two dogs" in t4 and ww.NOTES_BEGIN in t4)

# 5. a page with no Open items gets the region at the end
t5 = ww.insert_couple_notes(PAGE.split("## Open items")[0], "Note.", "agent:1", "Sage", "2026-10-18")
check("no Open items -> region at the end", t5.rstrip().endswith(ww.NOTES_END))

# 6. loose couple lookup over a scratch Couples folder
ww.COUPLES = TMP / "Couples"
ww.COUPLES.mkdir(parents=True)
(ww.COUPLES / "2026-11-07 Ashcombe.md").write_text(PAGE, encoding="utf-8")
(ww.COUPLES / "2026-09-26 Ashdown-Hollowell.md").write_text(PAGE.replace("Ashcombe — 2026-11-07", "Ashdown-Hollowell — 2026-09-26"), encoding="utf-8")
(ww.COUPLES / "2027-06-12 Northcott.md").write_text(PAGE.replace("Ashcombe — 2026-11-07", "Northcott — 2027-06-12"), encoding="utf-8")
check("finds by surname", (ww.find_couple("Ashcombe") or Path("x")).name == "2026-11-07 Ashcombe.md")
check("finds by 'Teo and Wren Ashcombe's wedding'", (ww.find_couple("Teo and Wren Ashcombe's wedding") or Path("x")).name == "2026-11-07 Ashcombe.md")
check("finds by the page name", (ww.find_couple("2026-11-07 Ashcombe") or Path("x")).name == "2026-11-07 Ashcombe.md")
check("finds Ashdown-Hollowell by one half", (ww.find_couple("Hollowell") or Path("x")).name == "2026-09-26 Ashdown-Hollowell.md")
check("no match -> None", ww.find_couple("Glenrock") is None)
check("stop-words alone -> None", ww.find_couple("the wedding couple") is None)
check("couple_notes refuses an unknown couple", ww.couple_notes("Glenrock", "x" * 30, commit=False)["status"] == "refused")
check("a NEW couple page is refused (the sync makes those)",
      ww.write({"title": "2028-01-01 Nobody", "type": "couple", "body": "a body long enough to pass the check"}, commit=False)["status"] == "refused")

print(f"\n{'ALL PASS' if not FAILS else 'FAILURES: ' + ', '.join(FAILS)} (scratch {TMP})")
sys.exit(1 if FAILS else 0)

"""learn.py - the operator's CLI over scripts/lib/learnings.py (per-lane learnings ledger).

  python assistant/scripts/learn.py record --lane wedding "<one generalizable lesson>" [--context <qid>]
  python assistant/scripts/learn.py show --lane wedding

Files: assistant/learnings/<lane>.md (tracked in git; deduped; capped at 50 lines by the
library). cards.py sweep appends the RAW correction (his edit / typed answer) the moment a
card is harvested; the operator distills it into a lesson here; dreaming reads both.
Never sends, never touches anything client-facing.
"""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
LEARN_DIR = Path(os.environ.get("ASSISTANT_LEARNINGS") or (HERE.parent / "learnings"))
sys.path.insert(0, str(HERE.parent.parent / "scripts" / "lib"))
import learnings  # noqa: E402

LANES = ("wedding", "personal", "home", "money", "owner6")


def main():
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("record"); p.add_argument("lesson"); p.add_argument("--lane", required=True); p.add_argument("--context", default="")
    p = sub.add_parser("show"); p.add_argument("--lane", required=True)
    a = ap.parse_args()
    if a.lane not in LANES:
        print(f"lane must be one of {LANES}"); return 2
    path = LEARN_DIR / f"{a.lane}.md"
    if a.cmd == "record":
        ok = learnings.record_lesson(path, a.lesson, context=a.context, title=f"{a.lane} lane learnings")
        print("RECORDED" if ok else "SKIPPED (empty or duplicate)"); return 0
    if a.cmd == "show":
        print(path.read_text(encoding="utf-8") if path.exists() else f"(no learnings yet for {a.lane})"); return 0
    return 1


if __name__ == "__main__":
    sys.exit(main())

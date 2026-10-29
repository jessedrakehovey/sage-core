#!/usr/bin/env python3
"""skill_size_check.py - flag SKILL.md files that have grown too large.

The dreaming loop auto-patches SKILL.md files, and every line is re-injected into each cron's
LLM context on every run, so unbounded growth quietly inflates per-run token cost.
This reports any SKILL.md over the line cap so it can be trimmed or split to an overflow
file the dream session reads. REPORT-ONLY (never edits -- auto-trimming a skill is unsafe).
Zero-LLM, stdlib. Exit 1 if any file is over the cap (so a cron can warn).

  python skill_size_check.py             # report (cap 450 lines)
  python skill_size_check.py --cap 400
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

# scripts/lib/skill_size_check.py -> parents[2] = wedding folder root
VAULT = Path(__file__).resolve().parents[2]
SKILLS = VAULT / ".claude" / "skills"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--cap", type=int, default=450, help="line cap per SKILL.md (default 450)")
    a = ap.parse_args()
    rows = []
    for p in sorted(SKILLS.glob("*/SKILL.md")):
        try:
            n = sum(1 for _ in p.open(encoding="utf-8", errors="replace"))
        except Exception:
            continue
        rows.append((p.parent.name, n))
    over = [(name, n) for name, n in rows if n > a.cap]
    for name, n in sorted(over, key=lambda r: -r[1]):
        print(f"OVER {name}: {n} lines (cap {a.cap})")
    top = sorted(rows, key=lambda r: -r[1])[:3]
    print("largest: " + ", ".join(f"{name}={n}" for name, n in top))
    print(f"SKILLS_OVER_CAP={len(over)} (cap={a.cap}, scanned={len(rows)})")
    return 1 if over else 0


if __name__ == "__main__":
    sys.exit(main())

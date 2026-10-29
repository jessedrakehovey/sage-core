#!/usr/bin/env python3
"""
memory_confidence.py  -  confidence + decay over the global auto-memory store.

ECC borrow #3 (everything-claude-code, 2026-06-29). ECC's instincts carry a
confidence float that decays ~0.02/week while unconfirmed; low confidence ->
"suggested, not enforced"; it never auto-deletes. This brings the same idea to
the owner's atomic auto-memory files (~/.claude/.../memory/*.md), which already have
per-file frontmatter (the natural fit narrative memory.md lacks).

SAFETY (mirrors the memory_revalidate.py roast discipline):
  - REPORT-ONLY by default: reads everything, writes NOTHING, deletes NOTHING.
  - --apply only ANNOTATES frontmatter (adds `confidence` + `last_reviewed`);
    it NEVER deletes a memory. Pruning stays a human decision. Because these
    files are NOT git-tracked, --apply first writes a timestamped backup.
  - Durable types (user/feedback/reference) do NOT decay - only situational
    `project` memories age (respects status-claims-vs-durable-memory).

Decay:  confidence = max(FLOOR, BASE - RATE * weeks_since(last_reviewed))
  last_reviewed comes from frontmatter if present, else the file mtime (seed).

Usage:
  python scripts/memory_confidence.py            # ranked staleness report
  python scripts/memory_confidence.py --apply     # also annotate frontmatter (backs up first)
"""
from __future__ import annotations
import argparse
import re
import shutil
from datetime import datetime
from pathlib import Path

MEM_DIR = (Path.home() / ".claude" / "projects"
           / "C--Users-owner1-Documents-Wedding-Video" / "memory")

BASE = 0.90          # starting confidence for a freshly-reviewed memory
RATE = 0.02          # confidence lost per week unconfirmed (ECC's number)
FLOOR = 0.10         # never decay below this
DECAYS = {"project"}             # only situational memories age
DURABLE = {"user", "feedback", "reference"}  # these stay put
STALE_BELOW = 0.60   # report flags project memories under this as prune candidates


def parse_frontmatter(text: str) -> tuple[dict, int, int]:
    """Return (fields, fm_start_idx, fm_end_idx) for the leading --- block.

    fields includes top-level `name` and the indented metadata keys
    (type, confidence, last_reviewed). Indices are line numbers of the fence.
    """
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        return {}, -1, -1
    end = -1
    for i in range(1, len(lines)):
        if lines[i].strip() == "---":
            end = i
            break
    if end == -1:
        return {}, -1, -1
    fields: dict = {}
    for ln in lines[1:end]:
        m = re.match(r"^\s*([A-Za-z_]+):\s*(.*)$", ln)
        if m:
            key, val = m.group(1), m.group(2).strip()
            if key not in fields:        # first occurrence wins (top-level name)
                fields[key] = val
    return fields, 0, end


def weeks_since(dt: datetime) -> float:
    return max(0.0, (datetime.now() - dt).total_seconds() / (7 * 86400))


def review_date(fields: dict, path: Path) -> tuple[datetime, str]:
    raw = fields.get("last_reviewed", "")
    m = re.search(r"(\d{4}-\d{2}-\d{2})", raw)
    if m:
        try:
            return datetime.strptime(m.group(1), "%Y-%m-%d"), "frontmatter"
        except ValueError:
            pass
    return datetime.fromtimestamp(path.stat().st_mtime), "mtime(seed)"


def decayed_confidence(mtype: str, reviewed: datetime) -> float:
    if mtype not in DECAYS:
        return BASE                      # durable: no decay
    return round(max(FLOOR, BASE - RATE * weeks_since(reviewed)), 3)


def annotate(path: Path, conf: float, today: str) -> None:
    """Add/update confidence + last_reviewed inside the metadata block. Backs up first."""
    text = path.read_text(encoding="utf-8", errors="replace")
    fields, _, end = parse_frontmatter(text)
    if end == -1:
        return
    bak_dir = MEM_DIR / "_backups" / today
    bak_dir.mkdir(parents=True, exist_ok=True)
    shutil.copy2(path, bak_dir / path.name)

    lines = text.splitlines()
    # find the metadata: block and its indentation
    meta_i = next((i for i, l in enumerate(lines[:end]) if re.match(r"^metadata:\s*$", l)), -1)
    set_conf = f"  confidence: {conf}"
    set_rev = f"  last_reviewed: {today}"
    def upsert(key: str, newline: str):
        for i in range(1, end):
            if re.match(rf"^\s*{key}:", lines[i]):
                lines[i] = newline
                return
        # insert right after metadata: (or before closing fence if no metadata block)
        at = (meta_i + 1) if meta_i != -1 else end
        lines.insert(at, newline)
    upsert("confidence", set_conf)
    upsert("last_reviewed", set_rev)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def main() -> None:
    ap = argparse.ArgumentParser(description="confidence + decay over auto-memory")
    ap.add_argument("--apply", action="store_true",
                    help="annotate frontmatter (confidence + last_reviewed); backs up first")
    args = ap.parse_args()

    if not MEM_DIR.exists():
        print(f"memory dir not found: {MEM_DIR}")
        return
    today = datetime.now().strftime("%Y-%m-%d")
    rows = []
    for path in sorted(MEM_DIR.glob("*.md")):
        if path.name == "MEMORY.md":
            continue
        text = path.read_text(encoding="utf-8", errors="replace")
        fields, _, end = parse_frontmatter(text)
        if end == -1:
            rows.append((1.0, path.name, "?", "no-frontmatter", "-"))
            continue
        mtype = fields.get("type", "?")
        reviewed, src = review_date(fields, path)
        conf = decayed_confidence(mtype, reviewed)
        rows.append((conf, path.name, mtype, f"{reviewed.strftime('%Y-%m-%d')} ({src})", src))

    rows.sort(key=lambda r: r[0])  # lowest confidence (stalest) first
    print("=" * 80)
    print(f"AUTO-MEMORY CONFIDENCE / DECAY  -  {len(rows)} memories  ({'APPLY' if args.apply else 'REPORT-ONLY'})")
    print(f"base={BASE} decay={RATE}/wk floor={FLOOR}; only {sorted(DECAYS)} types decay")
    print("=" * 80)
    stale = [r for r in rows if r[2] in DECAYS and r[0] < STALE_BELOW]
    print(f"\n[prune candidates] {len(stale)} project memories below {STALE_BELOW} confidence:")
    for conf, name, mtype, when, _ in stale:
        print(f"  {conf:>5}  {name}  ({mtype}, reviewed {when})")
    if not stale:
        print("  (none)")
    print(f"\n[all, stalest first]")
    for conf, name, mtype, when, _ in rows:
        flag = "  <- review" if (mtype in DECAYS and conf < STALE_BELOW) else ""
        print(f"  {conf:>5}  {name:<48} {mtype}{flag}")

    if args.apply:
        n = 0
        for path in sorted(MEM_DIR.glob("*.md")):
            if path.name == "MEMORY.md":
                continue
            text = path.read_text(encoding="utf-8", errors="replace")
            fields, _, end = parse_frontmatter(text)
            if end == -1:
                continue
            mtype = fields.get("type", "?")
            reviewed, _ = review_date(fields, path)
            conf = decayed_confidence(mtype, reviewed)
            annotate(path, conf, today)
            n += 1
        print(f"\n[--apply] annotated {n} files (backups in {MEM_DIR / '_backups' / today}). "
              f"NOTHING deleted; pruning stays manual.")
    else:
        print(f"\nREPORT-ONLY: nothing written. Re-run with --apply to annotate frontmatter.")


if __name__ == "__main__":
    main()

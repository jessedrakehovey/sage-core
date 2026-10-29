#!/usr/bin/env python3
"""rulings.py -- has the owner ALREADY ruled on this? Look it up before you card him.

Ported from the property OS (question_bridge/rulings.py, card-load P5). He answers some
decisions the same way every time and says so ("always", "never", "remember that").
Asking again costs him a card. `assistant/data/rulings.json` holds those rulings in his
own words.

  python assistant/scripts/rulings.py find "<the question you are about to ask>" [--json]
  python assistant/scripts/rulings.py list
  python assistant/scripts/rulings.py add --id <slug> --ruling "..." --quote "<his words>" \
        --keywords "a,b c" [--lane wedding] [--date 2026-09-30]
  python assistant/scripts/rulings.py retire <id> --why "..."

A match means: do not ask the open question. If no tap is needed, act within the ruling
and `cards.py note` it. If a tap is still required (any send, any HoneyBook write), post
the card PRE-DECIDED: his ruling as the recommended first option, quoted. A ruling NEVER
authorizes a send or a write on its own. `add` is for a ruling he typed himself; never
invent one, and never add anything touching the send-gate, pricing/dates commitments or
footage handling (protected core: propose it in Memory/skill-suggestions.md).

Exit: 0 = match(es), 4 = no ruling matches, 1 = usage error. Stdlib only.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
FILE = Path(os.environ.get("RULINGS_FILE") or (HERE.parent / "data" / "rulings.json"))

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass


def load() -> dict:
    try:
        return json.loads(FILE.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        return {"rulings": []}


def save(d: dict) -> None:
    FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp = FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(d, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(tmp, FILE)


def find(text: str, d: dict | None = None, lane: str = "") -> list:
    """Active rulings with at least one keyword present as a whole phrase, best first.
    One generic word is not a match: need a specific phrase (multi-word / long) or two hits."""
    d = d or load()
    low = " " + re.sub(r"\s+", " ", str(text or "").lower()) + " "
    out = []
    for r in d.get("rulings", []):
        if r.get("status") != "active":
            continue
        if lane and r.get("lane") and r["lane"] != lane:
            continue
        hits = [k for k in r.get("keywords", [])
                if re.search(r"(?<![a-z0-9])" + re.escape(k.lower()) + r"(?![a-z0-9])", low)]
        strong = [k for k in hits if " " in k or "-" in k or len(k) >= 9]
        if strong or len(hits) >= 2:
            out.append({"id": r["id"], "ruling": r["ruling"], "quote": r.get("quote", ""),
                        "date": r.get("date", ""), "lane": r.get("lane", ""),
                        "matched": hits, "score": len(hits)})
    out.sort(key=lambda x: (-x["score"], x["id"]))
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description="Look up owner1's standing rulings before carding him.")
    sub = ap.add_subparsers(dest="cmd", required=True)
    f = sub.add_parser("find")
    f.add_argument("text", nargs="?", default="")
    f.add_argument("--file", default="")
    f.add_argument("--lane", default="")
    f.add_argument("--json", action="store_true")
    sub.add_parser("list")
    a = sub.add_parser("add")
    a.add_argument("--id", required=True)
    a.add_argument("--ruling", required=True)
    a.add_argument("--quote", required=True, help="his own words, verbatim")
    a.add_argument("--keywords", required=True, help="comma-separated phrases")
    a.add_argument("--lane", default="")
    a.add_argument("--date", default=time.strftime("%Y-%m-%d"))
    r = sub.add_parser("retire")
    r.add_argument("id")
    r.add_argument("--why", required=True)
    args = ap.parse_args()

    if args.cmd == "find":
        text = Path(args.file).read_text(encoding="utf-8-sig") if args.file else args.text
        if not text.strip():
            print("nothing to look up", file=sys.stderr)
            return 1
        hits = find(text, lane=args.lane)
        if args.json:
            print(json.dumps({"matches": hits}, ensure_ascii=False, indent=1))
        else:
            for h in hits:
                print("RULING %s (%s) matched %s\n  %s\n  he said: \"%s\"" % (
                    h["id"], h["date"], ", ".join(h["matched"]), h["ruling"], h["quote"]))
            if not hits:
                print("NO_RULING")
        return 0 if hits else 4
    d = load()
    if args.cmd == "list":
        for x in d.get("rulings", []):
            print("%-8s %-36s %-8s %s" % (x.get("status"), x["id"], x.get("lane", ""), x["ruling"][:80]))
        return 0
    if args.cmd == "add":
        if not re.fullmatch(r"[a-z0-9][a-z0-9-]{2,60}", args.id):
            print("id must be a kebab-case slug", file=sys.stderr)
            return 1
        if any(x["id"] == args.id for x in d.get("rulings", [])):
            print("id already exists: retire it first or pick another", file=sys.stderr)
            return 1
        kws = [k.strip() for k in args.keywords.split(",") if k.strip()]
        if not kws:
            print("need at least one keyword", file=sys.stderr)
            return 1
        d.setdefault("rulings", []).append({"id": args.id, "status": "active", "date": args.date,
                                            "lane": args.lane, "keywords": kws,
                                            "ruling": args.ruling, "quote": args.quote})
        save(d)
        print("ADDED " + args.id)
        return 0
    if args.cmd == "retire":
        for x in d.get("rulings", []):
            if x["id"] == args.id:
                x["status"] = "retired"
                x["retired"] = {"date": time.strftime("%Y-%m-%d"), "why": args.why}
                save(d)
                print("RETIRED " + args.id)
                return 0
        print("no such ruling", file=sys.stderr)
        return 1
    return 1


if __name__ == "__main__":
    sys.exit(main())

#!/usr/bin/env python3
"""rulings.py -- has the owner ALREADY ruled on this? Look it up before you card him.

Ported from the property OS (question_bridge/rulings.py, card-load P5). He answers some
decisions the same way every time and says so ("always", "never", "remember that").
Asking again costs him a card. `assistant/data/rulings.json` holds those rulings in his
own words.

  python assistant/scripts/rulings.py find "<the question you are about to ask>" [--json]
  python assistant/scripts/rulings.py list
  python assistant/scripts/rulings.py add --id <slug> --ruling "..." --quote "<his words>"
        --keywords "a,b c" [--lane wedding] [--date 2026-09-30]
  python assistant/scripts/rulings.py retire <id> --why "..."

A match means: do not ask the open question. If no tap is needed, act within the ruling
and `cards.py note` it. If a tap is still required (any send, any the CRM write), post
the card PRE-DECIDED: his ruling as the recommended first option, quoted. A ruling NEVER
authorizes a send or a write on its own. `add` is for a ruling he typed himself; never
invent one, and never add anything touching the send-gate, pricing/dates commitments or
footage handling (protected core: propose it in Memory/skill-suggestions.md).

What counts as a match (decision study 2026-10-30, ported from the property OS the same
day): a keyword that can match on its own is a SPECIFIC multi-word / hyphenated phrase or
a record number; a lone word never matches alone, however long ("electrical" is not a
ruling about electrical closes), and the GENERIC words that sit on nearly every card
("email", "reply", "approve", "invoice"...) never create a match even as a pair -- they
only add to a matched ruling's score. `add` refuses a keyword set that can never match;
`list` flags the rulings whose keywords were cut before that rule (weak_rulings()).

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


# Words that sit on nearly every card whatever its topic. They still count toward a matched
# ruling's score (ranking), but they can never CREATE a match, alone or as a pair: "work
# order" + "photos" is every work-order card, "email" + "text" is every reply card. (Decision
# study 2026-10-30 in the property OS: 220 ruling hits on 100 sampled cards, 4 relevant, nearly
# all from these words and from any 9+ letter word counting as a phrase -- "electrical",
# "completion", "scheduled". After the rule: 31 hits on 14 cards, all on topic.) Organization-
# free: the card vocabulary every Sage shares, not one business's nouns.
GENERIC = {
    "work order", "work orders", "wo", "close", "closed", "completion", "complete",
    "note", "notes", "photo", "photos", "picture", "pictures",
    "email", "emails", "text", "texts", "reply", "thread", "send", "sent", "post", "draft",
    "approve", "approval", "approved", "tenant", "tenants", "resident", "client", "clients",
    "customer", "customers", "vendor", "vendors", "owner", "card", "question",
    "assign", "assigned", "route", "scheduled", "schedule", "add", "again", "already", "skip",
    "evidence", "upload", "attachment", "attachments", "in-house", "duplicate", "cancel", "late",
    "balance", "bill", "bills", "invoice", "auto", "ack", "charges", "deposit", "refund",
    "follow up", "follow-up", "followup",
}


def is_specific(k: str) -> bool:
    return k.strip().lower() not in GENERIC


def is_strong(k: str) -> bool:
    """A keyword that can match on its own: a specific multi-word / hyphenated phrase, or a
    record number ("191660", "181751-1"). A lone word never is, however long -- "electrical"
    is not a ruling about electrical closes -- and neither is a unit code ("B43" sits in every
    list of units), so those need a second specific keyword beside them."""
    k = k.strip()
    if not is_specific(k):
        return False
    if " " in k or "-" in k or "_" in k:
        return True
    return any(c.isdigit() for c in k) and len(k) >= 5


def can_match(keywords: list) -> bool:
    """Whether a keyword set can ever match: one strong keyword, or two distinct specific ones."""
    kws = [str(k) for k in (keywords or [])]
    return any(is_strong(k) for k in kws) or len({k.strip().lower() for k in kws if is_specific(k)}) >= 2


def find(text: str, d: dict | None = None, lane: str = "") -> list:
    """Active rulings whose keywords pin the text down, best first.

    A match needs one strong keyword (see is_strong) or two distinct specific ones; GENERIC
    words only add to the score once a ruling has matched. A ruling with a lane matches only
    when the caller's lane is empty or the same."""
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
        if can_match(hits):
            out.append({"id": r["id"], "ruling": r["ruling"], "quote": r.get("quote", ""),
                        "date": r.get("date", ""), "lane": r.get("lane", ""),
                        "matched": hits, "score": len(hits)})
    out.sort(key=lambda x: (-x["score"], x["id"]))
    return out


def weak_rulings(d: dict | None = None) -> list:
    """Active rulings that can never match: no strong keyword and fewer than two specific ones.
    `add` refuses these; `list` flags them. Re-cut their keywords (never the ruling's words)."""
    d = d or load()
    return [r["id"] for r in d.get("rulings", [])
            if r.get("status") == "active" and not can_match(r.get("keywords", []))]


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
        weak = set(weak_rulings(d))
        for x in d.get("rulings", []):
            print("%-8s %-36s %-8s %s%s" % (x.get("status"), x["id"], x.get("lane", ""), x["ruling"][:80],
                                             "   [WEAK KEYWORDS: can never match]" if x["id"] in weak else ""))
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
        if not can_match(kws):
            print("keywords can never match: give a specific multi-word phrase (not just %s...) "
                  "or two specific words" % ", ".join(sorted(GENERIC)[:6]), file=sys.stderr)
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

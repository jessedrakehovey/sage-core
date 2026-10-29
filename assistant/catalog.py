"""catalog.py - the connector catalog: which tool, which access tier, chosen by code (sage-core, 2026-10-27).

Discovery (onboarding.py) asks the owner what they use for each business function; THIS picks how the agent
reaches it, best tier first, from assistant/catalog/<tool>.json (schema in _schema.json):
  1 an official MCP connector or OAuth API    2 an official CLI    3 export / forwarding / webhook / device
  4 Playwright on the vendor's site (last resort: rots, may breach the vendor's terms)
Every entry says what the agent can read, what it can write, and what ALWAYS waits for an approval card
(any outbound message, any money, any deletion). An organization that wires a tool the catalog lacks writes
the entry back here: that is how the second organization makes the third one cheaper.

  python assistant/catalog.py list [--function crm]
  python assistant/catalog.py match "<what the owner typed>"
  python assistant/catalog.py plan <tool-id>
"""
from __future__ import annotations

import argparse
import difflib
import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
DIR = HERE / "catalog"
TIER_NAMES = {1: "official connector / API", 2: "official CLI", 3: "export, forwarding, webhook or device", 4: "browser automation (last resort)"}


def load() -> dict:
    out = {}
    for f in sorted(DIR.glob("*.json")):
        if f.name.startswith("_"):
            continue
        try:
            d = json.loads(f.read_text(encoding="utf-8-sig"))
        except (OSError, ValueError):
            continue
        if isinstance(d, dict) and d.get("id"):
            out[d["id"]] = d
    return out


def _norm(s: str) -> str:
    return "".join(ch for ch in str(s or "").lower() if ch.isalnum())


def match(text: str, cat: dict | None = None) -> dict | None:
    """The catalog entry the owner's words name: exact id/name, then a contained word, then the closest spelling."""
    cat = cat or load()
    q = _norm(text)
    if not q:
        return None
    for e in cat.values():
        if q in (_norm(e["id"]), _norm(e.get("name"))) or q in [_norm(a) for a in e.get("aliases", [])]:
            return e
    for e in cat.values():
        if _norm(e.get("name")) and (_norm(e.get("name")) in q or q in _norm(e.get("name"))):
            return e
    names = {_norm(e.get("name")): e for e in cat.values()}
    names.update({_norm(e["id"]): e for e in cat.values()})
    close = difflib.get_close_matches(q, list(names), n=1, cutoff=0.75)
    return names[close[0]] if close else None


def for_function(fn: str, cat: dict | None = None) -> list:
    cat = cat or load()
    return [e for e in cat.values() if fn in (e.get("functions") or [])]


def best_tier(entry: dict) -> dict | None:
    tiers = sorted([t for t in (entry.get("tiers") or []) if isinstance(t, dict)], key=lambda t: (int(t.get("tier", 9)), t.get("status") != "built"))
    return tiers[0] if tiers else None


def plan_text(entry: dict) -> str:
    lines = [f"{entry.get('name')} ({entry['id']}): {', '.join(entry.get('functions') or [])}"]
    for t in sorted(entry.get("tiers") or [], key=lambda t: int(t.get("tier", 9))):
        lines.append(f"  tier {t.get('tier')} {TIER_NAMES.get(int(t.get('tier', 9)), '')} [{t.get('status', '')}] via {t.get('path', '')}")
        lines.append(f"    how: {t.get('how', '')}")
        if t.get("reads"):
            lines.append(f"    reads: {', '.join(t['reads'])}")
        if t.get("writes"):
            lines.append(f"    writes: {', '.join(t['writes'])}")
        if t.get("always_tap"):
            lines.append(f"    always a card: {', '.join(t['always_tap'])}")
    for g in entry.get("gotchas") or []:
        lines.append(f"  gotcha: {g}")
    if entry.get("notes"):
        lines.append(f"  note: {entry['notes']}")
    return "\n".join(lines)


def main() -> int:
    sys.stdout.reconfigure(encoding="utf-8")
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("list"); p.add_argument("--function", default="")
    p = sub.add_parser("match"); p.add_argument("text")
    p = sub.add_parser("plan"); p.add_argument("tool")
    a = ap.parse_args()
    cat = load()
    if a.cmd == "list":
        for e in (for_function(a.function, cat) if a.function else cat.values()):
            bt = best_tier(e) or {}
            print(f"{e['id']:16} tier {bt.get('tier', '-')} [{bt.get('status', '')}]  {', '.join(e.get('functions') or [])}")
    elif a.cmd == "match":
        e = match(a.text, cat)
        print(plan_text(e) if e else f"no catalog entry for '{a.text}': the agent will ask how you reach it and write a new entry")
    elif a.cmd == "plan":
        e = cat.get(a.tool)
        print(plan_text(e) if e else f"no catalog entry {a.tool}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

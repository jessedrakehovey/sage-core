"""advice.py - the one-line recommendation on a card row (ruling 8, 2026-10-15).

example's advice.py, generalized: what to tap and why, then what else is waiting on it, how long
it has waited and what it costs. Deterministic, no model. An `item` is any dict with kind /
lane / title / age_days and optionally amount, waiting_on, blocks, fix.

  python assistant/scripts/tracking/advice.py     # the line for every current chase item
"""
from __future__ import annotations

import json
import math
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import common as C  # noqa: E402

MAX = 220

_TAP = {
    "card": "Tap the open card itself (this is a nudge, not a second copy)",
    "ticket": "Say what to do with it, or 'Drop it'",
    "followup": "Tap Handled if it's done, or let it go another day",
    "drift": "Approve the record fix; nothing goes to anyone",
}


def _days(n: float) -> str:
    n = int(round(n))
    return "today" if n <= 0 else ("1 day" if n == 1 else f"{n} days")


def _money(item: dict) -> str:
    amt = item.get("amount")
    try:
        v = float(str(amt).replace("$", "").replace(",", "")) if amt not in (None, "") else 0.0
    except ValueError:
        v = 0.0
    if v <= 0:
        return ""
    return f"${v:,.0f} rides on it"


def line(item: dict) -> str:
    kind = str(item.get("kind") or "")
    parts = []
    if item.get("fix"):
        parts.append(_TAP["drift"])
    elif kind in _TAP:
        parts.append(_TAP[kind])
    why = str(item.get("why") or "").strip()
    if why:
        parts.append(why)
    if item.get("waiting_on"):
        parts.append(f"{item['waiting_on']} is waiting")
    age = float(item.get("age_days") or 0)
    if age >= 1:
        parts.append(f"waited {_days(age)}")
    m = _money(item)
    if m:
        parts.append(m)
    if item.get("blocks"):
        parts.append("holds up " + str(item["blocks"]))
    s = "💡 " + "; ".join(p.rstrip(".") for p in parts if p) + "."
    return s if len(s) <= MAX else s[:MAX - 1].rstrip() + "…"


def score(item: dict, c: dict | None = None) -> float:
    """Importance for ranking inside a lane. An item that carries the priority brain's inputs
    (deadline / days_overdue / waiting_kind / hard) is scored by priority.py (ruling 10);
    anything else keeps the simple form below."""
    try:
        import priority
        if priority.applies(item):
            return priority.score(item, None, c)
    except Exception:  # noqa: BLE001 - the brain failing must never stop a chase
        pass
    imp = (c or C.cfg())["importance"]
    lane_w = float((imp.get("lane") or {}).get(str(item.get("lane") or ""), 1.0))
    kind_w = float((imp.get("kind") or {}).get(str(item.get("kind") or ""), 1.0))
    age = float(item.get("age_days") or 0)
    s = lane_w * kind_w * (1.0 + math.log1p(age))
    try:
        amt = float(str(item.get("amount") or "0").replace("$", "").replace(",", ""))
        if amt > 0:
            s += math.log10(amt + 1)
    except ValueError:
        pass
    if item.get("waiting_on"):
        s *= 1.5
    return round(s, 3)


if __name__ == "__main__":
    import chase
    for it in chase.candidates():
        print(f"{it['lane']:9} {it['kind']:9} {it['title'][:50]:50} {line(it)}")

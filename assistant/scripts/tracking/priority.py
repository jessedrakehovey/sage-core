"""priority.py - the priority brain and the Eisenhower lens (ruling 10, 2026-10-15; the shape is
the property OS's priority.py + eisenhower.py, with the owner's own definition of importance).

  importance = lane weight x (1 + dollars at stake + who is waiting + hard date)
      dollars   log10($ + 1) x dollar_weight       ($8,000 lead ~ 3.9)
      waiting   client > vendor > admin > nobody   (tracking.json -> priority.waiting)
      hard      a wedding day, a tax deadline, a legal date: + hard_bonus
  urgency    from the deadline: 0.5 far out, 1.0 at the deadline, up to 3.0 deeply overdue
             (no deadline: from age against an SLA)
  score      importance x urgency x staleness(age)   -- what advice.score returns for any item that
             carries a deadline / days_overdue / waiting_kind / importance (else its simple form)
  quadrant   DO (important + urgent) / SCHEDULE (important, not urgent) / DELEGATE / DELETE.
             SCHEDULE is the rescue lane: weekly.py adds one nudge line per Q2 item to the Friday
             readback and never cards it.

Pure functions over dicts; the only I/O is reading tracking.json and, for the Q2 lines, the
entity ledger entities.py writes.

  python assistant/scripts/tracking/priority.py          # the ranked entity stream
  python assistant/scripts/tracking/priority.py --q2     # the Friday nudge lines
"""
from __future__ import annotations

import argparse
import json
import math
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import common as C  # noqa: E402

DEFAULTS = {
    "waiting": {"client": 3.0, "vendor": 2.0, "admin": 1.0, "household": 1.5, "none": 0.0},
    "dollar_weight": 1.0,
    "hard_bonus": 3.0,
    "important_at": 4.0,
    "urgent_at": 1.0,
    "default_sla_days": 7,
    "q2_max_lines": 4,
    "q2_horizon_days": 45,
}
Q1, Q2, Q3, Q4 = "DO", "SCHEDULE", "DELEGATE", "DELETE"
LABEL = {Q1: "do now", Q2: "schedule it", Q3: "delegate or batch", Q4: "backlog"}


def pcfg(c: dict | None = None) -> dict:
    raw = (c or C.cfg()).get("priority") if c else (C.load_json(C.TRACKING_CFG, {}) or {}).get("priority")
    out = dict(DEFAULTS)
    if isinstance(raw, dict):
        out.update({k: v for k, v in raw.items() if not k.startswith("_")})
        out["waiting"] = dict(DEFAULTS["waiting"], **(raw.get("waiting") or {}))
    return out


def dollars(item: dict) -> float:
    for k in ("amount", "value", "dollars"):
        v = item.get(k)
        if v in (None, ""):
            continue
        try:
            f = float(str(v).replace("$", "").replace(",", ""))
        except ValueError:
            continue
        if f > 0:
            return f
    return 0.0


def importance(item: dict, p: dict | None = None, lane_w: dict | None = None) -> float:
    p = p or pcfg()
    lw = float((lane_w or {}).get(str(item.get("lane") or ""), 1.0))
    d = dollars(item)
    dol = math.log10(d + 1) * float(p["dollar_weight"]) if d > 0 else 0.0
    wk = str(item.get("waiting_kind") or ("client" if str(item.get("waiting_on") or "").lower().find("client") >= 0 else "none"))
    wait = float(p["waiting"].get(wk, p["waiting"].get("none", 0.0)))
    hard = float(p["hard_bonus"]) if item.get("hard") else 0.0
    return round(lw * (1.0 + dol + wait + hard), 3)


def urgency(item: dict, now=None, p: dict | None = None) -> float:
    p = p or pcfg()
    now = now or C.now_local()
    sla = float(item.get("sla_days") or p["default_sla_days"]) or 1.0
    od = item.get("days_overdue")
    if od is None and item.get("deadline"):
        dl = C.parse_any(item["deadline"])
        if dl:
            od = (now.date() - dl.date()).days
    if od is not None:
        od = float(od)
        if od >= 0:
            return round(min(3.0, 1.0 + od / sla), 2)
        return round(max(0.5, 1.0 + od / sla * 0.5), 2)
    age = item.get("age_days")
    if age is None:
        return 0.6
    return round(max(0.5, min(3.0, 0.3 + float(age) / sla)), 2)


def staleness(age_days) -> float:
    a = float(age_days or 0)
    return round(1.0 + min(1.0, math.log10(1 + max(a, 0)) / 2.5), 2)


def quadrant(imp: float, urg: float, hard: bool = False, p: dict | None = None) -> str:
    p = p or pcfg()
    important = bool(hard) or imp >= float(p["important_at"])
    urgent = urg >= float(p["urgent_at"])
    if important and urgent:
        return Q1
    if important:
        return Q2
    return Q3 if urgent else Q4


def applies(item: dict) -> bool:
    """advice.score hands over to this brain when the item carries any of its inputs."""
    return any(k in item for k in ("deadline", "days_overdue", "waiting_kind", "importance", "hard"))


def assess(item: dict, now=None, c: dict | None = None) -> dict:
    """importance / urgency / staleness / score / quadrant for one item (added in place and returned)."""
    cc = c or C.cfg()
    p = pcfg(cc)
    lane_w = (cc.get("importance") or {}).get("lane") or {}
    imp = float(item["importance"]) if isinstance(item.get("importance"), (int, float)) else importance(item, p, lane_w)
    urg = urgency(item, now, p)
    stale = staleness(item.get("age_days"))
    item.update({"importance": round(imp, 3), "urgency": urg, "staleness": stale,
                 "score": round(imp * urg * stale, 3), "quadrant": quadrant(imp, urg, bool(item.get("hard")), p)})
    return item


def score(item: dict, now=None, c: dict | None = None) -> float:
    return float(assess(dict(item), now, c)["score"])


def rank(items: list, now=None, c: dict | None = None) -> list:
    rows = [assess(dict(i), now, c) for i in items]
    return sorted(rows, key=lambda r: (0 if r.get("hard") and r["quadrant"] == Q1 else 1, -r["score"]))


# ------------------------------------------------------------------ the Q2 rescue lane
def q2_items(rows: list | None = None, now=None, c: dict | None = None) -> list:
    p = pcfg(c)
    now = now or C.now_local()
    if rows is None:
        rows = list((C.state("entities", {}) or {}).get("rows") or [])
    out = []
    for r in rows:
        if r.get("quadrant") != Q2 or not r.get("next_action"):
            continue
        dl = C.parse_any(r.get("deadline"))
        if dl and (dl.date() - now.date()).days > int(p["q2_horizon_days"]):
            continue
        out.append(r)
    return sorted(out, key=lambda r: (str(r.get("deadline") or "9999"), -float(r.get("importance") or 0)))


def q2_lines(rows: list | None = None, now=None, c: dict | None = None) -> list:
    """One plain line per important-not-urgent item, capped, for the Friday readback."""
    p = pcfg(c)
    now = now or C.now_local()
    lines = []
    for r in q2_items(rows, now, c)[:int(p["q2_max_lines"])]:
        when = ""
        dl = C.parse_any(r.get("deadline"))
        if dl:
            left = (dl.date() - now.date()).days
            when = f" (by {C.shortdate(dl)}, {left} days)" if left > 0 else f" (by {C.shortdate(dl)})"
        lines.append(f"{r.get('title')}: {r.get('next_action')}{when}.")
    return lines


def render(rows: list, top: int = 20) -> str:
    out = [f"PRIORITY - {len(rows)} open loops, importance x urgency x staleness"]
    for i, r in enumerate(rows[:top], 1):
        od = r.get("days_overdue")
        tag = f" overdue {int(od)} d" if isinstance(od, (int, float)) and od > 0 else ""
        out.append(f"{i:>2}. [{r.get('quadrant')}] {str(r.get('title') or '')[:40]:40} {r.get('score'):7.2f}  "
                   f"imp {r.get('importance')} urg {r.get('urgency')}{tag}  -> {str(r.get('next_action') or '')[:60]}")
    return "\n".join(out)


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--q2", action="store_true")
    ap.add_argument("--top", type=int, default=20)
    a = ap.parse_args()
    rows = list((C.state("entities", {}) or {}).get("rows") or [])
    if a.q2:
        for ln in q2_lines(rows):
            print(ln)
    else:
        print(render(rank(rows), a.top))

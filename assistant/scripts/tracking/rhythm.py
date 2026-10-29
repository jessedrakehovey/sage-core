"""rhythm.py - the owner's working rhythm, measured, never set by hand (ruling 17, 2026-10-15).

Ported in shape from example's rhythm.py (property OS): WHEN the owner answers (card answers and
chat lines, last `lookback_days`), and WHICH chase kinds he never answers. Output: a small
profile the chaser reads (cached in data/tracking/rhythm.json, rebuilt hourly).

  python assistant/scripts/tracking/rhythm.py      # print the profile
"""
from __future__ import annotations

import json
import sys
import time
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import common as C  # noqa: E402

CACHE_S = 3600


def _hours_from(answers: list, chat: list, min_share: float, quiet) -> list:
    cnt = Counter()
    for r in answers:
        d = C.parse_any(r.get("ts") or r.get("epoch"))
        if d:
            cnt[d.hour] += 1
    for r in chat:
        if str(r.get("who") or "").lower() in ("sage", "agent", ""):
            continue
        d = C.parse_any(r.get("ts"))
        if d:
            cnt[d.hour] += 1
    total = sum(cnt.values())
    if total < 10:                                   # too little evidence: the configured window
        return []
    hours = sorted(h for h, n in cnt.items() if n / total >= min_share)
    return [h for h in hours if not C.in_quiet(C.now_local().replace(hour=h, minute=0), quiet)]


def _ignored_kinds(chase_state: dict, ignore_after: int, lookback_days: int) -> list:
    """A kind the owner never answered in the window, over ignore_after asks, goes weekly."""
    asked, answered = Counter(), Counter()
    cut = time.time() - lookback_days * 86400
    for it in (chase_state.get("items") or {}).values():
        if float(it.get("last_sent_epoch") or 0) < cut:
            continue
        k = str(it.get("kind") or "")
        asked[k] += int(it.get("sends") or 0)
        if it.get("status") == "answered":
            answered[k] += 1
    return sorted(k for k, n in asked.items() if n >= ignore_after and answered[k] == 0 and k)


def build(force: bool = False) -> dict:
    cached = C.state("rhythm", {})
    if cached and not force and time.time() - float(cached.get("built_epoch") or 0) < CACHE_S:
        return cached
    r = C.cfg()["rhythm"]
    answers = C.card_answers(r["lookback_days"])
    chat = C.transcript(r["lookback_days"])
    prof = {
        "built_epoch": time.time(),
        "built": C.now_local().strftime("%Y-%m-%d %H:%M"),
        "samples": len(answers) + len([x for x in chat if str(x.get("who") or "").lower() not in ("sage", "agent", "")]),
        "active_hours": _hours_from(answers, chat, float(r["min_share"]), r["quiet_hours"]),
        "ignored_kinds": _ignored_kinds(C.state("chase", {}), int(r["ignore_after"]), int(r["lookback_days"])),
    }
    C.save_state("rhythm", prof)
    return prof


def good_time(now=None, prof: dict | None = None) -> bool:
    """Post a chase now? In the owner's measured active hours when we have them, else the
    configured post window. Never in quiet hours."""
    now = now or C.now_local()
    c = C.cfg()
    if C.in_quiet(now, c["rhythm"]["quiet_hours"]):
        return False
    prof = prof or build()
    hours = prof.get("active_hours") or []
    if hours:
        return now.hour in hours
    return C.in_hours(now, c["chase"]["post_hours"])


def advice(prof: dict | None = None) -> str:
    prof = prof or build()
    if not prof.get("active_hours"):
        return "Not enough answers yet to know his hours; using the configured window."
    hs = prof["active_hours"]
    s = f"Answers mostly {hs[0]:02d}:00-{hs[-1] + 1:02d}:00."
    if prof.get("ignored_kinds"):
        s += " Never answers: " + ", ".join(prof["ignored_kinds"]) + " (moved to the weekly card)."
    return s


if __name__ == "__main__":
    p = build(force=True)
    print(json.dumps(p, indent=1))
    print(advice(p))

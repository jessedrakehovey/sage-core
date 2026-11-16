"""thread_report.py - how many Google Chat threads the agent opens, from its thread_keys registry.

the owner 2026-11-04 ("hundreds of chat threads"): thread_keys gives every subject one thread. This prints, from
DATA/thread_keys.json, the number of NEW threads opened per space per day for the last 14 days and the subjects
with the most posts, so the count can be compared week over week. Ported from the sister agent family the same day.

    python assistant/agent/thread_report.py
    python assistant/agent/thread_report.py --days 14 --top 15
    python assistant/agent/thread_report.py --json

Read-only: one JSON file in, text out. No network, nothing posted.

What the numbers are:
  * "opened" = a thread the agent started (a header post). "adopted" = a person's thread the agent followed
    into; it opened nothing.
  * days are local days, from each entry's `opened` stamp.
  * the registry keeps the LATEST thread per subject. When a subject's thread ages out and a new one opens
    (gen 2, 3 ...), the earlier one is no longer listed, so a past day can read lower than it was; the count
    of such subjects is printed.
  * an entry whose header or thread belongs to ANOTHER space than the one it is filed under cannot be a real
    post (a test wrote it into the live registry); those are left out of every count and listed in one line.
"""
from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import core  # noqa: E402
import thread_keys as tk  # noqa: E402


def space_labels() -> dict:
    """{space id: 'home' | 'dm <person>' | 'team'} from the agent's own space records."""
    out = {}
    for who, sp in (core.load(core.DATA / "gchat_spaces.json", {}) or {}).items():
        if isinstance(sp, str) and sp:
            out[sp] = f"dm {who}"
    home = core.home_space()
    if home:
        out[home] = "home"
    return out


def _local_day(stamp: str) -> str:
    d = tk._parse(stamp)
    return d.astimezone().strftime("%Y-%m-%d") if d else ""


def _foreign(space: str, e: dict) -> bool:
    """The entry's header or thread is not in the space it is filed under."""
    return any(v and not str(v).startswith(space + "/") for v in (e.get("header"), e.get("thread")))


def report(registry: dict, days: int = 14, top: int = 15, today: datetime | None = None) -> dict:
    """{"days": [YYYY-MM-DD ...], "spaces": {space: {"opened": {day: n}, "adopted": {day: n},
    "total_opened", "total_adopted", "regenerated"}}, "top": [{space, subject, title, posts, ...}],
    "foreign": ["space subject", ...]}"""
    today = today or datetime.now()
    day_list = [(today - timedelta(days=i)).strftime("%Y-%m-%d") for i in range(days - 1, -1, -1)]
    window = set(day_list)
    spaces, rows, foreign = {}, [], []
    for space, ents in (registry or {}).items():
        if not isinstance(ents, dict):
            continue
        opened, adopted, regen, real = Counter(), Counter(), 0, 0
        for base, e in ents.items():
            if not isinstance(e, dict):
                continue
            if _foreign(space, e):
                foreign.append(f"{space} {base}")
                continue
            real += 1
            day = _local_day(e.get("opened") or "")
            if day in window:
                (adopted if e.get("adopted") else opened)[day] += 1
                if int(e.get("gen") or 1) > 1:
                    regen += 1
            rows.append({"space": space, "subject": base, "title": str(e.get("title") or ""),
                         "posts": int(e.get("n") or 0), "opened": day, "last": _local_day(e.get("last") or ""),
                         "adopted": bool(e.get("adopted")), "mark": str(e.get("mark") or "")})
        if real:
            spaces[space] = {"opened": dict(opened), "adopted": dict(adopted), "total_opened": sum(opened.values()),
                             "total_adopted": sum(adopted.values()), "regenerated": regen}
    rows.sort(key=lambda r: (-r["posts"], r["subject"]))
    return {"days": day_list, "spaces": spaces, "top": rows[:top], "foreign": foreign}


def render(rep: dict, labels: dict | None = None, name: str = "") -> str:
    labels = labels or {}
    days = rep["days"]
    out = [f"Chat threads opened by {name or 'the agent'}, {days[0]} to {days[-1]} ({len(days)} days)"]
    left_out = (f"left out, not real posts ({len(rep['foreign'])} test leftover(s) in the registry): "
                + ", ".join(rep["foreign"])) if rep.get("foreign") else ""
    if not rep["spaces"]:
        out.append("  no registry entries yet (nothing keyed, or chat_thread_keys is off)")
        return "\n".join(out + ([left_out] if left_out else []))
    for space, s in sorted(rep["spaces"].items()):
        label = f" ({labels[space]})" if space in labels else ""
        out.append("")
        out.append(f"{space}{label}: {s['total_opened']} opened, {s['total_adopted']} adopted from a person")
        out.append(f"  {'day':<16}{'opened':>7}{'adopted':>9}")
        for d in days:
            o, a = s["opened"].get(d, 0), s["adopted"].get(d, 0)
            if o or a:
                wd = datetime.strptime(d, "%Y-%m-%d").strftime("%a")
                out.append(f"  {d} {wd:<5}{o:>7}{a:>9}")
        if not (s["total_opened"] or s["total_adopted"]):
            out.append("  (none in this window)")
        if s["regenerated"]:
            out.append(f"  note: {s['regenerated']} subject(s) are on a 2nd+ thread; their earlier thread is not counted")
    out.append("")
    out.append(f"Top subjects by posts (all time in the registry, top {len(rep['top'])}):")
    out.append(f"  {'posts':>5}  {'subject':<26}{'last':<12}title")
    for r in rep["top"]:
        tag = ("[adopted] " if r["adopted"] else "") + (f"{r['mark']} " if r["mark"] else "")
        out.append(f"  {r['posts']:>5}  {r['subject']:<26}{r['last']:<12}{tag}{r['title'][:70]}")
    if left_out:
        out += ["", left_out]
    return "\n".join(out)


def main(argv=None) -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except (AttributeError, ValueError):
        pass
    ap = argparse.ArgumentParser(description="New Chat threads per space per day + top subjects, from thread_keys.json.")
    ap.add_argument("--days", type=int, default=14)
    ap.add_argument("--top", type=int, default=15)
    ap.add_argument("--json", action="store_true", help="the report as JSON")
    a = ap.parse_args(argv)
    rep = report(tk.Registry().dump(), max(1, a.days), max(1, a.top))
    name = str(core.profile().get("name") or "")
    print(json.dumps(rep, indent=1, ensure_ascii=False) if a.json else render(rep, space_labels(), name))
    return 0


if __name__ == "__main__":
    sys.exit(main())

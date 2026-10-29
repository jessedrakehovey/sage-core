"""dayplan.py - tomorrow, lined up the evening before, only when it is busy (ruling 6, 2026-10-15;
the shape is example's day_plan.py, without the texts: nothing here asks anyone anything).

At tracking.json -> dayplan.plan_at, once per target day: count tomorrow's timed items (calendar
events with a time, calls, deadlines from the world model and the chains, follow-ups that fire,
chases due back). min_timed_items or more -> ONE plain message to the shared space laying the
day out in blocks (morning / afternoon / evening); owner's items in their own line, only on days
she has any (ruling 16). Fewer -> silence (logged, nothing posted). Zero LLM.

  python assistant/scripts/tracking/dayplan.py            # tick (what the spine runs)
  python assistant/scripts/tracking/dayplan.py --dry-run  # print tomorrow's plan, post nothing
  python assistant/scripts/tracking/dayplan.py --force    # ignore the hour gate (still once per day)
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import common as C  # noqa: E402

DEFAULTS = {
    "enabled": True,
    "plan_at": "19:00",
    "min_timed_items": 3,
    "count_all_day": False,
    "blocks": [{"label": "Morning", "until": "12:00"}, {"label": "Afternoon", "until": "17:00"}, {"label": "Evening", "until": "24:00"}],
    "call_words": ["call", "zoom", "meeting", "consult", "phone"],
    "max_chars": 900,
}


def dcfg() -> dict:
    raw = (C.load_json(C.TRACKING_CFG, {}) or {}).get("dayplan") or {}
    out = dict(DEFAULTS)
    out.update({k: v for k, v in raw.items() if not k.startswith("_")})
    return out


def _t(s) -> tuple | None:
    try:
        h, m = str(s).split(":")[:2]
        return int(h), int(m)
    except (ValueError, AttributeError):
        return None


def _fmt(t: tuple) -> str:
    h, m = t
    ap = "am" if h < 12 else "pm"
    hh = h % 12 or 12
    return f"{hh}:{m:02d}{ap}" if m else f"{hh}{ap}"


# ------------------------------------------------------------------ tomorrow's items
def items_for(day, cal=None, entities=None, gaps=None, fus=None, chase_state=None, hb_tasks=None, d: dict | None = None) -> list:
    """Rows {time: (h,m)|None, kind: event|call|deadline|chase|followup, text, owner, counts}."""
    d = d or dcfg()
    ds = day.strftime("%Y-%m-%d")
    out = []
    cal = cal if cal is not None else ((C.load_json(C.vault_path("assistant/data/calendar.json"), {}) or {}).get("events") or [])
    for e in cal:
        if str(e.get("date") or "")[:10] != ds and not (str(e.get("date") or "")[:10] <= ds <= str(e.get("end_date") or e.get("date") or "")[:10]):
            continue
        title = str(e.get("title") or "")
        t = None if e.get("all_day") else _t(e.get("time"))
        kind = "call" if any(w in title.lower() for w in d["call_words"]) else "event"
        loc = str(e.get("location") or "").strip()
        out.append({"time": t, "kind": kind, "text": title[:70] + (f" ({loc[:30]})" if loc and loc.lower() not in title.lower() else ""),
                    "owner": str(e.get("owner") or "owner1"), "counts": bool(t) or bool(d.get("count_all_day"))})
    ents = entities if entities is not None else list((C.state("entities", {}) or {}).get("rows") or [])
    for r in ents:
        if str(r.get("deadline") or "") != ds or r.get("kind") in ("appointment",):
            continue
        out.append({"time": None, "kind": "deadline", "text": f"{r.get('title')}: {r.get('next_action')}"[:90],
                    "owner": str(r.get("owner") or "owner1"), "counts": True})
    gs = gaps if gaps is not None else list((C.state("chain_gaps", {}) or {}).get("rows") or [])
    seen = {x["text"] for x in out}
    for g in gs:
        if str(g.get("due") or "") != ds or g.get("status") not in ("up_next", "ask", "later"):
            continue
        txt = f"{g.get('title')}: {g.get('label')} due"[:90]
        if txt not in seen and not any(str(g.get("title")) in x["text"] for x in out if x["kind"] == "deadline"):
            out.append({"time": None, "kind": "deadline", "text": txt, "owner": "owner1", "counts": True})
    fus = fus if fus is not None else list((C.followups_state().get("items") or {}).values())
    for f in fus:
        if str(f.get("status") or "") != "waiting" or str(f.get("fire_at") or "")[:10] != ds:
            continue
        out.append({"time": _t(str(f.get("fire_at"))[11:16]), "kind": "followup", "text": f"reminder: {str(f.get('subject') or '')[:70]}",
                    "owner": "owner6" if f.get("lane") == "owner6" else "owner1", "counts": True})
    cs = chase_state if chase_state is not None else (C.state("chase", {}) or {}).get("items") or {}
    for it in cs.values():
        if it.get("status") == "snoozed" and str(it.get("snooze_until") or "")[:10] == ds:
            out.append({"time": None, "kind": "chase", "text": f"you asked to be reminded: {str(it.get('title') or '')[:60]}",
                        "owner": str(it.get("owner") or "owner1"), "counts": True})
    tasks = hb_tasks if hb_tasks is not None else ((C.load_json(C.vault_path("dashboard/data/honeybook.json"), {}) or {}).get("tasks") or [])
    for t in tasks:
        if str(t.get("status") or "") == "pending" and str(t.get("due_date") or "") == ds:
            out.append({"time": None, "kind": "deadline", "text": f"HoneyBook task: {t.get('description')} ({str(t.get('project') or '').replace(chr(39) + 's Project', '')})"[:90],
                        "owner": "owner1", "counts": True})
    out.sort(key=lambda x: (x["time"] is None, x["time"] or (0, 0), x["kind"]))
    return out


# ------------------------------------------------------------------ the message
def _block_of(t: tuple, blocks: list) -> int:
    for i, b in enumerate(blocks):
        u = _t(b.get("until") or "24:00") or (24, 0)
        if t < u:
            return i
    return len(blocks) - 1


def message(day, items: list, d: dict | None = None) -> str:
    d = d or dcfg()
    blocks = [dict(b, items=[]) for b in d["blocks"]]
    untimed, joh = [], []
    for it in items:
        if it["owner"] == "owner6":
            joh.append(it); continue
        if it["time"] is None:
            untimed.append(it); continue
        blocks[_block_of(it["time"], blocks)]["items"].append(it)
    lines = [f"Tomorrow, {day.strftime('%A %m/%d').replace(' 0', ' ')}:"]
    for b in blocks:
        if b["items"]:
            lines.append(f"{b['label']}: " + "; ".join(f"{_fmt(x['time'])} {x['text']}" for x in b["items"]) + ".")
    if untimed:
        lines.append("Also due: " + "; ".join(x["text"] for x in untimed) + ".")
    if joh:
        lines.append("owner6: " + "; ".join((f"{_fmt(x['time'])} " if x["time"] else "") + x["text"] for x in joh) + ".")
    text = "\n".join(lines)
    cap = int(d.get("max_chars") or 900)
    return text if len(text) <= cap else text[:cap - 1].rstrip() + "…"


def _post(text: str, dry: bool) -> bool:
    if dry:
        print(text); return True
    try:
        sys.path.insert(0, str(C.ROOT / "agent"))
        import bridge  # noqa: E402
        bridge.post("owner1", text, channel="dayplan")
        return True
    except Exception as e:  # noqa: BLE001
        C.log("dayplan_post_error", error=f"{type(e).__name__}: {str(e)[:160]}")
    try:
        subprocess.run([sys.executable, str(C.SCRIPTS / "notify.py")], input=text, text=True, encoding="utf-8",
                       cwd=str(C.SCRIPTS), timeout=30, capture_output=True, env={**os.environ, "CHAT_PUSH": "on"})
        return True
    except (OSError, subprocess.TimeoutExpired):
        return False


def tick(now=None, dry: bool = False, force: bool = False, items=None, d: dict | None = None) -> dict:
    d = d or dcfg()
    now = now or C.now_local()
    day = (now + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
    ds = day.strftime("%Y-%m-%d")
    if not d.get("enabled", True):
        return {"skipped": "disabled"}
    if not force and (now.hour, now.minute) < (_t(d.get("plan_at")) or (19, 0)):
        return {"skipped": "too early", "day": ds}
    st = C.state("dayplan", {})
    if st.get("day") == ds and not dry:
        return {"skipped": "done for the day", "day": ds}
    its = items if items is not None else items_for(day, d=d)
    n = sum(1 for x in its if x.get("counts"))
    out = {"day": ds, "timed": n, "items": len(its), "posted": False}
    if n >= int(d["min_timed_items"]):
        text = message(day, its, d)
        out["posted"] = _post(text, dry)
        out["text"] = text
    else:
        out["silent"] = True
    if not dry:
        C.save_state("dayplan", {"day": ds, "ts": now.strftime("%Y-%m-%d %H:%M"), "timed": n, "posted": out["posted"]})
        C.log("dayplan", day=ds, timed=n, posted=out["posted"])
    print("DAYPLAN " + json.dumps({k: v for k, v in out.items() if k != "text"}))
    return out


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--force", action="store_true")
    a = ap.parse_args()
    tick(dry=a.dry_run, force=a.force or a.dry_run)

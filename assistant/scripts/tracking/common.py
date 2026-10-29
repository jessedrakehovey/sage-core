"""common.py - shared plumbing for the tracking layer (assistant/scripts/tracking/).

Reads assistant/tracking.json (the organization's cadences and checks) and the record sources
the detectors look at. Zero-LLM. Writes only under assistant/data/tracking/. Never sends.
See assistant/TRACKING_DESIGN.md.
"""
from __future__ import annotations

import json
import os
import re
import sys
import time
from datetime import date, datetime, timedelta
from pathlib import Path

HERE = Path(__file__).resolve().parent
SCRIPTS = HERE.parent
sys.path.insert(0, str(SCRIPTS))
from alib import DATA, ROOT, VAULT, config, load_json, save_json  # noqa: E402

try:                                   # PS 5.1 consoles are cp1252; the advice line carries an emoji
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except (AttributeError, ValueError):
    pass

TRACKING_CFG = ROOT / "tracking.json"
STATE_DIR = DATA / "tracking"
LOG = STATE_DIR / "log.jsonl"

DEFAULTS = {
    "chase": {"enabled": True, "chases_per_lane_per_day": 1, "reask_hours": 24, "max_follow_ups": 3, "park_days": 14,
              "stale_card_hours": 24, "stale_ticket_days": 3, "stale_followup_days": 2, "post_hours": ["08:00", "20:30"],
              "lane_owner": {}, "default_owner": "owner1"},
    "drift": {"ledger_stage": {"enabled": False}, "crm_stage": {"enabled": False}, "armed_reminder": {"enabled": False},
              "past_event": {"enabled": False}, "urgent_kinds": []},
    "weekly": {"enabled": True, "readback_day": "fri", "readback_after": "15:00", "hygiene_max_rows": 12, "to": "both"},
    "rhythm": {"lookback_days": 30, "min_share": 0.05, "ignore_after": 3, "quiet_hours": [21, 7]},
    "importance": {"lane": {}, "kind": {"card": 2, "ticket": 1.5, "followup": 1.2, "drift": 1}},
    "scheduling": {"enabled": True, "google": True,
                   "windows": {d: ["10:00", "17:00"] for d in ("mon", "tue", "wed", "thu", "fri")},
                   "prefer_times": ["10:00", "14:00", "16:00"], "slots": 3, "duration_min": 30, "slot_step_min": 30, "buffer_min": 30,
                   "min_lead_hours": 20, "days_ahead": 7, "proposal_ttl_days": 5, "all_day_blocks": ["wedding", "shoot"],
                   "hb_snapshot": "dashboard/data/honeybook.json", "hold_title": "Call: {name}", "ask_words": "", "templates": {}},
    "probation": {"enabled": True, "threshold": 20, "lookback_days": 180, "classes": {}},
}

DOW = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]


def cfg() -> dict:
    """tracking.json over DEFAULTS, one level deep."""
    raw = load_json(TRACKING_CFG, {}) or {}
    out = {}
    for k, dv in DEFAULTS.items():
        v = dict(dv)
        if isinstance(raw.get(k), dict):
            v.update({kk: vv for kk, vv in raw[k].items() if not kk.startswith("_")})
        out[k] = v
    return out


def lanes() -> list:
    try:
        return list(config().get("lanes") or ["personal"])
    except SystemExit:
        return ["personal"]


def owner_for(lane: str, c: dict | None = None) -> str:
    ch = (c or cfg())["chase"]
    return str((ch.get("lane_owner") or {}).get(lane) or ch.get("default_owner") or "owner1")


def now_local() -> datetime:
    return datetime.now()


def hm(s: str) -> tuple:
    h, m = str(s).split(":")
    return int(h), int(m)


def in_hours(now: datetime, span) -> bool:
    (h1, m1), (h2, m2) = hm(span[0]), hm(span[1])
    t = now.hour * 60 + now.minute
    return h1 * 60 + m1 <= t <= h2 * 60 + m2


def in_quiet(now: datetime, quiet=(21, 7)) -> bool:
    a, b = int(quiet[0]), int(quiet[1])
    return (now.hour >= a or now.hour < b) if a > b else (a <= now.hour < b)


def parse_any(ts) -> datetime | None:
    """epoch | 'YYYY-MM-DDTHH:MM:SSZ' | 'YYYY-MM-DD HH:MM' | 'YYYY-MM-DD' -> naive local datetime."""
    if ts in (None, ""):
        return None
    if isinstance(ts, (int, float)):
        return datetime.fromtimestamp(float(ts))
    s = str(ts).strip()
    try:
        if s.endswith("Z"):
            return datetime.strptime(s[:19], "%Y-%m-%dT%H:%M:%S") + (datetime.now() - datetime.utcnow())
        if "T" in s:
            return datetime.fromisoformat(s[:19])
        if len(s) >= 16 and s[10] == " ":
            return datetime.strptime(s[:16], "%Y-%m-%d %H:%M")
        return datetime.strptime(s[:10], "%Y-%m-%d")
    except ValueError:
        return None


def age_days(ts, now: datetime | None = None) -> float:
    d = parse_any(ts)
    if d is None:
        return 0.0
    return max(0.0, ((now or now_local()) - d).total_seconds() / 86400.0)


def state(name: str, default):
    return load_json(STATE_DIR / f"{name}.json", default)


def save_state(name: str, obj) -> None:
    save_json(STATE_DIR / f"{name}.json", obj)


def log(event: str, **kw) -> None:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    row = {"ts": now_local().strftime("%Y-%m-%d %H:%M:%S"), "event": event}
    row.update(kw)
    with open(LOG, "a", encoding="utf-8") as f:
        f.write(json.dumps(row, ensure_ascii=False) + "\n")


def rows(path: Path, since_days: float | None = None) -> list:
    out = []
    try:
        with open(path, encoding="utf-8") as f:
            for ln in f:
                try:
                    out.append(json.loads(ln))
                except ValueError:
                    continue
    except OSError:
        return []
    if since_days is not None:
        cut = now_local() - timedelta(days=since_days)
        out = [r for r in out if (parse_any(r.get("ts") or r.get("epoch")) or cut) >= cut]
    return out


# ------------------------------------------------------------------ record sources
def cards_live() -> list:
    out = []
    for p in sorted((DATA / "cards" / "live").glob("*.json")):
        c = load_json(p, None)
        if isinstance(c, dict) and c.get("status") == "open":
            out.append(c)
    return out


def card(qid: str) -> dict | None:
    for d in ("live", "archive", "superseded"):
        c = load_json(DATA / "cards" / d / f"{qid}.json", None)
        if isinstance(c, dict):
            return c
    return None


def card_answers(since_days: float | None = None) -> list:
    return rows(DATA / "cards" / "answers.jsonl", since_days)


def tickets_all() -> dict:
    return load_json(DATA / "tickets.json", {}) or {}


def followups_state() -> dict:
    return load_json(DATA / "followups" / "state.json", {"items": {}}) or {"items": {}}


def exec_tickets(since_days: float | None = None) -> list:
    """The executor's ledger rows (what actually went out / was written)."""
    p = DATA / "exec" / "tickets.jsonl"
    if not p.exists():
        p = DATA / "exec_tickets.jsonl"
    return rows(p, since_days)


def transcript(since_days: float | None = None) -> list:
    return rows(DATA / "agent" / "transcript.jsonl", since_days)


def vault_path(rel: str) -> Path:
    p = Path(rel)
    return p if p.is_absolute() else VAULT / p


def slug(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", str(s or "").lower())


def words(s: str) -> set:
    return {w for w in re.findall(r"[a-z0-9']{3,}", str(s or "").lower())
            if w not in {"the", "and", "for", "with", "your", "this", "that", "from", "project", "projects"}}


def shortdate(d) -> str:
    dd = parse_any(d)
    if not dd:
        return str(d or "")
    return f"{dd.month}/{dd.day}" if dd.year == now_local().year else f"{dd.month}/{dd.day}/{dd.year}"

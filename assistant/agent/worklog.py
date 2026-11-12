"""worklog.py - the agent's third memory scope: a dated log of what it DID, beside the facts it knows and the rules it keeps.

Upgrade 10 of the Grok Bot / Muse teardown (2026-11-08; Grok Bot keeps a per-Bot work log next to its shared facts).
Facts (memory.json) say what is true, rules (rules.json) say how to behave; neither says "on Tuesday I sent the
client's file and the owner approved the calendar hold". That lived only in the audit ledger and the
transcript, so "what did you do about X last week" meant a transcript hunt. This module is that log, fed by code:

  add(what, lane, ref, by, kind)   one line per thing done (drain after a ticket ran, a card the policy answered,
                                   a routine that fired, a doer job that finished, a build that landed)
  recent(days, lane)               the rows
  render(days, limit)              the prompt block "WORK LOG (what you did lately)", newest last, capped

Store: data/agent/worklog.jsonl (the organization's own, git-ignored like the transcript). Nothing here sends.
Lines are kept short and plain; a secret, a code or a number longer than eight digits is never written.
"""
from __future__ import annotations

import json
import re
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import core  # noqa: E402

FILE = core.DATA / "worklog.jsonl"
KEEP_DAYS = 60
RENDER_DAYS = 7
RENDER_MAX = 40
_SECRET = re.compile(r"\b\d{9,}\b|\b(?:password|passcode|pin|otp|token)\b\s*[:=]\s*\S+", re.I)


def add(what: str, lane: str = "", ref: str = "", by: str = "", kind: str = "") -> dict:
    what = re.sub(r"\s+", " ", str(what or "")).strip()
    if len(what) < 6:
        return {}
    what = _SECRET.sub("[redacted]", what)[:240]
    row = {"ts": core.stamp(), "what": what, "lane": str(lane or "")[:20], "ref": str(ref or "")[:60],
           "by": str(by or "")[:40], "kind": str(kind or "")[:30]}
    core.append(FILE, row)
    return row


def recent(days: float = RENDER_DAYS, lane: str = "") -> list:
    cut = time.time() - days * 86400
    out = []
    for r in core.rows(FILE):
        try:
            if time.mktime(time.strptime(r["ts"], "%Y-%m-%d %H:%M")) < cut:
                continue
        except (KeyError, ValueError):
            continue
        if lane and r.get("lane") and r.get("lane") != lane:
            continue
        out.append(r)
    return out


def render(days: float = RENDER_DAYS, limit: int = RENDER_MAX) -> str:
    rows = recent(days)[-limit:]
    if not rows:
        return ""
    lines = [f"WORK LOG (what you did in the last {int(days)} days, oldest first; facts about the world are in memory, this is your own record):"]
    for r in rows:
        lane = f" [{r['lane']}]" if r.get("lane") else ""
        lines.append(f"- {r['ts']}{lane} {r['what']}")
    return "\n".join(lines)


def prune(days: int = KEEP_DAYS) -> int:
    rows = core.rows(FILE)
    keep = recent(days)
    if len(keep) == len(rows):
        return 0
    FILE.parent.mkdir(parents=True, exist_ok=True)
    FILE.write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in keep), encoding="utf-8")
    return len(rows) - len(keep)


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    a = sys.argv[1:]
    if a[:1] == ["add"] and len(a) >= 2:
        print(add(" ".join(a[1:]), by="cli"))
    elif a[:1] == ["prune"]:
        print(f"pruned {prune()}")
    else:
        print(render(days=float(a[0]) if a else RENDER_DAYS) or "(nothing in the work log)")

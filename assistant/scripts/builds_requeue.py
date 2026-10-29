"""builds_requeue.py - put builds back in the queue when their failure was never about the build.

Two shapes (2026-10-18, BUILD_PLAN §5):
  * "touches the protected core: assistant/doer/tools/meta.json" (and any other file the test run itself
    regenerates): a false positive the builder fixed on 10-18 (it now ignores regenerated files). Those
    requests go back to `retry` with attempts 0 so the next builder tick takes them.
  * "You've hit your session limit · resets 6:50pm": the account was out of turns, not the build. Once the
    reset time named in the summary has passed, the request goes back to `retry` with attempts 0. the owner chose
    this over a builder.py change ("Build it another way", 09:34): a spine job, no edit to the builder's core.

Zero-LLM; edits data/agent/builds.json only (through builder.save_state's merge, never clobbering an owner's
answer). Spine job `builds-requeue`, every 30 min.

  python assistant/scripts/builds_requeue.py [--dry-run] [--all-protected]
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import time
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "agent"))
import core  # noqa: E402

STATE = core.DATA / "builds.json"
REGENERATED = ("assistant/doer/tools/meta.json",)
_LIMIT = re.compile(r"session limit.*?resets\s+(\d{1,2}):(\d{2})\s*(am|pm)", re.I | re.S)


def reset_passed(summary: str, last: str = "", now: datetime | None = None) -> bool:
    """True when the reset clock time named in a session-limit summary is behind us: the failure's own
    stamp (`last`, "YYYY-MM-DD HH:MM") is from an earlier day, or it is today and the clock has passed."""
    m = _LIMIT.search(summary or "")
    if not m:
        return False
    h, mi, ap = int(m.group(1)) % 12, int(m.group(2)), m.group(3).lower()
    if ap == "pm":
        h += 12
    now = now or datetime.now()
    try:
        day = datetime.strptime(str(last)[:10], "%Y-%m-%d").date()
    except ValueError:
        day = now.date()
    if day < now.date():
        return True
    return (now.hour, now.minute) >= (h, mi)


def classify(key: str, s: dict, now: datetime | None = None) -> str:
    """'' or the reason this entry should go back to retry."""
    if not isinstance(s, dict) or not key.startswith("Wiki/"):
        return ""
    status, summary = str(s.get("status") or ""), str(s.get("summary") or "")
    if status == "failed" and summary.startswith("touches the protected core"):
        hits = [h.strip() for h in summary.split(":", 1)[1].split(",")] if ":" in summary else []
        if hits and all(h in REGENERATED for h in hits):
            return "false protected-core hit on a file the test run regenerates"
    if status == "error" and _LIMIT.search(summary) and reset_passed(summary, str(s.get("last") or ""), now):
        return "failed only on the account's session limit; the reset time has passed"
    return ""


def tick(dry: bool = False, now: datetime | None = None, all_protected: bool = False, kick: bool = True,
         broken_tests: bool = False, too_large: bool = False) -> dict:
    st = core.load(STATE, {})
    base = json.loads(json.dumps(st))       # as loaded: the merge-save keeps every key this tick leaves alone
    out = {"requeued": [], "dry": dry}
    for key, s in st.items():
        why = classify(key, s, now)
        summary = str((s or {}).get("summary") or "") if isinstance(s, dict) else ""
        if not why and all_protected and isinstance(s, dict) and str(s.get("status")) == "failed" and summary.startswith("touches the protected core"):
            why = "protected-core hit re-queued by hand"
        if not why and broken_tests and isinstance(s, dict) and str(s.get("status")) == "failed" and summary.startswith("breaks tests"):
            why = "'breaks tests' re-queued by hand (the suite was flaky; the builder now reruns a failing suite once)"
        if not why and too_large and isinstance(s, dict) and str(s.get("status")) == "failed" and summary.startswith("diff too large"):
            why = "'diff too large' re-queued by hand (the cap is 2000 lines since 2026-10-23)"
        if not why:
            continue
        out["requeued"].append({"key": key, "was": s.get("status"), "why": why})
        if not dry:
            s.update(status="retry", attempts=0, requeued=core.stamp(), requeue_why=why, prior_summary=str(s.get("summary") or "")[:200])
    if out["requeued"] and not dry:
        import builder
        builder.save_state(st, "", base)    # locked merge-save: an owner's newer answer on any key wins
        if kick:                            # --no-kick: a hand run from inside a Claude session (the nested claude has no auth there)
            try:
                builder.kick(force=False)
            except Exception:  # noqa: BLE001
                pass
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--all-protected", action="store_true", help="also re-queue every 'touches the protected core' failure, whatever the file")
    ap.add_argument("--no-kick", action="store_true", help="re-queue without starting the builder (the spine's next builder tick takes them)")
    ap.add_argument("--broken-tests", action="store_true", help="also re-queue every 'breaks tests' failure (after a flaky suite is fixed)")
    ap.add_argument("--too-large", action="store_true", help="also re-queue every 'diff too large' failure (after the cap was raised)")
    a = ap.parse_args()
    r = tick(dry=a.dry_run, all_protected=a.all_protected, kick=not a.no_kick, broken_tests=a.broken_tests, too_large=a.too_large)
    print("BUILDS_REQUEUE " + json.dumps({"n": len(r["requeued"]), "dry": r["dry"], "keys": [x["key"].rsplit("/", 1)[-1][:60] for x in r["requeued"]]}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())

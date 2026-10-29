"""spine.py - the deterministic Day Spine (roadmap Phase 5, ported from the vault's night_ops/runner.py).

One Windows task fires this every 10 minutes; it runs the zero-LLM housekeeping jobs listed
in assistant/spine_jobs.json, each on its own cadence (`every_min`), with a timeout and a
per-job state file so a crashed job never blocks the others. No job here calls an LLM. The
operator tick (LLM) stays a separate task; this spine is what makes approved sends,
card sweeps, keepalives and health checks happen promptly and cheaply between ticks.

  python assistant/scripts/spine.py [--force JOB] [--list] [--dry-run] [--group long]

WP2 (2026-10-24): one pass has a budget (PASS_BUDGET_S, inside the task's 20-min kill): a job whose timeout
would not fit in what is left is skipped this pass (logged `skipped: budget`, its stamp untouched so it runs
first next time) instead of being killed with every job after it. Jobs with "group": "long" (the doer tick,
the drain) run only from `--group long`, a second scheduled task with its own time limit, so a slow browser
run never starves the sweep, the sentinels or the lead sensor. A job that declares "expect": "<prefix>" must
end with a line starting so; exit 0 without it is `silent` and counts as a failure (the exit-0-with-swallowed-
exception class: dreaming, Travel Scout, the heartbeat).

Sentinel last line: SPINE ran=<n> ok=<n> failed=<n> skipped=<n>.
"""
from __future__ import annotations

import os
import argparse
import json
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
VAULT = ROOT.parent
JOBS = ROOT / "spine_jobs.json"
STATE = ROOT / "data" / "spine_state.json"
# tests set WV_LOG_DIR: a test run must never write fixture lines into the live log
LOG = Path(os.environ.get("WV_LOG_DIR") or (Path.home() / ".claude")) / "spine-wv.log"
PY = sys.executable
PASS_BUDGET_S = 17 * 60     # the task kills the pass at 20 min; leave room for the job that is running


def _log(msg):
    line = time.strftime("%Y-%m-%d %H:%M:%S") + "  " + msg
    print(line)
    try:
        LOG.parent.mkdir(parents=True, exist_ok=True)
        with open(LOG, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except OSError:
        pass


def _load(path, default):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return default


def _in_window(job, now):
    win = job.get("window")
    if not win:
        return True
    h = now.hour + now.minute / 60
    start, end = win
    return start <= h < end if start < end else (h >= start or h < end)


REFLEX = ROOT / "data" / "agent" / "reflex.json"


def job_timeout(job) -> int:
    """The job's budget: its own, unless the reflex (assistant/agent/reflex.py) raised it after proving the job
    runs clean alone but over budget (job_overrides). Capped at 900 s; never lowered here."""
    own = int(job.get("timeout_s", 300))
    try:
        o = (_load(REFLEX, {}) or {}).get("job_overrides") or {}
        new = int((o.get(job.get("name")) or {}).get("timeout_s") or 0)
        return min(max(own, new), 900) if new else own
    except (TypeError, ValueError, AttributeError):
        return own


def run_job(job, dry):
    cmd = [PY if c == "python" else c for c in job["cmd"]]
    if dry:
        return True, "DRY " + " ".join(cmd)
    try:
        # hidden by default (2026-10-28): a job never opens a console window of its own, whatever started the spine
        r = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace",
                           timeout=job_timeout(job), cwd=str(VAULT), creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    except subprocess.TimeoutExpired:
        return False, "timeout"
    out = ((r.stdout or "") + (r.stderr or "")).strip()
    last = out.splitlines()[-1] if out else ""
    exp = str(job.get("expect") or "")
    if r.returncode == 0 and exp and not any(ln.strip().startswith(exp) for ln in out.splitlines()[-3:]):
        return False, ("silent: exit 0 without a " + exp + " line: " + last[:200])[:300]
    return r.returncode == 0, last[:300]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--force", default="")
    ap.add_argument("--list", action="store_true")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--group", default="", help="run only jobs with this group (default: jobs with no group)")
    a = ap.parse_args()
    jobs = [j for j in _load(JOBS, {}).get("jobs", []) if str(j.get("group") or "") == a.group or (a.force and a.force == j.get("name"))]
    state = _load(STATE, {})
    now = datetime.now()
    if a.list:
        for j in jobs:
            st = state.get(j["name"], {})
            print(f"{j['name']:22} every {j.get('every_min', 10):>4}m  {'on ' if j.get('enabled', True) else 'off'}  last={st.get('last', '-')[:16]} ok={st.get('ok', '-')}  {st.get('result', '')[:70]}")
        return 0
    ran = ok = failed = skipped = 0
    pass_t0 = time.time()
    for j in jobs:
        name = j["name"]
        st = state.get(name, {})
        due = True
        if not a.force and not a.dry_run and int(j.get("timeout_s", 300)) > PASS_BUDGET_S - (time.time() - pass_t0):
            _log(f"{name}: skipped: budget ({PASS_BUDGET_S - (time.time() - pass_t0):.0f}s left of the pass)")
            skipped += 1
            continue
        if a.force and a.force != name:
            skipped += 1
            continue
        if not a.force:
            if not j.get("enabled", True) or not _in_window(j, now):
                skipped += 1
                continue
            try:
                last = datetime.strptime(st.get("last", "1970-01-01T00:00:00"), "%Y-%m-%dT%H:%M:%S")
            except ValueError:
                last = datetime(1970, 1, 1)
            due = (now - last).total_seconds() >= int(j.get("every_min", 10)) * 60 - 30   # 30 s slack for scheduler jitter
        if not due:
            skipped += 1
            continue
        t0 = time.time()
        good, result = run_job(j, a.dry_run)
        ran += 1
        ok += good
        failed += (not good)
        if not a.dry_run:
            fails = 0 if good else int(st.get("fails", 0)) + 1
            state[name] = {"last": now.strftime("%Y-%m-%dT%H:%M:%S"), "ok": good, "result": result,
                           "secs": round(time.time() - t0, 1), "fails": fails}
            _log(f"{name}: {'ok' if good else 'FAIL'} ({time.time() - t0:.1f}s) {result}")
            if fails and fails % 3 == 0:
                _log(f"{name}: {fails} consecutive failures")
            # after EVERY job: if Task Scheduler kills a long pass, the jobs that already ran
            # keep their stamp (no double run) and a failing job still counts its failures.
            try:
                STATE.parent.mkdir(parents=True, exist_ok=True)
                tmp = STATE.with_suffix(".tmp")
                tmp.write_text(json.dumps(state, indent=1), encoding="utf-8")
                tmp.replace(STATE)
            except OSError as e:
                _log(f"state save failed: {e}")
    _log(f"SPINE ran={ran} ok={ok} failed={failed} skipped={skipped}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

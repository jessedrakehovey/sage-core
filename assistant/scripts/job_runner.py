"""job_runner.py - runs ONE registered local job (jobs.py launch) to the end and says how it went.

Detached from drain so a job may take hours (a handoff build renders proxies all night). Output
goes to --log; when the command exits, one plain line lands in chat and on Mission Control
(cards.say) with the outcome and the last line of the log, and the audit ledger gets a row.

  python assistant/scripts/job_runner.py --lane wedding --name "Editor handoff build - couple: X" --log <file> -- <cmd...>
"""
from __future__ import annotations

import argparse
import subprocess
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
VAULT = HERE.parent.parent
sys.path.insert(0, str(HERE))


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--lane", default="personal")
    ap.add_argument("--name", required=True)
    ap.add_argument("--log", required=True)
    ap.add_argument("cmd", nargs=argparse.REMAINDER)
    a = ap.parse_args()
    cmd = [c for c in a.cmd if c != "--"]
    log = Path(a.log)
    log.parent.mkdir(parents=True, exist_ok=True)
    t0 = time.time()
    with open(log, "a", encoding="utf-8", errors="replace") as f:
        f.write(f"=== {a.name}\n=== {time.strftime('%Y-%m-%d %H:%M:%S')}  {' '.join(cmd)}\n")
        f.flush()
        try:
            rc = subprocess.call(cmd, cwd=str(VAULT), stdin=subprocess.DEVNULL, stdout=f, stderr=subprocess.STDOUT)
        except OSError as e:
            f.write(f"could not start: {e}\n")
            rc = 127
    mins = (time.time() - t0) / 60
    took = f"{mins / 60:.1f} h" if mins >= 90 else f"{mins:.0f} min"
    try:
        tail = [ln for ln in log.read_text(encoding="utf-8", errors="replace").splitlines() if ln.strip() and not ln.startswith("===")]
        last = tail[-1][:200] if tail else ""
    except OSError:
        last = ""
    text = (f"{a.name} finished in {took}: {last}" if rc == 0 else
            f"{a.name} FAILED after {took} (exit {rc}): {last} - log {log}")
    try:
        import cards
        import exec_store as es
        cards.say("jobs", a.lane, text)
        es.ledger("job_done", name=a.name[:120], rc=rc, minutes=round(mins, 1), log=str(log))
    except Exception as e:  # noqa: BLE001  the outcome is still in the log
        print(f"could not report in chat: {e}", file=sys.stderr)
    return rc


if __name__ == "__main__":
    sys.exit(main())

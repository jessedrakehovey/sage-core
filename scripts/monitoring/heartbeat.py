"""heartbeat.py — alert the owner via Mission Control if a critical wedding-automation component looks down.

QUIET when healthy (writes a log line, sends nothing). Posts ONE Mission Control card listing
problems only when something is wrong, so it can run often without nagging. Run on a schedule.
Telegram was removed 2026-10-04; Mission Control (assistant/data/cards.jsonl) is the channel.

Checks:
  1. "the organization" scheduled task: existence + log recency (26h).
     Only alerts when the task is ENABLED — a disabled task is intentional and must not page.
  2. graphify log recency (26h window). Only alerts when the graphify task is ENABLED.

To suppress repeat alerts for the same problem set, it remembers the last alert signature in
.cache/heartbeat_state.json and only re-pings when the problem set changes or after 12h.
"""
from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import time
from datetime import datetime
from pathlib import Path

HERE = Path(__file__).resolve().parent
CACHE = HERE / ".cache"
STATE = CACHE / "heartbeat_state.json"
LOG = Path.home() / ".claude" / "heartbeat-wv.log"
CLAUDE = Path.home() / ".claude"
_WV = HERE.parents[1]
NOTIFY = _WV / "assistant" / "scripts" / "notify.py"


H = 3600.0
RE_ALERT_AFTER = 12 * H  # re-ping an unchanged problem set at most every 12h


def log(m: str):
    LOG.parent.mkdir(parents=True, exist_ok=True)
    with LOG.open("a", encoding="utf-8") as f:
        f.write(f"{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}  {m}\n")


def age_h(p: Path) -> float | None:
    try:
        return (time.time() - p.stat().st_mtime) / H
    except FileNotFoundError:
        return None


def _task_state(task_name: str) -> tuple[bool, bool]:
    """Return (exists, enabled) for a Windows Scheduled Task.

    Uses schtasks /query /tn to probe existence and state.  Returns (False, False)
    if the task does not exist, (True, False) if it exists but is disabled,
    (True, True) if it is enabled/ready.  Never raises — on any error it returns
    (True, True) to avoid false-alarming when the check itself fails.
    """
    try:
        r = subprocess.run(
            ["schtasks", "/query", "/tn", task_name, "/fo", "csv", "/v"],
            capture_output=True, text=True, timeout=30,
            creationflags=subprocess.CREATE_NO_WINDOW,
        )
        if r.returncode != 0:
            # Non-zero usually means the task was not found.
            return False, False
        output = r.stdout or ""
        # The CSV /v output includes a "Scheduled Task State" column.
        # Its value is "Enabled" when the task is scheduled to run.
        enabled = "Enabled" in output
        return True, enabled
    except Exception as ex:
        log(f"task state check error ({task_name!r}): {type(ex).__name__}")
        return True, True  # don't false-alarm if the check itself fails


def _mc_alert(body: str) -> bool:
    """Best-effort Mission Control card. Never raises. Returns True on success."""
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8",
                                         suffix=".txt", delete=False) as f:
            f.write(body[:3500])
            tmp = f.name
        r = subprocess.run(
            [sys.executable, str(NOTIFY), "--file", tmp],
            capture_output=True, text=True, timeout=30,
        )
        Path(tmp).unlink(missing_ok=True)
        return r.returncode == 0
    except Exception:
        return False


def main() -> int:
    problems = []

    # ── Check 1: Wedding Dreaming scheduled task + log recency ──────────────
    DREAMING_TASK = "example-org"
    dreaming_log = CLAUDE / "dreaming-wv.log"

    task_exists, task_enabled = _task_state(DREAMING_TASK)
    if not task_exists:
        problems.append(
            f"🔴 Scheduled task \"{DREAMING_TASK}\" not found — "
            "dreaming is not registered. Re-run the task-registration step."
        )
    elif task_enabled:
        a = age_h(dreaming_log)
        if a is None:
            log(f"note: dreaming-wv.log has no log yet ({dreaming_log.name})")
        elif a > 26:
            problems.append(
                f"🟠 Wedding Dreaming: last ran {a:.0f}h ago (expected within ~24h). "
                f"Check ~/.claude/dreaming-wv.log."
            )
    else:
        log(f"note: task \"{DREAMING_TASK}\" is disabled — skipping log-recency check")

    # ── Check 2: graphify log recency (enabled-gated) ────────────────────────
    GRAPHIFY_TASK = "Wedding Graphify"
    graphify_log = CLAUDE / "graphify-wv.log"

    g_exists, g_enabled = _task_state(GRAPHIFY_TASK)
    if g_exists and g_enabled:
        a = age_h(graphify_log)
        if a is None:
            log(f"note: graphify-wv.log has no log yet ({graphify_log.name})")
        elif a > 26:
            problems.append(
                f"🟠 Graphify: last ran {a:.0f}h ago (expected within ~24h). "
                f"Check ~/.claude/graphify-wv.log."
            )
    elif g_exists:
        log(f"note: task \"{GRAPHIFY_TASK}\" is disabled — skipping log-recency check")
    else:
        log(f"note: task \"{GRAPHIFY_TASK}\" not found — skipping graphify check")

    # ── Alert logic ──────────────────────────────────────────────────────────
    sig = " | ".join(problems)

    st: dict = {}
    if STATE.exists():
        try:
            st = json.loads(STATE.read_text(encoding="utf-8"))
        except Exception:
            st = {}

    if not problems:
        log("OK — all components healthy")
        CACHE.mkdir(exist_ok=True)
        STATE.write_text(
            json.dumps({"last_sig": "", "last_alert_ts": st.get("last_alert_ts", 0)}),
            encoding="utf-8",
        )
        return 0

    # Something's wrong — alert, but de-dupe identical problem sets within RE_ALERT_AFTER.
    now = time.time()
    same = st.get("last_sig") == sig
    recent = (now - float(st.get("last_alert_ts", 0))) < RE_ALERT_AFTER
    if same and recent:
        log(f"problems unchanged + recently alerted; staying quiet: {sig}")
        return 0

    body = "Wedding automation heartbeat -- issues detected:\n\n" + "\n".join(problems)

    delivered = _mc_alert(body)
    if not delivered:
        log(f"ALERT UNDELIVERED: Mission Control notify failed; see heartbeat-wv.log: {sig}")
    CACHE.mkdir(exist_ok=True)
    STATE.write_text(json.dumps({"last_sig": sig, "last_alert_ts": now}), encoding="utf-8")
    log(f"ALERTED: {sig}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

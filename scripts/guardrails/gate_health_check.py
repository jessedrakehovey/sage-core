#!/usr/bin/env python3
"""gate_health_check.py — proactive send-gate regression alarm for wedding videography.

Runs the check_gates.py tripwire and, if any gate regressed, posts a Mission Control card.
The pre-commit hook only catches gate regressions that arrive via a COMMIT; this catches
RUNTIME regressions too — most importantly a 6:30 AM dreaming auto-apply that rewrites
.claude/settings.json or any manual edit. This cron is proactive; /fixgate is reactive.

Telegram was removed 2026-10-04; alerts go to Mission Control (assistant/data/cards.jsonl).
Quiet on pass. Stdlib only so it runs from any python / minimal cron env.

  python gate_health_check.py             # check; alert on FAIL; exit 1 on FAIL
  python gate_health_check.py --quiet     # same, but print nothing on PASS (cron use)
  python gate_health_check.py --no-alert  # check + print, never post card (for testing)
"""
import json
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
VAULT = HERE.parents[1]
CHECK = HERE / "check_gates.py"
NOTIFY = VAULT / "assistant" / "scripts" / "notify.py"


def _mc_alert(text: str) -> bool:
    """Best-effort Mission Control card. Never raises. Returns True on success."""
    try:
        import tempfile
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8",
                                         suffix=".txt", delete=False) as f:
            f.write(text[:3500])
            tmp = f.name
        r = subprocess.run(
            [sys.executable, str(NOTIFY), "--file", tmp],
            capture_output=True, text=True, timeout=30,
        )
        Path(tmp).unlink(missing_ok=True)
        return r.returncode == 0
    except Exception:
        return False


def _persistent_env_leak(name):
    """Return 'User' or 'Machine' if `name` is set in the PERSISTENT (registry) environment,
    else None. WV_GATED_SEND must only ever exist transiently in a per-send subprocess env.
    If it leaks into the persistent environment, every future session inherits it and the
    gmail-send hook blanket-allows — the exact threat to detect directly. Read-only; never raises."""
    try:
        import winreg
    except Exception:
        return None
    spots = [
        ("User", winreg.HKEY_CURRENT_USER, r"Environment"),
        ("Machine", winreg.HKEY_LOCAL_MACHINE,
         r"SYSTEM\CurrentControlSet\Control\Session Manager\Environment"),
    ]
    for label, root, path in spots:
        try:
            with winreg.OpenKey(root, path) as k:
                val, _ = winreg.QueryValueEx(k, name)
            if str(val).strip():
                return label
        except (FileNotFoundError, OSError):
            continue
    return None


def main() -> int:
    quiet = "--quiet" in sys.argv[1:]
    no_alert = "--no-alert" in sys.argv[1:]
    proc = subprocess.run([sys.executable, str(CHECK)], capture_output=True, text=True,
                          encoding="utf-8", errors="replace")
    out = ((proc.stdout or "") + (proc.stderr or "")).strip()
    problems = []
    if proc.returncode != 0:
        problems.append("GATE TEXT REGRESSION:\n" + out
                        + "\n-> run /fixgate, or inspect with check_gates.py")
    leak = _persistent_env_leak("WV_GATED_SEND")
    if leak:
        problems.append(
            f"ENV LEAK: WV_GATED_SEND is set in the persistent {leak} environment. "
            "It must only be set transiently per approved send; in the persistent env it "
            "can blanket-allow gmail sends across sessions. Remove it: "
            'setx WV_GATED_SEND "" then delete the empty value, or '
            "reg delete HKCU\\Environment /v WV_GATED_SEND /f"
        )
    if problems:
        msg = "\n\n".join(problems)
        print(msg)               # always surface a failure (cron logs it)
        if not no_alert:
            delivered = _mc_alert("\U0001F6A8 SEND-GATE HEALTH ALERT (Wedding)\n\n" + msg[:3500])
            if not delivered:
                print("GATE-ALERT UNDELIVERED: Mission Control notify failed; "
                      "check ~/.claude/gate-health-wv.log.")
        return 1
    if not quiet:
        print((out or "GATE CHECK PASSED") + "\nENV LEAK CHECK: clean (WV_GATED_SEND not in persistent env)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

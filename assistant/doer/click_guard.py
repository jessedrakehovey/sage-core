"""click_guard.py - the doer's cap on a control that will not take a click (2026-10-19).

the CRM's project pages never settle: four runs on 2026-10-18 clicked the participants row
("+2", "Add") until the 80-step limit, each click timing out on "waiting for element to be
visible, enabled and stable". prompt.md tells the turn to force such a control once through
browser_evaluate after two timeouts; this hook makes the stop law. doer.run_turn passes it with
--settings on the doer's browser tools:

  PostToolUse  a click/evaluate whose result is that timeout counts against its control (the
               snapshot ref, else the element name); the 3rd timeout on one control returns
               "block" with the stop message.
  PreToolUse   once a control is capped, every further browser action except a snapshot or a
               screenshot is denied with the same message, so the turn reports instead of looping.

State is <out_dir>/click_guard.json, cleared at the start of every turn. A cap, not a safety
gate: any error in here lets the call through.

  python assistant/doer/click_guard.py --hook <post|pre> <out_dir>    (stdin: the hook JSON)
"""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path

MAX_TIMEOUTS = 3
STATE = "click_guard.json"
LOOK_ONLY = ("browser_snapshot", "browser_take_screenshot")
TIMEOUT = re.compile(r"timeout|timed out", re.I)
UNSTABLE = re.compile(r"waiting for element to be[^\"]{0,40}stable|element is not stable|not stable", re.I)


def is_stuck(response) -> bool:
    """True for a click result that timed out waiting for the control to be visible/enabled/stable."""
    s = response if isinstance(response, str) else json.dumps(response, ensure_ascii=False)
    return bool(TIMEOUT.search(s) and UNSTABLE.search(s))


def control(tool_input: dict) -> str:
    ti = tool_input or {}
    return str(ti.get("ref") or ti.get("element") or "?")


def stop_message(key: str, n: int) -> str:
    return (f"The control {key} has timed out {n} times waiting to be stable. Stop working this page now: take "
            f"one browser_snapshot, then end with `DOER_BLOCKED: <site>'s <control> will not take a click "
            f"({n} timeouts); the page shows <url and what you see>; controls I could reach: <the buttons, links "
            f"and fields that did respond>`. Change nothing else.")


def _load(out_dir: Path) -> dict:
    try:
        return json.loads((out_dir / STATE).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def reset(out_dir: Path) -> None:
    try:
        (out_dir / STATE).unlink()
    except OSError:
        pass


def post(ev: dict, out_dir: Path) -> dict | None:
    """Count a stuck click; the 3rd on one control blocks with the stop message."""
    if not is_stuck(ev.get("tool_response")):
        return None
    st = _load(out_dir)
    key = control(ev.get("tool_input"))
    counts = st.setdefault("timeouts", {})
    counts[key] = int(counts.get(key, 0)) + 1
    if counts[key] >= MAX_TIMEOUTS and not st.get("capped"):
        st["capped"] = key
    (out_dir / STATE).write_text(json.dumps(st), encoding="utf-8")
    if counts[key] >= MAX_TIMEOUTS:
        return {"decision": "block", "reason": stop_message(key, counts[key])}
    return None


def pre(ev: dict, out_dir: Path) -> dict | None:
    """After the cap, only looking is allowed."""
    st = _load(out_dir)
    key = st.get("capped")
    if not key or str(ev.get("tool_name") or "").endswith(LOOK_ONLY):
        return None
    return {"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "deny",
                                   "permissionDecisionReason": stop_message(key, int(st["timeouts"].get(key, MAX_TIMEOUTS)))}}


def main(argv: list[str]) -> int:
    try:
        if len(argv) < 4 or argv[1] != "--hook":
            print(__doc__)
            return 0
        ev = json.loads(sys.stdin.read() or "{}")
        out = (post if argv[2] == "post" else pre)(ev, Path(argv[3]))
        if out:
            print(json.dumps(out))
    except Exception:  # noqa: BLE001 - a cap, never a reason for a call to fail
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))

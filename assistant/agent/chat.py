"""chat.py - talk to the agent from a terminal (the `local` channel).

  python assistant/agent/chat.py --as the owner "what weddings are coming up?"
  python assistant/agent/chat.py --as owner --no-wait "remind me friday 9am to call the florist"

Writes one line to the agent's inbox and waits for the bridge's reply in the outbox. The
bridge must be running (`bridge.py --status`); it is kept alive by the Day Spine.
"""
from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import core  # noqa: E402
from bridge import INBOX, OUTBOX, alive  # noqa: E402


def main() -> int:
    sys.stdout.reconfigure(encoding="utf-8")
    ap = argparse.ArgumentParser()
    ap.add_argument("text")
    ap.add_argument("--as", dest="who", default=core.first_owner())
    ap.add_argument("--no-wait", action="store_true")
    ap.add_argument("--timeout", type=int, default=600)
    a = ap.parse_args()
    if not core.person_for(handle=a.who):
        print(f"unknown person {a.who!r} (see assistant/agent/people.json)")
        return 2
    seen = len(core.rows(OUTBOX))
    core.append(INBOX, {"ts": core.stamp(), "from": a.who, "text": a.text})
    if a.no_wait:
        print("QUEUED")
        return 0
    if not alive():
        print("QUEUED, but the bridge is not running (python assistant/agent/bridge.py --status)")
        return 1
    deadline = time.time() + a.timeout
    while time.time() < deadline:
        new = [r for r in core.rows(OUTBOX)[seen:] if r.get("to") == a.who and r.get("via") == "local"]
        if new:
            print(new[0]["text"])
            return 0
        time.sleep(1.5)
    print("TIMEOUT waiting for a reply")
    return 1


if __name__ == "__main__":
    sys.exit(main())

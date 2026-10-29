"""orgcfg.py - the organization's own values, read from ONE file (WP5 of the stand-alone proposal, 2026-10-24).

Until this, the identities this OS may send from, the phones it may text from, the Mission Control URL, the
scheduled-task names the sentinels watch and the alert addresses were literals in seven modules (exec_store,
phone_bridge, chat_push, comms_page, doer, sentinels, sense_typeform). `new_org.py` could not stand up a second
organization without editing code. Now they live in `assistant/org.json` and every module asks here; the old
literal is each module's fallback when the file lacks the key, so this organization behaves exactly as before.

`assistant/org.json` is protected core (builder.PROTECTED_PATHS): the send identities are what the gate guards.
Business facts the agent LEARNS (name, people, lanes, services, duties) stay in `agent/state/org.json` (org.py);
this file holds what the CODE needs before anyone has taught it anything.

  python assistant/orgcfg.py            # print the resolved values
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
FILE = Path(os.environ["ASSISTANT_ORG"]) if os.environ.get("ASSISTANT_ORG") else ROOT / "org.json"
_CACHE = {"mtime": None, "data": {}}


def load() -> dict:
    try:
        m = FILE.stat().st_mtime
    except OSError:
        return {}
    if _CACHE["mtime"] != m:
        try:
            _CACHE["data"] = json.loads(FILE.read_text(encoding="utf-8-sig")) or {}
        except (OSError, ValueError):
            _CACHE["data"] = {}
        _CACHE["mtime"] = m
    return _CACHE["data"]


def get(key: str, default=None):
    v = load().get(key)
    return default if v is None else v


def tuple_of(key: str, default: tuple) -> tuple:
    v = get(key)
    return tuple(str(x) for x in v) if isinstance(v, (list, tuple)) and v else tuple(default)


def main() -> int:
    print(json.dumps({k: v for k, v in load().items() if not str(k).startswith("_")}, indent=1, ensure_ascii=False) or "{} (no org.json)")
    return 0


if __name__ == "__main__":
    sys.exit(main())

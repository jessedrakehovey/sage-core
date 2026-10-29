"""jobs.py - the local jobs an owner can run now or at a time from chat (2026-10-24).

the owner: "I need to be able to schedule text sends and everything else I've asked it to do today."
Sends already go through cards -> exec tickets -> drain; a ticket now carries `not_before`
(exec_store), so a card can say "sends at Sat Oct 25, 9:00 AM" and the tap mints a ticket
that drain leaves alone until then. This module is the same idea for LOCAL WORK: a registry
(assistant/jobs.json) of fixed commands with named values. Sage's `job` tool validates
against it and posts a card (exec kind `local`, payload {op, args}, send_at); the tap mints
the ticket; drain calls launch() when it is due. The registry is the allow-list - no shell,
no free-form commands, every placeholder named in the file.

  python assistant/scripts/jobs.py                 # what Sage sees (JOBS YOU CAN RUN)
  python assistant/scripts/jobs.py run handoff_build couple="example"   # launch one by hand
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
VAULT = HERE.parent.parent
FILE = Path(os.environ.get("ASSISTANT_JOBS") or (HERE.parent / "jobs.json"))
DATA = Path(os.environ.get("ASSISTANT_DATA") or (HERE.parent / "data"))
LOGS = DATA / "jobs"
PLACEHOLDER = re.compile(r"\{([a-z_][a-z0-9_]*)\}")


def registry() -> dict:
    try:
        return json.loads(FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {"jobs": [], "vars": {}}


def jobs() -> list:
    return [j for j in (registry().get("jobs") or []) if isinstance(j, dict) and j.get("key") and j.get("cmd")]


def _norm(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", str(s or "").lower()).strip()


def find(name: str) -> dict | None:
    """A job by key or alias (case and punctuation do not matter)."""
    n = _norm(name)
    if not n:
        return None
    for j in jobs():
        if _norm(j["key"]) == n or n in {_norm(a) for a in (j.get("aliases") or [])} or _norm(j.get("name")) == n:
            return j
    return None


def required(j: dict) -> list:
    """The value names the command itself needs (placeholders not supplied by `vars`)."""
    v = registry().get("vars") or {}
    return [p for part in j["cmd"] for p in PLACEHOLDER.findall(str(part)) if p not in v]


def build_cmd(j: dict, args: dict) -> list:
    """The exact argv for `j` with `args` filled in. ValueError names what is missing or bad."""
    args = {str(k): str(v).strip() for k, v in (args or {}).items() if str(v or "").strip()}
    bad = [k for k, v in args.items() if "\n" in v or "\r" in v]
    if bad:
        raise ValueError(f"job value {bad[0]} must be one line")
    known = set(j.get("args") or {}) | set(j.get("options") or {})
    stray = sorted(set(args) - known)
    if stray:
        raise ValueError(f"{j['key']} takes no value named {stray[0]}; it takes: {', '.join(sorted(known)) or 'nothing'}")
    missing = [p for p in required(j) if not args.get(p)]
    if missing:
        raise ValueError(f"{j['key']} needs {missing[0]} ({(j.get('args') or {}).get(missing[0], '')})")
    v = {**(registry().get("vars") or {}), **args}

    def fill(part):
        return PLACEHOLDER.sub(lambda m: v[m.group(1)], str(part))
    cmd = [fill(p) for p in j["cmd"]]
    for name, extra in (j.get("options") or {}).items():
        if args.get(name):
            cmd += [fill(p) for p in extra]
    return cmd


def describe(j: dict, args: dict) -> str:
    """'Editor handoff build - couple: example' (the words the owner reads on the card)."""
    vals = ", ".join(f"{k}: {v}" for k, v in (args or {}).items() if str(v or "").strip())
    return f"{j.get('name', j['key'])}" + (f" - {vals}" if vals else "")


def launch(j: dict, args: dict) -> dict:
    """Start the job detached (it may run for hours) with its output in a log; job_runner.py says
    the outcome in chat when it ends. -> {pid, log, cmd}. ValueError when the values are wrong."""
    cmd = build_cmd(j, args)
    LOGS.mkdir(parents=True, exist_ok=True)
    log = LOGS / f"{j['key']}_{time.strftime('%Y-%m-%d_%H%M%S')}.log"
    runner = [sys.executable, str(HERE / "job_runner.py"), "--lane", str(j.get("lane") or "personal"),
              "--name", describe(j, args), "--log", str(log), "--"] + cmd
    flags = 0
    if os.name == "nt":
        flags = subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP
    p = subprocess.Popen(runner, cwd=str(VAULT), stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                         stderr=subprocess.DEVNULL, creationflags=flags, close_fds=True)
    return {"pid": p.pid, "log": str(log), "cmd": cmd}


def render() -> str:
    js = jobs()
    if not js:
        return ""
    rows = []
    for j in js:
        a = ", ".join(f"{k} ({v})" for k, v in (j.get("args") or {}).items())
        o = ", ".join(j.get("options") or {})
        rows.append(f"- {j['key']}: {j.get('name', j['key'])} - {j.get('what', '')}. Values: {a or 'none'}"
                    + (f"; optional: {o}" if o else ""))
    return "\n".join(["JOBS YOU CAN RUN (job {job, args, at?}: ONE card for the owner's tap runs it; `at` = YYYY-MM-DD HH:MM "
                      "runs it then; long jobs report in chat when they finish):"] + rows)


if __name__ == "__main__":
    if len(sys.argv) > 2 and sys.argv[1] == "run":
        j = find(sys.argv[2])
        if not j:
            sys.exit(f"no job {sys.argv[2]!r}; jobs: {[x['key'] for x in jobs()]}")
        kv = dict(a.split("=", 1) for a in sys.argv[3:] if "=" in a)
        print(json.dumps(launch(j, kv), indent=1))
    else:
        print(render() or "no jobs registered")

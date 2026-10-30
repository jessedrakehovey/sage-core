"""switches.py - owners turn a standing job on or off, or run it now, from chat. Built 2026-10-23.

the owner, 2026-10-23: "Sage should be able to turn the schedule on and off from in chat" (the nightly
YouTube Scout), and the same day: "Make it so Sage can do anything I asked it to do" - the scout
could not be stopped because the doer may never touch a scheduled task. The jobs that may be
switched are DATA in switches.json (Sage is a template: a new job is a new entry there, never code).
A job names one or both of:
  "config" + "field"   one true/false field in one config file (flipped in place, layout kept)
  "task"               the Windows Scheduled Task registered from this repository (Enable- /
                       Disable- / Start-ScheduledTask, run by the BRIDGE in the owner's session;
                       the doer's computer policy still bans schtasks, that ban is unchanged)
Only the tasks listed here can be touched: the registry is the allow-list, and nothing here
creates, deletes or edits a task's trigger or action.

  the `switch` tool (gmcp.py) -> intents.validate -> actions.apply -> set_on() / run_now()
  render() feeds the brain's context (via routines.render) so Sage knows what it can switch, how
  each one stands right now and that it can run one on demand.

  python assistant/agent/switches.py            # every job and whether it is on
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import core  # noqa: E402

REGISTRY = Path(os.environ["AGENT_SWITCHES"]) if os.environ.get("AGENT_SWITCHES") else Path(__file__).resolve().parent / "switches.json"
ROOT = Path(os.environ["AGENT_SWITCH_ROOT"]) if os.environ.get("AGENT_SWITCH_ROOT") else core.VAULT
PS_TIMEOUT_S = 40


def jobs() -> list:
    try:
        return [j for j in (json.loads(REGISTRY.read_text(encoding="utf-8")).get("jobs") or [])
                if isinstance(j, dict) and j.get("key") and ((j.get("config") and j.get("field")) or j.get("task"))]
    except (OSError, ValueError):
        return []


def _norm(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", str(s or "").lower()).strip()


def find(name: str) -> dict | None:
    """A job by its key, name or one of its aliases (loose: 'the youtube scout' finds youtube-scout)."""
    n = _norm(name)
    if not n:
        return None
    for j in jobs():
        names = [j["key"], j.get("name", ""), j.get("task", "")] + list(j.get("aliases") or [])
        if any(n == _norm(x) for x in names):
            return j
    for j in jobs():
        names = [j["key"], j.get("name", ""), j.get("task", "")] + list(j.get("aliases") or [])
        if any(_norm(x) and (_norm(x) in n or n in _norm(x)) for x in names):
            return j
    return None


# ------------------------------------------------------------------ the config field
def _path(j: dict) -> Path:
    p = (ROOT / j["config"]).resolve()
    if ROOT.resolve() not in p.parents:
        raise ValueError("config outside the vault")
    return p


def config_state(j: dict) -> bool | None:
    """True / False, or None when the job has no config field or it can't be read."""
    if not (j.get("config") and j.get("field")):
        return None
    try:
        v = json.loads(_path(j).read_text(encoding="utf-8")).get(j["field"])
    except (OSError, ValueError):
        return None
    return v if isinstance(v, bool) else None


def _set_config(j: dict, on: bool) -> str:
    """Flip the one field in place. -> '' or the reason it was refused."""
    p = _path(j)
    text = p.read_text(encoding="utf-8")
    pat = re.compile(r'("' + re.escape(j["field"]) + r'"\s*:\s*)(true|false)')
    if len(pat.findall(text)) != 1:
        return f"{j['field']} is not a single true/false in {j['config']}"
    new = pat.sub(lambda m: m.group(1) + ("true" if on else "false"), text, count=1)
    json.loads(new)                                     # never write a config that no longer parses
    p.write_text(new, encoding="utf-8")
    return ""


# ------------------------------------------------------------------ the Windows task
def _ps(script: str) -> tuple[int, str]:
    """Run one PowerShell line (no profile, hidden). -> (returncode, output). Tests replace this."""
    try:
        r = subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command", script], capture_output=True,
                           text=True, encoding="utf-8", errors="replace", timeout=PS_TIMEOUT_S, creationflags=core.NO_WINDOW)
        return r.returncode, (r.stdout or "").strip() + (("\n" + r.stderr.strip()) if r.stderr.strip() else "")
    except (OSError, subprocess.SubprocessError) as e:
        return 1, f"{type(e).__name__}: {e}"


def _q(name: str) -> str:
    return "'" + str(name).replace("'", "''") + "'"


_STATES: dict = {"ts": 0.0, "map": {}}
STATE_CACHE_S = 45


def task_states(fresh: bool = False) -> dict:
    """{task name: 'Ready' | 'Running' | 'Disabled' | 'Queued'} for every registered task, in ONE PowerShell
    call, cached briefly: render() runs on every brain turn and eleven separate calls took 15 s."""
    import time
    names = [j["task"] for j in jobs() if j.get("task")]
    if not names:
        return {}
    if not fresh and time.time() - _STATES["ts"] < STATE_CACHE_S:
        return _STATES["map"]
    arr = ",".join(_q(n) for n in names)
    rc, out = _ps(f"Get-ScheduledTask -TaskName @({arr}) -ErrorAction SilentlyContinue | ForEach-Object {{ $_.TaskName + '|' + $_.State }}")
    m = {}
    for ln in out.splitlines():                            # Get-ScheduledTask exits 1 when ANY name is missing; the found ones still print
        if "|" in ln:
            n, s = ln.rsplit("|", 1)
            m[n.strip()] = s.strip()
    _STATES.update(ts=time.time(), map=m)                  # cached either way: a new organization has none registered yet (2026-10-29)
    return m


def task_state(j: dict, fresh: bool = False) -> str:
    """'Ready' / 'Running' / 'Disabled' / 'Queued' for the job's task, '' when it has none or can't be read."""
    if not j.get("task"):
        return ""
    return task_states(fresh).get(j["task"], "")


def _set_task(j: dict, on: bool) -> str:
    """Enable or disable the registered task. -> '' or the reason."""
    verb = "Enable-ScheduledTask" if on else "Disable-ScheduledTask"
    rc, out = _ps(f"{verb} -TaskName {_q(j['task'])} -ErrorAction Stop | Out-Null; (Get-ScheduledTask -TaskName {_q(j['task'])}).State")
    if rc:
        return f"the task could not be {'enabled' if on else 'disabled'}: {out[-160:]}"
    want = "Disabled" if not on else None
    got = out.splitlines()[-1].strip() if out else ""
    if want and got != want:
        return f"the task still reads {got or 'unknown'}"
    if on and got == "Disabled":
        return "the task still reads Disabled"
    return ""


# ------------------------------------------------------------------ what the owners get
def state(j: dict) -> bool | None:
    """Is the job on? The config field when it has one, else the task's state; None when unreadable."""
    c = config_state(j)
    if c is not None:
        return c
    t = task_state(j)
    return None if not t else t != "Disabled"


def set_on(key: str, on: bool, by: str = "") -> dict:
    """-> {"status": "applied" | "unchanged" | "refused", "job", "on", "reason"}. A job with both a
    config field and a task flips both, so a stopped job neither runs nor pretends to."""
    j = find(key)
    if not j:
        return {"status": "refused", "reason": f"no switchable job called {key!r}"}
    on = bool(on)
    c, t = config_state(j), task_state(j, fresh=True)
    _STATES["ts"] = 0.0                                 # whatever happens below, the next render reads the task afresh
    if c is None and not t:
        return {"status": "refused", "job": j["key"], "reason": f"can't read {j.get('config') or j.get('task')}"}
    t_on = None if not t else t != "Disabled"
    if (c is None or c == on) and (t_on is None or t_on == on):
        return {"status": "unchanged", "job": j["key"], "on": on}
    done = []
    if c is not None and c != on:
        why = _set_config(j, on)
        if why:
            return {"status": "refused", "job": j["key"], "reason": why}
        done.append(f"{j['config']} {j['field']}={'true' if on else 'false'}")
    if t_on is not None and t_on != on:
        why = _set_task(j, on)
        if why:
            return {"status": "refused" if not done else "applied", "job": j["key"], "on": on,
                    "reason": why + (" (the config field did change)" if done else "")}
        done.append(f"task '{j['task']}' {'enabled' if on else 'disabled'}")
    core.log_change("switch", f"{j.get('name', j['key'])} turned {'on' if on else 'off'}: " + "; ".join(done),
                    {"op": "none", "job": j["key"], "was": {"config": c, "task": t}}, by or "owner")
    return {"status": "applied", "job": j["key"], "on": on}


def run_now(key: str, by: str = "") -> dict:
    """Start the job's scheduled task right away. -> {"status": "started" | "refused", "job", "reason"}."""
    j = find(key)
    if not j:
        return {"status": "refused", "reason": f"no switchable job called {key!r}"}
    if not j.get("task"):
        return {"status": "refused", "job": j["key"], "reason": f"{j.get('name', j['key'])} has no scheduled task to start; it runs on its own switch"}
    if config_state(j) is False:
        return {"status": "refused", "job": j["key"], "reason": f"{j.get('name', j['key'])} is switched off in its config; turn it on first"}
    rc, out = _ps(f"Start-ScheduledTask -TaskName {_q(j['task'])} -ErrorAction Stop; (Get-ScheduledTask -TaskName {_q(j['task'])}).State")
    if rc:
        return {"status": "refused", "job": j["key"], "reason": f"the task did not start: {out[-160:]}"}
    core.log_change("switch_run", f"{j.get('name', j['key'])} started by hand (task '{j['task']}')", {"op": "none", "job": j["key"]}, by or "owner")
    return {"status": "started", "job": j["key"], "state": out.splitlines()[-1].strip() if out else ""}


def render() -> str:
    js = jobs()
    if not js:
        return ""
    rows = []
    for j in js:
        s = state(j)
        rows.append(f"- {j['key']}: {j.get('name', j['key'])} ({j.get('what', '')}) is "
                    + ("ON" if s else "OFF" if s is False else "unknown (state unreadable)")
                    + ("; can run now" if j.get("task") else ""))
    return "\n".join(["JOBS YOU CAN SWITCH (turn one on or off with switch {job, on}; start one this minute with switch {job, run: true}):"] + rows)


if __name__ == "__main__":
    print(render() or "no switchable jobs")

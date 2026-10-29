"""deadman.py - the thing that watches the watchers (WP2 of the stand-alone proposal, 2026-10-24).

Every alarm this system had ran INSIDE the thing it watched: the sentinels are a Day Spine job, so a dead
spine reports nothing; the bridge's /health needs the bridge; the comms probe's own log is read by the
spine. Dreaming sat disabled for three months (2026-06 to 09-30), Travel Scout died for 2.5 days behind
an exit-0 wrapper, the heartbeat job failed 13 runs in a row, and nothing said so.

This script is deliberately small and imports NOTHING from assistant/: it reads four files with the
standard library, and when one of the heartbeats is stale it tells the owner through paths it does not
depend on being healthy (a subprocess each, first that works wins):

  1. `cards.py say` (Sage's voice in the home space, and a Mission Control note)
  2. `email_send.py` to the owner's own address (inside the household, inside the gate; off with
     config.json -> deadman.email_fallback = false)
  3. `notify.py` (a dashboard card) + a banner file assistant/data/deadman_alert.json

Watched (local wall clock; each alarm is latched in assistant/data/deadman_state.json and re-raised after
REALERT_H; a recovery is said once):
  spine    assistant/data/spine_state.json      newest `last` older than 45 min inside 06:45-23:30
  bridge   assistant/data/agent/bridge_alive.json  `ts` older than 20 min inside 06:00-23:30 (Sage's heartbeat)
  probe    assistant/data/comms_probe_state.json  `last_wake` older than 40 min inside 07:00-21:30
  disk     the vault's drive under 5 GB free

Scheduled as its own Windows task ("the organization", every 30 min, assistant/tasks/Deadman.xml),
NOT a spine job. Last line: RESULT DEADMAN ok=<n> stale=<names>.

  python assistant/scripts/deadman.py [--dry-run] [--status]
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from datetime import datetime
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
VAULT = ROOT.parent
DATA = Path(os.environ["ASSISTANT_DATA"]) if os.environ.get("ASSISTANT_DATA") else ROOT / "data"
STATE = DATA / "deadman_state.json"
BANNER = DATA / "deadman_alert.json"
CONFIG = ROOT / "config.json"
PEOPLE = ROOT / "agent" / "people.json"
LOG = Path(os.environ.get("WV_LOG_DIR") or (Path.home() / ".claude")) / "deadman-wv.log"
PY = sys.executable

REALERT_H = 6
CHECKS = {
    "spine": {"file": DATA / "spine_state.json", "stale_min": 45, "window": (6.75, 23.5),
              "what": "the Day Spine (approved sends, card sweeps, the lead sensor, the follow-ups)"},
    "bridge": {"file": DATA / "agent" / "bridge_alive.json", "stale_min": 20, "window": (6.0, 23.5),
               "what": "Sage's chat bridge (nothing in Google Chat is being answered)"},
    "probe": {"file": DATA / "comms_probe_state.json", "stale_min": 40, "window": (7.0, 21.5),
              "what": "the comms probe (a new text or email will not wake anything)"},
}
DISK_MIN_GB = 5


def _log(msg: str) -> None:
    line = time.strftime("%Y-%m-%d %H:%M:%S") + "  " + msg
    print(line)
    try:
        LOG.parent.mkdir(parents=True, exist_ok=True)
        with open(LOG, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except OSError:
        pass


def _load(path: Path, default):
    try:
        return json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        return default


def _save(path: Path, obj) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(obj, indent=1, ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, path)
    except OSError as e:
        _log(f"state save failed: {e}")


def _in_window(window, now: datetime) -> bool:
    h = now.hour + now.minute / 60
    return window[0] <= h < window[1]


def _newest_epoch(name: str, obj) -> float:
    """The heartbeat's newest timestamp, by file shape (each store writes its own)."""
    if name == "spine" and isinstance(obj, dict):
        best = 0.0
        for st in obj.values():
            if isinstance(st, dict) and st.get("last"):
                try:
                    best = max(best, time.mktime(time.strptime(str(st["last"])[:19], "%Y-%m-%dT%H:%M:%S")))
                except ValueError:
                    pass
        return best
    if name == "bridge" and isinstance(obj, dict):
        try:
            return float(obj.get("ts") or 0)
        except (TypeError, ValueError):
            return 0.0
    if name == "probe" and isinstance(obj, dict):
        try:
            return max(float(obj.get("last_wake") or 0), float(obj.get("last_tick") or 0))
        except (TypeError, ValueError):
            return 0.0
    return 0.0


def check_all(now: datetime | None = None) -> dict:
    """-> {name: {"ok": bool, "age_min": float, "why": str}} for every watched heartbeat, plus disk."""
    now = now or datetime.now()
    out = {}
    for name, c in CHECKS.items():
        obj = _load(c["file"], None)
        if obj is None:
            out[name] = {"ok": not _in_window(c["window"], now), "age_min": None, "why": f"{c['file'].name} is missing"}
            continue
        newest = _newest_epoch(name, obj)
        age_min = (now.timestamp() - newest) / 60 if newest else None
        stale = age_min is None or age_min > c["stale_min"]
        out[name] = {"ok": not (stale and _in_window(c["window"], now)), "age_min": round(age_min, 1) if age_min is not None else None,
                     "why": "" if not stale else f"last heartbeat {age_min:.0f} min ago" if age_min is not None else "no heartbeat on record"}
    try:
        free_gb = shutil.disk_usage(str(VAULT)).free / 1e9
        out["disk"] = {"ok": free_gb >= DISK_MIN_GB, "age_min": None, "why": "" if free_gb >= DISK_MIN_GB else f"{free_gb:.1f} GB free on the vault drive"}
    except OSError:
        out["disk"] = {"ok": True, "age_min": None, "why": ""}
    return out


# ------------------------------------------------------------------ telling the owner, three ways
def _owner_email() -> str:
    p = _load(PEOPLE, {}) or {}
    for u in (p.get("users") or {}).values():
        if isinstance(u, dict) and u.get("role") == "owner" and u.get("emails"):
            return str(u["emails"][0])
    return ""


def _from_address() -> str:
    cfg = _load(CONFIG, {}) or {}
    for acct in (cfg.get("mail", {}).get("accounts") or []):
        if isinstance(acct, dict) and acct.get("address"):
            return str(acct["address"])
    return _owner_email()


def _run(cmd: list, timeout: int = 60, stdin_text: str = "") -> tuple:
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=timeout,
                           cwd=str(VAULT), input=stdin_text or None)
        return r.returncode == 0, ((r.stdout or "") + (r.stderr or "")).strip()[-300:]
    except (subprocess.TimeoutExpired, OSError) as e:
        return False, f"{type(e).__name__}"


def tell(text: str, dry: bool = False) -> str:
    """-> which path carried it ('chat' | 'email' | 'banner' | 'dry')."""
    if dry:
        _log("WOULD TELL: " + text)
        return "dry"
    ok, out = _run([PY, str(HERE / "cards.py"), "say", "--lane", "personal", "--owner", "deadman", text], timeout=90)
    if ok and '"said": true' in out and '"chat": 0' not in out:
        return "chat"
    _log(f"chat path failed ({out[-120:]}); trying email")
    cfg = _load(CONFIG, {}) or {}
    if (cfg.get("deadman") or {}).get("email_fallback", True):
        to, frm = _owner_email(), _from_address()
        if to and frm:
            with tempfile.NamedTemporaryFile("w", suffix=".txt", delete=False, encoding="utf-8") as f:
                f.write(text + "\n\n(The deadman watcher sends this only when the chat path is down too.)")
                body = f.name
            try:
                ok, out = _run([PY, str(HERE / "email_send.py"), "--from", frm, "--to", to, "--subject", "example assistant: something stopped", "--body-file", body], timeout=120)
            finally:
                try:
                    Path(body).unlink()
                except OSError:
                    pass
            if ok and "SENT=1" in out:
                return "email"
            _log(f"email path failed ({out[-120:]}); banner only")
    _run([PY, str(HERE / "notify.py")], timeout=30, stdin_text=text)
    _save(BANNER, {"ts": time.strftime("%Y-%m-%dT%H:%M:%S"), "text": text})
    return "banner"


def _muted(cls: str) -> bool:
    """A class of code-posted lines an owner switched off (assistant/agent/reflex.py keeps data/agent/reflex.json);
    read with the standard library only, as everything here is."""
    agent_data = Path(os.environ["AGENT_DATA"]) if os.environ.get("AGENT_DATA") else DATA / "agent"
    d = _load(agent_data / "reflex.json", {}) or {}
    return isinstance(d, dict) and cls in (d.get("muted") or {})


# ------------------------------------------------------------------ the tick
FIX_GAP_MIN = 20
# what the deadman tries by itself before a word reaches the owner (2026-10-25)
FIXES = {
    "probe": ["schtasks", "/run", "/tn", "example-org"],
    "bridge": [PY, str(VAULT / "assistant" / "agent" / "bridge.py"), "--ensure"],
    "spine": ["schtasks", "/run", "/tn", "example-org"],
}


def tick(dry: bool = False, now: datetime | None = None) -> dict:
    now = now or datetime.now()
    st = _load(STATE, {}) or {}
    alerted = st.get("alerted") or {}
    res = check_all(now)
    stale = [n for n, r in res.items() if not r["ok"]]
    said = []
    fixing = st.get("fixing") or {}
    for name in stale:
        last = float(alerted.get(name) or 0)
        if now.timestamp() - last < REALERT_H * 3600:
            continue
        # the owner 2026-10-24 17:40 / 21:32: "I don't want to see messages like this. I just want you to fix it."
        # First a stale heartbeat gets its fix run quietly; the owner hears only when it is STILL stale on the
        # next tick after a fix was tried (and on a tick inside the window).
        fix = FIXES.get(name)
        if os.environ.get("ASSISTANT_DATA") or os.environ.get("DEADMAN_NO_FIX"):
            fix = None            # a scratch-store test must never start a LIVE scheduled task (2026-10-25: a test did)
        if fix and not dry and float(fixing.get(name) or 0) < now.timestamp() - FIX_GAP_MIN * 60:
            ok_fix, out_fix = _run(fix, timeout=120)
            _log(f"auto-fix {name}: {'ok' if ok_fix else 'failed'} {out_fix[-120:]}")
            fixing[name] = now.timestamp()
            continue
        if fix and float(fixing.get(name) or 0) > now.timestamp() - FIX_GAP_MIN * 60 * 3 and name not in st.get("fix_failed", {}):
            # the fix ran within the last few ticks; give it one tick to show a heartbeat before telling anyone
            st.setdefault("fix_failed", {})[name] = now.timestamp()
            continue
        what = CHECKS.get(name, {}).get("what", name)
        text = (f"Heads up: {what} has stopped: {res[name]['why']}. Nothing on that path is happening until it is back. "
                f"Open Setup on Mission Control (System) to restart it, or say 'run the day spine' / 'restart the bridge' here.")
        if name == "disk":
            text = f"Heads up: {res[name]['why']}; sends and ingests will start failing. Free some space on that drive."
        how = tell(text, dry)
        said.append({"check": name, "via": how})
        if not dry:
            alerted[name] = now.timestamp()
    for name in list(alerted):
        if name not in stale and res.get(name, {}).get("ok"):
            line = f"{CHECKS.get(name, {}).get('what', name).split(' (')[0].capitalize()} is back; the gap was {((now.timestamp() - float(alerted[name])) / 60):.0f} min."
            if _muted("system_status"):
                _log("recovered quietly (system-status lines are switched off): " + line)
                how = "quiet"
            else:
                how = tell(line, dry)
            said.append({"check": name, "via": how, "recovered": True})
            if not dry:
                alerted.pop(name, None)
    for name in list(fixing):
        if res.get(name, {}).get("ok"):
            fixing.pop(name, None)
            (st.get("fix_failed") or {}).pop(name, None)
    if not dry:
        _save(STATE, {"alerted": alerted, "fixing": fixing, "fix_failed": st.get("fix_failed") or {},
                      "last_run": now.strftime("%Y-%m-%dT%H:%M:%S"), "last": res})
        if not stale:
            try:
                BANNER.unlink(missing_ok=True)
            except OSError:
                pass
    return {"ok": [n for n in res if res[n]["ok"]], "stale": stale, "said": said}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--status", action="store_true")
    a = ap.parse_args()
    if a.status:
        print(json.dumps(check_all(), indent=1))
        return 0
    r = tick(dry=a.dry_run)
    _log(f"RESULT DEADMAN ok={len(r['ok'])} stale={','.join(r['stale']) or '-'} said={len(r['said'])}")
    return 0


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    sys.exit(main())

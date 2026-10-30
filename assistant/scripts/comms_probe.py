"""comms_probe.py - the one-minute, zero-LLM wake: a human message lands, the operator runs.

Ported in shape from the property OS's scripts/comms/probe.py (blueprint D6: "wake <= 1 min
after an inbound lands"). Before this the operator only ran on its half-hour tick, so a text
or a lead could sit 29 minutes before anyone drafted a reply.

Every minute (scheduled task "example-org Comms Probe (assistant)", outside the harness so the
operator it starts can authenticate):
  1. rebuild the comms substrate (comms_state.py: states, SLAs, breach tickets)
  2. look for claimable tickets of the wake kinds (comms_law.json -> wake.kinds) younger than
     wake.max_age_min that this probe has not already woken for
  3. if there are any, inside the window, no operator in flight, and at least min_gap_min
     since the last wake: start operator_cron.ps1 -Comms detached and remember the ids

It never sends, never drafts, never calls a model. ALWAYS exits 0.

  python assistant/scripts/comms_probe.py            # probe + wake
  python assistant/scripts/comms_probe.py --dry-run  # say what it would do
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
from alib import DATA, VAULT, load_json, now_iso, save_json  # noqa: E402

STATE = DATA / "comms_probe_state.json"
LOG = Path.home() / ".claude" / "comms-probe-wv.log"
RUNNING = DATA / ".operator_running"          # written by operator_cron.ps1 while a run is in flight
RUNNING_STALE_MIN = 25


def log(msg: str) -> None:
    try:
        LOG.parent.mkdir(parents=True, exist_ok=True)
        with open(LOG, "a", encoding="utf-8") as f:
            f.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')}  {msg}\n")
    except OSError:
        pass


def _in_window(window) -> bool:
    try:
        a, b = window
        now = time.strftime("%H:%M")
        return a <= now <= b
    except Exception:  # noqa: BLE001
        return True


def operator_running() -> bool:
    try:
        return RUNNING.exists() and time.time() - RUNNING.stat().st_mtime < RUNNING_STALE_MIN * 60
    except OSError:
        return False


def burst_settling(wake: dict, work: list, now: float | None = None) -> bool:
    """the owner 2026-10-07: 'in group chats with lots of texts coming in at once, stall the drafter'.
    When `burst_texts` or more texts are waiting and the newest is younger than `settle_min`, hold
    the wake: the thread is still moving, and one run after it goes quiet answers what is actually
    open instead of drafting a reply per text. A lone text still wakes within the minute. Anything
    that is not a text (an email, a lead, a tap) is never held."""
    import tickets
    now = now or time.time()
    texts = [t for t in work if t.get("kind") == "imessage_in"]
    if len(texts) < int(wake.get("burst_texts", 3)):
        return False
    if any(t.get("kind") != "imessage_in" for t in work):
        return False
    # a group chat is several PEOPLE; one friend sending four lines is a conversation, and the
    # operator already folds that into one card. 2026-10-07 17:23-17:32 a single friend's texts
    # (plus a Google code) were held 12 minutes under the first version of this rule.
    senders = {str((t.get("payload") or {}).get("from") or "") for t in texts
               if re.fullmatch(r"\+?\d{7,15}", str((t.get("payload") or {}).get("from") or ""))}
    if len(senders) < int(wake.get("burst_senders", 2)):
        return False
    oldest = max(tickets._age_s(t.get("created", "")) for t in texts)
    if oldest >= float(wake.get("settle_max_min", 15)) * 60:
        return False                                 # a thread that never goes quiet still gets its run
    newest = min(tickets._age_s(t.get("created", "")) for t in texts)
    return newest < float(wake.get("settle_min", 8)) * 60


def pretriage(work: list) -> list:
    """Zero-LLM triage before any wake (the property OS's ack gate, ported 2026-10-08): a text that
    is only an acknowledgment or a tapback ("Thank youuuu", "Liked an image", a thumbs-up) is
    dismissed by code with a note, so the drafter is never woken for it and never has to judge
    it. Returns the work that still needs a human-grade look. Anything with an attachment, a
    number or real words passes through untouched."""
    import tickets
    sys.path.insert(0, str(HERE.parent / "agent"))
    import ack
    keep = []
    for t in work:
        p = t.get("payload") or {}
        if t.get("kind") == "imessage_in" and ack.is_pure_ack(str(p.get("text") or "")):
            if tickets._transition(t["id"], "dismissed", "probe", "ack or tapback only (zero-LLM pretriage)", tickets.OPEN):
                log(f"pretriage dismissed {t['id']}: {str(p.get('text') or '')[:40]!r}")
                continue
        keep.append(t)
    return keep


def new_work(wake: dict, seen: dict) -> list:
    import tickets
    kinds, lanes = set(wake.get("kinds") or []), set(wake.get("lanes") or [])
    max_age = float(wake.get("max_age_min", 30)) * 60
    out = []
    for t in tickets._load().values():
        if not isinstance(t, dict) or t.get("kind") not in kinds or (lanes and t.get("lane") not in lanes):
            continue
        if not tickets._claimable(t) or t["id"] in seen:
            continue
        if tickets._age_s(t.get("created", "")) > max_age:
            continue
        out.append(t)
    return out


def wake(dry: bool, extra: str = "-Comms") -> str:
    cron = VAULT / "assistant" / "scripts" / "operator_cron.ps1"
    if dry:
        return f"DRY_RUN would start operator_cron.ps1 {extra}"
    # CREATE_NO_WINDOW + NEW_PROCESS_GROUP only. DETACHED_PROCESS (first version, 2026-10-06 13:29)
    # gives PowerShell no console at all and it exits before the script runs: the probe logged
    # WOKE and nothing happened. A hidden window is what the scheduled tasks use too.
    flags = 0x00000200 | 0x08000000 if os.name == "nt" else 0
    subprocess.Popen(["powershell.exe", "-NoProfile", "-WindowStyle", "Hidden", "-ExecutionPolicy", "Bypass",
                      "-File", str(cron), extra], cwd=str(VAULT), creationflags=flags, close_fds=True,
                     stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    return f"WOKE operator_cron.ps1 {extra}"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args()
    try:
        import comms_state
        st = comms_state.build()
        if not a.dry_run:
            tmp = comms_state.STATE.with_suffix(".tmp")
            save_json(tmp, st)
            tmp.replace(comms_state.STATE)
            comms_state.breach_tickets(st)
        wake_cfg = comms_state.law().get("wake") or {}
    except Exception as e:  # noqa: BLE001
        log(f"substrate failed: {type(e).__name__}: {str(e)[:160]}")
        wake_cfg, st = {}, {"counts": {}}
    if not a.dry_run:
        try:   # a text the phone never fetched gets another wake-up SMS (phone_nudge.renudge_if_waiting, 2026-10-24)
            import phone_nudge
            if phone_nudge.renudge_if_waiting():
                log("re-nudged the phone: a text was still waiting to be fetched")
        except Exception as e:  # noqa: BLE001
            log(f"re-nudge check failed: {type(e).__name__}: {str(e)[:120]}")
    probe = load_json(STATE, {"woken": {}, "last_wake": 0}) or {}
    seen = probe.get("woken") or {}
    if not a.dry_run:
        # the heartbeat deadman.py watches (2026-10-25): every run, not only a run that woke the operator.
        # last_wake alone went stale for 13 h while the probe ran every minute, and the owner was told four
        # times that the probe had "stopped".
        probe["last_tick"] = time.time()
        probe["last_tick_at"] = now_iso()
        save_json(STATE, probe)
    work = new_work(wake_cfg, seen)
    if not a.dry_run:
        work = pretriage(work)
    why = ""
    if not work:
        why = "no new human work"
    elif not _in_window(wake_cfg.get("window") or ["00:00", "23:59"]):
        why = "outside the wake window (the half-hour tick still runs)"
    elif operator_running():
        why = "operator already in flight"
    elif time.time() - float(probe.get("last_wake", 0)) < float(wake_cfg.get("min_gap_min", 3)) * 60:
        why = "woke less than min_gap_min ago"
    elif burst_settling(wake_cfg, work):
        why = "text burst still settling (a group chat; one run once it goes quiet)"
    if why:
        line = f"new={len(work)} {why} | " + " ".join(f"{k}={v}" for k, v in (st.get("counts") or {}).items())
        if work or st.get("counts", {}).get("breached"):
            log(line)
        print("PROBE " + line)
        return 0
    res = wake(a.dry_run)
    if not a.dry_run:
        cutoff = time.time() - 6 * 3600
        seen = {k: v for k, v in seen.items() if float(v) > cutoff}
        for t in work:
            seen[t["id"]] = time.time()
        save_json(STATE, {"woken": seen, "last_wake": time.time(), "last_wake_at": now_iso(),
                          "last_titles": [t.get("title", "")[:80] for t in work][:6]})
    line = f"{res} for {len(work)}: " + "; ".join(f"[{t.get('lane')}] {t.get('kind')} {t.get('title', '')[:50]}" for t in work[:4])
    log(line)
    print("PROBE " + line)
    return 0


if __name__ == "__main__":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:  # noqa: BLE001
        pass
    try:
        sys.exit(main())
    except Exception as e:  # noqa: BLE001   a probe must never go red
        log(f"CRASH {type(e).__name__}: {str(e)[:200]}")
        print(f"PROBE crashed: {type(e).__name__}")
        sys.exit(0)

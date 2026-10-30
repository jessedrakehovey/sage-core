"""profile_lease.py — a durable, cooperative cross-process lease for a single
Chromium *persistent profile* directory.

WHY THIS EXISTS
---------------
A Chromium persistent profile (``--user-data-dir`` / ``launch_persistent_context``)
can only be opened by ONE live Chrome at a time. (Copied from the property Vault,
where example/Google Voice executors contend for their own profiles.) On the wedding
side the contenders are:

  * the wedding ``playwright-cli`` MCP (local-scope override)  -> ``pw-wv-cli-profile``
  * the CRM connector's own browser automation (if it has one)   -> ``pw-wv-cli-profile``

Wedding code must never lease a property profile; the ~/.claude/boundary guard blocks it.

Before this module the contenders either crashed ("Browser is already in use") or
HARD-KILLED each other's Chrome to steal the profile mid-run. This lease replaces
"kill and steal" with "wait your turn, and only break a lock whose holder is
actually dead."

DESIGN (deliberately boring + crash-safe)
-----------------------------------------
* One lock file per profile, in a SIDECAR dir (``~/.claude/pw-leases/``) so Chrome
  itself never touches it: ``<key>.lock`` holding JSON ``{pid,label,acquired,heartbeat}``.
* Mutual exclusion via an atomic ``O_CREAT|O_EXCL`` create — only one creator wins.
* The holder runs a background HEARTBEAT thread that rewrites ``heartbeat`` every
  ``HEARTBEAT_S`` seconds, proving it is alive even during a long (e.g. lease-fill) hold.
* A waiter breaks an existing lock ONLY when it is provably abandoned:
    - the holder PID is no longer alive (crash-safety; covers hard-kills), or
    - the heartbeat is older than ``STALE_TTL_S`` (backstop for a wedged-but-alive
      holder), which is set generously so legitimate long holds are never stolen.
* The lease is released on explicit ``release()``, on normal interpreter exit
  (``atexit``), and — implicitly — by death: a dead holder's PID fails the liveness
  check so the next waiter reclaims it. No daemon, no registry, no single point of
  failure.

PID liveness is done with raw Win32 (ctypes) so there is NO third-party dependency
(``psutil`` is not installed in the chat-wo venv). A POSIX fallback keeps the module
importable/testable on non-Windows.

Typical use (see ``launch_exec_browser`` in wo_create.py):

    from profile_lease import acquire_profile_lease
    acquire_profile_lease(profile_dir, label="wo_create")   # blocks politely, then returns
    ctx = p.chromium.launch_persistent_context(str(profile_dir), headless=False)
    ...                                                      # lease auto-releases at exit

or scoped:

    with profile_lease(profile_dir, label="harvest"):
        ctx = p.chromium.launch_persistent_context(...)
        ...
"""
from __future__ import annotations

import atexit
import contextlib
import json
import os
import sys
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

# --- tunables -------------------------------------------------------------------
LEASE_DIR = Path.home() / ".claude" / "pw-leases"
HEARTBEAT_S = 20.0          # how often the holder refreshes its heartbeat
STALE_TTL_S = 600.0         # break an alive-but-silent holder after this (10 min)
DEFAULT_TIMEOUT_S = 150.0   # how long a waiter blocks before logging that it is still waiting
POLL_S = 1.0                # waiter poll interval
_BORN_GRACE_S = 5.0         # tolerate an empty/just-created lock file this long


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _parse_iso(s: str) -> float:
    """ISO -> epoch seconds; returns 0.0 on any parse failure (=> treated as ancient)."""
    try:
        return datetime.strptime(s, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc).timestamp()
    except Exception:
        return 0.0


# --- PID liveness (Win32 native, psutil-free) -----------------------------------
def pid_alive(pid: int) -> bool:
    """True if a process with this PID currently exists. Conservative: on any
    ambiguity it returns True (we would rather wait than wrongly steal a lease)."""
    if not pid or pid < 0:
        return False
    if os.name == "nt":
        import ctypes
        from ctypes import wintypes

        k32 = ctypes.windll.kernel32
        SYNCHRONIZE = 0x00100000
        PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
        STILL_ACTIVE = 259
        ERROR_INVALID_PARAMETER = 87  # no such pid
        ERROR_ACCESS_DENIED = 5       # exists, but we can't open it -> alive

        handle = k32.OpenProcess(SYNCHRONIZE | PROCESS_QUERY_LIMITED_INFORMATION,
                                 False, wintypes.DWORD(pid))
        if not handle:
            return k32.GetLastError() == ERROR_ACCESS_DENIED
        try:
            code = wintypes.DWORD()
            if k32.GetExitCodeProcess(handle, ctypes.byref(code)):
                return code.value == STILL_ACTIVE
            return True  # couldn't read exit code -> assume alive
        finally:
            k32.CloseHandle(handle)
    else:  # POSIX fallback (tests / non-Windows)
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return False
        except PermissionError:
            return True
        return True


def _lock_path(profile) -> Path:
    """Map a profile dir to its sidecar lock file. Key = profile basename + a short
    hash of the full path, so two different profiles that share a basename never alias."""
    p = Path(profile)
    full = str(p.resolve()) if p.exists() else str(p)
    h = 0
    for ch in full.lower():
        h = (h * 131 + ord(ch)) & 0xFFFFFFFF
    return LEASE_DIR / f"{p.name}-{h:08x}.lock"


def _read_lease(lock: Path):
    try:
        raw = lock.read_text(encoding="utf-8")
    except FileNotFoundError:
        return None
    except Exception:
        return {}  # exists but unreadable -> let caller decide via age
    if not raw.strip():
        return {}
    try:
        return json.loads(raw)
    except Exception:
        return {}


def _write_payload(fd: int, label: str):
    payload = json.dumps({
        "pid": os.getpid(),
        "label": label,
        "acquired": _now_iso(),
        "heartbeat": _now_iso(),
    })
    os.write(fd, payload.encode("utf-8"))
    with contextlib.suppress(Exception):
        os.fsync(fd)


# --- the lease handle -----------------------------------------------------------
class _Lease:
    def __init__(self, lock: Path, label: str):
        self.lock = lock
        self.label = label
        self._released = False
        self._stop = threading.Event()
        self._beat_thread = threading.Thread(target=self._beat, name=f"lease-beat-{lock.name}",
                                    daemon=True)
        self._beat_thread.start()

    def _beat(self):
        while not self._stop.wait(HEARTBEAT_S):
            self.refresh()

    def refresh(self):
        if self._released:
            return
        data = _read_lease(self.lock)
        if not data or data.get("pid") != os.getpid():
            return  # someone else owns it now; stop pretending
        data["heartbeat"] = _now_iso()
        tmp = self.lock.with_suffix(".lock.tmp")
        try:
            tmp.write_text(json.dumps(data), encoding="utf-8")
            os.replace(tmp, self.lock)
        except Exception:
            with contextlib.suppress(Exception):
                tmp.unlink()

    def release(self):
        if self._released:
            return
        self._released = True
        self._stop.set()
        data = _read_lease(self.lock)
        if data and data.get("pid") == os.getpid():
            with contextlib.suppress(Exception):
                self.lock.unlink()
        _REGISTRY.pop(str(self.lock), None)

    # context-manager sugar
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.release()
        return False


# in-process registry so a re-entrant acquire on the same profile is a no-op
_REGISTRY: dict[str, _Lease] = {}
_REG_LOCK = threading.Lock()


def acquire_profile_lease(profile, label: str = "", timeout: float = DEFAULT_TIMEOUT_S,
                          stale_ttl: float = STALE_TTL_S) -> _Lease:
    """Block until this process owns the lease for ``profile``, then return a handle.

    Politely waits for a live holder (up to ``timeout``). Breaks a lock only when its
    holder is dead or its heartbeat is older than ``stale_ttl``; a live holder is never
    broken (``timeout`` only triggers one loud log line). Re-entrant within a process.
    Never raises for lock-infra problems: on a truly unexpected error it logs and
    returns a best-effort handle so the caller is never worse off than before."""
    lock = _lock_path(profile)
    key = str(lock)

    with _REG_LOCK:
        held = _REGISTRY.get(key)
        if held and not held._released:
            return held  # already ours in this process

    LEASE_DIR.mkdir(parents=True, exist_ok=True)
    deadline = time.time() + max(1.0, timeout)
    warned = False

    while True:
        # 1) try to create the lock atomically — the happy path.
        try:
            fd = os.open(str(lock), os.O_CREAT | os.O_EXCL | os.O_RDWR, 0o644)
        except FileExistsError:
            fd = None
        except Exception as e:
            _log(f"[lease] open error on {lock.name}: {type(e).__name__}: {e}; proceeding unguarded")
            lease = _Lease(lock, label)  # best effort; do not block the caller forever
            with _REG_LOCK:
                _REGISTRY[key] = lease
            return lease

        if fd is not None:
            try:
                _write_payload(fd, label)
            finally:
                os.close(fd)
            lease = _Lease(lock, label)
            with _REG_LOCK:
                _REGISTRY[key] = lease
            return lease

        # 2) lock exists — decide whether to wait or break it.
        data = _read_lease(lock)
        reason = _break_reason(lock, data, stale_ttl)
        if reason:
            _log(f"[lease] breaking {lock.name} ({reason}); reclaiming")
            _force_remove(lock)
            continue

        if time.time() >= deadline and not warned:
            # Never steal from a LIVE holder (2026-10-02: a keepalive broke the owner's
            # interactive CRM sign-in lease after 150s and its reaper closed his sign-in
            # window). Keep waiting; _break_reason frees it on death or stale heartbeat.
            holder = (data or {}).get("pid", "?")
            _log(f"[lease] still waiting on {lock.name} after {timeout:.0f}s "
                 f"(live holder pid={holder}, label={(data or {}).get('label')}); "
                 f"NOT breaking a live lease -- waiting for it to finish or go stale "
                 f"({stale_ttl:.0f}s)")
            warned = True

        time.sleep(POLL_S)


def _break_reason(lock: Path, data, stale_ttl: float):
    """Return a human reason string if the existing lock is safe to break, else None."""
    if data is None:
        return "vanished"  # file disappeared between create-fail and read
    if not data:  # empty / unparseable
        try:
            age = time.time() - lock.stat().st_mtime
        except Exception:
            return "unreadable"
        return "stale-empty" if age > _BORN_GRACE_S else None
    pid = data.get("pid")
    if not isinstance(pid, int):
        return "no-pid"
    if pid == os.getpid():
        return "self"  # leftover from a prior run of THIS pid (rare) -> reclaim
    if not pid_alive(pid):
        return f"holder-dead(pid={pid})"
    beat_age = time.time() - _parse_iso(data.get("heartbeat", ""))
    if beat_age > stale_ttl:
        return f"heartbeat-stale({beat_age:.0f}s>pid={pid})"
    return None


def _force_remove(lock: Path):
    with contextlib.suppress(Exception):
        lock.unlink()


def release_profile_lease(profile):
    """Release this process's lease on ``profile`` early, if held."""
    lease = _REGISTRY.get(str(_lock_path(profile)))
    if lease:
        lease.release()


@contextlib.contextmanager
def profile_lease(profile, label: str = "", timeout: float = DEFAULT_TIMEOUT_S):
    """Scoped lease: ``with profile_lease(dir, 'harvest'): ...`` — releases on exit."""
    lease = acquire_profile_lease(profile, label=label, timeout=timeout)
    try:
        yield lease
    finally:
        lease.release()


def _log(msg: str):
    line = f"{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}  {msg}"
    try:
        (Path.home() / ".claude" / "pw-lease.log").open("a", encoding="utf-8").write(line + "\n")
    except Exception:
        pass
    print(line, file=sys.stderr)


@atexit.register
def _release_all():
    for lease in list(_REGISTRY.values()):
        with contextlib.suppress(Exception):
            lease.release()


# --- self-test / manual helpers -------------------------------------------------
if __name__ == "__main__":
    import argparse

    ap = argparse.ArgumentParser(description="profile_lease self-test / manual hold")
    sub = ap.add_subparsers(dest="cmd")
    h = sub.add_parser("hold", help="acquire + hold a lease (for cross-process testing)")
    h.add_argument("profile")
    h.add_argument("seconds", type=float)
    h.add_argument("--label", default="manual-hold")
    sub.add_parser("selftest", help="run the in-process correctness checks")
    args = ap.parse_args()

    if args.cmd == "hold":
        acquire_profile_lease(args.profile, label=args.label)
        print(f"HELD {args.profile} for {args.seconds}s (pid {os.getpid()})")
        time.sleep(args.seconds)
        release_profile_lease(args.profile)
        print("RELEASED")
    elif args.cmd == "selftest":
        import tempfile

        ok = True
        tmp = Path(tempfile.mkdtemp()) / "fake-profile"

        # 1) basic acquire/release + re-entrancy
        l1 = acquire_profile_lease(tmp, label="t1", timeout=5)
        l2 = acquire_profile_lease(tmp, label="t1", timeout=5)
        ok &= (l1 is l2)
        print(f"[1] re-entrant same handle: {'OK' if l1 is l2 else 'FAIL'}")
        lp = _lock_path(tmp)
        ok &= lp.exists()
        print(f"[2] lock file created: {'OK' if lp.exists() else 'FAIL'}")
        l1.release()
        ok &= (not lp.exists())
        print(f"[3] lock file removed on release: {'OK' if not lp.exists() else 'FAIL'}")

        # 4) dead-holder lock is broken
        LEASE_DIR.mkdir(parents=True, exist_ok=True)
        dead_pid = 999999999  # implausible pid
        lp.write_text(json.dumps({"pid": dead_pid, "label": "ghost",
                                  "acquired": _now_iso(), "heartbeat": _now_iso()}),
                      encoding="utf-8")
        t0 = time.time()
        l3 = acquire_profile_lease(tmp, label="reclaim", timeout=10)
        broke_fast = (time.time() - t0) < 3
        owns = _read_lease(lp).get("pid") == os.getpid()
        ok &= broke_fast and owns
        print(f"[4] reclaimed dead holder fast+owned: {'OK' if broke_fast and owns else 'FAIL'}")
        l3.release()

        # 5) liveness: our own pid is alive, an absurd pid is not
        live = pid_alive(os.getpid())
        dead = pid_alive(dead_pid)
        ok &= live and not dead
        print(f"[5] pid_alive(self)={live} pid_alive(ghost)={dead}: "
              f"{'OK' if live and not dead else 'FAIL'}")

        print("SELFTEST", "PASS" if ok else "FAIL")
        sys.exit(0 if ok else 1)
    else:
        ap.print_help()

"""browser.py - the doer's ONE browser: a persistent Chromium profile opened with a CDP port.

It STAYS UP (2026-11-14). A job, a script or a sign-in check attaches to the one that is running; when it is done the
browser is parked (one blank tab, out of sight), never quit, so no window opens and closes around every job. Two
places it can be, switched by moving the window, never by a restart: the FOREGROUND (on the main monitor, in front)
and the BACKGROUND (off the screen and, on Windows, without a taskbar button; nothing it does there takes the
keyboard from a person working). config.json -> doer.mode: auto | background | foreground; doer.keep_open.

Why one shared browser: the model drives pages through the Playwright MCP, and login_fill.py
types passwords into the SAME pages from a separate process the model cannot see. Both connect
to this browser over CDP (http://127.0.0.1:<port>). The profile is the wedding `playwright-cli`
profile (~/.claude/pw-wv-cli-profile), so every site the owner has signed into there stays signed
in here, and vice versa. The profile lease (scripts/lib/profile_lease.py) keeps this launcher,
the MCP and the CRM automation from opening the profile at the same time.

  python assistant/doer/browser.py --open          start it (off-screen unless --show), print the CDP url
  python assistant/doer/browser.py --show          start it ON screen for the owner to sign in to a site
  python assistant/doer/browser.py --front         bring the RUNNING browser on screen, maximized, in front (no restart)
  python assistant/doer/browser.py --back          put the RUNNING browser out of sight (no restart; it keeps working there)
  python assistant/doer/browser.py --park          what a finished job does: one blank tab, out of sight, still up
  python assistant/doer/browser.py --close         really quit it (jobs no longer do: the browser stays up between them)
  python assistant/doer/browser.py --prune          close every leftover tab but one
  python assistant/doer/browser.py --status
  python assistant/doer/browser.py --probe-profile  one line for the connection check
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
VAULT = ROOT.parent
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(VAULT / "scripts" / "lib"))
from alib import load_json  # noqa: E402
sys.path.insert(0, str(ROOT))
import ports  # noqa: E402  (port guard: dial our own browser where it really is; never attach to another program's)

CFG = (load_json(ROOT / "config.json", {}) or {}).get("doer") or {}
PROFILE = Path(os.path.expanduser(os.environ.get("DOER_PROFILE") or CFG.get("profile") or "~/.claude/pw-wv-cli-profile"))
PORT = ports.current("cdp", int(os.environ.get("DOER_CDP_PORT") or CFG.get("cdp_port") or 9333))
STATE = Path(os.environ["DOER_DATA"]) / "browser.json" if os.environ.get("DOER_DATA") else ROOT / "data" / "doer" / "browser.json"
LEFT_OPEN = STATE.with_name("left_open.json")      # pages held open for an owner (leftopen.py, 2026-11-08)
UPLOADS = STATE.with_name("uploads.json")          # tabs with an upload running (upload.py, 2026-11-10)
WAITS = STATE.with_name("waits.json")              # pages waiting on an owner's sign-in check (2026-11-11)
WAIT_HOLD_MIN = int(CFG.get("wait_hold_min", 30))
CHROME_CANDIDATES = [
    Path(os.path.expandvars(r"%LOCALAPPDATA%\ms-playwright")),
]
LAUNCH_ARGS = ["--no-first-run", "--no-default-browser-check", "--disable-blink-features=AutomationControlled",
               # ONE --disable-features (Chrome keeps only the last one). CalculateNativeWinOcclusion: Windows otherwise
               # tells Chrome an off-screen / covered / locked-screen window is hidden and it stops painting, so the
               # live card's picture came back blank or black (2026-11-08)
               "--disable-features=FedCm,CalculateNativeWinOcclusion", "--window-size=1366,900", "--disable-session-crashed-bubble", "--hide-crash-restore-bubble",
               # an off-screen window keeps rendering, so the live card's CDP picture is never a stale frame (2026-11-03)
               "--disable-backgrounding-occluded-windows", "--disable-renderer-backgrounding"]
OFFSCREEN = ["--window-position=-32000,-32000"]
NO_WINDOW = 0x08000000 if os.name == "nt" else 0
# In front by default (the owner 2026-11-06: "bring to front should be standard for Sage PC work unless someone is working
# on the computer in the last 5 minutes and needs a clear screen"): the window opens on the main monitor unless a
# person touched the mouse or keyboard within clear_screen_idle_s; then it stays off-screen (Control PC still shows it).
IN_FRONT = bool(CFG.get("in_front", True))
CLEAR_SCREEN_IDLE_S = int(CFG.get("clear_screen_idle_s", 300))
# ONE browser that stays up (the owner 2026-11-14: "stop relaunching browsers when running new scripts. I don't like seeing
# browsers open and close over and over"). A job that is done no longer quits it: close_browser() PARKS it (one blank
# tab, out of sight) and the next job, script or sign-in check attaches to the same one. It quits only when told to
# (`--close`, close_browser(quit=True)) or with config.json -> doer.keep_open false (the old way: quit after every job).
KEEP_OPEN = bool(CFG.get("keep_open", True))
# Where a job's window is: "auto" (in front unless someone is at the PC: the 2026-11-06 rule), "background" (never in
# front by itself; Control PC and a sign-in still show it), "foreground" (always in front). Without doer.mode the older
# doer.in_front decides: true = auto, false = background. Either way the switch is a window move, never a restart.
MODE = str(CFG.get("mode") or ("auto" if IN_FRONT else "background")).lower()
# In the background the window is not merely off the screen: on Windows it has no taskbar button either (_taskbar). It
# keeps working there (pages load, clicks land, pictures are taken: measured 2026-11-14) and nothing it does there
# takes the keyboard from the window a person is typing in.
HIDE_IN_BACKGROUND = bool(CFG.get("hide_in_background", True))
JOB_LOCK = STATE.with_name(".doer.lock")           # doer.py's one-job-at-a-time lock: {pid, ts}


def idle_seconds() -> float:
    """Seconds since the last mouse or key on this PC (Windows' own count); a very large number when it cannot be read."""
    if os.name != "nt":
        return 1e9
    try:
        import ctypes
        from ctypes import wintypes

        class LASTINPUTINFO(ctypes.Structure):
            _fields_ = [("cbSize", wintypes.UINT), ("dwTime", wintypes.DWORD)]
        li = LASTINPUTINFO()
        li.cbSize = ctypes.sizeof(LASTINPUTINFO)
        if not ctypes.windll.user32.GetLastInputInfo(ctypes.byref(li)):
            return 1e9
        return max(0.0, (ctypes.windll.kernel32.GetTickCount() - li.dwTime) / 1000.0)
    except Exception:  # noqa: BLE001
        return 1e9


def someone_at_the_pc(within_s: float | None = None) -> bool:
    """True when a person used the mouse or keyboard within `within_s` (default config doer.clear_screen_idle_s)."""
    return idle_seconds() < float(CLEAR_SCREEN_IDLE_S if within_s is None else within_s)


def want_front() -> bool:
    """The default for a job's window, by doer.mode: foreground = in front; background = never; auto = in front
    unless someone is at the PC right now."""
    if MODE == "foreground":
        return True
    if MODE == "background":
        return False
    return not someone_at_the_pc()


def _pid_alive(pid) -> bool:
    try:
        pid = int(pid or 0)
    except (TypeError, ValueError):
        return False
    if pid <= 0:
        return False
    if os.name != "nt":
        try:
            os.kill(pid, 0)
            return True
        except OSError:
            return False
    try:
        import ctypes
        k = ctypes.windll.kernel32
        h = k.OpenProcess(0x1000, False, pid)             # PROCESS_QUERY_LIMITED_INFORMATION
        if not h:
            return False
        code = ctypes.c_ulong()
        ok = k.GetExitCodeProcess(h, ctypes.byref(code))
        k.CloseHandle(h)
        return bool(ok) and code.value == 259             # STILL_ACTIVE
    except Exception:  # noqa: BLE001
        return False


def job_running() -> int:
    """The pid of the browser job that is running right now (doer.py's lock, held by a live process); 0 when none."""
    try:
        st = json.loads(JOB_LOCK.read_text(encoding="utf-8")) if JOB_LOCK.is_file() else {}
    except Exception:  # noqa: BLE001
        st = {}
    pid = int((st or {}).get("pid") or 0) if isinstance(st, dict) else 0
    return pid if _pid_alive(pid) else 0


def in_use() -> bool:
    """Is someone working in the browser right now? A browser that is merely up is not: it stays up between jobs
    (KEEP_OPEN), so "is it running" no longer means "is it busy". Busy = a browser job is running, or a page is held
    open for an owner."""
    return bool(alive()) and bool(job_running() or held_tabs())


def cdp_url() -> str:
    return f"http://127.0.0.1:{PORT}"


def chrome_exe() -> Path | None:
    """The browser to start: doer.chrome (or DOER_CHROME) when set, else Playwright's Chromium when it is installed,
    else the Chrome or the Edge this PC already has. With the engine's own driver (browser_native.py) nothing has to
    be downloaded: every Windows PC ships a Chromium browser (Edge), and any of them speaks the same protocol."""
    named = os.environ.get("DOER_CHROME") or CFG.get("chrome")
    if named:
        p = Path(os.path.expandvars(named))
        if p.exists():
            return p
    for root in CHROME_CANDIDATES:
        if not root.exists():
            continue
        hits = sorted(root.glob("chromium-*/chrome-win*/chrome.exe"), reverse=True)
        if hits:
            return hits[0]
    for p in (Path(os.path.expandvars(r"%ProgramFiles%\Google\Chrome\Application\chrome.exe")),
              Path(os.path.expandvars(r"%ProgramFiles(x86)%\Google\Chrome\Application\chrome.exe")),
              Path(os.path.expandvars(r"%ProgramFiles(x86)%\Microsoft\Edge\Application\msedge.exe")),
              Path(os.path.expandvars(r"%ProgramFiles%\Microsoft\Edge\Application\msedge.exe"))):
        if p.exists():
            return p
    return None


def alive(timeout_s: float = 1.5) -> dict | None:
    try:
        with urllib.request.urlopen(cdp_url() + "/json/version", timeout=timeout_s) as r:
            return json.loads(r.read().decode("utf-8", "replace"))
    except Exception:
        return None


def held_tabs() -> set:
    """Tab ids of pages left open for an owner at a step the agent could not finish (leftopen.py, 2026-11-08: "leave
    the browser open for the user to do it themself unless the user selects ... do later"). Never pruned, and while
    any is held the browser is not quit (close_browser without force)."""
    try:
        d = json.loads(LEFT_OPEN.read_text(encoding="utf-8")) if LEFT_OPEN.is_file() else {}
    except Exception:  # noqa: BLE001
        d = {}
    held = {str(e.get("target") or "") for e in (d or {}).values()
            if isinstance(e, dict) and e.get("state") == "open" and e.get("target")}
    # a tab with an upload still running (upload.py refreshes `until` while it watches; 2026-11-10)
    try:
        up = json.loads(UPLOADS.read_text(encoding="utf-8")) if UPLOADS.is_file() else {}
    except Exception:  # noqa: BLE001
        up = {}
    now = time.time()
    held |= {str(k) for k, v in (up or {}).items() if isinstance(v, dict) and float(v.get("until") or 0) > now}
    # a page waiting on an owner's sign-in check: their tap or code answers THIS page (hold_wait)
    return held | {str(v.get("target")) for v in _waits().values() if v.get("target") and float(v.get("until") or 0) > now}


def _waits() -> dict:
    try:
        d = json.loads(WAITS.read_text(encoding="utf-8")) if WAITS.is_file() else {}
    except Exception:  # noqa: BLE001
        d = {}
    return {str(k): v for k, v in (d or {}).items() if isinstance(v, dict)}


def _save_waits(d: dict) -> None:
    try:
        WAITS.parent.mkdir(parents=True, exist_ok=True)
        WAITS.write_text(json.dumps(d, indent=1), encoding="utf-8")
    except OSError:
        pass


def hold_wait(tid: str, page: dict | None = None, minutes: float | None = None, now: float | None = None) -> dict:
    """The job stopped on a page that waits for its owner (a tap on their phone, a code): keep THAT page. A sign-in
    check answers the page that asked for it; a reload or a fresh sign-in sends a new one, so the owner's tap lands
    on a prompt nobody is waiting on any more (2026-11-10: five hours and six taps for one link, "you're stuck in a
    loop, just keep the same browser open"). While held the tab is never pruned and the browser never quit, by this
    job or any other. -> the entry, {} when there is no real page to hold."""
    page = active_page() if page is None else page
    url = str((page or {}).get("url") or "")
    if not tid or not page or not page.get("id") or url.startswith(("about:", "chrome:", "devtools:")):
        return {}
    now = now or time.time()
    e = {"target": str(page["id"]), "url": url[:300], "epoch": now,
         "until": now + 60 * float(WAIT_HOLD_MIN if minutes is None else minutes)}
    d = _waits()
    d[str(tid)] = e
    _save_waits(d)
    return e


def waiting(tid: str = "", except_tid: str = "", now: float | None = None) -> dict:
    """With `tid`: that job's held page ({} when none, or its time ran out). Without: every live one but `except_tid`."""
    now = now or time.time()
    live = {k: v for k, v in _waits().items() if float(v.get("until") or 0) > now}
    if tid:
        return live.get(str(tid)) or {}
    return {k: v for k, v in live.items() if k != str(except_tid)}


def release_wait(tid: str, before: float | None = None) -> bool:
    """The job moved on: its page is an ordinary tab again. With `before`, only an entry made before that moment goes
    (a run that stopped on a NEW wait keeps the one it just made)."""
    d = _waits()
    e = d.get(str(tid))
    if not e or (before is not None and float(e.get("epoch") or 0) >= before):
        return False
    d.pop(str(tid), None)
    _save_waits(d)
    return True


def expire_waits(now: float | None = None) -> list:
    """Drop holds whose time ran out. -> the job ids dropped (the caller closes the browser if nothing else needs it)."""
    now = now or time.time()
    d = _waits()
    gone = [k for k, v in d.items() if float(v.get("until") or 0) <= now]
    if gone:
        _save_waits({k: v for k, v in d.items() if k not in gone})
    return gone


def prune_tabs(keep: int = 1) -> int:
    """Close every page but `keep`, over plain HTTP (/json/close), so it can never hang.

    Why (2026-10-13): each run left its tabs open and the browser was ended by a hard kill, which
    Chromium treats as a crash and answers by restoring every tab. By 10/12 it reopened 60 tabs on
    each start; the Playwright MCP attaches to all of them, timed out three times, and the doer
    reported "the browser never finishes starting". The same pile made the polite Browser.close
    time out, so the hard kill ran again (and a hard kill drops cookies not yet flushed to disk,
    i.e. a fresh sign-in). Returns how many tabs were closed."""
    try:
        with urllib.request.urlopen(cdp_url() + "/json/list", timeout=5) as r:
            pages = [t for t in json.loads(r.read().decode("utf-8", "replace")) if t.get("type") == "page"]
    except Exception:
        return 0
    # a page held open for an owner is never closed here, and does not count toward `keep`
    held = held_tabs()
    pages = [t for t in pages if str(t.get("id") or "") not in held]
    # keep a real page over about:blank when there is one (a sign-in window may be mid-flow)
    pages.sort(key=lambda t: t.get("url", "").startswith("about:"))
    closed = 0
    for t in pages[keep:]:
        try:
            urllib.request.urlopen(cdp_url() + "/json/close/" + t["id"], timeout=5).read()
            closed += 1
        except Exception:
            pass
    return closed


def _cdp_browser_close() -> bool:
    """Send Browser.close on the browser's own CDP websocket. Stdlib only: no Playwright, no page
    attach, so it works with any interpreter and any number of tabs. Chromium then exits cleanly
    (exit_type Normal, cookie store flushed) instead of being killed."""
    import base64
    import socket
    from urllib.parse import urlparse
    v = alive()
    ws = (v or {}).get("webSocketDebuggerUrl") or ""
    if not ws:
        return False
    u = urlparse(ws)
    try:
        sock = socket.create_connection((u.hostname, u.port or 80), timeout=5)
        key = base64.b64encode(os.urandom(16)).decode()
        crlf = chr(13) + chr(10)
        sock.sendall(crlf.join([f"GET {u.path} HTTP/1.1", f"Host: {u.hostname}:{u.port}", "Upgrade: websocket",
                                "Connection: Upgrade", f"Sec-WebSocket-Key: {key}", "Sec-WebSocket-Version: 13",
                                "", ""]).encode())
        if b" 101 " not in sock.recv(4096).split(crlf.encode(), 1)[0]:
            sock.close()
            return False
        payload = json.dumps({"id": 1, "method": "Browser.close"}).encode()
        mask = os.urandom(4)
        n = len(payload)
        head = bytes([0x81]) + (bytes([0x80 | n]) if n < 126 else bytes([0x80 | 126]) + n.to_bytes(2, "big"))
        sock.sendall(head + mask + bytes(b ^ mask[i % 4] for i, b in enumerate(payload)))
        try:
            sock.recv(1024)
        except OSError:
            pass
        sock.close()
        return True
    except OSError:
        return False


def _keep_session_cookies() -> None:
    """Set "continue where you left off" (session.restore_on_startup = 1) while the browser is down.
    Chromium only keeps session-only sign-in cookies across a restart under that setting. Until
    2026-10-13 every close was a hard kill, and crash recovery kept them by accident; now that the
    close is clean, this keeps them on purpose. prune_tabs() keeps the restore down to one tab."""
    prefs = PROFILE / "Default" / "Preferences"
    try:
        d = json.loads(prefs.read_text(encoding="utf-8"))
    except Exception:
        return
    if (d.get("session") or {}).get("restore_on_startup") == 1:
        return
    d.setdefault("session", {})["restore_on_startup"] = 1
    try:
        tmp = prefs.with_suffix(".tmp")
        tmp.write_text(json.dumps(d, separators=(",", ":")), encoding="utf-8")
        os.replace(tmp, prefs)
    except OSError:
        pass


def _state() -> dict:
    return load_json(STATE, {}) or {}


def _save_state(d: dict) -> None:
    STATE.parent.mkdir(parents=True, exist_ok=True)
    STATE.write_text(json.dumps(d, indent=1), encoding="utf-8")


def _lease():
    """Hold the profile lease while the browser lives. Returns the lease object or None."""
    try:
        from profile_lease import acquire_profile_lease
        return acquire_profile_lease(PROFILE, label="doer-browser")
    except Exception as e:  # noqa: BLE001
        print(f"lease unavailable ({type(e).__name__}); continuing without it", file=sys.stderr)
        return None


def open_browser(show: bool | None = None, wait_s: float = 25.0) -> str:
    """Start the browser if it is not up; return the CDP url. Idempotent. `show` None = the default (want_front):
    in front unless someone is at the PC; True = in front, whatever; False = off-screen."""
    global PORT
    if alive() and not ports.ours("cdp", PORT):
        # another program's browser (another agent's doer, a stray Chrome) answers on our port: move, never attach to it
        PORT, _how = ports.claim("cdp", PORT)
    front_default = show is None and want_front()
    if alive():
        # the one that is up is the one everybody uses: it is never restarted to change where its window is
        # (until 2026-11-14 asking for it on screen while it was hidden quit it and started a new one)
        st = _state()
        if st.get("parked"):
            st.pop("parked", None)
            _save_state(st)
        prune_tabs()
        if (show or front_default) and st.get("offscreen", True):
            ok = False
            try:
                ok, _detail = show_on_screen()
            except Exception:  # noqa: BLE001
                pass
            if show and not ok and not held_tabs() and not job_running():
                close_browser(quit=True)                  # the window would not come on screen: only then, a fresh one
            else:
                return cdp_url()
        else:
            return cdp_url()
    show = bool(show) or front_default
    exe = chrome_exe()
    if not exe:
        raise RuntimeError("no Chromium found: `python -m playwright install chromium` or set doer.chrome in assistant/config.json")
    PROFILE.mkdir(parents=True, exist_ok=True)
    lease = _lease()
    _keep_session_cookies()
    PORT, _how = ports.claim("cdp", PORT)                 # a non-browser program on the port moves us too
    args = [str(exe), f"--remote-debugging-port={PORT}", f"--user-data-dir={PROFILE}"] + LAUNCH_ARGS
    offscreen = not show and bool(CFG.get("offscreen", True))
    if offscreen:
        args += OFFSCREEN
    args.append("about:blank")
    front_before = _foreground()
    p = subprocess.Popen(args, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                         creationflags=NO_WINDOW, close_fds=True)
    t0 = time.time()
    while time.time() - t0 < wait_s:
        if alive():
            prune_tabs()
            _save_state({"pid": p.pid, "port": PORT, "offscreen": offscreen, "started": time.strftime("%Y-%m-%dT%H:%M:%S"),
                         "lease": bool(lease), "profile": str(PROFILE)})
            if offscreen:
                for _ in range(10):                       # the window is a moment behind the port
                    if _windows(p.pid):
                        break
                    time.sleep(0.2)
                if HIDE_IN_BACKGROUND:
                    _taskbar(p.pid, False)
                _give_back_focus(front_before, p.pid)     # a start takes the keyboard; a background start gives it back
            if lease:
                # the lease lives in THIS process; keep a sidecar pid so --close can release it
                try:
                    lease.refresh()
                except Exception:
                    pass
            return cdp_url()
        if p.poll() is not None:
            break
        time.sleep(0.4)
    raise RuntimeError("the browser did not answer on its CDP port (another Chrome may hold the profile; close it or free the lease)")


def park() -> bool:
    """A job is done with the browser: it stays up, out of sight, on one blank tab, signed in and ready for the next
    one. No window closes and none opens. Pages held open for an owner are left exactly as they are (and then the
    window is not moved either). False when the browser is not running."""
    if not alive():
        return False
    held = held_tabs()
    try:
        with urllib.request.urlopen(cdp_url() + "/json/list", timeout=5) as r:
            pages = [t for t in json.loads(r.read().decode("utf-8", "replace")) if t.get("type") == "page"]
    except Exception:  # noqa: BLE001
        pages = []
    blank = ""
    try:                                              # the tab that stays: a blank one, so no site sits loaded for hours
        req = urllib.request.Request(cdp_url() + "/json/new?about:blank", method="PUT")
        with urllib.request.urlopen(req, timeout=8) as resp:
            blank = str(json.loads(resp.read().decode("utf-8", "replace")).get("id") or "")
    except Exception:  # noqa: BLE001
        pass
    if blank:
        for t in pages:
            if str(t.get("id") or "") in held:
                continue
            try:
                urllib.request.urlopen(cdp_url() + "/json/close/" + t["id"], timeout=5).read()
            except Exception:  # noqa: BLE001
                pass
    else:
        prune_tabs()
    if not held:
        try:
            send_to_back()
        except Exception:  # noqa: BLE001
            pass
    st = _state()
    if st:
        st["parked"] = time.strftime("%Y-%m-%dT%H:%M:%S")
        _save_state(st)
    return True


def close_browser(force: bool = False, quit: bool = False) -> bool:
    """A caller is done with the browser. With KEEP_OPEN (the default) that PARKS it (park(): stays up, out of sight,
    one blank tab) and only `quit=True` (`--close`, an owner's word) really ends it; with keep_open off it quits as it
    always did: Browser.close over CDP, then make sure its pid is gone. While a page is held open for an owner
    (held_tabs) nothing happens and this returns False; `force` is the owner's Do later (leftopen.later). A caller
    that is not the running browser job never parks or quits under that job (2026-11-14: a warm-up or a sign-in check
    that finished mid-job used to quit the job's browser)."""
    if not force and held_tabs() and alive():
        return False
    if not force and not quit:
        running = job_running()
        if running and running != os.getpid():
            return False
    if KEEP_OPEN and not quit:
        return park()
    st = _state()
    ok = False
    if alive():
        prune_tabs()   # one tab left, so the next start restores nothing stale
        ok = _cdp_browser_close()
    pid = int(st.get("pid") or 0)
    t0 = time.time()
    # a clean quit writes the cookie store to disk; give it time before any hard kill
    while alive() and time.time() - t0 < 20:
        time.sleep(0.3)
    if alive() and pid:
        subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"], capture_output=True, creationflags=NO_WINDOW)
        ok = True
    if pid and not alive():
        ok = True
    if STATE.exists():
        try:
            STATE.unlink()
        except OSError:
            pass
    return ok


def _cdp_calls(ws: str, calls: list) -> list:
    """Run CDP calls [(method, params), ...] on a websocket url, in order; returns each result (or
    {"error": ...}). Stdlib only, like _cdp_browser_close."""
    import base64
    import socket
    from urllib.parse import urlparse
    u = urlparse(ws)
    sock = socket.create_connection((u.hostname, u.port or 80), timeout=8)
    crlf = chr(13) + chr(10)
    sock.sendall(crlf.join([f"GET {u.path} HTTP/1.1", f"Host: {u.hostname}:{u.port}", "Upgrade: websocket",
                            "Connection: Upgrade", f"Sec-WebSocket-Key: {base64.b64encode(os.urandom(16)).decode()}",
                            "Sec-WebSocket-Version: 13", "", ""]).encode())
    buf = b""
    while b"\r\n\r\n" not in buf:
        chunk = sock.recv(4096)
        if not chunk:
            raise OSError("websocket closed during the handshake")
        buf += chunk
    head, buf = buf.split(b"\r\n\r\n", 1)
    if b" 101 " not in head.split(b"\r\n", 1)[0]:
        sock.close()
        raise OSError("websocket refused")

    def need(n: int) -> bytes:
        nonlocal buf
        while len(buf) < n:
            chunk = sock.recv(65536)
            if not chunk:
                raise OSError("websocket closed")
            buf += chunk
        out, buf = buf[:n], buf[n:]
        return out

    out = []
    try:
        for i, (method, params) in enumerate(calls, 1):
            if callable(params):                      # params built from the answers so far (same session: upload.py)
                params = params(out)
            payload = json.dumps({"id": i, "method": method, "params": params or {}}).encode()
            mask, n = os.urandom(4), len(payload)
            head = bytes([0x81]) + (bytes([0x80 | n]) if n < 126 else bytes([0x80 | 126]) + n.to_bytes(2, "big"))
            sock.sendall(head + mask + bytes(b ^ mask[j % 4] for j, b in enumerate(payload)))
            while True:                               # skip events until our id answers
                b0, b1 = need(2)
                ln = b1 & 0x7F
                ln = int.from_bytes(need(2), "big") if ln == 126 else int.from_bytes(need(8), "big") if ln == 127 else ln
                data = need(ln)
                if b0 & 0x0F != 1:
                    continue
                msg = json.loads(data.decode("utf-8", "replace"))
                if msg.get("id") == i:
                    out.append(msg.get("result") if "result" in msg else {"error": msg.get("error")})
                    break
    finally:
        sock.close()
    return out


def _screen() -> tuple:
    """(width, height) of the main monitor; (0, 0) when it cannot be read."""
    try:
        import ctypes
        return ctypes.windll.user32.GetSystemMetrics(0), ctypes.windll.user32.GetSystemMetrics(1)
    except Exception:  # noqa: BLE001
        return 0, 0


def on_main_monitor(bounds: dict, screen: tuple) -> bool:
    """A window a person can see: not minimized, and most of it on the main monitor (0,0 .. w,h)."""
    if not bounds or bounds.get("windowState") == "minimized" or not all(screen):
        return False
    if bounds.get("windowState") in ("maximized", "fullscreen") and abs(int(bounds.get("left", 0))) < 64 and abs(int(bounds.get("top", 0))) < 64:
        return True
    l, t = int(bounds.get("left", 0)), int(bounds.get("top", 0))
    w, h = int(bounds.get("width", 0)), int(bounds.get("height", 0))
    ow = max(0, min(l + w, screen[0]) - max(l, 0))
    oh = max(0, min(t + h, screen[1]) - max(t, 0))
    return w > 0 and h > 0 and ow * oh >= 0.6 * w * h


def _windows(pid: int) -> list:
    """Windows: the browser's own top-level windows (Chrome's window class, a title, no owner), shown or hidden."""
    if os.name != "nt" or not pid:
        return []
    try:
        import ctypes
        from ctypes import wintypes
        u = ctypes.windll.user32
        found = []

        @ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
        def _each(hwnd, _):
            p = wintypes.DWORD()
            u.GetWindowThreadProcessId(hwnd, ctypes.byref(p))
            if p.value == pid and u.GetWindowTextLengthW(hwnd) and not u.GetWindow(hwnd, 4):
                cls = ctypes.create_unicode_buffer(64)
                u.GetClassNameW(hwnd, cls, 64)
                if cls.value == "Chrome_WidgetWin_1":
                    found.append(hwnd)
            return True

        u.EnumWindows(_each, 0)
        return found
    except Exception:  # noqa: BLE001
        return []


def _taskbar(pid: int, show: bool) -> int:
    """Take the browser's windows off the taskbar and out of Alt-Tab (show=False) or put them back (show=True), on
    Windows, by the tool-window style. The window itself stays a real, painting window: a window that is HIDDEN
    outright stops painting and a picture of its page can hang (measured 2026-11-14: one of two timed out), while one
    that is off the screen without a taskbar button took every picture. The change needs the window hidden for an
    instant; it comes back WITHOUT taking the keyboard (SW_SHOWNOACTIVATE). -> how many windows; 0 off Windows."""
    hw = _windows(pid)
    if not hw:
        return 0
    import ctypes
    u = ctypes.windll.user32
    u.GetWindowLongPtrW.restype = ctypes.c_ssize_t
    u.GetWindowLongPtrW.argtypes = [ctypes.c_void_p, ctypes.c_int]
    u.SetWindowLongPtrW.restype = ctypes.c_ssize_t
    u.SetWindowLongPtrW.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_ssize_t]
    for h in hw:
        ex = u.GetWindowLongPtrW(h, -20)                              # GWL_EXSTYLE
        new = ((ex & ~0x80) | 0x40000) if show else ((ex | 0x80) & ~0x40000)   # WS_EX_TOOLWINDOW / WS_EX_APPWINDOW
        if new != ex:
            u.ShowWindow(h, 0)
            u.SetWindowLongPtrW(h, -20, new)
            u.ShowWindow(h, 4)                                        # SW_SHOWNOACTIVATE
    return len(hw)


def hidden() -> bool:
    """True when the browser is up and none of its windows has a taskbar button (the background of HIDE_IN_BACKGROUND)."""
    if os.name != "nt":
        return False
    hw = _windows(int(_state().get("pid") or 0))
    if not hw:
        return False
    import ctypes
    u = ctypes.windll.user32
    u.GetWindowLongPtrW.restype = ctypes.c_ssize_t
    u.GetWindowLongPtrW.argtypes = [ctypes.c_void_p, ctypes.c_int]
    return all(u.GetWindowLongPtrW(h, -20) & 0x80 for h in hw)


def _foreground() -> int:
    """Windows: the window a person is typing in right now (0 elsewhere)."""
    if os.name != "nt":
        return 0
    try:
        import ctypes
        return int(ctypes.windll.user32.GetForegroundWindow() or 0)
    except Exception:  # noqa: BLE001
        return 0


def _give_back_focus(prev: int, pid: int) -> bool:
    """A browser STARTING takes the keyboard from whatever a person was typing in (measured 2026-11-14; nothing it
    does afterwards in the background does). When ours just did, hand the keyboard back to the window that had it."""
    if os.name != "nt" or not prev or not pid:
        return False
    try:
        import ctypes
        from ctypes import wintypes
        u = ctypes.windll.user32
        now = u.GetForegroundWindow()
        if not now or now == prev:
            return False
        p = wintypes.DWORD()
        u.GetWindowThreadProcessId(now, ctypes.byref(p))
        if p.value != pid or not u.IsWindow(prev):
            return False                              # someone else is in front (the person switched): not ours to undo
        return bool(u.SetForegroundWindow(prev))
    except Exception:  # noqa: BLE001
        return False


def send_to_back() -> tuple:
    """Put the running browser out of sight WITHOUT a restart: every window off the screen and, on Windows,
    without a taskbar button. Whatever is open in it stays open and keeps working. -> (ok, detail)."""
    v = alive()
    ws = str((v or {}).get("webSocketDebuggerUrl") or "")
    if not ws:
        return False, "the browser is not running"
    try:
        with urllib.request.urlopen(cdp_url() + "/json/list", timeout=5) as r:
            pages = [t for t in json.loads(r.read().decode("utf-8", "replace")) if t.get("type") == "page"]
    except Exception as e:  # noqa: BLE001
        return False, f"could not list the browser's tabs ({type(e).__name__})"
    moved = 0
    try:
        wins = _cdp_calls(ws, [("Browser.getWindowForTarget", {"targetId": t["id"]}) for t in pages[:30]])
        wids = sorted({int((w or {}).get("windowId") or 0) for w in wins} - {0})
        calls = []
        for wid in wids:
            calls += [("Browser.setWindowBounds", {"windowId": wid, "bounds": {"windowState": "normal"}}),
                      ("Browser.setWindowBounds", {"windowId": wid, "bounds": {"left": -32000, "top": -32000}})]
        if calls:
            _cdp_calls(ws, calls)
        moved = len(wids)
    except (OSError, ValueError) as e:
        return False, f"could not move the window ({type(e).__name__})"
    st = _state()
    hid = _taskbar(int(st.get("pid") or 0), False) if HIDE_IN_BACKGROUND else 0
    if st:
        st["offscreen"] = True
        _save_state(st)
    return True, f"{moved} window(s) off the screen" + (", no taskbar button" if hid else "")


def _raise_window(pid: int) -> str:
    """Windows: maximize the browser's top-level window and put it in front. Best effort; returns
    'minimized' / 'hidden' / '' (fine) from the OS's own read-back."""
    if os.name != "nt" or not pid:
        return ""
    import ctypes
    from ctypes import wintypes
    u = ctypes.windll.user32
    found = []

    @ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    def _each(hwnd, _):
        p = wintypes.DWORD()
        u.GetWindowThreadProcessId(hwnd, ctypes.byref(p))
        if p.value == pid and u.IsWindowVisible(hwnd) and u.GetWindowTextLengthW(hwnd) and not u.GetWindow(hwnd, 4):
            found.append(hwnd)
        return True

    u.EnumWindows(_each, 0)
    if not found:
        return ""                                     # CDP's read-back decides then
    hwnd = found[0]
    u.ShowWindow(hwnd, 3)                             # SW_MAXIMIZE
    u.SetWindowPos(hwnd, -1, 0, 0, 0, 0, 0x0001 | 0x0002)   # topmost for a moment, so it is in front even if focus is refused
    u.SetWindowPos(hwnd, -2, 0, 0, 0, 0, 0x0001 | 0x0002)
    u.SetForegroundWindow(hwnd)
    if u.IsIconic(hwnd):
        return "minimized"
    return "" if u.IsWindowVisible(hwnd) else "hidden"


def show_on_screen() -> tuple:
    """Bring the running browser to the owner's main monitor, maximized and in front, WITHOUT a restart
    (a cart or a half-filled form lives in this session). Returns (ok, detail); ok only when code
    read back a window that is not minimized and sits on the main monitor (2026-10-28: the
    Lensrentals cart was left at checkout off-screen and the owner could not see it)."""
    if not alive():
        return False, "the browser is not running"
    try:
        with urllib.request.urlopen(cdp_url() + "/json/list", timeout=5) as r:
            pages = [t for t in json.loads(r.read().decode("utf-8", "replace")) if t.get("type") == "page"]
    except Exception as e:  # noqa: BLE001
        return False, f"could not list the browser's tabs ({type(e).__name__})"
    if not pages:
        return False, "the browser has no open page"
    page = active_page(pages) or pages[0]
    ws = str((alive() or {}).get("webSocketDebuggerUrl") or "")
    sw, sh = _screen()
    _taskbar(int(_state().get("pid") or 0), True)         # in front it is an ordinary window again, taskbar button and all
    try:
        win = _cdp_calls(ws, [("Browser.getWindowForTarget", {"targetId": page["id"]})])[0]
        wid = int((win or {}).get("windowId") or 0)
        if not wid:
            return False, "could not find the browser's window"
        _cdp_calls(ws, [("Browser.setWindowBounds", {"windowId": wid, "bounds": {"windowState": "normal"}}),
                        ("Browser.setWindowBounds", {"windowId": wid, "bounds": {"left": 0, "top": 0,
                                                     "width": min(1366, sw or 1366), "height": min(900, sh or 900)}}),
                        ("Browser.setWindowBounds", {"windowId": wid, "bounds": {"windowState": "maximized"}})])
        try:
            urllib.request.urlopen(cdp_url() + "/json/activate/" + page["id"], timeout=5).read()
        except Exception:  # noqa: BLE001
            pass
        os_says = _raise_window(int(_state().get("pid") or 0))
        time.sleep(0.5)
        bounds = (_cdp_calls(ws, [("Browser.getWindowBounds", {"windowId": wid})])[0] or {}).get("bounds") or {}
    except (OSError, ValueError) as e:
        return False, f"could not move the window ({type(e).__name__})"
    st = _state()
    if st:
        st["offscreen"] = False
        _save_state(st)
    if os_says:
        return False, f"Windows reports the window {os_says}"
    if not on_main_monitor(bounds, (sw, sh)):
        return False, f"the window did not land on the main monitor ({bounds.get('windowState', '?')} at {bounds.get('left')},{bounds.get('top')})"
    return True, f"maximized on the main monitor, showing {page.get('url', '')[:120]}"


def _page_focus(ws: str) -> tuple:
    """(visible, focused) of one page, asked of the page itself. (False, False) when it does not answer."""
    try:
        r = _cdp_calls(ws, [("Runtime.evaluate", {"expression": "document.visibilityState + '|' + document.hasFocus()",
                                                  "returnByValue": True})])[0] or {}
        v = str(((r.get("result") or {}).get("value")) or "")
        vis, _, foc = v.partition("|")
        return vis == "visible", foc == "true"
    except Exception:  # noqa: BLE001
        return False, False


def active_page(pages: list | None = None, focus=None) -> dict:
    """The page the job is working on: the tab that is visible AND focused, else a visible one, else the first real
    page (2026-11-06, the owner: "sometimes sage sends screenshots of pages it is not working on" - the first tab in
    Chrome's list is not the one in front once the job opened a second tab). `focus` = a stand-in for _page_focus
    in tests. {} when there is no page."""
    if pages is None:
        if not alive():
            return {}
        try:
            with urllib.request.urlopen(cdp_url() + "/json/list", timeout=5) as r:
                pages = [t for t in json.loads(r.read().decode("utf-8", "replace")) if t.get("type") == "page"]
        except Exception:  # noqa: BLE001
            return {}
    real = [p for p in pages if not str(p.get("url", "")).startswith(("about:", "chrome:", "devtools:", "chrome-extension:"))]
    pool = real or list(pages)
    if not pool:
        return {}
    if len(pool) == 1:
        return pool[0]
    focus = focus or _page_focus
    visible, focused = [], []
    for p in pool[:12]:
        vis, foc = focus(str(p.get("webSocketDebuggerUrl") or ""))
        if vis and foc:
            focused.append(p)
        elif vis:
            visible.append(p)
    return (focused or visible or pool)[0]


def front_page_ws() -> str:
    """The CDP websocket of the page the job is on ('' when the browser is down or has no page)."""
    return str(active_page().get("webSocketDebuggerUrl") or "")


def ready_to_paint(page: dict) -> str:
    """Make sure the window holding `page` can paint before a picture is taken (2026-11-08, the owner: "control images are
    showing blank or black"). A minimized window paints nothing and Page.captureScreenshot just hangs, so it is
    restored: in front when nobody is at the PC (the in-front rule), else off-screen, so it never jumps in front of
    a person working. -> '' (already fine) | 'front' | 'offscreen' | 'unknown'."""
    v = alive()
    bws = str((v or {}).get("webSocketDebuggerUrl") or "")
    if not bws or not page.get("id"):
        return "unknown"
    try:
        wid = int((_cdp_calls(bws, [("Browser.getWindowForTarget", {"targetId": page["id"]})])[0] or {}).get("windowId") or 0)
        if not wid:
            return "unknown"
        b = (_cdp_calls(bws, [("Browser.getWindowBounds", {"windowId": wid})])[0] or {}).get("bounds") or {}
        if b.get("windowState") != "minimized":
            return ""
        if want_front():
            ok, _ = show_on_screen()
            if ok:
                return "front"
        _cdp_calls(bws, [("Browser.setWindowBounds", {"windowId": wid, "bounds": {"windowState": "normal"}}),
                         ("Browser.setWindowBounds", {"windowId": wid, "bounds": {"left": -32000, "top": -32000}})])
        st = _state()
        if st:
            st["offscreen"] = True
            _save_state(st)
        time.sleep(0.4)                               # one frame to paint
        return "offscreen"
    except (OSError, ValueError):
        return "unknown"


def navigate(url: str) -> bool:
    """Send the front page to `url` (the owner's own sign-in, 2026-11-03). False when the browser is down."""
    ws = front_page_ws()
    if not ws or not str(url).startswith(("http://", "https://")):
        return False
    try:
        r = _cdp_calls(ws, [("Page.navigate", {"url": str(url)})])[0] or {}
        return not (isinstance(r, dict) and r.get("error"))
    except (OSError, ValueError):
        return False


def status() -> dict:
    v = alive()
    st = _state()
    return {"up": bool(v), "cdp": cdp_url() if v else "", "browser": (v or {}).get("Browser", ""), "offscreen": st.get("offscreen"),
            "pid": st.get("pid"), "profile": str(PROFILE), "in_front_default": IN_FRONT, "idle_s": round(idle_seconds(), 1),
            "someone_at_the_pc": someone_at_the_pc(), "mode": MODE, "keep_open": KEEP_OPEN, "hidden": hidden() if v else False,
            "in_use": in_use() if v else False, "parked": st.get("parked") if v else None}


def probe_profile() -> str:
    """conn_check (cmd_output): a line when the profile exists and has a cookie store."""
    cookies = PROFILE / "Default" / "Network" / "Cookies"
    if not cookies.exists():
        cookies = PROFILE / "Default" / "Cookies"
    if not PROFILE.exists():
        return ""
    age_d = (time.time() - cookies.stat().st_mtime) / 86400 if cookies.exists() else None
    return f"profile {PROFILE.name}" + (f", cookies touched {age_d:.0f}d ago" if age_d is not None else ", no cookies yet (sign in once with --show)")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--open", action="store_true")
    ap.add_argument("--show", action="store_true", help="start on screen so owner1 can sign in to a site")
    ap.add_argument("--front", action="store_true", help="bring the running browser on screen, maximized, in front")
    ap.add_argument("--back", action="store_true", help="put the running browser out of sight, no restart")
    ap.add_argument("--park", action="store_true", help="one blank tab, out of sight, still up")
    ap.add_argument("--close", action="store_true", help="really quit the browser")
    ap.add_argument("--prune", action="store_true", help="close every leftover tab but one")
    ap.add_argument("--status", action="store_true")
    ap.add_argument("--probe-profile", action="store_true")
    a = ap.parse_args()
    if a.probe_profile:
        line = probe_profile()
        print(line) if line else None
        return 0
    if a.prune:
        print(f"closed {prune_tabs()} tab(s)" if alive() else "not running")
        return 0
    if a.front:
        ok, detail = show_on_screen()
        print(("ON_SCREEN " if ok else "NOT_ON_SCREEN ") + detail)
        return 0 if ok else 1
    if a.back:
        ok, detail = send_to_back()
        print(("IN_BACKGROUND " if ok else "NOT_MOVED ") + detail)
        return 0 if ok else 1
    if a.park:
        print("parked" if park() else "not running")
        return 0
    if a.close:
        print("closed" if close_browser(quit=True) else "was not running")
        return 0
    if a.open or a.show:
        try:
            print(open_browser(show=a.show))
        except RuntimeError as e:
            print(f"FAILED {e}")
            return 1
        if a.show:
            print(f"The agent's browser is on screen. Sign in to the site you need, then close the window (or run --close).\nProfile: {PROFILE}")
            # hold the profile lease for as long as the window is open, so the doer does not start a run into it
            try:
                while alive():
                    time.sleep(2)
            except KeyboardInterrupt:
                close_browser(quit=True)
        return 0
    print(json.dumps(status(), indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main())

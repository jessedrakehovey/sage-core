"""browser.py - the doer's ONE browser: a persistent Chromium profile opened with a CDP port.

Why one shared browser: the model drives pages through the Playwright MCP, and login_fill.py
types passwords into the SAME pages from a separate process the model cannot see. Both connect
to this browser over CDP (http://127.0.0.1:<port>). The profile is the wedding `playwright-cli`
profile (~/.claude/pw-wv-cli-profile), so every site the owner has signed into there stays signed
in here, and vice versa. The profile lease (scripts/lib/profile_lease.py) keeps this launcher,
the MCP and the CRM automation from opening the profile at the same time.

  python assistant/doer/browser.py --open          start it (off-screen unless --show), print the CDP url
  python assistant/doer/browser.py --show          start it ON screen for the owner to sign in to a site
  python assistant/doer/browser.py --front         bring the RUNNING browser on screen, maximized, in front (no restart)
  python assistant/doer/browser.py --close
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
    """The default for a job's window: in front, unless someone is at the PC right now or in_front is off."""
    return IN_FRONT and not someone_at_the_pc()


def cdp_url() -> str:
    return f"http://127.0.0.1:{PORT}"


def chrome_exe() -> Path | None:
    if CFG.get("chrome"):
        p = Path(os.path.expandvars(CFG["chrome"]))
        if p.exists():
            return p
    for root in CHROME_CANDIDATES:
        if not root.exists():
            continue
        hits = sorted(root.glob("chromium-*/chrome-win*/chrome.exe"), reverse=True)
        if hits:
            return hits[0]
    for p in (Path(os.path.expandvars(r"%ProgramFiles%\Google\Chrome\Application\chrome.exe")),
              Path(os.path.expandvars(r"%ProgramFiles(x86)%\Google\Chrome\Application\chrome.exe"))):
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
        st = _state()
        if show and st.get("offscreen") and held_tabs():
            try:                                          # a page is held for an owner: move it, never restart over it
                show_on_screen()
            except Exception:  # noqa: BLE001
                pass
            prune_tabs()
            return cdp_url()
        if show and st.get("offscreen"):
            # on-screen was asked for but it is hidden: restart it visible
            close_browser()
        else:
            prune_tabs()
            if front_default and st.get("offscreen"):
                try:                                      # the default: bring the one that is up in front, no restart
                    show_on_screen()
                except Exception:  # noqa: BLE001
                    pass
            return cdp_url()
    show = bool(show) or front_default
    exe = chrome_exe()
    if not exe:
        raise RuntimeError("no Chromium found: `npx playwright install chromium` or set doer.chrome in assistant/config.json")
    PROFILE.mkdir(parents=True, exist_ok=True)
    lease = _lease()
    _keep_session_cookies()
    PORT, _how = ports.claim("cdp", PORT)                 # a non-browser program on the port moves us too
    args = [str(exe), f"--remote-debugging-port={PORT}", f"--user-data-dir={PROFILE}"] + LAUNCH_ARGS
    offscreen = not show and bool(CFG.get("offscreen", True))
    if offscreen:
        args += OFFSCREEN
    args.append("about:blank")
    p = subprocess.Popen(args, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                         creationflags=NO_WINDOW, close_fds=True)
    t0 = time.time()
    while time.time() - t0 < wait_s:
        if alive():
            prune_tabs()
            _save_state({"pid": p.pid, "port": PORT, "offscreen": offscreen, "started": time.strftime("%Y-%m-%dT%H:%M:%S"),
                         "lease": bool(lease), "profile": str(PROFILE)})
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


def close_browser(force: bool = False) -> bool:
    """Ask the browser to quit (Browser.close over CDP), then make sure its pid is gone. While a page is held open for
    an owner (held_tabs) it stays up and this returns False; `force` is the owner's Do later (leftopen.later)."""
    if not force and held_tabs() and alive():
        return False
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
            "someone_at_the_pc": someone_at_the_pc()}


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
    ap.add_argument("--close", action="store_true")
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
    if a.close:
        print("closed" if close_browser() else "was not running")
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
                close_browser()
        return 0
    print(json.dumps(status(), indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main())

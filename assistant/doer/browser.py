"""browser.py - the doer's ONE browser: a persistent Chromium profile opened with a CDP port.

Why one shared browser: the model drives pages through the Playwright MCP, and login_fill.py
types passwords into the SAME pages from a separate process the model cannot see. Both connect
to this browser over CDP (http://127.0.0.1:<port>). The profile is the wedding `playwright-cli`
profile (~/.claude/pw-wv-cli-profile), so every site the owner has signed into there stays signed
in here, and vice versa. The profile lease (scripts/lib/profile_lease.py) keeps this launcher,
the MCP and HoneyBook automation from opening the profile at the same time.

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

CFG = (load_json(ROOT / "config.json", {}) or {}).get("doer") or {}
PROFILE = Path(os.path.expanduser(os.environ.get("DOER_PROFILE") or CFG.get("profile") or "~/.claude/pw-wv-cli-profile"))
PORT = int(os.environ.get("DOER_CDP_PORT") or CFG.get("cdp_port") or 9333)
STATE = Path(os.environ["DOER_DATA"]) / "browser.json" if os.environ.get("DOER_DATA") else ROOT / "data" / "doer" / "browser.json"
CHROME_CANDIDATES = [
    Path(os.path.expandvars(r"%LOCALAPPDATA%\ms-playwright")),
]
LAUNCH_ARGS = ["--no-first-run", "--no-default-browser-check", "--disable-blink-features=AutomationControlled",
               "--disable-features=FedCm", "--window-size=1366,900", "--disable-session-crashed-bubble", "--hide-crash-restore-bubble"]
OFFSCREEN = ["--window-position=-32000,-32000"]
NO_WINDOW = 0x08000000 if os.name == "nt" else 0


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


def open_browser(show: bool = False, wait_s: float = 25.0) -> str:
    """Start the browser if it is not up; return the CDP url. Idempotent."""
    if alive():
        st = _state()
        if show and st.get("offscreen"):
            # on-screen was asked for but it is hidden: restart it visible
            close_browser()
        else:
            prune_tabs()
            return cdp_url()
    exe = chrome_exe()
    if not exe:
        raise RuntimeError("no Chromium found: `npx playwright install chromium` or set doer.chrome in assistant/config.json")
    PROFILE.mkdir(parents=True, exist_ok=True)
    lease = _lease()
    _keep_session_cookies()
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


def close_browser() -> bool:
    """Ask the browser to quit (Browser.close over CDP), then make sure its pid is gone."""
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
    pages.sort(key=lambda t: t.get("url", "").startswith(("about:", "chrome:")))
    page = pages[0]
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


def status() -> dict:
    v = alive()
    st = _state()
    return {"up": bool(v), "cdp": cdp_url() if v else "", "browser": (v or {}).get("Browser", ""), "offscreen": st.get("offscreen"),
            "pid": st.get("pid"), "profile": str(PROFILE)}


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

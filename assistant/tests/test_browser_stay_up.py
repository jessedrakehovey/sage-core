"""browser.py (2026-11-14): ONE browser that stays up, in the background or the foreground, never relaunched.

The owner: "I need it to fully support working in background and foreground modes ... stop relaunching browsers when
running new scripts. I don't like seeing browsers open and close over and over."

1. Pure: doer.mode decides where a job's window goes; a browser that is merely up is not "in use"; a caller that is
   not the running browser job never parks or quits under that job.
2. Live, on a throwaway browser of the test's own (its own profile, port, state folder and port ledger; never the
   agent's): it starts in the background with no taskbar button; a finished job parks it (same process, one blank tab); the next
   open attaches to the same process; it works there; `quit=True` alone ends it; keep_open off quits as before.
3. Live, only with SAGE_TEST_FRONT=1 (it puts a window in front of whoever is at the PC for a second): background ->
   foreground -> background are window moves, the process is the same one throughout.
   python assistant/tests/test_browser_stay_up.py
"""
import os as _os, tempfile as _tf
_os.environ.setdefault("WV_LOG_DIR", _tf.mkdtemp(prefix="wv_logs_"))
import json
import os
import socket
import subprocess
import sys
import tempfile
import time
import urllib.request
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")
HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
TMP = Path(tempfile.mkdtemp(prefix="wv_browser_up_"))
FAILS = []
_s = socket.socket()
_s.bind(("127.0.0.1", 0))
PORT = _s.getsockname()[1]
_s.close()
os.environ["DOER_DATA"] = str(TMP / "doer")
os.environ["DOER_PROFILE"] = str(TMP / "profile")
os.environ["DOER_CDP_PORT"] = str(PORT)
os.environ["SAGE_PORTS"] = str(TMP / "ports.json")
(TMP / "doer").mkdir(parents=True, exist_ok=True)
sys.path.insert(0, str(ROOT / "doer"))
sys.path.insert(0, str(ROOT / "scripts"))
import browser  # noqa: E402


def check(name, cond, extra=""):
    print(("ok   " if cond else "FAIL ") + name + (f"  {extra}" if extra and not cond else ""))
    if not cond:
        FAILS.append(name)


def tabs():
    with urllib.request.urlopen(browser.cdp_url() + "/json/list", timeout=5) as r:
        return [t for t in json.loads(r.read().decode("utf-8", "replace")) if t.get("type") == "page"]


def new_tab(url):
    req = urllib.request.Request(browser.cdp_url() + "/json/new?" + url.replace(" ", "%20"), method="PUT")
    with urllib.request.urlopen(req, timeout=8) as r:
        return json.loads(r.read().decode("utf-8", "replace"))


# ---- 1. pure --------------------------------------------------------------------------------------------------
check("the test never touches the agent's own browser", browser.PORT == PORT and str(browser.PROFILE).startswith(str(TMP)) and str(browser.STATE).startswith(str(TMP)))
_real_idle = browser.someone_at_the_pc
for mode, at_pc, want in [("foreground", True, True), ("foreground", False, True), ("background", True, False),
                          ("background", False, False), ("auto", True, False), ("auto", False, True)]:
    browser.MODE = mode
    browser.someone_at_the_pc = lambda within_s=None, v=at_pc: v
    check(f"mode {mode}, someone at the PC {at_pc}: in front = {want}", browser.want_front() is want)
browser.MODE = "background"                    # the live part below must never come in front by itself
browser.someone_at_the_pc = _real_idle

check("no lock file: no job is running", browser.job_running() == 0)
browser.JOB_LOCK.write_text(json.dumps({"pid": 999999, "ts": time.time()}), encoding="utf-8")
check("a lock held by a dead process is not a running job", browser.job_running() == 0)
browser.JOB_LOCK.write_text(json.dumps({"pid": os.getpid(), "ts": time.time()}), encoding="utf-8")
check("a lock held by a live process is a running job", browser.job_running() == os.getpid())
browser.JOB_LOCK.unlink()
check("a browser that is not running is not in use", browser.in_use() is False)

exe = browser.chrome_exe()
if not exe:
    print("skip the live part: no Chromium on this machine")
else:
    NOWIN = 0x08000000 if os.name == "nt" else 0
    other = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(600)"], creationflags=NOWIN)   # "another job"
    try:
        # ---- 2. live: stays up ------------------------------------------------------------------------------------
        t0 = time.time()
        browser.open_browser(show=False)
        cold = time.time() - t0
        pid = int(browser._state().get("pid") or 0)
        check("it starts", bool(browser.alive()) and pid > 0)
        if os.name == "nt":
            check("in the background it has no taskbar button", browser.hidden())
        check("up but idle is not 'in use'", browser.in_use() is False)

        new_tab("data:text/html,<title>job page</title>one")
        new_tab("data:text/html,<title>job page 2</title>two")
        check("a job opened pages", len(tabs()) >= 3)
        check("a finished job's close parks it", browser.close_browser() is True)
        left = tabs()
        check("parked: the same process is still up", bool(browser.alive()) and int(browser._state().get("pid") or 0) == pid and browser._pid_alive(pid))
        check("parked: one blank tab, no site left loaded", len(left) == 1 and left[0].get("url", "").startswith("about:blank"), str([t.get("url") for t in left]))
        check("parked: the state says so", bool(browser._state().get("parked")))

        t0 = time.time()
        browser.open_browser(show=False)
        warm = time.time() - t0
        check("the next job attaches to the same process (no relaunch)", int(browser._state().get("pid") or 0) == pid and browser._pid_alive(pid))
        check("attaching is faster than starting", warm < cold, f"warm {warm:.2f}s cold {cold:.2f}s")
        check("opening it again clears 'parked'", not browser._state().get("parked"))
        if os.name == "nt":
            check("still no taskbar button after the second open", browser.hidden())

        ws = browser.front_page_ws()
        got = browser._cdp_calls(ws, [("Page.navigate", {"url": "https://example.com/"})])[0] if ws else {"error": "no page"}
        time.sleep(1.5)
        r = browser._cdp_calls(browser.front_page_ws(), [("Runtime.evaluate", {"expression": "document.title + '|' + document.visibilityState + '|' + document.hasFocus()", "returnByValue": True}),
                                                         ("Page.captureScreenshot", {"format": "png"})])
        val = str(((r[0] or {}).get("result") or {}).get("value") or "")
        check("in the background: the page loaded and is painting (visible to itself)", "error" not in (got or {}) and "|visible|" in val, val)
        check("in the background: a picture of the page can be taken", len(str((r[1] or {}).get("data") or "")) > 1000)

        # another process's job is running: a helper that finishes must not park or quit under it
        browser.JOB_LOCK.write_text(json.dumps({"pid": other.pid, "ts": time.time()}), encoding="utf-8")
        new_tab("data:text/html,<title>the running job's page</title>")
        n = len(tabs())
        check("while a job runs the browser is in use", browser.in_use() is True)
        check("a helper's close under a running job does nothing", browser.close_browser() is False and len(tabs()) == n and browser._pid_alive(pid))
        browser.JOB_LOCK.write_text(json.dumps({"pid": os.getpid(), "ts": time.time()}), encoding="utf-8")
        check("the running job's own close parks it", browser.close_browser() is True and len(tabs()) == 1)
        browser.JOB_LOCK.unlink()

        # a page held open for an owner is never closed and never hidden away by a park
        held = new_tab("data:text/html,<title>held for the owner</title>")
        browser.LEFT_OPEN.write_text(json.dumps({"t1": {"state": "open", "target": held["id"]}}), encoding="utf-8")
        check("a held page: an ordinary close does nothing", browser.close_browser() is False)
        browser.park()
        check("a held page survives a park", any(t["id"] == held["id"] for t in tabs()))
        browser.LEFT_OPEN.unlink()

        ok, detail = browser.send_to_back()
        check("send to the background: no restart", ok and browser._pid_alive(pid) and browser._state().get("offscreen") is True, detail)

        # ---- 3. live: foreground and back (opt-in) -------------------------------------------------------------------
        if os.environ.get("SAGE_TEST_FRONT") == "1" and os.name == "nt":
            import ctypes
            u = ctypes.windll.user32
            before = u.GetForegroundWindow()
            ok, detail = browser.show_on_screen()
            vis = not browser.hidden()
            check("foreground: on the main monitor, shown, same process", ok and vis and browser._pid_alive(pid) and browser._state().get("offscreen") is False, detail)
            ok2, detail2 = browser.send_to_back()
            check("and back to the background: no taskbar button again, same process", ok2 and browser.hidden() and browser._pid_alive(pid), detail2)
            browser.open_browser(show=True)
            check("asking for it on screen while hidden moves it, never restarts it", int(browser._state().get("pid") or 0) == pid and browser._pid_alive(pid) and not browser.hidden())
            browser.send_to_back()
            if before:
                u.SetForegroundWindow(before)
        else:
            print("skip the foreground part (set SAGE_TEST_FRONT=1: it puts a window in front for a second)")

        check("status tells where it is", browser.status().get("up") and browser.status().get("mode") == "background" and browser.status().get("keep_open") is True)
        check("only quit=True really ends it", browser.close_browser(quit=True) is True and not browser.alive() and not browser._pid_alive(pid))

        # keep_open off: the old way, a close quits
        browser.KEEP_OPEN = False
        browser.open_browser(show=False)
        pid2 = int(browser._state().get("pid") or 0)
        check("keep_open off: a new process", pid2 and pid2 != pid)
        check("keep_open off: a close quits as before", browser.close_browser() is True and not browser.alive())
        browser.KEEP_OPEN = True
    finally:
        other.kill()
        try:
            if browser.alive():
                browser.close_browser(force=True, quit=True)
        except Exception:
            pass

print()
print("ALL PASSED" if not FAILS else f"{len(FAILS)} FAILED: {FAILS}", f"({TMP})")
sys.exit(1 if FAILS else 0)

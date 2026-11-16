"""The Sage app (assistant/app.py): the window an installed agent opens in, and the installer that ends by opening it.

1. The service: the window file is served with its key, every call without the key (or from another site) is refused,
   --dry looks without starting anything, an unknown step is a 404.
2. Jobs: a background job reports running, then its result.
3. The doctor table in plain words: ready only when everything this computer needs is there; what the app can fix is
   marked; rows that belong to later steps stay out.
4. Links: only the hosts the set-up names are opened.
5. The Claude sign-in: the token is found on a console screen (one row, wrapped rows), nothing else is taken for one,
   and a real console's screen can be read back (Windows).
6. The icon and the installer's corner image are valid files drawn by code.
7. The installer is one click and opens the app, never a browser page; the window carries no outside stylesheet or
   script and never names the agent's parts.
   python assistant/tests/test_app.py
"""
import os as _os, tempfile as _tf
_os.environ.setdefault("WV_LOG_DIR", _tf.mkdtemp(prefix="wv_logs_"))
import json
import os
import re
import socket
import struct
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")
HERE = Path(__file__).resolve().parent
ROOT = HERE.parent                                   # assistant/
TMP = Path(tempfile.mkdtemp(prefix="wv_app_"))
os.environ["SAGE_HOME"] = str(TMP / "home")
os.environ["SAGE_PACKS"] = str(TMP / "home" / "packs.json")
FAILS = []
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "installer" / "windows"))
import app  # noqa: E402
import sage  # noqa: E402
import make_assets  # noqa: E402
import welcome  # noqa: E402


def check(name, cond, extra=""):
    print(("ok   " if cond else "FAIL ") + name + (f"  {extra}" if extra and not cond else ""))
    if not cond:
        FAILS.append(name)


def free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


# ---------------------------------------------------------------- 1. the service
port = free_port()
env = {k: v for k, v in os.environ.items() if not k.startswith(("ASSISTANT_", "AGENT_", "DOER_"))}
env.update(PYTHONIOENCODING="utf-8")
srv = subprocess.Popen([sys.executable, str(ROOT / "app.py"), "--no-window", "--dry", "--port", str(port)], env=env, cwd=str(TMP),
                       stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True)


def http(method, path, body=None, key="", origin=None):
    data = json.dumps(body).encode("utf-8") if body is not None else None
    req = urllib.request.Request(f"http://127.0.0.1:{port}{path}", data=data, method=method)
    if data is not None:
        req.add_header("Content-Type", "application/json")
    if key:
        req.add_header("X-Sage-Key", key)
    if origin:
        req.add_header("Origin", origin)
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            return r.status, r.read().decode("utf-8")
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8")


try:
    first = srv.stdout.readline().strip()
    check("the service says where it is", first == f"APP http://127.0.0.1:{port}/", first)
    code, page = http("GET", "/")
    m = re.search(r'const KEY = "([^"]+)"', page)
    key = m.group(1) if m else ""
    check("the window file is served with its key filled in", code == 200 and "<title>Sage</title>" in page and len(key) >= 24 and "__SAGE_KEY__" not in page)
    code, body = http("GET", "/api/state")
    check("state without the key is refused", code == 403, body[:120])
    code, body = http("GET", "/api/state", key=key)
    st = json.loads(body) if code == 200 else {}
    check("state with the key: the app, no organization yet, creation possible or not said plainly", code == 200 and st.get("app") == "Sage" and st.get("pack") == "" and "jobs" in st, body[:200])
    code, body = http("POST", "/api/checks", {}, key="wrong")
    check("a call with the wrong key is refused", code == 403)
    code, body = http("POST", "/api/checks", {}, key=key, origin="http://evil.test")
    check("a call from another site is refused even with the key", code == 403)
    code, body = http("POST", "/api/start", {}, key=key)
    check("--dry starts nothing and says so", code == 200 and json.loads(body).get("ok") is False and "preview" in json.loads(body).get("message", "").lower(), body[:160])
    code, body = http("POST", "/api/claude", {}, key=key)
    check("--dry opens no sign-in window", code == 200 and json.loads(body).get("ok") is False)
    code, body = http("POST", "/api/google", {}, key=key)
    check("--dry signs in to nothing at Google and hands out no secret", code == 200 and json.loads(body).get("ok") is False
          and json.loads(http("POST", "/api/google_text", {"what": "secret"}, key=key)[1]).get("ok") is False)
    code, body = http("POST", "/api/google_status", {}, key=key)
    check("Google's status with no organization yet says so, plainly", code == 200 and json.loads(body).get("ok") is False and "organization" in json.loads(body).get("message", ""))
    code, body = http("POST", "/api/alive", {}, key=key)
    al = json.loads(body) if code == 200 else {}
    check("a second launch can ask how lately the window spoke (so it never opens a second window beside one that is loading)",
          al.get("ok") is True and al.get("ping_age", 99) < 30 and al.get("opening") is False, body[:160])
    code, body = http("POST", "/api/nothing", {}, key=key)
    check("an unknown step is a 404", code == 404)
    code, body = http("GET", "/fonts/../app.py")
    check("the fonts path serves fonts only", code == 404)
finally:
    srv.kill()

# ---------------------------------------------------------------- 2. jobs
j = app.job_start("test", "A test job", lambda note: (note("half way"), time.sleep(0.3), {"ok": True, "message": "done"})[-1])
g1 = app.job_get(j["job"])
time.sleep(0.8)
g2 = app.job_get(j["job"])
check("a job runs in the background and then carries its result", j.get("ok") and g1.get("status") == "running" and g2.get("status") == "done"
      and g2["result"]["message"] == "done" and g2.get("line") == "half way", json.dumps(g2)[:200])
j = app.job_start("test", "A job in steps", lambda note: (note("##step signin"), note("60% of 54 MB"), time.sleep(0.4), {"ok": True})[-1])
time.sleep(0.15)
g = app.job_get(j["job"])
check("a long job says which step it is on, apart from its small print", g.get("step") == "signin" and g.get("line") == "60% of 54 MB", json.dumps(g)[:200])
j = app.job_start("test", "A failing job", lambda note: 1 / 0)
time.sleep(0.3)
g = app.job_get(j["job"])
check("a job that raises ends failed with the reason, not a hung spinner", g.get("status") == "failed" and "ZeroDivisionError" in g["result"]["message"])
check("an unknown job says so", app.job_get("nope").get("status") == "gone")

# ---------------------------------------------------------------- 3. the doctor table in plain words
ROWS_OK = [{"check": c, "status": "ok", "detail": "x", "fix": ""} for c in ("python", "org pack", "engine manifest", "python packages", "playwright browsers", "browser tools", "claude cli", "disk")]
LATER = [{"check": "claude token", "status": "fail", "detail": "missing", "fix": ""}, {"check": "google chat", "status": "info", "detail": "not yet", "fix": ""},
         {"check": "serve", "status": "warn", "detail": "not running", "fix": ""}, {"check": "engine updates", "status": "info", "detail": "no feed", "fix": ""}]
real_doctor = welcome.doctor
welcome.doctor = lambda: {"ok": True, "rows": ROWS_OK + LATER, "fails": 1, "message": ""}
c = app.checks()
check("ready when everything this computer needs is there; the Claude sign-in and Google are other steps", c["ok"] and c["ready"] and all(i["status"] == "ok" for i in c["items"])
      and not any(i["key"] in ("claude token", "google chat", "serve") for i in c["items"]), json.dumps(c["items"])[:300])
bad = [dict(r, status="fail") if r["check"] in ("claude cli", "browser tools") else r for r in ROWS_OK] + [{"check": "shares with another agent", "status": "fail", "detail": "port 9020 is taken", "fix": ""}]
welcome.doctor = lambda: {"ok": True, "rows": bad, "fails": 3, "message": ""}
c = app.checks()
by = {i["key"]: i for i in c["items"]}
check("not ready with something missing; what the app can fix is marked, and one fix covers the packages and the browser tools",
      not c["ready"] and by["claude cli"]["fixable"] and by["browser tools"]["fix_key"] == "python packages" and "3 things" in c["message"], c["message"])
check("a failing row the app has no words for still shows, with the table's own sentence", by.get("shares with another agent", {}).get("detail") == "port 9020 is taken")
welcome.doctor = lambda: {"ok": False, "rows": [], "message": "no organization yet"}
check("no organization yet: not ready, and the reason is passed on", app.checks()["ready"] is False and "no organization" in app.checks()["message"])
welcome.doctor = real_doctor
check("a fix for something the app cannot install is refused, and nothing runs without an organization", app.fix("disk")["ok"] is False)

# ---------------------------------------------------------------- 4. links
check("a link outside the set-up's own hosts is refused", app.open_link({"url": "https://example.test/x"})["ok"] is False and app.open_link({"url": "http://console.cloud.google.com/"})["ok"] is False
      and app.open_link({"url": "https://google.com.evil.test/"})["ok"] is False)

# ---------------------------------------------------------------- 5. the Claude sign-in
tok = "sk-ant-" + "oat01-" + "Ab3_" * 24 + "xyz-9"          # shaped like one; made here, never a real one
w = 60
check("a token wrapped over three rows is put back together", sage.find_token(["", "  Long-lived token created.", "", "  " + tok[:w - 2], tok[w - 2:2 * w - 2], tok[2 * w - 2:], "", "  Store this token securely."], w) == tok)
check("a token inside a sentence on one row is cut out of it", sage.find_token(["Your token: " + tok + " (valid for a year)"], 300) == tok)
check("the last token on the screen wins", sage.find_token([tok.replace("xyz-9", "old-1"), "", tok], 300) == tok)
check("a short look-alike or a plain screen is not a token", sage.find_token(["sk-ant-" + "oat01-short", "nothing here"], 80) == "" and sage.find_token([], 0) == "")
if os.name == "nt":
    out_file = TMP / "console.json"
    snippet = (f"import sys, json; sys.path.insert(0, r'{ROOT}'); import sage; "
               f"print('  Long-lived token created.'); print(); print('  ' + {tok!r}); print(); print('  Store this token securely.'); sys.stdout.flush(); "
               f"rows, width = sage.console_lines(); open(r'{out_file}', 'w', encoding='utf-8').write(json.dumps({{'width': width, 'token': sage.find_token(rows, width)}}))")
    si = subprocess.STARTUPINFO()
    si.dwFlags |= subprocess.STARTF_USESHOWWINDOW
    si.wShowWindow = 0                                         # a real console, never shown
    subprocess.run([sys.executable, "-c", snippet], creationflags=subprocess.CREATE_NEW_CONSOLE, startupinfo=si, timeout=60, env=env)
    got = json.loads(out_file.read_text(encoding="utf-8")) if out_file.exists() else {}
    check("a real console's screen is read back and the token found on it (no copy and paste)", got.get("token") == tok and got.get("width", 0) > 0, str(got)[:200])
check("with no console to read, the reader says so and the paste prompt stays the way", isinstance(sage.console_lines(), tuple))

# ---------------------------------------------------------------- 6. the icon and the corner image
ico = make_assets.ico(sizes=(16, 32, 256))
n = struct.unpack("<HHH", ico[:6])
check("the icon is a real .ico with one picture per size", n == (0, 1, 3) and len(ico) > 2000)
px = make_assets.mark(64)
check("the mark: clear corners, a green tile, a white leaf in the middle", px[0][0][3] == 0 and px[6][32][:3] == make_assets.GREEN and px[6][32][3] == 255 and px[31][42][:3] == (255, 255, 255), str((px[0][0], px[6][32], px[31][42])))
bmp = make_assets.bmp24(55, 55)
check("the installer's corner image is a 24-bit bitmap of the right size", bmp[:2] == b"BM" and struct.unpack("<ii", bmp[18:26]) == (55, 55) and struct.unpack("<H", bmp[28:30])[0] == 24)
check("the picture file is a real PNG", make_assets.png(make_assets.mark(32))[:8] == b"\x89PNG\r\n\x1a\n")

# ---------------------------------------------------------------- 7. one click, an app window, one agent
iss = (ROOT / "installer" / "windows" / "SageSetup.iss").read_text(encoding="utf-8")
ps1 = (ROOT / "installer" / "windows" / "install.ps1").read_text(encoding="utf-8")
ui = (ROOT / "app_ui.html").read_text(encoding="utf-8")
check("the installer asks nothing and opens the app, never a page in a browser", "DisableWelcomePage=yes" in iss and "DisableFinishedPage=yes" in iss and "app.py" in iss
      and "welcome.py"" --open" not in iss and "--open" not in iss.split("[UninstallRun]")[0])
check("the installer shows the steps the install script reports", "##step" in iss and ps1.count("\nStep ") >= 6 and "##fail" in ps1 and "ExecAndLogOutput" in iss)
check("the Start-menu entry opens the app with its own icon", 'IconFilename: "{app}\\sage.ico"' in iss and "SetupIconFile" in iss)
check("the install script opens the app when it ends, not a browser", "app.py" in ps1 and "welcome.py" not in ps1.split("if (-not $NoLaunch)")[-1])
check("the window loads nothing from outside: no stylesheet, script or font from the network", not re.search(r'<(link|script)[^>]+(href|src)="https?://', ui) and "url(/fonts/" in ui)
shown = re.sub(r"<style>.*?</style>", "", ui, flags=re.S)
check("the window speaks as one agent and sends nobody to a page or a terminal",
      not re.search(r"\b(operator|doer|builder|supervisor|Mission Control|terminal|localhost|command line)\b", shown, flags=re.I), str(re.findall(r"\b(operator|doer|builder|supervisor|Mission Control|terminal|localhost|command line)\b", shown, flags=re.I))[:120])
check("the Google screen asks before anything in the owner's Google organization changes, and offers every value to copy",
      "Allow for this project" in ui and "{allow: true}" in ui and 'data-copy="' in ui and "google_relay" in ui and "google_check" in ui)
appsrc = (ROOT / "app.py").read_text(encoding="utf-8")
check("the window never shows the browser's 'restore pages' popup and keeps working while another window covers it",
      "--hide-crash-restore-bubble" in appsrc and "--disable-backgrounding-occluded-windows" in appsrc and "--disable-background-timer-throttling" in appsrc)
check("bringing the window forward restores it only when it is minimized (a maximized window is not shrunk)", "IsIconic" in appsrc and appsrc.count("ShowWindow(") == 1)
check("the app finds its windows by its own browser profile, never by a title another program may share", "FindWindowW" not in appsrc and "GetWindowThreadProcessId" in appsrc and 'home() / "app-window"' in appsrc)
check("a window never outlives its service", "close_windows()                        # the service is ending" in appsrc)
if os.name == "nt":
    check("a process's own command line can be read (how the app knows a browser window is its own)", "test_app.py" in app._command_line(os.getpid()), app._command_line(os.getpid())[:120])
    check("with no app window open, none is found: other programs' windows are never taken for the app's", app.app_windows() == [])
lock = app.home() / "app.lock"
lock.parent.mkdir(parents=True, exist_ok=True)
lock.write_text(str(os.getppid()), encoding="ascii")
held = app._claim_single()
lock.write_text("999999", encoding="ascii")
taken = app._claim_single()
mine = lock.read_text(encoding="ascii").strip() == str(os.getpid())
app._release_single()
check("one app at a time: a second launch finds the first one's lock; a lock left by a process that is gone is taken over and given back", held is False and taken is True and mine and not lock.exists())
check("the window paints light from its first frame, with no blur to redraw", 'name="color-scheme" content="light"' in ui and "backdrop-filter" not in ui)
check("every screen the flow names exists", all(f"  {k}: " in ui for k in ("intro", "org", "agent", "people", "building", "computer", "claude", "google", "home")))

print()
print("ALL PASSED" if not FAILS else f"{len(FAILS)} FAILED: {FAILS}", f"({TMP})")
sys.exit(1 if FAILS else 0)

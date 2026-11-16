"""app.py - the Sage app: a window of its own on this computer (sage-core, 2026-11-14).

An owner who installs Sage gets an app, the way any installed program is one: the installer ends by opening this
window and the Start-menu entry opens it again. No browser tab, no address to type. Inside, the first time: the
set-up (the organization's name, what to call the agent, the owners, what this computer still needs, the Claude
sign-in). From then on: the agent's home (running or paused, what is connected, what comes next).

  python assistant/app.py                 the window (starts the app's own local service, ends when the window closes)
  python assistant/app.py --no-window     the service alone; prints APP <url> (tests, a preview; add --dry to look without starting anything)
  python assistant/app.py --state         what the window would show, as JSON
  python assistant/app.py --quit          close the app if it is open
  python assistant/app.py --fetch-fonts [folder]   the two typefaces, once (the installer's bundle carries them)

How: the window is this computer's own Chromium-family browser (Edge, Chrome, else the agent's own browser) in app
mode, with a profile folder of its own, showing assistant/app_ui.html from a service bound to 127.0.0.1 on a port
the system picks. Every call carries a key made at start, so a page in some other browser tab cannot drive it. The
steps themselves are welcome.py's (create, doctor, start); this file adds the window, background jobs the window
polls, a plain-words reading of the doctor table, one-tap fixes for what the installer could not finish, the
sign-in window that closes itself, pause / resume, and the typefaces (fetched once, kept on this computer).
Stdlib only.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import secrets
import shutil
import subprocess
import sys
import threading
import time
import urllib.parse
import urllib.request
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

HERE = Path(__file__).resolve().parent
# the installed copy sits in <home>/engine/assistant: that home (install.json next to engine/) is where the app keeps
# its things, wherever the installer was pointed, without a variable a Start-menu entry cannot set
if not os.environ.get("SAGE_HOME") and HERE.parent.name == "engine" and (HERE.parent.parent / "install.json").exists():
    os.environ["SAGE_HOME"] = str(HERE.parent.parent)
sys.path.insert(0, str(HERE))
import welcome  # noqa: E402  (the steps: state, create, doctor, probe, start; one backend for the window and the page)
import google_connect  # noqa: E402  (the Google side with one sign-in; the window's Connect Google screen)

IS_WIN = os.name == "nt"
NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)
NEW_CONSOLE = getattr(subprocess, "CREATE_NEW_CONSOLE", 0)
UI = HERE / "app_ui.html"
TITLE = "Sage"
WINDOW_SIZE = (1180, 800)
IDLE_S = 900                     # with no window process to watch, this long without a ping ends the service (a hidden tab pings rarely)
KEY = secrets.token_urlsafe(24)
_LAST_PING = {"t": time.time()}
_QUIT = {"now": False}
_JOBS: dict = {}
_JOBS_LOCK = threading.Lock()
FONT_CSS = ("https://fonts.googleapis.com/css2?family=Fraunces:opsz,wght@9..144,500..700"
            "&family=DM+Sans:wght@400;500;600;700&display=swap")
FONT_FILES = {"Fraunces": "fraunces.woff2", "DM Sans": "dmsans.woff2"}
# where a link in the window may lead (opened in the owner's own browser, where they are signed in)
OPEN_HOSTS = ("google.com", "claude.ai", "anthropic.com", "claude.com")


# ---------------------------------------------------------------- where things are
def home() -> Path:
    return welcome.sage_home()


def fonts_dir() -> Path:
    return home() / "fonts"


def have_fonts() -> bool:
    return all((fonts_dir() / f).exists() for f in FONT_FILES.values())


def fetch_fonts(dst: Path | None = None) -> bool:
    """The two typefaces as files on this computer, so the window looks the same with no network. One request for
    the stylesheet, one per file; nothing about this computer or its owner is sent. False (and the window falls
    back to the system's own faces) when the network says no."""
    dst = dst or fonts_dir()
    try:
        req = urllib.request.Request(FONT_CSS, headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36"})
        with urllib.request.urlopen(req, timeout=20) as r:
            css = r.read().decode("utf-8")
        dst.mkdir(parents=True, exist_ok=True)
        got = 0
        for family, fname in FONT_FILES.items():
            # the "latin" block of this family: the last @font-face of the family whose comment says latin
            blocks = re.findall(r"/\*\s*([\w-]+)\s*\*/\s*@font-face\s*\{([^}]*)\}", css)
            url = ""
            for subset, body in blocks:
                if subset == "latin" and f"'{family}'" in body and "italic" not in body:
                    m = re.search(r"url\((https://[^)]+\.woff2)\)", body)
                    if m:
                        url = m.group(1)
            if not url:
                continue
            with urllib.request.urlopen(url, timeout=30) as r:
                data = r.read()
            if len(data) > 5000:
                tmp = dst / (fname + ".part")
                tmp.write_bytes(data)
                tmp.replace(dst / fname)
                got += 1
        return got == len(FONT_FILES)
    except Exception:  # noqa: BLE001  (no network, a changed stylesheet: the system faces do)
        return False


# ---------------------------------------------------------------- background jobs (the window polls them)
def job_start(kind: str, title: str, fn) -> dict:
    jid = secrets.token_hex(5)
    rec = {"id": jid, "kind": kind, "title": title, "status": "running", "line": "", "step": "", "link": "", "result": None, "started": time.time()}
    with _JOBS_LOCK:
        _JOBS[jid] = rec

    def note(line: str) -> None:
        line = str(line).strip()
        if line.startswith("##step "):                 # which step a long job is on (the window ticks its list by this)
            rec["step"], rec["line"], rec["link"] = line[7:].strip(), "", ""
        elif line.startswith("##link "):               # a page the step is waiting on, for a button that opens it again
            rec["link"] = line[7:].strip()
        else:
            rec["line"] = line[-200:]

    def run() -> None:
        try:
            res = fn(note)
        except Exception as e:  # noqa: BLE001
            res = {"ok": False, "message": f"{type(e).__name__}: {e}"}
        rec["result"] = res if isinstance(res, dict) else {"ok": bool(res), "message": ""}
        rec["status"] = "done" if rec["result"].get("ok") else "failed"
        rec["ended"] = time.time()

    threading.Thread(target=run, daemon=True).start()
    return {"ok": True, "job": jid}


def job_get(jid: str) -> dict:
    with _JOBS_LOCK:
        rec = _JOBS.get(jid)
    if not rec:
        return {"ok": False, "status": "gone", "message": "no such job"}
    return {"ok": True, **{k: rec[k] for k in ("id", "kind", "title", "status", "line", "step", "link", "result")}}


def running_jobs() -> list:
    with _JOBS_LOCK:
        return [{k: r[k] for k in ("id", "kind", "title", "line")} for r in _JOBS.values() if r["status"] == "running"]


def _stream(argv: list, note, cwd: str | None = None, timeout: int = 1800, shell_env: dict | None = None) -> tuple:
    """Run a command with no window, handing each output line to the job's status. (exit code, last lines)."""
    env = {k: v for k, v in os.environ.items() if k not in ("CLAUDECODE", "CLAUDE_CODE_ENTRYPOINT")}
    env["PYTHONIOENCODING"] = "utf-8"
    env.update(shell_env or {})
    tail: list = []
    try:
        p = subprocess.Popen(argv, cwd=cwd, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, encoding="utf-8",
                             errors="replace", creationflags=NO_WINDOW)
    except OSError as e:
        return 127, str(e)
    end = time.time() + timeout
    for line in p.stdout:  # type: ignore[union-attr]
        line = line.strip()
        if line:
            tail.append(line)
            tail[:] = tail[-12:]
            note(line)
        if time.time() > end:
            p.kill()
            return 124, "took too long"
    p.wait()
    return p.returncode, "\n".join(tail)


# ---------------------------------------------------------------- the doctor table, in plain words
# check -> (what the owner reads, when all is well, when it is not, can the app fix it itself)
NOW = {
    "python": ("Python", "installed, private to Sage", "Python is missing or too old", False),
    "org pack": ("Your organization's folder", "in place", "the organization's folder is not set up", False),
    "engine manifest": ("The Sage engine", "in place", "the engine's file list is missing", False),
    "python packages": ("Building blocks", "all here", "some building blocks did not install", True),
    "playwright browsers": ("A browser of its own", "installed", "the browser the agent works in is not installed yet", True),
    "browser tools": ("Browser hands", "working", "the agent cannot work its browser yet", True),
    "claude cli": ("Claude on this computer", "installed", "Claude is not installed on this computer yet", True),
    "engine updates": ("Updates", "new versions arrive by themselves", "", False),
    "disk": ("Room on the disk", "plenty", "less than 5 GB free: make some room", False),
}
FIX_SAME = {"browser tools": "python packages"}       # one fix covers both rows


def checks() -> dict:
    """welcome.doctor() read for an owner: the handful of things this computer needs now, each ok or with one plain
    sentence (and a Fix button where the app can do it). Everything else in the table belongs to a later step (the
    Claude sign-in, Google, the things the agent sets up from chat) and stays out of the way; `rows` keeps it all."""
    d = welcome.doctor()
    rows = d.get("rows") or []
    if not d.get("ok"):
        return {"ok": False, "ready": False, "message": d.get("message", ""), "items": [], "rows": rows}
    items = []
    for r in rows:
        name, st = r.get("check", ""), r.get("status", "")
        if name in NOW:
            title, good, bad, fixable = NOW[name]
            okay = st == "ok" or (name == "engine updates" and st == "info") or (name == "engine manifest" and st == "warn")
            items.append({"key": name, "title": title, "status": "ok" if okay else "fix", "detail": good if okay else bad,
                          "fixable": bool(fixable and not okay), "fix_key": FIX_SAME.get(name, name)})
        elif st == "fail" and name not in ("claude token", "claude auth (api key)", "headless probe", "chat relay"):
            # something this file has no words for (another agent on this computer sharing a port, say): the table's own
            items.append({"key": name, "title": name[:1].upper() + name[1:], "status": "fix", "detail": str(r.get("detail") or ""), "fixable": False, "fix_key": name})
    todo = [i for i in items if i["status"] != "ok"]
    return {"ok": True, "ready": not todo, "items": items, "rows": rows,
            "message": "This computer has everything it needs." if not todo else f"{len(todo)} thing{'s' if len(todo) != 1 else ''} to sort out."}


def fix(key: str) -> dict:
    pack = welcome.current_pack()
    if not pack:
        return {"ok": False, "message": "no organization yet"}
    py = welcome.pack_python(pack)
    if key == "claude cli":
        if IS_WIN:
            argv = ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", "irm https://claude.ai/install.ps1 | iex"]
        else:
            argv = ["bash", "-lc", "curl -fsSL https://claude.ai/install.sh | bash"]
        title = "Installing Claude"
    elif key == "playwright browsers":
        argv, title = [py, "-m", "playwright", "install", "chromium"], "Installing the browser"
        os.environ["PLAYWRIGHT_SKIP_BROWSER_GC"] = "1"      # never delete the shared folder's other browsers (install.ps1 says why)
    elif key == "python packages":
        argv, title = [py, "-m", "pip", "install", "--disable-pip-version-check", "--no-warn-script-location", "-r", str(pack / "assistant" / "requirements.txt")], "Installing the building blocks"
    else:
        return {"ok": False, "message": "nothing the app can do for that one by itself"}

    def run(note):
        rc, tail = _stream(argv, note, cwd=str(pack))
        return {"ok": rc == 0, "message": "done" if rc == 0 else (tail.splitlines()[-1] if tail else f"ended with code {rc}")}

    return job_start("fix", title, run)


# ---------------------------------------------------------------- the steps as jobs
def create(body: dict) -> dict:
    return job_start("create", "Creating the agent", lambda note: welcome.create(body))


def start() -> dict:
    return job_start("start", "Starting the agent", lambda note: welcome.start())


def probe() -> dict:
    return job_start("probe", "Saying hello to Claude", lambda note: welcome.probe())


def claude_window(mode: str = "subscription") -> dict:
    """The one window the owner signs in through themselves: a small console that runs the pack's `sage token --auto`
    (the Claude CLI's own sign-in, the token picked up and stored without a copy and paste) and closes by itself.
    Nothing secret passes through the app."""
    pack = welcome.current_pack()
    if not pack:
        return {"ok": False, "message": "no organization yet"}
    if mode != "api" and not welcome.claude_cli():
        return {"ok": False, "message": "Claude is not installed on this computer yet."}
    argv = [welcome.pack_python(pack), str(pack / "assistant" / "sage.py"), "token", "--auto"] + (["--api-key"] if mode == "api" else [])
    if not IS_WIN:
        return {"ok": False, "message": "Open a terminal and run: " + " ".join(f'"{a}"' if " " in a else a for a in argv)}
    quoted = " ".join(f'"{a}"' if " " in a else a for a in argv)
    try:
        subprocess.Popen(["cmd", "/c", f"title Sign in to Claude && mode con: cols=120 lines=40 && cd /d \"{pack}\" && {quoted}"],
                         cwd=str(pack), creationflags=NEW_CONSOLE, close_fds=True)
    except OSError as e:
        return {"ok": False, "message": f"could not open the sign-in window: {e}"}
    return {"ok": True, "message": "A small sign-in window opened."}


# ---------------------------------------------------------------- Google, with one sign-in (google_connect.py)
def _pack_or_none():
    return welcome.current_pack()


def google_run(body: dict) -> dict:
    pack = _pack_or_none()
    if not pack:
        return {"ok": False, "message": "no organization yet"}
    return job_start("google", "Connecting Google", lambda note: google_connect.run(
        pack, note, allow_keys_rule=bool(body.get("allow")), other_account=bool(body.get("other"))))


def google_status(body: dict) -> dict:
    pack = _pack_or_none()
    return google_connect.status(pack) if pack else {"ok": False, "message": "no organization yet"}


def google_text(body: dict) -> dict:
    pack = _pack_or_none()
    return google_connect.text(pack, str(body.get("what") or "")) if pack else {"ok": False, "message": "no organization yet"}


def google_relay(body: dict) -> dict:
    pack = _pack_or_none()
    return google_connect.save_relay(pack, str(body.get("url") or ""), str(body.get("id") or "")) if pack else {"ok": False, "message": "no organization yet"}


def google_check(body: dict) -> dict:
    pack = _pack_or_none()
    if not pack:
        return {"ok": False, "message": "no organization yet"}
    return job_start("google_check", "Checking the connection", lambda note: google_connect.check(pack))


def chat_connected(pack: Path) -> bool:
    """Connected means proven: the app's own check passed. An organization connected before this screen existed (its
    relay or endpoint already in the config, no record of a run here) counts as connected too."""
    gst = google_connect.load(pack)
    if (gst.get("done") or {}).get("checked"):
        return True
    gl = welcome._load(pack / "assistant" / "agent" / "gchat.local.json", {}) or {}
    return not gst and bool(gl.get("relay_webapp_url") or gl.get("endpoint_url") or gl.get("relay_deployment_id"))


def pause(on: bool) -> dict:
    pack = welcome.current_pack()
    if not pack:
        return {"ok": False, "message": "no organization yet"}
    rc, out = welcome._sage(pack, ["pause" if on else "resume"], timeout=30)
    return {"ok": rc == 0, "message": out.strip()[-160:]}


def open_link(body: dict) -> dict:
    """A link from the window, opened in the owner's own browser (their Google sign-in lives there, not in the app's
    window); the organization's folder in the file manager. Only the hosts the set-up steps name."""
    if body.get("folder"):
        pack = welcome.current_pack()
        if not pack:
            return {"ok": False, "message": "no organization yet"}
        try:
            if IS_WIN:
                os.startfile(str(pack))  # noqa: S606
            else:
                subprocess.Popen(["open" if sys.platform == "darwin" else "xdg-open", str(pack)])
        except OSError as e:
            return {"ok": False, "message": str(e)}
        return {"ok": True, "message": ""}
    url = str(body.get("url") or "")
    host = (urllib.parse.urlparse(url).hostname or "").lower()
    if not url.startswith("https://") or not any(host == h or host.endswith("." + h) for h in OPEN_HOSTS):
        return {"ok": False, "message": "that link is not one of the set-up steps"}
    webbrowser.open(url)
    return {"ok": True, "message": ""}


def state() -> dict:
    s = welcome.state()
    pack = Path(s["pack"]) if s.get("pack") else None
    a = (pack / "assistant") if pack else None
    s.update({
        "app": TITLE, "fonts": have_fonts(), "jobs": running_jobs(),
        "paused": bool(a and (a / "data" / ".sage_paused").exists()),
        "chat_connected": bool(pack and chat_connected(pack)),
        "google_accounts": len(((welcome._load(a / "agent" / "google.json", {}) or {}).get("accounts") or {})) if a else 0,
        "first_name": (str((s.get("owners") or [""])[0]).split() or [""])[0],
    })
    return s


# ---------------------------------------------------------------- the local service
def _page() -> str:
    try:
        return UI.read_text(encoding="utf-8").replace("__SAGE_KEY__", KEY)
    except OSError:
        return "<!doctype html><title>Sage</title><p>The app's window file is missing (assistant/app_ui.html). Run the installer again.</p>"


POSTS = {
    "create": lambda b: create(b), "checks": lambda b: checks(), "fix": lambda b: fix(str(b.get("key") or "")),
    "claude": lambda b: claude_window(str(b.get("mode") or "subscription")), "probe": lambda b: probe(), "start": lambda b: start(),
    "pause": lambda b: pause(True), "resume": lambda b: pause(False), "open": lambda b: open_link(b),
    "job": lambda b: job_get(str(b.get("id") or "")),
    "google": google_run, "google_status": google_status, "google_text": google_text, "google_relay": google_relay, "google_check": google_check,
    "quit": lambda b: (_QUIT.update(now=True) or {"ok": True, "message": "closing"}),
}


DRY = {"on": False}                # --dry: a preview or a test may look at every screen; nothing is started, installed or signed in
HOLD = {"on": False}               # --hold-agent: every set-up step is real, but the agent itself is never started (a trial on a test organization)
HOLD_STEPS = ("start", "pause", "resume")
DRY_STEPS = ("create", "fix", "claude", "probe", "start", "pause", "resume", "open", "google", "google_text", "google_relay", "google_check")


class _Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):  # quiet
        pass

    def _send(self, code: int, body, ctype: str, cache: str = "no-store") -> None:
        data = body if isinstance(body, bytes) else str(body).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype + ("; charset=utf-8" if ctype.startswith(("text/", "application/json")) else ""))
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", cache)
        self.end_headers()
        self.wfile.write(data)

    def _json(self, obj, code: int = 200) -> None:
        self._send(code, json.dumps(obj, ensure_ascii=False), "application/json")

    def _ours(self) -> bool:
        if not secrets.compare_digest(self.headers.get("X-Sage-Key") or "", KEY):
            return False
        origin = self.headers.get("Origin")
        return not origin or origin.split("://", 1)[-1].rstrip("/") == (self.headers.get("Host") or "")

    def do_GET(self):
        p = self.path.split("?")[0]
        if p in ("/", "/app", "/app/"):
            return self._send(200, _page(), "text/html")
        if p.startswith("/fonts/"):
            f = fonts_dir() / Path(p).name
            if f.suffix == ".woff2" and f.exists():
                return self._send(200, f.read_bytes(), "font/woff2", cache="max-age=31536000")
            return self._send(404, "", "text/plain")
        if p == "/api/state":
            if not self._ours():
                return self._json({"ok": False, "message": "not from the app's window"}, 403)
            _LAST_PING["t"] = time.time()
            return self._json(state())
        return self._send(404, "", "text/plain")

    def do_POST(self):
        p = self.path.split("?")[0]
        if not self._ours() or "application/json" not in (self.headers.get("Content-Type") or "").lower():
            return self._json({"ok": False, "message": "not from the app's window"}, 403)
        _LAST_PING["t"] = time.time()
        n = int(self.headers.get("Content-Length") or 0)
        try:
            body = json.loads(self.rfile.read(min(n, 64 * 1024)).decode("utf-8") or "{}") if n else {}
        except ValueError:
            body = {}
        step = p.rsplit("/", 1)[-1]
        fn = POSTS.get(step) if p.startswith("/api/") else None
        if fn is None:
            return self._json({"ok": False, "message": "no such step"}, 404)
        if DRY["on"] and step in DRY_STEPS:
            return self._json({"ok": False, "message": "This is a preview: nothing is started from here."})
        if HOLD["on"] and step in HOLD_STEPS:
            return self._json({"ok": False, "message": "This is a trial: the agent stays asleep."})
        try:
            self._json(fn(body if isinstance(body, dict) else {}))
        except Exception as e:  # noqa: BLE001
            self._json({"ok": False, "message": f"{type(e).__name__}: {e}"}, 500)


# ---------------------------------------------------------------- the window
def window_browser() -> str:
    """A Chromium-family browser that can show one page as an app window: Edge (on every Windows 10 and 11), Chrome,
    then the agent's own browser. '' = none (the default browser gets a tab, the last resort)."""
    cands: list = []
    if IS_WIN:
        for base in (os.environ.get("ProgramFiles(x86)", ""), os.environ.get("ProgramFiles", ""), os.environ.get("LOCALAPPDATA", "")):
            if base:
                cands += [Path(base) / "Microsoft" / "Edge" / "Application" / "msedge.exe", Path(base) / "Google" / "Chrome" / "Application" / "chrome.exe"]
        pw = Path(os.environ.get("PLAYWRIGHT_BROWSERS_PATH") or (Path(os.environ.get("LOCALAPPDATA", str(Path.home()))) / "ms-playwright"))
        if pw.exists():
            for d in sorted(pw.glob("chromium-*"), reverse=True):
                cands += list(d.glob("chrome-win*/chrome.exe"))
    elif sys.platform == "darwin":
        cands += [Path("/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"), Path("/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge"),
                  Path("/Applications/Chromium.app/Contents/MacOS/Chromium")]
    else:
        cands += [Path(p) for p in (shutil.which(n) for n in ("google-chrome", "microsoft-edge", "chromium", "chromium-browser")) if p]
    for c in cands:
        if c and Path(c).exists():
            return str(c)
    return ""


SCREEN = {"name": ""}              # --screen: the window opens on that screen (the Start-menu entry never passes it)


def open_window(url: str) -> subprocess.Popen | None:
    if SCREEN["name"]:
        url = url + "#" + SCREEN["name"]
    exe = window_browser()
    if not exe:
        webbrowser.open(url)
        return None
    prof = home() / "app-window"
    prof.mkdir(parents=True, exist_ok=True)
    argv = [exe, f"--app={url}", f"--user-data-dir={prof}", f"--window-size={WINDOW_SIZE[0]},{WINDOW_SIZE[1]}", "--no-first-run",
            "--no-default-browser-check", "--disable-sync", "--disable-extensions", "--disable-default-apps",
            "--disable-features=Translate,msEdgeTranslate,msHubApps,msEdgeSidebarV2,EdgeCollections,AutofillServerCommunication",
            "--password-store=basic"]
    try:
        return subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, close_fds=True)
    except OSError:
        webbrowser.open(url)
        return None


def _front_existing() -> bool:
    """A second click on the Start-menu entry: the window that is already open comes to the front (one app, one window)."""
    info = welcome._load(home() / "app.json", {}) or {}
    port, key = int(info.get("port") or 0), str(info.get("key") or "")
    if not port or not key:
        return False
    try:
        req = urllib.request.Request(f"http://127.0.0.1:{port}/api/state", headers={"X-Sage-Key": key})
        with urllib.request.urlopen(req, timeout=2) as r:
            if r.status != 200:
                return False
    except Exception:  # noqa: BLE001
        return False
    if IS_WIN:
        try:
            import ctypes
            u = ctypes.windll.user32
            hwnd = u.FindWindowW(None, TITLE)
            if hwnd:
                u.ShowWindow(hwnd, 9)          # SW_RESTORE
                u.SetForegroundWindow(hwnd)
                return True
        except Exception:  # noqa: BLE001
            pass
    open_window(f"http://127.0.0.1:{port}/")   # the service is alive but its window is gone: a new window on it
    return True


def quit_running() -> bool:
    """Close the app when it is open: its service ends, its window closes (the installer and the uninstaller ask
    this before they touch the files it runs from). False when it was not open."""
    info = welcome._load(home() / "app.json", {}) or {}
    port, key = int(info.get("port") or 0), str(info.get("key") or "")
    was = False
    if port and key:
        try:
            req = urllib.request.Request(f"http://127.0.0.1:{port}/api/quit", data=b"{}", method="POST",
                                         headers={"X-Sage-Key": key, "Content-Type": "application/json"})
            with urllib.request.urlopen(req, timeout=3) as r:
                was = r.status == 200
        except Exception:  # noqa: BLE001
            pass
    if IS_WIN:
        try:
            import ctypes
            hwnd = ctypes.windll.user32.FindWindowW(None, TITLE)
            if hwnd:
                ctypes.windll.user32.PostMessageW(hwnd, 0x0010, 0, 0)      # WM_CLOSE
                was = True
        except Exception:  # noqa: BLE001
            pass
    if was:
        time.sleep(1.5)
    return was


def _packs_own_copy() -> Path | None:
    """The organization's folder carries its own copy of the engine, and that is the copy updates keep current. Once an
    organization exists the window runs from there, so the app is never older than the agent it shows."""
    pack = welcome.current_pack()
    mine = Path(__file__).resolve()
    theirs = (pack / "assistant" / "app.py") if pack else None
    if theirs and theirs.exists() and theirs.resolve() != mine and not os.environ.get("SAGE_APP_CHILD"):
        return theirs
    return None


def run(window: bool = True, port: int = 0) -> int:
    if window and _front_existing():
        return 0
    srv = ThreadingHTTPServer(("127.0.0.1", port), _Handler)    # port 0 (the default): the system picks a free one, nothing to collide with
    srv.daemon_threads = True
    port = srv.server_address[1]
    url = f"http://127.0.0.1:{port}/"
    try:
        welcome._save(home() / "app.json", {"port": port, "key": KEY, "pid": os.getpid(), "started": time.strftime("%Y-%m-%dT%H:%M:%S")})
    except OSError:
        pass
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    if not have_fonts():
        threading.Thread(target=fetch_fonts, daemon=True).start()
    print(f"APP {url}", flush=True)
    proc = open_window(url) if window else None
    _LAST_PING["t"] = time.time() + 30                           # a slow first paint is not an idle window
    try:
        while True:
            time.sleep(1)
            if _QUIT["now"]:
                break
            if window and proc is None and time.time() - _LAST_PING["t"] > IDLE_S and not running_jobs():
                break                                            # no window process to watch (a tab in the default browser): it is closed when it stops pinging
            if proc is not None and proc.poll() is not None and time.time() - _LAST_PING["t"] > 12 and not running_jobs():
                break                                            # the browser process ended and nothing has pinged since
    except KeyboardInterrupt:
        pass
    srv.shutdown()
    try:
        info = welcome._load(home() / "app.json", {}) or {}
        if info.get("pid") == os.getpid():
            (home() / "app.json").unlink()
    except OSError:
        pass
    return 0


def main() -> int:
    if sys.stdout is not None and hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--no-window", action="store_true", help="the service alone (prints APP <url>)")
    ap.add_argument("--port", type=int, default=0, help="a fixed port for the service (tests, a preview); default: the system picks")
    ap.add_argument("--dry", action="store_true", help="look only: starting, installing, signing in and opening links answer 'preview' and do nothing")
    ap.add_argument("--hold-agent", action="store_true", help="a trial: every set-up step is real, the agent is never started")
    ap.add_argument("--screen", default="", choices=["", "computer", "claude", "google", "home"], help="open the window on this screen")
    ap.add_argument("--state", action="store_true", help="print what the window would show and exit")
    ap.add_argument("--quit", action="store_true", help="close the app if it is open (the installer and the uninstaller ask this)")
    ap.add_argument("--fetch-fonts", nargs="?", const="", default=None, metavar="FOLDER", help="fetch the typefaces (into FOLDER, default the app's own) and exit")
    a = ap.parse_args()
    if a.state:
        print(json.dumps(state(), indent=1, ensure_ascii=False))
        return 0
    if a.fetch_fonts is not None:
        ok = fetch_fonts(Path(a.fetch_fonts) if a.fetch_fonts else None)
        print("fonts ready" if ok else "fonts not fetched (the window uses the system's own faces)")
        return 0 if ok else 1
    if a.quit:
        print("closed" if quit_running() else "was not open")
        return 0
    DRY["on"] = bool(a.dry)
    HOLD["on"] = bool(a.hold_agent)
    SCREEN["name"] = a.screen
    theirs = _packs_own_copy() if not (a.no_window or a.dry or a.port or a.hold_agent or a.screen) else None
    if theirs:
        env = dict(os.environ, SAGE_APP_CHILD="1", SAGE_HOME=str(home()))
        py = welcome.pack_python(welcome.current_pack())
        pyw = Path(py).with_name("pythonw.exe") if IS_WIN else Path(py)
        try:
            return subprocess.call([str(pyw if pyw.exists() else py), str(theirs)], env=env, cwd=str(home()))
        except OSError:
            pass                                                 # the organization's copy will not start: this one does
    return run(window=not a.no_window, port=a.port)


if __name__ == "__main__":
    sys.exit(main())

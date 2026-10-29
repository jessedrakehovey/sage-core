"""welcome.py - the first-run wizard and the Start-menu launcher (sage-core, 2026-10-28).

The owner-facing install (assistant/installer/) ends by opening a browser here. No terminal, no git: the page
asks the three things the sage-setup skill asks (the organization's name, what to call the agent, the owners
as name + email), creates the pack with new_org.py, shows the doctor table with a fix line per FAIL, opens the
one window the owner must type in themselves (the Claude token: a console that runs `claude setup-token` and
then stores the pasted result as a User environment variable; nothing secret ever passes through this page),
lists the Google clicks with one link each, and starts the supervisor (sage.py install-startup), handing over
to the agent's own page. That page serves the same wizard at /welcome for an installed organization, so the
checks, the token and the Google steps can be revisited from the phone.

  python assistant/welcome.py                 the wizard on http://127.0.0.1:9018 (opens the browser)
  python assistant/welcome.py --open          the launcher: the agent's page when the organization runs, else the wizard
  python assistant/welcome.py --port N --no-browser
  python assistant/welcome.py --uninstall-startup   (the installer's uninstaller: stop serve, drop the logon entry)

Stdlib only; everything that touches the pack runs the pack's own sage.py as a subprocess, under the pack's
interpreter (org.json -> python). Where the installer put things is read from SAGE_HOME (default
%LOCALAPPDATA%\\Sage on Windows, ~/.sage elsewhere): install.json (python, engine, release URL) and pack.json
(the organization folder this machine runs), both written here or by the installer, never by hand.
"""
from __future__ import annotations

import argparse
import json
import os
import platform
import subprocess
import sys
import threading
import time
import urllib.request
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

HERE = Path(__file__).resolve().parent          # assistant/ of the engine, or of a pack when served by the agent's own page
ROOT = HERE.parent
IS_WIN = os.name == "nt"
NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)
NEW_CONSOLE = getattr(subprocess, "CREATE_NEW_CONSOLE", 0)
PORT = 9018
ENV_TOKEN = "CLAUDE_CODE_OAUTH_TOKEN"
ENV_KEY = "ANTHROPIC_API_KEY"
_SERVER = {"stop_at": 0.0}


def _load(path: Path, default):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        return default


def _save(path: Path, obj) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(obj, indent=1, ensure_ascii=False) + "\n", encoding="utf-8")


# ---------------------------------------------------------------- where things are
def sage_home() -> Path:
    if os.environ.get("SAGE_HOME"):
        return Path(os.environ["SAGE_HOME"])
    if IS_WIN:
        return Path(os.environ.get("LOCALAPPDATA", str(Path.home() / "AppData" / "Local"))) / "Sage"
    return Path.home() / ".sage"


def install_info() -> dict:
    return _load(sage_home() / "install.json", {}) or {}


def is_pack(root: Path) -> bool:
    """An organization's folder: a manifest written by an install (engine.installed) and a named org.json. An engine
    checkout's manifest says exported instead, and its org.json carries placeholder names."""
    man = _load(root / "engine_manifest.json", {}) or {}
    org = _load(root / "assistant" / "org.json", {}) or {}
    eng = man.get("engine") or {}
    return bool(eng.get("installed") or eng.get("updated")) and bool(org.get("name"))


def current_pack() -> Path | None:
    if is_pack(ROOT):
        return ROOT
    p = str((_load(sage_home() / "pack.json", {}) or {}).get("pack") or "")
    if p and is_pack(Path(p)):
        return Path(p)
    return None


def engine_root() -> Path:
    if is_pack(ROOT):
        man = _load(ROOT / "engine_manifest.json", {}) or {}
        er = str((man.get("engine") or {}).get("engine_root") or "")
        if er and (Path(er) / "assistant" / "new_org.py").exists():
            return Path(er)
        er = str(install_info().get("engine") or "")
        if er and (Path(er) / "assistant" / "new_org.py").exists():
            return Path(er)
    return ROOT


def pack_python(pack: Path | None) -> str:
    if pack:
        py = os.path.expanduser(str((_load(pack / "assistant" / "org.json", {}) or {}).get("python") or ""))
        if py and Path(py).exists():
            return py
    py = str(install_info().get("python") or "")
    if py and Path(py).exists():
        return py
    return sys.executable


def documents_dir() -> Path:
    if IS_WIN:
        try:
            import ctypes.wintypes
            buf = ctypes.create_unicode_buffer(ctypes.wintypes.MAX_PATH)
            ctypes.windll.shell32.SHGetFolderPathW(None, 5, None, 0, buf)      # CSIDL_PERSONAL
            if buf.value:
                return Path(buf.value)
        except Exception:  # noqa: BLE001
            pass
    return Path.home() / "Documents"


def _user_env(name: str) -> str:
    if os.environ.get(name):
        return os.environ[name]
    if IS_WIN:
        try:
            r = subprocess.run(["reg", "query", r"HKCU\Environment", "/v", name], capture_output=True, text=True, timeout=10, creationflags=NO_WINDOW)
            if r.returncode == 0:
                return r.stdout.strip().splitlines()[-1].split(None, 2)[-1]
        except (OSError, subprocess.TimeoutExpired, IndexError):
            pass
    return ""


def claude_cli() -> str:
    import shutil
    for cand in (os.environ.get("ASSISTANT_CLAUDE", ""), shutil.which("claude") or "", shutil.which("claude.cmd") or "",
                 str(Path.home() / ".local" / "bin" / ("claude.exe" if IS_WIN else "claude")),
                 str(Path(os.environ.get("APPDATA", "")) / "npm" / "claude.cmd") if IS_WIN else "/usr/local/bin/claude"):
        if cand and Path(cand).exists():
            return cand
    return ""


def _http_ok(url: str, timeout: float = 2.0) -> bool:
    try:
        with urllib.request.urlopen(url, timeout=timeout) as r:
            return 200 <= r.status < 300
    except Exception:  # noqa: BLE001
        return False


def mc_url(pack: Path | None) -> str:
    port = 9019
    if pack:
        port = int((_load(pack / "assistant" / "org.json", {}) or {}).get("mission_control_port") or 9019)
    return f"http://127.0.0.1:{port}"


def _sage(pack: Path, args: list, timeout: int = 180, env_extra: dict | None = None) -> tuple:
    env = {k: v for k, v in os.environ.items() if k not in ("CLAUDECODE", "CLAUDE_CODE_ENTRYPOINT")}
    env["PYTHONIOENCODING"] = "utf-8"
    env.update(env_extra or {})
    try:
        r = subprocess.run([pack_python(pack), str(pack / "assistant" / "sage.py")] + args, capture_output=True, text=True, encoding="utf-8",
                           errors="replace", timeout=timeout, cwd=str(pack), env=env, creationflags=NO_WINDOW)
        return r.returncode, (r.stdout or "") + (r.stderr or "")
    except subprocess.TimeoutExpired:
        return 124, "timed out"
    except OSError as e:
        return 127, str(e)


def serve_running(pack: Path) -> bool:
    alive = _load(pack / "assistant" / "data" / "serve_alive.json", {}) or {}
    try:
        ts = time.mktime(time.strptime(str(alive.get("ts") or ""), "%Y-%m-%dT%H:%M:%S"))
        return time.time() - ts < 70
    except ValueError:
        return False


# ---------------------------------------------------------------- state
def state() -> dict:
    pack = current_pack()
    org = (_load(pack / "assistant" / "org.json", {}) or {}) if pack else {}
    prof = (_load(pack / "assistant" / "agent" / "profile.json", {}) or {}) if pack else {}
    man = (_load(pack / "engine_manifest.json", {}) or {}) if pack else {}
    info = install_info()
    tok, key = bool(_user_env(ENV_TOKEN)), bool(_user_env(ENV_KEY))
    billing = str(org.get("claude_billing") or "subscription")
    url = mc_url(pack)
    up = _http_ok(url + "/api/health") if pack else False
    return {"pack": str(pack) if pack else "", "org": str(org.get("name") or ""), "agent": str(prof.get("name") or ""),
            "owners": [u.get("name") for u in ((_load(pack / "assistant" / "agent" / "people.json", {}) or {}).get("users") or {}).values()] if pack else [],
            "python": pack_python(pack), "engine": str(engine_root()), "engine_version": str((man.get("engine") or {}).get("version") or info.get("version") or ""),
            "release_url": str(org.get("engine_release_url") or info.get("release_url") or ""), "home": str(sage_home()), "os": platform.system(),
            "mc_url": url, "mc_up": up, "serve_running": serve_running(pack) if pack else False, "claude_cli": bool(claude_cli()),
            "token_set": tok, "api_key_set": key, "billing": billing, "auth_ok": (key if billing == "api" else tok),
            "default_folder": str(documents_dir()), "can_create": not is_pack(ROOT) and (engine_root() / "assistant" / "new_org.py").exists(),
            "secrets_dir": os.path.expanduser(str(org.get("secrets_dir") or "")) if pack else "", "google_doc": str(pack / "assistant" / "agent" / "GOOGLE_SETUP.md") if pack else ""}


# ---------------------------------------------------------------- the steps
def create(body: dict) -> dict:
    if current_pack():
        return {"ok": False, "message": "this machine already runs an organization: " + str(current_pack())}
    org = str(body.get("org") or "").strip()
    agent = str(body.get("agent") or "").strip()
    owners = [(str(o.get("name") or "").strip(), str(o.get("email") or "").strip()) for o in (body.get("owners") or []) if isinstance(o, dict)]
    owners = [(n, e) for n, e in owners if n]
    if not org or not agent or not owners:
        return {"ok": False, "message": "the organization's name, the agent's name and at least one owner (name and email) are needed"}
    if any("@" not in e for _, e in owners):
        return {"ok": False, "message": "every owner needs an email address (that is how the agent knows who may approve)"}
    folder = Path(str(body.get("folder") or "").strip() or (documents_dir() / f"{org} OS"))
    eng = engine_root()
    if folder.exists() and any(folder.iterdir()):
        return {"ok": False, "message": f"{folder} is not empty; pick another folder"}
    if folder.resolve() == eng.resolve() or eng.resolve() in folder.resolve().parents:
        return {"ok": False, "message": "the organization must live in its own folder, not inside the engine"}
    py = pack_python(None)
    args = [py, str(eng / "assistant" / "new_org.py"), org, "--agent", agent, "--out", str(folder)]
    for n, e in owners:
        args += ["--owner", f"{n}:{e}"]
    if str(body.get("what") or "").strip():
        args += ["--what", str(body["what"]).strip()[:300]]
    try:
        r = subprocess.run(args, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=600, cwd=str(eng), creationflags=NO_WINDOW)
    except subprocess.TimeoutExpired:
        return {"ok": False, "message": "creating the organization took too long"}
    if r.returncode != 0 or not (folder / "assistant" / "org.json").exists():
        return {"ok": False, "message": "could not create the organization: " + ((r.stderr or r.stdout).strip()[-400:] or f"exit {r.returncode}")}
    oj = folder / "assistant" / "org.json"
    o = _load(oj, {}) or {}
    o["python"] = py
    info = install_info()
    if info.get("release_url") or os.environ.get("SAGE_RELEASE_URL"):
        o["engine_release_url"] = str(os.environ.get("SAGE_RELEASE_URL") or info.get("release_url"))
    _save(oj, o)
    _save(sage_home() / "pack.json", {"pack": str(folder), "org": org, "agent": agent, "python": py, "engine": str(eng), "created": time.strftime("%Y-%m-%dT%H:%M:%S")})
    return {"ok": True, "message": f"{agent} is set up for {org} in {folder}", "pack": str(folder)}


def doctor() -> dict:
    pack = current_pack()
    if not pack:
        return {"ok": False, "message": "no organization yet", "rows": []}
    rc, out = _sage(pack, ["doctor", "--no-probe", "--json", "--fix"], timeout=240)
    try:
        rows = json.loads(out[out.index("["):])
    except ValueError:
        return {"ok": False, "message": "doctor did not answer: " + out.strip()[-300:], "rows": []}
    fails = [r for r in rows if r.get("status") == "fail"]
    return {"ok": True, "rows": rows, "fails": len(fails), "message": "everything this machine needs is in place" if not fails else f"{len(fails)} thing(s) still need doing"}


def token_window(mode: str) -> dict:
    pack = current_pack()
    if not pack:
        return {"ok": False, "message": "no organization yet"}
    if not IS_WIN:
        return {"ok": False, "message": f"open a terminal and run: \"{pack_python(pack)}\" \"{pack / 'assistant' / 'sage.py'}\" token" + (" --api-key" if mode == "api" else "")}
    if mode != "api" and not claude_cli():
        return {"ok": False, "message": "the Claude Code CLI is not installed yet (the doctor row 'claude cli' has the fix); install it, then try again"}
    argv = [pack_python(pack), str(pack / "assistant" / "sage.py"), "token"] + (["--api-key"] if mode == "api" else [])
    quoted = " ".join(f'"{a}"' if " " in a else a for a in argv)
    try:
        subprocess.Popen(["cmd", "/k", f"title Sage: your Claude {'API key' if mode == 'api' else 'token'} && cd /d \"{pack}\" && {quoted}"],
                         cwd=str(pack), creationflags=NEW_CONSOLE, close_fds=True)
    except OSError as e:
        return {"ok": False, "message": f"could not open the window: {e}"}
    return {"ok": True, "message": "A black window opened on this PC. Finish there (sign in, then paste the token when it asks), close it, and tap Check."}


def probe() -> dict:
    pack = current_pack()
    if not pack:
        return {"ok": False, "message": "no organization yet"}
    rc, out = _sage(pack, ["probe"], timeout=150)
    line = next((ln for ln in out.strip().splitlines()[::-1] if ln.startswith("PROBE")), out.strip()[-200:])
    return {"ok": rc == 0 and "PROBE ok" in line, "message": line}


def start() -> dict:
    pack = current_pack()
    if not pack:
        return {"ok": False, "message": "no organization yet"}
    url = mc_url(pack)
    if _http_ok(url + "/api/health") and serve_running(pack):
        return {"ok": True, "message": "already running", "mc_url": url}
    rc, out = _sage(pack, ["install-startup"], timeout=120)
    started = "startup entry" if rc == 0 else ""
    if not serve_running(pack):
        time.sleep(3)
    if not serve_running(pack):
        py = pack_python(pack)
        exe = Path(py)
        pyw = exe.with_name("pythonw.exe") if IS_WIN else exe
        flags = (0x00000008 | 0x00000200) if IS_WIN else 0
        try:
            subprocess.Popen([str(pyw if pyw.exists() else exe), str(pack / "assistant" / "sage.py"), "serve"], cwd=str(pack), creationflags=flags, close_fds=True,
                             stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=not IS_WIN)
            started = (started + " + " if started else "") + "serve started"
        except OSError as e:
            return {"ok": False, "message": f"could not start the supervisor: {e}; install-startup said: {out.strip()[-200:]}"}
    for _ in range(45):
        if _http_ok(url + "/api/health"):
            _SERVER["stop_at"] = time.time() + 8
            return {"ok": True, "message": f"running ({started or 'serve was already up'}); the agent's page is {url}", "mc_url": url}
        time.sleep(1)
    return {"ok": False, "message": f"the supervisor started ({started}) but its page did not answer within 45 s at {url}; "
                                    f"install-startup said: {out.strip()[-200:]}", "mc_url": url}


def uninstall_startup() -> dict:
    pack = current_pack()
    if not pack:
        return {"ok": True, "message": "no organization registered on this machine"}
    _sage(pack, ["stop"], timeout=30)
    rc, out = _sage(pack, ["uninstall-startup"], timeout=60)
    return {"ok": rc == 0, "message": out.strip()[-200:]}


# ---------------------------------------------------------------- the HTTP surface (standalone, or mounted by mc_server)
def handle(method: str, path: str, body: dict) -> tuple:
    """(status, body, content-type). GET /welcome -> the page; GET /api/welcome/state; POST /api/welcome/<step>."""
    if method == "GET" and path in ("/", "/welcome", "/welcome/"):
        return 200, PAGE, "text/html"
    if method == "GET" and path == "/api/welcome/state":
        return 200, json.dumps(state(), ensure_ascii=False), "application/json"
    if method == "POST" and path.startswith("/api/welcome/"):
        step = path.rsplit("/", 1)[-1]
        fn = {"create": lambda: create(body), "doctor": doctor, "token": lambda: token_window(str(body.get("mode") or "subscription")),
              "probe": probe, "start": start}.get(step)
        if fn is None:
            return 404, '{"ok":false,"message":"no such step"}', "application/json"
        return 200, json.dumps(fn(), ensure_ascii=False), "application/json"
    return 404, '{"ok":false,"message":"not found"}', "application/json"


class _Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):  # quiet
        pass

    def _send(self, code: int, body: str, ctype: str) -> None:
        data = body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype + "; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def _same_site(self) -> bool:
        if "application/json" not in (self.headers.get("Content-Type") or "").lower():
            return False
        origin = self.headers.get("Origin")
        return not origin or origin.split("://", 1)[-1].rstrip("/") == (self.headers.get("Host") or "")

    def do_GET(self):
        p = self.path.split("?")[0]
        code, body, ctype = handle("GET", p, {})
        self._send(code, body, ctype)

    def do_POST(self):
        p = self.path.split("?")[0]
        if not self._same_site():
            return self._send(403, '{"ok":false,"message":"cross-site request refused"}', "application/json")
        n = int(self.headers.get("Content-Length") or 0)
        try:
            body = json.loads(self.rfile.read(min(n, 64 * 1024)).decode("utf-8") or "{}") if n else {}
        except ValueError:
            body = {}
        try:
            code, out, ctype = handle("POST", p, body if isinstance(body, dict) else {})
        except Exception as e:  # noqa: BLE001
            code, out, ctype = 500, json.dumps({"ok": False, "message": f"{type(e).__name__}: {e}"}), "application/json"
        self._send(code, out, ctype)


def serve(port: int = PORT, open_browser: bool = True, idle_s: int = 7200) -> int:
    srv = ThreadingHTTPServer(("127.0.0.1", port), _Handler)
    srv.daemon_threads = True
    url = f"http://127.0.0.1:{port}/welcome"
    print(f"WELCOME {url}", flush=True)
    if open_browser:
        threading.Timer(0.5, lambda: webbrowser.open(url)).start()
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    end = time.time() + idle_s
    try:
        while time.time() < end:
            time.sleep(1)
            if _SERVER["stop_at"] and time.time() >= _SERVER["stop_at"]:
                break
    except KeyboardInterrupt:
        pass
    srv.shutdown()
    return 0


def launch(port: int = PORT) -> int:
    """The Start-menu entry: the agent's page when the organization runs; start it when it does not; the wizard otherwise."""
    pack = current_pack()
    if pack and _http_ok(mc_url(pack) + "/api/health"):
        webbrowser.open(mc_url(pack) + "/")
        return 0
    if pack and state()["auth_ok"]:
        r = start()
        if r.get("ok"):
            webbrowser.open(r["mc_url"] + "/")
            return 0
    return serve(port, open_browser=True)


def main() -> int:
    sys.stdout.reconfigure(encoding="utf-8")
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--open", action="store_true", help="the launcher: the agent's page if it runs, else start it, else the wizard")
    ap.add_argument("--port", type=int, default=PORT)
    ap.add_argument("--no-browser", action="store_true")
    ap.add_argument("--state", action="store_true", help="print the wizard's state as JSON and exit")
    ap.add_argument("--uninstall-startup", action="store_true")
    a = ap.parse_args()
    if a.state:
        print(json.dumps(state(), indent=1, ensure_ascii=False))
        return 0
    if a.uninstall_startup:
        r = uninstall_startup()
        print(("ok: " if r["ok"] else "FAILED: ") + r["message"])
        return 0 if r["ok"] else 1
    if a.open:
        return launch(a.port)
    return serve(a.port, open_browser=not a.no_browser)


PAGE = r"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1"><title>Welcome</title>
<style>
:root{--bg:#050B07;--panel:#0d1610;--ink:#FBFFFA;--mute:#9aa69a;--sage:#4A5C43;--line:#1c2a1f;--bad:#b4453f;--warn:#c9a44a;--ok:#7fbf6a}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--ink);font:15px/1.5 Poppins,system-ui,-apple-system,Segoe UI,sans-serif}
header{display:flex;align-items:center;gap:10px;padding:14px 20px;border-bottom:1px solid var(--line);background:var(--panel)}
header h1{font:600 22px "Playfair Display",Georgia,serif;margin:0;flex:1}header span{color:var(--mute);font-size:13px}
.wrap{display:grid;grid-template-columns:220px 1fr;gap:18px;max-width:980px;margin:0 auto;padding:18px 16px 60px}
@media (max-width:720px){.wrap{grid-template-columns:1fr}}
nav button{display:block;width:100%;text-align:left;background:none;border:1px solid var(--line);color:var(--mute);border-radius:10px;padding:10px 12px;margin-bottom:8px;font:inherit;font-size:14px;cursor:pointer}
nav button.on{background:var(--sage);color:var(--ink);border-color:var(--sage)}nav button.done::after{content:" ✓";color:var(--ok)}
.card{background:var(--panel);border:1px solid var(--line);border-radius:14px;padding:18px 20px;margin-bottom:14px}
h2{font:600 19px "Playfair Display",Georgia,serif;margin:0 0 8px}p{margin:6px 0;color:#d8e0d6}.mute{color:var(--mute);font-size:13px}
label{display:block;font-size:13px;color:var(--mute);margin:10px 0 4px}input{width:100%;background:#0a120c;border:1px solid var(--line);color:var(--ink);border-radius:9px;padding:9px 11px;font:inherit}
.owners .row{display:grid;grid-template-columns:1fr 1fr auto;gap:8px;margin-bottom:6px}
button.go{background:var(--sage);color:var(--ink);border:0;border-radius:10px;padding:10px 16px;font:500 14px inherit;cursor:pointer;margin:12px 8px 0 0}
button.sec{background:none;border:1px solid var(--line);color:var(--mute)}button:disabled{opacity:.5}
.res{margin-top:10px;font-size:13px;white-space:pre-wrap;color:#cfe3c7}.res.err{color:#e8a39f}
table{width:100%;border-collapse:collapse;font-size:13px;margin-top:8px}td{padding:6px 6px;border-top:1px solid var(--line);vertical-align:top}
.st{font-weight:600;width:52px}.st.ok{color:var(--ok)}.st.fail{color:var(--bad)}.st.warn{color:var(--warn)}.st.info{color:var(--mute)}
.fix{color:var(--mute);font-size:12px}code{background:#111c14;padding:1px 5px;border-radius:5px;font-size:12px}
ol li{margin:8px 0}a{color:#9DB392}
</style></head><body>
<header><h1 id="title">Welcome</h1><span id="sub"></span></header>
<div class="wrap"><nav id="nav"></nav><main id="main"></main></div>
<script>
let S = {}, step = null;
const STEPS = [["create","1 · Your organization"],["doctor","2 · This computer"],["token","3 · Your Claude account"],["google","4 · Google"],["start","5 · Start"]];
const el = (t, c, txt) => { const d = document.createElement(t); if (c) d.className = c; if (txt !== undefined) d.textContent = txt; return d; };
async function api(path, body) {
  const r = await fetch("/api/welcome/" + path, body === undefined ? {cache: "no-store"} : {method: "POST", headers: {"Content-Type": "application/json"}, body: JSON.stringify(body)});
  return r.json();
}
function done(k) {
  if (k === "create") return !!S.pack; if (k === "token") return !!S.auth_ok; if (k === "start") return S.mc_up && S.serve_running;
  if (k === "doctor") return S._doctor_ok === true; return false;
}
function firstOpen() { for (const [k] of STEPS) { if (!done(k)) return k; } return "start"; }
function drawNav() {
  const n = document.getElementById("nav"); n.innerHTML = "";
  for (const [k, label] of STEPS) { const b = el("button", (k === step ? "on" : "") + (done(k) ? " done" : ""), label); b.onclick = () => { step = k; draw(); }; n.appendChild(b); }
}
function res(id, msg, bad) { const r = document.getElementById(id); r.textContent = msg; r.className = "res" + (bad ? " err" : ""); }
function draw() {
  drawNav();
  document.getElementById("title").textContent = S.agent ? ("Welcome, this is " + S.agent) : "Welcome";
  document.getElementById("sub").textContent = S.org ? (S.org + " · engine " + (S.engine_version || "")) : ("engine " + (S.engine_version || ""));
  const m = document.getElementById("main"); m.innerHTML = "";
  const c = el("div", "card"); m.appendChild(c);
  if (step === "create") {
    c.appendChild(el("h2", "", S.pack ? "Your organization" : "Three things, then the agent exists"));
    if (S.pack) { c.appendChild(el("p", "", S.org + " runs from " + S.pack + ". Owners: " + (S.owners || []).join(", ") + ".")); return; }
    if (!S.can_create) { c.appendChild(el("p", "", "This page is served from an engine folder without new_org.py; run the installer again or `python assistant/new_org.py` by hand.")); return; }
    c.appendChild(el("p", "mute", "Nothing here leaves this computer. The agent drafts everything and sends nothing without an owner's tap; that is built in, not a setting."));
    const f = el("div"); c.appendChild(f);
    f.innerHTML = '<label>The organization\'s name</label><input id="org" placeholder="e.g. Acme Bakery">' +
      '<label>What to call the agent (your choice; it is your employee\'s name)</label><input id="agent" placeholder="e.g. Pip">' +
      '<label>One line on what the organization does (optional)</label><input id="what" placeholder="e.g. a bakery with wholesale accounts">' +
      '<label>Owners: the people who may approve (full name and email, one per row)</label><div class="owners" id="owners"></div>' +
      '<label>Folder for the organization\'s files</label><input id="folder">';
    const ow = f.querySelector("#owners");
    const addRow = (n, e) => { const r = el("div", "row"); r.innerHTML = '<input placeholder="Full name"><input placeholder="email@example.com">'; const x = el("button", "sec", "−"); x.onclick = () => r.remove(); r.appendChild(x); ow.appendChild(r); if (n) r.children[0].value = n; if (e) r.children[1].value = e; };
    addRow(); const add = el("button", "go sec", "+ another owner"); add.onclick = () => addRow(); c.appendChild(add);
    const org = f.querySelector("#org"), folder = f.querySelector("#folder");
    org.oninput = () => { folder.value = S.default_folder + (S.os === "Windows" ? "\\" : "/") + (org.value.trim() || "Org") + " OS"; }; org.oninput();
    const go = el("button", "go", "Create the agent"); c.appendChild(go); const r = el("div", "res"); r.id = "r-create"; c.appendChild(r);
    go.onclick = async () => {
      const owners = [...ow.querySelectorAll(".row")].map(x => ({name: x.children[0].value.trim(), email: x.children[1].value.trim()})).filter(o => o.name || o.email);
      go.disabled = true; res("r-create", "Creating… (half a minute)");
      const d = await api("create", {org: org.value, agent: f.querySelector("#agent").value, what: f.querySelector("#what").value, owners, folder: folder.value});
      res("r-create", d.message, !d.ok); go.disabled = false; if (d.ok) { await load(false); step = "doctor"; draw(); runDoctor(); }
    };
  } else if (step === "doctor") {
    c.appendChild(el("h2", "", "What this computer still needs"));
    c.appendChild(el("p", "mute", "Each FAIL has its fix on the line below it. WARN and info rows are fine for now."));
    const b = el("button", "go", "Run the checks"); b.onclick = runDoctor; c.appendChild(b);
    const r = el("div", "res"); r.id = "r-doctor"; c.appendChild(r); const t = el("div"); t.id = "doctor"; c.appendChild(t);
    if (S._rows) drawRows(S._rows);
  } else if (step === "token") {
    c.appendChild(el("h2", "", "The agent thinks with your Claude account"));
    c.appendChild(el("p", "", "Its runs count against your own Claude subscription. Tokens are never typed on this page: tap the button, a black window opens on this PC, you sign in there and paste the token where it asks. Nothing is stored anywhere but your own Windows account."));
    c.appendChild(el("p", "mute", "Status: " + (S.claude_cli ? "Claude Code CLI installed" : "Claude Code CLI NOT installed (see the checks)") + " · " + (S.token_set ? "token stored" : "no token yet") + (S.api_key_set ? " · API key stored" : "") + " · billing: " + S.billing));
    const b1 = el("button", "go", "Open the token window"); b1.onclick = async () => { b1.disabled = true; const d = await api("token", {mode: "subscription"}); res("r-token", d.message, !d.ok); b1.disabled = false; }; c.appendChild(b1);
    const b2 = el("button", "go sec", "Check"); b2.onclick = async () => { b2.disabled = true; res("r-token", "Asking Claude for one word… (up to two minutes)"); const d = await api("probe", {}); res("r-token", d.message, !d.ok); b2.disabled = false; await load(false); drawNav(); }; c.appendChild(b2);
    const b3 = el("button", "go sec", "I pay by API key instead"); b3.onclick = async () => { const d = await api("token", {mode: "api"}); res("r-token", d.message, !d.ok); }; c.appendChild(b3);
    const r = el("div", "res"); r.id = "r-token"; c.appendChild(r);
  } else if (step === "google") {
    c.appendChild(el("h2", "", "Google: the clicks that are yours alone"));
    c.appendChild(el("p", "mute", "Mail and calendar work with any Google account. The Chat surface (talking to the agent from your phone) needs Google Workspace. Everything else keeps working meanwhile: the agent's page, the checks, the drafts."));
    const ol = el("ol"); c.appendChild(ol);
    const items = [
      ['Google Workspace (for Chat; skip if you have it): <a target="_blank" href="https://workspace.google.com/">workspace.google.com</a>. About a day if a domain must be verified.'],
      ['A Cloud project for the agent: <a target="_blank" href="https://console.cloud.google.com/projectcreate">create project</a>, then enable the <a target="_blank" href="https://console.cloud.google.com/apis/library/gmail.googleapis.com">Gmail API</a> and the <a target="_blank" href="https://console.cloud.google.com/apis/library/calendar-json.googleapis.com">Calendar API</a>.'],
      ['The sign-in screen: <a target="_blank" href="https://console.cloud.google.com/auth/overview">Google Auth Platform</a> → Get started (app name = the agent, audience External, then Publish). A Desktop client under <a target="_blank" href="https://console.cloud.google.com/auth/clients">Clients</a>; download its JSON and save it as <code>client_secret.json</code> in ' + (S.secrets_dir ? '<code>' + S.secrets_dir + '</code>' : 'the secrets folder (org.json → secrets_dir)') + '.'],
      ['Each owner signs in once: on ' + (S.agent || 'the agent') + '\'s page → <a target="_blank" href="' + S.mc_url + '/setup">Setup</a> → the Google row with your name → Sign in (read-only mail and calendar; no send scope exists on these tokens).'],
      ['Chat (Workspace account): the <a target="_blank" href="https://console.cloud.google.com/apis/library/chat.googleapis.com">Google Chat API</a> → Configuration (the app, its name and avatar), a Pub/Sub topic + pull subscription, a service account key into the secrets folder. The runbook is <code>' + (S.google_doc || 'assistant/agent/GOOGLE_SETUP.md') + '</code>, Parts 2 and 3; the chat tells you what it still needs.'],
      ['Card buttons without any public address: the Apps Script relay in pull mode (<code>assistant/agent/gchat_relay/README.md</code>, about 15 minutes). Until then cards carry their choices as words you answer in the thread.']
    ];
    for (const [h] of items) { const li = el("li"); li.innerHTML = h; ol.appendChild(li); }
    c.appendChild(el("p", "mute", "Nothing here is required to start: the next step runs the agent and these can follow later, from the phone."));
  } else {
    c.appendChild(el("h2", "", S.mc_up ? "Running" : "Start the agent"));
    c.appendChild(el("p", "", "One process runs everything (the schedule, the message probe, the agent's own runs, the watchdog and the agent's page) and starts again at every logon. Pause or stop it any time from that page."));
    const b = el("button", "go", S.mc_up ? ("Open " + (S.agent || "the agent") + "'s page") : ("Start " + (S.agent || "the agent"))); c.appendChild(b);
    const r = el("div", "res"); r.id = "r-start"; c.appendChild(r);
    b.onclick = async () => { if (S.mc_up) { location.href = S.mc_url + "/"; return; } b.disabled = true; res("r-start", "Starting… (up to a minute)"); const d = await api("start", {}); res("r-start", d.message, !d.ok); b.disabled = false; if (d.ok) setTimeout(() => { location.href = d.mc_url + "/"; }, 1500); };
    if (!S.auth_ok) c.appendChild(el("p", "mute", "Note: without the Claude token (step 3) the agent runs its schedule but cannot think yet; add the token any time and it picks up."));
  }
}
function drawRows(rows) {
  const t = document.getElementById("doctor"); if (!t) return; t.innerHTML = ""; const tb = el("table");
  for (const r of rows) { const tr = el("tr"); tr.appendChild(el("td", "st " + r.status, r.status.toUpperCase())); const td = el("td"); td.appendChild(el("div", "", r.check + " · " + r.detail)); if ((r.status === "fail" || r.status === "warn") && r.fix) td.appendChild(el("div", "fix", "→ " + r.fix)); tr.appendChild(td); tb.appendChild(tr); }
  t.appendChild(tb);
}
async function runDoctor() {
  res("r-doctor", "Checking… (half a minute)"); const d = await api("doctor", {}); S._rows = d.rows || []; S._doctor_ok = d.ok && d.fails === 0;
  res("r-doctor", d.message, !d.ok || d.fails > 0); drawRows(S._rows); drawNav();
}
async function load(redraw = true) { const keep = {_rows: S._rows, _doctor_ok: S._doctor_ok}; S = Object.assign(await api("state"), keep); if (!step) step = firstOpen(); if (redraw) draw(); }
load();
</script></body></html>
"""


if __name__ == "__main__":
    sys.exit(main())

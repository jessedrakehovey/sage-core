"""sage.py - the one command an organization runs (sage-core, 2026-10-27).

Until this file the agent was a dozen Windows scheduled tasks (a spine every 10 min, a probe every minute, the
operator every 30, a deadman, Mission Control's keepalive, dreaming at 06:30), each a UTF-16 task XML naming this
PC's folders. One supervisor process replaces them: the same jobs, the same cadences, read from
assistant/serve.json, run from one loop that any OS can start at logon. Stdlib only. Zero LLM of its own.

  python assistant/sage.py doctor [--json] [--no-probe] [--fix]   what this machine still needs, with the fix for each
  python assistant/sage.py serve [--once] [--dry-run]              the supervisor (foreground; install-startup makes it a service)
  python assistant/sage.py status | health [--json] [--post]       alive? / the three-line daily report (did, failed, needs you)
  python assistant/sage.py pause | resume | stop                   hold every job but the deadman and Mission Control / let go / end serve
  python assistant/sage.py install-startup | uninstall-startup     start `serve` at logon (schtasks / launchd / systemd --user)
  python assistant/sage.py update [--engine DIR] [--check] [--force] [--dry-run]   a newer engine; pack files never touched.
                                                                   No --engine: the release feed (assistant/updater.py)
  python assistant/sage.py rollback                                the previous engine back (one step)
  python assistant/sage.py token [--api-key] | probe               the owner's Claude token (a console; never printed) / one headless turn
  python assistant/sage.py welcome [--open]                        the first-run wizard / the launcher (assistant/welcome.py)
  python assistant/sage.py module on|off <name>                    switch an optional engine module (assistant/engine.py MODULES)
  python assistant/sage.py new-org ... | export-engine DST [--git] | release [...] | install-skill | uninstall

Pause and stop are files under assistant/data (.sage_paused, .sage_stop) so a phone page or a card can flip them.
Every job runs with ASSISTANT_PY (this interpreter) and ASSISTANT_CLAUDE (the Claude CLI found on this machine)
in its environment, so no wrapper needs this PC's paths.
"""
from __future__ import annotations

import argparse
import json
import os
import platform
import shutil
import subprocess
import sys
import threading
import time
import urllib.request
from datetime import datetime
from pathlib import Path

HERE = Path(__file__).resolve().parent          # assistant/
ROOT = HERE.parent                               # the organization's folder
sys.path.insert(0, str(HERE))
import orgcfg  # noqa: E402

DATA = Path(os.environ["ASSISTANT_DATA"]) if os.environ.get("ASSISTANT_DATA") else HERE / "data"
SERVE_JOBS = HERE / "serve.json"
STATE = DATA / "serve_state.json"
ALIVE = DATA / "serve_alive.json"
PID = DATA / "sage_serve.pid"
PAUSED = DATA / ".sage_paused"
STOP = DATA / ".sage_stop"
RESTART = DATA / ".sage_restart"          # the updater asks for a restart; the loop re-execs itself on the new code
LOG = Path(os.environ.get("WV_LOG_DIR") or (Path.home() / ".claude")) / "sage-serve.log"
MC_URL = f"http://127.0.0.1:{int(os.environ.get('MC_PORT') or orgcfg.get('mission_control_port', 9019) or 9019)}"
GCHAT_LOCAL = HERE / "agent" / "gchat.local.json"
TICK_S = 20
IS_WIN = os.name == "nt"
NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)      # every child process runs hidden; 0 off Windows


def _slug() -> str:
    import re
    return re.sub(r"[^a-z0-9]+", "-", str(orgcfg.get("name", "sage")).lower()).strip("-") or "sage"


def _log(msg: str) -> None:
    line = time.strftime("%Y-%m-%d %H:%M:%S") + "  " + msg
    print(line, flush=True)
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
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(obj, indent=1, ensure_ascii=False), encoding="utf-8")
    for i in range(6):
        try:
            os.replace(tmp, path)
            return
        except PermissionError:
            time.sleep(0.2 * (i + 1))
    os.replace(tmp, path)


def claude_cli() -> str:
    """The Claude Code CLI on this machine: ASSISTANT_CLAUDE, else PATH, else the npm global folder."""
    for cand in (os.environ.get("ASSISTANT_CLAUDE", ""), shutil.which("claude") or "", shutil.which("claude.cmd") or "",
                 str(Path.home() / ".local" / "bin" / ("claude.exe" if IS_WIN else "claude")),         # the native installer (no Node)
                 str(Path(os.environ.get("APPDATA", "")) / "npm" / "claude.cmd") if IS_WIN else "/usr/local/bin/claude"):
        if cand and Path(cand).exists():
            return cand
    return ""


def jobs_python() -> str:
    """The interpreter the jobs' secrets and browser code run in: org.json -> python, else ASSISTANT_PY, else the
    venv alib knows (this machine's), else this interpreter. The same resolution alib.VENV_PY uses."""
    p = os.path.expanduser(str(orgcfg.get("python") or "")) or os.environ.get("ASSISTANT_PY") or ""
    if p and Path(p).exists():
        return p
    try:
        sys.path.insert(0, str(HERE / "scripts"))
        from alib import VENV_PY
        if VENV_PY.exists():
            return str(VENV_PY)
    except Exception:  # noqa: BLE001
        pass
    return sys.executable


def child_env() -> dict:
    env = dict(os.environ)
    env.setdefault("ASSISTANT_PY", jobs_python())
    c = claude_cli()
    if c:
        env.setdefault("ASSISTANT_CLAUDE", c)
    env.setdefault("PYTHONIOENCODING", "utf-8")
    return env


def _http_ok(url: str, timeout: float = 3.0) -> bool:
    try:
        with urllib.request.urlopen(url, timeout=timeout) as r:
            return 200 <= r.status < 300
    except Exception:  # noqa: BLE001
        return False


# ---------------------------------------------------------------- serve
def _in_window(job: dict, now: datetime) -> bool:
    win = job.get("window")
    if not win:
        return True
    h = now.hour + now.minute / 60
    a, b = float(win[0]), float(win[1])
    return a <= h < b if a < b else (h >= a or h < b)


def _due(job: dict, st: dict, now: datetime) -> bool:
    last_s = str(st.get("last") or "")
    try:
        last = datetime.strptime(last_s, "%Y-%m-%dT%H:%M:%S") if last_s else datetime(1970, 1, 1)
    except ValueError:
        last = datetime(1970, 1, 1)
    if job.get("at"):
        hh, mm = str(job["at"]).split(":")
        target = now.replace(hour=int(hh), minute=int(mm), second=0, microsecond=0)
        return now >= target and last < target
    return (now - last).total_seconds() >= float(job.get("every_min", 10)) * 60 - 5


def _platform_ok(job: dict) -> bool:
    if job.get("windows_only") and not IS_WIN:
        return False
    return True


def _cmd(job: dict) -> list:
    out = []
    for c in job.get("cmd", []):
        if c == "python":
            out.append(sys.executable)
        elif c == "powershell" and IS_WIN:
            out.append("powershell.exe")
        else:
            out.append(c)
    return out


def _mc_keepalive(dry: bool) -> tuple:
    if _http_ok(MC_URL + "/api/health"):
        return True, "mission control up"
    if dry:
        return True, "DRY would start mc_server.py"
    exe = Path(sys.executable)
    pyw = exe.with_name("pythonw.exe") if IS_WIN else exe
    flags = 0x00000008 | 0x00000200 if IS_WIN else 0
    try:
        subprocess.Popen([str(pyw if pyw.exists() else exe), str(HERE / "scripts" / "mc_server.py")], cwd=str(ROOT), env=child_env(),
                         creationflags=flags, close_fds=True, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                         start_new_session=not IS_WIN)
        return True, "started mc_server.py"
    except OSError as e:
        return False, f"could not start mc_server.py: {e}"


BUILTINS = {"mc_keepalive": _mc_keepalive}


def run_job(job: dict, dry: bool) -> tuple:
    if job.get("builtin"):
        return BUILTINS[job["builtin"]](dry)
    cmd = _cmd(job)
    if dry:
        return True, "DRY " + " ".join(cmd)
    try:
        # hidden by default (the owner 2026-10-28): serve runs under pythonw with no console, so a console child (python,
        # powershell) would open and flash its own window on every job without this flag
        r = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=int(job.get("timeout_s", 600)),
                           cwd=str(ROOT), env=child_env(), creationflags=NO_WINDOW)
    except subprocess.TimeoutExpired:
        return False, "timeout"
    except OSError as e:
        return False, f"could not start: {e}"
    out = ((r.stdout or "") + (r.stderr or "")).strip()
    last = out.splitlines()[-1] if out else ""
    exp = str(job.get("expect") or "")
    if r.returncode == 0 and exp and not any(ln.strip().startswith(exp) for ln in out.splitlines()[-3:]):
        return False, ("silent: exit 0 without a " + exp + " line: " + last[:200])[:300]
    return r.returncode == 0, last[:300]


class Serve:
    def __init__(self, dry: bool = False):
        self.dry = dry
        self.jobs = [j for j in (_load(SERVE_JOBS, {}) or {}).get("jobs", []) if j.get("enabled", True)]
        self.state = _load(STATE, {})
        self.locks = {j["name"]: threading.Lock() for j in self.jobs}
        self.threads = {}

    def paused(self) -> bool:
        return PAUSED.exists()

    def heartbeat(self) -> None:
        _save(ALIVE, {"ts": time.strftime("%Y-%m-%dT%H:%M:%S"), "pid": os.getpid(), "paused": self.paused(), "jobs": len(self.jobs),
                      "running": [n for n, t in self.threads.items() if t.is_alive()]})

    def _run(self, job: dict) -> None:
        name = job["name"]
        if not self.locks[name].acquire(blocking=False):
            return
        try:
            t0 = time.time()
            ok, result = run_job(job, self.dry)
            self.state[name] = {"last": time.strftime("%Y-%m-%dT%H:%M:%S"), "ok": ok, "result": result, "secs": round(time.time() - t0, 1)}
            if not self.dry:
                _save(STATE, self.state)          # a dry pass stamps nothing: the next real pass still owes every job
            _log(f"{name}: {'ok' if ok else 'FAILED'} ({self.state[name]['secs']}s) {result}")
        finally:
            self.locks[name].release()

    def pass_once(self, block: bool = False) -> list:
        now = datetime.now()
        started = []
        for job in self.jobs:
            name = job["name"]
            if not _platform_ok(job) or not _in_window(job, now) or not _due(job, self.state.get(name, {}), now):
                continue
            if self.paused() and not job.get("even_paused"):
                continue
            if block:
                self._run(job)
            else:
                t = self.threads.get(name)
                if t and t.is_alive():
                    continue
                t = threading.Thread(target=self._run, args=(job,), name=name, daemon=True)
                self.threads[name] = t
                t.start()
            started.append(name)
        return started

    def loop(self) -> int:
        _save(PID, {"pid": os.getpid(), "started": time.strftime("%Y-%m-%dT%H:%M:%S"), "python": sys.executable})
        if STOP.exists():
            STOP.unlink()
        _log(f"serve started: {len(self.jobs)} jobs, root {ROOT}, claude {claude_cli() or 'NOT FOUND'}")
        try:
            while True:
                if STOP.exists():
                    _log("serve: stop file seen, exiting")
                    STOP.unlink()
                    return 0
                if RESTART.exists():
                    # the updater (or a rollback) replaced engine files on disk; this process still runs the old code
                    why = RESTART.read_text(encoding="utf-8", errors="replace").strip()[:120]
                    RESTART.unlink()
                    _log(f"serve: restart requested ({why}); re-executing on the new code")
                    self.heartbeat()
                    os.execv(sys.executable, [sys.executable, str(HERE / "sage.py"), "serve"] + (["--dry-run"] if self.dry else []))
                self.pass_once()
                self.heartbeat()
                time.sleep(TICK_S)
        except KeyboardInterrupt:
            _log("serve: interrupted")
            return 0
        finally:
            try:
                PID.unlink()
            except OSError:
                pass


def serve_pid() -> int:
    d = _load(PID, {})
    pid = int(d.get("pid") or 0)
    if not pid:
        return 0
    alive = _load(ALIVE, {})
    try:
        ts = datetime.strptime(str(alive.get("ts") or ""), "%Y-%m-%dT%H:%M:%S")
        if (datetime.now() - ts).total_seconds() < 3 * TICK_S + 10:
            return pid
    except ValueError:
        pass
    return 0


# ---------------------------------------------------------------- doctor
def _which(name: str) -> str:
    return shutil.which(name) or (shutil.which(name + ".cmd") if IS_WIN else "") or ""


def _ver(cmd: list, timeout: int = 20) -> str:
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, encoding="utf-8", errors="replace")
        return ((r.stdout or "") + (r.stderr or "")).strip().splitlines()[0][:80] if (r.stdout or r.stderr) else ""
    except (OSError, subprocess.TimeoutExpired):
        return ""


def _user_env(name: str) -> str:
    """A User-scope env var on Windows even when this process did not inherit it (a fresh terminal would)."""
    if os.environ.get(name):
        return os.environ[name]
    if IS_WIN:
        try:
            r = subprocess.run(["reg", "query", r"HKCU\Environment", "/v", name], capture_output=True, text=True, timeout=10)
            if r.returncode == 0:
                return r.stdout.strip().splitlines()[-1].split(None, 2)[-1]
        except (OSError, subprocess.TimeoutExpired, IndexError):
            pass
    return ""


def _playwright_browsers() -> Path:
    if os.environ.get("PLAYWRIGHT_BROWSERS_PATH"):
        return Path(os.environ["PLAYWRIGHT_BROWSERS_PATH"])
    if IS_WIN:
        return Path(os.environ.get("LOCALAPPDATA", str(Path.home()))) / "ms-playwright"
    if platform.system() == "Darwin":
        return Path.home() / "Library" / "Caches" / "ms-playwright"
    return Path.home() / ".cache" / "ms-playwright"


def doctor(probe: bool = True, fix: bool = False) -> list:
    rows = []

    def add(name, status, detail, fixit=""):
        rows.append({"check": name, "status": status, "detail": detail, "fix": fixit})

    v = sys.version_info
    add("python", "ok" if v >= (3, 10) else "fail", f"{v.major}.{v.minor}.{v.micro} at {sys.executable}", "install Python 3.11+ and run sage with it")
    org = orgcfg.load()
    add("org pack", "ok" if org.get("name") else "fail", f"assistant/org.json: {org.get('name') or 'missing'}",
        'python assistant/new_org.py "<Org>" --owner "Name:email" --out <folder>')
    man = _load(ROOT / "engine_manifest.json", {})
    add("engine manifest", "ok" if man.get("files") else "warn", f"engine {((man.get('engine') or {}).get('version') or 'none')} ({len(man.get('files') or {})} files)",
        "python assistant/sage.py update --engine <sage-core folder>")
    # python packages, in the interpreter the jobs use (org.json -> python, else this one)
    py = jobs_python()
    r = subprocess.run([py, "-c", "import keyring, playwright; print('ok')"], capture_output=True, text=True, timeout=60) if Path(py).exists() else None
    pk_ok = bool(r and r.returncode == 0)
    add("python packages", "ok" if pk_ok else "fail", f"keyring + playwright in {py}" if pk_ok else f"missing in {py}: {(r.stderr if r else 'interpreter not found').strip()[-120:]}",
        f'"{py}" -m pip install -r assistant/requirements.txt')
    # semantic search (scripts/semantic.py) needs fastembed + numpy in org.json -> semantic_python (else the jobs' interpreter);
    # without them the wiki falls back to keyword search, so this row is info, never a failure (2026-10-29)
    sp = os.path.expanduser(str(org.get("semantic_python") or "")) or py
    r = subprocess.run([sp, "-c", "import fastembed, numpy; print('ok')"], capture_output=True, text=True, timeout=120) if Path(sp).exists() else None
    sem_ok = bool(r and r.returncode == 0)
    add("semantic search", "ok" if sem_ok else "info", f"fastembed + numpy in {sp}" if sem_ok else f"not installed in {sp}: wiki search falls back to keywords (scripts/wiki_tool.py search)",
        f'"{sp}" -m pip install -r assistant/requirements-semantic.txt   (then org.json -> semantic_python = that interpreter)')
    pb = _playwright_browsers()
    has_browsers = pb.exists() and any(pb.iterdir())
    add("playwright browsers", "ok" if has_browsers else "warn", str(pb) + ("" if has_browsers else " (none installed)"), f'"{py}" -m playwright install chromium')
    c = claude_cli()
    # git and Node are conveniences, not needs (2026-10-28): the installer ships the engine as a release and the Claude CLI
    # as its native build; only a git-clone install updates by git, only the npm build of the CLI runs on Node
    p = _which("git")
    add("git", "ok" if p else "warn", (_ver([p, "--version"]) or p) if p else "not installed (only a git-clone install updates by git; the installer and `sage update` do not need it)",
        "install Git (git-scm.com) if you work from a clone")
    p = _which("node")
    add("node", "ok" if p else ("info" if c else "warn"), (_ver([p, "--version"]) or p) if p else ("not needed: the Claude CLI here is the native build" if c else "not installed (only the npm build of the Claude CLI needs it)"),
        "install Node LTS (nodejs.org) only for `npm install -g @anthropic-ai/claude-code`")
    g = _which("gcloud")
    add("gcloud", "ok" if g else "info", (_ver([g, "--version"]) or g) if g else "not installed (optional: the Google steps work from the console links in agent/GOOGLE_SETUP.md)",
        "install the Google Cloud CLI (cloud.google.com/sdk) and run `gcloud auth login` as the owner, or follow the console links")
    add("claude cli", "ok" if c else "fail", (_ver([c, "--version"]) or c) if c else "not found (the agent's model turns run through it)",
        "install the Claude Code CLI: Windows `irm https://claude.ai/install.ps1 | iex`, macOS/Linux `curl -fsSL https://claude.ai/install.sh | bash` (or npm install -g @anthropic-ai/claude-code)")
    try:
        import updater
        rel = updater.release_url()
    except Exception:  # noqa: BLE001
        rel = os.environ.get("SAGE_RELEASE_URL", "") or str(org.get("engine_release_url") or "")
    add("engine updates", "ok" if rel else "info", f"nightly from {rel}" if rel else "no release feed (org.json engine_release_url, or a GitHub clone as the engine): updates by hand, `sage update --engine <folder>`",
        "set org.json -> engine_release_url to the release feed (release.json), then `sage update --check`")
    tok = _user_env("CLAUDE_CODE_OAUTH_TOKEN")
    billing = str(org.get("claude_billing") or "subscription")
    stray = [k for k in ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_BASE_URL") if _user_env(k)]
    if billing == "api":
        add("claude auth (api key)", "ok" if _user_env("ANTHROPIC_API_KEY") else "fail", "ANTHROPIC_API_KEY " + ("set" if _user_env("ANTHROPIC_API_KEY") else "missing"),
            "set the User env var ANTHROPIC_API_KEY to this organization's own key (with a monthly spend cap)")
    else:
        add("claude token", "ok" if tok else "fail", "CLAUDE_CODE_OAUTH_TOKEN " + ("set (user scope)" if tok else "missing: headless runs cannot sign in"),
            "the OWNER runs `claude setup-token` in a terminal on this machine and stores the result as the User env var CLAUDE_CODE_OAUTH_TOKEN")
        add("no stray api key", "ok" if not stray else "warn", "none" if not stray else ", ".join(stray) + " set: would silently switch billing to metered",
            "remove the variable unless this organization chose API billing (org.json claude_billing = api)")
    if probe and c:
        if os.environ.get("CLAUDECODE") or os.environ.get("CLAUDE_CODE_ENTRYPOINT"):
            add("headless probe", "warn", "skipped: run from a fresh terminal (inside a Claude session the token is scrubbed and the probe would say 'Not logged in', a false negative)",
                "python assistant/sage.py doctor   (in a new terminal window)")
        else:
            out = _ver([c, "-p", "Reply with exactly: OK", "--max-turns", "1"], timeout=120)
            add("headless probe", "ok" if "OK" in out else "fail", out[:120] or "no output", "fix the token / key above, then run the probe again")
    add("mission control", "ok" if _http_ok(MC_URL + "/api/health") else "warn", MC_URL + (" up" if _http_ok(MC_URL + "/api/health") else " not running"),
        "python assistant/sage.py serve  (its keepalive starts it)")
    pid = serve_pid()
    add("serve", "ok" if pid else "warn", f"running (pid {pid})" if pid else "not running", "python assistant/sage.py serve, then install-startup")
    sd = Path(os.path.expanduser(str(org.get("secrets_dir") or "")))
    if str(org.get("secrets_dir") or ""):
        if fix and not sd.exists():
            sd.mkdir(parents=True, exist_ok=True)
        add("secrets folder", "ok" if sd.exists() else "warn", str(sd) + ("" if sd.exists() else " (missing)"), "python assistant/sage.py doctor --fix")
    gh = Path(os.path.expanduser(str(org.get("google_home") or "")))
    acc = (_load(HERE / "agent" / "google.json", {}) or {}).get("accounts") or {}
    add("google (mail + calendar)", "ok" if acc else "info", f"{len(acc)} account(s) in agent/google.json" if acc else "not connected yet", "Mission Control /setup, or assistant/agent/GOOGLE_SETUP.md Part 1")
    chat = (HERE / "agent" / "gchat.local.json").exists()
    add("google chat", "ok" if chat else "info", "chat app configured" if chat else "not connected yet (needs Google Workspace)", "assistant/agent/GOOGLE_SETUP.md Parts 2-3")
    ts = _which("tailscale")
    push = ((_load(HERE / "config.json", {}) or {}).get("push") or {})
    gl = _load(GCHAT_LOCAL, {}) or {}
    relay_mode = "pull" if gl.get("relay_webapp_url") else ("push" if gl.get("endpoint_url") else "")
    if relay_mode == "pull":
        sys.path.insert(0, str(HERE / "scripts"))
        try:
            import relay_poll
            h = relay_poll.health()
        except Exception as e:  # noqa: BLE001
            h = {"ok": False, "error": str(e)[:120]}
        add("chat relay", "ok" if h.get("ok") else "fail", f"pull mode via the Apps Script web app: {'reachable, ' + str(h.get('pending', 0)) + ' queued' if h.get('ok') else h.get('error', 'unreachable')}",
            "redeploy the relay web app (agent/gchat_relay/README.md) and `sage relay url <web app url>`; the secret must match the script property RELAY_SECRET")
    elif relay_mode == "push":
        add("chat relay", "ok" if ts else "warn", "push mode (Mission Control reached through Tailscale Funnel)" + ("" if ts else "; tailscale not found"),
            "install Tailscale, or switch to pull mode: `sage relay secret`, deploy the web app, `sage relay url <url>`")
    else:
        add("chat relay", "info", "none yet: card taps cannot reach this PC", "`sage relay secret`, deploy agent/gchat_relay as Add-on + Web app, `sage relay url <web app url>`, `sage relay check`")
    add("card buttons", "ok" if push.get("buttons", True) else "info",
        "buttons on" if push.get("buttons", True) else "answered by words in the thread until the relay is checked",
        "`sage relay check` turns buttons on once taps can reach Mission Control")
    try:
        free = shutil.disk_usage(str(ROOT)).free / 1e9
        add("disk", "ok" if free > 5 else "fail", f"{free:.1f} GB free on {ROOT.anchor or ROOT}", "free space; the deadman alarms under 5 GB")
    except OSError:
        pass
    if gh and str(org.get("google_home") or "") and fix and not gh.exists():
        gh.mkdir(parents=True, exist_ok=True)
    return rows


def print_rows(rows: list) -> None:
    icon = {"ok": "ok  ", "warn": "WARN", "fail": "FAIL", "info": "info"}
    for r in rows:
        print(f"{icon.get(r['status'], r['status']):4} {r['check']:26} {r['detail']}")
        if r["status"] in ("warn", "fail") and r.get("fix"):
            print(f"{'':4} {'':26} -> {r['fix']}")


# ---------------------------------------------------------------- health
def health() -> dict:
    now = time.time()
    day = now - 24 * 3600

    def _rows(path):
        try:
            return [json.loads(ln) for ln in path.read_text(encoding="utf-8-sig").splitlines() if ln.strip()]
        except (OSError, ValueError):
            return []

    def _t(s):
        try:
            return datetime.strptime(str(s)[:19], "%Y-%m-%dT%H:%M:%S").timestamp()
        except ValueError:
            return 0.0

    outcomes = {}
    for r in _rows(DATA / "outcomes.jsonl"):
        if r.get("id"):
            outcomes.setdefault(r["id"], {}).update(r)
    recent = [o for o in outcomes.values() if _t(o.get("updated") or o.get("ts")) >= day]
    verified = [o for o in recent if o.get("state") == "verified"]
    failed = [o for o in recent if o.get("state") in ("failed", "unverified", "abandoned")]
    tickets = {}
    for r in _rows(DATA / "exec_tickets.jsonl"):
        if r.get("id"):
            tickets.setdefault(r["id"], {}).update(r)
    done = [t for t in tickets.values() if t.get("status") == "done" and _t(t.get("ts_done") or t.get("ts")) >= day]
    cards = {}
    for r in _rows(DATA / "cards.jsonl"):
        if r.get("id"):
            cards.setdefault(r["id"], {}).update(r)
    open_cards = [c for c in cards.values() if str(c.get("status") or c.get("state") or "open") in ("open", "held")]
    conn = _load(DATA / "connections_status.json", {}) or {}
    down = [k for k, v in (conn.get("connections") or conn).items() if isinstance(v, dict) and str(v.get("status") or v.get("state") or "").lower() in ("down", "fail", "failed", "not_configured", "expired")]
    dead = _load(DATA / "deadman_state.json", {}) or {}
    stale = [k for k, v in (dead.get("latched") or dead).items() if isinstance(v, dict) and v.get("stale")]
    pid = serve_pid()
    st = _load(STATE, {})
    failed_jobs = [k for k, v in st.items() if v.get("ok") is False]
    did = f"{len(verified)} verified outcome(s), {len(done)} approved action(s) done in the last 24 h"
    bad = ", ".join(x for x in [f"{len(failed)} outcome(s) failed or unverified" if failed else "", f"jobs failing: {', '.join(failed_jobs[:5])}" if failed_jobs else "",
                                f"deadman stale: {', '.join(stale)}" if stale else "", "" if pid else "serve is not running"] if x) or "nothing"
    need = ", ".join(x for x in [f"{len(open_cards)} card(s) waiting for a tap" if open_cards else "", f"connections down: {', '.join(down[:6])}" if down else ""] if x) or "nothing"
    return {"did": did, "failed": bad, "need": need, "serve_pid": pid, "open_cards": len(open_cards), "verified_24h": len(verified), "failed_24h": len(failed)}


# ---------------------------------------------------------------- startup registration
def _task_name() -> str:
    return f"{orgcfg.get('name', 'Sage')} Sage serve"


def install_startup() -> int:
    exe = Path(sys.executable)
    if IS_WIN:
        pyw = exe.with_name("pythonw.exe")
        xml = f"""<?xml version="1.0" encoding="UTF-16"?>
<Task version="1.2" xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">
  <RegistrationInfo><Description>Sage supervisor: the spine, the probe, the operator, the deadman and Mission Control in one process.</Description></RegistrationInfo>
  <Triggers><LogonTrigger><Enabled>true</Enabled></LogonTrigger></Triggers>
  <Principals><Principal id="Author"><LogonType>InteractiveToken</LogonType><RunLevel>LeastPrivilege</RunLevel></Principal></Principals>
  <Settings><MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy><DisallowStartIfOnBatteries>false</DisallowStartIfOnBatteries>
    <StopIfGoingOnBatteries>false</StopIfGoingOnBatteries><StartWhenAvailable>true</StartWhenAvailable><ExecutionTimeLimit>PT0S</ExecutionTimeLimit>
    <RestartOnFailure><Interval>PT1M</Interval><Count>99</Count></RestartOnFailure><Enabled>true</Enabled></Settings>
  <Actions Context="Author"><Exec><Command>{pyw if pyw.exists() else exe}</Command><Arguments>"{HERE / 'sage.py'}" serve</Arguments><WorkingDirectory>{ROOT}</WorkingDirectory></Exec></Actions>
</Task>
"""
        p = HERE / "tasks" / "SageServe.xml"
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(xml, encoding="utf-16")
        r = subprocess.run(["schtasks", "/create", "/tn", _task_name(), "/xml", str(p), "/f"], capture_output=True, text=True)
        print((r.stdout or r.stderr).strip())
        if r.returncode == 0:
            subprocess.run(["schtasks", "/run", "/tn", _task_name()], capture_output=True, text=True)
        return r.returncode
    if platform.system() == "Darwin":
        label = f"com.sage.{_slug()}"
        plist = Path.home() / "Library" / "LaunchAgents" / f"{label}.plist"
        plist.parent.mkdir(parents=True, exist_ok=True)
        plist.write_text(f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>Label</key><string>{label}</string>
  <key>ProgramArguments</key><array><string>{exe}</string><string>{HERE / 'sage.py'}</string><string>serve</string></array>
  <key>WorkingDirectory</key><string>{ROOT}</string>
  <key>RunAtLoad</key><true/><key>KeepAlive</key><true/>
  <key>StandardOutPath</key><string>{Path.home() / '.claude' / 'sage-serve.out.log'}</string>
  <key>StandardErrorPath</key><string>{Path.home() / '.claude' / 'sage-serve.err.log'}</string>
</dict></plist>
""", encoding="utf-8")
        r = subprocess.run(["launchctl", "load", "-w", str(plist)], capture_output=True, text=True)
        print(f"wrote {plist}; launchctl: {(r.stdout or r.stderr).strip() or 'loaded'}")
        return r.returncode
    unit = Path.home() / ".config" / "systemd" / "user" / f"sage-{_slug()}.service"
    unit.parent.mkdir(parents=True, exist_ok=True)
    unit.write_text(f"[Unit]\nDescription=Sage supervisor ({orgcfg.get('name', 'Sage')})\nAfter=network-online.target\n\n[Service]\n"
                    f"ExecStart={exe} {HERE / 'sage.py'} serve\nWorkingDirectory={ROOT}\nRestart=always\nRestartSec=30\n\n[Install]\nWantedBy=default.target\n", encoding="utf-8")
    r = subprocess.run(["systemctl", "--user", "enable", "--now", unit.name], capture_output=True, text=True)
    print(f"wrote {unit}; systemctl: {(r.stdout or r.stderr).strip() or 'enabled'}")
    return r.returncode


def uninstall_startup() -> int:
    if IS_WIN:
        r = subprocess.run(["schtasks", "/delete", "/tn", _task_name(), "/f"], capture_output=True, text=True)
        print((r.stdout or r.stderr).strip())
        return 0
    if platform.system() == "Darwin":
        plist = Path.home() / "Library" / "LaunchAgents" / f"com.sage.{_slug()}.plist"
        subprocess.run(["launchctl", "unload", "-w", str(plist)], capture_output=True, text=True)
        if plist.exists():
            plist.unlink()
        print(f"removed {plist}")
        return 0
    unit = f"sage-{_slug()}.service"
    subprocess.run(["systemctl", "--user", "disable", "--now", unit], capture_output=True, text=True)
    p = Path.home() / ".config" / "systemd" / "user" / unit
    if p.exists():
        p.unlink()
    print(f"removed {p}")
    return 0


# ---------------------------------------------------------------- the chat relay (card taps without Tailscale)
def relay_cmd(op: str, value: str = "") -> int:
    """secret: mint the shared secret into gchat.local.json (printed ONCE, for the script property RELAY_SECRET);
    url <web app url>: record the relay's web app URL (pull mode); check: health, and buttons on when it answers;
    poll: one poll now. The relay itself is assistant/agent/gchat_relay (README there)."""
    import secrets as _secrets
    gl = _load(GCHAT_LOCAL, {}) or {}
    if op == "secret":
        if gl.get("relay_secret") and value != "new":
            print("relay_secret is already set in agent/gchat.local.json (never printed again); `sage relay secret new` mints a fresh one")
            return 0
        s = _secrets.token_urlsafe(32)
        gl["relay_secret"] = s
        _save(GCHAT_LOCAL, gl)
        print(f"relay secret minted into {GCHAT_LOCAL.name}. Paste it ONCE into the Apps Script project's Script property RELAY_SECRET, then close this window:\n  {s}")
        return 0
    if op == "url":
        if not value.startswith("https://script.google.com/"):
            print("the value must be the Apps Script WEB APP URL (Deploy -> Manage deployments -> Web app -> URL, https://script.google.com/macros/s/.../exec)")
            return 1
        gl["relay_webapp_url"] = value.strip()
        gl["relay_mode"] = "pull"
        _save(GCHAT_LOCAL, gl)
        print("relay web app URL recorded (pull mode). Now: sage relay check")
        return 0
    sys.path.insert(0, str(HERE / "scripts"))
    import relay_poll
    if op == "check":
        h = relay_poll.health()
        if not h.get("ok"):
            print(f"relay NOT reachable: {h.get('error', h)}")
            return 1
        cfg_path = HERE / "config.json"
        cfg = _load(cfg_path, {}) or {}
        cfg.setdefault("push", {})["buttons"] = True
        _save(cfg_path, cfg)
        print(f"relay reachable ({h.get('mode')} mode, {h.get('pending', 0)} queued). Card buttons are ON (config.json push.buttons = true); serve's relay-poll job carries the taps.")
        return 0
    if op == "poll":
        s = relay_poll.run(0, 1, once=True)
        print(f"RELAY_POLL polls={s['polls']} events={s['events']} replied={s['replied']} late={s['late']} errors={s['errors']}")
        return 0
    return 2


# ---------------------------------------------------------------- modules, skill, uninstall
def module_switch(name: str, on: bool) -> int:
    import engine
    if name not in engine.MODULES:
        print(f"no module {name}; known: {', '.join(engine.MODULES)}")
        return 1
    sj_path = HERE / "spine_jobs.json"
    sj = _load(sj_path, {}) or {}
    n = 0
    for j in sj.get("jobs", []):
        if j.get("name") in engine.MODULES[name]["jobs"]:
            j["enabled"], j["module"] = on, name
            n += 1
    _save(sj_path, sj)
    org_path = HERE / "org.json"
    org = _load(org_path, {}) or {}
    mods = [m for m in (org.get("modules") or []) if m != name] + ([name] if on else [])
    org["modules"] = mods
    _save(org_path, org)
    print(f"module {name} {'on' if on else 'off'}: {n} job(s) {'enabled' if on else 'disabled'} ({engine.MODULES[name]['what']})")
    return 0


def install_skill() -> int:
    src = HERE / "setup_skill" / "sage-setup"
    dst = Path.home() / ".claude" / "skills" / "sage-setup"
    if not src.exists():
        print(f"no skill at {src}")
        return 1
    if dst.exists():
        shutil.rmtree(dst)
    shutil.copytree(src, dst)
    print(f"installed the sage-setup skill at {dst} (user scope: every Claude Code session on this machine sees /sage-setup)")
    return 0


def uninstall() -> int:
    STOP.touch()
    uninstall_startup()
    org = orgcfg.load()
    print(f"""serve told to stop; startup entry removed. Nothing of the organization's folder was deleted.
To finish by hand (each is the owner's own account):
  - Claude: remove the User env var CLAUDE_CODE_OAUTH_TOKEN (or the API key); the token itself expires on its own.
  - Google: console.cloud.google.com -> the agent's project(s): delete the OAuth client(s), the service account key, the Chat app; revoke at myaccount.google.com/permissions.
  - Mail app passwords: myaccount.google.com/apppasswords.
  - The OS credential store: entries under service "{org.get('keyring_service') or 'the agent'}".
  - Secrets folder: {org.get('secrets_dir') or '(none set)'}.
Then delete the folder {ROOT} when you are sure.""")
    return 0


# ---------------------------------------------------------------- the owner's token (typed in a console, never through a page)
def store_user_env(name: str, value: str) -> str:
    """A User-scope environment variable: the registry on Windows (and a broadcast so new processes see it), the shell
    profile elsewhere. Returns where it went. The value is never printed or logged."""
    os.environ[name] = value
    if IS_WIN:
        import winreg
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, "Environment", 0, winreg.KEY_SET_VALUE) as k:
            winreg.SetValueEx(k, name, 0, winreg.REG_SZ, value)
        try:
            import ctypes
            ctypes.windll.user32.SendMessageTimeoutW(0xFFFF, 0x001A, 0, "Environment", 2, 5000, None)
        except Exception:  # noqa: BLE001
            pass
        return "the User environment (HKCU\\Environment)"
    prof = Path.home() / (".zprofile" if platform.system() == "Darwin" else ".profile")
    lines = [ln for ln in (prof.read_text(encoding="utf-8").splitlines() if prof.exists() else []) if not ln.startswith(f"export {name}=")]
    lines.append(f'export {name}="{value}"')
    prof.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return str(prof)


def token_cmd(api_key: bool = False) -> int:
    """Runs in a console the owner sees (the wizard's button opens it). Subscription: `claude setup-token` signs in and
    prints a long-lived token; the owner copies it from the window and pastes it hidden here; it is stored as the User
    env var CLAUDE_CODE_OAUTH_TOKEN. --api-key: ANTHROPIC_API_KEY instead and org.json claude_billing = api."""
    import getpass
    name = "ANTHROPIC_API_KEY" if api_key else "CLAUDE_CODE_OAUTH_TOKEN"
    print(f"\n== Sage: your Claude {'API key' if api_key else 'token'} ==\nThe agent's model turns run on YOUR account; nothing here is sent anywhere, it is stored in your own user environment.\n")
    if not api_key:
        c = claude_cli()
        if not c:
            print("The Claude Code CLI is not installed yet. Install it first (sage doctor has the line), then run this again.")
            return 1
        print("1. A browser opens: sign in with the Claude account whose subscription the agent should use, then come back here.")
        print("2. The CLI prints your token below. Select it with the mouse, press Enter to copy it, then paste it where this window asks (nothing shows while you paste).\n")
        rc = subprocess.call([c, "setup-token"])
        if rc != 0:
            print(f"\n`claude setup-token` ended with code {rc}. If it printed a token anyway, paste it below; otherwise close this window and try again.")
    v = getpass.getpass(f"\nPaste the {name} value here (hidden): ").strip().strip('"')
    if not v:
        print("nothing entered; nothing stored")
        return 1
    where = store_user_env(name, v)
    if api_key:
        p = HERE / "org.json"
        o = _load(p, {}) or {}
        o["claude_billing"] = "api"
        _save(p, o)
    print(f"\nStored {name} ({len(v)} characters) in {where}. Close this window and tap Check on the page; the agent's runs use it from now on.")
    return 0


def probe_cmd() -> int:
    """One headless turn through the CLI with the stored token / key (read from the User environment, so a token stored a
    minute ago counts). Prints PROBE ok | PROBE failed: <why>."""
    c = claude_cli()
    if not c:
        print("PROBE failed: the Claude Code CLI is not installed")
        return 1
    org = orgcfg.load()
    env = {k: v for k, v in os.environ.items() if k not in ("CLAUDECODE", "CLAUDE_CODE_ENTRYPOINT")}
    tok, key = _user_env("CLAUDE_CODE_OAUTH_TOKEN"), _user_env("ANTHROPIC_API_KEY")
    if str(org.get("claude_billing") or "subscription") == "api":
        if not key:
            print("PROBE failed: no ANTHROPIC_API_KEY stored (sage token --api-key)")
            return 1
        env["ANTHROPIC_API_KEY"] = key
        env.pop("CLAUDE_CODE_OAUTH_TOKEN", None)
    else:
        if not tok:
            print("PROBE failed: no CLAUDE_CODE_OAUTH_TOKEN stored yet (sage token)")
            return 1
        env["CLAUDE_CODE_OAUTH_TOKEN"] = tok
        env.pop("ANTHROPIC_API_KEY", None)
    try:
        r = subprocess.run([c, "-p", "Reply with exactly: OK", "--max-turns", "1"], capture_output=True, text=True, encoding="utf-8", errors="replace",
                           timeout=120, env=env, creationflags=NO_WINDOW)
        out = ((r.stdout or "") + (r.stderr or "")).strip()
    except subprocess.TimeoutExpired:
        print("PROBE failed: no answer within two minutes")
        return 1
    if "OK" in out:
        print("PROBE ok: the agent can think on this account")
        return 0
    print("PROBE failed: " + (out[-200:].replace("\n", " ") or f"exit {r.returncode}, no output"))
    return 1


# ---------------------------------------------------------------- cli
def main() -> int:
    sys.stdout.reconfigure(encoding="utf-8")
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("doctor"); p.add_argument("--json", action="store_true"); p.add_argument("--no-probe", action="store_true"); p.add_argument("--fix", action="store_true")
    p = sub.add_parser("serve"); p.add_argument("--once", action="store_true"); p.add_argument("--dry-run", action="store_true")
    sub.add_parser("status")
    p = sub.add_parser("health"); p.add_argument("--json", action="store_true"); p.add_argument("--post", action="store_true")
    sub.add_parser("pause"); sub.add_parser("resume"); sub.add_parser("stop")
    sub.add_parser("install-startup"); sub.add_parser("uninstall-startup")
    p = sub.add_parser("update"); p.add_argument("--engine", default=""); p.add_argument("--force", action="store_true"); p.add_argument("--dry-run", action="store_true")
    p.add_argument("--check", action="store_true", help="what the release feed offers, without applying"); p.add_argument("--no-gate", action="store_true", help="compile check only (the feed path)")
    sub.add_parser("rollback", help="the previous engine back in one step (assistant/updater.py)")
    p = sub.add_parser("token", help="store the owner's Claude token (or --api-key) from a console; never printed"); p.add_argument("--api-key", action="store_true")
    sub.add_parser("probe", help="one headless turn with the stored token: PROBE ok | failed")
    p = sub.add_parser("welcome", help="the first-run wizard (assistant/welcome.py); --open = the launcher"); p.add_argument("rest", nargs=argparse.REMAINDER)
    p = sub.add_parser("release", help="build / verify a release of the engine (assistant/release.py)"); p.add_argument("rest", nargs=argparse.REMAINDER)
    p = sub.add_parser("module"); p.add_argument("state", choices=["on", "off"]); p.add_argument("name")
    p = sub.add_parser("relay", help="card taps without Tailscale: secret | url <web app url> | check | poll"); p.add_argument("op", choices=["secret", "url", "check", "poll"]); p.add_argument("value", nargs="?", default="")
    p = sub.add_parser("new-org"); p.add_argument("rest", nargs=argparse.REMAINDER)
    p = sub.add_parser("export-engine"); p.add_argument("dst"); p.add_argument("--git", action="store_true")
    sub.add_parser("install-skill"); sub.add_parser("uninstall")
    sub.add_parser("claude-cli", help="print the Claude Code CLI this machine runs (ASSISTANT_CLAUDE, PATH, the native or npm install); the wrappers ask here")
    a = ap.parse_args()
    if a.cmd == "doctor":
        rows = doctor(probe=not a.no_probe, fix=a.fix)
        if a.json:
            print(json.dumps(rows, indent=1))
        else:
            print_rows(rows)
        return 1 if any(r["status"] == "fail" for r in rows) else 0
    if a.cmd == "serve":
        s = Serve(dry=a.dry_run)
        if a.once:
            ran = s.pass_once(block=True)
            print(f"SERVE once: ran {len(ran)} of {len(s.jobs)} jobs: {', '.join(ran) or 'none due'}")
            return 0
        return s.loop()
    if a.cmd == "status":
        pid = serve_pid()
        print(f"serve: {'running pid ' + str(pid) if pid else 'not running'}{' (PAUSED)' if PAUSED.exists() else ''}; mission control {'up' if _http_ok(MC_URL + '/api/health') else 'down'}")
        for name, st in sorted((_load(STATE, {}) or {}).items()):
            print(f"  {name:18} last {str(st.get('last', '-'))[:16]}  {'ok ' if st.get('ok') else 'BAD'}  {str(st.get('result', ''))[:80]}")
        return 0
    if a.cmd == "health":
        h = health()
        if a.json:
            print(json.dumps(h, indent=1))
        else:
            text = f"Did: {h['did']}.\nFailed: {h['failed']}.\nNeeds you: {h['need']}."
            print(text)
            if a.post:
                subprocess.run([sys.executable, str(HERE / "scripts" / "notify.py")], input="HEALTH: " + text, text=True, encoding="utf-8", cwd=str(ROOT))
        return 0
    if a.cmd == "pause":
        PAUSED.parent.mkdir(parents=True, exist_ok=True)
        PAUSED.write_text(time.strftime("%Y-%m-%dT%H:%M:%S"), encoding="utf-8")
        print("paused: every job holds except the deadman and Mission Control; `resume` lets go")
        return 0
    if a.cmd == "resume":
        if PAUSED.exists():
            PAUSED.unlink()
        print("resumed")
        return 0
    if a.cmd == "stop":
        STOP.parent.mkdir(parents=True, exist_ok=True)
        STOP.touch()
        pid = serve_pid()
        print(f"stop requested{' (pid ' + str(pid) + ' exits within ' + str(TICK_S) + ' s)' if pid else ' (serve was not running)'}")
        return 0
    if a.cmd == "install-startup":
        return install_startup()
    if a.cmd == "uninstall-startup":
        return uninstall_startup()
    if a.cmd == "update":
        import engine
        if not a.engine:
            # no folder named: the release feed (org.json engine_release_url), the same path the nightly job and the
            # installer-made organizations use, so every kind of install updates identically
            import updater
            if a.check:
                c = updater.check()
                print(f"installed {c['installed'] or 'unknown'}; available {c.get('available') or '-'} ({c.get('relation')})" + (f": {c['detail']}" if c.get("detail") else ""))
                return 0
            r = updater.run(force=a.force, gates=("compile",) if a.no_gate else updater.GATES, dry=a.dry_run)
            print(f"{r['result']}: {r['detail']}")
            if r["result"] == "skipped" and "no release URL" in r["detail"]:
                print("or name a folder: python assistant/sage.py update --engine <sage-core folder>")
            return 0 if r["result"] in ("updated", "up to date", "skipped", "would update") or r["result"].startswith("not applied") else 1
        eng = a.engine
        if not (Path(eng) / engine.MANIFEST_NAME).exists():
            print(f"no engine at {eng} (engine_manifest.json missing)")
            return 1
        print(engine.update_summary(engine.update(ROOT, Path(eng), force=a.force, dry=a.dry_run)))
        if not a.dry_run:
            RESTART.parent.mkdir(parents=True, exist_ok=True)
            RESTART.write_text(f"update --engine {eng}", encoding="utf-8")
            print("now: python assistant/sage.py doctor --no-probe (serve restarts itself on the new code within a minute)")
        return 0
    if a.cmd == "rollback":
        import updater
        r = updater.rollback()
        print(("rolled back: " + r["summary"]) if r.get("ok") else ("nothing to roll back: " + r.get("detail", "")))
        return 0 if r.get("ok") else 1
    if a.cmd == "token":
        return token_cmd(api_key=a.api_key)
    if a.cmd == "probe":
        return probe_cmd()
    if a.cmd == "welcome":
        return subprocess.call([sys.executable, str(HERE / "welcome.py")] + list(a.rest))
    if a.cmd == "release":
        return subprocess.call([sys.executable, str(HERE / "release.py")] + list(a.rest))
    if a.cmd == "module":
        return module_switch(a.name, a.state == "on")
    if a.cmd == "relay":
        return relay_cmd(a.op, a.value)
    if a.cmd == "new-org":
        return subprocess.call([sys.executable, str(HERE / "new_org.py")] + list(a.rest))
    if a.cmd == "export-engine":
        import engine
        man = engine.export(Path(a.dst), src_root=ROOT, git=a.git)
        print(f"exported {len(man['files'])} files to {a.dst} (engine {man['engine']['version']})")
        return 0
    if a.cmd == "install-skill":
        return install_skill()
    if a.cmd == "claude-cli":
        c = claude_cli()
        print(c)
        return 0 if c else 1
    if a.cmd == "uninstall":
        return uninstall()
    return 0


if __name__ == "__main__":
    sys.exit(main())

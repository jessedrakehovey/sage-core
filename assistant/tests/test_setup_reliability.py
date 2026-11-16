"""What a NEW organization's computer needs to come up by itself (sage-core, 2026-11-15).

Every check here is a fault found by installing sage-core from its release onto a clean folder and walking the owner's
steps (create, the computer checks, Claude, start, stop, restart at logon); each one stopped a new organization's agent
on its first day while the source organization, which never runs those paths, looked fine.

  1. the organization's files keep their line endings (a doubled CR broke every PowerShell line continuation: the
     operator died with "'-ClaudeArgs' is not recognized")
  2. its rules / memory / routines stores start in their readers' shapes (the chat bridge crashed every minute on "{}")
  3. its send-gate tripwire has a baseline (else "FAIL: no baseline" every night)
  4. its PowerShell wrappers run the organization's own Python (they named a venv only the source PC has)
  5. the startup task names this user (a logon trigger without one needs an administrator: "Access is denied")
  6. the supervisor starts without a console (pythonw under Task Scheduler: sys.stdout is None)
  7. a job's summary line behind a log timestamp is still its summary line (clean runs read as FAILED)
  8. stop ends this organization's Mission Control too, and never another's
  9. installing the browser never deletes the other browsers in the shared Playwright folder
 10. dreaming runs in a folder that is not a git repository
   python assistant/tests/test_setup_reliability.py
"""
import os as _os, tempfile as _tf
_os.environ.setdefault("WV_LOG_DIR", _tf.mkdtemp(prefix="wv_logs_"))
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")
HERE = Path(__file__).resolve().parent
ROOT = HERE.parent                      # assistant/
VAULT = ROOT.parent
TMP = Path(tempfile.mkdtemp(prefix="wv_setuprel_"))
PACK = TMP / "Pine Street OS"
IS_WIN = os.name == "nt"
FAILS = []


def check(name, cond, extra=""):
    print(("ok   " if cond else "FAIL ") + name + (f"  {extra}" if extra and not cond else ""))
    if not cond:
        FAILS.append(name)


def env():
    e = {k: v for k, v in os.environ.items() if not k.startswith(("ASSISTANT_", "AGENT_", "DOER_", "SAGE_"))}
    e.update(PYTHONIOENCODING="utf-8", SAGE_HOME=str(TMP / "home"), WV_LOG_DIR=str(TMP / "logs"), CHAT_PUSH="off")
    return e


def py(code, cwd=PACK, timeout=120):
    r = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, encoding="utf-8", errors="replace",
                       cwd=str(cwd), env=env(), timeout=timeout)
    return r.returncode, (r.stdout or "") + (r.stderr or "")


# ---------------------------------------------------------------- the organization, made the way the app makes one
r = subprocess.run([sys.executable, str(ROOT / "new_org.py"), "Pine Street Bakery", "--agent", "Juniper", "--owner", "Robin Vale:robin@pine.test",
                    "--out", str(PACK)], capture_output=True, text=True, encoding="utf-8", errors="replace", cwd=str(VAULT), env=env(), timeout=600)
check("new_org makes the organization", (PACK / "assistant" / "org.json").exists(), (r.stdout + r.stderr)[-400:])
org = json.loads((PACK / "assistant" / "org.json").read_text(encoding="utf-8-sig"))
org["python"] = sys.executable                     # what the app's create step writes
(PACK / "assistant" / "org.json").write_text(json.dumps(org, indent=1), encoding="utf-8")

# 1. line endings
doubled = [str(f.relative_to(PACK)) for f in PACK.rglob("*") if f.is_file() and f.suffix.lower() in (".ps1", ".xml", ".json", ".md", ".py", ".sh")
           and b"\r\r\n" in f.read_bytes()]
check("1. no file of the new organization has a doubled carriage return", not doubled, doubled[:5])
op = (PACK / "assistant" / "scripts" / "operator_cron.ps1").read_bytes()
src = (ROOT / "scripts" / "operator_cron.ps1").read_bytes()
check("1. the operator's wrapper keeps its source's line ending style", (b"\r\n" in op) == (b"\r\n" in src) and op.count(b"\n") == src.count(b"\n"))

# 2. the stores
st = PACK / "assistant" / "agent" / "state"
shapes = {f: json.loads((st / f).read_text(encoding="utf-8")) for f in ("rules.json", "memory.json", "routines.json")}
check("2. rules / memory / routines start in their readers' shapes", "rules" in shapes["rules.json"] and "facts" in shapes["memory.json"]
      and "routines" in shapes["routines.json"], shapes)
sys.path.insert(0, str(ROOT / "agent"))
import core  # noqa: E402
p = TMP / "empty.json"
p.write_text("{}", encoding="utf-8")
got = core.load(p, {"next_id": 1, "routines": []})
p.write_text("[1, 2]", encoding="utf-8")
check("2. a store already seeded '{}' reads as that store, empty (older organizations heal)", got == {"next_id": 1, "routines": []}
      and core.load(p, {"a": 1}) == [1, 2])
rc, out = py("import sys; sys.path.insert(0, 'assistant/agent'); import routines; print('DUE', routines.due() if hasattr(routines, 'due') else routines.active())")
check("2. the routines module runs on a new organization", rc == 0 and "DUE" in out, out[-300:])

# 3. the gate baseline
gates = PACK / "scripts" / "guardrails"
if (gates / "check_gates.py").exists():
    check("3. the send-gate tripwire's baseline is frozen at creation", (gates / "gate_baseline.json").exists())
    rc, out = py("import subprocess, sys; r = subprocess.run([sys.executable, 'scripts/guardrails/check_gates.py'], capture_output=True, text=True); print(r.returncode, r.stdout[-200:])")
    check("3. and the tripwire passes on the files as installed", out.strip().startswith("0"), out[-300:])
else:
    check("3. (no send-gate tripwire in this engine)", True)

# 4. the PowerShell wrappers' interpreter
if IS_WIN:
    for name in ("operator_cron.ps1", "spine.ps1", "spine_long.ps1", "deadman.ps1", "mc_server.ps1"):
        f = PACK / "assistant" / "scripts" / name
        lines = f.read_text(encoding="utf-8-sig").splitlines()
        end = max(i for i, ln in enumerate(lines[:60]) if "$orgPy" in ln or ln.startswith("$pyw") or "if ((Split-Path -Leaf $py)" in ln) + 1
        head = "\n".join(ln for ln in lines[:end] if not ln.lstrip().startswith(("param(", ". (", "Set-StrictMode")))
        ps = head + "\nWrite-Output \"PY=$py\""
        rr = subprocess.run(["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", ps], capture_output=True, text=True,
                            encoding="utf-8", errors="replace", cwd=str(PACK), env={k: v for k, v in env().items() if k != "ASSISTANT_PY"}, timeout=120)
        got = next((ln[3:].strip() for ln in (rr.stdout or "").splitlines() if ln.startswith("PY=")), "")
        want = {sys.executable.lower(), str(Path(sys.executable).with_name("pythonw.exe")).lower()}
        check(f"4. {name} runs the organization's own Python (org.json -> python)", got.lower() in want, f"got {got!r} {rr.stderr[-200:]}")
    t = (PACK / "assistant" / "scripts" / "operator_cron.ps1").read_text(encoding="utf-8-sig")
    check("4. the operator finds this computer's Claude CLI when the source's path is missing", "claude-cli" in t and "Test-Path $claude" in t)

# 5. the startup task names this user (schtasks itself stubbed: nothing is registered by the test)
rc, out = py("import sys, subprocess; sys.path.insert(0, 'assistant'); import sage\n"
             "class R: returncode = 0; stdout = 'SUCCESS'; stderr = ''\n"
             "sage.subprocess.run = lambda *a, **k: R()\n"
             "sage.install_startup()\n"
             "x = (sage.HERE / 'tasks' / 'SageServe.xml').read_text(encoding='utf-16')\n"
             "print('USERIDS', x.count('<UserId>'), 'LOGON', '<LogonTrigger><Enabled>true</Enabled><UserId>' in x)")
if IS_WIN:
    check("5. the startup task's trigger and principal name this user (no administrator needed)", "USERIDS 2 LOGON True" in out, out[-300:])

# 6. no console: pythonw's sys.stdout / sys.stderr are None
rc, out = py("import sys, io; sys.path.insert(0, 'assistant'); real = sys.__stdout__\n"
             "import sage\n"
             "sys.stdout = None; sys.stderr = None; sys.argv = ['sage.py', 'status']\n"
             "try:\n    code = sage.main()\nexcept AttributeError as e:\n    code = 'CRASH ' + str(e)\n"
             "real.write('MAIN ' + str(code) + '\\n')")
check("6. the supervisor's command line starts with no console (the startup task)", "MAIN " in out and "CRASH" not in out, out[-300:])

# 7. a summary line behind a log timestamp
rc, out = py("import sys; sys.path.insert(0, 'assistant'); import sage\n"
             "job = {'name': 't', 'cmd': ['python', '-c', \"print('2026-11-15 17:51:47  SPINE ran=0 ok=0 failed=0')\"], 'expect': 'SPINE', 'timeout_s': 60}\n"
             "bad = {'name': 'u', 'cmd': ['python', '-c', \"print('nothing to see')\"], 'expect': 'SPINE', 'timeout_s': 60}\n"
             "print('OK', sage.run_job(job, False)[0], 'SILENT', sage.run_job(bad, False)[0])")
check("7. a timestamped summary line counts; a run without one is still caught", "OK True SILENT False" in out, out[-300:])

# 8. stop ends this organization's Mission Control, never another's
sys.path.insert(0, str(ROOT))
os.chdir(PACK)
for m in [k for k in list(sys.modules) if k in ("sage", "ports", "orgcfg")]:
    del sys.modules[m]
sys.path.insert(0, str(PACK / "assistant"))
import sage as psage  # noqa: E402  (the new organization's own copy)
HEALTH = {}


class H(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def do_GET(self):
        b = json.dumps(HEALTH).encode()
        self.send_response(200); self.send_header("Content-Length", str(len(b))); self.end_headers(); self.wfile.write(b)


srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
threading.Thread(target=srv.serve_forever, daemon=True).start()
psage._mc_url = lambda: f"http://127.0.0.1:{srv.server_address[1]}"
sleeper = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(120)"])
HEALTH.update(ok=True, pack="another-agent", pid=sleeper.pid)
psage.stop_page()
time.sleep(1)
check("8. stop never touches another agent's Mission Control", sleeper.poll() is None)
HEALTH.update(pack=psage.ports.slug())
said = psage.stop_page()
time.sleep(2)
check("8. stop ends this organization's own Mission Control", sleeper.poll() is not None and "stopped" in said, said)
if sleeper.poll() is None:
    sleeper.kill()
srv.shutdown()
os.chdir(VAULT)

# 9. the browser install keeps the shared folder's other browsers
inst = (ROOT / "installer" / "windows" / "install.ps1").read_text(encoding="utf-8-sig")
app = (ROOT / "app.py").read_text(encoding="utf-8")
boot = (ROOT / "setup_skill" / "sage-setup" / "bootstrap.ps1").read_text(encoding="utf-8-sig") + (ROOT / "setup_skill" / "sage-setup" / "bootstrap.sh").read_text(encoding="utf-8")
check("9. every browser install skips Playwright's clean-up of other browsers (installer, app fix, setup skill)",
      "PLAYWRIGHT_SKIP_BROWSER_GC = '1'" in inst and inst.index("PLAYWRIGHT_SKIP_BROWSER_GC") < inst.index("playwright install")
      and 'PLAYWRIGHT_SKIP_BROWSER_GC"] = "1"' in app and boot.count("PLAYWRIGHT_SKIP_BROWSER_GC") == 2)

# 10. dreaming outside a git repository
dc = (PACK / "scripts" / "dream_cron.ps1").read_text(encoding="utf-8-sig") if (PACK / "scripts" / "dream_cron.ps1").exists() else ""
check("10. dreaming asks git only in a repository", "$isRepo" in dc and "if ($isRepo) { (& git -C $vault rev-parse HEAD" in dc)
if IS_WIN and dc:
    pr = subprocess.run(["powershell", "-NoProfile", "-Command", "$t=$null;$e=$null;[void][System.Management.Automation.Language.Parser]::ParseFile("
                         f"'{PACK / 'scripts' / 'dream_cron.ps1'}',[ref]$t,[ref]$e); $e.Count"], capture_output=True, text=True, timeout=60)
    check("10. and the new organization's copy parses", pr.stdout.strip() == "0", pr.stdout + pr.stderr)

# 11. an organization made from an INSTALLED engine (the app's path: %LOCALAPPDATA%\Sage\engine) keeps the word "engine"
sys.path.insert(0, str(ROOT))
import engine as _engine  # noqa: E402
ENG = TMP / "home" / "engine"
_engine.export(ENG, src_root=VAULT, with_tests=False)
PACK2 = TMP / "Juniper Bakery OS"
r = subprocess.run([sys.executable, str(ENG / "assistant" / "new_org.py"), "Juniper Bakery", "--agent", "Fern", "--owner", "Sam Lee:sam@juniper.test",
                    "--out", str(PACK2)], capture_output=True, text=True, encoding="utf-8", errors="replace", cwd=str(ENG), env=env(), timeout=600)
hit = [str(f.relative_to(PACK2)) for f in (PACK2 / "assistant").rglob("*") if f.is_file() and f.suffix in (".json", ".md", ".ps1")
       and re.search(r"(?<![\\/])Juniper Bakery OS(?![\\/])", f.read_text(encoding="utf-8", errors="replace"))
       and f.name not in ("org.json", "core_sync.json")]          # a path that names the folder is right; a word that became it is not
sj = (PACK2 / "assistant" / "serve.json").read_text(encoding="utf-8") if (PACK2 / "assistant" / "serve.json").exists() else ""
check("11. made from an installed engine: no 'engine' in its prompts, notes or jobs became the folder's name",
      r.returncode == 0 and '"engine-update"' in sj and not hit, (hit[:6], (r.stdout + r.stderr)[-300:]))

print()
print("ALL PASSED" if not FAILS else f"{len(FAILS)} FAILED: {FAILS}", f"({TMP})")
if not FAILS:
    shutil.rmtree(TMP, ignore_errors=True)
sys.exit(1 if FAILS else 0)

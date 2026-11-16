"""stress_browser.py - load and abuse for the agent's browser (doer/browser.py + doer/browser_mcp.py). Not a gate test:
it takes minutes and is run by hand after a change to either file.

Everything happens in a throwaway browser of its own (own profile, port, state folder, port ledger); the agent's real
one is never touched. An "agent" here is what a real one is to the browser: its own browser_mcp.py process, attached
to the one browser, working in its own tab.

  1. LOAD     N agents at once (default 1, 4, 8), each a few dozen random actions in its own tab: clicks, typing, forms,
              selects, hovers, finds, pictures, keys, waits, a round trip to another page, a dialog, a button inside a
              frame, one inside a shadow root, one that is still moving, a reference that has gone stale.
              Checked: every action's result, that no agent ever landed on another's page, that each page's own
              counters match what its agent did, and how long each call took.
  2. ABUSE    the tab closed under the agent, the page crashed, the browser quit and restarted mid-session, a pop-up
              while in the background, a 5,000-row page, garbage on the protocol, a megabyte of typing, thirty
              park / open cycles, a helper parking under a running job, the server's memory after hundreds of calls.
  3. --front  (opt-in: it puts a window in front of whoever is at the PC) foreground / background flips while an agent
              keeps clicking.
  4. --real   (opt-in: the network) four agents at once on four public sites.

    python assistant/tests/stress_browser.py [--engine playwright|native] [--agents 1,4,8] [--rounds 40] [--seed 7] [--front] [--real]
"""
import os as _os, tempfile as _tf
_os.environ.setdefault("WV_LOG_DIR", _tf.mkdtemp(prefix="wv_logs_"))
import argparse
import json
import os
import queue
import random
import re
import socket
import statistics
import subprocess
import sys
import tempfile
import threading
import time
import urllib.request
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")
HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
TMP = Path(tempfile.mkdtemp(prefix="wv_stress_"))
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

NOWIN = 0x08000000 if os.name == "nt" else 0
FAILS, NOTES = [], []
ENGINE = "playwright"            # --engine: what drives the browser under the tool server (playwright | native)
CALL_TIMEOUT = 45


def check(name, cond, extra=""):
    print(("ok   " if cond else "FAIL ") + name + (f"  {extra}" if extra and not cond else ""), flush=True)
    if not cond:
        FAILS.append(name)


# ---- fixture pages ---------------------------------------------------------------------------------------------
APP = """<!doctype html><html><head><title>agent</title>
<style>@keyframes slide {from {left: 0} to {left: 220px}} #mover {position: relative; animation: slide .5s linear 1}</style></head><body>
<h1 id="who"></h1>
<button id="inc">Add one</button> <span id="count">0</span>
<input id="nm" aria-label="Name"> <input id="agree" type="checkbox" aria-label="Agree">
<select id="pick" aria-label="Pick"><option>Apple</option><option>Banana</option><option>Cherry</option></select>
<button id="hov">Hover target</button> <button id="ask">Ask me</button> <button id="pop">Open a window</button>
<button id="mover">Moving button</button> <a id="next" href="page2.html">Second page</a>
<div id="host"></div>
<iframe id="fr" title="inner" srcdoc="<button onclick='parent.S.frame++;parent.save()'>Frame button</button><script>document.addEventListener('keydown', function(e) { if (e.key === 'F9') { parent.S.keys++; parent.save(); } })</script>"></iframe>
<ul id="churn"></ul><table id="rows"></table>
<script>
const ID = new URLSearchParams(location.search).get('id') || 'x';
document.title = 'agent ' + ID; document.getElementById('who').textContent = 'Agent ' + ID;
var S = JSON.parse(sessionStorage.getItem('state') || 'null') || {id: ID, clicks: 0, hovered: 0, frame: 0, shadow: 0, moved: 0, keys: 0, loads: 0};
S.loads++;
function save() { sessionStorage.setItem('state', JSON.stringify(S)); document.getElementById('count').textContent = S.clicks; }
document.getElementById('inc').onclick = () => { S.clicks++; save(); };
document.getElementById('hov').onmouseover = () => { S.hovered++; save(); };
document.getElementById('ask').onclick = () => { alert('sure ' + ID); };
document.getElementById('pop').onclick = () => { window.open('about:blank', '_blank', 'width=420,height=320,left=200,top=200'); };
document.getElementById('mover').onclick = () => { S.moved++; save(); };
document.addEventListener('keydown', (e) => { if (e.key === 'F9') { S.keys++; save(); } });
const sh = document.getElementById('host').attachShadow({mode: 'open'});
const sb = document.createElement('button'); sb.textContent = 'Shadow button'; sb.onclick = () => { S.shadow++; save(); }; sh.appendChild(sb);
let tick = 0;
setInterval(() => { tick++; const ul = document.getElementById('churn'); ul.textContent = '';
  for (let i = 0; i < 5; i++) { const li = document.createElement('li'); const b = document.createElement('button'); b.textContent = 'Churn ' + i; li.appendChild(b); ul.appendChild(li); } }, 200);
const tb = document.getElementById('rows');
for (let i = 0; i < 300; i++) { const tr = tb.insertRow(); tr.insertCell().textContent = 'row ' + i; tr.insertCell().textContent = 'value ' + (i * 7); }
save();
</script></body></html>"""
PAGE2 = "<!doctype html><html><head><title>second</title></head><body><p>Second page</p><a href='javascript:history.back()'>Go back</a></body></html>"
HEAVY = """<!doctype html><html><head><title>heavy</title></head><body><table id="t"></table><script>
const t = document.getElementById('t');
for (let i = 0; i < 5000; i++) { const r = t.insertRow(); r.insertCell().textContent = 'item ' + i; const b = document.createElement('button'); b.textContent = 'Open ' + i; r.insertCell().appendChild(b); }
</script></body></html>"""
(TMP / "app.html").write_text(APP, encoding="utf-8")
(TMP / "page2.html").write_text(PAGE2, encoding="utf-8")
(TMP / "heavy.html").write_text(HEAVY, encoding="utf-8")
APP_URL = (TMP / "app.html").as_uri()


# ---- one agent = one browser_mcp.py process --------------------------------------------------------------------
class Agent:
    def __init__(self, name, extra=()):
        self.name, self.n, self.refs, self.lat, self.errors, self.cross = str(name), 0, {}, [], [], 0
        self.expect = {"clicks": 0, "frame": 0, "shadow": 0, "moved": 0, "keys": 0}
        self.out = TMP / f"out-{name}"
        self.proc = subprocess.Popen([sys.executable, str(ROOT / "doer" / "browser_mcp.py"), "--cdp-endpoint", browser.cdp_url(),
                                      "--output-dir", str(self.out), "--engine", ENGINE, *extra],
                                     stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, creationflags=NOWIN)
        self.q = queue.Queue()
        threading.Thread(target=self._read, daemon=True).start()
        threading.Thread(target=lambda: [None for _ in iter(self.proc.stderr.readline, b"")], daemon=True).start()

    def _read(self):
        for line in iter(self.proc.stdout.readline, b""):
            self.q.put(line)
        self.q.put(None)

    def raw(self, line: bytes, expect_reply=True, timeout=CALL_TIMEOUT):
        self.proc.stdin.write(line + b"\n")
        self.proc.stdin.flush()
        if not expect_reply:
            return None
        try:
            got = self.q.get(timeout=timeout)
        except queue.Empty:
            return "HANG"
        return None if got is None else json.loads(got.decode("utf-8"))

    def tool(self, name, _own=True, **args):
        self.n += 1
        t0 = time.time()
        rep = self.raw(json.dumps({"jsonrpc": "2.0", "id": self.n, "method": "tools/call", "params": {"name": name, "arguments": args}}).encode())
        dt = time.time() - t0
        if rep == "HANG" or rep is None:
            self.errors.append(f"{name}: {'no answer in ' + str(CALL_TIMEOUT) + 's' if rep == 'HANG' else 'server ended'}")
            return "", True
        self.lat.append((name, dt))
        res = rep.get("result") or {}
        txt = (res.get("content") or [{}])[0].get("text", "")
        if "[ref=" in txt:
            self.refs = {m.group(2): m.group(3) for m in re.finditer(r'- (\w+) "([^"]+)"[^\n]*\[ref=(\w+)\]', txt)}
        if _own:
            m = re.search(r"Page Title: (.*)", txt)
            if m and m.group(1).strip() not in (f"agent {self.name}", "second", ""):
                self.cross += 1
        return txt, bool(res.get("isError"))

    def must(self, what, name, **args):
        txt, err = self.tool(name, **args)
        if err:
            self.errors.append(f"{what}: {txt[:200]}")
        return txt, err

    def ref(self, label):
        if label not in self.refs:
            self.tool("browser_snapshot")
        return self.refs.get(label, "")

    def value(self, js):
        txt, err = self.tool("browser_evaluate", function=js)
        m = re.search(r"### Result\n(.*?)\n### Page", txt, re.S)
        try:
            return None if err or not m else json.loads(m.group(1))
        except ValueError:
            return None

    def alive(self):
        return self.proc.poll() is None

    def close(self):
        try:
            self.proc.stdin.close()
            self.proc.wait(timeout=10)
        except Exception:  # noqa: BLE001
            self.proc.kill()


def run_agent(a: Agent, rounds: int, seed: int):
    rnd = random.Random(seed)
    a.must("open own tab", "browser_tabs", action="new", url=f"{APP_URL}?id={a.name}")
    a.tool("browser_snapshot")
    ops = (["inc"] * 9 + ["type"] * 4 + ["form"] * 2 + ["select"] * 2 + ["hover"] * 3 + ["find"] * 2 + ["snapshot"] * 2 + ["key"] * 2
           + ["wait"] + ["shot"] + ["eval"] * 2 + ["nav"] * 2 + ["dialog"] + ["frame"] * 2 + ["shadow"] * 2 + ["mover"] + ["stale"] + ["tabs"] + ["console"])
    for i in range(rounds):
        op = rnd.choice(ops)
        if op == "inc":
            _, err = a.must("click Add one", "browser_click", element="Add one", target=a.ref("Add one"))
            a.expect["clicks"] += 0 if err else 1
        elif op == "type":
            token = f"{a.name}-{i}-{rnd.randrange(10**6)} é✓"
            a.must("type", "browser_type", element="Name", target=a.ref("Name"), text=token)
            got = a.value("() => document.getElementById('nm').value")
            if got != token:
                a.errors.append(f"typed {token!r} but the field holds {got!r}")
        elif op == "form":
            want = rnd.choice(["Apple", "Banana", "Cherry"])
            a.must("fill form", "browser_fill_form", fields=[{"name": "Name", "type": "textbox", "target": a.ref("Name"), "value": f"form-{a.name}-{i}"},
                                                             {"name": "Agree", "type": "checkbox", "target": a.ref("Agree"), "value": rnd.choice(["true", "false"])},
                                                             {"name": "Pick", "type": "combobox", "target": a.ref("Pick"), "value": want}])
            if a.value("() => document.getElementById('pick').value") != want:
                a.errors.append("the form's select did not take")
        elif op == "select":
            want = rnd.choice(["Apple", "Banana", "Cherry"])
            a.must("select", "browser_select_option", element="Pick", target=a.ref("Pick"), values=[want])
        elif op == "hover":
            a.must("hover", "browser_hover", element="Hover target", target=a.ref("Hover target"))
        elif op == "find":
            txt, err = a.must("find", "browser_find", text="row 29")
            if not err and "match" not in txt:
                a.errors.append("find returned nothing for a row that is there")
        elif op == "snapshot":
            a.must("snapshot", "browser_snapshot")
        elif op == "key":
            _, err = a.must("press key", "browser_press_key", key="F9")
            a.expect["keys"] += 0 if err else 1
        elif op == "wait":
            a.must("wait for text", "browser_wait_for", text=f"Agent {a.name}")
        elif op == "shot":
            txt, err = a.must("screenshot", "browser_take_screenshot", filename=f"s{i}.png")
            if not err and not (a.out / f"s{i}.png").exists():
                a.errors.append("a screenshot said saved but is not on disk")
        elif op == "eval":
            if a.value("() => S.id") != a.name:
                a.errors.append("evaluate ran on a page that is not this agent's")
        elif op == "nav":
            a.must("to the second page", "browser_click", element="Second page", target=a.ref("Second page"))
            a.must("back", "browser_navigate_back")
            a.tool("browser_snapshot")
        elif op == "dialog":
            txt, _ = a.tool("browser_click", element="Ask me", target=a.ref("Ask me"))
            if "dialog is open" not in txt:
                a.errors.append(f"a dialog was not reported: {txt[:120]}")
            txt, err = a.tool("browser_handle_dialog", accept=True)
            if err:
                a.errors.append(f"the dialog could not be answered: {txt[:160]}")
        elif op == "frame":
            _, err = a.must("click inside the frame", "browser_click", element="Frame button", target=a.ref("Frame button"))
            a.expect["frame"] += 0 if err else 1
        elif op == "shadow":
            _, err = a.must("click inside the shadow root", "browser_click", element="Shadow button", target=a.ref("Shadow button"))
            a.expect["shadow"] += 0 if err else 1
        elif op == "mover":
            a.must("reload", "browser_navigate", url=f"{APP_URL}?id={a.name}")       # the button is moving for half a second
            _, err = a.must("click the moving button", "browser_click", element="Moving button", target=a.ref("Moving button"))
            a.expect["moved"] += 0 if err else 1
        elif op == "stale":
            a.tool("browser_snapshot")
            dead = a.refs.get("Churn 3", "")
            time.sleep(0.5)                                                       # the list was rebuilt: the reference is gone
            t0 = time.time()
            txt, err = a.tool("browser_click", element="Churn 3", target=dead or "e99999")
            if time.time() - t0 > 12:
                a.errors.append("a stale reference took more than 12 s to fail")
        elif op == "tabs":
            a.must("list tabs", "browser_tabs", action="list")
        elif op == "console":
            a.must("console", "browser_console_messages", level="info")
    st = a.value("() => JSON.parse(sessionStorage.getItem('state'))") or {}
    a.final = st


def report_load(n_agents, agents, wall):
    lats = [dt for a in agents for _n, dt in a.lat]
    calls = len(lats)
    errs = [f"agent {a.name}: {e}" for a in agents for e in a.errors]
    cross = sum(a.cross for a in agents)
    wrong_page = [a.name for a in agents if (getattr(a, "final", {}) or {}).get("id") != a.name]
    drift = []
    for a in agents:
        f = getattr(a, "final", {}) or {}
        for k, v in a.expect.items():
            if f.get(k) != v:
                drift.append(f"agent {a.name} {k}: page {f.get(k)} agent {v}")
    lats.sort()
    p50 = statistics.median(lats) if lats else 0
    p95 = lats[int(len(lats) * 0.95) - 1] if len(lats) > 5 else (lats[-1] if lats else 0)
    worst = sorted(((dt, nm) for a in agents for nm, dt in a.lat), reverse=True)[:3]
    slow = ", ".join("{} {:.0f} ms".format(nm.replace("browser_", ""), dt * 1000) for dt, nm in worst)
    line = (f"{n_agents} agent(s): {calls} calls in {wall:.1f}s ({calls / max(wall, 0.01):.1f}/s), median {p50 * 1000:.0f} ms, "
            f"95% under {p95 * 1000:.0f} ms, slowest: {slow}")
    NOTES.append(line)
    print("     " + line, flush=True)
    check(f"{n_agents} agent(s) at once: every action did what it was asked", not errs, f"{len(errs)}: {errs[:4]}")
    check(f"{n_agents} agent(s) at once: no agent ever saw another agent's page", cross == 0 and not wrong_page, f"cross {cross} wrong {wrong_page}")
    check(f"{n_agents} agent(s) at once: each page's counters match what its agent did", not drift, str(drift[:5]))
    check(f"{n_agents} agent(s) at once: every server is still running", all(a.alive() for a in agents))


def rss_mb(pid):
    if os.name != "nt":
        return 0.0
    import ctypes
    from ctypes import wintypes

    class PMC(ctypes.Structure):
        _fields_ = [("cb", wintypes.DWORD), ("PageFaultCount", wintypes.DWORD), ("PeakWorkingSetSize", ctypes.c_size_t),
                    ("WorkingSetSize", ctypes.c_size_t), ("QuotaPeakPagedPoolUsage", ctypes.c_size_t), ("QuotaPagedPoolUsage", ctypes.c_size_t),
                    ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t), ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
                    ("PagefileUsage", ctypes.c_size_t), ("PeakPagefileUsage", ctypes.c_size_t)]
    k = ctypes.windll.kernel32
    k.OpenProcess.restype = wintypes.HANDLE
    h = k.OpenProcess(0x1000 | 0x0010, False, int(pid))
    if not h:
        return 0.0
    c = PMC()
    c.cb = ctypes.sizeof(PMC)
    ok = ctypes.windll.psapi.GetProcessMemoryInfo(wintypes.HANDLE(h), ctypes.byref(c), c.cb)
    k.CloseHandle(wintypes.HANDLE(h))
    return c.WorkingSetSize / 1048576 if ok else 0.0


def tabs():
    with urllib.request.urlopen(browser.cdp_url() + "/json/list", timeout=5) as r:
        return [t for t in json.loads(r.read().decode("utf-8", "replace")) if t.get("type") == "page"]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--agents", default="1,4,8")
    ap.add_argument("--rounds", type=int, default=40)
    ap.add_argument("--seed", type=int, default=7)
    ap.add_argument("--engine", choices=["playwright", "native"], default="playwright")
    ap.add_argument("--front", action="store_true")
    ap.add_argument("--real", action="store_true")
    ns = ap.parse_args()
    global ENGINE
    ENGINE = ns.engine
    print(f"engine: {ENGINE}")
    if not browser.chrome_exe():
        print("no Chromium on this machine: nothing to stress")
        return 0
    browser.MODE = "background"
    browser.open_browser(show=False)
    pid = int(browser._state().get("pid") or 0)
    print(f"throwaway browser up (process {pid}, port {PORT}); the agent's own is untouched\n")
    try:
        # ---- 1. LOAD ---------------------------------------------------------------------------------------------
        for n in [int(x) for x in ns.agents.split(",") if x.strip()]:
            agents = [Agent(f"L{n}a{i}") for i in range(n)]
            t0 = time.time()
            th = [threading.Thread(target=run_agent, args=(a, ns.rounds, ns.seed * 1000 + n * 10 + i)) for i, a in enumerate(agents)]
            [t.start() for t in th]
            [t.join(timeout=900) for t in th]
            check(f"{n} agent(s) at once: all finished", not any(t.is_alive() for t in th))
            report_load(n, agents, time.time() - t0)
            [a.close() for a in agents]
            check(f"{n} agent(s) at once: the browser is the same process, still up", bool(browser.alive()) and browser._pid_alive(pid))
            browser.park()
            check(f"{n} agent(s) at once: afterwards one blank tab is left", len(tabs()) == 1, str(len(tabs())))
            browser.open_browser(show=False)

        # ---- 2. ABUSE --------------------------------------------------------------------------------------------
        print()
        a = Agent("X")
        a.must("open", "browser_tabs", action="new", url=f"{APP_URL}?id=X")
        mine = [t for t in tabs() if "id=X" in t.get("url", "")][0]["id"]
        urllib.request.urlopen(browser.cdp_url() + "/json/close/" + mine, timeout=5).read()
        time.sleep(0.5)
        t0 = time.time()
        txt, err = a.tool("browser_snapshot", _own=False)
        check("the tab closed under the agent: the next call answers (no hang, no crash)", a.alive() and time.time() - t0 < 20, f"{time.time() - t0:.1f}s {txt[:100]}")
        txt, err = a.tool("browser_navigate", _own=False, url=f"{APP_URL}?id=X")
        check("the tab closed under the agent: it goes on in a tab that is left", not err and "agent X" in txt, txt[:160])

        ws = [t for t in tabs() if "id=X" in t.get("url", "")][0]["webSocketDebuggerUrl"]
        try:
            browser._cdp_calls(ws, [("Page.crash", {})])
        except Exception:  # noqa: BLE001 - the page dies before it can answer
            pass
        time.sleep(1.0)
        t0 = time.time()
        txt, err = a.tool("browser_snapshot", _own=False)
        check("the page crashed: the next call answers within 30 s and the server lives", a.alive() and time.time() - t0 < 30, f"{time.time() - t0:.1f}s")
        txt, err = a.tool("browser_navigate", _own=False, url=f"{APP_URL}?id=X")
        if err:
            txt, err = a.tool("browser_tabs", _own=False, action="new", url=f"{APP_URL}?id=X")
        check("the page crashed: the agent works again afterwards", not err and "agent X" in txt, txt[:200])

        browser.close_browser(force=True, quit=True)
        check("the browser was quit under the agent", not browser.alive())
        t0 = time.time()
        txt, err = a.tool("browser_snapshot", _own=False)
        check("browser gone: the call fails cleanly, soon, and the server lives", err and a.alive() and time.time() - t0 < 30, f"{time.time() - t0:.1f}s {txt[:100]}")
        browser.open_browser(show=False)
        pid = int(browser._state().get("pid") or 0)
        txt, err = a.tool("browser_navigate", _own=False, url=f"{APP_URL}?id=X")
        check("browser restarted: the same server attaches to the new one and works", not err and "agent X" in txt, txt[:200])

        txt, _ = a.tool("browser_snapshot")
        before = len(tabs())
        a.must("open a pop-up", "browser_click", element="Open a window", target=a.ref("Open a window"))
        time.sleep(1.5)
        pops = [t for t in tabs() if t.get("url", "").startswith("about:blank")]
        where = []
        for t in pops:
            try:
                bws = (browser.alive() or {}).get("webSocketDebuggerUrl")
                wid = browser._cdp_calls(bws, [("Browser.getWindowForTarget", {"targetId": t["id"]})])[0]["windowId"]
                where.append(browser._cdp_calls(bws, [("Browser.getWindowBounds", {"windowId": wid})])[0]["bounds"].get("left"))
            except Exception as e:  # noqa: BLE001
                where.append(type(e).__name__)
        check("a pop-up opened in the background is put in the background too", len(tabs()) == before + 1 and where and all(isinstance(x, int) and x < -10000 for x in where), f"{where}")
        txt, err = a.tool("browser_click", element="Add one", target=a.ref("Add one"))
        check("after a pop-up the agent is still on its own page", not err and "agent X" in txt, txt[:120])

        t0 = time.time()
        txt, err = a.tool("browser_navigate", _own=False, url=(TMP / "heavy.html").as_uri())
        big = time.time() - t0
        check("a 5,000-row page: loads and is read", not err and "Page Title: heavy" in txt, txt[:120])
        check("a 5,000-row page: the answer is cut to size, with a note", len(txt) < 70000 and "snapshot cut" in txt, str(len(txt)))
        check("a 5,000-row page: under 20 s", big < 20, f"{big:.1f}s")
        NOTES.append(f"5,000-row page: navigate + snapshot {big:.1f}s, answer {len(txt)} characters")
        t0 = time.time()
        txt, err = a.tool("browser_find", _own=False, text="Open 4999")
        check("a 5,000-row page: find reaches the last row", not err and "Open 4999" in txt, txt[:120])
        m = re.search(r"\[ref=(\w+)\]", txt)
        txt, err = a.tool("browser_click", _own=False, element="Open 4999", target=m.group(1) if m else "e1")
        check("a 5,000-row page: the last row's button is clicked (scrolled to)", not err, txt[:160])

        for bad, why in [(b"this is not json", "not JSON"), (b"{\"jsonrpc\":\"2.0\",\"id\":900,\"method\":\"tools/call\"}", "no params"),
                         (b"{\"jsonrpc\":\"2.0\",\"id\":901,\"method\":\"tools/call\",\"params\":{\"name\":\"browser_click\",\"arguments\":{\"target\":12345}}}", "a number for a reference"),
                         (b"{\"jsonrpc\":\"2.0\",\"id\":902,\"method\":\"tools/call\",\"params\":{\"name\":\"browser_fill_form\",\"arguments\":{\"fields\":\"nope\"}}}", "a string for a list"),
                         (b"{\"jsonrpc\":\"2.0\",\"id\":903,\"method\":\"tools/call\",\"params\":{\"name\":\"browser_resize\",\"arguments\":{}}}", "missing sizes"),
                         (b"{\"jsonrpc\":\"2.0\",\"id\":904,\"method\":\"tools/call\",\"params\":{\"name\":\"browser_evaluate\",\"arguments\":{\"function\":\"() => { throw new Error('boom') }\"}}}", "a script that throws"),
                         (b"{\"jsonrpc\":\"2.0\",\"id\":905,\"method\":\"tools/call\",\"params\":{\"name\":\"browser_evaluate\",\"arguments\":{\"function\":\"() => { while(true){} }\"}}}", "a script that never ends"),
                         (b"[]", "an empty batch"), (b"{\"jsonrpc\":\"2.0\",\"id\":906,\"method\":\"tools/call\",\"params\":{\"name\":\"browser_navigate\",\"arguments\":{\"url\":\"javascript:alert(1)\"}}}", "a javascript: address")]:
            t0 = time.time()
            rep = a.raw(bad, expect_reply=(bad != b"[]"), timeout=60)
            ok = rep != "HANG" and a.alive()
            check(f"garbage in ({why}): answered, server alive", ok, f"{time.time() - t0:.1f}s {str(rep)[:120]}")
            if rep == "HANG":
                break
        rep = a.raw(b"{\"jsonrpc\":\"2.0\",\"id\":950,\"method\":\"ping\"}", timeout=30)
        check("after all the garbage the server still answers a ping", isinstance(rep, dict) and rep.get("id") == 950, str(rep)[:120])
        if not (isinstance(rep, dict) and rep.get("id") == 950):
            # a server that hung stays hung: the rest of the run goes on with a fresh one instead of waiting on it for hours
            a.proc.kill()
            a = Agent("X2")
            a.name = "X"
            a.must("open", "browser_tabs", action="new", url=f"{APP_URL}?id=X")

        txt, err = a.tool("browser_navigate", url=f"{APP_URL}?id=X")
        big_text = ("lorem ipsum é✓ " * 20000)[:200000]          # a very large paste (a megabyte in a one-line field keeps the PAGE busy for 20 s)
        t0 = time.time()
        txt, err = a.tool("browser_type", element="Name", target=a.ref("Name"), text=big_text)
        got = a.value("() => document.getElementById('nm').value.length")
        check("200,000 characters typed into a field arrive whole", not err and got == len(big_text), f"err {err} got {got} in {time.time() - t0:.1f}s")
        NOTES.append(f"200,000 characters typed and read back: {time.time() - t0:.1f}s")

        m0 = rss_mb(a.proc.pid)
        a.tool("browser_snapshot")
        for i in range(400):
            a.tool("browser_click", element="Add one", target=a.ref("Add one"))
            if i % 50 == 0:
                a.tool("browser_take_screenshot", filename="m.png")
        m1 = rss_mb(a.proc.pid)
        NOTES.append(f"server memory: {m0:.0f} MB before, {m1:.0f} MB after 400 more clicks ({m1 - m0:+.0f} MB)")
        check("400 more clicks: the server's memory grows by less than 60 MB", m1 - m0 < 60, f"{m0:.0f} -> {m1:.0f} MB")
        check("400 more clicks: none failed", not a.errors, str(a.errors[:3]))

        # a helper parking while "a job" (this agent's server pid stands in for it) runs must not take the page away
        browser.JOB_LOCK.write_text(json.dumps({"pid": a.proc.pid, "ts": time.time()}), encoding="utf-8")
        n_tabs = len(tabs())
        check("a helper's close under a running job is refused", browser.close_browser() is False and len(tabs()) == n_tabs)
        txt, err = a.tool("browser_click", element="Add one", target=a.ref("Add one"))
        check("...and the job's page is untouched", not err and "agent X" in txt)
        browser.JOB_LOCK.unlink()
        a.close()

        t0 = time.time()
        same, one = True, True
        for i in range(30):
            browser.open_browser(show=False)
            req = urllib.request.Request(browser.cdp_url() + "/json/new?about:blank", method="PUT")
            urllib.request.urlopen(req, timeout=8).read()
            browser.close_browser()
            same = same and int(browser._state().get("pid") or 0) == pid and browser._pid_alive(pid)
            one = one and len(tabs()) == 1
        NOTES.append(f"30 open / park cycles: {time.time() - t0:.1f}s")
        check("30 open / park cycles: the same process every time", same)
        check("30 open / park cycles: one tab left every time (no pile-up)", one, str(len(tabs())))
        check("30 open / park cycles: still no taskbar button, still not 'in use'", (browser.hidden() or os.name != "nt") and not browser.in_use())

        # ---- 3. foreground / background flips under a working agent (opt-in) ----------------------------------------
        if ns.front and os.name == "nt":
            import ctypes
            u = ctypes.windll.user32
            keep = u.GetForegroundWindow()
            browser.open_browser(show=False)
            f = Agent("F")
            f.must("open", "browser_tabs", action="new", url=f"{APP_URL}?id=F")
            f.tool("browser_snapshot")
            stop = threading.Event()

            def clicker():
                while not stop.is_set():
                    _, e = f.must("click during a flip", "browser_click", element="Add one", target=f.ref("Add one"))
                    f.expect["clicks"] += 0 if e else 1

            th = threading.Thread(target=clicker)
            th.start()
            ok_all = True
            for _ in range(4):
                ok1, _d = browser.show_on_screen()
                time.sleep(1.0)
                ok2, _d = browser.send_to_back()
                time.sleep(0.6)
                ok_all = ok_all and ok1 and ok2
            stop.set()
            th.join(timeout=60)
            got = (f.value("() => S.clicks"), f.expect["clicks"])
            if keep:
                u.SetForegroundWindow(keep)
            check("4 foreground / background flips while an agent keeps clicking: every flip worked", ok_all)
            check("...no click was lost or failed during them", not f.errors and got[0] == got[1] and got[1] > 5, f"page {got[0]} agent {got[1]} errors {f.errors[:2]}")
            check("...same browser process throughout", browser._pid_alive(pid))
            NOTES.append(f"flips: {got[1]} clicks landed across 4 foreground / background flips")
            f.close()
        else:
            print("skip the foreground flips (--front: they put a window in front for a few seconds)")

        # ---- 4. real sites, four at once (opt-in) -------------------------------------------------------------------
        if ns.real:
            browser.open_browser(show=False)
            sites = ["https://example.com/", "https://en.wikipedia.org/wiki/Web_browser", "https://www.iana.org/help/example-domains", "https://www.w3.org/TR/wai-aria-1.2/"]
            agents = [Agent(f"R{i}") for i in range(len(sites))]

            def real(a, url):
                txt, err = a.tool("browser_tabs", _own=False, action="new", url=url)
                if err:
                    a.errors.append(f"open {url}: {txt[:120]}")
                    return
                txt, err = a.tool("browser_snapshot", _own=False)
                links = re.findall(r'- link "([^"]{3,40})"[^\n]*\[ref=(\w+)\]', txt)[:6]
                for name, ref in links[:4]:
                    t, e = a.tool("browser_hover", _own=False, element=name, target=ref)
                t, e = a.tool("browser_take_screenshot", _own=False, filename="real.png")
                if e or not (a.out / "real.png").exists():
                    a.errors.append(f"picture of {url}: {t[:120]}")
                t, e = a.tool("browser_find", _own=False, text="the")
                a.host = re.search(r"Page URL: (\S+)", a.tool("browser_snapshot", _own=False)[0])
                a.want = url

            t0 = time.time()
            th = [threading.Thread(target=real, args=(a, u)) for a, u in zip(agents, sites)]
            [t.start() for t in th]
            [t.join(timeout=240) for t in th]
            errs = [e for a in agents for e in a.errors]
            mixed = [a.name for a in agents if not getattr(a, "host", None) or a.want.split("/")[2] not in a.host.group(1)]
            NOTES.append(f"4 real sites at once: {sum(len(a.lat) for a in agents)} calls in {time.time() - t0:.1f}s")
            check("4 agents on 4 real sites at once: no errors", not errs, str(errs[:3]))
            check("4 agents on 4 real sites at once: each stayed on its own site", not mixed, str(mixed))
            [a.close() for a in agents]
        else:
            print("skip the real sites (--real: it uses the network)")
    finally:
        try:
            browser.close_browser(force=True, quit=True)
        except Exception:  # noqa: BLE001
            pass
    print("\n--- numbers ---")
    for n in NOTES:
        print("  " + n)
    print()
    print("ALL PASSED" if not FAILS else f"{len(FAILS)} FAILED: {FAILS}", f"({TMP})")
    return 1 if FAILS else 0


if __name__ == "__main__":
    sys.exit(main())

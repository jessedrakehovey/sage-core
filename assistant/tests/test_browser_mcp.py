"""browser_mcp.py (2026-11-14): the doer's own browser tool server, end to end, without Node.

1. Pure parts: the tool list is exactly the browser tools the doer allows; find_in_snapshot; the JSON-RPC shapes.
2. doer._mcp_config points the doer at this server with the jobs' interpreter (no npx anywhere in it).
3. Live: a throwaway browser of the test's own (its own profile folder and port; never the doer's), the server as a
   subprocess, every tool over the wire: navigate, snapshot with references, click by reference, type, fill a form,
   select, press a key, hover, wait, evaluate, find, tabs, a dialog, console messages, resize, a screenshot.
   Skipped (said, not failed) on a machine with no Chromium.
4. The agent's own cursor: it is in the page after an action, at the middle of the element it touched; it GLIDES there
   (sampled mid-way from a second connection: many places in between, each further along); it never shows in the
   snapshot, never takes a click meant for the page, shows in pictures; `--cursor off` leaves the page untouched.
5. The small arguments the Node server had: `filename` (the result lands in the job's own folder), screenshot `scale`.
   python assistant/tests/test_browser_mcp.py
"""
import os as _os, tempfile as _tf
_os.environ.setdefault("WV_LOG_DIR", _tf.mkdtemp(prefix="wv_logs_"))
import io
import os
import json
import re
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
TMP = Path(tempfile.mkdtemp(prefix="wv_browser_mcp_"))
FAILS = []
sys.path.insert(0, str(ROOT / "doer"))
sys.path.insert(0, str(ROOT / "scripts"))
import browser_mcp as bm  # noqa: E402


def check(name, cond, extra=""):
    print(("ok   " if cond else "FAIL ") + name + (f"  {extra}" if extra and not cond else ""))
    if not cond:
        FAILS.append(name)


# ---- 1. pure parts --------------------------------------------------------------------------------------------
import doer  # noqa: E402
allowed = set(re.findall(r"mcp__" + re.escape(doer.MCP) + r"__(browser_\w+)", doer.ALLOWED))
served = {n for n, _d, _s in bm.TOOLS}
check("the server offers exactly the browser tools the doer allows", served == allowed, f"only allowed {allowed - served}; only served {served - allowed}")
denied = set(re.findall(r"mcp__" + re.escape(doer.MCP) + r"__(browser_\w+)", doer.DENIED))
check("no denied browser tool exists in the server at all", not (served & denied), str(served & denied))
check("every tool has an implementation", all(hasattr(bm.Browser, "t_" + n[len("browser_"):]) for n in served))

SNAP = '- main [ref=e1]:\n  - heading "Invoices" [ref=e2]\n  - button "Pay now" [ref=e3]\n  - button "Cancel" [ref=e4]\n  - link "Pay later" [ref=e5]'
got = bm.find_in_snapshot(SNAP, text="pay")
check("find by text is case-insensitive and counts", got.startswith("2 match") and "e3" in got and "e5" in got, got)
got = bm.find_in_snapshot(SNAP, regex="/CANCEL/i")
check("find by /regex/i", got.startswith("1 match") and "e4" in got, got)
check("find with nothing found says so", bm.find_in_snapshot(SNAP, text="zebra") == "no match in the page snapshot")
try:
    bm.find_in_snapshot(SNAP, text="a", regex="b")
    check("find refuses text and regex together", False)
except bm.ToolError:
    check("find refuses text and regex together", True)
check("a long snapshot is cut with a note", "snapshot cut" in bm.clip("x" * 100, 10) and bm.clip("abc", 10) == "abc")


class _NoBrowser:
    def call(self, name, args):
        raise bm.ToolError("boom")


rep = bm.handle({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2024-11-05"}}, _NoBrowser())
check("initialize echoes the client's protocol version", rep["result"]["protocolVersion"] == "2024-11-05" and "tools" in rep["result"]["capabilities"])
check("a notification gets no reply", bm.handle({"jsonrpc": "2.0", "method": "notifications/initialized"}, _NoBrowser()) is None)
rep = bm.handle({"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": {"name": "browser_click", "arguments": {}}}, _NoBrowser())
check("a tool failure is a result the model reads, not a protocol error", rep["result"].get("isError") and "boom" in rep["result"]["content"][0]["text"])
rep = bm.handle({"jsonrpc": "2.0", "id": 3, "method": "nope"}, _NoBrowser())
check("an unknown method is a JSON-RPC error", rep["error"]["code"] == -32601)
sin, sout = io.BytesIO(b'not json\n{"jsonrpc":"2.0","id":9,"method":"ping"}\n'), io.BytesIO()
bm.serve(_NoBrowser(), sin, sout)
lines = [json.loads(x) for x in sout.getvalue().splitlines()]
check("a bad line is answered and the loop goes on", len(lines) == 2 and lines[0]["error"]["code"] == -32700 and lines[1]["id"] == 9)

# ---- 2. the doer's config ---------------------------------------------------------------------------------------
cfg = json.loads(doer._mcp_config(TMP).read_text(encoding="utf-8"))
srv = cfg["mcpServers"][doer.MCP]
flat = json.dumps(cfg)
check("the doer starts its browser tools with Python, not npx", "npx" not in flat and "@playwright" not in flat and srv["args"][0].endswith("browser_mcp.py"), flat)
check("the config carries the browser's address and the job's folder", "--cdp-endpoint" in srv["args"] and "--output-dir" in srv["args"])
_lab = str(((doer.CFG.get("cursor") or {}) if isinstance(doer.CFG.get("cursor"), dict) else {}).get("label") or "").strip()
check("the config turns the agent's cursor on; no name on it unless doer.cursor.label is set", "--cursor" in srv["args"]
      and srv["args"][srv["args"].index("--cursor") + 1] == "on" and (("--cursor-label" in srv["args"]) == bool(_lab)))
check("the server's interpreter exists", Path(srv["command"]).exists(), srv["command"])

# ---- 3. live ----------------------------------------------------------------------------------------------------
import browser  # noqa: E402
exe = browser.chrome_exe()
ok_pw, why = bm.check()
if not exe or not ok_pw:
    print(f"skip the live part: {'no Chromium on this machine' if not exe else why}")
else:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    NOWIN = 0x08000000 if os.name == "nt" else 0
    chrome = subprocess.Popen([str(exe), f"--remote-debugging-port={port}", f"--user-data-dir={TMP / 'profile'}", "--headless=new",
                               "--no-first-run", "--no-default-browser-check", "about:blank"],
                              stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, creationflags=NOWIN)
    cdp = f"http://127.0.0.1:{port}"
    for _ in range(60):
        try:
            urllib.request.urlopen(cdp + "/json/version", timeout=1).read()
            break
        except Exception:
            time.sleep(0.25)
    page = TMP / "page.html"
    page.write_text("""<html><head><title>Fixture</title></head><body>
<h1>Hello</h1>
<button id="go" onclick="document.title='clicked'">Go</button>
<button onclick="alert('are you sure')">Ask</button>
<button onmouseover="this.textContent='Hovered'">Hover me</button>
<input aria-label="Name" onkeydown="if(event.key==='Enter')document.title='submitted:'+this.value">
<input type="checkbox" aria-label="Agree">
<select aria-label="Pick"><option value="a">Apple</option><option value="b">Banana</option></select>
<script>console.log('hello from the page'); console.error('a page error');</script>
</body></html>""", encoding="utf-8")
    out = TMP / "out"
    srv = subprocess.Popen([sys.executable, str(ROOT / "doer" / "browser_mcp.py"), "--cdp-endpoint", cdp, "--output-dir", str(out),
                            "--cursor-label", "Pip", "--cursor-color", "#4A5C43"],
                           stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, creationflags=NOWIN)
    n = [0]

    def rpc(method, params=None):
        n[0] += 1
        srv.stdin.write(json.dumps({"jsonrpc": "2.0", "id": n[0], "method": method, "params": params or {}}).encode() + b"\n")
        srv.stdin.flush()
        return json.loads(srv.stdout.readline().decode("utf-8"))

    def tool(name, **args):
        r = rpc("tools/call", {"name": name, "arguments": args})["result"]
        return r["content"][0]["text"], bool(r.get("isError"))

    def ref(snap, pattern):
        m = re.search(pattern + r'[^\n]*?\[ref=(\w+)\]', snap)
        return m.group(1) if m else ""

    try:
        rep = rpc("initialize", {"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "t", "version": "0"}})
        check("live: initialize", rep["result"]["serverInfo"]["name"] == bm.NAME)
        check("live: tools/list", len(rpc("tools/list")["result"]["tools"]) == len(bm.TOOLS))
        txt, err = tool("browser_navigate", url=page.as_uri())
        check("live: navigate returns the page and its snapshot", not err and "Page Title: Fixture" in txt and 'button "Go"' in txt, txt[:300])
        go, name, agree, pick, hov, ask = (ref(txt, p) for p in ('button "Go"', 'textbox "Name"', 'checkbox "Agree"', 'combobox "Pick"', 'button "Hover me"', 'button "Ask"'))
        check("live: the snapshot marks elements with references", all((go, name, agree, pick, hov, ask)), txt[:400])
        txt, err = tool("browser_click", element="Go", target=go)
        check("live: click by reference (target)", not err and "Page Title: clicked" in txt, txt[:200])
        txt, err = tool("browser_navigate", url=page.as_uri())
        go = ref(txt, 'button "Go"')
        txt, err = tool("browser_click", element="Go", ref=go)
        check("live: click by the older `ref` argument", not err and "Page Title: clicked" in txt, txt[:200])
        txt, err = tool("browser_click", target="#go")
        check("live: click by a plain selector", not err, txt[:200])
        txt, err = tool("browser_click", target="e9999")
        check("live: a dead reference is an error in Playwright's own words", err and "aria-ref=e9999" in txt, txt[:200])
        snap, _ = tool("browser_snapshot")
        name, agree, pick, hov, ask = (ref(snap, p) for p in ('textbox "Name"', 'checkbox "Agree"', 'combobox "Pick"', 'button "Hover me"', 'button "Ask"'))
        txt, err = tool("browser_type", target=name, text="Ada", submit=True)
        check("live: type and submit", not err and "submitted:Ada" in txt, txt[:200])
        txt, err = tool("browser_fill_form", fields=[{"name": "Name", "type": "textbox", "target": name, "value": "Grace"},
                                                     {"name": "Agree", "type": "checkbox", "target": agree, "value": "true"},
                                                     {"name": "Pick", "type": "combobox", "target": pick, "value": "Banana"}])
        check("live: fill a form", not err and "filled 3" in txt, txt[:200])
        txt, err = tool("browser_evaluate", function="() => [document.querySelector('[aria-label=Name]').value, document.querySelector('[aria-label=Agree]').checked, document.querySelector('select').value]")
        check("live: evaluate reads what the form now holds", not err and '"Grace"' in txt and "true" in txt and '"b"' in txt, txt[:200])
        txt, err = tool("browser_select_option", target=pick, values=["Apple"])
        check("live: select an option by its visible text", not err and '"Apple" [selected]' in txt, txt[:400])
        txt, err = tool("browser_evaluate", function="(el) => el.tagName", target=pick)
        check("live: evaluate on an element", not err and "SELECT" in txt, txt[:200])
        txt, err = tool("browser_hover", target=hov)
        check("live: hover", not err and "Hovered" in txt, txt[:300])
        txt, err = tool("browser_press_key", key="End")
        check("live: press a key", not err)
        txt, err = tool("browser_wait_for", text="Hello")
        check("live: wait for text", not err and "waited for Hello" in txt, txt[:200])
        txt, err = tool("browser_wait_for", text="never there", time=1)
        check("live: waiting for text that never comes is an error", err, txt[:200])
        txt, err = tool("browser_find", text="hover")
        check("live: find in the page", not err and "match" in txt and "Hover" in txt, txt[:200])
        txt, err = tool("browser_console_messages", level="info")
        check("live: console messages", "hello from the page" in txt and "a page error" in txt, txt[:200])
        txt, err = tool("browser_console_messages", level="error")
        check("live: console messages by level", "a page error" in txt and "hello from the page" not in txt, txt[:200])
        txt, err = tool("browser_click", target=ask)
        check("live: a click that opens a dialog reports the dialog", "dialog is open" in txt and "are you sure" in txt, txt[:300])
        txt, err = tool("browser_snapshot")
        check("live: nothing else runs while a dialog is open", "browser_handle_dialog" in txt, txt[:200])
        txt, err = tool("browser_handle_dialog", accept=True)
        check("live: the dialog is answered", not err and "dialog accepted" in txt, txt[:200])
        txt, err = tool("browser_take_screenshot", filename="../../escape.png")
        shot = out / "escape.png"
        check("live: a screenshot lands inside the job's folder whatever path was asked", not err and shot.exists() and shot.stat().st_size > 500 and not (TMP.parent / "escape.png").exists(), txt)
        txt, err = tool("browser_resize", width=900, height=700)
        check("live: resize", not err and "900x700" in txt, txt)
        txt, err = tool("browser_tabs", action="new", url=page.as_uri())
        check("live: a new tab becomes the current one", not err and "(current)" in txt.splitlines()[-1], txt)
        txt, err = tool("browser_tabs", action="close")
        check("live: closing it goes back to a tab that is left", not err and txt.count("\n- ") + txt.count("\n-") >= 1, txt)
        txt, err = tool("browser_tabs", action="select", index=0)
        check("live: select a tab", not err and "- 0: (current)" in txt, txt)
        txt, err = tool("browser_navigate_back")
        check("live: back", not err, txt[:200])
        # ---- 4. the agent's own cursor ---------------------------------------------------------------------------
        big = TMP / "big.html"
        big.write_text("""<html><head><title>Cursor</title></head><body style="margin:0;height:2400px">
<button id="a" style="position:absolute;left:40px;top:40px;width:120px;height:40px" onclick="document.title='A'">Alpha</button>
<button id="b" style="position:absolute;left:900px;top:520px;width:140px;height:44px" onclick="document.title='B'">Bravo</button>
<button id="far" style="position:absolute;left:300px;top:2000px;width:140px;height:44px" onclick="document.title='FAR'">Far down</button>
</body></html>""", encoding="utf-8")
        tool("browser_resize", width=1200, height=800)
        txt, err = tool("browser_navigate", url=big.as_uri())
        a_ref, b_ref, far_ref = (ref(txt, p) for p in ('button "Alpha"', 'button "Bravo"', 'button "Far down"'))
        STATE = ("() => { const c = window.__agentCursor; const h = document.querySelector('[data-agent-cursor]');"
                 " return c ? {x: c.x, y: c.y, host: !!h, hidden: h && h.getAttribute('aria-hidden'), label: c.tag.textContent,"
                 " fill: c.arrow.getAttribute('fill'), pe: getComputedStyle(h).pointerEvents} : null }")

        def cursor():
            t, _e = tool("browser_evaluate", function=STATE)
            m = re.search(r"### Result\n(.*?)\n### Page", t, re.S)
            return json.loads(m.group(1)) if m else None

        check("cursor: a page nobody touched yet has none", cursor() is None)
        txt, err = tool("browser_click", element="Alpha", target=a_ref)
        c = cursor()
        check("cursor: after a click it is in the page, at the middle of the button", not err and "Page Title: A" in txt and c
              and abs(c["x"] - 100) <= 1 and abs(c["y"] - 60) <= 1, str(c))
        check("cursor: it carries the agent's name and colour", c and c["label"] == "Pip" and c["fill"] == "#4A5C43", str(c))
        check("cursor: hidden from the accessibility tree and never in the way of a click", c and c["hidden"] == "true" and c["pe"] == "none", str(c))
        snap, _ = tool("browser_snapshot")
        check("cursor: the snapshot does not show it", "Pip" not in snap and "agent-cursor" not in snap, snap[:300])
        txt, _ = tool("browser_evaluate", function="() => { const e = document.elementFromPoint(100, 60); return e && e.id }")
        check("cursor: what is under it is still the page's own element", '"a"' in txt, txt[:120])

        # the glide, sampled from a second connection while the click is still on its way
        import browser as _b
        ws = [t for t in json.loads(urllib.request.urlopen(cdp + "/json/list", timeout=5).read()) if t.get("type") == "page" and "big.html" in t.get("url", "")][0]["webSocketDebuggerUrl"]
        SAMPLE = ("Runtime.evaluate", {"expression": "(() => { const c = window.__agentCursor; if (!c) return null; const m = new DOMMatrix(getComputedStyle(c.wrap).transform); return [Math.round(m.e), Math.round(m.f)] })()", "returnByValue": True})
        n[0] += 1
        srv.stdin.write(json.dumps({"jsonrpc": "2.0", "id": n[0], "method": "tools/call", "params": {"name": "browser_click", "arguments": {"element": "Bravo", "target": b_ref}}}).encode() + b"\n")
        srv.stdin.flush()
        seen, t0 = [], time.time()
        while time.time() - t0 < 0.9:
            try:
                v = ((_b._cdp_calls(ws, [SAMPLE])[0] or {}).get("result") or {}).get("value")
                if v:
                    seen.append(tuple(v))
            except Exception:
                pass
            time.sleep(0.03)
        rep = json.loads(srv.stdout.readline().decode("utf-8"))["result"]
        took = time.time() - t0
        between = [p for p in dict.fromkeys(seen) if 100 < p[0] + 4 < 968]
        xs = [p[0] for p in dict.fromkeys(seen)]
        check("cursor: the click still lands", "Page Title: B" in rep["content"][0]["text"], rep["content"][0]["text"][:160])
        check("cursor: it glides: several places between the two buttons", len(between) >= 4, f"{len(between)} of {len(seen)} samples: {list(dict.fromkeys(seen))[:12]}")
        check("cursor: each place is further along than the last", xs == sorted(xs) and len(xs) >= 4, str(xs[:14]))
        check("cursor: the glide costs well under a second", took < 2.5, f"{took:.2f}s")
        c = cursor()
        check("cursor: it ends on the second button", c and abs(c["x"] - 970) <= 1 and abs(c["y"] - 542) <= 1, str(c))
        txt, err = tool("browser_click", element="Far down", target=far_ref)
        c = cursor()
        check("cursor: a target below the fold is scrolled to, and the cursor is on it inside the window", not err and "Page Title: FAR" in txt and c and 0 <= c["y"] <= 800, str(c))
        txt, err = tool("browser_take_screenshot", filename="with-cursor.png", scale="device")
        check("cursor: a picture of the page takes without trouble (scale=device)", not err and (out / "with-cursor.png").stat().st_size > 500, txt)

        # ---- 5. filename arguments -----------------------------------------------------------------------------
        txt, err = tool("browser_snapshot", filename="../snap.yml")
        check("filename: the snapshot is saved inside the job's folder", not err and (out / "snap.yml").exists() and "Far down" in (out / "snap.yml").read_text(encoding="utf-8") and not (TMP / "snap.yml").exists(), txt)
        txt, err = tool("browser_evaluate", function="() => ({a: 1})", filename="val.json")
        check("filename: an evaluate result is saved", not err and json.loads((out / "val.json").read_text(encoding="utf-8")) == {"a": 1}, txt)
        txt, err = tool("browser_find", text="far", filename="found.md")
        check("filename: a find result is saved", not err and "Far down" in (out / "found.md").read_text(encoding="utf-8"), txt)
        txt, err = tool("browser_console_messages", level="info", filename="console.log")
        check("filename: console messages are saved", not err and (out / "console.log").exists(), txt)

        # --cursor off: a second server on the same browser leaves the page alone
        off = subprocess.Popen([sys.executable, str(ROOT / "doer" / "browser_mcp.py"), "--cdp-endpoint", cdp, "--output-dir", str(out), "--cursor", "off"],
                               stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, creationflags=NOWIN)

        def otool(name, **args):
            off.stdin.write(json.dumps({"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": name, "arguments": args}}).encode() + b"\n")
            off.stdin.flush()
            r = json.loads(off.stdout.readline().decode("utf-8"))["result"]
            return r["content"][0]["text"], bool(r.get("isError"))

        try:
            otool("browser_navigate", url=big.as_uri())
            t2, e2 = otool("browser_click", target="#a")
            t3, _ = otool("browser_evaluate", function="() => !!document.querySelector('[data-agent-cursor]')")
            check("--cursor off: the click lands and nothing is added to the page", not e2 and "Page Title: A" in t2 and "false" in t3.split("### Page")[0], t3[:120])
        finally:
            off.stdin.close()
            off.wait(timeout=10)

        txt, err = tool("browser_run_code_unsafe", code="1")
        check("live: a denied tool does not exist", err and "no such tool" in txt, txt)
    finally:
        try:
            srv.stdin.close()
            srv.wait(timeout=10)
        except Exception:
            srv.kill()
        check("live: the server ends when its input closes, leaving the browser up", srv.returncode == 0 and chrome.poll() is None, f"server {srv.returncode} chrome {chrome.poll()}")
        errs = srv.stderr.read().decode("utf-8", "replace").strip()
        if errs and FAILS:
            print("server stderr:\n" + errs[-1500:])
        chrome.kill()

print()
print("ALL PASSED" if not FAILS else f"{len(FAILS)} FAILED: {FAILS}", f"({TMP})")
sys.exit(1 if FAILS else 0)

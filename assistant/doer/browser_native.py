"""browser_native.py - the engine's own browser driver: the same eighteen tools as browser_mcp.py, with no Playwright
underneath (2026-11-15). The owner: "can we replace playwright officially", then "I want ours undoubtably better for
our uses". It talks to the browser itself (cdp.py: the DevTools protocol, standard library only).

    python assistant/doer/browser_mcp.py --engine native --cdp-endpoint http://127.0.0.1:<port> --output-dir <folder>

What it does differently, on purpose, for an agent that shares one browser with other agents and with a person:

  * NOTHING CAN HANG. Every word to the browser has a time limit, so the worst a frozen page, a script that never ends
    or a dialog can do is cost that limit; a script that overruns is stopped in the page.
  * AGENTS DO NOT HEAR EACH OTHER. This process listens only to the tabs it works in: another agent's dialog, console
    or navigation never reaches it.
  * THE PAGE IS READ FROM THE BROWSER'S OWN ACCESSIBILITY TREE (what a screen reader gets), frames and shadow roots
    included, in one call. An element's reference is the browser's own id of that element, so it stays the same from
    one snapshot to the next for as long as the element lives, and a reference to an element that is gone fails at
    once instead of after a five-second wait.
  * A CLICK IS CHECKED BEFORE IT IS SENT: the element is on the page, shown, enabled, has stopped moving, and is what
    is really under the pointer at that spot. When something covers it the error says what.
  * IN THE BACKGROUND IT STAYS THERE. Every page is told it has the focus, so sites that wait for focus work in a
    window nobody sees; a pop-up opened from the background is put in the background.

Snapshot lines look like the ones the agent already knows:  - button "Reserve" [ref=e41]   - textbox "Name": Ada
"""
from __future__ import annotations

import base64
import json
import re
import threading
import time
import urllib.request
from pathlib import Path

import cdp
from browser_mcp import (ACTION_MS, COLOR, CURSOR_JS, EVAL_ASYNC_MS, GLIDE_MAX_MS, GLIDE_MIN_MS, LEVELS, NAV_MS, REF, ToolError,
                         _text, clip, find_in_snapshot, log)

STEP_S = 15.0                      # one ordinary word to the browser
SCRIPT_S = 15.0                    # a script of the agent's own
PASS_THROUGH = {"none", "RootWebArea", "MenuListPopup", "presentation", "Ignored"}
DROP = {"InlineTextBox", "LineBreak", "ListMarker"}
VALUED = {"textbox", "searchbox", "spinbutton", "slider", "combobox"}
MODS = {"alt": 1, "control": 2, "ctrl": 2, "controlormeta": 2, "meta": 4, "cmd": 4, "shift": 8}
KEYS = {  # name -> (key, code, windows virtual key, text)
    "enter": ("Enter", "Enter", 13, "\r"), "tab": ("Tab", "Tab", 9, ""), "escape": ("Escape", "Escape", 27, ""), "esc": ("Escape", "Escape", 27, ""),
    "backspace": ("Backspace", "Backspace", 8, ""), "delete": ("Delete", "Delete", 46, ""), "space": (" ", "Space", 32, " "),
    "arrowup": ("ArrowUp", "ArrowUp", 38, ""), "arrowdown": ("ArrowDown", "ArrowDown", 40, ""), "arrowleft": ("ArrowLeft", "ArrowLeft", 37, ""),
    "arrowright": ("ArrowRight", "ArrowRight", 39, ""), "home": ("Home", "Home", 36, ""), "end": ("End", "End", 35, ""),
    "pageup": ("PageUp", "PageUp", 33, ""), "pagedown": ("PageDown", "PageDown", 34, ""), "insert": ("Insert", "Insert", 45, ""),
}
KEYS.update({f"f{i}": (f"F{i}", f"F{i}", 111 + i, "") for i in range(1, 13)})

# what the page itself is asked about an element before it is touched
STATE_JS = """function() {
  if (!this.isConnected) return {gone: true};
  const el = this.nodeType === 1 ? this : this.parentElement;
  if (!el) return {gone: true};
  const st = getComputedStyle(el);
  const shown = el.getClientRects().length > 0 && st.visibility !== 'hidden' && st.display !== 'none';
  let off = false;
  for (let n = el; n; n = n.parentElement) {
    if (n.disabled === true && /^(BUTTON|INPUT|SELECT|TEXTAREA|FIELDSET|OPTION|OPTGROUP)$/.test(n.tagName)) { off = true; break; }
    if (n.getAttribute && n.getAttribute('aria-disabled') === 'true') { off = true; break; }
  }
  return {shown: shown, enabled: !off, tag: el.tagName, type: (el.type || ''), editable: !!el.isContentEditable};
}"""
# Everything a careful click needs to know, asked of the page in ONE call (2026-11-16: asked piece by piece it was nine
# words to the browser per look, and with eight agents at once that was most of a click's time): is the element on
# the page, shown, enabled, where is it (scrolled into view first when it is not), and is it what is really under the
# pointer there (shadow roots looked into). An element inside a frame answers {frame: true}: its place has to be
# measured from outside (the slower road, _ready_cdp).
READY_JS = """function(hit) {
  if (!this.isConnected) return {gone: true};
  const el = this.nodeType === 1 ? this : this.parentElement;
  if (!el) return {gone: true};
  const st = getComputedStyle(el);
  const shown = el.getClientRects().length > 0 && st.visibility !== 'hidden' && st.display !== 'none';
  let off = false;
  for (let n = el; n; n = n.parentElement) {
    if (n.disabled === true && /^(BUTTON|INPUT|SELECT|TEXTAREA|FIELDSET|OPTION|OPTGROUP)$/.test(n.tagName)) { off = true; break; }
    if (n.getAttribute && n.getAttribute('aria-disabled') === 'true') { off = true; break; }
  }
  if (window.top !== window) return {frame: true, shown: shown, enabled: !off};
  const first = () => { for (const r of el.getClientRects()) if (r.width >= 1 && r.height >= 1) return r; return null; };
  let r = first();
  if (!shown || !r) return {shown: false, enabled: !off, vw: innerWidth, vh: innerHeight};
  if (r.top < 0 || r.left < 0 || r.bottom > innerHeight || r.right > innerWidth) {
    el.scrollIntoView({block: 'center', inline: 'center', behavior: 'instant'});
    r = first() || r;
  }
  const x = Math.min(Math.max(r.left + r.width / 2, 0), innerWidth - 1), y = Math.min(Math.max(r.top + r.height / 2, 0), innerHeight - 1);
  let cover = '';
  if (hit) {
    let h = document.elementFromPoint(x, y);
    while (h && h.shadowRoot) { const inner = h.shadowRoot.elementFromPoint(x, y); if (!inner || inner === h) break; h = inner; }
    let ok = false;
    for (let n = h; n; n = n.parentNode || n.host) if (n === el) { ok = true; break; }
    if (!ok && h) {
      const lab = h.closest ? h.closest('label') : null;
      if (lab && lab.control === el) ok = true;
      if (el.tagName === 'LABEL' && el.control && (el.control === h || el.control.contains(h))) ok = true;
    }
    if (!ok) {
      if (!h) cover = 'nothing clickable (the spot is outside the page)';
      else {
        cover = '<' + h.tagName.toLowerCase() + (h.id ? ' id="' + h.id + '"' : '') + (typeof h.className === 'string' && h.className ? ' class="' + h.className.slice(0, 60) + '"' : '') + '>';
        const tx = (h.innerText || '').trim().replace(/\\s+/g, ' ').slice(0, 50);
        if (tx) cover += ' "' + tx + '"';
      }
    }
  }
  return {shown: true, enabled: !off, x: x, y: y, box: [r.left, r.top, r.width, r.height], cover: cover, vw: innerWidth, vh: innerHeight};
}"""
# is `hit` (what is under the pointer) the element itself, something inside it, or its label?
OWNS_JS = """function(hit) {
  for (let n = hit; n; n = n.parentNode || n.host) if (n === this) return true;
  const h = hit && hit.nodeType === 1 ? hit : (hit && hit.parentElement);
  const lab = h && h.closest ? h.closest('label') : null;
  if (lab && lab.control === this) return true;
  if (this.tagName === 'LABEL' && this.control && (this.control === hit || this.control.contains(hit))) return true;
  return false;
}"""
WHAT_JS = """function() {
  const el = this.nodeType === 1 ? this : this.parentElement;
  if (!el) return 'something';
  let s = '<' + el.tagName.toLowerCase() + (el.id ? ' id="' + el.id + '"' : '') + (el.className && el.className.baseVal === undefined && el.className ? ' class="' + String(el.className).slice(0, 60) + '"' : '') + '>';
  const t = (el.innerText || '').trim().replace(/\\s+/g, ' ').slice(0, 50);
  return t ? s + ' "' + t + '"' : s;
}"""
FOCUS_JS = """function() {
  this.scrollIntoView({block: 'center', inline: 'nearest'});
  this.focus();
  if (this.isContentEditable) { const r = document.createRange(); r.selectNodeContents(this); const s = getSelection(); s.removeAllRanges(); s.addRange(r); return 'editable'; }
  if (this.tagName === 'SELECT') return 'select';
  if (this.tagName === 'INPUT' && /^(date|time|datetime-local|month|week|color|range)$/.test(this.type)) return 'set';
  if (typeof this.select === 'function') { try { this.select(); } catch (e) {} return 'field'; }
  return 'other';
}"""
SET_JS = """function(v) {
  const proto = this.tagName === 'TEXTAREA' ? HTMLTextAreaElement.prototype : HTMLInputElement.prototype;
  const d = Object.getOwnPropertyDescriptor(proto, 'value');
  if (d && d.set) d.set.call(this, v); else this.value = v;
  this.dispatchEvent(new Event('input', {bubbles: true}));
  this.dispatchEvent(new Event('change', {bubbles: true}));
  return this.value;
}"""
SELECT_JS = """function(wanted) {
  if (this.tagName !== 'SELECT') return {error: 'that is not a drop-down list of the page (a <select>); click it and then click the option'};
  const opts = Array.from(this.options);
  const pick = [];
  for (const w of wanted) {
    const o = opts.find(o => o.value === w) || opts.find(o => o.label.trim() === w.trim() || o.text.trim() === w.trim())
      || opts.find(o => o.text.trim().toLowerCase() === w.trim().toLowerCase());
    if (!o) return {error: 'no option "' + w + '"; the options are: ' + opts.map(o => o.text.trim()).slice(0, 30).join(' | ')};
    if (o.disabled) return {error: 'the option "' + w + '" is disabled'};
    pick.push(o);
  }
  if (this.multiple) opts.forEach(o => { o.selected = pick.includes(o); }); else if (pick.length) this.value = pick[0].value;
  this.dispatchEvent(new Event('input', {bubbles: true}));
  this.dispatchEvent(new Event('change', {bubbles: true}));
  return {picked: pick.map(o => o.text.trim())};
}"""


def _gone() -> ToolError:
    return ToolError("that element is no longer on the page (the page changed since the snapshot): take a new snapshot and use the reference from it")


class Native:
    """One agent's hold on the shared browser. Same surface as browser_mcp.Browser: call(name, args), close()."""

    def __init__(self, cdp_url: str, out_dir: Path, cursor: bool = True, label: str = "", color: str = ""):
        self.cdp, self.out = cdp_url.rstrip("/"), out_dir
        self.cursor, self.label = bool(cursor), str(label or "")[:24]
        self.color = color if COLOR.match(color or "") else ""
        self.conn: cdp.Conn | None = None
        self.tab: dict | None = None
        self.tabs: dict = {}             # session id -> tab (the tabs this agent is attached to)
        self.by_tid: dict = {}           # target id -> tab
        self.at: dict = {}               # target id -> (x, y): where the cursor last was
        self.popups: list = []           # tabs our page opened, to put in the background
        self.order: list = []            # tab ids in the order this agent first saw them (a tab's number never shifts under it)
        self.oids: dict = {}             # (session, element id) -> the page's handle on it, for the length of one tool call
        self.vp = (1280, 800)            # the page's size, as the last look at an element reported it
        self.lock = threading.Lock()

    # ---- the connection -----------------------------------------------------------------------------------------
    def _http(self, path: str):
        with urllib.request.urlopen(self.cdp + path, timeout=5) as r:
            return json.loads(r.read().decode("utf-8", "replace"))

    def attach(self) -> None:
        if self.conn is None or not self.conn.alive:
            try:
                ws = self._http("/json/version")["webSocketDebuggerUrl"]
                self.conn = cdp.Conn(ws)
            except Exception as e:  # noqa: BLE001
                self.conn = None
                raise ToolError(f"the browser is not answering at {self.cdp} ({type(e).__name__}); it may have been closed") from None
            self.tabs, self.by_tid, self.tab = {}, {}, None
            self.conn.on(self._event)
            self.conn.send("Target.setDiscoverTargets", {"discover": True})
        if self.tab is None or self.tab.get("gone"):
            self.tab = None
            self._adopt(self._front())

    def _front(self) -> str:
        """The tab in front (the doer puts the job's own tab there), else the first page, else a new one."""
        pages = [t for t in self._http("/json/list") if t.get("type") == "page"]
        for t in pages[:10]:
            try:
                sid = self.conn.send("Target.attachToTarget", {"targetId": t["id"], "flatten": True})["sessionId"]
                try:
                    r = self.conn.send("Runtime.evaluate", {"expression": "document.visibilityState", "returnByValue": True}, session=sid, timeout=3)
                finally:
                    self.conn.post("Target.detachFromTarget", {"sessionId": sid})
                if (r.get("result") or {}).get("value") == "visible":
                    return t["id"]
            except cdp.CDPError:
                continue
        if pages:
            return pages[0]["id"]
        return self.conn.send("Target.createTarget", {"url": "about:blank"})["targetId"]

    def _adopt(self, tid: str) -> dict:
        tab = self.by_tid.get(tid)
        if tab is None or tab.get("gone"):
            sid = self.conn.send("Target.attachToTarget", {"targetId": tid, "flatten": True})["sessionId"]
            tab = {"tid": tid, "sid": sid, "dialog": None, "console": [], "navs": 0, "crashed": False, "gone": False, "oopif": {},
                   "frame_ix": {}, "life": set(), "loading": False, "asked": 0, "within": 0, "main": ""}
            self.tabs[sid], self.by_tid[tid] = tab, tab
            for m, p in (("Page.enable", None), ("Runtime.enable", None), ("DOM.enable", None),
                         ("Page.setLifecycleEventsEnabled", {"enabled": True}),
                         ("Emulation.setFocusEmulationEnabled", {"enabled": True}),      # the page believes it has the focus, in front or not
                         ("Target.setAutoAttach", {"autoAttach": True, "waitForDebuggerOnStart": False, "flatten": True})):
                try:
                    self.conn.send(m, p, session=sid, timeout=STEP_S)
                except cdp.CDPError as e:
                    log(f"{m}: {e}")
            try:
                tab["main"] = self.conn.send("Page.getFrameTree", session=sid, timeout=STEP_S)["frameTree"]["frame"]["id"]
            except cdp.CDPError:
                pass
        self.tab = tab
        return tab

    def _event(self, sid: str, method: str, p: dict) -> None:
        """On the reader thread: record only."""
        if method == "Target.targetCreated":
            info = p.get("targetInfo") or {}
            if info.get("type") == "page" and self.tab and info.get("openerId") == self.tab["tid"]:
                self.popups.append(info.get("targetId"))
            return
        if method == "Target.targetDestroyed":
            t = self.by_tid.get(p.get("targetId"))
            if t:
                t["gone"] = True
            return
        if method == "Target.detachedFromTarget":
            t = self.tabs.get(p.get("sessionId") or "")
            if t:
                t["gone"] = True
            else:
                for tb in self.tabs.values():
                    for fid, s in list(tb["oopif"].items()):
                        if s == p.get("sessionId"):
                            tb["oopif"].pop(fid, None)
            return
        tab = self.tabs.get(sid)
        if tab is None:
            return
        if method == "Target.attachedToTarget":
            info = p.get("targetInfo") or {}
            if info.get("type") == "iframe":
                tab["oopif"][info.get("targetId")] = p.get("sessionId")     # an out-of-process frame: its target id is its frame id
        elif method == "Page.javascriptDialogOpening":
            tab["dialog"] = p
        elif method == "Page.javascriptDialogClosed":
            tab["dialog"] = None
        elif method == "Runtime.consoleAPICalled":
            lvl = {"log": "info", "warning": "warning", "warn": "warning", "error": "error", "debug": "debug", "info": "info", "assert": "error"}.get(p.get("type"), "info")
            text = " ".join(str(a.get("value", a.get("description", ""))) for a in p.get("args") or [])
            tab["console"].append((tab["navs"], lvl, text))
            del tab["console"][:-500]
        elif method == "Runtime.exceptionThrown":
            d = p.get("exceptionDetails") or {}
            tab["console"].append((tab["navs"], "error", str((d.get("exception") or {}).get("description") or d.get("text") or "")[:500]))
        elif method == "Page.frameNavigated":
            f = p.get("frame") or {}
            if not f.get("parentId"):
                tab["navs"] += 1
                tab["crashed"] = False
                tab["main"] = f.get("id") or tab["main"]
        elif method in ("Page.frameRequestedNavigation", "Page.frameScheduledNavigation", "Page.frameStartedNavigating"):
            # the page has been told to go somewhere (a link followed, a form sent, a script's location change): the
            # action that caused it is not over until that page has arrived (_settle)
            if not tab["main"] or p.get("frameId") == tab["main"]:
                tab["asked"] += 1
        elif method == "Page.navigatedWithinDocument":
            if not tab["main"] or p.get("frameId") == tab["main"]:
                tab["within"] += 1
        elif method == "Page.frameStartedLoading":
            tab["loading"] = True
        elif method == "Page.frameStoppedLoading":
            tab["loading"] = False
        elif method == "Page.lifecycleEvent":
            tab["life"].add((p.get("loaderId"), p.get("name")))
            if len(tab["life"]) > 400:
                tab["life"] = set(list(tab["life"])[-100:])
        elif method == "Inspector.targetCrashed":
            tab["crashed"] = True

    # ---- words to the browser -------------------------------------------------------------------------------------
    def s(self, method: str, params: dict | None = None, timeout: float = STEP_S, sid: str = "", until=None):
        try:
            return self.conn.send(method, params, session=sid or self.tab["sid"], timeout=timeout, until=until)
        except cdp.Timeout:
            raise ToolError(f"the page did not answer within {timeout:g} s ({method.split('.')[-1]}); it may be frozen. Take a snapshot to see where things stand") from None
        except cdp.Closed:
            raise ToolError("the browser went away in the middle of that; it may have been closed") from None

    def ev(self, expr: str, timeout: float = 10.0, sid: str = ""):
        sid = sid or self.tab["sid"]
        tab = self.tab
        try:
            r = self.conn.send("Runtime.evaluate", {"expression": expr, "returnByValue": True, "awaitPromise": True, "userGesture": True,
                                                    "timeout": int(timeout * 1000)}, session=sid, timeout=timeout + 4,
                               until=lambda: bool(tab["dialog"]) or tab.get("gone"))
            if r is None:
                raise ToolError("a dialog opened in the page")       # a page showing a dialog answers nothing: do not wait for it
        except cdp.Timeout:
            self.conn.post("Runtime.terminateExecution", session=sid)
            raise ToolError(f"the page did not answer within {timeout:g} s, so the script running in it was stopped. Take a snapshot to see where things stand") from None
        except cdp.Closed:
            raise ToolError("the browser went away in the middle of that; it may have been closed") from None
        ex = r.get("exceptionDetails")
        if ex:
            msg = str((ex.get("exception") or {}).get("description") or ex.get("text") or "the script failed")
            if "terminated" in msg.lower():
                msg = f"the script did not finish within {timeout:g} s and was stopped"
            raise ToolError(msg[:1500])
        return (r.get("result") or {}).get("value")

    def on(self, node: tuple, fn: str, *args, timeout: float = 8.0):
        """Run `fn` with `this` = the element. node = (session, the browser's id of the element)."""
        sid, bid = node
        tab = self.tab
        waiting = lambda: bool(tab["dialog"]) or tab.get("gone")       # noqa: E731
        for attempt in (0, 1):
            oid = self.oids.get(node)
            try:
                if not oid:
                    oid = self.conn.send("DOM.resolveNode", {"backendNodeId": bid, "objectGroup": "agent"}, session=sid, timeout=STEP_S)["object"]["objectId"]
                    self.oids[node] = oid
            except cdp.Timeout:
                raise ToolError("the page did not answer; it may be frozen. Take a snapshot to see where things stand") from None
            except cdp.Closed:
                raise ToolError("the browser went away in the middle of that; it may have been closed") from None
            except cdp.CDPError:
                raise _gone() from None
            argv = [a if isinstance(a, dict) and "objectId" in a else {"value": a} for a in args]
            try:
                r = self.conn.send("Runtime.callFunctionOn", {"objectId": oid, "functionDeclaration": fn, "arguments": argv, "returnByValue": True,
                                                              "awaitPromise": True, "userGesture": True}, session=sid, timeout=timeout, until=waiting)
            except cdp.Timeout:
                self.conn.post("Runtime.terminateExecution", session=sid)
                raise ToolError(f"the page did not answer within {timeout:g} s, so the script running in it was stopped. Take a snapshot to see where things stand") from None
            except cdp.Closed:
                raise ToolError("the browser went away in the middle of that; it may have been closed") from None
            except cdp.CDPError as e:
                self.oids.pop(node, None)
                if attempt == 0:
                    continue                                 # the handle was from before the page changed: fetch it again, once
                raise ToolError(str(e)[:400]) from None
            if r is None:
                raise ToolError("a dialog opened in the page")
            ex = r.get("exceptionDetails")
            if ex:
                raise ToolError(str((ex.get("exception") or {}).get("description") or ex.get("text") or "the script failed")[:1500])
            return (r.get("result") or {}).get("value")

    # ---- finding an element -----------------------------------------------------------------------------------------
    def node(self, a: dict, key: str = "target", old: str = "ref", wait_ms: int = ACTION_MS) -> tuple:
        t = str(a.get(key) or a.get(old) or "").strip()
        if not t:
            raise ToolError(f"{key} is required: an element reference from the page snapshot (like e12) or a selector")
        m = re.match(r"^(?:f(\d+))?e(\d+)$", t)
        if m:
            if m.group(1):
                fid = next((f for f, k in self.tab["frame_ix"].items() if k == int(m.group(1))), "")
                sid = self.tab["oopif"].get(fid)
                if not sid:
                    raise _gone()
                return (sid, int(m.group(2)))
            return (self.tab["sid"], int(m.group(2)))
        end = time.monotonic() + wait_ms / 1000.0
        while True:                                          # a selector: wait for it to be there, like any action would
            try:
                root = self.s("DOM.getDocument", {"depth": 0})["root"]["nodeId"]
                nid = self.conn.send("DOM.querySelector", {"nodeId": root, "selector": t}, session=self.tab["sid"], timeout=STEP_S).get("nodeId")
                if nid:
                    bid = self.s("DOM.describeNode", {"nodeId": nid})["node"]["backendNodeId"]
                    return (self.tab["sid"], bid)
            except cdp.CDPError as e:
                if "selector" in str(e).lower() or "syntax" in str(e).lower():
                    raise ToolError(f"{t!r} is neither a reference from the snapshot nor a selector the page understands") from None
            if time.monotonic() > end:
                raise ToolError(f"Timeout {wait_ms}ms exceeded.\nCall log:\n  - waiting for the selector {t!r} to match an element")
            time.sleep(0.1)

    def _offset(self, sid: str) -> tuple:
        """Where an out-of-process frame sits in the page (its own coordinates start at its own corner)."""
        if sid == self.tab["sid"]:
            return 0.0, 0.0
        fid = next((f for f, s in self.tab["oopif"].items() if s == sid), "")
        try:
            owner = self.s("DOM.getFrameOwner", {"frameId": fid})["backendNodeId"]
            box = self.s("DOM.getBoxModel", {"backendNodeId": owner})["model"]["content"]
            return float(box[0]), float(box[1])
        except Exception:  # noqa: BLE001
            return 0.0, 0.0

    def quad(self, node: tuple):
        """The element's place on the page: (centre x, centre y, [x, y, w, h]) in the page's own pixels, or None."""
        sid, bid = node
        try:
            quads = self.conn.send("DOM.getContentQuads", {"backendNodeId": bid}, session=sid, timeout=STEP_S).get("quads") or []
        except cdp.Timeout:
            raise ToolError("the page did not answer; it may be frozen. Take a snapshot to see where things stand") from None
        except cdp.CDPError:
            return None
        for q in quads:
            xs, ys = q[0::2], q[1::2]
            w, h = max(xs) - min(xs), max(ys) - min(ys)
            if w >= 1 and h >= 1:
                ox, oy = self._offset(sid)
                return (sum(xs) / 4 + ox, sum(ys) / 4 + oy, [min(xs) + ox, min(ys) + oy, w, h])
        return None

    def ready(self, node: tuple, what: str = "click", hit: bool = True, timeout_ms: int = ACTION_MS) -> tuple:
        """Wait until the element can be acted on, the way a careful person would: it is on the page, shown, enabled,
        has stopped moving, and (for a click) is what is really under the pointer. -> (x, y, box). Two looks 45 ms
        apart, each ONE call to the page (READY_JS); "stopped moving" is by the clock, not by animation frames, which
        a tab that is not in front never gets (the Playwright-backed server timed out on exactly that with eight
        agents in one window, 2026-11-16)."""
        end = time.monotonic() + timeout_ms / 1000.0
        why, prev = "", None
        while True:
            st = self.on(node, READY_JS, bool(hit)) or {}
            if st.get("gone"):
                raise _gone()
            if st.get("frame"):
                return self._ready_cdp(node, what, hit, max(500, int((end - time.monotonic()) * 1000)))
            if st.get("vw"):
                self.vp = (st["vw"], st["vh"])
            if not st.get("shown"):
                why, prev = "the element is not visible", None
            elif not st.get("enabled"):
                why, prev = "the element is not enabled", None
            elif prev is None or max(abs(a - b) for a, b in zip(prev, st["box"])) > 0.5:
                why = "the element is not stable (it is still moving)" if prev is not None else why
                prev = st["box"]
                if time.monotonic() <= end:
                    time.sleep(0.045)
                    continue
            elif st.get("cover"):
                why = f"{st['cover']} is in the way: it would take the {what} instead"
                prev = None
            else:
                return (st["x"], st["y"], st["box"])
            if time.monotonic() > end:
                raise ToolError(f"Timeout {timeout_ms}ms exceeded.\nCall log:\n  - waiting for element to be visible, enabled and stable\n  - {why}")
            time.sleep(0.1)

    def _ready_cdp(self, node: tuple, what: str = "click", hit: bool = True, timeout_ms: int = ACTION_MS) -> tuple:
        """The same wait for an element inside a frame, measured from outside the frame."""
        sid, bid = node
        end = time.monotonic() + timeout_ms / 1000.0
        why, scrolled = "", False
        while True:
            st = self.on(node, STATE_JS) or {}
            if st.get("gone"):
                raise _gone()
            if not st.get("shown"):
                why = "the element is not visible"
            elif not st.get("enabled"):
                why = "the element is not enabled"
            else:
                if not scrolled:
                    try:
                        self.conn.send("DOM.scrollIntoViewIfNeeded", {"backendNodeId": bid}, session=sid, timeout=STEP_S)
                    except cdp.CDPError:
                        pass
                    scrolled = True
                q1 = self.quad(node)
                time.sleep(0.045)
                q2 = self.quad(node)
                if not q1 or not q2:
                    why = "the element is not visible"
                elif max(abs(a - b) for a, b in zip(q1[2], q2[2])) > 0.5:
                    why = "the element is not stable (it is still moving)"
                    scrolled = False
                elif not hit:
                    return q2
                else:
                    cover = self._covered(node, q2)
                    if not cover:
                        return q2
                    why = f"{cover} is in the way: it would take the {what} instead"
                    scrolled = False
            if time.monotonic() > end:
                raise ToolError(f"Timeout {timeout_ms}ms exceeded.\nCall log:\n  - waiting for element to be visible, enabled and stable\n  - {why}")
            time.sleep(0.1)

    def _where(self, node: tuple):
        """The element's box right now ([x, y, w, h]) or None: the cheap last look before a press."""
        st = self.on(node, READY_JS, False) or {}
        if st.get("gone"):
            raise _gone()
        if st.get("frame"):
            q = self.quad(node)
            return q[2] if q else None
        return st.get("box") if st.get("shown") and st.get("enabled") else None

    def _covered(self, node: tuple, q) -> str:
        """'' when the element (or something inside it, or its label) is what sits under the pointer at its centre."""
        sid, _bid = node
        if sid != self.tab["sid"]:
            return ""                                        # inside an out-of-process frame: the page cannot see into it
        try:
            # ignorePointerEventsNone False = hit-test like a real click does: what lets clicks through (the agent's own
            # cursor drawing, a see-through overlay) is not "in the way"
            got = self.conn.send("DOM.getNodeForLocation", {"x": int(q[0]), "y": int(q[1]), "includeUserAgentShadowDOM": False,
                                                            "ignorePointerEventsNone": False}, session=sid, timeout=STEP_S)
            hid = got.get("backendNodeId")
            if not hid:
                return ""
            oid = self.conn.send("DOM.resolveNode", {"backendNodeId": hid, "objectGroup": "agent"}, session=sid, timeout=STEP_S)["object"]["objectId"]
            if self.on(node, OWNS_JS, {"objectId": oid}):
                return ""
            return str(self.on((sid, hid), WHAT_JS) or "something")
        except ToolError:
            raise
        except cdp.CDPError:
            return ""

    # ---- the agent's own cursor ---------------------------------------------------------------------------------------
    def glide(self, q) -> int:
        """Move the agent's cursor to a spot; -> the milliseconds it took (0 when nobody can see it: it jumped)."""
        if not self.cursor or not q:
            return 0
        fx, fy = self.at.get(self.tab["tid"]) or (self.vp[0] * 0.5, self.vp[1] * 0.42)
        dist = ((q[0] - fx) ** 2 + (q[1] - fy) ** 2) ** 0.5
        ms = int(min(GLIDE_MAX_MS, max(GLIDE_MIN_MS, 160 + dist * 0.5))) if dist > 3 else 0
        took = self._cursor({"x": q[0], "y": q[1], "fx": fx, "fy": fy, "ms": ms})
        self.at[self.tab["tid"]] = (q[0], q[1])
        if took > 0:
            time.sleep(took / 1000.0 + 0.04)
        return took

    def _cursor(self, args: dict) -> int:
        try:
            return int(self.ev("(" + CURSOR_JS + ")(" + json.dumps({"label": self.label, "color": self.color, **args}) + ")", timeout=3) or 0)
        except ToolError:
            return 0

    # ---- input -----------------------------------------------------------------------------------------------------------
    def _mouse(self, kind: str, x: float, y: float, button: str = "none", count: int = 0, mods: int = 0, buttons: int = 0):
        return self.s("Input.dispatchMouseEvent", {"type": kind, "x": x, "y": y, "button": button, "clickCount": count, "modifiers": mods, "buttons": buttons},
                      timeout=10, until=lambda: bool(self.tab["dialog"]))

    def _key(self, combo: str) -> None:
        parts = [p for p in re.split(r"\+(?=.)", combo) if p] or [combo]
        mods = 0
        for p in parts[:-1]:
            if p.lower() not in MODS:
                raise ToolError(f"unknown modifier {p!r} in {combo!r}")
            mods |= MODS[p.lower()]
        name = parts[-1]
        if name.lower() in KEYS:
            key, code, vk, text = KEYS[name.lower()]
        elif len(name) == 1:
            key, text = name, name
            vk = ord(name.upper()) if name.isalnum() else 0
            code = ("Key" + name.upper()) if name.isalpha() else (("Digit" + name) if name.isdigit() else "")
        else:
            raise ToolError(f"unknown key {name!r}: a single character, or a name like Enter, Tab, Escape, ArrowDown, F5")
        if mods & ~8:
            text = ""                                        # Control+A is a command, not the letter a
        base = {"key": key, "code": code, "windowsVirtualKeyCode": vk, "nativeVirtualKeyCode": vk, "modifiers": mods}
        down = dict(base, type="keyDown" if text else "rawKeyDown")
        if text:
            down.update(text=text, unmodifiedText=text)
        self.s("Input.dispatchKeyEvent", down, timeout=10, until=lambda: bool(self.tab["dialog"]))
        self.s("Input.dispatchKeyEvent", dict(base, type="keyUp"), timeout=10, until=lambda: bool(self.tab["dialog"]))

    def _mark(self) -> tuple:
        """Where the tab stands before an action (for _settle)."""
        return (self.tab["navs"], self.tab["asked"], self.tab["within"])

    def _settle(self, mark) -> None:
        """An action is over when what it set in motion is over. A click on a link answers before the browser has
        even started for the next page; reporting then shows the agent the OLD page as the result (stress run,
        2026-11-15: with 4 and 8 agents at once ours did exactly that). So: one more word to the page (whatever the
        action set in motion is announced before that word is answered), and if the page was told to go somewhere,
        wait for it to get there and be readable. A page that goes nowhere costs one short word."""
        navs, asked, within = mark if isinstance(mark, tuple) else (mark, self.tab["asked"], self.tab["within"])
        tab, sid = self.tab, self.tab["sid"]
        busy = lambda: bool(tab["dialog"]) or tab.get("gone")      # noqa: E731
        try:
            self.conn.send("Runtime.evaluate", {"expression": "1", "returnByValue": True}, session=sid, timeout=3, until=busy)
        except cdp.CDPError:
            pass
        if tab["asked"] == asked and tab["navs"] == navs and not busy():
            time.sleep(0.012)                                    # the announcement and the answer travel separately: a breath
        if busy() or (tab["asked"] == asked and tab["navs"] == navs):
            return
        end = time.monotonic() + 6
        while tab["navs"] == navs and tab["within"] == within and time.monotonic() < end and not busy():
            time.sleep(0.015)                                    # asked to go, not there yet (or it became a download: 6 s at most)
        end = time.monotonic() + 12
        while time.monotonic() < end and not busy():
            try:
                r = self.conn.send("Runtime.evaluate", {"expression": "document.readyState", "returnByValue": True}, session=sid, timeout=2, until=busy)
                if r and (r.get("result") or {}).get("value") in ("interactive", "complete"):
                    return
            except cdp.CDPError:
                pass
            time.sleep(0.05)

    def _fill(self, node: tuple, text: str, slowly: bool = False) -> None:
        q = self.ready(node, "typing", hit=False)
        self.glide(q)
        kind = self.on(node, FOCUS_JS)
        if kind == "select":
            raise ToolError("that is a drop-down list; use browser_select_option")
        if kind == "set":
            self.on(node, SET_JS, text)
            return
        if kind == "other":
            raise ToolError("that element does not take typing (it is not a field)")
        sid = node[0]
        if not text:
            self._key("Delete")
        elif slowly:
            for ch in text:
                self.s("Input.dispatchKeyEvent", {"type": "keyDown", "text": ch, "unmodifiedText": ch, "key": ch}, sid=sid, timeout=10)
                self.s("Input.dispatchKeyEvent", {"type": "keyUp", "key": ch}, sid=sid, timeout=10)
        else:
            self.s("Input.insertText", {"text": text}, sid=sid, timeout=max(STEP_S, len(text) / 20000.0))

    # ---- reading the page ---------------------------------------------------------------------------------------------------
    def snapshot(self, a: dict | None = None) -> str:
        a = a or {}
        try:
            nodes = self.conn.send("Accessibility.getFullAXTree", {}, session=self.tab["sid"], timeout=30).get("nodes") or []
        except cdp.Timeout:
            raise ToolError("the page did not answer within 30 s when asked what is on it; it may be frozen") from None
        except cdp.Closed:
            raise ToolError("the browser went away in the middle of that; it may have been closed") from None
        only = self.node(a)[1] if (a.get("target") or a.get("ref")) else None
        lines = self._render(nodes, "", self.tab["sid"], int(a["depth"]) if a.get("depth") else 0, only)
        return "\n".join(lines) if lines else "(the page is empty)"

    def _render(self, nodes: list, pfx: str, sid: str, max_depth: int = 0, only: int | None = None, frames_left: list | None = None) -> list:
        by = {n["nodeId"]: n for n in nodes}
        if not nodes:
            return []
        frames_left = frames_left if frames_left is not None else [24]
        root = nodes[0]
        if only is not None:
            root = next((n for n in nodes if n.get("backendDOMNodeId") == only), None)
            if root is None:
                raise _gone()

        def props(n):
            return {p["name"]: (p.get("value") or {}).get("value") for p in n.get("properties") or []}

        def walk(n, depth) -> list:
            role = str((n.get("role") or {}).get("value") or "")
            if role in DROP:
                return []
            name = " ".join(str((n.get("name") or {}).get("value") or "").split())
            kids = [by[c] for c in n.get("childIds") or [] if c in by]
            if role == "StaticText":
                return [("t", name)] if name else []
            pr = props(n) if not n.get("ignored") else {}
            plain = role == "generic" and not name and not pr.get("focusable")
            if plain:
                plain = not any(str((k.get("role") or {}).get("value")) == "StaticText" for k in kids)
            if n.get("ignored") or role in PASS_THROUGH or plain or not role:
                out = []
                for k in kids:
                    out += walk(k, depth)
                return out
            if max_depth and depth >= max_depth:
                return []
            sub = []
            for k in kids:
                sub += walk(k, depth + 1)
            shown = {"Iframe": "iframe", "image": "img"}.get(role, role)
            head = "- " + shown + (f' "{name}"' if name else "")
            if shown == "heading" and pr.get("level"):
                head += f" [level={pr['level']}]"
            ck = pr.get("checked")
            if ck in ("true", True):
                head += " [checked]"
            elif ck == "mixed":
                head += " [checked=mixed]"
            for flag in ("selected", "expanded", "disabled", "pressed"):
                if pr.get(flag) in (True, "true"):
                    head += f" [{flag}]"
            if pr.get("focused") in (True, "true") and shown != "iframe":
                head += " [active]"
            bid = n.get("backendDOMNodeId")
            if bid:
                head += f" [ref={pfx}e{bid}]"
            extra = []
            if shown == "link" and pr.get("url"):
                extra.append(("l", "  - /url: " + str(pr["url"])[:300]))
            if role == "Iframe" and bid and frames_left[0] > 0:
                frames_left[0] -= 1
                extra += [("l", "  " + ln) for ln in self._frame_lines(sid, bid, pfx, frames_left)]
            texts = [x[1] for x in sub if x[0] == "t"]
            lines = [x for x in sub if x[0] == "l"]
            joined = " ".join(texts)
            val = str((n.get("value") or {}).get("value") or "") if shown in VALUED else ""
            if not lines and not extra:
                tail = ""
                if val and shown != "combobox":
                    tail = ": " + val
                elif joined and joined != name and joined not in name:
                    tail = ": " + joined
                return [("l", head + tail)]
            body = []
            if joined and joined != name and joined not in name and lines:
                body.append(("l", "  - text: " + joined))
            return [("l", head + ":")] + extra + body + [("l", "  " + x[1]) for x in lines]

        out = walk(root, 0)
        return [("- text: " + x[1]) if x[0] == "t" else x[1] for x in out]

    def _frame_lines(self, sid: str, iframe_bid: int, pfx: str, frames_left: list) -> list:
        try:
            fid = self.conn.send("DOM.describeNode", {"backendNodeId": iframe_bid}, session=sid, timeout=STEP_S)["node"].get("frameId")
            if not fid:
                return []
            child = self.tab["oopif"].get(fid)
            if child:
                k = self.tab["frame_ix"].setdefault(fid, len(self.tab["frame_ix"]) + 1)
                nodes = self.conn.send("Accessibility.getFullAXTree", {}, session=child, timeout=20).get("nodes") or []
                return self._render(nodes, f"f{k}", child, 0, None, frames_left)
            nodes = self.conn.send("Accessibility.getFullAXTree", {"frameId": fid}, session=sid, timeout=20).get("nodes") or []
            return self._render(nodes, pfx, sid, 0, None, frames_left)
        except cdp.CDPError:
            return []

    def _title_url(self) -> tuple:
        try:
            v = self.ev("[document.title, location.href]", timeout=5) or ["", ""]
            return str(v[0]), str(v[1])
        except ToolError:
            return "", ""

    def state(self, with_snapshot: bool = True) -> str:
        if self.tab["dialog"]:
            return self._dialog_note()
        title, url = self._title_url()
        out = [f"### Page\n- Page URL: {url}\n- Page Title: {title}"]
        if with_snapshot:
            try:
                out.append("### Snapshot\n```yaml\n" + clip(self.snapshot()) + "\n```")
            except ToolError as e:
                out.append(f"### Snapshot\n(not available: {e})")
        return "\n".join(out)

    def _dialog_note(self) -> str:
        d = self.tab["dialog"] or {}
        return (f'### Modal state\n- a "{d.get("type", "alert")}" dialog is open with the message "{d.get("message", "")}": '
                "answer it with browser_handle_dialog before anything else")

    def _save(self, a: dict, text: str, ext: str = "md"):
        fn = str(a.get("filename") or "").strip()
        if not fn:
            return None
        self.out.mkdir(parents=True, exist_ok=True)
        dst = self.out / (Path(fn).name or f"result.{ext}")
        dst.write_text(text, encoding="utf-8")
        return _text(f"saved: {dst.as_posix()} ({len(text)} characters)")

    def _tuck(self) -> None:
        """A window our page opened while our own window is in the background goes to the background too."""
        while self.popups:
            tid = self.popups.pop(0)
            try:
                mine = self.conn.send("Browser.getWindowForTarget", {"targetId": self.tab["tid"]}, timeout=5)
                if int((mine.get("bounds") or {}).get("left", 0)) > -10000:
                    continue
                pop = self.conn.send("Browser.getWindowForTarget", {"targetId": tid}, timeout=5)
                if pop["windowId"] != mine["windowId"] and int((pop.get("bounds") or {}).get("left", 0)) > -10000:
                    self.conn.send("Browser.setWindowBounds", {"windowId": pop["windowId"], "bounds": {"windowState": "normal"}}, timeout=5)
                    self.conn.send("Browser.setWindowBounds", {"windowId": pop["windowId"], "bounds": {"left": -32000, "top": -32000}}, timeout=5)
                    log("a pop-up window was put in the background with the rest")
            except cdp.CDPError as e:
                log(f"tuck: {e}")

    # ---- the tools --------------------------------------------------------------------------------------------------------------
    def call(self, name: str, a: dict) -> list:
        fn = getattr(self, "t_" + name[len("browser_"):], None) if name.startswith("browser_") else None
        if fn is None:
            raise ToolError(f"no such tool: {name}")
        if not isinstance(a, dict):
            raise ToolError("the arguments must be an object")
        self.attach()
        if self.tab["dialog"] and name != "browser_handle_dialog":
            return _text(self._dialog_note())
        if self.tab["crashed"] and name not in ("browser_navigate", "browser_tabs"):
            raise ToolError("the page crashed. Go to its address again with browser_navigate (or open a new tab) to carry on")
        try:
            return fn(a)
        except ToolError:
            if self.tab and self.tab["dialog"] and name != "browser_handle_dialog":
                return _text(self._dialog_note())      # the action opened a dialog and stalled behind it
            if self.tab and self.tab.get("gone"):
                raise ToolError("the tab was closed in the middle of that. The next call carries on in a tab that is still open") from None
            raise
        except (KeyError, TypeError, ValueError) as e:
            raise ToolError(f"bad arguments for {name}: {type(e).__name__}: {e}") from None
        except cdp.CDPError as e:
            raise ToolError(str(e)[:600]) from None
        finally:
            try:
                self.oids = {}
                if self.conn and self.conn.alive and self.tab and not self.tab.get("gone"):
                    self.conn.post("Runtime.releaseObjectGroup", {"objectGroup": "agent"}, session=self.tab["sid"])
                    if self.popups:
                        self._tuck()
            except Exception:  # noqa: BLE001
                pass

    def t_navigate(self, a):
        url = str(a.get("url") or "").strip()
        if not url:
            raise ToolError("url is required")
        if re.match(r"^\s*(javascript|vbscript):", url, re.I):
            raise ToolError("that is a script, not an address; use browser_evaluate to run a script in the page")
        if not re.match(r"^[a-z][a-z0-9+.-]*:", url, re.I):
            url = "https://" + url
        r = self.s("Page.navigate", {"url": url}, timeout=NAV_MS / 1000.0)
        if r.get("errorText") and "ERR_ABORTED" not in r["errorText"]:
            raise ToolError(f"the page did not load: {r['errorText']} ({url})")
        lid, end = r.get("loaderId"), time.monotonic() + NAV_MS / 1000.0
        while lid and time.monotonic() < end and not self.tab["dialog"] and not self.tab.get("gone"):
            if (lid, "DOMContentLoaded") in self.tab["life"] or (lid, "load") in self.tab["life"]:
                break
            time.sleep(0.03)
        self.tab["crashed"] = False
        return _text(self.state())

    def t_navigate_back(self, a):
        h = self.s("Page.getNavigationHistory")
        i = int(h.get("currentIndex", 0))
        if i <= 0:
            raise ToolError("there is no earlier page to go back to in this tab")
        mark = self._mark()
        self.s("Page.navigateToHistoryEntry", {"entryId": h["entries"][i - 1]["id"]})
        end = time.monotonic() + 15
        while self.tab["navs"] == mark[0] and self.tab["within"] == mark[2] and time.monotonic() < end:
            time.sleep(0.02)
        self._settle((mark[0], mark[1] - 1, mark[2]))            # it did go somewhere: wait until that page is readable
        return _text(self.state())

    def t_snapshot(self, a):
        snap = self.snapshot(a)
        title, url = self._title_url()
        return self._save(a, snap, "yml") or _text(f"### Page\n- Page URL: {url}\n- Page Title: {title}\n### Snapshot\n```yaml\n" + clip(snap) + "\n```")

    def t_click(self, a):
        node = self.node(a)
        q = self.ready(node, "click")
        glided = self.glide(q)
        mods = 0
        for m in a.get("modifiers") or []:
            mods |= MODS.get(str(m).lower(), 0)
        button = a.get("button") or "left"
        mask = {"left": 1, "right": 2, "middle": 4}.get(button, 1)
        end = time.monotonic() + ACTION_MS / 1000.0
        took = glided
        while True:
            if took:
                q = self.ready(node, "click")                  # the glide took a moment (or the element moved): look again
            self._mouse("mouseMoved", q[0], q[1], mods=mods)
            now = self._where(node)                            # the last look, with the pointer in place
            if now and max(abs(x - y) for x, y in zip(now, q[2])) <= 0.5:
                break
            took = 1
            if time.monotonic() > end:
                raise ToolError(f"Timeout {ACTION_MS}ms exceeded.\nCall log:\n  - waiting for element to be visible, enabled and stable\n"
                                "  - the element is not stable (it keeps moving)")
        mark = self._mark()
        base = {"x": q[0], "y": q[1], "button": button, "modifiers": mods}
        for n in ((1, 2) if a.get("doubleClick") else (1,)):
            # press and release go out together, with nothing of ours in between: an element that starts to move
            # between the two would get the press and not the release, and no click would happen
            self.conn.post("Input.dispatchMouseEvent", dict(base, type="mousePressed", clickCount=n, buttons=mask), session=self.tab["sid"])
            self._mouse("mouseReleased", q[0], q[1], button, n, mods, 0)
        if self.cursor and not self.tab["dialog"]:             # the ring: sent, not waited for
            self.conn.post("Runtime.evaluate", {"expression": "(" + CURSOR_JS + ")(" + json.dumps({"pulse": True, "color": self.color, "label": self.label}) + ")"},
                           session=self.tab["sid"])
        self._settle(mark)
        return _text(self.state())

    def t_type(self, a):
        node = self.node(a)
        self._fill(node, str(a.get("text") or ""), bool(a.get("slowly")))
        if a.get("submit"):
            mark = self._mark()
            self._key("Enter")
            self._settle(mark)
        return _text(self.state(with_snapshot=bool(a.get("submit"))))

    def t_fill_form(self, a):
        fields = a.get("fields")
        if not isinstance(fields, list) or not fields:
            raise ToolError("fields is required: a list of {name, type, target, value}")
        for f in fields:
            node, kind, val = self.node(f), str(f.get("type") or "textbox"), str(f.get("value") or "")
            if kind in ("checkbox", "radio"):
                want = val.strip().lower() == "true"
                if bool(self.on(node, "function() { return !!this.checked || this.getAttribute('aria-checked') === 'true'; }")) != want:
                    self.t_click({"target": f.get("target") or f.get("ref")})
            elif kind == "combobox":
                self.glide(self.ready(node, "choice", hit=False))
                r = self.on(node, SELECT_JS, [val]) or {}
                if r.get("error"):
                    raise ToolError(r["error"])
            elif kind == "slider":
                self.glide(self.ready(node, "change", hit=False))
                self.on(node, SET_JS, val)
            else:
                self._fill(node, val)
        return _text(f"filled {len(fields)} field(s)\n" + self.state(with_snapshot=False))

    def t_select_option(self, a):
        node = self.node(a)
        self.glide(self.ready(node, "choice", hit=False))
        mark = self._mark()
        r = self.on(node, SELECT_JS, [str(v) for v in (a.get("values") or [])]) or {}
        if r.get("error"):
            raise ToolError(r["error"])
        self._settle(mark)
        return _text(self.state())

    def t_press_key(self, a):
        key = str(a.get("key") or "")
        if not key:
            raise ToolError("key is required")
        mark = self._mark()
        self._key(key)
        self._settle(mark)
        return _text(self.state())

    def t_hover(self, a):
        node = self.node(a)
        q = self.ready(node, "pointer", hit=False)
        self.glide(q)
        self._mouse("mouseMoved", q[0], q[1])
        time.sleep(0.05)
        return _text(self.state())

    def t_wait_for(self, a):
        secs = min(float(a.get("time") or 0), 30.0)
        text, gone = a.get("text"), a.get("textGone")
        if not (text or gone):
            if secs <= 0:
                raise ToolError("give time, text or textGone")
            time.sleep(secs)
            return _text(f"waited {secs:g} s\n" + self.state())
        end = time.monotonic() + (secs if secs > 0 else ACTION_MS / 1000.0)
        probe = "(t) => !!document.body && document.body.innerText.includes(t)"
        while True:
            has_gone = gone is not None and not self.ev(f"({probe})({json.dumps(str(gone))})", timeout=5)
            has_text = text is not None and self.ev(f"({probe})({json.dumps(str(text))})", timeout=5)
            if (gone is None or has_gone) and (text is None or has_text):
                return _text(f"waited for {text or gone}\n" + self.state())
            if time.monotonic() > end:
                raise ToolError(f"Timeout exceeded: the text {str(text or gone)!r} did not {'appear' if text else 'go away'} in time")
            time.sleep(0.15)

    def t_take_screenshot(self, a):
        kind = str(a.get("type") or "").lower()
        fn = str(a.get("filename") or "").strip()
        if not kind:
            kind = "jpeg" if fn.lower().endswith((".jpg", ".jpeg")) else "png"
        if kind not in ("png", "jpeg"):
            raise ToolError("type must be png or jpeg")
        name = Path(fn).name if fn else f"page-{time.strftime('%Y%m%dT%H%M%S')}.{'jpg' if kind == 'jpeg' else 'png'}"
        self.out.mkdir(parents=True, exist_ok=True)
        dst = self.out / name
        dpr = float(self.ev("devicePixelRatio", timeout=5) or 1) or 1.0
        scale = 1.0 if a.get("scale") == "device" else 1.0 / dpr
        params = {"format": kind}
        if kind == "jpeg":
            params["quality"] = 85
        m = self.s("Page.getLayoutMetrics")
        if a.get("target") or a.get("ref"):
            q = self.ready(self.node(a), "picture", hit=False)
            vv = m.get("cssVisualViewport") or {}
            params["clip"] = {"x": q[2][0] + float(vv.get("pageX", 0)), "y": q[2][1] + float(vv.get("pageY", 0)), "width": q[2][2], "height": q[2][3], "scale": scale}
            params["captureBeyondViewport"] = True
        elif a.get("fullPage"):
            cs = m.get("cssContentSize") or {}
            params["clip"] = {"x": 0, "y": 0, "width": float(cs.get("width", 1280)), "height": min(float(cs.get("height", 800)), 16000), "scale": scale}
            params["captureBeyondViewport"] = True
        elif scale != 1.0:
            vv = m.get("cssVisualViewport") or {}
            params["clip"] = {"x": float(vv.get("pageX", 0)), "y": float(vv.get("pageY", 0)), "width": float(vv.get("clientWidth", 1280)),
                              "height": float(vv.get("clientHeight", 800)), "scale": scale}
        data = self.s("Page.captureScreenshot", params, timeout=25).get("data") or ""
        if not data:
            raise ToolError("the browser returned no picture")
        dst.write_bytes(base64.b64decode(data))
        return _text(f"screenshot saved: {dst.as_posix()}")

    def _pages(self) -> list:
        """The browser's tabs, oldest first, each keeping the number it first had (the browser lists newest first)."""
        pages = [t for t in self._http("/json/list") if t.get("type") == "page"]
        live = {t["id"] for t in pages}
        self.order = [i for i in self.order if i in live]
        for t in reversed(pages):
            if t["id"] not in self.order:
                self.order.append(t["id"])
        return sorted(pages, key=lambda t: self.order.index(t["id"]))

    def t_tabs(self, a):
        act = str(a.get("action") or "list")
        pages = self._pages()
        idx = a.get("index")
        if act == "new":
            url = str(a.get("url") or "about:blank")
            if re.match(r"^\s*(javascript|vbscript):", url, re.I):
                raise ToolError("that is a script, not an address")
            tid = self.conn.send("Target.createTarget", {"url": "about:blank"}, timeout=STEP_S)["targetId"]
            self._adopt(tid)
            if url != "about:blank":
                self.t_navigate({"url": url})
        elif act == "select":
            if idx is None or not 0 <= int(idx) < len(pages):
                raise ToolError(f"no tab {idx}; there are {len(pages)}")
            tid = pages[int(idx)]["id"]
            self.conn.send("Target.activateTarget", {"targetId": tid}, timeout=STEP_S)
            self._adopt(tid)
        elif act == "close":
            tid = self.tab["tid"] if idx is None else (pages[int(idx)]["id"] if 0 <= int(idx) < len(pages) else None)
            if tid is None:
                raise ToolError(f"no tab {idx}; there are {len(pages)}")
            mine = tid == self.tab["tid"]
            self.conn.send("Target.closeTarget", {"targetId": tid}, timeout=STEP_S)
            if mine:
                self.tab["gone"] = True
                time.sleep(0.15)
                self.tab = None
                self._adopt(self._front())
        elif act != "list":
            raise ToolError("action must be list, new, close or select")
        rows = [f"- {i}:{' (current)' if p['id'] == self.tab['tid'] else ''} [{p.get('title', '')}] ({p.get('url', '')})" for i, p in enumerate(self._pages())]
        return _text("### Open tabs\n" + "\n".join(rows))

    def t_handle_dialog(self, a):
        if not self.tab["dialog"]:
            raise ToolError("no dialog is open")
        p = {"accept": bool(a.get("accept"))}
        if a.get("promptText") is not None:
            p["promptText"] = str(a["promptText"])
        self.s("Page.handleJavaScriptDialog", p)
        self.tab["dialog"] = None
        time.sleep(0.05)
        return _text("dialog " + ("accepted" if a.get("accept") else "dismissed") + "\n" + self.state())

    def t_find(self, a):
        got = find_in_snapshot(self.snapshot(), str(a.get("text") or ""), str(a.get("regex") or ""), int(a.get("maxResults") or 0))
        return self._save(a, got) or _text(got)

    def t_console_messages(self, a):
        floor = LEVELS.index(a.get("level") if a.get("level") in LEVELS else "info")
        rows = [f"[{lvl.upper()}] {txt}" for nav, lvl, txt in self.tab["console"] if LEVELS.index(lvl) >= floor and (a.get("all") or nav == self.tab["navs"])]
        body = "\n".join(rows) if rows else "no console messages"
        return self._save(a, body, "log") or _text(body)

    def t_resize(self, a):
        w, h = int(a["width"]), int(a["height"])
        self.s("Emulation.setDeviceMetricsOverride", {"width": w, "height": h, "deviceScaleFactor": 0, "mobile": False})
        return _text(f"resized to {w}x{h}")

    def t_drag(self, a):
        src, dst = self.node(a, "startTarget", "startRef"), self.node(a, "endTarget", "endRef")
        q1 = self.ready(src, "drag")
        self.glide(q1)
        q2 = self.ready(dst, "drop", hit=False)
        self._mouse("mouseMoved", q1[0], q1[1])
        self._mouse("mousePressed", q1[0], q1[1], "left", 1, 0, 1)
        for i in range(1, 9):
            self._mouse("mouseMoved", q1[0] + (q2[0] - q1[0]) * i / 8, q1[1] + (q2[1] - q1[1]) * i / 8, "left", 0, 0, 1)
            time.sleep(0.02)
        self._mouse("mouseReleased", q2[0], q2[1], "left", 1, 0, 0)
        self.glide(q2)
        return _text(self.state())

    def t_evaluate(self, a):
        fn = str(a.get("function") or "").strip()
        if not fn:
            raise ToolError("function is required")
        mark = self._mark()
        race = (" return Promise.race([Promise.resolve(__r), new Promise((_, no) => setTimeout(() => no(new Error('the script did not finish in "
                + str(EVAL_ASYNC_MS // 1000) + " s')), " + str(EVAL_ASYNC_MS) + "))]);")
        if a.get("target") or a.get("ref"):
            res = self.on(self.node(a), "function() { const __f = (\n" + fn + "\n); const __r = typeof __f === 'function' ? __f(this) : __f;" + race + " }",
                          timeout=SCRIPT_S + 3)
        else:
            res = self.ev("(() => { const __f = (\n" + fn + "\n); const __r = typeof __f === 'function' ? __f() : __f;" + race + " })()", timeout=SCRIPT_S)
        body = json.dumps(res, indent=1, ensure_ascii=False, default=str)      # nothing returned reads as null, as before
        self._settle(mark)                                                     # a script may send the page somewhere too
        return self._save(a, body, "json") or _text("### Result\n" + body + "\n" + self.state(with_snapshot=False))

    def close(self) -> None:
        try:                                   # let go only: the browser is the doer's, never ours to close
            if self.conn is not None:
                self.conn.close()
        except Exception:  # noqa: BLE001
            pass

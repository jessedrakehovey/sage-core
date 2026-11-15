"""browser_mcp.py - the doer's own browser tool server: no Node, no npx, no download at run time (2026-11-14).

A stdio MCP server (newline-delimited JSON-RPC 2.0, standard library) that attaches to the doer's already-running
browser over its debugging port with the Playwright Python package the engine already needs, and offers the same
tool names the Node `@playwright/mcp` package did (browser_navigate, browser_snapshot, browser_click ...), so the
doer's allow / deny lists, its hooks (click_guard.py, egress.py) and prompt.md are untouched.

    python assistant/doer/browser_mcp.py --cdp-endpoint http://127.0.0.1:<port> --output-dir <folder>
    python assistant/doer/browser_mcp.py --check        # can this interpreter serve? (exit 0 = yes; one line why not)

What it deliberately does NOT offer (the doer's deny list names them; here they do not exist at all): running
arbitrary Playwright code, file upload, raw network requests, installing a browser.

Elements are addressed by the `[ref=e12]` marks of the page snapshot (Playwright's own `aria_snapshot(mode="ai")` +
its `aria-ref=` selector), or by a plain selector. `target` is the current name of that argument, `ref` the older
one; both are taken. stdout carries protocol lines only; everything else goes to stderr.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import time
from pathlib import Path

NAME, VERSION = "sage-browser", "1"
PROTOCOL = "2025-06-18"
ACTION_MS = 5000            # one click / type / hover (the Node server's default)
NAV_MS = 60000
SNAP_MAX = 60000            # characters of snapshot returned inline; the rest is cut with a note
REF = re.compile(r"^(?:f\d+)?e\d+$")
LEVELS = ["debug", "info", "warning", "error"]

_EL = {"type": "string", "description": "Human-readable element description"}
_TG = {"type": "string", "description": "Exact target element reference from the page snapshot, or a unique element selector"}


def _schema(props: dict | None = None, required: list | None = None) -> dict:
    return {"type": "object", "properties": props or {}, "required": required or []}


TOOLS = [
    ("browser_navigate", "Navigate to a URL", _schema({"url": {"type": "string", "description": "The URL to navigate to"}}, ["url"])),
    ("browser_navigate_back", "Go back to the previous page in the history", _schema()),
    ("browser_snapshot", "Capture accessibility snapshot of the current page, this is better than screenshot",
     _schema({"depth": {"type": "number", "description": "Limit the depth of the snapshot tree"},
              "boxes": {"type": "boolean", "description": "Include each element's bounding box"},
              "target": _TG})),
    ("browser_click", "Perform click on a web page",
     _schema({"element": _EL, "target": _TG, "doubleClick": {"type": "boolean"},
              "button": {"type": "string", "enum": ["left", "right", "middle"]},
              "modifiers": {"type": "array", "items": {"type": "string", "enum": ["Alt", "Control", "ControlOrMeta", "Meta", "Shift"]}}},
             ["target"])),
    ("browser_type", "Type text into editable element",
     _schema({"element": _EL, "target": _TG, "text": {"type": "string"},
              "submit": {"type": "boolean", "description": "Press Enter after"},
              "slowly": {"type": "boolean", "description": "Type one character at a time"}}, ["target", "text"])),
    ("browser_fill_form", "Fill multiple form fields",
     _schema({"fields": {"type": "array", "items": _schema(
         {"name": {"type": "string"}, "element": _EL, "target": _TG,
          "type": {"type": "string", "enum": ["textbox", "checkbox", "radio", "combobox", "slider"]},
          "value": {"type": "string", "description": "checkbox: true / false; combobox: the option's text"}},
         ["target", "name", "type", "value"])}}, ["fields"])),
    ("browser_select_option", "Select an option in a dropdown",
     _schema({"element": _EL, "target": _TG, "values": {"type": "array", "items": {"type": "string"}}}, ["target", "values"])),
    ("browser_press_key", "Press a key on the keyboard",
     _schema({"key": {"type": "string", "description": "Name of the key to press or a character, such as `ArrowLeft` or `a`"}}, ["key"])),
    ("browser_hover", "Hover over element on page", _schema({"element": _EL, "target": _TG}, ["target"])),
    ("browser_wait_for", "Wait for text to appear or disappear or a specified time to pass",
     _schema({"time": {"type": "number", "description": "Seconds, at most 30"}, "text": {"type": "string"}, "textGone": {"type": "string"}})),
    ("browser_take_screenshot", "Take a screenshot of the current page. Use browser_snapshot for actions.",
     _schema({"type": {"type": "string", "enum": ["png", "jpeg"]}, "filename": {"type": "string"},
              "element": _EL, "target": _TG, "fullPage": {"type": "boolean"}})),
    ("browser_tabs", "List, create, close, or select a browser tab.",
     _schema({"action": {"type": "string", "enum": ["list", "new", "close", "select"]}, "index": {"type": "number"},
              "url": {"type": "string"}}, ["action"])),
    ("browser_handle_dialog", "Handle a dialog",
     _schema({"accept": {"type": "boolean"}, "promptText": {"type": "string"}}, ["accept"])),
    ("browser_find", "Search the accessibility snapshot of the current page for text or a regular expression; returns "
                     "the matching lines with a little context, cheaper than the whole snapshot",
     _schema({"text": {"type": "string", "description": "Plain text (case-insensitive)"},
              "regex": {"type": "string", "description": "A regular expression; /pattern/i for flags"},
              "maxResults": {"type": "number"}})),
    ("browser_console_messages", "Returns console messages",
     _schema({"level": {"type": "string", "enum": ["error", "warning", "info", "debug"]}, "all": {"type": "boolean"}})),
    ("browser_resize", "Resize the browser window",
     _schema({"width": {"type": "number"}, "height": {"type": "number"}}, ["width", "height"])),
    ("browser_drag", "Perform drag and drop between two elements",
     _schema({"startElement": _EL, "startTarget": _TG, "endElement": _EL, "endTarget": _TG}, ["startTarget", "endTarget"])),
    ("browser_evaluate", "Evaluate JavaScript expression on page or element",
     _schema({"function": {"type": "string", "description": "() => { /* code */ } or (element) => { /* code */ } when a target is given"},
              "element": _EL, "target": _TG}, ["function"])),
]


def log(msg: str) -> None:
    print(f"[browser_mcp] {msg}", file=sys.stderr, flush=True)


class ToolError(Exception):
    """A failure the model should read as the tool's result (never a protocol error)."""


def find_in_snapshot(snap: str, text: str = "", regex: str = "", max_results: int = 0, context: int = 1) -> str:
    """The lines of a snapshot that match, each with `context` lines around it. Pure text: tested without a browser."""
    if bool(text) == bool(regex):
        raise ToolError("give either text or regex, not both")
    if regex:
        m = re.match(r"^/(.*)/([a-z]*)$", regex, re.S)
        pat, flags = (m.group(1), re.I if "i" in m.group(2) else 0) if m else (regex, 0)
        try:
            rx = re.compile(pat, flags)
        except re.error as e:
            raise ToolError(f"bad regular expression: {e}") from None
    else:
        rx = re.compile(re.escape(text), re.I)
    lines = snap.splitlines()
    hits = [i for i, ln in enumerate(lines) if rx.search(ln)]
    if not hits:
        return "no match in the page snapshot"
    total = len(hits)
    if max_results and max_results > 0:
        hits = hits[:int(max_results)]
    out = []
    for i in hits:
        lo, hi = max(0, i - context), min(len(lines), i + context + 1)
        out.append("\n".join(lines[lo:hi]))
    head = f"{total} match(es)" + (f", first {len(hits)} shown" if len(hits) < total else "")
    return head + "\n\n" + "\n---\n".join(out)


def clip(snap: str, limit: int = SNAP_MAX) -> str:
    if len(snap) <= limit:
        return snap
    return snap[:limit] + f"\n... (snapshot cut at {limit} of {len(snap)} characters: use browser_find, or browser_snapshot with a target)"


class Browser:
    """One attachment to the doer's browser. Connects on the first tool call; re-attaches if the link dropped."""

    def __init__(self, cdp: str, out_dir: Path):
        self.cdp, self.out = cdp, out_dir
        self.pw = self.browser = self.ctx = self.page = None
        self.dialog = None
        self.console: list[tuple[int, str, str]] = []     # (navigation count, level, text)
        self.navs = 0
        self._wired: set[int] = set()

    # ---- attachment ------------------------------------------------------------------------------------------
    def attach(self) -> None:
        if self.browser is not None and self.browser.is_connected():
            return
        from playwright.sync_api import sync_playwright
        if self.pw is None:
            self.pw = sync_playwright().start()
        self.browser = self.pw.chromium.connect_over_cdp(self.cdp, timeout=20000)
        ctxs = self.browser.contexts
        if not ctxs:
            raise ToolError("the browser has no window open")
        self.ctx = ctxs[0]
        self.ctx.on("page", self._wire)
        for p in self.ctx.pages:
            self._wire(p)
        self.page = self._front()

    def _front(self):
        """The tab in front (the doer puts the job's own tab there), else the first one."""
        pages = [p for p in self.ctx.pages if not p.is_closed()]
        if not pages:
            return self.ctx.new_page()
        for p in pages:
            try:
                if p.evaluate("document.visibilityState") == "visible":
                    return p
            except Exception:  # noqa: BLE001
                continue
        return pages[0]

    def _wire(self, page) -> None:
        if id(page) in self._wired:
            return
        self._wired.add(id(page))
        page.on("dialog", self._on_dialog)
        page.on("console", lambda m: self._on_console(m))
        page.on("framenavigated", lambda f, p=page: self._on_nav(f, p))

    def _on_dialog(self, d) -> None:
        self.dialog = d

    def _on_console(self, m) -> None:
        try:
            lvl = {"warn": "warning", "log": "info"}.get(m.type, m.type)
            self.console.append((self.navs, lvl if lvl in LEVELS else "info", m.text))
            del self.console[:-500]
        except Exception:  # noqa: BLE001
            pass

    def _on_nav(self, frame, page) -> None:
        try:
            if frame == page.main_frame and page is self.page:
                self.navs += 1
        except Exception:  # noqa: BLE001
            pass

    # ---- helpers ---------------------------------------------------------------------------------------------
    def loc(self, a: dict, key: str = "target", old: str = "ref"):
        t = str(a.get(key) or a.get(old) or "").strip()
        if not t:
            raise ToolError(f"{key} is required: an element reference from the page snapshot (like e12) or a selector")
        return self.page.locator(f"aria-ref={t}" if REF.match(t) else t)

    def snapshot(self, a: dict | None = None) -> str:
        a = a or {}
        kw = {"mode": "ai", "timeout": 15000}
        if a.get("depth"):
            kw["depth"] = int(a["depth"])
        if a.get("boxes"):
            kw["boxes"] = True
        if a.get("target") or a.get("ref"):
            return self.loc(a).aria_snapshot(**kw)
        return self.page.aria_snapshot(**kw)

    def state(self, with_snapshot: bool = True) -> str:
        if self.dialog is not None:
            return self._dialog_note()
        try:
            self.page.wait_for_load_state("domcontentloaded", timeout=5000)
        except Exception:  # noqa: BLE001
            pass
        out = [f"### Page\n- Page URL: {self.page.url}\n- Page Title: {self._title()}"]
        if with_snapshot:
            try:
                out.append("### Snapshot\n```yaml\n" + clip(self.snapshot()) + "\n```")
            except Exception as e:  # noqa: BLE001
                out.append(f"### Snapshot\n(not available: {_err(e)})")
        return "\n".join(out)

    def _title(self) -> str:
        try:
            return self.page.title()
        except Exception:  # noqa: BLE001
            return ""

    def _dialog_note(self) -> str:
        d = self.dialog
        return (f'### Modal state\n- a "{d.type}" dialog is open with the message "{d.message}": '
                "answer it with browser_handle_dialog before anything else")

    def _tabs(self) -> str:
        rows = []
        for i, p in enumerate(self.ctx.pages):
            try:
                title = p.title()
            except Exception:  # noqa: BLE001
                title = ""
            rows.append(f"- {i}:{' (current)' if p is self.page else ''} [{title}] ({p.url})")
        return "### Open tabs\n" + "\n".join(rows)

    # ---- tools -----------------------------------------------------------------------------------------------
    def call(self, name: str, a: dict) -> list[dict]:
        self.attach()
        if self.page is None or self.page.is_closed():
            self.page = self._front()
        if self.dialog is not None and name != "browser_handle_dialog":
            return _text(self._dialog_note())
        fn = getattr(self, "t_" + name[len("browser_"):], None) if name.startswith("browser_") else None
        if fn is None:
            raise ToolError(f"no such tool: {name}")
        try:
            return fn(a)
        except ToolError:
            raise
        except Exception as e:  # noqa: BLE001
            if self.dialog is not None:            # the action opened a dialog and stalled behind it
                return _text(self._dialog_note())
            raise ToolError(_err(e)) from None

    def t_navigate(self, a):
        url = str(a.get("url") or "").strip()
        if not url:
            raise ToolError("url is required")
        if not re.match(r"^[a-z][a-z0-9+.-]*:", url, re.I):
            url = "https://" + url
        self.page.goto(url, wait_until="domcontentloaded", timeout=NAV_MS)
        return _text(self.state())

    def t_navigate_back(self, a):
        self.page.go_back(wait_until="domcontentloaded", timeout=NAV_MS)
        return _text(self.state())

    def t_snapshot(self, a):
        return _text(f"### Page\n- Page URL: {self.page.url}\n- Page Title: {self._title()}\n### Snapshot\n```yaml\n"
                     + clip(self.snapshot(a)) + "\n```")

    def t_click(self, a):
        kw = {"timeout": ACTION_MS, "button": a.get("button") or "left"}
        if a.get("modifiers"):
            kw["modifiers"] = list(a["modifiers"])
        loc = self.loc(a)
        (loc.dblclick if a.get("doubleClick") else loc.click)(**kw)
        return _text(self.state())

    def t_type(self, a):
        loc, text = self.loc(a), str(a.get("text") or "")
        if a.get("slowly"):
            loc.press_sequentially(text, timeout=max(ACTION_MS, 80 * len(text) + ACTION_MS))
        else:
            loc.fill(text, timeout=ACTION_MS)
        if a.get("submit"):
            loc.press("Enter", timeout=ACTION_MS)
        return _text(self.state(with_snapshot=bool(a.get("submit"))))

    def t_fill_form(self, a):
        fields = a.get("fields") or []
        if not fields:
            raise ToolError("fields is required")
        for f in fields:
            loc, kind, val = self.loc(f), str(f.get("type") or "textbox"), str(f.get("value") or "")
            if kind in ("checkbox", "radio"):
                loc.set_checked(val.strip().lower() == "true", timeout=ACTION_MS)
            elif kind == "combobox":
                loc.select_option(label=val, timeout=ACTION_MS)
            else:
                loc.fill(val, timeout=ACTION_MS)
        return _text(f"filled {len(fields)} field(s)\n" + self.state(with_snapshot=False))

    def t_select_option(self, a):
        vals = [str(v) for v in (a.get("values") or [])]
        loc = self.loc(a)
        try:
            loc.select_option(vals, timeout=ACTION_MS)
        except Exception:  # noqa: BLE001 - the values were the options' visible text, not their value attributes
            loc.select_option(label=vals, timeout=ACTION_MS)
        return _text(self.state())

    def t_press_key(self, a):
        key = str(a.get("key") or "")
        if not key:
            raise ToolError("key is required")
        self.page.keyboard.press(key)
        return _text(self.state())

    def t_hover(self, a):
        self.loc(a).hover(timeout=ACTION_MS)
        return _text(self.state())

    def t_wait_for(self, a):
        secs = min(float(a.get("time") or 0), 30.0)
        text, gone = a.get("text"), a.get("textGone")
        if not (text or gone):
            if secs <= 0:
                raise ToolError("give time, text or textGone")
            self.page.wait_for_timeout(secs * 1000)
            return _text(f"waited {secs:g} s\n" + self.state())
        ms = secs * 1000 if secs > 0 else ACTION_MS
        if gone:
            self.page.get_by_text(str(gone)).first.wait_for(state="hidden", timeout=ms)
        if text:
            self.page.get_by_text(str(text)).first.wait_for(state="visible", timeout=ms)
        return _text(f"waited for {text or gone}\n" + self.state())

    def t_take_screenshot(self, a):
        kind = str(a.get("type") or "").lower()
        fn = str(a.get("filename") or "").strip()
        if not kind:
            kind = "jpeg" if fn.lower().endswith((".jpg", ".jpeg")) else "png"
        if kind not in ("png", "jpeg"):
            raise ToolError("type must be png or jpeg")
        name = Path(fn).name if fn else f"page-{time.strftime('%Y%m%dT%H%M%S')}.{'jpg' if kind == 'jpeg' else 'png'}"
        self.out.mkdir(parents=True, exist_ok=True)
        dst = self.out / name                              # always inside the job's own folder, whatever path was asked
        kw = {"path": str(dst), "type": kind, "timeout": 20000}
        if a.get("target") or a.get("ref"):
            self.loc(a).screenshot(**kw)
        else:
            self.page.screenshot(full_page=bool(a.get("fullPage")), **kw)
        return _text(f"screenshot saved: {dst.as_posix()}")

    def t_tabs(self, a):
        act = str(a.get("action") or "list")
        pages = self.ctx.pages
        idx = a.get("index")
        if act == "new":
            self.page = self.ctx.new_page()
            self._wire(self.page)
            if a.get("url"):
                self.page.goto(str(a["url"]), wait_until="domcontentloaded", timeout=NAV_MS)
        elif act == "select":
            if idx is None or not 0 <= int(idx) < len(pages):
                raise ToolError(f"no tab {idx}; there are {len(pages)}")
            self.page = pages[int(idx)]
            self.page.bring_to_front()
        elif act == "close":
            victim = self.page if idx is None else (pages[int(idx)] if 0 <= int(idx) < len(pages) else None)
            if victim is None:
                raise ToolError(f"no tab {idx}; there are {len(pages)}")
            victim.close()
            if victim is self.page:
                self.page = self._front()
        elif act != "list":
            raise ToolError("action must be list, new, close or select")
        return _text(self._tabs())

    def t_handle_dialog(self, a):
        d = self.dialog
        if d is None:
            raise ToolError("no dialog is open")
        self.dialog = None
        if a.get("accept"):
            d.accept(str(a["promptText"])) if a.get("promptText") is not None else d.accept()
        else:
            d.dismiss()
        return _text("dialog " + ("accepted" if a.get("accept") else "dismissed") + "\n" + self.state())

    def t_find(self, a):
        return _text(find_in_snapshot(self.snapshot(), str(a.get("text") or ""), str(a.get("regex") or ""),
                                      int(a.get("maxResults") or 0)))

    def t_console_messages(self, a):
        floor = LEVELS.index(a.get("level") if a.get("level") in LEVELS else "info")
        rows = [f"[{lvl.upper()}] {txt}" for nav, lvl, txt in self.console
                if LEVELS.index(lvl) >= floor and (a.get("all") or nav == self.navs)]
        return _text("\n".join(rows) if rows else "no console messages")

    def t_resize(self, a):
        self.page.set_viewport_size({"width": int(a["width"]), "height": int(a["height"])})
        return _text(f"resized to {int(a['width'])}x{int(a['height'])}")

    def t_drag(self, a):
        self.loc(a, "startTarget", "startRef").drag_to(self.loc(a, "endTarget", "endRef"), timeout=ACTION_MS)
        return _text(self.state())

    def t_evaluate(self, a):
        fn = str(a.get("function") or "").strip()
        if not fn:
            raise ToolError("function is required")
        res = self.loc(a).evaluate(fn, timeout=ACTION_MS) if (a.get("target") or a.get("ref")) else self.page.evaluate(fn)
        try:
            body = json.dumps(res, indent=1, ensure_ascii=False, default=str)
        except Exception:  # noqa: BLE001
            body = str(res)
        return _text("### Result\n" + (body if body is not None else "undefined") + "\n" + self.state(with_snapshot=False))

    def close(self) -> None:
        try:                                   # detach only: the browser is the doer's, never ours to close
            if self.pw is not None:
                self.pw.stop()
        except Exception:  # noqa: BLE001
            pass


def _text(s: str) -> list[dict]:
    return [{"type": "text", "text": s}]


def _err(e: Exception) -> str:
    """Playwright's own words (its call log carries the 'waiting for element to be visible, enabled and stable' line
    click_guard.py counts), without the box-drawing banner."""
    s = str(e)
    return re.sub(r"[─-╿]+", "", s).strip()[:4000] or type(e).__name__


def handle(req: dict, br: Browser) -> dict | None:
    """One JSON-RPC message -> its reply (None for a notification)."""
    mid, method, params = req.get("id"), req.get("method"), req.get("params") or {}
    if mid is None:
        return None
    if method == "initialize":
        return _ok(mid, {"protocolVersion": params.get("protocolVersion") or PROTOCOL, "capabilities": {"tools": {}},
                         "serverInfo": {"name": NAME, "version": VERSION}})
    if method == "ping":
        return _ok(mid, {})
    if method == "tools/list":
        return _ok(mid, {"tools": [{"name": n, "description": d, "inputSchema": s} for n, d, s in TOOLS]})
    if method == "tools/call":
        name, args = str(params.get("name") or ""), params.get("arguments") or {}
        try:
            return _ok(mid, {"content": br.call(name, args)})
        except ToolError as e:
            return _ok(mid, {"content": _text("Error: " + str(e)), "isError": True})
        except Exception as e:  # noqa: BLE001
            log(f"{name}: {type(e).__name__}: {e}")
            return _ok(mid, {"content": _text("Error: " + _err(e)), "isError": True})
    if method in ("resources/list", "prompts/list"):
        return _ok(mid, {method.split("/")[0]: []})
    return {"jsonrpc": "2.0", "id": mid, "error": {"code": -32601, "message": f"method not found: {method}"}}


def _ok(mid, result: dict) -> dict:
    return {"jsonrpc": "2.0", "id": mid, "result": result}


def check() -> tuple[bool, str]:
    """Can this interpreter serve? The Playwright package, new enough for the snapshot with element references."""
    try:
        import inspect
        from playwright.sync_api import Page
    except Exception as e:  # noqa: BLE001
        return False, f"the playwright package is missing here ({type(e).__name__})"
    if "mode" not in inspect.signature(Page.aria_snapshot).parameters:
        return False, "the playwright package here is too old for element references: pip install -U playwright"
    return True, "ok"


def serve(br: Browser, stdin=None, stdout=None) -> None:
    stdin, stdout = stdin or sys.stdin.buffer, stdout or sys.stdout.buffer
    for raw in iter(stdin.readline, b""):
        raw = raw.strip()
        if not raw:
            continue
        try:
            req = json.loads(raw.decode("utf-8"))
        except Exception:  # noqa: BLE001
            stdout.write(json.dumps({"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": "parse error"}}).encode("utf-8") + b"\n")
            stdout.flush()
            continue
        for one in (req if isinstance(req, list) else [req]):
            rep = handle(one, br) if isinstance(one, dict) else None
            if rep is not None:
                stdout.write(json.dumps(rep, ensure_ascii=False).encode("utf-8") + b"\n")
                stdout.flush()


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--cdp-endpoint", default="")
    ap.add_argument("--output-dir", default=".")
    ap.add_argument("--check", action="store_true")
    ns = ap.parse_args(argv)
    if ns.check:
        ok, why = check()
        print(why)
        return 0 if ok else 1
    if not ns.cdp_endpoint:
        ap.error("--cdp-endpoint is required")
    br = Browser(ns.cdp_endpoint, Path(ns.output_dir))
    try:
        serve(br)
    finally:
        br.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())

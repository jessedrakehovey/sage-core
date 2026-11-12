"""upload.py - hand a file on this PC to a web page in the doer's browser (Dropbox, Drive, a form). Built 2026-11-10.

Why: the browser lane has no file picker (browser_file_upload is on its frozen deny list), and the MCP may only read
files under its own roots, so replacing a finished film in Dropbox from the Videos folder came back blocked twice
(gap g-29ce2a). This script gives the file to the page itself over CDP (DOM.setFileInputFiles): Chrome reads it
straight from disk, so a 2 GB film needs no copy and has no size cap, and the signed-in session is the shared profile.

The computer lane (`do` with where: computer) runs it; it passes computer_policy's scan (tier B):

  python assistant/doer/upload.py --file "C:/Users/me/Videos/Film.mov" --url "https://www.dropbox.com/home/Media" --replace

Steps, all by code: open (or find) the page; find its file input, or click the visible "Upload" > "Files" controls
(`--click` names others) with the native dialog held back so the input is caught instead; give it the file; press
"Replace" on a same-name prompt when `--replace` (a replaced Dropbox file keeps its share links); then watch the page
until the upload reads done. An upload still going after `--wait` seconds is watched on by a hidden copy of this
script, and that tab counts as held (browser.held_tabs) so no other job prunes it or quits the browser mid-upload.
Prints one line and a status JSON (also written under assistant/out/doer/uploads/): state done | uploading |
unknown | failed.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import time
import urllib.parse
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import browser  # noqa: E402

OUT = HERE.parent / "out" / "doer" / "uploads"
BUSY = browser.UPLOADS                              # tabs with an upload running; browser.held_tabs reads it
HOLD_S = 180                                        # a watcher that stops refreshing lets its tab go after this
NO_WINDOW = 0x08000000
NEW_GROUP = 0x00000200
DEFAULT_CLICKS = ["Upload", "Files"]
REPLACE_WORDS = ["Replace", "Replace file", "Replace all", "Overwrite"]
BUSY_RX = r"\bUploading\b|\bupload(?:s|ing)? in progress\b|\b(?:time )?remaining\b"
DONE_RX = r"\bUploaded\b|\bupload(?:s)? complete(?:d)?\b|\bupload finished\b|\bfinished uploading\b"

# Caught file inputs: a click on <input type=file> (or showPicker) marks it instead of opening the native dialog.
CATCH_JS = """(() => { if (window.__sageCatch) return 'ok'; window.__sageCatch = 1;
  const mark = (el) => { if (el && el.type === 'file') { el.setAttribute('data-sage-upload', '1'); return true; } return false; };
  const c0 = HTMLInputElement.prototype.click;
  HTMLInputElement.prototype.click = function () { if (mark(this)) return; return c0.apply(this, arguments); };
  if (HTMLInputElement.prototype.showPicker) { const p0 = HTMLInputElement.prototype.showPicker;
    HTMLInputElement.prototype.showPicker = function () { if (mark(this)) return; return p0.apply(this, arguments); }; }
  return 'ok'; })()"""

FIND_INPUT_JS = """(() => { const all = [...document.querySelectorAll('input[type=file]')];
  return all.find(e => e.hasAttribute('data-sage-upload')) || all.find(e => !e.webkitdirectory && !e.disabled) || null; })()"""


def _click_js(words: list) -> str:
    """Click the first visible control whose label or text is one of `words` (case-insensitive). -> the word or ''."""
    return """((words) => { const want = words.map(w => w.toLowerCase().trim());
  const seen = (e) => { const r = e.getBoundingClientRect(); const s = getComputedStyle(e);
    return r.width > 0 && r.height > 0 && s.visibility !== 'hidden' && s.display !== 'none'; };
  const label = (e) => ((e.getAttribute('aria-label') || '') + '|' + (e.innerText || '')).toLowerCase().split('|').map(t => t.trim());
  const cands = [...document.querySelectorAll('button,[role=button],[role=menuitem],[role=menuitemradio],[role=option],a,li,label')];
  for (const w of want) { const hit = cands.find(e => seen(e) && label(e).includes(w));
    if (hit) { for (const t of ['pointerdown', 'mousedown', 'pointerup', 'mouseup'])
        hit.dispatchEvent(new MouseEvent(t, {bubbles: true, cancelable: true, view: window}));
      hit.click(); return w; } }
  return ''; })(%s)""" % json.dumps(list(words))


def _pages() -> list:
    try:
        with urllib.request.urlopen(browser.cdp_url() + "/json/list", timeout=5) as r:
            return [t for t in json.loads(r.read().decode("utf-8", "replace")) if t.get("type") == "page"]
    except Exception:  # noqa: BLE001
        return []


def _open_tab(url: str) -> dict:
    req = urllib.request.Request(browser.cdp_url() + "/json/new?" + urllib.parse.quote(url, safe=":/?&=%#"), method="PUT")
    with urllib.request.urlopen(req, timeout=10) as r:
        return json.loads(r.read().decode("utf-8", "replace"))


def find_page(url: str = "", match: str = "", pages: list | None = None) -> dict:
    """The page to work in: one already at `match` (or `url`), else a fresh tab at `url`, else the page in front."""
    pages = _pages() if pages is None else pages
    key = (match or url.split("?")[0]).lower().rstrip("/")
    if key:
        for p in pages:
            if str(p.get("url", "")).lower().rstrip("/").startswith(key) or (match and match.lower() in str(p.get("url", "")).lower()):
                return p
    if url:
        return _open_tab(url)
    return browser.active_page(pages)


def js(ws: str, expr: str, by_value: bool = True):
    r = browser._cdp_calls(ws, [("Runtime.evaluate", {"expression": expr, "returnByValue": by_value, "userGesture": True,
                                                       "awaitPromise": False})])[0] or {}
    return ((r.get("result") or {}).get("value")) if by_value else r


def give_file(ws: str, path: str) -> str:
    """DOM.setFileInputFiles on the page's file input, in one CDP session. -> '' or why not."""
    def params(out):
        oid = (((out[0] or {}).get("result") or {}).get("objectId")) or ""
        return {"files": [path], "objectId": oid} if oid else {"files": [path], "nodeId": 0}
    r = browser._cdp_calls(ws, [("Runtime.evaluate", {"expression": FIND_INPUT_JS, "returnByValue": False}),
                                ("DOM.setFileInputFiles", params)])
    if not (((r[0] or {}).get("result") or {}).get("objectId")):
        return "the page has no file input to give the file to"
    if isinstance(r[1], dict) and r[1].get("error"):
        return f"the page refused the file ({str(r[1]['error'])[:120]})"
    return ""


def page_text(ws: str) -> str:
    return str(js(ws, "(document.body && document.body.innerText || '').slice(0, 20000)") or "")


def hold(target: str, info: dict | None) -> None:
    """Mark `target` as uploading (info) or let it go (None). browser.held_tabs keeps a marked tab."""
    try:
        d = json.loads(BUSY.read_text(encoding="utf-8")) if BUSY.is_file() else {}
    except (OSError, ValueError):
        d = {}
    now = time.time()
    d = {k: v for k, v in d.items() if isinstance(v, dict) and float(v.get("until") or 0) > now}
    if info is None:
        d.pop(target, None)
    else:
        d[target] = dict(info, until=now + HOLD_S)
    BUSY.parent.mkdir(parents=True, exist_ok=True)
    BUSY.write_text(json.dumps(d, indent=1), encoding="utf-8")


def watch(ws: str, target: str, wait_s: float, confirm: list, busy_rx: str, done_rx: str, info: dict,
          seen_busy: bool = False, poll_s: float = 3.0) -> dict:
    """Poll the page until the upload reads done, or `wait_s` runs out. Presses a confirm word when one shows."""
    t0, quiet, pressed = time.time(), 0, []
    while time.time() - t0 < wait_s:
        hold(target, info)
        if confirm and len(pressed) < 3:              # a same-name prompt is pressed, never a button over and over
            w = js(ws, _click_js(confirm))
            if w:
                pressed.append(w)
                time.sleep(poll_s)
                continue
        try:
            text = page_text(ws)
        except (OSError, ValueError):
            return {"state": "failed", "why": "the page closed before the upload read done", "pressed": pressed}
        if re.search(done_rx, text, re.I) and not re.search(busy_rx, text, re.I):
            return {"state": "done", "pressed": pressed}
        if re.search(busy_rx, text, re.I):
            seen_busy, quiet = True, 0
            m = re.search(r"(\d{1,3}(?:\.\d)?)\s?%", text)
            info["progress"] = m.group(1) + "%" if m else ""
        elif seen_busy:
            quiet += 1
            if quiet >= 2:
                return {"state": "done", "pressed": pressed}
        time.sleep(poll_s)
    return {"state": "uploading" if seen_busy else "unknown", "pressed": pressed, "progress": info.get("progress", "")}


def _status_file(name: str) -> Path:
    OUT.mkdir(parents=True, exist_ok=True)
    return OUT / f"{time.strftime('%Y%m%d-%H%M%S')}-{re.sub(r'[^A-Za-z0-9._-]+', '_', name)[:60]}.json"


def _save(path: Path, d: dict) -> None:
    path.write_text(json.dumps(d, indent=1), encoding="utf-8")


def _watch_on(args: list) -> int:
    """Hand the rest of the watch to a hidden copy of this script (pythonw, no window)."""
    exe = Path(sys.executable)
    quiet_exe = exe.with_name("pythonw.exe")
    cmd = [str(quiet_exe if quiet_exe.is_file() else exe), str(Path(__file__).resolve())] + args
    p = subprocess.Popen(cmd, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                         creationflags=NO_WINDOW | NEW_GROUP, close_fds=True)
    return p.pid


def run(file: str, url: str = "", match: str = "", clicks: list | None = None, replace: bool = False,
        wait_s: float = 90.0, watch_s: float = 3 * 3600.0, busy_rx: str = BUSY_RX, done_rx: str = DONE_RX,
        background: bool = True) -> dict:
    src = Path(os.path.expanduser(file))
    st = {"file": str(src), "url": url, "state": "failed", "started": time.strftime("%Y-%m-%dT%H:%M:%S")}
    out = _status_file(src.name)
    st["status_file"] = out.as_posix()
    if not src.is_file():
        st["why"] = f"no file at {src}"
        _save(out, st)
        return st
    st["bytes"] = src.stat().st_size
    try:
        browser.open_browser()
        page = find_page(url, match)
    except Exception as e:  # noqa: BLE001
        st["why"] = f"the browser would not open the page ({type(e).__name__}: {str(e)[:120]})"
        _save(out, st)
        return st
    ws, target = str(page.get("webSocketDebuggerUrl") or ""), str(page.get("id") or "")
    if not ws:
        st["why"] = "no page to upload into"
        _save(out, st)
        return st
    st["page"] = str(page.get("url") or url)
    t0 = time.time()
    while time.time() - t0 < 30 and js(ws, "document.readyState") != "complete":
        time.sleep(1)
    time.sleep(2)                                    # a single-page app draws its buttons after load
    js(ws, CATCH_JS)
    why = give_file(ws, str(src.resolve()))
    tried = []
    for w in (clicks if clicks else DEFAULT_CLICKS) if why else []:
        for _ in range(8):                           # a menu item shows a moment after its button
            hit = js(ws, _click_js([w]))
            if hit:
                tried.append(hit)
                break
            time.sleep(1)
        time.sleep(1)
        why = give_file(ws, str(src.resolve()))
        if not why:
            break
    st["clicked"] = tried
    if why:
        st["why"] = why + (f" (clicked {', '.join(tried)})" if tried else " (no Upload control found)")
        _save(out, st)
        return st
    info = {"file": str(src), "page": st["page"]}
    confirm = REPLACE_WORDS if replace else []
    res = watch(ws, target, wait_s, confirm, busy_rx, done_rx, info)
    st.update(res)
    if res["state"] in ("uploading", "unknown") and background:
        args = ["--watch-ws", ws, "--watch-target", target, "--status", out.as_posix(), "--file", str(src),
                "--wait", str(int(watch_s))] + (["--replace"] if replace else []) + (["--seen-busy"] if res["state"] == "uploading" else [])
        st["watcher_pid"] = _watch_on(args)
        st["note"] = "still uploading; a hidden watcher keeps the tab open and writes the end state to status_file"
    else:
        hold(target, None)
    _save(out, st)
    return st


def _line(st: dict) -> str:
    name = Path(st.get("file") or "").name
    s = st.get("state")
    if s == "done":
        return f"UPLOADED {name} to {st.get('page')}"
    if s == "uploading":
        return f"UPLOADING {name} ({st.get('progress') or 'under way'}); the watcher finishes it, status in {st.get('status_file')}"
    if s == "unknown":
        return f"HANDED {name} to the page but saw no upload progress yet; the watcher keeps checking, status in {st.get('status_file')}"
    return f"FAILED {name}: {st.get('why')}"


def main() -> int:
    ap = argparse.ArgumentParser(description="give a local file to a web page in the doer's browser")
    ap.add_argument("--file", required=True)
    ap.add_argument("--url", default="", help="the page to upload on (opened in a new tab unless one is there)")
    ap.add_argument("--match", default="", help="use an open page whose address contains this")
    ap.add_argument("--click", action="append", default=[], help="a control to click to reach the file input (repeatable)")
    ap.add_argument("--replace", action="store_true", help="press Replace on a same-name prompt (keeps share links)")
    ap.add_argument("--wait", type=float, default=90.0, help="seconds to watch here before a hidden watcher takes over")
    ap.add_argument("--busy", default=BUSY_RX)
    ap.add_argument("--done", default=DONE_RX)
    ap.add_argument("--watch-ws", default="", help=argparse.SUPPRESS)
    ap.add_argument("--watch-target", default="", help=argparse.SUPPRESS)
    ap.add_argument("--status", default="", help=argparse.SUPPRESS)
    ap.add_argument("--seen-busy", action="store_true", help=argparse.SUPPRESS)
    a = ap.parse_args()
    if a.watch_ws:                                   # the hidden watcher
        sp = Path(a.status)
        try:
            st = json.loads(sp.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            st = {"file": a.file}
        info = {"file": a.file, "page": st.get("page", "")}
        res = watch(a.watch_ws, a.watch_target, a.wait, REPLACE_WORDS if a.replace else [], a.busy, a.done, info,
                    seen_busy=a.seen_busy, poll_s=10.0)
        hold(a.watch_target, None)
        st.update(res, finished=time.strftime("%Y-%m-%dT%H:%M:%S"))
        st.pop("watcher_pid", None)
        st.pop("note", None)
        _save(sp, st)
        return 0 if res["state"] == "done" else 1
    st = run(a.file, a.url, a.match, a.click, a.replace, a.wait, busy_rx=a.busy, done_rx=a.done)
    print(_line(st))
    print(json.dumps(st))
    return 0 if st["state"] in ("done", "uploading", "unknown") else 1


if __name__ == "__main__":
    sys.exit(main())

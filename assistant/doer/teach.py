"""teach.py - teach the browser agent a task by doing it once: the owner clicks, the recorder watches, a skill is written.

Upgrade 13 of the Grok Bot / Muse teardown (2026-11-08; Grok Bot's "Teach a task"). Until now a site's how-to reached
the doer two ways: the model wrote a skill after a run, or someone typed one. A chore that changes too often to
hand-script (a CRM's menus, a vendor portal) is easier shown than described. This module records a session in the
doer's own browser over CDP (the same port login_fill uses) and compiles what it saw into a skill (agent/skills.write):

  record(title, lane, seconds)   attach to the doer's browser, inject a recorder into every page, collect
                                 navigations, clicks and field edits until `seconds` pass or <doer data>/teach.stop
                                 exists, then write the skill. Values typed are NEVER kept: a field edit is recorded as
                                 "type into <field label>" with a <value> placeholder; a password field is noted as
                                 "sign in (the vault does this)".
  compile_steps(events)          pure: events -> the numbered how-to body (tested offline)
  write_skill(title, lane, body, triggers)  -> skills.write result

  python assistant/doer/teach.py start "<task title>" [--lane <lane>] [--seconds 600]
  python assistant/doer/teach.py stop
  python assistant/doer/teach.py compile <events.json>      (offline: print the body)

Rails: the recorder reads the page's DOM labels only, never the values; a URL is kept without its query string;
skills.write's refusals (secrets, permissions) hold; nothing here acts in the browser.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
from pathlib import Path
from urllib.parse import urlparse, urlunparse

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(ROOT / "agent"))

DATA = Path(os.environ["DOER_DATA"]) if os.environ.get("DOER_DATA") else ROOT / "data" / "doer"
STOP = DATA / "teach.stop"
EVENTS_DIR = DATA / "teach"
MAX_STEPS = 60
BINDING = "__sageTeach"
RECORDER_JS = r"""
(() => {
  if (window.__sageTeachOn) return; window.__sageTeachOn = true;
  const lab = (el) => {
    if (!el) return "";
    const a = el.getAttribute && (el.getAttribute("aria-label") || el.getAttribute("placeholder") || el.getAttribute("name") || el.getAttribute("title"));
    if (a) return a;
    if (el.id) { const l = document.querySelector('label[for="' + el.id + '"]'); if (l && l.innerText) return l.innerText; }
    const p = el.closest && el.closest("label"); if (p && p.innerText) return p.innerText;
    const t = (el.innerText || el.value || el.textContent || "").trim(); return t.slice(0, 60);
  };
  const send = (o) => { try { window.__BIND__(JSON.stringify(o)); } catch (e) {} };
  document.addEventListener("click", (e) => {
    const el = e.target.closest ? (e.target.closest("button,a,[role=button],[role=tab],[role=menuitem],input[type=submit],input[type=checkbox],input[type=radio],summary,li,td,div[tabindex]") || e.target) : e.target;
    send({t: "click", tag: (el.tagName || "").toLowerCase(), role: el.getAttribute ? (el.getAttribute("role") || "") : "", label: lab(el), href: el.href || "", url: location.href});
  }, true);
  document.addEventListener("change", (e) => {
    const el = e.target; const type = (el.type || "").toLowerCase();
    send({t: "edit", tag: (el.tagName || "").toLowerCase(), type: type, label: lab(el), url: location.href,
          choice: (el.tagName === "SELECT" && el.selectedOptions && el.selectedOptions[0]) ? el.selectedOptions[0].text.slice(0, 60) : "",
          checked: (type === "checkbox" || type === "radio") ? !!el.checked : null});
  }, true);
  document.addEventListener("submit", (e) => { send({t: "submit", label: lab(e.target.querySelector && e.target.querySelector("button[type=submit],input[type=submit]")), url: location.href}); }, true);
})();
""".replace("__BIND__", BINDING)


# ------------------------------------------------------------------ compile (pure)
def _clean_url(u: str) -> str:
    try:
        p = urlparse(str(u or ""))
        return urlunparse((p.scheme, p.netloc, p.path, "", "", ""))
    except ValueError:
        return ""


def _host(u: str) -> str:
    try:
        return urlparse(str(u or "")).hostname or ""
    except ValueError:
        return ""


def compile_steps(events: list) -> tuple:
    """-> (body markdown, hosts). Navigations, clicks, edits and submits become numbered steps; typed values are never
    in the body; consecutive edits on one page fold into one 'fill' step; clicks on the same control twice fold."""
    steps, hosts, last = [], [], None
    fills = []

    def flush_fills():
        nonlocal fills
        if fills:
            names = ", ".join(dict.fromkeys(f for f in fills if f))
            steps.append(f"Fill in: {names} (each with its value from the task; a sign-in field is the vault's job, never typed by hand)." if names
                         else "Fill in the form's fields.")
            fills = []

    for ev in events or []:
        if not isinstance(ev, dict):
            continue
        t = str(ev.get("t") or "")
        url = _clean_url(ev.get("url") or "")
        h = _host(ev.get("url") or "")
        if h and h not in hosts:
            hosts.append(h)
        if t == "nav":
            flush_fills()
            if url and url != last:
                steps.append(f"Go to {url}")
                last = url
        elif t == "click":
            label = re.sub(r"\s+", " ", str(ev.get("label") or "")).strip()[:60]
            if ev.get("tag") in ("input",) and not label:
                continue
            flush_fills()
            line = f"Click '{label}'" if label else f"Click the {ev.get('role') or ev.get('tag') or 'control'}"
            if ev.get("href") and _clean_url(ev["href"]) and _clean_url(ev["href"]) != url:
                line += f" (opens {_clean_url(ev['href'])})"
            if steps and steps[-1] == line:
                continue
            steps.append(line)
        elif t == "edit":
            label = re.sub(r"\s+", " ", str(ev.get("label") or "")).strip()[:60]
            if str(ev.get("type") or "") == "password":
                flush_fills()
                steps.append("Sign in here (python assistant/doer/login_fill.py --url <this page>; the vault fills it).")
                continue
            if ev.get("choice"):
                flush_fills()
                steps.append(f"Choose '{str(ev['choice'])[:60]}' in '{label or 'the list'}'")
            elif ev.get("checked") is not None:
                flush_fills()
                steps.append(f"{'Tick' if ev['checked'] else 'Untick'} '{label or 'the box'}'")
            else:
                fills.append(label or "a field")
        elif t == "submit":
            flush_fills()
            label = re.sub(r"\s+", " ", str(ev.get("label") or "")).strip()[:60]
            steps.append(f"Submit the form{(' with ' + repr(label)) if label else ''}. If this sends, pays, signs or changes an account, STOP here with a checkpoint card.")
    flush_fills()
    steps = steps[:MAX_STEPS]
    if not steps:
        return "", hosts
    body = "## How it was shown\n\n" + "\n".join(f"{i}. {s}" for i, s in enumerate(steps, 1))
    body += "\n\n## Watch out\n\n- Recorded by watching an owner once; a page that looks different means the site changed: read it afresh instead of forcing a step.\n"
    body += "- Values were not recorded: take them from the task; a sign-in or a one-time code is never typed by hand.\n"
    return body, hosts


def write_skill(title: str, lane: str, body: str, hosts: list, by: str = "teach") -> dict:
    import skills
    triggers = [title.lower()[:40]] + [h.replace("www.", "")[:40] for h in hosts[:3]]
    return skills.write({"title": title, "body": body, "via": "do", "lane": lane or "personal", "triggers": triggers}, by=by)


# ------------------------------------------------------------------ record (CDP)
def _ws_connect(ws: str):
    import base64
    import socket
    u = urlparse(ws)
    sock = socket.create_connection((u.hostname, u.port or 80), timeout=8)
    crlf = chr(13) + chr(10)
    sock.sendall(crlf.join([f"GET {u.path} HTTP/1.1", f"Host: {u.hostname}:{u.port}", "Upgrade: websocket", "Connection: Upgrade",
                            f"Sec-WebSocket-Key: {base64.b64encode(os.urandom(16)).decode()}", "Sec-WebSocket-Version: 13", "", ""]).encode())
    buf = b""
    while b"\r\n\r\n" not in buf:
        chunk = sock.recv(4096)
        if not chunk:
            raise OSError("websocket closed during the handshake")
        buf += chunk
    head, buf = buf.split(b"\r\n\r\n", 1)
    if b" 101 " not in head.split(b"\r\n", 1)[0]:
        raise OSError("websocket refused")
    return sock, buf


def _ws_send(sock, obj: dict) -> None:
    payload = json.dumps(obj).encode()
    mask, n = os.urandom(4), len(payload)
    head = bytes([0x81]) + (bytes([0x80 | n]) if n < 126 else bytes([0x80 | 126]) + n.to_bytes(2, "big") if n < 65536
                            else bytes([0x80 | 127]) + n.to_bytes(8, "big"))
    sock.sendall(head + mask + bytes(b ^ mask[j % 4] for j, b in enumerate(payload)))


def _ws_recv(sock, buf: bytearray, timeout: float) -> dict | None:
    sock.settimeout(timeout)

    def need(n):
        while len(buf) < n:
            try:
                chunk = sock.recv(65536)
            except OSError:
                return None
            if not chunk:
                return None
            buf.extend(chunk)
        out = bytes(buf[:n])
        del buf[:n]
        return out
    hd = need(2)
    if hd is None:
        return None
    b0, b1 = hd
    ln = b1 & 0x7F
    if ln == 126:
        ln = int.from_bytes(need(2) or b"\0\0", "big")
    elif ln == 127:
        ln = int.from_bytes(need(8) or b"\0" * 8, "big")
    data = need(ln)
    if data is None or (b0 & 0x0F) != 1:
        return {}
    try:
        return json.loads(data.decode("utf-8", "replace"))
    except ValueError:
        return {}


def record(title: str, lane: str = "personal", seconds: int = 600, say=print) -> dict:
    """Attach to the doer's browser, watch, compile, write. -> skills.write result or {"status": "refused", ...}."""
    import browser
    try:
        STOP.unlink()
    except OSError:
        pass
    alive = browser.alive()
    if not alive:
        return {"status": "refused", "reason": "the browser is not open; open it first (python assistant/doer/browser.py open --show)"}
    page = browser.active_page()
    ws = str(page.get("webSocketDebuggerUrl") or "")
    if not ws:
        return {"status": "refused", "reason": "no page to watch"}
    events = [{"t": "nav", "url": str(page.get("url") or "")}]
    sock, rest = _ws_connect(ws)
    buf = bytearray(rest)
    try:
        for i, (m, p) in enumerate([("Page.enable", {}), ("Runtime.enable", {}), ("Runtime.addBinding", {"name": BINDING}),
                                    ("Page.addScriptToEvaluateOnNewDocument", {"source": RECORDER_JS}),
                                    ("Runtime.evaluate", {"expression": RECORDER_JS})], 1):
            _ws_send(sock, {"id": i, "method": m, "params": p})
        say(f"Recording '{title}' for up to {seconds // 60} min. Do the task in the browser; stop with: python assistant/doer/teach.py stop")
        t_end = time.time() + max(30, int(seconds))
        while time.time() < t_end and not STOP.exists() and len(events) < 400:
            msg = _ws_recv(sock, buf, 1.0)
            if not msg:
                continue
            if msg.get("method") == "Runtime.bindingCalled" and (msg.get("params") or {}).get("name") == BINDING:
                try:
                    events.append(json.loads(msg["params"].get("payload") or "{}"))
                except ValueError:
                    pass
            elif msg.get("method") == "Page.frameNavigated" and not (msg.get("params") or {}).get("frame", {}).get("parentId"):
                events.append({"t": "nav", "url": str(msg["params"]["frame"].get("url") or "")})
                _ws_send(sock, {"id": 99, "method": "Runtime.evaluate", "params": {"expression": RECORDER_JS}})
    finally:
        try:
            sock.close()
        except OSError:
            pass
        try:
            STOP.unlink()
        except OSError:
            pass
    EVENTS_DIR.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    (EVENTS_DIR / f"{stamp}.json").write_text(json.dumps(events, indent=1), encoding="utf-8")
    body, hosts = compile_steps(events)
    if not body:
        return {"status": "refused", "reason": "nothing was done in the browser while I watched", "events": len(events)}
    r = write_skill(title, lane, body, hosts)
    r["events"] = len(events)
    return r


def main() -> int:
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd")
    s = sub.add_parser("start"); s.add_argument("title"); s.add_argument("--lane", default="personal"); s.add_argument("--seconds", type=int, default=600)
    sub.add_parser("stop")
    c = sub.add_parser("compile"); c.add_argument("events")
    a = ap.parse_args()
    if a.cmd == "start":
        print(json.dumps(record(a.title, a.lane, a.seconds), ensure_ascii=False))
    elif a.cmd == "stop":
        STOP.parent.mkdir(parents=True, exist_ok=True)
        STOP.write_text("stop", encoding="utf-8")
        print("stopping")
    elif a.cmd == "compile":
        body, hosts = compile_steps(json.loads(Path(a.events).read_text(encoding="utf-8-sig")))
        print(body or "(no steps)")
        print("hosts:", ", ".join(hosts))
    else:
        print(__doc__)
        return 1
    return 0


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    sys.exit(main())

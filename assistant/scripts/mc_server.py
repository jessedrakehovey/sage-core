"""mc_server.py - Mission Control for the personal OS (roadmap Phase 4, lean port).

A stdlib HTTP server on 127.0.0.1:9019 that serves the mission-control page and lets
the owner ANSWER cards from his phone (over Tailscale Serve, tailnet-only). Zero LLM at
runtime. The only writes it performs are the ones cards.py already allows the owner to do
from the CLI: answer / undo a card, and drop a request into the operator queue.

  GET  /                      the page (dashboard.build, cached 20 s)
  GET  /desk                  the Desk page (dashboard/build.py, cached 60 s)
  GET  /comms                 the Comms board (comms_page.py over comms_state.json); /api/comms = the JSON
  GET  /api/cards             open cards (JSON)
  POST /api/cards/answer      {qid, option, text?, revised?, q?}   -> cards.answer
  POST /api/cards/undo        {qid}                                -> cards.undo
  POST /api/ask               {text}  the owner's request -> ticket (kind the owner_request)
  POST /api/ads/kill          {on}    pause / resume the ad system (creates / removes ads/KILL)
  GET  /api/exec              executor counts + queued tickets
  GET  /api/health            {ok, ts}
  GET  /contacts              upload an iPhone contacts export (.vcf) from the phone's own Safari
  POST /api/contacts/import?owner=the owner|owner   raw .vcf body -> contacts.py (replaces that owner's book)
  GET  /welcome               the first-run wizard (assistant/welcome.py); POST /api/welcome/<step>
  GET  /chat                  the chat with the household agent (assistant/agent/chat_page.py)
  GET  /api/agent/thread      ?as=the owner|owner -> that person's conversation
  POST /api/agent/say         {as, text} -> one line into the agent's inbox (the bridge answers)
  POST /gchat/events          Google Chat's HTTP connection for the Sage app (public via Funnel :8443;
                              Google-signed token verified; taps answered on the spot, messages queued)

  The owners' iPhones (phone_bridge.py; every call needs the token, header X-example-Token or ?token=;
  account = owner-iphone (default) or the owner-iphone):
  POST /api/phone/inbound     {from, text, name?, received?, account?, participants?, chat_id?, chat_name?}  an incoming text -> ticket
  GET  /api/phone/outbox?account=<a>  approved texts for THAT phone to send (each handed over ONCE)
  POST /api/phone/ack         {id}  the phone sent it

Binding is loopback only; reach it through `tailscale serve` (see README). No auth of
its own: Tailscale identity is the door, and nothing here can send or spend.
"""
from __future__ import annotations

import os
import json
import re
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import cards  # noqa: E402
import exec_store as es  # noqa: E402
import phone_bridge  # noqa: E402
import tickets  # noqa: E402
from alib import OUT, ROOT, load_json, save_json, DATA  # noqa: E402
sys.path.insert(0, str(ROOT / "agent"))
import chat_page  # noqa: E402  (the agent's chat page; runs no model, only queues a line for the bridge)

sys.path.insert(0, str(ROOT))
import orgcfg  # noqa: E402
import ports  # noqa: E402  (port guard: a taken port moves this server; readers find it in <state_home>/ports.json)

PORT = int(os.environ.get("MC_PORT") or orgcfg.get("mission_control_port", 9019) or 9019)   # per organization (sage-core): two packs on one PC
MAX_BODY = 64 * 1024


def _int(v):
    try:
        return int(v or 0)
    except (TypeError, ValueError):
        return 0
_page = {"html": "", "ts": 0.0}
_lock = threading.Lock()
# tests set WV_LOG_DIR: a test run must never write fixture lines into the live log
LOG = Path(os.environ.get("WV_LOG_DIR") or (Path.home() / ".claude")) / "mission-control-wv.log"


def _log(msg):
    try:
        with open(LOG, "a", encoding="utf-8") as f:
            f.write(time.strftime("%Y-%m-%d %H:%M:%S") + "  " + msg + "\n")
    except OSError:
        pass


_desk = {"html": "", "ts": 0.0}
NAV_CHAT = ('<div style="padding:10px 16px;font:14px Poppins,system-ui,sans-serif">'
            '<a href="/chat" style="color:#9DB392">Chat with the agent &rarr;</a></div>')
NAV_BACK = ('<div style="padding:10px 16px;font:14px Poppins,system-ui,sans-serif">'
            '<a href="/" style="color:#9DB392">&larr; Mission Control</a></div>')


def _build_desk():
    """The Desk (dashboard/build.py: the CRM strip, leads, edit bay, nagging, system) served
    at /desk so there is ONE address for both pages. Rebuilt at most once a minute."""
    with _lock:
        if time.time() - _desk["ts"] > 60:
            try:
                import importlib.util
                spec = importlib.util.spec_from_file_location("desk_build", str(ROOT.parent / "dashboard" / "build.py"))
                mod = importlib.util.module_from_spec(spec)
                spec.loader.exec_module(mod)
                out = mod.build()
                html = Path(out).read_text(encoding="utf-8")
                _desk["html"] = html.replace("<body>", "<body>" + NAV_BACK, 1)
            except Exception as e:
                _desk["html"] = f"<pre>desk build failed: {e}</pre>"
            _desk["ts"] = time.time()
        return _desk["html"]


def _build_page(force=False):
    with _lock:
        if force or time.time() - _page["ts"] > 20:
            try:
                import dashboard
                dashboard.build()
                _page["html"] = (ROOT / "web" / "index.html").read_text(encoding="utf-8").replace("<body>", "<body>" + ("" if mc_read_only() else NAV_CHAT), 1)
            except Exception as e:
                _log(f"page build failed: {type(e).__name__}: {e}")
                try:
                    import dashboard
                    _page["html"] = dashboard.safe_page(f"{type(e).__name__}: {e}")
                except Exception:
                    _page["html"] = "<pre>mission control build failed; cards are still answerable with cards.py answer</pre>"
            _page["ts"] = time.time()
        return _page["html"]


MAX_UPLOAD = 30 * 1024 * 1024

# ---------------------------------------------------------------- the organization's own pages (2026-11-01)
# config.json -> pages: {"modules": {"<name>": {"file": "<path from the org root>", "page": "<fn -> html>", "api": "<fn -> dict>",
# "nav": "<link text>"}}, "static": {"<mount>": "<folder from the org root>"}}. /<name> renders the module's page function,
# /api/<name> its api function as JSON; /<mount>/<path> serves that file from <folder> (index.html for a folder).
# The engine carries no page of any organization's own: with no `pages` key nothing here answers.
_SAFE_SEG = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,80}")
_STATIC_TYPES = {".html": "text/html", ".png": "image/png", ".webmanifest": "application/manifest+json", ".json": "application/json",
                 ".svg": "image/svg+xml", ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".css": "text/css", ".js": "text/javascript", ".ico": "image/x-icon"}
_PAGE_MODULES: dict = {}


def pages_cfg() -> dict:
    cfg = (load_json(ROOT / "config.json", {}) or {}).get("pages") or {}
    return cfg if isinstance(cfg, dict) else {}


def _page_module(name: str, spec: dict):
    import importlib.util
    f = ROOT.parent / str(spec.get("file") or "")
    key = str(f)
    mod = _PAGE_MODULES.get(key)
    if mod is None or getattr(mod, "__mtime__", 0) != f.stat().st_mtime:
        s = importlib.util.spec_from_file_location(f"page_{re.sub(r'[^a-z0-9]', '_', name.lower())}", str(f))
        mod = importlib.util.module_from_spec(s)
        s.loader.exec_module(mod)
        mod.__mtime__ = f.stat().st_mtime
        _PAGE_MODULES[key] = mod
    return mod


def _extra_page(p: str):
    """-> (code, body, ctype) for a configured page or static file, else None."""
    cfg = pages_cfg()
    mods, static = cfg.get("modules") or {}, cfg.get("static") or {}
    seg = [x for x in p.split("/") if x]
    if not seg:
        return None
    name, api = (seg[0], False) if len(seg) == 1 else ((seg[1], True) if len(seg) == 2 and seg[0] == "api" else ("", False))
    spec = mods.get(name) if name else None
    if isinstance(spec, dict) and spec.get("file"):
        try:
            mod = _page_module(name, spec)
            if api:
                fn = getattr(mod, str(spec.get("api") or "status"))
                return 200, json.dumps(fn(), ensure_ascii=False, default=str), "application/json"
            fn = getattr(mod, str(spec.get("page") or "page"))
            return 200, fn(), "text/html"
        except Exception as e:  # noqa: BLE001
            _log(f"page {name} failed: {type(e).__name__}: {e}")
            return 500, f"<pre>{name} page failed: {type(e).__name__}: {e}</pre>", "text/html"
    if seg[0] in static and len(seg) <= 8:
        rest = seg[1:]
        if any(not _SAFE_SEG.fullmatch(x) or x.startswith(".") for x in rest):
            return 404, '{"error":"not found"}', "application/json"
        f = ROOT.parent / str(static[seg[0]])
        for x in rest:
            f = f / x
        if f.is_dir() or not rest or p.endswith("/"):
            f = f / "index.html"
        if not f.is_file():
            return 404, '{"error":"not found"}', "application/json"
        return 200, f.read_bytes(), _STATIC_TYPES.get(f.suffix.lower(), "application/octet-stream")
    return None


def _parse_multipart(body: bytes, boundary: bytes):
    """-> (fields {name: text}, files [(filename, bytes)]). Small and strict: one boundary, no nesting."""
    fields, files = {}, []
    for part in body.split(b"--" + boundary):
        part = part.strip(b"\r\n")
        if not part or part == b"--":
            continue
        head, _, data = part.partition(b"\r\n\r\n")
        if data.endswith(b"\r\n"):
            data = data[:-2]
        disp = next((h for h in head.split(b"\r\n") if h.lower().startswith(b"content-disposition")), b"").decode("utf-8", "replace")
        name = re.search(r'\bname="([^"]*)"', disp)
        fname = re.search(r'\bfilename="([^"]*)"', disp)
        if fname is not None:
            if fname.group(1):
                files.append((Path(fname.group(1).replace("\\", "/")).name, data))
        elif name:
            fields[name.group(1)] = data.decode("utf-8", "replace")[:4000]
    return fields, files


_RUN_TOLD: set = set()   # (ticket, why) already said in the card's thread by this server


def _hm(ts) -> str:
    try:
        return time.strftime("%I:%M %p", time.strptime(str(ts)[:19], "%Y-%m-%dT%H:%M:%S")).lstrip("0")
    except ValueError:
        return ""


def phone_run_why(ticket: str = "", items=None) -> tuple[str, str]:
    """Why a tap on "Send now from phone" would send nothing for this text, as (reason, plain line); ("", "") when
    the shortcut will pick it up. The shortcut only takes texts still waiting, and never in quiet hours: the owner tapped
    at 6:31 AM and on a text the phone had already picked up (2026-11-06/07), and the page said nothing."""
    items = phone_bridge.load_json(phone_bridge.OUTBOX, {"items": []})["items"] if items is None else items
    it = next((i for i in items if ticket and i.get("ticket") == ticket), None)
    st = str((it or {}).get("status") or "")
    if st == "sent":
        return "sent", f"That text is confirmed sent{' at ' + _hm(it.get('ts_sent')) if it.get('ts_sent') else ''}."
    if st == "handed":
        return "handed", (f"Your phone already picked that text up at {_hm(it.get('ts_handed'))}, so the shortcut won't take it again. "
                          "If it never went out, say \"send it again\" under the card.")
    if st in ("expired", "recalled"):
        return st, "That text was not sent and is no longer waiting. Say \"send it again\" under the card to send it."
    if phone_bridge.held_by_quiet(it):
        return "quiet", (f"It's quiet hours ({es.QUIET_START - 12} PM to {es.QUIET_END} AM), so the phone isn't given texts now. "
                         f"Tap the button again after {es.QUIET_END} AM.")
    return "", ""


def phone_run_page(ticket: str = "") -> str:
    """The page behind "Send now from phone": a big tap target that runs the phone's send shortcut, and, when the
    shortcut would send nothing for this card's text, why in one plain line (also said once in the card's thread)."""
    import html as _html
    from urllib.parse import quote
    name = str(orgcfg.get("phone_send_shortcut", "") or "Send Texts")
    url = "shortcuts://run-shortcut?name=" + quote(name)
    n = _html.escape(name)
    why, line = ("", "")
    try:
        why, line = phone_run_why(ticket)
        if why and why != "sent" and (ticket, why) not in _RUN_TOLD:
            _RUN_TOLD.add((ticket, why))
            phone_bridge._say({"ticket": ticket}, line)
    except Exception as e:  # noqa: BLE001  (the page always loads)
        _log(f"phone run page state failed: {e}")
    note = (f"<p id='why' style='padding:12px;border-radius:12px;background:#3F503A'>{_html.escape(line)}</p>" if line else "")
    return ("<!doctype html><html><head><meta charset='utf-8'><meta name='viewport' content='width=device-width,initial-scale=1'>"
            "<title>Send now from phone</title></head><body style='font-family:sans-serif;background:#050B07;color:#FBFFFA;padding:24px'>"
            + note +
            f"<p><a id='go' href='{_html.escape(url)}' style='display:block;padding:18px;border-radius:12px;background:#4A5C43;"
            f"color:#FBFFFA;text-align:center;font-size:20px;text-decoration:none'>Tap to send the waiting texts</a></p>"
            f"<p>This runs the “{n}” shortcut on this iPhone, which picks up the approved texts and sends them.</p>"
            f"<p>If nothing happens: open this page in Safari (the share button, then Open in Safari), or open the Shortcuts app "
            f"and tap “{n}”. It only works on the iPhone that has that shortcut.</p>"
            "</body></html>")


class H(BaseHTTPRequestHandler):
    server_version = "sage-mc/1"

    def _send(self, code, body, ctype="application/json"):
        data = body.encode("utf-8") if isinstance(body, str) else body
        self.send_response(code)
        self.send_header("Content-Type", ctype + "; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def _json(self):
        """The request body as a dict. Anything else (bad length, oversized, not JSON, not an
        object) is an empty dict, so a handler never crashes on body.get()."""
        try:
            n = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            return {}
        if n <= 0 or n > MAX_BODY:
            return {}
        try:
            obj = json.loads(self.rfile.read(n).decode("utf-8", errors="replace") or "{}")
        except ValueError:
            return {}
        return obj if isinstance(obj, dict) else {}

    def log_message(self, fmt, *args):  # quiet console; file log for API calls only
        if "/api/" in str(args[0] if args else ""):
            # never write the phone token (?token=...) into the log
            _log(re.sub(r"([?&]token=)[^&\s\"]+", r"\1***", fmt % args))

    def _same_site(self):
        """Card answers and asks must come from this page: a JSON content type (a cross-site
        form cannot send one without a preflight this server never answers) and, when the
        browser sends an Origin, one that matches the Host it is talking to."""
        if "application/json" not in (self.headers.get("Content-Type") or "").lower():
            return False
        origin = self.headers.get("Origin")
        if origin and origin.split("://", 1)[-1].rstrip("/") != (self.headers.get("X-Forwarded-Host") or self.headers.get("Host") or ""):
            return False
        return True

    def _contacts_import(self):
        """The /contacts page's upload: a raw .vcf body (too big for _json). Same-site only: a
        custom header a cross-site form cannot send, and a matching Origin when one is sent."""
        import contacts
        from urllib.parse import parse_qs, urlparse
        origin = self.headers.get("Origin")
        if self.headers.get("X-example-Contacts") != "1" or (
                origin and origin.split("://", 1)[-1].rstrip("/") != (self.headers.get("X-Forwarded-Host") or self.headers.get("Host") or "")):
            return self._send(403, '{"ok":false,"reason":"cross-site request refused"}')
        try:
            n = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            n = 0
        if n <= 0 or n > contacts.MAX_VCF:
            return self._send(400, '{"ok":false,"reason":"empty or too large"}')
        text = self.rfile.read(n).decode("utf-8-sig", errors="replace")
        owner = (parse_qs(urlparse(self.path).query).get("owner") or ["owner1"])[0]
        r = contacts.import_text(text, owner, source="mission-control upload")
        _log(f"contacts import owner={owner} by {self._who()} ok={r.get('ok')} count={r.get('count')}")
        return self._send(200 if r.get("ok") else 400, json.dumps(r))

    def _upload(self):
        """POST /api/agent/upload (multipart: as, text, file...): the chat paperclip. Each file goes
        into the library (agent/resources.py -> Raw/Uploads) and ONE line is queued for the bridge,
        so the agent reads the file and answers in the same turn. Same-origin only (an Origin that
        does not match the Host is refused); the tailnet login is logged with it."""
        ctype = self.headers.get("Content-Type") or ""
        origin = self.headers.get("Origin")
        if origin and origin.split("://", 1)[-1].rstrip("/") != (self.headers.get("X-Forwarded-Host") or self.headers.get("Host") or ""):
            return self._send(403, '{"ok":false,"reason":"cross-site upload refused"}')
        m = re.search(r'boundary="?([^";]+)"?', ctype)
        try:
            n = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            n = 0
        if not m or n <= 0 or n > MAX_UPLOAD:
            return self._send(400, json.dumps({"ok": False, "reason": f"bad upload (multipart, under {MAX_UPLOAD // (1024 * 1024)} MB)"}))
        fields, files = _parse_multipart(self.rfile.read(n), m.group(1).encode())
        import resources
        key, text = fields.get("as", ""), fields.get("text", "")
        person = chat_page.core.person_for(handle=key)
        if not person or not chat_page.core.can(person, "ask"):
            return self._send(403, '{"ok":false,"reason":"unknown person"}')
        kept, lines = [], []
        for fname, data in files[:6]:
            rec = resources.save(fname, data, person["name"], note=text, via="mission-control")
            lines.append(resources.describe(rec, person["name"]))
            if rec.get("ok"):
                kept.append(rec["rel"])
        if not lines:
            return self._send(400, '{"ok":false,"reason":"no file"}')
        msg = (text.strip() + "\n" if text.strip() else "") + "\n".join(lines)
        r = chat_page.say(key, msg, self._who())
        _log(f"agent upload as={key} by {self._who()} kept={len(kept)} of {len(files)}")
        return self._send(200 if r.get("ok") else 400, json.dumps({**r, "kept": kept}, ensure_ascii=False))

    def _who(self):
        """Tailscale Serve stamps the tailnet login on every proxied request; it records WHICH
        of the owner / owner tapped a card (either may approve, the owner 2026-10-01)."""
        return (self.headers.get("Tailscale-User-Login") or "local").strip()[:80]

    def _client_gone(self) -> bool:
        """True when the caller has closed its side of the connection (a readable socket that reads empty)."""
        try:
            import select
            import socket
            r, _, _ = select.select([self.connection], [], [], 0)
            return bool(r) and self.connection.recv(1, socket.MSG_PEEK) == b""
        except Exception:  # noqa: BLE001
            return True

    def _phone_ok(self, body=None):
        from urllib.parse import parse_qs, urlparse
        tok = self.headers.get(phone_bridge.TOKEN_HEADER) or (parse_qs(urlparse(self.path).query).get("token") or [""])[0]             or str((body or {}).get("token") or "")
        return phone_bridge.check_token(tok)

    def do_GET(self):
        p = self.path.split("?")[0]
        if p == "/gchat/ping":        # public reachability probe for the Funnel path (logs every hit)
            _log(f"gchat ping from {self.headers.get('X-Forwarded-For') or self.client_address[0]} ua={self.headers.get('User-Agent', '')[:60]}")
            return self._send(200, '{"ok":true,"who":"example-mc gchat"}')
        if p == "/api/phone/outbox":
            if not self._phone_ok():
                tok = self.headers.get(phone_bridge.TOKEN_HEADER)
                # never the value: length + shape only, enough to spot a stray space or a wrong key name
                _log(f"phone outbox 403: header_present={tok is not None} len={len(tok or '')} "
                     f"stripped_len={len((tok or '').strip())} other_token_headers={[h for h in self.headers.keys() if 'token' in h.lower()]}")
                return self._send(403, '{"error":"bad token"}')
            from urllib.parse import parse_qs, urlparse
            acct = (parse_qs(urlparse(self.path).query).get("account") or [phone_bridge.DEFAULT_ACCOUNT])[0]
            try:   # a text approved seconds ago is still in its undo window: hold this answer until it is out
                #    (phone_bridge.wait_for_release; the phone now asks when its owner leaves the chat app)
                waited = phone_bridge.wait_for_release(acct)
                if waited:
                    _log(f"phone outbox ({acct}) held {waited:.0f}s for a text in its undo window")
                    if self._client_gone():
                        # the phone gave up while we waited: hand nothing over (an item marked handed to a closed
                        # connection would never be sent, and is never offered twice); the next fetch takes it
                        _log(f"phone outbox ({acct}): the phone hung up during the wait; nothing handed")
                        return
            except Exception as e:
                _log(f"phone fetch wait failed: {e}")
            try:   # release approved texts whose undo window has passed, then hand them over
                import drain as _drain
                _drain.drain(lane="phone", min_age_s=cards.UNDO_S + 5)
            except Exception as e:
                _log(f"phone drain failed: {e}")
            items = phone_bridge.fetch_outbox(acct)
            if items:
                _log(f"phone outbox ({acct}) handed {len(items)}: " + ",".join(i["id"] for i in items))
            resp = {"items": items, "count": len(items)}
            if phone_bridge._quiet() and not items:
                resp["held"] = "quiet hours (21:00-07:00): nothing is sent now; a text still waiting in the morning comes back as a card instead of going out late"
            return self._send(200, json.dumps(resp, ensure_ascii=False))
        if p == "/phone/run":
            # the "Send now from phone" button (Chat card / page). A bare 302 to shortcuts:// did nothing
            # (2026-11-06): Chat's in-app browser drops a redirect to a custom scheme. So this is a page
            # with a link the owner taps (a tap is allowed to open Shortcuts) and, if even that fails,
            # the plain way to do it by hand. Nothing here sends or changes state; ?t=<ticket> (the card's text) adds
            # why a tap would send nothing for it (quiet hours, already picked up), said once in the card's thread too.
            from urllib.parse import parse_qs, urlparse
            t = (parse_qs(urlparse(self.path).query).get("t") or [""])[0][:80]
            return self._send(200, phone_run_page(t), "text/html")
        if p in ("/", "/index.html"):
            return self._send(200, _build_page(), "text/html")
        if p in ("/chat", "/chat/"):
            if mc_read_only():
                return self._send(200, _chat_only_page("Talk to the agent in Google Chat", "This page no longer takes messages: everything you type goes through Google Chat."), "text/html")
            return self._send(200, chat_page.page(chat_page.who_for(self._who())), "text/html")
        if p == "/setup/plaid" or p.startswith("/api/setup/plaid/"):
            # connect Relay through Plaid from the phone (scripts/money/plaid_wizard.py): keys to Credential
            # Manager, Plaid Link on the phone, read-only by the adapter's allow-list
            from urllib.parse import parse_qs, urlparse
            sys.path.insert(0, str(HERE / "money"))
            import plaid_wizard
            q = {k: v[0] for k, v in parse_qs(urlparse(self.path).query).items()}
            r = plaid_wizard.handle("GET", p, q)
            if r:
                return self._send(r[0], r[1], r[2])
        if p in ("/welcome", "/welcome/", "/api/welcome/state"):
            # the first-run wizard (assistant/welcome.py): the doctor table, the Claude token window, the Google
            # clicks and the start button, revisitable from the phone once the organization exists
            sys.path.insert(0, str(ROOT))
            import welcome
            code, out, ctype = welcome.handle("GET", "/welcome" if p.startswith("/welcome") else p, {})
            return self._send(code, out, ctype)
        if p in ("/setup", "/setup/"):
            # connections and logins, one button each (setup_actions.py); tailnet-only like every page here
            import setup_page
            import org
            name = chat_page.core.profile().get("name", "Agent")
            return self._send(200, setup_page.page(name, org.status_text()), "text/html")
        if p == "/api/setup":
            import setup_actions
            return self._send(200, json.dumps({"rows": setup_actions.rows()}, ensure_ascii=False))
        if p == "/api/setup/shot":
            import setup_actions
            r = setup_actions.run("", "shot", by=self._who())
            f = Path(r.get("path", "")) if r.get("ok") else None
            if not f or not f.is_file():
                return self._send(200, json.dumps(r, ensure_ascii=False))
            data = f.read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", "image/png")
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            return self.wfile.write(data)
        if p == "/api/agent/thread":
            from urllib.parse import parse_qs, urlparse
            key = (parse_qs(urlparse(self.path).query).get("as") or [""])[0]
            r = chat_page.thread(key)
            return self._send(200 if r.get("ok") else 400, json.dumps(r, ensure_ascii=False))
        if p in ("/desk", "/desk/"):
            return self._send(200, _build_desk(), "text/html")
        if p in ("/contacts", "/contacts/"):
            if mc_read_only():
                return self._send(200, _chat_only_page("Contacts come in through Google Chat", "Share your contacts export (.vcf) with the agent in Google Chat; it imports them into your phone book there."), "text/html")
            import contacts
            return self._send(200, _contacts_page(contacts.load(), self._who()), "text/html")
        if p in ("/comms", "/comms/"):
            import comms_page
            return self._send(200, comms_page.page(), "text/html")
        extra = _extra_page(p)                # the organization's own pages and static mounts (config.json -> pages)
        if extra is not None:
            return self._send(*extra)
        if p == "/api/comms":
            return self._send(200, json.dumps(load_json(DATA / "comms_state.json", {}), ensure_ascii=False))
        if p.startswith("/api/doer/shot/"):
            # the doer's review-page PNG for a checkpoint card: assistant/out/doer/<ticket>/<name> only
            m = re.fullmatch(r"/api/doer/shot/([0-9a-f]{16})/([A-Za-z0-9._-]+\.(?:png|jpe?g))", p)
            f = (OUT / "doer" / m.group(1) / m.group(2)) if m else None
            if not f or not f.is_file():
                return self._send(404, '{"error":"no such screenshot"}')
            data = f.read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", "image/png" if f.suffix == ".png" else "image/jpeg")
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "private, max-age=600")
            self.end_headers()
            return self.wfile.write(data)
        if p == "/api/cards":
            return self._send(200, json.dumps(cards.pending(), ensure_ascii=False))
        if p == "/api/exec":
            return self._send(200, json.dumps({"counts": es.counts(), "queued": es.pending()}, ensure_ascii=False))
        if p == "/api/health":
            return self._send(200, json.dumps({"ok": True, "ts": time.strftime("%Y-%m-%dT%H:%M:%S"), "pack": ports.slug(), "pid": os.getpid(), "port": PORT,
                                               "open_cards": len(cards.pending()), "queue": tickets.pending()}))
        return self._send(404, '{"error":"not found"}')

    def do_POST(self):
        try:
            return self._post()
        except Exception as e:                      # the phone gets an answer, never a dropped connection
            _log(f"POST {self.path.split('?')[0]} failed: {type(e).__name__}: {e}")
            return self._send(500, json.dumps({"ok": False, "reason": f"server error: {type(e).__name__}"}))

    def _post(self):
        p = self.path.split("?")[0]
        if p in ENTRY_POSTS and mc_read_only():
            # nothing is typed into Mission Control (config.json -> mission_control.read_only, the owner 2026-11-01)
            _log(f"refused {p} from {self._who()}: Mission Control is read-only, data entry is in Google Chat")
            return self._send(410, json.dumps({"ok": False, "reason": CHAT_ONLY}))
        if p == "/api/contacts/import":
            return self._contacts_import()
        if p == "/api/agent/upload":
            return self._upload()
        body = self._json()
        if p == "/gchat/events":
            # Google Chat's HTTP connection (through Tailscale Funnel :8443, the only public path).
            # Google signs every event; nothing happens before that token verifies. Button taps
            # are answered synchronously (gchat_events.py); messages are queued for the bridge.
            import gchat_events
            via_relay = gchat_events.verify_relay(self.headers.get("X-Sage-Relay", ""), gchat_events.relay_secret())
            if not via_relay and not gchat_events.verify_bearer(self.headers.get("Authorization", ""), gchat_events.audiences()):
                _log(f"gchat event refused: bad or missing Google token; unverified claims={gchat_events.peek_claims(self.headers.get('Authorization', ''))} expected aud in {gchat_events.audiences()}")
                return self._send(401, '{"error":"unauthorized"}')
            late = None
            if via_relay and self.headers.get("X-Sage-Late"):
                # a tap or message the relay held while this server was down (agent/gchat_relay/Code.gs
                # flushQueue): epoch ms it was queued at; the outcome goes to Chat by REST
                try:
                    late = float(self.headers.get("X-Sage-Late")) / 1000.0
                except ValueError:
                    late = 0.0
            _log(f"gchat event via {'relay' if via_relay else 'direct'}{' (held ' + str(int(time.time() - late)) + 's)' if late else ''}: keys={sorted(body.keys())[:6]}")
            return self._send(200, json.dumps(gchat_events.handle(body, _log, late), ensure_ascii=False))
        if p.startswith("/api/phone/"):
            if not self._phone_ok(body):
                return self._send(403, '{"error":"bad token"}')
            if p == "/api/phone/inbound":
                # which fields the phone actually filled (lengths only, never the text): the
                # Shortcut's token wiring is the usual fault when this returns 400
                _log("phone inbound fields: " + ", ".join(f"{k}={len(str(body.get(k) or ''))}" for k in ("from", "sender", "name", "text", "content", "account", "participants", "chat_id")))
                if (DATA / "phone_debug_once").exists():          # one-shot raw dump while wiring a new phone; delete the marker to stop
                    _log("phone inbound RAW: " + json.dumps(body, ensure_ascii=False)[:400])
                r = phone_bridge.inbound(body.get("from") or body.get("sender") or "", body.get("text") or body.get("content") or "",
                                         str(body.get("name") or ""), str(body.get("received") or ""),
                                         account=str(body.get("account") or phone_bridge.DEFAULT_ACCOUNT),
                                         # a group chat, when the Shortcut can say so (the Message trigger gives only sender + text today)
                                         participants=body.get("participants") or body.get("recipients"),
                                         chat_id=str(body.get("chat_id") or body.get("chat") or ""),
                                         chat_name=str(body.get("chat_name") or ""))
                try:   # a reply proves our unconfirmed text to them went out: mark it sent, close its card
                    import text_confirm
                    tc = text_confirm.run()
                    if tc["confirmed"] or tc["cards_closed"]:
                        _log(f"text confirmed by reply: {tc}")
                except Exception as e:  # noqa: BLE001
                    _log(f"text_confirm failed: {e}")
                return self._send(200 if r.get("ok") else 400, json.dumps(r, ensure_ascii=False))
            if p == "/api/phone/ack":
                return self._send(200, json.dumps(phone_bridge.ack(str(body.get("id") or "")), ensure_ascii=False))
            return self._send(404, '{"error":"not found"}')
        if not self._same_site():
            return self._send(403, '{"ok":false,"reason":"cross-site or non-JSON request refused"}')
        if p == "/api/agent/say":
            r = chat_page.say(str(body.get("as") or ""), str(body.get("text") or ""), self._who())
            _log(f"agent say as={body.get('as')} by {self._who()} ok={r.get('ok')}")
            return self._send(200 if r.get("ok") else 400, json.dumps(r, ensure_ascii=False))
        if p.startswith("/api/setup/plaid/"):
            sys.path.insert(0, str(HERE / "money"))
            import plaid_wizard
            r = plaid_wizard.handle("POST", p, body)
            if r:
                _log(f"plaid wizard {p.rsplit('/', 1)[-1]} by {self._who()}: {r[1][:60] if 'secret' not in r[1] else 'ok'}")
                return self._send(r[0], r[1], r[2])
        if p.startswith("/api/welcome/"):
            # a wizard step (welcome.py): doctor, the token window, the probe, start. Never a secret through this page.
            sys.path.insert(0, str(ROOT))
            import welcome
            code, out, ctype = welcome.handle("POST", p, body if isinstance(body, dict) else {})
            _log(f"welcome {p.rsplit('/', 1)[-1]} by {self._who()}: {out[:80]}")
            return self._send(code, out, ctype)
        if p == "/api/setup/run":
            # a Setup button: runs one connect / check step on the PC (setup_actions.py). Never a secret, never a send.
            import setup_actions
            rid, action = str(body.get("id") or ""), str(body.get("action") or "")
            if not rid:
                return self._send(400, '{"ok":false,"message":"id required"}')
            r = setup_actions.run(rid, action, by=self._who())
            _log(f"setup run id={rid} action={action or 'step'} by {self._who()} ok={r.get('ok')}")
            return self._send(200, json.dumps(r, ensure_ascii=False))
        if p == "/api/cards/answer":
            qid, option = str(body.get("qid") or ""), str(body.get("option") or "")
            if not qid or not option:
                return self._send(400, '{"ok":false,"reason":"qid and option required"}')
            if mc_read_only() and (str(body.get("text") or "").strip() or str(body.get("revised") or "").strip()):
                return self._send(410, json.dumps({"ok": False, "reason": CHAT_ONLY}))   # a tap is fine; typed words go through Chat
            r = cards.answer(qid, option, str(body.get("text") or ""), str(body.get("revised") or ""),
                             _int(body.get("q")), by="mc:" + self._who())
            _build_page(force=True)
            return self._send(200, json.dumps(r, ensure_ascii=False))
        if p == "/api/cards/undo":
            r = cards.undo(str(body.get("qid") or ""))
            _build_page(force=True)
            return self._send(200, json.dumps(r, ensure_ascii=False))
        if p == "/api/ads/kill":
            # pause / resume the ad system (ads/KILL). Never spends: budget moves only come from an approved card.
            sys.path.insert(0, str(ROOT.parent / "ads" / "scripts"))
            import panel as _ads_panel
            r = _ads_panel.set_kill(bool(body.get("on")), self._who())
            _log(f"ads kill -> {r['killed']} by {self._who()}")
            _build_page(force=True)
            return self._send(200, json.dumps(r))
        if p == "/api/ask":
            text = str(body.get("text") or "").strip()
            if not text:
                return self._send(400, '{"ok":false,"reason":"empty"}')
            added = tickets.add_many([{"source": "owner1", "source_id": f"{time.time():.0f}|{text[:40]}", "lane": "personal",
                                       "kind": "owner1_request", "title": f"owner1 asked: {text[:120]}",
                                       "summary": text[:600], "payload": {"text": text, "via": "mission-control"}}], "mc")
            return self._send(200, json.dumps({"ok": added > 0, "queued": added}))
        return self._send(404, '{"error":"not found"}')


ENTRY_POSTS = ("/api/contacts/import", "/api/agent/upload", "/api/agent/say", "/api/ask")
CHAT_ONLY = "Mission Control does not take typing any more: say it to the agent in Google Chat."


def mc_read_only() -> bool:
    try:
        import alib
        return alib.mc_read_only()
    except Exception:  # noqa: BLE001
        return False


def _chat_only_page(title: str, line: str) -> str:
    from html import escape
    return (f'<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">'
            f'<title>{escape(title)}</title><style>body{{margin:0;background:#050B07;color:#FBFFFA;font:16px/1.5 Poppins,system-ui,sans-serif;padding:32px 16px}}'
            f'main{{max-width:520px;margin:0 auto}}h1{{font:500 24px "Playfair Display",Georgia,serif}}a{{color:#9DB392}}</style></head><body><main>'
            f'<h1>{escape(title)}</h1><p>{escape(line)}</p><p><a href="/">Back to Mission Control</a></p></main></body></html>')


def _contacts_page(db, who):
    from html import escape
    me = "owner6" if "owner6" in who.lower() else "owner1"
    books = "".join(f"<li>{escape(o.title())}: {b['count']} contacts, imported {escape(b['imported'][:16].replace('T', ' '))}</li>"
                    for o, b in db.get("books", {}).items()) or "<li>Nothing imported yet.</li>"
    opts = "".join(f'<option value="{o}"{" selected" if o == me else ""}>{o.title()}</option>' for o in ("owner1", "owner6"))
    return f"""<!doctype html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Contacts</title><style>
body{{margin:0;background:#050B07;color:#FBFFFA;font:16px Poppins,system-ui,sans-serif}}
main{{max-width:560px;margin:0 auto;padding:16px}} h1{{font-family:'Playfair Display',Georgia,serif;font-weight:600}}
a{{color:#9DB392}} ol li{{margin:6px 0}} .box{{background:#0f1a12;border:1px solid #3F503A;border-radius:12px;padding:14px;margin:14px 0}}
button,select,input{{font:inherit}} button{{background:#4A5C43;color:#FBFFFA;border:0;border-radius:10px;padding:12px 18px;width:100%}}
#msg{{margin-top:12px;white-space:pre-wrap}}</style></head><body>{NAV_BACK}<main>
<h1>Phone contacts</h1>
<p>Teach the assistant who everyone is. Each upload replaces that phone's list.</p>
<div class="box"><b>On the iPhone</b><ol>
<li>Open <b>Contacts</b>, tap <b>Lists</b> (top left).</li>
<li>Press and hold <b>All Contacts</b> (or <b>All iCloud</b>), tap <b>Export</b>, then <b>Save to Files</b>.</li>
<li>Come back here, choose the file below, tap Upload.</li></ol></div>
<div class="box"><label>Whose phone <select id="owner">{opts}</select></label><br><br>
<input type="file" id="f" accept=".vcf,text/vcard,text/x-vcard"><br><br>
<button id="go">Upload contacts</button><div id="msg"></div></div>
<div class="box"><b>Imported so far</b><ul>{books}</ul></div>
</main><script>
document.getElementById('go').onclick = async () => {{
  const f = document.getElementById('f').files[0], msg = document.getElementById('msg');
  if (!f) {{ msg.textContent = 'Choose the .vcf file first.'; return; }}
  msg.textContent = 'Uploading ' + f.name + '...';
  try {{
    const r = await fetch('/api/contacts/import?owner=' + document.getElementById('owner').value,
      {{method: 'POST', headers: {{'Content-Type': 'text/vcard', 'X-example-Contacts': '1'}}, body: await f.text()}});
    const j = await r.json();
    msg.textContent = j.ok ? ('Imported ' + j.count + ' contacts (' + j.with_phone + ' with a phone number). The wiki is updated.')
                           : ('Not imported: ' + (j.reason || r.status));
  }} catch (e) {{ msg.textContent = 'Upload failed: ' + e; }}
}};
</script></body></html>"""


def _watch_code(srv, started: float) -> None:
    """Exit when the server's own code (or the Chat event / card modules it imports) changes on
    disk, so the spine's keepalive brings it back on the new code within 10 min. 2026-10-07: the
    server ran the morning's code through three fixes, including the one for repeated Chat answers."""
    import threading
    here = Path(__file__).resolve().parent
    files = [here / "mc_server.py", here / "gchat_events.py", here / "cards.py", here / "chat_push.py",
             here.parent / "agent" / "gchat.py", here.parent / "agent" / "core.py"]

    def loaded():
        # plus every script module it has imported since (2026-10-11: the texts path - phone_bridge,
        # tickets, text_gate - ran a day-old gate, so owner's texts kept making cards)
        mods = [Path(getattr(m, "__file__", "") or "") for m in list(sys.modules.values())]
        return files + [m for m in mods if m.suffix == ".py" and m.resolve().parent == here]

    def run():
        # restart only once the code has been still for settle_s: a work session saving a file five
        # times in four minutes meant five restarts, each a moment where a Chat tap found nobody home
        # (2026-10-18 17:47-17:51; the owner 2026-10-19 "Sage unable to process request on approvals")
        settle_s = 90
        while True:
            time.sleep(30)
            try:
                newest = max((p.stat().st_mtime for p in loaded() if p.exists()), default=0)
                if newest > started + 2 and time.time() - newest >= settle_s:
                    _log("code changed on disk; restarting myself on the new code")
                    for t in [t for t in threading.enumerate() if t.name.startswith("tap-")]:
                        t.join(15)                  # a tap still finishing on its own thread (gchat_events) ends first
                    RESTART["wanted"] = True
                    srv.shutdown()
                    return
            except OSError:
                pass
    threading.Thread(target=run, daemon=True).start()


RESTART = {"wanted": False}


def _respawn():
    """Start a fresh server process on the new code the moment this one has let go of the port.
    Until 2026-10-18 the server only exited and waited for the spine's 10-minute keepalive: a tap in
    Chat during that gap got "Sage is unable to process your request" and was lost (the owner, 09:21)."""
    import subprocess
    exe = Path(sys.executable)
    pyw = exe.with_name("pythonw.exe")
    flags = 0x08000000 | 0x00000200 if os.name == "nt" else 0          # CREATE_NO_WINDOW | CREATE_NEW_PROCESS_GROUP; never DETACHED_PROCESS: a venv launcher loses its console and the real python opens a visible one
    try:
        subprocess.Popen([str(pyw if pyw.exists() else exe), str(Path(__file__).resolve())], cwd=str(HERE.parent.parent),
                         creationflags=flags, close_fds=True, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        _log("respawned on the new code")
    except OSError as e:
        _log(f"respawn failed ({e}); the keepalive brings me back within 10 min")


def main():
    global PORT
    # the port guard (ports.py, 2026-10-31): our own previous instance still letting go -> wait for it; still ours after
    # that -> already up, leave quietly; anything else on the port -> the next free one, written to the ledger for every reader
    port, how = ports.claim("mission_control", PORT, wait_s=8.0)
    if how == "ours":
        _log(f"mission control is already up on 127.0.0.1:{PORT}; this start is not needed")
        print(f"mission control already on http://127.0.0.1:{PORT}")
        return
    if how == "moved":
        _log(f"port {PORT} is held by {ports.holder(PORT)}; mission control moves to {port}")
    PORT = port
    srv = ThreadingHTTPServer(("127.0.0.1", PORT), H)
    _log(f"=== mission control up on 127.0.0.1:{PORT} ===")
    print(f"mission control on http://127.0.0.1:{PORT}")
    _watch_code(srv, time.time())
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    if RESTART["wanted"]:
        srv.server_close()                              # the port is free before the replacement binds it
        _respawn()


if __name__ == "__main__":
    main()

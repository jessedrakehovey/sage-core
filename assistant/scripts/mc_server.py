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
  POST /api/phone/inbound     {from, text, name?, received?, account?}  an incoming text -> ticket
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
                _page["html"] = (ROOT / "web" / "index.html").read_text(encoding="utf-8").replace("<body>", "<body>" + NAV_CHAT, 1)
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
            try:   # release approved texts whose undo window has passed, then hand them over
                import drain as _drain
                _drain.drain(lane="phone", min_age_s=cards.UNDO_S + 5)
            except Exception as e:
                _log(f"phone drain failed: {e}")
            from urllib.parse import parse_qs, urlparse
            acct = (parse_qs(urlparse(self.path).query).get("account") or [phone_bridge.DEFAULT_ACCOUNT])[0]
            items = phone_bridge.fetch_outbox(acct)
            if items:
                _log(f"phone outbox ({acct}) handed {len(items)}: " + ",".join(i["id"] for i in items))
            resp = {"items": items, "count": len(items)}
            if phone_bridge._quiet():
                resp["held"] = "quiet hours (21:00-07:00): nothing is sent now; a text still waiting in the morning comes back as a card instead of going out late"
            return self._send(200, json.dumps(resp, ensure_ascii=False))
        if p == "/phone/run":
            # the "Send now from phone" button (Chat card / page): bounce to the Shortcuts URL scheme,
            # which runs the phone's own fetch-and-send shortcut. Nothing here sends or changes state.
            self.send_response(302)
            self.send_header("Location", "shortcuts://run-shortcut?name=example%20Send%20Texts")
            self.send_header("Content-Length", "0")
            self.end_headers()
            return None
        if p in ("/", "/index.html"):
            return self._send(200, _build_page(), "text/html")
        if p in ("/chat", "/chat/"):
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
            import contacts
            return self._send(200, _contacts_page(contacts.load(), self._who()), "text/html")
        if p in ("/comms", "/comms/"):
            import comms_page
            return self._send(200, comms_page.page(), "text/html")
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
            return self._send(200, json.dumps({"ok": True, "ts": time.strftime("%Y-%m-%dT%H:%M:%S"),
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
                _log("phone inbound fields: " + ", ".join(f"{k}={len(str(body.get(k) or ''))}" for k in ("from", "sender", "name", "text", "content", "account")))
                if (DATA / "phone_debug_once").exists():          # one-shot raw dump while wiring a new phone; delete the marker to stop
                    _log("phone inbound RAW: " + json.dumps(body, ensure_ascii=False)[:400])
                r = phone_bridge.inbound(body.get("from") or body.get("sender") or "", body.get("text") or body.get("content") or "",
                                         str(body.get("name") or ""), str(body.get("received") or ""),
                                         account=str(body.get("account") or phone_bridge.DEFAULT_ACCOUNT))
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
    flags = 0x00000008 | 0x00000200 if os.name == "nt" else 0          # DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP
    try:
        subprocess.Popen([str(pyw if pyw.exists() else exe), str(Path(__file__).resolve())], cwd=str(HERE.parent.parent),
                         creationflags=flags, close_fds=True, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        _log("respawned on the new code")
    except OSError as e:
        _log(f"respawn failed ({e}); the keepalive brings me back within 10 min")


def main():
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

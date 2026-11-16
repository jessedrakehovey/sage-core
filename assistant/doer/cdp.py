"""cdp.py - the engine's own line to a Chromium browser: the DevTools protocol over one websocket, standard library
only (2026-11-15). No Playwright, no Node, nothing to install.

    c = Conn(ws_url)                                    # the browser's webSocketDebuggerUrl (http://host:port/json/version)
    sid = c.send("Target.attachToTarget", {"targetId": tid, "flatten": True})["sessionId"]
    c.send("Page.navigate", {"url": "https://example.com"}, session=sid, timeout=60)
    c.on(lambda session, method, params: ...)           # every event, on the reader thread: record, never call send()

Every send() has a time limit and raises Timeout when the browser does not answer, so nothing built on this can hang
for good: the caller always gets its thread back and can decide what to do about a page that is stuck.
One reader thread owns the socket's incoming side (frames, fragments, pings); send() may be called from any thread.
"""
from __future__ import annotations

import base64
import json
import os
import socket
import struct
import threading
import time
from urllib.parse import urlparse


class CDPError(Exception):
    """The browser answered with an error."""


class Timeout(CDPError):
    """The browser did not answer in time."""


class Closed(CDPError):
    """The connection is gone (the browser quit, or the socket dropped)."""


class Conn:
    def __init__(self, ws_url: str, connect_timeout: float = 10.0):
        u = urlparse(ws_url)
        self.url = ws_url
        self.sock = socket.create_connection((u.hostname, u.port or 80), timeout=connect_timeout)
        key = base64.b64encode(os.urandom(16)).decode()
        crlf = chr(13) + chr(10)
        self.sock.sendall(crlf.join([f"GET {u.path} HTTP/1.1", f"Host: {u.hostname}:{u.port}", "Upgrade: websocket", "Connection: Upgrade",
                                     f"Sec-WebSocket-Key: {key}", "Sec-WebSocket-Version: 13", "", ""]).encode())
        head = b""
        end = (crlf + crlf).encode()
        while end not in head:
            chunk = self.sock.recv(4096)
            if not chunk:
                raise Closed("the browser closed the connection during the handshake")
            head += chunk
        head, rest = head.split(end, 1)
        if b" 101 " not in head.split(crlf.encode(), 1)[0]:
            raise CDPError("the browser refused the websocket: " + head.split(crlf.encode(), 1)[0].decode("latin-1", "replace"))
        self.sock.settimeout(None)
        self._buf = rest
        self._wlock = threading.Lock()
        self._plock = threading.Lock()
        self._pending: dict[int, list] = {}              # id -> [event, reply]
        self._next = 0
        self._handlers: list = []
        self.alive = True
        self.why_closed = ""
        self._t = threading.Thread(target=self._read_loop, name="cdp-reader", daemon=True)
        self._t.start()

    # ---- incoming ------------------------------------------------------------------------------------------------
    def _need(self, n: int) -> bytes:
        while len(self._buf) < n:
            chunk = self.sock.recv(262144)
            if not chunk:
                raise Closed("socket closed")
            self._buf += chunk
        out, self._buf = self._buf[:n], self._buf[n:]
        return out

    def _frame(self):
        b0, b1 = self._need(2)
        n = b1 & 0x7F
        if n == 126:
            n = struct.unpack(">H", self._need(2))[0]
        elif n == 127:
            n = struct.unpack(">Q", self._need(8))[0]
        mask = self._need(4) if b1 & 0x80 else b""
        data = self._need(n)
        if mask:
            data = bytes(c ^ mask[i % 4] for i, c in enumerate(data))
        return bool(b0 & 0x80), b0 & 0x0F, data

    def _read_loop(self) -> None:
        parts: list = []
        try:
            while True:
                fin, op, data = self._frame()
                if op == 0x8:                                # close
                    raise Closed("the browser closed the connection")
                if op == 0x9:                                # ping -> pong
                    self._write(0xA, data)
                    continue
                if op == 0xA:
                    continue
                parts.append(data)
                if not fin:
                    continue
                raw, parts = b"".join(parts), []
                try:
                    msg = json.loads(raw.decode("utf-8", "replace"))
                except ValueError:
                    continue
                if "id" in msg:
                    with self._plock:
                        slot = self._pending.pop(msg["id"], None)
                    if slot:
                        slot[1] = msg
                        slot[0].set()
                elif "method" in msg:
                    for h in list(self._handlers):
                        try:
                            h(msg.get("sessionId") or "", msg["method"], msg.get("params") or {})
                        except Exception:  # noqa: BLE001 - a handler's bug must never end the reader
                            pass
        except Exception as e:  # noqa: BLE001
            self.why_closed = str(e) or type(e).__name__
        finally:
            self.alive = False
            with self._plock:
                slots, self._pending = list(self._pending.values()), {}
            for slot in slots:
                slot[1] = {"error": {"message": "connection closed"}, "_closed": True}
                slot[0].set()

    # ---- outgoing ------------------------------------------------------------------------------------------------
    def _write(self, op: int, payload: bytes) -> None:
        n = len(payload)
        head = bytes([0x80 | op])
        if n < 126:
            head += bytes([0x80 | n])
        elif n < 65536:
            head += bytes([0x80 | 126]) + struct.pack(">H", n)
        else:
            head += bytes([0x80 | 127]) + struct.pack(">Q", n)
        mask = os.urandom(4)
        if n > 4096:                                       # a megabyte of typed text: mask it without a Python-level loop
            m = (mask * (n // 4 + 1))[:n]
            body = (int.from_bytes(payload, "big") ^ int.from_bytes(m, "big")).to_bytes(n, "big")
        else:
            body = bytes(c ^ mask[i % 4] for i, c in enumerate(payload))
        with self._wlock:
            self.sock.sendall(head + mask + body)

    def send(self, method: str, params: dict | None = None, session: str = "", timeout: float = 30.0, until=None) -> dict | None:
        """One command, its result. Raises Timeout (no answer in `timeout` s), Closed, or CDPError (the browser's own).
        `until`: a function looked at while waiting; when it turns true the wait is given up and None is returned
        (the mouse release that opens a dialog is only answered once the dialog is closed)."""
        if not self.alive:
            raise Closed(self.why_closed or "connection closed")
        ev = threading.Event()
        slot = [ev, None]
        with self._plock:
            self._next += 1
            mid = self._next
            self._pending[mid] = slot
        msg = {"id": mid, "method": method, "params": params or {}}
        if session:
            msg["sessionId"] = session
        try:
            self._write(0x1, json.dumps(msg, ensure_ascii=False).encode("utf-8"))
        except OSError as e:
            with self._plock:
                self._pending.pop(mid, None)
            self.alive = False
            raise Closed(str(e)) from None
        if until is None:
            got = ev.wait(timeout)
        else:
            end, got = time.monotonic() + timeout, False
            while not got and time.monotonic() < end:
                got = ev.wait(0.03)
                if not got and until():
                    with self._plock:
                        self._pending.pop(mid, None)
                    return None
        if not got:
            with self._plock:
                self._pending.pop(mid, None)
            raise Timeout(f"{method}: no answer in {timeout:g} s")
        rep = slot[1] or {}
        if rep.get("_closed"):
            raise Closed(self.why_closed or "connection closed")
        if "error" in rep:
            raise CDPError(str((rep["error"] or {}).get("message") or rep["error"]))
        return rep.get("result") or {}

    def post(self, method: str, params: dict | None = None, session: str = "") -> None:
        """Send and do not wait (for a command whose answer only comes once something else happens, like the mouse
        release that opens a dialog)."""
        with self._plock:
            self._next += 1
            mid = self._next
        msg = {"id": mid, "method": method, "params": params or {}}
        if session:
            msg["sessionId"] = session
        try:
            self._write(0x1, json.dumps(msg, ensure_ascii=False).encode("utf-8"))
        except OSError as e:
            self.alive = False
            raise Closed(str(e)) from None

    def on(self, handler) -> None:
        self._handlers.append(handler)

    def close(self) -> None:
        self.alive = False
        try:
            self._write(0x8, b"")
        except Exception:  # noqa: BLE001
            pass
        try:
            self.sock.close()
        except OSError:
            pass

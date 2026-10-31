"""ports (2026-10-31): a server of this pack that finds its port taken by something else moves to the next free one and
every reader dials where it really is; a port held by our OWN live server is recognised, never abandoned.

Scratch everything: a throwaway home, registry (SAGE_PACKS), ledger (SAGE_PORTS), org.json and browser record. Fake
Mission Control servers answer /api/health with a pack slug; plain sockets stand in for "another program".
   python assistant/tests/test_ports.py
"""
import os as _os, tempfile as _tf, json as _json
_os.environ.setdefault("WV_LOG_DIR", _tf.mkdtemp(prefix="wv_logs_"))
HOME = _tf.mkdtemp(prefix="wv_ports_home_")
_os.environ["USERPROFILE"] = _os.environ["HOME"] = HOME
_os.environ["SAGE_PACKS"] = _os.path.join(HOME, ".sage", "packs.json")
_os.environ["SAGE_PORTS"] = _os.path.join(HOME, ".sage", "ports.json")
_os.environ["DOER_DATA"] = _os.path.join(HOME, "doer")
_os.makedirs(_os.path.join(HOME, "pack", "assistant"), exist_ok=True)
_os.environ["ASSISTANT_ORG"] = _os.path.join(HOME, "pack", "assistant", "org.json")
_os.environ.pop("MC_PORT", None)
_os.environ.pop("DOER_CDP_PORT", None)
import json
import os
import re
import socket
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")
HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(ROOT))
FAILS = []


def check(name, cond, extra=""):
    print(("ok   " if cond else "FAIL ") + name + (f"  {extra}" if extra and not cond else ""))
    if not cond:
        FAILS.append(name)


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def hold(port: int) -> socket.socket:
    """Another program on `port`: a plain listening socket that speaks no HTTP."""
    s = socket.socket()
    s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    s.bind(("127.0.0.1", port))
    s.listen(64)              # every probe lands in the backlog until closed; a backlog of one went deaf after two
    return s


def fake_mc(port: int, pack: str) -> ThreadingHTTPServer:
    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def do_GET(self):
            body = json.dumps({"ok": True, "pack": pack, "pid": os.getpid(), "port": port}).encode()
            self.send_response(200 if self.path == "/api/health" else 404)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
    srv = ThreadingHTTPServer(("127.0.0.1", port), H)
    srv.daemon_threads = True
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


MC = free_port()
Path(os.environ["ASSISTANT_ORG"]).write_text(json.dumps({"name": "Test Org", "mission_control_port": MC}), encoding="utf-8")
import ports  # noqa: E402
import coexist  # noqa: E402

check("the pack's slug comes from org.json, no literal", ports.slug() == "test-org", ports.slug())
check("the wanted Mission Control port is org.json's", ports.wanted("mission_control") == MC)

# ---- 1. free: the wanted port
p, how = ports.claim("mission_control")
check("a free wanted port is taken as is", (p, how) == (MC, "free"), (p, how))
check("the ledger records it", (ports.ledger().get("mission_control") or {}).get("port") == MC, ports.ledger())
check("a reader dials the wanted port while nothing of ours is up elsewhere", ports.current("mission_control") == MC)

# ---- 2. held by another program: move to the next free one, and say who held it
other = hold(MC)
p2, how2 = ports.claim("mission_control")
check("a port held by another program moves us", how2 == "moved" and p2 != MC and p2 > MC, (p2, how2))
check("the next port is free, not just the next number", not ports.in_use(p2))
check("the ledger names the holder", "another program" in str(ports.ledger()["mission_control"].get("holder")), ports.ledger()["mission_control"])
check("a reader still dials the wanted port until ours answers on the new one", ports.current("mission_control") == MC)
mine = fake_mc(p2, "test-org")
check("ours() knows our own Mission Control by its slug", ports.ours("mission_control", p2))
check("a reader now dials the moved port", ports.current("mission_control") == p2)
check("the keepalive's URL follows", ports.url("mission_control").endswith(f":{p2}"))
check("the holder of the wanted port is described", ports.holder(MC) == "another program", ports.holder(MC))
r = {x["check"]: x for x in ports.rows()}
check("the doctor shows the move as ok with both ports", r["port guard: mission_control"]["status"] == "ok" and str(MC) in r["port guard: mission_control"]["detail"] and str(p2) in r["port guard: mission_control"]["detail"], r)
mine.shutdown(); mine.server_close()
check("when ours is gone the reader goes back to the wanted port", ports.current("mission_control") == MC)
other.close()

# ---- 3. another pack's Mission Control on our wanted port is not ours; our own is
theirs = fake_mc(MC, "other-org")
check("another pack's server on our port is not ours", not ports.ours("mission_control", MC))
check("the holder is named as that pack's Mission Control", "other-org" in ports.holder(MC), ports.holder(MC))
p3, how3 = ports.claim("mission_control")
check("we move off another pack's server", how3 == "moved" and p3 != MC, (p3, how3))
theirs.shutdown(); theirs.server_close()
ours_srv = fake_mc(MC, "test-org")
p4, how4 = ports.claim("mission_control")
check("our own live server on the wanted port means 'ours', no move", (p4, how4) == (MC, "ours"), (p4, how4))
ours_srv.shutdown(); ours_srv.server_close()

# ---- 4. never hop onto a port another pack on this machine claims
pack2 = Path(HOME) / "Documents" / "second-os"
(pack2 / "assistant").mkdir(parents=True)
(pack2 / "assistant" / "org.json").write_text(json.dumps({"name": "Second Org", "mission_control_port": MC + 1, "state_home": "~/.sage/second-org"}), encoding="utf-8")
(pack2 / "assistant" / "config.json").write_text(json.dumps({"doer": {"cdp_port": MC + 2}}), encoding="utf-8")
(pack2 / "engine_manifest.json").write_text(json.dumps({"engine": {"installed": True}}), encoding="utf-8")   # installed, so a pack, not an engine checkout
coexist.register(pack2)
check("the other pack is seen", any(c["slug"] == "second-org" for c in coexist.packs()))
check("its ports are taken for us", {MC + 1, MC + 2} <= ports._taken(), ports._taken())
held = hold(MC)
p5, how5 = ports.claim("mission_control")
check("the move skips the other pack's configured ports", how5 == "moved" and p5 not in (MC, MC + 1, MC + 2), (p5, how5))
held.close()

# ---- 5. the doer's browser: ours by its record (port + live pid), never by answering alone
CDP = free_port()
bstate = Path(os.environ["DOER_DATA"]) / "browser.json"
bstate.parent.mkdir(parents=True, exist_ok=True)
check("the cdp port is config.json's or the default", ports.wanted("cdp") in (9333, int((json.loads((ROOT / "config.json").read_text(encoding="utf-8-sig")).get("doer") or {}).get("cdp_port") or 9333)))
hb = hold(CDP)
bstate.write_text(json.dumps({"pid": os.getpid(), "port": CDP}), encoding="utf-8")
check("a browser whose record names the port and a live pid is ours", ports.ours("cdp", CDP))
bstate.write_text(json.dumps({"pid": 999999, "port": CDP}), encoding="utf-8")
check("a dead pid in the record is not ours", not ports.ours("cdp", CDP))
bstate.write_text(json.dumps({"pid": os.getpid(), "port": CDP + 1}), encoding="utf-8")
check("a record on another port is not ours", not ports.ours("cdp", CDP))
pc, howc = ports.claim("cdp", CDP)
check("a held cdp port moves the browser", howc == "moved" and pc != CDP, (pc, howc))
bstate.write_text(json.dumps({"pid": os.getpid(), "port": pc}), encoding="utf-8")
hb2 = hold(pc)
check("a reader dials the browser where its record says it lives", ports.current("cdp", CDP) == pc)
hb2.close()
check("...and the wanted port again once that one is dark", ports.current("cdp", CDP) == CDP)
hb.close()
check("pid_alive knows this process and not a made-up one", ports.pid_alive(os.getpid()) and not ports.pid_alive(999999))

# ---- 6. the setup page is never 'ours': it simply moves
S = free_port()
hs = hold(S)
ps_, hows = ports.claim("setup", S)
check("a held setup port moves the wizard", hows == "moved" and ps_ != S, (ps_, hows))
hs.close()

# ---- 7. the CLI
r = subprocess.run([sys.executable, str(ROOT / "ports.py"), "current", "mission_control"], capture_output=True, text=True, env=dict(os.environ), timeout=60)
check("the CLI prints the port to dial", r.returncode == 0 and r.stdout.strip() == str(MC), r.stdout[-100:] + r.stderr[-200:])
r = subprocess.run([sys.executable, str(ROOT / "ports.py"), "status"], capture_output=True, text=True, env=dict(os.environ), timeout=60)
check("status lists every kind", r.returncode == 0 and all(k in r.stdout for k in ("mission_control", "cdp", "setup")), r.stdout[-200:])

# ---- 8. the guard is wired into every binder and reader (static)
src = lambda rel: (ROOT / rel).read_text(encoding="utf-8", errors="replace")  # noqa: E731
check("Mission Control claims its port and answers health with its pack", 'ports.claim("mission_control"' in src("scripts/mc_server.py") and '"pack": ports.slug()' in src("scripts/mc_server.py"))
check("Mission Control leaves quietly when it is already up", 'how == "ours"' in src("scripts/mc_server.py"))
check("the doer's browser dials where it lives and moves off a foreign one", 'ports.current("cdp"' in src("doer/browser.py") and 'ports.ours("cdp", PORT)' in src("doer/browser.py") and 'ports.claim("cdp", PORT)' in src("doer/browser.py"))
check("the supervisor's keepalive and doctor follow the live port", "ports.url(" in src("sage.py") and "ports.rows()" in src("sage.py") and not re.search(r"\bMC_URL \+ ", src("sage.py")))
check("the setup page moves off a held port and dials the pack's live one", 'ports.claim("setup"' in src("welcome.py") and "ports.current_for(" in src("welcome.py"))
check("the relay dials the live port", 'ports.current("mission_control")' in src("scripts/relay_poll.py"))
ps1 = src("scripts/mc_server.ps1")
check("the keepalive script asks ports.py and has no collapsed backslash", "ports.py" in ps1 and "\\assistant\\ports.py" in ps1 and "\x07" not in ps1 and "-LocalPort 9019" not in ps1)
check("no machine path, host or account in ports.py", not re.search(r"[A-Z]:\\\\Users|\.ts\.net|tail\d|@\w+\.", src("ports.py")))

print(f"\n{'ALL PASS' if not FAILS else str(len(FAILS)) + ' FAILED'}")
sys.exit(1 if FAILS else 0)

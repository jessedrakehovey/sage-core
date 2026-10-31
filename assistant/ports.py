"""ports.py - port guards (2026-10-31): a server of this pack that finds its port taken by SOMETHING ELSE moves to the
next free one and writes down where it went, so every reader (the supervisor's keepalive, the doctor, the setup page,
the doer and its tools) dials the live port and never another agent's. Standard in the template, on every machine.

The owner's word: "make port guards standard in public release so it switches over if one is being used".

Kinds and where the wanted port comes from:
  mission_control   MC_PORT env, else org.json mission_control_port, else 9019
  cdp               DOER_CDP_PORT env, else config.json doer.cdp_port, else 9333   (the doer's own browser)
  setup             9018 (the welcome page)

Rules:
  * claim(kind) is called by the process that BINDS: free -> the wanted port; held by this pack's own live server -> the
    wanted port with state "ours" (the caller knows it is already up); held by anything else -> the next free port that
    no other pack on this machine claims, state "moved". The outcome is written to <state_home>/ports.json (SAGE_PORTS
    overrides the path; tests use a scratch one).
  * current(kind) is for every READER: the ledger's port when a live server of this pack answers there, else the wanted
    port. A reader never dials a port some other program holds and calls it ours.
  * ours(kind, port): Mission Control answers /api/health with its pack slug and pid; the doer browser's record
    (browser.json: pid, port) must name the port and that pid must be alive. The setup page is never "ours": it moves.

  python assistant/ports.py current <kind>     the port to dial now
  python assistant/ports.py status             every kind: wanted, bound, who answers
"""
from __future__ import annotations

import json
import os
import sys
import time
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(HERE))
import coexist  # noqa: E402
import orgcfg  # noqa: E402

DEFAULTS = {"mission_control": 9019, "cdp": 9333, "setup": 9018}
LEDGER = Path(os.environ["SAGE_PORTS"]) if os.environ.get("SAGE_PORTS") else orgcfg.state_home() / "ports.json"
BROWSER_STATE = Path(os.environ["DOER_DATA"]) / "browser.json" if os.environ.get("DOER_DATA") else HERE / "data" / "doer" / "browser.json"
HOP_LIMIT = 200


def _read(p: Path) -> dict:
    try:
        return json.loads(p.read_text(encoding="utf-8-sig")) or {}
    except (OSError, ValueError):
        return {}


def ledger() -> dict:
    return _read(LEDGER)


def _write(kind: str, row: dict) -> None:
    doc = ledger()
    doc["_doc"] = "where this pack's servers actually listen (ports.py); readers dial these when alive"
    doc[kind] = row
    try:
        LEDGER.parent.mkdir(parents=True, exist_ok=True)
        tmp = LEDGER.with_suffix(".tmp")
        tmp.write_text(json.dumps(doc, indent=1) + "\n", encoding="utf-8")
        os.replace(tmp, LEDGER)
    except OSError:
        pass


def slug() -> str:
    return coexist.slug(orgcfg.get("name") or ROOT.name)


def wanted(kind: str) -> int:
    if kind == "mission_control":
        return int(os.environ.get("MC_PORT") or orgcfg.get("mission_control_port", DEFAULTS[kind]) or DEFAULTS[kind])
    if kind == "cdp":
        doer = (_read(HERE / "config.json").get("doer") or {})
        return int(os.environ.get("DOER_CDP_PORT") or doer.get("cdp_port") or DEFAULTS[kind])
    return int(DEFAULTS.get(kind) or 0)


def in_use(port: int) -> bool:
    return coexist.port_in_use(int(port))


def health(port: int, timeout: float = 1.5) -> dict | None:
    """Mission Control's /api/health on `port`, or None when nothing (or something else) answers."""
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{int(port)}/api/health", timeout=timeout) as r:
            d = json.loads(r.read().decode("utf-8", "replace"))
            return d if isinstance(d, dict) else None
    except Exception:  # noqa: BLE001
        return None


def pid_alive(pid: int) -> bool:
    if not pid or int(pid) <= 0:
        return False
    if os.name == "nt":
        import ctypes
        k = ctypes.windll.kernel32
        h = k.OpenProcess(0x1000, False, int(pid))            # PROCESS_QUERY_LIMITED_INFORMATION
        if not h:
            return False
        try:
            code = ctypes.c_ulong()
            if not k.GetExitCodeProcess(h, ctypes.byref(code)):
                return False
            return code.value == 259                           # STILL_ACTIVE
        finally:
            k.CloseHandle(h)
    try:
        os.kill(int(pid), 0)
        return True
    except OSError:
        return False


def ours(kind: str, port: int, pack_slug: str | None = None) -> bool:
    """Does a live server of THIS pack (or of `pack_slug`) answer on `port`?"""
    if kind == "mission_control":
        h = health(port)
        return bool(h) and str(h.get("pack") or "") == (pack_slug or slug())
    if kind == "cdp":
        st = _read(BROWSER_STATE)
        return int(st.get("port") or 0) == int(port) and pid_alive(int(st.get("pid") or 0)) and in_use(port)
    return False


def _taken() -> set:
    """Ports other packs on this machine claim (never hop onto one of those), plus the setup page's."""
    me = coexist._norm(ROOT)
    out = {DEFAULTS["setup"]}
    for c in coexist.packs([ROOT]):
        if c["root"] != me:
            out |= {int(c["mc_port"]), int(c["cdp_port"])}
    return out


def holder(port: int) -> str:
    """One phrase on who answers on `port` (best effort, for logs and the doctor)."""
    h = health(port)
    if h and h.get("pack"):
        return f"Mission Control of {h['pack']}"
    if h:
        return "a Mission Control server"
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{int(port)}/json/version", timeout=1.0) as r:
            v = json.loads(r.read().decode("utf-8", "replace"))
            return f"a browser ({v.get('Browser') or 'CDP'})"
    except Exception:  # noqa: BLE001
        pass
    return "another program"


def claim(kind: str, want: int | None = None, wait_s: float = 0.0) -> tuple[int, str]:
    """For the process about to bind: (port, state) with state 'free' | 'ours' | 'moved'.
    `wait_s` > 0 waits that long for a port held by our own previous instance to be let go (a restart)."""
    want = int(want or wanted(kind))
    t0 = time.time()
    while True:
        if not in_use(want):
            _write(kind, {"wanted": want, "port": want, "state": "free", "since": time.strftime("%Y-%m-%dT%H:%M:%S")})
            return want, "free"
        if ours(kind, want):
            if wait_s and time.time() - t0 < wait_s:
                time.sleep(0.5)
                continue
            _write(kind, {"wanted": want, "port": want, "state": "ours", "since": time.strftime("%Y-%m-%dT%H:%M:%S")})
            return want, "ours"
        break
    taken = _taken() | {want}
    p = want + 1
    while (p in taken or in_use(p)) and p < want + HOP_LIMIT:
        p += 1
    who = holder(want)
    _write(kind, {"wanted": want, "port": p, "state": "moved", "holder": who, "since": time.strftime("%Y-%m-%dT%H:%M:%S")})
    return p, "moved"


def current(kind: str, want: int | None = None) -> int:
    """For readers: where this pack's `kind` answers now; the wanted port when nothing of ours is up."""
    want = int(want or wanted(kind))
    row = ledger().get(kind) or {}
    p = int(row.get("port") or 0)
    if p and p != want and in_use(p) and (ours(kind, p) or kind == "setup"):
        return p
    return want


def current_for(root: Path, kind: str = "mission_control") -> int:
    """Another pack's live port (the setup page asks for the pack it manages): its ledger, checked against its slug."""
    org = _read(Path(root) / "assistant" / "org.json")
    want = int(org.get("mission_control_port") or DEFAULTS["mission_control"]) if kind == "mission_control" else int(DEFAULTS.get(kind) or 0)
    sh = str(org.get("state_home") or "").strip()
    led = _read((Path(os.path.expanduser(sh)) if sh else Path.home() / ".claude") / "ports.json")
    p = int((led.get(kind) or {}).get("port") or 0)
    if p and p != want and in_use(p) and (kind != "mission_control" or ours(kind, p, coexist.slug(org.get("name") or Path(root).name))):
        return p
    return want


def url(kind: str = "mission_control") -> str:
    return f"http://127.0.0.1:{current(kind)}"


def rows() -> list:
    """Doctor rows: one per kind, fail only when a port of ours is held by something else and nothing moved."""
    out = []
    for kind in ("mission_control", "cdp"):
        w, c = wanted(kind), current(kind)
        row = ledger().get(kind) or {}
        if c != w:
            out.append({"check": f"port guard: {kind}", "status": "ok", "detail": f"wanted {w} is held by {row.get('holder') or 'another program'}; this pack moved to {c}", "fix": ""})
        elif in_use(w) and not ours(kind, w):
            out.append({"check": f"port guard: {kind}", "status": "warn", "detail": f"{w} is held by {holder(w)}; the next start moves to a free port by itself", "fix": "nothing to do; or give this pack its own port in org.json / config.json"})
        else:
            out.append({"check": f"port guard: {kind}", "status": "ok", "detail": f"{w}" + (" up" if in_use(w) else " free"), "fix": ""})
    return out


def main() -> int:
    sys.stdout.reconfigure(encoding="utf-8")
    a = sys.argv[1:]
    if a[:1] == ["current"] and len(a) == 2:
        print(current(a[1]))
        return 0
    if a[:1] == ["status"] or not a:
        for kind in ("mission_control", "cdp", "setup"):
            w, c = wanted(kind), current(kind)
            print(f"{kind:16} wanted {w:5}  now {c:5}  {'up' if in_use(c) else 'free'}" + (f"  (moved: {holder(w)} holds {w})" if c != w else ""))
        return 0
    print(__doc__)
    return 2


if __name__ == "__main__":
    sys.exit(main())

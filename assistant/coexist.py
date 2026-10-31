"""coexist.py - several Sage agents on one computer without one hurting another (2026-10-30).

the owner: "make it safe for me to run sage core on this pc without damaging my current systems and AI wiki or local host".
A second pack made with the engine's defaults would have bound the first one's Mission Control port, driven the first
one's signed-in browser (the doer's CDP port and profile folder), written into its logs (which its sentinels read), used
its stored Claude accounts and taken its Chat-bridge lock. This module knows every pack on the machine and what each one
claims, picks free values for a new one (new_org.py), and refuses to start a pack whose claims collide (sage.py serve /
install-startup, doctor rows).

Packs are found in the registry (~/.sage/packs.json, written by new_org.py) and by looking for `assistant/org.json` one
and two folders under Documents. An engine checkout (its engine_manifest.json was exported, never installed) is a
template, never a running pack. Registry rows whose folder is gone are dropped on the next register.

  python assistant/coexist.py            # every pack on this machine and what it claims
  python assistant/coexist.py check      # this pack's collisions (exit 1 on any)
"""
from __future__ import annotations

import json
import os
import re
import socket
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
REGISTRY = Path(os.environ.get("SAGE_PACKS") or (Path.home() / ".sage" / "packs.json"))
MC_DEFAULT, CDP_DEFAULT = 9019, 9333          # what a pack without its own values uses (orgcfg / doer.browser fallbacks)


def _read(p: Path) -> dict:
    try:
        return json.loads(p.read_text(encoding="utf-8-sig")) or {}
    except (OSError, ValueError):
        return {}


def _scratch(root: Path | str) -> bool:
    """A pack under the system temp folder is a test's scaffold, never a tenant of this machine: it is neither counted
    nor registered while the real registry is in use (a test that sets SAGE_PACKS gets a scratch registry and may use temp roots)."""
    if os.environ.get("SAGE_PACKS"):
        return False
    try:
        return _norm(root).startswith(_norm(tempfile.gettempdir()) + os.sep)
    except (OSError, ValueError):
        return False


def _norm(p: str | Path) -> str:
    return os.path.normcase(os.path.abspath(os.path.expanduser(str(p)))).rstrip("\\/")


def slug(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", str(name).lower()).strip("-")


def claims(root: Path) -> dict:
    """What the pack at `root` takes on this machine."""
    org = _read(root / "assistant" / "org.json")
    doer = (_read(root / "assistant" / "config.json").get("doer") or {})
    sh = str(org.get("state_home") or "").strip()
    return {"root": _norm(root), "name": str(org.get("name") or root.name), "slug": slug(org.get("name") or root.name),
            "mc_port": int(org.get("mission_control_port") or MC_DEFAULT), "cdp_port": int(doer.get("cdp_port") or CDP_DEFAULT),
            "profile": _norm(doer.get("profile") or "~/.claude/pw-wv-cli-profile"),
            "state_home": _norm(sh) if sh else _norm(Path.home() / ".claude"),
            "keyring_service": str(org.get("keyring_service") or "")}


def is_pack(root: Path) -> bool:
    """An organization's folder; an engine checkout (an exported manifest, never `installed` into a pack) is not one."""
    if not (root / "assistant" / "org.json").is_file():
        return False
    man = root / "engine_manifest.json"
    return not man.is_file() or bool((_read(man).get("engine") or {}).get("installed"))


def packs(extra: list | None = None) -> list:
    """Every pack on this machine (registry + Documents scan + `extra`), one row each."""
    roots = []
    for r in (_read(REGISTRY).get("packs") or []):
        roots.append(Path(str(r.get("root") or "")))
    docs = Path.home() / "Documents"
    for pat in ("*/assistant/org.json", "*/*/assistant/org.json"):
        try:
            roots += [p.parent.parent for p in docs.glob(pat)]
        except OSError:
            pass
    roots += [Path(x) for x in (extra or [])]
    seen, out = set(), []
    for r in roots:
        if not str(r) or not is_pack(r) or _scratch(r):
            continue
        k = _norm(r)
        if k not in seen:
            seen.add(k)
            out.append(claims(r))
    return out


def port_in_use(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(0.3)
        return s.connect_ex(("127.0.0.1", port)) == 0


def free_port(start: int, taken: set) -> int:
    p = start
    while p in taken or port_in_use(p):
        p += 1
    return p


def allocate(org_name: str, out: Path) -> dict:
    """Values for a NEW pack that collide with nothing on this machine. Raises ValueError when the name is taken."""
    others = [c for c in packs() if c["root"] != _norm(out)]
    s = slug(org_name)
    for c in others:
        if c["slug"] == s:
            raise ValueError(f"an organization called {c['name']!r} already runs from {c['root']}; pick another name")
    home = Path.home() / ".sage" / s
    taken_mc = {c["mc_port"] for c in others} | {9018}            # 9018 = the setup page (welcome.py)
    taken_cdp = {c["cdp_port"] for c in others}
    return {"state_home": f"~/.sage/{s}", "mission_control_port": free_port(MC_DEFAULT + 1, taken_mc),
            "cdp_port": free_port(CDP_DEFAULT + 1, taken_cdp), "profile": f"~/.sage/{s}/browser-profile",
            "keyring_service": f"{s}-assistant", "home": home}


def register(root: Path) -> None:
    if _scratch(root):
        return
    reg = _read(REGISTRY)
    rows = [r for r in (reg.get("packs") or []) if _norm(r.get("root") or "") != _norm(root) and is_pack(Path(str(r.get("root") or ""))) and not _scratch(str(r.get("root") or ""))]
    c = claims(root)
    rows.append({"root": str(root), "name": c["name"]})
    REGISTRY.parent.mkdir(parents=True, exist_ok=True)
    REGISTRY.write_text(json.dumps({"_doc": "every Sage pack on this computer (coexist.py)", "packs": rows}, indent=1) + "\n", encoding="utf-8")


def check(root: Path = ROOT) -> list:
    """doctor rows: one per collision between this pack and another pack on the machine; one ok row when there is none."""
    me = claims(root)
    rows = []

    def bad(what, other, fix):
        rows.append({"check": "shares with another agent", "status": "fail", "detail": f"{what} is also {other['name']}'s ({other['root']})", "fix": fix})

    fix_cfg = "give this pack its own value in assistant/org.json / assistant/config.json (python assistant/coexist.py shows what is free)"
    for o in packs([root]):
        if o["root"] == me["root"]:
            continue
        if o["mc_port"] == me["mc_port"]:
            bad(f"Mission Control port {me['mc_port']}", o, "set org.json mission_control_port to a free port")
        if o["cdp_port"] == me["cdp_port"]:
            bad(f"the doer browser port {me['cdp_port']}", o, "set config.json doer.cdp_port to a free port")
        if o["profile"] == me["profile"]:
            bad("the doer's signed-in browser profile", o, "set config.json doer.profile to a folder under this pack's state_home")
        if o["state_home"] == me["state_home"]:
            bad(f"the logs and locks folder {me['state_home']}", o, "set org.json state_home (e.g. ~/.sage/<slug>)")
        if me["keyring_service"] and o["keyring_service"] == me["keyring_service"]:
            bad(f"the credential store name {me['keyring_service']!r}", o, fix_cfg)
        if o["slug"] == me["slug"]:
            bad(f"the organization name {me['name']!r} (its scheduled task and folders)", o, fix_cfg)
    if not rows:
        rows.append({"check": "shares with another agent", "status": "ok", "detail": "nothing: own ports, browser, logs and locks", "fix": ""})
    return rows


def collisions(root: Path = ROOT) -> list:
    return [r for r in check(root) if r["status"] == "fail"]


def main() -> int:
    sys.stdout.reconfigure(encoding="utf-8")
    if len(sys.argv) > 1 and sys.argv[1] == "check":
        rows = check(Path(sys.argv[2]) if len(sys.argv) > 2 else ROOT)
        for r in rows:
            print(f"{r['status'].upper():5} {r['detail']}" + (f"\n      -> {r['fix']}" if r["fix"] else ""))
        print(f"COEXIST {'ok' if all(r['status'] == 'ok' for r in rows) else 'COLLISION'}")
        return 0 if all(r["status"] == "ok" for r in rows) else 1
    for c in packs([ROOT]):
        print(f"{c['name']}: {c['root']}\n  mission control :{c['mc_port']}  browser :{c['cdp_port']}  profile {c['profile']}\n"
              f"  logs+locks {c['state_home']}  keyring {c['keyring_service'] or '-'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

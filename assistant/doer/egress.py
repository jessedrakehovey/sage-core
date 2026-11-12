"""egress.py - where the browser agent and the desk may reach on the network: a named list, everything else refused.

Upgrade 14 of the Grok Bot / Muse teardown (2026-11-08; Muse's Sentinel allows egress per host and taints content
that came from outside; nanoMuse's egress allow-list). The doer reads pages that anyone wrote; a page could carry a
link that posts what it just read somewhere else. This module is the fence, per side:

  egress.json      {"doer": {"mode": "learn"|"allow_list"|"off", "hosts": [...]},
                    "desk": {"mode": ..., "hosts": [...]},
                    "seen": {"doer": {host: first_seen}, "desk": {...}}}
  mode learn       (the default on a new pack) every host is allowed and written to `seen`, so the list fills from
                   real work; nothing breaks on day one
  mode allow_list  a host not on the list (nor a vault host, when allow_vault_hosts) is REFUSED, and the refusal is
                   logged; the owner adds a host in chat ("let the browser use example.com")
  mode off         no check

  allowed(url, side) -> (ok, why)
  hook(side)         the PreToolUse hook on the doer's browser_navigate (doer._settings wires it); fail CLOSED in
                     allow_list mode, open in learn mode
  add(host, side)    an owner's word adds a host (ledgered)

Hosts match on the registrable name (a subdomain of a listed host is the same host). A host from the agent vault
(a site the owners gave a login for) is always allowed for the doer. Engine code: no organization's hosts in here;
the list is the pack's (data/doer/egress.json).
"""
from __future__ import annotations

import json
import os
import re
import sys
import time
from pathlib import Path
from urllib.parse import urlparse

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
DATA = Path(os.environ["DOER_DATA"]) if os.environ.get("DOER_DATA") else ROOT / "data" / "doer"
FILE = DATA / "egress.json"
LOG = Path(os.environ.get("WV_LOG_DIR") or (Path.home() / ".claude")) / "egress-wv.log"
SIDES = ("doer", "desk")
MODES = ("learn", "allow_list", "off")
ALWAYS = ("localhost", "127.0.0.1", "[::1]")
_URL = re.compile(r"https?://[^\s\"'<>)\]]+", re.I)


def _load() -> dict:
    try:
        d = json.loads(FILE.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        d = {}
    for s in SIDES:
        d.setdefault(s, {"mode": "learn", "hosts": []})
        d[s].setdefault("mode", "learn")
        d[s].setdefault("hosts", [])
    d.setdefault("seen", {s: {} for s in SIDES})
    for s in SIDES:
        d["seen"].setdefault(s, {})
    return d


def _save(d: dict) -> None:
    FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp = FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(d, indent=1, ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, FILE)


def _log(msg: str) -> None:
    try:
        LOG.parent.mkdir(parents=True, exist_ok=True)
        with open(LOG, "a", encoding="utf-8") as f:
            f.write(time.strftime("%Y-%m-%d %H:%M:%S") + "  " + msg + "\n")
    except OSError:
        pass


def host_of(url: str) -> str:
    u = str(url or "").strip()
    if not u:
        return ""
    if "://" not in u:
        u = "https://" + u
    try:
        h = (urlparse(u).hostname or "").lower()
    except ValueError:
        return ""
    return h


def base(host: str) -> str:
    parts = str(host or "").lower().strip(".").split(".")
    if len(parts) >= 3 and parts[-2] in ("co", "com", "org", "net", "gov", "ac") and len(parts[-1]) == 2:
        return ".".join(parts[-3:])
    return ".".join(parts[-2:]) if len(parts) >= 2 else str(host or "")


def _vault_hosts() -> set:
    try:
        sys.path.insert(0, str(HERE))
        import vault
        kp = vault._open()
        return {base(h) for e in vault._group(kp).entries for h in vault._entry_hosts(e) if h}
    except Exception:  # noqa: BLE001
        return set()


def mode(side: str) -> str:
    m = str(_load()[side].get("mode") or "learn").lower()
    return m if m in MODES else "learn"


def allowed(url: str, side: str = "doer", vault_hosts: set | None = None) -> tuple:
    """(ok, why). Learn mode allows and records; allow_list refuses what is not named."""
    side = side if side in SIDES else "doer"
    d = _load()
    m = str(d[side].get("mode") or "learn").lower()
    if m == "off":
        return True, "egress check off"
    host = host_of(url)
    if not host:
        return True, "no host"
    if host in ALWAYS or host.endswith((".localhost", ".test")):
        return True, "local"
    b = base(host)
    listed = {base(h) for h in d[side].get("hosts") or [] if str(h).strip()}
    if b in listed or host in listed:
        return True, f"{b} is on the {side} list"
    if side == "doer" and d[side].get("allow_vault_hosts", True):
        vh = vault_hosts if vault_hosts is not None else _vault_hosts()
        if b in vh:
            return True, f"{b} has a login in the vault"
    if m == "learn":
        if host not in d["seen"][side]:
            d["seen"][side][host] = time.strftime("%Y-%m-%d %H:%M")
            _save(d)
        return True, f"learn mode: {host} noted"
    _log(f"{side} REFUSED {host} ({url[:120]})")
    return False, (f"{host} is not on the {side}'s list of allowed sites. Stop here and say so in one line; an owner can "
                   f"add it with the words 'let the browser use {b}'.")


def add(host: str, side: str = "doer", by: str = "") -> bool:
    side = side if side in SIDES else "doer"
    h = host_of(host) or str(host or "").strip().lower()
    if not h or "." not in h:
        return False
    d = _load()
    if base(h) in {base(x) for x in d[side]["hosts"]}:
        return False
    d[side]["hosts"].append(base(h))
    _save(d)
    _log(f"{side} ADD {base(h)} by {by}")
    try:
        sys.path.insert(0, str(ROOT / "scripts"))
        import exec_store as es
        es.ledger("egress_add", side=side, host=base(h), by=str(by or "")[:40])
    except Exception:  # noqa: BLE001
        pass
    return True


def set_mode(side: str, m: str) -> bool:
    if side not in SIDES or m not in MODES:
        return False
    d = _load()
    d[side]["mode"] = m
    _save(d)
    return True


def urls_in(text: str) -> list:
    return _URL.findall(str(text or ""))


def hook(side: str = "doer") -> int:
    """PreToolUse: the browser's navigate (tool_input.url) or a fetch tool. Fail closed only in allow_list mode."""
    try:
        evt = json.loads(sys.stdin.read() or "{}")
        inp = evt.get("tool_input") or {}
        url = str(inp.get("url") or "")
        if not url:
            for u in urls_in(json.dumps(inp)):
                url = u
                break
        ok, why = (True, "no url") if not url else allowed(url, side)
    except Exception as e:  # noqa: BLE001
        ok, why = (mode(side) != "allow_list"), f"egress check error ({type(e).__name__})"
    if ok:
        return 0
    print(json.dumps({"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "deny", "permissionDecisionReason": why}}))
    return 0


def main() -> int:
    a = sys.argv[1:]
    if a[:1] == ["--hook"]:
        return hook(a[1] if len(a) > 1 else "doer")
    if a[:1] == ["check"] and len(a) >= 2:
        ok, why = allowed(a[1], a[2] if len(a) > 2 else "doer")
        print(("ALLOW " if ok else "DENY ") + why)
        return 0 if ok else 1
    if a[:1] == ["add"] and len(a) >= 2:
        print("added" if add(a[1], a[2] if len(a) > 2 else "doer", by="cli") else "not added")
        return 0
    if a[:1] == ["mode"] and len(a) == 3:
        print("ok" if set_mode(a[1], a[2]) else "bad side or mode")
        return 0
    d = _load()
    for s in SIDES:
        print(f"{s}: mode={d[s]['mode']} hosts={len(d[s]['hosts'])} seen={len(d['seen'][s])}")
        for h in sorted(d["seen"][s])[:40]:
            print(f"   seen {h}  {d['seen'][s][h]}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

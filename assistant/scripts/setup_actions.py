"""setup_actions.py - the buttons behind Mission Control's Setup page (/setup).

An owner should connect the agent to the services the business runs on without reading a
runbook: one row per connection or service, a state dot, and ONE button that does the next
step on the PC. The page is served to the owners' phones over Tailscale, so the step can be
started from anywhere; what needs a human hand (a password, a Google consent) is done at the
PC in the window this opens, and never typed by the agent.

Every row is an entry of assistant/connections.json (the technical registry conn_check.py
proves alive) or a service the owners told the agent about in chat (assistant/agent/org.py,
`services`). Each row gets a recipe, from its explicit `setup` block when it has one,
otherwise derived from how it is checked:

  terminal   open a console on the PC running the row's own renew command (setup_creds.py,
             vault.py add, gh auth login ...): the owner types the secret there, hidden
  env        a console that asks for ONE value (hidden) and stores it as a User env var
  window     the HoneyBook sign-in window (scripts/honeybook/hb_signin_window.py --spawn)
  signin     the agent's own browser ON SCREEN at the service's sign-in page; the owner signs
             in once and the session persists (the doer's profile)
  open_url   open a page in the default browser (an app-password page, a token page)
  mcp        a connector that must be re-authorised from a Claude Code session: shown, not run
  text       nothing to run; the instructions are the step

Also: `check` re-runs conn_check for that row; `probe` opens a service's URL off-screen in the
agent's browser and says whether it looks signed in; `shot` returns a PNG of what the browser
shows right now so the owner on the phone can see where a sign-in got to.

Nothing here reads, prints or stores a secret. Nothing sends. The browser helpers run in the
wedding-web venv (playwright) as a subprocess so the Mission Control server never imports it.

  python assistant/scripts/setup_actions.py list            -> every row with its state and next step
  python assistant/scripts/setup_actions.py run <id> [--action check|probe|open]
  python assistant/scripts/setup_actions.py shot            -> assistant/out/setup/shot.png
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
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from alib import DATA, OUT, ROOT, VAULT, VENV_PY, load_json, now_iso, save_json  # noqa: E402

REGISTRY = ROOT / "connections.json"
STATUS = DATA / "connections_status.json"
RUNS = DATA / "setup_runs.json"
SHOT_DIR = OUT / "setup"
NO_WINDOW = 0x08000000 if os.name == "nt" else 0
NEW_CONSOLE = 0x00000010 if os.name == "nt" else 0
PY = sys.executable

_URL_RE = re.compile(r"https?://[^\s)\]>\"']+")
_TICK_RE = re.compile(r"`([^`]+)`")
_LOGIN_HINT = re.compile(r"(?i)login|log-in|signin|sign-in|sign_in|auth|sso|account/enter|session/new|identifier")


# ------------------------------------------------------------------ rows
def _registry() -> list:
    return list((load_json(REGISTRY, {}) or {}).get("connections") or [])


def _status() -> dict:
    return (load_json(STATUS, {}) or {}).get("connections") or {}


def _org_services() -> list:
    try:
        sys.path.insert(0, str(ROOT / "agent"))
        import org  # noqa: E402
        return list(org.load().get("services") or [])
    except Exception:  # noqa: BLE001
        return []


def _renew_cmd(row: dict) -> list | None:
    """The first `backticked` command in the row's renew text, as argv, when it is one of
    the scripts an owner runs by hand. Anything else is instructions, not a button."""
    renew = str(row.get("renew") or "")
    cands = [m.group(1) for m in _TICK_RE.finditer(renew)]
    m = re.search(r"(?m)^\s*((?:python|gh|rclone|nlm|tailscale|claude)\s.*)$", renew)
    if m:
        cands.append(m.group(1))
    for cmd in cands:
        cmd = re.split(r"\s+\(|\s*\|\s*|;|,\s", cmd.strip())[0].strip()
        if "<" in cmd or not re.match(r"^(python|py|gh|claude|nlm|tailscale|npx|rclone)\b", cmd):
            continue
        argv = cmd.split()
        if argv[0] in ("python", "py"):
            argv[0] = PY
        return argv
    return None


def recipe(row: dict) -> dict:
    """{"kind", "label", "argv"|"url"|"name"|"profile", "how": one plain sentence}."""
    s = row.get("setup")
    if isinstance(s, dict) and s.get("kind"):
        return {**s, "label": s.get("label") or _label(s["kind"])}
    ct = str((row.get("check") or {}).get("type") or "")
    rid, renew = str(row.get("id") or ""), str(row.get("renew") or "")
    if rid in ("hb-browser", "hb-doer"):
        return {"kind": "window", "profile": "hb" if rid == "hb-browser" else "doer", "label": "Open sign-in window",
                "how": "A HoneyBook sign-in window opens on the PC and waits; sign in with Google there."}
    if ct == "imap":
        acct = str((row.get("check") or {}).get("account") or "")
        return {"kind": "terminal", "argv": [PY, "assistant/scripts/setup_creds.py", "mail", acct], "label": "Enter app password",
                "also_url": "https://myaccount.google.com/apppasswords",
                "how": "A console opens on the PC asking for the mailbox's Google app password (typed hidden). Make one at myaccount.google.com/apppasswords first."}
    if ct == "ics":
        acct = str((row.get("check") or {}).get("account") or "")
        return {"kind": "terminal", "argv": [PY, "assistant/scripts/setup_creds.py", "calendar", acct], "label": "Enter calendar link",
                "how": "A console opens asking for the calendar's secret iCal address (Google Calendar > Settings > the calendar > Secret address)."}
    if ct == "env":
        name = str((row.get("check") or {}).get("name") or "")
        url = (_URL_RE.search(renew) or [None])[0] if _URL_RE.search(renew) else ""
        return {"kind": "env", "name": name, "label": "Enter token", "also_url": url,
                "how": f"A console opens asking for {name} (typed hidden) and stores it for the crons."}
    if ct == "mcp" or "connector" in str(row.get("mechanism") or "").lower() and "oauth" in str(row.get("mechanism") or "").lower():
        return {"kind": "mcp", "label": "How to reconnect", "how": renew or "Re-authorise the connector from a Claude Code session in this folder."}
    argv = _renew_cmd(row)
    if argv:
        return {"kind": "terminal", "argv": argv, "label": "Run setup", "how": renew}
    url = _URL_RE.search(renew)
    if url:
        return {"kind": "open_url", "url": url.group(0).rstrip("."), "label": "Open page", "how": renew}
    return {"kind": "text", "label": "", "how": renew or "No setup step recorded for this one."}


def _label(kind: str) -> str:
    return {"terminal": "Run setup", "env": "Enter token", "window": "Open sign-in window", "signin": "Sign in",
            "open_url": "Open page", "mcp": "How to reconnect", "text": ""}.get(kind, "Run")


def _host(url: str) -> str:
    try:
        return urllib.parse.urlparse(url).netloc.lower().replace("www.", "")
    except ValueError:
        return ""


def rows() -> list:
    """Every connection and service as the Setup page shows it."""
    st = _status()
    runs = load_json(RUNS, {}) or {}
    out, seen_hosts = [], set()
    for row in _registry():
        rid = str(row.get("id") or "")
        s = st.get(rid, {})
        rc = recipe(row)
        for u in _URL_RE.findall(json.dumps(row)):
            seen_hosts.add(_host(u))
        out.append({"id": rid, "name": row.get("name") or rid, "lane": row.get("lane", ""), "owner": row.get("owner", ""),
                    "state": s.get("state", "unknown"), "detail": s.get("detail", ""), "checked": s.get("checked", ""),
                    "mechanism": row.get("mechanism", ""), "credential": row.get("credential", ""),
                    "kind": rc["kind"], "label": rc.get("label", ""), "how": rc.get("how", ""), "also_url": rc.get("also_url", ""),
                    "page": rc.get("page", ""), "page_label": rc.get("page_label", ""),
                    "last_run": runs.get(rid, {}), "source": "registry", "checkable": True})
    for sv in _org_services():
        sid = "svc-" + str(sv.get("id") or "")
        url = str(sv.get("url") or "")
        if url and _host(url) in seen_hosts:
            continue                       # the registry already covers this site with a real check
        runs_s = runs.get(sid, {})
        state = runs_s.get("probe_state", "manual")
        out.append({"id": sid, "name": sv.get("name") or sid, "lane": sv.get("lane", ""), "owner": "owner",
                    "state": state, "detail": runs_s.get("probe_detail", sv.get("for", "")), "checked": runs_s.get("probe_ts", ""),
                    "mechanism": "signed in once in the agent's browser; the session persists" if url else "no site address yet",
                    "credential": f"account {sv['account']}" if sv.get("account") else "",
                    "kind": "signin" if url else "text", "label": "Sign in" if url else "",
                    "how": (f"The agent's browser opens on the PC at {url}; sign in there once. Tap Check afterwards." if url
                            else "Tell the agent the site's address in chat and a Sign in button appears here."),
                    "url": url, "also_url": "", "last_run": runs_s, "source": "org", "checkable": bool(url)})
    order = {"dead": 0, "not_configured": 1, "unknown": 2, "manual": 3, "live": 4}
    out.sort(key=lambda r: (order.get(r["state"], 2), r["lane"], r["name"].lower()))
    return out


def summary_block(max_rows: int = 40) -> str:
    """For the agent's context: what is connected, what is not, and that the Setup page exists."""
    rs = rows()
    if not rs:
        return ""
    bad = [r for r in rs if r["state"] in ("dead", "not_configured")]
    L = [f"CONNECTIONS ({len(rs)} known; owners connect or fix any of them on Mission Control's Setup page, /setup, one button each; "
         f"you never ask for a password or a code in chat):"]
    for r in rs[:max_rows]:
        L.append(f"- {r['name']} [{r['lane']}]: {r['state']}" + (f" ({r['detail'][:60]})" if r.get("detail") else ""))
    if bad:
        L.append("Not working right now: " + ", ".join(r["name"] for r in bad[:8]) + ". If an ask needs one of these, say which and point at the Setup page.")
    try:
        import connect                                  # chat setup (2026-10-16): which rows the owner can do right in chat
        hint = connect.chat_hint()
        if hint:
            L.append(hint)
    except Exception:  # noqa: BLE001
        pass
    return "\n".join(L)


# ------------------------------------------------------------------ running a step
def _record(rid: str, d: dict) -> None:
    runs = load_json(RUNS, {}) or {}
    runs[rid] = {**(runs.get(rid) or {}), **d, "ts": now_iso()}
    save_json(RUNS, runs)


def _console(argv: list, title: str) -> dict:
    """A visible console on the PC that stays open after the command (the owner reads the
    result and closes it). The secret is typed in that window, never passed here."""
    if os.name != "nt":
        return {"ok": False, "message": "console steps are only wired for Windows"}
    quoted = " ".join(f'"{a}"' if " " in a else a for a in argv)
    cmd = ["cmd", "/k", f"title {title} && cd /d \"{VAULT}\" && {quoted}"]
    subprocess.Popen(cmd, cwd=str(VAULT), creationflags=NEW_CONSOLE, close_fds=True)
    return {"ok": True, "message": f"A console window opened on the PC: {title}. Finish it there, then tap Check."}


def _browser(op: str, url: str = "", timeout: int = 90) -> dict:
    """The playwright-side helpers run in the wedding-web venv as a subprocess."""
    py = str(VENV_PY) if VENV_PY.exists() else PY
    try:
        r = subprocess.run([py, str(Path(__file__).resolve()), "--browser-op", op, "--url", url], capture_output=True, text=True,
                           encoding="utf-8", errors="replace", timeout=timeout, cwd=str(VAULT), creationflags=NO_WINDOW)
    except subprocess.TimeoutExpired:
        return {"ok": False, "message": "the browser did not answer in time"}
    line = (r.stdout or "").strip().splitlines()[-1:] or [""]
    try:
        return json.loads(line[0])
    except ValueError:
        return {"ok": False, "message": (r.stderr or r.stdout or "browser helper failed").strip()[-300:]}


def _browser_op(op: str, url: str) -> dict:
    """Runs inside the venv: open a sign-in tab on screen, probe a URL off-screen, or screenshot."""
    sys.path.insert(0, str(ROOT / "doer"))
    import browser  # noqa: E402
    if op == "signin":
        cdp = browser.open_browser(show=True)
        from playwright.sync_api import sync_playwright
        with sync_playwright() as pw:
            b = pw.chromium.connect_over_cdp(cdp, timeout=8000)
            ctx = b.contexts[0] if b.contexts else b.new_context()
            page = ctx.new_page()
            page.goto(url, wait_until="domcontentloaded", timeout=45000)
            page.bring_to_front()
            title = page.title()
        return {"ok": True, "message": f"The agent's browser is on screen at {_host(url)} ({title[:60]}). Sign in there once, then tap Check."}
    if op == "probe":
        cdp = browser.open_browser(show=False)
        from playwright.sync_api import sync_playwright
        with sync_playwright() as pw:
            b = pw.chromium.connect_over_cdp(cdp, timeout=8000)
            ctx = b.contexts[0] if b.contexts else b.new_context()
            page = ctx.new_page()
            try:
                page.goto(url, wait_until="domcontentloaded", timeout=45000)
                page.wait_for_timeout(2500)
                final, title = page.url, page.title()
                has_pw = page.locator("input[type=password]").count() > 0
            finally:
                page.close()
        signed = not has_pw and not _LOGIN_HINT.search(final) and _host(final) != "accounts.google.com"
        return {"ok": True, "state": "live" if signed else "dead",
                "message": (f"Looks signed in: {_host(final)} opened to '{title[:50]}'." if signed
                            else f"Not signed in: {_host(url)} sent the browser to a sign-in page ({_host(final)}). Tap Sign in.")}
    if op == "shot":
        st = browser.alive()
        if not st:
            return {"ok": False, "message": "the agent's browser is not open"}
        from playwright.sync_api import sync_playwright
        SHOT_DIR.mkdir(parents=True, exist_ok=True)
        out = SHOT_DIR / "shot.png"
        with sync_playwright() as pw:
            b = pw.chromium.connect_over_cdp(browser.cdp_url(), timeout=8000)
            pages = [p for c in b.contexts for p in c.pages]
            if not pages:
                return {"ok": False, "message": "no page is open"}
            page = pages[-1]
            page.screenshot(path=str(out), full_page=False)
            return {"ok": True, "path": str(out), "url": page.url, "title": page.title()[:80]}
    return {"ok": False, "message": f"unknown browser op {op}"}


def _env_prompt(name: str) -> int:
    """Runs in the console the owner sees: ask hidden, store as a User env var. Never logged."""
    import getpass
    v = getpass.getpass(f"Paste the value for {name} (hidden): ").strip()
    if not v:
        print("nothing entered; nothing changed")
        return 1
    if os.name == "nt":
        import winreg
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, "Environment", 0, winreg.KEY_SET_VALUE) as k:
            winreg.SetValueEx(k, name, 0, winreg.REG_SZ, v)
        try:   # tell running apps the environment changed
            import ctypes
            ctypes.windll.user32.SendMessageTimeoutW(0xFFFF, 0x001A, 0, "Environment", 2, 5000, None)
        except Exception:  # noqa: BLE001
            pass
    else:
        os.environ[name] = v
    print(f"stored {name} as a User environment variable ({len(v)} characters). Crons pick it up on their next run.")
    return 0


def check(rid: str) -> dict:
    """Re-run conn_check for one registry row (or probe a service) and return its state."""
    if rid.startswith("svc-"):
        sv = next((s for s in _org_services() if "svc-" + str(s.get("id")) == rid), None)
        if not sv or not sv.get("url"):
            return {"ok": False, "message": "no site address for this service"}
        r = _browser("probe", sv["url"])
        if r.get("ok"):
            _record(rid, {"probe_state": r.get("state", "manual"), "probe_detail": r.get("message", ""), "probe_ts": now_iso()})
        return r
    try:
        r = subprocess.run([PY, "assistant/scripts/conn_check.py", "--only", rid], capture_output=True, text=True, encoding="utf-8",
                           errors="replace", timeout=120, cwd=str(VAULT), creationflags=NO_WINDOW)
    except subprocess.TimeoutExpired:
        return {"ok": False, "message": "the check timed out"}
    s = _status().get(rid, {})
    return {"ok": True, "state": s.get("state", "unknown"), "message": f"{s.get('state', 'unknown')}: {s.get('detail', '')}"[:200]}


def run(rid: str, action: str = "", by: str = "") -> dict:
    """The button. -> {"ok", "message", "state"?}. Nothing here is a secret or a send."""
    if action == "check":
        r = check(rid)
        _record(rid, {"last": "check", "by": by, "result": r.get("message", "")[:200]})
        return r
    if action == "shot":
        return _browser("shot")
    row = next((r for r in rows() if r["id"] == rid), None)
    if not row:
        return {"ok": False, "message": "no such connection"}
    reg = next((r for r in _registry() if r.get("id") == rid), None)
    rc = recipe(reg) if reg else {"kind": row["kind"], "url": row.get("url", ""), "how": row["how"]}
    kind = rc.get("kind")
    if action == "open" and rc.get("also_url"):
        os.startfile(rc["also_url"]) if os.name == "nt" else None  # noqa: B018
        res = {"ok": True, "message": f"Opened {_host(rc['also_url'])} in the PC's browser."}
    elif kind == "terminal":
        res = _console(list(rc.get("argv") or []), f"Setup: {row['name']}")
    elif kind == "env":
        res = _console([PY, "assistant/scripts/setup_actions.py", "--prompt-env", str(rc.get("name") or "")], f"Setup: {row['name']}")
    elif kind == "window":
        subprocess.Popen([PY, "scripts/honeybook/hb_signin_window.py", "--spawn", str(rc.get("profile") or "doer")], cwd=str(VAULT),
                         creationflags=NO_WINDOW, close_fds=True, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        res = {"ok": True, "message": "A HoneyBook sign-in window is opening on the PC. Sign in there, then tap Check."}
    elif kind == "signin":
        res = _browser("signin", str(rc.get("url") or row.get("url") or ""))
    elif kind == "open_url":
        if os.name == "nt":
            os.startfile(rc["url"])
        res = {"ok": True, "message": f"Opened {_host(rc['url'])} in the PC's browser. {rc.get('how', '')}".strip()}
    elif kind == "mcp":
        res = {"ok": True, "message": rc.get("how", "")}
    else:
        res = {"ok": True, "message": rc.get("how") or "Nothing to run for this one."}
    _record(rid, {"last": kind, "by": by, "result": res.get("message", "")[:200]})
    return res


def main() -> int:
    sys.stdout.reconfigure(encoding="utf-8")
    ap = argparse.ArgumentParser()
    ap.add_argument("cmd", nargs="?", default="list", choices=["list", "run", "shot", "summary"])
    ap.add_argument("id", nargs="?", default="")
    ap.add_argument("--action", default="")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--browser-op", default="", help=argparse.SUPPRESS)
    ap.add_argument("--url", default="", help=argparse.SUPPRESS)
    ap.add_argument("--prompt-env", default="", help=argparse.SUPPRESS)
    a = ap.parse_args()
    if a.prompt_env:
        return _env_prompt(a.prompt_env)
    if a.browser_op:
        try:
            print(json.dumps(_browser_op(a.browser_op, a.url)))
        except Exception as e:  # noqa: BLE001
            print(json.dumps({"ok": False, "message": f"{type(e).__name__}: {str(e)[:200]}"}))
        return 0
    if a.cmd == "list":
        rs = rows()
        if a.json:
            print(json.dumps(rs, ensure_ascii=False, indent=1))
        else:
            for r in rs:
                print(f"{r['state']:<15} {r['id']:<22} {r['name'][:34]:<34} {r['kind']:<9} {r['label']}")
        return 0
    if a.cmd == "summary":
        print(summary_block())
        return 0
    if a.cmd == "shot":
        print(json.dumps(_browser("shot")))
        return 0
    if a.cmd == "run" and a.id:
        print(json.dumps(run(a.id, a.action, by="cli"), ensure_ascii=False))
        return 0
    print(__doc__)
    return 0


if __name__ == "__main__":
    sys.exit(main())

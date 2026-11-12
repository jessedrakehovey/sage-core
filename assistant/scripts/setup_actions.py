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
  window     a CRM sign-in window (connector-provided script, spawned in --spawn mode)
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
import crm

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
    if ct == "imap":
        acct = str((row.get("check") or {}).get("account") or "")
        return {"kind": "terminal", "argv": [PY, "assistant/scripts/setup_creds.py", "mail", acct], "label": "Enter app password",
                "also_url": "https://myaccount.google.com/apppasswords",
                "how": "A console opens on the PC asking for the mailbox's Google app password (typed hidden). Make one at myaccount.google.com/apppasswords first."}
    if ct == "ics":
        acct = str((row.get("check") or {}).get("account") or (row.get("check") or {}).get("feed") or "")   # ics rows name it `feed`
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
            "open_url": "Open page", "mcp": "How to reconnect", "text": "", "job": "Run it"}.get(kind, "Run")


_SIGNIN_URL = re.compile(r"signin_url=(\S+)")


def _browser_up() -> bool:
    """Is the agent's browser running? Replaceable in tests."""
    try:
        sys.path.insert(0, str(ROOT / "doer"))
        import browser as agent_browser
        return bool(agent_browser.alive())
    except Exception:  # noqa: BLE001
        return False


def _browser_close() -> None:
    """A job that opened the agent's browser closes it again (the profile is shared with the doer). Replaceable in tests."""
    try:
        import browser as agent_browser
        agent_browser.close_browser()
    except Exception:  # noqa: BLE001
        pass


def _login_fill(url: str) -> str:
    """doer/login_fill.py on the page the job stopped at: SIGNED_IN / ALREADY / NEEDS_CODE / NO_ENTRY / FAILED. Replaceable in tests."""
    r = subprocess.run([str(VENV_PY), "assistant/doer/login_fill.py", "--url", url], cwd=str(VAULT), capture_output=True, text=True,
                       encoding="utf-8", errors="replace", timeout=120, creationflags=NO_WINDOW)
    lines = [ln for ln in ((r.stdout or "") + "\n" + (r.stderr or "")).strip().splitlines() if ln.strip()]
    return lines[-1] if lines else "FAILED no output"


def _job(rc: dict, row: dict, by: str = "", retry: bool = True) -> dict:
    """setup kind `job` (2026-11-04): a script the agent runs itself, in its own browser or on the PC, and reports in one
    line. `%VENV%` in argv = the playwright venv's python. Exit 0 = done, anything else = failed with the last line.
    Exit 2 = the site (Google, mostly) wants a sign-in: the owner is NEVER sent to the PC (the owner 2026-11-04: Sage is a
    portal into the PC, everything happens in Chat). The agent signs in itself from its vault (login_fill.py on the
    page the job stopped at) and runs the job again; a login the vault lacks becomes an Enter login card (the Chat
    dialog, saved to the vault, then the job runs again by itself); a one-time code is the same card's advice to
    add the code-generator secret, so codes are automatic from then on."""
    argv = [str(VENV_PY) if a == "%VENV%" else (PY if a == "python" else str(a)) for a in (rc.get("argv") or [])]
    if not argv:
        return {"ok": False, "message": "this job has no command"}
    if retry:                                     # the outer call: the agent's browser goes back to how it was found
        was_up = _browser_up()
        try:
            return _job(rc, row, by, retry=None)
        finally:
            if not was_up and _browser_up():
                _browser_close()
    retry = retry is None                         # None = the first inner run (may retry once); False = the retry itself
    try:
        r = subprocess.run(argv, cwd=str(VAULT), capture_output=True, text=True, encoding="utf-8", errors="replace",
                           timeout=int(rc.get("timeout_s") or 600), creationflags=NO_WINDOW)
    except subprocess.TimeoutExpired:
        return {"ok": False, "state": "failed", "message": f"{row['name']}: it ran past {int(rc.get('timeout_s') or 600)} s and was stopped; tap again to retry."}
    lines = [ln for ln in ((r.stdout or "") + "\n" + (r.stderr or "")).strip().splitlines() if ln.strip()]
    last = lines[-1] if lines else ""
    detail = re.sub(r"^[A-Z_]+(?: step=\S+)? status=\S+\s*", "", last).strip()      # "SHOTS_SETUP status=ok ..." / "CHAT_CONSOLE step=all status=ok ..."
    if r.returncode == 0:
        return {"ok": True, "state": "ok", "message": f"{row['name']}: done. {detail}".strip()}
    if r.returncode == 2:
        m = _SIGNIN_URL.search(detail)
        url = m.group(1) if m else ""
        site = _host(url) or "the site"
        detail = _SIGNIN_URL.sub("", detail).strip()
        line = _login_fill(url) if url else "FAILED the job named no sign-in page"
        if line.startswith(("SIGNED_IN", "ALREADY")) and retry:
            return _job(rc, row, by, retry=False)          # signed in from the vault: the job again, once
        if line.startswith("NO_ENTRY"):
            return {"ok": False, "state": "needs_login", "site": site,
                    "message": f"{site} wants a sign-in and my vault has no login for it. Tap Enter login: it opens a form on top of chat, "
                               f"goes straight to my vault, and I run {row['name']} again by myself."}
        if line.startswith("NEEDS_CODE"):
            return {"ok": False, "state": "needs_login", "site": site,
                    "message": f"{site} asked for a one-time code after the password. Tap Enter login and put the code-generator secret in "
                               f"(the 'can't scan?' text under the QR code in the account's 2-step settings); codes are mine from then on."}
        why = re.sub(r"^FAILED\s*", "", line)[:160] if line.startswith("FAILED") else line[:160]
        return {"ok": False, "state": "failed", "site": site,
                "message": f"{site} wants a sign-in and mine did not go through ({why}). Tap Enter login to give me a fresh one, then I try again."}
    return {"ok": False, "state": "failed", "message": f"{row['name']}: not done. {detail or last[-200:] or 'no output'}".strip()}


def _signin_from_vault(name: str, url: str) -> dict:
    """A site that wants a sign-in, done by the agent: its own browser (off-screen) at the page, the login from the vault.
    -> ok / needs_login (with the site, for the Enter login dialog) / failed. Nothing waits on the PC's screen."""
    site = _host(url) or name
    was_up = _browser_up()
    try:
        sys.path.insert(0, str(ROOT / "doer"))
        import browser as agent_browser
        agent_browser.open_browser()
        agent_browser.navigate(url)
        time.sleep(4)
        line = _login_fill(url)
    except Exception as e:  # noqa: BLE001
        line = f"FAILED {type(e).__name__}"
    finally:
        if not was_up and _browser_up():
            _browser_close()
    if line.startswith(("SIGNED_IN", "ALREADY")):
        return {"ok": True, "state": "ok", "site": site, "message": f"{name}: signed in again from my vault."}
    if line.startswith("NO_ENTRY"):
        return {"ok": False, "state": "needs_login", "site": site,
                "message": f"{name} is signed out and my vault has no login for {site}. Tap Enter login: it opens a form on top of chat, goes straight to my vault, and I sign in myself."}
    if line.startswith("NEEDS_CODE"):
        return {"ok": False, "state": "needs_login", "site": site,
                "message": f"{site} asked for a one-time code after the password. Tap Enter login and add the code-generator secret (the 'can't scan?' text under the QR code in the account's 2-step settings); codes are mine from then on."}
    why = re.sub(r"^FAILED\s*", "", line)[:160]
    return {"ok": False, "state": "failed", "site": site, "message": f"{name}: my sign-in did not go through ({why}). Tap Enter login to give me a fresh one and I try again."}


def _gap(rid: str, name: str, why: str, by: str) -> None:
    """A repair the agent cannot yet do from chat is a build for itself (capability.py), never a step for the owner."""
    try:
        sys.path.insert(0, str(ROOT / "agent"))
        import capability
        d = capability._load()
        ask = f"Fix the {name} connection from chat without anyone at the computer"
        g = capability._find(d, by or "owner", ask) or capability.open_gap(d, "capability", ask, why, by or "owner", {}, "setup from chat")
        if g and g.get("status") not in ("building", "built", "replayed", "capped"):
            capability.file_build(d, g, by=by or "owner")
        capability._save(d)
    except Exception:  # noqa: BLE001
        pass


def run_chat(rid: str, by: str = "chat", space: str = "", thread: str = "") -> dict:
    """The setup button pressed from CHAT (a connection card's "Set it up now", /signin, a job card): the chat-first
    version of run(). Nothing it does waits on the PC's screen or opens a console there (the owner 2026-11-04: the agent
    is 100% inside Google Chat, a portal into the PC):
      a job row            runs (run -> _job: a sign-in on the way is the vault's)
      a form row           its Chat form is posted where the owner is (chat_forms), or named
      a sign-in / window   the agent signs in itself from the vault (off-screen); no login -> needs_login for Enter login
      anything else        a build for the agent (capability gap), and one line saying so
    -> {"ok", "state": ok | form | needs_login | failed | gap, "message", "site"?}."""
    reg = next((r for r in _registry() if r.get("id") == rid), None)
    row = next((r for r in rows() if r["id"] == rid), None)
    if not reg and not row:
        return {"ok": False, "state": "failed", "message": "no such connection"}
    name = str((reg or row).get("name") or rid)
    rc = recipe(reg) if reg else {"kind": row["kind"], "url": row.get("url", ""), "how": row.get("how", "")}
    kind = str(rc.get("kind") or "")
    if kind == "job":
        return run(rid, by=by)
    if _chat_form(rid):
        posted = False
        if str(space or "").startswith("spaces/"):
            try:
                import chat_forms
                posted = chat_forms.post(rid, space, thread)
            except Exception:  # noqa: BLE001
                posted = False
        res = {"ok": True, "state": "form", "message": (f"{name}: its form is in our chat now. Type it there and it is stored; nothing else to do."
                                                       if posted else f"{name}: say 'set up {name}' here and its form comes up.")}
        _record(rid, {"last": "form", "by": by, "result": res["message"][:200]})
        return res
    url = str(rc.get("url") or (row or {}).get("url") or "")
    if kind in ("signin", "window") and url.startswith("http"):
        res = _signin_from_vault(name, url)
        _record(rid, {"last": "vault_signin", "by": by, "result": res["message"][:200], "state": res["state"]})
        return res
    _gap(rid, name, f"setup kind {kind or 'text'} has no chat path", by)
    res = {"ok": False, "state": "gap",
           "message": f"{name}: I can't fix this one from chat yet, so I've put building that on my own list. Nothing for you to do at a computer; I'll say here when it works."}
    _record(rid, {"last": "gap", "by": by, "result": res["message"][:200], "state": "gap"})
    return res


def run_job_async(rid: str, by: str, done) -> bool:
    """Run a job row on a thread and hand its result to `done(result)` (a Chat card edit): a job takes minutes, a tap
    is answered in seconds. -> True when started."""
    import threading
    reg = next((r for r in _registry() if r.get("id") == rid), None)
    if not reg:
        return False

    def work():
        try:
            res = run_chat(rid, by=by)
        except Exception as e:  # noqa: BLE001
            res = {"ok": False, "state": "failed", "message": f"{type(e).__name__}: {str(e)[:160]}"}
        try:
            done(res)
        except Exception:  # noqa: BLE001
            pass

    threading.Thread(target=work, daemon=True, name=f"job-{rid}").start()
    return True


def _host(url: str) -> str:
    try:
        return urllib.parse.urlparse(url).netloc.lower().replace("www.", "")
    except ValueError:
        return ""


def _chat_form(rid: str) -> bool:
    """A row whose values are typed in a Chat dialog. A job row is not one: its button runs the job (here and in Chat)."""
    try:
        import chat_forms
        s = chat_forms.spec(rid)
        return bool(s) and s.get("store") != "job"
    except Exception:  # noqa: BLE001
        return False


def rows() -> list:
    """Every connection and service as the Setup page shows it."""
    st = _status()
    runs = load_json(RUNS, {}) or {}
    out, seen_hosts = [], set()
    for row in _registry():
        rid = str(row.get("id") or "")
        s = st.get(rid, {})
        rc = recipe(row)
        if _chat_form(rid):
            # typed in through a Google Chat form, never here (chat_forms.py; the owner 2026-11-01: "Make all data entry for
            # anything go through google chat never mission control"): no console button, no phone page
            rc = {"kind": "chat", "label": "", "how": "Typed in through Google Chat: ask the agent to set it up and a form opens there."}
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
    L = [f"CONNECTIONS ({len(rs)} known; anything typed in - keys, tokens, app passwords - comes as a Chat form code posts, never "
         f"Mission Control; you never ask for a password or a code as a chat message):"]
    for r in rs[:max_rows]:
        L.append(f"- {r['name']} [{r['lane']}]: {r['state']}" + (f" ({r['detail'][:60]})" if r.get("detail") else ""))
    if bad:
        L.append("Not working right now: " + ", ".join(r["name"] for r in bad[:8]) + ". If an ask needs one of these, say which and call "
                 "`blocked` with that connection: its Chat form is posted for them.")
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
    if row["kind"] == "chat":
        return {"ok": True, "message": row["how"]}
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
        win_script = str(rc.get("script") or crm._hook("SIGNIN_WINDOW_SCRIPT") or "")
        if win_script:
            subprocess.Popen([PY, win_script, "--spawn", str(rc.get("profile") or "doer")], cwd=str(VAULT),
                             creationflags=NO_WINDOW, close_fds=True, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        res = {"ok": True, "message": f"A {crm.name()} sign-in window is opening on the PC. Sign in there, then tap Check."}
    elif kind == "signin":
        res = _browser("signin", str(rc.get("url") or row.get("url") or ""))
    elif kind == "open_url":
        if os.name == "nt":
            os.startfile(rc["url"])
        res = {"ok": True, "message": f"Opened {_host(rc['url'])} in the PC's browser. {rc.get('how', '')}".strip()}
    elif kind == "mcp":
        res = {"ok": True, "message": rc.get("how", "")}
    elif kind == "job":
        res = _job(rc, row, by)
    else:
        res = {"ok": True, "message": rc.get("how") or "Nothing to run for this one."}
    _record(rid, {"last": kind, "by": by, "result": res.get("message", "")[:200], **({"state": res["state"]} if res.get("state") else {})})
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

"""remote_desktop.py - "Control PC" (Chrome Remote Desktop) set up, and its PIN set, from Google Chat (2026-11-04).

The owner: "the google remote desktop needs to be able to be set up and installed from google chat. I can't use it
because I don't have the pin, which google chat should have set up for me too."

What an owner does: taps **Set the Control PC PIN** on a card, types the PIN they want in the form on top of chat.
What the agent does, on the PC, through its administrator helper (scripts/elevated.py):
  - the Remote Desktop host is missing            -> installs Google's own host (signature checked)
  - this PC is not registered as a host           -> gets a one-time code from remotedesktop.google.com/headless in its
                                                     own browser and registers the PC with it and the PIN
  - the host is there and registered (the usual)  -> sets the PIN (the hash Chrome stores) and restarts the host
The PIN is kept in this process's memory for ten minutes at most and on disk only as Chrome's own hash.

The one thing no program can do: Windows asks for an administrator's approval, on its own prompt, the first time
anything wants to change Remote Desktop. That approval is given ONCE, for the helper (the PC install does it for a new
organization). Until it exists, set_pin answers `needs_admin` with the action "Ask Windows now", which raises that
prompt; the moment it is approved the agent finishes by itself with the PIN it was given.

  python assistant/scripts/remote_desktop.py status
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
for _p in (HERE, ROOT / "doer"):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))
from alib import DATA, load_json, save_json  # noqa: E402
import elevated  # noqa: E402

STATE = Path(os.environ.get("REMOTE_DESKTOP_STATE") or (DATA / "remote_desktop_state.json"))
HEADLESS = "https://remotedesktop.google.com/headless"
PIN_KEEP_S = 600
_PIN = re.compile(r"\d{6,12}")
_PENDING: dict = {}
NO_WINDOW = 0x08000000 if os.name == "nt" else 0
ASK = {"label": "Ask Windows now", "do": "admin"}
NEEDS_ADMIN = ("Windows wants an administrator to approve this once, on its own prompt, before anything may change Remote Desktop. "
               "Tap Ask Windows now when someone is there to say Yes: I finish by myself with the PIN you gave me, and that prompt never comes again.")


def status() -> dict:
    exe = next(iter(elevated.CRD_DIR.glob("*/remoting_host.exe")), None) if elevated.CRD_DIR.exists() else None
    running = False
    try:
        r = subprocess.run(["sc", "query", "chromoting"], capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=15, creationflags=NO_WINDOW)
        running = "RUNNING" in (r.stdout or "")
    except Exception:  # noqa: BLE001
        pass
    st = load_json(STATE, {}) or {}
    return {"installed": bool(exe), "running": running, "helper": elevated.installed(), "pin_set_at": st.get("pin_set_at", ""),
            "owner": st.get("owner", ""), "host_name": st.get("host_name", "")}


def _keep(pin: str) -> None:
    _PENDING.clear()
    _PENDING.update({"pin": pin, "exp": time.time() + PIN_KEEP_S})


def _kept() -> str:
    if _PENDING.get("pin") and time.time() < float(_PENDING.get("exp") or 0):
        return str(_PENDING["pin"])
    _PENDING.clear()
    return ""


def set_pin(pin: str = "", by: str = "") -> dict:
    """The form's submit. -> {"ok", "state": ok | needs_admin | failed, "message", "action"?}."""
    pin = re.sub(r"\s", "", str(pin or ""))
    if not _PIN.fullmatch(pin):
        return {"ok": False, "state": "failed", "message": "A PIN is 6 to 12 digits, numbers only. Tap the button and try again."}
    _keep(pin)
    if not elevated.installed():
        return {"ok": False, "state": "needs_admin", "message": NEEDS_ADMIN, "action": ASK}
    return _apply(by)


def on_action(do: str, by: str = "") -> dict:
    """A button on the result card. `admin`: raise Windows' approval prompt for the helper, then finish with the kept PIN."""
    if do != "admin":
        return {"ok": False, "state": "failed", "message": "That button is not known."}
    r = elevated.install()
    if not r.get("ok"):
        return {"ok": False, "state": "needs_admin", "action": ASK,
                "message": "Windows did not get its Yes this time (the prompt waits about two minutes). Tap Ask Windows now again when someone is there."}
    if not _kept():
        return {"ok": True, "state": "ok", "message": "Windows approved it. Tap Set the Control PC PIN and give me the PIN once more; I had let it go for safety."}
    return _apply(by)


def _apply(by: str = "") -> dict:
    pin = _kept()
    if not pin:
        return {"ok": False, "state": "failed", "message": "I let that PIN go after ten minutes for safety. Tap the button and give it to me again."}
    st = elevated.request("crd_status")
    if st.get("state") == "no_helper":
        return {"ok": False, "state": "needs_admin", "message": NEEDS_ADMIN, "action": ASK}
    if not st.get("ok"):
        return {"ok": False, "state": "failed", "message": f"I could not read Remote Desktop's state ({st.get('message')})."}
    if not st.get("installed"):
        r = elevated.request("crd_install", timeout=700)
        if not r.get("ok"):
            return {"ok": False, "state": "failed", "message": f"Remote Desktop's host would not install ({r.get('message')})."}
        st = elevated.request("crd_status")
    if not st.get("registered"):
        code = _headless_code()
        if not code.get("ok"):
            return code
        r = elevated.request("crd_register", {"code": code["code"], "pin": pin, "name": os.environ.get("COMPUTERNAME") or "PC"}, timeout=240)
    else:
        r = elevated.request("crd_pin", {"pin": pin})
    if not r.get("ok"):
        return {"ok": False, "state": "failed", "message": f"The PIN did not take ({r.get('message')}). Tap the button to try again."}
    _PENDING.clear()
    save_json(STATE, {"pin_set_at": time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime()), "owner": r.get("owner", ""), "host_name": r.get("host_name", ""), "by": by})
    who = f" It is registered to {r['owner']}: open Control PC signed in as that account." if r.get("owner") else ""
    name = f" ({r['host_name']})" if r.get("host_name") else ""
    return {"ok": True, "state": "ok", "message": f"Control PC is ready: the PIN is set{name}.{who}"}


def _headless_code() -> dict:
    """A one-time registration code from Google's headless-setup page, read in the agent's own browser.
    -> {"ok": True, "code"} or a result for the card (a sign-in the vault lacks -> needs_login)."""
    try:
        import browser as agent_browser
        from playwright.sync_api import sync_playwright
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "state": "failed", "message": f"I could not open my browser for the registration ({type(e).__name__})."}
    pw = b = None
    try:
        agent_browser.open_browser()
        pw = sync_playwright().start()
        b = pw.chromium.connect_over_cdp(agent_browser.cdp_url())
        ctx = b.contexts[0]
        page = ctx.pages[0] if ctx.pages else ctx.new_page()
        page.goto(HEADLESS, wait_until="domcontentloaded", timeout=90000)
        time.sleep(5)
        if "accounts.google.com" in page.url:
            return {"ok": False, "state": "needs_login", "site": "accounts.google.com",
                    "message": "Google wants a sign-in before it will register this computer and my vault has no Google login. Tap Enter login, then set the PIN again."}
        for label in ("Begin", "Next", "Authorize"):
            btn = page.get_by_role("button", name=label, exact=False)
            if btn.count():
                btn.first.click(force=True, timeout=15000)
                time.sleep(4)
        text = page.evaluate("() => document.body.innerText")
        m = re.search(r'--code="?([\w/.\-]{10,400})"?', text)
        if not m:
            return {"ok": False, "state": "failed", "message": "Google's registration page did not give me a code this time. Tap the button to try again."}
        return {"ok": True, "code": m.group(1)}
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "state": "failed", "message": f"The registration page did not load ({type(e).__name__}). Tap the button to try again."}
    finally:
        try:
            if b:
                b.close()
            if pw:
                pw.stop()
        except Exception:  # noqa: BLE001
            pass


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    print(json.dumps(status(), indent=1))

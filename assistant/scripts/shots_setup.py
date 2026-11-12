"""shots_setup.py - the screenshots shared drive, made by the agent in its own browser (a Chat button, 2026-11-04).

shots.py hosts a job's screen pictures in a SHARED DRIVE the agent's service account belongs to (a service account has
no Drive of its own). The owner used to make that drive by hand; now the `shots-drive` connection's button runs this:

  1. Google Drive -> Shared drives: create the drive named config shots.drive_name (default "<agent> screenshots")
     unless one with that name is already there;
  2. open it -> Manage members -> add the service account's e-mail as Content manager;
  3. prove it: the service account lists the drive by name (shots.drive_id(refresh=True)).

Idempotent. It drives the agent's signed-in browser (doer/browser.py) over CDP with Playwright (the wedding-web venv),
never types a password: when Google asks the owner to sign in again it stops with NEEDS_SIGNIN (exit 2), puts the
browser on the owner's screen and the card in Chat says so. `rehearse` does the whole thing on a throwaway name and
deletes the drive after, to prove the clicks still work.

Last line: SHOTS_SETUP status=<ok|needs_signin|failed> <detail>

  "%VENV%" assistant/scripts/shots_setup.py all | rehearse | status
"""
from __future__ import annotations

import argparse
import base64
import json
import re
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
for _p in (HERE, ROOT, ROOT / "doer"):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

OUT = ROOT / "out" / "setup"
SHARED = "https://drive.google.com/drive/shared-drives"
ROLE = "Content manager"


def _shot(page, name: str) -> str:
    try:
        OUT.mkdir(parents=True, exist_ok=True)
        cdp = page.context.new_cdp_session(page)
        d = cdp.send("Page.captureScreenshot", {"format": "jpeg", "quality": 60})
        f = OUT / f"{name}.jpg"
        f.write_bytes(base64.b64decode(d["data"]))
        return f"assistant/out/setup/{name}.jpg"
    except Exception:  # noqa: BLE001
        return ""


def _signin_wall(page) -> bool:
    return "accounts.google.com" in str(page.url)


def _needs_signin(page) -> dict:
    """Google asks the owner to sign in again. The owner is never sent to the PC: the job's runner (setup_actions._job)
    signs in from the vault on this very page (signin_url) and runs the job again, or asks for the login in a Chat dialog."""
    return {"status": "needs_signin", "detail": f"Google asks for a sign-in signin_url={page.url}"}


def _page():
    import browser as agent_browser
    from playwright.sync_api import sync_playwright
    agent_browser.open_browser()
    pw = sync_playwright().start()
    b = pw.chromium.connect_over_cdp(agent_browser.cdp_url())
    ctx = b.contexts[0]
    pages = [x for x in ctx.pages if not x.url.startswith(("chrome:", "devtools:"))]
    page = pages[0] if pages else ctx.new_page()
    page.set_viewport_size({"width": 1366, "height": 900})
    return pw, b, page


def _text(page) -> str:
    return page.evaluate("() => document.body.innerText")


def _drive_rows(page) -> list:
    """The shared drives listed on the page, by name."""
    return page.evaluate("""() => [...document.querySelectorAll('[data-id], [role=row], [role=gridcell], [role=listitem]')]
        .map(e => (e.innerText || '').trim().split('\\n')[0]).filter(Boolean)""")


def ensure_drive(page, name: str) -> dict:
    page.goto(SHARED, wait_until="domcontentloaded", timeout=90000)
    time.sleep(6)
    if _signin_wall(page):
        return _needs_signin(page)
    if "Shared drives" not in _text(page):
        return {"status": "failed", "detail": "Google Drive's shared-drives page did not load (" + _shot(page, "drive_unexpected") + ")"}
    if drive_id_on_page(page, name, tries=1):
        return {"status": "ok", "detail": f"the shared drive \"{name}\" already exists", "created": False}
    opener = page.get_by_text("Create a shared drive", exact=False)
    if opener.count():
        opener.first.click(force=True, timeout=10000)
    else:
        page.get_by_role("button", name="New").first.click(force=True, timeout=10000)
        time.sleep(1)
        page.get_by_text("New shared drive", exact=False).first.click(force=True, timeout=10000)
    time.sleep(2)
    dlg = page.locator("[role=dialog]").last
    box = dlg.locator("input, textarea").first
    box.click(force=True, timeout=10000)
    box.fill(name, timeout=10000)
    dlg.get_by_role("button", name="Create").click(force=True, timeout=10000)
    time.sleep(5)
    if not drive_id_on_page(page, name, tries=6):           # the list catches up a few seconds after Create
        return {"status": "failed", "detail": f"the drive \"{name}\" did not appear after Create (" + _shot(page, "drive_create") + ")"}
    return {"status": "ok", "detail": f"made the shared drive \"{name}\"", "created": True}


def drive_id_on_page(page, name: str, tries: int = 5) -> str:
    """The id of the listed shared drive called `name` (the rows carry data-id); the list is reloaded while it is
    still catching up with a drive made a moment ago. '' when it never shows."""
    for i in range(tries):
        if i:
            page.goto(SHARED, wait_until="domcontentloaded", timeout=90000)
        time.sleep(4 if i else 1)
        rows = page.evaluate("""() => [...document.querySelectorAll('[data-id][role=row]')]
            .map(e => ({id: e.getAttribute('data-id'), text: (e.innerText || '').trim().split('\\n')[0]}))""")
        hit = next((r for r in rows if str(r.get("text") or "").strip().lower() == name.lower()), None)
        if hit and re.fullmatch(r"[\w-]{10,}", str(hit["id"])):
            return str(hit["id"])
    return ""


def open_drive(page, name: str) -> bool:
    did = drive_id_on_page(page, name)
    if not did:
        return False
    page.goto(f"https://drive.google.com/drive/folders/{did}", wait_until="domcontentloaded", timeout=90000)
    time.sleep(5)
    return name.lower() in _text(page).lower()


def add_member(page, name: str, email: str, role: str = ROLE) -> dict:
    """Manage members on the open drive: add `email` with `role`. Reports what it found when a control is missing."""
    # the drive's name in the header opens a menu with "Manage members"
    head = page.locator("[role=button]").filter(has_text=name)
    if not head.count():
        head = page.get_by_text(name, exact=True)
    head.first.click(force=True, timeout=10000)
    time.sleep(1.5)
    mm = page.get_by_text("Manage members", exact=False)
    if not mm.count():
        page.keyboard.press("Escape")
        return {"status": "failed", "detail": "no 'Manage members' in the drive's menu (" + _shot(page, "drive_menu") + ")"}
    mm.first.click(force=True, timeout=10000)
    time.sleep(3)
    fr = _share_frame(page)
    if fr is None:
        return {"status": "failed", "detail": "the Manage members dialog did not open (" + _shot(page, "drive_members") + ")"}
    if email.lower() in _frame_text(fr).lower():
        _press_done(fr, page)
        return {"status": "ok", "detail": f"{email} is already a member", "added": False}
    box = fr.get_by_role("combobox", name="Add people and groups")
    if not box.count():
        box = fr.locator("input[type=text]").first
    box.click(force=True, timeout=10000)
    box.type(email, delay=20)
    time.sleep(2.5)
    page.keyboard.press("Enter")                     # takes the suggestion / makes the chip
    time.sleep(2.5)
    if email.lower() not in _frame_text(fr).lower():
        return {"status": "failed", "detail": "the member dialog did not take the e-mail (" + _shot(page, "drive_members") + ")"}
    # the role of the pending invite: the dropdown next to the chip (Drive offers Content manager by default)
    picked = _pick_role(fr, page, role) or role.lower() in _frame_text(fr).lower()
    sent = _send_invite(fr, page)
    if not sent:
        return {"status": "failed", "detail": "no Send button for the invite (" + _shot(page, "drive_members_send") + ")"}
    fr2 = _share_frame(page)
    if fr2 is not None:
        txt = _frame_text(fr2)
        if email.lower() not in txt.lower():
            return {"status": "failed", "detail": "the share did not stick (" + _shot(page, "drive_members_after") + ")"}
        _press_done(fr2, page)
    return {"status": "ok", "detail": f"added {email} as {role if picked else 'a member (the role picker was not found; check it is Content manager)'}", "added": True}


def _send_invite(fr, page) -> bool:
    """Send the pending invite; answer the "outside your organization" question when Drive asks it."""
    b = fr.get_by_role("button", name="Send", exact=True)
    if not (b.count() and b.first.is_visible()):
        b = fr.get_by_role("button", name="Share", exact=True)
    if not (b.count() and b.first.is_visible()):
        return False
    b.first.click(force=True, timeout=10000)
    time.sleep(3)
    for _ in range(3):                       # the service account is outside the domain: Drive may ask once more
        hit = False
        for label in ("Share anyway", "Send anyway", "Share", "Send", "Yes", "Confirm", "Got it"):
            for scope in (fr, page):
                try:
                    w = scope.get_by_role("button", name=label, exact=True)
                    if w.count() and w.first.is_visible():
                        w.first.click(force=True, timeout=10000)
                        time.sleep(3)
                        hit = True
                        break
                except Exception:  # noqa: BLE001
                    pass
            if hit:
                break
        if not hit:
            break
    return True


IFRAME = "iframe[src*='drivesharing/driveshare']"


def _share_frame(page):
    """Drive's sharing dialog lives in its own iframe (drivesharing/driveshare). Found through the page's DOM (a frame
    locator): over a CDP connection Playwright's frame list may not know a frame made after the connect."""
    for _ in range(25):                      # Drive can take a while to bring the dialog up
        try:
            if page.locator(IFRAME).count():
                fl = page.frame_locator(IFRAME).last
                if fl.get_by_role("combobox", name="Add people and groups").count() or "Add people" in _frame_text(fl):
                    return fl
        except Exception:  # noqa: BLE001
            pass
        time.sleep(1)
    return None


def _frame_text(fr) -> str:
    try:
        return fr.locator("body").inner_text(timeout=5000)
    except Exception:  # noqa: BLE001
        return ""


def _press_done(fr, page) -> None:
    for label in ("Done", "Cancel"):
        b = fr.get_by_role("button", name=label, exact=True)
        if b.count() and b.first.is_visible():
            b.first.click(force=True, timeout=10000)
            time.sleep(1.5)
            return
    page.keyboard.press("Escape")


def _pick_role(fr, page, role: str) -> bool:
    """Set the pending invite's role dropdown (the last role control in the dialog) to `role`."""
    ctl = fr.locator("[role=button], [role=combobox], button").filter(has_text=re.compile(r"^\s*(Manager|Content manager|Contributor|Commenter|Viewer)\s*$"))
    if not ctl.count():
        return False
    try:
        if ctl.last.inner_text().strip().lower() == role.lower():
            return True                            # Drive already offers the wanted role for the new member
    except Exception:  # noqa: BLE001
        pass
    ctl.last.click(force=True, timeout=10000)
    time.sleep(1.2)
    for scope in (fr, page):
        opt = scope.get_by_role("menuitem", name=role, exact=False)
        if not opt.count():
            opt = scope.get_by_role("option", name=role, exact=False)
        if opt.count() and opt.first.is_visible():
            opt.first.click(force=True, timeout=10000)
            time.sleep(1)
            return True
    page.keyboard.press("Escape")
    return False


def verify(name: str, tries: int = 6) -> bool:
    import shots
    for _ in range(tries):
        if shots.drive_id(refresh=True):
            return True
        time.sleep(5)
    return False


def delete_drive(page, name: str) -> bool:
    """Only for a rehearsal's throwaway drive (empty): the drive's menu -> Delete shared drive -> confirm."""
    if not open_drive(page, name):
        return False
    head = page.locator("[role=button]").filter(has_text=name)
    (head.first if head.count() else page.get_by_text(name, exact=True).first).click(force=True, timeout=10000)
    time.sleep(1.5)
    d = page.get_by_text("Delete shared drive", exact=False)
    if not d.count():
        page.keyboard.press("Escape")
        return False
    d.first.click(force=True, timeout=10000)
    time.sleep(2)
    for label in ("Delete shared drive", "Delete"):
        b = page.locator("[role=dialog]").last.get_by_role("button", name=label, exact=False)
        if b.count():
            b.first.click(force=True, timeout=10000)
            time.sleep(4)
            break
    page.goto(SHARED, wait_until="domcontentloaded", timeout=90000)
    time.sleep(5)
    return name.lower() not in _text(page).lower()


def run(name: str = "", rehearse: bool = False) -> dict:
    import shots
    name = name or shots.drive_name()
    email = shots.sa_email()
    if not email:
        return {"status": "failed", "detail": "no chat service-account key (agent/gchat.local.json sa_key): nothing to add as a member"}
    pw = b = None
    try:
        pw, b, page = _page()
        r = ensure_drive(page, name)
        if r["status"] != "ok":
            return r
        steps = [r["detail"]]
        if not open_drive(page, name):
            return {"status": "failed", "detail": "; ".join(steps) + f"; could not open \"{name}\" (" + _shot(page, "drive_open") + ")"}
        m = add_member(page, name, email)
        if m["status"] != "ok":
            return {"status": "failed", "detail": "; ".join(steps) + "; " + m["detail"]}
        steps.append(m["detail"])
        ok = verify(name) if not rehearse else _rehearsal_verify(name, email)
        if rehearse:
            steps.append("deleted the rehearsal drive" if delete_drive(page, name) else "the rehearsal drive could not be deleted; delete it by hand")
        if not ok:
            return {"status": "failed", "detail": "; ".join(steps) + "; but the service account still does not see the drive (is the e-mail right, did the share go through?)"}
        return {"status": "ok", "detail": "; ".join(steps) + "; the service account sees it: pictures on the cards from now on"}
    except Exception as e:  # noqa: BLE001
        return {"status": "failed", "detail": f"{type(e).__name__}: {str(e)[:200]}"}
    finally:
        try:
            if b:
                b.close()
            if pw:
                pw.stop()
        except Exception:  # noqa: BLE001
            pass


def _rehearsal_verify(name: str, email: str) -> bool:
    """The service account lists a drive of exactly this name (not the configured one)."""
    import shots
    import urllib.parse
    for _ in range(6):
        try:
            r = shots._api(f"{shots.DRIVE}/drives?" + urllib.parse.urlencode({"pageSize": 100, "fields": "drives(id,name)"}))
            if any(str(d.get("name") or "").lower() == name.lower() for d in r.get("drives") or []):
                return True
        except Exception:  # noqa: BLE001
            pass
        time.sleep(5)
    return False


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("step", choices=["all", "rehearse", "status"])
    a = ap.parse_args()
    sys.stdout.reconfigure(encoding="utf-8")
    import shots
    if a.step == "status":
        print(json.dumps(shots.status(), indent=1))
        return 0
    r = run(f"{shots._agent_name()} setup rehearsal", rehearse=True) if a.step == "rehearse" else run()
    print(f"SHOTS_SETUP status={r.get('status')} {r.get('detail', '')}")
    return 0 if r.get("status") == "ok" else (2 if r.get("status") == "needs_signin" else 1)


if __name__ == "__main__":
    sys.exit(main())

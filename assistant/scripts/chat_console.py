"""chat_console.py - the Google-side setup of the Chat app, done by code in the agent's own browser.

What the owner used to do by hand in two Google pages, now one job that Sage runs from a Chat button (connection
`chat-home`, setup kind `job`) or `sage.py`:

  deploy      Apps Script: paste the relay (agent/gchat_relay/Code.gs) into the project and deploy a NEW VERSION of
              the add-on deployment (the deployment id in gchat.local.json keeps working; Chat picks the new code up).
  configure   Cloud console, Chat API -> Configuration: tick Support App Home, set its trigger to onAppHome, add
              every command of home_tab.COMMANDS that is not there yet, Save.
  all         both, in that order.
  status      what the last run recorded (data/chat_console_state.json).

It drives the agent's signed-in browser (doer/browser.py, the same profile the doer uses) over CDP with Playwright,
in the wedding-web venv. It never types a password: when Google asks the owner to sign in again (the console does,
every few hours) the run stops with NEEDS_SIGNIN, puts the browser on the owner's screen at that page, and the card
in Chat says so (Control PC shows it from a phone); tapping the button again carries on. Idempotent: a command that
already exists is skipped, a box already ticked is left alone.

Last line: CHAT_CONSOLE step=<deploy|configure|all> status=<ok|needs_signin|failed> <detail>

  "%VENV%" assistant/scripts/chat_console.py all | deploy | configure | status [--no-save]
"""
from __future__ import annotations

import argparse
import base64
import json
import os
import re
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
for _p in (HERE, ROOT, ROOT / "doer"):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))
from alib import DATA, load_json, save_json  # noqa: E402

STATE = Path(os.environ.get("CHAT_CONSOLE_STATE") or (DATA / "chat_console_state.json"))
OUT = ROOT / "out" / "setup"
CONSOLE = "https://console.cloud.google.com/apis/api/chat.googleapis.com/hangouts-chat?project={project}"
SCRIPT = "https://script.google.com/home/projects/{script_id}/edit"
HOME_TRIGGER = "onAppHome"
TYPE_RADIO = {"QUICK": "Quick command", "SLASH": "Slash command"}
NAME_LABEL = {"QUICK": "Quick command name", "SLASH": "Slash command name"}


def cfg() -> dict:
    try:
        return json.loads((ROOT / "agent" / "gchat.local.json").read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        return {}


def _state() -> dict:
    return load_json(STATE, {}) or {}


def _record(**kw) -> None:
    st = _state()
    st.update(kw)
    st["ts"] = time.strftime("%Y-%m-%d %H:%M:%S")
    save_json(STATE, st)


def _shot(page, name: str) -> str:
    try:
        OUT.mkdir(parents=True, exist_ok=True)
        cdp = page.context.new_cdp_session(page)
        d = cdp.send("Page.captureScreenshot", {"format": "jpeg", "quality": 60})
        f = OUT / f"{name}.jpg"
        f.write_bytes(base64.b64decode(d["data"]))
        return str(f)
    except Exception:  # noqa: BLE001
        return ""


def _signin_wall(page) -> bool:
    return "accounts.google.com" in str(page.url)


def _needs_signin(page) -> dict:
    """Google asks the owner to sign in again. The owner is never sent to the PC: the job's runner (setup_actions._job)
    signs in from the vault on this very page (signin_url) and runs the job again, or asks for the login in a Chat dialog."""
    return {"status": "needs_signin", "detail": f"Google asks for a sign-in signin_url={page.url}"}


# ------------------------------------------------------------------ the browser
def _page():
    """(playwright, browser, page): the agent's browser over CDP, opened if it was down."""
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


JS_FIELDS = """() => {
  const lab = (el) => { const id = el.id; let t = '';
    if (id) { const l = document.querySelector('label[for="' + CSS.escape(id) + '"]'); if (l) t = l.innerText.trim(); }
    if (!t) t = (el.getAttribute('aria-label') || el.closest('label')?.innerText || '').trim(); return t.slice(0, 80); };
  return {
    cb: [...document.querySelectorAll('input[type=checkbox]')].map(el => ({label: lab(el), checked: el.checked, id: el.id})),
    radios: [...document.querySelectorAll('input[type=radio]')].map(el => ({label: lab(el), checked: el.checked, id: el.id, name: el.name})),
    inp: [...document.querySelectorAll('input:not([type=checkbox]):not([type=hidden]):not([type=radio]), textarea')].map(el => ({label: lab(el), value: (el.value||'').slice(0,80), id: el.id})),
    text: document.body.innerText,
  };
}"""


def _fields(page) -> dict:
    return page.evaluate(JS_FIELDS)


def _fill_by_id(page, el_id: str, value: str) -> None:
    loc = page.locator("#" + re.sub(r"([^\w-])", r"\\\1", el_id))
    loc.click(force=True, timeout=10000)
    loc.fill(value, timeout=10000)


# ------------------------------------------------------------------ configure
def configure(page, commands: list | None = None, save: bool = True) -> dict:
    """Support App Home + trigger + the commands table, idempotent. -> {"status", "detail", "added", "present"}."""
    import home_tab
    commands = commands if commands is not None else home_tab.COMMANDS
    c = cfg()
    page.goto(CONSOLE.format(project=c.get("project_id") or ""), wait_until="domcontentloaded", timeout=90000)
    time.sleep(10)
    if _signin_wall(page):
        return _needs_signin(page)
    f = _fields(page)
    if "Interactive features" not in f["text"]:
        _shot(page, "console_unexpected")
        return {"status": "failed", "detail": "the Chat API configuration page did not load (assistant/out/setup/console_unexpected.jpg)"}
    changed = []
    # 1. Support App Home
    home_cb = next((x for x in f["cb"] if "Support App Home" in x["label"]), None)
    if not home_cb:
        return {"status": "failed", "detail": "no 'Support App Home' box on the page"}
    if not home_cb["checked"]:
        page.locator("#" + re.sub(r"([^\w-])", r"\\\1", home_cb["id"])).click(force=True, timeout=10000)
        time.sleep(2)
        changed.append("Support App Home ticked")
        f = _fields(page)
    trig = next((x for x in f["inp"] if x["label"].strip().lower() == "app home"), None)
    if trig and trig["value"].strip() != HOME_TRIGGER:
        _fill_by_id(page, trig["id"], HOME_TRIGGER)
        changed.append(f"App home trigger = {HOME_TRIGGER}")
    # 2. the commands: what is there already (ids typed into existing rows, or names in the page text)
    f = _fields(page)
    present_ids = {x["value"].strip() for x in f["inp"] if x["label"].strip().lower() == "command id" and x["value"].strip()}
    present_names = {x["value"].strip().lower() for x in f["inp"] if x["label"].strip().lower().endswith("command name") and x["value"].strip()}
    added = []
    # 2b. a command already there under its id but with an older name (2026-11-06: "Watch my PC" became "Control PC"):
    # the name field that follows that id field, in the page's own order, is retyped
    renamed = []
    for cid, name, typ, desc in commands:
        if str(cid) not in present_ids:
            continue
        inp = f["inp"]
        idx = next((i for i, x in enumerate(inp) if x["label"].strip().lower() == "command id" and x["value"].strip() == str(cid)), None)
        if idx is None:
            continue
        nm = next((x for x in inp[idx + 1:idx + 6] if x["label"].strip().lower() == NAME_LABEL[typ].lower()), None)
        if nm and nm["value"].strip() and nm["value"].strip().lower() != name.lower():
            _fill_by_id(page, nm["id"], name[:50])
            renamed.append(f"{nm['value'].strip()} -> {name}")
            time.sleep(0.5)
    if renamed:
        changed.append("renamed: " + ", ".join(renamed))
        f = _fields(page)
        present_names = {x["value"].strip().lower() for x in f["inp"] if x["label"].strip().lower().endswith("command name") and x["value"].strip()}
    for cid, name, typ, desc in commands:
        if str(cid) in present_ids or name.lower() in present_names or re.search(r"(?m)^\s*" + re.escape(name) + r"\s*$", f["text"]):
            continue
        before = _fields(page)
        empty = [x for x in before["inp"] if x["label"].strip().lower() == "command id" and not x["value"].strip()]
        if not empty:
            add = page.get_by_role("button", name="Add a command").first
            add.scroll_into_view_if_needed(timeout=10000)
            add.click(force=True, timeout=10000)           # a ripple overlay sits on the console's buttons
            time.sleep(1.5)
            after = _fields(page)
            empty = [x for x in after["inp"] if x["label"].strip().lower() == "command id" and not x["value"].strip()]
            if not empty:
                _shot(page, "console_no_row")
                return {"status": "failed", "detail": "Add a command opened no row", "added": added}
        row_id = empty[-1]["id"]
        _fill_by_id(page, row_id, str(cid))
        now = _fields(page)
        # the type radio of the NEWEST row: the last radio group whose labels are the command types
        groups = {}
        for r in now["radios"]:
            if r["label"] in TYPE_RADIO.values():
                groups.setdefault(r["name"], []).append(r)
        last_group = groups[sorted(groups, key=lambda n: int(re.sub(r"\D", "", n) or 0))[-1]] if groups else []
        want = next((r for r in last_group if r["label"] == TYPE_RADIO[typ]), None)
        if want and not want["checked"]:
            page.locator("#" + re.sub(r"([^\w-])", r"\\\1", want["id"])).click(force=True, timeout=10000)
            time.sleep(0.8)
        now = _fields(page)
        descs = [x for x in now["inp"] if x["label"].strip().lower() == "description" and not x["value"].strip()]
        names = [x for x in now["inp"] if x["label"].strip().lower() == NAME_LABEL[typ].lower() and not x["value"].strip()]
        if not descs or not names:
            _shot(page, "console_row_fields")
            return {"status": "failed", "detail": f"the new row for {name} has no description / name field", "added": added}
        _fill_by_id(page, descs[-1]["id"], desc[:50])
        _fill_by_id(page, names[-1]["id"], name[:50])
        added.append(name)
        time.sleep(0.5)
    if (changed or added) and save:
        page.get_by_role("button", name="Save", exact=True).first.click(timeout=15000)
        time.sleep(6)
        f2 = _fields(page)
        if _signin_wall(page):
            return dict(_needs_signin(page), added=added)
        missing = [n for _, n, _, _ in commands if n.lower() not in {x["value"].strip().lower() for x in f2["inp"] if x["label"].strip().lower().endswith("command name")}
                   and not re.search(r"(?m)^\s*" + re.escape(n) + r"\s*$", f2["text"])]
        # a field the console refused shows a mat-error under it (never the page's own "Log errors to Logging" label)
        err = page.evaluate("() => [...document.querySelectorAll('mat-error, [role=alert]')].map(e => e.innerText.trim()).filter(Boolean).slice(0, 3)")
        if missing or err:
            _shot(page, "console_after_save")
            return {"status": "failed", "detail": f"after Save: missing {missing[:6]}" + (f"; the console says: {'; '.join(err)[:160]}" if err else "") + " (assistant/out/setup/console_after_save.jpg)",
                    "added": added}
    _record(app_home=True, home_trigger=HOME_TRIGGER, commands=[n for _, n, _, _ in commands], configured_at=time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime()))
    detail = "; ".join(changed + ([f"commands added: {', '.join(added)}"] if added else [])) or "already set up (nothing to change)"
    return {"status": "ok", "detail": detail, "added": added, "changed": changed}


# ------------------------------------------------------------------ deploy
def _deployment_ids(c: dict) -> list:
    """(label, id) of every relay deployment that must carry the new code: the add-on (Chat's connection) and, in pull
    mode, the web app the PC polls (its id sits in relay_webapp_url: .../macros/s/<id>/exec)."""
    out = []
    if c.get("relay_deployment_id"):
        out.append(("add-on", str(c["relay_deployment_id"])))
    m = re.search(r"/macros/s/([\w-]+)/", str(c.get("relay_webapp_url") or ""))
    if m and m.group(1) not in [d for _, d in out]:
        out.append(("web app", m.group(1)))
    return out


def deploy(page, description: str = "", only: str = "") -> dict:
    """Paste Code.gs into the Apps Script project (the Monaco model, not keystrokes) and deploy a new version of EVERY
    relay deployment (the add-on, and the pull-mode web app; `only` = one of them). -> {"status", "detail", "version"}."""
    c = cfg()
    sid = str(c.get("relay_script_id") or "")
    deps = [d for d in _deployment_ids(c) if not only or d[0] == only]
    if not sid or not deps:
        return {"status": "failed", "detail": "gchat.local.json has no relay_script_id / relay_deployment_id"}
    code = (ROOT / "agent" / "gchat_relay" / "Code.gs").read_text(encoding="utf-8").replace("\r\n", "\n")
    page.goto(SCRIPT.format(script_id=sid), wait_until="domcontentloaded", timeout=90000)
    time.sleep(8)
    if _signin_wall(page):
        return _needs_signin(page)
    page.click(".monaco-editor .view-lines", timeout=20000)
    r = page.evaluate("""(code) => { const ms = monaco.editor.getModels(); const m = ms.find(x => x.getValue().includes('TAPS_')) || ms[0];
        if (m.getValue() === code) return {same: true}; m.setValue(code); return {same: false, ok: m.getValue() === code}; }""", code)
    if not (r.get("same") or r.get("ok")):
        return {"status": "failed", "detail": "the editor did not take the new Code.gs"}
    if not r.get("same"):
        page.keyboard.press("Control+S")
        time.sleep(3)
    done = []
    version = 0
    for label, dep in deps:
        r = _deploy_one(page, dep, description or f"relay {time.strftime('%Y-%m-%d %H:%M')}")
        if r.get("status") != "ok":
            return {"status": "failed", "detail": f"{label} deployment: {r.get('detail')}" + (f" (done before it: {', '.join(done)})" if done else "")}
        version = max(version, int(r.get("version") or 0))
        done.append(f"{label} at version {r['version']}")
    _record(relay_version=version, deployed_at=time.strftime("%Y-%m-%d %H:%M"))
    return {"status": "ok", "detail": "; ".join(done), "version": version}


def _deploy_one(page, dep: str, description: str) -> dict:
    """Manage deployments -> the row whose id is `dep` -> Edit -> New version -> Deploy. -> {"status", "detail", "version"}."""
    page.get_by_role("button", name="Deploy").first.click(timeout=15000)
    time.sleep(1.5)
    page.get_by_text("Manage deployments", exact=False).first.click(timeout=15000)
    time.sleep(3)
    dlg = page.locator("[role=dialog]").last
    rows = dlg.locator("[role=listbox] [role=option], [role=list] [role=listitem], [role=tab]")
    chosen = dep[:24] in dlg.inner_text()
    for i in range(min(rows.count(), 8)):
        if chosen:
            break
        try:
            rows.nth(i).click(force=True, timeout=5000)
            time.sleep(1.2)
            chosen = dep[:24] in dlg.inner_text()
        except Exception:  # noqa: BLE001
            continue
    if not chosen:
        _shot(page, "deploy_no_row")
        page.keyboard.press("Escape")
        return {"status": "failed", "detail": f"deployment {dep[:12]}… not found in Manage deployments"}
    pencil = dlg.locator("[aria-label*='Edit' i], [data-tooltip*='Edit' i], [title*='Edit' i]")   # the pencil, not its tooltip
    if not pencil.count():
        pencil = dlg.locator("[role=button]").filter(has_text=re.compile(r"^\s*edit\s*$", re.I))
    if not pencil.count():
        page.keyboard.press("Escape")
        return {"status": "failed", "detail": "the Edit pencil was not found in Manage deployments"}
    pencil.first.click(force=True, timeout=10000)
    time.sleep(1.5)
    ver = dlg.locator("text=/^Version \\d+ on/").first
    box = ver.bounding_box()
    if not box:
        page.keyboard.press("Escape")
        return {"status": "failed", "detail": "the Version dropdown was not found"}
    page.mouse.click(box["x"] + box["width"] / 2, box["y"] + box["height"] / 2)
    time.sleep(1.2)
    page.get_by_text("New version", exact=True).first.click(force=True, timeout=10000)
    time.sleep(0.8)
    desc = dlg.locator("textarea, input[type=text]").first
    desc.click(force=True)
    desc.fill(description[:200])
    btns = dlg.get_by_role("button", name="Deploy")
    enabled = [btns.nth(i) for i in range(btns.count()) if btns.nth(i).is_enabled() and btns.nth(i).is_visible()]
    target = enabled[-1] if enabled else None                 # the dialog's footer button is the last one
    if target is None:
        page.keyboard.press("Escape")
        return {"status": "failed", "detail": "no enabled Deploy button in the dialog"}
    target.click(force=True, timeout=10000)
    txt, m = "", None
    for _ in range(30):                                      # "Updating the deployment..." can take the better part of a minute
        time.sleep(3)
        txt = page.locator("[role=dialog]").last.inner_text() if page.locator("[role=dialog]").count() else ""
        m = re.search(r"Version (\d+) on", txt)
        if "successfully" in txt.lower() and m:
            break
        if "Updating" not in txt and "successfully" not in txt.lower() and txt:
            break
    if "successfully" not in txt.lower() or not m:
        _shot(page, "deploy_result")
        page.keyboard.press("Escape")
        return {"status": "failed", "detail": "no 'successfully updated' after Deploy (assistant/out/setup/deploy_result.jpg)"}
    try:
        page.get_by_role("button", name="Done").click(timeout=5000)
    except Exception:  # noqa: BLE001
        pass
    time.sleep(1.5)
    return {"status": "ok", "detail": f"version {m.group(1)}", "version": int(m.group(1))}


def run(step: str, save: bool = True, description: str = "", only: str = "") -> dict:
    pw = b = None
    try:
        pw, b, page = _page()
        if step == "deploy":
            r = deploy(page, description, only=only)
        elif step == "configure":
            r = configure(page, save=save)
        else:
            r = deploy(page, description)
            if r.get("status") == "ok":
                r2 = configure(page, save=save)
                r = dict(r2, detail=f"{r['detail']}; {r2['detail']}")
    except Exception as e:  # noqa: BLE001
        r = {"status": "failed", "detail": f"{type(e).__name__}: {str(e)[:200]}"}
    try:                                   # the connection check reads the state file: a failure is its error, a success clears it
        _record(error="" if r.get("status") == "ok" else f"{r.get('status')}: {str(r.get('detail') or '')[:200]}", last_step=step)
    except Exception:  # noqa: BLE001
        pass
    finally:
        try:
            if b:
                b.close()
            if pw:
                pw.stop()
        except Exception:  # noqa: BLE001
            pass
    return r


def status() -> dict:
    return _state()


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("step", choices=["all", "deploy", "configure", "status"])
    ap.add_argument("--no-save", action="store_true")
    ap.add_argument("--description", default="")
    ap.add_argument("--only", default="", choices=["", "add-on", "web app"], help="deploy: only this deployment")
    a = ap.parse_args()
    sys.stdout.reconfigure(encoding="utf-8")
    if a.step == "status":
        print(json.dumps(status(), indent=1))
        return 0
    r = run(a.step, save=not a.no_save, description=a.description, only=a.only)
    print(f"CHAT_CONSOLE step={a.step} status={r.get('status')} {r.get('detail', '')}")
    return 0 if r.get("status") == "ok" else (2 if r.get("status") == "needs_signin" else 1)


if __name__ == "__main__":
    sys.exit(main())

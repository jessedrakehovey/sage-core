"""login_fill.py - sign the doer's browser in to a site, out of the model's sight, and learn how.

Called by the doer's model turn (it is one of three things that turn may run in Bash) with the
URL of the login page it is on. This script, a plain program with no model, connects to the
doer's browser over CDP, looks the site up in the agent vault (vault.py), fills username,
password and, when the vault holds a TOTP secret, the one-time code, and submits. It prints ONE
line:

  SIGNED_IN host=<host> user=<username>      the login form is gone
  ALREADY host=<host>                         no login form on the page: the session is still good
  NEEDS_CODE host=<host> kind=<sms|email|app> a code field appeared and the vault has no TOTP:
                                             the doer asks the owner for the code on a card
  NO_ENTRY host=<host>                        the vault has no login for this site: the doer stops
  FAILED <reason>                             still on the login page (wrong password, captcha, ...)

PLAYBOOKS (2026-10-06, "a script for every sign-in we use"): the first time a site signs in, the
selectors and the shape that worked (one step or two, which code field) are saved per host in
assistant/data/doer/sites.json. The next time, that playbook is used first, so a site that the
heuristics barely found is signed in the same way every time. A playbook can also be written by
hand (login_url, user_sel, pass_sel, otp_sel, submit_sel, two_step, warm). Vault-entry Notes
JSON (vault.py) still works as a per-entry override.

  python assistant/doer/login_fill.py --url https://... [--code NNNNNN]     (the doer, mid-task)
  python assistant/doer/login_fill.py --test https://...                    try one site now, on screen; saves its playbook
  python assistant/doer/login_fill.py --warm [--all] [--only host]          sign in to every playbook with warm=true
                                                                            (--all: every vault entry) off-screen; the
                                                                            Day Spine runs this daily -> sites_status.json
  python assistant/doer/login_fill.py --sites                               list playbooks and their last result

The password never appears in stdout, stderr, the transcript or any log.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(ROOT / "scripts"))
from alib import ensure_venv  # noqa: E402
ensure_venv()
import vault  # noqa: E402

SITES = vault.DATA / "sites.json"                 # playbooks, keyed by host (git-ignored with the vault)
STATUS = vault.DATA / "sites_status.json"         # the warm pass's report (connection doer-sessions)
USER_SELS = ["input[autocomplete='username']", "input[autocomplete='email']", "input[type='email']",
             "input[name*='user' i]", "input[name*='email' i]", "input[id*='user' i]", "input[id*='email' i]",
             "input[name*='login' i]", "input[id*='login' i]", "input[type='tel']", "input[type='text']"]
PASS_SEL = "input[type='password']"
OTP_SELS = ["input[autocomplete='one-time-code']", "input[name*='otp' i]", "input[id*='otp' i]", "input[name*='code' i]",
            "input[id*='code' i]", "input[name*='token' i]", "input[inputmode='numeric']"]
NEXT_TEXT = re.compile(r"^(next|continue|sign in|log in|login|submit|verify|confirm)$", re.I)
ERR_TEXT = re.compile(r"incorrect|invalid|wrong|doesn.t match|try again|not recognized|couldn.t find|unable to sign", re.I)
CAPTCHA = re.compile(r"captcha|recaptcha|hcaptcha|i.m not a robot|verify you are human", re.I)
VERIFY_PAGE = re.compile(r"verification code|verify your (?:phone|identity|account)|send (?:a |the |you a )?code|how would you like to receive", re.I)
SMS_CHOICE = re.compile(r"text me|text message|sms|send (?:a )?text", re.I)
GO_TEXT = re.compile(r"^\W*(continue|send|send code|next|submit)\W*$", re.I)
SIGNIN_LINK = re.compile(r"^\W*(sign ?in|log ?in|login|my ?account|account|my\w* log ?in)\W*$", re.I)
SEARCHY = re.compile(r"search|query|q|lookup|find", re.I)


def _is_login_field(el) -> bool:
    """A username field, not the site's search box (the DMV landing page took the email as a
    search and the filler called it signed in, 2026-10-06)."""
    try:
        attrs = " ".join(str(el.get_attribute(a) or "") for a in ("type", "name", "id", "placeholder", "aria-label", "autocomplete", "role"))
        if SEARCHY.search(attrs) or "search" in str(el.evaluate("e => (e.closest('form') && (e.closest('form').getAttribute('role') || e.closest('form').id || e.closest('form').className)) || ''")).lower():
            return False
        return True
    except Exception:
        return True


# ------------------------------------------------------------------ playbooks
def sites() -> dict:
    try:
        d = json.loads(SITES.read_text(encoding="utf-8"))
        return d if isinstance(d, dict) else {}
    except (OSError, ValueError):
        return {}


def _save_sites(d: dict) -> None:
    SITES.parent.mkdir(parents=True, exist_ok=True)
    tmp = SITES.with_suffix(".tmp")
    tmp.write_text(json.dumps(d, indent=1, ensure_ascii=False), encoding="utf-8")
    tmp.replace(SITES)


def playbook(host: str) -> dict:
    d = sites()
    if host in d:
        return d[host]
    base = vault._base(host)
    for h, pb in d.items():
        if vault._base(h) == base and pb.get("domain_wide"):
            return pb
    return {}


def _learn(host: str, used: dict, start_url: str) -> None:
    """Remember what worked for this host. Hand-written keys are kept; learned ones are refreshed."""
    d = sites()
    pb = dict(d.get(host) or {})
    for k, v in used.items():
        if v and not pb.get(k + "_fixed"):
            pb[k] = v
    pb.setdefault("login_url", start_url)
    pb.setdefault("warm", False)
    pb["last_ok"] = time.strftime("%Y-%m-%dT%H:%M:%S")
    pb["ok_count"] = int(pb.get("ok_count", 0)) + 1
    d[host] = pb
    _save_sites(d)


def _mark(host: str, result: str) -> None:
    d = sites()
    if host in d:
        d[host]["last_result"] = result[:160]
        d[host]["last_try"] = time.strftime("%Y-%m-%dT%H:%M:%S")
        _save_sites(d)


# ------------------------------------------------------------------ page helpers
def _visible(page, sel: str):
    if not sel:
        return None
    try:
        loc = page.locator(sel)
        for i in range(min(loc.count(), 8)):
            el = loc.nth(i)
            if el.is_visible():
                return el
    except Exception:
        return None
    return None


def _first_visible(page, sels: list, login_only: bool = False):
    """(element, selector) of the first visible match; login_only skips search boxes."""
    for s in sels:
        try:
            loc = page.locator(s)
            for i in range(min(loc.count(), 8)):
                el = loc.nth(i)
                if el.is_visible() and (not login_only or _is_login_field(el)):
                    return el, s
        except Exception:
            continue
    return None, ""


def _submit(page, el, submit_sel: str = "") -> None:
    btn = _visible(page, submit_sel) if submit_sel else None
    if btn:
        btn.click()
        return
    try:
        for cand in page.locator("button, input[type=submit]").all()[:20]:
            if not cand.is_visible():
                continue
            txt = (cand.inner_text() if cand.evaluate("e => e.tagName") == "BUTTON" else (cand.get_attribute("value") or "")).strip()
            if NEXT_TEXT.match(txt or ""):
                cand.click()
                return
    except Exception:
        pass
    el.press("Enter")


def _settle(page, s: float = 1.5) -> None:
    try:
        page.wait_for_load_state("domcontentloaded", timeout=8000)
    except Exception:
        pass
    time.sleep(s)


def _body_text(page) -> str:
    try:
        return page.inner_text("body")[:4000]
    except Exception:
        return ""


def _pick_page(browser, url: str):
    host = vault.host_of(url)
    pages = [p for c in browser.contexts for p in c.pages]
    if not pages:
        return None
    for p in pages:
        if host and vault.host_of(p.url) == host:
            return p
    return pages[-1]


def _code_kind(page) -> str:
    t = _body_text(page).lower()
    if "text" in t or "sms" in t or "phone" in t:
        return "sms"
    if "email" in t:
        return "email"
    return "app"


def _choose_sms(page) -> bool:
    """A 'how do you want your code' page (the DMV: Text Me / Call Me / Continue, 2026-10-06):
    pick the text-message option and press Continue so the code field appears."""
    try:
        picked = False
        for el in page.locator("label, button, input[type=radio], a, div[role=radio], span").all()[:80]:
            if not el.is_visible():
                continue
            txt = (el.inner_text() or "").strip() if el.evaluate("e => e.tagName") != "INPUT" else (el.get_attribute("aria-label") or el.get_attribute("value") or "")
            if SMS_CHOICE.search(txt or "") and len(txt) < 60:
                el.click()
                picked = True
                break
        if not picked:
            return False
        for btn in page.locator("button, input[type=submit], a[role=button]").all()[:40]:
            if btn.is_visible() and GO_TEXT.match(((btn.inner_text() if btn.evaluate("e => e.tagName") != "INPUT" else btn.get_attribute("value")) or "").strip()):
                btn.click()
                break
        _settle(page)
        return True
    except Exception:
        return False


def _click_signin_link(page) -> bool:
    """A landing page with a 'Sign in' link but no form: follow it once."""
    try:
        for a in page.locator("a, button").all()[:60]:
            if a.is_visible() and SIGNIN_LINK.match((a.inner_text() or "").strip()):
                a.click()
                _settle(page)
                return True
    except Exception:
        pass
    return False


# ------------------------------------------------------------------ the fill
def fill_page(page, url: str, code: str = "", learn: bool = True) -> str:
    host = vault.host_of(page.url) or vault.host_of(url)
    try:
        kp = vault._open()
    except Exception as e:  # noqa: BLE001
        return f"FAILED vault: {e}"
    pb0 = playbook(host)
    entry = None
    if pb0.get("entry"):                                    # a playbook may pin WHICH vault entry (two saved logins for one site)
        entry = kp.find_entries(title=pb0["entry"], group=vault._group(kp), first=True)
    entry = entry or vault.find_for_url(kp, page.url) or vault.find_for_url(kp, url)
    if not entry:
        return f"NO_ENTRY host={host}"
    pb = {**pb0, **vault.overrides(entry)}                  # vault notes win over the learned playbook
    user, pw_, otp_uri = entry.username or "", entry.password or "", entry.otp or ""
    used = {"user_sel": "", "pass_sel": "", "otp_sel": "", "two_step": False}
    start_url = page.url

    if code:
        el = _visible(page, pb.get("otp_sel", "")) or _first_visible(page, OTP_SELS)[0]
        if not el:
            return "FAILED no code field is waiting on this page"
        el.fill(code)
        _submit(page, el, pb.get("submit_sel", ""))
        _settle(page)
        if _first_visible(page, OTP_SELS)[0] and ERR_TEXT.search(_body_text(page)):
            return "FAILED the code was not accepted"
        return f"SIGNED_IN host={host} user={user}"

    # no password field on this page: a landing page. Follow its sign-in link first; only if
    # there is none and there is still no login-looking field is the session considered good.
    if not _visible(page, PASS_SEL):
        if pb.get("login_url") and vault.host_of(pb["login_url"]) == host and page.url.rstrip("/") != pb["login_url"].rstrip("/"):
            try:
                page.goto(pb["login_url"], timeout=30000, wait_until="domcontentloaded")
                _settle(page, 1.0)
            except Exception:
                pass
        if not _visible(page, PASS_SEL):
            _click_signin_link(page)
        if not _visible(page, PASS_SEL) and not _first_visible(page, USER_SELS[:9], login_only=True)[0]:
            return f"ALREADY host={host}"
    password_filled = False

    for _round in range(3):
        if CAPTCHA.search(_body_text(page)) and not _visible(page, PASS_SEL):
            return "FAILED captcha on the login page; the owner has to sign in by hand (browser.py --show)"
        pw_el = _visible(page, pb.get("pass_sel", "")) or _visible(page, PASS_SEL)
        if pw_el:
            used["pass_sel"] = pb.get("pass_sel") or PASS_SEL
        u_el = _visible(page, pb.get("user_sel", ""))
        u_sel = pb.get("user_sel", "") if u_el else ""
        if not u_el:
            u_el, u_sel = _first_visible(page, USER_SELS, login_only=True)
        if u_el and user:
            used["user_sel"] = used["user_sel"] or u_sel
            try:
                if not (u_el.input_value() or "").strip():
                    u_el.fill(user)
            except Exception:
                pass
        if pw_el:
            login_page_url = page.url
            pw_el.fill(pw_)
            password_filled = True
            _submit(page, pw_el, pb.get("submit_sel", ""))
            _settle(page)
            break
        if u_el and user:
            _submit(page, u_el, pb.get("submit_sel", ""))        # two-step login: username first
            used["two_step"] = True
            try:
                page.wait_for_selector(pb.get("pass_sel") or PASS_SEL, state="visible", timeout=8000)
            except Exception:
                _settle(page, 0.5)
            continue
        return "FAILED no login fields on this page (is it the sign-in page?)"
    if not password_filled:
        return "FAILED no password field appeared after the username step (wrong page, or the site wants a human first)"
    start_url = login_page_url

    # after the password went in
    otp_el = _visible(page, pb.get("otp_sel", ""))
    otp_sel = pb.get("otp_sel", "") if otp_el else ""
    if not otp_el:
        otp_el, otp_sel = _first_visible(page, OTP_SELS)
    if otp_el and not _visible(page, PASS_SEL):
        used["otp_sel"] = otp_sel
        if otp_uri:
            import pyotp
            try:
                totp = pyotp.parse_uri(otp_uri) if otp_uri.startswith("otpauth://") else pyotp.TOTP(otp_uri)
                otp_el.fill(totp.now())
            except Exception as e:  # noqa: BLE001
                return f"FAILED TOTP could not be generated ({type(e).__name__})"
            _submit(page, otp_el, pb.get("submit_sel", ""))
            _settle(page)
            if _first_visible(page, OTP_SELS)[0] and ERR_TEXT.search(_body_text(page)):
                return "FAILED the one-time code was rejected"
            if learn:
                _learn(host, used, start_url)
            return f"SIGNED_IN host={host} user={user}"
        if learn:
            _learn(host, used, start_url)              # the shape is known even though a code is still needed
        return f"NEEDS_CODE host={host} kind={_code_kind(page)}"
    body = _body_text(page)
    if not otp_el and VERIFY_PAGE.search(body) and not _visible(page, PASS_SEL):
        # a verification-method page: choose SMS, then the code field should appear
        if _choose_sms(page):
            otp_el, otp_sel = _first_visible(page, OTP_SELS)
            if otp_el:
                used["otp_sel"] = otp_sel
                if learn:
                    _learn(host, used, start_url)
                return f"NEEDS_CODE host={host} kind=sms"
        if learn:
            _learn(host, used, start_url)
        return f"NEEDS_CODE host={host} kind={_code_kind(page)}"
    if _visible(page, PASS_SEL):
        if ERR_TEXT.search(body):
            return "FAILED the site says the login is wrong; the vault entry needs updating"
        if CAPTCHA.search(body):
            return "FAILED captcha after the password; the owner has to sign in by hand (browser.py --show)"
        return "FAILED still on the login page after submitting"
    if learn:
        _learn(host, used, start_url)
    return f"SIGNED_IN host={host} user={user}"


def fill(url: str, cdp: str, code: str = "") -> str:
    """The doer's entry point: the page the model is on, in the running browser."""
    from playwright.sync_api import sync_playwright
    with sync_playwright() as pw:
        try:
            browser = pw.chromium.connect_over_cdp(cdp, timeout=8000)
        except Exception as e:  # noqa: BLE001
            return f"FAILED browser not reachable at {cdp} ({type(e).__name__})"
        page = _pick_page(browser, url)
        if page is None:
            return "FAILED no open page"
        line = fill_page(page, url, code)
        _mark(vault.host_of(page.url) or vault.host_of(url), line)
        return line


# ------------------------------------------------------------------ warm + test
def _login_url_for(host: str, entry) -> str:
    pb = playbook(host)
    return pb.get("login_url") or vault.overrides(entry).get("login_url") or (entry.url or f"https://{host}/")


def warm(only: str = "", everything: bool = False, show: bool = False) -> dict:
    """Open a tab per site and make sure it is signed in. Default: playbooks with warm=true;
    --all: every vault host. Captchas, codes and failures are reported, never retried."""
    import browser
    kp = vault._open()
    by_host = {}
    for e in vault._group(kp).entries:
        for h in vault._entry_hosts(e):
            by_host.setdefault(h, e)
    pbs = sites()
    targets = [h for h in by_host if (everything or (pbs.get(h) or {}).get("warm")) and (not only or only in h)]
    report = {"ts": time.strftime("%Y-%m-%dT%H:%M:%S"), "sites": {}, "ok": 0, "attention": []}
    if not targets:
        report["note"] = "no sites flagged warm=true in sites.json (use --all, or --test a site then set warm)"
        _write_status(report)
        return report
    browser.open_browser(show=show)
    from playwright.sync_api import sync_playwright
    try:
        with sync_playwright() as pw:
            b = pw.chromium.connect_over_cdp(browser.cdp_url(), timeout=8000)
            ctx = b.contexts[0] if b.contexts else b.new_context()
            for host in targets:
                page = ctx.new_page()
                try:
                    page.goto(_login_url_for(host, by_host[host]), timeout=30000, wait_until="domcontentloaded")
                    _settle(page, 1.0)
                    line = fill_page(page, page.url)
                except Exception as e:  # noqa: BLE001
                    line = f"FAILED {type(e).__name__}: {str(e)[:100]}"
                finally:
                    try:
                        page.close()
                    except Exception:
                        pass
                _mark(host, line)
                report["sites"][host] = line
                if line.startswith(("SIGNED_IN", "ALREADY")):
                    report["ok"] += 1
                else:
                    report["attention"].append(f"{host}: {line}")
    finally:
        if not show:
            browser.close_browser()
    _write_status(report)
    return report


def _write_status(report: dict) -> None:
    STATUS.parent.mkdir(parents=True, exist_ok=True)
    out = dict(report)
    out["last_ok"] = report["ts"] if not report.get("attention") else (json.loads(STATUS.read_text(encoding="utf-8")).get("last_ok", "")
                                                                          if STATUS.exists() else "")
    out["error"] = "; ".join(report.get("attention") or [])[:300]
    STATUS.write_text(json.dumps(out, indent=1, ensure_ascii=False), encoding="utf-8")


def test(url: str, entry: str = "") -> str:
    """Try one site now with the browser on screen, learn its playbook, leave the window open.
    `entry` pins a vault entry title for this host (saved in the playbook)."""
    import browser
    if entry:
        d = sites(); h = vault.host_of(url)
        d.setdefault(h, {})["entry"] = entry
        _save_sites(d)
    browser.open_browser(show=True)
    from playwright.sync_api import sync_playwright
    with sync_playwright() as pw:
        b = pw.chromium.connect_over_cdp(browser.cdp_url(), timeout=8000)
        ctx = b.contexts[0] if b.contexts else b.new_context()
        page = ctx.new_page()
        page.goto(url if "://" in url else "https://" + url, timeout=30000, wait_until="domcontentloaded")
        _settle(page, 1.0)
        line = fill_page(page, page.url)
        host = vault.host_of(page.url) or vault.host_of(url)
        _mark(host, line)
        return line + (f"\nplaybook saved for {host}: {json.dumps({k: v for k, v in playbook(host).items() if k in ('login_url', 'user_sel', 'pass_sel', 'otp_sel', 'two_step', 'warm')})}"
                       if line.startswith(("SIGNED_IN", "NEEDS_CODE")) else "")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", default="")
    ap.add_argument("--code", default="")
    ap.add_argument("--cdp", default="")
    ap.add_argument("--test", default="", help="sign in to this site now, on screen, and save its playbook")
    ap.add_argument("--entry", default="", help="with --test: the vault entry title to use for this site")
    ap.add_argument("--warm", action="store_true", help="sign in to every site flagged warm=true")
    ap.add_argument("--all", action="store_true", help="with --warm: every vault host")
    ap.add_argument("--only", default="", help="with --warm: hosts containing this text")
    ap.add_argument("--show", action="store_true", help="with --warm: on screen")
    ap.add_argument("--sites", action="store_true", help="list playbooks")
    a = ap.parse_args()
    if a.sites:
        for h, pb in sorted(sites().items()):
            print(f"{h:<36} warm={'yes' if pb.get('warm') else 'no ':<3} ok={pb.get('ok_count', 0):<3} last={str(pb.get('last_try') or pb.get('last_ok') or '')[:16]:<17} {str(pb.get('last_result') or '')[:50]}")
        print(f"({len(sites())} playbooks in {SITES})")
        return 0
    if a.test:
        print(test(a.test, a.entry))
        return 0
    if a.warm:
        r = warm(a.only, a.all, a.show)
        for h, line in r["sites"].items():
            print(f"{h:<36} {line}")
        print(f"WARM ok={r['ok']} attention={len(r['attention'])}" + (f" ({r['note']})" if r.get("note") else ""))
        return 0
    if not a.url:
        ap.error("--url is required (or --test / --warm / --sites)")
    import browser
    line = fill(a.url, a.cdp or browser.cdp_url(), re.sub(r"\D", "", a.code or "")[:10])
    print(line)
    return 0 if line.startswith(("SIGNED_IN", "ALREADY")) else (2 if line.startswith(("NO_ENTRY", "NEEDS_CODE")) else 1)


if __name__ == "__main__":
    sys.exit(main())

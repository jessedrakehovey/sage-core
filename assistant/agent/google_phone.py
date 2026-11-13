"""google_phone.py - a Google sign-in done from the phone, through ONE chat link (2026-11-13).

The owner (2026-10-21): "You need to make a portal for me to sign in remotely somehow through Google chat", and
"zero computer use". `google_api.py auth` and `drive_ingest.py auth` need a browser AT the PC (Google's desktop
client only returns to http://localhost), so a stuck Google consent waited for someone to sit down there.

Now an owner says "connect owner's Google", "sign in to Google from my phone", "reconnect my Drive" and code
answers (no model turn) with one link per consent still missing:

  {mission_control_url}/oauth/go?t=<one-time key>   tailnet only (the same address as the cards)
      -> 302 to Google's consent page for THAT account and scope set (login_hint, offline, PKCE)
  {mission_control_url}/oauth/google?code=..&state=<key>   Google sends the phone back here
      -> the code is exchanged on the PC, the signed-in address must be the account's own (never the forbidden
         domain), and the token is written where the readers already look: <google_home>/<name>.json (mail,
         calendar, Sheets when google.json lists it) or <name>.drive.json (Drive, read-only). The page says
         Connected and closes; the hourly Drive job and the agent pick the token up on their next run.

A link works once (the key is deleted the moment it is used) and only for LINK_TTL seconds. Nothing secret is
logged, said in chat or kept: the key and its PKCE verifier sit in DATA/google_phone.json until used or expired;
the token goes only into its own file. It needs a Google "Web application" OAuth client saved as
client_secret_web.json next to the desktop one, with {mission_control_url}/oauth/google as its redirect address
(GOOGLE_SETUP.md, "Sign in from the phone"); without it the reply says so instead of a link.

  python assistant/agent/google_phone.py needed          # which consents are missing (names only)
"""
from __future__ import annotations

import base64
import hashlib
import html
import json
import re
import secrets
import sys
import time
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import core  # noqa: E402
import google_api  # noqa: E402

LINK_TTL = 15 * 60
STORE = core.DATA / "google_phone.json"
GO_PATH, BACK_PATH = "/oauth/go", "/oauth/google"
AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_URL = "https://oauth2.googleapis.com/token"
USERINFO = "https://www.googleapis.com/oauth2/v3/userinfo"
DRIVE_SCOPES = ["https://www.googleapis.com/auth/drive.readonly",
                "openid", "https://www.googleapis.com/auth/userinfo.email"]

# "connect owner's Google", "sign in to Google from my phone", "reconnect my Drive", "allow Sheets access"
_NOUN = (r"(?:google|gmail|g-?mail|(?:my|his|her|our|the|\w+'s) (?:google )?drive|google drive|google calendar|google sheets?"
         r"|sheets? (?:access|scope)|(?:mail|calendar|drive) (?:account|access))")
_ASK = re.compile(r"\b(?:connect|re-?connect|sign(?:ed)? ?in|log ?in|consent|allow|authori[sz]e|hook ?up|set ?up)\b"
                  r".{0,40}\b" + _NOUN + r"\b|\b" + _NOUN + r"\b.{0,30}\b(?:sign[- ]?in|log[- ]?in|consent|re-?connect)\b", re.I)
_STRONG = re.compile(r"\b(?:connect|re-?connect|consent|authori[sz]e|allow|hook ?up|set ?up)\b", re.I)
_AGAIN = re.compile(r"\b(?:re-?connect|again|re-?sign|re-?consent|re-?do)\b", re.I)


def asked(text: str) -> bool:
    t = " ".join(str(text or "").replace("’", "'").split())
    return bool(t) and len(t) <= 300 and not t.startswith(("[", "/")) and bool(_ASK.search(t))


def client_web() -> dict:
    """The Web application OAuth client ({} when it is not saved yet)."""
    try:
        return (json.loads((google_api.HOME / "client_secret_web.json").read_text(encoding="utf-8")).get("web") or {})
    except (OSError, ValueError, AttributeError):
        return {}


def mc_url() -> str:
    try:
        sys.path.insert(0, str(core.AGENT.parent))
        import orgcfg
        u = str(orgcfg.get("mission_control_url") or "").rstrip("/")
    except Exception:  # noqa: BLE001
        u = ""
    return u if u.startswith("https://") else ""


def redirect_uri() -> str:
    return mc_url() + BACK_PATH if mc_url() else ""


def token_path(name: str, kind: str) -> Path:
    return google_api.HOME / (f"{name}.drive.json" if kind == "drive" else f"{name}.json")


def scopes(name: str, kind: str) -> list:
    return list(DRIVE_SCOPES) if kind == "drive" else google_api.scopes_for(name)


def _granted(path: Path) -> set:
    try:
        sc = json.loads(path.read_text(encoding="utf-8")).get("scopes") or []
    except (OSError, ValueError, AttributeError):
        return set()
    return set(sc.split() if isinstance(sc, str) else sc)


def needed(text: str = "", person_key: str = "") -> list:
    """[(account name, kind)] the ask is about: the consents still missing (a token absent, or short of a scope
    google.json now declares); with "reconnect"/"again" also the connected ones. Narrowed by who is named
    ("owner's", "my") and what ("Drive", "sheet", "mail"/"calendar")."""
    t = str(text or "").lower().replace("’", "'")
    accts = google_api.accounts()
    named = {a.get("person") for a in accts.values() if a.get("person") and re.search(rf"\b{re.escape(str(a['person']))}\b", t)}
    if not named and person_key and re.search(r"\b(?:my|me|mine|i)\b", t):
        named = {person_key}
    want_drive = bool(re.search(r"\bdrive\b", t))
    want_agent = bool(re.search(r"\b(?:g-?mail|mail|calendar|sheets?|pricing)\b", t))
    want_sheet = bool(re.search(r"\b(?:sheets?|pricing)\b", t))
    again = bool(_AGAIN.search(t))
    out = []
    for name, a in accts.items():
        if named and a.get("person") not in named:
            continue
        kinds = (["agent"] if want_agent or not want_drive else []) + (["drive"] if a.get("drive") and (want_drive or not want_agent) else [])
        for kind in kinds:
            if kind == "agent" and want_sheet and google_api.SHEETS_SCOPE not in scopes(name, kind):
                continue
            path = token_path(name, kind)
            missing = not path.exists() or not set(scopes(name, kind)) <= _granted(path)
            if missing or again:
                out.append((name, kind))
    return out


def _load() -> dict:
    d = core.load(STORE, {})
    now = time.time()
    return {k: v for k, v in (d if isinstance(d, dict) else {}).items() if float((v or {}).get("exp") or 0) > now}


def mint(name: str, kind: str, by: str = "") -> str:
    """One single-use link for `name`'s `kind` consent ('' when the web client or Mission Control is missing)."""
    if name not in google_api.accounts() or kind not in ("agent", "drive") or not client_web() or not mc_url():
        return ""
    key = secrets.token_urlsafe(24)
    d = _load()
    d[key] = {"name": name, "kind": kind, "by": by, "exp": time.time() + LINK_TTL, "verifier": secrets.token_urlsafe(48)}
    core.save(STORE, d)
    return f"{mc_url()}{GO_PATH}?t={key}"


def _label(name: str, kind: str) -> str:
    a = google_api.accounts().get(name) or {}
    who = str(a.get("person") or "").capitalize()
    what = "Google Drive" if kind == "drive" else (
        "mail, calendar and Sheets" if google_api.SHEETS_SCOPE in scopes(name, kind) else "mail and calendar")
    return f"{who}'s {what} ({a.get('address', name)})".strip()


def reply(text: str, person_key: str = "") -> str:
    """The chat answer to a Google sign-in ask: the links, or the one line that says why there are none."""
    todo = needed(text, person_key)
    if not todo:
        if not _STRONG.search(str(text or "")):
            return ""        # "log in to Google Drive and grab the file": not a connection ask; the agent answers
        return ("Those Google accounts are already connected. Say \"reconnect\" with the account (for example "
                "\"reconnect my Drive\") if one stopped working and I'll send a fresh sign-in link.")
    if not mc_url():
        return "I can't make a phone sign-in link: Mission Control's address isn't set for this setup yet."
    if not client_web():
        return ("The phone sign-in isn't switched on yet: Google needs a web sign-in client for this PC, saved as "
                f"client_secret_web.json next to the others, with {redirect_uri()} as its address "
                "(GOOGLE_SETUP.md, \"Sign in from the phone\"). Once it's there, ask me again.")
    lines = [f"Here's your Google sign-in, right on your phone. Each link works once, for the next {LINK_TTL // 60} minutes:"]
    for name, kind in todo:
        lines.append(f"- {_label(name, kind)}: {mint(name, kind, person_key)}")
    lines.append("Open it, pick that account and tap Allow. The page says Connected and I carry on from there.")
    return "\n".join(lines)


# ------------------------------------------------------------------ the two Mission Control pages
def _b64(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).decode().rstrip("=")


def _page(title: str, line: str, close: bool = False) -> str:
    js = "<script>setTimeout(function(){window.close()},2500)</script>" if close else ""
    return ("<!doctype html><html lang=\"en\"><head><meta charset=\"utf-8\"><meta name=\"viewport\" "
            "content=\"width=device-width, initial-scale=1\"><title>" + html.escape(title) + "</title><style>"
            "body{margin:0;background:#050B07;color:#FBFFFA;font:16px/1.5 Poppins,system-ui,sans-serif;padding:32px 16px}"
            "main{max-width:520px;margin:0 auto}h1{font:500 24px \"Playfair Display\",Georgia,serif}</style></head>"
            f"<body><main><h1>{html.escape(title)}</h1><p>{html.escape(line)}</p></main>{js}</body></html>")


def go(key: str) -> tuple:
    """GET /oauth/go?t=<key> -> (302, Google's consent URL) or (code, page)."""
    rec = _load().get(str(key or ""))
    if not rec:
        return 410, _page("This link has expired", "Ask in Chat for a fresh Google sign-in link.")
    c = client_web()
    if not c:
        return 503, _page("Not switched on yet", "The phone sign-in client is missing on the PC.")
    q = {"client_id": c.get("client_id", ""), "redirect_uri": redirect_uri(), "response_type": "code",
         "scope": " ".join(scopes(rec["name"], rec["kind"])), "access_type": "offline", "prompt": "consent",
         "include_granted_scopes": "false", "state": key,
         "login_hint": (google_api.accounts().get(rec["name"]) or {}).get("address", ""),
         "code_challenge": _b64(hashlib.sha256(rec["verifier"].encode()).digest()), "code_challenge_method": "S256"}
    return 302, AUTH_URL + "?" + urllib.parse.urlencode(q)


def _post_form(url: str, data: dict) -> dict:
    """POST a form, JSON back. Tests replace this."""
    req = urllib.request.Request(url, data=urllib.parse.urlencode(data).encode(), method="POST",
                                 headers={"Content-Type": "application/x-www-form-urlencoded"})
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read().decode() or "{}")


def _userinfo(access_token: str) -> dict:
    """Who signed in. Tests replace this."""
    req = urllib.request.Request(USERINFO, headers={"Authorization": "Bearer " + access_token})
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read().decode() or "{}")


def _log(line: str) -> None:
    try:
        with (core.DATA / "google_phone.log").open("a", encoding="utf-8") as f:
            f.write(time.strftime("%Y-%m-%d %H:%M:%S ") + line + "\n")
    except OSError:
        pass


def back(state: str, code: str = "", error: str = "") -> tuple:
    """GET /oauth/google?state=..&code=.. -> (http code, page). The key is spent on the first return either way."""
    d = _load()
    rec = d.pop(str(state or ""), None)
    core.save(STORE, d)
    if not rec:
        return 410, _page("This link has expired", "Ask in Chat for a fresh Google sign-in link.")
    name, kind = rec["name"], rec["kind"]
    if error or not code:
        _log(f"{name} {kind}: not allowed ({str(error)[:40] or 'no code'})")
        return 200, _page("Nothing was connected", "Google didn't give access. Ask in Chat for a new link to try again.")
    c = client_web()
    try:
        tok = _post_form(TOKEN_URL, {"code": code, "client_id": c.get("client_id", ""), "client_secret": c.get("client_secret", ""),
                                     "redirect_uri": redirect_uri(), "grant_type": "authorization_code",
                                     "code_verifier": rec["verifier"]})
        got = str(_userinfo(str(tok.get("access_token") or "")).get("email") or "").lower()
    except Exception as e:  # noqa: BLE001
        _log(f"{name} {kind}: exchange failed ({type(e).__name__})")
        return 502, _page("Google didn't finish", "Something failed on the way back. Ask in Chat for a new link.")
    want = str((google_api.accounts().get(name) or {}).get("address") or "").lower()
    if got != want or got.endswith(google_api.FORBIDDEN_DOMAIN):
        _log(f"{name} {kind}: refused, signed in as another account")
        return 200, _page("Wrong account", f"That was {got or 'another account'}, but this link is for {want}. Nothing was stored.")
    if not tok.get("refresh_token"):
        _log(f"{name} {kind}: no refresh token returned")
        return 502, _page("Google didn't finish", "Google gave no lasting access. Ask in Chat for a new link.")
    exp = datetime.now(timezone.utc) + timedelta(seconds=int(tok.get("expires_in") or 3600))
    granted = str(tok.get("scope") or " ".join(scopes(name, kind))).split()
    # the authorized-user file google.oauth2.credentials reads back (google_api._creds, drive_ingest)
    body = {"token": tok.get("access_token"), "refresh_token": tok.get("refresh_token"), "token_uri": TOKEN_URL,
            "client_id": c.get("client_id"), "client_secret": c.get("client_secret"), "scopes": granted,
            "universe_domain": "googleapis.com", "account": "", "expiry": exp.strftime("%Y-%m-%dT%H:%M:%SZ")}
    path = token_path(name, kind)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(body), encoding="utf-8")
    _log(f"{name} {kind}: connected ({len(granted)} scopes)")
    return 200, _page("Connected", f"{want} is connected. You can close this page; I carry on from here.", close=True)


if __name__ == "__main__":
    if sys.argv[1:2] == ["needed"]:
        print(json.dumps(needed(" ".join(sys.argv[2:])), indent=1))
    else:
        print(__doc__)

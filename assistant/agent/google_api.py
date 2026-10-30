"""google_api.py - the household's Google calendars and mailboxes, for the agent.

One OAuth client owned by the personal side, one consent per person/mailbox, tokens under
~/.example-mail/google/ (the personal side's credential folder; never the property side's
`gmail` connector). Scopes are deliberately narrow:

  gmail.readonly      search and read mail. There is NO send or draft scope on these tokens:
                      outward mail stays on the approved-card path (assistant/README.md).
  calendar.events     read events, and add events to the owner's own calendar.
  calendar.readonly   list which calendars exist.
  spreadsheets        ONLY on an account that lists it under `extra_scopes` in google.json
                      (2026-10-18: the owner-films, for the pricing sheet). sheet_get reads cells;
                      sheet_set writes cells with valueInputOption=RAW, so a value is stored as
                      typed and never becomes a formula (a string starting with "=" is refused).
                      Nothing calls sheet_set except drain.py running a `sheet_set` exec ticket
                      that the owner approved on a card (every cell and value verbatim on the card).

Accounts are named in google.json. `auth` refuses to store a token whose signed-in address
is not the one the account names, so a wrong-account consent cannot slip in. A token is
loaded with the scopes it was GRANTED (stored in its file), so adding a scope here never
breaks an account until its owner re-consents: `auth <name>` again, one click.

  python assistant/agent/google_api.py auth the owner-personal    # opens a browser; the owner consents
  python assistant/agent/google_api.py status
  python assistant/agent/google_api.py calendar --days 7
  python assistant/agent/google_api.py mail the owner-films "is:unread newer_than:2d"
  python assistant/agent/google_api.py scopes the owner-films      # what the stored token may do
  python assistant/agent/google_api.py sheet the owner-films <spreadsheet_id> "season_multipliers!A1:D13"   # read-only
"""
from __future__ import annotations

import base64
import json
import os
import re
import sys
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import core  # noqa: E402

CONFIG = core.AGENT / "google.json"
def _google_home() -> Path:
    """AGENT_GOOGLE_HOME, else org.json -> google_home (a new organization's ~/.sage/<org>/google), else this machine's folder."""
    if os.environ.get("AGENT_GOOGLE_HOME"):
        return Path(os.environ["AGENT_GOOGLE_HOME"])
    try:
        sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
        import orgcfg
        if orgcfg.get("google_home"):
            return Path(os.path.expanduser(str(orgcfg.get("google_home"))))
    except Exception:  # noqa: BLE001
        pass
    return Path.home() / ".example-mail" / "google"


HOME = _google_home()
CLIENT = HOME / "client_secret.json"
SCOPES = ["https://www.googleapis.com/auth/gmail.readonly",
          "https://www.googleapis.com/auth/calendar.events",
          "https://www.googleapis.com/auth/calendar.readonly",
          "openid", "https://www.googleapis.com/auth/userinfo.email"]
SHEETS_SCOPE = "https://www.googleapis.com/auth/spreadsheets"
EXTRA_SCOPES_ALLOWED = {SHEETS_SCOPE}                # the only scopes google.json may add per account
FORBIDDEN_DOMAIN = "other-business.invalid"       # the property identity never crosses over
CAL = "https://www.googleapis.com/calendar/v3"
GM = "https://gmail.googleapis.com/gmail/v1/users/me"
SH = "https://sheets.googleapis.com/v4/spreadsheets"


def accounts() -> dict:
    return (core.load(CONFIG, {}).get("accounts") or {})


def scopes_for(name: str) -> list:
    """The scopes `auth <name>` asks for: the shared read scopes plus the account's own
    `extra_scopes` (google.json), filtered to the ones this module knows how to use."""
    extra = [s for s in ((accounts().get(name) or {}).get("extra_scopes") or []) if s in EXTRA_SCOPES_ALLOWED]
    return SCOPES + [s for s in extra if s not in SCOPES]


def _token_path(name: str) -> Path:
    return HOME / f"{name}.json"


def connected(name: str) -> bool:
    return _token_path(name).exists()


def granted_scopes(name: str) -> list:
    """The scopes stored with the token (what the owner actually consented to). [] when not connected."""
    try:
        sc = json.loads(_token_path(name).read_text(encoding="utf-8")).get("scopes") or []
    except (OSError, ValueError, AttributeError):
        return []
    return sc.split() if isinstance(sc, str) else [str(s) for s in sc]


def has_scope(name: str, scope: str) -> bool:
    return scope in granted_scopes(name)


def sheet_ready(name: str) -> tuple[bool, str]:
    """(True, "") when `name` may read and write Google Sheets; else (False, the one line that fixes it)."""
    if name not in accounts():
        return False, f"unknown Google account {name!r}; known: {sorted(accounts())}"
    if SHEETS_SCOPE not in scopes_for(name):
        return False, f"SHEET_SCOPE_MISSING: {name} is not declared for Sheets (extra_scopes in assistant/agent/google.json)"
    if not connected(name):
        return False, f"SHEET_SCOPE_MISSING: {name} is not connected; run: python assistant/agent/google_api.py auth {name}"
    if not has_scope(name, SHEETS_SCOPE):
        return False, (f"SHEET_SCOPE_MISSING: {name}'s token has no Sheets scope yet; the owner re-consents once: "
                       f"python assistant/agent/google_api.py auth {name}")
    return True, ""


def accounts_of(person_key: str) -> list:
    return [n for n, a in accounts().items() if a.get("person") == person_key and connected(n)]


def _creds(name: str):
    from google.oauth2.credentials import Credentials
    from google.auth.transport.requests import Request
    # scopes=None: the token is loaded with the scopes stored in its file (what was granted), so
    # a refresh asks Google for exactly those, never for a scope the owner has not consented to yet
    creds = Credentials.from_authorized_user_file(str(_token_path(name)), granted_scopes(name) or None)
    if not creds.valid:
        creds.refresh(Request())
        _token_path(name).write_text(creds.to_json(), encoding="utf-8")
    return creds


def _api(name: str, url: str, body=None, method: str = "") -> dict:
    """One Google REST call as `name`. Tests replace this function."""
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method or ("POST" if data else "GET"),
                                 headers={"Authorization": "Bearer " + _creds(name).token,
                                          "Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=45) as resp:
        txt = resp.read().decode()
        return json.loads(txt) if txt.strip() else {}


def auth(name: str) -> int:
    acct = accounts().get(name)
    if not acct:
        print(f"unknown account {name!r}; known: {list(accounts())}")
        return 2
    if not CLIENT.exists():
        print(f"missing {CLIENT} (the OAuth client; see assistant/agent/GOOGLE_SETUP.md)")
        return 2
    from google_auth_oauthlib.flow import InstalledAppFlow
    flow = InstalledAppFlow.from_client_secrets_file(str(CLIENT), scopes_for(name))
    creds = flow.run_local_server(port=0, login_hint=acct["address"], prompt="consent", access_type="offline")
    req = urllib.request.Request("https://www.googleapis.com/oauth2/v3/userinfo",
                                 headers={"Authorization": "Bearer " + creds.token})
    with urllib.request.urlopen(req, timeout=30) as resp:
        got = str(json.loads(resp.read().decode()).get("email") or "").lower()
    if got != acct["address"].lower() or got.endswith(FORBIDDEN_DOMAIN):
        print(f"REFUSED: signed in as {got or '?'}, but {name} must be {acct['address']}. Nothing stored.")
        return 1
    HOME.mkdir(parents=True, exist_ok=True)
    _token_path(name).write_text(creds.to_json(), encoding="utf-8")
    print(f"CONNECTED {name} ({got})")
    return 0


# ------------------------------------------------------------------ calendar
def _rfc(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def events(days: int = 14, who: str = "both", query: str = "", start: datetime | None = None) -> list:
    """Events from every connected calendar of `who` (a person key or 'both'), soonest first."""
    start = start or datetime.now().astimezone()
    out = []
    for name, a in accounts().items():
        if not connected(name) or (who != "both" and a.get("person") != who):
            continue
        for cal in a.get("calendars") or ["primary"]:
            q = {"timeMin": _rfc(start), "timeMax": _rfc(start + timedelta(days=max(1, min(int(days), 120)))),
                 "singleEvents": "true", "orderBy": "startTime", "maxResults": "100"}
            if query:
                q["q"] = query
            try:
                r = _api(name, f"{CAL}/calendars/{urllib.parse.quote(cal, safe='')}/events?" + urllib.parse.urlencode(q))
            except Exception as e:  # noqa: BLE001
                out.append({"account": name, "error": f"{type(e).__name__}: {str(e)[:120]}"})
                continue
            for ev in r.get("items") or []:
                if ev.get("status") == "cancelled":
                    continue
                s, e = ev.get("start") or {}, ev.get("end") or {}
                out.append({"account": name, "owner": a.get("person"), "id": ev.get("id"),
                            "title": ev.get("summary") or "(no title)",
                            "start": s.get("dateTime") or s.get("date"), "end": e.get("dateTime") or e.get("date"),
                            "all_day": "date" in s, "location": ev.get("location", ""),
                            "notes": str(ev.get("description") or "")[:300],
                            "with": [x.get("email") for x in (ev.get("attendees") or [])][:8],
                            "guests": [x.get("displayName") or x.get("email") for x in (ev.get("attendees") or [])
                                       if not x.get("resource") and x.get("responseStatus") != "declined"][:12],
                            "organizer": str((ev.get("organizer") or {}).get("email") or "").lower(),
                            "meet": meet_link(ev)})
    out.sort(key=lambda x: str(x.get("start") or ""))
    return out


_VIDEO = re.compile(r"https://(?:meet\.google\.com|[\w.-]*zoom\.us|teams\.microsoft\.com)/[^\s<>\"')]+")


def meet_link(ev: dict) -> str:
    """The event's video link: Google Meet, else a video entry point, else a Meet/Zoom/Teams URL
    written in the location or description (the CRM puts its link there). '' when none."""
    if ev.get("hangoutLink"):
        return str(ev["hangoutLink"])
    for p in (ev.get("conferenceData") or {}).get("entryPoints") or []:
        if p.get("entryPointType") == "video" and p.get("uri"):
            return str(p["uri"])
    m = _VIDEO.search(f"{ev.get('location') or ''} {ev.get('description') or ''}")
    return m.group(0).rstrip(".,;:") if m else ""


def create_event(name: str, title: str, start: str, end: str = "", location: str = "", notes: str = "",
                 all_day: bool = False, event_id: str = "") -> dict:
    """Add an event to the owner's own primary calendar. No attendees and sendUpdates=none, so
    nothing is ever sent to anyone. -> the created event {"id", "link"}. `event_id` (lowercase hex)
    makes a retry safe: if a timed-out attempt did land, Google answers 409 and that is success."""
    tz = core.profile().get("timezone", "America/Los_Angeles")
    if all_day:
        d0 = start[:10]
        d1 = end[:10] if end else (datetime.strptime(d0, "%Y-%m-%d") + timedelta(days=1)).strftime("%Y-%m-%d")
        when = {"start": {"date": d0}, "end": {"date": d1}}
    else:
        s = datetime.strptime(start[:16].replace("T", " "), "%Y-%m-%d %H:%M")
        e = datetime.strptime(end[:16].replace("T", " "), "%Y-%m-%d %H:%M") if end else s + timedelta(hours=1)
        if e <= s:
            raise ValueError("end must be after start")
        when = {"start": {"dateTime": s.strftime("%Y-%m-%dT%H:%M:00"), "timeZone": tz},
                "end": {"dateTime": e.strftime("%Y-%m-%dT%H:%M:00"), "timeZone": tz}}
    body = {"summary": title[:200], "location": location[:300], "description": notes[:2000], **when}
    if event_id:
        body["id"] = event_id
    try:
        r = _api(name, f"{CAL}/calendars/primary/events?sendUpdates=none", body)
    except urllib.error.HTTPError as e:
        if event_id and e.code == 409:
            return {"id": event_id, "link": ""}
        raise
    return {"id": r.get("id") or event_id, "link": r.get("htmlLink", "")}


def _when(start: str, end: str, all_day: bool, tz: str) -> dict:
    if all_day:
        d0 = start[:10]
        d1 = end[:10] if end else (datetime.strptime(d0, "%Y-%m-%d") + timedelta(days=1)).strftime("%Y-%m-%d")
        return {"start": {"date": d0}, "end": {"date": d1}}
    s = datetime.strptime(start[:16].replace("T", " "), "%Y-%m-%d %H:%M")
    e = datetime.strptime(end[:16].replace("T", " "), "%Y-%m-%d %H:%M") if end else s + timedelta(hours=1)
    if e <= s:
        raise ValueError("end must be after start")
    return {"start": {"dateTime": s.strftime("%Y-%m-%dT%H:%M:00"), "timeZone": tz},
            "end": {"dateTime": e.strftime("%Y-%m-%dT%H:%M:00"), "timeZone": tz}}


def update_event(name: str, event_id: str, title: str = "", start: str = "", end: str = "", location: str = None,
                 notes: str = None, all_day: bool = None) -> dict:
    """Change an event this agent created (the owner 2026-10-07: "give yourself permission to edit and
    remove events"; the all-day placeholders it could not fix piled up). Only the given fields
    change; a new start without an end keeps the old length when both are timed. sendUpdates=none."""
    tz = core.profile().get("timezone", "America/Los_Angeles")
    body = {}
    if title:
        body["summary"] = title[:200]
    if location is not None:
        body["location"] = str(location)[:300]
    if notes is not None:
        body["description"] = str(notes)[:2000]
    if start:
        body.update(_when(start, end, bool(all_day), tz))
    if not body:
        raise ValueError("nothing to change")
    r = _api(name, f"{CAL}/calendars/primary/events/{urllib.parse.quote(event_id, safe='')}?sendUpdates=none", body, method="PATCH")
    return {"id": r.get("id") or event_id, "link": r.get("htmlLink", "")}


def get_event(name: str, event_id: str, calendar: str = "primary") -> dict:
    return _api(name, f"{CAL}/calendars/{urllib.parse.quote(calendar, safe='')}/events/{urllib.parse.quote(event_id, safe='')}")


def add_guests(name: str, event_id: str, emails: list, calendar: str = "primary") -> dict:
    """Put people on an event as guests WITHOUT emailing them (sendUpdates=none): the event shows up
    on their own Google Calendar, and a guest joins the event's Google Meet without waiting at the door
    (2026-10-18: a team member has to be let into every vision-call Meet by hand). Only the account's own events
    (organizer = this account) can take guests; -> {"id", "added": [...], "already": [...]}."""
    ev = get_event(name, event_id, calendar)
    have = {str(a.get("email") or "").lower() for a in (ev.get("attendees") or [])}
    want = [e.strip() for e in emails if e and "@" in e]
    new = [e for e in want if e.lower() not in have]
    if not new:
        return {"id": event_id, "added": [], "already": want}
    attendees = list(ev.get("attendees") or []) + [{"email": e} for e in new]
    r = _api(name, f"{CAL}/calendars/{urllib.parse.quote(calendar, safe='')}/events/{urllib.parse.quote(event_id, safe='')}?sendUpdates=none",
             {"attendees": attendees}, method="PATCH")
    return {"id": r.get("id") or event_id, "added": new, "already": [e for e in want if e.lower() in have], "link": r.get("htmlLink", "")}


def remove_event(name: str, event_id: str) -> bool:
    """Undo for create_event: takes back an event this agent added."""
    try:
        _api(name, f"{CAL}/calendars/primary/events/{urllib.parse.quote(event_id, safe='')}?sendUpdates=none", method="DELETE")
        return True
    except Exception:  # noqa: BLE001
        return False


# ------------------------------------------------------------------ mail (read-only)
def _hdr(msg: dict, key: str) -> str:
    for h in ((msg.get("payload") or {}).get("headers") or []):
        if h.get("name", "").lower() == key:
            return h.get("value", "")
    return ""


def mail_search(name: str, query: str, max_results: int = 10) -> list:
    q = urllib.parse.urlencode({"q": query, "maxResults": str(max(1, min(int(max_results), 25)))})
    ids = [m["id"] for m in (_api(name, f"{GM}/messages?{q}").get("messages") or [])]
    out = []
    for mid in ids:
        m = _api(name, f"{GM}/messages/{mid}?format=metadata&metadataHeaders=From&metadataHeaders=To"
                       "&metadataHeaders=Subject&metadataHeaders=Date")
        out.append({"account": name, "id": mid, "thread": m.get("threadId"), "from": _hdr(m, "from"), "to": _hdr(m, "to"),
                    "subject": _hdr(m, "subject"), "date": _hdr(m, "date"), "snippet": m.get("snippet", ""),
                    "unread": "UNREAD" in (m.get("labelIds") or [])})
    return out


def _text(part: dict) -> str:
    if part.get("mimeType") == "text/plain" and (part.get("body") or {}).get("data"):
        return base64.urlsafe_b64decode(part["body"]["data"] + "==").decode("utf-8", "replace")
    for p in part.get("parts") or []:
        t = _text(p)
        if t:
            return t
    if part.get("mimeType") == "text/html" and (part.get("body") or {}).get("data"):
        html = base64.urlsafe_b64decode(part["body"]["data"] + "==").decode("utf-8", "replace")
        return re.sub(r"\s+", " ", re.sub(r"<(?:style|script)[^>]*>.*?</(?:style|script)>|<[^>]+>", " ", html, flags=re.S | re.I))
    return ""


def _parts(part: dict) -> list:
    out = [part]
    for p in part.get("parts") or []:
        out.extend(_parts(p))
    return out


_DRIVE_LINK = re.compile(r"https?://(?:drive|docs)\.google\.com/(?:file/d/|open\?id=|uc\?(?:[^\s<>\"]*&)?id=|document/d/|spreadsheets/d/|presentation/d/)([A-Za-z0-9_-]{20,})")


def drive_links(text: str) -> list:
    """Google Drive file ids linked in a message body (Gmail turns a big attachment into a Drive
    link: Corwin's 37 MB WAVs, 2026-10-12). Order kept, duplicates dropped."""
    seen, out = set(), []
    for m in _DRIVE_LINK.finditer(str(text or "")):
        if m.group(1) not in seen:
            seen.add(m.group(1))
            out.append(m.group(1))
    return out


def mail_attachments(name: str, message_id: str) -> list:
    """The real attachments of one message: [{filename, mime, bytes, attachment_id}]."""
    m = _api(name, f"{GM}/messages/{urllib.parse.quote(message_id, safe='')}?format=full")
    out = []
    for p in _parts(m.get("payload") or {}):
        b = p.get("body") or {}
        if p.get("filename") and b.get("attachmentId"):
            out.append({"filename": p["filename"], "mime": p.get("mimeType", ""), "bytes": int(b.get("size") or 0),
                        "attachment_id": b["attachmentId"]})
    return out


def mail_read(name: str, message_id: str, max_chars: int = 6000) -> dict:
    m = _api(name, f"{GM}/messages/{urllib.parse.quote(message_id, safe='')}?format=full")
    body = _text(m.get("payload") or {}).strip()
    atts = [{"filename": p["filename"], "bytes": int((p.get("body") or {}).get("size") or 0)}
            for p in _parts(m.get("payload") or {}) if p.get("filename") and (p.get("body") or {}).get("attachmentId")]
    return {"account": name, "id": message_id, "from": _hdr(m, "from"), "to": _hdr(m, "to"), "cc": _hdr(m, "cc"),
            "subject": _hdr(m, "subject"), "date": _hdr(m, "date"),
            "body": body[:max_chars], "attachments": atts, "drive_links": drive_links(body),
            "fetch_hint": ("python assistant/scripts/mail_attach.py " + name + " " + message_id + " --out <folder>  (saves the "
                           "attachments and the Drive-linked files to this PC)") if atts or drive_links(body) else ""}


# ------------------------------------------------------------------ sheets (cells, card-gated writes)
_A1 = re.compile(r"^(?:'(?P<qtab>[^']+)'!|(?P<tab>[^'!:]+)!)?(?P<cell>[A-Z]{1,3}[1-9][0-9]{0,6})$")


def a1_split(ref: str) -> tuple[str, str]:
    """'season_multipliers!D2' -> ('season_multipliers', 'D2'); 'D2' -> ('', 'D2'). ValueError when not ONE cell."""
    m = _A1.match(str(ref or "").strip())
    if not m:
        raise ValueError(f"{ref!r} is not a single cell reference like Tab!D2")
    return (m.group("qtab") or m.group("tab") or "").strip(), m.group("cell")


def a1_join(tab: str, cell: str) -> str:
    tab = str(tab or "").strip()
    if not tab:
        return cell
    return (f"'{tab}'" if re.search(r"[^A-Za-z0-9_]", tab) else tab) + "!" + cell


def cell_text(v) -> str:
    """One cell as the words a person reads on a card and as the key a before-check compares on:
    booleans TRUE/FALSE, numbers without a trailing .0 (1.0 -> '1', 0.95 -> '0.95'), text trimmed
    (numeric text normalised the same way), empty -> ''."""
    if v is None:
        return ""
    if isinstance(v, bool):
        return "TRUE" if v else "FALSE"
    if isinstance(v, (int, float)):
        return format(float(v), ".10g")
    s = str(v).strip()
    if s.upper() in ("TRUE", "FALSE"):
        return s.upper()
    try:
        return format(float(s.replace(",", "")), ".10g") if re.fullmatch(r"[-+]?[\d,]*\.?\d+(?:[eE][-+]?\d+)?", s) else s
    except ValueError:
        return s


def is_formula(v) -> bool:
    return isinstance(v, str) and v.lstrip().startswith("=")


def sheet_get(name: str, spreadsheet_id: str, rng: str, render: str = "UNFORMATTED_VALUE") -> list:
    """The values of a range as rows (list of lists; trailing empty cells are absent; [] when empty).
    UNFORMATTED_VALUE: numbers come back as numbers, TRUE/FALSE as booleans, so a currency or
    percent format never changes what a before-check compares."""
    q = urllib.parse.urlencode({"valueRenderOption": render, "majorDimension": "ROWS"})
    r = _api(name, f"{SH}/{urllib.parse.quote(str(spreadsheet_id), safe='')}/values/{urllib.parse.quote(str(rng), safe='')}?{q}")
    return r.get("values") or []


def sheet_set(name: str, spreadsheet_id: str, rng: str, values: list) -> str:
    """Write `values` (rows) into `rng` with valueInputOption=RAW: every value is stored exactly as
    given (a number stays a number, a string stays text), nothing is parsed, so no formula can ever
    be created here; a string that starts with '=' is refused outright. -> the updated A1 range.
    Only drain.py calls this, for a `sheet_set` ticket the owner approved on a card."""
    ok, why = sheet_ready(name)
    if not ok:
        raise PermissionError(why)
    rows = values if values and isinstance(values[0], (list, tuple)) else [values]
    for row in rows:
        for v in row:
            if is_formula(v):
                raise ValueError(f"refused: {str(v)[:40]!r} starts with '=' and sheet_set never writes a formula")
            if not isinstance(v, (str, int, float, bool)) or v is None:
                raise ValueError(f"refused: a cell value must be text, a number or TRUE/FALSE, not {type(v).__name__}")
    q = urllib.parse.urlencode({"valueInputOption": "RAW", "includeValuesInResponse": "true", "responseValueRenderOption": "UNFORMATTED_VALUE"})
    r = _api(name, f"{SH}/{urllib.parse.quote(str(spreadsheet_id), safe='')}/values/{urllib.parse.quote(str(rng), safe='')}?{q}",
             {"range": str(rng), "majorDimension": "ROWS", "values": [list(row) for row in rows]}, method="PUT")
    return str(r.get("updatedRange") or rng)


def status() -> dict:
    return {n: {"address": a.get("address"), "person": a.get("person"), "connected": connected(n),
                "sheets": has_scope(n, SHEETS_SCOPE)}
            for n, a in accounts().items()}


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    a = sys.argv[1:]
    if a[:1] == ["auth"] and len(a) == 2:
        sys.exit(auth(a[1]))
    if a[:1] == ["health"] and len(a) == 2:               # conn_check: silent = not set up, exit 1 = dead
        if not connected(a[1]):
            sys.exit(0)
        try:
            _api(a[1], f"{CAL}/users/me/calendarList?maxResults=1")
            print(f"connected as {accounts()[a[1]]['address']}")
            sys.exit(0)
        except Exception as e:  # noqa: BLE001
            print(f"{type(e).__name__}")
            sys.exit(1)
    if a[:1] == ["calendar"]:
        days = int(a[a.index("--days") + 1]) if "--days" in a else 7
        print(json.dumps(events(days), ensure_ascii=False, indent=1))
    elif a[:1] == ["mail"] and len(a) >= 3:
        print(json.dumps(mail_search(a[1], a[2]), ensure_ascii=False, indent=1))
    elif a[:1] == ["scopes"] and len(a) == 2:
        ok, why = sheet_ready(a[1])
        print(json.dumps({"account": a[1], "connected": connected(a[1]), "granted": granted_scopes(a[1]),
                          "asked_on_next_auth": scopes_for(a[1]), "sheets": ok, "sheets_fix": why}, indent=1))
    elif a[:1] == ["sheet"] and len(a) == 4:                 # read-only; there is no write command on purpose
        ok, why = sheet_ready(a[1])
        if not ok:
            print(why)
            sys.exit(1)
        print(json.dumps(sheet_get(a[1], a[2], a[3]), ensure_ascii=False, indent=1))
    else:
        print(json.dumps({"client": CLIENT.exists(), "accounts": status()}, indent=1))

"""space_reader.py - read the shared Google Chat spaces the way example does, and fetch voice notes.

A Chat app is only told about DMs and @mentions. example reads her whole space anyway: a
member's own sign-in (read-only Chat scopes) lists every message, so people talking to each
other are seen too, and attachments (voice notes, photos) can be downloaded. This is that,
for Sage: one owner's consent (owner3@example.invalid, the Workspace account that is in every
space with the app), read-only scopes only, polled by the bridge.

  python assistant/agent/space_reader.py auth        # the owner signs in once (read-only scopes)
  python assistant/agent/space_reader.py status
  python assistant/agent/space_reader.py spaces      # the spaces the app is in

Voice notes are transcribed on this machine with scripts/transcribe.py (faster-whisper,
no network); the audio never leaves the PC.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import core  # noqa: E402
import gchat  # noqa: E402

import google_api  # noqa: E402

HOME = google_api.HOME          # AGENT_GOOGLE_HOME, else org.json -> google_home, else this machine's folder
CLIENT = HOME / "client_secret_chat.json"
TOKEN = HOME / "chat_reader.json"
SCOPES = ["https://www.googleapis.com/auth/chat.messages.readonly",
          "https://www.googleapis.com/auth/chat.spaces.readonly",
          "openid", "https://www.googleapis.com/auth/userinfo.email"]


def _org(key: str, default: str = "") -> str:
    """assistant/org.json (orgcfg.py): the Workspace account that reads the spaces is the organization's own."""
    try:
        sys.path.insert(0, str(core.AGENT.parent))
        import orgcfg
        return str(orgcfg.get(key) or default)
    except Exception:  # noqa: BLE001
        return default


READER_ADDRESS = _org("chat_reader") or _org("alert_from")
WATERMARKS = core.DATA / "space_watermarks.json"
SEEN = core.DATA / "seen_messages.json"
ATTACH = core.DATA / "attachments"
WHISPER_PY = Path.home() / ".venvs" / "whisper" / "Scripts" / "python.exe"
AUDIO_EXT = (".m4a", ".mp3", ".ogg", ".oga", ".opus", ".wav", ".aac", ".amr", ".webm", ".mp4", ".mov", ".m4v")
IMAGE_EXT = (".jpg", ".jpeg", ".png", ".gif", ".webp", ".heic")
CHAT = "https://chat.googleapis.com/v1"


def configured() -> bool:
    return TOKEN.exists() and gchat.configured()


def _creds():
    from google.oauth2.credentials import Credentials
    from google.auth.transport.requests import Request
    creds = Credentials.from_authorized_user_file(str(TOKEN), SCOPES)
    if not creds.valid:
        creds.refresh(Request())
        TOKEN.write_text(creds.to_json(), encoding="utf-8")
    return creds


def _get(url: str, raw: bool = False, timeout: int = 20):
    """A read with one quick retry: Google's socket occasionally times out (under 1 % of
    polls on day one, each one freezing the loop for the full timeout)."""
    last = None
    for attempt in range(2):
        req = urllib.request.Request(url, headers={"Authorization": "Bearer " + _creds().token})
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                data = resp.read()
            return data if raw else (json.loads(data.decode()) if data.strip() else {})
        except urllib.error.HTTPError:
            raise
        except Exception as e:  # noqa: BLE001
            last = e
            time.sleep(1)
    raise last


def auth() -> int:
    if not CLIENT.exists():
        print(f"missing {CLIENT}")
        return 2
    from google_auth_oauthlib.flow import InstalledAppFlow
    flow = InstalledAppFlow.from_client_secrets_file(str(CLIENT), SCOPES)
    creds = flow.run_local_server(port=0, login_hint=READER_ADDRESS, prompt="consent", access_type="offline")
    req = urllib.request.Request("https://www.googleapis.com/oauth2/v3/userinfo", headers={"Authorization": "Bearer " + creds.token})
    with urllib.request.urlopen(req, timeout=30) as resp:
        got = str(json.loads(resp.read().decode()).get("email") or "").lower()
    if got != READER_ADDRESS:
        print(f"REFUSED: signed in as {got or '?'}, the reader must be {READER_ADDRESS}. Nothing stored.")
        return 1
    HOME.mkdir(parents=True, exist_ok=True)
    TOKEN.write_text(creds.to_json(), encoding="utf-8")
    print(f"CONNECTED reader ({got})")
    return 0


# ------------------------------------------------------------------ spaces + messages
_SPACES = {"at": 0.0, "rows": []}
NEW_SPACE_WINDOW_S = 30 * 60     # a space with no watermark yet (added to since the last list) is read this far back


def invalidate() -> None:
    """Forget the cached space list: the app was just added to (or removed from) a space (gchat.record_added)."""
    _SPACES.update(at=0.0, rows=[])


def spaces(max_age_s: int = 600) -> list:
    """Shared spaces the app is a member of (app auth; DMs are left to the event stream).
    Cached ten minutes: the bridge polls every few seconds and the list rarely changes."""
    if _SPACES["rows"] and time.time() - _SPACES["at"] < max_age_s:
        return _SPACES["rows"]
    out, page = [], ""
    while True:
        r = gchat._req(f"{CHAT}/spaces?pageSize=100" + (f"&pageToken={page}" if page else ""))
        for sp in r.get("spaces") or []:
            if sp.get("spaceType") == "SPACE" or sp.get("type") == "ROOM":
                out.append({"name": sp["name"], "title": sp.get("displayName", "")})
        page = r.get("nextPageToken") or ""
        if not page:
            _SPACES.update(at=time.time(), rows=out)
            return out


def _seen() -> list:
    return core.load(SEEN, [])


def mark_seen(names: list) -> None:
    s = _seen()
    s += [n for n in names if n and n not in s]
    core.save(SEEN, s[-600:])


def _addressed(msg: dict, text: str) -> tuple:
    """(direct?, why) with no model: an @mention of the app, or its name in the text."""
    name = core.profile().get("name", "Agent")
    for a in msg.get("annotations") or []:
        u = ((a.get("userMention") or {}).get("user") or {})
        if a.get("type") == "USER_MENTION" and u.get("type") == "BOT":
            return True, "@mention"
    if re.search(rf"(^\s*@?{re.escape(name)}\b|\b{re.escape(name)}[,:?!]|\bhey,?\s+{re.escape(name)}\b|\b{re.escape(name)}\b[^.\n]{{0,80}}\?)", text, re.I):
        return True, "named"
    return False, "people talking among themselves"


def poll() -> list:
    """New human messages in every shared space since the watermark, oldest first, each in the
    bridge's message shape plus `direct`, `why`, `msg_name` and `attachments`."""
    if not configured():
        return []
    wm = core.load(WATERMARKS, {})
    seen = set(_seen())
    out = []
    for sp in spaces():
        # a space seen for the first time (just added to) is read NEW_SPACE_WINDOW_S back, so a line written right
        # after the add is answered even when the list refreshed late (2026-10-27)
        since = wm.get(sp["name"]) or time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() - NEW_SPACE_WINDOW_S))
        q = urllib.parse.urlencode({"pageSize": "50", "orderBy": "createTime asc", "filter": f'createTime > "{since}"'})
        try:
            r = _get(f"{CHAT}/{sp['name']}/messages?{q}")
        except Exception as e:  # noqa: BLE001
            out.append({"error": f"{sp['title'] or sp['name']}: {type(e).__name__}: {str(e)[:100]}"})
            continue
        newest = since
        for m in r.get("messages") or []:
            newest = max(newest, m.get("createTime") or "")
            snd = m.get("sender") or {}
            if snd.get("type") != "HUMAN" or m.get("name") in seen:
                continue
            text = str(m.get("argumentText") or m.get("text") or "").strip()
            direct, why = _addressed(m, text)
            out.append({"channel": "gchat", "space": sp["name"], "space_title": sp["title"], "dm": False,
                        "thread": (m.get("thread") or {}).get("name", ""), "msg_name": m.get("name"),
                        "email": str(snd.get("email") or "").lower(), "chat_user": snd.get("name", ""),
                        "name": snd.get("displayName", ""), "text": text, "direct": direct, "why": why,
                        "attachments": m.get("attachment") or []})
        wm[sp["name"]] = newest
    core.save(WATERMARKS, wm)
    return out


# ------------------------------------------------------------------ attachments
def download(att: dict, stamp: str = "") -> Path | None:
    ref = (att.get("attachmentDataRef") or {}).get("resourceName")
    if not ref or not TOKEN.exists():
        return None
    ATTACH.mkdir(parents=True, exist_ok=True)
    name = re.sub(r"[^A-Za-z0-9._-]+", "_", att.get("contentName") or "file")
    path = ATTACH / f"{stamp or time.strftime('%Y%m%d-%H%M%S')}_{name}"[:120]
    try:
        path.write_bytes(_get(f"{CHAT}/media/{urllib.parse.quote(ref, safe='/')}?alt=media", raw=True))
        return path
    except Exception:  # noqa: BLE001
        return None


def transcribe(path: Path, timeout: int = 900) -> str:
    """Local faster-whisper through scripts/transcribe.py. '' on failure."""
    if not WHISPER_PY.exists():
        return ""
    try:
        subprocess.run([str(WHISPER_PY), str(core.VAULT / "scripts" / "transcribe.py"), str(path), "--out-dir", str(path.parent)],
                       capture_output=True, text=True, timeout=timeout, cwd=str(core.VAULT), creationflags=core.NO_WINDOW)
        txt = path.with_suffix(".txt")
        return txt.read_text(encoding="utf-8", errors="replace").strip() if txt.exists() else ""
    except (OSError, subprocess.TimeoutExpired):
        return ""


def describe_attachments(atts: list, who: str, stamp: str = "") -> list:
    """Lines for the brain: a voice note becomes its transcript, a photo a path to look at."""
    parts = []
    for att in atts or []:
        ct = str(att.get("contentType") or "").split(";")[0].lower()
        cname = att.get("contentName") or ""
        if att.get("driveDataRef") and not (att.get("attachmentDataRef") or {}).get("resourceName"):
            parts.append(f"[{who} shared a Google Drive file{' (' + cname + ')' if cname else ''}; files hosted on Drive cannot be opened from Chat. Ask for it as a direct upload.]")
            continue
        p = download(att, stamp)
        if not p:
            parts.append(f"[{who} sent an attachment{' (' + cname + ')' if cname else ''} that could not be fetched"
                         + ("" if TOKEN.exists() else "; the space reader is not connected (space_reader.py auth)") + "]")
            continue
        sfx = p.suffix.lower()
        if ct.startswith(("audio", "video")) or sfx in AUDIO_EXT:
            tr = transcribe(p)
            parts.append(f"[voice note from {who}, transcribed on this machine: \"{tr}\"]" if tr
                         else f"[voice note from {who} saved at {p}; transcription failed]")
        elif ct.startswith("image") or sfx in IMAGE_EXT:
            parts.append(f"[{who} sent a photo. LOOK AT IT with your Read tool before answering: {p}]")
        else:
            # a document: into the library (Raw/Uploads) so the wiki can cite it, and the brain reads it from there
            import resources
            parts.append(resources.describe(resources.from_attachment(p, who), who))
    return parts


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    a = sys.argv[1:]
    if a[:1] == ["auth"]:
        sys.exit(auth())
    if a[:1] == ["spaces"]:
        print(json.dumps(spaces(), indent=1))
    else:
        print(json.dumps({"client": CLIENT.exists(), "reader_token": TOKEN.exists(), "app": gchat.configured(),
                          "whisper": WHISPER_PY.exists()}, indent=1))

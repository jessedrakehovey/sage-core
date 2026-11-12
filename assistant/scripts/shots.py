"""shots.py - a screenshot an owner can see on a Chat card, hosted in Google Drive for a few minutes.

Google Chat shows a picture on a card only from an https address its servers can fetch, so a screenshot the
agent takes on this PC has to be hosted somewhere. This is Google-native and needs no public port on the PC:
the agent's service account (the same key that posts to Chat) uploads the picture into a SHARED DRIVE the
owner made for it and added the service account to (a service account has no Drive storage of its own, so a
shared drive is the one place it may put a file), shares the file by link, and the card shows it from
lh3.googleusercontent.com. A sweep deletes every file past its lifetime (default 15 minutes); the index of
what is hosted is <data>/shots_index.json.

Setup, once, by the owner (two clicks in Drive): New shared drive named as config shots.drive_name (default
"<agent name> screenshots"), then Manage members -> add the service account's e-mail as Content manager. Nothing
to paste: the drive is found by name. `status()` says whether it is ready; `doctor` shows it.

Config (config.json -> shots): backend "drive" | "off", drive_name, folder_id (a folder inside the shared
drive; "" = the drive's root), ttl_s. Org-free: the agent's name comes from the profile, the key from the chat
channel's config.

  python assistant/scripts/shots.py status
  python assistant/scripts/shots.py publish <file.png|jpg> [--ttl 900]
  python assistant/scripts/shots.py sweep
  python assistant/scripts/shots.py desktop <out.jpg>        # a picture of the main screen (the computer lane)
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(HERE))
from alib import DATA, config, load_json, save_json  # noqa: E402

INDEX = Path(os.environ.get("SHOTS_INDEX") or (DATA / "shots_index.json"))
DRIVE = "https://www.googleapis.com/drive/v3"
UPLOAD = "https://www.googleapis.com/upload/drive/v3/files?uploadType=multipart&supportsAllDrives=true&fields=id"
SCOPE = ["https://www.googleapis.com/auth/drive"]
DEFAULT_TTL_S = 900
MAX_WIDTH = 1000
_TOK = {"v": "", "exp": 0.0}


def _cfg() -> dict:
    c = {"backend": "drive", "drive_name": "", "folder_id": "", "ttl_s": DEFAULT_TTL_S}
    try:
        c.update(config().get("shots") or {})
    except Exception:  # noqa: BLE001
        pass
    return c


def enabled() -> bool:
    return str(_cfg().get("backend") or "off").lower() == "drive" and bool(_sa_key())


def _agent_name() -> str:
    try:
        return str((load_json(ROOT / "agent" / "profile.json", {}) or {}).get("name") or "Agent")
    except Exception:  # noqa: BLE001
        return "Agent"


def drive_name() -> str:
    return str(_cfg().get("drive_name") or f"{_agent_name()} screenshots")


def _sa_key() -> str:
    """The chat channel's service-account key file (agent/gchat.local.json -> sa_key), or ''."""
    try:
        d = json.loads((ROOT / "agent" / "gchat.local.json").read_text(encoding="utf-8-sig"))
        p = os.path.expanduser(str(d.get("sa_key") or ""))
        return p if p and Path(p).exists() else ""
    except (OSError, ValueError):
        return ""


def sa_email() -> str:
    try:
        return str(json.loads(Path(_sa_key()).read_text(encoding="utf-8")).get("client_email") or "")
    except (OSError, ValueError):
        return ""


def _token() -> str:
    if _TOK["v"] and time.time() < _TOK["exp"] - 60:
        return _TOK["v"]
    from google.oauth2 import service_account
    import google.auth.transport.requests
    creds = service_account.Credentials.from_service_account_file(_sa_key(), scopes=SCOPE)
    creds.refresh(google.auth.transport.requests.Request())
    _TOK["v"] = creds.token
    _TOK["exp"] = time.time() + 3000
    return _TOK["v"]


def _api(url: str, body: bytes | None = None, method: str = "", content_type: str = "application/json", timeout: int = 30) -> dict:
    """One Drive REST call as the service account. Tests replace this."""
    req = urllib.request.Request(url, data=body, method=method or ("POST" if body is not None else "GET"),
                                 headers={"Authorization": "Bearer " + _token(), "Content-Type": content_type})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        raw = r.read().decode("utf-8", "replace")
        return json.loads(raw) if raw.strip() else {}


# ------------------------------------------------------------------ the shared drive
def _state() -> dict:
    return load_json(INDEX, {}) or {}


def _save(d: dict) -> None:
    save_json(INDEX, d)


def drive_id(refresh: bool = False) -> str:
    """The shared drive named drive_name(), found through drives.list (the service account sees the shared drives
    it was added to) and remembered; '' when none has that name yet."""
    st = _state()
    want = drive_name().strip().lower()
    if not refresh and st.get("drive_id") and str(st.get("drive_name") or "").lower() == want:
        return str(st["drive_id"])
    try:
        r = _api(f"{DRIVE}/drives?" + urllib.parse.urlencode({"pageSize": 100, "fields": "drives(id,name)"}))
    except Exception:  # noqa: BLE001
        return ""
    for d in r.get("drives") or []:
        if str(d.get("name") or "").strip().lower() == want:
            st["drive_id"], st["drive_name"] = str(d["id"]), drive_name()
            _save(st)
            return str(d["id"])
    return ""


def ready() -> bool:
    return enabled() and bool(drive_id())


def status() -> dict:
    c = _cfg()
    if str(c.get("backend") or "off").lower() != "drive":
        return {"ok": False, "backend": c.get("backend"), "hint": "config.json shots.backend is off: cards carry no picture"}
    if not _sa_key():
        return {"ok": False, "backend": "drive", "hint": "no chat service-account key (agent/gchat.local.json sa_key)"}
    did = drive_id(refresh=True)
    if not did:
        return {"ok": False, "backend": "drive", "drive_name": drive_name(), "sa": sa_email(),
                "hint": f"make a shared drive named \"{drive_name()}\" in Google Drive and add {sa_email()} as Content manager"}
    return {"ok": True, "backend": "drive", "drive_name": drive_name(), "drive_id": did, "hosted": len(_state().get("files") or {})}


# ------------------------------------------------------------------ publish / sweep
def image_url(file_id: str) -> str:
    return f"https://lh3.googleusercontent.com/d/{file_id}"


def shrink(path: Path, max_width: int = MAX_WIDTH) -> tuple:
    """(bytes, mime) of the picture, re-encoded as a JPEG no wider than max_width (a card thumbnail, not a 4K PNG)."""
    try:
        from PIL import Image
        im = Image.open(path)
        im = im.convert("RGB")
        if im.width > max_width:
            im = im.resize((max_width, max(1, int(im.height * max_width / im.width))))
        import io
        buf = io.BytesIO()
        im.save(buf, "JPEG", quality=70, optimize=True)
        return buf.getvalue(), "image/jpeg"
    except Exception:  # noqa: BLE001
        data = path.read_bytes()
        return data, ("image/png" if path.suffix.lower() == ".png" else "image/jpeg")


def publish(path, ttl_s: int | None = None) -> str:
    """Host one picture; -> its card url, or '' when hosting is off, not set up, or failed. Never raises."""
    p = Path(path)
    if not p.is_file() or not enabled():
        return ""
    did = drive_id()
    if not did:
        return ""
    ttl = int(ttl_s if ttl_s is not None else _cfg().get("ttl_s") or DEFAULT_TTL_S)
    try:
        data, mime = shrink(p)
        parent = str(_cfg().get("folder_id") or did)
        meta = json.dumps({"name": f"shot-{int(time.time())}-{p.stem}"[:80] + (".jpg" if mime == "image/jpeg" else p.suffix),
                           "mimeType": mime, "parents": [parent]}).encode("utf-8")
        b = "shot-boundary-" + os.urandom(6).hex()
        body = (f"--{b}\r\nContent-Type: application/json; charset=UTF-8\r\n\r\n".encode() + meta
                + f"\r\n--{b}\r\nContent-Type: {mime}\r\n\r\n".encode() + data + f"\r\n--{b}--".encode())
        r = _api(UPLOAD, body, "POST", f"multipart/related; boundary={b}", timeout=60)
        fid = str(r.get("id") or "")
        if not fid:
            return ""
        _api(f"{DRIVE}/files/{fid}/permissions?supportsAllDrives=true",
             json.dumps({"role": "reader", "type": "anyone"}).encode("utf-8"), "POST")
        st = _state()
        files = st.setdefault("files", {})
        files[fid] = {"exp": time.time() + ttl, "src": p.name}
        _save(st)
        return image_url(fid)
    except Exception as e:  # noqa: BLE001
        _log(f"publish failed for {p.name}: {type(e).__name__}: {str(e)[:160]}")
        return ""


def sweep(now: float | None = None) -> int:
    """Delete every hosted picture past its lifetime. -> how many went."""
    st = _state()
    files = st.get("files") or {}
    now = time.time() if now is None else now
    gone = 0
    for fid, row in list(files.items()):
        if float(row.get("exp") or 0) > now:
            continue
        try:
            _api(f"{DRIVE}/files/{fid}?supportsAllDrives=true", None, "DELETE")
        except urllib.error.HTTPError as e:
            if e.code not in (404, 410):
                continue
        except Exception:  # noqa: BLE001
            continue
        files.pop(fid, None)
        gone += 1
    st["files"] = files
    _save(st)
    return gone


# ------------------------------------------------------------------ the desktop (the computer lane)
def capture_desktop(out_path) -> bool:
    """A picture of the main screen into out_path (JPEG). False when the screen cannot be read (locked, no session)."""
    try:
        from PIL import ImageGrab
        im = ImageGrab.grab()
        im = im.convert("RGB")
        if im.width > MAX_WIDTH:
            im = im.resize((MAX_WIDTH, max(1, int(im.height * MAX_WIDTH / im.width))))
        Path(out_path).parent.mkdir(parents=True, exist_ok=True)
        im.save(str(out_path), "JPEG", quality=70)
        return True
    except ImportError:
        return _capture_desktop_ps(out_path)
    except Exception as e:  # noqa: BLE001
        _log(f"desktop capture failed: {type(e).__name__}")
        return False


def _capture_desktop_ps(out_path) -> bool:
    """The same picture through Windows' own drawing library, for an interpreter without Pillow (2026-11-04: every
    computer job's card had no picture, 'ModuleNotFoundError')."""
    import subprocess
    out = Path(out_path)
    out.parent.mkdir(parents=True, exist_ok=True)
    ps = ("Add-Type -AssemblyName System.Windows.Forms,System.Drawing; $b=[System.Windows.Forms.Screen]::PrimaryScreen.Bounds; "
          "$bmp=New-Object System.Drawing.Bitmap $b.Width,$b.Height; $g=[System.Drawing.Graphics]::FromImage($bmp); "
          "$g.CopyFromScreen($b.Location,[System.Drawing.Point]::Empty,$b.Size); "
          # a locked or sleeping screen reads back one flat colour (black): no picture then (2026-11-08)
          "$seen=@{}; for($y=8;$y -lt $b.Height;$y+=[int]($b.Height/12)){for($x=8;$x -lt $b.Width;$x+=[int]($b.Width/12)){"
          "$seen[$bmp.GetPixel($x,$y).ToArgb()]=1}}; if($seen.Count -le 1){$g.Dispose(); $bmp.Dispose(); exit 3}; "
          f"$bmp.Save('{out}',[System.Drawing.Imaging.ImageFormat]::Jpeg); $g.Dispose(); $bmp.Dispose()")
    try:
        r = subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command", ps], capture_output=True, timeout=30,
                           creationflags=0x08000000 if os.name == "nt" else 0)
        return r.returncode == 0 and out.is_file() and out.stat().st_size > 1000
    except Exception as e:  # noqa: BLE001
        _log(f"desktop capture (powershell) failed: {type(e).__name__}")
        return False


def _log(msg: str) -> None:
    try:
        log = Path(os.environ.get("WV_LOG_DIR") or (Path.home() / ".claude")) / "shots.log"
        log.parent.mkdir(parents=True, exist_ok=True)
        with open(log, "a", encoding="utf-8") as f:
            f.write(time.strftime("%Y-%m-%d %H:%M:%S") + "  " + msg + "\n")
    except OSError:
        pass


def main() -> int:
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("status")
    p = sub.add_parser("publish"); p.add_argument("file"); p.add_argument("--ttl", type=int, default=None)
    sub.add_parser("sweep")
    d = sub.add_parser("desktop"); d.add_argument("out")
    a = ap.parse_args()
    sys.stdout.reconfigure(encoding="utf-8")
    if a.cmd == "status":
        print(json.dumps(status(), indent=1))
    elif a.cmd == "publish":
        print(publish(a.file, a.ttl) or "NOT_PUBLISHED")
    elif a.cmd == "sweep":
        gone = sweep()
        redrawn = 0
        try:                                       # a job card whose picture just expired is redrawn without it
            sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "doer"))
            import livecard
            redrawn = livecard.expire_pictures()
        except Exception:  # noqa: BLE001
            pass
        print(f"SWEPT {gone}" + (f" cards_redrawn={redrawn}" if redrawn else ""))
    else:
        print("OK" if capture_desktop(a.out) else "FAILED")
    return 0


if __name__ == "__main__":
    sys.exit(main())

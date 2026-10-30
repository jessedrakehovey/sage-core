"""mail_attach.py - save what an email carries onto this PC, with evidence (built 2026-10-15).

the owner, 2026-10-15: "Make sure you build everything you need so you can do stuff like this in
the future", after Sage could not pull Corwin's fixed SFX track out of Gmail. Two shapes of
"attachment" exist and both are handled:

  * a real Gmail attachment            -> users.messages.attachments.get (gmail.readonly)
  * a Google Drive link in the body    -> drive.readonly on the same account's Drive token
    (Gmail turns anything big into a Drive link; Corwin's 37 MB WAVs all arrived that way)

Nothing is ever overwritten: an existing name gets " (2)", " (3)"... Nothing is deleted.
A folder that looks like camera media (Video/, Audio/, Card N/, DCIM) is refused: the footage
folders are never written by the assistant. Every saved file is reported with its byte size
and sha256 (and Drive's own md5 when Drive supplied one, checked against the bytes), and the
same rows are appended to an evidence file the doer's report-back verifies
(assistant/doer/computer.py), so "saved" is a checked fact, not a claim.

  python assistant/scripts/mail_attach.py <account> <message_id> --out <folder> [--only <name>...]
                                          [--evidence <path>] [--list]

  account: the owner-personal | the owner-films | owner   (assistant/agent/google.json)
  --list   show what the message carries, save nothing
  Prints one JSON line per file: {"saved": path, "bytes": n, "sha256": ..., "source": "gmail|drive", ...}
  and a final SAVED=<n> line (or NOTHING_TO_SAVE).
"""
from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import re
import sys
import time
import urllib.parse
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent
VAULT = HERE.parent.parent
sys.path.insert(0, str(VAULT / "assistant" / "agent"))
sys.path.insert(0, str(VAULT / "scripts" / "drive_ingest"))

FOOTAGE_MARKERS = [r"(^|[\\/])Video([\\/]|$)", r"(^|[\\/])Audio([\\/]|$)", r"[\\/]Card \d+([\\/]|$)",
                   r"[\\/]DCIM([\\/]|$)", r"[\\/]PRIVATE[\\/](M4ROOT|AVCHD)", r"[\\/]CLIPS?[\\/]"]
DRIVE_FILES = "https://www.googleapis.com/drive/v3/files/"


def _safe_name(name: str) -> str:
    name = re.sub(r"[\\/:*?\"<>|\x00-\x1f]", "_", str(name or "")).strip().strip(".")
    return name[:180] or "attachment"


def _free_path(folder: Path, name: str) -> Path:
    """Never overwrite: the next free 'name (n).ext'."""
    p = folder / name
    if not p.exists():
        return p
    stem, ext = os.path.splitext(name)
    for n in range(2, 1000):
        q = folder / f"{stem} ({n}){ext}"
        if not q.exists():
            return q
    raise RuntimeError("no free file name")


def is_footage_folder(path: str) -> bool:
    s = re.sub(r"\\", "/", str(path))
    return any(re.search(m, s, re.I) for m in FOOTAGE_MARKERS)


def _sha256(p: Path) -> str:
    h = hashlib.sha256()
    with p.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _md5(p: Path) -> str:
    h = hashlib.md5()
    with p.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _evidence_path(explicit: str = "") -> Path | None:
    if explicit:
        return Path(explicit)
    tid = os.environ.get("DOER_COMPUTER_TICKET", "")
    if re.fullmatch(r"[0-9a-f]{16}", tid):
        return VAULT / "assistant" / "out" / "doer" / tid / "evidence.json"
    return None


def add_evidence(path: Path | None, rows: list, note: str = "") -> None:
    """Append file rows to the doer's evidence file (created if missing). Never raises."""
    if not path or not rows:
        return
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        ev = json.loads(path.read_text(encoding="utf-8")) if path.is_file() else {}
        if not isinstance(ev, dict):
            ev = {}
        files = ev.setdefault("files", [])
        for r in rows:
            files.append({"path": r["saved"], "bytes": r["bytes"], "sha256": r["sha256"], "from": r.get("source", "")})
        if note:
            ev.setdefault("notes", []).append(note)
        path.write_text(json.dumps(ev, indent=1, ensure_ascii=False), encoding="utf-8")
    except (OSError, ValueError):
        pass


# ------------------------------------------------------------------ fetchers
def gmail_items(account: str, message_id: str) -> tuple:
    """-> (attachments, drive_file_ids, headers) for one message."""
    import google_api as g
    atts = g.mail_attachments(account, message_id)
    m = g.mail_read(account, message_id, max_chars=200000)
    return atts, list(m.get("drive_links") or []), {"from": m.get("from"), "subject": m.get("subject"), "date": m.get("date")}


def save_gmail_attachment(account: str, message_id: str, att: dict, folder: Path) -> dict:
    import google_api as g
    url = f"{g.GM}/messages/{urllib.parse.quote(message_id, safe='')}/attachments/{urllib.parse.quote(att['attachment_id'], safe='')}"
    data = g._api(account, url)
    raw = base64.urlsafe_b64decode(str(data.get("data") or "") + "==")
    dest = _free_path(folder, _safe_name(att["filename"]))
    dest.write_bytes(raw)
    return {"saved": str(dest), "bytes": dest.stat().st_size, "sha256": _sha256(dest), "source": "gmail",
            "name": att["filename"], "declared_bytes": att.get("bytes")}


def drive_meta(account: str, file_id: str) -> dict:
    import drive_ingest as d
    return d._get(account, f"{DRIVE_FILES}{file_id}?fields=name,size,md5Checksum,modifiedTime,mimeType&supportsAllDrives=true")


def save_drive_file(account: str, file_id: str, folder: Path) -> dict:
    import drive_ingest as d
    meta = drive_meta(account, file_id)
    if str(meta.get("mimeType") or "").startswith("application/vnd.google-apps."):
        return {"skipped": file_id, "why": f"a Google Docs-type file ({meta.get('mimeType')}), not a plain file", "name": meta.get("name")}
    dest = _free_path(folder, _safe_name(meta.get("name") or file_id))
    req = urllib.request.Request(f"{DRIVE_FILES}{file_id}?alt=media&supportsAllDrives=true",
                                 headers={"Authorization": "Bearer " + d._creds(account).token})
    with urllib.request.urlopen(req, timeout=600) as resp, dest.open("wb") as f:
        for chunk in iter(lambda: resp.read(1 << 20), b""):
            f.write(chunk)
    row = {"saved": str(dest), "bytes": dest.stat().st_size, "sha256": _sha256(dest), "source": "drive",
           "name": meta.get("name"), "drive_id": file_id, "drive_modified": meta.get("modifiedTime")}
    if meta.get("md5Checksum"):
        row["drive_md5"] = meta["md5Checksum"]
        row["md5_matches_drive"] = (_md5(dest) == meta["md5Checksum"])
    if meta.get("size") and int(meta["size"]) != row["bytes"]:
        row["size_matches_drive"] = False
    return row


# ------------------------------------------------------------------ main
def run(account: str, message_id: str, out: str, only: list, evidence: str = "", list_only: bool = False) -> int:
    atts, ids, hdr = gmail_items(account, message_id)
    print(json.dumps({"message": message_id, "account": account, **hdr, "attachments": [a["filename"] for a in atts],
                      "drive_files": ids}, ensure_ascii=False))
    if list_only:
        for fid in ids:
            try:
                print(json.dumps({"drive_id": fid, **{k: v for k, v in drive_meta(account, fid).items()}}, ensure_ascii=False))
            except Exception as e:  # noqa: BLE001
                print(json.dumps({"drive_id": fid, "error": f"{type(e).__name__}: {str(e)[:160]}"}))
        return 0
    if not atts and not ids:
        print("NOTHING_TO_SAVE")
        return 0
    folder = Path(out).expanduser()
    if is_footage_folder(str(folder)):
        print(f"REFUSED the destination looks like a camera-media folder ({folder}); the assistant never writes there")
        return 2
    folder.mkdir(parents=True, exist_ok=True)
    want = [w.lower() for w in only or []]

    def wanted(name: str) -> bool:
        return not want or any(w in str(name or "").lower() for w in want)

    rows, errors = [], 0
    for a in atts:
        if not wanted(a["filename"]):
            continue
        try:
            r = save_gmail_attachment(account, message_id, a, folder)
            rows.append(r)
            print(json.dumps(r, ensure_ascii=False))
        except Exception as e:  # noqa: BLE001
            errors += 1
            print(json.dumps({"failed": a["filename"], "error": f"{type(e).__name__}: {str(e)[:200]}"}))
    for fid in ids:
        try:
            name = drive_meta(account, fid).get("name") if want else ""
            if want and not wanted(name):
                continue
            r = save_drive_file(account, fid, folder)
            if "saved" in r:
                rows.append(r)
            print(json.dumps(r, ensure_ascii=False))
        except Exception as e:  # noqa: BLE001
            errors += 1
            print(json.dumps({"failed": fid, "error": f"{type(e).__name__}: {str(e)[:200]}"}))
    add_evidence(_evidence_path(evidence), rows, f"mail_attach {account} {message_id} -> {folder} at {time.strftime('%Y-%m-%d %H:%M')}")
    print(f"SAVED={len(rows)}" + (f" FAILED={errors}" if errors else ""))
    return 1 if errors and not rows else 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("account")
    ap.add_argument("message_id")
    ap.add_argument("--out", default="", help="destination folder (created; existing files are never overwritten)")
    ap.add_argument("--only", nargs="*", default=[], help="save only files whose name contains one of these")
    ap.add_argument("--evidence", default="", help="evidence.json to append to (default: the doer ticket's, when run by the doer)")
    ap.add_argument("--list", action="store_true", help="show what the message carries; save nothing")
    a = ap.parse_args()
    if not a.list and not a.out:
        print("REFUSED --out <folder> is required (or --list)")
        return 2
    return run(a.account, a.message_id, a.out, a.only, a.evidence, a.list)


if __name__ == "__main__":
    sys.exit(main())

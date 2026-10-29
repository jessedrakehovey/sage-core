"""resources.py - files the owners hand the agent in chat become library sources.

"A simple wiki and resources upload option in chat" (the owner, 2026-10-11). The phone chat page
has a paperclip; Google Chat attachments arrive through space_reader. Either way a document
lands here:

    Raw/Uploads/YYYY-MM-DD_<name>          the file itself (Raw/ is the vault's source layer;
                                           every Raw/ file the wiki does not cite is a pending
                                           ingest for the nightly dreaming run and `wiki-ingest`)
    Raw/Uploads/YYYY-MM-DD_<name>.txt      a plain-text extract for Office documents, so the
                                           model's Read tool can open what it cannot parse
    assistant/data/uploads.jsonl           who sent what, when, and the note they typed with it

The brain is told in the same message ("[the owner uploaded ... read it, then file what matters as
a `note` with sources]"), so the owner gets an answer about the file at once and the wiki gets
a draft note citing it. Nothing here runs a model; nothing leaves the machine.

Refused: executables and scripts, anything over MAX_MB, media originals (footage never travels
through chat; card-ingest is the path for that).
"""
from __future__ import annotations

import io
import json
import re
import subprocess
import sys
import time
import zipfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import core  # noqa: E402

UPLOADS = core.VAULT / "Raw" / "Uploads"
MANIFEST = core.DATA / "uploads.jsonl"
MAX_MB = 25
REFUSED_EXT = {".exe", ".msi", ".bat", ".cmd", ".ps1", ".vbs", ".js", ".jar", ".scr", ".com", ".dll", ".lnk", ".reg"}
MEDIA_EXT = {".mp4", ".mov", ".avi", ".mxf", ".braw", ".r3d", ".wav", ".aif", ".aiff", ".mts", ".m4v"}
TEXT_EXT = {".txt", ".md", ".csv", ".json", ".yaml", ".yml", ".html", ".htm", ".rtf", ".ics", ".vcf", ".eml"}
DOC_EXT = {".docx", ".pptx", ".xlsx"}
_TAG = re.compile(r"<[^>]+>")


def _safe_name(name: str) -> str:
    name = re.sub(r"[\\/:*?\"<>|#\[\]^]+", " ", str(name or "file")).strip()
    name = re.sub(r"\s+", " ", name).lstrip(". ")[:90]     # no hidden files, no "../" remnants
    return name or "file"


def _extract(data: bytes, suffix: str) -> str:
    """Plain text from an Office document; '' when there is none to get."""
    try:
        z = zipfile.ZipFile(io.BytesIO(data))
    except zipfile.BadZipFile:
        return ""
    parts = []
    try:
        if suffix == ".docx":
            xml = z.read("word/document.xml").decode("utf-8", "replace")
            xml = re.sub(r"</w:p>", "\n", xml)
            parts.append(_TAG.sub("", xml))
        elif suffix == ".pptx":
            for n in sorted(x for x in z.namelist() if re.match(r"ppt/slides/slide\d+\.xml$", x)):
                xml = re.sub(r"</a:p>", "\n", z.read(n).decode("utf-8", "replace"))
                parts.append(f"--- {n.split('/')[-1]} ---\n" + _TAG.sub("", xml))
        elif suffix == ".xlsx":
            shared = []
            if "xl/sharedStrings.xml" in z.namelist():
                shared = [_TAG.sub("", s) for s in re.findall(r"<si>(.*?)</si>", z.read("xl/sharedStrings.xml").decode("utf-8", "replace"), re.S)]
            for n in sorted(x for x in z.namelist() if re.match(r"xl/worksheets/sheet\d+\.xml$", x)):
                xml = z.read(n).decode("utf-8", "replace")
                rows = []
                for row in re.findall(r"<row[^>]*>(.*?)</row>", xml, re.S):
                    cells = []
                    for c in re.finditer(r"<c([^>]*)>(.*?)</c>", row, re.S):
                        attrs, inner = c.group(1), c.group(2)
                        v = re.search(r"<v>(.*?)</v>", inner, re.S)
                        val = v.group(1) if v else _TAG.sub("", inner)
                        if 't="s"' in attrs and val.isdigit() and int(val) < len(shared):
                            val = shared[int(val)]
                        cells.append(val.strip())
                    rows.append(",".join(cells))
                parts.append(f"--- {n.split('/')[-1]} ---\n" + "\n".join(rows))
    except KeyError:
        return ""
    text = "\n".join(parts)
    text = re.sub(r"&amp;", "&", re.sub(r"&lt;", "<", re.sub(r"&gt;", ">", text)))
    return re.sub(r"\n{3,}", "\n\n", text).strip()


def _git(*args) -> bool:
    try:
        r = subprocess.run(["git", *args], cwd=str(core.VAULT), capture_output=True, text=True, timeout=60, creationflags=core.NO_WINDOW)
        return r.returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


def save(name: str, data: bytes, who: str, note: str = "", via: str = "chat", commit: bool = True) -> dict:
    """-> {"ok", "rel", "path", "extract", "size", "reason"}"""
    name = _safe_name(name)
    suffix = Path(name).suffix.lower()
    if suffix in REFUSED_EXT:
        return {"ok": False, "reason": "programs and scripts are not kept in the library"}
    if suffix in MEDIA_EXT:
        return {"ok": False, "reason": "footage and audio originals never travel through chat; plug the card in and ask for an ingest"}
    if not data:
        return {"ok": False, "reason": "the file was empty"}
    if len(data) > MAX_MB * 1024 * 1024:
        return {"ok": False, "reason": f"over {MAX_MB} MB; share a link or a smaller export"}
    UPLOADS.mkdir(parents=True, exist_ok=True)
    day = time.strftime("%Y-%m-%d")
    path = UPLOADS / f"{day}_{name}"
    n = 2
    while path.exists() and path.read_bytes() != data:
        path = UPLOADS / f"{day}_{Path(name).stem} ({n}){suffix}"
        n += 1
    path.write_bytes(data)
    root = UPLOADS.parents[1]                       # the vault (tests point UPLOADS elsewhere)
    rel = path.relative_to(root).as_posix()
    extract = ""
    if suffix in DOC_EXT:
        text = _extract(data, suffix)
        if text:
            xp = path.with_name(path.name + ".txt")
            xp.write_text(text, encoding="utf-8")
            extract = xp.relative_to(root).as_posix()
    core.append(MANIFEST, {"ts": core.stamp(), "who": who, "name": name, "rel": rel, "extract": extract, "size": len(data),
                           "note": str(note or "")[:400], "via": via})
    if commit and core.STATE == core.AGENT / "state":
        files = [rel] + ([extract] if extract else [])
        if _git("add", "--", *files):
            _git("commit", "-q", "-m", f"library: {who} uploaded {name} via {via}", "--", *files)
    return {"ok": True, "rel": rel, "path": str(path), "extract": extract, "size": len(data)}


def from_attachment(path: Path, who: str, note: str = "") -> dict:
    """A document that came through Google Chat (already downloaded by space_reader)."""
    try:
        data = path.read_bytes()
    except OSError as e:
        return {"ok": False, "reason": str(e)}
    name = re.sub(r"^\d{8}-\d{6}_", "", path.name)
    return save(name, data, who, note=note, via="google-chat")


def describe(rec: dict, who: str) -> str:
    """The line the brain gets with the message."""
    if not rec.get("ok"):
        return f"[{who} tried to upload a file but it was not kept: {rec.get('reason', 'unknown reason')}. Tell them in one line.]"
    where = rec["rel"] + (f"; plain-text extract at {rec['extract']}" if rec.get("extract") else "")
    return (f"[{who} uploaded a file into the library: {where} ({rec.get('size', 0) // 1024} KB). Open it with Read "
            f"(the extract for Office files), answer what they asked about it, and file what is worth keeping as a `note` "
            f"with \"sources\": [\"{rec['rel']}\"]. Say in one line where you filed it.]")


def recent(n: int = 10) -> list:
    return core.rows(MANIFEST)[-n:]


def pending() -> list:
    """Uploads no wiki note cites yet (the nightly ingest's queue for this folder)."""
    try:
        sys.path.insert(0, str(core.VAULT / "scripts"))
        import wiki_tool
        cited = wiki_tool.collect_ingested_sources()
    except Exception:  # noqa: BLE001
        cited = set()
    out = []
    for p in sorted(UPLOADS.glob("*")) if UPLOADS.exists() else []:
        if p.is_file() and not p.name.endswith(".txt") and p.stem.lower() not in cited:
            out.append(p.relative_to(UPLOADS.parents[1]).as_posix())
    return out


def render() -> str:
    rows = recent(8)
    if not rows:
        return ""
    pend = pending()
    L = [f"LIBRARY (files the owners uploaded; under Raw/Uploads; {len(pend)} not yet filed in the wiki):"]
    for r in rows:
        L.append(f"- {r.get('ts', '')[:10]} {r.get('who', '')}: {r.get('rel', '')}" + (f" ({r['note'][:60]})" if r.get("note") else ""))
    return "\n".join(L)


def status_text() -> str:
    rows = recent(1000)
    pend = pending()
    if not rows and not pend:
        return "Nothing uploaded yet. Tap the paperclip in chat to add a document, or send one in Google Chat."
    return f"{len(rows)} files uploaded; {len(pend)} not yet filed in the wiki" + (": " + ", ".join(Path(p).name for p in pend[:6]) if pend else ".")


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    print(status_text())
    for r in recent(20):
        print(json.dumps(r, ensure_ascii=False))

"""release.py - a versioned release of the engine (sage-core, 2026-10-28).

The owner-facing install path (the Windows installer, `assistant/installer/`) and the auto-updater
(`assistant/updater.py`) do not read git. They read a RELEASE: one zip of the engine's files plus a
`release.json` beside it with the version, the date, the archive's sha256 and the notes. This script
builds that pair into a git-ignored `dist/` folder.

  python assistant/release.py build [--out DIR] [--version V] [--notes TEXT | --notes-file F] [--url BASE]
  python assistant/release.py verify <zip> [<release.json>]
  python assistant/release.py version

Run from an engine checkout (sage-core: the files are already templates) the zip is the checkout's
manifest files as they are. Run from an organization's folder (the engine's source, or a pack) the
engine is EXPORTED first (engine.export: the organization's root becomes {{ORG_ROOT}}, every name is
scrubbed) and the export is what ships, so nothing of the organization travels either way.

The zip has no top-level folder: `engine_manifest.json`, `README.md` and `assistant/...` sit at its root,
so the updater can extract it straight into a staging folder and hand that folder to `engine.update`.
Versions are the engine's own form, `YYYY.MM.DD+<git short sha>` (engine._source_version); the updater
compares the date part and treats a different sha on the same date as "different, not newer".
"""
from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import subprocess
import sys
import tempfile
import time
import zipfile
from pathlib import Path

HERE = Path(__file__).resolve().parent          # assistant/
ROOT = HERE.parent
sys.path.insert(0, str(HERE))
import engine  # noqa: E402

NAME = "sage-core"
EXTRA_FILES = (engine.MANIFEST_NAME, "README.md", ".gitignore")


def sha256_of(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def is_engine_checkout(root: Path) -> bool:
    """An exported engine (sage-core) carries the root placeholder in its wrappers; a pack or the source
    organization's folder carries a real path there."""
    man = root / engine.MANIFEST_NAME
    if not man.exists():
        return False
    for probe in ("assistant/scripts/spine.ps1", "assistant/serve.json", "assistant/README.md"):
        p = root / probe
        if p.exists():
            try:
                if engine.ROOT_PLACEHOLDER in engine.read_text(p):
                    return True
            except OSError:
                pass
    try:
        org = json.loads((root / "assistant" / "org.json").read_text(encoding="utf-8-sig"))
        return str(org.get("name") or "") in ("", "example")
    except (OSError, ValueError):
        return False


def _manifest(root: Path) -> dict:
    try:
        return json.loads((root / engine.MANIFEST_NAME).read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        return {}


def version_of(root: Path) -> str:
    man = _manifest(root)
    v = str(((man.get("engine") or {}).get("version")) or "")
    if v and is_engine_checkout(root):
        return v
    return engine._source_version(root)


def _notes(root: Path, n: int = 15) -> list:
    """The last commit subjects, scrubbed like any prose the engine exports (no organization names)."""
    try:
        r = subprocess.run(["git", "log", f"-{n}", "--format=%s"], cwd=str(root), capture_output=True, text=True, encoding="utf-8",
                           errors="replace", timeout=20)
        lines = [ln.strip() for ln in (r.stdout or "").splitlines() if ln.strip()] if r.returncode == 0 else []
    except (OSError, subprocess.TimeoutExpired):
        lines = []
    return [engine.scrub(ln, "x.md")[:200] for ln in lines]


def build(root: Path, out: Path, version: str = "", notes: list | None = None, url_base: str = "") -> dict:
    """Write dist/<name>-<version>.zip + dist/release.json. Returns the release record."""
    root, out = Path(root).resolve(), Path(out).resolve()
    out.mkdir(parents=True, exist_ok=True)
    tmp = None
    if is_engine_checkout(root):
        src = root
        man = _manifest(src)
    else:
        tmp = Path(tempfile.mkdtemp(prefix="sage_release_"))
        src = tmp / NAME
        man = engine.export(src, src_root=root, with_tests=True)
    try:
        ver = version or str((man.get("engine") or {}).get("version") or version_of(src))
        safe = ver.replace("+", "-").replace("/", "-")
        zip_path = out / f"{NAME}-{safe}.zip"
        rels = sorted(set(list(man.get("files") or {}) + list(EXTRA_FILES)))
        rels = [r for r in rels if engine.ships(r, with_tests=True) or r in EXTRA_FILES]
        if tmp is None:     # an engine checkout: only what git tracks ships, never a stray file left in the folder (2026-10-29)
            try:
                r = subprocess.run(["git", "ls-files"], cwd=str(src), capture_output=True, text=True, encoding="utf-8", timeout=30)
                tracked = {ln.strip() for ln in (r.stdout or "").splitlines() if ln.strip()} if r.returncode == 0 else None
            except (OSError, subprocess.TimeoutExpired):
                tracked = None
            if tracked is not None:
                rels = [r for r in rels if r in tracked]
        n = 0
        with zipfile.ZipFile(zip_path, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=6) as z:
            for rel in rels:
                p = src / rel
                if not p.is_file():
                    continue
                zi = zipfile.ZipInfo(rel.replace("\\", "/"), date_time=(2026, 1, 1, 0, 0, 0))   # reproducible: the content is the identity
                zi.compress_type = zipfile.ZIP_DEFLATED
                zi.external_attr = 0o644 << 16
                z.writestr(zi, p.read_bytes())
                n += 1
        rec = {"name": NAME, "version": ver, "date": time.strftime("%Y-%m-%d"), "built": time.strftime("%Y-%m-%dT%H:%M:%S"),
               "file": zip_path.name, "sha256": sha256_of(zip_path), "size": zip_path.stat().st_size, "files": n,
               # notes only from an engine checkout: its commit subjects are already organization-free; a source organization's are not
               "notes": list(notes if notes is not None else (_notes(root) if src is root else [])), "source": "engine checkout" if src is root else "export"}
        if url_base:
            rec["url"] = url_base.rstrip("/") + "/" + zip_path.name
        (out / "release.json").write_text(json.dumps(rec, indent=1, ensure_ascii=False) + "\n", encoding="utf-8")
        (out / f"{zip_path.name}.sha256").write_text(f"{rec['sha256']}  {zip_path.name}\n", encoding="utf-8")
        return rec
    finally:
        if tmp is not None:
            shutil.rmtree(tmp, ignore_errors=True)


def verify(zip_path: Path, rel_json: Path | None = None) -> tuple:
    """(ok, message): the archive's sha256 matches release.json and the archive carries a manifest + sage.py."""
    zip_path = Path(zip_path)
    rel_json = Path(rel_json) if rel_json else zip_path.with_name("release.json")
    try:
        rec = json.loads(rel_json.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError) as e:
        return False, f"no readable release.json beside the archive: {e}"
    got = sha256_of(zip_path)
    if got != rec.get("sha256"):
        return False, f"sha256 mismatch: archive {got[:12]}..., release.json {str(rec.get('sha256'))[:12]}..."
    try:
        with zipfile.ZipFile(zip_path) as z:
            names = set(z.namelist())
            bad = z.testzip()
    except zipfile.BadZipFile as e:
        return False, f"not a zip: {e}"
    if bad:
        return False, f"corrupt member {bad}"
    for need in (engine.MANIFEST_NAME, "assistant/sage.py", "assistant/engine.py"):
        if need not in names:
            return False, f"archive lacks {need}"
    if any(n.startswith("/") or ".." in n.split("/") for n in names):
        return False, "archive carries an unsafe path"
    return True, f"ok: {rec.get('name')} {rec.get('version')}, {len(names)} files, sha256 {got[:12]}..."


def main() -> int:
    sys.stdout.reconfigure(encoding="utf-8")
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("build"); p.add_argument("--out", default=str(ROOT / "dist")); p.add_argument("--version", default="")
    p.add_argument("--notes", default=None); p.add_argument("--notes-file", default=""); p.add_argument("--url", default="", help="base URL the archive will be served from (written into release.json)")
    p = sub.add_parser("verify"); p.add_argument("zip"); p.add_argument("release_json", nargs="?", default="")
    sub.add_parser("version")
    a = ap.parse_args()
    if a.cmd == "version":
        print(version_of(ROOT))
        return 0
    if a.cmd == "verify":
        ok, msg = verify(Path(a.zip), Path(a.release_json) if a.release_json else None)
        print(("RELEASE verify " if ok else "RELEASE verify FAILED ") + msg)
        return 0 if ok else 1
    notes = None
    if a.notes is not None:
        notes = [ln.strip() for ln in a.notes.splitlines() if ln.strip()]
    elif a.notes_file:
        notes = [ln.strip() for ln in Path(a.notes_file).read_text(encoding="utf-8-sig").splitlines() if ln.strip()]
    rec = build(ROOT, Path(a.out), version=a.version, notes=notes, url_base=a.url)
    print(f"RELEASE {rec['version']} {Path(a.out) / rec['file']} sha256={rec['sha256']} files={rec['files']} size={rec['size']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

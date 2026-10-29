"""updater.py - the engine keeps itself current, however it was installed (sage-core, 2026-10-28).

An organization's folder (its pack) carries the engine's files plus `engine_manifest.json`. Until this file
a newer engine reached it only by hand: `python assistant/sage.py update --engine <sage-core folder>`,
which needs a git clone of the engine. The owner-facing install (the Windows installer) has no git and no
terminal, so the pack now updates itself from a RELEASE (assistant/release.py: one zip + release.json):

  1. read release.json at the release URL (org.json -> engine_release_url, SAGE_RELEASE_URL, else the GitHub Releases
     of the clone the engine came from; https, file:// or a local path), compare its version to the installed one (date part; a different sha on the same date
     is "different", applied only with --force);
  2. download the archive to a temp folder, check its sha256 against release.json;
  3. extract it to <pack>/.sage-engines/staging-<version>/ and GATE it there: every .py compiles,
     `sage.py doctor --no-probe` runs against this pack's org.json from the staged copy, and the staged
     engine's own test gate (assistant/tests/test_sage_core.py) passes;
  4. only then: snapshot the pack's current engine files to .sage-engines/previous/ (for `rollback`), apply
     with engine.update (pack files are never touched; files the organization changed by hand are reported
     and left alone unless --force), keep the staged copy as .sage-engines/<version>/, and ask the supervisor
     to restart itself (assistant/data/.sage_restart; sage.py serve re-execs on it).

A failure at any step leaves the pack exactly as it was and posts ONE Mission Control note (never Chat).
`sage.py update` with no --engine runs this same path, so a git-cloned install and an installer install update
identically; `sage.py rollback` puts the previous engine back in one step.

  python assistant/updater.py check            what is installed, what is available
  python assistant/updater.py run [--force] [--no-gate] [--url URL]
  python assistant/updater.py rollback
  python assistant/updater.py status
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.parse
import urllib.request
import zipfile
from pathlib import Path

HERE = Path(__file__).resolve().parent          # assistant/
ROOT = HERE.parent                               # the organization's folder (the pack)
sys.path.insert(0, str(HERE))
import engine  # noqa: E402
import orgcfg  # noqa: E402

DATA = Path(os.environ["ASSISTANT_DATA"]) if os.environ.get("ASSISTANT_DATA") else HERE / "data"
STATE = DATA / "updater_state.json"
RESTART = DATA / ".sage_restart"
ENGINES = ROOT / ".sage-engines"
PREVIOUS = ENGINES / "previous"
KEEP_ENGINES = 2
SENTINEL = "UPDATER"
VERSION_RE = re.compile(r"^(\d{4})\.(\d{2})\.(\d{2})(?:\+([0-9A-Za-z._-]+))?$")
GATES = ("compile", "doctor", "tests")
NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


def _log(msg: str) -> None:
    print(time.strftime("%H:%M:%S") + "  " + msg, flush=True)


def _load(path: Path, default):
    try:
        return json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        return default


def _save(path: Path, obj) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(obj, indent=1, ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, path)


def note(text: str) -> None:
    """A Mission Control note: a system line never goes to Chat (the voice gate's rule)."""
    try:
        DATA.mkdir(parents=True, exist_ok=True)
        with open(DATA / "cards.jsonl", "a", encoding="utf-8") as f:
            f.write(json.dumps({"ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "text": text[:3500], "kind": "note", "source": "updater"},
                               ensure_ascii=False) + "\n")
    except OSError:
        pass


# ---------------------------------------------------------------- versions
def parse_version(v: str) -> tuple:
    """'2026.10.28+58bca0a' -> ((2026, 10, 28), '58bca0a'); anything else -> ((0, 0, 0), v)."""
    m = VERSION_RE.match(str(v or "").strip())
    if not m:
        return (0, 0, 0), str(v or "")
    return (int(m.group(1)), int(m.group(2)), int(m.group(3))), (m.group(4) or "")


def compare(installed: str, available: str) -> str:
    """newer | same | older | different (same date, another build) | unknown (either side unparsable)."""
    (di, si), (da, sa) = parse_version(installed), parse_version(available)
    if di == (0, 0, 0) or da == (0, 0, 0):
        return "same" if installed == available else "unknown"
    if da > di:
        return "newer"
    if da < di:
        return "older"
    return "same" if si == sa else "different"


def installed_version() -> str:
    man = _load(ROOT / engine.MANIFEST_NAME, {})
    return str(((man.get("engine") or {}).get("version")) or "")


def is_pack() -> bool:
    """A pack carries a manifest written by an install (engine.installed); an engine checkout's says exported; the engine's
    source folder has none. Neither of the last two is updated by this."""
    man = _load(ROOT / engine.MANIFEST_NAME, {})
    eng = man.get("engine") or {}
    if not man.get("files") or not (eng.get("installed") or eng.get("updated")):
        return False
    return bool(orgcfg.get("name"))


# ---------------------------------------------------------------- the release feed
GITHUB_RE = re.compile(r"github\.com[:/]([^/\s]+)/([^/\s]+?)(?:\.git)?/?$", re.I)


def feed_from_origin(checkout: Path) -> str:
    """The GitHub Releases feed of a git checkout's origin (`.../releases/latest/download`), or ''. The engine carries no
    literal home: whoever hosts the engine is read from the clone it came from."""
    try:
        if not (Path(checkout) / ".git").exists():
            return ""
        r = subprocess.run(["git", "-C", str(checkout), "remote", "get-url", "origin"], capture_output=True, text=True, timeout=10, creationflags=NO_WINDOW)
        m = GITHUB_RE.search((r.stdout or "").strip()) if r.returncode == 0 else None
        return f"https://github.com/{m.group(1)}/{m.group(2)}/releases/latest/download" if m else ""
    except (OSError, subprocess.TimeoutExpired):
        return ""


def derived_url() -> str:
    """No URL configured: the engine checkout this pack was installed or last updated from, when it is a GitHub clone."""
    man = _load(ROOT / engine.MANIFEST_NAME, {})
    er = str(((man.get("engine") or {}).get("engine_root")) or "")
    return feed_from_origin(Path(er)) if er else ""


def release_url(explicit: str = "") -> str:
    u = explicit or os.environ.get("SAGE_RELEASE_URL", "") or str(orgcfg.get("engine_release_url") or "")
    return (u or derived_url()).strip()


def _is_remote(u: str) -> bool:
    return u.lower().startswith(("http://", "https://"))


def _local(u: str) -> Path:
    if u.lower().startswith("file://"):
        return Path(urllib.request.url2pathname(urllib.parse.urlparse(u).path))
    return Path(u)


def fetch(u: str, dst: Path | None = None, timeout: int = 60) -> bytes | Path:
    """Bytes of a URL / file (dst None), or the file written to dst (large downloads, streamed)."""
    if _is_remote(u):
        req = urllib.request.Request(u, headers={"User-Agent": "sage-updater"})
        with urllib.request.urlopen(req, timeout=timeout) as r:
            if dst is None:
                return r.read()
            dst.parent.mkdir(parents=True, exist_ok=True)
            with open(dst, "wb") as f:
                shutil.copyfileobj(r, f, 1 << 20)
            return dst
    p = _local(u)
    if dst is None:
        return p.read_bytes()
    dst.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(p, dst)
    return dst


def feed(u: str) -> tuple:
    """(release record, the URL/path of release.json) for a URL that names release.json or its folder."""
    if not u:
        raise ValueError("no release URL: set org.json -> engine_release_url (or SAGE_RELEASE_URL)")
    if _is_remote(u):
        rel = u if u.lower().endswith(".json") else u.rstrip("/") + "/release.json"
    else:
        p = _local(u)
        rel = str(p / "release.json") if p.is_dir() else str(p)
    rec = json.loads(fetch(rel).decode("utf-8-sig"))
    if not rec.get("version") or not rec.get("sha256") or not rec.get("file"):
        raise ValueError("release.json lacks version / sha256 / file")
    return rec, rel


def archive_url(rec: dict, rel_url: str) -> str:
    u = str(rec.get("url") or "")
    if _is_remote(u):
        return u
    if _is_remote(rel_url):
        return urllib.parse.urljoin(rel_url, rec["file"])
    return str(_local(rel_url).parent / rec["file"])


def check(url: str = "") -> dict:
    inst = installed_version()
    out = {"installed": inst, "url": release_url(url), "checked": time.strftime("%Y-%m-%dT%H:%M:%S")}
    if not is_pack():
        out.update(available="", relation="not a pack", detail="this folder is the engine's source, not an installed organization")
        return out
    try:
        rec, _ = feed(out["url"])
    except Exception as e:  # noqa: BLE001
        out.update(available="", relation="unreachable", detail=f"{type(e).__name__}: {str(e)[:160]}")
        return out
    out.update(available=rec["version"], relation=compare(inst, rec["version"]), notes=rec.get("notes", [])[:8], size=rec.get("size"))
    return out


# ---------------------------------------------------------------- the gate
def compile_tree(root: Path) -> list:
    """Every .py under assistant/ and scripts/ parsed in this process (no __pycache__ left in the staged tree). Returns the
    files that do not compile."""
    bad = []
    for top in ("assistant", "scripts"):
        base = root / top
        if not base.exists():
            continue
        for p in base.rglob("*.py"):
            if "__pycache__" in p.parts:
                continue
            try:
                compile(p.read_bytes(), str(p), "exec")
            except (SyntaxError, ValueError) as e:
                bad.append(f"{p.relative_to(root).as_posix()}:{getattr(e, 'lineno', '?')}")
    return bad


def gate(staged: Path, which: tuple = GATES, timeout: int = 1200) -> tuple:
    """(ok, detail) for a staged engine: compile, doctor (against THIS pack's org.json), the engine's own tests."""
    if "compile" in which:
        bad = compile_tree(staged)
        if bad:
            return False, f"{len(bad)} .py file(s) in the staged engine do not compile: {', '.join(bad[:3])}"
    env = {k: v for k, v in os.environ.items() if not k.startswith(("ASSISTANT_", "AGENT_", "DOER_"))}
    scratch = Path(tempfile.mkdtemp(prefix="sage_gate_"))
    env.update(WV_LOG_DIR=str(scratch / "logs"), CHAT_PUSH="off", PYTHONIOENCODING="utf-8")
    try:
        if "doctor" in which:
            e2 = dict(env, ASSISTANT_ORG=str(HERE / "org.json"), ASSISTANT_DATA=str(scratch / "data"))
            r = subprocess.run([sys.executable, str(staged / "assistant" / "sage.py"), "doctor", "--no-probe", "--json"], capture_output=True, text=True,
                               encoding="utf-8", errors="replace", timeout=300, cwd=str(staged), env=e2, creationflags=NO_WINDOW)
            out = r.stdout or ""
            try:
                rows = {x["check"]: x for x in json.loads(out[out.index("["):])}
            except (ValueError, KeyError):
                return False, "doctor did not answer from the staged engine: " + ((r.stderr or out).strip()[-200:])
            for must in ("python", "org pack", "engine manifest"):
                if rows.get(must, {}).get("status") == "fail":
                    return False, f"doctor from the staged engine fails on {must}: {rows[must].get('detail', '')[:120]}"
        tests = staged / "assistant" / "tests" / "test_sage_core.py"
        if "tests" in which and tests.exists():
            r = subprocess.run([sys.executable, str(tests)], capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=timeout,
                               cwd=str(staged), env=env, creationflags=NO_WINDOW)
            if r.returncode != 0:
                tail = ((r.stdout or "") + (r.stderr or "")).strip().splitlines()[-3:]
                return False, "the staged engine's test gate failed: " + " | ".join(tail)[-300:]
        return True, "compiled" + (", doctor ok" if "doctor" in which else "") + (", tests ok" if "tests" in which and tests.exists() else "")
    except subprocess.TimeoutExpired:
        return False, "the gate timed out"
    finally:
        shutil.rmtree(scratch, ignore_errors=True)


# ---------------------------------------------------------------- snapshot, apply, rollback
def snapshot() -> int:
    """The pack's current engine files + manifest into .sage-engines/previous/ (replaced each time)."""
    man = _load(ROOT / engine.MANIFEST_NAME, {})
    if PREVIOUS.exists():
        shutil.rmtree(PREVIOUS)
    PREVIOUS.mkdir(parents=True)
    n = 0
    for rel in (man.get("files") or {}):
        if engine.is_pack(rel):
            continue
        src = ROOT / rel
        if src.is_file():
            dst = PREVIOUS / rel
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, dst)
            n += 1
    shutil.copy2(ROOT / engine.MANIFEST_NAME, PREVIOUS / engine.MANIFEST_NAME)
    (PREVIOUS / "rollback.json").write_text(json.dumps({"version": installed_version(), "taken": time.strftime("%Y-%m-%dT%H:%M:%S"), "files": n,
                                                        "installed": str(((man.get("engine") or {}).get("installed")) or "")}, indent=1), encoding="utf-8")
    return n


def _extract(zip_path: Path, dst: Path) -> None:
    if dst.exists():
        shutil.rmtree(dst)
    dst.mkdir(parents=True)
    with zipfile.ZipFile(zip_path) as z:
        for info in z.infolist():
            name = info.filename.replace("\\", "/")
            if name.startswith("/") or ".." in name.split("/"):
                raise ValueError(f"unsafe path in archive: {name}")
            z.extract(info, dst)
    for need in (engine.MANIFEST_NAME, "assistant/sage.py", "assistant/engine.py"):
        if not (dst / need).is_file():
            raise ValueError(f"archive lacks {need}")


def _prune_engines() -> None:
    kept = sorted([p for p in ENGINES.iterdir() if p.is_dir() and p.name not in ("previous",) and not p.name.startswith("staging-")],
                  key=lambda p: p.stat().st_mtime, reverse=True)
    for p in kept[KEEP_ENGINES:]:
        shutil.rmtree(p, ignore_errors=True)


def _request_restart(why: str) -> None:
    try:
        RESTART.parent.mkdir(parents=True, exist_ok=True)
        RESTART.write_text(why, encoding="utf-8")
    except OSError:
        pass


def run(force: bool = False, gates: tuple = GATES, url: str = "", dry: bool = False) -> dict:
    """One update pass. Returns the record also written to updater_state.json."""
    st = _load(STATE, {})
    rec = {"ts": time.strftime("%Y-%m-%dT%H:%M:%S"), "installed": installed_version(), "result": "", "detail": ""}

    def finish(result: str, detail: str, failed: bool = False, **more) -> dict:
        rec.update(result=result, detail=detail, **more)
        st.update(last=rec, checked=rec["ts"])
        st.setdefault("history", []).append({k: rec[k] for k in ("ts", "installed", "result", "detail") if k in rec} | {k: rec[k] for k in ("available",) if k in rec})
        st["history"] = st["history"][-30:]
        _save(STATE, st)
        if failed:
            note(f"Engine update did not land: {detail}. The installed engine ({rec['installed'] or 'unknown'}) is untouched. "
                 f"`python assistant/sage.py update` retries; `sage update --engine <folder>` works offline.")
        return rec

    if not is_pack():
        return finish("skipped", "not a pack (the engine's source folder updates by git)")
    try:
        rel_rec, rel_url = feed(release_url(url))
    except Exception as e:  # noqa: BLE001
        if not release_url(url):
            return finish("skipped", "no release URL configured (org.json -> engine_release_url)")
        if "404" in str(e) and not (url or os.environ.get("SAGE_RELEASE_URL") or orgcfg.get("engine_release_url")):
            # a derived feed with no public release yet (or a private repository): quiet, not an alarm
            return finish("skipped", f"no public release at {release_url(url)} yet")
        return finish("unreachable", f"release feed: {type(e).__name__}: {str(e)[:160]}", failed=True)
    avail = str(rel_rec["version"])
    relation = compare(rec["installed"], avail)
    rec["available"] = avail
    if relation == "same" or (relation in ("older", "different", "unknown") and not force):
        return finish("up to date" if relation == "same" else f"not applied ({relation})",
                      f"installed {rec['installed'] or 'unknown'}, available {avail}" + ("" if relation == "same" else "; --force applies it anyway"))
    if dry:
        return finish("would update", f"{rec['installed'] or 'unknown'} -> {avail} (dry run)")
    tmp = Path(tempfile.mkdtemp(prefix="sage_update_"))
    staged = ENGINES / f"staging-{avail.replace('+', '-')}"
    try:
        zpath = tmp / str(rel_rec["file"])
        _log(f"downloading {archive_url(rel_rec, rel_url)}")
        fetch(archive_url(rel_rec, rel_url), zpath, timeout=600)
        import release as _release
        got = _release.sha256_of(zpath)
        if got != str(rel_rec["sha256"]).lower():
            return finish("hash mismatch", f"archive sha256 {got[:12]}... != release.json {str(rel_rec['sha256'])[:12]}...", failed=True)
        _log("extracting to the staging folder")
        _extract(zpath, staged)
        staged_ver = str(((_load(staged / engine.MANIFEST_NAME, {}).get("engine") or {}).get("version")) or "")
        if staged_ver and staged_ver != avail:
            return finish("version mismatch", f"release.json says {avail}, the archive's manifest says {staged_ver}", failed=True)
        _log(f"gate: {', '.join(gates)}")
        ok, detail = gate(staged, gates)
        if not ok:
            return finish("gate failed", detail, failed=True)
        installed_at = str(((_load(ROOT / engine.MANIFEST_NAME, {}).get("engine") or {}).get("installed")) or time.strftime("%Y-%m-%dT%H:%M:%S"))
        n = snapshot()
        _log(f"snapshot of {n} engine files kept for rollback; applying")
        rep = engine.update(ROOT, staged, force=force)
        final = ENGINES / avail.replace("+", "-")
        if final.exists():
            shutil.rmtree(final)
        os.replace(staged, final)
        man_p = ROOT / engine.MANIFEST_NAME
        man = _load(man_p, {})
        if man.get("engine"):
            man["engine"]["engine_root"] = str(final)
            man["engine"].setdefault("installed", installed_at)       # the mark of a pack (engine.update copies the release's header)
            _save(man_p, man)
        _prune_engines()
        _request_restart(f"engine {rec['installed']} -> {avail}")
        summary = engine.update_summary(rep)
        note(f"Engine updated {rec['installed'] or 'unknown'} -> {avail}: {summary.splitlines()[0]}." +
             (f" Left alone (changed by hand here): {', '.join(rep['conflicts'][:6])}." if rep["conflicts"] else "") +
             " The supervisor restarts itself; `python assistant/sage.py rollback` puts the previous engine back.")
        return finish("updated", summary.splitlines()[0], replaced=len(rep["replaced"]), added=len(rep["added"]), conflicts=rep["conflicts"],
                      removed=len(rep["removed"]), engine_root=str(final))
    except Exception as e:  # noqa: BLE001
        if PREVIOUS.exists() and (PREVIOUS / "rollback.json").exists() and _load(PREVIOUS / "rollback.json", {}).get("version") == rec["installed"] \
                and installed_version() != rec["installed"]:
            try:                                            # the apply itself broke midway: put the snapshot back
                restore_previous()
                return finish("failed, rolled back", f"{type(e).__name__}: {str(e)[:160]}", failed=True)
            except Exception as e2:  # noqa: BLE001
                return finish("failed, rollback failed", f"{type(e).__name__}: {str(e)[:120]}; rollback: {type(e2).__name__}: {str(e2)[:100]}", failed=True)
        return finish("failed", f"{type(e).__name__}: {str(e)[:200]}", failed=True)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
        if staged.exists():
            shutil.rmtree(staged, ignore_errors=True)


def restore_previous() -> dict:
    """The snapshot back, file by file: every file it holds is copied over the pack's (a hand edit made since the update
    goes with it: the snapshot IS the state before), a file the update ADDED (in the pack's manifest, not in the snapshot's,
    still as the update left it) is removed, the snapshot's manifest is the pack's again. Pack paths are never touched."""
    prev_man = _load(PREVIOUS / engine.MANIFEST_NAME, {})
    cur_man = _load(ROOT / engine.MANIFEST_NAME, {})
    restored, removed = 0, []
    for p in PREVIOUS.rglob("*"):
        if not p.is_file():
            continue
        rel = p.relative_to(PREVIOUS).as_posix()
        if rel in ("rollback.json", engine.MANIFEST_NAME) or engine.is_pack(rel):
            continue
        dst = ROOT / rel
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(p, dst)
        restored += 1
    for rel, was in (cur_man.get("files") or {}).items():
        if rel in (prev_man.get("files") or {}) or engine.is_pack(rel):
            continue
        f = ROOT / rel
        if f.exists() and engine._org_sha(ROOT, rel) == was:
            f.unlink()
            removed.append(rel)
    man = dict(prev_man)
    if man.get("engine"):
        man["engine"] = dict(man["engine"], engine_root=str(PREVIOUS), restored=time.strftime("%Y-%m-%dT%H:%M:%S"))
    _save(ROOT / engine.MANIFEST_NAME, man)
    return {"restored": restored, "removed": removed, "version": str((prev_man.get("engine") or {}).get("version") or "")}


def rollback() -> dict:
    """The previous engine back in one step, then the supervisor restarted."""
    info = _load(PREVIOUS / "rollback.json", {})
    if not (PREVIOUS / engine.MANIFEST_NAME).exists():
        return {"ok": False, "detail": "no previous engine kept (nothing was updated here yet)"}
    before = installed_version()
    r = restore_previous()
    summary = f"engine {r['version'] or info.get('version', 'previous')}: {r['restored']} file(s) put back, {len(r['removed'])} added by the update removed"
    _request_restart(f"rollback {before} -> {r['version']}")
    st = _load(STATE, {})
    st["last_rollback"] = {"ts": time.strftime("%Y-%m-%dT%H:%M:%S"), "from": before, "to": r["version"]}
    _save(STATE, st)
    note(f"Engine rolled back {before} -> {r['version'] or 'previous'}: {summary}. The supervisor restarts itself.")
    return {"ok": True, "from": before, "to": r["version"], "summary": summary}


def main() -> int:
    sys.stdout.reconfigure(encoding="utf-8")
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("check"); p.add_argument("--url", default=""); p.add_argument("--json", action="store_true")
    p = sub.add_parser("run"); p.add_argument("--force", action="store_true"); p.add_argument("--no-gate", action="store_true", help="compile check only")
    p.add_argument("--url", default=""); p.add_argument("--dry-run", action="store_true")
    p.add_argument("--gates", default="", help="comma list from compile,doctor,tests (default all)")
    sub.add_parser("rollback")
    sub.add_parser("status")
    a = ap.parse_args()
    if a.cmd == "check":
        c = check(a.url)
        if a.json:
            print(json.dumps(c, indent=1))
        else:
            print(f"{SENTINEL} check: installed {c['installed'] or 'unknown'}, available {c.get('available') or '-'} ({c.get('relation')})"
                  + (f": {c['detail']}" if c.get("detail") else "") + (f" from {c['url']}" if c.get("url") else ""))
        return 0
    if a.cmd == "run":
        gates = ("compile",) if a.no_gate else (tuple(g.strip() for g in a.gates.split(",") if g.strip()) if a.gates else GATES)
        r = run(force=a.force, gates=gates, url=a.url, dry=a.dry_run)
        print(f"{SENTINEL} {r['result']}: {r['detail']}")
        return 0 if r["result"] in ("updated", "up to date", "skipped", "would update") or r["result"].startswith("not applied") else 1
    if a.cmd == "rollback":
        r = rollback()
        print(f"{SENTINEL} rollback {'done' if r.get('ok') else 'FAILED'}: " + (r.get("summary") or r.get("detail") or ""))
        return 0 if r.get("ok") else 1
    st = _load(STATE, {})
    print(json.dumps({"installed": installed_version(), "release_url": release_url(), "last": st.get("last"), "last_rollback": st.get("last_rollback"),
                      "engines_kept": sorted(p.name for p in ENGINES.iterdir()) if ENGINES.exists() else []}, indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main())

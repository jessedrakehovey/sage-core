"""core_release.py - the template's public repository gets this agent's new work every week, on one tap.

core_sync.py keeps the template checkout (core_sync.json -> core_root) up to date with every commit here, organization
free; nothing published it. Pushes and releases were made by hand from whatever session happened to be open, so the
checkout drifted ahead of its public remote (2026-10-30: three commits nobody had pushed). Publishing is outward and
the repository is public, so it stays behind a tap, and behind the zero-personal-data gates (2026-10-29: `audit` = 0
was not clean, and a leak was public for 40 minutes):

  check    (weekly, from the spine) when the checkout is ahead of its remote and no publish card is open: run every
           gate; all clean -> ONE card "Publish the template update" whose tap runs `publish` (jobs.json core_publish);
           a gate fails -> a Mission Control note naming it, no card.
  publish  (the tap) every gate again on the checkout as it is NOW, then push the branch and cut a release
           (assistant/installer/windows/release.ps1 in the checkout). Its last line is said in chat.
  status   what is waiting, the last card, the last publish.

Gates: the checkout is clean; `core_sync.py audit` (every file, the organization's identity), `core_sync.py people`
(every person in the organization's records), `core_sync.py history` (every line and message the push would add, so a
name added then scrubbed between pushes still stops it), and the template's own tests run inside the checkout.
Switches: core_sync.json -> release {enabled, lane, tests, skip_subjects}. Zero LLM.

  python assistant/scripts/core_release.py check | publish | status [--dry-run]
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent          # assistant/scripts
ASSISTANT = HERE.parent
VAULT = ASSISTANT.parent
sys.path.insert(0, str(HERE))
import alib  # noqa: E402

STATE = alib.DATA / "core_release_state.json"
OWNER = "core_release"
JOB = "core_publish"
DEFAULTS = {"enabled": True, "lane": "", "tests": ["assistant/tests/test_sage_core.py", "assistant/tests/test_core_sync.py"],
            "skip_subjects": ["agent: wiki note", "agent: nightly state", "agent: skill", "core_sync refresh"]}


def cfg() -> dict:
    try:
        doc = json.loads((ASSISTANT / "core_sync.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        doc = {}
    c = dict(DEFAULTS, **(doc.get("release") or {}))
    c["core_root"] = str(doc.get("core_root") or "")
    c["enabled"] = bool(c.get("enabled")) and bool(doc.get("enabled", True)) and bool(c["core_root"])
    return c


def state() -> dict:
    try:
        return json.loads(STATE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def save(st: dict) -> None:
    STATE.parent.mkdir(parents=True, exist_ok=True)
    STATE.write_text(json.dumps(st, indent=1, ensure_ascii=False), encoding="utf-8")


def run(argv: list, cwd: Path, timeout: int = 900) -> tuple:
    try:
        r = subprocess.run(argv, cwd=str(cwd), capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=timeout,
                           stdin=subprocess.DEVNULL)
        return r.returncode, ((r.stdout or "") + (r.stderr or "")).strip()
    except (OSError, subprocess.TimeoutExpired) as e:
        return 1, f"{type(e).__name__}: {e}"


def git(core: Path, *args) -> str:
    return run(["git", *args], core, 120)[1]


def waiting(core: Path, c: dict) -> dict:
    """What the remote does not have yet: {upstream, count, subjects (the readable ones, newest first)}."""
    run(["git", "fetch", "-q"], core, 120)
    up = git(core, "rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{u}")
    if not up or " " in up or "fatal" in up:
        return {"upstream": "", "count": 0, "subjects": [], "error": "the checkout has no upstream branch"}
    rc, out = run(["git", "log", "--format=%s", f"{up}..HEAD"], core, 120)
    subs = [s.strip() for s in out.splitlines() if s.strip()] if rc == 0 else []
    skip = tuple(str(s).lower() for s in c.get("skip_subjects") or [])
    readable = [s.split(" (from ")[0] for s in subs if not s.lower().startswith(skip)]
    return {"upstream": up, "count": len(subs), "subjects": readable}


def gates(core: Path, c: dict) -> list:
    """[(gate, ok, last line)] - every one must pass before a card is posted and again before a push."""
    out = []
    dirty = git(core, "status", "--porcelain", "--untracked-files=no")
    out.append(("the checkout is clean", not dirty.strip(), dirty.splitlines()[0][:120] if dirty.strip() else "clean"))
    sync = [sys.executable, str(ASSISTANT / "core_sync.py")]
    for name, args in (("audit", ["audit", "--quiet"]), ("people", ["people"]), ("history", ["history", "--quiet"])):
        rc, txt = run(sync + args, VAULT, 600)
        last = next((ln for ln in reversed(txt.splitlines()) if "CORE_SYNC" in ln), txt.splitlines()[-1] if txt else "")
        out.append((f"core_sync {name}", rc == 0, last[:200]))
    for t in c.get("tests") or []:
        if not (core / t).exists():
            continue
        rc, txt = run([sys.executable, str(core / t)], core, 900)
        out.append((f"test {Path(t).name}", rc == 0, (txt.splitlines()[-1] if txt else "")[:200]))
    return out


def card_open(qid: str) -> bool:
    if not qid:
        return False
    import cards
    return (cards.LIVE / f"{qid}.json").exists()


def lane(c: dict) -> str:
    return str(c.get("lane") or "") or "personal"


def check(dry: bool = False) -> int:
    c = cfg()
    if not c["enabled"]:
        print("CORE_RELEASE off (no template checkout configured, or release.enabled false)")
        return 0
    core = Path(c["core_root"])
    st = state()
    w = waiting(core, c)
    if w.get("error"):
        print(f"CORE_RELEASE {w['error']}")
        return 0
    if not w["count"]:
        print("CORE_RELEASE nothing waiting: the remote has every commit")
        return 0
    if card_open(st.get("qid", "")):
        print(f"CORE_RELEASE a publish card is already open ({st['qid']}); {w['count']} commit(s) waiting")
        return 0
    res = gates(core, c)
    bad = [g for g in res if not g[1]]
    st.update({"checked": time.strftime("%Y-%m-%dT%H:%M:%S"), "waiting": w["count"], "gates": [list(g) for g in res]})
    if bad:
        text = (f"The template update did not reach a publish card: {len(bad)} check(s) failed, {w['count']} commit(s) waiting.\n"
                + "\n".join(f"- {g}: {last}" for g, _, last in bad))
        print("CORE_RELEASE gates failed:\n" + text)
        if not dry and st.get("last_fail") != [g[0] for g in bad]:
            import cards
            cards.note(OWNER, text)            # Mission Control only: a system note never goes to Chat
        st["last_fail"] = [g[0] for g in bad]
        if not dry:
            save(st)
        return 1
    st["last_fail"] = []
    top = list(dict.fromkeys(w["subjects"]))[:6]
    more = len(w["subjects"]) - len(top)
    q = (f"The public template is {w['count']} commit(s) behind this agent. New since the last publish:\n"
         + "\n".join(f"- {s[:140]}" for s in top) + (f"\n- and {more} more" if more > 0 else "")
         + "\nEvery privacy check passed (no organization names, no people, nothing in the commit history) and its tests pass. "
           "The checks run again at your tap, on whatever has synced by then, before anything is pushed.")
    question = {"question": q, "header": "Publish the template update", "lane": lane(c), "audience": "personal",
                "details": "\n".join(f"{g}: {last}" for g, _, last in res),
                "options": [{"label": "Publish (Recommended)", "description": "push to the public repository and cut a release"},
                            {"label": "Not this week"}],
                "exec": {"kind": "local", "payload": {"op": JOB, "args": {}}, "approve_label": "Publish (Recommended)"}}
    if dry:
        print("CORE_RELEASE dry-run: would post\n" + q)
        return 0
    import cards
    r = cards.post([question], owner=OWNER, lane=lane(c))
    if not r.get("posted"):
        print(f"CORE_RELEASE card refused: {r.get('reason')}")
        return 1
    st.update({"qid": r["qid"], "posted": time.strftime("%Y-%m-%dT%H:%M:%S"), "posted_head": git(core, "rev-parse", "--short", "HEAD")})
    save(st)
    print(f"CORE_RELEASE card {r['qid']} posted ({w['count']} commit(s) waiting)")
    return 0


def publish(dry: bool = False) -> int:
    c = cfg()
    if not c["enabled"]:
        print("Nothing was published: no template checkout is set up here.")
        return 1
    core = Path(c["core_root"])
    w = waiting(core, c)
    if w.get("error") or not w["count"]:
        print("Nothing was published: the public template already has everything." if not w.get("error")
              else f"Nothing was published: {w['error']}.")
        return 0 if not w.get("error") else 1
    res = gates(core, c)
    bad = [g for g in res if not g[1]]
    if bad:
        for g, _, last in bad:
            print(f"  {g}: {last}")
        print(f"Nothing was published: {len(bad)} privacy or test check(s) failed at the tap ({', '.join(g for g, _, _ in bad)}).")
        return 1
    if dry:
        print(f"dry-run: every check passed; would push {w['count']} commit(s) to {w['upstream']} and cut a release")
        return 0
    remote, _, branch = w["upstream"].partition("/")
    rc, txt = run(["git", "push", remote, f"HEAD:{branch}"], core, 300)
    if rc != 0:
        print(txt[-600:])
        print("Nothing was published: the push to the public repository failed.")
        return 1
    notes = "\n".join(f"- {s[:160]}" for s in list(dict.fromkeys(w["subjects"]))[:20]) or "maintenance"
    script = core / "assistant" / "installer" / "windows" / "release.ps1"
    rc, txt = run(["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(script), "-Notes", notes], core, 1800)
    tag = next((ln.split("]", 1)[1].strip().split()[0] for ln in txt.splitlines() if ln.startswith("[release]") and "published" in ln), "")
    st = state()
    st.update({"published": time.strftime("%Y-%m-%dT%H:%M:%S"), "published_count": w["count"], "tag": tag, "qid": ""})
    save(st)
    print(txt[-1500:])
    if rc != 0:
        print(f"The {w['count']} new commit(s) are pushed to the public repository, but the release build failed, so installed copies "
              f"will not update until a release is cut.")
        return 1
    print(f"The public template is up to date: {w['count']} new commit(s) pushed and released as {tag or 'a new version'}.")
    return 0


def status() -> int:
    c = cfg()
    st = state()
    if not c["enabled"]:
        print("off")
        return 0
    w = waiting(Path(c["core_root"]), c)
    print(f"template: {c['core_root']}  upstream: {w.get('upstream') or '-'}  waiting: {w['count']}")
    print(f"card: {st.get('qid') or '-'} ({'open' if card_open(st.get('qid', '')) else 'not open'})  posted: {st.get('posted', '-')}")
    print(f"last publish: {st.get('published', '-')} {st.get('tag', '')}  last failed gates: {', '.join(st.get('last_fail') or []) or '-'}")
    return 0


def main() -> int:
    sys.stdout.reconfigure(encoding="utf-8")
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("cmd", choices=("check", "publish", "status"))
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args()
    if a.cmd == "check":
        return check(a.dry_run)
    if a.cmd == "publish":
        return publish(a.dry_run)
    return status()


if __name__ == "__main__":
    sys.exit(main())

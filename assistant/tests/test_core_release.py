"""core_release (2026-10-30): the template's public repository gets new work only on a tap, and only past every gate.

A throwaway template checkout with a bare "public" remote. core_release's config, gates and the card store are pointed
at the fixture (gates are stubbed: core_sync's own gates have their tests in test_core_sync.py). Checks: nothing
waiting -> no card; commits waiting + clean gates -> ONE card whose exec is the core_publish job; a second check while
it is open posts nothing; publish with a failing gate pushes nothing; publish with clean gates pushes (the release
build is stubbed); the job is registered and takes no values.
   python assistant/tests/test_core_release.py
"""
import os as _os, tempfile as _tf
_os.environ.setdefault("WV_LOG_DIR", _tf.mkdtemp(prefix="wv_logs_"))
_os.environ["ASSISTANT_DATA"] = _tf.mkdtemp(prefix="wv_corerel_data_")
import json
import subprocess
import sys
import tempfile
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")
HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent / "scripts"))
import core_release as cr  # noqa: E402
import jobs  # noqa: E402

TMP = Path(tempfile.mkdtemp(prefix="wv_corerel_"))
REMOTE, CORE = TMP / "public.git", TMP / "core"
FAILS = []


def check(name, cond, extra=""):
    print(("ok   " if cond else "FAIL ") + name + (f"  {extra}" if extra and not cond else ""))
    if not cond:
        FAILS.append(name)


def g(cwd, *args):
    r = subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@example.invalid", *args], cwd=str(cwd),
                       capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=60)
    return r.stdout.strip()


def commit(msg):
    p = CORE / "assistant" / "x.py"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text((p.read_text(encoding="utf-8") if p.exists() else "") + f"# {msg}\n", encoding="utf-8")
    g(CORE, "add", "-A")
    g(CORE, "commit", "-q", "-m", msg)


subprocess.run(["git", "init", "-q", "--bare", str(REMOTE)], capture_output=True, timeout=60)
subprocess.run(["git", "init", "-q", "-b", "master", str(CORE)], capture_output=True, timeout=60)
commit("first")
g(CORE, "remote", "add", "origin", str(REMOTE))
g(CORE, "push", "-q", "-u", "origin", "master")

C = {"enabled": True, "core_root": str(CORE), "lane": "personal", "tests": [], "skip_subjects": ["agent: wiki note"]}
cr.cfg = lambda: dict(C)
GATES = {"ok": True}
cr.gates = lambda core, c: [("core_sync audit", GATES["ok"], "CORE_SYNC audit: 0 line(s)" if GATES["ok"] else "CORE_SYNC audit: 2 line(s)")]
POSTED, NOTES = [], []


class FakeCards:
    LIVE = TMP / "live"

    @staticmethod
    def post(qs, owner, lane=""):
        qid = f"q{len(POSTED) + 1}"
        POSTED.append(qs[0])
        FakeCards.LIVE.mkdir(exist_ok=True)
        (FakeCards.LIVE / f"{qid}.json").write_text("{}", encoding="utf-8")
        return {"posted": True, "qid": qid}

    @staticmethod
    def note(owner, text):
        NOTES.append(text)


sys.modules["cards"] = FakeCards

# ---------------------------------------------------------------- nothing waiting
rc = cr.check()
check("nothing waiting: no card", rc == 0 and not POSTED)

# ---------------------------------------------------------------- commits waiting, a gate fails: a note, no card
commit("rulings: a lone word never matches")
commit("agent: wiki note 'x' (applied, from y)")
GATES["ok"] = False
rc = cr.check()
check("a failed gate: no card, one Mission Control note naming it", rc == 1 and not POSTED and len(NOTES) == 1 and "core_sync audit" in NOTES[0], str(NOTES))
cr.check()
check("the same failure is not noted twice", len(NOTES) == 1, str(len(NOTES)))

# ---------------------------------------------------------------- clean: ONE card with the publish job
GATES["ok"] = True
rc = cr.check()
q = POSTED[0] if POSTED else {}
check("clean gates: one card, the readable change listed, the noise left out", rc == 0 and len(POSTED) == 1
      and "a lone word never matches" in q.get("question", "") and "wiki note" not in q.get("question", "")
      and "2 commit(s)" in q.get("question", ""), q.get("question", ""))
check("the card's tap runs the registered core_publish job, recommended first", q.get("exec", {}).get("kind") == "local"
      and q["exec"]["payload"] == {"op": "core_publish", "args": {}} and q["options"][0]["label"] == q["exec"]["approve_label"]
      and "(Recommended)" in q["options"][0]["label"], json.dumps(q.get("exec")))
cr.check()
check("while that card is open, no second card", len(POSTED) == 1, str(len(POSTED)))

# ---------------------------------------------------------------- publish: a gate failing at the tap pushes nothing
before = g(REMOTE, "rev-parse", "master")
GATES["ok"] = False
rc = cr.publish()
check("a gate failing at the tap: nothing pushed", rc == 1 and g(REMOTE, "rev-parse", "master") == before)

# ---------------------------------------------------------------- publish: clean -> pushed (release build stubbed)
GATES["ok"] = True
real_run = cr.run
cr.run = lambda argv, cwd, timeout=900: ((0, "[release] v2026.10.30-abc1234 published to https://example.invalid") if argv[0] == "powershell"
                                         else real_run(argv, cwd, timeout))
rc = cr.publish()
check("clean at the tap: the branch is pushed and the release tag recorded", rc == 0 and g(REMOTE, "rev-parse", "master") == g(CORE, "rev-parse", "HEAD")
      and cr.state().get("tag") == "v2026.10.30-abc1234" and not cr.state().get("qid"), json.dumps(cr.state()))
rc = cr.publish()
check("publishing again with nothing new is a no-op", rc == 0)

# ---------------------------------------------------------------- the job
j = jobs.find("core_publish")
check("core_publish is a registered job that takes no values", bool(j) and jobs.build_cmd(j, {})[-2:] == ["assistant/scripts/core_release.py", "publish"]
      and not jobs.required(j), json.dumps(j)[:200])

print()
print(f"{'ALL OK' if not FAILS else 'FAILED: ' + ', '.join(FAILS)}  ({TMP})")
sys.exit(1 if FAILS else 0)

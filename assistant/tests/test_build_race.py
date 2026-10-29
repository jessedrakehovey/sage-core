"""test_build_race.py - an owner's "Build it, I approve" tap must never be lost to another builds.json writer.

2026-10-18 19:01: the owner tapped Build it on two YouTube Scout cards a second apart. The first tap's kick
started a builder run that loaded builds.json before the second tap saved; core.stamp() is to the minute,
so the second tap's `answered` (19:01) was not newer than the run's start (19:01), and the run's save put
that request back to status=asked with no owner_ok. This reproduces it (same-minute stamps, two taps
racing a run that loaded first) in threads and in separate processes, and checks both approvals survive.

  python assistant/tests/test_build_race.py
"""
import os
import subprocess
import sys
import tempfile
import threading
from pathlib import Path

TMP = Path(tempfile.mkdtemp(prefix="build_race_"))
os.environ["WV_LOG_DIR"] = str(TMP / "logs")
os.environ["AGENT_STATE"] = str(TMP / "state")
os.environ["AGENT_DATA"] = str(TMP / "data")
os.environ["AGENT_SUGGESTIONS"] = str(TMP / "skill-suggestions.md")
os.environ["ASSISTANT_DATA"] = str(TMP / "adata")
os.environ["ASSISTANT_CARDS"] = str(TMP / "cards")
os.environ["CHAT_PUSH"] = "off"
os.environ["BUILDER_WT"] = str(TMP / "wt")

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "agent"))
sys.path.insert(0, str(ROOT / "scripts"))
import core  # noqa: E402
import builder  # noqa: E402

MINUTE = "2026-10-18 19:01"
core.stamp = lambda: MINUTE                         # every stamp in the same minute, as on 2026-10-18
builder.kick = lambda force=False: "BUILDER kicked (stub)"

A = "Wiki/Builder/Build request Catch jump cuts in the edit check.md"
B = "Wiki/Builder/Build request On-page SEO pass in the website check.md"
C = "Wiki/Builder/Build request something else.md"
IDEA = "(none: the owner approved the idea, not protected files)"

PASS = FAIL = 0


def check(name, cond):
    global PASS, FAIL
    PASS += bool(cond)
    FAIL += (not cond)
    print(("ok   " if cond else "FAIL ") + name)


def asked(title):
    return {"title": title, "attempts": 0, "status": "asked", "hits": [IDEA], "asked": "2026-10-18 01:40", "source": "yt-scout"}


def seed():
    core.save(builder.STATE, {A: asked("jump cuts"), B: asked("seo"), C: {"title": "else", "attempts": 0, "status": "retry"}})


def approved(st, k):
    s = st.get(k) or {}
    return s.get("status") == "approved" and s.get("owner_ok") == [IDEA] and s.get("answered") == MINUTE


# 1. the 19:01 sequence: a run loads, two taps land, the run saves its (stale) copy
seed()
run_state = core.load(builder.STATE, {})
base = __import__("json").loads(__import__("json").dumps(run_state))
started = core.stamp()
taps = [threading.Thread(target=builder.owner_answer, args=(k, builder.BUILD_OK, "owner1")) for k in (A, B)]
for t in taps:
    t.start()
for t in taps:
    t.join()
run_state[C].update(status="built", commit="abc1234", attempts=1)       # what the run itself did
builder.save_state(run_state, started, base)
st = core.load(builder.STATE, {})
check("race: both same-minute approvals survive a run that loaded before them", approved(st, A) and approved(st, B))
check("race: the run's own change to another request is kept", st[C].get("status") == "built" and st[C].get("commit") == "abc1234")

# 2. the same, through a caller with no snapshot (builds_requeue's old call): the owner's stamp still wins
seed()
stale = core.load(builder.STATE, {})
builder.owner_answer(B, builder.BUILD_OK, "owner1")
builder.save_state(stale, MINUTE)                                        # since == the tap's minute: the old bug
check("race: a same-minute tap beats a stale copy even without a snapshot", approved(core.load(builder.STATE, {}), B))

# 3. a key another writer added or removed while the run held its copy is not undone by the run's save
seed()
run_state = core.load(builder.STATE, {})
base = __import__("json").loads(__import__("json").dumps(run_state))
builder.edit_state(lambda s: s.update({"Wiki/Builder/Build request new idea.md": asked("new")}) or s.pop(C))
builder.save_state(run_state, MINUTE, base)
st = core.load(builder.STATE, {})
check("race: a request armed mid-run (YouTube Scout) is kept", "Wiki/Builder/Build request new idea.md" in st)
check("race: a request re-keyed away mid-run is not resurrected", C not in st)

# 4. separate processes: two taps in two processes while this process saves a stale run copy, many times
CHILD = r"""
import os, sys
sys.path.insert(0, sys.argv[1]); sys.path.insert(0, sys.argv[2])
import core, builder
core.stamp = lambda: %r
builder.kick = lambda force=False: "stub"
r = builder.owner_answer(sys.argv[3], builder.BUILD_OK, "owner1")
sys.exit(0 if r.get("ok") else 1)
""" % MINUTE
lost = 0
ROUNDS = 8
for i in range(ROUNDS):
    seed()
    run_state = core.load(builder.STATE, {})
    base = __import__("json").loads(__import__("json").dumps(run_state))
    kids = [subprocess.Popen([sys.executable, "-c", CHILD, str(ROOT / "agent"), str(ROOT / "scripts"), k], env=os.environ.copy())
            for k in (A, B)]
    run_state[C]["attempts"] = i + 1
    for _ in range(20):                                                  # the run saving while the taps land
        run_state[C]["saves"] = run_state[C].get("saves", 0) + 1
        builder.save_state(run_state, MINUTE, base)
    codes = [k.wait(60) for k in kids]
    builder.save_state(run_state, MINUTE, base)                         # and once more after them
    st = core.load(builder.STATE, {})
    if codes != [0, 0] or not (approved(st, A) and approved(st, B)):
        lost += 1
check(f"race: {ROUNDS} rounds of two taps in two processes racing a run's saves lose nothing", lost == 0)
check("race: the lock file is released", not (core.DATA / "state.lock").exists())

print(f"\n{PASS} passed, {FAIL} failed  (scratch: {TMP})")
sys.exit(1 if FAIL else 0)

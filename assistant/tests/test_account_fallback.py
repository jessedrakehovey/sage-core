"""Offline tests: the builder and the doer (browser and computer use) switch to profile.json
`claude_account_fallback` when a turn hits the usage limit, like the brain does (the owner 2026-10-26:
"the doer and builder need to be able to switch too"). No model, no browser, scratch stores.
   python assistant/tests/test_account_fallback.py
"""
import os as _os, tempfile as _tf
_os.environ.setdefault("WV_LOG_DIR", _tf.mkdtemp(prefix="wv_logs_"))   # never the live ~/.claude logs
import os
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
TMP = Path(tempfile.mkdtemp(prefix="wv_fallback_"))
os.environ["ASSISTANT_DATA"] = str(TMP / "adata")
os.environ["ASSISTANT_CARDS"] = str(TMP / "cards")
os.environ["ASSISTANT_LEARNINGS"] = str(TMP / "learnings")
os.environ["RULINGS_FILE"] = str(ROOT / "data" / "rulings.json")
os.environ["DOER_DATA"] = str(TMP / "doer")
os.environ["DOER_STATUS"] = str(TMP / "doer_status.md")
os.environ["DOER_VAULT_KEY"] = "test-key"
os.environ["AGENT_STATE"] = str(TMP / "agent_state")
os.environ["AGENT_DATA"] = str(TMP / "agent_data")
os.environ["AGENT_SUGGESTIONS"] = str(TMP / "sugg.md")
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT / "doer"))
sys.path.insert(0, str(ROOT / "agent"))
import core  # noqa: E402
import account  # noqa: E402
import builder  # noqa: E402
import doer  # noqa: E402
import computer  # noqa: E402

N = 0


def ok(cond, msg):
    global N
    N += 1
    if not cond:
        print(f"FAIL {N}: {msg}")
        sys.exit(1)
    print(f"ok {N}: {msg}")


LIMIT = "You've hit your session limit · resets 7:50pm (America/Los_Angeles)"
real_profile = core.profile


def with_fb(name):
    core.profile = lambda: {**real_profile(), "claude_account_fallback": name}
    core.save(account.LIMITS, {})        # a limit seen is remembered (reflex, 2026-10-27): each scenario starts fresh


ok(account.is_limit(LIMIT) and account.is_limit("", None, "rate limit reached") and not account.is_limit("timed out"),
   "account: the limit check matches the CLI's limit lines only")

# ---------------------------------------------------------------- doer (browser)
calls = []


def doer_turn(limited_first, session=""):
    def turn(prompt, out_dir, resume="", timeout=900, acct=None):
        calls.append((acct, resume, prompt))
        if acct is None and limited_first:
            return {"ok": False, "error": LIMIT, "text": "", "session": session or resume, "marker": "", "arg": ""}
        return {"ok": True, "text": "done", "session": "S-fb", "error": "", "marker": "", "arg": ""}
    return turn


try:
    with_fb("max20x")
    calls.clear(); core.save(account.LIMITS, {})     # a limit seen is remembered (reflex, 2026-10-27): each scenario starts fresh
    r = doer.with_fallback(doer_turn(True), "do the job", TMP)
    ok(r["ok"] and [c[0] for c in calls] == [None, "max20x"] and calls[1][2] == "do the job",
       "doer: a limit before any session runs the same turn on the fallback")
    calls.clear(); core.save(account.LIMITS, {})     # a limit seen is remembered (reflex, 2026-10-27): each scenario starts fresh
    r = doer.with_fallback(doer_turn(True, session="S-half"), "do the job", TMP)
    ok(r["ok"] and calls[1][1] == "S-half" and calls[1][2].startswith(doer.LIMIT_CONTINUE),
       "doer: a limit mid-job resumes that session on the fallback, told not to redo steps")
    calls.clear(); core.save(account.LIMITS, {})     # a limit seen is remembered (reflex, 2026-10-27): each scenario starts fresh
    r = doer.with_fallback(doer_turn(True), "go on", TMP, resume="S-old")
    ok(calls[1][1] == "S-old" and calls[1][2] == "go on", "doer: a limited resume is retried as the same resume")
    calls.clear(); core.save(account.LIMITS, {})     # a limit seen is remembered (reflex, 2026-10-27): each scenario starts fresh
    doer.with_fallback(doer_turn(False), "do the job", TMP)
    ok([c[0] for c in calls] == [None], "doer: a turn that worked runs once")

    def crashed(prompt, out_dir, resume="", timeout=900, acct=None):
        calls.append((acct, resume, prompt))
        return {"ok": False, "error": "it stopped on an error", "text": "", "session": "", "marker": "", "arg": ""}
    calls.clear(); core.save(account.LIMITS, {})     # a limit seen is remembered (reflex, 2026-10-27): each scenario starts fresh
    doer.with_fallback(crashed, "do the job", TMP)
    ok(len(calls) == 1, "doer: a crash that is not a limit is not retried")
    with_fb("")
    calls.clear(); core.save(account.LIMITS, {})     # a limit seen is remembered (reflex, 2026-10-27): each scenario starts fresh
    doer.with_fallback(doer_turn(True), "do the job", TMP)
    ok(len(calls) == 1, "doer: no fallback named, the limit is returned as is")

    # ------------------------------------------------------------ computer use rides the same wrapper
    with_fb("max20x")
    seen = []
    real_on = computer._run_turn_on

    def comp_on(prompt, out_dir, resume="", timeout=900, env_extra=None, acct=None):
        seen.append((acct, env_extra))
        if acct is None:
            return {"ok": False, "error": LIMIT, "text": "", "session": "", "marker": "", "arg": ""}
        return {"ok": True, "text": "done", "session": "S", "error": "", "marker": "", "arg": ""}
    computer._run_turn_on = comp_on
    try:
        r = computer.run_turn("click it", TMP, env_extra={"X": "1"})
    finally:
        computer._run_turn_on = real_on
    ok(r["ok"] and [s[0] for s in seen] == [None, "max20x"] and seen[1][1] == {"X": "1"},
       "computer use: a limit switches to the fallback, extra env kept")

    # ------------------------------------------------------------ builder
    runs, resets = [], []
    core.save(account.LIMITS, {})
    real_b, real_reset = builder._model_turn_on, builder.reset

    def b_on(req, wt, env, acct=None):
        runs.append(acct)
        if acct is None:
            return {"status": "error", "summary": LIMIT}
        return {"status": "built", "summary": "done"}
    builder._model_turn_on, builder.reset = b_on, (lambda wt: resets.append(wt))
    try:
        res = builder.model_turn({"key": "k"}, TMP, {})
        ok(res["status"] == "built" and runs == [None, "max20x"] and resets == [TMP],
           "builder: a limit resets the worktree and builds once more on the fallback")
        runs.clear(); resets.clear(); core.save(account.LIMITS, {})
        builder._model_turn_on = lambda req, wt, env, acct=None: (runs.append(acct), {"status": "error", "summary": "model turn timed out"})[1]
        builder.model_turn({"key": "k"}, TMP, {})
        ok(runs == [None] and not resets, "builder: a timeout is not a limit, no second account")
    finally:
        builder._model_turn_on, builder.reset = real_b, real_reset
finally:
    core.profile = real_profile

print(f"\nALL {N} CHECKS PASSED")

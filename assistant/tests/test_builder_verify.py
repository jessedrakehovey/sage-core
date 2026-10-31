"""Offline tests for the builder's 2026-10-23 changes ("make it so Sage can do anything I asked"): a
suite that fails once after a change is rerun before the build is blamed; the diff cap is 2000; the
'Build needs your OK' card is urgent; the builder renders its own state for the brain; the requeue
script can put 'breaks tests' and 'diff too large' failures back by hand. Scratch stores; no model,
no git, no PowerShell.
   python assistant/tests/test_builder_verify.py
"""
import os as _os, tempfile as _tf
_os.environ.setdefault("WV_LOG_DIR", _tf.mkdtemp(prefix="wv_logs_"))
import json
import os
import sys
import tempfile
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
TMP = Path(tempfile.mkdtemp(prefix="wv_builder_verify_"))
os.environ["ASSISTANT_DATA"] = str(TMP / "adata")
os.environ["ASSISTANT_CARDS"] = str(TMP / "cards")
os.environ["ASSISTANT_LEARNINGS"] = str(TMP / "learnings")
os.environ["DOER_DATA"] = str(TMP / "doer")
os.environ["AGENT_STATE"] = str(TMP / "agent_state")
os.environ["AGENT_DATA"] = str(TMP / "agent_data")
os.environ["AGENT_SUGGESTIONS"] = str(TMP / "sugg.md")
os.environ["CHAT_PUSH"] = "off"
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT / "agent"))
import core  # noqa: E402
import builder  # noqa: E402
import builds_requeue  # noqa: E402

N = 0


def ok(cond, msg):
    global N
    N += 1
    if not cond:
        print(f"FAIL {N}: {msg}")
        sys.exit(1)
    print(f"ok {N}: {msg}")


# ---------------------------------------------------------------- 1. verify: a flaky suite is rerun once before the build is blamed
WT = TMP / "wt"
WT.mkdir()
builder.changes = lambda wt: [("M", "assistant/scripts/x.md")]        # no .py: no compile step
builder.fence = lambda paths, allow, ignore: ([], [])
builder.diff_lines = lambda wt: 120
runs = []


def fake_tests(wt, env, only=()):
    runs.append(tuple(only))
    if not only:                                     # the full run after the change: two suites red
        return {"a.py": False, "b.py": False, "c.py": True}
    return {t: (t == "a.py") for t in only}         # the rerun: a.py was flaky, b.py is truly broken


builder.run_tests = fake_tests


class _Gate:
    returncode, stdout = 0, ""


builder.subprocess.run = lambda *a, **k: _Gate()
baseline = {"a.py": True, "b.py": True, "c.py": True}
okv, why, paths = builder.verify(WT, baseline, {})
ok(not okv and why == "breaks tests: b.py", f"only the suite that fails twice is blamed ({why})")
ok(runs == [(), ("a.py", "b.py")], f"one full run, then ONE rerun of just the red suites ({runs})")
runs.clear()
builder.run_tests = lambda wt, env, only=(): (runs.append(tuple(only)) or {"a.py": True, "b.py": True, "c.py": True})
ok(builder.verify(WT, baseline, {})[0] and runs == [()], "all green: no rerun")
runs.clear()
builder.run_tests = lambda wt, env, only=(): (runs.append(tuple(only)) or ({"a.py": False, "b.py": True, "c.py": True} if not only else {"a.py": True}))
ok(builder.verify(WT, baseline, {})[0] and runs == [(), ("a.py",)], "a suite green on the rerun never fails the build")
ok(builder.MAX_DIFF_LINES == 2000, "the diff cap is 2000 lines")
builder.diff_lines = lambda wt: 2001
ok(builder.verify(WT, baseline, {})[1].startswith("diff too large"), "the cap still holds above it")

# ---------------------------------------------------------------- 2. the brain sees the builder
today = time.strftime("%Y-%m-%d")
state = {"_runs": {today: 3}, "_last_start": time.time(),
         "Wiki/Operations/Build request Catch jump cuts in the edit check.md": {"status": "failed", "summary": "diff too large (1433 lines > 600)", "last": core.stamp()},
         "Wiki/Operations/Build request Weekly search-terms report.md": {"status": "approved", "last": core.stamp()},
         "Wiki/Operations/Build request Old thing.md": {"status": "built", "last": "2026-09-01 10:00"},
         "Wiki/Personal/Build request Asked one.md": {"status": "asked", "last": "2026-09-01 10:00"}}
core.save(builder.STATE, state)
builder.running = lambda: False
r = builder.render()
ok(r.startswith("BUILDS") and f"3 of {builder.RUNS_PER_DAY} scheduled runs used today" in r and "nothing building right now" in r,
   "runs used today and the cap are in the brain's context")
ok("- Catch jump cuts in the edit check: failed (diff too large" in r and "- Weekly search-terms report: approved, waiting for the next run" in r,
   "recent requests with their state in plain words")
ok("Old thing" not in r and "- Asked one: waiting for an owner's tap on its card" in r, "old finished ones drop out; anything still waiting on an owner stays")
import brain  # noqa: E402
ok("BUILDS" in brain.context_block(), "the block rides into the context")

# ---------------------------------------------------------------- 3. the owner card is urgent
import inspect  # noqa: E402
src = inspect.getsource(builder.ask_owner)
ok('"urgent": True' in src, "'Build needs your OK' is posted urgent (reaches chat now, not Monday)")

# ---------------------------------------------------------------- 4. requeue by hand: broken tests / too large
state.update({"Wiki/Operations/Build request Doer report cut off.md": {"status": "failed", "summary": "breaks tests: assistant/tests/test_phone_bridge.py", "last": core.stamp(), "attempts": 2}})
core.save(builder.STATE, state)
r = builds_requeue.tick(dry=True, kick=False)
ok(r["requeued"] == [], "the spine's own tick leaves 'breaks tests' and 'diff too large' alone")
r = builds_requeue.tick(dry=False, kick=False, broken_tests=True, too_large=True)
keys = sorted(x["key"].rsplit("/", 1)[-1] for x in r["requeued"])
ok(keys == ["Build request Catch jump cuts in the edit check.md", "Build request Doer report cut off.md"], f"both kinds go back by hand ({keys})")
st = core.load(builder.STATE, {})
ok(all(st[k]["status"] == "retry" and st[k]["attempts"] == 0 and st[k].get("prior_summary") for k in
       ("Wiki/Operations/Build request Catch jump cuts in the edit check.md", "Wiki/Operations/Build request Doer report cut off.md")),
   "re-queued entries are retry with attempts 0 and the old reason kept")
ok(st["Wiki/Operations/Build request Weekly search-terms report.md"]["status"] == "approved", "nothing else moved")

# ---------------------------------------------------------------- the owner's own ask approves the protected core (2026-10-30)
_owner = next((k for k, u in (core.people().get("users") or {}).items() if str(u.get("role")) == "owner"), "")
_staff = next((k for k, u in (core.people().get("users") or {}).items() if str(u.get("role")) == "staff"), "")
_req_owner = {"key": "Wiki/Personal/Build request x.md", "text": "# Build request x\n\nfix it\n\n<!-- capability: {\"gap\": \"g-1\", \"person\": \"" + _owner + "\", \"ask\": \"fix it\"} -->"}
_req_dream = {"key": "Wiki/Personal/Build request y.md", "text": "# Build request y\n\nfrom the nightly review"}
_req_staff = {"key": "Wiki/Personal/Build request z.md", "text": "# Build request z\n\n<!-- capability: {\"gap\": \"g-2\", \"person\": \"" + (_staff or "nobody") + "\"} -->"}
ok(bool(_owner) and builder.owner_asked(_req_owner) == _owner, "owner_asked: the capability footer names the owner")
ok(builder.owner_asked(_req_dream) == "" and builder.owner_asked(_req_staff) == "", "owner_asked: a dream-filed or staff-filed request is nobody's ask")
_prot = ["assistant/doer/computer_policy.py", "assistant/agent/brain.py"]
ok(builder.auto_ok(_req_owner, _prot) == sorted(_prot), "auto_ok: protected (not tap, not hard) files are approved by the owner's own ask")
ok(builder.auto_ok(_req_dream, _prot) == [], "auto_ok: nothing for a request nobody asked for")
ok(builder.auto_ok(_req_owner, _prot + ["assistant/scripts/email_send.py"]) == [] and builder.tap_hits(["assistant/scripts/email_send.py", "assistant/doer/vault.py", "assistant/agent/brain.py"]) == ["assistant/scripts/email_send.py", "assistant/doer/vault.py"],
   "auto_ok: a send or credential path keeps the whole request on a card (tap_hits)")
ok(builder.auto_ok(_req_owner, ["scripts/guardrails/check_gates.py"]) == [], "auto_ok: a hard path is never approved")

print(f"\nALL {N} PASSED")

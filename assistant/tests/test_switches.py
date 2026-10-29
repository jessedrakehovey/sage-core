"""Offline tests for switches (2026-10-23): an owner turns a standing job (the nightly YouTube Scout)
on or off from chat. The whole chain - the `switch` tool (gmcp) -> intents.validate/record ->
intents.collect -> actions.apply -> switches.set_on - runs against a scratch registry and a scratch
config file. Scratch stores; no model, no Google, no Chat.
   python assistant/tests/test_switches.py
"""
import os as _os, tempfile as _tf
_os.environ.setdefault("WV_LOG_DIR", _tf.mkdtemp(prefix="wv_logs_"))
import json
import os
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
TMP = Path(tempfile.mkdtemp(prefix="wv_switches_"))
os.environ["ASSISTANT_DATA"] = str(TMP / "adata")
os.environ["ASSISTANT_CARDS"] = str(TMP / "cards")
os.environ["ASSISTANT_LEARNINGS"] = str(TMP / "learnings")
os.environ["DOER_DATA"] = str(TMP / "doer")
os.environ["AGENT_STATE"] = str(TMP / "agent_state")
os.environ["AGENT_DATA"] = str(TMP / "agent_data")
os.environ["AGENT_SUGGESTIONS"] = str(TMP / "sugg.md")
os.environ["CHAT_PUSH"] = "off"
VAULT = TMP / "vault"
CFG = VAULT / "scripts" / "yt_scout" / "config.json"
CFG.parent.mkdir(parents=True)
CFG_TEXT = ('{\n  "_what": "test",\n  "enabled": true,\n  "videos_per_night": 1,\n'
            '  "topics": [\n    {"q": "a", "lane": "wedding"},\n    {"q": "b", "lane": "builder"}\n  ]\n}\n')
CFG.write_text(CFG_TEXT, encoding="utf-8")
REG = TMP / "switches.json"
REG.write_text(json.dumps({"jobs": [{"key": "youtube-scout", "name": "YouTube Scout", "what": "nightly",
                                     "config": "scripts/yt_scout/config.json", "field": "enabled",
                                     "aliases": ["youtube scout", "youtube"]}]}), encoding="utf-8")
os.environ["AGENT_SWITCHES"] = str(REG)
os.environ["AGENT_SWITCH_ROOT"] = str(VAULT)
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT / "agent"))
import core  # noqa: E402
import actions  # noqa: E402
import brain  # noqa: E402
import gmcp  # noqa: E402
import intents  # noqa: E402
import routines  # noqa: E402
import switches  # noqa: E402

N = 0


def ok(cond, msg):
    global N
    N += 1
    if not cond:
        print(f"FAIL {N}: {msg}")
        sys.exit(1)
    print(f"ok {N}: {msg}")


owner1 = core.person_for(handle="owner1")
assert owner1 and core.can(owner1, "schedule"), "test needs the owner from people.json"
actions.WAKE_DOER = False

# ---------------------------------------------------------------- 1. the registry and the context
ok(switches.find("the YouTube scout")["key"] == "youtube-scout", "find: loose names reach the job")
ok(switches.find("payroll") is None, "find: an unknown job is not guessed")
ok("youtube-scout" in routines.render() and "is ON" in routines.render(), "the brain's context lists the job and that it is on")
ok("mcp__example-google__switch" in brain.ALLOWED, "the brain may call the switch tool")
ok("switch" in gmcp.WRITE_NAMES, "gmcp offers the switch tool")

# ---------------------------------------------------------------- 2. the whole chain: tool -> intent -> apply
turn = intents.begin(owner1, "spaces/AAQAtest", "")
os.environ["AGENT_TURN"] = str(turn)
r = gmcp.call("switch", {"job": "youtube-scout", "on": False})
ok(r.startswith("accepted") and "turns off" in r, "switch off: accepted, with how to phrase it")
ok(json.loads(CFG.read_text(encoding="utf-8"))["enabled"] is True, "nothing changes until the bridge applies the turn")
out = actions.apply(intents.collect(turn), owner1, {"convo": "spaces/AAQAtest", "thread": ""})
ok(not out, "applied quietly (no error line)")
ok(json.loads(CFG.read_text(encoding="utf-8"))["enabled"] is False, "the scout's config now says off")
ok(CFG.read_text(encoding="utf-8") == CFG_TEXT.replace('"enabled": true', '"enabled": false'), "only the one field changed; the file's layout is kept")
ok("is OFF" in routines.render(), "the context now shows it off")
ok(any(r.get("kind") == "switch" for r in core.rows(core.CHANGES)), "the change is in the ledger")

turn = intents.begin(owner1, "spaces/AAQAtest", "")
os.environ["AGENT_TURN"] = str(turn)
r = gmcp.call("switch", {"job": "youtube", "on": False})
ok(r.startswith("accepted") and "already off" in r, "switching off what is off: said, nothing changes")
r = gmcp.call("switch", {"job": "payroll", "on": True})
ok(r.startswith("refused"), "an unknown job is refused")
r = gmcp.call("switch", {"job": "youtube-scout"})
ok(r.startswith("refused"), "on/off must be given")
r = gmcp.call("switch", {"job": "youtube-scout", "on": "on"})
ok(r.startswith("accepted") and "turns on" in r, "'on' as a word is understood")
actions.apply(intents.collect(turn), owner1, {"convo": "spaces/AAQAtest", "thread": ""})
ok(json.loads(CFG.read_text(encoding="utf-8"))["enabled"] is True, "switched back on")

# ---------------------------------------------------------------- 3. only owners
GUEST = {"key": "guest", "name": "Guest", "role": "guest"}
if not core.can(GUEST, "schedule"):
    ok(not intents.validate("switch", {"job": "youtube-scout", "on": False}, GUEST, {})[0], "a non-owner can't switch a job")
    ok(switches.state(switches.find("youtube-scout")) is True, "and the job stays as it was")

# ---------------------------------------------------------------- 4. a config the switch can't safely edit is refused
CFG.write_text('{"enabled": true, "nested": {"enabled": false}}', encoding="utf-8")
ok(switches.set_on("youtube-scout", False)["status"] == "refused", "two 'enabled' fields: refused rather than guess which")
CFG.write_text(CFG_TEXT, encoding="utf-8")

# ---------------------------------------------------------------- 5. scheduled tasks: on / off / run now through a fake PowerShell (2026-10-23)
TASKS = {"example YouTube Scout": "Ready", "example-org": "Ready"}
calls = []


def fake_ps(script):
    calls.append(script)
    import re as _re
    if script.startswith("Get-ScheduledTask -TaskName @("):
        names = [n.replace("''", "'") for n in _re.findall(r"'((?:[^']|'')*)'", script.split("@(", 1)[1].split(" -ErrorAction", 1)[0])]
        return 0, "\n".join(f"{n}|{TASKS[n]}" for n in names if n in TASKS)
    m = _re.match(r"(Enable|Disable|Start)-ScheduledTask -TaskName '((?:[^']|'')*)'", script)
    if not m:
        return 1, "unexpected"
    verb, name = m.group(1), m.group(2).replace("''", "'")
    if name not in TASKS:
        return 1, "Get-ScheduledTask : No MSFT_ScheduledTask objects found"
    if verb == "Disable":
        TASKS[name] = "Disabled"
    elif verb == "Enable":
        TASKS[name] = "Ready"
    else:
        if TASKS[name] == "Disabled":
            return 1, "Start-ScheduledTask : The task is disabled"
        TASKS[name] = "Running"
    return 0, TASKS[name]


switches._ps = fake_ps
REG.write_text(json.dumps({"jobs": [
    {"key": "youtube-scout", "name": "YouTube Scout", "what": "nightly", "config": "scripts/yt_scout/config.json", "field": "enabled",
     "task": "example YouTube Scout", "aliases": ["youtube scout", "the scout"]},
    {"key": "dreaming", "name": "Dreaming", "what": "6:30 daily", "task": "example-org", "aliases": ["dream"]},
    {"key": "ghost", "name": "Ghost", "what": "not registered", "task": "No Such Task"},
]}), encoding="utf-8")
switches._STATES.update(ts=0.0, map={})
ok(switches.find("dream")["key"] == "dreaming" and switches.find("example-org")["key"] == "dreaming", "a task-only job is found by alias or task name")
ok(switches.state(switches.find("dreaming")) is True and "dreaming: Dreaming (6:30 daily) is ON; can run now" in switches.render(), "a task-only job reads its state from the task, and says it can run now")
ok(len([c for c in calls if c.startswith("Get-ScheduledTask")]) == 1, "one batched PowerShell call for every task, cached")
r = switches.set_on("dreaming", False, by="test")
ok(r["status"] == "applied" and TASKS["example-org"] == "Disabled" and switches.state(switches.find("dreaming")) is False,
   "off: the task is disabled and the state reads OFF at once (cache dropped)")
ok(switches.set_on("dreaming", False)["status"] == "unchanged", "off again: unchanged")
ok(switches.run_now("dreaming")["status"] == "refused", "a disabled task is not started")
r = switches.set_on("dreaming", True, by="test")
ok(r["status"] == "applied" and TASKS["example-org"] == "Ready", "on: enabled again")
r = switches.run_now("dreaming", by="test")
ok(r["status"] == "started" and TASKS["example-org"] == "Running", "run now: Start-ScheduledTask, one run")
ok(any(c.get("kind") == "switch_run" for c in core.rows(core.CHANGES)), "the run is in the ledger")
r = switches.set_on("youtube-scout", False, by="test")
ok(r["status"] == "applied" and json.loads(CFG.read_text(encoding="utf-8"))["enabled"] is False and TASKS["example YouTube Scout"] == "Disabled",
   "a job with a config field AND a task: both go off")
ok(switches.run_now("youtube-scout")["status"] == "refused" and "turn it on first" in switches.run_now("youtube-scout")["reason"], "a config-off job is not started")
switches.set_on("youtube-scout", True, by="test")
ok(TASKS["example YouTube Scout"] == "Ready" and json.loads(CFG.read_text(encoding="utf-8"))["enabled"] is True, "and both back on")
ok(switches.set_on("ghost", False)["status"] == "refused" and switches.run_now("ghost")["status"] == "refused", "a task Windows does not have is refused, not invented")

# the whole chain for run-now: tool -> intent -> apply
turn = intents.begin(owner1, "spaces/AAQAtest", "")
os.environ["AGENT_TURN"] = str(turn)
r = gmcp.call("switch", {"job": "dreaming", "run": True})
ok(r.startswith("accepted") and "starts right after this reply" in r, "switch run: accepted with how to phrase it")
r = gmcp.call("switch", {"job": "dreaming"})
ok(r.startswith("refused") and "run: true" in r, "neither on nor run: refused, with the two forms")
TASKS["example-org"] = "Ready"
out = actions.apply(intents.collect(turn), owner1, {"convo": "spaces/AAQAtest", "thread": ""})
ok(not out and TASKS["example-org"] == "Running", "applied: the task is started, no error line")
print(f"\nALL {N} PASSED")

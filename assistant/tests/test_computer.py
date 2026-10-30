"""Offline tests for the doer's computer lane (assistant/doer/computer.py + computer_policy.py):
the code-enforced tiers, the script scan, the hook contract (fail closed, sha-bound approval,
once), path policy for Write/Edit, routing of a `do` with where=computer, the checkpoint card
carrying the exact command, drain's dry run, the prompt and the tools index. Scratch dirs; no
model, no browser, no network.   python assistant/tests/test_computer.py
"""
import os as _os, tempfile as _tf
_os.environ.setdefault("WV_LOG_DIR", _tf.mkdtemp(prefix="wv_logs_"))
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
VAULT = ROOT.parent
TMP = Path(tempfile.mkdtemp(prefix="wv_computer_"))
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
import cards  # noqa: E402
import exec_store as es  # noqa: E402
import tickets  # noqa: E402
import doer  # noqa: E402
import computer  # noqa: E402
import computer_policy as cp  # noqa: E402
import report  # noqa: E402
import actions  # noqa: E402
import core  # noqa: E402

PY = sys.executable
POLICY = ROOT / "doer" / "computer_policy.py"
N = 0


def ok(cond, msg):
    global N
    N += 1
    if not cond:
        print(f"FAIL {N}: {msg}")
        sys.exit(1)
    print(f"ok {N}: {msg}")


def tier(cmd):
    return cp.classify(cmd)[0]


# ---------------------------------------------------------------- 1. tiers by code
ok(tier("ls C:/Users/owner1/Documents") == "A", "listing a folder is A")
ok(tier("cat Wiki/_meta/index.md") == "A", "reading the wiki index is A")
ok(tier("ffprobe -v error E:/Weddings/Video/ZR1/Card 1/A001.MOV") == "A", "probing footage is A")
ok(tier("git status") == "A", "git status is A")
ok(tier("mkdir -p assistant/out/doer/abc/x") == "B", "a folder inside the write roots is B")
ok(tier("echo hi > assistant/out/doer/abc/r.txt") == "B", "redirect into the write roots is B")
ok(tier("echo hi > C:/Users/owner1/Desktop/r.txt") == "C", "redirect outside the write roots is C")
ok(tier("rm -rf assistant/out/doer/abc") == "C", "a delete even inside the write roots is C")
ok(tier("del C:/temp/a.txt") == "C", "del is C")
ok(tier("pip install rich") == "C" and tier("python -m pip install rich") == "C", "installing is C")
ok(tier("git commit -m x") == "C" and tier("git push") == "C", "git commit/push are C")
ok(tier("curl -X POST https://x.y -d a=b") == "C", "a network send is C")
ok(tier("taskkill /F /IM Resolve.exe") == "C", "killing a process is C")
ok(tier("robocopy E:/Card1 F:/Backup/Video /E") == "C", "copying INTO a footage folder is C")
ok(tier("format D:") == "NEVER" and tier("diskpart") == "NEVER", "formatting is NEVER")
ok(tier("rm E:/Weddings/Video/ZR1/Card 1/A001.MOV") == "NEVER", "deleting a footage file is NEVER")
ok(tier("mv D:/Shoots/Audio/H5/ZOOM0001.WAV D:/x.wav") == "NEVER", "moving footage is NEVER")
ok(tier("rm -rf Raw/Drive") == "NEVER" and tier("rm -rf .git") == "NEVER", "deleting protected vault paths is NEVER")
ok(tier("cat ~/.example-mail/creds.json") == "NEVER", "reading a credential file is NEVER")
ok(tier("cat 'C:/Users/owner1/Documents/owner1'\"'\"'s Vault/CLAUDE.md'") == "NEVER" or tier("ls \"C:/Users/owner1/Documents/owner1's Vault\"") == "NEVER", "the property vault is NEVER")
ok(tier("schtasks /query") == "NEVER" and tier("reg add HKCU\\x /v y") == "NEVER", "scheduled tasks and the registry are NEVER")
ok(tier("powershell -enc AAAA") == "NEVER" and tier("grep -e foo a.txt") == "A", "encoded PowerShell is NEVER, grep -e is not")
ok(tier("python assistant/scripts/drain.py --tick") == "NEVER", "the approval chain is NEVER")
ok(tier("python assistant/scripts/cards.py answer q1 Approve") == "NEVER", "answering a card is NEVER")
ok(tier("claude -p 'hi'") == "NEVER" and tier("python x.py --dangerously-skip-permissions") == "NEVER", "a nested agent is NEVER")
ok(tier("python -c \"import os; os.remove('x')\"") == "C", "inline python with a delete is C")
ok(tier("python -c \"import socket\"") == "NEVER", "inline python with a socket is NEVER")
ok(tier("python assistant/out/doer/nope/missing.py") == "NEVER", "running a script that does not exist is refused")
ok(tier("") == "NEVER", "an empty command is refused")
ok(tier("python assistant/doer/report.py checkpoint --ticket 0123456789abcdef --file assistant/out/doer/x/c.json") in ("A", "B"), "the checkpoint command is allowed")

# ---------------------------------------------------------------- 2. script scan
good = "# tool: count clips\nimport sys, pathlib\nprint(len(list(pathlib.Path(sys.argv[1]).rglob('*.MOV'))))\n"
ok(cp.scan_script(good)[0] == "B", "a clean tool scans B")
ok(cp.scan_script("import shutil\nshutil.rmtree('x')\n")[0] == "C", "a tool that deletes scans C")
ok(cp.scan_script("import smtplib\n")[0] == "NEVER", "a tool that imports smtplib scans NEVER")
ok(cp.scan_script("from ctypes import windll\n")[0] == "NEVER", "a tool that imports ctypes scans NEVER")
ok(cp.scan_script("x = eval(input())\n")[0] == "NEVER", "eval scans NEVER")
ok(cp.scan_script("import subprocess\nsubprocess.run(['ffprobe', 'a.mov'])\n")[0] == "B", "subprocess with a read-only program is B")
ok(cp.scan_script("import subprocess\nsubprocess.run('format D:', shell=True)\n")[0] == "NEVER", "subprocess with a NEVER string is NEVER")
tool_dir = ROOT / "doer" / "tools"
tf = Path.home() / "zz_wv_clean_tool_tmp.py"
tf.write_text(good, encoding="utf-8")
try:
    ok(tier(f"python {tf.as_posix()} E:/x") == "C", "a scanned-clean script OUTSIDE the tool folders is C")
finally:
    tf.unlink()
tool_path = ROOT / "out" / "doer" / "testtool_tmp.py"
tool_path.parent.mkdir(parents=True, exist_ok=True)
tool_path.write_text(good, encoding="utf-8")
try:
    ok(tier(f"python {tool_path.as_posix()} E:/x") == "B", "a scanned-clean script inside the write roots is B")
    bad_tool = ROOT / "out" / "doer" / "testtool_bad_tmp.py"
    bad_tool.write_text("import os\nos.remove('x')\n", encoding="utf-8")
    ok(tier(f"python assistant/out/doer/{bad_tool.name}") == "C", "a tool with a delete needs a card to run")
    bad_tool.unlink()
finally:
    tool_path.unlink()

# ---------------------------------------------------------------- 3. Write/Edit path policy
ok(cp.check_path_write("assistant/out/doer/abc/report.md", "# r")[0] == "B", "writing under out/doer is B")
ok(cp.check_path_write("assistant/doer/tools/clips.py", good)[0] == "B", "saving a clean tool is B")
ok(cp.check_path_write("assistant/doer/tools/evil.py", "import socket\n")[0] == "NEVER", "a tool with a socket is not even saved")
ok(cp.check_path_write("Wiki/Couples/x.md", "x")[0] == "C", "writing the wiki is refused")
ok(cp.check_path_write("E:/Weddings/Video/A.MOV", "x")[0] == "NEVER", "writing footage is NEVER")
ok(cp.check_path_write("assistant/data/doer/agent.kdbx", "x")[0] == "NEVER", "writing the vault is NEVER")
ok(cp.check_path_write("assistant/doer/tools/big.py", "x\n" * 500)[0] == "C", "an oversized tool is refused")


# ---------------------------------------------------------------- 4. the hook contract
def hook(evt, env=None):
    e = {**os.environ, **(env or {})}
    r = subprocess.run([PY, str(POLICY), "--hook"], input=json.dumps(evt), capture_output=True, text=True, env=e, timeout=60)
    try:
        return json.loads(r.stdout.strip().splitlines()[-1])["hookSpecificOutput"]
    except Exception:  # noqa: BLE001
        return {"permissionDecision": "parse-error", "permissionDecisionReason": r.stdout + r.stderr}


d = hook({"tool_name": "Bash", "tool_input": {"command": "ls assistant"}})
ok(d["permissionDecision"] == "allow", "hook allows a read")
d = hook({"tool_name": "Bash", "tool_input": {"command": "rm -rf C:/temp/x"}})
ok(d["permissionDecision"] == "deny" and "checkpoint" in d["permissionDecisionReason"].lower(), "hook refuses tier C and asks for a checkpoint")
d = hook({"tool_name": "Bash", "tool_input": {"command": "format D:"}})
ok(d["permissionDecision"] == "deny" and "no card" in d["permissionDecisionReason"].lower(), "hook refuses NEVER with no card")
d = hook({"tool_name": "Write", "tool_input": {"file_path": "Memory/memory.md", "content": "x"}})
ok(d["permissionDecision"] == "deny", "hook refuses a write outside the roots")
d = hook({"tool_name": "Bash"})
ok(d["permissionDecision"] == "deny", "hook fails closed on a malformed event")
r = subprocess.run([PY, str(POLICY), "--hook"], input="not json", capture_output=True, text=True, timeout=60)
ok('"deny"' in r.stdout, "hook fails closed on garbage stdin")
used = TMP / "approved.used"
cmd = "rm -rf  C:/temp/x"
env = {"DOER_APPROVED_SHA": cp.sha(cmd), "DOER_APPROVED_USED": str(used)}
d = hook({"tool_name": "Bash", "tool_input": {"command": "rm -rf C:/temp/x"}}, env)
ok(d["permissionDecision"] == "allow" and used.exists(), "the sha-bound approved command runs (whitespace-normalised)")
d = hook({"tool_name": "Bash", "tool_input": {"command": "rm -rf C:/temp/x"}}, env)
ok(d["permissionDecision"] == "deny", "the approved command runs only once")
d = hook({"tool_name": "Bash", "tool_input": {"command": "rm -rf C:/temp/y"}}, {**env, "DOER_APPROVED_USED": str(TMP / "u2")})
ok(d["permissionDecision"] == "deny", "a different tier C command is still refused during the approved turn")
d = hook({"tool_name": "Bash", "tool_input": {"command": "format D:"}}, {"DOER_APPROVED_SHA": cp.sha("format D:"), "DOER_APPROVED_USED": str(TMP / "u3")})
ok(d["permissionDecision"] == "deny", "a NEVER command is not unlocked by an approval")

# ---------------------------------------------------------------- 5. routing: Sage's do with where=computer
actions.WAKE_DOER = False
person = {"key": "owner1", "name": "owner1", "role": "owner"}
core.PEOPLE = getattr(core, "PEOPLE", None)
out = actions.apply({"do": {"lane": "home", "where": "computer", "text": "check how much free space is on every drive"}}, person, {"convo": "dm:owner1"})
tk = [t for t in tickets._load().values() if t.get("kind") == "agent_run"]
ok(len(tk) == 1 and tk[0]["payload"].get("where") == "computer", "a do with where=computer makes a computer agent_run ticket")
ok(computer.is_computer(tk[0]) and not computer.is_computer({"payload": {}}), "is_computer reads payload.where")
t0 = tk[0]
prompt = computer.build_prompt(t0)
ok("free space" in prompt and "assistant/doer/report.py checkpoint" in prompt and "DOER_DONE" in prompt, "the computer prompt carries the task, the checkpoint recipe and the markers")
ok("footage is sacred" in prompt.lower() and "assistant/out/doer" in prompt, "the prompt names the footage rule and the write roots")
r = doer.run_ticket(t0, dry=True)
ok(r == "DRY_RUN_OK", "doer.run_ticket dispatches a computer ticket to computer.run_ticket (dry)")
out2 = actions.apply({"do": {"lane": "home", "text": "check my DMV renewal"}}, person, {"convo": "dm:owner1"})
tk2 = [t for t in tickets._load().values() if t.get("kind") == "agent_run" and not computer.is_computer(t)]
ok(len(tk2) == 1 and tk2[0]["payload"].get("where") == "", "a do without where stays a browser task")

# ---------------------------------------------------------------- 6. the checkpoint card carries the exact command
spec = {"step": "Delete the 412 Resolve cache files (38 GB) under D:/CacheClip", "command": "rm -rf  D:/CacheClip/*",
        "values": {"folder": "D:/CacheClip", "files": "412", "size": "38 GB"}, "resume": "re-count the files, run the command, confirm the folder is empty"}
sf = TMP / "cp.json"
sf.write_text(json.dumps(spec), encoding="utf-8")
line = report.checkpoint(t0["id"], str(sf))
ok(line.startswith("CHECKPOINT="), f"the checkpoint card posts ({line})")
qid = line.split("=", 1)[1]
raw = next((c for c in cards._live() if c.get("id") == qid), None)
q = ((raw or {}).get("questions") or [{}])[0]
ok(raw is not None and "Runs exactly: rm -rf D:/CacheClip/*" in q.get("question", ""), "the card shows the exact command")
pl = (q.get("exec") or {}).get("payload") or {}
ok(pl.get("command") == "rm -rf D:/CacheClip/*" and pl.get("ticket") == t0["id"], "the exec payload carries the command and the ticket")
ok(es.validate_payload("agent_run", pl) == "", "exec_store accepts the agent_run payload with a command")
ok(computer.run_approved({"ticket": "0000000000000000", "step": "x", "resume": "y", "command": "ls"}, dry=True) == 1, "an unknown ticket is refused on the approved path")
blk = computer.approved_block(pl)
ok("rm -rf D:/CacheClip/*" in blk and "once" in blk, "the approved block names the exact command")
rc = doer.run_approved(pl, dry=True)
ok(rc == 0, "doer.run_approved dispatches a computer payload to computer.run_approved (dry)")

# ---------------------------------------------------------------- 7. tools index
idx_before = dict(computer.index())
tp = tool_dir / "zz_test_tool_tmp.py"
tp.write_text("# tool: count MOV clips in a folder\n# triggers: count clips, how many clips\n# tier: A\n# usage: python assistant/doer/tools/zz_test_tool_tmp.py <folder>\n" + good, encoding="utf-8")
try:
    idx = computer.index()
    ok("zz_test_tool_tmp" in idx and idx["zz_test_tool_tmp"]["scan"] == "B" and "count clips" in idx["zz_test_tool_tmp"]["triggers"], "a saved tool is indexed from its header")
    blk = computer.tools_block("how many clips are on the card")
    ok("* count" in blk, "a matching tool is starred for the task")
    ok(tier("python assistant/doer/tools/zz_test_tool_tmp.py E:/x") == "B", "a saved tool runs at tier B")
finally:
    tp.unlink()
    computer.index()
ok(set(computer.index()) == set(idx_before), "the index is rebuilt without the test tool")

# ---------------------------------------------------------------- 7b. milestone notes + carry on (2026-10-28), the computer lane
ok(tier('python assistant/doer/report.py note --ticket ' + t0["id"] + ' "Deleted the 412 cache files; moving on to the proxies"') == "A",
   "a milestone note is tier A whatever its words say (deleted, moving)")
ok(tier('python C:/elsewhere/report.py note --ticket x "rm -rf D:/x"') != "A", "a report.py from elsewhere is not the note")
p2 = computer.build_prompt(t0)
ok(f"report.py note --ticket {t0['id']}" in p2 and "## Milestone notes" in p2 and "carries on from the LAST one" in p2, "the computer prompt carries the note recipe")
import notes  # noqa: E402
notes.OUT = computer.OUT = doer.OUT = TMP / "out"
(TMP / "out" / t0["id"]).mkdir(parents=True, exist_ok=True)
NOTED = []
notes.post_line = lambda tk, text: NOTED.append(text) or "spaces/X/messages/n"
ok(notes.record(t0["id"], "Drives listed: C 120 GB free, D 2.1 TB free. Counting the cache files next.").startswith("NOTE=") and NOTED, "a note on a computer ticket lands and goes to the poster")
CALLS = []
CUT = {"ok": False, "text": "", "session": "c-cut", "error": "x", "marker": "", "arg": "", "raw": '"subtype": "error_max_turns"'}
def turns(seq):
    def _t(prompt, out_dir, resume="", timeout=0, env_extra=None):
        CALLS.append({"prompt": prompt, "resume": resume, "env": env_extra or {}})
        return dict(seq[min(len(CALLS), len(seq)) - 1])
    return _t
computer.run_turn = turns([CUT, {"ok": True, "text": "", "session": "c-cut", "error": "", "marker": "DOER_ANSWER", "arg": "C has 120 GB free, D has 2.1 TB."}])
DELIV = []
doer.deliver = lambda tk, text: DELIV.append(text)
doer._learn = lambda: type("L", (), {"after_run": staticmethod(lambda *a, **k: "")})()
doer.browser.open_browser = lambda show=False, wait_s=0: "http://127.0.0.1:1"
doer.browser.close_browser = lambda: True
tickets._transition(t0["id"], "claimed", "test", "", None)
computer.run_ticket(tickets._load()[t0["id"]])
ok(len(CALLS) == 2 and CALLS[1]["resume"] == "c-cut" and CALLS[1]["prompt"].startswith("## CARRY ON") and "Drives listed" in CALLS[1]["prompt"]
   and "files, folders and tools are as you left them" in CALLS[1]["prompt"] and CALLS[1]["env"].get("DOER_COMPUTER_TICKET") == t0["id"],
   "a computer run cut off by max_turns is carried on in its session with the notes, on the computer lane's own turn")
ok(tickets._load()[t0["id"]]["status"] == "done" and DELIV and "120 GB" in DELIV[-1] and NOTED[-1].startswith("Still on it"), "the second try finishes; the owner saw one 'still on it' line")
CALLS.clear(); DELIV.clear()
computer.run_turn = turns([CUT])
tickets._transition(t0["id"], "claimed", "test", "", None)
computer.run_ticket(tickets._load()[t0["id"]])
ok(len(CALLS) == 1 + int(doer.CFG.get("continues_max", 3)) and tickets._load()[t0["id"]]["status"] == "staged"
   and "times over" in DELIV[-1] and "It got as far as: Drives listed" in DELIV[-1], f"out of tries: staged, and the owner hears how far it got ({DELIV[-1][:80]})")

# ---------------------------------------------------------------- 8. gate shapes
src = (ROOT / "doer" / "computer.py").read_text(encoding="utf-8")
ok('ALLOWED = ",".join([' in src and 'DENIED = ",".join([' in src, "computer.py carries the two gate shapes")
ok("--dangerously-skip-permissions" not in src and '"--settings"' in src and '"--strict-mcp-config"' in src, "no skip-permissions; the settings hook and strict MCP are passed")
ok("PowerShell" in computer.DENIED.split(","), "PowerShell is denied")

print(f"\nALL {N} CHECKS PASSED")

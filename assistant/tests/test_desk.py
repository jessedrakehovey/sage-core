"""Offline tests for desk.py + desk_policy.py (the owner, 2026-10-30: "example acts. Sage overpromises and under
delivers"). A WORK ask typed in chat goes to the full desk (a detached Claude Code turn with write tools and a
code-enforced belt), questions and standing instructions stay with the chat brain, the reply is checked against the
record, and the brain's context carries what the desk did. Scratch stores; no model, no Google, no child process.
   python assistant/tests/test_desk.py
"""
import os as _os, tempfile as _tf
_os.environ.setdefault("WV_LOG_DIR", _tf.mkdtemp(prefix="wv_logs_"))
import json
import os
import re
import sys
import tempfile
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
VAULT = ROOT.parent
TMP = Path(tempfile.mkdtemp(prefix="wv_desk_"))
os.environ["ASSISTANT_DATA"] = str(TMP / "adata")
os.environ["ASSISTANT_CARDS"] = str(TMP / "cards")
os.environ["ASSISTANT_LEARNINGS"] = str(TMP / "learnings")
os.environ["RULINGS_FILE"] = str(ROOT / "data" / "rulings.json")
os.environ["DOER_DATA"] = str(TMP / "doer")
os.environ["AGENT_STATE"] = str(TMP / "agent_state")
os.environ["AGENT_DATA"] = str(TMP / "agent_data")
os.environ["AGENT_SUGGESTIONS"] = str(TMP / "sugg.md")
os.environ["CHAT_PUSH"] = "off"
os.environ["CRM_SNAPSHOT_FILE"] = str(TMP / "no_snapshot.json")
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT / "agent"))
sys.path.insert(0, str(VAULT / "scripts" / "guardrails"))
import core  # noqa: E402
import desk  # noqa: E402
import desk_policy as dp  # noqa: E402
import brain  # noqa: E402
import bridge  # noqa: E402
import check_gates  # noqa: E402

N = 0


def ok(cond, msg):
    global N
    N += 1
    if not cond:
        print(f"FAIL {N}: {msg}")
        sys.exit(1)
    print(f"ok {N}: {msg}")


OWNER = core.person_for(handle=next(iter(core.owners()), ""))
assert OWNER, "test needs an owner from people.json"
NAME = core.profile().get("name", "Agent")
STAFF = {"key": "visitor", "name": "A Visitor", "role": "staff"}

# ---------------------------------------------------------------- 1. what is work (the owner's real lines, Oct 24-30)
for line, want in (
    ("do it do not fail", "desk"), ("Fix the safety hook", "desk"), ("update pls", "desk"), ("Okay do it", "desk"),
    ("Fix it and send", "desk"), ("Well do it", "desk"), ("Try again", "desk"),
    ("follow up with them about the form (if we sent it. IF not send it right away)", "desk"),
    ("upload to drop box so I can print them", "desk"), ("Add the new admin to the team", "desk"),
    ("I did the payment already. Text him back a reasonable amount for drone operation", "desk"),
    ("respond to him again", "desk"), ("open the rental cart for the 11/7 shoot but leave out the drone for now", "desk"),
    (f"{NAME} make a list of top 5 things the business needs to be more successful today", "desk"),
    ("text a dropbox link of the two hour loop for their event", "desk"),
    ("ok. Promptly purge all bank related log-in info you have unless it is through something like plaid", "desk"),
    ("Is there any way to fix this? You should be able to fix this I programmed you to problem solve", "desk"),
    ("I printed them. Need to send now", "desk"), ("Make the freaking link already", "desk"),
    ("can you check if he paid", "desk"),
    # the chat brain keeps these
    ("Results??", "brain"), ("Have you uploaded their loop to dropbox and texted them yet?", "brain"),
    ("everytime you build a lens rental card add it as a saved cart", "brain"), ("He wants payment before the event. What is fair?", "brain"),
    ("What's on my calendar tomorrow?", "brain"), ("did he reply?", "brain"), ("remember that the new admin is staff", "brain"),
    ("remind me every Friday to check the ads", "brain"), ("from now on keep texts under two lines", "brain"),
    ("https://www.dropbox.com/scl/fi/x", "brain"), ("I don't see it", "brain"), ("[tapped Send]", "brain"), ("/promises", "brain"),
    ("Stop sending me messages like this", "brain"), ("ok thanks", "brain"), ("", "brain"),
):
    k, why = desk.classify(line)
    ok(k == want, f"classify {line[:50]!r} -> {k} ({why[:50]})")

# ---------------------------------------------------------------- 2. wants(): who, where, and the switch
msg = {"channel": "gchat", "direct": True, "why": "DM", "space": "spaces/X"}
ok(desk.wants("Fix the safety hook", msg, OWNER), "an owner's work line in a DM goes to the desk")
ok(not desk.wants("Fix the safety hook", msg, STAFF), "a staff member's line never reaches the desk")
ok(not desk.wants("Fix the safety hook", msg, OWNER, tcard={"id": "q1"}), "a card's thread stays with the brain (approvals)")
ok(not desk.wants("Fix the safety hook", msg, OWNER, scope="business"), "a team space stays with the brain")
ok(not desk.wants("Fix the safety hook", {**msg, "direct": False}, OWNER), "an ambient line never reaches the desk")
ok(not desk.wants("Fix the safety hook", {**msg, "why": "follow-up (weak)"}, OWNER), "a weak direct never reaches the desk")
ok(not desk.wants("Fix the safety hook", {**msg, "channel": "card"}, OWNER), "only chat channels reach the desk")
ok(desk.wants("where is the card", msg, OWNER, correction_kind="card"), "a correction ('where is the card') is the ask again: the desk")
ok(not desk.wants("where is the card", msg, OWNER, correction_kind="card", card_here=True), "...unless the card is already in this thread")
ok(not desk.wants("did he reply?", msg, OWNER), "a question stays with the brain")
os.environ["AGENT_DESK"] = "off"
ok(not desk.wants("Fix the safety hook", msg, OWNER), "AGENT_DESK=off sends everything to the brain")
del os.environ["AGENT_DESK"]
ok(desk.cfg()["enabled"] and desk.cfg()["model"] and int(desk.cfg()["timeout_s"]) >= 600, "profile desk settings load with defaults")

# ---------------------------------------------------------------- 3. the belt: what the hook refuses
for cmd in ("python assistant/scripts/email_send.py --ticket tk-1", "python assistant/scripts/cards.py answer q1 --option Send",
            "python assistant/scripts/cards.py reopen q1 --why x", "git push origin master", "git add -A", "git add .", "git commit -am x",
            "git stash", "git reset --hard HEAD~1", "claude -p 'x'", "echo x >> assistant/data/phone_outbox.json",
            "python -c \"open('assistant/data/exec_tickets.jsonl','a').write('x')\"", "python assistant/scripts/crm_send_message.py --x",
            "python scripts/crm/crm_exec.py --ticket x",
            "python assistant/scripts/drain.py", "python assistant/scripts/exec_store.py mint", "schtasks /run /tn x",
            "python -c \"import playwright\"", "Set-Content assistant/data/cards/live/q1.json x", "python assistant/doer/vault.py list",
            "python assistant/doer/login_fill.py", "setx FOO bar", "python scripts/guardrails/check_gates.py --freeze",
            "python assistant/scripts/client_texts.py --send", "cat assistant/agent/google.json", ""):
    allowed, why = dp.command_decision(cmd)
    ok(not allowed, f"refused: {cmd[:60]!r} ({why[:40]})")
for cmd in ("python assistant/scripts/cards.py post --file x.json --owner desk:d1 --asked", "python assistant/scripts/cards.py pending",
            "python -m py_compile assistant/agent/foo.py", "git add assistant/agent/foo.py && git commit -m 'x' -- assistant/agent/foo.py",
            "cat assistant/data/exec_tickets.jsonl", "python assistant/agent/desk.py note d1 'the card is up'", "python assistant/scripts/tickets.py list",
            "python assistant/doer/tools/open_cart.py --apply", "python assistant/tests/test_desk.py", "git status", "git diff",
            "grep -n foo assistant/agent/actions.py", "python assistant/agent/builder.py status", "python assistant/doer/doer.py --tick"):
    allowed, why = dp.command_decision(cmd)
    ok(allowed, f"allowed: {cmd[:60]!r} ({why[:40]})")
for p in (".claude/settings.json", "assistant/scripts/exec_store.py", "assistant/scripts/cards.py", "assistant/data/phone_outbox.json",
          "Memory/memory.md", "Raw/x.md", "assistant/agent/desk_policy.json", "assistant/agent/desk.py", "assistant/agent/desk_prompt.md",
          "scripts/guardrails/gate_baseline.json", "assistant/agent/people.json", "assistant/doer/vault.py", "assistant/agent/google.json",
          str(Path.home() / "x.txt")):
    allowed, why = dp.path_decision(p)
    ok(not allowed, f"never written: {p} ({why[:40]})")
for p in ("assistant/agent/actions.py", "assistant/agent/brain.py", "Wiki/Personal/x.md", "assistant/scripts/tickets.py", "assistant/tests/test_x.py",
          str(VAULT / "assistant" / "agent" / "core.py"), "C:/Users/someone/AppData/Local/Temp/x.txt"):
    allowed, why = dp.path_decision(p)
    ok(allowed, f"writable: {p} ({why[:40]})")
ok(dp.decide({"tool_name": "Bash", "tool_input": {"command": "git push"}})[0] is False, "decide(): Bash goes through command_decision")
ok(dp.decide({"tool_name": "Edit", "tool_input": {"file_path": "assistant/scripts/drain.py"}})[0] is False, "decide(): Edit goes through path_decision")
ok(dp.decide({"tool_name": "Write", "tool_input": {}})[0] is False, "decide(): a write with no path is refused (fail closed)")
ok(dp.decide({"tool_name": "Read", "tool_input": {"file_path": "assistant/scripts/drain.py"}})[0] is True, "decide(): reads pass")
ok("assistant/scripts/email_send.py" in dp.owner_paths() and ".claude/" in dp.owner_paths(), "the owner's paths come from builder.py")

# ---------------------------------------------------------------- 4. the command and the frozen belt
job = desk.start({"channel": "gchat"}, OWNER, "Fix the safety hook", {"convo": f"dm:{OWNER['key']}", "thread": "spaces/X/threads/T", "space": "spaces/X", "channel": "gchat"}, spawn=False)
ok(job["status"] == "running" and job["id"].startswith("d") and desk.load_job(job["id"])["text"] == "Fix the safety hook", "start(spawn=False) records the job")
turn = TMP / "turn.json"
turn.write_text("{}", encoding="utf-8")
cmd = desk.command(job, turn, TMP / "job")
ok("--dangerously-skip-permissions" in cmd and "--settings" in cmd and "--strict-mcp-config" in cmd and "--allowedTools" not in cmd,
   "the desk is a full turn: skip-permissions, a settings hook, strict MCP, no allow-list")
settings = json.loads(Path(cmd[cmd.index("--settings") + 1]).read_text(encoding="utf-8"))
hooks = settings["hooks"]["PreToolUse"]
ok(any("Bash" in h["matcher"] for h in hooks) and any("Edit" in h["matcher"] for h in hooks) and all("desk_policy.py" in h["hooks"][0]["command"] for h in hooks),
   "desk_policy.py is the PreToolUse hook on Bash and on Write/Edit")
belt = set(cmd[cmd.index("--disallowedTools") + 1].split(","))
ok(set(check_gates.CRITICAL_NEVER_ALLOW) <= belt, "every tool on the gate's never-allow list is in the desk's belt")
ok({"Task", "Agent", "mcp__gmail__send_email"} <= belt, "no nested agents, no mail send")
mcp = json.loads(Path(cmd[cmd.index("--mcp-config") + 1]).read_text(encoding="utf-8"))
ok(brain.MCP_SERVER in mcp["mcpServers"] and mcp["mcpServers"][brain.MCP_SERVER]["env"]["AGENT_TURN"] == str(turn), "the private deed server rides along, bound to this turn")
frozen = check_gates._canonical_deny((ROOT / "agent" / "desk.py").read_text(encoding="utf-8"), "py")
ok("mcp__gmail__send_email" in frozen and "mcp__playwright__browser_click" in frozen, "check_gates parses the desk's DENIED list")
ok("agent-desk" in check_gates.TARGETS and "agent-desk" in check_gates.SKIP_PERMS_EXEMPT, "the desk is a gate target, exempt from the skip-permissions check like dream_cron")

# ---------------------------------------------------------------- 5. the prompt: filled, and org-free
prompt = desk.render(job, OWNER)
left = re.findall(r"\{(?:name|household|person|vault|ask|correction|where|thread|running|job|thread_name|space|tap_paths|rules)\}", prompt)
ok(not left, f"every placeholder is filled ({left})")
ok("Fix the safety hook" in prompt and job["id"] in prompt and "spaces/X/threads/T" in prompt, "the ask, the job id and the thread are in the prompt")
ok("assistant/scripts/email_send.py" in prompt, "the owner's paths are named in the prompt")
org_words = {str(core.profile().get("name") or "")} | {str(p.get("name") or "").split()[0] for p in core.people().values()
                                                           if isinstance(p, dict) and p.get("name")}
org_words = {w for w in org_words if len(w) > 2}
for f in ("desk.py", "desk_policy.py", "desk_policy.json", "desk_prompt.md"):
    txt = (ROOT / "agent" / f).read_text(encoding="utf-8")
    hits = [w for w in org_words if re.search(r"\b" + re.escape(w) + r"\b", txt)]
    ok(not hits, f"{f} names no person or agent of this organization ({hits})")

# ---------------------------------------------------------------- 6. run(): the words checked against the record
calls = []


def fake_run(prompt, job, turn, reply="Done. The card is below for your tap.", okk=True, error=""):
    calls.append(prompt)
    return {"ok": okk, "text": reply, "error": error, "account": "test"}


desk._run_claude = lambda p, j, t: fake_run(p, j, t)
done = desk.run(job["id"])
ok(done["status"] == "done" and "Correction: no card has actually gone up" in done["reply"], "'the card is below' with no card posted gets the truth appended")
rows = [r for r in core.rows(core.TRANSCRIPT) if r.get("desk") == job["id"]]
ok(rows and rows[-1]["who"] == NAME and rows[-1]["channel"] == "desk" and rows[-1]["thread"] == "spaces/X/threads/T", "the reply is on the transcript, in the thread")
out = [r for r in core.rows(core.DATA / "outbox.jsonl") if r.get("desk") == job["id"]]
ok(out and out[-1]["to"] == OWNER["key"], "the reply is in the outbox for the owner")
ok(len(calls) == 1 and "Fix the safety hook" in calls[0], "one model turn, with the ask")
blk = desk.block(f"dm:{OWNER['key']}")
ok("DESK WORK" in blk and "done" in blk and "Fix the safety hook" in blk, "the brain's context carries what the desk did")

job2 = desk.start({"channel": "gchat"}, OWNER, "upload to drop box so I can print them", {"convo": f"dm:{OWNER['key']}", "thread": "", "space": "spaces/X", "channel": "gchat"}, spawn=False)
desk._run_claude = lambda p, j, t: fake_run(p, j, t, reply="", okk=False, error="timeout")
d2 = desk.run(job2["id"])
ok(d2["status"] == "failed" and d2["reply"].startswith("That one ran long"), "a timed-out desk says so once, plainly")
job3 = desk.start({"channel": "gchat"}, OWNER, "Add the new admin to the team", {"convo": f"dm:{OWNER['key']}", "thread": "", "space": "spaces/X", "channel": "gchat"}, spawn=False)
desk._run_claude = lambda p, j, t: fake_run(p, j, t, reply="I put the change on a card for you; it's posted above.", okk=True)
d3 = desk.run(job3["id"])
ok("Correction: no card" in d3["reply"], "'posted' with no card is corrected too")
desk._run_claude = lambda p, j, t: fake_run(p, j, t, reply="He is on the team now. Nothing needed from you.", okk=True)
job4 = desk.start({"channel": "gchat"}, OWNER, "Add the new admin to the team", {"convo": f"dm:{OWNER['key']}", "thread": "", "space": "spaces/X", "channel": "gchat"}, spawn=False)
d4 = desk.run(job4["id"])
ok(d4["reply"] == "He is on the team now. Nothing needed from you.", "a reply with no card claim is left as it is")
blk = desk.block(f"dm:{OWNER['key']}")
ok("failed" in blk and "running" not in blk, "the block shows the failed one and nothing running")

# ---------------------------------------------------------------- 7. notes: milestones yes, chatter no
job5 = desk.start({"channel": "gchat"}, OWNER, "text a dropbox link", {"convo": f"dm:{OWNER['key']}", "thread": "spaces/X/threads/T5", "space": "spaces/X", "channel": "gchat"}, spawn=False)
r = desk.note(job5["id"], "Still working on it")
ok(not r["ok"] and "chatter" in r["reason"], "'Still working' is never posted")
r = desk.note(job5["id"], "That didn't work, trying another way")
ok(not r["ok"], "'That didn't work' is never posted")
r = desk.note(job5["id"], "The Dropbox link is ready and the text to him is on the card below.")
ok(r["ok"], "a milestone line is posted")
ok(not desk.note("d-nope", "x")["ok"], "a note for an unknown job is refused")
blk = desk.block(f"dm:{OWNER['key']}")
ok("running since" in blk and "Dropbox link is ready" in blk, "a running job shows its milestone in the brain's context")

# ---------------------------------------------------------------- 8. the bridge: work goes to the desk, a question to the brain
started = []
real_start = desk.start
desk.start = lambda msg, person, text, ctx, spawn=True: (started.append((text, ctx)), real_start(msg, person, text, ctx, spawn=False))[1]
brain_calls = []


def fake_brain(prompt, resume="", model="", timeout=600, tools=None):
    brain_calls.append(prompt)
    return {"ok": True, "text": "Nothing on the calendar tomorrow.", "session": "s1"}


brain._run_claude = fake_brain
bridge._gchat = lambda: None
convo = f"dm:{OWNER['key']}"
bridge.handle({"channel": "local", "convo": convo, "person": OWNER, "text": "Fix the safety hook", "direct": True, "why": "DM", "space": "", "thread": ""})
ok(len(started) == 1 and started[0][0] == "Fix the safety hook" and not brain_calls, "a work line in the bridge goes to the desk, not the brain")
tail = [r for r in core.rows(core.TRANSCRIPT) if r.get("convo") == convo][-2:]
ok(tail[-1]["who"] == NAME and tail[-1]["text"] == desk.RECEIPT and tail[-1].get("desk"), "the owner hears the one receipt, tied to the job")
bridge.handle({"channel": "local", "convo": convo, "person": OWNER, "text": "What's on my calendar tomorrow?", "direct": True, "why": "DM", "space": "", "thread": ""})
ok(len(started) == 1 and len(brain_calls) == 1 and "DESK WORK" in brain_calls[0], "a question goes to the brain, whose context lists the desk's work")
os.environ["AGENT_DESK"] = "off"
bridge.handle({"channel": "local", "convo": convo, "person": OWNER, "text": "Fix the other hook", "direct": True, "why": "DM", "space": "", "thread": ""})
ok(len(started) == 1 and len(brain_calls) == 2, "with the desk off, the brain takes work as before")
del os.environ["AGENT_DESK"]

print(f"\nALL {N} CHECKS PASSED")

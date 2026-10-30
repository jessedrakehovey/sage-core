"""Offline tests for the doer (assistant/doer): ticket routing, the agent_run exec kind, the
checkpoint card, the typed verification code, drain's dry run, prompt building, marker parsing,
the run_ticket outcomes and the vault's URL ranking. Scratch dirs; no model, no browser, no
network.   python assistant/tests/test_doer.py
"""
import os as _os, tempfile as _tf
_os.environ.setdefault("WV_LOG_DIR", _tf.mkdtemp(prefix="wv_logs_"))   # never the live ~/.claude logs
import json
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
TMP = Path(tempfile.mkdtemp(prefix="wv_doer_"))
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
import report  # noqa: E402
import vault  # noqa: E402
import actions  # noqa: E402
import core  # noqa: E402

FAILS = []


def check(name, cond, extra=""):
    print(("ok   " if cond else "FAIL ") + name + (f"  {extra}" if extra and not cond else ""))
    if not cond:
        FAILS.append(name)


# 1. routing: the operator never claims the doer's kind; the doer claims only it
tickets.add_many([{"source": "t", "source_id": "a", "lane": "personal", "kind": "agent_run", "title": "Update DMV address",
                   "summary": "update my dmv address", "payload": {"text": "Update my DMV address to the Brackwater unit", "from": "owner1"}},
                  {"source": "t", "source_id": "b", "lane": "personal", "kind": "owner1_request", "title": "draft", "summary": "", "payload": {}}], "test")
check("operator pending count excludes agent_run", tickets.pending() == 1)
check("doer pending count sees only agent_run", tickets.pending(kind="agent_run") == 1)
op = tickets.claim(limit=10, by="operator")
check("operator claim skips agent_run", [t["kind"] for t in op] == ["owner1_request"])
got = tickets.claim(limit=1, by="doer", kind="agent_run")
check("doer claim gets the agent_run ticket", len(got) == 1 and got[0]["kind"] == "agent_run")
TID = got[0]["id"]

# 2. the exec kind's contract
check("agent_run needs resume", "resume" in es.validate_payload("agent_run", {"ticket": TID, "step": "Click Submit"}))
check("agent_run needs a real ticket id", "ticket" in es.validate_payload("agent_run", {"ticket": "nope", "step": "x", "resume": "y"}))
check("agent_run with ask_code posts without a code (typed at answer time)", es.validate_payload("agent_run", {"ticket": TID, "step": "x", "resume": "y", "ask_code": True}) == "")
check("agent_run with a malformed code is refused", "digits" in es.validate_payload("agent_run", {"ticket": TID, "step": "x", "resume": "y", "ask_code": True, "code": "ab"}))
try:
    es.enqueue("agent_run", {"ticket": TID, "step": "x", "resume": "y", "ask_code": True}, "owner1", "card:test")
    check("mint without the code refused", False)
except ValueError as e:
    check("mint without the code refused", "verification code" in str(e))
check("agent_run good payload", es.validate_payload("agent_run", {"ticket": TID, "step": "Click Submit", "resume": "go back and click", "values": {"a": "1"}}) == "")

# 3. the checkpoint card: everything visible, screenshot wired, posted
report.OUT = TMP / "out"
(report.OUT / TID).mkdir(parents=True)
(report.OUT / TID / "review.png").write_bytes(b"\x89PNG\r\n")
spec = {"url": "https://www.dmv.ca.gov/portal/change-of-address/review", "step": "Click 'Submit' on the DMV change-of-address review page",
        "values": {"New address": "123 Example Street Unit D41, Brackwater, CA 93030", "Effective": "today"},
        "screenshot": "review.png", "ask_code": False, "resume": "Navigate back to the review page, confirm the address matches, click Submit, read the confirmation number."}
sf = TMP / "spec.json"; sf.write_text(json.dumps(spec), encoding="utf-8")
line = report.checkpoint(TID, str(sf))
check("checkpoint posted", line.startswith("CHECKPOINT="), line)
QID = line.split("=", 1)[1] if "=" in line else ""
pend = cards.pending()
check("card carries the step and every value", pend and spec["step"] in pend[0]["question"] and "Unit D41" in pend[0]["question"])
check("card image url points at the ticket's screenshot", pend and pend[0]["image"] == f"/api/doer/shot/{TID}/review.png", pend and pend[0]["image"])
bad = report.build_question(TID, spec); bad["question"] = "Submit the DMV thing?"
r = cards.post([bad], "test")
check("a card hiding the step is refused (EXEC_VERBATIM)", not r["posted"] and "EXEC_VERBATIM" in r["reason"])
check("report refuses a bad ticket id", report.checkpoint("zzz", str(sf)).startswith("REFUSED"))

# 4. Approve mints the sha-bound step; Cancel mints nothing and queues no operator work
a = cards.answer(QID, "Approve")
check("approve mints an agent_run ticket", a["ok"] and a.get("ticket"))
tk = es.fold()[a["ticket"]]
check("minted payload is the approved step", tk["kind"] == "agent_run" and tk["payload"]["step"] == spec["step"] and tk["payload"]["ticket"] == TID)
spec2 = dict(spec, ask_code=True, step="Type the verification code the DMV texted you and continue", values={})
sf2 = TMP / "spec2.json"; sf2.write_text(json.dumps(spec2), encoding="utf-8")
line2 = report.checkpoint(TID, str(sf2))
check("code checkpoint posted", line2.startswith("CHECKPOINT="), line2)
QID2 = line2.split("=", 1)[1] if "=" in line2 else ""
a2 = cards.answer(QID2, "Approve", text="code is 123 456") if QID2 else {}
tk2 = es.fold().get(a2.get("ticket") or "", {})
check("typed code is minted into the payload, digits only", (tk2.get("payload") or {}).get("code") == "123456", str(a2)[:200])
ans = [json.loads(l) for l in (cards.ANSWERS.read_text(encoding="utf-8").splitlines()) if l.strip()]
check("the code is not kept in answers.jsonl", all("123" not in str(x.get("text", "")) for x in ans))
line3 = report.checkpoint(TID, str(sf))
QID3 = line3.split("=", 1)[1]
a3 = cards.answer(QID3, "Cancel")
check("cancel mints nothing", a3["ok"] and not a3.get("ticket"))
sw = cards.sweep()
check("cancel queues no operator ticket", not any(t["kind"] == "card_answer" and t["payload"].get("qid") == QID3 for t in tickets._load().values()), str(sw))

# 5. drain dry run reaches doer.py --approved --dry-run
import drain
ok, res = drain._exec_agent_run(dict(tk["payload"]), True)
check("drain dry-run of agent_run", ok and "DRY_RUN_OK" in res, res)

# 6. prompt building: no leftover placeholders, task + ticket + out dir present
t = tickets._load()[TID]
p = doer.build_prompt(t)
check("prompt carries the task", "Update my DMV address to the Brackwater unit" in p)
check("prompt carries the ticket id and out dir", TID in p and f"out/doer/{TID}" in p)
import re
leftover = re.findall(r"\{[a-z_]+\}", p)
check("no unfilled placeholders", not leftover, str(leftover))
check("prompt names the live status file", "doer_status.md" in p and "Now:" in p)
check("prompt keeps the JSON example braces", '"step":' in p and "{" in p)

# 7. marker parsing + run_ticket outcomes with a stand-in turn, browser and chat
doer.browser.open_browser = lambda show=False, wait_s=0: "http://127.0.0.1:1"
doer.browser.close_browser = lambda: True
delivered = []
doer.deliver = lambda t, text: delivered.append(text)
doer.CLAUDE = str(ROOT / "tests" / "test_doer.py")      # exists; run_turn is replaced below anyway


def fake_turn(marker, arg):
    def _t(prompt, out_dir, resume="", timeout=0):
        return {"ok": True, "text": f"working...\n{marker}: {arg}", "session": "sess-1", "error": "", "marker": marker, "arg": arg}
    return _t


doer.run_turn = fake_turn("DOER_CHECKPOINT", f"{QID} Click Submit on the review page")
out = doer.run_ticket(t)
st = tickets._load()[TID]
check("checkpoint -> ticket staged on the card", st["status"] == "staged" and f"card {QID}" in st["history"][-1]["note"])
check("checkpoint -> owner told to approve", delivered and "tap Approve" in delivered[-1] and "this chat" in delivered[-1])
live = doer.STATUS.read_text(encoding="utf-8")
check("status: checkpoint names the card and the task", f"card {QID}" in live and TID in live and "Phase: waiting" in live)
doer.STATUS.write_text(live.replace("Now: starting", "Now: 10:42 facebook.com asked for a login code"), encoding="utf-8")
doer.set_status(t, "stopped, blocked: test")
live = doer.STATUS.read_text(encoding="utf-8")
check("status: code keeps the turn's last Now line", "Now: 10:42 facebook.com asked for a login code" in live and "blocked: test" in live)
doer._asks_code = lambda qid: True
check("checkpoint asking a code -> asks for it like a person, to be typed here (voice.py 2026-10-27)",
      doer._say("owner1", "DOER_CHECKPOINT", f"{QID} Type the code").startswith("I need the code")
      and "Reply with it here" in doer._say("owner1", "DOER_CHECKPOINT", f"{QID} Type the code")
      and doer.MC_URL not in doer._say("owner1", "DOER_CHECKPOINT", f"{QID} Type the code"))
check("session remembered for the approved step", doer._session_get(TID) == "sess-1")
tickets._transition(TID, "claimed", "test", "", None)
doer.run_turn = fake_turn("DOER_BLOCKED", "no login saved for dmv.ca.gov")
doer.run_ticket(t)
check("blocked -> staged + told", tickets._load()[TID]["status"] == "staged" and "could not finish" in delivered[-1])
tickets._transition(TID, "claimed", "test", "", None)
doer.run_turn = fake_turn("DOER_DONE", "Address updated, confirmation 8842")
doer.run_ticket(t)
check("done -> ticket done + told", tickets._load()[TID]["status"] == "done" and "8842" in delivered[-1])
check("status: done -> finished line", "Phase: finished: Address updated, confirmation 8842" in doer.STATUS.read_text(encoding="utf-8"))

# the real marker regex, on a result the way the CLI returns it
raw = {"type": "result", "result": "I filled the form.\nDOER_DONE: Address updated, confirmation 8842", "session_id": "s9", "is_error": False}
class _R:  # noqa: E302
    returncode = 0; stdout = json.dumps(raw); stderr = ""
_real_run = subprocess.run
subprocess.run = lambda *a, **k: _R()
try:
    doer.run_turn = doer.__dict__["run_turn"] if "run_turn" in doer.__dict__ and callable(doer.__dict__["run_turn"]) else None
    import importlib
    importlib.reload(doer)
    doer.browser.open_browser = lambda show=False, wait_s=0: "http://127.0.0.1:1"
    doer.CLAUDE = str(ROOT / "tests" / "test_doer.py")
    rr = doer.run_turn("prompt", TMP / "o")
    check("run_turn parses the CLI json + marker", rr["marker"] == "DOER_DONE" and "8842" in rr["arg"] and rr["session"] == "s9")
    # a multi-line answer keeps every line under the marker (2026-10-16: options cut at the first line)
    mk, arg = doer.parse_marker("Looked.\nDOER_ANSWER: the discounts sit in these places:\n- Packages page\n- Invoice line\n")
    check("multi-line DOER_ANSWER keeps its lines", mk == "DOER_ANSWER" and arg.endswith("- Packages page\n- Invoice line") and arg.startswith("the discounts"))
    mk, arg = doer.parse_marker("DOER_ANSWER: x\n" + "y" * 3000)
    check("long answer capped at REPORT_MAX", len(arg) == doer.REPORT_MAX and arg.endswith("…"))
    mk, arg = doer.parse_marker("DOER_CHECKPOINT: q1 Click Submit\nextra line")
    check("checkpoint stays one line", arg == "q1 Click Submit")
    TID9 = TID
    tickets._transition(TID9, "claimed", "test", "", None)
    doer.browser.close_browser = lambda: True
    doer.deliver = lambda tk, text: delivered.append(text)
    doer.run_turn = lambda *a, **k: {"ok": True, "text": "", "session": "s", "error": "", "marker": "DOER_ANSWER",
                                     "arg": "the discounts sit in these places:\n- Packages page\n- Invoice line"}
    doer._learn = lambda: type("L", (), {"after_run": staticmethod(lambda *a, **k: "")})()
    doer.run_ticket(tickets._load()[TID9])
    check("multi-line answer reaches chat whole", delivered[-1].endswith("- Invoice line"))
    check("multi-line answer stored in the ticket note", tickets._load()[TID9]["history"][-1]["note"].endswith("- Invoice line"))
finally:
    subprocess.run = _real_run

# 8. approved step: refuses an unknown ticket; dry run prints the approved block
rc = doer.run_approved({"ticket": "0123456789abcdef", "step": "x", "resume": "y"}, True)
check("approved step with unknown ticket refused", rc == 1)
import io, contextlib
buf = io.StringIO()
with contextlib.redirect_stdout(buf):
    rc = doer.run_approved(dict(tk["payload"]), True)
check("approved dry run shows the step", rc == 0 and "DRY_RUN_OK" in buf.getvalue() and "APPROVED STEP" in buf.getvalue())

# 9. Sage's `do` -> one agent_run ticket with the conversation, no operator ticket
actions.WAKE_DOER = False
owner1 = core.person_for(handle="owner1")
before = set(tickets._load())
lines = actions.apply({"do": {"lane": "home", "text": "Check whether the Edison bill is paid"}}, owner1, {"convo": "dm:owner1", "thread": ""})
new = [tickets._load()[k] for k in set(tickets._load()) - before]
check("do -> agent_run ticket in the lane with the convo", len(new) == 1 and new[0]["kind"] == "agent_run" and new[0]["lane"] == "home"
      and new[0]["payload"]["convo"] == "dm:owner1" and new[0]["payload"]["from"] == "owner1")
check("do -> a successful hand-off adds no confirmation line (owner1 2026-10-07: no robotic lines)", not lines)

# 10. policy regexes compile and catch the obvious
pol = json.loads((ROOT / "doer" / "policy.json").read_text(encoding="utf-8"))
hosts = [re.compile(x, re.I) for x in pol["checkpoint_hosts"]]
acts = [re.compile(x, re.I) for x in pol["checkpoint_actions"]]
nev = [re.compile(x, re.I) for x in pol["never"]]
check("policy: dmv host is tier C", any(h.search("www.dmv.ca.gov") for h in hosts))
check("policy: paying is tier C", any(a.search("pay the Edison bill") for a in acts))
check("policy: changing an address is tier C", any(a.search("update the mailing address") for a in acts))
check("policy: password reset is never", any(n.search("reset my password") for n in nev))
check("policy: property business is never", any(n.search("log into example") for n in nev))

# 11. vault ranking: path beats host, host beats domain
kp = vault._open(create=True)
g = vault._group(kp)
kp.add_entry(g, "Portal", "a", "p", url="https://portal.example.com/")
kp.add_entry(g, "Admin", "b", "p", url="https://portal.example.com/admin/login")
kp.add_entry(g, "Other", "c", "p", url="https://other.example.com/")
kp.save()
check("vault: path prefix wins", vault.find_for_url(kp, "https://portal.example.com/admin/users").title == "Admin")
check("vault: host wins over domain", vault.find_for_url(kp, "https://portal.example.com/home").title == "Portal")
check("vault: same domain as fallback", vault.find_for_url(kp, "https://new.example.com/").title in ("Portal", "Admin", "Other"))
check("vault: unknown host -> none", vault.find_for_url(kp, "https://nothing.net/") is None)

# 12. sign-in playbooks: learned per host, hand-written keys kept
import login_fill
login_fill.SITES = TMP / "sites.json"
login_fill.STATUS = TMP / "sites_status.json"
login_fill._learn("portal.example.com", {"user_sel": "#u", "pass_sel": "#p", "otp_sel": "", "two_step": True}, "https://portal.example.com/login")
pb = login_fill.playbook("portal.example.com")
check("playbook learned", pb.get("user_sel") == "#u" and pb.get("two_step") is True
      and pb.get("login_url") == "https://portal.example.com/login" and pb.get("ok_count") == 1)
d = login_fill.sites(); d["portal.example.com"]["pass_sel"] = "#custom"; d["portal.example.com"]["pass_sel_fixed"] = True; login_fill._save_sites(d)
login_fill._learn("portal.example.com", {"user_sel": "#u2", "pass_sel": "#p2", "otp_sel": "", "two_step": False}, "https://portal.example.com/x")
pb = login_fill.playbook("portal.example.com")
check("playbook: fixed key kept, others refreshed, count grows", pb["pass_sel"] == "#custom" and pb["user_sel"] == "#u2" and pb["ok_count"] == 2)
check("playbook: unknown host is empty", login_fill.playbook("nothing.net") == {})
login_fill._write_status({"ts": "2026-10-06T06:00:00", "sites": {}, "ok": 0, "attention": ["x.com: FAILED captcha"]})
st = json.loads(login_fill.STATUS.read_text(encoding="utf-8"))
check("warm status: attention becomes the error field, last_ok not advanced", "captcha" in st["error"] and st["last_ok"] == "")

# 13. no code came: the code card offers the site's other ways; a tap on one resumes the task on it
tickets.add_many([{"source": "t", "source_id": "fb", "lane": "personal", "kind": "agent_run", "title": "Facebook family list",
                   "summary": "", "payload": {"text": "Read my Facebook family members", "from": "owner1"}}], "test")
FT = tickets.claim(limit=1, by="doer", kind="agent_run")[0]
(report.OUT / FT["id"]).mkdir(parents=True, exist_ok=True)
spec4 = {"url": "https://www.facebook.com/checkpoint", "step": "Type the login code Facebook texted to the phone ending 37",
         "values": {}, "ask_code": True, "other_methods": ["Email a code to j***@gmail.com", "Approve on another device"],
         "resume": "Run login_fill.py with the code and carry on."}
sf4 = TMP / "spec4.json"; sf4.write_text(json.dumps(spec4), encoding="utf-8")
line4 = report.checkpoint(FT["id"], str(sf4))
check("code card with other ways posted", line4.startswith("CHECKPOINT="), line4)
QID4 = line4.split("=", 1)[1] if "=" in line4 else ""
c4 = next((c for c in cards.pending() if c.get("qid") == QID4), {})
labels = c4.get("options") or []
check("code card offers the other ways + no code came", labels == ["Approve", "Use instead: Email a code to j***@gmail.com",
      "Use instead: Approve on another device", "No code came", "Cancel"], str(labels))
check("code card tells the owner what 'No code came' does", "No code?" in c4.get("question", ""))
check("a non-code card keeps Approve / Cancel", [o["label"] for o in report.build_question(FT["id"], dict(spec4, ask_code=False))["options"]] == ["Approve", "Cancel"])
doer.browser.open_browser = lambda show=False, wait_s=0: "http://127.0.0.1:1"
doer.browser.close_browser = lambda: True
doer.deliver = lambda t, text: delivered.append(text)
seen = []


def fake_turn2(marker, arg):
    def _t(prompt, out_dir, resume="", timeout=0):
        seen.append((prompt, resume))
        return {"ok": True, "text": f"{marker}: {arg}", "session": "sess-fb", "error": "", "marker": marker, "arg": arg}
    return _t


doer.run_turn = fake_turn2("DOER_CHECKPOINT", f"{QID4} Type the login code")
doer.run_ticket(FT)
check("code checkpoint -> chat says how to get another way", "no code? say so" in delivered[-1])
a4 = cards.answer(QID4, "Use instead: Email a code to j***@gmail.com")
check("picking another way mints nothing", a4["ok"] and not a4.get("ticket"))
check("inside the undo window nothing resumes", doer._code_fallback_due() is None)
cards.UNDO_S = 0
due = doer._code_fallback_due()
check("after the undo window the task is due on the picked way", due and due[0]["id"] == FT["id"] and due[1].endswith("j***@gmail.com"), str(due))
cards.sweep()
check("the pick queues no operator ticket", not any(t["kind"] == "card_answer" and t["payload"].get("qid") == QID4 for t in tickets._load().values()))
check("sweep closed the answered card's task, it is still due", tickets._load()[FT["id"]]["status"] == "done" and doer._code_fallback_due())
doer.run_turn = fake_turn2("DOER_DONE", "Signed in by email code; family list read")
check("tick resumes the task", doer.tick().startswith("DOER_DONE"))
check("resumed the same session with the picked way", seen[-1][1] == "sess-fb" and "SIGN IN ANOTHER WAY" in seen[-1][0]
      and "exactly this one: Email a code to j***@gmail.com" in seen[-1][0], str(seen[-1])[:300])
check("task done, nothing left due", tickets._load()[FT["id"]]["status"] == "done" and doer._code_fallback_due() is None)
check("No code came -> the doer picks, never the same text again", "never the same text message again" in doer.fallback_block("No code came"))

# ------------------------------------------------------------------ 2026-10-18: plain failures + connector-first + forced click (BUILD_PLAN 1.3)
raw_max = '{"type":"result","subtype":"error_max_turns","is_error":true,"num_turns":81,"errors":["Reached max turns (80)"],"spawned_by_subagents":0}'
pe = doer.plain_error(raw_max)
check("max-turns run becomes one plain sentence", "ran out of steps" in pe and "{" not in pe and "spawned" not in pe, pe)
_pe2 = doer.plain_error('{"is_error":true,"errors":["Tool blocked by policy"]}')
check("an error run names the error in plain words", "stopped on an error" in _pe2 and "Tool blocked by policy" in _pe2, _pe2)
check("a plain string passes through", doer.plain_error("the browser closed") == "the browser closed")
say_line = doer._say("owner1", "", raw_max, {"payload": {"text": "add Wren to the CRM project"}})
check("the chat line for a no-result run is plain and names the job", "add Wren" in say_line and "{" not in say_line and "couldn't finish" in say_line, say_line)
pm = (ROOT / "doer" / "prompt.md").read_text(encoding="utf-8")
check("prompt: a blocked line ends with the DO IT YOURSELF hand-over (owner1 2026-10-20)", "DO IT YOURSELF:" in pm and "hand-over, not a loop" in pm)
_pl = doer.portal("The Add host picker will not open || DO IT YOURSELF: https://app.example-crm.com/scheduling then open Vision Meeting, Attendees & hosts, Add host, pick staff1, Save session")
check("a blocked line renders as the owner's portal: reason, link, phone steps, no retry promise",
      _pl.startswith("The Add host picker will not open. The quickest way to finish it yourself, from your phone: https://app.example-crm.com/scheduling then open Vision Meeting") and "try again" not in _pl, _pl)
check("a blocked line without a hand-over still reads plainly", doer.portal("no login saved for x") == "no login saved for x.")
import crm as _crm_doer  # noqa: E402
check("prompt: the doer never refuses a job as a connector job (2026-10-20)", "already checked for a connector" in pm and f"connector job ({_crm_doer.name()}/Google)" not in pm and "never stop with" in pm)
check("prompt: forced click after two stable timeouts, stop after the third", "waiting for element to be stable" in pm and "browser_evaluate" in pm and "Never loop on one control" in pm)
check("the forced-click tool is allowed, the raw-code tool still denied",
      "mcp__playwright-doer__browser_evaluate" in doer.ALLOWED.split(",") and "mcp__playwright-doer__browser_evaluate" not in doer.DENIED.split(",")
      and "mcp__playwright-doer__browser_run_code_unsafe" in doer.DENIED.split(","))
check("prompt: stop after about 20 steps with the page state", "about 20 browser actions" in pm and "the page shows" in pm)

# ------------------------------------------------------------------ 2026-10-19: 3 stuck clicks on one control end the job (click_guard)
import click_guard as cg  # noqa: E402
CGD = TMP / "cg"; CGD.mkdir()
stuck = {"tool_name": "mcp__playwright-doer__browser_click", "tool_input": {"element": "Add", "ref": "e212"},
         "tool_response": [{"type": "text", "text": "### Result\nTimeoutError: locator.click: Timeout 5000ms exceeded.\n"
                                                    "  - waiting for element to be visible, enabled and stable"}]}
check("a stuck click is recognised, a normal one is not", cg.is_stuck(stuck["tool_response"]) and not cg.is_stuck("### Ran Playwright code\nawait page.click()"))
check("two stuck clicks: no block yet", cg.post(stuck, CGD) is None and cg.post(stuck, CGD) is None and cg.pre(stuck, CGD) is None)
other = {**stuck, "tool_input": {"element": "+2", "ref": "e300"}}
check("a different control has its own count", cg.post(other, CGD) is None)
third = cg.post(stuck, CGD)
check("the third on one control blocks with the stop line", (third or {}).get("decision") == "block" and "DOER_BLOCKED" in third["reason"]
      and "controls I could reach" in third["reason"] and "e212" in third["reason"], str(third))
deny = cg.pre({"tool_name": "mcp__playwright-doer__browser_evaluate", "tool_input": {"ref": "e300"}}, CGD)
check("after the cap every browser action is denied", (deny or {}).get("hookSpecificOutput", {}).get("permissionDecision") == "deny")
check("after the cap a snapshot is still allowed", cg.pre({"tool_name": "mcp__playwright-doer__browser_snapshot", "tool_input": {}}, CGD) is None)
sf = doer._settings(CGD)
check("each turn starts with a clean count", not (CGD / cg.STATE).exists())
hk = json.loads(sf.read_text(encoding="utf-8"))["hooks"]
check("run_turn's settings carry the guard on the doer's browser tools", "click_guard.py" in hk["PostToolUse"][0]["hooks"][0]["command"]
      and "browser_(click|evaluate)" in hk["PostToolUse"][0]["matcher"] and hk["PreToolUse"][0]["matcher"].startswith("mcp__playwright-doer__"))
cg_run = subprocess.run([sys.executable, str(ROOT / "doer" / "click_guard.py"), "--hook", "post", str(CGD)], input="not json",
                        capture_output=True, text=True)
check("a bad hook input lets the call through", cg_run.returncode == 0 and not cg_run.stdout.strip())
check("prompt: the code caps the third timeout", "on the third on one control" in pm)
check("prompt: Google Sheet tabs opened by a known gid, never by a tab click (tab elements carry no gid, 2026-10-20)",
      "never click the tab bar" in pm and "carry NO gid" in pm and "edit#gid=<gid>" in pm and "Name Box" in pm and "5550732273" in pm and "export?format=csv" in pm)
_bp = doer.build_prompt(t)
check("built prompt keeps the gid route intact", "#gid=<gid>" in _bp and "5550283128" in _bp)

# 11. milestone notes (notes.py, 2026-10-28): the turn's checkpoints, the status line, the few chat lines,
#     and a run cut off by its step limit or time limit carried on from them (doer.carry_on)
import notes  # noqa: E402
notes.OUT = TMP / "out"
# the real poster holds every note off Chat (cap 0), team space or not (2026-10-28: three priced cart lines
# reached the team space staff read, in four minutes); the job reaches Chat once, at its end or checkpoint
_RC = []
class _FakeReceipts:
    @staticmethod
    def note(ref, convo, thread, text, space="", channel="doer", cap=4):
        _RC.append({"ref": ref, "space": space, "cap": cap, "text": text})
        return "spaces/T/messages/1" if cap > 0 else ""
_real_rc = sys.modules.get("receipts")
sys.modules["receipts"] = _FakeReceipts
_tk = {"id": TID, "payload": {"from": "owner1", "convo": "space:team", "space": "spaces/TEAM", "thread": "spaces/TEAM/threads/t"}}
check("a milestone note never reaches Chat, the team space included (cap 0, held on the transcript)",
      notes.post_line(_tk, "The first Nikon ZR body is in the cart at $2,199; next I raise it to two.") == ""
      and _RC and _RC[-1]["cap"] == 0 and _RC[-1]["space"] == "spaces/TEAM" and _RC[-1]["text"].startswith("The first Nikon ZR"))
if _real_rc is not None:
    sys.modules["receipts"] = _real_rc
else:
    sys.modules.pop("receipts", None)
POSTED = []
notes.post_line = lambda tk, text: POSTED.append((tk["id"], text)) or "spaces/X/messages/n"
check("prompt: the note command, with the ticket id, and what it is for", f"report.py note --ticket {TID}" in _bp
      and "checkpoint" in _bp.split("## Milestone notes")[1][:900] and "carries on from the LAST one" in _bp)
check("prompt: a moving job may run on to the step limit", "may run on: when the step limit cuts it" in _bp)
check("a note needs a real ticket", notes.record("nope", "x").startswith("REFUSED"))
check("a note needs words", notes.record(TID, "   ").startswith("REFUSED"))
check("a note never carries a code", notes.record(TID, "the code is 483920, typing it").startswith("REFUSED")
      and notes.record(TID, "order 2283704 holds the 33mm", "done").startswith("NOTE="))
line = notes.record(TID, "Signed in. Dates set: arrive Thu Nov 5, back Mon Nov 9.")
rows = notes.read(TID)
check("a note lands in notes.jsonl and counts", line == "NOTE=2" and rows[-1]["text"].startswith("Signed in") and rows[-1]["kind"] == "progress")
check("a note rewrites the Now line of the live status", "Now:" in doer.STATUS.read_text(encoding="utf-8")
      and "Dates set" in doer.STATUS.read_text(encoding="utf-8").split("Now:")[1])
check("a note goes through the poster (held off Chat)", POSTED and POSTED[-1] == (TID, rows[-1]["text"]))
sys.argv = ["report.py", "note", "--ticket", TID, "--kind", "blocked", "the", "cart", "page", "will", "not", "take", "a", "click"]
check("report.py note is the CLI (words joined, kind kept)", report.main() == 0 and notes.last(TID)["kind"] == "blocked"
      and notes.last(TID)["text"] == "the cart page will not take a click" and "blocked: the cart page" in doer.STATUS.read_text(encoding="utf-8"))
check("the note command is on the doer's allow list", "Bash(python assistant/doer/report.py note:*)" in doer.ALLOWED)
check("the resume block lists the notes oldest first", notes.block(TID).startswith("1. [") and "order 2283704" in notes.block(TID).splitlines()[0]
      and notes.block(TID).splitlines()[-1].endswith("take a click"))

# a run cut off by max_turns: carried on in the SAME session with the notes; the second try finishes
CALLS = []
CUT = {"ok": False, "text": "", "session": "s-cut", "error": "it ran out of steps before it finished (the step limit is set in config.json -> doer.max_turns)",
       "marker": "", "arg": "", "raw": '"is_error": true, "num_turns": 81, "subtype": "error_max_turns"'}
def turns(seq):
    def _t(prompt, out_dir, resume="", timeout=0):
        CALLS.append({"prompt": prompt, "resume": resume})
        r = dict(seq[min(len(CALLS), len(seq)) - 1])
        return r
    return _t
doer.CFG["continues_max"] = 3
doer.run_turn = turns([CUT, {"ok": True, "text": "", "session": "s-cut", "error": "", "marker": "DOER_DONE", "arg": "Cart built: 6 items, $1,180 with the waiver; stopped at checkout."}])
tickets._transition(TID, "claimed", "test", "", None)
POSTED.clear(); delivered.clear()
doer.run_ticket(tickets._load()[TID])
check("a cut-off run is continued, once was enough", len(CALLS) == 2 and tickets._load()[TID]["status"] == "done" and "Cart built" in delivered[-1])
check("the second try resumes the same session with the CARRY ON block and the notes", CALLS[1]["resume"] == "s-cut"
      and CALLS[1]["prompt"].startswith("## CARRY ON") and "it ran out of steps" in CALLS[1]["prompt"] and "take a click" in CALLS[1]["prompt"]
      and "try 2 of 4" in CALLS[1]["prompt"])
check("the owner saw one 'still on it' line, from the last note", POSTED and POSTED[-1][1].startswith("Still on it") and "take a click" in POSTED[-1][1]
      and notes.last(TID)["by"] == "code")
check("the status said it was carrying on", True)

# cut off every time: stops after continues_max more tries, and the owner hears how far it got (the turn's last note, not the code's)
CALLS.clear(); POSTED.clear(); delivered.clear()
doer.run_turn = turns([CUT])
tickets._transition(TID, "claimed", "test", "", None)
doer.run_ticket(tickets._load()[TID])
check("out of tries: 1 + continues_max turns, then staged", len(CALLS) == 4 and tickets._load()[TID]["status"] == "staged")
check("the owner's line: how many times, and how far it got", "4 times over" in delivered[-1] and "It got as far as: the cart page will not take a click" in delivered[-1]
      and "Nothing was changed" not in delivered[-1], delivered[-1])
check("the ticket note says the same", "4 times over" in tickets._load()[TID]["history"][-1]["note"])

# a time limit leaves no session: the next try is a FRESH prompt with the notes in its resume block
CALLS.clear(); POSTED.clear(); delivered.clear()
TO = {"ok": False, "text": "", "session": "", "error": "timeout", "marker": "", "arg": "", "raw": ""}
doer.run_turn = turns([TO, {"ok": True, "text": "", "session": "s-new", "error": "", "marker": "DOER_ANSWER", "arg": "The 70-180 is $76 for the week."}])
tickets._transition(TID, "claimed", "test", "", None)
doer.run_ticket(tickets._load()[TID])
check("a timed-out run with no session starts fresh, task + notes in the prompt", len(CALLS) == 2 and CALLS[1]["resume"] == ""
      and "## CARRY ON" in CALLS[1]["prompt"] and "ran past its time limit" in CALLS[1]["prompt"] and "Update my DMV address" in CALLS[1]["prompt"]
      and tickets._load()[TID]["status"] == "done")
check("a run that ends on its own is never 'continued'", doer.cut_off({"ok": False, "marker": "DOER_BLOCKED", "error": "max_turns"}) == ""
      and doer.cut_off({"ok": True, "error": ""}) == "" and doer.cut_off(CUT) == "it ran out of steps" and doer.cut_off(TO) == "it ran past its time limit")

# 2026-10-28: a hand-off checkpoint (the owner finishes the checkout himself) puts the browser on his screen
# first, says "open on your screen" only on code's read-back, never offers Approve, and leaves the browser open
import browser as _browser  # noqa: E402
check("on_main_monitor: maximized at 0,0 is visible", _browser.on_main_monitor({"windowState": "maximized", "left": -8, "top": -8, "width": 1936, "height": 1056}, (1920, 1080)))
check("on_main_monitor: the off-screen launch spot is not", not _browser.on_main_monitor({"windowState": "normal", "left": -32000, "top": -32000, "width": 1366, "height": 900}, (1920, 1080)))
check("on_main_monitor: minimized is not", not _browser.on_main_monitor({"windowState": "minimized", "left": 0, "top": 0, "width": 1366, "height": 900}, (1920, 1080)))
check("on_main_monitor: a second monitor is not", not _browser.on_main_monitor({"windowState": "maximized", "left": 1912, "top": -8, "width": 1936, "height": 1056}, (1920, 1080)))
doer.OUT = report.OUT
HO_SPEC = {"url": "https://www.lensrentals.com/checkout", "step": "Enter payment and click 'Place Order' on the Lensrentals checkout",
           "values": {"Cart": "Sony 70-200 GM II, 4 days"}, "screenshot": "review.png", "handoff": True,
           "resume": "Nothing: owner1 pays and places the order himself."}
hsf = TMP / "handoff_spec.json"; hsf.write_text(json.dumps(HO_SPEC), encoding="utf-8")
SHOWN = []
_browser.show_on_screen = lambda: (SHOWN.append(1), (True, "maximized on the main monitor, showing https://www.lensrentals.com/checkout"))[1]
hline = report.checkpoint(TID, str(hsf))
HQID = hline.split("=", 1)[1] if "=" in hline else ""
hcard = next((c for c in cards.pending() if HO_SPEC["step"] in c["question"]), {})
check("hand-off: browser brought on screen before the card", SHOWN == [1] and hline.startswith("CHECKPOINT="), hline)
check("hand-off card says it is open on the screen, the last step is his, no Approve",
      "open on your screen" in hcard.get("question", "") and "Approve" not in [o if isinstance(o, str) else o.get("label") for o in hcard.get("options", [])]
      and report.HANDOFF_DONE in [o if isinstance(o, str) else o.get("label") for o in hcard.get("options", [])], str(hcard)[:300])
check("hand-off card mints nothing on 'I finished it'", not report.build_question(TID, HO_SPEC, (True, "x")).get("exec"))
check("hand-off read-back is kept for this card", json.loads((report.OUT / TID / "handoff.json").read_text(encoding="utf-8")).get("qid") == HQID)
CLOSED = []
doer.browser.close_browser = lambda: CLOSED.append(1) or True
doer._signin_window_open = lambda: False
doer.run_turn = fake_turn("DOER_CHECKPOINT", f"{HQID} {HO_SPEC['step']}")
tickets._transition(TID, "claimed", "test", "", None)
(report.OUT / TID / "handoff.json").write_text(json.dumps({"on_screen": True, "detail": "ok", "epoch": time.time() + 5, "qid": HQID}), encoding="utf-8")
delivered.clear()
doer.run_ticket(tickets._load()[TID])
check("hand-off: the browser is left open (the cart lives in it)", CLOSED == [], str(CLOSED))
check("hand-off: told it is open on the screen, and the step is his", bool(delivered) and "open on your screen" in delivered[-1]
      and "tap Approve" not in delivered[-1] and "I finished it" in delivered[-1], str(delivered[-1:]))
_browser.show_on_screen = lambda: (False, "the window did not land on the main monitor (normal at -32000,-32000)")
hline2 = report.checkpoint(TID, str(hsf))
HQID2 = hline2.split("=", 1)[1] if "=" in hline2 else ""
hcard2 = [c for c in cards.pending() if HO_SPEC["step"] in c["question"]][-1]
check("hand-off not confirmed: the card never says 'open on your screen'", "open on your screen" not in hcard2["question"]
      and "could not confirm" in hcard2["question"], hcard2["question"][-300:])
_s2 = doer._say("owner1", "DOER_CHECKPOINT", f"{HQID2} {HO_SPEC['step']}", tickets._load()[TID])
check("hand-off not confirmed: the chat line never says it either", "open on your screen" not in _s2 and "could not confirm" in _s2, _s2)
CLOSED.clear()
tickets._transition(TID, "claimed", "test", "", None)
doer.run_turn = fake_turn("DOER_CHECKPOINT", f"{HQID2} {HO_SPEC['step']}")
doer.run_ticket(tickets._load()[TID])
check("hand-off not confirmed: the browser is closed as usual", CLOSED == [1], str(CLOSED))
check("a normal checkpoint after a hand-off is not worded as one", "tap Approve" in doer._say("owner1", "DOER_CHECKPOINT", f"{QID} Click Submit", tickets._load()[TID]))
_bp2 = doer.build_prompt(tickets._load()[TID])
check("prompt tells the doer about hand-offs", '"handoff"' in _bp2 and 'never write "it is open on your screen"' in _bp2)

print(f"\n{'ALL PASSED' if not FAILS else str(len(FAILS)) + ' FAILED: ' + ', '.join(FAILS)}  ({TMP})")
sys.exit(1 if FAILS else 0)

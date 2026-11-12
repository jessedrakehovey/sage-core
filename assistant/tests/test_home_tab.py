"""Offline tests for the Home tab, the registered commands, the settings switches and the Send later picker
(2026-11-04): scripts/home_tab.py, gchat.parse_home / parse_command, gchat_events.home_open / home_click /
command_event / later_submit, switches dotted fields. Scratch stores; no model, no browser, no Google.
   python assistant/tests/test_home_tab.py
"""
import os as _os, tempfile as _tf
_os.environ.setdefault("WV_LOG_DIR", _tf.mkdtemp(prefix="wv_logs_"))   # never the live ~/.claude logs
import json
import os
import sys
import tempfile
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
TMP = Path(tempfile.mkdtemp(prefix="wv_home_"))
os.environ["ASSISTANT_DATA"] = str(TMP / "adata")
os.environ["ASSISTANT_CARDS"] = str(TMP / "cards")
os.environ["ASSISTANT_LEARNINGS"] = str(TMP / "learnings")
os.environ["RULINGS_FILE"] = str(ROOT / "data" / "rulings.json")
os.environ["DOER_DATA"] = str(TMP / "doer")
os.environ["DOER_STATUS"] = str(TMP / "doer_status.md")
os.environ["AGENT_STATE"] = str(TMP / "agent_state")
os.environ["AGENT_DATA"] = str(TMP / "agent_data")
os.environ["AGENT_HOME_SPACE"] = "spaces/HOME"
os.environ["AGENT_SUGGESTIONS"] = str(TMP / "sugg.md")
os.environ["HOME_TAB_CACHE"] = str(TMP / "home_cache.json")
os.environ["AGENT_SWITCHES"] = str(TMP / "switches.json")
os.environ["AGENT_SWITCH_ROOT"] = str(TMP)
os.environ["ASSISTANT_CHANNEL"] = "null"
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT / "doer"))
sys.path.insert(0, str(ROOT / "agent"))
import cards  # noqa: E402
import tickets  # noqa: E402
import shutil  # noqa: E402
for _sid in ("h1", "h2"):
    shutil.rmtree(ROOT / "out" / "doer" / tickets.make_id("t", _sid), ignore_errors=True)
import home_tab  # noqa: E402
import startup  # noqa: E402
startup.pending = lambda fresh=True: []          # the start-up sequence is done unless a test says otherwise
import gchat  # noqa: E402
import gchat_events  # noqa: E402
import chat_push  # noqa: E402
import switches  # noqa: E402
import livecard  # noqa: E402
import core  # noqa: E402
import exec_store as es  # noqa: E402

FAILS = []


def check(name, cond, extra=""):
    print(("ok   " if cond else "FAIL ") + name + (f"  {extra}" if extra and not cond else ""))
    if not cond:
        FAILS.append(name)


def widgets(card):
    return [w for s in card["sections"] for w in s.get("widgets", [])]


def buttons(card):
    return [b for w in widgets(card) if "buttonList" in w for b in w["buttonList"]["buttons"]]


def chip_labels(card):
    return [c["label"] for w in widgets(card) if "chipList" in w for c in w["chipList"]["chips"]]


def params(b):
    return {p["key"]: p["value"] for p in b["onClick"]["action"]["parameters"]}


# no Google, no wmic surprises: the pieces that leave the PC are stubbed
home_tab.today_events = lambda: [{"title": "Call with a couple", "start": "2026-11-04T15:00:00-08:00", "all_day": False, "location": "", "meet": "https://meet.google.com/abc"}]
home_tab.pc_health = lambda: {"cpu_pct": 12, "disk_free_gb": 410, "uptime_h": 30.5, "browser": "closed", "spine_last": "09:50"}

# ------------------------------------------------------------------ 1. the desk, empty
d = home_tab.desk("home")
check("desk: a header with the agent's name, four sections and the chips", d["header"]["title"].endswith("'s desk")
      and [s.get("header") for s in d["sections"][:4]] == ["Right now", "Waiting on you", "Coming up", "The PC"]
      and chip_labels(d) == ["Jobs", "Approvals", "Settings", "Help", "Control PC"])
check("desk: nothing running, nothing waiting, the PC line", "Nothing running" in json.dumps(d) and "Nothing waits on you" in json.dumps(d)
      and "CPU 12%" in json.dumps(d) and "410 GB free" in json.dumps(d))
check("desk: the calendar row has a Join link", any("Join" in json.dumps(w) and "meet.google.com" in json.dumps(w) for w in widgets(d)))

# ------------------------------------------------------------------ 2. a running job and an open card
tickets.add_many([{"source": "t", "source_id": "h1", "lane": "personal", "kind": "agent_run", "title": "Rent the 35mm",
                   "summary": "rent", "payload": {"text": "Rent the 35mm for the 7th", "from": "owner", "space": "spaces/HOME"}}], "test")
t = tickets.claim(limit=1, by="doer", kind="agent_run")[0]
TID = t["id"]


class FakeChannel:
    def __init__(self):
        self.posted, self.patched = [], []

    def post_body(self, space, body, thread=""):
        self.posted.append((space, body, thread))
        return f"{space}/messages/m{len(self.posted)}"

    def patch_body(self, name, body):
        self.patched.append((name, body))
        return True


livecard.CHANNEL = FakeChannel()
livecard.capture = lambda t, out_path: False
livecard.start(t, "Opening the browser")
st = livecard.state(TID)
st["shot_url"] = "https://lh3.googleusercontent.com/d/SHOT1"
livecard._save(TID, st)
r = cards.post([{"question": "A new lead wrote in. Reply?", "header": "New lead: a couple", "lane": "personal", "urgent": True,
                 "options": [{"label": "Draft a reply (Recommended)"}, {"label": "Not now"}]}], owner="test", lane="personal")
QID = r["qid"]
d = home_tab.desk("home")
j = json.dumps(d)
check("desk: the running job with its picture and Watch / Show / Stop", "Rent the 35mm" in j and "SHOT1" in j
      and [b["text"] for b in buttons(d)][:3] == ["Control PC", "Bring it to the front", "Stop"]
      and params(buttons(d)[2]) == {"do": "stop", "ctx": "home", "job": TID})
check("desk: the open card is listed and Open approvals is the filled button", "New lead: a couple" in j and "1 to answer now" in j
      and any(b["text"] == "Open approvals" and b.get("color") for b in buttons(d)))

# ------------------------------------------------------------------ 3. the views and the taps
a = home_tab.approvals_view("home")
bl = buttons(a)
check("approvals view: one section per card, its options as buttons, the recommended one filled and its label clean",
      a["header"]["subtitle"] == "1 open" and [b["text"] for b in bl[:2]] == ["Draft a reply", "Not now"] and params(bl[0])["option"] == "Draft a reply (Recommended)"
      and params(bl[0])["qid"] == QID)
card, toast = home_tab.act("answer", {"ctx": "home", "qid": QID, "option": "Draft a reply (Recommended)"}, {}, {"key": "owner"})
check("tap answer: the card is answered by the guarded path and the approvals view comes back",
      (cards._answers_index().get(QID) or {}).get("option") == "Draft a reply (Recommended)" and toast.startswith("Draft a reply: done")
      and card["header"]["title"] == "Approvals" and card["header"]["subtitle"] == "0 open", toast)
card, toast = home_tab.act("stop", {"ctx": "home", "job": TID}, {}, {"key": "owner"})
check("tap stop: the live card's stop flag is written, the jobs view comes back with a toast",
      livecard.stop_requested(livecard.OUT / TID) and card["header"]["title"] == "Jobs" and "Stopping" in toast)
card, toast = home_tab.act("stop", {"ctx": "home", "job": "0" * 16}, {}, {"key": "owner"})
check("tap stop on a job with no live card: the jobs view and a plain toast", card["header"]["title"] == "Jobs" and "not running" in toast, toast)
h = home_tab.help_view("home")
check("help lists the slash commands and the quick commands", "/desk" in json.dumps(h) and "Control PC" in json.dumps(h) and "/login" in json.dumps(h))

# ------------------------------------------------------------------ 4. settings switches (a scratch registry, a scratch config)
cfg = TMP / "cfg.json"
cfg.write_text(json.dumps({"leads": {"auto_send": True, "other": {"enabled": False}}, "texts": {"enabled": True}}, indent=1), encoding="utf-8")
rel = "cfg.json"
Path(os.environ["AGENT_SWITCHES"]).write_text(json.dumps({"jobs": [
    {"key": "auto-send", "name": "Auto-send", "what": "sends by itself", "config": rel, "field": "leads.auto_send"},
    {"key": "texts", "name": "Standing texts", "what": "the fixed texts", "config": rel, "field": "texts.enabled"}]}), encoding="utf-8")
switches._STATES["ts"] = 0.0
s = home_tab.settings_view("home")
sw = [w["decoratedText"]["switchControl"] for w in widgets(s) if "decoratedText" in w and "switchControl" in w["decoratedText"]]
check("settings: one switch per registered job, selected from its config field, onChange is home_action switch",
      [x["selected"] for x in sw] == [True, True] and params({"onClick": {"action": sw[0]["onChangeAction"]}})["key"] == "auto-send", json.dumps(sw)[:300])
core.log_change = lambda *a, **k: None
card, toast = home_tab.act("switch", {"ctx": "home", "key": "auto-send"}, {}, {"key": "owner"})
after = json.loads(cfg.read_text(encoding="utf-8"))
check("flip a dotted field off: only that field changes, formatting kept, toast says so",
      after["leads"]["auto_send"] is False and after["leads"]["other"]["enabled"] is False and after["texts"]["enabled"] is True
      and toast == "Auto-send is now off." and "indent" not in cfg.read_text() and cfg.read_text().count("\n") >= 8, toast)
card, toast = home_tab.act("switch", {"ctx": "home", "key": "texts"}, {"sw:texts": "on"}, {"key": "owner"})
check("flip on when already on: unchanged", toast == "Standing texts was already on.", toast)
card, toast = home_tab.act("switch", {"ctx": "home", "key": "texts"}, {}, {"key": "owner"})
check("flip texts.enabled off: the leaf under its own object, not the other 'enabled'",
      json.loads(cfg.read_text(encoding="utf-8"))["texts"]["enabled"] is False and json.loads(cfg.read_text(encoding="utf-8"))["leads"]["other"]["enabled"] is False)

# ------------------------------------------------------------------ 5. Chat's shapes through gchat_events
gchat_events._core = lambda: core
_person = {"key": "owner", "name": "Owner", "role": "owner"}
core.person_for = lambda handle="", email="", chat_user="": _person if email == "owner@example.com" else None
core.can = lambda person, what: bool(person)
home_ev = {"relayFunction": "onAppHome", "chat": {"user": {"email": "owner@example.com", "name": "users/1"}, "eventTime": "x"},
           "commonEventObject": {"hostApp": "CHAT"}}
r = gchat_events.handle(home_ev)
check("home open: RenderActions pushCard with the desk", list(r["action"]["navigations"][0].keys()) == ["pushCard"]
      and r["action"]["navigations"][0]["pushCard"]["header"]["title"].endswith("'s desk"))
r = gchat_events.handle(dict(home_ev, chat={"user": {"email": "staff@example.com"}}))
check("home open by a non-owner: a one-line card, nothing of the desk", "for the owners" in json.dumps(r) and "Right now" not in json.dumps(r))
click = {"relayFunction": "home_action", "chat": {"user": {"email": "owner@example.com"}, "buttonClickedPayload": {"space": {"name": "spaces/DM1"}}},
         "commonEventObject": {"parameters": {"do": "jobs", "ctx": "home"}}}
r = gchat_events.handle(click)
check("home chip tap: RenderActions updateCard with the view", list(r["action"]["navigations"][0].keys()) == ["updateCard"]
      and r["action"]["navigations"][0]["updateCard"]["header"]["title"] == "Jobs")
mclick = {"relayFunction": "home_action", "chat": {"user": {"email": "owner@example.com"}, "buttonClickedPayload": {"message": {"name": "spaces/DM1/messages/m9", "space": {"name": "spaces/DM1"}}}},
          "commonEventObject": {"parameters": {"do": "approvals", "ctx": "msg"}}}
r = gchat_events.handle(mclick)
check("a tap on a command's card in the space: the message is updated in place", "updateMessageAction" in json.dumps(r) and "Approvals" in json.dumps(r))
cmd = {"relayFunction": "onAppCommand", "chat": {"user": {"email": "owner@example.com"}, "appCommandPayload": {
    "appCommandMetadata": {"appCommandId": 1, "appCommandType": "SLASH_COMMAND"},
    "message": {"name": "spaces/DM1/messages/c1", "text": "/desk", "argumentText": "", "sender": {"email": "owner@example.com", "type": "HUMAN"},
                "space": {"name": "spaces/DM1", "type": "DM"}}}}}
r = gchat_events.handle(cmd)
check("/desk command: answered on the spot with the desk card as a message", "createMessageAction" in json.dumps(r)
      and r["hostAppDataAction"]["chatDataAction"]["createMessageAction"]["message"]["cardsV2"][0]["card"]["header"]["title"].endswith("'s desk"))
qcmd = dict(cmd, chat={"user": {"email": "owner@example.com"}, "appCommandPayload": {"appCommandMetadata": {"appCommandId": 13, "appCommandType": "QUICK_COMMAND"},
                                                                                     "space": {"name": "spaces/DM1"}}})
r = gchat_events.handle(qcmd)
check("quick command 13 (no text): named by its id -> the Watch card with the Remote Desktop link",
      "Watch" in json.dumps(r) and "remotedesktop.google.com" in json.dumps(r))
lcmd = dict(cmd, chat={"user": {"email": "owner@example.com"}, "appCommandPayload": {"appCommandMetadata": {"appCommandId": 6, "appCommandType": "SLASH_COMMAND"},
                                                                                     "message": {"text": "/login stripe.com", "argumentText": "stripe.com", "space": {"name": "spaces/DM1"}}}})
r = gchat_events.handle(lcmd)
check("/login stripe.com: the vault card with the Enter login dialog button", "Enter login" in json.dumps(r) and "stripe.com" in json.dumps(r) and "vault_add" in json.dumps(r))
other = dict(cmd, chat={"user": {"email": "owner@example.com"}, "appCommandPayload": {"appCommandMetadata": {"appCommandId": 77, "appCommandType": "SLASH_COMMAND"},
                                                                                      "message": {"name": "spaces/DM1/messages/c2", "text": "/rules", "space": {"name": "spaces/DM1", "type": "DM"}, "sender": {"email": "owner@example.com"}}}})
r = gchat_events.handle(other)
inbox = core.rows(gchat.INBOX_HTTP) if hasattr(core, "rows") else []
check("an unregistered-here command (/rules) goes to the brain as a message", r == {} and any(x.get("text") == "/rules" for x in inbox))
check("the commands table prints every id once", len({c[0] for c in home_tab.COMMANDS}) == len(home_tab.COMMANDS) and "/desk" in home_tab.commands_table())

# ------------------------------------------------------------------ 6. Send later: the picker on an outbound card, the submit -> not_before
_real_validate = es.validate_payload
es.validate_payload = lambda kind, payload: ""          # the sender identity is the organization's own; not what is tested here
r = cards.post([{"question": "Reply to the vendor?\n[EMAIL PREVIEW]\nfrom owner@example.com to vendor@example.com\nsubject: Friday\n\nThanks, see you Friday.", "header": "Email to a vendor", "lane": "personal",
                 "options": [{"label": "Send"}, {"label": "Skip"}],
                 "exec": {"kind": "email_send", "approve_label": "Send", "payload": {"from": "owner@example.com", "to": "vendor@example.com", "subject": "Friday",
                                                                                      "body": "Thanks, see you Friday."}}}],
               owner="test", lane="personal")
check("outbound card posted", r.get("posted"), r.get("reason"))
EQ = r["qid"]
ecard = cards._read(cards.LIVE / f"{EQ}.json")
eb = chat_push.card_body(ecard, "open")
later = [b for w in eb["cardsV2"][0]["card"]["sections"][-1]["widgets"] if "buttonList" in w for b in w["buttonList"]["buttons"] if b["text"] == "Send later"]
check("an outbound card carries a Send later dialog button", later and later[0]["onClick"]["action"]["interaction"] == "OPEN_DIALOG"
      and params(later[0]) == {"qid": EQ, "q": "0", "mode": "later"})
dlg = chat_push.later_dialog(ecard, 0, tz_offset_min=-480)
pick = [w["dateTimePicker"] for w in dlg["sections"][0]["widgets"] if "dateTimePicker" in w][0]
check("the picker dialog: DATE_AND_TIME, next full hour, the owner's offset, a 'Send then' button",
      pick["type"] == "DATE_AND_TIME" and int(pick["valueMsEpoch"]) % 3600000 == 0 and pick["timezoneOffsetDate"] == -480
      and any(b["text"] == "Send then" for w in dlg["sections"][0]["widgets"] if "buttonList" in w for b in w["buttonList"]["buttons"]))
when_ms = (int(time.time()) // 3600 + 3) * 3600 * 1000
sub = {"relayFunction": "card_edit_submit", "chat": {"user": {"email": "owner@example.com"}, "buttonClickedPayload": {"message": {"name": "spaces/HOME/messages/e1", "space": {"name": "spaces/HOME"}}}},
       "commonEventObject": {"parameters": {"qid": EQ, "q": "0", "mode": "later"}, "dialogEventType": "SUBMIT_DIALOG",
                             "formInputs": {"when": {"dateTimeInput": {"msSinceEpoch": when_ms, "hasDate": True, "hasTime": True}}},
                             "timeZone": {"id": "America/Los_Angeles", "offset": -28800000}}}
k = gchat.parse_click(sub)
check("parse_click reads a date-time input as the ms epoch", k and k["form"].get("when") == str(when_ms), repr(k and k.get("form")))
r = gchat_events.handle(sub)
a = cards._answers_index().get(EQ) or {}
tk = es.fold().get(a.get("ticket")) or {}
want = time.strftime("%Y-%m-%d %H:%M", time.localtime(when_ms / 1000))
check("send later: the card is answered Send with the picked time as the ticket's not_before; the dialog closes with the time in words",
      "CLOSE_DIALOG" in json.dumps(r) and a.get("option") == "Send" and str(tk.get("not_before") or "")[:16] == want and "Goes out" in json.dumps(r),
      json.dumps(r)[:200] + " " + json.dumps(tk)[:200])
check("send later: the picker line says when, not an id", es.when_text(want) in json.dumps(r))
past = dict(sub, commonEventObject=dict(sub["commonEventObject"], formInputs={"when": {"dateTimeInput": {"msSinceEpoch": 1000}}}))
r2 = cards.post([{"question": "Reply again?\n[EMAIL PREVIEW]\nfrom owner@example.com to vendor@example.com\nsubject: x\n\nok", "header": "Email to a vendor 2", "lane": "personal", "options": [{"label": "Send"}],
                  "exec": {"kind": "email_send", "approve_label": "Send", "payload": {"from": "owner@example.com", "to": "vendor@example.com", "subject": "x", "body": "ok"}}}],
                owner="test", lane="personal", asked=True)
past["commonEventObject"]["parameters"] = {"qid": r2.get("qid", EQ), "q": "0", "mode": "later"}
r = gchat_events.handle(past)
check("send later: a time in the past keeps the dialog open with the reason", "pushCard" in json.dumps(r) and "Pick a time that works" in json.dumps(r), json.dumps(r)[:200])

# ------------------------------------------------------------------ 7. the Google-side setup as a Chat button (chat-home, a job row)
import chat_forms  # noqa: E402
import setup_actions  # noqa: E402
js = chat_forms.spec("chat-home")
check("chat-home is a job row: a button label, the how, Control PC", js and js["store"] == "job" and js["title"] == "Set up the Home tab" and js["watch"] and "sign in" in js["intro"].lower(), repr(js)[:200])
jc = chat_forms.card("chat-home")
jb = [b for w in jc["cardsV2"][0]["card"]["sections"][0]["widgets"] if "buttonList" in w for b in w["buttonList"]["buttons"]]
check("the job card: one filled Run button (form_action do=run) and the Remote Desktop link",
      [b["text"] for b in jb] == ["Set up the Home tab", "Control PC"] and jb[0]["onClick"]["action"]["function"] == "form_action"
      and {q["key"]: q["value"] for q in jb[0]["onClick"]["action"]["parameters"]} == {"rid": "chat-home", "do": "run"} and jb[0].get("color"))
check("'set up the home tab' in chat finds the job row without a secret word", chat_forms.match("can you set up the Home tab now") == "chat-home"
      and chat_forms.match("redo the chat commands") == "chat-home" and chat_forms.match("add a form question") == "")
rc = chat_forms.job_card("chat-home", result={"ok": False, "state": "needs_login", "site": "accounts.google.com", "message": "accounts.google.com wants a sign-in and my vault has no login for it."})
jb2 = [b for w in rc["cardsV2"][0]["card"]["sections"][0]["widgets"] if "buttonList" in w for b in w["buttonList"]["buttons"]]
check("a needs_login result: Enter login opens the vault dialog for that site with the job's id, nothing points at the PC",
      "🔑" in json.dumps(rc, ensure_ascii=False) and jb2[0]["text"] == "Enter login" and jb2[0]["onClick"]["action"]["function"] == "vault_add"
      and {q["key"]: q["value"] for q in jb2[0]["onClick"]["action"]["parameters"]} == {"site": "accounts.google.com", "rid": "chat-home"}
      and "PC screen" not in json.dumps(rc) and "sign in there" not in json.dumps(rc), json.dumps(rc, ensure_ascii=False)[:300])
check("a job row is not a chat form on the Setup page (its button runs the job)", not setup_actions._chat_form("chat-home"))
# the job runner: exit 0 / 2 / 1 of a stand-in script (a flag file makes the second run of a sign-in case succeed)
fake = TMP / "fake_job.py"
fake.write_text('''import sys, os
flag = sys.argv[-1] if sys.argv[-1].endswith('.flag') else ''
mode = sys.argv[1]
if flag and os.path.exists(flag): mode = 'ok'
if flag: open(flag, 'w').close()
print('CHAT_CONSOLE step=all status=' + mode + ' ' + ' '.join(a for a in sys.argv[2:] if not a.endswith('.flag')))
sys.exit({'ok': 0, 'needs_signin': 2}.get(mode, 1))
''', encoding="utf-8")
row = {"name": "Chat app Home tab and commands"}
r0 = setup_actions._job({"argv": ["python", str(fake), "ok", "commands added: /desk"], "timeout_s": 30}, row)
r1 = setup_actions._job({"argv": ["python", str(fake), "failed", "no Save button"], "timeout_s": 30}, row)
check("job runner: exit 0 = done with the detail, else not done with the reason",
      r0["ok"] and r0["state"] == "ok" and "commands added: /desk" in r0["message"] and not r1["ok"] and r1["state"] == "failed" and "no Save button" in r1["message"], repr((r0, r1))[:300])
fills = []
setup_actions._login_fill = lambda url: fills.append(url) or "SIGNED_IN host=accounts.google.com user=o"
flag = TMP / "signin.flag"
r2 = setup_actions._job({"argv": ["python", str(fake), "needs_signin", "Google asks for a sign-in signin_url=https://accounts.google.com/v3/signin/x", str(flag)], "timeout_s": 30}, row)
check("sign-in wall + a vault login: login_fill on that page, then the job runs again and finishes; nothing about the PC",
      fills == ["https://accounts.google.com/v3/signin/x"] and r2["ok"] and r2["state"] == "ok", repr(r2)[:200])
setup_actions._login_fill = lambda url: "NO_ENTRY host=accounts.google.com"
r3 = setup_actions._job({"argv": ["python", str(fake), "needs_signin", "Google asks for a sign-in signin_url=https://accounts.google.com/v3/signin/x"], "timeout_s": 30}, row)
check("sign-in wall, no vault login: needs_login with the site, worded for the Enter login dialog",
      not r3["ok"] and r3["state"] == "needs_login" and r3["site"] == "accounts.google.com" and "Enter login" in r3["message"] and "PC" not in r3["message"], repr(r3)[:200])
setup_actions._login_fill = lambda url: "NEEDS_CODE host=accounts.google.com kind=app"
r4 = setup_actions._job({"argv": ["python", str(fake), "needs_signin", "signin_url=https://accounts.google.com/v3/signin/x"], "timeout_s": 30}, row)
check("sign-in wall, a one-time code: the card asks for the code-generator secret", r4["state"] == "needs_login" and "code-generator secret" in r4["message"])
# the tap: answered at once with the working card; the result edits the card by REST when the job ends
patched = []
gchat.patch_body = lambda name, body: patched.append((name, body)) or True
gchat_events._run_job = lambda sa, rid, by, done: (done({"ok": True, "state": "ok", "message": "Chat app Home tab and commands: done. relay v8"}) or True)
jev = {"relayFunction": "form_action", "chat": {"user": {"email": "owner@example.com"}, "buttonClickedPayload": {"message": {"name": "spaces/DM1/messages/j1", "space": {"name": "spaces/DM1"}}}},
       "commonEventObject": {"parameters": {"rid": "chat-home", "do": "run"}}}
r = gchat_events.handle(jev)
check("the Run tap: the card shows 'Working on it' now, and the result card replaces it by REST",
      "Working on it" in json.dumps(r) and patched and patched[-1][0] == "spaces/DM1/messages/j1" and "✅" in json.dumps(patched[-1][1], ensure_ascii=False) and "relay v8" in json.dumps(patched[-1][1]),
      json.dumps(r)[:200])

# the screenshots drive is a job row too (shots_setup.py)
sd = chat_forms.spec("shots-drive")
check("shots-drive is a job row with its own button and Control PC", sd and sd["store"] == "job" and sd["title"] == "Set up the screenshots drive" and sd["watch"])
check("'set up the screenshots drive' / 'make the pictures drive' find it", chat_forms.match("set up the screenshots drive") == "shots-drive"
      and chat_forms.match("can you set up the shared drive for the pictures") == "shots-drive")
check("both setup rows ship with every new pack", "chat-home" in __import__("new_org").GENERIC_CONNECTIONS and "shots-drive" in __import__("new_org").GENERIC_CONNECTIONS)

# ------------------------------------------------------------------ 8. the relay poller: a lost poll loses nothing (lease + ack), duplicates skipped
import relay_poll  # noqa: E402
relay_poll.SEEN = TMP / "relay_seen.json"
relay_poll.cfg = lambda: {"relay_webapp_url": "https://script.example/exec", "relay_secret": "s"}
relay_poll.mc_events_url = lambda: "http://mc.local/gchat/events"
posts = []
relay_poll._post = lambda url, body, headers=None, timeout=30: (posts.append((url, body)) or (200, {"ok": True}))
batch = {"events": [{"id": "e1", "at": time.time() * 1000, "fn": "form_action", "event": {"relayFunction": "form_action"}},
                    {"id": "e2", "at": time.time() * 1000, "fn": "card_answer", "event": {"relayFunction": "card_answer"}}]}
relay_poll._get = lambda url, timeout=30: batch
st = {"polls": 0, "events": 0, "replied": 0, "late": 0, "errors": 0}
n = relay_poll.poll_once(st)
for th in st.get("workers") or []:                # each event is handed over on its own thread (2026-11-04)
    th.join(10)
acks = [b for u, b in posts if b.get("op") == "ack"]
check("poller: both events handed to Mission Control, replied, and each acked once it was handed over",
      n == 2 and st["events"] == 2 and st["replied"] == 2 and sorted(i for a in acks for i in a["ids"]) == ["e1", "e2"]
      and sum(1 for u, b in posts if u.endswith("/gchat/events")) == 2, repr(posts)[:300])
posts.clear()
n = relay_poll.poll_once(st)                      # the relay hands the same (un-acked, leased) events over again
check("poller: the same events again are skipped but acked, nothing re-handed to Mission Control",
      n == 2 and st["events"] == 2 and not any(u.endswith("/gchat/events") for u, b in posts) and [b for u, b in posts if b.get("op") == "ack"][0]["ids"] == ["e1", "e2"])
# a late job tap: the working card goes up by REST because the relay already showed its own "got it"
patched.clear()
gchat_events._run_job = lambda sa, rid, by, done: True
late_ev = dict(jev, commonEventObject={"parameters": {"rid": "chat-home", "do": "run"}})
r = gchat_events.handle(late_ev, late=time.time() - 60)
check("a late job tap: the working card is put up by REST", patched and patched[-1][0] == "spaces/DM1/messages/j1" and "Working on it" in json.dumps(patched[-1][1]))

# ------------------------------------------------------------------ 9. every button function the PC's cards use exists in the relay
# (2026-11-04: form_action was missing from Code.gs -> Apps Script "Script function not found" -> Chat "Unable to process request")
import re as _re
gs = (ROOT / "agent" / "gchat_relay" / "Code.gs").read_text(encoding="utf-8")
defined = set(_re.findall(r"(?m)^function (\w+)\(", gs))
used = set()
for f in ("scripts/chat_push.py", "scripts/chat_forms.py", "scripts/home_tab.py", "doer/livecard.py", "scripts/vault_chat.py"):
    src = (ROOT / f).read_text(encoding="utf-8")
    if f in ("scripts/chat_push.py", "scripts/chat_forms.py"):          # their _button / _btn take the relay function second
        used |= set(_re.findall(r"""(?<!\w)_button\(\s*[^,
]+,\s*["'](\w+)["']""", src))
        used |= set(_re.findall(r"""(?<!\w)_btn\(\s*[^,
]+,\s*["'](\w+)["']""", src))
    used |= set(_re.findall(r""""function":\s*["'](\w+)["']""", src))
    used |= set(_re.findall(r"""^FN\s*=\s*["'](\w+)["']""", src, _re.M))
missing = sorted(u for u in used if u not in defined)
check("every button function the cards use is a function in the relay (Code.gs)", used and not missing, f"missing: {missing}; used: {sorted(used)}")

# the vault dialog from a job card: saved -> the job runs again by itself, the card follows
restarted = []
gchat_events._run_job = lambda sa, rid, by, done: (restarted.append(rid) or done({"ok": True, "state": "ok", "message": "Chat app Home tab and commands: done."}) or True)
gchat_events._vault_add = lambda fields: "ADDED vault accounts.google.com owner@example.com totp=yes"
patched.clear()
vsub = {"chat": {"user": {"email": "owner@example.com"}, "buttonClickedPayload": {"message": {"name": "spaces/DM1/messages/j1", "space": {"name": "spaces/DM1"}}}},
        "commonEventObject": {"parameters": {"site": "accounts.google.com", "rid": "chat-home"}, "dialogEventType": "SUBMIT_DIALOG",
                              "formInputs": {"site": {"stringInputs": {"value": ["accounts.google.com"]}}, "username": {"stringInputs": {"value": ["owner@example.com"]}},
                                             "password": {"stringInputs": {"value": ["hunter2"]}}, "totp": {"stringInputs": {"value": ["JBSWY3DP"]}}}},
        "relayFunction": "vault_add_submit"}
r = gchat_events.handle(vsub)
check("vault submit from a job card: the dialog closes with 'running again', the job restarts, the card ends on the result",
      "CLOSE_DIALOG" in json.dumps(r) and "Running" in json.dumps(r) and restarted == ["chat-home"] and patched and "✅" in json.dumps(patched[-1][1], ensure_ascii=False), json.dumps(r)[:200])
vd = chat_push.vault_dialog("accounts.google.com", rid="chat-home")
check("the vault dialog from a job carries the job id back on its Save button",
      {q["key"]: q["value"] for q in [b for w in vd["sections"][0]["widgets"] if "buttonList" in w for b in w["buttonList"]["buttons"]][0]["onClick"]["action"]["parameters"]} == {"site": "accounts.google.com", "rid": "chat-home"})

# the result line carries no script prefix, and a job that opened the agent's browser closes it again
fake2 = TMP / "fake_job2.py"
fake2.write_text("print('SHOTS_SETUP status=ok made the shared drive; the service account sees it')\n", encoding="utf-8")
ups = iter([False, True])
closed = []
setup_actions._browser_up = lambda: next(ups, True)
setup_actions._browser_close = lambda: closed.append(1)
r5 = setup_actions._job({"argv": ["python", str(fake2)], "timeout_s": 30}, {"name": "Screenshots shared drive"})
check("job result: 'SHOTS_SETUP status=ok' is not in the owner's words; the browser the job opened is closed",
      r5["ok"] and r5["message"] == "Screenshots shared drive: done. made the shared drive; the service account sees it" and closed == [1], repr(r5))
setup_actions._browser_up = lambda: True
closed.clear()
setup_actions._job({"argv": ["python", str(fake2)], "timeout_s": 30}, {"name": "x"})
check("a browser that was already up (a doer job's) is left alone", closed == [])

# the desk carries the start-up sequence while anything in it is to do, and its button posts the card into the chat
startup.pending = lambda fresh=True: [{"rid": "chat-home", "name": "Chat app Home tab and commands"}]
d2 = home_tab.desk("home")
fb = [b for w in d2["sections"][0]["widgets"] if "buttonList" in w for b in w["buttonList"]["buttons"]]
check("desk: 'Finish setting up' is the first section while a start-up job is pending, one button",
      d2["sections"][0].get("header") == "Finish setting up" and [b["text"] for b in fb] == ["Set up everything"] and params(fb[0])["do"] == "startup")
offered = []
startup.offer = lambda space, thread="", force=False: offered.append((space, force)) or True
home_tab._owner_space = lambda key: "spaces/DM1"
card, toast = home_tab.act("startup", {"ctx": "home"}, {}, {"key": "owner"})
check("tap: the start-up card is posted into the owner's chat (forced), the toast says where", offered == [("spaces/DM1", True)] and "in our chat" in toast, toast)
startup.pending = lambda fresh=True: []

print()
print("ALL PASSED" if not FAILS else f"{len(FAILS)} FAILED: {FAILS}", f" ({TMP})")
sys.exit(1 if FAILS else 0)

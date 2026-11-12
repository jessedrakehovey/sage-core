"""Offline tests for the live job card, the screenshot host and the sign-in card (2026-11-03):
doer/livecard.py, scripts/shots.py, doer/report.py login, chat_push's dialog buttons, gchat_events'
job taps and vault-submit-answers-the-card, doer.run_login_resume and the hand sign-in path.
Scratch stores; no model, no browser, no Google.   python assistant/tests/test_live_card.py
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
TMP = Path(tempfile.mkdtemp(prefix="wv_live_"))
os.environ["ASSISTANT_DATA"] = str(TMP / "adata")
os.environ["ASSISTANT_CARDS"] = str(TMP / "cards")
os.environ["ASSISTANT_LEARNINGS"] = str(TMP / "learnings")
os.environ["RULINGS_FILE"] = str(ROOT / "data" / "rulings.json")
os.environ["DOER_DATA"] = str(TMP / "doer")
os.environ["DOER_STATUS"] = str(TMP / "doer_status.md")
os.environ["DOER_VAULT_KEY"] = "test-key"
os.environ["AGENT_STATE"] = str(TMP / "agent_state")
os.environ["AGENT_PICKUP"] = "off"                     # a test step's end never wakes a real pick-up turn
os.environ["AGENT_DATA"] = str(TMP / "agent_data")
os.environ["AGENT_HOME_SPACE"] = "spaces/HOME"
os.environ["AGENT_SUGGESTIONS"] = str(TMP / "sugg.md")
os.environ["SHOTS_INDEX"] = str(TMP / "shots_index.json")
os.environ["ASSISTANT_CHANNEL"] = "null"
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT / "doer"))
sys.path.insert(0, str(ROOT / "agent"))
import cards  # noqa: E402
import tickets  # noqa: E402
import doer  # noqa: E402
import shutil  # noqa: E402
for _sid in ("lc", "lg", "self", "stopq"):                 # the fixture tickets' out/doer folders from an earlier run (ids are deterministic)
    shutil.rmtree(ROOT / "out" / "doer" / tickets.make_id("t", _sid), ignore_errors=True)
import report  # noqa: E402
import notes  # noqa: E402
import livecard  # noqa: E402
import click_guard  # noqa: E402
import shots  # noqa: E402
import chat_push  # noqa: E402
import gchat_events  # noqa: E402

FAILS = []


def check(name, cond, extra=""):
    print(("ok   " if cond else "FAIL ") + name + (f"  {extra}" if extra and not cond else ""))
    if not cond:
        FAILS.append(name)


class FakeChannel:
    def __init__(self):
        self.posted, self.patched = [], []

    def configured(self):
        return True

    def post_body(self, space, body, thread=""):
        self.posted.append((space, body, thread))
        return f"{space}/messages/m{len(self.posted)}"

    def patch_body(self, name, body):
        self.patched.append((name, body))
        return True


def widgets(body):
    card = body["cardsV2"][0]["card"] if "cardsV2" in body else body          # a message, or a dialog's card
    return [w for s in card["sections"] for w in s["widgets"]]


def buttons(body):
    return [b for w in widgets(body) if "buttonList" in w for b in w["buttonList"]["buttons"]]


# ------------------------------------------------------------------ 1. shots.py: the Drive host, stubbed
calls = []


def fake_api(url, body=None, method="", content_type="application/json", timeout=30):
    calls.append((method or ("POST" if body is not None else "GET"), url))
    if "/drives?" in url:
        return {"drives": [{"id": "0AAdrive", "name": "Agent screenshots"}, {"id": "0AAother", "name": "Other"}]}
    if "upload/drive" in url:
        return {"id": "FILE123"}
    return {}


shots._api = fake_api
shots._sa_key = lambda: str(ROOT / "tests" / "test_live_card.py")       # "a key exists"
shots._cfg = lambda: {"backend": "drive", "drive_name": "agent SCREENSHOTS", "folder_id": "", "ttl_s": 60}
png = TMP / "shot.png"
from PIL import Image  # noqa: E402
Image.new("RGB", (1600, 900), (74, 92, 67)).save(png)
check("shots: the shared drive is found by name, case-insensitively, and remembered", shots.drive_id() == "0AAdrive" and shots.drive_id() == "0AAdrive"
      and len([c for c in calls if "/drives?" in c[1]]) == 1)
url = shots.publish(png)
check("shots: publish uploads into the drive, link-shares, and returns the card url",
      url == "https://lh3.googleusercontent.com/d/FILE123" and any("upload/drive" in c[1] and "supportsAllDrives=true" in c[1] for c in calls)
      and any("/permissions" in c[1] for c in calls))
data, mime = shots.shrink(png)
check("shots: the picture travels as a JPEG no wider than 1000", mime == "image/jpeg" and len(data) < png.stat().st_size + 1)
idx = json.loads(Path(os.environ["SHOTS_INDEX"]).read_text(encoding="utf-8"))
check("shots: the hosted file is indexed with its lifetime", "FILE123" in idx["files"] and idx["files"]["FILE123"]["exp"] > time.time())
check("shots: sweep leaves a live file alone, deletes a dead one", shots.sweep() == 0 and shots.sweep(now=time.time() + 120) == 1
      and any(c == ("DELETE", "https://www.googleapis.com/drive/v3/files/FILE123?supportsAllDrives=true") for c in calls))
shots._cfg = lambda: {"backend": "off"}
check("shots: backend off -> no url, status says so", shots.publish(png) == "" and shots.status()["ok"] is False)
shots._cfg = lambda: {"backend": "drive", "drive_name": "Nowhere", "folder_id": "", "ttl_s": 60}
st = shots.status()
check("shots: a missing shared drive -> status tells the owner the two clicks", st["ok"] is False and "shared drive" in st["hint"] and "Nowhere" in st["hint"])
shots._cfg = lambda: {"backend": "drive", "drive_name": "agent screenshots", "folder_id": "", "ttl_s": 60}
shots.enabled = lambda: True

# ------------------------------------------------------------------ 2. the live card
tickets.add_many([{"source": "t", "source_id": "lc", "lane": "personal", "kind": "agent_run", "title": "Rent the 35mm",
                   "summary": "rent the 35mm", "payload": {"text": "Rent the 35mm for the 7th from the rental site", "from": "owner",
                                                           "space": "spaces/HOME", "thread": "spaces/HOME/threads/T1"}}], "test")
t = tickets.claim(limit=1, by="doer", kind="agent_run")[0]
TID = t["id"]
ch = FakeChannel()
livecard.CHANNEL = ch
def _fake_capture(t, out_path):
    Path(out_path).parent.mkdir(parents=True, exist_ok=True)
    Path(out_path).write_bytes(png.read_bytes())
    return True


livecard.capture = _fake_capture
name = livecard.start(t, "Opening the browser")
check("live card: posted once into the job's thread, as 'on it'", name == "spaces/HOME/messages/m1" and ch.posted[0][2] == "spaces/HOME/threads/T1"
      and ch.posted[0][1]["text"].startswith("On it: Rent the 35mm") and "on it since" in ch.posted[0][1]["cardsV2"][0]["card"]["header"]["subtitle"])
b0 = ch.posted[0][1]
labels = [b["text"] for b in buttons(b0)]
check("live card: Control PC (Remote Desktop link), Bring it to the front, Stop", labels == ["Control PC", "Bring it to the front", "Stop"]
      and buttons(b0)[0]["onClick"]["openLink"]["url"].startswith("https://remotedesktop.google.com")
      and {p["key"]: p["value"] for p in buttons(b0)[2]["onClick"]["action"]["parameters"]} == {"job": TID, "option": "Stop"})
check("live card: the first picture is hosted and on the card", any("image" in w and w["image"]["imageUrl"].endswith("/d/FILE123") for w in widgets(b0)))
notes.record(TID, "signed in to the rental site", "progress")
notes.record(TID, "dates set, 7 to 10 November", "progress")
b1 = ch.patched[-1][1]
texts = [w["decoratedText"]["text"] for w in widgets(b1) if "decoratedText" in w]
check("live card: a milestone note redraws the card in place with ✓ past steps and ▶ the current one",
      ch.patched[-1][0] == name and texts == ["✓ signed in to the rental site", "▶ dates set, 7 to 10 November"] and "step 2" in b1["cardsV2"][0]["card"]["header"]["subtitle"])
n_patch = len(ch.patched)
body = livecard.action(TID, "Stop", {"key": "owner"})
check("live card: Stop writes the flag and the card says it is stopping", livecard.stop_requested(livecard.OUT / TID).get("by") == "owner"
      and body and "Stopping at the next step" in json.dumps(body) and len(ch.patched) == n_patch + 1)
deny = click_guard.pre({"tool_name": "mcp__playwright-doer__browser_click"}, livecard.OUT / TID)
check("click guard: after Stop every browser tool is refused with the stop line",
      deny and deny["hookSpecificOutput"]["permissionDecision"] == "deny" and "stopped by the owner" in deny["hookSpecificOutput"]["permissionDecisionReason"])
check("click guard: no flag, no refusal", click_guard.pre({"tool_name": "mcp__playwright-doer__browser_click"}, TMP / "nowhere") is None)
livecard.finish(TID, "DOER_BLOCKED", "Stopped, as you asked.")
b2 = ch.patched[-1][1]
check("live card: finish after Stop -> 'stopped by you', no buttons; the report is the message under it, not repeated on the card",
      b2["text"].startswith("Stopped:") and "stopped by you" in b2["cardsV2"][0]["card"]["header"]["subtitle"] and not buttons(b2) and "Stopped, as you asked" not in json.dumps(b2))
check("live card: a job that is over drops its picture (the hosted file is deleted minutes later)",
      not any("image" in w for w in widgets(b2)) and "Picture from" not in json.dumps(b2))
livecard.finish(TID, "DOER_BLOCKED", "I could not open the page.", True)
check("live card: a quiet stop (nothing said in chat) leaves its reason on the card", "I could not open the page." in json.dumps(ch.patched[-1][1]))
livecard.start(t, "again")
livecard.finish(TID, "DOER_CHECKPOINT", "")
bw = ch.patched[-1][1]
check("live card: a job waiting on a tap keeps one last picture of where it stopped", any("image" in w for w in widgets(bw)) and "waiting for your tap" in json.dumps(bw))
n_before = len(ch.patched)
check("live card: a fresh picture is left alone by the expiry", livecard.expire_pictures(ttl_s=600) == 0 and len(ch.patched) == n_before)
check("live card: a picture past its lifetime is taken off the card in place (no broken image left standing)",
      livecard.expire_pictures(ttl_s=600, now=time.time() + 700) == 1 and not any("image" in w for w in widgets(ch.patched[-1][1]))
      and "Picture from" not in json.dumps(ch.patched[-1][1]) and livecard.expire_pictures(ttl_s=600, now=time.time() + 700) == 0)
import browser as _browser  # noqa: E402
_browser.show_on_screen = lambda: (True, "maximized on the main monitor")
livecard.start(t, "again")
check("live card: a job that comes back reuses its card (no second message)", len(ch.posted) == 1 and not livecard.stop_requested(livecard.OUT / TID))
body = livecard.action(TID, livecard.SHOW, {"key": "owner"})
check("live card: Bring it to the front brings the browser up and says Control PC shows it", body and "in front now: Control PC shows it" in json.dumps(body))
check("live card: a tap on a job with no card -> {}", livecard.action("0" * 16, "Stop") == {})
# where it goes: a staff space never gets one
t2 = dict(t, id="f" * 16, payload=dict(t["payload"], space="spaces/TEAM", thread="spaces/TEAM/threads/X"))
check("live card: never in a space that is not the home space or an owner DM", livecard.start(t2) == "" and len(ch.posted) == 1)

# ------------------------------------------------------------------ 3. the job tap through gchat_events
import core  # noqa: E402
gchat_events._core = lambda: core
_person = {"key": "owner", "name": "Owner", "role": "owner"}
core.person_for = lambda handle="", email="", chat_user="": _person
core.can = lambda person, what: True
ev = {"chat": {"user": {"email": "x@example.com"}, "buttonClickedPayload": {"message": {"name": "spaces/HOME/messages/m1", "space": {"name": "spaces/HOME"}}}},
      "commonEventObject": {"parameters": {"job": TID, "option": "Stop"}}, "relayFunction": "card_answer"}
r = gchat_events.handle(ev)
check("gchat: a live-card tap rides the card_answer relay function with {job, option} and answers with the redrawn card",
      "updateMessageAction" in json.dumps(r) and "Stopping at the next step" in json.dumps(r) and livecard.stop_requested(livecard.OUT / TID))
ev2 = dict(ev, commonEventObject={"parameters": {"job": "0" * 16, "option": "Stop"}})
check("gchat: a tap on an unknown job is a plain line", "not running any more" in json.dumps(gchat_events.handle(ev2)))

# ------------------------------------------------------------------ 4. the sign-in card (report.py login)
tickets.add_many([{"source": "t", "source_id": "lg", "lane": "personal", "kind": "agent_run", "title": "Cancel the gym plan",
                   "summary": "cancel the gym", "payload": {"text": "Cancel my gym plan on the gym site", "from": "owner", "thread": "spaces/HOME/threads/T9"}}], "test")
tl = [x for x in tickets.claim(limit=5, by="doer", kind="agent_run") if x["title"] == "Cancel the gym plan"][0]
LT = tl["id"]
line = report.login(LT, "www.gym.example", "https://www.gym.example/login", "NO_ENTRY host=gym.example")
check("login card: posted, LOGIN=<qid>", line.startswith("LOGIN=q"), line)
LQ = line.split("=", 1)[1]
card = cards._read(cards.LIVE / f"{LQ}.json")
q = card["questions"][0]
ex = q["exec"]
check("login card: urgent, in the job's thread, the exec is agent_run with payload.login + host + url",
      q.get("urgent") and card.get("chat_thread") == "spaces/HOME/threads/T9" and ex["kind"] == "agent_run"
      and ex["payload"]["login"] is True and ex["payload"]["host"] == "gym.example" and ex["payload"]["url"].endswith("/login") and ex["approve_label"] == "Enter login")
check("login card: Enter login / I'll sign in myself / Stop", [o["label"] for o in q["options"]] == ["Enter login", "I'll sign in myself", "Stop"])
body = chat_push.card_body(card, "open")
bl = buttons(body)
check("login card in chat: the approve is the vault DIALOG button carrying site + qid, the rest answer the card, plus Control PC",
      bl[0]["text"] == "Enter login" and bl[0]["onClick"]["action"]["function"] == "vault_add" and bl[0]["onClick"]["action"]["interaction"] == "OPEN_DIALOG"
      and {p["key"]: p["value"] for p in bl[0]["onClick"]["action"]["parameters"]} == {"site": "gym.example", "qid": LQ, "q": "0"}
      and bl[1]["onClick"]["action"]["function"] == "card_answer" and bl[-1]["text"] == "Control PC", json.dumps(bl)[:300])
check("login card: the doer's line asks for the login in the dialog, never in chat",
      doer._say("owner", "DOER_CHECKPOINT", f"{LQ} sign in to gym.example", tl).startswith("I need the gym.example login")
      and "not into this chat" in doer._say("owner", "DOER_CHECKPOINT", f"{LQ} sign in to gym.example", tl))
hq = report.build_login_question(LT, "bank.example", "https://bank.example/x", "FAILED captcha on the login page")
check("login card for a captcha: only I'll sign in myself / Stop, approve = Carry on", [o["label"] for o in hq["options"]] == ["I'll sign in myself", "Stop"]
      and hq["exec"]["approve_label"] == "Carry on" and hq["exec"]["payload"]["human"] is True)
dlg = chat_push.vault_dialog("gym.example", qid=LQ)
sb = buttons(dlg)[0]
check("vault dialog from a sign-in card: the Save button carries the qid and says it carries on",
      {p["key"]: p["value"] for p in sb["onClick"]["action"]["parameters"]} == {"site": "gym.example", "qid": LQ} and "carry on" in sb["text"])

# the dialog's submit: saved -> the card is answered -> the exec ticket is minted -> the doer is woken
gchat_events._vault_add = lambda fields: "ADDED vault gym.example owner@example.com totp=no"
woken = []
doer.wake = lambda: woken.append(1) or "WOKEN"
import drain  # noqa: E402
check("drain.kick never starts anything from a scratch store (a test)", drain.kick("doer").startswith("NOT_KICKED"))
kicked = []
drain.kick = lambda lane, after_s=0: kicked.append(lane) or "KICKED"
sub = {"chat": {"user": {"email": "x@example.com"}, "buttonClickedPayload": {"message": {"name": "spaces/HOME/messages/m5", "space": {"name": "spaces/HOME"}}}},
       "commonEventObject": {"parameters": {"site": "gym.example", "qid": LQ}, "dialogEventType": "SUBMIT_DIALOG",
                             "formInputs": {"site": {"stringInputs": {"value": ["gym.example"]}}, "username": {"stringInputs": {"value": ["owner@example.com"]}},
                                            "password": {"stringInputs": {"value": ["hunter2"]}}, "totp": {"stringInputs": {"value": [""]}}}},
       "relayFunction": "vault_add_submit"}
r = gchat_events.handle(sub)
a = cards._answers_index().get(LQ) or {}
check("vault submit with a qid: closes the dialog saying the job picks back up, answers the card with Enter login, mints the ticket, wakes the doer",
      "CLOSE_DIALOG" in json.dumps(r) and "picks back up in under a minute" in json.dumps(r) and a.get("option") == "Enter login" and a.get("ticket") and woken,
      json.dumps(r)[:200] + " " + json.dumps(a)[:200])
check("the approval's own lane is drained right after the undo window, not at the next 10-minute pass (2026-11-04)", kicked == ["doer"], repr(kicked))
check("the answered card says when it starts", "Starts in about" in chat_push.goes_out_line("agent_run") and "10 minutes" not in chat_push.goes_out_line("agent_run"))
import exec_store as es  # noqa: E402
ek = es.fold().get(a.get("ticket")) or {}
check("the minted exec ticket is the login resume: agent_run, payload.login, host, no code", ek.get("kind") == "agent_run" and ek["payload"].get("login") is True
      and ek["payload"]["host"] == "gym.example" and "code" not in ek["payload"], json.dumps(ek)[:300])
check("hunter2 is nowhere in the cards or ticket stores",
      "hunter2" not in "".join(p.read_text(encoding="utf-8") for p in list((TMP / "cards").rglob("*.json*")) + list((TMP / "adata").rglob("*.json*")) if p.is_file()))

# ------------------------------------------------------------------ 5. the resume: drain -> --approved -> run_login_resume -> the job carries on
doer.browser.open_browser = lambda show=False, wait_s=0: "http://127.0.0.1:1"
doer.browser.close_browser = lambda: True
delivered = []
doer.deliver = lambda t, text: delivered.append(text)
doer._session_set(LT, "sess-login")
prompts = []


def resume_turn(prompt, out_dir, resume="", timeout=0):
    prompts.append((prompt, resume))
    return {"ok": True, "text": "DOER_DONE: plan cancelled, confirmation 77", "session": "sess-login", "error": "", "marker": "DOER_DONE", "arg": "plan cancelled, confirmation 77"}


doer.run_turn = resume_turn
import io, contextlib  # noqa: E402
buf = io.StringIO()
with contextlib.redirect_stdout(buf):
    rc = doer.run_approved(dict(ek["payload"]))
check("login resume: the same session gets the SIGNED IN block with login_fill and the host; exit 0 = DOER_DONE for drain",
      rc == 0 and prompts and prompts[0][1] == "sess-login" and "SIGNED IN: CARRY ON" in prompts[0][0] and "gym.example" in prompts[0][0]
      and "login_fill.py" in prompts[0][0] and "DOER_RESULT=DOER_DONE" in buf.getvalue(), buf.getvalue()[:200])
check("login resume: the job's own outcome reaches the owner and the ticket is done",
      delivered and "77" in delivered[-1] and tickets._load()[LT]["status"] == "done")
prompts.clear()
doer.run_turn = lambda prompt, out_dir, resume="", timeout=0: {"ok": True, "text": "", "session": "sess-login", "error": "", "marker": "DOER_BLOCKED",
                                                               "arg": "gym.example still refuses the sign-in: wrong password"}
tickets._transition(LT, "staged", "test", "", None)
buf = io.StringIO()
with contextlib.redirect_stdout(buf):
    rc = doer.run_approved(dict(ek["payload"]))
again = [c for c in cards._live() if c.get("status") == "open" and c["id"] != LQ
         and ((c["questions"][0].get("exec") or {}).get("payload") or {}).get("login") and ((c["questions"][0].get("exec") or {}).get("payload") or {}).get("ticket") == LT]
check("login resume: a login the site refuses brings the sign-in card BACK in the job's thread, with what the site said (2026-11-04)",
      len(again) == 1 and again[0].get("chat_thread") == "spaces/HOME/threads/T9" and "wrong password" in again[0]["questions"][0]["question"]
      and [o["label"] for o in again[0]["questions"][0]["options"]] == ["Enter login", "I'll sign in myself", "Stop"], json.dumps(again)[:300] + buf.getvalue()[:200])
check("login resume: refused is not a false alarm for drain (exit 0, the card is back), and the job waits on that card",
      rc == 0 and "the sign-in card is back" in buf.getvalue() and tickets._load()[LT]["status"] == "staged"
      and again and f"card {again[0]['id']}: checkpoint" in str((tickets._load()[LT].get("history") or [{}])[-1].get("note")), buf.getvalue()[:200])
check("login resume: the owner is told the site did not accept it and that the card is back; nothing about a computer",
      delivered and delivered[-1].startswith("gym.example did not accept that login") and "wrong password" in delivered[-1] and "Enter login" in delivered[-1], delivered[-1] if delivered else "")
doer.run_turn = lambda prompt, out_dir, resume="", timeout=0: {"ok": True, "text": "", "session": "sess-login", "error": "", "marker": "DOER_BLOCKED",
                                                               "arg": "the page shows an empty cart, nothing to cancel"}
tickets._transition(LT, "staged", "test", "", None)
n_cards = len(cards._live())
buf = io.StringIO()
with contextlib.redirect_stdout(buf):
    rc = doer.run_approved(dict(ek["payload"]))
check("login resume: a block that is not about the login posts no sign-in card", len(cards._live()) == n_cards)
check("the refusal words: the site's own sentence counts, an unrelated 'incorrect' does not",
      doer.LOGIN_REFUSED.search('x still refuses the sign-in: the page says "The username or password is incorrect"')
      and doer.LOGIN_REFUSED.search("The password you entered is incorrect") and not doer.LOGIN_REFUSED.search("the total on the page looks incorrect"))

# ------------------------------------------------------------------ 6. "I'll sign in myself": browser on screen + the Carry on card
tickets.add_many([{"source": "t", "source_id": "self", "lane": "personal", "kind": "agent_run", "title": "Renew the parking permit",
                   "summary": "renew permit", "payload": {"text": "Renew the parking permit on the city site", "from": "owner", "thread": "spaces/HOME/threads/T2"}}], "test")
ts = [x for x in tickets.claim(limit=5, by="doer", kind="agent_run") if x["title"] == "Renew the parking permit"][0]
ST = ts["id"]
line = report.login(ST, "city.example", "https://city.example/signin", "FAILED the site says the login is wrong")
SQ = line.split("=", 1)[1]
tickets._transition(ST, "staged", "doer", f"card {SQ}: checkpoint, waiting for the owner's tap", {"claimed", "new"})
check("nothing due before the tap", doer._login_self_due() is None)
# the job ended on a refused login before the tap (its last line is no longer the checkpoint): the tap still counts
tickets._transition(ST, "claimed", "doer", "login handed over: carrying on with the job", {"staged"})
tickets._transition(ST, "staged", "doer", "blocked: city.example still refuses the sign-in", {"claimed"})
cards.answer(SQ, report.SELF, by="owner")
cards._answers_index.cache_clear() if hasattr(cards._answers_index, "cache_clear") else None
due = doer._login_self_due()
check("'I'll sign in myself' answered -> due once", due and due[0]["id"] == ST and due[1] == SQ and due[2]["host"] == "city.example", repr(due)[:200])
shown, navigated = [], []
_browser.open_browser = lambda show=False, wait_s=0: shown.append(show) or "http://127.0.0.1:1"
_browser.navigate = lambda url: navigated.append(url) or True
out = doer.self_signin(*due)
cc = cards._read(cards.LIVE / f"{out}.json") if out else None
check("self sign-in: the browser is opened ON screen at the sign-in page, and a Carry on card follows in the thread",
      shown == [True] and navigated == ["https://city.example/signin"] and cc and cc["questions"][0]["exec"]["payload"].get("self") is True
      and [o["label"] for o in cc["questions"][0]["options"]] == ["Carry on", "Stop"] and cc.get("chat_thread") == "spaces/HOME/threads/T2", repr(out))
check("self sign-in: the card says the browser is at the sign-in page and Control PC opens it (never a screen to go to)",
      "My browser is at the sign-in page; Control PC opens it" in cc["questions"][0]["question"] and "main monitor" not in cc["questions"][0]["question"])
check("self sign-in: once per card", doer._login_self_due() is None)
cb = buttons(chat_push.card_body(cc, "open"))
check("carry-on card in chat: Carry on answers the card (no vault dialog), plus Control PC",
      cb[0]["text"] == "Carry on" and cb[0]["onClick"]["action"]["function"] == "card_answer" and cb[-1]["text"] == "Control PC")
check("the stop option on a login card is just an answer: no exec minted",
      not (cards._answers_index().get(SQ) or {}).get("ticket"))

# ------------------------------------------------------------------ 7. the code card: Enter code is a dialog, the digits go into the step
spec = {"url": "https://mail.example/verify", "step": "Type the verification code mail.example sent you", "values": {}, "ask_code": True,
        "resume": "run login_fill.py --code <code> and carry on"}
(TMP / "spec.json").write_text(json.dumps(spec), encoding="utf-8")
tickets._transition(ST, "claimed", "test", "", None)
cl = report.checkpoint(ST, str(TMP / "spec.json"))
CQ = cl.split("=", 1)[1]
cc2 = cards._read(cards.LIVE / f"{CQ}.json")
cb2 = buttons(chat_push.card_body(cc2, "open"))
check("code card in chat: Enter code opens a dialog, the bare Approve is gone, No code came stays",
      cb2[0]["text"] == "Enter code" and cb2[0]["onClick"]["action"]["function"] == "card_edit" and cb2[0]["onClick"]["action"]["interaction"] == "OPEN_DIALOG"
      and "Approve" not in [b["text"] for b in cb2] and "No code came" in [b["text"] for b in cb2], json.dumps([b["text"] for b in cb2]))
cd = chat_push.edit_dialog(cc2, 0)
check("the code dialog: one Code field, a Sign in button that submits as card_edit_submit",
      cd["header"]["title"] == "Verification code" and any(w.get("textInput", {}).get("name") == "body" for w in widgets(cd))
      and buttons(cd)[0]["onClick"]["action"]["function"] == "card_edit_submit")
woken.clear()
csub = {"chat": {"user": {"email": "x@example.com"}, "buttonClickedPayload": {"message": {"name": "spaces/HOME/messages/m7", "space": {"name": "spaces/HOME"}}}},
        "commonEventObject": {"parameters": {"qid": CQ, "q": "0"}, "dialogEventType": "SUBMIT_DIALOG",
                              "formInputs": {"body": {"stringInputs": {"value": ["552 182"]}}}}, "relayFunction": "card_edit_submit"}
r = gchat_events.handle(csub)
a2 = cards._answers_index().get(CQ) or {}
ek2 = es.fold().get(a2.get("ticket")) or {}
check("code submit: the digits are minted into the step's payload (never kept on the card), the dialog closes, the doer is woken",
      "CLOSE_DIALOG" in json.dumps(r) and ek2.get("payload", {}).get("code") == "552182" and "552182" not in json.dumps(a2) and woken, json.dumps(r)[:200])
bad = dict(csub, commonEventObject=dict(csub["commonEventObject"], formInputs={"body": {"stringInputs": {"value": ["12"]}}}))
tickets._transition(ST, "claimed", "test", "", None)
cl3 = report.checkpoint(ST, str(TMP / "spec.json"))
bad["commonEventObject"]["parameters"] = {"qid": cl3.split("=", 1)[1], "q": "0"}
check("code submit: too short -> the dialog stays open with the reason", "4 to 10 digits" in json.dumps(gchat_events.handle(bad)))

# ------------------------------------------------------------------ 8. the owner's Stop closes the job's follow-ups (dream review 2026-10-17)
tickets.add_many([{"source": "t", "source_id": "stopq", "lane": "personal", "kind": "agent_run", "title": "Book the van",
                   "summary": "book the van", "payload": {"text": "Book the van for Saturday", "from": "owner", "thread": "spaces/HOME/threads/T4"}}], "test")
sp = [x for x in tickets.claim(limit=5, by="doer", kind="agent_run") if x["title"] == "Book the van"][0]
SP = sp["id"]
livecard.start(sp, "Opening the browser")
pq = report.login(SP, "vans.example", "https://vans.example/login", "FAILED the site says the login is wrong").split("=", 1)[1]
tickets._transition(SP, "staged", "doer", f"card {pq}: checkpoint, waiting for the owner's tap", {"claimed", "new"})
cards.answer(pq, report.SELF, by="owner")
check("before Stop: the hand sign-in on the van job is due", (doer._login_self_due() or [{}])[0].get("id") == SP)
queued = es.enqueue("agent_run", {"ticket": SP, "step": "Pay the van deposit", "values": {}, "resume": "press Pay"}, es.APPROVER, f"card:{pq}")
livecard.action(SP, "Stop", {"key": "owner"})
check("Stop: the job's queued approved step is cancelled and the job is marked stopped for good",
      es.fold()[queued["id"]]["status"] == "cancelled" and livecard.stopped(SP).get("by") == "owner", json.dumps(es.fold()[queued["id"]])[:200])
check("Stop: the answered hand sign-in no longer carries the job on", doer._login_self_due() is None)
check("Stop: no new action-preview card (checkpoint, sign-in, carry on) posts for the job",
      report.checkpoint(SP, str(TMP / "spec.json")).startswith("REFUSED the owner stopped")
      and report.login(SP, "vans.example", "", "NO_ENTRY").startswith("REFUSED the owner stopped")
      and report.carry_on_card(SP, {"host": "vans.example"}) == "")
livecard.start(sp, "again")
turns = []
doer.run_turn = lambda prompt, out_dir, resume="", timeout=0: turns.append(prompt) or {"ok": True, "marker": "DOER_DONE", "arg": "x", "session": ""}
buf = io.StringIO()
with contextlib.redirect_stdout(buf):
    rc = doer.run_approved({"ticket": SP, "step": "Pay the van deposit", "values": {}, "resume": "press Pay"})
check("Stop: an approved step minted after the stop runs nothing and closes quietly (exit 0, no re-card)",
      rc == 0 and not turns and "owner stopped this job" in buf.getvalue() and livecard.stopped(SP), buf.getvalue()[:200])
cut = {"ok": False, "marker": "", "error": "error_max_turns", "session": "s1"}
livecard.request_stop(SP, "owner")
check("Stop: a cut-off run does not carry on from its notes", doer.carry_on(sp, dict(cut), livecard.OUT / SP, 5)["continued"] == 0 and not turns)
tickets._transition(SP, "claimed", "test", "", None)
doer.run_ticket(sp)
check("a fresh claim of the job forgets the old Stop", not livecard.stopped(SP) and turns)

# the picture is of the page the job is on (2026-11-06, "screenshots of pages it is not working on"): the visible,
# focused tab, never just the first in Chrome's list
_pages = [{"url": "https://a.example/left", "webSocketDebuggerUrl": "ws://a"},
          {"url": "https://b.example/working", "webSocketDebuggerUrl": "ws://b"},
          {"url": "about:blank", "webSocketDebuggerUrl": "ws://c"}]
_focus = {"ws://a": (False, False), "ws://b": (True, True), "ws://c": (True, False)}
check("active page: the focused, visible tab wins over the first tab", _browser.active_page(_pages, focus=lambda ws: _focus[ws])["url"] == "https://b.example/working")
check("active page: no focus answer -> a visible tab, else the first real page; about:blank never",
      _browser.active_page(_pages, focus=lambda ws: (ws == "ws://a", False))["url"] == "https://a.example/left"
      and _browser.active_page(_pages, focus=lambda ws: (False, False))["url"] == "https://a.example/left"
      and _browser.active_page([_pages[2]])["url"] == "about:blank" and _browser.active_page([]) == {})
# in front by default, unless someone used the PC in the last five minutes (2026-11-06)
_idle = _browser.idle_seconds
_browser.idle_seconds = lambda: 12.0
check("someone at the PC in the last 5 minutes: the window stays off-screen", _browser.someone_at_the_pc() and not _browser.want_front())
_browser.idle_seconds = lambda: 900.0
check("nobody at the PC: in front is the default", not _browser.someone_at_the_pc() and _browser.want_front() and _browser.CLEAR_SCREEN_IDLE_S == 300)
_browser.idle_seconds = _idle

# blank or black pictures (2026-11-08, "control images are showing blank or black"): Windows' occlusion check made an
# off-screen window stop painting, a minimized one hung the capture, and a flat frame went on the card anyway
import struct as _st, zlib as _zl  # noqa: E402


def _png(w, h, px, ctype=2):
    bpp = 3 if ctype == 2 else 4
    rows = b"".join(b"\x00" + bytes(c for x in range(w) for c in px(x, y)[:bpp]) for y in range(h))
    ck = lambda k, d: _st.pack(">I", len(d)) + k + d + _st.pack(">I", _zl.crc32(k + d))  # noqa: E731
    return b"\x89PNG\r\n\x1a\n" + ck(b"IHDR", _st.pack(">IIBBBBB", w, h, 8, ctype, 0, 0, 0)) + ck(b"IDAT", _zl.compress(rows)) + ck(b"IEND", b"")


check("blank: an all-black frame and a flat page background are blank",
      livecard.blank(_png(40, 20, lambda x, y: (0, 0, 0, 255))) and livecard.blank(_png(40, 20, lambda x, y: (238, 242, 250, 255), ctype=6)))
check("blank: a frame with text on it is not",
      not livecard.blank(_png(40, 20, lambda x, y: (20, 20, 20) if (x // 3 + y // 2) % 4 == 0 else (250, 250, 250))))
check("blank: a picture it cannot read is never called blank", not livecard.blank(b"not a picture"))
_dis = [a for a in _browser.LAUNCH_ARGS if a.startswith("--disable-features=")]
check("browser: one --disable-features flag, and it turns off Windows' occlusion check (off-screen windows keep painting)",
      len(_dis) == 1 and "CalculateNativeWinOcclusion" in _dis[0] and "FedCm" in _dis[0])

print()
print("ALL PASSED" if not FAILS else f"{len(FAILS)} FAILED: {FAILS}", f" ({TMP})")
sys.exit(1 if FAILS else 0)

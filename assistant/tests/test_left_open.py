"""Offline tests for "a step it cannot finish is left open for the owner, with Control PC and Do later" (2026-11-08):
doer/leftopen.py, the browser's held-tab guard (browser.close_browser / prune_tabs / open_browser), doer.run_ticket and
run_approved leaving the page up, gchat_events routing the Do later tap, and doer.tick bringing a Do later back.
Scratch stores; no model, no browser, no Google.   python assistant/tests/test_left_open.py
"""
import os as _os, tempfile as _tf
_os.environ.setdefault("WV_LOG_DIR", _tf.mkdtemp(prefix="wv_logs_"))   # never the live ~/.claude logs
import io
import contextlib
import json
import os
import shutil
import sys
import tempfile
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
TMP = Path(tempfile.mkdtemp(prefix="wv_leftopen_"))
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
import tickets  # noqa: E402
import doer  # noqa: E402
import browser  # noqa: E402
import livecard  # noqa: E402
import leftopen  # noqa: E402
import chatfirst  # noqa: E402
import gchat_events  # noqa: E402

for _sid in ("lo1", "lo2", "lo3", "lo4", "lo5"):               # fixture tickets' out/doer folders from an earlier run
    shutil.rmtree(ROOT / "out" / "doer" / tickets.make_id("t", _sid), ignore_errors=True)

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


def buttons(body):
    card = body["cardsV2"][0]["card"]
    return [b for s in card["sections"] for w in s["widgets"] if "buttonList" in w for b in w["buttonList"]["buttons"]]


def params(b):
    return {p["key"]: p["value"] for p in ((b.get("onClick") or {}).get("action") or {}).get("parameters", [])}


ch = FakeChannel()
leftopen.CHANNEL = ch
livecard.CHANNEL = FakeChannel()                        # the live card has its own messages; not counted here
livecard.capture = lambda t, out_path: False
doer._mc_note = lambda text: None

# a stand-in browser: one page up at the step the job stopped on
PAGE = {"id": "TAB1", "url": "https://city.example/pay/ticket", "type": "page"}
B = {"up": True, "pages": [dict(PAGE)], "closed": [], "forced": [], "shown": 0, "front": True}
browser.active_page = lambda pages=None, focus=None: (B["pages"][-1] if B["pages"] and B["up"] else {})
browser.alive = lambda timeout_s=1.5: ({"Browser": "x"} if B["up"] else None)
browser.want_front = lambda: B["front"]
browser.show_on_screen = lambda: (B.__setitem__("shown", B["shown"] + 1) or (True, "maximized"))
_real_close = browser.close_browser
browser._cdp_browser_close = lambda: (B["closed"].append(time.time()), B.__setitem__("up", False)) and True
browser.prune_tabs = lambda keep=1: 0
browser.cdp_url = lambda: "http://127.0.0.1:1"
browser.open_browser = lambda show=None, wait_s=0: B.__setitem__("up", True) or "http://127.0.0.1:1"
leftopen._pages = lambda: (list(B["pages"]) if B["up"] else None)
leftopen._close_tab = lambda target: B["pages"].__setitem__(slice(None), [p for p in B["pages"] if p["id"] != target]) or True
leftopen._doer_busy = lambda: False


def mk(sid, text, thread):
    tickets.add_many([{"source": "t", "source_id": sid, "lane": "personal", "kind": "agent_run", "title": text[:40], "summary": text,
                       "payload": {"text": text, "from": "owner", "space": "spaces/HOME", "thread": thread}}], "test")
    return [x for x in tickets.claim(limit=9, by="doer", kind="agent_run") if x["summary"] == text][0]


# ------------------------------------------------------------------ 1. when a Do later comes back
lt = time.mktime((2026, 11, 8, 7, 0, 0, 0, 0, -1))
check("next slot: 7 AM -> tomorrow 9 AM (today's 9 is under the 3-hour wait)",
      time.localtime(leftopen.next_slot(lt))[2:4] == (9, 9))
check("next slot: 5 AM -> today 9 AM", time.localtime(leftopen.next_slot(time.mktime((2026, 11, 8, 5, 0, 0, 0, 0, -1))))[2:4] == (8, 9))
check("next slot: 10 PM -> tomorrow 9 AM", time.localtime(leftopen.next_slot(time.mktime((2026, 11, 8, 22, 0, 0, 0, 0, -1))))[2:4] == (9, 9))
check("when words: 'tomorrow at 9 AM'", leftopen.when_words(leftopen.next_slot(lt), lt) == "tomorrow at 9 AM", leftopen.when_words(leftopen.next_slot(lt), lt))

# ------------------------------------------------------------------ 2. which stops leave the page up
od = TMP / "od"
od.mkdir()
check("leave open: an owner-only step (a payment) -> yes",
      doer._leave_open({"marker": "DOER_BLOCKED", "arg": "the city site wants a card payment to finish"}, od))
check("leave open: a captcha -> yes", doer._leave_open({"marker": "DOER_BLOCKED", "arg": "a captcha blocks the form"}, od))
check("leave open: a block that is the agent's to build -> no (the build path, unchanged)",
      not doer._leave_open({"marker": "DOER_BLOCKED", "arg": "the export button does nothing"}, od))
check("leave open: stopped by the owner -> no", not doer._leave_open({"marker": "DOER_BLOCKED", "arg": "stopped by the owner"}, od))
check("leave open: done / a checkpoint -> no", not doer._leave_open({"marker": "DOER_DONE", "arg": "x"}, od)
      and not doer._leave_open({"marker": "DOER_CHECKPOINT", "arg": "q1 pay"}, od))
check("leave open: a refused handed-over login -> no (the sign-in card comes back)",
      not doer._leave_open({"marker": "DOER_BLOCKED", "arg": "x still refuses the sign-in: wrong password"}, od, login={"host": "x"}))

# ------------------------------------------------------------------ 3. run_ticket: an owner-only block keeps the browser and posts ONE message
t1 = mk("lo1", "Pay the parking ticket on the city site", "spaces/HOME/threads/T1")
T1 = t1["id"]
closed_by_doer, delivered = [], []
doer.deliver = lambda t, text: delivered.append(text)
real_doer_close = doer._close_browser
doer._close_browser = lambda t=None, since=0.0: closed_by_doer.append((t or {}).get("id"))
doer._budget_ok = lambda: True
doer._learn = lambda: type("L", (), {"after_run": staticmethod(lambda *a, **k: "")})()
doer.run_turn = lambda prompt, out_dir, resume="", timeout=0: {"ok": True, "text": "", "session": "s1", "error": "",
                                                               "marker": "DOER_BLOCKED", "arg": "the city site wants a card payment to finish"}
doer.carry_on = lambda t, r, out_dir, timeout, **k: r
out = doer.run_ticket(t1)
check("run_ticket: the browser is NOT closed at a step only the owner can take", closed_by_doer == [], repr(closed_by_doer))
check("run_ticket: ONE message posted, in the job's own thread", len(ch.posted) == 1 and ch.posted[0][0] == "spaces/HOME"
      and ch.posted[0][2] == "spaces/HOME/threads/T1", repr(ch.posted)[:300])
check("run_ticket: no second report line (deliver not called)", delivered == [], repr(delivered))
msg = ch.posted[0][1] if ch.posted else {"text": "", "cardsV2": [{"card": {"sections": []}}]}
bs = buttons(msg)
check("the message has exactly two buttons: Control PC (a link) and Do later",
      [b["text"] for b in bs] == ["Control PC", "Do later"] and "openLink" in bs[0]["onClick"], json.dumps(bs)[:300])
check("Do later rides card_answer with the job and the option (no relay change)",
      len(bs) == 2 and bs[1]["onClick"]["action"]["function"] == "card_answer" and params(bs[1]) == {"job": T1, "option": "Do later"})
check("Control PC opens the Remote Desktop link the live card uses", len(bs) == 2 and bs[0]["onClick"]["openLink"]["url"] == livecard.REMOTE_DESKTOP)
txt = msg["text"]
check("the words: why it stopped, that it is left open, both buttons, when it comes back",
      "card payment" in txt and "left it open" in txt and "Control PC" in txt and "Do later" in txt and "at 9 AM" in txt, txt)
check("the words pass the chat-first gate and speak as one (no doer/desk/operator, no ids)",
      not chatfirst.violations(txt) and not any(w in txt.lower() for w in ("doer", "desk", "operator", "builder")) and T1 not in txt, txt)
e = leftopen.entry(T1)
check("the page is held: state open, its tab and url recorded", e.get("state") == "open" and e.get("target") == "TAB1"
      and e.get("url") == PAGE["url"] and e.get("thread") == "spaces/HOME/threads/T1", json.dumps(e)[:300])
check("the page is brought in front (nobody at the PC)", B["shown"] >= 1 and e.get("in_front") is True)
check("the ticket waits (staged), it is not done", tickets._load()[T1]["status"] == "staged")

# ------------------------------------------------------------------ 4. the browser guard while a page is held
check("browser.held_tabs sees the held tab", browser.held_tabs() == {"TAB1"})
check("close_browser refuses to quit while a page is held", _real_close() is False and B["closed"] == [] and B["up"])
real_doer_close(None)
check("doer._close_browser leaves the browser up while a page is held", B["closed"] == [] and B["up"])

# ------------------------------------------------------------------ 5. a second job starting while the page is held
t2 = mk("lo2", "Look up the library opening hours", "spaces/HOME/threads/T2")
check("a new job is told to keep off the held tab", "A TAB IS HELD FOR THE OWNER" in doer._held_note(t2["id"])
      and doer._held_note(T1) == "")

# ------------------------------------------------------------------ 6. Do later: closed, held, says when
person = {"key": "owner", "name": "Owner"}
tap = gchat_events.job_click({}, {"params": {"job": T1, "option": "Do later"}}, person)
e = leftopen.entry(T1)
check("Do later (via the Chat tap): the browser quits (forced), the job is held for the next slot",
      len(B["closed"]) == 1 and not B["up"] and e.get("state") == "later" and e.get("due", 0) > time.time(), json.dumps(e)[:300])
flat = json.dumps(tap)
check("Do later: the tapped message says when it comes back and has no buttons", "bring it back" in flat and "at 9 AM" in flat
      and "buttonList" not in flat, flat[:400])
check("Do later: the ticket's note says it is held", "Do later" in str((tickets._load()[T1].get("history") or [{}])[-1].get("note")))
again = leftopen.later(T1, person)
check("Do later tapped twice: same answer, nothing closes twice", len(B["closed"]) == 1 and "bring it back" in json.dumps(again))
check("Do later on an unknown job -> {} (the tap says the job is not running)", leftopen.later("0" * 16) == {})

# ------------------------------------------------------------------ 7. it comes back at its time, with the same two buttons
n_posts = len(ch.posted)
check("sweep: not due yet -> nothing comes back", not any("brought back" in x for x in leftopen.sweep()) and len(ch.posted) == n_posts)
due = leftopen.entry(T1)["due"]
check("sweep: due but quiet hours -> nothing comes back", not any("brought back" in x for x in leftopen.sweep(quiet=True, now=due + 1))
      and len(ch.posted) == n_posts)
leftopen._open_tab = lambda url: (B["pages"].append({"id": "TAB2", "url": url, "type": "page"}) or B["pages"][-1])
B["pages"] = []
did = leftopen.sweep(now=due + 1)
e = leftopen.entry(T1)
check("sweep: due -> the page opens again and the message comes back in the same thread",
      any(x.endswith("brought back") for x in did) and len(ch.posted) == n_posts + 1 and ch.posted[-1][2] == "spaces/HOME/threads/T1", repr(did))
bs = buttons(ch.posted[-1][1])
check("the come-back message: the same two buttons and 'Back to'", [b["text"] for b in bs] == ["Control PC", "Do later"]
      and ch.posted[-1][1]["text"].startswith("Back to"), ch.posted[-1][1]["text"])
check("the come-back: held again on its new tab, offered twice", e.get("state") == "open" and e.get("target") == "TAB2" and e.get("offers") == 2)

# ------------------------------------------------------------------ 8. a held page settles by itself
B["pages"] = [{"id": "OTHER", "url": "https://x.example", "type": "page"}]
leftopen.sweep()
check("the owner closed the held tab -> settled (forgotten), the browser may close again", leftopen.entry(T1) == {} and browser.held_tabs() == set())
t3 = mk("lo3", "Renew the car registration on the state site", "spaces/HOME/threads/T3")
B["pages"], B["up"] = [{"id": "TAB3", "url": "https://dmv.example/renew", "type": "page"}], True
leftopen.offer(t3, "One step needs you: the site wants your identity check.", "identity")
B["up"] = False
leftopen.sweep()
check("the browser went down under a held page -> it waits for the next slot like a Do later",
      leftopen.entry(t3["id"]).get("state") == "later")
tickets._transition(t3["id"], "done", "test", "done by the owner", None)
leftopen.sweep()
check("a job that is over is forgotten", leftopen.entry(t3["id"]) == {})

# ------------------------------------------------------------------ 9. nothing to offer it in -> the old way (closed + said)
t4 = mk("lo4", "Pay the water bill", "")
tickets._transition(t4["id"], "claimed", "test", "", None)
t4["payload"]["space"] = "spaces/STAFF"                    # a space with staff in it: no owner-only chat
B["pages"], B["up"] = [{"id": "TAB4", "url": "https://water.example/pay", "type": "page"}], True
closed_by_doer.clear()
delivered.clear()
n_posts = len(ch.posted)
doer.run_ticket(t4)
check("no owner-only chat for the message -> the browser closes and the line is said as before",
      closed_by_doer and len(ch.posted) == n_posts and delivered and leftopen.entry(t4["id"]) == {}, repr((closed_by_doer, delivered))[:300])

# ------------------------------------------------------------------ 10. a non-owner block still closes and files its build quietly
t5 = mk("lo5", "Export the guest list from the planner site", "spaces/HOME/threads/T5")
doer._gap_for = lambda t, why: "b-1"
doer.run_turn = lambda prompt, out_dir, resume="", timeout=0: {"ok": True, "text": "", "session": "s5", "error": "",
                                                               "marker": "DOER_BLOCKED", "arg": "the export button does nothing"}
closed_by_doer.clear()
n_posts = len(ch.posted)
doer.run_ticket(t5)
check("a block that is the agent's own: browser closed, no left-open message", closed_by_doer == [t5["id"]] and len(ch.posted) == n_posts)

# ------------------------------------------------------------------ 11. switch off
leftopen.CFG = dict(leftopen.CFG, leave_open=False)
check("doer.leave_open false -> the old behaviour", not doer._leave_open({"marker": "DOER_BLOCKED", "arg": "a captcha"}, od))
leftopen.CFG = dict(leftopen.CFG, leave_open=True)

print()
print("ALL OK" if not FAILS else f"{len(FAILS)} FAILED: {FAILS}")
sys.exit(1 if FAILS else 0)

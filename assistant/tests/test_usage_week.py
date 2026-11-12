"""Offline tests for the five fixes that came out of a review of four days of real use (2026-11-07 to 11-10):

  1. a site's "tap Yes on your phone" check was reloaded and restarted until the owner's taps answered old
     prompts; the page that asked is now held open and the next step carries on in it
  2. a correction bounced off the rule limit (a card asked which rule to retire) and the fault came back two days
     later; a text an owner approved at night waited until 7 AM after they had said it must not
  3. a daily check kept running after the day it was for, the same "still open" list was said four mornings in a
     row, a stock line was tacked onto every trimmed progress note, long-cold leads each got their own card
  4. a question was answered with a one-word receipt because one sentence of the reply was a "can't"
  5. the name typed into the sign-in form was written into the transcript

Scratch stores; no model, no browser, no phone.
   python assistant/tests/test_usage_week.py
"""
import os as _os, tempfile as _tf
_os.environ.setdefault("WV_LOG_DIR", _tf.mkdtemp(prefix="wv_logs_"))
import json
import os
import sys
import tempfile
import time
from datetime import datetime
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
TMP = Path(tempfile.mkdtemp(prefix="wv_usage_"))
os.environ["ASSISTANT_DATA"] = str(TMP / "adata")
os.environ["ASSISTANT_CARDS"] = str(TMP / "cards")
os.environ["ASSISTANT_LEARNINGS"] = str(TMP / "learnings")
os.environ["DOER_DATA"] = str(TMP / "doer")
os.environ["AGENT_STATE"] = str(TMP / "agent_state")
os.environ["AGENT_DATA"] = str(TMP / "agent_data")
os.environ["AGENT_SUGGESTIONS"] = str(TMP / "sugg.md")
os.environ["CHAT_PUSH"] = "off"
os.environ["AGENT_DESK"] = "off"
for d in ("adata", "doer", "agent_state", "agent_data"):
    (TMP / d).mkdir(parents=True, exist_ok=True)
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT / "scripts" / "tracking"))
sys.path.insert(0, str(ROOT / "agent"))
sys.path.insert(0, str(ROOT / "doer"))
import capability  # noqa: E402
import chatfirst  # noqa: E402
import core  # noqa: E402
import exec_store as es  # noqa: E402
import phone_bridge as pb  # noqa: E402
import phone_nudge  # noqa: E402
import routines  # noqa: E402

FAILS = []


def check(name, cond, extra=""):
    print(("PASS " if cond else "FAIL ") + name + (f"  [{extra}]" if extra and not cond else ""))
    if not cond:
        FAILS.append(name)


NIGHT, DAY = datetime(2026, 11, 8, 21, 55), datetime(2026, 11, 8, 14, 0)

# ------------------------------------------------------------------ 2a. an approved send goes at any hour
tapped = {"approval_ref": "card:q1", "payload": {"to": "+15105550101", "body": "notes"}}
by_itself = {"approval_ref": "standing:client-texts", "payload": {"to": "+15105550101", "body": "reminder"}}
standing_card = {"approval_ref": "card:q2", "payload": {"standing": True}}
check("quiet hours do not hold what an owner approved on a card", not es.quiet_holds(tapped, NIGHT))
check("quiet hours still hold a text that goes by itself", es.quiet_holds(by_itself, NIGHT))
check("a standing send is never 'owner approved', even with a card ref", es.quiet_holds(standing_card, NIGHT))
check("nothing is held in the day", not es.quiet_holds(by_itself, DAY) and not es.quiet_holds(tapped, DAY))

pb._any_hour = lambda tid: tid == "tk-tapped"
ok1, _ = pb.queue_outbound({"to": "+15105550101", "body": "approved at night"}, "tk-tapped")
ok2, _ = pb.queue_outbound({"to": "+15105550102", "body": "goes by itself"}, "tk-standing")
pb._quiet = lambda: True
got = [i["body"] for i in pb.fetch_outbox()]
check("at night the phone is handed the approved text and only that one", ok1 and ok2 and got == ["approved at night"], str(got))
box = pb.load_json(pb.OUTBOX, {"items": []})["items"]
left = next(i for i in box if i["ticket"] == "tk-standing")
check("the other text is still waiting, and is held", left["status"] == "ready" and pb.held_by_quiet(left))
pb._quiet = lambda: False
check("it goes in the morning", [i["body"] for i in pb.fetch_outbox()] == ["goes by itself"])

# ------------------------------------------------------------------ 2b. the rule limit never loses a rule
d = core._rules()
d["rules"] = [{"id": f"r{i}", "rule": f"made-up rule number {i} about topic {i * 7919}", "why": "", "by": "owner",
               "added": "2026-09-01 08:00", "last": "2026-09-01 08:00", "seen": 1 if i == 3 else 5, "active": True}
              for i in range(1, core.MAX_RULES + 1)]
d["next_id"] = core.MAX_RULES + 1
core.save(core.RULES, d)
res = core.rule_add("When a site waits on a tap from a phone, keep that same page open until the tap comes", "an owner said so", "owner")
after = core._rules()["rules"]
act = [r for r in after if r.get("active", True)]
check("a new rule at the limit is applied, not carded or refused", res.get("status") == "applied", str(res))
check("the limit still holds", len(act) == core.MAX_RULES)
gone = [r for r in after if not r.get("active", True)]
check("the least used old rule made room", [r["id"] for r in gone] == ["r3"], str([r["id"] for r in gone]))
undo = [r for r in core.rows(core.CHANGES) if (r.get("undo") or {}).get("op") == "rule_restore" and (r.get("undo") or {}).get("id") == "r3"]
check("making room is in the ledger with its undo", len(undo) == 1)

# ------------------------------------------------------------------ 1. the page that waits on the owner is held
import browser  # noqa: E402
import report  # noqa: E402
TID, OTHER = "a" * 16, "b" * 16
page = {"id": "TAB1", "url": "https://files.example.com/verify_code", "type": "page"}
check("a phone-prompt step is a wait", report.is_wait({"step": "Open the app on your phone and tap Yes on the sign-in prompt just sent."}))
check("a code step is a wait", report.is_wait({"step": "Type the code", "ask_code": True}))
check("an ordinary last step is not", not report.is_wait({"step": "Click Submit on the change-of-address review page"}))
check("a hand-off is not (it has its own hold)", not report.is_wait({"step": "tap Yes on your phone", "handoff": True}))
e = browser.hold_wait(TID, page=page, now=1000.0)
check("the page is held", e.get("target") == "TAB1" and "TAB1" in {str(v.get("target")) for v in browser._waits().values()})
check("held_tabs carries it while its time runs", (browser.hold_wait(TID, page=page) or {}).get("target") == "TAB1" and "TAB1" in browser.held_tabs())
check("the job sees its own page", browser.waiting(TID).get("url", "").endswith("/verify_code"))
check("another job sees a held page that is not its own", list(browser.waiting(except_tid=OTHER)) == [TID] and not browser.waiting(except_tid=TID))
check("no blank page is held", browser.hold_wait(OTHER, page={"id": "T2", "url": "about:blank"}) == {})
import doer  # noqa: E402
blk = doer.same_page_block(TID)
check("the next step is told to carry on in the same page", "SAME PAGE" in blk and "verify_code" in blk and "Do NOT `browser_navigate`" in blk)
check("a job with no held page gets no such block", doer.same_page_block(OTHER) == "")
check("a hold made during this run survives the run's release", not browser.release_wait(TID, before=time.time() - 3600) and browser.waiting(TID))
check("the hold from before the run is released", browser.release_wait(TID, before=time.time() + 5) and not browser.waiting(TID))
browser.hold_wait(TID, page=page, minutes=1, now=time.time() - 600)
check("a hold nobody answered runs out", browser.expire_waits() == [TID] and "TAB1" not in browser.held_tabs())

# ------------------------------------------------------------------ 3. repeats, the stock line, stale checks
line = "The request link is done. Open a terminal and run the command."
check("the stock line is not tacked onto a trimmed note", chatfirst.clean(line) == "The request link is done.", chatfirst.clean(line))
check("it still stands in for a message that would be empty", chatfirst.clean("Open a terminal and run the command.") == chatfirst.HANDLED)
check("the browser job's cleaner does the same", doer.pc_only(line) == "The request link is done.", doer.pc_only(line))
check("a caller's own closing line is still appended", chatfirst.clean(line, "say 'set it up' here").endswith("say 'set it up' here"))

n0 = datetime(2026, 11, 2, 13, 44)
check("'Stop after Nov 7' is a last day", routines.last_day_from_words("Draft a short card asking for today's update. Stop after Nov 7.", n0) == "2026-11-07")
check("an ISO last day too", routines.last_day_from_words("check it daily until 2026-12-01", n0) == "2026-12-01")
check("no last day in the words, none made up", routines.last_day_from_words("Have the forms gone in yet?", n0) == "")
check("a routine can end itself", bool(routines._SELF_DONE.search("That is covered now.\nROUTINE_DONE")) and not routines._SELF_DONE.search("not ROUTINE_DONE yet"))
owner = next(iter(core.owners() or (core.people().get("users") or {})), "")
if owner:
    a = routines.add({"title": "Ask for an update", "when": {"daily": "09:00"}, "to": owner,
                      "prompt": "Check the text log for the latest word. Stop after Nov 7."}, by=owner, now=n0)
    row = routines.find(a.get("id")) if a.get("status") == "applied" else {}
    check("a schedule whose words name its last day gets that last day", row.get("until") == "2026-11-07", str(a))
    posts = []
    fired = routines.tick(lambda k, t: posts.append(t), now=datetime(2026, 11, 3, 9, 1),
                          run_prompt=lambda p: {"ok": True, "text": "That is covered now.\nROUTINE_DONE"})
    row = routines.find(a.get("id"), active_only=False)
    check("a check that says it is no longer needed is posted once and stopped", fired == 1 and row.get("active") is False
          and posts and "ROUTINE_DONE" not in posts[0], str((fired, row.get("active"), posts)))
else:
    check("people.json has an owner for the routine test", False)

import chains  # noqa: E402
import chase  # noqa: E402
check("a lead cold past the limit is archived with no card", chains._goes_by_itself({"why": "the step was due 104 days ago"}, None))
check("a lead only just stale still gets its one card", not chains._goes_by_itself({"why": "the step was due 61 days ago"}, None))
check("an organization can switch that off", not chains._goes_by_itself({"why": "the step was due 300 days ago"}, {"applies": {"auto_archive_days": 0}}))
st = {"items": {"a": {"status": "folded", "owner": "owner", "title": "Policy payment"}}}
ch = {"max_follow_ups": 3}
first, again, later = (chase.folded_line(st, datetime(2026, 11, 7, 7), ch), chase.folded_line(st, datetime(2026, 11, 8, 7), ch),
                       chase.folded_line(st, datetime(2026, 11, 15, 7), ch))
check("the 'still open' list is said once, not every morning", bool(first) and not again and bool(later))
st["items"]["b"] = {"status": "folded", "owner": "owner", "title": "Another one"}
check("... and again when it changes", bool(chase.folded_line(st, datetime(2026, 11, 16, 7), ch)))

# ------------------------------------------------------------------ 4. a question keeps its answer
check("the money boundary said in passing is not a gap to build",
      capability.classify("Yes, their email says tomorrow. Here are the bills. I can't move money myself.")[0] == "protected")
ask = "Are you sure the rental is due tomorrow? Also, how do you know we’re short? Can you give me a breakdown?"
full = ("Yes, their email at 4:31 PM says the order is due back tomorrow. I can't pull the bank feed from here. These are the bills "
        "I expect this week: one of 17.99 today and two more on Thursday, against the balance in each account they come out of.")
check("a question with a real answer keeps it", capability.answers_anyway(ask, full))
check("a question answered only with a can't does not", not capability.answers_anyway("Is it done?", "I can't tell from here."))
check("an ask to DO something still gets the short receipt", not capability.answers_anyway("Upload the file to the shared folder", "I can't upload from here. " + "x" * 300))

# ------------------------------------------------------------------ 5. the sign-in name is not written down
src = (ROOT / "scripts" / "gchat_events.py").read_text(encoding="utf-8")
check("the saved-login line names the site only", "saved a login for {host} in the vault" in src and "({user})" not in src)

print(f"\n{'ALL PASS' if not FAILS else str(len(FAILS)) + ' FAILED: ' + '; '.join(FAILS)}")
sys.exit(1 if FAILS else 0)

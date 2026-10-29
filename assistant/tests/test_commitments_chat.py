"""commitments.outcome: an ask already answered in its own thread is 'with the owner', never
'That draft never came back' (the example / questionnaire receipt of 2026-10-24 07:00)."""
import os as _os, tempfile as _tf
_os.environ.setdefault("WV_LOG_DIR", _tf.mkdtemp(prefix="wv_logs_"))
import json
import os
import sys
import tempfile
from datetime import datetime
from pathlib import Path

HERE = Path(__file__).resolve().parent
TMP = Path(tempfile.mkdtemp(prefix="wv_cmt_"))
os.environ["AGENT_DATA"] = str(TMP / "agent_data")
os.environ["AGENT_STATE"] = str(TMP / "agent_state")
os.environ["ASSISTANT_DATA"] = str(TMP / "adata")
os.environ["ASSISTANT_CARDS"] = str(TMP / "cards")
sys.path.insert(0, str(HERE.parent / "agent"))
import core  # noqa: E402
import commitments  # noqa: E402

FAILS = []


def check(name, cond, extra=""):
    print(("ok   " if cond else "FAIL ") + name + (f"  {extra}" if extra and not cond else ""))
    if not cond:
        FAILS.append(name)


THREAD = "spaces/AAQAtest/threads/abc"
core.DATA.mkdir(parents=True, exist_ok=True)
(core.DATA / "transcript.jsonl").write_text("\n".join(json.dumps(r) for r in [
    {"ts": "2026-10-23 22:53", "convo": "spaces/AAQAtest", "who": "owner1", "text": "Add example to example's project and send the questionnaire", "thread": THREAD},
    {"ts": "2026-10-23 22:55", "convo": "spaces/AAQAtest", "who": "Sage", "thread": THREAD,
     "text": "example is already on example's project. I can't send the questionnaire yet.\n- Tried: ...\n- Blocked because: ...\n- Need from you: the touch point link."},
    {"ts": "2026-10-23 22:56", "convo": "spaces/AAQAtest", "who": "Sage", "thread": "spaces/AAQAtest/threads/other", "text": "Need from you: something else entirely"},
]) + "\n", encoding="utf-8")

# a task hand-off whose operator ticket is still 'new' past its deadline
commitments._ticket = lambda tid: {"id": tid, "status": "new", "history": []}
it = {"id": "p-1", "kind": "task", "what": "Add example ... send the questionnaire", "ref": "tk1", "thread": THREAD,
      "created": "2026-10-23T22:55", "due": "2026-10-24T01:25", "status": "open", "reports": 0}
verdict, detail = commitments.outcome(it, datetime(2026, 10, 24, 7, 0))
check("answered in its thread -> owner, not slipped", verdict == "owner" and detail.startswith("answered in chat"), (verdict, detail))
check("the receipt line is silent for 'owner'", commitments.line_for(it, verdict, detail) == "")

# the same hand-off with no answer in that thread still slips (the other thread's line does not count)
it2 = dict(it, thread="spaces/AAQAtest/threads/nothing")
verdict, detail = commitments.outcome(it2, datetime(2026, 10, 24, 7, 0))
check("no answer in the thread -> slipped as before", verdict == "slipped", (verdict, detail))
check("...and that one still gets the 'never came back' line", "never came back" in commitments.line_for(it2, verdict, detail))

# an agent reply that is only a greeting is not an answer
(core.DATA / "transcript.jsonl").write_text(json.dumps({"ts": "2026-10-23 22:55", "who": "Sage", "thread": THREAD, "text": "On it."}) + "\n", encoding="utf-8")
verdict, _ = commitments.outcome(it, datetime(2026, 10, 24, 7, 0))
check("a bare 'On it' does not count as an answer", verdict == "slipped", verdict)

# Build request 2026-10-21: the operator's card for a thread ask was refused by the 24-hour recipient limit and it
# staged the ticket with that note; that read as "a card is up" and nothing ever said so in the thread
ask = "Post the revised example finishing-edit proposal card (feature $550, cinematic $450) here in the thread"
commitments._ticket = lambda tid: {"id": tid, "status": "staged", "history": [
    {"note": "card refused: FRESHNESS_REPEAT_RECIPIENT: this would be card #3 to ...ve.com in 24h"}]}
it3 = dict(it, what=ask, thread="spaces/AAQAtest/threads/example")
verdict, detail = commitments.outcome(it3, datetime(2026, 10, 21, 15, 0))
line = commitments.line_for(it3, verdict, detail)
check("a refused card is a failure, not 'with the owner'", verdict == "failed", (verdict, detail))
check("...and the thread gets one plain line saying the card was blocked",
      "didn't post" in line and "FRESHNESS_REPEAT_RECIPIENT" in line and "example" in line, line)
said = []
commitments._save({"items": [dict(it3, due="2026-10-21T23:00")]})
commitments._window_ok = lambda now: True
commitments.check(lambda c, th, txt: said.append((th, txt)), now=datetime(2026, 10, 21, 15, 0))
check("...at once (before the deadline), in the thread it was asked in",
      len(said) == 1 and said[0][0] == "spaces/AAQAtest/threads/example", said)
commitments._ticket = lambda tid: {"id": tid, "status": "staged", "history": [{"note": "card q0123456789ab (FRESHNESS ack: owner asked)"}]}
check("a ticket staged on its card is still 'with the owner'", commitments.outcome(it3)[0] == "owner")
commitments._ticket = lambda tid: {"id": tid, "status": "done", "history": [{"note": "sent; example refused the lower rate"}]}
check("a done task whose note only mentions someone refusing is still kept", commitments.outcome(it3)[0] == "kept")

print("\nALL PASS" if not FAILS else f"\nFAILED: {FAILS}")
sys.exit(1 if FAILS else 0)

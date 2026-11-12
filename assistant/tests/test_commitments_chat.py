"""commitments.outcome: an ask already answered in its own thread is 'with the owner', never
'That draft never came back' (the Wren / questionnaire receipt of 2026-10-24 07:00)."""
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
    {"ts": "2026-10-23 22:53", "convo": "spaces/AAQAtest", "who": "owner1", "text": "Add Wren to Teo's project and send the questionnaire", "thread": THREAD},
    {"ts": "2026-10-23 22:55", "convo": "spaces/AAQAtest", "who": "Sage", "thread": THREAD,
     "text": "Wren is already on Teo's project. I can't send the questionnaire yet.\n- Tried: ...\n- Blocked because: ...\n- Need from you: the touch point link."},
    {"ts": "2026-10-23 22:56", "convo": "spaces/AAQAtest", "who": "Sage", "thread": "spaces/AAQAtest/threads/other", "text": "Need from you: something else entirely"},
]) + "\n", encoding="utf-8")

# a task hand-off whose operator ticket is still 'new' past its deadline
commitments._ticket = lambda tid: {"id": tid, "status": "new", "history": []}
it = {"id": "p-1", "kind": "task", "what": "Add Wren ... send the questionnaire", "ref": "tk1", "thread": THREAD,
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
ask = "Post the revised Corwin finishing-edit proposal card (feature $550, cinematic $450) here in the thread"
commitments._ticket = lambda tid: {"id": tid, "status": "staged", "history": [
    {"note": "card refused: FRESHNESS_REPEAT_RECIPIENT: this would be card #3 to ...ve.com in 24h"}]}
it3 = dict(it, what=ask, thread="spaces/AAQAtest/threads/corwin")
verdict, detail = commitments.outcome(it3, datetime(2026, 10, 21, 15, 0))
line = commitments.line_for(it3, verdict, detail)
check("a refused card is a failure, not 'with the owner'", verdict == "failed", (verdict, detail))
check("...and the thread gets one plain line saying the card was blocked",
      "didn't post" in line and "FRESHNESS_REPEAT_RECIPIENT" in line and "Corwin" in line, line)
said = []
commitments._save({"items": [dict(it3, due="2026-10-21T23:00")]})
commitments._window_ok = lambda now: True
commitments.check(lambda c, th, txt: said.append((th, txt)), now=datetime(2026, 10, 21, 15, 0))
check("...at once (before the deadline), in the thread it was asked in",
      len(said) == 1 and said[0][0] == "spaces/AAQAtest/threads/corwin", said)
commitments._ticket = lambda tid: {"id": tid, "status": "staged", "history": [{"note": "card q0123456789ab (FRESHNESS ack: owner asked)"}]}
check("a ticket staged on its card is still 'with the owner'", commitments.outcome(it3)[0] == "owner")
commitments._ticket = lambda tid: {"id": tid, "status": "done", "history": [{"note": "sent; Corwin refused the lower rate"}]}
check("a done task whose note only mentions someone refusing is still kept", commitments.outcome(it3)[0] == "kept")

# Build request 2026-10-24: unkept-outcome lines never ask the owner to repeat; a task waiting on one fact names it
for kind, v in (("build", "slipped"), ("do", "slipped"), ("task", "slipped"), ("task", "failed")):
    ln = commitments.line_for(dict(it, kind=kind, reports=0), v, "still new")
    check(f"{kind} {v}: no 'say so once more' / 'ask me again'",
          ln and not any(w in ln.lower() for w in ("once more", "ask me again", "say the word")), ln)
blocked = commitments.line_for(dict(it, reports=0), "failed", "the card was blocked: DRAFT_LINT")
check("a blocked card line does not ask again either", "ask me again" not in blocked.lower(), blocked)
WAIT = "spaces/AAQAtest/threads/teo"
(core.DATA / "transcript.jsonl").write_text(json.dumps({"ts": "2026-10-23 23:40", "who": "Sage", "thread": WAIT,
    "text": "I'll send it once I have it. Need from you: the questionnaire file and the touch point link."}) + "\n", encoding="utf-8")
commitments._ticket = lambda tid: {"id": tid, "status": "new", "history": []}
it4 = dict(it, thread=WAIT, what="Teo and Wren: send the questionnaire")
verdict, detail = commitments.outcome(it4, datetime(2026, 10, 24, 7, 0))
line = commitments.line_for(it4, verdict, detail)
check("a task waiting on a fact says that one fact", verdict == "slipped" and "questionnaire file and the touch point link" in line
      and "never came back" not in line and "ask me again" not in line.lower(), (verdict, line))

# ...and a build that only asks to turn a standing job off is closed quietly when its switch is already off
sw_root = TMP / "swroot"
(sw_root / "scripts" / "yt_scout").mkdir(parents=True, exist_ok=True)
cfg = sw_root / "scripts" / "yt_scout" / "config.json"
cfg.write_text('{"enabled": false}', encoding="utf-8")
reg = TMP / "switches.json"
reg.write_text(json.dumps({"jobs": [{"key": "youtube-scout", "name": "YouTube Scout", "config": "scripts/yt_scout/config.json",
                                     "field": "enabled", "aliases": ["youtube scout", "nightly youtube"]}]}), encoding="utf-8")
import switches  # noqa: E402
switches.REGISTRY, switches.ROOT = reg, sw_root
import builder  # noqa: E402
KEY = "Wiki/Personal/Build request Turn off the nightly YouTube scout.md"
core.save(builder.STATE, {KEY: {"status": "failed", "attempts": 1}})
bit = {"id": "p-9", "kind": "build", "what": "Build request Turn off the nightly YouTube scout", "ref": KEY, "convo": "dm:owner1",
       "thread": "", "created": datetime.now().strftime(commitments.FMT), "due": "2026-10-24T08:00", "status": "open", "reports": 0}
said = []
commitments._save({"items": [bit]})
commitments.check(lambda c, th, txt: said.append(txt), now=datetime(2026, 10, 24, 8, 7))
check("switch already off: no 'Still not built' line", said == [], said)
row = commitments._load()["items"][0]
check("...the promise is closed as kept", row.get("status") == "kept", row)
check("...and the build request is dropped so the builder never takes it up",
      core.load(builder.STATE, {}).get(KEY, {}).get("status") == "dropped", core.load(builder.STATE, {}))
cfg.write_text('{"enabled": true}', encoding="utf-8")
core.save(builder.STATE, {KEY: {"status": ""}})
check("switch still on: the build is not handled", commitments.outcome(dict(bit))[0] == "slipped")
check("a longer ask that only mentions the job is not a switch ask",
      not commitments._switch_handled(dict(bit, what="Turn off youtube scout ideas cards in the digest")))

print("\nALL PASS" if not FAILS else f"\nFAILED: {FAILS}")
sys.exit(1 if FAILS else 0)

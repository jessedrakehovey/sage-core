"""test_digest_trim.py - the Monday digest trim (the owner 2026-11-09: 96 cards in ten messages, four in five the system's own
backlog). A stalled ticket's "Waiting Nd" card is a system card (Mission Control only); the digest has a ceiling of
max_messages and counts the overflow as shown; a date in a text needs an event word before it becomes a hold card; a
calendar hold may carry an owner's Always; another module's proposal becomes an idea line, said once.
   python assistant/tests/test_digest_trim.py
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
TMP = Path(tempfile.mkdtemp(prefix="wv_trim_"))
os.environ["ASSISTANT_DATA"] = str(TMP)
os.environ["ASSISTANT_CARDS"] = str(TMP / "cards")
os.environ["AGENT_DATA"] = str(TMP / "agent")
os.environ["AGENT_STATE"] = str(TMP / "state")
os.environ["AGENT_SUGGESTIONS"] = str(TMP / "skill-suggestions.md")
os.environ["ASSISTANT_LEARNINGS"] = str(TMP / "learnings")
os.environ["SKILL_RADAR_CANDIDATES"] = str(TMP / "radar.json")
os.environ["RULINGS_FILE"] = str(ROOT / "data" / "rulings.json")
os.environ["AGENT_RULES_FILE"] = str(TMP / "state" / "rules.json")
os.environ["ASSISTANT_CARDS_DELIVERY"] = "weekly"
os.environ["CHAT_PUSH"] = "off"
os.environ["WV_NO_TRIAGE"] = "1"
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT / "scripts" / "tracking"))
sys.path.insert(0, str(ROOT / "agent"))
import cards  # noqa: E402
import digest  # noqa: E402
import ideas  # noqa: E402
import policy  # noqa: E402

FAILS = []


def check(name, cond, extra=""):
    print(("ok   " if cond else "FAIL ") + name + (f"  {extra}" if extra and not cond else ""))
    if not cond:
        FAILS.append(name)


# ---------------------------------------------------------------- 1. a Waiting card is the system's own
check("'Waiting 12d: ...' is a system card", cards.is_system({"header": "Waiting 12d: 4 stuck item(s) need a look"}))
check("'Waiting 5d: owner1 asked: ...' too", cards.is_system({"header": "Waiting 5d: owner1 asked: Draft a contract"}))
check("a lead's next step is not", not cards.is_system({"header": "Next step · Lead · Someone"}))
r = cards.post([{"question": "This has waited 12 days with nothing since: 4 stuck items.", "header": "Waiting 12d: 4 stuck item(s) need a look",
                 "lane": "personal", "options": [{"label": "Hand it to Sage (Recommended)"}, {"label": "Close it"}]}], "sentinels")
check("posted as Mission Control only, never into the digest", r["posted"] and r.get("delivery") == "mc", str(r))

# ---------------------------------------------------------------- 2. the digest ceiling
for i in range(25):
    cards.post([{"question": f"Decision number {i} about thing {i}: file it under the right folder?", "header": f"Decision {i}", "lane": "personal",
                 "subject": f"thing-{i}", "options": [{"label": "File it (Recommended)"}, {"label": "Skip"}]}], "operator")
held = digest.held()
check("25 held cards", len(held) == 25, str(len(held)))
import chat_push  # noqa: E402
chat_push.targets = lambda lane="", kind="card": [("owner1", "spaces/TEST")]
msgs = digest.messages(datetime(2026, 11, 9, 8, 0))
check("at most two messages (max_per_message 10 -> 20 cards shown)", len(msgs) == 2 and sum(len(m["qids"]) for m in msgs) == 20, str([len(m["qids"]) for m in msgs]))
check("the last message names the overflow with the Mission Control link", len(msgs[-1]["overflow"]) == 5 and "5 more wait on Mission Control" in msgs[-1]["subtitle"], msgs[-1]["subtitle"])
check("the first message has no overflow list", msgs[0]["overflow"] == [])


class _G:
    def __init__(self):
        self.n = 0

    def post_body(self, space, body, thread=""):
        self.n += 1
        return f"spaces/TEST/messages/m{self.n}"

    def delete(self, name):
        return True


chat_push.enabled = lambda: True
chat_push._g = lambda: _G()
chat_push._stack_remember = lambda *a, **k: None
digest.cards.say = lambda *a, **k: {"said": True}
res = digest.post(datetime(2026, 11, 9, 8, 1), force=True)
check("post sends two messages for 25 cards", res["posted"] and res["messages"] == 2, str(res))
shown = [c for c in cards._live() if c.get("status") == "open" and int(c.get("digest_count") or 0) >= 1]
over = [c for c in shown if c.get("digest_overflow")]
check("every held card counts as shown once, the five past the ceiling flagged", len(shown) == 25 and len(over) == 5, f"{len(shown)} {len(over)}")

# ---------------------------------------------------------------- 3. a date needs an event
import mining  # noqa: E402
check("'surgery is scheduled for 11:30 tomorrow' names an event", mining.has_event_word("The surgery is scheduled for 11:30 tomorrow"))
check("'driving down to LA tonight ... hospital' is a trip (arriving / in town) only when it says so", not mining.has_event_word("They are driving down to LA tonight and will be there for a few days"))
check("'Please join me in praying for' is not an event", not mining.has_event_word("Please join me in praying for:"))
check("'But I'm planning on getting a very good night's sleep tonight' is not", not mining.has_event_word("But I'm planning on getting a very good night's sleep tonight."))
check("the organization may add words; require_event_word false turns the bar off",
      mining.has_event_word("the bouquet toss is at 9", {"words": r"\bbouquet toss\b"}) and mining.has_event_word("anything", {"require_event_word": False}))

# ---------------------------------------------------------------- 4. a hold can carry an Always
hold = {"header": "Date from Emily", "question": "Emily (text): \"The surgery is at 11:30 tomorrow.\" Put a hold on the calendar? Nobody is notified.",
        "lane": "personal", "options": [{"label": "Add to calendar (Recommended)"}, {"label": "Skip"}],
        "exec": {"kind": "calendar_hold", "approve_label": "Add to calendar (Recommended)", "payload": {"title": "Emily: surgery", "date": "2026-11-10"}}}
check("calendar_hold is no longer forever gated", not policy.forever_gated("calendar_hold") and "always" in policy.grant_scopes(hold, "personal"))
check("a send still is", policy.forever_gated("email_send") and policy.grant_scopes({"header": "Email to X", "question": "x", "exec": {"kind": "email_send"}}) == ["once"])

# ---------------------------------------------------------------- 5. proposals become idea lines
i1 = ideas.suggest("From a 51-minute video: make the review request ask every client plainly.", "youtube_scout", key="Wiki/x.md", say="build the review request idea")
check("suggest records an external idea with the phrase to act on it", i1["new"] and "Say 'build the review request idea'" in i1["text"])
i2 = ideas.suggest("From a 51-minute video: make the review request ask every client plainly.", "youtube_scout", key="Wiki/x.md", say="build the review request idea")
check("the same proposal twice is one idea", not i2["new"] and i2["id"] == i1["id"])
f = ideas.fresh()
check("it is fresh once", any(x["id"] == i1["id"] for x in f))
ideas.mark(f)
check("and never again", not any(x["id"] == i1["id"] for x in ideas.fresh()))
check("render numbers several ideas as lines", "1." in ideas.render([i1, {"id": "i2", "text": "two", "source": "x"}]))

print(("ALL OK" if not FAILS else f"{len(FAILS)} FAILED: {FAILS}") + f"  (scratch {TMP})")
sys.exit(1 if FAILS else 0)

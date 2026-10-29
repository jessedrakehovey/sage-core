"""test_chat_stack.py - offline checks for the stacked card in Google Chat (chat_push.stack_body
and friends) and for cards landing in the thread they were asked in. Scratch store, a fake Chat
client, no network.   python assistant/tests/test_chat_stack.py

the owner 2026-10-17: "If I ever get multiple cards stacking up on each other in chat delete the last
one and consolidate both of them or all of them in one card with multiple decisions so I can click
a drop down to see them all" ... "like how example does it"; and "When I give it feedback like I did
for Justin I want the new card to come in the same thread."
"""
import os as _os, tempfile as _tf
_os.environ.setdefault("WV_LOG_DIR", _tf.mkdtemp(prefix="wv_logs_"))
import json
import os
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
TMP = Path(tempfile.mkdtemp(prefix="wv_stack_"))
os.environ["ASSISTANT_DATA"] = str(TMP)
os.environ["ASSISTANT_CARDS"] = str(TMP / "cards")
os.environ["ASSISTANT_CARDS_DELIVERY"] = "now"     # the mid-week stack: the pre-digest delivery (digest tests: test_cards_digest.py)
os.environ["AGENT_DATA"] = str(TMP / "agent")
os.environ["AGENT_STATE"] = str(TMP / "state")
os.environ["RULINGS_FILE"] = str(ROOT / "data" / "rulings.json")
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT / "agent"))
sys.path.insert(0, str(ROOT / "doer"))
import cards  # noqa: E402
import chat_push  # noqa: E402

SPACE = "spaces/TEST"
FAILS = []


def check(name, cond, extra=""):
    print(("ok   " if cond else "FAIL ") + name + (f"  {extra}" if extra and not cond else ""))
    if not cond:
        FAILS.append(name)


class FakeChat:
    def __init__(self):
        self.n, self.posted, self.patched, self.deleted = 0, [], [], []

    def post_body(self, space, body, thread=""):
        self.n += 1
        name = f"{space}/messages/M{self.n}.M{self.n}"
        self.posted.append((name, body, thread))
        return name

    def patch_body(self, name, body):
        self.patched.append((name, body))
        return True

    def delete(self, name):
        self.deleted.append(name)
        return True

    def post(self, space, text, thread=""):
        return self.post_body(space, {"text": text}, thread)


fc = FakeChat()
chat_push.enabled = lambda: True                  # the scratch store normally switches Chat off
chat_push._g = lambda: fc
chat_push._home_space = lambda: SPACE
chat_push.targets = lambda lane="", kind="card": [("home", SPACE)]
chat_push.record_card = lambda card: 0           # the agent transcript is not under test here
chat_push.STACKS = TMP / "agent" / "chat_stacks.json"


def q(header, question, lane="personal"):
    return {"question": question, "header": header, "lane": lane,
            "options": [{"label": "Yes (Recommended)", "description": "why"}, {"label": "No"}]}


def sections_of(body):
    return body["cardsV2"][0]["card"]["sections"]


# 1. one card alone posts as itself
r1 = cards.post([q("Mechanic", "Pick the car up today?")], "operator", lane="personal")
check("first card posts alone", r1["posted"] and len(fc.posted) == 1 and fc.posted[-1][1]["cardsV2"][0]["cardId"] == r1["qid"])
m1 = fc.posted[-1][0]

def heads_of(body):
    """The bold head line of each decision inside the one fold, in order."""
    import re
    out = []
    for w in sections_of(body)[0]["widgets"][1:]:
        t = (w.get("decoratedText") or {}).get("text") or ""
        m = re.match(r"<b>(.*?)</b>", t)
        if m and "<font" in t:
            out.append(m.group(1))
    return out


def inside_of(body):
    return sections_of(body)[0]["widgets"][0]["decoratedText"]["text"]


# 2. a second open card folds both into ONE stack message with ONE drop-down; the first message comes down
r2 = cards.post([q("Justin", "Text Justin about the returned money?")], "operator", lane="personal")
body2 = fc.posted[-1][1]
check("second card takes the first message down", m1 in fc.deleted)
sec = sections_of(body2)
check("one stack message, ONE collapsible section for the whole card (example's shape)",
      body2["cardsV2"][0]["cardId"] == "stack" and len(sec) == 1 and sec[0].get("collapsible")
      and sec[0].get("uncollapsibleWidgetsCount") == 1)
check("stack header counts the decisions", body2["cardsV2"][0]["card"]["header"]["title"].startswith("2 decisions waiting"))
check("what is inside stays visible while collapsed, one line per lane",
      inside_of(body2).startswith("<b>Personal</b> Mechanic, Justin"))
check("every decision sits under its own head line inside the fold", heads_of(body2) == ["Mechanic", "Justin"])
check("a divider between decisions", sum(1 for w in sec[0]["widgets"] if "divider" in w) == 1)
buttons = json.dumps(body2)
check("every button still answers its own card", r1["qid"] in buttons and r2["qid"] in buttons)
stack_msg = fc.posted[-1][0]
c1 = cards._read(cards.LIVE / f"{r1['qid']}.json")
c2 = cards._read(cards.LIVE / f"{r2['qid']}.json")
check("both cards now point at the stack message", c1["chat"]["home"] == stack_msg and c2["chat"]["home"] == stack_msg)
check("the stack is remembered by its message", (chat_push._stacks().get(stack_msg) or {}).get("qids") == [r1["qid"], r2["qid"]])
check("phone text names what is inside", "Mechanic" in body2["text"] and "Justin" in body2["text"])

# 3. answering one card re-renders the whole stack: a tick on that head line, buttons left on the other
r = cards.answer(r1["qid"], "Yes (Recommended)", by="owner1")
check("answer recorded", r["ok"])
name, body3 = fc.patched[-1]
heads = heads_of(body3)
check("the stack message is patched, not replaced by one card",
      name == stack_msg and body3["cardsV2"][0]["cardId"] == "stack" and len(sections_of(body3)) == 1 and len(heads) == 2)
check("the answered card shows its outcome on its head line, open one first",
      heads[0] == "Justin" and heads[1].startswith("✅ Mechanic") and "Yes" in heads[1])
check("the visible summary line shows the tick too", "✅ Mechanic" in inside_of(body3))
check("the title says what is left", body3["cardsV2"][0]["card"]["header"]["title"] == "1 decision waiting · 1 done")
check("the open card keeps its buttons", r2["qid"] in json.dumps(body3))

# 4. a third card: the stack message comes down, a new stack carries the open ones plus the new one
n_del = len(fc.deleted)
r3 = cards.post([q("Insurance", "Check the GL policy on the insurer's site?", lane="money")], "operator", lane="money")
body4 = fc.posted[-1][1]
check("old stack message taken down", stack_msg in fc.deleted[n_del:])
check("new stack holds the open cards (the answered one is no longer open)", heads_of(body4) == ["Justin", "Insurance"])
check("lanes in the subtitle and one inside line per lane",
      "Personal" in body4["cardsV2"][0]["card"]["header"]["subtitle"] and "Money" in body4["cardsV2"][0]["card"]["header"]["subtitle"]
      and "<b>Money</b> Insurance" in inside_of(body4))
check("the old stack is forgotten once its cards moved on", stack_msg not in chat_push._stacks())

# 4b. two stacks in one space are both remembered (the 2026-10-17 restack made four)
chat_push._stack_remember(SPACE, f"{SPACE}/messages/OTHER.OTHER", ["qzzzzzzzzzzzz"])
check("stacks are keyed by message, several per space", len([k for k, v in chat_push._stacks().items() if v.get("space") == SPACE]) == 2)

# 5. a card asked for in a thread lands in that thread and is never stacked (the Justin ask)
n_posts = len(fc.posted)
r4 = cards.post([q("Justin's number", "Send me his number?")], "operator", lane="personal", thread=f"{SPACE}/threads/ASK")
check("a threaded card posts on its own, into the thread",
      len(fc.posted) == n_posts + 1 and fc.posted[-1][2] == f"{SPACE}/threads/ASK" and fc.posted[-1][1]["cardsV2"][0]["cardId"] == r4["qid"])
check("the threaded card did not join the stack", not any(r4["qid"] in (v.get("qids") or []) for v in chat_push._stacks().values()))

# 6. the switch
chat_push._cfg = lambda: {"fold_multi": False}
r5 = cards.post([q("Solo", "Fold off?")], "operator", lane="personal")
check("fold_multi off posts cards one by one", fc.posted[-1][1]["cardsV2"][0]["cardId"] == r5["qid"])
chat_push._cfg = lambda: {}

# 6b. the cap: a stack never holds more than MAX_STACK cards (Chat caps a card's size); the oldest stay put
for i in range(10):
    cards.post([q(f"Bulk {i}", f"Decision number {i}?")], "operator", lane="personal")
body6 = fc.posted[-1][1]
stack_bodies = [b for _, b, _ in fc.posted if b.get("cardsV2") and b["cardsV2"][0]["cardId"] == "stack"]
check("no stack ever exceeds MAX_STACK members", all(len(heads_of(b)) <= chat_push.MAX_STACK for b in stack_bodies))
# 7. a decided card leaves the drop-down (the owner 2026-10-17 evening: "a dropdown with every decision I ever
# made"): the card just tapped shows its outcome (Undo stays), every other decided card is gone on the
# next re-render, one left = the plain card
chat_push._cfg = lambda: {}
SPACE2 = "spaces/PAIR"
chat_push._home_space = lambda: SPACE2
chat_push.targets = lambda lane="", kind="card": [("home", SPACE2)]
cards.UNDO_S = 0
ra = cards.post([q("Alpha", "First of the pair?")], "operator", lane="home")
rb = cards.post([q("Beta", "Second of the pair?")], "operator", lane="home")
pair_msg = fc.posted[-1][0]
cards.answer(ra["qid"], "Yes (Recommended)", by="owner1")
_, b1 = fc.patched[-1]
check("just tapped: the answered card still shows in the stack with its tick", heads_of(b1) and heads_of(b1)[-1].startswith("✅ Alpha"))
cards.sweep()                                    # Alpha harvested: no longer open
cards.answer(rb["qid"], "No", by="owner1")
name2, b2 = fc.patched[-1]
check("next re-render: the earlier decision is gone; one left renders as the plain card, not a fold",
      name2 == pair_msg and b2["cardsV2"][0]["cardId"] == rb["qid"] and "Alpha" not in json.dumps(b2))
check("the stack forgets its decided members", not (chat_push._stacks().get(pair_msg) or {}).get("qids"))
cards.UNDO_S = 30                                # the undo window is open again
cards.undo(rb["qid"])
_, b3 = fc.patched[-1]
check("undo re-renders the lone card as open again", b3["cardsV2"][0]["cardId"] == rb["qid"] and "choose again" in json.dumps(b3))
cards.UNDO_S = 30
chat_push._home_space = lambda: SPACE
chat_push.targets = lambda lane="", kind="card": [("home", SPACE)]

check("a full stack stays whole; the overflow starts a new stack instead of splitting it",
      heads_of(body6) == ["Bulk 5", "Bulk 6", "Bulk 7", "Bulk 8", "Bulk 9"]      # Justin, Insurance, Solo + Bulk 0-4 filled the first
      and any(heads_of(b)[:2] == ["Justin", "Insurance"] and len(heads_of(b)) == chat_push.MAX_STACK for b in stack_bodies))

# 7. the operator's CLI passes the thread through; the agent's task tickets carry it
import subprocess
f = TMP / "card.json"
f.write_text(json.dumps([q("CLI", "From the operator?")]), encoding="utf-8")
env = dict(os.environ, CHAT_PUSH="off")
out = subprocess.run([sys.executable, str(ROOT / "scripts" / "cards.py"), "post", "--file", str(f), "--owner", "operator",
                      "--lane", "personal", "--thread", f"{SPACE}/threads/CLI"], capture_output=True, text=True, env=env, encoding="utf-8")
res = json.loads(out.stdout.strip().splitlines()[-1]) if out.stdout.strip() else {}
cli_card = cards._read(cards.LIVE / f"{res.get('qid', 'none')}.json") or {}
check("cards.py post --thread lands the card in the thread", res.get("posted") and cli_card.get("chat_thread") == f"{SPACE}/threads/CLI", out.stdout + out.stderr)

# 8. the doer says WHICH job it could not do
import doer
t = {"id": "t1", "payload": {"text": "Rent a lens for Nov 7 from lensrentals, the 35mm\n\nHOW WE DID THIS BEFORE: ..."}}
line = doer._say("owner1", "DOER_BLOCKED", "the site lists many 35mm lenses", t)
check("a blocked doer names the job", line.startswith('I could not finish on "Rent a lens for Nov 7 from lensrentals, the 35mm": the site'), line)
check("no job text, no empty quotes", doer._say("owner1", "DOER_BLOCKED", "x", {}) == "I could not finish: x.")   # portal() ends the reason with a period (2026-10-20)
check("a long job is cut with an ellipsis", doer.job_line({"payload": {"text": "a " * 200}}).endswith("…\""))

print()
print("ALL PASSED" if not FAILS else f"{len(FAILS)} FAILED: {FAILS}", f"({TMP})")
sys.exit(1 if FAILS else 0)

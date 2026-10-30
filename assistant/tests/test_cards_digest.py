"""test_cards_digest.py - offline checks for the weekly approvals digest (the owner 2026-10-20):
every non-urgent card is HELD (open on Mission Control, nothing in Chat) until ONE drop-down message
a week; the new-lead text and anything "urgent" goes to Chat at once, on its own, never stacked; a
decision on a digest card is final; a card whose action was not confirmed done comes back by itself.
Scratch store, a fake Chat client, no network.   python assistant/tests/test_cards_digest.py
"""
import os as _os, tempfile as _tf
_os.environ.setdefault("WV_LOG_DIR", _tf.mkdtemp(prefix="wv_logs_"))
import json
import os
import sys
import tempfile
import time
from datetime import datetime, timedelta
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
TMP = Path(tempfile.mkdtemp(prefix="wv_digest_"))
os.environ["ASSISTANT_DATA"] = str(TMP)
os.environ["ASSISTANT_CARDS"] = str(TMP / "cards")
os.environ["AGENT_DATA"] = str(TMP / "agent")
os.environ["AGENT_STATE"] = str(TMP / "state")
os.environ["RULINGS_FILE"] = str(ROOT / "data" / "rulings.json")
os.environ["ASSISTANT_CARDS_DELIVERY"] = "weekly"
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT / "agent"))
sys.path.insert(0, str(ROOT / "doer"))
import cards  # noqa: E402
import chat_push  # noqa: E402
import digest  # noqa: E402
import exec_store as es  # noqa: E402

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
        self.n += 1
        return f"{space}/messages/T{self.n}.T{self.n}"


G = FakeChat()
chat_push._g = lambda: G
chat_push.enabled = lambda: True
chat_push.targets = lambda lane="", kind="card": [("owner1", SPACE)]
chat_push.unreachable = lambda lane="", kind="card": []
chat_push.record_card = lambda card: 1
SAID = []
chat_push.push_say = lambda text, lane="", thread="": (SAID.append((lane, text)), 1)[1]
cards.UNDO_S = 0


def post(header, q, exec_kind="", urgent=None, thread="", opts=None, lane="personal", owner="operator"):
    d = {"header": header, "lane": lane, "question": q,
         "options": opts or [{"label": "Do it (Recommended)", "description": "the system does it"}, {"label": "Skip"}]}
    if exec_kind:
        d["exec"] = {"kind": exec_kind, "approve_label": "Send (Recommended)",
                     "payload": {"from": "owner3@example.invalid", "to": ["x@example.com"], "subject": "s", "body": q}}
        d["options"] = [{"label": "Send (Recommended)"}, {"label": "Skip"}]
        d["question"] = "From owner3@example.invalid to x@example.com, subject s:\n" + q
    if urgent is not None:
        d["urgent"] = urgent
    return cards.post([d], owner, lane=lane, thread=thread)


# ------------------------------------------------------------------ 1. policy
pol = cards.delivery_policy()
check("policy: weekly from the env, a day + hour, prefixes include 'New lead'", pol["delivery"] == "weekly" and 0 <= pol["day"] <= 6
      and "New lead" in pol["prefixes"] and pol["max_per_message"] >= 2, pol)
check("mid-week stacking is off under the weekly digest", chat_push.fold_multi_on() is False)

# ------------------------------------------------------------------ 2. held vs urgent
r1 = post("Vendor reply: Ambrose", "Reply to Ambrose about turnaround.")
c1 = cards._read(cards.LIVE / f"{r1['qid']}.json")
check("a normal card is HELD: posted, open, delivery weekly, NOT in Chat, advice says so",
      r1.get("posted") and r1.get("delivery") == "weekly" and c1.get("delivery") == "weekly" and c1.get("status") == "open"
      and not c1.get("chat") and not G.posted and any("HELD" in a for a in r1.get("advice") or []), (r1, len(G.posted)))
check("pending() marks it held", any(p["qid"] == r1["qid"] and p["held"] and not p["urgent"] for p in cards.pending()))

r2 = post("New lead: Odette & Linden", "New lead with a phone number. Text them?\n[TEXT PREVIEW] To +15550470647:\nHello Odette!", urgent=True)
c2 = cards._read(cards.LIVE / f"{r2['qid']}.json")
check("an urgent card goes to Chat at once, on its own (no stack), delivery now",
      r2.get("posted") and r2.get("delivery") == "now" and c2.get("urgent") is True and len(G.posted) == 1
      and c2.get("chat") == {"owner1": G.posted[0][0]} and G.posted[0][1]["cardsV2"][0]["cardId"] != "stack", (r2, len(G.posted)))

r3 = post("New lead: Pat", "A header that starts with the prefix, no flag.")
check("the 'New lead' header alone is urgent", cards._read(cards.LIVE / f"{r3['qid']}.json").get("urgent") is True and len(G.posted) == 2)

r4 = post("Reply to Jon", "Asked for in a chat thread.", exec_kind="email_send", thread="spaces/TEST/threads/T9")
check("a card asked for in a chat thread is not held: it lands in that thread now",
      r4.get("delivery") == "now" and len(G.posted) == 3 and G.posted[-1][2] == "spaces/TEST/threads/T9", (r4, G.posted[-1][2] if G.posted else None))

r5 = post("Checkin: Teo", "The standard check-in text goes at 1:00 PM by itself. Tap Hold.", opts=[{"label": "Hold this one (Recommended)"}, {"label": "Let it go as planned"}])
cq = cards._read(cards.LIVE / f"{r5['qid']}.json")
check("a plain decision card is held even with the hold-shaped buttons (no hold_text key)", cq.get("delivery") == "weekly")
r5b = cards.post([{"header": "Checkin: Teo 2", "lane": "wedding", "hold_text": "checkin|1", "no_recommendation": "goes by itself",
                   "question": "The standard check-in text goes at 1:00 PM by itself. Tap Hold.",
                   "options": [{"label": "Hold this one"}, {"label": "Let it go as planned"}]}], "client_texts", lane="wedding")
check("a standing text's preview (hold_text) is time-bound: Chat now", r5b.get("delivery") == "now" and len(G.posted) == 4, r5b)
two_urgent = [p for p in G.posted if p[1]["cardsV2"][0]["cardId"] == "stack"]
check("urgent cards never fold into a stack between digests", not two_urgent)

# ------------------------------------------------------------------ 3. held cards never nag, never expire early
import sentinels  # noqa: E402
found = [f for f in sentinels.collect() if f["key"].startswith("card:")]
c1["ts"] = c1["held_ts"] = time.time() - 5 * 86400
cards._write(cards.LIVE / f"{r1['qid']}.json", c1)
found = [f for f in sentinels.collect() if f["key"] == f"card:{r1['qid']}"]
check("a held card 5 days old is not 'stale' to the sentinel", not found)
check("expire() leaves a held card that had no digest yet", cards.expire() == 0 and (cards.LIVE / f"{r1['qid']}.json").exists())

# ------------------------------------------------------------------ 4. the slot
pol = dict(pol, day=0, hour=8)        # Monday 08:00
cards.delivery_policy = lambda: pol
mon = datetime(2026, 10, 19, 8, 0)    # a Monday
digest._save({"last_slot": digest.slot(mon - timedelta(minutes=1), pol).isoformat(timespec="minutes")})   # last week's digest went out
check("slot: Monday 07:59 -> last Monday; 08:00 -> today; Wednesday -> Monday", digest.slot(mon - timedelta(minutes=1), pol) == mon - timedelta(days=7)
      and digest.slot(mon, pol) == mon and digest.slot(mon + timedelta(days=2, hours=3), pol) == mon)
check("next slot is a week on", digest.next_slot(mon + timedelta(days=2), pol) == mon + timedelta(days=7))
check("due until posted for that slot", digest.due(mon + timedelta(hours=1), pol) is True)
r_dry = digest.post(mon + timedelta(hours=1), dry=True)
check("dry run counts, posts nothing, consumes no slot", r_dry.get("dry") and r_dry["cards"] == 2 and not r_dry.get("posted") and digest.due(mon + timedelta(hours=1), pol))
r_nd = digest.post(mon - timedelta(hours=1))
check("not due -> nothing", not r_nd.get("posted") and r_nd.get("reason") == "not due", r_nd)

# ------------------------------------------------------------------ 5. the digest message
# six held cards across lanes (two already there) -> one message
for i in range(4):
    post(f"Decision {i}", f"Question number {i}.", lane=("wedding" if i % 2 else "money"))
n_posted = len(G.posted)
r = digest.post(mon + timedelta(hours=1))
check("posted once for the slot: 6 cards, 1 message", r.get("posted") and r["cards"] == 6 and r["messages"] == 1 and len(G.posted) == n_posted + 1, r)
name, body, thread = G.posted[-1]
card = body["cardsV2"][0]["card"]
sec = card["sections"][0]
check("ONE collapsible drop-down, 6 waiting in the title, the lead line visible while collapsed, next digest in the subtitle",
      sec.get("collapsible") is True and sec.get("uncollapsibleWidgetsCount") == 1 and "6 waiting" in card["header"]["title"]
      and "Decide each one once" in sec["widgets"][0]["decoratedText"]["text"] and "next digest" in card["header"]["subtitle"], card["header"])
buttons = [w for w in sec["widgets"] if "buttonList" in w]
check("every member keeps its own buttons", len(buttons) == 6, len(buttons))
heads = [w["decoratedText"]["text"] for w in sec["widgets"] if "decoratedText" in w and "since" in w["decoratedText"]["text"]]
check("each member says how long it has waited", len(heads) == 6, len(heads))
held_now = [c for c in cards._live() if c.get("delivery") == "weekly"]
check("every held card now points at the digest message, digest_count 1, stack remembered",
      all(c.get("chat") == {"owner1": name} and c.get("digest_count") == 1 and c.get("digest_ts") for c in held_now)
      and chat_push.stack_of(held_now[0]).get("msg") == name and len(chat_push.stack_of(held_now[0]).get("qids") or []) == 6)
check("the slot is consumed: not due any more", not digest.due(mon + timedelta(hours=2), pol))
check("the ledger has the digest", any(e.get("event") == "card_digest" for e in es.tail(20)) if hasattr(es, "tail") else True)

# a tap on a member: answered through the guarded path, the fold re-renders without it
qid0 = held_now[0]["id"]
a = cards.answer(qid0, "Do it (Recommended)", by="test")
check("a digest member answers like any card", a.get("ok"), a)
body2 = chat_push.body_for(cards._read(cards.LIVE / f"{qid0}.json"), "answered", {"option": "Do it (Recommended)"})
t2 = body2["cardsV2"][0]["card"]["header"]["title"]
check("re-rendered: 5 waiting, 1 done; the tapped one keeps its Undo", "5 waiting" in t2 and "1 done" in t2, t2)
sw = cards.sweep()
q0 = held_now[0]["questions"][0]
check("the sweep harvests it; nothing re-asks it (ALREADY_ANSWERED)", sw["queued"] >= 1
      and "ALREADY_ANSWERED" in str(post(q0["header"], q0["question"], lane=q0["lane"]).get("reason")))

# ------------------------------------------------------------------ 6. carry-over then drop
old = next(c for c in cards._live() if c.get("delivery") == "weekly")
old["digest_ts"] = time.time() - 8 * 86400
cards._write(cards.LIVE / f"{old['id']}.json", old)
check("one digest, a week on: still alive (carry_over_weeks = 1)", cards.expire() == 0)
# the next digest a week later: the same cards again (the old message comes down), the carried one marked
n_posted = len(G.posted)
r = digest.post(mon + timedelta(days=7, hours=1))
check("next week's digest posts again, takes last week's message down", r.get("posted") and G.deleted[-1] == name and len(G.posted) == n_posted + 1, (r, G.deleted))
sec2 = G.posted[-1][1]["cardsV2"][0]["card"]["sections"][0]
check("a second-week member says it drops after this", any("2nd week" in w.get("decoratedText", {}).get("text", "") for w in sec2["widgets"]))
old = cards._read(cards.LIVE / f"{old['id']}.json")
old["digest_ts"] = time.time() - 8 * 86400
cards._write(cards.LIVE / f"{old['id']}.json", old)
n = cards.expire()
check("two digests unanswered, a week on: dropped (expired)", n == 1 and (cards.DONE / f"{old['id']}.json").exists() and not (cards.LIVE / f"{old['id']}.json").exists(), n)
import tickets  # noqa: E402
cards.sweep()
tk = [t for t in tickets._load().values() if t.get("kind") == "card_expired" and (t.get("payload") or {}).get("qid") == old["id"]]
check("...and no card_expired ticket re-posts it", not tk)

# ------------------------------------------------------------------ 7. "unless they are not confirmed done": reopen
re1 = post("Reply to staff1", "Thanks staff1, the payment lands Friday.", exec_kind="email_send", lane="wedding")
qid = re1["qid"]
cards.answer(qid, "Send (Recommended)", by="test")
h = cards.harvest()
tk_id = next((x.get("ticket") for x in h if x["qid"] == qid), "")
check("approved: consumed, ticket minted", (cards.DONE / f"{qid}.json").exists() and str(tk_id).startswith("tk-"), (h, tk_id))
rr = cards.reopen(qid, "the mail server refused the sender")
c = cards._read(cards.LIVE / f"{qid}.json")
check("reopen: the SAME card is open again, headed by what went wrong, old answer voided, held for the next digest",
      rr.get("ok") and c and c.get("status") == "open" and c["questions"][0]["question"].startswith("Not confirmed done last time (the mail server refused")
      and "Thanks staff1" in c["questions"][0]["question"] and cards.answer_of(qid) == "" and c.get("delivery") == "weekly" and c.get("digest_count") == 0, (rr, c and c.get("status")))
check("a reopened card is not harvested again by itself", not [x for x in cards.harvest() if x["qid"] == qid])
cards.answer(qid, "Send (Recommended)", by="test")
cards.harvest()
check("answered again: a new ticket from the same payload", len([t for t in es.pending() if t.get("approval_ref") == f"card:{qid}"]) >= 1)
cards.reopen(qid, "again")
cards.answer(qid, "Send (Recommended)", by="test"); cards.harvest()
check("at most REOPEN_MAX times", not cards.reopen(qid, "third time").get("ok"))

# an urgent card that failed goes straight back to Chat
ru = post("New lead: Zoe", "Text Zoe?\n[TEXT PREVIEW] To +18055550101:\nHello Zoe!", urgent=True)
cards.answer(ru["qid"], "Do it (Recommended)", by="test"); cards.harvest()
n_posted = len(G.posted)
rr = cards.reopen(ru["qid"], "phone offline")
check("an urgent card comes straight back to the phone", rr.get("ok") and rr.get("delivery") == "now" and len(G.posted) == n_posted + 1, rr)

# drain's hook: a PERMANENT failure reopens; an UNVERIFIED one never does
import drain  # noqa: E402
t_perm = {"id": "tk-x", "kind": "email_send", "approval_ref": f"card:{qid}", "payload": {}}
check("drain: UNVERIFIED never reopens (the message may have landed)", drain._reopen_card(t_perm, drain.UNVERIFIED + "timed out") is False)
check("drain: a standing ticket (no card) never reopens", drain._reopen_card({"id": "tk-y", "kind": "email_send", "approval_ref": "standing:x"}, drain.PERM + "bad") is False)

print(f"\n{len(FAILS)} FAILED" if FAILS else "\nALL OK")
sys.exit(1 if FAILS else 0)

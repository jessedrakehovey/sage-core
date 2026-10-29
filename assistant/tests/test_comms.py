"""Offline tests for the comms substrate (comms_state.py) and the one-minute probe's wake
decision (comms_probe.py). Scratch data dir; nothing is sent, no operator is started.
  python assistant/tests/test_comms.py
"""
import json
import os
import sys
import tempfile
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
TMP = Path(tempfile.mkdtemp(prefix="wv_comms_"))
os.environ["ASSISTANT_DATA"] = str(TMP)
os.environ["ASSISTANT_CARDS"] = str(TMP / "cards")
os.environ["ASSISTANT_LEARNINGS"] = str(TMP / "learnings")
os.environ["AGENT_STATE"] = str(TMP / "agent_state")       # Sage's memory, read for an owner's "I replied"
sys.path.insert(0, str(HERE.parent / "scripts"))
import cards  # noqa: E402
import comms_probe  # noqa: E402
import comms_state  # noqa: E402
import tickets  # noqa: E402
import text_gate  # noqa: E402

# the lead, the vendor and Sam are wedding parties here (text_gate.py only tickets those, 2026-10-11)
text_gate.business_numbers = lambda: {"555-057-8657", "8055550002", "8055550177"}
from alib import DATA, save_json  # noqa: E402

FAILS = []


def check(name, cond, extra=""):
    print(("ok   " if cond else "FAIL ") + name + (f"  {extra}" if extra and not cond else ""))
    if not cond:
        FAILS.append(name)


def stamp(dt_s):
    return time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(time.time() - dt_s))


def zstamp(dt_s):
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() - dt_s))


(DATA / "leads").mkdir(parents=True, exist_ok=True)
(DATA / "leads" / "t1.json").write_text(json.dumps({"lead": {"name": "Arden", "partner": "Reese", "phone": "+1 (555) 057-8657", "email": "ar@example.com"}}), encoding="utf-8")

# phone threads: a lead 20 min quiet (breach), a friend (personal, no SLA), an answered couple-free vendor
save_json(DATA / "phone_threads.json", {
    "+1555-057-8657": {"name": "", "messages": [{"ts": stamp(1200), "dir": "in", "text": "Hi owner1, is June 12 open?", "ref": ""}]},
    "+18055550001": {"name": "Dave", "messages": [{"ts": stamp(300), "dir": "in", "text": "golf sunday?", "ref": ""}]},
    "+18055550002": {"name": "", "messages": [{"ts": stamp(7200), "dir": "in", "text": "Quote attached", "ref": ""},
                                              {"ts": stamp(3600), "dir": "out", "text": "Thanks, looks good", "ref": "tk-1"}]},
    "+18055550003": {"name": "", "messages": [{"ts": stamp(60), "dir": "in", "text": "Your verification code is 482913", "ref": ""}]},
})
tickets.add_many([
    {"source": "imessage", "source_id": "a", "lane": "wedding", "kind": "imessage_in", "title": "Text from Arden",
     "payload": {"from": "+1555-057-8657", "text": "Hi owner1, is June 12 open?", "received": stamp(1200), "account": "owner1-iphone"}},
    {"source": "imessage", "source_id": "b", "lane": "personal", "kind": "imessage_in", "title": "Text from Dave",
     "payload": {"from": "+18055550001", "name": "Dave", "text": "golf sunday?", "received": stamp(300), "account": "owner1-iphone"}},
    {"source": "imessage", "source_id": "c", "lane": "wedding", "kind": "imessage_in", "title": "Text from vendor",
     "payload": {"from": "+18055550002", "text": "Quote attached", "received": stamp(7200), "account": "owner1-iphone"}},
    {"source": "mail:example", "source_id": "m1", "lane": "wedding", "kind": "email", "title": "ar@example.com: June wedding",
     "summary": "Hi! Sending over our timeline", "payload": {"account": "example", "from": "Arden <ar@example.com>", "subject": "June wedding", "bulk": False}},
    {"source": "mail:personal", "source_id": "m2", "lane": "money", "kind": "email", "title": "promo", "summary": "sale",
     "payload": {"account": "personal", "from": "deals@shop.example", "subject": "SALE", "bulk": True}},
], "test")

st = comms_state.build()
th = st["threads"]
check("lead text classified as lead by quiz phone", th["text:+1555-057-8657"]["class"] == "lead" and th["text:+1555-057-8657"]["name"] == "Arden & Reese")
check("lead 20 min quiet is needs_reply + breached (SLA 15 min)", th["text:+1555-057-8657"]["state"] == "needs_reply" and th["text:+1555-057-8657"]["sla"]["breached"])
check("friend's text is personal, closed, no SLA (wedding-only texts, 2026-10-08)",
      th["text:+18055550001"]["class"] == "personal" and th["text:+18055550001"]["state"] == "closed" and th["text:+18055550001"]["sla"] is None)
check("friend's text on owner1's line got no ticket", not any(t["payload"].get("from") == "+18055550001" for t in tickets._load().values()))
check("vendor thread with verified reply is awaiting_party", th["text:+18055550002"]["state"] == "awaiting_party")
check("verification code text is closed", th["text:+18055550003"]["state"] == "closed")
check("lead email thread is a lead (by address)", th["email:ar@example.com"]["class"] == "lead" and th["email:ar@example.com"]["state"] == "needs_reply")
check("bulk mail is not a thread", "email:deals@shop.example" not in th)
check("lanes + counts agree", st["counts"]["breached"] == 1 and set(st["lanes"]["breached"]) == {"text:+1555-057-8657"})

# a card open for the lead's number -> awaiting_the owner, no breach ticket
r = cards.post([{"question": "[TEXT PREVIEW] To Arden (+1555-057-8657):\nHi Arden! Yes, June 12 is open. - owner1", "header": "Reply to Arden",
                 "lane": "wedding", "options": [{"label": "Send (Recommended)"}, {"label": "Skip"}],
                 "exec": {"kind": "imessage_send", "approve_label": "Send (Recommended)",
                          "payload": {"account": "owner1-iphone", "to": "+1555-057-8657", "body": "Hi Arden! Yes, June 12 is open. - owner1"}}}], "test")
check("reply card posted", r["posted"], r)
st2 = comms_state.build()
check("open card -> awaiting_owner1 with the qid", st2["threads"]["text:+1555-057-8657"]["state"] == "awaiting_owner1" and st2["threads"]["text:+1555-057-8657"]["card"] == r["qid"])
n = comms_state.breach_tickets(st2)
check("no breach ticket while a card is open", n == 0)

# breach ticket when nothing is drafted: once, latched
cards._retire(cards._read(cards.LIVE / f"{r['qid']}.json"), "expired")
st3 = comms_state.build()
n1 = comms_state.breach_tickets(st3)
n2 = comms_state.breach_tickets(st3)
tk = [t for t in tickets._load().values() if t["kind"] == "sla_breach"]
check("breach ticket minted once and latched", n1 == 1 and n2 == 0 and len(tk) == 1 and "Arden" in tk[0]["title"], (n1, n2))

# the probe: wakes for fresh human work, not for old or already-woken tickets, not twice in a row
law = comms_state.law()["wake"]
work = comms_probe.new_work(law, {})
check("probe sees the fresh human tickets", {t["kind"] for t in work} >= {"imessage_in", "email"} and all(t["kind"] in law["kinds"] for t in work))
seen = {t["id"]: time.time() for t in work}
check("already-woken ids are skipped", comms_probe.new_work(law, seen) == [])
old = tickets._load()
for t in old.values():
    t["created"] = zstamp(3 * 3600)
save_json(DATA / "tickets.json", old)
check("tickets older than max_age do not wake", comms_probe.new_work(law, {}) == [])
comms_probe.RUNNING.write_text("x", encoding="utf-8")
check("in-flight marker blocks a wake", comms_probe.operator_running())

# zero-LLM pretriage: an ack-only text is dismissed by code, a real one passes (ported ack gate, 2026-10-08)
import tickets  # noqa: E402
tickets.add_many([{"source": "imessage", "source_id": "pt-ack", "lane": "personal", "kind": "imessage_in",
                   "title": "Text from Sam: Thank youuuu", "summary": "Thank youuuu", "payload": {"from": "+18055550177", "text": "Thank youuuu"}},
                  {"source": "imessage", "source_id": "pt-real", "lane": "personal", "kind": "imessage_in",
                   "title": "Text from Sam: can you do 6?", "summary": "can you do 6?", "payload": {"from": "+18055550177", "text": "can you do 6?"}}], "test")
_pt = [t for t in tickets._load().values() if t.get("source_id") in ("pt-ack", "pt-real")]
_kept = comms_probe.pretriage(_pt)
check("pretriage: the ack-only text is dismissed, the real question is kept",
      [t["source_id"] for t in _kept] == ["pt-real"] and tickets._load()[[t for t in _pt if t["source_id"] == "pt-ack"][0]["id"]]["status"] == "dismissed")

# a group-chat burst waits until the thread goes quiet (the owner 2026-10-07); a lone text never waits
# an owner who said in chat they replied themselves (phone, HoneyBook) closes the thread: no reply card,
# no breach chase (example 2026-10-22: the owner said 10/21 he had replied, her 10/9 text still raised a card)
(DATA / "leads" / "k.json").write_text(json.dumps({"lead": {"name": "example Underhill", "phone": "+1 805 555 0144"}}), encoding="utf-8")
_pt = json.loads((DATA / "phone_threads.json").read_text(encoding="utf-8"))
_pt["+18055550144"] = {"name": "example Underhill", "messages": [{"ts": stamp(13 * 86400), "dir": "in", "text": "Any update on the film?", "ref": ""}]}
save_json(DATA / "phone_threads.json", _pt)
KEN = "text:+18055550144"
check("example's 13-day-old text needs a reply before anyone says otherwise", comms_state.build()["threads"][KEN]["state"] == "needs_reply")


def _mem(*facts):
    (TMP / "agent_state").mkdir(exist_ok=True)
    save_json(TMP / "agent_state" / "memory.json", {"next_id": 9, "facts": [
        {"id": f"m{i}", "kind": "business", "subject": subj, "fact": fact, "by": by, "active": True,
         "added": time.strftime("%Y-%m-%d %H:%M", time.localtime(time.time() - ago))} for i, (subj, fact, by, ago) in enumerate(facts, 1)]})


_mem(("example Underhill (Underhill-Whitlock, 2026-06-20)", "On Oct 21, 2026 owner1 said he replied to example that her film is being finished. "
      "The reply to her is done, so don't draft or chase another one about it", "owner1", 86400))
st4 = comms_state.build()
check("owner said he replied after her text -> answered (awaiting_party), names the memory fact",
      st4["threads"][KEN]["state"] == "awaiting_party" and st4["threads"][KEN].get("owner_replied") == "m1", st4["threads"][KEN])
comms_state.breach_tickets(st4)
check("...and no unanswered-text chase for it", KEN not in st4["lanes"]["breached"]
      and not any(t["kind"] == "sla_breach" and t["payload"].get("thread") == KEN for t in tickets._load().values()))
_mem(("example Underhill", "owner1 replied to example himself", "sage-dream", 86400))
check("a dream-written 'replied' is not an owner's word", comms_state.build()["threads"][KEN]["state"] == "needs_reply")
_mem(("example Underhill", "owner1 hasn't replied to example yet", "owner1", 86400))
check("'hasn't replied' does not close it", comms_state.build()["threads"][KEN]["state"] == "needs_reply")
_mem(("Arden", "owner1 said he replied to Arden", "owner1", 86400))
check("a reply about someone else does not close example's thread", comms_state.build()["threads"][KEN]["state"] == "needs_reply")
_mem(("example example IV", "example's request is already answered: the example handoff package was emailed to him", "owner1", 86400))
check("a fact about example that only mentions example does not close her thread", comms_state.build()["threads"][KEN]["state"] == "needs_reply")
_mem(("example Underhill", "owner1 said he replied to example", "owner1", 86400))
_pt["+18055550144"]["messages"].append({"ts": stamp(3600), "dir": "in", "text": "curious if you have any updates?", "ref": ""})
save_json(DATA / "phone_threads.json", _pt)
check("a NEW text after his 'I replied' needs a reply again", comms_state.build()["threads"][KEN]["state"] == "needs_reply")

_w = {"burst_texts": 3, "settle_min": 8, "burst_senders": 2, "settle_max_min": 15}
_tx = lambda s, frm="+18055550101": {"kind": "imessage_in", "created": zstamp(s), "payload": {"from": frm}}
_grp = [_tx(30, "+18055550101"), _tx(90, "+18055550102"), _tx(200, "+18055550103")]
check("burst: three fresh texts from different people hold the wake", comms_probe.burst_settling(_w, _grp))
check("burst: once the newest is 8 min old it wakes", not comms_probe.burst_settling(_w, [_tx(500, "+1805550101"), _tx(600, "+18055550102"), _tx(900, "+18055550103")]))
check("burst: one friend sending four lines is not a group chat (never held)",
      not comms_probe.burst_settling(_w, [_tx(5), _tx(30), _tx(60), _tx(90)]))
check("burst: a Google code sender does not count as a person", not comms_probe.burst_settling(_w, [_tx(5), _tx(30), _tx(60, "pd20f57@example.invalid")]))
check("burst: a thread that never goes quiet still runs after 15 min",
      not comms_probe.burst_settling(_w, [_tx(20, "+18055550101"), _tx(400, "+18055550102"), _tx(16 * 60, "+18055550103")]))
check("burst: a single text never waits", not comms_probe.burst_settling(_w, [_tx(5)]))
check("burst: an email in the batch is never held", not comms_probe.burst_settling(_w, _grp + [{"kind": "email", "created": zstamp(5)}]))

print(f"\n{'ALL OK' if not FAILS else 'FAILED: ' + ', '.join(FAILS)}  ({len(FAILS)} failures)")
sys.exit(1 if FAILS else 0)

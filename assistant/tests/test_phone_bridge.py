"""test_phone_bridge.py - owner's iMessage bridge end to end, in scratch dirs.

inbound text -> ticket -> exec card guards -> approve -> drain (undo-safe) -> phone outbox
-> handed exactly once -> ack -> thread log; plus the HTTP token door on mc_server.

  python assistant/tests/test_phone_bridge.py
"""
import os as _os, tempfile as _tf
_os.environ.setdefault("WV_LOG_DIR", _tf.mkdtemp(prefix="wv_logs_"))   # never the live ~/.claude logs
import json
import os
import sys
import tempfile
import threading
import time
import urllib.request
from pathlib import Path

SCRATCH = Path(tempfile.mkdtemp(prefix="phone_bridge_"))
os.environ["ASSISTANT_DATA"] = str(SCRATCH / "data")
os.environ["ASSISTANT_CARDS"] = str(SCRATCH / "data" / "cards")
os.environ["ASSISTANT_LEARNINGS"] = str(SCRATCH / "learnings")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import cards  # noqa: E402
import drain  # noqa: E402
import exec_store as es  # noqa: E402
import phone_bridge as pb  # noqa: E402
import tickets  # noqa: E402
import text_gate  # noqa: E402
from alib import config as _config  # noqa: E402
# the line that texts leads (config leads.text_account) and another owner's line, by name from the pack
LEAD_LINE = str((_config().get("leads") or {}).get("text_account") or "")
OTHER_LINE = next((k for k in pb.ACCOUNTS if k != LEAD_LINE), "")

# the bridge plumbing below runs with phone.triage_all_texts on (every text is ticketed); the
# wedding-only gate section further down switches it back off and on again (2026-10-11)
text_gate.business_only = lambda: False
drain._quiet_hours = lambda now=None: False
pb._quiet = lambda: False
FAILS = []


def check(name, cond, extra=""):
    print(("PASS " if cond else "FAIL ") + name + (f"  [{extra}]" if extra and not cond else ""))
    if not cond:
        FAILS.append(name)


MOM = "(805) 555-0142"
H = "+18055550142"


def card(body, to=H, preview=True, account="owner6-iphone"):
    q = f'Mom texted: "Are you two coming Sunday?"\n{"[TEXT PREVIEW] " if preview else ""}To Mom ({to}):\n{body}'
    return [{"question": q, "header": "Reply to Mom", "lane": "owner6", "audience": "personal",
             "options": [{"label": "Send (Recommended)"}, {"label": "Skip"}],
             "exec": {"kind": "imessage_send", "approve_label": "Send (Recommended)",
                      "payload": {"account": account, "to": to, "body": body}}}]


# ---- identity + token
check("norm_handle formats a US number", pb.norm_handle(MOM) == H)
check("valid_handle rejects property email", not pb.valid_handle("x@other-business.invalid"))
tok = pb.token()
check("token minted and checks", pb.check_token(tok) and not pb.check_token("nope") and not pb.check_token(""))

# ---- inbound
r = pb.inbound(MOM, "Are you two coming Sunday? Dinner at 5", name="Mom", received="2026-10-01T10:00:00")
check("inbound queues one ticket", r == {"ok": True, "queued": 1}, r)
r2 = pb.inbound(MOM, "Are you two coming Sunday? Dinner at 5", name="Mom", received="2026-10-01T10:00:00")
check("same text re-posted is deduped", r2.get("queued") == 0, r2)
t = [x for x in tickets._load().values() if x["kind"] == "imessage_in"]
check("ticket is lane owner6 with sender", len(t) == 1 and t[0]["lane"] == "owner6" and t[0]["payload"]["from"] == H)
check("thread log holds the inbound", pb.thread(MOM)["messages"][-1]["dir"] == "in")
check("inbound needs from + text", not pb.inbound("", "hi")["ok"])

# ---- card guards
check("card without [TEXT PREVIEW] refused", not cards.post(card("Yes! We'll bring dessert", preview=False), "operator")["posted"])
check("unknown phone account refused", "EXEC_KEYS" in cards.post(card("Yes!", account="nokia-3310"), "operator").get("reason", ""))
check("owner1-iphone is an allowed account", cards.post(card("Hi Arden! Just saw your form. - owner1", account="owner1-iphone"), "operator").get("posted"))
check("email recipient on property domain refused",
      not cards.post(card("hi", to="a@other-business.invalid"), "operator")["posted"])
bad = card("Yes! We'll bring dessert")
bad[0]["question"] = bad[0]["question"].replace("We'll bring dessert", "")
check("body not verbatim refused", "EXEC_VERBATIM" in cards.post(bad, "operator").get("reason", ""))
p = cards.post(card("Yes! We'll bring dessert ❤️"), "operator", lane="owner6")
check("valid text card posts", p.get("posted"), p)
check("pending exposes exec_kind", any(c.get("exec_kind") == "imessage_send" for c in cards.pending()))

# ---- approve -> drain (undo-safe) -> outbox
a = cards.answer(p["qid"], "Send (Recommended)", by="mc:owner6@example")
check("approve mints a phone ticket", bool(a.get("ticket")) and es.fold()[a["ticket"]]["lane"] == "phone", a)
d = drain.drain(lane="phone", min_age_s=35)
check("drain respects the undo window", d["ran"] == 0 and es.fold()[a["ticket"]]["status"] == "queued", d)
d = drain.drain(lane="phone")
check("a card approval is held for the undo window by every caller", d["ran"] == 0 and es.fold()[a["ticket"]]["status"] == "queued", d)
drain.UNDO_FLOOR_S = 0
d = drain.drain(lane="phone")
check("drain hands it to the outbox", d["done"] == 1 and es.fold()[a["ticket"]]["status"] == "done", d)
ok, msg = pb.queue_outbound({"to": H, "body": "Yes! We'll bring dessert ❤️"}, a["ticket"])
check("outbox is idempotent per ticket", ok and "already" in msg)

items = pb.fetch_outbox()
check("phone fetch gets the exact approved text", len(items) == 1 and items[0]["body"] == "Yes! We'll bring dessert ❤️" and items[0]["to"] == H, items)
check("a second fetch gets nothing (at most once)", pb.fetch_outbox() == [])
check("not stale yet", pb.stale() == [])
check("stale after the window", len(pb.stale(minutes=-1)) == 1)
check("ack marks sent", pb.ack(items[0]["id"])["ok"])
check("thread logs the sent reply", pb.thread(H)["messages"][-1] == {**pb.thread(H)["messages"][-1], "dir": "out", "text": "Yes! We'll bring dessert ❤️"})
check("ack unknown id fails", not pb.ack("m-nope")["ok"])

# ---- duplicate guard (texting windows)
try:
    es.enqueue("imessage_send", {"account": "owner6-iphone", "to": H, "body": "another"}, "owner1", "card:x")
    check("second text to same contact within 2 min refused", False)
except PermissionError:
    check("second text to same contact within 2 min refused", True)

# ---- HTTP door
import mc_server  # noqa: E402
from http.server import ThreadingHTTPServer  # noqa: E402
srv = ThreadingHTTPServer(("127.0.0.1", 0), mc_server.H)
threading.Thread(target=srv.serve_forever, daemon=True).start()
base = f"http://127.0.0.1:{srv.server_address[1]}"


def call(path, body=None, token=None):
    req = urllib.request.Request(base + path, data=json.dumps(body).encode() if body is not None else None,
                                 method="POST" if body is not None else "GET",
                                 headers={"Content-Type": "application/json", **({"X-example-Token": token} if token else {})})
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return r.status, json.loads(r.read())
    except urllib.error.HTTPError as e:
        return e.code, {}


check("outbox without token is 403", call("/api/phone/outbox")[0] == 403)
check("inbound without token is 403", call("/api/phone/inbound", {"from": MOM, "text": "x"})[0] == 403)
s, b = call("/api/phone/inbound", {"from": "805-555-0199", "text": "Dentist moved to Tue Oct 14 at 3", "name": "Dr. Lee"}, tok)
check("inbound with token queues", s == 200 and b.get("queued") == 1, (s, b))
s, b = call("/api/phone/outbox", token=tok)
check("outbox with token answers", s == 200 and b.get("count") == 0, (s, b))
srv.shutdown()

# quiet hours: nothing leaves the outbox at night, even an item that is already ready
ok, msg = pb.queue_outbound({"to": H, "body": "late text"}, "tk-night")
pb._quiet = lambda: True
check("outbox hands nothing over in quiet hours", ok and pb.fetch_outbox() == [])
pb._quiet = lambda: False
check("the held text goes out after quiet hours", [i["body"] for i in pb.fetch_outbox()] == ["late text"])

# two phones, two outboxes: each Shortcut only ever sees texts approved for ITS number
ok_j, _ = pb.queue_outbound({"to": "+15105550101", "body": "from owner1", "account": "owner1-iphone"}, "tk-j1")
ok_h, _ = pb.queue_outbound({"to": "+18055550199", "body": "from owner6", "account": "owner6-iphone"}, "tk-h1")
got_j = pb.fetch_outbox("owner1-iphone")
check("owner1's phone gets only owner1's texts", ok_j and ok_h and [i["body"] for i in got_j] == ["from owner1"], got_j)
got_h = pb.fetch_outbox()
check("owner6's phone (default) gets only hers", [i["body"] for i in got_h] == ["from owner6"], got_h)
check("nothing left for either", pb.fetch_outbox("owner1-iphone") == [] and pb.fetch_outbox() == [])
# a text from a known quiz lead's number to the owner's phone is wedding work; anyone else is personal
text_gate.business_only = lambda: True            # the live default: wedding texts only
leads_dir = Path(os.environ["ASSISTANT_DATA"]) / "leads"
leads_dir.mkdir(parents=True, exist_ok=True)
(leads_dir / "tok1.json").write_text(json.dumps({"lead": {"name": "Arden", "phone": "+1 (555) 057-8657"}}), encoding="utf-8")
r = pb.inbound("555-057-8657", "Hi owner1, yes June 12 is right!", "Arden", account="owner1-iphone")
r2 = pb.inbound("805-555-0177", "golf sunday?", "Dave", account="owner1-iphone")
lanes = {t["payload"]["from"]: (t["lane"], t["payload"]["account"]) for t in tickets._load().values() if t["kind"] == "imessage_in"}
check("lead's text -> lane wedding on owner1-iphone", lanes.get("+15550578657") == ("wedding", "owner1-iphone"), lanes)
# the owner 2026-10-08: on his line only wedding texts are triaged (text_gate.py); a friend's text is
# kept in the thread log (so Sage can draft it on request) but never ticketed / carded
check("friend's text on owner1-iphone is not ticketed", "+18055550177" not in lanes and r2.get("queued") == 0, (lanes, r2))
check("...but it is in the thread log for an on-request draft", pb.thread("805-555-0177")["messages"][-1]["text"] == "golf sunday?")
check("a stale unconfirmed text to a friend on owner1's line is not a sentinel",
      text_gate.applies(LEAD_LINE) and not text_gate.applies(OTHER_LINE) and not text_gate.is_business("805-555-0177"))
(leads_dir / "tok2.json").write_text(json.dumps({"lead": {"name": "Vendor", "phone": "805-555-0166"}}), encoding="utf-8")
pb.inbound("805-555-0166", "Florals arrive at 2", "Rosa", account="owner1-iphone")
check("a wedding-matched number is ticketed in lane wedding",
      any(t["payload"].get("from") == "+18055550166" and t["lane"] == "wedding" for t in tickets._load().values() if t["kind"] == "imessage_in"))
# 2026-10-10: owner is a CRM contact (on her owner email), so her texts were carded as a
# "client". Family is never wedding work: her text is kept in the thread log but never ticketed.
import contacts  # noqa: E402
crm_file = SCRATCH / "crm.json"
crm_file.write_text(json.dumps({"contacts": [
    {"name": "owner6 example", "email": "owner6@example.invalid", "phone": "8055550193", "company": "example-org"},
    {"name": "Client Carla", "email": "carla@example.com", "phone": "8055550155"}]}), encoding="utf-8")
real_crm = os.environ.get("CRM_SNAPSHOT_FILE"); os.environ["CRM_SNAPSHOT_FILE"] = str(crm_file)   # crm connector's crm_people
rj = pb.inbound("805-555-0193", "Oh they did bring tents", "owner6 Pennywhistle", account="owner1-iphone")
check("a text from owner6's number makes no ticket / card",
      rj.get("queued") == 0 and not any(t["payload"].get("from") == "+18055550193" for t in tickets._load().values()), rj)
check("...but it is in the thread log", pb.thread("805-555-0193")["messages"][-1]["text"] == "Oh they did bring tents")
if any(hasattr(m, "crm_people") for m in __import__("alib").plugins()):   # the CRM's own clients: only with its plugin
    check("...while a real CRM client is still a wedding party",
          text_gate.is_business("805-555-0155") and not text_gate.is_business("805-555-0193"))
check("a number in Family and Friends is household, never wedding",
      not (text_gate.household_numbers() and text_gate.business_numbers() & text_gate.household_numbers()))
# 2026-10-11: owner's texts still reached the personal lane, arriving already laned `wedding`.
# An owner / family text is dropped whatever lane it carries and on every phone.
jt = {"source": "imessage", "kind": "imessage_in", "lane": "wedding", "payload": {"from": "+18055550193", "account": "owner1-iphone"}}
check("a household text arriving laned wedding is still dropped", text_gate.keep([jt]) == [])
check("...and on owner6's phone too",
      text_gate.keep([{**jt, "lane": "owner6", "payload": {**jt["payload"], "account": "owner6-iphone"}}]) == [])
if any(hasattr(m, "crm_people") for m in __import__("alib").plugins()):   # the CRM's own clients: only with its plugin
    check("...while a client text on owner6's phone still passes",
          len(text_gate.keep([{**jt, "lane": "owner6", "payload": {"from": "+18055550155", "account": "owner6-iphone"}}])) == 1)
# 2026-10-11: "I only want you to draft for wedding clients": an unmatched number makes no card on
# either phone, even when it arrives laned `wedding` / `owner` / `personal`
ft = {"source": "imessage", "kind": "imessage_in", "lane": "wedding", "payload": {"from": "+18055550199", "account": "owner1-iphone"}}
check("an unknown number laned wedding on owner1's line is dropped", text_gate.keep([ft]) == [])
check("an unknown number on owner6's phone is dropped",
      text_gate.keep([{**ft, "lane": "owner6", "payload": {**ft["payload"], "account": "owner6-iphone"}}]) == [])
kept = text_gate.keep([{**jt, "lane": "owner6", "payload": {"from": "+18055550155", "account": "owner6-iphone"}}])
if any(hasattr(m, "crm_people") for m in __import__("alib").plugins()):   # the CRM's own clients: only with its plugin
    check("...a client on owner6's phone keeps her lane", [k["lane"] for k in kept] == ["owner6"], kept)
rf = pb.inbound("805-555-0199", "lol same here", "Sam", account="owner6-iphone")
check("a friend's text to owner6's phone makes no ticket but is in the thread log",
      rf.get("queued") == 0 and pb.thread("805-555-0199")["messages"][-1]["text"] == "lol same here", rf)
# ruling 13 (2026-10-15): a time-related personal text is the one exception: ONE ticket, marked, lane kept
_pe = text_gate.exception_cfg
text_gate.exception_cfg = lambda: {"enabled": True, "time": True, "money": True, "include_family": True}
rf2 = pb.inbound("805-555-0199", "dinner friday at 7?", "Sam", account="owner6-iphone")
_exc = [t for t in tickets._load().values() if t["payload"].get("from") == "+18055550199" and t["payload"].get("text") == "dinner friday at 7?"]
check("...but a time-related one comes through marked payload.exception=time (ruling 13)",
      rf2.get("queued") == 1 and len(_exc) == 1 and _exc[0]["payload"].get("exception") == "time" and _exc[0]["lane"] == "owner6", (rf2, _exc))
text_gate.exception_cfg = lambda: {"enabled": False, "time": True, "money": True, "include_family": True}
rf3 = pb.inbound("805-555-0199", "dinner saturday at 7?", "Sam", account="owner6-iphone")
check("...and not when the switch is off", rf3.get("queued") == 0, rf3)
text_gate.exception_cfg = _pe
# 2026-10-24: Marlo (our contract shooter) asked about his pay; it was filed personal as a money exception.
# A text from one of our own team is wedding work: it always comes through, laned wedding, on either phone.
_tm = text_gate.team_member
text_gate.team_member = lambda h: "Quinn Heathcote" if text_gate._key(h) == "8055550188" else ""
mt = {"source": "imessage", "kind": "imessage_in", "lane": "personal",
      "payload": {"from": "+18055550188", "account": "owner1-iphone", "text": "What's the pay for Nov 7?"}}
check("a team member's text comes through laned wedding (Marlo, 2026-10-24)",
      [k["lane"] for k in text_gate.keep([mt])] == ["wedding"])
check("...on owner6's phone too",
      [k["lane"] for k in text_gate.keep([{**mt, "lane": "owner6", "payload": {**mt["payload"], "account": "owner6-iphone"}}])] == ["wedding"])
_on = text_gate.owner_numbers
text_gate.team_member = lambda h: "owner6 example"
text_gate.owner_numbers = lambda: {"8055550193"}
check("...while an owner's own number is never treated as team",
      text_gate.keep([{**mt, "payload": {**mt["payload"], "from": "+18055550193", "text": "home soon"}}]) == [])
text_gate.team_member, text_gate.owner_numbers = _tm, _on
check("non-text items pass the gate untouched", text_gate.keep([{"kind": "email_in", "lane": "personal"}]) == [{"kind": "email_in", "lane": "personal"}])
real_wo = text_gate.business_only
text_gate.business_only = lambda: False           # phone.triage_all_texts = true: the old behaviour
r3 = pb.inbound("805-555-0177", "bring the tent?", "Dave", account="owner1-iphone")
rj2 = pb.inbound("805-555-0193", "Won both games", "owner6 Pennywhistle", account="owner1-iphone")
text_gate.business_only = real_wo
if real_crm is None:
    os.environ.pop("CRM_SNAPSHOT_FILE", None)
else:
    os.environ["CRM_SNAPSHOT_FILE"] = real_crm
check("switch on: owner6's text is ticketed again too", rj2.get("queued") == 1, rj2)
check("switch on: every text on owner1's line is ticketed again (lane personal)",
      r3.get("queued") == 1 and any(t["payload"].get("text") == "bring the tent?" and t["lane"] == "personal" for t in tickets._load().values() if t["kind"] == "imessage_in"))
text_gate.business_only = lambda: False           # (an unknown number is only ticketed with the switch on)
check("unknown account falls back to owner6's", pb.inbound("805-555-0178", "x", account="nokia")["ok"] and
      [t for t in tickets._load().values() if t.get("payload", {}).get("from") == "+18055550178"][0]["lane"] == "owner6")

# a busy queue must tell the phone to retry, not swallow the text
import tickets  # noqa: E402
real_add = tickets.add_many
tickets.add_many = lambda items, owner: -1
r = pb.inbound(H, "are you free saturday?", "Mom")
tickets.add_many = real_add
check("inbound on a busy queue is parked, not lost", r.get("ok") and r.get("spilled") == 1, r)
cards.sweep()
check("the parked text becomes its ticket on the next sweep",
      any(t["kind"] == "imessage_in" and "saturday" in t["summary"] for t in tickets._load().values()))

# a text that waited past its moment is not sent late; it comes back as a ticket
ok, msg = pb.queue_outbound({"to": H, "body": "running 10 min late"}, "tk-late")
box = pb.load_json(pb.OUTBOX, {"items": []})
for i in box["items"]:
    if i["ticket"] == "tk-late":
        i["ts"] = "2026-01-01T08:00:00"
pb.save_json(pb.OUTBOX, box)
got = pb.fetch_outbox()
check("a stale approved text is not handed over", all(i["body"] != "running 10 min late" for i in got), got)
check("...and is ticketed for a re-card", any(t["source"] == "phone_outbox" and "NOT sent" in t["title"] for t in tickets._load().values()))

# ...even when the queue is busy at that moment: the outbox already says "expired", so the
# ticket is parked and queued by the next card sweep instead of vanishing
ok, msg = pb.queue_outbound({"to": H, "body": "see you at six"}, "tk-late-busy")
box = pb.load_json(pb.OUTBOX, {"items": []})
for i in box["items"]:
    if i["ticket"] == "tk-late-busy":
        i["ts"] = "2026-01-01T08:00:00"
pb.save_json(pb.OUTBOX, box)
tickets.add_many = lambda items, owner: -1
pb.fetch_outbox()
tickets.add_many = real_add
check("a stale text on a busy queue is not ticketed yet", not any("see you at six" in t["summary"] for t in tickets._load().values()))
cards.sweep()
check("...and is ticketed by the next sweep", any(t["source"] == "phone_outbox" and "see you at six" in t["summary"] for t in tickets._load().values()))

# a reply proves an unconfirmed text went out: it is marked sent and its card closes (2026-10-10,
# the Delia & Hollis card that sat open two days after Delia had answered the text)
import text_confirm  # noqa: E402
D, E, F = "+13035550180", "+13035550181", "+13035550182"
pb.fetch_outbox()
ok_d, _ = pb.queue_outbound({"to": D, "body": "Hi Delia and Hollis, this is owner1"}, "tk-intro")
ok_e, _ = pb.queue_outbound({"to": E, "body": "Hi Sam"}, "tk-sam")
pb.fetch_outbox()
ok_f, _ = pb.queue_outbound({"to": F, "body": "not fetched yet"}, "tk-wait")
box = pb.load_json(pb.OUTBOX, {"items": []})
for i in box["items"]:
    if i["ticket"] in ("tk-intro", "tk-sam"):      # handed ten minutes ago, so the reply is after it
        i["ts_handed"] = time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(time.time() - 600))
pb.save_json(pb.OUTBOX, box)
oid_d = next(i["id"] for i in box["items"] if i["ticket"] == "tk-intro")


def _plain(header, question, details=""):
    return cards.post([{"question": question, "header": header, "lane": "wedding", "details": details,
                        "options": [{"label": "It sent, all fine (Recommended)"}, {"label": "It never sent, I'll resend"}]}], "operator", lane="wedding")


c_d = _plain("Delia/Hollis text unconfirmed", "The text to Delia and Hollis (...0180) was handed to your iPhone but the phone never "
             "confirmed it sent. Do not re-send blindly.", f"outbox {oid_d}, status PHONE_QUEUED")
c_e = _plain("Sam text", "The text to Sam (...0181) was handed to your iPhone but never confirmed sent. Check Messages.")
c_x = cards.post([{"question": "Delia (...0180) asked to confirm Monday at 1 PM. Put it on the calendar?", "header": "Delia call",
                   "lane": "wedding", "options": [{"label": "Yes (Recommended)"}, {"label": "No"}]}], "operator", lane="wedding")
check("unconfirmed-text cards post", ok_d and ok_e and ok_f and c_d.get("posted") and c_e.get("posted") and c_x.get("posted"), (c_d, c_e, c_x))
# settle what the sections above left behind: Mom (tk-night) and Sam (tk-h1) really did text back after
# their hand-over, so a first run rightly confirms those two once the file takes more than a second
# (2026-10-23: the check below read {"confirmed": 2} for that reason, not for a bug). Only D/E/F are this
# section's fixtures, and none of them has replied yet.
_settled = text_confirm.run()
check("earlier fixtures settle without touching this section's items",
      all(i["status"] == ("handed" if i["ticket"] in ("tk-intro", "tk-sam") else "ready")
          for i in pb.load_json(pb.OUTBOX, {"items": []})["items"] if i["ticket"] in ("tk-intro", "tk-sam", "tk-wait")), _settled)
_tc0 = text_confirm.run(); check("no reply yet: nothing confirmed, nothing closed", _tc0 == {"confirmed": 0, "cards_closed": 0}, _tc0)
pb.inbound(D, "Hi! So excited to talk with you!!", "Delia")
pb.inbound(F, "who is this?", "Fran")
check("dry run reports without changing anything", text_confirm.run(dry=True) == {"confirmed": 1, "cards_closed": 1}
      and any(c["qid"] == c_d["qid"] for c in cards.pending()))
r = text_confirm.run()
item = next(i for i in pb.load_json(pb.OUTBOX, {"items": []})["items"] if i["id"] == oid_d)
check("a reply after the hand-over marks the text sent", r == {"confirmed": 1, "cards_closed": 1}
      and item["status"] == "sent" and item.get("confirmed_by") == "reply", (r, item))
msgs = pb.thread(D)["messages"]
check("...logged at its hand-over time, ahead of the reply (the reply still needs an answer)",
      [m["dir"] for m in msgs] == ["out", "in"] and msgs[0]["text"].startswith("Hi Delia") and msgs[0]["ts"] == item["ts_handed"], msgs)
check("...the sentinel stops chasing it", all(i["id"] != oid_d for i in pb.stale(minutes=-1)))
done = cards._read(cards.DONE / f"{c_d['qid']}.json") or {}
open_q = {c["qid"] for c in cards.pending()}
check("...and its 'text unconfirmed' card is closed as resolved", done.get("status") == "resolved" and c_d["qid"] not in open_q, done.get("status"))
check("a card about a text with no reply stays open", c_e["qid"] in open_q)
check("a card that only says 'confirm' about that number stays open", c_x["qid"] in open_q)
check("a text the phone never fetched is not confirmed by a reply",
      next(i for i in pb.load_json(pb.OUTBOX, {"items": []})["items"] if i["ticket"] == "tk-wait")["status"] == "ready")
check("idempotent: a second run does nothing", text_confirm.run() == {"confirmed": 0, "cards_closed": 0})
c_late = _plain("Delia text", "Your text to ...0180 was handed to your iPhone but never confirmed sent. Check Messages.")
check("a card posted after the reply (from an older digest) closes on the next run",
      c_late.get("posted") and text_confirm.run()["cards_closed"] == 1 and c_late["qid"] not in {c["qid"] for c in cards.pending()}, c_late)

# 2026-10-15: Casey Juniper, a friend and past CRM client, got two text cards in one morning.
# A Wiki/People note tagged friends/family makes the number household; a lead's note does not.
real_vault, fake = text_gate.VAULT, SCRATCH / "vault"
(fake / "Wiki" / "People").mkdir(parents=True, exist_ok=True)
(fake / "Wiki" / "People" / "Jon.md").write_text("---\ntype: person\ntags: [personal, friends]\n---\n\n# Jon\n\nFriend (phone +1 805-555-0136).\n", encoding="utf-8")
(fake / "Wiki" / "People" / "Lead.md").write_text("---\ntype: person\ntags: [personal]\n---\n\n# Lead\n\nPhone 805-555-0137.\n", encoding="utf-8")
text_gate.VAULT, real_wo = fake, text_gate.business_only
text_gate.business_only = lambda: True
home = text_gate.household_numbers()
check("a friend's own People note makes their number household", "8055550136" in home, home)
check("...a People note without family/friends tags does not", "8055550137" not in home, home)
check("...so the friend's text is dropped",
      text_gate.keep([{"kind": "imessage_in", "lane": "personal", "payload": {"from": "+18055550136", "account": "owner1-iphone"}}]) == [])
text_gate.VAULT, text_gate.business_only = real_vault, real_wo
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
if not __import__("engine").is_template():                     # the organization's own People notes: absent in the bare engine
    check("Avery Juniper's banter texts are personal: his People note makes his number household (2026-10-17)",
          "5550089108" in text_gate.household_numbers())
# and a newer text card for the same number on the same phone retires the older open one
J = "+18055550136"
o1 = cards.post(card("Sounds good", to=J, account="owner1-iphone"), "operator")
o2 = cards.post(card("Brown works", to=J, account="owner1-iphone"), "operator")
o3 = cards.post(card("Sounds good", to=J), "operator")          # same number, owner's phone: its own conversation
live = {c["qid"] for c in cards.pending()}
check("a newer text card supersedes the older one to the same person",
      o1.get("posted") and o2.get("posted") and o1["qid"] not in live and o2["qid"] in live
      and (cards._read(cards.DONE / f"{o1['qid']}.json") or {}).get("superseded_by") == o2["qid"], (o1, o2))
check("...a card on the other phone is left open", o3.get("posted") and o3["qid"] in live and o2["qid"] in live)

# 2026-10-17: AJ's card carried Marisa's hint ("...answer the gift exchange or holidays now"), copied
# from the card posted a second before. A text card's hint must come from its own conversation.
HINT = "Safe draft that commits to nothing; Edit & send if you want to answer the gift exchange or holidays now."
def _hinted(who, to, said, body, advice=HINT):
    q = card(body, to=to, account="owner1-iphone")
    q[0].update(header=f"Text reply: {who}", lane="personal", details=f"{who}: {said}", advice=advice)
    q[0]["question"] = f"{who} texted: {said}\n[TEXT PREVIEW] To {who} ({to}):\n{body}"
    return q
kr = cards.post(_hinted("Marisa", "+18055550141", "Want to be part of the family gift exchange? Plans for the holidays?",
                        "We'll think on the gift exchange and the holidays and let you know."), "operator")
aj = cards.post(_hinted("AJ", "+18055550142", "I think you and me use it more like a tool. It's a dark world out there",
                        "Ha, glad we're on the tool side of it"), "operator")
adv = lambda r: ((cards._read(cards.LIVE / f"{r['qid']}.json") or {}).get("questions") or [{}])[0].get("advice")
check("a text card's hint copied from another person's card is replaced with a plain one",
      aj.get("posted") and adv(aj) == text_gate.PLAIN_ADVICE
      and ((cards._read(cards.ARCHIVE / f"{aj['qid']}.json") or {}).get("questions") or [{}])[0].get("advice") == text_gate.PLAIN_ADVICE, adv(aj))
check("...the card the hint belongs to keeps it", kr.get("posted") and adv(kr) == HINT, adv(kr))
mk = cards.post(_hinted("Mike", "+18055550143", "Gift exchange this year? Holidays at ours?", "Count us in for the gift exchange!"), "operator")
check("...a hint that fits the card's own conversation is kept, even when the words match another card", adv(mk) == HINT, adv(mk))

# re-nudge (2026-10-24): a text still "ready" long after the last nudge gets another wake-up, capped
import json as _json, time as _time
import phone_nudge
fired = []
phone_nudge.fire = lambda n, force=False: fired.append(n) or True
_ob = phone_nudge.Path(os.environ["ASSISTANT_DATA"]) / "phone_outbox.json"
_ob.write_text(_json.dumps({"items": []}), encoding="utf-8")
check("re-nudge: nothing ready -> nothing", phone_nudge.renudge_if_waiting(now=_time.mktime((2026, 10, 24, 12, 0, 0, 0, 0, -1))) is False and fired == [])
noon = _time.mktime((2026, 10, 24, 12, 0, 0, 0, 0, -1))
old = _time.strftime("%Y-%m-%dT%H:%M:%S", _time.localtime(noon - 600))
_ob.write_text(_json.dumps({"items": [{"id": "m-1", "status": "ready", "ts": old, "to": "+18055550100", "body": "x"}]}), encoding="utf-8")
phone_nudge.STATE.write_text(_json.dumps({"ts": noon - 300, "to": "p530f74@example.invalid"}), encoding="utf-8")
check("re-nudge: a text waiting 10 min, last nudge 5 min ago -> fires", phone_nudge.renudge_if_waiting(now=noon) is True and fired == [1])
check("re-nudge: not again inside the window", phone_nudge.renudge_if_waiting(now=noon) is False and fired == [1])
phone_nudge.STATE.write_text(_json.dumps({"ts": noon - 300, "to": "x", "renudges": phone_nudge.RENUDGE_MAX}), encoding="utf-8")
check("re-nudge: capped per spell", phone_nudge.renudge_if_waiting(now=noon) is False and fired == [1])
night = _time.mktime((2026, 10, 24, 22, 0, 0, 0, 0, -1))
phone_nudge.STATE.write_text(_json.dumps({"ts": night - 300, "to": "x"}), encoding="utf-8")
_ob.write_text(_json.dumps({"items": [{"id": "m-1", "status": "ready", "ts": _time.strftime("%Y-%m-%dT%H:%M:%S", _time.localtime(night - 600)), "to": "+18055550100", "body": "x"}]}), encoding="utf-8")
check("re-nudge: never in quiet hours", phone_nudge.renudge_if_waiting(now=night) is False and fired == [1])
_ob.write_text(_json.dumps({"items": []}), encoding="utf-8")
phone_nudge.renudge_if_waiting(now=noon)
check("re-nudge: the counter resets once the outbox is empty", _json.loads(phone_nudge.STATE.read_text(encoding="utf-8")).get("renudges", 0) == 0)

print(f"\n{'ALL PASS' if not FAILS else str(len(FAILS)) + ' FAILED'}  (scratch {SCRATCH})")
sys.exit(1 if FAILS else 0)

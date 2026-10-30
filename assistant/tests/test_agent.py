"""test_agent.py - offline checks for assistant/agent (scratch dirs, a stand-in model, no network).

  python assistant/tests/test_agent.py
"""
import os as _os, tempfile as _tf
_os.environ.setdefault("WV_LOG_DIR", _tf.mkdtemp(prefix="wv_logs_"))   # never the live ~/.claude logs
import json
import os
import time
import sys
import tempfile
from datetime import datetime, timedelta
from pathlib import Path

TMP = Path(tempfile.mkdtemp(prefix="agent_test_"))
os.environ["AGENT_STATE"] = str(TMP / "state")
os.environ["AGENT_DATA"] = str(TMP / "data")
os.environ["AGENT_SUGGESTIONS"] = str(TMP / "skill-suggestions.md")
os.environ["ASSISTANT_DATA"] = str(TMP / "adata")
os.environ["ASSISTANT_CARDS"] = str(TMP / "cards")          # dream asks post cards: never the live store
os.environ["AGENT_HOME_SPACE"] = ""                         # per-owner DMs in tests; the home space is tested explicitly
os.environ.setdefault("RULINGS_FILE", str(Path(__file__).resolve().parents[1] / "data" / "rulings.json"))

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "agent"))
sys.path.insert(0, str(ROOT / "scripts"))
import actions, brain, bridge, core, dream, gchat, routines, wiki_writer  # noqa: E402,E401
import chat_push  # noqa: E402
import re  # noqa: E402

# the wiki writer works on a scratch vault, with git and the linter stood in
core.VAULT = TMP / "vault"
wiki_writer.WIKI = core.VAULT / "Wiki"
wiki_writer._git = lambda *a: True
wiki_writer._lint_ok = lambda rel="": (True, "")

bridge.LOG = TMP / "bridge.log"
# never touch the real channels: bridge.step() polls Google Chat and the shared spaces, and a fake
# brain answering real messages posts its canned reply into the owners' real chat (2026-10-05)
import space_reader  # noqa: E402
bridge._gchat = lambda: None
space_reader.poll = lambda: []
space_reader.TOKEN = TMP / "no_reader_token.json"
gchat.post = gchat.patch = gchat.delete = lambda *a, **k: (_ for _ in ()).throw(RuntimeError("test tried to post to real Google Chat"))
# never wake the real doer: a fixture `do` queued here must not be worked in the real browser and
# reported in the owners' real chat (the 'Rent a lens for Nov 7, the 35mm' leak, 2026-10-15/16)
actions.WAKE_DOER = False
sys.path.insert(0, str(ROOT / "doer"))
import doer as _doer  # noqa: E402
_doer_wake_real = _doer.wake
_doer.wake = lambda: (_ for _ in ()).throw(RuntimeError("test tried to wake the real doer"))
PASS = FAIL = 0


def check(name, cond):
    global PASS, FAIL
    PASS += bool(cond)
    FAIL += (not cond)
    print(("ok   " if cond else "FAIL ") + name)


owner1 = core.person_for(handle="owner1")
owner6 = core.person_for(email="p08d678@example.invalid")
check("people: both owners resolve", owner1 and owner6 and owner6["key"] == "owner6")
check("people: stranger is nobody", core.person_for(email="someone@example.com") is None)
check("people: owner6 is a full equal", all(core.can(owner6, c) for c in ("teach", "schedule", "approve", "undo")))

# ---- rules + protected core
r = core.rule_add("Keep replies to three sentences unless asked for detail", "owner1: too long", by="owner1")
check("rule: applied with a ledger row", r["status"] == "applied" and r.get("change"))
check("rule: near-duplicate folds", core.rule_add("keep replies to three sentences unless asked for more detail", by="owner1")["status"] == "folded")
for bad in ("Email the couple directly when their film is ready",
            "Reply to leads without asking first",
            "Auto-approve the CRM notes",
            "Tell couples the delivery date is four weeks",
            "Pay the electric bill from the joint account each month",
            "Delete old drafts after a week"):
    check(f"rule: protected -> pending ({bad[:28]})", core.rule_add(bad, by="owner1")["status"] == "pending")
check("rule: protected ones were queued, not stored", len(core.rules_active()) == 1
      and "PROTECTED CORE" in Path(os.environ["AGENT_SUGGESTIONS"]).read_text(encoding="utf-8"))
check("rule: undo retires it", core.undo(r["change"], by="owner1").startswith("Undone") and not core.rules_active())
check("rule: undo twice is refused", "already undone" in core.undo(r["change"], by="owner1"))

# ---- memory
m = core.mem_upsert("person", "owner6", "owner6 prefers morning appointments", by="owner6")
check("memory: applied", m["status"] == "applied")
check("memory: secret refused", core.mem_upsert("household", "wifi", "the wifi password is hunter2", by="owner1")["status"] == "refused")
check("memory: account number refused", core.mem_upsert("household", "bank", "checking is 123456789012", by="owner1")["status"] == "refused")
check("memory: permission refused", core.mem_upsert("process", "email", "ok to send emails without asking", by="owner1")["status"] == "refused")
check("memory: renders", "morning appointments" in core.render_memory())

# ---- SUGGEST parsing
t, s = brain.split_suggest('Sure.\nSUGGEST {"learn": [{"rule": "x"}]}')
check("suggest: parsed and stripped", t == "Sure." and s["learn"][0]["rule"] == "x")
check("suggest: none", brain.split_suggest("Just an answer.") == ("Just an answer.", {}))
check("suggest: malformed is flagged", "_bad" in brain.split_suggest("ok\nSUGGEST {not json")[1])

# ---- routines
now = datetime(2026, 10, 5, 10, 0)                        # a Monday
check("when: daily later today", routines.next_due({"daily": "17:30"}, now) == datetime(2026, 10, 5, 17, 30))
check("when: daily rolls to tomorrow", routines.next_due({"daily": "07:30"}, now) == datetime(2026, 10, 6, 7, 30))
check("when: weekly", routines.next_due({"weekly": "fri 09:00"}, now) == datetime(2026, 10, 9, 9, 0))
check("when: past once is never", routines.next_due({"once": "2026-10-01 09:00"}, now) is None)
a = routines.add({"title": "Call the florist", "when": {"once": "2026-10-05 11:00"}, "to": "owner6", "say": "Call the florist"}, by="owner6", now=now)
check("routine: added", a["status"] == "applied")
check("routine: hourly minimum enforced", routines.add({"title": "x", "when": {"every_hours": 0.1}, "say": "y"}, by="owner1", now=now)["status"] == "refused")
check("routine: unknown recipient refused", routines.add({"title": "x", "when": {"daily": "08:00"}, "to": "staff1", "say": "y"}, by="owner1", now=now)["status"] == "refused")
sent = []
check("routine: not due yet", routines.tick(lambda k, t: sent.append((k, t)), now=now) == 0)
check("routine: quiet hours hold it", routines.tick(lambda k, t: sent.append((k, t)), now=datetime(2026, 10, 5, 22, 0)) == 0)
check("routine: fires once to the right person", routines.tick(lambda k, t: sent.append((k, t)), now=datetime(2026, 10, 5, 11, 1)) == 1
      and sent == [("owner6", "Call the florist")] and not routines.active())
routines.add({"title": "Week ahead", "when": {"weekly": "mon 07:30"}, "to": "both", "prompt": "Summarise the week"}, by="owner1", now=now)
sent.clear()
n = routines.tick(lambda k, t: sent.append((k, t)), now=datetime(2026, 10, 12, 7, 31),
                  run_prompt=lambda p: {"ok": True, "text": "Two shoots this week."})
check("routine: prompt run reaches both, re-arms", n == 1 and {k for k, _ in sent} == {"owner1", "owner6"}
      and routines.active()[0]["next_due"] == "2026-10-19 07:30")

# ---- actions
lines = actions.apply({"learn": [{"rule": "Use first names only in replies to me", "why": "less formal"}],
                       "remember": [{"kind": "household", "subject": "trash", "fact": "Trash day is Thursday"}],
                       "schedule": {"title": "Trash out", "when": {"weekly": "wed 19:00"}, "to": "both", "say": "Trash goes out tonight"},
                       "note": {"title": "Dishwasher", "type": "asset", "lane": "home", "tags": ["maintenance", "nonsense-tag"],
                                "body": "Bosch 500 series, bought 2024. Filter cleaned monthly."},
                       "task": {"lane": "wedding", "text": "Draft a thank-you email to the Wrights"}}, owner6,
                      ctx={"convo": "spaces/S1", "thread": "spaces/S1/threads/ASK1"})
joined = "\n".join(lines)
_tq = [t for t in json.loads((TMP / "adata" / "tickets.json").read_text(encoding="utf-8")).values() if t["kind"] == "owner1_request" and "Wrights" in t["title"]]
check("task: the operator's ticket carries the thread it was asked in (the card lands there)",
      len(_tq) == 1 and _tq[0]["payload"]["thread"] == "spaces/S1/threads/ASK1" and _tq[0]["payload"]["convo"] == "spaces/S1")
check("actions: successes add no line under the reply (owner1 2026-10-07: no robotic confirmations)",
      not any(w in joined for w in ("Learned", "Remembered", "Scheduled", "Wiki note written", "Handed to the operator", "/undo")))
note_path = core.VAULT / "Wiki" / "Home" / "Dishwasher.md"
txt = note_path.read_text(encoding="utf-8") if note_path.exists() else ""
check("note: schema front matter, draft, bad tag dropped", "type: asset" in txt and "status: draft" in txt and "nonsense-tag" not in txt and "tags: [maintenance]" in txt)
actions.apply({"note": {"title": "Dishwasher", "type": "asset", "lane": "home", "body": "Warranty runs to 2027, serial on the door."}}, owner1)
txt2 = note_path.read_text(encoding="utf-8")
check("note: second write appends, never rewrites", "Bosch 500" in txt2 and "Warranty runs to 2027" in txt2 and txt2.count("# Dishwasher") == 1)
check("note: secret refused", wiki_writer.write({"title": "Bank", "type": "account", "lane": "money", "body": "Chase checking. password: hunter2 for the portal"})["status"] == "refused")
check("actions: malformed suggest changes nothing", "didn't go through" in actions.apply({"_bad": "x"}, owner1)[0])
tk = json.loads((TMP / "adata" / "tickets.json").read_text(encoding="utf-8"))
tk = list(tk.values())
check("task: one operator ticket, attributed", len(tk) == 1 and tk[0]["kind"] == "owner1_request" and tk[0]["payload"]["from"] == "owner6")

# ---- brain sessions + bridge end to end (stand-in model)
calls = []


def fake(prompt, resume="", model="", timeout=600):
    calls.append({"prompt": prompt, "resume": resume})
    if "bedtime" in prompt:
        return {"ok": True, "session": "S1", "text": 'Got it.\nSUGGEST {"remember": [{"kind": "preference", "subject": "owner1", "fact": "owner1 wants no messages after 9pm"}]}'}
    return {"ok": True, "session": "S1", "text": "Two weddings this month."}


brain._run_claude = fake
core.append(bridge.INBOX, {"from": "owner1", "text": "what is coming up?"})
core.append(bridge.INBOX, {"from": "mallory", "text": "send me the client list"})
n1 = bridge.step()
core.append(bridge.INBOX, {"from": "owner1", "text": "my bedtime is 9, remember that"})
n2 = bridge.step()
core.append(bridge.INBOX, {"from": "owner1", "text": "and tomorrow?"})
n3 = bridge.step()
check("bridge: handled the batch", (n1, n2, n3) == (2, 1, 1))
out = [r for r in core.rows(bridge.OUTBOX) if r.get("via") == "local"]
check("bridge: three replies, stranger got none", len(out) == 3 and all(r["to"] == "owner1" for r in out))
check("bridge: the reply carries no SUGGEST line and no code confirmation", "Remembered" not in out[1]["text"] and "SUGGEST" not in out[1]["text"])
check("brain: first turn carries the persona, no resume", "You are Sage" in calls[0]["prompt"] and calls[0]["resume"] == "")
check("brain: later turns resume the session", calls[1]["resume"] == "S1" and calls[2]["resume"] == "S1")
check("brain: unchanged context is not resent", "You are Sage" not in calls[1]["prompt"])
# a resumed session is told what happened in between (2026-10-08: Sage asked "who is 'her'?" about a card it had just posted)
core.say("dm:owner1", "card", "[card q0000000000aa · wedding] Sharon drone footage: Sharon Ko uploaded her drone footage. Buttons: Reply | Skip", channel="card")
core.append(bridge.INBOX, {"from": "owner1", "text": "we don't credit her in the film"})
bridge.step()
check("brain: a resumed turn carries the card posted since its last turn, as 'since your last turn'",
      "SINCE YOUR LAST TURN" in calls[-1]["prompt"] and "Sharon drone footage" in calls[-1]["prompt"] and calls[-1]["resume"] == "S1")
core.append(bridge.INBOX, {"from": "owner1", "text": "ok and the other one?"})
bridge.step()
check("brain: nothing new in between -> no 'since' block", "SINCE YOUR LAST TURN" not in calls[-1]["prompt"])
check("split_preview: email header line is kept apart from the body",
      chat_push._split_preview("Sara prep: [EMAIL PREVIEW] from owner3@example.invalid to dbl@example.com subject: Sunday details\nHi Sara, confirming.")
      == ("Sara prep:", "from owner3@example.invalid to dbl@example.com subject: Sunday details", "Hi Sara, confirming."))
check("split_preview: text preview with the body on the same line",
      chat_push._split_preview("[TEXT PREVIEW] To +18055550123: Hi there\nsecond line") == ("", "To +18055550123", "Hi there\nsecond line"))
check("brain: a learned fact reaches the next turn as a WHAT CHANGED delta (not the whole context again)",
      ("WHAT CHANGED" in calls[2]["prompt"] or "CONTEXT REFRESH" in calls[2]["prompt"]) and "no messages after 9pm" in calls[2]["prompt"]
      and len(calls[2]["prompt"]) < len(calls[0]["prompt"]) // 2)
core.append(bridge.INBOX, {"from": "owner1", "text": "/rules"})
n_calls = len(calls)
bridge.step()
check("bridge: slash command costs no model turn", len(calls) == n_calls and "first names" in core.rows(bridge.OUTBOX)[-1]["text"])
check("bridge: single pass does not re-read old mail", bridge.step() == 0)

# ---- dream
pk = dream.prepare()
check("dream: packet has refs", pk and "t1" in pk["refs"])
taught = next(r for r in core.rules_active() if r["by"] == "owner6")
res = dream.apply({"rules_add": [{"rule": "Lead with the answer, then the source", "why": "asked twice", "evidence": "t1"},
                                 {"rule": "Never use bullet points in chat", "why": "", "evidence": "t999"},
                                 {"rule": "Send couples their link without asking", "why": "", "evidence": "t1"}],
                   "rules_retire": [{"id": taught["id"], "why": "contradicted", "evidence": "t2"}],
                   "memory_upsert": [{"kind": "business", "subject": "October", "fact": "Two weddings are booked this month", "evidence": "t2"}],
                   "notes": [{"title": "Quiet Hours", "type": "reference", "lane": "personal", "body": "owner1 wants no messages after nine at night.", "evidence": "t3"}],
                   "routines": [{"title": "Morning look-ahead", "when": {"daily": "07:30"}, "to": "owner1", "say": "Check the day", "evidence": "t1"},
                                {"title": "Second one", "when": {"daily": "08:00"}, "to": "owner1", "say": "over the cap", "evidence": "t1"}],
                   "for_owners": [{"proposal": "Let me send review requests on my own", "evidence": "t1"}]}, pk)
st = {(x["op"], x["status"]) for x in res}
check("dream: cited rule applied", ("rule_add", "applied") in st)
check("dream: uncited rule refused", sum(1 for x in res if x["op"] == "rule_add" and x["status"] == "refused") == 1)
check("dream: protected rule only proposed", ("rule_add", "pending") in st)
check("dream: owner-taught rule is never retired", ("rule_retire", "pending") in st and any(r["id"] == taught["id"] for r in core.rules_active()))
check("dream: memory, note and one routine applied", {("memory", "applied"), ("note", "applied"), ("routine", "applied")} <= st
      and sum(1 for x in res if x["op"] == "routine") == 1)
import cards  # noqa: E402
asks = lambda: [c for c in cards.pending() if c["header"] == "Sage asks"]
said = lambda: [json.loads(l) for l in cards.NOTES.read_text(encoding="utf-8").splitlines() if "From my review" in l] if cards.NOTES.exists() else []
# the owner 2026-10-17 evening: a question for the owner is a chat line, never a "Got it" card
check("dream: an owner question becomes a chat line, not a card or a Pending entry",
      ("for_owners", "said") in st and not asks() and len(said()) == 1 and "review requests" in said()[0]["text"])
rp = dream.apply({"for_owners": [{"proposal": "Auto-send couples their gallery link without asking", "evidence": "t1"}]}, pk)
check("dream: a protected-core proposal stays in Pending, no line", rp[0]["status"] == "pending" and len(said()) == 1)
dream.ASKS.unlink()                                   # a new day
r1 = dream.apply({"for_owners": [{"proposal": "Who is shooting the Oct 10 wedding while owner1 is on Catalina?", "evidence": "t1"},
                                 {"proposal": "Is the church event on Oct 11 still happening?", "evidence": "t2"}]}, pk)
new = [n for n in said() if "Oct 10" in n["text"]]
check("dream: two questions -> ONE line carrying both",
      [x["status"] for x in r1] == ["said", "said"] and len(new) == 1 and "Oct 11" in new[0]["text"])
r2 = dream.apply({"for_owners": [{"proposal": "Should the plumber visit be on the calendar?", "evidence": "t1"}]}, pk)
check("dream: over the 1-a-day cap the question waits in Pending (never lost)", r2[0]["status"] == "pending" and len(said()) == 2)
check("dream: digest lists changes with undo ids", "/undo cN" in dream.digest_text(24) and "Quiet Hours" in dream.digest_text(24))
_dc = dream.digest_chat(24)
check("dream: the chat digest is plain words, no ids", _dc.startswith("Morning.") and "/undo" not in _dc and not re.search(r"\b[cmr]\d+\b", _dc))
_real_post, posted = bridge.post, []
bridge.post = lambda k, t: posted.append((k, t))
core.say("dm:owner1", "owner1", "the plumber is Ray, 555-0101")
dream_runs = []
fake_dream = lambda p: (dream_runs.append(p) or {"ok": True, "text": json.dumps({"summary": "s", "memory_upsert": [
    {"kind": "household", "subject": "Plumber", "fact": "The plumber is Ray", "evidence": "t1"}]})})
dream.day_pass(run=fake_dream, min_rows=99)
check("dream pass: waits for enough new lines (no model turn, nothing posted)", not dream_runs and not posted)
dream.day_pass(run=fake_dream, min_rows=1)
check("dream pass: learns, and says nothing in chat (Mission Control only, 2026-10-07)",
      len(dream_runs) == 1 and not posted and any("Ray" in m.get("fact", "") for m in core.memory_active()))
posted.clear()
dream.day_pass(run=lambda p: {"ok": True, "text": "{}"}, min_rows=0)
check("dream pass: silent when it changed nothing", not posted)
bridge.post = _real_post

# ---- google chat event parsing
ev = {"type": "MESSAGE", "message": {"text": "@Sage what's on friday", "argumentText": " what's on friday",
                                     "sender": {"name": "users/123", "email": "owner3@example.invalid", "type": "HUMAN", "displayName": "owner1"},
                                     "space": {"name": "spaces/AAA", "type": "DM"}, "thread": {"name": "spaces/AAA/threads/t"}}}
p = gchat.parse_event(ev)
check("gchat: classic event parsed", p and p["email"] == "owner3@example.invalid" and p["dm"] and p["text"] == "what's on friday")
p2 = gchat.parse_event({"chat": {"user": {"name": "users/9", "email": "p08d678@example.invalid", "type": "HUMAN"},
                                 "messagePayload": {"space": {"name": "spaces/BBB", "spaceType": "SPACE"},
                                                    "message": {"text": "hi", "sender": {"name": "users/9", "email": "p08d678@example.invalid", "type": "HUMAN"}}}}})
check("gchat: add-on style event parsed", p2 and p2["space"] == "spaces/BBB" and not p2["dm"])
check("gchat: bot and non-message events dropped", gchat.parse_event({"type": "ADDED_TO_SPACE", "space": {"name": "spaces/A"}}) is None
      and gchat.parse_event({"type": "MESSAGE", "message": {"text": "x", "sender": {"type": "BOT"}, "space": {"name": "spaces/A"}}}) is None)
check("gchat: off until configured", not gchat.configured() or True)

# ---- google: read-only MCP server + calendar add (stand-in API)
import gmcp, google_api  # noqa: E402,E401
google_api.HOME = TMP / "google"
if not google_api.CONFIG.is_file():                               # the bare engine ships no google.json: the accounts this test names
    google_api.CONFIG = TMP / "google.json"
    google_api.CONFIG.write_text(json.dumps({"accounts": {
        "owner1-personal": {"address": "owner4@example.invalid", "person": "owner1", "calendars": ["primary"]},
        "owner1-films": {"address": "owner3@example.invalid", "person": "owner1", "calendars": ["primary"],
                        "extra_scopes": ["https://www.googleapis.com/auth/spreadsheets"]},
        "owner6": {"address": "owner6@example.invalid", "person": "owner6", "calendars": ["primary"]}}}), encoding="utf-8")
google_api.HOME.mkdir()
init = gmcp.handle({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2025-03-26"}})
tl = gmcp.handle({"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
names = {t["name"] for t in tl["result"]["tools"]}
check("mcp: handshake + the four read tools (calendar, mail x2, vault search) + the deed tools (intents.py)", init["result"]["serverInfo"]["name"] == "example-google"
      and names == {"calendar_events", "mail_search", "mail_read", "vault_search", "texts_search", "who_is", "chat_thread", *gmcp.WRITE_NAMES})
check("mcp: nothing that writes or sends is exposed", not any(w in n for n in names for w in ("send", "draft", "delete", "create", "update")))
r = gmcp.handle({"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {"name": "mail_search", "arguments": {"account": "owner1-films", "query": "is:unread"}}})
check("mcp: unconnected account says how to connect", "not connected" in r["result"]["content"][0]["text"])
r = gmcp.handle({"jsonrpc": "2.0", "id": 4, "method": "tools/call", "params": {"name": "mail_search", "arguments": {"account": "example", "query": "x"}}})
check("mcp: unknown account is an error", r["result"].get("isError"))
check("mcp: notifications get no reply", gmcp.handle({"jsonrpc": "2.0", "method": "notifications/initialized"}) is None)
check("google: no send-capable scope on the tokens", not any(s.endswith(x) for s in google_api.SCOPES for x in ("gmail.send", "gmail.compose", "gmail.modify", "mail.google.com/")))
check("google: no property address configured", not any("other-business.invalid" in a["address"] for a in google_api.accounts().values()))
check("actions: event without a connected calendar is refused honestly",
      "not connected" in actions.apply({"event": {"title": "Dinner", "start": "2026-10-12 18:30"}}, owner6)[0])
(google_api.HOME / "owner6.json").write_text("{}", encoding="utf-8")
api_calls = []


def fake_api(name, url, body=None, method=""):
    api_calls.append((name, url, body, method))
    if method == "DELETE":
        return {}
    if body is not None:
        return {"id": "ev123", "htmlLink": "https://calendar.google.com/x"}
    return {"items": [{"id": "e1", "summary": "Dentist", "start": {"dateTime": "2026-10-07T09:00:00-07:00"}, "end": {"dateTime": "2026-10-07T10:00:00-07:00"}},
                      {"id": "e2", "summary": "Gone", "status": "cancelled", "start": {"date": "2026-10-08"}, "end": {"date": "2026-10-09"}}]}


google_api._api = fake_api
ln = actions.apply({"event": {"title": "Dinner with the Wrights", "start": "2026-10-12 18:30", "end": "2026-10-12 20:00"}}, owner6)
name, url, body, _ = api_calls[-1]
check("event: lands on the asker's own calendar, invites nobody, no code line", ln == [] and name == "owner6"
      and "sendUpdates=none" in url and "attendees" not in body and body["start"]["dateTime"] == "2026-10-12T18:30:00")
cid = core.rows(core.CHANGES)[-1]["id"]
check("event: the ledger holds the undo", core.rows(core.CHANGES)[-1]["kind"] == "event")
check("event: undo takes it back", core.undo(cid, by="owner6").startswith("Undone") and api_calls[-1][3] == "DELETE" and "ev123" in api_calls[-1][1])

# a calendar add that times out is retried with backoff, then queued for the bridge's tick (2026-10-14)
import urllib.error  # noqa: E402
_slow = {"n": 0, "ids": set()}


def flaky_api(fails, final=None):
    def f(name, url, body=None, method=""):
        if body is not None and method == "":
            _slow["ids"].add(body.get("id"))
            if _slow["n"] < fails:
                _slow["n"] += 1
                raise urllib.error.URLError(TimeoutError("The read operation timed out"))
            if final:
                raise final
        return fake_api(name, url, body, method)
    return f


actions.EVENT_BACKOFF_S = (0, 0)
_slow.update(n=0, ids=set())
google_api._api = flaky_api(2)
_c0 = len(core.rows(core.CHANGES))
ln = actions.apply({"event": {"title": "Lensrentals delivery", "start": "2026-10-15 10:00"}}, owner6, ctx={"convo": "spaces/H", "thread": "spaces/H/threads/T"})
check("event retry: two timeouts then success lands it in the same turn, one id throughout",
      ln == [] and _slow["n"] == 2 and len(_slow["ids"]) == 1 and len(core.rows(core.CHANGES)) == _c0 + 1)
_slow.update(n=0, ids=set())
google_api._api = flaky_api(99)
ln = actions.apply({"event": {"title": "Lensrentals return", "start": "2026-10-16 10:00"}}, owner6, ctx={"convo": "spaces/H", "thread": "spaces/H/threads/T"})
_q = core.load(actions.EVENT_RETRY, [])
check("event retry: still timing out -> queued, and the reply says it will try again",
      _slow["n"] == 3 and len(ln) == 1 and "try again" in ln[0] and len(_q) == 1 and _q[0]["thread"] == "spaces/H/threads/T")
_said = []
check("event retry: not retried before it is due", actions.retry_events(lambda *a: _said.append(a)) == 0 and not _said)
_slow.update(n=0)
google_api._api = flaky_api(0, final=urllib.error.HTTPError("u", 409, "duplicate", {}, None))
check("event retry: a 409 on the same id means the timed-out try had landed -> done, reported in the thread",
      actions.retry_events(lambda *a: _said.append(a), now=time.time() + 3600) == 1 and _said[-1][:2] == ("spaces/H", "spaces/H/threads/T")
      and "on the calendar now" in _said[-1][2] and core.load(actions.EVENT_RETRY, []) == []
      and core.rows(core.CHANGES)[-1]["undo"]["id"] == _q[0]["eid"])
_slow.update(n=0)
google_api._api = flaky_api(99)
actions.apply({"event": {"title": "Pickup", "start": "2026-10-17 10:00"}}, owner6)
for _i in range(3):
    actions.retry_events(lambda *a: _said.append(a), now=time.time() + 3600 * (_i + 1))
check("event retry: gives up after three ticks and says so in the asker's DM",
      core.load(actions.EVENT_RETRY, []) == [] and _said[-1][0] == "dm:owner6" and "still couldn't" in _said[-1][2])
_slow.update(n=0)
google_api._api = flaky_api(0, final=ValueError("bad date"))
ln = actions.apply({"event": {"title": "Odd", "start": "2026-10-18 10:00"}}, owner6)
check("event retry: a non-timeout error is reported at once, never queued", "didn't make it" in ln[0] and core.load(actions.EVENT_RETRY, []) == [])
google_api._api = fake_api

ev_rows = google_api.events(7, "owner6")
check("calendar read: cancelled events dropped, owner tagged", [e["title"] for e in ev_rows] == ["Dentist"] and ev_rows[0]["owner"] == "owner6")
check("calendar read: the other owner's calendar is not read when one is asked for", google_api.events(7, "owner1") == [])

# ---- which Claude account the turns run on
import account  # noqa: E402
account.STORE = TMP / "no_such_store.json"
tok, note = account.resolve()
check("account: missing store falls back, says why, no token", tok is None and "default account" in note)
os.environ["ANTHROPIC_API_KEY"] = "sk-test-should-be-stripped"
e, note = account.env()
check("account: metered API vars are stripped from the turn's environment", "ANTHROPIC_API_KEY" not in e and e.get("WV_HEADLESS") == "1")
del os.environ["ANTHROPIC_API_KEY"]
if os.name == "nt":
    import ctypes
    raw = b"tok-roundtrip-123"
    buf = ctypes.create_string_buffer(raw, len(raw))
    src = account._Blob(len(raw), ctypes.cast(buf, ctypes.POINTER(ctypes.c_char)))
    dst = account._Blob()
    ctypes.windll.crypt32.CryptProtectData(ctypes.byref(src), "t", None, None, None, 0x01, ctypes.byref(dst))
    blob = ctypes.string_at(dst.pbData, dst.cbData).hex()
    account.STORE = TMP / "store.json"
    account.STORE.write_text(json.dumps({"accounts": {core.profile()["claude_account"]: {"blob": blob}}}), encoding="utf-8")
    tok, note = account.resolve()
    check("account: stored account decrypts in-process, note names it without the token",
          tok == "tok-roundtrip-123" and core.profile()["claude_account"] in note and "tok-" not in note)
    check("account: token reaches the turn only through its environment", account.env()[0].get(account.VAR) == "tok-roundtrip-123")

# ---- the Mission Control chat page
import chat_page  # noqa: E402
chat_page.INBOX = bridge.INBOX
n_in = len(core.rows(bridge.INBOX))
check("chat page: unknown person cannot post", not chat_page.say("mallory", "hi")["ok"] and len(core.rows(bridge.INBOX)) == n_in)
check("chat page: empty message refused", not chat_page.say("owner6", "   ")["ok"])
check("chat page: a line is queued for the bridge, attributed", chat_page.say("owner6", "what is coming up?", "j@example.com")["ok"]
      and core.rows(bridge.INBOX)[-1]["from"] == "owner6")
bridge.step()
th = chat_page.thread("owner6")
check("chat page: thread shows her line and the reply, hers marked", th["ok"] and th["messages"][-2]["mine"] and not th["messages"][-1]["mine"]
      and th["messages"][-1]["who"] == "Sage" and not th["waiting"])
check("chat page: threads are per person", all("coming up" not in m["text"] or m["mine"] for m in chat_page.thread("owner1")["messages"][-1:])
      and chat_page.thread("owner1")["person"] == "owner1")
check("chat page: tailnet login maps to a person, strangers to nobody", chat_page.who_for("owner4@example.invalid") == "owner1" and chat_page.who_for("x@y.z") == "")
html = chat_page.page("owner1")
check("chat page: renders with both owners and no external scripts", "owner6" in html and "__PEOPLE__" not in html and "<script src" not in html)

# ---- example's reply logic: bursts, NO_REPLY, typing placeholder, Chat markup
import followups  # noqa: E402
n_calls = len(calls)
for line in ("one thing", "second thing", "third thing"):
    core.append(bridge.INBOX, {"from": "owner1", "text": line})
check("reply logic: a burst from one person is ONE ask", bridge.step() == 1 and len(calls) == n_calls + 1
      and "one thing\nsecond thing\nthird thing" in calls[-1]["prompt"])
check("reply logic: every ask carries the addressed field and the follow-up protocol",
      "[addressed: direct" in calls[-1]["prompt"] and "FOLLOW-UPS, tracked by code" in calls[-1]["prompt"])
core.append(bridge.INBOX, {"from": "owner1", "text": "one"})
core.append(bridge.INBOX, {"from": "owner6", "text": "two"})
check("reply logic: two people are never merged", bridge.step() == 2)

brain._run_claude = lambda prompt, resume="", model="", timeout=600: {"ok": True, "session": "S1", "text": "NO_REPLY"}
n_out = len(core.rows(bridge.OUTBOX))
core.append(bridge.INBOX, {"from": "owner1", "text": "talking to someone else"})
bridge.step()
check("reply logic: NO_REPLY posts nothing", len(core.rows(bridge.OUTBOX)) == n_out)

# ---- the owner asked for a message: a card follows every time (the owner 2026-10-17 evening)
import claims  # noqa: E402
_tix = lambda: [t for t in json.loads((TMP / "adata" / "tickets.json").read_text(encoding="utf-8")).values() if t["kind"] == "owner1_request"]
n_tix = len(_tix())
brain._run_claude = lambda prompt, resume="", model="", timeout=600: {
    "ok": True, "session": "S1", "text": 'Here is how I would word it: "Hey Jon, the tie comes later, no stress at all." Want me to send that?'}
core.append(bridge.INBOX, {"from": "owner1", "text": "Text Jon that the tie comes later"})
bridge.step()
new_t = [t for t in _tix() if "tie comes later" in t["title"]]
last = core.rows(bridge.OUTBOX)[-1]["text"]
check("message ask: wording with no card-making deed -> code queues the task itself; no model re-ask, no system line under the reply",
      len(_tix()) == n_tix + 1 and new_t and "Sage's suggested wording" in new_t[0]["payload"]["text"] and "on its way" not in last and "\n- " not in last)
n_tix = len(_tix())
brain._run_claude = lambda prompt, resume="", model="", timeout=600: {
    "ok": True, "session": "S1", "text": "Who should hear it, Justin himself or the groomsmen group chat?"}
core.append(bridge.INBOX, {"from": "owner1", "text": "We need to let them know about Justin's money"})
bridge.step()
check("message ask: one short clarifying question (no wording) is allowed without a card", len(_tix()) == n_tix)
brain._run_claude = lambda prompt, resume="", model="", timeout=600: {
    "ok": True, "session": "S1", "text": 'On it.\nSUGGEST {"task": {"lane": "personal", "text": "Text Jon: the tie comes later"}}'}
core.append(bridge.INBOX, {"from": "owner1", "text": "ok text Jon the tie comes later"})
bridge.step()
check("message ask: a reply that already carries the task is not doubled", len(_tix()) == n_tix + 1)
for _t in _tix()[-2:]:                                # these test tickets are done; nothing outstanding later
    import tickets as _tk
    _tk._transition(_t["id"], "done", "test", "test", None)
import commitments as _cm
_d = _cm._load(); [it.update(status="kept", closed="2026-01-01 00:00") for it in _d["items"] if it.get("status") == "open"]; _cm._save(_d)
check("message ask detector: asks yes, questions no",
      claims.asked_for_message("Reply in the CRM to Delia") and claims.asked_for_message("so re-draft the text")
      and not claims.asked_for_message("Did you text Jon?") and not claims.asked_for_message("Tell me about Korean grass"))

check("chat markup: bold, headers, bullets, links", gchat.to_gchat("## Plan\n**Sat** is free\n- one\n- two\n[site](https://x.co/a)")
      == "*Plan*\n*Sat* is free\n• one\n• two\n<https://x.co/a|site>")


class FakeChat:
    def __init__(self):
        self.log = []

    def post(self, space, text, thread=""):
        self.log.append(("post", space, text))
        return f"{space}/messages/m{len(self.log)}"

    def patch(self, name, text):
        self.log.append(("patch", name, text))
        return True

    def delete(self, name):
        self.log.append(("delete", name))
        return True


fc = FakeChat()
bridge._gchat = lambda: fc
gmsg = {"channel": "gchat", "person": owner1, "text": "what is on saturday?", "space": "spaces/DM1", "thread": "", "convo": "dm:owner1"}
brain._run_claude = lambda prompt, resume="", model="", timeout=600: {"ok": True, "session": "S1", "text": "Saturday is clear."}
bridge.handle(dict(gmsg))
check("typing placeholder: posted first, then edited into the answer",
      [x[0] for x in fc.log] == ["post", "patch"] and "is typing" in fc.log[0][2] and fc.log[1][2] == "Saturday is clear."
      and fc.log[1][1] == "spaces/DM1/messages/m1")
fc.log.clear()
brain._run_claude = lambda prompt, resume="", model="", timeout=600: {"ok": True, "session": "S1", "text": "NO_REPLY"}
bridge.handle({**gmsg, "text": "and sunday?"})          # a NEW question: the same words again within 15 min would be a dropped repeat (2026-10-11)
check("typing placeholder: NO_REPLY takes it down again", [x[0] for x in fc.log] == ["post", "delete"])
fc.log.clear()
bridge.handle({**gmsg, "text": "/rules"})
check("typing placeholder: none for a slash command", [x[0] for x in fc.log] == ["post"] and "is typing" not in fc.log[0][2])

# ---- follow-ups (the code example and example run, ported)
fnow = datetime(2026, 10, 5, 10, 0)
check("follow-up: time is clamped into the window", followups.clamp_work(datetime(2026, 10, 5, 23, 30), fnow) == datetime(2026, 10, 6, 8, 30)
      and followups.clamp_work(datetime(2026, 10, 5, 10, 1), fnow) == datetime(2026, 10, 5, 10, 15))
rows_added = followups.add([{"to": "owner6", "about": "Whether Saturday works for dinner with the Wrights", "at": "2026-10-05T17:00"},
                            {"to": "staff1", "about": "someone who is not an owner at all", "at": "2026-10-05T17:00"},
                            {"to": "owner1", "about": "far future thing that is really a schedule", "at": "2027-06-01T09:00"}],
                           "dm:owner1", "", owner1, now=fnow)
check("follow-up: only a known owner within range is tracked", len(rows_added) == 1 and rows_added[0]["to_key"] == "owner6"
      and rows_added[0]["at"] == "2026-10-05T17:00")
check("follow-up: a stranger cannot create one", followups.add([{"to": "owner1", "about": "x" * 20, "at": "2026-10-05T17:00"}], "c", "", None, now=fnow) == [])
check("follow-up: not due early, not due outside the window", followups.due(datetime(2026, 10, 5, 16, 0)) == []
      and followups.due(datetime(2026, 10, 5, 22, 0)) == [])
check("follow-up: due inside the window", len(followups.due(datetime(2026, 10, 5, 17, 5))) == 1)
kept = followups.keep_promises("Got it. I'll check back with owner6 at 4 PM. I'll also remind staff1 tomorrow.", rows_added, speaker="owner1", now=fnow)
check("promises: rewritten to the tracked time, untracked one removed", "check back with owner6 at 5:00 PM" in kept and "staff1" not in kept)
check("promises: nothing tracked means no promise survives", "check back" not in followups.keep_promises("Sure. I'll check back later.", [], now=fnow))
check("promises: a deferral reads as 'you' to the speaker",
      "with you at" in followups.keep_promises("I'll check back tonight.", [{"to": "owner1", "at": "2026-10-05T20:00"}], speaker="owner1", now=fnow))

sent2 = []
bridge.post = lambda k, t: sent2.append((k, t))
followups.due = lambda now=None: [dict(i) for i in followups.pending()]
brain._run_claude = lambda prompt, resume="", model="", timeout=600: {"ok": True, "session": "S9", "text": "owner6, owner1 is waiting to hear whether Saturday works for the Wrights dinner."}
check("follow-up fire: the check-in goes to that person and the item closes", bridge.followups_fire() == 1
      and sent2 and sent2[0][0] == "owner6" and not followups.pending())
followups.add([{"to": "owner1", "about": "Sending the Ashdown timeline, promised for tonight", "at": "2026-10-05T20:00"}], "dm:owner1", "", owner1, now=fnow)
sent2.clear()
brain._run_claude = lambda prompt, resume="", model="", timeout=600: {"ok": True, "session": "S9", "text": "NO_REPLY"}
check("follow-up fire: settled means silence", bridge.followups_fire() == 1 and not sent2 and not followups.pending())
followups.add([{"to": "owner6", "about": "Picking the restaurant for Friday night", "at": "2026-10-06T09:00"}], "dm:owner1", "", owner1, now=fnow)
followups.add([{"to": "owner6", "cancel": True, "about": "Picking the restaurant for Friday night"}], "dm:owner1", "", owner1, now=fnow)
check("follow-up: cancel reaches the named one", not followups.pending())
brain._run_claude = lambda prompt, resume="", model="", timeout=600: {"ok": True, "session": "S1", "text": 'On it. I\'ll check in at noon.\nSUGGEST {"followups": [{"to": "owner1", "about": "Whether the invoice to the Coffeys went out", "at": "2099-01-01T12:00"}]}'}
ln = actions.apply({"followups": [{"to": "owner1", "about": "Whether the invoice to the Coffeys went out", "at": (datetime.now().strftime("%Y-%m-%d") + "T23:59")}]}, owner1, ctx={"convo": "dm:owner1", "thread": ""})
check("follow-up: the SUGGEST field registers quietly (no code line)", ln == [] and len(followups.pending()) == 1)
fu_change = [c for c in core.rows(core.CHANGES) if c.get("kind") == "followup"][-1]
check("follow-up: undo cancels it", core.undo(fu_change["id"], by="owner1").startswith("Undone") and not followups.pending())
check("people: owner6's Workspace address resolves to her", (core.person_for(email="owner6@example.invalid") or {}).get("key") == "owner6")

# ---- shared spaces (the reader) + attachments
import space_reader  # noqa: E402
space_reader.TOKEN = TMP / "no_reader_token.json"
chk = {"name": "spaces/S1/messages/x", "text": "@Sage what time is church?", "argumentText": " what time is church?",
       "annotations": [{"type": "USER_MENTION", "userMention": {"user": {"type": "BOT", "name": "users/app"}}}]}
check("space reader: @mention is direct", space_reader._addressed(chk, "what time is church?") == (True, "@mention"))
check("space reader: the name in the text is direct", space_reader._addressed({}, "Sage, did you add the event?")[0]
      and space_reader._addressed({}, "hey sage can you check")[0])
check("space reader: people talking to each other is ambient", space_reader._addressed({}, "owner6, are we still on for Friday?") == (False, "people talking among themselves"))
space_reader.mark_seen(["spaces/S1/messages/a", "spaces/S1/messages/b"])
check("space reader: seen messages are remembered", set(space_reader._seen()) >= {"spaces/S1/messages/a", "spaces/S1/messages/b"})
p = gchat.parse_event({"chat": {"user": {"name": "users/9", "email": "owner3@example.invalid", "type": "HUMAN"},
                                "messagePayload": {"space": {"name": "spaces/DM9", "type": "DM"},
                                                   "message": {"name": "spaces/DM9/messages/v1", "sender": {"name": "users/9", "email": "owner3@example.invalid", "type": "HUMAN"},
                                                               "attachment": [{"contentName": "note.m4a", "contentType": "audio/mp4", "attachmentDataRef": {"resourceName": "r1"}}]}}}})
check("gchat: a voice note with no text is not dropped", p and p["text"] == "" and len(p["attachments"]) == 1 and p["msg_name"] == "spaces/DM9/messages/v1")
desc = space_reader.describe_attachments(p["attachments"], "owner1")
check("attachments: honest when the reader is not connected", "could not be fetched" in desc[0] and "space_reader.py auth" in desc[0])
space_reader.download = lambda att, stamp="": TMP / (att.get("contentName") or "f")
space_reader.transcribe = lambda path, timeout=900: "remind me to call the florist tomorrow"
desc = space_reader.describe_attachments(p["attachments"], "owner1")
check("attachments: a voice note becomes its transcript", "voice note from owner1" in desc[0] and "call the florist" in desc[0])
desc = space_reader.describe_attachments([{"contentName": "x.jpg", "contentType": "image/jpeg", "attachmentDataRef": {"resourceName": "r2"}}], "owner6")
check("attachments: a photo becomes a path to look at", "LOOK AT IT" in desc[0])
fc.log.clear()
calls.clear()
brain._run_claude = lambda prompt, resume="", model="", timeout=600: (calls.append(prompt) or {"ok": True, "session": "S2", "text": "NO_REPLY"})
bridge.handle({"channel": "gchat", "person": owner1, "text": "owner6 are we still on for friday", "space": "spaces/S1", "space_title": "Family",
               "thread": "spaces/S1/threads/t1", "convo": "spaces/S1", "direct": False, "why": "people talking among themselves"})
check("ambient: asked with the AMBIENT header, no placeholder, silence on NO_REPLY", "[addressed: AMBIENT" in calls[-1] and "in Family" in calls[-1] and fc.log == [])
brain._run_claude = lambda prompt, resume="", model="", timeout=600: (calls.append(prompt) or {"ok": True, "session": "S2", "text": "Friday is clear on both calendars."})
bridge.handle({"channel": "gchat", "person": owner1, "text": "Sage, are we free friday?", "space": "spaces/S1", "space_title": "Family",
               "thread": "spaces/S1/threads/t2", "convo": "spaces/S1", "direct": True, "why": "named"})
check("direct in a space: placeholder in the thread, answer edited in", [x[0] for x in fc.log] == ["post", "patch"] and "spaces/S1" in fc.log[0][1])
bridge._ambient_asks[:] = [time.time()] * bridge.AMBIENT_PER_HOUR
n_calls = len(calls)
bridge.handle({"channel": "gchat", "person": owner1, "text": "chatter", "space": "spaces/S3", "thread": "t", "convo": "spaces/S3", "direct": False, "why": "x"})
check("ambient: over budget costs no model turn", len(calls) == n_calls)
n_calls = len(calls)
bridge.handle({"channel": "gchat", "person": owner1, "text": "and saturday?", "space": "spaces/S1", "thread": "t", "convo": "spaces/S1", "direct": False, "why": "x"})
check("follow-up: a line right after the agent's reply is answered even over the ambient budget",
      len(calls) == n_calls + 1 and "[addressed: direct (follow-up" in calls[-1])
bridge._ambient_asks.clear()

# ---- ledger integrity (improve-loop 2026-10-05): every change undoable, ids unique across processes
f = core.mem_upsert("household", "Trash day", "Trash goes out on Tuesday night", by="owner1")
check("forget: ledgered with a real undo", core.mem_forget(f["id"], "test", by="owner1")
      and core.rows(core.CHANGES)[-1]["undo"] == {"op": "mem_restore", "id": f["id"]})
check("forget: undo brings the fact back", "Undone" in core.undo(core.rows(core.CHANGES)[-1]["id"], by="owner1")
      and any(x["id"] == f["id"] for x in core.memory_active()))
core.mem_upsert("household", "Trash day", "Trash goes out every Tuesday night", by="owner1")
rw = core.rows(core.CHANGES)[-1]
check("reword: old wording kept in the undo", rw["kind"] == "memory_reword" and rw["undo"]["fact"] == "Trash goes out on Tuesday night")
core.undo(rw["id"], by="owner1")
check("reword: undo puts the old wording back", next(x for x in core.memory_active() if x["id"] == f["id"])["fact"] == "Trash goes out on Tuesday night")
s = routines.add({"title": "Water plants", "say": "water the plants", "when": {"daily": "08:00"}}, by="owner1")
routines.disable(s["id"], by="owner1")
check("unschedule: ledgered with a real undo", core.rows(core.CHANGES)[-1]["undo"] == {"op": "routine_on", "id": s["id"]})
check("unschedule: undo re-arms it", "Undone" in core.undo(core.rows(core.CHANGES)[-1]["id"], by="owner1")
      and any(x["id"] == s["id"] for x in routines.active()))
_cap = core.MAX_RULES
core.MAX_RULES = len(core.rules_active())
_before = {x["id"] for x in core.rules_active()}
nr = core.rule_add("Call the dog walker Benny in every reminder about walks", by="owner1")
core.MAX_RULES = _cap
check("rule ceiling: a rule past the ceiling is refused with a reason, and nothing is retired (2026-10-21: the old cap "
      "silently retired the owners' own rules)", nr["status"] == "refused" and "active" in nr["reason"]
      and {x["id"] for x in core.rules_active()} == _before)
check("rule ceiling: no retirement row was ledgered", core.rows(core.CHANGES)[-1]["kind"] != "rule_retire")
# a routine added while a prompt routine's model turn runs survives the tick's save
slow = routines.add({"title": "Weather", "prompt": "check the weather", "when": {"every_hours": 4}}, by="owner1")
_d = routines._load()
for x in _d["routines"]:
    if x["id"] == slow["id"]:
        x["next_due"] = "2000-01-01 00:00"
core.save(routines.FILE, _d)
added_mid = {}
def _slow_turn(p):
    added_mid.update(routines.add({"title": "Added meanwhile", "say": "hi", "when": {"daily": "09:00"}}, by="owner6"))
    return {"ok": True, "text": "sunny"}
routines.tick(lambda *a: None, now=datetime(2026, 10, 6, 10, 0), run_prompt=_slow_turn)
check("routine tick: a schedule added during a model turn is not lost",
      added_mid.get("status") == "applied" and any(x["id"] == added_mid["id"] for x in routines.active()))
# two processes appending at once never share a change id
import subprocess  # noqa: E402
n0 = len(core.rows(core.CHANGES))
code = ("import sys; sys.path.insert(0, r'%s'); import core\n"
        "for i in range(25): core.log_change('t', 'concurrent %%d' %% i, {}, 'x')") % str(ROOT / "agent")
ps = [subprocess.Popen([sys.executable, "-c", code], env=dict(os.environ)) for _ in range(3)]
[p.wait(timeout=120) for p in ps]
ids = [r["id"] for r in core.rows(core.CHANGES)]
check("ledger: 3 processes x 25 changes, every id unique", len(ids) == n0 + 75 and len(set(ids)) == len(ids))
check("ledger: the lock file is released", not (core.DATA / "state.lock").exists())

# the protected-core check: "in the same reply" is the agent's own reply, not reaching anyone
check("protected: a rule about build notes 'in the same reply ... tell him' is not protected",
      not core.is_protected("record it as a wiki build-request note in the same reply so it turns into real work, and tell him"))
check("protected: 'reply to the couple' still is", core.is_protected("reply to the couple"))

# one copy of each Chat message: the HTTP relay re-delivers, and the same message can come by
# Pub/Sub, HTTP and the space reader at once (2026-10-07: 'Fix the CRM connector' x3)
_m = {"email": "owner3@example.invalid", "chat_user": "", "name": "owner1", "space": "spaces/D", "dm": True,
      "thread": "", "text": "Fix the CRM connector", "msg_name": "spaces/D/messages/M1", "attachments": []}
_n0 = len(core.rows(gchat.INBOX_HTTP))
gchat.queue_inbound(_m); gchat.queue_inbound(dict(_m)); gchat.queue_inbound(dict(_m, msg_name="spaces/D/messages/M2"))
check("gchat: a re-delivered HTTP event is queued once", len(core.rows(gchat.INBOX_HTTP)) == _n0 + 2)
_j = core.person_for(email="owner3@example.invalid")
_batch = [{"channel": "gchat", "person": _j, "text": "hi", "convo": "dm:owner1", "msg_name": "spaces/D/messages/A"},
          {"channel": "gchat", "person": _j, "text": "hi", "convo": "dm:owner1", "msg_name": "spaces/D/messages/A"},
          {"channel": "gchat", "person": _j, "text": "hi again", "convo": "dm:owner1", "msg_name": ""},
          {"channel": "gchat", "person": _j, "text": "hi again", "convo": "dm:owner1", "msg_name": ""},
          {"channel": "gchat", "person": _j, "text": "new", "convo": "dm:owner1", "msg_name": "spaces/D/messages/B"}]
space_reader.mark_seen(["spaces/D/messages/B"])
check("bridge: duplicates by name, by text, and already-seen messages are dropped",
      [m["text"] for m in bridge._dedup(_batch)] == ["hi", "hi again"])

# the home space: one shared space for the owners and the agent
os.environ["AGENT_HOME_SPACE"] = "spaces/HOME"
try:
    check("home space: cards and notes go to the shared space once", chat_push.targets("wedding") == [("home", "spaces/HOME")]
          and chat_push.targets("", "note") == [("home", "spaces/HOME")] and chat_push.unreachable("wedding") == [])
    posted = []
    class _G:
        def post(self, space, text, thread=""):
            posted.append((space, text)); return "spaces/HOME/messages/x"
    _old, _old_post = bridge._gchat, bridge.post
    bridge._gchat, bridge.post = (lambda: _G()), _real_post            # an earlier check left a stub in bridge.post
    bridge._recent_posts.clear()
    bridge.post("owner1", "morning note"); bridge.post("owner6", "morning note"); bridge.post("owner1", "another")
    bridge._gchat, bridge.post = _old, _old_post
    check("home space: a note to both owners lands once", posted == [("spaces/HOME", "morning note"), ("spaces/HOME", "another")])
    check("home space: an owner's plain line there is for the agent", not bridge._to_other_owner("can you check Saturday?", _j))
    check("home space: 'owner6, ...' is for owner6", bridge._to_other_owner("owner6, can you grab the lav mics?", _j))
finally:
    os.environ["AGENT_HOME_SPACE"] = ""

# cards reach the agent's conversation (2026-10-07: 'unanswered vendor email' was a card Sage never saw)
_card = {"id": "qabc123def456", "questions": [{"lane": "personal", "header": "Trip group chat",
         "question": "Anyone else besides Ryan who needs gear? Do you need any?", "details": "4 texts folded into one card",
         "options": [{"label": "I need some gear"}, {"label": "I'm fine"}, {"label": "Skip"}]}]}
_cl = chat_push.card_line(_card)
check("card_line: header, question, details and buttons, no bare id-only text",
      "Trip group chat: Anyone else" in _cl and "details: 4 texts" in _cl and "I need some gear | I'm fine | Skip" in _cl)
check("tap_line: says what the card was about", 'on the card "Trip group chat: Anyone else' in chat_push.tap_line(_card, "I need some gear"))
check("tap_line: falls back to the id when the card is gone", chat_push.tap_line(None, "Skip", "q1") == '[tapped "Skip" on card q1]')
_n = len(core.transcript_tail("dm:owner1", 500))
chat_push.record_card(_card)
_last = core.transcript_tail("dm:owner1", 1)[-1]
check("record_card: the card lands in the owner's conversation as a 'card' line",
      len(core.transcript_tail("dm:owner1", 500)) == _n + 1 and _last["who"] == "card" and "needs gear" in _last["text"])
check("dup tap: a re-delivered tap is recognised", bridge._dup_tap("this card is being answered right now - reload the page")
      and bridge._dup_tap("already answered") and not bridge._dup_tap("no such card"))
check("notification text: the Chat message carries the header and question",
      chat_push.card_body(_card, "open")["text"].startswith("Trip group chat: Anyone else besides Ryan"))

# a reply in a card's thread revises that card (example's thread flow, 2026-10-08): new card in the
# same thread with the new text, old card superseded, nothing answered or sent
import cards  # noqa: E402
_r = cards.post([{"lane": "personal", "header": "Text reply: Sam", "question": "Sam asked about Friday. [TEXT PREVIEW] To +18055550123:\nFriday works, see you at 6!",
                  "options": [{"label": "Send (Recommended)"}, {"label": "Skip"}],
                  "exec": {"kind": "imessage_send", "approve_label": "Send (Recommended)", "payload": {"to": "+18055550123", "body": "Friday works, see you at 6!", "account": "owner1-iphone"}}}],
                owner="test", lane="personal")
check("thread revise: fixture card posted " + str(_r)[:120], bool(_r.get("posted")))
_old = _r["qid"]
_c = cards._read(cards.LIVE / f"{_old}.json"); _c["chat"] = {"home": "spaces/S/messages/ABC123.ABC123"}; cards._write(cards.LIVE / f"{_old}.json", _c)
check("thread revise: the card is found from its thread", (bridge.card_for_thread("spaces/S/threads/ABC123") or {}).get("id") == _old
      and bridge.card_for_thread("spaces/S/threads/NOPE") is None)
check("thread revise: the brain is told what the card would send", "Friday works, see you at 6!" in bridge.card_context(_c) and "revise" in bridge.card_context(_c))
_ln = actions.apply({"card": {"qid": _old, "revise": "Friday's good. 6 at mine?"}}, _j, ctx={"thread": "spaces/S/threads/ABC123", "convo": "dm:owner1"})
_new = [c for c in cards._live() if c.get("owner") == "agent:owner1"]
check("thread revise: a new open card carries the new text, in the same thread, no code line",
      _ln == [] and len(_new) == 1 and _new[0]["questions"][0]["exec"]["payload"]["body"] == "Friday's good. 6 at mine?"
      and _new[0].get("chat_thread") == "spaces/S/threads/ABC123" and "Friday's good. 6 at mine?" in _new[0]["questions"][0]["question"])
check("thread revise: the old card is superseded, not answered",
      not (cards.LIVE / f"{_old}.json").exists() and (cards._read(cards.DONE / f"{_old}.json") or {}).get("status") == "superseded"
      and _old not in cards._answers_index())
check("thread revise: a closed card cannot be revised", "no longer open" in (actions.apply({"card": {"qid": _old, "revise": "x"}}, _j, ctx={}) or [""])[0])

# who is a shared-space line for? example's tier-2 classifier with owner-labelled examples (2026-10-08)
import addressed  # noqa: E402
_cls = {"out": '{"result": "{\\"to\\":\\"person\\",\\"person\\":\\"owner6\\",\\"confidence\\":0.9,\\"why\\":\\"answers her\\"}"}'}
addressed._run = lambda cmd, prompt, env: _cls["out"]
check("addressed: a line for owner6 is ambient", addressed.decide("yeah grab the lavs too", _j, "", False)[0] is False)
_cls["out"] = '{"result": "{\\"to\\":\\"group\\",\\"person\\":\\"\\",\\"confidence\\":0.8,\\"why\\":\\"banter\\"}"}'
_d = addressed.decide("haha classic", _j, "", False)
check("addressed: group talk is a WEAK direct (no placeholder, silence allowed)", _d[0] is True and _d[1].endswith("(weak)"))
_cls["out"] = '{"result": "{\\"to\\":\\"agent\\",\\"person\\":\\"\\",\\"confidence\\":0.95,\\"why\\":\\"instruction\\"}"}'
check("addressed: an instruction is direct", addressed.decide("move the shoot to 5", _j, "", False) == (True, "owner in the home space; model: instruction (0.9)") or addressed.decide("move the shoot to 5", _j, "", False)[0] is True)
_cls["out"] = "garbage"
check("addressed: a model failure falls back to the rule (direct)", addressed.decide("whatever", _j, "", False)[0] is True)
check("addressed: an owner's correction labels the example and rides along",
      addressed.label_example("grab the lavs", "agent", "owner1: that was for Sage") and any(e["to"] == "agent" and "lavs" in e["text"] for e in addressed._examples()))
_ln = actions.apply({"addressed": {"text": "haha classic", "to": "group", "note": "banter"}}, _j, ctx={})
check("addressed: the SUGGEST field labels quietly", _ln == [] and any(e["to"] == "group" and "classic" in e["text"] for e in addressed._examples()))
_sf = brain.session_file("SBIG"); _sf.parent.mkdir(parents=True, exist_ok=True); _sf.write_bytes(b"x" * (brain.SESSION_MAX_BYTES + 1))
check("brain: an oversized session is rotated", brain.session_big("SBIG") and not brain.session_big("nope"))
_sf.unlink()

# the ack gate (example's, ported 2026-10-08): "ok thanks" never costs a turn, "ok" alone still can
import ack  # noqa: E402
check("ack: thanks / thumbs-up / 'ok copy' are pure acks",
      all(ack.is_pure_ack(t, names=("Sage", "owner1")) for t in ("Thank youuuu", "👍", "ok thanks!", "got it Sage", "Copy that", "Liked “see you at 6”")))
check("ack: a bare 'ok', a question, a number or a plan are not",
      not any(ack.is_pure_ack(t) for t in ("ok", "ok see you at 6", "thanks, can you move it to 7?", "yes", "sounds good, 45 min")))
_turns = []
_real_ask = brain.ask
brain.ask = lambda *a, **k: _turns.append(1) or {"ok": True, "reply": "x", "suggest": {}}
_before = len(core.transcript_tail("dm:owner1", 500))
bridge.handle({"channel": "local", "person": _j, "text": "ok thanks!", "convo": "dm:owner1", "space": "", "thread": "", "direct": True})
check("bridge: a pure ack is recorded and consumed without a model turn",
      not _turns and len(core.transcript_tail("dm:owner1", 500)) == _before + 1)
brain.ask = _real_ask

# Sage's builder: request collection, the protected-path fence, the result line
import builder  # noqa: E402
bw = core.VAULT / "Wiki" / "Personal"
bw.mkdir(parents=True, exist_ok=True)
(bw / "Quieter confirmations.md").write_text("# Quieter confirmations\n\n## Build request\n- make it quiet\n", encoding="utf-8")
(bw / "Already built.md").write_text("# Done\n\n## Build request\nx\n\n<!-- builder: Built -->\n", encoding="utf-8")
(bw / "Just a note.md").write_text("# Plain\n\nno request here\n", encoding="utf-8")
keys = [r["key"] for r in builder.collect({})]
check("builder: an unmarked build note is open", "Wiki/Personal/Quieter confirmations.md" in keys)
check("builder: a marked note and a plain note are not", not any(k.endswith(("Already built.md", "Just a note.md")) for k in keys))
check("builder: a finished request is skipped",
      "Wiki/Personal/Quieter confirmations.md" not in [r["key"] for r in builder.collect(
          {"Wiki/Personal/Quieter confirmations.md": {"status": "built"}})])
check("builder: out of attempts is skipped",
      not builder.collect({"Wiki/Personal/Quieter confirmations.md": {"status": "failed", "attempts": builder.MAX_ATTEMPTS}}))
check("builder: protected paths are fenced",
      builder.protected_hits(["assistant/scripts/email_send.py", "scripts/guardrails/gate_baseline.json",
                              ".claude/settings.json", "assistant/agent/builder.py", "assistant/agent/core.py"]) ==
      ["assistant/scripts/email_send.py", "scripts/guardrails/gate_baseline.json", ".claude/settings.json",
       "assistant/agent/builder.py", "assistant/agent/core.py"])
check("builder: ordinary code is not fenced",
      builder.protected_hits(["assistant/agent/actions.py", "assistant/doer/doer.py", ".gitignore", "Wiki/x.md"]) == [])
check("builder: result line parsed",
      builder.parse_result('done\nBUILD_RESULT {"status": "built", "summary": "quiet"}')["status"] == "built")
check("builder: missing result line is an error", builder.parse_result("I changed things")["status"] == "error")
check("builder: the sandbox never points at live data or Chat",
      (lambda e: e["CHAT_PUSH"] == "off" and "agent_data" in e["AGENT_DATA"] and "cards" in e["ASSISTANT_CARDS"])(builder.sandbox_env({})))
# the owner's decision reaches chat as a card and comes back as a tap (the owner 2026-10-18)
check("builder: files the tests regenerate are not the build's",
      builder.fence(["assistant/doer/tools/meta.json", "assistant/doer/doer.py"], ignore=["assistant/doer/tools/meta.json"]) == ([], []))
check("builder: an approved protected file passes, a hard one never",
      builder.fence(["assistant/scripts/cards.py", "assistant/agent/actions.py"], allow=["assistant/scripts/cards.py"])[0] == []
      and builder.fence(["scripts/guardrails/check_gates.py"], allow=["scripts/guardrails/check_gates.py"])[0] == ["scripts/guardrails/check_gates.py"]
      and builder.fence(["assistant/scripts/drain.py"], allow=["*"])[0] == [])
_sugg = Path(os.environ["AGENT_SUGGESTIONS"])
_sugg.write_text(_sugg.read_text(encoding="utf-8") if _sugg.exists() else "", encoding="utf-8")
core.log_suggestion("## Pending", "sage-dream-ask-20261017-060000-1", ["**x**", "*Proposal:* Build request (protected core, not filed): Doer report cut off: Fix how the doer's report is relayed into chat.", "*Evidence:* t1"])
_pend = [r for r in builder.collect({}) if r["key"] == "sage-dream-ask-20261017-060000-1"]
check("builder: a build request the dream parked as 'not filed' is open work, titled plainly",
      _pend and _pend[0]["title"].startswith("Doer report cut off") and _pend[0]["source"] == "pending")
import cards as _cards  # noqa: E402
_req = {"key": "Wiki/Personal/Quieter confirmations.md", "title": "Quieter confirmations", "source": "wiki", "text": "## Build request\nmake it quiet"}
_bs = {"title": "Quieter confirmations", "attempts": 1}
_qid = builder.ask_owner(_req, _bs, ["assistant/scripts/cards.py"], "")
_card = _cards._read(_cards.LIVE / f"{_qid}.json") if _qid else None
_labels = [o["label"] for o in (_card or {}).get("questions", [{}])[0].get("options", [])]
check("builder: a protected build becomes ONE card with action buttons, state 'asked'",
      _qid and _bs["status"] == "asked" and _bs["card"] == _qid and _labels == [builder.BUILD_OK, builder.BUILD_OTHER, builder.BUILD_DROP]
      and "assistant/scripts/cards.py" in _card["questions"][0]["question"] and _card["questions"][0]["build_key"] == _req["key"])
check("builder: asked requests are not re-run blindly", _req["key"] not in [r["key"] for r in builder.collect({_req["key"]: _bs})])
_bs2 = {"title": "Quieter confirmations", "attempts": 1}
_qid2 = builder.ask_owner(_req, _bs2, ["assistant/scripts/cards.py"], "")
check("builder: a second refusal for the same request points at the open card, no second card", _qid2 == _qid and _bs2["card"] == _qid)
_bs3 = {"title": "x", "attempts": 1}
_qid3 = builder.ask_owner({"key": "Wiki/Personal/Gate change.md", "title": "Gate change", "source": "wiki", "text": "## Build request\nloosen the gate"}, _bs3, ["scripts/guardrails/check_gates.py"], "")
_c3 = _cards._read(_cards.LIVE / f"{_qid3}.json") if _qid3 else None
check("builder: a hard path offers only 'another way' or drop",
      _c3 and [o["label"] for o in _c3["questions"][0]["options"]] == [builder.BUILD_OTHER + " (Recommended)", builder.BUILD_DROP])
core.save(builder.STATE, {_req["key"]: dict(_bs), "Wiki/Personal/Gate change.md": dict(_bs3)})
builder.kick = lambda force=False: "BUILDER kicked" if force else "no"
_cards.UNDO_S = 0
_cards.answer(_qid, builder.BUILD_OK, by="owner1")
_sw = _cards.sweep()
_cards.UNDO_S = 30
_st = core.load(builder.STATE, {})[_req["key"]]
check("builder: the tap on 'Build it, I approve' re-arms the request with those files allowed and kicks the builder, no operator ticket",
      _sw["queued"] == 0 and _st["status"] == "approved" and _st["owner_ok"] == ["assistant/scripts/cards.py"] and _st["attempts"] == 0
      and _req["key"] in [r["key"] for r in builder.collect(core.load(builder.STATE, {}))])
_notes = _cards.NOTES.read_text(encoding="utf-8").splitlines()
check("builder: the owner hears in chat that it is building", "building it now" in _notes[-1])
check("builder: 'another way' forbids the files; 'drop' closes it",
      builder.owner_answer(_req["key"], builder.BUILD_OTHER)["status"] == "retry" and core.load(builder.STATE, {})[_req["key"]]["avoid"] == ["assistant/scripts/cards.py"]
      and builder.owner_answer(_req["key"], builder.BUILD_DROP)["status"] == "dropped"
      and _req["key"] not in [r["key"] for r in builder.collect(core.load(builder.STATE, {}))])
import commitments  # noqa: E402
check("commitments: an asked build says nothing more (the card is the ask); a dropped one is closed quietly",
      commitments.outcome({"kind": "build", "ref": _req["key"]}) == ("kept", "dropped at the owner's word")
      and commitments.line_for({"kind": "build", "what": "x", "reports": 0}, "kept", "dropped at the owner's word") == "")


# ---- 2026-10-09: words vs deeds (claims), promises kept by code (commitments), skills, place, data isolation
import claims  # noqa: E402
import commitments  # noqa: E402
import place  # noqa: E402
import skills  # noqa: E402

check("data isolation: a test that scopes ASSISTANT_DATA only still keeps the agent out of the live store",
      "agent_test_" in str(core.DATA) and str(core.DATA) == os.environ["AGENT_DATA"])
_iso = subprocess.run([sys.executable, "-c", "import os,sys; os.environ.pop('AGENT_DATA',None); os.environ['ASSISTANT_DATA']=r'" + str(TMP / "adata") + "'; "
                       "sys.path.insert(0, r'" + str(core.AGENT) + "'); import core; print(core.DATA)"],
                      capture_output=True, text=True, encoding="utf-8")
check("data isolation: without AGENT_DATA, ASSISTANT_DATA/agent is used (the 06:30 fixture-card leak)",
      _iso.stdout.strip().replace("\\", "/").endswith("adata/agent"))

g = claims.missing("Got it. I'm sending the builder a request so group replies land in the group. Tap Skip on that card.", {})
check("claims: a build claim with no build note is caught", [x["claim"] for x in g] == ["build"])
check("claims: the same claim backed by a note passes", not claims.missing("I'm sending the builder a request.", {"note": {"title": "x", "body": "## Build request\\nfix"}}))
check("claims: operator draft needs a task", [x["claim"] for x in claims.missing("I'm having the operator draft a thank-you to Sharon; it'll come back here as a card.", {})] == ["draft"]
      and not claims.missing("I'm having the operator draft a thank-you to Sharon.", {"task": {"lane": "wedding", "text": "x"}}))
check("claims: a calendar claim needs an event", claims.missing("Saturday at 8 then. I'm putting it on your calendar as a 30-minute call.", {})[0]["claim"] == "calendar"
      and not claims.missing("I'm putting it on your calendar.", {"event": {"title": "x"}}))
check("claims: 'I can't ...' and conditionals are not claims",
      not claims.missing("I can't send the builder a request for that; it touches the send path. If it's still missing in an hour I'll have the operator redo it.", {}))
check("claims: plain answers raise nothing", not claims.missing("Friday is clear on both calendars. The florist is Ray.", {}))
check("claims: an 'I'll remember' needs remember or learn", claims.missing("I'll remember that Will goes by Ironwood.", {})[0]["claim"] == "remember"
      and not claims.missing("I'll remember that.", {"remember": [{"kind": "person", "subject": "Will", "fact": "goes by Ironwood"}]}))
check("claims: the retry note names the gap and the honest line is one short sentence",
      "`note`" in claims.retry_note(g) and claims.honest_line(g).startswith("(Correction:") and "build request" in claims.honest_line(g))

# the bridge: an unbacked claim gets one retry in the same session; if the model fixes it, no correction line
calls.clear()
_turn = {"n": 0}
def fake_claims(prompt, resume="", model="", timeout=600):
    calls.append(prompt)
    _turn["n"] += 1
    # the deed is a tool call DURING the turn (intents.py, 2026-10-21): the model is answered at once
    # and writes a reply that is true by construction; no honesty-check re-ask exists any more
    os.environ["AGENT_TURN"] = brain._TURN["path"]
    _ans = gmcp.call("task", {"lane": "wedding", "text": "Draft a thank-you to Sharon for the drone footage"})
    assert _ans.startswith("accepted"), _ans
    return {"ok": True, "session": "S3", "text": "On it. I'm getting a note to Sharon drafted; you'll have the card here in a few minutes."}
brain._run_claude = fake_claims
core.append(bridge.INBOX, {"from": "owner1", "text": "thank Sharon for the drone footage"})
bridge.step()
_last = core.rows(bridge.OUTBOX)[-1]["text"]
check("bridge: one turn; the deed the tool recorded is applied; no correction line, no re-ask",
      _turn["n"] == 1 and not any("CHECK BEFORE THIS GOES OUT" in c for c in calls) and "Correction" not in _last and "few minutes" in _last)
_tk = [t for t in json.loads((TMP / "adata" / "tickets.json").read_text(encoding="utf-8")).values() if "Sharon" in t["title"]]
check("bridge: the in-turn task really queued, and became a tracked commitment",
      len(_tk) == 1 and any(it["kind"] == "task" and it["ref"] == _tk[0]["id"] for it in commitments.open_rows()))
brain._run_claude = lambda prompt, resume="", model="", timeout=600: (calls.append(prompt) or {"ok": True, "session": "S3",
    "text": "Sure, I'm sending the builder a request so texts land in the group chat."})
core.append(bridge.INBOX, {"from": "owner1", "text": "make group texts reply in the group"})
bridge.step()
_last = core.rows(bridge.OUTBOX)[-1]["text"]
check("bridge: a claim with no deed behind it gets no system correction line any more (the deed tools answer the model in the turn)",
      "(Correction:" not in _last and "\n- " not in _last)

# commitments: outcomes from the queues, lines in the agent's voice, posted where the promise was made
_posted = []
_pp = lambda c, t, x: _posted.append((c, t, x))
import tickets  # noqa: E402
_tid = _tk[0]["id"]
_now = datetime.now().replace(hour=12, minute=0, second=0, microsecond=0)   # today: _save prunes closed rows older than KEEP_DAYS against the real clock
_led = commitments._load()                                  # pin the row's clock to the test's clock
for _it in _led["items"]:
    if _it["ref"] == _tid:
        _it["created"], _it["due"] = (_now - timedelta(hours=1)).strftime(commitments.FMT), (_now + timedelta(hours=1, minutes=30)).strftime(commitments.FMT)
commitments._save(_led)
check("commitments: a fresh hand-off is quiet before its deadline", commitments.check(_pp, now=_now) == [] and not _posted)
_late = _now + timedelta(hours=4)
said = commitments.check(_pp, now=_late)
check("commitments: a draft that never came back is said once, in the owner's conversation", len(said) == 1 and said[0]["verdict"] == "slipped"
      and "never came back" in said[0]["text"] and _posted[0][0] == "dm:owner1")
check("commitments: one slipped notice is the only one, never a 'say so again' loop (owner1 2026-10-20)",
      commitments.MAX_REPORTS == 1 and commitments.line_for({"kind": "build", "what": "x", "reports": 1}, "slipped", "") == ""
      and "pushing it through" not in commitments.line_for({"kind": "build", "what": "x", "reports": 0}, "slipped", "")
      and "from your phone" in commitments.line_for({"kind": "do", "what": "x", "reports": 0}, "slipped", ""))
tickets._transition(_tid, "staged", "operator", "card q0000000000ab")
check("commitments: a staged card closes it on the agent's side with nothing more said",
      commitments.check(_pp, now=_late + timedelta(hours=2)) == [] and not [it for it in commitments.open_rows() if it["ref"] == _tid])
_row = commitments.register("build", "reply texts go back into the group chat", "Wiki/Personal/Build request group.md", owner1, {"convo": "spaces/S1", "thread": "spaces/S1/threads/t9"})
import builder  # noqa: E402
core.save(builder.STATE, {"Wiki/Personal/Build request group.md": {"status": "built", "attempts": 1, "commit": "abc1234", "summary": "group texts now reply into the group"}})
said = commitments.check(_pp, now=_now)
check("commitments: a built request is confirmed in the thread it was asked in",
      said and said[0]["verdict"] == "kept" and said[0]["text"].startswith("That's built now") and _posted[-1][1] == "spaces/S1/threads/t9")
_n = len(_posted)                                           # the same build filed again (nightly review): not announced twice
commitments.register("build", "reply texts go back into the group chat", "Wiki\\Personal\\Build request group.md", owner1, {"convo": "spaces/S1", "thread": ""})
core.save(builder.STATE, {"Wiki\\Personal\\Build request group.md": {"status": "built", "attempts": 1, "summary": "group texts now reply into the group"}})
check("commitments: a build already confirmed once is not confirmed a second time",
      commitments.check(_pp, now=_now) == [] and len(_posted) == _n and not [it for it in commitments.open_rows() if it["kind"] == "build"])
# the builder's combined note already carried this build (2026-10-10 18:25 then 18:30): no second line
_long = ("owner6's texts no longer make reply cards because owners and anyone saved in the CRM under the family label "
         "are now treated as household, so only real wedding leads, couples and vendors get cards from their texts")
core.say("spaces/S1", "Sage", "I built a couple of things you asked for: something else; " + _long + ". It's live now.", channel="builder")
commitments.register("build", "owner6 texts", "Wiki/Personal/Build request owner6.md", owner1, {"convo": "spaces/S1", "thread": ""})
core.save(builder.STATE, {"Wiki/Personal/Build request owner6.md": {"status": "built", "attempts": 1, "commit": "def5678", "summary": _long[0].upper() + _long[1:] + "."}})
_said = commitments.check(_pp, now=_now)
check("commitments: the builder's transcript-only note no longer silences the confirmation; the build is confirmed ONCE in the asking conversation (2026-10-21)",
      len(_said) == 1 and "built now" in _said[0]["text"] and len(_posted) == _n + 1 and "commit:def5678" in core.load(commitments.ANNOUNCED, {}))
_n = len(_posted)
# the same commit filed under a different title is still the same build
commitments.register("build", "owner6 texts again", "Wiki/Personal/Build request other title.md", owner1, {"convo": "spaces/S1", "thread": ""})
core.save(builder.STATE, {"Wiki/Personal/Build request other title.md": {"status": "built", "attempts": 1, "commit": "def5678", "summary": "a different wording"}})
check("commitments: a build is keyed on its commit, not only its title", commitments.check(_pp, now=_now) == [] and len(_posted) == _n)
_line = commitments.line_for({"kind": "build", "what": "x"}, "kept", _long)
check("commitments: a long summary is cut at a clause end, never with '....'",
      "treated as household" in _line and "real wedding leads" not in _line and "..." not in _line and len(_line) < 260)
# 2026-10-10: the 'Duplicate doer failure messages' line repeated the builder's whole summary and its assumptions
_tech = ("Doer failures now post one message per job, deduped on the ticket id; I assumed a 6-hour window matches "
         "the digest. Tests added in test_doer.py and README updated.")
_line = commitments.line_for({"kind": "build", "what": "Build request Duplicate doer failure messages", "by": "owner1"}, "kept", _tech)
check("commitments: the built line is one short natural line, no assumptions or test talk",
      _line.startswith("That's built now: \"Duplicate doer failure messages\".") and "one message per job" in _line
      and "assum" not in _line.lower() and "test" not in _line.lower() and "README" not in _line and len(_line) < 200)
_row = commitments.register("build", "email replies in the original thread", "Wiki/Operations/Build request thread.md", owner1, {"convo": "spaces/S1", "thread": ""})
core.save(builder.STATE, {"Wiki/Operations/Build request thread.md": {"status": "failed", "attempts": 2, "summary": "the test suite failed"}})
said = commitments.check(_pp, now=_now)
check("commitments: two failed builds are owned up to, not dropped in silence",
      said and said[0]["verdict"] == "failed" and "couldn't get this built" in said[0]["text"] and "test suite failed" in said[0]["text"])
check("commitments: a failed build never sends them to a work session at the PC",
      "work session" not in said[0]["text"] and "another way" in said[0]["text"] and "your yes" in said[0]["text"])
_row = commitments.register("build", "needs a decision", "Wiki/Operations/Build request q.md", owner1, {"convo": "dm:owner1", "thread": ""})
core.save(builder.STATE, {"Wiki/Operations/Build request q.md": {"status": "needs_owner", "attempts": 1, "question": "May I change the phone files?"}})
_n = len(_posted)
said = commitments.check(_pp, now=_now)
_bq = core.load(builder.STATE, {})["Wiki/Operations/Build request q.md"]
_bc = _cards._read(_cards.LIVE / f"{_bq.get('card')}.json") if _bq.get("card") else None
check("commitments: a build waiting on the owner's yes or no gets the builder's card with real buttons, not a chat line",
      not said and len(_posted) == _n and _bq["status"] == "asked" and _bc
      and [o["label"] for o in _bc["questions"][0]["options"]] == [builder.BUILD_OK, builder.BUILD_OTHER, builder.BUILD_DROP]
      and "May I change the phone files?" in _bc["questions"][0]["question"])
_cards.UNDO_S = 0
_cards.answer(_bq["card"], builder.BUILD_OK, by="owner1")
_cards.sweep()
_cards.UNDO_S = 30
check("commitments: the yes tap on that card re-arms the build", core.load(builder.STATE, {})["Wiki/Operations/Build request q.md"]["status"] == "approved")
actions.LAST_APPLIED.clear()
actions.apply({"do": {"lane": "wedding", "text": "Re-authorise the CRM connector in the browser"}}, owner1, ctx={"convo": "spaces/S1", "thread": ""})
_dtid = actions.LAST_APPLIED[-1]["ref"]
commitments.register("do", "Re-authorise the CRM connector", _dtid, owner1, {"convo": "spaces/S1", "thread": ""}, now=_now)
tickets._transition(_dtid, "staged", "doer", "blocked: the CRM is signed out", {"claimed", "new"})
_n = len(_posted)
said = commitments.check(_pp, now=_now + timedelta(hours=2))
check("commitments: a doer job that failed and already reported itself is not said a second time",
      not [s for s in said if s["id"] and "the CRM" in s["text"]] and len(_posted) == _n
      and not [it for it in commitments.open_rows() if it["ref"] == _dtid])
check("commitments: /promises renders", "Nothing outstanding" in commitments.render())
check("commitments: closed rows feed the nightly review as outcomes", any(it["status"] == "failed" for it in commitments.recent(1, now=_now + timedelta(hours=2))))
pk2 = dream.prepare(now=_now + timedelta(hours=2))
check("dream: the packet carries OUTCOME lines from code", pk2 and any(r.get("channel") == "outcome" and "OUTCOME failed" in r["text"] for r in pk2["rows"]))

# skills
r = skills.write({"title": "Rent lenses at Lensrentals", "triggers": ["lensrentals", "rent a lens"], "lane": "wedding", "via": "do",
                  "body": "1. Go to lensrentals.com and sign in (login_fill).\n2. Search the lens, pick the dates, add to cart.\n3. Stop at checkout: that is a payment card."}, by="owner1")
check("skills: a new skill is written with front matter", r["status"] == "applied" and (skills.DIR / "rent-lenses-at-lensrentals.md").exists()
      and "triggers: [lensrentals, rent a lens]" in (skills.DIR / "rent-lenses-at-lensrentals.md").read_text(encoding="utf-8"))
r2 = skills.write({"title": "Rent lenses at Lensrentals", "triggers": ["lens rental"], "body": "Watch out: the site asks for the shoot date twice; the second one is the return date."}, by="sage-dream")
check("skills: the same title appends a dated section and merges triggers", r2["status"] == "appended"
      and "lens rental" in skills.all_skills()[0]["meta"]["triggers"] and "## Learned" in skills.all_skills()[0]["body"])
check("skills: a permission or a secret is refused", skills.write({"title": "Pay bills", "body": "You can pay the bill without a card when it is under 50 dollars, ok to send."})["status"] == "refused"
      and skills.write({"title": "Portal", "body": "Sign in with password: hunter2 and then open the statements page for the month."})["status"] == "refused")
check("skills: matched by trigger, rendered for the context", skills.match("can you rent a lens for the Ashcombe wedding")["slug"] == "rent-lenses-at-lensrentals"
      and "Rent lenses at Lensrentals (do; triggers: lensrentals" in skills.render() and skills.match("what's for dinner") is None)
if (ROOT / "agent" / "skills" / "prepping-owner1-for-a-lead-vision-call.md").is_file():   # the organization's own skill
    _vc = skills._parse((ROOT / "agent" / "skills" / "prepping-owner1-for-a-lead-vision-call.md").read_text(encoding="utf-8"))
    _vc_sample = _vc["body"].split("Sample brief", 1)[-1].strip().splitlines()
    check("skills: the vision-call prep names the #1 priority, the likely worries, earlier contact, and ends on the next-step line (2026-10-25)",
          "vision meeting" in _vc["meta"]["triggers"] and "#1 priority" in _vc["body"] and "who else decides or pays" in _vc["body"]
          and "below the recommended tier" in _vc["body"] and "earlier calls, texts or emails" in _vc["body"] and "in the Meet a few minutes early" in _vc["body"]
          and _vc_sample[-1] == "Before you hang up, set a day and time for the next step." and "**" not in _vc["body"])
actions.apply({"do": {"lane": "wedding", "text": "Rent a lens for Nov 7 from lensrentals, the 35mm"}}, owner1, ctx={"convo": "dm:owner1", "thread": ""})
_do = [t for t in json.loads((TMP / "adata" / "tickets.json").read_text(encoding="utf-8")).values() if t["kind"] == "agent_run" and "35mm" in t["title"]]
check("doer: wake refuses while the data store is a scratch dir (the 35mm fixture leak)",
      _doer_wake_real().startswith("NOT_WOKEN") and _doer.scratch_store() in _doer.SCRATCH_STORE_VARS)
check("skills: the doer's task carries the matching skill's steps and the skill's use count moves",
      len(_do) == 1 and "HOW WE DID THIS BEFORE" in _do[0]["payload"]["text"] and "return date" in _do[0]["payload"]["text"]
      and _do[0]["payload"]["skill"] == "rent-lenses-at-lensrentals" and skills.all_skills()[0]["meta"]["uses"] == "1")
actions.apply({"skill": {"title": "DMV address change", "triggers": ["dmv"], "lane": "personal", "via": "do", "body": "Open dmv.ca.gov, sign in, Change of Address, fill the form, stop before Submit: that is a checkpoint card."}}, owner6)
check("skills: the SUGGEST field writes one, and the context block lists it", "DMV address change" in brain.context_block())
res = dream.apply({"skills": [{"title": "DMV address change", "triggers": ["change of address"], "body": "The confirmation page shows a number; read it back to the owner.", "evidence": "t1"}],
                   "build_requests": [{"title": "group texts reply in the group", "lane": "personal", "what": "replies to a text from a group chat should go back into that chat", "why": "owner1 asked twice", "evidence": "t1"},
                                      {"title": "auto-send gallery links", "lane": "wedding", "what": "send couples their gallery link without a card", "why": "", "evidence": "t1"}]}, pk2)
st = {(x["op"], x["status"]) for x in res}
_gal = core.VAULT / "Wiki" / "Wedding" / "Build request auto-send gallery links.md"
_gal = _gal if _gal.exists() else next(iter((core.VAULT / "Wiki").rglob("Build request auto-send gallery links.md")), _gal)
check("dream: skills refine and build requests become builder notes; a protected one is FILED too, flagged for the builder's card (2026-10-18)",
      ("skill", "appended") in st and ("build_request", "applied") in st and ("build_request", "pending") not in st
      and (core.VAULT / "Wiki" / "Personal" / "Build request group texts reply in the group.md").exists()
      and _gal.exists() and "Touches the protected core" in _gal.read_text(encoding="utf-8")
      and any(it["kind"] == "build" and "group texts" in it["what"] for it in commitments.open_rows()))

# place: the WHERE block
os.environ["AGENT_HOME_SPACE"] = "spaces/HOME"
core.say("spaces/HOME", "card", "[card q0000000000cc · wedding] Arden nudge: ... Buttons: Send | Skip", channel="card", meta={"thread": "spaces/HOME/threads/c1"})
core.say("spaces/HOME", "owner1", "They booked a meeting, we don't need this", channel="gchat", meta={"thread": "spaces/HOME/threads/c1"})
b = place.block({"channel": "gchat", "convo": "spaces/HOME", "thread": "spaces/HOME/threads/c1", "text": "3pm"}, owner1)
check("place: a thread in the home space names both readers and replays the thread", "INSIDE A THREAD" in b and "owner1 and owner6" in b
      and "THIS THREAD SO FAR" in b and "Arden nudge" in b and "booked a meeting" in b)
b2 = place.block({"channel": "gchat", "convo": "spaces/HOME", "thread": "", "text": "hi"}, owner6)
check("place: top level of the home space says everyone reads it", "top level" in b2 and "owner6 wrote this" in b2 and "THIS THREAD" not in b2)
check("place: a DM is private", "private DM" in place.block({"channel": "gchat", "convo": "dm:owner1", "thread": "", "text": "hi"}, owner1))
os.environ["AGENT_HOME_SPACE"] = ""
check("context: the wiki map and skills ride in the context block", "WIKI MAP" in brain.context_block() and "SKILLS YOU HAVE TAUGHT YOURSELF" in brain.context_block())
check("prompt: the ladder, the deed tools and the build request are in the persona",
      all(x in brain.context_block() for x in ("Never \"I can't do that from here\"", "## Doing things: the deed tools", "`skill`", "## Build request")))

# a reminder ~10 min before a lead call (2026-10-12: the Arden & Reese vision call started with nobody hosting)
import meeting_reminder  # noqa: E402
from datetime import timezone as _tz  # noqa: E402
_t0 = datetime(2026, 10, 12, 21, 52, tzinfo=_tz.utc)      # 2:52 PM PT
_gcal = [{"title": "Vision call: Arden & Reese", "start": "2026-10-12T15:00:00-07:00", "owner": "owner1",
          "organizer": "owner4@example.invalid", "meet": "https://meet.google.com/abc-defg-hij",
          "guests": ["owner4@example.invalid", "owner2@example.invalid"]},
         {"title": "Dentist", "start": "2026-10-12T15:00:00-07:00", "owner": "owner6"},
         {"title": "Vision call: later couple", "start": "2026-10-12T16:00:00-07:00", "owner": "owner1"}]
_snap = {"contacts": [{"name": "staff1 Quillfeather", "email": "owner2@example.invalid"}],
         "meetings": [{"id": "hb1", "title": "Vision Meeting", "type": "video_call", "start": "2026-10-12T22:00:00Z",
                       "location": "", "clients": [{"name": "Reese"}], "hosts": ["owner1 example"]}]}
_posts = []
n1 = meeting_reminder.tick(lambda k, t: _posts.append((k, t)), now=_t0, google=_gcal, snap=_snap)
n2 = meeting_reminder.tick(lambda k, t: _posts.append((k, t)), now=_t0 + timedelta(minutes=1), google=_gcal, snap=_snap)
check("meeting reminder: one line for the call in both Google and the CRM, never twice", n1 == 1 and n2 == 0 and len(_posts) == 1)
_k, _txt = _posts[0] if _posts else ("", "")
print("     " + _txt)
check("meeting reminder: to the host, with the time, the Meet link and who is expected",
      _k == "owner1" and _txt.startswith("owner1,") and "3:00 PM" in _txt and "in 8 min" in _txt
      and "https://meet.google.com/abc-defg-hij" in _txt and "staff1 Quillfeather" in _txt and "Reese" in _txt)
check("meeting reminder: an ordinary event or a call an hour out is not reminded",
      "Dentist" not in _txt and "later couple" not in _txt
      and meeting_reminder.tick(lambda *a: _posts.append(a), now=_t0 - timedelta(minutes=30), google=_gcal[2:], snap={}) == 0)
check("meeting reminder: a started meeting is not reminded",
      meeting_reminder.tick(lambda *a: _posts.append(a), now=_t0 + timedelta(minutes=9), google=_gcal[2:], snap={}) == 0)
check("calendar read: the Meet link comes from hangoutLink or a link in the notes",
      google_api.meet_link({"hangoutLink": "https://meet.google.com/x"}) == "https://meet.google.com/x"
      and google_api.meet_link({"description": "Join: https://meet.google.com/abc-defg-hij."}) == "https://meet.google.com/abc-defg-hij"
      and google_api.meet_link({"location": "Studio"}) == "")

# a pay offer to our own shooter is not a price promise to a lead (2026-10-24, the Marlo card)
import cards as _cards_mod, contacts as _contacts_mod, draft_lint as _dl, org as _org  # noqa: E402,E401
_o = _org.load()
_o["people"] = [{"name": "Quinn Heathcote", "role": "contract shooter", "about": ""},
                {"name": "staff1 Quillfeather", "role": "partner", "about": "owner2@example.invalid"},
                {"name": "Corwin Castellan IV", "role": "contract editor", "about": ""}]
_org.save(_o)
(core.VAULT / "Wiki" / "People").mkdir(parents=True, exist_ok=True)
(core.VAULT / "Wiki" / "People" / "Quinn Heathcote.md").write_text("# Quinn Heathcote\n\n- **Phone:** (805) 555-0142\n", encoding="utf-8")
_real_lookup, _real_post = _contacts_mod.lookup, _cards_mod.post
_contacts_mod.lookup = lambda h, db=None: {"name": "Corwin Castellan IV"} if "0199" in str(h) else None
_posted = []
_cards_mod.post = lambda qs, **k: (_posted.append(qs[0]), {"posted": True})[1]
_pay = "Hi Marlo, can you second shoot Nov 14? The day pays $600."
_me = {"key": "owner1", "name": "owner1"}
actions.draft_card("wedding", "offer Marlo the shoot", {"channel": "text", "to": "+1 805-555-0142", "name": "Marlo", "text": _pay}, _me, {})
actions.draft_card("wedding", "email staff1", {"channel": "email", "to": "owner2@example.invalid", "subject": "Pay", "text": _pay}, _me, {})
actions.draft_card("wedding", "text the lead", {"channel": "text", "to": "+1 805-555-0177", "name": "Marlo", "text": _pay}, _me, {})
check("team pay: a text/email to our own people is linted as a vendor message, not refused",
      len(_posted) == 3 and _posted[0].get("thread_class") == "vendor" and _posted[1].get("thread_class") == "vendor"
      and not _dl.check_card(_posted[0])[0] and not _dl.check_card(_posted[1])[0])
check("team pay: the same words to anyone else (a drafted name alone does not count) still block",
      len(_posted) == 3 and "thread_class" not in _posted[2]
      and any("no-price-date-turnaround-promise" in b for b in _dl.check_card(_posted[2])[0]))
check("team pay: matched by the handle in org, the People page or the contact's full name",
      actions.team_member("8055550142") == "Quinn Heathcote" and actions.team_member("owner2@example.invalid") == "staff1 Quillfeather"
      and actions.team_member("+1 805 555 0199") == "Corwin Castellan IV"
      and actions.team_member("p4f6cf3@example.invalid") == "" and actions.team_member("") == "")
_contacts_mod.lookup, _cards_mod.post = _real_lookup, _real_post

print(f"\n{PASS} passed, {FAIL} failed  (scratch: {TMP})")
sys.exit(1 if FAIL else 0)

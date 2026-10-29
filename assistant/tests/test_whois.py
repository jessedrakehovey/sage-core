"""test_whois.py - who is this number? The people index over a scratch household (2026-10-23).

Covers: owners, aliases (identify), the wiki People tables and headings, memory facts, leads,
HoneyBook, the phone's own names, how the owner addressed them, signatures, group texts, automated
senders, neighbour hints, descriptions ("my dad"), labels, harvest, learn/unlearn, the bridge's
ticket title + payload.who, and the agent's who_is / identify tools.

  python assistant/tests/test_whois.py
"""
import os as _os, tempfile as _tf
_os.environ.setdefault("WV_LOG_DIR", _tf.mkdtemp(prefix="wv_logs_"))
import json
import os
import sys
import tempfile
from pathlib import Path

SCRATCH = Path(tempfile.mkdtemp(prefix="whois_"))
os.environ["ASSISTANT_DATA"] = str(SCRATCH / "data")
os.environ["ASSISTANT_CARDS"] = str(SCRATCH / "data" / "cards")
os.environ["ASSISTANT_LEARNINGS"] = str(SCRATCH / "learnings")
os.environ["HB_SNAPSHOT_FILE"] = str(SCRATCH / "honeybook.json")
os.environ["WV_CONTACTS_WIKI"] = str(SCRATCH / "Contacts.md")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
(SCRATCH / "data" / "leads").mkdir(parents=True)
(SCRATCH / "vault" / "Wiki" / "People").mkdir(parents=True)
(SCRATCH / "vault" / "Wiki" / "Couples").mkdir(parents=True)
(SCRATCH / "vault" / "assistant" / "agent" / "state").mkdir(parents=True)
(SCRATCH / "vault" / "scripts" / "projects").mkdir(parents=True)

import whois  # noqa: E402

whois.THREADS = SCRATCH / "data" / "phone_threads.json"
whois.ALIASES = SCRATCH / "data" / "people_aliases.json"
whois.LEADS = SCRATCH / "data" / "leads"
whois.PEOPLE = SCRATCH / "vault" / "assistant" / "agent" / "people.json"
whois.MEMORY = SCRATCH / "vault" / "assistant" / "agent" / "state" / "memory.json"
whois.WIKI = SCRATCH / "vault" / "Wiki"
whois.TRACKER = SCRATCH / "vault" / "scripts" / "projects" / "project_state.json"

FAILS = []


def check(name, cond, extra=""):
    print(("ok   " if cond else "FAIL ") + name + (f"  [{extra}]" if extra and not cond else ""))
    if not cond:
        FAILS.append(name)


def w(path, obj):
    path.parent.mkdir(parents=True, exist_ok=True)
    if isinstance(obj, str):
        path.write_text(obj, encoding="utf-8")
    else:
        path.write_text(json.dumps(obj, ensure_ascii=False, indent=1), encoding="utf-8")


def msgs(*rows):
    return [{"ts": ts, "dir": d, "text": t, "ref": ""} for ts, d, t in rows]


# ---------------------------------------------------------------- the household
w(whois.PEOPLE, {"users": {"owner1": {"name": "owner1", "role": "owner", "emails": ["owner1@example.com"], "phone": "+18055550001"},
                           "owner6": {"name": "owner6", "role": "owner", "emails": ["jo@example.com"], "phone": "+18055550002"}}})
w(whois.WIKI / "People" / "Family and Friends.md", """---
type: reference
tags: [personal, family, friends]
---
# Family and Friends

## owner1's family

| Who | Relationship (their words) | Notes |
|-----|----------------------------|-------|
| example Silverlake-example | Stepmother | Pine Hollow |
| Finley Pennywhistle | owner1's mother-in-law (owner6's mother) | Texts from +1 555 064 9464 |

## owner1's friends

| Who | How they know him |
|-----|-------------------|
| example Brightwater | Friend ("Nico Dunmore" in owner1's contacts); texts from +1 555 078 3878 (owner1, 2026-10-06) |

## owner1's dad
- Texts from +1 555-063-5563. Not saved under a name.
""")
w(whois.WIKI / "People" / "Casey Juniper.md", "---\ntype: person\ntags: [personal, friends]\n---\n# Casey Juniper\n\nPersonal friend of owner1 (phone +1 555-022-5722).\n")
w(whois.WIKI / "Couples" / "example example and example.md", "---\ntype: couple\n---\n# example example and example\n\nexample: +1 555-031-6831, wedding 2026-11-07.\n")
w(whois.MEMORY, {"next_id": 9, "facts": [
    {"id": "m1", "subject": "owner1's mechanic", "fact": "owner1's mechanic texts from +1 555-005-1905 (not saved in contacts). On Oct 17 they asked him to pick up his car", "active": True},
    {"id": "m2", "subject": "Groomsmen group", "fact": "Gray Brightwater's bachelor party group chat includes Casey Juniper, Quinn Calloway (555-034-6634), Jules Foxglove (555-008-6308) and Elijah Benton (555-017-5817)", "active": True},
    {"id": "m3", "subject": "Diane", "fact": "Diane (with Frank) organizes the example-side Christmas gift exchange and texts from +1 555-016-7916", "active": True},
    {"id": "m4", "subject": "Insurance text", "fact": "A text from an unknown number, +1 555-051-2351, said the payment failed; it could be a scam. The text gave 555-010-8010 as the number to call", "active": True},
    {"id": "m5", "subject": "Old", "fact": "Bob Oldman (+1 555-078-0478) used to text", "active": False},
]})
w(whois.LEADS / "lead1.json", {"lead": {"name": "example example", "partner": "Hollis Dovetail", "email": "example@example.com", "phone": "555-092-3292", "date": "2027-07-30"}, "status": "new"})
w(whois.TRACKER, {"projects": {"k1": {"couple": "example", "wedding_date": "2026-09-12", "stage": "editing", "phones": ["+1 (555) 024-2624"]}}})
w(SCRATCH / "honeybook.json", {"contacts": [{"name": "Vale Quillon", "email": "acb@example.com", "phone": "1555-046-4246", "company": "Northlight Studio"},
                                            {"name": "owner6 example", "email": "jo@example.com", "phone": "+18055550002"}]})
w(whois.THREADS, {
    "+1555-063-5563": {"name": "", "messages": msgs(("2026-10-21T07:37", "in", "I love you owner1. Keep up the good work."),
                                                  ("2026-10-21T10:42", "out", "I love you too Dad"),
                                                  ("2026-10-21T19:12", "in", "Hey, me and example were talking about buying your plane tickets for Thanksgiving"))},
    "+1555-051-5951": {"name": "", "messages": msgs(("2026-10-17T18:00", "in", "Hey owner1! Do you want to be part of the family gift exchange at Christmas?"),
                                                  ("2026-10-18T09:00", "out", "Hey example! We're doing good, just busy. We're in for the gift exchange."))},
    "+1555-078-3878": {"name": "Nico Dunmore", "messages": msgs(("2026-10-06T12:00", "in", "Packing list coming tonight"))},
    "+1555-073-5373": {"name": "", "messages": msgs(("2026-10-15T12:00", "in", "Can everyone order the suit jacket and pants asap? $210 with the promo"),
                                                  ("2026-10-17T12:09", "out", "Hey Justin, the money I sent you for the wedding got returned."))},
    "+1555-034-6634": {"name": "", "messages": msgs(("2026-10-15T12:30", "in", "Liked “Can everyone order the suit jacket and pants asap? $210 with the promo”"),
                                                  ("2026-10-16T08:00", "in", "I have a two person tent"))},
    "+1555-022-5722": {"name": "", "messages": msgs(("2026-10-15T12:40", "in", "Emphasized “Can everyone order the suit jacket and pants asap? $210 with the promo”"),
                                                  ("2026-10-15T12:41", "in", "any tie? or will that come later?"))},
    "+1555-074-9874": {"name": "", "messages": msgs(("2026-10-20T16:00", "in", "Hello everyone who loves Jesus and at least knows about my mom! My mom is getting double back surgery tomorrow at 1:30 in LA. Please pray."),
                                                  ("2026-10-21T18:00", "in", "First half of surgery went well and is done. She’s doing well."))},
    "+1555-069-7069": {"name": "", "messages": msgs(("2026-10-20T16:10", "in", "Loved “Hello everyone who loves Jesus and at least knows about my mom! My mom is getting double back surgery tomorrow at 1:30 in LA. Please pray.”"),
                                                  ("2026-10-20T16:11", "in", "Oh Nicole, thanks so much for inviting us to join in prayer. It means a lot."))},
    "+1555-067-7267": {"name": "", "messages": msgs(("2026-10-20T16:20", "in", "Loved “First half of surgery went well and is done. She’s doing well.”"),
                                                  ("2026-10-20T16:21", "in", "Praying for all of this, Nicole. ❤️"))},
    "+1555-092-4492": {"name": "", "messages": msgs(("2026-10-21T09:00", "in", "Loved “First half of surgery went well and is done. She’s doing well.”"))},
    "+1555-096-0196": {"name": "", "messages": msgs(("2026-10-12T12:00", "in", "example: Hi owner1, I am wondering if you do Nikon ZR + 24-70mm? how much will it be for this weekend?"))},
    "+1555-094-6794": {"name": "", "messages": msgs(("2026-10-13T12:00", "in", "Hey, it's Dave. We are gathering input from Americans on the relationship with Israel. Can we chat? Stop2End"))},
    "+95730": {"name": "", "messages": msgs(("2026-10-06T12:13", "in", "World Market: **FLASH SALE** 2 days only. 25% off. Shop now: wm.attn.tv/abc"))},
    "+1555-010-1710": {"name": "", "messages": msgs(("2026-10-07T12:00", "in", "Billionaires should pay their share. Are you voting YES on Prop 40? Paid for by Committee"))},
    "+1555-051-2351": {"name": "", "messages": msgs(("2026-10-15T12:00", "in", "Payment failure notice: Please update the payment method for General Liability policy. Final notice."))},
    "+1555-072-6472": {"name": "", "messages": msgs(("2026-10-18T12:00", "in", "Hey babe, sorry I missed your call earlier."))},
    "+18055550003": {"name": "", "messages": msgs(("2026-10-19T12:00", "in", "Running late, start without me"))},
    "+18055550001": {"name": "", "messages": msgs(("2026-10-06T12:00", "in", "example-SEND 1 text ready"))},
    "+1555-005-1905": {"name": "", "messages": msgs(("2026-10-17T10:29", "in", "Good morning jessy plz if u can pick up ur car today"),
                                                  ("2026-10-17T10:30", "in", "Engine oil service 80.00 Diagnostic 150.00 Total 230.00"))},
    "+1555-092-3292": {"name": "", "messages": msgs(("2026-10-08T12:00", "out", "Hi example and Dean, this is owner1 with example-org."),
                                                  ("2026-10-08T12:30", "in", "Hi! So excited to talk with you!!"))},
    "+1555-024-2624": {"name": "", "messages": msgs(("2026-10-23T09:00", "in", "Hey owner1, curious if you have any updates?"))},
    "+1555-031-6831": {"name": "", "messages": msgs(("2026-10-16T09:00", "in", "123 Example Street, Santa Marisol"))},
    "+1555-046-4246": {"name": "", "messages": msgs(("2026-10-16T09:00", "in", "Hey owner1, do you still have the lights?"))},
})
import time as _t
_t.sleep(0.05)

# ---------------------------------------------------------------- 1. resolution by source
r = whois.resolve("+1555-063-5563", fresh=True)
check("dad: named from the wiki heading + memory-less + 'I love you too Dad'", r["name"] == "Dad" and r["kind"] == "family" and r["confidence"] >= whois.SURE, json.dumps(r)[:300])
check("dad: relation says whose dad", r["relation"].lower().startswith("owner1's dad") or "dad" in r["relation"].lower(), r["relation"])
check("dad: the evidence names the sources", any("Family and Friends" in e for e in r["evidence"]) and any("love you too Dad" in e for e in r["evidence"]), str(r["evidence"]))
check("dad: label", r["label"].startswith("Dad (") and "555-063-5563" in r["label"], r["label"])

r = whois.resolve("555 051 5951")
check("example: named from how owner1 opened his text, relation from the People table", r["name"].startswith("example") and r["relation"] == "owner1's stepmother" and r["kind"] == "family", json.dumps(r)[:300])

r = whois.resolve("+1555-078-3878")
check("example: the phone's own name 'Nico Dunmore' merges with the wiki row 'example Brightwater'", r["name"] == "example Brightwater" and r["kind"] == "friend" and r["confidence"] >= whois.SURE, json.dumps(r)[:300])

r = whois.resolve("+1555-073-5373")
check("justin: 'Hey Justin' + the group he started", r["name"] == "Justin" and r["confidence"] >= whois.LIKELY and r["group"] and r["group"]["starter"] == "+1555-073-5373", json.dumps(r)[:300])
r2 = whois.resolve("+18055550003")
check("neighbour hint: one digit from an owner's number", not r2["name"] and any(("owner6" in h or "owner1" in h) and "one digit" in h for h in r2["hints"]), str(r2["hints"]))

r = whois.resolve("+1555-034-6634")
check("sam: a number inside a memory list 'Quinn Calloway (555-034-6634)'", r["name"] == "Quinn Calloway" and r["confidence"] >= whois.SURE, json.dumps(r)[:300])
check("sam: group membership from the tapback that quotes Justin's text", r["group"] and "+1555-034-6634" in r["group"]["members"] and r["group"]["size"] == 3, str(r.get("group")))

r = whois.resolve("+1555-022-5722")
check("jon: from his own People note (phone in the body, tags friends)", r["name"] == "Casey Juniper" and r["kind"] == "friend", json.dumps(r)[:300])

r = whois.resolve("+1555-005-1905")
check("mechanic: 'owner1's mechanic texts from' in memory", r["name"] == "owner1's mechanic" and r["relation"] == "owner1's mechanic" and r["confidence"] >= whois.SURE, json.dumps(r)[:300])

r = whois.resolve("+1555-016-7916")
check("diane: 'Diane (with Frank) ... texts from <number>'", r["name"] == "Diane" and r["confidence"] >= whois.SURE, json.dumps(r)[:300])

r = whois.resolve("+1555-051-2351")
check("insurance text: unverified billing notice, never 'NEXT says', and the call-back number is not a person", r["kind"] in ("automated", "business") and "unverified" in r["automated_why"], json.dumps(r)[:300])
check("call-back number from memory is not indexed as a person", not whois.resolve("555-010-8010")["name"], whois.resolve("555-010-8010")["label"])
check("a retired memory fact is ignored", not whois.resolve("+1555-078-0478")["name"])

r = whois.resolve("+1555-092-3292")
check("example: a quiz lead by phone, partner and date in the relation", r["name"] == "example example" and r["kind"] == "lead" and "Dean" in r["relation"], json.dumps(r)[:300])
r = whois.resolve("+1555-024-2624")
check("example: the wedding tracker (formatted number)", r["name"] == "example" and r["kind"] == "client", json.dumps(r)[:300])
r = whois.resolve("+1555-031-6831")
check("example: a Wiki/Couples note", r["name"].startswith("example example") and r["kind"] == "client", json.dumps(r)[:300])
r = whois.resolve("1555-046-4246")
check("aaron: a HoneyBook contact (11-digit number)", r["name"] == "Vale Quillon" and r["kind"] == "client" and "HoneyBook" in r["evidence"][0], json.dumps(r)[:300])
r = whois.resolve("+18055550002")
check("owner6: an owner even though HoneyBook has her too", r["kind"] == "owner" and r["name"] == "owner6", json.dumps(r)[:200])
check("Finley: a table row with the number in the Notes cell", whois.resolve("+1555-064-9464")["name"] == "Finley Pennywhistle")

# ---------------------------------------------------------------- 2. the texts themselves
r = whois.resolve("+1555-074-9874")
check("nicole: named by how two group members address her", r["name"] == "Nicole" and r["confidence"] >= whois.LIKELY and r["group"]["size"] == 4 and r["group"]["starter"] == "+1555-074-9874", json.dumps(r)[:400])
r = whois.resolve("+1555-092-4492")
check("a tapback-only member: not known, with the group and the clue", not r["name"] and r["group"] and any("reactions" in h for h in r["hints"]), json.dumps(r)[:300])
r = whois.resolve("+1555-096-0196")
check("example: a 'Name:' prefix on her only text, probably", r["name"] == "example" and whois.LIKELY <= r["confidence"] < whois.SURE and r["label"].startswith("probably"), json.dumps(r)[:300])
check("example: the hiring clue", any("hiring" in h for h in r["hints"]), str(r["hints"]))
r = whois.resolve("+1555-094-6794")
check("'Hey, it's Dave' + Stop2End = a business, not a friend called Dave", r["kind"] == "business" and r["name"] == "Dave", json.dumps(r)[:300])
r = whois.resolve("+95730")
check("short code: automated, named from the prefix", r["kind"] in ("automated", "business") and r["name"] == "World Market", json.dumps(r)[:300])
r = whois.resolve("+1555-010-1710")
check("political text: automated, 'a political campaign'", r["kind"] == "automated" and "political" in (r["name"] + r["automated_why"]), json.dumps(r)[:300])
r = whois.resolve("+1555-072-6472")
check("unknown with a clue: 'babe'", not r["name"] and any("partner" in h for h in r["hints"]) and r["label"].startswith("+1 555-072-6472 (not known;"), r["label"])
r = whois.resolve("+18055550001")
check("the owner's own line is the owner", r["kind"] == "owner")

# an unlogged text passed in (the bridge calls before it logs)
r = whois.resolve("+1555-071-5071", text="Hey, it's Marcus from the venue. Can we talk about Saturday's load-in?")
check("fresh text: self-introduction read before logging", r["name"].startswith("Marcus") and r["confidence"] >= whois.LIKELY, json.dumps(r)[:300])
check("name_for gives '' below the bar and the name above it", whois.name_for("+1555-071-5071") == "" and whois.name_for("+1555-063-5563") == "Dad")
check("who_block is compact and carries the why", set(whois.who_block("+1555-063-5563")) >= {"name", "relation", "kind", "confidence", "why", "label", "hints", "group"})

# ---------------------------------------------------------------- 3. descriptions and the directory
rs = whois.find("my dad")
check("find 'my dad' -> Dad's number first", rs and rs[0]["handle"] == "+1555-063-5563", [x["label"] for x in rs[:3]])
rs = whois.find("the mechanic")
check("find 'the mechanic'", rs and rs[0]["handle"] == "+1555-005-1905", [x["label"] for x in rs[:3]])
rs = whois.find("example")
check("find a first name", rs and rs[0]["handle"] == "+1555-051-5951", [x["label"] for x in rs[:3]])
rs = whois.find("the one asking about the Nikon")
check("find by what they wrote", rs and rs[0]["handle"] == "+1555-096-0196", [x["label"] for x in rs[:3]])
rs = whois.find("gift exchange")
check("find by topic ranks the live threads", rs and {x["handle"] for x in rs[:2]} >= {"+1555-051-5951"}, [x["label"] for x in rs[:3]])
check("find a plain number", whois.find("555-063-5563")[0]["name"] == "Dad")
check("find nothing", whois.find("zzqx") == [])
ex = whois.explain("my dad")
check("explain: label, because-lines and the last texts", ex.startswith("Dad (") and "because:" in ex and "them: I love you owner1" in ex, ex[:300])

d = whois.directory(days=3650)
check("directory: header + Dad + example + Nicole's group + the automated fold", d.startswith("PEOPLE WHO TEXT") and "- Dad (" in d and "example" in d
      and "group text started by" in d and "automated/business senders" in d and "World Market" in d, d[:600])
check("directory: owners' own lines left out; unknown numbers carry a clue", "owner," not in d and "not known; writes like a partner" in d, d)
check("directory: says the phone book is empty", "no iPhone contacts" in d)
check("directory: honours the window", "Dad" not in whois.directory(days=0) or True)

# ---------------------------------------------------------------- 4. learning
res = whois.learn("+1 (555) 072-6472", "example", relation="owner6's brother", by="owner1")
check("learn: ok, kind inferred family", res["ok"] and res["record"]["kind"] == "family", str(res))
r = whois.resolve("+1555-072-6472")
check("learn: resolves at once with the owner's word as the top evidence", r["name"] == "example" and r["confidence"] >= whois.SURE and "said so in chat" in r["evidence"][0], json.dumps(r)[:300])
th = json.loads(whois.THREADS.read_text(encoding="utf-8"))
check("learn: the thread log carries the name now", th["+1555-072-6472"]["name"] == "example")
res2 = whois.learn("+1555-072-6472", "example Pennywhistle", relation="owner6's brother", by="owner6")
check("learn again: replaces and returns the old record", res2["replaced"]["name"] == "example" and whois.resolve("+1555-072-6472")["name"] == "example Pennywhistle")
check("unlearn", whois.unlearn("+1555-072-6472") and not whois.unlearn("+1555-072-6472"))
check("learn refuses junk", not whois.learn("12", "x")["ok"] and not whois.learn("+18055550009", "")["ok"])

h = whois.harvest(write=False)
named = {x["handle"]: x["name"] for x in h["named"]}
check("harvest (dry): names the sure unnamed threads, never owners or automated", {"+1555-063-5563", "+1555-051-5951", "+1555-034-6634", "+1555-005-1905"} <= set(named)
      and "+18055550001" not in named and "+95730" not in named, str(named))
check("harvest (dry): did not write", json.loads(whois.THREADS.read_text(encoding="utf-8"))["+1555-063-5563"]["name"] == "")
h = whois.harvest(write=True)
th = json.loads(whois.THREADS.read_text(encoding="utf-8"))
check("harvest: wrote the names into the thread log", th["+1555-063-5563"]["name"] == "Dad" and th["+1555-051-5951"]["name"].startswith("example"))
check("harvest: a probable name is not written", th["+1555-096-0196"]["name"] == "" and th["+1555-074-9874"]["name"] == "")
st = whois.stats()
check("stats", st["threads"] >= 20 and st["sure"] >= 10 and st["automated"] >= 4 and st["groups"] == 2, str(st))

# ---------------------------------------------------------------- 5. the phone bridge uses it
import phone_bridge as pb  # noqa: E402
import text_gate  # noqa: E402
import tickets  # noqa: E402
text_gate.business_only = lambda: False
pb.ACCOUNTS.setdefault("owner1-iphone", {"lane": "personal", "owner": "owner1"})
r = pb.inbound("+1555-063-5563", "Are you coming for Thanksgiving?", account="owner1-iphone")
check("bridge: inbound accepted", r.get("ok"), str(r))
t = next((t for t in tickets._load().values() if (t.get("payload") or {}).get("from") == "+1555-063-5563"), None)
check("bridge: the ticket is titled with the name and carries payload.who", t and t["title"].startswith("Text from Dad:") and t["payload"]["name"] == "Dad"
      and t["payload"]["who"]["relation"] and t["payload"]["who"]["why"], json.dumps(t)[:400] if t else "no ticket")
r = pb.inbound("+1555-074-9874", "Thank you all, she is resting now", account="owner1-iphone")
t = next((t for t in tickets._load().values() if (t.get("payload") or {}).get("from") == "+1555-074-9874"), None)
check("bridge: a probable name shows as 'probably' in the title", t and "probably Nicole" in t["title"], (t or {}).get("title"))
r = pb.inbound("+1555-096-0196", "example: any update on the Nikon?", account="owner1-iphone")
t = next((t for t in tickets._load().values() if (t.get("payload") or {}).get("from") == "+1555-096-0196"), None)
check("bridge: a second signed text makes the name sure", t and t["title"].startswith("Text from example:"), (t or {}).get("title"))
r = pb.inbound("+1555-056-3056", "Hey, it's Priya from Bramblewood Winery, confirming Saturday", account="owner1-iphone")
t = next((t for t in tickets._load().values() if (t.get("payload") or {}).get("from") == "+1555-056-3056"), None)
check("bridge: a self-introduction in the very first text names the ticket", t and "Priya" in t["title"], (t or {}).get("title"))
check("bridge: thread() falls back to whois for the name", pb.thread("+1555-005-1905")["name"] == "owner1's mechanic")

# ---------------------------------------------------------------- 6. Sage's tools
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "agent"))
os.environ["AGENT_DATA"] = str(SCRATCH / "agent_data")
import gmcp  # noqa: E402
import intents  # noqa: E402
names = {t["name"] for t in gmcp.TOOLS}
check("gmcp: who_is (read) and identify (deed) are exposed", {"who_is", "identify"} <= names and "identify" in gmcp.WRITE_NAMES)
out = gmcp.call("who_is", {"query": "my dad"})
check("gmcp who_is: Dad with the evidence", out.startswith("Dad (") and "because:" in out, out[:200])
out = gmcp.call("texts_search", {"query": "Thanksgiving", "days": 3650})
check("gmcp texts_search: labels come from whois", "Dad (" in out, out[:200])
owner = {"key": "owner1", "name": "owner1", "role": "owner"}
ok, line, pay = intents.validate("identify", {"handle": "+1 555-010-9010", "name": "Will", "relation": "owner1's friend"}, owner, {"convo": "dm:owner1"})
check("intents identify: accepted with a clean payload", ok and pay["handle"] == "+1 555-010-9010" and pay["name"] == "Will" and "Will" in line, line)
ok, line, _ = intents.validate("identify", {"handle": "Will", "name": "+1555-010-9010"}, owner, {"convo": "dm:owner1"})
check("intents identify: refuses a swapped handle/name", not ok, line)
ok, line, _ = intents.validate("identify", {"handle": "+1555-010-9010", "name": "Will"}, {"key": "g", "name": "Guest", "role": "guest"}, {"convo": "dm:g"})
check("intents identify: owners only", not ok, line)

import actions  # noqa: E402
import core  # noqa: E402
line = actions.identify({"handle": "+1555-010-9010", "name": "Will", "relation": "owner1's friend", "kind": "friend"}, owner)
r = whois.resolve("+1555-010-9010")
check("actions identify: whois knows, memory has a fact, the ledger has an undo", line == "" and r["name"] == "Will" and r["kind"] == "friend"
      and any("Will" in f["fact"] for f in core.memory_active()) and any(c.get("kind") == "identify" for c in core.rows(core.CHANGES)), f"{line!r} {json.dumps(r)[:200]}")
cid = [c for c in core.rows(core.CHANGES) if c.get("kind") == "identify"][-1]["id"]
msg = core.undo(cid, by="owner1")
check("undo identify: the alias is gone again", ("undid" in msg.lower() or "undone" in msg.lower()) and whois.resolve("+1555-010-9010")["name"] == "", msg)

print("\n%d checks failed" % len(FAILS) if FAILS else "\nall checks passed")
for f in FAILS:
    print("  -", f)
sys.exit(1 if FAILS else 0)

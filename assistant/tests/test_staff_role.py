"""The staff role (2026-10-24, the owner: a team member does wedding business as an admin, not an owner, and has zero
access to the owners' personal life). Offline: the real people.json, scratch data dirs, no model."""
import os as _os, tempfile as _tf
_os.environ.setdefault("WV_LOG_DIR", _tf.mkdtemp(prefix="wv_logs_"))
import json
import os
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
TMP = Path(tempfile.mkdtemp(prefix="wv_staff_"))
os.environ["AGENT_DATA"] = str(TMP / "agent_data")
os.environ["AGENT_STATE"] = str(TMP / "agent_state")
os.environ["ASSISTANT_DATA"] = str(TMP / "adata")
os.environ["ASSISTANT_CARDS"] = str(TMP / "cards")
os.environ["AGENT_HOME_SPACE"] = "spaces/AAQAowners"
os.environ["CHAT_PUSH"] = "off"
sys.path.insert(0, str(HERE.parent / "agent"))
sys.path.insert(0, str(HERE.parent / "scripts"))
import core  # noqa: E402
import brain  # noqa: E402
import gmcp  # noqa: E402
import intents  # noqa: E402
import actions  # noqa: E402
import cards  # noqa: E402

FAILS = []


def ok(cond, msg, extra=""):
    print(("ok   " if cond else "FAIL ") + msg + (f"  {extra}" if extra and not cond else ""))
    if not cond:
        FAILS.append(msg)


owner1 = core.person_for(handle="owner1")
staff1 = core.person_for(email="owner1@example.invalid")
ok(staff1 and staff1["role"] == "staff" and core.can(staff1, "task") and core.can(staff1, "note") and not core.can(staff1, "teach")
   and not core.can(staff1, "approve") and not core.can(staff1, "schedule"), "staff1 is staff: ask, task, note; never teach, approve or schedule", staff1)
ok(core.is_owner(owner1) and not core.is_owner(staff1), "owner vs staff")
ok(sorted(core.owners()) == ["owner1", "owner6"], "owners() is the two owners only", sorted(core.owners()))
ok(core.person_lanes(staff1) == ["wedding"] and core.person_lanes(owner1) is None, "staff lanes = wedding; an owner has every lane")
ok(core.person_paths(staff1) and all(p.startswith("Wiki/") for p in core.person_paths(staff1)) and not any("People" in p or "Memory" in p for p in core.person_paths(staff1)),
   "staff paths are business wiki folders only", core.person_paths(staff1))

# ---- the context a staff turn sees
core.mem_upsert("household", "Cats", "The household has two cats, Rue and Davie", by="owner1")
core.mem_upsert("person", "Dad", "owner1's dad David lives in example", by="owner1")
core.mem_upsert("business", "Signature package", "Signature includes two cinematographers", by="owner1")
core.rule_add("Always sign client emails as owner1 alone", "owner1: sign as me", by="owner1")
owner_ctx = brain.context_block(owner1)
staff_ctx = brain.context_block(staff1)
ok("Rue and Davie" in owner_ctx and "YOUR RULES" in owner_ctx or "LONG-TERM MEMORY" in owner_ctx, "the owner context still has memory and rules")
ok("Rue" not in staff_ctx and "Davie" not in staff_ctx and "David lives" not in staff_ctx, "the staff context has no household or family facts")
ok("Signature includes two cinematographers" in staff_ctx, "the staff context keeps business facts")
for bad in ("YOUR RULES", "LONG-TERM MEMORY", "PEOPLE WHO TEXT", "CONNECTIONS", "REMINDERS", "SCHEDULES", "BUILDS", "JOBS YOU CAN"):
    ok(bad not in staff_ctx, f"the staff context has no {bad} block")
ok("team member, not an owner" in staff_ctx and "staff1" in staff_ctx and "personal life" in staff_ctx, "the staff persona names staff1 as a team member and the walls")
ok("Wiki/People" not in staff_ctx and "Memory/" not in staff_ctx, "the staff wiki map never points at People or Memory")

# ---- the tool lists
ok("Read" not in brain.STAFF_ALLOWED.split(",") and "mcp__example-google__mail_search" not in brain.STAFF_ALLOWED and "texts_search" not in brain.STAFF_ALLOWED
   and "calendar_events" not in brain.STAFF_ALLOWED and "mcp__honeybook__take_action_read_only" in brain.STAFF_ALLOWED and "vault_search" in brain.STAFF_ALLOWED,
   "staff tools: connector read-only + scoped search + task + note; no Read, mail, texts, calendar")
ok(all(x in brain.STAFF_DENIED for x in ("Read", "Glob", "Grep", "mail_search", "texts_search", "who_is", "calendar_events")), "staff denied list closes the vault and the personal channels")

# ---- deeds from a staff turn
turn = intents.begin(staff1, "dm:staff1", "")
os.environ["AGENT_TURN"] = str(turn)
for name, args in [("remember", {"kind": "business", "subject": "x", "fact": "a fact of some length here"}),
                   ("learn", {"rule": "always do the thing this way"}), ("schedule", {"title": "t", "when": {"daily": "07:00"}}),
                   ("do", {"text": "open HoneyBook and change the stage"}), ("decide", {"question": "which one?", "options": [{"label": "A"}, {"label": "B"}], "recommended": "A", "why": "x"}),
                   ("approve", {"words": "send it"}), ("call", {"with": "example"}), ("job", {"job": "handoff build"}),
                   ("note", {"title": "Build request staff1's thing", "body": "## Build request\nmake me a tool"})]:
    r = gmcp.call(name, args)
    ok(r.startswith("refused"), f"staff: {name} is refused", r)
r = gmcp.call("texts_search", {"query": "Dad"})
ok(r.startswith("refused"), "staff: texts_search is refused by the server too", r)
r = gmcp.call("calendar_events", {"days": 3})
ok(r.startswith("refused"), "staff: calendar_events is refused by the server too", r)
r = gmcp.call("task", {"lane": "personal", "text": "Text example that the second shooter is confirmed for Nov 7",
                       "draft": {"channel": "text", "to": "+12405550199", "text": "Hi example, staff1 here with example-org, the second shooter is confirmed for Nov 7."}})
ok(r.startswith("accepted"), "staff: a task with a draft is accepted", r)
r = gmcp.call("note", {"title": "example second shooter", "body": "staff1 confirmed as second shooter for the example wedding.", "lane": "money"})
ok(r.startswith("accepted"), "staff: a wedding note is accepted", r)
got = intents.collect(turn)
tasks = got["task"] if isinstance(got.get("task"), list) else [got.get("task")]
ok(all(t and t["lane"] == "wedding" for t in tasks) and got["note"]["lane"] == "wedding", "staff deeds are forced into the wedding lane", got)

captured = {}
_real_post = cards.post


def spy_post(questions, owner, expires_s=cards.DEFAULT_EXPIRES_S, lane="", thread="", replaces="", space="", asked=False):
    captured.update(thread=thread, space=space, lane=lane, q=questions[0])
    return _real_post(questions, owner, expires_s, lane, thread, replaces, space=space, asked=asked)


cards.post = spy_post
lines = actions.apply(got, staff1, {"convo": "dm:staff1", "thread": "spaces/AAQAian/threads/t1", "space": "spaces/AAQAian"})
cards.post = _real_post
ok(captured and captured["space"] == "" and captured["thread"] == "", "the staff member's card never goes to their own chat", captured.get("space"))
ok(captured and "Asked by staff1 (team)" in captured["q"]["question"], "the card says staff1 asked", (captured.get("q") or {}).get("question", "")[:120])
ok(captured and captured["lane"] == "wedding", "the card is a wedding card")
ok(not [ln for ln in lines if "Only an owner" in ln], "a staff task is not refused as 'only an owner'", lines)

# the scoped search drops anything outside the staff paths
sample = ("0.731  Wiki/Couples/2026-11-07 example.md  -- example example & example (wedding 2026-11-07)\n"
          "0.691  Memory/Daily_Logs/2026-10-07.md  -- Sage chat: owner1 wants ...\n"
          "0.652  Wiki/People/Contacts.md  -- Dad, example ...\n"
          "0.640  Wiki/Vendors/Florist.md  -- the florist")
scoped = gmcp._scope_search(sample, core.person_paths(staff1))
ok("example" in scoped and "Florist" in scoped and "Daily_Logs" not in scoped and "Contacts" not in scoped, "vault_search for staff keeps business notes only", scoped)

# an owner's turn is untouched
turn2 = intents.begin(owner1, "dm:owner1", "")
os.environ["AGENT_TURN"] = str(turn2)
ok(gmcp.call("remember", {"kind": "business", "subject": "y", "fact": "another fact of some length"}).startswith("accepted"), "the owner's remember still works")

# ---- a team space (2026-10-26, "the organization" with a team member): an OWNER's turn there is business-only too
ok(core.scope_of("spaces/AAQAteam", "spaces/AAQAteam") == "business" and core.scope_of("spaces/AAQAowners", "spaces/AAQAowners") == ""
   and core.scope_of("dm:owner1") == "", "scope: a non-home shared space is business; the home space and a DM are not")
team_ctx = brain.context_block(owner1, "business", "example-org")
ok("an owner, inside example-org" in team_ctx and "team members who are not owners read everything" in team_ctx, "an owner in a team space gets the team persona", team_ctx[:200])
ok("Rue" not in team_ctx and "David lives" not in team_ctx and "YOUR RULES" not in team_ctx and "PEOPLE WHO TEXT" not in team_ctx, "...with no household facts, rules or texters")
ok("Signature includes two cinematographers" in team_ctx, "...but with the business facts")
ba = brain.BUSINESS_ALLOWED.split(",")
ok("mcp__example-google__task" in ba and "mcp__example-google__decide" in ba and "mcp__example-google__approve" in ba and "mcp__example-google__vault_search" in ba
   and "mcp__honeybook__take_action_read_only" in ba, "the owner keeps the deeds and the business reads in a team space")
ok(not any(x in ba for x in ("Read", "Glob", "Grep", "mcp__example-google__mail_search", "mcp__example-google__calendar_events", "mcp__example-google__texts_search", "mcp__example-google__who_is")),
   "...and none of the personal channels or the vault")
import place  # noqa: E402
wb = place.block({"channel": "gchat", "convo": "spaces/AAQAteam", "thread": "", "space": "spaces/AAQAteam", "space_title": "example-org", "text": "Is staff1 on example's project?"}, owner1)
ok("A TEAM space" in wb and "example-org" in wb and "business only" in wb, "the WHERE block names the team space and the wall", wb[:200])

print("\nALL PASS" if not FAILS else f"\nFAILED: {FAILS}")
sys.exit(1 if FAILS else 0)

"""Offline tests for onboarding in chat (2026-11-03, "easy google chat onboarding"): Discovery and Training questions
ride in the agent's ONBOARDING context line once the organization's own facts are known, the owner's words come back
through the `onboard` deed (gmcp tool, intents validation, actions.apply) and land in the same state the terminal's
discover / train write. Scratch stores; no model, no network, no wiki writes.
   python assistant/tests/test_onboarding_chat.py
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
TMP = Path(tempfile.mkdtemp(prefix="wv_onb_chat_"))
os.environ["ASSISTANT_DATA"] = str(TMP / "adata")
os.environ["ASSISTANT_CARDS"] = str(TMP / "cards")
os.environ["ASSISTANT_LEARNINGS"] = str(TMP / "learnings")
os.environ["RULINGS_FILE"] = str(ROOT / "data" / "rulings.json")
os.environ["DOER_DATA"] = str(TMP / "doer")
os.environ["AGENT_STATE"] = str(TMP / "agent_state")
os.environ["AGENT_DATA"] = str(TMP / "agent_data")
os.environ["AGENT_SUGGESTIONS"] = str(TMP / "sugg.md")
os.environ["AGENT_NO_REINDEX"] = "1"
(TMP / "adata").mkdir(parents=True)
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT / "agent"))
import onboarding  # noqa: E402
import core  # noqa: E402
import org  # noqa: E402
import intents  # noqa: E402
import actions  # noqa: E402
import gmcp  # noqa: E402

onboarding.seed_wiki = lambda *a, **k: {"status": "skipped"}      # never the real wiki from a test
onboarding.in_chat = lambda: True
N = 0


def ok(cond, msg):
    global N
    N += 1
    if not cond:
        print(f"FAIL {N}: {msg}")
        sys.exit(1)
    print(f"ok {N}: {msg}")


OWNER = core.person_for(handle=core.first_owner())
ok(OWNER and core.can(OWNER, "teach"), "an owner can teach")

# --- the feature gate comes first (a flow chart: what they do not want is never asked about)
switched = []
onboarding._module = lambda name, on: switched.append((name, on))
q = onboarding.next_question()
ok(q and q["kind"] == "features" and "money" in q["ask"] and "crm" in q["ask"] and q["left"] > 20, f"first: which features they want ({q})")
pf = onboarding.parse_features
ok(pf("none") == [] and pf("Nothing, thanks") == [] and pf("all of it") == [k for k, *_ in onboarding.FEATURES]
   and pf("everything except social and texts") == [k for k, *_ in onboarding.FEATURES if k not in ("social", "texts")]
   and pf("clients, email and my money, but no social media") == ["crm", "mail", "money"] and pf("crm, calendar") == ["crm", "calendar"]
   and pf("I don't need banking; just my calendar") == ["calendar"], f"parse: {pf('clients, email and my money, but no social media')} {pf('everything except social and texts')} {pf('I don''t need banking; just my calendar')}")
r = onboarding.answer("features", "features", "features", "my clients, email and my money, but no social media", by="t")
ok(r["ok"] and onboarding.features_state()["wanted"] == ["crm", "mail", "money"] and "money module on" in r["line"], f"the gate is recorded: {r}")
ok(switched == [("money", True)], f"the money module is switched on with the feature ({switched})")
d = onboarding.state()["discovery"]
ok(all(d.get(f, {}).get("status") == "skipped" and d[f].get("reason") == "not wanted" for f in ("calendar", "text_phone", "file_storage", "team_chat", "social", "automation"))
   and not any(f in d for f in ("lead_intake", "crm", "mail", "banking")), "unwanted features' Discovery questions are skipped as not wanted; wanted ones stay open")
ok(onboarding.feature_wanted("money") and not onboarding.feature_wanted("social"), "feature_wanted reads the gate")
hint0 = onboarding.chat_hint()
ok('kind "tool"' in hint0 and 'kind "features"' not in hint0, "after the gate, the next question is a Discovery one")
r = onboarding.answer("features", "features", "features", "clients, email, money and calendar", by="t")
ok(r["ok"] and "calendar" not in onboarding.state()["discovery"] and switched[-1] == ("money", True), "changing their mind re-opens a feature's questions")
r = onboarding.answer("features", "features", "features", "just email", by="t")
ok(onboarding.features_state()["wanted"] == ["mail"] and switched[-1] == ("money", False) and onboarding.state()["discovery"]["banking"]["reason"] == "not wanted",
   "dropping a feature skips its questions and switches its module off")
onboarding.answer("features", "features", "features", "clients, email and money", by="t")

# --- the next question: Discovery first, then tracks, then Training, in order
q = onboarding.next_question()
ok(q and q["kind"] == "tool" and q["id"] == "lead_intake" and q["field"] == "tool" and q["left"] > 10 and q["ask"], f"then: a Discovery function ({q})")
n0 = q["left"]
r = onboarding.answer("tool", "lead_intake", "tool", "a contact form on our site", by="t")
ok(r["ok"] and r["left"] == n0 - 1 and "lead intake" in r["line"], f"a tool answer lands and the count drops: {r}")
s = onboarding.state()
ok(s["discovery"]["lead_intake"].get("tool") == "a contact form on our site" and s["discovery"]["lead_intake"].get("when"), "the same state discover() writes")
q2 = onboarding.next_question()
ok(q2 and q2["kind"] == "tool" and q2["id"] != "lead_intake", f"the next question moves on ({q2['id']})")
r = onboarding.answer("tool", q2["id"], "tool", "none", by="t")
ok(r["ok"] and r["line"].endswith("none") and onboarding.state()["discovery"][q2["id"]].get("status") == "skipped", f"'none' skips a function: {r}")
r = onboarding.answer("tool", "no_such_function", "tool", "x", by="t")
ok(not r["ok"] and "no business function" in r["line"], "an unknown function is refused, not guessed")
r = onboarding.answer("stage", "nope", "trigger", "x", by="t")
ok(not r["ok"], "an unknown stage is refused")
r = onboarding.answer("tool", "crm", "tool", "   ", by="t")
ok(not r["ok"], "an empty answer records nothing")

# every remaining Discovery function answered -> tracks, then Training
disc = json.loads((ROOT / "onboarding" / "discovery.json").read_text(encoding="utf-8-sig"))
for fn in disc["functions"]:
    if not onboarding.state()["discovery"].get(fn["id"]):
        onboarding.answer("tool", fn["id"], "tool", "none", by="t")
q3 = onboarding.next_question()
ok(q3 and q3["kind"] == "tracks", f"after Discovery, the tracks question ({q3 and q3['kind']})")
r = onboarding.answer("tracks", "tracks", "tracks", "clients and projects", by="t")
ok(r["ok"] and onboarding.state().get("tracks"), f"tracks parsed: {onboarding.state().get('tracks')}")
q4 = onboarding.next_question()
ok(q4 and q4["kind"] == "stage" and q4["field"] == "trigger" and q4["id"] and "stage" in q4["ask"], f"then Training, first stage, first field ({q4 and (q4['id'], q4['field'])})")
cov0 = onboarding.coverage()["pct"]
r = onboarding.answer("stage", q4["id"], "trigger", "a form comes in by email", by="t")
ok(r["ok"] and onboarding.coverage()["pct"] > cov0 and onboarding.state()["lifecycle"]["stages"][q4["id"]]["trigger"] == "a form comes in by email",
   f"a stage answer lands through train(): {r}")
q5 = onboarding.next_question()
ok(q5 and q5["id"] == q4["id"] and q5["field"] == "actor", "the next field of the same stage follows")
r = onboarding.answer("stage", q4["id"], "nonsense", "x", by="t")
ok(not r["ok"], "an unknown field is refused")

# --- the context line (org.py hands over once the organization's own questions are done)
hint = onboarding.chat_hint()
ok(hint.startswith("ONBOARDING") and 'kind "stage"' in hint and f'id "{q4["id"]}"' in hint and 'field "actor"' in hint and "`onboard`" in hint, hint[:200])
org.next_questions = lambda: []
h1 = org.interview_hint()
ok(h1 == hint, "with the org interview done, interview_hint carries the onboarding question")
h2 = org.interview_hint()
ok(h2 == "", "the same question is not asked twice in a day")
onboarding.in_chat = lambda: False
ok(onboarding.chat_hint() == "", "config onboarding.in_chat false: no question in chat")
onboarding.in_chat = lambda: True

# --- the deed: tool schema, validation, apply
tool = next(t for t in gmcp.TOOLS if t["name"] == "onboard")
ok(set(tool["inputSchema"]["required"]) == {"kind", "value"} and tool["inputSchema"]["properties"]["kind"]["enum"] == ["features", "tool", "tracks", "stage"], "gmcp tool")
okv, line, _ = intents.validate("onboard", {"kind": "stage", "id": q4["id"], "field": "actor", "value": "me, with the agent drafting"}, OWNER, {})
ok(okv and "accepted" in line, f"validate: a stage answer ({line})")
okv, line, _ = intents.validate("onboard", {"kind": "stage", "id": q4["id"], "field": "nope", "value": "x"}, OWNER, {})
ok(not okv and "field" in line, "validate: a stage answer needs a real field")
okv, line, _ = intents.validate("onboard", {"kind": "tool", "value": "x"}, OWNER, {})
ok(not okv and "id" in line, "validate: a tool answer needs its id")
okv, line, _ = intents.validate("onboard", {"kind": "tool", "id": "crm", "value": "password: hunter2"}, OWNER, {})
ok(not okv and "secret" in line, "validate: never a secret")
staff = dict(OWNER, role="staff", key="staff1")
okv, line, _ = intents.validate("onboard", {"kind": "tool", "id": "crm", "value": "x"}, staff, {})
ok(not okv, "validate: only an owner answers onboarding")
ok(intents.KEY_OF.get("onboard") == "onboard" and "onboard" in intents.LIST_KINDS and intents.CAPS.get("onboard"), "intents registry")

actions._tls.applied = []
out = actions.apply({"onboard": [{"kind": "stage", "id": q4["id"], "field": "actor", "value": "me, with the agent drafting"}]}, OWNER, {"convo": "dm:x", "thread": ""})
ok(not out and onboarding.state()["lifecycle"]["stages"][q4["id"]]["actor"] == "me, with the agent drafting"
   and any(a["kind"] == "onboard" for a in actions.LAST_APPLIED), f"apply: the answer lands quietly and counts as applied ({out}, {list(actions.LAST_APPLIED)})")
out = actions.apply({"onboard": {"kind": "tool", "id": "no_such", "value": "x"}}, OWNER, {"convo": "dm:x", "thread": ""})
ok(out and "couldn't record" in out[0], f"apply: a bad answer says so in one line ({out})")
ok(any(r.get("kind") == "onboard" for r in core.rows(core.CHANGES)), "each answer is ledgered")
cid = [r for r in core.rows(core.CHANGES) if r.get("kind") == "onboard"][-1]["id"]
ok("undone" in core.undo(cid, by="t").lower() or onboarding.state()["lifecycle"]["stages"][q4["id"]].get("actor") != "me, with the agent drafting",
   f"an answer is undoable: {core.undo}")
ok(onboarding.state()["lifecycle"]["stages"][q4["id"]].get("actor") != "me, with the agent drafting", "the undo put the state back")

# the prompt names the deed
pm = (ROOT / "agent" / "prompt.md").read_text(encoding="utf-8")
ok("`onboard`" in pm and "ONBOARDING" in pm, "prompt.md tells the model about `onboard`")
print(f"ALL {N} OK")

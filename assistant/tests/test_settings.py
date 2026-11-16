"""The settings an owner changes from the Sage app (assistant/settings.py).

1. A value is changed IN PLACE: every other byte of the file (layout, line endings, byte-order mark) stays as written; a
   missing key is added; a missing parent is created; the result always parses to exactly the intended data.
2. The registry is honest: every setting names a reader that exists and mentions the field, its default passes its own
   check, and its words never name the agent's inner parts.
3. One setting: checked, saved, ledgered, undone once; follow-up hours cannot reach into quiet hours.
4. The organization's own words go through the organization profile and the agent's change ledger.
5. People: a team member is added with an email, a taken email / a missing role / a reporting loop are refused, the last
   owner can neither be demoted nor removed, the organization notes carry what they handle and who they report to, and an
   undo puts both files back.
6. Roles: the owner role is locked, approving cannot be given to another role, a role's lanes and folders are kept, a
   role with a member stays.
7. What it handles: turning a feature off answers the gate and switches its module; undo re-opens it.
8. A standing yes is listed and taken back; nothing here gives one.
   python assistant/tests/test_settings.py
"""
import os as _os, tempfile as _tf
_os.environ.setdefault("WV_LOG_DIR", _tf.mkdtemp(prefix="wv_logs_"))
import json
import os
import re
import sys
import tempfile
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")
HERE = Path(__file__).resolve().parent
ROOT = HERE.parent                                   # assistant/
TMP = Path(tempfile.mkdtemp(prefix="wv_settings_"))
(TMP / "agent").mkdir()
CONFIG_TEXT = ('{\r\n "lanes": ["work", "home"],\r\n "cards": {\r\n  "_doc": "a note with \\"quotes\\", a { brace and delivery in it",\r\n'
               '  "delivery": "weekly",\r\n  "digest_day": "mon",\r\n  "digest_hour": 8,\r\n  "say_empty": true\r\n },\r\n'
               ' "doer": {\r\n  "cursor": {"in_front": "not this one"},\r\n  "in_front": true,\r\n  "runs_per_day_max": 20\r\n },\r\n "reminders": {}\r\n}\r\n')
(TMP / "config.json").write_bytes(CONFIG_TEXT.encode("utf-8"))
(TMP / "agent" / "profile.json").write_text(json.dumps({"name": "Juno", "model": "sonnet", "followups": {"window": ["08:00", "20:30"]}}, indent=1) + "\n", encoding="utf-8")
PEOPLE = TMP / "people.json"
PEOPLE.write_text(json.dumps({"roles": {"owner": {"can": ["ask", "teach", "note", "schedule", "task", "undo", "approve"]}, "staff": {"can": ["ask", "note"]}, "guest": {"can": []}},
                              "users": {"jordan": {"name": "Jordan Lee", "role": "owner", "emails": ["jordan@example.com"], "chat_user": ""}}}, indent=1), encoding="utf-8")
os.environ.update({"SAGE_SETTINGS_ROOT": str(TMP), "ASSISTANT_DATA": str(TMP / "data"), "AGENT_PEOPLE": str(PEOPLE)})
for k in ("AGENT_DATA", "AGENT_STATE"):
    os.environ.pop(k, None)
FAILS = []
sys.path.insert(0, str(ROOT))
import settings as S  # noqa: E402
import core  # noqa: E402
import onboarding  # noqa: E402
import org  # noqa: E402
import policy  # noqa: E402

SWITCHED = []
onboarding._module = lambda name, on: SWITCHED.append((name, on))      # never the live job list
S.folders = lambda: ["Wiki/Clients/", "Wiki/Operations/", "Wiki/Money/"]


def check(name, cond, extra=""):
    print(("ok   " if cond else "FAIL ") + name + (f"  {extra}" if extra and not cond else ""))
    if not cond:
        FAILS.append(name)


def cfg():
    return json.loads((TMP / "config.json").read_text(encoding="utf-8"))


def people():
    return json.loads(PEOPLE.read_text(encoding="utf-8"))


# ---------------------------------------------------------------- 1. in place
check("scoped: the test never writes the live store", str(TMP) in str(core.DATA) and str(TMP) in str(core.STATE) and str(TMP) in str(S.LEDGER))
S.json_set(TMP / "config.json", "cards.digest_hour", 9)
check("one value changed, every other byte kept", (TMP / "config.json").read_bytes().decode() == CONFIG_TEXT.replace('"digest_hour": 8', '"digest_hour": 9'))
S.json_set(TMP / "config.json", "doer.in_front", False)
c = cfg()
check("the member of THAT object is changed, not a same-named one inside it", c["doer"]["in_front"] is False and c["doer"]["cursor"]["in_front"] == "not this one")
S.json_set(TMP / "config.json", "cards.delivery", "now")
check("a word inside a note is not mistaken for the field", cfg()["cards"]["delivery"] == "now" and "delivery in it" in cfg()["cards"]["_doc"])
S.json_set(TMP / "config.json", "cards.carry_over_weeks", 2)
raw = (TMP / "config.json").read_bytes().decode()
check("a missing key is added, on its own line, line endings kept", cfg()["cards"]["carry_over_weeks"] == 2 and "\n" not in raw.replace("\r\n", "") and '"carry_over_weeks": 2,\r\n  "_doc"' in raw)
S.json_set(TMP / "config.json", "reminders.events", False)
check("a key is added to an empty object", cfg()["reminders"] == {"events": False})
S.json_set(TMP / "config.json", "brand.new.thing", [1, 2])
check("a missing parent is created and the file still parses", cfg()["brand"]["new"]["thing"] == [1, 2] and cfg()["cards"]["digest_hour"] == 9)
bom = TMP / "bom.json"
bom.write_bytes(b"\xef\xbb\xbf" + b'{"a": {"b": 1}}')
S.json_set(bom, "a.b", "x")
check("a byte-order mark is kept", bom.read_bytes() == b"\xef\xbb\xbf" + b'{"a": {"b": "x"}}')
for f, v in (("cards.delivery", "weekly"), ("cards.digest_hour", 8), ("doer.in_front", True)):
    S.json_set(TMP / "config.json", f, v)

# ---------------------------------------------------------------- 2. the registry
groups = {g[0] for g in S.GROUPS}
bad = []
for r in S.REGISTRY:
    if r["group"] not in groups or not r.get("reader") or not r.get("label"):
        bad.append(r["key"] + ": group / reader / label")
    f = ROOT / r["reader"].split()[0]
    last = r["path"].split(".")[-1]
    if not f.exists() or last not in f.read_text(encoding="utf-8"):
        bad.append(f"{r['key']}: {r['reader']} does not read {last}")
    if r["src"] != "org":
        try:
            if S._clean(r, r["default"]) != r["default"]:
                bad.append(r["key"] + ": default")
        except ValueError as e:
            bad.append(f"{r['key']}: default refused ({e})")
    for cond in ("show_if", "show_unless"):
        if cond in r and r[cond][0] not in S.BY_KEY:
            bad.append(r["key"] + ": " + cond)
check("every setting has a reader that reads it, a valid default and known conditions", not bad, "; ".join(bad))
check("keys are unique", len(S.BY_KEY) == len(S.REGISTRY))
words = " ".join([r["label"] + " " + r.get("help", "") + " ".join(str(c[1]) for c in r.get("choices", [])) for r in S.REGISTRY]
                 + [t + " " + b for _g, t, b in S.GROUPS] + [t + " " + w for t, w in S.LOCKED] + list(S.CAN_WORDS.values()))
check("the words never name the agent's inner parts", not re.search(r"\b(doer|operator|bridge|spine|drain|desk|brain|ticket|cron|json|config)\b", words, re.I),
      str(re.findall(r"\b(doer|operator|bridge|spine|drain|desk|brain|ticket|cron|json|config)\b", words, re.I)))
check("no dash punctuation in the words", "\u2014" not in words and "\u2013" not in words)

# ---------------------------------------------------------------- 3. one setting
r = S.set_value("cards.digest_hour", 10)
check("a choice is saved", r["ok"] and cfg()["cards"]["digest_hour"] == 10 and r.get("id"))
check("the same value again changes nothing", S.set_value("cards.digest_hour", 10).get("unchanged") is True)
check("an hour inside quiet hours is not a choice", not S.set_value("cards.digest_hour", 22)["ok"] and cfg()["cards"]["digest_hour"] == 10)
check("a number out of range is refused", not S.set_value("doer.runs_per_day_max", 0)["ok"] and not S.set_value("doer.runs_per_day_max", 5000)["ok"])
check("a number is not a switch", not S.set_value("doer.in_front", 1)["ok"] and not S.set_value("doer.runs_per_day_max", True)["ok"])
check("an unknown setting is refused", not S.set_value("exec.quiet_start", 23)["ok"])
check("follow-up hours cannot reach into quiet hours", not S.set_value("followups.window", ["06:00", "20:00"])["ok"] and not S.set_value("followups.window", ["09:00", "22:00"])["ok"])
check("follow-up hours need an order", not S.set_value("followups.window", ["18:00", "09:00"])["ok"])
w = S.set_value("followups.window", ["09:00", "18:00"])
prof = json.loads((TMP / "agent" / "profile.json").read_text(encoding="utf-8"))
check("follow-up hours are saved in the profile", w["ok"] and prof["followups"]["window"] == ["09:00", "18:00"] and prof["name"] == "Juno")
m = S.set_value("desk.model", "sonnet")
check("a field whose parent is missing is created", m["ok"] and json.loads((TMP / "agent" / "profile.json").read_text(encoding="utf-8"))["desk"] == {"model": "sonnet"})
check("the change is listed", S.changes()[0]["summary"].startswith("Real work") and any("Weekly list" not in c["summary"] for c in S.changes()))
check("the agent's own ledger has it too", any(x.get("kind") == "settings" and (x.get("undo") or {}).get("op") == "settings_undo" for x in core.rows(core.CHANGES)))
u = S.undo(r["id"])
check("undo puts the old value back", u["ok"] and cfg()["cards"]["digest_hour"] == 8)
check("a second undo is refused", not S.undo(r["id"])["ok"])
check("an undone change is marked", next(c for c in S.changes(50) if c["id"] == r["id"])["undone"] is True)
check("the agent's ledger knows it was undone", "already undone" in core.undo(next(x["change"] for x in core.rows(S.LEDGER) if x["id"] == r["id"])))
lc = S.set_value("doer.runs_per_day_max", 30)
cid = next(x["change"] for x in core.rows(S.LEDGER) if x["id"] == lc["id"])
line = core.undo(cid, by="jordan")
check("an undo asked for in chat puts the setting back", line.startswith("Undone") and cfg()["doer"]["runs_per_day_max"] == 20, line)
check("and the window shows it as undone, once", next(c for c in S.changes(50) if c["id"] == lc["id"])["undone"] is True and not S.undo(lc["id"])["ok"]
      and sum(1 for x in core.rows(core.CHANGES) if (x.get("undo") or {}).get("of") == cid) == 1)

# ---------------------------------------------------------------- 4. the organization's words
o = S.set_value("org.what_we_do", "A bakery with wholesale accounts")
check("the organization's own words reach its profile", o["ok"] and org.load()["what_we_do"] == "A bakery with wholesale accounts")
check("and are in the model's context", "A bakery with wholesale accounts" in org.render())
check("an empty line is not saved over them", not S.set_value("org.what_we_do", "  ")["ok"])
S.set_value("org.what_we_do", "A bakery")
ou = S.undo(S.changes()[0]["id"])
check("undo goes through the agent's own ledger", ou["ok"] and org.load()["what_we_do"] == "A bakery with wholesale accounts", str(ou))
check("the organization's words are one row in the agent's ledger, not two", not any(x.get("kind") == "settings" and "What you do" in x.get("summary", "") for x in core.rows(core.CHANGES)))

# ---------------------------------------------------------------- 5. people
check("a new person needs an email address", not S.person_save({"name": "Sam Ortiz", "role": "staff"})["ok"])
check("a role that does not exist is refused", not S.person_save({"name": "Sam Ortiz", "email": "sam@example.com", "role": "boss"})["ok"])
check("an owner's address cannot be given to someone else", not S.person_save({"name": "Sam Ortiz", "email": "Jordan@Example.com", "role": "staff"})["ok"])
a = S.person_save({"name": "Sam Ortiz", "email": "Sam@Example.com", "role": "staff", "title": "Front of house", "reports_to": "jordan"})
p = people()
check("a team member is added", a["ok"] and a["key"] == "sam" and p["users"]["sam"]["emails"] == ["sam@example.com"] and p["users"]["sam"]["role"] == "staff")
check("the agent answers them as that role", core.person_for(email="sam@example.com")["role"] == "staff" and core.can(core.person_for(email="sam@example.com"), "ask")
      and not core.can(core.person_for(email="sam@example.com"), "approve") and not core.is_owner(core.person_for(email="sam@example.com")))
note = next(x for x in org.load()["people"] if x["name"] == "Sam Ortiz")
check("the organization notes say what they handle and who they report to", note["role"] == "staff" and note["about"] == "Front of house; reports to Jordan Lee", str(note))
check("the model reads that structure", "Sam Ortiz (staff): Front of house; reports to Jordan Lee" in org.render())
b = S.person_save({"name": "Sam Park", "email": "park@example.com", "role": "guest"})
check("a second Sam gets a key of their own", b["ok"] and b["key"] == "sam2")
check("nobody reports to themselves", not S.person_save({"key": "sam", "name": "Sam Ortiz", "role": "staff", "reports_to": "sam"})["ok"])
S.person_save({"key": "sam2", "name": "Sam Park", "role": "guest", "reports_to": "sam"})
check("a reporting loop is refused", not S.person_save({"key": "jordan", "name": "Jordan Lee", "role": "owner", "reports_to": "sam2"})["ok"])
check("the last owner cannot be demoted", not S.person_save({"key": "jordan", "name": "Jordan Lee", "role": "staff"})["ok"] and people()["users"]["jordan"]["role"] == "owner")
check("the last owner cannot be removed", not S.person_remove({"key": "jordan"})["ok"])
e = S.person_save({"key": "sam", "name": "Sam Ortiz", "role": "staff", "title": "Editor", "reports_to": "jordan"})
check("an edit keeps their address and what the chat bridge filled in", e["ok"] and people()["users"]["sam"]["emails"] == ["sam@example.com"] and "chat_user" in people()["users"]["sam"])
rm = S.person_remove({"key": "sam"})
check("a person is removed, and nobody is left reporting to them", rm["ok"] and "sam" not in people()["users"] and "reports_to" not in people()["users"]["sam2"]
      and core.person_for(email="sam@example.com") is None and not any(x["name"] == "Sam Ortiz" for x in org.load()["people"]))
un = S.undo(rm["id"])
check("undo brings them back in both places", un["ok"] and people()["users"]["sam"]["title"] == "Editor" and any(x["name"] == "Sam Ortiz" for x in org.load()["people"]))
second = S.person_save({"name": "Lee Chen", "email": "lee@example.com", "role": "owner"})
check("with a second owner, the first can step down", second["ok"] and S.person_save({"key": "jordan", "name": "Jordan Lee", "role": "staff"})["ok"])
S.person_save({"key": "jordan", "name": "Jordan Lee", "role": "owner"})

# ---------------------------------------------------------------- 6. roles
check("the owner role is locked", not S.role_save({"name": "owner", "can": ["ask"]})["ok"] and "approve" in people()["roles"]["owner"]["can"])
check("approving cannot be given to another role", not S.role_save({"name": "staff", "can": ["ask", "approve"]})["ok"] and "approve" not in people()["roles"]["staff"]["can"])
check("nor teaching, nor undo", not S.role_save({"name": "staff", "can": ["teach"]})["ok"] and not S.role_save({"name": "staff", "can": ["undo"]})["ok"])
rs = S.role_save({"name": "staff", "can": ["ask", "note", "task"], "lanes": ["work", "made-up"], "paths": ["Wiki/Clients/", "C:/Windows/"], "about": "the studio team"})
st = people()["roles"]["staff"]
check("a role is given a task hand-off, its lanes and its folders", rs["ok"] and st["can"] == ["ask", "note", "task"] and st["lanes"] == ["work"] and st["paths"] == ["Wiki/Clients/"])
sam = core.person_for(email="sam@example.com")
check("the engine honours it", core.can(sam, "task") and core.person_lanes(sam) == ["work"] and core.person_paths(sam) == ["Wiki/Clients/"])
cr = S.role_save({"name": "Contractor", "can": ["ask"], "lanes": ["work"]})
check("a role of the organization's own can be made", cr["ok"] and cr["name"] == "contractor" and people()["roles"]["contractor"]["can"] == ["ask"])
S.person_save({"key": "sam2", "name": "Sam Park", "role": "contractor"})
check("a role someone holds stays", not S.role_remove({"name": "contractor"})["ok"])
S.person_save({"key": "sam2", "name": "Sam Park", "role": "guest"})
check("an empty one of your own can go; the three built in stay", S.role_remove({"name": "contractor"})["ok"] and not S.role_remove({"name": "staff"})["ok"] and not S.role_remove({"name": "owner"})["ok"])
v = S.people_view()
check("the screen's view of people and roles", [u["key"] for u in v["users"]][:2] == sorted([u["key"] for u in v["users"] if u["role"] == "owner"], key=lambda k: people()["users"][k]["name"].lower())
      and next(r for r in v["roles"] if r["name"] == "owner")["locked"] and next(r for r in v["roles"] if r["name"] == "staff")["members"] == 1
      and [c["key"] for c in v["can_words"]] == ["ask", "note", "task"] and v["lanes"] == ["work", "home"])

# ---------------------------------------------------------------- 7. what it handles
fv = S.features_view()
check("before the gate is answered a feature is on, except a module that is still off", fv and onboarding.features_state() is None
      and [f["key"] for f in fv if not f["on"]] == ["money"])
(TMP / "org.json").write_text(json.dumps({"name": "Acme Bakery", "modules": ["money"]}), encoding="utf-8")
fv = S.features_view()
check("with its module on, it shows on", all(f["on"] for f in fv))
import engine  # noqa: E402
_mj = sorted(engine.MODULES["money"]["jobs"])[0]
(TMP / "spine_jobs.json").write_text(json.dumps({"jobs": [{"name": _mj, "enabled": False}, {"name": "something-else"}]}), encoding="utf-8")
check("the job list is the truth: its jobs off, it shows off", [f["key"] for f in S.features_view() if not f["on"]] == ["money"])
(TMP / "spine_jobs.json").write_text(json.dumps({"jobs": [{"name": _mj}]}), encoding="utf-8")
check("its jobs running, it shows on", all(f["on"] for f in S.features_view()))
ff = S.feature_set({"key": "money", "on": False})
check("turning one off answers the gate and switches its module", ff["ok"] and "money" not in onboarding.features_state()["wanted"] and "crm" in onboarding.features_state()["wanted"]
      and ("money", False) in SWITCHED and onboarding.feature_wanted("money") is False)
check("an unknown feature is refused", not S.feature_set({"key": "rockets", "on": True})["ok"])
fu = S.undo(ff["id"])
check("undo re-opens it", fu["ok"] and onboarding.features_state() is None and SWITCHED[-1] == ("money", True))
S.feature_set({"key": "social", "on": False})
f2 = S.feature_set({"key": "texts", "on": False})
S.undo(f2["id"])
check("undo of a later change keeps the earlier one", onboarding.features_state()["wanted"].count("texts") == 1 and "social" not in onboarding.features_state()["wanted"])

# ---------------------------------------------------------------- 8. standing yeses, the whole screen
policy.save_json(policy.GRANTS_FILE, {"grants": [{"id": "g1", "key": "k", "scope": "always", "option": "Yes", "by": "jordan", "ts": "2026-11-01 09:00",
                                                  "header": "File the weekly report", "lane": "work", "active": True}]})
check("a standing yes is listed", [g["what"] for g in S.grants_view()] == ["File the weekly report"])
check("and taken back", S.grant_revoke({"id": "g1"})["ok"] and S.grants_view() == [] and not S.grant_revoke({"id": "g1"})["ok"])
check("nothing here gives one", not any("grant" in op and "revoke" not in op for op in S.OPS))
snap = S.call({"op": "get"})
check("the whole screen in one call", snap["ok"] and [g["id"] for g in snap["groups"]] == [g[0] for g in S.GROUPS] and snap["agent"] == "Juno"
      and snap["people"]["users"] and snap["features"] and len(snap["locked"]) == len(S.LOCKED) and snap["changes"])
json.dumps(snap)
check("the app's calls", S.call({"op": "set", "key": "doer.live_card", "value": False})["ok"] and cfg()["doer"]["live_card"] is False
      and not S.call({"op": "nope"})["ok"] and not S.call({"op": "set", "key": "cards.delivery", "value": "hourly"})["ok"])
check("the hand-written file still has its layout", (TMP / "config.json").read_bytes().count(b"\r\n") > 10 and b'"_doc": "a note with' in (TMP / "config.json").read_bytes())

print()
print("FAILED: " + ", ".join(FAILS) if FAILS else "all settings checks passed")
sys.exit(1 if FAILS else 0)

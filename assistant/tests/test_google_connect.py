"""Connecting an agent to Google with one sign-in (assistant/google_connect.py), against a stand-in for Google's tool.

The stand-in (written here, run as a program exactly as the real tool is) keeps a pretend Google: who is signed in,
which projects exist, whether the organization blocks new keys. Every command the engine sends is recorded, so the
tests can say what was asked of Google and, as important, what was NOT.

1. The whole run on a Workspace account: project inside the organization, services, identity, key, message line,
   the config file the agent reads, and the sign-in handed back at the end.
2. A personal Google account stops before anything is made; another account carries on.
3. Google's terms not accepted yet: the run stops with that, and continues with the same project name.
4. Google's rule against new keys: nothing about the organization is changed until the owner allows it, and then the
   rule is lifted for the one project only.
5. A finished run asks Google for nothing more.
6. The guided part: what the owner copies, what they paste back (checked before it is saved), the links for this project.
   python assistant/tests/test_google_connect.py
"""
import os as _os, tempfile as _tf
_os.environ.setdefault("WV_LOG_DIR", _tf.mkdtemp(prefix="wv_logs_"))
import json
import os
import shutil
import sys
import tempfile
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")
HERE = Path(__file__).resolve().parent
ROOT = HERE.parent                                   # assistant/
TMP = Path(tempfile.mkdtemp(prefix="wv_gconnect_"))
os.environ["SAGE_HOME"] = str(TMP / "home")
os.environ["SAGE_PACKS"] = str(TMP / "home" / "packs.json")
FAILS = []
sys.path.insert(0, str(ROOT))
import google_connect as g  # noqa: E402

g.time.sleep = lambda s: None                        # the waits for Google to apply a change are not part of the test


def check(name, cond, extra=""):
    print(("ok   " if cond else "FAIL ") + name + (f"  {extra}" if extra and not cond else ""))
    if not cond:
        FAILS.append(name)


FAKE = TMP / "fake_google_tool.py"
FAKE.write_text(r'''
import json, os, sys
from pathlib import Path
P = Path(os.environ["FAKE_GOOGLE"])
S = json.loads(P.read_text(encoding="utf-8"))
a = sys.argv[1:]
S.setdefault("calls", []).append(a)
def out(rc=0, o="", e=""):
    P.write_text(json.dumps(S), encoding="utf-8")
    sys.stdout.write(o); sys.stderr.write(e); sys.exit(rc)
def opt(name):
    return a[a.index(name) + 1] if name in a else ""
j = " ".join(a)
if a[:1] == ["version"]: out(0, "Google Cloud SDK 500.0.0\n")
if a[:2] == ["auth", "list"]: out(0, (S.get("account") or "") + "\n")
if a[:2] == ["auth", "login"]:
    S["account"] = S.get("login_as") or ""
    out(0 if S["account"] else 1, "", "" if S["account"] else "ERROR: (gcloud.auth.login) the browser was closed\n")
if a[:2] == ["auth", "revoke"]: S["account"] = ""; out(0)
if a[:2] == ["organizations", "list"]: out(0, json.dumps(S.get("orgs") or []))
if a[:2] == ["organizations", "add-iam-policy-binding"]: S["org_admin"] = [a[2], opt("--member"), opt("--role")]; out(0)
if a[:2] == ["organizations", "get-iam-policy"]: out(0, " ".join(S.get("rule_admins") or []))
if a[:2] == ["org-policies", "describe"]:
    on = S.get("key_blocked") and not S.get("policy_off") and "managed" not in a[2]
    out(0, "name: projects/1/policies/" + a[2] + " spec: rules: - enforce: " + ("true" if on else "false"))
if a[:2] == ["projects", "describe"]:
    n = (S.get("projects") or {}).get(a[2])
    out(0, n + "\n") if n else out(1, "", "ERROR: (gcloud.projects.describe) NOT_FOUND: project not found\n")
if a[:2] == ["projects", "create"]:
    if S.get("tos"): out(1, "", "ERROR: (gcloud.projects.create) FAILED_PRECONDITION: Callers must accept Terms of Service\n")
    S.setdefault("projects", {})[a[2]] = "424242424242"; S["project_org"] = opt("--organization"); out(0)
if a[:2] == ["services", "enable"]: S["services"] = [x for x in a[2:] if "." in x]; out(0)
if a[:3] == ["iam", "service-accounts", "describe"]:
    S["described"] = S.get("described", 0) + 1
    if S.get("sa") and S["described"] > S.get("slow_identity", 0): out(0, a[3] + "\n")
    out(1, "", "ERROR: (gcloud.iam.service-accounts.describe) NOT_FOUND: Unknown service account\n")
if a[:3] == ["iam", "service-accounts", "create"]:
    if S.get("sa"): out(1, "", "ERROR: (gcloud.iam.service-accounts.create) Resource in projects is the subject of a conflict: Service account already exists\n")
    S["sa"] = a[3]; out(0)
if a[:4] == ["iam", "service-accounts", "keys", "create"]:
    if S.get("key_blocked") and not S.get("policy_off"):
        out(1, "", "ERROR: (gcloud.iam.service-accounts.keys.create) FAILED_PRECONDITION: Key creation is not allowed on this service account.\n- constraints/iam.disableServiceAccountKeyCreation\n")
    Path(a[4]).write_text(json.dumps({"type": "service_account", "client_email": opt("--iam-account"), "private_key": "-----BEGIN PRETEND KEY-----"}), encoding="utf-8")
    S["keys"] = S.get("keys", 0) + 1; out(0)
if a[:2] == ["org-policies", "set-policy"]:
    body = Path(a[2]).read_text(encoding="utf-8")
    S.setdefault("policies", []).append(body)
    if "iam.managed." in body: out(1, "", "ERROR: (gcloud.org-policies.set-policy) NOT_FOUND: no such constraint\n")
    S["policy_off"] = True; out(0)
if a[:1] == ["pubsub"]:
    key = " ".join(a[:4])
    if key in S.setdefault("pubsub", []) and "add-iam-policy-binding" not in a: out(1, "", "ERROR: (gcloud.pubsub) ALREADY_EXISTS: Resource already exists\n")
    S["pubsub"].append(key); out(0)
out(2, "", "ERROR: the stand-in does not know: " + j + "\n")
''', encoding="utf-8")
g.GCLOUD_CMD = [sys.executable, str(FAKE)]


def world(name: str, **kw) -> tuple:
    """A fresh pretend Google and a fresh organization folder. -> (pack, read) where read() is the pretend Google now."""
    f = TMP / f"{name}.json"
    f.write_text(json.dumps(dict({"orgs": [{"name": "organizations/123456", "displayName": "northwind.test"}], "login_as": "dana@northwind.test"}, **kw)), encoding="utf-8")
    os.environ["FAKE_GOOGLE"] = str(f)
    pack = TMP / name
    a = pack / "assistant"
    (a / "agent").mkdir(parents=True)
    (a / "data").mkdir()
    (a / "org.json").write_text(json.dumps({"name": "Northwind Studio", "google_home": str(TMP / name / "keys")}), encoding="utf-8")
    (a / "agent" / "profile.json").write_text(json.dumps({"name": "Juno"}), encoding="utf-8")
    (a / "agent" / "people.json").write_text(json.dumps({"users": {"dana": {"name": "Dana Whitfield", "email": "dana@northwind.test"}}}), encoding="utf-8")
    shutil.copytree(ROOT / "agent" / "gchat_relay", a / "agent" / "gchat_relay")
    return pack, lambda: json.loads(f.read_text(encoding="utf-8"))


def said(read, *words) -> list:
    return [c for c in read().get("calls", []) if all(w in " ".join(c) for w in words)]


# ---------------------------------------------------------------- 1. the whole run
pack, G = world("whole")
notes = []
r = g.run(pack, notes.append)
st = g.load(pack)
gl = json.loads((pack / "assistant" / "agent" / "gchat.local.json").read_text(encoding="utf-8")) if (pack / "assistant" / "agent" / "gchat.local.json").exists() else {}
key = pack / "keys" / "agent_sa.json"
check("one run does every step Google lets a program do", r.get("ok") is True and all(st["done"].get(k) for k, _ in g.STEPS), json.dumps(r)[:200])
check("the project is made inside the organization the owner signed in with, named for the agent", G().get("project_org") == "123456" and st["project_id"].startswith("juno-chat-") and st["project_number"] == "424242424242")
check("the services the agent needs are switched on", {"chat.googleapis.com", "gsuiteaddons.googleapis.com", "pubsub.googleapis.com"} <= set(G().get("services") or []))
check("the agent's key is in the organization's keys folder, whole, and no half-written file is left", key.exists() and g._key_ok(key, st["sa_email"]) and not (pack / "keys" / "agent_sa.json.part").exists())
check("the config file the agent reads is written: project, number, message line, key, pull mode, a secret", gl.get("project_id") == st["project_id"] and gl.get("project_number") == "424242424242"
      and gl.get("subscription") == g.SUBSCRIPTION and gl.get("relay_mode") == "pull" and len(gl.get("relay_secret", "")) >= 32 and "agent_sa.json" in gl.get("sa_key", ""), json.dumps(gl)[:300])
check("the agent's identity may read its message line", bool(said(G, "pubsub", "add-iam-policy-binding", st["sa_email"], "roles/pubsub.subscriber")))
check("the sign-in is handed back at the end: no owner account stays signed in on this computer", G().get("account") == "" and said(G, "auth", "revoke")[-1:] == [["auth", "revoke", "--all"]])
check("no rule of the Google organization was touched on a run that needed none", not said(G, "org-policies") and not said(G, "organizations", "add-iam-policy-binding"))
check("the window is told which step is running", [n for n in notes if n.startswith("##step ")] == ["##step " + k for k, _ in g.STEPS if k != "tool"], str(notes)[:200])
s = g.status(pack)
check("status: every step done, the guided part's values ready", s["auto_done"] and not s["relay_done"] and s["guide"]["project_number"] == "424242424242" and st["project_id"] in s["guide"]["links"]["chat_config"])

# ---------------------------------------------------------------- 2. a personal account
pack, G = world("personal", login_as="dana.w@" + g.PERSONAL[0])          # a made-up personal address, put together here
r = g.run(pack)
check("a personal Google account stops the run, says why in plain words, and nothing is made", r.get("ok") is False and r.get("needs") == "workspace" and "Workspace" in r["message"]
      and not said(G, "projects", "create"), json.dumps(r)[:200])
f = TMP / "personal.json"
w = json.loads(f.read_text(encoding="utf-8")); w["login_as"] = "dana@northwind.test"; f.write_text(json.dumps(w), encoding="utf-8")
r = g.run(pack, other_account=True)
check("signing in with the Workspace account instead carries on to the end", r.get("ok") is True and g.load(pack)["account"] == "dana@northwind.test", json.dumps(r)[:200])

# ---------------------------------------------------------------- 3. the terms
pack, G = world("terms", tos=True)
r = g.run(pack)
tried = g.load(pack).get("project_try")
check("Google's terms not accepted yet: the run stops with that one thing to do", r.get("ok") is False and r.get("needs") == "terms" and r.get("at") == "project", json.dumps(r)[:200])
f = TMP / "terms.json"
w = json.loads(f.read_text(encoding="utf-8")); w["tos"] = False; f.write_text(json.dumps(w), encoding="utf-8")
r = g.run(pack)
check("after that, the same run finishes, with the project name it first chose", r.get("ok") is True and g.load(pack)["project_id"] == tried)

# ---------------------------------------------------------------- 4. the rule against new keys
pack, G = world("rule", key_blocked=True)
r = g.run(pack)
check("the rule against new keys stops the run and ASKS; nothing in the Google organization has been changed", r.get("ok") is False and r.get("needs") == "keys"
      and not said(G, "org-policies") and not said(G, "organizations", "add-iam-policy-binding") and not (pack / "keys" / "agent_sa.json").exists(), json.dumps(r)[:200])
check("status shows where it stopped and what it needs", g.status(pack)["needs"] == "keys" and g.status(pack)["at"] == "identity")
r = g.run(pack, allow_keys_rule=True)
pid = g.load(pack)["project_id"]
pol = G().get("policies") or []
check("with the owner's Allow the key is made and the run finishes", r.get("ok") is True and (pack / "keys" / "agent_sa.json").exists(), json.dumps(r)[:200])
check("the rule is lifted for this ONE project, never for the organization", bool(pol) and all(p.startswith(f"name: projects/{pid}/policies/") and "enforce: false" in p for p in pol)
      and not any("organizations/" in p for p in pol), str(pol)[:300])
check("the owner's own account is the one given the right to change it", G().get("org_admin") == ["123456", "user:dana@northwind.test", "roles/orgpolicy.policyAdmin"], str(G().get("org_admin")))
check("only the name the rule is in force under is lifted (the other is left as it is)", len(pol) == 1 and "iam.disableServiceAccountKeyCreation" in pol[0] and "managed" not in pol[0], str(pol)[:200])
pack3, G3 = world("holder", key_blocked=True, rule_admins=["user:someone@northwind.test", "user:dana@northwind.test"])
g.run(pack3)
r = g.run(pack3, allow_keys_rule=True)
check("an owner who already holds the right to manage rules is given nothing: the organization's own access list is not touched",
      r.get("ok") is True and not said(G3, "organizations", "add-iam-policy-binding") and "rule_right_given" not in g.load(pack3), str(G3().get("org_admin")))

pack2, G2 = world("slow", slow_identity=3)
r = g.run(pack2)
calls = [" ".join(c[:4]) for c in G2().get("calls", [])]
first_key = next(i for i, c in enumerate(calls) if c.startswith("iam service-accounts keys create"))
check("the key is asked for only once Google knows the new identity (asked too early, the request hung on the first live run)",
      r.get("ok") is True and calls[:first_key].count("iam service-accounts describe sage-agent@" + g.load(pack2)["project_id"] + ".iam.gserviceaccount.com") == 4, str(calls[first_key - 5:first_key + 1]))
check("a refused key leaves no empty half-written file in the keys folder", not (pack / "keys" / "agent_sa.json.part").exists())

# ---------------------------------------------------------------- 5. a finished run asks for nothing more
before = len(G().get("calls", []))
r = g.run(pack)
check("running again when everything is done sends Google nothing", r.get("ok") is True and len(G().get("calls", [])) == before, f"{len(G().get('calls', [])) - before} more calls")

# ---------------------------------------------------------------- 6. the guided part
code, man, sec = g.text(pack, "code"), g.text(pack, "manifest"), g.text(pack, "secret")
gl = json.loads((pack / "assistant" / "agent" / "gchat.local.json").read_text(encoding="utf-8"))
check("what the owner copies: the relay's code, a manifest carrying the agent's name, the shared secret (the same every time)",
      code["ok"] and "function " in code["text"] and json.loads(man["text"])["addOns"]["common"]["name"] == "Juno" and sec["text"] == gl["relay_secret"] == g.text(pack, "secret")["text"])
check("an unknown name is refused", g.text(pack, "password")["ok"] is False)
dep, url = "AKfycbx" + "Q" * 40, "https://script.google.com/macros/s/AKfycby" + "W" * 40 + "/exec"
check("a pasted address that is not a web app address is refused in plain words", g.save_relay(pack, "https://script.google.com/home/projects/abc/edit", dep)["ok"] is False)
check("a pasted ID that is not a deployment ID is refused", g.save_relay(pack, url, "juno-relay")["ok"] is False)
check("the web app's own ID pasted twice is caught", g.save_relay(pack, url, "AKfycby" + "W" * 40)["ok"] is False)
r = g.save_relay(pack, url + " ", " " + dep)
gl = json.loads((pack / "assistant" / "agent" / "gchat.local.json").read_text(encoding="utf-8"))
check("the two things the owner pastes back are saved where the agent reads them", r["ok"] and gl["relay_webapp_url"] == url and gl["relay_deployment_id"] == dep and g.status(pack)["relay_done"])
gd = g.guide(pack)
check("the guide carries this project's own links and the values for the Chat page", pid in gd["links"]["signin_screen"] and pid in gd["links"]["chat_config"] and gd["app_name"] == "Juno"
      and gd["owners"] == "dana@northwind.test" and gd["deployment_id"] == dep and gd["avatar"].startswith("https://"))
src = (ROOT / "google_connect.py").read_text(encoding="utf-8")
check("the engine keeps Google's tool and its sign-in in the app's own folder, apart from any the owner already uses", "CLOUDSDK_CONFIG" in src and 'sdk_home() / "config"' in src)

print()
print("ALL PASSED" if not FAILS else f"{len(FAILS)} FAILED: {FAILS}", f"({TMP})")
if not FAILS:
    shutil.rmtree(TMP, ignore_errors=True)
sys.exit(1 if FAILS else 0)

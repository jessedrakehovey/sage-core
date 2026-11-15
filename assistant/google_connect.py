"""google_connect.py - connecting an agent to Google Chat with one sign-in (sage-core, 2026-11-14).

The Google side of a new agent used to be an hour in Google's consoles: a Cloud project, the services, an identity
for the agent and its key (behind a rule Google sets on every new organization), a message line, then the relay
script and the Chat app's own page. Everything Google lets a program do is done here, by Google's own command-line
tool, after the owner signs in ONCE in their browser:

  tool      Google's command-line tool, fetched from Google into the app's own folder (nothing is installed system-wide,
            and it keeps its settings in a folder of its own, apart from any the owner already uses)
  signin    the owner signs in to Google in their browser and allows it (the one sign-in)
  project   a Cloud project for the agent, inside the organization the owner signed in with
  services  Google Chat, add-ons, Pub/Sub, Gmail and Calendar switched on for that project
  identity  the agent's own identity (a service account) and its key file, saved in the organization's keys folder
            (when Google's rule against new keys stands in the way, the run stops and ASKS: nothing about the owner's
            Google organization is changed without their Allow, and then only for this one project)
  line      the message line (a Pub/Sub topic and subscription) and the agent's right to read it
  signout   the sign-in is handed back: no owner-level Google credential stays on this computer

What Google offers only as pages (the relay script and the Chat app's page) is the guided part: guide() hands the
app the exact values and links for THIS project, text() the code, the manifest and the secret to copy, save_relay()
takes the two things the owner copies back, and check() proves the whole line end to end. The app (assistant/app.py)
is the only caller; `python assistant/google_connect.py status` prints where a pack stands.

State: <pack>/assistant/data/google_connect.json. Every step first looks at what is already there, so a run that
stopped (a closed browser tab, no network, a rule in the way) picks up where it stopped. Stdlib only.
"""
from __future__ import annotations

import json
import os
import re
import secrets
import shutil
import subprocess
import sys
import time
import urllib.request
import zipfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import welcome  # noqa: E402

IS_WIN = os.name == "nt"
NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)
SDK_URL = "https://dl.google.com/dl/cloudsdk/channels/rapid/google-cloud-sdk.zip"          # Google's own download; runs on the pack's Python
SERVICES = ("chat.googleapis.com", "gsuiteaddons.googleapis.com", "pubsub.googleapis.com", "iam.googleapis.com",
            "orgpolicy.googleapis.com", "gmail.googleapis.com", "calendar-json.googleapis.com")
SA_NAME = "sage-agent"
TOPIC, SUBSCRIPTION = "chat-app-events", "chat-app-events-sub"
KEY_RULES = ("iam.disableServiceAccountKeyCreation", "iam.managed.disableServiceAccountKeyCreation")
AVATAR = "https://developers.google.com/chat/images/quickstart-app-avatar.png"
PERSONAL = ("gmail.com", "googlemail.com")
STEPS = [("tool", "Getting Google's setup tool"), ("signin", "Signing in to Google"), ("project", "Making a home in Google Cloud"),
         ("services", "Switching on the services"), ("identity", "Giving the agent its own identity"), ("line", "Opening the message line"),
         ("signout", "Handing the sign-in back")]
GCLOUD_CMD: list = []            # a test names its stand-in here; empty = the tool in the app's own folder


# ---------------------------------------------------------------- state
def _state_path(pack: Path) -> Path:
    return pack / "assistant" / "data" / "google_connect.json"


def load(pack: Path) -> dict:
    return welcome._load(_state_path(pack), {}) or {}


def save(pack: Path, st: dict) -> None:
    st["ts"] = time.strftime("%Y-%m-%dT%H:%M:%S")
    welcome._save(_state_path(pack), st)


def _org(pack: Path) -> dict:
    return welcome._load(pack / "assistant" / "org.json", {}) or {}


def _agent(pack: Path) -> str:
    return str((welcome._load(pack / "assistant" / "agent" / "profile.json", {}) or {}).get("name") or "Agent")


def _owners(pack: Path) -> list:
    users = ((welcome._load(pack / "assistant" / "agent" / "people.json", {}) or {}).get("users") or {}).values()
    return [str(u.get("email") or "") for u in users if u.get("email")]


def keys_dir(pack: Path) -> Path:
    o = _org(pack)
    return Path(os.path.expanduser(str(o.get("google_home") or o.get("secrets_dir") or (Path.home() / ".sage" / "google"))))


def _gl_path(pack: Path) -> Path:
    return pack / "assistant" / "agent" / "gchat.local.json"


def _slug(s: str) -> str:
    return re.sub(r"-+", "-", re.sub(r"[^a-z0-9]+", "-", s.lower())).strip("-")


# ---------------------------------------------------------------- Google's tool
def sdk_home() -> Path:
    return welcome.sage_home() / "google-setup"


def _gcloud_py() -> Path:
    return sdk_home() / "google-cloud-sdk" / "lib" / "gcloud.py"


def gc(pack: Path, args: list, timeout: int = 300, note=None) -> tuple:
    """Run one command of Google's tool with no window. (exit code, stdout, stderr). Its settings and its sign-in live in
    the app's own folder (CLOUDSDK_CONFIG), never in a configuration the owner already has on this computer."""
    py = welcome.pack_python(pack)
    argv = (list(GCLOUD_CMD) if GCLOUD_CMD else [py, str(_gcloud_py())]) + [str(a) for a in args]
    env = {k: v for k, v in os.environ.items() if not k.startswith("CLOUDSDK_")}
    env.update(CLOUDSDK_CONFIG=str(sdk_home() / "config"), CLOUDSDK_PYTHON=py, CLOUDSDK_CORE_DISABLE_PROMPTS="1",
               CLOUDSDK_CORE_DISABLE_USAGE_REPORTING="1", CLOUDSDK_COMPONENT_MANAGER_DISABLE_UPDATE_CHECK="1", PYTHONIOENCODING="utf-8")
    try:
        r = subprocess.run(argv, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=timeout, env=env,
                           creationflags=NO_WINDOW, stdin=subprocess.DEVNULL)
        return r.returncode, r.stdout or "", r.stderr or ""
    except subprocess.TimeoutExpired:
        return 124, "", "Google did not answer in time"
    except OSError as e:
        return 127, "", str(e)


def _why(err: str) -> str:
    """The sentence in Google's answer that says what went wrong (its tool prints a traceback's worth around it)."""
    lines = [ln.strip() for ln in (err or "").splitlines() if ln.strip()]
    for ln in lines:
        if ln.startswith("ERROR:"):
            return re.sub(r"^ERROR:\s*(\([\w.-]+\)\s*)?", "", ln)[:300]
    return (lines[-1] if lines else "no reason given")[:300]


def have_tool() -> bool:
    return bool(GCLOUD_CMD) or _gcloud_py().exists()


def get_tool(pack: Path, note) -> dict:
    if have_tool():
        return {"ok": True}
    home = sdk_home()
    home.mkdir(parents=True, exist_ok=True)
    z = home / "google-cloud-sdk.zip"
    try:
        with urllib.request.urlopen(SDK_URL, timeout=60) as r:
            total = int(r.headers.get("Content-Length") or 0)
            got = 0
            with open(str(z) + ".part", "wb") as f:
                while True:
                    chunk = r.read(1 << 18)
                    if not chunk:
                        break
                    f.write(chunk)
                    got += len(chunk)
                    if total:
                        note(f"{got * 100 // total}% of {total // (1 << 20)} MB")
        Path(str(z) + ".part").replace(z)
        note("unpacking")
        with zipfile.ZipFile(z) as zf:
            zf.extractall(home)
        z.unlink()
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "message": f"Google's setup tool did not download ({type(e).__name__}). Check the internet connection and try again."}
    rc, out, err = gc(pack, ["version"], timeout=180)
    if rc != 0:
        return {"ok": False, "message": "Google's setup tool did not start on this computer: " + _why(err or out)}
    return {"ok": True}


def remove_tool() -> None:
    shutil.rmtree(sdk_home(), ignore_errors=True)


# ---------------------------------------------------------------- the steps Google lets a program do
def _account(pack: Path) -> str:
    rc, out, _ = gc(pack, ["auth", "list", "--filter=status:ACTIVE", "--format=value(account)"], timeout=60)
    return out.strip().splitlines()[0].strip() if rc == 0 and out.strip() else ""


def step_signin(pack: Path, st: dict, note) -> dict:
    acct = _account(pack)
    if not acct:
        note("Your browser is opening. Sign in to Google and choose Allow.")
        rc, out, err = gc(pack, ["auth", "login", "--brief"], timeout=600)
        acct = _account(pack)
        if not acct:
            return {"ok": False, "message": "The Google sign-in did not finish. Try again, and choose Allow when Google asks." if rc in (0, 124)
                    else "The Google sign-in did not finish: " + _why(err)}
    st["account"] = acct
    if acct.rsplit("@", 1)[-1].lower() in PERSONAL:
        return {"ok": False, "needs": "workspace", "message": f"{acct} is a personal Google account. Google Chat apps need Google Workspace: "
                "an account on your organization's own domain. Sign in with that one instead."}
    return {"ok": True}


def step_project(pack: Path, st: dict, note) -> dict:
    if st.get("project_id"):
        rc, out, err = gc(pack, ["projects", "describe", st["project_id"], "--format=value(projectNumber)"], timeout=90)
        if rc == 0 and out.strip():
            st["project_number"] = out.strip()
            return {"ok": True}
    agent = _agent(pack)
    domain = str(st.get("account") or "").rsplit("@", 1)[-1].lower()
    rc, out, _ = gc(pack, ["organizations", "list", "--format=json"], timeout=90)
    try:
        orgs = json.loads(out) if rc == 0 and out.strip() else []
    except ValueError:
        orgs = []
    mine = next((o for o in orgs if str(o.get("displayName") or "").lower() == domain), orgs[0] if orgs else None)
    st["org_id"] = str((mine or {}).get("name") or "").rsplit("/", 1)[-1]
    pid = st.get("project_try") or f"{(_slug(agent) or 'agent')[:16]}-chat-{secrets.token_hex(3)}"
    if not pid[0].isalpha():
        pid = "a" + pid
    st["project_try"] = pid
    name = (re.sub(r"[^A-Za-z0-9 -]", "", agent)[:22].strip() or "Agent") + " agent"
    args = ["projects", "create", pid, "--name", name] + (["--organization", st["org_id"]] if st["org_id"] else [])
    rc, out, err = gc(pack, args, timeout=180)
    if rc != 0 and "already exists" not in err and "already in use" not in err:
        if "terms of service" in err.lower():
            return {"ok": False, "needs": "terms", "message": "Google Cloud asks every new account to accept its terms once. Open Google Cloud, accept them, then come back and continue."}
        st.pop("project_try", None)
        return {"ok": False, "message": "Google would not make the project: " + _why(err)}
    rc, out, err = gc(pack, ["projects", "describe", pid, "--format=value(projectNumber)"], timeout=90)
    if rc != 0 or not out.strip():
        return {"ok": False, "message": "The project was made but Google has not shown it yet. Continue in a minute."}
    st["project_id"], st["project_number"] = pid, out.strip()
    return {"ok": True}


def step_services(pack: Path, st: dict, note) -> dict:
    rc, out, err = gc(pack, ["services", "enable", *SERVICES, "--project", st["project_id"]], timeout=600)
    if rc != 0:
        return {"ok": False, "message": "Google would not switch the services on: " + _why(err)}
    return {"ok": True}


def _key_ok(path: Path, email: str) -> bool:
    try:
        d = json.loads(path.read_text(encoding="utf-8"))
        return d.get("type") == "service_account" and d.get("client_email") == email and bool(d.get("private_key"))
    except (OSError, ValueError):
        return False


def _blocked(err: str) -> bool:
    e = err or ""
    return "disableServiceAccountKeyCreation" in e or "Key creation is not allowed" in e


def allow_keys(pack: Path, st: dict, note) -> dict:
    """The owner said Allow: Google's rule against new keys is lifted for THIS project only (never for the
    organization). Two steps, as Google's console does it: the owner's account gets the right to manage rules, then the
    rule is overridden on the project. Both names the rule goes by are tried; one of them not existing is fine."""
    pid, acct, org = st["project_id"], st.get("account") or "", st.get("org_id") or ""
    if org and acct:
        rc, out, err = gc(pack, ["organizations", "add-iam-policy-binding", org, "--member", f"user:{acct}", "--role", "roles/orgpolicy.policyAdmin",
                                 "--condition=None"], timeout=180)
        if rc != 0:
            return {"ok": False, "message": "Google would not let this account manage the organization's rules (it must be an administrator of the Google Workspace): " + _why(err)}
    sdk_home().mkdir(parents=True, exist_ok=True)
    tmp = sdk_home() / "policy.yaml"
    done, last = 0, ""
    for rule in KEY_RULES:
        tmp.write_text(f"name: projects/{pid}/policies/{rule}\nspec:\n  rules:\n  - enforce: false\n", encoding="utf-8")
        for _ in range(4):                       # the right just granted takes a little while to count
            rc, out, err = gc(pack, ["org-policies", "set-policy", str(tmp), "--billing-project", pid], timeout=180)
            if rc == 0:
                done += 1
                break
            last = _why(err)
            if "PERMISSION_DENIED" not in err and "permission" not in err.lower():
                break
            note("Waiting for Google to apply the permission")
            time.sleep(20)
    try:
        tmp.unlink()
    except OSError:
        pass
    if not done:
        return {"ok": False, "message": "Google would not change the rule for this project: " + last}
    st["keys_allowed"] = time.strftime("%Y-%m-%dT%H:%M:%S")
    return {"ok": True}


def step_identity(pack: Path, st: dict, note, allow: bool = False) -> dict:
    pid = st["project_id"]
    email = f"{SA_NAME}@{pid}.iam.gserviceaccount.com"
    st["sa_email"] = email
    kd = keys_dir(pack)
    key = kd / "agent_sa.json"
    if _key_ok(key, email):
        st["key_path"] = str(key)
        return {"ok": True}
    rc, out, err = gc(pack, ["iam", "service-accounts", "create", SA_NAME, "--display-name", re.sub(r"[^A-Za-z0-9 -]", "", _agent(pack))[:60] or "Agent",
                             "--project", pid], timeout=180)
    if rc != 0 and "already exists" not in err:
        return {"ok": False, "message": "Google would not make the agent's identity: " + _why(err)}
    kd.mkdir(parents=True, exist_ok=True)
    part = kd / "agent_sa.json.part"
    waited = False
    for attempt in range(10):
        rc, out, err = gc(pack, ["iam", "service-accounts", "keys", "create", str(part), "--iam-account", email, "--project", pid], timeout=180)
        if rc == 0 and _key_ok(part, email):
            part.replace(key)
            st["key_path"] = str(key)
            return {"ok": True}
        if _blocked(err):
            if not allow and not st.get("keys_allowed"):
                return {"ok": False, "needs": "keys", "message": "Google blocks new keys on every new organization until an administrator allows them."}
            if not st.get("keys_allowed"):
                r = allow_keys(pack, st, note)
                if not r["ok"]:
                    return r
            note("Waiting for Google to apply the change (a few minutes)")
            waited = True
            time.sleep(30)
            continue
        if "NOT_FOUND" in err and attempt < 3:      # the identity made a second ago is not visible everywhere yet
            time.sleep(8)
            continue
        return {"ok": False, "message": "Google would not make the key: " + _why(err)}
    return {"ok": False, "message": "Google has not applied the change yet. Continue in a few minutes." if waited else "Google would not make the key."}


def step_line(pack: Path, st: dict, note) -> dict:
    pid, email = st["project_id"], st["sa_email"]
    for args in (["pubsub", "topics", "create", TOPIC, "--project", pid],
                 ["pubsub", "subscriptions", "create", SUBSCRIPTION, "--topic", TOPIC, "--project", pid],
                 ["pubsub", "subscriptions", "add-iam-policy-binding", SUBSCRIPTION, "--member", f"serviceAccount:{email}", "--role", "roles/pubsub.subscriber", "--project", pid]):
        rc, out, err = gc(pack, args, timeout=180)
        if rc != 0 and "ALREADY_EXISTS" not in err and "already exists" not in err.lower():
            return {"ok": False, "message": "Google would not open the message line: " + _why(err)}
    gl = welcome._load(_gl_path(pack), {}) or {}
    key = Path(st["key_path"])
    try:
        shown = "~/" + key.relative_to(Path.home()).as_posix()
    except ValueError:
        shown = str(key)
    gl.update(project_id=pid, project_number=st.get("project_number") or "", subscription=SUBSCRIPTION, sa_key=shown, relay_mode="pull")
    if not gl.get("relay_secret"):
        gl["relay_secret"] = secrets.token_urlsafe(32)
    welcome._save(_gl_path(pack), gl)
    return {"ok": True}


def step_signout(pack: Path, st: dict, note) -> dict:
    gc(pack, ["auth", "revoke", "--all"], timeout=90)          # nothing of the owner's Google account stays on this computer
    return {"ok": True}


RUN = {"tool": lambda pack, st, note, allow: get_tool(pack, note), "signin": lambda pack, st, note, allow: step_signin(pack, st, note),
       "project": lambda pack, st, note, allow: step_project(pack, st, note), "services": lambda pack, st, note, allow: step_services(pack, st, note),
       "identity": lambda pack, st, note, allow: step_identity(pack, st, note, allow), "line": lambda pack, st, note, allow: step_line(pack, st, note),
       "signout": lambda pack, st, note, allow: step_signout(pack, st, note)}


def run(pack: Path, note=lambda s: None, allow_keys_rule: bool = False, other_account: bool = False) -> dict:
    """Every step in order, each skipped when it is already done. Stops at the first that fails or needs the owner
    (`needs`: workspace | terms | keys); the state file remembers, so the next run carries on from there."""
    st = load(pack)
    done = st.setdefault("done", {})
    if other_account:
        if have_tool():
            gc(pack, ["auth", "revoke", "--all"], timeout=90)
        done.pop("signin", None)
        st.pop("account", None)
    st.pop("needs", None)
    st.pop("error", None)
    work = [k for k, _ in STEPS if k not in ("tool", "signin", "signout") and not done.get(k)]
    for key, title in STEPS:
        if key in ("tool", "signin"):
            # the tool and the sign-in are means, not ends: wanted only while a step that uses them is still to do,
            # and looked at afresh each run (the tool may have been removed, the sign-in handed back)
            if not work or (key == "tool" and have_tool()) or (key == "signin" and st.get("account") and _account(pack) == st["account"]):
                done.setdefault(key, time.strftime("%Y-%m-%dT%H:%M:%S"))
                continue
        elif done.get(key):
            continue
        if key == "signout" and not have_tool():
            done[key] = time.strftime("%Y-%m-%dT%H:%M:%S")
            continue
        st["at"] = key
        save(pack, st)
        note("##step " + key)
        r = RUN[key](pack, st, note, allow_keys_rule)
        if not r.get("ok"):
            st["needs"], st["error"] = r.get("needs") or "", r.get("message") or ""
            save(pack, st)
            return {"ok": False, "needs": st["needs"], "message": st["error"], "at": key}
        done[key] = time.strftime("%Y-%m-%dT%H:%M:%S")
        save(pack, st)
    st.pop("at", None)
    save(pack, st)
    return {"ok": True, "message": "Google is ready for the last two steps."}


# ---------------------------------------------------------------- the guided part (pages Google offers no program a way into)
def guide(pack: Path) -> dict:
    st = load(pack)
    gl = welcome._load(_gl_path(pack), {}) or {}
    pid, num, agent = st.get("project_id") or "", st.get("project_number") or "", _agent(pack)
    org = str(_org(pack).get("name") or "")
    return {
        "agent": agent, "project_id": pid, "project_number": num, "account": st.get("account") or "",
        "app_name": agent, "avatar": AVATAR, "description": f"{agent}, {org}'s assistant"[:40] if org else f"{agent}, your assistant",
        "owners": ", ".join(_owners(pack)), "deployment_id": str(gl.get("relay_deployment_id") or ""), "webapp_url": str(gl.get("relay_webapp_url") or ""),
        "links": {
            "signin_screen": f"https://console.cloud.google.com/auth/overview?project={pid}",
            "script_new": "https://script.google.com/home/projects/create",
            "chat_config": f"https://console.cloud.google.com/apis/api/chat.googleapis.com/hangouts-chat?project={pid}",
            "chat": "https://chat.google.com/",
        },
    }


def text(pack: Path, what: str) -> dict:
    """What the owner copies into Google's pages: the relay's code, its manifest (carrying the agent's name) or the secret
    the relay and this computer share. Handed to the app's own window for the clipboard, never shown on a page."""
    relay = pack / "assistant" / "agent" / "gchat_relay"
    try:
        if what == "code":
            return {"ok": True, "text": (relay / "Code.gs").read_text(encoding="utf-8").replace("\r\n", "\n")}
        if what == "manifest":
            m = json.loads((relay / "appsscript.json").read_text(encoding="utf-8"))
            m.setdefault("addOns", {}).setdefault("common", {})["name"] = _agent(pack)
            return {"ok": True, "text": json.dumps(m, indent=2)}
        if what == "secret":
            gl = welcome._load(_gl_path(pack), {}) or {}
            if not gl.get("relay_secret"):
                gl["relay_secret"] = secrets.token_urlsafe(32)
                welcome._save(_gl_path(pack), gl)
            return {"ok": True, "text": gl["relay_secret"]}
    except (OSError, ValueError) as e:
        return {"ok": False, "message": f"that file is missing from the agent's folder ({type(e).__name__})"}
    return {"ok": False, "message": "nothing by that name"}


def save_relay(pack: Path, webapp_url: str, deployment_id: str) -> dict:
    url, dep = str(webapp_url or "").strip(), str(deployment_id or "").strip()
    if not re.fullmatch(r"https://script\.google\.com/(a/macros/[\w.-]+|macros)/s/[\w-]+/exec", url):
        return {"ok": False, "message": "The web app address should look like https://script.google.com/macros/s/.../exec. Copy it from the first deployment."}
    if not re.fullmatch(r"AKfyc[\w-]{20,}", dep):
        return {"ok": False, "message": "The deployment ID should start with AKfyc. Copy it from the second (Add-on) deployment."}
    if dep in url:
        return {"ok": False, "message": "That ID belongs to the web app. The second deployment (Add-on) has its own ID: copy that one."}
    gl = welcome._load(_gl_path(pack), {}) or {}
    gl.update(relay_webapp_url=url, relay_deployment_id=dep, relay_mode="pull")
    welcome._save(_gl_path(pack), gl)
    st = load(pack)
    st.setdefault("done", {})["relay"] = time.strftime("%Y-%m-%dT%H:%M:%S")
    save(pack, st)
    return {"ok": True, "message": "Saved."}


def check(pack: Path) -> dict:
    """The proof: the agent's key signs in and reads its message line, and the relay answers this computer. Run by the
    pack's own programs (gchat.py --test, sage relay check), so it is the same test the agent's own doctor trusts."""
    py = welcome.pack_python(pack)
    env = {k: v for k, v in os.environ.items() if k not in ("CLAUDECODE", "CLAUDE_CODE_ENTRYPOINT")}
    env["PYTHONIOENCODING"] = "utf-8"
    out = {}
    for name, argv in (("key", [py, str(pack / "assistant" / "agent" / "gchat.py"), "--test"]), ("relay", [py, str(pack / "assistant" / "sage.py"), "relay", "check"])):
        try:
            r = subprocess.run(argv, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=120, cwd=str(pack), env=env, creationflags=NO_WINDOW)
            txt = ((r.stdout or "") + (r.stderr or "")).strip()
            out[name] = {"ok": r.returncode == 0 and ("OK" in txt if name == "key" else "reachable" in txt and "NOT" not in txt), "detail": txt.splitlines()[-1][:200] if txt else ""}
        except (OSError, subprocess.TimeoutExpired) as e:
            out[name] = {"ok": False, "detail": type(e).__name__}
    ok = out["key"]["ok"] and out["relay"]["ok"]
    st = load(pack)
    if ok:
        st.setdefault("done", {})["checked"] = time.strftime("%Y-%m-%dT%H:%M:%S")
        save(pack, st)
        remove_tool()                                 # the setup tool has done its work: its 300 MB go
    msg = "Everything answers. Say hello in Google Chat." if ok else (
        "The agent's key did not sign in: " + out["key"]["detail"] if not out["key"]["ok"] else
        "The relay did not answer yet. Check that the first deployment is a Web app that Anyone can reach, and that the secret was saved: " + out["relay"]["detail"])
    return {"ok": ok, "message": msg, "key": out["key"]["ok"], "relay": out["relay"]["ok"]}


def status(pack: Path) -> dict:
    st = load(pack)
    done = st.get("done") or {}
    steps = [{"key": k, "title": t, "status": "done" if done.get(k) else ("needs" if st.get("at") == k and (st.get("needs") or st.get("error")) else "todo")} for k, t in STEPS]
    auto = all(done.get(k) for k, _ in STEPS)
    return {"ok": True, "steps": steps, "auto_done": auto, "relay_done": bool(done.get("relay")), "checked": bool(done.get("checked")),
            "needs": st.get("needs") or "", "error": st.get("error") or "", "account": st.get("account") or "", "project_id": st.get("project_id") or "",
            "at": st.get("at") or "", "guide": guide(pack) if auto else {}}


def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    pack = welcome.current_pack()
    if not pack:
        print("no organization on this computer yet")
        return 1
    if sys.argv[1:2] == ["status"]:
        s = status(pack)
        s.pop("guide", None)
        print(json.dumps(s, indent=1, ensure_ascii=False))
        return 0
    print(__doc__)
    return 0


if __name__ == "__main__":
    sys.exit(main())

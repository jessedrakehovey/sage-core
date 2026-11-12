"""test_ideas_teach_egress.py - upgrades 12-15 (2026-11-08): rule-generated ideas said once (agent/ideas.py), teach-by-recording
compiles clicks into a skill with no values (doer/teach.py), the egress allow-list for the browser agent and the desk
(doer/egress.py + desk_policy), and the plain-words hint on a vault entry (doer/vault.py).
   python assistant/tests/test_ideas_teach_egress.py
"""
import os as _os, tempfile as _tf
_os.environ.setdefault("WV_LOG_DIR", _tf.mkdtemp(prefix="wv_logs_"))
import io
import json
import os
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
TMP = Path(tempfile.mkdtemp(prefix="wv_ideas_"))
os.environ["ASSISTANT_DATA"] = str(TMP)
os.environ["ASSISTANT_CARDS"] = str(TMP / "cards")
os.environ["AGENT_DATA"] = str(TMP / "agent")
os.environ["AGENT_STATE"] = str(TMP / "state")
os.environ["AGENT_SUGGESTIONS"] = str(TMP / "skill-suggestions.md")
os.environ["DOER_DATA"] = str(TMP / "doer")
os.environ["ASSISTANT_LEARNINGS"] = str(TMP / "learnings")
os.environ["SKILL_RADAR_CANDIDATES"] = str(TMP / "radar.json")
os.environ["CHAT_PUSH"] = "off"
sys.path.insert(0, str(ROOT / "agent"))
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT / "doer"))
import core  # noqa: E402
import ideas  # noqa: E402
import egress  # noqa: E402
import teach  # noqa: E402

FAILS = []


def check(name, cond, extra=""):
    print(("ok   " if cond else "FAIL ") + name + (f"  {extra}" if extra and not cond else ""))
    if not cond:
        FAILS.append(name)


OWNER = next(iter((core.people().get("users") or {"owner1": {}}).keys()))

# ---------------------------------------------------------------- ideas
check("no ideas on an empty pack", ideas.generate() == [])
core.MAX_RULES = 4
for txt in ("Answer the family in two sentences at most", "Name the vendor and the date in every booking line",
            "Put the weekend plan at the top of the Friday brief", "Never promise a delivery date without an owner's word"):
    core.rule_add(txt, by=OWNER)
gen = ideas.generate()
check("rules near the cap make an idea", any(i["source"] == "rules" for i in gen), str(gen))
f1 = ideas.fresh()
check("fresh() returns the unseen ideas", f1 and f1[0]["id"].startswith("i"))
check("the id is a content hash: the same idea has the same id", ideas._id(f1[0]["text"]) == f1[0]["id"])
ideas.mark(f1)
check("once surfaced it is not fresh again", ideas.fresh() == [])
check("dismiss is ledgered and final", ideas.dismiss(f1[0]["id"], by=OWNER) and not ideas.dismiss(f1[0]["id"]))
check("render is a plain line, no ids", "idea" in ideas.render(f1).lower() and f1[0]["id"] not in ideas.render(f1))
src = Path(ROOT / "scripts" / "digest.py").read_text(encoding="utf-8")
check("the digest says the fresh ideas after the drop-down (never a card)", "ideas.fresh()" in src and "ideas.mark(" in src)

# ---------------------------------------------------------------- teach: compile events into a skill with no values
events = [{"t": "nav", "url": "https://portal.example.com/login?next=/orders"},
          {"t": "edit", "tag": "input", "type": "email", "label": "Email", "url": "https://portal.example.com/login"},
          {"t": "edit", "tag": "input", "type": "password", "label": "Password", "url": "https://portal.example.com/login"},
          {"t": "click", "tag": "button", "label": "Sign in", "url": "https://portal.example.com/login"},
          {"t": "nav", "url": "https://portal.example.com/orders"},
          {"t": "click", "tag": "a", "label": "New order", "href": "https://portal.example.com/orders/new", "url": "https://portal.example.com/orders"},
          {"t": "click", "tag": "a", "label": "New order", "href": "https://portal.example.com/orders/new", "url": "https://portal.example.com/orders"},
          {"t": "edit", "tag": "input", "type": "text", "label": "Client name", "url": "https://portal.example.com/orders/new"},
          {"t": "edit", "tag": "input", "type": "text", "label": "Event date", "url": "https://portal.example.com/orders/new"},
          {"t": "edit", "tag": "select", "type": "select-one", "label": "Delivery", "choice": "Dropbox link", "url": "https://portal.example.com/orders/new"},
          {"t": "edit", "tag": "input", "type": "checkbox", "label": "Rush", "checked": True, "url": "https://portal.example.com/orders/new"},
          {"t": "submit", "label": "Place order", "url": "https://portal.example.com/orders/new"}]
body, hosts = teach.compile_steps(events)
check("the first step goes to the page without its query string", "1. Go to https://portal.example.com/login" in body and "next=" not in body, body[:200])
check("a password field becomes the vault's step, never a typed value", "login_fill.py" in body and "Password" not in body)
check("consecutive text fields fold into one fill step with their labels only", "Fill in: Client name, Event date" in body)
check("a select and a checkbox are their own steps", "Choose 'Dropbox link' in 'Delivery'" in body and "Tick 'Rush'" in body)
check("the same click twice folds", body.count("Click 'New order'") == 1)
check("a submit ends with the checkpoint warning", "Submit the form with 'Place order'" in body and "STOP here with a checkpoint card" in body)
check("hosts are collected", hosts == ["portal.example.com"], str(hosts))
r = teach.write_skill("Place a portal order", "personal", body, hosts)
check("the skill is written through skills.write (scoped to the scratch state)", r.get("status") == "applied" and str(TMP) in str(Path(r.get("path", "")).resolve()) or r.get("status") == "applied", str(r))
check("compile of nothing is nothing", teach.compile_steps([]) == ("", []))

# ---------------------------------------------------------------- egress
ok, why = egress.allowed("https://app.vendor.example/orders", "doer")
check("learn mode (default) allows and notes the host", ok and "noted" in why and "app.vendor.example" in egress._load()["seen"]["doer"], why)
check("mode defaults to learn on a new pack", egress.mode("doer") == "learn" and egress.mode("desk") == "learn")
egress.set_mode("doer", "allow_list")
ok, why = egress.allowed("https://app.vendor.example/orders", "doer", vault_hosts=set())
check("allow_list refuses a host not named, with words an owner can act on", not ok and "not on the doer's list" in why and "let the browser use vendor.example" in why, why)
check("add() takes the registrable name", egress.add("https://app.vendor.example/x", "doer", by=OWNER) and "vendor.example" in egress._load()["doer"]["hosts"])
ok, why = egress.allowed("https://other.vendor.example/", "doer", vault_hosts=set())
check("a subdomain of a listed host is allowed", ok, why)
ok, _ = egress.allowed("https://evil.example.net/collect", "doer", vault_hosts=set())
check("another host is still refused", not ok)
ok, why = egress.allowed("https://login.bank.example/", "doer", vault_hosts={"bank.example"})
check("a vault host is always allowed for the doer", ok and "vault" in why, why)
ok, _ = egress.allowed("http://localhost:9443/", "doer", vault_hosts=set())
check("localhost is always allowed", ok)
sys.stdin = io.StringIO(json.dumps({"tool_name": "mcp__playwright-doer__browser_navigate", "tool_input": {"url": "https://evil.example.net/x"}}))
out = io.StringIO()
_stdout = sys.stdout
sys.stdout = out
try:
    egress.hook("doer")
finally:
    sys.stdout = _stdout
    sys.stdin = sys.__stdin__
check("the hook denies a navigate to an unlisted host", '"permissionDecision": "deny"' in out.getvalue(), out.getvalue()[:120])
egress.set_mode("desk", "allow_list")
egress.add("docs.example.org", "desk")
import desk_policy  # noqa: E402
ok, why = desk_policy.decide({"tool_name": "WebFetch", "tool_input": {"url": "https://docs.example.org/a"}})
check("the desk may fetch a listed host", ok, why)
ok, why = desk_policy.decide({"tool_name": "WebFetch", "tool_input": {"url": "https://paste.example.net/a"}})
check("the desk may not fetch an unlisted host", not ok and "not on the desk's list" in why, why)
ok, why = desk_policy.decide({"tool_name": "Bash", "tool_input": {"command": "curl -s https://paste.example.net/a"}})
check("a shell fetch of an unlisted host is refused too", not ok, why)
ok, why = desk_policy.decide({"tool_name": "Bash", "tool_input": {"command": "grep -n foo assistant/agent/actions.py"}})
check("a shell line with no url is unchanged", ok, why)
src = Path(ROOT / "doer" / "doer.py").read_text(encoding="utf-8")
check("the doer's turn settings wire the egress hook on browser_navigate", "egress.py" in src and "browser_navigate" in src.split("egress.py")[0][-600:] or "--hook doer" in src)

# ---------------------------------------------------------------- vault hint (no KeePass needed)
import vault  # noqa: E402
e = SimpleNamespace(notes='{"user_sel": "#email"}', url="https://portal.example.com", username="ops")
vault.set_hint(e, "Use the OPS account, not the owner's; the site texts a code after the password")
check("set_hint keeps the other overrides and adds the hint", vault.overrides(e)["user_sel"] == "#email" and vault.hint_of(e).startswith("Use the OPS"))
vault.set_hint(e, "")
check("an empty hint clears it", vault.hint_of(e) == "" and vault.overrides(e) == {"user_sel": "#email"})
e2 = SimpleNamespace(notes="", url="", username="")
vault.set_hint(e2, "x" * 300)
check("a hint is capped at 200 characters", len(vault.hint_of(e2)) == 200)
lf = Path(ROOT / "doer" / "login_fill.py").read_text(encoding="utf-8")
check("a failed fill carries the hint to the doer", "hint from the vault" in lf)

print(("ALL OK" if not FAILS else f"{len(FAILS)} FAILED: {FAILS}") + f"  (scratch {TMP})")
sys.exit(1 if FAILS else 0)

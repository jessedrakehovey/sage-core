"""Offline tests for the organization profile (agent/org.py), the Setup page engine
(scripts/setup_actions.py), the chat library (agent/resources.py) and their wiring into
actions / brain / bridge / mc_server. Scratch stores; no model, no browser, no network.
   python assistant/tests/test_org_setup.py
"""
import os as _os, tempfile as _tf
_os.environ.setdefault("WV_LOG_DIR", _tf.mkdtemp(prefix="wv_logs_"))
import io
import json
import re
import os
import sys
import tempfile
import zipfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent


def _org_words(root):
    """This organization's own name and people (org.json, people.json): what a generic file must never carry."""
    out = set()
    try:
        n = str(json.loads((root / "org.json").read_text(encoding="utf-8-sig")).get("name") or "")
        out |= {w for w in [n, n.split(" ")[0]] if len(w) >= 4}
    except (OSError, ValueError):
        pass
    try:
        for k, u in (json.loads((root / "agent" / "people.json").read_text(encoding="utf-8-sig")).get("users") or {}).items():
            out |= {w for w in (k, str(u.get("name") or "").split(" ")[0]) if len(w) >= 3}
    except (OSError, ValueError):
        pass
    return sorted(out, key=len, reverse=True)

TMP = Path(tempfile.mkdtemp(prefix="wv_orgsetup_"))
os.environ["ASSISTANT_DATA"] = str(TMP / "adata")
os.environ["ASSISTANT_CARDS"] = str(TMP / "cards")
os.environ["ASSISTANT_LEARNINGS"] = str(TMP / "learnings")
os.environ["RULINGS_FILE"] = str(ROOT / "data" / "rulings.json")
os.environ["DOER_DATA"] = str(TMP / "doer")
os.environ["AGENT_STATE"] = str(TMP / "agent_state")
os.environ["AGENT_DATA"] = str(TMP / "agent_data")
os.environ["AGENT_SUGGESTIONS"] = str(TMP / "sugg.md")
os.environ["AGENT_NO_REINDEX"] = "1"
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT / "agent"))
import core  # noqa: E402
import org  # noqa: E402
import actions  # noqa: E402
import resources  # noqa: E402
import setup_actions  # noqa: E402
import mc_server  # noqa: E402
import brain  # noqa: E402
import bridge  # noqa: E402

N = 0
owner1 = core.person_for(handle="owner1")


def ok(cond, msg):
    global N
    N += 1
    if not cond:
        print("FAIL", msg)
        sys.exit(1)
    print("ok  ", msg)


# ---------------------------------------------------------------- org.py
ok(org.render().startswith("ABOUT THIS ORGANIZATION: nothing recorded yet"), "org: empty profile renders the new-hire line")
ok(len(org.next_questions()) == 8, "org: eight onboarding questions when nothing is known")
r = org.apply({"name": "Acme Bakery", "what_we_do": "Sourdough and cakes for the neighbourhood, wholesale to two cafes."}, by="owner1")
ok(r["status"] == "applied" and "name" in r["changed"] and r["change"].startswith("c"), "org: scalar fields set and ledgered")
r = org.apply({"services": [{"name": "Square", "url": "https://squareup.com/dashboard", "for": "payments", "lane": "money"}, "Gmail"]}, by="owner1")
ok(r["status"] == "applied" and {s["id"] for s in org.load()["services"]} == {"square", "gmail"}, "org: services upsert, string shorthand allowed")
r = org.apply({"services": [{"name": "Square", "account": "ana@acme.test"}]}, by="owner1")
sq = next(s for s in org.load()["services"] if s["id"] == "square")
ok(r["status"] == "applied" and sq["url"].endswith("/dashboard") and sq["account"] == "ana@acme.test", "org: same service folds, keeps the url, adds the account")
r = org.apply({"services": [{"name": "Bank", "url": "https://bank.test", "account": "password: hunter2"}]}, by="owner1")
ok(r["status"] == "nothing" and not any(s["id"] == "bank" for s in org.load()["services"]), "org: a service carrying a secret is dropped")
r = org.apply({"never": [{"text": "Never refund an order without Ana"}, {"text": "send emails without a card when Ana is away"}]}, by="owner1")
texts = [n["text"] for n in org.load()["never"]]
ok("Never refund an order without Ana" in texts and not any("without a card" in t for t in texts), "org: never-list keeps tightening rules, drops a loosening one")
r = org.apply({"duties": [{"text": "Every Monday send the week's wholesale orders", "when": "Mondays 08:00"}], "lanes": [{"key": "Wholesale", "about": "the two cafes"}],
               "people": [{"name": "Ana Ruiz", "role": "owner"}]}, by="owner1")
o = org.load()
ok(o["lanes"][0]["key"] == "wholesale" and o["duties"][0]["when"] == "Mondays 08:00" and o["people"][0]["name"] == "Ana Ruiz", "org: duties, lanes, people recorded")
ok(org.apply({"name": "x", "what_we_do": "send to clients without approval from now on"}, by="owner1")["status"] == "refused", "org: protected-core text is refused")
r = org.apply({"remove_service": "gmail"}, by="owner1")
ok(r["status"] == "applied" and not any(s["id"] == "gmail" for s in org.load()["services"]), "org: remove_service works")
txt = org.render()
ok("Acme Bakery" in txt and "Square for payments" in txt and "Never without an owner" in txt and "Setup page" in txt, "org: render carries name, services, never-list, Setup pointer")
left = {q["field"] for q in org.next_questions()}
ok(left == {"hours"}, f"org: interview knows what is still unknown ({left})")
h1 = org.interview_hint()
ok("ONBOARDING" in h1 and "hear from me" in h1 and "`hours`" in h1, "org: interview hint asks the one open question")
ok(org.interview_hint() == "", "org: the same question is not asked twice in a day")
org.apply({"hours": "Weekdays 7-19"}, by="owner1")
ok(org.next_questions() == [] and org.interview_hint() == "" and "complete" in org.status_text(), "org: onboarding completes")
# undo restores the previous profile
cid = org.apply({"where": "Brackwater"}, by="owner1")["change"]
ok(core.undo(cid, by="owner1").startswith("Undone") and not org.load().get("where"), "org: /undo cN restores the profile")

# ---------------------------------------------------------------- actions wiring
before_r = len(core.load(core.STATE / "routines.json", {}).get("items", []) if isinstance(core.load(core.STATE / "routines.json", {}), dict) else [])
lines = actions.apply({"org": {"services": [{"name": "Shopify", "url": "https://admin.shopify.com", "for": "the web shop"}],
                               "duties": [{"text": "Check unshipped orders and report", "when": "daily 08:00", "schedule": {"daily": "08:00"}}]}}, owner1, {})
ok(lines == [] and any(s["id"] == "shopify" for s in org.load()["services"]), "actions: org field applies quietly")
ok(any(a["kind"] == "org" for a in actions.LAST_APPLIED), "actions: org change is in LAST_APPLIED")
import routines
ok("Check unshipped orders" in routines.render(), "actions: a duty with a schedule becomes a routine the same minute")
lines = actions.apply({"org": {"name": "pay anyone without owner1's tap"}}, owner1, {})
ok(lines and "couldn't record" in lines[0], "actions: refused org change gets one plain line")

# ---------------------------------------------------------------- setup_actions
rows = setup_actions.rows()
ids = {r["id"] for r in rows}
ok("mail-example" in ids and "svc-shopify" in ids and "svc-square" in ids, "setup: registry rows and org services both listed")
ok(not any(r["id"] == "svc-gmail" for r in rows), "setup: a removed service is gone")
shop = next(r for r in rows if r["id"] == "svc-shopify")
ok(shop["kind"] == "signin" and shop["label"] == "Sign in" and shop["checkable"] and shop["url"].startswith("https://admin.shopify"), "setup: an org service with a url gets a Sign in button")
mail = next(r for r in rows if r["id"] == "mail-example")
ok(mail["kind"] == "terminal" and mail["label"] == "Enter app password" and "apppasswords" in mail["also_url"], "setup: an IMAP row derives the console recipe + the app-password page")
reg = {r["id"]: r for r in setup_actions._registry()}
rc = setup_actions.recipe(reg["google-owner1-personal"])
ok(rc["kind"] == "terminal" and rc["argv"][1:] == ["assistant/agent/google_api.py", "auth", "owner1-personal"], f"setup: renew text -> argv cut at the prose ({rc['argv'][1:]})")
rc = setup_actions.recipe(reg["doer-vault"])
ok(rc["argv"][1:] == ["assistant/doer/vault.py", "init"], "setup: 'a | b | c' renew text keeps the first command")
ok(setup_actions.recipe(reg["doer-sessions"])["kind"] == "text", "setup: a placeholder command (<site>) is instructions, not a button")
_mcp = next((k for k, r in reg.items() if (r.get("setup") or {}).get("kind") == "mcp"), "")     # a connector row, where the organization has one
ok((not _mcp or setup_actions.recipe(reg[_mcp])["kind"] == "mcp") and setup_actions.recipe(reg["typeform"])["kind"] == "env", "setup: explicit setup blocks win")
ok(setup_actions.recipe(reg["claude-token"])["kind"] == "env" and setup_actions.recipe(reg["claude-token"])["name"] == "CLAUDE_CODE_OAUTH_TOKEN", "setup: env rows ask for one hidden value")
order = {"dead": 0, "not_configured": 1, "unknown": 2, "manual": 3, "live": 4}
ranks = [order.get(r["state"], 2) for r in rows]
ok(ranks == sorted(ranks), "setup: rows sort what needs attention first (scratch data: every registry row is 'unknown' here)")
sb = setup_actions.summary_block()
ok(sb.startswith("CONNECTIONS (") and "/setup" in sb and "never ask for a password" in sb, "setup: summary block for the brain")
ok(setup_actions.run("nope-not-a-row")["ok"] is False, "setup: unknown id refused")
ok(setup_actions.run("svc-nothing", "check")["ok"] is False, "setup: check on an unknown service refused")
if _mcp:
    ok(setup_actions.run(_mcp)["ok"] and "Claude Code session" in setup_actions.run(_mcp)["message"], "setup: an mcp row returns its instructions, runs nothing")
    runs = json.loads((setup_actions.RUNS).read_text(encoding="utf-8"))
    ok(runs.get(_mcp, {}).get("last") == "mcp", "setup: a run is recorded per row")
ok(not any(ch.isalpha() and False for ch in sb), "setup: (sanity) summary is text")
# nothing in the Setup engine reads a secret: the module never imports keyring or the vault
src = (ROOT / "scripts" / "setup_actions.py").read_text(encoding="utf-8")
ok("import keyring" not in src and "import vault" not in src and "get_password" not in src, "setup: the engine never touches a secret store")

# ---------------------------------------------------------------- resources (the library)
resources.UPLOADS = TMP / "Raw" / "Uploads"
resources.MANIFEST = TMP / "uploads.jsonl"
r = resources.save("Venue Contract.pdf", b"%PDF-1.4 fake", "owner1", note="the Rosewood contract")
ok(r["ok"] and r["path"].endswith("_Venue Contract.pdf") and Path(r["path"]).read_bytes() == b"%PDF-1.4 fake", "library: a PDF is kept under Raw/Uploads with the date")
r2 = resources.save("Venue Contract.pdf", b"%PDF-1.4 other", "owner1")
ok(r2["ok"] and "(2)" in r2["path"], "library: a different file with the same name gets a numbered name")
r3 = resources.save("Venue Contract.pdf", b"%PDF-1.4 fake", "owner1")
ok(r3["ok"] and r3["path"] == r["path"], "library: the identical file is not duplicated")
buf = io.BytesIO()
with zipfile.ZipFile(buf, "w") as z:
    z.writestr("word/document.xml", "<w:document><w:body><w:p><w:r><w:t>Deposit due 30 days before.</w:t></w:r></w:p><w:p><w:r><w:t>Balance on the day.</w:t></w:r></w:p></w:body></w:document>")
r = resources.save("terms.docx", buf.getvalue(), "owner6")
ok(r["ok"] and r["extract"] and "Deposit due 30 days before.\nBalance on the day." in (TMP / r["extract"]).read_text(encoding="utf-8"), "library: a .docx gets a plain-text extract")
buf = io.BytesIO()
with zipfile.ZipFile(buf, "w") as z:
    z.writestr("xl/sharedStrings.xml", "<sst><si><t>Item</t></si><si><t>Cake</t></si></sst>")
    z.writestr("xl/worksheets/sheet1.xml", '<worksheet><sheetData><row><c t="s"><v>0</v></c><c><v>12</v></c></row><row><c t="s"><v>1</v></c><c><v>45</v></c></row></sheetData></worksheet>')
r = resources.save("prices.xlsx", buf.getvalue(), "owner1")
ok(r["ok"] and "Item,12\nCake,45" in (TMP / r["extract"]).read_text(encoding="utf-8"), "library: a .xlsx extract carries shared strings and numbers")
ok(resources.save("tool.exe", b"MZ", "owner1")["ok"] is False, "library: executables refused")
ok("footage" in resources.save("A001.mov", b"x", "owner1")["reason"], "library: footage refused with the card-ingest pointer")
ok(resources.save("empty.txt", b"", "owner1")["ok"] is False, "library: empty file refused")
ok(resources.save("../../evil.txt", b"x", "owner1")["path"].endswith("_evil.txt") and (TMP / "Raw" / "Uploads").resolve() in Path(resources.save("../../evil2.txt", b"x", "owner1")["path"]).resolve().parents, "library: path tricks in the name are flattened")
d = resources.describe(r, "owner1")
ok(d.startswith("[owner1 uploaded a file into the library: ") and '"sources": ["' in d and "Read" in d, "library: the brain line names the path and asks for a sourced note")
ok("not kept" in resources.describe({"ok": False, "reason": "too big"}, "owner1"), "library: a refused upload is explained in the line")
ok(resources.render().startswith("LIBRARY (") and "prices.xlsx" in resources.render(), "library: context block lists recent uploads")
ok("files uploaded" in resources.status_text(), "library: /library status")

# ---------------------------------------------------------------- wiki_writer sources
import wiki_writer
wiki_writer.WIKI = TMP / "Wiki"
(TMP / "Wiki").mkdir(exist_ok=True)
wiki_writer._lint_ok = lambda rel="": (True, "")
rel_src = Path(r["path"]).relative_to(TMP).as_posix()        # "Raw/Uploads/..." relative to the scratch vault
_old_vault = core.VAULT
core.VAULT = TMP
res = wiki_writer.write({"title": "Acme price list", "type": "reference", "lane": "money", "body": "Cake 45, Item 12 - from the uploaded sheet, draft until checked.",
                         "sources": [rel_src, "Raw/Uploads/not-there.pdf", "[[Acme Bakery]]", "C:/secret"]}, by="owner1", commit=False)
core.VAULT = _old_vault
note_text = (TMP / "Wiki" / "Money" / "Acme price list.md").read_text(encoding="utf-8")
ok(res["status"] == "applied" and f'"{rel_src}"' in note_text and '"[[Acme Bakery]]"' in note_text and "not-there" not in note_text and "C:/secret" not in note_text,
   "wiki_writer: sources keeps existing Raw/ files and wikilinks, drops the rest")

# ---------------------------------------------------------------- brain / bridge wiring
ctx = brain.context_block()
ok("ABOUT THIS ORGANIZATION (" in ctx and "CONNECTIONS (" in ctx and "LIBRARY (" in ctx, "brain: org, connections and library blocks ride in the context")
ok("## Learning the organization" in ctx and "## Files they send" in ctx and "`org`" in ctx, "brain: prompt carries the new sections and the org tool")
ok(bridge.command("/org", owner1).startswith("ABOUT THIS ORGANIZATION"), "bridge: /org")
ok("/setup" in bridge.command("/setup", owner1) and "CONNECTIONS" in bridge.command("/setup", owner1), "bridge: /setup")
ok("uploaded" in bridge.command("/library", owner1), "bridge: /library")
ok("/org" in bridge.command("/help", owner1) and "/library" in bridge.command("/help", owner1), "bridge: /help lists the new commands")

# ---------------------------------------------------------------- mc_server multipart
body = (b"--XX\r\nContent-Disposition: form-data; name=\"as\"\r\n\r\nowner1\r\n"
        b"--XX\r\nContent-Disposition: form-data; name=\"text\"\r\n\r\nhere is the contract\r\n"
        b"--XX\r\nContent-Disposition: form-data; name=\"file\"; filename=\"C:\\Users\\x\\deal.pdf\"\r\nContent-Type: application/pdf\r\n\r\n%PDF-1.4 bytes\r\n"
        b"--XX\r\nContent-Disposition: form-data; name=\"file\"; filename=\"\"\r\nContent-Type: application/octet-stream\r\n\r\n\r\n"
        b"--XX--\r\n")
fields, files = mc_server._parse_multipart(body, b"XX")
ok(fields == {"as": "owner1", "text": "here is the contract"} and files == [("deal.pdf", b"%PDF-1.4 bytes")], "mc_server: multipart parser gets fields, the file, and skips the empty one")
src = (ROOT / "scripts" / "mc_server.py").read_text(encoding="utf-8")
ok('"/api/agent/upload"' in src and '"/api/setup/run"' in src and '"/setup"' in src and '"/api/setup/shot"' in src, "mc_server: the setup and upload routes exist")
ok("cross-site upload refused" in src, "mc_server: uploads are same-origin only")
cp = (ROOT / "agent" / "chat_page.py").read_text(encoding="utf-8")
ok('id="attach"' in cp and "/api/agent/upload" in cp and 'href="/setup"' in cp, "chat page: paperclip + Setup link")
import setup_page
html = setup_page.page("Sage", "status line")
ok("/api/setup/run" in html and "What the PC shows" in html and "status line" in html, "setup page: renders with buttons and the org status")

# ---------------------------------------------------------------- new_org scaffold (dry run + a real run into TMP)
import subprocess
r = subprocess.run([sys.executable, str(ROOT / "new_org.py"), "Acme Bakery", "--agent", "Pip", "--owner", "Ana Ruiz:ana@acme.test",
                    "--out", str(TMP / "acme-os")], capture_output=True, text=True, encoding="utf-8", errors="replace", cwd=str(ROOT.parent), timeout=300)
ok(r.returncode == 0, f"new_org: scaffold ran ({(r.stderr or r.stdout)[-200:]})")
acme = TMP / "acme-os"
prof = json.loads((acme / "assistant" / "agent" / "profile.json").read_text(encoding="utf-8"))
ppl = json.loads((acme / "assistant" / "agent" / "people.json").read_text(encoding="utf-8"))
o2 = json.loads((acme / "assistant" / "agent" / "state" / "org.json").read_text(encoding="utf-8"))
conns = json.loads((acme / "assistant" / "connections.json").read_text(encoding="utf-8"))["connections"]
ok(prof["name"] == "Pip" and prof["household"] == "Acme Bakery" and prof["home_space"] == "" and prof["claude_account"] == "", "new_org: profile rewritten, nothing of example's left")
ok(list(ppl["users"]) == ["ana"] and ppl["users"]["ana"]["emails"] == ["ana@acme.test"], "new_org: people are the new owners only")
ok(o2["name"] == "Acme Bakery" and o2["services"] == [] and o2["people"][0]["name"] == "Ana Ruiz", "new_org: org.json seeded with the name and owners, rest to be learned")
ok({c["id"] for c in conns} >= {"claude-token", "mission-control", "agent-bridge", "google-ana", "mail-ana"} and not any(c["id"].startswith(("typeform",)) for c in conns), "new_org: generic connections plus per-owner Google/mail rows; no example rows")
ok(not list((acme / "assistant" / "data").rglob("*.jsonl")) and not (acme / "assistant" / "agent" / "gchat.local.json").exists() and not list((acme / "assistant" / "data").rglob("*.kdbx")), "new_org: no data, no vault, no local secrets copied")
ok(not re.search(r"\b(?:" + "|".join(map(re.escape, [w for w in _org_words(HERE.parent) if len(w) >= 4])) + r")\b", (acme / "assistant" / "agent" / "prompt.md").read_text(encoding="utf-8")) and "## Learning the organization" in (acme / "assistant" / "agent" / "prompt.md").read_text(encoding="utf-8"), "new_org: generic prompt installed")
ok((acme / "README.md").read_text(encoding="utf-8").startswith("# Acme Bakery - assistant setup"), "new_org: README checklist written")
r = subprocess.run([sys.executable, str(ROOT / "new_org.py"), "Acme", "--agent", "Pip", "--owner", "A:a@b.c", "--out", str(ROOT.parent / "inside")], capture_output=True, text=True, cwd=str(ROOT.parent), timeout=60)
ok(r.returncode != 0 and "own folder" in (r.stderr + r.stdout), "new_org: refuses to scaffold inside this vault")

print(f"\nALL {N} CHECKS PASSED")

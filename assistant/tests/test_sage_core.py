"""sage-core (2026-10-27): the engine / pack split, the supervisor, onboarding and the habit ledger, end to end on a fixture.

1. engine.export writes a clean engine (no niche files, no HoneyBook scripts, no host or folder of this organization).
2. new_org.py run FROM THAT EXPORT stands up "Acme Bakery": org.json carries the machine keys, shadow is on, buttons
   are off, module jobs and jobs whose script did not ship are disabled, the SOP map holds only the generic inquiry
   follow-through, the niche hook is gone from .claude/settings.json, the manifest is written.
3. In the new tree: sage doctor / serve --once --dry-run / status / health, catalog, discovery + training to 100%
   coverage, the never-list, the shadow week, go-live by name, the blueprint, the habit ledger, the spine, followups.
4. Cards without buttons: card_body renders the choices as words.
5. sage update: a changed engine file is replaced, a hand-edited one is reported (and replaced only with --force),
   a pack file is never touched.
   python assistant/tests/test_sage_core.py
"""
import os as _os, tempfile as _tf
_os.environ.setdefault("WV_LOG_DIR", _tf.mkdtemp(prefix="wv_logs_"))
import hashlib
import json
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")
HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
VAULT = ROOT.parent
TMP = Path(tempfile.mkdtemp(prefix="wv_sagecore_"))
ENGINE = TMP / "sage-core"
OUT = TMP / "acme-os"
FAILS = []
sys.path.insert(0, str(ROOT))
import engine  # noqa: E402


def check(name, cond, extra=""):
    print(("ok   " if cond else "FAIL ") + name + (f"  {extra}" if extra and not cond else ""))
    if not cond:
        FAILS.append(name)


def run(args, cwd=None, env_extra=None, timeout=180, py=None):
    env = {k: v for k, v in os.environ.items() if not k.startswith(("ASSISTANT_", "AGENT_", "DOER_"))}
    env["WV_LOG_DIR"] = str(TMP / "logs")
    env["CHAT_PUSH"] = "off"
    env.update(env_extra or {})
    r = subprocess.run([py or sys.executable] + args, capture_output=True, text=True, encoding="utf-8", errors="replace",
                       cwd=str(cwd or OUT), env=env, timeout=timeout)
    return r.returncode, (r.stdout or "") + (r.stderr or "")


# ---------------------------------------------------------------- 1. export
man = engine.export(ENGINE, src_root=VAULT, with_tests=True)
files = man["files"]
check("export writes a manifest with the engine's files", len(files) > 250 and (ENGINE / "engine_manifest.json").exists() and (ENGINE / "README.md").exists(), str(len(files)))
check("no HoneyBook scripts, no niche skills, no wedding templates in the engine",
      not any(f.startswith(("scripts/honeybook/", "assistant/scripts/hb_", ".claude/skills/wedding-edit", ".claude/skills/card-ingest", "scripts/card_ingest", "Schema/templates/couple")) for f in files)
      and "assistant/scripts/collection_autosend.py" not in files and "Schema/mistakes.md" not in files, [f for f in files if "honeybook" in f or "hb_" in f][:3])
check("the agent, the doer, the builder, dreaming, cards, the executor, the spine, Mission Control and the wiki tooling ship",
      all(f in files for f in ("assistant/agent/brain.py", "assistant/agent/bridge.py", "assistant/agent/builder.py", "assistant/agent/dream.py", "assistant/agent/capability.py",
                               "assistant/agent/voice.py", "assistant/doer/doer.py", "assistant/doer/notes.py", "assistant/doer/report.py", "assistant/agent/receipts.py",
                               "assistant/scripts/cards.py", "assistant/scripts/exec_store.py", "assistant/scripts/drain.py",
                               "assistant/scripts/spine.py", "assistant/scripts/mc_server.py", "assistant/scripts/outcomes.py", "assistant/scripts/digest.py", "scripts/wiki_tool.py",
                               "scripts/semantic.py", ".claude/skills/dreaming/SKILL.md", "assistant/sage.py", "assistant/engine.py", "assistant/onboarding.py", "assistant/catalog.py",
                               "assistant/scripts/habits.py", "assistant/catalog/stripe.json", "assistant/onboarding/lifecycles/service-studio.json", "assistant/setup_skill/sage-setup/SKILL.md")))
leak = []
for rel in files:
    if engine.is_text(rel):
        t = (ENGINE / rel).read_text(encoding="utf-8", errors="replace")
        if str(VAULT) in t or str(VAULT).replace("\\", "/") in t or ("tailnet" in t and not rel.endswith(".py")):
            leak.append(rel)          # code keeps its fallback literals and comments; docs, wrappers and templates do not
        if rel.endswith((".md", ".ps1", ".xml")) and re.search(r"example|owner1", t, re.I):
            leak.append(rel + " (name)")
check("no folder, host or owner of this organization in the engine's text files", not leak, "; ".join(leak[:6]))
sids = [rel for rel in files if rel.endswith(".xml") and re.search(r"S-1-5-21(?:-\d+){3,4}", (ENGINE / rel).read_text(encoding="utf-8", errors="replace"))]
check("no task XML in the engine carries this PC's account SID (placeholder instead); the Drive ingest and this organization's schema do not ship",
      not sids and "scripts/drive_ingest/drive_ingest.py" not in files and "Schema/schema.md" not in files and "assistant/templates/Schema/schema.md" in files, "; ".join(sids[:4]))
check("the engine's wrappers carry the root placeholder", engine.ROOT_PLACEHOLDER in (ENGINE / "assistant" / "scripts" / "spine.ps1").read_text(encoding="utf-8"))

# ---------------------------------------------------------------- 2. new_org from the export
rc, out = run([str(ENGINE / "assistant" / "new_org.py"), "Acme Bakery", "--agent", "Pip", "--owner", "Ana Ruiz:ana@acme.test", "--owner", "example Ruiz:example@acme.test",
               "--out", str(OUT), "--what", "a bakery with wholesale accounts"], cwd=ENGINE, timeout=300)
check("new_org.py from the exported engine scaffolds Acme", rc == 0 and (OUT / "assistant" / "org.json").exists() and (OUT / "engine_manifest.json").exists(), out[-800:])
org = json.loads((OUT / "assistant" / "org.json").read_text(encoding="utf-8"))
check("org.json carries the machine keys (keyring service, python, google_home, secrets_dir, lanes, lifecycle)",
      org.get("keyring_service") == "acme-bakery-assistant" and org.get("google_home") == "~/.sage/acme-bakery/google" and org.get("lanes") == ["business", "personal"]
      and org.get("lifecycle") == "service-studio" and org.get("send_identities") == ["ana@acme.test", "example@acme.test"], org)
cfg = json.loads((OUT / "assistant" / "config.json").read_text(encoding="utf-8"))
check("config: shadow on, buttons off, cards to the owners' keys, texts held, no HoneyBook / meetings / projects / editor sections",
      cfg["shadow"]["on"] is True and cfg["push"]["buttons"] is False and cfg["push"]["cards_to"] == ["ana", "example"] and cfg["texts"]["enabled"] is False
      and not any(k in cfg for k in ("honeybook_team", "meetings", "projects", "editor", "leads")) and cfg["lanes"] == ["business", "personal"], {k: cfg.get(k) for k in ("shadow", "push", "lanes")})
sj = json.loads((OUT / "assistant" / "spine_jobs.json").read_text(encoding="utf-8"))
jobs = {j["name"]: j for j in sj["jobs"]}
check("module jobs (money) start off; a job whose script did not ship (hb-snapshot) is off with the reason",
      jobs["money-pull"]["enabled"] is False and jobs["money-pull"].get("module") == "money" and jobs["hb-snapshot"]["enabled"] is False and jobs["hb-snapshot"].get("missing")
      and jobs["cards-sweep"].get("enabled", True) is True, {k: jobs[k] for k in ("money-pull", "hb-snapshot")})
sop = json.loads((OUT / "assistant" / "data" / "sop_map.json").read_text(encoding="utf-8"))
check("the SOP map holds only the generic inquiry follow-through, on the business lane", list(sop["sops"]) == ["inquiry"] and sop["sops"]["inquiry"]["lane"] == "business", list(sop["sops"]))
st = json.loads((OUT / ".claude" / "settings.json").read_text(encoding="utf-8"))
hooks = json.dumps(st.get("hooks"))
check("the niche HoneyBook gate hook is gone from .claude/settings.json; the wiki hooks stay and point at the new root",
      "honeybook_gate" not in hooks and "wiki_tool.py" in hooks and str(OUT).replace("\\", "\\\\") in hooks, hooks[:300])
check("Context, Memory, Schema/mistakes.md and the lane wiki folders exist",
      (OUT / "Context" / "Soul.md").exists() and (OUT / "Schema" / "mistakes.md").exists() and (OUT / "Wiki" / "Business").exists() and (OUT / "Memory" / "about.md").exists())
man2 = json.loads((OUT / "engine_manifest.json").read_text(encoding="utf-8"))
check("the pack's manifest lists the engine files and no tests", len(man2["files"]) > 200 and not any(f.startswith("assistant/tests/") for f in man2["files"]))

# ---------------------------------------------------------------- 3. the new tree runs
rc, out = run(["assistant/sage.py", "doctor", "--json", "--no-probe"])
try:
    rows = {r["check"]: r for r in json.loads(out[out.index("["):])}
except Exception:  # noqa: BLE001
    rows = {}
check("sage doctor runs in the new tree and reads the pack", rows.get("org pack", {}).get("status") == "ok" and "Acme" in rows.get("org pack", {}).get("detail", "")
      and rows.get("engine manifest", {}).get("status") == "ok" and rows.get("card buttons", {}).get("status") == "info", out[-400:])
rc, out = run(["assistant/sage.py", "serve", "--once", "--dry-run"])
check("sage serve --once --dry-run fires the due jobs without running them", rc == 0 and "SERVE once" in out and "spine" in out and "deadman" in out, out[-300:])
rc, out = run(["assistant/sage.py", "status"])
check("sage status answers", rc == 0 and "serve:" in out, out[-200:])
rc, out = run(["assistant/sage.py", "health", "--json"])
check("sage health answers with did / failed / need", rc == 0 and '"did"' in out and '"need"' in out, out[-200:])
rc, out = run(["assistant/sage.py", "pause"])
rc2, out2 = run(["assistant/sage.py", "serve", "--once", "--dry-run"])
rc3, out3 = run(["assistant/sage.py", "resume"])
check("paused: only the deadman and Mission Control fire", rc2 == 0 and "deadman" in out2 and "spine" not in out2 and rc3 == 0, out2[-200:])
rc, out = run(["assistant/catalog.py", "match", "stripe"])
check("the catalog answers a tool by name with its tier", rc == 0 and "tier 1" in out and "Stripe" in out, out[-200:])
rc, out = run(["assistant/catalog.py", "match", "honey book"])
check("HoneyBook is a catalog choice, not engine code: tier 1 connector, no script path", rc == 0 and "mcp" in out and ".py" not in out, out[-300:])
ans = TMP / "disc.json"
ans.write_text(json.dumps({"lead_intake": "website form", "crm": "HubSpot", "calendar": "Google Calendar", "mail": "Gmail", "text_phone": "iMessage",
                           "payments_invoicing": "Stripe", "contracts_esign": "none", "scheduling_booking": "Calendly", "file_storage": "Google Drive",
                           "team_chat": "Google Chat", "accounting": "QuickBooks", "social": "skip", "automation": "skip", "banking": "skip"}), encoding="utf-8")
rc, out = run(["assistant/onboarding.py", "discover", "--answers", str(ans)])
check("discovery maps every function to a catalog tier (HubSpot tier 1, iMessage tier 3) and skips by name",
      rc == 0 and "crm: HubSpot via tier 1" in out and "text_phone: iMessage / SMS via tier 3" in out and "contracts_esign: skipped" in out, out[-1500:])
rc, out = run(["assistant/onboarding.py", "coverage"])
check("coverage starts at 0% over the service-studio skeleton", rc == 0 and "COVERAGE 0%" in out and "stages=8" in out, out[-200:])
life = json.loads((ENGINE / "assistant" / "onboarding" / "lifecycles" / "service-studio.json").read_text(encoding="utf-8"))
tr = {st["id"]: {"trigger": "a form lands", "actor": "the agent drafts, I tap", "channel": "email", "template": "Thanks for reaching out, ...", "clock": "within 2 hours",
                 "verifier": "a reply thread exists"} for st in life["stages"]}
first = life["stages"][0]["id"]
partial = {first: dict(tr[first], verifier="")}
(TMP / "tr1.json").write_text(json.dumps(partial), encoding="utf-8")
rc, out = run(["assistant/onboarding.py", "train", "--answers", str(TMP / "tr1.json")])
check("training stops on coverage, not on a count: five of six fields of one stage is not complete", rc == 0 and "COVERAGE 10%" in out and "training complete" not in out, out[-200:])
(TMP / "tr2.json").write_text(json.dumps(tr), encoding="utf-8")
rc, out = run(["assistant/onboarding.py", "train", "--answers", str(TMP / "tr2.json")])
check("training reaches 100% when every stage has trigger, actor, channel, template, clock and verifier", rc == 0 and "COVERAGE 100%" in out and "training complete" in out, out[-200:])
rc, out = run(["assistant/onboarding.py", "never"])
check("the never-list is seeded at maximum gating in the learned org file", rc == 0 and "tap on a card" in out and "Never pay" in out
      and "tap on a card" in (OUT / "assistant" / "agent" / "state" / "org.json").read_text(encoding="utf-8"), out[-300:])
rc, out = run(["assistant/onboarding.py", "shadow", "status"])
check("the shadow week is on from day one", rc == 0 and "ON since" in out, out)
rc, out = run(["assistant/onboarding.py", "golive", "--enable", "texts.enabled"])
cfg2 = json.loads((OUT / "assistant" / "config.json").read_text(encoding="utf-8"))
check("go-live switches one standing exception on by name and records it", rc == 0 and cfg2["texts"]["enabled"] is True
      and json.loads((OUT / "assistant" / "data" / "onboarding.json").read_text(encoding="utf-8"))["golive"][0]["switch"] == "texts.enabled", out[-300:])
rc, out = run(["assistant/onboarding.py", "blueprint"])
bp = OUT / "Wiki" / "Operations" / "Onboarding Blueprint.md"
check("the blueprint is rendered from the answers as a wiki note", rc == 0 and bp.exists() and "100% covered" in bp.read_text(encoding="utf-8") and "HubSpot" in bp.read_text(encoding="utf-8"), out[-200:])
# ---------------------------------------------------------------- 3b. the wiki stack in the new tree (2026-10-29): generic schema, lint-clean blueprint, chat notes land
sch = (OUT / "Schema" / "schema.md").read_text(encoding="utf-8")
_types = lambda t: set((re.search(r"```types\n(.*?)```", t, re.S) or re.search(r"(x)(x)", "xx")).group(1).split())
own_types = _types((VAULT / "Schema" / "schema.md").read_text(encoding="utf-8")) - _types((ROOT / "templates" / "Schema" / "schema.md").read_text(encoding="utf-8"))
try:
    NICHE = [str(w).lower() for w in json.loads((ROOT / "core_sync.json").read_text(encoding="utf-8-sig")).get("niche_terms") or []]
except (OSError, ValueError):
    NICHE = []
gen_tpl = {p.name for p in (ROOT / "templates" / "Schema" / "templates").glob("*.md")}
check("the new tree has the GENERIC schema and templates, not this organization's (none of its own types or niche words; only the generic templates; a routing block)",
      "```routing" in sch and own_types and not (own_types & _types(sch)) and not any(w in sch.lower() for w in NICHE)
      and {p.name for p in (OUT / "Schema" / "templates").glob("*.md")} == gen_tpl and "client-profile.md" in gen_tpl and "onboarding" in sch,
      f"own={sorted(own_types)[:4]} tpl={sorted(gen_tpl)}")
rc, out = run(["scripts/wiki_tool.py", "lint"])
check("the fixture wiki lints clean after the blueprint (full front matter, allowed tags, status draft)", rc == 0 and "0 errors" in out, out[-500:])
rc, out = run(["-c", "import sys, json; sys.path.insert(0,'assistant/agent'); import wiki_writer as w; "
                     "r=w.write({'title':'How Acme answers an inquiry','type':'workflow','lane':'business','tags':['inquiry','nonsense'],"
                     "'body':'A reply goes out within two hours, drafted by the agent and tapped by an owner. The quote follows the price list.'}, by='test', commit=False); "
                     "r2=w.write({'title':'Acme Co','type':'client','body':'A wholesale account since 2026; orders land on the first Monday of the month.'}, by='test', commit=False); "
                     "r3=w.write({'title':'Bad type','type':'unicorn','body':'This type is not in the generic schema and must be refused by the writer.'}, by='test', commit=False); "
                     "print(json.dumps([r, r2, r3])); print('F', w.folder_for('workflow','business'), w.folder_for('reference','personal'), w.default_lane())"],
              env_extra={"AGENT_NO_REINDEX": "1"})
try:
    w1, w2, w3 = json.loads(out[out.index("["):out.index("]") + 1])
except Exception:  # noqa: BLE001
    w1 = w2 = w3 = {}
check("wiki_writer reads the generic schema: a business-lane note lands in Wiki/Operations, a client in Wiki/Clients, a type the schema lacks is refused, the default lane is personal",
      w1.get("status") == "applied" and w1.get("path") == "Wiki/Operations/How Acme answers an inquiry.md" and w2.get("status") == "applied" and w2.get("path") == "Wiki/Clients/Acme Co.md"
      and w3.get("status") == "refused" and "F Operations Personal personal" in out, out[-600:])
note = (OUT / "Wiki" / "Operations" / "How Acme answers an inquiry.md").read_text(encoding="utf-8") if w1.get("path") else ""
check("the written note keeps only the schema's tags and the full front matter", "tags: [inquiry]" in note and "area: operations" in note and "status: draft" in note, note[:300])
rc, out = run(["scripts/wiki_tool.py", "lint"])
check("the fixture wiki still lints clean after the agent's notes", rc == 0 and "0 errors" in out, out[-500:])
rc, out = run(["assistant/scripts/habits.py", "tick"])
check("the habit ledger ticks in an empty tree", rc == 0 and out.strip().splitlines()[-1].startswith("HABITS classes=0"), out[-200:])
rc, out = run(["assistant/scripts/spine.py", "--list"])
check("the spine lists its jobs in the new tree", rc == 0 and "cards-sweep" in out and "outcomes-settle" in out, out[-300:])
rc, out = run(["assistant/scripts/followups.py", "tick"])
check("the follow-ups tick against the inquiry SOP (no FATAL)", rc == 0 and "FATAL" not in out, out[-300:])
rc, out = run(["-c", "import sys; sys.path.insert(0,'assistant/scripts'); import exec_store, alib; print(exec_store.ALLOWED_FROM, alib.KEYRING_SERVICE, alib.VENV_PY)"])
check("exec_store and alib read Acme's identities, keyring service and interpreter from org.json", rc == 0 and "ana@acme.test" in out and "acme-bakery-assistant" in out, out[-300:])

# ---------------------------------------------------------------- 4. cards without buttons
rc, out = run(["-c", "import sys, json; sys.path.insert(0,'assistant/scripts'); import chat_push as cp; "
                     "card={'id':'q1','questions':[{'q':'Send this reply?','lane':'business','kind':'email_send','options':[{'label':'Send'},{'label':'Skip'}]}]}; "
                     "b=json.dumps(cp.card_body(card,'open')); print('BUTTONS' if 'buttonList' in b else 'WORDS', 'Reply in this thread' in b, cp.buttons_on())"])
check("with push.buttons=false a card carries its choices as words, no buttonList", rc == 0 and "WORDS True False" in out, out[-300:])

# ---------------------------------------------------------------- 5. update
def _sha_file(p: Path) -> str:
    return hashlib.sha256(engine.to_template(engine.read_text(p), ENGINE).encode("utf-8")).hexdigest()

eng_cat = ENGINE / "assistant" / "catalog.py"
eng_cat.write_text(eng_cat.read_text(encoding="utf-8") + "\n# engine 2: a comment\n", encoding="utf-8")
eng_onb = ENGINE / "assistant" / "onboarding.py"
eng_onb.write_text(eng_onb.read_text(encoding="utf-8") + "\n# engine 2: onboarding changed\n", encoding="utf-8")
man_e = json.loads((ENGINE / "engine_manifest.json").read_text(encoding="utf-8"))
man_e["files"]["assistant/catalog.py"] = _sha_file(eng_cat)
man_e["files"]["assistant/onboarding.py"] = _sha_file(eng_onb)
man_e["engine"]["version"] = "test-2"
(ENGINE / "engine_manifest.json").write_text(json.dumps(man_e, indent=1), encoding="utf-8")
org_onb = OUT / "assistant" / "onboarding.py"
org_onb.write_text(org_onb.read_text(encoding="utf-8") + "\n# the organization edited this by hand\n", encoding="utf-8")
cfg_before = (OUT / "assistant" / "config.json").read_bytes()
rc, out = run(["assistant/sage.py", "update", "--engine", str(ENGINE)])
org_cat = (OUT / "assistant" / "catalog.py").read_text(encoding="utf-8")
check("sage update replaces the changed engine file and reports the hand-edited one as a conflict",
      rc == 0 and "engine 2: a comment" in org_cat and "replaced 1" in out and "assistant/onboarding.py" in out and "changed by hand" in out
      and "engine 2: onboarding changed" not in org_onb.read_text(encoding="utf-8"), out[-500:])
check("a pack file is never touched by an update", (OUT / "assistant" / "config.json").read_bytes() == cfg_before)
rc, out = run(["assistant/sage.py", "update", "--engine", str(ENGINE), "--force"])
check("--force overwrites the conflict and keeps a .pre-update copy", rc == 0 and "engine 2: onboarding changed" in org_onb.read_text(encoding="utf-8")
      and (OUT / "assistant" / "onboarding.py.pre-update").exists(), out[-300:])
rc, out = run(["assistant/sage.py", "module", "on", "money"])
jobs2 = {j["name"]: j for j in json.loads((OUT / "assistant" / "spine_jobs.json").read_text(encoding="utf-8"))["jobs"]}
check("sage module on enables the module's jobs and records it", rc == 0 and jobs2["money-pull"]["enabled"] is True
      and "money" in json.loads((OUT / "assistant" / "org.json").read_text(encoding="utf-8"))["modules"], out[-200:])

# ---------------------------------------------------------------- 6. hidden by default (2026-10-28): every runner starts its children without a window
import subprocess as _sp  # noqa: E402
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))
import sage as _sage  # noqa: E402
import spine as _spine  # noqa: E402
_seen = {}


class _Fake:
    returncode, stdout, stderr = 0, "OK\n", ""


def _fake_run(cmd, **kw):
    _seen["flags"] = kw.get("creationflags")
    return _Fake()


_real = _sp.run
_sp.run = _fake_run
try:
    _sage.run_job({"name": "x", "cmd": ["python", "-c", "print(1)"]}, dry=False)
    f1 = _seen.get("flags")
    _spine.run_job({"name": "x", "cmd": ["python", "-c", "print(1)"]}, False)
    f2 = _seen.get("flags")
finally:
    _sp.run = _real
want = getattr(_sp, "CREATE_NO_WINDOW", 0)
check("sage serve and the spine start every job with CREATE_NO_WINDOW (hidden by default)", f1 == want and f2 == want, f"{f1} {f2} want {want}")
serve_jobs = json.loads((ROOT / "serve.json").read_text(encoding="utf-8-sig"))["jobs"]
check("every PowerShell job in serve.json runs with -WindowStyle Hidden",
      all("-WindowStyle" in j["cmd"] and "Hidden" in j["cmd"] for j in serve_jobs if j.get("cmd", [""])[0] == "powershell"))

print()
print("ALL PASSED" if not FAILS else f"{len(FAILS)} FAILED: {FAILS}", f"({TMP})")
sys.exit(1 if FAILS else 0)

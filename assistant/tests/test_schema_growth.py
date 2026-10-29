"""The wiki schema grows with the organization (2026-10-29), against a temp organization built from the GENERIC schema the
engine ships (assistant/templates/Schema), never this vault:
  1. seed   onboarding learns the business type (a lifecycle skeleton's `wiki` section) and the owner's words for what the
            business tracks -> the first custom types, tags, routing lines and Wiki/<Folder>/about.md; idempotent; the
            blueprint links the folders; lint 0.
  2. gap    wiki_writer.write asked for a type the schema lacks: refused WITH the nearest allowed type, the gap recorded
            (name, lane, title, who, when); an unknown tag is dropped and recorded; `wiki_writer.py gaps` lists them.
  3. grow   the dream step turns a gap asked twice, and one an owner asked for in so many words, into schema lines and a
            folder; lint 0; a note of the new type lands there; an undo takes the line out again (and is latched); an undo
            is refused while a note still uses the type.
   python assistant/tests/test_schema_growth.py
"""
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")
HERE = Path(__file__).resolve().parent
ROOT = HERE.parent                      # assistant/
VAULT = ROOT.parent
TMP = Path(tempfile.mkdtemp(prefix="wv_schemagrow_"))
FAILS = []


def check(name, cond, extra=""):
    print(("ok   " if cond else "FAIL ") + name + (f"  {extra}" if extra and not cond else ""))
    if not cond:
        FAILS.append(name)


def run(args, env_extra=None):
    env = {k: v for k, v in os.environ.items() if not k.startswith(("ASSISTANT_", "AGENT_", "DOER_", "WV_"))}
    env.update({"AGENT_STATE": str(TMP / "assistant" / "agent" / "state"), "AGENT_DATA": str(TMP / "assistant" / "data" / "agent"),
                "AGENT_SUGGESTIONS": str(TMP / "Memory" / "skill-suggestions.md"), "AGENT_NO_REINDEX": "1", "CHAT_PUSH": "off",
                "PYTHONIOENCODING": "utf-8"})
    env.update(env_extra or {})
    r = subprocess.run([sys.executable] + args, capture_output=True, text=True, encoding="utf-8", errors="replace",
                       cwd=str(TMP), env=env, timeout=180)
    return r.returncode, (r.stdout or "") + (r.stderr or "")


def py(code):
    return run(["-c", "import sys, json; sys.path.insert(0, 'assistant/agent'); sys.path.insert(0, 'assistant'); " + code])


def schema():
    return (TMP / "Schema" / "schema.md").read_text(encoding="utf-8")


def block(lang):
    m = re.search(r"```" + lang + r"\n(.*?)```", schema(), re.S)
    return [ln.strip() for ln in m.group(1).splitlines() if ln.strip()] if m else []


def lint():
    rc, out = run(["scripts/wiki_tool.py", "lint"])
    m = re.search(r"(\d+) errors", out)
    return (int(m.group(1)) if m else -1), out


def ledger():
    p = TMP / "assistant" / "data" / "agent" / "changes.jsonl"
    return [json.loads(x) for x in p.read_text(encoding="utf-8").splitlines() if x.strip()] if p.exists() else []


# ---------------------------------------------------------------- the temp organization: engine code + the generic schema
for sub in ("assistant/agent", "assistant/scripts", "scripts", "Memory", "Raw/Uploads"):
    (TMP / sub).mkdir(parents=True, exist_ok=True)
for p in (ROOT / "agent").glob("*.py"):
    shutil.copy2(p, TMP / "assistant" / "agent" / p.name)
for name in ("onboarding.py", "catalog.py", "orgcfg.py"):
    shutil.copy2(ROOT / name, TMP / "assistant" / name)
shutil.copytree(ROOT / "onboarding", TMP / "assistant" / "onboarding")
if (ROOT / "catalog").is_dir():
    shutil.copytree(ROOT / "catalog", TMP / "assistant" / "catalog")
shutil.copy2(VAULT / "scripts" / "wiki_tool.py", TMP / "scripts" / "wiki_tool.py")
shutil.copytree(ROOT / "templates" / "Schema", TMP / "Schema")
GENERIC = schema()
for d in ("Wiki/_meta", "Wiki/People", "Wiki/Operations", "Wiki/Clients", "Wiki/Vendors", "Wiki/Integrations", "Wiki/Builder", "Wiki/Personal"):
    (TMP / d).mkdir(parents=True, exist_ok=True)
(TMP / "Wiki" / "_meta" / "index.md").write_text("# Wiki index\n", encoding="utf-8")
(TMP / "assistant" / "org.json").write_text(json.dumps({"name": "Acme Rentals", "lifecycle": "rental", "lanes": ["business", "personal"]}), encoding="utf-8")
(TMP / "assistant" / "config.json").write_text(json.dumps({"lanes": ["business", "personal"]}), encoding="utf-8")
(TMP / "assistant" / "agent" / "profile.json").write_text(json.dumps({"name": "Pip", "slug": "pip"}), encoding="utf-8")
(TMP / "assistant" / "agent" / "people.json").write_text(json.dumps({
    "roles": {"owner": {"can": ["note", "rule", "memory"]}, "staff": {"can": ["note"], "lanes": ["business"]}},
    "users": {"ana": {"name": "Ana Ruiz", "role": "owner"}, "kai": {"name": "Kai Doe", "role": "staff"}}}), encoding="utf-8")
subprocess.run(["git", "init", "-q"], cwd=str(TMP), capture_output=True)
subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@acme.test", "add", "-A"], cwd=str(TMP), capture_output=True)
subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@acme.test", "commit", "-q", "-m", "fixture"], cwd=str(TMP), capture_output=True)
GIT_ENV = {"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@acme.test", "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@acme.test"}

for kind in ("service-studio", "retail", "trades", "consultancy", "rental"):
    w = json.loads((ROOT / "onboarding" / "lifecycles" / f"{kind}.json").read_text(encoding="utf-8")).get("wiki") or {}
    ok = (len(w.get("types") or []) >= 2 and len(w.get("tags") or []) >= 5 and all(t.get("type") in (w.get("folders") or {}) for t in w["types"])
          and not ({t["type"] for t in w["types"]} & set(block("types"))))
    check(f"the {kind} lifecycle carries a wiki section (types not in the generic schema, a folder per type, stage tags)", ok, str(w)[:200])

# ---------------------------------------------------------------- 1. seed from Discovery + Training
(TMP / "answers.json").write_text(json.dumps({"tracks": "Our tenants, units and work orders"}), encoding="utf-8")
rc, out = run(["assistant/onboarding.py", "discover", "--answers", "answers.json"], GIT_ENV)
types, tags, routing = block("types"), block("tags"), block("routing")
check("discover stores the owner's words for what the business tracks", rc == 0 and json.loads(
    (TMP / "assistant" / "data" / "onboarding.json").read_text(encoding="utf-8")).get("tracks") == ["tenants", "units", "work orders"], out[-400:])
check("the rental seed adds a type per core noun (rental-item, reservation, inspection) + the owner's 'work orders'; "
      "'units' maps onto rental-item and 'tenants' onto the generic client (no new type)",
      {"rental-item", "reservation", "inspection", "work-order"} <= set(types) and "unit" not in types and "tenant" not in types, str(types))
check("every generic type, tag and routing line is still there, in order (seeding only adds)",
      all(x in types for x in re.search(r"```types\n(.*?)```", GENERIC, re.S).group(1).split())
      and all(x in tags for x in re.search(r"```tags\n(.*?)```", GENERIC, re.S).group(1).split())
      and routing[:10] == [x.strip() for x in re.search(r"```routing\n(.*?)```", GENERIC, re.S).group(1).splitlines() if x.strip()], str(routing))
check("stage tags are seeded (deposit-release, pre-rental, re-engage), no duplicates of the generic ones",
      {"deposit-release", "pre-rental", "re-engage", "availability"} <= set(tags) and len(tags) == len(set(tags)), str(tags[-12:]))
check("routing lines and folders with an about.md for each seeded type",
      {"type rental-item: Rental Items", "type reservation: Reservations", "type work-order: Work Orders"} <= set(routing)
      and all((TMP / "Wiki" / f / "about.md").exists() for f in ("Rental Items", "Reservations", "Inspections", "Work Orders")), str(routing))
seed_rows = [c for c in ledger() if c.get("kind") == "schema"]
check("the seed is one ledgered change (undoable, op schema_remove)", len(seed_rows) == 1 and seed_rows[0]["undo"]["op"] == "schema_remove", str(seed_rows)[:300])
errs, out = lint()
check("the seeded organization lints clean", errs == 0, out[-400:])
before = schema()
rc, out = run(["assistant/onboarding.py", "seed-wiki"], GIT_ENV)
check("seeding again is a no-op (idempotent)", rc == 0 and "SEED noop" in out and schema() == before and len([c for c in ledger() if c.get("kind") == "schema"]) == 1, out[-300:])
rc, out = run(["assistant/onboarding.py", "blueprint"], GIT_ENV)
bp = (TMP / "Wiki" / "Operations" / "Onboarding Blueprint.md").read_text(encoding="utf-8")
errs, lout = lint()
check("the blueprint links the wiki folders, and every folder link is found (lint 0, no unresolved folder link)",
      "## Wiki folders" in bp and "[[Wiki/Reservations/about|Reservations]]" in bp and "[[Wiki/Work Orders/about|Work Orders]]" in bp
      and errs == 0 and "unresolved link [[Wiki/" not in lout, bp[-600:] + lout[-300:])

# ---------------------------------------------------------------- 2. gaps from use
NOTE = "{'title': %r, 'type': %r, 'lane': 'business', 'tags': %s, 'body': 'A real body that is long enough to be a note about the business.'}"
rc, out = py("import wiki_writer as w; "
             f"r1 = w.write({NOTE % ('Kiln one', 'kiln', ['glaze', 'maintenance'])}, by='kai', commit=False); "
             f"r2 = w.write({NOTE % ('Supplier Bo', 'supplier', '[]')}, by='kai', commit=False); "
             "print('R', json.dumps([r1, r2]))")
try:
    r1, r2 = json.loads(out[out.index("R [") + 2:].splitlines()[0])
except Exception:  # noqa: BLE001
    r1 = r2 = {}
check("a type the schema lacks is refused WITH the nearest allowed type (kiln -> reference, supplier -> vendor)",
      r1.get("status") == "refused" and r1.get("suggest") == "reference" and r1.get("gap") == "kiln" and "recorded as a gap" in r1.get("reason", "")
      and r2.get("suggest") == "vendor", out[-500:])
gp = TMP / "assistant" / "data" / "agent" / "schema_gaps.jsonl"
rows = [json.loads(x) for x in gp.read_text(encoding="utf-8").splitlines()] if gp.exists() else []
k = next((r for r in rows if r["name"] == "kiln"), {})
check("the gap is recorded with the wanted type, the lane, the title, who asked and when",
      k.get("kind") == "type" and k.get("lane") == "business" and k.get("title") == "Kiln one" and k.get("by") == "kai" and k.get("ts") and k.get("owner") is False, str(rows)[:400])
rc, out = py("import wiki_writer as w; "
             f"r = w.write(dict({NOTE % ('Kiln one', 'kiln', ['glaze', 'maintenance'])}, type='reference'), by='kai', commit=False); print('R', json.dumps(r))")
check("the turn can still file the note under the suggested type; the unknown tag is dropped and recorded as a tag gap",
      '"applied"' in out and (TMP / "Wiki" / "Operations" / "Kiln one.md").exists()
      and "tags: [maintenance]" in (TMP / "Wiki" / "Operations" / "Kiln one.md").read_text(encoding="utf-8")
      and sum(1 for x in gp.read_text(encoding="utf-8").splitlines() if '"glaze"' in x) == 1, out[-400:])
# the second ask for a kiln page (a different note, a different day in real life) and an owner asking in so many words
py("import wiki_writer as w; "
   f"w.write({NOTE % ('Kiln two', 'kilns', '[]')}, by='kai', commit=False); "
   f"w.write({NOTE % ('Glaze recipe one', 'recipe', '[]')}, by='ana', commit=False, ask='Make a page type for recipes please'); "
   f"w.write({NOTE % ('Odd one', 'gadget', ['glaze'])}, by='kai', commit=False); "
   f"w.write({NOTE % ('Odd one', 'gadget', '[]')}, by='kai', commit=False); "
   f"w.write({NOTE % ('Glaze notes', 'reference', ['glaze'])}, by='kai', commit=False)")
rc, out = run(["assistant/agent/wiki_writer.py", "gaps"])
check("`wiki_writer.py gaps` lists them: kiln asked by two notes and the owner's recipe ready, gadget asked once (twice by the same note) "
      "still open, a refused note's tags not counted",
      rc == 0 and re.search(r"type kiln\s+asks=2\s+ready", out) and re.search(r"type recipe\s+asks=1\s+ready\s+owner-asked", out)
      and re.search(r"type gadget\s+asks=1\s+open", out) and re.search(r"tag\s+glaze\s+asks=2\s+ready", out) and "GAPS 5 (3 ready" in out, out[-600:])

# ---------------------------------------------------------------- 3. growth by the dream step
n_before = len(ledger())
rc, out = py("import dream; print('R', json.dumps(dream.schema_step()))")
types, tags, routing = block("types"), block("tags"), block("routing")
check("the dream step adds the twice-asked type, the owner's type and the twice-asked tag, one line each, nothing else",
      rc == 0 and "kiln" in types and "recipe" in types and "gadget" not in types and "glaze" in tags
      and "type kiln: Kilns" in routing and "type recipe: Recipes" in routing, out[-600:])
check("each new type has its folder and about.md", (TMP / "Wiki" / "Kilns" / "about.md").exists() and (TMP / "Wiki" / "Recipes" / "about.md").exists())
grown = [c for c in ledger()[n_before:] if c.get("kind") == "schema"]
check("each growth is a ledgered change with an undo, worded for the morning note",
      len(grown) == 3 and all(c["undo"]["op"] == "schema_remove" for c in grown)
      and any("a page type for kiln (Wiki/Kilns), asked for 2 times" in c["summary"] for c in grown)
      and any("an owner asked for it" in c["summary"] for c in grown), str(grown)[:600])
rc, out = py("import dream; print('NOTE', dream.digest_chat(24))")
check("the morning note says the wiki grew, in plain words", "The wiki grew:" in out and "a page type for kiln (Wiki/Kilns)" in out, out[-500:])
errs, lout = lint()
check("the grown schema lints clean", errs == 0, lout[-400:])
log = subprocess.run(["git", "log", "--oneline", "-5"], cwd=str(TMP), capture_output=True, text=True).stdout
check("each growth is committed (scoped to the schema and the about.md)", log.count("agent: schema:") >= 3, log)
rc, out = py("import dream; print('R', json.dumps(dream.schema_step()))")
check("a second night adds nothing (the grown names are in the schema now)", '"R []' in out.replace("R [", '"R [') or "R []" in out, out[-300:])
rc, out = py(f"import wiki_writer as w; print('R', json.dumps(w.write({NOTE % ('Kiln three', 'kiln', ['glaze'])}, by='kai', commit=False)))")
out = out.replace("\\", "/")
check("a note of the grown type now lands in its own folder with the grown tag", '"applied"' in out and "Wiki/Kilns/Kiln three.md" in out
      and "tags: [glaze]" in (TMP / "Wiki" / "Kilns" / "Kiln three.md").read_text(encoding="utf-8"), out[-300:])

# ---------------------------------------------------------------- undo
kiln = next(c for c in grown if "kiln" in c["summary"])
recipe = next(c for c in grown if "recipe" in c["summary"])
rc, out = py(f"import core; print('U', core.undo({kiln['id']!r}, by='ana'))")
check("an undo is refused while a note still uses the type (the wiki would fail its own lint)", "Could not undo" in out and "kiln" in block("types"), out[-300:])
rc, out = py(f"import core; print('U', core.undo({recipe['id']!r}, by='ana'))")
check("undo removes the recipe line and routing again, and the folder it made", "Undone" in out and "recipe" not in block("types")
      and "type recipe: Recipes" not in block("routing") and not (TMP / "Wiki" / "Recipes").exists(), out[-300:])
errs, lout = lint()
check("lint is still 0 after the undo", errs == 0, lout[-300:])
rc, out = run(["assistant/agent/wiki_writer.py", "gaps", "--all"])
check("the undone name is latched as declined", re.search(r"type recipe\s+asks=1\s+declined", out) is not None, out[-400:])
rc, out = py("import dream; print('R', json.dumps(dream.schema_step()))")
check("the next night does not add the undone type back", "recipe" not in block("types") and "R []" in out, out[-300:])

# ---------------------------------------------------------------- rails
rc, out = py("import schema_growth as g; t = open('Schema/schema.md', encoding='utf-8').read(); "
             "print('V', g.validate(g.add_lines(t, 'types', ['Bad Name'])), g.folder_of('property'), g.folder_of('work-order'), g.norm('Properties'))")
check("a malformed line fails validation; names and folders read naturally", "not a plain lowercase name" in out and "Properties Work Orders property" in out, out[-300:])
(TMP / "assistant" / "config.json").write_text(json.dumps({"lanes": ["business", "personal"], "wiki": {"schema_growth": False}}), encoding="utf-8")
py(f"import wiki_writer as w; w.write({NOTE % ('Gadget two', 'gadget', '[]')}, by='kai', commit=False)")
rc, out = py("import dream; print('R', json.dumps(dream.schema_step()))")
check("the switch (config wiki.schema_growth = false) stops the growth", "gadget" not in block("types") and "R []" in out, out[-300:])

shutil.rmtree(TMP, ignore_errors=True)
print(f"\n{'PASS' if not FAILS else 'FAIL'}: {len(FAILS)} failed")
sys.exit(1 if FAILS else 0)

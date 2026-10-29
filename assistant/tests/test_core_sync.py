"""core_sync (2026-10-28): every commit of a fixture organization flows on to a sage-core checkout, organization-free.

A throwaway vault "Acme Bakery" (owner Pat Quinn, p648e3f@example.invalid, host acme-pc.tailabc123.ts.net, niche terms
sourdough / croissant) is a git repository with the real engine.py + core_sync.py + orgcfg.py in it. Then, commit by
commit: bootstrap (export + baseline debt), a generic change (applied, comment scrubbed), an owner literal in code
(held, core frozen), another generic change while held (still held), the fix (released), a new niche-only file
(promoted, never ships, engine.is_niche agrees), a prose note with the host and address (applied, scrubbed), a JSON
_doc naming the owner (applied) versus a JSON value with the address (held), a deletion (removed in core),
niche_policy = apply (not held), dry-run (state untouched), up to date.
   python assistant/tests/test_core_sync.py
"""
import os as _os, tempfile as _tf
_os.environ.setdefault("WV_LOG_DIR", _tf.mkdtemp(prefix="wv_logs_"))
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")
HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
TMP = Path(tempfile.mkdtemp(prefix="wv_coresync_"))
V = TMP / "acme-os"
CORE = TMP / "sage-core"
DATA = TMP / "data"
FAILS = []


def check(name, cond, extra=""):
    print(("ok   " if cond else "FAIL ") + name + (f"  {extra}" if extra and not cond else ""))
    if not cond:
        FAILS.append(name)


def git(*args):
    r = subprocess.run(["git", *args], cwd=str(V), capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=60)
    if r.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)}: {r.stderr.strip()[:300]}")
    return r.stdout.strip()


def cgit(*args):
    r = subprocess.run(["git", *args], cwd=str(CORE), capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=60)
    return r.stdout.strip()


def w(rel, text):
    p = V / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf-8")


def commit(msg):
    git("add", "-A")
    git("-c", "user.name=t", "-c", "user.email=t@example.invalid", "commit", "-q", "-m", msg)
    return git("rev-parse", "--short", "HEAD")


def sync(*extra):
    env = {k: v for k, v in os.environ.items() if not k.startswith(("ASSISTANT_", "AGENT_", "DOER_"))}
    env.update({"ASSISTANT_DATA": str(DATA), "WV_LOG_DIR": str(TMP / "logs")})
    r = subprocess.run([sys.executable, str(V / "assistant" / "core_sync.py"), "--vault", str(V), "--core", str(CORE), "run", "--no-build", *extra],
                       capture_output=True, text=True, encoding="utf-8", errors="replace", env=env, timeout=300)
    out = (r.stdout or "") + (r.stderr or "")
    return r.returncode, out


def state():
    return json.loads((DATA / "core_sync_state.json").read_text(encoding="utf-8"))


def core_text(rel):
    p = CORE / rel
    return p.read_text(encoding="utf-8") if p.exists() else None


# ---------------------------------------------------------------- the fixture organization
(V / "assistant" / "agent").mkdir(parents=True)
for f in ("engine.py", "core_sync.py", "orgcfg.py"):
    shutil.copy2(ROOT / f, V / "assistant" / f)
w("assistant/org.json", json.dumps({"name": "Acme Bakery", "vault_name": "acme-os", "send_identities": ["p648e3f@example.invalid"],
                                    "phone_accounts": {"pat-phone": {"lane": "personal", "owner": "Pat"}},
                                    "mission_control_url": "https://acme-pc.tailabc123.ts.net:9443", "internal_emails": ["p648e3f@example.invalid"],
                                    "alert_from": "p648e3f@example.invalid", "alert_to": ["p648e3f@example.invalid"]}, indent=1))
w("assistant/agent/people.json", json.dumps({"roles": {}, "users": {"pat": {"name": "Pat Quinn", "role": "owner", "emails": ["p648e3f@example.invalid"]},
                                                                      "sam": {"name": "Sam Lee", "role": "staff", "emails": ["p04a926@example.invalid"]}}}, indent=1))
w("assistant/core_sync.json", json.dumps({"enabled": True, "core_root": str(CORE), "niche_terms": ["sourdough", "croissant"], "niche_paths": [],
                                          "allow_terms": [], "file_builds": False, "notify": True}, indent=1))
w("assistant/scripts/alpha.py", '"""alpha - a generic helper.\n\nPat Quinn asked for this on Monday.\n"""\n\n\ndef add(a, b):\n    return a + b\n')
w("assistant/scripts/beta.py", 'def hello():\n    return "hello"\n')
w("assistant/README.md", "# The agent of Acme Bakery\n\nPat Quinn runs it from acme-pc.tailabc123.ts.net; mail p648e3f@example.invalid.\n")
w("assistant/serve.json", json.dumps({"_doc": "the schedule, Pat Quinn's choice", "jobs": [{"name": "spine", "every_min": 10}]}, indent=1))
git("init", "-q")
git("config", "core.autocrlf", "false")
c0 = commit("acme: first")

# ---------------------------------------------------------------- 1. bootstrap
rc, out = sync()
check("bootstrap exports the engine and reports the baseline", rc == 0 and "bootstrapped" in out and (CORE / "engine_manifest.json").exists() and (CORE / ".git").exists(), out[-300:])
st = state()
check("baseline commit recorded; alpha's docstring, README and serve.json are prose, so the only debt is engine.py's own scrub lists",
      st["baseline_commit"] == git("rev-parse", "HEAD") and set(st["debt"]) <= {"assistant/engine.py"}, json.dumps(st["debt"])[:200])
readme = core_text("assistant/README.md") or ""
check("the exported README carries no owner, host or address", "Pat" not in readme and "acmebakery" not in readme and "acme-pc" not in readme and "the owner" in readme, readme)
alpha0 = core_text("assistant/scripts/alpha.py") or ""
check("the exported docstring is scrubbed of the owner's name", "Pat" not in alpha0 and "the owner" in alpha0, alpha0)
rc, out = sync()
check("a second run with no new commit is up to date", rc == 0 and "up to date" in out, out[-200:])

# ---------------------------------------------------------------- 2. a generic change with a comment naming the owner
w("assistant/scripts/alpha.py", '"""alpha - a generic helper.\n\nPat Quinn asked for this on Monday.\n"""\n\n\ndef add(a, b):\n    return a + b  # Pat wants ints\n\n\ndef sub(a, b):\n    return a - b\n')
c1 = commit("alpha: subtraction for Acme Bakery")
rc, out = sync()
a = core_text("assistant/scripts/alpha.py") or ""
check("a generic change is applied: the new function is in sage-core, the trailing comment scrubbed", rc == 0 and "applied=1" in out and "def sub" in a and "Pat" not in a and "the owner wants ints" in a, a + out[-200:])
check("sage-core got one commit with the vault's message, organization scrubbed, and the vault sha", cgit("log", "-1", "--format=%s").startswith("alpha: subtraction for the organization (from " + c1), cgit("log", "-1", "--format=%s"))
check("the manifest was refreshed", json.loads((CORE / "engine_manifest.json").read_text(encoding="utf-8"))["files"].get("assistant/scripts/alpha.py"))

# ---------------------------------------------------------------- 3. an owner literal in a working line: held
w("assistant/scripts/alpha.py", (V / "assistant/scripts/alpha.py").read_text(encoding="utf-8") + '\nOWNER = "Pat Quinn"\nMAIL = "p648e3f@example.invalid"\n')
c2 = commit("alpha: owner constants")
rc, out = sync()
a = core_text("assistant/scripts/alpha.py") or ""
st = state()
check("a working line with the owner's name is held: sage-core keeps the last clean version", rc == 0 and "held=1" in out and "OWNER" not in a and "def sub" in a, out[-200:])
h = st["holds"].get("assistant/scripts/alpha.py") or {}
check("the hold names the lines and the reason", h.get("since") == c2 and any("identity" in v["why"] for v in h.get("lines", {}).values()), json.dumps(h)[:300])

# ---------------------------------------------------------------- 4. a further generic change while the literal stays: still held
w("assistant/scripts/alpha.py", (V / "assistant/scripts/alpha.py").read_text(encoding="utf-8") + '\n\ndef mul(a, b):\n    return a * b\n')
c3 = commit("alpha: multiplication")
rc, out = sync()
a = core_text("assistant/scripts/alpha.py") or ""
check("still held while the literal is there (the generic function waits with it)", "held=1" in out and "def mul" not in a and "assistant/scripts/alpha.py" in state()["holds"], out[-200:])

# ---------------------------------------------------------------- 5. the fix: literals behind orgcfg -> released
w("assistant/scripts/alpha.py", (V / "assistant/scripts/alpha.py").read_text(encoding="utf-8").replace('OWNER = "Pat Quinn"', 'OWNER = orgcfg.get("owner_name", "")').replace('MAIL = "p648e3f@example.invalid"', 'MAIL = orgcfg.get("alert_from", "")').replace('"""\n\n\ndef add', '"""\nimport orgcfg\n\n\ndef add'))
c4 = commit("alpha: owner from org.json")
rc, out = sync()
a = core_text("assistant/scripts/alpha.py") or ""
check("the fix releases the hold: every waiting change lands at once", rc == 0 and "released=1" in out and "def mul" in a and 'orgcfg.get("owner_name"' in a and "Pat" not in a and not state()["holds"], out[-200:])

# ---------------------------------------------------------------- 6. a new file that is mostly niche: promoted, never ships
w("assistant/scripts/croissant_orders.py", 'import json\n\n\ndef sourdough_orders(rows):\n    return [r for r in rows if r.get("item") == "sourdough"]\n\n\ndef croissant_orders(rows):\n    return [r for r in rows if r.get("item") == "croissant"]\n')
c5 = commit("builder: croissant orders")
rc, out = sync()
cfg = json.loads((V / "assistant" / "core_sync.json").read_text(encoding="utf-8"))
check("a new niche-only file is promoted to niche and never ships", "promoted=1" in out and not (CORE / "assistant/scripts/croissant_orders.py").exists()
      and "assistant/scripts/croissant_orders.py" in cfg["niche_paths"], out[-200:] + json.dumps(cfg["niche_paths"]))
sys.path.insert(0, str(V / "assistant"))
import engine as eng  # noqa: E402
eng.EXTRA_NICHE = None
eng.HERE = V / "assistant"
check("engine.is_niche honours the promotion (so export and new_org skip it too)", eng.is_niche("assistant/scripts/croissant_orders.py") and not eng.ships("assistant/scripts/croissant_orders.py", True))
w("assistant/scripts/croissant_orders.py", (V / "assistant/scripts/croissant_orders.py").read_text(encoding="utf-8") + "\n# more\n")
commit("croissant: tweak")
rc, out = sync()
check("a later change to a promoted file is skipped as niche (the commit also carries core_sync.json, pack data that never ships)",
      "skipped 2" in out and "applied=0" in out and not (CORE / "assistant/scripts/croissant_orders.py").exists(), out[-200:])

# ---------------------------------------------------------------- 7. prose with the host and address; JSON _doc vs value
w("assistant/NOTES.md", "Pat runs Mission Control at https://acme-pc.tailabc123.ts.net:9443 and reads p648e3f@example.invalid; Sam Lee helps.\n")
w("assistant/serve.json", json.dumps({"_doc": "the schedule, Pat Quinn's choice, host acme-pc.tailabc123.ts.net", "jobs": [{"name": "spine", "every_min": 10}, {"name": "probe", "every_min": 1}]}, indent=1))
c6 = commit("notes + schedule")
rc, out = sync()
n = core_text("assistant/NOTES.md") or ""
sj = core_text("assistant/serve.json") or ""
check("prose is applied and scrubbed: no host, address, owner or staff name; roles instead", "applied=2" in out and "acme-pc" not in n and "acmebakery" not in n and "Pat" not in n and "Sam" not in n and "the owner" in n and "a team member" in n, n + out[-200:])
check("a JSON _doc field is prose: applied, host scrubbed, keys untouched", '"probe"' in sj and "acme-pc.tailabc123" not in sj and '"_doc"' in sj, sj)
w("assistant/serve.json", json.dumps({"_doc": "the schedule", "alert": "p648e3f@example.invalid", "jobs": [{"name": "spine", "every_min": 10}]}, indent=1))
c7 = commit("schedule: alert address")
rc, out = sync()
check("a JSON VALUE with the address is a working line: held", "held=1" in out and '"probe"' in (core_text("assistant/serve.json") or "") and "assistant/serve.json" in state()["holds"], out[-200:])

# ---------------------------------------------------------------- 8. deletion
(V / "assistant/scripts/beta.py").unlink()
c8 = commit("beta: gone")
rc, out = sync()
check("a deleted engine file is removed from sage-core and committed", rc == 0 and not (CORE / "assistant/scripts/beta.py").exists() and "beta: gone" in cgit("log", "-1", "--format=%s")
      and "assistant/scripts/beta.py" not in json.loads((CORE / "engine_manifest.json").read_text(encoding="utf-8"))["files"], out[-200:])

# ---------------------------------------------------------------- 9. niche_policy = apply, dry-run, classify
cfg = json.loads((V / "assistant" / "core_sync.json").read_text(encoding="utf-8"))
cfg["niche_policy"] = "apply"
w("assistant/core_sync.json", json.dumps(cfg, indent=1))
w("assistant/scripts/alpha.py", (V / "assistant/scripts/alpha.py").read_text(encoding="utf-8") + '\nKIND = "sourdough"\n')
c9 = commit("alpha: a niche constant, policy apply")
rc, out = sync("--dry-run")
check("dry-run changes nothing", "dry-run" in out and state()["last_commit"] != git("rev-parse", "HEAD") and "KIND" not in (core_text("assistant/scripts/alpha.py") or ""), out[-200:])
rc, out = sync()
check("with niche_policy = apply a niche line is applied, not held (core_sync.json is pack data and stays home)",
      "applied=1" in out and "held=0" in out and "KIND" in (core_text("assistant/scripts/alpha.py") or "") and "assistant/serve.json" in state()["holds"], out[-200:])
check("the pack template carries no address and no owner word, with identifier-safe tokens in their place",
      "acmebakery" not in (core_text("assistant/org.json") or "x") and "pat" not in (core_text("assistant/org.json") or "").lower()
      and "owner1-phone" in (core_text("assistant/org.json") or ""), core_text("assistant/org.json"))
env = {k: v for k, v in os.environ.items() if not k.startswith(("ASSISTANT_", "AGENT_", "DOER_"))}
env.update({"ASSISTANT_DATA": str(DATA), "WV_LOG_DIR": str(TMP / "logs")})
r = subprocess.run([sys.executable, str(V / "assistant" / "core_sync.py"), "--vault", str(V), "--core", str(CORE), "audit"],
                   capture_output=True, text=True, encoding="utf-8", errors="replace", env=env, timeout=120)
check("audit: not one line of the template mentions the organization (names, addresses, hosts, the vault path), the held file aside",
      r.returncode == 0 and "audit: 0 line(s)" in r.stdout, r.stdout[-600:])
env = {k: v for k, v in os.environ.items() if not k.startswith(("ASSISTANT_", "AGENT_", "DOER_"))}
env.update({"ASSISTANT_DATA": str(DATA), "WV_LOG_DIR": str(TMP / "logs")})
r = subprocess.run([sys.executable, str(V / "assistant" / "core_sync.py"), "--vault", str(V), "--core", str(CORE), "classify", "assistant/serve.json"],
                   capture_output=True, text=True, encoding="utf-8", errors="replace", env=env, timeout=120)
check("classify shows the held line", "HOLD" in r.stdout and "address" in r.stdout, r.stdout)
r = subprocess.run([sys.executable, str(V / "assistant" / "core_sync.py"), "--vault", str(V), "--core", str(CORE), "status"],
                   capture_output=True, text=True, encoding="utf-8", errors="replace", env=env, timeout=120)
check("status reads", "holds: 1" in r.stdout and "behind: 0" in r.stdout, r.stdout)
notes = [json.loads(ln) for ln in (DATA / "cards.jsonl").read_text(encoding="utf-8").splitlines() if ln.strip()]
check("a Mission Control note per run that changed something, none for an up-to-date run", 6 <= len(notes) <= 12 and all(n.get("source") == "core_sync" for n in notes), str(len(notes)))

print()
print(f"{'ALL OK' if not FAILS else 'FAILED: ' + ', '.join(FAILS)}  ({TMP})")
sys.exit(1 if FAILS else 0)

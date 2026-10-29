"""core_sync's TEMPLATE engine (2026-10-28): a property vault whose scripts/rescue/build_template.py holds the drop rules,
the {{TOKEN}} substitutions and the PII scrub flows on to a pm-operator-template checkout, commit by commit.

A throwaway vault "prop-os" carries a miniature build_template.py with the real builder's API (DROP_PREFIXES, STUBS,
SUBS, UNIT_RE, LEFT_RE, TEXT_EXT, Scrubber, PROPERTY_JSON, INSTANTIATE, README). Then: bootstrap into an empty core
(tokens in code, residents scrubbed, data dropped, stubs and package files written, one commit), a generic change
with a resident's name (applied, scrubbed), a hard-coded unit number in a working line (held; build_cmd filed;
note_cmd posted), the fix (released), a new unit-only file (promoted to niche), a change to dropped data (skipped),
auto-detection of the engine, status.
   python assistant/tests/test_core_sync_template.py
"""
import os as _os, tempfile as _tf
_os.environ.setdefault("WV_LOG_DIR", _tf.mkdtemp(prefix="wv_logs_"))
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")
HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
TMP = Path(tempfile.mkdtemp(prefix="wv_coresync_tpl_"))
V = TMP / "prop-os"
CORE = TMP / "pm-template"
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
    return subprocess.run(["git", *args], cwd=str(CORE), capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=60).stdout.strip()


def w(rel, text):
    p = V / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf-8")


def commit(msg):
    git("add", "-A")
    git("-c", "user.name=t", "-c", "user.email=t@example.invalid", "commit", "-q", "-m", msg)
    return git("rev-parse", "--short", "HEAD")


def sync(*extra, cmd="run"):
    env = {k: v for k, v in os.environ.items() if not k.startswith(("ASSISTANT_", "AGENT_", "DOER_"))}
    env["WV_LOG_DIR"] = str(TMP / "logs")
    r = subprocess.run([sys.executable, str(ROOT / "core_sync.py"), "--vault", str(V), "--core", str(CORE), cmd, *extra],
                       capture_output=True, text=True, encoding="utf-8", errors="replace", env=env, timeout=300)
    return r.returncode, (r.stdout or "") + (r.stderr or "")


def state():
    return json.loads((V / "scripts/rescue/.cache/core_sync/core_sync_state.json").read_text(encoding="utf-8"))


def core_text(rel):
    p = CORE / rel
    return p.read_text(encoding="utf-8") if p.exists() else None


# ---------------------------------------------------------------- the fixture vault, with a miniature of the real builder
BUILDER = r'''
"""miniature of scripts/rescue/build_template.py - the API core_sync's template engine reads."""
import json, re
from pathlib import Path
VAULT = Path(__file__).resolve().parents[2]
DROP_PREFIXES = ("Wiki/Tenants/", "Memory/", "Raw/", ".cache/")
DROP_ROOT_FILES = True
KEEP_ROOT = {"CLAUDE.md", "HANDOFF.md", ".gitignore"}
DROP_NAME_RE = re.compile(r"(\.jsonl$|_state\.json$|^vendors\.json$|\.log$)", re.I)
STUBS = {"Schema/mistakes.md": "# Mistakes Log\n\n## Entries\n", "Memory/memory.md": "# Memory\n"}
TEXT_EXT = {".py", ".ps1", ".md", ".json", ".txt", ""}
SUBS = [(r"Acme Realty Group", "{{COMPANY_NAME}}"), (r"Acme Realty", "{{COMPANY_SHORT}}"), (r"Oak Court", "{{PROPERTY_NAME}}"),
        (r"oak[_-]?court", "{{PROPERTY_SLUG}}"), (r"(?<=[\"'(\s=`])009(?=[\"')\s,.`:;\]]|$)", "{{PROPERTY_ID}}")]
SUBS = [(re.compile(p), t) for p, t in SUBS]
UNIT_RE = re.compile(r"\b[A-D]\d{2}\b")
LEFT_RE = re.compile(r"\b009\b")
PROPERTY_JSON = {"COMPANY_NAME": "", "PROPERTY_NAME": ""}
INSTANTIATE = "# instantiate.py - fills the tokens\n"
README = "# template\n"


def harvest_people():
    full = set()
    for p in (VAULT / "Wiki" / "Tenants").glob("*.md"):
        for m in re.finditer(r"\*\*Resident\(s\):\*\*\s*(.+)", p.read_text(encoding="utf-8")):
            full.add(m.group(1).strip())
    return full, set(), set()


class Scrubber:
    def __init__(self):
        full, single, vendors = harvest_people()
        self.maps, self.sizes = {}, (len(full), 0, 0)
        self.rx_full = re.compile("|".join(re.escape(n) for n in sorted(full, key=len, reverse=True)), re.I) if full else None

    def scrub(self, s):
        if self.rx_full:
            m = self.maps.setdefault("person", {})
            s = self.rx_full.sub(lambda x: m.setdefault(x.group(0).lower(), f"Resident {len(m) + 1:03d}"), s)
        return s
'''
w("scripts/rescue/build_template.py", BUILDER)
w("scripts/comms/send_notice.py", 'import json\n\nPROPERTY = "Oak Court"   # the property\n\n\ndef subject(unit):\n    return f"{PROPERTY} notice for {unit}"\n')
w("scripts/lib/vendors.json", '{"vendors": [{"name": "Handy Helpers"}]}\n')
w("Wiki/Tenants/A12.md", "**Resident(s):** Dana Okafor\n")
w("CLAUDE.md", "# Acme Realty Group operator\n\nOak Court, property 009.\n")
w("Schema/mistakes.md", "# Mistakes Log\n\n## Entries\n\n- A12 Dana Okafor ...\n")
w("wolist.md", "A12 blinds\n")
w("note_sink.py", 'import sys, pathlib\npathlib.Path("notes.txt").open("a", encoding="utf-8").write(sys.argv[1] + "\\n---\\n")\n')
w("build_sink.py", 'import sys\nprint("opened br-abc123 for", sys.argv[1])\n')
w("scripts/rescue/core_sync.json", json.dumps({"enabled": True, "core_root": str(CORE), "niche_terms": ["pet policy"], "niche_paths": [],
                                              "note_cmd": ["python", "note_sink.py", "{text}"],
                                              "build_cmd": ["python", "build_sink.py", "{file}", "{ask}"]}, indent=1))
w(".gitignore", ".cache/\nnotes.txt\n")
git("init", "-q")
git("config", "core.autocrlf", "false")
c0 = commit("prop: first")

# ---------------------------------------------------------------- 1. bootstrap into an empty core
rc, out = sync()
check("the template engine is auto-detected and bootstraps", rc == 0 and "bootstrapped" in out and (CORE / ".git").exists(), out[-300:])
sn = core_text("scripts/comms/send_notice.py") or ""
check("code is tokenized the builder's way (the template's contract), the property name gone", "{{PROPERTY_NAME}}" in sn and "Oak Court" not in sn, sn)
check("dropped data never travels; stubs and package files are written", not (CORE / "Wiki/Tenants/A12.md").exists() and not (CORE / "wolist.md").exists()
      and not (CORE / "scripts/lib/vendors.json").exists() and (core_text("Schema/mistakes.md") or "").strip() == "# Mistakes Log\n\n## Entries".strip()
      and (CORE / "instantiate.py").exists() and (CORE / "property.json").exists() and (CORE / "TEMPLATE_README.md").exists())
check("the kept root file carries tokens", "{{COMPANY_NAME}}" in (core_text("CLAUDE.md") or "") and "{{PROPERTY_ID}}" in (core_text("CLAUDE.md") or ""), core_text("CLAUDE.md"))
check("the builder itself never ships; the sync's own config is dropped as data", not (CORE / "scripts/rescue/build_template.py").exists())
st = state()
check("state lives under scripts/rescue/.cache (git-ignored); baseline recorded; the unit in send_notice is not debt (no unit there yet)",
      st["baseline_commit"] == git("rev-parse", "HEAD") and "scripts/comms/send_notice.py" not in st["debt"], json.dumps(st["debt"])[:200])
check("the note went through note_cmd", "template sync started" in ((V / "notes.txt").read_text(encoding="utf-8") if (V / "notes.txt").exists() else ""))
check("one template commit", cgit("rev-list", "--count", "HEAD") == "1" and "(from " + c0 in cgit("log", "-1", "--format=%s"), cgit("log", "-1", "--format=%s"))

# ---------------------------------------------------------------- 2. a generic change naming a resident in a comment
w("scripts/comms/send_notice.py", (V / "scripts/comms/send_notice.py").read_text(encoding="utf-8") + '\n\ndef body(name):\n    # Dana Okafor asked for plain words\n    return f"Dear {name}, your Oak Court notice is attached."\n')
c1 = commit("notice: body, as Dana Okafor asked")
rc, out = sync()
sn = core_text("scripts/comms/send_notice.py") or ""
check("applied: the resident becomes a neutral fake, the property name a token, in code and comment alike", "applied=1" in out and "Resident 001" in sn and "Okafor" not in sn and "your {{PROPERTY_NAME}} notice" in sn, sn + out[-200:])
check("the template commit message is scrubbed too", "Resident 001" in cgit("log", "-1", "--format=%s") and "(from " + c1 in cgit("log", "-1", "--format=%s"), cgit("log", "-1", "--format=%s"))

# ---------------------------------------------------------------- 3. a hard-coded unit number in a working line: held, build filed
w("scripts/comms/send_notice.py", (V / "scripts/comms/send_notice.py").read_text(encoding="utf-8") + '\nDEFAULT_UNIT = "A12"\n')
c2 = commit("notice: default unit")
rc, out = sync()
st = state()
h = st["holds"].get("scripts/comms/send_notice.py") or {}
check("a unit number in a working line is held: the template keeps the last clean version", "held=1" in out and "DEFAULT_UNIT" not in (core_text("scripts/comms/send_notice.py") or "")
      and any("unit number" in v["why"] for v in h.get("lines", {}).values()), out[-200:] + json.dumps(h)[:200])
check("ONE build request through build_cmd, its id kept on the hold", h.get("build") == "br-abc123", json.dumps(h)[:200])
notes = (V / "notes.txt").read_text(encoding="utf-8")
check("the hold is on the board through note_cmd", "held scripts/comms/send_notice.py" in notes and "br-abc123" in notes, notes[-300:])

# ---------------------------------------------------------------- 4. the fix releases the hold
w("scripts/comms/send_notice.py", (V / "scripts/comms/send_notice.py").read_text(encoding="utf-8").replace('DEFAULT_UNIT = "A12"', 'DEFAULT_UNIT = json.load(open("property.json"))["default_unit"]'))
c3 = commit("notice: default unit from property.json")
rc, out = sync()
check("released, the waiting change lands", "released=1" in out and "default_unit" in (core_text("scripts/comms/send_notice.py") or "") and not state()["holds"], out[-200:])

# ---------------------------------------------------------------- 5. a new file that is only about this property: promoted
w("scripts/comms/a12_blinds.py", 'UNITS = ["A12", "B33"]\n\n\ndef blinds(unit):\n    return unit in UNITS and unit != "C09"\n')
c4 = commit("comms: blinds for A12 and B33")
rc, out = sync()
cfg = json.loads((V / "scripts/rescue/core_sync.json").read_text(encoding="utf-8"))
check("a new unit-only file is promoted to niche and never ships", "promoted=1" in out and not (CORE / "scripts/comms/a12_blinds.py").exists()
      and "scripts/comms/a12_blinds.py" in cfg["niche_paths"] and cfg["note_cmd"], out[-200:] + json.dumps(cfg)[:300])

# ---------------------------------------------------------------- 6. dropped data, niche term, status
w("Wiki/Tenants/A12.md", "**Resident(s):** Dana Okafor\n\nPet: cat\n")
w("scripts/comms/send_notice.py", (V / "scripts/comms/send_notice.py").read_text(encoding="utf-8") + '\nPET = "pet policy for 009"\n')
c5 = commit("tenant note + pet policy line")
rc, out = sync()
check("a change to dropped data is skipped; a niche_terms word in a working line holds", "skipped 1" in out and "held=1" in out and "scripts/comms/send_notice.py" in state()["holds"], out[-250:])
rc, out = sync(cmd="status")
check("status names the engine and the core", "engine: template" in out and "pm-template" in out and "holds: 1" in out, out)
rc, out = sync("scripts/comms/send_notice.py", cmd="classify")
check("classify shows the hold and the lines the export tokenizes", "HOLD" in out and "tokn" in out and "{{PROPERTY_NAME}}" in out, out)

print()
print(f"{'ALL OK' if not FAILS else 'FAILED: ' + ', '.join(FAILS)}  ({TMP})")
sys.exit(1 if FAILS else 0)

"""core_sync's relay between agents (2026-10-28, the owner: "upgrades sync across all these agents as long as they are
generally useful and not specific to the organization"): a Sage fixture and a property-template fixture share one
commons folder. A generic code upgrade committed in one lands in its template (organization-free), is dropped in the
commons as a note with the template's diff, and the OTHER side's next sync files one port request with its builder
(Sage: a wiki build request; template: build_cmd with the diff written next to the state). Nightly state, docs, tests
and held files are never relayed; a note is filed once; the first inbox pass skips what was there before.
   python assistant/tests/test_core_sync_relay.py
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
TMP = Path(tempfile.mkdtemp(prefix="wv_coresync_relay_"))
S, SCORE = TMP / "acme-os", TMP / "sage-core"
P, PCORE = TMP / "prop-os", TMP / "pm-template"
COMMONS = TMP / "agent-commons"
SDATA = TMP / "sdata"
FAILS = []


def check(name, cond, extra=""):
    print(("ok   " if cond else "FAIL ") + name + (f"  {extra}" if extra and not cond else ""))
    if not cond:
        FAILS.append(name)


def git(v, *args):
    r = subprocess.run(["git", *args], cwd=str(v), capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=60)
    if r.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)}: {r.stderr.strip()[:300]}")
    return r.stdout.strip()


def w(v, rel, text):
    p = v / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf-8")


def commit(v, msg):
    git(v, "add", "-A")
    git(v, "-c", "user.name=t", "-c", "user.email=t@example.invalid", "commit", "-q", "-m", msg)
    return git(v, "rev-parse", "--short", "HEAD")


def sync(v, core, *extra, cmd="run"):
    env = {k: v2 for k, v2 in os.environ.items() if not k.startswith(("ASSISTANT_", "AGENT_", "DOER_"))}
    env["WV_LOG_DIR"] = str(TMP / "logs")
    if v == S:
        env["ASSISTANT_DATA"] = str(SDATA)
    r = subprocess.run([sys.executable, str(ROOT / "core_sync.py"), "--vault", str(v), "--core", str(core), cmd, *extra],
                       capture_output=True, text=True, encoding="utf-8", errors="replace", env=env, timeout=300)
    return r.returncode, (r.stdout or "") + (r.stderr or "")


# ---------------------------------------------------------------- the Sage fixture
(S / "assistant" / "agent").mkdir(parents=True)
for f in ("engine.py", "core_sync.py", "orgcfg.py"):
    shutil.copy2(ROOT / f, S / "assistant" / f)
w(S, "assistant/org.json", json.dumps({"name": "Acme Bakery", "vault_name": "acme-os", "send_identities": ["p648e3f@example.invalid"], "phone_accounts": {},
                                       "mission_control_url": "https://acme-pc.tailabc123.ts.net:9443", "internal_emails": [], "alert_from": "p648e3f@example.invalid", "alert_to": []}))
w(S, "assistant/agent/people.json", json.dumps({"roles": {}, "users": {"pat": {"name": "Pat Quinn", "role": "owner", "emails": ["p648e3f@example.invalid"]}}}))
w(S, "assistant/agent/wiki_writer.py", 'import re, pathlib\nHERE = pathlib.Path(__file__).resolve().parents[2]\n\n\ndef write(note, by="", commit=True):\n'
                                        '    title = re.sub(r"\\s+", " ", re.sub(r\'[\\\\/:*?"<>|#^\\[\\]]+\', " ", note["title"])).strip()[:90]\n    p = HERE / "Wiki" / "Personal" / (title + ".md")\n'
                                        '    p.parent.mkdir(parents=True, exist_ok=True)\n    p.write_text(note["body"], encoding="utf-8")\n'
                                        '    return {"status": "applied", "path": p.relative_to(HERE).as_posix()}\n')
w(S, "assistant/core_sync.json", json.dumps({"enabled": True, "core_root": str(SCORE), "niche_terms": [], "file_builds": False, "notify": False,
                                             "relay": {"enabled": True, "commons": str(COMMONS), "backfill": 5}}))
w(S, "assistant/scripts/alpha.py", 'def fetch(url):\n    return url\n')
w(S, "assistant/tests/test_alpha.py", 'import alpha\n')
git(S, "init", "-q"); git(S, "config", "core.autocrlf", "false")
commit(S, "acme: first")

# ---------------------------------------------------------------- the property fixture (the miniature builder of test_core_sync_template)
(P / "scripts" / "rescue").mkdir(parents=True)
BUILDER = r'''
import json, re
from pathlib import Path
VAULT = Path(__file__).resolve().parents[2]
DROP_PREFIXES = ("Wiki/Tenants/", "Memory/", "Raw/", ".cache/")
DROP_ROOT_FILES = True
KEEP_ROOT = {"CLAUDE.md", ".gitignore"}
DROP_NAME_RE = re.compile(r"(\.jsonl$|_state\.json$)", re.I)
STUBS = {"Schema/mistakes.md": "# Mistakes Log\n"}
TEXT_EXT = {".py", ".ps1", ".md", ".json", ".txt", ""}
SUBS = [(re.compile(r"Oak Court"), "{{PROPERTY_NAME}}")]
UNIT_RE = re.compile(r"\b[A-D]\d{2}\b")
LEFT_RE = re.compile(r"\b009\b")
PROPERTY_JSON = {"PROPERTY_NAME": ""}
INSTANTIATE = "# instantiate.py\n"
README = "# template\n"


class Scrubber:
    def __init__(self):
        self.maps, self.sizes, self.rx_full = {}, (0, 0, 0), None

    def scrub(self, s):
        return s
'''
w(P, "scripts/rescue/build_template.py", BUILDER)
w(P, "scripts/comms/send_notice.py", 'PROPERTY = "Oak Court"\n\n\ndef subject(unit):\n    return f"{PROPERTY} notice"\n')
w(P, "CLAUDE.md", "# operator\n")
w(P, "build_sink.py", 'import sys, pathlib\npathlib.Path("builds.txt").open("a", encoding="utf-8").write(" | ".join(sys.argv[1:]) + "\\n")\nprint("opened br-0a1b2c")\n')
w(P, "scripts/rescue/core_sync.json", json.dumps({"enabled": True, "core_root": str(PCORE), "notify": False, "file_builds": True,
                                                  "build_cmd": ["python", "build_sink.py", "{file}", "{ask}"],
                                                  "relay": {"enabled": True, "commons": str(COMMONS), "backfill": 5}}))
w(P, ".gitignore", ".cache/\nbuilds.txt\n")
git(P, "init", "-q"); git(P, "config", "core.autocrlf", "false")
commit(P, "prop: first")

# ---------------------------------------------------------------- 1. both bootstrap; nothing is relayed by a bootstrap
rc, out = sync(S, SCORE)
check("sage side bootstraps", rc == 0 and "bootstrapped" in out, out[-200:])
rc, out = sync(P, PCORE)
check("property side bootstraps", rc == 0 and "bootstrapped" in out, out[-200:])
check("a bootstrap drops nothing in the commons", not (COMMONS / "upgrades").exists() or not list((COMMONS / "upgrades").glob("*.md")))

# ---------------------------------------------------------------- 2. a generic code upgrade on the Sage side is relayed
w(S, "assistant/scripts/alpha.py", 'import time\n\n\ndef fetch(url, tries=3):\n    for i in range(tries):\n        try:\n            return url\n        except OSError:\n            time.sleep(1)\n')
c1 = commit(S, "alpha: retry a fetch three times before giving up")
rc, out = sync(S, SCORE)
notes = sorted((COMMONS / "upgrades").glob("*.md")) if (COMMONS / "upgrades").exists() else []
check("the upgrade is dropped in the commons with the template's diff", "relayed=1" in out and len(notes) == 1 and "source: \"sage\"" in notes[0].read_text(encoding="utf-8")
      and "+def fetch(url, tries=3):" in notes[0].read_text(encoding="utf-8"), out[-200:] + str([n.name for n in notes]))
w(S, "assistant/scripts/alpha.py", (S / "assistant/scripts/alpha.py").read_text(encoding="utf-8") + "\n# docs only\n")
commit(S, "docs: a comment")
w(S, "assistant/tests/test_alpha.py", 'import alpha\nassert alpha.fetch("x") == "x"\n')
commit(S, "alpha: a test")
rc, out = sync(S, SCORE)
notes = sorted((COMMONS / "upgrades").glob("*.md"))
check("a docs commit and a tests-only commit are applied but never relayed", "applied=2" in out and "relayed=0" in out and len(notes) == 1, out[-200:])

# ---------------------------------------------------------------- 3. the property side's inbox files ONE port request through its builder
rc, out = sync(P, PCORE)
builds = (P / "builds.txt").read_text(encoding="utf-8") if (P / "builds.txt").exists() else ""
req = list((P / "scripts/rescue/.cache/core_sync/relay").glob("*.md")) if (P / "scripts/rescue/.cache/core_sync/relay").exists() else []
check("the property side files a port request with its builder, the diff written next to its state", "ports=1" in out and "Port from sage-core" in builds
      and "assistant/scripts/alpha.py" in builds and len(req) == 1 and "+def fetch(url, tries=3):" in req[0].read_text(encoding="utf-8")
      and "change nothing and close the request as declined" in req[0].read_text(encoding="utf-8"), out[-200:] + builds[-300:])
st = json.loads((P / "scripts/rescue/.cache/core_sync/core_sync_state.json").read_text(encoding="utf-8"))
rc, out = sync(P, PCORE)
check("filed once: the next run sees nothing new", "ports=0" in out and len(st["relay_seen"]) == 1 and st["relay_baseline"], out[-200:])

# ---------------------------------------------------------------- 4. the other way round: a property upgrade reaches Sage as a wiki build request
w(P, "scripts/comms/send_notice.py", (P / "scripts/comms/send_notice.py").read_text(encoding="utf-8") + '\n\ndef retry(fn, tries=3):\n    for _ in range(tries):\n        try:\n            return fn()\n        except OSError:\n            pass\n')
c2 = commit(P, "notice: retry sends three times, Oak Court asked")
rc, out = sync(P, PCORE)
notes = sorted((COMMONS / "upgrades").glob("*.md"))
tpl = [n for n in notes if 'source: "template"' in n.read_text(encoding="utf-8")]
check("the property upgrade is in the commons, tokenized, not the property's name", "relayed=1" in out and len(tpl) == 1 and "Oak Court" not in tpl[0].read_text(encoding="utf-8")
      and "{{PROPERTY_NAME}}" in tpl[0].read_text(encoding="utf-8") and "+def retry(fn, tries=3):" in tpl[0].read_text(encoding="utf-8"), out[-200:])
rc, out = sync(S, SCORE)
wiki = list((S / "Wiki" / "Personal").glob("Build request Port from pm-template*.md")) if (S / "Wiki" / "Personal").exists() else []
check("Sage files a wiki build request for it, with the diff and the not_code rule", "ports=1" in out and len(wiki) == 1 and "+def retry(fn, tries=3):" in wiki[0].read_text(encoding="utf-8")
      and "answer not_code" in wiki[0].read_text(encoding="utf-8"), out[-200:] + str([x.name for x in wiki]))
rc, out = sync(S, SCORE)
check("Sage does not see its own upgrade as foreign, nor the filed one again", "ports=0" in out, out[-200:])

# ---------------------------------------------------------------- 5. a held commit is never relayed
w(S, "assistant/scripts/alpha.py", (S / "assistant/scripts/alpha.py").read_text(encoding="utf-8") + '\nOWNER = "Pat Quinn"\n')
commit(S, "alpha: owner constant")
rc, out = sync(S, SCORE)
check("a held commit is not relayed", "held=1" in out and "relayed=0" in out and len(sorted((COMMONS / "upgrades").glob("*.md"))) == 2, out[-200:])

print()
print(f"{'ALL OK' if not FAILS else 'FAILED: ' + ', '.join(FAILS)}  ({TMP})")
sys.exit(1 if FAILS else 0)

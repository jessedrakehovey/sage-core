"""The owner-facing install and update path (sage-core, 2026-10-28), end to end on a fixture organization.

1. release.py: a release of the engine (zip without a top folder + release.json with the archive's sha256) builds from an
   engine checkout and verifies; an organization's folder is exported first, never zipped raw.
2. updater.py: version compare (date part; same date + other sha = different), a sha256 mismatch applies nothing, a
   staged engine that fails its gate applies nothing, a good release lands (engine file replaced, pack file untouched,
   manifest version moved, previous engine kept, restart requested), rollback puts the previous engine back.
3. welcome.py: the setup page creates a pack from the three answers, reports it, runs the doctor, refuses cross-site posts.
4. The local page carries the organization's and the agent's names, never a literal.
   python assistant/tests/test_installer.py
"""
import os as _os, tempfile as _tf
_os.environ.setdefault("WV_LOG_DIR", _tf.mkdtemp(prefix="wv_logs_"))
import hashlib
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import urllib.request
import zipfile
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")
HERE = Path(__file__).resolve().parent
ROOT = HERE.parent                                   # assistant/
VAULT = ROOT.parent
TMP = Path(tempfile.mkdtemp(prefix="wv_installer_"))
ENGINE = TMP / "sage-core"
OUT = TMP / "acme-os"
FAILS = []
sys.path.insert(0, str(ROOT))
import engine  # noqa: E402
import release  # noqa: E402
import updater  # noqa: E402


def check(name, cond, extra=""):
    print(("ok   " if cond else "FAIL ") + name + (f"  {extra}" if extra and not cond else ""))
    if not cond:
        FAILS.append(name)


def run(args, cwd=None, env_extra=None, timeout=300):
    env = {k: v for k, v in os.environ.items() if not k.startswith(("ASSISTANT_", "AGENT_", "DOER_", "SAGE_"))}
    env.update(WV_LOG_DIR=str(TMP / "logs"), CHAT_PUSH="off", PYTHONIOENCODING="utf-8", SAGE_HOME=str(TMP / "home"))
    env.update(env_extra or {})
    r = subprocess.run([sys.executable] + args, capture_output=True, text=True, encoding="utf-8", errors="replace", cwd=str(cwd or OUT), env=env, timeout=timeout)
    return r.returncode, (r.stdout or "") + (r.stderr or "")


def sha(p: Path) -> str:
    return hashlib.sha256(p.read_bytes()).hexdigest()


# ---------------------------------------------------------------- 1. a release from the engine
man = engine.export(ENGINE, src_root=VAULT, with_tests=True)
check("the export is an engine checkout", release.is_engine_checkout(ENGINE))   # VAULT may itself be one (this test runs inside sage-core too)
dist1 = TMP / "dist1"
rec1 = release.build(ENGINE, dist1, notes=["first"])
z1 = dist1 / rec1["file"]
check("release.build writes the zip + release.json (sha256, version, file) from an engine checkout",
      z1.exists() and (dist1 / "release.json").exists() and rec1["sha256"] == sha(z1) and rec1["version"] == man["engine"]["version"]
      and rec1["source"] == "engine checkout", json.dumps({k: rec1[k] for k in ("version", "file", "source")}))
with zipfile.ZipFile(z1) as z:
    names = z.namelist()
check("the archive has no top folder and carries the manifest, sage.py and the updater", "engine_manifest.json" in names and "assistant/sage.py" in names
      and "assistant/updater.py" in names and "assistant/welcome.py" in names and "assistant/installer/windows/install.ps1" in names
      and not any(n.startswith("sage-core/") for n in names), names[:3])
check("the archive carries no pack state (the pack's template files travel once, as new_org's templates)", not any(n.startswith(("assistant/data/", "assistant/agent/state/", "assistant/out/")) for n in names))
ok, msg = release.verify(z1)
check("release.verify passes on a good pair", ok, msg)
bad = TMP / "dist_bad"
shutil.copytree(dist1, bad)
rj = json.loads((bad / "release.json").read_text(encoding="utf-8"))
rj["sha256"] = "0" * 64
(bad / "release.json").write_text(json.dumps(rj), encoding="utf-8")
ok, msg = release.verify(bad / rec1["file"])
check("release.verify fails on a sha256 mismatch", not ok and "mismatch" in msg, msg)

# ---------------------------------------------------------------- 2. a pack from the export, then updates
rc, out = run([str(ENGINE / "assistant" / "new_org.py"), "Acme Bakery", "--agent", "Pip", "--owner", "Ana Ruiz:ana@acme.test", "--out", str(OUT)], cwd=ENGINE, timeout=400)
check("new_org stands up the fixture organization", rc == 0 and (OUT / "assistant" / "org.json").exists(), out[-300:])
check("a pack is never taken for an engine checkout (release.py would export it first)", not release.is_engine_checkout(OUT))
org_json = json.loads((OUT / "assistant" / "org.json").read_text(encoding="utf-8"))
check("a new organization's org.json carries engine_release_url (empty for an engine with no GitHub origin)", org_json.get("engine_release_url") == "")
# an engine cloned from GitHub hands its Releases feed to every organization made from it (no account name in the engine)
subprocess.run(["git", "init", "-q"], cwd=str(ENGINE), capture_output=True)
subprocess.run(["git", "remote", "add", "origin", "https://github.com/acme-tools/sage-core.git"], cwd=str(ENGINE), capture_output=True)
check("feed_from_origin reads a GitHub clone's Releases feed", updater.feed_from_origin(ENGINE) == "https://github.com/acme-tools/sage-core/releases/latest/download", updater.feed_from_origin(ENGINE))
rc, out = run([str(ENGINE / "assistant" / "new_org.py"), "Acme Two", "--agent", "Pip", "--owner", "Ana Ruiz:ana@acme.test", "--out", str(TMP / "acme-two")], cwd=ENGINE, timeout=400)
org2 = json.loads((TMP / "acme-two" / "assistant" / "org.json").read_text(encoding="utf-8")) if (TMP / "acme-two" / "assistant" / "org.json").exists() else {}
check("new_org from a GitHub clone writes that feed into org.json", org2.get("engine_release_url") == "https://github.com/acme-tools/sage-core/releases/latest/download", out[-200:])
shutil.rmtree(TMP / "acme-two", ignore_errors=True)
shutil.rmtree(ENGINE / ".git", ignore_errors=True)

check("parse_version reads the engine's version form", updater.parse_version("2026.10.28+58bca0a") == ((2026, 10, 28), "58bca0a") and updater.parse_version("junk")[0] == (0, 0, 0))
check("compare: newer / older / same / different / unknown",
      updater.compare("2026.10.28+aaa", "2026.11.02+bbb") == "newer" and updater.compare("2026.11.02+bbb", "2026.10.28+aaa") == "older"
      and updater.compare("2026.10.28+aaa", "2026.10.28+aaa") == "same" and updater.compare("2026.10.28+aaa", "2026.10.28+bbb") == "different"
      and updater.compare("2026.10.28+aaa", "junk") == "unknown")

# the vault itself is not a pack: the updater skips it
rc, out = run([str(ROOT / "updater.py"), "check"], cwd=VAULT)
check("the engine's source folder is never updated by the feed (not a pack)", rc == 0 and "not a pack" in out, out[-200:])

v1 = man["engine"]["version"]


def make_engine(tag: str, version: str, break_it: bool = False) -> Path:
    e = TMP / f"engine-{tag}"
    shutil.copytree(ENGINE, e)
    cat = e / "assistant" / "catalog.py"
    cat.write_text(cat.read_text(encoding="utf-8") + ("\ndef broken(:\n" if break_it else f"\n# engine {tag}: a comment\n"), encoding="utf-8")
    m = json.loads((e / "engine_manifest.json").read_text(encoding="utf-8"))
    m["files"]["assistant/catalog.py"] = hashlib.sha256(engine.to_template(engine.read_text(cat), e).encode("utf-8")).hexdigest()
    m["engine"]["version"] = version
    (e / "engine_manifest.json").write_text(json.dumps(m, indent=1), encoding="utf-8")
    return e


e2 = make_engine("two", "2027.01.02+aaaaaaa")
dist2 = TMP / "dist2"
rec2 = release.build(e2, dist2, notes=["two"])
check("a release builds from the second engine with its own version", rec2["version"] == "2027.01.02+aaaaaaa" and (dist2 / rec2["file"]).exists())
cat_before = (OUT / "assistant" / "catalog.py").read_bytes()
cfg_before = (OUT / "assistant" / "config.json").read_bytes()

# (a) sha mismatch: nothing applied
bad2 = TMP / "dist2_bad"
shutil.copytree(dist2, bad2)
rj = json.loads((bad2 / "release.json").read_text(encoding="utf-8"))
rj["sha256"] = "f" * 64
(bad2 / "release.json").write_text(json.dumps(rj), encoding="utf-8")
rc, out = run([str(OUT / "assistant" / "updater.py"), "run", "--no-gate", "--url", str(bad2)])
check("a sha256 mismatch applies nothing and exits non-zero", rc != 0 and "hash mismatch" in out and (OUT / "assistant" / "catalog.py").read_bytes() == cat_before, out[-300:])
notes = [json.loads(ln) for ln in (OUT / "assistant" / "data" / "cards.jsonl").read_text(encoding="utf-8").splitlines() if ln.strip()] if (OUT / "assistant" / "data" / "cards.jsonl").exists() else []
check("the failure left ONE note for the agent's page, never a chat line", len([n for n in notes if n.get("source") == "updater"]) == 1 and "did not land" in notes[-1]["text"], str(notes[-1:])[:200])

# (b) a broken engine fails the gate: nothing applied
e3 = make_engine("three", "2027.01.03+bbbbbbb", break_it=True)
dist3 = TMP / "dist3"
release.build(e3, dist3, notes=["three"])
rc, out = run([str(OUT / "assistant" / "updater.py"), "run", "--no-gate", "--url", str(dist3)])
check("a staged engine that does not compile fails the gate; nothing applied, no engine kept",
      rc != 0 and "gate failed" in out and (OUT / "assistant" / "catalog.py").read_bytes() == cat_before and not (OUT / ".sage-engines" / "2027.01.03-bbbbbbb").exists()
      and not any(p.name.startswith("staging-") for p in (OUT / ".sage-engines").iterdir()) if (OUT / ".sage-engines").exists() else True, out[-300:])

# (c) the good release lands, gated by compile + doctor against this pack
rc, out = run([str(OUT / "assistant" / "updater.py"), "run", "--gates", "compile,doctor", "--url", str(dist2)])
man_after = json.loads((OUT / "engine_manifest.json").read_text(encoding="utf-8"))
check("the good release lands: engine file replaced, manifest version moved, exit 0",
      rc == 0 and "updated" in out and b"engine two: a comment" in (OUT / "assistant" / "catalog.py").read_bytes() and man_after["engine"]["version"] == "2027.01.02+aaaaaaa", out[-400:])
check("a fresh pack's files count as unchanged by hand: nothing is held back as a conflict", "changed by hand" not in out and "conflicts" not in out.lower(), out[-300:])
check("the pack's manifest carried the engine's full version from the export (sha included)", v1 == man["engine"]["version"] and "+" in v1, v1)
check("a pack file is never touched by the update", (OUT / "assistant" / "config.json").read_bytes() == cfg_before)
check("the previous engine is kept for rollback and the supervisor is asked to restart",
      (OUT / ".sage-engines" / "previous" / "rollback.json").exists() and json.loads((OUT / ".sage-engines" / "previous" / "rollback.json").read_text())["version"] == v1
      and (OUT / ".sage-engines" / "2027.01.02-aaaaaaa" / "engine_manifest.json").exists() and (OUT / "assistant" / "data" / ".sage_restart").exists())
rc, out = run([str(OUT / "assistant" / "updater.py"), "run", "--no-gate", "--url", str(dist2)])
check("the same release again is 'up to date'", rc == 0 and "up to date" in out, out[-200:])
rc, out = run([str(OUT / "assistant" / "sage.py"), "update", "--check"], env_extra={"SAGE_RELEASE_URL": str(dist2)})
check("sage update --check reads the feed from SAGE_RELEASE_URL", rc == 0 and "2027.01.02+aaaaaaa" in out and "same" in out, out[-200:])

# (d) rollback
(OUT / "assistant" / "data" / ".sage_restart").unlink()
rc, out = run([str(OUT / "assistant" / "sage.py"), "rollback"])
man_back = json.loads((OUT / "engine_manifest.json").read_text(encoding="utf-8"))
check("sage rollback restores the previous engine file and manifest version, and asks for a restart",
      rc == 0 and (OUT / "assistant" / "catalog.py").read_bytes() == cat_before and man_back["engine"]["version"] == v1 and (OUT / "assistant" / "data" / ".sage_restart").exists(), out[-300:])
check("rollback leaves no .pre-update litter and the pack file is still untouched", not list(OUT.rglob("*.pre-update")) and (OUT / "assistant" / "config.json").read_bytes() == cfg_before)
rc, out = run([str(OUT / "assistant" / "updater.py"), "check", "--url", str(dist2)])
check("after the rollback the feed is 'newer' again", rc == 0 and "(newer)" in out, out[-200:])
rc, out = run([str(OUT / "assistant" / "sage.py"), "doctor", "--json", "--no-probe"])
try:
    rows = {r["check"]: r for r in json.loads(out[out.index("["):])}
except Exception:  # noqa: BLE001
    rows = {}
check("doctor: git is a WARN not a FAIL, gcloud is info, and the engine-updates row exists",
      rows.get("git", {}).get("status") in ("ok", "warn") and rows.get("gcloud", {}).get("status") in ("ok", "info") and "engine updates" in rows, out[-300:])

# ---------------------------------------------------------------- 3. the setup page (standalone, from the engine)
def free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


rc, out = run([str(ENGINE / "assistant" / "welcome.py"), "--state"], cwd=ENGINE)
st = json.loads(out[out.index("{"):]) if "{" in out else {}
check("welcome --state from a bare engine: no pack yet, creation possible", rc == 0 and st.get("pack") == "" and st.get("can_create") is True, out[-300:])
port = free_port()
env = {k: v for k, v in os.environ.items() if not k.startswith(("ASSISTANT_", "SAGE_"))}
env.update(SAGE_HOME=str(TMP / "home"), PYTHONIOENCODING="utf-8", WV_LOG_DIR=str(TMP / "logs"))
srv = subprocess.Popen([sys.executable, str(ENGINE / "assistant" / "welcome.py"), "--port", str(port), "--no-browser"], cwd=str(ENGINE), env=env,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def http(method, path, body=None, origin=None):
    data = json.dumps(body).encode("utf-8") if body is not None else None
    req = urllib.request.Request(f"http://127.0.0.1:{port}{path}", data=data, method=method)
    if data is not None:
        req.add_header("Content-Type", "application/json")
    if origin:
        req.add_header("Origin", origin)
    try:
        with urllib.request.urlopen(req, timeout=600) as r:
            return r.status, r.read().decode("utf-8")
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8")


try:
    for _ in range(50):
        try:
            code, page = http("GET", "/welcome")
            break
        except Exception:  # noqa: BLE001
            time.sleep(0.2)
    check("the setup page serves", code == 200 and "Welcome" in page and "Mission Control" not in page)
    code, body = http("POST", "/api/welcome/create", {"org": "Beta Shop", "agent": "Mo", "owners": [{"name": "Lee Park", "email": "lee@beta.test"}]}, origin="http://evil.test")
    check("a cross-site post is refused", code == 403, body[:120])
    code, body = http("POST", "/api/welcome/create", {"org": "Beta Shop", "agent": "Mo", "owners": [{"name": "Lee Park", "email": ""}], "folder": str(TMP / "beta-os")}, origin=f"http://127.0.0.1:{port}")
    check("an owner without an email is refused with a plain sentence", code == 200 and json.loads(body)["ok"] is False and "email" in json.loads(body)["message"], body[:200])
    code, body = http("POST", "/api/welcome/create", {"org": "Beta Shop", "agent": "Mo", "what": "a corner shop", "owners": [{"name": "Lee Park", "email": "lee@beta.test"}], "folder": str(TMP / "beta-os")},
                      origin=f"http://127.0.0.1:{port}")
    d = json.loads(body)
    check("the three answers create the pack", code == 200 and d.get("ok") is True and (TMP / "beta-os" / "assistant" / "org.json").exists(), body[:300])
    home = json.loads((TMP / "home" / "pack.json").read_text(encoding="utf-8")) if (TMP / "home" / "pack.json").exists() else {}
    beta_org = json.loads((TMP / "beta-os" / "assistant" / "org.json").read_text(encoding="utf-8"))
    check("the pack is registered for this machine and knows its interpreter", home.get("pack") == str(TMP / "beta-os") and beta_org.get("python") == sys.executable, str(home)[:200])
    code, body = http("GET", "/api/welcome/state")
    st2 = json.loads(body)
    check("state now names the organization, the agent and its owners", st2.get("org") == "Beta Shop" and st2.get("agent") == "Mo" and "Lee Park" in st2.get("owners", []), body[:300])
    code, body = http("POST", "/api/welcome/create", {"org": "X", "agent": "Y", "owners": [{"name": "A B", "email": "a@b.test"}]}, origin=f"http://127.0.0.1:{port}")
    check("a second organization is refused while one is registered", json.loads(body).get("ok") is False)
    code, body = http("POST", "/api/welcome/doctor", {}, origin=f"http://127.0.0.1:{port}")
    d = json.loads(body)
    rows2 = {r["check"]: r for r in d.get("rows", [])}
    check("the doctor runs from the page against the new pack", d.get("ok") is True and rows2.get("org pack", {}).get("status") == "ok" and "Beta" in rows2.get("org pack", {}).get("detail", ""), body[:300])
    code, body = http("POST", "/api/welcome/nothing", {}, origin=f"http://127.0.0.1:{port}")
    check("an unknown step is a 404", code == 404)
finally:
    srv.kill()

# ---------------------------------------------------------------- 4. the page carries the organization's names
rc, out = run(["-c", "import sys; sys.path.insert(0,'assistant/scripts'); import dashboard as d; print(d._brand(), '|', d._page_title())"], cwd=TMP / "beta-os")
check("the local page is titled with the organization and the agent, no literal", rc == 0 and "Beta Shop | Mo" in out, out[-200:])
rc, out = run(["-c", "import sys; sys.path.insert(0,'assistant/scripts'); import dashboard as d; print(d._page_title())"], cwd=VAULT)
check("the source organization's page is titled with its agent's name as well (no literal anywhere)", rc == 0 and "Mission Control" not in out and out.strip().splitlines()[-1].strip() != "", out[-200:])

print()
print("ALL PASSED" if not FAILS else f"{len(FAILS)} FAILED: {FAILS}", f"({TMP})")
if not FAILS:
    shutil.rmtree(TMP, ignore_errors=True)
sys.exit(1 if FAILS else 0)

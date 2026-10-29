"""new_org.py - stand up this assistant for another organization, from a copy of this folder.

the owner (2026-10-11): "someday Sage will be the template for creating personal learning agents for
businesses". The code under assistant/ holds no business facts; what makes it the organization' is
a handful of JSON files, prompt.md and the state folders. This script writes a fresh set for a
new organization and prints the Setup checklist the owners then finish on Mission Control.

  python assistant/new_org.py "Acme Bakery" --agent Pip --owner "Ana Ruiz:ana@acme.test" --owner "example Ruiz:example@acme.test" \
        [--out C:/path/to/acme-os]          # default: a sibling folder of this vault named after the org

What it does, in the OUT folder (never in this one):
  copies  assistant/ (code only: no data/, out/, state/, learnings/, gchat.local.json, google.json tokens)
          scripts/ (the shared tooling the agent calls), Schema/, .claude/ (hooks, skills), the wiki scaffold
  writes  assistant/agent/profile.json, people.json, prompt.md (from templates/prompt.generic.md),
          state/org.json (name + owners; everything else is learned in chat),
          assistant/connections.json with the generic rows (Claude token, Mission Control, agent bridge,
          doer browser + vault; the Google rows per owner), Wiki/ and Memory/ skeletons, a README
  prints  the Setup checklist (what the owners connect on /setup, in order)

What stays per organization and must be decided by its owners: who may approve what, the never-list
(org.json `never` + profile `extra_protected`), which actions are reversible enough to auto-apply.
Credentials are never copied: every connection is made again, by the new owners, on their machine.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent          # assistant/ (this vault's, or an exported engine's)
VAULT = HERE.parent
sys.path.insert(0, str(HERE))
import engine  # noqa: E402  (sage-core, 2026-10-27: the engine / pack contract both this script and `sage update` read)

SKIP_DIRS = {"data", "out", "__pycache__", "learnings", ".git", "state", "gchat_relay", "tools", "skills", "tests"}
SKIP_FILES = {"gchat.local.json", "google.json", "org.json"}
# the data files a new organization cannot run without (followups exits FATAL without a sop_map; the comms law sets
# the reply clocks; client_texts holds the standing-text words to rewrite): copied as templates (WP5, 2026-10-24)
DATA_TEMPLATES = ("sop_map.json", "comms_law.json", "client_texts.json")
SRC = engine.org_values(VAULT)          # the source organization's own words (org.json + people.json), never literals
SRC_OWNER_KEYS = [p["key"] for p in SRC["persons"] if p["role"] == "owner"] or ["owner1"]


def _read_json(p: Path) -> dict:
    try:
        d = json.loads(p.read_text(encoding="utf-8-sig"))
        return d if isinstance(d, dict) else {}
    except (OSError, ValueError):
        return {}


def _source_pairs(org_name: str, owners: list, out_name: str) -> tuple:
    """(scrub, infra): regex pairs that turn the source organization's names, hosts, addresses and owners into the new
    organization's (names -> its name, the i-th owner's address -> its i-th owner's, people -> "the owner"). infra is the
    subset without people (the PC's user path carries the owner's name; the wrappers keep it)."""
    v = SRC
    emails = sorted(v["emails"])
    new_mail = [o["email"] for o in owners if o["email"]] or ["owner@example.invalid"]
    infra, people = [], []
    src_org = _read_json(VAULT / "assistant" / "org.json")
    src_name = str(src_org.get("name") or "")
    short = src_name.split()[0] if src_name.split() else ""
    tasks = [str(x) for x in (src_org.get("watched_tasks") or [])]
    for task in sorted(tasks, key=len, reverse=True):          # "<Short> Day Spine (assistant)" -> "<Org> Day Spine (assistant)"
        infra.append((re.compile(re.escape(task), re.I), task.replace(short, org_name, 1) if short and short in task else f"{org_name} {task}"))
    for n in sorted(v["names"] - set(tasks), key=len, reverse=True):
        infra.append((engine._rx_word(n), org_name))
    if len(short) >= 3:
        infra.append((engine._rx_word(short), org_name))
    for h in sorted(v["hosts"], key=len, reverse=True):
        infra.append((re.compile(re.escape(h), re.I), "<this-pc>.<tailnet>.ts.net"))
    for part in sorted(v["hostparts"], key=len, reverse=True):
        infra.append((engine._rx_word(part), "tailnet"))
    for e in sorted(emails, key=len, reverse=True):
        infra.append((re.compile(re.escape(e), re.I), new_mail[0]))        # every source address -> the first new owner's
    for d in sorted(v["domains"], key=len, reverse=True):
        infra.append((re.compile(re.escape(d), re.I), "example.invalid"))
    for s in sorted(v["stems"], key=len, reverse=True):
        infra.append((engine._rx_word(s), "example"))
    for loc in sorted(v["locals"], key=len, reverse=True):
        infra.append((re.compile(re.escape(loc), re.I), owners[0]["key"]))
    if v.get("root"):
        infra.append((re.compile(re.escape(Path(v["root"]).name), re.I), out_name))
    for p in v["persons"]:
        who = "the owner" if p["role"] == "owner" else "a team member"
        for w in sorted({p["name"]} | set(p["words"]), key=len, reverse=True):
            if len(w) >= 3:
                people.append((engine._rx_word(w + "'s"), who + "'s"))
                people.append((engine._rx_word(w), who))
    for s in sorted(v["surnames"], key=len, reverse=True):
        people.append((engine._rx_word(s), org_name))
    return infra + people, infra
GENERIC_CONNECTIONS = ["claude-token", "mission-control", "tailscale", "agent-bridge", "doer-browser", "doer-vault", "doer-sessions", "pw-profiles"]


def _read_any(f: Path) -> str:
    """A task XML may be UTF-16 (the form schtasks wants) or UTF-8 (the form git keeps)."""
    b = f.read_bytes()
    if b[:2] in (bytes([0xFF, 0xFE]), bytes([0xFE, 0xFF])):
        return b.decode("utf-16")
    return b.decode("utf-8-sig", errors="replace")


def _slug(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", s.lower()).strip("-")


def _user_sid() -> str:
    """The installing Windows user's account SID (for the scheduled-task XMLs), '' elsewhere or when unknown."""
    if os.name != "nt":
        return ""
    try:
        r = subprocess.run(["whoami", "/user"], capture_output=True, text=True, timeout=15, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        m = engine.SID_RX.search(r.stdout or "")
        return m.group(0) if m else ""
    except (OSError, subprocess.TimeoutExpired):
        return ""


def _origin_feed(checkout: Path) -> str:
    """The GitHub Releases feed (`.../releases/latest/download`) of this checkout's git origin, or '' (updater.feed_from_origin)."""
    try:
        import updater
        return updater.feed_from_origin(checkout)
    except Exception:  # noqa: BLE001
        return ""


def _copy_tree(src: Path, dst: Path) -> int:
    n = 0
    for p in src.rglob("*"):
        rel = p.relative_to(src)
        if any(part in SKIP_DIRS for part in rel.parts) or p.name in SKIP_FILES:
            continue
        if p.is_dir():
            (dst / rel).mkdir(parents=True, exist_ok=True)
            continue
        if p.suffix in (".jsonl", ".log", ".kdbx", ".png") or p.name.endswith(".local.json"):
            continue
        if p.suffix == ".md" and (p.name.startswith(("BUILD_PLAN", "BUILD_PROPOSAL", "PLAN-", "TRACKING_DESIGN")) or p.name.endswith("_IPHONE_SETUP.md")):
            continue          # this organization's planning history travels nowhere (WP5)
        (dst / rel).parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(p, dst / rel)
        n += 1
    return n


def main() -> int:
    sys.stdout.reconfigure(encoding="utf-8")
    ap = argparse.ArgumentParser()
    ap.add_argument("org", help="the organization's name")
    ap.add_argument("--agent", required=True, help="what the owners call their agent (their choice, asked at setup; never defaulted)")
    ap.add_argument("--owner", action="append", default=[], help='"Full Name:email" (repeat)')
    ap.add_argument("--out", default="", help="destination folder (default: sibling of this vault)")
    ap.add_argument("--what", default="", help="one sentence: what the organization does")
    ap.add_argument("--module", action="append", default=[], choices=sorted(engine.MODULES), help="optional engine module to switch on (repeat)")
    ap.add_argument("--lifecycle", default="service-studio", help="lifecycle skeleton for training (assistant/onboarding/lifecycles/)")
    ap.add_argument("--lanes", default="business,personal", help="comma-separated lanes (default business,personal)")
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args()
    if not a.owner:
        sys.exit("at least one --owner \"Name:email\" is required")
    out = Path(a.out) if a.out else VAULT.parent / (_slug(a.org) + "-os")
    if out.resolve() == VAULT.resolve() or VAULT.resolve() in out.resolve().parents:
        sys.exit("the new organization must live in its own folder, outside this one")
    if out.exists() and any(out.iterdir()):
        sys.exit(f"{out} is not empty")
    owners = []
    for o in a.owner:
        name, _, email = o.partition(":")
        owners.append({"key": _slug(name.split()[0]), "name": name.strip(), "email": email.strip()})
    print(f"New organization: {a.org}\nAgent: {a.agent}\nOwners: {', '.join(o['name'] for o in owners)}\nFolder: {out}")
    if a.dry_run:
        return 0
    out.mkdir(parents=True)
    scrub_pairs, infra_pairs = _source_pairs(a.org, owners, out.name)
    # the engine (code + templates) with the new root substituted; nothing niche, no state, no tests (engine.py)
    manifest_files = engine.install_tree(VAULT, out, with_tests=False)
    n = len(manifest_files)
    # the wiki contract (2026-10-29): the GENERIC schema, about pages and note templates the engine ships under
    # assistant/templates/Schema, never this organization's own Schema/ (its types, tags, naming and routing are its
    # business's; engine.NICHE_PATHS keeps them out of install_tree). The new organization owns its copy from here on.
    gen = HERE / "templates" / "Schema"
    if gen.is_dir():
        for p in gen.rglob("*"):
            if p.is_file():
                dst = out / "Schema" / p.relative_to(gen)
                dst.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(p, dst)
    lanes = [x.strip() for x in a.lanes.split(",") if x.strip()] or ["business"]
    for d in ("Wiki/_meta", "Wiki/People", "Wiki/Operations", "Wiki/Clients", "Wiki/Vendors", "Wiki/Integrations", "Wiki/Builder",
              "Memory/Daily_Logs", "Raw/Uploads", "Context", "assistant/agent/state", "assistant/data", "assistant/out", "assistant/learnings"):
        (out / d).mkdir(parents=True, exist_ok=True)
    for lane in lanes:
        (out / "Wiki" / lane.capitalize()).mkdir(parents=True, exist_ok=True)
    (out / "Memory" / "memory.md").write_text(f"# Memory\n\nLong-term facts about {a.org}, distilled from daily logs.\n", encoding="utf-8")
    (out / "Memory" / "about.md").write_text("# Memory\n\nDaily_Logs/ is the raw capture (append-only, written by the SessionEnd hook); memory.md is what the "
                                             "nightly dreaming distils from it; skill-suggestions.md and automation-suggestions.md are the gated queues.\n", encoding="utf-8")
    (out / "Context" / "Soul.md").write_text(f"# {a.agent}\n\nThe assistant of {a.org}. Calm, exact, warm. Drafts freely; nothing outward goes without an owner's tap. "
                                             "Learns the organization's voice from its own messages during the shadow week.\n", encoding="utf-8")
    (out / "Context" / "User.md").write_text("# Owners\n\n" + "\n".join(f"- {o['name']} ({o['email']})" for o in owners) + "\n", encoding="utf-8")
    (out / "Schema" / "mistakes.md").write_text("# Mistakes Log\n\nRead at every session start. One entry per mistake the agent made, so none is made twice.\n\n"
                                                "## Format\n\n---\n**Date:** YYYY-MM-DD\n**Workflow:** \n**What happened:** \n**Why it was wrong:** \n**Correct behavior:** \n---\n\n## Entries\n", encoding="utf-8")
    # .claude/settings.json: a hook whose script did not ship (a niche gate) would fail every session
    st_path = out / ".claude" / "settings.json"
    if st_path.exists():
        try:
            st = json.loads(st_path.read_text(encoding="utf-8-sig"))
            for ev, arr in list((st.get("hooks") or {}).items()):
                keep = []
                for h in arr:
                    cmds = " ".join(x.get("command", "") for x in h.get("hooks", []))
                    paths = re.findall(r'"([^"]+\.py)"', cmds)
                    if all((Path(p).exists() if os.path.isabs(p) else (out / p).exists()) for p in paths):
                        keep.append(h)
                st["hooks"][ev] = keep
            st_path.write_text(json.dumps(st, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        except ValueError:
            pass
    # the data templates (WP5): sop_map / comms_law / client_texts, with this organization's names scrubbed; rulings start empty
    for name in DATA_TEMPLATES:
        src = HERE / "data" / name
        if not src.exists():
            src = HERE / "templates" / "data" / name          # an exported engine carries the data templates here
        if src.exists():
            txt = src.read_text(encoding="utf-8")
            for rx, rep in scrub_pairs:
                txt = rx.sub(rep, txt)
            if name == "sop_map.json":
                # the generic inquiry follow-through only (reply clock, two quote nudges): the lifecycle itself is
                # learned in Training (onboarding.py), never copied from another organization's SOPs
                try:
                    d = json.loads(txt)
                    sops = d.get("sops") or {}
                    keep = {k: v for k, v in sops.items() if k == "inquiry"}
                    for v in keep.values():
                        v["lane"] = lanes[0]
                        v["doc"] = ""
                        for stp in (v.get("steps") or {}).values():
                            if isinstance(stp.get("action"), dict):
                                stp["action"]["skill"] = ""
                    d["sops"] = keep
                    txt = json.dumps(d, indent=1, ensure_ascii=False) + "\n"
                except ValueError:
                    pass
            (out / "assistant" / "data" / name).write_text(txt, encoding="utf-8")
    (out / "assistant" / "data" / "rulings.json").write_text(json.dumps({"_doc": "standing rulings the owners give on cards (rulings.py); empty for a new organization", "rulings": []}, indent=1) + "\n", encoding="utf-8")
    # the organization file the CODE reads (orgcfg.py): identities, phones, Mission Control, watched tasks
    org_name = a.org
    (out / "assistant" / "org.json").write_text(json.dumps({
        "_doc": "The organization's own values the code needs (assistant/orgcfg.py). Protected core: send_identities and phone_accounts are what the send gate guards.",
        "name": org_name, "vault_name": out.name,
        "send_identities": [o["email"] for o in owners if o["email"]],
        "phone_accounts": {f"{o['key']}-iphone": {"lane": "personal", "owner": o["name"].split()[0]} for o in owners},
        "default_phone_account": f"{owners[0]['key']}-iphone",
        "mission_control_url": "https://<this-pc>.<tailnet>.ts.net:9443",
        "watched_tasks": [f"{org_name} Day Spine (assistant)", f"{org_name} Day Spine - long jobs (assistant)", f"{org_name} Deadman (assistant)",
                          f"{org_name} Comms Probe (assistant)", "example-org", "example-org"],
        "internal_emails": [o["email"] for o in owners if o["email"]],
        "alert_from": owners[0]["email"], "alert_to": [o["email"] for o in owners if o["email"]],
        "phone_send_shortcut": f"{org_name} Send Texts",
        # the phone bridge's own marks (sage-core, 2026-10-28): the SMS phrase that wakes the phone's Shortcut, the token
        # header the phone sends, and the Workspace account that reads the Chat spaces (space_reader.py)
        "phone_nudge_mark": f"{_slug(a.org).upper()}-SEND", "phone_token_header": f"X-{_slug(a.org).title().replace('-', '')}-Token",
        "chat_reader": owners[0]["email"],
        # sage-core (2026-10-27): the machine values the code used to hard-wire to this PC
        "keyring_service": f"{_slug(a.org)}-assistant", "python": "", "semantic_python": "", "mission_control_port": 9019,
        "secrets_dir": f"~/.sage/{_slug(a.org)}", "google_home": f"~/.sage/{_slug(a.org)}/google",
        "modules": list(a.module), "lifecycle": a.lifecycle, "lanes": lanes, "engine": "sage-core",
        # the auto-updater (assistant/updater.py): where release.json lives. SAGE_RELEASE_URL (the installer bakes it in), else
        # the GitHub Releases of the clone this engine is (its git origin), else empty = updates by hand (`sage update --engine`)
        "engine_release_url": os.environ.get("SAGE_RELEASE_URL", "") or _origin_feed(VAULT)}, indent=1, ensure_ascii=False) + "\n", encoding="utf-8")
    # core_sync (2026-10-28): a fresh organization contributes its engine changes to its own sage-core checkout, with its own niche words
    (out / "assistant" / "core_sync.json").write_text(json.dumps({
        "_doc": "core_sync.py: this organization's engine changes flow on to the sage-core checkout at core_root, made organization-free. "
                "niche_terms = words that mark a working line of code as this business's own (held until moved behind config / a module / "
                "a niche path); niche_paths = files the sync promoted to niche; allow_terms = never flag these.",
        "enabled": False, "core_root": "", "scrub_code_comments": True, "niche_policy": "hold", "identity_policy": "hold",
        "file_builds": True, "notify": True, "niche_terms": [], "niche_paths": [], "allow_paths": [], "allow_terms": []},
        indent=1, ensure_ascii=False) + "\n", encoding="utf-8")
    # optional modules start OFF; any job whose script did not ship (niche) is off too, with the reason on the row
    sj_path = out / "assistant" / "spine_jobs.json"
    try:
        sj = json.loads(sj_path.read_text(encoding="utf-8-sig"))
        job_module = {j: m for m, spec in engine.MODULES.items() for j in spec["jobs"]}
        for j in sj.get("jobs", []):
            m = job_module.get(j.get("name"))
            if m and m not in a.module:
                j["enabled"], j["module"] = False, m
            script = next((c for c in j.get("cmd", []) if c.endswith((".py", ".ps1"))), "")
            if script and not os.path.isabs(script) and not (out / script).exists():
                j["enabled"], j["missing"] = False, script
        sj_path.write_text(json.dumps(sj, indent=1, ensure_ascii=False) + "\n", encoding="utf-8")
    except (OSError, ValueError):
        pass
    # the task XMLs and the PowerShell wrappers name this folder and these task names: rewrite both for the new root
    for ps in (out / "assistant" / "scripts").glob("*.ps1"):
        ps.write_text(ps.read_text(encoding="utf-8").replace(str(VAULT), str(out)), encoding="utf-8")
    sid = _user_sid()
    for xml in list((out / "assistant" / "tasks").glob("*.xml")) + list((out / "scripts").glob("*.xml")):
        txt = _read_any(xml).replace(str(VAULT), str(out))
        for rx, rep in infra_pairs:
            txt = rx.sub(rep, txt)
        # the source PC's account SID (or the template's placeholder) becomes the installing user's own (2026-10-29)
        txt = engine.SID_RX.sub(sid or engine.SID_PLACEHOLDER, txt).replace(engine.SID_PLACEHOLDER, sid or engine.SID_PLACEHOLDER)
        xml.write_text(txt, encoding="utf-8")
    (out / "Wiki" / "_meta" / "index.md").write_text("# Wiki index\n", encoding="utf-8")
    agent = out / "assistant" / "agent"
    profile = json.loads((HERE / "agent" / "profile.json").read_text(encoding="utf-8"))
    profile.pop("page_title", None)        # the local page is titled with the new agent's name unless its owners set one
    profile.update({"name": a.agent, "slug": _slug(a.agent), "household": a.org, "home_space": "", "claude_account": "",
                    "read_roots": ["Wiki", "Memory/memory.md", "Context", "assistant/README.md", "assistant/agent/skills"],
                    "extra_protected": ""})
    (agent / "profile.json").write_text(json.dumps(profile, indent=1, ensure_ascii=False) + "\n", encoding="utf-8")
    people = {"_doc": "Who the agent answers. Anyone not listed is never answered.",
              "roles": {"owner": {"can": ["ask", "teach", "note", "schedule", "task", "undo", "approve"]},
                        "staff": {"can": ["ask", "note"]}, "guest": {"can": []}},
              "users": {o["key"]: {"name": o["name"], "role": "owner", "emails": [o["email"]] if o["email"] else [], "chat_user": ""} for o in owners}}
    (agent / "people.json").write_text(json.dumps(people, indent=1, ensure_ascii=False) + "\n", encoding="utf-8")
    shutil.copy2(HERE / "agent" / "templates" / "prompt.generic.md", agent / "prompt.md")
    (agent / "prompt.local.md").write_text("ORGANIZATION NOTES (this organization's own records, lanes and habits; the generic prompt is prompt.md)" + chr(10), encoding="utf-8")
    (agent / "google.json").write_text(json.dumps({"_doc": "Google accounts the agent reads (see GOOGLE_SETUP.md). Fill per owner.",
                                                   "accounts": {}}, indent=1) + "\n", encoding="utf-8")
    org = {"name": a.org, "what_we_do": a.what, "where": "", "hours": "", "tone": "",
           "people": [{"name": o["name"], "role": "owner", "about": o["email"]} for o in owners],
           "lanes": [], "services": [], "duties": [], "never": [], "updated": time.strftime("%Y-%m-%dT%H:%M:%S")}
    (agent / "state" / "org.json").write_text(json.dumps(org, indent=1, ensure_ascii=False) + "\n", encoding="utf-8")
    for f in ("rules.json", "memory.json", "routines.json"):
        (agent / "state" / f).write_text("{}\n", encoding="utf-8")
    reg = json.loads((HERE / "connections.json").read_text(encoding="utf-8"))
    rows = [r for r in reg["connections"] if r["id"] in GENERIC_CONNECTIONS]
    for o in owners:
        rows.append({"id": f"google-{o['key']}", "name": f"Google calendar + mail (read): {o['email'] or o['name']}", "lane": "personal",
                     "mechanism": "Google API over OAuth (gmail.readonly, calendar.events), read by the agent", "credential": f"OAuth token in assistant/data/agent/google/{o['key']}.json",
                     "check": {"type": "cmd_output", "argv": ["python", "assistant/agent/google_api.py", "status", o["key"]], "ok_contains": "ok"},
                     "renew": f"python assistant/agent/google_api.py auth {o['key']}  (the owner signs in and consents)", "owner": o["key"]})
        rows.append({"id": f"mail-{o['key']}", "name": f"Mail: {o['email'] or o['name']}", "lane": "personal", "mechanism": "IMAP read + Drafts append",
                     "credential": f"Credential Manager / imap:{o['key']}", "check": {"type": "imap", "account": o["key"]},
                     "renew": f"python assistant/scripts/setup_creds.py mail {o['key']}", "owner": o["key"]})
    for r in rows:
        r["owner"] = owners[0]["key"] if r.get("owner") in SRC_OWNER_KEYS else r.get("owner", owners[0]["key"])
    (out / "assistant" / "connections.json").write_text(json.dumps({"_doc": reg["_doc"], "connections": rows}, indent=1, ensure_ascii=False) + "\n", encoding="utf-8")
    cfg = json.loads((HERE / "config.json").read_text(encoding="utf-8"))
    cfg["mail"] = {"accounts": [{"key": o["key"], "address": o["email"], "secret": f"imap:{o['key']}", "imap": "imap.gmail.com", "smtp": "smtp.gmail.com"} for o in owners if o["email"]]}
    cfg["calendar"] = {"feeds": []}
    cfg.pop("leads", None)
    (cfg.get("doer") or {}).pop("signin_window", None)   # the sign-in window probe names this organization's CRM
    cfg["plugins"] = {"_doc": cfg.get("plugins", {}).get("_doc", "The organization's own extensions, loaded by name (alib.plugins)."), "modules": []}   # this organization's CRM and texts extensions never ship
    for k in ("honeybook_team", "meetings", "projects", "editor"):
        cfg.pop(k, None)                       # this organization's own integrations and production settings
    cfg["lanes"] = lanes
    keys = [o["key"] for o in owners]
    cfg["push"] = {"_doc": "Where cards and notes go (chat_push.py). buttons:false = cards are answered by words in their thread "
                           "(no public endpoint needed); switch to true once Chat button taps can reach Mission Control.",
                   "fold_multi": True, "cards_to": keys, "notes_to": keys[:1], "lanes_to": {}, "buttons": False}
    if isinstance(cfg.get("texts"), dict):
        cfg["texts"]["enabled"] = False       # maximum gating until the owners switch a standing text on, one by one
    cfg["shadow"] = {"_doc": "Training's shadow week (onboarding.py shadow): every reply, reminder and follow-up is drafted and carded, "
                             "nothing goes out on its own. `onboarding.py golive` lists the standing exceptions to switch on by name.",
                     "on": True, "since": time.strftime("%Y-%m-%d")}
    cfg["habits"] = {"_doc": "habits.py: an ask class that succeeded verified N times becomes a build request to codify it.", "codify": False, "min_verified": 3}
    (out / "assistant" / "config.json").write_text(json.dumps(cfg, indent=1, ensure_ascii=False) + "\n", encoding="utf-8")
    # tracking.json (assistant/TRACKING_DESIGN.md): the cadences carry over; the two detectors that read example's own
    # record sources (the delivery ledger + editor, the HoneyBook snapshot) start OFF until the new org names theirs.
    trk = json.loads((HERE / "tracking.json").read_text(encoding="utf-8"))
    for k in ("ledger_stage", "crm_stage"):
        trk["drift"][k]["enabled"] = False
    trk["chase"]["lane_owner"] = {}
    trk["importance"]["lane"] = {}
    (out / "assistant" / "tracking.json").write_text(json.dumps(trk, indent=1, ensure_ascii=False) + "\n", encoding="utf-8")
    # chains.json (ruling 4): the four chains carry over as declared defaults; the new org edits the gates and the
    # evidence words to its own lifecycle (nothing wedding-shaped lives in code)
    (out / "assistant" / "chains.json").write_text((HERE / "chains.json").read_text(encoding="utf-8"), encoding="utf-8")
    # mining.json (rulings 3 + 13): the date / commitment / money-sender vocabulary and the personal-text exception words
    # carry over as they are; the scheduling templates in tracking.json get the new org's sign-off instead of example's
    (out / "assistant" / "mining.json").write_text((HERE / "mining.json").read_text(encoding="utf-8"), encoding="utf-8")
    sched = trk.get("scheduling") or {}
    for k, v in list((sched.get("templates") or {}).items()):
        if isinstance(v, str):
            for rx, rep in infra_pairs:
                v = rx.sub(rep, v)
            sched["templates"][k] = v
    trk["scheduling"] = sched
    (out / "assistant" / "tracking.json").write_text(json.dumps(trk, indent=1, ensure_ascii=False) + "\n", encoding="utf-8")
    # nothing of this organization in the new one's data, prompts or docs (code comments keep their history): one
    # case-insensitive pass over every .json / .md / .ps1 / .xml under the new assistant/ (WP5 boot test checks it)
    scrub, infra = scrub_pairs, infra_pairs
    for f in list((out / "assistant").rglob("*.ps1")) + list((out / "assistant").rglob("*.xml")):
        txt = _read_any(f)
        new = txt
        for rx, rep in infra:            # the organization's names only; never the owner's name (the PC's user path carries it)
            new = rx.sub(rep, new)
        if new != txt:
            f.write_text(new, encoding="utf-8")
    for f in (out / "assistant").rglob("*"):
        if f.suffix.lower() not in (".json", ".md") or not f.is_file() or f.name == "org.json":
            continue          # the .ps1 / .xml files were rewritten above (their paths may contain the PC's user name)
        txt = _read_any(f)
        new = txt
        for rx, rep in scrub:
            new = rx.sub(rep, new)
        if os.environ.get("NEW_ORG_DEBUG"):
            print(f"SCRUB {f.relative_to(out)} changed={new != txt}", file=sys.stderr)
        if new != txt:
            f.write_text(new, encoding="utf-8")
    # the manifest records each file as WRITTEN here (after the scrub above), so `sage update` sees a file nobody touched
    # since the install as "unchanged by hand" and replaces it; and the engine's own version from the export's manifest
    # (an exported engine has no git, so _source_version alone would drop the sha and the updater would call the first
    # release "different") (2026-10-28)
    manifest_files = {rel: (engine._org_sha(out, rel) or sha_) for rel, sha_ in manifest_files.items()}
    man_path = engine.write_manifest(out, manifest_files, VAULT, modules=a.module)
    try:
        src_man = json.loads((VAULT / engine.MANIFEST_NAME).read_text(encoding="utf-8-sig"))
        ver = str((src_man.get("engine") or {}).get("version") or "")
        if ver:
            man = json.loads(man_path.read_text(encoding="utf-8"))
            man["engine"]["version"] = ver
            man_path.write_text(json.dumps(man, indent=1, ensure_ascii=False) + "\n", encoding="utf-8")
    except (OSError, ValueError, KeyError):
        pass
    checklist = [
        f"# {a.org} - assistant setup", "",
        f"Agent: **{a.agent}**. Owners: {', '.join(o['name'] for o in owners)}. Created {time.strftime('%Y-%m-%d')} from the sage-core engine (`assistant/new_org.py`; "
        f"engine files listed in `engine_manifest.json`, updated with `python assistant/sage.py update`).", "",
        "## First hour (the owners, on their PC)", "",
        "1. `python assistant/sage.py doctor` says what this machine still needs (Python packages, Playwright, the Claude CLI and its token, gcloud). "
        "The token: `claude setup-token` once, stored as the User env var `CLAUDE_CODE_OAUTH_TOKEN`; the agent's model turns run on it.",
        "2. `python assistant/sage.py serve` runs everything (the spine, the probe, the operator, the deadman, Mission Control) in one process; "
        "`python assistant/sage.py install-startup` makes it start at logon. Mission Control is http://127.0.0.1:9019 on this PC.",
        "3. Open **/setup** and work down **Needs attention**: one button per connection. Google (calendar + mail) per owner, mail app passwords, the agent's browser signed in to each site.",
        "4. Google Chat for the agent: follow `assistant/agent/GOOGLE_SETUP.md` (needs Google Workspace). Until then the phone chat page (/chat) is the conversation.",
        "5. Discovery and Training: `python assistant/onboarding.py discover`, then `train` until coverage is 100%, then the shadow week; `golive` switches each standing exception on by name.",
        "6. Say hello in chat. The agent asks what it still needs to know (name, what you do, who is who, lanes, services, duties, never-list, hours), one question at a time, and remembers.", "",
        "## What is NOT copied", "",
        "No credentials, cookies, tokens, rules, memories, wiki notes or chat history from the source organization. Everything is learned here.", "",
        "## Where things live", "",
        "`assistant/agent/state/org.json` what the owners taught about the business · `assistant/connections.json` the connection registry · `Wiki/` compiled knowledge · `Raw/Uploads/` files handed over in chat · `assistant/agent/prompt.md` the agent's instructions (generic template).",
    ]
    (out / "README.md").write_text("\n".join(checklist) + "\n", encoding="utf-8")
    print(f"copied {n} files\n\n" + "\n".join(checklist))
    return 0


if __name__ == "__main__":
    sys.exit(main())

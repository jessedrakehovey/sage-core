"""engine.py - the engine / org-pack contract (sage-core, 2026-10-27).

the owner (2026-10-27): "a copy of what Sage is and how it works but not org specific, and remove any features
that are specifically niche to my org or system." Until this file the only way to a second organization was
`new_org.py`, which copied this folder: a fork, so a fix in brain.py would have to be hand-applied to every
organization. This module draws the line once and both `new_org.py` and `sage update` read it:

  ENGINE   the code and the generic templates: everything under assistant/ (the agent, deeds, cards, the
           executor, the doer, the builder, dreaming, reflex, capability gaps, voice, outcomes, the digest,
           the spine, Mission Control, learnings), the knowledge tooling under scripts/ (wiki, semantic search,
           dreaming cron, graphify, guardrails, monitoring, evals, skill radar, drive ingest), Schema/, the
           generic Claude skills. Owned by the engine: `sage update` replaces these files.
  PACK     everything the organization owns: data/, out/, learnings/, agent/state/, agent/skills/, the
           profile / people / prompt / google / org / config / connections / tracking / chains / mining /
           spine_jobs files, Wiki/, Memory/, Raw/, Context/, Schema/mistakes.md, the task XMLs. An update
           never touches a pack path. The engine carries each pack file once, as the TEMPLATE `new_org.py`
           fills; in the organization's folder the same path is the organization's.
  MODULE   optional engine parts an organization switches on: `money` (the personal-finance lane), `typeform`
           (the lead sensor). Their files ship; their spine jobs start off.
  NICHE    never exported: the wedding lifecycle skills, the HoneyBook scripts (the CRM is a catalog choice the
           owner makes in Discovery, never engine code), the Resolve editor, card ingest, ads, lots, travel,
           taxes, rentals, shoot-day, the owners' phone setup notes, planning history.

The engine's root path is written as {{ORG_ROOT}} in exported text files; `new_org.py` and `sage update`
substitute the organization's folder. The manifest (`engine_manifest.json`) lists every engine file with
its sha256 so an update knows which files the organization changed by hand (those are reported, never
overwritten without --force).

  python assistant/engine.py export <dst> [--git]       # a clean engine tree (sage-core) from this folder
  python assistant/engine.py manifest [<root>]          # print the manifest an export of <root> would carry
  python assistant/engine.py update <org_root> --engine <engine_root> [--force] [--dry-run]
  python assistant/engine.py is-pack <relpath>
"""
from __future__ import annotations

import argparse
import hashlib
import io
import json
import os
import re
import shutil
import subprocess
import sys
import time
import tokenize
from pathlib import Path

HERE = Path(__file__).resolve().parent          # assistant/
VAULT = HERE.parent
ROOT_PLACEHOLDER = "{{ORG_ROOT}}"
MANIFEST_NAME = "engine_manifest.json"
TEXT_SUFFIXES = (".ps1", ".xml", ".vbs", ".cmd", ".bat", ".md", ".json", ".txt", ".yaml", ".yml", ".html", ".css", ".js", ".py")

# ---------------------------------------------------------------- what ships (relative to the vault root)
ENGINE_ROOTS = ("assistant", "scripts", "Schema", ".claude")
SKIP_DIRS = {"__pycache__", ".git", "node_modules", ".pytest_cache", ".semantic_store", ".cache", "graphify-out"}
# inside assistant/: the organization's own state never travels (new_org creates empty ones)
SKIP_ASSISTANT_DIRS = {"data", "out", "learnings", "gchat_relay", "tools"}
SKIP_ASSISTANT_AGENT_DIRS = {"state", "skills"}
SKIP_FILES = {"gchat.local.json", "google.json", "index.html"}          # plus any *_IPHONE_SETUP.md (an owner's phone notes)
SKIP_SUFFIXES = (".jsonl", ".log", ".kdbx", ".png", ".jpg", ".jpeg", ".gif", ".mp4", ".wav", ".pyc", ".tmp")
SKIP_PLANNING_PREFIXES = ("BUILD_PLAN", "BUILD_PROPOSAL", "PLAN-", "TRACKING_DESIGN", "SAGE2_PLAN", "SAGE_CORE_PLAN")
DATA_TEMPLATES = ("sop_map.json", "comms_law.json", "client_texts.json")     # new_org.py reads the same three
# niche to this organization or this machine: never exported
NICHE_PATHS = {
    "scripts/card_ingest", "scripts/music_license", "scripts/projects", "scripts/yt_ingest", "scripts/yt_scout",
    "scripts/omi_ingest", "scripts/transcribe.py", "scripts/yt_ingest_cron.ps1", "scripts/yt_ingest_cron.xml",
    # the owner (2026-10-27): "no need for a HoneyBook connector module, that can be set by the user if desired":
    # the CRM is a catalog entry an organization picks in Discovery, never engine code
    "scripts/honeybook", "scripts/guardrails/honeybook_gate.py", "assistant/mcp_honeybook.json",
    "assistant/scripts/hb_exec.py", "assistant/scripts/hb_snapshot.py", "assistant/scripts/collection_autosend.py",
    "assistant/scripts/team_on_projects.py", "assistant/scripts/meeting_guests.py", "assistant/scripts/lead_meetings.py",
    "assistant/scripts/sense_honeybook.py",
    "assistant/scripts/sense_editor.py",
    # this organization's Drive accounts and mail home are wired into the hourly Drive ingest (2026-10-29): its own path
    "scripts/drive_ingest",
    "Schema/mistakes.md",
    # the wiki contract is the organization's (its types, tags, naming and routing are its business's); the engine ships
    # the GENERIC one under assistant/templates/Schema and new_org.py installs that (2026-10-29)
    "Schema/schema.md", "Schema/about.md", "Schema/templates", "Schema/skills",
    "Schema/templates/couple-profile.md", "Schema/templates/shoot-call-sheet.md", "Schema/templates/delivery-note.md",
    "Schema/templates/venue-profile.md", "Schema/templates/package-profile.md",
    ".claude/settings.local.json",
}
NICHE_SKILLS = {"ad-scout", "booking-confirmation", "card-ingest", "dashboard", "delivery-tracker", "editor-handoff",
                "inquiry-reply", "lot-scout", "music-license-check", "shoot-prep", "tax-return", "travel-scout",
                "wedding-edit", "youtube-scout", "fill-timesheet"}
# optional modules: files ship with the engine, their jobs start OFF in a new organization
MODULES = {
    "money": {"what": "personal-finance lane: read-only bank feeds (Plaid / SimpleFIN), categorising, forecasts; never pays",
              "files": ["assistant/scripts/money", "assistant/money.json", "assistant/money_rules.json"],
              "jobs": ["money-pull", "money-pull-plaid", "money-categorize", "money-forecast", "money-quarterly", "money-monthly"]},
    "typeform": {"what": "Typeform lead sensor", "files": ["assistant/scripts/sense_typeform.py"], "jobs": ["sense-typeform"]},
}
# the organization's own files (relative to the org root): an update never touches these
PACK_PREFIXES = ("assistant/data/", "assistant/out/", "assistant/learnings/", "assistant/agent/state/", "assistant/agent/skills/",
                 "assistant/tasks/", "assistant/state/", "Wiki/", "Memory/", "Raw/", "Context/", "Schema/templates/")
# pack files that are pure organization data, never a template: new_org.py writes a fresh one (2026-10-29: the scrubbed
# copy of one organization's notes still carried its people)
PACK_NEVER_TEMPLATE = {"assistant/agent/prompt.local.md", "assistant/core_sync.json"}     # core_sync.json lists the people it scrubs
PACK_FILES = {"assistant/agent/profile.json", "assistant/agent/people.json", "assistant/agent/prompt.md", "assistant/agent/prompt.local.md",
              "assistant/agent/google.json", "assistant/agent/gchat.local.json", "assistant/org.json", "assistant/config.json",
              "assistant/connections.json", "assistant/tracking.json", "assistant/chains.json", "assistant/mining.json",
              "assistant/spine_jobs.json", "assistant/jobs.json", "assistant/money.json", "assistant/money_rules.json",
              "assistant/mcp_honeybook.json", "assistant/switches.json", "assistant/agent/switches.json", "assistant/core_sync.json",
              "Schema/mistakes.md", "Schema/schema.md", "Schema/about.md", "README.md", "CLAUDE.md", "HANDOFF.md", ".claude/settings.local.json", MANIFEST_NAME}
# engine files an organization's core_sync.py promoted to niche (assistant/core_sync.json -> niche_paths): a new file the
# builder wrote that is only about that organization never ships. core_sync sets this; otherwise read once from the vault.
EXTRA_NICHE: set | None = None
# the organization's names and hosts, scrubbed from engine docs and wrappers (never from code lines)
# ---------------------------------------------------------------- the organization's identity, read from the pack
# (2026-10-28, the owner: "any mention of the owner or the business must not be in sage-core"): the names, addresses, hosts
# and words that identify THIS organization come from org.json and people.json, never from literals here, and an
# export scrubs every line - prose readably ("the owner", "the organization"), working code with identifier-safe
# tokens (owner1, family1, example-org, owner1@example.invalid) so the template still compiles and stays consistent
# across files. A new organization's values are substituted by new_org.py.
PUBLIC_MAIL = ("gmail.com", "outlook.com", "yahoo.com", "icloud.com", "hotmail.com", "me.com", "live.com")

# ---------------------------------------------------------------- zero personal data (the owner, 2026-10-28: "sage-core should have
# zero personal data in it"). Beyond the organization's own identity, an export carries NO address, phone number or named
# third party: every e-mail address that is not an obvious placeholder becomes a stable p<hash>@example.invalid (the same
# address maps to the same token everywhere, so a test that compares a fixture with an output still holds), every phone
# number that is not a 555 number becomes a stable 555-01xx-xxxx of the same shape, and the words in
# assistant/core_sync.json -> identity_terms (places, client and vendor names, other organizations) become "example".
EMAIL_RX = re.compile(r"\b[A-Za-z0-9._%+-]+@(?:[A-Za-z0-9-]+\.)+[A-Za-z]{2,}\b")
EMAIL_KEEP = re.compile(r"(?:@(?:[a-z0-9-]+\.)*(?:example\.(?:invalid|com|org|net)|[a-z0-9-]+\.test|[a-z0-9-]+\.invalid|[a-z0-9-]+\.example|acme\.test|x\.com|y\.com|b\.co|anthropic\.com|"
                        r"googleapis\.com|gserviceaccount\.com|google\.com|github\.com|apple\.com|microsoft\.com)$|^(?:noreply|no-reply|owner\d*|owner\d*@|staff\d*|p[0-9a-f]{6})@)", re.I)
PHONE_RX = re.compile(r"(?<![\w.-])(\+?1[\s.-]?)?(\(?)(\d{3})(\)?[\s.-]?)(\d{3})([\s.-]?)(\d{4})(?![\w-])")
# a Windows account SID in a scheduled-task XML names this PC's user (2026-10-29): the template carries a placeholder,
# new_org.py writes the installing user's own
SID_RX = re.compile(r"S-1-5-21(?:-\d+){3,4}")
SID_PLACEHOLDER = "{{USER_SID}}"
# record ids and private links that point at one organization's accounts (2026-10-29): a 24-hex record id (CRM
# projects, users, scheduler sessions) becomes a stable fake of the same shape, a claude.ai artifact link a placeholder,
# a street address a made-up one
HEXID_RX = re.compile(r"(?<![0-9A-Za-z])[0-9a-f]{24}(?![0-9A-Za-z])")
ARTIFACT_RX = re.compile(r"(claude\.ai/(?:code/)?artifact/)[A-Za-z0-9_-]{8,}")
GDOC_RX = re.compile(r"((?:drive|docs)\.google\.com/(?:file/d/|(?:document|spreadsheets|presentation|forms)/d/|open\?id=|drive/folders/))[A-Za-z0-9_-]{20,}")
GID_RX = re.compile(r"(?<![A-Za-z0-9_-])1(?=[A-Za-z0-9_-]*[a-z])(?=[A-Za-z0-9_-]*[A-Z])[A-Za-z0-9_-]{24,43}(?![A-Za-z0-9_-])")   # a bare Drive/Sheets id
STREET_RX = re.compile(r"\b\d{2,6}\s+[A-Z][a-z]+(?:\s[A-Z][a-z]+)?\s(?:Street|St|Avenue|Ave|Road|Rd|Boulevard|Blvd|Drive|Dr|Lane|Ln|Court|Ct|Place|Pl)\b")
EXTRA_IDENTITY = None
PSEUDONYMS = None


def pseudonyms() -> list:
    """assistant/core_sync.json -> pseudonyms {real: stand-in}: people, places and ids from the organization's own
    records, each replaced by the same made-up stand-in everywhere (a test that compares two names still holds).
    Longest first."""
    global PSEUDONYMS
    if PSEUDONYMS is None:
        try:
            d = json.loads((HERE / "core_sync.json").read_text(encoding="utf-8-sig"))
            m = d.get("pseudonyms") or {}
            PSEUDONYMS = sorted(((str(k), str(v)) for k, v in m.items() if len(str(k)) >= 3), key=lambda kv: len(kv[0]), reverse=True)
        except (OSError, ValueError, AttributeError):
            PSEUDONYMS = []
    return PSEUDONYMS


def extra_identity() -> list:
    """assistant/core_sync.json -> identity_terms: words that identify people, places and other organizations in this
    organization's files (never derivable from org.json). Scrubbed to "example" in prose and code alike."""
    global EXTRA_IDENTITY
    if EXTRA_IDENTITY is None:
        try:
            d = json.loads((HERE / "core_sync.json").read_text(encoding="utf-8-sig"))
            EXTRA_IDENTITY = [str(w) for w in (d.get("identity_terms") or []) if len(str(w)) >= 3]
        except (OSError, ValueError, AttributeError):
            EXTRA_IDENTITY = []
    return EXTRA_IDENTITY


def _tok(s: str, n: int) -> str:
    return hashlib.sha1(s.lower().encode("utf-8")).hexdigest()[:n]


def _email_sub(m) -> str:
    e = m.group(0)
    return e if EMAIL_KEEP.search(e) else f"p{_tok(e, 6)}@example.invalid"


def _phone_sub(m) -> str:
    cc, lp, area, mid, exch, sep2, last = m.groups()
    if area == "555" or exch == "555":
        return m.group(0)
    h = _tok(area + exch + last, 6)
    digits = str(int(h, 16) % 10000).zfill(4)
    return f"{cc or ''}{lp or ''}555{mid or '-'}0{digits[:2]}{sep2 or '-'}{digits[2:]}{digits[:2]}"      # 555-0xx-xxxx: nobody's number


def pii_scrub(text: str) -> str:
    """Addresses and phone numbers that are nobody's: the same input always gives the same token."""
    if "@" in text:
        text = EMAIL_RX.sub(_email_sub, text)
    if any(ch.isdigit() for ch in text):
        text = PHONE_RX.sub(_phone_sub, text)
    if "S-1-5-21" in text:
        text = SID_RX.sub(SID_PLACEHOLDER, text)
    text = HEXID_RX.sub(lambda m: _tok(m.group(0), 24), text)
    if "google.com" in text:
        text = GDOC_RX.sub(r"\1EXAMPLE-FILE-ID", text)
    text = GID_RX.sub(lambda m: "1EXAMPLE" + _tok(m.group(0), 16), text)
    if "artifact/" in text:
        text = ARTIFACT_RX.sub(r"\1EXAMPLE-ARTIFACT", text)
    text = STREET_RX.sub("123 Example Street", text)
    return text


def _loadj(p: Path) -> dict:
    try:
        d = json.loads(Path(p).read_text(encoding="utf-8-sig"))
        return d if isinstance(d, dict) else {}
    except (OSError, ValueError):
        return {}


def org_values(root: Path | None = None) -> dict:
    """What identifies the organization whose pack lives at root (default: this vault): org.json + people.json."""
    a = (Path(root) / "assistant") if root else HERE
    org, people = _loadj(a / "org.json"), _loadj(a / "agent" / "people.json")
    names, emails, hosts, words, surnames, persons = set(), set(), set(), set(), set(), []
    for k in ("name", "vault_name", "phone_send_shortcut"):
        if org.get(k):
            names.add(str(org[k]))
    for t in org.get("watched_tasks") or []:
        names.add(str(t))
    for k in ("send_identities", "internal_emails", "alert_to"):
        for e in org.get(k) or []:
            emails.add(str(e).lower())
    if org.get("alert_from"):
        emails.add(str(org["alert_from"]).lower())
    for k in (org.get("phone_accounts") or {}):
        if len(str(k)) >= 3:
            words.add(str(k))
    m = re.match(r"https?://([^/:]+)", str(org.get("mission_control_url") or ""))
    if m:
        hosts.add(m.group(1).lower())
    for key, u in (people.get("users") or {}).items():
        nm, role = str(u.get("name") or "").strip(), str(u.get("role") or "owner")
        pe = [str(e).lower() for e in (u.get("emails") or []) if e]
        emails.update(pe)
        parts = nm.split()
        if len(parts) > 1 and len(parts[-1]) >= 3:
            surnames.add(parts[-1])
        pw = {w for w in ([parts[0]] if parts else []) + [str(key)] if len(w) >= 3}
        persons.append({"key": str(key), "name": nm, "role": role, "words": pw})
    domains = {e.split("@", 1)[1] for e in emails if "@" in e and not e.endswith(PUBLIC_MAIL)}
    locals_ = {e.split("@", 1)[0]: e for e in emails if "@" in e and len(e.split("@", 1)[0]) >= 6}
    stems = {d.split(".", 1)[0] for d in domains if len(d.split(".", 1)[0]) >= 5}
    try:
        import socket
        hosts.add(socket.gethostname().lower())
    except OSError:
        pass
    hostparts = {part for h in hosts for part in h.split(".") if len(part) >= 6 and part not in ("ts", "net", "com", "local")}
    words |= set(extra_identity())          # places, clients, vendors, other organizations (core_sync.json identity_terms)
    return {"names": names, "emails": emails, "domains": domains, "hosts": hosts, "hostparts": hostparts, "words": words,
            "surnames": surnames, "persons": persons, "locals": locals_, "stems": stems, "root": str(root or VAULT)}


def _rx_word(w: str):
    r"""A whole word, case-insensitive: letters and digits bound it (an underscore does not: `mcp__org-google` and
    `owner_request` carry the organization's word and are renamed consistently everywhere). A JSON-escaped line break or
    tab right before it (`\nOwner`) is a boundary too: the `n` of `\n` is a letter, and the standing texts carry their
    sign-off exactly like that."""
    return re.compile(r"(?:(?<![A-Za-z0-9])|(?<=\\[ntr]))" + re.escape(w) + r"(?![A-Za-z0-9])", re.I)


def _build_scrubs(root: Path | None = None) -> tuple:
    """(INFRA, ORG, OWNER, CODE): readable pairs for prose (the first three, applied in that order) and
    identifier-safe pairs for working lines of code (the fourth). Longest match first inside each group."""
    v = org_values(root)
    infra, org, owner, code = [], [], [], []
    for real, fake in pseudonyms():         # first, so a full name goes whole before its parts meet the other pairs
        rx = _rx_word(real) if re.match(r"^[\w .'&-]+$", real) else re.compile(re.escape(real))
        infra.append((rx, fake))
        code.append((rx, fake))
    emails = sorted(v["emails"])
    idx = {e: i + 1 for i, e in enumerate(emails)}
    for e in sorted(emails, key=len, reverse=True):
        rx = re.compile(re.escape(e), re.I)
        infra.append((rx, f"owner{idx[e]}@example.invalid"))
        code.append((rx, f"owner{idx[e]}@example.invalid"))
    for h in sorted(v["hosts"], key=len, reverse=True):
        rx = re.compile(re.escape(h), re.I)
        infra.append((rx, "<this-pc>.<tailnet>.ts.net" if "." in h else "<this-pc>"))
        code.append((rx, "this-pc.tailnet.ts.net" if "." in h else "this-pc"))
    for part in sorted(v["hostparts"], key=len, reverse=True):
        infra.append((_rx_word(part), "tailnet"))
        code.append((_rx_word(part), "tailnet"))
    for d in sorted(v["domains"], key=len, reverse=True):
        infra.append((re.compile(re.escape(d), re.I), "example.invalid"))
        code.append((re.compile(re.escape(d), re.I), "example.invalid"))
    for s in sorted(v["stems"], key=len, reverse=True):
        infra.append((_rx_word(s), "example"))
        code.append((_rx_word(s), "example"))
    for loc, e in sorted(v["locals"].items(), key=lambda kv: len(kv[0]), reverse=True):
        infra.append((re.compile(re.escape(loc), re.I), "owner"))
        code.append((re.compile(re.escape(loc), re.I), f"owner{idx.get(e, 1)}"))
    for n in sorted(v["names"], key=len, reverse=True):
        org.append((_rx_word(n), "the organization"))
        code.append((_rx_word(n), "example-org"))
    for j, s in enumerate(sorted(v["surnames"], key=len, reverse=True), 1):
        org.append((_rx_word(s), "the organization"))
        code.append((_rx_word(s), f"family{j}"))
    oi = si = 0
    for p in v["persons"]:
        if p["role"] == "owner":
            oi += 1
            who, tok = "the owner", f"owner{oi}"
        else:
            si += 1
            who, tok = "a team member", f"staff{si}"
        for w in sorted({p["name"]} | p["words"], key=len, reverse=True):
            if len(w) < 3:
                continue
            owner.append((_rx_word(w + "'s"), who + "'s"))
            owner.append((_rx_word(w), who))
            code.append((_rx_word(w), tok))
    for w in sorted(extra_identity(), key=len, reverse=True):
        org.append((_rx_word(w), "example"))
        code.append((_rx_word(w), "example"))
    for form in _root_forms(Path(v["root"])):
        code.insert(0, (re.compile(re.escape(form), re.I), ROOT_PLACEHOLDER))
    return infra, org, owner, code


# the organization's own extensions (config.json -> plugins.modules, niche files) never ship: the template lists none
PLUGINS_OFF = re.compile(r'("plugins"\s*:\s*\{[^{}]*?"modules"\s*:\s*)\[[^\]]*\]', re.S)


# ---------------------------------------------------------------- classification
def _rel(p: Path, root: Path) -> str:
    return p.relative_to(root).as_posix()


def is_pack(rel: str) -> bool:
    rel = rel.replace("\\", "/").lstrip("./")
    return rel in PACK_FILES or any(rel.startswith(pfx) for pfx in PACK_PREFIXES)


def extra_niche() -> set:
    global EXTRA_NICHE
    if EXTRA_NICHE is None:
        try:
            d = json.loads((HERE / "core_sync.json").read_text(encoding="utf-8-sig"))
            EXTRA_NICHE = {str(p).replace("\\", "/") for p in (d.get("niche_paths") or [])}
        except (OSError, ValueError, AttributeError):
            EXTRA_NICHE = set()
    return EXTRA_NICHE


def is_niche(rel: str) -> bool:
    rel = rel.replace("\\", "/")
    if rel in NICHE_PATHS or any(rel.startswith(n + "/") for n in NICHE_PATHS):
        return True
    if rel in extra_niche() or any(rel.startswith(n + "/") for n in extra_niche()):
        return True
    parts = rel.split("/")
    if len(parts) >= 3 and parts[0] == ".claude" and parts[1] == "skills" and parts[2] in NICHE_SKILLS:
        return True
    return False


def module_of(rel: str) -> str:
    rel = rel.replace("\\", "/")
    for name, m in MODULES.items():
        for f in m["files"]:
            if rel == f or rel.startswith(f + "/"):
                return name
    return ""


def ships(rel: str, with_tests: bool) -> bool:
    """Does this engine-root file travel in an export (and in a new organization's copy)?"""
    rel = rel.replace("\\", "/")
    parts = rel.split("/")
    name = parts[-1]
    if parts[0] not in ENGINE_ROOTS:
        return False            # a root file (HANDOFF.md, the organization's own notes) is never engine
    if rel in PACK_NEVER_TEMPLATE or rel.startswith("assistant/state/"):
        return False            # the organization's own data (2026-10-29): never in an export, a manifest or a release
    if any(part in SKIP_DIRS for part in parts) or name in SKIP_FILES or name.endswith(SKIP_SUFFIXES) or name.endswith((".local.json", "_IPHONE_SETUP.md")):
        return False
    if name.endswith(".md") and name.startswith(SKIP_PLANNING_PREFIXES):
        return False
    if is_niche(rel):
        return False
    if parts[0] == "assistant":
        if len(parts) > 1 and parts[1] in SKIP_ASSISTANT_DIRS:
            return False
        if len(parts) > 2 and parts[1] == "agent" and parts[2] in SKIP_ASSISTANT_AGENT_DIRS:
            return False
        if len(parts) > 1 and parts[1] == "tests" and not with_tests:
            return False
        if len(parts) > 1 and parts[1] == "web":
            return False            # built by dashboard.py at runtime
    return True


def engine_files(root: Path, with_tests: bool = True) -> list:
    out = []
    for top in ENGINE_ROOTS:
        base = root / top
        if not base.exists():
            continue
        for p in base.rglob("*"):
            if p.is_file() and ships(_rel(p, root), with_tests):
                out.append(_rel(p, root))
    return sorted(out)


# ---------------------------------------------------------------- text handling
def read_text(f: Path) -> str:
    b = f.read_bytes()
    if b[:2] in (b"\xff\xfe", b"\xfe\xff"):
        return b.decode("utf-16")
    return b.decode("utf-8-sig", errors="replace")


def is_text(rel: str) -> bool:
    """Files whose root path is templated and substituted. Code is NOT one of them: every .py resolves its paths
    from __file__, and a Windows path written into a Python string literal is a broken escape."""
    return rel.lower().endswith(TEXT_SUFFIXES) and not rel.lower().endswith(".py")


def _root_forms(root: Path) -> list:
    s = str(root)
    forms = [s, s.replace("\\", "/"), s.replace("\\", "\\\\")]
    return sorted(set(forms), key=len, reverse=True)


# built once the helpers above exist (_root_forms is used by the code pairs)
INFRA_SCRUB, ORG_SCRUB, OWNER_SCRUB, CODE_SCRUB = _build_scrubs()


def to_template(text: str, root: Path) -> str:
    for form in _root_forms(root):
        text = text.replace(form, ROOT_PLACEHOLDER)
    return text


def from_template(text: str, root: Path, rel: str) -> str:
    s = str(root)
    if rel.endswith(".json"):
        s = s.replace("\\", "\\\\")          # a JSON string escapes its backslashes
    return text.replace(ROOT_PLACEHOLDER, s)


def scrub(text: str, rel: str) -> str:
    """PROSE scrub, readable: no host, domain, address, organization name or owner of the source organization. For a
    .json rel only the infra pairs apply (keys and matchers keep their words; export_text handles a whole JSON file
    line by line, working lines with the code-safe pairs)."""
    low = rel.lower()
    for rx, rep in INFRA_SCRUB:
        text = rx.sub(rep, text)
    if low.endswith(".json"):
        if rel.replace("\\", "/").endswith("assistant/config.json"):
            text = PLUGINS_OFF.sub(r"\1[]", text)
        return pii_scrub(text)
    for rx, rep in ORG_SCRUB:
        text = rx.sub(rep, text)
    for rx, rep in OWNER_SCRUB:
        text = rx.sub(rep, text)
    return pii_scrub(text)


def scrub_code(text: str) -> str:
    """WORKING-LINE scrub, identifier-safe: the same identity, as tokens code can carry (owner1, family1,
    example-org, owner1@example.invalid, this-pc, {{ORG_ROOT}})."""
    for rx, rep in CODE_SCRUB:
        text = rx.sub(rep, text)
    return pii_scrub(text)


# ---------------------------------------------------------------- working lines vs prose
_SKIP_TOKENS = (tokenize.NL, tokenize.INDENT, tokenize.DEDENT, tokenize.ENCODING, tokenize.ENDMARKER)
PROSE_SUFFIXES = (".md", ".txt", ".xml", ".yaml", ".yml", ".base", ".canvas")
_JSON_PROSE_KEY = re.compile(r'^\s*"_[A-Za-z0-9_]*"\s*:')
_LINE_COMMENT = {".ps1": re.compile(r"^\s*#"), ".sh": re.compile(r"^\s*#"), ".cmd": re.compile(r"^\s*(?:rem\b|::)", re.I),
                 ".bat": re.compile(r"^\s*(?:rem\b|::)", re.I), ".vbs": re.compile(r"^\s*'"), ".js": re.compile(r"^\s*(?://|/?\*)"),
                 ".gs": re.compile(r"^\s*(?://|/?\*)"), ".css": re.compile(r"^\s*/\*.*\*/\s*$"), ".html": re.compile(r"^\s*<!--.*-->\s*$")}


def py_prose_spans(text: str) -> set:
    """Line numbers (1-based) that are comments or docstrings only: a logical line that is nothing but one string
    literal (a docstring) or a comment. A line with code and a trailing comment is code."""
    try:
        toks = list(tokenize.generate_tokens(io.StringIO(text).readline))
    except (tokenize.TokenError, SyntaxError, IndentationError):
        return set()
    prose, code_lines, stmt = set(), set(), []

    def flush():
        if len(stmt) == 1 and stmt[0].type == tokenize.STRING:
            prose.update(range(stmt[0].start[0], stmt[0].end[0] + 1))
        else:
            for t in stmt:
                code_lines.update(range(t.start[0], t.end[0] + 1))
        stmt.clear()

    for t in toks:
        if t.type == tokenize.COMMENT:
            prose.add(t.start[0])
        elif t.type == tokenize.NEWLINE:
            flush()
        elif t.type not in _SKIP_TOKENS:
            stmt.append(t)
    flush()
    return prose - code_lines


def py_trailing(text: str, prose: set) -> dict:
    """{line_no: column} of a comment that trails code on the same line."""
    try:
        toks = [t for t in tokenize.generate_tokens(io.StringIO(text).readline) if t.type == tokenize.COMMENT]
    except (tokenize.TokenError, SyntaxError, IndentationError):
        return {}
    return {t.start[0]: t.start[1] for t in toks if t.start[0] not in prose}


def working_lines(rel: str, text: str) -> dict:
    """{line_no: text} of the lines that DO something (code without its trailing comment, JSON values that are not
    _doc fields). Prose files -> {}."""
    low = rel.lower()
    lines = text.splitlines()
    if low.endswith(PROSE_SUFFIXES):
        return {}
    if low.endswith(".py"):
        prose = py_prose_spans(text)
        trailing = py_trailing(text, prose)
        return {i: (ln[:trailing[i]] if i in trailing else ln) for i, ln in enumerate(lines, 1) if i not in prose and ln.strip()}
    if low.endswith(".json"):
        return {i: ln for i, ln in enumerate(lines, 1) if ln.strip() and not _JSON_PROSE_KEY.match(ln)}
    rx = next((r for suf, r in _LINE_COMMENT.items() if low.endswith(suf)), None)
    return {i: ln for i, ln in enumerate(lines, 1) if ln.strip() and not (rx and rx.match(ln))}


def textual(rel: str) -> bool:
    """Files an export rewrites line by line: the templated text kinds plus code the engine used to ship raw."""
    return is_text(rel) or rel.lower().endswith((".py", ".gs"))


def export_text(rel: str, text: str, root: Path = VAULT) -> str:
    """The template form of one text file: every prose line templated and scrubbed readably, every working line
    scrubbed with the code-safe tokens (a .py keeps its paths out of to_template: a Windows path written back into
    a Python literal is a broken escape; the vault path itself becomes {{ORG_ROOT}} through the code pairs)."""
    if not textual(rel):
        return text
    low = rel.lower()
    work = working_lines(rel, text)
    py_like = low.endswith((".py", ".gs"))
    out = []
    for i, ln in enumerate(text.splitlines(keepends=True), 1):
        if i in work:
            code_part = work[i]
            if len(code_part) < len(ln.rstrip("\r\n")) and ln.startswith(code_part):
                out.append(scrub_code(code_part) + scrub(ln[len(code_part):], "x.md"))     # code, then its trailing comment
            else:
                out.append(scrub_code(ln))
        else:
            out.append(scrub(ln if py_like else to_template(ln, root), "x.md"))
    txt = "".join(out)
    if rel.replace("\\", "/").endswith("assistant/config.json"):
        txt = PLUGINS_OFF.sub(r"\1[]", txt)
    return txt


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _source_version(root: Path) -> str:
    try:
        r = subprocess.run(["git", "rev-parse", "--short", "HEAD"], cwd=str(root), capture_output=True, text=True, timeout=10)
        short = (r.stdout or "").strip() if r.returncode == 0 else ""
    except (OSError, subprocess.TimeoutExpired):
        short = ""
    return time.strftime("%Y.%m.%d") + (f"+{short}" if short else "")


# ---------------------------------------------------------------- export
def export_file(src_root: Path, rel: str, dst_root: Path, with_scrub: bool = True) -> bytes:
    """Copy one engine file into dst as a TEMPLATE (root -> placeholder, names scrubbed). Returns the bytes written."""
    src = src_root / rel
    dst = dst_root / rel
    dst.parent.mkdir(parents=True, exist_ok=True)
    if with_scrub and textual(rel):
        data = export_text(rel, read_text(src), src_root).encode("utf-8")
        dst.write_bytes(data)
    elif is_text(rel):
        data = to_template(read_text(src), src_root).encode("utf-8")
        dst.write_bytes(data)
    else:
        data = src.read_bytes()
        dst.write_bytes(data)
    try:
        shutil.copystat(src, dst)
    except OSError:
        pass
    return data


def export(dst: Path, src_root: Path = VAULT, with_tests: bool = True, git: bool = False) -> dict:
    """A clean engine tree at dst (sage-core). Nothing of the source organization's data, state or names."""
    dst = Path(dst)
    if dst.resolve() == src_root.resolve() or src_root.resolve() in dst.resolve().parents:
        raise SystemExit("the engine must be exported outside this folder")
    if dst.exists() and any(p for p in dst.iterdir() if p.name != ".git"):
        raise SystemExit(f"{dst} is not empty")
    dst.mkdir(parents=True, exist_ok=True)
    files = {}
    for rel in engine_files(src_root, with_tests):
        files[rel] = sha(export_file(src_root, rel, dst))
    # the three data files a new organization cannot boot without travel as TEMPLATES (new_org.py reads them from
    # assistant/data in this vault and from assistant/templates/data in an exported engine)
    for name in DATA_TEMPLATES:
        src = src_root / "assistant" / "data" / name
        if not src.exists():
            src = src_root / "assistant" / "templates" / "data" / name
        if src.exists():
            rel = f"assistant/templates/data/{name}"
            out = dst / rel
            out.parent.mkdir(parents=True, exist_ok=True)
            txt = export_text(rel, read_text(src), src_root)
            out.write_text(txt, encoding="utf-8")
            files[rel] = sha(txt.encode("utf-8"))
    man = {"engine": {"name": "sage-core", "version": _source_version(src_root), "exported": time.strftime("%Y-%m-%dT%H:%M:%S"),
                      "source": "export", "root_placeholder": ROOT_PLACEHOLDER},
           "modules": {k: {"what": v["what"], "jobs": v["jobs"]} for k, v in MODULES.items()},
           "pack_prefixes": list(PACK_PREFIXES), "pack_files": sorted(PACK_FILES), "files": files}
    (dst / MANIFEST_NAME).write_text(json.dumps(man, indent=1, ensure_ascii=False) + "\n", encoding="utf-8")
    (dst / "README.md").write_text(_engine_readme(man), encoding="utf-8")
    (dst / ".gitignore").write_text("__pycache__/\n*.pyc\n*.log\n*.jsonl\n*.local.json\nassistant/data/\nassistant/out/\nassistant/learnings/\n"
                                    "assistant/agent/state/\n.semantic_store/\n", encoding="utf-8")
    if git:
        for cmd in (["git", "init", "-q"], ["git", "add", "-A"], ["git", "-c", "user.name=sage-core", "-c", "user.email=sage-core@example.invalid",
                                                                  "commit", "-q", "-m", f"sage-core {man['engine']['version']}"]):
            subprocess.run(cmd, cwd=str(dst), capture_output=True, text=True, timeout=120)
    return man


def _engine_readme(man: dict) -> str:
    mods = "\n".join(f"- `{k}`: {v['what']}" for k, v in man["modules"].items())
    return f"""# sage-core {man['engine']['version']}

The engine of a Sage agent: the code, generic prompts, generic skills and templates, with nothing of any
organization in it. An organization is a separate folder (its *pack*) made from this engine by
`python assistant/new_org.py "<Org>" --owner "Name:email" --out <folder>`; the pack is updated to a newer
engine with `python assistant/sage.py update --engine <this folder>` (pack files are never touched; engine
files the organization changed by hand are reported, not overwritten).

Start here: `python assistant/sage.py doctor` (what this machine still needs), then the `sage-setup` skill in
`assistant/setup_skill/` (install it with `python assistant/sage.py install-skill`).

## Installing

Three ways onto a machine, one update path (`assistant/installer/README.md` has the detail):

1. **The installer (Windows, no terminal, no git):** `SageSetup.exe` (built by `assistant/installer/windows/build.ps1`)
   installs a private Python, this engine as a release, the browser and the Claude Code CLI under `%LOCALAPPDATA%\\Sage`,
   then opens the setup page (`assistant/welcome.py`): the organization's name, what to call the agent, the owners; it
   creates the pack, shows the doctor table, opens the one window the owner types the Claude token into, lists the
   Google clicks, starts the agent. Nothing secret passes through the page.
2. **A git clone:** `python assistant/new_org.py "<Org>" --agent <Name> --owner "Full Name:email" --out <folder>`,
   then `python assistant/sage.py doctor` and `serve` in that folder (`sage welcome` opens the same setup page).
3. **The copilot skill** (`/sage-setup` in Claude Code) for a guided install; it ends in the same pack.

Update, whichever way it was installed: `python assistant/sage.py update` (the release feed, `org.json ->
engine_release_url`, gated in a staging folder, pack files never touched; the supervisor runs it nightly),
`sage update --engine <this folder>` from a clone, `sage rollback` for the previous engine. Releases:
`python assistant/release.py build` (the zip + `release.json` into `dist/`).

Optional modules (their jobs start off; `sage module on <name>`):
{mods}

Files: {len(man['files'])}. Manifest: `engine_manifest.json`.
"""


def manifest_of(root: Path, with_tests: bool = True) -> dict:
    """The manifest an export of root would carry (sha of the templated, scrubbed bytes)."""
    files = {}
    for rel in engine_files(root, with_tests):
        src = root / rel
        if textual(rel):
            files[rel] = sha(export_text(rel, read_text(src), root).encode("utf-8"))
        else:
            files[rel] = sha(src.read_bytes())
    return {"engine": {"version": _source_version(root)}, "files": files}


def install_tree(src_root: Path, org_root: Path, with_tests: bool = False, with_scrub: bool = False) -> dict:
    """Every engine file from src_root (this vault or an exported engine) into a NEW organization's folder, with the
    organization's own root substituted. Returns {rel: sha of the templated bytes} for the manifest. new_org.py
    passes with_scrub=False: it scrubs the new tree itself with the new organization's own names."""
    files = {}
    for rel in engine_files(src_root, with_tests):
        src, dst = src_root / rel, org_root / rel
        dst.parent.mkdir(parents=True, exist_ok=True)
        if is_text(rel):
            tmpl = to_template(read_text(src), src_root)
            if with_scrub:
                tmpl = scrub(tmpl, rel)
            files[rel] = sha(tmpl.encode("utf-8"))
            dst.write_text(from_template(tmpl, org_root, rel), encoding="utf-8")
        else:
            data = src.read_bytes()
            files[rel] = sha(data)
            dst.write_bytes(data)
    return files


def write_manifest(org_root: Path, files: dict, src_root: Path, modules: list | None = None) -> Path:
    man = {"engine": {"name": "sage-core", "version": _source_version(src_root), "installed": time.strftime("%Y-%m-%dT%H:%M:%S"),
                      "engine_root": str(src_root), "root_placeholder": ROOT_PLACEHOLDER},
           "modules_on": list(modules or []), "pack_prefixes": list(PACK_PREFIXES), "pack_files": sorted(PACK_FILES), "files": files}
    p = Path(org_root) / MANIFEST_NAME
    p.write_text(json.dumps(man, indent=1, ensure_ascii=False) + "\n", encoding="utf-8")
    return p


# ---------------------------------------------------------------- update
def _org_sha(org_root: Path, rel: str) -> str | None:
    f = org_root / rel
    if not f.exists():
        return None
    if is_text(rel):
        return sha(to_template(read_text(f), org_root).encode("utf-8"))
    return sha(f.read_bytes())


def install_file(engine_root: Path, rel: str, org_root: Path) -> None:
    """Engine template -> the organization's folder, with its own root substituted."""
    src, dst = engine_root / rel, org_root / rel
    dst.parent.mkdir(parents=True, exist_ok=True)
    if is_text(rel):
        dst.write_text(from_template(read_text(src), org_root, rel), encoding="utf-8")
    else:
        shutil.copy2(src, dst)


def update(org_root: Path, engine_root: Path, force: bool = False, dry: bool = False) -> dict:
    org_root, engine_root = Path(org_root).resolve(), Path(engine_root).resolve()
    new = json.loads((engine_root / MANIFEST_NAME).read_text(encoding="utf-8-sig"))
    old_path = org_root / MANIFEST_NAME
    old = json.loads(old_path.read_text(encoding="utf-8-sig")) if old_path.exists() else {"files": {}}
    rep = {"added": [], "replaced": [], "unchanged": [], "conflicts": [], "removed": [], "skipped_pack": [], "forced": []}
    for rel, new_sha in new["files"].items():
        if is_pack(rel):
            rep["skipped_pack"].append(rel)
            continue
        if rel.startswith("assistant/tests/") and not (org_root / "assistant" / "tests").exists():
            continue                                    # a lean organization carries no tests
        cur = _org_sha(org_root, rel)
        was = old["files"].get(rel)
        if cur is None:
            rep["added"].append(rel)
        elif cur == new_sha:
            rep["unchanged"].append(rel)
            continue
        elif was is not None and cur == was:
            rep["replaced"].append(rel)
        elif force:
            rep["forced"].append(rel)
            if not dry:
                shutil.copy2(org_root / rel, str(org_root / rel) + ".pre-update")
        else:
            rep["conflicts"].append(rel)
            continue
        if not dry:
            install_file(engine_root, rel, org_root)
    for rel, was in old["files"].items():
        if rel in new["files"] or is_pack(rel):
            continue
        cur = _org_sha(org_root, rel)
        if cur is not None and (cur == was or force):
            rep["removed"].append(rel)
            if not dry:
                (org_root / rel).unlink()
    if not dry:
        keep = dict(new)
        keep["engine"] = dict(new["engine"], updated=time.strftime("%Y-%m-%dT%H:%M:%S"), engine_root=str(engine_root))
        old_path.write_text(json.dumps(keep, indent=1, ensure_ascii=False) + "\n", encoding="utf-8")
    rep["version"] = new["engine"].get("version", "")
    return rep


def update_summary(rep: dict) -> str:
    s = (f"engine {rep.get('version', '')}: added {len(rep['added'])}, replaced {len(rep['replaced'])}, removed {len(rep['removed'])}, "
         f"unchanged {len(rep['unchanged'])}, pack untouched {len(rep['skipped_pack'])}")
    if rep["forced"]:
        s += f", forced {len(rep['forced'])} (backups *.pre-update)"
    if rep["conflicts"]:
        s += "\nchanged by hand here, left alone (re-run with --force to overwrite, a .pre-update copy is kept):\n  " + "\n  ".join(rep["conflicts"])
    return s


# ---------------------------------------------------------------- cli
def main() -> int:
    sys.stdout.reconfigure(encoding="utf-8")
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("export"); p.add_argument("dst"); p.add_argument("--git", action="store_true"); p.add_argument("--no-tests", action="store_true")
    p = sub.add_parser("manifest"); p.add_argument("root", nargs="?", default=str(VAULT))
    p = sub.add_parser("update"); p.add_argument("org_root"); p.add_argument("--engine", required=True); p.add_argument("--force", action="store_true")
    p.add_argument("--dry-run", action="store_true")
    p = sub.add_parser("is-pack"); p.add_argument("rel")
    sub.add_parser("list")
    a = ap.parse_args()
    if a.cmd == "export":
        man = export(Path(a.dst), with_tests=not a.no_tests, git=a.git)
        print(f"exported {len(man['files'])} files to {a.dst} (engine {man['engine']['version']})")
    elif a.cmd == "manifest":
        print(json.dumps(manifest_of(Path(a.root)), indent=1))
    elif a.cmd == "update":
        print(update_summary(update(Path(a.org_root), Path(a.engine), force=a.force, dry=a.dry_run)))
    elif a.cmd == "is-pack":
        print("pack" if is_pack(a.rel) else ("niche" if is_niche(a.rel) else (f"module:{module_of(a.rel)}" if module_of(a.rel) else "engine")))
    elif a.cmd == "list":
        for rel in engine_files(VAULT):
            m = module_of(rel)
            print(f"{'pack-template' if is_pack(rel) else ('module:' + m if m else 'engine'):16} {rel}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

"""core_sync.py - every change to an agent flows on to its organization-free template, made org-free on the way.

the owner (2026-10-28): "a program that automatically applies any changes to Sage to the sage-core org-agnostic system
by making it not org specific, as long as it is not specifically a change that only applies to my business or life";
then "make it do the same with example and example and example ... a system was already made to sync those, integrate that".

Two agents, two template contracts, ONE sync:

  sage      this folder (assistant/engine.py draws the engine / pack / module / niche line by PATH; `export` writes the
            whole engine once). The template is sage-core: org values come from org.json / people.json at runtime.
  template  the property vault (scripts/rescue/build_template.py: drops property data, swaps the property's identifiers
            for {{TOKENS}}, scrubs residents / vendors / phones / addresses to neutral fakes). The template is
            pm-operator-template: instantiate.py fills the tokens. The sync IMPORTS that builder and uses its rules.

The sync is the continuous half of either: it walks every commit the vault made since the last sync, looks at what
each one changed, and lands the generic part in the template checkout (`core_root`), one template commit per vault
commit with the vault's message. Zero LLM. Nothing of the organization travels:

  * pack and niche paths never travel; a NEW file whose working lines are mostly about this organization is promoted
    to niche on the spot (core_sync.json -> niche_paths, honoured by the engine's own is_niche where it has one);
  * prose is scrubbed (sage: hosts, domains, owners from org.json / people.json, and the comments and docstrings of
    code too; template: the builder's tokens and PII scrub over the whole file, which is that contract);
  * a WORKING line of code that still carries the organization after the scrub - sage: an owner's name or address,
    the business name, this PC's host, the vault path, a phone account, or a niche_terms word; template: a unit
    number, a bare property id, or a niche_terms word - is a change that only applies to this business: the file
    is HELD at its last clean version in the template, and ONE build request is filed (sage: the capability loop ->
    the builder; template: scripts/builder_ops/request.py open) to move that code behind config / a module / a
    niche path. The next sync after the fix releases the hold by itself. Lines already there before the first sync
    are DEBT (reported once, `debt` lists them), never a hold;
  * every exported file must verify (sage: .py compiles, no prose carries a host or address; template: no raw
    identifier or resident name survives), or that file is put back and held with the reason.

Record: <state_dir>/core_sync_state.json (last synced commit, holds, debt), core_sync_ledger.jsonl (one row per file
per commit). A board note (Mission Control; never Chat) when a run applied, held or released something. Switches in
the vault's core_sync.json (sage: assistant/core_sync.json; template: scripts/rescue/core_sync.json).

  python core_sync.py [--vault DIR] [--core DIR] [--engine auto|sage|template] run [--dry-run] [--no-build]
  python core_sync.py ... status | holds | debt | ledger [-n N] | classify <relpath> [--rev REV] | refresh [--dry-run]
  python core_sync.py ... audit            every text file of the template scanned for the organization's identity (exit 1 on a hit)
"""
from __future__ import annotations

import argparse
import hashlib
import importlib.util
import io
import json
import os
import re
import subprocess
import sys
import time
import tokenize
from pathlib import Path

HERE = Path(__file__).resolve().parent          # assistant/ (or sage-core/assistant/)
VAULT = HERE.parent
sys.path.insert(0, str(HERE))
import engine  # noqa: E402  (this folder's engine: the line classification both the export and the flags use)
SENTINEL = "CORE_SYNC"
CODE_SUFFIXES = (".py", ".ps1", ".js", ".gs", ".html", ".css", ".vbs", ".cmd", ".bat", ".sh")
MAX_BUILDS_PER_RUN = 3
GIT_AUTHOR = ["-c", "user.name=core-sync", "-c", "user.email=core-sync@example.invalid"]
EMPTY_TREE = "4b825dc642cb6eb9a060e54bf8d69288fbee4904"

DEFAULT_CONFIG = {
    "enabled": True, "core_root": "", "scrub_code_comments": True, "niche_policy": "hold", "identity_policy": "hold",
    "file_builds": True, "notify": True, "niche_terms": [], "niche_paths": [], "allow_paths": [], "allow_terms": [],
    "flag_units": True, "state_dir": "", "note_cmd": [], "build_cmd": [],
    # the organization's own records whose people must never reach the template (vault_people), what to never count as
    # a person, extra private ids, and the stand-ins an export writes for them (2026-10-29)
    "people_sources": [], "people_allow": [], "private_tokens": [], "pseudonyms": {},
    # the relay between agents (the owner 2026-10-28: "upgrades sync across all these agents as long as they are generally
    # useful"): a generic CODE upgrade that landed in this template is dropped as an organization-free note in the
    # commons folder; every other agent's sync picks it up from there and files one port request with its builder
    "relay": {"enabled": False, "commons": "", "label": "", "max_per_run": 3, "max_diff_kb": 12, "backfill": 0},
}
RELAY_SKIP_SUBJECT = re.compile(r"^\s*(?:agent:\s*(?:nightly state|skill|wiki note)|docs?:|core_sync:|template rebuild|sage template|template template)", re.I)
RELAY_CODE_SUFFIXES = (".py", ".ps1", ".js", ".gs", ".sh", ".cmd", ".bat")

# paths of the run (set by init_paths once the engine is known)
STATE_DIR = HERE / "data"
STATE = STATE_DIR / "core_sync_state.json"
LEDGER = STATE_DIR / "core_sync_ledger.jsonl"
LOG = Path(os.environ.get("WV_LOG_DIR") or (Path.home() / ".claude")) / "core-sync.log"


# ---------------------------------------------------------------- small io
def _log(msg: str) -> None:
    line = time.strftime("%Y-%m-%d %H:%M:%S") + "  " + msg
    print(line, flush=True)
    try:
        LOG.parent.mkdir(parents=True, exist_ok=True)
        with open(LOG, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except OSError:
        pass


def _load(path: Path, default):
    try:
        return json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        return default


def _save(path: Path, obj) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(obj, indent=1, ensure_ascii=False) + "\n", encoding="utf-8")
    for i in range(6):
        try:
            os.replace(tmp, path)
            return
        except PermissionError:
            time.sleep(0.2 * (i + 1))
    os.replace(tmp, path)


def init_paths(state_dir: Path) -> None:
    global STATE_DIR, STATE, LEDGER
    STATE_DIR = Path(state_dir)
    STATE = STATE_DIR / "core_sync_state.json"
    LEDGER = STATE_DIR / "core_sync_ledger.jsonl"


def state() -> dict:
    st = _load(STATE, {}) or {}
    st.setdefault("last_commit", "")
    st.setdefault("baseline_commit", "")
    st.setdefault("holds", {})          # rel -> {"since": sha, "lines": {text: {"why": .., "line": n}}, "noted": bool, "build": key}
    st.setdefault("debt", {})           # rel -> [ {"line": n, "why": .., "text": ..} ] at baseline (reported once)
    st.setdefault("runs", 0)
    st.setdefault("relay_seen", [])     # commons upgrade ids this side has filed (or passed at the relay's baseline)
    st.setdefault("relay_baseline", False)
    return st


def ledger(row: dict) -> None:
    row = dict(row, ts=time.strftime("%Y-%m-%dT%H:%M:%S"))
    LEDGER.parent.mkdir(parents=True, exist_ok=True)
    with open(LEDGER, "a", encoding="utf-8") as f:
        f.write(json.dumps(row, ensure_ascii=False) + "\n")


def git(args: list, cwd: Path, check: bool = False, binary: bool = False):
    kw = {"stdout": subprocess.PIPE, "stderr": subprocess.PIPE} if binary else {"capture_output": True, "text": True, "encoding": "utf-8", "errors": "replace"}
    r = subprocess.run(["git", *args], cwd=str(cwd), timeout=600, **kw)
    if check and r.returncode != 0:
        err = r.stderr.decode("utf-8", "replace") if binary else r.stderr
        raise RuntimeError(f"git {' '.join(args)}: {(err or '').strip()[:300]}")
    return r


def _decode(b: bytes) -> str:
    if b[:2] in (b"\xff\xfe", b"\xfe\xff"):
        return b.decode("utf-16")
    return b.decode("utf-8-sig", errors="replace")


def _rev_text(vault: Path, rev: str, rel: str) -> bytes | None:
    r = git(["show", f"{rev}:{rel}"], vault, binary=True)
    return r.stdout if r.returncode == 0 else None


def _run_cmd(cmd: list, cwd: Path, fills: dict, timeout: int = 120) -> tuple:
    """A configured command ([..., "{ask}", ...]) with its placeholders filled; (rc, output)."""
    argv = [str(a).format(**fills) for a in cmd]
    if argv and argv[0] == "python":
        argv[0] = sys.executable
    try:
        r = subprocess.run(argv, cwd=str(cwd), capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=timeout)
        return r.returncode, ((r.stdout or "") + (r.stderr or "")).strip()
    except (OSError, subprocess.TimeoutExpired) as e:
        return 1, f"{type(e).__name__}: {e}"


# ---------------------------------------------------------------- working lines vs prose (engine-independent)
# line classification lives in engine.py (one truth for the export and the flags)
working_lines = engine.working_lines
_py_prose_spans = engine.py_prose_spans
_py_trailing = engine.py_trailing
_LINE_COMMENT = engine._LINE_COMMENT


class _PiiEmail:
    """A regex-like object: search() hits on an e-mail address the engine's pii_scrub would not keep."""
    @staticmethod
    def search(s: str):
        for m in engine.EMAIL_RX.finditer(s):
            if not engine.EMAIL_KEEP.search(m.group(0)):
                return m
        return None


class _PiiPhone:
    @staticmethod
    def search(s: str):
        for m in engine.PHONE_RX.finditer(s):
            if m.group(3) != "555" and m.group(5) != "555":
                return m
        return None


_PII_EMAIL, _PII_PHONE = _PiiEmail(), _PiiPhone()


# ---------------------------------------------------------------- the organization's people (2026-10-29)
# The identity rules above know the owners and a hand-kept list; a template went public still naming ~25 clients,
# friends and family from test fixtures. core_sync.json -> people_sources lists the organization's own records
# (contacts, CRM snapshot, the agent's memory, lead files, people/client notes): every full name, surname and record
# token in them is harvested here, any export line carrying one is flagged (held / removed), and `people --write`
# gives each one found in the template a made-up stand-in (core_sync.json -> pseudonyms, applied by engine's scrub).
_NAME_KEYS = {"name", "full_name", "fullname", "display_name", "partner", "partner_name", "client_name", "contact_name", "subject",
              "first_name", "last_name"}      # core_sync.json -> people_name_keys adds an organization's own (its client words)
_NAME_SHAPE = re.compile(r"^[A-Z][a-zA-Z'\-]+(?: [A-Z][a-zA-Z'\-]+){1,3}$")
_TOKEN_SHAPE = re.compile(r"^(?=.*\d)(?=.*[a-z])[a-z0-9]{24,40}$")
_PEOPLE = None
_EXTRA_KEYS: set = set()


def _json_names(obj, out: set) -> None:
    if isinstance(obj, dict):
        fn, ln = obj.get("first_name"), obj.get("last_name")
        if isinstance(fn, str) and isinstance(ln, str) and fn.strip() and ln.strip():
            out.add(f"{fn.strip()} {ln.strip()}")
        for k, v in obj.items():
            if isinstance(v, str) and str(k).lower() in _NAME_KEYS | _EXTRA_KEYS:
                for part in re.split(r"\s+(?:&|and)\s+|,|/|\(", v):
                    out.add(part.strip())
            elif isinstance(v, (dict, list)):
                _json_names(v, out)
    elif isinstance(obj, list):
        for v in obj:
            _json_names(v, out)


def vault_people(vault: Path, cfg: dict) -> dict:
    """{'full': {names}, 'surnames': {surnames}, 'tokens': {record tokens}} from core_sync.json -> people_sources."""
    global _PEOPLE
    if _PEOPLE is not None:
        return _PEOPLE
    raw, tokens = set(), set()
    _EXTRA_KEYS.update(str(k).lower() for k in (cfg.get("people_name_keys") or []))
    for src in cfg.get("people_sources") or []:
        for p in sorted(vault.glob(str(src))):
            if not p.is_file():
                continue
            if _TOKEN_SHAPE.match(p.stem):
                tokens.add(p.stem)              # a record file named by its id (a form response, a lead)
            if p.suffix.lower() == ".json":
                try:
                    _json_names(json.loads(p.read_text(encoding="utf-8-sig")), raw)
                except (OSError, ValueError):
                    pass
            elif p.suffix.lower() == ".md":
                for part in re.split(r"\s+(?:&|and)\s+|,|\(", p.stem):
                    raw.add(part.strip())
    for t in cfg.get("private_tokens") or []:
        tokens.add(str(t))
    # people named only in prose (the agent's memory, people notes): "First Last" where First is a known contact's first name
    not_first = {str(w).lower() for w in (cfg.get("not_first_names") or [])}
    firsts = {n.split()[0] for n in raw if _NAME_SHAPE.match(n.strip()) and len(n.split()[0]) >= 3 and n.split()[0].lower() not in not_first}
    pair = re.compile(r"\b([A-Z][a-z]{2,})\s+([A-Z][a-zA-Z'\-]{2,})\b")
    for src in cfg.get("people_text_sources") or []:
        for p in sorted(vault.glob(str(src))):
            try:
                txt = p.read_text(encoding="utf-8-sig", errors="replace")
            except OSError:
                continue
            for a, b in pair.findall(txt):
                if a in firsts:
                    raw.add(f"{a} {b}")
    skip = {str(s).lower() for s in (cfg.get("people_allow") or [])}
    full, surnames = set(), set()
    for nm in raw:
        nm = re.sub(r"\s+", " ", nm).strip()
        nm = re.sub(r"'s (?:project|wedding|event)$", "", nm, flags=re.I)       # a CRM project title carries its client's name
        if not _NAME_SHAPE.match(nm) or nm.lower() in skip:
            continue
        full.add(nm)
        last = nm.split()[-1]
        if len(last) >= 4 and last.lower() not in skip:
            surnames.add(last)
    _PEOPLE = {"full": full, "surnames": surnames, "tokens": tokens}
    return _PEOPLE


class _AnyOf:
    """One compiled alternation over many literals (a thousand names stay one regex): search() -> a match whose
    group(0) is the literal found."""
    def __init__(self, words, case: bool):
        words = sorted({w for w in words if w}, key=len, reverse=True)
        self.rx = re.compile(r"(?<![A-Za-z0-9])(?:" + "|".join(re.escape(w) for w in words) + r")(?![A-Za-z0-9])",
                             0 if case else re.I) if words else None

    def search(self, s: str):
        return self.rx.search(s) if self.rx else None


def people_patterns(vault: Path, cfg: dict) -> list:
    """(regex, label) pairs: a full name (any case), a surname (as written, capitalized) or a record token from the
    organization's records, minus what pseudonyms already replace."""
    pp = vault_people(vault, cfg)
    return [(_AnyOf(pp["full"], case=False), "a person's name from the organization's records"),
            (_AnyOf(pp["surnames"], case=True), "a surname from the organization's records"),
            (_AnyOf(pp["tokens"], case=True), "a record id from the organization's records")]


def flag_line(ln: str, ident: list, niche: list, allow: list) -> tuple:
    """('identity'|'niche'|'', label) for one working line."""
    probe = ln
    for a in allow:
        probe = a.sub(" ", probe)
    for rx, label in ident:
        if rx.search(probe):
            return "identity", label
    for rx, label in niche:
        if rx.search(probe):
            return "niche", label
    return "", ""


CONNECTOR_LABEL = "a connector of the organization's own"


def for_path(rel: str, ident: list) -> list:
    """The identity patterns that apply to rel: catalog and onboarding reference may name any service (they are how
    the agent learns to connect one), so the connector pattern skips them (2026-10-29)."""
    keep = ("assistant/catalog/", "assistant/onboarding/")
    if rel.replace("\\", "/").startswith(keep):
        return [(rx, lb) for rx, lb in ident if not lb.startswith(CONNECTOR_LABEL)]
    return ident


def flags_of(rel: str, text: str, ident: list, niche: list, allow: list) -> dict:
    """{line_no: (kind, label, text)} for every flagged working line of the file."""
    out = {}
    ident = for_path(rel, ident)
    for n, ln in working_lines(rel, text).items():
        kind, label = flag_line(ln, ident, niche, allow)
        if kind:
            out[n] = (kind, label, ln.strip())
    return out


def norm(ln: str) -> str:
    return re.sub(r"\s+", " ", ln.strip())


def niche_patterns(cfg: dict) -> list:
    pats = []
    for t in cfg.get("niche_terms") or []:
        t = str(t).strip()
        if not t:
            continue
        if t.endswith("_"):            # a prefix such as crm_
            pats.append((re.compile(r"(?<![A-Za-z0-9])" + re.escape(t), re.I), f"niche {t}"))
        else:
            pats.append((re.compile(r"(?<![A-Za-z])" + re.escape(t) + r"(?![A-Za-z])", re.I), f"niche {t}"))
    return pats


def allow_patterns(cfg: dict) -> list:
    return [re.compile(re.escape(str(t)), re.I) for t in (cfg.get("allow_terms") or []) if str(t).strip()]


# ================================================================ the Sage engine (assistant/engine.py)
class SageEngine:
    """The engine / pack / module / niche contract of assistant/engine.py; sage-core as the template."""
    name = "sage"
    config_rel = "assistant/core_sync.json"

    def __init__(self, vault: Path, cfg: dict):
        self.vault, self.cfg = vault, cfg
        spec = importlib.util.spec_from_file_location("sage_engine", str(vault / "assistant" / "engine.py"))
        self.e = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.e)
        self.set_niche_paths(cfg.get("niche_paths") or [])
        if self.vault.resolve() != Path(self.e.VAULT).resolve():      # the engine's scrub pairs are built for its own vault
            self.e.INFRA_SCRUB, self.e.ORG_SCRUB, self.e.OWNER_SCRUB, self.e.CODE_SCRUB = self.e._build_scrubs(self.vault)

    @staticmethod
    def detect(vault: Path) -> bool:
        return (vault / "assistant" / "engine.py").exists()

    def state_dir(self) -> Path:
        if self.cfg.get("state_dir"):
            return self.vault / str(self.cfg["state_dir"])
        return Path(os.environ["ASSISTANT_DATA"]) if os.environ.get("ASSISTANT_DATA") else self.vault / "assistant" / "data"

    def set_niche_paths(self, paths: list) -> None:
        self.e.EXTRA_NICHE = {str(p).replace("\\", "/") for p in paths}

    def files(self, rev: str) -> list:
        return self.e.engine_files(self.vault, with_tests=True)

    def ships(self, rel: str) -> bool:
        return self.e.ships(rel, with_tests=True)

    def is_pack(self, rel: str) -> bool:
        return self.e.is_pack(rel)

    def is_pack_template(self, rel: str) -> bool:
        """The pack files the engine carries ONCE as the template new_org fills (profile, people, org, config ...). Pack
        DATA (Wiki/, Memory/, data/, state/, the task XMLs) is the organization's own and never travels."""
        rel = rel.replace("\\", "/")
        if rel in getattr(self.e, "PACK_NEVER_TEMPLATE", set()):
            return False                    # pure organization data (prompt.local.md): new_org writes a fresh one
        return rel in self.e.PACK_FILES or rel.startswith("assistant/tasks/")      # the task XMLs are templates new_org rewrites

    def is_niche(self, rel: str) -> bool:
        return self.e.is_niche(rel)

    def why_not(self, rel: str) -> str:
        return "pack" if self.is_pack(rel) else ("niche" if self.is_niche(rel) else "never ships")

    def textual(self, rel: str) -> bool:
        return self.e.textual(rel)

    def core_ready(self, core: Path) -> bool:
        return (core / self.e.MANIFEST_NAME).exists() and (core / ".git").exists()

    # -- the organization's markers, from the pack (engine.org_values reads org.json + people.json)
    def identity(self) -> list:
        """(regex, label) pairs: a working line of code matching one carries this organization's identity. The same
        values engine's scrub pairs are built from; after an export none of them may match anywhere."""
        v = self.e.org_values(self.vault)
        pats = []
        for e in sorted(v["emails"]):
            pats.append((re.compile(re.escape(e), re.I), f"address {e}"))
        for d in sorted(v["domains"]):
            pats.append((re.compile(re.escape(d), re.I), f"domain {d}"))
        for h in sorted(v["hosts"]):
            pats.append((re.compile(re.escape(h), re.I), f"host {h}"))
        for n in sorted(v["names"], key=len, reverse=True):
            pats.append((self.e._rx_word(n), f"name {n}"))
        words = set(v["words"]) | set(v["stems"]) | set(v["locals"]) | set(v["surnames"]) | set(v["hostparts"])
        for p in v["persons"]:
            words |= {p["name"]} | set(p["words"])
        for w in sorted((w for w in words if len(w) >= 3), key=len, reverse=True):
            pats.append((self.e._rx_word(w), f"word {w}"))
        for form in self.e._root_forms(self.vault):
            pats.append((re.compile(re.escape(form), re.I), "the vault path"))
        # zero personal data (2026-10-28): any real address or phone number counts, whoever's it is
        pats.append((_PII_EMAIL, "an e-mail address"))
        pats.append((_PII_PHONE, "a phone number"))
        pats.append((re.compile(r"S-1-5-21(?:-\d+){3,4}"), "a user account SID"))       # a task XML names this PC's user (2026-10-29)
        pats.extend(people_patterns(self.vault, self.cfg))                                # anyone in the organization's records (2026-10-29)
        # the organization's own connectors (its CRM): the engine ships none (core_sync.json -> connector_terms)
        terms = [str(x) for x in (self.cfg.get("connector_terms") or []) if str(x).strip()]
        if terms:
            pats.append((re.compile("|".join(re.escape(x) for x in terms), re.I), f"{CONNECTOR_LABEL} ({', '.join(terms[:3])})"))
        # an owner's name inside a joined word ("OwnerBeach_MIX.wav", a cut-off address) slips past whole-word rules
        joined = {w for p in v["persons"] for w in [p["name"].split()[0] if p["name"] else ""] + p["name"].split()[1:] if len(w) >= 4}
        joined |= {s for s in v["surnames"] if len(s) >= 4}
        if joined:
            pats.append((re.compile("|".join(re.escape(w) for w in sorted(joined, key=len, reverse=True)), re.I), "an owner's name inside a word"))
        return pats

    def prose_scrub(self, s: str, rel: str = "x.md") -> str:
        return self.e.scrub(self.e.to_template(s, self.vault), "x.md")

    def export_text(self, rel: str, txt: str) -> str:
        """engine.export_text: every prose line scrubbed readably, every working line with the code-safe tokens, so
        the template carries no mention of the organization at all (the owner 2026-10-28)."""
        return self.e.export_text(rel, txt, self.vault)

    def verify(self, rel: str, data: bytes, ident: list) -> str:
        """'' when clean: a .py compiles, and NO line of the exported file still matches the organization's identity."""
        if rel.lower().endswith(".py"):
            try:
                compile(data.decode("utf-8", "replace"), rel, "exec")
            except SyntaxError as e:
                return f"does not compile: {e.msg} (line {e.lineno})"
        if self.textual(rel):
            txt = data.decode("utf-8", "replace")
            for rx, label in for_path(rel, ident):
                if rx.search(txt):
                    return f"still carries {label}"
        return ""

    def bootstrap_tree(self, core: Path, head: str, export_bytes) -> list:
        """engine.export's clean tree (already scrubbed line by line), every text file re-written from HEAD's content
        so the baseline is a commit, not the working tree. Returns the files."""
        man = self.e.export(core, src_root=self.vault, with_tests=True, git=False)
        for rel in man["files"]:
            if rel.startswith("assistant/templates/data/") or not self.textual(rel):
                continue
            raw = _rev_text(self.vault, head, rel)
            if raw is None:
                raw = (self.vault / rel).read_bytes()        # not committed yet: the working tree, as engine.export saw it
            (core / rel).write_bytes(export_bytes(rel, raw))
        return list(man["files"]) + [self.e.MANIFEST_NAME, "README.md", ".gitignore"]

    def finish_commit(self, core: Path) -> list:
        """Refresh the manifest + README before a template commit; returns the extra paths to add."""
        e = self.e
        man = e.manifest_of(core, with_tests=True)
        full = _load(core / e.MANIFEST_NAME, {}) or {}
        full.setdefault("engine", {})
        full["engine"].update({"name": "sage-core", "version": man["engine"]["version"], "synced": time.strftime("%Y-%m-%dT%H:%M:%S"),
                               "root_placeholder": e.ROOT_PLACEHOLDER, "source": "export"})       # never the source folder's name
        full["modules"] = {k: {"what": v["what"], "jobs": v["jobs"]} for k, v in e.MODULES.items()}
        full["pack_prefixes"], full["pack_files"], full["files"] = list(e.PACK_PREFIXES), sorted(e.PACK_FILES), man["files"]
        (core / e.MANIFEST_NAME).write_text(json.dumps(full, indent=1, ensure_ascii=False) + "\n", encoding="utf-8")
        (core / "README.md").write_text(e._engine_readme(full), encoding="utf-8")
        if getattr(e, "ENGINE_GITIGNORE", ""):
            (core / ".gitignore").write_text(e.ENGINE_GITIGNORE, encoding="utf-8")     # the engine's own, never the organization's
        return [e.MANIFEST_NAME, "README.md", ".gitignore"]

    def note(self, text: str) -> None:
        """A Mission Control note (a system note never goes to Chat: the voice gate's rule)."""
        cards = self.state_dir() / "cards.jsonl"
        cards.parent.mkdir(parents=True, exist_ok=True)
        with open(cards, "a", encoding="utf-8") as f:
            f.write(json.dumps({"ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "text": text[:3500], "kind": "note", "source": "core_sync"},
                               ensure_ascii=False) + "\n")

    def file_build(self, rel: str, ask: str, since: str) -> str:
        """ONE wiki build request per held file, the shape builder.collect reads ("Build request" in the title,
        no builder mark). Written straight through wiki_writer, not capability.file_build: that one folds asks that
        share vocabulary into the first open request (same_ask), and every hold's ask shares most of its words, so
        three held files became one request about the first (2026-10-28). The hold's `build` key is the dedupe."""
        sys.path.insert(0, str(self.vault / "assistant" / "agent"))
        import wiki_writer  # noqa: E402
        title = f"Build request Make {rel.replace('/', ' ').replace('_', '-')} organization-free"[:90]   # wiki_tool FILENAME_RE: no _
        body = (f"## Build request\n\n{ask}\n\nFile: `{rel}`. Held out of sage-core by core_sync since {since}; the next sync "
                f"releases it by itself once the flagged lines are gone (`python assistant/core_sync.py classify {rel}` shows them). "
                f"Tests: `assistant/tests/test_template.py`, `test_sage_core.py`, `test_core_sync.py`.")
        x = wiki_writer.write({"title": title, "type": "workflow", "lane": "personal", "body": body}, by="core_sync")
        if x.get("status") not in ("applied", "appended"):
            _log(f"build request for {rel} refused: {x.get('reason')}")
            return ""
        try:
            import core as agent_core  # noqa: E402
            agent_core.log_change("core_sync_build", f"held {rel}: build request filed {x.get('path')}", {"op": "none"}, "core_sync")
        except Exception:  # noqa: BLE001
            pass
        return str(x.get("path") or "")

    def file_port(self, up: dict, note_path: Path, diff: str) -> str:
        """ONE wiki build request for an upgrade another agent landed: the builder ports the mechanism in this agent's
        own shape, or answers not_code when there is nothing here it fits."""
        sys.path.insert(0, str(self.vault / "assistant" / "agent"))
        import wiki_writer  # noqa: E402
        subj = re.sub(r"[^A-Za-z0-9 \-&+',.()#]+", " ", str(up.get("subject") or "upgrade"))
        label = re.sub(r"[^A-Za-z0-9 -]+", " ", str(up.get("label") or up.get("source") or "another agent"))
        title = re.sub(r"\s+", " ", f"Build request Port from {label} - {subj}").strip()[:90]     # no ':' (an NTFS stream in a filename)
        body = port_body(up, note_path, diff, "answer not_code with the reason in the summary")
        x = wiki_writer.write({"title": title, "type": "workflow", "lane": "personal", "body": body}, by="core_sync")
        if x.get("status") not in ("applied", "appended"):
            _log(f"port request for {up.get('id')} refused: {x.get('reason')}")
            return ""
        return str(x.get("path") or "")


# ================================================================ the template engine (scripts/rescue/build_template.py)
class TemplateEngine:
    """The property vault's own builder: its drop rules, {{TOKEN}} substitutions and PII scrub, imported as a module;
    pm-operator-template as the template (instantiate.py fills the tokens)."""
    name = "template"
    config_rel = "scripts/rescue/core_sync.json"
    BUILDER = "scripts/rescue/build_template.py"
    EXTRAS = ("property.json", "instantiate.py", "TEMPLATE_README.md", "TEMPLATE_REPORT.md", ".gitignore", ".gitattributes")

    def __init__(self, vault: Path, cfg: dict):
        self.vault, self.cfg = vault, cfg
        spec = importlib.util.spec_from_file_location("build_template", str(vault / self.BUILDER))
        self.b = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.b)
        self.b.VAULT = vault
        self._scrubber = None
        self.niche_paths = {str(p).replace("\\", "/") for p in (cfg.get("niche_paths") or [])}

    @staticmethod
    def detect(vault: Path) -> bool:
        return (vault / TemplateEngine.BUILDER).exists()

    @property
    def scrubber(self):
        if self._scrubber is None:
            self._scrubber = self.b.Scrubber()
        return self._scrubber

    def state_dir(self) -> Path:
        return self.vault / str(self.cfg.get("state_dir") or "scripts/rescue/.cache/core_sync")

    def set_niche_paths(self, paths: list) -> None:
        self.niche_paths = {str(p).replace("\\", "/") for p in paths}

    def files(self, rev: str) -> list:
        r = git(["ls-tree", "-r", "--name-only", rev], self.vault)
        return sorted(rel for rel in (r.stdout or "").splitlines() if rel and self.ships(rel))

    def is_pack(self, rel: str) -> bool:
        return rel in self.b.STUBS

    def is_niche(self, rel: str) -> bool:
        return rel in self.niche_paths or any(rel.startswith(n + "/") for n in self.niche_paths)

    def ships(self, rel: str) -> bool:
        b = self.b
        name = rel.rsplit("/", 1)[-1]
        if rel == self.BUILDER or rel.startswith(b.DROP_PREFIXES) or b.DROP_NAME_RE.search(name):
            return False
        if "/" not in rel and b.DROP_ROOT_FILES and rel not in b.KEEP_ROOT:
            return False
        if rel in b.STUBS or self.is_niche(rel):      # a stub is written once by bootstrap, never synced
            return False
        return True

    def why_not(self, rel: str) -> str:
        if rel in self.b.STUBS:
            return "stub (the builder replaces it)"
        return "niche" if self.is_niche(rel) else "dropped by build_template (data)"

    def textual(self, rel: str) -> bool:
        return Path(rel).suffix.lower() in self.b.TEXT_EXT

    def core_ready(self, core: Path) -> bool:
        return (core / ".git").exists() and (core / "instantiate.py").exists()

    def identity(self) -> list:
        """What survives the builder's own substitutions: unit numbers and a bare property id (the builder's report
        calls these 'NOT agnostic yet'), plus any identifier the builder tokenizes (those never hold: the export
        tokenizes them, which is the template's contract; they are listed by `classify` as 'token ...')."""
        pats = []
        if self.cfg.get("flag_units", True):
            pats.append((self.b.UNIT_RE, "unit number"))
            pats.append((self.b.LEFT_RE, "bare property id"))
        return pats

    def tokens(self) -> list:
        return [(rx, f"token {tok}") for rx, tok in self.b.SUBS]

    def prose_scrub(self, s: str, rel: str = "x.md") -> str:
        for rx, token in self.b.SUBS:
            s = rx.sub(token, s)
        return self.scrubber.scrub(s)

    def export_text(self, rel: str, txt: str) -> str:
        return self.prose_scrub(txt, rel)            # the builder's contract: tokens and the PII scrub over the whole file

    def verify(self, rel: str, data: bytes, ident: list) -> str:
        """'' when no raw identifier or resident name survived the export."""
        txt = data.decode("utf-8", "replace")
        for rx, tok in self.b.SUBS:
            if rx.search(txt) and "{{" not in rx.pattern:
                return f"still carries what {tok} replaces"
        sc = self.scrubber
        if sc.rx_full and sc.rx_full.search(txt):
            return "still carries a resident's name"
        return ""

    def bootstrap_tree(self, core: Path, head: str, export_bytes) -> list:
        """Every shipping file at HEAD into the template checkout, the builder's stubs and package files alongside;
        tracked files that no longer ship are removed. Returns the paths written or removed."""
        b = self.b
        core.mkdir(parents=True, exist_ok=True)
        written = []
        wanted = set()
        for rel in self.files(head):
            raw = _rev_text(self.vault, head, rel)
            if raw is None:
                continue
            data = export_bytes(rel, raw)
            dst = core / rel
            dst.parent.mkdir(parents=True, exist_ok=True)
            if not dst.exists() or dst.read_bytes() != data:
                dst.write_bytes(data)
                written.append(rel)
            wanted.add(rel)
        for rel, body in b.STUBS.items():
            p = core / rel
            p.parent.mkdir(parents=True, exist_ok=True)
            if not p.exists():
                p.write_text(body, encoding="utf-8")
                written.append(rel)
            wanted.add(rel)
        for rel, body in (("property.json", json.dumps(b.PROPERTY_JSON, indent=2)), ("instantiate.py", b.INSTANTIATE), ("TEMPLATE_README.md", b.README)):
            p = core / rel
            if not p.exists():
                p.write_text(body, encoding="utf-8")
                written.append(rel)
        if (core / ".git").exists():
            tracked = (git(["ls-files"], core).stdout or "").splitlines()
            for rel in tracked:
                if rel not in wanted and rel not in self.EXTRAS and rel not in b.STUBS:
                    (core / rel).unlink(missing_ok=True)
                    written.append(rel)
        return written

    def finish_commit(self, core: Path) -> list:
        return []

    def note(self, text: str) -> None:
        cmd = self.cfg.get("note_cmd") or []
        if cmd:
            rc, out = _run_cmd(cmd, self.vault, {"text": text[:600]})
            if rc != 0:
                _log(f"note_cmd failed: {out[:200]}")
        else:
            _log("note: " + text.replace("\n", " | ")[:400])

    def file_build(self, rel: str, ask: str, since: str) -> str:
        cmd = self.cfg.get("build_cmd") or []
        if not cmd:
            return ""
        rc, out = _run_cmd(cmd, self.vault, {"ask": ask[:900], "file": rel, "since": since})
        if rc != 0:
            _log(f"build_cmd failed for {rel}: {out[:200]}")
            return ""
        m = re.search(r"\b(br-[0-9a-f]+)\b", out)
        return m.group(1) if m else (out.splitlines()[-1][:80] if out else "filed")

    def file_port(self, up: dict, note_path: Path, diff: str) -> str:
        """ONE build request through build_cmd for an upgrade another agent landed; the diff travels as a file next to
        the state (a command line cannot carry it)."""
        cmd = self.cfg.get("build_cmd") or []
        if not cmd:
            return ""
        folder = self.state_dir() / "relay"
        folder.mkdir(parents=True, exist_ok=True)
        req = folder / f"{up.get('id', 'upgrade')}.md"
        req.write_text(port_body(up, note_path, diff, "close the request as declined with the reason"), encoding="utf-8")
        ask = (f"Port from {up.get('label') or up.get('source')} if it applies here: {str(up.get('subject') or '')[:160]}. "
               f"The organization-free diff and the instructions: {req}")
        files = up.get("files") or []
        rc, out = _run_cmd(cmd, self.vault, {"ask": ask[:900], "file": files[0] if files else "", "since": str(up.get("date") or "")})
        if rc != 0:
            _log(f"build_cmd failed for port {up.get('id')}: {out[:200]}")
            return ""
        m = re.search(r"\b(br-[0-9a-f]+)\b", out)
        return m.group(1) if m else (out.splitlines()[-1][:80] if out else "filed")


ENGINES = {"sage": SageEngine, "template": TemplateEngine}


# ================================================================ the relay between agents (the commons)
def relay_cfg(cfg: dict, vault: Path, eng) -> dict:
    r = dict(DEFAULT_CONFIG["relay"])
    r.update(cfg.get("relay") or {})
    if not r.get("commons"):
        r["commons"] = str(vault.parent / "agent-commons")
    if not r.get("label"):
        r["label"] = Path(str(cfg.get("core_root") or eng.name)).name
    return r


def port_body(up: dict, note_path: Path, diff: str, decline: str) -> str:
    files = "\n".join(f"- `{f}`" for f in (up.get("files") or [])[:30])
    return (f"## Build request\n\nAn upgrade landed in **{up.get('label') or up.get('source')}**, another agent of the same family, on "
            f"{up.get('date', '')}: *{up.get('subject', '')}*. Port it here IF IT APPLIES: the mechanism in this agent's own shape (its own "
            f"modules, config files and tests), never the files as they are; business facts never as literals. If this agent already "
            f"does this, or has no subsystem the change fits, change nothing and {decline}.\n\nFiles there:\n{files}\n\n"
            f"The organization-free diff, from that agent's template checkout (also at `{note_path}`):\n\n```diff\n{diff}\n```\n")


def upgrade_worthy(subject: str, applied: list, eng) -> list:
    """The applied CODE files of a commit that make it an upgrade worth relaying (none -> not relayed): a working
    script outside tests and pack templates; nightly state, skills, wiki notes and docs never travel."""
    if RELAY_SKIP_SUBJECT.search(subject or ""):
        return []
    out = []
    for rel, _why in applied:
        low = rel.lower()
        if not low.endswith(RELAY_CODE_SUFFIXES) or "/tests/" in low or low.startswith("tests/") or "/test_" in low:
            continue
        if eng.is_pack(rel):
            continue
        out.append(rel)
    return out


def relay_out(eng, core: Path, core_sha: str, vault_sha: str, subject: str, applied: list, rc: dict) -> Path | None:
    """Drop this commit's upgrade in the commons as an organization-free note with the template's own diff."""
    files = upgrade_worthy(subject, applied, eng)
    if not files or not core_sha:
        return None
    folder = Path(rc["commons"]) / "upgrades"
    folder.mkdir(parents=True, exist_ok=True)
    uid = f"{vault_sha[:7]}-{eng.name}"
    for p in folder.glob(f"*-{uid}.md"):
        return p                                      # already dropped (a re-run)
    r = git(["show", "--format=", "--no-color", core_sha, "--", *files], core)
    diff = r.stdout or ""
    cap = int(rc.get("max_diff_kb") or 12) * 1024
    if len(diff.encode("utf-8")) > cap:
        diff = diff.encode("utf-8")[:cap].decode("utf-8", "ignore") + f"\n... (trimmed to {cap // 1024} KB; the whole change is commit {core_sha} in {core.name})\n"
    up = {"id": uid, "source": eng.name, "label": rc["label"], "vault_commit": vault_sha, "template_commit": core_sha,
          "date": time.strftime("%Y-%m-%d"), "subject": eng.prose_scrub(subject), "files": files}
    txt = "---\n" + "\n".join(f"{k}: {json.dumps(v, ensure_ascii=False)}" for k, v in up.items()) + "\n---\n"
    txt += f"# {up['subject']}\n\nFrom {rc['label']} ({eng.name} engine), template commit {core_sha}, vault commit {vault_sha[:7]}, {up['date']}.\n\n"
    txt += "Files:\n" + "\n".join(f"- `{f}`" for f in files) + "\n\n```diff\n" + diff + "\n```\n"
    p = folder / f"{time.strftime('%Y%m%d-%H%M%S')}-{uid}.md"
    p.write_text(txt, encoding="utf-8")
    return p


def _read_upgrade(p: Path) -> dict | None:
    try:
        txt = p.read_text(encoding="utf-8")
    except OSError:
        return None
    m = re.match(r"---\n(.*?)\n---\n", txt, re.S)
    if not m:
        return None
    up = {}
    for ln in m.group(1).splitlines():
        if ":" in ln:
            k, v = ln.split(":", 1)
            try:
                up[k.strip()] = json.loads(v.strip())
            except ValueError:
                up[k.strip()] = v.strip()
    d = re.search(r"```diff\n(.*?)\n```", txt, re.S)
    up["diff"] = d.group(1) if d else ""
    return up


def relay_inbox(eng, cfg: dict, st: dict, rc: dict, dry: bool) -> list:
    """Other agents' upgrades in the commons that this side has not seen: one port request each, capped per run.
    The first time, everything already there is passed (relay_baseline), except the last `backfill` notes."""
    folder = Path(rc["commons"]) / "upgrades"
    if not folder.exists():
        return []
    notes = sorted(folder.glob("*.md"))
    todo = []
    for p in notes:
        up = _read_upgrade(p)
        if not up or up.get("source") == eng.name or up.get("id") in st["relay_seen"]:
            continue
        todo.append((p, up))
    if not st.get("relay_baseline"):
        keep = int(rc.get("backfill") or 0)
        passed = todo[:-keep] if keep else todo
        for p, up in passed:
            st["relay_seen"].append(up["id"])
        todo = todo[-keep:] if keep else []
        st["relay_baseline"] = True
        if passed:
            _log(f"relay baseline: {len(passed)} earlier upgrade(s) in the commons passed over")
    filed = []
    for p, up in todo[:int(rc.get("max_per_run") or 3)]:
        if dry:
            filed.append((up["id"], "(dry-run)"))
            continue
        try:
            key = eng.file_port(up, p, up.get("diff", ""))
        except Exception as e:  # noqa: BLE001
            _log(f"port request for {up['id']} not filed: {type(e).__name__}: {e}")
            key = ""
        st["relay_seen"].append(up["id"])          # seen either way: a refusal is logged, never retried every run
        if key:
            filed.append((up["id"], key))
            ledger({"action": "port-filed", "why": f"{up['id']} {up.get('subject', '')[:80]} -> {key}"})
    return filed


def pick_engine(vault: Path, name: str = "auto"):
    if name in ENGINES:
        return ENGINES[name]
    for cls in (SageEngine, TemplateEngine):
        if cls.detect(vault):
            return cls
    raise SystemExit(f"{vault}: neither assistant/engine.py nor {TemplateEngine.BUILDER} found; pass --engine")


def config(vault: Path, cls) -> dict:
    cfg = dict(DEFAULT_CONFIG)
    cfg.update(_load(vault / cls.config_rel, {}) or {})
    if not cfg.get("core_root"):
        cfg["core_root"] = str(vault.parent / ("sage-core" if cls is SageEngine else "pm-operator-template"))
    cfg["niche_paths"] = [str(p).replace("\\", "/") for p in (cfg.get("niche_paths") or [])]
    return cfg


def save_niche_paths(vault: Path, eng, paths: list) -> None:
    p = vault / eng.config_rel
    keep = _load(p, {}) or {k: v for k, v in DEFAULT_CONFIG.items() if k not in ("state_dir", "note_cmd", "build_cmd", "flag_units")}
    keep["niche_paths"] = list(paths)
    _save(p, keep)


# ---------------------------------------------------------------- one file
def export_bytes_for(eng, cfg: dict):
    def export_bytes(rel: str, raw: bytes) -> bytes:
        if not eng.textual(rel):
            return raw
        return eng.export_text(rel, _decode(raw)).encode("utf-8")
    return export_bytes


def decide(eng, rel: str, status: str, text: str, added: list, is_new: bool, cfg: dict, st: dict, ident: list, niche: list, allow: list) -> dict:
    """What to do with one file at one commit: applied | held | skipped | promoted | deleted, with why."""
    if not eng.ships(rel):
        return {"action": "skipped", "why": eng.why_not(rel), "flags": []}
    if status == "D":
        return {"action": "deleted", "why": "removed here", "flags": []}
    if rel in (cfg.get("allow_paths") or []):
        return {"action": "applied", "why": "allow_paths", "flags": []}
    if eng.is_pack(rel):                # a sage pack TEMPLATE travels once (new_org fills it); pack DATA never
        if hasattr(eng, "is_pack_template") and not eng.is_pack_template(rel):
            return {"action": "skipped", "why": "pack data", "flags": []}
        return {"action": "applied", "why": "pack template", "flags": []}
    flags = flags_of(rel, text, ident, niche, allow)
    work = working_lines(rel, text)
    added_norm = {norm(a) for a in added if a.strip()}
    if is_new and work:                 # a brand-new file that is mostly about the organization never ships
        n_flag = sum(1 for n in flags if n in work)
        if flags and n_flag * 2 >= len(work) and cfg.get("niche_policy", "hold") != "apply":
            return {"action": "promoted", "why": f"new file, {n_flag} of {len(work)} working lines are about this organization",
                    "flags": [(n, *flags[n]) for n in sorted(flags)]}
    hold = st["holds"].get(rel) or {"lines": {}}
    live = {}
    for n, (kind, label, txt) in flags.items():
        key = norm(txt)
        if key in added_norm:
            live[key] = {"why": f"{kind}: {label}", "line": n}
        elif key in hold.get("lines", {}):
            live[key] = dict(hold["lines"][key], line=n)
    policy_off = {"identity": cfg.get("identity_policy", "hold") == "apply", "niche": cfg.get("niche_policy", "hold") == "apply"}
    live = {k: v for k, v in live.items() if not policy_off.get(v["why"].split(":", 1)[0], False)}
    if live:
        return {"action": "held", "why": "; ".join(f"line {v['line']}: {v['why']}" for v in list(live.values())[:4]), "flags": live}
    return {"action": "applied", "why": "clean" if not flags else f"clean change ({len(flags)} older flagged lines are debt)", "flags": []}


# ---------------------------------------------------------------- one commit
def _parent(vault: Path, sha: str) -> str:
    """The commit's first parent (a merge is read against the line it merged INTO); the empty tree for a root commit."""
    r = git(["rev-parse", "--verify", "-q", sha + "^1"], vault)
    return (r.stdout or "").strip() or EMPTY_TREE


def _changed(vault: Path, sha: str) -> list:
    """[(status, rel)] of every path a commit touched (renames as delete + add)."""
    r = git(["diff", "--name-status", "-M", _parent(vault, sha), sha], vault)
    out = []
    for ln in (r.stdout or "").splitlines():
        if not ln.strip():
            continue
        parts = ln.split("\t")
        st = parts[0]
        if st.startswith("R") and len(parts) == 3:
            out.append(("D", parts[1].replace("\\", "/")))
            out.append(("A", parts[2].replace("\\", "/")))
        elif len(parts) >= 2:
            out.append((st[0], parts[-1].replace("\\", "/")))
    return out


def _added_lines(vault: Path, sha: str, rel: str) -> list:
    r = git(["diff", "-U0", "--no-color", _parent(vault, sha), sha, "--", rel], vault)
    return [ln[1:] for ln in (r.stdout or "").splitlines() if ln.startswith("+") and not ln.startswith("+++")]


def _subject(vault: Path, sha: str) -> str:
    return (git(["log", "-1", "--format=%s", sha], vault).stdout or "").strip()


def _commit_core(eng, core: Path, touched: list, message: str) -> str:
    """Commit only what this sync wrote (plus what the engine refreshes: manifest, README)."""
    extras = eng.finish_commit(core)
    if extras:
        git(["add", "-A", "--", *extras], core)
    existing = [t for t in touched if (core / t).exists()]
    gone = [t for t in touched if not (core / t).exists()]
    for i in range(0, len(existing), 200):               # a bootstrap adds hundreds: keep each command line short
        git(["add", "--", *existing[i:i + 200]], core)
    for i in range(0, len(gone), 200):
        git(["rm", "-q", "--cached", "--ignore-unmatch", "--", *gone[i:i + 200]], core)
    r = git([*GIT_AUTHOR, "commit", "-q", "-m", message], core)
    if r.returncode != 0 and "nothing to commit" not in (r.stdout or "") + (r.stderr or ""):
        raise RuntimeError("template commit failed: " + ((r.stderr or r.stdout) or "").strip()[:300])
    return git(["rev-parse", "--short", "HEAD"], core).stdout.strip()


def apply_commit(eng, vault: Path, core: Path, sha: str, cfg: dict, st: dict, ident: list, niche: list, allow: list, dry: bool, rc: dict | None = None) -> dict:
    subject = _subject(vault, sha)
    short = sha[:7]
    rep = {"sha": short, "subject": subject, "applied": [], "held": [], "released": [], "skipped": [], "promoted": [], "deleted": [], "failed": [], "relayed": None}
    export_bytes = export_bytes_for(eng, cfg)
    touched = []
    for status, rel in _changed(vault, sha):
        raw = None if status == "D" else _rev_text(vault, sha, rel)
        text = _decode(raw) if raw is not None and eng.textual(rel) else ""
        if status == "D" and rel in (cfg.get("niche_paths") or []):
            cfg["niche_paths"].remove(rel)            # a promoted file that is gone: forget the promotion
            eng.set_niche_paths(cfg["niche_paths"])
        d = decide(eng, rel, status, text, _added_lines(vault, sha, rel) if status != "D" else [], status == "A", cfg, st, ident, niche, allow)
        act = d["action"]
        if act == "skipped":
            rep["skipped"].append((rel, d["why"]))
            continue
        if act == "promoted":
            cfg.setdefault("niche_paths", [])
            if rel not in cfg["niche_paths"]:
                cfg["niche_paths"].append(rel)
            eng.set_niche_paths(cfg["niche_paths"])
            rep["promoted"].append((rel, d["why"]))
            ledger({"commit": short, "file": rel, "action": "promoted", "why": d["why"]})
            continue
        if act == "deleted":
            dst = core / rel
            if dst.exists():
                if not dry:
                    dst.unlink()
                touched.append(rel)
            st["holds"].pop(rel, None)
            rep["deleted"].append((rel, d["why"]))
            ledger({"commit": short, "file": rel, "action": "deleted"})
            continue
        if act == "held":
            was = st["holds"].get(rel)
            st["holds"][rel] = {"since": (was or {}).get("since") or short, "lines": d["flags"], "subject": subject,
                                "noted": bool((was or {}).get("noted")), "build": (was or {}).get("build", "")}
            rep["held"].append((rel, d["why"]))
            ledger({"commit": short, "file": rel, "action": "held", "why": d["why"]})
            continue
        data = export_bytes(rel, raw)
        bad = eng.verify(rel, data, ident)
        if bad:
            st["holds"][rel] = {"since": short, "lines": {}, "subject": subject, "noted": False, "verify": bad, "build": (st["holds"].get(rel) or {}).get("build", "")}
            rep["failed"].append((rel, bad))
            ledger({"commit": short, "file": rel, "action": "held", "why": "verify: " + bad})
            continue
        if not dry:
            dst = core / rel
            dst.parent.mkdir(parents=True, exist_ok=True)
            if not dst.exists() or dst.read_bytes() != data:
                dst.write_bytes(data)
        touched.append(rel)
        if rel in st["holds"]:
            st["holds"].pop(rel, None)
            rep["released"].append((rel, d["why"]))
        rep["applied"].append((rel, d["why"]))
        ledger({"commit": short, "file": rel, "action": "applied", "why": d["why"]})
    if touched and not dry:
        core_sha = _commit_core(eng, core, touched, f"{eng.prose_scrub(subject)} (from {short})")
        if rc and rc.get("enabled"):
            try:
                p = relay_out(eng, core, core_sha, sha, subject, rep["applied"], rc)
            except Exception as e:  # noqa: BLE001
                _log(f"relay out failed for {short}: {type(e).__name__}: {e}")
                p = None
            if p:
                rep["relayed"] = p.name
                ledger({"commit": short, "action": "relayed", "why": p.name})
    return rep


# ---------------------------------------------------------------- bootstrap, debt, refresh
def bootstrap(eng, vault: Path, core: Path, cfg: dict, head: str) -> dict:
    if core.exists() and any(p for p in core.iterdir() if p.name != ".git") and not eng.core_ready(core):
        raise SystemExit(f"{core} exists and is not a {eng.name} template checkout")
    existing = eng.core_ready(core)
    written = eng.bootstrap_tree(core, head, export_bytes_for(eng, cfg))
    if not (core / ".git").exists():
        git(["init", "-q"], core, check=True)
    label = "re-exported at" if existing else "exported at"
    _commit_core(eng, core, written, f"{eng.name} template {label} {head[:7]} by core_sync (from {head[:7]})")
    _log(f"bootstrap: {label} {head[:7]}: {len(written)} files written into {core}")
    return {"bootstrap": label, "files": len(written), "core": str(core)}


def baseline_debt(eng, vault: Path, rev: str, ident: list, niche: list, allow: list) -> dict:
    debt = {}
    for rel in eng.files(rev):
        if not eng.textual(rel) or eng.is_pack(rel):
            continue
        raw = _rev_text(vault, rev, rel)
        if raw is None:
            continue
        fl = flags_of(rel, _decode(raw), ident, niche, allow)
        if fl:
            debt[rel] = [{"line": n, "why": f"{k}: {lbl}", "text": t[:120]} for n, (k, lbl, t) in sorted(fl.items())]
    return debt


def audit(eng, vault: Path, core: Path, quiet: bool = False) -> int:
    """Every text file the template checkout holds, scanned with this organization's identity patterns: the proof that
    no mention of the organization is in it. Prints the offenders; exit 1 when there are any."""
    if not core.exists():
        print(f"{SENTINEL} audit: {core} does not exist")
        return 1
    ident = eng.identity()
    files = (git(["ls-files"], core).stdout or "").splitlines()
    hits, n_files = 0, 0
    for rel in files:
        if not eng.textual(rel) and not rel.lower().endswith((".md", ".txt", ".json")):
            continue
        p = core / rel
        if not p.is_file():
            continue
        n_files += 1
        try:
            txt = _decode(p.read_bytes())
        except OSError:
            continue
        for i, ln in enumerate(txt.splitlines(), 1):
            # the manifest lists every shipped file, catalog reference included (it may name any service)
            line_rel = rel if not (rel == getattr(getattr(eng, "e", None), "MANIFEST_NAME", "engine_manifest.json") and re.search(r'"assistant/(?:catalog|onboarding)/', ln)) \
                else "assistant/catalog/x"
            for rx, label in for_path(line_rel, ident):
                if rx.search(ln):
                    hits += 1
                    if not quiet and hits <= 60:
                        print(f"  {rel}:{i}: {label}: {ln.strip()[:100]}")
                    break
    print(f"{SENTINEL} audit: {hits} line(s) mention the organization in {n_files} text files of {core.name}")
    return 1 if hits else 0


_FAKE_FIRST = ("Avery", "Blake", "Casey", "Dana", "Ellis", "Finley", "Gray", "Harper", "Indy", "Jules", "Kai", "Lane", "Morgan",
               "Noel", "Oakley", "Parker", "Rowan", "Reese", "Sloane", "Tatum", "Umber", "Vale", "Wren", "Yael", "Zion", "Arden",
               "Brook", "Cove", "Drew", "Emery", "Fable", "Glenn", "Hollis", "Ira", "Jody", "Kit", "Linden", "Marlo", "Nico", "Onyx")
_FAKE_LAST = ("Ashdown", "Brightwater", "Calloway", "Dunmore", "Everhart", "Fairbanks", "Glenrock", "Hollowell", "Ironwood",
              "Juniper", "Kestrel", "Larkspur", "Merriweather", "Northcott", "Oakhurst", "Pennywhistle", "Quillon", "Rookwood",
              "Silverlake", "Thistlewood", "Underhill", "Vantablack", "Whitlock", "Yarrow", "Zephyrine", "Amberly", "Birchell",
              "Copperfield", "Dovetail", "Elmswood", "Foxglove", "Greystone", "Heathcote", "Ivybridge", "Jessamine", "Kingsmere")


def _fake(real: str, pool: tuple, taken: set) -> str:
    h = int(hashlib.sha1(real.lower().encode("utf-8")).hexdigest(), 16)
    for i in range(len(pool) * len(pool)):
        cand = pool[(h + i) % len(pool)]
        if cand.lower() not in taken:
            return cand
    return pool[h % len(pool)] + str(h % 97)


def people(eng, vault: Path, core: Path, cfg: dict, write: bool = False) -> int:
    """Every person, surname and record id from the organization's records that the template still carries; with
    --write each gets a stable made-up stand-in in core_sync.json -> pseudonyms (then `refresh` re-exports)."""
    pp = vault_people(vault, cfg)
    pats = people_patterns(vault, cfg)
    found = {"full": set(), "surname": set(), "token": set()}
    where = {}
    for rel in (git(["ls-files"], core).stdout or "").splitlines():
        p = core / rel
        if not p.is_file() or (not eng.textual(rel) and not rel.lower().endswith((".md", ".txt", ".json"))):
            continue
        txt = _decode(p.read_bytes())
        for (rx, _label), kind in zip(pats, ("full", "surname", "token")):
            if rx.rx is None:
                continue
            for m in rx.rx.finditer(txt):
                found[kind].add(m.group(0))
                where.setdefault(m.group(0), set()).add(rel)
    n = sum(len(v) for v in found.values())
    print(f"{SENTINEL} people: {len(pp['full'])} names, {len(pp['surnames'])} surnames, {len(pp['tokens'])} record ids known; "
          f"{n} still in {core.name}")
    for kind in ("full", "surname", "token"):
        for w in sorted(found[kind]):
            print(f"  {kind:7} {w}  ({', '.join(sorted(where[w])[:3])}{' ...' if len(where[w]) > 3 else ''})")
    if not write or not n:
        return 1 if n else 0
    path = vault / eng.config_rel
    doc = _load(path, {}) or {}
    ps = dict(doc.get("pseudonyms") or {})
    taken = {w.lower() for w in pp["surnames"]} | {w.split()[0].lower() for w in pp["full"]} | {v.lower() for v in ps.values()}
    sur_map = {}
    def fake_last(s):
        if s not in sur_map:
            hit = next((ps[k] for k in ps if k.lower() == s.lower()), "")
            sur_map[s] = hit or _fake(s, _FAKE_LAST, taken)
            taken.add(sur_map[s].lower())
        return sur_map[s]
    for nm in sorted(found["full"], key=len, reverse=True):
        real = next((f for f in pp["full"] if f.lower() == nm.lower()), nm)
        if real in ps:
            continue
        parts = real.split()
        first = _fake(parts[0], _FAKE_FIRST, set())
        ps[real] = f"{first} {fake_last(parts[-1])}"
    for s in sorted(found["surname"]):
        if s not in ps:
            ps[s] = fake_last(s)
    for t in sorted(found["token"]):
        if t not in ps:
            ps[t] = "x" + hashlib.sha1(t.encode("utf-8")).hexdigest()[:len(t) - 1]
    doc["pseudonyms"] = dict(sorted(ps.items(), key=lambda kv: kv[0].lower()))
    if not path.exists():
        print(f"{SENTINEL} people: no {eng.config_rel} to write")
        return 1
    path.write_text(json.dumps(doc, indent=1, ensure_ascii=False) + "\n", encoding="utf-8")
    print(f"{SENTINEL} people: {len(ps)} pseudonyms in {eng.config_rel}; run `refresh` to re-export")
    return 0


def refresh(eng, vault: Path, core: Path, cfg: dict, dry: bool = False) -> int:
    """Re-export every file the template already holds from the last synced commit under the CURRENT rules (after a
    scrub rule or niche_terms change). Held files stay at their held version; nothing new is decided."""
    st = state()
    rev = st["last_commit"]
    if not rev or not eng.core_ready(core):
        print(f"{SENTINEL} refresh: nothing synced yet")
        return 1
    ident = eng.identity()
    export_bytes = export_bytes_for(eng, cfg)
    touched, held_back = [], []
    zero = bool(cfg.get("zero_identity", True))
    for rel in eng.files(rev):
        if not eng.textual(rel) or (eng.is_pack(rel) and eng.name != "sage"):
            continue
        if rel in st["holds"] and not zero:
            continue
        raw = _rev_text(vault, rev, rel)
        if raw is None:
            continue
        data = export_bytes(rel, raw)
        bad = eng.verify(rel, data, ident)
        if bad:
            held_back.append(rel)
            if zero:
                # zero personal data: a file the rules cannot clean does not stay in the template at an older, dirtier
                # version; it leaves, held with the reason, and comes back by itself once a commit makes it clean
                dst = core / rel
                if dst.exists():
                    touched.append(rel)
                    if not dry:
                        dst.unlink()
                h = st["holds"].get(rel) or {"since": rev[:7], "lines": {}, "subject": "refresh", "noted": False, "build": ""}
                h["verify"] = bad
                st["holds"][rel] = h
            continue
        if rel in st["holds"] and zero:
            st["holds"].pop(rel, None)         # clean under the current rules: released
        dst = core / rel
        if not dst.exists() or dst.read_bytes() != data:
            touched.append(rel)
            if not dry:
                dst.parent.mkdir(parents=True, exist_ok=True)
                dst.write_bytes(data)
    # the three data templates (sop_map / comms_law / client_texts) come from assistant/data at the synced commit, as engine.export writes them
    if hasattr(eng, "e") and hasattr(eng.e, "DATA_TEMPLATES"):
        for name in eng.e.DATA_TEMPLATES:
            raw = _rev_text(vault, rev, f"assistant/data/{name}") or _rev_text(vault, rev, f"assistant/templates/data/{name}")
            if raw is None:
                continue
            rel = f"assistant/templates/data/{name}"
            data = eng.e.export_text(rel, _decode(raw)).encode("utf-8")
            bad = eng.verify(rel, data, ident)
            dst = core / rel
            if bad:
                held_back.append(rel)
                if zero and dst.exists():
                    touched.append(rel)
                    if not dry:
                        dst.unlink()
                continue
            if not dst.exists() or dst.read_bytes() != data:
                touched.append(rel)
                if not dry:
                    dst.parent.mkdir(parents=True, exist_ok=True)
                    dst.write_bytes(data)
    # what the template holds but should not: a file that no longer ships (promoted to niche, pack data, a root file)
    keep = {eng.e.MANIFEST_NAME, "README.md", ".gitignore"} if hasattr(eng, "e") else {"README.md", ".gitignore"}
    stale = []
    for rel in (git(["ls-files"], core).stdout or "").splitlines():
        rel = rel.strip()
        if not rel or rel in keep:
            continue
        if not eng.ships(rel) or (eng.is_pack(rel) and hasattr(eng, "is_pack_template") and not eng.is_pack_template(rel)):
            stale.append(rel)
            touched.append(rel)
            if not dry and (core / rel).exists():
                (core / rel).unlink()
    if touched and not dry:
        _commit_core(eng, core, touched, f"core_sync refresh under the current rules (at {rev[:7]})")
        ledger({"commit": rev[:7], "action": "refresh", "why": f"{len(touched)} files re-exported, {len(held_back)} held back, {len(stale)} stale removed"})
    if not dry:
        _save(STATE, st)
    print(f"{SENTINEL} {'dry-run ' if dry else ''}refresh: {len(touched)} file(s) re-exported or removed, {len(held_back)} would not verify"
          + (" (removed from the template: zero personal data)" if zero and held_back else ""))
    for rel in held_back[:15]:
        print("  not clean:", rel)
    for rel in stale[:15]:
        print("  no longer ships, removed:", rel)
    return 0


# ---------------------------------------------------------------- build requests + notes
def file_builds(eng, st: dict, cfg: dict) -> list:
    """ONE build request per held file, capped per run."""
    if not cfg.get("file_builds", True):
        return []
    filed = []
    for rel, h in list(st["holds"].items()):
        if h.get("build") or len(filed) >= MAX_BUILDS_PER_RUN:
            continue
        lines = h.get("lines") or {}
        why = h.get("verify") or "; ".join(f"line {v.get('line')}: {v.get('why')}" for v in list(lines.values())[:6])
        ask = (f"Make {rel} organization-free so it can ship in the template: {why}. Move the organization's names, addresses, "
               f"hosts, unit numbers and niche logic behind configuration, a module or a niche path; behaviour here must not change.")
        try:
            key = eng.file_build(rel, ask, str(h.get("since") or ""))
        except Exception as e:  # noqa: BLE001
            _log(f"build request for {rel} not filed: {type(e).__name__}: {e}")
            key = ""
        if key:
            h["build"] = key
            filed.append((rel, key))
            ledger({"file": rel, "action": "build-filed", "why": key})
    return filed


def note(eng, text: str, cfg: dict) -> None:
    if not cfg.get("notify", True) or not text.strip():
        return
    try:
        eng.note(text)
    except Exception as e:  # noqa: BLE001
        _log(f"note not written: {type(e).__name__}: {e}")


def _summary(reps: list, st: dict, released: list, new_holds: list, filed: list, core: Path, ports: list | None = None) -> str:
    n_app = sum(len(r["applied"]) for r in reps)
    parts = [f"template sync: {len(reps)} commit(s) read, {n_app} file(s) applied to {core.name}"]
    if new_holds:
        parts.append(f"{len(new_holds)} held (organization-specific code)")
    if released:
        parts.append(f"{len(released)} released")
    relayed = [r["relayed"] for r in reps if r.get("relayed")]
    if relayed:
        parts.append(f"{len(relayed)} upgrade(s) dropped in the commons for the other agents")
    body = []
    for uid, key in (ports or [])[:8]:
        body.append(f"- port request filed from another agent's upgrade {uid}: {key}")
    for rel, why in new_holds[:8]:
        body.append(f"- held {rel}: {why}")
    for rel, key in filed:
        body.append(f"- build request filed for {rel}: {key}")
    for rel, why in released[:8]:
        body.append(f"- released {rel} ({why})")
    for rel, why in [(rel, why) for r in reps for rel, why in r["promoted"]][:8]:
        body.append(f"- {rel} promoted to niche (never ships): {why}")
    tail = f"Open holds: {len(st['holds'])}. `core_sync.py holds` lists them." if st["holds"] else ""
    return "\n".join([", ".join(parts) + ".", *body, tail]).strip()


# ---------------------------------------------------------------- run
def run(eng, vault: Path, core: Path, cfg: dict, dry: bool = False, no_build: bool = False, limit: int = 0) -> int:
    if not cfg.get("enabled", True):
        print(f"{SENTINEL} disabled")
        return 0
    st = state()
    head = git(["rev-parse", "HEAD"], vault).stdout.strip()
    if not head:
        print(f"{SENTINEL} no git HEAD in {vault}")
        return 1
    ident, niche, allow = eng.identity(), niche_patterns(cfg), allow_patterns(cfg)
    if not st["last_commit"] or not eng.core_ready(core):
        if dry:
            n = len(eng.files(head))
            debt = baseline_debt(eng, vault, head, ident, niche, allow)
            print(f"{SENTINEL} dry-run: would bootstrap {core} at {head[:7]}: {n} files ship, debt {sum(len(v) for v in debt.values())} lines in {len(debt)} files")
            return 0
        bootstrap(eng, vault, core, cfg, head)
        st["last_commit"] = st["baseline_commit"] = head
        st["debt"] = baseline_debt(eng, vault, head, ident, niche, allow)
        n_debt = sum(len(v) for v in st["debt"].values())
        _save(STATE, st)
        ledger({"commit": head[:7], "action": "bootstrap", "why": f"{core}; debt {n_debt} lines in {len(st['debt'])} files"})
        note(eng, f"template sync started: {eng.name} template written to {core} at {head[:7]}. Debt (organization literals already in working "
                  f"code, worked off over time, never a hold): {n_debt} lines in {len(st['debt'])} files. `core_sync.py debt` lists them.", cfg)
        print(f"{SENTINEL} bootstrapped {core} at {head[:7]} debt={n_debt}")
        return 0
    rc = relay_cfg(cfg, vault, eng)
    if head == st["last_commit"]:
        ports = relay_inbox(eng, cfg, st, rc, dry) if rc.get("enabled") else []
        if not dry:
            _save(STATE, st)
            if ports:
                note(eng, "port requests filed from the commons:\n" + "\n".join(f"- {uid}: {key}" for uid, key in ports), cfg)
        print(f"{SENTINEL} up to date at {head[:7]} holds={len(st['holds'])} ports={len(ports)}")
        return 0
    r = git(["rev-list", "--reverse", "--first-parent", f"{st['last_commit']}..{head}"], vault)
    shas = [s for s in (r.stdout or "").split() if s]
    if not shas:                        # history rewritten (reset / amend): read HEAD as the next commit
        _log(f"last synced commit {st['last_commit'][:7]} is not an ancestor of HEAD {head[:7]}; treating HEAD as the next commit")
        shas = [head]
    if limit:
        shas = shas[:limit]
    before_holds = set(st["holds"])
    reps, released, filed = [], [], []
    for sha in shas:
        try:
            rep = apply_commit(eng, vault, core, sha, cfg, st, ident, niche, allow, dry, rc)
        except Exception as e:  # noqa: BLE001
            _log(f"commit {sha[:7]} failed: {type(e).__name__}: {e}")
            print(f"{SENTINEL} failed at {sha[:7]}: {type(e).__name__}: {e}")
            if not dry:
                _save(STATE, st)
            return 1
        reps.append(rep)
        released += rep["released"]
        if not dry:
            st["last_commit"] = sha
            st["runs"] += 1
            _save(STATE, st)
        _log(f"{sha[:7]} {rep['subject'][:60]!r}: applied {len(rep['applied'])} held {len(rep['held'])} promoted {len(rep['promoted'])} "
             f"deleted {len(rep['deleted'])} skipped {len(rep['skipped'])} failed {len(rep['failed'])}")
    new_holds = [(rel, (st["holds"][rel].get("verify") or "; ".join(f"line {v.get('line')}: {v.get('why')}" for v in list((st["holds"][rel].get("lines") or {}).values())[:3])))
                 for rel in st["holds"] if rel not in before_holds]
    ports = relay_inbox(eng, cfg, st, rc, dry) if rc.get("enabled") and not no_build else []
    if not dry:
        if not no_build:
            filed = file_builds(eng, st, cfg)
        for rel in st["holds"]:
            st["holds"][rel]["noted"] = True
        _save(STATE, st)
        if cfg.get("niche_paths") != ((_load(vault / eng.config_rel, {}) or {}).get("niche_paths") or []):
            save_niche_paths(vault, eng, cfg.get("niche_paths") or [])
        if any(r["applied"] or r["held"] or r["promoted"] or r["deleted"] or r["failed"] for r in reps) or released or ports:
            note(eng, _summary(reps, st, released, new_holds, filed, core, ports), cfg)
    n_app = sum(len(r["applied"]) for r in reps)
    n_held = sum(len(r["held"]) + len(r["failed"]) for r in reps)
    print(f"{SENTINEL} {'dry-run ' if dry else ''}commits={len(reps)} applied={n_app} held={n_held} released={len(released)} "
          f"promoted={sum(len(r['promoted']) for r in reps)} relayed={sum(1 for r in reps if r.get('relayed'))} ports={len(ports)} "
          f"holds={len(st['holds'])} at {shas[-1][:7]}")
    return 0


# ---------------------------------------------------------------- cli
def _print_holds(st: dict) -> None:
    if not st["holds"]:
        print("no holds")
        return
    for rel, h in sorted(st["holds"].items()):
        print(f"{rel}  (held since {h.get('since')}{', build ' + h['build'] if h.get('build') else ''})")
        if h.get("verify"):
            print(f"   verify: {h['verify']}")
        for v in list((h.get("lines") or {}).values())[:10]:
            print(f"   line {v.get('line')}: {v.get('why')}")


def main() -> int:
    sys.stdout.reconfigure(encoding="utf-8")
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--vault", default=str(VAULT))
    ap.add_argument("--core", default="")
    ap.add_argument("--engine", default="auto", choices=("auto", "sage", "template"))
    ap.add_argument("--state", default="", help="state folder (default: the engine's; tests and dry runs point it elsewhere)")
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("run"); p.add_argument("--dry-run", action="store_true"); p.add_argument("--no-build", action="store_true")
    p.add_argument("--limit", type=int, default=0)
    sub.add_parser("bootstrap")
    p = sub.add_parser("refresh"); p.add_argument("--dry-run", action="store_true")
    p = sub.add_parser("audit"); p.add_argument("--quiet", action="store_true")
    p = sub.add_parser("people"); p.add_argument("--write", action="store_true")
    sub.add_parser("status"); sub.add_parser("holds"); sub.add_parser("debt")
    p = sub.add_parser("ledger"); p.add_argument("-n", type=int, default=30)
    p = sub.add_parser("classify"); p.add_argument("rel"); p.add_argument("--rev", default="HEAD")
    a = ap.parse_args()
    vault = Path(a.vault).resolve()
    cls = pick_engine(vault, a.engine)
    cfg = config(vault, cls)
    eng = cls(vault, cfg)
    init_paths(Path(a.state).resolve() if a.state else eng.state_dir())
    core = Path(a.core or cfg["core_root"]).expanduser().resolve()
    if a.cmd == "run":
        return run(eng, vault, core, cfg, dry=a.dry_run, no_build=a.no_build, limit=a.limit)
    if a.cmd == "bootstrap":
        st = state()
        if st["last_commit"]:
            print(f"already bootstrapped at {st['baseline_commit'][:7]} (core {core})")
            return 0
        return run(eng, vault, core, cfg)
    if a.cmd == "refresh":
        return refresh(eng, vault, core, cfg, dry=a.dry_run)
    if a.cmd == "audit":
        return audit(eng, vault, core, quiet=a.quiet)
    if a.cmd == "people":
        return people(eng, vault, core, cfg, write=a.write)
    st = state()
    if a.cmd == "status":
        head = git(["rev-parse", "--short", "HEAD"], vault).stdout.strip()
        behind = git(["rev-list", "--count", f"{st['last_commit']}..HEAD"], vault).stdout.strip() if st["last_commit"] else "?"
        print(f"engine: {eng.name}  vault: {vault}")
        print(f"core: {core} ({'ready' if eng.core_ready(core) else 'not exported yet'})")
        print(f"synced: {st['last_commit'][:7] or '-'}  head: {head}  behind: {behind}  runs: {st['runs']}")
        print(f"holds: {len(st['holds'])}  debt (at bootstrap): {sum(len(v) for v in st['debt'].values())} lines in {len(st['debt'])} files")
        return 0
    if a.cmd == "holds":
        _print_holds(st)
        return 0
    if a.cmd == "debt":
        for rel, rows in sorted(st["debt"].items()):
            print(f"{rel} ({len(rows)})")
            for r in rows[:6]:
                print(f"   line {r['line']}: {r['why']}  {r.get('text', '')[:80]}")
        if not st["debt"]:
            print("no debt recorded (bootstrap not run yet)" if not st["last_commit"] else "no debt")
        return 0
    if a.cmd == "ledger":
        try:
            rows = LEDGER.read_text(encoding="utf-8").splitlines()[-a.n:]
        except OSError:
            rows = []
        for ln in rows:
            print(ln)
        return 0
    if a.cmd == "classify":
        rel = a.rel.replace("\\", "/")
        ident, niche, allow = eng.identity(), niche_patterns(cfg), allow_patterns(cfg)
        print("ships" if eng.ships(rel) else eng.why_not(rel), rel)
        raw = _rev_text(vault, a.rev, rel)
        if raw is None:
            print("not in", a.rev)
            return 1
        text = _decode(raw)
        fl = flags_of(rel, text, ident, niche, allow)
        held = st["holds"].get(rel)
        for n, (k, lbl, t) in sorted(fl.items()):
            tag = "HOLD" if held and norm(t) in (held.get("lines") or {}) else "debt"
            print(f"  {tag:4} line {n}: {k} ({lbl})  {t[:90]}")
        if eng.name == "template":
            toks = flags_of(rel, text, eng.tokens(), [], [])
            for n, (k, lbl, t) in sorted(toks.items())[:20]:
                print(f"  tokn line {n}: {lbl} (tokenized on export)  {t[:80]}")
        print(f"{len(fl)} flagged working line(s); {'held' if held else 'not held'}")
        return 0
    return 0


if __name__ == "__main__":
    sys.exit(main())

"""form_ingest.py - a completed CRM intake form's answers filed onto the client's page, by code.

The owner, 2026-10-29, after the agent told a team member it had no location or times for a booked client while the
client's intake form had been complete in the CRM for days: "have you ingested the form data? If you haven't done that
yet, it needs to be a standard operation whenever a form comes filled in."

What this does (zero-LLM, a Day Spine job after the CRM snapshot):
  * the CRM snapshot (crm.forms) carries `answers` for every completed form file (the connector fetches them);
  * each one not yet filed is written onto its client's wiki page (the client pages folder, matched on the page's CRM
    id) as a block between `<!-- form:<id>:begin -->` / `:end -->` markers under "Story and notes", and the plain
    facts (location, start time, headcount, day-of contact, coordinator, plus whatever the organization's field map
    adds) go into the delivery ledger entry (scripts/projects/project_state.json), so every drafter and team-facing
    reply answers "where and when" from the record;
  * a page that already carries a form section written by hand is left alone and counted as filed; the note is
    linted alone (wiki_tool lint <path>) and reverted if it fails; the write is committed scoped;
  * data/form_ingest.json remembers what was filed, so this is idempotent.

Settings: config.json -> wiki.client_pages (the folder of client pages, default Wiki/Clients); data/form_fields.json
(the organization's own question -> ledger field map, [{"field": ..., "match": regex}], read before the generic
defaults below). Nothing here names a CRM, a person or an organization.

  python assistant/scripts/form_ingest.py [--dry-run] [--no-commit]
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
from alib import DATA, VAULT as _VAULT, config, load_json, save_json  # noqa: E402
import crm  # noqa: E402

VAULT = Path(os.environ.get("FORM_INGEST_VAULT") or _VAULT)
STATE = DATA / "form_ingest.json"
FIELD_MAP = DATA / "form_fields.json"
PROJECT_STATE = VAULT / "scripts" / "projects" / "project_state.json"
WIKI_TOOL = VAULT / "scripts" / "wiki_tool.py"
NO_WINDOW = 0x08000000 if os.name == "nt" else 0
DONE = ("completed", "complete", "answered", "submitted")
# generic question -> ledger field defaults; the organization's data/form_fields.json comes first
DEFAULT_FIELDS = [
    ("location", r"(location|address).*(name|event|main)|(event|main).*(location|address)|^(event )?location\b"),
    ("start_time", r"what time .*(start|begin)|(start|begin).*time|time.*(start|begin)"),
    ("headcount", r"how many (guests|people|attendees)|head ?count|guest count|number of (guests|attendees)"),
    ("day_of_contact", r"reached on the day|day[- ]of contact|on[- ]site contact|contact.*(on the )?day of"),
    ("coordinator", r"coordinator|planner"),
]
_SECTION_WORDS = ("form", "intake")        # + data/form_fields.json -> legacy_section_words (what the organization called its form)


def _filed_rx() -> re.Pattern:
    words = list(_SECTION_WORDS) + [str(w) for w in (load_json(FIELD_MAP, {}) or {}).get("legacy_section_words") or []]
    return re.compile(r"(?im)^#{2,4}\s.*(" + "|".join(re.escape(w) for w in words if w) + r").*\(completed")


def _name_keys() -> list:
    return [str(k) for k in (load_json(FIELD_MAP, {}) or {}).get("client_name_keys") or []] + ["client", "name"]


def pages_dir() -> Path:
    rel = os.environ.get("CLIENT_PAGES_DIR") or str(((config().get("wiki") or {}).get("client_pages")) or "Wiki/Clients")
    p = Path(rel)
    return p if p.is_absolute() else VAULT / p


def id_keys() -> list:
    """Front-matter / ledger keys that hold a CRM id: the contract's names plus the connector's own older ones."""
    al = crm._aliases() if hasattr(crm, "_aliases") else {}
    keys = ["crm_id", "crm_workspace_id", "thread_id", "workspace_id"]
    for k in ("crm_id", "thread_id"):
        keys += [a for a in (al.get(k) or []) if a not in keys]
    return keys


def fm_ids(text: str) -> set:
    out = set()
    rx = re.compile(r'^(?:' + "|".join(re.escape(k) for k in id_keys()) + r'):\s*"?([0-9a-f]{24})"?\s*$', re.M)
    for m in rx.finditer(text[:3000]):
        out.add(m.group(1))
    return out


def field_map() -> list:
    """[(field, compiled regex)]: the organization's map first, then the generic defaults."""
    out = []
    for row in (load_json(FIELD_MAP, {}) or {}).get("fields") or []:
        if isinstance(row, dict) and row.get("field") and row.get("match"):
            try:
                out.append((str(row["field"]), re.compile(str(row["match"]), re.I)))
            except re.error:
                continue
    for f, rx in DEFAULT_FIELDS:
        out.append((f, re.compile(rx, re.I)))
    return out


def completed(snap: dict) -> list:
    """Completed form files whose answers the snapshot carries."""
    out = []
    for q in crm.forms(snap) or []:
        if not isinstance(q, dict) or q.get("_error") or q.get("answers_error"):
            continue
        if str(q.get("status") or "").lower() not in DONE:
            continue
        ans = [a for a in (q.get("answers") or []) if isinstance(a, dict) and str(a.get("a") or "").strip()]
        if ans and q.get("id"):
            out.append(dict(q, answers=ans))
    return out


def client_page(ids: set) -> Path | None:
    """The client page whose front matter names one of these CRM ids."""
    d = pages_dir()
    if not d.is_dir():
        return None
    for p in sorted(d.glob("*.md")):
        try:
            head = p.read_text(encoding="utf-8", errors="replace")[:3000]
        except OSError:
            continue
        if ids & fm_ids(head):
            return p
    return None


def project_entry(ps: dict, ids: set) -> tuple:
    projects = ps.get("projects") if isinstance(ps.get("projects"), dict) else ps
    keys = id_keys()
    for key, e in (projects or {}).items():
        if isinstance(e, dict) and ids & {str(e.get(k) or "") for k in keys}:
            return key, e
    return "", None


def fields(answers: list, fmap: list | None = None) -> dict:
    out = {}
    for field, rx in (fmap if fmap is not None else field_map()):
        if field in out:
            continue
        for a in answers:
            if rx.search(str(a.get("q") or "")):
                out[field] = str(a.get("a")).strip()
                break
    return out


def when_done(q: dict) -> str:
    ats = sorted(str(a.get("at") or "")[:10] for a in q.get("answers") or [] if a.get("at"))
    return ats[-1] if ats else time.strftime("%Y-%m-%d")


def block(q: dict) -> str:
    name = re.sub(r"\s+", " ", str(q.get("name") or "Intake form")).strip()
    lines = [f"<!-- form:{q['id']}:begin -->",
             f"### {name} (completed {when_done(q)}, filed from {crm.name()})", ""]
    for a in q["answers"]:
        qq = re.sub(r"\s+", " ", str(a.get("q") or "")).strip().rstrip("?:")
        lines.append(f"- **{qq}:** {str(a.get('a')).strip()}")
    lines += [f"<!-- form:{q['id']}:end -->", ""]
    return "\n".join(lines)


def already_filed(text: str, qid: str) -> bool:
    legacy = any(f"<!-- {w}:{qid}:" in text for w in (load_json(FIELD_MAP, {}) or {}).get("legacy_section_words") or [])
    return f"<!-- form:{qid}:" in text or legacy or bool(_filed_rx().search(text))


def insert_block(text: str, blk: str) -> str:
    nl = "\r\n" if "\r\n" in text else "\n"
    blk = blk.replace("\n", nl)
    m = re.search(r"^<!-- sage:notes:begin -->[ \t]*\r?\n", text, re.M)
    if not m:
        m = re.search(r"^## Story and notes[ \t]*\r?\n(?:[ \t]*\r?\n)?", text, re.M)
    if m:
        return text[:m.end()] + blk + nl + text[m.end():]
    return text.rstrip() + nl + nl + "## Story and notes" + nl + nl + blk


def touch_updated(text: str) -> str:
    return re.sub(r"(?m)^updated:\s*\S+\s*$", f"updated: {time.strftime('%Y-%m-%d')}", text, count=1)


def git(*args) -> subprocess.CompletedProcess:
    return subprocess.run(["git", *args], cwd=str(VAULT), capture_output=True, text=True, encoding="utf-8",
                          errors="replace", creationflags=NO_WINDOW)


def lint_ok(page: Path) -> bool:
    if not WIKI_TOOL.is_file():
        return True
    r = subprocess.run([sys.executable, str(WIKI_TOOL), "lint", str(page)], cwd=str(VAULT), capture_output=True,
                       text=True, encoding="utf-8", errors="replace", creationflags=NO_WINDOW, timeout=120)
    return r.returncode == 0


def run(dry: bool = False, commit: bool = True) -> dict:
    snap = crm.snapshot() or {}
    st = load_json(STATE, {}) or {}
    st.setdefault("ingested", {})
    st.setdefault("unmatched", {})
    ps = load_json(PROJECT_STATE, {}) or {}
    fmap = field_map()
    filed, pages, names = [], [], []
    ps_changed = False
    for q in completed(snap):
        qid = str(q["id"])
        if qid in st["ingested"]:
            continue
        ids = {str(q.get("project_id") or ""), str(q.get("workspace_id") or "")} - {""}
        page = client_page(ids)
        key, entry = project_entry(ps, ids)
        if page is None and entry is None:
            st["unmatched"][qid] = time.strftime("%Y-%m-%d %H:%M")
            print(f"FORM unmatched {qid}: no client page or ledger entry for {sorted(ids)}")
            continue
        if page is not None:
            text = page.read_text(encoding="utf-8")
            if already_filed(text, qid):
                print(f"FORM already on the page: {page.name}")
            else:
                new = touch_updated(insert_block(text, block(q)))
                if dry:
                    print(f"DRY would file {len(q['answers'])} answers on {page.name}")
                else:
                    page.write_text(new, encoding="utf-8")
                    if not lint_ok(page):
                        page.write_text(text, encoding="utf-8")
                        print(f"FORM lint failed on {page.name}; page left as it was")
                        continue
                    pages.append(page)
        if entry is not None:
            f = fields(q["answers"], fmap)
            fe = dict(entry.get("form") or {})
            fe.update({"id": qid, "completed": when_done(q), **f})
            entry["form"] = fe
            for k, v in f.items():
                if not str(entry.get(k) or "").strip() or str(entry.get(k)).upper().startswith("TODO"):
                    entry[k] = v
            ps_changed = True
            names.append(next((str(entry[k]) for k in _name_keys() if entry.get(k)), key))
        st["ingested"][qid] = time.strftime("%Y-%m-%d %H:%M")
        filed.append(qid)
    if dry:
        return {"filed": filed, "dry": True}
    if ps_changed:
        save_json(PROJECT_STATE, ps)
    save_json(STATE, st)
    if commit and (pages or ps_changed) and (VAULT / ".git").exists():
        rel = [str(p.relative_to(VAULT)).replace("\\", "/") for p in pages]
        if ps_changed:
            rel.append(str(PROJECT_STATE.relative_to(VAULT)).replace("\\", "/"))
        git("add", "--", *rel)
        git("commit", "-q", "-m", f"wiki: intake form answers filed for {', '.join(sorted(set(names)) or ['a client'])}"
            "\n\nform_ingest.py (zero-LLM): a completed CRM form's answers onto the client's page and ledger entry.",
            "--", *rel)
    print(f"FORM_INGEST filed={len(filed)} pages={len(pages)} ledger={'yes' if ps_changed else 'no'}")
    return {"filed": filed, "pages": [str(p) for p in pages], "ledger": ps_changed}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--no-commit", action="store_true")
    a = ap.parse_args()
    run(dry=a.dry_run, commit=not a.no_commit)
    return 0


if __name__ == "__main__":
    sys.exit(main())

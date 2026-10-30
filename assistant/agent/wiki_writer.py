"""wiki_writer.py - the one path by which the agent adds knowledge to Wiki/.

Deterministic and narrow. The model supplies a title, a type, tags and a markdown body;
this code decides the folder, writes the front matter Schema/schema.md requires, keeps the
note `status: draft` (an owner verifies it), runs the wiki linter and makes a scoped commit.
An existing note is never rewritten: the body is appended as a dated section between
markers, so an undo can take exactly that section out again.

Refused: Packages (owned by the pricing engine), a NEW Couples page (the CRM sync creates
those), anything outside Wiki/, and any body that carries a secret (the schema's money rule).
Couples notes (2026-10-18): a body for an existing couple goes INSIDE the page's sync-safe
"## Story and notes" region (sage:notes markers), which the CRM sync never touches; type
"couple" (or a title naming the couple) routes there. couple_notes() is the one-call form.
"""
from __future__ import annotations

import os
import re
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import core  # noqa: E402

WIKI = core.VAULT / "Wiki"
SCHEMA = core.VAULT / "Schema" / "schema.md"
if not SCHEMA.is_file():                                   # the bare engine: only the generic contract
    SCHEMA = core.VAULT / "assistant" / "templates" / "Schema" / "schema.md"
CONFIG = core.VAULT / "assistant" / "config.json"
# this organization's routing, used when its Schema/schema.md carries no ```routing block (the generic schema the engine
# ships has one, so a new organization's notes land in the folders new_org.py made; 2026-10-29)
FOLDERS = {"person": "People", "asset": "Home", "account": "Money", "vendor": "Vendors", "venue": "Venues"}
LANE_FOLDERS = {"personal": "Personal", "home": "Home", "money": "Money", "wedding": "Operations"}
TYPES = ("person", "asset", "provider", "account", "concept", "reference", "workflow", "vendor", "venue", "couple")
_SECRET = re.compile(r"\b(?:password|passcode|pin|cvv|ssn|social security|routing|api key|secret key)\b\s*[:=]|\b\d{12,}\b", re.I)


def _schema_block(lang: str) -> str:
    """The body of the schema's fenced ```<lang> block ('' when absent): types, tags and routing are machine-read."""
    try:
        m = re.search(r"```" + lang + r"\n(.*?)```", SCHEMA.read_text(encoding="utf-8"), re.S)
        return m.group(1) if m else ""
    except OSError:
        return ""


def _allowed_tags() -> set:
    return set(_schema_block("tags").split())


def types() -> tuple:
    """The note types Schema/schema.md allows (its ```types block), else this organization's list."""
    return tuple(_schema_block("types").split()) or TYPES


def lanes() -> list:
    """assistant/config.json -> lanes (the first is the business lane)."""
    try:
        import json
        v = (json.loads(CONFIG.read_text(encoding="utf-8-sig")) or {}).get("lanes") or []
        return [str(x) for x in v]
    except (OSError, ValueError, AttributeError):
        return []


def default_lane() -> str:
    """personal when the organization has that lane (every owner's own notes), else its business lane."""
    ls = lanes()
    return "personal" if ("personal" in ls or not ls) else ls[0]


def routing() -> tuple:
    """({type: Folder}, {lane: Folder}, from_schema): the schema's ```routing block (`type x: Folder` / `lane y: Folder`
    lines), else this organization's defaults."""
    blk = _schema_block("routing")
    if not blk.strip():
        return dict(FOLDERS), dict(LANE_FOLDERS), False
    ty, la = {}, {}
    for ln in blk.splitlines():
        m = re.match(r"\s*(type|lane)\s+([\w-]+)\s*:\s*([\w][\w -]*?)\s*$", ln)
        if m:
            (ty if m.group(1) == "type" else la)[m.group(2).lower()] = m.group(3)
    return ty, la, True


def folder_for(typ: str, lane: str) -> str:
    """The Wiki/ subfolder a note goes to: by type first, then by lane; a lane with no rule routes to Wiki/<Lane>/ when
    that folder exists, the business lane to Operations, anything else to Personal."""
    ty, la, from_schema = routing()
    if typ in ty:
        return ty[typ]
    if typ == "provider" and not from_schema:
        return "Home" if lane == "home" else "Personal"
    if lane in la:
        return la[lane]
    cap = lane[:1].upper() + lane[1:]
    if lane and (WIKI / cap).is_dir():
        return cap
    ls = lanes()
    if ls and lane == ls[0]:
        return "Operations"
    return "Personal"


def _git(*args) -> bool:
    try:
        r = subprocess.run(creationflags=core.NO_WINDOW, args=["git", *args], cwd=str(core.VAULT), capture_output=True, text=True, timeout=60)
        return r.returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


def _lint_ok(rel: str = "") -> tuple[bool, str]:
    """Lint the one note just written (rel), not the whole wiki: an older note that fails the contract must not refuse
    every new one (2026-10-29: the onboarding blueprint did exactly that in a new organization). Wiki/_meta is exempt by
    the contract. No rel = the whole wiki."""
    try:
        r = subprocess.run(creationflags=core.NO_WINDOW, args=[sys.executable, "scripts/wiki_tool.py", "lint"] + ([rel] if rel else []), cwd=str(core.VAULT),
                           capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=120)
    except (OSError, subprocess.TimeoutExpired) as e:
        return False, str(e)
    out = (r.stdout or "") + (r.stderr or "")
    m = re.search(r"(\d+) errors", out)
    errs = [ln for ln in out.splitlines() if ln.startswith("ERROR")]
    return (m is not None and int(m.group(1)) == 0), "; ".join(errs[:3])


NOTES_BEGIN, NOTES_END = "<!-- sage:notes:begin -->", "<!-- sage:notes:end -->"
NOTES_HEADING = "## Story and notes"
COUPLES = WIKI / "Couples"


def find_couple(name: str):
    """The Couples note for a couple named loosely ("Ashcombe", "Teo and Wren", "2026-11-07 Ashcombe",
    "Teo Ashcombe's wedding"): the note whose file stem (date + surname) or H1 shares a word with the
    name, newest wedding first; None when nothing matches or two different couples do."""
    words = lambda s: set(re.findall(r"[a-z]{3,}", re.sub(r"'s\b", "", str(s or "").lower())))
    toks = words(name) - {"and", "the", "wedding", "couple", "couples", "project", "notes", "note", "story", "with", "for", "their", "file", "page"}
    if not toks or not COUPLES.is_dir():
        return None
    hits = []
    for p in sorted(COUPLES.glob("*.md"), reverse=True):
        stem_words = words(p.stem)
        try:
            h1 = re.search(r"^# (.+)$", p.read_text(encoding="utf-8"), re.M)
            stem_words |= words(h1.group(1) if h1 else "")
        except OSError:
            pass
        if toks & stem_words:
            hits.append(p)
    if len(hits) == 1:
        return hits[0]
    if len(hits) > 1:                                   # "Ashcombe" plus the date wins over "Ashcombe" alone
        exact = [p for p in hits if re.sub(r"\s+", " ", str(name or "")).strip().lower() == p.stem.lower()]
        return exact[0] if len(exact) == 1 else None
    return None


def insert_couple_notes(text: str, body: str, marker: str, name: str, today: str) -> str:
    """Put a dated section INSIDE the couple note's sync-safe region (## Story and notes between
    NOTES_BEGIN/NOTES_END). The region is created on first use, before '## Open items' when that
    heading exists, else at the end. The CRM sync script rewrites only its own sync begin/end block, so
    this region survives every nightly sync; the dated section's own marker keeps it undoable."""
    section = f"\n<!-- {marker}:begin -->\n### {today}, added by {name}\n\n{body.strip()}\n<!-- {marker}:end -->\n"
    if NOTES_BEGIN in text and NOTES_END in text:
        i = text.index(NOTES_END)
        return text[:i].rstrip("\n") + "\n" + section + text[i:]
    region = f"{NOTES_HEADING}\n\n{NOTES_BEGIN}" + section + f"{NOTES_END}\n"
    m = re.search(r"^## Open items\s*$", text, re.M)
    if m:
        return text[:m.start()].rstrip("\n") + "\n\n" + region + "\n" + text[m.start():]
    return text.rstrip("\n") + "\n\n" + region


def couple_notes(couple: str, body: str, by: str = "", commit: bool = True) -> dict:
    """File notes on a couple's wedding page (a meeting recording, a voice note, a call): the one path
    for every 'remember this about X's wedding'. -> write()'s result."""
    p = find_couple(couple)
    if not p:
        return {"status": "refused", "reason": f"no Couples note matches {couple!r}; name them the way the page is named (date + surname)"}
    return write({"title": p.stem, "type": "couple", "lane": "wedding", "tags": ["booking"], "body": body}, by=by, commit=commit)


def _gap(kind: str, name: str, lane: str, title: str, by: str, ask: str) -> None:
    """A type or tag the schema lacks: recorded for the nightly growth (schema_growth.py), never fatal."""
    try:
        import schema_growth
        schema_growth.record_gap(kind, name, lane, title, by, ask)
    except Exception:  # noqa: BLE001
        pass


def write(note: dict, by: str = "", commit: bool = True, ask: str = "") -> dict:
    """-> {"status": applied|appended|refused, "path", "reason"[, "suggest"]}. ask = the words that led to the note (an
    owner saying "make a page type for X" there grows the schema the same night)."""
    title = re.sub(r'[\\/:*?"<>|#^\[\]]+', " ", str(note.get("title") or "")).strip()[:90]
    title = re.sub(r"\s+", " ", title)
    body = str(note.get("body") or "").strip()
    typ = str(note.get("type") or "reference").lower()
    lane = str(note.get("lane") or default_lane()).lower()
    if len(title) < 3 or len(body) < 20:
        return {"status": "refused", "reason": "a note needs a title and a real body"}
    allowed_types = types()
    if typ not in allowed_types:
        # a gap, not just a no (2026-10-29): recorded for the nightly growth, and the nearest allowed type is named so the
        # turn can still file the note under it
        _gap("type", typ, lane, title, by, ask)
        try:
            import schema_growth
            near = schema_growth.nearest_type(typ, allowed_types)
        except Exception:  # noqa: BLE001
            near = "reference" if "reference" in allowed_types else ""
        return {"status": "refused", "reason": f"type {typ!r} is not in the schema yet (recorded as a gap); nearest allowed type: {near}"
                + f" (allowed: {', '.join(allowed_types)})", "suggest": near, "gap": typ}
    if typ == "couple" and not (find_couple(title) or next((p for p in COUPLES.glob(f"{title}.md")), None)):
        return {"status": "refused", "reason": f"no Couples note matches {title!r}: a couple's page is created by the CRM sync, notes are added to it"}
    if _SECRET.search(body):
        return {"status": "refused", "reason": "the note looks like it holds a secret or an account number"}
    folder = folder_for(typ, lane)
    allowed_tags = _allowed_tags()
    for t in (note.get("tags") or [])[:6]:
        if isinstance(t, str) and t.strip() and t not in allowed_tags and allowed_tags:
            _gap("tag", t, lane, title, by, ask)
    tags = [t for t in (note.get("tags") or []) if t in allowed_tags][:6] or [t for t in (lane, "personal") if t in allowed_tags][:1]
    today = time.strftime("%Y-%m-%d")
    name = core.profile().get("name", "agent")
    existing = next((p for p in WIKI.rglob(f"{title}.md") if "_meta" not in p.parts), None)
    if not existing and typ == "couple":
        existing = find_couple(title)
    marker = f"agent:{time.strftime('%Y%m%d-%H%M%S')}"
    if existing:
        if existing.parent.name == "Packages":
            return {"status": "refused", "reason": "Packages notes are maintained by the pricing engine"}
        old = existing.read_text(encoding="utf-8")
        new = re.sub(r"^updated: .*$", f"updated: {today}", old, count=1, flags=re.M)
        if existing.parent.name == "Couples":
            # the CRM sync rewrites only its hb:begin/end block, so a story-and-notes region of our
            # own is safe there (2026-10-18: Teo & Wren's meeting recording was refused as "maintained
            # by their own sync" and Sage told the owner it had been filed; see insert_couple_notes)
            new = insert_couple_notes(new, body, marker, name, today)
        else:
            new = new.rstrip("\n") + f"\n\n<!-- {marker}:begin -->\n## Added by {name}, {today}\n\n{body}\n<!-- {marker}:end -->\n"
        existing.write_text(new, encoding="utf-8")
        path, status, before = existing, "appended", old
    else:
        path = WIKI / folder / f"{title}.md"
        path.parent.mkdir(parents=True, exist_ok=True)
        # sources: Raw/ files that exist (an upload from chat, a Drive document) or wikilinks; anything else is dropped
        srcs = [str(s).strip() for s in (note.get("sources") or []) if isinstance(s, str)]
        srcs = [s for s in srcs if (s.startswith("[[") and s.endswith("]]")) or (s.startswith("Raw/") and (core.VAULT / s).is_file())][:8]
        fm = ["---", f"type: {typ}", f"area: {folder.lower()}", f"tags: [{', '.join(tags)}]", f"created: {today}",
              f"updated: {today}", "sources: [" + ", ".join(f'"{s}"' for s in srcs) + "]", "status: draft", "---", "", f"# {title}", "",
              f"> [!note] Written by {name} from chat on {today}. Draft until an owner verifies it.", "", body, ""]
        path.write_text("\n".join(fm), encoding="utf-8")
        status, before = "applied", None
    ok, why = _lint_ok(path.relative_to(core.VAULT).as_posix())
    if not ok:                                             # never leave the wiki failing its own contract
        if before is None:
            path.unlink(missing_ok=True)
        else:
            path.write_text(before, encoding="utf-8")
        return {"status": "refused", "reason": f"the wiki linter rejected it ({why or 'lint failed'})"}
    rel = path.relative_to(core.VAULT).as_posix()
    if commit:
        _git("add", "--", rel)
        _git("commit", "-q", "-m", f"agent: wiki note '{title}' ({status}, from {by or 'chat'})", "--", rel)
    cid = core.log_change("note", f"wiki note {rel} ({status})",
                          {"op": "note_remove", "path": rel, "marker": marker if existing else ""}, by)
    reindex()
    return {"status": status, "path": rel, "change": cid}


import gmcp  # noqa: E402

VENV_PY = gmcp.VENV_PY          # org.json -> semantic_python, else this machine's vault-mem venv


def reindex() -> None:
    """Rebuild the wiki index and the semantic store, detached, so the agent's vault_search sees
    the note it just wrote (2026-10-09: 'cat litter' found a colour-grade note; the store was 4 days
    stale). Skipped in tests (scoped state) and when the venv is missing."""
    if core.STATE != core.AGENT / "state" or os.environ.get("AGENT_NO_REINDEX"):
        return
    flags = 0x00000008 | 0x00000200 | core.NO_WINDOW if os.name == "nt" else 0
    try:
        subprocess.Popen([sys.executable, "scripts/wiki_tool.py", "index"], cwd=str(core.VAULT), creationflags=flags,
                         stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        if VENV_PY.exists():
            subprocess.Popen([str(VENV_PY), "scripts/semantic.py", "build"], cwd=str(core.VAULT), creationflags=flags,
                             stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except OSError:
        pass


def remove(rel: str, marker: str = "") -> bool:
    """Undo for write(): take out the appended section, or retire the note this agent created."""
    path = (core.VAULT / rel).resolve()
    if WIKI.resolve() not in path.parents or not path.exists():
        return False
    if marker:
        text = path.read_text(encoding="utf-8")
        new = re.sub(r"\n*<!-- " + re.escape(marker) + r":begin -->.*?<!-- " + re.escape(marker) + r":end -->\n?", "\n",
                     text, flags=re.S)
        if new == text:
            return False
        path.write_text(new, encoding="utf-8")
        _git("commit", "-q", "-m", f"agent: undo note section in {rel}", "--", rel)
        return True
    if not _git("rm", "-q", "--", rel):                    # git keeps the history; an untracked file is left alone
        return False
    _git("commit", "-q", "-m", f"agent: undo wiki note {rel}", "--", rel)
    return True


if __name__ == "__main__":
    # python assistant/agent/wiki_writer.py gaps [--all]: the types and tags asked for that the schema lacks
    sys.stdout.reconfigure(encoding="utf-8")
    if len(sys.argv) > 1 and sys.argv[1] == "gaps":
        import schema_growth
        sys.exit(schema_growth.print_gaps("--all" in sys.argv[2:]))
    print("usage: wiki_writer.py gaps [--all]")
    sys.exit(2)

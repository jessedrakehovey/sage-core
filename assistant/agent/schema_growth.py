"""schema_growth.py - the wiki schema grows with the organization, by code (2026-10-29).

The engine ships a GENERIC Schema/schema.md (assistant/templates/Schema). Its fenced ```types, ```tags and ```routing
blocks are machine-read by the note writer (wiki_writer.py) and the linter (scripts/wiki_tool.py). This module is the one
path by which those blocks change on their own, in three seams:

  seed    Discovery / Training learned the business type (a lifecycle skeleton in assistant/onboarding/lifecycles/, whose
          `wiki` section names the core nouns, the stage tags and their folders) and the owner's own words for the things
          the business tracks. The first custom vocabulary is written once: a type per noun, a short tag set from the
          stage names, a routing line per type and Wiki/<Folder>/about.md.
  gap     the note writer was asked for a type or tag the schema lacks: the ask is recorded (DATA/schema_gaps.jsonl: the
          wanted name, lane, title, who asked, when, the asker's words) and the refusal names the nearest allowed type,
          so the turn can still file the note.
  grow    the nightly dream turns a gap asked for at least twice, or once by an owner in so many words ("make a page
          type for units"), into a one-line addition to a block (+ the routing line, the folder and its about.md).

Rails: only ADDS lines, never removes or renames an existing type, tag or routing rule; a name must be a plain lowercase
word; the protected core is never a type; every change is linted (scripts/wiki_tool.py lint) and rolled back when the
error count rises or the schema stops parsing; every change is ledgered (core.log_change, op schema_remove) and undoable,
and an undone name is latched so the next night does not add it back. A schema with no ```routing block (an older
organization that routes by code) gets no routing line and no folder: adding a routing block would re-route every note.
Switch: assistant/config.json -> wiki.schema_growth (default on).

  python assistant/agent/schema_growth.py gaps [--all]
  python assistant/agent/schema_growth.py grow [--dry-run]
  python assistant/agent/schema_growth.py seed            (from assistant/data/onboarding.json)
"""
from __future__ import annotations

import argparse
import difflib
import json
import re
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import core  # noqa: E402

SCHEMA_REL = "Schema/schema.md"
GAPS = core.DATA / "schema_gaps.jsonl"
LEDGER = core.DATA / "schema_growth.json"          # {"grown": {"type:unit": stamp}, "declined": {"type:unit": stamp}}
CONFIG = core.VAULT / "assistant" / "config.json"
GROW_MIN_ASKS = 2
GROW_PER_NIGHT = 3
NAME_RX = re.compile(r"^[a-z][a-z0-9]*(?:-[a-z0-9]+)*$")
# an owner asking in so many words: "make a page type for units", "add a tag for warranty", "new note type called recipe"
EXPLICIT_RX = re.compile(r"\b(?:page|note|wiki)\s+type\b|\btype\s+(?:for|called|named)\b|\bnew\s+(?:page\s+|note\s+)?type\b"
                         r"|\btag\s+(?:for|called|named)\b|\bnew\s+tag\b", re.I)
# generic nearest-type hints (the wanted word -> the generic type it usually is); a near spelling of an allowed type wins first
SYNONYMS = {
    "customer": "client", "patient": "client", "student": "client", "member": "client", "tenant": "client", "guest": "client",
    "lead": "client", "prospect": "client",
    "job": "project", "order": "project", "engagement": "project", "booking": "project", "case": "project", "matter": "project",
    "reservation": "project", "rental": "project", "deal": "project", "event": "project", "session": "project",
    "supplier": "vendor", "contractor": "vendor", "partner": "vendor", "subcontractor": "vendor", "manufacturer": "vendor",
    "contact": "person", "staff": "person", "employee": "person", "friend": "person", "family": "person", "team-member": "person",
    "equipment": "asset", "unit": "asset", "property": "asset", "vehicle": "asset", "device": "asset", "product": "asset",
    "item": "asset", "tool": "asset", "appliance": "asset",
    "procedure": "workflow", "process": "workflow", "how-to": "workflow", "howto": "workflow", "sop": "workflow",
    "checklist": "reference", "price-list": "reference", "spec": "reference", "template": "reference",
    "policy": "concept", "topic": "concept", "idea": "concept",
    "bank": "account", "card": "account", "loan": "account", "subscription": "account", "insurance": "account",
    "software": "integration", "app": "integration", "system": "integration", "crm": "integration",
}


# ------------------------------------------------------------------ the schema text
def schema_path() -> Path:
    return core.VAULT / SCHEMA_REL


def _read() -> str:
    try:
        return schema_path().read_text(encoding="utf-8")
    except OSError:
        return ""


def block(text: str, lang: str):
    """The lines of the fenced ```<lang> block (stripped, non-empty), or None when the block is absent."""
    m = re.search(r"```" + lang + r"\n(.*?)```", text, re.S)
    return [ln.strip() for ln in m.group(1).splitlines() if ln.strip()] if m else None


def add_lines(text: str, lang: str, lines: list) -> str:
    """Append lines at the end of the ```<lang> block (only those not already there); the text unchanged when absent."""
    m = re.search(r"(```" + lang + r"\n)(.*?)(```)", text, re.S)
    if not m:
        return text
    have = {ln.strip() for ln in m.group(2).splitlines()}
    new = []
    for ln in lines:
        if ln.strip() and ln.strip() not in have and ln.strip() not in new:
            new.append(ln.strip())
    if not new:
        return text
    body = m.group(2) if (m.group(2).endswith("\n") or not m.group(2)) else m.group(2) + "\n"
    return text[:m.start(2)] + body + "".join(ln + "\n" for ln in new) + text[m.end(2):]


def remove_lines(text: str, lang: str, lines: list) -> str:
    m = re.search(r"(```" + lang + r"\n)(.*?)(```)", text, re.S)
    if not m:
        return text
    drop = {ln.strip() for ln in lines}
    kept = [ln for ln in m.group(2).splitlines(keepends=True) if ln.strip() not in drop]
    return text[:m.start(2)] + "".join(kept) + text[m.end(2):]


def validate(text: str) -> list:
    """Problems that would break the machine readers: a missing types/tags block, a bad or duplicate name, a routing line
    that does not parse."""
    out = []
    for lang in ("types", "tags"):
        b = block(text, lang)
        if b is None:
            out.append(f"no ```{lang} block")
            continue
        bad = [x for x in b if not NAME_RX.match(x)]
        if bad:
            out.append(f"{lang}: not a plain lowercase name: {bad[:3]}")
        if len(set(b)) != len(b):
            out.append(f"{lang}: duplicate lines")
    r = block(text, "routing")
    if r is not None:
        for ln in r:
            if not re.match(r"(type|lane)\s+[\w-]+\s*:\s*[\w][\w -]*$", ln):
                out.append(f"routing line does not parse: {ln!r}")
    return out


def lint_errors():
    """The wiki linter's error count against the current schema (None = it did not run or the schema did not parse)."""
    try:
        r = subprocess.run(creationflags=core.NO_WINDOW, args=[sys.executable, "scripts/wiki_tool.py", "lint"], cwd=str(core.VAULT),
                           capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=180)
    except (OSError, subprocess.TimeoutExpired):
        return None
    m = re.search(r"(\d+) errors", (r.stdout or "") + (r.stderr or ""))
    return int(m.group(1)) if m else None


# ------------------------------------------------------------------ names
def norm(name: str) -> str:
    """'Work Orders' -> 'work-order', 'Properties' -> 'property', 'units' -> 'unit'."""
    w = re.sub(r"[^a-z0-9]+", "-", str(name or "").lower()).strip("-")
    if w.endswith("ies") and len(w) > 4:
        w = w[:-3] + "y"
    elif w.endswith(("sses", "ches", "shes", "xes")):
        w = w[:-2]
    elif w.endswith("s") and not w.endswith(("ss", "us", "is")) and len(w) > 3:
        w = w[:-1]
    return w


def folder_of(name: str) -> str:
    """'unit' -> 'Units', 'property' -> 'Properties', 'work-order' -> 'Work Orders'."""
    words = [x for x in str(name or "").split("-") if x]
    if not words:
        return ""
    last = words[-1]
    if last.endswith("y") and len(last) > 2 and last[-2] not in "aeiou":
        last = last[:-1] + "ies"
    elif last.endswith(("s", "x", "ch", "sh")):
        last = last + "es"
    else:
        last = last + "s"
    words[-1] = last
    return " ".join(x[:1].upper() + x[1:] for x in words)


def nearest_type(want: str, allowed=None) -> str:
    """The allowed type closest to a wanted one: a near spelling first, then the generic synonym, else reference."""
    allowed = list(allowed if allowed is not None else (block(_read(), "types") or []))
    if not allowed:
        return ""
    w = norm(want)
    if w in allowed:
        return w
    close = difflib.get_close_matches(w, allowed, n=1, cutoff=0.75)
    if close:
        return close[0]
    syn = SYNONYMS.get(w) or SYNONYMS.get(w.split("-")[-1])
    if syn in allowed:
        return syn
    for fallback in ("reference", "concept", "workflow"):
        if fallback in allowed:
            return fallback
    return allowed[0]


def enabled() -> bool:
    try:
        cfg = json.loads(CONFIG.read_text(encoding="utf-8-sig")) or {}
        return bool((cfg.get("wiki") or {}).get("schema_growth", True))
    except (OSError, ValueError, AttributeError):
        return True


# ------------------------------------------------------------------ gaps (seam 2)
def _is_owner(by: str) -> bool:
    try:
        return str(by or "") in core.owners()
    except Exception:  # noqa: BLE001
        return False


def explicit(ask: str, name: str) -> bool:
    """An ask that names the new type or tag in so many words."""
    a = str(ask or "").lower()
    if not a or not EXPLICIT_RX.search(a):
        return False
    w = norm(name)
    return any(x and x in a for x in (w, w.replace("-", " "), folder_of(w).lower()))


def record_gap(kind: str, name: str, lane: str = "", title: str = "", by: str = "", ask: str = "") -> dict:
    """One wanted type or tag the schema lacks. -> the row written ({} when the name is not worth recording)."""
    w = norm(name)
    if kind not in ("type", "tag") or not w or not NAME_RX.match(w) or len(w) > 32:
        return {}
    owner = _is_owner(by)
    row = {"ts": core.stamp(), "kind": kind, "name": w, "lane": str(lane or ""), "title": str(title or "")[:120],
           "by": str(by or ""), "owner": owner, "explicit": owner and explicit(ask, w), "ask": str(ask or "")[:300]}
    try:
        core.append(GAPS, row)
    except Exception:  # noqa: BLE001
        return {}
    return row


def _ledger() -> dict:
    d = core.load(LEDGER, {}) or {}
    d.setdefault("grown", {})
    d.setdefault("declined", {})
    return d


def gaps(include_closed: bool = False) -> list:
    """Gaps grouped by (kind, name): [{kind, name, asks, explicit, lanes, titles, by, first, last, state}] newest first.
    state: ready (asked twice, or once by an owner in so many words), open, in schema, declined (an undone growth)."""
    text = _read()
    have = {"type": set(block(text, "types") or []), "tag": set(block(text, "tags") or [])}
    led = _ledger()
    groups: dict = {}
    for r in core.rows(GAPS):
        k = (r.get("kind"), r.get("name"))
        if not all(k) or k[0] not in have:
            continue
        g = groups.setdefault(k, {"kind": k[0], "name": k[1], "asks": 0, "explicit": False, "lanes": [], "titles": [], "by": [],
                                  "first": r.get("ts", ""), "last": ""})
        # an ask = a distinct note (title): the same note retried, or refused and re-filed, is still one ask
        ask_key = str(r.get("title") or "").strip().lower() or f"#{r.get('ts')}-{g['asks']}"
        if ask_key not in g.setdefault("_keys", set()):
            g["_keys"].add(ask_key)
            g["asks"] += 1
        g["explicit"] = g["explicit"] or bool(r.get("explicit"))
        for key, val in (("lanes", r.get("lane")), ("titles", r.get("title")), ("by", r.get("by"))):
            if val and val not in g[key]:
                g[key].append(val)
        g["last"] = r.get("ts", "")
    out = []
    for (kind, name), g in groups.items():
        g.pop("_keys", None)
        key = f"{kind}:{name}"
        g["state"] = ("in schema" if name in have[kind] else "declined" if key in led["declined"]
                      else "ready" if (g["asks"] >= GROW_MIN_ASKS or g["explicit"]) else "open")
        if include_closed or g["state"] in ("open", "ready"):
            out.append(g)
    return sorted(out, key=lambda g: g["last"], reverse=True)


# ------------------------------------------------------------------ one change, linted, ledgered (seams 1 and 3)
def _about(folder: str, what: str) -> str:
    return (f"# {folder}\n\n{what}\n\nNotes here follow `Schema/schema.md`. The agent added this folder to the schema's routing "
            f"when the organization first needed it; undoing that change retires it again.\n")


def change(adds: dict, abouts: dict, by: str, summary: str, latch: list, commit: bool = True) -> dict:
    """adds {lang: [lines]} into the schema blocks; abouts {Folder: what} -> Wiki/<Folder>/about.md (only when missing).
    Linted: rolled back when the schema stops parsing or the wiki linter's error count rises. -> {status, change, reason}"""
    path = schema_path()
    before = _read()
    if not before:
        return {"status": "refused", "reason": "no Schema/schema.md"}
    text = before
    for lang, lines in adds.items():
        text = add_lines(text, lang, lines)
    if text == before:
        return {"status": "noop", "reason": "already in the schema"}
    probs = validate(text)
    if probs:
        return {"status": "refused", "reason": "; ".join(probs[:3])}
    errs_before = lint_errors()
    path.write_text(text, encoding="utf-8")
    created = []
    for folder, what in abouts.items():
        d = core.VAULT / "Wiki" / folder
        if (d / "about.md").exists():
            continue
        new_dir = not d.exists()
        d.mkdir(parents=True, exist_ok=True)
        (d / "about.md").write_text(_about(folder, what), encoding="utf-8")
        created.append({"folder": folder, "dir": new_dir})
    errs_after = lint_errors()
    if errs_after is None or (errs_before is not None and errs_after > errs_before):
        path.write_text(before, encoding="utf-8")
        _drop_folders(created)
        return {"status": "refused", "reason": f"the wiki linter rejected the schema change ({errs_before} -> {errs_after} errors); rolled back"}
    added = {lang: [ln for ln in block(text, lang) or [] if ln not in set(block(before, lang) or [])] for lang in adds}
    added = {k: v for k, v in added.items() if v}
    rels = [SCHEMA_REL] + [f"Wiki/{c['folder']}/about.md" for c in created]
    if commit:
        import wiki_writer
        wiki_writer._git("add", "--", *rels)
        wiki_writer._git("commit", "-q", "-m", f"agent: {summary[:90]} (from {by or 'agent'})", "--", *rels)
    cid = core.log_change("schema", summary, {"op": "schema_remove", "lines": added, "folders": created, "latch": latch}, by)
    led = _ledger()
    for k in latch:
        led["grown"][k] = core.stamp()
        led["declined"].pop(k, None)
    core.save(LEDGER, led)
    return {"status": "applied", "change": cid, "added": added, "folders": [c["folder"] for c in created]}


def _drop_folders(created: list) -> None:
    for c in created:
        d = core.VAULT / "Wiki" / c["folder"]
        try:
            (d / "about.md").unlink(missing_ok=True)
            if c.get("dir") and d.is_dir() and not any(d.iterdir()):
                d.rmdir()
        except OSError:
            pass


def in_use(lines: dict) -> list:
    """Wiki notes whose front matter uses one of these types or tags (vault-relative paths)."""
    types, tags = set(lines.get("types") or []), set(lines.get("tags") or [])
    if not types and not tags:
        return []
    out = []
    for p in sorted((core.VAULT / "Wiki").rglob("*.md")):
        if "_meta" in p.parts or p.name.lower() == "about.md":
            continue
        try:
            head = p.read_text(encoding="utf-8", errors="replace")[:1500]
        except OSError:
            continue
        m = re.search(r"^type:\s*([\w-]+)", head, re.M)
        g = re.search(r"^tags:\s*\[(.*?)\]", head, re.M)
        ntags = {x.strip().strip("\"'") for x in (g.group(1).split(",") if g else [])}
        if (m and m.group(1) in types) or (ntags & tags):
            out.append(p.relative_to(core.VAULT).as_posix())
    return out


def remove(u: dict) -> bool:
    """Undo for change(): take the added lines out again, retire an about.md this change created (the folder too when it
    is empty: a note filed there since is never touched), and latch the names so growth does not add them back."""
    text = _read()
    if not text:
        return False
    used = in_use(u.get("lines") or {})
    if used:                                           # taking a name out from under a note would fail the wiki's own lint
        print(f"schema undo refused: still used by {', '.join(used[:3])}", file=sys.stderr)
        return False
    new = text
    for lang, lines in (u.get("lines") or {}).items():
        new = remove_lines(new, lang, lines)
    if new == text and not u.get("folders"):
        return False
    schema_path().write_text(new, encoding="utf-8")
    _drop_folders(u.get("folders") or [])
    led = _ledger()
    for k in u.get("latch") or []:
        led["grown"].pop(k, None)
        led["declined"][k] = core.stamp()
    core.save(LEDGER, led)
    try:
        import wiki_writer
        rels = [SCHEMA_REL] + [f"Wiki/{c['folder']}/about.md" for c in (u.get("folders") or [])]
        wiki_writer._git("add", "-A", "--", *rels)
        wiki_writer._git("commit", "-q", "-m", "agent: undo a schema addition", "--", *rels)
    except Exception:  # noqa: BLE001
        pass
    return True


def _lines_for(kind: str, name: str, routed: bool) -> tuple:
    """-> ({lang: [lines]}, {Folder: what}) for one new type or tag."""
    if kind == "tag":
        return {"tags": [name]}, {}
    if not routed:
        return {"types": [name]}, {}
    folder = folder_of(name)
    return ({"types": [name], "routing": [f"type {name}: {folder}"]},
            {folder: f"One note per {name.replace('-', ' ')} the organization keeps track of (type `{name}`)."})


# ------------------------------------------------------------------ seam 3: the nightly growth
def grow(dry: bool = False, by: str = "", limit: int = GROW_PER_NIGHT, commit: bool = True) -> list:
    """Every ready gap (asked twice, or once by an owner in so many words) becomes a schema line, at most `limit` a night.
    -> [{kind, name, status, reason|change}]"""
    if not enabled():
        return []
    by = by or (core.profile().get("slug", "agent") + "-dream")
    routed = block(_read(), "routing") is not None
    out = []
    for g in gaps():
        if g["state"] != "ready":
            continue
        if len(out) >= limit:
            break
        name, kind = g["name"], g["kind"]
        if core.is_protected(name.replace("-", " ")):
            out.append({"kind": kind, "name": name, "status": "refused", "reason": "protected core"})
            continue
        why = "an owner asked for it" if g["explicit"] else f"asked for {g['asks']} times"
        if dry:
            out.append({"kind": kind, "name": name, "status": "would-apply", "reason": why})
            continue
        adds, abouts = _lines_for(kind, name, routed)
        where = f" (Wiki/{folder_of(name)})" if abouts else ""
        summary = (f"schema: a page type for {name.replace('-', ' ')}{where}, {why}" if kind == "type"
                   else f"schema: a tag for {name}, {why}")
        x = change(adds, abouts, by, summary, [f"{kind}:{name}"], commit=commit)
        out.append({"kind": kind, "name": name, "status": x["status"], "reason": x.get("reason", "") or x.get("change", "")})
    return out


# ------------------------------------------------------------------ seam 1: the onboarding seed
def seed_plan(skel: dict, tracks=None) -> dict:
    """What the lifecycle skeleton's `wiki` section and the owner's own words for the things they track add to the schema
    as it stands. -> {"types": [(name, folder, what)], "tags": [..]}"""
    text = _read()
    have_t, have_g = set(block(text, "types") or []), set(block(text, "tags") or [])
    w = (skel or {}).get("wiki") or {}
    folders = {norm(k): str(v) for k, v in (w.get("folders") or {}).items()}
    types, seen = [], set(have_t)
    words = {}                                          # the owner's word -> a skeleton type ("jobs" -> work-order)
    for t in w.get("types") or []:
        t = t if isinstance(t, dict) else {"type": t}
        n = norm(t.get("type", ""))
        for x in [n] + [norm(y) for y in (t.get("words") or [])]:
            words.setdefault(x, n)
        if n and NAME_RX.match(n) and n not in seen:
            types.append((n, folders.get(n) or folder_of(n), str(t.get("what") or "")))
            seen.add(n)
    for raw in (tracks or []):
        n = norm(raw)
        n = words.get(n, n)
        if n in seen or not n or not NAME_RX.match(n) or len(n) > 32 or core.is_protected(n.replace("-", " ")):
            continue
        if SYNONYMS.get(n) in have_t:                   # the owner's noun is a generic type under another name ("customers")
            continue
        types.append((n, folders.get(n) or folder_of(n), f"One note per {n.replace('-', ' ')} the business tracks (the owner's word)."))
        seen.add(n)
    tags = []
    for x in w.get("tags") or []:
        x = re.sub(r"[^a-z0-9]+", "-", str(x).lower()).strip("-")
        if x and NAME_RX.match(x) and x not in have_g and x not in tags:
            tags.append(x)
    return {"types": types, "tags": tags}


def seed(skel: dict, tracks=None, by: str = "onboarding", commit: bool = True) -> dict:
    """Write the first custom vocabulary (idempotent: what is already in the schema is skipped). -> change()'s result + plan"""
    routed = block(_read(), "routing") is not None
    if not routed:                     # a hand-built schema (no routing block) is the organization's own: never seeded over
        return {"status": "skipped", "reason": "the schema has no routing block (a hand-built contract); seeding is for the generic one",
                "plan": {"types": [], "tags": []}}
    plan = seed_plan(skel, tracks)
    if not plan["types"] and not plan["tags"]:
        return {"status": "noop", "plan": plan}
    adds = {"types": [t[0] for t in plan["types"]], "tags": plan["tags"], "routing": [f"type {n}: {f}" for n, f, _ in plan["types"]]}
    abouts = {f: (what or f"One note per {n.replace('-', ' ')} (type `{n}`).") for n, f, what in plan["types"]}
    latch = [f"type:{t[0]}" for t in plan["types"]] + [f"tag:{t}" for t in plan["tags"]]
    summary = (f"schema: seeded for a {(skel or {}).get('name') or 'new'} business: "
               + (", ".join(t[0] for t in plan["types"]) or "no new types") + (f" + {len(plan['tags'])} tags" if plan["tags"] else ""))
    x = change(adds, abouts, by, summary, latch, commit=commit)
    x["plan"] = plan
    return x


def folders() -> list:
    """The folders the schema's routing names (type and lane rules), for the blueprint's links."""
    out = []
    for ln in block(_read(), "routing") or []:
        m = re.match(r"(?:type|lane)\s+[\w-]+\s*:\s*(.+)$", ln)
        if m and m.group(1).strip() not in out:
            out.append(m.group(1).strip())
    return out


# ------------------------------------------------------------------ cli
def print_gaps(include_closed: bool = False) -> int:
    gs = gaps(include_closed)
    if not gs:
        print("GAPS none" + ("" if include_closed else " open"))
        return 0
    for g in gs:
        print(f"{g['kind']:<4} {g['name']:<22} asks={g['asks']:<3} {g['state']:<9} "
              + ("owner-asked " if g["explicit"] else "") + f"last={g['last']} lanes={','.join(g['lanes'])} "
              + f"e.g. {(g['titles'] or [''])[0][:60]!r}")
    print(f"GAPS {len(gs)} ({sum(1 for g in gs if g['state'] == 'ready')} ready for the nightly growth)")
    return 0


def main() -> int:
    sys.stdout.reconfigure(encoding="utf-8")
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("gaps"); p.add_argument("--all", action="store_true")
    p = sub.add_parser("grow"); p.add_argument("--dry-run", action="store_true")
    sub.add_parser("seed")
    a = ap.parse_args()
    if a.cmd == "gaps":
        return print_gaps(a.all)
    if a.cmd == "grow":
        for r in grow(dry=a.dry_run):
            print(f"{r['kind']:<4} {r['name']:<22} {r['status']:<11} {r['reason']}")
        return 0
    sys.path.insert(0, str(core.VAULT / "assistant"))
    import onboarding
    print(json.dumps(onboarding.seed_wiki(), indent=1, default=str))
    return 0


if __name__ == "__main__":
    sys.exit(main())

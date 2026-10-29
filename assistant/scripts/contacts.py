"""contacts.py - the owners' phone contacts, so the OS knows who everyone is (2026-10-06).

An iPhone exports its whole address book as ONE .vcf (Contacts > Lists > long-press
"All Contacts" > Export). That file reaches this PC through Mission Control's /contacts
page (upload from the phone's own Safari) or the CLI below. Each import REPLACES that
owner's book (a contact deleted on the phone disappears here too) and rewrites the wiki
directory note Wiki/People/Contacts.md so Sage and the operator find people by search.

Kept per person: name, nickname, company, title, phones, emails, birthday, related names
(the "mother", "spouse" labels from the phone). Photos, street addresses and notes are
dropped on purpose. The book lives in assistant/data/contacts.json (git-ignored).

  python assistant/scripts/contacts.py import <file.vcf> [--owner <owner key from people.json>]
  python assistant/scripts/contacts.py who <phone|email|name>     (read-only lookup)
  python assistant/scripts/contacts.py stats
  python assistant/scripts/contacts.py wiki                         (rewrite the wiki note)
"""
from __future__ import annotations

import argparse
import json
import os
import quopri
import re
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from alib import DATA, ROOT, load_json, save_json  # noqa: E402

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

BOOK = DATA / "contacts.json"
# tests set WV_CONTACTS_WIKI: a test run never rewrites the real wiki note
WIKI_NOTE = Path(os.environ.get("WV_CONTACTS_WIKI") or (ROOT.parent / "Wiki" / "People" / "Contacts.md"))
def _owner_keys() -> tuple:
    """The owners from assistant/agent/people.json (role owner), in order; one generic book when there are none."""
    try:
        d = json.loads((ROOT / "agent" / "people.json").read_text(encoding="utf-8-sig"))
        keys = tuple(k for k, u in (d.get("users") or {}).items() if str(u.get("role") or "") == "owner")
        if keys:
            return keys
    except (OSError, ValueError, AttributeError):
        pass
    return ("owner",)


OWNERS = _owner_keys()
MAX_VCF = 40 * 1024 * 1024


# ------------------------------------------------------------------ vCard parsing
def _unfold(text: str) -> list[str]:
    """RFC 6350 line unfolding, plus quoted-printable soft breaks (vCard 2.1 exports)."""
    lines: list[str] = []
    for raw in text.replace("\r\n", "\n").replace("\r", "\n").split("\n"):
        if raw[:1] in (" ", "\t") and lines:
            lines[-1] += raw[1:]
        elif lines and lines[-1].endswith("=") and "QUOTED-PRINTABLE" in lines[-1].split(":", 1)[0].upper():
            lines[-1] = lines[-1][:-1] + raw
        else:
            lines.append(raw)
    return lines


def _unescape(v: str) -> str:
    return re.sub(r"\\([nN,;:\\])", lambda m: "\n" if m.group(1) in "nN" else m.group(1), v).strip()


def _split(v: str, sep=";") -> list[str]:
    return [_unescape(p) for p in re.split(r"(?<!\\)" + re.escape(sep), v)]


def _label(params: str) -> str:
    types = [t.lower() for t in re.findall(r"TYPE=([^;:]+)", params, re.I) for t in t.split(",")]
    types += [t.lower() for t in params.split(";")[1:] if "=" not in t]
    for t in ("cell", "mobile", "iphone", "home", "work", "main", "other"):
        if t in types:
            return "mobile" if t in ("cell", "iphone") else t
    return ""


def parse_vcf(text: str) -> list[dict]:
    people, cur, labels, group_vals = [], None, {}, {}
    for line in _unfold(text):
        if not line.strip() or ":" not in line:
            continue
        head, value = line.split(":", 1)
        group, _, rest = head.partition(".") if "." in head.split(";", 1)[0] else ("", "", head)
        prop = rest.split(";", 1)[0].upper()
        params = rest
        if "QUOTED-PRINTABLE" in params.upper():
            value = quopri.decodestring(value.encode("latin-1", "replace")).decode("utf-8", "replace")
        if prop == "BEGIN" and value.strip().upper() == "VCARD":
            cur, labels, group_vals = {"name": "", "nickname": "", "org": "", "title": "", "phones": [],
                                       "emails": [], "birthday": "", "related": []}, {}, {}
            continue
        if cur is None:
            continue
        if prop == "END" and value.strip().upper() == "VCARD":
            for fld in ("phones", "emails"):              # Apple writes item1.TEL then item1.X-ABLabel
                for e in cur[fld]:
                    g = e.pop("_g", "")
                    if g and g in labels:
                        e["label"] = labels[g]
            for g, (kind, v) in group_vals.items():       # X-ABRELATEDNAMES with a separate X-ABLabel
                if kind == "rel":
                    cur["related"].append({"name": v, "label": labels.get(g, "")})
            if not cur["name"]:
                cur["name"] = cur["org"] or (cur["emails"][0]["value"] if cur["emails"] else "") or \
                    (cur["phones"][0]["value"] if cur["phones"] else "")
            if cur["name"]:
                people.append(cur)
            cur = None
            continue
        if prop == "X-ABLABEL":
            labels[group] = re.sub(r"^_\$!<(.*)>!\$_$", r"\1", _unescape(value)).lower()
            continue
        if prop == "FN":
            cur["name"] = _unescape(value)
        elif prop == "N" and not cur["name"]:
            parts = _split(value) + [""] * 5
            cur["name"] = " ".join(p for p in (parts[3], parts[1], parts[0], parts[4]) if p).strip()
        elif prop == "NICKNAME":
            cur["nickname"] = _unescape(value)
        elif prop == "ORG":
            cur["org"] = " - ".join(p for p in _split(value) if p)
        elif prop == "TITLE":
            cur["title"] = _unescape(value)
        elif prop == "TEL":
            v = _unescape(value).replace("tel:", "")
            if v:
                cur["phones"].append({"value": v, "label": _label(params), "_g": group})
        elif prop == "EMAIL":
            v = _unescape(value).replace("mailto:", "")
            if "@" in v:
                cur["emails"].append({"value": v, "label": _label(params), "_g": group})
        elif prop == "BDAY":
            b = _unescape(value)[:10]
            # iOS marks "no year" as 1604-MM-DD (X-APPLE-OMIT-YEAR) or --MM-DD
            cur["birthday"] = b[2:] if b.startswith("--") else b[5:] if b.startswith("1604-") or "OMIT-YEAR" in params.upper() else b
        elif prop == "X-ABRELATEDNAMES":
            group_vals[group or f"r{len(group_vals)}"] = ("rel", _unescape(value))
    return people


# ------------------------------------------------------------------ keys
def phone_key(v: str) -> str:
    d = re.sub(r"\D", "", str(v or ""))
    return d[-10:] if len(d) >= 10 else d


def email_key(v: str) -> str:
    return str(v or "").strip().lower()


# ------------------------------------------------------------------ store
def load() -> dict:
    return load_json(BOOK, {"books": {}})


def import_file(path, owner: str = "", source: str = "chat") -> dict:
    """A .vcf on disk (a chat attachment, an upload) -> import_text. The caller deletes the file."""
    try:
        text = Path(path).read_bytes().decode("utf-8", errors="replace")
    except OSError as e:
        return {"ok": False, "reason": f"could not read the file: {e}"}
    return import_text(text, owner=owner or OWNERS[0], source=source)


def import_text(text: str, owner: str = "", source: str = "iphone") -> dict:
    owner = owner if owner in OWNERS else OWNERS[0]
    if len(text) > MAX_VCF:
        return {"ok": False, "reason": "file too large"}
    if "BEGIN:VCARD" not in text.upper():
        return {"ok": False, "reason": "not a contacts (.vcf) file"}
    people = parse_vcf(text)
    if not people:
        return {"ok": False, "reason": "no contacts found in the file"}
    for p in people:
        p["owner"] = owner
    db = load()
    db.setdefault("books", {})[owner] = {"source": source, "imported": time.strftime("%Y-%m-%dT%H:%M:%S"),
                                         "count": len(people), "people": people}
    save_json(BOOK, db)
    try:
        write_wiki(db)
        wiki = True
    except OSError:
        wiki = False
    return {"ok": True, "owner": owner, "count": len(people),
            "with_phone": sum(1 for p in people if p["phones"]),
            "with_email": sum(1 for p in people if p["emails"]), "wiki": wiki}


def everyone(db: dict | None = None) -> list[dict]:
    db = db or load()
    return [p for b in db.get("books", {}).values() for p in b.get("people", [])]


def crm_people() -> list[dict]:
    """The CRM's clients in the phone book's person shape (a plugin's `crm_people()`, alib.plugins), so a
    client texting is a known contact too; none when no plugin has a CRM."""
    from alib import plugin_call
    return plugin_call("crm_people") or []


def lookup(handle: str, db: dict | None = None) -> dict | None:
    """The contact a phone number or email belongs to; None when nobody has it. The iPhone books
    first, then the CRM's clients."""
    h = str(handle or "").strip()
    if not h:
        return None
    people = everyone(db) + crm_people()
    if "@" in h:
        k = email_key(h)
        return next((p for p in people if any(email_key(e["value"]) == k for e in p.get("emails", []))), None)
    k = phone_key(h)
    if len(k) < 7:
        return None
    return next((p for p in people if any(phone_key(e["value"]) == k for e in p.get("phones", []))), None)


def name_for(handle: str) -> str:
    try:
        p = lookup(handle)
    except Exception:
        return ""
    return (p or {}).get("name", "")


def search(q: str, db: dict | None = None) -> list[dict]:
    hit = lookup(q, db)
    if hit:
        return [hit]
    ql = q.strip().lower()
    return [p for p in everyone(db) if ql and (ql in p["name"].lower() or ql in p.get("nickname", "").lower()
                                               or ql in p.get("org", "").lower())]


# ------------------------------------------------------------------ wiki
def _cell(s: str) -> str:
    return str(s or "").replace("|", "/").replace("\n", " ").strip()


def _fmt_phone(v: str) -> str:
    d = re.sub(r"\D", "", v)
    if len(d) == 11 and d[0] == "1":
        d = d[1:]
    return f"({d[:3]}) {d[3:6]}-{d[6:]}" if len(d) == 10 else v.strip()


def write_wiki(db: dict | None = None) -> Path:
    db = db or load()
    merged: dict[str, dict] = {}
    for p in everyone(db):                     # one row per person even if both phones have them
        key = p["name"].strip().lower()
        if key in merged:
            m = merged[key]
            m["owners"].add(p["owner"])
            for f in ("phones", "emails"):
                seen = {phone_key(e["value"]) if f == "phones" else email_key(e["value"]) for e in m[f]}
                m[f] += [e for e in p[f] if (phone_key(e["value"]) if f == "phones" else email_key(e["value"])) not in seen]
            for f in ("org", "title", "birthday", "nickname"):
                m[f] = m.get(f) or p.get(f, "")
            m["related"] += p.get("related", [])
        else:
            merged[key] = {**p, "owners": {p["owner"]}, "phones": list(p["phones"]), "emails": list(p["emails"]),
                           "related": list(p.get("related", []))}
    rows = sorted(merged.values(), key=lambda p: p["name"].lower())
    books = db.get("books", {})
    today = time.strftime("%Y-%m-%d")
    src = ", ".join(f"{o.title()}'s iPhone ({b['count']} contacts, {b['imported'][:10]})" for o, b in books.items())
    out = ["---", "type: reference", "area: people", "tags: [personal, family, friends]",
           f"created: {today}", f"updated: {today}", "sources:", '  - "[[Family and Friends]]"', "status: draft", "---", "",
           "# Contacts", "",
           "Everyone in the owners' phone contacts, so the assistant knows who a number or email belongs to. "
           "Relationships and context live in [[Family and Friends]] and each person's own note in this folder.", "",
           "> [!info] Generated - do not hand-edit",
           f"> Rewritten by `assistant/scripts/contacts.py` on every import. From: {src or 'nothing imported yet'}. "
           "Street addresses, notes and photos are left out on purpose. Look up one number with "
           "`python assistant/scripts/contacts.py who <number>`.", "",
           f"**{len(rows)} people.**", "",
           "| Name | Company / title | Phone | Email | Birthday | Related | Phone book |",
           "|------|-----------------|-------|-------|----------|---------|------------|"]
    for p in rows:
        name = _cell(p["name"]) + (f" ({_cell(p['nickname'])})" if p.get("nickname") else "")
        org = _cell(" - ".join(x for x in (p.get("org"), p.get("title")) if x))
        ph = "<br>".join(_cell(_fmt_phone(e["value"]) + (f" {e['label']}" if e.get("label") else "")) for e in p["phones"])
        em = "<br>".join(_cell(e["value"]) for e in p["emails"])
        rel = "<br>".join(_cell(f"{r['label']}: {r['name']}" if r.get("label") else r["name"]) for r in p["related"])
        own = ", ".join(sorted(o.title() for o in p["owners"]))
        out.append(f"| {name} | {org} | {ph} | {em} | {_cell(p.get('birthday'))} | {rel} | {own} |")
    WIKI_NOTE.parent.mkdir(parents=True, exist_ok=True)
    WIKI_NOTE.write_text("\n".join(out) + "\n", encoding="utf-8")
    return WIKI_NOTE


# ------------------------------------------------------------------ CLI
def main():
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("import"); p.add_argument("file"); p.add_argument("--owner", default=OWNERS[0], choices=OWNERS)
    p = sub.add_parser("who"); p.add_argument("query")
    sub.add_parser("stats")
    sub.add_parser("wiki")
    a = ap.parse_args()
    if a.cmd == "import":
        raw = Path(a.file).read_bytes()
        r = import_text(raw.decode("utf-8-sig", errors="replace"), a.owner, source=Path(a.file).name)
        print(json.dumps(r, indent=1))
        return 0 if r["ok"] else 1
    if a.cmd == "who":
        hits = search(a.query)
        print(json.dumps(hits[:20], ensure_ascii=False, indent=1) if hits else "no contact matches")
        return 0
    if a.cmd == "stats":
        db = load()
        print(json.dumps({o: {"count": b["count"], "imported": b["imported"], "source": b["source"]}
                          for o, b in db.get("books", {}).items()}, indent=1))
        return 0
    if a.cmd == "wiki":
        print(write_wiki())
    return 0


if __name__ == "__main__":
    sys.exit(main())

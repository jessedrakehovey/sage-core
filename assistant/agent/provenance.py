"""provenance.py - every fact an agent reads carries where it came from and when; stale, sentinel and conflicting facts read as UNKNOWN.

Seed B of the agent-intelligence plan (2026-10-31, "truth with provenance", RESHAPE verdict: no world package, no
confidence floats). The agents compose their briefs from many records: a work board rebuilt every few hours, a
nightly ledger pull, a key log, access notes, synced notes, memory, card answers, chat messages. Each of those is
right at the moment it was written and wrong some time after, and a cached zero or null is often "not checked",
never "none". Three failure shapes recur, all logged as mistakes in the organizations that run this engine:

  stale      a record older than its kind's limit was quoted as current ("the board says Scheduled" from a board
             two days old)
  sentinel   a known false negative of the system of record was read as a fact (a filtered pull returning zero rows
             for a record that exists; a null timestamp on a job that is scheduled; a windowed pull missing an old
             record; a null cached count read as zero)
  conflict   two sources disagreed and whichever was read first won, silently

This module is the deterministic half of the fix. It never decides what is TRUE; it decides what may be ASSERTED.

  fact(name, value, as_of, source, kind, ...)      one annotated fact: {fact, value, as_of, source, kind, age_h,
                                                   stale, unknown, reason, sentinel, conflict, candidates}
  merge(facts)                                     one fact from several sources; disagreement is kept side by side
                                                   (conflict=True, candidates=[...]) and reads as unknown
  known(f)                                         the value, or None when the fact may not be asserted
  describe(f) / verdict_lines(facts)               plain-words lines for a prompt head or a card ("UNKNOWN: ...")

Rules are DATA, never code: provenance_rules.json (max age in hours per source kind; null = durable, never stale)
and sentinels.json (named false negatives: which kind / source / field / value / context reads as unknown, and why).
The files beside this module are the engine's generic templates; an organization binds its own copies with its
system of record's names (load_rules(path) / load_sentinels(path)) and passes them in, or sets the module defaults
with configure(rules=..., sentinels=...).

as_of accepts ISO timestamps, dates, "MM/DD/YYYY [at] HH:MM AM" forms, epoch seconds, and the RELATIVE forms "now",
"now-50h", "now-3d", "now-20m" so a replay fixture stays fresh or stale on purpose, whatever day it runs.

Pure functions, no I/O beyond reading the two JSON files and file mtimes (file_as_of). Nothing here sends, writes
or decides an action: the gate it feeds (the organization's fact gate) does.

  python provenance.py describe <facts.json>       print the verdict for each fact in a JSON list
"""
from __future__ import annotations

import json
import re
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path

HERE = Path(__file__).resolve().parent
RULES_FILE = HERE / "provenance_rules.json"
SENTINELS_FILE = HERE / "sentinels.json"

UNKNOWN = "UNKNOWN"
_DEFAULTS: dict = {"rules": None, "sentinels": None}
_REL = re.compile(r"^now(?:\s*([+-])\s*(\d+(?:\.\d+)?)\s*([mhd]))?$", re.I)
_FORMATS = ("%Y-%m-%dT%H:%M:%S", "%Y-%m-%dT%H:%M", "%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d",
            "%m/%d/%Y at %I:%M %p", "%m/%d/%Y %I:%M %p", "%m/%d/%Y %H:%M", "%m/%d/%Y", "%Y-%m-%dT%H:%M:%S.%f")


# ------------------------------------------------------------------ data
def _loadj(p, default):
    try:
        return json.loads(Path(p).read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        return default


def load_rules(path=None) -> dict:
    """{"max_age_hours": {kind: hours | null}, "default_max_age_hours": hours | null}."""
    d = _loadj(path or RULES_FILE, {})
    return {"max_age_hours": dict(d.get("max_age_hours") or {}), "default_max_age_hours": d.get("default_max_age_hours")}


def load_sentinels(path=None) -> list:
    d = _loadj(path or SENTINELS_FILE, {})
    rows = d.get("sentinels") if isinstance(d, dict) else d
    return [r for r in (rows or []) if isinstance(r, dict) and r.get("name")]


def configure(rules=None, sentinels=None) -> None:
    """Set the module defaults (an organization's own rules / sentinels), used when a call passes neither."""
    if rules is not None:
        _DEFAULTS["rules"] = load_rules(rules) if isinstance(rules, (str, Path)) else dict(rules)
    if sentinels is not None:
        _DEFAULTS["sentinels"] = load_sentinels(sentinels) if isinstance(sentinels, (str, Path)) else list(sentinels)


def _rules(rules) -> dict:
    if rules is not None:
        return rules
    if _DEFAULTS["rules"] is None:
        _DEFAULTS["rules"] = load_rules()
    return _DEFAULTS["rules"]


def _sentinels(sentinels) -> list:
    if sentinels is not None:
        return sentinels
    if _DEFAULTS["sentinels"] is None:
        _DEFAULTS["sentinels"] = load_sentinels()
    return _DEFAULTS["sentinels"]


def max_age_hours(kind: str, rules=None):
    """The kind's limit in hours; None = durable (never stale). An unlisted kind takes the default."""
    r = _rules(rules)
    table = r.get("max_age_hours") or {}
    if kind in table:
        return table[kind]
    return r.get("default_max_age_hours")


# ------------------------------------------------------------------ time
def parse_when(s, now: datetime | None = None) -> datetime | None:
    """A timestamp in any of the accepted forms -> datetime (naive, local), or None."""
    if s is None or s == "":
        return None
    if isinstance(s, datetime):
        return s
    if isinstance(s, (int, float)):
        try:
            return datetime.fromtimestamp(float(s))
        except (OverflowError, OSError, ValueError):
            return None
    t = str(s).strip()
    m = _REL.match(t)
    if m:
        base = now or datetime.now()
        if not m.group(1):
            return base
        n = float(m.group(2))
        unit = m.group(3).lower()
        delta = timedelta(minutes=n) if unit == "m" else timedelta(hours=n) if unit == "h" else timedelta(days=n)
        return base + delta if m.group(1) == "+" else base - delta
    t = t.replace("Z", "")
    if re.match(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}[+-]\d{2}:\d{2}$", t):
        t = t[:19]
    for fmt in _FORMATS:
        try:
            return datetime.strptime(t[:len(t)], fmt)
        except ValueError:
            continue
    for fmt in ("%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M:%S"):
        try:
            return datetime.strptime(t[:19], fmt)
        except ValueError:
            continue
    return None


def age_hours(as_of, now: datetime | None = None) -> float | None:
    dt = parse_when(as_of, now)
    if dt is None:
        return None
    return round(((now or datetime.now()) - dt).total_seconds() / 3600.0, 2)


def file_as_of(path) -> str:
    """The file's modification time as an ISO stamp ('' when the file is missing): the as_of of a record whose
    content carries no stamp of its own."""
    try:
        return time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(Path(path).stat().st_mtime))
    except OSError:
        return ""


def stamp(dt: datetime | None = None) -> str:
    return (dt or datetime.now()).strftime("%Y-%m-%dT%H:%M:%S")


def short(as_of, now: datetime | None = None) -> str:
    """A short human form of a stamp for a prompt line: today -> HH:MM, this year -> MM/DD HH:MM, else the date."""
    dt = parse_when(as_of, now)
    if dt is None:
        return str(as_of or "no time")
    n = now or datetime.now()
    if dt.date() == n.date():
        return dt.strftime("%H:%M")
    if dt.year == n.year:
        return dt.strftime("%m/%d %H:%M") if (dt.hour or dt.minute) else dt.strftime("%m/%d")
    return dt.strftime("%Y-%m-%d")


def _age_words(h: float) -> str:
    if h < 1:
        return f"{int(round(h * 60))} min"
    if h < 72:
        return f"{h:.0f} h"
    return f"{h / 24:.0f} d"


# ------------------------------------------------------------------ sentinels
def _in(value, options) -> bool:
    for o in options:
        if o is None and value is None:
            return True
        if isinstance(o, bool) or isinstance(value, bool):
            if o is value:
                return True
            continue
        if o == value:
            return True
        if isinstance(o, str) and isinstance(value, str) and o.lower() == value.lower():
            return True
    return False


def _listed(x) -> list:
    return list(x) if isinstance(x, (list, tuple)) else [x]


def sentinel_hits(value, kind: str = "", source: str = "", field: str = "", context: dict | None = None,
                  sentinels=None) -> list:
    """The sentinel rules this reading trips. A rule trips when EVERY condition it states holds:
      when.kind      the fact's kind is one of these
      when.source_re regex over the source string
      when.field     the field name is one of these
      when.value_in  the value is one of these (null, 0, "", false ...)
      when.context   each key here equals the reading's context value (a list = one of)
    A rule with no conditions never trips."""
    ctx = dict(context or {})
    hits = []
    for rule in _sentinels(sentinels):
        when = rule.get("when") or {}
        if not when:
            continue
        if "kind" in when and kind not in _listed(when["kind"]):
            continue
        if "source_re" in when and not re.search(str(when["source_re"]), str(source or ""), re.I):
            continue
        if "field" in when and field not in _listed(when["field"]):
            continue
        if "value_in" in when and not _in(value, _listed(when["value_in"])):
            continue
        ok = True
        for k, want in (when.get("context") or {}).items():
            have = ctx.get(k)
            if isinstance(want, list):
                if not _in(have, want):
                    ok = False
                    break
            elif isinstance(want, bool):
                if bool(have) is not want:
                    ok = False
                    break
            elif have != want and str(have).lower() != str(want).lower():
                ok = False
                break
        if ok:
            hits.append(rule)
    return hits


# ------------------------------------------------------------------ facts
def fact(name: str, value, as_of, source: str, kind: str = "", *, field: str = "", context: dict | None = None,
         now: datetime | None = None, rules=None, sentinels=None) -> dict:
    """One fact with its provenance and its verdict.

    stale    = the kind has a max age and the record is older (or carries no as_of at all)
    unknown  = stale, or a sentinel tripped, or (merge) a conflict: the value may be shown with its stamp but never
               asserted as current truth
    The value is kept as the source gave it (so a line can say "last known X as of T"); known(f) returns None."""
    now = now or datetime.now()
    f = {"fact": str(name), "value": value, "as_of": stamp(parse_when(as_of, now)) if parse_when(as_of, now) else "",
         "source": str(source or ""), "kind": str(kind or ""), "age_h": age_hours(as_of, now), "stale": False,
         "unknown": False, "reason": "", "sentinel": "", "conflict": False, "candidates": []}
    limit = max_age_hours(f["kind"], rules)
    if limit is not None:
        if f["age_h"] is None:
            f.update(stale=True, unknown=True, reason=f"no as_of on a {f['kind'] or 'timed'} record (limit {limit} h)")
        elif f["age_h"] > float(limit):
            f.update(stale=True, unknown=True,
                     reason=f"{f['kind'] or 'record'} of {short(f['as_of'], now)} is {_age_words(f['age_h'])} old, over {_age_words(float(limit))}")
    hits = sentinel_hits(value, f["kind"], f["source"], field or name, context, sentinels)
    if hits:
        f["sentinel"] = hits[0]["name"]
        f["unknown"] = True
        why = str(hits[0].get("why") or "").strip()
        f["reason"] = (f["reason"] + "; " if f["reason"] else "") + f"sentinel {hits[0]['name']}" + (f": {why}" if why else "")
    return f


def known(f: dict):
    """The value an agent may assert, or None (unknown, stale, sentinel, conflict)."""
    if not isinstance(f, dict) or f.get("unknown"):
        return None
    return f.get("value")


def _same(a, b) -> bool:
    if isinstance(a, str) and isinstance(b, str):
        return a.strip().lower() == b.strip().lower()
    return a == b


def merge(facts: list, now: datetime | None = None) -> dict:
    """One fact from several readings of the same thing. Unknown readings never vote. Usable readings that agree ->
    the freshest, with every source listed. Usable readings that disagree -> conflict: both kept in `candidates`,
    value None, unknown True, reason names each side with its stamp. Nothing usable -> unknown with the candidates."""
    now = now or datetime.now()
    facts = [f for f in facts if isinstance(f, dict)]
    if not facts:
        return {"fact": "", "value": None, "as_of": "", "source": "", "kind": "", "age_h": None, "stale": False,
                "unknown": True, "reason": "no reading", "sentinel": "", "conflict": False, "candidates": []}
    name = facts[0].get("fact", "")
    usable = [f for f in facts if not f.get("unknown")]
    cands = [{"value": f.get("value"), "as_of": f.get("as_of"), "source": f.get("source"), "kind": f.get("kind"),
              "unknown": bool(f.get("unknown")), "reason": f.get("reason", "")} for f in facts]
    if not usable:
        out = dict(facts[0])
        out.update(value=None, unknown=True, conflict=False, candidates=cands,
                   reason="; ".join(c["reason"] for c in cands if c["reason"]) or "every reading is unknown")
        return out
    usable.sort(key=lambda f: (f.get("age_h") if f.get("age_h") is not None else 1e9))
    head = usable[0]
    distinct = []
    for f in usable:
        if not any(_same(f.get("value"), d.get("value")) for d in distinct):
            distinct.append(f)
    if len(distinct) > 1:
        sides = " but ".join(f"{d.get('source') or d.get('kind') or 'a source'} says {d.get('value')!r} ({short(d.get('as_of'), now)})"
                             for d in distinct)
        return {"fact": name, "value": None, "as_of": head.get("as_of", ""), "source": " | ".join(str(d.get("source")) for d in distinct),
                "kind": head.get("kind", ""), "age_h": head.get("age_h"), "stale": False, "unknown": True,
                "reason": f"conflict: {sides}", "sentinel": "", "conflict": True, "candidates": cands}
    out = dict(head)
    out["sources"] = [str(f.get("source")) for f in usable]
    out["candidates"] = cands if len(facts) > 1 else []
    return out


def gate(facts: list) -> dict:
    """What a brief may assert: {known: {name: value}, unknown: [name...], conflicts: [name...], lines: [...]}"""
    out = {"known": {}, "unknown": [], "conflicts": [], "lines": []}
    for f in facts:
        if not isinstance(f, dict):
            continue
        if f.get("conflict"):
            out["conflicts"].append(f.get("fact"))
        if f.get("unknown"):
            out["unknown"].append(f.get("fact"))
            out["lines"].append(describe(f))
        else:
            out["known"][f.get("fact")] = f.get("value")
    return out


# ------------------------------------------------------------------ words
def describe(f: dict, now: datetime | None = None) -> str:
    """One plain-words line. Known: 'open jobs 3 (board 20:46)'. Unknown: 'open jobs UNKNOWN: board of 10/29 09:00
    is 60 h old, over 30 h; last known 3'. Conflict: 'status UNKNOWN: conflict: board says ... but ... says ...'."""
    now = now or datetime.now()
    name = str(f.get("fact") or "fact")
    src = str(f.get("source") or f.get("kind") or "")
    if not f.get("unknown"):
        tail = f" ({src} {short(f['as_of'], now)})" if f.get("as_of") else (f" ({src})" if src else "")
        return f"{name} {f.get('value')!r}{tail}"
    line = f"{name} {UNKNOWN}: {f.get('reason') or 'unknown'}"
    if f.get("stale") and f.get("value") is not None and not f.get("conflict"):
        line += f"; last known {f.get('value')!r}"
    return line


def verdict_lines(facts: list, now: datetime | None = None, only_unknown: bool = True) -> list:
    """The lines a prompt head or a card shows: by default only the facts that may NOT be asserted."""
    out = []
    for f in facts:
        if not isinstance(f, dict):
            continue
        if only_unknown and not f.get("unknown"):
            continue
        out.append(describe(f, now))
    return out


def main(argv=None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if len(argv) >= 2 and argv[0] == "describe":
        rows = _loadj(argv[1], [])
        facts = [fact(r.get("fact", ""), r.get("value"), r.get("as_of"), r.get("source", ""), r.get("kind", ""),
                      field=r.get("field", ""), context=r.get("context")) for r in rows if isinstance(r, dict)]
        for ln in verdict_lines(facts, only_unknown=False):
            print(ln)
        return 0
    print(__doc__.strip().splitlines()[0])
    print("  python provenance.py describe <facts.json>")
    return 2


if __name__ == "__main__":
    sys.exit(main())

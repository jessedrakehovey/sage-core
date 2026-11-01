"""truth.py - this agent's binding of the provenance engine (Seed B, 2026-11-01): the brief's facts carry as_of + source.

The owner, 2026-11-01: "give sage normal sage this too" (the truth-with-provenance seed sage-core carries). The brain reads
many cached stores (calendar, mail, the CRM snapshot, the bank feed, the connections board), each right when it was
written and wrong some time after; an account that is not connected reads like an empty one. provenance.py decides
what may be ASSERTED; this module feeds it this organization's own rules and stores and gives the brain one block:

  FACTS AS OF   every store the brief states facts from, with its source and time; a fact older than its kind's limit,
                one that trips a named sentinel, or two sources that disagree reads as UNKNOWN / CONFLICT in plain
                words (provenance.describe), never as its stale value.

The rules are pack DATA, never code: assistant/data/provenance_rules.json (max age per kind), sentinels.json (the
false negatives logged as mistakes, each with why) and brief_facts.json (which store, which stamp, which value). An
organization without them falls back to the engine's templates beside provenance.py and an empty brief.

Stamps: a UTC or offset stamp ("...Z", "+00:00") is turned into local time before provenance reads it (the engine
reads a stamp as local, so a Z stamp would look hours in the future).

  python assistant/agent/truth.py shadow     every brief fact with its verdict (changes nothing)
  python assistant/agent/truth.py render     the block the brain gets
"""
from __future__ import annotations

import json
import os
import re
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import core  # noqa: E402
import provenance  # noqa: E402

ADATA = Path(os.environ["ASSISTANT_DATA"]) if os.environ.get("ASSISTANT_DATA") else core.AGENT.parent / "data"
RULES = ADATA / "provenance_rules.json"
SENTINELS = ADATA / "sentinels.json"
SOURCES = ADATA / "brief_facts.json"
UNKNOWN = provenance.UNKNOWN
HEAD = ("FACTS AS OF (where each fact below was read and when. A line reading UNKNOWN is a fact you may NOT state as "
        "current: its record is older than its limit, it is a known false negative of that system, or two sources "
        "disagree (CONFLICT: say both with their times, then check the record; never pick one). Say you have not "
        "checked and check with a tool; never fill the gap with the old value.)")
_OFFSET = re.compile(r"T\d{2}:\d{2}(?::\d{2}(?:\.\d+)?)?(?:Z|[+-]\d{2}:?\d{2})$")


def _load(p: Path, default):
    try:
        return json.loads(Path(p).read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        return default


def bind() -> None:
    """The engine reads this organization's rules and sentinels (the engine's templates when the pack has none)."""
    provenance.configure(rules=RULES if RULES.exists() else provenance.RULES_FILE,
                         sentinels=SENTINELS if SENTINELS.exists() else provenance.SENTINELS_FILE)


def local_stamp(s):
    """'2026-11-01T14:45:25Z' -> '2026-11-01T06:45:25' (local, naive); anything else unchanged."""
    if not isinstance(s, str) or not _OFFSET.search(s.strip()):
        return s
    try:
        dt = datetime.fromisoformat(s.strip().replace("Z", "+00:00"))
        return dt.astimezone().replace(tzinfo=None).strftime("%Y-%m-%dT%H:%M:%S")
    except ValueError:
        return s


def fact(name, value, as_of, source, kind="", *, field="", context=None, now=None) -> dict:
    """provenance.fact with this organization's rules and local stamps."""
    bind()
    return provenance.fact(name, value, local_stamp(as_of), source, kind, field=field, context=context, now=now)


def from_rows(rows: list, now=None) -> list:
    """Literal readings [{fact, value, as_of, source, kind, field?, context?}] -> facts, merged by name (two sources
    of one fact that disagree -> one CONFLICT fact). A replay fixture and the stores both come through here."""
    readings: dict = {}
    for r in rows or []:
        if not isinstance(r, dict) or not r.get("fact"):
            continue
        f = fact(r["fact"], r.get("value"), r.get("as_of"), r.get("source", ""), r.get("kind", ""),
                 field=r.get("field", ""), context=r.get("context"), now=now)
        readings.setdefault(f["fact"], []).append(f)
    return [fs[0] if len(fs) == 1 else provenance.merge(fs, now) for fs in readings.values()]


def _path(p: str) -> Path:
    if p == "@crm":
        sys.path.insert(0, str(core.AGENT.parent / "scripts"))
        import crm
        return crm.snapshot_path()
    q = Path(p)
    return q if q.is_absolute() else core.VAULT / q


def store_rows(spec: dict | None = None) -> list:
    """brief_facts.json -> literal readings, one per store (or per entry of an `each` dict). A store that cannot be
    read becomes a reading with no stamp (stale -> UNKNOWN), never a silent gap."""
    spec = spec if spec is not None else _load(SOURCES, {})
    rows = []
    for s in (spec.get("sources") or []) if isinstance(spec, dict) else []:
        try:
            path = _path(str(s.get("path") or ""))
        except Exception:  # noqa: BLE001
            path = None
        data = _load(path, None) if path else None
        stamp_key = str(s.get("as_of") or "@mtime")
        as_of = (provenance.file_as_of(path) if path else "") if stamp_key == "@mtime" else (data or {}).get(stamp_key) if isinstance(data, dict) else ""
        base = {"source": s.get("source") or str(s.get("path")), "kind": s.get("kind", ""), "as_of": as_of or ""}
        if s.get("each"):
            items = (data or {}).get(s["each"]) if isinstance(data, dict) else None
            for k, v in (items or {}).items():
                rows.append({**base, "fact": f"{s['fact']} {k}", "value": v, "field": "status"})
            if not items:
                rows.append({**base, "fact": s["fact"], "value": None, "as_of": ""})
            continue
        v = (data or {}).get(s.get("value")) if isinstance(data, dict) and s.get("value") else None
        if s.get("count") and v is not None:
            v = len(v) if hasattr(v, "__len__") else v
        rows.append({**base, "fact": s["fact"], "value": v if data is not None else None,
                     "as_of": base["as_of"] if data is not None else ""})
    return rows


def brief_facts(now=None) -> list:
    return from_rows(store_rows(), now)


def render(facts: list | None = None, now=None) -> str:
    """The FACTS AS OF block: every fact with its source and time, UNKNOWN / CONFLICT in plain words."""
    try:
        facts = brief_facts(now) if facts is None else facts
    except Exception as e:  # noqa: BLE001
        return f"FACTS AS OF: not readable right now ({type(e).__name__}); treat every cached fact as unchecked."
    lines = provenance.verdict_lines(facts, now, only_unknown=False)
    return (HEAD + "\n" + "\n".join("- " + ln for ln in lines)) if lines else ""


def main(argv=None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:  # noqa: BLE001
        pass
    if argv and argv[0] == "shadow":
        for f in brief_facts():
            verdict = "CONFLICT" if f.get("conflict") else (UNKNOWN if f.get("unknown") else "known")
            print(f"{verdict:8} {provenance.describe(f)}   [as_of {f.get('as_of') or '-'}, source {f.get('source') or '-'}]")
        return 0
    if argv and argv[0] == "render":
        print(render())
        return 0
    print(__doc__.strip().splitlines()[0])
    return 2


if __name__ == "__main__":
    sys.exit(main())

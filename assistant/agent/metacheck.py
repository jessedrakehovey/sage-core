"""metacheck.py - a card whose ACTION is high-stakes gets a deferred fresh-context re-check of its key fact before the
owner reads it. Gated by action TYPE from a data table, never by a number.

Seed C of the agent-intelligence plan (RESHAPE verdict, 2026-10-31). Dropped on purpose: a model-self-reported
confidence (the model grading its own homework), a decimal on the card (it invites a fast tap), and an inline
fresh-context pass on the hot path (minutes per action). Kept: spend more scrutiny where the stakes are high, and log
it so calibration can be measured.

What this module decides, as pure functions over dicts:

  classify(card, table)            -> (action_type, stakes)  from the card's exec kind, preview marker and audience,
                                      against the held-type table (metacheck.json; <= 5 held types; an organization
                                      binds its own copy with its own exec kinds / audiences)
  hold(card_id, action_type, ...)  -> the hold record (the plan's shape, below)
  shadow(card_id, action_type, ...)-> the same record with held=false: the UNARMED log row, so five days of
                                      (action_type, stakes, text, outcome) exist before any routing changes
  resolve(hold, verifier, now, office_open, table) -> what happens to the held card now:
        confirmed    released unchanged (one quiet detail line)
        discrepancy  released, flagged in plain words "verifier found a difference, needs your read"; the card's
                     question text and options are NEVER rewritten by this module (apply() only appends a line)
        timeout      past timeout_s with no verdict: released with "verifier timed out, verify by hand", but a
                     timed-out hold never auto-releases outside office hours (owner decision 4): it waits for the
                     office to open and reads as held meanwhile
        pending      still inside the window, nothing to do
  apply(card, resolution)          -> the card with the verifier line appended to its detail text; questions, options
                                      and exec payload untouched (asserted), so the tap stays exactly as it was
  calibration(rows, days, now)     -> plain-words lines over the ledger: how many high-stakes cards, how many held,
                                      how many the verifier confirmed / disputed / timed out; words, no ratios

The hold record (plan section 2C):
  {"card": "<id>", "action_type": "send_customer_email", "stakes": "high", "held_at": "...",
   "reason": "re-check the recipient, the date and any amount in this email before it is sent",
   "verifier": {"status": "confirmed|discrepancy|timeout", "finding": "...", "ts": "..."},
   "timeout_s": 900, "owner_decision": null}
  (an organization's binding may rename the owner-decision key to its owner's name when it writes its own ledger)

Invariants, tested in assistant/tests/test_metacheck.py:
  * a hold can only ADD a check: no resolution ever removes an option, changes a label, rewrites question text or
    sets anything a consumer could read as "skip the tap";
  * every hold carries a timeout (an aged held card is evidence of when it was checked, not of now);
  * no number or confidence appears in any line this module produces.

I/O: none beyond the ledger path handed to append_row / read_rows (JSONL, one record per line). No organization
vocabulary: the words "customer", "staff", "record" come from the table and may be renamed by the organization.

  python metacheck.py classify <card.json> [--table metacheck.json]
  python metacheck.py calibration <holds.jsonl> [--days 7]
"""
from __future__ import annotations

import json
import re
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
TABLE_FILE = HERE / "metacheck.json"
MAX_HELD_TYPES = 5
DEFAULT_TIMEOUT_S = 900
NONE_TYPE = "none"
STATUSES = ("confirmed", "discrepancy", "timeout")
_MARKER = re.compile(r"\[([A-Z][A-Z ]*PREVIEW)\]")
_NUMBERISH = re.compile(r"\d+(?:\.\d+)?\s*%|\b0\.\d+\b|\bconfidence\b", re.I)


# ------------------------------------------------------------------ table
def _loadj(p, default):
    try:
        return json.loads(Path(p).read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        return default


def load_table(path=None) -> dict:
    """The action-type table. Held types beyond MAX_HELD_TYPES are dropped (the plan's cap), in file order."""
    d = _loadj(path or TABLE_FILE, {})
    held = {}
    for name, row in (d.get("held_types") or {}).items():
        if not isinstance(row, dict):
            continue
        if len(held) >= MAX_HELD_TYPES:
            break
        held[str(name)] = {
            "stakes": str(row.get("stakes") or "high"),
            "exec_kinds": [str(x).strip().lower() for x in (row.get("exec_kinds") or []) if str(x).strip()],
            "markers": [str(x).strip().upper() for x in (row.get("markers") or []) if str(x).strip()],
            "audiences": [str(x).strip().lower() for x in (row.get("audiences") or []) if str(x).strip()],
            "unknown_audience": bool(row.get("unknown_audience")),
            "reason": str(row.get("reason") or "re-check the key fact before this goes out"),
        }
    lines = {"confirmed": "verifier re-checked this: it matches the source",
             "discrepancy": "verifier found a difference, needs your read",
             "timeout": "verifier timed out, verify by hand",
             "waiting_office": "held until the office opens; the re-check timed out after hours"}
    lines.update({k: str(v) for k, v in (d.get("lines") or {}).items() if str(v).strip()})
    try:
        timeout_s = int(d.get("timeout_s") or DEFAULT_TIMEOUT_S)
    except (TypeError, ValueError):
        timeout_s = DEFAULT_TIMEOUT_S
    try:
        verify_within_s = int(d.get("verify_within_s") or max(60, timeout_s * 2 // 3))
    except (TypeError, ValueError):
        verify_within_s = max(60, timeout_s * 2 // 3)
    return {"held_types": held, "lines": lines, "timeout_s": max(60, timeout_s),
            "verify_within_s": max(60, min(verify_within_s, timeout_s))}


# ------------------------------------------------------------------ classify
def _questions(card: dict) -> list:
    qs = card.get("questions") if isinstance(card, dict) else None
    if isinstance(qs, list) and qs:
        return [q for q in qs if isinstance(q, dict)]
    return [card] if isinstance(card, dict) else []


def _exec_kind(q: dict) -> str:
    ex = q.get("exec")
    if isinstance(ex, dict):
        return str(ex.get("kind") or "").strip().lower()
    return ""


def _markers(q: dict) -> list:
    return [m.group(1).strip().upper() for m in _MARKER.finditer(str(q.get("question") or ""))]


def _audience(q: dict) -> str:
    return str(q.get("audience") or "").strip().lower()


def classify_question(q: dict, table: dict | None = None) -> tuple:
    """(action_type, stakes) for ONE question. Exec kind wins (it is what the tap will run); then a preview marker
    with a matching audience; a marker with no audience lands on the first type that takes unknown audiences
    (fail toward the check, never away from it). Nothing matched: (NONE_TYPE, 'low')."""
    table = table or load_table()
    held = table["held_types"]
    kind = _exec_kind(q)
    if kind:
        for name, row in held.items():
            if kind in row["exec_kinds"]:
                return name, row["stakes"]
    marks = _markers(q)
    if marks:
        aud = _audience(q)
        for name, row in held.items():
            if any(m in row["markers"] for m in marks) and aud and aud in row["audiences"]:
                return name, row["stakes"]
        if not aud:
            for name, row in held.items():
                if any(m in row["markers"] for m in marks) and row["unknown_audience"]:
                    return name, row["stakes"]
    return NONE_TYPE, "low"


def classify(card: dict, table: dict | None = None) -> tuple:
    """(action_type, stakes) for a card: the first question that classifies as a held type decides (a card with
    one high-stakes question is a high-stakes card)."""
    table = table or load_table()
    for q in _questions(card):
        t, s = classify_question(q, table)
        if t != NONE_TYPE:
            return t, s
    return NONE_TYPE, "low"


def is_held_type(action_type: str, table: dict | None = None) -> bool:
    table = table or load_table()
    return str(action_type or "") in table["held_types"]


def reason_for(action_type: str, table: dict | None = None) -> str:
    table = table or load_table()
    row = table["held_types"].get(str(action_type or ""))
    return row["reason"] if row else ""


def key_text(card: dict, limit: int = 240) -> str:
    """The head of what the owner would read: the first question's text after any preview marker, one line."""
    qs = _questions(card)
    txt = str(qs[0].get("question") or "") if qs else ""
    txt = _MARKER.sub("", txt)
    txt = re.sub(r"\s+", " ", txt).strip()
    return txt[:limit]


# ------------------------------------------------------------------ records
def stamp(ts: float | None = None) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(ts if ts is not None else time.time()))


def _epoch(s) -> float:
    if isinstance(s, (int, float)):
        return float(s)
    try:
        return time.mktime(time.strptime(str(s)[:19], "%Y-%m-%dT%H:%M:%S"))
    except (TypeError, ValueError):
        return 0.0


def hold(card_id: str, action_type: str, stakes: str = "high", reason: str = "", now: float | None = None,
         timeout_s: int | None = None, text: str = "", outcome: str = "", owner: str = "",
         table: dict | None = None) -> dict:
    """The hold record for a card that IS held (armed). Always carries a timeout."""
    table = table or load_table()
    t = int(timeout_s or table["timeout_s"])
    return {"card": str(card_id), "action_type": str(action_type), "stakes": str(stakes or "high"),
            "held_at": stamp(now), "reason": reason or reason_for(action_type, table) or "re-check the key fact",
            "verifier": None, "timeout_s": max(60, t), "owner_decision": None,
            "held": True, "text": str(text or "")[:240], "outcome": str(outcome or ""), "owner": str(owner or "")}


def shadow(card_id: str, action_type: str, stakes: str, text: str, outcome: str, now: float | None = None,
           owner: str = "", table: dict | None = None) -> dict:
    """The UNARMED row: the same shape, held=false, so the five-day shadow log reads like the live ledger will."""
    table = table or load_table()
    return {"card": str(card_id), "action_type": str(action_type), "stakes": str(stakes or "low"),
            "held_at": stamp(now), "reason": reason_for(action_type, table) if is_held_type(action_type, table) else "",
            "verifier": None, "timeout_s": table["timeout_s"], "owner_decision": None,
            "held": False, "text": str(text or "")[:240], "outcome": str(outcome or ""), "owner": str(owner or "")}


def verdict(status: str, finding: str = "", now: float | None = None) -> dict:
    """A verifier result. Unknown statuses read as 'discrepancy' (a verdict the code cannot name needs the owner's read)."""
    st = str(status or "").strip().lower()
    if st not in STATUSES:
        st = "discrepancy"
    return {"status": st, "finding": re.sub(r"\s+", " ", str(finding or "")).strip()[:600], "ts": stamp(now)}


# ------------------------------------------------------------------ resolve
def resolve(h: dict, verifier: dict | None = None, now: float | None = None, office_open: bool = True,
            table: dict | None = None) -> dict:
    """What happens to the held card now. Returns
      {"status": confirmed|discrepancy|timeout|pending|waiting_office, "release": bool, "line": str,
       "flag": bool, "hold": <the hold with its verifier filled in>}
    `line` is the plain-words detail line to append to the card (empty while pending). `flag` marks a card the owner
    must read closely (a discrepancy). Nothing here touches the card's text or options; see apply()."""
    table = table or load_table()
    lines = table["lines"]
    now = time.time() if now is None else float(now)
    out = dict(h)
    v = verifier if isinstance(verifier, dict) and verifier.get("status") else (h.get("verifier") if isinstance(h.get("verifier"), dict) else None)
    if v and v.get("status") in ("confirmed", "discrepancy"):
        out["verifier"] = verdict(v["status"], v.get("finding", ""), _epoch(v.get("ts")) or now)
        if v["status"] == "confirmed":
            return {"status": "confirmed", "release": True, "line": lines["confirmed"], "flag": False, "hold": out}
        finding = out["verifier"]["finding"]
        line = lines["discrepancy"] + (": " + finding if finding else "")
        return {"status": "discrepancy", "release": True, "line": line, "flag": True, "hold": out}
    held_at = _epoch(h.get("held_at"))
    timeout_s = int(h.get("timeout_s") or table["timeout_s"])
    timed_out = (v and v.get("status") == "timeout") or (held_at and now >= held_at + timeout_s)
    if not timed_out:
        return {"status": "pending", "release": False, "line": "", "flag": False, "hold": out}
    out["verifier"] = verdict("timeout", (v or {}).get("finding", "") if v else "", now)
    if not office_open:
        return {"status": "waiting_office", "release": False, "line": lines["waiting_office"], "flag": False, "hold": out}
    return {"status": "timeout", "release": True, "line": lines["timeout"], "flag": False, "hold": out}


def apply(card: dict, resolution: dict) -> dict:
    """The card with the verifier's line appended to the FIRST question's detail text (and `metacheck` filled in).
    Questions, options and exec payloads are copied through untouched; a changed one raises, since a hold may only
    add a check."""
    before = json.dumps([(q.get("question"), q.get("options"), q.get("exec")) for q in _questions(card)], sort_keys=True)
    out = json.loads(json.dumps(card))
    line = str(resolution.get("line") or "").strip()
    qs = out.get("questions") if isinstance(out.get("questions"), list) else None
    target = qs[0] if qs and isinstance(qs[0], dict) else out
    if line:
        det = str(target.get("details") or "").rstrip()
        target["details"] = (det + "\n\n" if det else "") + "Metacheck: " + line
    mc = dict(out.get("metacheck") or {})
    h = resolution.get("hold") or {}
    mc.update({"action_type": h.get("action_type", mc.get("action_type")), "stakes": h.get("stakes", mc.get("stakes")),
               "status": resolution.get("status"), "flag": bool(resolution.get("flag")),
               "verifier": h.get("verifier")})
    out["metacheck"] = mc
    after = json.dumps([(q.get("question"), q.get("options"), q.get("exec")) for q in _questions(out)], sort_keys=True)
    if before != after:
        raise ValueError("metacheck.apply changed a question, an option or an exec payload; a hold may only add a check")
    if _NUMBERISH.search(line):
        raise ValueError("metacheck line carries a number or a confidence word; plain words only")
    return out


# ------------------------------------------------------------------ ledger (the only I/O, on the path given)
def append_row(path, row: dict) -> None:
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    with p.open("a", encoding="utf-8") as f:
        f.write(json.dumps(row, ensure_ascii=False) + "\n")


def read_rows(path) -> list:
    p = Path(path)
    if not p.is_file():
        return []
    out = []
    for ln in p.read_text(encoding="utf-8", errors="replace").splitlines():
        ln = ln.strip()
        if not ln:
            continue
        try:
            r = json.loads(ln)
        except ValueError:
            continue
        if isinstance(r, dict) and r.get("card"):
            out.append(r)
    return out


def latest_by_card(rows: list) -> dict:
    """card id -> its newest row (a verifier row written later overrides the hold row; same shape, same key)."""
    out = {}
    for r in rows:
        out[str(r["card"])] = r
    return out


def pending(rows: list, now: float | None = None, table: dict | None = None) -> list:
    """Held rows whose verifier has not spoken and whose timeout has not passed: what the deferred verifier picks up."""
    table = table or load_table()
    now = time.time() if now is None else float(now)
    out = []
    for r in latest_by_card(rows).values():
        if not r.get("held"):
            continue
        if resolve(r, None, now, True, table)["status"] == "pending":
            out.append(r)
    return out


# ------------------------------------------------------------------ calibration, in words
def _words(n: int, one: str, many: str | None = None) -> str:
    many = many or (one + "s")
    if n == 0:
        return "no " + many
    if n == 1:
        return "one " + one
    small = {2: "two", 3: "three", 4: "four", 5: "five", 6: "six", 7: "seven", 8: "eight", 9: "nine", 10: "ten"}
    return f"{small.get(n, str(n))} {many}"


def calibration(rows: list, days: int = 7, now: float | None = None) -> list:
    """Plain-words lines over the last `days` of rows (hold and shadow rows alike): high-stakes cards by type, how
    many were held, how the verifier ruled. Counts are spelled out; no ratio, no percent."""
    now = time.time() if now is None else float(now)
    since = now - days * 86400
    recent = [r for r in latest_by_card(rows).values() if _epoch(r.get("held_at")) >= since]
    high = [r for r in recent if str(r.get("stakes")) == "high"]
    lines = [f"Last {days} days: {_words(len(recent), 'card')} logged, {_words(len(high), 'high-stakes card')}."]
    by_type = {}
    for r in high:
        by_type[r.get("action_type")] = by_type.get(r.get("action_type"), 0) + 1
    for t, n in sorted(by_type.items(), key=lambda kv: -kv[1]):
        lines.append(f"  {t.replace('_', ' ')}: {_words(n, 'card')}")
    held = [r for r in high if r.get("held")]
    if held:
        v = {"confirmed": 0, "discrepancy": 0, "timeout": 0, "pending": 0}
        for r in held:
            st = (r.get("verifier") or {}).get("status") if isinstance(r.get("verifier"), dict) else None
            v[st if st in v else "pending"] += 1
        lines.append(f"Held for a re-check: {_words(len(held), 'card')}; the verifier confirmed {_words(v['confirmed'], 'card')}, "
                     f"found a difference on {_words(v['discrepancy'], 'card')}, timed out on {_words(v['timeout'], 'card')}, "
                     f"still pending on {_words(v['pending'], 'card')}.")
    else:
        lines.append("Nothing was held (shadow mode: every card went its usual way).")
    return lines


# ------------------------------------------------------------------ CLI
def main(argv=None) -> int:
    a = list(sys.argv[1:] if argv is None else argv)
    if not a or a[0] not in ("classify", "calibration"):
        print(__doc__.strip().splitlines()[-2:][0].strip())
        print(__doc__.strip().splitlines()[-1].strip())
        return 2
    if a[0] == "classify":
        table = load_table(a[a.index("--table") + 1]) if "--table" in a else load_table()
        card = _loadj(a[1], {})
        t, s = classify(card, table)
        print(json.dumps({"action_type": t, "stakes": s, "held_type": is_held_type(t, table), "reason": reason_for(t, table)}))
        return 0
    days = int(a[a.index("--days") + 1]) if "--days" in a else 7
    for ln in calibration(read_rows(a[1]), days):
        print(ln)
    return 0


if __name__ == "__main__":
    sys.exit(main())

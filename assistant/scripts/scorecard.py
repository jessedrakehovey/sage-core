"""scorecard.py - learning that measures itself (WP6 of the stand-alone proposal, 2026-10-24).

Every week the system says, in numbers, whether it got better. Zero-LLM, read-only over the stores the other
parts already write: the outcome ledger (outcomes.jsonl), the exec tickets, the cards and their answers, the
commitments, the sentinels, the owners' own lines in the transcript, and the per-lane learnings.

  compute(days)   -> {kinds: {kind: {claimed, verified, failed, unverified, abandoned, kept_pct, median_verify_min,
                      retries}}, failure_classes: {class: n}, cards: {posted, answered, edited, skipped, expired,
                      per_decision}, repeat_asks, said_without_deed, stuck_tickets, loops: {source: {generated, acted,
                      discarded, verdict}}, lessons: [{lesson, before, after, verdict}]}
  render(sc)      -> one plain paragraph for the Monday digest
  record(sc)      -> one row in assistant/data/scorecard.jsonl (the trend) + an eval_ledger row
  dream_rows(sc)  -> the lines the nightly review reads (the worst metric and the top failure classes)

Conversion watch (the property OS's loop_health): a loop that produced things nobody acted on is IGNORED (under
10% acted) and one that produced nothing for 7 days is DEAD; both get one line in the digest. Lessons eval: a
learnings line that did not reduce edits of its card kind in 30 days is flagged for the dream to reword or
retire (the one thing the property's ledger lacks).

  python assistant/scripts/scorecard.py [--days 7] [--json] [--record]
"""
from __future__ import annotations

import argparse
import json
import os
import re
import statistics
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
VAULT = ROOT.parent
sys.path.insert(0, str(HERE))
from alib import DATA, load_json  # noqa: E402
import crm

STORE = DATA / "scorecard.jsonl"
LEARN_DIR = Path(os.environ.get("ASSISTANT_LEARNINGS") or (ROOT / "learnings"))
FMT = "%Y-%m-%dT%H:%M:%S"
IGNORED_BELOW = 0.10
DEAD_DAYS = 7
REPEAT = re.compile(r"\b(again|still|didn'?t|did not|never|you (told|said)|where is|nothing happened|not working|fail(ed|ure)?|wrong|no card)\b", re.I)
SKIP_OPTS = re.compile(r"^\s*(skip|drop|dismiss|ignore|not now|later|remind me|leave it|never mind|cancel|no\b)", re.I)
_FAILURE_CLASSES_BASE = [
    ("credential", r"sign(ed)? out|password|login|credential|token|not configured|no login|re-?auth"),
    ("timeout", r"time(d)? out|timeout|unverified"),
    ("connector", None),   # filled lazily with crm.name()
    ("browser", r"browser|click|element|snapshot|doer|page"),
    ("phone", r"phone|outbox|\back\b|imessage|\btext\b"),
    ("refused", r"refus|forbid|not allowed|policy|blank"),
    ("model-claimed", r"no deed|claim with no|said"),
]


def _failure_classes():
    crm_word = re.escape(crm.name().lower())
    return [(name, (rf"{crm_word}|connector|is not a function|\bapi\b|http|sheet" if rx is None else rx))
            for name, rx in _FAILURE_CLASSES_BASE]


# kept for import compatibility (callers that read the list directly)
FAILURE_CLASSES = _failure_classes()


def _ts(s) -> float:
    try:
        return time.mktime(time.strptime(str(s)[:19], FMT))
    except (TypeError, ValueError):
        return 0.0


def _classify(detail: str, kind: str = "") -> str:
    if kind == "said":
        return "model-claimed"
    d = str(detail or "")
    for name, rx in _failure_classes():
        if re.search(rx, d, re.I):
            return name
    return "other"


# ------------------------------------------------------------------ the pieces
def _outcome_metrics(since: float) -> tuple:
    import outcomes
    kinds, classes, said = {}, {}, 0
    for r in outcomes.fold().values():
        if _ts(r.get("updated") or r.get("created")) < since:
            continue
        k = str(r.get("kind") or "?")
        st = str(r.get("state") or "")
        if k == "said":
            said += 1
            classes["model-claimed"] = classes.get("model-claimed", 0) + 1
            continue
        m = kinds.setdefault(k, {"claimed": 0, "verified": 0, "failed": 0, "unverified": 0, "abandoned": 0, "retries": 0, "_mins": []})
        m[st if st in m else "claimed"] += 1
        if (r.get("evidence") or {}).get("retry_ticket"):
            m["retries"] += 1
        if st == "verified" and r.get("closed"):
            m["_mins"].append(max(0.0, (_ts(r["closed"]) - _ts(r.get("created"))) / 60))
        if st in ("failed", "abandoned"):
            det = str((r.get("checks") or [{}])[-1].get("detail") or "")
            c = _classify(det, k)
            classes[c] = classes.get(c, 0) + 1
    for k, m in kinds.items():
        term = m["verified"] + m["failed"] + m["abandoned"]
        m["kept_pct"] = round(100.0 * m["verified"] / term) if term else None
        m["median_verify_min"] = round(statistics.median(m["_mins"]), 1) if m["_mins"] else None
        m.pop("_mins", None)
    return kinds, classes, said


def _card_metrics(since: float) -> dict:
    import cards
    posted = answered = edited = skipped = expired = 0
    try:
        files = list(cards.ARCHIVE.glob("q*.json"))
    except OSError:
        files = []
    answers = cards._answers_index()
    for f in files:
        c = cards._read(f)
        if not isinstance(c, dict) or float(c.get("ts") or 0) < since:
            continue
        posted += 1
        a = answers.get(c.get("id"))
        if a and not a.get("undone"):
            answered += 1
            if a.get("revised"):
                edited += 1
            if SKIP_OPTS.match(str(a.get("option") or "")):
                skipped += 1
        elif str(c.get("status") or "") == "expired":
            expired += 1
    return {"posted": posted, "answered": answered, "edited": edited, "skipped": skipped, "expired": expired,
            "per_decision": round(posted / max(1, answered - skipped), 2) if posted else 0}


def _repeat_asks(since: float) -> int:
    tr = DATA / "agent" / "transcript.jsonl"
    try:
        import json as _j
        n = 0
        for ln in tr.read_text(encoding="utf-8", errors="replace").splitlines()[-4000:]:
            try:
                r = _j.loads(ln)
            except ValueError:
                continue
            if str(r.get("who") or "").lower() in ("sage", "card", "code", "") or _ts(str(r.get("ts") or "").replace(" ", "T")) < since:
                continue
            if REPEAT.search(str(r.get("text") or "")) and not str(r.get("text") or "").startswith("["):
                n += 1
        return n
    except OSError:
        return 0


def _stuck(since: float) -> int:
    n = 0
    for t in (load_json(DATA / "tickets.json", {}) or {}).values():
        if isinstance(t, dict) and t.get("kind") == "stuck_work" and _ts(str(t.get("created") or "").replace("Z", "")) >= since:
            n += 1
    return n


def _loops(since: float, history_days: int = 60) -> dict:
    """Per card source (owner): generated / acted / discarded in the window; DEAD when the source produced nothing
    in DEAD_DAYS after having produced before; IGNORED when under IGNORED_BELOW acted on (5+ generated)."""
    import cards
    answers = cards._answers_index()
    out = {}
    long_since = time.time() - history_days * 86400
    try:
        files = list(cards.ARCHIVE.glob("q*.json"))
    except OSError:
        files = []
    for f in files:
        c = cards._read(f)
        if not isinstance(c, dict) or float(c.get("ts") or 0) < long_since:
            continue
        src = str(c.get("owner") or "?").split(":")[0]
        row = out.setdefault(src, {"generated": 0, "acted": 0, "discarded": 0, "last": 0.0, "ever": 0})
        row["ever"] += 1
        row["last"] = max(row["last"], float(c.get("ts") or 0))
        if float(c.get("ts") or 0) < since:
            continue
        row["generated"] += 1
        a = answers.get(c.get("id"))
        if a and not a.get("undone") and not SKIP_OPTS.match(str(a.get("option") or "")):
            row["acted"] += 1
        elif (a and SKIP_OPTS.match(str(a.get("option") or ""))) or str(c.get("status") or "") == "expired":
            row["discarded"] += 1
    now = time.time()
    for src, row in out.items():
        if row["ever"] and now - row["last"] > DEAD_DAYS * 86400 and row["generated"] == 0:
            row["verdict"] = "DEAD"
        elif row["generated"] >= 5 and row["acted"] / row["generated"] < IGNORED_BELOW:
            row["verdict"] = "IGNORED"
        else:
            row["verdict"] = "ok"
        row["last"] = time.strftime(FMT, time.localtime(row["last"])) if row["last"] else ""
    return out


def _lessons(days_window: int = 30) -> list:
    """Each learnings line names the card (qid) it came from; edits of that card's header kind before and after."""
    import cards
    out = []
    answers = cards._answers_index()
    header_of, ts_of = {}, {}
    try:
        for f in cards.ARCHIVE.glob("q*.json"):
            c = cards._read(f)
            if isinstance(c, dict):
                header_of[c.get("id")] = re.sub(r"[:(].*$", "", str((c.get("questions") or [{}])[0].get("header") or "")).strip().lower()[:30]
                ts_of[c.get("id")] = float(c.get("ts") or 0)
    except OSError:
        pass
    for path in LEARN_DIR.glob("*.md") if LEARN_DIR.exists() else []:
        for ln in path.read_text(encoding="utf-8", errors="replace").splitlines():
            m = re.match(r"- (\d{4}-\d{2}-\d{2}) — (.+?)\s*\((q[0-9a-f]{6,})\)\s*$", ln.strip())
            if not m:
                continue
            day, lesson, qid = m.groups()
            when = _ts(day + "T12:00:00")
            kind = header_of.get(qid, "")
            if not kind:
                continue
            same = [q for q, h in header_of.items() if h == kind and q != qid]
            before = [q for q in same if when - days_window * 86400 <= ts_of.get(q, 0) < when]
            after = [q for q in same if when <= ts_of.get(q, 0) < when + days_window * 86400]
            eb = sum(1 for q in before if (answers.get(q) or {}).get("revised"))
            ea = sum(1 for q in after if (answers.get(q) or {}).get("revised"))
            rb = eb / len(before) if before else None
            ra = ea / len(after) if after else None
            verdict = "too early" if len(after) < 3 else ("no effect" if rb is not None and ra is not None and ra >= rb else "helped")
            out.append({"lane": path.stem, "day": day, "lesson": lesson[:120], "kind": kind, "before": [eb, len(before)], "after": [ea, len(after)], "verdict": verdict})
    return out


# ------------------------------------------------------------------ the card
def compute(days: float = 7) -> dict:
    since = time.time() - days * 86400
    kinds, classes, said = _outcome_metrics(since)
    sc = {"ts": time.strftime(FMT), "days": days, "kinds": kinds, "failure_classes": dict(sorted(classes.items(), key=lambda kv: -kv[1])),
          "said_without_deed": said, "cards": _card_metrics(since), "repeat_asks": _repeat_asks(since), "stuck_tickets": _stuck(since),
          "loops": _loops(since), "lessons": _lessons()}
    tot_term = sum(m["verified"] + m["failed"] + m["abandoned"] for m in kinds.values())
    tot_ver = sum(m["verified"] for m in kinds.values())
    sc["kept_pct_all"] = round(100.0 * tot_ver / tot_term) if tot_term else None
    sc["open_claims"] = sum(m["claimed"] + m["unverified"] for m in kinds.values())
    worst = sorted([(k, m) for k, m in kinds.items() if m.get("kept_pct") is not None and (m["verified"] + m["failed"] + m["abandoned"]) >= 3], key=lambda km: km[1]["kept_pct"])
    sc["worst_kind"] = worst[0][0] if worst else ""
    return sc


def render(sc: dict) -> str:
    k = sc.get("kinds") or {}
    tot = sum(m["verified"] + m["failed"] + m["abandoned"] for m in k.values())
    bits = []
    if tot:
        bits.append(f"{tot} things reached a verdict this week, {sc.get('kept_pct_all')}% confirmed landed")
        worst = sc.get("worst_kind")
        if worst and (k[worst].get("kept_pct") or 100) < 100:
            bits.append(f"the weakest kind was {worst.replace('_', ' ')} at {k[worst]['kept_pct']}%")
    else:
        bits.append("nothing reached a verdict this week yet")
    if sc.get("open_claims"):
        bits.append(f"{sc['open_claims']} still unconfirmed")
    fc = sc.get("failure_classes") or {}
    if fc:
        top = list(fc.items())[:2]
        bits.append("failures were mostly " + " and ".join(f"{n} {c}" for c, n in top))
    c = sc.get("cards") or {}
    if c.get("posted"):
        bits.append(f"{c['posted']} cards, {c['answered']} answered ({c['edited']} edited, {c['skipped']} skipped, {c['expired']} expired)")
    if sc.get("repeat_asks"):
        bits.append(f"{sc['repeat_asks']} lines where you had to repeat or correct")
    if sc.get("said_without_deed"):
        bits.append(f"{sc['said_without_deed']} times I said something was in motion with nothing behind it")
    bad_loops = [(f"{s} (nothing in {DEAD_DAYS} days)" if r["verdict"] == "DEAD" else f"{s} ({r['acted']} of {r['generated']} acted on)")
                 for s, r in (sc.get("loops") or {}).items() if r.get("verdict") in ("DEAD", "IGNORED") and s not in ("mc-test", "test")]
    if bad_loops:
        bits.append("loops nobody uses: " + "; ".join(bad_loops[:3]))
    no_effect = [l for l in (sc.get("lessons") or []) if l.get("verdict") == "no effect"]
    if no_effect:
        bits.append(f"{len(no_effect)} lesson{'s' if len(no_effect) != 1 else ''} did not reduce your edits")
    try:                                  # the reflex (assistant/agent/reflex.py): faults caught and fixed by code
        sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "agent"))
        import reflex
        rb = reflex.scorecard_bit(7)
        if rb:
            bits.append(rb)
    except Exception:  # noqa: BLE001
        pass
    try:                                  # long-running problems (assistant/agent/projects.py): on the board, moved, solved
        import projects
        pb = projects.scorecard_bit(7)
        if pb:
            bits.append(pb)
    except Exception:  # noqa: BLE001
        pass
    return "Scorecard: " + "; ".join(bits) + "."


def dream_rows(sc: dict) -> list:
    """What the nightly review should weigh: the worst kind, the top failure classes, the ignored loops, the lessons
    that did not help. Each a line; the dream proposes at most one rule, skill or build request against the worst."""
    rows = [f"SCORECARD {render(sc)}"]
    for c, n in list((sc.get("failure_classes") or {}).items())[:3]:
        rows.append(f"SCORECARD failure class {c}: {n} this week")
    for s, r in (sc.get("loops") or {}).items():
        if r.get("verdict") in ("DEAD", "IGNORED"):
            rows.append(f"SCORECARD loop {s} is {r['verdict']}: {r['acted']} of {r['generated']} acted on")
    for l in (sc.get("lessons") or []):
        if l.get("verdict") == "no effect":
            rows.append(f"SCORECARD lesson had no effect ({l['lane']}, {l['day']}): \"{l['lesson']}\" edits {l['before'][0]}/{l['before'][1]} before, {l['after'][0]}/{l['after'][1]} after")
    return rows


def record(sc: dict) -> None:
    STORE.parent.mkdir(parents=True, exist_ok=True)
    slim = {k: v for k, v in sc.items() if k not in ("lessons",)}
    with open(STORE, "a", encoding="utf-8") as f:
        f.write(json.dumps(slim, ensure_ascii=False) + "\n")
    try:
        import subprocess
        subprocess.run([sys.executable, str(VAULT / "scripts" / "evals" / "eval_ledger.py"), "record", "--pipeline", "scorecard",
                        "--artifact", str(STORE), "--verdict", "PASS" if (sc.get("kept_pct_all") or 0) >= 70 or sc.get("kept_pct_all") is None else "FAIL",
                        "--note", render(sc)[:300]], capture_output=True, text=True, timeout=60, cwd=str(VAULT))
    except Exception:  # noqa: BLE001
        pass


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=float, default=7)
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--record", action="store_true")
    a = ap.parse_args()
    sc = compute(a.days)
    if a.record:
        record(sc)
    print(json.dumps(sc, indent=1, ensure_ascii=False) if a.json else render(sc))
    print("RESULT SCORECARD kept=%s open=%s" % (sc.get("kept_pct_all"), sc.get("open_claims")))
    return 0


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    sys.exit(main())

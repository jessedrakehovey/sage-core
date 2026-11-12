#!/usr/bin/env python3
"""packets_from_cards.py - regression packets made from the owners' answered cards (Seed F, Sage binding, 2026-11-03).

Every answered card is an owner's judgment on a real situation: the system proposed something (the card), the owner
approved it, declined it, skipped it, edited the draft or typed a note. This turns those answers into packets for
scripts/evals/regression_packets.py (one JSONL line each, under packets/cards/<agent>.jsonl), zero LLM, nothing sent.

Rules (the plan's, kept): expectations come from what the owner DID: an approved exec card expects the `approve` deed and
forbids none; a declined or skipped one forbids `approve` (do not re-propose) and expects a plain `reply`; an edited
draft forbids the phrases the owner edited away; the rationale is quoted ONLY from the owner's own typed note, else
null (the packet runs on its deeds). Near-identical cards cluster into one packet (same header family and answer
family, with names, numbers, dates and quoted text blanked); the packet carries n_cards and the qids it stands for.
Declines, edits and typed notes go to the `regression` suite; plain approvals to `full`. Scoring is `deeds` for an
exec card (the replay must show the same deed), `judge` otherwise. The replay is a two-turn exchange: input.history
holds the card as the agent's own earlier post, input.text is the owner's answer, both dated as of the card.

  python scripts/evals/packets_from_cards.py [--days 90] [--out scripts/evals/packets/cards] [--dry-run]

Sources: assistant/data/cards/answers.jsonl (the taps) + assistant/data/cards/archive/<qid>.json (the cards).
Org-free: the agent name, the owners and the deed kinds come from the pack (people.json, agent/intents.py).
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
VAULT = HERE.parents[1]
ROOT = VAULT / "assistant"
sys.path.insert(0, str(ROOT / "scripts"))
from alib import DATA  # noqa: E402

ANSWERS = DATA / "cards" / "answers.jsonl"
ARCHIVE = DATA / "cards" / "archive"
OUT = HERE / "packets" / "cards"
AGENT = "assistant"
DECLINE = re.compile(r"^(?:skip|no\b|decline|drop|not now|don't|do not|hold|later|handled|did not look|i handled)", re.I)
_BLANK = [(re.compile(r"\b\d[\d,./:-]*\b"), "N"), (re.compile(r"\"[^\"]{1,80}\""), "\"...\""), (re.compile(r"'[^']{1,80}'"), "'...'"),
          (re.compile(r"\b[A-Z][a-z]+(?:\s+[A-Z][a-z]+)?\b"), "Name"), (re.compile(r"[\w.+-]+@[\w.-]+"), "address")]


def _rows(path: Path) -> list:
    try:
        return [json.loads(ln) for ln in path.read_text(encoding="utf-8-sig", errors="replace").splitlines() if ln.strip()]
    except (OSError, ValueError):
        return []


def _card(qid: str, archive: Path) -> dict | None:
    try:
        d = json.loads((archive / f"{qid}.json").read_text(encoding="utf-8-sig"))
        return d if isinstance(d, dict) else None
    except (OSError, ValueError):
        return None


def _q(card: dict) -> dict:
    qs = card.get("questions")
    return qs[0] if isinstance(qs, list) and qs and isinstance(qs[0], dict) else card


def family(text: str) -> str:
    s = str(text or "")
    for rx, rep in _BLANK:
        s = rx.sub(rep, s)
    return re.sub(r"\s+", " ", s).strip()[:120].lower()


def answer_family(option: str) -> str:
    o = str(option or "").replace("(Recommended)", "").strip()
    if DECLINE.match(o):
        return "decline"
    return "approve" if o else "none"


def _sender(by: str, owners: list) -> str:
    b = str(by or "").split(":")[-1].strip().lower()
    return b if b in owners else (owners[0] if owners else "owner")


def _owners() -> list:
    try:
        d = json.loads((ROOT / "agent" / "people.json").read_text(encoding="utf-8-sig"))
        return [str(k) for k, u in (d.get("users") or {}).items() if str(u.get("role") or "owner") == "owner"]
    except (OSError, ValueError):
        return []


def _edited_away(before: str, after: str) -> list:
    """Sentences of the draft the owner removed when they edited it: the replay must not say them."""
    if not before or not after:
        return []
    sb = {s.strip() for s in re.split(r"(?<=[.!?])\s+", before) if len(s.strip()) > 12}
    sa = set(re.split(r"(?<=[.!?])\s+", after))
    return [s[:120] for s in sorted(sb) if s not in sa and s not in after][:4]


def packet_for(cluster: list, owners: list) -> dict:
    """One packet from the answers of one cluster (same card family, same answer family); the newest answer leads."""
    cluster = sorted(cluster, key=lambda r: str(r["answer"].get("ts") or ""))
    lead = cluster[-1]
    card, a = lead["card"], lead["answer"]
    q = _q(card)
    ex = q.get("exec") if isinstance(q.get("exec"), dict) else {}
    fam = answer_family(a.get("option"))
    note = str(a.get("text") or "").strip()
    when = str(card.get("ts") and time.strftime("%Y-%m-%d %H:%M", time.localtime(float(card["ts"]))) or a.get("ts") or "")[:16]
    history = [f"[{when} {AGENT}] {str(q.get('header') or '')}: {str(q.get('question') or '')[:1500]}"]
    expect, forbid, forbid_phrases = [], [], []
    if ex:
        if fam == "approve":
            expect = ["approve"]
        elif fam == "decline":
            forbid = ["approve"]
            expect = ["reply"]
    else:
        expect = ["reply"]
    if a.get("revised") and a.get("revised_text"):
        forbid_phrases = _edited_away(str((ex.get("payload") or {}).get("body") or (ex.get("payload") or {}).get("text") or ""), str(a["revised_text"]))
    stem = re.sub(r"[^a-z0-9]+", "-", (family(q.get("header") or q.get("question") or "card") + "-" + fam)).strip("-")[:60]
    pid = f"card-{stem}-{str(a.get('qid') or '')[-6:]}"
    suite = "regression" if (fam == "decline" or note or a.get("revised")) else "full"
    return {"id": pid, "agent": AGENT, "tested_rule": "cards." + (str(ex.get("kind")) if ex else "decision"),
            "rationale": note or None,
            "input": {"sender": _sender(a.get("by"), owners), "role": "owner", "text": str(a.get("option") or "").replace(" (Recommended)", "") + (f" {note}" if note else ""),
                      "thread": f"card:{a.get('qid')}", "history": history, "as_of": when},
            "fixtures": {}, "expect_deeds": expect, "forbid_deeds": forbid, "forbid_phrases": forbid_phrases, "require_phrases": [],
            "suite": suite, "scoring": "deeds" if ex else "judge", "n_cards": len(cluster), "qids": [r["answer"].get("qid") for r in cluster],
            "source": {"kind": "card", "exec": str(ex.get("kind") or ""), "answer_family": fam, "made": time.strftime("%Y-%m-%d")}}


def build(days: float = 90, answers: Path | None = None, archive: Path | None = None) -> list:
    answers, archive = answers or ANSWERS, archive or ARCHIVE
    owners = _owners()
    since = time.time() - float(days) * 86400
    clusters: dict = {}
    for a in _rows(answers):
        if a.get("undone") or float(a.get("epoch") or 0) < since or not a.get("qid"):
            continue
        card = _card(str(a["qid"]), archive)
        if not card:
            continue
        q = _q(card)
        key = (family(q.get("header") or q.get("question")), answer_family(a.get("option")))
        clusters.setdefault(key, []).append({"answer": a, "card": card})
    out = [packet_for(c, owners) for c in clusters.values()]
    out.sort(key=lambda p: p["id"])
    return out


def write(packets: list, out_dir: Path | None = None) -> Path:
    out_dir = out_dir or OUT
    out_dir.mkdir(parents=True, exist_ok=True)
    p = out_dir / f"{AGENT}.jsonl"
    p.write_text("".join(json.dumps(x, ensure_ascii=False) + "\n" for x in packets), encoding="utf-8")
    return p


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--days", type=float, default=90)
    ap.add_argument("--out", default="")
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args(argv)
    sys.stdout.reconfigure(encoding="utf-8")
    packets = build(a.days)
    reg = sum(1 for p in packets if p["suite"] == "regression")
    print(f"PACKETS {len(packets)} from the owners' answered cards ({reg} regression, {len(packets) - reg} full)")
    if a.dry_run:
        for p in packets[:20]:
            print(f"  {p['id']}  n={p['n_cards']}  {p['suite']}/{p['scoring']}  expect={p['expect_deeds']} forbid={p['forbid_deeds']}")
        return 0
    print("wrote", write(packets, Path(a.out) if a.out else None))
    return 0


if __name__ == "__main__":
    sys.exit(main())

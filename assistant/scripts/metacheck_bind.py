"""metacheck_bind.py - Seed C bound to this agent's cards (2026-11-03, the owner: "update sage so it fits the seeds").

agent/metacheck.py decides, as pure functions, which cards are high-stakes (by ACTION TYPE from agent/metacheck.json:
a message to a customer, a message to a colleague, a write to a system of record, a payment) and what a verifier's
verdict does to the card: it may only ADD a plain-words line, never touch the question, the options or the exec
payload, never show a number. This module is the binding: it runs at cards.post, before the card is written, so the
owner reads the verifier's line with the card.

The verifier here is deterministic and instant (no model, no delay: an urgent card still reaches Chat the minute it is
posted). It re-reads the facts the tap would act on against the records this PC already holds:
  - the recipient of a send: a phone number or an address the phone books / the CRM know (contacts.lookup), or the
    person the card is replying to (the inbound's own sender); an unknown one is a discrepancy
  - every date written in the message: a date already in the past is a discrepancy (a wrong year, a stale draft)
  - a payment's amount, when the record names one and the message names another
Nothing to re-check (no recipient, no date, no amount) reads as confirmed with that said in words. An error inside the
verifier reads as a timeout ("verify by hand"): the hold never blocks a card and never auto-releases anything.

Every card gets ONE ledger row (data/agent/metacheck.jsonl): a HOLD row with the verdict for a held type, a SHADOW row
(held=false) for the rest, so `metacheck.py calibration data/agent/metacheck.jsonl` reads the whole picture and the
weekly readback can say how many high-stakes cards the verifier confirmed or disputed.

  python assistant/scripts/metacheck_bind.py classify <card.json>     # (action_type, stakes) for a posted card
  python assistant/scripts/metacheck_bind.py verify <card.json>       # the verifier's verdict, nothing written
  python assistant/scripts/metacheck_bind.py calibration [--days 7]   # plain-words lines over the ledger
"""
from __future__ import annotations

import json
import re
import sys
import time
from datetime import date, datetime
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parent / "agent"))
from alib import DATA, ROOT  # noqa: E402
import metacheck as MC  # noqa: E402

LEDGER = DATA / "agent" / "metacheck.jsonl"
TABLE = ROOT / "agent" / "metacheck.json"
_MONTHS = "jan|feb|mar|apr|may|jun|jul|aug|sep|sept|oct|nov|dec"
_DATE_ISO = re.compile(r"\b(20\d{2})-(\d{2})-(\d{2})\b")
_DATE_US = re.compile(r"\b(\d{1,2})/(\d{1,2})/(20\d{2}|\d{2})\b")
_DATE_WORD = re.compile(r"\b(?:(" + _MONTHS + r")[a-z]*\.?\s+(\d{1,2})(?:st|nd|rd|th)?(?:,?\s+(20\d{2}))?)\b", re.I)
_MONEY = re.compile(r"\$\s?(\d{1,3}(?:,\d{3})*|\d+)(?:\.(\d{2}))?")


def table() -> dict:
    return MC.load_table(TABLE)


def _questions(card: dict) -> list:
    qs = card.get("questions")
    return [q for q in qs if isinstance(q, dict)] if isinstance(qs, list) else [card]


def _payload(card: dict) -> dict:
    for q in _questions(card):
        ex = q.get("exec")
        if isinstance(ex, dict) and isinstance(ex.get("payload"), dict):
            return ex["payload"]
    return {}


def _text(card: dict) -> str:
    q = _questions(card)[0] if _questions(card) else {}
    p = _payload(card)
    return "\n".join(str(x or "") for x in (q.get("question"), q.get("details"), p.get("text"), p.get("body"), p.get("subject")))


# ------------------------------------------------------------------ the deterministic verifier
def _recipients(card: dict) -> list:
    p = _payload(card)
    to = p.get("to")
    out = [str(x).strip() for x in (to if isinstance(to, (list, tuple)) else [to]) if str(x or "").strip()]
    return out


def _known(handle: str, card: dict) -> bool:
    """A phone book / CRM contact, or the person this card replies to (the inbound's sender)."""
    h = str(handle or "").strip()
    q = _questions(card)[0] if _questions(card) else {}
    inbound = str(q.get("reply_to") or q.get("from_handle") or q.get("inbound_from") or "").strip()
    if inbound and _same_handle(inbound, h):
        return True
    try:
        import contacts
        return contacts.lookup(h) is not None
    except Exception:  # noqa: BLE001
        return False


def _same_handle(a: str, b: str) -> bool:
    try:
        import contacts
        if "@" in a or "@" in b:
            return contacts.email_key(a) == contacts.email_key(b)
        return contacts.phone_key(a) == contacts.phone_key(b)
    except Exception:  # noqa: BLE001
        return a.lower() == b.lower()


def _dates(text: str, today: date) -> list:
    out = []
    for y, m, d in _DATE_ISO.findall(text):
        try:
            out.append(date(int(y), int(m), int(d)))
        except ValueError:
            pass
    for m, d, y in _DATE_US.findall(text):
        try:
            yy = int(y) if len(y) == 4 else 2000 + int(y)
            out.append(date(yy, int(m), int(d)))
        except ValueError:
            pass
    for mon, d, y in _DATE_WORD.findall(text):
        try:
            mi = ("jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec").index(mon.lower()[:3]) + 1
            yy = int(y) if y else today.year
            dt = date(yy, mi, int(d))
            if not y and dt < today:            # "Nov 12" with no year is the next one
                dt = date(yy + 1, mi, int(d))
            out.append(dt)
        except ValueError:
            pass
    return out


def verify(card: dict, today: date | None = None) -> dict:
    """-> {"status": confirmed|discrepancy, "finding": words} over the facts the tap would act on."""
    today = today or date.today()
    problems, checked = [], []
    rcpts = _recipients(card)
    if rcpts:
        unknown = [r for r in rcpts if not _known(r, card)]
        if unknown:
            problems.append("the recipient is nobody the phone books, the CRM or this conversation know")
        else:
            checked.append("the recipient is known")
    text = _text(card)
    past = [d for d in _dates(text, today) if d < today]
    if past:
        problems.append("a date in the message is already in the past")
    elif _dates(text, today):
        checked.append("the dates in it are ahead")
    p = _payload(card)
    amt = p.get("amount")
    if amt not in (None, ""):
        said = {m[0].replace(",", "") for m in _MONEY.findall(text)}
        try:
            want = str(int(float(amt)))
        except (TypeError, ValueError):
            want = ""
        if want and said and want not in said:
            problems.append("the amount in the words is not the amount on the record")
        elif want:
            checked.append("the amount matches the record")
    if problems:
        return {"status": "discrepancy", "finding": "; ".join(problems)}
    return {"status": "confirmed", "finding": ("; ".join(checked) if checked else "no recipient, date or amount to re-check")}


# ------------------------------------------------------------------ the binding
def bind(card: dict, owner: str = "", now: float | None = None, ledger: Path | None = None, today: date | None = None) -> dict:
    """The card as it should be written: classified, and for a held type re-checked with the verifier's line appended
    (metacheck.apply). One ledger row either way. Never raises: a failure inside reads as a timeout line."""
    now = time.time() if now is None else float(now)
    ledger = ledger or LEDGER
    tb = table()
    t, stakes = MC.classify(card, tb)
    key = MC.key_text(card)
    if not MC.is_held_type(t, tb):
        MC.append_row(ledger, MC.shadow(card.get("id", ""), t, stakes, key, "", now, owner, tb))
        return card
    h = MC.hold(card.get("id", ""), t, stakes, "", now, None, key, "", owner, tb)
    try:
        v = verify(card, today)
        res = MC.resolve(h, MC.verdict(v["status"], v["finding"], now), now, True, tb)
    except Exception as e:  # noqa: BLE001
        res = MC.resolve(h, MC.verdict("timeout", f"verifier failed: {type(e).__name__}", now), now, True, tb)
    try:
        out = MC.apply(card, res)
    except ValueError:                           # the invariant tripped: the card goes out untouched, the row says so
        out = card
        res["hold"]["outcome"] = "apply refused"
    MC.append_row(ledger, res["hold"])
    return out


def calibration_lines(days: int = 7, ledger: Path | None = None) -> list:
    rows = MC.read_rows(ledger or LEDGER)
    return MC.calibration(rows, days)


def main(argv=None) -> int:
    a = list(argv if argv is not None else sys.argv[1:])
    sys.stdout.reconfigure(encoding="utf-8")
    if not a:
        print(__doc__)
        return 2
    if a[0] == "calibration":
        days = int(a[a.index("--days") + 1]) if "--days" in a else 7
        for ln in calibration_lines(days):
            print(ln)
        return 0
    card = json.loads(Path(a[1]).read_text(encoding="utf-8-sig"))
    if a[0] == "classify":
        print(json.dumps(MC.classify(card, table())))
    elif a[0] == "verify":
        print(json.dumps(verify(card), indent=1))
    else:
        print(__doc__)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())

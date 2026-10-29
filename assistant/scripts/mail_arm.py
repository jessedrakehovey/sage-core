"""mail_arm.py - money + home lanes: arm follow-ups from the mail the sensor already ticketed.

Zero-LLM, read-only on mail (it reads TICKETS, never the mailbox). Runs after `mail.py sense`
and before `followups.py tick`. For each new mail ticket it looks for three shapes and arms
the matching SOP step (assistant/data/sop_map.json):

  bill / statement with a due date     -> money/bill-due        (reminds T-3d and on the day)
  renewal notice with a date           -> money/renewal         (T-14d)
  shipment with an expected date       -> home/delivery-arriving (the day it lands)

Conservative on purpose: it arms ONLY when a date parses and lies in the future (within a
year). Everything else is left to the operator, which still sees the ticket. It never pays,
never clicks a link, never opens the message; amounts are copied from the subject/snippet
for the reminder text only.

  python assistant/scripts/mail_arm.py            -> prints SENSE_RESULT mail_arm ...
"""
from __future__ import annotations

import re
import sys
from datetime import date, datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import followups  # noqa: E402
import tickets  # noqa: E402
from alib import DATA, load_json, save_json  # noqa: E402

STATE = DATA / "mail_arm_state.json"
MONTHS = {m: i for i, m in enumerate(["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"], 1)}
_AMOUNT = re.compile(r"\$\s?(\d{1,3}(?:,\d{3})*(?:\.\d{2})?|\d+(?:\.\d{2})?)")
_DATE_PATTERNS = [
    re.compile(r"\b(\d{4})-(\d{2})-(\d{2})\b"),                                                       # 2026-10-15
    re.compile(r"\b(\d{1,2})/(\d{1,2})(?:/(\d{2,4}))?\b"),                                            # 10/15[/2026]
    re.compile(r"\b(jan|feb|mar|apr|may|jun|jul|aug|sep|sept|oct|nov|dec)[a-z]*\.?\s+(\d{1,2})(?:st|nd|rd|th)?(?:,?\s+(\d{4}))?", re.I),
]
BILL = re.compile(r"\b(payment due|amount due|due (?:on|by|date)|statement (?:is )?ready|your bill|autopay scheduled|minimum payment|past due)\b", re.I)
_DUE = re.compile(r"\b(payment due|amount due|due (?:on|by|date)|past due)\b", re.I)
RENEW = re.compile(r"\b(renews? on|renewal|will renew|expires? on|expiring|registration due|policy (?:period|renew))\b", re.I)
SHIP = re.compile(r"\b(arriving|out for delivery|estimated delivery|expected delivery|delivery (?:date|by)|will arrive|shipped)\b", re.I)


ROLL_AFTER_DAYS = 180      # a yearless date this far back means next year ("Jan 5" read in December)


def find_dates(text: str, today: date, window_days: int = 365, allow_past_days: int = 0) -> list:
    """Every plausible absolute date in `text` as [(pos, end, date, has_year)], in text order.
    Kept inside today-allow_past_days .. today+window_days. A date WITHOUT a year that is only a
    little in the past ("payment received Sep 30", read on Oct 2) is a past date, not next year's:
    it used to be rolled forward a year and armed a reminder for something already paid. A date
    with ANY year is taken as written. Shared with tracking/mining.py (ruling 3), which reads the
    same patterns rather than keeping a second copy."""
    found = []
    for i, pat in enumerate(_DATE_PATTERNS):
        for m in pat.finditer(text):
            try:
                has_year = True
                if i == 0:
                    d = date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
                elif i == 1:
                    y = m.group(3)
                    has_year = bool(y)
                    y = int(y) + (2000 if y and len(y) == 2 else 0) if y else today.year
                    d = date(y, int(m.group(1)), int(m.group(2)))
                else:
                    mon = MONTHS[m.group(1)[:3].lower()]
                    has_year = bool(m.group(3))
                    y = int(m.group(3)) if m.group(3) else today.year
                    d = date(y, mon, int(m.group(2)))
                if d < today and not has_year and (today - d).days > ROLL_AFTER_DAYS:
                    d = d.replace(year=d.year + 1)
            except (ValueError, KeyError):
                continue
            if today - timedelta(days=allow_past_days) <= d <= today + timedelta(days=window_days):
                found.append((m.start(), m.end(), d, has_year))
    found.sort()
    return found


def find_date(text: str, today: date, after: int = 0):
    """The first plausible future date (today .. today+365) at or after character `after`,
    else None (see find_dates for the year rules)."""
    found = [(pos, d) for pos, _end, d, _y in find_dates(text, today)]
    return next((d for pos, d in found if pos >= after), found[0][1] if found and not after else None)


def classify(subject: str, snippet: str, today: date):
    """-> (sop, step, vars) or None."""
    text = f"{subject}\n{snippet}"
    what = re.sub(r"\s+", " ", subject).strip()[:80] or "item"
    for sop, step, pat in (("home", "delivery-arriving", SHIP), ("money", "bill-due", BILL), ("money", "renewal", RENEW)):
        k = pat.search(text)
        if not k:
            continue
        if step == "bill-due":               # "due on/by ..." names the due date; "statement ready" does not
            k = _DUE.search(text) or k
        # the date that FOLLOWS the keyword is the one it talks about ("Statement 10/01 ... due on
        # 10/25" is due 10/25); only when nothing follows it, the first date in the text
        d = find_date(text, today, after=k.start()) or find_date(text, today)
        if not d:
            return None
        vars = {"what": what, "anchor": d.isoformat()}
        if step == "bill-due":
            amt = _AMOUNT.search(text, k.start()) or _AMOUNT.search(text)
            vars["amount"] = "$" + amt.group(1) if amt else "amount not stated"
        return sop, step, vars
    return None


def run(today=None):
    today = today or date.today()
    st = load_json(STATE, {"seen": []})
    seen = set(st.get("seen", []))
    armed = scanned = 0
    for t in tickets._load().values():
        if not str(t.get("source", "")).startswith("mail:") or t["id"] in seen:
            continue
        seen.add(t["id"])
        scanned += 1
        p = t.get("payload") or {}
        hit = classify(str(p.get("subject") or t.get("title") or ""), str(p.get("snippet") or t.get("summary") or ""), today)
        if not hit:
            continue
        sop, step, vars = hit
        if step == "delivery-arriving" and p.get("bulk"):
            continue                         # "shipped" in a newsletter is not a parcel on its way
        # the same bill mailed twice (statement, then a reminder) is ONE duty: key on who + when
        sender = re.sub(r".*<|>.*", "", str(p.get("from") or "")).strip().lower() or t["id"]
        vars["unit"] = f"{sender}|{vars['anchor']}"
        vars["cancel"] = "python assistant/scripts/followups.py mark <id> --status done"
        try:
            r = followups.arm(sop, step, vars, "mail_arm")
        except ValueError:
            continue
        armed += bool(r.get("armed"))
    st["seen"] = sorted(seen)[-5000:]
    save_json(STATE, st)
    return {"scanned": scanned, "armed": armed}


if __name__ == "__main__":
    try:
        r = run()
        print(f"SENSE_RESULT mail_arm scanned={r['scanned']} armed={r['armed']}")
    except Exception as e:
        print(f"SENSE_RESULT mail_arm error={type(e).__name__}: {e}")

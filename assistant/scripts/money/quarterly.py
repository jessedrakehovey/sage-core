"""quarterly.py - running Schedule C profit -> federal + California estimated quarterlies; set-asides.

From the ledger's business buckets (money_rules.json side=business): income minus expenses year to
date = Schedule C profit. `annualize` (money.json -> quarterly.method) projects it over the year,
then: SE tax (92.35% x 15.3%, Social Security part capped), AGI = profit - half SE + other income,
federal taxable = AGI - standard deduction - QBI (20%), federal brackets; California taxable =
AGI - CA standard deduction, CA brackets. Tables live in money.json -> tax_tables (re-checked by the
owner each January; the shipped federal 2026 numbers are from Rev. Proc. 2025-32, CA is 2025).
Safe harbour: when quarterly.prior_year_total_tax is set, the lower of the two is used.

Each due date (money.json -> quarterly.due) is ARMED through followups.py as the existing `money /
quarterly-estimate` step (T-10d, unit q:<year>-<n>); the amount it should name is in
data/money/quarterly.json for whoever works the ticket. After a CRM client payment lands (bucket
income:crm) ONE card suggests the set-aside, stating what Relay's auto-split already moves
when money.json -> relay.splits_pct is filled in. Report only: nothing here moves or pays anything.

  python assistant/scripts/money/quarterly.py            # tick (the spine runs this)
  python assistant/scripts/money/quarterly.py --dry-run  # print the estimate and what would be posted
  python assistant/scripts/money/quarterly.py estimate   # the numbers, as JSON
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import common as C  # noqa: E402
import crm  # noqa: E402

OUT = C.DIR / "quarterly.json"


# ------------------------------------------------------------------ math (pure)
def bracket_tax(taxable: float, brackets: list) -> float:
    tax, lower = 0.0, 0.0
    for upper, rate in brackets or []:
        if taxable <= lower:
            break
        hi = float(upper) if upper is not None else float("inf")
        tax += (min(taxable, hi) - lower) * float(rate)
        lower = hi
    return round(max(0.0, tax), 2)


def se_tax(profit: float, fed: dict) -> float:
    base = max(0.0, profit) * float(fed.get("se_base_factor") or 0.9235)
    ss = min(base, float(fed.get("ss_wage_base") or 10 ** 9)) * float(fed.get("ss_rate") or 0.124)
    med = base * float(fed.get("medicare_rate") or 0.029)
    return round(ss + med, 2)


def estimate_from_profit(profit_annual: float, c: dict) -> dict:
    """Full-year federal + CA liability for an annual Schedule C profit."""
    tt = c["tax_tables"]
    fed, ca, q = tt.get("federal") or {}, tt.get("california") or {}, c["quarterly"]
    other = float(q.get("other_income_annual") or 0)
    se = se_tax(profit_annual, fed)
    agi = profit_annual - se / 2 + other
    qbi = max(0.0, (profit_annual - se / 2)) * float(fed.get("qbi_pct") or 0.2)
    fed_taxable = max(0.0, agi - float(fed.get("standard_deduction") or 0) - qbi)
    fed_income = bracket_tax(fed_taxable, fed.get("brackets") or [])
    ca_taxable = max(0.0, agi - float(ca.get("standard_deduction") or 0))
    ca_tax = bracket_tax(ca_taxable, ca.get("brackets") or [])
    federal_total = round(fed_income + se, 2)
    prior = q.get("prior_year_total_tax")
    safe = None
    if isinstance(prior, (int, float)) and prior > 0:
        safe = round(float(prior) * (1.10 if agi > 150000 else 1.0), 2)
    return {"profit_annual": round(profit_annual, 2), "se_tax": se, "agi": round(agi, 2), "qbi": round(qbi, 2),
            "federal_taxable": round(fed_taxable, 2), "federal_income_tax": fed_income, "federal_total": federal_total,
            "ca_taxable": round(ca_taxable, 2), "ca_tax": ca_tax, "total": round(federal_total + ca_tax, 2),
            "safe_harbor_federal": safe,
            "effective_rate": round((federal_total + ca_tax) / profit_annual, 3) if profit_annual > 0 else 0.0}


# ------------------------------------------------------------------ ledger sums
def schedule_c(led: dict, year: int, r: dict | None = None, until: date | None = None) -> dict:
    r = r or C.rules()
    buckets = r.get("buckets") or {}
    income = expense = paid_fed = paid_ca = 0.0
    by_bucket = {}
    for t in C.txns_sorted(led, date(year, 1, 1), until or date(year, 12, 31)):
        b = buckets.get(t.get("bucket") or "")
        if not b or t.get("pending"):
            continue
        amt = float(t.get("amount") or 0)
        if b.get("side") == "business" and b.get("kind") == "income":
            income += amt
            by_bucket[t["bucket"]] = by_bucket.get(t["bucket"], 0.0) + amt
        elif b.get("side") == "business" and b.get("kind") == "expense":
            expense += -amt
            by_bucket[t["bucket"]] = by_bucket.get(t["bucket"], 0.0) + -amt
        elif t.get("bucket") == "tax:federal":
            paid_fed += -amt
        elif t.get("bucket") == "tax:california":
            paid_ca += -amt
    return {"year": year, "income": round(income, 2), "expense": round(expense, 2), "profit": round(income - expense, 2),
            "paid_federal": round(paid_fed, 2), "paid_ca": round(paid_ca, 2), "by_bucket": {k: round(v, 2) for k, v in by_bucket.items()}}


def due_dates(c: dict, year: int) -> list:
    out = []
    for d in c["quarterly"].get("due") or []:
        y = year + 1 if d.get("next_year") else year
        out.append({"n": int(d["n"]), "date": date(y, int(d["month"]), int(d["day"])), "federal_share": float(d.get("federal_share") or 0.25),
                    "ca_share": float(d.get("ca_share") or 0.25)})
    return out


def estimate(led: dict, c: dict | None = None, today: date | None = None) -> dict:
    c = c or C.cfg()
    today = today or date.today()
    year = today.year if today.month > 1 or today.day > 15 else today.year - 1   # Jan 1-15 still settles last year's Q4
    sc = schedule_c(led, year, until=min(today, date(year, 12, 31)))
    elapsed = (min(today, date(year, 12, 31)) - date(year, 1, 1)).days + 1
    frac = max(elapsed / 365.0, 1 / 12)
    annual = sc["profit"] / frac if c["quarterly"].get("method", "annualize") == "annualize" else sc["profit"]
    est = estimate_from_profit(max(0.0, annual), c)
    fed_year = min(est["federal_total"], est["safe_harbor_federal"]) if est.get("safe_harbor_federal") else est["federal_total"]
    dues = due_dates(c, year)
    nxt = next((d for d in dues if d["date"] >= today), None)
    fed_due_so_far = sum(d["federal_share"] for d in dues if d["date"] <= (nxt["date"] if nxt else date(year + 1, 1, 15)))
    ca_due_so_far = sum(d["ca_share"] for d in dues if d["date"] <= (nxt["date"] if nxt else date(year + 1, 1, 15)))
    fed_pay = max(0.0, fed_year * fed_due_so_far - sc["paid_federal"])
    ca_pay = max(0.0, est["ca_tax"] * ca_due_so_far - sc["paid_ca"])
    pct = est["effective_rate"] if est["profit_annual"] > 0 else float(c["quarterly"].get("set_aside_pct_floor") or 0.25)
    pct = min(max(pct, float(c["quarterly"].get("set_aside_pct_floor") or 0.25)), float(c["quarterly"].get("set_aside_pct_cap") or 0.4))
    return {"year": year, "as_of": today.isoformat(), "ytd": sc, "annualized": est, "method": c["quarterly"].get("method", "annualize"),
            "next_due": ({"n": nxt["n"], "date": nxt["date"].isoformat(), "federal": round(fed_pay, 2), "ca": round(ca_pay, 2),
                          "total": round(fed_pay + ca_pay, 2)} if nxt else None),
            "set_aside_pct": round(pct, 3)}


# ------------------------------------------------------------------ reminders + set-asides
def arm_next(est: dict, c: dict, dry: bool) -> bool:
    nd = est.get("next_due")
    if not nd:
        return False
    if dry:
        print(f"WOULD ARM quarterly-estimate q:{est['year']}-{nd['n']} on {nd['date']} ({C.money(nd['total'])})")
        return True
    import followups
    try:
        followups.arm("money", "quarterly-estimate", {"unit": f"q:{est['year']}-{nd['n']}", "anchor": nd["date"],
                                                       "amount": C.money(nd["total"]), "what": f"Q{nd['n']} estimated tax"}, "money.quarterly")
        return True
    except Exception as e:  # noqa: BLE001
        C.log("arm_error", key=f"q:{est['year']}-{nd['n']}", error=f"{type(e).__name__}: {str(e)[:120]}")
        return False


def new_payments(led: dict, st: dict, today: date | None = None, since_days: int = 14) -> list:
    seen = set(st.get("set_aside_seen") or [])
    floor = ((today or date.today()) - timedelta(days=since_days)).isoformat()
    rows = [t for t in led["transactions"].values() if t.get("bucket") == "income:crm" and not t.get("pending")
            and t["id"] not in seen and str(t.get("posted") or "") >= floor and float(t.get("amount") or 0) > 0]
    rows.sort(key=lambda t: t.get("posted", ""))
    return rows


def set_aside_card(rows: list, est: dict, led: dict, c: dict) -> dict:
    total = sum(float(t["amount"]) for t in rows)
    pct = float(est.get("set_aside_pct") or 0.25)
    want = total * pct
    accts = {(led["accounts"].get(t["account_id"]) or {}).get("name") or "?" for t in rows}
    splits = c["relay"].get("splits_pct") or {}
    relay_tax = splits.get("tax")
    if isinstance(relay_tax, (int, float)):
        moved = total * float(relay_tax) / 100.0
        tail = (f" Relay's split already moves {relay_tax:g}% ({C.money(moved)}) to Tax, so "
                + (f"{C.money(want - moved)} more belongs there." if want - moved > 1 else "you are covered."))
        advice = (f"💡 Noted is enough: Relay's split covers it." if want - moved <= 1
                  else f"💡 Top the Tax bucket up by {C.money(want - moved)}: Relay's {relay_tax:g}% is under the {pct * 100:.0f}% this year's rate needs.")
    else:
        tail = " Tell me Relay's tax-split percentage once (money.json -> relay.splits_pct) and I will net it out."
        advice = f"💡 Set aside {C.money(want)} ({pct * 100:.0f}% of it) for taxes before spending the rest; Q{(est.get('next_due') or {}).get('n', '?')} is due {(est.get('next_due') or {}).get('date', '?')}."
    n = len(rows)
    q = {"header": "Money: set-aside", "lane": "money",
         "question": (f"{n} {crm.name()} payment{'s' if n != 1 else ''} landed ({C.money(total)}) in {', '.join(sorted(accts))}. "
                      f"At this year's rate ({pct * 100:.0f}%) set aside {C.money(want)} for taxes." + tail),
         "details": "\n".join(f"{t['posted']}  {C.money(float(t['amount']), True)}  {t.get('description', '')[:50]}" for t in rows),
         "advice": advice,
         "options": [{"label": "Noted (Recommended)"}, {"label": "Relay already covers it"}]}
    return q


def tick(now=None, dry: bool = False) -> dict:
    c = C.cfg()
    now = now or C.now_local()
    led = C.ledger()
    st = C.state()
    est = estimate(led, c, now.date())
    armed = arm_next(est, c, dry)
    rows = new_payments(led, st, now.date())
    posted = False
    if rows and C.in_hours(now, c["forecast"].get("post_hours") or ["08:00", "20:30"]):
        q = set_aside_card(rows, est, led, c)
        if dry:
            print("WOULD POST CARD " + json.dumps(q, ensure_ascii=False)[:800])
        else:
            import cards
            # "Noted" / "Relay already covers it" are not actions the system can take: the set-aside
            # is a chat line (the owner 2026-10-17 evening, cards.NEEDS_ACTION)
            r = cards.say("money.quarterly", "money", f"{q['header']}: {q['question']} {q['advice']}".strip())
            if r.get("said"):
                posted = True
                st["set_aside_seen"] = (st.get("set_aside_seen") or [])[-500:] + [t["id"] for t in rows]
                C.log("set_aside_said", n=len(rows))
            else:
                C.log("set_aside_refused", reason=str(r.get("reason"))[:160])
    if not dry:
        C.save_json(OUT, est)
        C.save_state(st)
    out = {"year": est["year"], "profit_ytd": est["ytd"]["profit"], "next_due": est.get("next_due"), "armed": armed,
           "set_aside_rows": len(rows), "posted": posted}
    print("QUARTERLY " + json.dumps(out))
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("cmd", nargs="?", default="tick", choices=["tick", "estimate"])
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args()
    if a.cmd == "estimate":
        print(json.dumps(estimate(C.ledger()), indent=1))
        return 0
    tick(dry=a.dry_run)
    return 0


if __name__ == "__main__":
    sys.exit(main())

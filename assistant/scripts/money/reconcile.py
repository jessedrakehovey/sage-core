"""reconcile.py - the weekly reconciliation: does the ledger still agree with every bank, and do the
envelopes still add up? ONE card a week (reconcile.day / after, once per ISO week), held for the Monday
digest like every other money card; the owner's tap is the reconciliation.

Per spending account and card the bank's own balance is checked against what the ledger expects:
  expected = the balance at the last reconciliation + every posted (non-pending) transaction since.
Within reconcile.tolerance the account is reconciled; off by more and the card names the difference and
the likely reasons it can see (a transaction the feed sent twice: same amount and merchant within
dup_window_days; a pending row older than pending_max_days that never posted; a single transaction equal
to the difference). A balance older than stale_days is "stale": the feed, not the money, needs a look.
The tap "Reconciled" takes the bank's balances as the new baseline for every account (the only way a
feed hiccup is ever cleared, and it is the owner's call). "Hold on" leaves the baselines alone.

The same card lists the envelopes that are overspent (with the envelope that has the most slack to cover
each), how many of the week's transactions are still unplaced (the Friday card places them), and the
age of money (how many days the dollars spent this week sat in the accounts before they left; the
number an envelope budget tries to grow). Words typed in the card's thread ("move 50 from dining to
groceries", "cover groceries", "assign 200 to travel") are applied through budget.py. Zero LLM; read-only
on every bank.

  python assistant/scripts/money/reconcile.py            # tick: harvest answers + weekly card when due (the spine runs this)
  python assistant/scripts/money/reconcile.py check      # the account check and age of money now, as JSON
  python assistant/scripts/money/reconcile.py baseline   # take today's bank balances as the baseline (what the tap does)
  python assistant/scripts/money/reconcile.py --dry-run | --force
"""
from __future__ import annotations

import argparse
import json
import statistics
import sys
import time
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import common as C  # noqa: E402
import budget as B  # noqa: E402

HEADER = "Money: this week's reconciliation"


# ------------------------------------------------------------------ the account check (pure)
def _since(led: dict, aid: str, base: dict) -> list:
    through = str(base.get("through") or "")
    same = set(base.get("same_day_ids") or [])
    out = []
    for t in led["transactions"].values():
        if t.get("account_id") != aid:
            continue
        p = str(t.get("posted") or "")[:10]
        if p > through or (p == through and t.get("id") not in same):
            out.append(t)
    out.sort(key=lambda t: (t.get("posted", ""), t.get("id", "")))
    return out


def _suspects(rows: list, diff: float, c: dict, today: date) -> list:
    k = c["reconcile"]
    out = []
    win = int(k.get("dup_window_days") or 3)
    keyed = {}
    for t in rows:
        if t.get("pending"):
            continue
        key = (round(float(t.get("amount") or 0), 2), C.merchant_key(t.get("description") or ""))
        for u in keyed.get(key, []):
            d1, d2 = C.pdate(t.get("posted")), C.pdate(u.get("posted"))
            if d1 and d2 and abs((d1 - d2).days) <= win:
                out.append(f"{str(t.get('description') or '')[:28]} {C.money(float(t['amount']), cents=True)} appears twice ({u['posted']} and {t['posted']})")
        keyed.setdefault(key, []).append(t)
    old = today - timedelta(days=int(k.get("pending_max_days") or 10))
    for t in rows:
        d = C.pdate(t.get("posted"))
        if t.get("pending") and d and d < old:
            out.append(f"{str(t.get('description') or '')[:28]} {C.money(float(t['amount']), cents=True)} has been pending since {t['posted']}")
    for t in rows:
        if abs(abs(float(t.get("amount") or 0)) - abs(diff)) < 0.005:
            out.append(f"the difference is exactly {str(t.get('description') or '')[:28]} {C.money(float(t['amount']), cents=True)} on {t['posted']}")
            break
    return out[:4]


def check(led: dict, b: dict | None = None, c: dict | None = None, today: date | None = None) -> dict:
    """Every spending account and card: bank balance vs the ledger's expectation since the last baseline."""
    c = c or C.cfg()
    b = b if b is not None else B.load()
    today = today or C.now_local().date()
    k = c["reconcile"]
    tol = float(k.get("tolerance") or 1.0)
    rows = []
    for a in B.accounts(led, c):
        if a["role"] not in ("spending", "card"):
            continue
        bank = round(float(a.get("balance") or 0), 2)
        as_of = C.pdate(a.get("balance_date"))
        row = {"id": a["id"], "name": a.get("name"), "side": a["side"], "role": a["role"], "bank": bank,
               "as_of": as_of.isoformat() if as_of else "", "state": "no baseline", "expected": None, "diff": 0.0, "note": "", "suspects": []}
        if as_of and (today - as_of).days > int(k.get("stale_days") or 3):
            row["state"], row["note"] = "stale", f"the bank balance is from {as_of.isoformat()}"
        base = (b.get("reconciled") or {}).get(a["id"])
        if base:
            since = _since(led, a["id"], base)
            exp = round(float(base.get("balance") or 0) + sum(float(t.get("amount") or 0) for t in since if not t.get("pending")), 2)
            diff = round(bank - exp, 2)
            row.update({"expected": exp, "diff": diff, "since": len(since)})
            if row["state"] != "stale":
                if abs(diff) <= tol:
                    row["state"] = "ok"
                else:
                    row["state"] = "off"
                    row["note"] = f"bank {C.money(bank, cents=True)}, ledger expects {C.money(exp, cents=True)}: off by {C.money(diff, cents=True)}"
                    stuck = [t for t in led["transactions"].values() if t.get("account_id") == a["id"] and t.get("pending") and t not in since]
                    row["suspects"] = _suspects(since + stuck, diff, c, today)
        rows.append(row)
    return {"rows": rows, "age_of_money": age_of_money(led, c, today), "unplaced_week": unplaced_week(led, c, today),
            "date": today.isoformat()}


def baseline(b: dict, led: dict, c: dict | None = None, today: date | None = None, ids: list | None = None) -> int:
    """Take each account's bank balance as the truth from here on."""
    c = c or C.cfg()
    today = today or C.now_local().date()
    n = 0
    for a in B.accounts(led, c):
        if a["role"] not in ("spending", "card") or (ids and a["id"] not in ids):
            continue
        through = (C.pdate(a.get("balance_date")) or today).isoformat()
        same = sorted(t["id"] for t in led["transactions"].values()
                      if t.get("account_id") == a["id"] and str(t.get("posted") or "")[:10] == through)
        b.setdefault("reconciled", {})[a["id"]] = {"date": today.isoformat(), "balance": round(float(a.get("balance") or 0), 2),
                                                   "through": through, "same_day_ids": same}
        n += 1
    return n


# ------------------------------------------------------------------ age of money + the week's unplaced rows
def age_of_money(led: dict, c: dict | None = None, today: date | None = None) -> int | None:
    """FIFO: the dollars spent most recently came from the oldest income not yet spent; how old was it?
    The median over the last reconcile.age_sample outflows, in days. None when there is no income on record."""
    c = c or C.cfg()
    today = today or C.now_local().date()
    k = c["reconcile"]
    floor = (today - timedelta(days=int(k.get("age_lookback_days") or 365))).isoformat()
    accts = {a["id"] for a in B.accounts(led, c) if a["role"] in ("spending", "card")}
    inc, out = [], []
    for t in led["transactions"].values():
        if t.get("account_id") not in accts or str(t.get("posted") or "") < floor or t.get("pending"):
            continue
        bkt = str(t.get("bucket") or "")
        if bkt in ("transfer", "ignore") or (not bkt and C.is_transfer(t.get("description") or "")):
            continue
        amt = float(t.get("amount") or 0)
        d = C.pdate(t.get("posted"))
        if not d:
            continue
        (inc if amt > 0 else out).append((d, abs(amt)))
    if not inc or not out:
        return None
    inc.sort()
    out.sort()
    queue = [[d, a] for d, a in inc]
    ages = []
    for d, a in out:
        while a > 0.005 and queue:
            qd, qa = queue[0]
            take = min(a, qa)
            a -= take
            queue[0][1] -= take
            age = (d - qd).days
            if queue[0][1] <= 0.005:
                queue.pop(0)
        if a > 0.005:
            age = 0                                    # spent more than ever came in: no age to speak of
        ages.append(max(age, 0))
    sample = ages[-int(k.get("age_sample") or 10):]
    return int(statistics.median(sample)) if sample else None


def unplaced_week(led: dict, c: dict | None = None, today: date | None = None) -> dict:
    c = c or C.cfg()
    today = today or C.now_local().date()
    floor = (today - timedelta(days=7)).isoformat()
    accts = {a["id"] for a in B.accounts(led, c) if a["role"] in ("spending", "card")}
    n, amt = 0, 0.0
    for t in led["transactions"].values():
        if t.get("account_id") in accts and str(t.get("posted") or "") >= floor and not t.get("bucket") \
                and float(t.get("amount") or 0) < 0 and not C.is_transfer(t.get("description") or ""):
            n += 1
            amt += -float(t["amount"])
    return {"n": n, "amount": round(amt, 2)}


# ------------------------------------------------------------------ the weekly card
def _cover_from(env: dict, side_rows: list) -> str:
    best = max((x for x in side_rows if x["key"] != env["key"] and x["available"] >= -env["available"]),
               key=lambda x: x["available"], default=None)
    return best["label"] if best else ""


def card(led: dict, b: dict, c: dict, r: dict, today: date) -> dict | None:
    chk = check(led, b, c, today)
    mdl = B.model(led, b, c, r, today)
    rows = chk["rows"]
    if not rows:
        return None
    k = c["reconcile"]
    lines = []
    ok = [x for x in rows if x["state"] == "ok"]
    if ok:
        lines.append(f"{len(ok)} account{'s' if len(ok) != 1 else ''} match the bank to the dollar.")
    for x in [x for x in rows if x["state"] == "off"][:int(k.get("max_rows") or 8)]:
        s = f"{x['name']}: {x['note']}."
        if x["suspects"]:
            s += " Likely: " + "; ".join(x["suspects"]) + "."
        lines.append(s)
    for x in [x for x in rows if x["state"] == "stale"]:
        lines.append(f"{x['name']}: {x['note']}; the feed needs a look before the money does.")
    fresh = [x for x in rows if x["state"] == "no baseline"]
    if fresh:
        lines.append(f"{len(fresh)} account{'s' if len(fresh) != 1 else ''} get{'s' if len(fresh) == 1 else ''} a first baseline when you tap Reconciled.")
    for side, s in mdl["sides"].items():
        for env in s["overspent"]:
            src = _cover_from(env, s["envelopes"])
            lines.append(f"{env['label']} ({side}) is overspent by {C.money(-env['available'])}"
                         + (f"; {src} has the slack to cover it: type \"cover {env['label'].lower()} from {src.lower()}\"." if src
                            else "; nothing on that side has enough slack, so it comes out of next month."))
        if s["ready"] < 0:
            lines.append(f"Ready to assign ({side}) is {C.money(s['ready'])}: the cards owe more than the cash covers.")
    uw = chk["unplaced_week"]
    if uw["n"]:
        lines.append(f"{uw['n']} of this week's transactions ({C.money(uw['amount'])}) have no envelope yet; the Friday card places them.")
    aom = chk["age_of_money"]
    if aom is not None:
        lines.append(f"Age of money: {aom} day{'s' if aom != 1 else ''} (how long the dollars spent this week sat in the accounts first).")
    totals = " · ".join(f"{side}: ready {C.money(s['ready'])}, in envelopes {C.money(s['held'])}, cards owe {C.money(s['card_debt'])}"
                        for side, s in mdl["sides"].items())
    off = [x for x in rows if x["state"] == "off"]
    stale = [x for x in rows if x["state"] == "stale"]
    overs = [e["label"] for s in mdl["sides"].values() for e in s["overspent"]]
    head = [f"Week of {today.strftime('%b %d')}: {len(ok)} of {len(rows)} accounts match the bank"
            + (f", {len(off)} off ({', '.join(str(x['name']) for x in off[:3])})" if off else "")
            + (f", {len(stale)} stale" if stale else "") + (f", {len(fresh)} awaiting a first baseline" if fresh else "") + "."]
    if overs:
        head.append(f"Overspent: {', '.join(overs[:4])}" + (" and more" if len(overs) > 4 else "") + ".")
    if uw["n"]:
        head.append(f"{uw['n']} rows unplaced this week.")
    if aom is not None:
        head.append(f"Age of money {aom}d.")
    return {"header": HEADER, "lane": "money",
            "question": " ".join(head) + " The account-by-account check is in the details.",
            "details": "\n".join(lines) + "\n\n" + totals,
            "advice": "💡 Reconciled: the bank's balances become the new baseline for every account. Type any moves between envelopes in this thread.",
            "options": [{"label": "Reconciled, all good (Recommended)", "description": "take the bank's balances as the baseline"},
                        {"label": "Hold on", "description": "leave the baselines; the same check comes back next week"}]}


def harvest(b: dict, led: dict, c: dict, r: dict, answers: list | None = None) -> int:
    pending = b.get("week_cards") or {}
    if not pending:
        return 0
    if answers is None:
        import categorize
        answers = categorize.card_answers(60)
    by_qid = {str(a.get("qid")): a for a in answers if not a.get("undone")}
    n = 0
    for qid in list(pending):
        a = by_qid.get(qid)
        if not a:
            if float(pending[qid].get("ts") or 0) < time.time() - 45 * 86400:
                pending.pop(qid)
            continue
        pending.pop(qid)
        opt = str(a.get("option") or "")
        said = []
        if opt.startswith("Reconciled"):
            said.append(f"baselined {baseline(b, led, c)} accounts")
        ops, bad = B.parse_words(str(a.get("text") or a.get("revised_text") or ""), r)
        said += B.run_ops(b, ops, led, B.month_key(C.now_local().date()), c, r, f"week card {qid}")
        n += 1
        C.log("week_answer", qid=qid, option=opt[:40], said=said, bad=bad)
        if ops:
            B._lesson("; ".join(said), qid, c)
    b["week_cards"] = pending
    return n


def tick(now=None, dry: bool = False, force: bool = False) -> dict:
    c, r = C.cfg(), C.rules()
    k = c["reconcile"]
    now = now or C.now_local()
    led = C.ledger()
    b = B.load()
    harvested = harvest(b, led, c, r)
    wk = C.iso_week(now)
    due = force or (C.DOW[now.weekday()] == str(k.get("day") or "sun").lower()[:3] and (now.hour, now.minute) >= C.hm(k.get("after") or "18:00"))
    out = {"harvested": harvested, "qid": ""}
    if not k.get("card", True) and not force:
        due = False                                     # the money talk asks instead (brief.py)
    if due and (b.get("week_posted") != wk or force):
        q = card(led, b, c, r, now.date())
        if q:
            if dry:
                print("WOULD POST CARD " + json.dumps(q, ensure_ascii=False)[:2000])
            else:
                import cards
                res = cards.post([q], owner="money.reconcile", lane="money")
                if res.get("posted"):
                    out["qid"] = res["qid"]
                    b.setdefault("week_cards", {})[res["qid"]] = {"week": wk, "ts": time.time()}
                    b["week_posted"] = wk
                    C.log("week_card", qid=res["qid"])
                else:
                    C.log("week_refused", reason=str(res.get("reason"))[:200])
        elif not dry:
            b["week_posted"] = wk
    if not dry:
        B.save(b)
    print("RECONCILE " + json.dumps(out))
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("cmd", nargs="?", default="tick", choices=["tick", "check", "baseline"])
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--force", action="store_true")
    a = ap.parse_args()
    if a.cmd == "check":
        print(json.dumps(check(C.ledger()), indent=1, ensure_ascii=False))
        return 0
    if a.cmd == "baseline":
        b = B.load()
        n = baseline(b, C.ledger())
        if not a.dry_run:
            B.save(b)
        print(f"baselined {n} accounts")
        return 0
    tick(dry=a.dry_run, force=a.force)
    return 0


if __name__ == "__main__":
    sys.exit(main())

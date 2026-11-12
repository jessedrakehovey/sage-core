"""forecast.py - recurring charges from history -> upcoming bills, armed as follow-ups; two warnings.

From the ledger alone (no model): a merchant that charged the same account at a steady interval
(money.json -> forecast.periods: weekly / monthly / quarterly / yearly, each with a tolerance in days)
at least min_occurrences times is a recurring bill. Its next due date is the last charge plus the
period. Every bill due inside horizon_days is ARMED through assistant/scripts/followups.py as the
existing `money / bill-due` step (unit `bill:<merchant>|<account>`; followups dedupes, re-times a moved
anchor, and fires the T-3d reminder ticket) - no new reminder path.

Two warnings, each ONE card, latched warn_latch_days per key, posted only inside post_hours:
  low balance   the bills due in the next low_balance_days on an account exceed its balance
  price jump    the latest charge is more than price_jump_pct (and price_jump_min dollars) above
                the merchant's median

Merchant classes (2026-11-01, the owner: "You might be counting one time purchases as reoccurring"): every
merchant is `bill` (subscription or bill), `stores`, `restaurants`, `transfer`, `income` or `other`: the
owner's word first (money_rules.json -> merchant_classes, set by "Not a bill" on the price card), then its
bucket, then its name (forecast.variable_match), then its amounts. Only a `bill` is recurring, so only a
bill is armed or can say "price went up"; a bill nobody placed must also have steady amounts (the charges
before the latest within amount_cv_max), so a bigger shop is never a price rise. Own-account transfers
(common.is_transfer) are never spending.
Writes data/money/forecast.json for the readback. Never pays anything.

  python assistant/scripts/money/forecast.py            # tick (the spine runs this)
  python assistant/scripts/money/forecast.py --dry-run  # print bills + warnings, arm and post nothing
  python assistant/scripts/money/forecast.py list       # the recurring bills it sees
"""
from __future__ import annotations

import argparse
import json
import re
import statistics
import sys
import time
from collections import defaultdict
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import common as C  # noqa: E402

OUT = C.DIR / "forecast.json"


def _period(gaps: list, periods: dict) -> str:
    if not gaps:
        return ""
    med = statistics.median(gaps)
    best, best_d = "", 10 ** 9
    for name, (days, tol) in periods.items():
        d = abs(med - float(days))
        if d <= float(tol) and d < best_d:
            best, best_d = name, d
    if not best:
        return ""
    days, tol = periods[best]
    if sum(1 for g in gaps if abs(g - float(days)) <= float(tol) * 1.5) < max(2, int(len(gaps) * 0.6)):
        return ""
    return best


NOT_A_BILL = "Not a bill, just shopping"


def _cv(amts: list) -> float:
    if len(amts) < 2:
        return 0.0
    m = statistics.mean(amts)
    return statistics.pstdev(amts) / m if m else 0.0


def merchant_class(key: str, rows: list, c: dict | None = None, r: dict | None = None) -> tuple:
    """(class, source) for one merchant's charges, oldest first: the owner's word, then the bucket,
    then the name, then the amounts."""
    c = c or C.cfg()
    f = c["forecast"]
    r = r if r is not None else C.rules()
    own = str((r.get("merchant_classes") or {}).get(key) or "")
    if own:
        return own, "owner"
    last = rows[-1] if rows else {}
    bucket = str(last.get("bucket") or "")
    kind = ((r.get("buckets") or {}).get(bucket) or {}).get("kind")
    if bucket in ("transfer", "ignore") or kind == "transfer":
        return "transfer", "bucket"
    if kind == "income":
        return "income", "bucket"
    if bucket in (f.get("bill_buckets") or []):
        return "bill", "bucket"
    for cls, keys in (f.get("variable_buckets") or {}).items():
        if bucket in (keys or []):
            return cls, "bucket"
    desc = str(last.get("description") or "")
    if C.is_transfer(desc, c):
        return "transfer", "name"
    name = f"{key} {desc}".lower()
    if not re.search(str(f.get("subscription_match") or "$^"), name):
        for cls, rx in (f.get("variable_match") or {}).items():
            try:
                if rx and re.search(str(rx), name):
                    return cls, "name"
            except re.error:
                continue
    hist = [abs(float(t.get("amount") or 0)) for t in rows][-7:-1]
    if _cv(hist) > float(f.get("amount_cv_max") or 0.4):
        return "other", "amounts"
    return "bill", "amounts"


def recurring(led: dict, c: dict | None = None, today: date | None = None) -> list:
    """[{key, merchant, account_id, account, period, every_days, amount (median), last_amount,
         last_posted, next_due, n, jump: bool}] sorted by next_due."""
    c = c or C.cfg()
    f = c["forecast"]
    today = today or date.today()
    r = C.rules()
    periods = {k: (float(v[0]), float(v[1])) for k, v in (f.get("periods") or {}).items() if isinstance(v, (list, tuple)) and len(v) == 2}
    groups = defaultdict(list)
    for t in led["transactions"].values():
        amt = float(t.get("amount") or 0)
        if amt >= 0 or t.get("pending") or t.get("bucket") in ("transfer", "ignore") or C.is_transfer(t.get("description"), c):
            continue
        key = C.merchant_key(t.get("description") or "")
        if not key:
            continue
        groups[(key, t.get("account_id"))].append(t)
    out = []
    for (key, aid), rows in groups.items():
        if len(rows) < int(f.get("min_occurrences") or 3):
            continue
        rows.sort(key=lambda t: t.get("posted", ""))
        dates = [C.pdate(t["posted"]) for t in rows]
        if any(d is None for d in dates):
            continue
        gaps = [(dates[i] - dates[i - 1]).days for i in range(1, len(dates)) if (dates[i] - dates[i - 1]).days > 0]
        period = _period(gaps, periods)
        if not period:
            continue
        cls, src = merchant_class(key, rows, c, r)
        if cls != "bill":
            continue                                   # a bigger shop is not a price rise (2026-11-01)
        amts = [abs(float(t["amount"])) for t in rows]
        med = statistics.median(amts[-6:])
        last = amts[-1]
        every = periods[period][0]
        nxt = dates[-1] + timedelta(days=int(every))
        while nxt < today - timedelta(days=int(periods[period][1])):
            nxt += timedelta(days=int(every))
        jump = last > med * (1 + float(f.get("price_jump_pct") or 0.1)) and last - med >= float(f.get("price_jump_min") or 2)
        acct = led["accounts"].get(aid) or {}
        out.append({"key": f"bill:{key}|{aid}", "merchant": key.title(), "account_id": aid, "account": acct.get("name") or "?",
                    "bucket": rows[-1].get("bucket") or "", "period": period, "every_days": int(every), "amount": round(med, 2),
                    "last_amount": round(last, 2), "last_posted": dates[-1].isoformat(), "next_due": nxt.isoformat(),
                    "n": len(rows), "jump": jump, "days_to_due": (nxt - today).days, "class": cls, "class_src": src,
                    "merchant_key": key})
    out.sort(key=lambda b: b["next_due"])
    return out


def arm_bills(bills: list, c: dict, today: date, dry: bool) -> int:
    horizon = int(c["forecast"].get("horizon_days") or 35)
    due = [b for b in bills if 0 <= b["days_to_due"] <= horizon]
    if dry:
        for b in due:
            print(f"WOULD ARM bill-due {b['merchant']} {C.money(b['amount'], True)} on {b['next_due']} ({b['account']})")
        return len(due)
    import followups
    n = 0
    for b in due:
        try:
            r = followups.arm("money", "bill-due", {"unit": b["key"], "what": f"{b['merchant']} ({b['period']})",
                                                     "amount": C.money(b["amount"], True), "anchor": b["next_due"]}, "money.forecast")
            n += bool(r) and str(r.get("status", "")) not in ("duplicate",) if isinstance(r, dict) else bool(r)
        except Exception as e:  # noqa: BLE001
            C.log("arm_error", key=b["key"], error=f"{type(e).__name__}: {str(e)[:120]}")
    return n


def warnings(bills: list, led: dict, c: dict, today: date) -> list:
    f = c["forecast"]
    out = []
    soon = defaultdict(list)
    for b in bills:
        if 0 <= b["days_to_due"] <= int(f.get("low_balance_days") or 7):
            soon[b["account_id"]].append(b)
    for aid, rows in soon.items():
        acct = led["accounts"].get(aid) or {}
        bal = acct.get("balance")
        if bal is None or acct.get("kind") == "card":
            continue
        need = sum(b["amount"] for b in rows)
        if need > float(bal):
            names = ", ".join(f"{b['merchant']} {C.money(b['amount'])} ({b['next_due'][5:]})" for b in rows[:4])
            out.append({"key": f"lowbal:{aid}:{today.isocalendar()[1]}", "kind": "low_balance", "account": acct.get("name") or aid,
                        "header": "Money: low balance", "amount": round(need - float(bal), 2),
                        "question": f"{acct.get('name') or 'An account'} holds {C.money(bal)} but {C.money(need)} of bills land in the next "
                                    f"{f.get('low_balance_days', 7)} days: {names}. I never move money; this is yours to top up.",
                        "advice": f"💡 Move at least {C.money(need - float(bal))} into {acct.get('name') or 'the account'} before "
                                  f"{rows[0]['next_due']}; a bounced autopay costs a fee and a late mark."})
    for b in bills:
        if b["jump"]:
            out.append({"key": f"jump:{b['key']}:{b['last_amount']}", "kind": "price_jump", "account": b["account"],
                        "merchant_key": b.get("merchant_key", ""),
                        "header": "Money: price went up", "amount": round(b["last_amount"] - b["amount"], 2),
                        "question": f"{b['merchant']} charged {C.money(b['last_amount'], True)} on {b['last_posted']}; it used to be "
                                    f"{C.money(b['amount'], True)} every {b['period'][:-2] if b['period'].endswith('ly') else b['period']} "
                                    f"({b['n']} charges on {b['account']}). Keep it, or is it time to shop or cancel?",
                        "advice": f"💡 Keep if you still use it: the rise is {C.money(b['last_amount'] - b['amount'], True)} a "
                                  f"{b['period'].replace('ly', '')}, {C.money((b['last_amount'] - b['amount']) * (365 / b['every_days']))} a year."})
    return out


def post_warnings(ws: list, st: dict, c: dict, now, dry: bool) -> int:
    latch_days = int(c["forecast"].get("warn_latch_days") or 7)
    seen = st.setdefault("warned", {})
    for k in list(seen):
        if time.time() - float(seen[k]) > 60 * 86400:
            seen.pop(k)
    posted = 0
    for w in ws:
        if time.time() - float(seen.get(w["key"], 0)) < latch_days * 86400:
            continue
        if not C.in_hours(now, c["forecast"].get("post_hours") or ["08:00", "20:30"]):
            continue
        q = {"header": w["header"], "lane": "money", "question": w["question"], "advice": w["advice"],
             "options": ([] if w["kind"] == "low_balance"
                         else [{"label": "Keep (Recommended)"}, {"label": "Shop it"}, {"label": "Cancel it myself"},
                               {"label": NOT_A_BILL, "description": "it changes with what I buy; no price card for it again"}])}
        if dry:
            print("WOULD POST CARD " + json.dumps(q, ensure_ascii=False)[:600])
            posted += 1
            continue
        import cards
        if w["kind"] == "price_jump":
            # 2026-11-09: nine "Money: price went up" cards sat in one Monday digest. A price rise is information, not a
            # tap: one idea line under the digest (agent/ideas.py), said once per rise; an owner answers in words.
            try:
                sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "agent"))
                import ideas
                ideas.suggest(w["question"].replace(" Keep it, or is it time to shop or cancel?", ""), "money_forecast", key=w["key"],
                              say=f"not a bill: {w.get('merchant_key') or 'that one'}" if w.get("merchant_key") else "")
                seen[w["key"]] = time.time()
                posted += 1
                C.log("warning_idea", kind=w["kind"], key=w["key"])
            except Exception as e:  # noqa: BLE001
                C.log("warning_refused", kind=w["kind"], reason=f"{type(e).__name__}: {str(e)[:120]}")
            continue
        if w["kind"] == "low_balance":
            # a heads-up nobody can tap into action is a chat line, not a "Noted" card (the owner 2026-10-17)
            r = cards.say("money.forecast", "money", f"{w['header']}: {w['question']} {w['advice']}".strip())
            r["posted"] = r.get("said", False); r["qid"] = ""
        else:
            r = cards.post([q], owner="money.forecast", lane="money")
        if r.get("posted"):
            seen[w["key"]] = time.time()
            if w["kind"] == "price_jump" and r.get("qid"):
                st.setdefault("jump_cards", {})[r["qid"]] = {"merchant_key": w.get("merchant_key", ""), "ts": time.time()}
            posted += 1
            C.log("warning_card", kind=w["kind"], key=w["key"], qid=r["qid"])
        else:
            C.log("warning_refused", kind=w["kind"], reason=str(r.get("reason"))[:160])
    return posted


def harvest(st: dict, answers: list | None = None) -> int:
    """The owner's "Not a bill" on a price card is that merchant's class from now on (money_rules.json ->
    merchant_classes); any other answer just closes the card."""
    pending = st.get("jump_cards") or {}
    if not pending:
        return 0
    if answers is None:
        import categorize
        answers = categorize.card_answers(60)
    by_qid = {str(a.get("qid")): a for a in answers if not a.get("undone")}
    r, n = None, 0
    for qid in list(pending):
        a = by_qid.get(qid)
        if not a:
            if float(pending[qid].get("ts") or 0) < time.time() - 21 * 86400:
                pending.pop(qid)                       # a card nobody answered in three weeks
            continue
        key = str(pending[qid].get("merchant_key") or "")
        if key and str(a.get("option") or "").startswith(NOT_A_BILL):
            r = r or C.rules()
            r.setdefault("merchant_classes", {})[key] = "other"
            n += 1
            C.log("merchant_class", key=key, cls="other", qid=qid)
        pending.pop(qid)
    if r is not None:
        C.save_rules(r)
    st["jump_cards"] = pending
    return n


def tick(now=None, dry: bool = False) -> dict:
    c = C.cfg()
    now = now or C.now_local()
    today = now.date()
    led = C.ledger()
    st = C.state()
    if not dry:
        harvest(st)
    bills = recurring(led, c, today)
    armed = arm_bills(bills, c, today, dry)
    ws = warnings(bills, led, c, today)
    posted = post_warnings(ws, st, c, now, dry)
    if not dry:
        C.save_json(OUT, {"ts": now.strftime("%Y-%m-%d %H:%M"), "bills": bills, "warnings": [{k: v for k, v in w.items() if k != "question"} for w in ws]})
        C.save_state(st)
    out = {"bills": len(bills), "armed": armed, "warnings": len(ws), "posted": posted}
    print("FORECAST " + json.dumps(out))
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("cmd", nargs="?", default="tick", choices=["tick", "list"])
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args()
    if a.cmd == "list":
        for b in recurring(C.ledger()):
            print(f"{b['next_due']}  {C.money(b['amount'], True):>10}  {b['period']:<9} {b['merchant'][:28]:<28} {b['account'][:20]}  x{b['n']}"
                  f"  {b['class']}/{b['class_src']}" + ("  PRICE UP" if b["jump"] else ""))
        return 0
    tick(dry=a.dry_run)
    return 0


if __name__ == "__main__":
    sys.exit(main())

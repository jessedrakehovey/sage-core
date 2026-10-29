"""monthly.py - the plain-sentence money readback on the 1st, to the owners' shared space.

Last month from the ledger: income (HoneyBook called out), spend by bucket with the top few on each
side, business profit and its change against the month before, balances by account kind, and where
the next quarterly stands. Posted once per month (money.json -> monthly.day / after) through the
agent's own poster (assistant/agent/bridge.post: home space once, outbox, thread root) so owner
reads it too (ruling 16). Deterministic; no model.

  python assistant/scripts/money/monthly.py            # tick (the spine runs this hourly)
  python assistant/scripts/money/monthly.py --dry-run  # print the readback for last month, post nothing
  python assistant/scripts/money/monthly.py --month 2026-09 --dry-run
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import common as C  # noqa: E402
import quarterly  # noqa: E402

AGENT = C.ROOT / "agent"


def _sums(led: dict, first: date, last: date, r: dict) -> dict:
    buckets = r.get("buckets") or {}
    out = {"income": 0.0, "income_hb": 0.0, "biz": {}, "personal": {}, "tax": 0.0, "unplaced": 0, "unplaced_amt": 0.0, "n": 0}
    for t in C.txns_sorted(led, first, last):
        if t.get("pending"):
            continue
        out["n"] += 1
        amt = float(t.get("amount") or 0)
        b = buckets.get(t.get("bucket") or "")
        if not b:
            if t.get("bucket") != "transfer":
                out["unplaced"] += 1
                out["unplaced_amt"] += abs(amt)
            continue
        if b.get("kind") == "income":
            out["income"] += amt
            if t.get("bucket") == "income:honeybook":
                out["income_hb"] += amt
        elif b.get("kind") == "expense":
            side = "biz" if b.get("side") == "business" else "personal"
            out[side][t["bucket"]] = out[side].get(t["bucket"], 0.0) + -amt
        elif b.get("kind") == "tax":
            out["tax"] += -amt
    out["biz_total"] = sum(out["biz"].values())
    out["personal_total"] = sum(out["personal"].values())
    out["profit"] = out["income"] - out["biz_total"]
    return out


def _top(d: dict, r: dict, n: int) -> str:
    rows = sorted(d.items(), key=lambda kv: -kv[1])[:n]
    return ", ".join(f"{C.bucket_meta(k, r)['label'].lower()} {C.money(v)}" for k, v in rows if v > 0)


def _delta(cur: float, prev: float) -> str:
    if prev == 0 and cur == 0:
        return "flat"
    if prev == 0:
        return "up from nothing"
    d = cur - prev
    pct = abs(d) / abs(prev) * 100
    return f"{'up' if d > 0 else 'down'} {C.money(abs(d))} ({pct:.0f}%) on the month before"


def readback_text(led: dict, year: int, month: int, c: dict | None = None) -> str:
    c = c or C.cfg()
    r = C.rules()
    first, last = C.month_bounds(year, month)
    cur = _sums(led, first, last, r)
    py, pm = (year - 1, 12) if month == 1 else (year, month - 1)
    pf, pl = C.month_bounds(py, pm)
    prev = _sums(led, pf, pl, r)
    top_n = int(c["monthly"].get("top_n") or 3)
    name = first.strftime("%B")
    if cur["n"] == 0:
        return f"Money, {name}: the bank feed has no transactions for the month yet, so there is nothing to read back."
    lines = [f"Money, {name}:"]
    inc = f"Income {C.money(cur['income'])}"
    if cur["income_hb"]:
        inc += f", {C.money(cur['income_hb'])} of it HoneyBook payments"
    lines.append(inc + ".")
    biz = f"Business spend {C.money(cur['biz_total'])}"
    if cur["biz"]:
        biz += f" (top: {_top(cur['biz'], r, top_n)})"
    lines.append(biz + ".")
    per = f"Personal spend {C.money(cur['personal_total'])}"
    if cur["personal"]:
        per += f" (top: {_top(cur['personal'], r, top_n)})"
    lines.append(per + ".")
    lines.append(f"Business profit {C.money(cur['profit'])}, {_delta(cur['profit'], prev['profit'])}.")
    if cur["tax"]:
        lines.append(f"Tax payments {C.money(cur['tax'])}.")
    bal = {}
    for a in led["accounts"].values():
        if a.get("balance") is not None:
            bal[a["kind"]] = bal.get(a["kind"], 0.0) + float(a["balance"])
    if bal:
        order = ["operating", "tax", "savings", "personal", "card", "unknown"]
        lines.append("Balances: " + ", ".join(f"{k} {C.money(bal[k])}" for k in order if k in bal) + ".")
    est = quarterly.estimate(led, c, last)
    nd = est.get("next_due")
    if nd:
        lines.append(f"Taxes: Schedule C profit {C.money(est['ytd']['profit'])} so far this year; Q{nd['n']} ({nd['date']}) looks like "
                     f"{C.money(nd['total'])} federal + CA at the current pace.")
    if cur["unplaced"]:
        lines.append(f"{cur['unplaced']} transaction{'s' if cur['unplaced'] != 1 else ''} ({C.money(cur['unplaced_amt'])}) are still unplaced; "
                     f"they are on Friday's card.")
    return "\n".join(lines)


def _post(text: str, c: dict) -> bool:
    try:
        sys.path.insert(0, str(AGENT))
        import bridge
        bridge.post(str(c["monthly"].get("to") or "owner1"), text, channel="readback")
        return True
    except Exception as e:  # noqa: BLE001
        C.log("readback_post_error", error=f"{type(e).__name__}: {str(e)[:160]}")
        return False


def tick(now=None, dry: bool = False, force: bool = False, month: str = "") -> dict:
    c = C.cfg()
    m = c["monthly"]
    now = now or C.now_local()
    if month:
        y, mo = int(month[:4]), int(month[5:7])
    else:
        y, mo = (now.year - 1, 12) if now.month == 1 else (now.year, now.month - 1)
    key = f"{y:04d}-{mo:02d}"
    st = C.state()
    if not force and not dry:
        if now.day != int(m.get("day") or 1) or (now.hour, now.minute) < C.hm(m.get("after") or "08:00"):
            return {"skipped": "not the day"}
        if st.get("monthly_last") == key:
            return {"skipped": "done this month"}
    text = readback_text(C.ledger(), y, mo, c)
    if dry:
        print(text)
        return {"month": key, "dry": True}
    ok = _post(text, c)
    st["monthly_last"] = key
    C.save_state(st)
    C.log("monthly_readback", month=key, ok=ok)
    print("MONTHLY " + json.dumps({"month": key, "posted": ok}))
    return {"month": key, "posted": ok}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--force", action="store_true")
    ap.add_argument("--month", default="", help="YYYY-MM (default: last month)")
    a = ap.parse_args()
    tick(dry=a.dry_run, force=a.force, month=a.month)
    return 0


if __name__ == "__main__":
    sys.exit(main())

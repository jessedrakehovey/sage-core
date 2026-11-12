"""categorize.py - every transaction gets a bucket; the ones that don't become ONE weekly card.

Rules live in assistant/money_rules.json (ruling 15): the first `rules` row whose regex matches the
description (and sign / account, when set) wins; then the `learned` rows, which the owner's typed
answers on the weekly card produce. Transfers between the owner's own accounts (same amount, opposite
sign, within transfer_window_days) are paired and bucketed `transfer` without a rule. Relay's bucket
accounts are recognised by name (money.json -> accounts.kinds).

The weekly card ("N transactions I can't place"), posted on categorize.unknowns_day after
unknowns_after, once per ISO week, numbers the rows. The owner types "1 software, 2 personal, 3 skip"
(any bucket key, label, alias or unique prefix) in the card's thread; `harvest` reads the typed
answer, writes one learned rule per row keyed on the merchant handle, re-runs the rules, and records
the lesson in assistant/learnings/money.md. Zero LLM anywhere.

  python assistant/scripts/money/categorize.py            # apply rules + harvest answers + weekly card when due
  python assistant/scripts/money/categorize.py --dry-run  # print what would be posted, write nothing
  python assistant/scripts/money/categorize.py --force    # post the card now (still once per week)
  python assistant/scripts/money/categorize.py unknowns   # list the unplaced rows
  python assistant/scripts/money/categorize.py bulk       # the merchant-level card now (--dry-run to print it)

The bulk card (2026-11-02): when a backlog of unplaced rows repeats the same merchants, ONE card lists the
top merchants (count, total, an example line) instead of single transactions; one typed answer per merchant
becomes the learned rule that places every row of that merchant, past and future. Posted by the tick when at
least categorize.bulk_min_merchants merchants have bulk_min_count or more unplaced rows, latched
bulk_latch_days; answered through the same harvest as the weekly card.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import time
from datetime import timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import common as C  # noqa: E402

HEADER = "Money: unplaced transactions"


# ------------------------------------------------------------------ rules
def _compile(rows: list) -> list:
    out = []
    for r in rows:
        try:
            out.append((r, re.compile(str(r.get("match") or ""), re.I),
                        re.compile(str(r.get("account") or ""), re.I) if r.get("account") else None))
        except re.error:
            continue
    return out


def _fits(rule, rx, arx, t: dict, acct: dict) -> bool:
    desc = t.get("description") or ""
    # learned rules are keyed on the merchant handle (noise words, store numbers and dates stripped),
    # so the same merchant matches however the bank pads the line this month
    if not rx.search(desc) and not rx.search(t.get("payee") or "") and not rx.search(C.merchant_key(desc)):
        return False
    sign = str(rule.get("sign") or "")
    if sign == "in" and float(t.get("amount") or 0) <= 0:
        return False
    if sign == "out" and float(t.get("amount") or 0) >= 0:
        return False
    if arx and not arx.search(acct.get("name") or ""):
        return False
    return True


def pair_transfers(led: dict, window_days: int) -> int:
    """Same amount, opposite sign, different own accounts, within the window -> both `transfer`."""
    byamt = {}
    for t in led["transactions"].values():
        if t.get("bucket") and t.get("bucket_src") != "transfer":
            continue
        byamt.setdefault(round(abs(float(t.get("amount") or 0)), 2), []).append(t)
    n = 0
    for amt, rows in byamt.items():
        if amt <= 0 or len(rows) < 2:
            continue
        outs = [t for t in rows if float(t["amount"]) < 0]
        ins = [t for t in rows if float(t["amount"]) > 0]
        used = set()
        for o in outs:
            od = C.pdate(o.get("posted"))
            for i in ins:
                if i["id"] in used or i["account_id"] == o["account_id"]:
                    continue
                idd = C.pdate(i.get("posted"))
                if od and idd and abs((idd - od).days) <= window_days:
                    for t in (o, i):
                        if t.get("bucket_src") != "transfer":
                            t.update({"bucket": "transfer", "bucket_src": "transfer", "rule_id": ""})
                            n += 1
                    used.add(i["id"])
                    break
    return n


def apply(led: dict, r: dict | None = None, c: dict | None = None, only_unplaced: bool = True) -> dict:
    """Run the rules over the ledger in place. Learned rules are checked before the shipped ones
    (the owner's word beats a generic pattern)."""
    r = r or C.rules()
    c = c or C.cfg()
    learned = _compile(r.get("learned") or [])
    shipped = _compile(r.get("rules") or [])
    placed = 0
    for t in led["transactions"].values():
        if only_unplaced and t.get("bucket") and t.get("bucket_src") in ("learned", "manual"):
            continue
        if only_unplaced and t.get("bucket") and t.get("bucket_src") == "transfer":
            continue
        acct = led["accounts"].get(t.get("account_id")) or {}
        hit = None
        for rule, rx, arx in learned + shipped:
            if _fits(rule, rx, arx, t, acct):
                hit = rule
                break
        if hit and hit.get("bucket") in (r.get("buckets") or {}):
            if t.get("bucket") != hit["bucket"] or t.get("rule_id") != hit.get("id"):
                placed += 1
            t.update({"bucket": hit["bucket"], "bucket_src": "learned" if hit.get("learned") else "rule", "rule_id": str(hit.get("id") or "")})
        elif not t.get("bucket") and C.is_transfer(t.get("description"), c):
            # a card payment, a round-up, an online transfer: own money moving, never spending (2026-11-01)
            t.update({"bucket": "transfer", "bucket_src": "transfer", "rule_id": ""})
            placed += 1
        elif not t.get("bucket"):
            t.update({"bucket": "", "bucket_src": "none", "rule_id": ""})
    placed += pair_transfers(led, int(c["categorize"].get("transfer_window_days") or 3))
    return {"placed": placed, "unplaced": len(unplaced(led, c))}


def unplaced(led: dict, c: dict | None = None) -> list:
    c = c or C.cfg()
    floor = float(c["categorize"].get("min_abs_amount") or 0)
    rows = [t for t in led["transactions"].values() if not t.get("bucket") and abs(float(t.get("amount") or 0)) >= floor
            and not t.get("pending")]
    rows.sort(key=lambda t: (-abs(float(t.get("amount") or 0)), t.get("posted", "")))
    return rows


# ------------------------------------------------------------------ the weekly card
def _row(i: int, t: dict, led: dict) -> str:
    acct = (led["accounts"].get(t.get("account_id")) or {}).get("name") or "?"
    d = C.pdate(t.get("posted"))
    when = d.strftime("%b %d") if d else "?"
    return f"{i}. {when}  {C.money(float(t['amount']), cents=True):>10}  {str(t.get('description') or '')[:40]}  ({acct[:22]})"


def card(led: dict, c: dict | None = None) -> tuple:
    """(question dict, rows) or (None, [])."""
    c = c or C.cfg()
    rows = unplaced(led, c)[:int(c["categorize"].get("unknowns_max_rows") or 12)]
    if not rows:
        return None, []
    total = sum(abs(float(t["amount"])) for t in rows)
    n = len(rows)
    keys = sorted(k for k in C.rules()["buckets"] if not k.startswith("_"))
    q = {"header": HEADER, "lane": "money",
         "question": f"{n} transaction{'s' if n != 1 else ''} I can't place ({C.money(total)} in all). Reply in this thread with the "
                     f"number and a bucket, like \"1 software, 2 personal, 3 skip\"; each answer becomes a rule so the same "
                     f"merchant never asks again.\n" + "\n".join(_row(i, t, led) for i, t in enumerate(rows, 1)),
         "details": "Buckets: " + ", ".join(keys),
         "advice": f"💡 Type the placements: {n} row{'s' if n != 1 else ''} to place, {C.money(total)} of this month's "
                   f"Schedule C and personal totals wait on them; nothing here moves money.",
         "options": [{"label": "Placed in this thread (Recommended)", "description": "I typed the numbers and buckets below"},
                     {"label": "Skip this week", "description": "the same rows come back next Friday"}]}
    return q, [{"txn_id": t["id"], "description": t.get("description", ""), "amount": t["amount"]} for t in rows]


BULK_HEADER = "Money: place the repeat merchants"


def bulk_rows(led: dict, c: dict | None = None, r: dict | None = None) -> list:
    """Unplaced merchants with at least bulk_min_count rows, most rows first:
    [{merchant, n, total, example (a txn), accounts}]."""
    c = c or C.cfg()
    k = c["categorize"]
    groups = {}
    for t in led["transactions"].values():
        if t.get("bucket") or t.get("pending") or C.is_transfer(t.get("description") or "", c):
            continue
        key = C.merchant_key(t.get("description") or "")
        if not key:
            continue
        g = groups.setdefault(key, {"merchant": key, "n": 0, "total": 0.0, "example": t, "accounts": set()})
        g["n"] += 1
        g["total"] += float(t.get("amount") or 0)
        g["accounts"].add(str((led["accounts"].get(t.get("account_id")) or {}).get("name") or "")[:18])
    rows = [g for g in groups.values() if g["n"] >= int(k.get("bulk_min_count") or 3)]
    rows.sort(key=lambda g: (-g["n"], g["merchant"]))
    return rows[:int(k.get("bulk_max_rows") or 30)]


def bulk_card(led: dict, c: dict | None = None, r: dict | None = None) -> tuple:
    """(question, rows) for the merchant-level card, or (None, []) when the backlog is small."""
    c = c or C.cfg()
    r = r or C.rules()
    rows = bulk_rows(led, c, r)
    if len(rows) < int(c["categorize"].get("bulk_min_merchants") or 8):
        return None, []
    n_txn = sum(g["n"] for g in rows)
    keys = sorted(k for k in r["buckets"] if not k.startswith("_"))
    lines = [f"{i}. {g['example'].get('description', '')[:32]}  {g['n']} rows  {C.money(g['total'])}  ({', '.join(sorted(g['accounts']))[:40]})"
             for i, g in enumerate(rows, 1)]
    q = {"header": BULK_HEADER, "lane": "money",
         "question": f"{len(rows)} merchants cover {n_txn} of the transactions nobody has placed. One word each places all of them, now and "
                     f"from here on: reply like \"1 groceries, 2 shopping, 3 skip\" (skip = ask again later; ignore = never count it). The numbered list is in the details.",
         "details": "\n".join(lines) + "\n\nBuckets: " + ", ".join(keys),
         "advice": f"💡 Type the placements: every envelope target and the Schedule C totals wait on these {n_txn} rows; nothing here moves money.",
         "options": [{"label": "Placed in this thread (Recommended)", "description": "I typed the numbers and buckets below"},
                     {"label": "Skip for now", "description": "the same merchants come back in two weeks"}]}
    return q, [{"txn_id": g["example"]["id"], "description": g["example"].get("description", ""), "amount": g["example"].get("amount"), "merchant": g["merchant"], "n": g["n"]} for g in rows]


def post_bulk(led: dict, st: dict, c: dict, now, dry: bool, force: bool) -> str:
    """The bulk card when due (latched categorize.bulk_latch_days). Returns the qid or ''."""
    k = c["categorize"]
    if not k.get("bulk_card", True) and not force:
        return ""
    if not force and time.time() - float(st.get("bulk_ts") or 0) < int(k.get("bulk_latch_days") or 14) * 86400:
        return ""
    q, rows = bulk_card(led, c)
    if not q:
        return ""
    if dry:
        print("WOULD POST CARD " + json.dumps(q, ensure_ascii=False)[:2500])
        return "dry"
    import cards
    res = cards.post([q], owner="money.categorize", lane="money")
    if res.get("posted"):
        st.setdefault("unknowns_cards", {})[res["qid"]] = {"ts": time.time(), "rows": rows, "bulk": True}
        st["bulk_ts"] = time.time()
        C.log("bulk_card", qid=res["qid"], rows=len(rows), txns=sum(x["n"] for x in rows))
        return res["qid"]
    C.log("bulk_refused", reason=str(res.get("reason"))[:200])
    return ""


# ------------------------------------------------------------------ learning from the typed answer
def resolve_bucket(word: str, r: dict | None = None) -> str:
    r = r or C.rules()
    w = re.sub(r"\s+", " ", str(word or "").strip().lower().strip(".,;:"))
    if not w:
        return ""
    buckets = {k: v for k, v in (r.get("buckets") or {}).items() if not k.startswith("_")}
    aliases = {k.lower(): v for k, v in (r.get("aliases") or {}).items() if not k.startswith("_")}
    if w in buckets:
        return w
    if w in aliases and aliases[w] in buckets:
        return aliases[w]
    for k, v in buckets.items():
        if str(v.get("label") or "").lower() == w:
            return k
    cands = {k for k in buckets if k.startswith(w) or k.split(":")[-1].startswith(w)}
    cands |= {k for k, v in buckets.items() if str(v.get("label") or "").lower().startswith(w)}
    cands |= {v for k, v in aliases.items() if k.startswith(w) and v in buckets}
    return cands.pop() if len(cands) == 1 else ""


_PAIR = re.compile(r"(?:^|[,;\n]|\band\b)\s*#?(\d{1,2})\s*[:=.)-]?\s*([a-z][a-z :&'/-]*?)\s*(?=(?:[,;\n]|\band\b\s*#?\d|$))", re.I)


def parse_answer(text: str, nrows: int, r: dict | None = None) -> tuple:
    """'1 software, 2 personal:dining, 3 skip' -> ({1: 'biz:software', ...}, [unrecognised words])."""
    r = r or C.rules()
    out, bad = {}, []
    for m in _PAIR.finditer(str(text or "")):
        i, word = int(m.group(1)), m.group(2).strip()
        if not (1 <= i <= nrows):
            continue
        b = resolve_bucket(word, r)
        if b:
            out[i] = b
        else:
            bad.append(f"{i} {word}")
    return out, bad


def learn(rows: list, placements: dict, qid: str, r: dict | None = None, c: dict | None = None) -> list:
    """One learned rule per placed row, keyed on the merchant handle. Returns the new rules."""
    r = r or C.rules()
    c = c or C.cfg()
    have = {str(x.get("match")): x for x in r.get("learned") or []}
    added = []
    for i, bucket in placements.items():
        if i - 1 >= len(rows):
            continue
        row = rows[i - 1]
        key = C.merchant_key(row.get("description") or "")
        if not key:
            continue
        match = r"\b" + re.escape(key).replace(r"\ ", r"\s+")
        if match in have:
            have[match]["bucket"] = bucket
            continue
        rule = {"id": "L" + str(int(time.time() * 1000))[-9:] + f"{i:02d}", "match": match, "bucket": bucket, "learned": True,
                "from": qid, "example": str(row.get("description") or "")[:60], "ts": time.strftime("%Y-%m-%d")}
        r.setdefault("learned", []).append(rule)
        have[match] = rule
        added.append(rule)
        _lesson(f"'{key}' is {C.bucket_meta(bucket, r)['label']} ({bucket})", qid, c)
    C.save_rules(r)
    return added


def _lesson(text: str, qid: str, c: dict) -> None:
    try:
        sys.path.insert(0, str(C.VAULT / "scripts" / "lib"))
        import learnings
        learnings.record_lesson(C.VAULT / str(c["categorize"].get("learn_to") or "assistant/learnings/money.md"),
                                text, context=f"card {qid}", title="Money lane learnings")
    except Exception as e:  # noqa: BLE001
        C.log("lesson_error", error=f"{type(e).__name__}: {str(e)[:120]}")


def card_answers(since_days: float = 60) -> list:
    """The owners' taps and typed text on every card (cards.py's answers.jsonl, same store the
    tests redirect with ASSISTANT_CARDS)."""
    import os
    root = Path(os.environ.get("ASSISTANT_CARDS") or (C.DATA / "cards"))
    out, floor = [], time.time() - since_days * 86400
    try:
        with open(root / "answers.jsonl", encoding="utf-8-sig") as f:
            for ln in f:
                try:
                    a = json.loads(ln)
                except ValueError:
                    continue
                if float(a.get("epoch") or 0) >= floor:
                    out.append(a)
    except OSError:
        pass
    return out


def harvest(led: dict, st: dict, answers: list | None = None) -> int:
    """Read the owner's answers on our own unknowns cards (answers.jsonl), learn, re-apply."""
    if answers is None:
        answers = card_answers(60)
    pending = st.get("unknowns_cards") or {}
    if not pending:
        return 0
    by_qid = {}
    for a in answers:
        by_qid.setdefault(str(a.get("qid")), a)
    learned = 0
    r = C.rules()
    for qid in list(pending):
        a = by_qid.get(qid)
        if not a or a.get("undone"):
            if pending[qid].get("ts", 0) < time.time() - 21 * 86400:
                pending.pop(qid)                       # a card nobody answered in three weeks
            continue
        rows = pending[qid].get("rows") or []
        placements, bad = parse_answer(str(a.get("text") or ""), len(rows), r)
        if placements:
            added = learn(rows, placements, qid, r)
            r = C.rules()
            for i, bucket in placements.items():
                if i - 1 < len(rows):
                    t = led["transactions"].get(rows[i - 1]["txn_id"])
                    if t:
                        t.update({"bucket": bucket, "bucket_src": "manual", "rule_id": ""})
            learned += len(added)
            if added or pending[qid].get("bulk"):
                apply(led, C.rules())                   # the new rules place every row of those merchants
            C.log("unknowns_learned", qid=qid, placed=len(placements), rules=len(added), bad=bad)
        elif bad:
            C.log("unknowns_unparsed", qid=qid, bad=bad)
        pending.pop(qid)
    st["unknowns_cards"] = pending
    return learned


# ------------------------------------------------------------------ tick
def tick(now=None, dry: bool = False, force: bool = False) -> dict:
    c = C.cfg()
    k = c["categorize"]
    now = now or C.now_local()
    led = C.ledger()
    st = C.state()
    learned = harvest(led, st)
    r = apply(led, c=c)
    out = {"placed": r["placed"], "unplaced": r["unplaced"], "learned": learned, "qid": ""}
    wk = C.iso_week(now)
    due = force or (C.DOW[now.weekday()] == str(k.get("unknowns_day") or "fri").lower()[:3]
                    and (now.hour, now.minute) >= C.hm(k.get("unknowns_after") or "15:00"))
    if not k.get("weekly_card", True) and not force:
        due = False                                     # the money talk asks instead (brief.py)
    if due and st.get("unknowns_week") != wk:
        q, rows = card(led, c)
        if q:
            if dry:
                print("WOULD POST CARD " + json.dumps(q, ensure_ascii=False)[:1500])
            else:
                import cards
                res = cards.post([q], owner="money.categorize", lane="money")
                if res.get("posted"):
                    out["qid"] = res["qid"]
                    st.setdefault("unknowns_cards", {})[res["qid"]] = {"ts": time.time(), "rows": rows}
                    st["unknowns_week"] = wk
                    C.log("unknowns_card", qid=res["qid"], rows=len(rows))
                else:
                    C.log("unknowns_refused", reason=str(res.get("reason"))[:200])
        elif not dry:
            st["unknowns_week"] = wk
    out["bulk_qid"] = post_bulk(led, st, c, now, dry, False)
    if not dry:
        C.save_ledger(led)
        C.save_state(st)
    print("CATEGORIZE " + json.dumps(out))
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("cmd", nargs="?", default="tick", choices=["tick", "unknowns", "bulk"])
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--force", action="store_true")
    a = ap.parse_args()
    if a.cmd == "unknowns":
        led = C.ledger()
        for i, t in enumerate(unplaced(led), 1):
            print(_row(i, t, led))
        return 0
    if a.cmd == "bulk":
        led, st = C.ledger(), C.state()
        apply(led)
        qid = post_bulk(led, st, C.cfg(), C.now_local(), a.dry_run, True)
        if not a.dry_run:
            C.save_ledger(led)
            C.save_state(st)
        print("BULK " + json.dumps({"qid": qid}))
        return 0
    tick(dry=a.dry_run, force=a.force)
    return 0


if __name__ == "__main__":
    sys.exit(main())

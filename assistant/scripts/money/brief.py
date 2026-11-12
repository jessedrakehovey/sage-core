"""brief.py - the money lane as a conversation: what the agent sees, the questions only an owner can answer,
and the deeds their answers become. Zero LLM here; the agent (assistant/agent) reads `block()` in its context,
asks ONE question at a time in its own words, and calls the `money` tool with the owner's answer, which lands
in `apply()`.

the owner, 2026-11-03: "that process is too complex, it needs to be simple, like talking and clarifying with my
finance advisor." So no numbered cards, no reply grammar: the brief ranks the open questions (an unplaced
merchant with its count and monthly average, an overspent envelope and what could cover it, an envelope with no
target, accounts waiting for a first baseline, a new month's envelopes to fund) and the agent works through
them in chat. Every deed is ledgered (core.log_change) and undoable (`undo`).

  python assistant/scripts/money/brief.py            # the context block
  python assistant/scripts/money/brief.py questions  # the ranked questions as JSON
"""
from __future__ import annotations

import json
import re
import sys
import time
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
# another lane's common.py (scripts/tracking/) may already sit in sys.modules in the agent's process: load ours, then
# put theirs back, so neither lane gets the other's module
_other = sys.modules.get("common")
if _other is not None and Path(getattr(_other, "__file__", "") or ".").resolve().parent != Path(__file__).resolve().parent:
    sys.modules.pop("common")
else:
    _other = None
import common as C  # noqa: E402
import budget as B  # noqa: E402
import categorize  # noqa: E402
import reconcile as R  # noqa: E402
import forecast  # noqa: E402
if _other is not None:
    sys.modules["common"] = _other

OPS = ("place", "target", "move", "cover", "assign", "reconciled", "fund", "history")


# ------------------------------------------------------------------ the open questions
def merchants(led: dict, c: dict | None = None, months: int = 12, n: int = 8) -> list:
    """Unplaced merchants, most charges first, with what an advisor would say about each."""
    c = c or C.cfg()
    rows = categorize.bulk_rows(led, c)[:n]
    today = C.now_local().date()
    out = []
    for g in rows:
        ex = g["example"]
        dates = sorted(C.pdate(t.get("posted")) for t in led["transactions"].values()
                       if not t.get("bucket") and C.merchant_key(t.get("description") or "") == g["merchant"] and C.pdate(t.get("posted")))
        span = max(1, min(months, ((today - dates[0]).days // 30) + 1)) if dates else months
        out.append({"kind": "merchant", "key": g["merchant"], "name": str(ex.get("description") or g["merchant"])[:40],
                    "n": g["n"], "total": round(g["total"], 2), "per_month": round(g["total"] / span, 2),
                    "months": span, "accounts": sorted(g["accounts"]), "example_id": ex["id"],
                    "sign": "in" if g["total"] > 0 else "out"})
    return out


def questions(led: dict | None = None, b: dict | None = None, c: dict | None = None, r: dict | None = None, n: int = 6) -> list:
    """Ranked: a new month to fund, overspent envelopes, the biggest unplaced merchants, envelopes without a
    target, accounts awaiting a baseline. Each carries `ask`, the plain fact for the agent to phrase."""
    led = led if led is not None else C.ledger()
    c, r = c or C.cfg(), r or C.rules()
    b = b if b is not None else B.load()
    today = C.now_local().date()
    mdl = B.model(led, b, c, r, today)
    out = []
    for side, s in mdl["sides"].items():
        assigned = (b["months"].get(mdl["month"]) or {}).get("assigned") or {}
        if s["envelopes"] and (s["spending"] or s["cards"]) and not any(assigned.values()) and s["ready"] > 0 \
                and b.get("fund_asked", {}).get(side) != mdl["month"]:
            p = B.plan(side, mdl)
            if p["fund"]:
                out.append({"kind": "fund", "side": side, "month": mdl["month"], "ready": s["ready"], "total": p["total"],
                            "plan": p["fund"], "ask": f"{side}: {C.money(s['ready'])} is ready to assign for {today.strftime('%B')}; refilling every "
                                                     f"envelope to its target takes {C.money(p['total'])} ({', '.join(B.label(e, r) + ' ' + C.money(a) for e, a in list(p['fund'].items())[:5])}"
                                                     + (", ..." if len(p["fund"]) > 5 else "") + "). Fund it?"})
    for side, s in mdl["sides"].items():
        for e in s["overspent"]:
            src = R._cover_from(e, s["envelopes"])
            out.append({"kind": "overspent", "side": side, "envelope": e["key"], "short": round(-e["available"], 2), "cover_from": src,
                        "ask": f"{e['label']} ({side}) is over by {C.money(-e['available'])} this month"
                               + (f"; {src} has the slack to cover it." if src else f"; nothing on the {side} side has slack, ready to assign is {C.money(s['ready'])}.")})
    for m in merchants(led, c):
        out.append({**m, "ask": f"{m['name']}: {m['n']} {'deposits' if m['sign'] == 'in' else 'charges'}, {C.money(abs(m['total']))} over {m['months']} month{'s' if m['months'] != 1 else ''}"
                                f" (about {C.money(abs(m['per_month']))} a month) on {', '.join(m['accounts'])[:60]}. Which envelope is it?"})
    for side, s in mdl["sides"].items():
        for e in s["envelopes"]:
            if not e["target"] and e["activity"] < 0 and e["key"] not in (b.get("targets") or {}):
                out.append({"kind": "target", "side": side, "envelope": e["key"], "spent": round(-e["activity"], 2),
                            "ask": f"{e['label']} ({side}) has no monthly target yet; {C.money(-e['activity'])} spent so far this month."})
    chk = R.check(led, b, c, today)
    fresh = [x for x in chk["rows"] if x["state"] == "no baseline"]
    off = [x for x in chk["rows"] if x["state"] == "off"]
    if fresh:
        out.append({"kind": "baseline", "accounts": [x["name"] for x in fresh],
                    "ask": f"{len(fresh)} accounts ({', '.join(str(x['name']) for x in fresh[:4])}{', ...' if len(fresh) > 4 else ''}) have never been reconciled; "
                           "when the owner says the bank's balances are right, that is the first baseline."})
    for x in off:
        out.append({"kind": "off", "account": x["name"], "diff": x["diff"], "suspects": x["suspects"],
                    "ask": f"{x['name']}: {x['note']}." + (" Likely: " + "; ".join(x["suspects"]) + "." if x["suspects"] else "")})
    # the order an advisor would take: fund a new month, the merchants that explain the most money, an overspend that
    # CAN be covered, one missing target, then the baseline and anything off at the bank; never six of one kind
    caps = {"fund": 2, "merchant": 3, "overspent": 2, "target": 1, "baseline": 1, "off": 2}
    order = ("fund", "merchant", "overspent", "target", "baseline", "off")
    ranked, seen = [], {}
    for kind in order:
        rows = [q for q in out if q["kind"] == kind]
        if kind == "overspent":
            rows.sort(key=lambda q: (not q.get("cover_from"), -q["short"]))
        for q in rows[:caps[kind]]:
            ranked.append(q)
    return ranked[:n]


# ------------------------------------------------------------------ the context block
def block(n: int = 6) -> str:
    led = C.ledger()
    if not led["accounts"]:
        return ""
    c, r, b = C.cfg(), C.rules(), B.load()
    today = C.now_local().date()
    mdl = B.model(led, b, c, r, today)
    chk = R.check(led, b, c, today)
    lines = [f"MONEY (the envelope budget on the bank feeds, as of {mdl['generated']}; every figure below is from the ledger; nothing here moves money at a bank):"]
    for side, s in mdl["sides"].items():
        if not (s["spending"] or s["cards"] or s["vaults"]):
            continue
        over = ", ".join(f"{e['label']} {C.money(-e['available'])}" for e in s["overspent"][:5])
        lines.append(f"- {side}: ready to assign {C.money(s['ready'])} (cash {C.money(s['cash'])}, in envelopes {C.money(s['held'])}, cards owe {C.money(s['card_debt'])})"
                     + (f"; overspent: {over}" if over else "; nothing overspent")
                     + (f"; {C.money(s['unplaced'])} spent this month has no envelope yet" if s["unplaced"] else ""))
        envs = ", ".join(f"{e['label']} {C.money(e['available'])}" + (f"/{C.money(e['target'])}" if e["target"] else "") for e in s["envelopes"] if e["available"] or e["target"] or e["activity"])
        if envs:
            lines.append(f"  envelopes ({side}, available/target): {envs}")
        if s["vaults"]:
            lines.append(f"  vaults ({side}): " + ", ".join(f"{v['name']} {C.money(v['balance'])}" for v in s["vaults"]))
    rows = chk["rows"]
    ok = sum(1 for x in rows if x["state"] == "ok")
    parts = [f"{ok} of {len(rows)} accounts match the bank"]
    for st, word in (("off", "off"), ("stale", "stale feed"), ("no baseline", "never reconciled")):
        k = [x for x in rows if x["state"] == st]
        if k:
            parts.append(f"{len(k)} {word} ({', '.join(str(x['name']) for x in k[:4])})")
    unpl = len(categorize.unplaced(led, c))
    lines.append("- accounts: " + "; ".join(parts) + (f"; age of money {chk['age_of_money']} days" if chk["age_of_money"] is not None else "")
                 + f"; {unpl} charges still have no envelope")
    lines.append("- envelope names: " + "; ".join(f"{side}: " + ", ".join(e["label"] for e in s["envelopes"]) for side, s in mdl["sides"].items() if s["envelopes"]))
    qs = questions(led, b, c, r, n)
    if qs:
        lines.append("MONEY QUESTIONS (facts only an owner has; ask ONE per reply, in your own words with the figures that matter, the way their "
                     "financial advisor would - never a numbered list, a card, or a reply format; their answer goes to the `money` tool the same turn):")
        for i, q in enumerate(qs, 1):
            lines.append(f"{i}. [{q['kind']}] {q['ask']}")
    return "\n".join(lines)


# ------------------------------------------------------------------ what a card owed then, and what changed since
def history(account: str = "", days: int = 30, led: dict | None = None, c: dict | None = None, r: dict | None = None,
            today: date | None = None, n: int = 12) -> dict:
    """Read only (2026-11-08, "it was $1,300 a month ago and now it's over 3,000, where did the other 1,000 come
    from"): for each credit card (or the one named), what it owed `days` ago, worked back from today's bank balance
    through the ledger, and every charge and payment since, biggest first, with the envelope each sits in.
    -> {"ok", "line"}; nothing changes."""
    led = led if led is not None else C.ledger()
    c, r = c or C.cfg(), r or C.rules()
    today = today or C.now_local().date()
    days = max(1, min(int(days or 30), 400))
    since = today - timedelta(days=days)
    cards = [a for a in B.accounts(led, c) if a["role"] == "card"]
    w = str(account or "").strip().lower()
    if w:
        hit = [a for a in cards if w in str(a.get("name") or "").lower() or w == str(a.get("id") or "").lower()]
        cards = hit or cards
    if not cards:
        return {"ok": False, "line": "no credit card is on the bank feeds yet."}
    out = []
    for a in cards:
        rows = [t for t in led["transactions"].values() if t.get("account_id") == a["id"] and (C.pdate(t.get("posted")) or since) > since]
        bal = float(a.get("balance") or 0)
        then = bal - sum(float(t.get("amount") or 0) for t in rows)
        charges = sorted((t for t in rows if float(t.get("amount") or 0) < 0), key=lambda t: float(t.get("amount") or 0))
        paid = sum(float(t.get("amount") or 0) for t in rows if float(t.get("amount") or 0) > 0)
        biz = -sum(float(t.get("amount") or 0) for t in charges if str(t.get("bucket") or "").startswith("biz:"))
        head = (f"{a.get('name') or a['id']}: owed {C.money(-then)} on {since.isoformat()}, "
                f"owes {C.money(-bal)} now (bank, {str(a.get('balance_date') or '')[:10] or 'latest'}); "
                f"since then {len(charges)} charges {C.money(-sum(float(t.get('amount') or 0) for t in charges))}"
                f" ({C.money(biz)} of it in business envelopes), payments and credits {C.money(paid)}.")
        lines = [head]
        for t in charges[:n]:
            env = t.get("bucket") or ""
            lines.append(f"  {str(t.get('posted') or '')[:10]} {str(t.get('description') or '')[:40]} {C.money(-float(t.get('amount') or 0), True)}"
                         f" ({B.label(env, r) if env else 'no envelope yet'}){' pending' if t.get('pending') else ''}")
        if len(charges) > n:
            lines.append(f"  ... {len(charges) - n} smaller charges, {C.money(-sum(float(t.get('amount') or 0) for t in charges[n:]))} together")
        out.append("\n".join(lines))
    return {"ok": True, "line": "\n".join(out)}


def bills(days: int = 0, led: dict | None = None, c: dict | None = None, today: date | None = None) -> dict:
    """Read only (2026-11-09, "are you sure the lens rentals are due tomorrow? how do you know we don't have enough
    money to pay our expenses? give me a breakdown"): the bills forecast.py predicts in the next `days` (default
    low_balance_days), each with how it is known (its rhythm, the last charge, how many), and per account the
    balance against the bills that land on it. -> {"ok", "line"}; nothing changes."""
    led = led if led is not None else C.ledger()
    c = c or C.cfg()
    today = today or C.now_local().date()
    days = max(1, min(int(days or c["forecast"].get("low_balance_days") or 7), 60))
    due = [b for b in forecast.recurring(led, c, today) if 0 <= b["days_to_due"] <= days]
    if not due:
        return {"ok": True, "line": f"no recurring bill is predicted in the next {days} days from the bank history."}
    roles = {a["id"]: a for a in B.accounts(led, c)}
    lines = [f"bills predicted in the next {days} days (a bill = the same merchant on the same account at a steady rhythm, "
             f"next due = last charge + its period; a guess from the bank history, not a statement):"]
    by_acct = {}
    for b in due:
        by_acct.setdefault(b["account_id"], []).append(b)
        lines.append(f"  {b['next_due']} {b['merchant']} about {C.money(b['amount'], True)} from {b['account']}: {b['period']}, "
                     f"{b['n']} charges so far, last {C.money(b['last_amount'], True)} on {b['last_posted']}")
    for aid, rows in by_acct.items():
        a = roles.get(aid) or {}
        need = sum(b["amount"] for b in rows)
        if a.get("role") == "card" or a.get("balance") is None:
            lines.append(f"{a.get('name') or rows[0]['account']}: {C.money(need)} of these go on this card (no cash needed today).")
            continue
        bal = float(a["balance"])
        lines.append(f"{a.get('name') or aid}: balance {C.money(bal)} (bank, {str(a.get('balance_date') or '')[:10] or 'latest'}), "
                     f"bills {C.money(need)}, " + (f"short {C.money(need - bal)}." if need > bal else f"{C.money(bal - need)} left after them."))
    return {"ok": True, "line": "\n".join(lines)}


# ------------------------------------------------------------------ resolving the owner's words
def resolve_envelope(word: str, r: dict | None = None) -> str:
    r = r or C.rules()
    w = str(word or "").strip().lower()
    if w in ("ready", "ready to assign", "rta"):
        return "ready"
    return categorize.resolve_bucket(w, r)


def resolve_merchant(word: str, led: dict | None = None, c: dict | None = None) -> dict | None:
    """The unplaced merchant the owner means: by key, by the name shown, or a unique substring."""
    led = led if led is not None else C.ledger()
    w = C.merchant_key(word) or str(word or "").strip().lower()
    if not w:
        return None
    rows = categorize.bulk_rows(led, c or C.cfg())
    for g in rows:
        if g["merchant"] == w or str(g["example"].get("description") or "").strip().lower() == str(word or "").strip().lower():
            return g
    hits = [g for g in rows if w in g["merchant"] or g["merchant"] in w]
    if len(hits) == 1:
        return hits[0]
    # any unplaced row at all with that merchant key (fewer than bulk_min_count charges)
    for t in led["transactions"].values():
        if not t.get("bucket") and C.merchant_key(t.get("description") or "") == w:
            return {"merchant": w, "n": 1, "total": float(t.get("amount") or 0), "example": t, "accounts": set()}
    return None


# ------------------------------------------------------------------ the deeds
def apply(op: str, payload: dict, by: str = "") -> dict:
    """-> {"ok", "line", "undo"}; every change ledgered by the caller with `undo`."""
    op = str(op or "").lower()
    c, r = C.cfg(), C.rules()
    led = C.ledger()
    b = B.load()
    month = B.month_key(C.now_local().date())
    why = f"chat ({by})" if by else "chat"
    if op == "history":
        return history(str(payload.get("account") or ""), int(_amt(payload.get("days")) or 30), led, c, r)
    if op == "place":
        g = resolve_merchant(str(payload.get("merchant") or ""), led, c)
        env = resolve_envelope(str(payload.get("envelope") or ""), r)
        if not g or not env or env == "ready":
            return {"ok": False, "line": "I couldn't match that merchant or envelope."}
        ex = g["example"]
        before = {t["id"]: (t.get("bucket"), t.get("bucket_src"), t.get("rule_id")) for t in led["transactions"].values()
                  if not t.get("bucket") and C.merchant_key(t.get("description") or "") == g["merchant"]}
        added = categorize.learn([{"description": ex.get("description", ""), "txn_id": ex["id"]}], {1: env}, f"chat:{by or 'owner'}", C.rules(), c)
        rule_id = added[0]["id"] if added else next((x["id"] for x in C.rules().get("learned") or [] if x.get("bucket") == env and re.search(x["match"], ex.get("description", ""), re.I)), "")
        for tid in before:
            led["transactions"][tid].update({"bucket": env, "bucket_src": "learned", "rule_id": rule_id})
        categorize.apply(led, C.rules(), c)
        C.save_ledger(led)
        C.log("chat_place", merchant=g["merchant"], bucket=env, rows=len(before), by=by)
        return {"ok": True, "line": f"{str(ex.get('description') or g['merchant'])[:40]} is {B.label(env, r)} from now on ({len(before)} charges placed)",
                "undo": {"op": "money_undo", "what": "place", "rule_id": rule_id, "rows": list(before)}}
    if op in ("target",):
        env = resolve_envelope(str(payload.get("envelope") or ""), r)
        amt = _amt(payload.get("amount"))
        if not env or env == "ready" or amt is None:
            return {"ok": False, "line": "I need the envelope and the monthly amount."}
        prev = (b.get("targets") or {}).get(env)
        b.setdefault("targets", {})[env] = round(amt, 2)
        B.save(b)
        return {"ok": True, "line": f"{B.label(env, r)} target is {C.money(amt)} a month", "undo": {"op": "money_undo", "what": "target", "envelope": env, "before": prev}}
    if op in ("move", "assign", "cover"):
        dst = resolve_envelope(str(payload.get("to") or payload.get("envelope") or ""), r)
        src = resolve_envelope(str(payload.get("from") or "ready"), r) or "ready"
        if not dst or dst == "ready":
            return {"ok": False, "line": "I need the envelope the money goes to."}
        if op == "cover":
            mdl = B.model(led, b, c, r)
            row = next((x for s in mdl["sides"].values() for x in s["envelopes"] if x["key"] == dst), None)
            amt = -row["available"] if row and row["available"] < 0 else 0.0
            if amt <= 0:
                return {"ok": False, "line": f"{B.label(dst, r)} is not overspent."}
        else:
            amt = _amt(payload.get("amount"))
            if amt is None:
                return {"ok": False, "line": "I need the amount."}
        B.move(b, amt, src, dst, month, why)
        B.save(b)
        src_name = "ready to assign" if src == "ready" else B.label(src, r)
        return {"ok": True, "line": f"{C.money(amt)} moved from {src_name} to {B.label(dst, r)}",
                "undo": {"op": "money_undo", "what": "move", "amount": round(amt, 2), "from": src, "to": dst, "month": month}}
    if op == "reconciled":
        before = dict(b.get("reconciled") or {})
        n = R.baseline(b, led, c)
        B.save(b)
        return {"ok": True, "line": f"reconciled: the bank's balances on {n} accounts are the baseline from today", "undo": {"op": "money_undo", "what": "reconciled", "before": before}}
    if op == "fund":
        side = str(payload.get("side") or "").lower()
        mdl = B.model(led, b, c, r)
        sides = [side] if side in mdl["sides"] else [s for s in mdl["sides"]]
        funded, undo = [], []
        for s in sides:
            p = B.plan(s, mdl)
            if p["fund"]:
                B.apply_plan(b, p, month, why)
                funded.append(f"{s} {C.money(p['total'])} into {len(p['fund'])} envelopes")
                undo.append({"side": s, "fund": p["fund"]})
            b.setdefault("fund_asked", {})[s] = month
        B.save(b)
        if not funded:
            return {"ok": True, "line": "every envelope was already at its target; nothing to assign", "undo": {"op": "money_undo", "what": "fund", "plans": [], "month": month}}
        return {"ok": True, "line": "funded: " + "; ".join(funded), "undo": {"op": "money_undo", "what": "fund", "plans": undo, "month": month}}
    return {"ok": False, "line": f"no money deed called {op!r}"}


def _amt(v):
    try:
        x = float(str(v).replace("$", "").replace(",", "").strip())
        return abs(x) if x == x else None
    except (TypeError, ValueError):
        return None


def undo(u: dict) -> bool:
    what = str(u.get("what") or "")
    b = B.load()
    if what == "place":
        r = C.rules()
        r["learned"] = [x for x in r.get("learned") or [] if x.get("id") != u.get("rule_id")]
        C.save_rules(r)
        led = C.ledger()
        for tid in u.get("rows") or []:
            t = led["transactions"].get(tid)
            if t and t.get("rule_id") == u.get("rule_id"):
                t.update({"bucket": "", "bucket_src": "none", "rule_id": ""})
        for t in led["transactions"].values():
            if t.get("rule_id") == u.get("rule_id"):
                t.update({"bucket": "", "bucket_src": "none", "rule_id": ""})
        C.save_ledger(led)
        return True
    if what == "target":
        if u.get("before") is None:
            b.get("targets", {}).pop(u.get("envelope"), None)
        else:
            b.setdefault("targets", {})[u["envelope"]] = u["before"]
        B.save(b)
        return True
    if what == "move":
        B.move(b, float(u.get("amount") or 0), u.get("to"), u.get("from"), str(u.get("month") or B.month_key(C.now_local().date())), "undo") \
            if u.get("from") != "ready" else B.assign(b, u.get("to"), -float(u.get("amount") or 0), str(u.get("month") or ""), "undo")
        B.save(b)
        return True
    if what == "reconciled":
        b["reconciled"] = dict(u.get("before") or {})
        B.save(b)
        return True
    if what == "fund":
        for p in u.get("plans") or []:
            for e, amt in (p.get("fund") or {}).items():
                B.assign(b, e, -float(amt), str(u.get("month") or ""), "undo fund")
            b.get("fund_asked", {}).pop(p.get("side"), None)
        B.save(b)
        return True
    return False


def main() -> int:
    if len(sys.argv) > 1 and sys.argv[1] == "questions":
        print(json.dumps(questions(), indent=1, ensure_ascii=False, default=str))
    else:
        print(block())
    return 0


if __name__ == "__main__":
    sys.exit(main())

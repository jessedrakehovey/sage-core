"""budget.py - the envelope budget (give every dollar a job) over the money lane's ledger.

The ledger (common.py) already holds every account and transaction from the read-only feeds. This module
adds the budget on top, the way an envelope budget works:

  Ready to assign (per side)  = cash in the spending accounts - every envelope's positive available
                                - what the credit cards owe
  Envelope available (month)  = carried over (last month's available, floored at 0 unless
                                budget.carry_overspend) + assigned this month + activity this month
  Activity                    = the transactions in the SPENDING accounts and CARDS whose bucket is
                                that envelope (pending included; own-account transfers never)

Every account plays one ROLE (money.json -> accounts.overrides[id].budget, else by its type and name):
  spending  the checking / operating accounts day-to-day money leaves from (and income lands in)
  card      a credit card: its spending counts against the envelopes, its balance is debt the budget covers
  vault     a sub-account that IS an envelope (a savings bucket, a tax bucket, an emergency fund):
            its balance is its available, the owner funds it at the bank, nothing virtual to assign
  off       outside the budget (investments, loans)
A card charge from an envelope leaves "ready to assign" unchanged (the envelope drops, the card debt
rises by the same amount), and paying the card leaves it unchanged too (cash and debt drop together) -
the money for the card payment is held, which is the whole point.

Assigning is the owner's decision. On budget.fund_day ONE card per side proposes the month's plan
("refill every envelope to its target": money.json -> budget.targets, the owner's typed targets, else the
median of the last target_months months of activity) and the tap funds it; words typed in the thread
("groceries 500", "move 50 from dining to groceries", "assign 200 to travel", "target groceries 600")
change it. The same words work on the weekly reconciliation card (reconcile.py) and from the command
line. Zero LLM; nothing here moves money at a bank - an envelope is a number in data/money/budget.json.

  python assistant/scripts/money/budget.py              # tick: harvest typed answers + fund card when due (the spine runs this)
  python assistant/scripts/money/budget.py status       # the budget as JSON (Mission Control /api/money)
  python assistant/scripts/money/budget.py show         # the budget in plain text
  python assistant/scripts/money/budget.py assign 200 personal:groceries [--month 2026-11]
  python assistant/scripts/money/budget.py move 50 personal:dining personal:groceries
  python assistant/scripts/money/budget.py target personal:groceries 600
  python assistant/scripts/money/budget.py start 2026-11  # the first budgeted month (default: the month of the first assignment)
  python assistant/scripts/money/budget.py --dry-run | --force
"""
from __future__ import annotations

import argparse
import html
import json
import re
import statistics
import sys
import time
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import common as C  # noqa: E402

BUDGET = C.DIR / "budget.json"
ROLES = ("spending", "card", "vault", "off")
FUND_HEADER = "Money: fund the envelopes"


# ------------------------------------------------------------------ state
def load() -> dict:
    b = C.load_json(BUDGET, {}) or {}
    b.setdefault("months", {})          # "YYYY-MM": {"assigned": {env: amt}, "log": [...]}
    b.setdefault("targets", {})         # env -> amt, the owner's typed targets (money.json -> budget.targets is the organization's)
    b.setdefault("reconciled", {})      # account id -> {date, balance, through, same_day_ids}
    b.setdefault("fund_cards", {})      # qid -> {side, month, plan, ts}
    b.setdefault("week_cards", {})      # qid -> {week, ts}
    return b


def save(b: dict) -> None:
    C.save_json(BUDGET, b)


def month_key(d: date) -> str:
    return f"{d.year:04d}-{d.month:02d}"


def months_between(a: str, b: str) -> list:
    ya, ma = int(a[:4]), int(a[5:7])
    yb, mb = int(b[:4]), int(b[5:7])
    out = []
    while (ya, ma) <= (yb, mb):
        out.append(f"{ya:04d}-{ma:02d}")
        ma += 1
        if ma == 13:
            ya, ma = ya + 1, 1
    return out


# ------------------------------------------------------------------ accounts and envelopes
def role(acct: dict, c: dict | None = None) -> str:
    """spending | card | vault | off for one ledger account (overrides by id first, then type + name)."""
    c = c or C.cfg()
    ov = (c["accounts"].get("overrides") or {}).get(str(acct.get("id") or "")) or {}
    if str(ov.get("budget") or "") in ROLES:
        return str(ov["budget"])
    k = c["budget"]
    name = str(acct.get("name") or "").lower()
    typ = str(acct.get("type") or "").lower()
    sub = str(acct.get("subtype") or "").lower()
    kind, _ = C.account_kind(acct.get("name") or "", str(acct.get("id") or ""), c)
    if typ == "credit" or kind == "card":
        return "card"
    if typ in ("investment", "brokerage", "loan") or _rx(k.get("off_match")).search(name):
        return "off"
    if kind in ("savings", "tax") or "cash management" in sub or _rx(k.get("vault_match")).search(name):
        return "vault"
    return "spending"


def _rx(pat) -> re.Pattern:
    try:
        return re.compile(str(pat or "$^"), re.I)
    except re.error:
        return re.compile("$^")


def side_of(acct: dict, c: dict | None = None) -> str:
    return C.account_kind(acct.get("name") or "", str(acct.get("id") or ""), c)[1]


def accounts(led: dict, c: dict | None = None) -> list:
    """Every ledger account with its live kind, side and role (classified now, not as stored at merge)."""
    c = c or C.cfg()
    out = []
    for a in led["accounts"].values():
        kind, side = C.account_kind(a.get("name") or "", str(a.get("id") or ""), c)
        out.append({**a, "kind": kind, "side": side, "role": role(a, c)})
    out.sort(key=lambda a: (a["side"], ROLES.index(a["role"]), str(a.get("name") or "")))
    return out


def envelopes(side: str, r: dict | None = None) -> list:
    """The virtual envelopes of one side: its expense (and tax) buckets, in money_rules.json order."""
    r = r or C.rules()
    return [k for k, v in (r.get("buckets") or {}).items()
            if not k.startswith("_") and v.get("side") == side and v.get("kind") in ("expense", "tax")]


def label(env: str, r: dict | None = None) -> str:
    return str(C.bucket_meta(env, r).get("label") or env)


# ------------------------------------------------------------------ the model (pure: ledger + budget state -> numbers)
def _activity(led: dict, accts: list, since: str) -> dict:
    """(month, env) -> sum of amounts in spending accounts and cards; plus (month, '') for unplaced rows."""
    live = {a["id"]: a for a in accts if a["role"] in ("spending", "card")}
    out = {}
    for t in led["transactions"].values():
        a = live.get(t.get("account_id"))
        if not a:
            continue
        m = str(t.get("posted") or "")[:7]
        if len(m) != 7 or m < since:
            continue
        b = str(t.get("bucket") or "")
        if b == "transfer" or b == "ignore" or (not b and C.is_transfer(t.get("description") or "")):
            continue
        amt = float(t.get("amount") or 0)
        if not b and amt > 0:
            continue                                   # an unplaced deposit is income until a rule says otherwise
        out[(m, b, a["side"])] = out.get((m, b, a["side"]), 0.0) + amt
    return out


def start_month(b: dict, c: dict, today: date) -> str:
    s = str(b.get("start_month") or c["budget"].get("start_month") or "")
    if re.fullmatch(r"\d{4}-\d{2}", s):
        return s
    months = sorted(m for m, v in b.get("months", {}).items() if (v.get("assigned") or {}))
    return months[0] if months else month_key(today)


def target(env: str, b: dict, c: dict, act: dict, month: str, r: dict | None = None) -> float:
    """The envelope's monthly target: the owner's typed one, the organization's, else the median of the
    last target_months months of activity (0 when nothing was ever spent)."""
    t = (b.get("targets") or {}).get(env)
    if t is None:
        t = (c["budget"].get("targets") or {}).get(env)
    if t is not None:
        try:
            return round(float(t), 2)
        except (TypeError, ValueError):
            pass
    n = int(c["budget"].get("target_months") or 3)
    y, m = int(month[:4]), int(month[5:7])
    vals = []
    side = C.bucket_meta(env, r).get("side")
    for _ in range(n):
        m -= 1
        if m == 0:
            y, m = y - 1, 12
        k = f"{y:04d}-{m:02d}"
        v = -float(act.get((k, env, side), 0.0))
        if v > 0:
            vals.append(v)
    return round(statistics.median(vals), 2) if vals else 0.0


def model(led: dict, b: dict | None = None, c: dict | None = None, r: dict | None = None, today: date | None = None) -> dict:
    c = c or C.cfg()
    r = r or C.rules()
    b = b if b is not None else load()
    today = today or C.now_local().date()
    cur = month_key(today)
    start = start_month(b, c, today)
    if start > cur:
        start = cur
    accts = accounts(led, c)
    # activity from target_months before the start month too, so the first month's targets have history to read
    y, mo = int(start[:4]), int(start[5:7]) - (int(c["budget"].get("target_months") or 3) + 1)
    while mo <= 0:
        y, mo = y - 1, mo + 12
    act = _activity(led, accts, f"{y:04d}-{mo:02d}")
    carry = bool(c["budget"].get("carry_overspend"))
    out = {"month": cur, "start": start, "sides": {}, "generated": C.now_local().strftime("%Y-%m-%d %H:%M")}
    for side in c["budget"].get("sides") or ["personal", "business"]:
        envs = envelopes(side, r)
        avail = {e: 0.0 for e in envs}
        rows = []
        for m in months_between(start, cur):
            assigned = (b["months"].get(m) or {}).get("assigned") or {}
            nxt = {}
            for e in envs:
                carried = avail[e] if carry else max(avail[e], 0.0)
                a = float(assigned.get(e) or 0.0)
                ac = float(act.get((m, e, side), 0.0))
                nxt[e] = round(carried + a + ac, 2)
                if m == cur:
                    rows.append({"key": e, "label": label(e, r), "carried": round(carried, 2), "assigned": round(a, 2),
                                 "activity": round(ac, 2), "available": nxt[e],
                                 "target": target(e, b, c, act, cur, r)})
            avail = nxt
        side_accts = [a for a in accts if a["side"] == side]
        cash = sum(float(a.get("balance") or 0) for a in side_accts if a["role"] == "spending")
        debt = -sum(min(float(a.get("balance") or 0), 0.0) for a in side_accts if a["role"] == "card")
        held = sum(max(x["available"], 0.0) for x in rows)
        unplaced = float(act.get((cur, "", side), 0.0))
        out["sides"][side] = {
            "ready": round(cash - held - debt, 2), "cash": round(cash, 2), "card_debt": round(debt, 2), "held": round(held, 2),
            "envelopes": rows,
            "overspent": [x for x in rows if x["available"] < -0.004],
            "unplaced": round(-unplaced, 2),
            "vaults": [{"id": a["id"], "name": a.get("name"), "balance": round(float(a.get("balance") or 0), 2),
                        "as_of": a.get("balance_date")} for a in side_accts if a["role"] == "vault"],
            "cards": [{"id": a["id"], "name": a.get("name"), "owed": round(-min(float(a.get("balance") or 0), 0.0), 2),
                       "as_of": a.get("balance_date")} for a in side_accts if a["role"] == "card"],
            "spending": [{"id": a["id"], "name": a.get("name"), "balance": round(float(a.get("balance") or 0), 2),
                          "as_of": a.get("balance_date")} for a in side_accts if a["role"] == "spending"],
        }
    return out


def plan(side: str, mdl: dict, ready: float | None = None) -> dict:
    """Refill every envelope to its target, in envelope order, as far as ready-to-assign allows.
    -> {"fund": {env: amt}, "total": x, "underfunded": [(env, short)], "ready": x}."""
    s = mdl["sides"][side]
    ready = s["ready"] if ready is None else float(ready)
    left = max(ready, 0.0)
    fund, under = {}, []
    for e in s["envelopes"]:
        before = e["carried"] + e["assigned"]            # what it holds before this month's spending
        want = round(max(e["target"] - before, 0.0), 2)
        if want <= 0:
            continue
        give = round(min(want, left), 2)
        if give > 0:
            fund[e["key"]] = give
            left = round(left - give, 2)
        if give < want:
            under.append((e["key"], round(want - give, 2)))
    return {"fund": fund, "total": round(sum(fund.values()), 2), "underfunded": under, "ready": round(ready, 2)}


# ------------------------------------------------------------------ writes (an envelope is a number in budget.json)
def _log(b: dict, month: str, **row) -> None:
    b["months"].setdefault(month, {"assigned": {}, "log": []}).setdefault("log", []).append(
        {"ts": C.now_local().strftime("%Y-%m-%d %H:%M"), **row})


def assign(b: dict, env: str, amount: float, month: str, why: str = "") -> dict:
    m = b["months"].setdefault(month, {"assigned": {}, "log": []})
    m["assigned"][env] = round(float(m["assigned"].get(env) or 0) + float(amount), 2)
    _log(b, month, op="assign", env=env, amount=round(float(amount), 2), why=why)
    return m


def move(b: dict, amount: float, src: str, dst: str, month: str, why: str = "") -> None:
    """Money from one envelope to another (src may be 'ready' = ready to assign)."""
    if src != "ready":
        assign(b, src, -abs(float(amount)), month, why or f"to {dst}")
    assign(b, dst, abs(float(amount)), month, why or (f"from {src}" if src != "ready" else "from ready to assign"))


def apply_plan(b: dict, p: dict, month: str, why: str) -> int:
    for e, amt in (p.get("fund") or {}).items():
        assign(b, e, amt, month, why)
    return len(p.get("fund") or {})


# ------------------------------------------------------------------ the owner's words: 'move 50 from dining to groceries'
_NUM = r"\$?\s*(\d+(?:[.,]\d+)?)"
_W = r"([a-z][a-z0-9 :&'/-]*?)"
_P = {
    "move": re.compile(rf"\bmove\s+{_NUM}\s+(?:from\s+)?{_W}\s+(?:to|->|into)\s+{_W}\s*(?=$|[,;\n.]|\band\b)", re.I),
    "assign": re.compile(rf"\b(?:assign|add|put)\s+{_NUM}\s+(?:to|in|into)\s+{_W}\s*(?=$|[,;\n.]|\band\b)", re.I),
    "cover": re.compile(rf"\bcover\s+{_W}(?:\s+(?:from|with)\s+{_W})?\s*(?=$|[,;\n.]|\band\b)", re.I),
    "target": re.compile(rf"\btarget\s+{_W}\s+(?:to\s+|at\s+|=\s*)?{_NUM}\s*(?=$|[,;\n.]|\band\b)", re.I),
    "pair": re.compile(rf"(?:^|[,;\n]|\band\b)\s*{_W}\s*[:=]?\s*{_NUM}\s*(?=$|[,;\n]|\band\b)", re.I),
}


def _env(word: str, r: dict) -> str:
    import categorize
    w = str(word or "").strip().lower()
    if w in ("ready", "ready to assign", "rta", "available"):
        return "ready"
    return categorize.resolve_bucket(w, r)


def parse_words(text: str, r: dict | None = None, allow_pairs: bool = False) -> tuple:
    """-> ([ops], [unrecognised]). ops: ('move', amt, src, dst) | ('assign', amt, env) | ('cover', env, src) |
    ('target', env, amt) | ('set', env, amt) (the 'groceries 500' form, only on a fund card)."""
    r = r or C.rules()
    ops, bad = [], []
    t = str(text or "")

    def num(s):
        return float(str(s).replace(",", ""))

    for m in _P["move"].finditer(t):
        a, b2 = _env(m.group(2), r), _env(m.group(3), r)
        (ops.append(("move", num(m.group(1)), a, b2)) if a and b2 and b2 != "ready" else bad.append(m.group(0).strip()))
    for m in _P["target"].finditer(t):
        e = _env(m.group(1), r)
        (ops.append(("target", e, num(m.group(2)))) if e and e != "ready" else bad.append(m.group(0).strip()))
    for m in _P["assign"].finditer(t):
        e = _env(m.group(2), r)
        (ops.append(("assign", num(m.group(1)), e)) if e and e != "ready" else bad.append(m.group(0).strip()))
    for m in _P["cover"].finditer(t):
        e, s = _env(m.group(1), r), (_env(m.group(2), r) if m.group(2) else "ready")
        (ops.append(("cover", e, s or "ready")) if e and e != "ready" else bad.append(m.group(0).strip()))
    if allow_pairs:
        taken = [m.span() for k in ("move", "target", "assign", "cover") for m in _P[k].finditer(t)]
        for m in _P["pair"].finditer(t):
            if any(a <= m.start(1) < z for a, z in taken):
                continue
            e = _env(m.group(1), r)
            if e and e != "ready":
                ops.append(("set", e, num(m.group(2))))
    return ops, bad


def run_ops(b: dict, ops: list, led: dict, month: str, c: dict, r: dict, why: str) -> list:
    """Apply typed ops; 'cover X' takes what X is short from the named envelope or ready to assign.
    Returns the lines said back."""
    said = []
    for op in ops:
        if op[0] == "move":
            _, amt, src, dst = op
            move(b, amt, src, dst, month, why)
            said.append(f"moved {C.money(amt)} from {label(src, r) if src != 'ready' else 'ready to assign'} to {label(dst, r)}")
        elif op[0] == "assign":
            _, amt, env = op
            assign(b, env, amt, month, why)
            said.append(f"assigned {C.money(amt)} to {label(env, r)}")
        elif op[0] == "target":
            _, env, amt = op
            b.setdefault("targets", {})[env] = round(amt, 2)
            said.append(f"{label(env, r)} target is {C.money(amt)} a month")
        elif op[0] == "cover":
            _, env, src = op
            mdl = model(led, b, c, r)
            row = next((x for s in mdl["sides"].values() for x in s["envelopes"] if x["key"] == env), None)
            short = -row["available"] if row and row["available"] < 0 else 0.0
            if short <= 0:
                said.append(f"{label(env, r)} is not overspent")
                continue
            move(b, short, src, env, month, why)
            said.append(f"covered {label(env, r)}'s {C.money(short)} from {label(src, r) if src != 'ready' else 'ready to assign'}")
    return said


# ------------------------------------------------------------------ the monthly fund card
def fund_question(side: str, mdl: dict, r: dict, p: dict) -> dict:
    s = mdl["sides"][side]
    month_name = date(int(mdl["month"][:4]), int(mdl["month"][5:7]), 1).strftime("%B")
    lines = [f"{label(e, r)} {C.money(a)}" for e, a in p["fund"].items()]
    under = [f"{label(e, r)} short {C.money(x)}" for e, x in p["underfunded"]]
    q = (f"Ready to assign for {month_name} ({side}): {C.money(s['ready'])} "
         f"(cash {C.money(s['cash'])}, already in envelopes {C.money(s['held'])}"
         + (f", cards owe {C.money(s['card_debt'])}" if s["card_debt"] else "") + ").")
    det = []
    if p["fund"]:
        q += f" The plan refills {len(p['fund'])} envelopes to their targets, {C.money(p['total'])} in all (the lines are in the details)."
        det.append("The plan: " + ", ".join(lines) + ".")
    else:
        q += " Every envelope is already at its target, so nothing needs assigning."
    if under:
        q += f" {len(under)} short of their target."
        det.append("Not enough to fill: " + ", ".join(under) + ".")
    if s["ready"] < 0:
        q += " Below zero means the cards owe more than the cash on hand covers; the envelopes keep what they have."
    det.append("Type changes in this thread like \"groceries 500\", \"move 50 from dining to groceries\" or \"target groceries 600\".")
    det.append("Envelopes now: " + ", ".join(f"{x['label']} {C.money(x['available'])}" + (f" of {C.money(x['target'])}" if x["target"] else "") for x in s["envelopes"]))
    return {"header": f"{FUND_HEADER} ({side})", "lane": "money", "question": q,
            "details": "\n".join(det),
            "advice": "💡 Fund it: every dollar on hand gets a job for the month; nothing here moves money at the bank.",
            "options": [{"label": "Fund it (Recommended)", "description": "assign the plan (plus anything I typed here)"},
                        {"label": "Only what I typed", "description": "assign just the lines I typed in this thread"},
                        {"label": "Skip this month", "description": "leave the envelopes as they are"}]}


def post_fund_cards(b: dict, led: dict, c: dict, r: dict, now, dry: bool, force: bool) -> list:
    k = c["budget"]
    if not k.get("fund_card", True):
        return []                                  # the money talk asks instead (brief.py)
    cur = month_key(now.date())
    due = force or (now.day >= int(k.get("fund_day") or 1) and (now.hour, now.minute) >= C.hm(k.get("fund_after") or "08:30"))
    if not due or not C.in_hours(now, k.get("post_hours") or ["08:00", "20:30"]) and not force:
        return []
    mdl = model(led, b, c, r, now.date())
    posted = []
    for side in k.get("sides") or ["personal", "business"]:
        if (b.setdefault("fund_month", {})).get(side) == cur and not force:
            continue
        s = mdl["sides"].get(side)
        if not s or not s["envelopes"] or not (s["spending"] or s["cards"]):
            continue                                   # a side with no accounts has no budget yet
        p = plan(side, mdl)
        q = fund_question(side, mdl, r, p)
        if dry:
            print("WOULD POST CARD " + json.dumps(q, ensure_ascii=False)[:1500])
            posted.append(side)
            continue
        import cards
        res = cards.post([q], owner="money.budget", lane="money")
        if res.get("posted"):
            b["fund_cards"][res["qid"]] = {"side": side, "month": cur, "plan": p["fund"], "ts": time.time()}
            b["fund_month"][side] = cur
            C.log("fund_card", side=side, qid=res["qid"], total=p["total"])
            posted.append(side)
        else:
            C.log("fund_refused", side=side, reason=str(res.get("reason"))[:200])
    return posted


def harvest(b: dict, led: dict, c: dict, r: dict, answers: list | None = None) -> int:
    """The owner's taps and words on our fund cards (and the weekly card's typed moves, see reconcile.py)."""
    pending = b.get("fund_cards") or {}
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
        info = pending.pop(qid)
        month, side = str(info.get("month") or month_key(C.now_local().date())), str(info.get("side") or "")
        opt = str(a.get("option") or "")
        text = str(a.get("text") or a.get("revised_text") or "")
        ops, bad = parse_words(text, r, allow_pairs=True)
        sets = {e: amt for op in ops if op[0] == "set" for _, e, amt in [op]}
        others = [op for op in ops if op[0] != "set"]
        said = []
        if opt.startswith("Skip"):
            C.log("fund_skipped", qid=qid, side=side)
            continue
        if opt.startswith("Fund"):
            p = {"fund": dict(info.get("plan") or {})}
            p["fund"].update(sets)
            apply_plan(b, p, month, f"fund card {qid}")
            said.append(f"funded {len(p['fund'])} envelopes")
        elif sets:
            apply_plan(b, {"fund": sets}, month, f"fund card {qid} (typed)")
            said.append(f"assigned {len(sets)} typed lines")
        said += run_ops(b, others, led, month, c, r, f"fund card {qid}")
        n += 1
        C.log("fund_answer", qid=qid, side=side, option=opt[:40], said=said, bad=bad)
        if said:
            _lesson("; ".join(said), qid, c)
    b["fund_cards"] = pending
    return n


def _lesson(text: str, qid: str, c: dict) -> None:
    try:
        sys.path.insert(0, str(C.VAULT / "scripts" / "lib"))
        import learnings
        learnings.record_lesson(C.VAULT / str(c["categorize"].get("learn_to") or "assistant/learnings/money.md"),
                                text, context=f"card {qid}", title="Money lane learnings")
    except Exception as e:  # noqa: BLE001
        C.log("lesson_error", error=f"{type(e).__name__}: {str(e)[:120]}")


# ------------------------------------------------------------------ tick + text + page
def tick(now=None, dry: bool = False, force: bool = False) -> dict:
    c, r = C.cfg(), C.rules()
    now = now or C.now_local()
    led = C.ledger()
    b = load()
    harvested = harvest(b, led, c, r)
    posted = post_fund_cards(b, led, c, r, now, dry, force)
    if not dry:
        save(b)
    mdl = model(led, b, c, r, now.date())
    out = {"harvested": harvested, "fund_cards": posted,
           "ready": {s: v["ready"] for s, v in mdl["sides"].items()},
           "overspent": {s: len(v["overspent"]) for s, v in mdl["sides"].items()}}
    print("BUDGET " + json.dumps(out))
    return out


def status() -> dict:
    """Mission Control /api/money."""
    return model(C.ledger())


def text(mdl: dict | None = None) -> str:
    mdl = mdl or status()
    r = C.rules()
    out = [f"Budget for {mdl['month']} (from {mdl['start']})"]
    for side, s in mdl["sides"].items():
        out.append(f"\n[{side}] ready to assign {C.money(s['ready'])}  cash {C.money(s['cash'])}  in envelopes {C.money(s['held'])}"
                   f"  cards owe {C.money(s['card_debt'])}  unplaced this month {C.money(s['unplaced'])}")
        for e in s["envelopes"]:
            flag = "  OVERSPENT" if e["available"] < 0 else ""
            out.append(f"  {e['label']:28} carried {C.money(e['carried']):>8}  assigned {C.money(e['assigned']):>8}  activity {C.money(e['activity']):>9}"
                       f"  available {C.money(e['available']):>9}  target {C.money(e['target']):>7}{flag}")
        for v in s["vaults"]:
            out.append(f"  (vault) {str(v['name'])[:26]:28} {C.money(v['balance']):>10}")
    return "\n".join(out)


_CSS = ("<style>:root{--bg:#050B07;--fg:#FBFFFA;--sage:#4A5C43;--sage2:#9DB392;--line:#1b2a1f;--mute:#DFE0E1}"
        "body{margin:0;background:var(--bg);color:var(--fg);font:15px/1.45 Poppins,system-ui,sans-serif;padding:16px}"
        "h1,h2,h3{font-family:'Playfair Display',Georgia,serif;font-weight:500;margin:.2em 0}h1{font-size:28px}h2{font-size:21px;color:var(--sage2);margin-top:24px}"
        "h3{font-size:16px;color:var(--sage2);margin-top:16px}.card{border:1px solid var(--line);border-radius:12px;padding:12px 14px;margin:10px 0;background:#0a140d}"
        "table{border-collapse:collapse;width:100%}td,th{text-align:left;padding:6px 8px;border-bottom:1px solid var(--line);vertical-align:top}"
        "th{color:var(--sage2);font-weight:600;font-size:12px;text-transform:uppercase;letter-spacing:.04em}td.n,th.n{text-align:right;font-variant-numeric:tabular-nums}"
        ".ok{color:#9DB392}.warn{color:#E8C170}.bad{color:#E07A5F}code{background:#111c14;padding:1px 5px;border-radius:5px;font-size:13px}a{color:var(--sage2)}.mute{color:var(--mute);opacity:.75}"
        ".big{font-size:26px;font-family:'Playfair Display',Georgia,serif}.bar{height:6px;background:#162219;border-radius:3px;overflow:hidden;margin-top:3px}.bar i{display:block;height:6px;background:var(--sage2)}"
        ".bar i.bad{background:#E07A5F}.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(170px,1fr));gap:10px}@media(max-width:600px){td,th{padding:5px 4px;font-size:13px}}</style>")


def page() -> str:
    """The Mission Control /money page: read-only, the commands that change things in the footer."""
    e = html.escape
    mdl = status()
    rec = _reconcile_rows()
    parts = ["<!doctype html><html lang=en><head><meta charset=utf-8><meta name=viewport content='width=device-width,initial-scale=1'>",
             "<title>Money</title>", _CSS, "</head><body><div style='font-size:14px'><a href='/'>&larr; back</a></div>",
             f"<h1>Money</h1><div class=mute>Budget month {e(mdl['month'])} &middot; budgeting since {e(mdl['start'])} &middot; as of {e(mdl['generated'])}</div>"]
    for side, s in mdl["sides"].items():
        cls = "ok" if s["ready"] >= 0 else "bad"
        parts.append(f"<h2>{e(side.title())}</h2><div class=grid>"
                     f"<div class=card><div class=mute>Ready to assign</div><div class='big {cls}'>{e(C.money(s['ready']))}</div></div>"
                     f"<div class=card><div class=mute>Cash in spending accounts</div><div class=big>{e(C.money(s['cash']))}</div></div>"
                     f"<div class=card><div class=mute>Held in envelopes</div><div class=big>{e(C.money(s['held']))}</div></div>"
                     f"<div class=card><div class=mute>Cards owe</div><div class='big {'warn' if s['card_debt'] else ''}'>{e(C.money(s['card_debt']))}</div></div>"
                     f"<div class=card><div class=mute>Unplaced this month</div><div class='big {'warn' if s['unplaced'] else ''}'>{e(C.money(s['unplaced']))}</div></div></div>")
        parts.append("<h3>Envelopes</h3><table><tr><th>Envelope</th><th class=n>Carried</th><th class=n>Assigned</th><th class=n>Spent</th><th class=n>Available</th><th class=n>Target</th></tr>")
        for x in s["envelopes"]:
            av = x["available"]
            cls = "bad" if av < 0 else ("warn" if x["target"] and av < 0.15 * x["target"] else "ok")
            pct = 0 if not x["target"] else max(0, min(100, int(100 * av / x["target"])))
            bar = f"<div class=bar><i class='{'bad' if av < 0 else ''}' style='width:{pct}%'></i></div>" if x["target"] else ""
            parts.append(f"<tr><td>{e(x['label'])}{bar}</td><td class=n>{e(C.money(x['carried']))}</td><td class=n>{e(C.money(x['assigned']))}</td>"
                         f"<td class=n>{e(C.money(-x['activity']))}</td><td class='n {cls}'>{e(C.money(av))}</td><td class='n mute'>{e(C.money(x['target']) if x['target'] else '-')}</td></tr>")
        if not s["envelopes"]:
            parts.append("<tr><td colspan=6 class=mute>No envelopes on this side: add expense buckets to money_rules.json.</td></tr>")
        parts.append("</table>")
        if s["vaults"]:
            parts.append("<h3>Vaults (an envelope with its own account)</h3><table><tr><th>Account</th><th class=n>Balance</th><th>As of</th></tr>")
            for v in s["vaults"]:
                parts.append(f"<tr><td>{e(str(v['name']))}</td><td class=n>{e(C.money(v['balance']))}</td><td class=mute>{e(str(v['as_of'] or '')[:10])}</td></tr>")
            parts.append("</table>")
        parts.append("<h3>Accounts</h3><table><tr><th>Account</th><th>Role</th><th class=n>Bank balance</th><th>Reconciled</th></tr>")
        for a in s["spending"] + s["cards"]:
            rr = rec.get(a["id"]) or {}
            st = rr.get("state") or "no baseline"
            cls = {"ok": "ok", "off": "bad", "stale": "warn"}.get(st, "mute")
            note = rr.get("note") or ""
            parts.append(f"<tr><td>{e(str(a['name']))}</td><td class=mute>{'card' if 'owed' in a else 'spending'}</td>"
                         f"<td class=n>{e(C.money(-a['owed'] if 'owed' in a else a['balance']))}</td><td class={cls}>{e(st)}{(' <span class=mute>' + e(note) + '</span>') if note else ''}</td></tr>")
        parts.append("</table>")
    parts.append("<div class='mute' style='margin-top:22px'>Move money: <code>python assistant/scripts/money/budget.py move 50 personal:dining personal:groceries</code> &middot; "
                 "assign: <code>budget.py assign 200 personal:groceries</code> &middot; target: <code>budget.py target personal:groceries 600</code>. "
                 "The weekly reconciliation card takes the same words typed in its thread. Nothing here moves money at a bank.</div></body></html>")
    return "".join(parts)


def _reconcile_rows() -> dict:
    try:
        import reconcile
        return {row["id"]: row for row in reconcile.check(C.ledger(), load(), C.cfg())["rows"]}
    except Exception as ex:  # noqa: BLE001
        C.log("page_reconcile_error", error=f"{type(ex).__name__}: {str(ex)[:120]}")
        return {}


# ------------------------------------------------------------------ cli
def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("cmd", nargs="?", default="tick", choices=["tick", "status", "show", "assign", "move", "target", "start", "page"])
    ap.add_argument("args", nargs="*")
    ap.add_argument("--month", default="")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--force", action="store_true")
    a = ap.parse_args()
    if a.cmd == "status":
        print(json.dumps(status(), indent=1, ensure_ascii=False))
        return 0
    if a.cmd == "show":
        print(text())
        return 0
    if a.cmd == "page":
        print(page())
        return 0
    b = load()
    r = C.rules()
    month = a.month or month_key(C.now_local().date())
    import categorize
    if a.cmd == "start":
        if not re.fullmatch(r"\d{4}-\d{2}", a.args[0] if a.args else ""):
            print("usage: budget.py start YYYY-MM")
            return 2
        b["start_month"] = a.args[0]
    elif a.cmd == "assign":
        amt, env = float(a.args[0]), categorize.resolve_bucket(a.args[1], r)
        if not env:
            print(f"no envelope matches {a.args[1]!r}")
            return 2
        assign(b, env, amt, month, "cli")
        print(f"assigned {C.money(amt)} to {label(env, r)} for {month}")
    elif a.cmd == "move":
        amt, src, dst = float(a.args[0]), categorize.resolve_bucket(a.args[1], r) or ("ready" if a.args[1] == "ready" else ""), categorize.resolve_bucket(a.args[2], r)
        if not src or not dst:
            print("usage: budget.py move AMOUNT FROM_ENVELOPE|ready TO_ENVELOPE")
            return 2
        move(b, amt, src, dst, month, "cli")
        print(f"moved {C.money(amt)} from {label(src, r) if src != 'ready' else 'ready to assign'} to {label(dst, r)} for {month}")
    elif a.cmd == "target":
        env, amt = categorize.resolve_bucket(a.args[0], r), float(a.args[1])
        if not env:
            print(f"no envelope matches {a.args[0]!r}")
            return 2
        b["targets"][env] = round(amt, 2)
        print(f"{label(env, r)} target {C.money(amt)} a month")
    else:
        tick(dry=a.dry_run, force=a.force)
        return 0
    if not a.dry_run:
        save(b)
    return 0


if __name__ == "__main__":
    sys.exit(main())

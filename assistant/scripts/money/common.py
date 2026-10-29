"""common.py - shared plumbing for the money lane (assistant/scripts/money/).

Reads assistant/money.json (the organization's settings and tax tables) and assistant/money_rules.json
(merchant rules). Writes only under assistant/data/money/ (git-ignored). Zero-LLM. READ-ONLY on every
bank: nothing here can pay, transfer or enter a credential. See assistant/TRACKING_DESIGN.md rulings
14 and 15.

The ledger (data/money/ledger.json) is the contract every job reads:
  {"accounts": {id: {id, name, org, kind, side, balance, balance_date, currency}},
   "transactions": {id: {id, account_id, posted (YYYY-MM-DD), amount (float, outflow negative),
                         description, pending, bucket, bucket_src (rule|learned|transfer|none), rule_id}},
   "pulled": {"last_ok": iso, "provider": ...}}
A feed adapter only ever merges into it (simplefin.merge, plaid.merge); each provider is its own adapter
and money.json -> feeds lists them (cfg()["feeds"]; cfg()["feed"] stays the SimpleFIN block for older callers).
"""
from __future__ import annotations

import hashlib
import json
import re
import sys
from datetime import date, datetime, timedelta
from pathlib import Path

HERE = Path(__file__).resolve().parent
SCRIPTS = HERE.parent
sys.path.insert(0, str(SCRIPTS))
from alib import DATA, ROOT, VAULT, load_json, save_json  # noqa: E402

try:                                   # PS 5.1 consoles are cp1252; the advice line carries an emoji
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except (AttributeError, ValueError):
    pass

MONEY_CFG = ROOT / "money.json"
RULES_PATH = ROOT / "money_rules.json"
DIR = DATA / "money"
LEDGER = DIR / "ledger.json"
STATE = DIR / "state.json"
LOG = DIR / "log.jsonl"

DEFAULTS = {
    "feed": {"provider": "simplefin", "secret": "simplefin:access_url", "initial_days": 365, "overlap_days": 5,
             "window_days": 60, "include_pending": True, "max_pulls_per_day": 6},
    "accounts": {"kinds": [], "overrides": {}},
    "relay": {"splits_pct": {}},
    "categorize": {"unknowns_day": "fri", "unknowns_after": "15:00", "unknowns_max_rows": 12, "min_abs_amount": 1.0,
                   "transfer_window_days": 3, "learn_to": "assistant/learnings/money.md"},
    "forecast": {"horizon_days": 35, "min_occurrences": 3,
                 "periods": {"weekly": [7, 2], "monthly": [30, 5], "quarterly": [91, 8], "yearly": [365, 20]},
                 "price_jump_pct": 0.1, "price_jump_min": 2.0, "low_balance_days": 7, "warn_latch_days": 7,
                 "post_hours": ["08:00", "20:30"]},
    "quarterly": {"method": "annualize", "remind_days_before": 10, "due": [], "set_aside_pct_floor": 0.25,
                  "set_aside_pct_cap": 0.40, "other_income_annual": 0, "prior_year_total_tax": None, "filing_status": "single"},
    "tax_tables": {"year": 2026, "federal": {}, "california": {}},
    "monthly": {"day": 1, "after": "08:00", "to": "owner1", "top_n": 3},
}

DOW = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]


def cfg() -> dict:
    """money.json over DEFAULTS, one level deep (keys starting with _ are docs).

    Feeds: money.json may carry a single `feed` block (the original SimpleFIN shape) or a `feeds` LIST, one
    block per provider. cfg()["feeds"] is always that list; cfg()["feed"] is the SimpleFIN block (or the
    first feed) so simplefin.py and older callers keep working. feed_cfg(c, "plaid") picks one by provider."""
    raw = load_json(MONEY_CFG, {}) or {}
    out = {}
    for k, dv in DEFAULTS.items():
        v = dict(dv)
        if isinstance(raw.get(k), dict):
            v.update({kk: vv for kk, vv in raw[k].items() if not kk.startswith("_")})
        out[k] = v
    feeds = []
    for blk in (raw.get("feeds") if isinstance(raw.get("feeds"), list) else []) + ([raw["feed"]] if isinstance(raw.get("feed"), dict) else []):
        if isinstance(blk, dict) and not any(f.get("provider") == blk.get("provider") for f in feeds):
            b = dict(DEFAULTS["feed"]) if blk.get("provider", "simplefin") == "simplefin" else {}
            b.update({kk: vv for kk, vv in blk.items() if not kk.startswith("_")})
            feeds.append(b)
    out["feeds"] = feeds
    sf = next((f for f in feeds if f.get("provider") == "simplefin"), None)
    if sf is not None:
        out["feed"] = sf
    return out


def feed_cfg(c: dict | None, provider: str) -> dict | None:
    """The money.json feed block for one provider, or None when the organization has not listed it."""
    c = c or cfg()
    return next((f for f in c.get("feeds") or [] if f.get("provider") == provider), None)


def rules() -> dict:
    r = load_json(RULES_PATH, {}) or {}
    r.setdefault("buckets", {})
    r.setdefault("aliases", {})
    r.setdefault("rules", [])
    r.setdefault("learned", [])
    return r


def save_rules(r: dict) -> None:
    save_json(RULES_PATH, r)


# ------------------------------------------------------------------ ledger + state
def ledger() -> dict:
    d = load_json(LEDGER, None)
    if not isinstance(d, dict):
        d = {}
    d.setdefault("accounts", {})
    d.setdefault("transactions", {})
    d.setdefault("pulled", {})
    return d


def save_ledger(d: dict) -> None:
    save_json(LEDGER, d)


def state() -> dict:
    return load_json(STATE, {}) or {}


def save_state(st: dict) -> None:
    save_json(STATE, st)


def log(event: str, **kw) -> None:
    DIR.mkdir(parents=True, exist_ok=True)
    row = {"ts": now_local().strftime("%Y-%m-%d %H:%M:%S"), "event": event}
    row.update(kw)
    with open(LOG, "a", encoding="utf-8") as f:
        f.write(json.dumps(row, ensure_ascii=False) + "\n")


# ------------------------------------------------------------------ time
def now_local() -> datetime:
    return datetime.now()


def hm(s: str) -> tuple:
    h, m = str(s).split(":")
    return int(h), int(m)


def in_hours(now: datetime, span) -> bool:
    (h1, m1), (h2, m2) = hm(span[0]), hm(span[1])
    t = now.hour * 60 + now.minute
    return h1 * 60 + m1 <= t < h2 * 60 + m2


def iso_week(now: datetime) -> str:
    y, w, _ = now.isocalendar()
    return f"{y}-W{w:02d}"


def pdate(s) -> date | None:
    try:
        return date.fromisoformat(str(s)[:10])
    except (TypeError, ValueError):
        return None


# ------------------------------------------------------------------ money text
def money(v: float, cents: bool = False) -> str:
    v = float(v or 0)
    s = f"${abs(v):,.2f}" if cents else f"${abs(v):,.0f}"
    return "-" + s if v < 0 else s


def account_kind(name: str, aid: str = "", c: dict | None = None) -> tuple:
    """(kind, side) for an account from money.json -> accounts (overrides by id first, then name patterns)."""
    c = c or cfg()
    ov = (c["accounts"].get("overrides") or {}).get(aid)
    if isinstance(ov, dict) and ov.get("kind"):
        return str(ov["kind"]), str(ov.get("side") or "business")
    low = str(name or "").lower()
    for k in c["accounts"].get("kinds") or []:
        try:
            if re.search(str(k.get("match") or ""), low):
                return str(k.get("kind") or "unknown"), str(k.get("side") or "personal")
        except re.error:
            continue
    return "unknown", "personal"


_NOISE = re.compile(r"(\b(pos|debit|credit|purchase|payment|card|ach|des|id|co|ppd|web|ccd|tst|sq|pp|sp)\b|\*|#\d+|\d{2,}|[^a-z0-9 &.'-])", re.I)


def merchant_key(desc: str) -> str:
    """A stable merchant handle from a bank description: strip card/ACH noise, dates, store numbers;
    keep the first three words. 'SQ *BLUE BOTTLE COFFEE 0421' -> 'blue bottle coffee'."""
    s = str(desc or "").lower()
    s = re.sub(r"\d{1,2}/\d{1,2}(/\d{2,4})?", " ", s)
    s = _NOISE.sub(" ", s)
    words = [w for w in s.split() if len(w) > 1]
    return " ".join(words[:3]).strip()


def txn_id(account_id: str, posted: str, amount, desc: str) -> str:
    """Deterministic id when the feed gives none."""
    h = hashlib.sha1(f"{account_id}|{posted}|{amount}|{desc}".encode("utf-8")).hexdigest()[:16]
    return "t" + h


def txns_sorted(led: dict, since: date | None = None, until: date | None = None) -> list:
    out = []
    for t in led["transactions"].values():
        d = pdate(t.get("posted"))
        if d is None or (since and d < since) or (until and d > until):
            continue
        out.append(t)
    out.sort(key=lambda t: (t.get("posted", ""), t.get("id", "")))
    return out


def bucket_meta(key: str, r: dict | None = None) -> dict:
    r = r or rules()
    return dict((r["buckets"] or {}).get(key) or {"label": key or "unplaced", "side": "none", "kind": "none", "line": ""})


def month_bounds(year: int, month: int) -> tuple:
    first = date(year, month, 1)
    last = (date(year + 1, 1, 1) if month == 12 else date(year, month + 1, 1)) - timedelta(days=1)
    return first, last

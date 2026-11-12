"""preconditions.py - a step reads the record before it fires; a chase loop is a state machine written as data.

Seed D of the agent-intelligence plan (2026-10-31, "problem spaces", RESHAPE verdict: one state machine and one
port, no planner that pre-selects judgment). Two things an agent's chase loop lacked:

  1. the steps of a chase (ask, ask again, change channel, raise it with someone else) lived in prose, so each turn
     re-derived where a chase stood from a counter and a date, and nothing told the step whether the record
     already showed it done, not done, or unreadable;
  2. a step fired on time alone, with the ticket telling an operator to "check the record first".

This module is the deterministic half of the fix. It never acts: `applicable()` names the next step the chain
would take and WHY, in plain words, as context the model reads; the model may always answer "none of these", and
every step stays a tap for the owner (the send gate is outside this module and is not weakened by it).

The verdict vocabulary (the wedding-side original of 2026-10-24 and this core agree):

  satisfied   the record shows the fact                   -> the precondition holds / the step is already done
  open        the record shows it does NOT hold yet       -> the step may not fire (or: fire it, nothing done yet)
  unknown     the record cannot say (stale, unstamped, no reader for that fact) -> never advance; say so
  moot        the record says the matter is closed (answered, cancelled, finished) -> nothing to fire

A chain (JSON):
  {"chain": "<name>", "initial": "<state>",
   "operators": [{"id": "<step>", "precond": ["<fact>", "days_since_<event>>=N", ...], "effect": "<fact the step
                  establishes>", "gated": true, "script": "<what runs on the tap>"}, ...]}

A record is a dict: the row an organization's ledger already keeps for one ask (the shape outreach.py reads:
`channel_history` [{channel, ts, result}] or the older counters), with `as_of` = when the record was last read or
written, optional `state` / `facts` (facts the writer asserts) and `events` ({event: ts}, explicit stamps). Facts
the engine derives from the history: asked_once, asked_twice (two unanswered asks in a row on one channel),
channel_changed (an unanswered ask on a second channel after a stall), answered (a reply after the last ask);
`escalated` is read from the record (events.escalated or facts). `days_since_<event>` reads events, then the
history ("ask" = the latest ask, "reask" = the latest ask of a two-or-more run, bare "days_since" = the latest
entry); a missing or stale `as_of`, or an unstamped event, reads UNKNOWN.

  register(name, fn)                   fn(record, now, **params) -> (verdict, evidence): a fact the pack can read
  register_quantity(name, fn)          fn(record, now) -> (number | None, evidence): a count or age a precondition
                                       compares ("<name>>=N"); a name ending in "_" matches as a prefix
  check(precond, record, now)          one precondition -> (verdict, evidence)
  evaluate(step, record, now)          every precondition of a step -> (verdict, [evidence]): moot > unknown >
                                       open > satisfied; the step's effect already on the record -> satisfied, done
  applicable(chain, record, now)       the next operator the chain would take, or None (nothing due, unknown, moot)
  explain(chain, record, now)          {operator, verdict, state, facts, why: [...], line} - the plain-words reading
  load_chain(path) / validate(chain)   the chain as data

Pure functions over dicts; no I/O beyond load_chain. No organization, person, channel product or system of record
is named here. Staleness: `as_of` older than `max_age_hours` (default 36) reads UNKNOWN; a binding passes its own.
"""
from __future__ import annotations

import json
import re
import sys
from datetime import datetime, timedelta
from pathlib import Path

HERE = Path(__file__).resolve().parent


def _load_outreach():
    """outreach.py lives beside this file (in the engine and in every vendored copy); import it by name when the
    folder is on sys.path, else by path, so a binding that imports this module under another name still works."""
    try:
        import outreach as oc  # noqa: F401
        return oc
    except ImportError:
        import importlib.util
        spec = importlib.util.spec_from_file_location("cog_outreach", HERE / "outreach.py")
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        return mod


_oc = _load_outreach()

SATISFIED, OPEN, UNKNOWN, MOOT = "satisfied", "open", "unknown", "moot"
VERDICTS = (SATISFIED, OPEN, UNKNOWN, MOOT)
_RANK = {MOOT: 3, UNKNOWN: 2, OPEN: 1, SATISFIED: 0}
MAX_AGE_HOURS = 36.0
TERMINAL_STATUSES = ("answered", "settled", "closed", "done", "cancelled", "invalid", "dismissed", "resolved")
_CMP = re.compile(r"^\s*([A-Za-z_][\w.-]*)\s*(>=|<=|==|=|>|<)\s*(-?\d+(?:\.\d+)?)\s*$")
_WORDS = {0: "no", 1: "one", 2: "two", 3: "three", 4: "four", 5: "five", 6: "six"}

_CHECKS: dict = {}
_QUANTITIES: dict = {}


# ------------------------------------------------------------------ registry
def register(name: str, fn) -> None:
    """A fact the pack can read: fn(record, now, **params) -> (verdict, evidence)."""
    _CHECKS[str(name)] = fn


def register_quantity(name: str, fn) -> None:
    """A number a precondition compares: fn(record, now) -> (number | None, evidence). None = unknown."""
    _QUANTITIES[str(name)] = fn


def _quantity(name: str):
    if name in _QUANTITIES:
        return _QUANTITIES[name], ""
    for k, fn in _QUANTITIES.items():
        if k.endswith("_") and name.startswith(k):
            return fn, name[len(k):]
    return None, ""


# ------------------------------------------------------------------ time
_REL = re.compile(r"^now(?:\s*([+-])\s*(\d+(?:\.\d+)?)\s*([mhd]))?$", re.I)


def _when(s, now: datetime | None = None) -> datetime | None:
    """A stamp -> datetime, or None. Besides the ISO forms outreach reads, the RELATIVE forms "now", "now-3d",
    "now-50h", "now+20m" so a replay fixture keeps its age whatever day it runs (the same forms provenance reads)."""
    if isinstance(s, str):
        m = _REL.match(s.strip())
        if m:
            base = now or datetime.now()
            if not m.group(1):
                return base
            n = float(m.group(2))
            u = m.group(3).lower()
            delta = timedelta(minutes=n) if u == "m" else timedelta(hours=n) if u == "h" else timedelta(days=n)
            return base + delta if m.group(1) == "+" else base - delta
    return _oc._when(s)


def _days(a, b) -> float | None:
    da, db = _when(a, b if isinstance(b, datetime) else None), _when(b)
    if da is None or db is None:
        return None
    return round((db - da).total_seconds() / 86400.0, 2)


def _n(n) -> str:
    try:
        return _WORDS.get(int(n), str(int(n)))
    except (TypeError, ValueError):
        return str(n)


def _ago(days) -> str:
    if days is None:
        return "at an unknown time"
    d = float(days)
    if d < 1 / 24:
        return "just now"
    if d < 1:
        return f"{int(round(d * 24))} h ago"
    return f"{int(round(d))} day{'s' if int(round(d)) != 1 else ''} ago"


def record_age_hours(record: dict, now: datetime) -> float | None:
    """Hours since the record was last read or written (its as_of), or None when it carries no stamp."""
    dt = _when((record or {}).get("as_of"), now)
    if dt is None:
        return None
    return round((now - dt).total_seconds() / 3600.0, 2)


def record_fresh(record: dict, now: datetime, max_age_hours: float = MAX_AGE_HOURS) -> tuple:
    """(True, '') when the record may be read for time facts; (False, why) when it is unstamped or stale."""
    h = record_age_hours(record, now)
    if h is None:
        return False, "the record carries no as_of (when it was last read) so its ages cannot be trusted"
    if h > float(max_age_hours):
        return False, f"the record was last read {_ago(h / 24.0)}, over its {int(max_age_hours)} h limit"
    return True, ""


# ------------------------------------------------------------------ the history, read as facts and events
def _asks(record: dict) -> list:
    """Every entry of the record's channel history, in the order the ledger keeps them (a list derived from an older
    row's counters has unstamped middle entries; sorting by stamp would move those to the front and break the run)."""
    return _oc.history(record if isinstance(record, dict) else {})


def _runs(ents: list) -> dict:
    """{channel: unanswered asks in a row at the end of that channel's entries} (a bounce is an unanswered try)."""
    out: dict = {}
    for e in ents:
        if e.get("result") == _oc.REPLIED:
            out[e["channel"]] = 0
        else:
            out[e["channel"]] = out.get(e["channel"], 0) + 1
    return {c: n for c, n in out.items() if n}


def events(record: dict) -> dict:
    """{event: ts}: the record's explicit `events`, then what the history implies and the writer did not stamp:
    ask (the latest ask), first_ask, reask (the latest ask of a run of two or more on one channel), reply (the
    latest reply), any (the latest entry), channel_change (the first ask on a new channel after a stall)."""
    record = record if isinstance(record, dict) else {}
    ev = {str(k): v for k, v in (record.get("events") or {}).items() if isinstance(record.get("events"), dict)}
    ents = _asks(record)
    if not ents:
        return ev
    ev.setdefault("any", ents[-1].get("ts") or "")
    ev.setdefault("ask", ents[-1].get("ts") or "")
    ev.setdefault("first_ask", ents[0].get("ts") or "")
    replies = [e for e in ents if e.get("result") == _oc.REPLIED]
    if replies:
        ev.setdefault("reply", replies[-1].get("ts") or "")
    # the latest ask that was the 2nd+ unanswered in a row on its channel
    run: dict = {}
    reask_ts, change_ts, stalled_on = None, None, set()
    for e in ents:
        ch = e.get("channel")
        if e.get("result") == _oc.REPLIED:
            run[ch] = 0
            continue
        run[ch] = run.get(ch, 0) + 1
        if run[ch] >= 2:
            reask_ts = e.get("ts") or ""
            stalled_on.add(ch)
        elif stalled_on and ch not in stalled_on and run[ch] == 1 and change_ts is None:
            change_ts = e.get("ts") or ""
    if reask_ts is not None:
        ev.setdefault("reask", reask_ts)
    if change_ts is not None:
        ev.setdefault("channel_change", change_ts)
    return ev


def _fact_asked_once(record: dict, now: datetime, **_) -> tuple:
    ents = _asks(record)
    if not ents:
        return OPEN, "no ask on the record yet"
    if not _runs(ents):
        return OPEN, "every ask on the record was answered"
    last = [e for e in ents if e.get("result") != _oc.REPLIED][-1]
    return SATISFIED, f"asked {_ago(_days(last.get('ts'), now)) if last.get('ts') else 'at an unknown time'} on {last.get('channel')}, no reply"


def _fact_asked_twice(record: dict, now: datetime, **_) -> tuple:
    ents = _asks(record)
    runs = _runs(ents)
    hit = [(c, n) for c, n in runs.items() if n >= 2]
    if not hit:
        if runs:
            c, n = next(iter(runs.items()))
            return OPEN, f"{_n(n)} unanswered ask on {c}; not asked twice there yet"
        return OPEN, "no unanswered ask on the record"
    c, n = max(hit, key=lambda x: x[1])
    return SATISFIED, f"{_n(n)} asks on {c} went unanswered"


def _fact_channel_changed(record: dict, now: datetime, **_) -> tuple:
    ev = events(record)
    if ev.get("channel_change"):
        ents = _asks(record)
        e = next((x for x in ents if (x.get("ts") or "") == ev["channel_change"]), None)
        ch = e.get("channel") if e else "another channel"
        return SATISFIED, f"asked again on {ch} {_ago(_days(ev['channel_change'], now))} after the stall, no reply"
    if "channel_changed" in _facts_on(record):
        return SATISFIED, "the record says the channel was changed"
    return OPEN, "no ask on another channel since the stall"


def _fact_escalated(record: dict, now: datetime, **_) -> tuple:
    ev = events(record)
    if ev.get("escalated") or "escalated" in _facts_on(record):
        ts = ev.get("escalated")
        return SATISFIED, "raised with someone else" + (f" {_ago(_days(ts, now))}" if ts and _when(ts) else "")
    return OPEN, "not raised with anyone else yet"


def _fact_answered(record: dict, now: datetime, **_) -> tuple:
    ents = _asks(record)
    if ents and ents[-1].get("result") == _oc.REPLIED:
        return MOOT, f"answered {_ago(_days(ents[-1].get('ts'), now))}: nothing to chase"
    if ents and not _runs(ents):
        return MOOT, "every ask was answered: nothing to chase"
    return OPEN, "not answered"


def _facts_on(record: dict) -> set:
    """Facts the writer asserted on the record (`facts` list, `state`)."""
    record = record if isinstance(record, dict) else {}
    out = {str(x) for x in (record.get("facts") or []) if x} if isinstance(record.get("facts"), (list, tuple, set)) else set()
    if record.get("state"):
        out.add(str(record["state"]))
    return out


_EVENT_WORDS = {"any": "last contact", "ask": "last ask", "reask": "second ask", "first_ask": "first ask",
                "channel_change": "channel change", "reply": "reply", "escalated": "escalation"}


def _event_words(key: str) -> str:
    return _EVENT_WORDS.get(key, key.replace("_", " "))


def _days_since(record: dict, now: datetime, event: str = "") -> tuple:
    ev = events(record)
    key = event or "any"
    ts = ev.get(key)
    if ts is None:
        return None, f"no {_event_words(key)} on the record"
    d = _days(ts, now)
    if d is None:
        return None, f"the {_event_words(key)} carries no time"
    return d, f"{_event_words(key)} {_ago(d)}"


register("asked_once", _fact_asked_once)
register("asked_twice", _fact_asked_twice)
register("channel_changed", _fact_channel_changed)
register("escalated", _fact_escalated)
register("answered", _fact_answered)
register_quantity("days_since_", _days_since)
register_quantity("days_since", lambda r, now: _days_since(r, now, ""))


# ------------------------------------------------------------------ one precondition
def _compare(v: float, op: str, n: float) -> bool:
    return {">=": v >= n, "<=": v <= n, ">": v > n, "<": v < n, "==": v == n, "=": v == n}[op]


def check(precond, record: dict, now: datetime | None = None, max_age_hours: float = MAX_AGE_HOURS,
          terminal_statuses=TERMINAL_STATUSES) -> tuple:
    """One precondition -> (verdict, evidence). A string: "<fact>" (registered check, or a fact the record asserts),
    "<quantity><op>N" (registered quantity). A dict: {"check": <name>, ...params}. Time facts on an unstamped or
    stale record read UNKNOWN; a record in a terminal status reads MOOT whatever the precondition."""
    now = now or datetime.now()
    record = record if isinstance(record, dict) else {}
    st = str(record.get("status") or "").lower()
    if st and st in {str(s).lower() for s in (terminal_statuses or ())}:
        return MOOT, f"the record is {st}"
    if isinstance(precond, dict):
        spec = dict(precond)
        name = str(spec.pop("check", "") or "")
        fn = _CHECKS.get(name)
        if not fn:
            return UNKNOWN, f"no reader for {name!r}"
        try:
            return _norm(fn(record, now, **spec))
        except Exception as e:  # noqa: BLE001
            return UNKNOWN, f"{name} failed: {type(e).__name__}"
    s = str(precond or "").strip()
    if not s:
        return SATISFIED, "no condition"
    m = _CMP.match(s)
    if m:
        name, op, n = m.group(1), m.group(2), float(m.group(3))
        fn, arg = _quantity(name)
        if not fn:
            return UNKNOWN, f"no reader for {name!r}"
        fresh, why = record_fresh(record, now, max_age_hours)
        if not fresh:
            return UNKNOWN, why
        try:
            val, ev = fn(record, now, arg) if arg else fn(record, now)
        except Exception as e:  # noqa: BLE001
            return UNKNOWN, f"{name} failed: {type(e).__name__}"
        if val is None:
            return UNKNOWN, ev
        ok = _compare(float(val), op, n)
        return (SATISFIED if ok else OPEN), f"{ev} ({'meets' if ok else 'short of'} {name} {op} {n:g})"
    fn = _CHECKS.get(s)
    if fn:
        try:
            v, ev = _norm(fn(record, now))
        except Exception as e:  # noqa: BLE001
            return UNKNOWN, f"{s} failed: {type(e).__name__}"
        if v == OPEN and s in _facts_on(record):
            return SATISFIED, f"the record says {s}"
        return v, ev
    if s in _facts_on(record):
        return SATISFIED, f"the record says {s}"
    return UNKNOWN, f"no reader for {s!r}"


def _norm(res) -> tuple:
    if isinstance(res, tuple) and len(res) == 2 and res[0] in VERDICTS:
        return res[0], str(res[1] or "")
    if res is True:
        return SATISFIED, ""
    if res is False:
        return OPEN, ""
    return UNKNOWN, "the reader gave no verdict"


# ------------------------------------------------------------------ a step
def evaluate(step: dict, record: dict, now: datetime | None = None, max_age_hours: float = MAX_AGE_HOURS,
             terminal_statuses=TERMINAL_STATUSES) -> tuple:
    """Every precondition of a step -> (verdict, [evidence]). moot > unknown > open > satisfied. When the step's
    effect already holds on the record the step is done by record: (satisfied, ['already <effect>: ...'])."""
    now = now or datetime.now()
    step = step if isinstance(step, dict) else {}
    eff = str(step.get("effect") or "")
    if eff:
        v, ev = check(eff, record, now, max_age_hours, terminal_statuses)
        if v == MOOT:
            return MOOT, [ev]
        if v == SATISFIED:
            return SATISFIED, [f"already {eff.replace('_', ' ')}: {ev}"]
    verdict, out = SATISFIED, []
    for p in (step.get("precond") or []):
        v, ev = check(p, record, now, max_age_hours, terminal_statuses)
        out.append(ev if ev else str(p))
        if _RANK[v] > _RANK[verdict]:
            verdict = v
    if not step.get("precond"):
        out.append("no precondition")
    return verdict, out


# ------------------------------------------------------------------ the chain
def load_chain(path) -> dict:
    d = json.loads(Path(path).read_text(encoding="utf-8-sig"))
    probs = validate(d)
    if probs:
        raise ValueError("; ".join(probs))
    return d


def validate(chain) -> list:
    """Problems with a chain as data: [] when it is well formed."""
    probs = []
    if not isinstance(chain, dict):
        return ["the chain is not an object"]
    if not chain.get("chain"):
        probs.append("no chain name")
    if not chain.get("initial"):
        probs.append("no initial state")
    ops = chain.get("operators")
    if not isinstance(ops, list) or not ops:
        return probs + ["no operators"]
    seen_id, seen_eff = set(), set()
    for i, op in enumerate(ops):
        if not isinstance(op, dict) or not op.get("id"):
            probs.append(f"operator {i} has no id")
            continue
        if op["id"] in seen_id:
            probs.append(f"operator id {op['id']!r} repeats")
        seen_id.add(op["id"])
        if not isinstance(op.get("precond", []), list):
            probs.append(f"{op['id']}: precond is not a list")
        if not op.get("effect"):
            probs.append(f"{op['id']}: no effect")
        elif op["effect"] in seen_eff:
            probs.append(f"{op['id']}: effect {op['effect']!r} repeats")
        seen_eff.add(op.get("effect"))
    return probs


def explain(chain: dict, record: dict, now: datetime | None = None, max_age_hours: float = MAX_AGE_HOURS,
            terminal_statuses=TERMINAL_STATUSES) -> dict:
    """The chain's reading of one record: {operator (dict | None), verdict, state, facts, why: [plain words], line}.
    Walks the operators in order; the first whose effect does not hold yet decides: satisfied -> that operator is
    due; open -> it is not due yet (nothing fires); unknown -> nothing fires, and the line says why; moot -> the
    matter is closed. The chain never advances on UNKNOWN."""
    now = now or datetime.now()
    chain = chain if isinstance(chain, dict) else {}
    record = record if isinstance(record, dict) else {}
    name = str(chain.get("chain") or "chain")
    state, done = str(chain.get("initial") or ""), []
    # the matter is closed: a reply after the last ask, or a terminal status (answered, settled, cancelled ...)
    v0, ev0 = check("answered", record, now, max_age_hours, terminal_statuses)
    if v0 == MOOT:
        return {"operator": None, "verdict": MOOT, "state": state, "facts": done, "why": [ev0],
                "line": f"{name} chain: nothing to do, {ev0}"}
    for op in (chain.get("operators") or []):
        if not isinstance(op, dict):
            continue
        eff = str(op.get("effect") or "")
        v_eff, ev_eff = check(eff, record, now, max_age_hours, terminal_statuses) if eff else (OPEN, "")
        if v_eff == MOOT:
            return {"operator": None, "verdict": MOOT, "state": state, "facts": done, "why": [ev_eff],
                    "line": f"{name} chain: nothing to do, {ev_eff}"}
        if v_eff == SATISFIED:
            state = eff
            done.append(f"{eff.replace('_', ' ')}: {ev_eff}")
            continue
        verdict, why = evaluate(op, record, now, max_age_hours, terminal_statuses)
        oid = str(op.get("id") or "").replace("_", " ")
        if verdict == SATISFIED:
            line = f"{name} chain says: {oid} is due" + (" (" + "; ".join(x for x in why if x and x != "no precondition") + ")" if any(x != "no precondition" for x in why) else "") + "."
            return {"operator": op, "verdict": SATISFIED, "state": state, "facts": done, "why": why, "line": line}
        if verdict == MOOT:
            return {"operator": None, "verdict": MOOT, "state": state, "facts": done, "why": why,
                    "line": f"{name} chain: nothing to do, " + "; ".join(why)}
        if verdict == UNKNOWN:
            return {"operator": None, "verdict": UNKNOWN, "state": state, "facts": done, "why": why,
                    "line": f"{name} chain: UNKNOWN whether {oid} is due, " + "; ".join(why) + ". Nothing advances until the record is read again."}
        return {"operator": None, "verdict": OPEN, "state": state, "facts": done, "why": why,
                "line": f"{name} chain says: {oid} is not due yet (" + "; ".join(why) + ")."}
    return {"operator": None, "verdict": SATISFIED, "state": state, "facts": done, "why": ["every step of the chain is done"],
            "line": f"{name} chain: every step is done; what is left is a decision, not a step."}


def applicable(chain: dict, record: dict, now: datetime | None = None, max_age_hours: float = MAX_AGE_HOURS,
               terminal_statuses=TERMINAL_STATUSES):
    """The next operator the chain would take for this record, or None (not due yet, unknown, moot, or finished)."""
    return explain(chain, record, now, max_age_hours, terminal_statuses).get("operator")


def line(chain: dict, record: dict, now: datetime | None = None, **kw) -> str:
    """The one plain-words line for a prompt head or a card."""
    return explain(chain, record, now, **kw).get("line") or ""


def main(argv=None) -> int:
    a = list(argv if argv is not None else sys.argv[1:])
    if len(a) < 2 or a[0] != "explain":
        print(__doc__)
        return 2
    now = None
    if "--now" in a:                       # a fixed clock (tests, a replay): "YYYY-MM-DDTHH:MM"
        i = a.index("--now")
        try:
            now = datetime.fromisoformat(a[i + 1])
        except (IndexError, ValueError):
            print("--now needs YYYY-MM-DDTHH:MM")
            return 2
        del a[i:i + 2]
    try:
        chain = load_chain(a[1])
        rec = json.loads(Path(a[2]).read_text(encoding="utf-8-sig")) if len(a) > 2 else {}
    except (OSError, ValueError) as e:
        print(f"cannot read: {e}")
        return 1
    print(json.dumps(explain(chain, rec, now), indent=1, default=str))
    return 0


if __name__ == "__main__":
    sys.exit(main())

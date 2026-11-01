"""outreach.py - every ask an agent puts to a person carries the channel it went on and whether it was answered;
after `min_tries` unanswered asks on ONE channel the item reads as STALLED. That verdict is context the model reads
("two asks to <name> on chat went unanswered; change channel or ask <manager>?"), never an action: a change of
channel is a message to someone, and a message to anyone is a card for the owner (the send gate decides).

Seed A of the agent-intelligence plan (2026-10-31, "goals", RESHAPE verdict): no goal store beside the rows that
exist (a follow-up ledger, a chase ledger, a case file), no ladder that a count advances by itself. The failure it
names: an agent asks the same person the same question on the same channel a third time, because nothing told it
that two asks already went unanswered there; the record of the asks was one integer. Two fields on the row fix it:

  channel_history: [{"channel": "chat", "ts": "2026-10-30T09:00", "result": "no_reply"}, ...]
                   appended by the code at its existing send and reply paths (result = no_reply | replied | bounced)
  route_tries:     {"chat": 2}     DERIVED: unanswered asks in a row per channel, since that channel's last reply
  needs_channel_review: true       DERIVED: stalled_outreach(item): some channel's route_tries >= min_tries

  record_send(item, channel, ts, result)    an ask went out on a channel (default result no_reply: open)
  record_reply(item, ts, channel)           the person answered: open asks up to ts (on that channel, or all) become replied
  record_bounce(item, channel, ts)          the ask never reached them (a bounce counts as an unanswered try)
  history(item)                             the row's channel_history, or one DERIVED from the counters an older row
                                            carries (sends / unanswered / first_sent / last_sent / last_reply_ts) so the
                                            rows written before this existed read back the same way (entries derived=True)
  route_tries(item) / stalled_channels(item, min_tries) / stalled_outreach(item, min_tries) -> bool
  refresh(item, min_tries)                  write the two derived fields on the row
  review_line(item, who, asker, min_tries)  the plain-words line for a prompt head; '' when nothing is stalled
  merge(items)                              one view of several rows about the same ask (a ledger that closes a row per
                                            check-in keeps the asks to one person in one thread on several rows)
  shadow(items, min_tries)                  the read-back the plan's shadow test asks for: how many items are stalled

Pure functions over dicts; no I/O except the CLI. No organization, person, channel product or system of record is
named here: channel values are whatever the binding passes ("chat", "dm", "text", "email", ...).

  python outreach.py shadow <state.json> [--min-tries 2] [--channel-key source=hygiene:dm]
      <state.json> holds {"items": {key: row}} or {"items": [row, ...]} or [row, ...]; prints the shadow report
"""
from __future__ import annotations

import json
import sys
from datetime import datetime

NO_REPLY, REPLIED, BOUNCED = "no_reply", "replied", "bounced"
RESULTS = (NO_REPLY, REPLIED, BOUNCED)
DEFAULT_CHANNEL = "chat"
MIN_TRIES = 2
# the counters an older row carries, when it has no channel_history yet (a binding may rename them with configure)
LEGACY_KEYS = {"sends": "sends", "unanswered": "unanswered", "first_ts": "first_sent", "last_ts": "last_sent",
               "reply_ts": "last_reply_ts"}
_WORDS = {1: "one", 2: "two", 3: "three", 4: "four", 5: "five", 6: "six"}


def configure(legacy_keys: dict | None = None, default_channel: str = "") -> None:
    """A binding's names for the legacy counters / its default channel."""
    global DEFAULT_CHANNEL
    if legacy_keys:
        LEGACY_KEYS.update({k: v for k, v in legacy_keys.items() if k in LEGACY_KEYS})
    if default_channel:
        DEFAULT_CHANNEL = default_channel


# ------------------------------------------------------------------ time
def _when(s):
    """A stamp -> datetime (naive, the zone suffix dropped) or None; stamps are compared only within one row."""
    if s is None or s == "":
        return None
    if isinstance(s, datetime):
        return s.replace(tzinfo=None)
    if isinstance(s, (int, float)):
        try:
            return datetime.fromtimestamp(float(s))
        except (OverflowError, OSError, ValueError):
            return None
    t = str(s).strip().replace("Z", "")
    for cut in (None, 19, 16, 10):
        try:
            return datetime.fromisoformat(t if cut is None else t[:cut])
        except ValueError:
            continue
    return None


def _stamp(ts) -> str:
    return ts.strftime("%Y-%m-%dT%H:%M:%S") if isinstance(ts, datetime) else str(ts or "")


def _not_after(a, b) -> bool:
    """a <= b when both parse; an unparseable side never blocks (an open ask with no stamp is answered by any reply)."""
    da, db = _when(a), _when(b)
    if da is None or db is None:
        return True
    return da <= db


# ------------------------------------------------------------------ the list
def _entries(raw) -> list:
    out = []
    for e in (raw if isinstance(raw, list) else []):
        if not isinstance(e, dict):
            continue
        ch = str(e.get("channel") or DEFAULT_CHANNEL)
        res = str(e.get("result") or NO_REPLY)
        if res not in RESULTS:
            res = NO_REPLY
        row = {"channel": ch, "ts": str(e.get("ts") or ""), "result": res}
        if e.get("derived"):
            row["derived"] = True
        out.append(row)
    return out


def derive_history(channel: str, sends, unanswered=None, first_ts="", last_ts="", reply_ts="") -> list:
    """The channel_history an older row WOULD have had: `sends` asks on one channel, the last `unanswered` of them
    open. unanswered=None is read from the stamps the way the chase ledgers do: a reply after the last ask -> 0;
    no reply ever -> every send; a reply before the last ask -> 1 (at least the last ask is open)."""
    try:
        n = max(0, int(sends or 0))
    except (TypeError, ValueError):
        n = 0
    if unanswered is None:
        r, l = _when(reply_ts), _when(last_ts)
        if r and (not l or r >= l):
            u = 0
        else:
            u = n if not r else 1
    else:
        try:
            u = int(unanswered or 0)
        except (TypeError, ValueError):
            u = 0
    u = max(0, min(n, u))
    out = []
    for i in range(n):
        ts = first_ts if i == 0 else (last_ts if i == n - 1 else "")
        out.append({"channel": channel or DEFAULT_CHANNEL, "ts": str(ts or ""),
                    "result": NO_REPLY if i >= n - u else REPLIED, "derived": True})
    return out


def legacy_counts(item: dict, keys: dict | None = None) -> dict | None:
    """{sends, unanswered, first_ts, last_ts, reply_ts} from the counters on an older row, or None when it has none."""
    k = dict(LEGACY_KEYS, **(keys or {}))
    if not isinstance(item, dict) or k["sends"] not in item:
        return None
    return {"sends": item.get(k["sends"]), "unanswered": item.get(k["unanswered"]) if k["unanswered"] in item else None,
            "first_ts": item.get(k["first_ts"]) or "", "last_ts": item.get(k["last_ts"]) or "",
            "reply_ts": item.get(k["reply_ts"]) or ""}


def history(item: dict, channel: str = "", keys: dict | None = None) -> list:
    """The row's channel_history (a copy), or one derived from its legacy counters when it carries none. A reply stamp
    on the row newer than the last open entry (a tap or a reply path that never touched the list) closes those
    entries here, so the reading is right even when the writer was not this module."""
    if not isinstance(item, dict):
        return []
    ents = _entries(item.get("channel_history"))
    if not ents:
        lc = legacy_counts(item, keys)
        if lc:
            ents = derive_history(channel or DEFAULT_CHANNEL, lc["sends"], lc["unanswered"], lc["first_ts"], lc["last_ts"], lc["reply_ts"])
        return ents
    reply_ts = item.get(dict(LEGACY_KEYS, **(keys or {}))["reply_ts"])
    if reply_ts:
        _close(ents, reply_ts, None)
    return ents


def _close(ents: list, ts, channel) -> int:
    n = 0
    for e in ents:
        if e["result"] != NO_REPLY or (channel and e["channel"] != channel):
            continue
        if ts is None or ts == "" or _not_after(e["ts"], ts):
            e["result"] = REPLIED
            n += 1
    return n


# ------------------------------------------------------------------ the verdict
def route_tries(item: dict, channel: str = "", keys: dict | None = None) -> dict:
    """{channel: unanswered asks in a row on it}: the trailing run of entries on each channel not answered (a bounce
    is a try that failed), counted from that channel's last reply."""
    out: dict = {}
    for e in history(item, channel, keys):
        if e["result"] == REPLIED:
            out[e["channel"]] = 0
        else:
            out[e["channel"]] = out.get(e["channel"], 0) + 1
    return {ch: n for ch, n in out.items() if n}


def stalled_channels(item: dict, min_tries: int = MIN_TRIES, channel: str = "", keys: dict | None = None) -> list:
    rt = route_tries(item, channel, keys)
    return [ch for ch, n in rt.items() if n >= max(1, int(min_tries))]


def stalled_outreach(item: dict, min_tries: int = MIN_TRIES, channel: str = "", keys: dict | None = None) -> bool:
    """True when some channel carries min_tries or more unanswered asks in a row. A reading, never a trigger."""
    return bool(stalled_channels(item, min_tries, channel, keys))


def refresh(item: dict, min_tries: int = MIN_TRIES, channel: str = "", keys: dict | None = None) -> dict:
    """Write the two derived fields on the row (route_tries, needs_channel_review). The list itself is left as it is:
    an older row keeps its counters until its next send appends a real entry."""
    if not isinstance(item, dict):
        return item
    item["route_tries"] = route_tries(item, channel, keys)
    item["needs_channel_review"] = stalled_outreach(item, min_tries, channel, keys)
    return item


# ------------------------------------------------------------------ the writers (called at the code's existing send / reply paths)
def record_send(item: dict, channel: str = "", ts=None, result: str = NO_REPLY, min_tries: int = MIN_TRIES,
                keys: dict | None = None) -> dict:
    """An ask went out. The row's list starts from its derived reading when this is its first real entry, so the
    older asks are not forgotten the moment the new field appears."""
    if not isinstance(item, dict):
        return item
    ents = history(item, channel, keys)
    ents.append({"channel": channel or DEFAULT_CHANNEL, "ts": _stamp(ts), "result": result if result in RESULTS else NO_REPLY})
    item["channel_history"] = ents[-60:]
    return refresh(item, min_tries, channel, keys)


def record_reply(item: dict, ts=None, channel: str = "", min_tries: int = MIN_TRIES, keys: dict | None = None) -> dict:
    """The person answered: every open ask up to ts (on that channel, or on every channel) is replied. Idempotent."""
    if not isinstance(item, dict):
        return item
    ents = history(item, "", keys)
    if ents:
        _close(ents, _stamp(ts), channel or None)
        item["channel_history"] = ents[-60:]
    return refresh(item, min_tries, "", keys)


def record_bounce(item: dict, channel: str = "", ts=None, min_tries: int = MIN_TRIES, keys: dict | None = None) -> dict:
    return record_send(item, channel, ts, BOUNCED, min_tries, keys)


# ------------------------------------------------------------------ reading several rows as one
def merge(items, channel: str = "", keys: dict | None = None) -> dict:
    """One row whose channel_history is every row's, in time order (stable for unstamped entries). For ledgers that
    close a row per check-in: the asks to one person in one thread live on several rows, and the stall is theirs
    together."""
    ents = []
    for it in (items.values() if isinstance(items, dict) else (items or [])):
        ents += history(it, channel, keys)
    ents.sort(key=lambda e: (_when(e["ts"]) or datetime.min))
    return {"channel_history": ents}


# ------------------------------------------------------------------ words
def _n(n: int) -> str:
    return _WORDS.get(int(n), str(n))


def review_line(item: dict, who: str = "", asker: str = "", min_tries: int = MIN_TRIES, channel: str = "",
                keys: dict | None = None) -> str:
    """'two asks to <who> on chat went unanswered; change channel or ask <asker>?' -- '' when nothing is stalled.
    Plain words for a prompt head or a card; the model decides what to do with it."""
    rt = route_tries(item, channel, keys)
    stalled = [(ch, n) for ch, n in rt.items() if n >= max(1, int(min_tries))]
    if not stalled:
        return ""
    who = str(who or "").strip()
    asker = str(asker or "").strip()
    parts = []
    for ch, n in stalled:
        parts.append(f"{_n(n)} ask{'s' if n != 1 else ''}" + (f" to {who}" if who else "") + f" on {ch} went unanswered")
    tail = ("change channel or raise it with someone else?" if not asker or asker.lower() == who.lower()
            else f"change channel or ask {asker}?")
    return "; ".join(parts) + "; " + tail


# ------------------------------------------------------------------ the shadow read-back
def shadow(items, min_tries: int = MIN_TRIES, channel_of=None, keys: dict | None = None, open_statuses=()) -> dict:
    """How many rows already carry min_tries+ unanswered asks on one channel. channel_of(row) -> channel name for a
    row without a list (default DEFAULT_CHANNEL); open_statuses limits the 'open' count to rows in those statuses."""
    rows = list(items.items()) if isinstance(items, dict) else [(str(i), it) for i, it in enumerate(items or [])]
    rep = {"items": 0, "with_history": 0, "stalled": 0, "stalled_open": 0, "by_channel": {}, "by_tries": {}, "stalled_keys": []}
    for key, it in rows:
        if not isinstance(it, dict):
            continue
        rep["items"] += 1
        ch = str((channel_of(it) if channel_of else "") or DEFAULT_CHANNEL)
        if _entries(it.get("channel_history")):
            rep["with_history"] += 1
        rt = route_tries(it, ch, keys)
        hit = [(c, n) for c, n in rt.items() if n >= max(1, int(min_tries))]
        if not hit:
            continue
        rep["stalled"] += 1
        if not open_statuses or str(it.get("status") or "") in open_statuses:
            rep["stalled_open"] += 1
        for c, n in hit:
            rep["by_channel"][c] = rep["by_channel"].get(c, 0) + 1
            rep["by_tries"][str(n)] = rep["by_tries"].get(str(n), 0) + 1
        rep["stalled_keys"].append(key)
    return rep


def main(argv=None) -> int:
    a = list(argv if argv is not None else sys.argv[1:])
    if not a or a[0] != "shadow" or len(a) < 2:
        print(__doc__)
        return 2
    min_tries, rule = MIN_TRIES, ""
    i = 2
    while i < len(a):
        if a[i] == "--min-tries" and i + 1 < len(a):
            min_tries, i = int(a[i + 1]), i + 2
        elif a[i] == "--channel-key" and i + 1 < len(a):
            rule, i = a[i + 1], i + 2
        else:
            i += 1
    try:
        d = json.loads(open(a[1], encoding="utf-8-sig").read())
    except (OSError, ValueError) as e:
        print(f"cannot read {a[1]}: {e}")
        return 1
    items = d.get("items") if isinstance(d, dict) and "items" in d else d
    chan = None
    if rule and "=" in rule and ":" in rule:        # field=value:channel  (a row whose field has that value went on that channel)
        fv, ch = rule.rsplit(":", 1)
        f, v = fv.split("=", 1)
        chan = (lambda it: ch if str(it.get(f) or "") == v else DEFAULT_CHANNEL)
    rep = shadow(items, min_tries, chan)
    print(json.dumps(rep, indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main())

"""Test sends close quietly (2026-10-25: "He said you would stop sending these messages").

A send whose recipient is a test or placeholder (an example.com / .test / .invalid address, a 555-01xx number) or whose
body is a bare test word ("s", "hi 0", "test") is never real work. When one fails it gets no "Did not go" card, it never
trips a channel's "paused" card, and it is not retried every 10 minutes:

  * outcomes.ask_owner / settle close its outcome row quietly (is_test_row);
  * tickets.add_many drops its executor-exception ticket;
  * sweep() (run by outcomes.py settle, every spine pass) cancels its queued executor tickets, pulls it back out of the
    phone outbox while the phone has not fetched it, closes every open card about it, and resets a channel breaker
    that only test sends tripped.

Zero-LLM, stdlib only, never raises from the public helpers.
  python assistant/scripts/test_sends.py [--dry-run]
"""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

TEST_DOMAIN = re.compile(r"@(?:[\w-]+\.)*(?:example\.(?:com|net|org)|[\w-]+\.(?:test|example|invalid|localhost))$", re.I)
TEST_PHONE = re.compile(r"^1?\d{3}55501\d{2}$")          # the fictional 555-0100..0199 block
TEST_BODY = re.compile(r"(?i)\s*(?:s|x|test(?:ing)?(?: \d+)?|(?:hi|hey|hello) \d+)\s*[.!]?\s*")


def is_test_handle(h) -> bool:
    h = str(h or "").strip()
    if not h:
        return False
    if "@" in h:
        return bool(TEST_DOMAIN.search(h))
    return bool(TEST_PHONE.fullmatch(re.sub(r"\D", "", h)))


def is_test_payload(p) -> bool:
    """A send payload to a test/placeholder recipient, or with a bare test word for its body."""
    if not isinstance(p, dict):
        return False
    to = p.get("to") or p.get("email") or ""
    handles = [x for x in (to if isinstance(to, (list, tuple)) else [to]) if str(x or "").strip()]
    if handles and all(is_test_handle(x) for x in handles):
        return True
    body = p.get("body") if p.get("body") is not None else p.get("text")
    return isinstance(body, str) and bool(body.strip()) and bool(TEST_BODY.fullmatch(body))


def _tickets() -> dict:
    try:
        import exec_store as es
        return es.fold()
    except Exception:  # noqa: BLE001
        return {}


def is_test_row(row, tickets=None) -> bool:
    """An outcome-ledger row (outcomes.py) about a test send. Never raises."""
    try:
        if not isinstance(row, dict):
            return False
        t = (tickets if tickets is not None else _tickets()).get(str(row.get("ref") or "")) or {}
        if t:
            return is_test_payload(t.get("payload") or {})
        what = str(row.get("what") or "")
        return row.get("kind") in ("imessage_send", "email_send") and bool(what.strip()) and bool(TEST_BODY.fullmatch(what))
    except Exception:  # noqa: BLE001
        return False


def is_test_ticket(t) -> bool:
    return isinstance(t, dict) and is_test_payload(t.get("payload") or {})


def _card_is_test(q: dict, rows: dict, tickets: dict) -> bool:
    ex = q.get("exec") if isinstance(q.get("exec"), dict) else {}
    if ex and is_test_payload(ex.get("payload") or {}):
        return True
    oid = str(q.get("outcome_id") or "")
    return bool(oid) and is_test_row(rows.get(oid) or {}, tickets)


def sweep(dry: bool = False) -> dict:
    """Close every trace of a test send. -> counts. Never raises."""
    out = {"cancelled": 0, "recalled": 0, "rows": 0, "cards": 0, "breakers": 0}
    try:
        import exec_store as es
    except Exception:  # noqa: BLE001
        return out
    tickets = tickets0 = _tickets()
    for t in list(tickets.values()):
        if t.get("status") != "queued" or not is_test_ticket(t):
            continue
        out["cancelled"] += 1
        if not dry:
            try:
                es.set_status(t["id"], "cancelled", "a test send: closed quietly, never retried")
            except Exception:  # noqa: BLE001
                out["cancelled"] -= 1
    # the phone outbox: a test text still waiting for the phone is pulled back (phone_bridge.recall)
    try:
        import phone_bridge
        from alib import load_json
        box = load_json(phone_bridge.OUTBOX, {"items": []}) or {"items": []}
        for it in box.get("items") or []:
            if it.get("status") != "ready" or not (is_test_handle(it.get("to")) or is_test_payload(it)):
                continue
            out["recalled"] += 1
            if not dry and it.get("ticket") and phone_bridge.recall(str(it["ticket"])):
                try:
                    es.mark_recalled(str(it["ticket"]), "a test send: pulled from the phone outbox before the phone fetched it")
                except Exception:  # noqa: BLE001
                    pass
    except Exception:  # noqa: BLE001
        pass
    # outcome rows about test sends that wait on the owner: closed quietly (one still on its normal path settles as usual)
    rows = {}
    try:
        import outcomes
        rows = outcomes.fold()
        for r in list(rows.values()):
            if r.get("state") != "unverified" or not is_test_row(r, tickets):
                continue
            out["rows"] += 1
            if not dry:
                outcomes.set_state(r["id"], "abandoned", {}, "a test send: closed quietly", escalation="none")
    except Exception:  # noqa: BLE001
        pass
    # open cards: a test send's own card, its "Did not go" card, a "paused" card only test sends tripped
    tickets = _tickets() if not dry else tickets
    try:
        import cards
        for c in cards._live():
            if c.get("status") != "open":
                continue
            q = (c.get("questions") or [{}])[0]
            lane = str(q.get("breaker_lane") or "")
            if lane:
                # judged on the queue as it stood before this sweep cancelled the test tickets
                failing = [t for t in tickets0.values() if str(t.get("lane") or "") == lane and t.get("status") == "queued"]
                if not failing or any(not is_test_ticket(t) for t in failing):
                    continue
                if not dry:
                    import drain
                    if drain.breaker_tripped(lane):
                        drain.breaker_reset(lane, by="test_sends")
                        out["breakers"] += 1
                    cards.close_card(c["id"], "only test sends tripped it; the channel is open again")
                out["cards"] += 1
                continue
            if _card_is_test(q, rows, tickets):
                out["cards"] += 1
                if not dry:
                    cards.close_card(c["id"], "a test send: closed quietly")
    except Exception:  # noqa: BLE001
        pass
    return out


def main() -> int:
    r = sweep(dry="--dry-run" in sys.argv)
    print("TEST_SENDS " + json.dumps(r))
    return 0


if __name__ == "__main__":
    sys.exit(main())

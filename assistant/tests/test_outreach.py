"""Offline tests for assistant/agent/outreach.py (Seed A, goals: channel_history + needs_channel_review on the rows
that exist). The writers append at the existing send / reply paths; route_tries is derived per channel; two
unanswered asks on one channel read as stalled; a reply resets the channel; a bounce counts as a failed try; older
rows with only counters read back the same way; several rows about one ask merge; the review line is plain words
and empty when nothing is stalled; the shadow read-back counts; and the engine file names no organization.
   python assistant/tests/test_outreach.py
"""
import contextlib
import io
import json
import sys
import tempfile
from datetime import datetime
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(ROOT / "agent"))
import outreach as oc  # noqa: E402

N = 0


def ok(cond, msg):
    global N
    N += 1
    if not cond:
        print(f"FAIL {N}: {msg}")
        sys.exit(1)
    print(f"ok {N}: {msg}")


# --- a fresh row: nothing stalled
it = {"id": "f-1", "status": "pending"}
ok(oc.history(it) == [] and oc.route_tries(it) == {} and not oc.stalled_outreach(it), "a row with no asks is not stalled")
ok(oc.review_line(it, "a person", "the owner") == "", "no review line when nothing is stalled")

# --- two sends on one channel, no reply -> stalled; the derived fields are on the row
oc.record_send(it, "chat", "2026-10-30T09:00")
ok(it["route_tries"] == {"chat": 1} and it["needs_channel_review"] is False, "one unanswered ask: tries 1, no review")
oc.record_send(it, "chat", datetime(2026, 10, 31, 9, 0))
ok(it["channel_history"][1]["ts"] == "2026-10-31T09:00:00", "a datetime stamp is written as ISO")
ok(it["route_tries"] == {"chat": 2} and it["needs_channel_review"] is True, "two unanswered asks on chat: needs_channel_review")
ok(oc.stalled_outreach(it) and oc.stalled_channels(it) == ["chat"], "stalled_outreach / stalled_channels agree")
ok(not oc.stalled_outreach(it, min_tries=3), "min_tries=3 is not reached by two asks")
line = oc.review_line(it, who="a person", asker="the owner")
ok(line == "two asks to a person on chat went unanswered; change channel or ask the owner?", f"review line: {line}")
ok(oc.review_line(it, who="the owner", asker="the owner").endswith("raise it with someone else?"),
   "when the asker is the one not answering the line asks for another reader")
ok(oc.review_line(it).startswith("two asks on chat went unanswered"), "review line without a name")

# --- a third ask on ANOTHER channel does not count against chat, and the new channel is not stalled
oc.record_send(it, "text", "2026-10-31T12:00")
ok(it["route_tries"] == {"chat": 2, "text": 1} and it["needs_channel_review"] is True, "a try on a second channel is its own count")

# --- a reply closes the open asks and resets every channel
oc.record_reply(it, "2026-10-31T13:00")
ok(all(e["result"] == "replied" for e in it["channel_history"]), "a reply marks every open ask replied")
ok(it["route_tries"] == {} and it["needs_channel_review"] is False, "after a reply nothing is stalled")
oc.record_send(it, "chat", "2026-11-01T09:00")
ok(it["route_tries"] == {"chat": 1}, "the count restarts after the reply")

# --- a reply on one channel only
it2 = {}
oc.record_send(it2, "chat", "2026-10-30T09:00")
oc.record_send(it2, "email", "2026-10-30T09:05")
oc.record_send(it2, "chat", "2026-10-31T09:00")
oc.record_reply(it2, "2026-10-31T10:00", channel="email")
ok(it2["route_tries"] == {"chat": 2} and it2["needs_channel_review"], "a reply on email leaves chat's two asks open")

# --- a reply stamped BEFORE the second ask closes only the first
it3 = {}
oc.record_send(it3, "chat", "2026-10-30T09:00")
oc.record_send(it3, "chat", "2026-10-31T09:00")
oc.record_reply(it3, "2026-10-30T15:00")
ok([e["result"] for e in it3["channel_history"]] == ["replied", "no_reply"] and it3["route_tries"] == {"chat": 1},
   "a reply between two asks answers only the earlier one")

# --- a bounce is a failed try
it4 = {}
oc.record_bounce(it4, "email", "2026-10-30T09:00")
oc.record_send(it4, "email", "2026-10-31T09:00")
ok(it4["route_tries"] == {"email": 2} and it4["needs_channel_review"], "a bounce plus an unanswered ask on email is stalled")

# --- older rows: counters only (the chase-ledger shape), read the same way
old = {"sends": 3, "first_sent": "2026-07-15T15:30:36Z", "last_sent": "2026-07-30T15:00:30Z", "status": "awaiting"}
h = oc.history(old)
ok(len(h) == 3 and all(e["derived"] and e["result"] == "no_reply" for e in h), "3 sends, never a reply -> three open derived asks")
ok(h[0]["ts"] == "2026-07-15T15:30:36Z" and h[-1]["ts"] == "2026-07-30T15:00:30Z" and h[1]["ts"] == "", "derived stamps: first, blank, last")
ok(oc.route_tries(old) == {"chat": 3} and oc.stalled_outreach(old), "a legacy row with three unanswered sends is stalled")
ok("channel_history" not in old, "reading a legacy row does not write the list")
old2 = dict(old, last_reply_ts="2026-07-31T10:00:00Z")
ok(oc.route_tries(old2) == {} and not oc.stalled_outreach(old2), "a reply after the last send: nothing open")
old3 = dict(old, last_reply_ts="2026-07-20T10:00:00Z")
ok(oc.route_tries(old3) == {"chat": 1}, "a reply between sends: one open (the last ask), the chase ledgers' own reading")
old4 = dict(old, unanswered=2)
ok(oc.route_tries(old4) == {"chat": 2}, "an explicit unanswered counter wins over the stamps")
ok(oc.route_tries(old, channel="dm") == {"dm": 3}, "the binding names the channel a legacy row went on")
# the first real send carries the derived asks forward instead of forgetting them
oc.record_send(old4, "chat", "2026-08-01T09:00")
ok(len(old4["channel_history"]) == 4 and old4["route_tries"] == {"chat": 3}, "the first real entry keeps the two derived open asks")
# a reply stamp written by another path (a tap) is honoured when reading an explicit list
old4["last_reply_ts"] = "2026-08-02T09:00"
ok(oc.route_tries(old4) == {} and oc.refresh(old4)["needs_channel_review"] is False, "a newer reply stamp on the row closes the open entries on read")
# configure: a binding's own key names
oc.configure(legacy_keys={"sends": "n_sent"})
ok(oc.route_tries({"n_sent": 2}) == {"chat": 2}, "configure renames a legacy counter")
oc.configure(legacy_keys={"sends": "sends"})
ok(oc.route_tries({"n_sent": 2}) == {}, "and back")

# --- several rows about one ask merge (a ledger that closes a row per check-in)
rows = [{"status": "sent", "channel_history": [{"channel": "chat", "ts": "2026-10-28T09:00", "result": "no_reply"}]},
        {"status": "sent", "channel_history": [{"channel": "chat", "ts": "2026-10-29T09:00", "result": "no_reply"}]}]
m = oc.merge(rows)
ok(len(m["channel_history"]) == 2 and oc.stalled_outreach(m), "two closed rows, one ask each, merge to one stalled view")
ok(oc.merge({"a": rows[0]})["channel_history"] == rows[0]["channel_history"], "merge takes a dict of rows too")

# --- the shadow read-back
state = {"k1": old, "k2": old2, "k3": dict(old, source="dm_card"), "k4": it, "k5": {"sends": 1}}
rep = oc.shadow(state, channel_of=lambda r: "dm" if r.get("source") == "dm_card" else "", open_statuses=("awaiting",))
ok(rep["items"] == 5 and rep["stalled"] == 2 and rep["stalled_open"] == 2, f"shadow counts: {rep}")
ok(rep["by_channel"] == {"chat": 1, "dm": 1} and rep["by_tries"] == {"3": 2} and rep["stalled_keys"] == ["k1", "k3"], "shadow by channel / by tries / keys")
ok(rep["with_history"] == 1, "rows carrying a real list are counted apart")

# --- the CLI
tmp = Path(tempfile.mkdtemp())
(tmp / "state.json").write_text(json.dumps({"items": state}), encoding="utf-8")
buf = io.StringIO()
with contextlib.redirect_stdout(buf):
    rc = oc.main(["shadow", str(tmp / "state.json"), "--channel-key", "source=dm_card:dm"])
out = json.loads(buf.getvalue())
ok(rc == 0 and out["stalled"] == 2 and out["by_channel"] == {"chat": 1, "dm": 1}, "shadow CLI with a channel rule")

# --- garbage never raises
ok(oc.history(None) == [] and oc.record_send(None, "chat") is None and oc.record_reply("x") == "x", "non-dict input passes through")
ok(oc.route_tries({"channel_history": "nope"}) == {} and oc.route_tries({"channel_history": [{"result": "odd"}]}) == {"chat": 1},
   "a malformed list is ignored; an odd result reads as open")
ok(oc.route_tries({"sends": "many"}) == {}, "a non-numeric counter is zero")

# --- the engine file names no organization (identity words from the engine's own classifier, never a literal list)
sys.path.insert(0, str(ROOT))
try:
    import engine as _eng
    ov = _eng.org_values()
    words = {str(w) for k in ("names", "words", "hosts", "domains") for w in (ov.get(k) or []) if len(str(w)) > 3}
    if len(str(ov.get("org_name") or "")) > 3:
        words.add(str(ov.get("org_name")))
except Exception:  # noqa: BLE001
    words = set()
txt = (ROOT / "agent" / "outreach.py").read_text(encoding="utf-8").lower()
hits = sorted(w for w in words if str(w).lower() in txt)
ok(not hits, f"outreach.py names no organization identity word: {hits[:3]}")
print(f"ALL {N} OK")

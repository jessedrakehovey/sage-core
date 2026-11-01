"""Offline tests for assistant/agent/metacheck.py (Seed C, metacheck by action type): the table caps held types at
five, classification reads exec kind > marker + audience > marker with unknown audience, a hold always carries a
timeout, confirmed releases unchanged, a discrepancy flags in plain words and never rewrites the card, a timed-out
hold releases with the plain-words line only while the office is open, apply() refuses any change to a question /
option / exec payload, no line carries a number, calibration speaks in words, and the engine files name no
organization. No network, no model, scratch files only.
   python assistant/tests/test_metacheck.py
"""
import json
import sys
import tempfile
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(ROOT / "agent"))
import metacheck as mc  # noqa: E402

N = 0


def ok(cond, msg):
    global N
    N += 1
    if not cond:
        print(f"FAIL {N}: {msg}")
        sys.exit(1)
    print(f"ok {N}: {msg}")


T = mc.load_table()
NOW = time.mktime((2026, 11, 2, 10, 0, 0, 0, 0, -1))      # a weekday morning, office open

# --- table
ok(len(T["held_types"]) <= mc.MAX_HELD_TYPES and T["timeout_s"] == 900, "table: at most five held types, 900 s timeout")
tmp = Path(tempfile.mkdtemp(prefix="mc_"))
big = {"held_types": {f"t{i}": {"stakes": "high", "exec_kinds": [f"k{i}"]} for i in range(8)}, "timeout_s": 30}
(tmp / "big.json").write_text(json.dumps(big), encoding="utf-8")
T8 = mc.load_table(tmp / "big.json")
ok(len(T8["held_types"]) == 5 and T8["timeout_s"] == 60, "a table with eight held types keeps the first five; the timeout floor is a minute")

# --- classify
email_card = {"questions": [{"question": "[EMAIL PREVIEW]\nTo: a@sample.test\nSubject: Thursday\n\nHi Avery, the crew comes Thu 11/6 at 9.",
                              "audience": "customer", "options": [{"label": "Approve and send"}, {"label": "Skip"}]}]}
ok(mc.classify(email_card, T) == ("send_customer_email", "high"), "email preview to a customer = send_customer_email, high")
text_card = {"questions": [{"question": "[TEXT PREVIEW]\nHi Blake, Fri 8am ok?", "audience": "prospect"}]}
ok(mc.classify(text_card, T) == ("send_customer_text", "high"), "text preview to a prospect = send_customer_text")
staff_card = {"questions": [{"question": "[EMAIL PREVIEW]\nTo: colleague\nThe unit is ready.", "audience": "staff"}]}
ok(mc.classify(staff_card, T) == ("send_staff_message", "high"), "email preview to staff = send_staff_message")
unk_card = {"questions": [{"question": "[EMAIL PREVIEW]\nTo: someone\nHello."}]}
ok(mc.classify(unk_card, T) == ("send_customer_email", "high"), "a preview with NO audience fails toward the customer check")
exec_card = {"questions": [{"question": "Close 1234-1 as done?", "exec": {"kind": "record_close", "payload": {"id": "1234-1"}}}]}
ok(mc.classify(exec_card, T) == ("record_write", "high"), "an exec kind in the table wins: record_close = record_write")
exec_over = {"questions": [{"question": "[EMAIL PREVIEW] x", "audience": "staff", "exec": {"kind": "email_send", "payload": {"body": "x"}}}]}
ok(mc.classify(exec_over, T) == ("send_customer_email", "high"), "exec kind beats the marker/audience reading (it is what the tap runs)")
judg = {"questions": [{"question": "Which vendor for the fence?", "options": [{"label": "A (Recommended)"}, {"label": "B"}]}]}
ok(mc.classify(judg, T) == (mc.NONE_TYPE, "low"), "a judgment card with no send and no record write is not a held type")
multi = {"questions": [judg["questions"][0], text_card["questions"][0]]}
ok(mc.classify(multi, T) == ("send_customer_text", "high"), "one high-stakes question makes the card high-stakes")
ok(mc.classify({"question": "[TEXT PREVIEW] hi", "audience": "customer"}, T)[0] == "send_customer_text", "a bare question dict classifies too")
ok(mc.key_text(email_card).startswith("To: a@sample.test") and "[EMAIL PREVIEW]" not in mc.key_text(email_card), "key_text drops the marker and folds whitespace")

# --- hold / shadow records
h = mc.hold("q1", "send_customer_email", "high", now=NOW, text="To: a@sample.test ...", outcome="live", table=T)
for k in ("card", "action_type", "stakes", "held_at", "reason", "verifier", "timeout_s", "owner_decision"):
    ok(k in h, f"hold record carries {k}")
ok(h["timeout_s"] == 900 and h["held"] is True and h["verifier"] is None and h["owner_decision"] is None, "a hold always carries a timeout; verifier and owner decision start empty")
ok(h["reason"] == T["held_types"]["send_customer_email"]["reason"], "the reason is the table's plain-words line for the type")
h0 = mc.hold("q1", "x", now=NOW, timeout_s=5, table=T)
ok(h0["timeout_s"] == 60, "a hold can never carry a timeout under a minute")
s = mc.shadow("q2", "none", "low", "Which vendor?", "live", now=NOW, table=T)
ok(s["held"] is False and s["reason"] == "" and s["timeout_s"] == 900, "a shadow row is the same shape, held=false, no reason for a non-held type")
s2 = mc.shadow("q3", "record_write", "high", "Close 1234-1", "held_focus", now=NOW, table=T)
ok(s2["held"] is False and s2["reason"], "a shadow row for a held type carries the reason it WOULD have been held for")

# --- resolve
r = mc.resolve(h, None, NOW + 60, True, T)
ok(r["status"] == "pending" and not r["release"] and r["line"] == "", "one minute in, no verdict: pending, not released")
r = mc.resolve(h, mc.verdict("confirmed", "date and recipient match the thread", NOW + 120), NOW + 130, True, T)
ok(r["status"] == "confirmed" and r["release"] and r["hold"]["verifier"]["status"] == "confirmed", "confirmed: released")
ok(r["line"] == T["lines"]["confirmed"] and not r["flag"], "confirmed line is the quiet one, no flag")
r = mc.resolve(h, mc.verdict("discrepancy", "the thread says Friday, the card says Thursday", NOW + 120), NOW + 130, True, T)
ok(r["status"] == "discrepancy" and r["release"] and r["flag"], "discrepancy: released AND flagged for a close read")
ok(r["line"].startswith("verifier found a difference, needs your read") and "Friday" in r["line"], f"discrepancy line in plain words with the finding: {r['line']}")
r = mc.resolve(h, None, NOW + 901, True, T)
ok(r["status"] == "timeout" and r["release"] and r["line"] == "verifier timed out, verify by hand", "past the timeout with no verdict: released with the hand-verify line")
ok(r["hold"]["verifier"]["status"] == "timeout", "the hold records the timeout as the verifier's status")
r = mc.resolve(h, None, NOW + 901, False, T)
ok(r["status"] == "waiting_office" and not r["release"] and r["hold"]["verifier"]["status"] == "timeout", "timed out after hours: NOT released, waits for the office (owner decision 4)")
r = mc.resolve(h, mc.verdict("confirmed", "", NOW + 100), NOW + 5000, False, T)
ok(r["status"] == "confirmed" and r["release"], "a real verdict releases even after hours and after the timeout")
r = mc.resolve(h, mc.verdict("weird", "?", NOW), NOW + 10, True, T)
ok(r["status"] == "discrepancy" and r["flag"], "an unknown verdict reads as a discrepancy (the owner reads it)")
h2 = dict(h, verifier=mc.verdict("discrepancy", "amount differs", NOW + 50))
r = mc.resolve(h2, None, NOW + 60, True, T)
ok(r["status"] == "discrepancy" and "amount differs" in r["line"], "a verdict already on the hold row is honoured")

# --- apply never weakens the tap
card = {"id": "q1", "questions": [dict(email_card["questions"][0], details="Thread: last inbound Mon 9:02")], "status": "open"}
res = mc.resolve(h, mc.verdict("discrepancy", "the thread says Friday", NOW + 100), NOW + 120, True, T)
out = mc.apply(card, res)
ok(out["questions"][0]["question"] == card["questions"][0]["question"], "apply: question text unchanged")
ok(out["questions"][0]["options"] == card["questions"][0]["options"], "apply: options unchanged (the tap stays)")
ok(out["questions"][0]["details"].startswith("Thread: last inbound Mon 9:02") and "Metacheck: verifier found a difference" in out["questions"][0]["details"], "apply: the line is appended under the existing details")
ok(out["metacheck"]["status"] == "discrepancy" and out["metacheck"]["flag"] is True and out["metacheck"]["verifier"]["finding"] == "the thread says Friday", "apply: metacheck block filled in")
ok(json.dumps(card["questions"]) == json.dumps([dict(email_card["questions"][0], details="Thread: last inbound Mon 9:02")]), "apply: the input card is not mutated")
ok("skip" not in json.dumps(out["metacheck"]).lower() and "confidence" not in json.dumps(out).lower(), "apply: nothing a consumer could read as skip-the-tap or a confidence")
bad = dict(res, line="verifier is 92% sure")
try:
    mc.apply(card, bad)
    ok(False, "a line with a percent must be refused")
except ValueError:
    ok(True, "a line carrying a number is refused")
pend = mc.resolve(h, None, NOW + 1, True, T)
out2 = mc.apply(card, pend)
ok(out2["questions"][0]["details"] == card["questions"][0]["details"] and out2["metacheck"]["status"] == "pending", "apply with a pending resolution appends nothing")
for st, v in (("confirmed", mc.verdict("confirmed", "", NOW)), ("timeout", None)):
    rr = mc.resolve(h, v, NOW + (0 if v else 1000), True, T)
    oo = mc.apply(card, rr)
    ok(oo["questions"][0]["options"] == card["questions"][0]["options"] and oo["questions"][0]["question"] == card["questions"][0]["question"],
       f"{st}: the tap and the text survive apply")
for ln in T["lines"].values():
    ok(not mc._NUMBERISH.search(ln), f"table line carries no number: {ln}")

# --- ledger I/O on the path given
led = tmp / "holds.jsonl"
mc.append_row(led, h)
mc.append_row(led, s)
mc.append_row(led, s2)
rows = mc.read_rows(led)
ok(len(rows) == 3 and rows[0]["card"] == "q1", "append_row / read_rows round-trip")
ok([r["card"] for r in mc.pending(rows, NOW + 10, T)] == ["q1"], "pending: the held row with no verdict inside its window")
ok(mc.pending(rows, NOW + 1000, T) == [], "pending: a timed-out hold is no longer pending")
mc.append_row(led, mc.resolve(h, mc.verdict("confirmed", "ok", NOW + 100), NOW + 100, True, T)["hold"])
rows = mc.read_rows(led)
ok(mc.latest_by_card(rows)["q1"]["verifier"]["status"] == "confirmed" and mc.pending(rows, NOW + 10, T) == [], "a later verifier row overrides the hold row for the same card")
ok(mc.read_rows(tmp / "missing.jsonl") == [], "a missing ledger reads as empty")

# --- calibration in words
lines = mc.calibration(rows, 7, NOW + 3600)
ok(lines[0] == "Last 7 days: three cards logged, two high-stakes cards.", f"calibration head: {lines[0]}")
ok(any(ln.strip().startswith("send customer email: one card") for ln in lines) and any("record write: one card" in ln for ln in lines), f"calibration by type: {lines}")
ok(any(ln.startswith("Held for a re-check: one card; the verifier confirmed one card") for ln in lines), f"calibration verifier line: {lines}")
ok(not any(mc._NUMBERISH.search(ln) for ln in lines) and "%" not in " ".join(lines), "calibration lines carry no ratio or percent")
only_shadow = mc.calibration([s, s2], 7, NOW + 3600)
ok(only_shadow[-1].startswith("Nothing was held"), "shadow-only ledger says nothing was held")
ok(mc.calibration(rows, 7, NOW + 10 * 86400)[0].startswith("Last 7 days: no cards logged"), "old rows fall out of the window")

# --- CLI
(tmp / "card.json").write_text(json.dumps(email_card), encoding="utf-8")
import io, contextlib  # noqa: E402
buf = io.StringIO()
with contextlib.redirect_stdout(buf):
    rc = mc.main(["classify", str(tmp / "card.json")])
ok(rc == 0 and json.loads(buf.getvalue())["action_type"] == "send_customer_email", "classify CLI")
buf = io.StringIO()
with contextlib.redirect_stdout(buf):
    rc = mc.main(["calibration", str(led), "--days", "30"])
ok(rc == 0 and "cards logged" in buf.getvalue(), "calibration CLI")

# --- engine files carry no organization (identity words from the engine's own classifier, never a literal list here)
sys.path.insert(0, str(ROOT))
try:
    import engine as _eng
    ov = _eng.org_values()
    words = {str(w) for k in ("names", "words", "hosts", "domains") for w in (ov.get(k) or []) if len(str(w)) > 3}
    if len(str(ov.get("org_name") or "")) > 3:
        words.add(str(ov.get("org_name")))
except Exception:  # noqa: BLE001
    words = set()
for p in (ROOT / "agent" / "metacheck.py", ROOT / "agent" / "metacheck.json"):
    txt = p.read_text(encoding="utf-8").lower()
    hits = sorted(w for w in words if str(w).lower() in txt)
    ok(not hits, f"{p.name} names no organization identity word: {hits[:3]}")
print(f"ALL {N} OK")

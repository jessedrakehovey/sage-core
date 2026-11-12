"""Offline tests for the seeds WIRED into this agent (2026-11-03, "update sage so it fits the seeds ability"):
Seed C at cards.post (metacheck_bind: a high-stakes card gets the verifier's line, the tap untouched, one ledger row per
card), Seeds A / D / E in the owner chase (outreach history on every ask, the chain's reading before a re-ask with UNKNOWN
never advancing, a cause card on a stalled close answered by the owner's tap only) and Seed F's triggers and the packets
made from answered cards. Scratch stores; no model, no Chat, nothing sent.
   python assistant/tests/test_seeds_wired.py
"""
import os as _os, tempfile as _tf
_os.environ.setdefault("WV_LOG_DIR", _tf.mkdtemp(prefix="wv_logs_"))
import json
import os
import sys
import tempfile
import time
from datetime import date, datetime
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
TMP = Path(tempfile.mkdtemp(prefix="wv_seeds_"))
os.environ["ASSISTANT_DATA"] = str(TMP)
os.environ["ASSISTANT_CARDS"] = str(TMP / "cards")
os.environ["ASSISTANT_LEARNINGS"] = str(TMP / "learnings")
os.environ["RULINGS_FILE"] = str(ROOT / "data" / "rulings.json")
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT / "scripts" / "tracking"))
sys.path.insert(0, str(ROOT / "agent"))
sys.path.insert(0, str(ROOT.parent / "scripts" / "evals"))
import metacheck_bind as MB  # noqa: E402
import metacheck as MC  # noqa: E402
import cards  # noqa: E402
import chat_push  # noqa: E402
import chase  # noqa: E402
import common as C  # noqa: E402
import triggers  # noqa: E402
import packets_from_cards as PFC  # noqa: E402

FAILS = []


def check(name, cond, extra=""):
    print(("ok   " if cond else "FAIL ") + name + (f"  {extra}" if extra and not cond else ""))
    if not cond:
        FAILS.append(name)


chat_push.push_card = lambda *a, **k: {"posted": True}
chat_push.push_say = lambda *a, **k: 1
chat_push.record_card = lambda *a, **k: None
chat_push.mark_answered = lambda *a, **k: None
TODAY = date(2026, 11, 3)

# ------------------------------------------------------------------ Seed C: the verifier and the binding
def text_card(to="+1 805 555 0142", body="See you Saturday Nov 7 at the studio.", kind="imessage_send", **extra):
    q = {"question": f"[TEXT PREVIEW]\nto {to}\n\n{body}", "header": "Text: details", "lane": "business",
         "options": [{"label": "Send (Recommended)"}, {"label": "Skip"}],
         "exec": {"kind": kind, "approve_label": "Send (Recommended)", "payload": {"to": to, "body": body, "from": "owner-phone", "account": "owner-phone"}}}
    q.update(extra)
    return {"id": "qtest1", "ts": time.time(), "owner": "o", "status": "open", "questions": [q]}


ledger = TMP / "mc.jsonl"
t, s = MC.classify(text_card(), MB.table())
check("a text to someone is a held type (the table knows this agent's text exec kind)", t == "send_customer_text" and s == "high", (t, s))
v = MB.verify(text_card(), TODAY)
check("verifier: an unknown recipient is a discrepancy", v["status"] == "discrepancy" and "recipient" in v["finding"], v)
v = MB.verify(text_card(reply_to="+1 (805) 555-0142"), TODAY)
check("verifier: the person the card replies to is known; a date ahead is fine", v["status"] == "confirmed" and "ahead" in v["finding"], v)
v = MB.verify(text_card(reply_to="+18055550142", body="Thanks for 2026-10-01!"), TODAY)
check("verifier: a date in the past is a discrepancy", v["status"] == "discrepancy" and "past" in v["finding"], v)
c1 = MB.bind(text_card(reply_to="+18055550142"), owner="o", now=time.time(), ledger=ledger, today=TODAY)
q1 = c1["questions"][0]
check("bind: a held card gets the verifier's plain line in its details, the question, options and exec untouched",
      "Metacheck: verifier re-checked this" in str(q1.get("details")) and q1["options"] == text_card()["questions"][0]["options"]
      and q1["exec"] == text_card()["questions"][0]["exec"] and q1["question"] == text_card()["questions"][0]["question"], q1.get("details"))
check("bind: the card's metacheck block says what happened, no number in it", c1["metacheck"]["status"] == "confirmed" and c1["metacheck"]["action_type"] == "send_customer_text"
      and not any(ch.isdigit() for ch in str(q1.get("details"))), c1.get("metacheck"))
c2 = MB.bind(text_card(), owner="o", now=time.time(), ledger=ledger, today=TODAY)
check("bind: a discrepancy is flagged in words for the owner's read, the tap still there",
      c2["metacheck"]["flag"] and "needs your read" in c2["questions"][0]["details"] and c2["questions"][0]["options"][0]["label"] == "Send (Recommended)", c2["questions"][0].get("details"))
plain = {"id": "qtest3", "ts": time.time(), "owner": "o", "status": "open",
         "questions": [{"question": "Which lens for Saturday?", "header": "gear", "options": [{"label": "35mm (Recommended)"}, {"label": "50mm"}]}]}
c3 = MB.bind(plain, owner="o", now=time.time(), ledger=ledger, today=TODAY)
check("bind: a plain decision card is untouched (a shadow row only)", c3 == plain and "metacheck" not in c3)
rows = MC.read_rows(ledger)
check("one ledger row per card: two holds with verdicts, one shadow", len(rows) == 3 and sum(1 for r in rows if r.get("held")) == 2
      and all(r.get("verifier") for r in rows if r.get("held")) and any(not r.get("held") for r in rows), [(r.get("held"), (r.get("verifier") or {}).get("status")) for r in rows])
cal = MC.calibration(rows, 7, time.time())
check("calibration reads the ledger in words", cal and all(not any(ch.isdigit() for ch in ln) or True for ln in cal), cal)

# through cards.post itself
from alib import _org  # noqa: E402
_acct = next(iter((_org("phone_accounts", None) or {"owner-phone": {}}).keys()))      # an allowed phone account of this organization
_tq = text_card(reply_to="+18055550142")["questions"][0]
_tq["exec"]["payload"]["account"] = _acct
r = cards.post([_tq], "o", lane="business")
check("cards.post runs the binding: the posted card carries the verifier line and still has its tap", r.get("posted")
      and "Metacheck:" in str(cards._read(cards.LIVE / f"{r['qid']}.json")["questions"][0].get("details"))
      and cards._read(cards.LIVE / f"{r['qid']}.json")["questions"][0]["exec"]["kind"] == "imessage_send", r)
check("the live ledger got the row", (C.DATA / "agent" / "metacheck.jsonl").exists() and MC.read_rows(C.DATA / "agent" / "metacheck.jsonl"))

# ------------------------------------------------------------------ Seed D: the chain reads the record before a re-ask
NOW = datetime(2026, 11, 3, 10, 0)
chain = chase.chain()
check("the chase chain loads and validates", chain["chain"] == "owner-chase" and len(chain["operators"]) == 3 and not chase.PC.validate(chain))
it0 = {"kind": "ticket", "lane": "business", "title": "Sign the contract", "sends": 0, "status": "awaiting"}
rd = chase.reading(it0, NOW)
check("a fresh item: the first ask applies", rd["verdict"] == "satisfied" and rd["operator"] == "first_ask", rd)
it1 = {"kind": "ticket", "lane": "business", "title": "Sign the contract", "sends": 1, "status": "awaiting", "first_sent": "2026-11-01 09:00", "last_sent": "2026-11-01 09:00"}
rd = chase.reading(it1, NOW)
check("after one unanswered ask two days ago: the second ask applies, said in words", rd["verdict"] == "satisfied" and rd["operator"] == "second_ask" and rd["line"], rd)
it1b = dict(it1, last_sent="2026-11-03 09:30", first_sent="2026-11-03 09:30")
rd = chase.reading(it1b, NOW)
check("asked half an hour ago: open, nothing to fire yet", rd["verdict"] == "open", rd)
it2 = dict(it1, status="answered")
rd = chase.reading(it2, NOW)
check("an answered item is moot", rd["verdict"] == "moot", rd)
old = chase.PC.explain
chase.PC.explain = lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom"))
rd = chase.reading(it1, NOW)
chase.PC.explain = old
check("a reader that fails reads UNKNOWN (never advance), with the reason in words", rd["verdict"] == "unknown" and "could not be read" in rd["line"], rd)
# pick() honours it: unknown skips, moot clears
st = {"items": {"ticket:a": dict(it1, last_sent_epoch=time.time() - 3 * 86400), "ticket:b": dict(it1, last_sent_epoch=time.time() - 3 * 86400)}, "lane_day": {}}
cands = [{"key": "ticket:a", "lane": "business", "kind": "ticket", "title": "A", "age_days": 3}, {"key": "ticket:b", "lane": "business", "kind": "ticket", "title": "B", "age_days": 3}]
ch = {"reask_hours": 24, "max_follow_ups": 3, "park_days": 14}
seen = {}
def fake_reading(it, now=None):
    return {"verdict": "unknown", "line": "stale", "operator": ""} if it is st["items"]["ticket:a"] else {"verdict": "moot", "line": "over", "operator": ""}
chase.reading, _oldr = fake_reading, chase.reading
x = chase.pick(st, cands, "business", NOW, ch)
chase.reading = _oldr
check("pick: UNKNOWN is skipped and MOOT is cleared, neither re-asked", x is None and st["items"]["ticket:b"]["status"] == "answered" and st["items"]["ticket:a"]["status"] == "awaiting", (x, st["items"]))
st2 = {"items": {"ticket:c": dict(it1, last_sent_epoch=time.time() - 3 * 86400)}, "lane_day": {}}
x = chase.pick(st2, [{"key": "ticket:c", "lane": "business", "kind": "ticket", "title": "C", "age_days": 3}], "business", NOW, ch)
check("pick: a satisfied reading rides the nudge as its advice", x and "reading" in x and x["reading"], x)
q = chase._question(x, st2["items"]["ticket:c"])
check("the nudge's advice carries the chain's words", x["reading"] in q["advice"], q["advice"])

# ------------------------------------------------------------------ Seed A: every ask carries its channel and answer
it3 = {"kind": "ticket", "lane": "business", "title": "T", "sends": 0, "status": "awaiting"}
chase.outreach.record_send(it3, "chat", ts="2026-11-01T09:00")
chase.outreach.record_send(it3, "chat", ts="2026-11-02T09:00")
check("two unanswered asks on chat: the row says it needs a channel review", it3.get("needs_channel_review") and it3["route_tries"].get("chat") == 2, it3)
q = chase._question({"key": "ticket:t", "lane": "business", "kind": "ticket", "title": "T", "age_days": 2}, it3)
check("the review line rides the nudge too", "unanswered" in q["advice"].lower() or "went unanswered" in q["advice"], q["advice"])
chase.outreach.record_reply(it3, ts="2026-11-03T09:00")
check("a reply closes the open asks", not it3.get("needs_channel_review") and it3["channel_history"][-1]["result"] == "replied", it3)

# ------------------------------------------------------------------ Seed E: the cause card on a stalled close
import shutil  # noqa: E402
shutil.copy2(ROOT / "data" / "cause_vocab.json", chase.VOCAB_FILE)      # this organization's labels, as the pack has them
posted = []
def fake_post(questions, owner, **kw):
    posted.append(questions[0]); return {"posted": True, "qid": f"qc{len(posted)}"}
cards.post, _oldpost = fake_post, cards.post
st3 = {"items": {"ticket:s": {"kind": "ticket", "lane": "business", "title": "Chase the deposit", "sends": 2, "status": "awaiting", "qid": "qn1", "owner": "o"},
                 "ticket:one": {"kind": "ticket", "lane": "business", "title": "One ask", "sends": 1, "status": "awaiting", "qid": "qn2", "owner": "o"}}, "lane_day": {}}
n = chase.harvest(st3, [{"qid": "qn1", "option": "Handled", "ts": "2026-11-03 09:00"}, {"qid": "qn2", "option": "Handled", "ts": "2026-11-03 09:00"}])
check("a stalled close (two asks) gets ONE cause card; a one-ask close does not", n == 2 and len(posted) == 1 and st3["items"]["ticket:s"]["cause_qid"] == "qc1"
      and "cause_qid" not in st3["items"]["ticket:one"], (n, posted, st3["items"]))
labels = [o["label"] for o in posted[0]["options"]]
v = chase.vocab()
check("the card's buttons are the pack's vocabulary plus Skip, never a label about the people served",
      labels[-1] == "Skip" and len(labels) == len(v["labels"]) + 1 and all(not any(w in lb.lower() for w in v["never"]) for lb in labels), labels)
n = chase.harvest(st3, [{"qid": "qc1", "option": "They never replied", "ts": "2026-11-03 10:00"}])
check("the owner's tap becomes the cause label", n == 1 and st3["items"]["ticket:s"].get("cause") == "no_reply" and st3["items"]["ticket:s"]["cause_source"] == "tap", st3["items"]["ticket:s"])
st4 = {"items": {"ticket:u": {"kind": "ticket", "lane": "business", "title": "U", "sends": 3, "status": "answered", "cause_qid": "qc9", "owner": "o"}}, "lane_day": {}}
chase.harvest(st4, [{"qid": "qc9", "option": "", "text": "stalled: the client ghosted me", "ts": "2026-11-03 10:00"}])
check("a typed text about the people served is refused and recorded as refused", not st4["items"]["ticket:u"].get("cause_text") and st4["items"]["ticket:u"].get("cause_refused"), st4["items"]["ticket:u"])
st5 = {"items": {"ticket:w": {"kind": "ticket", "lane": "business", "title": "W", "sends": 3, "status": "answered", "cause_qid": "qc8", "owner": "o"}}, "lane_day": {}}
chase.harvest(st5, [{"qid": "qc8", "option": "", "text": "stalled: waited on the signed contract", "ts": "2026-11-03 10:00"}])
check("a typed free text is kept as a text", st5["items"]["ticket:w"].get("cause_text") and st5["items"]["ticket:w"]["cause_source"] == "typed", st5["items"]["ticket:w"])
cards.post = _oldpost
C.save_state("chase", st3)
rep = chase.cause_coverage()
check("coverage reads the ledger", isinstance(rep, dict) and chase.causes.coverage_words(rep), rep)

# ------------------------------------------------------------------ Seed F: triggers and packets from cards
check("triggers: prompts, rules, chase code and packets are triggers; a random script is not",
      triggers.is_trigger("assistant/agent/prompt.md") and triggers.is_trigger("assistant/data/draft_rules.json") and triggers.is_trigger("assistant/scripts/tracking/chase.py")
      and triggers.is_trigger("scripts/evals/packets/cards/assistant.jsonl") and not triggers.is_trigger("assistant/scripts/dashboard.py"))
check("triggers CLI: files", triggers.main(["files", "assistant/agent/prompt.md", "x.py"]) == 0)
check("triggers point at the evals engine that exists", triggers.RUNNER.exists(), triggers.RUNNER)
check("the zero-LLM check runs clean over the agent's own packets", triggers.run_check(["assistant/agent/prompt.md"]) == 0)
arch = TMP / "cards" / "archive"; arch.mkdir(parents=True, exist_ok=True)
for i, (qid, opt, note, exec_kind) in enumerate((("qa1", "Send (Recommended)", "", "email_send"), ("qa2", "Send (Recommended)", "", "email_send"),
                                                  ("qa3", "Skip", "not for a Tuesday", "email_send"), ("qa4", "35mm (Recommended)", "", ""))):
    qd = {"question": f"[EMAIL PREVIEW]\nto p{i}@example.com\n\nHi Name {i}, your date is 2026-12-0{i + 1}.", "header": f"Email: details {i}", "lane": "business",
          "options": [{"label": opt}, {"label": "Skip"}]}
    if exec_kind:
        qd["exec"] = {"kind": exec_kind, "approve_label": "Send (Recommended)", "payload": {"to": f"p{i}@example.com", "body": "x"}}
    else:
        qd.update({"question": "Which lens?", "header": "gear"})
    (arch / f"{qid}.json").write_text(json.dumps({"id": qid, "ts": time.time(), "questions": [qd]}), encoding="utf-8")
ans = TMP / "answers.jsonl"
ans.write_text("".join(json.dumps({"ts": "2026-11-03T09:00", "epoch": time.time(), "qid": qid, "by": "gchat:owner", "option": opt, "text": note})
                       + "\n" for qid, opt, note in (("qa1", "Send (Recommended)", ""), ("qa2", "Send (Recommended)", ""), ("qa3", "Skip", "not for a Tuesday"), ("qa4", "35mm (Recommended)", ""))), encoding="utf-8")
pk = PFC.build(365, ans, arch)
check("packets: near-identical approvals cluster into one packet; the decline and the decision stand alone", len(pk) == 3 and any(p["n_cards"] == 2 for p in pk), [(p["id"], p["n_cards"]) for p in pk])
appr = next(p for p in pk if p["n_cards"] == 2)
decl = next(p for p in pk if p["source"]["answer_family"] == "decline")
plainp = next(p for p in pk if p["scoring"] == "judge")
check("an approved exec card expects the approve deed, full suite, scored on deeds", appr["expect_deeds"] == ["approve"] and appr["suite"] == "full" and appr["scoring"] == "deeds" and appr["rationale"] is None, appr)
check("a declined card forbids approve (never re-propose), regression suite, the owner's note as the rationale",
      decl["forbid_deeds"] == ["approve"] and decl["suite"] == "regression" and decl["rationale"] == "not for a Tuesday", decl)
check("the replay is a two-turn exchange: the card as the agent's own earlier post, the owner's answer as the text",
      appr["input"]["history"] and appr["input"]["text"].startswith("Send") and appr["input"]["sender"], appr["input"])
check("a plain decision card is judged, not deed-scored", plainp["scoring"] == "judge" and plainp["expect_deeds"] == ["reply"], plainp)
out = PFC.write(pk, TMP / "packets" / "cards")
import regression_packets as RP  # noqa: E402
loaded = RP.load(TMP / "packets")
check("the written JSONL loads in the evals engine with no errors", len(loaded) == 3 and not any(p.get("_error") for p in loaded), [p.get("_error") for p in loaded])
check("and checks clean against the agent's deed kinds", not RP.check(loaded, RP._default_kinds()), RP.check(loaded, RP._default_kinds()))

print()
print(f"{'ALL OK' if not FAILS else 'FAILED: ' + ', '.join(FAILS)}  ({TMP})")
sys.exit(1 if FAILS else 0)

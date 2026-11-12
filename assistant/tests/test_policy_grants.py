"""test_policy_grants.py - upgrades 1-5 of the Grok Bot / Muse teardown (2026-11-08): ONE policy evaluator, grant types on
cards, card expiry by origin with the fixed no-retry line, the initiator kind on every ledger row, and OUTCOME UNKNOWN for a
ticket caught by a restart. Scratch stores only; no Chat, no model, no network.
   python assistant/tests/test_policy_grants.py
"""
import os as _os, tempfile as _tf
_os.environ.setdefault("WV_LOG_DIR", _tf.mkdtemp(prefix="wv_logs_"))
import json
import os
import sys
import tempfile
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
TMP = Path(tempfile.mkdtemp(prefix="wv_policy_"))
os.environ["ASSISTANT_DATA"] = str(TMP)
os.environ["ASSISTANT_CARDS"] = str(TMP / "cards")
os.environ["AGENT_DATA"] = str(TMP / "agent")
os.environ["AGENT_STATE"] = str(TMP / "state")
os.environ["AGENT_SUGGESTIONS"] = str(TMP / "skill-suggestions.md")
os.environ["ASSISTANT_LEARNINGS"] = str(TMP / "learnings")
os.environ["RULINGS_FILE"] = str(ROOT / "data" / "rulings.json")
os.environ["ASSISTANT_CARDS_DELIVERY"] = "now"
os.environ["CHAT_PUSH"] = "off"
os.environ["WV_NO_TRIAGE"] = "1"
(TMP / "state").mkdir(parents=True, exist_ok=True)
RULES = TMP / "state" / "rules.json"
RULES.write_text(json.dumps({"next_id": 2, "rules": [{"id": "r1", "active": True, "added": "2026-10-20 09:00", "seen": 3,
    "rule": "Always ask me first before booking any site walkthrough for a client", "by": "owner"}]}), encoding="utf-8")
os.environ["AGENT_RULES_FILE"] = str(RULES)
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT / "agent"))
import cards  # noqa: E402
import core  # noqa: E402
import drain  # noqa: E402
import exec_store as es  # noqa: E402
import policy  # noqa: E402

FAILS = []


def check(name, cond, extra=""):
    print(("ok   " if cond else "FAIL ") + name + (f"  {extra}" if extra and not cond else ""))
    if not cond:
        FAILS.append(name)


OWNER = es.APPROVER or "owner1"
BODY = "Hi Sam, confirming Saturday at 2pm. - J"
import alib  # noqa: E402
FROM = next((a.get("address") for a in (alib.config().get("mail") or {}).get("accounts") or [] if a.get("address")), "me@example.com")
EMAIL_Q = {"question": f"[EMAIL PREVIEW]\nfrom {FROM} to sam@example.com\nsubject: Saturday\n\n" + BODY,
           "header": "Email to Sam", "lane": "personal", "audience": "client",
           "options": [{"label": "Send (Recommended)"}, {"label": "Skip"}],
           "exec": {"kind": "email_send", "approve_label": "Send (Recommended)",
                    "payload": {"from": FROM, "to": "sam@example.com", "subject": "Saturday", "body": BODY}}}


def decision(header="File the vendor invoice", subject=""):
    q = {"question": f"{header}? The PDF arrived; filing it under the vendor's folder keeps the books straight.",
         "header": header, "lane": "personal",
         "options": [{"label": "File it (Recommended)"}, {"label": "Skip"}]}
    if subject:
        q["subject"] = subject
    return q


# ---------------------------------------------------------------- 1. annotate + decide
q = json.loads(json.dumps(EMAIL_Q))
policy.annotate(q, "personal", "operator")
check("a send card offers only `once`", q["grants"] == ["once"] and "always_rule" not in q, str(q.get("grants")))
check("a send card carries a caution by code", "outside the house" in str(q.get("caution")), q.get("caution"))
d = policy.decide(q, "personal")
check("decide: a send is forever gated -> ask", d["verdict"] == "ask" and d["source"] == "forever_gated", str(d))

q = decision()
policy.annotate(q, "personal", "operator")
check("a decision card offers every scope and an always rule", "always" in q["grants"] and "From now on" in q["always_rule"]
      and q["reason"] == "nothing standing covers this", str(q))
check("the always rule names the recommended choice without the tag", "File it" in q["always_rule"] and "Recommended" not in q["always_rule"], q["always_rule"])

d = policy.decide({"header": "Pay the deposit", "question": "Pay the deposit of 400 to the vendor?", "lane": "personal",
                   "options": [{"label": "Pay (Recommended)"}, {"label": "Skip"}]}, "personal")
check("decide: money words -> ask whatever else says", d["verdict"] == "ask" and d["source"] == "protected_words", str(d))
d = policy.decide(decision(), "personal", trigger={"kind": "mail", "authenticated": False})
check("decide: an unauthenticated mail set it off -> ask", d["verdict"] == "ask" and d["source"] == "untrusted_trigger", str(d))
d = policy.decide(decision(), "personal", trigger={"marker": True})
check("decide: the injection marker -> ask", d["verdict"] == "ask" and d["source"] == "untrusted_trigger", str(d))
d = policy.decide({"header": "Book site walkthrough", "question": "Book the site walkthrough for the client on Friday?", "lane": "personal",
                   "options": [{"label": "Book it (Recommended)"}, {"label": "Skip"}]}, "personal")
check("decide: the owner's own ask-first rule wins -> ask", d["verdict"] == "ask" and d["source"] == "owner_rule" and "r1" in d["why"], str(d))

# ---------------------------------------------------------------- 2. a grant from a tap, then the policy answers
r = cards.post([decision()], "operator")
check("a decision card posts and is not auto-answered yet", r["posted"] and not r.get("auto"), str(r))
qid = r["qid"]
a = cards.answer(qid, "File it (Recommended)", by=OWNER, grant="always")
check("the tap with grant=always stands", a["ok"] and a.get("grant", "").startswith("g"), str(a))
gs = policy.active_grants()
check("one active grant, keyed on header+lane", len(gs) == 1 and gs[0]["scope"] == "always" and gs[0]["key"].startswith("file the vendor invoice|personal"), str(gs))
d = policy.decide(decision(), "personal")
check("decide: the grant allows the same card next time", d["verdict"] == "allow" and d["source"] == "grant" and d["option"] == "File it (Recommended)", str(d))
time.sleep(0.05)
r2 = cards.post([decision()], "operator")
check("the next card is answered by the policy, by code", r2["posted"] and r2.get("auto") and r2.get("option") == "File it (Recommended)", str(r2))
ans = cards._answers_index().get(r2["qid"]) or {}
check("the answer is signed policy:<grant>", str(ans.get("by", "")).startswith("policy:g"), str(ans))
led = [json.loads(ln) for ln in es.LEDGER.read_text(encoding="utf-8").splitlines() if ln.strip()]
check("the ledger has a card_auto row with the reason", any(x.get("event") == "card_auto" and x.get("qid") == r2["qid"] and x.get("source") == "grant" for x in led))
d = policy.decide(decision(subject="other-vendor"), "personal")
check("a different subject under the same header is NOT covered", d["verdict"] == "ask", str(d))

a = cards.post([json.loads(json.dumps(EMAIL_Q))], "operator")
check("a send card is never auto-answered", a["posted"] and not a.get("auto"))
a2 = cards.answer(a["qid"], "Send (Recommended)", by=OWNER, grant="always")
check("grant=always on a send is refused but the tap still counts", a2["ok"] and a2.get("grant") == "" and "always needs a tap" in a2.get("grant_reason", ""), str(a2))
check("still exactly one grant", len(policy.active_grants()) == 1)

# until / this_task
g = policy.grant(decision("Archive the newsletter"), "personal", "until", OWNER, until="3 days")
check("grant until '3 days' parses", g["ok"] and g.get("until"), str(g))
g2 = policy.grant(decision("Archive the newsletter"), "personal", "until", OWNER, until="someday")
check("grant until a bad date is refused with words", not g2["ok"] and "could not read" in g2["reason"], str(g2))
g3 = policy.grant(decision("Sort the receipts"), "personal", "this_task", OWNER)
check("this_task needs a task/thread", not g3["ok"], str(g3))
g4 = policy.grant(decision("Sort the receipts"), "personal", "this_task", OWNER, task="spaces/X/threads/1")
check("this_task grant is tied to its thread", g4["ok"] and policy.find_grant(policy.key_for(decision("Sort the receipts"), "personal"), task="spaces/X/threads/1")
      and not policy.find_grant(policy.key_for(decision("Sort the receipts"), "personal"), task="spaces/Y/threads/2"))
exp = [x for x in policy.active_grants() if x["scope"] == "until"][0]
check("an expired grant is not active", not policy.find_grant(exp["key"], now=exp["until_ts"] + 10))
g5 = policy.grant(decision("Drop it test"), "personal", "always", OWNER, option="Skip")
check("a standing NO is refused (a no is already final)", not g5["ok"], str(g5))

# undo through the change ledger
ch = [c for c in core.changes_since(1) if c.get("kind") == "grant"]
check("every grant is a ledger change with an undo", len(ch) >= 3 and all(c["undo"].get("op") == "grant_revoke" for c in ch), str(len(ch)))
first = next(c for c in ch if "vendor invoice" in c["summary"])
msg = core.undo(first["id"], by=OWNER)
check("/undo revokes the grant", msg.startswith("Undone") and policy.decide(decision(), "personal")["verdict"] == "ask", msg)
check("render_grants lists what stands, in words", "Archive the newsletter" in policy.render_grants() and "File the vendor" not in policy.render_grants())

# ---------------------------------------------------------------- 3. origin + expiry + the fixed line
r = cards.post([decision("Routine check A")], "operator", origin="routine")
c = cards._read(cards.LIVE / f"{r['qid']}.json")
check("a routine-born card delivered now dies in 30 min", c["origin"] == "routine" and c["wait_s"] == policy.ROUTINE_EXPIRES_S, str(c.get("wait_s")))
r_chat = cards.post([decision("Chat ask B")], "operator", asked=True)
c2 = cards._read(cards.LIVE / f"{r_chat['qid']}.json")
check("a card an owner asked for in chat never expires", c2["origin"] == "chat" and c2["wait_s"] >= 300 * 86400, str(c2.get("wait_s")))
r_env = None
os.environ["ASSISTANT_CARD_ORIGIN"] = "routine"
try:
    r_env = cards.post([decision("Routine check C")], "operator")
finally:
    os.environ.pop("ASSISTANT_CARD_ORIGIN", None)
c3 = cards._read(cards.LIVE / f"{r_env['qid']}.json")
check("the process-wide origin (routines.tick / the spine) is read", c3["origin"] == "routine")
c["ts"] = time.time() - policy.ROUTINE_EXPIRES_S - 60
cards._write(cards.LIVE / f"{r['qid']}.json", c)
n = cards.expire()
dead = cards._read(cards.DONE / f"{r['qid']}.json")
check("expire() tombstones it with no_retry", n >= 1 and dead and dead.get("status") == "expired" and dead.get("no_retry") is True, str(n))
check("the chat card is still open", (cards._read(cards.LIVE / f"{r_chat['qid']}.json") or {}).get("status") == "open")
s = cards.answer(r_chat["qid"], "Skip", by=OWNER)
time.sleep(0.01)
cards.UNDO_S = 0
rows = cards.harvest()
sk = next((h for h in rows if h["qid"] == r_chat["qid"]), None)
check("a Skip harvests with the ONE fixed no-retry line", sk and sk.get("instruction") == policy.NO_RETRY_LINE and sk.get("origin") == "chat", str(sk))
check("the fixed line forbids retry, re-ask and rewording", all(w in policy.NO_RETRY_LINE for w in ("retry", "re-ask", "reword")))

# ---------------------------------------------------------------- 4. initiator kind
check("initiator: an owner's key", es.initiator_kind(OWNER) == "owner" and es.initiator_kind(f"gchat:{OWNER}") == "owner")
check("initiator: a policy answer is standing", es.initiator_kind("policy:g12") == "standing")
check("initiator: drain/code is system; the sweep is schedule", es.initiator_kind("drain") == "system" and es.initiator_kind("cards.sweep") == "schedule")
es.set_initiator("staff")
check("initiator: the thread's set_initiator", es.initiator_kind() == "staff")
es.set_initiator("")
os.environ["SAGE_INITIATOR"] = "schedule"
check("initiator: the process env", es.initiator_kind() == "schedule")
os.environ.pop("SAGE_INITIATOR", None)
es.ledger("probe", by=OWNER)
last = json.loads(es.LEDGER.read_text(encoding="utf-8").splitlines()[-1])
check("every ledger row carries `initiator`", last.get("initiator") == "owner", str(last))
check("the chain still verifies", es.verify_ledger()[0], es.verify_ledger()[1])
check("INITIATORS are the six kinds", set(es.INITIATORS) == {"owner", "staff", "schedule", "inbound", "standing", "system"})

# ---------------------------------------------------------------- 5. OUTCOME UNKNOWN after a restart
b = es.boot_id()
check("boot_id is a stable stamp", b and b == es.boot_id())
t = es.enqueue("local", {"op": "noop-test", "args": {}}, es.APPROVER, "card:qtest000000001")
es._append(es.TICKETS, {"id": t["id"], "ts": time.strftime("%Y-%m-%dT%H:%M:%S"), "status": "running",
                        "ts_run": time.strftime("%Y-%m-%dT%H:%M:%S"), "attempts": 1, "boot": "1"})
n = drain.reap_stale_running()
cur = es.fold()[t["id"]]
check("a running ticket from an earlier boot is settled at once as OUTCOME UNKNOWN", n == 1 and cur["status"] == "exception" and "OUTCOME UNKNOWN" in cur["result"], str(cur.get("result")))
t2 = es.enqueue("local", {"op": "noop-test-2", "args": {}}, es.APPROVER, "card:qtest000000002")
es._append(es.TICKETS, {"id": t2["id"], "ts": time.strftime("%Y-%m-%dT%H:%M:%S"), "status": "running",
                        "ts_run": time.strftime("%Y-%m-%dT%H:%M:%S"), "attempts": 1, "boot": es.boot_id()})
n = drain.reap_stale_running()
check("a fresh running ticket on THIS boot is left alone", n == 0 and es.fold()[t2["id"]]["status"] == "running")

print(("ALL OK" if not FAILS else f"{len(FAILS)} FAILED: {FAILS}") + f"  (scratch {TMP})")
sys.exit(1 if FAILS else 0)

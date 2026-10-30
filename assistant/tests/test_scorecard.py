"""Offline tests for WP6 (2026-10-24): the scorecard, graduation and the conversion watch. Scratch stores; chat off.
   python assistant/tests/test_scorecard.py
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
TMP = Path(tempfile.mkdtemp(prefix="wv_score_"))
os.environ["ASSISTANT_DATA"] = str(TMP / "adata")
os.environ["ASSISTANT_CARDS"] = str(TMP / "cards")
os.environ["ASSISTANT_LEARNINGS"] = str(TMP / "learnings")
os.environ["CHAT_PUSH"] = "off"
os.environ["RULINGS_FILE"] = str(ROOT / "data" / "rulings.json")
os.environ["WV_EVAL_LEDGER"] = str(TMP / "eval.jsonl")
(TMP / "adata").mkdir(parents=True)
(TMP / "learnings").mkdir(parents=True)
import shutil
shutil.copy(ROOT / "data" / "sop_map.json" if (ROOT / "data" / "sop_map.json").is_file() else ROOT / "templates" / "data" / "sop_map.json", TMP / "adata" / "sop_map.json")     # triage reads its vocabulary from DATA
sys.path.insert(0, str(ROOT / "scripts"))
import cards  # noqa: E402
import exec_store as es  # noqa: E402
import graduation  # noqa: E402
for _k, _req in (("acme_set_stage", [("workspace_id", "project_id"), ("stage",)]), ("acme_message", [("workspace_id",), ("body",)])):
    if _k not in es.KIND_LANES:                                # no CRM plugin here (the bare engine): a stand-in kind
        es.KIND_LANES[_k] = "crm"
        es.REQUIRED_KEYS[_k] = _req
import outcomes as oc  # noqa: E402
import scorecard  # noqa: E402

FAILS = []


def check(name, cond, extra=""):
    print(("ok   " if cond else "FAIL ") + name + (f"  {extra}" if extra and not cond else ""))
    if not cond:
        FAILS.append(name)


def post_exec(kind, payload, header, approve="Approve", skip="Skip", owner="operator"):
    q = {"question": f"[SHEET CELLS]\n{json.dumps(payload)}" if kind == "sheet_set" else f"x {json.dumps(payload)}", "header": header,
         "lane": "wedding", "audience": "internal", "options": [{"label": approve}, {"label": skip}],
         "exec": {"kind": kind, "approve_label": approve, "payload": payload}}
    return cards.post([q], owner, lane="wedding")


# ------------------------------------------------------------------ 1. the scorecard reads the ledger and the cards
for i in range(4):
    r = oc.claim("email_send", f"tk-e{i}", f"mail {i}", lane="wedding")
    oc.set_state(r["id"], "verified" if i < 3 else "failed", {}, "server accepted it" if i < 3 else "connector refused: is not a function")
r = oc.claim("acme_message", "tk-h1", "crm", lane="wedding")
oc.set_state(r["id"], "failed", {}, "connector write unverified (timed out)")
oc.said_without_deed("owner1", "dm:owner1", "", "I've filed it on their page.", ["note"])
sc = scorecard.compute(7)
check("kinds counted with a kept rate", sc["kinds"]["email_send"]["kept_pct"] == 75 and sc["kinds"]["acme_message"]["failed"] == 1, sc["kinds"])
check("failure classes named", sc["failure_classes"].get("connector") == 1 and sc["failure_classes"].get("timeout") == 1 and sc["failure_classes"].get("model-claimed") == 1, sc["failure_classes"])
check("the weakest kind is the one with the lowest rate (3+ verdicts)", sc["worst_kind"] == "email_send", sc["worst_kind"])
check("said-without-deed counted", sc["said_without_deed"] == 1)
text = scorecard.render(sc)
check("renders one plain paragraph", text.startswith("Scorecard:") and "60% confirmed landed" in text and "connector" in text, text)
rows = scorecard.dream_rows(sc)
check("dream rows carry the paragraph and the failure classes", rows[0].startswith("SCORECARD Scorecard:") and any("failure class connector" in r for r in rows), rows)
scorecard.record(sc)
check("recorded to the trend store and the eval ledger", (TMP / "adata" / "scorecard.jsonl").exists() and (TMP / "eval.jsonl").exists())

# ------------------------------------------------------------------ 2. graduation: offered only after enough untouched approvals, never for a send
c = graduation.cfg()
check("sends, money and the doer never graduate", "email_send" not in c["classes"] and "imessage_send" not in c["classes"] and "ads_apply" not in c["classes"])
graduation.cfg = lambda: {**c, "min_samples": 3, "classes": list(c["classes"]) + ["acme_set_stage"]}   # a CRM connector's stage move may graduate
ok, why = graduation.eligible("acme_set_stage", graduation.cfg())
check("not eligible with no taps", not ok and "0 of 3" in why, why)
qids = []
for i in range(3):
    r = post_exec("acme_set_stage", {"workspace_id": "0123456789abcdef0123456%d" % i, "stage": "Booked"}, f"Move {i}", approve="Move it")
    check(f"stage card {i} posts", r["posted"], r)
    qids.append(r["qid"])
    cards.answer(r["qid"], "Move it")
ok, why = graduation.eligible("acme_set_stage", graduation.cfg())
check("eligible after 3 untouched approvals", ok, why)
# one failed outcome of that kind blocks it
r = oc.claim("acme_set_stage", "tk-s1", "stage", lane="wedding")
oc.set_state(r["id"], "failed", {}, "the read-back shows another stage")
ok2, why2 = graduation.eligible("acme_set_stage", graduation.cfg())
check("a failed outcome in the window blocks graduation", not ok2 and "1 failed outcomes" in why2, why2)
oc.STORE.write_text("\n".join(ln for ln in oc.STORE.read_text(encoding="utf-8").splitlines() if '"ref": "tk-s1"' not in ln and r["id"] not in ln) + "\n", encoding="utf-8")
t = graduation.tick(dry=False)
check("tick offers ONE decide card for the eligible kind", t["offered"] == ["acme_set_stage"], t)
offered = [x for x in cards.pending() if x["header"].startswith("Make it automatic")]
check("the card recommends automatic first and names the kind", offered and offered[0]["options"][0].startswith("Make it automatic"), offered)
t2 = graduation.tick(dry=False)
check("not offered twice inside 30 days", t2["offered"] == [], t2)
cards.answer(offered[0]["qid"], "Make it automatic (Recommended)")
cards.UNDO_S = 0
sw = cards.sweep()
cards.UNDO_S = 30
check("the tap grants it by code, no operator ticket", graduation.granted("acme_set_stage") and sw["queued"] == 0, (graduation._state(), sw))
# a graduated gate runs as a standing action in triage (a fake CRM connector: the engine ships none)
import types as _types  # noqa: E402
import alib as _alib  # noqa: E402
import crm as _crm  # noqa: E402
_FAKE = _types.SimpleNamespace(CRM=True, SERVICE="AcmeCRM", STAGE_KINDS={"acme_set_stage"}, PARTICIPANT_KINDS={"acme_add_participant"},
                               MESSAGE_KINDS={"acme_message"}, owns_kind=lambda k: str(k).startswith("acme_"), valid_thread=lambda s: bool(s))
_alib.plugins = lambda: [_FAKE]
_crm._plugins = lambda: [_FAKE]
_crm._ALIASES = None
import triage_next as tn
_V = {**tn.vocab(), "actions": {"lost": [{"do": "acme_set_stage", "gate": "card", "needs": "workspace", "reason": "went another direction"},
                                         {"do": "followups_stop", "gate": "auto", "why": "went another direction"}]}}   # the connector's stage move for a lost lead
pl = tn.plan([{"type": "lost", "quote": "went another direction"}], {"kind": "booked", "unit": "u1", "workspace_id": "fa8da98a5bdb77b4902cbb43", "couple": "A & B"}, _V)
stage = next(a for a in pl if a["do"] == "acme_set_stage")
check("triage runs a granted kind as auto", stage["gate"] == "auto" and stage.get("graduated"), stage)
acted = tn.act([stage], {"kind": "booked", "unit": "u1", "workspace_id": "fa8da98a5bdb77b4902cbb43", "couple": "A & B"}, [{"type": "lost", "quote": "x"}], None, dry=False)
check("...minting under the graduation's standing ref", "queued (graduated)" in acted[0]["detail"] and any("standing:graduation acme_set_stage" in (x.get("approval_ref") or "") for x in es.fold().values()), acted)
# one failed outcome demotes it
r = oc.claim("acme_set_stage", "tk-s2", "stage", lane="wedding")
oc.set_state(r["id"], "failed", {}, "the read-back shows another stage")
t3 = graduation.tick(dry=False)
check("a failed outcome demotes the granted kind back to a card", t3["demoted"] == ["acme_set_stage"] and not graduation.granted("acme_set_stage"), t3)

# ------------------------------------------------------------------ 3. conversion watch
for i in range(6):
    rr = cards.post([{"question": f"Chase item {i}: still waiting", "header": f"Chase {i}", "lane": "personal", "audience": "personal",
                      "options": [{"label": "Bring it back up (Recommended)"}, {"label": "Drop it"}]}], "tracking.chase", lane="personal")
    if rr.get("posted"):
        cards.answer(rr["qid"], "Drop it")
sc2 = scorecard.compute(7)
loop = sc2["loops"].get("tracking.chase") or {}
check("a loop whose cards are all dropped is IGNORED", loop.get("verdict") == "IGNORED" and loop["generated"] >= 5, loop)
check("the digest names it", "tracking.chase" in scorecard.render(sc2))

print()
print("ALL PASSED" if not FAILS else f"{len(FAILS)} FAILED: {FAILS}", f"({TMP})")
sys.exit(1 if FAILS else 0)

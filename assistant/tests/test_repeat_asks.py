"""Offline tests: one decision is asked once, whatever header the producer gives it (decision_keys.py).
Each case replays a repeat from the live archive (2026-10-15..19). Scratch data dir; never sends.
    python assistant/tests/test_repeat_asks.py
"""
import os as _os, tempfile as _tf
_os.environ.setdefault("WV_LOG_DIR", _tf.mkdtemp(prefix="wv_logs_"))   # never the live ~/.claude logs
import json
import os
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
SCRIPTS = HERE.parent / "scripts"
TMP = Path(tempfile.mkdtemp(prefix="wv_repeat_"))
os.environ["ASSISTANT_DATA"] = str(TMP)
os.environ["ASSISTANT_CARDS"] = str(TMP / "cards")
os.environ["ASSISTANT_LEARNINGS"] = str(TMP / "learnings")
os.environ["RULINGS_FILE"] = str(TMP / "rulings.json")
SNAP = TMP / "crm.json"
os.environ["CRM_SNAPSHOT_FILE"] = str(SNAP)
sys.path.insert(0, str(SCRIPTS))
sys.path.insert(0, str(SCRIPTS / "tracking"))
(TMP / "rulings.json").write_text("{\"rulings\": []}", encoding="utf-8")

EMERY, ERIN, KIT, ARDEN = "7a526f92a67810e84d91c914", "18af48f52af3a9ad6806ffe1", "0625fb0f0233ee9e30f952d6", "03ca993ad76ecf08ab4acf52"


def snap(activity=None):
    activity = activity or {}
    SNAP.write_text(json.dumps({"ts": "2026-10-19T07:00:00Z", "projects": [
        {"id": pid, "project_id": pid[:-1] + "0", "name": f"{n}'s Project", "status": "lead_sent",
         "stage_moved_at": "2026-09-25T02:11:51+00:00", "last_activity_at": activity.get(pid, "2026-10-02T00:38:23+00:00")}
        for pid, n in ((EMERY, "Finley Larkspur"), (ERIN, "Indy Greystone"), (KIT, "Avery Elmswood"), (ARDEN, "Blake Northcott"))]}),
        encoding="utf-8")
    os.utime(SNAP, None)


snap()
# a fake CRM connector (the engine ships none): installed before the engine modules read its kinds
import types as _types  # noqa: E402
import alib as _alib  # noqa: E402
import crm as _crm  # noqa: E402
_FAKE = _types.SimpleNamespace(CRM=True, SERVICE="AcmeCRM", PREVIEW_TAG="[ACME PREVIEW]", ENTITY_PREFIX_ALIASES={"ac"},
                               MESSAGE_KINDS={"acme_message"}, KIND_LANES={"acme_message": "crm"},
                               REQUIRED_KEYS={"acme_message": [("workspace_id",), ("body",)]}, OUTBOUND_KINDS={"acme_message"},
                               owns_kind=lambda k: str(k).startswith("acme_"), path_ok=lambda k: (True, ""), valid_thread=lambda s: bool(s))
_alib.plugins = lambda: [_FAKE]
_crm._plugins = lambda: [_FAKE]
_crm._ALIASES = None
import cards  # noqa: E402
import decision_keys as dk  # noqa: E402
import exec_store as es  # noqa: E402

FAILS = []


def check(name, cond, extra=""):
    print(("ok   " if cond else "FAIL ") + name + (f"  {extra}" if extra and not cond else ""))
    if not cond:
        FAILS.append(name)


def decision(header, question, rec="Draft a nudge (Recommended)", other="Drop the lead", **kw):
    return dict({"header": header, "lane": "wedding", "question": question,
                 "options": [{"label": rec}, {"label": other}]}, **kw)


def text_card(header, to, body, lane="personal"):
    return {"header": header, "lane": lane, "audience": "personal",
            "question": f"Hollis texted \"6 : 4 5\".\n[TEXT PREVIEW] To {to}:\n{body}",
            "options": [{"label": "Send (Recommended)"}, {"label": "Skip"}],
            "exec": {"kind": "imessage_send", "approve_label": "Send (Recommended)",
                     "payload": {"account": "owner1-iphone", "to": to, "body": body}}}


def post(q, owner="operator", **kw):
    return cards.post([q], owner, lane=q.get("lane", ""), **kw)


# --- entity keys -----------------------------------------------------------------------------
check("full client name -> the CRM lead", dk.entities({"question": "Finley Larkspur's proposal has sat 10 days"}) == {f"crm:{EMERY}"})
check("a first name alone keys nobody", dk.entities({"question": "Emery asked about dates"}) == set())
check("chain key lead:<id>:<gate> -> the same lead", dk.entities({"chain_key": f"lead:{ERIN}:contract"}) == {f"crm:{ERIN}"})
check("the connector's older key prefix -> the workspace key", dk.entities({"entity": f"ac:{ERIN[:-1]}0"}) == {f"crm:{ERIN}"})
check("a digest naming 4 people is about none of them (never locks one)",
      dk.focus({"question": "Finley Larkspur, Indy Greystone, Avery Elmswood and Blake Northcott are quiet."}) == set())
check("a list in details does not count as focus", dk.focus({"question": "18 cold leads", "details": "Indy Greystone 45d"}) == set()
      and dk.entities({"question": "18 cold leads", "details": "Indy Greystone 45d"}) == {f"crm:{ERIN}"})
check("a text recipient -> phone key", dk.entities(text_card("x", "+15550783878", "hi")) == {"phone:5550783878"})

# --- 1. Emery: Drop on the chase card (10-16), then 'Emery nudge' from the operator (10-19) ----------
r = post(decision("Nudge · wedding", "Finley Larkspur's Project - proposal follow-up (7d) has waited 14 days.",
                  other="Drop it"))
check("first nudge card posts", r.get("posted"), r)
cards.answer(r["qid"], "Drop it")
r2 = post(decision("Emery nudge", "Finley Larkspur's proposal has sat quiet for 10 days. Want me to draft a short check-in?"))
check("Emery re-asked under a new header after Drop -> ALREADY_ANSWERED", not r2.get("posted") and "ALREADY_ANSWERED" in r2.get("reason", ""), r2)
check("the refusal names her and the earlier card", "Finley Larkspur" in r2.get("reason", "") and r["qid"] in r2.get("reason", ""), r2)
check("the dropped lead is latched for the sensors", f"crm:{EMERY}" in dk.dropped_leads(cards))
snap({EMERY: "2099-01-01T00:00:00+00:00"})           # she wrote back / the CRM moved after the tap
r3 = post(decision("Emery nudge", "Finley Larkspur replied in the CRM. Want me to draft an answer?", rec="Draft it (Recommended)", other="Skip"))
check("CRM activity after the Drop releases the latch", r3.get("posted"), r3)
cards.answer(r3["qid"], "Skip")
snap()

# --- 2. Cold leads: Erin dropped on the chain card (10-15), then named in 'Cold leads' (10-19) ----------
r = post(decision("Next step · Lead · Indy Greystone", "Indy Greystone: the signed contract has not come in. Book it?",
                  rec="Booked (Recommended)", other="Drop", chain_key=f"lead:{ERIN}:contract"), owner="tracking.chains")
check("chain card posts", r.get("posted"), r)
cards.answer(r["qid"], "Drop")
cold = decision("Cold leads", "18 leads have been quiet over 3 weeks. The two newest, Indy Greystone and Avery Elmswood, have "
                "proposals out. Want me to draft nudges for those two?", rec="Draft nudges for both (Recommended)", other="Skip")
r = post(cold)
check("a digest naming a dropped lead -> refused, names Erin", not r.get("posted") and "Indy Greystone" in r.get("reason", ""), r)
only = decision("Cold leads", "Avery Elmswood (51d, Jan 2027 wedding) has a proposal out. Want me to draft a nudge?")
r = post(only)
check("re-posted without her -> goes up", r.get("posted"), r)
kit_open = r.get("qid")

# --- 3. one person, one open question (any header) -------------------------------------------------
r = post(decision("Kit follow-up", "Avery Elmswood's proposal is quiet. Draft a check-in?"))
check("second open card about Kit under another header -> DUPLICATE_SUBJECT",
      not r.get("posted") and "DUPLICATE_SUBJECT" in r.get("reason", "") and kit_open in r.get("reason", ""), r)
r = post(decision("Nudge · wedding", "Cold leads has waited 1 day. Avery Elmswood (51d)", rec="Draft it now (Recommended)",
                  other="Drop it", chase_of=f"card:{kit_open}"), owner="tracking.chase")
check("the chaser may still re-surface the open card (ruling 1)", r.get("posted"), r)
r = post(decision("Arden nudge", "Blake Northcott's proposal is quiet. Draft a check-in?"))
check("a different person is a different decision", r.get("posted"), r)

# --- 4. Hollis: the same draft re-carded after the send (10-18 16:07 -> 16:10) ------------------------
UPSIDE = "ϛㄣ:9 ʇɐ noʎ ǝǝs 'ʇᴉ ʇoƃ"
r = post(text_card("Text reply: Hollis", "+15550783878", UPSIDE))
check("first reply card posts", r.get("posted"), r)
cards.answer(r["qid"], "Send (Recommended)")
check("the tap minted a send ticket", any(t.get("kind") == "imessage_send" for t in es.fold().values()))
r2 = post(text_card("Text reply: Hollis Brightwater", "+15550783878", UPSIDE))
check("same draft to the same number after the tap -> ALREADY_ANSWERED", not r2.get("posted") and "ALREADY_ANSWERED" in r2.get("reason", ""), r2)
r3 = post(text_card("Text reply: Hollis", "+15550783878", "See you there, I'll grab a table."))
check("a NEW reply to him (new text) still posts", r3.get("posted"), r3)

# --- 5. Marisa / AJ: two open reply cards for one conversation ---------------------------------------
r4 = post(text_card("Reply to Hollis", "+15550783878", "See you there! I'll grab a table."))
check("the same draft again while it is open, new header -> DUPLICATE_SUBJECT", not r4.get("posted") and "DUPLICATE_SUBJECT" in r4.get("reason", "")
      and "--replaces" in r4.get("reason", ""), r4)
r5 = post(text_card("Reply to Hollis", "+15550783878", "See you there! I'll grab a table."), replaces=r3["qid"])
check("...posted as a revision with --replaces it goes up", r5.get("posted"), r5)
r6 = post(text_card("Text Hollis: running late", "+15550783878", "Running 10 min late, sorry!"))
check("a different message to the same person is its own card", r6.get("posted"), r6)

# --- 6. a nudge offered right after an approved message to that lead (Emery 10-18 -> 10-19) -------------
CRM_BODY = "Hi Arden, just checking in on the collection. Happy to answer anything! owner1"
cards.answer(next(c["id"] for c in cards._live() if "Arden" in c["questions"][0]["header"]), "Drop the lead")
snap({ARDEN: "2099-01-01T00:00:00+00:00"})          # (he wrote back, so the drop no longer stands)
crmq = {"header": "Reply in the CRM: Arden", "lane": "wedding", "audience": "client",
       "question": f"Arden asked a question.\n[ACME PREVIEW] To Blake Northcott:\n{CRM_BODY}",
       "options": [{"label": "Send (Recommended)"}, {"label": "Skip"}],
       "exec": {"kind": "acme_message", "approve_label": "Send (Recommended)",
                "payload": {"workspace_id": ARDEN, "in_reply_to_id": "a" * 24, "body": CRM_BODY}}}
r = post(crmq)
check("CRM reply card posts", r.get("posted"), r)
cards.answer(r["qid"], "Send (Recommended)")
r = post(decision("Arden nudge", "Blake Northcott has not answered yet. Want me to draft a follow-up?"))
check("a nudge offer after an approved message to him -> ALREADY_ANSWERED", not r.get("posted") and "ALREADY_ANSWERED" in r.get("reason", ""), r)

print(f"\n{'ALL OK' if not FAILS else str(len(FAILS)) + ' FAILED'}")
sys.exit(1 if FAILS else 0)

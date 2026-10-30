"""Offline tests for the Google Chat push of cards (chat_push.py + gchat.parse_click + the
bridge's click handler). Scratch stores only; nothing reaches Google (CHAT_PUSH=off, and the
gchat module is stubbed where the bridge would call it).   python assistant/tests/test_chat_push.py
"""
import os as _os, tempfile as _tf
_os.environ.setdefault("WV_LOG_DIR", _tf.mkdtemp(prefix="wv_logs_"))   # never the live ~/.claude logs
import json
import os
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
SCRIPTS, AGENT = HERE.parent / "scripts", HERE.parent / "agent"
TMP = Path(tempfile.mkdtemp(prefix="wv_chatpush_"))
os.environ["ASSISTANT_DATA"] = str(TMP)
os.environ["ASSISTANT_CARDS"] = str(TMP / "cards")
os.environ["ASSISTANT_LEARNINGS"] = str(TMP / "learnings")
os.environ["AGENT_DATA"] = str(TMP / "agent")
os.environ["AGENT_HOME_SPACE"] = ""                         # per-owner DMs here; the home space is tested explicitly
os.environ["RULINGS_FILE"] = str(HERE.parent / "data" / "rulings.json")
sys.path.insert(0, str(SCRIPTS))
sys.path.insert(0, str(AGENT))
import cards  # noqa: E402
import chat_push  # noqa: E402
import gchat  # noqa: E402
REAL_PUSH_CARD = chat_push.push_card   # section 11 stubs it; section 12 drives the real one

FAILS = []


def check(name, cond, extra=""):
    print(("ok   " if cond else "FAIL ") + name + (f"  {extra}" if extra and not cond else ""))
    if not cond:
        FAILS.append(name)


# 1. a scratch store never pushes to Google
check("push disabled under a scratch store", not chat_push.enabled())

# 2. the card body: one button per option, carrying only qid/option/q
Q = {"question": "New quiz lead: Arden & Reese, wedding 2027-06-12, budget $8,000+. What should I do?\nSee https://x.test/a",
     "header": "Hot lead - what next?", "lane": "wedding", "details": "Pricing: Legacy $9,260 <script>",
     "options": [{"label": "Draft a text to them (Recommended)"}, {"label": "Draft an email reply"},
                 {"label": "I'll reach out myself"}, {"label": "Not a real lead"}]}
card = {"id": "q0123456789ab", "ts": 1.0, "status": "open", "questions": [Q]}
body = chat_push.card_body(card, "open")
c = body["cardsV2"][0]["card"]
btns = [w for s in c["sections"] for w in s["widgets"] if "buttonList" in w][0]["buttonList"]["buttons"]
check("one button per option, '(Recommended)' shown as the filled green button not as text",
      [b["text"] for b in btns] == [chat_push._RECOMMENDED.sub("", o["label"]).strip() for o in Q["options"]]
      and [b["onClick"]["action"]["parameters"][1]["value"] if b["onClick"]["action"]["parameters"][1]["key"] == "option" else None for b in btns][0] is not None
      and any("color" in b for b in btns) == any("(Recommended)" in o["label"] for o in Q["options"]))
params = {p["key"]: p["value"] for p in btns[0]["onClick"]["action"]["parameters"]}
check("button carries qid/option/q only", params == {"qid": "q0123456789ab", "option": "Draft a text to them (Recommended)", "q": "0"}
      and btns[0]["onClick"]["action"]["function"] == "card_answer")
kingsmere = "\n".join(w["textParagraph"]["text"] for s in c["sections"] for w in s["widgets"] if "textParagraph" in w)
check("html escaped, link kept, newline -> br", "&lt;script&gt;" in kingsmere and '<a href="https://x.test/a">' in kingsmere and "<br>" in kingsmere)
check("header: plain title, lane in the subtitle (2026-10-08 redesign)", c["header"]["title"] == "Hot lead - what next?" and c["header"]["subtitle"].startswith("Wedding"))
check("details section folded", any(s.get("collapsible") for s in c["sections"]))

# 3. answered: buttons replaced by the choice + an Undo button
body = chat_push.card_body(card, "answered", {"option": "Draft a text to them (Recommended)", "by": "gchat:owner1", "ticket": "tk-1"})
c = body["cardsV2"][0]["card"]
allb = [b for s in c["sections"] for w in s["widgets"] if "buttonList" in w for b in w["buttonList"]["buttons"]]
check("answered card has only Undo", [b["text"] for b in allb] == ["Undo"] and allb[0]["onClick"]["action"]["function"] == "card_undo")
txt = "\n".join(w["textParagraph"]["text"] for s in c["sections"] for w in s["widgets"] if "textParagraph" in w)
check("answered card names the chooser and the real send timing (never a flat 'about 35 s')", "owner1" in txt.lower() and ("Goes to your phone" in txt or "Runs at the next pass" in txt or "Held until" in txt) and "35 s" not in txt)
tcard = {"id": "q0123456789ab", "questions": [dict(Q, exec={"kind": "imessage_send", "payload": {}, "approve_label": "Send"})]}
body = chat_push.card_body(tcard, "answered", {"option": "Send", "by": "gchat:owner1", "ticket": "tk-2"})
allb = [b for s in body["cardsV2"][0]["card"]["sections"] for w in s["widgets"] if "buttonList" in w for b in w["buttonList"]["buttons"]]
check("approved text card offers Send now from phone (https link)", [b["text"] for b in allb] == ["Undo", "Send now from phone"]
      and allb[1]["onClick"]["openLink"]["url"].startswith("https://") and allb[1]["onClick"]["openLink"]["url"].endswith("/phone/run"))
# 3b. the mark on a tapped card means it really happened (Delia, 2026-10-24): queued ⏳, refused ❌, landed ✅
send_card = {"id": "q0123456789ab", "questions": [dict(Q, exec={"kind": "email_send", "payload": {}, "approve_label": "Send"})]}


def _first(b):
    """The answered card's status line (its last section's first paragraph)."""
    return [w["textParagraph"]["text"] for w in b["cardsV2"][0]["card"]["sections"][-1]["widgets"] if "textParagraph" in w][0]


t_q = _first(chat_push.card_body(send_card, "answered", {"option": "Send", "by": "owner1", "ticket": "tk-3"}))
t_m = _first(chat_push.card_body(send_card, "answered", {"option": "Send", "by": "owner1", "mint_error": "unable to process your request"}))
t_f = _first(chat_push.card_body(send_card, "answered", {"option": "Send", "by": "owner1", "ticket": "tk-3", "failed": True,
                                                  "result": "Not done: SEND_FAILED=REFUSED the member did not approve this change"}))
t_ok = _first(chat_push.card_body(send_card, "answered", {"option": "Send", "by": "owner1", "ticket": "tk-3",
                                                   "result": "Sent at 10:40 AM (message 1234abcd)."}))
t_w = _first(chat_push.card_body(send_card, "answered", {"option": "Send", "by": "owner1", "ticket": "tk-3",
                                                  "result": "The CRM took the move but the read-back shows another stage; check the pipeline."}))
t_plain = _first(chat_push.card_body(card, "answered", {"option": "I'll reach out myself", "by": "owner1"}))
check("a queued send shows ⏳, never a green check, until its result is in", t_q.startswith("⏳") and "✅" not in t_q, t_q)
check("a refused send shows ❌ and says it was not sent", t_m.startswith("❌") and "Not sent" in t_m and "✅" not in t_m, t_m)
check("a failed send shows ❌ with what went wrong", t_f.startswith("❌") and "did not approve" in t_f and "✅" not in t_f, t_f)
check("a confirmed send shows ✅ with its evidence line", t_ok.startswith("✅") and "1234abcd" in t_ok and "Runs at the next pass" not in t_ok, t_ok)
check("a send whose read-back disagrees shows ⚠️, not ✅", t_w.startswith("⚠️"), t_w)
check("a plain choice (nothing to send) keeps its ✅", t_plain.startswith("✅"), t_plain)
st = json.dumps(chat_push.stack_body([(send_card, "answered", {"option": "Send", "ticket": "tk-3"}), (card, "open", None)]), ensure_ascii=False)
check("a stack shows the queued send as ⏳ too", "⏳ Hot lead" in st and "✅ Hot lead" not in st)

body = chat_push.card_body(card, "expired")
check("expired card has no buttons", not any("buttonList" in w for s in body["cardsV2"][0]["card"]["sections"] for w in s["widgets"]))

# 4. long text is capped, never dropped
big = dict(Q, question="x" * 9000, details="y" * 9000)
body = chat_push.card_body({"id": "q0123456789ab", "questions": [big]}, "open")
kingsmere = [w["textParagraph"]["text"] for s in body["cardsV2"][0]["card"]["sections"] for w in s["widgets"] if "textParagraph" in w]
check("question/details capped", len(kingsmere[0]) <= chat_push.QUESTION_MAX + 8 and len(kingsmere[1]) <= chat_push.DETAILS_MAX + 8)

# 5. parse_click: add-on shape and classic shape
addon = {"commonEventObject": {"invokedFunction": "card_answer", "parameters": {"qid": "q0123456789ab", "option": "Skip", "q": "0"}},
         "chat": {"user": {"name": "users/123", "email": "owner3@example.invalid", "displayName": "owner1"},
                  "buttonClickedPayload": {"message": {"name": "spaces/AAA/messages/BBB", "space": {"name": "spaces/AAA", "type": "DM"}}}}}
k = gchat.parse_click(addon)
check("add-on click parsed", k and k["function"] == "card_answer" and k["params"]["qid"] == "q0123456789ab"
      and k["email"] == "owner3@example.invalid" and k["msg_name"] == "spaces/AAA/messages/BBB" and k["space"] == "spaces/AAA")
classic = {"type": "CARD_CLICKED", "user": {"name": "users/123", "email": "owner3@example.invalid"},
           "space": {"name": "spaces/AAA"}, "message": {"name": "spaces/AAA/messages/CCC"},
           "action": {"actionMethodName": "card_undo", "parameters": [{"key": "qid", "value": "q0123456789ab"}]}}
k = gchat.parse_click(classic)
check("classic click parsed", k and k["function"] == "card_undo" and k["params"] == {"qid": "q0123456789ab"} and k["msg_name"].endswith("CCC"))
check("a plain message is not a click", gchat.parse_click({"type": "MESSAGE", "message": {"text": "hi"}, "space": {"name": "spaces/AAA"}}) is None)
check("a message event is still a message", gchat.parse_event({"type": "MESSAGE", "message": {"text": "hi", "sender": {"type": "HUMAN", "email": "a@b.c"}},
                                                                 "space": {"name": "spaces/AAA", "type": "DM"}}) is not None)

# 6. the bridge's click handler answers through cards.answer and edits the Chat message
import bridge  # noqa: E402
bridge.LOG = TMP / "bridge.log"           # never the live log
r = cards.post([Q], "test", lane="wedding")
check("card posts with push off", r["posted"] and r["chat"] == [])
qid = r["qid"]
edits, posts = [], []
chat_push.mark_answered = lambda c, a: edits.append(("answered", c["id"], a["option"])) or 1
chat_push.mark_undone = lambda c: edits.append(("undone", c["id"])) or 1
bridge.post = lambda key, text: posts.append((key, text))
people = json.loads((AGENT / "people.json").read_text(encoding="utf-8-sig"))
owner1_email = people["users"]["owner1"]["emails"][0]
bridge.click({"email": owner1_email, "chat_user": "", "function": "card_answer", "space": "spaces/AAA",
              "msg_name": "spaces/AAA/messages/BBB", "params": {"qid": qid, "option": "I'll reach out myself", "q": "0"}})
live = cards._read(cards.LIVE / f"{qid}.json")
check("tap answered the card", live["status"] == "answered" and edits[-1] == ("answered", qid, "I'll reach out myself"))
ans = cards._answers_index()[qid]
check("answer recorded as a gchat tap by owner1", ans["by"] == "gchat:owner1" and ans["option"] == "I'll reach out myself")
bridge.click({"email": owner1_email, "chat_user": "", "function": "card_answer", "space": "spaces/AAA",
              "msg_name": "spaces/AAA/messages/BBB", "params": {"qid": qid, "option": "Not a real lead", "q": "0"}})
check("second tap refused quietly (a re-delivered tap, 2026-10-07): no change, nothing posted",
      cards._answers_index()[qid]["option"] == "I'll reach out myself" and not any("could not be answered" in t for _, t in posts))
bridge.click({"email": owner1_email, "chat_user": "", "function": "card_undo", "space": "spaces/AAA",
              "msg_name": "spaces/AAA/messages/BBB", "params": {"qid": qid}})
check("undo tap reopens the card", cards._read(cards.LIVE / f"{qid}.json")["status"] == "open" and edits[-1] == ("undone", qid))
n_edits = len(edits)
bridge.click({"email": "stranger@example.com", "chat_user": "users/999", "function": "card_answer", "space": "spaces/ZZZ",
              "msg_name": "spaces/ZZZ/messages/1", "params": {"qid": qid, "option": "Not a real lead", "q": "0"}})
check("a stranger's tap is ignored", cards._read(cards.LIVE / f"{qid}.json")["status"] == "open" and len(edits) == n_edits)

# 7. targets: only owners with a DM space, per lane
(TMP / "agent").mkdir(parents=True, exist_ok=True)
(TMP / "agent" / "gchat_spaces.json").write_text(json.dumps({"owner1": "spaces/J", "owner6": "spaces/H"}), encoding="utf-8")
chat_push.SPACES = TMP / "agent" / "gchat_spaces.json"
chat_push._cfg = lambda: dict(chat_push.DEFAULTS)
check("wedding cards reach both owners", chat_push.targets("wedding") == [("owner1", "spaces/J"), ("owner6", "spaces/H")])
check("owner6 lane reaches owner6 only", chat_push.targets("owner6") == [("owner6", "spaces/H")])
check("other lanes reach owner1", chat_push.targets("money") == [("owner1", "spaces/J")])
(TMP / "agent" / "gchat_spaces.json").write_text(json.dumps({"owner1": "spaces/J"}), encoding="utf-8")
check("no DM yet = not a target", chat_push.targets("wedding") == [("owner1", "spaces/J")])
check("operator reports are not pushed as notes", chat_push.push_text("OPERATOR: claimed=1") == 0)
check("no system note reaches Chat by default (owner1 2026-10-08: cards and Sage's own words only)",
      chat_push.push_text("Optimization Scout: 5 proposal(s)") == 0 and chat_push.push_text("⏰ Text unanswered 2h") == 0)

# 8. the HTTP endpoint (gchat_events): token gate, taps answered synchronously, messages queued
import gchat_events  # noqa: E402
AUD = ["5550325432", "https://x.test/gchat/events"]
ok_claims = {"iss": gchat_events.CHAT_ISSUER, "aud": "5550325432"}
check("no bearer -> refused", gchat_events.verify_bearer("", AUD, _verify=lambda t: ok_claims) is None)
check("good token -> claims", gchat_events.verify_bearer("Bearer abc", AUD, _verify=lambda t: ok_claims) is not None)
check("wrong issuer -> refused", gchat_events.verify_bearer("Bearer abc", AUD, _verify=lambda t: {"iss": "x", "aud": "5550325432"}) is None)
check("wrong audience -> refused", gchat_events.verify_bearer("Bearer abc", AUD, _verify=lambda t: {"iss": gchat_events.CHAT_ISSUER, "aud": "999"}) is None)
def _boom(t):
    raise ValueError("bad")
check("verifier exception -> refused", gchat_events.verify_bearer("Bearer abc", AUD, _verify=_boom) is None)
r = cards.post([Q], "test", lane="wedding")
qid2 = r["qid"]
ev = {"commonEventObject": {"invokedFunction": "card_answer", "parameters": {"qid": qid2, "option": "Draft an email reply", "q": "0"}},
      "chat": {"user": {"name": "users/123", "email": owner1_email, "displayName": "owner1"},
               "buttonClickedPayload": {"message": {"name": "spaces/AAA/messages/BBB", "space": {"name": "spaces/AAA", "type": "DM"}}}}}
resp = gchat_events.handle(ev)
upd = resp.get("hostAppDataAction", {}).get("chatDataAction", {}).get("updateMessageAction", {}).get("message", {})
check("http tap answers the card", cards._read(cards.LIVE / f"{qid2}.json")["status"] == "answered")
check("http tap replies with the updated card (Undo only)", bool(upd.get("cardsV2")) and
      [b["text"] for s_ in upd["cardsV2"][0]["card"]["sections"] for w in s_["widgets"] if "buttonList" in w for b in w["buttonList"]["buttons"]] == ["Undo"])
resp = gchat_events.handle(ev)
upd2 = resp.get("hostAppDataAction", {}).get("chatDataAction", {}).get("updateMessageAction", {}).get("message", {})
check("second http tap -> the card as it stands, never an empty reply (Chat shows {} as 'unable to process')",
      resp != {} and bool(upd2.get("cardsV2")) and "Draft an email reply" in json.dumps(upd2) and "createMessageAction" not in json.dumps(resp)
      and cards._answers_index()[qid2]["option"] == "Draft an email reply")
ev_u = {"commonEventObject": {"invokedFunction": "card_undo", "parameters": {"qid": qid2}},
        "chat": {"user": {"email": owner1_email}, "buttonClickedPayload": {"message": {"name": "spaces/AAA/messages/BBB", "space": {"name": "spaces/AAA"}}}}}
resp = gchat_events.handle(ev_u)
check("http undo reopens with buttons", cards._read(cards.LIVE / f"{qid2}.json")["status"] == "open" and "card_answer" in json.dumps(resp))
ev_s = json.loads(json.dumps(ev)); ev_s["chat"]["user"] = {"email": "stranger@example.com", "name": "users/9"}
resp = gchat_events.handle(ev_s)
check("stranger's http tap refused, card untouched", "for the owners" in json.dumps(resp) and cards._read(cards.LIVE / f"{qid2}.json")["status"] == "open")
classic = {"type": "CARD_CLICKED", "user": {"email": owner1_email}, "space": {"name": "spaces/AAA"}, "message": {"name": "spaces/AAA/messages/C"},
           "action": {"actionMethodName": "card_answer", "parameters": [{"key": "qid", "value": qid2}, {"key": "option", "value": "Not a real lead"}]}}
resp = gchat_events.handle(classic)
check("classic tap -> UPDATE_MESSAGE envelope", resp.get("actionResponse", {}).get("type") == "UPDATE_MESSAGE" and "cardsV2" in resp)
msg_ev = {"chat": {"messagePayload": {"message": {"text": "hi sage", "sender": {"type": "HUMAN", "email": owner1_email, "name": "users/123"},
                                                  "space": {"name": "spaces/AAA", "spaceType": "DIRECT_MESSAGE"}}}}}
resp = gchat_events.handle(msg_ev)
queued = gchat.take_inbound()
check("http message queued for the bridge, empty reply", resp == {} and len(queued) == 1 and queued[0]["text"] == "hi sage" and queued[0]["dm"])
check("inbox cursor advances", gchat.take_inbound() == [])

# 8. an owner with no DM is never skipped silently; adding the app records the DM (2026-10-06)
sp_file = TMP / "agent" / "gchat_spaces.json"
sp_file.write_text(json.dumps({"owner1": "spaces/J"}), encoding="utf-8")
check("owner6 is reported unreachable for wedding cards", chat_push.unreachable("wedding") == ["owner6"])
check("nobody is unreachable for owner1-only lanes", chat_push.unreachable("money") == [])
jo_email = people["users"]["owner6"]["emails"][0]
classic = {"type": "ADDED_TO_SPACE", "space": {"name": "spaces/JO", "type": "DM", "singleUserBotDm": True},
           "user": {"name": "users/42", "email": jo_email}}
addon = {"chat": {"user": {"name": "users/42", "email": jo_email},
                  "addedToSpacePayload": {"space": {"name": "spaces/JO2", "spaceType": "DIRECT_MESSAGE"}}}}
check("ADDED_TO_SPACE parsed (classic + add-on)", gchat.parse_added(classic)["space"] == "spaces/JO" and gchat.parse_added(addon)["space"] == "spaces/JO2"
      and gchat.parse_added({"type": "MESSAGE"}) is None)
r_add = gchat_events.handle(classic)
check("owner6 adding Sage records her DM and gets a hello", json.loads(sp_file.read_text(encoding="utf-8")).get("owner6") == "spaces/JO"
      and "Connected" in json.dumps(r_add) and chat_push.unreachable("wedding") == [])
stranger = {"type": "ADDED_TO_SPACE", "space": {"name": "spaces/X", "type": "DM"}, "user": {"name": "users/9", "email": "stranger@example.com"}}
check("a stranger adding the app records nothing, no reply", gchat_events.handle(stranger) == {}
      and "spaces/X" not in sp_file.read_text(encoding="utf-8"))
room = {"type": "ADDED_TO_SPACE", "space": {"name": "spaces/ROOM", "type": "ROOM"}, "user": {"name": "users/42", "email": jo_email}}
gchat_events.handle(room)
check("added to a shared room is not a DM: not recorded", json.loads(sp_file.read_text(encoding="utf-8")).get("owner6") == "spaces/JO")

# 9. "Edit & send": the dialog opens prefilled with the outgoing text; submitting approves with the edited text
QT = {"header": "Text reply", "lane": "personal",
      "question": "Reply to +18055550100?\n[TEXT PREVIEW] To +18055550100:\nOriginal draft text.",
      "options": [{"label": "Send (Recommended)"}, {"label": "Skip"}],
      "exec": {"kind": "imessage_send", "payload": {"account": "owner1-iphone", "to": "+18055550100", "body": "Original draft text."},
               "approve_label": "Send (Recommended)"}}
r = cards.post([QT], "test")
qid3 = r["qid"]
body3 = chat_push.card_body(cards._read(cards.LIVE / f"{qid3}.json"), "open")
btns3 = [b for s_ in body3["cardsV2"][0]["card"]["sections"] for w in s_["widgets"] if "buttonList" in w for b in w["buttonList"]["buttons"]]
edit_btn = next((b for b in btns3 if b["text"] == "Edit & send"), None)
check("exec card carries an Edit & send button that opens a dialog",
      edit_btn is not None and edit_btn["onClick"]["action"]["interaction"] == "OPEN_DIALOG" and edit_btn["onClick"]["action"]["function"] == "card_edit")
check("a card without outgoing text has no Edit button", "Edit & send" not in json.dumps(chat_push.card_body(cards._read(cards.LIVE / f"{qid2}.json"), "open")))
ev_e = {"commonEventObject": {"invokedFunction": "card_edit", "parameters": {"qid": qid3, "q": "0"}},
        "chat": {"user": {"name": "users/123", "email": owner1_email}, "buttonClickedPayload": {"message": {"name": "spaces/AAA/messages/E", "space": {"name": "spaces/AAA"}}}}}
resp = gchat_events.handle(ev_e)
dlg = json.dumps(resp)
check("edit tap opens a dialog prefilled with the draft", "pushCard" in dlg and "Original draft text." in dlg and "card_edit_submit" in dlg)
ev_sub = {"commonEventObject": {"dialogEventType": "SUBMIT_DIALOG", "parameters": {"qid": qid3, "q": "0"},
                                "formInputs": {"body": {"stringInputs": {"value": ["My own words instead."]}}}},
          "relayFunction": "card_edit_submit",
          "chat": {"user": {"name": "users/123", "email": owner1_email}, "buttonClickedPayload": {"message": {"name": "spaces/AAA/messages/E", "space": {"name": "spaces/AAA"}}}}}
resp = gchat_events.handle(ev_sub)
ans3 = cards._answers_index().get(qid3) or {}
check("dialog submit approves the card with the edited text",
      cards._read(cards.LIVE / f"{qid3}.json")["status"] == "answered" and ans3.get("option") == "Send (Recommended)"
      and ans3.get("revised_text") == "My own words instead." and ans3.get("by") == "gchat:owner1")
check("dialog closes with a confirmation", "CLOSE_DIALOG" in json.dumps(resp) and "Approved with your text" in json.dumps(resp))
check("the Chat message is rewritten with the edited text", edits and edits[-1][0] == "answered" and edits[-1][1] == qid3)
ev_empty = json.loads(json.dumps(ev_sub)); ev_empty["commonEventObject"]["formInputs"]["body"]["stringInputs"]["value"] = ["   "]
check("an empty edit sends nothing", "Nothing sent" in json.dumps(gchat_events.handle(ev_empty)))

# 10. Marisa, 2026-10-18: a draft with [the owner: ...] blanks never goes out as written
import exec_store as es  # noqa: E402
BL = "Hey! On the gift exchange: [owner1: yes / no]. For the holidays, [owner1: our plans]."
QB = {"header": "Text reply: Marisa", "lane": "personal",
      "question": "Marisa asked about the gift exchange.\n[TEXT PREVIEW] To +18055550111:\n" + BL,
      "options": [{"label": "Edit & send (Recommended)"}, {"label": "Skip"}],
      "exec": {"kind": "imessage_send", "payload": {"account": "owner1-iphone", "to": "+18055550111", "body": BL},
               "approve_label": "Edit & send (Recommended)"}}
r = cards.post([QB], "test")
check("EDIT_LABEL: a one-tap approve named 'Edit & send' is refused at post", not r["posted"] and "EDIT_LABEL" in r["reason"], r)
QB["options"][0]["label"] = QB["exec"]["approve_label"] = "Send (Recommended)"
r = cards.post([QB], "test")
check("the same draft with a plain Send label posts", r["posted"], r)
qidb = r["qid"]
bodyb = chat_push.card_body(cards._read(cards.LIVE / f"{qidb}.json"), "open")
btnsb = [b for s_ in bodyb["cardsV2"][0]["card"]["sections"] for w in s_["widgets"] if "buttonList" in w for b in w["buttonList"]["buttons"]]
names = [b["text"] for b in btnsb]
check("a draft with blanks shows no one-tap Send, only Edit & send (filled) and Skip",
      "Send" not in names and names[0] == "Edit & send" and btnsb[0].get("color") and "Skip" in names, names)
check("the draft header says it has blanks", "has blanks" in json.dumps(bodyb))
ra = cards.answer(qidb, "Send (Recommended)", by="gchat:owner1")
check("a direct approve of the unfilled draft is refused and the card stays open",
      not ra["ok"] and ra.get("blanks") and cards._read(cards.LIVE / f"{qidb}.json")["status"] == "open", ra)
ev_b = json.loads(json.dumps(ev_sub)); ev_b["commonEventObject"]["parameters"]["qid"] = qidb
ev_b["commonEventObject"]["formInputs"]["body"]["stringInputs"]["value"] = ["Hey! Count us in. For the holidays, [owner1: our plans]."]
resp = gchat_events.handle(ev_b)
check("an edit that leaves a blank keeps the dialog open with his words and the error",
      "card_edit_submit" in json.dumps(resp) and "Count us in" in json.dumps(resp) and "still a blank" in json.dumps(resp)
      and cards._read(cards.LIVE / f"{qidb}.json")["status"] == "open", json.dumps(resp)[:300])
ev_b["commonEventObject"]["formInputs"]["body"]["stringInputs"]["value"] = ["Hey! Count us in. We're home for Christmas."]
resp = gchat_events.handle(ev_b)
ansb = cards._answers_index().get(qidb) or {}
check("a fully filled edit approves and mints", "Approved with your text" in json.dumps(resp) and ansb.get("ticket"), ansb)
try:
    es.enqueue("imessage_send", {"account": "owner1-iphone", "to": "+18055550122", "body": "See you [TODO time]"}, "owner1", "card:test")
    refused = False
except ValueError as e:
    refused = "unfilled blank" in str(e)
check("exec_store refuses to mint an outbound payload with a blank", refused)
check("blank detector: real texts pass, placeholders are caught",
      es.unfilled_blanks({"body": "Call me at 3 [lol] - see you {soon}"}) == []
      and len(es.unfilled_blanks({"subject": "Hi {{first_name}}", "body": "[owner1: confirm date] <<price>>"})) == 3)

# 11. the owner 2026-10-19: "make the buttons instantly work and then only tell me later if the requests
# didn't work with a fresh approval"
import threading  # noqa: E402
import time as _time  # noqa: E402
said, repushed = [], []
chat_push.push_say = lambda text, lane="", thread="": said.append(text) or 1
chat_push.push_card = lambda c: repushed.append(c["id"]) or {"owner1": "spaces/AAA/messages/NEW"}
real_push_result = chat_push.push_result
chat_push.push_result = lambda c, text: said.append(text) or 1
real_answer = cards.answer
gate = threading.Event()


def slow_answer(*a, **kw):
    gate.wait(5)
    return real_answer(*a, **kw)


gchat_events.TAP_WAIT_S = 0.2
r = cards.post([dict(Q, header="Slow tap")], "test", lane="wedding")
qs = r["qid"]
repushed.clear()
ev_slow = json.loads(json.dumps(ev)); ev_slow["commonEventObject"]["parameters"] = {"qid": qs, "option": "Draft an email reply", "q": "0"}
cards.answer = slow_answer
t0 = _time.time()
resp = gchat_events.handle(ev_slow)
quick = _time.time() - t0
upd = resp.get("hostAppDataAction", {}).get("chatDataAction", {}).get("updateMessageAction", {}).get("message", {})
check("a slow tap answers Chat at once with the choice shown (Undo only)",
      quick < 1.5 and bool(upd.get("cardsV2")) and "Draft an email reply" in json.dumps(upd)
      and [b["text"] for s_ in upd["cardsV2"][0]["card"]["sections"] for w in s_["widgets"] if "buttonList" in w for b in w["buttonList"]["buttons"]] == ["Undo"],
      f"{quick:.2f}s")
gate.set()
for t in [t for t in threading.enumerate() if t.name.startswith("tap-")]:
    t.join(5)
check("...and the work finishes behind it, silently when it worked",
      cards._read(cards.LIVE / f"{qs}.json")["status"] == "answered" and not said and not repushed, (cards._read(cards.LIVE / f"{qs}.json")["status"], said, repushed))

# the owner 2026-10-25 (Corwin's resend, error at 5:36, sent at 5:40): "I don't want it to say, unable to process
# request and then work a little bit later it should not say the error". The relay delivered that one tap
# twice; the second got {} back, which Chat shows as the error. A slow tap shows ⏳ working on it, a duplicate
# delivery shows the same, and the card flips to the real outcome when the result lands.
check("the slow tap's card says it is working on it (⏳), not ✅", "⏳" in json.dumps(upd, ensure_ascii=False)
      and "Working on it" in json.dumps(upd), json.dumps(upd, ensure_ascii=False)[:300])
check("...and flips to the real outcome by itself when the result lands", edits and edits[-1][:2] == ("answered", qs), edits[-3:])
gate.clear()
r = cards.post([dict(Q, header="Double-delivered tap")], "test", lane="wedding")
qd = r["qid"]
calls = []


def first_slow(qid, option, q_index=0, by="owner1", **kw):
    """The first delivery holds the card's answering lock while its send is slow; later ones are the real path."""
    calls.append(qid)
    if len(calls) > 1:
        return real_answer(qid, option, q_index=q_index, by=by, **kw)
    mark = cards.ROOT / "locks" / f"{qid}.answering"
    mark.parent.mkdir(parents=True, exist_ok=True)
    mark.write_text("", encoding="utf-8")
    try:
        gate.wait(5)
        return cards._answer_locked(qid, option, q_index=q_index, by=by)
    finally:
        mark.unlink()


cards.answer = first_slow
ev_d = json.loads(json.dumps(ev)); ev_d["commonEventObject"]["parameters"] = {"qid": qd, "option": "Draft an email reply", "q": "0"}
first = {}
t1 = threading.Thread(target=lambda: first.update(gchat_events.handle(ev_d)), name="tap-first")
t1.start()
_time.sleep(0.05)
t0 = _time.time()
dup = gchat_events.handle(ev_d)
dup_s = _time.time() - t0
dj = json.dumps(dup, ensure_ascii=False)
check("the second delivery of a tap still sending: the working card, never {} or an error line",
      dup != {} and "updateMessageAction" in dj and "⏳" in dj and "could not" not in dj and "unable" not in dj
      and len(calls) == 2 and dup_s < 0.15, (dj[:300], calls, dup_s))
gate.set()
t1.join(5)
for t in [t for t in threading.enumerate() if t.name.startswith("tap-")]:
    t.join(5)
check("...and the tap itself went through once", cards._read(cards.LIVE / f"{qd}.json")["status"] == "answered"
      and cards._answers_index()[qd]["option"] == "Draft an email reply" and not said and not repushed, (said, repushed))
cards.answer = real_answer

gate.clear()
r = cards.post([dict(Q, header="Failing tap")], "test", lane="wedding")
qf = r["qid"]
repushed.clear()
cards.answer = lambda *a, **kw: (gate.wait(5), {"ok": False, "reason": "exec store locked"})[1]
ev_f = json.loads(json.dumps(ev)); ev_f["commonEventObject"]["parameters"] = {"qid": qf, "option": "Draft an email reply", "q": "0"}
gchat_events.handle(ev_f)
gate.set()
for t in [t for t in threading.enumerate() if t.name.startswith("tap-")]:
    t.join(5)
check("a slow tap that then fails comes back as the same card, fresh, with a line saying why",
      repushed == [qf] and said and "didn't go through" in said[-1] and "exec store locked" in said[-1] and "again" in said[-1], (said, repushed, qf))
cards.answer = real_answer

# a tap the relay held while Mission Control was restarting (X-Sage-Late)
said.clear(); repushed.clear(); edits.clear()
r = cards.post([dict(Q, header="Held tap")], "test", lane="wedding")
qh = r["qid"]
repushed.clear()
ev_h = json.loads(json.dumps(ev)); ev_h["commonEventObject"]["parameters"] = {"qid": qh, "option": "Not a real lead", "q": "0"}
check("a held tap is acted on when it arrives, and its card redrawn by REST",
      gchat_events.handle(ev_h, late=_time.time() - 60) == {} and cards._read(cards.LIVE / f"{qh}.json")["status"] == "answered"
      and edits and edits[-1][:2] == ("answered", qh) and not said)
r = cards.post([dict(Q, header="Stale tap")], "test", lane="wedding")
qo = r["qid"]
repushed.clear()
ev_o = json.loads(json.dumps(ev)); ev_o["commonEventObject"]["parameters"] = {"qid": qo, "option": "Not a real lead", "q": "0"}
gchat_events.handle(ev_o, late=_time.time() - 3 * 3600)
check("a tap held over 2 hours is not acted on: the card comes back fresh instead",
      cards._read(cards.LIVE / f"{qo}.json")["status"] == "open" and repushed == [qo] and "late" in said[-1], (said, repushed, qo))
said.clear()
gchat_events.handle(ev_h, late=_time.time() - 30)
check("a held tap on a card already answered the same way says nothing", not said, said)

# the drain's result line rewrites the tapped card's own message: ⏳ becomes ✅, or ❌ for 'Not done'
_saved = (chat_push.enabled, chat_push._edit_all, chat_push.card_thread, cards._answers_index)
redone = []
chat_push.enabled = lambda: True
chat_push._edit_all = lambda c, b, what: redone.append((what, _first(b))) or 1
chat_push.card_thread = lambda c: ""
cards._answers_index = lambda: {"q0123456789ab": {"qid": "q0123456789ab", "option": "Send", "by": "gchat:owner1", "ticket": "tk-3"}}
real_push_result(send_card, "Sent at 10:40 AM (message 1234abcd).")
real_push_result(send_card, "Not done: SEND_FAILED=REFUSED the member did not approve this change. The card is back on your phone.")
cards._answers_index = lambda: {"q0123456789ab": {"qid": "q0123456789ab", "undone": True}}
real_push_result(send_card, "Sent at 10:41 AM.")
chat_push.enabled, chat_push._edit_all, chat_push.card_thread, cards._answers_index = _saved
check("push_result re-marks the card: ✅ on evidence, ❌ on Not done, untouched once undone",
      [w for w, _ in redone] == ["mark_result", "mark_result"] and redone[0][1].startswith("✅") and redone[1][1].startswith("❌"), redone)

# 12. Marlo, 2026-10-24: a reply to a text an owner sent from a card goes into that card's own thread
orig = {"id": "qmatiasorig01", "ts": 1.0, "status": "answered", "chat_thread": "spaces/HIRE/threads/T1",
        "questions": [dict(Q, header="Text Marlo")]}
cards._write(cards.ARCHIVE / "qmatiasorig01.json", orig)
tk = es.enqueue("imessage_send", {"account": "owner1-iphone", "to": "+18055550188", "body": "Can you shoot Nov 7?"}, "owner1", "card:qmatiasorig01")
es.set_status(tk["id"], "done", "sent")
rq = {"header": "Text reply: Marlo", "lane": "wedding", "question": "Marlo asked about the pay for Nov 7.",
      "options": [{"label": "Send (Recommended)"}, {"label": "Skip"}],
      "exec": {"kind": "imessage_send", "payload": {"account": "owner1-iphone", "to": "+1 (805) 555-0188", "body": "It's $500."}}}
check("a reply card finds the thread of the card that sent the text", chat_push.reply_thread({"id": "qr1", "questions": [rq]}) == "spaces/HIRE/threads/T1")
check("...not for a card that is not a reply", chat_push.reply_thread({"id": "qr2", "questions": [dict(rq, header="Book Marlo", question="Book Marlo for Nov 7?")]}) == "")
check("...not for someone we never texted", chat_push.reply_thread({"id": "qr3", "questions": [dict(rq, exec={"kind": "imessage_send", "payload": {"to": "+18055550100", "body": "x"}})]}) == "")
check("...not once the send is older than the window", chat_push.reply_thread({"id": "qr4", "questions": [rq]}, hours=0) == "")
check("...and a card asked for in its own thread keeps it", chat_push.reply_thread({"id": "qr5", "chat_thread": "spaces/X/threads/Y", "questions": [rq]}) == "")
posted, _sv = [], (chat_push.enabled, chat_push._g, chat_push.targets)


class _G:
    def post_body(self, space, body, thread=""):
        posted.append((space, thread))
        return space + "/messages/M1"


chat_push.enabled, chat_push._g, chat_push.targets = (lambda: True), (lambda: _G()), (lambda lane, what: [("owner1", "spaces/DMJ"), ("owner6", "spaces/DMO")])
rc = {"id": "qr6", "questions": [rq]}
REAL_PUSH_CARD(rc)
chat_push.enabled, chat_push._g, chat_push.targets = _sv
check("push_card posts the reply card once, into the live thread", posted == [("spaces/HIRE", "spaces/HIRE/threads/T1")]
      and rc.get("chat_thread") == "spaces/HIRE/threads/T1", posted)

# a card handed on to the operator after a draft-lint refusal (2026-10-26, Odette): the operator posts it with
# --thread from the team-space ask; it lands in THAT thread of the team space, not the home space
posted.clear()
chat_push.enabled, chat_push._g, chat_push.targets = (lambda: True), (lambda: _G()), (lambda lane, what: [("home", "spaces/HOME")])
tq = {"header": "Email to Odette", "lane": "wedding", "question": "owner1 asked for the Odette follow-up.",
      "options": [{"label": "Send (Recommended)"}, {"label": "Skip"}]}
REAL_PUSH_CARD({"id": "qteam01", "chat_thread": "spaces/TEAM/threads/T9", "questions": [tq]})
check("a card asked for in a team-space thread posts under that ask, in the team space",
      posted == [("spaces/TEAM", "spaces/TEAM/threads/T9")], posted)
posted.clear()
REAL_PUSH_CARD({"id": "qhome01", "chat_thread": "spaces/HOME/threads/T2", "questions": [tq]})
check("...and one asked for in a home-space thread still posts there once", posted == [("spaces/HOME", "spaces/HOME/threads/T2")], posted)
posted.clear()
REAL_PUSH_CARD({"id": "qtop01", "questions": [tq]})
check("...and a card with no thread still goes to the home space", posted == [("spaces/HOME", "")], posted)
chat_push.enabled, chat_push._g, chat_push.targets = _sv

print(f"\n{'ALL OK' if not FAILS else 'FAILED: ' + ', '.join(FAILS)}  ({len(FAILS)} failures)")
sys.exit(1 if FAILS else 0)

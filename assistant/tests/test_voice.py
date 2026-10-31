"""Offline tests for the voice (assistant/agent/voice.py + cards.is_system, 2026-10-27): interacting with Sage feels
like interacting with a person. Scratch stores; no model, no Chat, no git. Each check replays a line or a card that
reached the owner's phone on 2026-10-24..27:
  - system talk (status, raw output, chase lines, scorecards, review summaries, slipped builds, unconfirmed) never
    reaches Chat; the switch brings it back;
  - a send result names the person and drops the Message-ID; a build receipt says what changed; a doer waiting on a
    code asks for it plainly; a blocked doer line quotes the job at a word; acks and routines read like a person;
  - ids, ticket codes and preview markers are gone from any line, the model's reply included;
  - the same words to the same place within the hour post once;
  - a system card (sentinel, outcome, connection, breaker, self-test) is never urgent whatever its poster said and
    never in the digest: Mission Control only; a send card, a 'Waiting Nd' ask and a build OK are not system cards;
  - the reflex works system cards: presses the button once, files a build an hour later, and says one plain line
    only for a sign-in that is the owner's to do; a held card is never a leak;
  - receipts post the reworded line and keep quiet on system talk.
   python assistant/tests/test_voice.py
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
TMP = Path(tempfile.mkdtemp(prefix="wv_voice_"))
os.environ["ASSISTANT_DATA"] = str(TMP / "adata")
os.environ["ASSISTANT_CARDS"] = str(TMP / "cards")
os.environ["ASSISTANT_LEARNINGS"] = str(TMP / "learnings")
os.environ["RULINGS_FILE"] = str(ROOT / "data" / "rulings.json")
os.environ["DOER_DATA"] = str(TMP / "doer")
os.environ["AGENT_STATE"] = str(TMP / "agent_state")
os.environ["AGENT_DATA"] = str(TMP / "agent_data")
os.environ["AGENT_SUGGESTIONS"] = str(TMP / "sugg.md")
os.environ["CHAT_PUSH"] = "off"
os.environ["DEADMAN_NO_FIX"] = "1"
os.environ["CRM_SNAPSHOT_FILE"] = str(TMP / "no_snapshot.json")
os.environ["ASSISTANT_CARDS_DELIVERY"] = "weekly"
for d in ("adata", "cards", "agent_state", "agent_data", "doer"):
    (TMP / d).mkdir(parents=True, exist_ok=True)
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT / "agent"))
import core  # noqa: E402
import voice  # noqa: E402
import cards  # noqa: E402
import digest  # noqa: E402
import reflex  # noqa: E402
import receipts  # noqa: E402
import outcomes  # noqa: E402
import sentinels  # noqa: E402
import chat_push  # noqa: E402
sys.path.insert(0, str(ROOT / "doer"))
import doer  # noqa: E402

N = 0


def ok(cond, msg):
    global N
    N += 1
    if not cond:
        print(f"FAIL {N}: {msg}")
        sys.exit(1)
    print(f"ok {N}: {msg}")


# a scratch phone book
(TMP / "adata" / "contacts.json").write_text(json.dumps({"books": {"owner1": {"people": [
    {"name": "Quinn Heathcote", "phones": [{"value": "+15550734973"}], "emails": []},
    {"name": "Corwin Castellan IV", "phones": [{"value": "(555) 049-1349"}], "emails": [{"value": "p5d59f2@example.invalid"}]}]}}}), encoding="utf-8")

# ------------------------------------------------------------------ 1. system talk never reaches Chat
BLOCKED_LINES = {
    "system_status": "Heads up: the comms probe (a new text or email will not wake anything) has stopped: last heartbeat 108 min ago.",
    "system_status ": "The comms probe is back; the gap was 60 min.",
    "raw_output": "Drain ran: DRAIN ran=0 done=0 exception=0 held_quiet=0 reaped=0",
    "raw_output ": "Ran cards-sweep: 2026-10-26 19:49:57  SPINE ran=1 ok=0 failed=1 skipped=49",
    "chase_line": "Still waiting on you: card open 39h unanswered: [wedding] $1,000 deposit for Odette and Linden?",
    "chase_line ": "Still open, asked 3 times each so I've stopped carding them: Policy payment due 10/26.",
    "scorecard": "Scorecard: 55 things reached a verdict this week, 76% confirmed landed",
    "review_summary": "From my review of yesterday's chats: The scorecard says the tracking.mining loop is being ignored",
    "build_slipped": "Still not built: Turn off the nightly YouTube scout. If you still want it, say so once more.",
    "unconfirmed": "I could not confirm that Upload owner1's 2022 back-tax packet into Google Drive on je went; check once and tell me.",
    "progress": "Still on it; it stopped partway without noting a milestone, so I'm taking that piece again.",
    "progress ": "I couldn't finish \"the cart\": the window is not on screen. It got as far as: signed in.",
    "progress  ": "That still didn't take, so I'm building it a different way; I'll finish this here when it lands.",
}
for cls, line in BLOCKED_LINES.items():
    ok(voice.classify(line) == cls.strip() and voice.gate(line) == "", f"held back ({cls.strip()}): {line[:50]!r}")
os.environ["ASSISTANT_VOICE_SYSTEM"] = "chat"
ok(voice.gate(BLOCKED_LINES["scorecard"]).startswith("Scorecard:"), "the switch lets system talk through again")
del os.environ["ASSISTANT_VOICE_SYSTEM"]
ok(voice.gate("owner1, Sloane & Bram's film has been waiting on your review for 9 days.") ==
   "owner1, Sloane & Bram's film has been waiting on your review for 9 days.", "a person's own line passes untouched")
held = voice.recent(1)
ok(sum(v for k, v in held.items() if k.startswith("quiet:")) >= 10, f"what was held back is counted ({held})")

# ------------------------------------------------------------------ 2. the lines a person would say, reworded
ok(voice.gate("Text to +15550734973 handed to the phone at 12:19 PM; the phone's Shortcut sends it.") == "Texted Quinn Heathcote at 12:19 PM, from your phone.",
   "a handed text names the person, not the number")
ok(voice.gate("Text to +18055550123 handed to the phone at 1:00 PM; the phone's Shortcut sends it.") == "Texted (805) 555-0123 at 1:00 PM, from your phone.",
   "...an unknown number reads as a phone number")
ok(voice.gate("Sent by email to p5d59f2@example.invalid at 4:45 PM (Message-ID <0e1d2c3b4a5968778695a4b3c2d1e0f9@example.invalid>).") == "Emailed Corwin Castellan IV at 4:45 PM.",
   "an email result names the person and drops the Message-ID")
ok(voice.gate("Sent by email to x@y.com at 6:08 PM (Message-ID <1@h>), but Gmail put it in a NEW thread, not as a reply in the original conversation.")
   == "Emailed x@y.com at 6:08 PM, though it went as a new thread rather than a reply.", "...a thread miss is still said, plainly")
ok(voice.gate("Confirmed at 3:30 PM: the text to +15550491349 went (they replied, so it went).") == "Corwin Castellan IV replied, so that text went.", "a confirmed text")
# CRM send result: install a fake connector so crm.name() returns "AcmeCRM" for this test
import types as _types
import crm as _crm_mod
_fake_crm = _types.SimpleNamespace(CRM=True, SERVICE="AcmeCRM", PREVIEW_TAG="[ACME PREVIEW]",
                                   MESSAGE_KINDS={"acme_message"}, INBOUND_KINDS={"acme_message_in"},
                                   STAGE_KINDS={"acme_set_stage"}, CHANNEL_ALIASES={"acme"},
                                   valid_thread=lambda s: bool(__import__("re").fullmatch(r"[0-9a-f]{24}", str(s or ""))))
_orig_connector = _crm_mod._connector
_crm_mod._connector = lambda: _fake_crm
ok(voice.gate("Sent in AcmeCRM at 2:10 PM (AcmeCRM message 1a2b3c4d, status sent); AcmeCRM logged it at 2026-10-25 21:10 UTC.") == "Sent in AcmeCRM at 2:10 PM.",
   "a CRM send result without the ids")
_crm_mod._connector = _orig_connector   # restore
t = voice.gate("Not done: NOT_CONFIGURED smtp:personal (set with setup_creds.py mail personal). The card is back on your phone.")
ok(t == "That didn't go: the personal mail account has no send login yet (Mission Control > Setup fixes that). The card is back for another tap.", f"an error code in plain words: {t!r}")
t = voice.gate('That\'s built now, the one you asked for: "A CRM send that was refused must not show a green check on the card". What changed: Approval cards now show an hourglass while a send is waiting. If anything feels off, say so and I\'ll undo it.')
ok(t == "Done: Approval cards now show an hourglass while a send is waiting. Say if anything feels off and I'll undo it.", f"a build receipt says what changed: {t!r}")
t = voice.gate('I built a fix for this one: "A card handed on after a draft-lint refusal posts in the thread where it was asked". What changed: Build request A card handed on after a dra. If anything feels off, say so and I\'ll undo it.')
ok(t == 'Done, that\'s built: "A card handed on after a draft-lint refusal posts in the thread where it was asked". Say if anything feels off and I\'ll undo it.',
   f"...a receipt whose 'what changed' is the truncated title falls back to the title: {t!r}")
t = voice.gate('I built a fix for this one: "Doer falls back". What changed: 5a778a2 2026-10-26 20:11: the builder and the doer switch to the fallback account on a usage limit. If anything feels off, say so and I\'ll undo it.')
ok(t.startswith("Done: The builder and the doer switch to the fallback account"), f"...no commit id, no date: {t!r}")
t = voice.gate("I am waiting for a verification code: Type the 6-digit MyDMV code just texted to the phone ending 737 (it expires in 10 minutes). It was sent to your phone or email. Open the card on Mission Control (https://x), type the code in the note box and tap Approve; I will carry on. No code? Tap 'No code came'.")
ok(t == "I need the 6-digit MyDMV code just texted to the phone ending 737 (it expires in 10 minutes). Reply with it here and I'll carry on.", f"a code ask, as a person asks: {t!r}")
t = voice.gate('One step needs you on "Upload owner1\'s 2022 back-tax packet from C:/Users/owner1/Documents/Taxes/2022 into Google Drive on ownernam…": the site wants a verification code only your phone gets.')
ok(t == 'One step needs you on "Upload owner1\'s 2022 back-tax packet": the site wants a verification code only your phone gets.', f"an owner-only block quotes the job at a word: {t!r}")
ok(voice.gate('I could not finish on "Upload the packet": my rules bar me from reading anything under Taxes.') == "",
   "a 'could not finish' line never reaches Chat any more (the build is filed instead, 2026-10-30)")
ok(voice.gate("Handing that to Sage to work through; it comes back here with what was done.") == "On it. I'll come back here with what I did.", "a tap ack reads like a person")
ok(voice.gate("Tony's costume party, time to get ready: Tony's costume party is at 6:30 tonight, 248 S Example Ave in Marrow Bay.", "routine")
   == "Tony's costume party is at 6:30 tonight, 248 S Example Ave in Marrow Bay.", "a routine drops its 'Title: ' prefix")
ok(voice.gate("Back taxes nudge: Morning. Have the back taxes gone in yet?", "routine") == "Morning. Have the back taxes gone in yet?", "...even when the title is not in the body")
ok(voice.gate("owner1, Corwin said yes to the editing terms. (card qa1b2c3d4e5f6)") == "owner1, Corwin said yes to the editing terms.", "a card id is gone")
ok(voice.polish("approved acme_add_team_member tk-0316aeedb4b8 still queued; [ACTION PREVIEW] Type the code") == "approved acme_add_team_member still queued; Type the code", "ticket codes and preview markers are gone")
ok(voice.names("Text to +15550734973: resend") == "Text to Quinn Heathcote: resend", "a card header names the person")
ok(voice.plain("PERMANENT: DELIVERY UNVERIFIED: the doer did not confirm the step") == "I couldn't confirm it was delivered; the doer did not confirm the step", "codes in plain words")

# ------------------------------------------------------------------ 3. the same words once an hour
ok(voice.gate("Same words twice", "say", "spaces/X") == "Same words twice" and voice.gate("Same words twice", "say", "spaces/X") == "", "the same words to the same place within the hour post once")
ok(voice.gate("Same words twice", "say", "spaces/Y") == "Same words twice", "...another place is another message")

# ------------------------------------------------------------------ 4. system cards: never urgent, never in the digest
r = cards.sentinel_card({"key": "spine:cards-sweep", "header": "Spine job cards-sweep failing", "text": "spine job 'cards-sweep' has failed 3 runs in a row: timeout",
                         "act": {"spine_job": "cards-sweep"}}, [{"label": "Run it now (Recommended)"}, {"label": "Leave it"}])
ok(r.get("posted") and r.get("delivery") == "mc" and r.get("chat") == [], f"a sentinel card is Mission Control only, whatever its poster said: {r}")
sc = cards._read(cards.LIVE / f"{r['qid']}.json")
ok(sc.get("delivery") == "mc" and not sc.get("urgent") and not sc["questions"][0].get("urgent"), "...stored without the poster's urgent flag")
row = outcomes.claim("agent_run", "tk-aaaa1111bbbb", "Restart the MyDMV sign-in", lane="personal", by="test")
rr = outcomes.ask_owner(dict(row, checks=[{"detail": "waited 159 min on route task"}]), dry=False)
oc = cards._read(cards.LIVE / f"{rr.get('qid')}.json") if rr.get("posted") else None
ok(oc and oc.get("delivery") == "mc", f"a 'Not confirmed' outcome card is Mission Control only: {rr}")
cq = cards.connection_card("crm-doer", "AcmeCRM in the doer's browser", "last good 10h ago (limit 9h)", "sign in again in the doer's browser")
rc = cards.post([cq], "sentinels", lane=cq["lane"])
ok(rc.get("delivery") == "mc", "a connection-down card is Mission Control only")
rb = cards.breaker_card("personal", 2, 0)
ok(rb.get("delivery") == "mc", "a paused-channel card is Mission Control only")
rs = cards.post([{"header": "A nightly self-test failed", "lane": "personal", "audience": "personal", "urgent": True,
                  "question": "nightly self-test test_phone_bridge FAILED: 3 FAILED", "options": [{"label": "Hand it to Sage (Recommended)"}, {"label": "Leave it"}]}], "sentinels", lane="personal")
ok(rs.get("delivery") == "mc", "a failed self-test card is Mission Control only, by its header")
ok(not any(c.get("delivery") == "mc" for c in digest.held()), "no system card is in the weekly digest")
rw = cards.post([{"header": "Waiting 5d: owner1: add Wren to Teo's project", "lane": "wedding", "sentinel_key": "staged:t1", "sentinel_act": {"close": ["t1"]},
                  "question": "This has waited 5 days with nothing since: add Wren.", "options": [{"label": "Hand it to Sage (Recommended)"}, {"label": "Close it"}],
                  "clears_when": {"sentinel": "staged:t1"}}], "sentinels", lane="wedding")
ok(rw.get("delivery") == "weekly", "a stalled ask ('Waiting Nd') is the owner's decision: it waits for the digest, not Mission Control only")
rsend = cards.post([{"header": "Text to +15550734973", "lane": "wedding", "audience": "client", "urgent": True, "question": "Text Marlo to ask if he's free Nov 7.\n[TEXT PREVIEW] To +15550734973:\nHey Marlo, free Nov 7?",
                     "options": [{"label": "Send (Recommended)"}, {"label": "Skip"}], "exec": {"kind": "imessage_send", "approve_label": "Send (Recommended)", "payload": {"account": "owner1-iphone", "to": "+15550734973", "body": "Hey Marlo, free Nov 7?"}}}],
                   "agent:owner1", lane="wedding", asked=True)
ok(rsend.get("delivery") == "now", "a send card an owner asked for is urgent as before")
body = chat_push.card_body(cards._read(cards.LIVE / f"{rsend['qid']}.json"), "open")
title = body["cardsV2"][0]["card"]["header"]["title"]
draft = json.dumps(body)
ok(title == "Text to Quinn Heathcote" and "Hey Marlo, free Nov 7?" in draft, f"the card's header names the person; the draft stays exactly as it goes out: {title!r}")
rb2 = cards.post([{"header": "Build needs your OK", "lane": "wedding", "urgent": True, "question": "To build X I'd have to change cards.py. What do you want?",
                   "options": [{"label": "Go ahead (Recommended)"}, {"label": "Drop it"}]}], "builder", lane="wedding")
ok(rb2.get("delivery") == "now", "a build that needs the owner's OK is his decision, not a system card")
os.environ["ASSISTANT_CARDS_SYSTEM"] = "chat"
r_old = cards.sentinel_card({"key": "sensor:acmecrm", "header": "AcmeCRM sensor quiet", "text": "snapshot 14h old", "act": {}}, [{"label": "Run the sensor now (Recommended)"}, {"label": "Leave it"}])
ok(r_old.get("delivery") == "now", "the switch brings the old behaviour back")
del os.environ["ASSISTANT_CARDS_SYSTEM"]

# a held system card in the transcript is never a leak, even for a muted class
d = reflex._load()
d["muted"]["build_unconfirmed"] = {"ts": time.time() - 3600, "rule": "r1", "by": "owner1"}
reflex._save(d)
core.say("spaces/HOME", "card", "[card q1 · personal] Not confirmed: build: I could not confirm this landed", channel="card", meta={"qid": "q1", "held": True})
ok(not [f for f in reflex.d_leaks() if f["key"] == "leak:build_unconfirmed"], "a card held on Mission Control is not a leak")
d = reflex._load()
d["muted"].pop("build_unconfirmed", None)
reflex._save(d)

# ------------------------------------------------------------------ 5. the reflex works system cards
found = {f["key"]: f for f in reflex.d_system_cards()}
ok(f"card:{r['qid']}" in found and f"card:{rc['qid']}" in found and f"card:{rr['qid']}" not in found,
   "the reflex sees the sentinel and connection cards; the outcome ledger keeps its own")
pressed = []
_act = sentinels.act
sentinels.act = lambda a, by="owner": pressed.append((a, by)) or "Ran cards-sweep: SPINE ran=1 ok=1"
res = reflex.tick(only=("card",))
sentinels.act = _act
ok(pressed and pressed[0][0] == {"spine_job": "cards-sweep"} and pressed[0][1] == "reflex", f"step 0 presses the sentinel card's own button, by code: {pressed}")
fnd = reflex._load()["findings"][f"card:{r['qid']}"]
ok(fnd["step"] == 1 and not fnd["fixes"][0].get("final"), "...and gives it an hour")
said = []
_say = cards.say
cards.say = lambda owner, lane, text, thread="": said.append((lane, text)) or {"said": True}
_w = reflex._window_ok
reflex._window_ok = lambda: True
NOTES = []
_fb = reflex._file_build
reflex._file_build = lambda f, title, body: NOTES.append(title) or "Wiki/Personal/x.md"
res2 = reflex.tick(only=("card",), now=time.time() + 3700)
reflex.tick(only=("card",), now=time.time() + 7500)
reflex._file_build, cards.say, reflex._window_ok = _fb, _say, _w
ok(NOTES and NOTES[0].startswith("Fix: Spine job cards-sweep failing"), f"an hour on, still open: one build request with the evidence: {NOTES}")
conn_line = [t for lane, t in said if "AcmeCRM in the doer's browser" in t]
ok(conn_line and conn_line[0].startswith("AcmeCRM in the doer's browser has stopped working") and "Couldn't fix" not in conn_line[0],
   f"a sign-in only the owner can do: one plain line in the card's own words, not a report: {said}")
ok(not any("cards-sweep" in t for _, t in said), "...and nothing in Chat for the sentinel the reflex is still working")

# ------------------------------------------------------------------ 6. receipts speak through the gate
POSTS = []


class FakeChat:
    @staticmethod
    def configured():
        return True

    @staticmethod
    def post(space, text, thread=""):
        POSTS.append((space, text, thread))
        return f"{space}/messages/m{len(POSTS)}.m{len(POSTS)}"

    @staticmethod
    def patch(name, text):
        POSTS.append(("patch", text, name))
        return True


receipts._gchat = lambda: FakeChat
core.save(core.DATA / "gchat_spaces.json", {"owner1": "spaces/DMOWNER1"})
receipts.say("b1", "spaces/HOME", "", "Still not built: Turn off the nightly YouTube scout. If you still want it, say so once more and I'll file it again.", space="spaces/HOME")
ok(not POSTS, "a 'Still not built' receipt stays off Chat")
rows = [x for x in core.rows(core.TRANSCRIPT) if x.get("ref") == "b1"]
ok(rows and rows[-1].get("held"), "...but Sage's transcript knows, marked held")
receipts.say("b1", "spaces/HOME", "", 'That\'s built now, the one you asked for: "YouTube scout off switch". What changed: The nightly YouTube scout now has an off switch Sage can flip. If anything feels off, say so and I\'ll undo it.', space="spaces/HOME")
ok(POSTS and POSTS[-1][1] == "Done: The nightly YouTube scout now has an off switch Sage can flip. Say if anything feels off and I'll undo it.", f"the landed receipt posts in a person's words: {POSTS}")
n = len(POSTS)
receipts.say("b2", "spaces/HOME", "", 'That\'s built now, the one you asked for: "YouTube scout off switch". What changed: The nightly YouTube scout now has an off switch Sage can flip. If anything feels off, say so and I\'ll undo it.', space="spaces/HOME")
ok(len(POSTS) == n, "the same words under a second reference do not post twice (the 08:18 double)")

# ------------------------------------------------------------------ 7. the doer at the source
_job = "Upload owner1's 2022 back-tax packet from C:/Taxes/2022 into Google Drive on owner4@example.invalid"
_jl = doer.job_line({"payload": {"text": _job}}, 60)
_cut = _jl.strip('"').rstrip("…")
ok(_jl.startswith('"') and _jl.endswith('…"') and _job.startswith(_cut) and _job[len(_cut)] == " " and 40 < len(_cut) <= 60,
   "the doer quotes a job cut at a word, never mid-name")
line = doer._say("owner1", "DOER_CHECKPOINT", "qabc Type the 6-digit MyDMV code just texted to the phone ending 737 (it expires in 10 minutes).",
                 {"id": "t1", "payload": {"text": "Restart the MyDMV sign-in"}}) if hasattr(doer, "_asks_code") else ""
_ac = getattr(doer, "_asks_code", None)
if _ac is not None:
    doer._asks_code = lambda qid: True
    line = doer._say("owner1", "DOER_CHECKPOINT", "qabc Type the 6-digit MyDMV code just texted to the phone ending 737 (it expires in 10 minutes).", {"id": "t1", "payload": {"text": "x"}})
    doer._asks_code = _ac
    ok(line.startswith("I need the 6-digit MyDMV code just texted to the phone ending 737") and "Mission Control" not in line, f"the doer asks for a code like a person: {line!r}")

print(f"\nALL OK ({N} checks)")

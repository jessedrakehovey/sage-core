"""Offline tests for the long-message layout (agent/layout.py, 2026-11-04): a long text keeps its opening as the
message and folds the rest into a card; nothing is dropped, nothing is added; a short text is left alone; and the
one door (gchat.post / gchat.patch) sends the card and falls back to plain text when Chat refuses it.
No Google, no network.   python assistant/tests/test_layout.py
"""
import os as _os, tempfile as _tf
_os.environ.setdefault("WV_LOG_DIR", _tf.mkdtemp(prefix="wv_logs_"))   # never the live ~/.claude logs
import io
import json
import os
import re
import sys
import tempfile
import urllib.error
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
TMP = Path(tempfile.mkdtemp(prefix="wv_layout_"))
os.environ["AGENT_STATE"] = str(TMP / "agent_state")
os.environ["AGENT_DATA"] = str(TMP / "agent_data")
os.environ["ASSISTANT_DATA"] = str(TMP / "adata")
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT / "agent"))
import layout  # noqa: E402

FAILS = []
CONF = dict(layout.DEFAULTS)


def check(name, cond, extra=""):
    print(("ok   " if cond else "FAIL ") + name + (f"  {extra}" if extra and not cond else ""))
    if not cond:
        FAILS.append(name)


def words(s):
    return sorted(re.findall(r"[A-Za-z0-9$%]+", re.sub(r"https?://\S+", " ", s)))


def shown(sec):
    """The widgets in view before Show more is tapped."""
    w = sec["widgets"]
    return w[:sec["uncollapsibleWidgetsCount"]] if sec.get("collapsible") else w


def text_of(ws):
    return " ".join(w["textParagraph"]["text"] for w in ws)


# ------------------------------------------------------------------ 1. short stays plain
lead, card = layout.split("Done. The permit is renewed and the receipt is in your inbox.", CONF)
check("a short message is left alone: no card, the same words", card is None and lead.startswith("Done. The permit"))
check("a medium list under the limits is left alone too", layout.split("Three things:\n- one\n- two\n- three", CONF)[1] is None)

# ------------------------------------------------------------------ 2. headings and lists: one folded section each
LIST = """Here's the full list. Do these after the first two are done.

**Money**
- The checking account and both shared cards
- The side-job payer, since that pay comes through it
- Any loans, such as student loans
- The tax records, with the employer's form

**Work and school**
- Payroll, which also covers benefits and any retirement account
- The alumni records and any professional license

**Home and bills**
- The lease, or the property paperwork if your name is on it
- Electric, gas, water, internet, trash and phone
- The mail, so everything arrives under the right name

**Government**
- Voter registration
- Vehicle registration and title
- The passport, which is free to correct within a year of issue

**Online**
- Email, the phone contact card and the store accounts
- Subscriptions and loyalty cards

I haven't verified every site's current steps, so check each one when you get there. If you'd like, I'll look up the exact documents each one wants."""
lead, card = layout.split(LIST, CONF)
secs = card["card"]["sections"]
check("the opening paragraph is the message itself, word for word", lead == "Here's the full list. Do these after the first two are done.")
check("one section per heading, in order, the heading as its header (no asterisks)",
      [s.get("header", "") for s in secs] == ["Money", "Work and school", "Home and bills", "Government", "Online", ""], [s.get("header") for s in secs])
check("a section with several lines is folded: one line in view, the rest behind Show more",
      secs[0].get("collapsible") and secs[0]["uncollapsibleWidgetsCount"] == 1 and "checking account" in text_of(shown(secs[0]))
      and "student loans" not in text_of(shown(secs[0])) and "student loans" in text_of(secs[0]["widgets"]))
check("a two-line section is short enough to show whole", not secs[1].get("collapsible") and "alumni records" in text_of(secs[1]["widgets"]))
check("the closing words after the last list stay in a section of their own",
      secs[-1].get("header") is None and "haven't verified" in text_of(shown(secs[-1])))
check("bullets are bullets, one per line", "• The checking account" in text_of(secs[0]["widgets"]) and "<br>" in secs[0]["widgets"][1]["textParagraph"]["text"])
check("nothing is dropped and nothing is added: the same words", words(LIST) == words(lead + "\n" + layout.plain(card)))
total_view = len(lead) + sum(len(s.get("header") or "") + len(re.sub(r"<[^>]+>", "", text_of(shown(s)))) for s in secs)
check("what is in view is well under the whole", total_view < len(LIST) * 0.75, f"{total_view} of {len(LIST)}")

# ------------------------------------------------------------------ 3. a wall of prose with the reader's next move
WALL = ("One step needs you on \"Renew the parking permit\": I never got past the city site's sign-in, so nothing was opened, edited or saved. "
        "At permits.city.example the Continue with Google button opened the account chooser, which lists only the work address. "
        "The other account is not signed into this browser. I did not pick the work address, because it was already tried and the site said it has no account. "
        "Using another account would mean typing an email and password, which I may not do. The other sign-in ways were not tried. "
        "The quickest way to finish it yourself, from your phone: https://permits.city.example/login then tap Continue with Google and choose the other address. "
        "If it says no account, tap Can't log in and enter both addresses to see which one the permit is under. Once in, send me the account email so I can retry.")
lead, card = layout.split(WALL, CONF)
secs = card["card"]["sections"]
check("a wall of prose: the first sentence or two is the message", lead.startswith("One step needs you on") and lead.endswith("work address.") and len(lead) < 330, lead)
check("the narration is folded", secs[0].get("collapsible") and "not signed into this browser" in text_of(shown(secs[0])) and "may not do" not in text_of(shown(secs[0])))
check("the reader's next move opens its own section and is in view, with the address as a link",
      text_of(shown(secs[1])).startswith("The quickest way to finish it yourself") and '<a href="https://permits.city.example/login">' in text_of(shown(secs[1])))
check("a wall loses no words either", words(WALL) == words(lead + "\n" + layout.plain(card)))
check("a web address, a time and an initial do not end a sentence",
      layout.sentences("Go to permits.city.example at 5.30 p.m. with J. Yarrow. Then call.") == ["Go to permits.city.example at 5.30 p.m. with J. Yarrow.", "Then call."],
      repr(layout.sentences("Go to permits.city.example at 5.30 p.m. with J. Yarrow. Then call.")))

# ------------------------------------------------------------------ 4. numbered steps, capital headings, nested bullets
STEPS = "Here's the order that works.\n\n" + "\n".join(f"{i}. **Step {i}.** Do the thing for step {i}, and wait for the letter that confirms it arrived." for i in range(1, 8)) \
        + "\n\nIt's easiest to do the first three in order."
lead, card = layout.split(STEPS, CONF)
s0 = card["card"]["sections"][0]
check("numbered steps keep their numbers and their bold titles", "1. <b>Step 1.</b>" in text_of(shown(s0)) and "7. <b>Step 7.</b>" in text_of(s0["widgets"]) and s0.get("collapsible"))
CAPS = ("The ballot for the address, from the county guide, with every contest on it and the two races that are not on it this year.\nPolls close at 8.\n"
        "STATE (vote for one)\n- Governor: A; B\n- Treasurer: C; D\n- Controller: E; F\n- Secretary: G; H\n\nJUDICIAL (yes or no)\n- Supreme Court: I; J\n- Court of Appeal:\n - Division One: K; L\n - Division Two: M; N\n"
        + "\n".join(f"- Measure {c}: a long enough line about what the measure would change in the city" for c in "ABCDEF"))
lead, card = layout.split(CAPS, CONF)
heads = [s.get("header", "") for s in card["card"]["sections"]]
check("a short line in capitals is a heading even straight under a paragraph", "STATE (vote for one)" in heads and "JUDICIAL (yes or no)" in heads, heads)
jud = next(s for s in card["card"]["sections"] if s.get("header", "").startswith("JUDICIAL"))
check("a nested bullet is indented under its parent", "◦ Division One" in text_of(jud["widgets"]) and "&nbsp;" in text_of(jud["widgets"]))

# ------------------------------------------------------------------ 5. safe HTML, links, switches
h = layout.to_html("Use <b>this</b> & that: [the form](https://x.example/a?b=1&c=2), *bold*, _soft_, snake_case_name stays, <https://y.example|here>.")
check("card text: the agent's own angle brackets are escaped, links and bold are kept, an underscore inside a word is left",
      "&lt;b&gt;this&lt;/b&gt; &amp; that" in h and '<a href="https://x.example/a?b=1&amp;c=2">the form</a>' in h and "<b>bold</b>" in h and "<i>soft</i>" in h
      and "snake_case_name" in h and '<a href="https://y.example">here</a>' in h, h)
check("switched off: plain text", layout.split(LIST, dict(CONF, enabled=False))[1] is None)
check("a code block is never laid out", layout.split(LIST + "\n```\nx = 1\n```", CONF)[1] is None)
check("too big for a card: plain text, in parts as before", layout.split(LIST * 20, CONF)[1] is None)
check("every section has widgets with text, and a folded one keeps exactly one in view",
      all(s["widgets"] and all(w["textParagraph"]["text"] for w in s["widgets"]) and (not s.get("collapsible") or s["uncollapsibleWidgetsCount"] == 1)
          for t in (LIST, WALL, STEPS, CAPS) for s in layout.split(t, CONF)[1]["card"]["sections"]))

# ------------------------------------------------------------------ 6. the door: gchat.post / gchat.patch
import gchat  # noqa: E402
sent = []
gchat._with_thread = lambda space, body, thread="", key="": ("https://chat.example/v1/" + space + "/messages", body)
gchat.mentionize = lambda text, space="": text


def ok_req(url, body=None, method="", idempotent=False):
    sent.append((url, body, method))
    return {"name": "spaces/S/messages/m1", "thread": {"name": "spaces/S/threads/t1"}}


gchat._req = ok_req
name = gchat.post("spaces/S", LIST, "spaces/S/threads/t1")
check("post: a long text is ONE message, the opening as its text and the rest as its card",
      name == "spaces/S/messages/m1" and len(sent) == 1 and sent[0][1]["text"].startswith("Here's the full list") and len(sent[0][1]["text"]) < 100
      and sent[0][1]["cardsV2"][0]["cardId"] == "more" and len(sent[0][1]["cardsV2"][0]["card"]["sections"]) == 6, json.dumps(sent)[:300])
sent.clear()
gchat.post("spaces/S", "Done. The permit is renewed.")
check("post: a short text is a plain message with no card", len(sent) == 1 and sent[0][1] == {"text": "Done. The permit is renewed."})
sent.clear()
gchat.patch("spaces/S/messages/m1", WALL)
check("patch: the placeholder becomes the laid-out answer in place (text and card in one edit)",
      len(sent) == 1 and sent[0][2] == "PATCH" and "updateMask=text,cardsV2" in sent[0][0] and sent[0][1]["cardsV2"] and sent[0][1]["text"].startswith("One step needs you"))


def refusing(url, body=None, method="", idempotent=False):
    sent.append((url, body, method))
    if body and body.get("cardsV2"):
        raise urllib.error.HTTPError(url, 400, "Bad Request", {}, io.BytesIO(b"{}"))
    return {"name": "spaces/S/messages/m2"}


sent.clear()
gchat._req = refusing
name = gchat.post("spaces/S", LIST)
check("post: when Chat refuses the card the same words still go up, as plain text",
      name == "spaces/S/messages/m2" and len(sent) == 2 and "cardsV2" not in sent[1][1] and "Voter registration" in sent[1][1]["text"])
sent.clear()
check("patch: the same fallback", gchat.patch("spaces/S/messages/m1", WALL) and "cardsV2" not in sent[-1][1] and "updateMask=text" in sent[-1][0] and "quickest way" in sent[-1][1]["text"])

print()
print("ALL PASSED" if not FAILS else f"{len(FAILS)} FAILED: {FAILS}", f" ({TMP})")
sys.exit(1 if FAILS else 0)

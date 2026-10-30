"""chat_push.py - every card and alert reaches the owners' phones through Google Chat. Zero-LLM.

Built 2026-10-05 after an $8,000 quiz lead sat on Mission Control for an evening: the sensor
caught it in six minutes, but nothing on this machine could *push* (the example alert mail
had no password, Telegram is retired, Mission Control is a page one has to open). Sage's
Google Chat app was live and could already post as itself, so this module makes it speak
first:

  push_card(card)      a question card -> one Chat message per owner DM, with a button per
                       option (a tap is a CARD_CLICKED event; bridge.py turns it into
                       cards.answer, exactly as a tap on Mission Control)
  mark_answered(card)  the posted message is edited in place: buttons gone, who chose what,
                       an Undo button for the 30 s window
  mark_undone(card)    the buttons come back
  mark_closed(card)    expired / consumed: the message says so
  push_text(text)      a plain note (notify.py): dreaming digests, heartbeat alerts, briefs

Rules it keeps:
  - It posts only into a DM an owner already opened with Sage (assistant/data/agent/
    gchat_spaces.json: recorded the moment they ADD the app (gchat.record_added, both event
    paths), or the first time they write, or by discover() for an owner whose numeric Chat
    id is known). Nobody else.
  - An owner the config routes cards to who has NO DM is never skipped silently: one log
    line a day, `--status` names them, and the owner gets one note a week saying who is not
    reached and how to fix it (2026-10-06: every wedding card had silently skipped owner).
  - Every button carries only {qid, option, q}; the answer path is cards.answer, the same
    guarded function Mission Control calls. Nothing here sends anything outward.
  - Never raises. A Chat failure must never fail a card post, a cron or the operator.
  - Off in test runs (ASSISTANT_CARDS / ASSISTANT_DATA set) and when CHAT_PUSH=off.

Config (assistant/config.json -> "push", all optional):
  {"cards_to": ["the owner"], "lanes_to": {"wedding": ["the owner", "owner"], "owner": ["owner"]},
   "notes_to": ["the owner"], "skip_note_prefixes": ["OPERATOR:"]}

  python assistant/scripts/chat_push.py --status          # also names owners who get no cards
  python assistant/scripts/chat_push.py --discover        # look up owners' DMs now
  python assistant/scripts/chat_push.py --backfill        # open cards never pushed -> Chat
  python assistant/scripts/chat_push.py --say "text"      # a test note to the owners
"""
from __future__ import annotations

import html
import json
import os
import re
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
AGENT = HERE.parent / "agent"
sys.path.insert(0, str(HERE))
for p in (str(AGENT),):
    if p not in sys.path:
        sys.path.append(p)

SPACES = Path(os.environ.get("AGENT_DATA") or (HERE.parent / "data" / "agent")) / "gchat_spaces.json"

def _orgcfg():
    """assistant/orgcfg.py (WP5): the organization's own values from assistant/org.json; the literal below is the fallback."""
    import sys as _sys
    from pathlib import Path as _P
    root = str(_P(__file__).resolve().parents[1])
    if root not in _sys.path:
        _sys.path.insert(0, root)
    import orgcfg
    return orgcfg


try:
    MC_URL = str(_orgcfg().get("mission_control_url") or "https://localhost:9443")
except Exception:  # noqa: BLE001
    MC_URL = "https://localhost:9443"


def _people() -> dict:
    """assistant/agent/people.json: the owners (role owner), in file order."""
    try:
        return json.loads((HERE.parent / "agent" / "people.json").read_text(encoding="utf-8-sig")) or {}
    except (OSError, ValueError):
        return {}


def _owners() -> dict:
    users = _people().get("users") or {}
    return {k: u for k, u in users.items() if str((u or {}).get("role") or "") == "owner"} or dict(users)


def _first_owner() -> str:
    """The first owner's key: the fallback DM when no conversation is on record (core.first_owner, by file)."""
    return next(iter(_owners()), "owner")
QUESTION_MAX = 3500
DETAILS_MAX = 2500
BUTTON_MAX = 40
UNDO_S = 30


def _business_lane() -> str:
    try:
        from alib import business_lane
        return business_lane()
    except Exception:  # noqa: BLE001
        return "business"


# without a config.json push section: cards and notes to the first owner, the business lane's cards to every owner,
# a lane named after an owner to that owner alone
DEFAULTS = {"cards_to": [_first_owner()], "lanes_to": {_business_lane(): list(_owners()), **{k: [k] for k in _owners()}},
            "notes_to": [_first_owner()], "skip_note_prefixes": ["OPERATOR:"]}
# tests set WV_LOG_DIR: a test run must never write fixture lines into the live log
LOG = Path(os.environ.get("WV_LOG_DIR") or (Path.home() / ".claude")) / "chat-push.log"
STATE = SPACES.parent / "chat_push_state.json"
DISCOVER_EVERY_S = 6 * 3600
NUDGE_EVERY_S = 7 * 86400


def _log(msg: str) -> None:
    try:
        LOG.parent.mkdir(parents=True, exist_ok=True)
        with open(LOG, "a", encoding="utf-8") as f:
            f.write(f"{time.strftime('%Y-%m-%dT%H:%M:%S')}  {msg}\n")
    except OSError:
        pass


def enabled() -> bool:
    if os.environ.get("CHAT_PUSH", "").lower() in ("off", "0", "false"):
        return False
    if os.environ.get("ASSISTANT_CARDS") or os.environ.get("ASSISTANT_DATA"):   # a test's scratch store
        return False
    try:
        return bool(_g().configured())
    except Exception:  # noqa: BLE001
        return False


def _cfg() -> dict:
    c = dict(DEFAULTS)
    try:
        from alib import config
        c.update(config().get("push") or {})
    except Exception:  # noqa: BLE001
        pass
    return c


def _spaces() -> dict:
    try:
        return json.loads(SPACES.read_text(encoding="utf-8-sig")) or {}
    except (OSError, ValueError):
        return {}


def _home_space() -> str:
    """The owners' shared space with the agent (agent/core.home_space). '' = per-owner DMs."""
    try:
        import core
        return core.home_space()
    except Exception:  # noqa: BLE001
        return ""


def targets(lane: str = "", kind: str = "card") -> list:
    """[(person_key, space)] for a lane. With a home space every card and note goes there once
    (the owner 2026-10-07: one space for Sage, owner and him); otherwise only owners who already
    opened a DM with the app."""
    home = _home_space()
    if home:
        return [("home", home)]
    c, sp = _cfg(), _spaces()
    keys = list(c.get("notes_to") or []) if kind == "note" else list((c.get("lanes_to") or {}).get(lane) or c.get("cards_to") or [])
    out, seen = [], set()
    for k in keys:
        if k in sp and k not in seen and str(sp[k]).startswith("spaces/"):
            out.append((k, sp[k]))
            seen.add(k)
    return out


def unreachable(lane: str = "", kind: str = "card") -> list:
    """Owners the config routes this lane to who have no DM with the app (they never get it)."""
    if _home_space():
        return []
    c, sp = _cfg(), _spaces()
    keys = list(c.get("notes_to") or []) if kind == "note" else list((c.get("lanes_to") or {}).get(lane) or c.get("cards_to") or [])
    return [k for k in dict.fromkeys(keys) if not str(sp.get(k) or "").startswith("spaces/")]


def _state() -> dict:
    try:
        return json.loads(STATE.read_text(encoding="utf-8")) or {}
    except (OSError, ValueError):
        return {}


def _save_state(st: dict) -> None:
    try:
        STATE.parent.mkdir(parents=True, exist_ok=True)
        tmp = STATE.with_suffix(".tmp")
        tmp.write_text(json.dumps(st, indent=1), encoding="utf-8")
        os.replace(tmp, STATE)
    except OSError:
        pass


def discover(force: bool = False) -> dict:
    """Find the DM of every owner who has none yet (spaces.findDirectMessage) and record it in
    gchat_spaces.json. App auth accepts only a numeric users/<id>, never an email (403, tried
    10/06), so this covers owners whose chat_user is known; everyone else is recorded when they
    add the app. At most every DISCOVER_EVERY_S unless forced. -> {person_key: space}. Never raises."""
    st, found = _state(), {}
    if not force and time.time() - float(st.get("discover_ts") or 0) < DISCOVER_EVERY_S:
        return found
    st["discover_ts"] = time.time()
    _save_state(st)
    try:
        import core
        g = _g()
        users = (core.owners() if hasattr(core, "owners") else core.people().get("users")) or {}
        sp = _spaces()
        for key, u in users.items():
            if str(sp.get(key) or "").startswith("spaces/"):
                continue
            for who in ([u["chat_user"]] if str(u.get("chat_user") or "").startswith("users/") else []):
                try:
                    space = g.find_dm(who)
                except Exception as e:  # noqa: BLE001
                    _log(f"discover {key}: {type(e).__name__}: {str(e)[:120]}")
                    continue
                if space:
                    found[key] = space
                    break
        if found:
            sp = _spaces()                      # re-read: the bridge may have written meanwhile
            sp.update({k: v for k, v in found.items() if not str(sp.get(k) or "").startswith("spaces/")})
            SPACES.parent.mkdir(parents=True, exist_ok=True)
            SPACES.write_text(json.dumps(sp, indent=1), encoding="utf-8")
            _log("discovered DM for " + ",".join(found))
    except Exception as e:  # noqa: BLE001
        _log(f"discover crashed: {type(e).__name__}: {str(e)[:200]}")
    return found


def _note_unreachable(missing: list, lane: str) -> None:
    """Never silent: a log line a day per person, a findDirectMessage try, a weekly note to the owner."""
    st = _state()
    day = time.strftime("%Y-%m-%d")
    for k in missing:
        if st.get(f"logged:{k}") != day:
            st[f"logged:{k}"] = day
            _log(f"skipped {k}: no Chat DM with the app, so {lane or 'these'} cards do not reach them")
    _save_state(st)
    if discover():
        return
    st = _state()
    if time.time() - float(st.get("nudge_ts") or 0) >= NUDGE_EVERY_S:
        names = " and ".join(m.capitalize() for m in missing)
        if push_text(f"{names}: not getting {lane or 'some'} cards, because Sage has no Chat DM with {names} yet. "
                     f"Ask {names} to open Google Chat, search for the Sage app and add it. Cards then reach "
                     f"{names} on their own; nobody has to message Sage first."):
            st = _state()
            st["nudge_ts"] = time.time()
            _save_state(st)


# ------------------------------------------------------------------ message bodies
def _para(text: str, limit: int) -> str:
    """Plain text -> the small HTML Chat's textParagraph accepts (<b>, <i>, <br>, <a>)."""
    t = str(text or "").strip()
    if len(t) > limit:
        t = t[:limit - 1].rstrip() + "…"
    t = html.escape(t, quote=False)
    t = re.sub(r"\*\*([^*\n]+)\*\*", r"<b>\1</b>", t)
    t = re.sub(r"(https?://[^\s<>\"]+)", r'<a href="\1">\1</a>', t)
    return t.replace("\n", "<br>")


SAGE_GREEN = {"red": 0.29, "green": 0.36, "blue": 0.26, "alpha": 1}      # example.invalid #4A5C43
_RECOMMENDED = re.compile(r"\s*\((?:recommended|rec\.?)\)\s*$", re.I)


def lane_name(lane: str) -> str:
    """A lane as a subtitle: an owner's own lane by their name (people.json), any other capitalised."""
    lane = str(lane or "")
    u = (_people().get("users") or {}).get(lane) or {}
    return str(u.get("name") or "").split(" ")[0] or (lane.capitalize() if lane else "")


def buttons_on() -> bool:
    """config.json push.buttons (default true). False: cards carry their choices as words, answered in the thread,
    for a machine with no public HTTPS path for Chat's button taps (sage-core, 2026-10-27)."""
    try:
        return bool(_cfg().get("buttons", True))
    except Exception:  # noqa: BLE001
        return True


def _button(label: str, fn: str, params: dict, interaction: str = "", filled: bool = False) -> dict:
    """One Chat button. `filled` paints it in the brand green (Chat renders a coloured button as
    filled, the rest as outlined), so the recommended choice reads at a glance instead of carrying
    a "(Recommended)" suffix in its text. The full label stays in the parameters: that is what
    cards.answer matches."""
    action = {"function": fn, "parameters": [{"key": k, "value": str(v)} for k, v in params.items()]}
    if interaction:
        action["interaction"] = interaction          # OPEN_DIALOG: the tap opens a dialog instead of acting
    shown = _RECOMMENDED.sub("", str(label)).strip() or str(label)
    b = {"text": shown[:BUTTON_MAX], "onClick": {"action": action}}
    if filled:
        b["color"] = SAGE_GREEN
    return b


def _split_preview(question: str) -> tuple:
    """(the ask, the message text) when the question carries a '[TEXT PREVIEW] To ...:' block;
    the preview is shown as its own section instead of inline."""
    q = str(question or "")
    m = re.search(r"\[(?:TEXT|EMAIL) PREVIEW\][ \t]*([^\n]*)\n?", q)
    if not m:
        return q, "", ""
    head, body = m.group(1).strip(), q[m.end():].strip()
    if head.lower().startswith("to ") and ": " in head:          # "[TEXT PREVIEW] To +1805...: Hi there" on one line
        head, first = head.split(": ", 1)
        body = (first + ("\n" + body if body else "")).strip()
    elif not (head.lower().startswith(("to ", "from ")) or head.endswith(":")):
        body, head = (head + ("\n" + body if body else "")).strip(), ""   # the tag alone, text on the same line
    return q[:m.start()].rstrip(), head.rstrip(":").strip(), body


def exec_text(q: dict) -> tuple:
    """(key, text) of the message an exec card would send, or ("", "")."""
    ex = q.get("exec") or {}
    p = ex.get("payload") or {}
    for k in ("body", "text", "note", "description"):     # cards._EXEC_TEXT_KEYS
        if p.get(k):
            return k, str(p[k])
    return "", ""


def draft_blanks(q: dict) -> list:
    """The [the owner: ...] style blanks still in the text an exec card would send (exec_store.BLANK_RE)."""
    ex = q.get("exec") or {}
    try:
        import exec_store
    except Exception:  # noqa: BLE001
        return []
    return exec_store.unfilled_blanks(ex.get("payload") or {}) if ex.get("kind") in exec_store.OUTBOUND_KINDS else []


def edit_dialog(card: dict, q_index: int = 0, text: str | None = None, error: str = "") -> dict:
    """The dialog behind "Edit & send": the outgoing text in a multi-line box, prefilled, and
    one Send button. Submitting answers the card with the edited text (gchat_events.click
    -> cards.answer(revised=...)), exactly like the pencil on Mission Control. `text`/`error`
    re-open it with the owner's own edit kept when a submit was refused (a blank still in it)."""
    q = (card.get("questions") or [{}])[q_index]
    qid = str(card.get("id") or "")
    ex = q.get("exec") or {}
    key, draft = exec_text(q)
    text = draft if text is None else text
    p = ex.get("payload") or {}
    to = p.get("to") or ""
    if not to and isinstance(p.get("to_list"), list):
        to = ", ".join(str(x) for x in p["to_list"])
    head = str(ex.get("kind") or "").replace("_", " ") + (f" to {to}" if to else "")
    top = []
    if error:
        top.append({"textParagraph": {"text": f'<font color="#B3261E"><b>{html.escape(error[:300])}</b></font>'}})
    elif draft_blanks(q):
        top.append({"textParagraph": {"text": "<b>Fill every [ ... ] blank before you send.</b> It will not go out with one left in."}})
    return {"header": {"title": "Edit before sending", "subtitle": head[:200]},
            "sections": [{"widgets": top + [
                {"textInput": {"name": "body", "label": "Message (sent exactly as written)", "type": "MULTIPLE_LINE",
                               "value": text[:4000]}},
                {"buttonList": {"buttons": [_button(str(ex.get("approve_label") or "Send"), "card_edit_submit",
                                                    {"qid": qid, "q": str(q_index)})]}}]}]}


def vault_dialog(site: str = "", error: str = "", username: str = "") -> dict:
    """The dialog behind "Enter login" (vault_chat.card): site, username, password, an optional code-generator
    secret, one Save button. What is typed here goes to the app as formInputs, never into the conversation;
    gchat_events.click (vault_add_submit) hands it to doer/vault.py add-stdin and closes the dialog."""
    top = []
    if error:
        top.append({"textParagraph": {"text": f'<font color="#B3261E"><b>{html.escape(error[:300])}</b></font>'}})
    top.append({"textParagraph": {"text": "Typed here, saved in the vault on my computer, never posted in chat. "
                                          "The site needs its web address (stripe.com, app.hubspot.com)."}})
    return {"header": {"title": "Login for the vault", "subtitle": (site or "which site, and the username and password")[:200]},
            "sections": [{"widgets": top + [
                {"textInput": {"name": "site", "label": "Site (web address)", "type": "SINGLE_LINE", "value": (site or "")[:200]}},
                {"textInput": {"name": "username", "label": "Username or email", "type": "SINGLE_LINE", "value": (username or "")[:200]}},
                {"textInput": {"name": "password", "label": "Password", "type": "SINGLE_LINE", "value": ""}},
                {"textInput": {"name": "totp", "label": "Code-generator secret (optional; the 'can't scan?' text under the QR code)", "type": "SINGLE_LINE", "value": ""}},
                {"buttonList": {"buttons": [_button("Save in the vault", "vault_add_submit", {"site": (site or "")[:200]}, filled=True)]}}]}]}


def card_body(card: dict, state: str = "open", answered: dict | None = None) -> dict:
    """The Chat message for a question card. state: open | answered | undone | expired | consumed | resolved."""
    q = (card.get("questions") or [{}])[0]
    qid = str(card.get("id") or "")
    lane, header = str(q.get("lane") or ""), _names(str(q.get("header") or "Decision"))   # "Text to Quinn Heathcote", never a raw number
    ex = q.get("exec") or {}
    kind = str(ex.get("kind") or "")
    words = {"imessage_send": "Sends a text when you approve", "email_send": "Sends an email when you approve"}
    try:
        import alib
        words.update(alib.plugin_map("SEND_WORDS"))          # the organization's own kinds say what their button does
    except Exception:  # noqa: BLE001
        pass
    sends = words.get(kind, "Acts when you approve" if ex else "")
    if ex.get("send_at"):
        try:
            import exec_store as _es
            sends = (sends or "Acts when you approve").replace("when you approve", f"at {_es.when_text(ex['send_at'])} once you approve")
        except Exception:  # noqa: BLE001
            pass
    # Layout (the owner 2026-10-08, "make it look good"): the ask in plain words; the exact text that
    # would go out in its own block; details folded; one filled green button for the recommended
    # choice, the rest outlined; a small footer. No ids in the body.
    ask, to, preview = _split_preview(q.get("question"))
    if not preview and exec_text(q)[1]:
        preview = exec_text(q)[1]
    ask, to = _names(ask), _names(to)         # the ask names people; the draft below stays exactly as it goes out
    # Three labelled sections, always in this order (the owner 2026-10-17 evening: "I want to know exactly
    # where the draft is, where the details are, and what the approval is about, all clearly stated in
    # separate sections"): THE DECISION (what a tap does), DRAFT (the exact text that goes out, when
    # there is one), DETAILS (the evidence, folded). Advice sits under the decision.
    sections = []
    ask_header = "What you're approving" if ex else "The decision"
    groups = [g for g in (q.get("groups") or []) if isinstance(g, dict) and g.get("rows")]
    if groups:
        # a many-row card folds by subject: one collapsible section per group, its first row visible
        # (example's approvals card by unit, the owner 2026-10-16 "can multiple approval cards be made
        # collapsible?"); the ask keeps only its lead line, the rows live in the sections
        lead = ask.strip().split("\n", 1)[0]
        if lead:
            sections.append({"widgets": [{"textParagraph": {"text": _para(lead, QUESTION_MAX)}}]})
        for g in groups[:10]:
            rows = [str(r) for r in g["rows"]][:8]
            sections.append({"header": str(g.get("title") or "")[:80] + (f" · {len(rows)}" if len(rows) > 1 else ""),
                             "collapsible": len(rows) > 1, "uncollapsibleWidgetsCount": 1,
                             "widgets": [{"textParagraph": {"text": _para(r, 600)}} for r in rows]})
    elif ask.strip():
        sections.append({"header": ask_header, "widgets": [{"textParagraph": {"text": _para(ask, QUESTION_MAX)}}]})
    if preview:
        p = ex.get("payload") or {}
        who = to or ("To " + str(p.get("to") or ", ".join(str(x) for x in (p.get("to_list") or [])) or "")).rstrip()
        what = "Email draft" if "[EMAIL PREVIEW]" in str(q.get("question") or "") or kind == "email_send" else "Draft"
        if who.strip() and who != "To":
            what += " · " + (who if who.lower().startswith(("to ", "from ")) else "to " + who)
        what += (" · has blanks, tap Edit & send to fill them" if draft_blanks(q) else " · goes out exactly as written")
        sections.append({"header": what,
                         "widgets": [{"textParagraph": {"text": _para(preview, QUESTION_MAX)}}]})
    if q.get("advice"):
        # ruling 8 (2026-10-15): the one-line recommendation on every card, example's advice.py shape
        sections.append({"widgets": [{"textParagraph": {"text": _para(q.get("advice"), 300)}}]})
    if q.get("details"):
        sections.append({"header": "Details", "collapsible": True, "uncollapsibleWidgetsCount": 0,
                         "widgets": [{"textParagraph": {"text": _para(q.get("details"), DETAILS_MAX)}}]})
    for r in (q.get("rulings") or [])[:1]:
        sections.append({"widgets": [{"decoratedText": {"topLabel": "Standing ruling", "text": _para(r.get("ruling"), 300), "wrapText": True}}]})
    if state in ("open", "undone"):
        opts = [o for o in (q.get("options") or []) if isinstance(o, dict) and o.get("label")]
        blanks = draft_blanks(q)
        if blanks:
            # a draft with an unfilled blank has no one-tap send: the only way out is the edit box
            # (Marisa, 2026-10-18: the draft went out with both [the owner: ...] blanks in it)
            appr = str(ex.get("approve_label") or "Approve").strip().lower()
            opts = [o for o in opts if str(o["label"]).strip().lower() != appr]
        rec = [i for i, o in enumerate(opts) if _RECOMMENDED.search(str(o["label"]))] if not blanks else []
        if not rec and len(opts) > 1 and ex and not blanks:
            rec = [0]                                   # an approve card: the first button is the approve
        btns = [_button(o["label"], "card_answer", {"qid": qid, "option": o["label"], "q": "0"}, filled=(i in rec))
                for i, o in enumerate(opts)]
        if (btns or blanks) and exec_text(q)[1]:
            # the text an approve would send can be rewritten first: the tap opens a dialog
            edit = _button("Edit & send", "card_edit", {"qid": qid, "q": "0"},
                           interaction="OPEN_DIALOG", filled=bool(blanks))
            btns = ([edit] + btns[:6]) if blanks else (btns[:6] + [edit])
        if btns and not buttons_on():
            # no public endpoint for taps (a new organization before Funnel / a relay): the choices are words,
            # answered in the card's own thread (the owner's-words-are-the-tap path, 2026-10-24)
            names = [_RECOMMENDED.sub("", str(o["label"])).strip() for o in opts][:6]
            line = "Reply in this thread with one of: " + " · ".join(f"<b>{html.escape(n)}</b>" if i in rec else html.escape(n) for i, n in enumerate(names))
            widgets = [{"textParagraph": {"text": line}}]
        else:
            widgets = [{"buttonList": {"buttons": btns[:7]}}] if btns else []
        foot = ("Answer withdrawn, choose again. " if state == "undone" else "") + f'<font color="#777777">Also on <a href="{MC_URL}">Mission Control</a></font>'
        widgets.append({"textParagraph": {"text": foot}})
        sections.append({"widgets": widgets})
    elif state == "answered":
        a = answered or {}
        who = str(a.get("by") or "").split(":")[-1].capitalize() or "an owner"
        chosen = _RECOMMENDED.sub("", str(a.get("option") or "")).strip()
        line = f"{answered_icon(a)} <b>{html.escape(chosen)}</b> · {who}, {time.strftime('%-I:%M %p') if os.name != 'nt' else time.strftime('%I:%M %p').lstrip('0')}"
        if a.get("result"):
            line += f"<br>{html.escape(str(a['result'])[:400])}"
        elif a.get("ticket"):
            line += f"<br><font color=\"#777777\">{html.escape(goes_out_line(kind))}</font>"
        elif a.get("mint_error"):
            line += f"<br><b>Not sent:</b> {html.escape(str(a['mint_error'])[:200])}"
        elif a.get("working"):
            line += "<br><font color=\"#777777\">Working on it. This card updates itself when it is done.</font>"
        widgets = [{"textParagraph": {"text": line}}]
        if a.get("revised"):
            widgets.append({"decoratedText": {"topLabel": "Edited text", "text": _para(a["revised"], 1200), "wrapText": True}})
        btns = [_button("Undo", "card_undo", {"qid": qid})] if buttons_on() else []
        if a.get("ticket") and kind == "imessage_send":
            # an https link (Chat allows no custom schemes) that Mission Control bounces to the
            # shortcuts:// URL; Safari then launches "example-org Send Texts" on the phone. Nothing
            # sends unless the drain already put this approved text in that phone's outbox.
            btns.append({"text": "Send now from phone", "onClick": {"openLink": {"url": f"{MC_URL}/phone/run"}}})
        if btns:
            widgets.append({"buttonList": {"buttons": btns}})
        sections.append({"widgets": widgets})
    else:
        foot = {"expired": "⌛ Expired unanswered.", "consumed": "✅ Done.", "superseded": "↻ Replaced by the revised card in this thread.",
                "resolved": "✅ Recovered on its own, no action needed"
                            + (f": {html.escape(str(card.get('resolved_why'))[:160])}" if card.get("resolved_why") else "") + "."
                }.get(state, state.capitalize() + ".")
        sections.append({"widgets": [{"textParagraph": {"text": foot}}]})
    title = header
    kind_line = ""
    subtitle = " · ".join(x for x in (lane_name(lane), sends) if x)
    # the message's plain text is what the phone notification shows (a card alone shows nothing
    # useful on an iPhone lock screen, the owner 2026-10-07): the header and the first line of the question
    preview = re.sub(r"\s+", " ", ask if ask.strip() else preview).strip()
    text = f"{header}: {preview[:180]}" + ("..." if len(preview) > 180 else "")
    if state == "answered":
        text = f"{header}: answered ({str((answered or {}).get('option') or '')[:60]})"
    elif state not in ("open", "undone"):
        text = f"{header}: {state}"
    return {"text": text[:300],
            "cardsV2": [{"cardId": qid or "card", "card": {"header": {"title": title[:200], "subtitle": subtitle[:200]},
                                                            "sections": sections}}]}


# ------------------------------------------------------------------ the agent's view of cards
def agent_convo(person_key: str) -> str:
    """Where the agent's conversation with this owner lives: the home space, else their DM."""
    return _home_space() or f"dm:{person_key}"


def card_line(card: dict) -> str:
    """One transcript line describing a card, so the agent knows what was asked (2026-10-07: Sage
    could not tell the owner what 'unanswered vendor email' meant because cards never reached its
    conversation; it only ever saw '[tapped X on card qid]')."""
    q = (card.get("questions") or [{}])[0]
    lane, header = str(q.get("lane") or ""), str(q.get("header") or "Decision")
    question = re.sub(r"\s+", " ", str(q.get("question") or "")).strip()
    opts = [str(o.get("label") or "") for o in (q.get("options") or []) if isinstance(o, dict) and o.get("label")]
    details = re.sub(r"\s+", " ", str(q.get("details") or "")).strip()
    line = f"[card {card.get('id', '')}{' · ' + lane if lane else ''}] {header}: {question[:400]}"
    if details:
        line += f" (details: {details[:300]})"
    if q.get("advice"):
        line += " " + re.sub(r"\s+", " ", str(q.get("advice"))).strip()[:220]
    if opts:
        line += " Buttons: " + " | ".join(opts[:6])
    return line


def tap_line(card: dict | None, option: str, qid: str = "") -> str:
    if not card:
        return f"[tapped \"{option}\" on card {qid}]"
    q = (card.get("questions") or [{}])[0]
    question = re.sub(r"\s+", " ", str(q.get("question") or "")).strip()
    return f"[tapped \"{option}\" on the card \"{q.get('header') or 'Decision'}: {question[:140]}\"]"


def card_thread(card: dict) -> str:
    """The Chat thread this card lives in: the one it was posted into, else the thread its own
    message started (spaces/S/messages/ID.ID -> spaces/S/threads/ID). '' when it never reached Chat."""
    if card.get("chat_thread"):
        return str(card["chat_thread"])
    for name in (card.get("chat") or {}).values():
        m = re.match(r"(spaces/[^/]+)/messages/([^/.]+)", str(name))
        if m:
            return f"{m.group(1)}/threads/{m.group(2)}"
    return ""


REPLY_THREAD_HOURS = 6.0


def reply_thread(card: dict, hours: float = REPLY_THREAD_HOURS) -> str:
    """A reply card to someone an owner texted / emailed from a card in the last `hours` belongs in that
    card's Chat thread, where the talk is happening (Marlo, 2026-10-24: his pay question landed apart
    from the live hiring thread). '' when the card has its own place, is not a reply, or no such send."""
    if not isinstance(card, dict) or card.get("chat_thread") or card.get("chat_space"):
        return ""
    try:
        import cards as cards_mod
        import exec_store as es
        q = (card.get("questions") or [{}])[0] or {}
        if not cards_mod._is_reply(q):
            return ""
        to = (((q.get("exec") or {}).get("payload") or {}).get("to")) if isinstance(q.get("exec"), dict) else ""
        to = to[0] if isinstance(to, (list, tuple)) and to else to
        to = str(to or "").strip()
        digits = re.sub(r"\D", "", to)[-10:]
        if not to or ("@" not in to and len(digits) < 10):
            return ""
        best, best_t = "", 0.0
        for t in es.fold().values():
            if t.get("status") != "done" or t.get("kind") not in es.OUTBOUND_KINDS:
                continue
            ref = str(t.get("approval_ref") or "")
            if not ref.startswith("card:"):
                continue
            tt = str((t.get("payload") or {}).get("to") or "")
            if ("@" in to and tt.strip().lower() != to.lower()) or ("@" not in to and re.sub(r"\D", "", tt)[-10:] != digits):
                continue
            try:
                done = time.mktime(time.strptime(str(t.get("ts_done") or "")[:19], "%Y-%m-%dT%H:%M:%S"))
            except ValueError:
                continue
            if time.time() - done > hours * 3600 or done <= best_t:
                continue
            old = cards_mod._read(cards_mod.ARCHIVE / f"{ref[5:]}.json")
            th = card_thread(old) if isinstance(old, dict) else ""
            if th:
                best, best_t = th, done
        return best
    except Exception as e:  # noqa: BLE001
        _log(f"reply_thread failed: {type(e).__name__}: {str(e)[:160]}")
        return ""


def record_card(card: dict) -> int:
    """Write the card into the agent's conversation with each owner it was posted to. Never raises."""
    try:
        import core
        lane = str((card.get("questions") or [{}])[0].get("lane") or "")
        line = card_line(card)
        convos = {agent_convo(k) for k, _ in targets(lane, "card")} or {agent_convo(_first_owner())}
        meta = {"qid": card.get("id"), "thread": card_thread(card)}
        if not card.get("chat") and card.get("delivery") in ("weekly", "mc"):
            meta["held"] = True               # on Mission Control, not in Chat (so a muted class is no leak)
        for c in convos:
            core.say(c, "card", line, channel="card", meta=meta)
        return len(convos)
    except Exception as e:  # noqa: BLE001
        _log(f"record_card crashed: {type(e).__name__}: {str(e)[:200]}")
        return 0


# ------------------------------------------------------------------ the voice (agent/voice.py, 2026-10-27)
def _agent_path() -> None:
    """agent/ at the END of sys.path: at the front it shadows scripts/ modules of the same name (followups.py)."""
    if str(AGENT) not in sys.path:
        sys.path.append(str(AGENT))


def _voice(text: str, channel: str = "", space: str = "") -> str:
    """Every line code posts passes the voice gate: system talk never reaches Chat (its Mission Control note is
    already written), the rest is said the way a person says it. The text comes back unchanged when the
    module is missing, never raises."""
    try:
        _agent_path()
        import voice
        return voice.gate(text, channel, space)
    except Exception as e:  # noqa: BLE001
        _log(f"voice gate failed ({type(e).__name__}: {str(e)[:120]}); posting as is")
        return str(text or "").strip()


def _fresh(space: str, text: str) -> bool:
    try:
        _agent_path()
        import voice
        return voice.fresh(space, text)
    except Exception:  # noqa: BLE001
        return True


def _names(text: str) -> str:
    try:
        _agent_path()
        import voice
        return voice.names(text)
    except Exception:  # noqa: BLE001
        return str(text or "")


# ------------------------------------------------------------------ posting
def _g():
    """The chat channel behind one seam (assistant/channels, WP4 2026-10-24): Google Chat today, by
    `agent/profile.json -> channel`; a test sets ASSISTANT_CHANNEL=null and nothing is posted."""
    if str(HERE.parent) not in sys.path:
        sys.path.insert(0, str(HERE.parent))
    import channels
    return channels.get()


def push_card(card: dict) -> dict:
    """Post an open card to every owner it concerns. Returns {person_key: message name}."""
    out = {}
    if isinstance(card, dict) and card.get("id"):
        try:   # a newer text card closes the older one for the same person (cards.py is protected core)
            import text_gate
            text_gate.supersede_older(card)
        except Exception:  # noqa: BLE001
            pass
    if not enabled() or not isinstance(card, dict) or not card.get("id"):
        return out
    try:
        lane = str((card.get("questions") or [{}])[0].get("lane") or "")
        body = card_body(card, "open")
        g = _g()
        where = targets(lane, "card")
        if str(card.get("chat_space") or "").startswith("spaces/"):
            # asked for in an owner's DM (2026-10-21): the card goes where the ask was made, nowhere else
            sp = str(card["chat_space"])
            who = next((k for k, v in _spaces().items() if v == sp), "dm")
            where = [(who, sp)]
        elif "/threads/" in str(card.get("chat_thread") or "") and \
                str(card["chat_thread"]).split("/threads/")[0] not in [s for _k, s in where]:
            # asked for in a thread of another space (the team space, 2026-10-26: the Odette card handed on
            # after a lint refusal landed in the home space): the thread's own space, nowhere else
            sp = str(card["chat_thread"]).split("/threads/")[0]
            where = [(next((k for k, v in _spaces().items() if v == sp), "space"), sp)]
        th = reply_thread(card)
        if th:
            # a reply in a live conversation goes into the thread of the card that sent the text
            card["chat_thread"] = th
            sp = th.split("/threads/")[0]
            where = [(next((k for k, v in _spaces().items() if v == sp), "dm"), sp)]
        for key, space in where:
            try:
                name = ""
                if fold_multi_on() and not card.get("chat_thread") and not card.get("chat_space"):
                    try:
                        import cards as cards_mod
                        name = _post_stack(card, body, space, key, g, cards_mod)
                    except Exception as e:  # noqa: BLE001
                        _log(f"stack failed, posting alone: {type(e).__name__}: {str(e)[:160]}")
                if not name:
                    name = g.post_body(space, body, str(card.get("chat_thread") or ""))   # a revision lands in the thread it was asked for in
                if name:
                    out[key] = name
            except Exception as e:  # noqa: BLE001
                _log(f"push_card {card['id']} to {key} failed: {type(e).__name__}: {str(e)[:200]}")
        if out:
            _log(f"pushed card {card['id']} to {','.join(out)}")
        missing = unreachable(lane, "card")
        if missing:
            _note_unreachable(missing, lane)
    except Exception as e:  # noqa: BLE001
        _log(f"push_card crashed: {type(e).__name__}: {str(e)[:200]}")
    return out


# ------------------------------------------------------------------ the stack: many open cards, ONE message
# the owner 2026-10-17: "If I ever get multiple cards stacking up on each other in chat delete the last
# one and consolidate both of them or all of them in one card with multiple decisions so I can
# click a drop down to see them all and they don't clutter the chat. This should be built drop
# down like how example does it." example's shape (property vault, turn_agent/chat_app.fold_sections):
# one card, one collapsible section per decision, the header the one visible line, the question,
# facts, advice and buttons under it; the outcome shows on the header once tapped. Here the
# decisions are whole cards (each its own qid, so every button keeps answering its own card):
# a new top-level card that finds other open cards in the space takes their messages down and
# posts one stack message in their place; a tap re-renders the whole stack. Cards posted into a
# thread (a revision asked for there) are never stacked: they belong where they were asked for.
STACKS = SPACES.parent / "chat_stacks.json"


def fold_multi_on() -> bool:
    """Mid-week stacking of top-level cards. Off under the weekly digest (the owner 2026-10-20: "normal
    approvals will be singular cards from now on besides one drop down card with them all together
    once a week"): the only cards that reach Chat between digests are urgent, each on its own."""
    if not bool(_cfg().get("fold_multi", True)):
        return False
    try:
        import cards as cards_mod
        return cards_mod.delivery_policy()["delivery"] != "weekly"
    except Exception:  # noqa: BLE001
        return True


def _stacks() -> dict:
    try:
        return json.loads(STACKS.read_text(encoding="utf-8")) or {}
    except (OSError, ValueError):
        return {}


def _stacks_save(d: dict) -> None:
    try:
        STACKS.parent.mkdir(parents=True, exist_ok=True)
        STACKS.write_text(json.dumps(d, indent=1, ensure_ascii=False), encoding="utf-8")
    except OSError as e:
        _log(f"stacks save failed: {e}")


def stack_of(card: dict) -> dict:
    """The stack this card's Chat message belongs to ({space, msg, qids}), or {}. Stacks are
    keyed by their message name: a space holds several (the 2026-10-17 restack made four)."""
    names = set(str(n) for n in (card.get("chat") or {}).values())
    for msg, st in _stacks().items():
        if msg in names:
            return dict(st, msg=msg)
    return {}


def _stack_set(name: str, qids: list) -> None:
    """Replace the remembered members of one stack message; none left = the stack is forgotten."""
    d = _stacks()
    if name not in d:
        return
    if qids:
        d[name]["qids"] = list(qids)
    else:
        d.pop(name, None)
    _stacks_save(d)


DIGEST_TITLE = "This week's approvals · {n} waiting"          # the weekly digest's title, kept on every re-render


def _stack_remember(space: str, name: str, qids: list, **meta) -> None:
    """`meta` (title / subtitle / lead): how a digest message is headed, so a tap re-renders it
    the same way instead of as a plain stack."""
    d = _stacks()
    d[name] = {"space": space, "qids": list(qids), "ts": time.strftime("%Y-%m-%d %H:%M")}
    d[name].update({k: str(v) for k, v in meta.items() if v})
    # a stack message no live card points at any more (its cards were answered, or folded into a
    # newer stack and this message taken down) is forgotten
    try:
        import cards as cards_mod
        pointed = {str(n) for c in cards_mod._live() if c.get("status") == "open" for n in (c.get("chat") or {}).values()}
        for k in list(d):
            if k != name and k not in pointed:
                d.pop(k, None)
    except Exception:  # noqa: BLE001
        pass
    _stacks_save(d)


def _member_state(qid: str, cards_mod) -> tuple:
    """(card, state, answered) for one stack member from the store."""
    c = cards_mod._read(cards_mod.LIVE / f"{qid}.json")
    if c:
        if c.get("status") == "answered":
            a = cards_mod._answers_index().get(qid) or {}
            return c, "answered", {"option": a.get("option"), "by": str(a.get("by") or "").split(":")[-1],
                                   "ticket": a.get("ticket"), "mint_error": a.get("mint_error"), "revised": a.get("revised_text") or ""}
        return c, "open", None
    c = cards_mod._read(cards_mod.DONE / f"{qid}.json")
    if c:
        return c, str(c.get("status") or "consumed"), None
    return None, "", None


def _flatten(sections: list) -> list:
    """One card's sections as a flat widget list (a section header becomes a grey label line)."""
    out = []
    for s in sections:
        h = str(s.get("header") or "").strip()
        if h:
            out.append({"decoratedText": {"text": f'<font color="#5f6368">{html.escape(h)}</font>', "wrapText": True}})
        out.extend(s.get("widgets") or [])
    return out


_STATE_ICON = {"answered": "✅", "consumed": "✅", "resolved": "✅", "expired": "⌛", "superseded": "↻", "undone": "↩"}


def answered_icon(a: dict | None) -> str:
    """The mark on a tapped card. A green check only means it really happened (the owner 2026-10-24, Delia:
    "I got a green checkmark, but it also says unable to process your request. I need confirmation for
    these types of things that they actually send"): a tap that queued an action shows ⏳ until the
    drain posts its evidence line (push_result), ❌ when the action was refused or failed, ✅ for a
    plain choice or a confirmed result."""
    a = a or {}
    if a.get("mint_error") or a.get("failed"):
        return "❌"
    if a.get("working"):                          # the tap is taken, its action still running (gchat_events)
        return "⏳"
    if a.get("ticket"):
        # the mark comes from the outcome ledger when it has a row (2026-10-24): a text handed to a phone that
        # never acks is ⏳ until a reply or the owner confirms it, never ✅ at hand-off
        st = _outcome_state(str(a["ticket"]))
        if st == "verified":
            return "✅"
        if st == "failed":
            return "❌"
        if st == "unverified":
            return "⚠️"
        if st in ("claimed", "verifying"):
            return "⏳"
    if a.get("ticket") and not a.get("result"):
        return "⏳"
    if a.get("result") and _UNCONFIRMED.search(str(a["result"])):
        return "⚠️"
    return "✅"


# a drain result line that is not a clean confirmation: it went through but the read-back disagrees
# (drain._result_line's "...; check the project" family) or the server refused part of it
_UNCONFIRMED = re.compile(r"\bcheck (the|it|Sent)\b|did not match|did NOT|could not confirm", re.I)


def _outcome_state(ticket_id: str) -> str:
    try:
        import outcomes
        return outcomes.state_for_ticket(ticket_id)
    except Exception:  # noqa: BLE001
        return ""


def result_answered(card: dict, text: str) -> dict | None:
    """The tapped card's answer with the drain's result folded in, for re-rendering its message: a
    result line starting 'Not done' is a failure (❌), anything else is the evidence that it landed."""
    try:
        import cards as cards_mod
        a = cards_mod._answers_index().get(str(card.get("id") or "")) or {}
    except Exception:  # noqa: BLE001
        return None
    if not a.get("option") or a.get("undone"):
        return None
    t = str(text or "").strip()
    return {"option": a.get("option"), "by": str(a.get("by") or "").split(":")[-1], "ticket": a.get("ticket"),
            "revised": a.get("revised_text") or "", "result": t, "failed": t.lower().startswith("not done")}


def stack_body(members: list, title: str = "", subtitle: str = "", lead: str = "") -> dict:
    """members: [(card, state, answered)]. ONE drop-down for the whole card, example's shape
    (property vault, chat_app._one_fold_section; the owner 2026-10-17 "I want exactly what example
    does for unit turn daily checks"): collapsed, the card is its title ("5 decisions waiting ·
    1 done") plus one visible block listing what is inside, one line per lane; Show more opens
    every decision in turn, each under a bold head line (header · state), with its question,
    the text that would go out, advice, details and its own buttons, a divider between them.
    Open cards first, then the decided ones, in posting order."""
    members = [m for m in members if m and m[0]]
    members.sort(key=lambda m: (0 if m[1] in ("open", "undone") else 1, float(m[0].get("ts") or 0)))
    open_n = sum(1 for m in members if m[1] in ("open", "undone"))
    done_n = len(members) - open_n
    by_lane, heads, widgets, lanes = {}, [], [], []
    for n, (card, state, answered) in enumerate(members):
        q = (card.get("questions") or [{}])[0]
        header = str(q.get("header") or "Decision")
        lane = lane_name(str(q.get("lane") or "")) or "Other"
        if lane not in lanes:
            lanes.append(lane)
        icon = answered_icon(answered) if state == "answered" else _STATE_ICON.get(state, "")
        tail = ""
        if state == "answered" and answered:
            tail = " · " + _RECOMMENDED.sub("", str(answered.get("option") or "")).strip()[:30]
        elif state not in ("open", "undone"):
            tail = f" · {state}"
        heads.append(header)
        by_lane.setdefault(lane, []).append((f"{icon} " if icon else "") + header)
        if n:
            widgets.append({"divider": {}})
        head = (f"{icon} " if icon else "") + header + tail
        sub = lane
        if card.get("delivery") == "weekly" and card.get("held_ts"):      # a digest member: how long it has waited
            sub += " · since " + time.strftime("%b %d", time.localtime(float(card["held_ts"])))
            if int(card.get("digest_count") or 0) > 1:
                sub += " · 2nd week, drops after this"
        if card.get("reopen_count"):
            sub += " · came back: not confirmed done"
        widgets.append({"decoratedText": {"text": f"<b>{html.escape(head)}</b>" + f'\n<font color="#5f6368">{html.escape(sub)}</font>',
                                          "wrapText": True}})
        widgets.extend(_flatten(card_body(card, state, answered)["cardsV2"][0]["card"]["sections"]))
    if title:
        title = title.replace("{n}", str(open_n))
        if done_n:
            title += f" · {done_n} done"
    else:
        title = f"{open_n} decision{'s' if open_n != 1 else ''} waiting" if open_n else "All decided"
        if done_n:
            title += f" · {done_n} done"
    inside = "\n".join(f"<b>{html.escape(k)}</b> {html.escape(', '.join(v))}" for k, v in by_lane.items())
    if lead:
        inside = f'<font color="#5f6368">{html.escape(lead)}</font>\n' + inside
    # the description of what is inside stays visible while collapsed; everything else is under Show more
    widgets.insert(0, {"decoratedText": {"text": inside[:1500], "wrapText": True}})
    section = {"widgets": widgets, "collapsible": True, "uncollapsibleWidgetsCount": 1}
    text = f"{title}: " + "; ".join(heads)
    return {"text": text[:300],
            "cardsV2": [{"cardId": "stack", "card": {"header": {"title": title[:200], "subtitle": (subtitle or " · ".join(lanes))[:200]},
                                                     "sections": [section]}}]}


MAX_STACK = 8          # members per stack message (Chat caps a card's size; older open cards stay where they are)


def _stackable_others(card: dict, space: str, cards_mod) -> list:
    """The other open cards whose Chat message sits top-level in this space, at most
    MAX_STACK - 1 of them, newest message first. Cards sharing one message (an existing stack)
    come along whole or not at all: taking some of a stack's cards and deleting its message
    would orphan the rest (2026-10-17 11:30, one card left pointing at a gone message)."""
    by_msg = {}
    for c in cards_mod._live():
        if c.get("id") == card.get("id") or c.get("status") != "open" or c.get("chat_thread"):
            continue
        for n in (c.get("chat") or {}).values():
            if str(n).startswith(space + "/messages/"):
                by_msg.setdefault(str(n), []).append(c)
                break
    groups = sorted(by_msg.values(), key=lambda g: -max(float(c.get("ts") or 0) for c in g))
    out = []
    for g in groups:
        if len(out) + len(g) <= MAX_STACK - 1:
            out.extend(g)
    return out


def _post_stack(card: dict, body_new: dict, space: str, key: str, g, cards_mod) -> str:
    """Fold `card` with the other open cards of the space into one message. Returns the stack
    message name, or '' (then the caller posts the card on its own)."""
    others = _stackable_others(card, space, cards_mod)
    if not others:
        return ""
    old = set()
    for c in others:
        old.update(str(n) for n in (c.get("chat") or {}).values() if str(n).startswith(space + "/messages/"))
    others.sort(key=lambda c: float(c.get("ts") or 0))
    members = [(c, "open", None) for c in others] + [(card, "open", None)]
    body = stack_body(members)
    name = g.post_body(space, body, "")
    if not name:
        return ""
    for n in old:
        try:
            g.delete(n)
        except Exception as e:  # noqa: BLE001
            _log(f"stack: could not take down {n}: {type(e).__name__}")
    for c in others:
        c["chat"] = {k: name for k in (c.get("chat") or {"home": ""})}
        cards_mod._write(cards_mod.LIVE / f"{c['id']}.json", c)
    _stack_remember(space, name, [c["id"] for c in others] + [card["id"]])
    _log(f"stacked {len(members)} cards into one message in {space} ({', '.join(x[0]['id'] for x in members)})")
    return name


def body_for(card: dict, state: str, answered: dict | None = None) -> dict:
    """The message body that shows this card in its new state: the card alone, or, when its
    message is a stack, the whole stack re-rendered with this card's new state.

    A stack holds only what still WAITS (the owner 2026-10-17 evening: "I get a card with a dropdown
    with every decision I ever made ... I only want multiple approvals on one card if they stack on
    top of each other"): the card being marked right now shows its new state (an answered one keeps
    its Undo for the window), every other decided card leaves the drop-down, and the remembered
    members shrink with it. One member left = the plain card, no fold."""
    st = stack_of(card)
    if not st:
        return card_body(card, state, answered)
    try:
        import cards as cards_mod
        members = []
        qids = [q for q in (st.get("qids") or []) if q != card.get("id")] + [str(card.get("id") or "")]
        for qid in qids:
            if qid == card.get("id"):
                members.append((card, state, answered))
            else:
                m = _member_state(qid, cards_mod)
                if m[0] and m[1] in ("open", "undone"):
                    members.append(m)
        _stack_set(st["msg"], [m[0]["id"] for m in members if m[1] in ("open", "undone")])
        if len(members) == 1 and not st.get("title"):
            c, s, a = members[0]
            return card_body(c, s, a)
        return stack_body(members, title=str(st.get("title") or ""), subtitle=str(st.get("subtitle") or ""), lead=str(st.get("lead") or ""))
    except Exception as e:  # noqa: BLE001
        _log(f"body_for stack failed: {type(e).__name__}: {str(e)[:160]}")
        return card_body(card, state, answered)


def _edit_all(card: dict, body: dict, what: str) -> int:
    n = 0
    if not enabled() or not isinstance(card, dict):
        return n
    try:
        g = _g()
        for key, name in (card.get("chat") or {}).items():
            try:
                if g.patch_body(name, body):
                    n += 1
            except Exception as e:  # noqa: BLE001
                _log(f"{what} {card.get('id')} for {key} failed: {type(e).__name__}: {str(e)[:200]}")
    except Exception as e:  # noqa: BLE001
        _log(f"{what} crashed: {type(e).__name__}: {str(e)[:200]}")
    return n


def mark_answered(card: dict, answered: dict) -> int:
    return _edit_all(card, body_for(card, "answered", answered), "mark_answered")


def mark_undone(card: dict) -> int:
    return _edit_all(card, body_for(card, "undone"), "mark_undone")


def goes_out_line(kind: str, hour: int | None = None) -> str:
    """When the approved action really runs (2026-10-21: every card said 'about 35 s'; only texts are that
    quick, everything else waits for the next drain run, and outbound kinds hold overnight)."""
    k = str(kind or "")
    try:
        import exec_store as _es
        h = time.localtime().tm_hour if hour is None else int(hour)
        quiet_now = h >= _es.QUIET_START or h < _es.QUIET_END
        end = f"{_es.QUIET_END}:00 AM"
        outbound = k in _es.OUTBOUND_KINDS
    except Exception:  # noqa: BLE001
        quiet_now, end, outbound = False, "7:00 AM", k in ("imessage_send", "email_send")
    if outbound and quiet_now:
        # the owner 2026-10-24, 10:50 PM: tapped Send, pressed the phone button twice, nothing moved, no word why.
        # The hold stays (his call); the card now says so the moment he taps.
        what = "to your phone" if k == "imessage_send" else "out"
        return f"Held until {end} (quiet hours, 9 pm to 7 am): it goes {what} then. Undo works until it has gone."
    if k == "imessage_send":
        return f"Goes to your phone in about {UNDO_S + 6} s. Undo works until then."
    hold = " (held overnight, 9 pm to 7 am)" if outbound else ""
    return f"Runs at the next pass, within 10 minutes{hold}. Undo works until it has run."


def mark_closed(card: dict, status: str) -> int:
    return _edit_all(card, body_for(card, status), "mark_closed")


def push_result(card: dict, text: str) -> int:
    """The outcome of a card's send, in that card's own Chat thread (and nowhere else): the
    evidence line the owner asked for after a tap (2026-10-15). Not a system note, so it is
    not subject to notes_in_chat. -> messages posted."""
    t = str(text or "").strip()
    if not t or not enabled():
        return 0
    # the card itself trades its ⏳ for ✅ (or ❌) now that the result is known (2026-10-24)
    a = result_answered(card, t)
    if a:
        _edit_all(card, card_body(card, "answered", a), "mark_result")
    thread = card_thread(card)
    if not thread:
        return 0
    space = thread.split("/threads/")[0]
    t = _voice(t, "result", thread)           # "Texted Marlo at 12:19 PM", never a Message-ID (voice.py)
    if not t:
        return 0
    try:
        g = _g()
        if g.post(space, t[:1500], thread):
            try:
                import core
                core.say(agent_convo(_first_owner()), core.profile().get("name", "Sage"), t, channel="result",
                         meta={"qid": card.get("id"), "thread": thread})
            except Exception:  # noqa: BLE001
                pass
            return 1
    except Exception as e:  # noqa: BLE001
        _log(f"push_result failed: {type(e).__name__}: {str(e)[:200]}")
    return 0


def push_say(text: str, lane: str = "", thread: str = "") -> int:
    """A plain line in Sage's own voice to the owners the lane concerns, in the home space (or in
    `thread`), and into the agent's transcript so Sage knows it was said. This is the path for
    everything that is NOT a decision the system can act on: a fact only the owner can supply, a
    thing only he can do, a heads-up (the owner 2026-10-17: "If you need me to do something, it's not
    an approval card. You just need to ask me in the chat."). Not a system note, so notes_in_chat
    does not gate it. -> messages posted."""
    t = str(text or "").strip()
    if not t or not enabled():
        return 0
    t = _voice(t, "say")                      # a person's words or nothing (voice.py, 2026-10-27)
    if not t:
        return 0
    n = 0
    try:
        g = _g()
        name = "Sage"
        try:
            import core
            name = core.profile().get("name", name)
        except Exception:  # noqa: BLE001
            core = None
        shown = g.to_gchat(t[:3800]) if hasattr(g, "to_gchat") else t[:3800]
        for key, space in targets(lane, "card"):
            try:
                if not _fresh(thread or space, t):
                    continue                  # the same words to the same place within the hour
                msg = g.post(space, shown, thread or "")
                if msg:
                    n += 1
                    if core is not None:
                        # a top-level line starts a thread: the transcript row is filed IN that thread and the line is
                        # its root, so an owner's reply under it reads as a reply to it (2026-10-29: the back-taxes line)
                        th = thread or f"{space}/threads/{str(msg).rsplit('/', 1)[-1].split('.')[0]}"
                        core.say(agent_convo(key), name, t, channel="say", meta={"thread": th})
                        if not thread:
                            core.note_thread_root(th, name, t, "say", space)
            except Exception as e:  # noqa: BLE001
                _log(f"push_say to {key} failed: {type(e).__name__}: {str(e)[:200]}")
    except Exception as e:  # noqa: BLE001
        _log(f"push_say crashed: {type(e).__name__}: {str(e)[:200]}")
    return n


def push_text(text: str, lane: str = "") -> int:
    """A plain note to the owners (notify.py). Operator run reports are skipped: each item in
    them already arrived as its own card."""
    t = str(text or "").strip()
    if not t or not enabled():
        return 0
    c = _cfg()
    if not c.get("notes_in_chat"):
        # the owner 2026-10-08: no system notes in Chat at all (SLA "text unanswered" alerts, dreaming
        # updates, scout proposals, heartbeat, operator reports). Chat carries cards and Sage's
        # own words; every note still lands on Mission Control. Flip push.notes_in_chat to true to undo.
        return 0
    if any(t.startswith(p) for p in (c.get("skip_note_prefixes") or [])):
        return 0
    t = _voice(t, "note")
    if not t:
        return 0
    n = 0
    try:
        g = _g()
        for key, space in targets(lane, "note"):
            try:
                if not _fresh(space, t):
                    continue
                if g.post(space, t[:3800]):
                    n += 1
            except Exception as e:  # noqa: BLE001
                _log(f"push_text to {key} failed: {type(e).__name__}: {str(e)[:200]}")
    except Exception as e:  # noqa: BLE001
        _log(f"push_text crashed: {type(e).__name__}: {str(e)[:200]}")
    return n


# ------------------------------------------------------------------ CLI
def backfill() -> int:
    import cards
    n = 0
    for c in cards._live():
        if c.get("status") == "open" and not c.get("chat"):
            chat = push_card(c)
            if chat:
                c["chat"] = chat
                cards._write(cards.LIVE / f"{c['id']}.json", c)
                n += 1
    return n


if __name__ == "__main__":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:  # noqa: BLE001
        pass
    a = sys.argv[1:]
    from alib import business_lane
    biz = business_lane()
    if a[:1] == ["--status"]:
        print(f"enabled={enabled()} cards->{targets(biz)} notes->{targets('', 'note')}")
        miss = unreachable(biz)
        if miss:
            print(f"UNREACHABLE (configured for {biz} cards, no Chat DM with Sage): {', '.join(miss)}")
    elif a[:1] == ["--discover"]:
        print(f"DISCOVERED {discover(force=True) or 'none'}  unreachable={unreachable(biz)}")
    elif a[:1] == ["--backfill"]:
        print(f"BACKFILL pushed={backfill()}")
    elif a[:1] == ["--say"] and len(a) > 1:
        print(f"PUSHED {push_text(a[1])}")
    elif a[:1] == ["--restack"]:
        # fold the open top-level cards already sitting in the home space into stack messages of
        # MAX_STACK, newest first; dry unless --go (it takes the old messages down)
        import cards as cards_mod
        space = _home_space()
        known = set(_stacks())                       # cards already in a stack message are left alone
        live = [c for c in cards_mod._live() if c.get("status") == "open" and not c.get("chat_thread")
                and any(str(n).startswith(space + "/messages/") and str(n) not in known for n in (c.get("chat") or {}).values())]
        live.sort(key=lambda c: -float(c.get("ts") or 0))
        batches = [live[i:i + MAX_STACK] for i in range(0, len(live), MAX_STACK)]
        for b in batches:
            print(f"stack of {len(b)}: " + "; ".join(str((c.get('questions') or [{}])[0].get('header')) for c in b))
        if "--go" in a and space and enabled():
            g = _g()
            for b in batches:
                if len(b) < 2:
                    continue
                newest, rest = b[0], b[1:]
                rest.sort(key=lambda c: float(c.get("ts") or 0))
                members = [(c, "open", None) for c in rest] + [(newest, "open", None)]
                name = g.post_body(space, stack_body(members), "")
                if not name:
                    print("post failed; stopping"); break
                for c in b:
                    for n in set(str(x) for x in (c.get("chat") or {}).values() if str(x).startswith(space + "/messages/")):
                        try:
                            g.delete(n)
                        except Exception as e:  # noqa: BLE001
                            _log(f"restack: could not take down {n}: {type(e).__name__}")
                    c["chat"] = {k: name for k in (c.get("chat") or {"home": ""})}
                    cards_mod._write(cards_mod.LIVE / f"{c['id']}.json", c)
                _stack_remember(space, name, [c["id"] for c in rest] + [newest["id"]])
                print(f"RESTACKED {len(b)} -> {name}")
        elif "--go" in a:
            print("not run: no home space or Chat push disabled")
        else:
            print(f"dry run: {len(live)} open cards in {len(batches)} stack(s); add --go to do it")
    elif a[:1] == ["--rerender"]:
        # patch every remembered stack message with the current shape and states (no re-post)
        import cards as cards_mod
        g = _g()
        for msg, st in list(_stacks().items()):
            # only what still waits: a decided member has left the drop-down (body_for)
            members = [m for m in (_member_state(q, cards_mod) for q in st.get("qids") or []) if m[0] and m[1] in ("open", "undone")]
            _stack_set(msg, [m[0]["id"] for m in members])
            if not members:
                print(f"forgotten {msg} (nothing open)")
                continue
            body = card_body(*members[0]) if len(members) == 1 else stack_body(members)
            ok = g.patch_body(msg, body) if enabled() else False
            print(f"{'RERENDERED' if ok else 'skipped'} {msg} ({len(members)} open cards)")
    else:
        print(__doc__)

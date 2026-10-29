"""Who a card is about, so one decision is asked once whatever its header says.

the owner 2026-10-19: "I still get repeat approval questions sometimes." cards.py's DUPLICATE_SUBJECT /
ALREADY_ANSWERED guards key on the HEADER, and the operator rewords headers between runs
('Nudge - wedding' -> 'Emery nudge', 'Stuck items' -> 'Cold leads'), and exec cards skipped the
duplicate checks entirely (example's upside-down text was carded again 3 minutes after it went).
This module gives every card an ENTITY set that does not depend on wording:

  hb:<workspace id>   a HoneyBook lead / couple: `entity`, a chain key 'lead:<id>:<gate>', an exec
                      payload's workspace_id/project_id, or the client's FULL name ("Finley Larkspur")
                      found in the header, question or details (matched against the HoneyBook snapshot)
  phone:<10 digits>   an imessage_send recipient (or `entity: "phone:+1..."`)
  email:<address>     an email_send recipient

and check(q, ...) refuses, in front of cards.post:

  DUPLICATE_SUBJECT  a decision card about an entity that already has an OPEN decision card in the
                     lane (any header); an exec reply card to a recipient that already has an OPEN
                     exec card of the same kind with the same draft (revise it with --replaces)
  ALREADY_ANSWERED   a decision card about an entity whose latest decision-card answer was a Drop
                     (latched DROP_LATCH_DAYS, released when HoneyBook shows a new stage/activity
                     after the tap or a later non-drop answer); a card offering to nudge / follow up
                     an entity that was just messaged (an approved send in the last SENT_COOLDOWN_H);
                     an exec card whose draft already went (or is queued) to that recipient today

Exec cards that follow a decision (the owner tapped "Draft a nudge") are never blocked by the decision
rules, and a revision (`replaces`) skips everything here. Reads only; any fault returns None so a
bug here can never stop a card.
"""
from __future__ import annotations

import json
import os
import re
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
VAULT = HERE.parents[1]
DROP_LATCH_DAYS = 14
SENT_COOLDOWN_H = 72
EXEC_REPEAT_H = 24
SAME_DRAFT = 0.75             # difflib ratio at which two drafts are the same message
REPLY_KINDS = {"imessage_send", "email_send", "hb_message"}
_DROP = re.compile(r"^\s*(drop|let (it|them|her|him) go|stop (asking|following))\b", re.I)
_CONTACT = re.compile(r"\b(nudge|follow[- ]?up|check[- ]?in|reach out|chase|ping)\b", re.I)
_SENTISH = re.compile(r"^\s*(send|approve|yes|edit & send|edit and send)\b", re.I)


def _snap_file() -> Path:
    return Path(os.environ.get("HB_SNAPSHOT_FILE") or (VAULT / "dashboard" / "data" / "honeybook.json"))


_SNAP_CACHE: dict = {}


def _projects() -> list:
    f = _snap_file()
    try:
        m = f.stat().st_mtime
    except OSError:
        return []
    if _SNAP_CACHE.get("m") != (str(f), m):
        try:
            snap = json.loads(f.read_text(encoding="utf-8-sig"))
        except Exception:  # noqa: BLE001
            snap = {}
        _SNAP_CACHE.update(m=(str(f), m), projects=[p for p in (snap.get("projects") or []) if isinstance(p, dict)])
    return _SNAP_CACHE.get("projects") or []


def _client_name(p) -> str:
    n = re.sub(r"['’]s project\s*$", "", str(p.get("name") or ""), flags=re.I).strip()
    return n if len(n.split()) >= 2 else ""          # full names only: a first name is too loose


def _hb_key(pid) -> str:
    pid = str(pid or "").strip().lower()
    for p in _projects():
        if pid and pid in (str(p.get("id") or "").lower(), str(p.get("project_id") or "").lower()):
            return "hb:" + str(p.get("id") or pid).lower()
    return "hb:" + pid if pid else ""


def _digits(v) -> str:
    d = re.sub(r"\D", "", str(v or ""))
    return d[-10:] if len(d) >= 10 else ""


def _norm(e) -> str:
    e = str(e or "").strip().lower()
    if not e:
        return ""
    kind, _, val = e.partition(":")
    if kind in ("lead", "couple", "hb", "project", "workspace") and re.fullmatch(r"[0-9a-f]{24}", val.split(":")[0]):
        return _hb_key(val.split(":")[0])
    if kind == "phone":
        return "phone:" + _digits(val) if _digits(val) else ""
    if kind == "email" and "@" in val:
        return "email:" + val.strip()
    return e


FOCUS_MAX = 3                 # a card naming more people than this is a digest: it never locks one of them


def _text(q, details=True) -> str:
    return " ".join(str(q.get(k) or "") for k in ("header", "question") + (("details",) if details else ()))


def focus(q) -> set:
    """Who a card is ABOUT: its entities from the header and question only (a digest's list in
    `details` does not count), and nobody when it names more than FOCUS_MAX people."""
    e = entities(q, details=False)
    return e if len(e) <= FOCUS_MAX else set()


def entities(q, details: bool = True) -> set:
    """Every entity a card names (see the module doc). Empty when it names nobody we can key."""
    if not isinstance(q, dict):
        return set()
    out = set()
    ent = q.get("entity") or q.get("entities")
    for e in ([ent] if isinstance(ent, str) else ent or []):
        out.add(_norm(e))
    ck = str(q.get("chain_key") or "")
    if ck.startswith(("lead:", "couple:")):
        out.add(_norm(ck))
    ex = q.get("exec") if isinstance(q.get("exec"), dict) else {}
    pl = ex.get("payload") if isinstance(ex.get("payload"), dict) else {}
    for k in ("workspace_id", "project_id"):
        if re.fullmatch(r"[0-9a-f]{24}", str(pl.get(k) or "")):
            out.add(_hb_key(pl[k]))
    if ex.get("kind") == "imessage_send" and _digits(pl.get("to")):
        out.add("phone:" + _digits(pl.get("to")))
    if ex.get("kind") == "email_send":
        to = pl.get("to")
        for a in (to if isinstance(to, (list, tuple)) else [to]):
            if "@" in str(a or ""):
                out.add("email:" + str(a).strip().lower())
    txt = _text(q, details).lower()
    for p in _projects():
        n = _client_name(p).lower()
        if n and re.search(r"(?<![a-z])" + re.escape(n) + r"(?![a-z])", txt):
            out.add("hb:" + str(p.get("id") or p.get("project_id")).lower())
    out.discard("")
    return out


def _hb_moved_since(ent: str, ts: float) -> bool:
    if not ent.startswith("hb:"):
        return False
    from datetime import datetime
    for p in _projects():
        if ent[3:] not in (str(p.get("id") or "").lower(), str(p.get("project_id") or "").lower()):
            continue
        for k in ("stage_moved_at", "last_activity_at"):
            try:
                d = datetime.fromisoformat(str(p.get(k) or "").replace("Z", "+00:00")).timestamp()
            except ValueError:
                continue
            if d > ts:
                return True
    return False


def _answered(cards_mod, window_s: float) -> list:
    """[(retired_ts, card, question, option)] for every answered card retired inside the window, newest first."""
    now, rows, idx = time.time(), [], cards_mod._answers_index()
    try:     # consumed cards are in DONE; a fresh tap sits in LIVE as 'answered' through the undo window
        files = list(cards_mod.DONE.glob("q*.json")) + list(cards_mod.LIVE.glob("q*.json"))
    except OSError:
        return rows
    for f in files:
        try:
            if now - f.stat().st_mtime > window_s:
                continue
        except OSError:
            continue
        c = cards_mod._read(f)
        if not isinstance(c, dict) or c.get("status") not in ("consumed", "answered"):
            continue
        a = idx.get(c.get("id")) or {}
        if a.get("undone") or not a.get("option"):
            continue
        rows.append((float(c.get("retired_ts") or a.get("epoch") or 0), c, (c.get("questions") or [{}])[0], str(a["option"])))
    rows.sort(key=lambda r: -r[0])
    return rows


def _when(ts: float) -> str:
    return time.strftime("%a %H:%M", time.localtime(ts))


def _who(ents) -> str:
    names = {}
    for p in _projects():
        names["hb:" + str(p.get("id") or "").lower()] = _client_name(p) or str(p.get("name") or "")
    return ", ".join(sorted(names.get(e) or e for e in ents))


def _draft(ex) -> str:
    pl = (ex or {}).get("payload") or {}
    return str(pl.get("body") or pl.get("text") or pl.get("message") or "")


def check(q, lane: str, cards_mod, replaces: str = "") -> dict | None:
    """None when the card may go up, else a refusal {reason, qid} for cards.post."""
    if replaces or not isinstance(q, dict):
        return None
    ex = q.get("exec") if isinstance(q.get("exec"), dict) else None
    ents = entities(q)
    if not ents:
        return None
    lane = str(q.get("lane") or lane or "personal")
    live = [c for c in cards_mod._live() if c.get("status") == "open"]
    if ex and ex.get("kind") in REPLY_KINDS:
        return _check_exec(q, ex, ents, live, cards_mod)
    if ex:
        return None
    for c in live if not q.get("chase_of") else []:     # a chase re-surfaces an open card on purpose (ruling 1)
        cq = (c.get("questions") or [{}])[0]
        if cq.get("exec") or str(cq.get("lane") or "personal") != lane:
            continue
        both = focus(q) & focus(cq)
        if both:
            return {"qid": c["id"], "reason": f"DUPLICATE_SUBJECT: card {c['id']} ({cq.get('header')!r}) is already open about "
                                               f"{_who(both)}; one person, one open question. Stage the ticket on that card "
                                               "(note 'card <qid>'), or revise it with --replaces, instead of posting another."}
    rows = _answered(cards_mod, DROP_LATCH_DAYS * 86400)
    for e in sorted(ents):
        latest = next(((ts, c, cq, opt) for ts, c, cq, opt in rows
                       if not cq.get("exec") and e in focus(cq)), None)
        if latest and _DROP.match(latest[3]) and not _hb_moved_since(e, latest[0]):
            return {"qid": latest[1]["id"], "reason": f"ALREADY_ANSWERED: owner1 chose {latest[3]!r} about {_who({e})} on "
                                                      f"{latest[1]['id']} ({latest[2].get('header')!r}) at {_when(latest[0])}, and "
                                                      "HoneyBook shows nothing new since. Leave them out (re-post without them), never "
                                                      "ask again (owner1 2026-10-17: 'I'm getting the same questions over and over')."}
    if _CONTACT.search(_text(q) + " " + " ".join(str(o.get("label") or "") for o in q.get("options") or [] if isinstance(o, dict))):
        cutoff = time.time() - SENT_COOLDOWN_H * 3600
        for ts, c, cq, opt in rows:
            if ts < cutoff:
                break
            cx = cq.get("exec") or {}
            if cx.get("kind") in REPLY_KINDS and _SENTISH.match(opt) and focus(q) & entities(cq):
                return {"qid": c["id"], "reason": f"ALREADY_ANSWERED: a message to {_who(focus(q) & entities(cq))} was approved on "
                                                  f"{c['id']} ({cq.get('header')!r}) at {_when(ts)}. Wait for their reply; do not "
                                                  "offer another nudge or follow-up yet."}
    return None


def _check_exec(q, ex, ents, live, cards_mod) -> dict | None:
    import difflib
    kind = ex.get("kind")
    draft = re.sub(r"\s+", " ", _draft(ex)).strip().lower()
    for c in live:
        cq = (c.get("questions") or [{}])[0]
        cx = cq.get("exec") or {}
        old = re.sub(r"\s+", " ", _draft(cx)).strip().lower()
        # the same message drafted twice (example, AJ, 'Text example: almost there' x2); a different
        # message to the same person (a standing-text preview, a call offer) is its own card
        if cx.get("kind") == kind and ents & entities(cq) and draft and old \
                and difflib.SequenceMatcher(None, draft, old).ratio() >= SAME_DRAFT:
            return {"qid": c["id"], "reason": f"DUPLICATE_SUBJECT: card {c['id']} ({cq.get('header')!r}) already holds a {kind} draft "
                                               f"to {_who(ents & entities(cq))} saying the same thing. One message, one card: post a new version with "
                                               f"--replaces {c['id']} so it replaces that one."}
    try:
        import exec_store as es
        mine = es._outbound_target(kind, ex.get("payload") or {})
        cutoff = time.time() - EXEC_REPEAT_H * 3600
        for t in es.fold().values():
            if t.get("kind") != kind or t.get("status") not in ("queued", "running", "done"):
                continue
            try:
                tts = time.mktime(time.strptime(str(t.get("ts")), "%Y-%m-%dT%H:%M:%S"))
            except (TypeError, ValueError):
                continue
            if tts < cutoff or es._outbound_target(kind, t.get("payload") or {}).split("|")[:2] != mine.split("|")[:2]:
                continue
            old = re.sub(r"\s+", " ", _draft(t)).strip().lower()
            if draft and old and difflib.SequenceMatcher(None, draft, old).ratio() >= SAME_DRAFT:
                ref = str(t.get("approval_ref") or "").replace("card:", "")
                return {"qid": ref, "reason": f"ALREADY_ANSWERED: this draft was approved at {t.get('ts')} ({ref or t.get('id')}, "
                                              f"ticket {t.get('id')} {t.get('status')}). It is already sent or on its way; "
                                              "never re-card the same message. A failed send comes back as an exec_exception."}
    except Exception:  # noqa: BLE001
        return None
    return None


def dropped_leads(cards_mod=None) -> set:
    """HoneyBook keys ('hb:<id>') whose latest decision-card answer was a Drop that still stands
    (inside DROP_LATCH_DAYS, nothing new in HoneyBook since the tap). Sensors leave these out of
    their tickets, so a dropped lead is not dug up again in next week's cold-leads digest."""
    try:
        if cards_mod is None:
            import cards as cards_mod
        seen, out = set(), set()
        for ts, c, cq, opt in _answered(cards_mod, DROP_LATCH_DAYS * 86400):
            if cq.get("exec"):
                continue
            for e in focus(cq):
                if e.startswith("hb:") and e not in seen:
                    seen.add(e)
                    if _DROP.match(opt) and not _hb_moved_since(e, ts):
                        out.add(e)
        return out
    except Exception:  # noqa: BLE001
        return set()

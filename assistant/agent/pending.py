"""pending.py - the owners' approval queue, deduped and put in front of them in Google Chat. Built 2026-10-18.

Memory/skill-suggestions.md `## Pending` is where everything only an owner may approve waits: rules
that touch the protected core, rules an owner taught that the review wants retired, the nightly
review's asks, and the vault dreaming's protected-core patches. By 2026-10-18 it held 90 entries,
most of them the same few asks re-filed every night, and nothing put them in front of anyone
(the owner: "fix it so they do not duplicate and create a surface for me to approve them in google chat").

  tick = sync with the builder (its dropped requests leave the queue)
       -> dedupe (same kind + same proposal = one entry, the newest wording kept, "raised N times")
       -> age out stale asks (ASK_MAX_DAYS: a dated question nobody answered is moot)
       -> surface: one card per entry in chat, at most MAX_OPEN open at once (the stack folds them)

Who carries what:
  * a code-change ask (builder._CODE_ASK) belongs to the builder, which already asks the owner on
    a "Build needs your OK" card; this module only dedupes it (and surfaces it if the builder
    calls it not_code)
  * rule     -> "Add the rule" adds it as owner-approved / "Drop it"
  * retire   -> "Retire it" / "Keep it"
  * ask      -> "Have Sage do it" queues an operator ticket (outward steps still come back as cards), or a
                doer job for CRM Scheduler hosts, which no connector covers, or a read-only doer look
                for a check on a sheet or a site / "Drop it"
  * patch    -> "Build it" files a build request the builder may build with the owner's OK
                (hard paths never) / "Drop it"
Every answer leaves ## Pending, is recorded under ## Applied automatically or ## Dismissed, and is
latched (LATCH): core.log_suggestion refuses to file the same proposal again.

  python assistant/agent/pending.py status | tick [--dry-run] | dedupe [--dry-run]
"""
from __future__ import annotations

import argparse
import difflib
import re
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import core  # noqa: E402

CARDS = core.DATA / "pending_cards.json"      # key -> {"qid", "ts", "posts"}
MAX_OPEN = 3                                   # pending cards open in chat at once
REPOST_AFTER_S = 3 * 86400                     # an expired, unanswered card comes back once after this
MAX_POSTS = 2
ASK_MAX_DAYS = 7
ADD, DROP = "Add the rule (Recommended)", "Drop it"
RETIRE, KEEP = "Retire it (Recommended)", "Keep it"
DO_IT = "Have Sage do it (Recommended)"
BUILD = "Build it (Recommended)"

_SECTION = re.compile(r"(?ms)^## Pending[ \t]*\n(.*?)(?=^## |\Z)")
_ENTRY = re.compile(r"(?m)^(?:- key: |### key: )")


# ------------------------------------------------------------------ parsing
def _section(text: str):
    m = _SECTION.search(text)
    return (m.start(1), m.end(1)) if m else (None, None)


def entries(text: str | None = None) -> list:
    """Every entry under ## Pending: {"key", "kind", "text", "title", "ts", "raw"}."""
    text = text if text is not None else _read()
    a, b = _section(text)
    if a is None:
        return []
    body = text[a:b]
    starts = [m.start() for m in _ENTRY.finditer(body)]
    out = []
    for i, s in enumerate(starts):
        raw = body[s:starts[i + 1] if i + 1 < len(starts) else len(body)]
        if raw.startswith("### "):
            # the vault dreaming's format: keep its prose up to the next blank-line paragraph that is not part of it
            raw = re.split(r"\n(?=\*\(|\n(?!- |  ))", raw, maxsplit=1)[0].rstrip("\n") + "\n"
        else:
            raw = re.match(r"- key: \S+\n(?:  .*\n?)*", raw).group(0)
        out.append(_parse(raw))
    return out


def _parse(raw: str) -> dict:
    head = raw.split("\n", 1)[0]
    key = re.sub(r"^(?:- key: |### key: )", "", head).split()[0]
    field = lambda name: (re.search(r"\*" + name + r":\*\s*(.*)", raw) or [None, ""])[1].strip()  # noqa: E731
    if raw.startswith("### "):
        kind = "patch"
        title = head.split(None, 3)[3] if len(head.split(None, 3)) > 3 else key
        prop = re.search(r"(?s)Proposed patch:\s*(.*?)(?=\n- \w|\Z)", raw)
        text = title + (". " + re.sub(r"\s+", " ", prop.group(1)).strip() if prop else "")
    elif "-retire-" in key:
        kind, title = "retire", (re.search(r"retiring rule (r\d+)", raw) or [None, ""])[1]
        text = field("Rule") + (" | why: " + field("Why") if field("Why") else "")
    elif "-rule-protected-" in key:
        kind, text = "rule", field("Proposed rule")
        title = text[:80]
    else:
        text = field("Proposal") or field(r"Proposed \w+")
        kind = "build" if _code_ask(raw) else "ask"
        title = text[:80]
    m = re.search(r"(\d{8})-?\d*", key)
    ts = time.mktime(time.strptime(m.group(1), "%Y%m%d")) if m else 0.0
    if not ts:
        fs = re.search(r"First seen:\s*(\d{4}-\d{2}-\d{2})", raw)
        ts = time.mktime(time.strptime(fs.group(1), "%Y-%m-%d")) if fs else time.time()
    return {"key": key, "kind": kind, "text": text, "title": title, "ts": ts, "raw": raw}


def _code_ask(raw: str) -> bool:
    try:
        import builder
        return bool(builder._CODE_ASK.search(raw))
    except Exception:  # noqa: BLE001
        return bool(re.search(r"(?i)\b(?:needs a code change|code change|system change|build request)\b", raw))


def _read() -> str:
    try:
        return core.SUGGESTIONS.read_text(encoding="utf-8")
    except OSError:
        return ""


# ------------------------------------------------------------------ sameness
def _squash(s: str) -> str:
    s = re.sub(r"(?i)^build request\s*\((?:protected core, )?not filed\)\s*:\s*", "", str(s or ""))
    return re.sub(r"[^a-z0-9 ]+", " ", s.lower()).split("|")[0].strip()


def _title_of(e: dict) -> str:
    """A build ask's own name ("Doer report cut off after the first line in chat"), the part before its first colon."""
    t = re.sub(r"(?i)^build request\s*\((?:protected core, )?not filed\)\s*:\s*", "", e["text"])
    return _squash(re.split(r"\s*[:(]", t, maxsplit=1)[0]) if ":" in t[:120] else ""


def same(a: dict, b: dict) -> bool:
    """Two entries are the same proposal: same rule to retire, or the same words near enough."""
    group = lambda k: "ask" if k in ("ask", "build") else k  # noqa: E731
    if group(a["kind"]) != group(b["kind"]):
        return False
    if a["kind"] == "retire":
        return a["title"] == b["title"]
    ta, tb = _title_of(a), _title_of(b)
    if ta and tb and (ta == tb or difflib.SequenceMatcher(None, ta, tb).ratio() >= 0.8):
        return True
    return similar_text(a["text"], b["text"])


_STOP = set("""the and but for with from that this these those are was were been its not can cannot only when which who
what why how all any each every one two three four five also still just about into over under after before because
owner1 owner1's owner6 sage owner owners card cards build builds builder change changes would should could will need
needs needed waiting wait tap tapped asked asks ask yes your you they them there here then than have has had does did
done make makes made some more most""".split())


def _terms(s: str) -> set:
    s = re.sub(r"(?i)^build request\s*\((?:protected core, )?not filed\)\s*:\s*", "", str(s or ""))
    return {w[:6] for w in re.findall(r"[a-z0-9_./-]+", s.lower()) if len(w) >= 4 and w not in _STOP}


def similar_text(x: str, y: str) -> bool:
    """Same proposal, by its content words (stems of 6). Tuned on the 2026-10-18 backlog: real
    rewordings score Jaccard >= .4 or containment >= .7, different asks about the same area stay
    under (.2/.4). Rewordings below that are the dream's job (_waiting_block), not this backstop's."""
    a, b = _terms(x), _terms(y)
    if len(a) < 3 or len(b) < 3:
        return _squash(x) == _squash(y)
    inter = len(a & b)
    return inter / len(a | b) >= 0.4 or inter / min(len(a), len(b)) >= 0.7


# ------------------------------------------------------------------ rewriting the section
def _drop_raw(text: str, raws: list) -> str:
    for r in raws:
        text = text.replace(r, "", 1)
    a, b = _section(text)
    if a is not None:
        text = text[:a] + re.sub(r"\n{3,}", "\n\n", text[a:b]) + text[b:]
    return text


def _write(text: str) -> None:
    core.SUGGESTIONS.write_text(text, encoding="utf-8")


def latch(e: dict, outcome: str) -> None:
    d = core.load(core.PENDING_LATCH, [])
    d.append({"kind": e["kind"], "text": e["text"][:600], "outcome": outcome, "ts": core.stamp(), "key": e["key"]})
    core.save(core.PENDING_LATCH, d[-400:])


def close(e: dict, outcome: str, line: str, section: str) -> None:
    """Take one entry out of ## Pending, record why under `section`, and latch it."""
    with core.locked:
        text = _read()
        if e["raw"] in text:
            _write(_drop_raw(text, [e["raw"]]))
    core.log_suggestion(section, e["key"] + "-" + outcome, [line])
    latch(e, outcome)


def dedupe(dry: bool = False) -> list:
    """Fold repeats into one entry: the NEWEST wording stays (it carries the latest context), with a
    'raised N times since' line; the older copies leave the file. -> [(kept key, [dropped keys])]."""
    with core.locked:
        text = _read()
        es = sorted(entries(text), key=lambda e: e["ts"], reverse=True)
        kept, folds, gone = [], [], []
        for e in es:
            twin = next((k for k in kept if same(k, e)), None)
            if twin is None:
                kept.append(e)
                continue
            gone.append(e["raw"])
            f = next((f for f in folds if f[0] is twin), None)
            if f:
                f[1].append(e)
            else:
                folds.append((twin, [e]))
        if dry or not folds:
            return [(k["key"], [x["key"] for x in xs]) for k, xs in folds]
        for k, xs in folds:
            n = len(xs) + 1 + _raised(k["raw"])
            since = time.strftime("%Y-%m-%d", time.localtime(min([x["ts"] for x in xs] + [k["ts"]])))
            line = f"  *Raised {n} times since {since}.*\n"
            new = re.sub(r"(?m)^  \*Raised \d+ times since [\d-]+\.\*\n", "", k["raw"])
            new = new.rstrip("\n") + "\n" + line
            text = text.replace(k["raw"], new, 1)
            k["raw"] = new
        _write(_drop_raw(text, gone))
    return [(k["key"], [x["key"] for x in xs]) for k, xs in folds]


def _raised(raw: str) -> int:
    m = re.search(r"\*Raised (\d+) times", raw)
    return int(m.group(1)) - 1 if m else 0


def sync_builder(dry: bool = False) -> list:
    """The builder's verdicts close build asks: dropped by the owner -> Dismissed. (Built ones are
    closed by the builder itself on merge.)"""
    st = core.load(core.DATA / "builds.json", {})
    out = []
    for e in entries():
        if e["kind"] != "build":
            continue
        s = st.get(e["key"]) or {}
        if s.get("status") == "dropped":
            out.append(e["key"])
            if not dry:
                close(e, "dropped", f"Dropped by an owner on the builder's card ({s.get('answered', '')}).", "## Dismissed (latched — never re-propose)")
    return out


def age_out(dry: bool = False) -> list:
    """An ask nobody answered in ASK_MAX_DAYS was about a moment that has passed; it leaves the queue
    (recorded, latched, in git). Rules, retirements and patches never age out."""
    cut = time.time() - ASK_MAX_DAYS * 86400
    cards = core.load(CARDS, {})
    out = []
    for e in entries():
        if e["kind"] != "ask" or e["ts"] >= cut or _card_open(cards.get(e["key"])):
            continue
        out.append(e["key"])
        if not dry:
            close(e, "expired", f"Expired {core.stamp()[:10]}: unanswered for {ASK_MAX_DAYS}+ days. Proposal was: {e['text'][:300]}",
                  "## Dismissed (latched — never re-propose)")
    return out


# ------------------------------------------------------------------ the chat surface
def _cards_mod():
    scripts = str(core.AGENT.parent / "scripts")
    if scripts not in sys.path:
        sys.path.append(scripts)
    import cards
    return cards


def _card_open(c: dict | None) -> bool:
    if not c or not c.get("qid"):
        return False
    try:
        return any(x.get("id") == c["qid"] and x.get("status") == "open" for x in _cards_mod()._live())
    except Exception:  # noqa: BLE001
        return False


def _lane(e: dict) -> str:
    t = e["text"]
    if re.search(r"(?i)\b(bank|plaid|simplefin|relay|tax|money|price|quote)\b", t):
        return "money" if not re.search(r"(?i)\b(quote|price)\b", t) else "wedding"
    if re.search(r"(?i)\b(lead|couple|wedding|client|shoot|film|booking|booked)\b", t):
        return "wedding"
    return "personal"


def _short(s: str, n: int = 300) -> str:
    s = re.sub(r"\s+", " ", str(s or "")).strip()
    return s if len(s) <= n else s[:n].rsplit(" ", 1)[0] + "..."


def card_for(e: dict) -> dict | None:
    raised = _raised(e["raw"]) + 1
    again = f" (raised {raised} times)" if raised > 1 else ""
    base = {"lane": _lane(e), "subject": e["key"], "pending_key": e["key"],
            "details": f"Queue entry: {e['key']}\n\n" + _short(e["raw"], 1200)}
    if e["kind"] == "rule":
        return {**base, "header": "New rule needs your OK",
                "question": f"My nightly review wants to add a rule that touches approvals or sending, so it's yours to OK{again}: "
                            f"\"{_short(e['text'], 240)}\". Add it?",
                "advice": "💡 Add it and I follow it from now on; you can undo it from Mission Control.",
                "options": [{"label": ADD, "description": "I follow it from now on"}, {"label": DROP, "description": "never proposed again"}]}
    if e["kind"] == "retire":
        rule, why = (e["text"].split(" | why: ", 1) + [""])[:2]
        return {**base, "header": f"Retire rule {e['title']}?",
                "question": f"You taught me \"{_short(rule, 200)}\". My review thinks it no longer fits: {_short(why, 200)} Retire it?",
                "options": [{"label": RETIRE, "description": "I stop following it (undoable)"}, {"label": KEEP, "description": "it stays, never asked again"}]}
    if e["kind"] == "patch":
        return {**base, "header": "System change needs your OK",
                "question": f"A fix my review found{again}: {_short(e['title'], 160).rstrip('.')}. It touches how messages get sent, "
                            "so it waits for your OK. Want the builder to fix it?",
                "advice": "💡 Build it: the builder makes the change with your OK, tested, with an undo.",
                "options": [{"label": BUILD, "description": "the builder makes it, tested, with an undo"}, {"label": DROP, "description": "never proposed again"}]}
    if e["kind"] == "ask":
        return {**base, "header": "Needs your OK",
                "question": f"From my nightly review{again}: {_short(e['text'], 300)} Want me to take care of it?",
                "advice": "💡 Type an answer instead of tapping if it needs a decision from you; I'll act on what you write.",
                "options": [{"label": DO_IT, "description": "I handle it; anything outward still comes back as a card"},
                            {"label": DROP, "description": "never proposed again"}]}
    return None


def surface(dry: bool = False) -> list:
    """Post cards for the oldest uncarded entries until MAX_OPEN are open. Build asks are the builder's."""
    st = core.load(CARDS, {})
    es = [e for e in entries() if e["kind"] != "build" or _not_code(e["key"])]
    if e_keys := {e["key"] for e in es}:
        st = {k: v for k, v in st.items() if k in e_keys}
    open_n = sum(1 for e in es if _card_open(st.get(e["key"])))
    posted = []
    now = time.time()
    for e in sorted(es, key=lambda e: e["ts"]):
        if open_n >= MAX_OPEN:
            break
        c = st.get(e["key"]) or {}
        if _card_open(c):
            continue
        if c.get("qid") and (int(c.get("posts", 0)) >= MAX_POSTS or now - float(c.get("ts", 0)) < REPOST_AFTER_S):
            continue
        card = card_for(e)
        if not card:
            continue
        if dry:
            posted.append(e["key"]); open_n += 1
            continue
        try:
            r = _cards_mod().post([card], "pending", lane=card["lane"])
        except Exception as ex:  # noqa: BLE001
            r = {"posted": False, "reason": f"{type(ex).__name__}: {ex}"}
        if r.get("posted") or r.get("qid"):
            st[e["key"]] = {"qid": r.get("qid"), "ts": now, "posts": int(c.get("posts", 0)) + 1}
            posted.append(e["key"])
            open_n += 1
        else:
            print(f"pending: card for {e['key']} not posted: {r.get('reason')}", file=sys.stderr)
    if not dry:
        core.save(CARDS, st)
    return posted


def _not_code(key: str) -> bool:
    return (core.load(core.DATA / "builds.json", {}).get(key) or {}).get("status") == "not_code"


# ------------------------------------------------------------------ the owner's tap
def answer(key: str, option: str, text: str = "", by: str = "owner") -> dict:
    """cards.sweep -> here. Does what the button says, closes the entry, says one plain line."""
    e = next((x for x in entries() if x["key"] == key), None)
    if not e:
        return {"ok": False, "reason": "no longer pending"}
    opt = re.sub(r"\s*\((?:recommended)[^)]*\)\s*$", "", str(option or ""), flags=re.I).strip().lower()
    applied, dismissed = "## Applied automatically", "## Dismissed (latched — never re-propose)"
    said = ""
    if opt.startswith("drop") or opt.startswith("keep"):
        close(e, "dropped", f"{'Kept' if opt.startswith('keep') else 'Dropped'} by {by} on a chat card {core.stamp()}.", dismissed)
        said = "Okay, the rule stays." if opt.startswith("keep") else "Dropped; I won't bring that one up again."
    elif e["kind"] == "rule" and opt.startswith("add"):
        with core.locked:
            r = core._rule_add(e["text"], f"approved by {by} on a chat card", by)
        close(e, "approved", f"Approved by {by} on a chat card {core.stamp()}: rule {r.get('id', '')} ({r.get('status')}).", applied)
        said = "Done, that's a rule now. Say the word and I'll undo it."
    elif e["kind"] == "retire" and opt.startswith("retire"):
        ok = core.rule_retire(e["title"], f"owner approved on a chat card ({by})", by=by)
        close(e, "approved", f"Retired {e['title']} by {by} on a chat card {core.stamp()} ({'ok' if ok else 'already inactive'}).", applied)
        said = f"Retired. I'll stop following it; it can come back if you want."
    elif e["kind"] == "patch" and opt.startswith("build"):
        k = _file_build(e, by)
        close(e, "approved", f"Approved by {by} on a chat card {core.stamp()}; filed for the builder as {k}.", applied)
        said = "Got it, the builder has it with your OK. You'll hear from me here when it lands, with an undo."
    elif e["kind"] in ("ask", "build") and opt.startswith("have sage"):
        tid = _queue(e, text, by)
        close(e, "approved", f"Approved by {by} on a chat card {core.stamp()}; handed to {'the doer' if _to_doer(e['text']) else 'the operator'} ({tid or 'already queued'}).", applied)
        said = "On it. Anything that goes out to someone will still come to you as a card first."
    else:
        return {"ok": False, "reason": f"unknown option {option!r}"}
    try:
        _cards_mod().say("pending", _lane(e), said)
    except Exception:  # noqa: BLE001
        pass
    return {"ok": True, "status": opt}


# CRM Scheduler hosts: no connector covers them, so the operator refused the tap as "a connector
# job" (a team member on the Vision Meeting, 2026-10-20). An ask about them goes straight to the doer's browser.
_SCHEDULER_HOSTS = re.compile(r"(?i)\b(co-?host|add host|multiple hosts|scheduler hosts?|session hosts?)\b")
_HOST_STEPS = ("In the CRM Scheduler (Tools > Scheduler > the session type > Attendees & hosts > Multiple hosts): "
               "note the Preview slot count, open the Add host dropdown, pick the person, save the session, then report "
               "the Preview slot count before and after.")
# A check that needs eyes on a sheet or a site (cells A1-A4 on the pricing sheet, 2026-10-19): the operator
# can't open either and handed the tap back with "ask me to have the doer look". It goes to the doer, read-only.
_LOOK = re.compile(r"(?i)\b(check|verify|confirm|look at|read|see (?:if|whether|what)|compare)\b")
_SITE = re.compile(r"(?i)(\b(?:google )?(?:sheets?|spreadsheet|cells?|website|web ?site|site|web ?page)\b|https?://|\b[\w-]+\.(?:com|net|org|io|co)\b|\b[A-Z]{1,2}\d+\s*[-:]\s*[A-Z]{1,2}\d+\b)")
_WRITE = re.compile(r"(?i)\b(change|update|set|edit|fix|add|remove|delete|replace|write|send|post|publish)\b")
_LOOK_STEPS = ("Read-only: open it in the browser, look, and report exactly what you see (the values, cell by cell). "
               "Change, save or send nothing.")


def _is_look(t: str) -> bool:
    return bool(_LOOK.search(t) and _SITE.search(t) and not _WRITE.search(t))


def _to_doer(t: str) -> bool:
    return bool(_SCHEDULER_HOSTS.search(t) or _is_look(t))


def _queue(e: dict, text: str, by: str) -> str:
    import actions
    person = core.person_for(handle=by) or core.person_for(handle="owner1") or {"key": by, "name": by.title()}
    t = e["text"] + (f"\n\nThe owner's answer on the card: {text}" if text else "")
    ctx = {"convo": core.home_space() or f"dm:{core.first_owner()}", "thread": ""}
    if _SCHEDULER_HOSTS.search(e["text"]):
        return actions._queue_do("wedding", t + "\n\n" + _HOST_STEPS, person, ctx)
    if _is_look(e["text"]):
        return actions._queue_do(_lane(e), t + "\n\n" + _LOOK_STEPS, person, ctx)
    return actions._queue_task(_lane(e), t, person, ctx)


def _file_build(e: dict, by: str) -> str:
    """A protected-core patch the owner approved becomes a build request the builder may build with
    that approval (builder.owner_answer semantics: any protected file but the hard paths)."""
    import wiki_writer
    title = ("Build request " + re.sub(r"\s+", " ", e["title"]))[:90]
    x = wiki_writer.write({"title": title, "type": "workflow", "lane": "personal",
                           "body": "## Build request\n\n" + e["raw"] + f"\n\n**Approved by {by} on a chat card {core.stamp()}.**"}, by=by)
    key = str(x.get("path") or "")
    if key:
        key = Path(key).resolve().relative_to(core.VAULT).as_posix() if Path(key).is_absolute() else key
        with core.locked:                # one lock for every builds.json writer (builder.save_state, owner taps)
            st = core.load(core.DATA / "builds.json", {})
            st[key] = {"title": title, "attempts": 0, "status": "approved", "owner_ok": ["*"], "answered": core.stamp(), "by": by}
            core.save(core.DATA / "builds.json", st)
        try:
            import builder
            builder.kick(force=True)
        except Exception:  # noqa: BLE001
            pass
    return key


# ------------------------------------------------------------------ cli
def tick(dry: bool = False) -> dict:
    return {"builder_closed": sync_builder(dry), "folded": dedupe(dry), "expired": age_out(dry), "carded": surface(dry)}


def status() -> str:
    es = entries()
    by = {}
    for e in es:
        by[e["kind"]] = by.get(e["kind"], 0) + 1
    return f"{len(es)} pending: " + ", ".join(f"{k} {v}" for k, v in sorted(by.items()))


if __name__ == "__main__":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:  # noqa: BLE001
        pass
    ap = argparse.ArgumentParser()
    ap.add_argument("cmd", choices=["status", "tick", "dedupe"])
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args()
    if a.cmd == "status":
        print(status())
    elif a.cmd == "dedupe":
        for k, xs in dedupe(a.dry_run):
            print(f"{k} <- {len(xs)}: {', '.join(xs)}")
    else:
        r = tick(a.dry_run)
        print(f"PENDING closed={len(r['builder_closed'])} folded={sum(len(x[1]) for x in r['folded'])} "
              f"expired={len(r['expired'])} carded={len(r['carded'])} :: {status()}")

"""dream.py - the agent's nightly learning pass over its own conversations, and the daily digest.

In-chat learning catches the correction someone states outright. This pass sees the day as
a whole: the preference nobody spelled out, the fact mentioned in passing, the thing asked
for three times that should be a schedule or a wiki note, the rule a later message
contradicted. Ported from the property OS's chat-agent dream, with two additions: it may
write wiki notes and create schedules, through the same writers the chat uses.

  nightly = prepare (zero-LLM) -> one read-only model turn -> apply (zero-LLM, validated)
            -> hand the transcript to the vault's own dreaming -> digest -> commit state/

Rails
  * Evidence: every change cites a transcript ref (t1, t2 ...) that exists in the packet.
  * Caps per night come from profile.json `dream`.
  * Protected core never self-edits (core.rule_add queues it under ## Pending).
  * A question for the owners (for_owners) becomes ONE card a day on their phones; only a
    protected-core proposal waits in ## Pending.
  * A rule an owner taught is never retired by the dream; that becomes a proposal.
  * Everything applied lands in the change ledger, so the digest lists it with its undo.

  python assistant/agent/dream.py nightly [--dry-run]
  python assistant/agent/dream.py pass [--min-rows 6]   (daytime: same learning, posts only what it just changed)
  python assistant/agent/dream.py digest [--hours 24] [--print]
  python assistant/agent/dream.py status
"""
from __future__ import annotations

import argparse
import json
from datetime import datetime
import os
import re
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import commitments  # noqa: E402
import core  # noqa: E402
import routines  # noqa: E402
import skills  # noqa: E402
import wiki_writer  # noqa: E402

STATE = core.DATA / "dream_state.json"
PACKET = core.DATA / "dream_packet.json"
JOURNAL = core.DATA / "dream_journal.jsonl"
MAX_ROWS = 400
PASS_MIN_ROWS = 6        # a daytime pass waits for this many new lines; a nightly run takes any

INSTRUCTIONS = """You are reviewing your own conversations since your last review. The transcript is below; each
line has a ref (t1, t2 ...). Decide what you should carry forward so the owners never have to repeat themselves.

Look for: a correction or preference that was implied but never saved as a rule; a durable fact about a person,
the home or the business that came up in passing; knowledge worth a wiki note (a person's profile, an appliance,
a how-we-do-it); something they asked for repeatedly that should be a schedule; a rule or fact that a later
message contradicted; two rules that say the same thing.

Also learn the way a person learns a job (owner1 2026-10-09):
- A SKILL: a job that went through the doer or the operator (its report is in the transcript), or an owner
  walking you through how something works, or a correction about how a kind of task should be handled. Write
  the how-to: when it applies, the steps, what to watch out for, what went wrong last time. Refine an existing
  skill rather than adding a near-duplicate (the list of skills you hold is in your context).
- A BUILD REQUEST: something the owners asked for that the system could not do, did wrong, or that you had to
  say no to; a hand-off that failed or slipped (the OUTCOME lines, written by code, say what happened). Describe
  what should change, why, and the evidence. The builder reads these and writes the code. Never ask for
  anything in the protected core (sending, approvals, money, credentials, footage, your own tool list).
- A broken promise (an OUTCOME line marked unkept/failed) is also a lesson about how you phrase things: say
  less, or say it only once the record shows it.
- The SCORECARD lines are the week in numbers, written by code: the weakest kind of action, the failure classes,
  loops whose output nobody acts on (DEAD / IGNORED), lessons that did not reduce the owners' edits. Propose at
  most ONE rule, skill or build request against the worst of them, citing the SCORECARD row; an IGNORED loop is a
  for_owners proposal to switch it off; a lesson with no effect is a rules_retire or a reworded rules_add.

Be conservative. One day's chatter rarely justifies more than a few changes. Do not restate what the active
rules and memory already hold. Never propose anything in the protected core as a rule or fact; put it in
for_owners instead.

Reply with ONE JSON object and nothing else:
{"summary": "one or two sentences on what you learned",
 "rules_add": [{"rule": "...", "why": "...", "evidence": "t3"}],
 "rules_retire": [{"id": "r2", "why": "...", "evidence": "t9"}],
 "memory_upsert": [{"kind": "person|preference|household|business|process", "subject": "...", "fact": "...", "evidence": "t4"}],
 "memory_retire": [{"id": "m3", "why": "..."}],
 "notes": [{"title": "...", "type": "person|asset|provider|account|concept|reference|workflow|vendor|venue", "lane": "personal|home|money|wedding", "tags": [], "body": "markdown", "evidence": "t5 t6"}],
 "routines": [{"title": "...", "when": {"daily": "07:30"}, "to": "owner1|owner6|both", "say": "...", "evidence": "t7"}],
 "skills": [{"title": "...", "triggers": ["..."], "lane": "personal|home|money|wedding", "via": "do|task|answer", "body": "markdown how-to", "evidence": "t9"}],
 "build_requests": [{"title": "...", "lane": "personal|home|money|wedding", "what": "what should change", "why": "...", "evidence": "t10"}],
 "for_owners": [{"proposal": "...", "evidence": "t8"}]}
Use empty lists where there is nothing."""


def _waiting_block() -> str:
    """What already waits on the owners (## Pending, on cards in chat via pending.py, and the builder's
    open requests). 2026-10-18: the review could not see it and re-filed the same few asks every night,
    reworded each time (90 entries). It must not raise any of these again."""
    try:
        import pending
        items = [f"- {e['text'][:220]}" for e in pending.entries()]
    except Exception:  # noqa: BLE001
        items = []
    try:
        import builder
        items += [f"- (build) {r['title'][:160]}" for r in builder.collect() if r.get("source") == "wiki"]
    except Exception:  # noqa: BLE001
        pass
    if not items:
        return ""
    return ("\n\nALREADY WAITING ON THE OWNERS (each is already on a card or with the builder). Do NOT raise any of these "
            "again in for_owners, rules_add or build_requests, not even reworded or as a reminder that it is still "
            "waiting; the owners see them. Only raise something genuinely new.\n" + "\n".join(items[:80]))


def prepare(now: datetime | None = None) -> dict | None:
    st = core.load(STATE, {})
    start = int(st.get("rows_seen", 0))
    all_rows = core.rows(core.TRANSCRIPT)
    new = [r for r in all_rows[start:] if r.get("channel") not in ("routine", "digest")][-MAX_ROWS:]
    if not new:
        return None
    # what became of the day's hand-offs, as code saw it (kept, failed, unkept): the review learns from
    # outcomes, not just from words (the owner 2026-10-09: constant improvement, like a human would)
    try:
        for it in commitments.recent(30, now=now)[-20:]:
            new.append({"ts": it.get("closed") or it.get("created") or "", "convo": it.get("convo", ""), "who": "code",
                        "channel": "outcome", "text": f"OUTCOME {it.get('status')}: {it.get('kind')} \"{it.get('what')}\""
                                                      + (f" ({it.get('detail')})" if it.get("detail") else "")})
    except Exception:  # noqa: BLE001
        pass
    # the week's numbers and the loops / lessons nobody is served by (scorecard.py, WP6): the review proposes at
    # most one rule, skill or build request against the worst of them, citing the row
    try:
        sys.path.insert(0, str(core.AGENT.parent / "scripts"))
        import scorecard
        for line in scorecard.dream_rows(scorecard.compute(7))[:8]:
            new.append({"ts": core.stamp() if hasattr(core, "stamp") else "", "convo": "", "who": "code", "channel": "scorecard", "text": line})
    except Exception:  # noqa: BLE001
        pass
    refs = {}
    for i, r in enumerate(new, 1):
        r["ref"] = f"t{i}"
        refs[r["ref"]] = r.get("ts", "")
    packet = {"generated": core.stamp(), "rows_end": len(all_rows), "refs": refs, "rows": new}
    core.save(PACKET, packet)
    return packet


def _cite_ok(ev, refs: dict) -> bool:
    cites = re.findall(r"\bt\d{1,4}\b", " ".join(ev) if isinstance(ev, list) else str(ev or ""))
    return bool(cites) and all(c in refs for c in cites)


def _json_from(text: str) -> dict | None:
    m = re.search(r"\{.*\}", str(text or ""), re.S)
    if not m:
        return None
    try:
        d = json.loads(m.group(0))
        return d if isinstance(d, dict) else None
    except ValueError:
        return None


def apply(out: dict, packet: dict, dry: bool = False) -> list:
    caps = core.profile().get("dream") or {}
    refs = packet.get("refs") or {}
    owners = set((core.people().get("users") or {}))
    by = core.profile().get("slug", "agent") + "-dream"
    res = []

    def note(op, status, what, reason=""):
        res.append({"op": op, "status": status, "what": str(what)[:200], "reason": reason})

    for r in (out.get("rules_add") or [])[:int(caps.get("rules_add", 3))]:
        if not _cite_ok(r.get("evidence"), refs):
            note("rule_add", "refused", r.get("rule"), "no valid evidence ref"); continue
        if dry:
            note("rule_add", "pending" if core.is_protected(r.get("rule", "")) else "would-apply", r.get("rule")); continue
        x = core.rule_add(str(r.get("rule") or ""), f"dream [{r.get('evidence')}]: {str(r.get('why') or '')[:300]}", by=by)
        note("rule_add", x["status"], r.get("rule"), x.get("reason", "") or x.get("id", ""))

    active = {r["id"]: r for r in core.rules_active()}
    for r in (out.get("rules_retire") or [])[:int(caps.get("rules_retire", 3))]:
        rid = str(r.get("id") or "")
        cur = active.get(rid)
        if not cur:
            note("rule_retire", "refused", rid, "no such active rule"); continue
        if not _cite_ok(r.get("evidence"), refs):
            note("rule_retire", "refused", rid, "no valid evidence ref"); continue
        if cur.get("by") in owners:
            if not dry:
                core.log_suggestion("## Pending", f"{by}-retire-{rid}-{time.strftime('%Y%m%d')}", [
                    f"**The nightly review suggests retiring rule {rid} ({cur.get('by')} taught it, so it was not retired).**",
                    f"*Rule:* {cur['rule']}", f"*Why:* {str(r.get('why') or '')[:300]} [{r.get('evidence')}]",
                    f"*Apply:* tell the agent \"drop rule {rid}\" if you agree."])
            note("rule_retire", "pending", cur["rule"], "an owner taught it"); continue
        ok = True if dry else core.rule_retire(rid, f"dream: {str(r.get('why') or '')[:160]}", by=by)
        note("rule_retire", "applied" if ok else "failed", cur["rule"])

    for f in (out.get("memory_upsert") or [])[:int(caps.get("memory_upsert", 10))]:
        if not _cite_ok(f.get("evidence"), refs):
            note("memory", "refused", f.get("fact"), "no valid evidence ref"); continue
        if dry:
            note("memory", "would-apply", f.get("fact")); continue
        x = core.mem_upsert(str(f.get("kind") or ""), str(f.get("subject") or ""), str(f.get("fact") or ""),
                            evidence=f"dream {packet.get('generated', '')[:10]} [{f.get('evidence')}]", by=by)
        note("memory", x["status"], f.get("fact"), x.get("reason", "") or x.get("id", ""))

    for f in (out.get("memory_retire") or [])[:int(caps.get("memory_retire", 6))]:
        ok = True if dry else core.mem_forget(str(f.get("id") or ""), f"dream: {str(f.get('why') or '')[:160]}", by=by)
        note("memory_retire", "applied" if ok else "refused", f.get("id"))

    for n in (out.get("notes") or [])[:int(caps.get("notes", 3))]:
        if not _cite_ok(n.get("evidence"), refs):
            note("note", "refused", n.get("title"), "no valid evidence ref"); continue
        if dry:
            note("note", "would-apply", n.get("title")); continue
        x = wiki_writer.write(n, by=by)
        if x["status"] == "refused" and x.get("suggest") and x.get("gap"):   # recorded as a gap; filed under the nearest type
            x = wiki_writer.write(dict(n, type=x["suggest"]), by=by)
        note("note", x["status"], n.get("title"), x.get("reason", "") or x.get("path", ""))

    for s in (out.get("routines") or [])[:int(caps.get("routines", 1))]:
        if not _cite_ok(s.get("evidence"), refs):
            note("routine", "refused", s.get("title"), "no valid evidence ref"); continue
        if dry:
            note("routine", "would-apply", s.get("title")); continue
        x = routines.add(s, by=by)
        note("routine", x["status"], s.get("title"), x.get("reason", "") or x.get("id", ""))

    for k in (out.get("skills") or [])[:int(caps.get("skills", 2))]:
        if not _cite_ok(k.get("evidence"), refs):
            note("skill", "refused", k.get("title"), "no valid evidence ref"); continue
        if dry:
            note("skill", "would-apply", k.get("title")); continue
        x = skills.write(k, by=by)
        note("skill", x["status"], k.get("title"), x.get("reason", "") or x.get("path", ""))

    for b in (out.get("build_requests") or [])[:int(caps.get("build_requests", 2))]:
        if not _cite_ok(b.get("evidence"), refs):
            note("build_request", "refused", b.get("title"), "no valid evidence ref"); continue
        what, why = str(b.get("what") or "").strip(), str(b.get("why") or "").strip()
        title = str(b.get("title") or "").strip()
        protected = core.is_protected(what + " " + why + " " + title)
        if dry:
            note("build_request", "would-apply", title); continue
        # a request that touches the protected core is FILED like any other (until 2026-10-18 it was parked
        # under ## Pending as "not filed" and nothing ever read it); the builder's card in chat is the gate
        body = ("## Build request\n\n" + what + ("\n\n**Why:** " + why if why else "")
                + ("\n\n**Touches the protected core:** the builder asks the owner on a card before changing any of it." if protected else "")
                + f"\n\n**Evidence:** transcript {b.get('evidence')} ({packet.get('generated', '')[:10]}, nightly review)")
        x = wiki_writer.write({"title": ("Build request " + title)[:90] if not title.lower().startswith("build request") else title,
                               "type": "workflow", "lane": b.get("lane") or "personal", "body": body}, by=by)
        note("build_request", x["status"], title, x.get("reason", "") or x.get("path", ""))
        if x["status"] in ("applied", "appended"):
            commitments.register("build", title, x.get("path", ""), {"key": by}, {"convo": core.home_space() or f"dm:{core.first_owner()}", "thread": ""})

    asks = []
    for q in (out.get("for_owners") or [])[:3]:
        txt = str(q.get("proposal") if isinstance(q, dict) else q)[:400]
        ev = q.get("evidence") if isinstance(q, dict) else ""
        if not txt.strip():
            continue
        if core.is_protected(txt):
            # a protected-core change waits in the gated queue for a working session
            if not dry:
                _log_owner_ask(by, len(res), txt, ev)
            note("for_owners", "pending", txt, "protected core")
        else:
            asks.append((txt, ev))
    if asks:
        # everything else is a QUESTION, often dated ("who shoots Oct 10?"): it goes to the owners'
        # phones as a card. Until 10/06 it only reached skill-suggestions.md, which nobody opens.
        status = "would-card" if dry else _card_owner_asks(asks, by, len(res))
        for txt, _ in asks:
            note("for_owners", status, txt)
    return res


ASKS = core.DATA / "dream_asks.json"
ASK_CARDS_PER_DAY = 1        # the roast's cap: a dream question is one card a day, the rest fold into its details
_WEDDING = re.compile(r"wedding|shoot|couple|booking|booked|honeybook|client|venue|inquir", re.I)


def _log_owner_ask(by, n, txt, ev):
    core.log_suggestion("## Pending", f"{by}-ask-{time.strftime('%Y%m%d-%H%M%S')}-{n}", [
        "**The agent's nightly review proposes a change only an owner may make (not applied).**",
        f"*Proposal:* {txt}", f"*Evidence:* {ev}"])


def _card_owner_asks(asks, by, n) -> str:
    """ONE card for tonight's questions (first one asked, the rest in its details), at most
    ASK_CARDS_PER_DAY a day across the nightly run and the daytime passes. Over the cap, or if
    the card cannot be posted, the questions go to ## Pending as before: never lost."""
    day = time.strftime("%Y-%m-%d")
    st = core.load(ASKS, {})
    if int((st.get(day) or {}).get("n", 0)) >= ASK_CARDS_PER_DAY:
        for txt, ev in asks:
            _log_owner_ask(by, n, txt, ev)
        return "pending"
    first = asks[0][0]
    rest = [t for t, _ in asks[1:]]
    lane = "wedding" if _WEDDING.search(" ".join(t for t, _ in asks)) else "personal"
    # a question for the owner is a chat line, not a card: a "Got it" button is nothing he can
    # decide (the owner 2026-10-17 evening, NOT_A_DECISION). He answers in words, here.
    text = "From my review of yesterday's chats: " + first.rstrip() + (" Also: " + " ".join(rest) if rest else "") + \
           " Answer here whenever suits you."
    try:                                   # switched off at an owner's word (reflex.py): the asks wait for the Monday digest
        import reflex
        if not reflex.allowed("review_summary"):
            for txt, ev in asks:
                _log_owner_ask(by, n, txt, ev)
            return "pending"
    except Exception:  # noqa: BLE001
        pass
    try:
        scripts = str(core.AGENT.parent / "scripts")
        if scripts not in sys.path:
            sys.path.append(scripts)       # appended: agent/followups.py and scripts/followups.py share a name
        import cards
        r = cards.say(by, lane, text)
    except Exception as e:  # noqa: BLE001
        r = {"said": False, "reason": f"{type(e).__name__}: {e}"}
    if not r.get("said"):
        for txt, ev in asks:
            _log_owner_ask(by, n, txt, ev)
        return "pending"
    st[day] = {"n": int((st.get(day) or {}).get("n", 0)) + 1, "said": text[:200]}
    core.save(ASKS, {k: v for k, v in st.items() if k >= time.strftime("%Y-%m-%d", time.localtime(time.time() - 14 * 86400))})
    return "said"


def _to_daily_log(packet: dict, summary: str) -> None:
    """Hand the conversation to the vault's own dreaming (06:30), which reads Memory/Daily_Logs:
    that is what turns chat into memory.md facts, wiki promotions and skill patches."""
    name = core.profile().get("name", "Agent")
    day = time.strftime("%Y-%m-%d")
    path = core.VAULT / "Memory" / "Daily_Logs" / f"{day}.md"
    lines = [f"\n## {name} chat ({packet.get('generated')})", ""]
    if summary:
        lines += [f"*{name}'s own summary:* {summary}", ""]
    lines += [f"- [{r.get('ts', '')[11:]}] **{r.get('who')}**: {str(r.get('text', '')).replace(chr(10), ' ')[:400]}"
              for r in packet.get("rows", [])]
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "a", encoding="utf-8") as f:
            f.write("\n".join(lines) + "\n")
    except OSError:
        pass


def _changes(hours: float) -> list:
    ch = [c for c in core.changes_since(hours) if c.get("kind") != "undo"]
    undone = {(c.get("undo") or {}).get("of") for c in core.changes_since(hours) if c.get("kind") == "undo"}
    return [c for c in ch if c["id"] not in undone]


def digest_text(hours: float = 24.0) -> str:
    """The Mission Control version: every change with its undo id."""
    ch = _changes(hours)
    if not ch:
        return ""
    name = core.profile().get("name", "Agent")
    lines = [f"{name}'s changes in the last {int(hours)} h ({len(ch)}). Reply /undo cN to reverse one."]
    lines += [f"{c['id']}  {c['summary'][:150]}" + (f"  (by {c['by']})" if c.get("by") else "") for c in ch[-25:]]
    return "\n".join(lines)


_SHAPES = {   # ledger summary -> the plain phrase (ids dropped). Group label, regex, group of the phrase
    "rule": ("New habits", re.compile(r"^rule \w+:\s*(.+)$", re.S)),
    "memory": ("Remembered", re.compile(r"^remembered \w+ \([^)]*\):\s*(.+)$", re.S)),
    "note": ("Notes written", re.compile(r"^wiki note (?:.*/)?([^/]+?)\.md", re.S)),
    "event": ("Calendar", re.compile(r"^calendar event '(.+?)' \S+ \S+ on (\w+)'s calendar", re.S)),
    "routine": ("Reminders set", re.compile(r"^(?:routine|schedule) \w+:\s*(.+)$", re.S)),
    "schema": ("The wiki grew", re.compile(r"^schema: (.+?)(?:, (?:asked for \d+ times|an owner asked for it))?$", re.S)),
}


def digest_chat(hours: float = 24.0) -> str:
    """The chat version (the owner 2026-10-07: no rule or record numbers, nothing that reads like a
    status report): a short morning note in plain words; the ids stay on Mission Control."""
    ch = _changes(hours)
    if not ch:
        return ""
    groups: dict = {}
    for c in ch:
        label, rx = _SHAPES.get(str(c.get("kind")), ("", None))
        if not rx:
            continue                                        # follow-ups and the like: not worth a morning line
        m = rx.match(str(c.get("summary") or "").strip())
        if not m:
            continue
        t = m.group(1).strip().rstrip(".")
        if c.get("kind") == "event":
            t = f"'{t}' on {m.group(2).capitalize()}'s calendar"
        if len(t) > 110:                                    # cut at a word, never mid-name
            t = t[:110].rsplit(" ", 1)[0] + "..."
        groups.setdefault(label, []).append(t)
    if not groups:
        return ""
    parts = []
    for label, items in groups.items():
        shown = items[:2]
        more = len(items) - len(shown)
        parts.append(f"{label}: " + "; ".join(shown) + (f" (+{more} more)" if more else ""))
    return "Morning. A few things I picked up yesterday. " + ". ".join(parts) + ". If any of that's off, just tell me and I'll drop it."


def digest(hours: float = 24.0, print_only: bool = False) -> int:
    text = digest_text(hours)
    if not text:
        print("DIGEST nothing changed")
        return 0
    if print_only:
        print(text)
        return 0
    chat = digest_chat(hours)
    # the owner 2026-10-08: no dreaming updates in Chat. The plain-words version goes into the
    # transcript (so Sage can answer "what did you learn?") and the full list to Mission Control.
    core.say(core.home_space() or f"dm:{core.first_owner()}", core.profile().get("name", "Agent"), chat, channel="digest")
    try:                                                   # the full list, with undo ids: Mission Control only
        subprocess.run(creationflags=core.NO_WINDOW, args=[sys.executable, str(core.AGENT.parent / "scripts" / "notify.py")], input=text, text=True,
                       encoding="utf-8", cwd=str(core.AGENT.parent / "scripts"), timeout=30, capture_output=True,
                       env={**os.environ, "CHAT_PUSH": "off"})
    except (OSError, subprocess.TimeoutExpired):
        pass
    print(f"DIGEST posted ({len(chat)} chars to chat, {len(text)} to Mission Control)")
    return 0


def merge_rules(dry: bool = False) -> list:
    """Fold near-duplicate active rules (pending.similar_text) so the list stays short without any cap
    retiring an owner's rule (2026-10-21). A dream-written rule folds into an earlier one; an owner's rule
    is never retired here: when an owner's later rule repeats a dream-written earlier one, the dream's
    goes. Each fold is a ledgered, undoable retirement. -> [{"kept", "folded"}]."""
    import pending
    owners = set((core.people().get("users") or {}))
    rules = sorted(core.rules_active(), key=lambda r: str(r.get("added") or ""))
    out, gone = [], set()
    for i, a in enumerate(rules):
        if a["id"] in gone:
            continue
        for b in rules[i + 1:]:
            if b["id"] in gone or not pending.similar_text(a["rule"], b["rule"]):
                continue
            a_owner, b_owner = a.get("by") in owners, b.get("by") in owners
            if b_owner and a_owner:
                continue                                   # two owner rules: theirs to reconcile
            victim, keeper = (a, b) if (b_owner and not a_owner) else (b, a)
            if not dry:
                core.rule_retire(victim["id"], f"folded into {keeper['id']} (nightly merge)", by=core.profile().get("slug", "agent") + "-dream")
            gone.add(victim["id"])
            out.append({"kept": keeper["id"], "folded": victim["id"]})
    return out


def schema_step(dry: bool = False) -> list:
    """The wiki schema grows from use (schema_growth.py, 2026-10-29): a type or tag the note writer was asked for twice, or
    once by an owner in so many words, becomes one line in Schema/schema.md (+ routing, folder, about.md). Zero-LLM, linted
    and rolled back on failure, ledgered like a rule (the morning note lists it, /undo takes it out). Never fatal."""
    try:
        import schema_growth
        res = schema_growth.grow(dry=dry, by=core.profile().get("slug", "agent") + "-dream")
    except Exception as e:  # noqa: BLE001
        print(f"schema growth skipped: {type(e).__name__}: {str(e)[:120]}")
        return []
    for r in res:
        print(f"{'schema_' + r['kind']:<13} {r['status']:<11} {r['name']}  {r['reason']}")
    return res


def _commit_state() -> None:
    rel = core.STATE.relative_to(core.VAULT).as_posix() if core.VAULT in core.STATE.parents else ""
    if rel:
        wiki_writer._git("add", "--", rel)
        wiki_writer._git("commit", "-q", "-m", "agent: nightly state (rules, memory, schedules)", "--", rel)


def _pass_note(before: int) -> str:
    """The changes a daytime pass just made, with their undo ids ('' when it changed nothing)."""
    new = [c for c in core.rows(core.CHANGES)[before:] if c.get("kind") != "undo"]
    if not new:
        return ""
    name = core.profile().get("name", "Agent")
    return "\n".join([f"{name} just learned from today's chats ({len(new)}). Reply /undo cN to reverse one."]
                     + [f"{c['id']}  {c['summary'][:150]}" for c in new])


def day_pass(run=None, min_rows: int = PASS_MIN_ROWS) -> int:
    """A daytime learning pass: the nightly review over what is new since the last one, so a
    lesson becomes a rule / fact / note / schedule within hours. No 24 h digest; it posts
    only what this pass changed, and nothing at all when it changed nothing."""
    packet = prepare()
    n = len([r for r in (packet or {}).get("rows", []) if r.get("who") != core.profile().get("name", "Agent")])
    if n < min_rows:
        print(f"DREAM pass skipped ({n} new lines from people, waiting for {min_rows})")
        return 0
    before = len(core.rows(core.CHANGES))
    nightly(run=run, digest_hours=0)
    text = _pass_note(before)
    if text:
        # Mission Control only (the owner 2026-10-07: mid-day 'I just learned...' posts read as robotic
        # status reports); the morning digest is the one note in chat, and /changes lists these
        try:
            subprocess.run(creationflags=core.NO_WINDOW, args=[sys.executable, str(core.AGENT.parent / "scripts" / "notify.py")], input=text, text=True,
                           encoding="utf-8", cwd=str(core.AGENT.parent / "scripts"), timeout=30, capture_output=True,
                           env={**os.environ, "CHAT_PUSH": "off"})
        except (OSError, subprocess.TimeoutExpired):
            pass
    return 0


def nightly(dry: bool = False, run=None, digest_hours: float = 24.0) -> int:
    packet = prepare()
    if packet:
        if run is None:
            import brain
            run = lambda p: brain.run_once(p, model=core.profile().get("dream_model", "opus"), timeout=600)  # noqa: E731
        body = "\n".join(f"{r['ref']} [{r.get('ts')}] ({r.get('convo')}) {r.get('who')}: {str(r.get('text', ''))[:1200]}"
                         for r in packet["rows"])
        r = run(INSTRUCTIONS + _waiting_block() + "\n\nTRANSCRIPT\n" + body)
        out = _json_from(r.get("text", "")) if r.get("ok") else None
        if out is None:
            print(f"DREAM no usable output ({r.get('error') or 'not JSON'}); nothing applied, watermark kept")
        else:
            res = apply(out, packet, dry=dry)
            for x in res:
                print(f"{x['op']:<13} {x['status']:<11} {x['what'][:100]}  {x['reason']}")
            if not dry:
                core.append(JOURNAL, {"ts": core.stamp(), "summary": str(out.get("summary") or "")[:600], "results": res})
                _to_daily_log(packet, str(out.get("summary") or "")[:600])
                core.save(STATE, {"rows_seen": packet["rows_end"], "last_run": core.stamp(),
                                  "last": {k: sum(1 for x in res if x["status"] == k)
                                           for k in ("applied", "appended", "folded", "pending", "refused")}})
    else:
        print("DREAM nothing new")
    try:                                                   # near-duplicate rules fold into one (never an owner's)
        for m in merge_rules(dry=dry):
            print(f"{'rule_merge':<13} {'would-fold' if dry else 'folded':<11} {m['folded']} -> {m['kept']}")
    except Exception as e:  # noqa: BLE001
        print(f"rule merge skipped: {type(e).__name__}: {str(e)[:120]}")
    schema_step(dry=dry)
    if not dry:
        if digest_hours:
            digest(digest_hours)
        _commit_state()
    return 0


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    ap = argparse.ArgumentParser()
    ap.add_argument("cmd", choices=["nightly", "pass", "digest", "status"])
    ap.add_argument("--min-rows", type=int, default=PASS_MIN_ROWS)
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--hours", type=float, default=24.0)
    ap.add_argument("--print", dest="print_only", action="store_true")
    a = ap.parse_args()
    if a.cmd == "nightly":
        sys.exit(nightly(a.dry_run))
    if a.cmd == "pass":
        sys.exit(day_pass(min_rows=a.min_rows))
    if a.cmd == "digest":
        sys.exit(digest(a.hours, a.print_only))
    print(json.dumps({"state": core.load(STATE, {}), "rules": len(core.rules_active()),
                      "memory": len(core.memory_active()), "schedules": len(routines.active())}, indent=1))

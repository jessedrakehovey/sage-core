"""core.py - the agent's own stores: profile, people, rules, memory, change ledger, transcript.

Ported from the property OS's shared chat-agent core (scripts/lib/chat_agent/rules.py +
dream.py memory half), rewritten as one stdlib module with no business specifics: every
name, person and protected topic that is particular to this household comes from
profile.json / people.json, so a new agent is a new folder with new data files.

  state/   (tracked in git, the rollback point)   rules.json  memory.json  routines.json
  data     (assistant/data/agent, git-ignored)     transcript.jsonl  changes.jsonl  sessions.json ...

Rails (same graduated loop as the dreaming skill):
  * PROTECTED CORE is never self-edited. A rule or fact about sending to people, approvals,
    paying or moving money, price/date commitments, credentials, deleting, footage or the
    tool lists is refused here and queued in Memory/skill-suggestions.md `## Pending`.
  * Every applied change gets a ledger row with an undo, and shows in the daily digest.
  * Small and capped: one sentence per rule, duplicates fold, the oldest least-seen retires.
Zero LLM. Never sends, never touches anything client-facing.
"""
from __future__ import annotations

import json
import os
import re
import sys
import time
from pathlib import Path

AGENT = Path(__file__).resolve().parent                  # assistant/agent
VAULT = AGENT.parents[1]
# A test that scopes the assistant's store (ASSISTANT_DATA) but not the agent's must never write
# into the live transcript: 2026-10-09 06:30 the dreaming self-test's fixture cards ("Sara prep" to
# sara@example.com, "Reply to Mom") landed in Sage's live conversation and it asked the owners about them.
DATA = Path(os.environ["AGENT_DATA"]) if os.environ.get("AGENT_DATA") else (
    Path(os.environ["ASSISTANT_DATA"]) / "agent" if os.environ.get("ASSISTANT_DATA") else AGENT.parent / "data" / "agent")
# A run whose store is not the live one (a test's scratch folder) keeps its state and its roster writes there too:
# 2026-10-29 test_whois wrote memory facts into the live state/ and test_chat_push pinned a fixture Chat id
# ("users/42") onto an owner in the live people.json.
try:
    SCOPED = DATA.resolve() != (AGENT.parent / "data" / "agent").resolve()
except OSError:
    SCOPED = True
STATE = Path(os.environ["AGENT_STATE"]) if os.environ.get("AGENT_STATE") else (DATA / "state" if SCOPED else AGENT / "state")
PEOPLE = Path(os.environ["AGENT_PEOPLE"]) if os.environ.get("AGENT_PEOPLE") else AGENT / "people.json"
SUGGESTIONS = Path(os.environ["AGENT_SUGGESTIONS"]) if os.environ.get("AGENT_SUGGESTIONS") \
    else VAULT / "Memory" / "skill-suggestions.md"

RULES = STATE / "rules.json"
MEMORY = STATE / "memory.json"
TRANSCRIPT = DATA / "transcript.jsonl"
CHANGES = DATA / "changes.jsonl"

# every child process is started windowless: the bridge runs detached, so a console child
# (claude.cmd, git, python) would otherwise pop a command window on the owner's screen (2026-10-05)
NO_WINDOW = 0x08000000 if os.name == "nt" else 0

# A ceiling that REFUSES, never a cap that retires (2026-10-21: the old cap of 60 silently retired the
# owners' own "keep it short", "not robotic", "reply in thread" and "say plainly whether it was done"
# rules to make room for dream-written ones). The nightly dream merges near-duplicates instead.
MAX_RULES = 150
# The memory cap DOES retire, but only a fact the nightly dream inferred (by "<slug>-dream"): a fact a
# person stated in chat, from a console or through the builder is never a cap victim (mem_cap_retirable;
# ported from the property OS 2026-10-30, where the cap had aged out eleven of the owner's own rules).
# When every active fact is a person's, the set runs over the cap with no victim.
MAX_MEMORY = 200
MEM_KINDS = ("person", "preference", "household", "business", "process")


# ------------------------------------------------------------------ io
def load(path, default):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        return default


def save(path, obj) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(obj, ensure_ascii=False, indent=1), encoding="utf-8")
    for i in range(6):
        try:
            os.replace(tmp, path)
            return
        except PermissionError:                           # Windows: indexer / AV holds the target
            time.sleep(0.25 * (i + 1))
    os.replace(tmp, path)


class _StateLock:
    """One cross-process lock for every read-change-save of state/ and the change ledger.
    The bridge, the nightly dream and chat.py are separate processes: without it two of them
    hand out the same change id, or one save drops a rule the other just wrote. Re-entrant
    within a process; a lock file older than STALE_S (a crashed holder) is broken."""
    STALE_S = 60

    def __init__(self):
        import threading
        self._rl = threading.RLock()
        self._depth = 0
        self._fd = None

    def __enter__(self):
        self._rl.acquire()
        self._depth += 1
        if self._depth > 1:
            return self
        lock = DATA / "state.lock"
        lock.parent.mkdir(parents=True, exist_ok=True)
        deadline = time.time() + 30
        while True:
            try:
                self._fd = os.open(str(lock), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
                os.write(self._fd, str(os.getpid()).encode())
                return self
            except (FileExistsError, PermissionError):     # Windows reports a pending delete as PermissionError
                try:
                    if time.time() - lock.stat().st_mtime > self.STALE_S:
                        lock.unlink()
                        continue
                except OSError:
                    pass
                if time.time() > deadline:                 # never deadlock the bridge: proceed unlocked, say so
                    self._fd = None
                    print(f"core: state lock busy for 30 s ({lock}); proceeding unlocked", file=__import__("sys").stderr)
                    return self
                time.sleep(0.05)

    def __exit__(self, *exc):
        self._depth -= 1
        try:
            if self._depth == 0 and self._fd is not None:
                os.close(self._fd)
                self._fd = None
                for i in range(5):
                    try:
                        (DATA / "state.lock").unlink()
                        break
                    except FileNotFoundError:
                        break
                    except PermissionError:
                        time.sleep(0.05 * (i + 1))
        finally:
            self._rl.release()
        return False


locked = _StateLock()


def append(path, row: dict) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps(row, ensure_ascii=False) + "\n")


def rows(path) -> list:
    out = []
    try:
        for ln in Path(path).read_text(encoding="utf-8").splitlines():
            try:
                out.append(json.loads(ln))
            except ValueError:
                continue
    except OSError:
        pass
    return out


def stamp() -> str:
    return time.strftime("%Y-%m-%d %H:%M")


def profile() -> dict:
    return load(AGENT / "profile.json", {})


def home_space() -> str:
    """The one Google Chat space the owners share with the agent (profile.json `home_space`,
    the owner 2026-10-07: 'all Sage communication in one space with owner and I'). Everything the
    agent says on its own (cards, notes, digests, routines, the doer's reports) goes there once,
    instead of to each owner's DM. '' = per-owner DMs. Tests clear it with AGENT_HOME_SPACE=''."""
    if "AGENT_HOME_SPACE" in os.environ:
        return os.environ["AGENT_HOME_SPACE"]
    return str(profile().get("home_space") or "")


def scope_of(convo: str, space: str = "") -> str:
    """'' for a DM or the owners' home space (full context); 'business' for any other shared space the app
    is in (2026-10-26, the owner: "I made a group chat with a team member that we are using for wedding business
    coordination. Please configure yourself to respond in that group chat called the organization."). A space
    an owner shares with team members never carries the owners' personal context, whoever is writing."""
    s = str(space or convo or "")
    if not s.startswith("spaces/"):
        return ""
    return "" if s == home_space() else "business"


def people() -> dict:
    return load(PEOPLE, {"roles": {}, "users": {}})


def is_owner(person: dict | None) -> bool:
    return bool(person) and str(person.get("role") or "") == "owner"


def owners() -> dict:
    """Only the owners (role owner): everything the agent says on its own, every reminder, every card goes to
    THEM. A staff member (role staff, 2026-10-24: a team member) is answered when they write and gets nothing else."""
    return {k: u for k, u in (people().get("users") or {}).items() if str(u.get("role") or "") == "owner"}


def role_of(person: dict | None) -> dict:
    return (people().get("roles") or {}).get(str((person or {}).get("role") or ""), {}) or {}


def person_lanes(person: dict | None):
    """None = every lane (an owner); a list = the only lanes this person may touch or hear about."""
    lanes = role_of(person).get("lanes")
    return list(lanes) if isinstance(lanes, list) and lanes else None


def person_paths(person: dict | None):
    """None = the whole vault (an owner); a list of path prefixes a staff member's searches are limited to."""
    paths = role_of(person).get("paths")
    return [str(x).replace("\\", "/") for x in paths] if isinstance(paths, list) and paths else None


def _owner_words() -> list:
    """The owners' keys and first names (people.json), lower-case, for the safety regexes; a scaffolded
    organization gets its own owners here, never this one's (2026-10-21)."""
    out = []
    try:
        for k, u in (people().get("users") or {}).items():
            for w in (k, str(u.get("name") or "").split(" ")[0]):
                w = re.sub(r"[^a-z]", "", str(w).lower())
                if w and w not in out:
                    out.append(w)
    except Exception:  # noqa: BLE001
        pass
    return out or ["owner"]


_OWNER_WORDS = _owner_words()


def first_owner() -> str:
    """The first owner's key (people.json), for a fallback DM when no conversation is on record."""
    try:
        return next(iter(owners() or people().get("users") or {}), "owner")
    except Exception:  # noqa: BLE001
        return "owner"


def person_for(handle: str = "", email: str = "", chat_user: str = "") -> dict | None:
    """The known person behind a channel identity, or None (strangers are never answered)."""
    p = people()
    for key, u in (p.get("users") or {}).items():
        if (handle and handle.lower() == key) or \
           (email and email.lower() in [e.lower() for e in u.get("emails", [])]) or \
           (chat_user and chat_user == u.get("chat_user")):
            if email and str(chat_user).startswith("users/") and u.get("chat_user") != chat_user:
                # pin the Chat id the first time this person writes, so gchat.mentionize can tag them
                u["chat_user"] = chat_user
                try:
                    if SCOPED and PEOPLE == AGENT / "people.json":
                        raise OSError("a scoped run never writes the live roster")
                    save(PEOPLE, p)
                except OSError:
                    pass
            return {"key": key, **u}
    return None


def can(person: dict | None, what: str) -> bool:
    if not person:
        return False
    return what in ((people().get("roles") or {}).get(person.get("role"), {}).get("can") or [])


# ------------------------------------------------------------------ protected core
_PROTECTED = re.compile(
    # reaching a person outside this household (how the agent words its OWN replies is fine)
    # ("in the same reply" / "in my reply" is the agent's own reply, a noun: not reaching anyone)
    r"(?<!same )(?<!the )(?<!my )(?<!your )(?<!each )"
    r"\b(?:send|sent|text|sms|email|e-mail|message|dm|reply|respond|post|publish|contact|call)\b[^.;]{0,50}"
    r"\b(?:client|couple|bride|groom|lead|vendor|planner|venue|guest|customer|anyone|people|them|him|her|"
    r"directly|yourself|on (?:my|our|his|her) behalf)\b|"
    r"\bapprov(?:e|al|als|ed)?\b|per-item|send.?gate|"
    r"without (?:" + "|".join(_OWNER_WORDS) + r"|asking|approval|a card|a tap|permission|checking)|"
    r"auto-?(?:send|approve|execute|book|pay|reply|publish|post)|skip (?:the )?(?:card|tap|approval)|"
    r"\b(?:pay|transfer|wire|venmo|zelle|withdraw|purchase|buy|order)\b[^.;]{0,40}"
    r"\b(?:bill|invoice|money|funds|rent|card|account|for (?:me|us))\b|"
    r"\b(?:quote|promise|commit|guarantee|offer|tell)\b[^.;]{0,50}"
    r"\b(?:price|pricing|discount|rate|turnaround|delivery date|deadline)\b|"
    r"\b(?:password|passcode|credential|api key|token|2fa|login|sign.?in)\b|"
    r"\b(?:delete|erase|wipe|format|overwrite)\b|\bfootage\b|original media|"
    r"allow(?:ed)?tools|disallowedtools|tool ?list|permission", re.I)

# memory is facts, never licence and never a secret
_MEM_REFUSE = re.compile(
    r"\b(?:can|may|allowed to|ok to|okay to|free to)\b[^.;]{0,40}"
    r"\b(?:send|text|email|post|message|pay|book|approve|delete|publish|reply)\b|"
    r"\bwithout (?:approval|asking|a card|a tap)\b|\bno need to (?:ask|card|confirm)\b|"
    r"auto-?(?:send|approve|execute|book|pay)|skip (?:the )?(?:card|tap|approval)|"
    r"\b(?:password|passcode|pin|ssn|social security|routing number|cvv|api key|token)\b|"
    r"\b\d{9,}\b", re.I)


def protected_pattern() -> re.Pattern:
    extra = str(profile().get("extra_protected") or "")
    return re.compile(_PROTECTED.pattern + ("|" + extra if extra else ""), re.I) if extra else _PROTECTED


def is_protected(text: str) -> bool:
    return bool(protected_pattern().search(str(text or "")))


def _norm(s: str) -> str:
    return re.sub(r"[^a-z0-9 ]+", " ", str(s or "").lower()).strip()


def similar(a: str, b: str) -> bool:
    wa, wb = set(_norm(a).split()), set(_norm(b).split())
    if not wa or not wb:
        return False
    return len(wa & wb) / max(1, min(len(wa), len(wb))) >= 0.7


PENDING_LATCH = DATA / "pending_latch.json"     # proposals an owner already answered (pending.py)


def _proposal_of(lines) -> str:
    for ln in lines:
        m = re.match(r"\s*\*(?:Proposal|Proposed \w+|Rule):\*\s*(.*)", str(ln))
        if m:
            return m.group(1).strip()
    return ""


def pending_duplicate(key: str, body_lines: list) -> str:
    """The key of an open ## Pending entry (or an owner's earlier answer) that is this same proposal,
    else ''. 2026-10-18: the nightly review re-filed the same few asks every night (90 entries)."""
    try:
        import pending
        kind = "retire" if "-retire-" in key else "rule" if "-rule-protected-" in key else "ask"
        prop = _proposal_of(body_lines)
        if not prop:
            return ""
        me = {"key": key, "kind": kind, "text": prop, "title": (re.search(r"retire-(r\d+)", key) or [None, ""])[1] if kind == "retire" else prop[:80]}
        for e in pending.entries():
            if pending.same(me, e):
                return e["key"]
        for d in load(PENDING_LATCH, []):
            if kind == "retire" and d.get("kind") == "retire":
                if me["title"] and me["title"] == (re.search(r"retire-(r\d+)", str(d.get("key"))) or [None, ""])[1]:
                    return str(d.get("key"))
            elif pending.similar_text(prop, d.get("text", "")):
                return str(d.get("key"))
    except Exception:  # noqa: BLE001  a broken check never loses a proposal
        return ""
    return ""


def log_suggestion(section: str, key: str, body_lines: list) -> None:
    """Append one entry under `section` (## Pending or ## Applied automatically). A Pending entry that
    is already waiting (or was already answered) is not filed twice."""
    if section == "## Pending" and pending_duplicate(key, body_lines):
        return
    try:
        text = SUGGESTIONS.read_text(encoding="utf-8") if SUGGESTIONS.exists() else ""
        entry = "\n- key: " + key + "\n" + "\n".join("  " + ln for ln in body_lines) + "\n"
        m = re.search(r"^" + re.escape(section) + r"[ \t]*$", text, re.M)   # line-start heading only
        if m:
            i = m.end()
            text = text[:i] + "\n" + entry.rstrip("\n") + "\n" + text[i:].lstrip("\n")
        else:
            text = text.rstrip("\n") + "\n\n" + section + entry
        SUGGESTIONS.parent.mkdir(parents=True, exist_ok=True)
        SUGGESTIONS.write_text(text, encoding="utf-8")
    except OSError:
        pass


def pend(what: str, text: str, why: str, by: str) -> None:
    """A change only an owner may make by hand: queue it, never apply it."""
    name = profile().get("name", "agent")
    log_suggestion("## Pending", f"{profile().get('slug', 'agent')}-{what}-protected-" + time.strftime("%Y%m%d-%H%M%S"), [
        f"**PROTECTED CORE - PROPOSAL ONLY ({name}, not applied).**",
        f"*Proposed {what}:* {text}",
        f"*From {by} ({stamp()}):* {why or '(none given)'}",
        "*Why gated:* touches sending, approvals, money, price/date commitments, credentials, deleting, "
        "footage or tool lists."])


# ------------------------------------------------------------------ change ledger
def log_change(kind: str, summary: str, undo: dict, by: str) -> str:
    with locked:                                           # count + append as one step: ids never collide
        cid = "c" + str(len(rows(CHANGES)) + 1)
        append(CHANGES, {"id": cid, "ts": stamp(), "kind": kind, "summary": summary[:300], "undo": undo, "by": by})
    return cid


def changes_since(hours: float = 24.0) -> list:
    cut = time.time() - hours * 3600
    out = []
    for r in rows(CHANGES):
        try:
            if time.mktime(time.strptime(r["ts"], "%Y-%m-%d %H:%M")) >= cut:
                out.append(r)
        except (KeyError, ValueError):
            continue
    return out


def undo(cid: str, by: str = "") -> str:
    """Reverse one ledger row. Returns a human line."""
    with locked:                                           # two /undo of one row must not both run
        return _undo(cid, by)


def _undo(cid: str, by: str) -> str:
    row = next((r for r in rows(CHANGES) if r.get("id") == cid), None)
    if not row:
        return f"No change {cid}."
    if any(r.get("kind") == "undo" and (r.get("undo") or {}).get("of") == cid for r in rows(CHANGES)):
        return f"{cid} was already undone."
    u = row.get("undo") or {}
    op, ok = u.get("op"), False
    if op == "rule_retire":
        ok = rule_retire(u.get("id", ""), f"undo {cid}", by=by, ledger=False)
    elif op == "rule_restore":
        ok = _rule_set_active(u.get("id", ""), True)
    elif op == "mem_forget":
        ok = mem_forget(u.get("id", ""), f"undo {cid}", by=by, ledger=False)
    elif op == "mem_restore":
        ok = _mem_set(u.get("id", ""), active=True)
    elif op == "mem_reword":
        ok = _mem_set(u.get("id", ""), fact=u.get("fact", ""))
    elif op == "routine_off":
        import routines
        ok = routines.disable(u.get("id", ""), by=by, ledger=False)
    elif op == "routine_on":
        import routines
        ok = routines.enable(u.get("id", ""))
    elif op == "event_remove":
        import google_api
        ok = google_api.remove_event(u.get("account", ""), u.get("id", ""))
    elif op == "followup_cancel":
        import followups
        ok = followups.cancel(u.get("id", ""))
    elif op == "note_remove":
        import wiki_writer
        ok = wiki_writer.remove(u.get("path", ""), u.get("marker", ""))
    elif op == "schema_remove":                                 # a type / tag / routing line the schema grew (schema_growth.py)
        import schema_growth
        ok = schema_growth.remove(u)
    elif op == "org_restore":
        import org
        ok = org.restore(u.get("before"))
    elif op == "reflex_unmute":                                 # a message class switched off by an owner's rule (reflex.py)
        import reflex
        ok = reflex.unmute(u.get("cls", ""))
    elif op == "project_restore":                               # a long-running problem's record as it was (projects.py)
        import projects
        ok = projects.restore(u.get("id", ""), u.get("before"))
    elif op == "alias_forget":                                  # the `identify` deed (whois.py)
        sys.path.insert(0, str(VAULT / "assistant" / "scripts"))
        import whois
        before = u.get("before")
        ok = whois.learn(u.get("handle", ""), before.get("name", ""), before.get("relation", ""), before.get("kind", ""),
                         by=before.get("by", ""), evidence=before.get("evidence", "")).get("ok") if isinstance(before, dict) and before.get("name") \
            else whois.unlearn(u.get("handle", ""))
    if not ok:
        return f"Could not undo {cid} ({row.get('summary', '')[:80]})."
    append(CHANGES, {"id": "c" + str(len(rows(CHANGES)) + 1), "ts": stamp(), "kind": "undo",
                     "summary": f"undid {cid}: {row.get('summary', '')[:200]}", "undo": {"of": cid}, "by": by})
    return f"Undone {cid}: {row.get('summary', '')[:120]}"


# ------------------------------------------------------------------ rules
def _rules() -> dict:
    return load(RULES, {"next_id": 1, "rules": []})


_TIGHTENS = re.compile(r"\b(?:never|don'?t|do not|stop|no longer|not|only|unless|without my say|before (?:asking|checking))\b", re.I)
_LOOSENS = re.compile(r"without (?:" + "|".join(_OWNER_WORDS) + r"|asking|approval|a card|a tap|permission|checking|waiting)|skip (?:the )?(?:card|tap|approval)|"
                      r"auto-?(?:send|approve|execute|book|pay|reply|publish|post)|no need to (?:ask|card|confirm|wait)|"
                      r"\b(?:don'?t|do not|never|stop) (?:ask|wait|check|card)\w*\b[^.;]{0,30}\b(?:before|first|approval|permission|send|sending|pay|post)|"
                      r"\b(?:may|can|allowed to|free to|go ahead and)\b[^.;]{0,30}\b(?:send|pay|delete|post|publish|approve|sign)\b|"
                      r"\b(?:password|passcode|credential|api key|token|2fa)\b|\b(?:delete|erase|wipe|format|overwrite)\b[^.;]{0,30}\bfootage\b|"
                      r"allow(?:ed)?tools|disallowedtools|tool ?list", re.I)


def tightens(rule: str) -> bool:
    """A rule that narrows what the agent does ("never draft personal texts", "no nudge to a lead
    who booked a meeting", "no build without my approval") is safe to apply at once even when it
    mentions sending or approving. 2026-10-06..11: four such rules from the owner were queued as
    "protected core" and he saw "Not learned" / "One thing I can't change myself" each time. A rule
    that LOOSENS a gate (send without a card, skip approval, credentials) stays protected."""
    r = str(rule or "")
    return bool(_TIGHTENS.search(r)) and not _LOOSENS.search(r)


def rule_add(rule: str, why: str = "", by: str = "") -> dict:
    """-> {"status": applied|folded|pending|refused, ...}"""
    rule = re.sub(r"\s+", " ", str(rule or "")).strip().rstrip(".")[:280]
    if len(rule) < 12:
        return {"status": "refused", "reason": "rule too short"}
    if (is_protected(rule) and not tightens(rule)) or _LOOSENS.search(rule):
        pend("rule", rule, why, by)
        return {"status": "pending", "rule": rule, "reason": "protected core, queued for an owner"}
    with locked:
        res = _rule_add(rule, why, by)
    if res.get("status") in ("applied", "folded"):
        # the reflex (2026-10-27): a rule that switches off a class of code-posted lines ("no more 'still not
        # built' reminders") mutes that code the same turn; a rule alone only reaches the model
        try:
            import reflex
            muted = reflex.bind_rule(rule, rid=str(res.get("id") or ""), by=by)
            if muted:
                res["muted"] = muted
        except Exception:  # noqa: BLE001
            pass
    return res


def _rule_add(rule: str, why: str, by: str) -> dict:
    d = _rules()
    now = stamp()
    for r in d["rules"]:
        if r.get("active", True) and similar(r["rule"], rule):
            r["seen"] = int(r.get("seen", 1)) + 1
            r["last"] = now
            save(RULES, d)
            return {"status": "folded", "id": r["id"], "rule": r["rule"]}
    if sum(1 for r in d["rules"] if r.get("active", True)) >= MAX_RULES:
        return {"status": "refused", "reason": f"{MAX_RULES} rules are active; merge or retire one first (/rules)"}
    rid = f"r{d['next_id']}"
    d["next_id"] = int(d["next_id"]) + 1
    d["rules"].append({"id": rid, "rule": rule, "why": str(why or "")[:600], "by": by,
                       "added": now, "last": now, "seen": 1, "active": True})
    save(RULES, d)
    cid = log_change("rule", f"rule {rid}: {rule}", {"op": "rule_retire", "id": rid}, by)
    return {"status": "applied", "id": rid, "rule": rule, "change": cid}


def _rule_set_active(rid: str, on: bool, why: str = "") -> bool:
    with locked:
        d = _rules()
        for r in d["rules"]:
            if r["id"] == rid and bool(r.get("active", True)) != on:
                r["active"] = on
                if not on:
                    r["retired"] = stamp() + (" " + why if why else "")
                else:
                    r.pop("retired", None)
                save(RULES, d)
                return True
    return False


def rule_retire(rid: str, why: str = "", by: str = "", ledger: bool = True) -> bool:
    cur = next((r for r in _rules()["rules"] if r["id"] == rid), None)
    if not _rule_set_active(rid, False, why):
        return False
    if ledger:
        log_change("rule_retire", f"retired {rid}: {cur['rule']}", {"op": "rule_restore", "id": rid}, by)
    return True


def rules_active() -> list:
    return [r for r in _rules()["rules"] if r.get("active", True)]


def render_rules() -> str:
    rs = rules_active()
    if not rs:
        return ""
    lines = ["LEARNED RULES (from the owners' feedback; they override your defaults, never the protected core):"]
    lines += [f"- [{r['id']}] {r['rule']}  (from {r.get('by') or '?'}, {r['added'][:10]})" for r in rs]
    return "\n".join(lines)


# ------------------------------------------------------------------ memory
def _mem() -> dict:
    return load(MEMORY, {"next_id": 1, "facts": []})


def memory_active() -> list:
    now = stamp()
    return [f for f in _mem()["facts"] if f.get("active", True) and not (f.get("expires") and f["expires"] < now)]


def mem_upsert(kind: str, subject: str, fact: str, evidence: str = "", by: str = "", expires: str = "") -> dict:
    kind = kind if kind in MEM_KINDS else "process"
    subject = re.sub(r"\s+", " ", str(subject or "")).strip()[:60]
    fact = re.sub(r"\s+", " ", str(fact or "")).strip().rstrip(".")[:300]
    if len(fact) < 8:
        return {"status": "refused", "reason": "too short"}
    if _MEM_REFUSE.search(fact + " " + subject):
        return {"status": "refused", "reason": "reads as a permission or a secret", "fact": fact}
    with locked:
        return _mem_upsert(kind, subject, fact, evidence, by, expires)


def mem_cap_retirable(f: dict) -> bool:
    """Only a fact the nightly dream INFERRED (by "<slug>-dream", dream.py) may be retired by the MAX_MEMORY
    cap. Everything else came from a person's words in the moment -- an owner or staff member in chat (by =
    their people.json key), a console (by "owner1-cli" and the like), the builder -- and only a person forgets
    those (mem_forget). Property OS decision study 2026-10-30: its cap had retired 11 of the owner's own rules."""
    return str(f.get("by") or "").endswith("-dream")


def _mem_upsert(kind, subject, fact, evidence, by, expires) -> dict:
    d = _mem()
    now = stamp()
    for f in d["facts"]:
        if f.get("active", True) and f.get("subject", "").lower() == subject.lower() and similar(f["fact"], fact):
            old = f["fact"]
            f.update({"seen": int(f.get("seen", 1)) + 1, "last": now, "fact": fact})   # newer wording wins
            save(MEMORY, d)
            if _norm(old) != _norm(fact):                  # a rewording replaces text: ledgered, undoable
                log_change("memory_reword", f"reworded {f['id']} ({subject}): {fact}",
                           {"op": "mem_reword", "id": f["id"], "fact": old}, by)
            return {"status": "folded", "id": f["id"], "fact": fact}
    mid = f"m{d['next_id']}"
    d["next_id"] = int(d["next_id"]) + 1
    d["facts"].append({"id": mid, "kind": kind, "subject": subject, "fact": fact, "evidence": str(evidence)[:300],
                       "by": by, "added": now, "last": now, "seen": 1, "active": True,
                       **({"expires": expires} if expires else {})})
    act = [f for f in d["facts"] if f.get("active", True)]
    victim = None
    pool = [f for f in act if f["id"] != mid and mem_cap_retirable(f)]   # never the fact just added
    if len(act) > MAX_MEMORY and pool:
        # retire the least-seen, oldest DREAM-INFERRED fact to keep the prompt small; a person's fact is
        # never a cap victim (mem_cap_retirable), so when only those remain the set runs over the cap
        victim = sorted(pool, key=lambda f: (int(f.get("seen", 1)), f.get("last", "")))[0]
        victim["active"] = False
        victim["retired"] = now + " (cap)"
    save(MEMORY, d)
    cid = log_change("memory", f"remembered {mid} ({subject}): {fact}", {"op": "mem_forget", "id": mid}, by)
    if victim:
        log_change("memory_forget", f"retired {victim['id']} (memory cap {MAX_MEMORY}): {victim['fact']}",
                   {"op": "mem_restore", "id": victim["id"]}, by)
    return {"status": "applied", "id": mid, "fact": fact, "change": cid}


def _mem_set(mid: str, active: bool | None = None, fact: str = "") -> bool:
    """Undo helper: re-activate a fact, or put back its earlier wording."""
    with locked:
        d = _mem()
        for f in d["facts"]:
            if f["id"] != mid:
                continue
            if active is not None:
                if bool(f.get("active", True)) == active:
                    return False
                f["active"] = active
                f.pop("retired", None)
            if fact:
                f["fact"] = fact
            save(MEMORY, d)
            return True
    return False


def mem_forget(mid: str, why: str = "", by: str = "", ledger: bool = True) -> bool:
    with locked:
        d = _mem()
        for f in d["facts"]:
            if f["id"] == mid and f.get("active", True):
                f["active"] = False
                f["retired"] = stamp() + (" " + why if why else "")
                save(MEMORY, d)
                if ledger:
                    log_change("memory_forget", f"forgot {mid}: {f['fact']}", {"op": "mem_restore", "id": mid}, by)
                return True
    return False


def mem_restore(mid: str, why: str = "", by: str = "", force: bool = False) -> dict:
    """Re-activate a retired fact (a cap victim, a dream retirement an owner disagrees with). Refuses when the
    fact is already active, or when an active fact on the same subject already says the same thing (unless
    force). Ledgered with an undo (mem_forget). -> {"status": restored|refused|similar, ...}"""
    with locked:
        d = _mem()
        for f in d["facts"]:
            if f["id"] != mid:
                continue
            if f.get("active", True):
                return {"status": "refused", "reason": "already active", "id": mid}
            if not force:
                for o in d["facts"]:
                    if o is not f and o.get("active", True) and o.get("subject", "").lower() == f.get("subject", "").lower()                             and similar(o["fact"], f["fact"]):
                        return {"status": "similar", "id": mid, "similar_to": o["id"], "fact": o["fact"]}
            f["active"] = True
            f.pop("retired", None)
            f["restored"] = stamp() + (" " + why if why else "")
            f["last"] = stamp()
            save(MEMORY, d)
            cid = log_change("memory_restore", f"restored {mid} ({f.get('subject', '')}): {f['fact']}",
                             {"op": "mem_forget", "id": mid}, by)
            return {"status": "restored", "id": mid, "fact": f["fact"], "change": cid}
    return {"status": "refused", "reason": "no such fact", "id": mid}


def render_memory() -> str:
    fs = memory_active()
    if not fs:
        return ""
    order = {k: i for i, k in enumerate(MEM_KINDS)}
    fs.sort(key=lambda f: (order.get(f.get("kind"), 9), f.get("subject", "")))
    lines = ["LONG-TERM MEMORY (what you have learned about this household; the wiki and live records win "
             "when they disagree):"]
    lines += [f"- [{f['id']}] {f.get('subject', '')}: {f['fact']}" for f in fs]
    return "\n".join(lines)


# ------------------------------------------------------------------ transcript
def say(convo: str, who: str, text: str, channel: str = "", meta: dict | None = None) -> None:
    append(TRANSCRIPT, {"ts": stamp(), "convo": convo, "who": who, "text": str(text)[:6000],
                        "channel": channel, **(meta or {})})


def transcript_tail(convo: str, n: int = 12) -> list:
    return [r for r in rows(TRANSCRIPT) if r.get("convo") == convo][-n:]


THREAD_ROOTS = DATA / "thread_roots.json"


def note_thread_root(thread: str, who: str, text: str, channel: str = "", convo: str = "") -> None:
    """Remember what started a Chat thread. Messages code posts (doer reports, builder notes,
    digests, the agent's own replies) start threads an owner may reply in; 2026-10-11 the owner's
    'I don't need this anymore' in such a thread met a guess because nothing on record said what
    the thread was. Bounded to the last 400 roots."""
    if not thread:
        return
    with locked:
        d = load(THREAD_ROOTS, {})
        known = str((d.get(thread) or {}).get("text") or "")
        if not text and known:
            return                                                # never blank a root that is known
        if text and known and known != str(text):
            return                                                # the first message IS the root; a later line in the thread never replaces it (2026-10-29)
        d[thread] = {"ts": stamp(), "who": str(who or "")[:60], "text": str(text or "")[:1500], "channel": channel, "convo": convo}
        if len(d) > 400:
            for k in sorted(d, key=lambda k: d[k].get("ts", ""))[:len(d) - 400]:
                d.pop(k, None)
        save(THREAD_ROOTS, d)


def thread_root(thread: str) -> dict:
    return (load(THREAD_ROOTS, {}) or {}).get(thread) or {}


def transcript_thread(convo: str, thread: str, n: int = 10) -> list:
    """The lines of one Chat thread (a card's thread, or a thread an owner started), oldest first.
    When the transcript has nothing for the thread, the remembered root message is the one line."""
    if not thread:
        return []
    out = [r for r in rows(TRANSCRIPT) if r.get("convo") == convo and r.get("thread") == thread][-n:]
    root = thread_root(thread)
    if root and root.get("text") and not any(r.get("text") == root.get("text") for r in out[:1]):
        out = [{"ts": root.get("ts", ""), "convo": convo, "who": root.get("who", ""), "text": root.get("text", ""),
                "channel": root.get("channel", ""), "thread": thread, "root": True}] + out[-(n - 1):]
    return out

"""brain.py - one read-only model turn for the agent, and the deeds it records on the way.

The model never writes and never sends. It runs headless with read tools only, from a
scratch working directory (so the project's hooks and permission allow-list do not apply),
and resumes one session per conversation per day so a chat keeps its thread. Everything it
wants changed it puts forward through the deed tools of its private MCP server (gmcp.py ->
intents.py: validated at once, applied by the bridge after the turn). A trailing
`SUGGEST {json}` line is still read as the fallback (2026-10-21).

Context (persona + rules + memory + schedules) is sent on the first turn of a session and
again only when it changes, so a rule learned in one message is in force on the next.

  python assistant/agent/brain.py context          # print the context block as the model sees it
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import account  # noqa: E402
import core  # noqa: E402
import held  # noqa: E402
import intents  # noqa: E402

CLAUDE = os.path.expandvars(r"%APPDATA%\npm\claude.cmd")
# check_gates.py parses these two shapes to freeze/verify the allow/deny lists.
ALLOWED = ",".join(["Read", "Glob", "Grep",
                    "mcp__example-google__calendar_events", "mcp__example-google__mail_search",
                    "mcp__example-google__mail_read", "mcp__example-google__vault_search", "mcp__example-google__texts_search",
                    "mcp__example-google__who_is",       # who a number is, with the evidence (whois.py, 2026-10-23)
                    # the deeds (intents.py, 2026-10-21): each validates and RECORDS an intent for this
                    # turn; the bridge applies it afterwards. None of them sends or writes by itself.
                    "mcp__example-google__task", "mcp__example-google__do", "mcp__example-google__call",
                    "mcp__example-google__card_revise", "mcp__example-google__remember", "mcp__example-google__forget",
                    "mcp__example-google__learn", "mcp__example-google__unlearn", "mcp__example-google__note",
                    "mcp__example-google__skill", "mcp__example-google__org", "mcp__example-google__schedule",
                    "mcp__example-google__unschedule", "mcp__example-google__followup", "mcp__example-google__event",
                    "mcp__example-google__event_change", "mcp__example-google__event_remove", "mcp__example-google__addressed",
                    "mcp__example-google__switch",    # an owner turns a standing job on/off (switches.py, 2026-10-23)
                    "mcp__example-google__job",       # an owner runs a registered local job now or at a time (jobs.py, 2026-10-24)
                    "mcp__example-google__blocked",   # the agent names its own blocker; code builds or portals (capability.py, 2026-10-25)
                    "mcp__example-google__identify",    # an owner says who a number is (whois.learn, 2026-10-23)
                    "mcp__example-google__approve",     # an owner's "send it" / "send it at 8am" in a card's thread (2026-10-24; missing from this list until 10-26)
                    "mcp__example-google__decide",      # a decision card with the recommendation first (2026-10-23; missing from this list until 10-26)
                    "mcp__example-google__project"])    # a long-running problem worked on a schedule with every try on record (projects.py, 2026-10-27)
DENIED = ",".join(["Bash", "PowerShell", "Edit", "Write", "NotebookEdit", "WebFetch", "WebSearch", "Task", "Agent",
                   "mcp__gmail__send_email", "mcp__gmail__delete_email",
                   "mcp__example-mail-personal__send_email", "mcp__example-mail-films__send_email",
                   "mcp__playwright__browser_click", "mcp__playwright-cli__browser_click"])
SESSIONS = core.DATA / "sessions.json"
# The private server's name is whatever the gate's allow-list names its tools (check_gates.py freezes that list;
# gmcp.py serves under the same name): never a second literal to drift from it.
MCP_SERVER = next((t.split("__")[1] for t in ALLOWED.split(",") if t.startswith("mcp__") and t.endswith("__vault_search")), "sage-google")
ALLOWED += f",mcp__{MCP_SERVER}__chat_thread"      # the thread / the space's newest lines, from Google Chat itself (chat_thread.py, 2026-10-29)


def _alib():
    sys.path.insert(0, str(core.AGENT.parent / "scripts"))
    import alib
    return alib


def _business_lane() -> str:
    try:
        return _alib().business_lane()
    except Exception:  # noqa: BLE001
        return "business"


def _apply_crm_tools(allowed: str, denied: str) -> tuple[str, str]:
    """Inject the CRM connector's read/write tool names at runtime so the engine lists no CRM."""
    try:
        a = _alib()
        read_tools = a.plugin_set("MCP_READ_TOOLS")
        write_tools = a.plugin_set("MCP_WRITE_TOOLS")
        if read_tools:
            allowed = allowed + "," + ",".join(sorted(read_tools))
        if write_tools:
            denied = denied + "," + ",".join(sorted(write_tools))
    except Exception:  # noqa: BLE001
        pass
    return allowed, denied


# A staff member's turn (role staff, 2026-10-24): the business connector read-only, the wiki search (scoped by
# gmcp.py to the role's paths), and the two deeds the role can use. No Read/Glob/Grep over the vault, no mail,
# calendar, texts or who_is, no learning tools: nothing that could carry an owner's personal life.
STAFF_ALLOWED = ",".join([f"mcp__{MCP_SERVER}__vault_search", f"mcp__{MCP_SERVER}__task", f"mcp__{MCP_SERVER}__note",
                          f"mcp__{MCP_SERVER}__chat_thread"])   # the team-space thread the turn is in, nothing else (chat_thread.py, 2026-10-29)
STAFF_DENIED = DENIED + ",Read,Glob,Grep,mcp__example-google__calendar_events,mcp__example-google__mail_search,mcp__example-google__mail_read," \
               "mcp__example-google__texts_search,mcp__example-google__who_is"
STAFF_MEMORY_KINDS = ("business",)
# An OWNER's turn in a team space (scope business): the staff read tools plus every deed tool; still no Read/Glob/
# Grep over the vault, no mail, calendar, texts or who_is, because team members read the replies.
BUSINESS_ALLOWED = ",".join(["mcp__example-google__vault_search"] +
                            [t for t in ALLOWED.split(",") if t.startswith("mcp__example-google__") and t not in
                             ("mcp__example-google__calendar_events", "mcp__example-google__mail_search", "mcp__example-google__mail_read",
                              "mcp__example-google__vault_search", "mcp__example-google__texts_search", "mcp__example-google__who_is")])


def _jobs_render() -> str:
    """JOBS YOU CAN RUN (scripts/jobs.py): the registered local jobs the `job` tool may card."""
    try:
        sys.path.insert(0, str(core.AGENT.parent / "scripts"))
        import jobs
        return jobs.render()
    except Exception:  # noqa: BLE001
        return ""
ASKS = core.DATA / "asks.json"
WORKDIR = core.DATA / "ask"
MCP_CONFIG = core.DATA / "mcp.json"
RECENT_N = 20            # cards and the doer's reports now sit in the conversation too (2026-10-07)


def staff_context(person: dict, scope: str = "", space_title: str = "") -> str:
    """What a staff member's turn may know (2026-10-24, the owner: a team member does wedding business as an admin, not an owner,
    and has zero access to the owners' personal life): the staff persona, the organization, business facts
    only, the lane's skills and the wiki map limited to the role's paths. No rules, routines, jobs, texters,
    connections, library or builder state."""
    import org
    import skills
    p = core.profile()
    owner_here = core.is_owner(person)
    staff_role = (core.people().get("roles") or {}).get("staff") or {}
    lanes = (core.person_lanes(person) if not owner_here else None) or list(staff_role.get("lanes") or []) or [_business_lane()]
    paths = (core.person_paths(person) if not owner_here else None) or [str(x) for x in (staff_role.get("paths") or [])]
    tpl = core.AGENT / "templates" / ("prompt.team.md" if owner_here else "prompt.staff.md")
    fmt = dict(name=p.get("name", "the agent"), org=str((org.load() or {}).get("name") or p.get("household", "the business")),
               member=str(person.get("name") or "a team member"), owners=" and ".join(u["name"] for u in core.owners().values()) or "the owners",
               lanes=", ".join(lanes) or "the business", paths=", ".join(paths) or "the business notes", space=space_title or "a shared team space")
    persona = tpl.read_text(encoding="utf-8").format(**fmt) if tpl.exists() else \
        f"You are {p.get('name', 'the agent')}. You are talking with {person.get('name')} about {', '.join(lanes) or 'the business'} only."
    facts = [f for f in core.memory_active() if f.get("kind") in STAFF_MEMORY_KINDS]
    mem = ""
    if facts:
        mem = "BUSINESS FACTS (what you have learned about the business; the records win when they disagree):\n" + \
              "\n".join(f"- {f.get('subject', '')}: {f['fact']}" for f in sorted(facts, key=lambda f: f.get("subject", "")))
    sk = ""
    try:
        rows = [s for s in skills.all_skills() if str((s.get("meta") or {}).get("lane") or "") in lanes]
        if rows:
            sk = "SKILLS FOR THIS WORK:\n" + "\n".join(f"- {s['title']}: triggers {', '.join((s['meta'].get('triggers') or [])[:6]) or '-'}" for s in rows)
    except Exception:  # noqa: BLE001
        pass
    wmap = wiki_map()
    if wmap and paths:
        secs = {Path(x).name.lower() for x in (pp.rstrip("/") for pp in paths)}
        keep = [ln for ln in wmap.splitlines()[1:] if ln[2:].split(" (")[0].strip().lower() in secs]
        wmap = ("WIKI MAP (the business notes you may search with vault_search):\n" + "\n".join(keep)) if keep else ""
    return "\n\n".join(x for x in [persona, org.render(), mem, sk, wmap] if x)


def sent_block(hours: float = 30.0) -> str:
    """SENT RECENTLY: every outbound the executor completed in the last `hours` (texts handed to the phone, emails,
    CRM messages and files), zero-LLM from the exec tickets, so a turn in one thread knows what went out
    from another (2026-10-24: Sage offered Corwin an example handoff four hours after one had been emailed)."""
    sys.path.insert(0, str(core.AGENT.parent / "scripts"))
    try:
        import exec_store as es
        import alib
        import crm as _crm_m
        sent_kinds = set(es.OUTBOUND_KINDS) | alib.plugin_set("SENT_KINDS")     # the organization's own kinds report too
        kind_words = {"imessage_send": "text", "email_send": "email", **alib.plugin_map("KIND_WORDS")}
        rows = []
        for t in es.fold().values():
            if t.get("status") != "done" or t.get("kind") not in sent_kinds:
                continue
            try:
                done = time.mktime(time.strptime(str(t.get("ts_done") or "")[:19], "%Y-%m-%dT%H:%M:%S"))
            except (TypeError, ValueError):
                continue
            if time.time() - done > hours * 3600:
                continue
            p = t.get("payload") or {}
            what = str(p.get("subject") or p.get("body") or p.get("step_match") or "")
            what = re.sub(r"\s+", " ", what).strip()[:90]
            to = str(p.get("to") or _crm_m.thread_id(p) or "")
            kind = kind_words.get(t["kind"], t["kind"])
            rows.append((done, f"- {time.strftime('%a %I:%M %p', time.localtime(done)).replace(' 0', ' ')}: {kind} to {to}: \"{what}\""))
        rows.sort()
        if not rows:
            return ""
        return "SENT RECENTLY (what already went out from here, every thread; never offer or send these again unless asked):\n" + "\n".join(r[1] for r in rows[-25:])
    except Exception:  # noqa: BLE001
        return ""


def context_block(person: dict | None = None, scope: str = "", space_title: str = "") -> str:
    if person and (not core.is_owner(person) or scope == "business"):
        return staff_context(person, scope, space_title)
    p = core.profile()
    names = " and ".join(u["name"] for u in (core.owners() or core.people().get("users") or {}).values())
    persona = (core.AGENT / "prompt.md").read_text(encoding="utf-8").format(
        name=p.get("name", "the agent"), household=p.get("household", ""), people=names,
        vault=str(core.VAULT).replace("\\", "/"))
    # prompt.md is the generic template (works for any organization); prompt.local.md holds THIS
    # organization's own records, lanes and habits (2026-10-21). A scaffolded org starts with none.
    local = core.AGENT / "prompt.local.md"
    if local.exists():
        txt = local.read_text(encoding="utf-8").strip()
        if txt:
            persona += "\n\n" + txt
    import org
    import resources
    import routines
    import skills
    import builder
    try:
        import reflex
        reflex_block = reflex.render()
    except Exception:  # noqa: BLE001
        reflex_block = ""
    try:                      # the long-running problems on the board (projects.py, 2026-10-27): never "I don't know where that stands"
        import projects
        projects_block = projects.render()
    except Exception:  # noqa: BLE001
        projects_block = ""
    parts = [persona, org.render(), core.render_rules(), core.render_memory(), routines.render(), _jobs_render(), skills.render(),
             wiki_map(), resources.render(), texters_block(), sent_block(), connections_block(), builder.render(), reflex_block, projects_block]
    return "\n\n".join(x for x in parts if x)


def texters_block() -> str:
    """PEOPLE WHO TEXT (whois.directory, 2026-10-23): every human who texted the owners' phones lately,
    best name with how sure and why, group texts marked, automated senders folded. the owner: 'Sage has a
    difficult time sorting through and deducing who people are from inbound text messages.'"""
    sys.path.insert(0, str(core.VAULT / "assistant" / "scripts"))
    try:
        import whois
        return whois.directory(days=30)
    except Exception as e:  # noqa: BLE001
        return f"PEOPLE WHO TEXT: not readable right now ({type(e).__name__}); use who_is and texts_search."


def connections_block() -> str:
    """What is connected and what is not (scripts/setup_actions.py), so the agent names the
    Setup page instead of asking for a password or saying it cannot reach something."""
    try:
        sys.path.insert(0, str(core.AGENT.parent / "scripts"))
        import setup_actions
        return setup_actions.summary_block()
    except Exception:  # noqa: BLE001
        return ""


_WIKI_MAP = {"mtime": 0.0, "text": ""}
WIKI_MAP_PER_SECTION = 30


def wiki_map() -> str:
    """What the wiki holds, by section, from Wiki/_meta/index.md (titles only): the agent should
    know what exists before it says it doesn't know (the owner 2026-10-09, 'fully acquainted with the
    wiki'). Cached on the index's mtime; a rebuilt index refreshes the model's context."""
    idx = core.VAULT / "Wiki" / "_meta" / "index.md"
    try:
        mt = idx.stat().st_mtime
    except OSError:
        return ""
    if mt == _WIKI_MAP["mtime"] and _WIKI_MAP["text"]:
        return _WIKI_MAP["text"]
    sections, cur = {}, ""
    for ln in idx.read_text(encoding="utf-8", errors="replace").splitlines():
        m = re.match(r"^##\s+(.+?)\s*$", ln)
        if m:
            cur = m.group(1).strip()
            continue
        t = re.match(r"^- \[\[([^\]]+)\]\]", ln)
        if t and cur:
            sections.setdefault(cur, []).append(t.group(1).strip())
    if not sections:
        return ""
    lines = ["WIKI MAP (every note under Wiki/, by folder; search with vault_search, open with Read `Wiki/<Folder>/<Title>.md`):"]
    for sec, titles in sections.items():
        shown = titles[:WIKI_MAP_PER_SECTION]
        more = len(titles) - len(shown)
        lines.append(f"- {sec} ({len(titles)}): " + "; ".join(shown) + (f"; +{more} more" if more else ""))
    _WIKI_MAP.update(mtime=mt, text="\n".join(lines))
    return _WIKI_MAP["text"]


def split_suggest(answer: str) -> tuple[str, dict]:
    """(reply text, suggest dict). The LAST line that starts with SUGGEST carries the JSON."""
    lines = str(answer or "").rstrip().splitlines()
    for i in range(len(lines) - 1, -1, -1):
        m = re.match(r"\s*`?SUGGEST\s*(\{.*)", lines[i])
        if m:
            raw = "\n".join([m.group(1)] + lines[i + 1:]).strip().rstrip("`").strip()
            try:
                sug = json.loads(raw)
            except ValueError:
                sug = {"_bad": raw[:300]}
            return "\n".join(lines[:i]).rstrip(), sug if isinstance(sug, dict) else {}
    return "\n".join(lines), {}


def _budget_ok() -> bool:
    d = core.load(ASKS, {})
    day = time.strftime("%Y-%m-%d")
    n = int(d.get(day, 0))
    if n >= int(core.profile().get("asks_per_day_max", 150)):
        return False
    core.save(ASKS, {day: n + 1})
    return True


def _run_claude(prompt: str, resume: str = "", model: str = "", timeout: int = 600, tools: tuple | None = None) -> dict:
    """One headless turn -> {"ok", "text", "session", "error"}. Tests replace this function.
    When the account is at its usage limit and profile.json names a `claude_account_fallback`,
    the same turn runs once more on that account (empty by default: the limit is then held and
    retried by the bridge, held.py). `tools` = (allowed, denied) for a staff turn; None = the owners' lists."""
    # the reflex (2026-10-27): the account known to have room goes first; a limit seen on one account is
    # remembered until its reset, so no later turn pays for a refused call. One try per account, in order.
    # An account that REFUSES the turn (sign-in gone, the organization switched Claude Code off) counts the
    # same as one at its limit: out for a while, the next account runs the turn (account.out_until).
    order = account.order()
    r = None
    for acct in order:
        r = _run_claude_on(prompt, resume, model, timeout, acct=acct, tools=tools)
        if r.get("ok") or not account.out_until(r.get("error") or ""):
            return r
        account.note_limit(acct, r.get("error") or "")
    if r is not None and len(order) > 1:
        r["both_limited"] = True
    return r or {"ok": False, "error": "no account"}


import threading


class _TurnSlot(threading.local):
    """The turn file of the chat turn in flight ON THIS THREAD (intents.py); "" for a sessionless run.
    Thread-local since the bridge runs one conversation per worker thread (2026-10-21)."""
    def __init__(self):
        self.path = ""
        self.tools = None          # (allowed, denied) for a staff turn on this thread; None = the owners' lists

    def __getitem__(self, k):
        return self.path if k == "path" else self.tools if k == "tools" else None

    def __setitem__(self, k, v):
        if k == "path":
            self.path = v
        elif k == "tools":
            self.tools = v


_TURN = _TurnSlot()
_SES_LOCK = threading.Lock()      # sessions.json is read-modify-written per turn; two conversations must not race
CTX_DIR = core.DATA / "ctx"       # the last context each conversation's session saw (for WHAT CHANGED deltas)
DELTA_MAX_SHARE = 0.4             # a delta bigger than this share of the full context is sent as a full refresh


def _run_claude_on(prompt: str, resume: str = "", model: str = "", timeout: int = 600, acct: str | None = None,
                   tools: tuple | None = None) -> dict:
    turn = _TURN["path"]
    if not Path(CLAUDE).exists():
        return {"ok": False, "error": "claude CLI not found"}
    p = core.profile()
    WORKDIR.mkdir(parents=True, exist_ok=True)
    tools = tools or _TURN["tools"]
    allowed, denied = tools or (ALLOWED, DENIED)
    allowed, denied = _apply_crm_tools(allowed, denied)
    cmd = [CLAUDE, "-p", "--model", model or p.get("model", "sonnet"), "--output-format", "json",
           "--max-turns", str(p.get("max_turns", 30))] + ([] if tools else ["--add-dir", str(core.VAULT)]) + [
           "--allowedTools", allowed, "--disallowedTools", denied]
    # calendar + mail reads and the deed tools: a private stdio server for this turn only (gmcp.py), never
    # registered machine-wide. AGENT_TURN tells it who is asking and where (intents.py); a sessionless run
    # (a routine, the dream) has no turn, so its deed tools answer "no context" and record nothing.
    server = {"command": sys.executable, "args": [str(core.AGENT / "gmcp.py")]}
    if turn:
        env_t = {"AGENT_TURN": str(turn), "AGENT_DATA": str(core.DATA), "AGENT_STATE": os.environ.get("AGENT_STATE", ""),
                 "ASSISTANT_DATA": os.environ.get("ASSISTANT_DATA", ""), "ASSISTANT_CARDS": os.environ.get("ASSISTANT_CARDS", "")}
        server["env"] = {k: v for k, v in env_t.items() if v}
    mcp_cfg = Path(turn).with_suffix(".mcp.json") if turn else MCP_CONFIG
    try:
        extra = _alib().plugin_map("MCP_SERVERS")          # the organization's own services (this organization: its CRM)
    except Exception:  # noqa: BLE001
        extra = {}
    core.save(mcp_cfg, {"mcpServers": {MCP_SERVER: server, **extra}})
    cmd += ["--mcp-config", str(mcp_cfg), "--strict-mcp-config"]   # only these servers; no user-scope servers in a turn
    if resume:
        cmd += ["--resume", resume]
    env, acct_note = account.env(acct)                     # which Claude subscription (profile.json claude_account)
    try:
        r = subprocess.run(creationflags=core.NO_WINDOW, args=cmd, input=prompt, capture_output=True, text=True, encoding="utf-8", errors="replace",
                           timeout=timeout, cwd=str(WORKDIR), env=env)
    except subprocess.TimeoutExpired:
        return {"ok": False, "error": "timeout"}
    out = (r.stdout or "").strip()
    for ln in reversed(out.splitlines() or [""]):
        try:
            d = json.loads(ln)
        except ValueError:
            continue
        if isinstance(d, dict) and "result" in d:
            return {"ok": not d.get("is_error"), "text": str(d.get("result") or ""), "account": acct_note,
                    "session": str(d.get("session_id") or ""), "error": str(d.get("result") or "")[:200] if d.get("is_error") else ""}
    return {"ok": False, "account": acct_note, "error": ((r.stderr or "") + out)[-300:] or f"exit {r.returncode}"}


def ask(person: dict, text: str, convo: str, channel: str = "", extra: str = "", sender: str = "",
        thread: str = "", card: dict | None = None, space: str = "", scope: str = "") -> dict:
    """A chat turn -> {"ok", "reply", "suggest", "error", "turn"}. `suggest` is what the deed tools
    recorded this turn (intents.collect); a trailing SUGGEST line is still honoured as the fallback."""
    if not _budget_ok():
        return {"ok": False, "reply": "I've used up today's questions; it resets at midnight.", "suggest": {}}
    turn = intents.begin(person, convo, thread, card, space)
    staff = not core.is_owner(person)
    tools = (STAFF_ALLOWED, STAFF_DENIED) if staff else ((BUSINESS_ALLOWED, STAFF_DENIED) if scope == "business" else None)
    ctx = context_block(person, scope)
    h = hashlib.sha1(ctx.encode("utf-8")).hexdigest()[:12]
    day = time.strftime("%Y-%m-%d")
    with _SES_LOCK:
        ses = core.load(SESSIONS, {})
    s = ses.get(convo) or {}
    fresh = not s.get("sid") or s.get("day") != day or session_big(s.get("sid", ""))
    head = f"[{time.strftime('%Y-%m-%d %H:%M %a')} | from {sender or person['name']}" + (f" via {channel}" if channel else "") + "]"
    if extra:                                              # the bridge's per-turn protocol (addressing, follow-ups)
        head += "\n" + extra.rstrip("\n")
    all_rows = core.rows(core.TRANSCRIPT)
    if fresh:
        recent = core.transcript_tail(convo, RECENT_N)
        tail = "\n".join(f"[{r['ts']}] {r['who']}: {r['text'][:500]}" for r in recent)
        prompt = ctx + ("\n\nRECENT CONVERSATION (before this session):\n" + tail if tail else "") + f"\n\n{head}\n{text}"
    else:
        # What happened in this conversation since the model's last turn that it did NOT see: cards
        # the system posted, taps, the doer's and builder's reports, routines. A resumed session
        # only ever got the owner's new line, so on 2026-10-08 Sage asked "who is 'her'?" about a
        # card it had posted 23 minutes earlier (the owner: "her context is still bad").
        since = since_last_turn(all_rows, int(s.get("row", 0)), convo, person, text)
        prompt = ""
        if s.get("ctx") != h:
            # only what changed since this session last saw the context (a rule learned, a fact, a connection),
            # not the whole ~20K tokens again (2026-10-21); a big change still gets the full refresh
            prompt = context_delta(convo, ctx) + "\n\n"
        if since:
            prompt += "SINCE YOUR LAST TURN (in this conversation, not from you; the newest may be what they are replying to):\n" + since + "\n\n"
        prompt += f"{head}\n{text}"
    timeout = int(core.profile().get("turn_timeout_s", 600))
    _TURN["path"] = str(turn)                          # the deed tools of this run record into this turn's file
    _TURN["tools"] = tools                             # a staff turn's narrower tool lists (thread-local, read by _run_claude_on)
    try:
        r = _run_claude(prompt, resume="" if fresh else s["sid"], timeout=timeout)
        if not r.get("ok") and not account.out_until(r.get("error") or "") and not intents.count(turn):
            # a lost session or a process that died (seen live: exit 0xC000013A on a first turn): start over
            # once, fresh. Not after a usage limit (held.py keeps the question), and not when the first run
            # already recorded deeds (a retry would double them).
            time.sleep(2)
            recent = core.transcript_tail(convo, RECENT_N)
            tail = "\n".join(f"[{x['ts']}] {x['who']}: {x['text'][:500]}" for x in recent)
            r = _run_claude(ctx + ("\n\nRECENT CONVERSATION:\n" + tail if tail else "") + f"\n\n{head}\n{text}", timeout=timeout)
    finally:
        _TURN["path"] = ""
        _TURN["tools"] = None
    if not r.get("ok"):
        return {"ok": False, "reply": "", "suggest": {}, "error": r.get("error", "unknown"), "account": r.get("account", "")}
    with _SES_LOCK:
        ses = core.load(SESSIONS, {})
        ses[convo] = {"sid": r.get("session") or s.get("sid", ""), "day": day, "ctx": h, "row": len(all_rows)}
        core.save(SESSIONS, ses)
    remember_context(convo, ctx)
    reply, sug = split_suggest(r["text"])
    recorded = intents.collect(turn)
    if recorded:
        # the deed tools are the record; a SUGGEST line on top only adds keys they did not cover
        sug = {**{k: v for k, v in sug.items() if k != "_bad"}, **recorded}
    return {"ok": True, "reply": reply, "suggest": sug, "account": r.get("account", ""), "turn": str(turn)}


def _ctx_path(convo: str) -> Path:
    return CTX_DIR / (re.sub(r"[^A-Za-z0-9]+", "-", str(convo or "none")).strip("-")[:80] + ".txt")


def remember_context(convo: str, ctx: str) -> None:
    """What this conversation's session has now seen, for the next delta."""
    try:
        CTX_DIR.mkdir(parents=True, exist_ok=True)
        _ctx_path(convo).write_text(ctx, encoding="utf-8")
    except OSError:
        pass


def context_delta(convo: str, ctx: str) -> str:
    """The lines added and removed since the session last saw the context, as one short block; the
    full context when nothing older is on record or the change is large (DELTA_MAX_SHARE)."""
    import difflib
    try:
        old = _ctx_path(convo).read_text(encoding="utf-8")
    except OSError:
        old = ""
    full = "[CONTEXT REFRESH: your rules, memory or schedules changed. The current set replaces the old one.]\n" + ctx
    if not old:
        return full
    added, removed = [], []
    for ln in difflib.unified_diff(old.splitlines(), ctx.splitlines(), lineterm="", n=0):
        if ln.startswith("+++") or ln.startswith("---") or ln.startswith("@@"):
            continue
        if ln.startswith("+") and ln[1:].strip():
            added.append(ln[1:])
        elif ln.startswith("-") and ln[1:].strip():
            removed.append(ln[1:])
    if not added and not removed:
        return full
    body = "\n".join((["ADDED:"] + ["+ " + a for a in added] if added else []) + (["REMOVED:"] + ["- " + r for r in removed] if removed else []))
    if len(body) > DELTA_MAX_SHARE * len(ctx):
        return full
    return ("[WHAT CHANGED since your last turn: these lines of your rules, memory, schedules, skills, organization "
            "or connections were added or removed; everything else you were given still stands.]\n" + body)


SESSION_MAX_BYTES = 1_500_000      # the property OS found a --resume'd 1.2 MB session made every turn slow


def session_file(sid: str) -> Path:
    """The transcript claude keeps for a session started in WORKDIR."""
    slug = re.sub(r"[^A-Za-z0-9]", "-", str(WORKDIR.resolve()))
    return Path.home() / ".claude" / "projects" / slug / f"{sid}.jsonl"


def session_big(sid: str) -> bool:
    """True when the resumed session's transcript has grown past SESSION_MAX_BYTES: start fresh
    (the last 20 transcript lines are re-sent) instead of paying for the whole day on every turn."""
    if not sid:
        return False
    try:
        return session_file(sid).stat().st_size > SESSION_MAX_BYTES
    except OSError:
        return False


SYSTEM_CHANNELS = ("card", "doer", "builder", "digest", "routine", "follow-up")


def since_last_turn(all_rows: list, row: int, convo: str, person: dict, text: str, limit: int = 12) -> str:
    """Transcript lines added after the model's last turn that it never saw: system lines in this
    conversation (and cards/reports posted in the home space, which the owners answer from a DM
    too), plus taps. Owner and agent lines are already in the model's own session, so they are
    left out, as is the line being answered now."""
    owners = {u["name"] for u in (core.people().get("users") or {}).values()}
    agent = core.profile().get("name", "Agent")
    home = core.home_space()
    out = []
    for r in all_rows[row:]:
        if r.get("convo") != convo and not (home and r.get("convo") == home and r.get("channel") in SYSTEM_CHANNELS):
            continue
        click = bool((r.get("meta") or {}).get("click"))
        if r.get("who") == agent and r.get("channel") not in SYSTEM_CHANNELS:
            continue
        if r.get("who") in owners and not click:
            continue
        out.append(f"[{r.get('ts', '')}] {r.get('who')}: {str(r.get('text') or '')[:600]}")
    return "\n".join(out[-limit:])


def run_once(prompt: str, model: str = "", with_context: bool = True, timeout: int = 600) -> dict:
    """A sessionless turn (a scheduled routine, the nightly dream). -> {"ok", "text", "error"}"""
    full = (context_block() + "\n\n" if with_context else "") + prompt
    return _run_claude(full, model=model, timeout=timeout)


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    if len(sys.argv) > 1 and sys.argv[1] == "context":
        print(context_block())

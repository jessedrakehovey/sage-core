"""onboarding.py - Discovery, Training, the never-list, the shadow week, go-live (sage-core, 2026-10-27).

the owner's plan had `tools`, `consultation` and `training` as three commands and a 20-question minimum. Here:
  discover   ONE conversation over the business functions (assistant/onboarding/discovery.json): what tool, and the
             catalog picks the access tier (catalog.py). "Where are your leads" and "what CRM" are the same answer.
  map        the capability map the owner can read: what the agent reads, writes, and what always waits for a tap.
  coverage   Training's stopping rule is COVERAGE, not a count: every stage of the business's lifecycle skeleton
             (assistant/onboarding/lifecycles/<type>.json) needs trigger, actor, channel, template, clock, verifier.
  train      asks the next missing field until coverage is 100%; answers land in data/onboarding.json (machine-
             readable), and `blueprint` renders them into a wiki note as a by-product, never the source.
  seed-wiki  the wiki schema's first custom vocabulary (agent/schema_growth.py): the lifecycle skeleton's `wiki` section
             (a type per core noun, tags from the stage names, a folder per type) plus the owner's own words for what the
             business tracks (discovery's `tracks` answer). Runs on its own after discover and train; idempotent.
  never      the never-list (agent/state/org.json -> never): seeded at maximum gating, loosened only by name.
  shadow     the shadow week: everything drafted and carded, nothing sent on its own (config.json shadow + switches).
  golive     the standing exceptions, switched on one by one, by name, with the date recorded.

Non-interactive: --answers <file.json> for discover ({function: "tool"}) and train ({stage: {field: value}}).
  python assistant/onboarding.py discover|map|coverage [--quiet]|train|never [--add ..]|shadow on|off|status|golive [--enable key]|blueprint
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE / "scripts"))
import catalog  # noqa: E402
import orgcfg  # noqa: E402

DATA = Path(os.environ["ASSISTANT_DATA"]) if os.environ.get("ASSISTANT_DATA") else HERE / "data"
STATE = DATA / "onboarding.json"
CONFIG = HERE / "config.json"
AGENT_STATE = Path(os.environ["AGENT_STATE"]) if os.environ.get("AGENT_STATE") else HERE / "agent" / "state"
ORG_LEARNED = AGENT_STATE / "org.json"
DISCOVERY = HERE / "onboarding" / "discovery.json"
LIFECYCLES = HERE / "onboarding" / "lifecycles"
FIELDS = ["trigger", "actor", "channel", "template", "clock", "verifier"]
FIELD_ASK = {
    "trigger": "What starts this stage? (an email, a form, a payment, a date, a reply, you saying so)",
    "actor": "Who does it today: you, a staff member, or should the agent do it (drafting, with your tap)?",
    "channel": "Where does it happen: email, text, the CRM, a call, in person?",
    "template": "The words you use. Paste a real recent example, or 'none' if there is no fixed wording.",
    "clock": "How fast must it happen? (within 2 hours; the same day; 14 days before; 2 days after)",
    "verifier": "How would we know it happened? (a reply, a record in the CRM, a paid invoice, a calendar event)",
}
NEVER_SEED = [
    "Nothing outward (email, text, post, message, form) goes without an owner's tap on a card.",
    "Never commit to a price, a date, a deadline or anything contractual; those are blanks for the owner.",
    "Never pay, transfer, refund or move money; never enter a credential anywhere.",
    "Never delete a file, a message or a record; never move original media.",
    "Never speak as the owner to a client about anything the owner has not already said.",
]
STANDING_SWITCHES = {
    "texts.enabled": "the standing client texts go out from the owner's phone without a card",
    "leads.auto_send_collection": "the priced collection goes to a lead on its own when the date is open",
    "cards.delivery": "weekly = cards held for one Monday digest; immediate = every card the minute it is posted",
}


def _load(p: Path, default):
    try:
        return json.loads(p.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        return default


def _save(p: Path, obj) -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(obj, indent=1, ensure_ascii=False) + "\n", encoding="utf-8")


def state() -> dict:
    s = _load(STATE, {}) or {}
    s.setdefault("discovery", {})
    s.setdefault("lifecycle", {"type": str(orgcfg.get("lifecycle") or "service-studio"), "stages": {}})
    s.setdefault("golive", [])
    return s


def lifecycle(kind: str) -> dict:
    d = _load(LIFECYCLES / f"{kind}.json", {})
    if not d.get("stages"):
        d = _load(LIFECYCLES / "service-studio.json", {"stages": []})
    return d


def _ask(prompt: str, answers: dict | None, key, default: str = "") -> str:
    if answers is not None:
        v = answers
        for k in (key if isinstance(key, tuple) else (key,)):
            v = v.get(k, {}) if isinstance(v, dict) else {}
        return str(v if isinstance(v, str) else (v or default))
    try:
        return input(prompt + " ").strip() or default
    except EOFError:
        return default


# ---------------------------------------------------------------- discovery
def discover(answers: dict | None = None, only: str = "") -> dict:
    s = state()
    cat = catalog.load()
    disc = _load(DISCOVERY, {"functions": []})
    for fn in disc.get("functions", []):
        fid = fn["id"]
        if only and fid != only:
            continue
        cur = s["discovery"].get(fid, {})
        if cur.get("tool") and answers is None and not only:
            continue
        tool = _ask(f"[{fid}] {fn['ask']} (examples: {', '.join(fn.get('examples') or [])[:80]}; 'none' / 'skip')", answers, fid, "")
        if not tool:
            continue
        if tool.lower() in ("none", "skip", "n/a", "-"):
            s["discovery"][fid] = {"tool": "", "status": "skipped", "when": time.strftime("%Y-%m-%d")}
            continue
        e = catalog.match(tool, cat)
        bt = catalog.best_tier(e) if e else None
        s["discovery"][fid] = {"tool": tool, "catalog_id": e["id"] if e else "", "tier": int(bt["tier"]) if bt else 0,
                               "path": bt.get("path") if bt else "", "status": "to-connect" if e else "uncatalogued",
                               "how": bt.get("how") if bt else "No catalog entry yet: the agent asks how you reach it today and writes one.",
                               "when": time.strftime("%Y-%m-%d")}
    try:
        crm_build(s)
    except Exception as e:  # noqa: BLE001   filing the build never costs the owner their Discovery answers
        print(f"CRM_BUILD_SKIPPED {type(e).__name__}: {str(e)[:120]}", file=sys.stderr)
    ask = disc.get("tracks") or {}
    if ask and not only and (answers is not None and answers.get("tracks") or answers is None and not s.get("tracks")):
        raw = answers.get("tracks") if answers is not None else _ask(f"[tracks] {ask.get('ask', '')} (examples: {', '.join(ask.get('examples') or [])})", None, "tracks", "")
        if raw:
            s["tracks"] = parse_tracks(raw)
    _save(STATE, s)
    seed_wiki(s)
    return s


def _crm_connected() -> bool:
    sys.path.insert(0, str(HERE / "scripts"))
    try:
        import crm
        return crm._connector() is not None
    except Exception:  # noqa: BLE001
        return False


def crm_build(s: dict, write=None) -> str:
    """The engine ships no CRM (assistant/CRM_CONTRACT.md). When Discovery names one and no connector is installed,
    file ONE build request so the builder writes `crm_<id>.py` against the contract. Returns the request's path
    ('' when none was needed). `write` is the wiki writer (tests pass a fake)."""
    d = (s.get("discovery") or {}).get("crm") or {}
    tool = str(d.get("tool") or "").strip()
    if not tool or d.get("status") == "skipped" or _crm_connected():
        return ""
    cid = str(d.get("catalog_id") or "") or "".join(ch for ch in tool.lower() if ch.isalnum())[:24] or "crm"
    if (s.get("crm_build") or {}).get("id") == cid:
        return ""
    how = str(d.get("how") or "")
    body = (f"## Build request\n\nThe owner tracks clients and projects in {tool}. Write the CRM connector "
            f"`assistant/scripts/crm_{cid}.py` against `assistant/CRM_CONTRACT.md` and add `crm_{cid}` to "
            f"`assistant/config.json -> plugins.modules`.\n\n"
            f"- Start with the snapshot (read-only): a refresh command in `SENSOR_CMDS` and a spine job that writes the "
            f"contract's JSON. Writes (messages, stage moves) come after, each one only behind an owner's tap.\n"
            f"- How {tool} connects (catalog): {how or 'not catalogued yet: ask the owner how they reach it and add a catalog entry.'}\n"
            f"- Done when `crm.connected()` is true, `crm.snapshot()` returns this business's projects, and the "
            f"tests in `assistant/tests/test_crm.py` pass against it.")
    lane = "business"
    if write is None:
        sys.path.insert(0, str(HERE / "agent"))
        import wiki_writer
        write = wiki_writer.write
        lane = (wiki_writer.lanes() or [lane])[0]
    x = write({"title": f"Build request Connect {tool} as the CRM"[:90], "type": "workflow", "lane": lane, "body": body}, by="onboarding")
    if x.get("status") not in ("applied", "appended"):
        return ""
    s["crm_build"] = {"id": cid, "tool": tool, "path": str(x.get("path") or ""), "when": time.strftime("%Y-%m-%d")}
    return s["crm_build"]["path"]


def parse_tracks(raw) -> list:
    """'Jobs, customers and units' / ["jobs", "units"] -> ["jobs", "customers", "units"] (the owner's own words)."""
    import re
    items = raw if isinstance(raw, list) else re.split(r",|;|/|\band\b|\n", str(raw or ""))
    out = []
    for x in items:
        x = re.sub(r"^(?:our|the|my|all|every)\s+", "", str(x).strip().lower()).strip(" .")
        if x and len(x) <= 40 and x not in out:
            out.append(x)
    return out[:12]


def seed_wiki(s: dict | None = None, commit: bool = True) -> dict:
    """Write the schema's first custom vocabulary from the learned business type and the owner's words (schema_growth.seed:
    linted, ledgered, undoable; what the schema already has is skipped, so a re-run adds only what is new). Never fatal."""
    s = s or state()
    kind = s["lifecycle"].get("type") or "service-studio"
    try:
        sys.path.insert(0, str(HERE / "agent"))
        import schema_growth
        return schema_growth.seed(lifecycle(kind), s.get("tracks") or [], by="onboarding", commit=commit)
    except Exception as e:  # noqa: BLE001
        return {"status": "skipped", "reason": f"{type(e).__name__}: {str(e)[:160]}"}


def capability_map(s: dict | None = None) -> str:
    s = s or state()
    cat = catalog.load()
    disc = _load(DISCOVERY, {"functions": []})
    lines = ["What the agent can reach, by business function:"]
    for fn in disc.get("functions", []):
        d = s["discovery"].get(fn["id"])
        if not d:
            lines.append(f"- {fn['id']}: not asked yet")
            continue
        if not d.get("tool"):
            lines.append(f"- {fn['id']}: skipped")
            continue
        e = cat.get(d.get("catalog_id") or "")
        bt = catalog.best_tier(e) if e else None
        if not bt:
            lines.append(f"- {fn['id']}: {d['tool']} (no catalog entry: owner's portal until one is written)")
            continue
        lines.append(f"- {fn['id']}: {e['name']} via tier {bt['tier']} ({catalog.TIER_NAMES[int(bt['tier'])]}), {d.get('status')}")
        lines.append(f"    reads: {', '.join(bt.get('reads') or []) or 'nothing yet'}")
        lines.append(f"    writes: {', '.join(bt.get('writes') or []) or 'nothing'}")
        lines.append(f"    always a card: {', '.join(bt.get('always_tap') or []) or 'every outbound message, every payment, every deletion'}")
    return "\n".join(lines)


# ---------------------------------------------------------------- training
def coverage(s: dict | None = None) -> dict:
    s = s or state()
    kind = s["lifecycle"].get("type") or "service-studio"
    skel = lifecycle(kind)
    stages = skel.get("stages", [])
    filled = s["lifecycle"].get("stages", {})
    total = len(stages) * len(FIELDS)
    have = 0
    missing = {}
    for st in stages:
        got = filled.get(st["id"], {})
        miss = [f for f in FIELDS if not str(got.get(f) or "").strip()]
        have += len(FIELDS) - len(miss)
        if miss:
            missing[st["id"]] = miss
    pct = int(round(100 * have / total)) if total else 0
    return {"type": kind, "name": skel.get("name", kind), "pct": pct, "stages": len(stages), "missing": missing, "complete": not missing and bool(stages)}


def train(answers: dict | None = None, only_stage: str = "", limit: int = 0) -> dict:
    s = state()
    kind = s["lifecycle"].get("type") or "service-studio"
    skel = lifecycle(kind)
    asked = 0
    for st in skel.get("stages", []):
        if only_stage and st["id"] != only_stage:
            continue
        got = s["lifecycle"]["stages"].setdefault(st["id"], {})
        for f in FIELDS:
            if str(got.get(f) or "").strip():
                continue
            prompt = f"[{st['name']}: {st.get('what', '')}] {FIELD_ASK[f]}" + (f" (typical: {st.get('typical_' + f)})" if st.get("typical_" + f) else "")
            v = _ask(prompt, answers, (st["id"], f), "")
            if v:
                got[f] = v
                got["when"] = time.strftime("%Y-%m-%d")
            asked += 1
            if limit and asked >= limit:
                _save(STATE, s)
                return coverage(s)
    _save(STATE, s)
    seed_wiki(s)
    return coverage(s)


# ---------------------------------------------------------------- the never-list, the shadow week, go-live
def never_list(add: str = "", seed: bool = True) -> list:
    org = _load(ORG_LEARNED, {}) or {}
    nev = [x for x in (org.get("never") or []) if isinstance(x, str)]
    if seed and not nev:
        nev = list(NEVER_SEED)
    if add and add not in nev:
        nev.append(add)
    org["never"] = nev
    org["updated"] = time.strftime("%Y-%m-%dT%H:%M:%S")
    _save(ORG_LEARNED, org)
    return nev


def _get(cfg: dict, dotted: str):
    v = cfg
    for k in dotted.split("."):
        v = v.get(k) if isinstance(v, dict) else None
    return v


def _set(cfg: dict, dotted: str, value) -> None:
    ks = dotted.split(".")
    d = cfg
    for k in ks[:-1]:
        d = d.setdefault(k, {})
    d[ks[-1]] = value


def shadow(mode: str) -> dict:
    cfg = _load(CONFIG, {}) or {}
    sh = cfg.setdefault("shadow", {})
    if mode == "on":
        sh.update({"on": True, "since": time.strftime("%Y-%m-%d")})
        if isinstance(cfg.get("texts"), dict):
            cfg["texts"]["enabled"] = False
        if isinstance(cfg.get("leads"), dict):
            cfg["leads"]["auto_send_collection"] = False
        _save(CONFIG, cfg)
    elif mode == "off":
        sh.update({"on": False, "ended": time.strftime("%Y-%m-%d")})
        _save(CONFIG, cfg)
    return {"on": bool(sh.get("on")), "since": sh.get("since", ""), "ended": sh.get("ended", "")}


def golive(enable: str = "", value: str = "") -> list:
    cfg = _load(CONFIG, {}) or {}
    s = state()
    if enable:
        if enable not in STANDING_SWITCHES:
            raise SystemExit(f"unknown switch {enable}; known: {', '.join(STANDING_SWITCHES)}")
        val = value if value else True
        if isinstance(val, str) and val.lower() in ("true", "false"):
            val = val.lower() == "true"
        _set(cfg, enable, val)
        _save(CONFIG, cfg)
        s["golive"].append({"switch": enable, "value": val, "when": time.strftime("%Y-%m-%dT%H:%M")})
        _save(STATE, s)
    rows = []
    for k, what in STANDING_SWITCHES.items():
        rows.append({"switch": k, "value": _get(cfg, k), "what": what})
    return rows


def _schema_tags() -> set:
    """The tags Schema/schema.md allows (its fenced ```tags block), so the blueprint's front matter passes lint."""
    try:
        import re
        m = re.search(r"```tags\n(.*?)```", (ROOT / "Schema" / "schema.md").read_text(encoding="utf-8"), re.S)
        return set(m.group(1).split()) if m else set()
    except OSError:
        return set()


def blueprint(s: dict | None = None) -> Path:
    s = s or state()
    cov = coverage(s)
    skel = lifecycle(cov["type"])
    org = orgcfg.get("name", "the organization")
    p = ROOT / "Wiki" / "Operations" / "Onboarding Blueprint.md"
    today = time.strftime("%Y-%m-%d")
    created = today
    if p.exists():                      # `created` never changes after creation (the schema's rule)
        import re
        m = re.search(r"^created: (\d{4}-\d{2}-\d{2})", p.read_text(encoding="utf-8", errors="replace"), re.M)
        created = m.group(1) if m else today
    allowed = _schema_tags()
    tags = [t for t in ("onboarding", "blueprint", "automation") if t in allowed][:2]
    # the full front matter Schema/schema.md requires (2026-10-29: the old title/date/status:complete header failed
    # lint, and the agent's note writer then refused every chat note in a new organization); completeness is a heading
    lines = ["---", "type: workflow", "area: operations", f"tags: [{', '.join(tags)}]", f"created: {created}", f"updated: {today}",
             "sources: [\"[[Onboarding Blueprint]]\"]", "status: draft", "---", "", f"# {org}: onboarding blueprint", "",
             "> [!info] Rendered by `assistant/onboarding.py blueprint` from `assistant/data/onboarding.json`. Edit the answers, not this note. "
             + ("Onboarding is **complete**." if cov["complete"] else "Onboarding is **in progress**."), "",
             "## Tools and access", "", capability_map(s).replace("What the agent can reach, by business function:", "").strip(), "",
             f"## Lifecycle: {cov['name']} ({cov['pct']}% covered)", ""]
    for st in skel.get("stages", []):
        got = s["lifecycle"]["stages"].get(st["id"], {})
        lines += [f"### {st['name']}", "", f"_{st.get('what', '')}_", ""]
        for f in FIELDS:
            lines.append(f"- **{f}:** {got.get(f) or '_missing_'}")
        lines.append("")
    try:                                # the folders the schema routes to, seeded from the lifecycle and grown from use
        sys.path.insert(0, str(HERE / "agent"))
        import schema_growth
        fl = [f for f in schema_growth.folders() if (ROOT / "Wiki" / f).is_dir()]
    except Exception:  # noqa: BLE001
        fl = []
    if fl:
        lines += ["## Wiki folders", "", "Where the agent files what it learns (the schema's routing; it grows as new kinds of notes are asked for).", ""]
        lines += [f"- [[Wiki/{f}/about|{f}]]" if (ROOT / "Wiki" / f / "about.md").exists() else f"- `Wiki/{f}/`" for f in fl] + [""]
    nev = never_list(seed=False)
    lines += ["## Never", ""] + [f"- {x}" for x in nev] + ["", "## Standing exceptions switched on", ""]
    lines += [f"- {g['switch']} = {g['value']} ({g['when']})" for g in s.get("golive", [])] or ["- none yet (shadow week)"]
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return p


# ---------------------------------------------------------------- in chat (2026-11-03)
# the owner: "easy google chat onboarding". Until now Discovery and Training ran only here, at a prompt. The agent's
# ONBOARDING context line (agent/org.py interview_hint) now carries the next unanswered Discovery function or Training
# field once the organization's own questions are done, the owner answers in their own words in chat, and the `onboard`
# deed (agent/gmcp.py, intents.py, actions.py) lands the answer here through the same discover() / train() code the
# terminal uses. One question a turn, a few a day, never a form. Switch: config.json -> onboarding.in_chat (new_org.py
# writes true; an organization long past onboarding sets false).
def in_chat() -> bool:
    try:
        cfg = _load(HERE / "config.json", {}) or {}
        v = (cfg.get("onboarding") or {}).get("in_chat")
        return True if v is None else bool(v)
    except Exception:  # noqa: BLE001
        return True


# ---------------------------------------------------------------- the feature gate (the owner 2026-11-03: "like a flow chart, if they
# don't want banking management or plaid then don't ask for plaid connection setup or budget questions, if they don't have a CRM
# don't ask those questions"). The FIRST onboarding question is which of these the owner wants the agent to handle; every
# Discovery function of a feature they did not pick is skipped as "not wanted" (never asked, no connection offered), and a feature
# that is an engine module (money: the bank feeds, the budget, the weekly check) is switched on or off with it. They can change
# their mind later in chat ("actually, do handle my money"): answering the gate again re-opens or closes the questions.
FEATURES = [
    # key, how the owner hears it, the Discovery functions it opens, the engine module it switches, words that mean it
    ("crm", "your clients and projects (leads, a CRM or booking tool, invoices, contracts, scheduling links)",
     ["lead_intake", "crm", "payments_invoicing", "contracts_esign", "scheduling_booking"], "", r"crm|clients?|leads?|bookings?|invoic|contracts?|projects?|customers?"),
    ("mail", "your email", ["mail"], "", r"e-?mails?|gmail|inbox|outlook"),
    ("calendar", "your calendar", ["calendar"], "", r"calendars?|appointments?"),
    ("texts", "texts from your phone", ["text_phone"], "", r"texts?|texting|sms|imessage|phone"),
    ("money", "your money: read-only bank feeds, an envelope budget and a weekly check (never pays anything)",
     ["banking", "accounting"], "money", r"money|bank|banking|budget|plaid|financ|accounting|bookkeeping|cash|spending"),
    ("files", "your documents and files", ["file_storage"], "", r"files?|documents?|drive|dropbox|storage"),
    ("team", "your team chat", ["team_chat"], "", r"team|staff|slack|contractors?"),
    ("social", "social media posts", ["social"], "", r"social|instagram|facebook|tiktok|posts?"),
    ("automation", "automations between your apps", ["automation"], "", r"automations?|zapier|make\.com|integrations?"),
]
_NEG = re.compile(r"(?:\b(?:no|not|never|without|skip|don't|dont|do not|nor)\b[^,.;]{0,24})$", re.I)


def features_state(s: dict | None = None) -> dict | None:
    """{"wanted": [keys], "when"} once the gate was answered; None before (nothing is gated for an organization that was
    never asked, so an existing one keeps every question and module it has)."""
    s = s or state()
    f = s.get("features")
    return f if isinstance(f, dict) and isinstance(f.get("wanted"), list) else None


def feature_wanted(key: str, s: dict | None = None) -> bool:
    f = features_state(s)
    return True if f is None else key in f["wanted"]


def parse_features(value: str) -> list:
    """The owner's words -> the feature keys they want. 'none' / 'nothing' -> []; 'all' / 'everything' -> every key; else each
    feature whose key or words appear, unless a 'no' / 'not' / 'without' stands right before it."""
    v = str(value or "").strip()
    low = v.lower()
    if re.fullmatch(r"\s*(?:none|nothing|no|nope|no thanks|neither)\s*[.!]?\s*", low):
        return []
    every = bool(re.search(r"\b(?:all|everything|all of (?:it|them|those|these))\b", low))
    minus = every and bool(re.search(r"\b(?:not|except|but|minus|apart from)\b", low))
    if every and not minus:
        return [k for k, *_ in FEATURES]
    named = []
    for key, _what, _fns, _mod, words in FEATURES:
        rx = re.compile(r"(?<![a-z])(?:" + key + "|" + words + r")", re.I)
        hits = [m for m in rx.finditer(v) if minus or not _NEG.search(v[max(0, m.start() - 30):m.start()])]
        if hits:
            named.append(key)
    if minus:                                   # "everything except social": all but what is named
        return [k for k, *_ in FEATURES if k not in named]
    return named


def _module(name: str, on: bool) -> None:
    """Switch an engine module with the feature (sage.py module on|off): its spine jobs and org.json -> modules."""
    sys.path.insert(0, str(HERE))
    import sage
    sage.module_switch(name, on)


def apply_features(wanted: list, s: dict | None = None) -> dict:
    """Record the gate and make it true: unwanted features' Discovery functions are skipped as "not wanted" (an answered one
    keeps its answer), a re-wanted feature's questions open again, a module feature is switched with it."""
    s = s or state()
    wanted = [k for k, *_ in FEATURES if k in set(wanted)]
    s["features"] = {"wanted": wanted, "when": time.strftime("%Y-%m-%d")}
    for key, _what, fns, mod, _words in FEATURES:
        for fid in fns:
            cur = s["discovery"].get(fid) or {}
            if key in wanted:
                if cur.get("status") == "skipped" and cur.get("reason") == "not wanted":
                    s["discovery"].pop(fid, None)               # asked again, now that they want it
            elif not cur.get("tool"):
                s["discovery"][fid] = {"tool": "", "status": "skipped", "reason": "not wanted", "when": time.strftime("%Y-%m-%d")}
        if mod:
            try:
                _module(mod, key in wanted)
            except Exception as e:  # noqa: BLE001   the gate is recorded either way; the module switch is reported
                print(f"MODULE_SWITCH_SKIPPED {mod}: {type(e).__name__}: {str(e)[:120]}", file=sys.stderr)
    _save(STATE, s)
    return s


def features_ask() -> str:
    return ("Which of these do you want me to handle? " + "; ".join(f"{w} ({k})" for k, w, *_ in FEATURES)
            + ". Any of them, all, or none; you can add one later.")


def remaining(s: dict | None = None) -> int:
    """How many Discovery functions and Training fields still have no answer."""
    s = s or state()
    disc = _load(DISCOVERY, {"functions": []})
    n = 0 if features_state(s) else 1
    n += sum(1 for fn in disc.get("functions", []) if not (s["discovery"].get(fn["id"], {}).get("tool") or s["discovery"].get(fn["id"], {}).get("status") == "skipped"))
    if disc.get("tracks") and not s.get("tracks"):
        n += 1
    c = coverage(s)
    return n + sum(len(v) for v in c["missing"].values())


def next_question(s: dict | None = None) -> dict | None:
    """The next thing Discovery or Training still needs, as one question for chat:
    {"kind": "tool"|"tracks"|"stage", "id", "field", "ask", "examples", "typical", "left"}; None when onboarding is done."""
    s = s or state()
    left = remaining(s)
    if features_state(s) is None:
        return {"kind": "features", "id": "features", "field": "features", "ask": features_ask(),
                "examples": ["clients, email and calendar", "all of it", "just my money", "none"], "typical": "", "left": left}
    disc = _load(DISCOVERY, {"functions": []})
    for fn in disc.get("functions", []):
        cur = s["discovery"].get(fn["id"], {})
        if cur.get("tool") or cur.get("status") == "skipped":
            continue
        return {"kind": "tool", "id": fn["id"], "field": "tool", "ask": str(fn.get("ask") or ""), "examples": list(fn.get("examples") or [])[:4],
                "typical": "", "left": left}
    tr = disc.get("tracks") or {}
    if tr and not s.get("tracks"):
        return {"kind": "tracks", "id": "tracks", "field": "tracks", "ask": str(tr.get("ask") or ""), "examples": list(tr.get("examples") or [])[:4],
                "typical": "", "left": left}
    kind = s["lifecycle"].get("type") or "service-studio"
    for st in lifecycle(kind).get("stages", []):
        got = s["lifecycle"]["stages"].get(st["id"], {})
        for f in FIELDS:
            if str(got.get(f) or "").strip():
                continue
            return {"kind": "stage", "id": st["id"], "field": f, "ask": f"About the {st['name']} stage ({st.get('what', '')}): {FIELD_ASK[f]}",
                    "examples": [], "typical": str(st.get("typical_" + f) or ""), "left": left}
    return None


def answer(kind: str, id_: str, field: str, value: str, by: str = "") -> dict:
    """An owner's answer from chat -> the same state discover() / train() write. -> {"ok", "line", "left"}."""
    v = re.sub(r"\s+", " ", str(value or "")).strip()[:600]
    kind, id_, field = str(kind or "").strip(), str(id_ or "").strip(), str(field or "").strip()
    if not v:
        return {"ok": False, "line": "nothing to record", "left": remaining()}
    before = json.loads(json.dumps(state()))
    if kind == "features":
        wanted = parse_features(v)
        s = apply_features(wanted)
        line = "handling: " + (", ".join(wanted) if wanted else "none of the optional features") + (" (money module on)" if "money" in wanted else "")
    elif kind == "tool":
        disc = _load(DISCOVERY, {"functions": []})
        fn = next((f for f in disc.get("functions", []) if f["id"] == id_), None)
        if not fn:
            return {"ok": False, "line": f"no business function called {id_ or '(blank)'}", "left": remaining()}
        s = discover(answers={id_: v}, only=id_)
        got = s["discovery"].get(id_, {})
        line = f"{id_.replace('_', ' ')}: " + ("none" if got.get("status") == "skipped" else f"{got.get('tool') or v} ({got.get('status') or 'noted'})")
    elif kind == "tracks":
        s = discover(answers={"tracks": v})
        line = "tracks: " + ", ".join(str(t.get("name") or t) if isinstance(t, dict) else str(t) for t in (s.get("tracks") or [])) if s.get("tracks") else "tracks: noted"
    elif kind == "stage":
        skel = lifecycle(state()["lifecycle"].get("type") or "service-studio")
        st = next((x for x in skel.get("stages", []) if x["id"] == id_), None)
        if not st or field not in FIELDS:
            return {"ok": False, "line": f"no stage {id_ or '(blank)'} / field {field or '(blank)'}", "left": remaining()}
        c = train(answers={id_: {field: v}}, only_stage=id_)
        line = f"{st['name']} {field}: {v[:80]}" + (f" (training {c['pct']}%)" if c else "")
    else:
        return {"ok": False, "line": f"unknown onboarding kind {kind!r}", "left": remaining()}
    left = remaining()
    cid = ""
    try:
        sys.path.insert(0, str(HERE / "agent"))
        import core as _core
        cid = _core.log_change("onboard", f"onboarding {kind} {id_}{(' ' + field) if kind == 'stage' else ''}: {v[:160]}",
                               {"op": "onboarding_restore", "before": before}, by or "chat")
    except Exception:  # noqa: BLE001   the answer is saved either way; the ledger line is a courtesy
        pass
    return {"ok": True, "line": line, "left": left, "change": cid}


def restore(before) -> bool:
    """Undo of one chat answer (core.undo op onboarding_restore): the state file as it was before it."""
    if not isinstance(before, dict):
        return False
    _save(STATE, before)
    return True


def chat_hint() -> str:
    """The ONBOARDING line for the model's context when the next unknown is a Discovery or Training question, or ''."""
    if not in_chat():
        return ""
    q = next_question()
    if not q:
        return ""
    ex = (" For example: " + ", ".join(str(e) for e in q["examples"]) + ".") if q.get("examples") else ""
    typ = (f" Typical: {q['typical']}.") if q.get("typical") else ""
    if q["kind"] == "features":
        return (f"ONBOARDING (first question; {q['left']} things unknown): if the moment allows, ask this in your own words, once, at the end "
                f"of your reply: \"{q['ask']}\" Save their answer with `onboard` (kind \"features\", id \"features\", field \"features\", "
                "value = the keys they want, comma-separated, or 'all' or 'none'). What they do not pick is never asked about and never set up; "
                "they can add one later by saying so. Never ask if they are busy with something urgent.")
    return (f"ONBOARDING (you are still learning how this organization works; {q['left']} things unknown): if the moment allows, ask this "
            f"in your own words, once, at the end of your reply: \"{q['ask']}\"{ex}{typ} They may answer 'none' or 'skip'. "
            f"Save their answer with `onboard` (kind \"{q['kind']}\", id \"{q['id']}\", field \"{q['field']}\", value = their words). "
            "Never ask if they are busy with something urgent.")


# ---------------------------------------------------------------- cli
def main() -> int:
    sys.stdout.reconfigure(encoding="utf-8")
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("discover"); p.add_argument("--answers", default=""); p.add_argument("--function", default="")
    sub.add_parser("map")
    p = sub.add_parser("coverage"); p.add_argument("--quiet", action="store_true"); p.add_argument("--json", action="store_true")
    p = sub.add_parser("train"); p.add_argument("--answers", default=""); p.add_argument("--stage", default=""); p.add_argument("--limit", type=int, default=0)
    p = sub.add_parser("never"); p.add_argument("--add", default="")
    p = sub.add_parser("shadow"); p.add_argument("mode", choices=["on", "off", "status"])
    p = sub.add_parser("golive"); p.add_argument("--enable", default=""); p.add_argument("--value", default="")
    sub.add_parser("blueprint")
    sub.add_parser("seed-wiki")
    sub.add_parser("next")
    sub.add_parser("features")
    p = sub.add_parser("answer"); p.add_argument("kind", choices=["features", "tool", "tracks", "stage"]); p.add_argument("id"); p.add_argument("field"); p.add_argument("value")
    a = ap.parse_args()
    if a.cmd == "features":
        f = features_state()
        print("not asked yet" if f is None else ("handling: " + (", ".join(f["wanted"]) or "none of the optional features") + f"  (since {f.get('when')})"))
    elif a.cmd == "next":
        q = next_question()
        print(json.dumps(q, indent=1, ensure_ascii=False) if q else "onboarding complete")
    elif a.cmd == "answer":
        r = answer(a.kind, a.id, a.field, a.value, by="cli")
        print(("ok   " if r["ok"] else "no   ") + r["line"] + f"  ({r['left']} left)")
        return 0 if r["ok"] else 1
    elif a.cmd == "discover":
        ans = _load(Path(a.answers), {}) if a.answers else None
        s = discover(ans, a.function)
        print(capability_map(s))
        print("\nYour own documents: put them in Raw/ (any type: md, pdf, docx, pptx, transcripts), then "
              "`python scripts/wiki_tool.py sources` lists what the wiki does not hold yet; say \"ingest\" in a Claude session "
              "(the wiki-ingest skill) to compile them into Wiki/ notes. Nothing is ingested on its own until the nightly dreaming job runs.")
    elif a.cmd == "map":
        print(capability_map())
    elif a.cmd == "coverage":
        c = coverage()
        if a.json:
            print(json.dumps(c, indent=1))
        elif not a.quiet:
            for st, miss in c["missing"].items():
                print(f"  {st:18} missing: {', '.join(miss)}")
        print(f"COVERAGE {c['pct']}% lifecycle={c['type']} stages={c['stages']} incomplete={len(c['missing'])}")
    elif a.cmd == "train":
        ans = _load(Path(a.answers), {}) if a.answers else None
        c = train(ans, a.stage, a.limit)
        print(f"COVERAGE {c['pct']}% lifecycle={c['type']} stages={c['stages']} incomplete={len(c['missing'])}" + ("  (training complete: run `shadow on`)" if c["complete"] else ""))
    elif a.cmd == "never":
        for x in never_list(a.add):
            print(f"- {x}")
    elif a.cmd == "shadow":
        r = shadow(a.mode)
        print(f"shadow week: {'ON since ' + r['since'] if r['on'] else 'off' + (' (ended ' + r['ended'] + ')' if r['ended'] else '')}")
    elif a.cmd == "golive":
        for r in golive(a.enable, a.value):
            print(f"  {r['switch']:28} = {json.dumps(r['value'])}   {r['what']}")
    elif a.cmd == "seed-wiki":
        r = seed_wiki()
        plan = r.get("plan") or {}
        print(f"SEED {r.get('status')} types={[t[0] for t in plan.get('types') or []]} tags={plan.get('tags') or []} "
              f"folders={r.get('folders') or []} {r.get('reason', '')}".rstrip())
    elif a.cmd == "blueprint":
        print(f"wrote {blueprint()}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
